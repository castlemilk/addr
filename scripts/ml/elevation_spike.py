"""Open DSM/DTM/nDSM spike for training (elevation-spike-v1).

Uses the Dutch AHN 0.5 m WCS (PDOK) to fetch a DSM and DTM for the Utrecht and Rotterdam
AOIs, derives nDSM = DSM - DTM, writes compressed COGs and indexes them in SQLite with an
R*Tree. Also renders a hillshade and a 3D surface preview so the result is inspectable.

AHN is open (CC BY 4.0). This is the open stand-in for the USGS 3DEP + NAIP idea:
3DEP's dynamic service is bare-earth DEM only, while AHN publishes both DSM and DTM.

    python -m scripts.ml.elevation_spike \
        --out /Volumes/gamma-systems-2/addr-imagery/elevation --aoi utrecht
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import warnings
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.warp import transform as warp_transform
from rasterio.transform import from_origin

WCS = "https://service.pdok.nl/rws/ahn/wcs/v1_0"
STAC = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d/items"
LICENCE = "CC BY 4.0 (AHN / PDOK)"
ATTRIBUTION = "Actueel Hoogtebestand Nederland (AHN), PDOK"
AOIS = {
    "utrecht": (5.200, 52.070, 5.225, 52.095),
    "rotterdam": (4.100, 51.930, 4.125, 51.955),
    "zurich": (8.520, 47.390, 8.570, 47.435),
    "interlaken": (7.820, 46.660, 7.870, 46.700),
}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rd_bbox(bbox):
    w, s, e, n = bbox
    xs, ys = warp_transform("EPSG:4326", "EPSG:28992", [w, e], [s, n])
    return min(xs), min(ys), max(xs), max(ys)


def wsen(ds):
    left, bottom, right, top = ds.bounds
    lons, lats = warp_transform(ds.crs, "EPSG:4326", [left, right], [bottom, top])
    return min(lons), min(lats), max(lons), max(lats)


def fetch_coverage(coverage: str, bbox, timeout: float = 180.) -> np.ndarray:
    """Mosaic 1 km WCS chunks into one 0.5 m grid; the service rejects larger requests."""
    from rasterio.io import MemoryFile
    xmin, ymin, xmax, ymax = rd_bbox(bbox)
    pixel = 0.5
    xmin = math.floor(xmin / pixel) * pixel
    ymin = math.floor(ymin / pixel) * pixel
    xmax = math.ceil(xmax / pixel) * pixel
    ymax = math.ceil(ymax / pixel) * pixel
    width = int(round((xmax - xmin) / pixel))
    height = int(round((ymax - ymin) / pixel))
    out = np.full((height, width), np.nan, dtype="float32")
    nodata = -9999.0
    chunk = 1000.0
    for x0 in np.arange(xmin, xmax, chunk):
        for y0 in np.arange(ymin, ymax, chunk):
            x1, y1 = min(x0 + chunk, xmax), min(y0 + chunk, ymax)
            response = requests.get(
                WCS,
                params=[
                    ("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
                    ("COVERAGEID", coverage), ("FORMAT", "image/tiff"),
                    ("SUBSET", f"x({x0},{x1})"), ("SUBSET", f"y({y0},{y1})"),
                ],
                timeout=timeout,
            )
            if response.status_code != 200:
                raise RuntimeError(f"AHN {coverage} {x0},{y0}-{x1},{y1}: HTTP {response.status_code} {response.text[:120]}")
            with MemoryFile(response.content) as mem, mem.open() as ds:
                arr = ds.read(1)
                col = int(round((ds.bounds.left - xmin) / pixel))
                row = int(round((ymax - ds.bounds.top) / pixel))
                out[row:row + arr.shape[0], col:col + arr.shape[1]] = arr
                nodata = ds.nodata if ds.nodata is not None else nodata
    return out, from_origin(xmin, ymax, pixel, pixel), "EPSG:28992", nodata


def write_cog(path: Path, arr: np.ndarray, transform, crs, nodata=-9999.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(
        driver="COG", dtype="float32", count=1, width=arr.shape[1], height=arr.shape[0],
        crs=crs, transform=transform, compress="DEFLATE", predictor=2,
        blocksize=512, overviews="AUTO", nodata=nodata,
    )
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(arr.astype("float32"), 1)
    with rasterio.open(path) as ds:
        layout = ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT")
        if layout != "COG":
            raise RuntimeError(f"{path} is not a COG (layout={layout})")
        return {"width": ds.width, "height": ds.height, "overviews": ds.overviews(1),
                "bounds_wsen": wsen(ds)}


def block_max(arr: np.ndarray, target_px: int) -> np.ndarray:
    """Block-max downsample: preserves buildings and trees instead of point-sampling spikes."""
    step = max(1, int(math.ceil(max(arr.shape) / target_px)))
    h, w = arr.shape
    h2, w2 = (h // step) * step, (w // step) * step
    if h2 == 0 or w2 == 0:
        return arr
    blocks = arr[:h2, :w2].reshape(h2 // step, step, w2 // step, step)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmax(blocks, axis=(1, 3))


def render_preview(arr: np.ndarray, path: Path, max_px: int = 1200):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.cm as cm
    from PIL import Image

    from scipy.ndimage import median_filter
    a = median_filter(block_max(arr.astype("float64"), max_px), size=3)
    finite = np.isfinite(a)
    a = np.where(finite, a, 0.0)
    gy, gx = np.gradient(a)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = math.radians(315), math.radians(45)
    shade = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    shade = np.clip(shade, 0, 1)
    norm = np.clip(a / 12.0, 0, 1)
    rgb = (cm.turbo(norm)[:, :, :3] * (0.55 + 0.45 * shade)[:, :, None] * 255).astype("uint8")
    ground = a < 0.5
    rgb[ground] = (0.90 * 255 * (0.65 + 0.35 * shade[ground])).astype("uint8")[:, None].repeat(3, axis=1)
    Image.fromarray(rgb).save(path)


def render_3d(arr: np.ndarray, path: Path, max_px: int = 220):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from scipy.ndimage import median_filter, gaussian_filter
    a = median_filter(block_max(arr.astype("float64"), max_px), size=3)
    a = np.where(np.isfinite(a), a, 0.0)
    a = gaussian_filter(a, sigma=1.0)
    a = np.clip(a, 0, 15)
    yy, xx = np.mgrid[0:a.shape[0], 0:a.shape[1]]
    fig = plt.figure(figsize=(8, 6), dpi=150)
    ax = fig.add_subplot(111, projection="3d")
    ax.plot_surface(xx, yy, a, cmap="magma", linewidth=0, antialiased=False,
                    rstride=1, cstride=1, vmin=0, vmax=12, shade=True)
    ax.set_zlim(0, 15)
    ax.view_init(elev=35, azim=-55)
    ax.set_box_aspect((1, 1, 0.45))
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    fig.savefig(path, facecolor="white")
    plt.close(fig)


class Catalog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY, source_id TEXT, product TEXT, aoi TEXT,
                capture TEXT, licence TEXT, attribution TEXT, gsd_m REAL,
                crs TEXT, bounds_wsen TEXT, path TEXT, bytes INTEGER, sha256 TEXT,
                width INTEGER, height INTEGER, dtype TEXT, nodata REAL,
                compression TEXT, created_at TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS asset_index USING rtree(id, minx, maxx, miny, maxy);
        """)
        self.conn.commit()

    def add(self, row):
        for old in self.conn.execute(
            "SELECT id FROM assets WHERE source_id=? AND product=? AND aoi=?",
            (row["source_id"], row["product"], row["aoi"]),
        ).fetchall():
            self.conn.execute("DELETE FROM asset_index WHERE id=?", (old[0],))
            self.conn.execute("DELETE FROM assets WHERE id=?", (old[0],))
        cur = self.conn.execute(
            "INSERT INTO assets (source_id,product,aoi,capture,licence,attribution,gsd_m,crs,"
            "bounds_wsen,path,bytes,sha256,width,height,dtype,nodata,compression,created_at)"
            " VALUES (:source_id,:product,:aoi,:capture,:licence,:attribution,:gsd_m,:crs,"
            ":bounds_wsen,:path,:bytes,:sha256,:width,:height,:dtype,:nodata,:compression,:created_at)",
            row,
        )
        self.conn.execute(
            "INSERT INTO asset_index (id,minx,maxx,miny,maxy) VALUES (?,?,?,?,?)",
            (cur.lastrowid, *row["rtree"]),
        )
        self.conn.commit()

    def query(self, bbox):
        """Assets whose WGS84 bounds intersect the bbox."""
        west, south, east, north = bbox
        rows = self.conn.execute(
            "SELECT a.* FROM assets a JOIN asset_index i ON a.id=i.id"
            " WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? ORDER BY a.aoi, a.product",
            (west, east, south, north),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()


SWISS_COLLECTIONS = {
    "dsm": ("ch.swisstopo.swissalti3d", "swissALTI3D surface model 0.5 m"),
}
# swissSURFACE3D is itself a DSM (the publisher states so), so there is no open
# bare-earth raster to difference against, and an opening-derived ground is biased on
# sloped terrain. Switzerland therefore ships the absolute DSM only, no nDSM.
SWISS_ASSET_SUFFIX = "0.5_2056_5728.tif"


def merge_stac(collection: str, bbox, res: float = 0.5, timeout: float = 120.):
    """Merge every STAC tile of a collection whose bbox intersects the AOI onto one grid."""
    import shutil
    import tempfile
    from rasterio.merge import merge as rio_merge

    env = {"AWS_NO_SIGN_REQUEST": "YES", "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
           "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif", "GDAL_HTTP_TIMEOUT": "120",
           "VSI_CACHE": "TRUE", "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES"}
    url = f"https://data.geo.admin.ch/api/stac/v0.9/collections/{collection}/items"
    response = requests.get(url, params={"bbox": ",".join(str(v) for v in bbox), "limit": 200}, timeout=timeout)
    response.raise_for_status()
    features = [f for f in response.json().get("features", [])
                if any(a.get("href", "").endswith(SWISS_ASSET_SUFFIX) for a in f.get("assets", {}).values())]
    if not features:
        raise RuntimeError(f"no {collection} tiles intersect {bbox}")
    xs, ys = warp_transform("EPSG:4326", "EPSG:2056", [bbox[0], bbox[2]], [bbox[1], bbox[3]])
    left, right = min(xs), max(xs)
    bottom, top = min(ys), max(ys)
    with rasterio.Env(**env):
        datasets = [rasterio.open(a["href"]) for f in features
                    for a in f["assets"].values() if a["href"].endswith(SWISS_ASSET_SUFFIX)]
        try:
            arr, transform = rio_merge(datasets, bounds=(left, bottom, right, top), res=(res, res),
                                       indexes=[1], nodata=None)
        finally:
            for ds in datasets:
                ds.close()
    capture = sorted({(f["properties"].get("datetime") or "")[:10] for f in features if f["properties"].get("datetime")})
    return arr[0].astype("float64"), transform, (capture[0] if capture else "swiss")


def write_local_cog(array: np.ndarray, transform, crs: str, local: Path) -> dict:
    profile = dict(driver="COG", dtype="float32", count=1, width=array.shape[1], height=array.shape[0],
                   crs=crs, transform=transform, compress="DEFLATE", predictor=2,
                   blocksize=512, overviews="AUTO")
    with rasterio.open(local, "w", **profile) as ds:
        ds.write(array.astype("float32"), 1)
    with rasterio.open(local) as ds:
        if ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG":
            raise RuntimeError(f"{local} is not a COG")
        return {"width": ds.width, "height": ds.height, "overviews": ds.overviews(1), "bounds": ds.bounds}


def fetch_swissalti(name: str, bbox, out_root: Path, catalog: Catalog):
    import shutil
    import tempfile

    dsm, transform, dsm_capture = merge_stac(SWISS_COLLECTIONS["dsm"][0], bbox)
    out_root.mkdir(parents=True, exist_ok=True)
    base = out_root / name
    base.mkdir(parents=True, exist_ok=True)
    written = []
    for product, array, capture in (("dsm", dsm, dsm_capture),):
        work = Path(tempfile.mkdtemp(prefix=f"{product}-")) / f"{product}_05m.tif"
        data = np.where(np.isfinite(array), array, -9999.0) if product != "ndsm" else np.where(np.isfinite(array), array, -9999.0)
        info = write_local_cog(data, transform, "EPSG:2056", work)
        path = base / f"{product}_05m.tif"
        shutil.copyfile(work, path)
        raw = path.read_bytes()
        with rasterio.open(path) as ds:
            if ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG":
                raise RuntimeError(f"{path} is not a COG after copy")
            lons, lats = warp_transform("EPSG:2056", "EPSG:4326",
                                        [ds.bounds.left, ds.bounds.right], [ds.bounds.bottom, ds.bounds.top])
            width, height = ds.width, ds.height
        catalog.add({
            "source_id": "swissalti3d", "product": product, "aoi": name,
            "capture": capture, "licence": "Open data (swisstopo)", "attribution": "swisstopo",
            "gsd_m": 0.5, "crs": "EPSG:2056",
            "bounds_wsen": json.dumps([min(lons), min(lats), max(lons), max(lats)]),
            "path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "width": width, "height": height, "dtype": "float32", "nodata": -9999.0,
            "compression": "DEFLATE predictor=2", "created_at": now(),
            "rtree": (min(lons), max(lons), min(lats), max(lats)),
        })
        written.append((product, path, len(raw), info, capture))
        print(f"  {product}: {width}x{height} {len(raw)/1e6:.1f} MB overviews={info['overviews']} "
              f"capture={capture}", flush=True)
    print("  (no nDSM for Switzerland: no open bare-earth model)", flush=True)
    return written


def run_aoi(name: str, bbox, out_root: Path, catalog: Catalog):
    print(f"{name}: fetching AHN DSM/DTM 0.5 m", flush=True)
    dsm, transform, crs, nodata = fetch_coverage("dsm_05m", bbox)
    dtm, _, _, _ = fetch_coverage("dtm_05m", bbox)
    if dsm.shape != dtm.shape:
        raise RuntimeError(f"DSM/DTM shape mismatch {dsm.shape} vs {dtm.shape}")
    sentinel = (np.abs(dsm) > 1e6) | (np.abs(dtm) > 1e6)
    dsm = np.where(sentinel, np.nan, dsm)
    dtm = np.where(sentinel, np.nan, dtm)
    valid = np.isfinite(dsm) & np.isfinite(dtm)
    ndsm = np.full(dsm.shape, np.nan, dtype="float32")
    ndsm[valid] = dsm[valid] - dtm[valid]
    ndsm[ndsm < 0] = np.nan
    base = out_root / name
    results = []
    for product, arr in (("dsm", dsm), ("dtm", dtm), ("ndsm", ndsm)):
        path = base / f"{product}_05m.tif"
        data = np.where(np.isfinite(arr), arr, -9999.0).astype("float32")
        info = write_cog(path, data, transform, crs, -9999.0)
        raw = path.read_bytes()
        results.append((product, path, len(raw), hashlib.sha256(raw).hexdigest(), info))
        print(f"  {product}: {info['width']}x{info['height']} {len(raw)/1e6:.1f} MB "
              f"overviews={info['overviews']}", flush=True)
    render_preview(ndsm, base / "ndsm-hillshade.png")
    render_3d(ndsm, base / "ndsm-3d.png")
    print(f"  rendered ndsm-hillshade.png and ndsm-3d.png", flush=True)
    for product, path, size, digest, info in results:
        b = info["bounds_wsen"]
        catalog.add({
            "source_id": "ahn", "product": product, "aoi": name,
            "capture": "AHN latest (DSM/DTM 0.5 m)", "licence": LICENCE,
            "attribution": ATTRIBUTION, "gsd_m": 0.5, "crs": str(crs),
            "bounds_wsen": json.dumps([round(v, 8) for v in b]), "path": str(path),
            "bytes": size, "sha256": digest, "width": info["width"], "height": info["height"],
            "dtype": "float32", "nodata": -9999.0, "compression": "DEFLATE predictor=2",
            "created_at": now(),
            "rtree": (b[0], b[2], b[1], b[3]),
        })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--aoi", choices=sorted(AOIS) + ["all"], default="utrecht")
    args = parser.parse_args(argv)
    names = sorted(AOIS) if args.aoi == "all" else [args.aoi]
    catalog = Catalog(args.out / "catalog.db")
    for name in names:
        if name in ("zurich", "interlaken"):
            fetch_swissalti(name, AOIS[name], args.out, catalog)
            continue
        run_aoi(name, AOIS[name], args.out, catalog)
    catalog.close()
    print(f"catalog: {args.out / 'catalog.db'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
