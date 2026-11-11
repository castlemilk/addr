"""Google satellite tile archive (google-tiles-v1), paced like a slow map viewer.

Downloads the public `mt1.google.com/vt/lyrs=s` tile endpoint for a lon/lat box
into an XYZ tree, one request at a time, with a jittered interval and
Retry-After-aware backoff. The archive is resumable and records bytes, sha256
and response headers per tile in SQLite.

This endpoint is the one the addr pipeline already uses for saved scenes and
review rasters. It is not the licensed Google Maps Platform route: the public
tile endpoint carries no bulk-extraction licence, and the documented licensed
route is the Map Tiles API (100,000 2D tiles free per month). The crawler
therefore requires --acknowledge-terms, makes no attempt to defeat bot
controls, and stops on 401/403 rather than retrying around them. Prefer the
Map Tiles API for anything beyond an internal review archive.

Output layout under --out:

    tiles.db                       resumable manifest (tiles + crawls tables)
    latest.json                    summary of the most recent crawl
    crawls/crawl-<UTC>.json        one summary per crawl, never rewritten
    failures.jsonl                 one row per failed tile (appended)
    README.txt                     provenance and terms
    satellite/<z>/<x>/<y>.jpg      the tiles
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sqlite3
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import requests

LAYER = "satellite"
ENDPOINT = "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"
ATTRIBUTION = "Imagery © Google, provider shown in Google Earth (Airbus over Mooroolbark, 2025-12-12 at z18-20)."
USER_AGENT = "Mozilla/5.0 (compatible; addr-tile-archive/1.0; internal review archive)"
DEFAULT_BBOX = (145.298, -37.803, 145.354, -37.758)  # W,S,E,N: Mooroolbark scan
DEFAULT_ZOOMS = (18, 19, 20)
DEFAULT_INTERVAL = 0.5  # one request every 0.5 s, jittered: ~5 h for the full scan
JPEG_MAGIC = b"\xff\xd8\xff"
KNOWN_DATES = {
    "18": "2025-12-12", "19": "2025-12-12", "20": "2025-12-12", "21": "2023-10-18",
    "source": "data/references/imagery-probes/google-tm-dates-mooroolbark-2026-10-19.json",
}

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
    def __init__(self, status: int | None, message: str, *, retryable: bool, retry_after: float = 0.0):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class Tile:
    __slots__ = ("z", "x", "y")

    def __init__(self, z: int, x: int, y: int):
        self.z, self.x, self.y = z, x, y

    @property
    def relpath(self) -> Path:
        return Path(LAYER) / str(self.z) / str(self.x) / f"{self.y}.jpg"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def tile_xy(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 2**z
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_ranges(bbox: tuple[float, float, float, float], zooms: tuple[int, ...]):
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


def parse_bbox(value: str):
    parts = [float(p) for p in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox needs four comma-separated numbers: W,S,E,N")
    return tuple(parts)


def parse_zooms(value: str):
    zooms = tuple(sorted({int(z) for z in value.split(",") if z.strip()}))
    if not zooms or zooms[0] < 0 or zooms[-1] > 22:
        raise argparse.ArgumentTypeError("zooms must be within 0-22")
    return zooms


class Archive:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(root / "tiles.db", timeout=30)
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def tile_path(self, tile: Tile) -> Path:
        return self.root / tile.relpath

    def load_index(self) -> dict[tuple[int, int, int], sqlite3.Row]:
        return {(r["z"], r["x"], r["y"]): r for r in self.conn.execute("SELECT * FROM tiles")}

    def adopt(self, tile: Tile):
        path = self.tile_path(tile)
        try:
            if path.stat().st_size == 0:
                return None
            with path.open("rb") as handle:
                if handle.read(3) != JPEG_MAGIC:
                    return None
        except OSError:
            return None
        row = {
            "z": tile.z, "x": tile.x, "y": tile.y,
            "path": str(tile.relpath), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "last_modified": None, "etag": None, "fetched_at": now(),
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
            (now(), fields["fetched"], fields["adopted"], fields["skipped"], fields["failed"],
             fields["bytes"], fields["status"], fields.get("detail"), crawl_id),
        )
        self.conn.commit()

    def summary(self, crawl_id: int, bbox, zooms) -> dict:
        crawl = self.conn.execute("SELECT * FROM crawls WHERE id=?", (crawl_id,)).fetchone()
        per_zoom = []
        for z, xmin, xmax, ymin, ymax in tile_ranges(bbox, zooms):
            rows = self.conn.execute(
                "SELECT bytes,last_modified FROM tiles WHERE z=? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                (z, xmin, xmax, ymin, ymax),
            ).fetchall()
            dates = Counter(r["last_modified"] or "unknown" for r in rows)
            per_zoom.append({
                "z": z, "tiles": len(rows), "bytes": sum(r["bytes"] for r in rows),
                "last_modified": dict(sorted(dates.items())),
                "known_capture_date": KNOWN_DATES.get(str(z)),
            })
        return {
            "format": "google-tiles-v1",
            "provider": "Google satellite (mt1 lyrs=s)",
            "layer": LAYER,
            "attribution": ATTRIBUTION,
            "endpoint_template": ENDPOINT,
            "known_capture_dates": KNOWN_DATES,
            "bbox_wsen": list(bbox),
            "zooms": list(zooms),
            "crawl": dict(crawl) if crawl else None,
            "archive": {
                "tiles": sum(z["tiles"] for z in per_zoom),
                "bytes": sum(z["bytes"] for z in per_zoom),
                "per_zoom": per_zoom,
            },
        }

    def write_summary(self, summary: dict) -> Path:
        (self.root / "crawls").mkdir(exist_ok=True)
        started = summary["crawl"]["started_at"].replace(":", "").replace("-", "")
        dated = self.root / "crawls" / f"crawl-{started}.json"
        text = json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
        dated.write_text(text)
        (self.root / "latest.json").write_text(text)
        return dated


def fetch_tile(session, tile: Tile, root: Path, timeout: float) -> dict:
    url = ENDPOINT.format(z=tile.z, y=tile.y, x=tile.x)
    try:
        response = session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        raise FetchError(None, f"request failed: {exc}", retryable=True) from exc
    if response.status_code != 200:
        retry_after = 0.0
        try:
            retry_after = float(response.headers.get("Retry-After", "0") or 0)
        except ValueError:
            pass
        retryable = response.status_code in (429, 500, 502, 503, 504)
        raise FetchError(response.status_code, f"HTTP {response.status_code}: {response.text[:120].strip()}",
                         retryable=retryable, retry_after=retry_after)
    body = response.content
    if not body.startswith(JPEG_MAGIC):
        raise FetchError(response.status_code, f"not a JPEG ({response.headers.get('Content-Type')}, {len(body)} bytes)",
                         retryable=False)
    path = root / tile.relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{tile.y}.{os.getpid()}.part")
    part.write_bytes(body)
    os.replace(part, path)
    return {
        "z": tile.z, "x": tile.x, "y": tile.y, "path": str(tile.relpath),
        "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        "last_modified": response.headers.get("Last-Modified"),
        "etag": response.headers.get("ETag"), "fetched_at": now(),
    }


def download_with_retries(session, tile, root, timeout, retries):
    last = None
    for attempt in range(retries + 1):
        try:
            return fetch_tile(session, tile, root, timeout)
        except FetchError as exc:
            last = exc
            if not exc.retryable or attempt == retries:
                raise
            wait = max(exc.retry_after, min(5 * 2**attempt, 60))
            if exc.status is None:
                wait = max(wait, 15)  # DNS / connection outage: give the resolver time
            print(f"  retry z{tile.z}/{tile.x}/{tile.y} in {wait:.0f}s: {exc}", file=sys.stderr, flush=True)
            time.sleep(wait)
    assert last is not None
    raise last


def write_readme(root: Path):
    (root / "README.txt").write_text(
        "Google satellite tile archive\n"
        "=============================\n\n"
        f"Layer: {LAYER} (mt1.google.com/vt, JPEG, Web Mercator, levels 0-22)\n"
        f"Attribution: {ATTRIBUTION}\n\n"
        "This is an internal review archive from the public tile endpoint, not the licensed\n"
        "Google Maps Platform Map Tiles API. Direct bulk extraction is outside the Maps\n"
        "Platform terms; the licensed route is the Map Tiles API (100,000 2D tiles free per\n"
        "month). The crawl is single-threaded, paced and stops on 401/403.\n\n"
        "Known capture dates over Mooroolbark (Google Earth time-machine metadata):\n"
        "  z18-z20: 2025-12-12 (Image © 2026 Airbus, epoch 367)\n"
        "  z21:     2023-10-18 (epoch 1021)\n"
        "See data/references/imagery-probes/google-tm-dates-mooroolbark-2026-10-19.json.\n\n"
        "tiles.db records sha256, bytes, ETag and Last-Modified for every tile;\n"
        "latest.json summarises the most recent crawl.\n"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bbox", type=parse_bbox, default=DEFAULT_BBOX)
    parser.add_argument("--zooms", type=parse_zooms, default=DEFAULT_ZOOMS)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between requests before jitter (default: 0.5)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-consecutive-failures", type=int, default=30,
                        help="abort after this many failures without a success (default: 30)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--acknowledge-terms", action="store_true",
                        help="required: record that this internal archive is outside the Map Tiles API licence")
    args = parser.parse_args(argv)

    bbox, zooms = tuple(args.bbox), tuple(args.zooms)
    ranges = tile_ranges(bbox, zooms)
    counts = {z: (xmax - xmin + 1) * (ymax - ymin + 1) for z, xmin, xmax, ymin, ymax in ranges}
    expected = sum(counts.values())
    print(f"plan: {expected:,} tiles for bbox {bbox} zooms {zooms} -> {args.out}")
    for z, xmin, xmax, ymin, ymax in ranges:
        print(f"  z{z}: {counts[z]:,} tiles  x {xmin}-{xmax}  y {ymin}-{ymax}")
    print(f"pace: one request every {args.interval:.2f}s (jittered), single worker")
    if args.dry_run:
        return 0

    archive = Archive(args.out)
    write_readme(args.out)

    if args.verify:
        index = archive.load_index()
        missing = broken = 0
        for tile in iter_tiles(bbox, zooms):
            row = index.get((tile.z, tile.x, tile.y))
            path = archive.tile_path(tile)
            if row is None:
                missing += 1
            elif not path.exists() or path.stat().st_size != row["bytes"]:
                broken += 1
        total = archive.conn.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM tiles").fetchone()
        print(f"verify: {total[0]:,} manifest rows, {total[1]:,} bytes; "
              f"{missing:,} planned tiles missing, {broken:,} manifest/file mismatches")
        archive.close()
        return 0 if missing == 0 and broken == 0 else 1

    if not args.acknowledge_terms:
        print(
            "refusing to crawl without --acknowledge-terms.\n"
            "The public mt1 endpoint is not the licensed Google Maps Platform route; bulk\n"
            "extraction is outside its terms. The licensed route is the Map Tiles API\n"
            "(GOOGLE_MAPS_API_KEY, 100,000 2D tiles free per month).",
            file=sys.stderr,
        )
        archive.close()
        return 2

    crawl_id = archive.start_crawl(bbox, zooms, expected)
    fetched = adopted = skipped = failed = 0
    consecutive_failures = 0
    status = "complete"
    detail = None

    index = archive.load_index()
    todo: list[Tile] = []
    for tile in iter_tiles(bbox, zooms):
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

    print(f"resume: {skipped:,} archived, {adopted:,} adopted, {len(todo):,} to fetch")
    session = requests.Session()
    started = time.monotonic()
    failure_rows: list[dict] = []
    try:
        for done, tile in enumerate(todo, 1):
            try:
                row = download_with_retries(session, tile, args.out, args.timeout, args.retries)
            except FetchError as exc:
                failed += 1
                consecutive_failures += 1
                failure_rows.append({"z": tile.z, "x": tile.x, "y": tile.y,
                                     "status": exc.status, "error": str(exc), "at": now()})
                if exc.status in (401, 403):
                    status, detail = "aborted", f"authorisation/block failure: {exc}"
                    break
                if consecutive_failures >= args.max_consecutive_failures:
                    status, detail = "aborted", f"{consecutive_failures} consecutive failures; last: {exc}"
                    break
                time.sleep(5)
            else:
                consecutive_failures = 0
                fetched += 1
                archive.record(row)
                if done % 100 == 0:
                    archive.conn.commit()
            if done % 100 == 0 or done == len(todo):
                elapsed = max(time.monotonic() - started, 1e-9)
                rate = done / elapsed
                remaining = (len(todo) - done) / rate if rate else 0
                print(f"  {done:,}/{len(todo):,} fetched={fetched:,} failed={failed:,} "
                      f"{rate:.2f} tiles/s eta {remaining/3600:.1f} h", flush=True)
            time.sleep(max(0.0, args.interval * random.uniform(0.8, 1.2)))
    except KeyboardInterrupt:
        status, detail = "interrupted", "KeyboardInterrupt"
        print("\ninterrupted; manifest keeps every completed tile", file=sys.stderr)
    finally:
        session.close()
        archive.conn.commit()

    if failure_rows:
        with (args.out / "failures.jsonl").open("a") as handle:
            for row in failure_rows:
                handle.write(json.dumps(row) + "\n")

    total_bytes = archive.conn.execute("SELECT COALESCE(SUM(bytes),0) FROM tiles").fetchone()[0]
    archive.finish_crawl(crawl_id, fetched=fetched, adopted=adopted, skipped=skipped,
                         failed=failed, bytes=total_bytes, status=status, detail=detail)
    summary = archive.summary(crawl_id, bbox, zooms)
    summary["run"] = {
        "fetched": fetched, "adopted": adopted, "skipped": skipped, "failed": failed,
        "duration_s": round(time.monotonic() - started, 1),
        "interval_s": args.interval,
        "failures_file": "failures.jsonl" if failure_rows else None,
    }
    path = archive.write_summary(summary)
    print(f"{status}: {fetched:,} fetched, {adopted:,} adopted, {skipped:,} skipped, {failed:,} failed; "
          f"{total_bytes:,} bytes total; summary {path}")
    archive.close()
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
