"""Slippy map math, Mapbox Vector Tile encoding, and MBTiles storage.

This module is intentionally free of heavy dependencies (stdlib only) so tile
encoding can be unit tested without onnxruntime, numpy, shapely, or PIL.
"""

from __future__ import annotations

import gzip
import math
import sqlite3
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

TILE_EXTENT = 4096
MVT_BUFFER = 64
WORLD_SPAN_M = 40075016.68557849
MAX_LATITUDE = 85.05112878

_CMD_MOVE_TO = 1
_CMD_LINE_TO = 2
_CMD_CLOSE_PATH = 7
_GEOM_TYPE_POLYGON = 3


def lonlat_to_tile_xy(lon: float, lat: float, z: int) -> tuple[float, float]:
    """Fractional Web Mercator tile coordinate for a lon/lat at zoom z."""
    n = float(1 << z)
    lat = max(min(lat, MAX_LATITUDE), -MAX_LATITUDE)
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def tile_xy_to_lonlat(x: float, y: float, z: int) -> tuple[float, float]:
    """Inverse of :func:`lonlat_to_tile_xy` for fractional tile coordinates."""
    n = float(1 << z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def tile_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """Returns (west, south, east, north) bounds for an integer tile."""
    west, north = tile_xy_to_lonlat(x, y, z)
    east, south = tile_xy_to_lonlat(x + 1, y + 1, z)
    return west, south, east, north


def iter_tiles(bbox: tuple[float, float, float, float], z: int):
    """Yields integer (x, y) tiles intersecting a (min_lon, min_lat, max_lon, max_lat) bbox."""
    min_lon, min_lat, max_lon, max_lat = bbox
    n = 1 << z
    x0, y0 = lonlat_to_tile_xy(min_lon, max_lat, z)
    x1, y1 = lonlat_to_tile_xy(max_lon, min_lat, z)
    xa, xb = max(0, int(math.floor(x0))), min(n - 1, int(math.floor(x1)))
    ya, yb = max(0, int(math.floor(y0))), min(n - 1, int(math.floor(y1)))
    for x in range(xa, xb + 1):
        for y in range(ya, yb + 1):
            yield x, y


def lonlat_to_tile_local(
    lon: float, lat: float, x: int, y: int, z: int, extent: int = TILE_EXTENT
) -> tuple[float, float]:
    """Projects lon/lat into tile-local MVT coordinates (y grows southward)."""
    fx, fy = lonlat_to_tile_xy(lon, lat, z)
    return (fx - x) * extent, (fy - y) * extent


def tile_local_to_lonlat(
    lx: float, ly: float, x: int, y: int, z: int, extent: int = TILE_EXTENT
) -> tuple[float, float]:
    """Inverse of :func:`lonlat_to_tile_local`."""
    return tile_xy_to_lonlat(x + lx / extent, y + ly / extent, z)


def tile_span_meters(lat: float, z: int) -> float:
    """Ground width of one tile in meters at the given latitude and zoom."""
    return WORLD_SPAN_M * math.cos(math.radians(lat)) / (1 << z)


def meters_per_extent_unit(lat: float, z: int, extent: int = TILE_EXTENT) -> float:
    return tile_span_meters(lat, z) / extent


def gzip_compress(data: bytes, level: int = 6) -> bytes:
    return gzip.compress(data, compresslevel=level)


def ring_signed_area(ring: Sequence[tuple[float, float]]) -> float:
    """Shoelace area. In y-down tile space a positive area is clockwise on screen."""
    total = 0.0
    if len(ring) < 3:
        return 0.0
    prev = ring[-1]
    for point in ring:
        total += prev[0] * point[1] - point[0] * prev[1]
        prev = point
    return total / 2.0


def _clean_ring(ring: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    cleaned: list[tuple[int, int]] = []
    for point in ring:
        if not cleaned or point != cleaned[-1]:
            cleaned.append(point)
    if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    return cleaned


def _cmd(command_id: int, count: int) -> int:
    return (command_id & 0x7) | (count << 3)


def _varint(buf: bytearray, value: int) -> None:
    if value < 0:
        value &= (1 << 64) - 1
    while value > 0x7F:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value)


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 31)


def _tag(buf: bytearray, field_number: int, wire_type: int) -> None:
    _varint(buf, (field_number << 3) | wire_type)


def _bytes_field(buf: bytearray, field_number: int, payload: bytes) -> None:
    _tag(buf, field_number, 2)
    _varint(buf, len(payload))
    buf.extend(payload)


def _varint_field(buf: bytearray, field_number: int, value: int) -> None:
    _tag(buf, field_number, 0)
    _varint(buf, value)


def _encode_value(value) -> bytes:
    buf = bytearray()
    if isinstance(value, bool):
        _varint_field(buf, 7, 1 if value else 0)
    elif isinstance(value, int):
        _varint_field(buf, 4, value)
    elif isinstance(value, float):
        _tag(buf, 3, 1)
        buf.extend(struct.pack("<d", value))
    else:
        _bytes_field(buf, 1, str(value).encode("utf-8"))
    return bytes(buf)


def _value_key(value) -> str:
    return f"{type(value).__name__}:{value}"


@dataclass
class MVTFeature:
    """A polygon feature: one or more polygons, each a list of rings (exterior first)."""

    polygons: list[list[list[tuple[int, int]]]] = field(default_factory=list)
    properties: dict = field(default_factory=dict)
    feature_id: int | None = None

    @classmethod
    def from_rings(
        cls,
        exterior: list[tuple[int, int]],
        holes: list[list[tuple[int, int]]] | None = None,
        properties: dict | None = None,
        feature_id: int | None = None,
    ) -> MVTFeature:
        rings = [exterior] + list(holes or [])
        return cls(polygons=[rings], properties=dict(properties or {}), feature_id=feature_id)


def _polygon_commands(
    polygon: list[list[tuple[int, int]]],
) -> list[tuple[int, list[int]]]:
    commands: list[tuple[int, list[int]]] = []
    for index, raw_ring in enumerate(polygon):
        ring = _clean_ring(raw_ring)
        if len(ring) < 3:
            continue
        is_clockwise = ring_signed_area(ring) >= 0.0
        if is_clockwise != (index == 0):
            ring = ring[::-1]

        x0, y0 = ring[0]
        deltas: list[int] = []
        for (px, py), (qx, qy) in pairwise(ring):
            deltas.extend((qx - px, qy - py))
        commands.append((_cmd(_CMD_MOVE_TO, 1), [x0, y0]))
        commands.append((_cmd(_CMD_LINE_TO, len(ring) - 1), deltas))
        commands.append((_cmd(_CMD_CLOSE_PATH, 1), []))
    return commands


def _encode_geometry(
    polygons: list[list[list[tuple[int, int]]]],
) -> bytes:
    buf = bytearray()
    for polygon in polygons:
        for command, params in _polygon_commands(polygon):
            _varint(buf, command)
            for param in params:
                _varint(buf, _zigzag(param))
    return bytes(buf)


def encode_mvt_layer(
    name: str, features: list[MVTFeature], extent: int = TILE_EXTENT
) -> bytes:
    keys: dict[str, int] = {}
    key_list: list[str] = []
    values: dict[str, int] = {}
    value_list: list[bytes] = []
    encoded_features: list[bytes] = []

    for feature in features:
        tags: list[int] = []
        for key, value in feature.properties.items():
            if value is None:
                continue
            if key not in keys:
                keys[key] = len(key_list)
                key_list.append(key)
            value_key = _value_key(value)
            if value_key not in values:
                values[value_key] = len(value_list)
                value_list.append(_encode_value(value))
            tags.extend((keys[key], values[value_key]))

        feature_buf = bytearray()
        if feature.feature_id is not None:
            _varint_field(feature_buf, 1, feature.feature_id)
        if tags:
            packed = bytearray()
            for tag in tags:
                _varint(packed, tag)
            _bytes_field(feature_buf, 2, bytes(packed))
        _varint_field(feature_buf, 3, _GEOM_TYPE_POLYGON)
        _bytes_field(feature_buf, 4, _encode_geometry(feature.polygons))
        encoded_features.append(bytes(feature_buf))

    layer = bytearray()
    _varint_field(layer, 15, 2)
    _bytes_field(layer, 1, name.encode("utf-8"))
    for encoded in encoded_features:
        _bytes_field(layer, 2, encoded)
    for key in key_list:
        _bytes_field(layer, 3, key.encode("utf-8"))
    for value in value_list:
        _bytes_field(layer, 4, value)
    _varint_field(layer, 5, extent)
    return bytes(layer)


def encode_mvt_tile(
    layers: dict[str, list[MVTFeature]], extent: int = TILE_EXTENT
) -> bytes:
    tile = bytearray()
    for name, features in layers.items():
        if not features:
            continue
        _bytes_field(tile, 3, encode_mvt_layer(name, features, extent))
    return bytes(tile)


class MBTilesWriter:
    """Streams gzip-compressed MVT payloads into an MBTiles SQLite container."""

    def __init__(self, path: str | Path, overwrite: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if overwrite and self.path.exists():
            self.path.unlink()
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS tiles ("
            "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS tile_index "
            "ON tiles (zoom_level, tile_column, tile_row)"
        )
        self.tile_count = 0
        self._dirty = 0

    def set_metadata(self, metadata: dict[str, str]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in metadata.items()],
        )
        self.conn.commit()

    def write_tile(self, z: int, x: int, y: int, data: bytes, compress: bool = True) -> None:
        if not data:
            return
        if compress:
            data = gzip_compress(data)
        tms_row = (1 << z) - 1 - y
        self.conn.execute(
            "INSERT OR REPLACE INTO tiles "
            "(zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
            (z, x, tms_row, sqlite3.Binary(data)),
        )
        self.tile_count += 1
        self._dirty += 1
        if self._dirty >= 512:
            self.conn.commit()
            self._dirty = 0

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> MBTilesWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_mbtiles_tile(
    path: str | Path, z: int, x: int, y: int, decompress: bool = True
) -> bytes | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? "
            "AND tile_row = ?",
            (z, x, (1 << z) - 1 - y),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    data = bytes(row[0])
    if decompress and data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def read_mbtiles_metadata(path: str | Path) -> dict[str, str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name, value FROM metadata").fetchall()
    finally:
        conn.close()
    return {name: value for name, value in rows}


def vector_layers_json(layers: dict[str, dict]) -> str:
    import json

    return json.dumps(
        {"vector_layers": [{"id": name, **spec} for name, spec in layers.items()]},
        separators=(",", ":"),
    )


def clamp_tile_polygon(
    ring: Sequence[tuple[float, float]],
    extent: int = TILE_EXTENT,
    buffer: int = MVT_BUFFER,
) -> list[tuple[int, int]]:
    """Rounds a ring to integer tile-local coordinates without clipping."""
    return [(int(round(x)), int(round(y))) for x, y in ring]
