"""Linear-continuity sidecar run-v2 (2026-10-03): merge_linear_fragments over the recapture windows, both layers, with the
reviewer's renders and marks sheet.

Reviewer authorisation (verbatim, 2026-10-03 15:10 AEST): "yes to defaults" on
data/reviews/eyes/experiments/linear-continuity-2026-10-03/DECISION-CARD.md; item 3(a) = sidecar only. This script applies
scene_extract.merge_linear_fragments (the authorised design, LINEAR_MERGE; the function the extraction path does NOT call)
to the model layer (latest.json objects) and the review layer (review.json objects) of every win-* / lot-* window and writes,
under data/reviews/eyes/experiments/linear-continuity-2026-10-03/run-v2/:

  <scene>.<layer>.json   features (merged_from, gaps, bridge geometry in pixels and metres), superseded members, refusals,
                         per-kind counts and two checks (decided pieces merged; bridges touching a blocker or a decided object)
  metrics.json           per layer and kind: pieces_in, features_out, merges by step, refusals by reason, gaps closed, bridge
                         area, area change %, pieces under 1 m2 before / after, same-mask groups rejoined fully / partly / not;
                         a sensitivity table (counts only) for d road 1.0 vs 1.5 m and footpath 0.8 vs 1.0 m; the checks
  renders/<scene>/feature-<n>-<id>.png and renders/<scene>.sheet.png    the 9 marks windows, review layer, native pixels x2
  renders/<scene>.no-merge.txt   one line when a marks window has no merge
  MARKS-<date>.md        the sheet the reviewer fills (right / wrong / unclear per merged feature); sub-eps blocker contacts
                         and, when run-v2/marks-notes.json exists ({feature id: text}), agent hints are printed on the line
                         (hints, never marks)

Repair r2 (2026-10-03, after the skeptic round): a bridge under BRIDGE_INSET_M2 gets a 2-px magenta outline and a magnified
inset of its region (features 7 and 8 had invisible bridges); member-id labels avoid each other (feature 8 lost one); the
sheet badge sits in a band above each panel (it overprinted the header) and a panel never shrinks below SHEET_MIN_SCALE;
per-feature records carry bridge_near_blockers / bridge_near_decided (0 < overlap <= touch_eps_m2) and metrics.checks
states the eps.

Read-only on data/scenes. No model is loaded: scene_extract is imported for its geometry only and nothing here touches the
segmenter. No latest.json / review.json / confidence.* is written; nothing is promoted. Every figure is a count or a
geometry, never accuracy. Run from the repo root:

    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_continuity_run              # everything
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_continuity_run --no-render  # JSON + metrics only
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_continuity_run --scenes win-67c56bcab05af7a4
"""
from __future__ import annotations

import argparse
import json
import math
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely import make_valid
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from ..scene_extract import (
    LINEAR_MERGE,
    LINEAR_REFUSALS,
    RING_SIMPLIFY,
    _metric_shape,
    _protected,
    merge_linear_fragments,
)
from ..segmentation import iter_polygons

ROOT = Path(__file__).resolve().parents[3]
SCENES = ROOT/'data/scenes'
EXP = ROOT/'data/reviews/eyes/experiments/linear-continuity-2026-10-03'
OUT = EXP/'run-v2'
VERSION = 'linear-continuity-run-v2'      # the marked set (2026-10-03 19:40 "yes to defaults"); new runs name their --out dir


def run_version(out):
    """'linear-continuity-<out dir name>': run-v2 is the reviewer's marked set and is never rewritten; run-v3 carries the
    19:40 defaults (result floor, water + building blockers, 2 px simplification)."""
    return f'linear-continuity-{Path(out).name}'
MARKS_DATE = '2026-10-03'
# The census's 8 most fragmented windows + the reviewer's own win-67c (DECISION-CARD item 3a).
MARKS_WINDOWS = ['win-150d924743036f24', 'win-17b5c9b30a5d3ff9', 'win-da0e5d5badc3b3ba', 'win-d24efc00bcd6a3e4',
                 'win-fa2e955f73294188', 'win-1dff35d6922a4132', 'win-ab373041656385bf', 'win-4cc07d6908cc688b',
                 'win-67c56bcab05af7a4']
RENDER_LAYER = 'review'
# Sensitivity variants: only d_m changes; every guard stays as approved. Counts only.
SENSITIVITY = {'road_d1.5': {'road': 1.5}, 'footpath_d1.0': {'footpath': 1.}}
UNDER_M2 = 1.

# ------------------------------------------------------------------------------------------------------------ render style
MARGIN_M = 6.
UPSCALE = 2
MEMBER_PALETTE = [(0, 210, 255), (255, 165, 0), (120, 255, 90), (255, 95, 95), (175, 130, 255), (0, 255, 200),
                  (255, 215, 120), (120, 190, 255), (255, 140, 220), (190, 255, 160)]
BRIDGE_FILL = (255, 0, 255, 120)
BRIDGE_LINE = (255, 0, 255)
CONFIRMED = (255, 255, 0)
OTHER_LINEAR = (170, 170, 170)
FEATURE_LINE = (255, 255, 255)
BACKGROUND = (18, 18, 18)
SHEET_CELL_MAX = 1100
SHEET_MAX_W = 2400
SHEET_MIN_SCALE = .75      # a sheet panel never shrinks below this: 12-px ids stay >= 9 px (da0e panel 6 was unreadable)
BRIDGE_INSET_M2 = .2       # a bridge under this area gets a 2-px outline and a magnified inset of its region
BRIDGE_THIN_M = .15        # a bridge whose mean width (area / half perimeter) is under this gets the 2-px outline too
INSET_SCALE = 4
INSET_PAD_M = .75
INSET_MAX_PX = 640         # the inset (drawn beside or below the crop, never over it) is at most this wide / tall


def window_ids(scenes_root=SCENES):
    return sorted(p.name for p in [*scenes_root.glob('win-*'), *scenes_root.glob('lot-*')]
                  if (p/'latest.json').exists() and not p.name.startswith('qa_'))


def load_scene(scene_id, scenes_root=SCENES):
    scene_dir = scenes_root/scene_id
    latest = json.loads((scene_dir/'latest.json').read_text())
    review_path = scene_dir/'review.json'
    review = json.loads(review_path.read_text()) if review_path.exists() else None
    return latest, review, scene_dir/f"{latest['image_sha256']}.png"


def design_with(d_m_overrides):
    return {**LINEAR_MERGE, 'd_m': {**LINEAR_MERGE['d_m'], **d_m_overrides}}


def design_json(design):
    return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, tuple) else v) for k, v in design.items()}


def run_merge(objects, to_meters, design=LINEAR_MERGE):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)      # shapely 2.1 oriented_envelope noise, same as polygon_details
        return merge_linear_fragments(objects, to_meters, design=design)


def pixel_spacing(to_meters):
    return float(abs(np.linalg.det(np.asarray(to_meters, dtype=float)[:2]))**.5)


def polygon_parts(geom, transform=None, decimals=3):
    """Polygon parts of a geometry as [{ring, holes}] (optionally transformed by a 3x3 matrix)."""
    def convert(coords):
        pts = np.asarray(coords, dtype=float)
        if transform is not None:
            pts = (np.c_[pts, np.ones(len(pts))] @ transform)[:, :2]
        return [[round(float(x), decimals), round(float(y), decimals)] for x, y in pts]
    return [{'ring': convert(p.exterior.coords[:-1]), 'holes': [convert(h.coords[:-1]) for h in p.interiors]}
            for p in iter_polygons(geom) if p.area > 0]


def classify_group(member_ids, feature_of):
    """One same-mask group (>= 2 candidate pieces sharing kind + score): 'full' when every member ended in one feature,
    'partial' when at least two did, 'none' when no two members share a feature."""
    targets = Counter(feature_of.get(i, i) for i in member_ids)
    if len(targets) == 1:
        return 'full'
    return 'partial' if max(targets.values()) >= 2 else 'none'


def bridge_of(feature_shape, member_shapes, eps):
    """The added surface of a feature: feature minus the union of its members, parts under eps m2 dropped."""
    bridge = feature_shape.difference(unary_union(member_shapes))
    parts = [p for p in iter_polygons(bridge) if p.area > eps]
    return unary_union(parts) if parts else Polygon()


def contact_record(o, overlap_m2):
    return {'id': o['id'], 'kind': o['kind'], 'status': o.get('status'), 'overlap_m2': float(f"{overlap_m2:.2e}")}


def analyse_layer(scene_id, layer, objects, to_meters, design=LINEAR_MERGE, version=VERSION):
    """merge_linear_fragments on one layer plus the sidecar's own re-measurement of every feature (bridge geometry, blocker
    and decided contact, member statuses) and per-kind extras (area change, under-1 m2 counts, same-mask groups)."""
    affine = np.asarray(to_meters, dtype=float)
    inverse = np.linalg.inv(np.c_[affine, [0., 0., 1.]])
    kinds = tuple(design['kinds'])
    eps = design['touch_eps_m2']
    merged, report = run_merge(objects, to_meters, design)
    by_id = {o['id']: o for o in objects}
    shapes = {o['id']: s for o in objects if (s := _metric_shape(o, affine)) is not None}
    live = [o for o in objects if o['id'] in shapes and o.get('status') not in ('rejected', 'superseded')]
    decided = [o for o in objects if o['id'] in shapes and o.get('status') in design['decided_statuses']]
    new_ids = {f['id'] for f in report['features']}
    new_objects = {o['id']: o for o in merged if o['id'] in new_ids and o.get('source') == design['source']}
    feature_of = {m: f['id'] for f in report['features'] for m in f['members']}
    checks = Counter({'features': 0, 'decided_members': 0, 'protected_members': 0, 'bridge_touches_blocker': 0,
                      'bridge_touches_decided': 0, 'features_touching_blocker_or_decided': 0,
                      'features_with_sub_eps_contact': 0, 'features_simplified': 0, 'slop_touches_blocker': 0,
                      'slop_touches_decided': 0})
    features = []
    for frep in report['features']:
        obj = new_objects[frep['id']]
        kind = obj['kind']
        feature_shape = _metric_shape(obj, affine)
        members = [by_id[i] for i in frep['members']]
        # The fill is measured on the guard-tested ring. A ring simplified at emission (19:40 card item 5) moves its edges by
        # up to 2 px; that slop (simplified feature minus the guard-tested feature) is measured apart from the bridge.
        tested_shape = (_metric_shape({'ring': frep['ring_unsimplified'], 'holes': frep.get('holes_unsimplified') or []}, affine)
                        if frep.get('ring_unsimplified') else None) or feature_shape
        bridge = bridge_of(tested_shape, [shapes[i] for i in frep['members']], eps)
        slop_parts = [p for p in iter_polygons(feature_shape.difference(tested_shape)) if p.area > eps] if tested_shape is not feature_shape else []
        slop = unary_union(slop_parts) if slop_parts else Polygon()
        merges = [m for m in report['merges'] if m['kind'] == kind and set(m['a_ids']) | set(m['b_ids']) <= set(frep['members'])]
        blocker_objects = [o for o in live if o['kind'] in design['blocker_kinds'] or (o['kind'] in kinds and o['kind'] != kind)]
        contacts = {o['id']: bridge.intersection(shapes[o['id']]).area for o in blocker_objects+decided}

        blockers = [contact_record(o, contacts[o['id']]) for o in blocker_objects if contacts[o['id']] > eps]
        decided_hits = [contact_record(o, contacts[o['id']]) for o in decided if contacts[o['id']] > eps]
        # sub-eps contacts (0 < overlap <= touch_eps_m2) pass the approved tolerance for ring rounding; they are listed on the
        # feature and the marks line so "0 touches" reads as "none above eps", never counted as a touch.
        near_blockers = [contact_record(o, contacts[o['id']]) for o in blocker_objects if 0. < contacts[o['id']] <= eps]
        near_decided = [contact_record(o, contacts[o['id']]) for o in decided if 0. < contacts[o['id']] <= eps]
        decided_members = [o['id'] for o in members if o.get('status') != 'candidate']
        protected_members = [o['id'] for o in members if _protected(o)]
        checks['features'] += 1
        checks['decided_members'] += len(decided_members)
        checks['protected_members'] += len(protected_members)
        checks['bridge_touches_blocker'] += bool(blockers)
        checks['bridge_touches_decided'] += bool(decided_hits)
        checks['features_touching_blocker_or_decided'] += bool(blockers or decided_hits)
        checks['features_with_sub_eps_contact'] += bool(near_blockers or near_decided)
        slop_contacts = {o['id']: slop.intersection(shapes[o['id']]).area for o in blocker_objects+decided} if not slop.is_empty else {}
        slop_blockers = [contact_record(o, slop_contacts[o['id']]) for o in blocker_objects if slop_contacts.get(o['id'], 0.) > eps]
        slop_decided = [contact_record(o, slop_contacts[o['id']]) for o in decided if slop_contacts.get(o['id'], 0.) > eps]
        checks['features_simplified'] += tested_shape is not feature_shape
        checks['slop_touches_blocker'] += bool(slop_blockers)
        checks['slop_touches_decided'] += bool(slop_decided)
        gaps = [m['gap_m'] for m in merges]
        features.append({
            'id': obj['id'], 'kind': kind, 'n_members': len(members),
            'members': [{'id': o['id'], 'status_in': o.get('status'), 'score': o.get('score'),
                         'area_m2': round(shapes[o['id']].area, 3), 'protected': _protected(o)} for o in members],
            'steps': sorted({m['step'] for m in merges}), 'gaps_m': gaps, 'gap_max_m': max(gaps) if gaps else None,
            'merges': merges, 'merged_from': obj['merged_from'],
            'area_m2': frep['area_m2'], 'pieces_union_m2': frep['pieces_union_m2'],
            # bridge_m2 = the report's feature-minus-union-of-pieces (since repair r2; it was the sum of per-pair fills);
            # bridge_recomputed_m2 = the same quantity re-measured here from the stored ring, parts under eps dropped. Renders
            # paint the recomputed geometry; the two agree to ring rounding.
            'bridge_m2': frep['bridge_m2'], 'bridge_recomputed_m2': round(float(bridge.area), 3),
            'vertices': frep['vertices'], 'vertex_cap': frep.get('vertex_cap', False), 'simplified': frep.get('simplified'),
            'largest_member_m2': frep.get('largest_member_m2'),
            # simplification slop: the simplified outline's surface outside the guard-tested ring, and what it overlaps (> eps)
            'slop_m2': round(float(slop.area), 3), 'slop_blockers': slop_blockers, 'slop_decided': slop_decided,
            'bridge': {'pixels': polygon_parts(bridge, inverse, 2), 'metres': polygon_parts(bridge)},
            'bridge_blockers': blockers, 'bridge_decided': decided_hits,
            'bridge_near_blockers': near_blockers, 'bridge_near_decided': near_decided,
            'decided_members': decided_members, 'protected_members': protected_members,
            'object': obj})
    superseded = [{'id': o['id'], 'kind': o['kind'], 'score': o.get('score'), 'status_in': by_id[o['id']].get('status'),
                   'area_m2': round(shapes[o['id']].area, 3), 'replacement_ids': o.get('replacement_ids')}
                  for o in merged if o['id'] in feature_of]
    kinds_out = {}
    for kind in kinds:
        joinable = [o for o in objects if o.get('kind') == kind and o.get('status') == 'candidate'
                    and not _protected(o) and o['id'] in shapes]
        area_before = {o['id']: shapes[o['id']].area for o in joinable}
        singles = [o for o in joinable if o['id'] not in feature_of]
        kind_features = [f for f in features if f['kind'] == kind]
        feature_areas = [_metric_shape(f['object'], affine).area for f in kind_features]
        before = sum(area_before.values())
        after = sum(area_before[o['id']] for o in singles) + sum(feature_areas)
        groups = defaultdict(list)
        for o in joinable:
            groups[round(float(o.get('score') or 0.), design['score_round'])].append(o['id'])
        multi = [ids for ids in groups.values() if len(ids) >= 2]
        tally = Counter(classify_group(ids, feature_of) for ids in multi)
        kinds_out[kind] = {
            **report['kinds'][kind],
            'area_before_m2': round(before, 3), 'area_after_m2': round(after, 3),
            'area_change_pct': round((after-before)/before*100., 3) if before else None,
            'under_1m2_before': sum(1 for a in area_before.values() if a < UNDER_M2),
            'under_1m2_after': sum(1 for o in singles if area_before[o['id']] < UNDER_M2) + sum(1 for a in feature_areas if a < UNDER_M2),
            'under_1m2_superseded': sum(1 for o in joinable if o['id'] in feature_of and area_before[o['id']] < UNDER_M2),
            'same_mask_groups': {'groups': len(multi), 'pieces_in_groups': sum(len(g) for g in multi),
                                 'full': tally['full'], 'partial': tally['partial'], 'none': tally['none']}}
    return {'version': version, 'scene': scene_id, 'layer': layer, 'design': design_json(design),
            'objects_in': len(objects), 'objects_out': len(merged),
            'status_in': dict(Counter(o.get('status') for o in objects if o.get('kind') in kinds)),
            'kinds': kinds_out, 'features': features, 'superseded': superseded, 'refusals': report['refusals'],
            'checks': dict(checks)}


def sensitivity_counts(objects, to_meters, design):
    _, report = run_merge(objects, to_meters, design)
    return report['kinds']


# ------------------------------------------------------------------------------------------------------------- aggregate

def _gap_stats(gaps):
    gaps = sorted(gaps)
    return {'n': len(gaps), 'median': round(float(np.median(gaps)), 3) if gaps else None,
            'p90': round(float(np.percentile(gaps, 90)), 3) if gaps else None, 'max': gaps[-1] if gaps else None}


def aggregate_kind(layer_results, kind):
    totals, refusals, groups = Counter(), Counter(), Counter()
    gaps, bridge = [], 0.
    for r in layer_results:
        k = r['kinds'].get(kind)
        if not k:
            continue
        for key in ('pieces_in', 'candidates', 'features_out', 'multi_piece_features', 'pieces_superseded',
                    'under_1m2_before', 'under_1m2_after', 'under_1m2_superseded'):
            totals[key] += k[key]
        for key in ('merges_undone_by_floor', 'simplified', 'vertex_cap'):
            totals[key] += k.get(key, 0)
        totals['merges_step1'] += k['merges']['step1']
        totals['merges_step2'] += k['merges']['step2']
        totals['area_before_m2'] += k['area_before_m2']
        totals['area_after_m2'] += k['area_after_m2']
        bridge += k['bridge_area_m2']
        refusals.update(k['refusals'])
        groups.update(k['same_mask_groups'])
        gaps += [m['gap_m'] for f in r['features'] if f['kind'] == kind for m in f['merges']]
    before, after = totals['area_before_m2'], totals['area_after_m2']
    return {'pieces_in': totals['pieces_in'], 'candidates': totals['candidates'], 'features_out': totals['features_out'],
            'multi_piece_features': totals['multi_piece_features'], 'pieces_superseded': totals['pieces_superseded'],
            'merges': {'step1': totals['merges_step1'], 'step2': totals['merges_step2'],
                       'total': totals['merges_step1']+totals['merges_step2']},
            'merges_undone_by_floor': totals['merges_undone_by_floor'],
            'simplified': totals['simplified'], 'vertex_cap': totals['vertex_cap'],
            'refusals': {reason: refusals[reason] for reason in LINEAR_REFUSALS if refusals[reason]},
            'gaps_closed_m': _gap_stats(gaps), 'bridge_area_m2': round(bridge, 3),
            'area_before_m2': round(before, 3), 'area_after_m2': round(after, 3),
            'area_change_pct': round((after-before)/before*100., 3) if before else None,
            'under_1m2': {'before': totals['under_1m2_before'], 'after': totals['under_1m2_after'],
                          'superseded': totals['under_1m2_superseded']},
            'same_mask_groups': {key: groups[key] for key in ('groups', 'pieces_in_groups', 'full', 'partial', 'none')}}


def aggregate_sensitivity(rows):
    """rows: list of report['kinds'] dicts (one per scene) for one variant and layer -> per-kind counts."""
    out = {}
    for kind in LINEAR_MERGE['kinds']:
        totals, refusals = Counter(), Counter()
        gap_max = 0.
        for kinds in rows:
            k = kinds.get(kind)
            if not k:
                continue
            totals['features_out'] += k['features_out']
            totals['multi_piece_features'] += k['multi_piece_features']
            totals['pieces_superseded'] += k['pieces_superseded']
            totals['merges_step1'] += k['merges']['step1']
            totals['merges_step2'] += k['merges']['step2']
            totals['bridge_area_m2'] += k['bridge_area_m2']
            refusals.update(k['refusals'])
            gap_max = max(gap_max, k['gaps_closed_m']['max'] or 0.)
        out[kind] = {'features_out': totals['features_out'], 'multi_piece_features': totals['multi_piece_features'],
                     'pieces_superseded': totals['pieces_superseded'],
                     'merges': {'step1': totals['merges_step1'], 'step2': totals['merges_step2'],
                                'total': totals['merges_step1']+totals['merges_step2']},
                     'refusals': {reason: refusals[reason] for reason in LINEAR_REFUSALS if refusals[reason]},
                     'bridge_area_m2': round(totals['bridge_area_m2'], 3), 'gap_closed_max_m': round(gap_max, 3)}
    return out


def build_metrics(results, sensitivity, scene_ids, version=VERSION):
    """results: {scene: {layer: analyse_layer(...)}}; sensitivity: {variant: {layer: [report kinds per scene]}}."""
    layers = {}
    checks = {}
    for layer in ('model', 'review'):
        layer_results = [results[s][layer] for s in scene_ids if layer in results[s]]
        layers[layer] = {kind: aggregate_kind(layer_results, kind) for kind in LINEAR_MERGE['kinds']}
        total = Counter()
        for r in layer_results:
            total.update(r['checks'])
        checks[layer] = {**dict(total), 'scenes': len(layer_results), 'touch_eps_m2': LINEAR_MERGE['touch_eps_m2'],
                         'touch_rule': 'a bridge touches an object when their intersection exceeds touch_eps_m2; smaller contacts '
                                       'are listed per feature as bridge_near_blockers / bridge_near_decided',
                         'features_with_decided_member': sum(1 for r in layer_results for f in r['features'] if f['decided_members']),
                         'bridge_recompute_max_abs_diff_m2': round(max((abs(f['bridge_m2']-f['bridge_recomputed_m2'])
                                                                        for r in layer_results for f in r['features']), default=0.), 3)}
    sens = {}
    for variant, per_layer in sensitivity.items():
        sens[variant] = {'d_m': design_with(SENSITIVITY[variant])['d_m'], 'changed': SENSITIVITY[variant],
                         'layers': {layer: aggregate_sensitivity(rows) for layer, rows in per_layer.items()}}
    default_rows = {layer: [results[s][layer]['kinds'] for s in scene_ids if layer in results[s]] for layer in ('model', 'review')}
    sens = {'default': {'d_m': dict(LINEAR_MERGE['d_m']), 'changed': {},
                        'layers': {layer: aggregate_sensitivity(rows) for layer, rows in default_rows.items()}}, **sens}
    table = []
    for kind, variant in (('road', 'road_d1.5'), ('footpath', 'footpath_d1.0')):
        for layer in ('model', 'review'):
            a, b = sens['default']['layers'][layer][kind], sens[variant]['layers'][layer][kind]
            table.append({'kind': kind, 'layer': layer,
                          'd_default_m': LINEAR_MERGE['d_m'][kind], 'd_variant_m': SENSITIVITY[variant][kind],
                          'merges_default': a['merges']['total'], 'merges_variant': b['merges']['total'],
                          'multi_piece_features_default': a['multi_piece_features'], 'multi_piece_features_variant': b['multi_piece_features'],
                          'pieces_superseded_default': a['pieces_superseded'], 'pieces_superseded_variant': b['pieces_superseded'],
                          'refusals_default': a['refusals'], 'refusals_variant': b['refusals'],
                          'bridge_area_m2_default': a['bridge_area_m2'], 'bridge_area_m2_variant': b['bridge_area_m2'],
                          'gap_closed_max_m_default': a['gap_closed_max_m'], 'gap_closed_max_m_variant': b['gap_closed_max_m']})
    marks = {s: {layer: {'multi_piece_features': len(results[s][layer]['features']),
                         'merges': sum(len(f['merges']) for f in results[s][layer]['features'])}
                 for layer in results[s]} for s in MARKS_WINDOWS if s in results}
    vertices = [f['vertices'] for s in scene_ids for r in results[s].values() for f in r['features']]
    simplified = [f for s in scene_ids for r in results[s].values() for f in r['features'] if (f.get('simplified') or {}).get('applied')]
    return {'version': version, 'computed_at': datetime.now(UTC).isoformat(timespec='seconds'),
            'design': design_json(LINEAR_MERGE), 'scenes': len(scene_ids),
            'scenes_without_review': [s for s in scene_ids if 'review' not in results[s]],
            'layers': layers, 'checks': checks,
            'sensitivity': {'note': 'd_m only; every guard as approved; counts, never accuracy', 'variants': sens, 'table': table},
            'marks_windows': {'layer': RENDER_LAYER, 'windows': marks,
                              'features_to_mark': sum(m.get(RENDER_LAYER, {}).get('multi_piece_features', 0) for m in marks.values())},
            'vertex_cap': {'features_over_200_vertices': sum(1 for v in vertices if v > 200), 'max_vertices': max(vertices, default=0),
                           'features_simplified': len(simplified),
                           'simplified_max_abs_area_change_pct': round(max((abs(f['simplified']['area_change_pct'] or 0.) for f in simplified), default=0.), 3)}}


# ---------------------------------------------------------------------------------------------------------------- render

def load_font(size):
    for candidate in ('/System/Library/Fonts/Menlo.ttc', '/System/Library/Fonts/Helvetica.ttc',
                      '/System/Library/Fonts/Supplemental/Arial.ttf'):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def halo_text(draw, xy, text, font, colour, halo=(0, 0, 0)):
    x, y = xy
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        draw.text((x+dx, y+dy), text, fill=halo, font=font)
    draw.text((x, y), text, fill=colour, font=font)


def draw_dotted(draw, a, b, colour, width, on=4, off=4):
    (x0, y0), (x1, y1) = a, b
    length = math.hypot(x1-x0, y1-y0)
    if length == 0:
        return
    ux, uy = (x1-x0)/length, (y1-y0)/length
    t = 0.
    while t < length:
        s = min(length, t+on)
        draw.line([(x0+ux*t, y0+uy*t), (x0+ux*s, y0+uy*s)], fill=colour, width=width)
        t += on+off


def draw_outline(draw, geom, colour, width, tf, dotted=False):
    for part in iter_polygons(geom):
        for ring in (part.exterior, *part.interiors):
            pts = [tf(x, y) for x, y in ring.coords]
            if dotted:
                for a, b in zip(pts[:-1], pts[1:], strict=False):
                    draw_dotted(draw, a, b, colour, width)
            else:
                draw.line(pts, fill=colour, width=width, joint='curve')


def fill_geom(overlay, geom, fill, tf):
    draw = ImageDraw.Draw(overlay)
    for part in iter_polygons(geom):
        draw.polygon([tf(x, y) for x, y in part.exterior.coords[:-1]], fill=fill)
        for hole in part.interiors:
            draw.polygon([tf(x, y) for x, y in hole.coords[:-1]], fill=(0, 0, 0, 0))


def wrap_lines(draw, lines, font, width):
    out = []
    for line in lines:
        current = ''
        for word in line.split(' '):
            trial = (current+' '+word).strip()
            if draw.textlength(trial, font=font) <= width or not current:
                current = trial
            else:
                out.append(current)
                current = word
        if current:
            out.append(current)
    return out


def pixel_polygon(obj):
    try:
        poly = Polygon(obj['ring'], [h for h in obj.get('holes') or [] if len(h) >= 3])
    except (ValueError, TypeError):
        return None
    return poly if poly.area > 0 else None


def scale_bar_metres(crop_width_px, spacing):
    for metres in (20., 10., 5., 2., 1., .5):
        if metres/spacing*UPSCALE <= crop_width_px*.3:
            return metres
    return .5


def place_label(boxes, x, y, w, h, width, height, step=16, tries=8):
    """Top-left corner for a w x h label wanted at (x, y): clamped to the canvas and moved down, then up, in `step` px
    hops until it overlaps none of `boxes` [(x0, y0, x1, y1)]; the chosen box is appended to `boxes`. Falls back to the
    clamped wanted spot when every hop collides."""
    def clamp(px, py):
        return min(max(px, 2), max(2, width-w-2)), min(max(py, 2), max(2, height-h-2))

    def free(bx):
        return all(bx[2] <= o[0] or bx[0] >= o[2] or bx[3] <= o[1] or bx[1] >= o[3] for o in boxes)
    chosen = None
    for dy in [0, *(step*i for i in range(1, tries+1)), *(-step*i for i in range(1, tries+1))]:
        px, py = clamp(x, y+dy)
        bx = (px, py, px+w, py+h)
        if free(bx):
            chosen = bx
            break
    if chosen is None:
        px, py = clamp(x, y)
        chosen = (px, py, px+w, py+h)
    boxes.append(chosen)
    return chosen[0], chosen[1]


def inset_plan(bridge_bounds, canvas_size, pad_px, max_px=INSET_MAX_PX):
    """For a small bridge: (source region, scale, (inset width, height)) of a magnified inset. The region is the bridge
    bounds padded by pad_px and clipped to the canvas; the scale is INSET_SCALE, reduced in half steps (not below 2) so the
    inset is at most max_px wide and tall. The inset is drawn beside the crop (or below it when it does not fit beside), so
    it never covers the imagery."""
    width, height = canvas_size
    x0, y0, x1, y1 = bridge_bounds
    rx0, ry0 = int(max(0, math.floor(x0-pad_px))), int(max(0, math.floor(y0-pad_px)))
    rx1, ry1 = int(min(width, math.ceil(x1+pad_px))), int(min(height, math.ceil(y1+pad_px)))
    rw, rh = max(1, rx1-rx0), max(1, ry1-ry0)
    scale = max(2., math.floor(min(INSET_SCALE, max_px/rw, max_px/rh)*2.)/2.)
    return (rx0, ry0, rx1, ry1), scale, (int(rw*scale), int(rh*scale))


def feature_title(index, scene_id, feature):
    steps = '+'.join(str(s) for s in feature['steps'])
    gap = f"{feature['gap_max_m']:.2f}" if feature['gap_max_m'] is not None else '-'
    return f"{index}. {scene_id} {feature['id']} {feature['kind']} step {steps} gap {gap} m bridge {feature['bridge_recomputed_m2']:.2f} m2"


def render_feature(image, scene_id, feature, index, objects, to_meters, review_revision, out_path):
    """One crop: feature bounds + MARGIN_M, native pixels upscaled x2 nearest; raw imagery, members outlined in their own
    colour with 8-char ids, bridge filled magenta, feature outline bold white, reviewer-decided linear pieces in yellow
    (never merged), other live linear pieces thin grey, a scale bar and the title. Everything is drawn on the crop canvas,
    so PIL clips at the crop edge."""
    spacing = pixel_spacing(to_meters)
    fpoly = pixel_polygon(feature['object'])
    x0, y0, x1, y1 = fpoly.bounds
    m = MARGIN_M/spacing
    cx0, cy0 = int(max(0, math.floor(x0-m))), int(max(0, math.floor(y0-m)))
    cx1, cy1 = int(min(image.width, math.ceil(x1+m))), int(min(image.height, math.ceil(y1+m)))
    canvas = image.crop((cx0, cy0, cx1, cy1)).resize(((cx1-cx0)*UPSCALE, (cy1-cy0)*UPSCALE), Image.NEAREST)
    font, small = load_font(14), load_font(12)
    member_ids = [mm['id'] for mm in feature['members']]
    colours = {mid: MEMBER_PALETTE[i % len(MEMBER_PALETTE)] for i, mid in enumerate(member_ids)}

    def tf(x, y):
        return ((x-cx0)*UPSCALE, (y-cy0)*UPSCALE)
    view = box(cx0, cy0, cx1, cy1)
    kinds = LINEAR_MERGE['kinds']
    members = set(member_ids)
    by_id = {o['id']: o for o in objects}
    linear_in_view = [(o, p) for o in objects if o.get('kind') in kinds and o['id'] not in members
                      and o.get('status') not in ('rejected', 'superseded') and (p := pixel_polygon(o)) is not None
                      and p.intersects(view)]
    draw = ImageDraw.Draw(canvas)
    for o, p in linear_in_view:                                           # 1. other live linear candidates, thin grey
        if o.get('status') == 'candidate':
            draw_outline(draw, p, OTHER_LINEAR, 1, tf)
    overlay = Image.new('RGBA', canvas.size, (0, 0, 0, 0))                  # 2. the bridge, semi-transparent magenta
    bridge_parts = [Polygon(part['ring'], part['holes']) for part in feature['bridge']['pixels'] if len(part['ring']) >= 3]
    for part in bridge_parts:
        fill_geom(overlay, part, BRIDGE_FILL, tf)
    canvas = Image.alpha_composite(canvas.convert('RGBA'), overlay).convert('RGB')
    # Repair r3 (2026-10-03, render skeptic round 2): the inset is cut from THIS state, bridge fill on raw imagery with only
    # 1-px member outlines, never from the finished canvas, where the 6-px feature halo and 2-px member outlines paint over a
    # sub-0.2 m2 bridge (features 2, 7 and 8 showed no magenta at any zoom).
    inset_source = canvas.copy()
    inset_draw = ImageDraw.Draw(inset_source)
    for mid in member_ids:
        p = pixel_polygon(by_id[mid])
        if p is not None:
            draw_outline(inset_draw, p, colours[mid], 1, tf)
    draw = ImageDraw.Draw(canvas)
    small_bridge = bool(bridge_parts) and feature['bridge_recomputed_m2'] < BRIDGE_INSET_M2
    metre_parts = [make_valid(Polygon(part['ring'], part['holes'])) for part in feature['bridge']['metres'] if len(part['ring']) >= 3]
    bridge_area, bridge_len = sum(p.area for p in metre_parts), sum(p.length for p in metre_parts)
    mean_width = bridge_area/(bridge_len/2.) if bridge_len else 0.
    thin_bridge = bool(bridge_parts) and bridge_len > 0 and mean_width < BRIDGE_THIN_M
    for part in bridge_parts:                           # a sub-0.2 m2 bridge gets a 2-px outline + inset; a sliver the outline
        draw_outline(draw, part, BRIDGE_LINE, 2 if (small_bridge or thin_bridge) else 1, tf)
    draw_outline(draw, fpoly, (0, 0, 0), 6, tf)                             # 3. feature outline, bold white with halo
    draw_outline(draw, fpoly, FEATURE_LINE, 3, tf)
    for mid in member_ids:                                                  # 4. members in their own colour
        p = pixel_polygon(by_id[mid])
        if p is not None:
            draw_outline(draw, p, colours[mid], 2, tf)
    for o, p in linear_in_view:                                             # 5. reviewer-decided linear pieces, yellow
        if o.get('status') in ('confirmed', 'needs_adjustment'):
            draw_outline(draw, p, CONFIRMED, 2, tf, dotted=o['status'] == 'needs_adjustment')
    inset = None
    boxes = []                                                              # label boxes already placed
    if small_bridge or thin_bridge:                                         # 5b. magnified inset of the bridge region (r3: slivers too)
        bounds = [part.bounds for part in bridge_parts]                     # bounds of bounds: no union of possibly invalid parts
        bx0, by0 = min(b[0] for b in bounds), min(b[1] for b in bounds)
        bx1, by1 = max(b[2] for b in bounds), max(b[3] for b in bounds)
        (ix0, iy0), (ix1, iy1) = tf(bx0, by0), tf(bx1, by1)
        region, scale, size = inset_plan((ix0, iy0, ix1, iy1), canvas.size, INSET_PAD_M/spacing*UPSCALE)
        inset = (inset_source.crop(region).resize(size, Image.NEAREST), region, scale)  # from the pre-outline state

    def place(lx, ly, text):
        w, h = draw.textlength(text, font=small), small.size+2
        return place_label(boxes, lx, ly, w, h, canvas.width, canvas.height, step=small.size+4)
    for mid in member_ids:                                                  # 6. labels: member ids beside small pieces
        p = pixel_polygon(by_id[mid])
        if p is None:
            continue
        bx0, by0, bx1, by1 = p.bounds
        if (bx1-bx0)*UPSCALE < 60 or (by1-by0)*UPSCALE < 24:
            lx, ly = tf(bx1, by0)
            lx, ly = lx+4, ly-small.size
        else:
            lx, ly = tf(*p.representative_point().coords[0])
            lx, ly = lx-30, ly-7
        halo_text(draw, place(lx, ly, mid), mid, small, colours[mid])
    for o, p in linear_in_view:
        if o.get('status') in ('confirmed', 'needs_adjustment'):
            text = f"{'*' if o['status'] == 'confirmed' else '~'} {o['id'][:8]} {o['kind']} {o['status']}"
            lx, ly = tf(*p.representative_point().coords[0])
            halo_text(draw, place(lx-30, ly+8, text), text, small, CONFIRMED)
    metres = scale_bar_metres(canvas.width, spacing)                         # 7. scale bar
    bar = metres/spacing*UPSCALE
    bx, by = 12, canvas.height-14
    draw.line([(bx, by), (bx+bar, by)], fill=(0, 0, 0), width=6)
    draw.line([(bx, by), (bx+bar, by)], fill=(255, 255, 255), width=2)
    halo_text(draw, (bx, by-small.size-6), f"{metres:g} m", small, (255, 255, 255))
    panel = canvas
    if inset is not None:                        # 8. the inset beside the crop (below when it does not fit): magenta frame,
        image_part, region, scale = inset        #    the source region dotted on the crop
        rx0, ry0, rx1, ry1 = region
        for a, b in (((rx0, ry0), (rx1, ry0)), ((rx1, ry0), (rx1, ry1)), ((rx1, ry1), (rx0, ry1)), ((rx0, ry1), (rx0, ry0))):
            draw_dotted(draw, a, b, BRIDGE_LINE, 1, on=3, off=3)
        frame = ImageDraw.Draw(image_part)
        frame.rectangle([0, 0, image_part.width-1, image_part.height-1], outline=BRIDGE_LINE, width=2)
        halo_text(frame, (4, 3), f"inset x{scale:g} · bridge {feature['bridge_recomputed_m2']:.2f} m2 · dotted box on the crop",
                  small, BRIDGE_LINE)
        if canvas.width+8+image_part.width <= max(canvas.width, 640):
            panel = Image.new('RGB', (canvas.width+8+image_part.width, max(canvas.height, image_part.height)), BACKGROUND)
            panel.paste(canvas, (0, 0))
            panel.paste(image_part, (canvas.width+8, 0))
        else:
            panel = Image.new('RGB', (max(canvas.width, image_part.width), canvas.height+8+image_part.height), BACKGROUND)
            panel.paste(canvas, (0, 0))
            panel.paste(image_part, (0, canvas.height+8))
    # title block above the panel; the badge number is the one used on the sheet and in MARKS
    width = max(panel.width, 640)
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    badge = f"{index}"
    bw = int(probe.textlength(badge, font=font))+12
    title = feature_title(index, scene_id, feature)
    lines = [f"members {', '.join(member_ids)} · pieces {feature['pieces_union_m2']:.1f} -> feature {feature['area_m2']:.1f} m2 "
             f"· gaps {', '.join(f'{g:.2f}' for g in feature['gaps_m'])} m · {RENDER_LAYER} layer rev {review_revision} "
             f"· {spacing:.3f} m/px x{UPSCALE} · crop {cx1-cx0}x{cy1-cy0} px",
             'coloured outline + id = member piece · magenta fill = bridge (added surface) · white bold = merged feature '
             '· yellow (* / ~) = reviewer confirmed / needs_adjustment, never merged · grey = other live linear pieces'
             + (f' · magenta frame = x{inset[2]:g} inset of the dotted bridge region, beside / below the crop (bridge < {BRIDGE_INSET_M2:g} m2)'
                if inset else '')
             + (f' · bridge drawn with a 2-px outline: a sliver {mean_width:.2f} m wide on average'
                if thin_bridge and not small_bridge else '')]
    title_lines = wrap_lines(probe, [title], font, width-bw-14)
    lines = wrap_lines(probe, lines, small, width-12)
    header = 6+len(title_lines)*(font.size+4)+len(lines)*(small.size+4)+4
    tile = Image.new('RGB', (width, header+panel.height+6), BACKGROUND)
    draw = ImageDraw.Draw(tile)
    draw.rectangle([6, 4, 6+bw, 4+font.size+6], fill=BRIDGE_LINE)
    draw.text((12, 6), badge, fill=(255, 255, 255), font=font)
    y = 6
    for line in title_lines:
        draw.text((6+bw+8, y), line, fill=(255, 255, 255), font=font)
        y += font.size+4
    for line in lines:
        draw.text((6, y), line, fill=(235, 235, 235), font=small)
        y += small.size+4
    tile.paste(panel, ((width-panel.width)//2, header))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tile.save(out_path)
    return tile


def contact_sheet(tiles, scene_id, review_revision, out_path):
    """All merged features of one window, numbered, packed into rows (the per-feature PNGs stay native x2; a tile wider
    or taller than SHEET_CELL_MAX shrinks for the sheet only, so each cell also carries its number at sheet scale)."""
    font, big = load_font(15), load_font(28)
    band = big.size+16                                        # the #N badge sits in this band above the panel, not on it
    scaled = []
    for n, tile in tiles:
        s = max(SHEET_MIN_SCALE, min(1., SHEET_CELL_MAX/max(tile.size)))
        if s < 1.:
            tile = tile.resize((max(1, int(tile.width*s)), max(1, int(tile.height*s))), Image.BILINEAR)
        scaled.append((n, tile))
    rows, row = [], []
    for n, tile in scaled:                                   # shelf packing in order, SHEET_MAX_W wide
        if row and sum(t.width for _, t in row)+len(row)*8+tile.width > SHEET_MAX_W:
            rows.append(row)
            row = []
        row.append((n, tile))
    if row:
        rows.append(row)
    width = max(sum(t.width for _, t in r)+(len(r)+1)*8 for r in rows)
    header = 30
    height = header+sum(max(t.height for _, t in r)+band+8 for r in rows)
    sheet = Image.new('RGB', (max(width, 900), height), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    numbers = ', '.join(str(n) for n, _ in scaled)
    draw.text((8, 7), f"{scene_id} · {RENDER_LAYER} layer rev {review_revision} · {len(scaled)} merged feature(s) "
                      f"#{numbers} · mark each right / wrong / unclear in MARKS-{MARKS_DATE}.md", fill=(255, 255, 255), font=font)
    y = header
    for r in rows:
        x = 8
        for n, tile in r:
            label = f"#{n}"
            bw = int(draw.textlength(label, font=big))+16
            draw.rectangle([x, y+2, x+bw, y+band-4], fill=BRIDGE_LINE)
            draw.text((x+8, y+6), label, fill=(255, 255, 255), font=big)
            draw.text((x+bw+10, y+(band-font.size)//2), f"feature {n} · mark in MARKS-{MARKS_DATE}.md", fill=(235, 235, 235), font=font)
            sheet.paste(tile, (x, y+band))
            x += tile.width+8
        y += max(t.height for _, t in r)+band+8
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


def render_context(image, scene_id, objects, to_meters, review_revision, refusals, out_path):
    """Unnumbered context image for a marks window without a merge: the whole window at native x2 with every live linear
    piece (candidates grey, reviewer confirmed yellow *, needs_adjustment yellow dotted ~) so the sheet shows what stayed."""
    spacing = pixel_spacing(to_meters)
    canvas = image.resize((image.width*UPSCALE, image.height*UPSCALE), Image.NEAREST)
    draw = ImageDraw.Draw(canvas)
    font, small = load_font(14), load_font(12)

    def tf(x, y):
        return (x*UPSCALE, y*UPSCALE)
    live = [(o, p) for o in objects if o.get('kind') in LINEAR_MERGE['kinds']
            and o.get('status') not in ('rejected', 'superseded') and (p := pixel_polygon(o)) is not None]
    for o, p in live:
        if o.get('status') == 'candidate':
            draw_outline(draw, p, OTHER_LINEAR, 1, tf)
    decided = [(o, p) for o, p in live if o.get('status') in ('confirmed', 'needs_adjustment')]
    for o, p in decided:
        draw_outline(draw, p, CONFIRMED, 2, tf, dotted=o['status'] == 'needs_adjustment')
    for o, p in decided:
        text = f"{'*' if o['status'] == 'confirmed' else '~'} {o['id'][:8]} {o['kind'][0]}"
        lx, ly = tf(*p.representative_point().coords[0])
        lx = min(max(lx-20, 2), canvas.width-draw.textlength(text, font=small)-2)
        ly = min(max(ly-6, 2), canvas.height-small.size-2)
        halo_text(draw, (lx, ly), text, small, CONFIRMED)
    metres = scale_bar_metres(canvas.width, spacing)
    bar = metres/spacing*UPSCALE
    bx, by = 12, canvas.height-14
    draw.line([(bx, by), (bx+bar, by)], fill=(0, 0, 0), width=6)
    draw.line([(bx, by), (bx+bar, by)], fill=(255, 255, 255), width=2)
    halo_text(draw, (bx, by-small.size-6), f"{metres:g} m", small, (255, 255, 255))
    counts = Counter(r['reason'] for r in refusals)
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    lines = wrap_lines(probe, [f"{scene_id} · {RENDER_LAYER} layer rev {review_revision} · NO MERGE under the approved design "
                               f"· nothing to mark (context only, not numbered)",
                               f"refusals {dict(sorted(counts.items())) or 'none'} · live linear pieces {len(live)} · reviewer-decided "
                               f"{len(decided)} (yellow: * confirmed, ~ needs_adjustment; never merged) · grey = candidates · "
                               f"{spacing:.3f} m/px x{UPSCALE}"], small, canvas.width-12)
    header = 6+len(lines)*(small.size+4)+4
    tile = Image.new('RGB', (canvas.width, header+canvas.height+6), BACKGROUND)
    d = ImageDraw.Draw(tile)
    for i, line in enumerate(lines):
        d.text((6, 6+i*(small.size+4)), line, fill=(255, 255, 255) if i == 0 else (235, 235, 235), font=font if i == 0 else small)
    tile.paste(canvas, (0, header))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tile.save(out_path)
    return out_path


def render_window(scene_id, result, objects, image_path, to_meters, review_revision, renders_dir, start_index):
    """Renders the review-layer features of one marks window; returns (rows for MARKS, next index, sheet path or note)."""
    features = sorted(result['features'], key=lambda f: (LINEAR_MERGE['kinds'].index(f['kind']), f['id']))
    if not features:
        refusals = Counter(r['reason'] for r in result['refusals'])
        decided = sum(1 for o in objects if o.get('kind') in LINEAR_MERGE['kinds'] and o.get('status') in ('confirmed', 'needs_adjustment'))
        note = (f"{scene_id} {RENDER_LAYER} layer rev {review_revision}: no merge under the approved design "
                f"(refusals {dict(sorted(refusals.items())) or 'none'}; {decided} reviewer-decided linear pieces untouched); nothing to mark.")
        note_path = renders_dir/f'{scene_id}.no-merge.txt'
        note_path.parent.mkdir(parents=True, exist_ok=True)
        context = render_context(Image.open(image_path).convert('RGB'), scene_id, objects, to_meters, review_revision,
                                 result['refusals'], renders_dir/f'{scene_id}.context.png')
        note_path.write_text(note+f" Context render (not numbered): {context.name}\n")
        return [], start_index, note_path
    image = Image.open(image_path).convert('RGB')
    tiles, rows = [], []
    index = start_index
    for feature in features:
        path = renders_dir/scene_id/f"feature-{index}-{feature['id']}.png"
        tiles.append((index, render_feature(image, scene_id, feature, index, objects, to_meters, review_revision, path)))
        rows.append({'n': index, 'scene': scene_id, 'feature': feature, 'render': path})
        index += 1
    sheet = contact_sheet(tiles, scene_id, review_revision, renders_dir/f'{scene_id}.sheet.png')
    return rows, index, sheet


def slop_text(feature):
    """'slop 0.42 m2 on vegetation 1234abcd 0.31 m2, ...' for a simplified feature whose slop overlaps something; '' otherwise."""
    hits = feature.get('slop_blockers', []) + feature.get('slop_decided', [])
    if not hits:
        return ''
    return (f"slop {feature.get('slop_m2', 0):.2f} m2 on "
            + ', '.join(f"{h['kind']} {h['id'][:8]} {h['overlap_m2']:.2g} m2" for h in hits))


def contact_text(feature):
    """'sub-eps contact: kind id8 1.2e-05 m2, ...' for the bridge's below-tolerance overlaps, or ''."""
    near = feature.get('bridge_near_blockers', [])+feature.get('bridge_near_decided', [])
    if not near:
        return ''
    return 'sub-eps contact: '+', '.join(f"{h['kind']} {h['id'][:8]} {h['overlap_m2']:.1e} m2" for h in near)


def write_marks(path, windows, metrics, out=OUT, notes=None):
    """windows: [(scene_id, rows, sheet_or_note_path, review_revision)] in MARKS_WINDOWS order; notes: {feature id: text}
    printed on the line as 'hint:' (an agent's reading for the reviewer's eye, never a mark)."""
    notes = notes or {}
    eps = LINEAR_MERGE['touch_eps_m2']
    def rel(p):
        p = Path(p).resolve()
        return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)
    total = sum(len(rows) for _, rows, _, _ in windows)
    with_merges = [(s, len(rows)) for s, rows, _, _ in windows if rows]
    lines = [f"# Linear-continuity {Path(out).name} marks sheet, {MARKS_DATE}", '',
             "Mark each line right or wrong; 'all right' or 'wrong: 3, 7' in chat is enough (numbers are the global line numbers "
             "below; the same number is on the render badge and in the file name).", '',
             f"Totals: {total} merged features to mark across {len(with_merges)} of {len(windows)} windows "
             f"({', '.join(f'{s} {n}' for s, n in with_merges) or 'none'}); "
             f"{len(windows)-len(with_merges)} window(s) had no merge in the {RENDER_LAYER} layer.",
             f"Layer: {RENDER_LAYER} (review.json objects). Design: d driveway {LINEAR_MERGE['d_m']['driveway']} / footpath "
             f"{LINEAR_MERGE['d_m']['footpath']} / road {LINEAR_MERGE['d_m']['road']} m; bridge <= {LINEAR_MERGE['bridge_max_ratio']:g} x "
             f"smaller piece; union ratio <= {LINEAR_MERGE['union_ratio_max']}; blockers {', '.join(LINEAR_MERGE['blocker_kinds'])} "
             f"+ other linear classes; decided pieces block; candidates only; result floor {LINEAR_MERGE.get('result_floor_m2', 0):g} m2 "
             f"(feature AND largest member); rings over {RING_SIMPLIFY['vertices']} vertices simplified at {RING_SIMPLIFY['tolerance_px']:g} px. Checks over all {metrics['scenes']} windows, both layers: decided "
             f"pieces merged = {sum(c['decided_members'] for c in metrics['checks'].values())}, features whose bridge touches a "
             f"blocker = {sum(c['bridge_touches_blocker'] for c in metrics['checks'].values())}, touching a decided object = "
             f"{sum(c['bridge_touches_decided'] for c in metrics['checks'].values())}. A touch is an intersection > {eps:g} m2 "
             f"(touch_eps_m2, the tolerance for ring rounding); smaller contacts (0 < overlap <= {eps:g} m2) are printed on the "
             f"line as 'sub-eps contact': {sum(c.get('features_with_sub_eps_contact', 0) for c in metrics['checks'].values())} "
             f"feature(s) over both layers. The 2 x rule reads the smallest original piece the bridge touches. Rings simplified at "
             f"emission: {sum(c.get('features_simplified', 0) for c in metrics['checks'].values())} feature(s) over both layers; their "
             f"outline moves up to {RING_SIMPLIFY['tolerance_px']:g} px from the members' rings and that slop (not the bridge) overlaps a "
             f"blocker on {sum(c.get('slop_touches_blocker', 0) for c in metrics['checks'].values())} and a decided object on "
             f"{sum(c.get('slop_touches_decided', 0) for c in metrics['checks'].values())} of them (printed on the line as 'slop').",
             "Render key: coloured outline + 8-char id = member piece; magenta fill = bridge (the added surface); white bold = "
             "the merged feature; yellow (* / ~) = your confirmed / needs_adjustment pieces, shown so you can see they were not "
             "merged; grey = other live linear pieces; magenta frame = magnified inset of the dotted bridge region when the "
             f"bridge is under {BRIDGE_INSET_M2:g} m2. 'bridge' is the painted surface (feature minus its members). 'hint:' = an "
             "agent's reading for your eye, not a mark. Paths are relative to the repo root.", '']
    for scene_id, rows, sheet, revision in windows:
        if rows:
            lines.append(f"## {scene_id} — {len(rows)} feature(s) · review rev {revision} · sheet {rel(sheet)}")
            lines.append('')
            for row in rows:
                f = row['feature']
                steps = '+'.join(str(s) for s in f['steps'])
                gap = f"{f['gap_max_m']:.2f}" if f['gap_max_m'] is not None else '-'
                extras = [t for t in (contact_text(f), slop_text(f), f"hint: {notes[f['id']]}" if f['id'] in notes else '') if t]
                lines.append(f"{row['n']}. {f['kind']} {f['id']} = {', '.join(m['id'] for m in f['members'])} · gap {gap} m · "
                             f"bridge {f['bridge_recomputed_m2']:.2f} m2 · step {steps} · render {rel(row['render'])}"
                             + ''.join(f" · {t}" for t in extras) + " · [ right / wrong / unclear ]")
        else:
            lines.append(f"## {scene_id} — no merge · review rev {revision} · note {rel(sheet)}")
            lines.append('')
            lines.append(f"- {sheet.read_text().strip()}")
        lines.append('')
    lines += ['---', f"Generated by scripts/ml/experiments/linear_continuity_run.py at {metrics['computed_at']}; metrics in "
                     f"{rel(out/'metrics.json')}; per-scene results {rel(out)}/<scene>.<layer>.json. Nothing promoted; "
                     "data/scenes untouched."]
    path.write_text('\n'.join(lines)+'\n')
    return path


# ------------------------------------------------------------------------------------------------------------------ main

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--scenes', nargs='*', help='scene ids (default: every win-* / lot-* window with a latest.json)')
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--no-render', action='store_true', help='skip the renders and the marks sheet')
    parser.add_argument('--scenes-root', type=Path, default=SCENES, help='scene directory (read-only; default data/scenes)')
    parser.add_argument('--notes', type=Path, help='JSON {feature id: hint} printed on the marks lines (default <out>/marks-notes.json)')
    args = parser.parse_args(argv)
    notes_path = args.notes or args.out/'marks-notes.json'
    notes = json.loads(notes_path.read_text()) if notes_path.exists() else {}
    scene_ids = args.scenes or window_ids(args.scenes_root)
    args.out.mkdir(parents=True, exist_ok=True)
    results, sensitivity = {}, {variant: defaultdict(list) for variant in SENSITIVITY}
    scenes = {}
    for scene_id in scene_ids:
        latest, review, image_path = load_scene(scene_id, args.scenes_root)
        to_meters = latest['pixel_to_meters']
        layers = {'model': latest['objects'], **({'review': review['objects']} if review else {})}
        results[scene_id] = {}
        for layer, objects in layers.items():
            result = analyse_layer(scene_id, layer, objects, to_meters, version=run_version(args.out))
            result.update({'image_width': latest['image_width'], 'image_height': latest['image_height'],
                           'pixel_to_meters': to_meters, 'model_signature': latest.get('model_signature'),
                           'review_revision': review.get('revision') if review else None})
            (args.out/f'{scene_id}.{layer}.json').write_text(json.dumps(result, indent=1))
            results[scene_id][layer] = result
            for variant, overrides in SENSITIVITY.items():
                sensitivity[variant][layer].append(sensitivity_counts(objects, to_meters, design_with(overrides)))
        scenes[scene_id] = (latest, review, image_path)
    metrics = build_metrics(results, sensitivity, scene_ids, version=run_version(args.out))
    (args.out/'metrics.json').write_text(json.dumps(metrics, indent=1))
    print(json.dumps({'layers': metrics['layers'], 'checks': metrics['checks'], 'sensitivity_table': metrics['sensitivity']['table'],
                      'marks_windows': metrics['marks_windows'], 'vertex_cap': metrics['vertex_cap']}, indent=1))
    print('written:', args.out/'metrics.json', f'+ {sum(len(v) for v in results.values())} layer files')
    if args.no_render:
        return
    windows, index = [], 1
    renders_dir = args.out/'renders'
    for scene_id in MARKS_WINDOWS:
        if scene_id not in results or RENDER_LAYER not in results[scene_id]:
            continue
        latest, review, image_path = scenes[scene_id]
        revision = review.get('revision') if review else None
        rows, index, sheet = render_window(scene_id, results[scene_id][RENDER_LAYER], review['objects'], image_path,
                                           latest['pixel_to_meters'], revision, renders_dir, index)
        windows.append((scene_id, rows, sheet, revision))
        print('rendered:', scene_id, len(rows), sheet)
    marks = write_marks(args.out/f'MARKS-{MARKS_DATE}.md', windows, metrics, args.out, notes)
    print('written:', marks)


if __name__ == '__main__':
    main()
