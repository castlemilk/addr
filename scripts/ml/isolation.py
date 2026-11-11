"""Isolation quality report: how cleanly roofs, canopies, and obstacles separate.

Computes building/building, tree/tree, and building/tree conflicts in an MBTiles
archive plus per-address canopy-over-roof shares, and renders a diagnostic map.

Usage:
    python -m scripts.ml.isolation --tiles data/australia_tiles.mbtiles \
        --records public/address-roofs.json --out data/reviews/isolation.json \
        --map data/reviews/isolation.png
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw
from shapely.strtree import STRtree

from .addresses import METERS_PER_DEGREE, load_buildings, load_trees

CONFLICT_IOU = 0.3
TREE_INSIDE_RATIO = 0.6
TREE_MIN_AREA_M2 = 4.0


def _tree_area_m2(polygon, properties: dict, meters_per_unit: float) -> float:
    stored = properties.get("area_m2")
    if stored:
        return float(stored) * (meters_per_unit / 0.118) ** 0 if False else float(stored)
    return polygon.area * (METERS_PER_DEGREE**2)


def analyze(
    tiles_path: Path, records_path: Path | None
) -> dict:
    buildings = load_buildings(tiles_path)
    trees = load_trees(tiles_path)
    building_polys = [item[0] for item in buildings]
    tree_items = [
        item for item in trees if _tree_area_m2(item[0], item[1], item[2]) >= TREE_MIN_AREA_M2
    ]
    tree_polys = [item[0] for item in tree_items]
    building_tree = STRtree(building_polys)
    tree_tree = STRtree(tree_polys) if tree_polys else None

    tree_overlap_pairs = 0
    tree_conflicts = []
    for index, (polygon, properties, meters_per_unit) in enumerate(tree_items):
        area_m2 = _tree_area_m2(polygon, properties, meters_per_unit)
        best_ratio = 0.0
        best_iou = 0.0
        for candidate_index in building_tree.query(polygon):
            candidate = building_polys[int(candidate_index)]
            if not polygon.intersects(candidate):
                continue
            intersection = polygon.intersection(candidate).area
            if intersection <= 0:
                continue
            ratio = intersection / polygon.area
            union = polygon.union(candidate).area
            iou = intersection / union if union > 0 else 0.0
            if ratio > best_ratio:
                best_ratio = ratio
            if iou > best_iou:
                best_iou = iou
        if best_ratio > 0:
            tree_conflicts.append(
                {
                    "tree_index": index,
                    "area_m2": round(area_m2, 1),
                    "inside_ratio": round(best_ratio, 2),
                    "iou": round(best_iou, 2),
                }
            )
        if tree_tree is not None:
            for other_index in tree_tree.query(polygon):
                other_index = int(other_index)
                if other_index <= index:
                    continue
                other = tree_polys[other_index]
                if not polygon.intersects(other):
                    continue
                union = polygon.union(other).area
                if union <= 0:
                    continue
                intersection = polygon.intersection(other).area
                if intersection / union > CONFLICT_IOU:
                    tree_overlap_pairs += 1

    mostly_inside = [
        item for item in tree_conflicts if item["inside_ratio"] >= TREE_INSIDE_RATIO
    ]
    record_conflicts = []
    if records_path is not None and records_path.exists():
        from shapely.geometry import Polygon

        tree_tree_bbox = [(poly.bounds, poly) for poly in tree_polys]
        records = json.loads(records_path.read_text())
        for record in records:
            ring = record.get("ring_meters") or []
            if len(ring) < 3:
                continue
            clat, clon = record["centroid"]["lat"], record["centroid"]["lon"]
            cos_lat = math.cos(math.radians(clat))
            polygon = Polygon(
                [
                    (
                        clon + x / (METERS_PER_DEGREE * cos_lat),
                        clat + y / METERS_PER_DEGREE,
                    )
                    for x, y in ring
                ]
            )
            if polygon.area <= 0:
                continue
            bounds = polygon.bounds
            canopy_m2 = 0.0
            for (minx, miny, maxx, maxy), tree_poly in tree_tree_bbox:
                if maxx < bounds[0] or minx > bounds[2]:
                    continue
                if maxy < bounds[1] or miny > bounds[3]:
                    continue
                if not polygon.intersects(tree_poly):
                    continue
                canopy_m2 += (
                    polygon.intersection(tree_poly).area
                    * (METERS_PER_DEGREE**2)
                    * cos_lat
                )
            ring_area_m2 = polygon.area * (METERS_PER_DEGREE**2) * cos_lat
            share = canopy_m2 / ring_area_m2 if ring_area_m2 > 0 else 0.0
            if share > 0.05:
                record_conflicts.append(
                    {
                        "address_id": record["address_id"],
                        "full_address": record["full_address"],
                        "area_m2": record["area_m2"],
                        "canopy_share": round(share, 3),
                    }
                )

    record_conflicts.sort(key=lambda item: item["canopy_share"], reverse=True)
    return {
        "buildings": len(building_polys),
        "trees_total": len(trees),
        "trees_analyzed": len(tree_items),
        "trees_touching_buildings": len(tree_conflicts),
        "trees_mostly_inside_buildings": len(mostly_inside),
        "tree_tree_overlap_pairs": tree_overlap_pairs,
        "tree_conflicts": tree_conflicts,
        "records_with_canopy_over_roof": len(record_conflicts),
        "record_conflicts": record_conflicts[:200],
    }


def render_map(
    tiles_path: Path, out_path: Path, report: dict, bbox: tuple[float, float, float, float]
) -> None:
    buildings = load_buildings(tiles_path)
    trees = load_trees(tiles_path)
    width, height = 1400, 1000
    west, south, east, north = bbox
    image = Image.new("RGB", (width, height), (15, 23, 42))
    draw = ImageDraw.Draw(image)

    def to_px(lon: float, lat: float) -> tuple[float, float]:
        px = (lon - west) / (east - west) * width
        py = (north - lat) / (north - south) * height
        return px, py

    def draw_polygon(polygon, outline, fill=None) -> None:
        points = [to_px(lon, lat) for lon, lat in polygon.exterior.coords]
        if fill is not None:
            draw.polygon(points, fill=fill)
        draw.line(points, fill=outline, width=1)

    for polygon, _, _ in buildings:
        if not (west <= polygon.centroid.x <= east and south <= polygon.centroid.y <= north):
            continue
        draw_polygon(polygon, (148, 163, 184))

    tree_polys = []
    for polygon, properties, meters_per_unit in trees:
        if _tree_area_m2(polygon, properties, meters_per_unit) < TREE_MIN_AREA_M2:
            continue
        tree_polys.append(polygon)
        draw_polygon(polygon, (45, 212, 191), fill=(19, 78, 74))

    conflict_ids = {item["tree_index"] for item in report["tree_conflicts"]}
    for index, polygon in enumerate(tree_polys):
        if index not in conflict_ids:
            continue
        points = [to_px(lon, lat) for lon, lat in polygon.exterior.coords]
        draw.line(points, fill=(248, 113, 113), width=3)

    image.save(out_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tiles", type=Path, required=True)
    parser.add_argument("--records", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--map", type=Path, default=None)
    parser.add_argument(
        "--bbox",
        type=str,
        default="145.308,-37.797,145.320,-37.786",
        help="west,south,east,north for the diagnostic map",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = analyze(args.tiles, args.records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    if args.map:
        parts = [float(part) for part in args.bbox.split(",")]
        render_map(args.tiles, args.map, report, tuple(parts))
    print(
        f"buildings={report['buildings']} trees={report['trees_analyzed']} "
        f"trees_touching={report['trees_touching_buildings']} "
        f"mostly_inside={report['trees_mostly_inside_buildings']} "
        f"tree_overlaps={report['tree_tree_overlap_pairs']} "
        f"records_with_canopy={report['records_with_canopy_over_roof']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
