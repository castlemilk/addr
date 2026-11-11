"""Exports CRM corrections as a training reference dataset.

Each row pairs the corrected boundary with the extracted footprint it replaced
(when available), plus address context and authorship, so the corrections can be
used for fine-tuning or evaluation. Writes JSONL plus a manifest summary.

Usage:
    python -m scripts.ml.export_training \
        --db data/australia_geo.db \
        --records public/address-roofs.json \
        --out data/training/corrections.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from shapely.geometry import Polygon

from .addresses import METERS_PER_DEGREE
from .review_feedback import boundary_signature, latest_feedback


def _geometry_to_ring(geometry_json: str) -> list[list[float]] | None:
    try:
        geometry = json.loads(geometry_json)
        if geometry.get("type") != "Polygon" or len(geometry["coordinates"]) != 1:
            return None
        coordinates = geometry["coordinates"][0]
        ring = [[float(lon), float(lat)] for lon, lat in coordinates]
        if any(not math.isfinite(lon) or not math.isfinite(lat) or abs(lon) > 180 or abs(lat) > 90 for lon, lat in ring):
            return None
        polygon = Polygon(ring)
        if not polygon.is_valid or polygon.area <= 0:
            return None
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    return ring if len(ring) >= 3 else None


def boundary_metrics(predicted: list, reference: list, tolerance_m: float = 0.5) -> dict:
    """Boundary metrics in a shared local metre frame; never compare lat/lon distances."""
    origin = Polygon(reference).centroid
    scale_x = METERS_PER_DEGREE * math.cos(math.radians(origin.y))
    def local(ring):
        return Polygon([((x - origin.x) * scale_x, (y - origin.y) * METERS_PER_DEGREE) for x, y in ring])
    source, target = local(predicted), local(reference)
    if not source.is_valid or not target.is_valid or source.area <= 0 or target.area <= 0:
        return {}
    precision = source.boundary.intersection(target.boundary.buffer(tolerance_m)).length / source.length
    recall = target.boundary.intersection(source.boundary.buffer(tolerance_m)).length / target.length
    return {
        "iou": round(source.intersection(target).area / source.union(target).area, 4),
        "boundary_precision": round(precision, 4),
        "boundary_recall": round(recall, 4),
        "boundary_f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
        "boundary_tolerance_m": tolerance_m,
        "hausdorff_m": round(source.boundary.hausdorff_distance(target.boundary), 3),
        "centroid_shift_m": round(source.centroid.distance(target.centroid), 3),
        "area_error_m2": round(source.area - target.area, 3),
    }


def spatial_split(ring: list) -> tuple[str, str]:
    # Shared spatial cells keep nearby roofs and inherited units in one split.
    # Group by geographic cell, never by address ID (which leaks shared roofs).
    center = Polygon(ring).centroid
    group = f"{math.floor(center.x / .003)}:{math.floor(center.y / .003)}"
    bucket = int(hashlib.sha256(group.encode()).hexdigest()[:8], 16) % 10
    return group, "test" if bucket == 0 else "validation" if bucket == 1 else "train"


def export_corrections(
    db_path: Path, records_path: Path, out_path: Path, *, feedback_path: Path | None = None
) -> dict:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, address_id, kind, geometry_json, label, properties, "
            "updated_at, updated_by FROM crm_features ORDER BY address_id, kind"
        ).fetchall()
    finally:
        connection.close()

    records = {}
    if records_path.exists():
        for record in json.loads(records_path.read_text()):
            records[record["address_id"]] = record

    feedback = latest_feedback(feedback_path) if feedback_path else {}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    exported = 0
    with open(out_path, "w") as handle:
        for (
            feature_id,
            address_id,
            kind,
            geometry_json,
            label,
            properties_json,
            updated_at,
            updated_by,
        ) in rows:
            corrected = _geometry_to_ring(geometry_json)
            if corrected is None:
                continue
            record = records.get(address_id, {})
            extracted = None
            suppressed_ring = record.get("suppressed_ring_meters")
            centroid = record.get("centroid") or {}
            original_centroid = record.get("suppressed_centroid") or centroid
            if suppressed_ring and original_centroid:
                cos_lat = math.cos(math.radians(original_centroid["lat"]))
                extracted = [
                    [
                        original_centroid["lon"]
                        + point[0] / (METERS_PER_DEGREE * cos_lat),
                        original_centroid["lat"] + point[1] / METERS_PER_DEGREE,
                    ]
                    for point in suppressed_ring
                ]
            try:
                properties = json.loads(properties_json or "{}")
            except ValueError:
                properties = {}
            review = feedback.get(address_id, {})
            signature = boundary_signature(record)
            metrics = boundary_metrics(extracted, corrected) if extracted else None
            group, split = spatial_split(corrected)
            current_ring = [[centroid["lon"] + x / (METERS_PER_DEGREE * math.cos(math.radians(centroid["lat"]))),
                             centroid["lat"] + y / METERS_PER_DEGREE] for x, y in record.get("ring_meters", [])] if centroid else []
            # Approval is bound to the actual corrected boundary, not just an ID.
            current_metrics = boundary_metrics(current_ring, corrected) if current_ring else {}
            geometry_current = current_metrics.get("hausdorff_m", math.inf) <= 0.05
            approved = (review.get("status") == "approved" and review.get("boundary_verified") is True
                        and not review.get("issues") and not review.get("rejected_cuts")
                        and bool(signature) and review.get("boundary_signature") == signature and geometry_current)
            # Placement tripwire: a correction whose new ring sits far from the
            # G-NAF pin may have picked a neighbouring building (e.g. 27
            # Cambridge corrected onto Unit 4/29's roof, ~25 m from its pin).
            # Reviewers check rows with pin_to_corrected_centroid_m > 15.
            pin_shift_m = None
            pin = record.get("address_point") or {}
            if kind == "roof" and pin.get("lat") is not None and corrected:
                lon = sum(point[0] for point in corrected) / len(corrected)
                lat = sum(point[1] for point in corrected) / len(corrected)
                cos_lat = math.cos(math.radians(lat))
                pin_shift_m = round(
                    math.hypot(
                        (lon - pin["lon"]) * METERS_PER_DEGREE * cos_lat,
                        (lat - pin["lat"]) * METERS_PER_DEGREE,
                    ),
                    1,
                )
            row = {
                "schema_version": 2,
                "feature_id": feature_id,
                "reference_source": "crm-correction",
                "reference_status": "human-reviewed" if approved else "unverified",
                "training_eligible": kind == "roof" and approved,
                "height_source": "estimate-not-a-height-label",
                "review": review,
                "spatial_group": group,
                "split": split,
                "boundary_metrics": metrics,
                "address_id": address_id,
                "kind": str(kind),
                "label": label,
                "updated_at": updated_at,
                "updated_by": updated_by,
                "properties": properties,
                "corrected_geometry": {"type": "Polygon", "coordinates": [corrected + [corrected[0]]]},
                "extracted_geometry": (
                    {"type": "Polygon", "coordinates": [extracted + [extracted[0]]]}
                    if extracted
                    else None
                ),
                "context": {
                    "full_address": record.get("full_address"),
                    "address_point": record.get("address_point"),
                    "centroid": centroid,
                    "kind": record.get("kind"),
                    "area_m2": record.get("area_m2"),
                    "isolation": record.get("isolation"),
                    "quality_score": record.get("quality_score"),
                    "quality_flags": record.get("quality_flags", []),
                },
                "pin_to_corrected_centroid_m": pin_shift_m,
                "needs_review": bool(pin_shift_m is not None and pin_shift_m > 15),
            }
            handle.write(json.dumps(row) + "\n")
            exported += 1

    manifest = {
        "corrections": exported,
        "out": str(out_path),
        "kinds": {},
        "users": {},
        "needs_review": 0,
        "training_eligible": 0,
        "splits": {},
        "paired_roof_boundaries": 0,
    }
    for line in out_path.read_text().splitlines():
        item = json.loads(line)
        manifest["kinds"][item["kind"]] = manifest["kinds"].get(item["kind"], 0) + 1
        manifest["users"][item["updated_by"]] = (
            manifest["users"].get(item["updated_by"], 0) + 1
        )
        if item.get("training_eligible"):
            manifest["training_eligible"] += 1
            manifest["splits"][item["split"]] = manifest["splits"].get(item["split"], 0) + 1
        if item["kind"] == "roof" and item.get("boundary_metrics"):
            manifest["paired_roof_boundaries"] += 1
        if item.get("needs_review"):
            manifest["needs_review"] += 1
    (out_path.parent / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/australia_geo.db"))
    parser.add_argument("--records", type=Path, default=Path("public/address-roofs.json"))
    parser.add_argument("--feedback", type=Path, default=Path("data/reviews/feedback.json"))
    parser.add_argument("--out", type=Path, default=Path("data/training/corrections.jsonl"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = export_corrections(args.db, args.records, args.out, feedback_path=args.feedback)
    print(
        f"corrections={manifest['corrections']} kinds={manifest['kinds']} "
        f"users={manifest['users']} out={manifest['out']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
