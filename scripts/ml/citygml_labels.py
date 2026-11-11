"""Roof plane labels from CityGML (citygml-labels-v1).

swissBUILDINGS3D 3.0 (open data, swisstopo) is CityGML in EPSG:2056. Building heights
live on `bldg:measuredHeight` (metres above ground), roof types on the roof surface
(`OBJEKTART`), and the geometry is inline 3D `gml:posList` under each `bldg:RoofSurface`.
This module streams the XML and rasterises label planes onto the grid of an existing DSM
raster, so the labels align pixel for pixel with the ortho and the height data:

    roofmask.tif    uint8   1 = roof surface pixel
    roofheight.tif  float32 metres above ground (bldg:measuredHeight, measured)
    roofz.tif      float32 absolute roof elevation (metres, from the geometry)
    roofpitch.tif  float32 roof pitch estimate in degrees
    roofclass.tif  uint8   building usage class (see CLASSES); 0 = no roof
    roofsid.tif    uint32  per-building instance id (1..N) for instance segmentation

Pitch is an estimate per building: the roof height range over half the shorter footprint
side. It is not a fitted plane. Only roof surfaces are rasterised, so walls are excluded.

    python -m scripts.ml.citygml_labels --gml tile.gml \
        --grid /Volumes/gamma-systems-2/addr-imagery/elevation/zurich/dsm_05m.tif \
        --out /Volumes/gamma-systems-2/addr-imagery/labels/zurich
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.transform import Affine
from shapely.geometry import Polygon

# swissBUILDINGS3D 3.0 carries no roof-form attribute, so the class plane is the
# building usage published in OBJEKTART (German), not a roof type.
CLASSES = {
    0: "no-roof",
    1: "single-family building",
    2: "high-rise",
    3: "open building (carport, shed)",
    4: "under construction",
    5: "religious building",
    6: "greenhouse",
    7: "chimney / vent shaft",
    8: "storage tank",
    9: "tower",
    10: "chapel",
    11: "underground building",
    12: "other / not visible",
}
TYPE_TO_CLASS = {
    "gebäude einzelhaus": 1, "gebaeude einzelhaus": 1, "wohnhaus": 1,
    "hochhaus": 2, "offenes gebaeude": 3, "im bau": 4,
    "sakrales gebaeude": 5, "treibhaus": 6,
    "lueftungsschacht": 7, "hochkamin": 7, "lagertank": 8,
    "turm": 9, "sakraler turm": 9, "kapelle": 10,
    "unterirdisches gebaeude": 11, "gebäude unsichtbar": 12, "gebaeude unsichtbar": 12,
}
GML = "http://www.opengis.net/gml"
BLDG = "http://www.opengis.net/citygml/building/2.0"
GEN = "http://www.opengis.net/citygml/generics/2.0"
BATCH = 20000


def local(tag: str) -> str:
    return tag.split("}")[-1]


def class_of(roof_type: str | None) -> int:
    if not roof_type:
        return 8
    return TYPE_TO_CLASS.get(roof_type.strip().lower(), 8)


def ring_points(poslist: str):
    values = [float(v) for v in poslist.split()]
    return [tuple(values[i:i + 3]) for i in range(0, len(values) - 2, 3)]


def roof_type_of(surface) -> str | None:
    for attr in surface.iter(f"{{{GEN}}}stringAttribute"):
        if attr.get("name") == "OBJEKTART":
            value = attr.find(f"{{{GEN}}}value")
            if value is not None and value.text:
                return value.text
    return None


def measured_height(element) -> float | None:
    node = element.find(f"{{{BLDG}}}measuredHeight")
    if node is None or not node.text:
        return None
    try:
        return float(node.text)
    except ValueError:
        return None


def surface_polygons(surface):
    """Yield (polygon, z_min, z_max) for each polygon of one roof surface."""
    for poly in surface.iter(f"{{{GML}}}Polygon"):
        exterior = None
        holes = []
        for child in poly:
            name = local(child.tag)
            if name not in ("exterior", "interior"):
                continue
            pos = child.find(f".//{{{GML}}}posList")
            if pos is None or not pos.text:
                continue
            points = ring_points(pos.text)
            if len(points) < 3:
                continue
            if name == "exterior" and exterior is None:
                exterior = points
            else:
                holes.append(points)
        if not exterior:
            continue
        try:
            geometry = Polygon([(p[0], p[1]) for p in exterior],
                               [[(p[0], p[1]) for p in hole] for hole in holes])
        except Exception:
            continue
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty:
            continue
        zs = [p[2] for p in exterior]
        yield geometry, min(zs), max(zs)


def attr_value(element, kind: str, name: str) -> str | None:
    for attr in element.iter(f"{{{GEN}}}{kind}"):
        if attr.get("name") == name:
            value = attr.find(f"{{{GEN}}}value")
            if value is not None and value.text:
                return value.text
    return None


def attr_double(element, kind: str, name: str) -> float | None:
    text = attr_value(element, kind, name)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def measured_heights(element) -> list[float]:
    out = []
    for node in element.iter(f"{{{BLDG}}}measuredHeight"):
        if node.text:
            try:
                out.append(float(node.text))
            except ValueError:
                pass
    return out


def normal_z(points) -> float:
    """Newell normal z of a 3D ring: near 1 = horizontal (roof), near 0 = vertical (wall)."""
    nz = 0.0
    n = len(points)
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        nz += (a[0] - b[0]) * (a[1] + b[1])
    area = 0.0
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        area += a[0] * b[1] - b[0] * a[1]
    area *= 0.5
    if abs(area) < 1e-9:
        return 0.0
    return nz / (2.0 * area)


def solid_roof_polygons(element):
    """Roof-like polygons (near-horizontal) from a lod2Solid, walls dropped."""
    for solid in element.iter(f"{{{BLDG}}}lod2Solid"):
        for poly in solid.iter(f"{{{GML}}}Polygon"):
            ring = poly.find(f"{{{GML}}}exterior/{{{GML}}}LinearRing/{{{GML}}}posList")
            if ring is None or not ring.text:
                continue
            points = ring_points(ring.text)
            if len(points) < 3 or abs(normal_z(points)) < 0.5:
                continue
            try:
                geometry = Polygon([(p[0], p[1]) for p in points])
            except Exception:
                continue
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            if geometry.is_empty:
                continue
            zs = [p[2] for p in points]
            yield geometry, min(zs), max(zs)

def write_raster(path: Path, array: np.ndarray, transform: Affine, dtype: str, nodata):
    profile = dict(driver="COG", dtype=dtype, count=1, width=array.shape[1], height=array.shape[0],
                   crs="EPSG:2056", transform=transform, compress="DEFLATE", predictor=2,
                   blocksize=512, overviews="AUTO", nodata=nodata)
    data = array
    if data.dtype.kind == "f":
        # NaN and the declared nodata are the same thing on disk; keep one representation
        data = np.where(np.isfinite(data), data, nodata)
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(data.astype(dtype), 1)
    with rasterio.open(path) as ds:
        if ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG":
            raise RuntimeError(f"{path} is not a COG")
    return path


def main(argv=None) -> int:
    import xml.etree.ElementTree as ET

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gml", type=Path, required=True)
    parser.add_argument("--grid", type=Path, required=True, help="DSM raster defining the output grid")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    with rasterio.open(args.grid) as ds:
        transform, width, height = ds.transform, ds.width, ds.height

    arrays = {
        "mask": np.zeros((height, width), dtype="uint8"),
        "height": np.full((height, width), np.nan, dtype="float32"),
        "z": np.full((height, width), np.nan, dtype="float32"),
        "pitch": np.full((height, width), np.nan, dtype="float32"),
        "class": np.zeros((height, width), dtype="uint8"),
        "sid": np.zeros((height, width), dtype="uint32"),
    }
    batches = {key: [] for key in arrays}
    counts = Counter()
    buildings = 0
    typed = 0
    negative_heights = 0

    def flush():
        if not batches["mask"]:
            return
        for key, dtype, fill in (("mask", "uint8", 0), ("height", "float32", np.nan),
                                 ("z", "float32", np.nan), ("pitch", "float32", np.nan),
                                 ("class", "uint8", 0), ("sid", "uint32", 0)):
            arrays[key] = rasterize(batches[key], out_shape=(height, width), transform=transform,
                                    fill=fill, dtype=dtype)
        for key in batches:
            batches[key] = []

    for event, element in ET.iterparse(args.gml, events=("end",)):
        if local(element.tag) != "Building":
            continue
        own = element.find(f"{{{BLDG}}}boundedBy/{{{BLDG}}}RoofSurface")
        polygons = []
        z_min_all = None
        z_max_all = None
        if own is not None:
            for geometry, z_min, z_max in surface_polygons(own):
                polygons.append(geometry)
                z_min_all = z_min if z_min_all is None else min(z_min_all, z_min)
                z_max_all = z_max if z_max_all is None else max(z_max_all, z_max)
        if not polygons:
            for geometry, z_min, z_max in solid_roof_polygons(element):
                polygons.append(geometry)
                z_min_all = z_min if z_min_all is None else min(z_min_all, z_min)
                z_max_all = z_max if z_max_all is None else max(z_max_all, z_max)
        if not polygons:
            element.clear()
            continue
        buildings += 1
        roof_type = attr_value(element, "stringAttribute", "OBJEKTART")
        if roof_type:
            typed += 1
        roof_class = class_of(roof_type)
        heights = measured_heights(element)
        height_value = max(heights) if heights else None
        dach_max = attr_double(element, "doubleAttribute", "DACH_MAX")
        dach_min = attr_double(element, "doubleAttribute", "DACH_MIN")
        ground_point = attr_double(element, "doubleAttribute", "GELAENDEPUNKT")
        if height_value is None and dach_max is not None and ground_point is not None:
            height_value = dach_max - ground_point
        if height_value is None and dach_max is not None and dach_min is not None:
            height_value = dach_max - dach_min
        if height_value is not None and height_value < 0:
            # A roof below its own ground point means the unit has no valid ground
            # reference ("Im Bau" units); clamp and count it rather than shipping a
            # negative height as if it were a measurement.
            negative_heights += 1
            height_value = 0.0
        instance = buildings
        xs = [p[0] for geometry in polygons for p in geometry.exterior.coords]
        ys = [p[1] for geometry in polygons for p in geometry.exterior.coords]
        run = max(1.0, min(max(xs) - min(xs), max(ys) - min(ys)) / 2.0)
        rise = (dach_max - dach_min) if (dach_max is not None and dach_min is not None) else ((z_max_all or 0) - (z_min_all or 0))
        pitch = math.degrees(math.atan2(rise, run))
        counts[CLASSES[roof_class]] += 1
        for geometry in polygons:
            batches["mask"].append((geometry, 1))
            batches["height"].append((geometry, height_value if height_value is not None else np.nan))
            batches["z"].append((geometry, dach_max if dach_max is not None else z_max_all))
            batches["pitch"].append((geometry, pitch))
            batches["class"].append((geometry, roof_class))
            batches["sid"].append((geometry, instance))
        if len(batches["mask"]) >= BATCH:
            flush()
        element.clear()
    flush()

    args.out.mkdir(parents=True, exist_ok=True)
    write_raster(args.out / "roofmask.tif", arrays["mask"], transform, "uint8", 0)
    write_raster(args.out / "roofheight.tif", arrays["height"], transform, "float32", np.nan)
    write_raster(args.out / "roofz.tif", arrays["z"], transform, "float32", np.nan)
    write_raster(args.out / "roofpitch.tif", arrays["pitch"], transform, "float32", np.nan)
    write_raster(args.out / "roofclass.tif", arrays["class"], transform, "uint8", 0)
    write_raster(args.out / "roofsid.tif", arrays["sid"], transform, "uint32", 0)
    summary = {
        "format": "citygml-labels-v1", "source": str(args.gml), "grid": str(args.grid),
        "classes": CLASSES, "buildings": buildings, "buildings_with_roof_type": typed,
        "roof_type_counts": dict(counts),
        "pixels": {key: int(np.count_nonzero(arrays[key] > 0)) if key in ("mask", "class", "sid")
                   else int(np.isfinite(arrays[key]).sum()) for key in arrays},
        "seconds": round(time.monotonic() - started, 1),
    }
    (args.out / "labels.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
