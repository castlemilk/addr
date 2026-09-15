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
from dataclasses import asdict, dataclass
from pathlib import Path

import mapbox_vector_tile
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

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


def associate_addresses(
    buildings: list[tuple[Polygon, dict, float]],
    addresses: list[dict],
    places: list[dict] | None = None,
    max_distance_m: float = 20.0,
    category_radius_m: float = 12.0,
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
            )
        )
    return records


def load_buildings(tiles_path: Path) -> list[tuple[Polygon, dict, float]]:
    """Decodes the building layer from an MBTiles archive into WGS84 polygons."""
    connection = sqlite3.connect(f"file:{tiles_path}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
    ).fetchall()
    connection.close()

    buildings: list[tuple[Polygon, dict, float]] = []
    seen: set[tuple[float, float]] = set()
    for zoom, column, row, data in rows:
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        layers = mapbox_vector_tile.decode(data)
        layer = layers.get("building")
        if not layer:
            continue
        tile_row_tms = row
        y = (1 << zoom) - 1 - tile_row_tms
        west, south, east, north = tile_bounds(column, y, zoom)
        span = east - west
        meters_per_unit = span * METERS_PER_DEGREE / 4096.0
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
                            tile_local_to_lonlat(x, yy, column, y, zoom)
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
                buildings.append((polygon, feature["properties"], meters_per_unit))
    return buildings


def load_addresses(db_path: Path) -> list[dict]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT id, unit, full_address, lat, lon FROM addresses"
    ).fetchall()
    connection.close()
    return [
        {"id": row[0], "unit": row[1], "full_address": row[2], "lat": row[3], "lon": row[4]}
        for row in rows
    ]


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

    records = associate_addresses(
        buildings,
        addresses,
        places,
        max_distance_m=args.max_distance_m,
        category_radius_m=args.category_radius_m,
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
