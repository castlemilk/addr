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
