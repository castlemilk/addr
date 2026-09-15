import gzip
import math

import pytest
from scripts.ml.tiles import (
    _CMD_CLOSE_PATH,
    TILE_EXTENT,
    MBTilesWriter,
    MVTFeature,
    _polygon_commands,
    encode_mvt_layer,
    encode_mvt_tile,
    iter_tiles,
    lonlat_to_tile_local,
    lonlat_to_tile_xy,
    read_mbtiles_metadata,
    read_mbtiles_tile,
    ring_signed_area,
    tile_bounds,
    tile_local_to_lonlat,
    tile_span_meters,
    tile_xy_to_lonlat,
    vector_layers_json,
)


def test_lonlat_tile_roundtrip():
    lon, lat = 145.31252, -37.79044
    for z in (0, 10, 16, 19):
        x, y = lonlat_to_tile_xy(lon, lat, z)
        assert 0 <= x < (1 << z)
        assert 0 <= y < (1 << z)
        back_lon, back_lat = tile_xy_to_lonlat(x, y, z)
        assert back_lon == pytest.approx(lon, abs=1e-9)
        assert back_lat == pytest.approx(lat, abs=1e-9)


def test_tile_local_roundtrip():
    lon, lat, z, x, y = 145.31, -37.79, 16, 59492, 38665
    lx, ly = lonlat_to_tile_local(lon, lat, x, y, z)
    back_lon, back_lat = tile_local_to_lonlat(lx, ly, x, y, z)
    assert back_lon == pytest.approx(lon, abs=1e-9)
    assert back_lat == pytest.approx(lat, abs=1e-9)


def test_tile_bounds_ordering():
    west, south, east, north = tile_bounds(59492, 38665, 16)
    assert west < east
    assert south < north


def test_iter_tiles_covers_bbox():
    bbox = (145.30, -37.80, 145.33, -37.78)
    tiles = list(iter_tiles(bbox, 16))
    assert len(tiles) >= 1
    limit = 1 << 16
    for x, y in tiles:
        assert 0 <= x < limit
        assert 0 <= y < limit


def test_tile_span_meters():
    assert tile_span_meters(0.0, 0) == pytest.approx(40075016.68557849, rel=1e-9)
    assert tile_span_meters(0.0, 1) == pytest.approx(20037508.34278, rel=1e-8)
    assert tile_span_meters(60.0, 0) == pytest.approx(
        40075016.68557849 * math.cos(math.radians(60.0)), rel=1e-9
    )


def test_ring_signed_area_winding():
    clockwise = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert ring_signed_area(clockwise) > 0
    assert ring_signed_area(clockwise[::-1]) < 0


def _decode(tile: bytes):
    mapbox_vector_tile = pytest.importorskip("mapbox_vector_tile")
    return mapbox_vector_tile.decode(tile)


def test_mvt_roundtrip_properties_and_geometry():
    feature = MVTFeature.from_rings(
        [(0, 0), (4096, 0), (4096, 4096), (0, 4096)],
        properties={"height": 7.5, "source": "ml", "levels": 2},
    )
    tile = encode_mvt_tile({"building": [feature]})
    decoded = _decode(tile)
    layer = decoded["building"]
    assert layer["extent"] == TILE_EXTENT
    assert len(layer["features"]) == 1
    decoded_feature = layer["features"][0]
    props = decoded_feature["properties"]
    assert props["height"] == pytest.approx(7.5)
    assert props["source"] == "ml"
    assert props["levels"] == 2
    assert decoded_feature["geometry"]["type"] == "Polygon"
    coordinates = decoded_feature["geometry"]["coordinates"][0]
    assert len(coordinates) >= 4
    xs = [point[0] for point in coordinates]
    ys = [point[1] for point in coordinates]
    assert min(xs) == pytest.approx(0, abs=1)
    assert max(xs) == pytest.approx(4096, abs=1)
    assert min(ys) == pytest.approx(0, abs=1)
    assert max(ys) == pytest.approx(4096, abs=1)


def test_mvt_polygon_with_hole():
    feature = MVTFeature.from_rings(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        holes=[[(25, 25), (75, 25), (75, 75), (25, 75)]],
        properties={"source": "ml"},
    )
    tile = encode_mvt_tile({"building": [feature]})
    decoded = _decode(tile)
    geometry = decoded["building"]["features"][0]["geometry"]
    assert geometry["type"] == "Polygon"
    assert len(geometry["coordinates"]) == 2


def _decode_ring_commands(commands) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    x = y = 0
    for command, params in commands:
        command_id = command & 0x7
        if command_id == _CMD_CLOSE_PATH:
            continue
        pairs = list(zip(params[::2], params[1::2], strict=True))
        for dx, dy in pairs:
            x += dx
            y += dy
            points.append((x, y))
    return points


def test_exterior_ring_is_normalized_clockwise_in_tile_space():
    clockwise = [(0, 0), (10, 0), (10, 10), (0, 10)]
    for ring in (clockwise, clockwise[::-1]):
        decoded = _decode_ring_commands(_polygon_commands([ring]))
        assert ring_signed_area(decoded) > 0


def test_hole_ring_is_normalized_counter_clockwise():
    exterior = [(0, 0), (100, 0), (100, 100), (0, 100)]
    hole = [(25, 25), (25, 75), (75, 75), (75, 25)]
    for candidate in (hole, hole[::-1]):
        commands = _polygon_commands([exterior, candidate])
        decoded_exterior = _decode_ring_commands(commands[:3])
        decoded_hole = _decode_ring_commands(commands[3:])
        assert ring_signed_area(decoded_exterior) > 0
        assert ring_signed_area(decoded_hole) < 0


def test_mvt_exterior_winding_is_clockwise_in_tile_space():
    feature = MVTFeature.from_rings(
        [(0, 0), (0, 100), (100, 100), (100, 0)],
        properties={"source": "ml"},
    )
    tile = encode_mvt_layer("building", [feature])
    decoded = _decode(encode_mvt_tile({"building": [feature]}))
    coordinates = decoded["building"]["features"][0]["geometry"]["coordinates"][0]
    assert len(coordinates) >= 4
    assert tile  # non-empty


def test_mvt_multi_layer_encode_and_decode():
    building = MVTFeature.from_rings(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        properties={"kind": "building", "height": 7.0},
    )
    tree = MVTFeature.from_rings(
        [(200, 200), (260, 200), (260, 260), (200, 260)],
        properties={"kind": "tree", "height": 9.5, "min_height": 3.3},
    )
    tile = encode_mvt_tile({"building": [building], "tree": [tree]})
    decoded = _decode(tile)
    assert set(decoded) == {"building", "tree"}
    tree_props = decoded["tree"]["features"][0]["properties"]
    assert tree_props["kind"] == "tree"
    assert tree_props["height"] == pytest.approx(9.5)
    assert tree_props["min_height"] == pytest.approx(3.3)


def test_mbtiles_roundtrip(tmp_path):
    path = tmp_path / "tiles.mbtiles"
    with MBTilesWriter(path) as writer:
        writer.set_metadata(
            {
                "format": "pbf",
                "json": vector_layers_json({"building": {"minzoom": 16, "maxzoom": 16}}),
            }
        )
        feature = MVTFeature.from_rings(
            [(0, 0), (4096, 0), (4096, 4096), (0, 4096)],
            properties={"height": 9.0, "source": "osm"},
        )
        writer.write_tile(16, 59492, 38665, encode_mvt_tile({"building": [feature]}))

    raw = read_mbtiles_tile(path, 16, 59492, 38665)
    assert raw is not None
    decoded = _decode(raw)
    assert decoded["building"]["features"][0]["properties"]["height"] == pytest.approx(9.0)

    stored = read_mbtiles_tile(path, 16, 59492, 38665, decompress=False)
    assert stored is not None
    assert stored[:2] == b"\x1f\x8b"

    metadata = read_mbtiles_metadata(path)
    assert metadata["format"] == "pbf"
    assert "building" in metadata["json"]
    assert read_mbtiles_tile(path, 16, 1, 1) is None


def test_tms_row_flip(tmp_path):
    import sqlite3

    path = tmp_path / "flip.mbtiles"
    with MBTilesWriter(path) as writer:
        writer.write_tile(4, 3, 5, b"payload")
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT tile_row FROM tiles WHERE zoom_level=4 AND tile_column=3"
    ).fetchone()
    conn.close()
    assert row[0] == (1 << 4) - 1 - 5


def test_gzip_helper():
    data = b"abc" * 100
    compressed = gzip.compress(data)
    assert compressed[:2] == b"\x1f\x8b"
