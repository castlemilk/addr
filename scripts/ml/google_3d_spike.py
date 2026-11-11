"""Bounded Google 3D Tiles + Street View spike (google-3d-spike-v1).

Uses only the documented Google Maps Platform endpoints:
  POST https://tile.googleapis.com/v1/createSession          (Map Tiles API, 3D)
  GET  https://tile.googleapis.com/v1/3dtiles/root.json
  GET  https://maps.googleapis.com/maps/api/streetview/metadata
  GET  https://maps.googleapis.com/maps/api/streetview

The spike is deliberately small: it walks only 3D Tiles children whose bounding region
intersects the AOI, fetches at most --max-tiles, and saves at most --max-images Street View
images. It is not a bulk extractor.

An API key is required. Set GOOGLE_MAPS_API_KEY (a Cloud project with the Map Tiles API
and Street View Static API enabled). Without it the script refuses and makes no request.
Google's terms still apply; a key/free tier is not the same as an open licence. Review
the terms before storing or training on the output.

    GOOGLE_MAPS_API_KEY=... python -m scripts.ml.google_3d_spike \
        --out /Volumes/gamma-systems-2/addr-imagery/google-spike \
        --lat -37.79033 --lon 145.31259 --max-tiles 12 --max-images 2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests

CREATE_SESSION = "https://tile.googleapis.com/v1/createSession"
ROOT = "https://tile.googleapis.com/v1/3dtiles/root.json"
STREET_META = "https://maps.googleapis.com/maps/api/streetview/metadata"
STREET_IMAGE = "https://maps.googleapis.com/maps/api/streetview"
KEY_ENV = "GOOGLE_MAPS_API_KEY"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def region_intersects(region, bbox) -> bool:
    """3D Tiles region is [west, south, east, north, minH, maxH] in radians."""
    if not region or len(region) < 4:
        return True
    w, s, e, n = (math.degrees(v) for v in region[:4])
    west, south, east, north = bbox
    return not (e < west or w > east or n < south or s > north)


def fetch_3d(key: str, bbox, out: Path, max_tiles: int, timeout: float) -> dict:
    session = requests.post(
        CREATE_SESSION, params={"key": key},
        json={"mapType": "photorealistic", "language": "en", "region": "AU"},
        timeout=timeout,
    )
    session.raise_for_status()
    payload = session.json()
    token = payload.get("session")
    if not token:
        raise RuntimeError(f"createSession returned no session: {payload}")
    out.mkdir(parents=True, exist_ok=True)
    root = requests.get(ROOT, params={"session": token, "key": key}, timeout=timeout)
    root.raise_for_status()
    root_json = root.json()
    (out / "root.json").write_text(json.dumps(root_json, indent=2) + "\n")
    manifest = {
        "format": "google-3d-spike-v1", "created_at": now(), "bbox_wsen": list(bbox),
        "session_expiry": payload.get("expiry"), "tile_width": payload.get("tileWidth"),
        "root_sha256": sha(root.content), "tiles": [], "skipped": 0,
    }
    queue = []
    root_node = root_json.get("root", {})
    for child in root_node.get("children", []) or []:
        queue.append(child)
    if root_node.get("content"):
        queue.insert(0, root_node)
    seen = set()
    while queue and len(manifest["tiles"]) < max_tiles:
        node = queue.pop(0)
        if not region_intersects(node.get("boundingVolume", {}).get("region"), bbox):
            manifest["skipped"] += 1
            continue
        content = node.get("content") or {}
        uri = content.get("uri")
        if not uri:
            continue
        absolute = urljoin(ROOT, uri)
        if absolute in seen:
            continue
        seen.add(absolute)
        response = requests.get(absolute, params={"session": token, "key": key}, timeout=timeout)
        if response.status_code != 200:
            manifest["tiles"].append({"uri": absolute, "status": response.status_code})
            continue
        suffix = ".glb" if response.content[:4] == b"glTF" else ".json"
        name = f"tile-{len(manifest['tiles']):03d}{suffix}"
        path = out / name
        path.write_bytes(response.content)
        manifest["tiles"].append({
            "uri": absolute, "path": name, "bytes": len(response.content),
            "sha256": sha(response.content), "region": node.get("boundingVolume", {}).get("region"),
        })
        if suffix == ".json":
            try:
                child_json = response.json()
            except ValueError:
                continue
            for child in (child_json.get("root", {}) or {}).get("children", []) or []:
                queue.append(child)
    (out / "manifest-3d.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def fetch_street(key: str, lat: float, lon: float, out: Path, max_images: int, timeout: float) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    meta_response = requests.get(
        STREET_META, params={"location": f"{lat},{lon}", "key": key}, timeout=timeout
    )
    meta_response.raise_for_status()
    meta = meta_response.json()
    result = {"format": "google-streetview-spike-v1", "created_at": now(),
              "metadata": meta, "images": []}
    if meta.get("status") != "OK" or max_images <= 0:
        (out / "manifest-streetview.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    image = requests.get(
        STREET_IMAGE,
        params={"size": "640x640", "location": f"{lat},{lon}", "fov": 80, "pitch": 0,
                "source": "outdoor", "key": key},
        timeout=timeout,
    )
    if image.status_code == 200 and image.headers.get("Content-Type", "").startswith("image/"):
        path = out / "streetview-000.jpg"
        path.write_bytes(image.content)
        result["images"].append({"path": path.name, "bytes": len(image.content),
                                 "sha256": sha(image.content), "pano_id": meta.get("pano_id"),
                                 "date": meta.get("date"), "copyright": meta.get("copyright")})
    else:
        result["image_error"] = {"status": image.status_code, "body": image.text[:200]}
    (out / "manifest-streetview.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lat", type=float, default=-37.79033)
    parser.add_argument("--lon", type=float, default=145.31259)
    parser.add_argument("--max-tiles", type=int, default=12)
    parser.add_argument("--max-images", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=60.)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    key = os.environ.get(KEY_ENV, "").strip()
    print(f"AOI {args.lat},{args.lon}; caps: {args.max_tiles} 3D tiles, {args.max_images} Street View images")
    if args.dry_run:
        return 0
    if not key:
        print(
            f"refusing: {KEY_ENV} is not set.\n"
            "These are documented, keyed Google Maps Platform APIs; an unregistered caller gets HTTP 403.\n"
            "Enable Map Tiles API + Street View Static API in a Cloud project, create an API key, and set "
            f"{KEY_ENV}. A small spike should stay inside the free tier, but Google's terms still apply.",
            file=sys.stderr,
        )
        return 2
    bbox = (args.lon - 0.004, args.lat - 0.003, args.lon + 0.004, args.lat + 0.003)
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_3d = fetch_3d(key, bbox, args.out / "3d", args.max_tiles, args.timeout)
    manifest_sv = fetch_street(key, args.lat, args.lon, args.out / "streetview", args.max_images, args.timeout)
    summary = {
        "generated_at": now(), "aoi": [args.lat, args.lon], "bbox_wsen": list(bbox),
        "tiles_3d": len(manifest_3d["tiles"]), "streetview_status": manifest_sv["metadata"].get("status"),
        "streetview_images": len(manifest_sv["images"]),
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
