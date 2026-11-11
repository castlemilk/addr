"""Applies global (cross-tile) canopy/building conflict resolution to an archive.

The per-tile pipeline carve cannot see conflicts split across tile boundaries,
so canopies swallowed by building detections survive. This tool decodes the
whole archive, resolves conflicts globally (subtracts canopies that are >=60%
inside a building, never dropping a building below the keep ratio), and rewrites
tiles with properties and untouched geometries preserved.

Usage:
    python -m scripts.ml.refine_conflicts --in data/australia_tiles.mbtiles \
        --out data/australia_tiles_refined.mbtiles
"""

from __future__ import annotations

import argparse
import gzip
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

import mapbox_vector_tile
from shapely.geometry import Polygon, box
from shapely.strtree import STRtree

from .tiles import (
    MVTFeature,
    encode_mvt_tile,
    tile_bounds,
    tile_local_to_lonlat,
    wgs84_to_mvt_local,
)

CONTAINMENT = 0.6
MIN_TREE_AREA_M2 = 15.0
MIN_KEEP_RATIO = 0.25
MVT_BUFFER = 32


def _polygon_parts(geometry: dict) -> list[list]:
    if geometry.get("type") == "Polygon":
        return [geometry.get("coordinates") or []]
    return geometry.get("coordinates") or []


def _make_polygon(rings: list) -> Polygon | None:
    try:
        polygon = Polygon(rings[0], rings[1:])
    except (ValueError, IndexError):
        return None
    if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
        return None
    return polygon


def _decode(data: bytes) -> dict:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})


def refine(
    source: Path,
    destination: Path,
    *,
    containment: float = CONTAINMENT,
    min_tree_area_m2: float = MIN_TREE_AREA_M2,
    min_keep_ratio: float = MIN_KEEP_RATIO,
) -> dict:
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
    ).fetchall()
    metadata = connection.execute("SELECT name, value FROM metadata").fetchall()
    connection.close()

    tiles: dict[tuple[int, int, int], dict] = {}
    buildings: list[dict] = []
    trees: list[dict] = []

    for zoom, column, row, data in rows:
        layers = _decode(data)
        xyz_y = (1 << zoom) - 1 - row
        tile = {"zoom": zoom, "x": column, "y": xyz_y, "layers": {}}
        for name, layer in layers.items():
            items = []
            for feature in layer.get("features", []):
                properties = feature.get("properties", {})
                for part in _polygon_parts(feature.get("geometry") or {}):
                    local = _make_polygon(part)
                    if local is None:
                        continue
                    world = Polygon(
                        [
                            tile_local_to_lonlat(px, py, column, xyz_y, zoom)
                            for px, py in part[0]
                        ],
                        [
                            [
                                tile_local_to_lonlat(px, py, column, xyz_y, zoom)
                                for px, py in ring
                            ]
                            for ring in part[1:]
                        ],
                    )
                    item = {
                        "rings": part,
                        "properties": properties,
                        "world": world,
                        "refined": None,
                    }
                    items.append(item)
                    if name == "building":
                        buildings.append(item)
                    elif name == "tree":
                        trees.append(item)
            tile["layers"][name] = items
        tiles[(zoom, column, row)] = tile

    tree_polys = [tree["world"] for tree in trees]
    building_polys = [building["world"] for building in buildings]
    tree_index = STRtree(tree_polys) if tree_polys else None
    building_index = STRtree(building_polys) if building_polys else None

    contained_trees = 0
    for tree, tree_poly in zip(trees, tree_polys, strict=True):
        if float(tree["properties"].get("area_m2") or 0.0) < min_tree_area_m2:
            continue
        if tree_index is None or building_index is None:
            continue
        best = 0.0
        for candidate_index in building_index.query(tree_poly):
            candidate = building_polys[int(candidate_index)]
            if not candidate.intersects(tree_poly):
                continue
            intersection = candidate.intersection(tree_poly).area
            best = max(best, intersection / tree_poly.area)
        if best >= containment:
            contained_trees += 1

    carved = 0
    reverted = 0
    for building in buildings:
        geometry = building["world"]
        original_area = geometry.area
        changed = False
        if tree_index is not None:
            for tree_index_value in tree_index.query(geometry):
                tree = trees[int(tree_index_value)]
                if float(tree["properties"].get("area_m2") or 0.0) < min_tree_area_m2:
                    continue
                tree_poly = tree["world"]
                if not geometry.intersects(tree_poly):
                    continue
                intersection = geometry.intersection(tree_poly).area
                if tree_poly.area <= 0 or intersection / tree_poly.area < containment:
                    continue
                geometry = geometry.difference(tree_poly)
                changed = True
        if not changed:
            continue
        if geometry.is_empty:
            reverted += 1
            continue
        if geometry.geom_type == "MultiPolygon":
            geometry = max(geometry.geoms, key=lambda part: part.area)
        if geometry.area < original_area * min_keep_ratio:
            reverted += 1
            continue
        building["refined"] = geometry
        carved += 1

    output = sqlite3.connect(destination)
    output.executescript(
        """
        DROP TABLE IF EXISTS tiles;
        DROP TABLE IF EXISTS metadata;
        CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
        CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
        CREATE TABLE metadata (name TEXT, value TEXT);
        """
    )
    output.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)", metadata)

    for (zoom, column, row), tile in tiles.items():
        xyz_y = tile["y"]
        west, south, east, north = tile_bounds(column, xyz_y, zoom)
        pad = max((east - west) * MVT_BUFFER / 4096.0, 1e-6)
        clip = box(west - pad, south - pad, east + pad, north + pad)
        layers_out: dict[str, list[MVTFeature]] = {}
        for name, items in tile["layers"].items():
            encoded = []
            for item in items:
                properties = item["properties"]
                refined = item["refined"]
                if refined is not None:
                    clipped = refined.intersection(clip)
                    parts = (
                        [clipped]
                        if clipped.geom_type == "Polygon"
                        else list(getattr(clipped, "geoms", []))
                    )
                    if not parts:
                        continue
                    part = max(parts, key=lambda candidate: candidate.area)
                else:
                    part = item["world"]
                exterior = [
                    wgs84_to_mvt_local(lon, lat, column, xyz_y, zoom)
                    for lon, lat in part.exterior.coords
                ]
                holes = [
                    [
                        wgs84_to_mvt_local(lon, lat, column, xyz_y, zoom)
                        for lon, lat in interior.coords
                    ]
                    for interior in part.interiors
                ]
                encoded.append(
                    MVTFeature.from_rings(
                        [(int(round(px)), int(round(py))) for px, py in exterior],
                        [
                            [(int(round(px)), int(round(py))) for px, py in ring]
                            for ring in holes
                        ],
                        properties=properties,
                    )
                )
            layers_out[name] = encoded
        output.execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
            (zoom, column, row, encode_mvt_tile(layers_out)),
        )
    output.commit()
    output.close()
    return {
        "buildings": len(buildings),
        "trees": len(trees),
        "contained_trees": contained_trees,
        "carved_buildings": carved,
        "reverted": reverted,
    }


def _verify_roundtrip(source: Path) -> float:
    """Re-encodes a decoded building and reports the max coordinate delta."""
    import gzip as _gzip

    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    row = connection.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles LIMIT 1"
    ).fetchone()
    connection.close()
    zoom, column, row, data = row
    if data[:2] == b"\x1f\x8b":
        data = _gzip.decompress(data)
    layers = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})
    xyz_y = (1 << zoom) - 1 - row
    feature = layers["building"]["features"][0]
    coordinates = feature["geometry"]["coordinates"]
    polygons = [coordinates] if feature["geometry"]["type"] == "Polygon" else coordinates
    ring = polygons[0][0]
    encoded = []
    for px, py in ring:
        lon, lat = tile_local_to_lonlat(px, py, column, xyz_y, zoom)
        ex, ey = wgs84_to_mvt_local(lon, lat, column, xyz_y, zoom)
        encoded.append((int(round(ex)), int(round(ey))))
    blob = encode_mvt_tile(
        {"building": [MVTFeature.from_rings(encoded, properties={})]}
    )
    decoded_feature = mapbox_vector_tile.decode(blob, default_options={"y_coord_down": True})["building"]["features"][0]
    decoded_coords = decoded_feature["geometry"]["coordinates"]
    decoded_polygons = (
        [decoded_coords]
        if decoded_feature["geometry"]["type"] == "Polygon"
        else decoded_coords
    )
    decoded_ring = decoded_polygons[0][0]
    # Winding and the starting vertex can change during MVT encoding.
    return Polygon(ring).hausdorff_distance(Polygon(decoded_ring))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="source", type=Path, required=True)
    parser.add_argument("--out", dest="destination", type=Path, required=True)
    parser.add_argument("--containment", type=float, default=CONTAINMENT)
    parser.add_argument("--min-tree-area", type=float, default=MIN_TREE_AREA_M2)
    parser.add_argument("--min-keep-ratio", type=float, default=MIN_KEEP_RATIO)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = refine(
        args.source,
        args.destination,
        containment=args.containment,
        min_tree_area_m2=args.min_tree_area,
        min_keep_ratio=args.min_keep_ratio,
    )
    drift = _verify_roundtrip(args.source)
    print(f"roundtrip max coordinate delta: {drift:.3f} local units")
    if drift > 1.0:
        print("ERROR: re-encoding round-trip drift exceeds tolerance", file=sys.stderr)
        return 2
    print(
        f"buildings={report['buildings']} trees={report['trees']} "
        f"contained_trees={report['contained_trees']} carved={report['carved_buildings']} "
        f"reverted={report['reverted']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
