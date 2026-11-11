"""Vicmap Basemaps aerial tile archive (vicmap-tiles-v1).

Downloads the Vicmap Basemaps WMTS aerial layer (`AERIAL_WM_256`, EPSG:3857:256,
PNG, levels 0-20) for a lon/lat box into an XYZ tree, with a resumable SQLite
manifest that records the bytes, sha256, ETag and Last-Modified header of every
tile. The Last-Modified header is the only capture-date evidence the service
publishes per tile, so the summary reports its distribution rather than claiming
a single "capture date".

The service is licensed and billed by map-page impressions. The crawler refuses
to fetch unless VICMAP_BASEMAPS_LICENCE is set (any non-empty value, normally
the licence number). A dry run makes no requests and needs no licence.

Output layout under --out:

    tiles.db                       resumable manifest (tiles + crawls tables)
    latest.json                    summary of the most recent crawl
    crawls/crawl-<UTC>.json        one summary per crawl, never rewritten
    failures.jsonl                 one row per failed tile (appended)
    README.txt                     attribution and service terms
    AERIAL_WM_256/<z>/<x>/<y>.png  the tiles

The archive is a licensed basemap copy, not imagery evidence for a scene: keep
the attribution with any extract and do not redistribute it outside the licence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

LAYER = "AERIAL_WM_256"
TILE_MATRIX_SET = "EPSG:3857:256"
ENDPOINT = (
    "https://base.maps.vic.gov.au/service?SERVICE=WMTS&VERSION=1.0.0&REQUEST=GetTile"
    f"&LAYER={LAYER}&STYLE=default&TILEMATRIXSET={TILE_MATRIX_SET}"
    "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/png"
)
ATTRIBUTION = "© State of Victoria (Department of Transport and Planning), Vicmap Basemaps"
LICENCE_ENV = "VICMAP_BASEMAPS_LICENCE"
DEFAULT_BBOX = (145.298, -37.803, 145.354, -37.758)  # W,S,E,N: Mooroolbark scan
DEFAULT_ZOOMS = (18, 19, 20)
USER_AGENT = "addr-vicmap-archive/1.0 (licensed Vicmap Basemaps archive; local research)"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    z INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    last_modified TEXT,
    etag TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (z, x, y)
);
CREATE TABLE IF NOT EXISTS crawls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    bbox TEXT NOT NULL,
    zooms TEXT NOT NULL,
    expected INTEGER NOT NULL,
    fetched INTEGER NOT NULL DEFAULT 0,
    adopted INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    detail TEXT
);
"""


class FetchError(Exception):
    def __init__(self, status: int | None, message: str, *, retryable: bool):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class Tile:
    z: int
    x: int
    y: int

    @property
    def relpath(self) -> Path:
        return Path(LAYER) / str(self.z) / str(self.x) / f"{self.y}.png"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def tile_xy(lon: float, lat: float, z: int) -> tuple[float, float]:
    """Fractional Web Mercator tile coordinate (x, y)."""
    n = 2**z
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_ranges(bbox: tuple[float, float, float, float], zooms: tuple[int, ...]):
    """Inclusive (z, xmin, xmax, ymin, ymax) ranges covering the lon/lat box."""
    west, south, east, north = bbox
    if not (west < east and south < north):
        raise ValueError(f"bbox must be W,S,E,N with W<E and S<N: {bbox}")
    out = []
    for z in zooms:
        x0, y0 = tile_xy(west, north, z)
        x1, y1 = tile_xy(east, south, z)
        out.append((z, int(x0 // 1), int(x1 // 1), int(y0 // 1), int(y1 // 1)))
    return out


def iter_tiles(bbox, zooms):
    for z, xmin, xmax, ymin, ymax in tile_ranges(bbox, zooms):
        for x in range(xmin, xmax + 1):
            for y in range(ymin, ymax + 1):
                yield Tile(z, x, y)


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox needs four comma-separated numbers: W,S,E,N")
    return tuple(parts)  # type: ignore[return-value]


def parse_zooms(value: str) -> tuple[int, ...]:
    zooms = tuple(sorted({int(z) for z in value.split(",") if z.strip()}))
    if not zooms or zooms[0] < 0 or zooms[-1] > 20:
        raise argparse.ArgumentTypeError("zooms must be within 0-20 (Vicmap aerial tops out at 20)")
    return zooms


class Archive:
    def __init__(self, root: Path):
        self.root = root
        self.db_path = root / "tiles.db"
        root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    def close(self):
        self.conn.close()

    def tile_path(self, tile: Tile) -> Path:
        return self.root / tile.relpath

    def load_index(self) -> dict[tuple[int, int, int], sqlite3.Row]:
        """One read for the whole manifest: per-tile SELECTs over SMB are pathologically slow."""
        return {
            (row["z"], row["x"], row["y"]): row
            for row in self.conn.execute("SELECT * FROM tiles")
        }

    def adopt(self, tile: Tile) -> dict | None:
        """Register a tile already on disk without a manifest row; None if unusable."""
        path = self.tile_path(tile)
        try:
            if path.stat().st_size == 0:
                return None
            with path.open("rb") as handle:
                if handle.read(8) != PNG_MAGIC:
                    return None
        except OSError:
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        row = {
            "z": tile.z, "x": tile.x, "y": tile.y,
            "path": str(tile.relpath), "bytes": path.stat().st_size,
            "sha256": digest, "last_modified": None, "etag": None,
            "fetched_at": now(),
        }
        self.record(row)
        return row

    def record(self, row: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO tiles (z,x,y,path,bytes,sha256,last_modified,etag,fetched_at)"
            " VALUES (:z,:x,:y,:path,:bytes,:sha256,:last_modified,:etag,:fetched_at)",
            row,
        )

    def start_crawl(self, bbox, zooms, expected) -> int:
        cur = self.conn.execute(
            "INSERT INTO crawls (started_at,bbox,zooms,expected,status) VALUES (?,?,?,?,?)",
            (now(), json.dumps(bbox), json.dumps(zooms), expected, "running"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_crawl(self, crawl_id: int, **fields):
        self.conn.execute(
            "UPDATE crawls SET finished_at=?, fetched=?, adopted=?, skipped=?, failed=?,"
            " bytes=?, status=?, detail=? WHERE id=?",
            (
                now(), fields["fetched"], fields["adopted"], fields["skipped"],
                fields["failed"], fields["bytes"], fields["status"],
                fields.get("detail"), crawl_id,
            ),
        )
        self.conn.commit()

    def summary(self, crawl_id: int, bbox, zooms, licence_marker: str) -> dict:
        crawl = self.conn.execute("SELECT * FROM crawls WHERE id=?", (crawl_id,)).fetchone()
        per_zoom = []
        for z, xmin, xmax, ymin, ymax in tile_ranges(bbox, zooms):
            rows = self.conn.execute(
                "SELECT bytes,last_modified FROM tiles"
                " WHERE z=? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                (z, xmin, xmax, ymin, ymax),
            ).fetchall()
            dates = Counter(r["last_modified"] or "unknown" for r in rows)
            per_zoom.append({
                "z": z,
                "tiles": len(rows),
                "bytes": sum(r["bytes"] for r in rows),
                "last_modified": dict(sorted(dates.items())),
            })
        dates = [
            r["last_modified"] for r in self.conn.execute(
                "SELECT DISTINCT last_modified FROM tiles WHERE last_modified IS NOT NULL"
            ).fetchall()
        ]
        return {
            "format": "vicmap-tiles-v1",
            "provider": "Vicmap Basemaps",
            "layer": LAYER,
            "tile_matrix_set": TILE_MATRIX_SET,
            "attribution": ATTRIBUTION,
            "endpoint_template": ENDPOINT,
            "bbox_wsen": list(bbox),
            "zooms": list(zooms),
            "licence_env": LICENCE_ENV,
            "licence_marker": licence_marker,
            "crawl": dict(crawl) if crawl else None,
            "archive": {
                "tiles": sum(z["tiles"] for z in per_zoom),
                "bytes": sum(z["bytes"] for z in per_zoom),
                "per_zoom": per_zoom,
                "capture_last_modified_range": [min(dates), max(dates)] if dates else None,
            },
        }

    def write_summary(self, summary: dict) -> Path:
        root = self.root
        (root / "crawls").mkdir(exist_ok=True)
        started = summary["crawl"]["started_at"].replace(":", "").replace("-", "")
        dated = root / "crawls" / f"crawl-{started}.json"
        text = json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
        dated.write_text(text)
        (root / "latest.json").write_text(text)
        return dated


def fetch_tile(session: requests.Session, tile: Tile, root: Path, timeout: float) -> dict:
    url = ENDPOINT.format(z=tile.z, y=tile.y, x=tile.x)
    try:
        response = session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        raise FetchError(None, f"request failed: {exc}", retryable=True) from exc
    if response.status_code != 200:
        retryable = response.status_code in (429,) or response.status_code >= 500
        raise FetchError(
            response.status_code,
            f"HTTP {response.status_code}: {response.text[:120].strip()}",
            retryable=retryable,
        )
    body = response.content
    if not body.startswith(PNG_MAGIC):
        raise FetchError(
            response.status_code,
            f"not a PNG ({response.headers.get('Content-Type')}, {len(body)} bytes)",
            retryable=False,
        )
    path = root / tile.relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{tile.y}.{os.getpid()}.{threading.get_ident()}.part")
    part.write_bytes(body)
    os.replace(part, path)
    return {
        "z": tile.z, "x": tile.x, "y": tile.y,
        "path": str(tile.relpath),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "last_modified": response.headers.get("Last-Modified"),
        "etag": response.headers.get("ETag"),
        "fetched_at": now(),
    }


def download_with_retries(session, tile, root, timeout, retries, delay):
    last: FetchError | None = None
    for attempt in range(retries + 1):
        try:
            if delay:
                time.sleep(delay)
            return fetch_tile(session, tile, root, timeout)
        except FetchError as exc:
            last = exc
            if not exc.retryable or attempt == retries:
                raise
            time.sleep(min(2**attempt, 30))
    assert last is not None
    raise last


def write_readme(root: Path):
    (root / "README.txt").write_text(
        "Vicmap Basemaps aerial tile archive\n"
        "==================================\n\n"
        f"Layer: {LAYER} ({TILE_MATRIX_SET}, PNG, Web Mercator, levels 0-20)\n"
        f"Attribution: {ATTRIBUTION}\n\n"
        "Vicmap Basemaps is a licensed service billed by map-page impressions. This\n"
        "archive may be used only within the terms of the licence recorded in\n"
        f"{LICENCE_ENV}. Tiles are not imagery evidence for a scene and must not be\n"
        "redistributed outside the licence.\n\n"
        "tiles.db records sha256, bytes, ETag and Last-Modified for every tile;\n"
        "latest.json summarises the most recent crawl and the per-zoom capture dates.\n"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bbox", type=parse_bbox, default=DEFAULT_BBOX,
                        help="W,S,E,N lon/lat (default: Mooroolbark scan)")
    parser.add_argument("--zooms", type=parse_zooms, default=DEFAULT_ZOOMS,
                        help="comma-separated zoom levels (default: 18,19,20; max 20)")
    parser.add_argument("--out", type=Path, required=True, help="archive root (e.g. /Volumes/gamma-systems-2/addr-tiles/vicmap)")
    parser.add_argument("--workers", type=int, default=6, help="parallel requests (default: 6)")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout seconds")
    parser.add_argument("--retries", type=int, default=4, help="retries per retryable failure")
    parser.add_argument("--delay", type=float, default=0.05, help="seconds each worker sleeps before a request")
    parser.add_argument("--max-consecutive-failures", type=int, default=25,
                        help="abort when this many failures happen without a success")
    parser.add_argument("--limit", type=int, default=0, help="fetch at most N tiles (smoke test)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and make no requests")
    parser.add_argument("--force", action="store_true", help="re-fetch tiles already in the manifest")
    parser.add_argument("--verify", action="store_true", help="check manifest/file agreement and exit")
    args = parser.parse_args(argv)

    bbox = tuple(args.bbox)
    zooms = tuple(args.zooms)
    ranges = tile_ranges(bbox, zooms)
    counts = {z: (xmax - xmin + 1) * (ymax - ymin + 1) for z, xmin, xmax, ymin, ymax in ranges}
    expected = sum(counts.values())
    tiles = list(iter_tiles(bbox, zooms))
    assert len(tiles) == expected

    print(f"plan: {expected:,} tiles for bbox {bbox} zooms {zooms} -> {args.out}")
    for z, xmin, xmax, ymin, ymax in ranges:
        print(f"  z{z}: {counts[z]:,} tiles  x {xmin}-{xmax}  y {ymin}-{ymax}")
    if args.dry_run:
        return 0

    archive = Archive(args.out)
    write_readme(args.out)

    if args.verify:
        index = archive.load_index()
        missing = broken = 0
        for tile in tiles:
            row = index.get((tile.z, tile.x, tile.y))
            path = archive.tile_path(tile)
            if row is None:
                missing += 1
            elif not path.exists():
                broken += 1
            elif path.stat().st_size != row["bytes"]:
                broken += 1
        total = archive.conn.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM tiles").fetchone()
        print(f"verify: {total[0]:,} manifest rows, {total[1]:,} bytes; "
              f"{missing:,} planned tiles missing, {broken:,} manifest/file mismatches")
        archive.close()
        return 0 if missing == 0 and broken == 0 else 1

    licence_marker = os.environ.get(LICENCE_ENV, "").strip()
    if not licence_marker:
        print(
            f"refusing to fetch: {LICENCE_ENV} is not set.\n"
            "Vicmap Basemaps is a licensed, impression-billed service; set the variable to the "
            "licence number (or another non-empty marker) to confirm the licence covers this archive.",
            file=sys.stderr,
        )
        archive.close()
        return 2

    crawl_id = archive.start_crawl(bbox, zooms, expected)
    fetched = adopted = skipped = failed = 0
    written_bytes = 0
    consecutive_failures = 0
    status = "complete"
    detail = None

    todo: list[Tile] = []
    index = archive.load_index()
    for tile in tiles:
        row = index.get((tile.z, tile.x, tile.y))
        path = archive.tile_path(tile)
        if row is not None and path.exists() and not args.force:
            skipped += 1
            continue
        if row is None and path.exists() and not args.force:
            if archive.adopt(tile) is not None:
                adopted += 1
                continue
        todo.append(tile)
    if args.limit:
        todo = todo[: args.limit]

    print(f"resume: {skipped:,} already archived, {adopted:,} adopted, {len(todo):,} to fetch")
    lock = threading.Lock()
    failure_rows: list[dict] = []
    started = time.monotonic()
    local = threading.local()

    def worker(tile: Tile):
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = requests.Session()
        return download_with_retries(
            session, tile, args.out, args.timeout, args.retries, args.delay
        )

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(worker, tile): tile for tile in todo}
            for done, future in enumerate(as_completed(futures), 1):
                tile = futures[future]
                try:
                    row = future.result()
                except FetchError as exc:
                    failed += 1
                    consecutive_failures += 1
                    failure_rows.append({"z": tile.z, "x": tile.x, "y": tile.y,
                                         "status": exc.status, "error": str(exc), "at": now()})
                    if exc.status in (401, 403):
                        status, detail = "aborted", f"authorisation failure: {exc}"
                        break
                    if consecutive_failures >= args.max_consecutive_failures:
                        status, detail = "aborted", f"{consecutive_failures} consecutive failures; last: {exc}"
                        break
                else:
                    consecutive_failures = 0
                    fetched += 1
                    written_bytes += row["bytes"]
                    with lock:
                        archive.record(row)
                if done % 200 == 0:
                    archive.conn.commit()
                if done % 250 == 0 or done == len(todo):
                    elapsed = max(time.monotonic() - started, 1e-9)
                    rate = done / elapsed
                    remaining = (len(todo) - done) / rate if rate else 0
                    print(
                        f"  {done:,}/{len(todo):,} fetched={fetched:,} failed={failed:,} "
                        f"{rate:.1f} tiles/s eta {remaining/60:.1f} min",
                        flush=True,
                    )
    except KeyboardInterrupt:
        status, detail = "interrupted", "KeyboardInterrupt"
        print("\ninterrupted; manifest keeps every completed tile", file=sys.stderr)
    finally:
        archive.conn.commit()

    if failure_rows:
        with (args.out / "failures.jsonl").open("a") as handle:
            for row in failure_rows:
                handle.write(json.dumps(row) + "\n")

    total_bytes = archive.conn.execute("SELECT COALESCE(SUM(bytes),0) FROM tiles").fetchone()[0]
    archive.finish_crawl(
        crawl_id, fetched=fetched, adopted=adopted, skipped=skipped, failed=failed,
        bytes=total_bytes, status=status, detail=detail,
    )
    summary = archive.summary(crawl_id, bbox, zooms, licence_marker)
    summary["run"] = {
        "fetched": fetched, "adopted": adopted, "skipped": skipped, "failed": failed,
        "written_bytes": written_bytes,
        "duration_s": round(time.monotonic() - started, 1),
        "failures_file": "failures.jsonl" if failure_rows else None,
    }
    path = archive.write_summary(summary)
    print(
        f"{status}: {fetched:,} fetched, {adopted:,} adopted, {skipped:,} skipped, {failed:,} failed; "
        f"{total_bytes:,} bytes total; summary {path}"
    )
    archive.close()
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
