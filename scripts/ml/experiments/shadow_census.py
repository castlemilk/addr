"""Read-only census of cast-shadow inclusion in tree / vegetation (and car) outlines on the 54 recapture windows (tree-shadow-2026-10-08).

Reviewer (chat, 8 Oct ~08:20 AEDT): win-69468b2b783cd964 object 547f6490e66df5d0 (a tree, confirmed 7 Oct) "is picking up shadows".
SAM3 masks are modal: a 'tree canopy' prompt can return crown + the tree's cast shadow as one blob. This script measures how often and
how much, per class and layer, with a simple photometric pixel rule and a per-window sun azimuth read from building shadows. Every
figure is a count or a geometry; nothing is accuracy; nothing is promoted; nothing under data/scenes is written; no inference.

Population: the 54 windows of data/reviews/eyes/recapture/windows-2026-10-01.jsonl (window_scene_id). Layers: "review" =
data/scenes/<id>/review.json objects that are live (status not rejected / superseded); "model" = data/scenes/<id>/latest.json objects
(all candidates). Classes: tree, vegetation, car (car = comparison class). Frame = <scene>/<image_sha256>.png; rings are frame pixels.

Pixel rule (fixed before the census; thresholds derived ONLY from the 5 hand-labelled calibration examples recorded in
calibration-labels.json, never from the reviewer's example or its window):
  features  V_rel = max(R,G,B) / median frame max(R,G,B) (value); C = max(R,G,B) - min(R,G,B) (chroma, the 'saturation' test: HSV
            saturation (max-min)/max inflates in dark pixels and was higher in labelled shadow than in lit ground, so it was dropped);
            Bdom = (B - max(R,G))/(R+G+B) (blue shift: blue exceeds both red and green; a first try with (B-R)/(R+G+B) called the
            dark teal crowns of the inner capture shadow, while G-B separated crown from shadow in all 5 examples);
            ExG = (2G-R-B)/(R+G+B) (green dominance; keeps olive crowns where R > G);
            T = local std of luminance (0.299R+0.587G+0.114B, 0..1) in a TEXTURE_WINDOW_M square window.
  shadow    V_rel <= t_v AND C <= t_c_chroma AND Bdom >= t_b AND T <= t_t   (tested first; a dark, smooth, blue-shifted pixel is shadow
            even when green-tinted: about a quarter of the labelled ground shadow had ExG >= t_g)
  crown     (ExG >= t_g OR T >= t_c) AND NOT shadow
  other     everything else
  Each threshold is a decision stump (Youden J) on the labelled pixels: t_v, t_c_chroma, t_b, t_t separate labelled shadow from
  labelled crown + other, restricted to cuts that keep >= SHADOW_TEST_RECALL of labelled shadow (the tests are AND-ed);
  t_g separates crown from shadow + other; t_c separates crown from other with <= CROWN_TEXTURE_MAX_PASS of other let through
  (a secondary route for dark, non-green crown pixels; at plain Youden J it admitted half the lit ground).

Sun azimuth (per window, from building shadows): live review-layer buildings >= 20 m2, ring simplified at 0.3 m, straight edges
>= 3 m, central 70 % of each edge. Along each edge's outward normal (samples inside any building, tree or car mask or off-frame
skipped) the share of shadow-class pixels (the rule above) per distance gives the dark-band width w = distance from 0.1 m to the first
distance whose shadow share drops below BAND_SHADOW_MIN (w = 0 when the 0.1-0.4 m share is already below it).
Primary (method B): phi = bearing of the length-weighted first harmonic of the 0.2-1.0 m shadow-class share over edge normals
(theta_i = edge normal compass bearing; the side whose near band holds the most shadow), sun azimuth = phi + 180.
Cross-checks: A = band-offset fit w_i = L_b * max(0, cos(theta_i - phi)) (L_b per building, 1-degree grid, L_b closed form; noisy when
few edges carry a band); C = first harmonic of 0.2-1.0 m luminance darkness (no classifier). A first version of A that found the band
from luminance alone (half-way recovery to the 4-6 m level) called a band on ~60 % of edges and scattered.
Spread: bootstrap over buildings (200 resamples, circular std). Reliable = B spread <= 25 deg and >= 12 edges; otherwise the area mean.
Capture check: windows grouped by area (east ~145.31 E, inner ~144.98 E); circular mean and per-window deviation.

Per object: shadow share = shadow px / mask px; shadow m2; crown centroid (crown px; non-shadow px for cars or when crown < 30 px);
sun-opposite share = shadow px whose offset from that centroid has a positive component along the shadow bearing (cast shadow -> near
1; a dark crown interior -> about 0.5). Control: the same share on a perpendicular bearing (expected about 0.5).
Secondary, threshold-free (added after the reviewer example showed dappled shadow failing the smooth-texture test): shadow-side
excess = (non-crown px on the sun-opposite side - non-crown px on the sun side) / mask px, around the crown centroid. A loose but
symmetric outline scores about 0; crown + a cast shadow scores about the shadow's share whether or not the rule calls it shadow.

Run from the repo root (subcommands, in order):
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.shadow_census sheet       # calibration candidate crops with a pixel grid
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.shadow_census calibrate   # labels -> calibration.json thresholds
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.shadow_census sun         # per-window sun azimuth -> sun.json
  scripts/ml/.venv/bin/python -m scripts.ml.experiments.shadow_census census      # census.json, objects.jsonl, census.md, renders/
Outputs ONLY under data/reviews/eyes/experiments/tree-shadow-2026-10-08/census/ (+ heartbeat lines in .run/session-2026-10-08-shadow/census/).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

ROOT = Path(__file__).resolve().parents[3]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCENES = ROOT/'data/scenes'
WINDOWS_FILE = ROOT/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
OUT_DIR = ROOT/'data/reviews/eyes/experiments/tree-shadow-2026-10-08/census'
RUN_DIR = ROOT/'.run/session-2026-10-08-shadow/census'
LABELS_FILE = OUT_DIR/'calibration-labels.json'
CALIBRATION_FILE = OUT_DIR/'calibration.json'
SUN_FILE = OUT_DIR/'sun.json'
REVIEWER_EXAMPLE = ('win-69468b2b783cd964', '547f6490e66df5d0')
CLASSES = ('tree', 'vegetation', 'car')
LIVE_EXCLUDED = ('rejected', 'superseded')
SHARE_LEVELS = (0.10, 0.20, 0.30)
TEXTURE_WINDOW_M = 0.4
CROWN_MIN_PX = 30
SHADOW_TEST_RECALL = 0.90    # each AND-ed shadow test keeps >= this share of labelled shadow pixels (tolerates ~10 % label noise:
                             # road markings and edge pixels inside the shadow boxes; at 0.95 the value cut rose to 0.87, the
                             # value test alone passed 55 % of labelled non-shadow and the rule called 23 % of E2's lit asphalt shadow)
CROWN_TEXTURE_MAX_PASS = 0.10   # the crown texture route admits <= this share of labelled lit ground
# sun azimuth from building shadows
BUILDING_MIN_M2 = 20.0
SIMPLIFY_M = 0.3
EDGE_MIN_M = 3.0
EDGE_TRIM = 0.15
PROFILE_MAX_M = 6.0
NEAR_M = (0.1, 0.4)
FAR_M = (4.0, 6.0)
BAND_SHADOW_MIN = 0.3          # a sample distance is 'in the band' while >= this share of its samples are shadow-class
NEAR_BAND_M = (0.2, 1.0)        # method B
BOOTSTRAP = 200
SUN_RELIABLE_STD = 25.0
SUN_RELIABLE_EDGES = 12


def now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def heartbeat(message):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUN_DIR/'progress.txt', 'a') as handle:
        handle.write(f'{datetime.now().strftime("%H:%M:%S")} {message}\n')


def load_json(path):
    with open(path) as handle:
        return json.load(handle)


def window_ids():
    rows = [json.loads(line) for line in open(WINDOWS_FILE) if line.strip()]
    return sorted({r['window_scene_id'] for r in rows})


def area_group(meta):
    return 'east' if meta['centroid']['lon'] > 145.1 else 'inner'


def load_scene(scene_id):
    scene_dir = SCENES/scene_id
    meta = load_json(scene_dir/'latest.json')
    review = load_json(scene_dir/'review.json') if (scene_dir/'review.json').exists() else None
    return scene_dir, meta, review


def pixel_geometry(meta):
    m = np.asarray(meta['pixel_to_meters'], dtype=float)[:2]      # rows: d(E,N)/dx, d(E,N)/dy
    pixel_area = abs(float(np.linalg.det(m)))
    return m, pixel_area, math.sqrt(pixel_area)


def bearing_of_pixel_vector(lin, vx, vy):
    east, north = np.array([vx, vy]) @ lin
    return math.degrees(math.atan2(east, north)) % 360


def pixel_vector_of_bearing(lin, bearing_deg):
    b = math.radians(bearing_deg)
    v = np.array([math.sin(b), math.cos(b)]) @ np.linalg.inv(lin)
    return v/np.linalg.norm(v)


def frame_features(image_path, mpp):
    rgb = np.asarray(Image.open(image_path).convert('RGB'), dtype=np.float32)/255.
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    total = r+g+b+1e-3
    vmax, vmin = rgb.max(-1), rgb.min(-1)
    lum = 0.299*r+0.587*g+0.114*b
    window = int(min(15, max(3, round(TEXTURE_WINDOW_M/mpp)//2*2+1)))
    mean = ndimage.uniform_filter(lum, window)
    sq = ndimage.uniform_filter(lum*lum, window)
    texture = np.sqrt(np.clip(sq-mean*mean, 0, None))
    return {'rgb': rgb, 'lum': lum, 'V_rel': vmax/max(float(np.median(vmax)), 1e-3), 'C': vmax-vmin,
            'Bdom': (b-np.maximum(r, g))/total, 'ExG': (2*g-r-b)/total, 'T': texture, 'texture_window_px': window,
            'V_median': float(np.median(vmax))}


def ring_polygon(obj):
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return None
    try:
        poly = Polygon(ring, [h for h in (obj.get('holes') or []) if len(h) >= 3])
    except (ValueError, TypeError):
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if not poly.is_empty and poly.area > 0 else None


def rasterise(obj, width, height):
    mask = Image.new('L', (width, height), 0)
    draw = ImageDraw.Draw(mask)
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return np.zeros((height, width), bool)
    draw.polygon([tuple(map(float, p)) for p in ring], fill=1)
    for hole in obj.get('holes') or []:
        if len(hole) >= 3:
            draw.polygon([tuple(map(float, p)) for p in hole], fill=0)
    return np.asarray(mask, dtype=bool)


def live_review_objects(review):
    return [o for o in (review or {}).get('objects', []) if o.get('status') not in LIVE_EXCLUDED]


# ---------------------------------------------------------------- calibration sheet

def sheet(args):
    """Candidate crops (review-layer trees with a dark share, both areas, never the reviewer's window) with a 10 px grid."""
    out = OUT_DIR/'calibration-sheet'
    out.mkdir(parents=True, exist_ok=True)
    candidates = []
    for scene_id in window_ids():
        if scene_id == REVIEWER_EXAMPLE[0]:
            continue
        scene_dir, meta, review = load_scene(scene_id)
        _, pixel_area, mpp = pixel_geometry(meta)
        feats = frame_features(scene_dir/f"{meta['image_sha256']}.png", mpp)
        w, h = meta['image_width'], meta['image_height']
        for obj in live_review_objects(review):
            if obj['kind'] != 'tree':
                continue
            mask = rasterise(obj, w, h)
            area = mask.sum()*pixel_area
            if not 10 <= area <= 120:
                continue
            dark = float((feats['V_rel'][mask] < 0.55).mean())
            candidates.append((area_group(meta), dark, scene_id, obj['id']))
    rng = random.Random(8)
    picks = []
    for group in ('east', 'inner'):
        pool = [c for c in candidates if c[0] == group and 0.2 <= c[1] <= 0.6]
        rng.shuffle(pool)
        seen = set()
        for c in pool:
            if c[2] in seen:
                continue
            seen.add(c[2])
            picks.append(c)
            if len(seen) >= args.per_group:
                break
    for group, _dark, scene_id, object_id in picks:
        scene_dir, meta, review = load_scene(scene_id)
        obj = next(o for o in review['objects'] if o['id'] == object_id)
        crop_with_grid(scene_dir/f"{meta['image_sha256']}.png", obj, out/f'{group}-{scene_id}-{object_id}.png')
    (out/'candidates.json').write_text(json.dumps([dict(zip(('group', 'dark_share_vrel_lt_0p55', 'scene', 'object'), p, strict=True))
                                                   for p in picks], indent=1))
    print(f'{len(picks)} candidate crops in {out.relative_to(ROOT)}')


def crop_with_grid(image_path, obj, path, margin=40, scale=3):
    image = Image.open(image_path).convert('RGB')
    xs, ys = [p[0] for p in obj['ring']], [p[1] for p in obj['ring']]
    x0, y0 = max(0, int(min(xs))-margin)//10*10, max(0, int(min(ys))-margin)//10*10
    x1, y1 = min(image.width, int(max(xs))+margin), min(image.height, int(max(ys))+margin)
    crop = image.crop((x0, y0, x1, y1)).resize(((x1-x0)*scale, (y1-y0)*scale), Image.NEAREST)
    draw = ImageDraw.Draw(crop)
    for gx in range(x0, x1, 10):
        draw.line([((gx-x0)*scale, 0), ((gx-x0)*scale, crop.height)], fill=(255, 255, 255) if gx % 50 == 0 else (90, 90, 90), width=1)
        if gx % 50 == 0:
            draw.text(((gx-x0)*scale+2, 2), str(gx), fill=(255, 255, 0))
    for gy in range(y0, y1, 10):
        draw.line([(0, (gy-y0)*scale), (crop.width, (gy-y0)*scale)], fill=(255, 255, 255) if gy % 50 == 0 else (90, 90, 90), width=1)
        if gy % 50 == 0:
            draw.text((2, (gy-y0)*scale+2), str(gy), fill=(255, 255, 0))
    draw.line([((x-x0)*scale, (y-y0)*scale) for x, y in obj['ring']+[obj['ring'][0]]], fill=(255, 40, 40), width=2)
    crop.save(path)


FEATURES = ('V_rel', 'C', 'Bdom', 'ExG', 'T')
CROWN_COLOUR, SHADOW_COLOUR, OTHER_COLOUR = (40, 220, 60), (255, 0, 200), (150, 150, 150)


def stump(pos, neg, direction, min_recall=0.0, max_pass=1.0):
    """Youden-J cut on one feature: direction 'le' (positive if value <= t) or 'ge'; cuts that keep < min_recall of pos or pass > max_pass
    of neg are skipped."""
    values = np.concatenate([pos, neg])
    cuts = np.unique(np.quantile(values, np.linspace(0, 1, 401)))
    best = None
    for t in cuts:
        tpr = float((pos <= t).mean() if direction == 'le' else (pos >= t).mean())
        fpr = float((neg <= t).mean() if direction == 'le' else (neg >= t).mean())
        if tpr < min_recall or fpr > max_pass:
            continue
        j = tpr-fpr
        if best is None or j > best[0]:
            best = (j, float(t), tpr, fpr)
    j, t, tpr, fpr = best
    return {'threshold': round(t, 4), 'direction': direction, 'youden_j': round(j, 3), 'pos_recall': round(tpr, 3),
            'neg_pass': round(fpr, 3), 'min_recall': min_recall, 'max_pass': max_pass}


def classify(feats, th):
    """0 = other, 1 = crown, 2 = shadow (the documented rule; thresholds from calibration.json)."""
    green = feats['ExG'] >= th['t_g']
    shadow = ((feats['V_rel'] <= th['t_v']) & (feats['C'] <= th['t_c_chroma']) & (feats['Bdom'] >= th['t_b'])
              & (feats['T'] <= th['t_t']))
    crown = (green | (feats['T'] >= th['t_c'])) & ~shadow
    out = np.zeros(feats['V_rel'].shape, np.uint8)
    out[crown] = 1
    out[shadow] = 2
    return out


def load_thresholds():
    return load_json(CALIBRATION_FILE)['thresholds']


def overlay(rgb_u8, classes, mask=None, alpha=0.55):
    base = rgb_u8.astype(np.float32)
    colour = np.zeros_like(base)
    for value, c in ((0, OTHER_COLOUR), (1, CROWN_COLOUR), (2, SHADOW_COLOUR)):
        colour[classes == value] = c
    a = np.full(classes.shape, alpha, np.float32) if mask is None else np.where(mask, alpha, 0.).astype(np.float32)
    return (base*(1-a[..., None])+colour*a[..., None]).clip(0, 255).astype(np.uint8)


def calibrate(args):
    labels = load_json(LABELS_FILE)
    pixels = defaultdict(lambda: defaultdict(list))
    per_example = {}
    frames = {}
    for ex in labels['examples']:
        assert ex['scene'] != REVIEWER_EXAMPLE[0], 'the reviewer window is excluded from calibration'
        scene_dir, meta, _ = load_scene(ex['scene'])
        _, _, mpp = pixel_geometry(meta)
        feats = frame_features(scene_dir/f"{meta['image_sha256']}.png", mpp)
        frames[ex['id']] = (meta, feats)
        counts = Counter()
        for region in ex['regions']:
            x0, y0, x1, y1 = region['box']
            for f in FEATURES:
                pixels[region['class']][f].append(feats[f][y0:y1, x0:x1].ravel())
            counts[region['class']] += (x1-x0)*(y1-y0)
        per_example[ex['id']] = dict(counts)
    cls = {c: {f: np.concatenate(v) for f, v in d.items()} for c, d in pixels.items()}
    not_shadow = {f: np.concatenate([cls['crown'][f], cls['other'][f]]) for f in FEATURES}
    not_crown = {f: np.concatenate([cls['shadow'][f], cls['other'][f]]) for f in FEATURES}
    stumps = {
        't_v': stump(cls['shadow']['V_rel'], not_shadow['V_rel'], 'le', SHADOW_TEST_RECALL),
        't_c_chroma': stump(cls['shadow']['C'], not_shadow['C'], 'le', SHADOW_TEST_RECALL),
        't_b': stump(cls['shadow']['Bdom'], not_shadow['Bdom'], 'ge', SHADOW_TEST_RECALL),
        't_t': stump(cls['shadow']['T'], not_shadow['T'], 'le', SHADOW_TEST_RECALL),
        't_g': stump(cls['crown']['ExG'], not_crown['ExG'], 'ge'),
        't_c': stump(cls['crown']['T'], cls['other']['T'], 'ge', max_pass=CROWN_TEXTURE_MAX_PASS),
    }
    thresholds = {k: v['threshold'] for k, v in stumps.items()}
    confusion = {}
    renders = []
    for ex in labels['examples']:
        meta, feats = frames[ex['id']]
        classes = classify(feats, thresholds)
        rows = {}
        for region in ex['regions']:
            x0, y0, x1, y1 = region['box']
            got = Counter(int(v) for v in classes[y0:y1, x0:x1].ravel())
            key = region['class']
            rows.setdefault(key, Counter()).update(got)
        confusion[ex['id']] = {k: {'other': v[0], 'crown': v[1], 'shadow': v[2]} for k, v in rows.items()}
        renders.append(calibration_render(ex, meta, feats, classes))
    pooled = defaultdict(Counter)
    for rows in confusion.values():
        for k, v in rows.items():
            pooled[k].update(v)
    percentiles = {c: {f: [round(float(np.percentile(cls[c][f], q)), 4) for q in (5, 25, 50, 75, 95)] for f in FEATURES} for c in cls}
    out = {'version': 'shadow-census-calibration-v1', 'computed_at': now(), 'code_sha256': CODE_SHA256,
           'labels_sha256': hashlib.sha256(LABELS_FILE.read_bytes()).hexdigest(), 'texture_window_m': TEXTURE_WINDOW_M,
           'thresholds': thresholds, 'stumps': stumps, 'labelled_pixels': per_example,
           'class_feature_percentiles_p5_p25_p50_p75_p95': percentiles,
           'resubstitution_confusion_rows_truth_cols_rule': {'pooled': {k: dict(v) for k, v in pooled.items()}, 'per_example': confusion},
           'renders': renders,
           'note': 'Resubstitution on the 5 labelled examples only (the pixels the cuts were fitted on): a consistency check, not accuracy.'}
    CALIBRATION_FILE.write_text(json.dumps(out, indent=1))
    print(json.dumps(thresholds))
    print('pooled', {k: dict(v) for k, v in pooled.items()})


def calibration_render(ex, meta, feats, classes, margin=25, scale=3):
    boxes = [r['box'] for r in ex['regions']]
    x0 = max(0, min(b[0] for b in boxes)-margin)
    y0 = max(0, min(b[1] for b in boxes)-margin)
    x1 = min(meta['image_width'], max(b[2] for b in boxes)+margin)
    y1 = min(meta['image_height'], max(b[3] for b in boxes)+margin)
    rgb = (feats['rgb'][y0:y1, x0:x1]*255).astype(np.uint8)
    panels = [Image.fromarray(rgb), Image.fromarray(overlay(rgb, classes[y0:y1, x0:x1]))]
    panels = [p.resize((p.width*scale, p.height*scale), Image.NEAREST) for p in panels]
    sheet_ = Image.new('RGB', (panels[0].width*2+6, panels[0].height), (255, 255, 255))
    for i, p in enumerate(panels):
        sheet_.paste(p, (i*(p.width+6), 0))
    draw = ImageDraw.Draw(sheet_)
    colours = {'crown': (255, 255, 0), 'shadow': (0, 255, 255), 'other': (255, 255, 255)}
    for i in range(2):
        for r in ex['regions']:
            bx0, by0, bx1, by1 = r['box']
            off = i*(panels[0].width+6)
            draw.rectangle([off+(bx0-x0)*scale, (by0-y0)*scale, off+(bx1-x0)*scale-1, (by1-y0)*scale-1], outline=colours[r['class']], width=2)
    draw.text((4, 4), f"{ex['id']} {ex['scene']} labels (yellow crown, cyan shadow, white other)", fill=(255, 255, 0))
    path = OUT_DIR/'calibration-check'/f"{ex['id']}-{ex['scene']}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_.save(path)
    return str(path.relative_to(ROOT))


def circ_mean(degrees, weights=None):
    a = np.radians(np.asarray(degrees, float))
    w = np.ones_like(a) if weights is None else np.asarray(weights, float)
    return float(np.degrees(math.atan2((w*np.sin(a)).sum(), (w*np.cos(a)).sum())) % 360)


def circ_std(degrees):
    a = np.radians(np.asarray(degrees, float))
    r = math.hypot(np.sin(a).mean(), np.cos(a).mean())
    return float(np.degrees(math.sqrt(max(0., -2*math.log(max(r, 1e-9))))))


def angle_diff(a, b):
    return float((a-b+180) % 360-180)


def union_mask(objects, kinds, width, height):
    mask = np.zeros((height, width), bool)
    for obj in objects:
        if obj['kind'] in kinds:
            mask |= rasterise(obj, width, height)
    return mask


def edge_profiles(meta, objects, lum, shadow):
    """One row per straight building edge: building index, normal bearing, length, shadow-class band width w (m), near-band
    shadow-class fraction, near-band luminance (all along the edge's outward normal; blocked / off-frame samples skipped)."""
    lin, pixel_area, mpp = pixel_geometry(meta)
    width, height = meta['image_width'], meta['image_height']
    blocked = union_mask(objects, ('building', 'tree', 'car'), width, height)
    d_px = np.arange(0, PROFILE_MAX_M/mpp+1e-9, 1.0)
    d_m = d_px*mpp
    near_sel = (d_m >= NEAR_M[0]) & (d_m <= NEAR_M[1])
    far_sel = (d_m >= FAR_M[0]) & (d_m <= FAR_M[1])
    band_sel = (d_m >= NEAR_BAND_M[0]) & (d_m <= NEAR_BAND_M[1])
    rows, b_index = [], 0
    for obj in objects:
        if obj['kind'] != 'building':
            continue
        poly = ring_polygon(obj)
        if poly is None or poly.geom_type != 'Polygon' or poly.area*pixel_area < BUILDING_MIN_M2:
            continue
        poly = orient(poly.simplify(SIMPLIFY_M/mpp), sign=1.0)
        if poly.is_empty or poly.geom_type != 'Polygon':
            continue
        coords = np.asarray(poly.exterior.coords)
        used = False
        for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:], strict=True):
            length_px = math.hypot(x1-x0, y1-y0)
            if length_px*mpp < EDGE_MIN_M:
                continue
            ux, uy = (x1-x0)/length_px, (y1-y0)/length_px
            nx, ny = uy, -ux                      # outward normal of a positively oriented ring
            ts = np.arange(EDGE_TRIM*length_px, (1-EDGE_TRIM)*length_px, 1.0)
            px = np.rint(x0+ux*ts[:, None]+nx*d_px[None, :]).astype(int)
            py = np.rint(y0+uy*ts[:, None]+ny*d_px[None, :]).astype(int)
            inside = (px >= 0) & (px < width) & (py >= 0) & (py < height)
            pxc, pyc = np.clip(px, 0, width-1), np.clip(py, 0, height-1)
            valid = inside & ~blocked[pyc, pxc]
            values = np.where(valid, lum[pyc, pxc], np.nan)
            svalues = np.where(valid, shadow[pyc, pxc], np.nan)
            enough = valid.mean(0) >= 0.4
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                profile = np.where(enough, np.nanmedian(values, 0), np.nan)
                sprofile = np.where(enough, np.nanmean(svalues, 0), np.nan)
            near, far = np.nanmedian(profile[near_sel]) if np.isfinite(profile[near_sel]).any() else np.nan, \
                np.nanmedian(profile[far_sel]) if np.isfinite(profile[far_sel]).any() else np.nan
            band = np.nanmedian(profile[band_sel]) if np.isfinite(profile[band_sel]).any() else np.nan
            if not (np.isfinite(near) and np.isfinite(far)) or not np.isfinite(sprofile[near_sel]).any():
                continue
            near_shadow = float(np.nanmean(sprofile[near_sel]))
            if near_shadow < BAND_SHADOW_MIN:
                w = 0.
            else:
                after = np.nonzero((d_m >= NEAR_M[0]) & ~(sprofile >= BAND_SHADOW_MIN))[0]   # NaN (blocked) ends the band too
                w = float(d_m[after[0]]) if len(after) else PROFILE_MAX_M
            band_shadow = float(np.nanmean(sprofile[band_sel])) if np.isfinite(sprofile[band_sel]).any() else np.nan
            rows.append({'b': b_index, 'object_id': obj['id'], 'normal_bearing': bearing_of_pixel_vector(lin, nx, ny),
                         'length_m': length_px*mpp, 'w_m': w, 'near_shadow': near_shadow, 'band_shadow': band_shadow,
                         'near': float(near), 'far': float(far), 'band': float(band)})
            used = True
        b_index += used
    return rows


def fit_shadow_bearing(rows, counts=None):
    """Band-offset model w_i = L_b * max(0, cos(theta_i - phi)); returns phi (deg) maximising sum_b N_b^2 / D_b."""
    theta = np.radians([r['normal_bearing'] for r in rows])
    w = np.array([r['w_m'] for r in rows])
    omega = np.array([r['length_m'] for r in rows])
    b = np.array([r['b'] for r in rows])
    phis = np.radians(np.arange(360))
    c = np.clip(np.cos(theta[:, None]-phis[None, :]), 0, None)
    nb = b.max()+1
    num = np.zeros((nb, 360))
    den = np.zeros((nb, 360))
    np.add.at(num, b, (omega*w)[:, None]*c)
    np.add.at(den, b, omega[:, None]*c*c)
    with np.errstate(all='ignore'):
        gain = np.where(den > 0, num*num/den, 0.)
    weights = np.ones(nb) if counts is None else counts
    objective = (weights[:, None]*gain).sum(0)
    return float(np.argmax(objective)), objective


def harmonic_bearing(rows, key='band', sign=1.0, building_counts=None):
    """Bearing of the length-weighted first harmonic of (median - value) * sign over edge normals; sign -1 for shadow fractions.
    building_counts (bootstrap) multiplies each edge's weight by its building's draw count."""
    band = np.array([r[key] for r in rows], float)
    ok = np.isfinite(band)
    if ok.sum() < 4:
        return None, None
    theta = np.radians([r['normal_bearing'] for r in rows])[ok]
    omega = np.array([r['length_m'] for r in rows])[ok]
    if building_counts is not None:
        omega = omega*building_counts[np.array([r['b'] for r in rows])[ok]]
    dark = sign*(np.median(band[ok])-band[ok])
    east, north = (omega*dark*np.sin(theta)).sum(), (omega*dark*np.cos(theta)).sum()
    resultant = math.hypot(east, north)/max((omega*np.abs(dark)).sum(), 1e-9)
    return float(math.degrees(math.atan2(east, north)) % 360), round(resultant, 3)


def sun(args):
    rng = np.random.default_rng(8)
    thresholds = load_thresholds()
    windows = {}
    for i, scene_id in enumerate(window_ids()):
        scene_dir, meta, review = load_scene(scene_id)
        _, _, mpp = pixel_geometry(meta)
        feats = frame_features(scene_dir/f"{meta['image_sha256']}.png", mpp)
        shadow = (classify(feats, thresholds) == 2).astype(np.float32)
        objects = live_review_objects(review) if review else meta['objects']
        rows = edge_profiles(meta, objects, feats['lum'], shadow)
        entry = {'scene': scene_id, 'area': area_group(meta), 'lon': meta['centroid']['lon'], 'lat': meta['centroid']['lat'],
                 'm_per_px': round(mpp, 4), 'buildings': len({r['b'] for r in rows}), 'edges': len(rows),
                 'band_edges': sum(1 for r in rows if r['w_m'] > 0)}
        phi_b, resultant = harmonic_bearing(rows, 'band_shadow', -1.0) if rows else (None, None)
        if phi_b is not None:
            nb = max(r['b'] for r in rows)+1
            boots = []
            for _ in range(BOOTSTRAP):
                draw = rng.multinomial(nb, np.ones(nb)/nb).astype(float)
                boots.append(harmonic_bearing(rows, 'band_shadow', -1.0, draw)[0])
            boots = [b for b in boots if b is not None]
            phi_c, resultant_c = harmonic_bearing(rows, 'band', 1.0)
            entry.update({'shadow_bearing_B': round(phi_b, 1), 'sun_azimuth_B': round((phi_b+180) % 360, 1), 'harmonic_resultant_B': resultant,
                          'bootstrap_circ_std_deg_B': round(circ_std(boots), 1),
                          'shadow_bearing_C': round(phi_c, 1) if phi_c is not None else None, 'harmonic_resultant_C': resultant_c,
                          'B_minus_C_deg': round(angle_diff(phi_b, phi_c), 1) if phi_c is not None else None})
        if entry['band_edges'] >= 3:
            phi_a, _ = fit_shadow_bearing(rows)
            nb = max(r['b'] for r in rows)+1
            boots = [fit_shadow_bearing(rows, rng.multinomial(nb, np.ones(nb)/nb).astype(float))[0] for _ in range(BOOTSTRAP)]
            lengths = [r['w_m'] for r in rows if r['w_m'] > 0]
            entry.update({'shadow_bearing_A': phi_a, 'bootstrap_circ_std_deg_A': round(circ_std(boots), 1),
                          'B_minus_A_deg': round(angle_diff(phi_b, phi_a), 1) if phi_b is not None else None,
                          'band_width_m_median': round(float(np.median(lengths)), 2)})
        windows[scene_id] = entry
        heartbeat(f'sun {i+1}/54 {scene_id} edges {entry["edges"]} phiB {entry.get("shadow_bearing_B")}')
    areas = {}
    reliable = lambda e: (e.get('shadow_bearing_B') is not None and e['bootstrap_circ_std_deg_B'] <= SUN_RELIABLE_STD   # noqa: E731
                          and e['edges'] >= SUN_RELIABLE_EDGES)
    for area in ('east', 'inner'):
        members = [e for e in windows.values() if e['area'] == area and e.get('shadow_bearing_B') is not None]
        good = [e for e in members if reliable(e)]
        mean = circ_mean([e['shadow_bearing_B'] for e in good]) if good else None
        for e in members:
            e['deviation_from_area_mean_deg'] = round(angle_diff(e['shadow_bearing_B'], mean), 1) if mean is not None else None
        areas[area] = {'windows': sum(1 for e in windows.values() if e['area'] == area), 'with_estimate': len(members),
                       'reliable': len(good), 'shadow_bearing_mean_reliable': round(mean, 1) if mean is not None else None,
                       'sun_azimuth_mean_reliable': round((mean+180) % 360, 1) if mean is not None else None,
                       'circ_std_reliable_deg': round(circ_std([e['shadow_bearing_B'] for e in good]), 1) if len(good) > 1 else None,
                       'reliable_within_20deg_of_mean': sum(1 for e in good if abs(e['deviation_from_area_mean_deg']) <= 20),
                       'reliable_within_30deg_of_mean': sum(1 for e in good if abs(e['deviation_from_area_mean_deg']) <= 30),
                       'all_within_30deg_of_mean': sum(1 for e in members if abs(e['deviation_from_area_mean_deg']) <= 30)}
    for e in windows.values():
        ok = reliable(e)
        e['reliable'] = bool(ok)
        e['shadow_bearing_used'] = e['shadow_bearing_B'] if ok else areas[e['area']]['shadow_bearing_mean_reliable']
        e['shadow_bearing_source'] = 'window' if ok else 'area_mean'
    out = {'version': 'shadow-census-sun-v1', 'computed_at': now(), 'code_sha256': CODE_SHA256,
           'method': {'B': 'first harmonic of the 0.2-1.0 m shadow-class share over straight-edge normals (primary)',
                      'A': 'band-offset fit w_i = L_b*max(0,cos(theta_i-phi)); w = width of the shadow-class band (cross-check)',
                      'C': 'first harmonic of near-band luminance darkness over edge normals (classifier-free cross-check)',
                      'params': {'building_min_m2': BUILDING_MIN_M2, 'simplify_m': SIMPLIFY_M, 'edge_min_m': EDGE_MIN_M, 'edge_trim': EDGE_TRIM,
                                 'near_m': NEAR_M, 'far_m': FAR_M, 'profile_max_m': PROFILE_MAX_M, 'band_shadow_min': BAND_SHADOW_MIN,
                                 'near_band_m_B': NEAR_BAND_M, 'bootstrap': BOOTSTRAP,
                                 'reliable': f'method B bootstrap circ std <= {SUN_RELIABLE_STD} deg and >= {SUN_RELIABLE_EDGES} edges; otherwise the area mean of reliable windows is used'},
                      'bearings': 'compass degrees (0 = N, 90 = E); shadow bearing = direction shadows fall; sun azimuth = shadow + 180'},
           'areas': areas, 'windows': windows}
    SUN_FILE.write_text(json.dumps(out, indent=1))
    print(json.dumps(areas))


def object_row(obj, mask, classes, meta, s_vec, s_vec_area, calib_boxes):
    _, pixel_area, mpp = pixel_geometry(meta)
    n = int(mask.sum())
    ys, xs = np.nonzero(mask)
    c = classes[ys, xs]
    crown, shadow, other = int((c == 1).sum()), int((c == 2).sum()), int((c == 0).sum())
    if obj['kind'] != 'car' and crown >= CROWN_MIN_PX:
        basis, sel = 'crown', c == 1
    elif (c != 2).sum() >= CROWN_MIN_PX:
        basis, sel = 'non_shadow', c != 2
    else:
        basis, sel = 'polygon', np.ones_like(c, bool)
    cx, cy = float(xs[sel].mean()), float(ys[sel].mean())
    row = {'kind': obj['kind'], 'n_px': n, 'raster_area_m2': round(n*pixel_area, 2), 'crown_px': crown, 'shadow_px': shadow, 'other_px': other,
           'shadow_share': round(shadow/n, 4), 'crown_share': round(crown/n, 4), 'other_share': round(other/n, 4),
           'shadow_m2': round(shadow*pixel_area, 3), 'centroid_basis': basis, 'centroid_px': [round(cx, 1), round(cy, 1)],
           'm_per_px': round(mpp, 4), 'vertices': len(obj.get('ring') or []),
           'touches_frame': bool(min(xs.min(), ys.min()) <= 1 or xs.max() >= meta['image_width']-2 or ys.max() >= meta['image_height']-2),
           'calibration_overlap': any(mask[b[1]:b[3], b[0]:b[2]].any() for b in calib_boxes)}
    noncrown = c != 1
    if basis == 'crown' and noncrown.any():
        along_all = (xs-cx)*s_vec[0]+(ys-cy)*s_vec[1]
        opp, sun_side = int((noncrown & (along_all > 0)).sum()), int((noncrown & (along_all < 0)).sum())
        row['noncrown_share'] = round(float(noncrown.mean()), 4)
        row['noncrown_sun_opposite_share'] = round(opp/max(1, opp+sun_side), 4)
        row['shadow_side_excess'] = round((opp-sun_side)/n, 4)
    else:
        row.update({'noncrown_share': round(float(noncrown.mean()), 4), 'noncrown_sun_opposite_share': None, 'shadow_side_excess': None})
    if shadow:
        sx, sy = xs[c == 2]-cx, ys[c == 2]-cy
        along = sx*s_vec[0]+sy*s_vec[1]
        row['sun_opposite_share'] = round(float((along > 0).mean()), 4)
        row['cast_shadow_m2'] = round(float((along > 0).sum())*pixel_area, 3)
        row['shadow_offset_along_m'] = round(float(along.mean())*mpp, 3)
        if s_vec_area is not None:
            row['sun_opposite_share_area_mean'] = round(float(((sx*s_vec_area[0]+sy*s_vec_area[1]) > 0).mean()), 4)
        row['perpendicular_side_share'] = round(float(((-sx*s_vec[1]+sy*s_vec[0]) > 0).mean()), 4)   # control: bearing + 90 deg
        row['shadow_centroid_offset_px'] = [round(float(sx.mean()), 2), round(float(sy.mean()), 2)]
    else:
        row.update({'sun_opposite_share': None, 'cast_shadow_m2': 0.0, 'shadow_offset_along_m': None, 'sun_opposite_share_area_mean': None,
                    'perpendicular_side_share': None, 'shadow_centroid_offset_px': None})
    return row


def summarise(rows):
    out = {'n': len(rows), 'raster_area_m2_total': round(sum(r['raster_area_m2'] for r in rows), 1),
           'shadow_m2_total': round(sum(r['shadow_m2'] for r in rows), 1),
           'cast_shadow_m2_total': round(sum(r['cast_shadow_m2'] for r in rows), 1)}
    shadow_px = sum(r['shadow_px'] for r in rows)
    out['pooled_shadow_share'] = round(shadow_px/max(1, sum(r['n_px'] for r in rows)), 4)
    out['pooled_sun_opposite_share'] = round(sum((r['sun_opposite_share'] or 0)*r['shadow_px'] for r in rows)/shadow_px, 4) if shadow_px else None
    out['pooled_perpendicular_side_share'] = round(sum((r['perpendicular_side_share'] or 0)*r['shadow_px'] for r in rows)/shadow_px, 4) if shadow_px else None
    out['pooled_sun_opposite_share_area_mean'] = (round(sum((r['sun_opposite_share_area_mean'] or 0)*r['shadow_px'] for r in rows)/shadow_px, 4)
                                                  if shadow_px else None)
    for level in SHARE_LEVELS:
        tag = f'ge{int(level*100)}'
        hit = [r for r in rows if r['shadow_share'] >= level]
        opp = [r['sun_opposite_share'] for r in hit if r['sun_opposite_share'] is not None]
        out[f'share_{tag}'] = len(hit)
        out[f'share_{tag}_shadow_m2'] = round(sum(r['shadow_m2'] for r in hit), 1)
        out[f'share_{tag}_sun_opposite_median'] = round(float(np.median(opp)), 3) if opp else None
        out[f'share_{tag}_sun_opposite_ge70'] = sum(1 for o in opp if o >= 0.7)
        out[f'share_{tag}_decisions'] = dict(Counter(str(r['decision']) for r in hit))
    for level in SHARE_LEVELS:
        tag = f'ge{int(level*100)}'
        hit = [r for r in rows if (r.get('shadow_side_excess') or 0) >= level]
        out[f'excess_{tag}'] = len(hit)
        out[f'excess_{tag}_decisions'] = dict(Counter(str(r['decision']) for r in hit))
    out['excess_ge20_and_shadow_share_lt10'] = sum(1 for r in rows if (r.get('shadow_side_excess') or 0) >= .2 and r['shadow_share'] < .1)
    out['decisions_all'] = dict(Counter(str(r['decision']) for r in rows))
    return out


def tree_bearing(rows, lin):
    """Shadow bearing implied by the trees themselves: shadow-px-weighted circular mean of the crown -> shadow centroid offsets."""
    vecs = [(r['shadow_centroid_offset_px'], r['shadow_px']) for r in rows
            if r['kind'] == 'tree' and r['shadow_share'] >= 0.10 and r['shadow_centroid_offset_px'] and r['centroid_basis'] == 'crown']
    if len(vecs) < 3:
        return None, len(vecs)
    bearings = [bearing_of_pixel_vector(lin, v[0], v[1]) for v, _ in vecs]
    return round(circ_mean(bearings, [w for _, w in vecs]), 1), len(vecs)


def census(args):
    thresholds = load_thresholds()
    sun_ = load_json(SUN_FILE)
    labels = load_json(LABELS_FILE)
    calib_boxes = defaultdict(list)
    for ex in labels['examples']:
        calib_boxes[ex['scene']].extend(r['box'] for r in ex['regions'])
    rows, skipped, frames, window_checks = [], Counter(), {}, {}
    for i, scene_id in enumerate(window_ids()):
        scene_dir, meta, review = load_scene(scene_id)
        lin, pixel_area, mpp = pixel_geometry(meta)
        feats = frame_features(scene_dir/f"{meta['image_sha256']}.png", mpp)
        classes = classify(feats, thresholds)
        sw = sun_['windows'][scene_id]
        bearing = sw['shadow_bearing_used']
        area_mean = sun_['areas'][sw['area']]['shadow_bearing_mean_reliable']
        s_vec = pixel_vector_of_bearing(lin, bearing)
        s_vec_area = pixel_vector_of_bearing(lin, area_mean) if area_mean is not None else None
        w, h = meta['image_width'], meta['image_height']
        review_by_id = {o['id']: o for o in (review or {}).get('objects', [])}
        model_by_id = {o['id']: o for o in meta['objects']}
        window_rows = []
        for layer, objects in (('review', (review or {}).get('objects', [])), ('model', meta['objects'])):
            for obj in objects:
                if obj['kind'] not in CLASSES:
                    continue
                status = obj.get('status')
                if layer == 'review' and status in LIVE_EXCLUDED:
                    skipped[f'review_{status}_{obj["kind"]}'] += 1
                    continue
                mask = rasterise(obj, w, h)
                if not mask.any():
                    skipped[f'{layer}_empty_mask'] += 1
                    continue
                row = object_row(obj, mask, classes, meta, s_vec, s_vec_area, calib_boxes.get(scene_id, []))
                rv = review_by_id.get(obj['id'])
                decision = status if layer == 'review' else (rv.get('status') if rv else 'absent_from_review')
                edited = None
                if layer == 'review' and obj['id'] in model_by_id:
                    edited = (obj.get('ring') != model_by_id[obj['id']].get('ring'))
                row.update({'scene': scene_id, 'area': sw['area'], 'layer': layer, 'id': obj['id'], 'decision': decision,
                            'model_status': obj.get('status') if layer == 'model' else None, 'geometry_edited': edited,
                            'geometry_reviewed': obj.get('geometry_reviewed'), 'score': obj.get('score'), 'prompt': obj.get('prompt'),
                            'shadow_bearing': bearing, 'shadow_bearing_source': sw['shadow_bearing_source']})
                window_rows.append(row)
        tb, tn = tree_bearing([r for r in window_rows if r['layer'] == 'review'], lin)
        window_checks[scene_id] = {'area': sw['area'], 'building_shadow_bearing_B': sw.get('shadow_bearing_B'), 'used': bearing,
                                   'source': sw['shadow_bearing_source'], 'tree_shadow_bearing': tb, 'trees_used': tn,
                                   'tree_minus_used_deg': round(angle_diff(tb, bearing), 1) if tb is not None and bearing is not None else None}
        rows.extend(window_rows)
        frames[scene_id] = (scene_dir, meta, feats, classes, s_vec, sw)
        heartbeat(f'census {i+1}/54 {scene_id} rows {len(window_rows)}')
    out_rows = OUT_DIR/'objects.jsonl'
    with open(out_rows, 'w') as handle:
        for r in rows:
            handle.write(json.dumps(r)+'\n')
    aggregates = {}
    for layer in ('review', 'model'):
        for kind in CLASSES:
            sel = [r for r in rows if r['layer'] == layer and r['kind'] == kind]
            aggregates[f'{layer}/{kind}'] = summarise(sel)
            for area in ('east', 'inner'):
                aggregates[f'{layer}/{kind}/{area}'] = summarise([r for r in sel if r['area'] == area])
    flagged = {}
    for layer in ('review', 'model'):
        for kind in CLASSES:
            sel = [r for r in rows if r['layer'] == layer and r['kind'] == kind and r['shadow_share'] >= 0.10
                   and (r['sun_opposite_share'] or 0) >= 0.7]
            flagged[f'{layer}/{kind}'] = {'n': len(sel), 'shadow_m2': round(sum(r['shadow_m2'] for r in sel), 1),
                                          'cast_shadow_m2': round(sum(r['cast_shadow_m2'] for r in sel), 1),
                                          'decisions': dict(Counter(str(r['decision']) for r in sel))}
    decided = [r for r in rows if r['layer'] == 'review' and r['decision'] in ('confirmed', 'needs_adjustment', 'rejected')]
    example = {layer: next((r for r in rows if r['scene'] == REVIEWER_EXAMPLE[0] and r['id'] == REVIEWER_EXAMPLE[1] and r['layer'] == layer), None)
               for layer in ('review', 'model')}
    rank = {}
    for layer in ('review', 'model'):
        if example[layer]:
            trees = [r['shadow_share'] for r in rows if r['layer'] == layer and r['kind'] == 'tree']
            rank[layer] = {'trees': len(trees), 'trees_with_higher_share': sum(1 for t in trees if t > example[layer]['shadow_share']),
                           'percentile': round(100*sum(1 for t in trees if t <= example[layer]['shadow_share'])/len(trees), 1)}
    tb_checks = [v for v in window_checks.values() if v['tree_minus_used_deg'] is not None]
    renders = [] if args.no_renders else make_renders(rows, frames)
    if args.no_renders and (OUT_DIR/'census.json').exists():
        renders = load_json(OUT_DIR/'census.json').get('renders', [])
    byeye = load_json(OUT_DIR/'byeye.json') if (OUT_DIR/'byeye.json').exists() else None
    result = {'version': 'shadow-census-v1', 'computed_at': now(), 'code_sha256': CODE_SHA256,
              'inputs': {'windows_file': str(WINDOWS_FILE.relative_to(ROOT)), 'windows': len(frames),
                         'calibration_sha256': hashlib.sha256(CALIBRATION_FILE.read_bytes()).hexdigest(),
                         'sun_sha256': hashlib.sha256(SUN_FILE.read_bytes()).hexdigest()},
              'thresholds': thresholds, 'skipped': dict(skipped), 'rows': len(rows),
              'aggregates': aggregates, 'cast_shadow_like_share_ge10_and_sun_opposite_ge70': flagged,
              'reviewer_decided_review_layer': {'n': len(decided), 'by_decision': {d: summarise([r for r in decided if r['decision'] == d])
                                                                                for d in ('confirmed', 'needs_adjustment', 'rejected')}},
              'reviewer_example': {'scene': REVIEWER_EXAMPLE[0], 'object': REVIEWER_EXAMPLE[1], 'rows': example, 'rank': rank},
              'sun': {'areas': sun_['areas'], 'tree_vs_building_bearing': {
                  'windows': len(tb_checks),
                  'median_abs_diff_deg': round(float(np.median([abs(v['tree_minus_used_deg']) for v in tb_checks])), 1) if tb_checks else None,
                  'within_30deg': sum(1 for v in tb_checks if abs(v['tree_minus_used_deg']) <= 30),
                  'per_window': window_checks}},
              'renders': renders, 'byeye': byeye}
    (OUT_DIR/'census.json').write_text(json.dumps(result, indent=1))
    write_report(result, rows)
    print(f"rows {len(rows)}; review/tree {aggregates['review/tree']['n']} ge10 {aggregates['review/tree']['share_ge10']}; "
          f"renders {len(renders)}")


RENDER_BINS = {'tree': ((0, .02, 1), (.02, .05, 1), (.05, .10, 2), (.10, .20, 3), (.20, .30, 2), (.30, .50, 2), (.50, 1.01, 1)),
               'vegetation': ((0, .02, 1), (.02, .10, 1), (.10, 1.01, 1)),
               'car': ((0, .10, 1), (.10, .30, 1), (.30, 1.01, 1))}   # class: (share lo, hi, count); 12 + 3 + 3 = 18 ... + 2 below
RENDER_EXTRA = (('tree', .10, 1.01, 2),)   # two more trees from >= 10 % (the population of interest) -> 20 objects


def pick_render_rows(rows):
    """20 review-layer objects across the shadow-share range: per class and share bin, a seeded random draw (seed 8) among objects
    >= 4 m2 that do not overlap a calibration box and are not the reviewer's example."""
    rng = random.Random(8)
    eligible = [r for r in rows if r['layer'] == 'review' and r['raster_area_m2'] >= 4 and not r['calibration_overlap']
                and (r['scene'], r['id']) != REVIEWER_EXAMPLE]
    picks, seen = [], set()
    plan = [(kind, lo, hi, n) for kind, bins in RENDER_BINS.items() for lo, hi, n in bins]+list(RENDER_EXTRA)
    for kind, lo, hi, n in plan:
        pool = sorted((r for r in eligible if r['kind'] == kind and lo <= r['shadow_share'] < hi and (r['scene'], r['id']) not in seen),
                      key=lambda r: (r['scene'], r['id']))
        for r in rng.sample(pool, min(n, len(pool))):
            seen.add((r['scene'], r['id']))
            picks.append(r)
    return sorted(picks, key=lambda r: (('tree', 'vegetation', 'car').index(r['kind']), r['shadow_share']))


def render(row, frame, path, title):
    scene_dir, meta, feats, classes, s_vec, sw = frame
    obj_source = load_scene(row['scene'])
    objects = (obj_source[2] or {}).get('objects', []) if row['layer'] == 'review' else obj_source[1]['objects']
    obj = next(o for o in objects if o['id'] == row['id'])
    w, h = meta['image_width'], meta['image_height']
    mask = rasterise(obj, w, h)
    _, _, mpp = pixel_geometry(meta)
    ys, xs = np.nonzero(mask)
    margin = int(max(12, 1.5/mpp))
    x0, y0 = max(0, xs.min()-margin), max(0, ys.min()-margin)
    x1, y1 = min(w, xs.max()+margin+1), min(h, ys.max()+margin+1)
    rgb = (feats['rgb'][y0:y1, x0:x1]*255).astype(np.uint8)
    cls = classes[y0:y1, x0:x1]
    m = mask[y0:y1, x0:x1]
    annotated = overlay(rgb, cls, m)
    outside_shadow = (~m) & (cls == 2)
    annotated[outside_shadow] = (annotated[outside_shadow]*0.7+np.array(SHADOW_COLOUR)*0.3).astype(np.uint8)
    scale = max(1.0, min(6.0, 380/max(x1-x0, y1-y0)))
    if max(x1-x0, y1-y0)*scale > 460:
        scale = 460/max(x1-x0, y1-y0)
    size = (max(1, int((x1-x0)*scale)), max(1, int((y1-y0)*scale)))
    resample = Image.NEAREST if scale >= 1.5 else Image.LANCZOS
    panels = [Image.fromarray(rgb).resize(size, resample), Image.fromarray(annotated).resize(size, resample)]
    header = 34
    sheet_ = Image.new('RGB', (size[0]*2+8, size[1]+header), (0, 0, 0))
    sheet_.paste(panels[0], (0, header))
    sheet_.paste(panels[1], (size[0]+8, header))
    draw = ImageDraw.Draw(sheet_)
    draw.text((4, 2), title, fill=(255, 255, 255))
    draw.text((4, 17), f"share {row['shadow_share']:.2f}  sun-opp {row['sun_opposite_share'] if row['sun_opposite_share'] is not None else '-'}  "
              f"shadow bearing {sw['shadow_bearing_used']:.0f} ({sw['shadow_bearing_source']})  green crown / magenta shadow / grey other",
              fill=(255, 220, 120))
    ring = [((x-x0)*scale+size[0]+8, (y-y0)*scale+header) for x, y in obj['ring']]
    draw.line(ring+[ring[0]], fill=(255, 255, 0), width=1)
    cx, cy = row['centroid_px']
    px, py = (cx-x0)*scale+size[0]+8, (cy-y0)*scale+header
    draw.ellipse([px-3, py-3, px+3, py+3], fill=(255, 255, 255))
    length = 0.22*min(size)
    arrow(draw, (px, py), (px+s_vec[0]*length, py+s_vec[1]*length), (0, 255, 255))
    corner = (size[0]+8+22, header+22)
    arrow(draw, (corner[0]-s_vec[0]*16, corner[1]-s_vec[1]*16), (corner[0]+s_vec[0]*16, corner[1]+s_vec[1]*16), (255, 255, 0), width=3)
    draw.text((size[0]+8+4, header+44), 'light', fill=(255, 255, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_.save(path)


def arrow(draw, start, end, colour, width=2):
    draw.line([start, end], fill=colour, width=width)
    dx, dy = end[0]-start[0], end[1]-start[1]
    norm = math.hypot(dx, dy) or 1
    ux, uy = dx/norm, dy/norm
    for side in (1, -1):
        draw.line([end, (end[0]-ux*8+side*uy*5, end[1]-uy*8-side*ux*5)], fill=colour, width=width)


def make_renders(rows, frames):
    out = []
    targets = []
    for layer in ('review', 'model'):
        row = next((r for r in rows if r['scene'] == REVIEWER_EXAMPLE[0] and r['id'] == REVIEWER_EXAMPLE[1] and r['layer'] == layer), None)
        review_row = next((r for r in rows if r['scene'] == REVIEWER_EXAMPLE[0] and r['id'] == REVIEWER_EXAMPLE[1] and r['layer'] == 'review'), None)
        if row and (layer == 'review' or review_row is None or review_row['geometry_edited']):
            targets.append((f'00-example-{layer}', row))
    targets.extend((f'{i+1:02d}', r) for i, r in enumerate(pick_render_rows(rows)))
    for name, row in targets:
        path = OUT_DIR/'renders'/f"{name}-{row['scene']}-{row['id']}-{row['layer']}.png"
        title = f"{name} {row['scene']} {row['id']} {row['kind']} {row['layer']} decision {row['decision']} {row['raster_area_m2']} m2"
        render(row, frames[row['scene']], path, title)
        out.append({'name': name, 'path': str(path.relative_to(ROOT)), 'scene': row['scene'], 'id': row['id'], 'kind': row['kind'],
                    'layer': row['layer'], 'decision': row['decision'], 'shadow_share': row['shadow_share'],
                    'sun_opposite_share': row['sun_opposite_share'], 'raster_area_m2': row['raster_area_m2']})
    heartbeat(f'renders {len(out)}')
    return out


def pct(x):
    return '-' if x is None else f'{100*x:.0f} %'


def write_report(result, rows):
    a = result['aggregates']
    lines = ['# Cast-shadow inclusion census (tree-shadow-2026-10-08)', '',
             f"Computed {result['computed_at']} by `scripts/ml/experiments/shadow_census.py` (code sha256 {result['code_sha256'][:12]}). "
             f"Read-only; no inference; nothing under data/scenes written. {result['inputs']['windows']} windows "
             f"(`{result['inputs']['windows_file']}`), {result['rows']} object rows (review layer = live review.json objects; model layer = "
             'latest.json objects). Counts and geometry only; nothing here is accuracy.', '',
             '## Pixel rule (calibrated on 5 hand-picked examples, never the reviewer example or its window)', '',
             '| test | threshold |', '|---|---|']
    th = result['thresholds']
    lines += [f"| shadow: value V_rel <= | {th['t_v']} |", f"| shadow: chroma max-min <= | {th['t_c_chroma']} |",
              f"| shadow: blue shift (B - max(R,G))/sum >= | {th['t_b']} |", f"| shadow: local texture (0.4 m std of luminance) <= | {th['t_t']} |",
              f"| crown: ExG >= | {th['t_g']} |", f"| crown: texture >= (secondary route) | {th['t_c']} (no cut with positive Youden J; effectively off) |", '',
              'Shadow is tested first (all four), then crown (green or textured), else other. Labels, stumps and the labelled-pixel '
              'resubstitution are in `calibration.json` / `calibration-labels.json`; check renders in `calibration-check/`.', '',
              '## Counts by class and layer', '',
              '| layer / class | objects | shadow share >= 10 % | >= 20 % | >= 30 % | shadow m2 (all) | shadow m2 in >= 10 % objects | '
              'shadow on sun-opposite side (pooled) | median sun-opposite share, >= 10 % objects | >= 10 % and sun-opposite >= 70 % |',
              '|---|---|---|---|---|---|---|---|---|---|']
    for layer in ('review', 'model'):
        for kind in CLASSES:
            g = a[f'{layer}/{kind}']
            f = result['cast_shadow_like_share_ge10_and_sun_opposite_ge70'][f'{layer}/{kind}']
            lines.append(f"| {layer} / {kind} | {g['n']} | {g['share_ge10']} | {g['share_ge20']} | {g['share_ge30']} | {g['shadow_m2_total']} | "
                         f"{g['share_ge10_shadow_m2']} | {pct(g['pooled_sun_opposite_share'])} | {g['share_ge10_sun_opposite_median']} | {f['n']} |")
    lines += ['', 'Secondary measure, shadow-side excess (non-crown area on the sun-opposite side minus the sun side, as a share of the '
              'outline; trees and vegetation with a crown centroid). It does not use the shadow test, so it also counts dappled shadow and '
              'lit ground beyond the shadow; it was added after the reviewer example (see notes.md).', '',
              '| layer / class | excess >= 10 % | >= 20 % | >= 30 % | excess >= 20 % but shadow share < 10 % | decisions at excess >= 20 % |', '|---|---|---|---|---|---|']
    for layer in ('review', 'model'):
        for kind in ('tree', 'vegetation'):
            g = a[f'{layer}/{kind}']
            lines.append(f"| {layer} / {kind} | {g['excess_ge10']} | {g['excess_ge20']} | {g['excess_ge30']} | {g['excess_ge20_and_shadow_share_lt10']} | "
                         f"{json.dumps(g['excess_ge20_decisions'])} |")
    lines += ['', '### By area (review layer)', '', '| class / area | objects | >= 10 % | >= 20 % | >= 30 % | shadow m2 | pooled sun-opposite |',
              '|---|---|---|---|---|---|---|']
    for kind in CLASSES:
        for area in ('east', 'inner'):
            g = a[f'review/{kind}/{area}']
            if g['n']:
                lines.append(f"| {kind} / {area} | {g['n']} | {g['share_ge10']} | {g['share_ge20']} | {g['share_ge30']} | {g['shadow_m2_total']} | "
                             f"{pct(g['pooled_sun_opposite_share'])} |")
    lines += ['', "## The reviewer's decisions on these objects", '',
              'Review-layer statuses of the objects at each share level (candidate = not yet decided). Rejected / superseded objects are '
              f"not live and are excluded from the review layer; skipped counts: {json.dumps(result['skipped'])}.", '',
              '| layer / class | level | decisions |', '|---|---|---|']
    for layer in ('review',):
        for kind in CLASSES:
            g = a[f'{layer}/{kind}']
            lines.append(f"| {layer} / {kind} | all | {json.dumps(g['decisions_all'])} |")
            for tag in ('ge10', 'ge20', 'ge30'):
                lines.append(f"| {layer} / {kind} | {tag} | {json.dumps(g[f'share_{tag}_decisions'])} |")
    rd = result['reviewer_decided_review_layer']
    lines += ['', f"Reviewer-decided review-layer objects (tree / vegetation / car): {rd['n']}.", '',
              '| decision | n | >= 10 % | >= 20 % | >= 30 % | pooled sun-opposite |', '|---|---|---|---|---|---|']
    for d, g in rd['by_decision'].items():
        if g['n']:
            lines.append(f"| {d} | {g['n']} | {g['share_ge10']} | {g['share_ge20']} | {g['share_ge30']} | {pct(g['pooled_sun_opposite_share'])} |")
    ex = result['reviewer_example']
    lines += ['', "## The reviewer's example", '']
    for layer, r in ex['rows'].items():
        if r:
            rank = ex['rank'].get(layer, {})
            lines.append(f"- {layer} layer: {r['raster_area_m2']} m2, {r['vertices']} vertices, decision {r['decision']}, shadow share "
                         f"{pct(r['shadow_share'])} ({r['shadow_m2']} m2), crown {pct(r['crown_share'])}, other {pct(r['other_share'])}, "
                         f"sun-opposite share {pct(r['sun_opposite_share'])} (area-mean bearing: {pct(r.get('sun_opposite_share_area_mean'))}), "
                         f"non-crown {pct(r['noncrown_share'])} of which {pct(r['noncrown_sun_opposite_share'])} on the sun-opposite side, "
                         f"shadow-side excess {pct(r['shadow_side_excess'])}, "
                         f"shadow bearing {r['shadow_bearing']} ({r['shadow_bearing_source']}), touches frame {r['touches_frame']}; "
                         f"{rank.get('trees_with_higher_share')} of {rank.get('trees')} {layer}-layer trees have a higher share "
                         f"(percentile {rank.get('percentile')}).")
    sa = result['sun']
    lines += ['', '## Sun azimuth from building shadows', '',
              'Per window: share of shadow-class pixels in the 0.2-1.0 m band outside each straight building edge (>= 3 m), first harmonic '
              'over the edge normals (method B, primary); cross-checks A (band-width fit) and C (luminance only). Details per window in '
              '`sun.json`.', '', '| area | windows | with estimate | reliable | shadow bearing (mean) | sun azimuth | circ std | reliable within 20 / 30 deg |',
              '|---|---|---|---|---|---|---|---|']
    for area, g in sa['areas'].items():
        lines.append(f"| {area} | {g['windows']} | {g['with_estimate']} | {g['reliable']} | {g['shadow_bearing_mean_reliable']} | "
                     f"{g['sun_azimuth_mean_reliable']} | {g['circ_std_reliable_deg']} | {g['reliable_within_20deg_of_mean']} / {g['reliable_within_30deg_of_mean']} |")
    tv = sa['tree_vs_building_bearing']
    lines += ['', f"Independent check from the trees themselves (crown -> shadow centroid offsets, review-layer trees with share >= 10 %, "
                  f">= 3 per window): {tv['windows']} windows, median |tree - building| {tv['median_abs_diff_deg']} deg, {tv['within_30deg']} within 30 deg.", '']
    lines += ['## Renders', '', 'Raw crop (left) and annotated crop (right: green crown, magenta shadow, grey other inside the outline; faint '
              'magenta = shadow-class pixels outside it; white dot = crown centroid; cyan arrow = shadow bearing; yellow corner arrow = '
              'direction of light).', '', '| render | class | decision | share | sun-opposite | by eye |', '|---|---|---|---|---|---|']
    byeye = {b['name']: b for b in (result.get('byeye') or {}).get('reads', [])}
    for r in result['renders']:
        b = byeye.get(r['name'])
        verdict = f"{b['verdict']}: {b['note']}" if b else 'not read'
        lines.append(f"| `{r['path'].split('/')[-1]}` | {r['kind']} | {r['decision']} | {pct(r['shadow_share'])} | {pct(r['sun_opposite_share'])} | {verdict} |")
    if result.get('byeye'):
        t = Counter(b['verdict'] for b in result['byeye']['reads'])
        lines += ['', f"By-eye tally (is the classifier's 'shadow' the cast shadow?): {dict(t)} over {sum(t.values())} renders read."]
    (OUT_DIR/'census.md').write_text('\n'.join(lines)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('sheet')
    p.add_argument('--per-group', type=int, default=6)
    sub.add_parser('calibrate')
    sub.add_parser('sun')
    p = sub.add_parser('census')
    p.add_argument('--no-renders', action='store_true')
    args = parser.parse_args()
    {'sheet': sheet, 'calibrate': calibrate, 'sun': sun, 'census': census}[args.command](args)


if __name__ == '__main__':
    main()
