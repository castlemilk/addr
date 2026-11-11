import hashlib

import pytest
from scripts.ml import google_tiles as gt

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


class FakeResponse:
    def __init__(self, body=JPEG, status=200, headers=None):
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


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


def test_mooroolbark_plan_matches_the_vicmap_grid():
    ranges = gt.tile_ranges(gt.DEFAULT_BBOX, gt.DEFAULT_ZOOMS)
    assert ranges == [
        (18, 236874, 236915, 160804, 160845),
        (19, 473749, 473831, 321608, 321691),
        (20, 947499, 947662, 643217, 643382),
    ]
    counts = [(xmax - xmin + 1) * (ymax - ymin + 1) for _, xmin, xmax, ymin, ymax in ranges]
    assert counts == [1764, 6972, 27224]
    assert sum(counts) == len(list(gt.iter_tiles(gt.DEFAULT_BBOX, gt.DEFAULT_ZOOMS))) == 35960


def test_fetch_tile_writes_jpeg_and_keeps_headers(tmp_path):
    session = FakeSession(FakeResponse(headers={"ETag": '"abc"'}))
    tile = gt.Tile(20, 947539, 643305)
    row = gt.fetch_tile(session, tile, tmp_path, timeout=5)
    assert (tmp_path / tile.relpath).read_bytes() == JPEG
    assert row["bytes"] == len(JPEG) and row["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert "TILEMATRIX" not in session.urls[0]
    assert "x=947539" in session.urls[0] and "y=643305" in session.urls[0] and "z=20" in session.urls[0]
    assert not list(tmp_path.rglob("*.part"))


def test_fetch_tile_rejects_html(tmp_path):
    session = FakeSession(FakeResponse(body=b"<html>sorry</html>", headers={"Content-Type": "text/html"}))
    with pytest.raises(gt.FetchError) as error:
        gt.fetch_tile(session, gt.Tile(18, 1, 1), tmp_path, timeout=5)
    assert "not a JPEG" in str(error.value) and not error.value.retryable


def test_rate_limit_keeps_retry_after_and_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(gt.time, "sleep", lambda _: None)
    session = SequenceSession([
        FakeResponse(body=b"", status=429, headers={"Retry-After": "7"}),
        FakeResponse(),
    ])
    row = gt.download_with_retries(session, gt.Tile(18, 1, 1), tmp_path, timeout=5, retries=1)
    assert row["bytes"] == len(JPEG) and len(session.urls) == 2


def test_auth_block_is_not_retried(tmp_path):
    session = SequenceSession([FakeResponse(body=b"blocked", status=403)])
    with pytest.raises(gt.FetchError) as error:
        gt.download_with_retries(session, gt.Tile(18, 1, 1), tmp_path, timeout=5, retries=3)
    assert error.value.status == 403 and not error.value.retryable
    assert len(session.urls) == 1


def test_terms_gate_makes_no_requests(tmp_path):
    assert gt.main(["--out", str(tmp_path), "--limit", "1"]) == 2
    assert not list(tmp_path.rglob("*.jpg"))


def test_summary_records_known_capture_dates(tmp_path):
    archive = gt.Archive(tmp_path)
    crawl = archive.start_crawl(gt.DEFAULT_BBOX, (20,), 1)
    tile = gt.Tile(20, 947539, 643305)
    archive.record({
        "z": 20, "x": 947539, "y": 643305, "path": tile.relpath.as_posix(),
        "bytes": 120, "sha256": "a" * 64, "last_modified": None, "etag": None, "fetched_at": gt.now(),
    })
    archive.finish_crawl(crawl, fetched=1, adopted=0, skipped=0, failed=0, bytes=120, status="complete")
    summary = archive.summary(crawl, gt.DEFAULT_BBOX, (20,))
    assert summary["archive"]["tiles"] == 1
    assert summary["archive"]["per_zoom"][0]["known_capture_date"] == "2025-12-12"
    assert summary["known_capture_dates"]["21"] == "2023-10-18"
    archive.close()
