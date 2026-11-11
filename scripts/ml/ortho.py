"""Multi-source ortho composite for roofline extraction.

The TU Delft pipeline works on true orthophotos (10 cm, nadir, no shadows).
Our closest substitute is a median composite of all available ortho providers
(Google + ESRI): transient shadows, seasonal foliage and provider-specific
resampling noise all fall out in the median, so the edges LSD needs survive.

Both providers are already georeferenced; small mis-registration (a pixel or
two) is corrected by phase correlation before compositing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import cv2
import numpy as np
from PIL import Image

from .imagery import (
    PROVIDER_MAX_ZOOM,
    ImageryFetcher,
    ImageryWindow,
)

DEFAULT_PROVIDERS = ("google", "esri")


class OrthoCompositor:
    """Fetches the same slippy tile from several providers and fuses them."""

    def __init__(
        self,
        *,
        providers: Sequence[str] = DEFAULT_PROVIDERS,
        zoom: int = 18,
        out_px: int = 576,
        margin_px: int = 32,
        imagery_offset: int = 1,
    ):
        self.providers = tuple(providers)
        self.zoom = zoom
        offset = imagery_offset
        self.fetchers = {
            provider: ImageryFetcher(
                provider=provider,
                imagery_offset=offset,
                out_px=out_px,
                margin_px=margin_px,
            )
            for provider in self.providers
        }

    def window(self, x: int, y: int) -> tuple[ImageryWindow, int]:
        """Returns (fused window, number of providers that contributed)."""
        zoom = self.zoom
        base: ImageryWindow | None = None
        arrays: list[np.ndarray] = []
        for provider in self.providers:
            fetcher = self.fetchers[provider]
            if zoom > PROVIDER_MAX_ZOOM.get(provider, zoom):
                continue
            window = fetcher.window(zoom, x, y)
            if base is None:
                base = window
                arrays.append(np.asarray(window.image.convert("RGB"), dtype=np.float32))
                continue
            candidate = np.asarray(window.image.convert("RGB"), dtype=np.float32)
            if candidate.shape != arrays[0].shape:
                candidate = cv2.resize(
                    candidate,
                    (arrays[0].shape[1], arrays[0].shape[0]),
                    interpolation=cv2.INTER_LINEAR,
                )
            aligned = align_to(arrays[0], candidate)
            arrays.append(aligned)
        if base is None or not arrays:
            raise ValueError("no provider could serve the requested window")
        if len(arrays) == 1:
            fused = arrays[0]
        else:
            fused = np.median(np.stack(arrays, axis=0), axis=0)
        image = Image.fromarray(np.clip(fused, 0, 255).astype(np.uint8))
        return replace(base, image=image), len(arrays)


def align_to(reference: np.ndarray, candidate: np.ndarray, max_shift: int = 8) -> np.ndarray:
    """Phase-correlates candidate onto reference and warps it (subpixel)."""
    ref_gray = cv2.cvtColor(reference.astype(np.uint8), cv2.COLOR_RGB2GRAY)
    cand_gray = cv2.cvtColor(candidate.astype(np.uint8), cv2.COLOR_RGB2GRAY)
    window = cv2.createHanningWindow(
        (ref_gray.shape[1], ref_gray.shape[0]), cv2.CV_32F
    )
    (dx, dy), _ = cv2.phaseCorrelate(
        ref_gray.astype(np.float32), cand_gray.astype(np.float32), window
    )
    if not np.isfinite(dx) or not np.isfinite(dy):
        return candidate
    if abs(dx) > max_shift or abs(dy) > max_shift:
        return candidate
    matrix = np.float32([[1, 0, -dx], [0, 1, -dy]])
    warped = cv2.warpAffine(
        candidate,
        matrix,
        (candidate.shape[1], candidate.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return warped


def composite_gray(image: Image.Image) -> np.ndarray:
    """Edge-preserving grayscale for line detection (CLAHE on luminance)."""
    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)
