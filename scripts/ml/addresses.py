"""Per-address roof extraction and building characterisation.

Associates G-NAF address points with AI-extracted building footprints from the
tile archive, then emits one record per address containing the roof polygon in
metres, roof model metadata (type/eave/ridge/orientation), area, and a building
kind (house/units/school/business/industrial/civic). Addresses that share a
footprint are linked as units of a multi-dwelling so downstream 3D rendering can
model them consistently.

Workflow (mirrors the spatial model):
  1. extract per-address boundaries (nearest/containing AI roof footprint)
  2. enrich with roof metadata (SAM3-refined tile props or the roof model)
  3. emit per-address records for the 3D reconstruction layer
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sqlite3
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import mapbox_vector_tile
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

from .footprints import LocalFootprint, dedupe_footprints
from .roof import model_roof
from .tiles import tile_bounds, tile_local_to_lonlat

METERS_PER_DEGREE = 111_320.0
SCRIPT_MAX_PROPS = 5

SCHOOL_CATEGORIES = {
    "school",
    "elementary_school",
    "middle_school",
    "high_school",
    "primary_school",
    "secondary_school",
    "university",
    "college",
    "academy",
    "preschool",
}
INDUSTRIAL_CATEGORIES = {
    "industrial",
    "warehouse",
    "factory",
    "logistics",
    "manufacturing",
    "storage",
}
CIVIC_CATEGORIES = {
    "church",
    "church_cathedral",
    "library",
    "city_hall",
    "government_office",
    "police_station",
    "fire_station",
    "post_office",
    "community_center",
    "courthouse",
}
BUSINESS_CATEGORIES = {
    "shopping",
    "shopping_center",
    "supermarket",
    "grocery_store",
    "department_store",
    "convenience_store",
    "hotel",
    "lodging",
    "motel",
    "bank",
    "financial_service",
    "hospital",
    "medical_clinic",
    "pharmacy",
    "restaurant",
    "cafe",
    "fast_food_restaurant",
    "bar",
    "office",
    "corporate_office",
    "real_estate_agency",
    "car_dealer",
    "fitness_center",
    "gym",
    "childcare",
    "kindergarten",
    "veterinary_care",
}
CATEGORY_SETS = {
    "school": SCHOOL_CATEGORIES,
    "industrial": INDUSTRIAL_CATEGORIES,
    "civic": CIVIC_CATEGORIES,
    "business": BUSINESS_CATEGORIES,
}


@dataclass
class RoofMetadata:
    roof_type: str
    eave_height: float
    ridge_height: float
    height: float
    ridge_angle: float
    area_m2: float
    source: str


@dataclass
class AddressRoof:
    address_id: int
    full_address: str
    unit: str | None
    category: str | None
    kind: str
    height: float
    eave_height: float
    ridge_height: float
    roof_type: str
    ridge_angle: float
    area_m2: float
    levels: int | None
    source: str
    address_point: dict
    centroid: dict
    ring_meters: list[list[float]]
    width_meters: float
    depth_meters: float
    distance_meters: float
    address_count: int
    neighbours: list[str]
    boundary_source: str = "extracted"
    canopy_over_roof_m2: float = 0.0
    canopy_over_roof_share: float = 0.0
    overlapping_trees: int = 0
    isolation: str = "clean"
    quality_score: int = 100
    quality_grade: str = "A"
    quality_flags: list[str] = field(default_factory=list)
    suppressed_centroid: dict | None = None
    suppressed_ring_meters: list[list[float]] | None = None
    rooflines: dict | None = None
    # Roof surface material (e.g. "terracotta" for red tile): set only from a
    # CRM correction, drives mesh colour in the 3D views, never geometry.
    roof_material: str | None = None
    # Explicit CRM roof-family correction; separate from inferred tile metadata.
    roof_form_source: str | None = None
    # Other address ids claiming (near-)identical geometry (IoU >= 0.9):
    # normal for unit complexes sharing one building, but a standalone house
    # sharing a units block's ring (e.g. 27 Cambridge vs Unit 4/29) is suspect.
    shared_with: list[int] = field(default_factory=list)


def classify_building(
    category: str | None,
    area_m2: float,
    height_m: float,
    address_count: int,
) -> str:
    """Maps premises category plus footprint metrics to a building kind.

    Only exact premises categories count: the places feed also contains mobile
    services registered at residential addresses, which must not reclassify a
    house.
    """
    if area_m2 < 25:
        return "outbuilding"

    normalized = (category or "").strip().lower()
    for kind, categories in CATEGORY_SETS.items():
        if normalized in categories:
            if kind == "school" or kind == "industrial" or kind == "civic":
                return kind
            if address_count >= 8 or area_m2 >= 900:
                return "large"
            return kind
    if address_count >= 8 or area_m2 >= 900 or height_m >= 12:
        return "large"
    if address_count >= 2:
        return "units"
    if area_m2 >= 400 or height_m >= 9:
        return "business"
    return "house"


def _ring_to_meters(
    ring: Iterable[Sequence[float]], center_lon: float, center_lat: float
) -> list[list[float]]:
    cos_lat = math.cos(math.radians(center_lat))
    return [
        [(lon - center_lon) * METERS_PER_DEGREE * cos_lat, (lat - center_lat) * METERS_PER_DEGREE]
        for lon, lat in ring
    ]


def _numeric(value: float | int | str | None, fallback: float = 0.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def roof_metadata_for(polygon: Polygon, properties: dict, meters_per_unit: float) -> RoofMetadata:
    """Reads roof metadata from tile props, falling back to the roof model."""
    height = _numeric(properties.get("render_height") or properties.get("height"))
    eave = _numeric(properties.get("eave_height"))
    ridge = _numeric(properties.get("ridge_height"))
    roof_type = str(properties.get("roof_type") or "")
    angle = _numeric(properties.get("ridge_angle"))
    area = _numeric(properties.get("area_m2"))
    if not area:
        area = polygon.area * (meters_per_unit**2)
    if not roof_type or ridge < height or eave >= ridge:
        model = model_roof(polygon, height or 5.5, meters_per_unit=meters_per_unit)
        roof_type = model.roof_type
        eave = model.eave_height
        ridge = model.ridge_height
        angle = model.ridge_angle_deg
    return RoofMetadata(
        roof_type=roof_type,
        eave_height=round(eave, 2),
        ridge_height=round(ridge, 2),
        height=round(max(height, ridge), 2),
        ridge_angle=round(angle, 1),
        area_m2=round(area, 1),
        source=str(properties.get("source") or "ai"),
    )


def _isolation_grade(share: float) -> str:
    if share < 0.02:
        return "clean"
    if share < 0.1:
        return "light"
    if share < 0.25:
        return "moderate"
    return "obstructed"


def record_canopy_metrics(
    ring_meters: list[list[float]],
    centroid: dict,
    trees: list[tuple[Polygon, dict, float]],
    *,
    min_tree_area_m2: float = 4.0,
) -> tuple[float, float, int, str]:
    """Canopy area/share overlapping a record's roof ring."""
    if not ring_meters or not trees:
        return 0.0, 0.0, 0, "clean"
    clat, clon = centroid["lat"], centroid["lon"]
    cos_lat = math.cos(math.radians(clat))
    polygon = Polygon(
        [
            (
                clon + x / (METERS_PER_DEGREE * cos_lat),
                clat + y / METERS_PER_DEGREE,
            )
            for x, y in ring_meters
        ]
    )
    if polygon.is_empty or polygon.area <= 0:
        return 0.0, 0.0, 0, "clean"
    ring_area_m2 = polygon.area * (METERS_PER_DEGREE**2) * cos_lat
    bounds = polygon.bounds
    canopy_m2 = 0.0
    overlapping = 0
    for tree_polygon, properties, _ in trees:
        if float(properties.get("area_m2") or 0.0) < min_tree_area_m2:
            continue
        tree_bounds = tree_polygon.bounds
        if tree_bounds[2] < bounds[0] or tree_bounds[0] > bounds[2]:
            continue
        if tree_bounds[3] < bounds[1] or tree_bounds[1] > bounds[3]:
            continue
        if not polygon.intersects(tree_polygon):
            continue
        overlapping += 1
        canopy_m2 += (
            polygon.intersection(tree_polygon).area * (METERS_PER_DEGREE**2) * cos_lat
        )
    share = canopy_m2 / ring_area_m2 if ring_area_m2 > 0 else 0.0
    return (
        round(canopy_m2, 1),
        round(share, 3),
        overlapping,
        _isolation_grade(share),
    )


def record_quality(
    *,
    isolation: str,
    boundary_source: str,
    distance_meters: float,
    area_m2: float,
    eave_height: float,
    kind: str,
    overlapping_trees: int,
) -> tuple[int, str, list[str]]:
    """Explainable per-address quality score (100 best) with reason flags."""
    score = 100
    flags: list[str] = []

    if isolation == "light":
        score -= 5
        flags.append("canopy-light")
    elif isolation == "moderate":
        score -= 15
        flags.append("canopy-moderate")
    elif isolation == "obstructed":
        score -= 30
        flags.append("canopy-obstructed")
    if overlapping_trees >= 2:
        score -= 5
        flags.append("multi-canopy")

    if boundary_source == "crm":
        flags.append("crm-corrected")
    # G-NAF points sit at property frontages, so 8-15 m to the building is
    # normal placement, not an error; only >20 m is considered far. Verified
    # CRM corrections were accepted by a reviewer, so placement is settled.
    if boundary_source != "crm":
        if distance_meters > 20:
            score -= 15
            flags.append("far-address-point")
        elif distance_meters > 12:
            score -= 5
            flags.append("distant-address-point")

    # An eave at the model's clamp is uncertainty about height, not a defect:
    # 2.4 m is a typical single-storey eave.
    if eave_height <= 2.5 and area_m2 > 200:
        score -= 5
        flags.append("clamped-eave")
    if area_m2 < 20 and kind != "outbuilding":
        score -= 20
        flags.append("tiny-footprint")
    if kind == "house" and area_m2 > 500:
        score -= 10
        flags.append("large-for-house")
    if kind in ("large", "business") and area_m2 < 120:
        score -= 5
        flags.append("small-for-kind")

    score = max(0, min(100, score))
    if score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    else:
        grade = "D"
    return score, grade, flags


def associate_addresses(
    buildings: list[tuple[Polygon, dict, float]],
    addresses: list[dict],
    places: list[dict] | None = None,
    max_distance_m: float = 20.0,
    category_radius_m: float = 12.0,
    trees: list[tuple[Polygon, dict, float]] | None = None,
) -> list[AddressRoof]:
    """Matches address points to building footprints and builds roof records."""
    if not buildings:
        return []

    polygons = [item[0] for item in buildings]
    tree = STRtree(polygons)
    place_list = places or []
    place_points = [Point(place["lon"], place["lat"]) for place in place_list]
    place_tree = STRtree(place_points) if place_points else None

    assignments: list[int | None] = []
    distances: list[float] = []
    for address in addresses:
        point = Point(address["lon"], address["lat"])
        index = tree.query(point, predicate="contains")
        distance = 0.0
        if len(index) == 0:
            nearest = tree.query_nearest(point, max_distance=max_distance_m)
            if len(nearest) > 0:
                index = [int(nearest[0])]
                distance = point.distance(polygons[int(nearest[0])]) * METERS_PER_DEGREE
                if distance > max_distance_m:
                    index = []
        assignments.append(int(index[0]) if len(index) else None)
        distances.append(distance)

    counts: dict[int, int] = {}
    dwelling_keys: dict[int, set[tuple[float, float]]] = {}
    for position, index in enumerate(assignments):
        if index is None:
            continue
        key = (
            round(addresses[position]["lat"], 5),
            round(addresses[position]["lon"], 5),
        )
        dwelling_keys.setdefault(index, set()).add(key)
    for index, keys in dwelling_keys.items():
        counts[index] = len(keys)

    building_positions: dict[int, list[int]] = {}
    for position, index in enumerate(assignments):
        if index is not None:
            building_positions.setdefault(index, []).append(position)

    records: list[AddressRoof] = []
    for position, index in enumerate(assignments):
        if index is None:
            continue
        polygon, properties, meters_per_unit = buildings[index]
        cos_lat = math.cos(math.radians(polygon.centroid.y))
        address = addresses[position]
        point = Point(address["lon"], address["lat"])
        category = address.get("category")

        if place_tree is not None and not category:
            nearest_place = place_tree.query_nearest(point, max_distance=category_radius_m)
            if len(nearest_place) > 0:
                category = place_list[int(nearest_place[0])].get("category")

        roof = roof_metadata_for(polygon, properties, meters_per_unit)
        ring = _ring_to_meters(polygon.exterior.coords, polygon.centroid.x, polygon.centroid.y)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        bounds = polygon.bounds
        width_m = (bounds[2] - bounds[0]) * METERS_PER_DEGREE * cos_lat
        depth_m = (bounds[3] - bounds[1]) * METERS_PER_DEGREE
        count = counts.get(index, 1)
        kind = classify_building(category, roof.area_m2, roof.height, count)
        neighbours = [
            addresses[other]["full_address"]
            for other in building_positions.get(index, [])
            if other != position
        ][:SCRIPT_MAX_PROPS]

        canopy_m2, canopy_share, overlapping_trees, isolation = record_canopy_metrics(
            ring,
            {"lat": polygon.centroid.y, "lon": polygon.centroid.x},
            trees or [],
        )
        quality_score, quality_grade, quality_flags = record_quality(
            isolation=isolation,
            boundary_source="extracted",
            distance_meters=round(distances[position], 2),
            area_m2=roof.area_m2,
            eave_height=roof.eave_height,
            kind=kind,
            overlapping_trees=overlapping_trees,
        )

        records.append(
            AddressRoof(
                address_id=int(address.get("id", 0)),
                full_address=address.get("full_address", ""),
                unit=address.get("unit"),
                category=category,
                kind=kind,
                height=roof.height,
                eave_height=roof.eave_height,
                ridge_height=roof.ridge_height,
                roof_type=roof.roof_type,
                ridge_angle=roof.ridge_angle,
                area_m2=roof.area_m2,
                levels=int(address["levels"]) if address.get("levels") else None,
                source=roof.source,
                address_point={"lat": address["lat"], "lon": address["lon"]},
                centroid={"lat": polygon.centroid.y, "lon": polygon.centroid.x},
                ring_meters=ring,
                width_meters=round(width_m, 1),
                depth_meters=round(depth_m, 1),
                distance_meters=round(distances[position], 2),
                address_count=count,
                neighbours=neighbours,
                canopy_over_roof_m2=canopy_m2,
                canopy_over_roof_share=canopy_share,
                overlapping_trees=overlapping_trees,
                isolation=isolation,
                quality_score=quality_score,
                quality_grade=quality_grade,
                quality_flags=quality_flags,
            )
        )
    return records


#: Unit points (letterboxes, driveways) scatter away from the building, so the
#: fixed association radius strands them while their base number matches. An
#: unmatched unit inherits its street-number sibling's ring within this radius.
INHERIT_SIBLING_RADIUS_M = 30.0

#: Pairwise ring overlap at or above this marks records as co-claimants.
SHARED_RING_IOU = 0.9


def _record_lonlat_polygon(record: AddressRoof) -> Polygon | None:
    cos_lat = math.cos(math.radians(record.centroid["lat"]))
    points = [
        (
            record.centroid["lon"] + x / (METERS_PER_DEGREE * cos_lat),
            record.centroid["lat"] + y / METERS_PER_DEGREE,
        )
        for x, y in record.ring_meters
    ]
    polygon = Polygon(points)
    if polygon.is_empty or not polygon.is_valid:
        return None
    return polygon


def inherit_unit_rings(
    records: list[AddressRoof],
    addresses: list[dict],
    *,
    radius_m: float = INHERIT_SIBLING_RADIUS_M,
    suppressed: set[int] | frozenset[int] = frozenset(),
) -> int:
    """Rescues unmatched unit addresses from their street-number sibling's ring.

    Units share their development's building by definition, but their G-NAF
    points often sit beyond the association radius while the base number (or a
    sibling unit) matched. Each rescued record clones the nearest same-number
    ring within ``radius_m`` of the unit point and carries an informational
    ``inherited-ring`` flag (no score penalty — the geometry is the sibling's
    measured footprint, only the attribution is coarse).
    """
    matched_ids = {record.address_id for record in records}
    siblings: dict[tuple[str | None, str | None], list[AddressRoof]] = {}
    for record in records:
        if record.address_id in suppressed:
            continue
        row = next(
            (item for item in addresses if int(item.get("id", 0)) == record.address_id),
            None,
        )
        if row is None:
            continue
        siblings.setdefault((row.get("number"), row.get("street")), []).append(record)

    inherited = 0
    for address in addresses:
        address_id = int(address.get("id", 0))
        if address_id in matched_ids or address_id in suppressed:
            continue
        if not (address.get("unit") or "").strip():
            continue
        candidates = siblings.get((address.get("number"), address.get("street")), [])
        if not candidates:
            continue
        point = Point(address["lon"], address["lat"])
        cos_lat = math.cos(math.radians(address["lat"]))
        best: AddressRoof | None = None
        best_distance = radius_m
        for sibling in candidates:
            polygon = _record_lonlat_polygon(sibling)
            if polygon is None:
                continue
            distance = point.distance(polygon) * METERS_PER_DEGREE * cos_lat
            if distance <= best_distance:
                best, best_distance = sibling, distance
        if best is None:
            continue
        kind = classify_building(None, best.area_m2, best.height, best.address_count)
        quality_score, quality_grade, quality_flags = record_quality(
            isolation=best.isolation,
            boundary_source=best.boundary_source,
            distance_meters=round(best_distance, 2),
            area_m2=best.area_m2,
            eave_height=best.eave_height,
            kind=kind,
            overlapping_trees=best.overlapping_trees,
        )
        quality_flags = [*quality_flags, "inherited-ring"]
        neighbours = [name for name in best.neighbours if name != address["full_address"]]
        neighbours = [*neighbours, best.full_address][:SCRIPT_MAX_PROPS]
        best.neighbours = [
            name for name in [*best.neighbours, address["full_address"]][:SCRIPT_MAX_PROPS]
        ]
        records.append(
            AddressRoof(
                address_id=address_id,
                full_address=address.get("full_address", ""),
                unit=address.get("unit"),
                category=None,
                kind=kind,
                height=best.height,
                eave_height=best.eave_height,
                ridge_height=best.ridge_height,
                roof_type=best.roof_type,
                ridge_angle=best.ridge_angle,
                area_m2=best.area_m2,
                levels=None,
                source=best.source,
                address_point={"lat": address["lat"], "lon": address["lon"]},
                centroid=dict(best.centroid),
                ring_meters=[list(point) for point in best.ring_meters],
                width_meters=best.width_meters,
                depth_meters=best.depth_meters,
                distance_meters=round(best_distance, 2),
                address_count=best.address_count,
                neighbours=neighbours,
                boundary_source=best.boundary_source,
                canopy_over_roof_m2=best.canopy_over_roof_m2,
                canopy_over_roof_share=best.canopy_over_roof_share,
                overlapping_trees=best.overlapping_trees,
                isolation=best.isolation,
                quality_score=quality_score,
                quality_grade=quality_grade,
                quality_flags=quality_flags,
            )
        )
        matched_ids.add(address_id)
        inherited += 1
    return inherited


def mark_shared_rings(
    records: list[AddressRoof], *, iou_threshold: float = SHARED_RING_IOU
) -> int:
    """Lists co-claimant address ids on records with (near-)identical rings.

    Returns the number of records sharing with at least one other address.
    """
    polygons = {record.address_id: _record_lonlat_polygon(record) for record in records}
    marked = 0
    for record in records:
        polygon = polygons[record.address_id]
        if polygon is None:
            record.shared_with = []
            continue
        co_claimants = []
        for other in records:
            if other.address_id == record.address_id:
                continue
            candidate = polygons[other.address_id]
            if candidate is None:
                continue
            union = polygon.union(candidate).area
            if union <= 0:
                continue
            if polygon.intersection(candidate).area / union >= iou_threshold:
                co_claimants.append(other.address_id)
        record.shared_with = sorted(co_claimants)
        if co_claimants:
            marked += 1
    return marked


def load_features(
    tiles_path: Path, layer_name: str = "building"
) -> list[tuple[Polygon, dict, float]]:
    """Decodes one MVT layer from an MBTiles archive into WGS84 polygons."""
    connection = sqlite3.connect(f"file:{tiles_path}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
    ).fetchall()
    connection.close()

    features: list[tuple[Polygon, dict, float]] = []
    seen: set[tuple[float, float]] = set()
    for zoom, column, row, data in rows:
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        # MVT and tile_local_to_lonlat both use y southward. The decoder's
        # default flips y to Cartesian coordinates, mirroring each whole tile.
        layers = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})
        layer = layers.get(layer_name)
        if not layer:
            continue
        tile_row_tms = row
        y = (1 << zoom) - 1 - tile_row_tms
        west, south, east, north = tile_bounds(column, y, zoom)
        extent = layer.get("extent", 4096)
        span = east - west
        meters_per_unit = span * METERS_PER_DEGREE * math.cos(math.radians((north + south) / 2)) / extent
        for feature in layer["features"]:
            geometry = feature["geometry"]
            polygons = (
                [geometry["coordinates"]]
                if geometry["type"] == "Polygon"
                else geometry["coordinates"]
            )
            for polygon_coords in polygons:
                world = []
                for ring in polygon_coords:
                    world.append(
                        [
                            tile_local_to_lonlat(x, yy, column, y, zoom, extent)
                            for x, yy in ring
                        ]
                    )
                try:
                    polygon = Polygon(world[0], world[1:])
                except (ValueError, IndexError):
                    continue
                if polygon.is_empty or not polygon.is_valid:
                    continue
                key = (round(polygon.centroid.x, 5), round(polygon.centroid.y, 5))
                if key in seen:
                    continue
                seen.add(key)
                features.append((polygon, feature["properties"], meters_per_unit))

    return features


def load_buildings(tiles_path: Path) -> list[tuple[Polygon, dict, float]]:
    """Building footprints with duplicates/nested fragments removed."""
    buildings = load_features(tiles_path, "building")
    if len(buildings) > 1:
        proxies = []
        by_geometry: dict[int, tuple[Polygon, dict, float]] = {}
        for item in buildings:
            polygon, properties, meters_per_unit = item
            by_geometry[id(polygon)] = item
            proxies.append(
                LocalFootprint(
                    geometry=polygon,
                    source=str(properties.get("source") or "ml"),
                    roof_type=properties.get("roof_type"),
                    eave_height=properties.get("eave_height"),
                    ridge_height=properties.get("ridge_height"),
                )
            )
        kept = dedupe_footprints(proxies)
        buildings = [by_geometry[id(proxy.geometry)] for proxy in kept]
    return buildings


def load_trees(tiles_path: Path) -> list[tuple[Polygon, dict, float]]:
    """Decoded tree-canopy layer (centroid-deduped across tiles)."""
    return load_features(tiles_path, "tree")


def load_addresses(db_path: Path) -> list[dict]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT id, unit, number, street, full_address, lat, lon FROM addresses"
    ).fetchall()
    connection.close()
    return [
        {
            "id": row[0],
            "unit": row[1],
            "number": row[2],
            "street": row[3],
            "full_address": row[4],
            "lat": row[5],
            "lon": row[6],
        }
        for row in rows
    ]


def load_suppressed_addresses(db_path: Path) -> set[int]:
    """Address ids marked suppressed in the CRM (confirmed false positives)."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT address_id FROM crm_address_meta WHERE lower(status) = 'suppressed'"
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        connection.close()
    return {int(row[0]) for row in rows}


def apply_crm_roof_overrides(
    records: list[AddressRoof],
    db_path: Path,
    trees: list[tuple[Polygon, dict, float]] | None = None,
) -> int:
    """Replaces record geometry with CRM roof corrections (review-loop feedback)."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT address_id, geometry_json, properties FROM crm_features WHERE kind = 'roof'"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    finally:
        connection.close()

    by_address = {record.address_id: record for record in records}
    applied = 0
    for address_id, geometry_json, properties_json in rows:
        record = by_address.get(address_id)
        if record is None:
            continue
        try:
            geometry = json.loads(geometry_json)
            coordinates = geometry["coordinates"][0]
            polygon = Polygon([(float(lon), float(lat)) for lon, lat in coordinates])
        except (ValueError, KeyError, TypeError, IndexError):
            continue
        if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
            continue
        if record.boundary_source != "crm":
            # Preserve the original extracted footprint as the training "before";
            # repeated regenerations must not overwrite it with prior overrides.
            record.suppressed_centroid = dict(record.centroid)
            record.suppressed_ring_meters = [list(point) for point in record.ring_meters]
        cos_lat = math.cos(math.radians(polygon.centroid.y))
        ring = _ring_to_meters(polygon.exterior.coords, polygon.centroid.x, polygon.centroid.y)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        if not ring:
            continue
        record.ring_meters = ring
        record.centroid = {"lat": polygon.centroid.y, "lon": polygon.centroid.x}
        record.area_m2 = round(polygon.area * METERS_PER_DEGREE * METERS_PER_DEGREE * cos_lat, 1)
        bounds = polygon.bounds
        record.width_meters = round((bounds[2] - bounds[0]) * METERS_PER_DEGREE * cos_lat, 1)
        record.depth_meters = round((bounds[3] - bounds[1]) * METERS_PER_DEGREE, 1)
        point_lat = record.address_point["lat"]
        point_lon = record.address_point["lon"]
        record.distance_meters = round(
            math.hypot(
                (polygon.centroid.y - point_lat) * METERS_PER_DEGREE,
                (polygon.centroid.x - point_lon) * METERS_PER_DEGREE * math.cos(math.radians(point_lat)),
            ),
            2,
        )
        try:
            extra = json.loads(properties_json or "{}")
        except ValueError:
            extra = {}
        if isinstance(extra, dict):
            for source_key, target_key in (
                ("eave_height", "eave_height"),
                ("ridge_height", "ridge_height"),
                ("height", "height"),
            ):
                value = extra.get(source_key)
                if value is None:
                    continue
                try:
                    setattr(record, target_key, round(float(value), 2))
                except (TypeError, ValueError):
                    continue
            if extra.get("roof_type"):
                record.roof_type = str(extra["roof_type"])
            if extra.get("roof_form_source") == "reviewer":
                record.roof_form_source = "reviewer"
            if isinstance(extra.get("roof_material"), str) and extra["roof_material"].strip():
                record.roof_material = extra["roof_material"].strip().lower()
            if extra.get("ridge_height") is not None:
                try:
                    record.height = max(record.height or 0.0, float(extra["ridge_height"]))
                except (TypeError, ValueError):
                    pass
        record.boundary_source = "crm"
        if trees is not None:
            (
                record.canopy_over_roof_m2,
                record.canopy_over_roof_share,
                record.overlapping_trees,
                record.isolation,
            ) = record_canopy_metrics(record.ring_meters, record.centroid, trees)
        record.quality_score, record.quality_grade, record.quality_flags = record_quality(
            isolation=record.isolation,
            boundary_source="crm",
            distance_meters=record.distance_meters,
            area_m2=record.area_m2,
            eave_height=record.eave_height,
            kind=record.kind,
            overlapping_trees=record.overlapping_trees,
        )
        applied += 1
    return applied


def load_places(db_path: Path) -> list[dict]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = connection.execute("SELECT name, category, lat, lon FROM places").fetchall()
    connection.close()
    return [
        {"name": row[0], "category": row[1], "lat": row[2], "lon": row[3]}
        for row in rows
    ]


def _in_bbox(lon: float, lat: float, bbox: tuple[float, float, float, float]) -> bool:
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north


_SILHOUETTE_STATE: dict | None = None


def _silhouette_worker_init(zoom: int) -> None:
    global _SILHOUETTE_STATE
    from .ortho import OrthoCompositor

    _SILHOUETTE_STATE = {"zoom": zoom, "compositor": OrthoCompositor(zoom=zoom), "windows": {}}


def _silhouette_job(record: dict) -> tuple[int, dict | None]:
    import math as _math

    from .rooflines import Frame
    from .silhouette import snap_ring

    if _SILHOUETTE_STATE is None:
        return record["address_id"], None
    zoom = _SILHOUETTE_STATE["zoom"]
    compositor = _SILHOUETTE_STATE["compositor"]
    windows = _SILHOUETTE_STATE["windows"]
    centroid = record["centroid"]
    n = 2**zoom
    tile_x = int((centroid["lon"] + 180) / 360 * n)
    lat_rad = _math.radians(centroid["lat"])
    tile_y = int(
        (1 - _math.log(_math.tan(lat_rad) + 1 / _math.cos(lat_rad)) / _math.pi) / 2 * n
    )
    key = (tile_x, tile_y)
    if key not in windows:
        windows[key] = compositor.window(tile_x, tile_y)[0]
    window = windows[key]
    frame = Frame(record, window, zoom, tile_x, tile_y)
    try:
        result = snap_ring(record["ring_meters"], window, frame)
    except Exception as error:  # noqa: BLE001
        print(f"silhouette failed for {record['address_id']}: {error}", file=sys.stderr)
        return record["address_id"], None
    return record["address_id"], result


def apply_silhouette_snap(records, *, zoom: int = 18, workers: int = 6) -> int:
    """Snaps non-CRM rings onto image edges where alignment clearly improves."""
    import math as _math
    import os
    import time
    from concurrent.futures import ProcessPoolExecutor

    from .rooflines import Frame  # noqa: F401  (import check for worker parity)

    if not records:
        return 0
    worker_count = max(1, min(workers, len(records), os.cpu_count() or 4))
    started = time.time()
    results: dict[int, dict] = {}
    completed = 0
    with ProcessPoolExecutor(
        max_workers=worker_count,
        initializer=_silhouette_worker_init,
        initargs=(zoom,),
    ) as pool:
        for address_id, result in pool.map(
            _silhouette_job, [asdict(record) for record in records], chunksize=4
        ):
            completed += 1
            if result is not None:
                results[address_id] = result
            if completed % 100 == 0:
                print(
                    f"silhouette {completed}/{len(records)} ({time.time() - started:.0f}s)",
                    flush=True,
                )
    applied = 0
    accepted_iou: list[float] = []
    for record in records:
        if record.boundary_source == "crm":
            continue
        result = results.get(record.address_id)
        if not result or result.get("iou", 0.0) < 0.5:
            continue
        improvement = result.get("edge_score_after", 0.0) - result.get("edge_score_before", 0.0)
        if improvement <= 0.01:
            continue
        ring = result["ring"]
        if len(ring) < 3:
            continue
        record.ring_meters = ring
        polygon = Polygon([(x, y) for x, y in ring])
        record.area_m2 = round(polygon.area, 1)
        xs = [point[0] for point in ring]
        ys = [point[1] for point in ring]
        record.width_meters = round(max(xs) - min(xs), 1)
        record.depth_meters = round(max(ys) - min(ys), 1)
        point_lat = record.address_point["lat"]
        point_lon = record.address_point["lon"]
        record.distance_meters = round(
            _math.hypot(
                (record.centroid["lat"] - point_lat) * METERS_PER_DEGREE,
                (record.centroid["lon"] - point_lon)
                * METERS_PER_DEGREE
                * _math.cos(_math.radians(point_lat)),
            ),
            2,
        )
        record.quality_score, record.quality_grade, record.quality_flags = record_quality(
            isolation=record.isolation,
            boundary_source=record.boundary_source,
            distance_meters=record.distance_meters,
            area_m2=record.area_m2,
            eave_height=record.eave_height,
            kind=record.kind,
            overlapping_trees=record.overlapping_trees,
        )
        accepted_iou.append(float(result["iou"]))
        applied += 1
    mean_iou = sum(accepted_iou) / len(accepted_iou) if accepted_iou else 0.0
    print(
        f"silhouette snapped={applied}/{len(records)} mean_iou={mean_iou:.2f} "
        f"in {time.time() - started:.0f}s ({worker_count} workers)"
    )
    return applied


_WORKER_EXTRACTOR = None


def _roofline_worker_init(zoom: int) -> None:
    global _WORKER_EXTRACTOR
    from .rooflines import RooflineExtractor

    _WORKER_EXTRACTOR = RooflineExtractor(zoom=zoom)


def _roofline_payload(result: dict) -> dict:
    return {
        "pipeline_version": result.get("pipeline_version"),
        "geometry_source": result.get("geometry_source"),
        "height_source": result.get("height_source"),
        "structure_audit": result.get("structure_audit"),
        "image_structure_audit": result.get("image_structure_audit"),
        "prior_error": result.get("prior_error"),
        "eave_regularization": result.get("eave_regularization"),
        "roof_form_selection": result.get("roof_form_selection"),
        "footprint_coords": result.get("footprint_coords"),
        "footprint_area_m2": result.get("footprint_area_m2"),
        "parts": [
            {
                "ring": part["ring_meters"],
                "z": part["ring_z"],
                "area_m2": part["area_m2"],
                "slope_deg": part["slope_deg"],
                "pitch_deg": part.get("pitch_deg", part.get("slope_deg")),
                "pitch_ratio": part.get("pitch_ratio"),
                "azimuth_deg": part.get("azimuth_deg"),
                "area_3d_m2": part.get("area_3d_m2"),
                "kind": part.get("kind"),
                "z_range": part.get("z_range"),
            }
            for part in result["parts"]
        ],
        "lines": [
            {
                "kind": line.get("kind", "ridge"),
                "line_meters": line["line_meters"],
                "line_z": line.get("line_z"),
                "length_2d_m": line.get("length_2d_m"),
                "length_3d_m": line.get("length_3d_m"),
                "slope_deg": line.get("slope_deg"),
            }
            if isinstance(line, dict)
            else line
            for line in result["rooflines"]
        ],
    }


def _roofline_job(record: dict) -> tuple[int, dict | None]:
    if _WORKER_EXTRACTOR is None:
        return record["address_id"], None
    try:
        result = _WORKER_EXTRACTOR.extract(record)
    except Exception as error:  # noqa: BLE001
        print(f"rooflines failed for {record['address_id']}: {error}", file=sys.stderr)
        return record["address_id"], None
    if not result["parts"]:
        return record["address_id"], None
    return record["address_id"], _roofline_payload(result)


def apply_rooflines(records, *, zoom: int = 18, workers: int = 6) -> int:
    """Attaches per-record roofline partitions (TU Delft port) in worker processes."""
    import os
    import time
    from concurrent.futures import ProcessPoolExecutor

    from .review_feedback import boundary_signature, latest_feedback, rejected_cuts_for

    if not records:
        return 0
    worker_count = max(1, min(workers, len(records), os.cpu_count() or 4))
    started = time.time()
    payloads: dict[int, dict] = {}
    # Unit addresses often share exactly the same roof. Reconstruct it once,
    # but never reuse a model across different geometry or reviewer cuts.
    feedback = latest_feedback()
    unique, representatives, keys = [], {}, {}
    for record in records:
        item = asdict(record)
        key = json.dumps([boundary_signature(item), item.get("boundary_source"), item.get("roof_form_source"), item.get("eave_height"), item.get("ridge_height"),
                          item.get("roof_type"), rejected_cuts_for(item, feedback.get(record.address_id, {}))])
        if key not in keys:
            keys[key] = record.address_id
            unique.append(item)
        representatives[record.address_id] = keys[key]
    completed = 0
    with ProcessPoolExecutor(
        max_workers=worker_count,
        initializer=_roofline_worker_init,
        initargs=(zoom,),
    ) as pool:
        for address_id, payload in pool.map(
            _roofline_job, unique, chunksize=4
        ):
            completed += 1
            if payload is not None:
                payloads[address_id] = payload
            if completed % 50 == 0:
                print(
                    f"rooflines {completed}/{len(unique)} unique roofs "
                    f"({time.time() - started:.0f}s)",
                    flush=True,
                )
    applied = 0
    for record in records:
        payload = payloads.get(representatives[record.address_id])
        if payload:
            record.rooflines = payload
            coords = payload.get("footprint_coords")
            if coords and len(coords) >= 3:
                record.ring_meters = coords
                xs = [x for x, _ in coords]
                ys = [y for _, y in coords]
                record.width_meters = round(max(xs) - min(xs), 1)
                record.depth_meters = round(max(ys) - min(ys), 1)
            area = payload.get("footprint_area_m2")
            if area and area > 0:
                record.area_m2 = round(area, 1)
            applied += 1
    print(
        f"rooflines applied={applied}/{len(records)} "
        f"in {time.time() - started:.0f}s ({worker_count} workers)"
    )
    return applied


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tiles", type=Path, required=True, help="MBTiles building archive")
    parser.add_argument("--db", type=Path, required=True, help="Geo service SQLite database")
    parser.add_argument("--out", type=Path, required=True, help="Output file")
    parser.add_argument(
        "--bbox",
        type=str,
        default=None,
        help="west,south,east,north filter applied to addresses",
    )
    parser.add_argument("--format", choices=("jsonl", "json"), default="jsonl")
    parser.add_argument("--max-distance-m", type=float, default=12.0)
    parser.add_argument("--category-radius-m", type=float, default=12.0)
    parser.add_argument(
        "--places",
        action="store_true",
        help="Use nearest POI categories (only when the places feed is trusted)",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--include-suppressed",
        action="store_true",
        help="keep CRM-suppressed records (default: excluded as false positives)",
    )
    parser.add_argument(
        "--apply-crm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="replace record geometry with CRM roof corrections (default: on)",
    )
    parser.add_argument(
        "--silhouette-snap",
        action="store_true",
        help="snap non-CRM rings onto image edges before rooflines (opt-in)",
    )
    parser.add_argument(
        "--rooflines",
        action="store_true",
        help="attach TU Delft roofline partitions per record (slow: one imagery+depth pass per address)",
    )
    parser.add_argument("--rooflines-zoom", type=int, default=18)
    parser.add_argument(
        "--rooflines-workers",
        type=int,
        default=6,
        help="worker processes for the roofline pass (each loads its own depth model)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bbox = None
    if args.bbox:
        parts = [float(part) for part in args.bbox.split(",")]
        if len(parts) != 4:
            raise SystemExit("--bbox expects west,south,east,north")
        bbox = (parts[0], parts[1], parts[2], parts[3])

    buildings = load_buildings(args.tiles)
    addresses = load_addresses(args.db)
    if bbox:
        addresses = [
            address for address in addresses if _in_bbox(address["lon"], address["lat"], bbox)
        ]
    if args.limit:
        addresses = addresses[: args.limit]
    places = load_places(args.db) if args.places else []
    trees = load_trees(args.tiles)

    records = associate_addresses(
        buildings,
        addresses,
        places,
        max_distance_m=args.max_distance_m,
        category_radius_m=args.category_radius_m,
        trees=trees,
    )

    suppressed_ids: set[int] = set()
    if not args.include_suppressed:
        suppressed_ids = load_suppressed_addresses(args.db)
    inherited = inherit_unit_rings(records, addresses, suppressed=suppressed_ids)
    if inherited:
        print(f"unit-ring inheritances={inherited}")

    applied = apply_crm_roof_overrides(records, args.db, trees) if args.apply_crm else 0
    if applied:
        print(f"crm roof overrides applied={applied}")

    suppressed = 0
    if suppressed_ids:
        before = len(records)
        records = [
            record for record in records if record.address_id not in suppressed_ids
        ]
        suppressed = before - len(records)
        print(f"crm suppressed records excluded={suppressed}")

    if args.silhouette_snap:
        apply_silhouette_snap(
            records, zoom=args.rooflines_zoom, workers=args.rooflines_workers
        )

    shared = mark_shared_rings(records)
    if shared:
        print(f"records sharing a ring with another address={shared}")

    if args.rooflines:
        apply_rooflines(
            records, zoom=args.rooflines_zoom, workers=args.rooflines_workers
        )

    payload = [json.dumps(asdict(record)) for record in records]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        args.out.write_text("[\n" + ",\n".join(payload) + "\n]\n")
    else:
        args.out.write_text("\n".join(payload) + ("\n" if payload else ""))

    kinds: dict[str, int] = {}
    for record in records:
        kinds[record.kind] = kinds.get(record.kind, 0) + 1
    matched = len(records)
    print(
        f"addresses={len(addresses)} matched={matched} "
        f"buildings={len(buildings)} kinds={kinds}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
