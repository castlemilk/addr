"""Matching, summary, flag and reference-agreement helpers of the CLAHE detector experiment on synthetic polygons."""
import json
import sqlite3
from contextlib import closing

import numpy as np
import pytest
from scripts.ml import clahe_detector_experiment as exp
from scripts.ml.scene_confidence import geographic
from scripts.ml.scene_region import region_frame
from shapely.geometry import mapping


def square(x, y, size):
    return [[x, y], [x+size, y], [x+size, y+size], [x, y+size]]


def obj(oid, ring, kind='building', score=.7, raw=None):
    data = {'id': oid, 'kind': kind, 'status': 'candidate', 'score': score, 'ring': ring}
    if raw is not None:
        data['boundary_refinement'] = {'raw_ring': raw}
    return data


SCENE = {'pixel_to_meters': [[0.25, 0], [0, -0.25], [-144, 144]], 'owned_object_ids': ['a']}


def test_clahe_rgb_keeps_shape_dtype_and_changes_contrast():
    rng = np.random.default_rng(0)
    low = (rng.normal(120, 6, (96, 96, 3)).clip(0, 255)).astype(np.uint8)
    out = exp.clahe_rgb(low, tile_grid=(2, 2))
    assert out.shape == low.shape and out.dtype == np.uint8
    assert out.mean(axis=2).std() > low.mean(axis=2).std()
    with pytest.raises(ValueError):
        exp.clahe_rgb(low.astype(np.float32))


def test_match_objects_is_greedy_by_iou_and_per_kind():
    base = [obj('a', square(0, 0, 10)), obj('b', square(20, 0, 10)), obj('t', square(40, 40, 10), kind='tree'), obj('bad', [[0, 0], [1, 1]])]
    cand = [obj('a2', square(1, 0, 10)),             # IoU 9/11 = .82 with a
            obj('a3', square(4, 0, 10)),             # IoU 6/14 = .43 with a: below .5 -> only_candidate
            obj('b2', square(20, 2, 10)),            # IoU 8/12 = .67 with b
            obj('t2', square(40, 40, 10), kind='solar_panel')]  # same shape, other kind -> no match
    match = exp.match_objects(base, cand)
    pairs = {(b['id'], c['id']): v for b, c, v in match['matched']}
    assert set(pairs) == {('a', 'a2'), ('b', 'b2')}
    assert pairs[('a', 'a2')] == pytest.approx(9/11, abs=1e-3) and pairs[('b', 'b2')] == pytest.approx(8/12, abs=1e-3)
    assert [o['id'] for o in match['only_baseline']] == ['t']
    assert sorted(o['id'] for o in match['only_candidate']) == ['a3', 't2']
    assert match['skipped'] == [('baseline', 'bad')]


def test_match_objects_uses_each_object_once_preferring_the_higher_iou():
    base = [obj('a', square(0, 0, 10))]
    cand = [obj('near', square(1, 0, 10)), obj('exact', square(0, 0, 10))]
    match = exp.match_objects(base, cand)
    assert [(b['id'], c['id'], v) for b, c, v in match['matched']] == [('a', 'exact', 1.0)]
    assert [o['id'] for o in match['only_candidate']] == ['near']


def test_summarise_matches_reports_denominators_and_paired_deltas():
    base = [obj('a', square(0, 0, 10), score=.5, raw=square(0, 0, 10)), obj('b', square(20, 0, 10), score=.8), obj('lost', square(60, 60, 4), score=.55)]
    cand = [obj('a2', square(0, 0, 12), score=.6, raw=square(0, 0, 12)+[[0, 5]]), obj('b2', square(20, 0, 10), score=.7), obj('new', square(80, 80, 6), score=.51)]
    summary = exp.summarise_matches(exp.match_objects(base, cand), SCENE)
    b = summary['building']
    assert (b['baseline_total'], b['candidate_total'], b['matched'], b['only_baseline'], b['only_candidate']) == (3, 3, 2, 1, 1)
    assert b['matched_share_of_baseline'] == pytest.approx(2/3, abs=1e-3)
    assert b['iou']['median'] == pytest.approx((100/144+1)/2, abs=1e-3) and b['iou']['n_below_0_8'] == 1 and b['iou']['n_identical_ring'] == 1
    assert b['area_m2_delta']['n'] == 2 and b['area_m2_delta']['p90'] == pytest.approx(44*.0625*.9, abs=.05)  # linear interpolation between 0 and 2.75
    assert b['score_delta'] == {'median': 0.0, 'p10': pytest.approx(-.08, abs=1e-6), 'p90': pytest.approx(.08, abs=1e-6), 'n_up': 1, 'n_down': 1, 'n': 2}
    assert b['vertices']['raw_candidate_median'] == 5 and b['vertices']['raw_baseline_median'] == 4  # objects without a raw ring are ignored
    assert b['only_baseline_objects'][0]['object_id'] == 'lost' and b['only_baseline_objects'][0]['area_m2'] == 1.0
    assert b['only_candidate_objects'][0]['object_id'] == 'new' and b['only_candidate_objects'][0]['owned'] is False
    assert summary['tree'] == {**summary['tree'], 'baseline_total': 0, 'matched': 0, 'matched_share_of_baseline': None}


def test_focus_object_reports_lost_or_matched():
    base = [obj(exp.FOCUS_OBJECT, square(0, 0, 10))]
    assert exp.focus_object(exp.match_objects(base, []), SCENE)['outcome'] == 'only_baseline'
    matched = exp.focus_object(exp.match_objects(base, [obj('x', square(0, 0, 10))]), SCENE)
    assert matched['outcome'] == 'matched' and matched['iou'] == 1.0 and matched['ring_identical']
    assert exp.focus_object(exp.match_objects([], []), SCENE) is None


def test_building_flags_and_flag_summary_read_only(tmp_path):
    db = tmp_path/'eyes.sqlite'
    with closing(sqlite3.connect(db)) as con:
        con.execute('create table object_quality(scene_id, object_id, model_signature, version, payload)')
        rows = [('s', 'shed', 'm', 'building-flags-v2', json.dumps({'score': .5, 'area_m2': 40, 'class_hints': {'class_hint': 'outbuilding', 'flags': [{'flag': 'secondary-structure'}]}})),
                ('s', 'house', 'm', 'building-flags-v2', json.dumps({'class_hints': {'class_hint': 'house', 'flags': []}})),
                ('s', 'old', 'm', 'building-flags-v1', json.dumps({'class_hints': {'class_hint': 'outbuilding', 'flags': []}})),
                ('other', 'shed', 'm', 'building-flags-v2', json.dumps({'class_hints': {'class_hint': 'commercial', 'flags': []}}))]
        con.executemany('insert into object_quality values (?,?,?,?,?)', rows)
        con.commit()
    flags = exp.building_flags(db, 's', ['shed', 'house', 'old', 'missing'])
    assert set(flags) == {'shed', 'house'} and flags['shed']['flags'] == ['secondary-structure']
    summary = exp.flag_summary(flags, ['shed', 'house', 'missing'])
    assert summary == {'n': 3, 'with_flag_rows': 2, 'secondary_structure': 1, 'outbuilding_hint': 1, 'non_residential_hint': 0,
                       'class_hints': {'house': 1, 'outbuilding': 1}}
    assert exp.building_flags(tmp_path/'absent.sqlite', 's', ['shed']) == {}


def test_reference_agreement_round_trips_wgs84_geometry_through_the_scene_frame(tmp_path):
    frame = region_frame([145.31, -37.7905, 145.311, -37.7895])
    scene = {**frame, 'id': 'scene-a', 'region': {'bounds': [145.31, -37.7905, 145.311, -37.7895]}}
    roof = obj('roof', square(100, 100, 40))
    ref_geom = mapping(geographic(roof, scene))
    db = tmp_path/'eyes.sqlite'
    with closing(sqlite3.connect(db)) as con:
        con.execute('create table "references"(ref_id TEXT PRIMARY KEY, tier TEXT, source_kind TEXT, scene_id TEXT, object_id TEXT, crm_feature_id INTEGER, '
                    'address_id INTEGER, spatial_group TEXT, split TEXT, west REAL, south REAL, east REAL, north REAL, version TEXT, payload TEXT)')
        w, s, e, n = geographic(roof, scene).bounds
        con.execute('insert into "references" values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('r1', 'T1', 'confirmed', 'scene-a', 'roof', None, None, 'g', 'validation', w, s, e, n, 'v', json.dumps({'geometry_wgs84': ref_geom})))
        con.execute('insert into "references" values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('far', 'T1', 'confirmed', 'scene-z', 'x', None, None, 'g', 'validation', 150, -30, 150.001, -29.999, 'v', json.dumps({'geometry_wgs84': ref_geom})))
        con.execute('insert into "references" values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('t3', 'T3', 'agent', 'scene-a', 'roof', None, None, 'g', 'validation', w, s, e, n, 'v', json.dumps({'geometry_wgs84': ref_geom})))
        con.commit()
    refs = exp.references_in_tile(db, scene)
    assert [r['ref_id'] for r in refs] == ['r1'] and refs[0]['same_image'] is True
    rows = exp.reference_agreement(refs, [roof], [obj('shifted', square(110, 100, 40))])
    assert rows[0]['baseline']['iou'] == pytest.approx(1.0, abs=1e-3)
    assert rows[0]['candidate'] == {'iou': pytest.approx(30/50, abs=1e-3), 'object_id': 'shifted'}
    assert rows[0]['iou_delta'] == pytest.approx(-.4, abs=1e-3)
    assert exp.reference_agreement(refs, [], [])[0]['baseline'] == {'iou': 0., 'object_id': None}


def test_object_identity_counts_shared_ids_and_identical_rings():
    base = [obj('a', square(0, 0, 10)), obj('b', square(20, 0, 10))]
    cand = [obj('a', square(0, 0, 10)), obj('b', square(20, 1, 10), score=.1), obj('c', square(50, 50, 5))]
    assert exp.object_identity(base, cand) == {'baseline': 2, 'candidate': 3, 'shared_ids': 2, 'identical_rings': 1, 'identical_scores': 1}


def test_contact_sheet_draws_every_difference(tmp_path):
    from PIL import Image
    image = Image.new('RGB', (64, 64), (90, 90, 90))
    base = [obj('a', square(2, 2, 10)), obj('lost', square(30, 30, 8))]
    cand = [obj('a2', square(2, 2, 13)), obj('new', square(50, 5, 6), kind='tree')]
    match = exp.match_objects(base, cand)
    counts = exp.contact_sheet(SCENE, image, match, tmp_path/'sheet.png', scale=2, title='t')
    assert counts == {'only_baseline': 1, 'only_candidate': 1, 'low_iou': 1}
    sheet = Image.open(tmp_path/'sheet.png')
    assert sheet.size[0] == 128 and sheet.size[1] > 128


def test_roi_sheet_makes_one_tile_per_building_difference(tmp_path):
    from PIL import Image
    image = Image.new('RGB', (200, 200), (90, 90, 90))
    base = [obj('a', square(20, 20, 10)), obj('lost', square(100, 100, 8)), obj('tree', square(150, 150, 8), kind='tree')]
    cand = [obj('a2', square(20, 20, 13)), obj('new', square(60, 150, 6))]
    match = exp.match_objects(base, cand)
    assert exp.roi_sheet(image, base, cand, match, tmp_path/'crops.png', window=40, scale=2) == 3
    assert Image.open(tmp_path/'crops.png').size == (3*80+12, 80)
    assert exp.roi_sheet(image, base, base, exp.match_objects(base, base), tmp_path/'none.png') == 0


def test_classify_unmatched_separates_merges_from_absent_detections():
    base = [obj('u1', square(0, 0, 10)), obj('u2', square(10, 0, 10)), obj('gone', square(50, 50, 10)), obj('t', square(80, 80, 6), kind='tree')]
    cand = [obj('merged', square(0, 0, 20)), obj('fresh', square(70, 0, 8)), obj('t2', square(80, 82, 6), kind='tree')]
    match = exp.match_objects(base, cand)
    assert [b['id'] for b, _, _ in match['matched']] == ['t']
    reasons = exp.classify_unmatched(match)
    by_id = {r['object_id']: r for r in reasons['only_baseline'] + reasons['only_candidate']}
    assert by_id['u1']['reason'] == 'absorbed' and by_id['u1']['partners'][0] == {'object_id': 'merged', 'iou': .25, 'share': 1.0}
    assert by_id['u2']['reason'] == 'absorbed' and by_id['gone']['reason'] == 'absent' and by_id['fresh']['reason'] == 'absent'
    assert by_id['merged']['reason'] == 'partial' and by_id['merged']['covered_share'] == .5
    assert reasons['counts'] == {'only_baseline': {'building': {'absorbed': 2, 'absent': 1}, 'tree': {}, 'solar_panel': {}},
                                 'only_candidate': {'building': {'partial': 1, 'absent': 1}, 'tree': {}, 'solar_panel': {}}}
