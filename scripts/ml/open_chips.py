"""Sample training chips from the open COG catalog (open-chips-v1).

The catalog is the lookup; the COG is the image. For a WGS84 bbox this module finds
every intersecting asset, reads fixed-ground-size windows directly from the compressed
COG (no full-file decode), and writes JPEG chips plus a JSONL manifest with source,
capture date, licence, ground/pixel GSD and sha256.

    python -m scripts.ml.open_chips --bbox 5.20,52.07,5.25,52.115 \
        --db /Volumes/gamma-systems-2/addr-imagery/open/catalog.db \
        --out /Volumes/gamma-systems-2/addr-imagery/chips --size-m 512 --stride-m 512 --res-m 0.5

The requested resolution is clamped up to the asset's ground GSD so the sampler never
invents detail by upsampling.

With `--height-db` the sampler also joins the 3D height store, and with `--labels-dir`
the roof label planes, writing one compressed `.npz` beside each chip with the
channels that were found (height, pitch, roofmask, roofclass, roofsid). The manifest
row then lists the channels, so a training loader can pair RGB with its 3D target
without re-deriving the join.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.windows import Window

from .open_imagery import Catalog, bbox_3857, bbox_4326


def window_in(asset_crs, bounds, ds, left, bottom, right, top, px, resampling):
    """Window in an asset's own CRS covering the chip bounds, read at the chip's pixel size."""
    from rasterio.warp import transform as warp_transform
    xs, ys = warp_transform("EPSG:3857", asset_crs, [left, right], [bottom, top])
    col_off = (min(xs) - ds.bounds.left) / ds.transform.a
    row_off = (ds.bounds.top - max(ys)) / abs(ds.transform.e)
    width = (max(xs) - min(xs)) / ds.transform.a
    height = (max(ys) - min(ys)) / abs(ds.transform.e)
    window = Window(col_off=col_off, row_off=row_off, width=width, height=height)
    fill = 0.0 if np.dtype(ds.dtypes[0]).kind in "ui" else np.nan
    # Read the window at native resolution with the no-data masked, then resample the
    # masked array ourselves: letting rasterio resample raw values blends -9999 into
    # roof edges as -9997, which no later mask can undo.
    native = ds.read(1, window=window, boundless=True, fill_value=fill, masked=True)
    native = np.ma.masked_where(native <= -9990.0, native)
    if native.shape != (px, px):
        if resampling == Resampling.nearest:
            rows = (np.arange(px) * native.shape[0] // px).clip(0, native.shape[0] - 1)
            cols = (np.arange(px) * native.shape[1] // px).clip(0, native.shape[1] - 1)
            native = native[rows][:, cols]
        else:
            filled = np.ma.filled(native, np.nan).astype("float64")
            valid = ~np.ma.getmaskarray(native)
            rows = (np.arange(px) * native.shape[0] // px).clip(0, native.shape[0] - 1)
            cols = (np.arange(px) * native.shape[1] // px).clip(0, native.shape[1] - 1)
            picked = np.ascontiguousarray(filled[rows][:, cols])
            picked_valid = np.ascontiguousarray(valid[rows][:, cols])
            if picked_valid.any():
                # fill the holes from the nearest valid native value before smoothing
                from scipy.ndimage import distance_transform_edt
                if not picked_valid.all():
                    holes = distance_transform_edt(~picked_valid, return_distances=False, return_indices=True)
                    picked = np.ascontiguousarray(picked[tuple(holes)])
                    picked_valid = np.ascontiguousarray(picked_valid[tuple(holes)])
                data = np.where(picked_valid, picked, np.nan)
            else:
                data = np.full((px, px), np.nan)
            return data.astype("float32")
    return np.ma.filled(native, fill)


def label_channels(height_assets, labels_dir, left, bottom, right, top, px) -> dict:
    """Height and roof-label planes for one chip, keyed by channel name."""
    from rasterio.warp import transform as warp_transform
    channels = {}
    for asset in height_assets:
        with rasterio.open(asset["path"]) as ds:
            if asset.get("product") == "ndsm":
                data = window_in(ds.crs, None, ds, left, bottom, right, top, px, Resampling.bilinear)
                channels["height"] = data.astype("float32")
            elif asset.get("product") == "dsm":
                data = window_in(ds.crs, None, ds, left, bottom, right, top, px, Resampling.bilinear)
                channels["dsm"] = data.astype("float32")
    if labels_dir:
        for name, dtype in (("roofmask", "uint8"), ("roofheight", "float32"), ("roofz", "float32"),
                            ("roofpitch", "float32"), ("roofclass", "uint8"), ("roofsid", "uint32")):
            path = Path(labels_dir) / f"{name}.tif"
            if not path.exists():
                continue
            with rasterio.open(path) as ds:
                resampling = Resampling.nearest if dtype in ("uint8", "uint32") else Resampling.bilinear
                data = window_in(ds.crs, None, ds, left, bottom, right, top, px, resampling)
                channels[name] = data.astype(dtype)
    return channels


def sample(asset: dict, bbox, out_root: Path, *, size_m: float, stride_m: float, res_m: float,
           quality: int, limit: int, written: int, height_assets=None, labels_dir=None) -> int:
    with rasterio.open(asset["path"]) as ds:
        a = json.loads(asset["bounds_wsen"])
        west, south = max(bbox[0], a[0]), max(bbox[1], a[1])
        east, north = min(bbox[2], a[2]), min(bbox[3], a[3])
        if west >= east or south >= north:
            return written
        xmin, ymin, xmax, ymax = bbox_3857((west, south, east, north))
        lat = (south + north) / 2
        res = max(res_m, float(asset.get("gsd_m") or res_m))
        px = max(1, int(round(size_m / res)))
        step = stride_m / max(0.01, math.cos(math.radians(lat)))
        chip_m = size_m / max(0.01, math.cos(math.radians(lat)))
        cols = max(1, math.ceil((xmax - xmin) / step))
        rows = max(1, math.ceil((ymax - ymin) / step))
        out_dir = out_root / asset["source_id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = out_root / "chips.jsonl"
        for j in range(rows):
            for i in range(cols):
                if limit and written >= limit:
                    return written
                left, bottom = xmin + i * step, ymin + j * step
                right, top = left + chip_m, bottom + chip_m
                if right > ds.bounds.right or top > ds.bounds.top or left < ds.bounds.left or bottom < ds.bounds.bottom:
                    continue
                window = Window(
                    col_off=(left - ds.bounds.left) / ds.transform.a,
                    row_off=(ds.bounds.top - top) / abs(ds.transform.e),
                    width=(right - left) / ds.transform.a,
                    height=(top - bottom) / abs(ds.transform.e),
                )
                arr = ds.read([1, 2, 3], window=window, out_shape=(3, px, px), resampling=Resampling.bilinear)
                wsen = bbox_4326(left, bottom, right, top)
                slug = f"{asset['source_id']}_{wsen[0]:.6f}_{wsen[1]:.6f}_{px}".replace("-", "m").replace(".", "p")
                path = out_dir / f"{slug}.jpg"
                Image.fromarray(arr.transpose(1, 2, 0)).save(path, quality=quality)
                data = path.read_bytes()
                channels = label_channels(height_assets or [], labels_dir, left, bottom, right, top, px)
                labels_path = None
                if channels:
                    labels_path = path.with_suffix(".npz")
                    packed = {}
                    for key, values in channels.items():
                        values = np.ascontiguousarray(values)
                        if values.dtype.kind == "f":
                            values = np.where(values <= -9990, np.nan, values)
                        packed[key] = values
                    np.savez_compressed(labels_path, **packed)
                row = {
                    "chip": str(path), "source_id": asset["source_id"], "capture": asset.get("capture"),
                    "licence": asset.get("licence"), "attribution": asset.get("attribution"),
                    "gsd_m": asset.get("gsd_m"), "res_m": res, "size_m": size_m, "pixels": px,
                    "bounds_wsen": list(wsen), "asset": asset["path"],
                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "labels": str(labels_path) if labels_path else None,
                    "channels": sorted(channels),
                }
                with manifest.open("a") as handle:
                    handle.write(json.dumps(row) + "\n")
                written += 1
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bbox", required=True, type=lambda s: tuple(float(v) for v in s.split(",")))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", default=None, help="only this catalog source_id")
    parser.add_argument("--size-m", type=float, default=512.)
    parser.add_argument("--stride-m", type=float, default=512.)
    parser.add_argument("--res-m", type=float, default=0.5)
    parser.add_argument("--quality", type=int, default=92)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--height-db", type=Path, default=None,
                        help="elevation catalog.db to join height channels from")
    parser.add_argument("--labels-dir", type=Path, default=None,
                        help="directory with roofmask/roofheight/roofclass/roofsid rasters")
    args = parser.parse_args(argv)
    catalog = Catalog(args.db)
    assets = [a for a in catalog.query(args.bbox) if not args.source or a["source_id"] == args.source]
    height_assets = []
    if args.height_db and args.height_db.exists():
        from .elevation_spike import Catalog as ElevationCatalog
        elevation = ElevationCatalog(args.height_db)
        height_assets = elevation.query(args.bbox)
        elevation.close()
    print(f"{len(assets)} assets intersect the bbox")
    written = 0
    for asset in assets:
        before = written
        written = sample(asset, args.bbox, args.out, size_m=args.size_m, stride_m=args.stride_m,
                         res_m=args.res_m, quality=args.quality, limit=args.limit, written=written,
                         height_assets=height_assets, labels_dir=args.labels_dir)
        print(f"  {asset['source_id']}: {written - before} chips")
    print(f"wrote {written} chips to {args.out}")
    catalog.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
