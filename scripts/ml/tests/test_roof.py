import math

import pytest
from scripts.ml.roof import minimum_area_rectangle, model_roof
from shapely.geometry import Polygon


def _rectangle(width: float, height: float) -> Polygon:
    return Polygon([(0, 0), (width, 0), (width, height), (0, height)])


def test_minimum_area_rectangle_dimensions():
    angle, long_side, short_side = minimum_area_rectangle(_rectangle(20, 6))
    assert long_side == pytest.approx(20, rel=1e-6)
    assert short_side == pytest.approx(6, rel=1e-6)
    assert angle % (math.pi / 2) == pytest.approx(0, abs=1e-6)


def test_flat_roof_for_low_buildings():
    model = model_roof(_rectangle(20, 6), 2.0)
    assert model.roof_type == "flat"
    assert model.eave_height == pytest.approx(2.0)
    assert model.ridge_height == pytest.approx(2.0)


def test_gabled_roof_for_elongated_footprint():
    model = model_roof(_rectangle(20, 6), 7.0)
    assert model.roof_type == "gabled"
    assert model.aspect == pytest.approx(20 / 6, rel=1e-2)
    assert model.eave_height < model.ridge_height
    assert model.eave_height >= 2.4


def test_hipped_roof_for_square_footprint():
    model = model_roof(_rectangle(10, 9), 8.0)
    assert model.roof_type == "hipped"
    assert model.ridge_height == pytest.approx(8.0)


def test_meters_per_unit_scales_rise():
    polygon = _rectangle(30, 10)
    small = model_roof(polygon, 9.0, meters_per_unit=0.4)
    large = model_roof(polygon, 9.0, meters_per_unit=1.0)
    assert small.eave_height > large.eave_height
    assert small.ridge_height == large.ridge_height == pytest.approx(9.0)


def test_none_height_defaults_flat():
    model = model_roof(_rectangle(10, 10), None)
    assert model.roof_type == "flat"
    assert model.eave_height == pytest.approx(3.0)
