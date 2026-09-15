import pytest
from scripts.ml.footprints import LocalFootprint, merge_footprints
from scripts.ml.osm import build_query, extract_height, parse_overpass
from shapely.geometry import Polygon


def _square(size=10.0):
    return Polygon([(0, 0), (size, 0), (size, size), (0, size)])


def test_extract_height_from_tag():
    height, min_height, levels = extract_height({"height": "12.5 m"})
    assert height == pytest.approx(12.5)
    assert min_height is None
    assert levels is None


def test_extract_height_from_levels():
    height, min_height, levels = extract_height(
        {"building:levels": "3", "building:min_level": "1"}
    )
    assert height == pytest.approx(3 * 3.1)
    assert min_height == pytest.approx(3.1)
    assert levels == 3


def test_extract_height_missing():
    assert extract_height({}) == (None, None, None)


def test_parse_overpass_way():
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 1,
                "tags": {"building": "house", "height": "8"},
                "geometry": [
                    {"lat": -37.79, "lon": 145.31},
                    {"lat": -37.79, "lon": 145.32},
                    {"lat": -37.792, "lon": 145.32},
                    {"lat": -37.792, "lon": 145.31},
                    {"lat": -37.79, "lon": 145.31},
                ],
            },
            {"type": "node", "id": 2},
            {"type": "way", "id": 3, "tags": {}, "geometry": []},
        ]
    }
    footprints = parse_overpass(payload)
    assert len(footprints) == 1
    footprint = footprints[0]
    assert footprint.height == pytest.approx(8.0)
    assert len(footprint.ring) == 4
    assert footprint.ring[0] == (145.31, -37.79)


def test_build_query_contains_bbox():
    query = build_query((145.0, -38.0, 146.0, -37.0))
    assert "way[\"building\"]" in query
    assert "(-38.0,145.0,-37.0,146.0)" in query
    assert "out geom" in query


def test_merge_prefers_osm_geometry_and_backfills_height():
    ml = [LocalFootprint(geometry=_square(10.0), source="ml", height=8.4)]
    osm = [LocalFootprint(geometry=_square(10.2), source="osm", height=None)]
    merged = merge_footprints(ml, osm, iou_threshold=0.45)
    assert len(merged) == 1
    assert merged[0].source == "osm+ml"
    assert merged[0].height == pytest.approx(8.4)
    assert merged[0].geometry.area == pytest.approx(10.2 * 10.2)


def test_merge_keeps_osm_height_when_present():
    ml = [LocalFootprint(geometry=_square(10.0), source="ml", height=8.4)]
    osm = [LocalFootprint(geometry=_square(10.0), source="osm", height=15.0)]
    merged = merge_footprints(ml, osm)
    assert len(merged) == 1
    assert merged[0].source == "osm"
    assert merged[0].height == pytest.approx(15.0)


def test_merge_keeps_unmatched_ml_and_osm():
    ml = [LocalFootprint(geometry=_square(10.0), source="ml", height=6.0)]
    osm = [
        LocalFootprint(
            geometry=Polygon([(100, 100), (110, 100), (110, 110), (100, 110)]),
            source="osm",
            height=12.0,
        )
    ]
    merged = merge_footprints(ml, osm)
    assert len(merged) == 2
    sources = {footprint.source for footprint in merged}
    assert sources == {"ml", "osm"}
