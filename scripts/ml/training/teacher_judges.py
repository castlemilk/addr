"""Independent judges for a hi-res SAM3 teacher run (teacher-judges-v1).

A teacher enhancement must be scored by something other than the shape prior it optimises. Three judges, all
agreement or evidence measures, never accuracy:

  outlines      per instance, overlap with the open building outlines fetched by teacher_outlines.py (LINZ, CC BY
                4.0, metre-class edges): matched (IoU >= 0.5), part (mostly inside one outline but much smaller),
                merged (covers two or more outlines), unmatched (no outline touched). Coverage: outlines no teacher
                instance covers. Same for the control arm on the student grid. Judges counts, not corners.
  edge-support  fraction of each outline's boundary that lies within a small band of a strong image gradient on the
                NATIVE pixels (Sobel magnitude above the window's 90th percentile, distance transform). Judges whether
                a boundary sits on a real edge; a regulariser that moves edges off the image loses here.
  cross-capture the same cells taught from a second capture of the same houses (teacher_sources select
                --windows-from): one-to-one matches by IoU on a common grid, IoU and mean boundary distance of the
                matched pairs, and the share of instances present in one capture only. Stability across captures is
                the strongest independent signal available without people.

Outputs go under <teacher>/judges/. Per-instance rows are kept so later enhancements can be weighed instance by
instance (a confidence for the training kit).

  scripts/ml/training/.venv/bin/python -m scripts.ml.training.teacher_judges outlines --source linz-chc-2025-0075
  scripts/ml/training/.venv/bin/python -m scripts.ml.training.teacher_judges edge-support --source linz-chc-2025-0075
  scripts/ml/training/.venv/bin/python -m scripts.ml.training.teacher_judges cross-capture --source linz-chc-2025-0075 --other linz-chc-2020-0075
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import common
from . import hires_teacher as ht
from . import teacher_outlines as outlines
from . import teacher_sources as ts

VERSION = 'teacher-judges-v1'
MATCH_IOU = .5
PART_INSIDE = .8            # instance mostly inside one outline ...
PART_RATIO = .6             # ... and covering less than this share of it: a roof part
MERGED_COVER = .5           # an instance covering this share of two or more outlines: merged buildings
TOUCH = .1                  # below this overlap share nothing counts as touched
OUTLINE_MIN_M2 = 6.         # outlines smaller than the teacher's own floor are not expected
JUDGED_MIN_M2 = 10.         # LINZ captures buildings of 10 m2 and more; smaller instances cannot be judged by outlines
EDGE_PERCENTILE = 90.
EDGE_BAND_M = .15
SAMPLE_STEP_M = .1
AREA_BANDS = (('lt_50m2', 0., 50.), ('50_200m2', 50., 200.), ('ge_200m2', 200., float('inf')))


def _num(value, digits=4):
    return None if value is None else round(float(value), digits)


def _spread(values):
    values = np.array([v for v in values if v is not None], dtype=float)
    if not values.size:
        return {'n': 0, 'median': None}
    return {'n': int(values.size), 'median': _num(np.median(values)), 'q25': _num(np.percentile(values, 25)),
            'q75': _num(np.percentile(values, 75)), 'mean': _num(values.mean())}


def _band(area_m2):
    for name, lo, hi in AREA_BANDS:
        if lo <= area_m2 < hi:
            return name
    return AREA_BANDS[-1][0]


def window_bootstrap_share(per_window_num, per_window_den, seed=20260928, draws=1000):
    """95 percent interval of a pooled share, resampling windows (the unit of independence)."""
    num, den = np.array(per_window_num, float), np.array(per_window_den, float)
    if not den.sum():
        return None
    rng = np.random.default_rng(seed)
    shares = []
    for _ in range(draws):
        idx = rng.integers(0, len(num), len(num))
        d = den[idx].sum()
        shares.append(num[idx].sum() / d if d else np.nan)
    shares = np.array(shares)
    shares = shares[np.isfinite(shares)]
    return [_num(np.percentile(shares, 2.5)), _num(np.percentile(shares, 97.5))] if shares.size else None


def judges_dir(root, spec):
    return ts.teacher_dir(root, spec) / 'judges'


def _load(root, source_id):
    root = Path(root)
    spec = ts.resolved(ts.source(source_id), root)
    ht.set_source(source_id, root)
    crops, records = ht.load_records(root)
    return root, spec, crops, records


def teacher_polygons(record, simplify_px=ht.SIMPLIFY_PX):
    """(row, polygon in native px) for every teacher instance with a vectorisable mask."""
    rows = []
    for row in record['teacher']:
        piece = ht.rle_piece(row['rle_native'], row['bbox_native'], row['score'])
        polygon = ht.exterior_polygon(piece, simplify_px)
        if polygon is not None and polygon.is_valid and not polygon.is_empty:
            rows.append((row, polygon))
    return rows


def control_polygons(record, simplify_px=ht.SIMPLIFY_PX):
    """(row, polygon in student px) for every control instance."""
    rows = []
    for row in record['control']:
        piece = ht.rle_piece(row['rle'], row['bbox'], row['score'])
        polygon = ht.exterior_polygon(piece, simplify_px)
        if polygon is not None and polygon.is_valid and not polygon.is_empty:
            rows.append((row, polygon))
    return rows


# --- outlines judge --------------------------------------------------------------------------------------------------

def classify_against_outlines(polygon, outline_polys, tree=None):
    """How one instance relates to the open outlines: matched / part / merged / unmatched, with the numbers behind it."""
    from shapely.strtree import STRtree
    if tree is None:
        tree = STRtree(outline_polys)
    hits = [outline_polys[i] for i in tree.query(polygon)] if outline_polys else []
    area = polygon.area
    best_iou, best_inside, covered = 0., 0., []
    for outline in hits:
        inter = polygon.intersection(outline).area
        if inter <= 0:
            continue
        union = polygon.union(outline).area
        best_iou = max(best_iou, inter / union if union else 0.)
        best_inside = max(best_inside, inter / area if area else 0.)
        if outline.area and inter / outline.area >= MERGED_COVER:
            covered.append(outline)
    if best_iou >= MATCH_IOU:
        kind = 'matched'
    elif len(covered) >= 2:
        kind = 'merged'
    elif best_inside >= PART_INSIDE and best_iou < PART_RATIO:
        kind = 'part'
    elif best_inside >= TOUCH:
        kind = 'overlapping'
    else:
        kind = 'unmatched'
    return {'kind': kind, 'iou_best': _num(best_iou), 'inside_best': _num(best_inside), 'outlines_covered': len(covered)}


def outlines_judge(root, source_id, limit=None):
    root, spec, crops, records = _load(root, source_id)
    gsd = crops['gsd_m']
    size = crops['native_window_px']
    student_m = size * gsd / ht.STUDENT_PX
    rows, per_window = [], []
    for record in records[:limit or len(records)]:
        w = record['window']
        outline_rows = outlines.load(root, spec, w['id'])
        if outline_rows is None:
            continue
        native_affine = w['native_affine']
        from shapely.geometry import box as shapely_box
        window_box = shapely_box(0., 0., float(size), float(size))
        outline_native = outlines.to_window_pixels([r['geometry'] for r in outline_rows], native_affine)
        # clip to the window: an outline that lies outside the crop cannot be covered and must not count against coverage
        outline_native = [q for q in (p.intersection(window_box) for p in outline_native)
                          if not q.is_empty and q.geom_type in ('Polygon', 'MultiPolygon') and q.area * gsd * gsd >= OUTLINE_MIN_M2]
        outline_native = [max(q.geoms, key=lambda g: g.area) if q.geom_type == 'MultiPolygon' else q for q in outline_native]
        scale = ht.STUDENT_PX / size
        from shapely import affinity
        outline_student = [affinity.scale(p, xfact=scale, yfact=scale, origin=(0, 0)) for p in outline_native]
        from shapely.strtree import STRtree
        tree_native = STRtree(outline_native) if outline_native else None
        tree_student = STRtree(outline_student) if outline_student else None
        counts = {'teacher': {}, 'control': {}}
        covered_by = {'teacher': np.zeros(len(outline_native), bool), 'control': np.zeros(len(outline_native), bool)}
        for arm, polys, tree, outs, px_m in (('teacher', teacher_polygons(record), tree_native, outline_native, gsd),
                                              ('control', control_polygons(record), tree_student, outline_student, student_m)):
            for row, polygon in polys:
                result = classify_against_outlines(polygon, outs, tree) if outs else {'kind': 'no_outlines', 'iou_best': None,
                                                                                       'inside_best': None, 'outlines_covered': 0}
                area_m2 = polygon.area * px_m * px_m
                three = {'matched': 'whole', 'part': 'part', 'unmatched': 'unmatched'}.get(result['kind'], 'other')
                rows.append({'window': w['id'], 'arm': arm, 'index': row['index'], 'score': row['score'], 'area_m2': _num(area_m2, 2),
                             'band': _band(area_m2), 'judged': area_m2 >= JUDGED_MIN_M2, 'three_class': three, **result})
                counts[arm][result['kind']] = counts[arm].get(result['kind'], 0) + 1
                if outs:
                    for i in tree.query(polygon):
                        o = outs[i]
                        if o.area and polygon.intersection(o).area / o.area >= MERGED_COVER:
                            covered_by[arm][i] = True
        per_window.append({'window': w['id'], 'outlines': len(outline_native), 'teacher_instances': len(record['teacher']),
                           'control_instances': len(record['control']), 'kinds': counts,
                           'outlines_covered': {arm: int(v.sum()) for arm, v in covered_by.items()}})
    summary = summarise_outlines(rows, per_window)
    folder = judges_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / 'outlines_rows.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    payload = {'version': VERSION, 'judge': 'outlines', 'source': spec['id'], 'computed_at': common.now_iso(),
               'thresholds': {'match_iou': MATCH_IOU, 'part_inside': PART_INSIDE, 'part_ratio': PART_RATIO, 'merged_cover': MERGED_COVER,
                              'touch': TOUCH, 'outline_min_m2': OUTLINE_MIN_M2, 'judged_min_m2': JUDGED_MIN_M2,
                              'outlines_clipped_to_window': True},
               'note': ('open building outlines are metre-class and may predate the imagery; an unmatched instance is a '
                        'candidate roof part or non-building, not a proven error; agreement, not accuracy'),
               'summary': summary, 'windows': per_window}
    common.write_json_atomic(folder / 'outlines.json', payload)
    return {k: payload[k] for k in ('version', 'judge', 'source', 'summary')}


def summarise_outlines(rows, per_window):
    out = {}
    for arm in ('teacher', 'control'):
        arm_rows = [r for r in rows if r['arm'] == arm]
        kinds = {}
        for r in arm_rows:
            kinds[r['kind']] = kinds.get(r['kind'], 0) + 1
        n = len(arm_rows)
        by_band = {}
        for name, _, _ in AREA_BANDS:
            band_rows = [r for r in arm_rows if r['band'] == name]
            by_band[name] = {'n': len(band_rows), 'matched_share': _num(sum(r['kind'] == 'matched' for r in band_rows) / len(band_rows))
                             if band_rows else None,
                             'unmatched_share': _num(sum(r['kind'] == 'unmatched' for r in band_rows) / len(band_rows)) if band_rows else None}
        num = [pw['kinds'][arm].get('matched', 0) for pw in per_window]
        den = [sum(pw['kinds'][arm].values()) for pw in per_window]
        cov_num = [pw['outlines_covered'][arm] for pw in per_window]
        cov_den = [pw['outlines'] for pw in per_window]
        judged = [r for r in arm_rows if r.get('judged', True)]
        out[arm] = {'instances': n, 'kinds': kinds, 'judged_instances': len(judged),
                    'three_class_judged': {c: _num(sum(r['three_class'] == c for r in judged) / len(judged)) if judged else None
                                           for c in ('whole', 'part', 'unmatched', 'other')},
                    'matched_share': _num(kinds.get('matched', 0) / n) if n else None,
                    'matched_share_ci95_window_bootstrap': window_bootstrap_share(num, den),
                    'part_share': _num(kinds.get('part', 0) / n) if n else None,
                    'merged_share': _num(kinds.get('merged', 0) / n) if n else None,
                    'unmatched_share': _num(kinds.get('unmatched', 0) / n) if n else None,
                    'outline_coverage': _num(sum(cov_num) / sum(cov_den)) if sum(cov_den) else None,
                    'outline_coverage_ci95_window_bootstrap': window_bootstrap_share(cov_num, cov_den),
                    'iou_best_matched': _spread([r['iou_best'] for r in arm_rows if r['kind'] == 'matched']),
                    'by_area': by_band}
    out['outlines_total'] = int(sum(pw['outlines'] for pw in per_window))
    out['windows'] = len(per_window)
    return out


# --- edge-support judge ----------------------------------------------------------------------------------------------

def edge_distance_map(native_rgb, percentile=EDGE_PERCENTILE):
    """Distance (px) from every pixel to the nearest strong-gradient pixel of the native image."""
    import cv2
    from scipy import ndimage
    gray = cv2.cvtColor(np.asarray(native_rgb, dtype=np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = np.hypot(gx, gy)
    threshold = float(np.percentile(magnitude, percentile))
    strong = magnitude > threshold          # strictly above: a mostly flat image must not mark every pixel as an edge
    if not strong.any():
        strong = magnitude > 0.
    return ndimage.distance_transform_edt(~strong), threshold


def boundary_support(polygon, distance_map, band_px, step_px):
    """Share of boundary samples within band_px of a strong edge; None when the polygon has no length."""
    ring = polygon.exterior
    count = max(int(ring.length / step_px), 8)
    h, w = distance_map.shape
    hits = 0
    for k in range(count):
        point = ring.interpolate(k / count, normalized=True)
        x, y = int(round(point.x)), int(round(point.y))
        if 0 <= x < w and 0 <= y < h and distance_map[y, x] <= band_px:
            hits += 1
    return hits / count


def edge_support_judge(root, source_id, limit=None, band_m=EDGE_BAND_M, step_m=SAMPLE_STEP_M):
    from PIL import Image
    from shapely import affinity
    root, spec, crops, records = _load(root, source_id)
    gsd = crops['gsd_m']
    size = crops['native_window_px']
    rows, per_window = [], []
    for record in records[:limit or len(records)]:
        w = record['window']
        native = np.asarray(Image.open(root / w['native_file']).convert('RGB'))
        distance, threshold = edge_distance_map(native)
        band_px, step_px = band_m / gsd, max(1., step_m / gsd)
        scale = size / ht.STUDENT_PX
        window_rows = []
        for arm, polys, to_native in (('teacher', teacher_polygons(record), None), ('control', control_polygons(record), scale)):
            for row, polygon in polys:
                if to_native:
                    polygon = affinity.scale(polygon, xfact=to_native, yfact=to_native, origin=(0, 0))
                support = boundary_support(polygon, distance, band_px, step_px)
                area_m2 = polygon.area * gsd * gsd
                window_rows.append({'window': w['id'], 'arm': arm, 'index': row['index'], 'area_m2': _num(area_m2, 2),
                                    'band': _band(area_m2), 'edge_support': _num(support)})
        # chance level: the same share for a random circle of median size dropped at random places
        rng = np.random.default_rng(hash(w['id']) & 0xffff)
        chance = []
        for _ in range(30):
            radius = 6. / gsd
            cx, cy = rng.uniform(radius, size - radius, 2)
            from shapely.geometry import Point
            chance.append(boundary_support(Point(cx, cy).buffer(radius, 16), distance, band_px, step_px))
        rows.extend(window_rows)
        per_window.append({'window': w['id'], 'edge_threshold': _num(threshold, 1), 'chance_support': _num(np.mean(chance)),
                           **{arm: _spread([r['edge_support'] for r in window_rows if r['arm'] == arm]) for arm in ('teacher', 'control')}})
    summary = {}
    for arm in ('teacher', 'control'):
        arm_rows = [r for r in rows if r['arm'] == arm]
        summary[arm] = {'edge_support': _spread([r['edge_support'] for r in arm_rows]),
                        'by_area': {name: _spread([r['edge_support'] for r in arm_rows if r['band'] == name]) for name, _, _ in AREA_BANDS}}
    summary['chance_support'] = _spread([pw['chance_support'] for pw in per_window])
    paired = {}
    for pw in per_window:
        if pw['teacher']['median'] is not None and pw['control']['median'] is not None:
            paired[pw['window']] = pw['teacher']['median'] - pw['control']['median']
    summary['teacher_minus_control_per_window_median'] = _spread(list(paired.values()))
    summary['band_m'], summary['step_m'], summary['edge_percentile'] = band_m, step_m, EDGE_PERCENTILE
    folder = judges_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / 'edge_support_rows.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    payload = {'version': VERSION, 'judge': 'edge-support', 'source': spec['id'], 'computed_at': common.now_iso(),
               'note': ('share of each outline\'s boundary within the band of a strong native gradient; control outlines are '
                        'mapped from the student grid to native pixels; chance_support is the same measure for random '
                        'circles; evidence that a boundary sits on an image edge, not accuracy'),
               'summary': summary, 'windows': per_window}
    common.write_json_atomic(folder / 'edge_support.json', payload)
    return {k: payload[k] for k in ('version', 'judge', 'source', 'summary')}


# --- cross-capture judge ---------------------------------------------------------------------------------------------

def _registration_shifts(folder, other_id):
    """Per-instance (dx, dy) native px from A to B when NCC passed, plus the per-window shift as the fallback."""
    per_instance, per_window = {}, {}
    rows_path = folder / f'register_{other_id}_rows.jsonl'
    meta_path = folder / f'register_{other_id}.json'
    if rows_path.exists():
        for r in _read_rows(rows_path):
            if r.get('status') == 'ok' and r.get('ncc', 0.) >= REG['ncc_min']:
                per_instance[(r['window'], r['index'])] = (r['dx_px'], r['dy_px'])
    if meta_path.exists():
        meta = common.read_json(meta_path)
        gsd = meta['summary']['settings']['gsd_m']
        for w in meta.get('windows', []):
            per_window[w['window']] = (w['global_dx_m'] / gsd, w['global_dy_m'] / gsd)
    return per_instance, per_window


def cross_capture_judge(root, source_id, other_id, limit=None):
    """Teacher instances of two captures of the same cells, matched one-to-one on the native grid.

    When `register` has run, each A instance is moved by its own image-derived shift (the window shift when its NCC
    failed) before IoU is computed, so agreement is not depressed by the captures' own misregistration."""
    from shapely import affinity
    root, spec, crops, records = _load(root, source_id)
    other = ts.resolved(ts.source(other_id), root)
    ht.set_source(other_id, root)
    other_crops, other_records = ht.load_records(root)
    ht.set_source(source_id, root)
    if abs(other_crops['gsd_m'] - crops['gsd_m']) > 1e-9:
        raise SystemExit('cross-capture needs equal pixel sizes; 5 cm teacher records do not exist yet')
    gsd = crops['gsd_m']
    shifts, window_shifts = _registration_shifts(judges_dir(root, spec), other['id'])
    by_cell = {(r['window']['row'], r['window']['col']): r for r in other_records}
    rows, per_window = [], []
    for record in records[:limit or len(records)]:
        w = record['window']
        twin = by_cell.get((w['row'], w['col']))
        if twin is None:
            continue
        a = teacher_polygons(record)
        b = teacher_polygons(twin)
        # both on native px of their own window; same cell and pixel, so coordinates coincide when the GSDs match
        ratio = other_crops['gsd_m'] / gsd
        if abs(ratio - 1.) > 1e-6:
            b = [(row, affinity.scale(p, xfact=ratio, yfact=ratio, origin=(0, 0))) for row, p in b]
        fallback = window_shifts.get(w['id'], (0., 0.))
        pieces_a = []
        for row, _ in a:
            piece = ht.rle_piece(row['rle_native'], row['bbox_native'], row['score'])
            dx, dy = shifts.get((w['id'], row['index']), fallback)
            piece.x0, piece.y0 = int(round(piece.x0 + dx)), int(round(piece.y0 + dy))
            pieces_a.append(piece)
        pieces_b = [ht.rle_piece(row['rle_native'], row['bbox_native'], row['score']) for row, _ in b]
        matches = ht.match_pieces(pieces_a, pieces_b, MATCH_IOU) if pieces_a and pieces_b else []
        matched_a = {i for i, _, _ in matches}
        matched_b = {j for _, j, _ in matches}
        for i, j, iou in matches:
            dx, dy = shifts.get((w['id'], a[i][0]['index']), fallback)
            pa, pb = affinity.translate(a[i][1], dx, dy), b[j][1]
            to_m = [gsd, 0., 0., gsd, 0., 0.]
            dist = ht.mean_boundary_distance(affinity.affine_transform(pa, to_m), affinity.affine_transform(pb, to_m))
            area_m2 = pa.area * gsd * gsd
            rows.append({'window': w['id'], 'index': a[i][0]['index'], 'other_index': b[j][0]['index'], 'iou': _num(iou),
                         'mean_boundary_distance_m': _num(dist, 3), 'area_m2': _num(area_m2, 2), 'band': _band(area_m2)})
        per_window.append({'window': w['id'], 'instances': len(a), 'other_instances': len(b), 'matched': len(matches),
                           'only_here': len(a) - len(matched_a), 'only_other': len(b) - len(matched_b)})
    total_a = sum(pw['instances'] for pw in per_window)
    total_b = sum(pw['other_instances'] for pw in per_window)
    matched = sum(pw['matched'] for pw in per_window)
    summary = {'windows': len(per_window), 'instances': total_a, 'other_instances': total_b, 'matched_pairs': matched,
               'registration': {'per_instance_shifts': len(shifts), 'window_shifts': len(window_shifts),
                                'note': 'A instances moved by their image-derived shift before matching' if shifts or window_shifts
                                else 'no registration found: raw coordinates'},
               'matched_share': _num(matched / total_a) if total_a else None,
               'matched_share_ci95_window_bootstrap': window_bootstrap_share([pw['matched'] for pw in per_window],
                                                                             [pw['instances'] for pw in per_window]),
               'only_here_share': _num(sum(pw['only_here'] for pw in per_window) / total_a) if total_a else None,
               'only_other_share': _num(sum(pw['only_other'] for pw in per_window) / total_b) if total_b else None,
               'iou_matched': _spread([r['iou'] for r in rows]),
               'mean_boundary_distance_m_matched': _spread([r['mean_boundary_distance_m'] for r in rows]),
               'by_area': {name: {'n': sum(r['band'] == name for r in rows),
                                  'iou_median': _spread([r['iou'] for r in rows if r['band'] == name])['median']} for name, _, _ in AREA_BANDS}}
    folder = judges_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f'cross_capture_{other["id"]}_rows.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    payload = {'version': VERSION, 'judge': 'cross-capture', 'source': spec['id'], 'other': other['id'], 'computed_at': common.now_iso(),
               'note': ('teacher instances of two captures of the same cells matched one-to-one at IoU >= 0.5 on the native '
                        'grid; a stable instance is not thereby correct, but instability marks the least trustworthy labels'),
               'summary': summary, 'windows': per_window}
    common.write_json_atomic(folder / f'cross_capture_{other["id"]}.json', payload)
    return {k: payload[k] for k in ('version', 'judge', 'source', 'other', 'summary')}



# --- two-capture registration (image only; never by mask agreement) ------------------------------------------------

REG = {'downsample': 4, 'search_m': 2.5, 'pad_m': 2., 'ncc_min': .35, 'min_area_m2': 20., 'blur_px': 2.}
# measured on four Christchurch cells (2025 vs 2020-21): NCC on raw gradient magnitude matched 0-2 percent of buildings at
# 0.5; grey patches 21-51 percent; gradient magnitude blurred by 2 px 71-90 percent at 0.35. Residuals against the
# per-window shift run 0.3-1.4 m, so buildings really do move between captures and a per-building offset is needed.


def load_native(root, record):
    from PIL import Image
    return np.asarray(Image.open(Path(root) / record['window']['native_file']).convert('RGB'))


def gray_f32(rgb):
    import cv2
    return cv2.cvtColor(np.asarray(rgb, dtype=np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32)


def sobel(gray):
    import cv2
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return gx, gy, np.hypot(gx, gy)


def registration_image(gray, blur_px=REG['blur_px']):
    """Gradient magnitude softened by a small Gaussian: what the per-building NCC correlates."""
    import cv2
    _, _, mag = sobel(gray)
    return cv2.GaussianBlur(mag, (0, 0), blur_px) if blur_px else mag


def global_shift(gray_a, gray_b, downsample=REG['downsample']):
    """Whole-window translation B relative to A in native px (phase correlation on downsampled grey), and its response."""
    import cv2
    small_a = cv2.resize(gray_a, None, fx=1. / downsample, fy=1. / downsample, interpolation=cv2.INTER_AREA)
    small_b = cv2.resize(gray_b, None, fx=1. / downsample, fy=1. / downsample, interpolation=cv2.INTER_AREA)
    window = cv2.createHanningWindow(small_a.shape[::-1], cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(small_a - small_a.mean(), small_b - small_b.mean(), window)
    return float(dx * downsample), float(dy * downsample), float(response)


def _parabolic(values, index):
    """Sub-pixel peak offset from a 3-point parabola around index (clamped to +-0.5)."""
    if index <= 0 or index >= len(values) - 1:
        return 0.
    left, centre, right = values[index - 1], values[index], values[index + 1]
    denominator = left - 2 * centre + right
    if abs(denominator) < 1e-9:
        return 0.
    return float(np.clip(.5 * (left - right) / denominator, -.5, .5))


def ncc_shift(mag_a, mag_b, bounds_px, pad_px, search_px):
    """Translation of the building patch from A to B by normalised cross-correlation of gradient magnitude.

    Returns (dx, dy, ncc_peak) in native px, or None when the patch leaves the window."""
    import cv2
    h, w = mag_a.shape
    x0, y0, x1, y1 = (int(np.floor(bounds_px[0])) - pad_px, int(np.floor(bounds_px[1])) - pad_px,
                      int(np.ceil(bounds_px[2])) + pad_px, int(np.ceil(bounds_px[3])) + pad_px)
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h or x1 - x0 < 8 or y1 - y0 < 8:
        return None
    template = mag_a[y0:y1, x0:x1]
    sx0, sy0, sx1, sy1 = x0 - search_px, y0 - search_px, x1 + search_px, y1 + search_px
    if sx0 < 0 or sy0 < 0 or sx1 > w or sy1 > h:
        return None
    region = mag_b[sy0:sy1, sx0:sx1]
    if template.std() < 1e-6 or region.std() < 1e-6:
        return None
    result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    iy, ix = np.unravel_index(int(np.argmax(result)), result.shape)
    dx = ix - search_px + _parabolic(result[iy, :], ix)
    dy = iy - search_px + _parabolic(result[:, ix], iy)
    return float(dx), float(dy), float(result[iy, ix])


def twin_records(root, source_id, other_id, need_records=False):
    """A's records paired with B's window of the same cell (row, col).

    The B side is the crops entry (native pixels) unless need_records is set, so registration and the boundary judge
    run as soon as B's windows are cut; cross-capture matching needs B's teacher records. Both sources are loaded
    fully before set_source switches back."""
    root, spec, crops, records = _load(root, source_id)
    other = ts.resolved(ts.source(other_id), root)
    ht.set_source(other_id, root)
    other_crops, other_records = ht.load_records(root)
    ht.set_source(source_id, root)
    if need_records:
        by_cell = {(r['window']['row'], r['window']['col']): r for r in other_records}
    else:
        by_cell = {(w['row'], w['col']): {'window': w} for w in other_crops['windows']}
    pairs = [(r, by_cell[(r['window']['row'], r['window']['col'])]) for r in records
             if (r['window']['row'], r['window']['col']) in by_cell]
    return root, spec, other, crops, other_crops, pairs


def register(root, source_id, other_id, limit=None, check_only=False):
    """Per-window global shift and per-building NCC shift from capture A to capture B, image content only.

    When B's pixel is finer than A's (the 5 cm capture against the 7.5 cm one) B's grey is area-resampled onto A's
    grid by exactly gsd_b / gsd_a (5436 -> 3624 px) before anything is correlated: shifts stay in A pixels, dx_m /
    dy_m are added, and ncc_same_grid (A's grey against B's resampled grey moved back by the window shift) records
    whether the two products share photons. Equal pixels run the original code unchanged.
    check_only computes every row in memory, returns {rows, summary} and writes nothing (no folder, no file)."""
    root, spec, other, crops, other_crops, pairs = twin_records(root, source_id, other_id)
    gsd = crops['gsd_m']
    other_gsd = other_crops['gsd_m']
    resample = abs(other_gsd - gsd) > 1e-9
    if resample and other_gsd > gsd:
        raise SystemExit(f'registration resamples the finer capture onto the coarser grid; {other["id"]} ({other_gsd} m) '
                         f'is coarser than {spec["id"]} ({gsd} m)')
    pad_px, search_px = int(round(REG['pad_m'] / gsd)), int(round(REG['search_m'] / gsd))
    rows, per_window = [], []
    for record, twin in pairs[:limit or len(pairs)]:
        gray_a, gray_b = gray_f32(load_native(root, record)), gray_f32(load_native(root, twin))
        if resample:
            expected = (gray_b.shape[0] * other_gsd / gsd, gray_b.shape[1] * other_gsd / gsd)
            if abs(expected[0] - gray_a.shape[0]) > .5 or abs(expected[1] - gray_a.shape[1]) > .5:
                raise SystemExit(f"{twin['window']['id']}: {gray_b.shape[:2]} px at {other_gsd} m is not the ground of "
                                 f"{record['window']['id']}: {gray_a.shape[:2]} px at {gsd} m (re-crop the other capture)")
            gray_b = _resample_to_grid(gray_b, gray_a.shape[0], gray_a.shape[1])
        gdx, gdy, response = global_shift(gray_a, gray_b)
        mag_a, mag_b = registration_image(gray_a), registration_image(gray_b)
        n_ok = 0
        for row, polygon in teacher_polygons(record):
            if polygon.area * gsd * gsd < REG['min_area_m2']:
                continue
            result = ncc_shift(mag_a, mag_b, polygon.bounds, pad_px, search_px)
            if result is None:
                rows.append({'window': record['window']['id'], 'index': row['index'], 'status': 'outside_search'})
                continue
            dx, dy, ncc = result
            n_ok += ncc >= REG['ncc_min']
            entry = {'window': record['window']['id'], 'index': row['index'], 'status': 'ok', 'dx_px': _num(dx, 3), 'dy_px': _num(dy, 3),
                     'residual_m': _num(np.hypot(dx - gdx, dy - gdy) * gsd, 4), 'ncc': _num(ncc), 'area_m2': _num(polygon.area * gsd * gsd, 1)}
            if resample:
                entry['dx_m'], entry['dy_m'] = _num(dx * gsd, 4), _num(dy * gsd, 4)
            rows.append(entry)
        window = {'window': record['window']['id'], 'global_dx_m': _num(gdx * gsd, 4), 'global_dy_m': _num(gdy * gsd, 4),
                  'global_shift_m': _num(np.hypot(gdx, gdy) * gsd, 4), 'phase_response': _num(response), 'buildings_ncc_ok': int(n_ok)}
        if resample:
            window['ncc_same_grid'] = _num(_same_grid_ncc(gray_a, gray_b, gdx, gdy, search_px))
        per_window.append(window)
    ok = [r for r in rows if r['status'] == 'ok']
    summary = {'windows': len(per_window), 'buildings': len(rows), 'ncc_ok': len([r for r in ok if r['ncc'] >= REG['ncc_min']]),
               'ncc_ok_share': _num(len([r for r in ok if r['ncc'] >= REG['ncc_min']]) / len(rows)) if rows else None,
               'global_shift_m': _spread([pw['global_shift_m'] for pw in per_window]),
               'residual_m_ncc_ok': _spread([r['residual_m'] for r in ok if r['ncc'] >= REG['ncc_min']]),
               'ncc': _spread([r['ncc'] for r in ok]), 'settings': {**REG, 'gsd_m': gsd}}
    if resample:
        same_grid = _spread([pw['ncc_same_grid'] for pw in per_window])
        summary.update({'other_gsd_m': other_gsd, 'scale': _num(gsd / other_gsd, 6), 'resampled_other': True,
                        'ncc_same_grid': same_grid, 'ncc_same_grid_median': same_grid['median']})
    if check_only:
        return {'version': VERSION, 'judge': 'register', 'source': spec['id'], 'other': other['id'], 'check_only': True,
                'summary': summary, 'windows': per_window, 'rows': rows}
    folder = judges_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f'register_{other["id"]}_rows.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    payload = {'version': VERSION, 'judge': 'register', 'source': spec['id'], 'other': other['id'], 'computed_at': common.now_iso(),
               'note': ('image-only registration (phase correlation per window, NCC of blurred gradient magnitude per building); '
                        'masks never enter; residual is the per-building shift against the window shift'),
               'summary': summary, 'windows': per_window}
    if resample:
        payload['resampling'] = (f'{other["id"]} grey area-averaged from {other_gsd} m onto the {gsd} m grid (factor '
                                 f'{other_gsd / gsd:.6f}, cv2 INTER_AREA) before correlation; dx_px / dy_px are {gsd} m pixels; '
                                 'ncc_same_grid is the Pearson correlation of the two greys after the window shift '
                                 '(about 1 when both products come from the same photons)')
    common.write_json_atomic(folder / f'register_{other["id"]}.json', payload)
    return {k: payload[k] for k in ('version', 'judge', 'source', 'other', 'summary')}


def _resample_to_grid(gray, out_h, out_w):
    """Area average of a finer grey window onto the coarser grid (exact area weights, like ht.area_downsample)."""
    import cv2
    return cv2.resize(np.asarray(gray, dtype=np.float32), (int(out_w), int(out_h)), interpolation=cv2.INTER_AREA)


def _same_grid_ncc(gray_a, gray_b, gdx, gdy, margin_px):
    """Pearson correlation of A's grey and B's grey on the same grid after B is moved back by the window shift.

    Computed over the window interior (margin_px plus the shift from every edge); about 1 when the two products share
    photons, lower when they are separate sorties (sun, parallax, moved objects)."""
    import cv2
    h, w = gray_a.shape
    moved = cv2.warpAffine(np.asarray(gray_b, dtype=np.float32), np.float32([[1, 0, gdx], [0, 1, gdy]]), (w, h),
                           flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
    m = int(margin_px + np.ceil(max(abs(gdx), abs(gdy))))
    if h - 2 * m < 8 or w - 2 * m < 8:
        return None
    a = gray_a[m:h - m, m:w - m].astype(np.float32)
    b = moved[m:h - m, m:w - m]
    a = a - float(a.mean(dtype=np.float64))              # a Python float keeps float32 (NEP 50): no 100 MB float64 copies
    b = b - float(b.mean(dtype=np.float64))
    denominator = np.sqrt(float((a * a).sum(dtype=np.float64)) * float((b * b).sum(dtype=np.float64)))
    return float((a * b).sum(dtype=np.float64) / denominator) if denominator > 0 else None


# --- boundary judge J1: does an outline drawn on capture A sit on an edge in capture B's pixels? ---------------------

J1 = {'profile_m': .45, 'step_m': .025, 'peak_tol_m': .10, 'gain': 3., 'angle_tol_deg': 20., 'sample_m': .1,
      'corner_zone_m': .6, 'corner_turn_deg': 30., 'corner_simplify_m': .25, 'buffer_m': 2., 'exg_veg': .06}


def ring_samples(polygon, step_px):
    """(points, outward unit normals) sampled every step_px along the exterior ring."""
    ring = polygon.exterior
    count = max(int(ring.length / step_px), 8)
    points, normals = [], []
    eps = max(step_px * .25, .5)
    for k in range(count):
        d = k / count
        p = ring.interpolate(d, normalized=True)
        before = ring.interpolate((d - eps / ring.length) % 1., normalized=True)
        after = ring.interpolate((d + eps / ring.length) % 1., normalized=True)
        tx, ty = after.x - before.x, after.y - before.y
        norm = np.hypot(tx, ty)
        if norm < 1e-9:
            continue
        nx, ny = ty / norm, -tx / norm
        from shapely.geometry import Point
        if polygon.contains(Point(p.x + nx * eps, p.y + ny * eps)):
            nx, ny = -nx, -ny
        points.append((p.x, p.y))
        normals.append((nx, ny))
    return np.array(points, float), np.array(normals, float)


def corner_points(polygon, gsd):
    simple = polygon.simplify(J1['corner_simplify_m'] / gsd, preserve_topology=True)
    if simple.geom_type != 'Polygon' or simple.is_empty:
        return np.zeros((0, 2))
    turns = ht.turning_angles(simple)
    pts = np.asarray(simple.exterior.coords, float)[:-1]
    return pts[turns >= J1['corner_turn_deg']] if len(pts) == len(turns) else np.zeros((0, 2))


def j1_support(polygon, gx, gy, mag, rgb, gsd, shift_px=(0., 0.), settings=J1):
    """Counts of supported boundary samples of `polygon` (A pixels) against the gradient of image B.

    A sample is supported when the gradient-magnitude profile along the outward normal peaks within peak_tol_m of the
    ring, the peak exceeds gain x the median gradient in a buffer_m band around the building, and the gradient direction
    at the peak is within angle_tol_deg of the normal. Samples whose B pixel is vegetation (ExG > exg_veg) are excluded
    and counted. Corner-zone samples lie within corner_zone_m of a corner of the 0.25 m-simplified ring."""
    from scipy import ndimage
    h, w = mag.shape
    points, normals = ring_samples(polygon, settings['sample_m'] / gsd)
    if not len(points):
        return None
    points = points + np.asarray(shift_px, float)
    offsets = np.arange(-settings['profile_m'], settings['profile_m'] + 1e-9, settings['step_m']) / gsd
    xs = points[:, 0:1] + normals[:, 0:1] * offsets[None, :]
    ys = points[:, 1:2] + normals[:, 1:2] * offsets[None, :]
    inside = (xs >= 0) & (xs <= w - 1) & (ys >= 0) & (ys <= h - 1)
    profiles = ndimage.map_coordinates(mag, [ys.ravel(), xs.ravel()], order=1, mode='nearest').reshape(xs.shape)
    profiles[~inside] = 0.
    # local reference gradient: median magnitude in the building's 2 m buffer (bbox approximation)
    minx, miny, maxx, maxy = polygon.bounds
    pad = settings['buffer_m'] / gsd
    bx0, by0 = int(max(0, minx - pad)), int(max(0, miny - pad))
    bx1, by1 = int(min(w, maxx + pad + 1)), int(min(h, maxy + pad + 1))
    reference = float(np.median(mag[by0:by1, bx0:bx1])) if bx1 > bx0 and by1 > by0 else float(np.median(mag))
    peak_index = profiles.argmax(axis=1)
    peak_t = offsets[peak_index] * gsd
    peak_mag = profiles[np.arange(len(points)), peak_index]
    px = np.clip(np.rint(xs[np.arange(len(points)), peak_index]).astype(int), 0, w - 1)
    py = np.clip(np.rint(ys[np.arange(len(points)), peak_index]).astype(int), 0, h - 1)
    grad_dir = np.stack([gx[py, px], gy[py, px]], axis=1)
    grad_norm = np.hypot(grad_dir[:, 0], grad_dir[:, 1]) + 1e-9
    cosine = np.abs((grad_dir * normals).sum(axis=1)) / grad_norm
    angle_ok = cosine >= np.cos(np.radians(settings['angle_tol_deg']))
    supported = (np.abs(peak_t) <= settings['peak_tol_m']) & (peak_mag >= settings['gain'] * max(reference, 1e-6)) & angle_ok
    # vegetation in B at the sample position
    sx = np.clip(np.rint(points[:, 0]).astype(int), 0, w - 1)
    sy = np.clip(np.rint(points[:, 1]).astype(int), 0, h - 1)
    rgb_f = rgb[sy, sx].astype(np.float32)
    exg = (2 * rgb_f[:, 1] - rgb_f[:, 0] - rgb_f[:, 2]) / (rgb_f.sum(axis=1) + 1.)
    veg = exg > settings['exg_veg']
    corners = corner_points(polygon, gsd)
    if len(corners):
        d = np.sqrt(((points[:, None, :] - (corners[None, :, :] + np.asarray(shift_px))) ** 2).sum(axis=2)).min(axis=1)
        corner_zone = d <= settings['corner_zone_m'] / gsd
    else:
        corner_zone = np.zeros(len(points), bool)
    keep = ~veg
    return {'samples': int(keep.sum()), 'supported': int((supported & keep).sum()), 'veg_excluded': int(veg.sum()),
            'corner_samples': int((corner_zone & keep).sum()), 'corner_supported': int((supported & corner_zone & keep).sum()),
            'peak_offset_m_median': _num(np.median(np.abs(peak_t[keep])), 4) if keep.any() else None}


def cell_split(root, source_id):
    """train / judge (val + test) split of the source's windows, keyed like distill_dataset (shared cell ids)."""
    from .distill_dataset import assign_splits
    ht.set_source(source_id, root)
    crops, _ = ht.load_records(root)
    windows = {w['id']: w for w in crops['windows']}
    splits = assign_splits(sorted(windows), key=lambda wid: windows[wid].get('shared_id') or wid)
    return {wid: ('train' if split == 'train' else 'judge') for wid, split in splits.items()}


def _variant_polygons(polygon, gsd, variants, gx_a=None, gy_a=None, mag_a=None, rgb_a=None):
    """Outline variants of one A instance in native px: raw, offsets, rounding, ungated and gated orthogonalisation."""
    out = {}
    for name in variants:
        if name == 'raw':
            out[name] = polygon
        elif name.startswith('offset'):
            d = float(name.split(':')[1]) / gsd
            candidate = polygon.buffer(d, join_style=2)
            out[name] = candidate if candidate.geom_type == 'Polygon' and not candidate.is_empty else None
        elif name.startswith('round'):
            r = float(name.split(':')[1]) / gsd
            candidate = polygon.buffer(-r).buffer(r)
            out[name] = candidate if candidate.geom_type == 'Polygon' and not candidate.is_empty else None
        elif name == 'ortho':
            out[name] = common.orthogonalise(polygon, gsd)
        elif name == 'ortho_gated':
            out[name] = gated_orthogonalise(polygon, gsd, gx_a, gy_a, mag_a, rgb_a)
    return out


GATE = {'min_support_ratio': .98, 'min_corner_ratio': 1.0}


def gated_orthogonalise(polygon, gsd, gx_a, gy_a, mag_a, rgb_a, gate=GATE):
    """common.orthogonalise accepted only when A's own pixels support the squared outline at least as well as the raw one.

    Polygon-level gate on the same edge-support measure (J1 on capture A): pooled support ratio >= min_support_ratio
    and corner-zone support ratio >= min_corner_ratio, else the raw outline is kept. The verdict on whether this
    helps is J1 on capture B (the gate never sees B)."""
    candidate = common.orthogonalise(polygon, gsd)
    if candidate is None or gx_a is None:
        return None
    raw = j1_support(polygon, gx_a, gy_a, mag_a, rgb_a, gsd)
    new = j1_support(candidate, gx_a, gy_a, mag_a, rgb_a, gsd)
    if not raw or not new or not raw['samples'] or not new['samples']:
        return None
    pooled_ok = (new['supported'] / new['samples']) >= gate['min_support_ratio'] * (raw['supported'] / raw['samples'])
    raw_corner = raw['corner_supported'] / raw['corner_samples'] if raw['corner_samples'] else 0.
    new_corner = new['corner_supported'] / new['corner_samples'] if new['corner_samples'] else 0.
    corner_ok = new_corner >= gate['min_corner_ratio'] * raw_corner
    return candidate if pooled_ok and corner_ok else None


def population(root, spec, record, registration, gsd, size, min_area_m2=REG['min_area_m2'], margin_m=3.):
    """P_linz: A teacher instances matched to an outline (whole), >= min_area_m2, >= margin_m inside the window, NCC ok."""
    kinds = {}
    rows_path = judges_dir(root, spec) / 'outlines_rows.jsonl'
    if rows_path.exists():
        for line in open(rows_path):
            r = json.loads(line)
            if r['window'] == record['window']['id'] and r['arm'] == 'teacher':
                kinds[r['index']] = r['kind']
    margin = margin_m / gsd
    chosen = []
    for row, polygon in teacher_polygons(record):
        if polygon.area * gsd * gsd < min_area_m2 or kinds.get(row['index']) != 'matched':
            continue
        minx, miny, maxx, maxy = polygon.bounds
        if minx < margin or miny < margin or maxx > size - margin or maxy > size - margin:
            continue
        reg = registration.get((record['window']['id'], row['index']))
        if reg is None or reg.get('status') != 'ok' or reg['ncc'] < REG['ncc_min']:
            continue
        chosen.append((row, polygon, (reg['dx_px'], reg['dy_px'])))
    return chosen


def boundary(root, source_id, other_id, variants, cells='judge', limit=None, peak_tol_m=None, prefix='boundary'):
    """J1 of outline variants drawn on A, scored on B's pixels at the registered per-building offset (P_linz).

    When B's pixel differs from A's (the 5 cm capture against the 7.5 cm outlines) every variant polygon and the
    registered shift (A pixels) are scaled into B's pixels by gsd_a / gsd_b, so J1's corner points follow the polygon,
    and J1 runs at B's own pixel size. `prefix` names the outputs: the ladder writes ladder_<tag>, a plain run
    boundary_<tag> (the two share the tag, so they must not share the prefix)."""
    from shapely import affinity
    if peak_tol_m is not None:
        J1['peak_tol_m'] = float(peak_tol_m)
    root, spec, other, crops, other_crops, pairs = twin_records(root, source_id, other_id)
    gsd, size = crops['gsd_m'], crops['native_window_px']
    other_gsd = other_crops['gsd_m']
    scale = gsd / other_gsd if abs(other_gsd - gsd) > 1e-9 else None
    reg_path = judges_dir(root, spec) / f'register_{other["id"]}_rows.jsonl'
    if not reg_path.exists():
        raise SystemExit('run `register` first')
    registration = {}
    for line in open(reg_path):
        r = json.loads(line)
        registration[(r['window'], r['index'])] = r
    split = cell_split(root, source_id)
    rows, per_window = [], []
    for record, twin in pairs:
        wid = record['window']['id']
        if cells != 'all' and split.get(wid) != cells:
            continue
        rgb_a, rgb_b = load_native(root, record), load_native(root, twin)
        gx_a, gy_a, mag_a = sobel(gray_f32(rgb_a))
        gx_b, gy_b, mag_b = sobel(gray_f32(rgb_b))
        members = population(root, spec, record, registration, gsd, size)
        window_rows = []
        for row, polygon, shift in members[:limit or len(members)]:
            polys = _variant_polygons(polygon, gsd, variants, gx_a, gy_a, mag_a, rgb_a)
            entry = {'window': wid, 'index': row['index'], 'area_m2': _num(polygon.area * gsd * gsd, 1), 'variants': {}}
            for name, poly in polys.items():
                if poly is None:
                    entry['variants'][name] = None
                    continue
                if scale:
                    result = j1_support(affinity.scale(poly, xfact=scale, yfact=scale, origin=(0, 0)), gx_b, gy_b, mag_b, rgb_b,
                                        other_gsd, (shift[0] * scale, shift[1] * scale))
                else:
                    result = j1_support(poly, gx_b, gy_b, mag_b, rgb_b, gsd, shift)
                if result:
                    result['iou_raw'] = _num(poly.intersection(polygon).area / max(poly.union(polygon).area, 1e-9))
                    result['area_ratio'] = _num(poly.area / max(polygon.area, 1e-9))
                entry['variants'][name] = result
            window_rows.append(entry)
        rows.extend(window_rows)
        per_window.append({'window': wid, 'split': split.get(wid), 'population': len(members), 'scored': len(window_rows)})
    summary = summarise_boundary(rows, variants)
    folder = judges_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    tag = f'{other["id"]}_{cells}_tol{int(round(J1["peak_tol_m"] * 100)):02d}cm'
    with open(folder / f'{prefix}_{tag}_rows.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    # 'judge' stays 'boundary' for every prefix (v1 payload): ladder runs are told apart by `variants` (LADDER) or by
    # output_prefix 'ladder' (2025-005 on); the 2020 train-cell boundary_* files are ladder runs without that key.
    # A later version: 'judge': prefix (or judge_variant 'ladder'); filter on variants or the file prefix, never .judge.
    payload = {'version': VERSION, 'judge': 'boundary', 'source': spec['id'], 'other': other['id'], 'cells': cells, 'variants': variants,
               'settings': {**J1, 'gate': GATE, 'population': 'P_linz: outline-matched instances >= 20 m2, >= 3 m inside the window, NCC >= 0.5'},
               'computed_at': common.now_iso(),
               'note': ('share of non-vegetation boundary samples whose normal profile peaks on a strong, aligned edge in the OTHER '
                        'capture at the image-registered offset; the outline never saw those pixels; agreement with a second '
                        'capture, not accuracy'),
               'summary': summary, 'windows': per_window}
    if prefix != 'boundary':
        payload['output_prefix'] = prefix
    if scale:
        payload['other_gsd_m'], payload['scale'] = other_gsd, _num(scale, 6)
        payload['scoring_grid'] = (f'variant polygons and the registered shift scaled by {scale:g} into {other["id"]} pixels; '
                                   f'J1 run at {other_gsd} m (profile, tolerance and corner zone in metres)')
    common.write_json_atomic(folder / f'{prefix}_{tag}.json', payload)
    return {k: payload[k] for k in ('version', 'judge', 'source', 'other', 'cells', 'summary')}


def summarise_boundary(rows, variants):
    out = {'buildings': len(rows), 'windows': len({r['window'] for r in rows})}
    for name in variants:
        present = [r for r in rows if r['variants'].get(name)]
        num = {}
        den = {}
        cnum = {}
        cden = {}
        for r in present:
            v = r['variants'][name]
            num[r['window']] = num.get(r['window'], 0) + v['supported']
            den[r['window']] = den.get(r['window'], 0) + v['samples']
            cnum[r['window']] = cnum.get(r['window'], 0) + v['corner_supported']
            cden[r['window']] = cden.get(r['window'], 0) + v['corner_samples']
        wins = sorted(den)
        pooled = sum(num.values()) / sum(den.values()) if sum(den.values()) else None
        corner = sum(cnum.values()) / sum(cden.values()) if sum(cden.values()) else None
        paired = {'higher': 0, 'lower': 0, 'equal': 0}
        paired_corner = {'higher': 0, 'lower': 0, 'equal': 0}
        for r in present:
            raw, v = r['variants'].get('raw'), r['variants'][name]
            if not raw or name == 'raw' or not raw['samples'] or not v['samples']:
                continue
            a, b = v['supported'] / v['samples'], raw['supported'] / raw['samples']
            paired['higher' if a > b + 1e-9 else 'lower' if a < b - 1e-9 else 'equal'] += 1
            if raw['corner_samples'] and v['corner_samples']:
                a, b = v['corner_supported'] / v['corner_samples'], raw['corner_supported'] / raw['corner_samples']
                paired_corner['higher' if a > b + 1e-9 else 'lower' if a < b - 1e-9 else 'equal'] += 1
        out[name] = {'buildings': len(present), 'refused': len(rows) - len(present),
                     'j1_pooled': _num(pooled), 'j1_pooled_ci95_window_bootstrap': window_bootstrap_share([num[w] for w in wins], [den[w] for w in wins]),
                     'j1_corner': _num(corner), 'j1_corner_ci95_window_bootstrap': window_bootstrap_share([cnum[w] for w in wins], [cden[w] for w in wins]),
                     'veg_excluded': int(sum(r['variants'][name]['veg_excluded'] for r in present)),
                     'paired_vs_raw': paired, 'paired_corner_vs_raw': paired_corner,
                     'iou_raw_median': _spread([r['variants'][name].get('iou_raw') for r in present])['median'],
                     'area_ratio_median': _spread([r['variants'][name].get('area_ratio') for r in present])['median']}
    return out


LADDER = ('raw', 'offset:0.075', 'offset:-0.075', 'offset:0.15', 'offset:-0.15', 'round:0.15', 'round:0.30')
BASELINES = ('raw', 'ortho', 'ortho_gated')



# --- per-instance evidence ledger ------------------------------------------------------------------------------------

LEDGER_POLICY = {
    'version': 'ledger-policy-v1',
    'note': ('recommendations only; the export does not change labels until a policy is switched on and judged by a signal '
             'the policy never used'),
    'rules': [
        'keep: outline whole (matched), or cross-capture matched with edge support >= 0.6',
        'part: outline kind part (a piece of a larger mapped building): candidate for merging into its parent',
        'ignore: no outline touched, area < 12 m2, and not matched in the other capture (or no other capture)',
        'review: everything else (unmatched >= 12 m2, overlapping, merged, unjudged with weak edge support)',
    ],
}


def _read_rows(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in open(path)]


def ledger(root, source_id, other_id=None):
    """One row per teacher instance joining every judge available; recommendation per LEDGER_POLICY, labels untouched."""
    root, spec, crops, records = _load(root, source_id)
    gsd, size = crops['gsd_m'], crops['native_window_px']
    folder = judges_dir(root, spec)
    outlines_rows = {(r['window'], r['index']): r for r in _read_rows(folder / 'outlines_rows.jsonl') if r['arm'] == 'teacher'}
    edge_rows = {(r['window'], r['index']): r for r in _read_rows(folder / 'edge_support_rows.jsonl') if r['arm'] == 'teacher'}
    reg_rows, cross_rows, cross_windows = {}, {}, set()
    if other_id:
        reg_rows = {(r['window'], r['index']): r for r in _read_rows(folder / f'register_{other_id}_rows.jsonl')}
        cross_rows = {(r['window'], r['index']): r for r in _read_rows(folder / f'cross_capture_{other_id}_rows.jsonl')}
        cross_meta = folder / f'cross_capture_{other_id}.json'
        if cross_meta.exists():
            cross_windows = {w['window'] for w in common.read_json(cross_meta).get('windows', [])}
    rows, counts = [], {}
    for record in records:
        wid = record['window']['id']
        polys = teacher_polygons(record)
        # nesting: share of each instance's area inside a larger teacher instance of the same window
        from shapely.strtree import STRtree
        geoms = [p for _, p in polys]
        tree = STRtree(geoms) if geoms else None
        for row, polygon in polys:
            key = (wid, row['index'])
            area_m2 = polygon.area * gsd * gsd
            nested = 0.
            if tree is not None:
                for i in tree.query(polygon):
                    other = geoms[i]
                    if other is polygon or other.area <= polygon.area:
                        continue
                    nested = max(nested, polygon.intersection(other).area / max(polygon.area, 1e-9))
            minx, miny, maxx, maxy = polygon.bounds
            cut = minx <= 1 or miny <= 1 or maxx >= size - 1 or maxy >= size - 1
            o = outlines_rows.get(key, {})
            e = edge_rows.get(key, {})
            c = cross_rows.get(key)
            r = reg_rows.get(key, {})
            cross_status = None
            if other_id and wid in cross_windows:
                cross_status = 'matched' if c else 'only_here'
            entry = {'window': wid, 'index': row['index'], 'score': row['score'], 'area_m2': _num(area_m2, 2), 'band': _band(area_m2),
                     'subtiles': len(row.get('subtiles', [])), 'cut_by_window_edge': bool(cut), 'nested_in_larger': _num(nested),
                     'outline_kind': o.get('kind'), 'outline_three_class': o.get('three_class'), 'outline_judged': o.get('judged'),
                     'outline_iou_best': o.get('iou_best'), 'outline_inside_best': o.get('inside_best'),
                     'edge_support': e.get('edge_support'),
                     'registration_ncc': r.get('ncc'), 'registration_status': r.get('status'),
                     'cross_capture': cross_status, 'cross_iou': c['iou'] if c else None,
                     'cross_boundary_distance_m': c['mean_boundary_distance_m'] if c else None}
            entry['recommendation'] = recommend(entry)
            counts[entry['recommendation']] = counts.get(entry['recommendation'], 0) + 1
            rows.append(entry)
    with open(folder / 'ledger_rows.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    n = len(rows)
    by_band = {}
    for name, _, _ in AREA_BANDS:
        band_rows = [r for r in rows if r['band'] == name]
        by_band[name] = {'n': len(band_rows), **{k: _num(sum(r['recommendation'] == k for r in band_rows) / len(band_rows))
                                                 for k in ('keep', 'part', 'ignore', 'review')}} if band_rows else {'n': 0}
    summary = {'instances': n, 'recommendations': counts,
               'shares': {k: _num(v / n) for k, v in counts.items()} if n else {},
               'by_area': by_band,
               'cross_capture_available': bool(cross_windows),
               'cross_matched_share': _num(sum(r['cross_capture'] == 'matched' for r in rows) / max(1, sum(r['cross_capture'] is not None for r in rows)))
               if cross_windows else None,
               'nested_ge_half_share': _num(sum((r['nested_in_larger'] or 0) >= .5 for r in rows) / n) if n else None,
               'cut_by_window_edge_share': _num(sum(r['cut_by_window_edge'] for r in rows) / n) if n else None}
    payload = {'version': VERSION, 'judge': 'ledger', 'source': spec['id'], 'other': other_id, 'policy': LEDGER_POLICY,
               'computed_at': common.now_iso(), 'summary': summary}
    common.write_json_atomic(folder / 'ledger.json', payload)
    return {k: payload[k] for k in ('version', 'judge', 'source', 'other', 'summary')}


def recommend(entry, min_edge=.6, small_m2=12.):
    kind = entry.get('outline_kind')
    cross = entry.get('cross_capture')
    edge = entry.get('edge_support') or 0.
    if kind == 'matched' or (cross == 'matched' and edge >= min_edge):
        return 'keep'
    if kind == 'part':
        return 'part'
    if kind == 'unmatched' and (entry.get('area_m2') or 0.) < small_m2 and cross != 'matched':
        return 'ignore'
    return 'review'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('judge', choices=('outlines', 'edge-support', 'cross-capture', 'register', 'boundary', 'ladder', 'ledger',
                                          'corners'))
    parser.add_argument('--source', default='linz-chc-2025-0075')
    parser.add_argument('--other', help='second capture source for cross-capture, register, boundary, ladder, ledger, corners')
    parser.add_argument('--root', default='.')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--cells', choices=('train', 'judge', 'all'), default=None)
    parser.add_argument('--variants', help='comma-separated outline variants for boundary (default raw,ortho,ortho_gated)')
    parser.add_argument('--peak-tol-m', type=float, default=None, help='J1 peak tolerance in metres (default 0.10)')
    parser.add_argument('--null-shift-m', type=float, default=0., help='corners: add this many metres to the registered shift '
                                                                          '(null control; outputs get a _null<m>m suffix)')
    parser.add_argument('--check-only', action='store_true', help='register: compute the rows in memory, print {rows, summary}, '
                                                                   'write nothing')
    args = parser.parse_args(argv)
    if args.check_only and args.judge != 'register':
        raise SystemExit('--check-only applies to register only (every other judge writes its outputs)')
    if args.null_shift_m and args.judge != 'corners':
        raise SystemExit('--null-shift-m applies to corners only')
    if args.judge == 'outlines':
        result = outlines_judge(args.root, args.source, args.limit)
    elif args.judge == 'edge-support':
        result = edge_support_judge(args.root, args.source, args.limit)
    elif args.judge == 'ledger':
        result = ledger(args.root, args.source, args.other)
    else:
        if not args.other:
            raise SystemExit(f'{args.judge} needs --other SOURCE')
        if args.judge == 'cross-capture':
            result = cross_capture_judge(args.root, args.source, args.other, args.limit)
        elif args.judge == 'register':
            result = register(args.root, args.source, args.other, args.limit, check_only=args.check_only)
        elif args.judge == 'ladder':
            result = boundary(args.root, args.source, args.other, list(LADDER), args.cells or 'train', args.limit, args.peak_tol_m,
                              prefix='ladder')
        elif args.judge == 'corners':
            from . import teacher_corners
            result = teacher_corners.corners_judge(args.root, args.source, args.other, args.cells or 'all', args.limit,
                                                   null_shift_m=args.null_shift_m)
        else:
            variants = args.variants.split(',') if args.variants else list(BASELINES)
            result = boundary(args.root, args.source, args.other, variants, args.cells or 'judge', args.limit, args.peak_tol_m)
    json.dump(result, sys.stdout, indent=1, sort_keys=True, default=str)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
