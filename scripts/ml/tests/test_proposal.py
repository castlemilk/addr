import json

from scripts.ml.proposal import (
    compare,
    list_proposals,
    record_polygon_lonlat,
)
from shapely.geometry import Polygon


def make_feature(id, kind="object", label="roof-box proposal", ring=None):
    ring = ring or [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    return {
        "id": id,
        "kind": kind,
        "label": label,
        "geometry_json": json.dumps({"type": "Polygon", "coordinates": [ring]}),
    }


def test_list_proposals_keeps_only_roof_boxes():
    features = [
        make_feature(1),
        make_feature(2, kind="roof", label="verified gable"),
        make_feature(3, label="shed"),
        make_feature(4, label="roof-box proposal (second look)"),
        make_feature(5, label="roof-box proposal", ring=[(0, 0), (1, 1)]),
    ]
    found = list_proposals(features)
    assert [feature["id"] for feature, _ in found] == [1, 4]


def test_compare_identical_rings_adopts():
    ring = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    result = compare(ring, ring)
    assert result["iou"] == 1.0
    assert result["centroid_shift_m"] == 0.0
    assert result["verdict"] == "adopt"


def test_compare_disjoint_rings_rejects():
    box = Polygon([(100, 100), (110, 100), (110, 110), (100, 110)])
    record = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    result = compare(box, record)
    assert result["iou"] == 0.0
    assert result["verdict"] == "reject"


def test_compare_half_overlap_adjusts():
    box = Polygon([(2.5, 0), (12.5, 0), (12.5, 10), (2.5, 10)])
    record = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    result = compare(box, record)
    assert result["verdict"] == "adjust"
    assert result["box_covers_record_share"] == 0.75


def test_record_polygon_round_trips_ring_meters():
    record = {
        "centroid": {"lat": -37.79, "lon": 145.31},
        "ring_meters": [[-5, -5], [5, -5], [5, 5], [-5, 5]],
    }
    polygon = record_polygon_lonlat(record)
    assert polygon.is_valid
    assert abs(polygon.centroid.x - 145.31) < 1e-9
    assert abs(polygon.centroid.y + 37.79) < 1e-9
