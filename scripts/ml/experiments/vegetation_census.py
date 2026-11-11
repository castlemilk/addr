"""Read-only vegetation census and sub-type taxonomy probe on the 54 recapture windows (vegetation-classes-2026-10-08).

Reviewer (chat, 8 Oct ~08:45 AEDT): "can you classify vegetation further to: grass, hedge, tree etc".

Today a scene object has kind `tree` (SAM3 prompt 'tree canopy') or `vegetation` (prompt 'grass'); hedge / bush / shrub
are descriptive labels only (.opencode/skills/addr-roof-tracing/references/vegetation.md). This script measures every live
tree / vegetation object of the review layer and applies a fixed, documented rule that proposes a sub-type. Every figure is
a count or a geometry / photometry statistic; nothing is accuracy; nothing is promoted; nothing under data/scenes is
written; no inference (no SAM3, no depth model).

Population: the 54 windows of data/reviews/eyes/recapture/windows-2026-10-01.jsonl (window_scene_id). Layers:
"review" = data/scenes/<id>/review.json objects that are live (status not rejected / superseded) - measured;
"model" = data/scenes/<id>/latest.json objects - counted only. Frame = <scene>/<image_sha256>.png; rings in frame pixels;
metres through the frame's pixel_to_meters (conformal, so one metres-per-pixel scale).

Per object (mask = the ring raster minus holes):
  area_m2, eq_diam_m (2 sqrt(A / pi)), mrr_width_m / mrr_length_m (short / long side of the minimum rotated rectangle),
  elongation (long / short), compactness (4 pi A / P^2 on the polygon), solidity (A / convex hull A),
  frame_edge_m (perimeter within 1.5 px of the image border), clipped_by_frame (field present);
  median Lab (sRGB D65), hue_deg = atan2(b*, a*), chroma; ExG median and green_share (ExG >= t_g);
  texture = local std of luminance in a 0.6 m window (shadow_census.frame_features), median over the mask eroded by
  half a window (falls back to the full mask under 20 px); shadow_share / crown_share = pixel classes of the tree-shadow
  programme's calibrated rule (shadow_census.classify, thresholds from its calibration.json);
  ring_shadow_share = shadow-class share of a 0.2-1.5 m outer ring (pixels of live buildings excluded), direction-free;
  lee_contrast / lee_shadow_share / sun_shadow_share = the cast-shadow cue with the per-window shadow bearing of the
  tree-shadow programme's sun.json: the mask shifted 0.1-0.8 m along the bearing (lee band) and against it (sun band),
  pixels of live buildings and live tree-kind objects left out; lee_contrast = (median lee lum - median sun lum) / median
  sun lum; casts_shadow = lee_contrast <= -0.2 AND lee_shadow_share - sun_shadow_share >= 0.1 (shadow-class pixels);
  soil_share = brown non-green non-shadow pixels (Lab hue 40-80, L 40-85);
  lab_L_rel = median L* minus the frame's median L* of green pixels;
  building_overlap / paved_overlap = share of the mask under live building / road+driveway+footpath objects;
  ndsm_p50 / ndsm_p90 = null: no nDSM covers any window (see coverage in census.json).

Sub-type rule (RULE dict, v4; first match wins, order = CLASSES). Design values fixed from domain sizes plus the
distribution of reviewer-confirmed tree / vegetation objects, then revised three times after by-eye reads of the renders
(v1 -> v4, each change commented at RULE); NOT tuned for accuracy and NOT validated against labels. unique_estimate in
census.json folds objects the overlapping windows share (centroid within 2 m, area ratio <= 2).

Run from the repo root:
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_census measure   # rows.jsonl, coverage.json
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_census classify  # census.json, tables.md
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.vegetation_census render    # renders/<class>/*, contact sheet
Outputs ONLY under data/reviews/eyes/experiments/vegetation-classes-2026-10-08/census/ (+ heartbeat lines in
.run/session-2026-10-08-cuts-veg/veg/progress.txt).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from . import shadow_census as sc

ROOT = Path(__file__).resolve().parents[3]
SCENES = ROOT/'data/scenes'
WINDOWS_FILE = ROOT/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
OUT_DIR = ROOT/'data/reviews/eyes/experiments/vegetation-classes-2026-10-08/census'
RUN_DIR = ROOT/'.run/session-2026-10-08-cuts-veg/veg'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SHADOW_CODE = ROOT/'scripts/ml/experiments/shadow_census.py'
SHADOW_CALIBRATION = ROOT/'data/reviews/eyes/experiments/tree-shadow-2026-10-08/census/calibration.json'
SUN_FILE = ROOT/'data/reviews/eyes/experiments/tree-shadow-2026-10-08/census/sun.json'
LEE_M = (0.1, 0.8)      # lee / sun band: pixels outside the mask reached by shifting it 0.1-0.8 m along / against the shadow bearing
LINZ_CATALOG = ROOT/'data/elevation/linz-chc-1m/christchurch_2024-2025/catalog.json'
HEIGHTS_SUMMARY = ROOT/'data/reviews/eyes/heights/summary.json'
DEPTH_DIR = ROOT/'data/elevation/depth'
VEG_KINDS = ('tree', 'vegetation')
PAVED = ('road', 'driveway', 'footpath')
LIVE_EXCLUDED = ('rejected', 'superseded')
RING_M = (0.2, 1.5)
FRAME_EDGE_PX = 1.5
MIN_INNER_PX = 20

# Sub-type rule v4, first match wins. Units: m, m2, shares 0..1, texture = std of luminance (0..1) in a 0.6 m window,
# L_rel = object median L* minus the frame's median L* of green pixels. v1 (first by-eye read, notes.md) caught narrow lawn
# strips as hedges, dark-frame lawns as trees, shadowed crowns as garden beds and specks as shrubs; v2 changes are marked.
# v3 (second by-eye read): a strip is a hedge only when it casts a shadow (lee_contrast, sun bearing from the tree-shadow
# programme's sun.json); a clipped hedge is smooth, so the strip test now runs before the lawn test.
# v4 (third by-eye read): lee darkness alone also fires on lawn strips between a pale footpath and a dark road or beside a
# fence / wall; a cast shadow must also be shadow-class pixels (lee_shadow_share - sun_shadow_share >= cast_shadow_min).
# v5 tried and reverted (fourth by-eye read): leaving out the zone each live building shades did not clear the two grass
# strips beside unoutlined buildings and emptied the lee band of a true hedge between buildings (notes.md).
RULE = {
    'speck_area_max': 1.0,          # v2: under 1 m2 -> speck (the editor's outline-hints tiny_area_for_kind for tree / vegetation)
    'not_veg_green_max': 0.25,      # green_share below this AND hue outside the lawn band -> not green (bare, leafless, paving)
    'shadow_dominant_min': 0.5,     # shadow_share at or above this -> mostly cast shadow
    'on_roof_min': 0.5,             # building_overlap at or above this -> foliage over (or misread as) roof
    'lawn_texture_max': 0.055,      # v2 (0.035 -> 0.055): smooth ground cover; confirmed trees with L_rel >= 0 all >= 0.055
    'lawn_L_rel_min': 0.0,          # ...and at least as light as the frame's median green pixel (lawns are lit; crowns self-shade)
    'lawn_smooth_texture_max': 0.035,  # v2: or very smooth ...
    'lawn_smooth_L_rel_min': -5.0,     # ...and not much darker than the frame's green (a dense dark crown stays a crown)
    'lawn_ring_shadow_max': 0.15,   # ...and little cast shadow around it
    'lawn_hue': (85.0, 200.0),      # Lab hue band: green to yellow-green (dry lawn included), not brown soil / mulch
    'sliver_frame_share_min': 0.5,  # frame-border contact >= half the long side of an elongated object -> a frame-cut sliver
    'hedge_width': (0.5, 4.0),      # minimum-rotated-rectangle short side (a trimmed hedge or screen planting)
    'hedge_length_min': 4.0,
    'hedge_elongation_min': 3.0,
    'hedge_solidity_min': 0.6,      # a strip, not an L-shaped canopy mass or a lacy outline
    'hedge_lee_max': -0.2,          # v3: lee band >= 20 percent darker than the sun-side band -> the strip stands up (hedge)
    'cast_shadow_min': 0.1,         # v4: ...and its lee band holds >= 10 points more shadow-class pixels than the sun-side band
    'clipped_lee_max': -0.3,        # v3: a smooth light patch under clipped_area_max casting this much shadow -> clipped shrub
    'clipped_area_max': 20.0,
    # v3/v4: a garden bed is ground level: it does not pass the cast-shadow test (casts) used for hedges
    'garden_soil_share': (0.15, 0.7),  # v2: share of brown non-green non-shadow pixels (soil, mulch) between these ...
    'garden_green_min': 0.3,           # ...with foliage present ...
    'garden_area_min': 4.0,            # ...on a patch of at least 4 m2 -> garden bed / mixed planting
    'soil_hue': (40.0, 80.0), 'soil_L': (40.0, 85.0),  # v4: brown / tan pixel band for soil_share (v1-v3: hue 40-95, L 35-80 took olive eucalypt foliage)
    'tree_group_area_min': 150.0,   # larger crowns are usually several trees (tree-group-split programme)
    'tree_eq_diam_min': 4.0,        # crown spread >= 4 m (12.6 m2) -> tree; smaller textured clump -> shrub
}
CLASSES = ('speck', 'not-green', 'shadow', 'on-roof', 'frame-sliver', 'hedge', 'lawn', 'garden-bed', 'tree-group', 'tree',
           'shrub')
CLASS_COLOURS = {'speck': (255, 255, 255), 'not-green': (160, 160, 160), 'shadow': (255, 0, 200), 'on-roof': (90, 200, 255),
                 'frame-sliver': (255, 80, 80),
                 'lawn': (190, 255, 90), 'hedge': (255, 170, 0), 'garden-bed': (200, 120, 60),
                 'tree-group': (0, 120, 255), 'tree': (40, 220, 60), 'shrub': (255, 240, 0)}


def now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def heartbeat(message):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUN_DIR/'progress.txt', 'a') as handle:
        handle.write(f'{datetime.now().strftime("%H:%M:%S")} census: {message}\n')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def window_ids():
    return sorted({json.loads(line)['window_scene_id'] for line in open(WINDOWS_FILE) if line.strip()})


def srgb_to_lab(rgb):
    """rgb float 0..1 (..., 3) -> Lab (D65)."""
    lin = np.where(rgb <= 0.04045, rgb/12.92, ((rgb+0.055)/1.055)**2.4)
    m = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ m.T/np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787*xyz+16/116)
    return np.stack([116*f[..., 1]-16, 500*(f[..., 0]-f[..., 1]), 200*(f[..., 1]-f[..., 2])], axis=-1)


def live(objects):
    return [o for o in objects or [] if o.get('status') not in LIVE_EXCLUDED]


def r(value, digits=3):
    return None if value is None or (isinstance(value, float) and not math.isfinite(value)) else round(float(value), digits)


def shape_features(obj, mpp, width, height):
    poly = sc.ring_polygon(obj)
    if poly is None:
        return None
    area = poly.area*mpp*mpp
    mrr = poly.minimum_rotated_rectangle
    xs, ys = mrr.exterior.coords.xy
    sides = sorted(math.hypot(xs[i+1]-xs[i], ys[i+1]-ys[i])*mpp for i in range(2))
    perimeter = poly.length*mpp
    hull = poly.convex_hull.area*mpp*mpp
    ring = np.asarray(obj['ring'], dtype=float)
    seg = np.vstack([ring, ring[:1]])
    edge = 0.0
    for (x0, y0), (x1, y1) in zip(seg[:-1], seg[1:], strict=True):
        on = lambda x, y: x <= FRAME_EDGE_PX or y <= FRAME_EDGE_PX or x >= width-FRAME_EDGE_PX or y >= height-FRAME_EDGE_PX  # noqa: E731
        if on(x0, y0) and on(x1, y1):
            edge += math.hypot(x1-x0, y1-y0)*mpp
    return {'area_m2': r(area, 2), 'eq_diam_m': r(2*math.sqrt(area/math.pi), 2),
            'mrr_width_m': r(sides[0], 2), 'mrr_length_m': r(sides[1], 2),
            'elongation': r(sides[1]/max(sides[0], 1e-6), 2),
            'compactness': r(4*math.pi*area/max(perimeter*perimeter, 1e-9)), 'solidity': r(area/max(hull, 1e-9)),
            'frame_edge_m': r(edge, 2)}


def shifted(mask, dx, dy):
    out = np.zeros_like(mask)
    h, w = mask.shape
    xs, ys = slice(max(0, dx), min(w, w+dx)), slice(max(0, dy), min(h, h+dy))
    xt, yt = slice(max(0, -dx), min(w, w-dx)), slice(max(0, -dy), min(h, h-dy))
    out[ys, xs] = mask[yt, xt]
    return out


def lee_features(mask, vec, steps, lum, excluded, sun_row, pix=None):
    """Cast-shadow cue: luminance just outside the mask on the shadow side (lee) against the sun side, 0.1-0.8 m out.
    lee_contrast = (median lee lum - median sun-side lum) / median sun-side lum; below 0 = darker lee = the object stands up.
    Pixels of live buildings and live tree-kind objects are left out of both bands (v3)."""
    if vec is None:
        return {'lee_contrast': None, 'lee_px': 0, 'sun_reliable': None}
    lee, sunside = np.zeros_like(mask), np.zeros_like(mask)
    for k in steps:
        dx, dy = int(round(vec[0]*k)), int(round(vec[1]*k))
        lee |= shifted(mask, dx, dy)
        sunside |= shifted(mask, -dx, -dy)
    lee &= ~mask & ~excluded          # excluded = live buildings + live tree-kind objects (their own darkness is not a cast shadow)
    sunside &= ~mask & ~excluded
    if lee.sum() < 10 or sunside.sum() < 10:
        return {'lee_contrast': None, 'lee_px': int(lee.sum()), 'sun_reliable': bool(sun_row.get('reliable'))}
    a, b = float(np.median(lum[lee])), float(np.median(lum[sunside]))
    out = {'lee_contrast': r((a-b)/max(b, 1e-3)), 'lee_px': int(lee.sum()), 'sun_reliable': bool(sun_row.get('reliable'))}
    if pix is not None:     # v4: shadow-class share (tree-shadow calibrated rule) in each band; a cast shadow is shadow-class
        out['lee_shadow_share'] = r((pix[lee] == 2).mean())
        out['sun_shadow_share'] = r((pix[sunside] == 2).mean())
    return out


def measure(args):
    th = sc.load_thresholds()
    sun = json.loads(SUN_FILE.read_text())['windows'] if SUN_FILE.exists() else {}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows, model_counts, review_counts = [], defaultdict(Counter), defaultdict(Counter)
    ids = window_ids()
    for index, scene_id in enumerate(ids):
        scene_dir, meta, review = sc.load_scene(scene_id)
        for o in meta.get('objects', []):
            model_counts[o['kind']][scene_id] += 1
        objects = live((review or {}).get('objects'))
        for o in (review or {}).get('objects', []):
            if o['kind'] in VEG_KINDS:
                review_counts[(o['kind'], o.get('status'))][scene_id] += 1
        veg = [o for o in objects if o['kind'] in VEG_KINDS]
        if not veg:
            continue
        lin, pixel_area, mpp = sc.pixel_geometry(meta)
        w, h = meta['image_width'], meta['image_height']
        feats = sc.frame_features(scene_dir/f"{meta['image_sha256']}.png", mpp)
        lab = srgb_to_lab(feats['rgb'])
        pix = sc.classify(feats, th)
        green = feats['ExG'] >= th['t_g']
        hue = np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) % 360
        soil = (~green & (pix != 2) & (hue >= RULE['soil_hue'][0]) & (hue <= RULE['soil_hue'][1])
                & (lab[..., 0] >= RULE['soil_L'][0]) & (lab[..., 0] <= RULE['soil_L'][1]))
        buildings = np.zeros((h, w), bool)
        paved = np.zeros((h, w), bool)
        standing = np.zeros((h, w), bool)
        for o in objects:
            if o['kind'] == 'tree':
                standing |= sc.rasterise(o, w, h)
            if o['kind'] == 'building':
                buildings |= sc.rasterise(o, w, h)
            elif o['kind'] in PAVED:
                paved |= sc.rasterise(o, w, h)
        frame_l50 = float(np.median(lab[..., 0]))
        frame_green_l50 = float(np.median(lab[..., 0][green])) if green.any() else frame_l50
        half = feats['texture_window_px']//2
        sun_row = sun.get(scene_id)
        lee_vec = sc.pixel_vector_of_bearing(lin, sun_row['shadow_bearing_used']) if sun_row else None
        lee_steps = sorted({max(1, round(d/mpp)) for d in np.linspace(LEE_M[0], LEE_M[1], 6)})
        ring_in, ring_out = max(1, round(RING_M[0]/mpp)), max(2, round(RING_M[1]/mpp))
        for o in veg:
            full = sc.rasterise(o, w, h)
            n = int(full.sum())
            shape = shape_features(o, mpp, w, h)
            if n == 0 or shape is None:
                rows.append({'scene': scene_id, 'id': o['id'], 'kind': o['kind'], 'status': o['status'], 'empty': True})
                continue
            x0, y0, x1, y1 = crop_box(o, w, h, ring_out+3)
            win = (slice(y0, y1), slice(x0, x1))
            mask = full[win]
            inner = ndimage.binary_erosion(mask, iterations=half) if half else mask
            if inner.sum() < MIN_INNER_PX:
                inner = mask
            grown_in = ndimage.binary_dilation(mask, iterations=ring_in)
            grown_out = ndimage.binary_dilation(mask, iterations=ring_out)
            band = grown_out & ~grown_in & ~buildings[win]
            pix_w, green_w, tex_w, exg_w, soil_w = pix[win], green[win], feats['T'][win], feats['ExG'][win], soil[win]
            L, A, B = (lab[win][..., i][mask] for i in range(3))
            a50, b50 = float(np.median(A)), float(np.median(B))
            rows.append({
                'scene': scene_id, 'id': o['id'], 'kind': o['kind'], 'status': o['status'],
                'geometry_reviewed': bool(o.get('geometry_reviewed')), 'source': o.get('source'), 'label': o.get('label'),
                'has_notes': bool(o.get('notes')), 'clipped_by_frame': bool(o.get('clipped_by_frame')),
                'carried': bool(o.get('carried_from')), 'mpp': r(mpp, 4), 'px': n, **shape,
                'lab_L': r(np.median(L), 1), 'frame_L50': r(frame_l50, 1), 'frame_green_L50': r(frame_green_l50, 1),
                'lab_L_rel': r(float(np.median(L))-frame_green_l50, 1), 'lab_a': r(a50, 1), 'lab_b': r(b50, 1),
                'hue_deg': r(math.degrees(math.atan2(b50, a50)) % 360, 1), 'chroma': r(math.hypot(a50, b50), 1),
                'lab_ab_spread': r(float(np.sqrt(np.var(A)+np.var(B))), 2),
                'exg_p50': r(np.median(exg_w[mask])), 'green_share': r(green_w[mask].mean()),
                'soil_share': r(soil_w[mask].mean()),
                'texture_p50': r(np.median(tex_w[inner]), 4), 'texture_p75': r(np.percentile(tex_w[inner], 75), 4),
                'texture_inner_px': int(inner.sum()),
                'shadow_share': r((pix_w[mask] == 2).mean()), 'crown_share': r((pix_w[mask] == 1).mean()),
                'ring_shadow_share': r((pix_w[band] == 2).mean()) if band.any() else None,
                **lee_features(mask, lee_vec, lee_steps, feats['lum'][win], buildings[win] | standing[win], sun_row, pix_w),
                'building_overlap': r((mask & buildings[win]).sum()/n), 'paved_overlap': r((mask & paved[win]).sum()/n),
                'ndsm_p50': None, 'ndsm_p90': None})
        heartbeat(f'measure {index+1}/{len(ids)} {scene_id} veg={len(veg)}')
    with open(OUT_DIR/'rows.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True)+'\n')
    coverage = height_coverage(ids)
    coverage['model_counts'] = {k: {'objects': sum(c.values()), 'windows': len(c)} for k, c in sorted(model_counts.items())}
    coverage['review_counts'] = {f'{k}:{s}': {'objects': sum(c.values()), 'windows': len(c)}
                                 for (k, s), c in sorted(review_counts.items())}
    coverage['model_counts_per_window'] = {k: dict(sorted(model_counts[k].items())) for k in VEG_KINDS}
    (OUT_DIR/'coverage.json').write_text(json.dumps(coverage, indent=1, sort_keys=True))
    print(f'{len(rows)} rows; coverage.json written')


def height_coverage(ids):
    """Which height sources reach the windows: LINZ nDSM (Christchurch), Vic LiDAR (licensed), the depth cache."""
    out = {'windows': len(ids), 'computed_at': now()}
    catalog = json.loads(LINZ_CATALOG.read_text())
    out['linz_ndsm'] = {'epoch': catalog.get('epoch'), 'crs': catalog.get('crs'), 'gsd_m': catalog.get('gsd_m'),
                        'region': 'Christchurch, NZ (EPSG:2193)', 'windows_covered': 0}
    lat = [json.loads((SCENES/s/'latest.json').read_text())['centroid'] for s in ids]
    out['window_centroid_range'] = {'lon': [r(min(c['lon'] for c in lat), 4), r(max(c['lon'] for c in lat), 4)],
                                    'lat': [r(min(c['lat'] for c in lat), 4), r(max(c['lat'] for c in lat), 4)]}
    out['linz_ndsm']['why_zero'] = 'every window centroid is in Melbourne (lat -37.7 .. -37.9); the LINZ tiles are at lat ~ -43.5'
    summary = json.loads(HEIGHTS_SUMMARY.read_text())
    out['vic_lidar'] = {'status': summary['lidar']['status'],
                        'collections': [c['filename'] for c in summary['lidar']['covering_collections']]}
    cached = sorted(p.name for p in DEPTH_DIR.iterdir() if p.is_dir())
    out['depth_cache'] = {'scene_dirs': len(cached), 'window_dirs': sum(s in cached for s in ids),
                          'note': 'Depth Anything relative relief cached for the Mooroolbark tile scenes only; '
                                  'heights-v1 metre fit not adopted (best cv R2 0.13 on buildings)'}
    out['ndsm_windows_covered'] = 0
    return out


WORLD = 40075016.68557849   # scene_addresses.WORLD (web-mercator circumference, the local-metre projection)
SAME_OBJECT_M = 2.0          # centroids of two windows' objects within this distance and area ratio <= 2 -> one physical object


def lonlat(meta, x, y):
    """Frame pixel -> lon / lat through pixel_to_meters and scene_recapture.local_metres inverted."""
    a = np.asarray(meta['pixel_to_meters'], dtype=float)
    east, north = x*a[0, 0]+y*a[1, 0]+a[2, 0], x*a[0, 1]+y*a[1, 1]+a[2, 1]
    lon0, lat0 = meta['centroid']['lon'], meta['centroid']['lat']
    scale = WORLD*math.cos(math.radians(lat0))
    y0 = math.asinh(math.tan(math.radians(lat0)))/(2*math.pi)
    return lon0+east/scale*360, math.degrees(math.atan(math.sinh(2*math.pi*(y0+north/scale))))


def unique_estimate(rows):
    """The 54 windows overlap: approximate physical-object counts per class by union-find over cross-window pairs."""
    metas = {}
    points = []
    for x in rows:
        if x['scene'] not in metas:
            scene_dir, meta, review = sc.load_scene(x['scene'])
            metas[x['scene']] = (meta, {o['id']: o for o in review['objects']})
        meta, objs = metas[x['scene']]
        cx, cy = sc.ring_polygon(objs[x['id']]).centroid.coords[0]
        lon, lat = lonlat(meta, cx, cy)
        points.append((lon, lat))
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    lat_m = 111_320.0
    pts = np.asarray(points)
    lon_m = lat_m*math.cos(math.radians(float(np.mean(pts[:, 1]))))
    for i in range(len(rows)):
        d = np.hypot((pts[:, 0]-pts[i, 0])*lon_m, (pts[:, 1]-pts[i, 1])*lat_m)
        for j in np.flatnonzero(d <= SAME_OBJECT_M):
            if j <= i or rows[j]['scene'] == rows[i]['scene']:
                continue
            a, b = rows[i]['area_m2'], rows[j]['area_m2']
            if max(a, b) <= 2*min(a, b):
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)
    out = Counter()
    split = 0
    for members in groups.values():
        classes = Counter(rows[i]['proposed'] for i in members)
        out[classes.most_common(1)[0][0]] += 1
        split += len(classes) > 1
    return {'method': f'centroid within {SAME_OBJECT_M} m, area ratio <= 2, different windows; majority class per group',
            'physical_objects': len(groups), 'object_rows': len(rows), 'groups_with_disagreeing_class': split,
            'by_class': dict(out)}


def strip_shape(row):
    w0, w1 = RULE['hedge_width']
    return (w0 <= row['mrr_width_m'] <= w1 and row['mrr_length_m'] >= RULE['hedge_length_min']
            and row['elongation'] >= RULE['hedge_elongation_min'] and row['solidity'] >= RULE['hedge_solidity_min'])


def casts_shadow(row):
    """(bool, lee minus sun-side shadow-class share): darker lee band AND shadow-class pixels there (RULE v4)."""
    lee = row.get('lee_contrast')
    lee_shadow = (row.get('lee_shadow_share') or 0.0)-(row.get('sun_shadow_share') or 0.0)
    return (lee is not None and lee <= RULE['hedge_lee_max'] and lee_shadow >= RULE['cast_shadow_min']), lee_shadow


def rule_class(row):
    """(class, reason) for one measured row; first match wins (RULE v4)."""
    t = RULE
    if row['area_m2'] < t['speck_area_max']:
        return 'speck', f"area {row['area_m2']:.2f} m2"
    hue_ok = row['hue_deg'] is not None and t['lawn_hue'][0] <= row['hue_deg'] <= t['lawn_hue'][1]
    if row['green_share'] < t['not_veg_green_max'] and not hue_ok:
        return 'not-green', f"green {row['green_share']:.2f} < {t['not_veg_green_max']} and hue {row['hue_deg']:.0f}"
    if row['shadow_share'] >= t['shadow_dominant_min']:
        return 'shadow', f"shadow share {row['shadow_share']:.2f}"
    if row['building_overlap'] >= t['on_roof_min']:
        return 'on-roof', f"building overlap {row['building_overlap']:.2f}"
    lee = row.get('lee_contrast')
    lee_txt = 'n/a' if lee is None else f'{lee:+.2f}'
    casts, lee_shadow = casts_shadow(row)
    lee_txt += f', lee shadow {lee_shadow:+.2f}'
    if (row['elongation'] >= t['hedge_elongation_min']
            and row['frame_edge_m'] >= t['sliver_frame_share_min']*row['mrr_length_m']):
        return 'frame-sliver', f"frame contact {row['frame_edge_m']:.1f} m of {row['mrr_length_m']:.1f} m long side"
    w0, w1 = t['hedge_width']
    strip = (w0 <= row['mrr_width_m'] <= w1 and row['mrr_length_m'] >= t['hedge_length_min']
             and row['elongation'] >= t['hedge_elongation_min'] and row['solidity'] >= t['hedge_solidity_min'])
    strip_txt = f"strip {row['mrr_width_m']:.1f} x {row['mrr_length_m']:.1f} m (elong {row['elongation']:.1f})"
    if strip and casts:
        return 'hedge', f"{strip_txt}, casts shadow (lee {lee_txt})"
    ring = row['ring_shadow_share'] if row['ring_shadow_share'] is not None else 0.0
    smooth_light = row['texture_p50'] < t['lawn_texture_max'] and row['lab_L_rel'] >= t['lawn_L_rel_min']
    very_smooth = row['texture_p50'] < t['lawn_smooth_texture_max'] and row['lab_L_rel'] >= t['lawn_smooth_L_rel_min']
    if (smooth_light or very_smooth) and ring < t['lawn_ring_shadow_max'] and hue_ok:
        if casts and lee <= t['clipped_lee_max'] and row['area_m2'] < t['clipped_area_max']:
            return 'shrub', f"smooth but casts shadow (lee {lee_txt}), {row['area_m2']:.0f} m2: clipped shrub?"
        return 'lawn', (f"{strip_txt + ', ' if strip else ''}smooth (texture {row['texture_p50']:.3f}), "
                        f"L rel {row['lab_L_rel']:+.0f}, lee {lee_txt}")
    ground = not casts
    if strip and ground:
        return 'garden-bed', f"textured {strip_txt}, no cast shadow (lee {lee_txt})"
    s0, s1 = t['garden_soil_share']
    if (s0 <= row['soil_share'] < s1 and row['green_share'] >= t['garden_green_min']
            and row['area_m2'] >= t['garden_area_min'] and ground):
        return 'garden-bed', f"soil / mulch share {row['soil_share']:.2f}, green {row['green_share']:.2f}, lee {lee_txt}"
    if row['area_m2'] >= t['tree_group_area_min']:
        return 'tree-group', f"area {row['area_m2']:.0f} m2"
    if row['eq_diam_m'] >= t['tree_eq_diam_min']:
        return 'tree', f"textured crown, spread {row['eq_diam_m']:.1f} m, lee {lee_txt}"
    return 'shrub', f"textured clump, spread {row['eq_diam_m']:.1f} m, lee {lee_txt}"


VEG_WORDS = r'hedge|grass|lawn|shrub|bush|garden|mulch|turf|vegetation|foliage|canopy|crown'


def label_scan():
    """Labels / notes on tree and vegetation objects in every review.json (all scenes, not only the 54 windows)."""
    labels, notes, other, statuses, scenes = defaultdict(Counter), Counter(), Counter(), Counter(), 0
    for path in sorted(SCENES.glob('*/review.json')):
        try:
            review = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        scenes += 1
        for o in review.get('objects', []):
            text = f"{o.get('label', '')} {o.get('notes') or ''}".lower()
            if o['kind'] in VEG_KINDS:
                statuses[f"{o['kind']}:{o.get('status')}"] += 1
                labels[o['kind']][re.sub(r'\d+', '#', o.get('label', ''))] += 1
                if o.get('notes'):
                    who = 'reviewer' if o['notes'].lstrip().startswith('[reviewer') or not re.match(
                        r'(Codex|Assistant)', o['notes'].lstrip()) else 'agent'
                    notes[f"{o['kind']}:{who}"] += 1
            elif re.search(VEG_WORDS, text):
                other[f"{o['kind']}:{o.get('label', '')[:60]}"] += 1
    return {'review_files': scenes, 'status_counts': dict(statuses),
            'labels': {k: {'distinct': len(c), 'top': c.most_common(12)} for k, c in labels.items()},
            'notes_by_author_guess': dict(notes), 'non_vegetation_kinds_with_vegetation_words': other.most_common(15),
            'structure_kind_on_vegetation': 0}


def load_rows():
    return [json.loads(line) for line in open(OUT_DIR/'rows.jsonl') if line.strip()]


def pct(values, q):
    return r(np.percentile(values, q), 3) if len(values) else None


def classify(args):
    rows = [x for x in load_rows() if not x.get('empty')]
    for row in rows:
        row['proposed'], row['reason'] = rule_class(row)
        row['casts_shadow'] = casts_shadow(row)[0]
        row['frame_cut'] = bool(row['frame_edge_m'] and row['frame_edge_m'] >= 1.0) or row['clipped_by_frame']
    with open(OUT_DIR/'rows.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True)+'\n')
    counts = {c: Counter() for c in CLASSES}
    for row in rows:
        counts[row['proposed']][row['kind']] += 1
    by_class = {}
    for c in CLASSES:
        sub = [x for x in rows if x['proposed'] == c]
        by_class[c] = {
            'objects': len(sub), 'windows': len({x['scene'] for x in sub}),
            'from_kind': dict(counts[c]), 'confirmed': sum(x['status'] == 'confirmed' for x in sub),
            'frame_cut': sum(x['frame_cut'] for x in sub), 'area_m2_total': r(sum(x['area_m2'] for x in sub), 1),
            'area_m2_p50': pct([x['area_m2'] for x in sub], 50),
            'texture_p50_p50': pct([x['texture_p50'] for x in sub], 50),
            'green_share_p50': pct([x['green_share'] for x in sub], 50),
            'shadow_share_p50': pct([x['shadow_share'] for x in sub], 50),
            'strip_shaped': sum(strip_shape(x) for x in sub), 'casts_shadow': sum(bool(x['casts_shadow']) for x in sub),
            'lee_contrast_p50': pct([x['lee_contrast'] for x in sub if x.get('lee_contrast') is not None], 50)}
    dist = {}
    for kind in VEG_KINDS:
        for status in ('confirmed', 'candidate', 'needs_adjustment'):
            sub = [x for x in rows if x['kind'] == kind and x['status'] == status]
            if not sub:
                continue
            dist[f'{kind}:{status}'] = {'n': len(sub), **{f: [pct([x[f] for x in sub if x[f] is not None], q) for q in (10, 50, 90)]
                                        for f in ('area_m2', 'eq_diam_m', 'mrr_width_m', 'elongation', 'compactness',
                                                  'solidity', 'texture_p50', 'green_share', 'hue_deg', 'lab_L',
                                                  'shadow_share', 'ring_shadow_share', 'building_overlap')}}
    coverage = json.loads((OUT_DIR/'coverage.json').read_text())
    report = {'version': 'vegetation-census-v1', 'computed_at': now(), 'code_sha256': CODE_SHA256,
              'shadow_classifier': {'module_sha256': sha(SHADOW_CODE), 'calibration_sha256': sha(SHADOW_CALIBRATION),
                                    'thresholds': sc.load_thresholds()},
              'population': {'windows': coverage['windows'], 'review_live_measured': len(rows),
                             'review_live_by_kind': dict(Counter(x['kind'] for x in rows)),
                             'review_status_counts': coverage['review_counts'], 'model_counts': coverage['model_counts']},
              'height': {k: coverage[k] for k in ('linz_ndsm', 'vic_lidar', 'depth_cache', 'ndsm_windows_covered',
                                                  'window_centroid_range')},
              'rule': RULE, 'rule_order': list(CLASSES), 'by_class': by_class, 'feature_p10_p50_p90': dist,
              'cross_tab_kind_by_class': {k: {c: counts[c][k] for c in CLASSES} for k in VEG_KINDS},
              'strip_shaped_total': sum(strip_shape(x) for x in rows),
              'strip_shaped_casting_shadow': sum(strip_shape(x) and bool(x['casts_shadow']) for x in rows),
              'casts_shadow_by_kind': {k: sum(bool(x['casts_shadow']) for x in rows if x['kind'] == k) for k in VEG_KINDS},
              'sun': {'file': str(SUN_FILE.relative_to(ROOT)), 'sha256': sha(SUN_FILE) if SUN_FILE.exists() else None,
                      'rows_without_lee': sum(x.get('lee_contrast') is None for x in rows),
                      'rows_on_unreliable_sun_windows': sum(x.get('sun_reliable') is False for x in rows)},
              'labels': label_scan(), 'unique_estimate': unique_estimate(rows)}
    (OUT_DIR/'census.json').write_text(json.dumps(report, indent=1, sort_keys=False))
    lines = ['| class | objects | windows | from tree | from vegetation | confirmed | frame-cut | area p50 m2 | texture p50 | green p50 |',
             '|---|---|---|---|---|---|---|---|---|---|']
    for c in CLASSES:
        b = by_class[c]
        lines.append(f"| {c} | {b['objects']} | {b['windows']} | {b['from_kind'].get('tree', 0)} | {b['from_kind'].get('vegetation', 0)} "
                     f"| {b['confirmed']} | {b['frame_cut']} | {b['area_m2_p50']} | {b['texture_p50_p50']} | {b['green_share_p50']} |")
    (OUT_DIR/'tables.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))


def crop_box(obj, w, h, margin):
    xs, ys = [p[0] for p in obj['ring']], [p[1] for p in obj['ring']]
    x0, y0 = max(0, int(min(xs))-margin), max(0, int(min(ys))-margin)
    x1, y1 = min(w, int(max(xs))+margin+1), min(h, int(max(ys))+margin+1)
    return x0, y0, x1, y1


def render_example(row, path, size=300):
    scene_dir, meta, review = sc.load_scene(row['scene'])
    obj = next(o for o in review['objects'] if o['id'] == row['id'])
    image = Image.open(scene_dir/f"{meta['image_sha256']}.png").convert('RGB')
    margin = max(25, int(0.35*max(row['mrr_length_m']/row['mpp'], 40)))
    x0, y0, x1, y1 = crop_box(obj, image.width, image.height, margin)
    scale = size/max(x1-x0, y1-y0)
    crop = image.crop((x0, y0, x1, y1)).resize((max(1, round((x1-x0)*scale)), max(1, round((y1-y0)*scale))), Image.LANCZOS)
    raw = crop.copy()
    draw = ImageDraw.Draw(crop)
    pts = [((x-x0)*scale, (y-y0)*scale) for x, y in obj['ring']]
    draw.line(pts+[pts[0]], fill=CLASS_COLOURS[row['proposed']], width=2)
    mrr = sc.ring_polygon(obj).minimum_rotated_rectangle
    draw.line([((x-x0)*scale, (y-y0)*scale) for x, y in mrr.exterior.coords], fill=(255, 255, 255), width=1)
    bar = 2/row['mpp']*scale          # 2 m scale bar
    draw.rectangle([4, crop.height-10, 4+bar, crop.height-6], fill=(255, 255, 255))
    panel = Image.new('RGB', (raw.width+crop.width+6, max(raw.height, crop.height)+58), (20, 20, 20))
    panel.paste(raw, (0, 0))
    panel.paste(crop, (raw.width+6, 0))
    text = ImageDraw.Draw(panel)
    y = max(raw.height, crop.height)+2
    text.text((4, y), f"{row['proposed']} | {row['kind']} {row['status']} | {row['scene']} {row['id']}", fill=(255, 255, 255))
    text.text((4, y+14), f"A {row['area_m2']:.1f} m2  w {row['mrr_width_m']:.1f} x {row['mrr_length_m']:.1f} m  elong {row['elongation']:.1f}  "
                         f"solid {row['solidity']:.2f}  frame {row['frame_edge_m']:.1f} m", fill=(220, 220, 220))
    text.text((4, y+28), f"tex {row['texture_p50']:.3f}  green {row['green_share']:.2f}  hue {row['hue_deg']:.0f}  L {row['lab_L']:.0f}  "
                         f"shadow {row['shadow_share']:.2f}  ring {row['ring_shadow_share'] or 0:.2f}  bldg {row['building_overlap']:.2f}", fill=(220, 220, 220))
    text.text((4, y+42), row['reason'][:90], fill=(255, 220, 120))
    panel.save(path)
    return panel


def render(args):
    rows = [x for x in load_rows() if 'proposed' in x]
    out = OUT_DIR/'renders'
    rng = random.Random(args.seed)
    picks = {}
    for c in CLASSES:
        pool = [x for x in rows if x['proposed'] == c]
        rng.shuffle(pool)
        chosen, seen = [], Counter()
        for x in sorted(pool, key=lambda x: x['status'] != 'confirmed'):   # confirmed first, then random
            if seen[x['scene']] >= 2:
                continue
            chosen.append(x)
            seen[x['scene']] += 1
            if len(chosen) >= args.per_class:
                break
        picks[c] = chosen
    sheet_tiles = []
    manifest = []
    for c, chosen in picks.items():
        (out/c).mkdir(parents=True, exist_ok=True)
        for i, x in enumerate(chosen):
            path = out/c/f"{i+1:02d}-{x['scene']}-{x['id']}.png"
            panel = render_example(x, path)
            manifest.append({'class': c, 'path': str(path.relative_to(ROOT)), 'scene': x['scene'], 'id': x['id'],
                             'kind': x['kind'], 'status': x['status'], 'reason': x['reason']})
            sheet_tiles.append((c, panel))
        heartbeat(f'render {c} {len(chosen)}')
    tile_w, tile_h, cols = 300, 200, args.per_class
    sheet = Image.new('RGB', (cols*(tile_w+4)+110, len(picks)*(tile_h+4)), (10, 10, 10))
    draw = ImageDraw.Draw(sheet)
    for row_index, c in enumerate(picks):
        draw.text((4, row_index*(tile_h+4)+tile_h//2), c, fill=CLASS_COLOURS[c])
        for col, (_, panel) in enumerate([t for t in sheet_tiles if t[0] == c]):
            # contact-sheet tile = the annotated half of the panel
            half = panel.crop((panel.width//2+3, 0, panel.width, panel.height-58))
            half.thumbnail((tile_w, tile_h))
            sheet.paste(half, (110+col*(tile_w+4), row_index*(tile_h+4)))
    sheet.save(out/'contact-sheet.png')
    (out/'manifest.json').write_text(json.dumps(manifest, indent=1))
    print(f'{len(manifest)} renders + contact sheet in {out.relative_to(ROOT)}')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('measure').set_defaults(func=measure)
    sub.add_parser('classify').set_defaults(func=classify)
    p = sub.add_parser('render')
    p.add_argument('--per-class', type=int, default=6)
    p.add_argument('--seed', type=int, default=8)
    p.set_defaults(func=render)
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
