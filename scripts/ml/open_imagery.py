"""Open-licence orthophoto ingest (open-imagery-v1).

Fetches a lon/lat box from open WMS, ArcGIS ImageServer/MapServer or (later) STAC-COG
sources at native ground sample distance and writes one Cloud-Optimised GeoTIFF per
source and area:

    driver=COG, 512x512 internal tiles, JPEG/ DEFLATE compression, overviews
    layout=COG (verified by rasterio tags), random window reads for training chips

A SQLite catalog with an R*Tree index records provider, licence, capture, GSD, CRS,
WGS84 bounds, local path, bytes and sha256, so an AOI query returns the COGs to read
without walking the filesystem:

    SELECT a.path FROM assets a JOIN asset_index i ON a.id=i.id
    WHERE i.maxx >= ? AND i.minx <= ? AND i.maxy >= ? AND i.miny <= ?;

Only sources with a licence that permits reuse and derivative training data are in
SOURCES. Google/Mapbox/Azure/Esri basemap terms are not training-data licences and are
deliberately excluded; Vicmap is handled by the separate licensed archive.

Usage:
    python -m scripts.ml.open_imagery list
    python -m scripts.ml.open_imagery fetch --source naip
    python -m scripts.ml.open_imagery fetch --source ign --bbox 4.86,45.75,4.91,45.795 \
        --out /Volumes/gamma-systems-2/addr-imagery/open
    python -m scripts.ml.open_imagery query --bbox 4.86,45.75,4.91,45.795
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import requests
from PIL import Image
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import transform as warp_transform

import rasterio

USER_AGENT = "addr-open-imagery/1.0 (open-licence training archive)"
JPEG_QUALITY = 90


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    kind: str                      # arcgis_image | arcgis_map | wms
    url: str
    licence: str
    attribution: str
    gsd_m: float
    default_bbox: tuple[float, float, float, float]
    layer: str | None = None
    fmt: str = "image/jpeg"
    max_px: int = 2048
    zoom: int | None = None
    coverage: str = ""
    capture: str = "latest service mosaic"
    notes: str = ""


SOURCES: dict[str, Source] = {s.id: s for s in (
    Source(
        "naip", "US NAIP (USDA/USGS) latest mosaic", "arcgis_image",
        "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer",
        "Public domain (USDA/USGS)", "USDA Farm Service Agency / USGS National Map",
        0.6, (-105.115, 39.790, -105.065, 39.835), fmt="tiff", max_px=2048,
        coverage="Conterminous US, 0.6 m since 2018, 2-3 year cycle",
        capture="latest service mosaic (2025 acquisition complete; 2026 dates publishing)",
        notes="Alpha band dropped; RGB written as JPEG COG.",
    ),
    Source(
        "nsw", "NSW Spatial Services imagery (SIX)", "arcgis_map",
        "https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Imagery/MapServer",
        "CC BY 4.0", "NSW Department of Customer Service (Spatial Services)",
        0.1, (150.970, -33.750, 151.020, -33.705), fmt="png", max_px=2048,
        coverage="NSW, urban 5-10 cm", capture="latest cached mosaic",
    ),
    Source(
        "ign", "France IGN BD ORTHO", "wms",
        "https://data.geopf.fr/wms-r/wms", "Open Licence 2.0 (Etalab)", "IGN France",
        0.2, (4.860, 45.750, 4.910, 45.795), layer="HR.ORTHOIMAGERY.ORTHOPHOTOS",
        fmt="image/jpeg", max_px=2048,
        coverage="France, 20 cm, 3-4 year cycle", capture="latest BD ORTHO mosaic",
    ),
    Source(
        "pnoa", "Spain PNOA", "wms",
        "https://www.ign.es/wms-inspire/pnoa-ma", "Open (IGN Spain)", "IGN Spain",
        0.25, (-3.660, 40.520, -3.610, 40.565), layer="OI.OrthoimageCoverage",
        fmt="image/jpeg", max_px=2048,
        coverage="Spain, 25-50 cm, 2-3 year cycle", capture="latest PNOA mosaic",
    ),
    Source(
        "pdok", "Netherlands PDOK 2026 ortho HR", "wms",
        "https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0", "Open (PDOK)", "Beeldmateriaal Nederland",
        0.08, (5.200, 52.070, 5.250, 52.115), layer="2026_orthoHR",
        fmt="image/jpeg", max_px=2048,
        coverage="Netherlands, 8 cm HR, annual", capture="2026 orthoHR",
    ),
    Source(
        "swisstopo", "Switzerland swisstopo SWISSIMAGE", "wms",
        "https://wms.geo.admin.ch/", "Open (swisstopo)", "swisstopo",
        0.1, (8.520, 47.390, 8.570, 47.435), layer="ch.swisstopo.swissimage-product",
        fmt="image/png", max_px=2048,
        coverage="Switzerland, 10 cm, ~3 year cycle", capture="latest SWISSIMAGE",
    ),
    Source(
        "sentinel2", "Sentinel-2 L2A true colour (10 m context)", "sentinel2",
        "https://earth-search.aws.element84.com/v1/search",
        "Copernicus Sentinel Data Terms (free, attribution)",
        "Contains modified Copernicus Sentinel data 2026",
        10.0, (145.298, -37.803, 145.354, -37.758), fmt="image/tiff",
        coverage="Global, 10 m, ~5-day revisit", capture="latest cloud-free scene",
    ),
    Source(
        "linz", "New Zealand LINZ Christchurch 0.075 m (STAC COG)", "linz-stac",
        "s3://nz-imagery/canterbury/christchurch_2025_0.075m/rgb/2193/",
        "CC BY 4.0", "Toitu Te Whenua Land Information New Zealand (LINZ)",
        0.075, (172.620, -43.540, 172.630, -43.530), fmt="image/tiff",
        coverage="Christchurch urban, 7.5 cm, March 2025",
        capture="March 2025",
        notes="Public S3 COGs; no Basemaps API key. Uses teacher_sources STAC catalogue.",
    ),
)}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def bbox_3857(bbox: tuple[float, float, float, float]):
    west, south, east, north = bbox
    xs, ys = warp_transform("EPSG:4326", "EPSG:3857", [west, east], [south, north])
    return min(xs), min(ys), max(xs), max(ys)


def bbox_4326(xmin: float, ymin: float, xmax: float, ymax: float):
    lons, lats = warp_transform("EPSG:3857", "EPSG:4326", [xmin, xmax], [ymin, ymax])
    return min(lons), min(lats), max(lons), max(lats)


class Catalog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                licence TEXT NOT NULL,
                attribution TEXT NOT NULL,
                capture TEXT,
                gsd_m REAL NOT NULL,
                pixel_m REAL,
                crs TEXT NOT NULL,
                bounds_wsen TEXT NOT NULL,
                path TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                compression TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS assets_source ON assets(source_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS asset_index USING rtree(id, minx, maxx, miny, maxy);
        """)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(assets)")}
        if "pixel_m" not in cols:
            self.conn.execute("ALTER TABLE assets ADD COLUMN pixel_m REAL")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def replace(self, row: dict):
        old = self.conn.execute(
            "SELECT id FROM assets WHERE source_id=? AND path=?", (row["source_id"], row["path"])
        ).fetchall()
        for r in old:
            self.conn.execute("DELETE FROM asset_index WHERE id=?", (r["id"],))
            self.conn.execute("DELETE FROM assets WHERE id=?", (r["id"],))
        cur = self.conn.execute(
            "INSERT INTO assets (source_id,title,licence,attribution,capture,gsd_m,pixel_m,crs,bounds_wsen,"
            "path,bytes,sha256,width,height,compression,created_at)"
            " VALUES (:source_id,:title,:licence,:attribution,:capture,:gsd_m,:pixel_m,:crs,:bounds_wsen,"
            ":path,:bytes,:sha256,:width,:height,:compression,:created_at)",
            row,
        )
        self.conn.execute(
            "INSERT INTO asset_index (id,minx,maxx,miny,maxy) VALUES (?,?,?,?,?)",
            (cur.lastrowid, *row["rtree"]),
        )
        self.conn.commit()

    def summary(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT source_id, COUNT(*) AS assets, COALESCE(SUM(bytes),0) AS bytes,"
            " COALESCE(SUM(width*height),0) AS pixels FROM assets GROUP BY source_id ORDER BY source_id"
        )]

    def query(self, bbox):
        west, south, east, north = bbox
        rows = self.conn.execute(
            "SELECT a.* FROM assets a JOIN asset_index i ON a.id=i.id"
            " WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? ORDER BY a.source_id",
            (west, east, south, north),
        ).fetchall()
        return [dict(r) for r in rows]


def get_with_retries(session, url, params, timeout, retries=8):
    last = None
    for attempt in range(retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout,
                                   headers={"User-Agent": USER_AGENT})
        except requests.RequestException as exc:
            last = exc
        else:
            if response.status_code not in (429, 500, 502, 503, 504):
                return response
            last = RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")
        if attempt == retries:
            break
        wait = min(5 * 2 ** attempt, 60)
        print(f"  retry in {wait}s: {last}", file=sys.stderr, flush=True)
        time.sleep(wait)
    raise RuntimeError(str(last))


def request_chunk(source: Source, bounds, width: int, height: int, timeout: float, session) -> Image.Image:
    xmin, ymin, xmax, ymax = bounds
    if source.kind == "wms":
        params = {
            "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
            "LAYERS": source.layer, "STYLES": "", "CRS": "EPSG:3857",
            "BBOX": f"{xmin},{ymin},{xmax},{ymax}", "WIDTH": width, "HEIGHT": height,
            "FORMAT": source.fmt,
        }
        response = get_with_retries(session, source.url, params, timeout)
    elif source.kind == "arcgis_image":
        params = {
            "bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": "3857", "imageSR": "3857",
            "size": f"{width},{height}", "format": "tiff", "f": "image",
            "interpolation": "RSP_BilinearInterpolation",
        }
        response = get_with_retries(session, source.url.rstrip("/") + "/exportImage", params, timeout)
    elif source.kind == "arcgis_map":
        params = {
            "bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": "3857", "imageSR": "3857",
            "size": f"{width},{height}", "format": "png", "f": "image",
        }
        response = get_with_retries(session, source.url.rstrip("/") + "/export", params, timeout)
    else:
        raise ValueError(f"unsupported source kind {source.kind}")
    if response.status_code != 200:
        raise RuntimeError(f"{source.id} HTTP {response.status_code}: {response.text[:160]}")
    content_type = response.headers.get("Content-Type", "")
    if content_type and not content_type.startswith(("image/", "application/octet-stream")):
        raise RuntimeError(f"{source.id} returned {content_type}: {response.text[:160]}")
    data = response.content
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        with MemoryFile(data) as mem, mem.open() as ds:
            arr = ds.read()
    else:
        arr = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))
        arr = arr.transpose(2, 0, 1)
    return to_rgb(arr)


def to_rgb(arr: np.ndarray) -> np.ndarray:
    if arr.shape[0] == 4:
        rgb = arr[:3].astype(np.float32)
        alpha = arr[3:4].astype(np.float32) / 255.0
        white = np.full_like(rgb, 255.0)
        return np.clip(rgb * alpha + white * (1 - alpha), 0, 255).astype(np.uint8)
    if arr.shape[0] == 1:
        return np.repeat(arr[:1], 3, axis=0).astype(np.uint8)
    return arr[:3].astype(np.uint8)


def resize_rgb(arr: np.ndarray, width: int, height: int) -> np.ndarray:
    if arr.shape[2] == width and arr.shape[1] == height:
        return arr
    image = Image.fromarray(arr.transpose(1, 2, 0), "RGB").resize((width, height), Image.LANCZOS)
    return np.asarray(image).transpose(2, 0, 1)


def fetch_linz(source: Source, bbox, out_root: Path, catalog: Catalog, *, quality: int = JPEG_QUALITY,
               timeout: float = 120., pause: float = 0.) -> dict:
    from rasterio.enums import Resampling
    from rasterio.warp import reproject
    from scripts.ml.training import teacher_sources as ts

    spec = ts.source("linz-chc-2025-0075")
    root = Path(".")
    ts.catalog(root, spec)
    cat = ts.load_catalog(root, spec)
    bounds = ts.bbox_to_crs(spec["crs"], bbox)
    names = ts.tiles_covering(cat, bounds)
    if not ts.fully_covered(cat, bounds):
        raise RuntimeError("LINZ STAC tiles do not fully cover the AOI")
    paths = [f"s3://{cat['bucket']}/{cat['tiles'][n]['key']}" for n in names]
    rgba, src_transform = ts.read_mosaic(paths, bounds, res=spec["gsd_m"])
    alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
    rgb = np.clip(rgba[:, :, :3].astype(np.float32) * alpha + 255.0 * (1 - alpha), 0, 255).astype(np.uint8)
    xmin, ymin, xmax, ymax = bbox_3857(tuple(bbox))
    lat_mid = (bbox[1] + bbox[3]) / 2
    pixel_m = source.gsd_m / max(0.01, math.cos(math.radians(lat_mid)))
    width = max(1, int(round((xmax - xmin) / pixel_m)))
    height = max(1, int(round((ymax - ymin) / pixel_m)))
    transform = from_origin(xmin, ymax, pixel_m, pixel_m)
    dst = np.zeros((3, height, width), dtype="uint8")
    reproject(source=np.moveaxis(rgb, 2, 0), destination=dst,
              src_transform=src_transform, src_crs=spec["crs"],
              dst_transform=transform, dst_crs="EPSG:3857",
              resampling=Resampling.bilinear)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = f"{bbox[0]:.4f}_{bbox[1]:.4f}_{bbox[2]:.4f}_{bbox[3]:.4f}".replace("-", "m").replace(".", "p")
    path = out_root / source.id / f"{slug}_{stamp}.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(driver="COG", dtype="uint8", count=3, width=width, height=height,
                   crs="EPSG:3857", transform=transform, compress="JPEG", quality=quality,
                   blocksize=512, overviews="AUTO")
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(dst)
    raw = path.read_bytes()
    with rasterio.open(path) as ds:
        layout = ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT")
        overviews = ds.overviews(1)
    if layout != "COG":
        raise RuntimeError(f"{path} is not a COG (layout={layout})")
    wsen = bbox_4326(xmin, ymin, xmax, ymax)
    row = {
        "source_id": source.id, "title": source.title, "licence": source.licence,
        "attribution": source.attribution, "capture": source.capture,
        "gsd_m": source.gsd_m, "pixel_m": round(pixel_m, 6), "crs": "EPSG:3857",
        "bounds_wsen": json.dumps(list(wsen)), "path": str(path), "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "width": width, "height": height,
        "compression": f"JPEG q{quality}", "created_at": now(),
        "rtree": (wsen[0], wsen[2], wsen[1], wsen[3]),
    }
    catalog.replace(row)
    return {**row, "overviews": overviews, "chunks": len(names), "seconds": 0,
            "layout": layout, "tiles": names}


def fetch_sentinel2(source: Source, bbox, out_root: Path, catalog: Catalog, *, quality: int = JPEG_QUALITY,
                    timeout: float = 90., pause: float = 0.) -> dict:
    import requests as _requests
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    body = {
        "collections": ["sentinel-2-l2a"], "bbox": list(bbox), "limit": 10,
        "datetime": "2026-01-01T00:00:00Z/2026-12-31T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": 15}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    response = _requests.post(source.url, json=body, timeout=timeout)
    response.raise_for_status()
    features = response.json().get("features", [])
    if not features:
        raise RuntimeError("no Sentinel-2 scene under 15% cloud in 2026 for this AOI")
    feature = features[0]
    props = feature["properties"]
    url = feature["assets"]["visual"]["href"]
    xmin, ymin, xmax, ymax = bbox_3857(tuple(bbox))
    lat_mid = (bbox[1] + bbox[3]) / 2
    pixel_m = source.gsd_m / max(0.01, math.cos(math.radians(lat_mid)))
    width = max(1, int(round((xmax - xmin) / pixel_m)))
    height = max(1, int(round((ymax - ymin) / pixel_m)))
    transform = from_origin(xmin, ymax, pixel_m, pixel_m)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = f"{bbox[0]:.4f}_{bbox[1]:.4f}_{bbox[2]:.4f}_{bbox[3]:.4f}".replace("-", "m").replace(".", "p")
    path = out_root / source.id / f"{slug}_{stamp}.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    dst = np.zeros((3, height, width), dtype="uint8")
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
        with rasterio.open("/vsicurl/" + url) as src:
            reproject(source=rasterio.band(src, [1, 2, 3]), destination=dst,
                      src_transform=src.transform, src_crs=src.crs,
                      dst_transform=transform, dst_crs="EPSG:3857",
                      resampling=Resampling.bilinear)
    profile = dict(driver="COG", dtype="uint8", count=3, width=width, height=height,
                   crs="EPSG:3857", transform=transform, compress="JPEG", quality=quality,
                   blocksize=512, overviews="AUTO")
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(dst)
    raw = path.read_bytes()
    with rasterio.open(path) as ds:
        layout = ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT")
        overviews = ds.overviews(1)
    if layout != "COG":
        raise RuntimeError(f"{path} is not a COG (layout={layout})")
    wsen = bbox_4326(xmin, ymin, xmax, ymax)
    row = {
        "source_id": source.id, "title": source.title, "licence": source.licence,
        "attribution": source.attribution, "capture": props.get("datetime", source.capture),
        "gsd_m": source.gsd_m, "pixel_m": round(pixel_m, 6), "crs": "EPSG:3857",
        "bounds_wsen": json.dumps(list(wsen)), "path": str(path), "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "width": width, "height": height,
        "compression": f"JPEG q{quality}", "created_at": now(),
        "rtree": (wsen[0], wsen[2], wsen[1], wsen[3]),
    }
    catalog.replace(row)
    return {**row, "overviews": overviews, "chunks": 1, "seconds": 0, "layout": layout,
            "scene": feature.get("id"), "cloud_cover": props.get("eo:cloud_cover")}


def fetch_xyz(source: Source, bbox, out_root: Path, catalog: Catalog, *, quality: int = JPEG_QUALITY,
              timeout: float = 60., pause: float = .1) -> dict:
    z = source.zoom
    n = 2 ** z
    west, south, east, north = bbox

    def tx(lon):
        return (lon + 180.0) / 360.0 * n

    def ty(lat):
        r = math.radians(lat)
        return (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n

    x0, x1 = math.floor(tx(west)), math.floor(tx(east))
    y0, y1 = math.floor(ty(north)), math.floor(ty(south))
    width, height = (x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256
    xmin = x0 / n * 360.0 - 180.0
    xmax = (x1 + 1) / n * 360.0 - 180.0
    ymax = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y0 / n))))
    ymin = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y1 + 1) / n))))
    xmin, ymin, xmax, ymax = bbox_3857((xmin, ymin, xmax, ymax))
    pixel_m = 156543.03392804097 / n
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = f"{bbox[0]:.4f}_{bbox[1]:.4f}_{bbox[2]:.4f}_{bbox[3]:.4f}".replace("-", "m").replace(".", "p")
    path = out_root / source.id / f"{slug}_{stamp}.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(driver="COG", dtype="uint8", count=3, width=width, height=height,
                   crs="EPSG:3857", transform=from_origin(xmin, ymax, pixel_m, pixel_m),
                   compress="JPEG", quality=quality, blocksize=256, overviews="AUTO")
    started = time.monotonic()
    total = (x1 - x0 + 1) * (y1 - y0 + 1)
    done = 0
    with rasterio.Env(COMPRESS_OVERVIEW="JPEG", JPEG_QUALITY_OVERVIEW=str(max(70, quality - 5))):
        with rasterio.open(path, "w", **profile) as ds:
            for row, y in enumerate(range(y0, y1 + 1)):
                for col, x in enumerate(range(x0, x1 + 1)):
                    url = source.url.format(z=z, x=x, y=y)
                    with requests.Session() as session:
                        response = get_with_retries(session, url, {}, timeout)
                    data = response.content
                    if response.headers.get("Content-Type", "").startswith("application/json"):
                        raise RuntimeError(f"{source.id} z{z}/{x}/{y}: {data[:160].decode('utf-8', 'replace')}")
                    if not data.startswith((b"\xff\xd8\xff", b"RIFF", b"\x89PNG")):
                        raise RuntimeError(f"{source.id} z{z}/{x}/{y}: not an image ({response.headers.get('Content-Type')})")
                    image = Image.open(io.BytesIO(data)).convert("RGBA")
                    arr = to_rgb(np.asarray(image).transpose(2, 0, 1))
                    arr = resize_rgb(arr, 256, 256)
                    ds.write(arr, window=((row * 256, (row + 1) * 256), (col * 256, (col + 1) * 256)))
                    done += 1
                    if done % 100 == 0:
                        print(f"  {done:,}/{total:,} tiles", flush=True)
                    if pause:
                        time.sleep(pause)
    raw = path.read_bytes()
    with rasterio.open(path) as ds:
        layout = ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT")
        overviews = ds.overviews(1)
    if layout != "COG":
        raise RuntimeError(f"{path} is not a COG (layout={layout})")
    lat_mid = (bbox[1] + bbox[3]) / 2
    wsen = bbox_4326(xmin, ymin, xmax, ymax)
    row = {
        "source_id": source.id, "title": source.title, "licence": source.licence,
        "attribution": source.attribution, "capture": source.capture,
        "gsd_m": round(pixel_m * math.cos(math.radians(lat_mid)), 4), "pixel_m": round(pixel_m, 6),
        "crs": "EPSG:3857", "bounds_wsen": json.dumps(list(wsen)), "path": str(path),
        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "width": width, "height": height, "compression": f"JPEG q{quality}", "created_at": now(),
        "rtree": (wsen[0], wsen[2], wsen[1], wsen[3]),
    }
    catalog.replace(row)
    return {**row, "overviews": overviews, "chunks": total,
            "seconds": round(time.monotonic() - started, 1), "layout": layout, "profile": profile}


def fetch(source_id: str, bbox, out_root: Path, catalog: Catalog, *, max_px: int | None = None,
          quality: int = JPEG_QUALITY, timeout: float = 60., pause: float = .15) -> dict:
    source = SOURCES[source_id]
    if source.kind == "xyz":
        return fetch_xyz(source, bbox, out_root, catalog, quality=quality, timeout=timeout, pause=pause)
    if source.kind == "sentinel2":
        return fetch_sentinel2(source, bbox, out_root, catalog, quality=quality, timeout=timeout, pause=pause)
    if source.kind == "linz-stac":
        return fetch_linz(source, bbox, out_root, catalog, quality=quality, timeout=timeout, pause=pause)
    max_px = max_px or source.max_px
    xmin, ymin, xmax, ymax = bbox_3857(tuple(bbox))
    # The archive is EPSG:3857 but the source GSD is metres on the ground. At lat 52
    # one ground metre is 1/cos(lat) Mercator metres; using the ground GSD directly
    # would oversample by 1.6x. `pixel_m` is the Mercator pixel size that preserves
    # the native ground sample distance.
    lat_mid = (bbox[1] + bbox[3]) / 2
    pixel_m = source.gsd_m / max(0.01, math.cos(math.radians(lat_mid)))
    width = max(1, int(round((xmax - xmin) / pixel_m)))
    height = max(1, int(round((ymax - ymin) / pixel_m)))
    # Snap the AOI out to whole request chunks. The NAIP ImageServer 504s on some
    # non-square edge sizes (e.g. 1085x629) but serves the padded full chunk.
    width = math.ceil(width / max_px) * max_px
    height = math.ceil(height / max_px) * max_px
    xmax = xmin + width * pixel_m
    ymin = ymax - height * pixel_m
    if width * height > 4_000_000_000:
        raise SystemExit(f"{source.id}: {width}x{height} px is too large for one COG; split the AOI")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = f"{bbox[0]:.4f}_{bbox[1]:.4f}_{bbox[2]:.4f}_{bbox[3]:.4f}".replace("-", "m").replace(".", "p")
    path = out_root / source.id / f"{slug}_{stamp}.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(
        driver="COG", dtype="uint8", count=3, width=width, height=height,
        crs="EPSG:3857", transform=from_origin(xmin, ymax, pixel_m, pixel_m),
        compress="JPEG", quality=quality, blocksize=512, overviews="AUTO",
    )
    started = time.monotonic()
    chunks = []
    total_chunks = math.ceil(width / max_px) * math.ceil(height / max_px)
    with rasterio.Env(COMPRESS_OVERVIEW="JPEG", JPEG_QUALITY_OVERVIEW=str(max(70, quality - 5))):
        with rasterio.open(path, "w", **profile) as ds:
            for row0 in range(0, height, max_px):
                for col0 in range(0, width, max_px):
                    w = min(max_px, width - col0)
                    h = min(max_px, height - row0)
                    cb = (
                        xmin + col0 * pixel_m,
                        ymax - (row0 + h) * pixel_m,
                        xmin + (col0 + w) * pixel_m,
                        ymax - row0 * pixel_m,
                    )
                    try:
                        with requests.Session() as chunk_session:
                            image = request_chunk(source, cb, w, h, timeout, chunk_session)
                    except Exception as exc:
                        raise RuntimeError(f"chunk col={col0} row={row0} {w}x{h} bbox={cb}: {exc}") from exc
                    arr = resize_rgb(image, w, h)
                    ds.write(arr, window=((row0, row0 + h), (col0, col0 + w)))
                    chunks.append((col0, row0, w, h))
                    if len(chunks) % 100 == 0:
                        print(f"  {len(chunks):,}/{total_chunks:,} chunks", flush=True)
                    if pause:
                        time.sleep(pause)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    with rasterio.open(path) as ds:
        layout = ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT")
        overviews = ds.overviews(1)
        actual = (ds.width, ds.height, ds.crs, ds.transform)
    if layout != "COG":
        raise RuntimeError(f"{path} is not a COG (layout={layout})")
    wsen = bbox_4326(xmin, ymin, xmax, ymax)
    row = {
        "source_id": source.id, "title": source.title, "licence": source.licence,
        "attribution": source.attribution, "capture": source.capture,
        "gsd_m": source.gsd_m, "pixel_m": round(pixel_m, 6), "crs": "EPSG:3857",
        "bounds_wsen": json.dumps(list(wsen)), "path": str(path),
        "bytes": len(data), "sha256": digest, "width": width, "height": height,
        "compression": f"JPEG q{quality}", "created_at": now(),
        "rtree": (wsen[0], wsen[2], wsen[1], wsen[3]),
    }
    catalog.replace(row)
    return {**row, "overviews": overviews, "chunks": len(chunks),
            "seconds": round(time.monotonic() - started, 1), "layout": layout,
            "profile": profile}


def register_existing(source_id: str, path: Path, catalog: Catalog, capture: str | None = None) -> dict:
    source = SOURCES[source_id]
    data = path.read_bytes()
    with rasterio.open(path) as ds:
        b = ds.bounds
        pixel = ds.transform.a
        width, height = ds.width, ds.height
        layout = ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT")
        if layout != "COG":
            raise SystemExit(f"{path} is not a COG (layout={layout})")
        wsen = bbox_4326(b.left, b.bottom, b.right, b.top)
    row = {
        "source_id": source.id, "title": source.title, "licence": source.licence,
        "attribution": source.attribution, "capture": capture or source.capture,
        "gsd_m": source.gsd_m, "pixel_m": round(pixel, 6), "crs": "EPSG:3857",
        "bounds_wsen": json.dumps(list(wsen)), "path": str(path), "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height,
        "compression": f"JPEG q{JPEG_QUALITY}", "created_at": now(),
        "rtree": (wsen[0], wsen[2], wsen[1], wsen[3]),
    }
    catalog.replace(row)
    return row


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    fetch_p = sub.add_parser("fetch")
    fetch_p.add_argument("--source", required=True, choices=sorted(SOURCES))
    fetch_p.add_argument("--bbox", type=lambda s: tuple(float(v) for v in s.split(",")), default=None,
                         help="W,S,E,N lon/lat (default: the source's pilot AOI)")
    fetch_p.add_argument("--out", type=Path, default=Path("data/imagery/open"))
    fetch_p.add_argument("--db", type=Path, default=None)
    fetch_p.add_argument("--max-px", type=int, default=None)
    fetch_p.add_argument("--quality", type=int, default=JPEG_QUALITY)
    fetch_p.add_argument("--pause", type=float, default=.15, help="seconds between chunk requests")
    fetch_p.add_argument("--dry-run", action="store_true")
    reg_p = sub.add_parser("register")
    reg_p.add_argument("--source", required=True, choices=sorted(SOURCES))
    reg_p.add_argument("--path", type=Path, required=True)
    reg_p.add_argument("--db", type=Path, default=None)
    reg_p.add_argument("--out", type=Path, default=Path("data/imagery/open"))
    reg_p.add_argument("--capture", default=None)
    assets_p = sub.add_parser("assets")
    assets_p.add_argument("--db", type=Path, default=None)
    assets_p.add_argument("--out", type=Path, default=Path("data/imagery/open"))
    query_p = sub.add_parser("query")
    query_p.add_argument("--bbox", required=True, type=lambda s: tuple(float(v) for v in s.split(",")))
    query_p.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.command == "list":
        for s in SOURCES.values():
            print(f"{s.id:10s} {s.gsd_m*100:5.1f} cm  {s.licence:24s} {s.title}")
            print(f"           {s.coverage}")
        return 0

    db = args.db or (args.out / "catalog.db")
    if args.command == "fetch":
        source = SOURCES[args.source]
        bbox = tuple(args.bbox) if args.bbox else source.default_bbox
        if len(bbox) != 4:
            raise SystemExit("bbox needs W,S,E,N")
        xmin, ymin, xmax, ymax = bbox_3857(bbox)
        pixel_m = source.gsd_m / max(0.01, math.cos(math.radians((bbox[1] + bbox[3]) / 2)))
        width = int(round((xmax - xmin) / pixel_m))
        height = int(round((ymax - ymin) / pixel_m))
        max_px = args.max_px or source.max_px
        chunks = math.ceil(width / max_px) * math.ceil(height / max_px)
        print(f"{source.id}: {width}x{height} px at {source.gsd_m} m; {chunks} requests; "
              f"~{width*height*3/1e6:.0f} MB raw")
        if args.dry_run:
            return 0
        catalog = Catalog(db)
        row = fetch(source.id, bbox, args.out, catalog, max_px=args.max_px, quality=args.quality, pause=args.pause)
        print(json.dumps({k: row[k] for k in ("path", "bytes", "width", "height", "chunks", "seconds", "overviews", "layout")}, indent=2))
        return 0
    if args.command == "assets":
        catalog = Catalog(db)
        for row in catalog.summary():
            print(json.dumps(row))
        catalog.close()
        return 0
    if args.command == "register":
        catalog = Catalog(db)
        row = register_existing(args.source, args.path, catalog, args.capture)
        print(json.dumps({k: row[k] for k in ("path", "bytes", "width", "height", "pixel_m", "bounds_wsen")}, indent=2))
        return 0
    if args.command == "query":
        catalog = Catalog(db)
        for row in catalog.query(args.bbox):
            print(json.dumps({k: row[k] for k in ("source_id", "title", "capture", "gsd_m", "pixel_m", "bounds_wsen", "path", "bytes")}))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
