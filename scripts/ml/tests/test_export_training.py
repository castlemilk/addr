import json
import sqlite3

from scripts.ml.export_training import export_corrections


def make_db(path, features):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE crm_features (id INTEGER PRIMARY KEY, address_id INTEGER, "
        "kind TEXT, geometry_json TEXT, label TEXT, properties TEXT, "
        "updated_at TEXT, updated_by TEXT)"
    )
    for feature in features:
        connection.execute(
            "INSERT INTO crm_features (address_id, kind, geometry_json, label, "
            "properties, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            feature,
        )
    connection.commit()
    connection.close()


def box(lon, lat, size_deg=0.0002):
    ring = [
        [lon, lat],
        [lon + size_deg, lat],
        [lon + size_deg, lat + size_deg],
        [lon, lat + size_deg],
        [lon, lat],
    ]
    return json.dumps({"type": "Polygon", "coordinates": [ring]})


def test_pin_shift_tripwire(tmp_path):
    db = tmp_path / "crm.db"
    make_db(
        db,
        [
            # Near its pin: fine.
            (27, "roof", box(145.31252, -37.79044), "ok", "{}", "t", "ed"),
            # ~25 m east of its pin: the 27-Cambridge failure shape.
            (29, "roof", box(145.31280, -37.79038), "wrong", "{}", "t", "ed"),
        ],
    )
    records = tmp_path / "records.json"
    records.write_text(
        json.dumps(
            [
                {
                    "address_id": 27,
                    "full_address": "27 CAMBRIDGE ROAD",
                    "address_point": {"lat": -37.79044, "lon": 145.31252},
                    "centroid": {"lat": -37.79044, "lon": 145.31252},
                },
                {
                    "address_id": 29,
                    "full_address": "29 CAMBRIDGE ROAD",
                    "address_point": {"lat": -37.79044, "lon": 145.31252},
                    "centroid": {"lat": -37.79044, "lon": 145.31252},
                },
            ]
        )
    )
    out = tmp_path / "corrections.jsonl"
    manifest = export_corrections(db, records, out)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    by_address = {row["address_id"]: row for row in rows}
    assert by_address[27]["needs_review"] is False
    assert by_address[27]["pin_to_corrected_centroid_m"] < 15
    assert by_address[29]["needs_review"] is True
    assert by_address[29]["pin_to_corrected_centroid_m"] > 15
    assert manifest["needs_review"] == 1


def test_original_boundary_uses_suppressed_centroid(tmp_path):
    db = tmp_path / "crm.db"
    make_db(db, [(27, "roof", box(145.313, -37.79), "roof", "{}", "t", "ed")])
    records = tmp_path / "records.json"
    records.write_text(json.dumps([{
        "address_id": 27,
        "centroid": {"lon": 145.313, "lat": -37.79},
        "suppressed_centroid": {"lon": 145.312, "lat": -37.791},
        "suppressed_ring_meters": [[0, 0], [10, 0], [10, 10], [0, 10]],
    }]))
    out = tmp_path / "out.jsonl"
    export_corrections(db, records, out)
    row = json.loads(out.read_text())
    assert row["extracted_geometry"]["coordinates"][0][0] == [145.312, -37.791]
    assert row["boundary_metrics"]["centroid_shift_m"] > 100
    assert row["training_eligible"] is False


def test_boundary_metrics_and_spatial_split():
    from scripts.ml.export_training import boundary_metrics, spatial_split
    ring = json.loads(box(145.3, -37.8))["coordinates"][0]
    same = boundary_metrics(ring, ring)
    assert same["iou"] == 1 and same["boundary_f1"] == 1
    assert same["hausdorff_m"] == 0
    nearby = [[x + .00001, y] for x, y in ring]
    assert spatial_split(ring) == spatial_split(nearby)


def test_reject_invalid_or_non_polygon_training_labels():
    from scripts.ml.export_training import _geometry_to_ring
    for geometry in [
        {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]]]},
        {"type": "Polygon", "coordinates": [[[float("nan"), 0], [1, 0], [1, 1], [0, 0]]]},
    ]:
        assert _geometry_to_ring(json.dumps(geometry)) is None


def test_training_eligibility_requires_current_approved_geometry(tmp_path):
    import math

    from scripts.ml.addresses import METERS_PER_DEGREE
    from scripts.ml.export_training import boundary_signature
    db = tmp_path / "crm.db"
    geo = box(145.3, -37.8)
    make_db(db, [(27, "roof", geo, "roof", "{}", "t", "ed")])
    center = {"lon": 145.3001, "lat": -37.7999}
    ring = json.loads(geo)["coordinates"][0][:-1]
    record = {"address_id": 27, "boundary_source": "crm", "centroid": center,
              "ring_meters": [[(x - center["lon"]) * METERS_PER_DEGREE * math.cos(math.radians(center["lat"])),
                               (y - center["lat"]) * METERS_PER_DEGREE] for x, y in ring]}
    records = tmp_path / "records.json"
    records.write_text(json.dumps([record]))
    feedback = tmp_path / "feedback.json"
    review = {"status": "approved", "boundary_verified": True, "boundary_signature": boundary_signature(record), "updated_at": "2026-09-22"}
    feedback.write_text(json.dumps({"suburban_benchmark:27": review}))
    out = tmp_path / "out.jsonl"
    manifest = export_corrections(db, records, out, feedback_path=feedback)
    assert manifest["training_eligible"] == 1
    review["boundary_signature"] = "old-boundary"
    feedback.write_text(json.dumps({"suburban_benchmark:27": review}))
    assert export_corrections(db, records, out, feedback_path=feedback)["training_eligible"] == 0
    # The CRM feature can change before address-roofs.json is regenerated.
    review["boundary_signature"] = boundary_signature(record)
    feedback.write_text(json.dumps({"suburban_benchmark:27": review}))
    connection = sqlite3.connect(db)
    connection.execute("UPDATE crm_features SET geometry_json = ?", (box(145.31, -37.8),))
    connection.commit()
    connection.close()
    assert export_corrections(db, records, out, feedback_path=feedback)["training_eligible"] == 0
