"""merge_linear_fragments (scene_extract): the authorised linear-continuity post-processing merge (2026-10-03 "yes to defaults").

Synthetic geometry is drawn in metres and converted to pixels through a known pixel_to_meters matrix (0.1 m/px, y flipped,
origin at 50 m), the same shape the scenes store. Every expectation is a count, a status or a geometry; nothing here is accuracy.
"""
import ast
import json
import re
from pathlib import Path

import numpy as np
from scripts.ml.scene_extract import (
    APP_ISSUE_AREA,
    APP_ISSUE_BOUNDS,
    APP_ISSUE_BUDGET,
    APP_ISSUE_CROSS,
    APP_ISSUE_GAP_DISCONNECT,
    APP_ISSUE_GAP_OUTSIDE,
    APP_ISSUE_GAP_OVERLAP,
    APP_ISSUE_OVERLAP,
    APP_ISSUE_POINTS,
    APP_ISSUE_REMAINING_AREA,
    APP_RING_RULES_UNCAPPED,
    LINEAR_MERGE,
    LINEAR_REFUSALS,
    RING_SIMPLIFY,
    app_ring_issues,
    finalise_app_ring,
    merge_linear_fragments,
    simplify_long_ring,
)
from shapely.geometry import Polygon

ML = Path(__file__).resolve().parents[1]
AFFINE = np.array([[.1, 0.], [0., -.1], [0., 50.]])     # pixel -> metres: x_m = .1 px, y_m = 50 - .1 py
SPACING = .1


def px(points_m):
    return [[round(x/SPACING, 2), round((50.-y)/SPACING, 2)] for x, y in points_m]


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def metric(obj):
    ring = np.asarray(obj['ring'], float)
    return Polygon(np.c_[ring, np.ones(len(ring))] @ AFFINE,
                   [np.c_[np.asarray(h, float), np.ones(len(h))] @ AFFINE for h in obj.get('holes') or []])


def obj(oid, kind, ring_m, status='candidate', score=.5, **extra):
    shape = Polygon(ring_m)
    return {'id': oid, 'kind': kind, 'label': kind.capitalize(), 'ring': px(ring_m), 'status': status, 'geometry_reviewed': False,
            'source': 'sam3-text-onnx', 'score': score, 'prompt': kind,
            'model': {'area_m2': round(shape.area, 2), 'color': '#808080', 'height_m': .05}, **extra}


def run(objects):
    before = json.dumps(objects, sort_keys=True)
    out, report = merge_linear_fragments(objects, AFFINE)
    assert json.dumps(objects, sort_keys=True) == before, 'the input list must not be mutated'
    return out, report


def features(out):
    return [o for o in out if o.get('source') == LINEAR_MERGE['source']]


def test_design_constants_are_the_authorised_defaults_and_disabled():
    assert LINEAR_MERGE['kinds'] == ('road', 'footpath', 'driveway')
    assert LINEAR_MERGE['d_m'] == {'driveway': .6, 'footpath': .8, 'road': 1.}
    assert LINEAR_MERGE['bridge_max_ratio'] == 2. and LINEAR_MERGE['union_ratio_max'] == 1.15
    # 19:40 "yes to defaults" on the marks card: water + building block, result floor 1 m2, 2 px simplification above 150.
    assert LINEAR_MERGE['blocker_kinds'] == ('car', 'tree', 'vegetation', 'pool', 'water', 'building')
    assert LINEAR_MERGE['decided_statuses'] == ('confirmed', 'rejected', 'needs_adjustment', 'superseded')
    assert LINEAR_MERGE['result_floor_m2'] == 1.
    assert LINEAR_MERGE['score_round'] == 4 and LINEAR_MERGE['enabled'] is False
    assert set(LINEAR_REFUSALS) >= {'no_bridge', 'not_connected', 'bridge_too_large', 'union_ratio', 'touches_blocker',
                                    'touches_decided', 'invalid_after_rounding', 'result_floor'}
    assert RING_SIMPLIFY == {'vertices': 150, 'hole_vertices': 200, 'tolerance_px': 2., 'cap': 200, 'max_holes': 32, 'total': 2000}
    source = (ML/'scene_extract.py').read_text()
    assert 'polygon.simplify(2, preserve_topology=True)' in source            # the extraction path's inline original (extract_objects)


def test_a_one_mask_split_by_plain_pavement_rejoins_in_step_1_with_lineage():
    a = obj('aaaa000000000001', 'driveway', rect(0, 0, 10, 3), score=.4777)
    b = obj('bbbb000000000002', 'driveway', rect(10.4, 0, 20, 3), score=.4777)     # 0.4 m of pavement between the two components
    out, report = run([a, b])
    assert len(out) == 3
    feature, = features(out)
    members = [o for o in out if o['id'] in (a['id'], b['id'])]
    assert all(m['status'] == 'superseded' and m['replacement_ids'] == [feature['id']] for m in members)
    assert {k: v for k, v in members[0].items() if k not in ('status', 'replacement_ids')} == {k: v for k, v in a.items() if k != 'status'}
    assert feature['status'] == 'candidate' and feature['kind'] == 'driveway' and feature['score'] == .4777
    assert feature['replaces'] == feature['derived_from'] == [a['id'], b['id']]
    assert [m['id'] for m in feature['merged_from']] == [a['id'], b['id']]
    assert all(m['step'] == 1 and abs(m['gap_m']-.4) < .01 for m in feature['merged_from'])
    assert len(feature['id']) == 16 and re.fullmatch(r'[0-9a-f]{16}', feature['id'])
    assert feature['geometry_reviewed'] is False and feature['prompt'] == 'driveway' and feature['label'] == 'Driveway'
    shape, pieces = metric(feature), metric(a).union(metric(b))
    assert abs(pieces.area-58.8) < 1e-6                                      # 10 x 3 + 9.6 x 3: the bridge is extra
    assert shape.is_valid and pieces.area < shape.area < pieces.area*1.15 and abs(feature['model']['area_m2']-shape.area) < .02
    assert abs(shape.area-pieces.area-report['merges'][0]['bridge_m2']) < .01
    assert shape.contains(metric(a).buffer(-.01)) and shape.contains(metric(b).buffer(-.01))
    xs, ys = zip(*feature['ring'], strict=True)
    assert min(xs) == 0 and max(xs) == 200 and min(ys) == 470 and max(ys) == 500      # pixels, stored precision
    assert all(len(str(v).split('.')[-1]) <= 2 for v in xs+ys)
    k = report['kinds']['driveway']
    assert k['pieces_in'] == 2 and k['candidates'] == 2 and k['features_out'] == 1 and k['multi_piece_features'] == 1
    assert k['merges'] == {'step1': 1, 'step2': 0} and k['refusals'] == {}
    assert k['gaps_closed_m']['n'] == 1 and abs(k['gaps_closed_m']['median']-.4) < .01 and k['bridge_area_m2'] > 1.
    assert report['merges'][0]['step'] == 1 and report['merges'][0]['kind'] == 'driveway'


def test_b_a_speck_near_a_body_is_refused_by_the_two_x_bridge_rule():
    body = obj('b0d7000000000001', 'driveway', rect(0, 0, 10, 4), score=.4777)                                  # 40 m2
    side = (.15)**.5
    speck = obj('5bec000000000002', 'driveway', rect(10.4, 1.8, 10.4+side, 1.8+side), score=.4777)               # 0.15 m2, 0.4 m away
    assert abs(metric(speck).area-.15) < .01
    out, report = run([body, speck])
    assert features(out) == [] and [o['status'] for o in out] == ['candidate', 'candidate']
    refusal, = report['refusals']
    assert refusal['reason'] == 'bridge_too_large' and refusal['step'] == 1
    assert refusal['bridge_m2'] > 2*.15 and refusal['smaller_side_m2'] < .16 and refusal['bridge_ratio'] > 2
    assert report['kinds']['driveway']['refusals'] == {'bridge_too_large': 1}
    assert report['kinds']['driveway']['merges'] == {'step1': 0, 'step2': 0} and report['kinds']['driveway']['features_out'] == 2


def test_b2_a_speck_welded_onto_a_body_cannot_anchor_a_bridge_seven_times_its_size():
    """Repair r2 (skeptic finding 1): the 2 x rule reads the smallest ORIGINAL PIECE the bridge touches, not the smaller
    agglomerated side. win-a74017cfd3b50b29 road 1b0fd2b3d1681d29: a 0.26 m2 speck joined a body, then the cluster took a
    1.8 m2 and a 7.2 m2 bridge that both touched the speck (7x / 27.5x it) because the cluster side was 4.9 / 89 m2."""
    body = obj('b0d7000000000001', 'road', rect(0, 0, 10, 4), score=.5)                                   # 40 m2
    speck = obj('5bec000000000002', 'road', rect(10.1, 1.75, 10.6, 2.25), score=.5)                       # 0.25 m2, 0.1 m off the body
    other = obj('07e2000000000003', 'road', rect(11.5, 0, 21.5, 4), score=.5)                             # 40 m2, 0.9 m from the speck
    out, report = run([body, speck, other])
    feature, = features(out)
    assert feature['replaces'] == [speck['id'], body['id']]                                              # step 1: speck + body only
    assert [o['status'] for o in out[:3]] == ['superseded', 'superseded', 'candidate']
    refusal, = report['refusals']
    assert refusal['reason'] == 'bridge_too_large' and refusal['step'] == 1
    assert set(refusal['a_ids']) | set(refusal['b_ids']) == {body['id'], speck['id'], other['id']}
    assert abs(refusal['smallest_piece_m2']-.25) < .01                                                    # the speck the bridge touches
    assert refusal['smaller_side_m2'] > 30                                                                 # the cluster rule would have passed
    assert 2*.25 < refusal['bridge_m2'] < 2*refusal['smaller_side_m2'] and refusal['bridge_ratio'] > 2
    k = report['kinds']['road']
    assert k['features_out'] == 2 and k['merges'] == {'step1': 1, 'step2': 0} and k['refusals'] == {'bridge_too_large': 1}
    assert k['pieces_superseded'] == 2
    merge, = report['merges']
    assert merge['bridge_ratio'] <= 2 and abs(merge['smallest_piece_m2']-.25) < .01


def test_b3_the_reported_bridge_area_is_the_surface_added_not_the_sum_of_fills():
    """Repair r2 (skeptic finding 3): a later member landing on an earlier fill must not count the fill twice."""
    a = obj('aaaa000000000001', 'road', rect(0, 0, 10, 4), score=.5)
    b = obj('bbbb000000000002', 'road', rect(10.6, 0, 20, 4), score=.5)            # step 1 fills the 0.6 x 4 m gap first
    c = obj('cccc000000000003', 'road', rect(10.2, 3, 10.5, 8), score=.45)         # step 2: 1.5 m2 strip standing on 0.3 m2 of that fill
    out, report = run([a, b, c])
    feature, = features(out)
    assert len(feature['merged_from']) == 3 and [m['step'] for m in report['merges']] == [1, 2]
    added = metric(feature).area-metric(a).union(metric(b)).union(metric(c)).area
    frep, = report['features']
    assert abs(frep['bridge_m2']-added) < .01 and abs(report['kinds']['road']['bridge_area_m2']-added) < .01
    assert sum(m['bridge_m2'] for m in report['merges']) > added+.1                 # the per-pair fills overlap; their sum overstates


def test_b4_a_ring_that_collapses_on_rounding_is_refused_and_no_feature_is_emitted():
    """Repair r2 (skeptic finding 2): the make_valid-after-rounding guard. A piece whose two halves hang on a 0.003 px neck
    (sub-0.005 px, so the neck's edges round onto one line) joins a neighbour; the merged ring at ring_decimals = 2 is
    self-touching, make_valid splits it into two parts of 25 and 52 m2 and the second part is far over valid_area_tol."""
    dumbbell = {'id': 'd0bb000000000001', 'kind': 'driveway', 'label': 'Driveway', 'status': 'candidate', 'geometry_reviewed': False,
                'source': 'sam3-text-onnx', 'score': .5, 'prompt': 'driveway', 'model': {'area_m2': 50., 'color': '#808080'},
                'ring': [[0, 450], [50, 450], [50, 475], [60, 475], [60, 450], [110, 450], [110, 500], [60, 500],
                         [60, 475.003], [50, 475.003], [50, 500], [0, 500]]}
    assert Polygon(dumbbell['ring']).is_valid and abs(metric(dumbbell).area-50.) < .01
    neighbour = obj('0e16000000000002', 'driveway', rect(11.4, 0, 16.4, 5), score=.5)      # 25 m2, 0.4 m right of the dumbbell
    out, report = run([dumbbell, neighbour])
    assert features(out) == [] and [o['status'] for o in out] == ['candidate', 'candidate']
    refusal, = report['refusals']
    assert refusal['reason'] == 'invalid_after_rounding' and refusal['step'] == 1
    assert refusal['bridge_ratio'] <= 2 and refusal['union_ratio'] <= 1.15 and 'blockers' not in refusal
    assert report['kinds']['driveway'] == {**report['kinds']['driveway'], 'features_out': 2, 'merges': {'step1': 0, 'step2': 0},
                                           'refusals': {'invalid_after_rounding': 1}, 'bridge_area_m2': 0.}
    assert all(o.get('source') != LINEAR_MERGE['source'] for o in out)                  # no invalid ring reaches the layer


def test_b5_a_ring_that_make_valid_repairs_within_tolerance_is_accepted_with_the_repaired_ring():
    """The other half of the guard: a 0.09 m2 tag hanging on a 0.003 px neck (0.17 % of the feature) collapses on rounding;
    make_valid separates it, the area moves under valid_area_tol, and the feature is emitted with the valid main ring."""
    tagged = {'id': '7a99000000000001', 'kind': 'driveway', 'label': 'Driveway', 'status': 'candidate', 'geometry_reviewed': False,
              'source': 'sam3-text-onnx', 'score': .5, 'prompt': 'driveway', 'model': {'area_m2': 25.09, 'color': '#808080'},
              'ring': [[0, 450], [50, 450], [50, 475], [51, 475], [51, 473.5], [54, 473.5], [54, 476.5], [51, 476.5],
                       [51, 475.003], [50, 475.003], [50, 500], [0, 500]]}
    assert Polygon(tagged['ring']).is_valid and abs(metric(tagged).area-25.09) < .01
    below = obj('be10000000000002', 'driveway', rect(0, -5.4, 5, -.4), score=.5)         # 25 m2, 0.4 m under the square
    out, report = run([tagged, below])
    feature, = features(out)
    assert report['refusals'] == [] and report['kinds']['driveway']['merges'] == {'step1': 1, 'step2': 0}
    shape = metric(feature)
    assert Polygon(feature['ring'], feature.get('holes')).is_valid and shape.is_valid
    assert abs(shape.area-52.) < .05                                                    # square + 2 m2 bridge + below; the tag is gone
    assert all(len(str(v).split('.')[-1]) <= 2 for xy in feature['ring'] for v in xy)


def test_c_a_bridge_crossing_a_car_is_refused():
    a = obj('aaaa000000000001', 'road', rect(0, 0, 10, 4), score=.5)
    b = obj('bbbb000000000002', 'road', rect(10.5, 0, 20, 4), score=.5)
    car = obj('caaa000000000003', 'car', rect(10.1, .5, 10.4, 2.5), score=.9)
    out, report = run([a, b, car])
    assert features(out) == []
    refusal, = report['refusals']
    assert refusal['reason'] == 'touches_blocker' and refusal['blockers'] == [{'id': car['id'], 'kind': 'car'}]
    assert out[2] is car


def test_d_a_bridge_touching_a_candidate_footpath_piece_refuses_a_driveway_join():
    a = obj('aaaa000000000001', 'driveway', rect(0, 0, 10, 3), score=.5)
    b = obj('bbbb000000000002', 'driveway', rect(10.5, 0, 20, 3), score=.5)
    path = obj('f00d000000000003', 'footpath', rect(10.1, 1, 10.4, 2), score=.7)
    out, report = run([a, b, path])
    assert features(out) == []
    reasons = {(r['kind'], r['reason']) for r in report['refusals']}
    assert ('driveway', 'touches_blocker') in reasons
    driveway, = [r for r in report['refusals'] if r['kind'] == 'driveway']
    assert driveway['blockers'] == [{'id': path['id'], 'kind': 'footpath'}]
    assert report['kinds']['footpath']['features_out'] == 1 and report['kinds']['footpath']['merges'] == {'step1': 0, 'step2': 0}


def test_e_a_confirmed_piece_never_merges_and_blocks_a_bridge():
    confirmed = obj('c0f1000000000001', 'driveway', rect(0, 0, 10, 3), status='confirmed', score=.4777, geometry_reviewed=True)
    candidate = obj('ca9d000000000002', 'driveway', rect(10.4, 0, 20, 3), score=.4777)
    out, report = run([confirmed, candidate])
    assert out == [confirmed, candidate] and out[0] is confirmed
    assert report['kinds']['driveway'] == {**report['kinds']['driveway'], 'pieces_in': 2, 'candidates': 1, 'features_out': 1,
                                           'merges': {'step1': 0, 'step2': 0}, 'refusals': {}}
    a = obj('aaaa000000000001', 'driveway', rect(0, 0, 10, 3), score=.5)
    b = obj('bbbb000000000002', 'driveway', rect(10.5, 0, 20, 3), score=.5)
    decided = obj('dec1000000000003', 'driveway', rect(10.1, .5, 10.4, 2.5), status='confirmed', score=.3)
    out, report = run([a, b, decided])
    assert features(out) == [] and out[2] is decided
    refusal, = report['refusals']
    assert refusal['reason'] == 'touches_decided'
    assert refusal['blockers'] == [{'id': decided['id'], 'kind': 'driveway', 'status': 'confirmed'}]
    for status in ('rejected', 'needs_adjustment', 'superseded'):          # any class: 'other' is not a blocker kind
        blocker = obj('dec2000000000004', 'other', rect(10.1, .5, 10.4, 2.5), status=status, score=.3)
        _, report = run([a, b, blocker])
        assert [r['reason'] for r in report['refusals']] == ['touches_decided'], status


def test_f_a_union_ratio_over_1_15_is_refused():
    a = obj('aaaa000000000001', 'driveway', rect(0, 0, 1, 1), score=.5)
    b = obj('bbbb000000000002', 'driveway', rect(1.5, 0, 2.5, 1), score=.5)
    out, report = run([a, b])
    assert features(out) == []
    refusal, = report['refusals']
    assert refusal['reason'] == 'union_ratio' and refusal['union_ratio'] > 1.15 and refusal['bridge_ratio'] <= 2
    assert report['kinds']['driveway']['refusals'] == {'union_ratio': 1}


def test_g_step_2_joins_two_different_score_candidates_of_one_kind():
    a = obj('aaaa000000000001', 'road', rect(0, 0, 10, 4), score=.6)
    b = obj('bbbb000000000002', 'road', rect(10.5, 0, 20, 4), score=.45)
    out, report = run([a, b])
    feature, = features(out)
    assert feature['score'] == .6 and feature['kind'] == 'road'
    assert all(m['step'] == 2 and abs(m['gap_m']-.5) < .01 for m in feature['merged_from'])
    assert report['kinds']['road']['merges'] == {'step1': 0, 'step2': 1}
    assert report['merges'][0]['step'] == 2


def test_h_classes_never_mix():
    road = obj('aaaa000000000001', 'road', rect(0, 0, 10, 3), score=.5)
    driveway = obj('bbbb000000000002', 'driveway', rect(10.3, 0, 20, 3), score=.5)
    path = obj('cccc000000000003', 'footpath', rect(0, 3.2, 20, 4.5), score=.5)
    out, report = run([road, driveway, path])
    assert out == [road, driveway, path] and features(out) == []
    assert report['merges'] == [] and report['refusals'] == []
    assert all(report['kinds'][k]['features_out'] == 1 for k in ('road', 'driveway', 'footpath'))


def test_i_non_linear_objects_pass_through_byte_identical():
    a = obj('aaaa000000000001', 'footpath', rect(0, 0, 10, 1.5), score=.5)
    b = obj('bbbb000000000002', 'footpath', rect(10.5, 0, 20, 1.5), score=.5)
    others = [obj('caaa000000000003', 'car', rect(30, 30, 34, 32), score=.9, odd_field={'x': [1, 2]}),
              obj('7ee0000000000004', 'tree', rect(0, 20, 6, 26), status='confirmed', score=.8, holes=[px(rect(2, 22, 3, 23))]),
              obj('b1d0000000000005', 'building', rect(20, 20, 30, 30), status='needs_adjustment', carried_from={'scene': 'x'}),
              {'id': 'bad0000000000006', 'kind': 'water', 'ring': [[0, 0]], 'status': 'candidate'}]
    objects = [others[0], a, others[1], b, others[2], others[3]]
    before = [json.dumps(o, sort_keys=True) for o in others]
    out, report = run(objects)
    assert [o for o in out if o['kind'] not in LINEAR_MERGE['kinds']] == others
    assert all(out[i] is objects[i] for i in (0, 2, 4, 5))
    assert [json.dumps(o, sort_keys=True) for o in others] == before
    assert len(features(out)) == 1 and report['kinds']['footpath']['merges'] == {'step1': 1, 'step2': 0}
    assert [o['id'] for o in out[:6]] == [o['id'] for o in objects]           # input order kept, features appended


def test_j_the_merge_is_deterministic_and_order_independent():
    a = obj('aaaa000000000001', 'road', rect(0, 0, 10, 4), score=.6)
    b = obj('bbbb000000000002', 'road', rect(10.5, 0, 20, 4), score=.45)
    c = obj('cccc000000000003', 'road', rect(20.6, 0, 30, 4), score=.45)
    out1, report1 = run([a, b, c])
    out2, report2 = run([a, b, c])
    out3, report3 = run([c, a, b])
    assert json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True)
    assert json.dumps(report1, sort_keys=True) == json.dumps(report2, sort_keys=True)
    assert {f['id'] for f in features(out1)} == {f['id'] for f in features(out3)}
    assert [f['ring'] for f in features(out1)] == [f['ring'] for f in features(out3)]
    feature, = features(out1)
    assert feature['replaces'] == [a['id'], b['id'], c['id']] and len(feature['replaces']) == 3


def test_k_nothing_in_the_extraction_path_calls_the_merge():
    source = (ML/'scene_extract.py').read_text()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, 'id', getattr(node.func, 'attr', None)) == 'merge_linear_fragments']
    assert calls == [], [c.lineno for c in calls]
    definitions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'merge_linear_fragments']
    assert len(definitions) == 1
    mentions = [i for i, line in enumerate(source.splitlines(), 1) if 'merge_linear_fragments' in line]
    def_line = definitions[0].lineno
    for lineno in mentions:
        line = source.splitlines()[lineno-1].strip()
        assert lineno == def_line or line.startswith('#') or 'NOT called' in line, (lineno, line)
    # 2026-10-03 19:40 "yes to defaults" (card item 6): the join is promoted through scene_linear_promote.py, an explicit dated
    # opt-in for WINDOW re-extracts; that module is the only other caller allowed. The extraction path itself stays clean.
    allowed_callers = {'scene_extract.py', 'scene_linear_promote.py'}
    for path in sorted(ML.glob('*.py')):
        if path.name not in allowed_callers:
            assert 'merge_linear_fragments' not in path.read_text(), path.name
    for entry in ('extract_objects', 'analyse_scene', 'build_scene', 'save_scene'):
        node, = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == entry]
        names = {getattr(c.func, 'id', getattr(c.func, 'attr', None)) for c in ast.walk(node) if isinstance(c, ast.Call)}
        assert 'merge_linear_fragments' not in names, entry


def test_l_the_result_floor_refuses_a_two_speck_feature_under_1_m2():
    """19:40 card item 2 (marks lines 5 and 8): 0.3 + 0.35 m2 specks make a 0.72 m2 'driveway'; every bridge guard passes
    (bridge 0.075 m2 = 0.25 x the smaller piece, union ratio 1.11) and the floor refuses the cluster whole."""
    a = obj('aaaa000000000001', 'driveway', rect(0, 0, .6, .5), score=.5)                 # 0.30 m2
    b = obj('bbbb000000000002', 'driveway', rect(.75, 0, 1.45, .5), score=.5)             # 0.35 m2, 0.15 m away
    out, report = run([a, b])
    assert features(out) == [] and out == [a, b] and out[0] is a and out[1] is b         # the pieces exactly as they were
    refusal, = report['refusals']
    assert refusal['reason'] == 'result_floor' and refusal['step'] == 'result' and refusal['ids'] == [a['id'], b['id']]
    assert .7 < refusal['feature_m2'] < .75 and abs(refusal['largest_member_m2']-.35) < .01 and refusal['floor_m2'] == 1.
    k = report['kinds']['driveway']
    assert k['features_out'] == 2 and k['multi_piece_features'] == 0 and k['pieces_superseded'] == 0
    assert k['merges'] == {'step1': 0, 'step2': 0} and k['merges_undone_by_floor'] == 1 and k['refusals'] == {'result_floor': 1}
    assert k['bridge_area_m2'] == 0. and k['gaps_closed_m']['n'] == 0
    merge, = report['merges']
    assert merge['undone'] == 'result_floor' and merge['bridge_ratio'] <= 2 and merge['union_ratio'] <= 1.15


def test_l2_the_result_floor_refuses_a_2_6_m2_feature_whose_largest_member_is_0_9_m2():
    """Marks line 2 (three 0.61 / 1.04 / 0.64 m2 slivers): the feature clears 1 m2 but no member does."""
    a = obj('aaaa000000000001', 'footpath', rect(0, 0, 1.5, .6), score=.5)                # 0.90 m2
    b = obj('bbbb000000000002', 'footpath', rect(1.6, 0, 3.1, .6), score=.5)              # 0.90 m2, 0.1 m gap
    c = obj('cccc000000000003', 'footpath', rect(3.2, 0, 4.4, .6), score=.5)              # 0.72 m2, 0.1 m gap
    out, report = run([a, b, c])
    assert features(out) == [] and out == [a, b, c] and all(out[i] is o for i, o in enumerate((a, b, c)))
    refusal, = report['refusals']
    assert refusal['reason'] == 'result_floor' and refusal['ids'] == [a['id'], b['id'], c['id']]
    assert 2.5 < refusal['feature_m2'] < 2.7 and abs(refusal['largest_member_m2']-.9) < .01
    k = report['kinds']['footpath']
    assert k['features_out'] == 3 and k['merges'] == {'step1': 0, 'step2': 0} and k['merges_undone_by_floor'] == 2
    assert k['refusals'] == {'result_floor': 1} and all(m['undone'] == 'result_floor' for m in report['merges'])


def test_l3_the_result_floor_accepts_a_35_m2_body_with_a_0_55_m2_speck():
    """Marks line 7 (win-ab373041656385bf driveway 9de493cd7792d847): the feature and its largest member both clear 1 m2."""
    body = obj('b0d7000000000001', 'driveway', rect(0, 0, 10, 3.5), score=.5)              # 35 m2
    speck = obj('5bec000000000002', 'driveway', rect(10.3, 1, 11.3, 1.55), score=.5)       # 0.55 m2, 0.3 m away
    out, report = run([body, speck])
    feature, = features(out)
    assert report['refusals'] == [] and feature['replaces'] == [speck['id'], body['id']]
    assert [o['status'] for o in out[:2]] == ['superseded', 'superseded']
    frep, = report['features']
    assert abs(frep['largest_member_m2']-35.) < .01 and frep['area_m2'] > 35.5 and frep['vertex_cap'] is False and 'simplified' not in frep
    k = report['kinds']['driveway']
    assert k['merges'] == {'step1': 1, 'step2': 0} and k['merges_undone_by_floor'] == 0 and k['refusals'] == {}
    assert k['features_out'] == 1 and k['pieces_superseded'] == 2 and k['simplified'] == 0 and k['vertex_cap'] == 0


def test_l4_a_speck_pair_welded_onto_a_body_passes_the_floor_through_the_body():
    """The floor reads the agglomerated cluster: two specks that first join each other and then the body are one feature
    whose largest member is the body; the same specks without the body are refused."""
    s1 = obj('5bec000000000001', 'road', rect(10.2, 1, 11., 1.5), score=.5)               # 0.40 m2, 0.2 m from the body
    s2 = obj('5bec000000000002', 'road', rect(11.15, 1, 12.05, 1.5), score=.5)            # 0.45 m2, 0.15 m from s1 (pair 0.85 m2)
    body = obj('b0d7000000000003', 'road', rect(0, 0, 10, 4), score=.45)                   # 40 m2
    out, report = run([s1, s2, body])
    feature, = features(out)
    assert set(feature['replaces']) == {s1['id'], s2['id'], body['id']} and report['refusals'] == []
    out, report = run([s1, s2])
    assert features(out) == [] and [r['reason'] for r in report['refusals']] == ['result_floor']


def test_m_water_and_building_candidates_block_a_bridge():
    """19:40 card item 4 (marks line 2: a bridge inside a 48 m2 water candidate)."""
    for kind in ('water', 'building'):
        a = obj('aaaa000000000001', 'road', rect(0, 0, 10, 4), score=.5)
        b = obj('bbbb000000000002', 'road', rect(10.5, 0, 20, 4), score=.5)
        blocker = obj('b10c000000000003', kind, rect(9, -1, 12, 5), score=.4)              # candidate, covers the gap
        out, report = run([a, b, blocker])
        assert features(out) == [] and out[2] is blocker, kind
        refusal, = report['refusals']
        assert refusal['reason'] == 'touches_blocker' and refusal['blockers'] == [{'id': blocker['id'], 'kind': kind}], kind
        assert report['kinds']['road']['refusals'] == {'touches_blocker': 1}
    # a water candidate away from the bridge does not block; a rejected one on the bridge blocks as a decided object (test_e)
    far = obj('b10c000000000003', 'water', rect(14, 6, 17, 9), score=.4)
    out, report = run([a, b, far])
    assert len(features(out)) == 1 and report['refusals'] == []
    rejected = obj('b10c000000000003', 'water', rect(9, -1, 12, 5), status='rejected', score=.4)
    out, report = run([a, b, rejected])
    assert features(out) == [] and [r['reason'] for r in report['refusals']] == ['touches_decided']


def wavy_rect(x0, y0, x1, y1, n, amplitude):
    """A rectangle whose top edge is sampled at n points alternating y1 / y1 + amplitude (metres): n + 3 vertices."""
    xs = np.linspace(x0, x1, n)
    top = [(float(x), y1 + (amplitude if i % 2 else 0.)) for i, x in enumerate(xs)]
    return [(x0, y0), (x1, y0), *reversed(top)]


def test_n_a_220_vertex_merged_ring_is_simplified_at_2_px_below_200_with_the_area_within_half_a_percent():
    """19:40 card item 5: two 110-vertex pieces (a 0.1 px wobble along the top edge, as a mask contour carries) merge into a
    ring over 150 vertices; simplify_long_ring (the extraction path's 2 px, preserve_topology) brings it under the 200 cap."""
    a = obj('aaaa000000000001', 'driveway', wavy_rect(0, 0, 10, 3, 110, .01), score=.5)
    b = obj('bbbb000000000002', 'driveway', wavy_rect(10.4, 0, 20, 3, 110, .01), score=.5)
    assert len(a['ring']) == 112 and len(b['ring']) == 112
    out, report = run([a, b])
    feature, = features(out)
    frep, = report['features']
    assert frep['simplified']['applied'] is True and frep['simplified']['vertices_before'] > 200
    assert frep['vertices'] == len(feature['ring']) < 200 and frep['vertex_cap'] is False and report['vertex_cap'] == []
    pieces = metric(a).union(metric(b)).area
    shape = metric(feature)
    assert shape.is_valid and Polygon(feature['ring']).is_valid
    unsimplified = pieces + report['merges'][0]['bridge_m2']
    assert abs(shape.area-unsimplified)/unsimplified < .005                           # area moves < 0.5 %
    assert abs(frep['simplified']['area_change_pct']) < .5
    assert all(len(str(v).split('.')[-1]) <= 2 for xy in feature['ring'] for v in xy)
    assert report['kinds']['driveway']['simplified'] == 1 and report['kinds']['driveway']['vertex_cap'] == 0
    # the helper is the extraction path's rule: under the budget the same object comes back, over it simplify(2) is applied
    small = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    assert simplify_long_ring(small) is small
    big = Polygon([(x, (x % 2)*.1) for x in range(0, 160)] + [(160, 50), (0, 50)])
    assert len(simplify_long_ring(big).exterior.coords)-1 < 160 and len(big.exterior.coords)-1 == 162
    assert simplify_long_ring(big).equals_exact(big.simplify(2, preserve_topology=True), 0)


def test_n2_a_feature_still_over_the_cap_after_simplification_is_reported_not_dropped():
    """A merged ring whose simplified form keeps more than 200 vertices (real 2.5 px steps every 0.05 m) stays in the layer and
    is listed under report['vertex_cap'], mirroring the extraction path's drop condition without the drop."""
    def saw(x0, x1, y, n, amp):
        xs = np.linspace(x0, x1, n)
        return [(float(x), y + (amp if i % 2 else 0.)) for i, x in enumerate(xs)]
    a_ring = [(0, 0), (12, 0), *reversed(saw(0, 12, 3, 240, .3))]           # 0.3 m = 3 px teeth survive a 2 px simplification
    b_ring = [(12.4, 0), (24, 0), *reversed(saw(12.4, 24, 3, 240, .3))]
    a = obj('aaaa000000000001', 'road', a_ring, score=.5)
    b = obj('bbbb000000000002', 'road', b_ring, score=.5)
    out, report = run([a, b])
    feature, = features(out)
    frep, = report['features']
    assert frep['vertex_cap'] is True and frep['vertices'] == len(feature['ring']) > 200
    cap, = report['vertex_cap']
    assert cap['id'] == feature['id'] and cap['vertices'] == frep['vertices'] and cap['cap'] == 200
    assert report['kinds']['road']['vertex_cap'] == 1 and report['kinds']['road']['features_out'] == 1
    assert [o['status'] for o in out[:2]] == ['superseded', 'superseded']


# --- 2026-10-03 rings: the app's outline rules (src/lib/scene-types.ts polygonIssues) ported, the two lossless repairs -----------

APP_FIXTURE = ML/'tests/fixtures/linear-app-ring-rules-2026-10-03.json'


def square(x0=10., y0=10., size=20.):
    return [[x0, y0], [x0+size, y0], [x0+size, y0+size], [x0, y0+size]]


def consecutive_duplicates(ring):
    return [i for i in range(len(ring)) if np.hypot(*(np.subtract(ring[i], ring[(i+1) % len(ring)]))) < 1e-6]


def test_o_app_ring_issues_is_the_apps_polygon_issues_rule_for_rule():
    """Every branch of polygonIssues, in its words: the count rule (3..200 finite points), bounds, the 1 px2 area, ANY two vertices
    within 1e-6, a proper crossing, an endpoint on a non-adjacent segment (|turn| < 1e-8), the 32 / 2000 budget, the per-hole
    rules with the 'Gap n:' prefix, a hole outside, a hole crossing, a shared edge, overlapping holes, a contact cycle."""
    assert app_ring_issues(square(), [], 512, 512) == []
    assert app_ring_issues([[0, 0], [1, 0]], [], 512, 512) == [APP_ISSUE_POINTS]
    assert app_ring_issues([[0, 0], [1, 0], [1, float('nan')]], [], 512, 512) == [APP_ISSUE_POINTS]
    circle = [[256+100*np.cos(t), 256+100*np.sin(t)] for t in np.linspace(0, 2*np.pi, 201, endpoint=False)]
    assert app_ring_issues(circle, [], 512, 512) == [APP_ISSUE_POINTS]                     # 201 > 200
    assert app_ring_issues(circle, [], 512, 512, rules=APP_RING_RULES_UNCAPPED) == []     # the count rules relaxed, geometry judged
    assert app_ring_issues(square(500., 500.), [], 512, 512) == [APP_ISSUE_BOUNDS]
    assert app_ring_issues(square(500., 500.)) == []                                     # no image: the bounds rule alone is skipped
    assert app_ring_issues([[0, 0], [.5, 0], [.5, .5], [0, .5]], [], 512, 512) == [APP_ISSUE_AREA, APP_ISSUE_REMAINING_AREA]
    dup = square()
    dup.insert(2, list(dup[1]))
    assert app_ring_issues(dup, [], 512, 512) == [APP_ISSUE_OVERLAP]
    touch = [[0, 0], [20, 0], [20, 20], [10, 20], [10, 0], [0, 20]]                       # vertex 4 lies on the base edge
    assert app_ring_issues(touch, [], 512, 512) == [APP_ISSUE_CROSS]                      # an endpoint on a non-adjacent segment
    near = [[0, 0], [20, 0], [20, 20], [10, 20], [10, 0.0000005], [0, 20]]                # 5e-7 px above the base: turn 1e-5 > 1e-8
    assert app_ring_issues(near, [], 512, 512) == []
    assert app_ring_issues([[0, 0], [20, 20], [20, 0], [0, 30]], [], 512, 512) == [APP_ISSUE_CROSS]   # a bow-tie, a proper crossing
    assert app_ring_issues([[0, 0], [20, 20], [20, 0], [0, 20]], [], 512, 512) == [APP_ISSUE_AREA, APP_ISSUE_CROSS]   # symmetric: zero area too
    spike = [[0, 0], [20, 0], [20, 20], [10, 20], [5, 25], [8, 22], [0, 20]]              # [10,20] -> [5,25] -> [8,22]: a back-track
    assert app_ring_issues(spike, [], 512, 512) == [APP_ISSUE_CROSS]
    assert app_ring_issues(square(), [square(12., 12., 2.)]*33, 512, 512) == [APP_ISSUE_BUDGET]
    big = [[256+200*np.cos(t), 256+200*np.sin(t)] for t in np.linspace(0, 2*np.pi, 200, endpoint=False)]
    assert app_ring_issues(big, [[[200+30*np.cos(t), 200+30*np.sin(t)] for t in np.linspace(0, 2*np.pi, 200, endpoint=False)]]*10, 512, 512) == [APP_ISSUE_BUDGET]
    hole_dup = square(12., 12., 4.)
    hole_dup.insert(1, list(hole_dup[0]))
    assert app_ring_issues(square(), [hole_dup], 512, 512) == [f'Gap 1: {APP_ISSUE_OVERLAP}']
    assert app_ring_issues(square(), [square(12., 12., 4.), [[20, 20], [20.001, 20], [20.001, 20.001]]], 512, 512) == [f'Gap 2: {APP_ISSUE_AREA}']
    assert app_ring_issues(square(), [square(40., 40., 4.)], 512, 512) == [APP_ISSUE_GAP_OUTSIDE.format(n=1)]
    assert app_ring_issues(square(), [square(25., 12., 10.)], 512, 512) == [APP_ISSUE_GAP_OUTSIDE.format(n=1)]  # crosses the outline
    assert app_ring_issues(square(), [square(10., 12., 4.)], 512, 512) == [APP_ISSUE_GAP_OUTSIDE.format(n=1)]  # shares the left edge
    assert app_ring_issues(square(), [[[10, 12], [14, 15], [10, 18]]], 512, 512) == [APP_ISSUE_GAP_OUTSIDE.format(n=1)]  # shares part of the left edge
    assert app_ring_issues(square(), [[[10, 15], [14, 12], [14, 18]]], 512, 512) == []                       # one point contact: fine
    assert app_ring_issues(square(), [square(12., 12., 6.), square(14., 14., 6.)], 512, 512) == [APP_ISSUE_GAP_OVERLAP]
    assert app_ring_issues(square(), [square(12., 12., 6.), square(13., 13., 2.)], 512, 512) == [APP_ISSUE_GAP_OVERLAP]
    assert app_ring_issues(square(), [square(12., 12., 4.), square(16., 16., 4.)], 512, 512) == []          # two gaps touching at a corner
    # a cycle of contacts: the outline touched by two gaps that also touch each other closes off a region
    cycle = [[[10, 15], [14, 12], [14, 18]], [[14, 18], [18, 22], [10, 22]]]                # shell-gap1 (10,15), shell-gap2 (10,22), gap1-gap2 (14,18)
    assert app_ring_issues(square(), cycle, 512, 512) == [APP_ISSUE_GAP_DISCONNECT]
    assert app_ring_issues(square(0., 0., 1.5), [[[0.1, 0.1], [1.4, 0.1], [1.4, 1.4], [0.1, 1.4]]], 512, 512) == [APP_ISSUE_REMAINING_AREA]


def test_o2_finalise_app_ring_drops_a_consecutive_duplicate_and_a_back_track_spike_losslessly():
    """The two repairs: a duplicate after rounding (the three 'Outline points overlap' windows), the first / last pair included,
    and a zero-width back-track whose tip is collinear with its neighbours (the two 'Outline edges cross' windows). Area and the
    point set outside the dropped vertices are untouched; the repaired outline passes the app's rules; a clean ring is returned as it came."""
    clean = square()
    assert finalise_app_ring(clean, [], 512, 512) == (clean, [], [], None)
    dup = [[10., 10.], [30., 10.], [30., 10.], [30., 30.], [10., 30.], [10., 10.]]
    assert app_ring_issues(dup, [], 512, 512) == [APP_ISSUE_OVERLAP]
    ring, holes, issues, repairs = finalise_app_ring(dup, [], 512, 512)
    assert (ring, holes, issues) == (clean, [], []) and repairs == {'dropped': 2, 'ring': {'duplicate': 2, 'collinear': 0, 'vertices_before': 6, 'vertices_after': 4}, 'holes': []}
    spike = [[10, 10], [30, 10], [30, 30], [20, 30], [15, 35], [18, 32], [10, 30]]
    assert app_ring_issues(spike, [], 512, 512) == [APP_ISSUE_CROSS]
    ring, holes, issues, repairs = finalise_app_ring(spike, [], 512, 512)
    assert issues == [] and ring == [[10, 10], [30, 10], [30, 30], [20, 30], [18, 32], [10, 30]]   # the tip gone, the base vertex kept
    assert Polygon(ring).area == Polygon(spike).buffer(0).area == 410. and repairs['ring'] == {'duplicate': 0, 'collinear': 1, 'vertices_before': 7, 'vertices_after': 6}
    straight = [[10, 10], [20, 10], [30, 10], [30, 30], [10, 30]]                        # a straight-through point: dropped too, lossless
    ring, _, issues, repairs = finalise_app_ring(straight, [], 512, 512)
    assert ring == clean and issues == [] and repairs['ring'] == {'duplicate': 0, 'collinear': 1, 'vertices_before': 5, 'vertices_after': 4}
    # the same per hole, reported with the hole's index
    hole = [[12., 12.], [12., 12.], [16., 12.], [16., 16.], [12., 16.]]
    ring, holes, issues, repairs = finalise_app_ring(clean, [square(20., 20., 4.), hole], 512, 512)
    assert ring == clean and holes == [square(20., 20., 4.), square(12., 12., 4.)] and issues == []
    assert repairs == {'dropped': 1, 'ring': {'duplicate': 0, 'collinear': 0, 'vertices_before': 4, 'vertices_after': 4},
                       'holes': [{'index': 1, 'duplicate': 1, 'collinear': 0, 'vertices_before': 5, 'vertices_after': 4}]}


def test_o3_an_outline_the_repairs_cannot_mend_keeps_its_app_issue():
    """Only the two lossless repairs run: a proper crossing, a self-touch (two non-consecutive equal vertices) and a hole outside
    the outline are reported as the app would, never 'fixed'."""
    assert finalise_app_ring([[0, 0], [20, 20], [20, 0], [0, 30]], [], 512, 512)[2] == [APP_ISSUE_CROSS]
    touch = [[0, 0], [20, 0], [20, 20], [10, 10], [0, 20], [-10, 35], [-10, -5], [10, 10]]      # (10,10) twice, no collinear triple
    ring, _, issues, repairs = finalise_app_ring(touch, [], 512, 512)
    assert issues == [APP_ISSUE_BOUNDS, APP_ISSUE_OVERLAP] and repairs is None and ring == touch
    assert finalise_app_ring(square(), [square(40., 40., 4.)], 512, 512)[2] == [APP_ISSUE_GAP_OUTSIDE.format(n=1)]
    # a hole left under three points by the repairs (zero area: win-c2a076d238992634's intermediate footpath pair carried [A, B, A])
    # is an app issue by default, so the pair is refused and never handed to shapely; drop_collapsed_holes removes it and says so
    degenerate = [[12., 12.], [14., 15.], [12., 12.]]
    ring, holes, issues, repairs = finalise_app_ring(square(), [degenerate], 512, 512)
    assert ring == square() and holes == [[[12., 12.], [14., 15.]]] and issues == [f'Gap 1: {APP_ISSUE_POINTS}']
    assert repairs == {'dropped': 1, 'ring': {'duplicate': 0, 'collinear': 0, 'vertices_before': 4, 'vertices_after': 4},
                       'holes': [{'index': 0, 'duplicate': 1, 'collinear': 0, 'vertices_before': 3, 'vertices_after': 2}]}
    ring, holes, issues, repairs = finalise_app_ring(square(), [degenerate], 512, 512, drop_collapsed_holes=True)
    assert (ring, holes, issues) == (square(), [], []) and repairs['dropped'] == 2
    assert repairs['holes_dropped'] == [{'index': 0, 'vertices_before': 3, 'points_after_repair': 2}]
    assert LINEAR_REFUSALS == ('no_bridge', 'not_connected', 'bridge_too_large', 'union_ratio', 'touches_blocker', 'touches_decided',
                               'invalid_after_rounding', 'app_ring_rules', 'result_floor')


def test_o4_the_merge_repairs_a_duplicate_after_rounding_and_emits_an_app_clean_ring():
    """A natural case of the three 'Outline points overlap' windows: a 5 m square and its neighbour 0.4 m right, shifted up by
    0.125 m, so the fill's fillet meets the neighbour within 0.005 px of a corner and two vertices round onto one point (GEOS
    accepts a repeated point, the app does not). The feature is emitted with the repaired ring, the repair is reported, the
    area is the square + fill + square, and the ring passes app_ring_issues with the image size."""
    a = obj('a000000000000001', 'driveway', rect(0, 0, 5, 5))
    b = obj('b000000000000002', 'driveway', rect(5.4, .125, 10.4, 5.125))
    out, report = merge_linear_fragments([a, b], AFFINE, image_size=(512, 512))
    feature, = features(out)
    row, = report['features']
    assert report['refusals'] == [] and row['ring_repairs']['ring']['duplicate'] >= 1 and row['ring_repairs']['dropped'] == row['ring_repairs']['ring']['vertices_before']-len(feature['ring'])
    assert report['kinds']['driveway']['ring_repaired'] == 1 and report['kinds']['driveway']['merges'] == {'step1': 1, 'step2': 0}
    assert consecutive_duplicates(feature['ring']) == [] and app_ring_issues(feature['ring'], feature.get('holes') or [], 512, 512) == []
    assert Polygon(feature['ring']).is_valid and abs(metric(feature).area-(50.+row['bridge_m2'])) < .02
    assert [o['status'] for o in out[:2]] == ['superseded', 'superseded'] and feature['replaces'] == [a['id'], b['id']]
    # the same input through the unrepaired path would have reached the app with a duplicate: the report says what was dropped
    assert row['ring_repairs']['ring']['vertices_before'] > len(feature['ring'])


def test_o5_a_merged_ring_the_app_would_refuse_is_refused_app_ring_rules_and_the_pieces_stay():
    """The refusal path: the join of test_a judged with an image the merged ring does not fit (the bounds rule, the one rule the
    repairs cannot mend here) is refused 'app_ring_rules' with the app's message; both pieces come back exactly as they were,
    no feature is emitted, and the kind's report counts the refusal under the new reason."""
    a = obj('a000000000000001', 'driveway', rect(0, 0, 5, 5))
    b = obj('b000000000000002', 'driveway', rect(5.4, 0, 10.4, 5))
    out, report = merge_linear_fragments([a, b], AFFINE, image_size=(100, 512))      # the join reaches x = 104 px
    assert out == [a, b] and features(out) == []
    refusal, = report['refusals']
    assert refusal['reason'] == 'app_ring_rules' and refusal['app_issues'] == [APP_ISSUE_BOUNDS] and refusal['step'] == 1
    assert report['kinds']['driveway']['refusals'] == {'app_ring_rules': 1} and report['kinds']['driveway']['features_out'] == 2
    accepted, _ = merge_linear_fragments([a, b], AFFINE, image_size=(512, 512))         # the same join inside the image is accepted
    assert len(features(accepted)) == 1


def test_o6_app_ring_issues_agrees_with_the_app_on_the_five_refused_and_the_eighteen_promoted_windows():
    """The fixture holds the merged features exactly as planned on 2026-10-03: for each of the five windows the app refused at the
    module's own qa_ scratch POST, app_ring_issues reports the app's message class on a feature of the labelled kind and nothing on
    the other features; after finalise_app_ring every ring is clean, shapely-valid and of the same area (the repairs are lossless).
    For the 32 features of the 18 windows the app accepted and that were promoted, app_ring_issues reports nothing."""
    fixture = json.loads(APP_FIXTURE.read_text())
    assert len(fixture['refused']) == 5 and len(fixture['accepted']) == 18
    for window in fixture['refused']:
        label, message = window['app_message'].split(': ', 1)
        size = (window['image_width'], window['image_height'])
        failing = [f for f in window['features'] if app_ring_issues(f['ring'], f['holes'], *size)]
        assert [f['label'] for f in failing] == [label], window['window']
        assert app_ring_issues(failing[0]['ring'], failing[0]['holes'], *size) == [message], window['window']
        for f in window['features']:
            ring, holes, issues, repairs = finalise_app_ring(f['ring'], f['holes'], *size)
            assert issues == [] and Polygon(ring, holes).is_valid, (window['window'], f['id'])
            assert abs(Polygon(ring, holes).area-Polygon(f['ring'], f['holes']).area) < 1e-6, (window['window'], f['id'])
            assert (repairs is not None) == (f in failing), (window['window'], f['id'])
            if f in failing:
                kind = 'duplicate' if message == APP_ISSUE_OVERLAP else 'collinear'
                assert repairs['ring'][kind] >= 1 and repairs['holes'] == []
    accepted = [(window, f) for window in fixture['accepted'] for f in window['features']]
    assert len(accepted) == 32
    for window, f in accepted:
        assert app_ring_issues(f['ring'], f['holes'], window['image_width'], window['image_height']) == [], (window['window'], f['id'])
