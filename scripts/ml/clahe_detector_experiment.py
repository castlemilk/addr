"""CLAHE detector experiment: does contrast enhancement change WHAT SAM3 finds, or only the outlines?

One declared variable: the whole-frame SAM3 encode sees a CLAHE variant of the saved PNG
(cv2 CLAHE on the LAB L channel, clipLimit 3.0, tileGridSize 12x12, back to RGB; the editor's
"Enhanced contrast" recipe). Everything else (surface refinement, colours, roof-line
evidence, lot crops) keeps the saved PNG, because scene_isolated.VariantSegmenter only swaps
segmenter.encode_image for the original frame. Runs land under
data/reviews/eyes/experiments/clahe-detector-v1/ (never data/scenes).

Comparison: objects are matched per kind by greedy IoU >= 0.5 on the pixel rings; the
report gives matched / only-baseline / only-candidate counts with denominators, IoU
distribution of matches, area, detector score and vertex deltas, building-flags-v2 hints
for lost objects (read-only from data/reviews/eyes/eyes.sqlite) and T1/T2 reference
agreement (IoU with the reference geometry, baseline and candidate). All of these are model
estimates and reference agreement, not accuracy; nothing here is a label.

  python -m scripts.ml.clahe_detector_experiment variant SCENE --out /tmp/addr-eyes/clahe/SCENE.npy [--preview PNG]
  python -m scripts.ml.clahe_detector_experiment compare --experiment-root DIR --scenes ID... [--control ID]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

VERSION = 'clahe-detector-v1'
REPO = Path(__file__).resolve().parents[2]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
CLAHE_PARAMS = {'space': 'LAB', 'channel': 'L', 'clip_limit': 3.0, 'tile_grid': [12, 12], 'library': 'cv2'}
KINDS = ('building', 'tree', 'solar_panel')
MATCH_IOU = .5
LOW_IOU = .8
EYES_DB = Path('data/reviews/eyes/eyes.sqlite')
FLAG_VERSION = 'building-flags-v2'
REFERENCE_TIERS = ('T1', 'T2')
FOCUS_OBJECT = '7fdd138f819cb06c'
NOTE = 'Model estimates and reference agreement, not accuracy. Nothing here is a label.'


# --------------------------------------------------------------------------- variant

def clahe_rgb(rgb, *, clip_limit=CLAHE_PARAMS['clip_limit'], tile_grid=tuple(CLAHE_PARAMS['tile_grid'])):
    """HxWx3 uint8 RGB -> CLAHE on the LAB L channel -> HxWx3 uint8 RGB (the editor recipe)."""
    import cv2
    data = np.ascontiguousarray(np.asarray(rgb))
    if data.ndim != 3 or data.shape[2] != 3 or data.dtype != np.uint8:
        raise ValueError('clahe_rgb expects an HxWx3 uint8 RGB array')
    lab = cv2.cvtColor(data, cv2.COLOR_RGB2LAB)
    clahe = cv2.createCLAHE(clipLimit=float(clip_limit), tileGridSize=tuple(int(t) for t in tile_grid))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return np.ascontiguousarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))


def scene_png(scene_id, repo=REPO):
    folder = Path(repo)/'data/scenes'/scene_id
    scene = json.loads((folder/'latest.json').read_text())
    path = folder/f"{scene['image_sha256']}.png"
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != scene['image_sha256']:
        raise ValueError('Source imagery changed')
    return scene, Image.open(path).convert('RGB')


def build_variant(scene_id, out_npy, *, preview=None, repo=REPO):
    """Save the CLAHE variant as HxWx3 uint8 .npy (scene_isolated --image-variant) and record its hash."""
    scene, image = scene_png(scene_id, repo)
    rgb = np.asarray(image, dtype=np.uint8)
    variant = clahe_rgb(rgb)
    out_npy = Path(out_npy)
    out_npy.parent.mkdir(parents=True, exist_ok=True)
    np.save(out_npy, variant, allow_pickle=False)
    gray_before, gray_after = rgb.mean(axis=2), variant.mean(axis=2)
    record = {'version': VERSION, 'scene_id': scene_id, 'image_sha256': scene['image_sha256'], 'params': CLAHE_PARAMS,
              'variant_sha256': hashlib.sha256(variant.tobytes()).hexdigest(), 'shape': list(variant.shape), 'npy': str(out_npy),
              'luminance_std_before': round(float(gray_before.std()), 2), 'luminance_std_after': round(float(gray_after.std()), 2),
              'mean_abs_pixel_change': round(float(np.abs(variant.astype(int)-rgb.astype(int)).mean()), 2),
              'code_sha256': CODE_SHA256, 'computed_at': datetime.now(UTC).isoformat()}
    if preview:
        Path(preview).parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(variant).save(preview)
        record['preview'] = str(preview)
    return record



# --------------------------------------------------------------------------- geometry

def polygon_of(obj):
    """Shapely polygon of an object's pixel ring (holes kept); None when degenerate."""
    from shapely.errors import GEOSException
    from shapely.geometry import Polygon
    try:
        polygon = Polygon(obj['ring'], obj.get('holes') or None)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
    except (ValueError, TypeError, KeyError, GEOSException):
        return None
    return polygon if not polygon.is_empty and polygon.area > 0 else None


def iou(a, b):
    if a is None or b is None:
        return 0.
    from shapely.errors import GEOSException
    try:
        inter = a.intersection(b).area
    except GEOSException:
        return 0.
    union = a.area + b.area - inter
    return float(inter/union) if union > 0 else 0.


def pixel_scale(scene):
    """Metres per pixel from the scene affine (sign-free)."""
    return abs(float(scene['pixel_to_meters'][0][0]))


def area_m2(polygon, scene):
    return round(float(polygon.area)*pixel_scale(scene)**2, 2) if polygon is not None else None


def match_objects(baseline, candidate, *, threshold=MATCH_IOU, kinds=KINDS):
    """Greedy IoU matching per kind: pairs sorted by IoU, each object used once, IoU >= threshold.

    Returns {'matched': [(b, c, iou)], 'only_baseline': [b], 'only_candidate': [c]} where b/c
    are the object dicts. Objects whose kind is not in `kinds`, or whose ring is degenerate, are
    reported under 'skipped'.
    """
    result = {'matched': [], 'only_baseline': [], 'only_candidate': [], 'skipped': []}
    for kind in kinds:
        bs = [o for o in baseline if o.get('kind') == kind]
        cs = [o for o in candidate if o.get('kind') == kind]
        pb = {o['id']: polygon_of(o) for o in bs}
        pc = {o['id']: polygon_of(o) for o in cs}
        pairs = []
        for b in bs:
            if pb[b['id']] is None:
                continue
            for c in cs:
                if pc[c['id']] is None:
                    continue
                if pb[b['id']].bounds[2] < pc[c['id']].bounds[0] or pc[c['id']].bounds[2] < pb[b['id']].bounds[0] \
                        or pb[b['id']].bounds[3] < pc[c['id']].bounds[1] or pc[c['id']].bounds[3] < pb[b['id']].bounds[1]:
                    continue
                value = iou(pb[b['id']], pc[c['id']])
                if value >= threshold:
                    pairs.append((value, b['id'], c['id']))
        pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
        used_b, used_c = set(), set()
        by_b, by_c = {o['id']: o for o in bs}, {o['id']: o for o in cs}
        for value, bid, cid in pairs:
            if bid in used_b or cid in used_c:
                continue
            used_b.add(bid)
            used_c.add(cid)
            result['matched'].append((by_b[bid], by_c[cid], round(value, 4)))
        for b in bs:
            if pb[b['id']] is None:
                result['skipped'].append(('baseline', b['id']))
            elif b['id'] not in used_b:
                result['only_baseline'].append(b)
        for c in cs:
            if pc[c['id']] is None:
                result['skipped'].append(('candidate', c['id']))
            elif c['id'] not in used_c:
                result['only_candidate'].append(c)
    return result


def _quantile(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pos = (len(values)-1)*q
    lo, hi = int(pos), min(int(pos)+1, len(values)-1)
    return round(values[lo] + (values[hi]-values[lo])*(pos-lo), 4)


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 4) if values else None


def _vertices(obj):
    return len(obj.get('ring') or [])


def _raw_vertices(obj):
    raw = (obj.get('boundary_refinement') or {}).get('raw_ring')
    return len(raw) if raw else None  # None (not 0) when the object carries no raw ring


def summarise_matches(match, scene, *, kinds=KINDS, candidate_scene=None):
    """Per-kind counts with denominators and paired deltas (candidate minus baseline) for matched objects.

    `owned` for candidate objects is read from candidate_scene (its ids differ from the baseline's).
    """
    cand_scene = candidate_scene or scene
    out = {}
    for kind in kinds:
        pairs = [(b, c, v) for b, c, v in match['matched'] if b['kind'] == kind]
        only_b = [o for o in match['only_baseline'] if o['kind'] == kind]
        only_c = [o for o in match['only_candidate'] if o['kind'] == kind]
        n_b, n_c = len(pairs)+len(only_b), len(pairs)+len(only_c)
        ious = [v for _, _, v in pairs]
        areas_b = [area_m2(polygon_of(b), scene) for b, _, _ in pairs]
        areas_c = [area_m2(polygon_of(c), scene) for _, c, _ in pairs]
        area_delta = [c-b for b, c in zip(areas_b, areas_c, strict=True) if b is not None and c is not None]
        area_rel = [(c-b)/b for b, c in zip(areas_b, areas_c, strict=True) if b and c is not None]
        score_delta = [float(c.get('score') or 0)-float(b.get('score') or 0) for b, c, _ in pairs]
        vert_delta = [_vertices(c)-_vertices(b) for b, c, _ in pairs]
        out[kind] = {
            'baseline_total': n_b, 'candidate_total': n_c, 'matched': len(pairs),
            'only_baseline': len(only_b), 'only_candidate': len(only_c),
            'matched_share_of_baseline': round(len(pairs)/n_b, 4) if n_b else None,
            'matched_share_of_candidate': round(len(pairs)/n_c, 4) if n_c else None,
            'iou': {'median': _median(ious), 'p10': _quantile(ious, .1), 'min': min(ious) if ious else None,
                    'n_below_0_8': sum(v < LOW_IOU for v in ious), 'n_identical_ring': sum(b['ring'] == c['ring'] for b, c, _ in pairs)},
            'area_m2_delta': {'median': _median(area_delta), 'p10': _quantile(area_delta, .1), 'p90': _quantile(area_delta, .9),
                              'median_relative': _median(area_rel), 'n': len(area_delta)},
            'score_delta': {'median': _median(score_delta), 'p10': _quantile(score_delta, .1), 'p90': _quantile(score_delta, .9),
                            'n_up': sum(d > 1e-9 for d in score_delta), 'n_down': sum(d < -1e-9 for d in score_delta), 'n': len(score_delta)},
            'vertices': {'baseline_median': _median([_vertices(b) for b, _, _ in pairs]), 'candidate_median': _median([_vertices(c) for _, c, _ in pairs]),
                         'delta_median': _median(vert_delta), 'raw_baseline_median': _median([_raw_vertices(b) for b, _, _ in pairs]),
                         'raw_candidate_median': _median([_raw_vertices(c) for _, c, _ in pairs]), 'n': len(pairs)},
            'only_baseline_objects': [_brief(o, scene) for o in only_b],
            'only_candidate_objects': [_brief(o, cand_scene) for o in only_c],
            'only_baseline_score_median': _median([float(o.get('score') or 0) for o in only_b]),
            'only_candidate_score_median': _median([float(o.get('score') or 0) for o in only_c]),
            'only_baseline_area_m2_median': _median([area_m2(polygon_of(o), scene) for o in only_b]),
            'only_candidate_area_m2_median': _median([area_m2(polygon_of(o), scene) for o in only_c]),
        }
    return out


def classify_unmatched(match, *, cover=.9, kinds=KINDS):
    """Why an unmatched object has no partner: absorbed (>= cover of its area lies under same-kind objects of the other side,
    i.e. a merge/split with a different topology), partial (some overlap below the match rule) or absent (no overlap at all)."""
    def side(objects, others):
        rows = []
        polys = [(o, polygon_of(o)) for o in others]
        for o in objects:
            p = polygon_of(o)
            if p is None:
                continue
            hits = [(q, iou(p, pq), pq.intersection(p).area/p.area) for q, pq in polys if q['kind'] == o['kind'] and pq is not None and pq.intersects(p)]
            covered = sum(share for _, _, share in hits)
            rows.append({'object_id': o['id'], 'kind': o['kind'], 'covered_share': round(min(covered, 1.), 3),
                         'reason': 'absorbed' if covered >= cover else 'partial' if hits else 'absent',
                         'partners': [{'object_id': q['id'], 'iou': round(v, 3), 'share': round(sh, 3)} for q, v, sh in sorted(hits, key=lambda t: -t[2])[:3]]})
        return rows
    from collections import Counter
    base_rows = side(match['only_baseline'], [o for o in match['only_candidate']] + [c for _, c, _ in match['matched']])
    cand_rows = side(match['only_candidate'], [o for o in match['only_baseline']] + [b for b, _, _ in match['matched']])
    def counts(rows):
        return {kind: dict(Counter(r['reason'] for r in rows if r['kind'] == kind)) for kind in kinds}
    return {'only_baseline': base_rows, 'only_candidate': cand_rows, 'counts': {'only_baseline': counts(base_rows), 'only_candidate': counts(cand_rows)},
            'note': f'absorbed = at least {cover:.0%} of the object area lies under same-kind objects of the other side (merge/split); '
                    'absent = no same-kind overlap at all (a genuinely different detection).'}


def _brief(obj, scene):
    polygon = polygon_of(obj)
    centroid = polygon.centroid if polygon is not None else None
    return {'object_id': obj['id'], 'kind': obj['kind'], 'score': obj.get('score'), 'area_m2': area_m2(polygon, scene),
            'vertices': _vertices(obj), 'centroid_px': [round(centroid.x, 1), round(centroid.y, 1)] if centroid is not None else None,
            'owned': obj['id'] in set(scene.get('owned_object_ids') or [])}


# --------------------------------------------------------------------------- eyes index (read-only)

def _connect_readonly(path):
    return sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True, timeout=30)


def building_flags(eyes_db, scene_id, object_ids, *, version=FLAG_VERSION):
    """{object_id: {'flags': [...], 'class_hint': str|None}} from object_quality rows of one version; missing ids absent."""
    wanted = set(object_ids)
    out = {}
    if not wanted or not Path(eyes_db).exists():
        return out
    with closing(_connect_readonly(eyes_db)) as db:
        rows = db.execute('select object_id, payload from object_quality where scene_id=? and version=?', (scene_id, version)).fetchall()
    for oid, payload in rows:
        if oid not in wanted:
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        hints = data.get('class_hints') or {}
        out[oid] = {'flags': sorted({f.get('flag') for f in hints.get('flags') or [] if isinstance(f, dict) and f.get('flag')}),
                    'class_hint': hints.get('class_hint'), 'score': data.get('score'), 'area_m2': data.get('area_m2')}
    return out


def flag_summary(flags, object_ids):
    """Counts of secondary-structure flags / outbuilding hints among object_ids, with the denominator."""
    ids = list(object_ids)
    rows = [flags.get(i) for i in ids]
    known = [r for r in rows if r]
    return {'n': len(ids), 'with_flag_rows': len(known),
            'secondary_structure': sum('secondary-structure' in r['flags'] for r in known),
            'outbuilding_hint': sum(r.get('class_hint') == 'outbuilding' for r in known),
            'non_residential_hint': sum(r.get('class_hint') in ('non-residential', 'commercial') or 'non-residential-context' in r['flags'] for r in known),
            'class_hints': dict(sorted(__import__('collections').Counter(r.get('class_hint') or 'none' for r in known).items()))}


def wgs84_to_pixels(points, scene):
    """WGS84 (lon, lat) -> scene pixel coordinates, the inverse of scene_confidence.geographic."""
    from .paired_compare import to_local
    local = to_local(points, scene)
    return (np.c_[local, np.ones(len(local))] @ np.asarray(scene['meters_to_pixel'])).tolist()


def references_in_tile(eyes_db, scene, *, tiers=REFERENCE_TIERS):
    """T1/T2 reference rows whose bounds intersect the scene's region bounds, with pixel polygons and scene provenance."""
    from shapely.geometry import Polygon, shape
    bounds = (scene.get('region') or {}).get('bounds')
    if not bounds or not Path(eyes_db).exists():
        return []
    west, south, east, north = bounds
    marks = ','.join('?'*len(tiers))
    with closing(_connect_readonly(eyes_db)) as db:
        rows = db.execute(f'select ref_id, tier, source_kind, scene_id, object_id, split, payload from "references" '
                          f'where tier in ({marks}) and east>=? and west<=? and north>=? and south<=?',
                          (*tiers, west, east, south, north)).fetchall()
    out = []
    width, height = scene.get('image_width') or 0, scene.get('image_height') or 0
    frame = Polygon([(0, 0), (width, 0), (width, height), (0, height)]) if width and height else None
    for ref_id, tier, source_kind, ref_scene, object_id, split, payload in rows:
        try:
            data = json.loads(payload)
            geom = shape(data['geometry_wgs84'])
        except (ValueError, KeyError, TypeError):
            continue
        rings = [geom] if geom.geom_type == 'Polygon' else list(getattr(geom, 'geoms', []))
        pixel = None
        for ring in rings:
            poly = Polygon(wgs84_to_pixels(list(ring.exterior.coords), scene))
            if not poly.is_valid:
                poly = poly.buffer(0)
            pixel = poly if pixel is None else pixel.union(poly)
        if pixel is None or pixel.is_empty:
            continue
        inside = pixel.intersection(frame).area/pixel.area if frame is not None and pixel.area else None
        out.append({'ref_id': ref_id, 'tier': tier, 'source_kind': source_kind, 'scene_id': ref_scene, 'object_id': object_id, 'split': split,
                    'same_image': ref_scene == scene.get('id'), 'drawn_over_scene_ids': data.get('drawn_over_scene_ids'),
                    'polygon': pixel, 'share_inside_frame': round(inside, 3) if inside is not None else None})
    return out


def _best_iou(polygon, side):
    scored = sorted(((iou(polygon, p), o['id']) for o, p in side if p is not None), reverse=True)
    return {'iou': round(scored[0][0], 4), 'object_id': scored[0][1]} if scored and scored[0][0] > 0 else {'iou': 0., 'object_id': None}


def reference_agreement(references, baseline, candidate):
    """Best building IoU per reference on each side (model estimate vs human reference; agreement, not accuracy)."""
    pb = [(o, polygon_of(o)) for o in baseline if o.get('kind') == 'building']
    pc = [(o, polygon_of(o)) for o in candidate if o.get('kind') == 'building']
    rows = []
    for ref in references:
        b, c = _best_iou(ref['polygon'], pb), _best_iou(ref['polygon'], pc)
        rows.append({**{k: v for k, v in ref.items() if k != 'polygon'}, 'baseline': b, 'candidate': c,
                     'iou_delta': round(c['iou']-b['iou'], 4), 'centroid_px': [round(ref['polygon'].centroid.x, 1), round(ref['polygon'].centroid.y, 1)]})
    return rows


# --------------------------------------------------------------------------- runs

def load_run(run_root, scene_id):
    run_root = Path(run_root)
    base = json.loads((run_root/'baseline/data/scenes'/scene_id/'latest.json').read_text())
    cand = json.loads((run_root/'data/scenes'/scene_id/'latest.json').read_text())
    record_path = run_root/'isolated-run.json'
    record = json.loads(record_path.read_text()) if record_path.exists() else None
    return base, cand, record


def object_identity(baseline, candidate):
    """How many objects reproduce by id and by exact ring (used for the no-variant control)."""
    b = {o['id']: o for o in baseline}
    c = {o['id']: o for o in candidate}
    shared = sorted(b.keys() & c.keys())
    return {'baseline': len(b), 'candidate': len(c), 'shared_ids': len(shared),
            'identical_rings': sum(b[i].get('ring') == c[i].get('ring') for i in shared),
            'identical_scores': sum(b[i].get('score') == c[i].get('score') for i in shared)}


def focus_object(match, scene, object_id=FOCUS_OBJECT, candidate_scene=None):
    base = next((o for o in match['only_baseline'] if o['id'] == object_id), None)
    if base is not None:
        return {'object_id': object_id, 'outcome': 'only_baseline', 'baseline': _brief(base, scene)}
    for b, c, value in match['matched']:
        if b['id'] == object_id:
            return {'object_id': object_id, 'outcome': 'matched', 'iou': value, 'baseline': _brief(b, scene), 'candidate': _brief(c, candidate_scene or scene),
                    'ring_identical': b['ring'] == c['ring']}
    return None


def contact_sheet(scene, image, match, out_path, *, references=None, scale=2, title=''):
    """Differences on the original image at `scale`: only-baseline orange, only-candidate green, matched IoU<0.8 yellow."""
    from PIL import ImageDraw, ImageFont
    big = image.resize((image.width*scale, image.height*scale), Image.LANCZOS)
    header = 72
    sheet = Image.new('RGB', (big.width, big.height+header), (20, 20, 20))
    sheet.paste(big, (0, header))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 22)
        small = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 18)
    except OSError:
        font = small = ImageFont.load_default()
    colours = {'only_baseline': (255, 140, 0), 'only_candidate': (40, 220, 80), 'low_iou': (255, 230, 0), 'reference': (80, 160, 255)}
    def ring(points, colour, width=3):
        pts = [(x*scale, y*scale+header) for x, y in points]
        draw.line(pts+[pts[0]], fill=colour, width=width)
    def label(points, text, colour):
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        x, y = min(xs)*scale, min(ys)*scale+header-24
        w = draw.textlength(text, font=small)+8
        draw.rectangle([x, y, x+w, y+22], fill=(0, 0, 0))
        draw.text((x+4, y+1), text, fill=colour, font=small)
    counts = {'only_baseline': 0, 'only_candidate': 0, 'low_iou': 0}
    for o in match['only_baseline']:
        ring(o['ring'], colours['only_baseline'])
        label(o['ring'], f"-{o['kind'][:4]} {o['id'][:6]} s{float(o.get('score') or 0):.2f}", colours['only_baseline'])
        counts['only_baseline'] += 1
    for o in match['only_candidate']:
        ring(o['ring'], colours['only_candidate'])
        label(o['ring'], f"+{o['kind'][:4]} {o['id'][:6]} s{float(o.get('score') or 0):.2f}", colours['only_candidate'])
        counts['only_candidate'] += 1
    for b, c, value in match['matched']:
        if value < LOW_IOU:
            ring(b['ring'], colours['low_iou'], 2)
            ring(c['ring'], colours['low_iou'], 4)
            label(b['ring'], f"~{b['kind'][:4]} {b['id'][:6]} iou{value:.2f}", colours['low_iou'])
            counts['low_iou'] += 1
    for ref in references or []:
        ring(list(ref['polygon'].exterior.coords), colours['reference'], 2)
        label(list(ref['polygon'].exterior.coords), f"ref {ref['tier']} {ref['ref_id'][:8]}", colours['reference'])
    text = (f"{title}  orange = only baseline ({counts['only_baseline']}), green = only CLAHE candidate ({counts['only_candidate']}), "
            f"yellow = matched IoU < {LOW_IOU} ({counts['low_iou']}; thin = baseline, thick = candidate), blue = T1/T2 reference. "
            f"Model estimates, not labels.")
    draw.text((12, 10), text[:220], fill=(255, 255, 255), font=font)
    if len(text) > 220:
        draw.text((12, 40), text[220:], fill=(255, 255, 255), font=font)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return counts


def roi_sheet(image, baseline, candidate, match, out_path, *, window=140, scale=3, columns=5, kinds=('building',)):
    """Crop grid of every difference (lost, new, matched IoU < 0.8) with all baseline (orange) and candidate (green) rings of that kind."""
    from PIL import ImageDraw
    from shapely.geometry import box as make_box
    rois = [(f"lost {o['id'][:8]} s{float(o.get('score') or 0):.2f}", o) for o in match['only_baseline'] if o['kind'] in kinds]
    rois += [(f"new {o['id'][:8]} s{float(o.get('score') or 0):.2f}", o) for o in match['only_candidate'] if o['kind'] in kinds]
    rois += [(f"low {b['id'][:8]} iou{v:.2f}", b) for b, c, v in match['matched'] if v < LOW_IOU and b['kind'] in kinds]
    shapes = {'baseline': [(o, polygon_of(o)) for o in baseline if o['kind'] in kinds], 'candidate': [(o, polygon_of(o)) for o in candidate if o['kind'] in kinds]}
    tiles = []
    for name, o in rois:
        polygon = polygon_of(o)
        if polygon is None:
            continue
        cx, cy = polygon.centroid.x, polygon.centroid.y
        left, top = int(cx-window/2), int(cy-window/2)
        frame = make_box(left, top, left+window, top+window)
        crop = image.crop((left, top, left+window, top+window)).resize((window*scale, window*scale), Image.LANCZOS)
        draw = ImageDraw.Draw(crop)
        for side, colour in (('baseline', (255, 140, 0)), ('candidate', (40, 220, 80))):
            for other, shape_ in shapes[side]:
                if shape_ is not None and shape_.intersects(frame):
                    pts = [((x-left)*scale, (y-top)*scale) for x, y in other['ring']]
                    draw.line(pts+[pts[0]], fill=colour, width=2)
        draw.rectangle([0, 0, window*scale, 20], fill=(0, 0, 0))
        draw.text((4, 4), name, fill=(255, 255, 255))
        tiles.append(crop)
    if not tiles:
        return 0
    columns = min(columns, len(tiles))
    rows = (len(tiles)+columns-1)//columns
    sheet = Image.new('RGB', (columns*window*scale+(columns-1)*6, rows*window*scale+(rows-1)*6), 'white')
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % columns)*(window*scale+6), (i//columns)*(window*scale+6)))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return len(tiles)


def compare_scene(run_root, scene_id, *, eyes_db=EYES_DB, sheet_path=None, repo=REPO):
    base, cand, record = load_run(run_root, scene_id)
    match = match_objects(base['objects'], cand['objects'])
    summary = summarise_matches(match, base, candidate_scene=cand)
    building_ids = [o['id'] for o in base['objects'] if o['kind'] == 'building']
    flags = building_flags(eyes_db, scene_id, building_ids)
    lost = [o['id'] for o in match['only_baseline'] if o['kind'] == 'building']
    kept = [b['id'] for b, _, _ in match['matched'] if b['kind'] == 'building']
    refs = references_in_tile(eyes_db, base)
    agreement = reference_agreement(refs, base['objects'], cand['objects'])
    owned = set(base.get('owned_object_ids') or [])
    result = {
        'scene_id': scene_id, 'run_root': str(run_root), 'image_sha256': base['image_sha256'],
        'baseline_model_signature': base.get('model_signature'), 'candidate_model_signature': cand.get('model_signature'),
        'isolated_run': {k: record.get(k) for k in ('signature_reproduced', 'elapsed', 'objects', 'assess_error', 'warnings')} if record else None,
        'variables': (record or {}).get('variables'),
        'counts': {'baseline_objects': len(base['objects']), 'candidate_objects': len(cand['objects']),
                   'baseline_owned': len(owned), 'skipped_degenerate': len(match['skipped'])},
        'per_kind': summary,
        'lost_building_flags': {**flag_summary(flags, lost), 'objects': [{'object_id': i, **flags.get(i, {'flags': None, 'class_hint': None})} for i in lost]},
        'kept_building_flags': flag_summary(flags, kept),
        'all_baseline_building_flags': flag_summary(flags, building_ids),
        'unmatched_reasons': classify_unmatched(match),
        'focus_object': focus_object(match, base, candidate_scene=cand),
        'references': {'n_in_tile': len(refs), 'same_image': sum(r['same_image'] for r in refs), 'rows': agreement,
                       'baseline_iou_median': _median([r['baseline']['iou'] for r in agreement]),
                       'candidate_iou_median': _median([r['candidate']['iou'] for r in agreement]),
                       'n_candidate_better': sum(r['iou_delta'] > .01 for r in agreement), 'n_baseline_better': sum(r['iou_delta'] < -.01 for r in agreement),
                       'note': 'IoU of the best building outline with the human reference geometry (agreement, not accuracy); '
                               'references drawn over another image carry a cross-imagery caveat.'},
        'low_iou_pairs': [{'baseline_id': b['id'], 'candidate_id': c['id'], 'kind': b['kind'], 'iou': v, 'baseline': _brief(b, base), 'candidate': _brief(c, cand)}
                          for b, c, v in match['matched'] if v < LOW_IOU],
    }
    if sheet_path:
        _, image = scene_png(scene_id, repo)
        result['contact_sheet'] = str(sheet_path)
        result['contact_sheet_counts'] = contact_sheet(base, image, match, sheet_path, references=refs, scale=2, title=scene_id)
        roi_path = Path(sheet_path).with_name(Path(sheet_path).name.replace('-diff.png', '-building-crops.png'))
        result['building_crops_sheet'] = {'path': str(roi_path), 'tiles': roi_sheet(image, base['objects'], cand['objects'], match, roi_path)}
    return result


def control_check(run_root, scene_id):
    base, cand, record = load_run(run_root, scene_id)
    return {'scene_id': scene_id, 'run_root': str(run_root),
            'signature_reproduced': (record or {}).get('signature_reproduced'),
            'baseline_diff': (record or {}).get('baseline_diff'), 'variables': (record or {}).get('variables'),
            'object_identity': object_identity(base['objects'], cand['objects']), 'elapsed_s': (record or {}).get('elapsed')}


def totals(scenes):
    out = {}
    for kind in KINDS:
        rows = [s['per_kind'][kind] for s in scenes.values()]
        ious = []
        out[kind] = {k: sum(r[k] for r in rows) for k in ('baseline_total', 'candidate_total', 'matched', 'only_baseline', 'only_candidate')}
        out[kind]['iou_below_0_8'] = sum(r['iou']['n_below_0_8'] for r in rows)
        out[kind]['identical_rings'] = sum(r['iou']['n_identical_ring'] for r in rows)
        out[kind]['score_up'] = sum(r['score_delta']['n_up'] for r in rows)
        out[kind]['score_down'] = sum(r['score_delta']['n_down'] for r in rows)
        del ious
    lost = {k: sum(s['lost_building_flags'][k] for s in scenes.values()) for k in ('n', 'with_flag_rows', 'secondary_structure', 'outbuilding_hint', 'non_residential_hint')}
    all_b = {k: sum(s['all_baseline_building_flags'][k] for s in scenes.values()) for k in ('n', 'with_flag_rows', 'secondary_structure', 'outbuilding_hint', 'non_residential_hint')}
    refs = [r for s in scenes.values() for r in s['references']['rows']]
    reasons = {}
    for side in ('only_baseline', 'only_candidate'):
        reasons[side] = {}
        for kind in KINDS:
            reasons[side][kind] = {}
            for s in scenes.values():
                for reason, n in s['unmatched_reasons']['counts'][side][kind].items():
                    reasons[side][kind][reason] = reasons[side][kind].get(reason, 0) + n
    out['unmatched_reasons'] = reasons
    out['lost_building_flags'] = lost
    out['all_baseline_building_flags'] = all_b
    out['references'] = {'n': len(refs), 'baseline_iou_median': _median([r['baseline']['iou'] for r in refs]),
                         'candidate_iou_median': _median([r['candidate']['iou'] for r in refs]),
                         'n_candidate_better': sum(r['iou_delta'] > .01 for r in refs), 'n_baseline_better': sum(r['iou_delta'] < -.01 for r in refs),
                         'n_within_0_01': sum(abs(r['iou_delta']) <= .01 for r in refs)}
    return out


def _pct(part, whole):
    return f'{part}/{whole} ({100*part/whole:.0f}%)' if whole else f'{part}/0'


def write_markdown(report, path):
    """REPORT.md in plain sentences with denominators; numbers only from report.json."""
    lines = [f"# CLAHE detector experiment ({report['experiment']})", '',
             f"Computed {report['computed_at']}. {report['note']}", '',
             f"Variable: {report['variable']}. CLAHE: {json.dumps(report['clahe'])}. Match rule: {report['match_rule']}.",
             'Only the whole-frame SAM3 encode saw the CLAHE frame; surface refinement, colours, roof-line evidence and crops kept the saved PNG.', '']
    control = report.get('control')
    if control:
        oi = control['object_identity']
        lines += ['## Control (no variant, same flags)', '',
                  f"Scene {control['scene_id']}: model_signature reproduced = {control['signature_reproduced']}; "
                  f"{oi['shared_ids']}/{oi['baseline']} baseline object ids reappear in the control candidate ({oi['candidate']} objects), "
                  f"{oi['identical_rings']}/{oi['shared_ids']} with byte-identical rings and {oi['identical_scores']}/{oi['shared_ids']} identical scores. "
                  f"Elapsed {control['elapsed_s']} s.", '']
        diff = control.get('baseline_diff') or {}
        if control['signature_reproduced'] is False and diff:
            lines += [f"Signature difference keys: {json.dumps(diff.get('top_level'))}; analysis: {json.dumps(diff.get('analysis'))}. "
                      'With --no-parcel-fetch the candidate carries an empty parcel set, which is expected to differ from production; the detector output (ids, rings, scores) is the reproduction check that matters here.', '']
    t = report['totals']
    lines += ['## Totals over the compared scenes', '']
    for kind in KINDS:
        k = t[kind]
        lines.append(f"- {kind}: baseline {k['baseline_total']}, candidate {k['candidate_total']}; matched {_pct(k['matched'], k['baseline_total'])} of baseline objects "
                     f"and {_pct(k['matched'], k['candidate_total'])} of candidate objects; only-baseline {k['only_baseline']}, only-candidate {k['only_candidate']}; "
                     f"matched pairs with IoU below {LOW_IOU}: {_pct(k['iou_below_0_8'], k['matched'])}; byte-identical rings {_pct(k['identical_rings'], k['matched'])}; "
                     f"detector score up {k['score_up']} / down {k['score_down']} of {k['matched']}.")
    lines += ['', f"Why unmatched objects have no partner (absorbed = merge/split under same-kind objects of the other side, absent = no same-kind overlap): "
              f"only-baseline {json.dumps(t['unmatched_reasons']['only_baseline'])}; only-candidate {json.dumps(t['unmatched_reasons']['only_candidate'])}."]
    lf, ab = t['lost_building_flags'], t['all_baseline_building_flags']
    lines += ['', f"Only-baseline buildings: {lf['n']}; {lf['with_flag_rows']} have a {FLAG_VERSION} row; secondary-structure flagged {lf['secondary_structure']}, "
              f"outbuilding hint {lf['outbuilding_hint']}, non-residential hint {lf['non_residential_hint']}. For all baseline buildings the same rates are "
              f"{_pct(ab['secondary_structure'], ab['with_flag_rows'])} secondary-structure and {_pct(ab['outbuilding_hint'], ab['with_flag_rows'])} outbuilding "
              f"(rows exist for {ab['with_flag_rows']}/{ab['n']} buildings; owned roofs only).", '']
    r = t['references']
    lines += [f"T1/T2 references whose bounds fall in these tiles: {r['n']}. Best-building IoU with the reference: baseline median {r['baseline_iou_median']}, "
              f"candidate median {r['candidate_iou_median']}; candidate better by more than 0.01 for {r['n_candidate_better']}, baseline better for {r['n_baseline_better']}, "
              f"within 0.01 for {r['n_within_0_01']} of {r['n']}. Reference agreement, not accuracy.", '']
    for sid, sc in report['scenes'].items():
        c = sc['counts']
        lines += [f'## {sid}', '',
                  f"Baseline {c['baseline_objects']} objects ({c['baseline_owned']} owned), candidate {c['candidate_objects']}; degenerate rings skipped {c['skipped_degenerate']}. "
                  f"Isolated run: {json.dumps(sc['isolated_run'])}. Detector frame substitutions: {(sc.get('variables') or {}).get('detector_frame_substitutions')}.", '']
        for kind in KINDS:
            k = sc['per_kind'][kind]
            lines.append(f"- {kind}: baseline {k['baseline_total']}, candidate {k['candidate_total']}, matched {k['matched']}, only-baseline {k['only_baseline']}, only-candidate {k['only_candidate']}. "
                         f"Matched IoU median {k['iou']['median']}, p10 {k['iou']['p10']}, min {k['iou']['min']}, below {LOW_IOU}: {k['iou']['n_below_0_8']}, identical rings {k['iou']['n_identical_ring']}. "
                         f"Area change (candidate minus baseline, m²): median {k['area_m2_delta']['median']}, p10 {k['area_m2_delta']['p10']}, p90 {k['area_m2_delta']['p90']}, median relative {k['area_m2_delta']['median_relative']}. "
                         f"Score change: median {k['score_delta']['median']}, p10 {k['score_delta']['p10']}, p90 {k['score_delta']['p90']}, up {k['score_delta']['n_up']}, down {k['score_delta']['n_down']}. "
                         f"Ring vertices median baseline {k['vertices']['baseline_median']} vs candidate {k['vertices']['candidate_median']} (raw rings {k['vertices']['raw_baseline_median']} vs {k['vertices']['raw_candidate_median']}). "
                         f"Only-baseline score median {k['only_baseline_score_median']}, area median {k['only_baseline_area_m2_median']} m²; only-candidate score median {k['only_candidate_score_median']}, area median {k['only_candidate_area_m2_median']} m².")
        lf = sc['lost_building_flags']
        lines += ['', f"Only-baseline buildings with {FLAG_VERSION} rows: {lf['with_flag_rows']}/{lf['n']}; secondary-structure {lf['secondary_structure']}, outbuilding hint {lf['outbuilding_hint']}, "
                  f"non-residential hint {lf['non_residential_hint']}; class hints {json.dumps(lf['class_hints'])}."]
        for o in lf['objects']:
            lines.append(f"  - {o['object_id']}: flags {o.get('flags')}, class hint {o.get('class_hint')}, score {o.get('score')}, area {o.get('area_m2')} m²")
        ur = sc['unmatched_reasons']
        lines += ['', f"Unmatched reasons: only-baseline {json.dumps(ur['counts']['only_baseline'])}; only-candidate {json.dumps(ur['counts']['only_candidate'])}."]
        for row in ur['only_baseline'] + ur['only_candidate']:
            if row['reason'] == 'absorbed':
                lines.append(f"  - {row['kind']} {row['object_id']} absorbed ({row['covered_share']}) by {[(q['object_id'], q['iou']) for q in row['partners']]}")
        f = sc.get('focus_object')
        if f:
            lines += ['', f"Object {f['object_id']}: {json.dumps(f)}"]
        refs = sc['references']
        lines += ['', f"T1/T2 references in tile: {refs['n_in_tile']} ({refs['same_image']} drawn over this scene). Baseline IoU median {refs['baseline_iou_median']}, candidate {refs['candidate_iou_median']}; "
                  f"candidate better {refs['n_candidate_better']}, baseline better {refs['n_baseline_better']}."]
        for row in refs['rows']:
            lines.append(f"  - {row['ref_id'][:12]} {row['tier']} {row['source_kind']} from {row['scene_id']}/{row['object_id']} (same image: {row['same_image']}): "
                         f"baseline IoU {row['baseline']['iou']} ({row['baseline']['object_id']}), candidate IoU {row['candidate']['iou']} ({row['candidate']['object_id']}), delta {row['iou_delta']}")
        if sc['low_iou_pairs']:
            lines += ['', f"Matched pairs with IoU below {LOW_IOU} ({len(sc['low_iou_pairs'])}):"]
            for pair in sc['low_iou_pairs']:
                lines.append(f"  - {pair['kind']} {pair['baseline_id']} -> {pair['candidate_id']}: IoU {pair['iou']}, area {pair['baseline']['area_m2']} -> {pair['candidate']['area_m2']} m², "
                             f"score {pair['baseline']['score']} -> {pair['candidate']['score']}, vertices {pair['baseline']['vertices']} -> {pair['candidate']['vertices']}")
        lines += ['', f"Contact sheet: {sc.get('contact_sheet')} (counts {json.dumps(sc.get('contact_sheet_counts'))}).", '']
    Path(path).write_text('\n'.join(lines) + '\n')
    return path


def compare_experiment(experiment_root, scene_ids, *, control=None, eyes_db=EYES_DB, repo=REPO):
    experiment_root = Path(experiment_root)
    scenes, written = {}, []
    for sid in scene_ids:
        sheet = experiment_root/'sheets'/f'{sid}-diff.png'
        scenes[sid] = compare_scene(experiment_root/'runs'/sid, sid, eyes_db=eyes_db, sheet_path=sheet, repo=repo)
        written.append(str(sheet))
    report = {'version': VERSION, 'experiment': 'clahe-detector-v1', 'variable': 'whole-frame SAM3 encode on a CLAHE variant of the saved PNG',
              'clahe': CLAHE_PARAMS, 'match_rule': f'greedy IoU >= {MATCH_IOU} per kind on pixel rings', 'note': NOTE,
              'control': control_check(experiment_root/'runs'/f'{control}--control', control) if control else None,
              'scenes': scenes, 'totals': totals(scenes), 'code_sha256': CODE_SHA256, 'computed_at': datetime.now(UTC).isoformat()}
    path = experiment_root/'report.json'
    path.write_text(json.dumps(report, indent=1, default=str))
    written.append(str(path))
    written.append(str(write_markdown(report, experiment_root/'REPORT.md')))
    report['written'] = written
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    v = sub.add_parser('variant', help='Build the CLAHE .npy for one saved scene.')
    v.add_argument('scene_id')
    v.add_argument('--out', type=Path, required=True)
    v.add_argument('--preview', type=Path)
    c = sub.add_parser('compare', help='Compare baseline vs candidate runs and write report.json / REPORT.md / contact sheets.')
    c.add_argument('--experiment-root', type=Path, required=True)
    c.add_argument('--scenes', nargs='+', required=True)
    c.add_argument('--control', help='Scene id whose no-variant control run sits at runs/<id>--control.')
    c.add_argument('--eyes-db', type=Path, default=EYES_DB)
    args = parser.parse_args(argv)
    if args.command == 'variant':
        print(json.dumps(build_variant(args.scene_id, args.out, preview=args.preview), indent=1))
        return 0
    report = compare_experiment(args.experiment_root.resolve(), args.scenes, control=args.control, eyes_db=args.eyes_db)
    print(json.dumps({'scenes': list(report['scenes']), 'written': report['written']}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
