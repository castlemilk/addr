import math

import pytest
from scripts.ml.addresses import (
    associate_addresses,
    classify_building,
    roof_metadata_for,
)
from shapely.geometry import Polygon

LAT = -37.79044
LON = 145.31252
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT))


def square(center_lon: float, center_lat: float, size_m: float = 20.0) -> Polygon:
    half_lat = (size_m / 2) / M_PER_DEG_LAT
    half_lon = (size_m / 2) / M_PER_DEG_LON
    return Polygon(
        [
            (center_lon - half_lon, center_lat - half_lat),
            (center_lon + half_lon, center_lat - half_lat),
            (center_lon + half_lon, center_lat + half_lat),
            (center_lon - half_lon, center_lat + half_lat),
        ]
    )


def building(size_m: float = 14.0, props: dict | None = None, meters_per_unit: float = 0.3):
    polygon = square(LON, LAT, size_m)
    default_props = {
        "height": 6.0,
        "eave_height": 2.8,
        "ridge_height": 6.0,
        "roof_type": "hipped",
        "ridge_angle": 42.0,
        "area_m2": size_m * size_m,
        "source": "ai",
    }
    return (polygon, props if props is not None else default_props, meters_per_unit)


def address(offset_east_m: float = 0.0, offset_north_m: float = 0.0, **extra) -> dict:
    return {
        "id": extra.pop("id", 1),
        "full_address": extra.pop("full_address", "1 TEST STREET, MOOROOLBARK VIC 3138"),
        "unit": extra.pop("unit", None),
        "lat": LAT + offset_north_m / M_PER_DEG_LAT,
        "lon": LON + offset_east_m / M_PER_DEG_LON,
        **extra,
    }


def test_classify_house():
    assert classify_building(None, 220, 5.0, 1) == "house"


def test_classify_units_when_shared_footprint():
    assert classify_building(None, 260, 6.0, 2) == "units"


def test_classify_school_overrides_shared_footprint():
    assert classify_building("primary_school", 260, 6.0, 2) == "school"


def test_classify_business_by_category_and_metrics():
    assert classify_building("cafe", 120, 4.0, 1) == "business"
    assert classify_building(None, 500, 4.0, 1) == "business"
    assert classify_building(None, 120, 13.0, 1) == "large"


def test_mobile_service_category_does_not_reclassify_house():
    assert classify_building("life_coach", 220, 5.0, 1) == "house"
    assert classify_building("home_service", 220, 5.0, 1) == "house"
    assert classify_building("driving_school", 220, 5.0, 1) == "house"


def test_associate_containing_address_returns_metric_ring():
    records = associate_addresses([building()], [address()])
    assert len(records) == 1
    record = records[0]
    assert record.kind == "house"
    assert record.distance_meters == 0.0
    assert record.address_count == 1
    assert len(record.ring_meters) == 4
    assert abs(record.width_meters - 14) < 0.5
    assert abs(record.depth_meters - 14) < 0.5


@pytest.mark.parametrize("extent", [4096, 8192])
def test_mvt_export_preserves_geographic_location_and_north_south(extent, tmp_path):
    """An asymmetric northern roof must not move to the south of its tile."""
    from scripts.ml.addresses import load_features
    from scripts.ml.tiles import MBTilesWriter, MVTFeature, encode_mvt_tile, tile_local_to_lonlat

    z, x, y = 18, 236880, 160830
    ring = [(500, 240), (1300, 240), (1300, 550), (950, 550), (950, 1000), (500, 1000)]
    path = tmp_path / "roof.mbtiles"
    writer = MBTilesWriter(path)
    writer.write_tile(z, x, y, encode_mvt_tile({"building": [MVTFeature.from_rings(ring)]}, extent))
    writer.close()
    result = load_features(path)
    expected = Polygon([tile_local_to_lonlat(px, py, x, y, z, extent) for px, py in ring])
    assert len(result) == 1
    assert result[0][0].hausdorff_distance(expected) < 1e-10
    assert result[0][0].intersection(expected).area / expected.area > .99999


def test_associate_outside_radius_is_skipped():
    records = associate_addresses([building()], [address(offset_east_m=90.0)])
    assert records == []


def test_associate_multiple_addresses_become_units():
    addresses = [
        address(id=1, unit="1", offset_east_m=-3.0),
        address(id=2, unit="2", offset_east_m=3.0),
    ]
    records = associate_addresses([building()], addresses)
    assert len(records) == 2
    assert all(record.kind == "units" for record in records)
    assert [record.address_count for record in records] == [2, 2]
    assert records[0].neighbours == ["1 TEST STREET, MOOROOLBARK VIC 3138"]


def test_duplicate_address_records_count_as_one_dwelling():
    addresses = [
        address(id=1, unit=None),
        address(id=2, unit="UNIT 1"),
    ]
    records = associate_addresses([building()], addresses)
    assert len(records) == 2
    assert all(record.kind == "house" for record in records)
    assert all(record.address_count == 1 for record in records)


def test_associate_nearest_building_wins():
    near = building(size_m=18.0)
    far_polygon = square(LON + 0.0005, LAT, 18.0)
    far = (far_polygon, near[1], 0.3)
    records = associate_addresses([near, far], [address()])
    assert len(records) == 1
    assert records[0].centroid == pytest.approx({"lat": LAT, "lon": LON}, abs=1e-5)


def test_place_category_drives_business_kind():
    records = associate_addresses(
        [building()],
        [address()],
        places=[{"name": "Corner Cafe", "category": "cafe", "lat": LAT, "lon": LON}],
    )
    assert records[0].kind == "business"
    assert records[0].category == "cafe"


def test_roof_metadata_uses_tile_props():
    polygon = square(LON, LAT)
    metadata = roof_metadata_for(
        polygon,
        {
            "height": 7.4,
            "eave_height": 4.8,
            "ridge_height": 7.4,
            "roof_type": "gabled",
            "ridge_angle": 12.0,
            "area_m2": 136.0,
            "source": "ai",
        },
        meters_per_unit=0.118,
    )
    assert metadata.roof_type == "gabled"
    assert metadata.eave_height == 4.8
    assert metadata.ridge_height == 7.4
    assert metadata.area_m2 == 136.0


def test_roof_metadata_falls_back_to_model():
    polygon = square(LON, LAT, 30.0)
    metadata = roof_metadata_for(polygon, {}, meters_per_unit=M_PER_DEG_LAT)
    assert metadata.roof_type in {"flat", "hipped", "gabled"}
    assert metadata.ridge_height >= metadata.eave_height
    assert metadata.area_m2 > 0
    assert metadata.source == "ai"


def test_record_canopy_metrics_detects_overlap():
    from scripts.ml.addresses import record_canopy_metrics

    ring = [[-10, -10], [10, -10], [10, 10], [-10, 10]]
    centroid = {"lat": -37.79, "lon": 145.31}
    from shapely.geometry import box

    cos_lat = 0.7907
    scale = 111320.0 * cos_lat
    tree = box(145.31, -37.79, 145.31 + 10 / scale, -37.79 + 10 / 111320.0)
    area_m2, share, overlapping, grade = record_canopy_metrics(
        ring,
        centroid,
        [(tree, {"area_m2": 100.0}, 0.1)],
    )
    assert overlapping == 1
    assert 90 < area_m2 < 110
    assert 0.2 < share < 0.3
    assert grade == "moderate"

    clean = record_canopy_metrics(
        ring,
        centroid,
        [(tree, {"area_m2": 100.0}, 0.1)],
        min_tree_area_m2=1000.0,
    )
    assert clean[2] == 0 and clean[3] == "clean"


def test_record_quality_flags_and_grades():
    from scripts.ml.addresses import record_quality

    clean = record_quality(
        isolation="clean", boundary_source="extracted", distance_meters=3.0,
        area_m2=200.0, eave_height=4.0, kind="house", overlapping_trees=0,
    )
    assert clean == (100, "A", [])

    obstructed = record_quality(
        isolation="obstructed", boundary_source="extracted", distance_meters=0.0,
        area_m2=500.0, eave_height=2.4, kind="units", overlapping_trees=2,
    )
    score, grade, flags = obstructed
    assert grade == "C"
    assert "canopy-obstructed" in flags
    assert "point-inside-large" not in flags
    assert "clamped-eave" in flags
    assert "multi-canopy" in flags

    corrected = record_quality(
        isolation="clean", boundary_source="crm", distance_meters=25.0,
        area_m2=300.0, eave_height=3.0, kind="house", overlapping_trees=0,
    )
    score, grade, flags = corrected
    assert flags == ["crm-corrected"]
    assert grade == "A"

    uncorrected_far = record_quality(
        isolation="clean", boundary_source="extracted", distance_meters=25.0,
        area_m2=300.0, eave_height=3.0, kind="house", overlapping_trees=0,
    )
    assert "far-address-point" in uncorrected_far[2]

    normal_placement = record_quality(
        isolation="clean", boundary_source="extracted", distance_meters=10.0,
        area_m2=220.0, eave_height=4.0, kind="house", overlapping_trees=0,
    )
    assert normal_placement == (100, "A", [])

    outbuilding = record_quality(
        isolation="clean", boundary_source="extracted", distance_meters=9.0,
        area_m2=14.0, eave_height=4.6, kind="outbuilding", overlapping_trees=0,
    )
    assert outbuilding[0] == 100
    assert "tiny-footprint" not in outbuilding[2]

    tiny = record_quality(
        isolation="clean", boundary_source="extracted", distance_meters=2.0,
        area_m2=12.0, eave_height=3.0, kind="house", overlapping_trees=0,
    )
    assert tiny[1] == "B"
    assert "tiny-footprint" in tiny[2]


def test_classify_tiny_footprint_is_outbuilding():
    from scripts.ml.addresses import classify_building

    assert classify_building(None, 14.0, 4.6, 1) == "outbuilding"
    assert classify_building(None, 120.0, 4.6, 1) == "house"
    assert classify_building(None, 14.0, 4.6, 2) == "outbuilding"


def test_inherit_unit_rings_rescues_nearby_unit():
    from scripts.ml.addresses import inherit_unit_rings

    buildings = [building(size_m=20.0)]
    base = address(id=1, full_address="29 TEST STREET, MOOROOLBARK VIC 3138")
    base["number"], base["street"] = "29", "TEST STREET"
    records = associate_addresses(buildings, [base], max_distance_m=12.0)
    assert len(records) == 1

    unit = address(id=2, offset_east_m=18.0, full_address="Unit UNIT 1 29 TEST STREET, MOOROOLBARK VIC 3138")
    unit["unit"], unit["number"], unit["street"] = "UNIT 1", "29", "TEST STREET"
    rescued = inherit_unit_rings(records, [base, unit])
    assert rescued == 1
    assert len(records) == 2
    clone = next(record for record in records if record.address_id == 2)
    assert clone.ring_meters == records[0].ring_meters
    assert clone.centroid == records[0].centroid
    assert "inherited-ring" in clone.quality_flags
    assert clone.quality_score == clone.quality_score  # score computed, flag unpenalised
    assert "Unit UNIT 1 29 TEST STREET, MOOROOLBARK VIC 3138" in records[0].neighbours


def test_inherit_unit_rings_skips_far_plain_and_suppressed():
    from scripts.ml.addresses import inherit_unit_rings

    buildings = [building(size_m=20.0)]
    base = address(id=1, full_address="29 TEST STREET, MOOROOLBARK VIC 3138")
    base["number"], base["street"] = "29", "TEST STREET"
    records = associate_addresses(buildings, [base], max_distance_m=12.0)

    far_unit = address(id=2, offset_east_m=60.0, full_address="Unit UNIT 9 29 TEST STREET, MOOROOLBARK VIC 3138")
    far_unit["unit"], far_unit["number"], far_unit["street"] = "UNIT 9", "29", "TEST STREET"
    plain = address(id=3, offset_east_m=18.0, full_address="31 TEST STREET, MOOROOLBARK VIC 3138")
    plain["number"], plain["street"] = "31", "TEST STREET"
    near_unit = address(id=4, offset_east_m=18.0, full_address="Unit UNIT 2 29 TEST STREET, MOOROOLBARK VIC 3138")
    near_unit["unit"], near_unit["number"], near_unit["street"] = "UNIT 2", "29", "TEST STREET"
    rescued = inherit_unit_rings(
        records, [base, far_unit, plain, near_unit], suppressed={4}
    )
    assert rescued == 0
    assert len(records) == 1


def test_mark_shared_rings_lists_co_claimants():
    from scripts.ml.addresses import mark_shared_rings

    buildings = [building(size_m=20.0)]
    first = address(id=1, full_address="27 TEST STREET, MOOROOLBARK VIC 3138")
    second = address(id=2, full_address="Unit UNIT 4 29 TEST STREET, MOOROOLBARK VIC 3138")
    records = associate_addresses(buildings, [first, second], max_distance_m=12.0)
    assert len(records) == 2
    assert mark_shared_rings(records) == 2
    assert records[0].shared_with == [2]
    assert records[1].shared_with == [1]


def test_apply_crm_carries_roof_material():
    import json as json_module
    import sqlite3
    import tempfile
    from pathlib import Path as FsPath

    from scripts.ml.addresses import AddressRoof, apply_crm_roof_overrides

    db = FsPath(tempfile.mkdtemp()) / "crm.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE crm_features (address_id INTEGER, kind TEXT, geometry_json TEXT, properties TEXT)"
    )
    ring = [[145.31, -37.79], [145.32, -37.79], [145.32, -37.78], [145.31, -37.78]]
    connection.execute(
        "INSERT INTO crm_features VALUES (?, ?, ?, ?)",
        (
            1,
            "roof",
            json_module.dumps({"type": "Polygon", "coordinates": [ring + [ring[0]]]}),
            json_module.dumps({"roof_material": " Terracotta "}),
        ),
    )
    connection.commit()
    connection.close()
    record = AddressRoof(
        address_id=1, full_address="1 TEST STREET", unit=None, category=None,
        kind="house", height=5.0, eave_height=2.8, ridge_height=5.0,
        roof_type="hipped", ridge_angle=0.0, area_m2=200.0, levels=None,
        source="ai", address_point={"lat": -37.79, "lon": 145.31},
        centroid={"lat": -37.79, "lon": 145.31}, ring_meters=[[0, 0], [1, 0], [1, 1]],
        width_meters=1.0, depth_meters=1.0, distance_meters=2.0,
        address_count=1, neighbours=[],
    )
    assert record.roof_material is None
    assert apply_crm_roof_overrides([record], db) == 1
    assert record.roof_material == "terracotta"
    assert record.boundary_source == "crm"
