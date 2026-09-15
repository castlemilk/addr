import gzip
import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from scripts.ml.pipeline import PipelineConfig
from scripts.ml.server import (
    GenerationCoordinator,
    TileHTTPServer,
    TileLRU,
    TileRequestHandler,
)


def make_config(tmp_path, **overrides) -> PipelineConfig:
    values = dict(
        bbox=None,
        zooms=[],
        procedural=True,
        heuristic=True,
        use_osm=False,
        out_px=256,
        margin_px=0,
        imagery_offset=1,
        min_area_m2=10.0,
        cache_dir=tmp_path / "imagery",
    )
    values.update(overrides)
    return PipelineConfig(**values)


@pytest.fixture
def tile_server(tmp_path):
    coordinator = GenerationCoordinator(make_config(tmp_path), concurrency=1, prefetch=False)
    httpd = TileHTTPServer(("127.0.0.1", 0), TileRequestHandler, coordinator)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base_url, coordinator
    httpd.shutdown()
    httpd.server_close()
    coordinator.close()


def http_get(url, timeout=120.0):
    request = urllib.request.Request(url)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()
    return response.status, response.headers, response.read()


def test_healthz(tile_server):
    base_url, _ = tile_server
    status, _, body = http_get(f"{base_url}/healthz")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_stats_endpoint(tile_server):
    base_url, _ = tile_server
    status, _, body = http_get(f"{base_url}/stats")
    assert status == 200
    stats = json.loads(body)
    assert stats["generated"] == 0
    assert "cache" in stats


def test_tile_generation_and_cache(tile_server):
    import mapbox_vector_tile

    base_url, coordinator = tile_server
    url = f"{base_url}/tile/16/59220/40207.pbf"
    status, headers, body = http_get(url)
    assert status == 200
    assert headers.get("Content-Encoding") == "gzip"
    assert headers.get("X-Tile-Generated") == "ai"
    raw = gzip.decompress(body)
    decoded = mapbox_vector_tile.decode(raw)
    features = decoded["building"]["features"]
    assert len(features) >= 1

    status, _, cached = http_get(url)
    assert status == 200
    assert gzip.decompress(cached) == raw
    assert coordinator.generated == 1
    assert coordinator.stats()["cache"]["hits"] >= 1


def test_empty_tile_returns_204_and_is_cached(tmp_path):
    coordinator = GenerationCoordinator(
        make_config(tmp_path, min_area_m2=1e9), concurrency=1, prefetch=False
    )
    httpd = TileHTTPServer(("127.0.0.1", 0), TileRequestHandler, coordinator)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, _, body = http_get(f"{base_url}/tile/16/59220/40207.pbf")
        assert status == 204
        assert body == b""
        assert coordinator.generated == 1

        status, _, _ = http_get(f"{base_url}/tile/16/59220/40207.pbf")
        assert status == 204
        assert coordinator.generated == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        coordinator.close()


def test_invalid_tile_coordinates(tile_server):
    base_url, _ = tile_server
    status, _, _ = http_get(f"{base_url}/tile/2/99/0.pbf")
    assert status == 400
    status, _, _ = http_get(f"{base_url}/tile/abc/0/0.pbf")
    assert status == 404


def test_single_flight_dedupes_concurrent_requests(tmp_path, monkeypatch):
    calls = []

    def slow_generate(cfg, ctx, z, x, y):
        calls.append((z, x, y))
        time.sleep(0.3)
        return b"payload"

    monkeypatch.setattr("scripts.ml.server.generate_tile_mvt", slow_generate)
    coordinator = GenerationCoordinator(make_config(tmp_path), concurrency=2, prefetch=False)
    results = []

    def request_tile():
        results.append(coordinator.get_tile(16, 10, 20))

    threads = [threading.Thread(target=request_tile) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(calls) == 1
    assert len(results) == 4
    assert all(result is not None and gzip.decompress(result) == b"payload" for result in results)
    coordinator.close()


def test_tile_lru_eviction():
    cache = TileLRU(max_entries=2, max_bytes=1024)
    cache.put((1, 1, 1), b"a")
    cache.put((1, 1, 2), b"b")
    cache.put((1, 1, 3), b"c")
    assert len(cache) == 2
    found, _ = cache.get((1, 1, 1))
    assert not found
    found, value = cache.get((1, 1, 3))
    assert found and value == b"c"


def test_server_trees_enabled_by_default():
    from scripts.ml.server import build_config, parse_args

    args = parse_args([])
    assert args.trees is True
    assert parse_args(["--no-trees"]).trees is False
    assert build_config(args).with_trees is True


def test_missing_tile_returns_none(tmp_path, monkeypatch):
    coordinator = GenerationCoordinator(make_config(tmp_path), concurrency=1, prefetch=False)

    def failing(cfg, ctx, z, x, y):
        raise RuntimeError("model exploded")

    monkeypatch.setattr("scripts.ml.server.generate_tile_mvt", failing)
    assert coordinator.get_tile(16, 1, 1) is None
    assert coordinator.failed == 1
    assert coordinator.stats()["failed"] == 1
    coordinator.close()


def test_persist_mbtiles_write_through(tmp_path, monkeypatch):
    from scripts.ml.tiles import read_mbtiles_tile

    def fake_generate(cfg, ctx, z, x, y):
        return b"mvt-payload"

    monkeypatch.setattr("scripts.ml.server.generate_tile_mvt", fake_generate)
    persist_path = tmp_path / "persisted.mbtiles"
    coordinator = GenerationCoordinator(
        make_config(tmp_path), concurrency=1, prefetch=False, persist_path=persist_path
    )
    payload = coordinator.get_tile(16, 100, 200)
    coordinator.close()

    assert payload is not None and gzip.decompress(payload) == b"mvt-payload"
    stored = read_mbtiles_tile(persist_path, 16, 100, 200)
    assert stored == b"mvt-payload"
    assert coordinator.persisted == 1
