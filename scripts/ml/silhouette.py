"""Silhouette refinement: snap footprint rings to the orthophoto edges.

The paper uses cadastral footprints; our footprints come from an AI detector
and are often blobby or offset from the roof edge. This module pulls each ring
vertex onto the strongest nearby image gradient along the vertex normal (the
classic active-contour snap), with a distance penalty, a cap on per-vertex
movement and a light smoothing pass so the outline stays simple.

It also reports split candidates for merged blobs: internal rooflines whose
two sides differ in height by more than a threshold are evidence that the
footprint contains more than one building (or a building plus a lower annex).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np
from PIL import ImageDraw
from shapely.geometry import Polygon

from .imagery import ImageryFetcher, ImageryWindow
from .ortho import OrthoCompositor, composite_gray
from .rooflines import Frame

SEARCH_M = 1.6
STEP_M = 0.15
MAX_SHIFT_M = 1.8
DISTANCE_PENALTY = 0.55


def _gradient_magnitude(window: ImageryWindow) -> np.ndarray:
    gray = composite_gray(window.image).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = np.hypot(gx, gy)
    if magnitude.max() > 0:
        magnitude = magnitude / magnitude.max()
    return magnitude


def _bilinear(image: np.ndarray, x: float, y: float) -> float:
    height, width = image.shape
    if x < 0 or y < 0 or x >= width - 1 or y >= height - 1:
        return 0.0
    x0, y0 = int(x), int(y)
    fx, fy = x - x0, y - y0
    top = image[y0, x0] * (1 - fx) + image[y0, x0 + 1] * fx
    bottom = image[y0 + 1, x0] * (1 - fx) + image[y0 + 1, x0 + 1] * fx
    return float(top * (1 - fy) + bottom * fy)


def snap_ring(
    ring: list[list[float]],
    window: ImageryWindow,
    frame: Frame,
    *,
    search_m: float = SEARCH_M,
    step_m: float = STEP_M,
    max_shift_m: float = MAX_SHIFT_M,
    passes: int = 2,
) -> dict:
    """Snaps each ring vertex to the strongest nearby edge along its normal."""
    magnitude = _gradient_magnitude(window)
    points = [np.array(point, dtype=float) for point in ring]
    if len(points) < 4:
        return {"ring": ring, "mean_shift_m": 0.0, "iou": 1.0, "moved": 0}
    original = [(float(x), float(y)) for x, y in points]
    if search_m <= 0 or step_m <= 0 or max_shift_m < 0:
        raise ValueError("search_m and step_m must be positive; max_shift_m nonnegative")
    offsets = np.unique(np.append(np.arange(-search_m, search_m + 1e-9, step_m), 0.0))
    for _ in range(max(1, passes)):
        moved: list[np.ndarray] = []
        for index, point in enumerate(points):
            previous = points[(index - 1) % len(points)]
            following = points[(index + 1) % len(points)]
            tangent = following - previous
            norm = float(np.hypot(*tangent))
            if norm < 1e-6:
                moved.append(point)
                continue
            normal = np.array([-tangent[1], tangent[0]]) / norm
            # outward normal: away from the centroid
            centroid = np.mean(np.array(points), axis=0)
            if float(np.dot(normal, point - centroid)) < 0:
                normal = -normal
            best_score = 0.0
            best_delta = 0.0
            for delta in offsets:
                candidate = point + normal * float(delta)
                px, py = frame.metre_to_pixel(float(candidate[0]), float(candidate[1]))
                score = _bilinear(magnitude, px, py) - DISTANCE_PENALTY * abs(delta) / search_m
                if score > best_score:
                    best_score = score
                    best_delta = float(delta)
            shift = max(-max_shift_m, min(max_shift_m, best_delta))
            if best_score <= 0:
                shift = 0.0
            moved.append(point + normal * shift)
        # Never average roof corners: Laplacian smoothing shrinks rectangles and
        # fills concave courtyards even on a completely blank image. Bound total
        # movement from the original vertex, across all refinement passes.
        points = []
        for origin, candidate in zip(original, moved, strict=True):
            delta = candidate - np.array(origin)
            distance = float(np.linalg.norm(delta))
            points.append(np.array(origin) + delta * min(1.0, max_shift_m / max(distance, 1e-9)))

    new_ring = [[round(float(x), 2), round(float(y), 2)] for x, y in points]
    polygon_old = Polygon(original)
    polygon_new = Polygon([(x, y) for x, y in new_ring])
    if not polygon_new.is_valid or polygon_new.area < polygon_old.area * 0.4:
        return {"ring": ring, "mean_shift_m": 0.0, "iou": 1.0, "moved": 0}

    def ring_score(points_list) -> float:
        scores = []
        for x, y in points_list:
            px, py = frame.metre_to_pixel(float(x), float(y))
            scores.append(_bilinear(magnitude, px, py))
        return float(np.mean(scores)) if scores else 0.0

    score_before = ring_score(original)
    score_after = ring_score(new_ring)
    shifts = [
        math.hypot(new_ring[i][0] - original[i][0], new_ring[i][1] - original[i][1])
        for i in range(len(original))
    ]
    intersection = polygon_old.intersection(polygon_new)
    union = polygon_old.union(polygon_new)
    iou = float(intersection.area / union.area) if union.area > 0 else 1.0
    return {
        "ring": new_ring,
        "mean_shift_m": round(float(np.mean(shifts)), 2),
        "max_shift_m": round(float(np.max(shifts)), 2),
        "moved": int(sum(1 for shift in shifts if shift > 0.15)),
        "iou": round(iou, 3),
        "area_before_m2": round(polygon_old.area, 1),
        "area_after_m2": round(polygon_new.area, 1),
        "edge_score_before": round(score_before, 4),
        "edge_score_after": round(score_after, 4),
    }


def split_candidates(parts: list[dict], min_drop_m: float = 0.9, min_edge_m: float = 3.0) -> list[dict]:
    """Adjacent roofline parts whose planes differ enough to be separate roofs."""
    candidates = []
    for i, first in enumerate(parts):
        plane_a = first.get("plane")
        if not plane_a:
            continue
        for j in range(i + 1, len(parts)):
            second = parts[j]
            plane_b = second.get("plane")
            if not plane_b:
                continue
            drop = abs(
                (plane_a["a"] * first["centroid"][0] + plane_a["b"] * first["centroid"][1] + plane_a["c"])
                - (plane_b["a"] * second["centroid"][0] + plane_b["b"] * second["centroid"][1] + plane_b["c"])
            )
            if drop >= min_drop_m:
                candidates.append(
                    {
                        "part_a": i,
                        "part_b": j,
                        "height_drop_m": round(drop, 2),
                        "area_a_m2": first["area_m2"],
                        "area_b_m2": second["area_m2"],
                    }
                )
    return candidates


def _load_record(records_path: Path, address_id: int) -> dict:
    for record in json.loads(records_path.read_text()):
        if record["address_id"] == address_id:
            return record
    raise SystemExit(f"address {address_id} not found in {records_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=Path("public/address-roofs.json"))
    parser.add_argument("--address-id", type=int, required=True)
    parser.add_argument("--zoom", type=int, default=18)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--debug", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    record = _load_record(args.records, args.address_id)
    zoom = args.zoom
    n = 2**zoom
    centroid = record["centroid"]
    tile_x = int((centroid["lon"] + 180) / 360 * n)
    lat_rad = math.radians(centroid["lat"])
    tile_y = int(
        (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n
    )
    compositor = OrthoCompositor(zoom=zoom)
    try:
        window, _ = compositor.window(tile_x, tile_y)
    except Exception:  # noqa: BLE001
        window = ImageryFetcher(provider="google", imagery_offset=1, out_px=576, margin_px=32).window(zoom, tile_x, tile_y)
    frame = Frame(record, window, zoom, tile_x, tile_y)

    result = snap_ring(record["ring_meters"], window, frame)
    output = {
        "address_id": record["address_id"],
        "full_address": record["full_address"],
        "before": record["ring_meters"],
        "after": result["ring"],
        **{key: value for key, value in result.items() if key != "ring"},
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(output, indent=2))
    if args.debug:
        image = window.image.copy().convert("RGB")
        draw = ImageDraw.Draw(image, "RGBA")
        for ring, colour in ((record["ring_meters"], (239, 68, 68, 230)), (result["ring"], (34, 197, 94, 230))):
            pixels = [frame.metre_to_pixel(x, y) for x, y in ring]
            draw.line(pixels + [pixels[0]], fill=colour, width=3)
        args.debug.parent.mkdir(parents=True, exist_ok=True)
        image.save(args.debug)
    print(
        f"address={record['address_id']} moved={result['moved']}/{len(record['ring_meters'])} "
        f"mean_shift={result['mean_shift_m']}m max={result['max_shift_m']}m iou={result['iou']} "
        f"area {result['area_before_m2']} -> {result['area_after_m2']} m2"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
