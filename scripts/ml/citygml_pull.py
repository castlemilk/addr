"""Pull swissBUILDINGS3D tiles and build roof label planes for an AOI (citygml-pull-v1).

For each CityGML tile that intersects the AOI this downloads the public open-data
`.citygml.zip`, extracts it, runs the label extraction onto the DSM grid, then deletes
the extracted XML (each tile is ~1.7 GB of GML). The per-tile rasters are merged onto
the shared grid, so the result is one set of label planes per AOI.

    python -m scripts.ml.citygml_pull --aoi zurich \
        --grid /Volumes/gamma-systems-2/addr-imagery/elevation/zurich/dsm_05m.tif \
        --out /Volumes/gamma-systems-2/addr-imagery/labels/zurich
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np
import rasterio
import requests

STAC = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissbuildings3d_3_0/items"
LAYERS = ("roofmask", "roofheight", "roofz", "roofpitch", "roofclass", "roofsid")


def tiles_for(bbox, limit: int = 40) -> list[dict]:
    response = requests.get(STAC, params={"bbox": ",".join(str(v) for v in bbox), "limit": limit}, timeout=120)
    response.raise_for_status()
    out = []
    for feature in response.json().get("features", []):
        hrefs = [a["href"] for a in feature.get("assets", {}).values() if a["href"].endswith(".citygml.zip")]
        if hrefs:
            out.append({"id": feature["id"], "href": hrefs[0],
                        "datetime": (feature["properties"].get("datetime") or "")[:10],
                        "bbox": feature.get("bbox")})
    return out


def fetch_zip(href: str, target: Path, timeout: float = 900.) -> Path:
    with requests.get(href, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
    return target


def run_tile(gml: Path, grid: Path, out: Path) -> None:
    subprocess.run([sys.executable, "-u", "-m", "scripts.ml.citygml_labels",
                    "--gml", str(gml), "--grid", str(grid), "--out", str(out)],
                   check=True)


def merge(tile_dirs: list[Path], out: Path, grid: Path) -> dict:
    with rasterio.open(grid) as ds:
        transform, width, height = ds.transform, ds.width, ds.height
        crs = ds.crs
    arrays = {}
    for layer in LAYERS:
        with rasterio.open(tile_dirs[0] / f"{layer}.tif") as ds:
            arrays[layer] = ds.read(1)
    for tile_dir in tile_dirs[1:]:
        for layer in LAYERS:
            with rasterio.open(tile_dir / f"{layer}.tif") as ds:
                data = ds.read(1)
            mask = arrays["roofmask"]
            if layer == "roofmask":
                arrays[layer] = np.where(data > 0, 1, mask)
            elif layer == "roofsid":
                arrays[layer] = np.where(data > 0, data, arrays[layer])
            else:
                if data.dtype.kind == "f":
                    take = np.isfinite(data) & (data != -9999)
                else:
                    take = data > 0
                arrays[layer] = np.where(take, data, arrays[layer])
    out.mkdir(parents=True, exist_ok=True)
    for layer, array in arrays.items():
        path = out / f"{layer}.tif"
        dtype = "uint32" if layer == "roofsid" else ("uint8" if layer in ("roofmask", "roofclass") else "float32")
        nodata = 0 if dtype.startswith("uint") else -9999.0
        profile = dict(driver="COG", dtype=dtype, count=1, width=width, height=height,
                       crs=crs, transform=transform,
                       compress="DEFLATE", predictor=2, blocksize=512, overviews="AUTO", nodata=nodata)
        data = array
        if data.dtype.kind == "f":
            data = np.where(np.isfinite(data), data, nodata)
        with rasterio.open(path, "w", **profile) as ds:
            ds.write(data.astype(dtype), 1)
        with rasterio.open(path) as ds:
            if ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG":
                raise RuntimeError(f"{path} is not a COG")
    return {"layers": list(arrays), "pixels": {k: int(np.count_nonzero(v)) for k, v in arrays.items()}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--aoi", required=True, help="named AOI or W,S,E,N")
    parser.add_argument("--grid", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)
    if args.aoi.count(",") == 3:
        bbox = tuple(float(v) for v in args.aoi.split(","))
    else:
        from .elevation_spike import AOIS
        bbox = AOIS[args.aoi]
    items = tiles_for(bbox, args.limit)
    print(f"{len(items)} CityGML tiles intersect {bbox}", flush=True)
    started = time.monotonic()
    tile_dirs = []
    for item in items:
        work = Path(tempfile.mkdtemp(prefix="b3d-"))
        print(f"  {item['id']} ({item['datetime']})", flush=True)
        try:
            archive = fetch_zip(item["href"], work / "tile.zip")
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(work)
            archive.unlink()
            gml = next(work.glob("*.gml"))
            tile_out = work / "labels"
            run_tile(gml, args.grid, tile_out)
            tile_dirs.append(tile_out)
        except Exception as exc:
            print(f"    FAILED: {exc}", flush=True)
        finally:
            for item_gml in work.glob("*.gml"):
                item_gml.unlink(missing_ok=True)
    if not tile_dirs:
        raise SystemExit("no tiles were parsed")
    # keep the tile outputs, drop the bulky archives
    merged = merge(tile_dirs, args.out, args.grid)
    for tile_dir in tile_dirs:
        shutil.rmtree(tile_dir.parent, ignore_errors=True)
    summary = {"format": "citygml-pull-v1", "aoi": args.aoi, "bbox": list(bbox),
               "tiles": [i["id"] for i in items], "tile_count": len(tile_dirs),
               "seconds": round(time.monotonic() - started, 1), **merged}
    (args.out / "labels.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
