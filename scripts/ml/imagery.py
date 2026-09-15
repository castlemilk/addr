"""High-resolution aerial imagery fetching with disk caching and window stitching.

For each output tile the fetcher downloads the covering imagery-zoom children in
parallel, stitches them into a pixel window that extends `margin_px` beyond the
tile on every side, and exposes exact pixel <-> tile-local coordinate transforms.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw

LOGGER = logging.getLogger("ml.imagery")

PROVIDERS: dict[str, str] = {
    "google": "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
    "google-hybrid": "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
    "esri": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}"
    ),
}

PROVIDER_MAX_ZOOM: dict[str, int] = {
    "google": 20,
    "google-hybrid": 20,
    "esri": 19,
}

CHILD_TILE_PX = 256


@dataclass
class ImageryWindow:
    """A stitched imagery window covering one output tile plus a margin."""

    image: Image.Image
    z: int
    x: int
    y: int
    imagery_zoom: int
    out_px: int
    margin_px: int
    scale: float
    base_px: int
    tile_span_meters: float

    @property
    def meters_per_pixel(self) -> float:
        return self.tile_span_meters / self.base_px

    def pixel_to_local(
        self, px: float, py: float, extent: int = 4096
    ) -> tuple[float, float]:
        units = extent / self.out_px
        return (px / self.scale - self.margin_px) * units, (
            py / self.scale - self.margin_px
        ) * units

    def local_to_pixel(self, lx: float, ly: float, extent: int = 4096) -> tuple[float, float]:
        units = self.out_px / extent
        return (lx * units + self.margin_px) * self.scale, (
            ly * units + self.margin_px
        ) * self.scale


class ImageryFetcher:
    """Fetches and caches aerial imagery tiles for a slippy map provider."""

    def __init__(
        self,
        provider: str = "google",
        cache_dir: str | Path = "data/imagery",
        imagery_offset: int = 1,
        out_px: int = 512,
        margin_px: int = 32,
        timeout: float = 20.0,
        retries: int = 3,
        workers: int = 8,
        procedural: bool = False,
        session: requests.Session | None = None,
    ):
        if provider not in PROVIDERS and not procedural:
            raise ValueError(f"unknown imagery provider: {provider}")
        self.provider = provider
        self.cache_dir = Path(cache_dir) / provider
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.imagery_offset = imagery_offset
        self.out_px = out_px
        self.margin_px = margin_px
        self.timeout = timeout
        self.retries = retries
        self.workers = workers
        self.procedural = procedural
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            "addr-ai-building-pipeline/1.0 (+https://github.com/benebsworth/addr)",
        )
        self.fetch_count = 0
        self.cache_hits = 0

    def window(self, z: int, x: int, y: int) -> ImageryWindow:
        imagery_zoom = min(z + self.imagery_offset, PROVIDER_MAX_ZOOM.get(self.provider, z))
        imagery_zoom = max(imagery_zoom, z)
        base_px = CHILD_TILE_PX << (imagery_zoom - z)
        scale = base_px / self.out_px
        margin_img = self.margin_px * scale
        size = base_px + 2.0 * margin_img
        left = x * base_px - margin_img
        top = y * base_px - margin_img

        first_tx = math.floor(left / CHILD_TILE_PX)
        first_ty = math.floor(top / CHILD_TILE_PX)
        last_tx = math.floor((left + size - 1) / CHILD_TILE_PX)
        last_ty = math.floor((top + size - 1) / CHILD_TILE_PX)

        width = int(round(size))
        height = int(round(size))
        canvas = Image.new("RGB", (width, height), (26, 32, 44))

        coords = [
            (tx, ty)
            for ty in range(first_ty, last_ty + 1)
            for tx in range(first_tx, last_tx + 1)
        ]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            images = list(pool.map(lambda c: self._child(imagery_zoom, c[0], c[1]), coords))

        missing = 0
        for (tx, ty), child in zip(coords, images, strict=True):
            if child is None:
                missing += 1
                continue
            paste_x = int(round(tx * CHILD_TILE_PX - left))
            paste_y = int(round(ty * CHILD_TILE_PX - top))
            canvas.paste(child, (paste_x, paste_y))

        if missing:
            LOGGER.warning(
                "imagery incomplete for tile %s/%s/%s: %d of %d children unavailable",
                z,
                x,
                y,
                missing,
                len(coords),
            )

        from .tiles import tile_span_meters

        lat = _tile_center_lat(y, z)
        return ImageryWindow(
            image=canvas,
            z=z,
            x=x,
            y=y,
            imagery_zoom=imagery_zoom,
            out_px=self.out_px,
            margin_px=self.margin_px,
            scale=scale,
            base_px=base_px,
            tile_span_meters=tile_span_meters(lat, z),
        )

    def _child(self, z: int, x: int, y: int) -> Image.Image | None:
        if self.procedural:
            return procedural_tile(z, x, y)
        path = self.cache_dir / str(z) / f"{x}_{y}.jpg"
        if path.exists():
            self.cache_hits += 1
            try:
                with Image.open(path) as cached:
                    return cached.convert("RGB")
            except OSError:
                path.unlink(missing_ok=True)

        url = PROVIDERS[self.provider].format(z=z, x=x, y=y)
        last_error: Exception | None = None
        for _ in range(self.retries):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                content = response.content
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                self.fetch_count += 1
                with Image.open(io.BytesIO(content)) as img:
                    return img.convert("RGB")
            except Exception as error:
                last_error = error
        if last_error is not None:
            LOGGER.debug("imagery fetch failed for %s: %s", url, last_error)
        return None


def _tile_center_lat(y: int, z: int) -> float:
    from .tiles import tile_xy_to_lonlat

    _, lat = tile_xy_to_lonlat(0.5, y + 0.5, z)
    return lat


def procedural_tile(z: int, x: int, y: int, size: int = CHILD_TILE_PX) -> Image.Image:
    """Deterministic synthetic aerial tile used for offline builds and tests."""
    seed = int(hashlib.sha1(f"{z}/{x}/{y}".encode()).hexdigest()[:8], 16)
    image = Image.new("RGB", (size, size), (74, 104, 57))
    draw = ImageDraw.Draw(image)

    for i in range(6):
        shade = 60 + ((seed >> (i * 3)) & 0x1F)
        x0 = ((seed >> (i * 4)) % size) - size // 4
        y0 = ((seed >> (i * 5)) % size) - size // 4
        draw.ellipse(
            [x0, y0, x0 + size // 2, y0 + size // 2],
            fill=(40 + shade % 30, 90 + shade % 40, 45 + shade % 25),
        )

    bx = size // 4 + (seed % (size // 8))
    by = size // 4 + ((seed >> 3) % (size // 8))
    bw = size // 3
    bh = size // 3
    draw.rectangle([bx, by, bx + bw, by + bh], fill=(51, 65, 85))
    draw.line([bx, by, bx + bw, by + bh], fill=(148, 163, 184), width=2)
    draw.line([bx + bw, by, bx, by + bh], fill=(148, 163, 184), width=2)
    draw.rectangle([bx, by, bx + bw, by + bh], outline=(203, 213, 225), width=2)

    draw.rectangle(
        [size * 0.7, 0, size, size * 0.12], fill=(203, 213, 225)
    )
    return image


def procedural_window(z: int, x: int, y: int, out_px: int = 512) -> Image.Image:
    """Single deterministic image covering a whole tile for fully offline runs."""
    return procedural_tile(z, x, y, size=out_px)


def image_to_array(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGB"), dtype=np.uint8)
