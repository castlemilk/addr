"""Roof-join sidecar run-v1 (2026-10-07): merge_building_fragments over the 54 recapture windows, both layers, with the reviewer's
renders and marks sheet.

Reviewer authorisation (verbatim, 2026-10-07 ~00:15 AEDT): "yes to defaults" on
data/reviews/eyes/experiments/roof-fragmentation-2026-10-06/DECISION-CARD.md; item 1(a) = a guarded BUILDING join beside the linear
join, rolled out "as the linear card: sidecar re-run on the 54 windows (both layers), each join rendered with its bridge coloured,
you mark right / wrong ..., the tally gates promotion". This script applies scene_extract.merge_building_fragments (BUILDING_MERGE;
the function the extraction path does NOT call) to the model layer (latest.json objects; counts only) and the review layer
(review.json objects; the rendered and marked layer) of every win-* / lot-* window and writes, under
data/reviews/eyes/experiments/roof-fragmentation-2026-10-06/run-v1/:

  <scene>.<layer>.json   features (members, rules, merges with their figures, bridge and absorbed geometry in pixels and metres,
                         carried members), superseded members, refusals by reason, umbrellas, counts and checks
  metrics.json           per layer: pieces -> features per rule and step, refusals by reason, umbrellas, party-wall pairs >= 20 m2
                         before / after (must be equal), decided (confirmed) pieces merged (must be 0), bridges touching a decided
                         object or a third building, carried-member features (rendered, not applied), gaps, bridge area, area change,
                         pieces under 1 m2 before / after, simplification; the named windows' feature counts
  renders/<scene>/feature-<n>-<id>.png   EVERY multi-piece feature of the review layer, native pixels (integer nearest upscale so the
                         crop reads), members in distinct colours with 8-char ids, absorbed + bridged surface filled magenta, the
                         resulting outline bold white, occluders green (the strip's occluder labelled), decided buildings yellow,
                         scale bar, title with the rule
  renders/<scene>.sheet.png              per-window contact sheet of its features; <scene>.no-join.txt when a window has none
  MARKS-2026-10-07.md    one numbered line per feature (window, feature id, members, rule, figures, render path,
                         [ right / wrong / unclear ]); the six named windows first; header says how to answer in chat

Run-v2 (2026-10-07, after the skeptic's by-eye tally of run-v1, .run/session-2026-10-07-roofjoin/eye-tally.md: right 25 / wrong 30 /
unclear 19, small_touch 2 / 28 / 12): `--design v2` runs the PROPOSAL dict scene_extract.BUILDING_MERGE_V2 (party-wall guard on step 1,
step-1 outside gap 0.1 m, small_touch off; not approved, nothing promoted) into `--out run-v2`; metrics.json then also carries
'v1_features' (every run-v1 review feature identical / shrunk / gone in v2, with the skeptic's mark) and 'v2_new'; the marks sheet
MARKS-v2-<date>.md keeps identical features in run-v1 order with the v1 line number and the skeptic's mark as a HINT. Renders: absorbed
fill 25 % (outline kept), bridge 60 %, crops padded to >= 200 px a side into the image. `--tone-study` writes
<out>/analysis/small-touch-tone.{json,md}: median RGB / CIELAB under the smaller and the largest member of every run-v1 review feature
and the deltaE76 between them, against the skeptic's marks (numbers only).

Raw panel (2026-10-07, after the v2 by-eye pass, .run/session-2026-10-07-roofjoin/eye-tally-v2.md: the 25 % fill still reads solid purple
on dark roofs and pink on pale ones, and 4 of 6 disagreements needed a raw crop): `--raw-panel` (default ON for --design v2, OFF for v1
so run-v1 renders are not rewritten) puts the SAME crop at the SAME scale to the LEFT of the annotated panel, native imagery with 1 px
outlines only (the merged feature white, each member in its colour on top), no fill, no labels over the pixels, captioned "raw: 1 px
outlines, no fill"; the annotated panel is unchanged. `--render-only` re-draws <out>/renders (feature PNGs, contact sheets, no-join
notes) from the <scene>.review.json files and metrics.json already in <out>, without recomputing a join; it rewrites neither the JSON
nor the marks sheet (same numbering and file names) and refuses when a scene's live review revision or model signature has moved.

Read-only on data/scenes. No model is loaded: scene_extract is imported for its geometry only. No latest.json / review.json /
confidence.* is written; nothing is promoted; no count moves. Every figure is a count or a geometry, never accuracy. Run from the
repo root (nice'd: the host is shared):

    nice -n 19 scripts/ml/.venv/bin/python -m scripts.ml.experiments.roof_join_run              # everything (v1)
    nice -n 19 scripts/ml/.venv/bin/python -m scripts.ml.experiments.roof_join_run --no-render  # JSON + metrics only
    nice -n 19 scripts/ml/.venv/bin/python -m scripts.ml.experiments.roof_join_run --scenes win-b6901e4edcbc5495 --out /tmp/x
    nice -n 19 scripts/ml/.venv/bin/python -m scripts.ml.experiments.roof_join_run --design v2 --out <EXP>/run-v2
    nice -n 19 scripts/ml/.venv/bin/python -m scripts.ml.experiments.roof_join_run --tone-study --out <EXP>/run-v2
    nice -n 19 scripts/ml/.venv/bin/python -m scripts.ml.experiments.roof_join_run --design v2 --out <EXP>/run-v2 --render-only
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from ..scene_extract import (
    BUILDING_MERGE,
    BUILDING_MERGE_V2,
    BUILDING_REFUSALS,
    BUILDING_RING_SIMPLIFY,
    BUILDING_RULES,
    _building_decided,
    _building_joinable,
    _metric_shape,
    _protected,
    merge_building_fragments,
)
from ..segmentation import iter_polygons

ROOT = Path(__file__).resolve().parents[3]
SCENES = ROOT/'data/scenes'
EXP = ROOT/'data/reviews/eyes/experiments/roof-fragmentation-2026-10-06'
OUT = EXP/'run-v1'
RUN_V1 = EXP/'run-v1'                                   # the reference run for 'v1_features' / marks inheritance (read-only)
EYE_TALLY = ROOT/'.run/session-2026-10-07-roofjoin/eye-tally.md'   # the skeptic's 74 marks on run-v1 (hints only)
MARKS_DATE = '2026-10-07'
DESIGNS = {'v1': BUILDING_MERGE, 'v2': BUILDING_MERGE_V2}
# What v2 changed, one sentence each (printed on the v2 marks sheet header).
V2_CHANGES = ["party_wall_guard_step1: the party-wall guard (both sides >= 20 m2, cover < 0.05) now also refuses the two components of ONE "
              "mask in step 1, which v1 exempted (run-v1 #15 lot-29772, 291.7 + 31.1 m2 at 0.32 m).",
              "step1_outside_gap_max_m 0.1: a same-mask component with no overlap and a gap over 0.1 m is not rejoined in step 1 (it is "
              "still read by step 2's own rules), which v1 bridged up to 0.5 m (run-v1 #7 win-6dbb766c398af77e, 5.6 m2 at 0.49 m).",
              "small_touch off: the 'touching within 0.5 m and under 12 m2' rule no longer fires (run-v1: 42 features, 2 right / 28 wrong / "
              "12 unclear by eye); cover, occluder_strip and same_mask stay as approved.",
              "Renders: the absorbed surface is filled at 25 % (outline kept) instead of near-opaque, the bridge at 60 %; crops are "
              "padded to at least 200 px a side into the image, and a piece within 4 m of the image edge says so on the title."]
# The card's six named windows, in the card's order (items 1-6); rendered first on the marks sheet.
NAMED_WINDOWS = ['win-088ce9350360b877', 'win-36e1a99bfe889daf', 'win-69468b2b783cd964', 'win-6dbb766c398af77e',
                 'win-b6901e4edcbc5495', 'win-fa2e955f73294188']
NAMED_OBJECTS = {'win-088ce9350360b877': 'cb0c275d8d047eee', 'win-36e1a99bfe889daf': '1ddf5a8ce600afc8',
                 'win-69468b2b783cd964': 'd86ebd7f0565eeac', 'win-6dbb766c398af77e': '133736ada6ace260',
                 'win-b6901e4edcbc5495': 'afd10322539fe730', 'win-fa2e955f73294188': '23f916def2008f85'}
RENDER_LAYER = 'review'
UNDER_M2 = 1.
# Sensitivity variants (counts only; nothing rendered or applied): the strip rule's share bar and the touching radius. Every other
# guard stays as approved.
SENSITIVITY = {'strip_share_0.8': {'strip_cover_min': .8}, 'strip_share_0.5': {'strip_cover_min': .5}, 'touch_1.0m': {'touch_m': 1.}}
KIND = BUILDING_MERGE['kind']

# ------------------------------------------------------------------------------------------------------------ render style
MARGIN_M = 4.
MIN_CROP_PX = 480          # integer nearest upscale until the crop's longest side reaches this (native pixels, no resampling)
MIN_SIDE_PX = 200          # run-v2: no crop under this many native pixels on a side; padded into the image (run-v1 #4 was 158x50 px)
MAX_UPSCALE = 4
MEMBER_PALETTE = [(0, 210, 255), (255, 165, 0), (120, 255, 90), (255, 95, 95), (175, 130, 255), (0, 255, 200),
                  (255, 215, 120), (120, 190, 255), (255, 140, 220), (190, 255, 160), (255, 255, 120), (140, 255, 255)]
BRIDGE_FILL = (255, 0, 255, 153)      # the strip: surface the feature carries beyond its pieces (60 %; run-v1 was 150/255)
ABSORBED_FILL = (255, 0, 255, 64)     # the smaller members' surface outside the largest member (25 %; run-v1 was 70/255, near-opaque by eye)
ABSORBED_LINE = (255, 150, 255)       # run-v2: the absorbed surface keeps an outline now that its fill is faint
BRIDGE_LINE = (255, 0, 255)
OCCLUDER = (40, 220, 60)
DECIDED = (255, 255, 0)
OTHER_BUILDING = (175, 175, 175)
FEATURE_LINE = (255, 255, 255)
BACKGROUND = (18, 18, 18)
SHEET_CELL_MAX = 1000      # per panel: a two-panel (raw + annotated) tile may be twice as wide before the sheet shrinks it
RAW_CAPTION = 'raw: 1 px outlines, no fill'
PANEL_GAP = 10             # background between the raw panel (left) and the annotated panel (right)
SHEET_MAX_W = 2400
SHEET_MIN_SCALE = .75


def window_ids(scenes_root=SCENES):
    return sorted(p.name for p in [*scenes_root.glob('win-*'), *scenes_root.glob('lot-*')]
                  if (p/'latest.json').exists() and not p.name.startswith('qa_'))


def load_scene(scene_id, scenes_root=SCENES):
    scene_dir = scenes_root/scene_id
    latest = json.loads((scene_dir/'latest.json').read_text())
    review_path = scene_dir/'review.json'
    review = json.loads(review_path.read_text()) if review_path.exists() else None
    return latest, review, scene_dir/f"{latest['image_sha256']}.png"


def design_json(design):
    return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, tuple) else v) for k, v in design.items()}


def run_merge(objects, to_meters, image_size, design=BUILDING_MERGE):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)      # shapely 2.1 oriented_envelope noise, same as polygon_details
        return merge_building_fragments(objects, to_meters, image_size=image_size, design=design)


def sensitivity_counts(objects, to_meters, image_size, overrides, design=BUILDING_MERGE):
    _, report = run_merge(objects, to_meters, image_size, {**design, **overrides})
    k = report['kinds'][KIND]
    return {'multi_piece_features': k['multi_piece_features'], 'pieces_superseded': k['pieces_superseded'], 'merges': k['merges'],
            'refusals': k['refusals'], 'bridge_area_m2': k['bridge_area_m2']}


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


def parts_over(geom, eps):
    parts = [p for p in iter_polygons(geom) if p.area > eps]
    return unary_union(parts) if parts else Polygon()


def contact_record(o, overlap_m2):
    return {'id': o['id'], 'kind': o['kind'], 'status': o.get('status'), 'overlap_m2': float(f"{overlap_m2:.2e}")}


def same_mask_key(o):
    return (o.get('source'), o.get('prompt'), round(float(o.get('score') or 0.), BUILDING_MERGE['score_round']))


def building_pairs(objects, shapes, design=BUILDING_MERGE):
    """Every touching pair (gap <= touch_m) of live building pieces of one object list with the figures the join reads; the
    party-wall count of the card (both sides >= 20 m2, cover < 0.05) under the join's guard definition and the census's stricter
    one (same-mask pairs excluded as well)."""
    live = [o for o in objects if o.get('kind') == KIND and o['id'] in shapes and o.get('status') not in ('rejected', 'superseded')]
    pairs = []
    for i, a in enumerate(live):
        for b in live[i+1:]:
            ga, gb = shapes[a['id']], shapes[b['id']]
            gap = ga.distance(gb)
            if gap > design['touch_m']:
                continue
            smaller = min(ga.area, gb.area)
            cover = ga.intersection(gb).area/smaller if smaller else 0.
            party = smaller >= design['substantial_m2'] and cover < design['party_wall_cover_max']
            pairs.append({'a': a['id'], 'b': b['id'], 'gap_m': round(float(gap), 3), 'cover': round(float(cover), 4),
                          'smaller_m2': round(float(smaller), 3), 'party_wall': bool(party),
                          'party_wall_strict': bool(party and same_mask_key(a) != same_mask_key(b))})
    return pairs


def analyse_layer(scene_id, layer, objects, to_meters, image_size, version, design=BUILDING_MERGE):
    """merge_building_fragments on one layer plus the sidecar's own re-measurement of every feature (bridge and absorbed geometry,
    decided / third-building contact, member statuses, carried members) and the layer's before / after counts."""
    affine = np.asarray(to_meters, dtype=float)
    inverse = np.linalg.inv(np.c_[affine, [0., 0., 1.]])
    eps = design['touch_eps_m2']
    merged, report = run_merge(objects, to_meters, image_size, design)
    by_id = {o['id']: o for o in objects}
    shapes = {o['id']: s for o in objects if (s := _metric_shape(o, affine)) is not None}
    live = [o for o in objects if o['id'] in shapes and o.get('status') not in ('rejected', 'superseded')]
    decided = [o for o in objects if o['id'] in shapes and _building_decided(o, design)]
    occluders = [o for o in live if o['kind'] in design['occluder_kinds']]
    buildings = [o for o in live if o['kind'] == KIND]
    new_ids = {f['id'] for f in report['features']}
    new_objects = {o['id']: o for o in merged if o['id'] in new_ids and o.get('source') == design['source']}
    feature_of = {m: f['id'] for f in report['features'] for m in f['members']}
    checks = Counter({'features': 0, 'decided_members': 0, 'protected_members': 0, 'confirmed_members': 0,
                      'bridge_touches_decided': 0, 'bridge_touches_third_building': 0, 'slop_touches_decided': 0,
                      'slop_touches_third_building': 0, 'features_with_carried_member': 0,
                      'features_simplified': 0, 'features_over_cap': 0})
    features = []
    for frep in report['features']:
        obj = new_objects[frep['id']]
        feature_shape = _metric_shape(obj, affine)
        members = [by_id[i] for i in frep['members']]
        member_shapes = [shapes[i] for i in frep['members']]
        largest_id = max(frep['members'], key=lambda i: shapes[i].area)
        union = unary_union(member_shapes)
        tested_shape = (_metric_shape({'ring': frep['ring_unsimplified'], 'holes': frep.get('holes_unsimplified') or []}, affine)
                        if frep.get('ring_unsimplified') else None) or feature_shape
        bridge = parts_over(tested_shape.difference(union), eps)               # the strip(s): surface beyond the pieces
        absorbed = parts_over(union.difference(shapes[largest_id]), eps)      # the smaller members' surface outside the largest
        merges = [m for m in report['merges'] if set(m['a_ids']) | set(m['b_ids']) <= set(frep['members']) and not m.get('undone')]
        member_set = set(frep['members'])
        # The join tests its strip (frep['bridge_m2'], metres before rounding) against decided objects; the recomputed bridge also
        # holds ring-rounding / repair slop (2 dp pixels, a collinear vertex dropped). A contact on a feature whose strip is empty is
        # slop, reported apart (slop_*), never counted as a bridge touching something.
        contacts_third = [contact_record(o, bridge.intersection(shapes[o['id']]).area) for o in buildings
                          if o['id'] not in member_set and not bridge.is_empty and bridge.intersection(shapes[o['id']]).area > eps]
        contacts_decided = [contact_record(o, bridge.intersection(shapes[o['id']]).area) for o in decided
                            if not bridge.is_empty and bridge.intersection(shapes[o['id']]).area > eps]
        slop_only = frep['bridge_m2'] <= eps
        third, slop_third = ([], contacts_third) if slop_only else (contacts_third, [])
        decided_hits, slop_decided = ([], contacts_decided) if slop_only else (contacts_decided, [])
        strip_occluders = {m['strip']['occluder']['id']: m['strip']['occluder'] for m in merges
                           if m.get('rule') == 'occluder_strip' and m.get('strip') and m['strip'].get('occluder')}
        occluders_over = [contact_record(o, union.intersection(shapes[o['id']]).area) for o in occluders
                          if union.intersection(shapes[o['id']]).area > UNDER_M2 and o['id'] not in strip_occluders]
        decided_members = [o['id'] for o in members if _building_decided(o, design)]
        confirmed_members = [o['id'] for o in members if o.get('status') == 'confirmed']
        protected_members = [o['id'] for o in members if _protected(o)]
        not_joinable = [o['id'] for o in members if not _building_joinable(o, design)]
        checks['features'] += 1
        checks['decided_members'] += len(decided_members)
        checks['confirmed_members'] += len(confirmed_members)
        checks['protected_members'] += len(protected_members)
        checks['bridge_touches_decided'] += bool(decided_hits)
        checks['bridge_touches_third_building'] += bool(third)
        checks['slop_touches_decided'] += bool(slop_decided)
        checks['slop_touches_third_building'] += bool(slop_third)
        checks['features_with_carried_member'] += bool(frep['carried_members'])
        checks['features_simplified'] += bool((frep.get('simplified') or {}).get('applied'))
        checks['features_over_cap'] += bool(frep.get('vertex_cap'))
        gaps = [m['gap_m'] for m in merges]
        covers = [m['cover'] for m in merges]
        features.append({
            'id': obj['id'], 'kind': KIND, 'n_members': len(members), 'rules': frep['rules'],
            'members': [{'id': o['id'], 'status_in': o.get('status'), 'score': o.get('score'), 'area_m2': round(shapes[o['id']].area, 3),
                         'carried': bool(o.get('carried_from')), 'carried_role': (o.get('carried_from') or {}).get('role')
                         if isinstance(o.get('carried_from'), dict) else None, 'vertices': len(o.get('ring') or []),
                         'protected': _protected(o), 'largest': o['id'] == largest_id} for o in members],
            'largest_member': largest_id, 'steps': sorted({m['step'] for m in merges}),
            'gaps_m': gaps, 'gap_max_m': max(gaps) if gaps else None, 'covers': covers, 'cover_max': max(covers) if covers else None,
            'smaller_min_m2': min((m['smaller_m2'] for m in merges), default=None),
            'merges': merges, 'merged_from': obj['merged_from'],
            'area_m2': frep['area_m2'], 'pieces_union_m2': frep['pieces_union_m2'], 'largest_member_m2': frep['largest_member_m2'],
            'bridge_m2': frep['bridge_m2'], 'bridge_recomputed_m2': round(float(bridge.area), 3),
            'absorbed_m2': round(float(absorbed.area), 3),
            'vertices': frep['vertices'], 'vertex_cap': frep.get('vertex_cap', False), 'simplified': frep.get('simplified'),
            'bridge': {'pixels': polygon_parts(bridge, inverse, 2), 'metres': polygon_parts(bridge)},
            'absorbed': {'pixels': polygon_parts(absorbed, inverse, 2), 'metres': polygon_parts(absorbed)},
            'strip_occluders': sorted(strip_occluders.values(), key=lambda o: o['id']),
            'occluders_over_1m2': occluders_over,
            'bridge_decided': decided_hits, 'bridge_third_buildings': third,
            'slop_m2': round(float(bridge.area), 3) if slop_only else round(float(max(0., bridge.area-frep['bridge_m2'])), 3),
            'slop_decided': slop_decided, 'slop_third_buildings': slop_third,
            'carried_members': frep['carried_members'], 'decided_members': decided_members, 'confirmed_members': confirmed_members,
            'protected_members': protected_members, 'not_joinable_members': not_joinable,
            'named_object_member': [i for i in frep['members'] if i == NAMED_OBJECTS.get(scene_id)],
            'object': obj})
    superseded = [{'id': o['id'], 'kind': o['kind'], 'score': o.get('score'), 'status_in': by_id[o['id']].get('status'),
                   'area_m2': round(shapes[o['id']].area, 3), 'replacement_ids': o.get('replacement_ids')}
                  for o in merged if o['id'] in feature_of]
    joinable = [o for o in objects if o['id'] in shapes and _building_joinable(o, design)]
    area_before = {o['id']: shapes[o['id']].area for o in joinable}
    singles = [o for o in joinable if o['id'] not in feature_of]
    feature_areas = [_metric_shape(f['object'], affine).area for f in features]
    before = sum(area_before.values())
    after = sum(area_before[o['id']] for o in singles)+sum(feature_areas)
    groups = defaultdict(list)
    for o in joinable:
        groups[same_mask_key(o)].append(o['id'])
    multi = [ids for ids in groups.values() if len(ids) >= 2]
    tally = Counter(('full' if len(t := Counter(feature_of.get(i, i) for i in ids)) == 1 else 'partial' if max(t.values()) >= 2 else 'none')
                    for ids in multi)
    pairs_before = building_pairs(objects, shapes, design)
    merged_shapes = {**shapes, **{f['id']: _metric_shape(f['object'], affine) for f in features}}
    pairs_after = building_pairs(merged, merged_shapes, design)
    party = {'before': sum(1 for p in pairs_before if p['party_wall']), 'after': sum(1 for p in pairs_after if p['party_wall']),
             'strict_before': sum(1 for p in pairs_before if p['party_wall_strict']),
             'strict_after': sum(1 for p in pairs_after if p['party_wall_strict'])}
    party['equal'] = party['before'] == party['after']
    party['strict_equal'] = party['strict_before'] == party['strict_after']
    # Why before / after move without any party wall being merged: (inside) an input party-wall pair whose two pieces ended in ONE
    # feature (the card's NEVER; must be 0 under the strict definition, a same-mask pair is one detection); (collapsed) a duplicate
    # mask's pairs with the same neighbour fold into one once the duplicates are one feature; (appeared) a feature that grew past
    # 20 m2 or within 0.5 m of a substantial neighbour; (gone) a pair whose canonical pair is no longer a party wall.
    before_party = {frozenset((p['a'], p['b'])): p for p in pairs_before if p['party_wall']}
    inside = [p for k, p in before_party.items() if len({feature_of.get(i) for i in k}) == 1 and feature_of.get(next(iter(k)))]
    canonical = {frozenset(feature_of.get(i, i) for i in k) for k in before_party}
    after_party = {frozenset((p['a'], p['b'])) for p in pairs_after if p['party_wall']}
    party.update({'inside_a_feature': len(inside), 'inside_a_feature_strict': sum(1 for p in inside if p['party_wall_strict']),
                  'inside_pairs': [{'a': p['a'], 'b': p['b'], 'same_mask': not p['party_wall_strict']} for p in inside],
                  'collapsed_by_duplicates': len(before_party)-len(canonical),
                  'appeared': len([k for k in after_party if k not in canonical]),
                  'gone': len([k for k in canonical if k not in after_party])})
    counts = {
        **report['kinds'][KIND],
        'area_before_m2': round(before, 3), 'area_after_m2': round(after, 3),
        'area_change_pct': round((after-before)/before*100., 3) if before else None,
        'under_1m2_before': sum(1 for a in area_before.values() if a < UNDER_M2),
        'under_1m2_after': sum(1 for o in singles if area_before[o['id']] < UNDER_M2)+sum(1 for a in feature_areas if a < UNDER_M2),
        'under_1m2_superseded': sum(1 for o in joinable if o['id'] in feature_of and area_before[o['id']] < UNDER_M2),
        'same_mask_groups': {'groups': len(multi), 'pieces_in_groups': sum(len(g) for g in multi),
                             'full': tally['full'], 'partial': tally['partial'], 'none': tally['none']},
        'touching_pairs_before': len(pairs_before), 'touching_pairs_after': len(pairs_after), 'party_wall_pairs': party}
    checks['party_wall_pairs_unequal'] = int(not party['equal'])
    checks['party_wall_pairs_inside_a_feature'] = party['inside_a_feature']
    checks['party_wall_pairs_inside_a_feature_strict'] = party['inside_a_feature_strict']
    return {'version': version, 'scene': scene_id, 'layer': layer, 'design': design_json(design),
            'objects_in': len(objects), 'objects_out': len(merged),
            'status_in': dict(Counter(o.get('status') for o in objects if o.get('kind') == KIND)),
            'counts': counts, 'umbrellas': report['umbrellas'], 'features': features, 'superseded': superseded,
            'refusals': report['refusals'], 'vertex_cap': report['vertex_cap'], 'checks': dict(checks)}


# ------------------------------------------------------------------------------------------------------------- aggregate

def _gap_stats(values):
    values = sorted(values)
    return {'n': len(values), 'median': round(float(np.median(values)), 3) if values else None,
            'p90': round(float(np.percentile(values, 90)), 3) if values else None, 'max': values[-1] if values else None}


def aggregate_layer(layer_results):
    totals, refusals, groups, party = Counter(), Counter(), Counter(), Counter()
    merges = Counter()
    gaps, covers, bridge = [], [], 0.
    for r in layer_results:
        k = r['counts']
        for key in ('pieces_in', 'candidates', 'joinable', 'carried_undecided_joinable', 'features_out', 'multi_piece_features',
                    'pieces_superseded', 'merges_undone_by_floor', 'pairs_tried', 'umbrellas', 'simplified', 'vertex_cap',
                    'ring_repaired', 'features_with_carried_member', 'under_1m2_before', 'under_1m2_after', 'under_1m2_superseded',
                    'touching_pairs_before', 'touching_pairs_after'):
            totals[key] += k.get(key, 0)
        totals['area_before_m2'] += k['area_before_m2']
        totals['area_after_m2'] += k['area_after_m2']
        bridge += k['bridge_area_m2']
        merges.update(k['merges'])
        refusals.update(k['refusals'])
        groups.update(k['same_mask_groups'])
        party.update({key: k['party_wall_pairs'][key] for key in ('before', 'after', 'strict_before', 'strict_after', 'inside_a_feature',
                                                                   'inside_a_feature_strict', 'collapsed_by_duplicates', 'appeared', 'gone')})
        gaps += [m['gap_m'] for f in r['features'] for m in f['merges']]
        covers += [m['cover'] for f in r['features'] for m in f['merges']]
    before, after = totals['area_before_m2'], totals['area_after_m2']
    return {**{key: totals[key] for key in ('pieces_in', 'candidates', 'joinable', 'carried_undecided_joinable', 'features_out',
                                            'multi_piece_features', 'pieces_superseded', 'merges_undone_by_floor', 'pairs_tried',
                                            'umbrellas', 'simplified', 'vertex_cap', 'ring_repaired', 'features_with_carried_member')},
            'merges': {'step1': merges['step1'], 'step2': merges['step2'], 'total': merges['step1']+merges['step2'],
                       **{rule: merges[rule] for rule in BUILDING_RULES}},
            'refusals': {reason: refusals[reason] for reason in BUILDING_REFUSALS if refusals[reason]},
            'gaps_closed_m': _gap_stats(gaps), 'cover_of_joins': _gap_stats(covers), 'bridge_area_m2': round(bridge, 3),
            'area_before_m2': round(before, 3), 'area_after_m2': round(after, 3),
            'area_change_pct': round((after-before)/before*100., 3) if before else None,
            'under_1m2': {'before': totals['under_1m2_before'], 'after': totals['under_1m2_after'], 'superseded': totals['under_1m2_superseded']},
            'same_mask_groups': {key: groups[key] for key in ('groups', 'pieces_in_groups', 'full', 'partial', 'none')},
            'touching_pairs': {'before': totals['touching_pairs_before'], 'after': totals['touching_pairs_after']},
            'party_wall_pairs_ge_20m2': {'before': party['before'], 'after': party['after'], 'equal': party['before'] == party['after'],
                                         'strict_before': party['strict_before'], 'strict_after': party['strict_after'],
                                         'strict_equal': party['strict_before'] == party['strict_after'],
                                         'inside_a_feature': party['inside_a_feature'], 'inside_a_feature_strict': party['inside_a_feature_strict'],
                                         'collapsed_by_duplicates': party['collapsed_by_duplicates'], 'appeared': party['appeared'], 'gone': party['gone'],
                                         'definition': 'both sides >= 20 m2, gap <= 0.5 m, cover of the smaller < 0.05; strict = same-mask pairs '
                                                       'excluded (the census); inside_a_feature = input party-wall pairs whose pieces ended in one '
                                                       'feature (the NEVER check; strict must be 0); collapsed / appeared / gone explain before vs after'}}


def aggregate_sensitivity(rows):
    merges, refusals, totals = Counter(), Counter(), Counter()
    for row in rows:
        merges.update(row['merges'])
        refusals.update(row['refusals'])
        totals['multi_piece_features'] += row['multi_piece_features']
        totals['pieces_superseded'] += row['pieces_superseded']
        totals['bridge_area_m2'] += row['bridge_area_m2']
    return {'multi_piece_features': totals['multi_piece_features'], 'pieces_superseded': totals['pieces_superseded'],
            'merges': {'step1': merges['step1'], 'step2': merges['step2'], 'total': merges['step1']+merges['step2'],
                       **{rule: merges[rule] for rule in BUILDING_RULES}},
            'refusals': {reason: refusals[reason] for reason in BUILDING_REFUSALS if refusals[reason]},
            'bridge_area_m2': round(totals['bridge_area_m2'], 3)}


def build_metrics(results, scene_ids, version, sensitivity=None, design=BUILDING_MERGE, design_name='v1', reference=None):
    layers, checks, umbrellas = {}, {}, {}
    for layer in ('model', 'review'):
        layer_results = [results[s][layer] for s in scene_ids if layer in results[s]]
        layers[layer] = aggregate_layer(layer_results)
        total = Counter()
        for r in layer_results:
            total.update(r['checks'])
        checks[layer] = {**dict(total), 'scenes': len(layer_results), 'touch_eps_m2': design['touch_eps_m2'],
                         'confirmed_pieces_merged': total['confirmed_members'], 'decided_pieces_merged': total['decided_members'],
                         'party_wall_pairs_equal_every_scene': total['party_wall_pairs_unequal'] == 0,
                         'party_wall_pairs_inside_a_feature': total['party_wall_pairs_inside_a_feature'],
                         'party_wall_pairs_inside_a_feature_strict': total['party_wall_pairs_inside_a_feature_strict'],
                         'bridge_recompute_max_abs_diff_m2': round(max((abs(f['bridge_m2']-f['bridge_recomputed_m2'])
                                                                        for r in layer_results for f in r['features']), default=0.), 3)}
        umbrellas[layer] = [{'scene': r['scene'], **u} for r in layer_results for u in r['umbrellas']]
    named = {s: {layer: {'multi_piece_features': len(results[s][layer]['features']),
                         'features_with_named_object': sum(1 for f in results[s][layer]['features'] if f['named_object_member']),
                         'named_object': NAMED_OBJECTS[s]} for layer in results[s]} for s in NAMED_WINDOWS if s in results}
    vertices = [f['vertices'] for s in scene_ids for r in results[s].values() for f in r['features']]
    simplified = [f for s in scene_ids for r in results[s].values() for f in r['features'] if (f.get('simplified') or {}).get('applied')]
    carried = [(s, f['id'], f['carried_members']) for s in scene_ids for r in results[s].values() for f in r['features'] if f['carried_members']]
    comparison = compare_with_reference(results, reference) if reference else {}
    return {'version': version, 'computed_at': datetime.now(UTC).isoformat(timespec='seconds'),
            'design_name': design_name, 'design': design_json(design),
            **({'proposal': 'BUILDING_MERGE_V2: proposal after the 7 Oct by-eye tally, not approved; nothing promoted',
                'v2_flags': {k: design[k] for k in ('party_wall_guard_step1', 'step1_outside_gap_max_m', 'small_touch') if k in design}}
               if design_name != 'v1' else {}),
            'scenes': len(scene_ids),
            'scenes_without_review': [s for s in scene_ids if 'review' not in results[s]],
            'layers': layers, 'checks': checks, 'umbrellas': umbrellas,
            **comparison,
            'named_windows': {'layer': RENDER_LAYER, 'windows': named},
            'carried_member_features': {'note': 'rendered, not applied: counts only after the dated WINDOW / RULES line (card item 1)',
                                        'n': len(carried), 'features': [{'scene': s, 'id': i, 'carried_members': c} for s, i, c in carried]},
            'vertex_rule': {'building_vertices': BUILDING_RING_SIMPLIFY['vertices'], 'tolerance_px': BUILDING_RING_SIMPLIFY['tolerance_px'],
                            'features_over_200_vertices': sum(1 for v in vertices if v > 200), 'max_vertices': max(vertices, default=0),
                            'features_simplified': len(simplified),
                            'simplified_max_abs_area_change_pct': round(max((abs(f['simplified']['area_change_pct'] or 0.) for f in simplified), default=0.), 3)},
            'review_layer_features_to_mark': sum(len(results[s][RENDER_LAYER]['features']) for s in scene_ids if RENDER_LAYER in results[s]),
            'sensitivity': {'note': 'counts only, nothing rendered or applied; every other guard as approved',
                            'variants': {variant: {'changed': SENSITIVITY[variant],
                                                   'layers': {layer: aggregate_sensitivity(rows) for layer, rows in per_layer.items()}}
                                         for variant, per_layer in (sensitivity or {}).items()}},
            'note': 'counts and geometry only, never accuracy; nothing promoted; data/scenes untouched'}


# ---------------------------------------------------------------------------------------------------------------- render

def load_font(size):
    for candidate in ('/System/Library/Fonts/Menlo.ttc', '/System/Library/Fonts/Helvetica.ttc', '/System/Library/Fonts/Supplemental/Arial.ttf'):
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


def scale_bar_metres(crop_width_px, spacing, scale):
    for metres in (20., 10., 5., 2., 1., .5):
        if metres/spacing*scale <= crop_width_px*.3:
            return metres
    return .5


def place_label(boxes, x, y, w, h, width, height, step=16, tries=8):
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


def draw_raw_panel(raw, fpoly, members, tf):
    """The raw panel: the annotated panel's crop at its scale, before any drawing, with 1 px outlines only. The merged feature in
    white first, then each member in its own colour on top, so every member's whole outline shows and the white remains where the
    feature departs from its members (bridge, simplification). No fill, no label, no scale bar over the pixels. members: [(polygon,
    colour)]."""
    draw = ImageDraw.Draw(raw)
    draw_outline(draw, fpoly, FEATURE_LINE, 1, tf)
    for poly, colour in members:
        draw_outline(draw, poly, colour, 1, tf)
    return raw


def rule_text(feature):
    return '+'.join(feature['rules'])


def figures_text(feature):
    """The figures behind a feature's rule(s): cover, gap, strip share in which occluder, the smaller side, bridge and absorbed."""
    parts = []
    if feature['cover_max'] is not None and ('cover' in feature['rules'] or feature['cover_max'] > 0):
        parts.append(f"cover {feature['cover_max']:.2f}")
    if feature['gap_max_m'] is not None:
        parts.append(f"gap {feature['gap_max_m']:.2f} m")
    for occ in feature['strip_occluders']:
        parts.append(f"strip {occ['share']:.2f} in {occ['kind']} {occ['id'][:8]}")
    if 'small_touch' in feature['rules'] and feature['smaller_min_m2'] is not None:
        parts.append(f"smaller {feature['smaller_min_m2']:.1f} m2")
    parts.append(f"bridge {feature['bridge_recomputed_m2']:.2f} m2")
    parts.append(f"absorbed {feature['absorbed_m2']:.2f} m2")
    return ' · '.join(parts)


def feature_title(index, scene_id, feature):
    return f"{index}. {scene_id} {feature['id']} building {rule_text(feature)} · {feature['n_members']} pieces {feature['pieces_union_m2']:.1f} -> {feature['area_m2']:.1f} m2"


def crop_box(bounds, margin_px, width, height, min_side=MIN_SIDE_PX):
    """run-v2 crop: the feature's bounds + margin, widened to at least min_side native pixels a side around the feature's centre,
    then shifted into the image (padding into it) and clipped only where the image itself is smaller. Returns ((x0, y0, x1, y1),
    notes): notes['edges'] names the image sides the FEATURE touches (within 1 px: the piece is cut by the frame, said on the title),
    notes['margin_short'] the sides where the image ends inside the margin (said on the info line), and notes['shifted'] is set when
    the box had to move to stay inside the image."""
    x0, y0, x1, y1 = bounds
    cx, cy = (x0+x1)/2., (y0+y1)/2.
    w, h = max(x1-x0+2*margin_px, float(min_side)), max(y1-y0+2*margin_px, float(min_side))
    bx0, by0 = cx-w/2., cy-h/2.
    bx1, by1 = bx0+w, by0+h
    shifted = False
    if bx0 < 0:
        bx1, bx0, shifted = bx1-bx0, 0., True
    if by0 < 0:
        by1, by0, shifted = by1-by0, 0., True
    if bx1 > width:
        bx0, bx1, shifted = max(0., bx0-(bx1-width)), float(width), True
    if by1 > height:
        by0, by1, shifted = max(0., by0-(by1-height)), float(height), True
    at = [side for side, near in (('left', x0 <= 1.), ('top', y0 <= 1.), ('right', x1 >= width-1.), ('bottom', y1 >= height-1.)) if near]
    short = [side for side, near in (('left', x0 < margin_px), ('top', y0 < margin_px), ('right', x1 > width-margin_px),
                                     ('bottom', y1 > height-margin_px)) if near and side not in at]
    return (int(math.floor(bx0)), int(math.floor(by0)), int(math.ceil(bx1)), int(math.ceil(by1))), {'edges': at, 'margin_short': short,
                                                                                                     'shifted': shifted}


def inherit_text(inherit, rule=None):
    """The run-v1 lineage of a v2 feature for the render's info line (no mark here: the skeptic's marks are hints on the sheet only)."""
    if not inherit:
        return ''
    if inherit['status'] == 'identical':
        changed = f"; rule was {inherit['v1_rule']} in run-v1" if rule and inherit.get('v1_rule') and inherit['v1_rule'] != rule else ''
        return f" · = run-v1 #{inherit['v1_n']} (same members{changed})"
    if inherit['status'] == 'shrunk':
        return f" · shrunk from run-v1 #{inherit['v1_n']} (dropped {', '.join(i[:8] for i in inherit['dropped'])})"
    return ' · new in v2 (member set not in run-v1)'


def render_feature(image, scene_id, feature, index, objects, to_meters, review_revision, out_path, marks_name=None, inherit=None,
                   design=BUILDING_MERGE, raw_panel=False):
    """One crop: feature bounds + MARGIN_M at native pixels, padded to >= MIN_SIDE_PX a side into the image (integer nearest upscale so
    the crop reads): members outlined in their own colour with 8-char ids, absorbed surface light magenta at 25 % with its outline,
    bridge (strip) magenta at 60 %, feature outline bold white, occluders green (the strip's occluder thick + labelled), decided
    buildings yellow (* confirmed, ~ needs_adjustment), other live candidates grey, a scale bar, the title with the rule; a piece
    within MARGIN_M of the image edge says so on the title. raw_panel: the same crop at the same scale to the LEFT of that panel,
    1 px outlines only and no fill (draw_raw_panel), captioned RAW_CAPTION above it; the annotated panel is drawn as without it."""
    spacing = pixel_spacing(to_meters)
    fpoly = pixel_polygon(feature['object'])
    m = MARGIN_M/spacing
    (cx0, cy0, cx1, cy1), edge = crop_box(fpoly.bounds, m, image.width, image.height)
    scale = int(min(MAX_UPSCALE, max(1, math.ceil(MIN_CROP_PX/max(cx1-cx0, cy1-cy0)))))
    canvas = image.crop((cx0, cy0, cx1, cy1)).resize(((cx1-cx0)*scale, (cy1-cy0)*scale), Image.NEAREST)
    raw = canvas.copy() if raw_panel else None                                         # the raw panel's pixels: taken before any drawing
    font, small = load_font(14), load_font(12)
    member_ids = [mm['id'] for mm in feature['members']]
    colours = {mid: MEMBER_PALETTE[i % len(MEMBER_PALETTE)] for i, mid in enumerate(member_ids)}

    def tf(x, y):
        return ((x-cx0)*scale, (y-cy0)*scale)
    view = box(cx0, cy0, cx1, cy1)
    members = set(member_ids)
    by_id = {o['id']: o for o in objects}
    in_view = [(o, p) for o in objects if o['id'] not in members and o.get('status') not in ('rejected', 'superseded')
               and (p := pixel_polygon(o)) is not None and p.intersects(view)]
    buildings_in_view = [(o, p) for o, p in in_view if o.get('kind') == KIND]
    occluders_in_view = [(o, p) for o, p in in_view if o.get('kind') in design['occluder_kinds']]
    strip_ids = {occ['id'] for occ in feature['strip_occluders']}
    draw = ImageDraw.Draw(canvas)
    for o, p in buildings_in_view:                                                    # 1. other live building candidates, thin grey
        if _building_joinable(o, design):
            draw_outline(draw, p, OTHER_BUILDING, 1, tf)
    overlay = Image.new('RGBA', canvas.size, (0, 0, 0, 0))                            # 2. absorbed (25 %) and bridge (60 %) magenta
    for part in feature['absorbed']['pixels']:
        if len(part['ring']) >= 3:
            fill_geom(overlay, Polygon(part['ring'], part['holes']), ABSORBED_FILL, tf)
    for part in feature['bridge']['pixels']:
        if len(part['ring']) >= 3:
            fill_geom(overlay, Polygon(part['ring'], part['holes']), BRIDGE_FILL, tf)
    canvas = Image.alpha_composite(canvas.convert('RGBA'), overlay).convert('RGB')
    draw = ImageDraw.Draw(canvas)
    for part in feature['absorbed']['pixels']:                                        # the absorbed surface keeps an outline (faint fill)
        if len(part['ring']) >= 3:
            draw_outline(draw, Polygon(part['ring'], part['holes']), ABSORBED_LINE, 1, tf)
    for part in feature['bridge']['pixels']:
        if len(part['ring']) >= 3:
            draw_outline(draw, Polygon(part['ring'], part['holes']), BRIDGE_LINE, 1, tf)
    for o, p in occluders_in_view:                                                    # 3. occluders green; the strip's occluder thick
        draw_outline(draw, p, OCCLUDER, 3 if o['id'] in strip_ids else 1, tf, dotted=o['id'] not in strip_ids)
    draw_outline(draw, fpoly, (0, 0, 0), 6, tf)                                       # 4. feature outline, bold white with halo
    draw_outline(draw, fpoly, FEATURE_LINE, 3, tf)
    for mid in member_ids:                                                            # 5. members in their own colour
        p = pixel_polygon(by_id[mid])
        if p is not None:
            draw_outline(draw, p, colours[mid], 2, tf)
    for o, p in buildings_in_view:                                                    # 6. decided buildings, yellow
        if o.get('status') in ('confirmed', 'needs_adjustment') and not _building_joinable(o, design):
            draw_outline(draw, p, DECIDED, 2, tf, dotted=o['status'] == 'needs_adjustment')
    boxes = []

    def place(lx, ly, text):
        w, h = draw.textlength(text, font=small), small.size+2
        return place_label(boxes, lx, ly, w, h, canvas.width, canvas.height, step=small.size+4)
    for mm in feature['members']:                                                     # 7. labels: member ids (+ m2, carried flag)
        p = pixel_polygon(by_id[mm['id']])
        if p is None:
            continue
        text = f"{mm['id'][:8]} {mm['area_m2']:.1f}m2" + (' carried' if mm['carried'] else '') + ('*' if mm['largest'] else '')
        bx0, by0, bx1, by1 = p.bounds
        if (bx1-bx0)*scale < 70 or (by1-by0)*scale < 24:
            lx, ly = tf(bx1, by0)
            lx, ly = lx+4, ly-small.size
        else:
            lx, ly = tf(*p.representative_point().coords[0])
            lx, ly = lx-36, ly-7
        halo_text(draw, place(lx, ly, text), text, small, colours[mm['id']])
    for o, p in occluders_in_view:
        if o['id'] in strip_ids:
            text = f"{o['kind']} {o['id'][:8]} (strip inside)"
            lx, ly = tf(*p.representative_point().coords[0])
            halo_text(draw, place(lx-30, ly+8, text), text, small, OCCLUDER)
    for o, p in buildings_in_view:
        if o.get('status') in ('confirmed', 'needs_adjustment') and not _building_joinable(o, design):
            text = f"{'*' if o['status'] == 'confirmed' else '~'} {o['id'][:8]} {o['status']}"
            lx, ly = tf(*p.representative_point().coords[0])
            halo_text(draw, place(lx-30, ly+8, text), text, small, DECIDED)
    metres = scale_bar_metres(canvas.width, spacing, scale)                          # 8. scale bar
    bar = metres/spacing*scale
    bx, by = 12, canvas.height-14
    draw.line([(bx, by), (bx+bar, by)], fill=(0, 0, 0), width=6)
    draw.line([(bx, by), (bx+bar, by)], fill=(255, 255, 255), width=2)
    halo_text(draw, (bx, by-small.size-6), f"{metres:g} m", small, (255, 255, 255))
    if raw is not None:                                                               # 9. the raw panel (left): 1 px outlines, no fill
        raw = draw_raw_panel(raw, fpoly, [(p, colours[mid]) for mid in member_ids if (p := pixel_polygon(by_id[mid])) is not None], tf)
    panels_w = canvas.width+(raw.width+PANEL_GAP if raw is not None else 0)
    width = max(panels_w, 700)
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    badge = f"{index}"
    bw = int(probe.textlength(badge, font=font))+12
    title = feature_title(index, scene_id, feature)
    if edge['edges']:
        title += f" · PIECE AT THE IMAGE EDGE ({', '.join(edge['edges'])}: cut by the frame; crop padded into the image)"
    carried = f" · CARRIED member(s) {', '.join(i[:8] for i in feature['carried_members'])}: rendered, not applied (card item 1)" if feature['carried_members'] else ''
    lines = [f"members {', '.join(i[:8] for i in member_ids)} · {figures_text(feature)} · {RENDER_LAYER} layer rev {review_revision} "
             f"· {spacing:.3f} m/px x{scale} native · crop {cx1-cx0}x{cy1-cy0} px"
             + (' (shifted into the image)' if edge['shifted'] else '')
             + (f" (margin under {MARGIN_M:g} m at {', '.join(edge['margin_short'])}: the image ends)" if edge['margin_short'] else '')
             + f" · {feature['vertices']} vertices"
             + (' (simplified 2 px)' if (feature.get('simplified') or {}).get('applied') else '') + carried + inherit_text(inherit, rule_text(feature)),
             'coloured outline + id = member piece (* = largest) · light magenta fill 25 % + outline = absorbed (smaller members outside '
             'the largest) · magenta 60 % = bridge (strip added between pieces) · white bold = merged feature · green = tree / vegetation / '
             'solar / car (thick = the strip lies inside it) · yellow (* / ~) = reviewer confirmed / needs_adjustment, never merged · '
             'grey = other candidates'
             + (' · LEFT panel (raw) = the same crop at the same scale with no fill and no labels: 1 px outlines only, white = merged '
                'feature, member colours on top; judge the material under each member there' if raw is not None else '')]
    title_lines = wrap_lines(probe, [title], font, width-bw-14)
    lines = wrap_lines(probe, lines, small, width-12)
    header = 6+len(title_lines)*(font.size+4)+len(lines)*(small.size+4)+4
    caption_h = small.size+6 if raw is not None else 0
    tile = Image.new('RGB', (width, header+caption_h+canvas.height+6), BACKGROUND)
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
    x0 = (width-panels_w)//2
    if raw is not None:
        draw.text((x0, header+1), RAW_CAPTION, fill=(235, 235, 235), font=small)
        tile.paste(raw, (x0, header+caption_h))
        x0 += raw.width+PANEL_GAP
    tile.paste(canvas, (x0, header+caption_h))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tile.save(out_path)
    return tile


def contact_sheet(tiles, scene_id, review_revision, out_path, marks_name=None, panels=1):
    """All merged features of one window, numbered, packed into rows (tiles wider or taller than SHEET_CELL_MAX shrink for the sheet
    only, the width read per panel: a raw + annotated tile is panels=2; each cell carries its number in a band above the panel)."""
    marks_name = marks_name or f'MARKS-{MARKS_DATE}.md'
    font, big = load_font(15), load_font(28)
    band = big.size+16
    scaled = []
    for n, tile in tiles:
        s = max(SHEET_MIN_SCALE, min(1., SHEET_CELL_MAX/max(tile.width/panels, tile.height)))
        if s < 1.:
            tile = tile.resize((max(1, int(tile.width*s)), max(1, int(tile.height*s))), Image.BILINEAR)
        scaled.append((n, tile))
    rows, row = [], []
    for n, tile in scaled:
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
    draw.text((8, 7), f"{scene_id} · {RENDER_LAYER} layer rev {review_revision} · {len(scaled)} joined feature(s) #{numbers} "
                      f"· mark each right / wrong / unclear in {marks_name}", fill=(255, 255, 255), font=font)
    y = header
    for r in rows:
        x = 8
        for n, tile in r:
            label = f"#{n}"
            bw = int(draw.textlength(label, font=big))+16
            draw.rectangle([x, y+2, x+bw, y+band-4], fill=BRIDGE_LINE)
            draw.text((x+8, y+6), label, fill=(255, 255, 255), font=big)
            draw.text((x+bw+10, y+(band-font.size)//2), f"feature {n} · mark in {marks_name}", fill=(235, 235, 235), font=font)
            sheet.paste(tile, (x, y+band))
            x += tile.width+8
        y += max(t.height for _, t in r)+band+8
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


def render_window(scene_id, result, objects, image_path, to_meters, review_revision, renders_dir, start_index, marks_name=None,
                  inherit_of=None, design=BUILDING_MERGE, design_name='v1', raw_panel=False):
    """Renders every review-layer feature of one window; returns (rows for MARKS, next index, sheet path or no-join note). Order:
    features identical to a run-v1 feature first, in run-v1's line order (inherit_of), then the window's other features (the
    named object's first, then by id); without a reference the run-v1 order (named first, then by id)."""
    inherit_of = inherit_of or {}

    def order(f):
        inh = inherit_of.get((scene_id, f['id']))
        if inh and inh['status'] == 'identical':
            return (0, inh['v1_n'], f['id'])
        return (1, -bool(f['named_object_member']), f['id'])
    features = sorted(result['features'], key=order)
    label = "the card's defaults" if design_name == 'v1' else f'the {design_name} proposal (not approved)'
    if not features:
        refusals = Counter(r['reason'] for r in result['refusals'])
        decided = sum(1 for o in objects if o.get('kind') == KIND and o.get('status') in ('confirmed', 'needs_adjustment'))
        note = (f"{scene_id} {RENDER_LAYER} layer rev {review_revision}: no join under {label} "
                f"(refusals {dict(sorted(refusals.items())) or 'none'}; umbrellas {len(result['umbrellas'])}; "
                f"{decided} reviewer-decided building pieces untouched); nothing to mark.")
        note_path = renders_dir/f'{scene_id}.no-join.txt'
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(note+'\n')
        return [], start_index, note_path
    image = Image.open(image_path).convert('RGB')
    tiles, rows = [], []
    index = start_index
    for feature in features:
        inherit = inherit_of.get((scene_id, feature['id']))
        path = renders_dir/scene_id/f"feature-{index}-{feature['id']}.png"
        tiles.append((index, render_feature(image, scene_id, feature, index, objects, to_meters, review_revision, path,
                                            marks_name=marks_name, inherit=inherit, design=design, raw_panel=raw_panel)))
        rows.append({'n': index, 'scene': scene_id, 'feature': feature, 'render': path, 'inherit': inherit})
        index += 1
    sheet = contact_sheet(tiles, scene_id, review_revision, renders_dir/f'{scene_id}.sheet.png', marks_name, panels=2 if raw_panel else 1)
    return rows, index, sheet


def write_marks(path, windows, metrics, out=OUT, design=BUILDING_MERGE, design_name='v1'):
    """windows: [(scene_id, rows, sheet_or_note_path, review_revision)] with the six named windows first. Under a v2 design the
    header also says that the skeptic's run-v1 marks are hints only and lists what v2 changed, one sentence each; a line identical
    to a run-v1 feature carries 'v1 #n' and the hint in brackets."""
    def rel(p):
        p = Path(p).resolve()
        return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)
    total = sum(len(rows) for _, rows, _, _ in windows)
    with_joins = [(s, len(rows)) for s, rows, _, _ in windows if rows]
    review = metrics['layers']['review']
    checks = metrics['checks']
    d = design
    v2 = design_name != 'v1'
    small_touch_on = bool(d.get('small_touch', True))
    step1_gap = float(d.get('step1_outside_gap_max_m', d['touch_m']))
    lines = [f"# Roof-join {Path(out).name} marks sheet, {MARKS_DATE}" + (f" — {design_name} PROPOSAL (not approved)" if v2 else ''), '',
             '**How to answer in chat:** one line is enough, with the global line numbers below (the same number sits on the render '
             "badge, in the file name and on the window's contact sheet): `right: 1, 4, 9` / `wrong: 2` / `unclear: 3`, or `all right` "
             '/ `all wrong`. A number you do not mention stays unmarked. "right" = the white outline is one roof surface (or one '
             'building) and the pieces inside it belong together; "wrong" = it joins separate roofs / buildings or non-roof surface; '
             '"unclear" = the imagery does not say. Nothing is applied by your answer alone: the tally gates promotion (card item 1), '
             'then dated RULES + DESIGN entries and a windows-only re-extract.', '']
    if v2:
        comparison = metrics.get('v1_features') or {}
        totals = comparison.get('totals_by_status_and_mark') or {}

        def by_mark(status):
            t = totals.get(status) or {}
            return f"{sum(t.values())} (right {t.get('right', 0)} / wrong {t.get('wrong', 0)} / unclear {t.get('unclear', 0)})"
        lines += ["**This is a proposal beside the approved defaults, nothing promoted.** The design is scene_extract.BUILDING_MERGE_V2 "
                  "(dated 2026-10-07: proposal after the 7 Oct by-eye tally, not approved); the card's bold defaults (BUILDING_MERGE, run-v1) "
                  "keep their values and the join is still not called from extraction. **The skeptic's run-v1 marks printed in brackets "
                  "[HINT ...] are hints only**: they were made on run-v1's renders (near-opaque fill, some crops cut at the frame edge) by "
                  "a by-eye pass, not by you; a feature identical to a run-v1 feature (same member set) keeps its run-v1 line number as "
                  "'v1 #n' and comes first in its window in run-v1 order; shrunk (a subset of a run-v1 member set) and new features follow. "
                  "Your mark on this sheet is the one that counts.",
                  '', '**What v2 changed, one sentence each:**', *[f"- {s}" for s in V2_CHANGES], '',
                  f"**Run-v1 -> v2 (review layer):** of run-v1's {comparison.get('n', 0)} features, identical {by_mark('identical')}, shrunk "
                  f"{by_mark('shrunk')}, gone {by_mark('gone')}; v2 features with a member set not in run-v1: {(metrics.get('v2_new') or {}).get('n', 0)} "
                  f"({(metrics.get('v2_new') or {}).get('n_shrunk', 0)} shrunk from a run-v1 feature, {(metrics.get('v2_new') or {}).get('n_strict_new', 0)} new).",
                  '']
    lines += [f"Totals: {total} joined features to mark across {len(with_joins)} of {len(windows)} windows "
              f"({', '.join(f'{s} {n}' for s, n in with_joins) or 'none'}); {len(windows)-len(with_joins)} window(s) had no join in the "
              f"{RENDER_LAYER} layer. The six named windows of the card come first, in the card's order.",
              f"Layer: {RENDER_LAYER} (review.json objects; the model layer is counted in metrics.json only). Design ("
              + ('BUILDING_MERGE_V2, the proposal' if v2 else "BUILDING_MERGE, the card's bold defaults")
              + "): same-mask components first"
              + (f" (v2: not when both are >= {d['substantial_m2']:g} m2 with cover < {d['party_wall_cover_max']:g}, and not when a component "
                 f"wholly outside the other sits over {step1_gap:g} m away)" if v2 else '')
              + f"; then cover >= {d['cover_min']:g} of the smaller (dedupe gap included), "
              f"or the facing-edge strip inside ONE tree / vegetation / solar_panel / car object (share >= {d['strip_cover_min']:g}) touching "
              f"no third building"
              + (f", or touching within {d['touch_m']:g} m with the smaller under {d['small_m2']:g} m2" if small_touch_on
                 else f" (v2: the 'touching within {d['touch_m']:g} m with the smaller under {d['small_m2']:g} m2' rule is OFF)")
              + f"; never two sides >= {d['substantial_m2']:g} m2 that only touch (cover < {d['party_wall_cover_max']:g}); an umbrella covering >= {d['umbrella_pieces']} "
              f"pieces of >= {d['substantial_m2']:g} m2 is refused and listed; candidates only (a carried frame-cut target still on the carry's "
              f"needs_adjustment placeholder counts as a candidate), a decided piece blocks any bridge; result floor {d['result_floor_m2']:g} m2 "
              f"(feature AND largest member); rings over {BUILDING_RING_SIMPLIFY['vertices']} vertices simplified at "
              f"{BUILDING_RING_SIMPLIFY['tolerance_px']:g} px; the app's outline rules on every emitted ring.",
              f"Checks over all {metrics['scenes']} windows, both layers: confirmed pieces merged = "
             f"{sum(c['confirmed_pieces_merged'] for c in checks.values())}, decided pieces merged = "
             f"{sum(c['decided_pieces_merged'] for c in checks.values())}, bridges touching a decided object = "
             f"{sum(c['bridge_touches_decided'] for c in checks.values())}, bridges touching a third building = "
             f"{sum(c['bridge_touches_third_building'] for c in checks.values())} (ring-rounding slop, not a strip, on a decided object: "
             f"{sum(c['slop_touches_decided'] for c in checks.values())} feature(s), printed on the line); party-wall pairs >= 20 m2 that "
             f"ended inside one feature = {sum(c['party_wall_pairs_inside_a_feature_strict'] for c in checks.values())} (the NEVER check; "
             f"{sum(c['party_wall_pairs_inside_a_feature'] for c in checks.values())} counting the same-mask pairs step 1 re-joins); party-wall "
             f"pairs before / after (review layer) {review['party_wall_pairs_ge_20m2']['before']} / {review['party_wall_pairs_ge_20m2']['after']} "
             f"(the difference is bookkeeping, not a merged wall: {review['party_wall_pairs_ge_20m2']['collapsed_by_duplicates']} pairs collapsed when "
             f"duplicate masks became one feature, {review['party_wall_pairs_ge_20m2']['appeared']} appeared where a feature grew past 20 m2 or "
             f"within 0.5 m of a neighbour, {review['party_wall_pairs_ge_20m2']['gone']} gone). Review layer: {review['pieces_in']} building pieces, {review['joinable']} joinable, "
             f"{review['multi_piece_features']} features from {review['pieces_superseded']} pieces; merges same_mask {review['merges']['same_mask']} / "
             f"cover {review['merges']['cover']} / occluder_strip {review['merges']['occluder_strip']} / small_touch {review['merges']['small_touch']}; "
             f"refusals {review['refusals']}; umbrellas listed {review['umbrellas']}. Features with a CARRIED member: "
             f"{metrics['carried_member_features']['n']} over both layers (rendered, not applied: card item 1).",
             "Render key: coloured outline + 8-char id = member piece (* = the largest); light magenta fill (25 %) + outline = absorbed "
             "surface (the smaller members outside the largest); magenta (60 %) = bridge (the strip added between pieces); white bold = the "
             "merged feature; green = tree / vegetation / solar panel / car (thick + labelled when the strip lies inside it); yellow (* / ~) "
             "= your confirmed / needs_adjustment pieces, never merged; grey = other live candidates. Native pixels, integer upscale stated "
             "on the tile; crops padded to >= 200 px a side into the image, a piece within 4 m of the image edge says so on the title. "
             "Paths are relative to the repo root.", '']
    for scene_id, rows, sheet, revision in windows:
        named = f" · NAMED #{NAMED_WINDOWS.index(scene_id)+1} {NAMED_OBJECTS[scene_id][:8]}" if scene_id in NAMED_OBJECTS else ''
        if rows:
            lines.append(f"## {scene_id} — {len(rows)} feature(s) · review rev {revision}{named} · sheet {rel(sheet)}")
            lines.append('')
            for row in rows:
                f = row['feature']
                extras = []
                inh = row.get('inherit')
                if inh and inh['status'] == 'identical':
                    extras.append(f"v1 #{inh['v1_n']} [HINT: skeptic's run-v1 mark {inh['v1_mark'] or 'unmarked'}]"
                                  + (f" · RULE CHANGED: {inh['v1_rule']} in v1 -> {rule_text(f)} in v2 (same members)" if inh.get('rule_changed') else ''))
                elif inh and inh['status'] == 'shrunk':
                    extras.append(f"shrunk from v1 #{inh['v1_n']} (dropped {', '.join(i[:8] for i in inh['dropped'])}) "
                                  f"[HINT: skeptic's run-v1 mark on the LARGER set {inh['v1_mark'] or 'unmarked'}; not this feature]")
                elif inh:
                    extras.append('new in v2 (member set not in run-v1)')
                if f['named_object_member']:
                    extras.append(f"holds named object {f['named_object_member'][0][:8]}")
                if f['carried_members']:
                    extras.append(f"CARRIED {', '.join(i[:8] for i in f['carried_members'])} (rendered, not applied)")
                if f['bridge_third_buildings']:
                    extras.append('bridge touches third building '+', '.join(h['id'][:8] for h in f['bridge_third_buildings']))
                if f['bridge_decided']:
                    extras.append('bridge touches decided '+', '.join(h['id'][:8] for h in f['bridge_decided']))
                if f['slop_decided'] or f['slop_third_buildings']:
                    extras.append(f"rounding slop {f['slop_m2']:.2f} m2 on "+', '.join(f"{h['kind']} {h['id'][:8]} {h['overlap_m2']:.1e} m2"
                                                                                   for h in f['slop_decided']+f['slop_third_buildings']))
                if f['vertex_cap']:
                    extras.append(f"over the 200-vertex cap ({f['vertices']})")
                lines.append(f"{row['n']}. {scene_id} {f['id']} = {', '.join(m['id'][:8] for m in f['members'])} · rule {rule_text(f)} · "
                             f"{figures_text(f)} · pieces {f['pieces_union_m2']:.1f} -> {f['area_m2']:.1f} m2 · render {rel(row['render'])}"
                             + ''.join(f" · {t}" for t in extras) + " · [ right / wrong / unclear ]")
        else:
            lines.append(f"## {scene_id} — no join · review rev {revision}{named} · note {rel(sheet)}")
            lines.append('')
            lines.append(f"- {sheet.read_text().strip()}")
        lines.append('')
    if v2 and (metrics.get('v1_features') or {}).get('rows'):
        gone = [r for r in metrics['v1_features']['rows'] if r['status'] == 'gone']
        lines += [f"## Run-v1 features gone in v2 ({len(gone)}; nothing to mark, listed so the comparison is complete)", '']
        lines += [f"- v1 #{r['n']} {r['scene']} {r['id']} · rule {r['rule']} · skeptic {r['mark'] or 'unmarked'} · members "
                  f"{', '.join(i[:8] for i in r['members'])} · why: {r.get('why', '')}" for r in gone]
        lines.append('')
    lines += ['---', f"Generated by scripts/ml/experiments/roof_join_run.py at {metrics['computed_at']}; metrics in {rel(out/'metrics.json')}; "
                     f"per-scene results {rel(out)}/<scene>.<layer>.json. Nothing promoted; data/scenes untouched; no count moves."]
    path.write_text('\n'.join(lines)+'\n')
    return path


# ------------------------------------------------------------------------------------ the run-v1 reference (features, marks)

MARKS_LINE = re.compile(r'^(\d+)\. (\S+) ([0-9a-f]{16}) = ')
TALLY_ROW = re.compile(r'^\| (\d+) \| (\S+) \| (\S+) \| ([^|]*) \| (.*) \|\s*$')


def normalise_mark(raw):
    low = (raw or '').strip().lower()
    return 'right' if low.startswith('right') else 'wrong' if low.startswith('wrong') else 'unclear' if low.startswith('unclear') else None


def load_reference(v1_dir, tally_path=None, layer=RENDER_LAYER):
    """run-v1's features of one layer (member sets, rules, figures), the marks-sheet line number of each and the skeptic's by-eye mark
    (eye-tally.md rows `| n | scene | rule | by eye | note |`). Read-only; returns None when the directory has no layer files."""
    v1_dir = Path(v1_dir)
    features = {}
    for path in sorted(v1_dir.glob(f'*.{layer}.json')):
        r = json.loads(path.read_text())
        for f in r['features']:
            features[(r['scene'], f['id'])] = {
                'scene': r['scene'], 'id': f['id'], 'members': sorted(m['id'] for m in f['members']), 'members_full': f['members'],
                'largest_member': f['largest_member'], 'rules': f['rules'], 'rule': '+'.join(f['rules']), 'n': None, 'mark': None,
                'mark_raw': None, 'note': None, 'tally_rule': None, 'absorbed_m2': f['absorbed_m2'], 'gap_max_m': f['gap_max_m'],
                'cover_max': f['cover_max'], 'smaller_min_m2': f['smaller_min_m2'], 'area_m2': f['area_m2'],
                'pieces_union_m2': f['pieces_union_m2'], 'bridge_m2': f['bridge_recomputed_m2'], 'carried_members': f['carried_members'],
                'pixel_to_meters': r['pixel_to_meters'], 'review_revision': r.get('review_revision')}
    if not features:
        return None
    marks = sorted(v1_dir.glob('MARKS-*.md'))
    if marks:
        for line in marks[0].read_text().splitlines():
            m = MARKS_LINE.match(line)
            if m and (m.group(2), m.group(3)) in features:
                features[(m.group(2), m.group(3))]['n'] = int(m.group(1))
    by_n = {f['n']: f for f in features.values() if f['n'] is not None}
    tally_path = Path(tally_path) if tally_path else None
    if tally_path and tally_path.exists():
        for line in tally_path.read_text().splitlines():
            m = TALLY_ROW.match(line)
            if m and int(m.group(1)) in by_n and by_n[int(m.group(1))]['scene'] == m.group(2):
                by_n[int(m.group(1))].update({'tally_rule': m.group(3), 'mark_raw': m.group(4).strip(), 'mark': normalise_mark(m.group(4)),
                                              'note': m.group(5).strip()})
    return {'dir': str(v1_dir), 'marks_sheet': str(marks[0]) if marks else None, 'tally': str(tally_path) if tally_path else None,
            'layer': layer, 'features': features,
            'marks_total': dict(Counter(f['mark'] or 'unmarked' for f in features.values()))}


def compare_with_reference(results, reference, layer=RENDER_LAYER):
    """'v1_features': every reference feature identical (same member set in this run), shrunk (a proper subset of its members is a
    feature here) or gone, with the skeptic's mark; 'v2_new': features of this run whose member set is not a reference member set
    (shrunk from a reference feature, or new); 'inherit_of': {(scene, feature id): lineage} for the renders and the marks sheet.
    Counts and member sets only."""
    here = {(s, f['id']): {'members': set(m['id'] for m in f['members']), 'rules': f['rules'], 'area_m2': f['area_m2'],
                           'refusals': results[s][layer]['refusals']}
            for s in results for f in results[s].get(layer, {}).get('features', [])}
    rows, inherit_of = [], {}
    for key, ref in sorted(reference['features'].items(), key=lambda kv: (kv[1]['n'] if kv[1]['n'] is not None else 10**6, kv[0])):
        members = set(ref['members'])
        same = [k for k, v in here.items() if k[0] == key[0] and v['members'] == members]
        subs = [k for k, v in here.items() if k[0] == key[0] and v['members'] < members]
        ran = key[0] in results and layer in results[key[0]]
        status = 'identical' if same else 'shrunk' if subs else 'gone' if ran else 'not_run'
        kept = set().union(*(here[k]['members'] for k in subs)) if subs else set()
        dropped = sorted(members-kept) if status != 'identical' else []
        # why a member set is no longer whole: the refusal reasons this run recorded on pairs inside the reference member set
        refusals = results[key[0]][layer]['refusals'] if key[0] in results and layer in results[key[0]] else []
        inside = Counter(r['reason'] for r in refusals if r.get('a_ids') is not None
                         and set(r['a_ids']) | set(r['b_ids']) <= members)
        row = {'n': ref['n'], 'scene': key[0], 'id': key[1], 'rule': ref['rule'], 'tally_rule': ref['tally_rule'], 'mark': ref['mark'],
               'mark_raw': ref['mark_raw'], 'note': ref['note'], 'status': status, 'members': ref['members'], 'members_dropped': dropped,
               'v2_features': [{'id': k[1], 'members': sorted(here[k]['members']), 'rules': here[k]['rules'], 'area_m2': here[k]['area_m2']}
                               for k in same+subs],
               'why': ', '.join(f'{reason} {n}' for reason, n in sorted(inside.items())) if status != 'identical' else ''}
        rows.append(row)
        for k in same:
            inherit_of[k] = {'status': 'identical', 'v1_n': ref['n'], 'v1_id': key[1], 'v1_mark': ref['mark'], 'v1_rule': ref['rule'],
                             'v2_rule': '+'.join(here[k]['rules']), 'rule_changed': ref['rule'] != '+'.join(here[k]['rules']), 'dropped': []}
        for k in subs:
            inherit_of.setdefault(k, {'status': 'shrunk', 'v1_n': ref['n'], 'v1_id': key[1], 'v1_mark': ref['mark'], 'v1_rule': ref['rule'],
                                      'v2_rule': '+'.join(here[k]['rules']), 'dropped': sorted(members-here[k]['members'])})
    totals = {status: dict(Counter(r['mark'] or 'unmarked' for r in rows if r['status'] == status))
              for status in ('identical', 'shrunk', 'gone', 'not_run')}
    for row in rows:                                   # an identical member set may have joined under another rule this run
        row['rule_changed'] = [f"{row['rule']} -> {'+'.join(f['rules'])}" for f in row['v2_features'] if '+'.join(f['rules']) != row['rule']] \
            if row['status'] == 'identical' else []
    new = []
    for k, v in sorted(here.items()):
        if k in inherit_of and inherit_of[k]['status'] == 'identical':
            continue
        inh = inherit_of.get(k)
        if inh is None:
            inherit_of[k] = inh = {'status': 'new', 'v1_n': None, 'v1_id': None, 'v1_mark': None, 'v1_rule': None,
                                   'v2_rule': '+'.join(v['rules']), 'dropped': []}
        new.append({'scene': k[0], 'id': k[1], 'members': sorted(v['members']), 'rules': v['rules'], 'area_m2': v['area_m2'],
                    'relation': f"shrunk from v1 #{inh['v1_n']}" if inh['status'] == 'shrunk' else 'new member set', 'v1_n': inh['v1_n'],
                    'v1_mark_on_larger_set': inh['v1_mark'] if inh['status'] == 'shrunk' else None})
    by_rule = {}
    for status in ('identical', 'shrunk', 'gone', 'not_run'):
        by_rule[status] = {rule: dict(Counter(r['mark'] or 'unmarked' for r in rows if r['status'] == status and (r['tally_rule'] or r['rule']) == rule))
                           for rule in sorted({r['tally_rule'] or r['rule'] for r in rows if r['status'] == status})}
    return {'v1_features': {'reference': reference['dir'], 'marks_sheet': reference['marks_sheet'], 'tally': reference['tally'], 'layer': layer,
                            'n': len(rows), 'totals_by_status': {s: sum(t.values()) for s, t in totals.items()},
                            'identical_with_rule_changed': [{'n': r['n'], 'scene': r['scene'], 'id': r['id'], 'change': r['rule_changed']}
                                                            for r in rows if r['rule_changed']],
                            'totals_by_status_and_mark': totals, 'by_rule_status_mark': by_rule,
                            'note': "the skeptic's marks are hints on run-v1's renders, not this run's; counts only", 'rows': rows},
            'v2_new': {'n': len(new), 'n_shrunk': sum(1 for r in new if r['v1_n'] is not None),
                       'n_strict_new': sum(1 for r in new if r['v1_n'] is None), 'rows': new},
            'inherit_of': {f'{s}:{i}': v for (s, i), v in sorted(inherit_of.items())}}


# ------------------------------------------------------------------------------------------- tone study (run-v1 features)

def srgb_to_lab(rgb):
    """sRGB (0-255, n x 3) -> CIELAB (D65, 2 deg)."""
    c = np.asarray(rgb, dtype=float)/255.
    lin = np.where(c <= .04045, c/12.92, ((c+.055)/1.055)**2.4)
    xyz = lin @ np.array([[.4124564, .3575761, .1804375], [.2126729, .7151522, .0721750], [.0193339, .1191920, .9503041]]).T
    xyz = xyz/np.array([.95047, 1., 1.08883])
    f = np.where(xyz > (6./29.)**3, np.cbrt(xyz), xyz/(3.*(6./29.)**2)+4./29.)
    return np.c_[116.*f[:, 1]-16., 500.*(f[:, 0]-f[:, 1]), 200.*(f[:, 1]-f[:, 2])]


def pixels_inside(array, poly, erode_px=0.):
    """The image pixels (n x 3) whose centres fall inside poly (pixel coordinates), poly eroded by erode_px where that leaves area.
    Returns (pixels, eroded: bool)."""
    g = poly.buffer(-erode_px) if erode_px else poly
    eroded = erode_px > 0 and not g.is_empty and g.area > 0
    if not eroded:
        g = poly
    height, width = array.shape[:2]
    x0, y0, x1, y1 = g.bounds
    bx0, by0 = max(0, int(math.floor(x0))), max(0, int(math.floor(y0)))
    bx1, by1 = min(width, int(math.ceil(x1))+1), min(height, int(math.ceil(y1))+1)
    if bx1 <= bx0 or by1 <= by0:
        return np.zeros((0, 3), dtype=np.uint8), eroded
    mask = Image.new('L', (bx1-bx0, by1-by0), 0)
    draw = ImageDraw.Draw(mask)
    for part in iter_polygons(g):
        draw.polygon([(x-bx0, y-by0) for x, y in part.exterior.coords[:-1]], fill=255)
        for hole in part.interiors:
            draw.polygon([(x-bx0, y-by0) for x, y in hole.coords[:-1]], fill=0)
    sel = np.asarray(mask) > 0
    return array[by0:by1, bx0:bx1][sel], eroded


def best_threshold(rows, value='delta_e', keep=None):
    """The single deltaE bar that best separates the skeptic's right from wrong marks (unclear ignored): a feature is kept when
    value <= t (and keep(row), when given); score = right kept + wrong refused; ties -> fewer wrong kept, then the larger t.
    Returns the bar with its counts, the sweep at round bars and the marked counts."""
    marked = [r for r in rows if r['mark'] in ('right', 'wrong') and r.get(value) is not None]
    n_right = sum(1 for r in marked if r['mark'] == 'right')
    n_wrong = len(marked)-n_right
    if not marked:
        return {'n_right': 0, 'n_wrong': 0, 'best': None, 'sweep': []}
    values = sorted({round(float(r[value]), 3) for r in marked})
    candidates = [values[0]-.001, *((a+b)/2. for a, b in zip(values, values[1:], strict=False)), values[-1]+.001]

    def counts(t):
        kept = [r for r in marked if r[value] <= t and (keep(r) if keep else True)]
        right_kept = sum(1 for r in kept if r['mark'] == 'right')
        wrong_kept = sum(1 for r in kept if r['mark'] == 'wrong')
        return {'t': round(float(t), 2), 'right_kept': right_kept, 'right_lost': n_right-right_kept, 'wrong_refused': n_wrong-wrong_kept,
                'wrong_kept': wrong_kept, 'score': right_kept+(n_wrong-wrong_kept), 'of': len(marked)}
    best = None
    for t in candidates:
        c = counts(t)
        if best is None or (c['score'], -c['wrong_kept'], c['t']) > (best['score'], -best['wrong_kept'], best['t']):
            best = c
    return {'n_right': n_right, 'n_wrong': n_wrong, 'best': best, 'sweep': [counts(t) for t in (5., 10., 15., 20., 25., 30., 40.)]}


def tone_study(reference, out_dir, scenes_root=SCENES, erode_px=1.):
    """For every reference (run-v1) review feature: median RGB and CIELAB of the frame pixels inside the smaller member (eroded 1 px)
    and inside the largest member, the deltaE76 between them, the absorbed share and gap, and the skeptic's mark; the best deltaE bar
    (and deltaE AND cover > 0) by right-vs-wrong separation for the small_touch features and for the two reference bands. Read-only on
    imagery; writes <out_dir>/small-touch-tone.{json,md}. Numbers only, no accuracy claim."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    images, objects_of = {}, {}
    rows = []
    for _key, ref in sorted(reference['features'].items(), key=lambda kv: (kv[1]['n'] if kv[1]['n'] is not None else 10**6, kv[0])):
        scene_id = ref['scene']
        if scene_id not in images:
            latest, review, image_path = load_scene(scene_id, scenes_root)
            images[scene_id] = np.asarray(Image.open(image_path).convert('RGB'))
            objects_of[scene_id] = {o['id']: o for o in (review or latest)['objects']}
        array, by_id = images[scene_id], objects_of[scene_id]
        polys = {mid: pixel_polygon(by_id[mid]) for mid in ref['members'] if mid in by_id and pixel_polygon(by_id[mid]) is not None}
        if ref['largest_member'] not in polys or len(polys) < 2:
            rows.append({'n': ref['n'], 'scene': scene_id, 'id': ref['id'], 'rule': ref['tally_rule'] or ref['rule'], 'mark': ref['mark'],
                         'error': 'member ring missing in the live layer'})
            continue
        largest = polys[ref['largest_member']]
        others = {mid: p for mid, p in polys.items() if mid != ref['largest_member']}
        smaller_id = max(others, key=lambda mid: (others[mid].difference(largest).area, mid))     # the member with most surface outside the largest
        smaller = others[smaller_id]
        spacing = pixel_spacing(ref['pixel_to_meters'])
        small_px, small_eroded = pixels_inside(array, smaller, erode_px)
        large_px, _ = pixels_inside(array, largest, 0.)
        if len(small_px) < 3 or len(large_px) < 3:
            rows.append({'n': ref['n'], 'scene': scene_id, 'id': ref['id'], 'rule': ref['tally_rule'] or ref['rule'], 'mark': ref['mark'],
                         'error': f'too few pixels (smaller {len(small_px)}, largest {len(large_px)})'})
            continue
        small_rgb, large_rgb = np.median(small_px, axis=0), np.median(large_px, axis=0)
        small_lab, large_lab = np.median(srgb_to_lab(small_px), axis=0), np.median(srgb_to_lab(large_px), axis=0)
        smaller_m2 = smaller.area*spacing**2
        member = next((m for m in ref['members_full'] if m['id'] == smaller_id), None)
        rows.append({'n': ref['n'], 'scene': scene_id, 'id': ref['id'], 'rule': ref['tally_rule'] or ref['rule'], 'rules': ref['rules'],
                     'mark': ref['mark'], 'mark_raw': ref['mark_raw'], 'note': ref['note'],
                     'smaller_member': smaller_id, 'smaller_m2': round(float(smaller_m2), 2), 'smaller_carried': bool(member and member['carried']),
                     'largest_member': ref['largest_member'], 'largest_m2': round(float(largest.area*spacing**2), 2),
                     'n_members': len(ref['members']), 'absorbed_m2': ref['absorbed_m2'],
                     'absorbed_share': round(float(min(1., ref['absorbed_m2']/smaller_m2)), 3) if smaller_m2 else None,
                     'gap_m': ref['gap_max_m'], 'cover': ref['cover_max'] if ref['cover_max'] is not None else 0.,
                     'smaller_pixels': int(len(small_px)), 'smaller_eroded_1px': bool(small_eroded), 'largest_pixels': int(len(large_px)),
                     'smaller_rgb': [int(round(v)) for v in small_rgb], 'largest_rgb': [int(round(v)) for v in large_rgb],
                     'smaller_lab': [round(float(v), 1) for v in small_lab], 'largest_lab': [round(float(v), 1) for v in large_lab],
                     'delta_e': round(float(np.linalg.norm(small_lab-large_lab)), 2),
                     'delta_l': round(float(small_lab[0]-large_lab[0]), 1)})
    good = [r for r in rows if 'error' not in r]
    groups = {'small_touch': [r for r in good if r['rule'] == 'small_touch'],
              'cover+small_touch': [r for r in good if r['rule'] == 'cover+small_touch'],
              'cover_or_same_mask': [r for r in good if r['rule'] in ('cover', 'same_mask')]}

    def stats(values):
        values = sorted(values)
        return {'n': len(values), 'min': values[0], 'median': round(float(np.median(values)), 2), 'p75': round(float(np.percentile(values, 75)), 2),
                'max': values[-1]} if values else {'n': 0}
    summary = {}
    for name, group in groups.items():
        summary[name] = {'n': len(group), 'marks': dict(Counter(r['mark'] or 'unmarked' for r in group)),
                         'delta_e_by_mark': {mark: stats([r['delta_e'] for r in group if r['mark'] == mark]) for mark in ('right', 'wrong', 'unclear')},
                         'delta_e_all': stats([r['delta_e'] for r in group]),
                         'cover_gt_0': {mark: sum(1 for r in group if r['mark'] == mark and r['cover'] > 0) for mark in ('right', 'wrong', 'unclear')},
                         'threshold_delta_e': best_threshold(group),
                         'threshold_delta_e_and_cover_gt_0': best_threshold(group, keep=lambda r: r['cover'] > 0)}
    result = {'version': 'small-touch-tone-v1', 'computed_at': datetime.now(UTC).isoformat(timespec='seconds'),
              'reference': reference['dir'], 'tally': reference['tally'], 'erode_px': erode_px,
              'method': 'median RGB and CIELAB (D65) of frame pixels inside the smaller member (the member with most surface outside the '
                        'largest; eroded 1 px where that leaves area) and inside the largest member; deltaE76 between the two medians; '
                        'absorbed share = absorbed m2 / smaller member m2; gap and cover from the run-v1 feature; the mark from eye-tally.md. '
                        'Thresholds: a feature is kept when deltaE <= t (and cover > 0 for the second bar); score = right kept + wrong refused, '
                        'unclear ignored. Numbers only, no claim about accuracy.',
              'n_features': len(rows), 'n_measured': len(good), 'errors': [r for r in rows if 'error' in r], 'summary': summary, 'rows': rows}
    (out_dir/'small-touch-tone.json').write_text(json.dumps(result, indent=1))
    (out_dir/'small-touch-tone.md').write_text(tone_markdown(result, groups))
    return result


def tone_markdown(result, groups):
    s = result['summary']

    def table(group):
        head = ['| dE | # | scene | rule | skeptic | smaller m2 | absorbed share | gap m | cover | smaller Lab | largest Lab | smaller RGB | largest RGB | px |',
                '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
        body = [f"| {r['delta_e']:.1f} | {r['n']} | {r['scene']} | {r['rule']} | {r['mark'] or '-'} | {r['smaller_m2']:.1f}"
                f"{' (carried)' if r['smaller_carried'] else ''} | {r['absorbed_share'] if r['absorbed_share'] is not None else '-'} | "
                f"{r['gap_m'] if r['gap_m'] is not None else '-'} | {r['cover']:.2f} | {' '.join(f'{v:.0f}' for v in r['smaller_lab'])} | "
                f"{' '.join(f'{v:.0f}' for v in r['largest_lab'])} | {' '.join(str(v) for v in r['smaller_rgb'])} | "
                f"{' '.join(str(v) for v in r['largest_rgb'])} | {r['smaller_pixels']}{'' if r['smaller_eroded_1px'] else ' (no erosion)'} |"
                for r in sorted(group, key=lambda r: r['delta_e'])]
        return head+body

    def bar(th, label):
        b = th['best']
        if not b:
            return f"- {label}: no right/wrong marks to separate."
        return (f"- {label}: best bar deltaE <= {b['t']:.1f}: right kept {b['right_kept']} of {th['n_right']}, wrong refused {b['wrong_refused']} "
                f"of {th['n_wrong']} (wrong kept {b['wrong_kept']}, right lost {b['right_lost']}); sweep "
                + ', '.join(f"t {c['t']:g}: R {c['right_kept']}/{th['n_right']} W-refused {c['wrong_refused']}/{th['n_wrong']}" for c in th['sweep']) + '.')
    lines = [f"# Tone study: the surface under the smaller member vs the largest, run-v1 review features ({result['computed_at']})", '',
             result['method'], '', f"Reference {result['reference']}; marks {result['tally']}; {result['n_measured']} of {result['n_features']} features "
             f"measured" + (f"; errors: {len(result['errors'])}" if result['errors'] else '') + '. Numbers only.', '']
    for name, group in groups.items():
        g = s[name]
        lines += [f"## {name} ({g['n']} features; skeptic {g['marks']})", '',
                  f"deltaE by mark: right {g['delta_e_by_mark']['right']}, wrong {g['delta_e_by_mark']['wrong']}, unclear {g['delta_e_by_mark']['unclear']}; "
                  f"all {g['delta_e_all']}; cover > 0 by mark {g['cover_gt_0']}.", '',
                  bar(g['threshold_delta_e'], 'deltaE alone'), bar(g['threshold_delta_e_and_cover_gt_0'], 'deltaE AND cover > 0'), '',
                  *table(group), '']
    st, cs, ref = s['small_touch'], s['cover+small_touch'], s['cover_or_same_mask']
    b1, b2 = st['threshold_delta_e']['best'], st['threshold_delta_e_and_cover_gt_0']['best']

    def med(g, mark):
        d = g['delta_e_by_mark'][mark]
        return f"{d['median']:.1f} (n {d['n']}, {d['min']:.1f}-{d['max']:.1f})" if d.get('n') else 'n 0'
    lines += ['## What the table shows', '',
              f"Among the {st['n']} small_touch features the skeptic's right marks have median deltaE {med(st, 'right')}, the wrong marks "
              f"{med(st, 'wrong')} and the unclear {med(st, 'unclear')}; cover > 0 holds for right {st['cover_gt_0']['right']}, wrong "
              f"{st['cover_gt_0']['wrong']}, unclear {st['cover_gt_0']['unclear']}. "
              + (f"The single best deltaE bar ({b1['t']:.1f}) keeps {b1['right_kept']} of {st['threshold_delta_e']['n_right']} right and refuses "
                 f"{b1['wrong_refused']} of {st['threshold_delta_e']['n_wrong']} wrong ({b1['wrong_kept']} wrong kept); with cover > 0 also required "
                 f"the best bar ({b2['t']:.1f}) keeps {b2['right_kept']} right and refuses {b2['wrong_refused']} wrong ({b2['wrong_kept']} kept). "
                 if b1 and b2 else '')
              + f"The reference bands sit at median deltaE {ref['delta_e_all'].get('median', '-')} for the {ref['n']} cover / same_mask features "
              f"(right {med(ref, 'right')}, wrong {med(ref, 'wrong')}) and {cs['delta_e_all'].get('median', '-')} for the {cs['n']} cover+small_touch "
              f"features (right {med(cs, 'right')}). These are medians of frame pixels under outlines the join drew, not a measure of accuracy.", '']
    return '\n'.join(lines)


# ------------------------------------------------------------------------------------------------------------------ main

def marks_sheet_name(design_name):
    return f'MARKS-{MARKS_DATE}.md' if design_name == 'v1' else f'MARKS-{design_name}-{MARKS_DATE}.md'


MARKS_RENDER = re.compile(r' · render (\S+\.png)')


def render_only(out, design_name, raw_panel, scenes_root=SCENES, only=None):
    """--render-only: re-draw <out>/renders (feature PNGs, contact sheets, no-join notes) from the <scene>.review.json files and
    metrics.json ('inherit_of') an earlier run wrote into <out>; no join is recomputed and neither the JSON nor the marks sheet is
    rewritten (the numbering and the file names come out the same, in the run's order: named windows first, then by id). Refuses when
    the run's design differs from design_name, or when a scene's live review revision, model signature, pixel size or image size no
    longer matches what the run read (the renders draw the live review objects). only: render just these scenes (the numbering still
    counts every window). Returns the marks sheet's render paths that do not exist afterwards."""
    out = Path(out)
    metrics = json.loads((out/'metrics.json').read_text())
    if metrics.get('design_name', 'v1') != design_name:
        raise SystemExit(f"--render-only: {out} was run with design {metrics.get('design_name', 'v1')}, not {design_name}")
    design = DESIGNS[design_name]
    inherit_of = {tuple(k.split(':', 1)): v for k, v in (metrics.get('inherit_of') or {}).items()}
    results = {r['scene']: r for r in (json.loads(path.read_text()) for path in sorted(out.glob(f'*.{RENDER_LAYER}.json')))}
    if not results:
        raise SystemExit(f'--render-only: no *.{RENDER_LAYER}.json in {out}')
    scenes, stale = {}, []
    for scene_id, r in results.items():
        latest, review, image_path = load_scene(scene_id, scenes_root)
        live = {'review_revision': review.get('revision') if review else None, 'model_signature': latest.get('model_signature'),
                'pixel_to_meters': latest['pixel_to_meters'], 'image_width': latest['image_width'], 'image_height': latest['image_height']}
        moved = {k: (r.get(k), v) for k, v in live.items() if r.get(k) != v}
        if moved or review is None:
            stale.append(f"{scene_id} {moved or 'no review.json'}")
        scenes[scene_id] = (review, image_path)
    if stale:
        raise SystemExit('--render-only: the live scene moved since the run (re-run the join instead):\n  '+'\n  '.join(stale))
    marks_name = marks_sheet_name(design_name)
    renders_dir = out/'renders'
    order = [s for s in NAMED_WINDOWS if s in results] + [s for s in sorted(results) if s not in NAMED_WINDOWS]
    index = 1
    for scene_id in order:
        r = results[scene_id]
        if only and scene_id not in only:
            index += len(r['features'])
            continue
        review, image_path = scenes[scene_id]
        rows, index, sheet = render_window(scene_id, r, review['objects'], image_path, r['pixel_to_meters'], r['review_revision'],
                                           renders_dir, index, marks_name, inherit_of, design, design_name, raw_panel=raw_panel)
        print('rendered:', scene_id, len(rows), sheet.name, flush=True)
    marks = out/marks_name
    missing = []
    if marks.exists():
        paths = [m.group(1) for line in marks.read_text().splitlines() if (m := MARKS_RENDER.search(line))]
        missing = [path for path in paths if not (ROOT/path).exists()]
        print(f'{marks.name}: {len(paths)-len(missing)} of {len(paths)} render paths exist (sheet not rewritten)', flush=True)
    return missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--scenes', nargs='*', help='scene ids (default: every win-* / lot-* window with a latest.json)')
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--design', choices=sorted(DESIGNS), default='v1',
                        help='v1 = BUILDING_MERGE (the approved defaults, run-v1); v2 = BUILDING_MERGE_V2 (the 7 Oct proposal, not approved)')
    parser.add_argument('--no-render', action='store_true', help='skip the renders and the marks sheet')
    parser.add_argument('--raw-panel', action=argparse.BooleanOptionalAction, default=None,
                        help='put a raw panel (same crop and scale, 1 px outlines, no fill) left of every annotated feature panel '
                             '(default: on for --design v2, off for v1)')
    parser.add_argument('--render-only', action='store_true',
                        help='re-draw <out>/renders from the <scene>.review.json + metrics.json already in <out>; no join recomputed, '
                             'JSON and marks sheet not rewritten')
    parser.add_argument('--scenes-root', type=Path, default=SCENES, help='scene directory (read-only; default data/scenes)')
    parser.add_argument('--v1-dir', type=Path, default=RUN_V1,
                        help="run-v1 reference (features, marks sheet) for 'v1_features' / 'v2_new' / hints; skipped when missing or = --out")
    parser.add_argument('--tally', type=Path, default=EYE_TALLY, help="the skeptic's eye-tally.md (hints only)")
    parser.add_argument('--no-reference', action='store_true', help='do not read the run-v1 reference')
    parser.add_argument('--tone-study', action='store_true',
                        help="only the tone study on the reference run's review features -> <out>/analysis/small-touch-tone.{json,md}")
    args = parser.parse_args(argv)
    design = DESIGNS[args.design]
    raw_panel = args.raw_panel if args.raw_panel is not None else args.design != 'v1'
    if args.render_only:
        if args.no_render or args.tone_study:
            parser.error('--render-only cannot be combined with --no-render or --tone-study')
        missing = render_only(args.out, args.design, raw_panel, args.scenes_root, set(args.scenes or []))
        if missing:
            raise SystemExit(f"render paths on the marks sheet that do not exist: {missing}")
        return
    version = f'roof-join-{args.out.name}'
    reference = None
    if not args.no_reference and args.v1_dir.exists() and args.v1_dir.resolve() != args.out.resolve():
        reference = load_reference(args.v1_dir, args.tally)
        if reference:
            print(f"reference: {len(reference['features'])} run-v1 {reference['layer']} features from {args.v1_dir}; marks {reference['marks_total']}", flush=True)
    if args.tone_study:
        if not reference:
            parser.error('--tone-study needs the run-v1 reference (--v1-dir)')
        result = tone_study(reference, args.out/'analysis', args.scenes_root)
        print(json.dumps({'n_measured': result['n_measured'], 'errors': len(result['errors']),
                          'summary': {k: {'n': v['n'], 'marks': v['marks'], 'best_delta_e': v['threshold_delta_e']['best'],
                                          'best_delta_e_and_cover': v['threshold_delta_e_and_cover_gt_0']['best']} for k, v in result['summary'].items()}}, indent=1))
        print('written:', args.out/'analysis'/'small-touch-tone.json', args.out/'analysis'/'small-touch-tone.md')
        return
    scene_ids = args.scenes or window_ids(args.scenes_root)
    args.out.mkdir(parents=True, exist_ok=True)
    results, scenes = {}, {}
    sensitivity = {variant: defaultdict(list) for variant in SENSITIVITY}
    for n, scene_id in enumerate(scene_ids, 1):
        latest, review, image_path = load_scene(scene_id, args.scenes_root)
        to_meters = latest['pixel_to_meters']
        image_size = (latest['image_width'], latest['image_height'])
        layers = {'model': latest['objects'], **({'review': review['objects']} if review else {})}
        results[scene_id] = {}
        for layer, objects in layers.items():
            result = analyse_layer(scene_id, layer, objects, to_meters, image_size, version, design)
            result.update({'design_name': args.design, 'image_width': image_size[0], 'image_height': image_size[1], 'pixel_to_meters': to_meters,
                           'model_signature': latest.get('model_signature'), 'review_revision': review.get('revision') if review else None})
            (args.out/f'{scene_id}.{layer}.json').write_text(json.dumps(result, indent=1))
            results[scene_id][layer] = result
            for variant, overrides in SENSITIVITY.items():
                sensitivity[variant][layer].append(sensitivity_counts(objects, to_meters, image_size, overrides, design))
        scenes[scene_id] = (latest, review, image_path)
        counts = {layer: len(results[scene_id][layer]['features']) for layer in results[scene_id]}
        print(f"scene {n}/{len(scene_ids)} {scene_id} features {counts}", flush=True)
    metrics = build_metrics(results, scene_ids, version, sensitivity, design, args.design, reference)
    inherit_of = {tuple(k.split(':', 1)): v for k, v in (metrics.get('inherit_of') or {}).items()}
    (args.out/'metrics.json').write_text(json.dumps(metrics, indent=1))
    print(json.dumps({'design': args.design,
                      'layers': {layer: {k: v for k, v in m.items() if k in ('pieces_in', 'joinable', 'multi_piece_features', 'pieces_superseded',
                                                                               'merges', 'refusals', 'umbrellas', 'party_wall_pairs_ge_20m2')}
                                 for layer, m in metrics['layers'].items()},
                      'checks': metrics['checks'], 'named_windows': metrics['named_windows'],
                      'carried_member_features': metrics['carried_member_features']['n'], 'vertex_rule': metrics['vertex_rule'],
                      **({'v1_features': {k: v for k, v in metrics['v1_features'].items() if k in ('n', 'totals_by_status', 'totals_by_status_and_mark')},
                          'v2_new': {k: v for k, v in metrics['v2_new'].items() if k != 'rows'}} if 'v1_features' in metrics else {})}, indent=1))
    print('written:', args.out/'metrics.json', f'+ {sum(len(v) for v in results.values())} layer files', flush=True)
    if args.no_render:
        return
    marks_name = marks_sheet_name(args.design)
    windows, index = [], 1
    renders_dir = args.out/'renders'
    order = [s for s in NAMED_WINDOWS if s in results] + [s for s in scene_ids if s not in NAMED_WINDOWS]
    for scene_id in order:
        if RENDER_LAYER not in results[scene_id]:
            continue
        latest, review, image_path = scenes[scene_id]
        revision = review.get('revision') if review else None
        rows, index, sheet = render_window(scene_id, results[scene_id][RENDER_LAYER], review['objects'], image_path,
                                           latest['pixel_to_meters'], revision, renders_dir, index, marks_name, inherit_of, design, args.design,
                                           raw_panel=raw_panel)
        windows.append((scene_id, rows, sheet, revision))
        print('rendered:', scene_id, len(rows), sheet.name, flush=True)
    marks = write_marks(args.out/marks_name, windows, metrics, args.out, design, args.design)
    print('written:', marks)


if __name__ == '__main__':
    sys.exit(main())
