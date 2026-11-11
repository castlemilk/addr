"""Photo roof layer (layers card 2026-10-17, layer 0): where does the aerial photo itself say this roof is?

Per saved outline: GrabCut on the scene image seeded from the outline (eroded outline = probable roof, a ring 3-6 m outside = probable
background, everything in between left to the photo's colours and edges), on a crop around the roof. Returns the photo-supported roof
region in scene pixels. Microsoft's building footprints (data/elevation/derived/ms-buildings, traced independently from imagery) are a
second, independent roof layer. Lot logic may only remove roof area that NEITHER layer supports. Diagnostics, never labels.
"""
from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform, unary_union

MS = Path('data/elevation/derived/ms-buildings/bbox.geojsonl.gz')


def _mpp(scene):
    return abs(float(np.linalg.det(np.asarray(scene['pixel_to_meters'], dtype=float)[:2]))) ** 0.5


def photo_roof(image_rgb, ring, mpp, iters=4):
    """Photo-supported roof region (shapely, scene px) for one outline; None when the crop is degenerate."""
    roof = Polygon(ring).buffer(0)
    if roof.is_empty or roof.area < 9:
        return None
    pad = 7.0 / mpp
    x0, y0, x1, y1 = roof.bounds
    H, W = image_rgb.shape[:2]
    cx0, cy0 = max(int(x0 - pad), 0), max(int(y0 - pad), 0)
    cx1, cy1 = min(int(x1 + pad) + 1, W), min(int(y1 + pad) + 1, H)
    crop = np.ascontiguousarray(image_rgb[cy0:cy1, cx0:cx1, ::-1])  # BGR for OpenCV
    if crop.shape[0] < 8 or crop.shape[1] < 8:
        return None
    mask = np.full(crop.shape[:2], cv2.GC_PR_BGD, np.uint8)
    off = lambda g: [np.round(np.asarray(p.exterior.coords) - [cx0, cy0]).astype(np.int32) for p in getattr(g, 'geoms', [g]) if p.geom_type == 'Polygon']
    out = roof.buffer(3.0 / mpp)
    fg = np.zeros(crop.shape[:2], np.uint8); cv2.fillPoly(fg, off(roof), 1)
    ring_out = np.zeros(crop.shape[:2], np.uint8); cv2.fillPoly(ring_out, off(out), 1)
    mask[ring_out == 1] = cv2.GC_PR_BGD
    mask[fg == 1] = cv2.GC_PR_FGD
    core = roof.buffer(-1.0 / mpp)
    if not core.is_empty:
        c = np.zeros(crop.shape[:2], np.uint8); cv2.fillPoly(c, off(core), 1); mask[c == 1] = cv2.GC_FGD
    near = np.zeros(crop.shape[:2], np.uint8); cv2.fillPoly(near, off(roof.buffer(6.0 / mpp)), 1)
    mask[near == 0] = cv2.GC_BGD  # everything more than 6 m from the outline (fill the inside, not the frame: a frame has a hole)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(crop, mask, None, bgd, fgd, iters, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return None
    m = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polys = [Polygon((c[:, 0] + [cx0, cy0]).tolist()).buffer(0) for c in cs if len(c) >= 3]
    polys = [p for p in polys if p.area > 0 and p.intersects(roof)]
    return unary_union(polys) if polys else None


@lru_cache(maxsize=1)
def _ms_all():
    from shapely.strtree import STRtree
    geoms = [shape(json.loads(l)['geometry']) for l in gzip.open(MS, 'rt')] if MS.exists() else []
    return geoms, STRtree(geoms) if geoms else None


def ms_roof(scene, ring):
    """Microsoft footprints overlapping this outline, in scene px (shapely), or None."""
    from .parcel_units import _inverse
    from .scene_addresses import image_projection
    geoms, tree = _ms_all()
    if not tree:
        return None
    roof = Polygon(ring).buffer(0)
    inv = _inverse(scene)
    xs = [inv(x, y) for x, y in ((roof.bounds[0], roof.bounds[1]), (roof.bounds[2], roof.bounds[3]))]
    q = box(min(p[0] for p in xs), min(p[1] for p in xs), max(p[0] for p in xs), max(p[1] for p in xs)).buffer(0.0002)
    proj = image_projection(scene)
    hits = [transform(proj, geoms[i]).buffer(0) for i in tree.query(q)]
    hits = [h for h in hits if h.intersects(roof)]
    return unary_union(hits) if hits else None


_IMG = {}


def guarded_bound(scene, ring, parcels, pfi=None, pfis=None):
    """bound_to_lot with the photo layer as guard (layers card 2026-10-17): the single entry point for every lot clip."""
    from .parcel_units import bound_to_lot
    key = scene['id']
    if key not in _IMG:
        _IMG.clear()
        _IMG[key] = cv2.imread(f"data/scenes/{key}/{scene['image_sha256']}.png")[:, :, ::-1].copy()
    return bound_to_lot(scene, ring, parcels, pfi=pfi, pfis=pfis, protect=photo_roof(_IMG[key], ring, _mpp(scene)))
