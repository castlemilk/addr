"""Carves canopies out of a record's roof ring and stores the result in the CRM.

A review-loop helper: when a record's ring includes canopy area (flagged by the
isolation metric), subtracting the overlapping tree polygons yields the roof.
Dry-run by default; --apply upserts the carved ring via the CRM API (requires
CRM_TOKEN with roof write scope).

Usage:
    CRM_TOKEN=addr__... python -m scripts.ml.carve_canopies \
        --records public/address-roofs.json --address-id 25892 --apply
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from shapely.geometry import Polygon

from .addresses import METERS_PER_DEGREE, load_trees

MIN_TREE_AREA_M2 = 20.0
MIN_TREE_INTERSECTION_M2 = 4.0
MIN_KEEP_RATIO = 0.55
CRM_URL = os.environ.get("CRM_URL", "http://localhost:8085/crm.v1.CrmService/UpsertFeature")


def _ring_lonlat(record: dict) -> Polygon:
    centroid = record["centroid"]
    cos_lat = math.cos(math.radians(centroid["lat"]))
    return Polygon(
        [
            (
                centroid["lon"] + x / (METERS_PER_DEGREE * cos_lat),
                centroid["lat"] + y / METERS_PER_DEGREE,
            )
            for x, y in record["ring_meters"]
        ]
    )


def carve(
    record: dict, trees: list, *, min_tree_area_m2: float, min_keep_ratio: float
) -> Polygon | None:
    ring = _ring_lonlat(record)
    if ring.is_empty or not ring.is_valid:
        return None
    geometry = ring
    carved_trees = 0
    for tree_polygon, properties, _ in trees:
        if float(properties.get("area_m2") or 0.0) < min_tree_area_m2:
            continue
        if not geometry.intersects(tree_polygon):
            continue
        intersection = geometry.intersection(tree_polygon).area
        # Intersection area in m^2 (degrees^2 scaled by latitude).
        cos_lat = math.cos(math.radians(ring.centroid.y))
        if intersection * (METERS_PER_DEGREE**2) * cos_lat < MIN_TREE_INTERSECTION_M2:
            continue
        geometry = geometry.difference(tree_polygon)
        carved_trees += 1
    if carved_trees == 0 or geometry.is_empty:
        return None
    if geometry.geom_type == "MultiPolygon":
        geometry = max(geometry.geoms, key=lambda part: part.area)
    if geometry.area < ring.area * min_keep_ratio:
        return None
    return geometry


def upsert(address_id: int, polygon: Polygon, record: dict, token: str, label: str) -> str:
    coordinates = [
        [round(lon, 7), round(lat, 7)] for lon, lat in polygon.exterior.coords
    ]
    body = {
        "addressId": str(address_id),
        "kind": "FEATURE_KIND_ROOF",
        "geometryJson": json.dumps({"type": "Polygon", "coordinates": [coordinates]}),
        "label": label,
        "properties": {
            "eave_height": str(record.get("eave_height") or ""),
            "ridge_height": str(record.get("ridge_height") or ""),
            "roof_type": str(record.get("roof_type") or ""),
        },
    }
    request = urllib.request.Request(
        CRM_URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310
        payload = json.loads(response.read())
    return str(payload.get("feature", {}).get("id"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=Path("public/address-roofs.json"))
    parser.add_argument("--tiles", type=Path, default=Path("data/australia_tiles.mbtiles"))
    parser.add_argument("--address-id", type=int, action="append", required=True)
    parser.add_argument("--min-tree-area", type=float, default=MIN_TREE_AREA_M2)
    parser.add_argument("--min-keep-ratio", type=float, default=MIN_KEEP_RATIO)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = {row["address_id"]: row for row in json.loads(args.records.read_text())}
    trees = load_trees(args.tiles)
    token = os.environ.get("CRM_TOKEN", "")
    if args.apply and not token:
        print("CRM_TOKEN is required with --apply", file=sys.stderr)
        return 2

    for address_id in args.address_id:
        record = records.get(address_id)
        if record is None:
            print(f"{address_id}: no record")
            continue
        polygon = carve(
            record,
            trees,
            min_tree_area_m2=args.min_tree_area,
            min_keep_ratio=args.min_keep_ratio,
        )
        if polygon is None:
            print(f"{address_id}: no carve needed/possible")
            continue
        cos_lat = math.cos(math.radians(polygon.centroid.y))
        area_m2 = round(polygon.area * METERS_PER_DEGREE**2 * cos_lat, 1)
        print(
            f"{address_id}: carve {record['area_m2']}m2 -> {area_m2}m2 "
            f"(canopy share before {record['canopy_over_roof_share']})"
        )
        if args.apply:
            feature_id = upsert(address_id, polygon, record, token, "canopy carve (review loop)")
            print(f"{address_id}: crm feature {feature_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
