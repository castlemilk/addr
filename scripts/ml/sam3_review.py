"""Standalone SAM3 roof-boundary review.

Runs the SAM 3 text-promptable segmenter separately from the tile pipeline so
extracted roof outlines can be inspected and validated before being baked into
an MBTiles archive. For each tile it writes:

  * ``preview_{z}_{x}_{y}.png`` — imagery with existing archive footprints and
    SAM3 proposals overlaid (green = SAM3 extra, yellow = matched replacement,
    red = existing/missed) plus per-detection IoU labels
  * ``sam3_proposals.geojson`` — SAM3 roof polygons in WGS84 with score, area, and
    match status against the existing archive
  * ``report.json`` — per-tile and aggregate match statistics (mean IoU,
    replaced/added/missed counts, area deltas)
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import mapbox_vector_tile
from PIL import ImageDraw
from shapely.geometry import Polygon
from shapely.validation import make_valid

from .imagery import ImageryFetcher, ImageryWindow
from .sam3 import Sam3TextSegmenter
from .segmentation import mask_to_polygons
from .tiles import iter_tiles, tile_local_to_lonlat

REPLACE_COLOR = (250, 204, 21)
EXTRA_COLOR = (22, 163, 74)
EXISTING_COLOR = (220, 38, 38)
BOX_COLOR = (34, 211, 238)
TEXT_COLOR = (15, 23, 42)
TEXT_STROKE = (255, 255, 255)


def clean_polygon(polygon: Polygon) -> Polygon | None:
    """Repairs self-intersecting rings and keeps the largest polygon part."""
    if polygon is None or polygon.is_empty:
        return None
    if polygon.is_valid:
        return polygon
    repaired = make_valid(polygon)
    if isinstance(repaired, Polygon):
        return repaired if not repaired.is_empty else None
    parts = [
        part
        for part in getattr(repaired, "geoms", [])
        if isinstance(part, Polygon)
    ]
    if not parts:
        return None
    return max(parts, key=lambda part: part.area)


@dataclass
class ReviewProposal:
    polygon: Polygon
    score: float
    area_m2: float
    box_px: tuple[float, float, float, float] | None = None
    prompt: str = ""


@dataclass
class MatchResult:
    status: str  # "replace" | "weak" | "extra"
    iou: float
    existing_index: int | None
    area_delta_ratio: float


def match_proposals(
    existing: list[Polygon],
    proposals: list[ReviewProposal],
    min_iou: float = 0.3,
) -> tuple[list[MatchResult], list[MatchResult], list[int]]:
    """Matches SAM3 proposals against existing footprints.

    Returns per-proposal results, per-existing results, and the indexes of
    proposals that replace an existing footprint.
    """
    proposal_results: list[MatchResult] = []
    existing_results: list[MatchResult] = [
        MatchResult("missed", 0.0, None, 0.0) for _ in existing
    ]
    replaced: list[int] = []

    for proposal_index, proposal in enumerate(proposals):
        best_index = -1
        best_iou = 0.0
        for index, footprint in enumerate(existing):
            if proposal.polygon.area <= 0 or footprint.area <= 0:
                continue
            union = proposal.polygon.union(footprint).area
            if union <= 0:
                continue
            iou = proposal.polygon.intersection(footprint).area / union
            if iou > best_iou:
                best_iou = iou
                best_index = index

        if best_index >= 0 and best_iou >= min_iou:
            ratio = proposal.polygon.area / max(existing[best_index].area, 1e-6)
            if 0.5 <= ratio <= 2.0:
                proposal_results.append(MatchResult("replace", best_iou, best_index, ratio))
                existing_results[best_index] = MatchResult("replace", best_iou, proposal_index, ratio)
                replaced.append(proposal_index)
                continue
            proposal_results.append(MatchResult("weak", best_iou, best_index, ratio))
            if existing_results[best_index].status == "missed":
                existing_results[best_index] = MatchResult("weak", best_iou, proposal_index, ratio)
            continue

        proposal_results.append(MatchResult("extra", best_iou, best_index if best_index >= 0 else None, 0.0))
        if best_index >= 0 and existing_results[best_index].status == "missed":
            existing_results[best_index] = MatchResult("weak", best_iou, proposal_index, 0.0)

    return proposal_results, existing_results, replaced


def to_pixel_space(window: ImageryWindow, polygons: list[Polygon]) -> list[Polygon]:
    """Converts tile-local (MVT, 0..4096) polygons to output-image pixel space."""
    converted: list[Polygon] = []
    for polygon in polygons:
        exterior = [window.local_to_pixel(lx, ly) for lx, ly in polygon.exterior.coords]
        interiors = [
            [window.local_to_pixel(lx, ly) for lx, ly in ring.coords]
            for ring in polygon.interiors
        ]
        cleaned = clean_polygon(Polygon(exterior, interiors))
        if cleaned is not None and not cleaned.is_empty:
            converted.append(cleaned)
    return converted


def load_existing_footprints(
    archive: Path, z: int, x: int, y: int
) -> list[Polygon]:
    connection = sqlite3.connect(f"file:{archive}?mode=ro", uri=True)
    row = connection.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, (1 << z) - 1 - y),
    ).fetchone()
    connection.close()
    if row is None:
        return []
    data = row[0]
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    layers = mapbox_vector_tile.decode(data)
    layer = layers.get("building")
    if not layer:
        return []
    polygons: list[Polygon] = []
    for feature in layer["features"]:
        geometry = feature["geometry"]
        parts = (
            [geometry["coordinates"]]
            if geometry["type"] == "Polygon"
            else geometry["coordinates"]
        )
        for part in parts:
            polygon = Polygon(part[0], part[1:])
            cleaned = clean_polygon(polygon)
            if cleaned is not None and not cleaned.is_empty:
                polygons.append(cleaned)
    return polygons


def draw_ring(
    draw: ImageDraw.ImageDraw,
    polygon: Polygon,
    color: tuple[int, int, int],
    width: int = 3,
) -> None:
    """Draws a polygon already converted to output-image pixel space."""
    points = [tuple(point) for point in polygon.exterior.coords]
    if len(points) >= 2:
        draw.line(points, fill=color, width=width)


def draw_box(
    draw: ImageDraw.ImageDraw,
    box_px: tuple[float, float, float, float],
    color: tuple[int, int, int],
    width: int = 2,
) -> None:
    x0, y0, x1, y1 = box_px
    for offset in range(width):
        draw.rectangle([x0 - offset, y0 - offset, x1 + offset, y1 + offset], outline=color)


def write_preview(
    window: ImageryWindow,
    z: int,
    x: int,
    y: int,
    existing_px: list[Polygon],
    proposals: list[ReviewProposal],
    existing_results: list[MatchResult],
    proposal_results: list[MatchResult],
    out_dir: Path,
) -> None:
    image = window.image.copy()
    draw = ImageDraw.Draw(image)

    for index, polygon in enumerate(existing_px):
        status = existing_results[index].status if index < len(existing_results) else "missed"
        color = REPLACE_COLOR if status == "replace" else EXISTING_COLOR
        draw_ring(draw, polygon, color, width=3 if status != "missed" else 2)

    for index, proposal in enumerate(proposals):
        result = proposal_results[index] if index < len(proposal_results) else None
        replaced = bool(result and result.status == "replace")
        mask_color = REPLACE_COLOR if replaced else EXTRA_COLOR
        draw_ring(draw, proposal.polygon, mask_color, width=3 if not replaced else 2)
        if proposal.box_px is not None:
            draw_box(draw, proposal.box_px, BOX_COLOR)
            label = f"{proposal.prompt} {proposal.score:.2f} {proposal.area_m2:.0f}m2"
            if result and result.status == "weak" and result.iou > 0:
                label = f"{proposal.prompt} IoU {result.iou:.2f}"
            draw.text(
                (proposal.box_px[0], max(0.0, proposal.box_px[1] - 18)),
                label,
                fill=BOX_COLOR,
                stroke_width=2,
                stroke_fill=TEXT_STROKE,
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    image.save(out_dir / f"preview_{z}_{x}_{y}.png")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbox", type=str, required=True, help="west,south,east,north")
    parser.add_argument("--zooms", type=str, default="16")
    parser.add_argument("--provider", default="google")
    parser.add_argument("--imagery-offset", type=int, default=3)
    parser.add_argument(
        "--prompt",
        "--prompts",
        dest="prompts",
        default="house",
        help="comma-separated SAM3 prompts, e.g. house,tree",
    )
    parser.add_argument("--threshold", type=float, default=0.4)
    parser.add_argument("--min-iou", type=float, default=0.3)
    parser.add_argument("--models-dir", type=Path, default=None)
    parser.add_argument(
        "--existing",
        type=Path,
        default=None,
        help="MBTiles archive to compare against (e.g. data/australia_tiles.mbtiles)",
    )
    parser.add_argument("--out", type=Path, default=Path("data/sam3-review"))
    parser.add_argument("--limit-tiles", type=int, default=0)
    parser.add_argument("--min-area-m2", type=float, default=12.0)
    parser.add_argument("--simplify-px", type=float, default=0.75)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    west, south, east, north = (float(part) for part in args.bbox.split(","))
    zooms = [int(part) for part in args.zooms.split(",") if part.strip()]
    zoom = zooms[0]
    tiles = [(zoom, x, y) for x, y in iter_tiles((west, south, east, north), zoom)]
    if args.limit_tiles:
        tiles = tiles[: args.limit_tiles]
    if not tiles:
        print("no tiles in bbox")
        return 1

    models_dir = args.models_dir or Path(__file__).resolve().parent / "models" / "sam3"
    segmenter = Sam3TextSegmenter(models_dir)
    fetcher = ImageryFetcher(
        provider=args.provider,
        imagery_offset=args.imagery_offset,
        out_px=576,
        margin_px=32,
    )

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    prompts = [part.strip() for part in args.prompts.split(",") if part.strip()]
    geojson_features: list[dict] = []
    reports: list[dict] = []
    total_replace = total_extra = total_missed = 0
    iou_values: list[float] = []
    prompt_stats: dict[str, dict[str, float]] = {
        prompt: {"proposals": 0, "replace": 0, "extra": 0, "iou_sum": 0.0}
        for prompt in prompts
    }

    for z, x, y in tiles:
        started = time.time()
        window = fetcher.window(z, x, y)
        features = segmenter.encode_image(window.image)
        meters_per_px = max(window.meters_per_pixel, 1e-6)
        min_area_px = max(8.0, args.min_area_m2 / (meters_per_px**2))
        simplify_px = max(0.0, args.simplify_px * window.scale)
        width, height = window.image.size
        proposals: list[ReviewProposal] = []
        for prompt in prompts:
            proposals_raw = segmenter.proposals(
                features, prompt, window.image.size, threshold=args.threshold
            )
            for proposal in proposals_raw:
                parts = mask_to_polygons(
                    proposal.mask, min_area_px=min_area_px, simplify_px=simplify_px
                )
                if not parts:
                    continue
                polygon = clean_polygon(max(parts, key=lambda part: part.area))
                if polygon is None:
                    continue
                bx0, by0, bx1, by1 = proposal.box
                proposals.append(
                    ReviewProposal(
                        polygon=polygon,
                        score=proposal.score,
                        area_m2=polygon.area * meters_per_px**2,
                        box_px=(
                            max(0.0, bx0 * width),
                            max(0.0, by0 * height),
                            min(float(width), bx1 * width),
                            min(float(height), by1 * height),
                        ),
                        prompt=prompt,
                    )
                )

        existing_local = (
            load_existing_footprints(args.existing, z, x, y) if args.existing else []
        )
        existing_px = to_pixel_space(window, existing_local) if existing_local else []
        proposal_results, existing_results, _ = match_proposals(
            existing_px, proposals, min_iou=args.min_iou
        )

        for index, proposal in enumerate(proposals):
            result = proposal_results[index]
            iou_values.append(result.iou)
            ring = []
            for px, py in proposal.polygon.exterior.coords:
                lx, ly = window.pixel_to_local(px, py)
                ring.append(tile_local_to_lonlat(lx, ly, x, y, z))
            prompt_stats[proposal.prompt]["proposals"] += 1
            prompt_stats[proposal.prompt][result.status] = (
                prompt_stats[proposal.prompt].get(result.status, 0) + 1
            )
            prompt_stats[proposal.prompt]["iou_sum"] += result.iou
            geojson_features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {
                        "tile": f"{z}/{x}/{y}",
                        "prompt": proposal.prompt,
                        "score": round(proposal.score, 3),
                        "area_m2": round(proposal.area_m2, 1),
                        "match_iou": round(result.iou, 3),
                        "status": result.status,
                    },
                }
            )

        replace = sum(1 for result in proposal_results if result.status == "replace")
        extra = sum(1 for result in proposal_results if result.status == "extra")
        missed = sum(1 for result in existing_results if result.status == "missed")
        total_replace += replace
        total_extra += extra
        total_missed += missed

        write_preview(
            window,
            z,
            x,
            y,
            existing_px,
            proposals,
            existing_results,
            proposal_results,
            out_dir,
        )
        reports.append(
            {
                "tile": f"{z}/{x}/{y}",
                "proposals": len(proposals),
                "existing": len(existing_px),
                "replace": replace,
                "extra": extra,
                "missed": missed,
                "elapsed_s": round(time.time() - started, 1),
            }
        )
        print(
            f"{z}/{x}/{y}: sam3={len(proposals)} existing={len(existing_px)} "
            f"replace={replace} extra={extra} missed={missed} "
            f"({reports[-1]['elapsed_s']}s)"
        )

    (out_dir / "sam3_proposals.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": geojson_features})
    )
    mean_iou = sum(iou_values) / len(iou_values) if iou_values else 0.0
    by_prompt = {
        prompt: {
            "proposals": int(stats["proposals"]),
            "replace": int(stats.get("replace", 0)),
            "extra": int(stats.get("extra", 0)),
            "weak": int(stats.get("weak", 0)),
            "mean_best_iou": round(stats["iou_sum"] / stats["proposals"], 3)
            if stats["proposals"]
            else 0.0,
        }
        for prompt, stats in prompt_stats.items()
    }
    report = {
        "prompts": prompts,
        "threshold": args.threshold,
        "min_iou": args.min_iou,
        "imagery_offset": args.imagery_offset,
        "tiles": reports,
        "by_prompt": by_prompt,
        "totals": {
            "replace": total_replace,
            "extra": total_extra,
            "missed": total_missed,
            "proposals": sum(item["proposals"] for item in reports),
            "mean_best_iou": round(mean_iou, 3),
        },
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    print(
        f"\nreview complete: {out_dir}\n"
        f"  prompts={','.join(prompts)} proposals={report['totals']['proposals']} "
        f"replace={total_replace} extra={total_extra} missed={total_missed} "
        f"mean_iou={mean_iou:.3f}"
    )
    for prompt, stats in by_prompt.items():
        print(
            f"  {prompt}: proposals={stats['proposals']} replace={stats['replace']} "
            f"extra={stats['extra']} weak={stats['weak']} "
            f"mean_iou={stats['mean_best_iou']:.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
