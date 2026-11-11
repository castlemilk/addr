import math
import sqlite3
from pathlib import Path

from scripts.ml import tile_index


def test_tile_bounds_are_wgs84():
    west, south, east, north = tile_index.tile_wsen(0, 0, 0)
    assert west == -180 and east == 180
    assert math.isclose(south, -85.0511287798066) and math.isclose(north, 85.0511287798066)
    west, south, east, north = tile_index.tile_wsen(1, 0, 0)
    assert west == -180 and east == 0 and south == 0


def test_index_and_bbox_query(tmp_path):
    db = tmp_path / "tiles.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE tiles (z INTEGER, x INTEGER, y INTEGER, path TEXT, bytes INTEGER, sha256 TEXT,
                            last_modified TEXT, etag TEXT, fetched_at TEXT,
                            PRIMARY KEY (z,x,y));
        INSERT INTO tiles VALUES (20, 947539, 643305, 'a.png', 120, 'x', NULL, NULL, 'now');
        INSERT INTO tiles VALUES (20, 947539, 643306, 'b.png', 121, 'y', NULL, NULL, 'now');
    """)
    conn.commit()
    conn.close()
    built = tile_index.build(db, "test", "layer", "CC0")
    rows = tile_index.query(db, (145.311, -37.783, 145.313, -37.781))
    assert len(rows) >= 1
    assert {r[3] for r in rows} & {"a.png", "b.png"}
    meta = built.execute("SELECT tiles,zooms FROM archive_meta").fetchone()
    assert meta[0] == 2 and meta[1] == "[20]"
    built.close()
