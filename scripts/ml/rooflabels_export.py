"""Export measured roof labels as a dataset-v1 training set (rooflabels-v1).

The swissBUILDINGS3D label planes are measured geometry, not a model's guess: roof
mask, measured height above ground, absolute roof elevation, pitch and a per-building
instance id. This exporter turns them into the training kit's `dataset-v1` layout so
`train.py` and `evaluate.py` read them unchanged:

  images/<window>.png        the ortho chip, area-averaged to the student grid
  labels/<window>.png        uint16: 0 background, 65535 ignore, else the instance index
  train.json / val.json / test.json
                             COCO polygons with per-instance provenance 'swissbuildings3d-3-0',
                             tier T3 (machine), weight 1.0, boundary_reference false
  manifest.json, DATASET_CARD.md

The instance ids come from the measured `roofsid` plane, so the index is stable and
independent of the window grid: the same building keeps its id in every window it
touches. Split by window, ordered by sha256(seed:window id), so no pixel is shared
between splits. The label grid is the DSM grid the label planes live on; the image is
read from the ortho at the same window.

This is a *measured-label* dataset: the labels come from the publisher's survey geometry,
not from a segmentation model, so agreement with the model is evidence about the model,
not a self-consistent loop.

    python -m scripts.ml.rooflabels_export \
        --ortho-db /Volumes/gamma-systems-2/addr-imagery/open/catalog.db \
        --labels /Volumes/gamma-systems-2/addr-imagery/labels/zurich \
        --aoi zurich --out data/reviews/eyes/training/rooflabels-zurich
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform as warp_transform
from rasterio.windows import Window

IGNORE = 65535
STUDENT_M = 0.23595384232187258      # saved scan pixel size (z19 at Mooroolbark)
CATEGORY = {"id": 1, "name": "roof"}
PROVENANCE = "swissbuildings3d-3-0"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def assign_splits(window_ids, seed: int, fractions=(0.15, 0.15)):
    """Window id -> split, ordered by sha256(seed:id). No window is in two splits."""
    ordered = sorted(window_ids, key=lambda w: hashlib.sha256(f"{seed}:{w}".encode()).hexdigest())
    n = len(ordered)
    n_test = round(fractions[0] * n)
    n_val = round(fractions[1] * n)
    return {w: ("test" if i < n_test else "val" if i < n_test + n_val else "train")
            for i, w in enumerate(ordered)}


def ortho_asset(db_path: Path, bbox):
    """The ortho COG whose bounds contain the AOI (the source that matches the labels)."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    west, south, east, north = bbox
    rows = conn.execute(
        "SELECT a.* FROM assets a JOIN asset_index i ON a.id=i.id"
        " WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=?"
        " ORDER BY a.source_id", (west, east, south, north)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def polygon_of(mask: np.ndarray) -> list[list[float]]:
    """The instance's outline as a flat polygon list, from the repo's own vectoriser."""
    from .segmentation import mask_to_polygons
    polygons = mask_to_polygons(mask, min_area_px=4.0, simplify_px=0.75)
    if not polygons:
        return []
    return [[float(x), float(y)] for x, y in polygons[0].exterior.coords]


def export(labels_dir: Path, ortho_rows, aoi: str, out: Path, seed: int, stride_px: int,
           student_m: float) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    (out / "images").mkdir(exist_ok=True)
    (out / "labels").mkdir(exist_ok=True)
    with rasterio.open(labels_dir / "roofsid.tif") as ds:
        sid = ds.read(1)
        labels_transform = ds.transform
        labels_crs = ds.crs
        width, height = ds.width, ds.height
    with rasterio.open(labels_dir / "roofmask.tif") as ds:
        mask = ds.read(1)
    with rasterio.open(labels_dir / "roofheight.tif") as ds:
        roof_height = ds.read(1)
    with rasterio.open(labels_dir / "roofpitch.tif") as ds:
        roof_pitch = ds.read(1)

    ortho = ortho_rows[0] if ortho_rows else None
    if ortho is None:
        raise SystemExit("no ortho asset covers the AOI")

    # window grid over the label raster, in label pixels
    windows = []
    for row in range(0, height, stride_px):
        for col in range(0, width, stride_px):
            windows.append((col, row, min(stride_px, width - col), min(stride_px, height - row)))
    ids = [f"{aoi}-w{col:06d}_{row:06d}" for col, row, _, _ in windows]
    splits = assign_splits(ids, seed)

    coco = {s: {"info": {"version": "rooflabels-v1", "split": s, "seed": seed, "created_at": now()},
                "images": [], "annotations": [], "categories": [CATEGORY]} for s in ("train", "val", "test")}
    counts = Counter()
    written = []
    for (col, row, w, h), window_id in zip(windows, ids):
        sub_mask = mask[row:row + h, col:col + w]
        if not (sub_mask > 0).any():
            continue
        sub_sid = sid[row:row + h, col:col + w]
        sub_height = roof_height[row:row + h, col:col + w]
        sub_pitch = roof_pitch[row:row + h, col:col + w]
        # label image: instance ids, 0 background
        label = np.zeros((h, w), dtype=np.uint16)
        instance_ids = sorted(int(v) for v in np.unique(sub_sid) if v > 0)
        for index, instance in enumerate(instance_ids, start=1):
            sel = sub_sid == instance
            label[sel] = index
            counts[f"_instances_{index}"] += 0
        image_path = out / "images" / f"{window_id}.png"
        label_path = out / "labels" / f"{window_id}.png"
        # the ortho chip on the same ground window
        left, top = labels_transform * (col, row)
        right, bottom = labels_transform * (col + w, row + h)
        xs, ys = warp_transform(labels_crs, "EPSG:3857", [left, right], [top, bottom])
        with rasterio.open(ortho["path"]) as ods:
            win = Window(
                col_off=(min(xs) - ods.bounds.left) / ods.transform.a,
                row_off=(ods.bounds.top - max(ys)) / abs(ods.transform.e),
                width=(max(xs) - min(xs)) / ods.transform.a,
                height=(max(ys) - min(ys)) / abs(ods.transform.e),
            )
            chip = ods.read([1, 2, 3], window=win, out_shape=(3, h, w),
                            resampling=Resampling.bilinear, boundless=True, fill_value=0)
        from PIL import Image
        Image.fromarray(chip.transpose(1, 2, 0)).save(image_path)
        from scipy.ndimage import zoom
        label_img = label
        if (h, w) != (chip.shape[1], chip.shape[2]):
            label_img = np.asarray(zoom(label.astype("float32"), (chip.shape[1] / h, chip.shape[2] / w), order=0)).astype(np.uint16)
        Image.fromarray(label_img).save(label_path)
        split = splits[window_id]
        coco[split]["images"].append({
            "id": window_id, "file_name": f"images/{window_id}.png",
            "width": int(label_img.shape[1]), "height": int(label_img.shape[0]),
            "gsd_m": student_m, "source": ortho["path"], "provenance": PROVENANCE,
        })
        for index, instance in enumerate(instance_ids, start=1):
            sel = label_img == index
            if not sel.any():
                continue
            heights = sub_height[sub_sid == instance]
            heights = heights[np.isfinite(heights) & (heights > -9990)]
            pitches = sub_pitch[sub_sid == instance]
            pitches = pitches[np.isfinite(pitches) & (pitches > -9990)]
            poly = polygon_of(sel)
            if not poly:
                continue
            annotation = {
                "id": len(coco[split]["annotations"]) + 1,
                "image_id": window_id, "category_id": CATEGORY["id"],
                "segmentation": [poly], "area": int(sel.sum()),
                "iscrowd": 0, "ignore": 0,
                "provenance": PROVENANCE, "tier": "T3", "weight": 1.0,
                "boundary_reference": False,
                "measured_height_m": round(float(np.median(heights)), 3) if heights.size else None,
                "roof_pitch_deg": round(float(np.median(pitches)), 2) if pitches.size else None,
                "source_instance_id": instance,
            }
            coco[split]["annotations"].append(annotation)
            counts["_annotations"] += 1
        counts[f"_windows_{split}"] += 1
        written.append(window_id)

    for split in ("train", "val", "test"):
        (out / f"{split}.json").write_text(json.dumps(coco[split], indent=1) + "\n")
    (out / "references.json").write_text(json.dumps([], indent=1) + "\n")
    manifest = {
        "format": "rooflabels-v1", "aoi": aoi, "created_at": now(), "seed": seed,
        "labels_dir": str(labels_dir), "ortho": ortho["path"],
        "label_grid": {"crs": str(labels_crs), "transform": list(labels_transform)[:6],
                       "width": width, "height": height, "cell_px": stride_px},
        "student_m": student_m,
        "windows": len(written), "annotations": counts["_annotations"],
        "splits": {s: counts[f"_windows_{s}"] for s in ("train", "val", "test")},
        "provenance": PROVENANCE,
        "note": ("Labels are measured publisher geometry (roof mask, measured height, pitch, "
                 "instance id), not a segmentation model's output."),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "DATASET_CARD.md").write_text(
        "# rooflabels-v1\n\n"
        f"Area: {aoi}. Labels: swissBUILDINGS3D 3.0 measured roof geometry rasterised on the "
        "0.5 m DSM grid, windowed to the training grid. Provenance per annotation: "
        f"'{PROVENANCE}', tier T3 (machine), weight 1.0, boundary_reference false.\n\n"
        "These labels come from the publisher's survey geometry rather than a model, so "
        "agreement with a model is evidence about the model.\n\n"
        f"Windows: {len(written)}. Annotations: {counts['_annotations']}. "
        f"Splits: {manifest['splits']}. Seed {seed}.\n"
    )
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--ortho-db", type=Path, required=True)
    parser.add_argument("--aoi", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260928)
    parser.add_argument("--stride-px", type=int, default=512, help="label pixels per window")
    parser.add_argument("--student-m", type=float, default=STUDENT_M)
    args = parser.parse_args(argv)
    with rasterio.open(args.labels / "roofsid.tif") as ds:
        b = ds.bounds
        lons, lats = warp_transform(ds.crs, "EPSG:4326", [b.left, b.right], [b.bottom, b.top])
    bbox = (min(lons), min(lats), max(lons), max(lats))
    rows = ortho_asset(args.ortho_db, bbox)
    if not rows:
        raise SystemExit(f"no ortho asset covers {bbox}")
    manifest = export(args.labels, rows, args.aoi, args.out, args.seed, args.stride_px, args.student_m)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
