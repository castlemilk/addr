"""Review loop for user-drawn roof-box proposals.

A roof box is drawn on the map (Address CRM -> "Propose box", press-drag-release
a rectangle) and saved as a CRM OBJECT feature labelled "roof-box proposal". It
is deliberately *not* a roof override: the committed extraction stays untouched
until a proposal is measured and explicitly applied. This module is the agent
side of that loop:

  python -m scripts.ml.proposal show --address "27 CAMBRIDGE ROAD"
      list proposals with box/record areas, IoU against the record ring,
      centroid shift, and a verdict (adopt / adjust / reject).

  python -m scripts.ml.proposal apply --address-id 29790 [--proposal-id N]
      promote the newest (or selected) proposal to a ROOF override via the CRM
      API (needs CRM_TOKEN with crm.roof.write), so `task addresses`
      regenerates the record from the proposed ring.

CRM state is read straight from SQLite (read-only, like address_review);
writes go through the Connect CRM API (same pattern as carve_canopies).
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

from .address_review import DEFAULT_DB, DEFAULT_RECORDS, build_context
from .addresses import METERS_PER_DEGREE

PROPOSAL_LABEL_PREFIX = "roof-box"

CRM_URL = os.environ.get(
    "CRM_URL", "http://localhost:8085/crm.v1.CrmService/UpsertFeature"
)

ADOPT_IOU = 0.7
ADJUST_IOU = 0.4


def _proposal_polygon(feature: dict) -> Polygon | None:
    try:
        geometry = json.loads(feature["geometry_json"])
    except (TypeError, ValueError):
        return None
    if geometry.get("type") != "Polygon" or not geometry.get("coordinates"):
        return None
    try:
        polygon = Polygon(geometry["coordinates"][0])
    except (ValueError, TypeError):
        return None
    if polygon.is_empty or not polygon.is_valid:
        return None
    return polygon


def list_proposals(features: list[dict]) -> list[tuple[dict, Polygon]]:
    """Roof-box proposal features paired with their polygons (newest last)."""
    found = []
    for feature in features:
        if feature.get("kind") != "object":
            continue
        if not (feature.get("label") or "").startswith(PROPOSAL_LABEL_PREFIX):
            continue
        polygon = _proposal_polygon(feature)
        if polygon is None:
            continue
        found.append((feature, polygon))
    return found


def record_polygon_lonlat(record: dict) -> Polygon:
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


def _m2(degrees2: float, lat: float) -> float:
    return degrees2 * (METERS_PER_DEGREE**2) * math.cos(math.radians(lat))


def _shift_meters(a: Polygon, b: Polygon) -> float:
    lat = (a.centroid.y + b.centroid.y) / 2
    cos_lat = math.cos(math.radians(lat))
    dx = (a.centroid.x - b.centroid.x) * METERS_PER_DEGREE * cos_lat
    dy = (a.centroid.y - b.centroid.y) * METERS_PER_DEGREE
    return math.hypot(dx, dy)


def compare(box: Polygon, record_ring: Polygon) -> dict:
    """Areas, overlap, and a verdict for a proposed box vs the record ring."""
    lat = record_ring.centroid.y
    box_area = _m2(box.area, lat)
    record_area = _m2(record_ring.area, lat)
    inter_area = _m2(box.intersection(record_ring).area, lat) if box.intersects(record_ring) else 0.0
    union = box_area + record_area - inter_area
    iou = inter_area / union if union > 0 else 0.0
    if iou >= ADOPT_IOU:
        verdict = "adopt"
    elif iou >= ADJUST_IOU:
        verdict = "adjust"
    else:
        verdict = "reject"
    return {
        "box_area_m2": round(box_area, 1),
        "record_area_m2": round(record_area, 1),
        "intersection_m2": round(inter_area, 1),
        "iou": round(iou, 3),
        "centroid_shift_m": round(_shift_meters(box, record_ring), 1),
        "box_covers_record_share": round(inter_area / record_area, 3) if record_area else 0.0,
        "verdict": verdict,
    }


def show(
    *,
    query: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
    records_path: Path = DEFAULT_RECORDS,
    db_path: Path = DEFAULT_DB,
) -> dict:
    context = build_context(
        query=query, lat=lat, lon=lon, address_id=address_id, records_path=records_path,
        db_path=db_path,
    )
    if context is None:
        return {"error": "no matching address record"}
    record = context.record
    proposals = list_proposals(context.features)
    ring = record_polygon_lonlat(record)
    return {
        "address_id": record["address_id"],
        "full_address": record["full_address"],
        "record_area_m2": round(record["area_m2"], 1),
        "record_kind": record["kind"],
        "boundary_source": record.get("boundary_source", "extracted"),
        "proposals": [
            {
                "id": feature["id"],
                "label": feature.get("label"),
                "updated_at": feature.get("updated_at"),
                "updated_by": feature.get("updated_by"),
                "box_lonlat": [
                    [round(lon, 7), round(lat, 7)] for lon, lat in polygon.exterior.coords
                ],
                **compare(polygon, ring),
            }
            for feature, polygon in proposals
        ],
    }


def apply_proposal(
    *,
    query: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
    proposal_id: int | None = None,
    token: str = "",
    records_path: Path = DEFAULT_RECORDS,
    db_path: Path = DEFAULT_DB,
) -> dict:
    context = build_context(
        query=query, lat=lat, lon=lon, address_id=address_id, records_path=records_path,
        db_path=db_path,
    )
    if context is None:
        return {"error": "no matching address record"}
    proposals = list_proposals(context.features)
    if not proposals:
        return {"error": "no roof-box proposals for this address"}
    if proposal_id is not None:
        selected = [item for item in proposals if int(item[0]["id"]) == proposal_id]
        if not selected:
            return {"error": f"proposal {proposal_id} not found"}
        feature, polygon = selected[0]
    else:
        feature, polygon = proposals[-1]
    record = context.record
    ring = record_polygon_lonlat(record)
    comparison = compare(polygon, ring)
    coordinates = [[round(lon, 7), round(lat, 7)] for lon, lat in polygon.exterior.coords]
    # Replace any existing roof override for this address: apply_crm_roof_overrides
    # applies every roof-kind row in turn, so a second row would race the first
    # nondeterministically. Replacing keeps exactly one committed ring.
    replaces = next(
        (feature["id"] for feature in context.features if feature.get("kind") == "roof"),
        0,
    )
    body = {
        "addressId": str(record["address_id"]),
        "kind": "FEATURE_KIND_ROOF",
        "geometryJson": json.dumps({"type": "Polygon", "coordinates": [coordinates]}),
        "label": f"roof-box proposal {feature['id']} (review loop)",
        "properties": {
            "eave_height": str(record.get("eave_height") or ""),
            "ridge_height": str(record.get("ridge_height") or ""),
            "roof_type": str(record.get("roof_type") or ""),
        },
    }
    if replaces:
        body["id"] = str(replaces)
    request = urllib.request.Request(
        CRM_URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310
        payload = json.loads(response.read())
    feature_id = str(payload.get("feature", {}).get("id"))
    return {
        "address_id": record["address_id"],
        "proposal_id": int(feature["id"]),
        "roof_feature_id": feature_id,
        "replaced_roof_feature_id": int(replaces) if replaces else None,
        "next": "run `task addresses` to regenerate the record, then re-render the review",
        **comparison,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "apply"):
        child = sub.add_parser(name)
        child.add_argument("--address", default=None)
        child.add_argument("--lat", type=float, default=None)
        child.add_argument("--lon", type=float, default=None)
        child.add_argument("--address-id", type=int, default=None)
        child.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
        child.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub.choices["apply"].add_argument("--proposal-id", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "show":
        print(
            json.dumps(
                show(
                    query=args.address,
                    lat=args.lat,
                    lon=args.lon,
                    address_id=args.address_id,
                    records_path=args.records,
                    db_path=args.db,
                ),
                indent=1,
            )
        )
        return 0
    token = os.environ.get("CRM_TOKEN", "")
    if not token:
        print("no CRM_TOKEN: trying unauthenticated (works on local -crm-noauth servers)", file=sys.stderr)
    print(
        json.dumps(
            apply_proposal(
                query=args.address,
                lat=args.lat,
                lon=args.lon,
                address_id=args.address_id,
                proposal_id=args.proposal_id,
                token=token,
                records_path=args.records,
                db_path=args.db,
            ),
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
