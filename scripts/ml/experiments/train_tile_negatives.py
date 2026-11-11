"""Read-only worklist of likely FALSE-POSITIVE small building candidates on the dataset-v2-trainclean TRAIN tiles.

Why: the round-5 fold card (fold-prep-2026-10-06/DECISION-CARD.md item 2) has 0 admissible negatives because every counted
rejection sits on a frozen v2 TEST tile. A rejection made in the editor on a TRAIN tile removes that annotation from the train
split (r5_fold_prep.apply_negatives: the object becomes background), so the reviewer's Backspace there is a usable negative.
This script ranks the undecided small candidates most likely to be flat patches, not roofs, so those clicks are spent well.

Population: scenes of data/reviews/eyes/training/dataset-v2-trainclean/train.json (the split r5_fold_prep / prepare_coco use)
that have a latest.json; every train image is checked against the frozen judged splits (dataset-v2 test / val, dataset-v1
test) on scene_id, tile_group and sha256 exactly as r5_fold_prep.refusals does, and a hit excludes the scene. Candidates:
latest.json building objects with model.area_m2 < 25 (r5_precision.SMALL_M2) and status candidate whose review.json object (when
one exists) is still undecided (status candidate or needs_adjustment); the stored top-level `score` is the detector score.
Only a candidate with a train.json annotation (same scene_id, object_id) is RANKED: an object under the 4 m2 export floor or
not exported has no annotation to remove, so its rejection would not enter the fold (counted, not ranked).

Evidence, from the files only (named in SCORE_TERMS with its weight; the total is the plain sum):
  surface          share of the candidate inside the same scene's road / footpath / driveway / water / pool objects
  car              share inside car objects
  tree_inside      share inside tree / vegetation canopy polygons
  tree_shadow_side dark candidate (luma <= DARK_RATIO x the scene's roof reference) within SHADOW_NEAR_M of a tree, on the
                   scene's shadow side: the shadow azimuth is read from the imagery (the darkest of 8 directions when every
                   tree canopy is shifted SHADOW_PROBE_M outward, canopies masked out); tree_adjacent_dark when the direction
                   is off or unknown
  no_lot           lot-tags-v1 row (eyes index object_quality, current model_signature) flags no_lot: no parcel holds >= 2%
                   of the object (outside parcel coverage or on the road reserve)
  low_texture      1 - (luma std inside the polygon / median roof-reference std), clipped to [0, 1]
  brightness       |log2(candidate luma / roof-reference luma)| minus the BRIGHTNESS_DEAD_BAND, clipped to [0, 1]
  elongated        min-rotated-rectangle aspect >= ELONGATED_ASPECT
  irregular        area / min-rotated-rectangle area < RECTILINEAR_MIN
  tiny             area < TINY_M2
  low_score        0.6 - score, clipped at 0
  roof_part        NEGATIVE: share inside another building object of >= 25 m2 (a part of a roof is a roof)
Roof reference per scene: review.json confirmed buildings when >= REF_CONFIRMED_MIN, else the top REF_LARGE_N buildings of
>= REF_LARGE_M2 by score (the kind is recorded per row).

Nothing is decided and nothing is written under data/scenes, the registry, RULES / DESIGN / WINDOW or the eyes index.
No inference. Outputs ONLY under data/reviews/eyes/training/sam3-mac/round5/negatives-worklist-2026-10-06/: census.json,
WORKLIST.md, renders/<rank>-<scene>-<object>.png (native x3, candidate red, evidence cyan, other buildings thin yellow, scale
bar), renders/contact-sheet.png.

Run from the repo root: scripts/ml/.venv/bin/python -m scripts.ml.experiments.train_tile_negatives [--top 40] [--no-render]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.affinity import translate
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from ..training.sam3_mac import r5_fold_prep as fp

VERSION = 'train-tile-negatives-v1'
ROOT = Path('.')
TRAINCLEAN = Path('data/reviews/eyes/training/dataset-v2-trainclean')
SCENES = Path('data/scenes')
EYES_SQLITE = Path('data/reviews/eyes/eyes.sqlite')
OUT_DIR = Path('data/reviews/eyes/training/sam3-mac/round5/negatives-worklist-2026-10-06')
URL = 'http://localhost:3008/scenes?scene={scene}&status=all&layer=edited&object={oid}'
INSTRUCTION = 'Backspace if not a roof; Right arrow if it is'
SMALL_M2 = 25.                 # r5_precision.SMALL_M2: the small-roof class
EXPORT_FLOOR_M2 = 4.           # r5_precision.MIN_AREA_M2: under it nothing was exported to train.json
UNDECIDED = ('candidate', 'needs_adjustment')
SURFACE_KINDS = ('road', 'footpath', 'driveway', 'water', 'pool')
CANOPY_KINDS = ('tree', 'vegetation')
# Design values, stated before the first run and not tuned on the reviewer's answers.
DARK_RATIO = .8
SHADOW_NEAR_M = 4.
SHADOW_PROBE_M = 2.5
SHADOW_ANGLE_DEG = 60.
SHADOW_CONTRAST_MIN = .04
SHADOW_TREES_MAX = 24
ELONGATED_ASPECT = 3.5
RECTILINEAR_MIN = .6
TINY_M2 = 6.
REF_CONFIRMED_MIN = 3
REF_LARGE_M2, REF_LARGE_N = 60., 10
ERODE_PX = 1
BRIGHTNESS_DEAD_BAND = .15     # log2 units: within +-11% of the reference roofs is no evidence
RENDER_HALF_M = 12.           # render window half-width in metres (grown to fit the candidate + 4 m)
SCORE_TERMS = {       # name: (weight, what is multiplied)
    'surface': (3.0, 'share inside road/footpath/driveway/water/pool objects'),
    'car': (2.0, 'share inside car objects'),
    'tree_inside': (2.0, 'share inside tree/vegetation canopy'),
    'tree_shadow_side': (1.5, '1 when dark, within 4 m of a tree and on the scene shadow side'),
    'tree_adjacent_dark': (0.75, '1 when dark and within 4 m of a tree but not on the shadow side (or azimuth unknown)'),
    'no_lot': (1.5, '1 when the lot-tags-v1 row flags no_lot'),
    'low_texture': (1.0, 'clip(1 - std / reference std, 0, 1)'),
    'brightness': (0.5, 'clip(|log2(luma / reference luma)| - 0.15, 0, 1)'),
    'elongated': (0.75, '1 when min-rect aspect >= 3.5'),
    'irregular': (0.5, '1 when area / min-rect area < 0.6'),
    'tiny': (0.5, '1 when area < 6 m2'),
    'low_score': (2.0, 'clip(0.6 - score, 0, inf)'),
    'roof_part': (-1.0, 'share inside another building of >= 25 m2'),
}
DIRECTIONS = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in range(0, 360, 45)]   # image axes: x right, y down


def now_iso():
    return datetime.now(UTC).isoformat(timespec='seconds')


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def polygon(obj):
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return None
    try:
        poly = Polygon([(float(p[0]), float(p[1])) for p in ring], [[(float(p[0]), float(p[1])) for p in h] for h in obj.get('holes') or [] if len(h) >= 3])
    except (TypeError, ValueError):
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    return None if poly.is_empty else poly


def share_in(poly, other):
    if other is None or other.is_empty or poly.area <= 0:
        return 0.
    return float(poly.intersection(other).area / poly.area)


def raster(geom, x0, y0, w, h):
    """Boolean mask (h, w) of geom drawn with the pixel origin at (x0, y0)."""
    img = Image.new('L', (max(1, w), max(1, h)), 0)
    draw = ImageDraw.Draw(img)
    parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    for part in parts:
        if part.is_empty or part.geom_type != 'Polygon':
            continue
        draw.polygon([(x-x0, y-y0) for x, y in part.exterior.coords], fill=1)
        for hole in part.interiors:
            draw.polygon([(x-x0, y-y0) for x, y in hole.coords], fill=0)
    return np.asarray(img, dtype=bool)


def pixel_stats(luma, geom):
    """(mean, std, n) of luma inside geom (clipped to the image)."""
    h, w = luma.shape
    bx0, by0, bx1, by1 = geom.bounds
    x0, y0 = max(0, int(math.floor(bx0))), max(0, int(math.floor(by0)))
    x1, y1 = min(w, int(math.ceil(bx1))+1), min(h, int(math.ceil(by1))+1)
    if x1 <= x0 or y1 <= y0:
        return None, None, 0
    mask = raster(geom, x0, y0, x1-x0, y1-y0)
    vals = luma[y0:y1, x0:x1][mask]
    if vals.size == 0:
        return None, None, 0
    return float(vals.mean()), float(vals.std()), int(vals.size)


def interior_stats(luma, poly):
    eroded = poly.buffer(-ERODE_PX)
    mean, std, n = pixel_stats(luma, eroded) if not eroded.is_empty else (None, None, 0)
    if n >= 8:
        return mean, std, n, 'eroded'
    mean, std, n = pixel_stats(luma, poly)
    return mean, std, n, 'full'


def shape_terms(poly, mpp):
    rect = poly.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    if len(coords) < 4:
        return {'aspect': None, 'rectilinearity': None}
    a = math.dist(coords[0], coords[1]) or 1e-6
    b = math.dist(coords[1], coords[2]) or 1e-6
    return {'aspect': round(max(a, b)/min(a, b), 2), 'rectilinearity': round(poly.area/rect.area, 3) if rect.area > 0 else None,
            'long_m': round(max(a, b)*mpp, 2), 'short_m': round(min(a, b)*mpp, 2)}


def shadow_azimuth(luma, trees, canopy_mask, mpp):
    """Scene shadow direction read from the imagery: the darkest of 8 directions when each of the largest trees is shifted
    SHADOW_PROBE_M outward (canopies masked). Returns {'direction': (dx, dy) | None, 'index', 'contrast', 'means', 'trees'}."""
    h, w = luma.shape
    shift = SHADOW_PROBE_M/mpp
    sums, counts = np.zeros(len(DIRECTIONS)), np.zeros(len(DIRECTIONS))
    picked = sorted((t for t in trees if t is not None), key=lambda g: -g.area)[:SHADOW_TREES_MAX]
    for tree in picked:
        for k, (dx, dy) in enumerate(DIRECTIONS):
            probe = translate(tree, dx*shift, dy*shift)
            bx0, by0, bx1, by1 = probe.bounds
            x0, y0 = max(0, int(math.floor(bx0))), max(0, int(math.floor(by0)))
            x1, y1 = min(w, int(math.ceil(bx1))+1), min(h, int(math.ceil(by1))+1)
            if x1 <= x0 or y1 <= y0:
                continue
            mask = raster(probe, x0, y0, x1-x0, y1-y0) & ~canopy_mask[y0:y1, x0:x1]
            if mask.any():
                sums[k] += float(luma[y0:y1, x0:x1][mask].sum())
                counts[k] += int(mask.sum())
    if not picked or (counts == 0).any():
        return {'direction': None, 'index': None, 'contrast': None, 'means': None, 'trees': len(picked)}
    means = sums/counts
    k = int(means.argmin())
    contrast = float((means.max()-means.min())/max(means.max(), 1e-6))
    direction = DIRECTIONS[k] if contrast >= SHADOW_CONTRAST_MIN else None
    return {'direction': direction, 'index': k if direction else None, 'contrast': round(contrast, 4), 'means': [round(float(m), 1) for m in means],
            'trees': len(picked)}


def angle_between(v, u):
    dot = v[0]*u[0] + v[1]*u[1]
    nv, nu = math.hypot(*v), math.hypot(*u)
    if nv == 0 or nu == 0:
        return None
    return math.degrees(math.acos(max(-1., min(1., dot/(nv*nu)))))


def lot_tags(conn, scene_id, signature):
    """{object_id: (flags, lots_text)} of the scene's lot-tags-v1 model-layer rows at the current model_signature; None when
    the scene has no rows at all; 'stale' when rows exist only for another signature."""
    rows = conn.execute("select object_id, model_signature, payload from object_quality where version='lot-tags-v1' and scene_id=?",
                        (scene_id,)).fetchall()
    if not rows:
        return None, 'none'
    out = {}
    for oid, sig, payload in rows:
        if sig != signature:
            continue
        tag = (json.loads(payload) or {}).get('tag') or {}
        if tag and (json.loads(payload).get('layer') or 'model') == 'model':
            out[oid] = (list(tag.get('flags') or []), [str(lot_item.get('lot') or lot_item) if isinstance(lot_item, dict) else str(lot_item)
                                                        for lot_item in (tag.get('lots') or [])][:4])
    return out, ('current' if out else 'stale')


def roof_reference(luma, buildings, review_status):
    confirmed = [b for b in buildings if review_status.get(b['id']) == 'confirmed' and b['poly'] is not None]
    if len(confirmed) >= REF_CONFIRMED_MIN:
        picked, kind = confirmed, 'review_confirmed'
    else:
        large = [b for b in buildings if b['poly'] is not None and b['area_m2'] >= REF_LARGE_M2]
        picked, kind = sorted(large, key=lambda b: -(b['score'] or 0))[:REF_LARGE_N], 'large_high_score'
        if not picked:
            picked, kind = sorted((b for b in buildings if b['poly'] is not None), key=lambda b: -b['area_m2'])[:REF_LARGE_N], 'largest_any'
    means, stds = [], []
    for b in picked:
        mean, std, n, _ = interior_stats(luma, b['poly'])
        if n:
            means.append(mean)
            stds.append(std)
    if not means:
        return {'kind': 'none', 'n': 0, 'luma': None, 'std': None}
    return {'kind': kind, 'n': len(means), 'luma': round(float(np.median(means)), 1), 'std': round(float(np.median(stds)), 2)}


def score_row(ev):
    terms = {}
    terms['surface'] = ev['surface_share']
    terms['car'] = ev['car_share']
    terms['tree_inside'] = ev['canopy_share']
    terms['tree_shadow_side'] = 1. if ev['shadow'] == 'shadow_side' else 0.
    terms['tree_adjacent_dark'] = 1. if ev['shadow'] == 'adjacent_dark' else 0.
    terms['no_lot'] = 1. if 'no_lot' in (ev['lot_flags'] or ()) else 0.
    terms['low_texture'] = max(0., min(1., 1. - ev['texture_ratio'])) if ev['texture_ratio'] is not None else 0.
    terms['brightness'] = max(0., min(1., abs(math.log2(ev['brightness_ratio'])) - BRIGHTNESS_DEAD_BAND)) if ev['brightness_ratio'] else 0.
    terms['elongated'] = 1. if (ev['aspect'] or 0) >= ELONGATED_ASPECT else 0.
    terms['irregular'] = 1. if ev['rectilinearity'] is not None and ev['rectilinearity'] < RECTILINEAR_MIN else 0.
    terms['tiny'] = 1. if ev['area_m2'] < TINY_M2 else 0.
    terms['low_score'] = max(0., .6 - ev['score']) if ev['score'] is not None else 0.
    terms['roof_part'] = ev['roof_part_share']
    contributions = {k: round(SCORE_TERMS[k][0]*v, 3) for k, v in terms.items() if v}
    return round(sum(contributions.values()), 3), {k: round(v, 3) for k, v in terms.items() if v}, contributions


def evidence_words(ev, contributions):
    words = []
    for kind, s in sorted(ev['surface_by_kind'].items(), key=lambda kv: -kv[1]):
        words.append(f'{kind} {s:.2f}')
    if ev['car_share'] >= .05:
        words.append(f"car {ev['car_share']:.2f}")
    if ev['canopy_share'] >= .05:
        words.append(f"inside tree canopy {ev['canopy_share']:.2f}")
    if ev['shadow'] == 'shadow_side':
        words.append(f"shadow side of tree ({ev['tree_distance_m']} m)")
    elif ev['shadow'] == 'adjacent_dark':
        words.append(f"dark beside tree ({ev['tree_distance_m']} m)")
    if 'no_lot' in (ev['lot_flags'] or ()):
        words.append('no_lot (road reserve / outside parcels)')
    if contributions.get('low_texture'):
        words.append(f"low texture {ev['texture_ratio']:.2f}x roofs")
    if contributions.get('brightness'):
        words.append(f"brightness {ev['brightness_ratio']:.2f}x roofs")
    if contributions.get('elongated'):
        words.append(f"elongated {ev['aspect']}:1")
    if contributions.get('irregular'):
        words.append(f"irregular {ev['rectilinearity']}")
    if contributions.get('tiny'):
        words.append('tiny')
    if contributions.get('roof_part'):
        words.append(f"inside larger building {ev['roof_part_share']:.2f} (counts against)")
    if ev.get('parent_id'):
        words.append('has parent (roof part?)')
    return words


# --- renders --------------------------------------------------------------------------------------------------------

def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def draw_geom(draw, geom, offset, scale, colour, width):
    parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    for part in parts:
        if part.is_empty or part.geom_type != 'Polygon':
            continue
        for ring in [part.exterior, *part.interiors]:
            pts = [((x-offset[0])*scale, (y-offset[1])*scale) for x, y in ring.coords]
            if len(pts) >= 2:
                draw.line(pts, fill=colour, width=width, joint='curve')


def render(image, row, cand_poly, evidence_geoms, other_buildings, mpp, path, scale=3, half_m=RENDER_HALF_M):
    """Fixed metre window centred on the candidate (grown when the candidate needs it), native pixels resized x`scale` NEAREST."""
    bx0, by0, bx1, by1 = cand_poly.bounds
    half = max(half_m, (max(bx1-bx0, by1-by0)*mpp)/2 + 4.)/mpp
    mx, my = (bx0+bx1)/2, (by0+by1)/2
    w, h = image.size
    cx0, cy0 = max(0, int(math.floor(mx-half))), max(0, int(math.floor(my-half)))
    cx1, cy1 = min(w, int(math.ceil(mx+half))), min(h, int(math.ceil(my+half)))
    crop = image.crop((cx0, cy0, cx1, cy1)).convert('RGB')
    crop = crop.resize((crop.width*scale, crop.height*scale), Image.NEAREST)
    draw = ImageDraw.Draw(crop)
    offset = (cx0, cy0)
    for geom in other_buildings:
        draw_geom(draw, geom, offset, scale, (230, 200, 40), 1)
    for geom in evidence_geoms:
        draw_geom(draw, geom, offset, scale, (0, 220, 255), 2)
    draw_geom(draw, cand_poly, offset, scale, (255, 40, 40), 2)
    bar = 5./mpp*scale
    y = crop.height-12
    draw.line([(10, y), (10+bar, y)], fill=(255, 255, 255), width=3)
    draw.text((10, y-16), '5 m', fill=(255, 255, 255), font=_font(12))
    badge = _font(26)
    draw.rectangle([(0, 0), (16+draw.textlength(f"#{row['rank']}", font=badge), 32)], fill=(0, 0, 0))
    draw.text((8, 2), f"#{row['rank']}", fill=(255, 80, 80), font=badge)
    lines = [f"#{row['rank']} {row['scene_id']}/{row['object_id']} · {row['area_m2']} m2 · score {row['score']} · total {row['total']}",
             'evidence: ' + ('; '.join(row['evidence_words']) or 'none'),
             'terms: ' + ', '.join(f'{k} {v:+.2f}' for k, v in row['contributions'].items()),
             f"reference roofs: {row['reference']['kind']} n={row['reference']['n']} luma {row['reference']['luma']} std {row['reference']['std']} · "
             f"candidate luma {row['luma']} std {row['std']} · shadow azimuth idx {row['scene_shadow_index']} · lot-tags {row['lot_tags_state']}",
             f'legend: red = candidate · cyan = overlapping / nearest evidence objects · thin yellow = other buildings · native x{scale} · window {2*half*mpp:.0f} m']
    font = _font(13)
    width = int(max(crop.width, 720, max(ImageDraw.Draw(crop).textlength(t, font=font) for t in lines)+12))
    header = 18*len(lines)+6
    canvas = Image.new('RGB', (width, crop.height+header), (18, 18, 18))
    canvas.paste(crop, (0, header))
    hd = ImageDraw.Draw(canvas)
    for i, text in enumerate(lines):
        hd.text((6, 3+18*i), text, fill=(235, 235, 235) if i < 3 else (160, 160, 160), font=font)
    canvas.save(path)
    return {'path': str(path), 'crop_px': [cx0, cy0, cx1, cy1], 'size': [canvas.width, canvas.height]}


def contact_sheet(paths, out, cell=400, columns=5):
    if not paths:
        return None
    thumbs = []
    for p in paths:
        im = Image.open(p).convert('RGB')
        s = cell/im.width
        thumbs.append(im.resize((cell, max(1, int(im.height*s)))))
    row_h = max(t.height for t in thumbs)
    rows = math.ceil(len(thumbs)/columns)
    sheet = Image.new('RGB', (cell*columns, row_h*rows), (10, 10, 10))
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % columns)*cell, (i//columns)*row_h))
    sheet.save(out)
    return str(out)


# --- the census -----------------------------------------------------------------------------------------------------

def scene_candidates(scene_id, image_row, anns_by_key, conn, log):
    """Evidence rows for one scene's small undecided building candidates; (rows, scene_summary)."""
    sdir = SCENES/scene_id
    latest = read_json(sdir/'latest.json')
    mpp = float(latest['pixel_to_meters'][0][0])
    review = read_json(sdir/'review.json') if (sdir/'review.json').exists() else None
    review_status = {o.get('id'): o.get('status') for o in (review or {}).get('objects') or [] if isinstance(o, dict)}
    objects = [o for o in latest.get('objects') or [] if isinstance(o, dict)]
    by_kind = {}
    for o in objects:
        by_kind.setdefault(o.get('kind'), []).append({'id': o.get('id'), 'poly': polygon(o), 'area_m2': float((o.get('model') or {}).get('area_m2') or 0),
                                                     'score': o.get('score'), 'obj': o})
    buildings = by_kind.get('building', [])
    small = [b for b in buildings if b['area_m2'] < SMALL_M2 and b['obj'].get('status') == 'candidate']
    summary = {'scene_id': scene_id, 'mpp': round(mpp, 6), 'objects': len(objects), 'buildings': len(buildings), 'small_candidates': len(small),
               'review_json': review is not None, 'kinds': {k: len(v) for k, v in by_kind.items()}}
    if not small:
        return [], summary
    png = sdir/f"{latest['image_sha256']}.png"
    if not png.exists():
        summary['error'] = 'scene_png_missing'
        return [], summary
    image = Image.open(png).convert('RGB')
    arr = np.asarray(image, dtype=np.float32)
    luma = .299*arr[..., 0] + .587*arr[..., 1] + .114*arr[..., 2]
    surface = {k: unary_union([x['poly'] for x in by_kind.get(k, []) if x['poly'] is not None]) for k in SURFACE_KINDS if by_kind.get(k)}
    cars = unary_union([x['poly'] for x in by_kind.get('car', []) if x['poly'] is not None]) if by_kind.get('car') else None
    trees = [x['poly'] for k in CANOPY_KINDS for x in by_kind.get(k, []) if x['poly'] is not None]
    canopy = unary_union(trees) if trees else None
    canopy_mask = raster(canopy, 0, 0, image.width, image.height) if canopy is not None else np.zeros(luma.shape, dtype=bool)
    shadow = shadow_azimuth(luma, [x['poly'] for x in by_kind.get('tree', []) if x['poly'] is not None], canopy_mask, mpp)
    summary['shadow'] = shadow
    reference = roof_reference(luma, buildings, review_status)
    summary['reference'] = reference
    tags, tags_state = lot_tags(conn, scene_id, latest.get('model_signature'))
    summary['lot_tags_state'] = tags_state
    large = [b for b in buildings if b['poly'] is not None and b['area_m2'] >= SMALL_M2]
    rows = []
    for b in small:
        o, poly = b['obj'], b['poly']
        ann = anns_by_key.get((scene_id, o.get('id')))
        rstatus = review_status.get(o.get('id'))
        row = {'scene_id': scene_id, 'object_id': o.get('id'), 'area_m2': round(b['area_m2'], 2), 'score': o.get('score'), 'prompt': o.get('prompt'),
               'source': o.get('source'), 'parent_id': o.get('parent_id'), 'group_id': o.get('group_id'), 'review_status': rstatus,
               'undecided': rstatus is None or rstatus in UNDECIDED, 'in_train_json': ann is not None,
               'snapshot_area_m2': ann.get('area_m2') if ann else None, 'ann_id': ann.get('id') if ann else None, 'image_id': image_row['id'],
               'tile': {'scene_id': scene_id, 'tile_group': image_row.get('tile_group'), 'sha256': image_row.get('sha256')},
               'model_signature_match': latest.get('model_signature') == image_row.get('model_signature'), 'mpp': round(mpp, 6),
               'url': URL.format(scene=scene_id, oid=o.get('id'))}
        if poly is None:
            row.update(error='invalid_polygon')
            rows.append(row)
            continue
        ev = {'area_m2': b['area_m2'], 'score': o.get('score'), 'parent_id': o.get('parent_id')}
        ev['surface_by_kind'] = {k: round(share_in(poly, g), 3) for k, g in surface.items() if share_in(poly, g) >= .05}
        ev['surface_share'] = round(share_in(poly, unary_union(list(surface.values()))), 3) if surface else 0.
        ev['car_share'] = round(share_in(poly, cars), 3) if cars is not None else 0.
        ev['canopy_share'] = round(share_in(poly, canopy), 3) if canopy is not None else 0.
        ev['roof_part_share'] = round(share_in(poly, unary_union([x['poly'] for x in large if x['id'] != o.get('id')])), 3) if large else 0.
        mean, std, n, how = interior_stats(luma, poly)
        ev.update(luma=round(mean, 1) if mean is not None else None, std=round(std, 2) if std is not None else None, pixels=n, stats_mask=how)
        ev['brightness_ratio'] = round(mean/reference['luma'], 3) if mean is not None and reference['luma'] else None
        ev['texture_ratio'] = round(std/reference['std'], 3) if std is not None and reference['std'] else None
        ev.update(shape_terms(poly, mpp))
        # nearest tree and the shadow side
        nearest, dist = None, None
        for t in trees:
            d = poly.distance(t)
            if dist is None or d < dist:
                nearest, dist = t, d
        dark = mean is not None and reference['luma'] and mean <= DARK_RATIO*reference['luma']
        ev['tree_distance_m'] = round(dist*mpp, 2) if dist is not None else None
        ev['dark'] = bool(dark)
        ev['shadow'] = None
        if dark and dist is not None and dist*mpp <= SHADOW_NEAR_M and ev['canopy_share'] < .5:
            vec = (poly.centroid.x-nearest.centroid.x, poly.centroid.y-nearest.centroid.y)
            ang = angle_between(vec, shadow['direction']) if shadow['direction'] else None
            ev['tree_vector_angle_to_shadow_deg'] = round(ang, 1) if ang is not None else None
            ev['shadow'] = 'shadow_side' if ang is not None and ang <= SHADOW_ANGLE_DEG else 'adjacent_dark'
        tag = (tags or {}).get(o.get('id'))
        ev['lot_flags'] = tag[0] if tag else None
        ev['lots'] = tag[1] if tag else None
        total, terms, contributions = score_row(ev)
        row.update(ev, total=total, terms=terms, contributions=contributions, evidence_words=evidence_words(ev, contributions),
                   reference=reference, scene_shadow_index=shadow['index'], lot_tags_state=tags_state)
        row['_poly'] = poly
        touching = [x['poly'] for k in (*SURFACE_KINDS, 'car', *CANOPY_KINDS) for x in by_kind.get(k, []) if x['poly'] is not None and share_in(poly, x['poly']) >= .05]
        row['_evidence_geoms'] = touching + ([nearest] if ev['shadow'] and nearest is not None else [])
        row['evidence_object_ids'] = [x['id'] for k in (*SURFACE_KINDS, 'car', *CANOPY_KINDS) for x in by_kind.get(k, []) if x['poly'] is not None and share_in(poly, x['poly']) >= .05]
        row['_others'] = [x['poly'] for x in buildings if x['poly'] is not None and x['id'] != o.get('id')]
        rows.append(row)
    if any(r.get('in_train_json') and r.get('undecided') for r in rows):
        log(f"{scene_id}: small {len(small)} in_train {sum(1 for r in rows if r['in_train_json'])} shadow idx {shadow['index']} ref {reference['kind']} n={reference['n']} lot-tags {tags_state}")
    return rows, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--top', type=int, default=40)
    parser.add_argument('--out', default=str(OUT_DIR))
    parser.add_argument('--no-render', action='store_true')
    parser.add_argument('--scale', type=int, default=3)
    parser.add_argument('--log', default=None, help='append progress lines to this file (stderr otherwise)')
    args = parser.parse_args(argv)
    out = Path(args.out)
    (out/'renders').mkdir(parents=True, exist_ok=True)
    log_handle = open(args.log, 'a') if args.log else sys.stderr

    def log(line):
        print(line, file=log_handle, flush=True)

    train_path = TRAINCLEAN/'train.json'
    train = read_json(train_path)
    images = {i['scene_id']: i for i in train['images']}
    anns_by_key = {(a.get('scene_id') or images.get(a['image_id'], {}).get('scene_id'), a.get('object_id')): a for a in train['annotations']}
    judged = fp.judged_index()
    refused = {}
    for scene_id, image_row in images.items():
        hits = fp.refusals({'scene_id': scene_id, 'tile_group': image_row.get('tile_group'), 'sha256': image_row.get('sha256')}, judged)
        if hits:
            refused[scene_id] = hits
    scene_ids = sorted(s for s in images if s not in refused)
    with_latest = [s for s in scene_ids if (SCENES/s/'latest.json').exists()]
    log(f'train images {len(images)} refused (judged tiles) {len(refused)} scenes with latest.json {len(with_latest)}')
    conn = sqlite3.connect(f'file:{EYES_SQLITE}?mode=ro', uri=True)
    rows, summaries, errors = [], [], []
    for scene_id in with_latest:
        try:
            scene_rows, summary = scene_candidates(scene_id, images[scene_id], anns_by_key, conn, log)
        except Exception as exc:  # noqa: BLE001 - a census records the failure and moves on
            errors.append({'scene_id': scene_id, 'error': repr(exc)})
            log(f'{scene_id}: ERROR {exc!r}')
            continue
        rows.extend(scene_rows)
        summaries.append(summary)
    conn.close()
    ranked = sorted((r for r in rows if r.get('in_train_json') and r.get('undecided') and not r.get('error')), key=lambda r: (-r['total'], r['scene_id'], r['object_id']))
    for i, r in enumerate(ranked, 1):
        r['rank'] = i
    top = ranked[:args.top]
    renders = []
    if not args.no_render:
        for r in top:
            path = out/'renders'/f"{r['rank']:02d}-{r['scene_id']}-{r['object_id']}.png"
            image = Image.open(SCENES/r['scene_id']/f"{read_json(SCENES/r['scene_id']/'latest.json')['image_sha256']}.png")
            renders.append(render(image, r, r['_poly'], r['_evidence_geoms'], r['_others'], r['mpp'], path, scale=args.scale))
            r['render'] = renders[-1]['path']
        sheet = contact_sheet([x['path'] for x in renders], out/'renders'/'contact-sheet.png')
    else:
        sheet = None
    for r in rows:
        for k in ('_poly', '_evidence_geoms', '_others'):
            r.pop(k, None)

    def kinds_of(r):
        c = r.get('contributions') or {}
        kinds = [k for k in c if c[k] > 0]
        return kinds or ['none']

    not_ranked = Counter()
    for r in rows:
        if r.get('error'):
            not_ranked['invalid_polygon'] += 1
        elif not r['undecided']:
            not_ranked[f"decided:{r['review_status']}"] += 1
        elif not r['in_train_json']:
            not_ranked['under_export_floor' if r['area_m2'] < EXPORT_FLOOR_M2 else 'not_in_train_json'] += 1
    population = {'train_images': len(images), 'refused_judged_tiles': len(refused), 'refused_detail': refused, 'scenes_with_latest': len(with_latest),
                  'scenes_with_small_candidates': sum(1 for s in summaries if s['small_candidates']), 'scenes_with_review_json': sum(1 for s in summaries if s['review_json']),
                  'small_candidates': len(rows), 'small_undecided': sum(1 for r in rows if r.get('undecided')),
                  'ranked (undecided, in train.json)': len(ranked), 'ranked_ge_12m2': sum(1 for r in ranked if r['area_m2'] >= 12),
                  'not_ranked_by_reason': dict(not_ranked), 'scenes_in_ranked': len({r['scene_id'] for r in ranked}),
                  'by_evidence_kind (ranked, any positive contribution)': dict(Counter(k for r in ranked for k in kinds_of(r))),
                  'by_evidence_kind (top)': dict(Counter(k for r in top for k in kinds_of(r))),
                  'top_scenes': dict(Counter(r['scene_id'] for r in top)), 'top_area_m2': {'min': min((r['area_m2'] for r in top), default=None), 'max': max((r['area_m2'] for r in top), default=None),
                                                                                             'ge_12': sum(1 for r in top if r['area_m2'] >= 12)},
                  'top_total': {'min': min((r['total'] for r in top), default=None), 'max': max((r['total'] for r in top), default=None)},
                  'shadow_azimuth_known_scenes': sum(1 for s in summaries if (s.get('shadow') or {}).get('direction')),
                  'lot_tags_state': dict(Counter(s.get('lot_tags_state', 'n/a') for s in summaries if s['small_candidates'])),
                  'reference_kind': dict(Counter((s.get('reference') or {}).get('kind', 'n/a') for s in summaries if s['small_candidates'])),
                  'errors': errors}
    census = {'version': VERSION, 'computed_at': now_iso(), 'read_only': True, 'wrote_only_under': str(out),
              'inputs': {'train_json': str(train_path), 'train_json_sha256': sha256_file(train_path), 'judged_splits': {k: {'sha256_file': v['sha256_file'], 'path': v['path']} for k, v in judged.items()},
                         'eyes_sqlite': str(EYES_SQLITE), 'code_sha256': sha256_file(Path(__file__))},
              'design': {'SMALL_M2': SMALL_M2, 'EXPORT_FLOOR_M2': EXPORT_FLOOR_M2, 'DARK_RATIO': DARK_RATIO, 'SHADOW_NEAR_M': SHADOW_NEAR_M, 'SHADOW_PROBE_M': SHADOW_PROBE_M,
                         'SHADOW_ANGLE_DEG': SHADOW_ANGLE_DEG, 'SHADOW_CONTRAST_MIN': SHADOW_CONTRAST_MIN, 'ELONGATED_ASPECT': ELONGATED_ASPECT, 'RECTILINEAR_MIN': RECTILINEAR_MIN,
                         'TINY_M2': TINY_M2, 'REF_CONFIRMED_MIN': REF_CONFIRMED_MIN, 'REF_LARGE_M2': REF_LARGE_M2, 'REF_LARGE_N': REF_LARGE_N, 'ERODE_PX': ERODE_PX, 'BRIGHTNESS_DEAD_BAND': BRIGHTNESS_DEAD_BAND, 'RENDER_HALF_M': RENDER_HALF_M,
                         'directions_image_axes': DIRECTIONS},
              'score_terms': {k: {'weight': w, 'input': d} for k, (w, d) in SCORE_TERMS.items()}, 'ranking': 'total desc, scene_id, object_id; top = first --top rows',
              'population': population, 'top': [{k: v for k, v in r.items()} for r in top], 'ranked': ranked, 'not_ranked': [r for r in rows if r not in ranked],
              'scenes': summaries, 'renders': renders, 'contact_sheet': sheet, 'instruction': INSTRUCTION}
    with open(out/'census.json', 'w') as handle:
        json.dump(census, handle, indent=1, sort_keys=True, default=str)
    write_worklist(out/'WORKLIST.md', census, top)
    log(f"ranked {len(ranked)} top {len(top)} renders {len(renders)} -> {out}")
    print(json.dumps({'population': {k: v for k, v in population.items() if k not in ('refused_detail', 'errors')}, 'top': len(top), 'out': str(out)}, indent=1, sort_keys=True))
    return 0


def write_worklist(path, census, top):
    pop = census['population']
    lines = [f"# Negatives worklist: likely false-positive small buildings on TRAIN tiles ({census['computed_at']})", '',
             f"Read-only ({census['version']}). Population: {pop['train_images']} dataset-v2-trainclean train images, {pop['refused_judged_tiles']} refused as frozen judged "
             f"tiles, {pop['scenes_with_latest']} scenes with latest.json, {pop['small_candidates']} building candidates under {SMALL_M2:g} m2, "
             f"{pop['small_undecided']} undecided, {pop['ranked (undecided, in train.json)']} with a train.json annotation (rankable: a rejection removes that "
             f"annotation from the fold's train split); not ranked: {pop['not_ranked_by_reason']}.", '',
             'A Backspace here is a hard negative for the round-5 fold (r5_fold_prep negatives --trainclean scan; the object becomes background). '
             'A Right arrow is a confirmed small roof. Nothing in this list is a decision; the evidence is from the files only.', '',
             'Score terms (weight x input, plain sum): ' + '; '.join(f"{k} {w:+g} x {d}" for k, (w, d) in SCORE_TERMS.items()), '',
             f"Top {len(top)} of {pop['ranked (undecided, in train.json)']} ranked; scenes in top: {len(pop['top_scenes'])}; by evidence kind (top): {pop['by_evidence_kind (top)']}.", '',
             f"Renders: {Path(census['wrote_only_under'])/'renders'} (native x3; red candidate, cyan evidence, thin yellow other buildings, 5 m bar); contact sheet renders/contact-sheet.png.", '']
    for r in top:
        lines.append(f"{r['rank']}. [{r['scene_id']}/{r['object_id']}]({r['url']}) · {r['area_m2']} m2 · score {r['score']} · total {r['total']} · "
                     f"evidence: {'; '.join(r['evidence_words']) or 'none (shape/score only)'} · {INSTRUCTION}")
    lines += ['', f"Next after the clicks: scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_fold_prep negatives --trainclean {TRAINCLEAN} ... (the fold card's item 2).", '']
    with open(path, 'w') as handle:
        handle.write('\n'.join(lines))


if __name__ == '__main__':
    sys.exit(main())
