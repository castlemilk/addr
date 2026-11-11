import hashlib

import pytest
from scripts.ml import vicmap_tiles as vt

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class FakeResponse:
    def __init__(self, body=PNG, status=200, headers=None):
        self.content = body
        self.status_code = status
        self.headers = headers or {}
        self.text = ""


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.response


def test_mooroolbark_plan_ranges_and_counts():
    ranges = vt.tile_ranges(vt.DEFAULT_BBOX, vt.DEFAULT_ZOOMS)
    assert ranges == [
        (18, 236874, 236915, 160804, 160845),
        (19, 473749, 473831, 321608, 321691),
        (20, 947499, 947662, 643217, 643382),
    ]
    counts = [(xmax - xmin + 1) * (ymax - ymin + 1) for _, xmin, xmax, ymin, ymax in ranges]
    assert counts == [1764, 6972, 27224]
    assert sum(counts) == len(list(vt.iter_tiles(vt.DEFAULT_BBOX, vt.DEFAULT_ZOOMS))) == 35960


def test_zooms_above_the_vicmap_ceiling_are_rejected():
    with pytest.raises(Exception):
        vt.parse_zooms("18,21")
    assert vt.parse_zooms("20,18,18") == (18, 20)


def test_fetch_tile_writes_atomically_and_keeps_headers(tmp_path):
    url_headers = {"Last-Modified": "Sat, 14 Feb 2026 06:18:49 GMT", "ETag": '"abc"'}
    session = FakeSession(FakeResponse(headers=url_headers))
    tile = vt.Tile(20, 947539, 643305)
    row = vt.fetch_tile(session, tile, tmp_path, timeout=5)
    path = tmp_path / tile.relpath
    assert path.read_bytes() == PNG
    assert row["bytes"] == len(PNG) and row["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert row["last_modified"] == url_headers["Last-Modified"]
    assert "TILEMATRIX=20" in session.urls[0] and "TILEROW=643305" in session.urls[0]
    assert "TILECOL=947539" in session.urls[0]
    assert not list(tmp_path.rglob("*.part"))


def test_fetch_tile_rejects_non_png_and_marks_it_unretryable(tmp_path):
    session = FakeSession(FakeResponse(body=b"<html>busy</html>", headers={"Content-Type": "text/html"}))
    with pytest.raises(vt.FetchError) as error:
        vt.fetch_tile(session, vt.Tile(18, 1, 1), tmp_path, timeout=5)
    assert "not a PNG" in str(error.value) and not error.value.retryable


def test_licence_gate_makes_no_requests(tmp_path, monkeypatch):
    monkeypatch.delenv(vt.LICENCE_ENV, raising=False)
    assert vt.main(["--out", str(tmp_path), "--limit", "1"]) == 2
    assert not list(tmp_path.rglob("*.png"))


def test_archive_summary_counts_only_the_planned_box(tmp_path):
    archive = vt.Archive(tmp_path)
    crawl = archive.start_crawl(vt.DEFAULT_BBOX, (18,), 1)
    archive.record({
        "z": 18, "x": 236874, "y": 160804, "path": vt.Tile(18, 236874, 160804).relpath.as_posix(),
        "bytes": 120, "sha256": "a" * 64, "last_modified": "Sat, 13 Dec 2025 07:03:43 GMT",
        "etag": None, "fetched_at": vt.now(),
    })
    archive.record({
        "z": 18, "x": 1, "y": 1, "path": "elsewhere.png",
        "bytes": 999, "sha256": "b" * 64, "last_modified": "Sat, 13 Dec 2025 07:03:43 GMT",
        "etag": None, "fetched_at": vt.now(),
    })
    archive.finish_crawl(crawl, fetched=1, adopted=0, skipped=0, failed=0, bytes=120, status="complete")
    summary = archive.summary(crawl, vt.DEFAULT_BBOX, (18,), "test-marker")
    assert summary["archive"]["tiles"] == 1 and summary["archive"]["bytes"] == 120
    assert summary["archive"]["per_zoom"][0]["last_modified"] == {"Sat, 13 Dec 2025 07:03:43 GMT": 1}
    assert len(archive.load_index()) == 2
    archive.close()
