"""Roof parameter estimation for 3D house modelling.

Combines the refined footprint outline with the depth-derived roof-top height
to estimate eave height, ridge height, roof type, and ridge orientation. The
parameters are emitted as vector-tile properties and consumed by the Three.js
roof mesh layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

from shapely.geometry import Polygon

DEFAULT_PITCH_RATIO = 0.45
MIN_RISE_M = 0.9
MIN_EAVE_M = 2.4


@dataclass
class RoofModel:
    roof_type: str
    eave_height: float
    ridge_height: float
    ridge_angle_deg: float
    aspect: float


def minimum_area_rectangle(polygon: Polygon) -> tuple[float, float, float]:
    """Returns (angle_radians, long_side, short_side) of the minimum-area rectangle."""
    hull = polygon.convex_hull
    if not isinstance(hull, Polygon) or hull.is_empty:
        return 0.0, 0.0, 0.0
    coordinates = list(cast(Polygon, hull).exterior.coords)[:-1]
    if len(coordinates) < 3:
        return 0.0, 0.0, 0.0

    best = (0.0, 0.0, 0.0)
    best_area = math.inf
    for index in range(len(coordinates)):
        x0, y0 = coordinates[index]
        x1, y1 = coordinates[(index + 1) % len(coordinates)]
        edge = math.hypot(x1 - x0, y1 - y0)
        if edge < 1e-9:
            continue
        ux, uy = (x1 - x0) / edge, (y1 - y0) / edge
        min_u = min_v = math.inf
        max_u = max_v = -math.inf
        for px, py in coordinates:
            u = (px - x0) * ux + (py - y0) * uy
            v = -(px - x0) * uy + (py - y0) * ux
            min_u, max_u = min(min_u, u), max(max_u, u)
            min_v, max_v = min(min_v, v), max(max_v, v)
        width = max_u - min_u
        height = max_v - min_v
        area = width * height
        if area < best_area:
            best_area = area
            if width >= height:
                best = (math.atan2(uy, ux), width, height)
            else:
                best = (math.atan2(ux, -uy), height, width)

    angle, long_side, short_side = best
    angle = angle % math.pi
    return angle, long_side, short_side


def model_roof(
    polygon: Polygon, height: float | None, meters_per_unit: float = 1.0
) -> RoofModel:
    """Estimates a pitched-roof model from footprint geometry and roof-top height.

    `meters_per_unit` converts footprint dimensions into metres (defaults to 1.0
    for polygons already expressed in metres).
    """
    angle, long_side, short_side = minimum_area_rectangle(polygon)
    long_side *= meters_per_unit
    short_side *= meters_per_unit
    if short_side <= 1e-6:
        short_side = math.sqrt(max(polygon.area, 1.0)) * meters_per_unit
    aspect = long_side / short_side if short_side > 0 else 1.0
    ridge_angle_deg = math.degrees(angle) % 180.0

    if height is None:
        return RoofModel("flat", 3.0, 3.0, ridge_angle_deg, aspect)

    rise = max(MIN_RISE_M, short_side * 0.5 * DEFAULT_PITCH_RATIO)
    rise = min(rise, max(0.0, height - MIN_EAVE_M))
    if rise < MIN_RISE_M or height < MIN_EAVE_M + MIN_RISE_M:
        return RoofModel("flat", height, height, ridge_angle_deg, aspect)

    eave = max(MIN_EAVE_M, height - rise)
    roof_type = "gabled" if aspect >= 1.7 else "hipped"
    return RoofModel(
        roof_type, round(eave, 1), round(height, 1), round(ridge_angle_deg, 1), round(aspect, 2)
    )
