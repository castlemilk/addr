"""Realtime AI building-tile generation sidecar.

Serves Mapbox Vector Tiles generated on demand from high-resolution aerial
imagery using the same segmentation + depth pipeline used for batch builds.
The Go tile microservice calls this service on MBTiles misses and caches the
result, so map pans generate new 3D building tiles within seconds.

Endpoints:
    GET /healthz
    GET /stats
    GET /tile/{z}/{x}/{y}.pbf

Usage:
    python -m scripts.ml.server --port 8090 --heights
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import queue
import re
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "scripts.ml"

from .pipeline import PipelineConfig, PipelineContext, build_context, generate_tile_mvt
from .presets import DEFAULT_SEGMENTER, SEGMENTER_PRESETS
from .tiles import MBTilesWriter

LOGGER = logging.getLogger("ml.server")
TILE_PATH = re.compile(r"^/tile/(\d{1,2})/(\d+)/(\d+)\.pbf$")
MAX_ZOOM = 22


class TileLRU:
    """Thread-safe LRU cache for gzipped MVT payloads (empty payloads included)."""

    def __init__(self, max_entries: int = 2048, max_bytes: int = 256 * 1024 * 1024):
        self._items: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
        self._bytes = 0
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: tuple[int, int, int]) -> tuple[bool, bytes]:
        with self._lock:
            if key not in self._items:
                self.misses += 1
                return False, b""
            value = self._items.pop(key)
            self._items[key] = value
            self.hits += 1
            return True, value

    def put(self, key: tuple[int, int, int], value: bytes) -> None:
        with self._lock:
            if key in self._items:
                self._bytes -= len(self._items[key])
                self._items.pop(key)
            self._items[key] = value
            self._bytes += len(value)
            while self._items and (
                len(self._items) > self._max_entries or self._bytes > self._max_bytes
            ):
                _, evicted = self._items.popitem(last=False)
                self._bytes -= len(evicted)
                self.evictions += 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._items),
                "bytes": self._bytes,
                "hits": self.hits,
                "misses": self.misses,
                "hit_ratio": (self.hits / total) if total else 0.0,
                "evictions": self.evictions,
            }


class GenerationCoordinator:
    """Single-flight, concurrency-limited tile generation with neighbor prefetch."""

    def __init__(
        self,
        cfg: PipelineConfig,
        concurrency: int = 2,
        cache: TileLRU | None = None,
        persist_path: Path | None = None,
        prefetch: bool = True,
        prefetch_workers: int = 2,
        prefetch_queue_size: int = 256,
    ):
        self.cfg = cfg
        self.cache = cache or TileLRU()
        self.concurrency = max(1, concurrency)
        self._semaphore = threading.Semaphore(self.concurrency)
        self._context: PipelineContext | None = None
        self._context_lock = threading.Lock()
        self._inflight: dict[tuple[int, int, int], threading.Event] = {}
        self._inflight_lock = threading.Lock()
        self._writer: MBTilesWriter | None = None
        if persist_path is not None:
            self._writer = MBTilesWriter(persist_path, overwrite=False)
        self._writer_lock = threading.Lock()
        self.prefetch = prefetch
        self._queue: queue.Queue[tuple[int, int, int]] = queue.Queue(
            maxsize=prefetch_queue_size
        )
        self._queued: set[tuple[int, int, int]] = set()
        self._queued_lock = threading.Lock()
        self.generated = 0
        self.failed = 0
        self.prefetched = 0
        self.persisted = 0
        self._workers: list[threading.Thread] = []
        if prefetch:
            for index in range(max(1, prefetch_workers)):
                worker = threading.Thread(
                    target=self._prefetch_worker, name=f"prefetch-{index}", daemon=True
                )
                worker.start()
                self._workers.append(worker)

    def close(self) -> None:
        with self._writer_lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None

    def context(self) -> PipelineContext:
        if self._context is None:
            with self._context_lock:
                if self._context is None:
                    self._context = build_context(self.cfg)
        return self._context

    def get_tile(self, z: int, x: int, y: int) -> bytes | None:
        key = (z, x, y)
        found, value = self.cache.get(key)
        if found:
            return value

        with self._inflight_lock:
            event = self._inflight.get(key)
            primary = event is None
            if primary:
                event = threading.Event()
                self._inflight[key] = event

        if not primary:
            assert event is not None
            event.wait(timeout=180.0)
            found, value = self.cache.get(key)
            return value if found else None

        try:
            return self._generate(z, x, y)
        finally:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            event.set()  # type: ignore[union-attr]

    def stats(self) -> dict:
        with self._inflight_lock:
            inflight = len(self._inflight)
        return {
            "generated": self.generated,
            "failed": self.failed,
            "prefetched": self.prefetched,
            "persisted": self.persisted,
            "inflight": inflight,
            "queue": self._queue.qsize(),
            "concurrency": self.concurrency,
            "cache": self.cache.stats(),
        }

    def _generate(self, z: int, x: int, y: int) -> bytes | None:
        started = time.time()
        try:
            with self._semaphore:
                raw = generate_tile_mvt(self.cfg, self.context(), z, x, y)
            payload = gzip.compress(raw, compresslevel=6) if raw else b""
        except Exception as error:
            self.failed += 1
            LOGGER.exception("tile generation failed for %s/%s/%s: %s", z, x, y, error)
            return None

        self.cache.put((z, x, y), payload)
        self.generated += 1
        if raw and self._writer is not None:
            with self._writer_lock:
                if self._writer is not None:
                    self._writer.write_tile(z, x, y, raw)
                    self.persisted += 1
        LOGGER.info(
            "generated tile %s/%s/%s in %.2fs (%d bytes)",
            z,
            x,
            y,
            time.time() - started,
            len(payload),
        )
        if self.prefetch:
            self._enqueue_neighbors(z, x, y)
        return payload

    def _enqueue_neighbors(self, z: int, x: int, y: int) -> None:
        limit = 1 << z
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if not (0 <= nx < limit and 0 <= ny < limit):
                    continue
                key = (z, nx, ny)
                found, _ = self.cache.get(key)
                if found:
                    continue
                with self._queued_lock:
                    if key in self._queued:
                        continue
                    self._queued.add(key)
                try:
                    self._queue.put_nowait(key)
                except queue.Full:
                    with self._queued_lock:
                        self._queued.discard(key)
                    return

    def _prefetch_worker(self) -> None:
        while True:
            key = self._queue.get()
            try:
                with self._queued_lock:
                    self._queued.discard(key)
                found, _ = self.cache.get(key)
                if found:
                    continue
                z, x, y = key
                self._generate(z, x, y)
                self.prefetched += 1
            except Exception:
                LOGGER.exception("prefetch failed for tile %s", key)
            finally:
                self._queue.task_done()


class TileHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    coordinator: GenerationCoordinator

    def __init__(self, address, handler, coordinator: GenerationCoordinator):
        super().__init__(address, handler)
        self.coordinator = coordinator


class TileRequestHandler(BaseHTTPRequestHandler):
    server_version = "addr-tile-generator/1.0"

    def log_message(self, format: str, *args) -> None:
        LOGGER.debug("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        coordinator = self.server.coordinator  # type: ignore[attr-defined]

        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/stats":
            self._send_json(200, coordinator.stats())
            return

        match = TILE_PATH.match(path)
        if not match:
            self._send_json(404, {"error": "not found", "hint": "/tile/{z}/{x}/{y}.pbf"})
            return

        z = int(match.group(1))
        x = int(match.group(2))
        y = int(match.group(3))
        limit = 1 << z
        if z > MAX_ZOOM or not (0 <= x < limit and 0 <= y < limit):
            self._send_json(400, {"error": "tile coordinates out of range"})
            return

        payload = coordinator.get_tile(z, x, y)
        if payload is None:
            self._send_json(500, {"error": "tile generation failed"})
            return

        self.send_response(204 if not payload else 200)
        self._cors_headers()
        if payload:
            self.send_header("Content-Type", "application/vnd.mapbox-vector-tile")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "public, max-age=604800, immutable")
            self.send_header("ETag", f'"ai-{z}-{x}-{y}-{len(payload)}"')
            self.send_header("X-Tile-Generated", "ai")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


def build_config(args: argparse.Namespace) -> PipelineConfig:
    return PipelineConfig(
        bbox=None,
        zooms=[],
        provider=args.provider,
        cache_dir=args.imagery_dir,
        imagery_offset=args.imagery_offset,
        out_px=args.out_px,
        margin_px=args.margin_px,
        seg_preset=args.seg_preset,
        seg_model=args.seg_model,
        depth_model=args.depth_model,
        seg_stride=args.seg_stride,
        threshold=args.threshold,
        min_area_m2=args.min_area_m2,
        simplify_px=args.simplify_px,
        with_heights=args.heights,
        assume_height=args.assume_height,
        with_trees=args.trees,
        tree_min_area_m2=args.tree_min_area_m2,
        tree_max_area_m2=args.tree_max_area_m2,
        tree_min_height_m=args.tree_min_height,
        tree_max_height_m=args.tree_max_height,
        assume_tree_height=args.assume_tree_height,
        exg_threshold=args.exg_threshold,
        sam3=args.sam3,
        sam3_models_dir=args.sam3_models_dir,
        sam3_prompt=args.sam3_prompt,
        sam3_threshold=args.sam3_threshold,
        sam3_min_iou=args.sam3_min_iou,
        sam3_trees=args.sam3_trees,
        sam3_tree_prompt=args.sam3_tree_prompt,
        use_osm=False,
        heuristic=args.heuristic,
        procedural=args.procedural,
        require_models=args.require_models,
        workers=args.workers,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ml.server",
        description="Realtime building-tile generation service for the Go tile microservice.",
    )
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", default=8090, type=int)
    parser.add_argument("--concurrency", default=2, type=int, help="parallel tile generations")
    parser.add_argument("--cache-entries", default=2048, type=int)
    parser.add_argument("--cache-mb", default=256, type=int)
    parser.add_argument("--prefetch", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--prefetch-workers", default=2, type=int)
    parser.add_argument("--persist-mbtiles", default=None, type=Path)
    parser.add_argument("--provider", default="google", choices=["google", "google-hybrid", "esri"])
    parser.add_argument("--imagery-dir", default="data/imagery", type=Path)
    parser.add_argument("--imagery-offset", default=1, type=int)
    parser.add_argument("--out-px", default=512, type=int)
    parser.add_argument("--margin-px", default=32, type=int)
    parser.add_argument(
        "--seg-preset",
        default=DEFAULT_SEGMENTER,
        choices=sorted(SEGMENTER_PRESETS),
    )
    parser.add_argument("--seg-model", default=None, type=Path)
    parser.add_argument("--depth-model", default=None, type=Path)
    parser.add_argument("--seg-stride", default=None, type=int)
    parser.add_argument("--threshold", default=None, type=float)
    parser.add_argument("--min-area-m2", default=20.0, type=float)
    parser.add_argument("--simplify-px", default=1.25, type=float)
    parser.add_argument("--heights", action="store_true", help="estimate heights with the depth model")
    parser.add_argument("--assume-height", default=7.0, type=float)
    parser.add_argument(
        "--trees",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="extract tree canopies (requires --heights)",
    )
    parser.add_argument("--tree-min-area-m2", default=6.0, type=float)
    parser.add_argument("--tree-max-area-m2", default=900.0, type=float)
    parser.add_argument("--tree-min-height", default=3.0, type=float)
    parser.add_argument("--tree-max-height", default=30.0, type=float)
    parser.add_argument("--assume-tree-height", default=9.0, type=float)
    parser.add_argument("--exg-threshold", default=0.05, type=float)
    parser.add_argument("--sam3", action="store_true", help="refine roof outlines with SAM3 (slower first pass)")
    parser.add_argument("--sam3-prompt", default="house")
    parser.add_argument("--sam3-threshold", default=0.4, type=float)
    parser.add_argument("--sam3-min-iou", default=0.3, type=float)
    parser.add_argument("--sam3-trees", action="store_true", help="also refine/complete tree canopies with SAM3")
    parser.add_argument("--sam3-tree-prompt", default="tree")
    parser.add_argument("--sam3-models-dir", default=None, type=Path)
    parser.add_argument("--heuristic", action="store_true")
    parser.add_argument("--procedural", action="store_true")
    parser.add_argument("--require-models", action="store_true")
    parser.add_argument("--workers", default=8, type=int, help="imagery fetch threads")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = build_config(args)
    cache = TileLRU(max_entries=args.cache_entries, max_bytes=args.cache_mb * 1024 * 1024)
    coordinator = GenerationCoordinator(
        cfg,
        concurrency=args.concurrency,
        cache=cache,
        persist_path=args.persist_mbtiles,
        prefetch=args.prefetch,
        prefetch_workers=args.prefetch_workers,
    )
    server = TileHTTPServer((args.bind, args.port), TileRequestHandler, coordinator)
    LOGGER.info(
        "realtime tile generator listening on %s:%d (preset=%s, heights=%s, trees=%s, concurrency=%d)",
        args.bind,
        args.port,
        args.seg_preset,
        args.heights,
        args.trees,
        args.concurrency,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("shutting down tile generator")
    finally:
        server.server_close()
        coordinator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
