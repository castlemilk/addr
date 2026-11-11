"""Attribution audit: does each record's ring belong to its own address?

The 27-Cambridge failure mode: a record's ring sits on a neighbouring
building while its own G-NAF pin is far away. The detector compares, per
record, the distance from the OWN pin to the ring edge against the NEAREST
OTHER pin's distance. Measurement over the demo bbox (665 records): median
disagreement 0.0 m, p99 8.9 m, max 13.3 m (27 Cambridge itself). A 10 m
threshold with a different-street-number requirement isolates genuine
cross-address misattributions from normal yard placement and unit-complex
coarseness.

Usage:
    python -m scripts.ml.attribution --records public/address-roofs.json \\
        --db data/australia_geo.db --out data/reviews/attribution.json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

from .address_review import DEFAULT_DB, DEFAULT_RECORDS
from .addresses import METERS_PER_DEGREE

#: Own-pin minus nearest-other-pin ring distance at/above this is a suspect.
ATTRIBUTION_DIFF_M = 10.0

#: Only other pins within this radius of the ring are considered.
SEARCH_RADIUS_M = 60.0


def _record_polygon(record: dict) -> Polygon | None:
    centroid = record.get("centroid") or {}
    ring = record.get("ring_meters") or []
    if not centroid or len(ring) < 3:
        return None
    cos_lat = math.cos(math.radians(centroid["lat"]))
    polygon = Polygon(
        [
            (
                centroid["lon"] + x / (METERS_PER_DEGREE * cos_lat),
                centroid["lat"] + y / METERS_PER_DEGREE,
            )
            for x, y in ring
        ]
    )
    if polygon.is_empty or not polygon.is_valid:
        return None
    return polygon


def load_pins(db_path: Path) -> list[dict]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, unit, number, street, full_address, lat, lon FROM addresses"
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "address_id": row[0],
            "unit": row[1],
            "number": row[2],
            "street": row[3],
            "full_address": row[4],
            "lat": row[5],
            "lon": row[6],
        }
        for row in rows
    ]


def _edge_distance_m(polygon: Polygon, lat: float, lon: float) -> float:
    return Point(lon, lat).distance(polygon) * METERS_PER_DEGREE * math.cos(
        math.radians(lat)
    )


def audit(
    records: list[dict],
    pins: list[dict],
    *,
    diff_threshold_m: float = ATTRIBUTION_DIFF_M,
) -> dict:
    """Ranks records whose ring fits another address's pin better than its own."""
    points = [Point(pin["lon"], pin["lat"]) for pin in pins]
    pin_by_id = {pin["address_id"]: pin for pin in pins}
    tree = STRtree(points)
    suspects = []
    checked = 0
    for record in records:
        polygon = _record_polygon(record)
        if polygon is None:
            continue
        checked += 1
        own_point = record.get("address_point") or {}
        if own_point.get("lat") is None:
            continue
        own_dist = _edge_distance_m(polygon, own_point["lat"], own_point["lon"])
        best_other = None
        best_dist = math.inf
        for index in tree.query(
            Point(own_point["lon"], own_point["lat"]).buffer(
                SEARCH_RADIUS_M / METERS_PER_DEGREE
            )
        ):
            pin = pins[int(index)]
            if pin["address_id"] == record["address_id"]:
                continue
            distance = _edge_distance_m(polygon, pin["lat"], pin["lon"])
            if distance < best_dist:
                best_dist, best_other = distance, pin
        if best_other is None:
            continue
        disagreement = own_dist - best_dist
        if disagreement < diff_threshold_m:
            continue
        own_pin = pin_by_id.get(record["address_id"], {})
        same_development = (
            (own_pin.get("number"), own_pin.get("street"))
            == (best_other.get("number"), best_other.get("street"))
            and own_pin.get("number") is not None
        )
        suspects.append(
            {
                "address_id": record["address_id"],
                "full_address": record["full_address"],
                "kind": record.get("kind"),
                "area_m2": record.get("area_m2"),
                "boundary_source": record.get("boundary_source", "extracted"),
                "own_pin_dist_m": round(own_dist, 1),
                "other_pin_dist_m": round(best_dist, 2),
                "disagreement_m": round(disagreement, 1),
                "other_address_id": best_other["address_id"],
                "other_full_address": best_other["full_address"],
                "tier": (
                    "same-development"
                    if same_development
                    else "cross-address"
                ),
                "shared_with": record.get("shared_with", []),
            }
        )
    suspects.sort(
        key=lambda item: (item["tier"] != "cross-address", -item["disagreement_m"])
    )
    return {
        "records_checked": checked,
        "diff_threshold_m": diff_threshold_m,
        "suspects": suspects,
        "cross_address": sum(1 for item in suspects if item["tier"] == "cross-address"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=Path("data/reviews/attribution.json"))
    parser.add_argument("--threshold", type=float, default=ATTRIBUTION_DIFF_M)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = json.loads(args.records.read_text())
    pins = load_pins(args.db)
    result = audit(records, pins, diff_threshold_m=args.threshold)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1))
    print(
        f"checked={result['records_checked']} suspects={len(result['suspects'])} "
        f"(cross-address={result['cross_address']}) -> {args.out}"
    )
    for item in result["suspects"][:15]:
        print(
            f"  {item['tier']:<14} {item['address_id']:<6} diff={item['disagreement_m']:<5} "
            f"own={item['own_pin_dist_m']}m other={item['other_pin_dist_m']}m "
            f"{item['full_address'][:38]}  <- {item['other_full_address'][:38]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
