"""Add an R*Tree spatial index to an XYZ tile archive's tiles.db (tile-index-v1).

The archive's `tiles` table is keyed by (z,x,y), which is fast for an exact tile but
not for "all tiles intersecting this bbox". This adds a SQLite R*Tree over WGS84 tile
bounds plus an `archive_meta` row, so a quick-fetch query is:

    SELECT t.z,t.x,t.y,t.path
    FROM tile_index i JOIN tiles t ON t.rowid=i.id
    WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=?;

Usage:
    python -m scripts.ml.tile_index --db /Volumes/gamma-systems-2/addr-tiles/vicmap/tiles.db \
        --provider vicmap --layer AERIAL_WM_256 --licence "Vicmap Basemaps licence"
    python -m scripts.ml.tile_index --db ... --query 145.30,-37.80,145.32,-37.78
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def tile_wsen(z: int, x: int, y: int):
    n = 2**z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def build(db: Path, provider: str, layer: str, licence: str, source: str | None = None):
    conn = sqlite3.connect(db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS tile_index USING rtree(id, minx, maxx, miny, maxy);
        CREATE TABLE IF NOT EXISTS archive_meta (
            provider TEXT NOT NULL,
            layer TEXT NOT NULL,
            licence TEXT NOT NULL,
            source TEXT,
            tiles INTEGER NOT NULL,
            bytes INTEGER NOT NULL,
            bbox_wsen TEXT NOT NULL,
            zooms TEXT NOT NULL,
            built_at TEXT NOT NULL,
            PRIMARY KEY (provider, layer)
        );
    """)
    rows = conn.execute("SELECT rowid,z,x,y FROM tiles").fetchall()
    conn.execute("DELETE FROM tile_index")
    bounds = [tile_wsen(z, x, y) for _, z, x, y in rows]
    conn.executemany(
        "INSERT INTO tile_index (id,minx,maxx,miny,maxy) VALUES (?,?,?,?,?)",
        [(rowid, b[0], b[2], b[1], b[3]) for (rowid, _, _, _), b in zip(rows, bounds)],
    )
    total, nbytes = conn.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM tiles").fetchone()
    zooms = sorted({z for _, z, _, _ in rows})
    bbox = (
        min(b[0] for b in bounds), min(b[1] for b in bounds),
        max(b[2] for b in bounds), max(b[3] for b in bounds),
    )
    conn.execute(
        "INSERT OR REPLACE INTO archive_meta (provider,layer,licence,source,tiles,bytes,bbox_wsen,zooms,built_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (provider, layer, licence, source, total, nbytes, json.dumps([round(v, 6) for v in bbox]),
         json.dumps(zooms), datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    print(f"{db}: {total:,} tiles, {nbytes:,} bytes, zooms {zooms}, bbox {tuple(round(v, 4) for v in bbox)}")
    return conn


def query(db: Path, bbox):
    west, south, east, north = bbox
    conn = sqlite3.connect(db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    rows = conn.execute(
        "SELECT t.z,t.x,t.y,t.path FROM tile_index i JOIN tiles t ON t.rowid=i.id"
        " WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? ORDER BY t.z,t.x,t.y",
        (west, east, south, north),
    ).fetchall()
    print(json.dumps({"bbox": list(bbox), "tiles": len(rows),
                      "first": rows[:3], "zooms": sorted({r[0] for r in rows})}, indent=2))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--provider", default="unknown")
    parser.add_argument("--layer", default="unknown")
    parser.add_argument("--licence", default="unknown")
    parser.add_argument("--source", default=None)
    parser.add_argument("--query", type=lambda s: tuple(float(v) for v in s.split(",")), default=None,
                        help="W,S,E,N lon/lat")
    args = parser.parse_args(argv)
    if args.query:
        query(args.db, args.query)
        return 0
    build(args.db, args.provider, args.layer, args.licence, args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
