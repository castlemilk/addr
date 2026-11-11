import sqlite3

import numpy as np
import pytest

pytest.importorskip("rasterio")
from scripts.ml import open_imagery as oi


def test_bbox_3857_and_back():
    bbox = (4.860, 45.750, 4.910, 45.795)
    xmin, ymin, xmax, ymax = oi.bbox_3857(bbox)
    west, south, east, north = oi.bbox_4326(xmin, ymin, xmax, ymax)
    assert abs(west - bbox[0]) < 1e-6 and abs(south - bbox[1]) < 1e-6
    assert abs(east - bbox[2]) < 1e-6 and abs(north - bbox[3]) < 1e-6


def test_alpha_composites_on_white():
    arr = np.zeros((4, 2, 2), dtype=np.uint8)
    arr[0] = 10
    arr[1] = 20
    arr[2] = 30
    arr[3] = 128
    rgb = oi.to_rgb(arr)
    assert rgb.shape == (3, 2, 2)
    assert rgb[0, 0, 0] > 10 and rgb[0, 0, 0] < 255


def test_catalog_spatial_query(tmp_path):
    catalog = oi.Catalog(tmp_path / "catalog.db")
    row = {
        "source_id": "naip", "title": "NAIP", "licence": "Public domain", "attribution": "USGS",
        "capture": "2026", "gsd_m": 0.6, "pixel_m": 0.78, "crs": "EPSG:3857",
        "bounds_wsen": "[-105.115,39.79,-105.065,39.835]", "path": "/tmp/a.tif",
        "bytes": 1, "sha256": "x", "width": 10, "height": 10, "compression": "JPEG q90",
        "created_at": "now", "rtree": (-105.115, -105.065, 39.79, 39.835),
    }
    catalog.replace(row)
    assert len(catalog.query((-105.10, 39.80, -105.09, 39.81))) == 1
    assert len(catalog.query((10.0, 10.0, 11.0, 11.0))) == 0
    catalog.close()
