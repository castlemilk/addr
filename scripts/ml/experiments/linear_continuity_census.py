"""Linear-class fragmentation census (road, footpath, driveway), 2026-10-03. Read-only over data/scenes.

Reviewer direction (verbatim, relayed by the harness 2026-10-03 13:55 AEST): "the modelling needs to be better at
maintaining continuous road/pathway/driveways rather than the little fragments."

This module measures how fragmented the three linear classes are in the scenes as they stand. It is a census, not an
experiment: nothing is re-inferred, no model is loaded, nothing under data/scenes is written, and every figure is a
count or a geometry (agreement between layers is never accuracy). It imports shapely, numpy and json only; nothing from
scene_extract (which pulls torch/onnx).

How a linear piece is produced today (read from scripts/ml/scene_extract.py and scripts/ml/sam3.py, not changed here):
  the whole frame is resized to the 1008 px SAM3 input (no tiling), each prompt ('road', 'sidewalk', 'driveway') yields up
  to 80 instance proposals with score >= .45, each proposal mask is binarised at logit > 0 and resized back to the frame,
  mask_to_polygons(min_area_px=12, simplify_px=.9) vectorises it and EVERY disconnected component of one proposal mask
  becomes its own object (same score, same prompt); then a duplicate rule drops a candidate whose intersection with a
  retained same-kind object exceeds .8 of the smaller area. There is no same-kind closure or distance merge for linear
  classes (merge_canopy_fragments only folds sub-1 m2 tree/vegetation pieces into a 10x larger container).

Run from the repo root:
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_continuity_census
Outputs (only here): data/reviews/eyes/experiments/linear-continuity-2026-10-03/{census.json,census.md,pieces.jsonl}

Definitions (all distances in metres through the scene's pixel_to_meters affine unless marked px):
  piece            one object of kind road|footpath|driveway. Model layer = latest.json objects (status != superseded).
                   Review layer = review.json objects that are live (status not rejected/superseded); rejected pieces are
                   counted in the status table but excluded from the geometry.
  width / length   short / long side of the metric minimum_rotated_rectangle; aspect = length / width.
  feature(d)       a connected component of unary_union(piece.buffer(d)) over the same class in one scene,
                   d in {0, .5, 1, 2} m; pieces per feature = pieces / features(d). At d = 0 only touching or overlapping
                   pieces join.
  gap              distance from a piece to its nearest same-class piece in the same scene and layer (None if alone).
  touches frame    the pixel polygon is within 1 px of the frame boundary.
  same-score group pieces of one class sharing an identical 4-dp detector score: components of one proposal mask
                   (scores are rounded to 4 dp, so an accidental tie between two proposals is possible but rare).
  confirmed-adjacent  two confirmed pieces of one class within one pixel of each other: the reviewer accepted
                   fragments as they were rather than merging them.
  tile alignment   share of exterior boundary length (px) within 2 px of an interior multiple of 512 (x or y), versus the
                   random expectation 4 px x lines / extent per axis; frame-edge share reported separately. 512 px frames
                   have no interior 512 line, so the interior share is None for them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from shapely import make_valid
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

VERSION = 'linear-continuity-census-v1'
ROOT = Path(__file__).resolve().parents[3]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
OUT = ROOT/'data/reviews/eyes/experiments/linear-continuity-2026-10-03'
SCENES = ROOT/'data/scenes'
DIRECTION = 'the modelling needs to be better at maintaining continuous road/pathway/driveways rather than the little fragments.'
LINEAR = ('road', 'footpath', 'driveway')
BUFFERS = (0.0, 0.5, 1.0, 2.0)
FRAME_TOL_PX = 1.0
TILE_PX = 512
TILE_TOL_PX = 2.0
WINDOW_PREFIXES = ('win-', 'lot-')
REVIEW_SET_PREFIXES = ('house-', 'roof_training_250-')
LIVE_EXCLUDED = ('rejected', 'superseded')
SMALL_M2 = (0.5, 1.0, 2.0)


def load_json(path):
    with open(path) as handle:
        return json.load(handle)


def population(root=SCENES):
    """(scene_dir, group) for the 53 windows and every review-set scene with a review.json; sorted, read-only."""
    rows = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not (path/'latest.json').exists():
            continue
        if path.name.startswith(WINDOW_PREFIXES):
            rows.append((path, 'windows'))
        elif path.name.startswith(REVIEW_SET_PREFIXES) and (path/'review.json').exists():
            rows.append((path, 'review_set'))
    return rows


def affine_params(pixel_to_meters):
    m = np.asarray(pixel_to_meters, dtype=float)      # 3x2: [x y 1] @ m = [X Y]
    return [m[0, 0], m[1, 0], m[0, 1], m[1, 1], m[2, 0], m[2, 1]]


def polygonal(geometry):
    """The polygonal part of a geometry (make_valid can return collections); None when empty."""
    if geometry.is_empty:
        return None
    if geometry.geom_type == 'Polygon':
        return geometry
    parts = [g for g in getattr(geometry, 'geoms', []) if g.geom_type in ('Polygon', 'MultiPolygon') and not g.is_empty]
    if not parts:
        return None
    merged = unary_union(parts)
    return merged if not merged.is_empty else None


def piece_polygon(obj):
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return None
    try:
        polygon = Polygon(ring, [h for h in (obj.get('holes') or []) if len(h) >= 3])
    except (ValueError, TypeError):
        return None
    if not polygon.is_valid:
        polygon = polygonal(make_valid(polygon))
    return polygon if polygon is not None and polygon.area > 0 else None


def rotated_rectangle_sides(metric):
    with warnings.catch_warnings():              # shapely warns on degenerate envelopes; the sides below stay finite
        warnings.simplefilter('ignore', RuntimeWarning)
        corners = list(metric.minimum_rotated_rectangle.exterior.coords)
    if len(corners) < 5:                       # degenerate (a line): width 0
        length = metric.length/2 if metric.geom_type != 'Polygon' else metric.exterior.length/2
        return 0.0, float(length)
    sides = [math.dist(corners[i], corners[i+1]) for i in range(4)]
    return float(min(sides)), float(max(sides))


def component_count(geometry):
    if geometry.is_empty:
        return 0
    return len(geometry.geoms) if geometry.geom_type.startswith('Multi') or geometry.geom_type == 'GeometryCollection' else 1


def features_at(metric_polygons, d):
    if not metric_polygons:
        return 0
    shapes = metric_polygons if d == 0 else [p.buffer(d) for p in metric_polygons]
    return component_count(unary_union(shapes))


def percentile(values, q):
    values = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return round(float(np.percentile(values, q)), 3) if values else None


def median(values):
    return percentile(values, 50)


def frame_touch(pixel_polygon, width, height, tol=FRAME_TOL_PX):
    return bool(box(0, 0, width, height).exterior.distance(pixel_polygon) <= tol)


def tile_bands(width, height, tile=TILE_PX, tol=TILE_TOL_PX):
    """(interior band polygon or None, frame-edge band polygon, number of interior lines per axis)."""
    xs = [x for x in range(tile, width, tile)]
    ys = [y for y in range(tile, height, tile)]
    interior = [box(x-tol, -tol, x+tol, height+tol) for x in xs] + [box(-tol, y-tol, width+tol, y+tol) for y in ys]
    frame = box(0, 0, width, height).exterior.buffer(tol)
    return (unary_union(interior) if interior else None), frame, (len(xs), len(ys))


def boundary_alignment(pixel_polygons, width, height):
    """Share of exterior boundary length within the interior 512 px bands and within the frame-edge band."""
    interior, frame, (nx, ny) = tile_bands(width, height)
    total = sum(p.exterior.length for p in pixel_polygons)
    if total <= 0:
        return {'boundary_px': 0.0, 'interior_512_share': None, 'frame_edge_share': None, 'random_expectation_interior': None}
    boundaries = [LineString(p.exterior.coords) for p in pixel_polygons]
    inside = sum(b.intersection(interior).length for b in boundaries) if interior is not None else None
    edge = sum(b.intersection(frame).length for b in boundaries)
    expectation = (nx*2*TILE_TOL_PX/width + ny*2*TILE_TOL_PX/height) if interior is not None else None
    return {'boundary_px': round(total, 1), 'interior_512_lines': [nx, ny],
            'interior_512_share': None if inside is None else round(inside/total, 4),
            'random_expectation_interior': None if expectation is None else round(expectation, 4),
            'frame_edge_share': round(edge/total, 4)}


def analyse_pieces(objects, scene, layer):
    """Per-piece rows for the linear classes of one layer; geometry in metres, frame test in pixels."""
    params = affine_params(scene['pixel_to_meters'])
    width, height = scene['image_width'], scene['image_height']
    rows, invalid = [], Counter()
    for obj in objects:
        kind = obj.get('kind')
        if kind not in LINEAR or obj.get('status') == 'superseded':
            continue
        pixel = piece_polygon(obj)
        if pixel is None:
            invalid[kind] += 1
            continue
        metric = polygonal(make_valid(affine_transform(pixel, params)))
        if metric is None:
            invalid[kind] += 1
            continue
        short, long = rotated_rectangle_sides(metric)
        rows.append({'scene': scene['id'], 'layer': layer, 'id': obj['id'], 'kind': kind, 'status': obj.get('status'),
                     'source': obj.get('source'), 'score': obj.get('score'), 'prompt': obj.get('prompt'),
                     'live': obj.get('status') not in LIVE_EXCLUDED,
                     'area_m2': round(metric.area, 3), 'width_m': round(short, 3), 'length_m': round(long, 3),
                     'aspect': round(long/short, 2) if short > 0 else None,
                     'perimeter_m': round(metric.length, 2), 'vertices': len(pixel.exterior.coords)-1, 'holes': len(pixel.interiors),
                     'touches_frame': frame_touch(pixel, width, height),
                     'replaces': len(obj.get('replaces') or []), 'clipped_by_frame': bool(obj.get('clipped_by_frame')),
                     'geometry_reviewed': bool(obj.get('geometry_reviewed')),
                     '_pixel': pixel, '_metric': metric})
    return rows, invalid


def class_summary(rows, px_m, width, height):
    """Fragmentation measures for the pieces of one class in one scene/layer (rows already filtered to live pieces)."""
    metric = [r['_metric'] for r in rows]
    n = len(rows)
    gaps = []
    for i, a in enumerate(metric):
        others = [a.distance(b) for j, b in enumerate(metric) if j != i]
        gaps.append(min(others) if others else None)
    for r, g in zip(rows, gaps, strict=True):
        r['gap_m'] = None if g is None else round(g, 3)
    features = {str(d): features_at(metric, d) for d in BUFFERS}
    present = [g for g in gaps if g is not None]
    scores = Counter(r['score'] for r in rows if r['score'] is not None)
    same_score_groups = [c for c in scores.values() if c >= 2]
    # Components of one proposal mask: how far apart do they sit, and at which buffer would they rejoin?
    group_gaps, group_features = [], {str(d): [0, 0] for d in BUFFERS}     # [groups joined into one feature, groups]
    for score, count in scores.items():
        if count < 2:
            continue
        members = [r['_metric'] for r in rows if r['score'] == score]
        for r in rows:
            if r['score'] == score:
                r['same_score_group_size'] = count
        for i, a in enumerate(members):
            group_gaps.append(min(a.distance(b) for j, b in enumerate(members) if j != i))
        for d in BUFFERS:
            group_features[str(d)][1] += 1
            group_features[str(d)][0] += int(features_at(members, d) == 1)
    return {'pieces': n, 'total_area_m2': round(sum(r['area_m2'] for r in rows), 2),
            'same_score_gap_median_m': median(group_gaps), 'same_score_gap_p90_m': percentile(group_gaps, 90),
            'same_score_groups_one_feature_at_d': {d: v[0] for d, v in group_features.items()},
            'median_area_m2': median([r['area_m2'] for r in rows]), 'median_width_m': median([r['width_m'] for r in rows]),
            'median_length_m': median([r['length_m'] for r in rows]), 'median_aspect': median([r['aspect'] for r in rows]),
            'features': features, 'pieces_per_feature': {d: (round(n/f, 3) if f else None) for d, f in features.items()},
            'gap_median_m': median(present), 'gap_p90_m': percentile(present, 90),
            'pieces_touching_other_piece': sum(1 for g in present if g <= px_m),
            'pieces_touching_frame': sum(1 for r in rows if r['touches_frame']),
            'pieces_under_m2': {str(t): sum(1 for r in rows if r['area_m2'] < t) for t in SMALL_M2},
            'same_score_groups': len(same_score_groups), 'pieces_in_same_score_groups': sum(same_score_groups),
            'alignment': boundary_alignment([r['_pixel'] for r in rows], width, height)}


def review_summary(all_rows, px_m):
    """Status table, reviewer-source pieces and confirmed-adjacent pairs for the linear pieces of one class."""
    statuses = Counter(r['status'] for r in all_rows)
    confirmed = [r for r in all_rows if r['status'] == 'confirmed']
    pairs, involved = 0, set()
    for i, a in enumerate(confirmed):
        for b in confirmed[i+1:]:
            if a['_metric'].distance(b['_metric']) <= px_m:
                pairs += 1
                involved.update((a['id'], b['id']))
    within_1m = 0
    for a in confirmed:
        if any(a is not b and a['_metric'].distance(b['_metric']) <= 1.0 for b in confirmed):
            within_1m += 1
    return {'statuses': dict(sorted(statuses.items())),
            'source_reviewer': sum(1 for r in all_rows if r['source'] == 'reviewer'),
            'sources': dict(Counter(r['source'] for r in all_rows)),
            'geometry_reviewed': sum(1 for r in all_rows if r['geometry_reviewed']),
            'with_replaces': sum(1 for r in all_rows if r['replaces']),
            'clipped_by_frame': sum(1 for r in all_rows if r['clipped_by_frame']),
            'confirmed': len(confirmed), 'confirmed_adjacent_pairs': pairs, 'confirmed_pieces_in_adjacent_pairs': len(involved),
            'confirmed_pieces_within_1m_of_confirmed': within_1m,
            'confirmed_under_1m2': sum(1 for r in confirmed if r['area_m2'] < 1.0),
            'confirmed_median_area_m2': median([r['area_m2'] for r in confirmed])}


def census_scene(path, group):
    scene = load_json(path/'latest.json')
    px_m = float(math.sqrt(abs(np.linalg.det(np.asarray(scene['pixel_to_meters'], dtype=float)[:2]))))
    width, height = scene['image_width'], scene['image_height']
    layers = {'model': scene.get('objects', [])}
    review = None
    if (path/'review.json').exists():
        review = load_json(path/'review.json')
        layers['review'] = review.get('objects', [])
    out = {'scene': scene['id'], 'group': group, 'image_width': width, 'image_height': height, 'pixel_m': round(px_m, 4),
           'review_revision': review.get('revision') if review else None, 'review_updated_at': review.get('updated_at') if review else None,
           'review_signature_matches_latest': (review.get('model_signature') == scene.get('model_signature')) if review else None,
           'layers': {}}
    pieces = []
    for layer, objects in layers.items():
        rows, invalid = analyse_pieces(objects, scene, layer)
        pieces.extend(rows)
        per_class = {}
        for kind in LINEAR:
            kind_rows = [r for r in rows if r['kind'] == kind]
            live = [r for r in kind_rows if r['live']]
            entry = class_summary(live, px_m, width, height) if live else {'pieces': 0}
            if layer == 'review':
                entry['review'] = review_summary(kind_rows, px_m)
            entry['invalid_rings'] = invalid.get(kind, 0)
            per_class[kind] = entry
        out['layers'][layer] = per_class
    return out, pieces


def aggregate(scene_rows, pieces):
    """Pooled per group x layer x class: counts summed, piece-level medians pooled over all pieces."""
    agg = {}
    by_key = defaultdict(list)
    for p in pieces:
        if p['live']:
            by_key[(next(s['group'] for s in scene_rows if s['scene'] == p['scene']), p['layer'], p['kind'])].append(p)
    for group in ('windows', 'review_set'):
        agg[group] = {}
        for layer in ('model', 'review'):
            agg[group][layer] = {}
            for kind in LINEAR:
                entries = [s['layers'][layer][kind] for s in scene_rows if s['group'] == group and layer in s['layers']]
                present = [e for e in entries if e.get('pieces')]
                rows = by_key.get((group, layer, kind), [])
                features = {str(d): sum(e['features'][str(d)] for e in present) for d in BUFFERS}
                n = len(rows)
                gaps = [r['gap_m'] for r in rows if r.get('gap_m') is not None]
                entry = {'scenes': len(entries), 'scenes_with_class': len(present), 'pieces': n,
                         'total_area_m2': round(sum(r['area_m2'] for r in rows), 1),
                         'median_area_m2': median([r['area_m2'] for r in rows]), 'p90_area_m2': percentile([r['area_m2'] for r in rows], 90),
                         'median_width_m': median([r['width_m'] for r in rows]), 'median_length_m': median([r['length_m'] for r in rows]),
                         'median_aspect': median([r['aspect'] for r in rows]),
                         'features': features, 'pieces_per_feature': {d: (round(n/f, 3) if f else None) for d, f in features.items()},
                         'gap_median_m': median(gaps), 'gap_p90_m': percentile(gaps, 90), 'pieces_with_neighbour': len(gaps),
                         'pieces_touching_other_piece': sum(e.get('pieces_touching_other_piece', 0) for e in present),
                         'pieces_touching_frame': sum(1 for r in rows if r['touches_frame']),
                         'pieces_under_m2': {str(t): sum(1 for r in rows if r['area_m2'] < t) for t in SMALL_M2},
                         'same_score_groups': sum(e.get('same_score_groups', 0) for e in present),
                         'pieces_in_same_score_groups': sum(e.get('pieces_in_same_score_groups', 0) for e in present),
                         'same_score_gap_median_m': median([r['gap_m'] for r in rows if r.get('same_score_group_size')]),
                         'same_score_gap_p90_m': percentile([r['gap_m'] for r in rows if r.get('same_score_group_size')], 90),
                         'same_score_groups_one_feature_at_d': {str(d): sum(e.get('same_score_groups_one_feature_at_d', {}).get(str(d), 0) for e in present) for d in BUFFERS},
                         'scene_ppf_d1_median': median([e['pieces_per_feature']['1.0'] for e in present if e['pieces_per_feature'].get('1.0')]),
                         'scenes_with_ppf_d1_over_1': sum(1 for e in present if (e['pieces_per_feature'].get('1.0') or 0) > 1)}
                if layer == 'review':
                    reviews = [e['review'] for e in entries if 'review' in e]
                    statuses = Counter()
                    for r in reviews:
                        statuses.update(r['statuses'])
                    entry['review'] = {'statuses': dict(sorted(statuses.items())),
                                       'source_reviewer': sum(r['source_reviewer'] for r in reviews),
                                       'geometry_reviewed': sum(r['geometry_reviewed'] for r in reviews),
                                       'with_replaces': sum(r['with_replaces'] for r in reviews),
                                       'clipped_by_frame': sum(r['clipped_by_frame'] for r in reviews),
                                       'confirmed': sum(r['confirmed'] for r in reviews),
                                       'confirmed_adjacent_pairs': sum(r['confirmed_adjacent_pairs'] for r in reviews),
                                       'confirmed_pieces_in_adjacent_pairs': sum(r['confirmed_pieces_in_adjacent_pairs'] for r in reviews),
                                       'confirmed_pieces_within_1m_of_confirmed': sum(r['confirmed_pieces_within_1m_of_confirmed'] for r in reviews),
                                       'confirmed_under_1m2': sum(r['confirmed_under_1m2'] for r in reviews),
                                       'scenes_with_confirmed_adjacent': sum(1 for r in reviews if r['confirmed_adjacent_pairs'])}
                agg[group][layer][kind] = entry
    return agg


def fragmentation_score(scene_row, layer='model'):
    """Area-weighted pieces per feature at d = 1.0 across the three classes; None when the layer has no linear piece."""
    total, weighted = 0.0, 0.0
    for kind in LINEAR:
        e = scene_row['layers'].get(layer, {}).get(kind, {})
        if e.get('pieces'):
            ppf = e['pieces_per_feature']['1.0']
            total += e['total_area_m2']
            weighted += e['total_area_m2']*ppf
    return round(weighted/total, 3) if total else None


def rank_windows(scene_rows, layer='model'):
    ranked = []
    for s in scene_rows:
        if s['group'] != 'windows':
            continue
        score = fragmentation_score(s, layer)
        if score is None:
            continue
        detail = {k: {'pieces': s['layers'][layer][k]['pieces'], 'features_d1': s['layers'][layer][k]['features']['1.0'],
                      'area_m2': s['layers'][layer][k]['total_area_m2']}
                  for k in LINEAR if s['layers'][layer][k].get('pieces')}
        ranked.append({'scene': s['scene'], 'score_weighted_ppf_d1': score, 'size_px': s['image_width'],
                       'linear_pieces': sum(d['pieces'] for d in detail.values()),
                       'linear_features_d1': sum(d['features_d1'] for d in detail.values()),
                       'linear_area_m2': round(sum(d['area_m2'] for d in detail.values()), 1), 'classes': detail})
    ranked.sort(key=lambda r: (-r['score_weighted_ppf_d1'], -r['linear_pieces'], r['scene']))
    return ranked


def top_fragmented_per_class(scene_rows, layer='model', n=3):
    out = {}
    for kind in LINEAR:
        rows = [(s['layers'][layer][kind]['pieces_per_feature']['1.0'], s['layers'][layer][kind]['pieces'], s)
                for s in scene_rows if s['group'] == 'windows' and s['layers'][layer][kind].get('pieces', 0) >= 2]
        rows.sort(key=lambda t: (-t[0], -t[1], t[2]['scene']))
        out[kind] = [{'scene': s['scene'], 'size_px': s['image_width'], 'pieces': pieces, 'features_d1': s['layers'][layer][kind]['features']['1.0'],
                      'pieces_per_feature_d1': ppf, 'alignment': s['layers'][layer][kind]['alignment']} for ppf, pieces, s in rows[:n]]
    return out


def pooled_alignment(pieces, scene_rows):
    """Boundary-length-weighted interior-512 and frame-edge shares over all 1024 px windows (model layer), per class."""
    sizes = {s['scene']: (s['image_width'], s['image_height']) for s in scene_rows if s['group'] == 'windows'}
    out = {}
    for kind in LINEAR:
        rows = [p for p in pieces if p['layer'] == 'model' and p['kind'] == kind and p['scene'] in sizes and sizes[p['scene']][0] >= 2*TILE_PX]
        if not rows:
            out[kind] = None
            continue
        total = inside = edge = 0.0
        for p in rows:
            width, height = sizes[p['scene']]
            interior, frame, _ = tile_bands(width, height)
            boundary = LineString(p['_pixel'].exterior.coords)
            total += boundary.length
            inside += boundary.intersection(interior).length
            edge += boundary.intersection(frame).length
        out[kind] = {'windows_1024': len({p['scene'] for p in rows}), 'pieces': len(rows), 'boundary_px': round(total, 1),
                     'interior_512_share': round(inside/total, 4), 'random_expectation_interior': round(2*2*TILE_TOL_PX/1024, 4),
                     'frame_edge_share': round(edge/total, 4)}
    return out


def fmt(value, digits=2):
    if value is None:
        return '-'
    if isinstance(value, float):
        return f'{value:.{digits}f}'
    return str(value)


def write_markdown(report, path):
    lines = [f"# Linear-class fragmentation census (road, footpath, driveway) - {report['computed_at'][:10]}", '',
             f"Reviewer direction (verbatim): \"{DIRECTION}\"", '',
             'Read-only census over data/scenes; every figure is a count or a geometry, never accuracy; nothing promoted. '
             f"Code `scripts/ml/experiments/linear_continuity_census.py` ({report['version']}, sha256 {report['code_sha256'][:12]}).", '',
             '## Population', '']
    pop = report['population']
    lines += [f"- Windows (lot-*, win-*): {pop['windows']} scenes, all with review.json; sizes {pop['window_sizes']}.",
              f"- Review set (house-*, roof_training_250-*) with a review.json: {pop['review_set']} scenes ({pop['review_set_breakdown']}).",
              f"- Linear pieces read: model layer {pop['pieces_model']}, review layer {pop['pieces_review']} "
              f"(live {pop['pieces_review_live']}); invalid rings skipped: {pop['invalid_rings']}.", '',
              '## How pieces arise (read from the code, unchanged)', '',
              'Whole frame resized to 1008 px for SAM3 (no tiling); per prompt up to 80 proposals with score >= .45; each proposal mask '
              'binarised at logit > 0; `mask_to_polygons(min_area_px=12, simplify_px=.9)`; every disconnected component of one proposal '
              'mask becomes its own object (same score); duplicate rule drops a candidate covering > .8 of the smaller same-kind shape. '
              'No same-kind closure or distance merge exists for linear classes (`merge_canopy_fragments` is tree/vegetation only, < 1 m2 into a 10x container).', '',
              '## Per-class aggregate (pooled over scenes; features summed per scene then pooled)', '',
              '| group | layer | class | scenes w/ class | pieces | feat touching | feat gap<=1m | feat gap<=2m | feat gap<=4m | pcs/feat gap<=2m | pcs/feat gap<=4m | med area m2 | med width m | med length m | med gap m | p90 gap m | touching other | touching frame | < 1 m2 | same-score pcs |',
              '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for group in ('windows', 'review_set'):
        for layer in ('model', 'review'):
            for kind in LINEAR:
                e = report['aggregates'][group][layer][kind]
                if not e['pieces']:
                    lines.append(f'| {group} | {layer} | {kind} | {e["scenes_with_class"]} | 0 | | | | | | | | | | | | | | | |')
                    continue
                f = e['features']
                lines.append(f"| {group} | {layer} | {kind} | {e['scenes_with_class']}/{e['scenes']} | {e['pieces']} | {f['0.0']} | {f['0.5']} | {f['1.0']} | {f['2.0']} | "
                             f"{fmt(e['pieces_per_feature']['1.0'])} | {fmt(e['pieces_per_feature']['2.0'])} | {fmt(e['median_area_m2'])} | {fmt(e['median_width_m'])} | "
                             f"{fmt(e['median_length_m'])} | {fmt(e['gap_median_m'])} | {fmt(e['gap_p90_m'])} | {e['pieces_touching_other_piece']} | {e['pieces_touching_frame']} | "
                             f"{e['pieces_under_m2']['1.0']} | {e['pieces_in_same_score_groups']} ({e['same_score_groups']} groups) |")
    lines += ['', 'pcs/feat = pieces per connected feature after buffer(d) + unary_union, which joins pieces up to 2d apart: "gap<=1m" / "gap<=2m" / "gap<=4m" '
              'are d = 0.5 / 1 / 2 m (the json keys stay "0.5" / "1.0" / "2.0"; before 2026-10-03 PM these columns were labelled d0.5 / d1 / d2, which read as a 1 m gap) '
              'and "feat touching" is d = 0; "touching other" = nearest same-class piece within one pixel; '
              '"same-score pcs" = pieces sharing a 4-dp score with another piece of the class in the scene (components of one proposal mask).', '',
              '### Components of one proposal mask (same-score groups, model layer)', '',
              '| group | class | groups | pieces in groups | share of pieces | gap to nearest sibling: median m | p90 m | groups rejoined into one feature at gap<=1m | gap<=2m | gap<=4m |',
              '|---|---|---|---|---|---|---|---|---|---|']
    for group in ('windows', 'review_set'):
        for kind in LINEAR:
            e = report['aggregates'][group]['model'][kind]
            if not e['pieces']:
                continue
            j = e['same_score_groups_one_feature_at_d']
            lines.append(f"| {group} | {kind} | {e['same_score_groups']} | {e['pieces_in_same_score_groups']} | {fmt(e['pieces_in_same_score_groups']/e['pieces'])} | "
                         f"{fmt(e['same_score_gap_median_m'])} | {fmt(e['same_score_gap_p90_m'])} | {j['0.5']} | {j['1.0']} | {j['2.0']} |")
    lines += ['', 'Gap here is to the nearest same-class piece of any score (the sibling is usually, not always, the nearest). '
              '"Rejoined" counts groups whose members form one connected feature after buffer(d) + union.', '',
              '## Review layer: statuses and whether the reviewer accepted adjacent fragments', '',
              '| group | class | candidate | confirmed | needs_adjustment | rejected | source=reviewer | geometry_reviewed | replaces | clipped_by_frame | confirmed-adjacent pairs (pieces) | confirmed within 1 m of confirmed | confirmed < 1 m2 |',
              '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for group in ('windows', 'review_set'):
        for kind in LINEAR:
            r = report['aggregates'][group]['review'][kind].get('review')
            if not r:
                continue
            s = r['statuses']
            lines.append(f"| {group} | {kind} | {s.get('candidate', 0)} | {s.get('confirmed', 0)} | {s.get('needs_adjustment', 0)} | {s.get('rejected', 0)} | "
                         f"{r['source_reviewer']} | {r['geometry_reviewed']} | {r['with_replaces']} | {r['clipped_by_frame']} | "
                         f"{r['confirmed_adjacent_pairs']} ({r['confirmed_pieces_in_adjacent_pairs']}) in {r['scenes_with_confirmed_adjacent']} scenes | "
                         f"{r['confirmed_pieces_within_1m_of_confirmed']} | {r['confirmed_under_1m2']} |")
    lines += ['', '## The 8 most fragmented windows (model layer; area-weighted pieces per feature at gap <= 2 m (d = 1.0) across the three classes)', '',
              '| rank | window | px | score | linear pieces | features gap<=2m | linear area m2 | road pcs/feat | footpath pcs/feat | driveway pcs/feat |', '|---|---|---|---|---|---|---|---|---|---|']
    for i, w in enumerate(report['most_fragmented_windows'], 1):
        cells = []
        for kind in LINEAR:
            d = w['classes'].get(kind)
            cells.append(f"{d['pieces']}/{d['features_d1']}" if d else '-')
        lines.append(f"| {i} | {w['scene']} | {w['size_px']} | {fmt(w['score_weighted_ppf_d1'])} | {w['linear_pieces']} | {w['linear_features_d1']} | {fmt(w['linear_area_m2'], 1)} | {' | '.join(cells)} |")
    lines += ['', f"Windows ranked: {report['windows_ranked']} of {pop['windows']} have at least one linear piece in the model layer.", '',
              '## Tile alignment (does fragmentation follow 512 px lines?)', '',
              'Share of exterior boundary length within 2 px of an interior multiple of 512 px (only 1024 px frames have one: x = 512, y = 512); '
              'random expectation for a boundary spread over the frame = 2 lines x 4 px / 1024 = 0.0078. Frame-edge share (within 2 px of the frame) shown separately.', '',
              '| class | window | px | pieces | pcs/feat gap<=2m | boundary px | interior-512 share | random | frame-edge share |', '|---|---|---|---|---|---|---|---|---|']
    for kind in LINEAR:
        for w in report['tile_alignment']['top3_per_class'][kind]:
            a = w['alignment']
            lines.append(f"| {kind} | {w['scene']} | {w['size_px']} | {w['pieces']} | {fmt(w['pieces_per_feature_d1'])} | {fmt(a['boundary_px'], 0)} | "
                         f"{fmt(a['interior_512_share'], 4)} | {fmt(a['random_expectation_interior'], 4)} | {fmt(a['frame_edge_share'], 4)} |")
    lines += ['', '| class | pooled over 1024 px windows | pieces | boundary px | interior-512 share | random | frame-edge share |', '|---|---|---|---|---|---|---|']
    for kind in LINEAR:
        a = report['tile_alignment']['pooled_1024_windows'][kind]
        if a:
            lines.append(f"| {kind} | {a['windows_1024']} windows | {a['pieces']} | {fmt(a['boundary_px'], 0)} | {fmt(a['interior_512_share'], 4)} | {fmt(a['random_expectation_interior'], 4)} | {fmt(a['frame_edge_share'], 4)} |")
    w = report['reviewer_window']
    lines += ['', f"## The reviewer's window {w['scene']} (review.json revision {w['review_revision']})", '',
              '| layer | class | pieces | feat touching | feat gap<=2m | feat gap<=4m | med area m2 | med width m | statuses | confirmed-adjacent pairs |', '|---|---|---|---|---|---|---|---|---|---|']
    for layer in ('model', 'review'):
        for kind in LINEAR:
            e = w['layers'][layer][kind]
            if not e.get('pieces'):
                continue
            r = e.get('review', {})
            lines.append(f"| {layer} | {kind} | {e['pieces']} | {e['features']['0.0']} | {e['features']['1.0']} | {e['features']['2.0']} | {fmt(e['median_area_m2'])} | {fmt(e['median_width_m'])} | "
                         f"{r.get('statuses', '-')} | {r.get('confirmed_adjacent_pairs', '-')} |")
    lines += ['', '## Per-scene table (model layer; windows then review set; classes with pieces only)', '',
              '| scene | group | px | class | pieces | feat touching | feat gap<=2m | feat gap<=4m | pcs/feat gap<=2m | area m2 | med area | med width | med gap | frame | same-score pcs |', '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for s in report['scenes']:
        for kind in LINEAR:
            e = s['layers']['model'][kind]
            if not e.get('pieces'):
                continue
            lines.append(f"| {s['scene']} | {s['group']} | {s['image_width']} | {kind} | {e['pieces']} | {e['features']['0.0']} | {e['features']['1.0']} | {e['features']['2.0']} | "
                         f"{fmt(e['pieces_per_feature']['1.0'])} | {fmt(e['total_area_m2'], 1)} | {fmt(e['median_area_m2'])} | {fmt(e['median_width_m'])} | {fmt(e['gap_median_m'])} | "
                         f"{e['pieces_touching_frame']} | {e['pieces_in_same_score_groups']} |")
    lines += ['', '## Caveats', '']
    lines += [f'- {c}' for c in report['caveats']]
    path.write_text('\n'.join(lines) + '\n')


def run(out=OUT, scenes_root=SCENES):
    out.mkdir(parents=True, exist_ok=True)
    scene_rows, pieces = [], []
    for path, group in population(scenes_root):
        row, rows = census_scene(path, group)
        scene_rows.append(row)
        pieces.extend(rows)
    agg = aggregate(scene_rows, pieces)
    ranked = rank_windows(scene_rows)
    windows = [s for s in scene_rows if s['group'] == 'windows']
    review_set = [s for s in scene_rows if s['group'] == 'review_set']
    reviewer_window = next((s for s in scene_rows if s['scene'] == 'win-67c56bcab05af7a4'), None)
    report = {
        'version': VERSION, 'code_sha256': CODE_SHA256, 'computed_at': datetime.now(UTC).isoformat(timespec='seconds'),
        'direction': DIRECTION, 'classes': list(LINEAR), 'buffers_m': list(BUFFERS),
        'definitions': {'piece': 'object of kind road|footpath|driveway; model layer = latest.json (status != superseded); review layer = review.json live pieces (status not rejected/superseded)',
                        'feature_d': 'connected component of unary_union(piece.buffer(d)) per class per scene; pieces_per_feature = pieces / features',
                        'width_length': 'short / long side of the metric minimum_rotated_rectangle',
                        'gap': 'distance to the nearest same-class piece in the same scene and layer',
                        'touches_frame': f'pixel polygon within {FRAME_TOL_PX} px of the frame boundary',
                        'same_score_group': 'pieces of one class sharing an identical 4-dp score (components of one proposal mask)',
                        'confirmed_adjacent': 'two confirmed pieces of one class within one pixel of each other',
                        'tile_alignment': f'share of exterior boundary length within {TILE_TOL_PX} px of an interior multiple of {TILE_PX} px; random expectation 2*tol*lines/extent per axis'},
        'production_path': {'model_input_px': 1008, 'tiling': 'none (whole frame resized)', 'proposals_per_prompt_max': 80, 'score_threshold': .45,
                            'mask_binarisation': 'logit > 0 after bilinear resize to the frame', 'min_area_px': 12, 'simplify_px': .9,
                            'components': 'each disconnected component of a proposal mask is a separate object', 'duplicate_rule': 'intersection / min(area) > .8 same kind',
                            'linear_merge': 'none', 'source_files': ['scripts/ml/scene_extract.py', 'scripts/ml/sam3.py', 'scripts/ml/segmentation.py']},
        'population': {'windows': len(windows), 'window_sizes': dict(Counter(f"{s['image_width']} px" for s in windows)),
                       'review_set': len(review_set), 'review_set_breakdown': dict(Counter(s['scene'].split('-')[0] for s in review_set)),
                       'pieces_model': sum(1 for p in pieces if p['layer'] == 'model'), 'pieces_review': sum(1 for p in pieces if p['layer'] == 'review'),
                       'pieces_review_live': sum(1 for p in pieces if p['layer'] == 'review' and p['live']),
                       'invalid_rings': sum(e['invalid_rings'] for s in scene_rows for layer in s['layers'].values() for e in layer.values())},
        'review_signature_mismatch_scenes': sorted(s['scene'] for s in scene_rows if s['review_signature_matches_latest'] is False),
        'key_numbers': {group: {kind: {'pieces': agg[group]['model'][kind]['pieces'], 'features_d1': agg[group]['model'][kind]['features']['1.0'],
                                       'pieces_per_feature_d1': agg[group]['model'][kind]['pieces_per_feature']['1.0'],
                                       'median_gap_m': agg[group]['model'][kind]['gap_median_m'],
                                       'share_under_1m2': round(agg[group]['model'][kind]['pieces_under_m2']['1.0']/max(1, agg[group]['model'][kind]['pieces']), 3),
                                       'share_in_same_score_groups': round(agg[group]['model'][kind]['pieces_in_same_score_groups']/max(1, agg[group]['model'][kind]['pieces']), 3),
                                       'share_touching_frame': round(agg[group]['model'][kind]['pieces_touching_frame']/max(1, agg[group]['model'][kind]['pieces']), 3)}
                                for kind in LINEAR} for group in ('windows', 'review_set')},
        'aggregates': agg, 'most_fragmented_windows': ranked[:8], 'windows_ranked': len(ranked),
        'tile_alignment': {'top3_per_class': top_fragmented_per_class(scene_rows), 'pooled_1024_windows': pooled_alignment(pieces, scene_rows)},
        'reviewer_window': reviewer_window, 'scenes': scene_rows,
        'caveats': ['Every number is a count or geometry from latest.json / review.json; agreement between the model and review layers is not accuracy.',
                    'The review layer carries the detector pieces with the reviewer\'s statuses; no linear piece has source "reviewer", so no hand-drawn or merged linear ring exists to compare against.',
                    'pieces per feature at d > 0 counts separate detections that lie within 2d of each other as one feature (d = 1 m is a gap <= 2 m, labelled so since 2026-10-03 PM); two genuinely distinct driveways 1 m apart would also join at d = 0.5.',
                    'same-score groups rely on the 4-dp rounded score; an accidental tie between two proposals is possible but rare.',
                    'Tile alignment is only defined for 1024 px frames (interior x/y = 512); the detector itself sees the whole frame at 1008 px, so any alignment would come from the imagery mosaic or the frame cut, not from detector tiling.',
                    'Rejected review pieces are excluded from the review-layer geometry and shown in the status table; superseded pieces are excluded everywhere.']}
    (out/'census.json').write_text(json.dumps(report, indent=1, sort_keys=False, default=str) + '\n')
    with open(out/'pieces.jsonl', 'w') as handle:
        for p in pieces:
            handle.write(json.dumps({k: v for k, v in p.items() if not k.startswith('_')}) + '\n')
    write_markdown(report, out/'census.md')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', type=Path, default=OUT, help='output folder (default: the 2026-10-03 experiment folder)')
    args = parser.parse_args(argv)
    report = run(args.out)
    pop = report['population']
    print(f"scenes {pop['windows']} windows + {pop['review_set']} review-set; pieces model {pop['pieces_model']} review {pop['pieces_review']}")
    for group in ('windows', 'review_set'):
        for kind in LINEAR:
            e = report['aggregates'][group]['model'][kind]
            print(f"  {group:10s} {kind:9s} pieces {e['pieces']:5d} features gap<=2m {e['features']['1.0']:5d} pcs/feat gap<=2m {fmt(e['pieces_per_feature']['1.0'])} "
                  f"median gap {fmt(e['gap_median_m'])} m  <1m2 {e['pieces_under_m2']['1.0']}")
    print('most fragmented:', ', '.join(w['scene'] for w in report['most_fragmented_windows']))
    print('written:', args.out/'census.json', args.out/'census.md', args.out/'pieces.jsonl')


if __name__ == '__main__':
    main()
