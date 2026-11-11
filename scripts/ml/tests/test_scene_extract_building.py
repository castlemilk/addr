"""merge_building_fragments (scene_extract): the authorised building join (2026-10-07 "yes to defaults" on the roof-fragmentation card)
and card item 2(a)'s extraction half (buildings simplified at 2 px above 50 vertices).

Synthetic geometry is drawn in metres and converted to pixels through a known pixel_to_meters matrix (0.1 m/px, y flipped, origin at
50 m), the shape the scenes store. Every expectation is a count, a status, a reason or a geometry; nothing here is accuracy.
"""
import ast
import json
import math
from pathlib import Path

import numpy as np
from scripts.ml.scene_extract import (
    BUILDING_MERGE,
    BUILDING_OCCLUDER_KINDS,
    BUILDING_REFUSALS,
    BUILDING_RING_SIMPLIFY,
    BUILDING_RULES,
    BUILDING_SIMPLIFY_VERTICES,
    RING_SIMPLIFY,
    app_ring_issues,
    building_umbrellas,
    merge_building_fragments,
    ring_simplify_budget,
    simplify_long_ring,
)
from shapely.geometry import Polygon

ML = Path(__file__).resolve().parents[1]
AFFINE = np.array([[.1, 0.], [0., -.1], [0., 50.]])     # pixel -> metres: x_m = .1 px, y_m = 50 - .1 py
SPACING = .1
IMAGE = (1000, 1000)


def px(points_m):
    return [[round(x/SPACING, 2), round((50.-y)/SPACING, 2)] for x, y in points_m]


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def metric(obj):
    ring = np.asarray(obj['ring'], float)
    return Polygon(np.c_[ring, np.ones(len(ring))] @ AFFINE,
                   [np.c_[np.asarray(h, float), np.ones(len(h))] @ AFFINE for h in obj.get('holes') or []])


def obj(oid, ring_m, kind='building', status='candidate', score=.5, **extra):
    shape = Polygon(ring_m)
    return {'id': oid, 'kind': kind, 'label': kind.replace('_', ' ').capitalize(), 'ring': px(ring_m), 'status': status,
            'geometry_reviewed': False, 'source': 'sam3-text-onnx', 'score': score,
            'prompt': {'building': 'building roof', 'tree': 'tree canopy', 'car': 'car'}.get(kind, kind),
            'model': {'area_m2': round(shape.area, 2), 'color': '#8f8e90', 'height_m': 3.2}, **extra}


def run(objects, image_size=IMAGE):
    before = json.dumps(objects, sort_keys=True)
    out, report = merge_building_fragments(objects, AFFINE, image_size=image_size)
    assert json.dumps(objects, sort_keys=True) == before, 'the input list must not be mutated'
    return out, report


def features(out):
    return [o for o in out if o.get('source') == BUILDING_MERGE['source']]


def reasons(report):
    return [r['reason'] for r in report['refusals']]


# ---------------------------------------------------------------------------------------------------------- the constants

def test_design_constants_are_the_card_defaults_and_disabled():
    assert BUILDING_MERGE['kind'] == 'building' and BUILDING_MERGE['touch_m'] == .5
    assert BUILDING_MERGE['cover_min'] == .5 and BUILDING_MERGE['small_m2'] == 12. and BUILDING_MERGE['substantial_m2'] == 20.
    assert BUILDING_MERGE['party_wall_cover_max'] == .05 and BUILDING_MERGE['umbrella_pieces'] == 3
    assert BUILDING_MERGE['occluder_kinds'] == BUILDING_OCCLUDER_KINDS == ('tree', 'vegetation', 'solar_panel', 'car')
    assert BUILDING_MERGE['result_floor_m2'] == 1. and BUILDING_MERGE['ring_decimals'] == 2
    assert BUILDING_MERGE['decided_statuses'] == ('confirmed', 'rejected', 'needs_adjustment', 'superseded')
    assert BUILDING_MERGE['source'] == 'building-merge-v1' and BUILDING_MERGE['enabled'] is False
    assert BUILDING_RULES == ('same_mask', 'cover', 'occluder_strip', 'small_touch')
    assert set(BUILDING_REFUSALS) >= {'umbrella', 'party_wall', 'touches_third_building', 'no_rule', 'no_bridge', 'touches_decided',
                                      'invalid_after_rounding', 'app_ring_rules', 'result_floor'}
    assert BUILDING_SIMPLIFY_VERTICES == 50
    assert BUILDING_RING_SIMPLIFY == {**RING_SIMPLIFY, 'vertices': 50} and RING_SIMPLIFY['vertices'] == 150
    source = (ML/'scene_extract.py').read_text()
    block = source[source.index('BUILDING_OCCLUDER_KINDS = '):source.index('BUILDING_MERGE = ')]
    comment = source[source.index('# --- the building join (2026-10-07)'):source.index('BUILDING_OCCLUDER_KINDS = ')]
    assert '"yes to defaults"' in comment and 'roof-fragmentation-2026-10-06/DECISION-CARD.md' in comment and '2026-10-07' in comment
    assert 'never two pieces >= 20 m2 that only touch' in comment and 'umbrella covering >= 3 pieces' in comment
    assert block == '' or block.startswith('BUILDING_OCCLUDER_KINDS')


# ------------------------------------------------------------------------------------------------------------ the rules fire

def test_a_same_mask_components_rejoin_in_step_1_with_lineage():
    a = obj('aaaa000000000001', rect(0, 0, 8, 6), score=.4777)                       # 48 m2
    b = obj('bbbb000000000002', rect(8.3, 0, 12, 6), score=.4777)                    # 22.2 m2, 0.3 m of roof lost to the split
    out, report = run([a, b])
    feature, = features(out)
    members = [o for o in out if o['id'] in (a['id'], b['id'])]
    assert all(m['status'] == 'superseded' and m['replacement_ids'] == [feature['id']] for m in members)
    assert {k: v for k, v in members[0].items() if k not in ('status', 'replacement_ids')} == {k: v for k, v in a.items() if k != 'status'}
    assert feature['status'] == 'candidate' and feature['kind'] == 'building' and feature['label'] == 'Building'
    assert feature['source'] == 'building-merge-v1' and feature['geometry_reviewed'] is False and feature['prompt'] == 'building roof'
    assert feature['replaces'] == feature['derived_from'] == [a['id'], b['id']]
    assert [m['id'] for m in feature['merged_from']] == [a['id'], b['id']]
    assert all(m['step'] == 1 and m['rule'] == 'same_mask' and abs(m['gap_m']-.3) < .01 for m in feature['merged_from'])
    assert len(feature['id']) == 16 and feature['model']['color'] == '#8f8e90'
    shape, pieces = metric(feature), metric(a).union(metric(b))
    assert shape.is_valid and pieces.area < shape.area < pieces.area+6*.3*1.2 and abs(feature['model']['area_m2']-shape.area) < .02
    assert shape.contains(metric(a).buffer(-.01)) and shape.contains(metric(b).buffer(-.01))
    assert all(len(str(v).split('.')[-1]) <= 2 for v in np.ravel(feature['ring']))
    k = report['kinds']['building']
    assert k['pieces_in'] == 2 and k['joinable'] == 2 and k['features_out'] == 1 and k['multi_piece_features'] == 1
    assert k['merges'] == {'step1': 1, 'step2': 0, 'same_mask': 1, 'cover': 0, 'occluder_strip': 0, 'small_touch': 0}
    assert k['refusals'] == {} and k['umbrellas'] == 0 and report['umbrellas'] == []
    assert report['features'][0]['rules'] == ['same_mask'] and report['features'][0]['carried_members'] == []
    assert app_ring_issues(feature['ring'], feature.get('holes', []), *IMAGE) == []
    # two masks (different scores) never pass as one mask: the pair falls to step 2, where nothing fires for two large pieces
    _, report = run([a, obj('bbbb000000000002', rect(8.3, 0, 12, 6), score=.51)])
    assert report['kinds']['building']['merges']['same_mask'] == 0 and reasons(report) == ['party_wall']


def test_b_the_cover_rule_absorbs_a_slice_covered_at_least_half_including_the_dedupe_gap():
    body = obj('b0d7000000000001', rect(5, 0, 14, 9), score=.62)                        # 81 m2, the window's own detection
    slice_ = obj('51ce000000000002', rect(3.7, 0, 8, 3), score=.44)                     # 12.9 m2, 69.8 % inside the body (#2's case)
    out, report = run([body, slice_])
    feature, = features(out)
    assert [m['rule'] for m in feature['merged_from']] == ['cover', 'cover']
    assert abs(feature['merged_from'][0]['cover']-.698) < .005 and feature['merged_from'][0]['bridge_m2'] == 0.
    assert feature['score'] == .62 and feature['replaces'] == sorted([body['id'], slice_['id']])
    assert abs(metric(feature).area-(81+1.3*3)) < .05                                    # the union, nothing added (x >= 0: the app's bounds rule)
    assert report['kinds']['building']['merges'] == {**report['kinds']['building']['merges'], 'step2': 1, 'cover': 1}
    assert report['features'][0]['bridge_m2'] == 0.
    for cover in (.5, .85, .96):                                                        # the dedupe gap (0.8-0.95) is inside the rule
        width = 4*cover
        dup = obj('d0b1000000000003', rect(14-width, 0, 18-width, 4), score=.4)         # 16 m2, `cover` of it inside the body
        _, report = run([body, dup])
        assert report['kinds']['building']['merges']['cover'] == 1, cover
    thin = obj('7h1n000000000004', rect(12, 0, 16, 4), score=.4)                         # 16 m2 at cover 0.5 exactly -> fires
    _, report = run([body, thin])
    assert report['kinds']['building']['merges']['cover'] == 1 and report['merges'][0]['cover'] == .5
    under = obj('u0de000000000005', rect(12.2, 0, 16.2, 4), score=.4)                    # 16 m2 at cover 0.45 -> no rule
    _, report = run([body, under])
    assert features(_) == [] and reasons(report) == ['no_rule'] and report['refusals'][0]['cover'] == .45


def test_c_the_occluder_strip_rule_joins_across_one_tree_and_refuses_a_third_building_or_two_trees():
    a = obj('aaaa000000000001', rect(0, 0, 6, 5), score=.6)                               # 30 m2
    b = obj('bbbb000000000002', rect(6.4, 0, 9.4, 5), score=.45)                          # 15 m2: not under 12, so only the strip rule can fire
    tree = obj('7ee0000000000003', rect(5.5, -1, 7, 6), kind='tree', score=.8)            # covers the 0.4 m strip whole
    out, report = run([a, b, tree])
    feature, = features(out)
    merge, = report['merges']
    assert merge['rule'] == 'occluder_strip' and merge['rules'] == ['occluder_strip'] and merge['step'] == 2
    assert merge['strip']['occluder']['id'] == tree['id'] and merge['strip']['occluder']['kind'] == 'tree'
    assert merge['strip']['occluder']['share'] >= .95 and 1.9 < merge['bridge_m2'] < 2.6         # 0.4 x 5 m plus the rounded ends
    assert feature['merged_from'][0]['occluder'] == merge['strip']['occluder']
    assert out[2] is tree and metric(feature).contains(metric(a).buffer(-.01)) and metric(feature).contains(metric(b).buffer(-.01))
    assert report['kinds']['building']['merges']['occluder_strip'] == 1 and report['kinds']['building']['bridge_area_m2'] > 1.9
    # every occluder kind of the card works the same; a pool does not
    for kind in ('vegetation', 'solar_panel', 'car'):
        _, report = run([a, b, obj('0cc1000000000004', rect(5.5, -1, 7, 6), kind=kind, score=.8)])
        assert report['kinds']['building']['merges']['occluder_strip'] == 1, kind
    _, report = run([a, b, obj('p001000000000005', rect(5.5, -1, 7, 6), kind='pool', score=.8)])
    assert reasons(report) == ['no_rule'] and report['refusals'][0]['strip']['occluder'] is None
    # the strip touching a third live building refuses the pair (a confirmed one here: no other rule can fire)
    third = obj('3rd0000000000006', rect(6, 4, 6.4, 7), status='confirmed', score=.7)
    out, report = run([a, b, tree, third])
    assert features(out) == [] and reasons(report) == ['touches_third_building']
    assert report['refusals'][0]['third_buildings'] == [{'id': third['id'], 'kind': 'building', 'status': 'confirmed'}]
    # inside ONE object: two trees each covering half of the strip do not count
    half1 = obj('7ee1000000000007', rect(5.5, -1, 7, 2.5), kind='tree', score=.8)
    half2 = obj('7ee2000000000008', rect(5.5, 2.5, 7, 6), kind='tree', score=.8)
    _, report = run([a, b, half1, half2])
    assert reasons(report) == ['no_rule'] and abs(report['refusals'][0]['strip']['occluder']['share']-.5) < .1
    # a tree over 70 % of the strip is not "inside"
    part = obj('7ee3000000000009', rect(5.5, -1, 7, 3.5), kind='tree', score=.8)
    _, report = run([a, b, part])
    assert reasons(report) == ['no_rule'] and .6 < report['refusals'][0]['strip']['occluder']['share'] < .95
    # overlapping pieces have no strip between them: the tree over the seam does not make a rule
    _, report = run([a, obj('bbbb000000000002', rect(5.8, 0, 8.8, 5), score=.45), tree])
    assert reasons(report) == ['no_rule'] and 'strip' not in report['refusals'][0]


def test_d_the_small_touch_rule_joins_a_piece_under_12_m2_within_half_a_metre():
    house = obj('h0us000000000001', rect(0, 0, 10, 10), score=.7)                         # 100 m2
    shed = obj('5hed000000000002', rect(10.4, 0, 12.4, 2.5), score=.4)                   # 5 m2, 0.4 m away, nothing between
    out, report = run([house, shed])
    feature, = features(out)
    merge, = report['merges']
    assert merge['rule'] == 'small_touch' and merge['smaller_m2'] == 5. and abs(merge['gap_m']-.4) < .01
    assert .9 < merge['bridge_m2'] < 1.3 and feature['merged_from'][1]['rule'] == 'small_touch'
    assert report['kinds']['building']['merges']['small_touch'] == 1
    # at 12 m2 the rule stops; past 0.5 m the pair is never tried
    _, report = run([house, obj('5hed000000000002', rect(10.4, 0, 13.4, 4), score=.4)])   # 12 m2 exactly
    assert reasons(report) == ['no_rule'] and report['refusals'][0]['smaller_m2'] == 12.
    _, report = run([house, obj('5hed000000000002', rect(10.6, 0, 12.6, 2.5), score=.4)])  # 0.6 m away
    assert report['merges'] == [] and report['refusals'] == [] and report['kinds']['building']['pairs_tried'] == 0
    # a piece that overlaps a little (cover 0.03, #5's case) touches at gap 0 and joins the same way, with no bridge
    sliver = obj('51iv000000000003', rect(9.8, 9, 12, 11.2), score=.4)                   # 4.84 m2, 0.2 x 1 m inside the house
    out, report = run([house, sliver])
    merge, = report['merges']
    assert merge['rule'] == 'small_touch' and merge['gap_m'] == 0. and merge['bridge_m2'] == 0. and abs(merge['cover']-.041) < .01


# ----------------------------------------------------------------------------------------------------------- the guards refuse

def test_e_two_pieces_of_20_m2_that_only_touch_never_merge_even_under_a_tree():
    a = obj('aaaa000000000001', rect(0, 0, 8, 5), score=.6)                               # 40 m2
    b = obj('bbbb000000000002', rect(8.1, 0, 16.1, 5), score=.45)                         # 40 m2, 0.1 m party wall
    tree = obj('7ee0000000000003', rect(7.5, -1, 8.7, 6), kind='tree', score=.8)         # a canopy over the wall
    out, report = run([a, b, tree])
    assert features(out) == [] and reasons(report) == ['party_wall']
    assert [o['status'] for o in out] == ['candidate', 'candidate', 'candidate'] and out[0] is a and out[1] is b
    assert report['kinds']['building']['refusals'] == {'party_wall': 1} and report['kinds']['building']['features_out'] == 2
    _, report = run([a, obj('bbbb000000000002', rect(8, 0, 16, 5), score=.45)])          # a shared edge, gap 0
    assert reasons(report) == ['party_wall'] and report['refusals'][0]['cover'] == 0.
    # 20 m2 is the bar: a 19.9 m2 side is not a party wall (and not a rule either: nothing fires for it)
    _, report = run([a, obj('bbbb000000000002', rect(8.1, 0, 12.08, 5), score=.45)])
    assert reasons(report) == ['no_rule']
    # two large pieces that overlap at cover >= 0.5 are a duplicate mask, not a party wall
    _, report = run([a, obj('bbbb000000000002', rect(3, 0, 11, 5), score=.45)])
    assert report['kinds']['building']['merges']['cover'] == 1 and report['refusals'] == []
    # the guard reads the ORIGINAL pieces too: a mask spanning two houses absorbs one and never carries it onto the other
    left = obj('1eft000000000003', rect(0, 0, 8, 5), score=.6)                           # 40 m2
    right = obj('r1gh000000000004', rect(8.1, 0, 16.1, 5), score=.45)                     # 40 m2, 0.1 m party wall
    span = obj('5pan000000000005', rect(2, 0, 14, 5), score=.5)                          # 60 m2 over both: cover .75 of each
    out, report = run([left, right, span])
    feature, = features(out)
    assert feature['replaces'] == sorted([left['id'], span['id']]) and report['kinds']['building']['merges']['cover'] == 1
    refusal, = report['refusals']
    assert refusal['reason'] == 'party_wall' and refusal['party_wall_pieces'] == [{'a': left['id'], 'b': right['id']}]
    assert refusal['cover'] >= .5                                                           # the clusters' cover would have fired
    # a party-wall pair is a step-2 guard; one mask split in two 40 m2 halves rejoins in step 1
    _, report = run([a, obj('bbbb000000000002', rect(8.1, 0, 16.1, 5), score=.6)])
    assert report['kinds']['building']['merges'] == {**report['kinds']['building']['merges'], 'same_mask': 1} and report['refusals'] == []


def test_f_decided_pieces_never_merge_and_block_any_bridge_while_an_undecided_carry_is_joinable():
    house = obj('h0us000000000001', rect(0, 0, 10, 10), status='confirmed', score=.7, geometry_reviewed=True)
    shed = obj('5hed000000000002', rect(10.4, 0, 12.4, 2.5), score=.4)
    out, report = run([house, shed])
    assert out == [house, shed] and out[0] is house and features(out) == []
    k = report['kinds']['building']
    assert k['pieces_in'] == 2 and k['candidates'] == 1 and k['joinable'] == 1 and k['pairs_tried'] == 0 and k['features_out'] == 1
    for status in ('confirmed', 'rejected', 'needs_adjustment', 'superseded'):
        decided = obj('dec1000000000003', rect(10.1, .5, 10.3, 2), kind='other', status=status, score=.3)
        _, report = run([obj('h0us000000000001', rect(0, 0, 10, 10), score=.7), shed, decided])
        assert reasons(report) == ['touches_decided'], status
        assert report['refusals'][0]['blockers'] == [{'id': decided['id'], 'kind': 'other', 'status': status}]
        assert report['refusals'][0]['rule'] == 'small_touch'                                # the rule fired; the bridge was blocked
    # a needs_adjustment candidate of the reviewer's own (no carry) is decided; the carry's placeholder is not
    draft = obj('5hed000000000002', rect(10.4, 0, 12.4, 2.5), status='needs_adjustment', score=.4)
    _, report = run([obj('h0us000000000001', rect(0, 0, 10, 10), score=.7), draft])
    assert report['kinds']['building']['joinable'] == 1 and report['merges'] == []
    carried = {'scene_id': 'map-0000000000000000', 'object_id': 'o', 'role': 'frame_cut_target', 'method': 'georeferenced'}
    target = obj('5hed000000000002', rect(10.4, 0, 12.4, 2.5), status='needs_adjustment', score=.4, carried_from=carried,
                 notes='Carried from map-0000000000000000 through the georeferenced frame as a frame-cut target')
    out, report = run([obj('h0us000000000001', rect(0, 0, 10, 10), score=.7), target])
    feature, = features(out)
    assert report['kinds']['building']['joinable'] == 2 and report['kinds']['building']['carried_undecided_joinable'] == 1
    assert report['features'][0]['carried_members'] == [target['id']] and report['kinds']['building']['features_with_carried_member'] == 1
    assert [o['status'] for o in out[:2]] == ['superseded', 'superseded'] and feature['replaces'] == sorted([target['id'], 'h0us000000000001'])
    decided_target = {**target, 'status': 'rejected', 'provenance': {'human_decision': {'source': 'human:chat'}}}
    _, report = run([obj('h0us000000000001', rect(0, 0, 10, 10), score=.7), decided_target])
    assert report['kinds']['building']['joinable'] == 1 and report['merges'] == [] and report['refusals'] == []
    adjusted = {**target, 'provenance': {'human_decision': {'source': 'human:ui'}}}                  # needs_adjustment by the reviewer
    _, report = run([obj('h0us000000000001', rect(0, 0, 10, 10), score=.7), adjusted])
    assert report['kinds']['building']['joinable'] == 1 and report['merges'] == []


def test_g_an_umbrella_over_three_pieces_of_20_m2_is_refused_and_listed():
    umbrella = obj('umb0000000000001', rect(0, 0, 30, 10), score=.9)                      # 300 m2 over a unit row
    units = [obj(f'un1t00000000000{i}', rect(10*i, 0, 10*i+10, 10), score=.5+i/100) for i in range(3)]   # 100 m2 each
    out, report = run([umbrella, *units])
    assert features(out) == [] and all(o['status'] == 'candidate' for o in out)
    assert report['umbrellas'] == [{'id': umbrella['id'], 'area_m2': 300., 'covered': [{'id': u['id'], 'area_m2': 100., 'cover': 1.}
                                                                                       for u in units]}]
    assert report['kinds']['building']['umbrellas'] == 1
    assert sorted(reasons(report)) == ['party_wall', 'party_wall', 'umbrella', 'umbrella', 'umbrella']
    assert all(r['umbrellas'] == [umbrella['id']] for r in report['refusals'] if r['reason'] == 'umbrella')
    shapes = {o['id']: metric(o) for o in [umbrella, *units]}
    assert list(building_umbrellas([umbrella, *units], shapes)) == [umbrella['id']]
    # confirmed dwellings under it count too; two dwellings are not an umbrella (the cover rule absorbs them)
    confirmed = [{**u, 'status': 'confirmed'} for u in units]
    _, report = run([umbrella, *confirmed])
    assert report['umbrellas'][0]['id'] == umbrella['id'] and report['kinds']['building']['joinable'] == 1 and report['merges'] == []
    two = obj('umb0000000000002', rect(0, 0, 20, 10), score=.9)
    out, report = run([two, *units[:2]])
    assert report['umbrellas'] == [] and len(features(out)) == 1 and report['kinds']['building']['merges']['cover'] == 1
    assert features(out)[0]['replaces'] == sorted([two['id'], units[0]['id']])             # the mask absorbs one unit; the units only touch
    refusal, = report['refusals']
    assert refusal['reason'] == 'party_wall' and refusal['party_wall_pieces'] == [{'a': units[0]['id'], 'b': units[1]['id']}]
    # three pieces under 20 m2 each do not make an umbrella (the cover rule applies piece by piece)
    small = obj('umb0000000000003', rect(0, 0, 12, 4), score=.9)                          # 48 m2
    sheds = [obj(f'5hed00000000000{i}', rect(4*i, 0, 4*i+4, 4), score=.5+i/100) for i in range(3)]      # 16 m2 each
    _, report = run([small, *sheds])
    assert report['umbrellas'] == [] and report['kinds']['building']['merges']['cover'] == 3


def test_h_the_result_floor_refuses_a_feature_under_1_m2_and_leaves_the_pieces():
    s1 = obj('5pk1000000000001', rect(0, 0, .5, .6), score=.4777)                          # 0.3 m2
    s2 = obj('5pk2000000000002', rect(.6, 0, 1.1, .6), score=.4777)                        # 0.3 m2, same mask, 0.1 m away
    out, report = run([s1, s2])
    assert features(out) == [] and out[0] is s1 and out[1] is s2
    refusal, = [r for r in report['refusals'] if r['reason'] == 'result_floor']
    assert refusal['ids'] == [s1['id'], s2['id']] and refusal['feature_m2'] < 1. and refusal['largest_member_m2'] < 1.
    assert report['merges'][0]['undone'] == 'result_floor' and report['kinds']['building']['merges_undone_by_floor'] == 1
    assert report['kinds']['building']['refusals'] == {'result_floor': 1} and report['kinds']['building']['features_out'] == 2
    # a 0.5 m2 speck on a 30 m2 body passes through the body
    body = obj('b0d7000000000003', rect(0, 0, 6, 5), score=.4777)
    speck = obj('5pk3000000000004', rect(6.1, 0, 6.6, 1), score=.4777)
    out, report = run([body, speck])
    assert len(features(out)) == 1 and report['kinds']['building']['merges_undone_by_floor'] == 0


def test_m_the_cluster_level_party_wall_guard_refuses_two_grown_clusters_of_20_m2_that_only_touch():
    # two houses, each a mask split in two 18 m2 halves: no ORIGINAL piece reaches the 20 m2 bar, every step-1 cluster does
    a1 = obj('a1a1000000000001', rect(0, 0, 4, 4.5), score=.4777)                          # 18 m2
    a2 = obj('a2a2000000000002', rect(4.2, 0, 8.2, 4.5), score=.4777)                      # 18 m2, the same mask, 0.2 m split
    b1 = obj('b1b1000000000003', rect(8.4, 0, 12.4, 4.5), score=.51)                       # 18 m2, the neighbour's mask, 0.2 m party wall
    b2 = obj('b2b2000000000004', rect(12.6, 0, 16.6, 4.5), score=.51)                      # 18 m2, 0.2 m split
    tree = obj('7ee0000000000005', rect(7.7, -1, 8.9, 5.5), kind='tree', score=.8)        # a canopy over the party wall: the strip rule would fire
    pieces = [a1, a2, b1, b2]
    assert all(metric(p).area < BUILDING_MERGE['substantial_m2'] for p in pieces)
    out, report = run([*pieces, tree])
    assert {tuple(f['replaces']) for f in features(out)} == {(a1['id'], a2['id']), (b1['id'], b2['id'])}
    assert all(metric(f).area >= BUILDING_MERGE['substantial_m2'] for f in features(out))   # both clusters grew past the bar
    assert [o['status'] for o in out[:5]] == ['superseded']*4+['candidate'] and out[4] is tree
    k = report['kinds']['building']
    assert k['merges'] == {'step1': 2, 'step2': 0, 'same_mask': 2, 'cover': 0, 'occluder_strip': 0, 'small_touch': 0}
    assert reasons(report) == ['party_wall'] and k['refusals'] == {'party_wall': 1} and k['features_out'] == 2
    refusal, = report['refusals']
    assert refusal['step'] == 2 and refusal['a_ids'] == [a1['id'], a2['id']] and refusal['b_ids'] == [b1['id'], b2['id']]
    assert refusal['smaller_m2'] >= 20. and refusal['cover'] == 0. and abs(refusal['gap_m']-.2) < .01
    assert 'party_wall_pieces' not in refusal and 'rule' not in refusal     # the CLUSTER check refused it, before any rule was read
    # without the canopy no rule could fire either way; the refusal is still the party wall, read first
    _, report = run(pieces)
    assert reasons(report) == ['party_wall'] and 'party_wall_pieces' not in report['refusals'][0]


def test_n_a_protected_piece_is_never_joinable_neither_as_a_member_nor_absorbed():
    plain_house = obj('h0us000000000001', rect(0, 0, 10, 10), score=.7)                    # test_d's small-touch pair: two plain
    plain_shed = obj('5hed000000000002', rect(10.4, 0, 12.4, 2.5), score=.4)               # candidates join
    _, report = run([plain_house, plain_shed])
    assert report['kinds']['building']['merges']['small_touch'] == 1
    marks = [{'geometry_reviewed': True}, {'roof_feedback': {'verdict': 'wrong_outline'}}, {'roof_line_edits': [{'op': 'move'}]},
             {'source': 'roof-pipeline'}, {'source': 'crm-roof-pipeline'}]                # _protected, one marker at a time
    for mark in marks:
        house = obj('h0us000000000001', rect(0, 0, 10, 10), score=.7, **mark)             # a protected candidate never becomes a member
        out, report = run([house, plain_shed])
        k = report['kinds']['building']
        assert features(out) == [] and out == [house, plain_shed] and out[0] is house and out[1] is plain_shed, mark
        assert k['pieces_in'] == 2 and k['candidates'] == 2 and k['joinable'] == 1 and k['pairs_tried'] == 0, mark
        assert k['features_out'] == 1 and report['merges'] == [] and report['refusals'] == [], mark
        shed = obj('5hed000000000002', rect(10.4, 0, 12.4, 2.5), score=.4, **mark)        # ... nor is it absorbed
        out, report = run([plain_house, shed])
        assert features(out) == [] and out[1] is shed and report['kinds']['building']['joinable'] == 1, mark
        assert report['kinds']['building']['pairs_tried'] == 0 and report['merges'] == [], mark
        half = obj('bbbb000000000003', rect(10.3, 0, 14, 10), score=.7, **mark)           # not even as the other half of its own mask
        out, report = run([plain_house, half])
        assert features(out) == [] and out[1] is half and report['kinds']['building']['joinable'] == 1, mark
        assert report['kinds']['building']['merges']['same_mask'] == 0 and report['merges'] == [], mark


def test_o_step_1_never_joins_two_components_of_one_mask_more_than_half_a_metre_apart():
    a = obj('aaaa000000000001', rect(0, 0, 8, 6), score=.4777)                               # 48 m2
    far = obj('bbbb000000000002', rect(8.6, 0, 12, 6), score=.4777)                           # 20.4 m2, the same mask, 0.6 m away
    out, report = run([a, far])
    k = report['kinds']['building']
    assert features(out) == [] and out[0] is a and out[1] is far and [o['status'] for o in out] == ['candidate', 'candidate']
    assert k['joinable'] == 2 and k['pairs_tried'] == 0 and k['features_out'] == 2
    assert report['merges'] == [] and report['refusals'] == []                               # never tried, not refused
    assert k['merges'] == {'step1': 0, 'step2': 0, 'same_mask': 0, 'cover': 0, 'occluder_strip': 0, 'small_touch': 0}
    # a canopy over the 0.6 m gap does not bring the pair back: past touch_m a pair is never tried, in either step
    tree = obj('7ee0000000000003', rect(7.5, -1, 9.1, 7), kind='tree', score=.8)
    _, report = run([a, far, tree])
    assert report['kinds']['building']['pairs_tried'] == 0 and report['merges'] == []
    # at 0.45 m and at 0.5 m exactly the components are still one mask: the pair joins in step 1
    for x in (8.45, 8.5):
        _, report = run([a, obj('bbbb000000000002', rect(x, 0, x+3.4, 6), score=.4777)])
        assert report['kinds']['building']['merges'] == {**report['kinds']['building']['merges'], 'step1': 1, 'same_mask': 1}, x
        assert report['merges'][0]['step'] == 1 and abs(report['merges'][0]['gap_m']-(x-8)) < .01, x


# ------------------------------------------------------------------------------------------------ the vertex rule (item 2a)

def _arc_piece(oid, cx, cy, r, n, score):
    pts = [(cx+r*math.cos(math.pi*i/(n-1)), cy+r*math.sin(math.pi*i/(n-1))) for i in range(n)]      # a half disc, n vertices on the arc
    return obj(oid, pts, score=score)


def test_i_buildings_simplify_at_2_px_above_50_vertices_at_extraction_and_at_emission_other_kinds_at_150():
    assert ring_simplify_budget('building') is BUILDING_RING_SIMPLIFY and ring_simplify_budget('building')['vertices'] == 50
    for kind in ('tree', 'road', 'driveway', 'car', 'solar_panel', 'pool', 'vegetation', 'water', 'footpath', 'other'):
        assert ring_simplify_budget(kind) is RING_SIMPLIFY, kind
    arc = Polygon([(40*math.cos(math.pi*i/79), 40*math.sin(math.pi*i/79)) for i in range(80)])        # 80 vertices in pixels
    assert simplify_long_ring(arc, budget=ring_simplify_budget('tree')) is arc                        # under 150: untouched
    simplified = simplify_long_ring(arc, budget=ring_simplify_budget('building'))
    assert simplified is not arc and len(simplified.exterior.coords)-1 < 80 and abs(simplified.area-arc.area)/arc.area < .05
    source = (ML/'scene_extract.py').read_text()
    tree = ast.parse(source)
    extract, = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'extract_objects']
    calls = {getattr(c.func, 'id', getattr(c.func, 'attr', None)) for c in ast.walk(extract) if isinstance(c, ast.Call)}
    assert 'ring_simplify_budget' in calls and 'polygon.simplify(2, preserve_topology=True)' in source
    assert "budget['vertices']" in ast.get_source_segment(source, extract) and "budget['hole_vertices']" in ast.get_source_segment(source, extract)
    # at emission: a merged building ring over 50 vertices is simplified at 2 px (reported), one under 50 is not
    body = _arc_piece('arc0000000000001', 10, 10, 4, 70, .6)                                          # ~25 m2 half disc, 70 vertices
    speck = obj('5pk0000000000002', rect(5.4, 9, 5.9, 11), score=.4)                                   # 1 m2, 0.1 m from the flat side
    out, report = run([body, speck])
    feature, = features(out)
    frep, = report['features']
    assert frep['simplified']['applied'] is True and frep['simplified']['vertices_before'] > 50
    assert frep['vertices'] == frep['simplified']['vertices_after'] < frep['simplified']['vertices_before']
    assert len(feature['ring']) == frep['vertices'] and abs(frep['simplified']['area_change_pct']) < 5.   # the census's 2 px p90 is 2.3 %
    assert frep['ring_unsimplified'] and len(frep['ring_unsimplified']) == frep['simplified']['vertices_before']
    assert report['kinds']['building']['simplified'] == 1 and report['kinds']['building']['vertex_cap'] == 0 and frep['vertex_cap'] is False
    assert app_ring_issues(feature['ring'], feature.get('holes', []), *IMAGE) == []
    out, report = run([_arc_piece('arc0000000000003', 10, 10, 4, 30, .6), speck])
    assert report['features'][0].get('simplified') is None and report['kinds']['building']['simplified'] == 0


# -------------------------------------------------------------------------------------- pass-through, determinism, not called

def test_j_non_building_objects_and_unusable_rings_pass_through_byte_identical_in_order():
    a = obj('aaaa000000000001', rect(0, 0, 8, 6), score=.4777)
    b = obj('bbbb000000000002', rect(8.3, 0, 12, 6), score=.4777)
    others = [obj('caaa000000000003', rect(30, 30, 34, 32), kind='car', score=.9, odd_field={'x': [1, 2]}),
              obj('7ee0000000000004', rect(0, 20, 6, 26), kind='tree', status='confirmed', score=.8, holes=[px(rect(2, 22, 3, 23))]),
              obj('r0ad000000000005', rect(20, 20, 30, 30), kind='road', status='needs_adjustment', carried_from={'scene': 'x'}),
              {'id': 'bad0000000000006', 'kind': 'building', 'ring': [[0, 0]], 'status': 'candidate'},
              obj('p00l000000000007', rect(14, 0, 18, 4), kind='pool', score=.5)]
    objects = [others[0], a, others[1], b, others[2], others[3], others[4]]
    before = [json.dumps(o, sort_keys=True) for o in others]
    out, report = run(objects)
    assert [o for o in out if o['id'] in {x['id'] for x in others}] == others
    assert all(out[i] is objects[i] for i in (0, 2, 4, 5, 6))
    assert [json.dumps(o, sort_keys=True) for o in others] == before
    assert len(features(out)) == 1 and report['kinds']['building']['merges']['same_mask'] == 1
    assert [o['id'] for o in out[:7]] == [o['id'] for o in objects] and out[-1]['source'] == 'building-merge-v1'
    assert report['kinds']['building']['pieces_in'] == 3 and report['kinds']['building']['joinable'] == 2   # the one-point ring is not


def test_k_the_merge_is_deterministic_and_order_independent():
    house = obj('h0us000000000001', rect(0, 0, 10, 10), score=.7)
    shed = obj('5hed000000000002', rect(10.4, 0, 12.4, 2.5), score=.4)
    dup = obj('d0b1000000000003', rect(1, 1, 9, 9), score=.5)
    tree = obj('7ee0000000000004', rect(9.5, -1, 11, 4), kind='tree', score=.8)
    out1, report1 = run([house, shed, dup, tree])
    out2, report2 = run([house, shed, dup, tree])
    out3, report3 = run([tree, dup, shed, house])
    assert json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True)
    assert json.dumps(report1, sort_keys=True) == json.dumps(report2, sort_keys=True)
    assert {f['id'] for f in features(out1)} == {f['id'] for f in features(out3)}
    assert [f['ring'] for f in features(out1)] == [f['ring'] for f in features(out3)]
    feature, = features(out1)
    assert feature['replaces'] == sorted([house['id'], shed['id'], dup['id']])
    assert sorted(report1['features'][0]['rules']) == ['cover', 'occluder_strip', 'small_touch']   # the tree over the shed's strip fires too
    assert [o['id'] for o in out3[:4]] == [tree['id'], dup['id'], shed['id'], house['id']]


def test_l_nothing_in_the_extraction_path_calls_the_building_merge():
    source = (ML/'scene_extract.py').read_text()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, 'id', getattr(node.func, 'attr', None)) == 'merge_building_fragments']
    assert calls == [], [c.lineno for c in calls]
    definitions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'merge_building_fragments']
    assert len(definitions) == 1
    lines = source.splitlines()
    for lineno, line in enumerate(lines, 1):
        if 'merge_building_fragments' in line:
            stripped = line.strip()
            assert lineno == definitions[0].lineno or stripped.startswith('#') or 'NOT called' in line, (lineno, stripped)
    assert BUILDING_MERGE['enabled'] is False
    for path in sorted(ML.glob('*.py')):                       # the sidecar lives in experiments/; no module beside scene_extract calls it
        if path.name != 'scene_extract.py':
            assert 'merge_building_fragments' not in path.read_text(), path.name
    for entry in ('extract_objects', 'analyse_scene', 'build_scene', 'save_scene'):
        node, = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == entry]
        names = {getattr(c.func, 'id', getattr(c.func, 'attr', None)) for c in ast.walk(node) if isinstance(c, ast.Call)}
        assert 'merge_building_fragments' not in names and 'building_umbrellas' not in names, entry


# ------------------------------------------------------------- the v2 proposal flags (2026-10-07, after the by-eye tally; not approved)

def run_with(objects, design, image_size=IMAGE):
    before = json.dumps(objects, sort_keys=True)
    out, report = merge_building_fragments(objects, AFFINE, image_size=image_size, design=design)
    assert json.dumps(objects, sort_keys=True) == before, 'the input list must not be mutated'
    return out, report


def test_p_the_v2_proposal_dict_sits_beside_the_approved_defaults_which_keep_their_values():
    from scripts.ml.scene_extract import BUILDING_MERGE_V2
    flags = {'party_wall_guard_step1': True, 'step1_outside_gap_max_m': .1, 'small_touch': False}
    assert BUILDING_MERGE_V2 == {**BUILDING_MERGE, **flags}
    assert not any(flag in BUILDING_MERGE for flag in flags)                                 # v1 reads its defaults: absent / 0.5 / True
    assert BUILDING_MERGE_V2['enabled'] is False and BUILDING_MERGE_V2['source'] == 'building-merge-v1'
    assert 'party_wall_step1' in BUILDING_REFUSALS and 'step1_outside_gap' in BUILDING_REFUSALS
    source = (ML/'scene_extract.py').read_text()
    comment = source[source.index('BUILDING_MERGE = '):source.index('BUILDING_MERGE_V2 = ')]
    assert 'proposal after the 7 Oct by-eye tally, not approved' in comment and '2026-10-07' in comment


def test_q_step_1_party_wall_the_15_shape_is_refused_under_v2_and_joined_under_v1():
    from scripts.ml.scene_extract import BUILDING_MERGE_V2
    row = obj('r0w0000000000001', rect(0, 0, 29, 10), score=.4777)                          # 290 m2, the unit row
    unit = obj('un1t000000000002', rect(29.3, 0, 32.4, 10), score=.4777)                    # 31 m2, the same mask, 0.3 m beyond the seam
    out, report = run([row, unit])                                                           # v1: step 1 exempt from the party wall
    assert len(features(out)) == 1 and report['kinds']['building']['merges']['same_mask'] == 1 and report['refusals'] == []
    out, report = run_with([row, unit], BUILDING_MERGE_V2)                                   # v2: refused before any rule is read
    k = report['kinds']['building']
    assert features(out) == [] and out[0] is row and out[1] is unit and [o['status'] for o in out] == ['candidate', 'candidate']
    assert reasons(report) == ['party_wall_step1'] and k['refusals'] == {'party_wall_step1': 1} and k['features_out'] == 2
    refusal, = report['refusals']
    assert refusal['step'] == 1 and refusal['cover'] == 0. and abs(refusal['gap_m']-.3) < .01 and refusal['smaller_m2'] == 31.
    assert 'rule' not in refusal and 'party_wall_pieces' not in refusal
    assert k['merges'] == {'step1': 0, 'step2': 0, 'same_mask': 0, 'cover': 0, 'occluder_strip': 0, 'small_touch': 0}
    # the guard reads 20 m2 on BOTH sides: a 15 m2 component of the same mask 0.05 m away still rejoins under v2
    small = obj('5ma1000000000003', rect(29.05, 0, 30.55, 10), score=.4777)                 # 15 m2
    out, report = run_with([row, small], BUILDING_MERGE_V2)
    assert len(features(out)) == 1 and report['kinds']['building']['merges']['same_mask'] == 1 and report['refusals'] == []
    # two 20 m2 components that overlap (cover >= 0.05) are a torn mask, not a party wall: joined under v2
    torn = obj('t0rn000000000004', rect(27, 0, 32, 10), score=.4777)                        # 50 m2, 2 x 10 m inside the row (cover .4)
    out, report = run_with([row, torn], BUILDING_MERGE_V2)
    assert len(features(out)) == 1 and report['kinds']['building']['merges']['same_mask'] == 1
    # the flag off (v1's default): the outside-gap guard still holds the 0.3 m pair in step 1 (0.3 > 0.1) and step 2 reads it as the
    # party wall it is; with both flags at v1 it joins again
    _, report = run_with([row, unit], {**BUILDING_MERGE_V2, 'party_wall_guard_step1': False})
    assert reasons(report) == ['step1_outside_gap', 'party_wall'] and report['kinds']['building']['merges']['same_mask'] == 0
    _, report = run_with([row, unit], {**BUILDING_MERGE_V2, 'party_wall_guard_step1': False, 'step1_outside_gap_max_m': .5})
    assert report['kinds']['building']['merges']['same_mask'] == 1 and report['refusals'] == []


def test_r_step_1_a_component_wholly_outside_the_other_joins_under_v2_only_within_a_tenth_of_a_metre():
    from scripts.ml.scene_extract import BUILDING_MERGE_V2
    body = obj('b0dy000000000001', rect(0, 0, 8, 6), score=.4777)                            # 48 m2
    far = obj('far0000000000002', rect(8.49, 0, 11, 6), score=.4777)                          # 15.1 m2 (under the party-wall bar), 0.49 m away
    near = obj('near000000000003', rect(8.05, 0, 10.5, 6), score=.4777)                       # 0.05 m away
    over = obj('0ver000000000004', rect(7.8, 0, 10.5, 6), score=.4777)                        # overlapping 0.2 m
    for piece in (far, near, over):                                                           # v1 joins all three in step 1
        _, report = run([body, piece])
        assert report['kinds']['building']['merges'] == {**report['kinds']['building']['merges'], 'step1': 1, 'same_mask': 1}, piece['id']
        assert report['refusals'] == []
    out, report = run_with([body, far], BUILDING_MERGE_V2)
    k = report['kinds']['building']
    assert features(out) == [] and out[0] is body and out[1] is far and [o['status'] for o in out] == ['candidate', 'candidate']
    refusal = report['refusals'][0]
    assert refusal['reason'] == 'step1_outside_gap' and refusal['step'] == 1 and refusal['outside_gap_max_m'] == .1
    assert refusal['cover'] == 0. and abs(refusal['gap_m']-.49) < .01 and 'rule' not in refusal
    assert k['merges']['same_mask'] == 0 and k['features_out'] == 2
    # "not joined in step 1": step 2 still reads the pair under its own rules (small_touch off, nothing covers -> no_rule) ...
    assert reasons(report) == ['step1_outside_gap', 'no_rule'] and k['refusals'] == {'step1_outside_gap': 1, 'no_rule': 1}
    # ... so a canopy over the 0.49 m gap bridges it by the occluder-strip rule in step 2
    out, report = run_with([body, far, obj('7ee0000000000005', rect(7.5, -1, 9, 7), kind='tree', score=.8)], BUILDING_MERGE_V2)
    assert len(features(out)) == 1 and report['kinds']['building']['merges'] == {**report['kinds']['building']['merges'], 'step2': 1, 'occluder_strip': 1}
    assert reasons(report) == ['step1_outside_gap'] and features(out)[0]['merged_from'][0]['rule'] == 'occluder_strip'
    for piece in (near, over):                                                                # abutting or overlapping components still join
        out, report = run_with([body, piece], BUILDING_MERGE_V2)
        assert len(features(out)) == 1 and report['kinds']['building']['merges']['same_mask'] == 1 and report['refusals'] == [], piece['id']
    edge = obj('edge000000000006', rect(8.1, 0, 10.5, 6), score=.4777)                        # 0.1 m exactly: not over the bar
    _, report = run_with([body, edge], BUILDING_MERGE_V2)
    assert report['kinds']['building']['merges']['same_mask'] == 1 and report['refusals'] == []
    _, report = run_with([body, far], {**BUILDING_MERGE_V2, 'step1_outside_gap_max_m': .5})   # v1's default value: joins
    assert report['kinds']['building']['merges']['same_mask'] == 1 and report['refusals'] == []


def test_s_small_touch_off_under_v2_leaves_a_touching_5_m2_piece_alone_while_cover_still_joins():
    from scripts.ml.scene_extract import BUILDING_MERGE_V2
    house = obj('h0us000000000001', rect(0, 0, 10, 10), score=.7)                             # 100 m2
    shed = obj('5hed000000000002', rect(10.4, 0, 12.4, 2.5), score=.4)                        # 5 m2, 0.4 m away (test_d's pair)
    _, report = run([house, shed])
    assert report['kinds']['building']['merges']['small_touch'] == 1
    out, report = run_with([house, shed], BUILDING_MERGE_V2)
    k = report['kinds']['building']
    assert features(out) == [] and out[0] is house and out[1] is shed and [o['status'] for o in out] == ['candidate', 'candidate']
    assert reasons(report) == ['no_rule'] and k['refusals'] == {'no_rule': 1} and k['features_out'] == 2 and k['pairs_tried'] == 1
    assert k['merges'] == {'step1': 0, 'step2': 0, 'same_mask': 0, 'cover': 0, 'occluder_strip': 0, 'small_touch': 0}
    touching = obj('70uc000000000003', rect(10, 0, 12, 2.5), score=.4)                        # gap 0, cover 0: still no rule
    _, report = run_with([house, touching], BUILDING_MERGE_V2)
    assert reasons(report) == ['no_rule']
    sliver = obj('51iv000000000004', rect(9.8, 9, 12, 11.2), score=.4)                        # cover .04 (#5's shape): no rule under v2
    _, report = run_with([house, sliver], BUILDING_MERGE_V2)
    assert reasons(report) == ['no_rule'] and abs(report['refusals'][0]['cover']-.041) < .01
    body = obj('b0d7000000000005', rect(5, 0, 14, 9), score=.62)                              # test_b's cover pair joins under v2
    slice_ = obj('51ce000000000006', rect(3.7, 0, 8, 3), score=.44)                           # 12.9 m2, cover .698
    out, report = run_with([body, slice_], BUILDING_MERGE_V2)
    feature, = features(out)
    assert [m['rule'] for m in feature['merged_from']] == ['cover', 'cover'] and report['kinds']['building']['merges']['cover'] == 1
    small_dup = obj('5dup000000000007', rect(8, 0, 10, 2.5), score=.4)                        # 5 m2 fully inside the house: cover 1 joins
    out, report = run_with([house, small_dup], BUILDING_MERGE_V2)
    assert len(features(out)) == 1 and report['merges'][0]['rules'] == ['cover']               # small_touch no longer listed beside it
    _, report = run([house, small_dup])
    assert report['merges'][0]['rules'] == ['cover', 'small_touch']                            # v1 lists both
    _, report = run_with([house, shed], {**BUILDING_MERGE_V2, 'small_touch': True})           # the flag back on: v1's small_touch
    assert report['kinds']['building']['merges']['small_touch'] == 1
