"""teacher-checks-v2: normalisation of the three feedback schemas, scoring with injected fired tokens, object_missing, per-catch
specificity, report paths, older object-hints rows read as stale and dropped by --write-index, and the solar_fixture_like population rate
with its Wilson interval. Every share is agreement with the reviewer's theme or a population flag rate, never accuracy."""
import json
from pathlib import Path

from scripts.ml import teacher_checks as tc

REPO = Path(__file__).resolve().parents[3]
SETS = {'cp20': REPO/'data/references/eyes-checkpoints/CP20/user-feedback-2026-09-30.json',
        'cp3': REPO/'data/references/eyes-checkpoints/CP3/user-feedback-2026-09-26.json',
        'sep28': REPO/'data/references/eyes-checkpoints/REVIEW-FEEDBACK-2026-09-28.json'}


def cases_of(name):
    return tc.normalise(json.loads(SETS[name].read_text()), source=SETS[name].name)


def test_normalise_counts_and_themes_across_the_three_schemas():
    cp20, cp3, sep28 = cases_of('cp20'), cases_of('cp3'), cases_of('sep28')
    assert (len(cp20), len(cp3), len(sep28)) == (16, 13, 11)
    themes = {c['object_id']: c['themes'] for c in cp20}
    assert themes['d7f7ceb84a6d0db4'] == ['frame-cut/centring/lot']
    assert themes['5d1c61a0f7ef49d9'] == ['sub-division block']
    assert themes['35f242ae81062884'] == ['tree over-split']
    assert themes['924c83ad298eef05'] == ['class error']
    assert themes['689ea9979a45b7e3'] == ['primary vs secondary']
    assert themes['587e522a2986ccea'] == ['noisy roof outline']
    assert themes['816ff673852a41c9'] == ['reflect']
    assert themes['55a5f9963565960c'] == ['frame-cut/centring/lot']
    assert all(not c['negative'] for c in cp20) and all(not c['negative'] for c in cp3)
    informational = [c['object_id'] for c in cp3 if c['informational']]
    assert informational == ['19e7744ed31b7627'], 'user-edited/large-difference is informational, not scored'
    assert next(c for c in cp3 if c['object_id'] == 'f5c74b7e8b6230df')['themes'] == ['tree as building']
    assert next(c for c in cp3 if c['object_id'] == '324d3af96371f418')['themes'] == ['units should split']
    negatives = [c['object_id'] for c in sep28 if c['negative']]
    assert negatives == ['887c2d078f693396', '6e0372f51e315335']
    shed = next(c for c in sep28 if c['object_id'] == '309ebd49a4abe960')
    assert shed['themes'] == ['structure sub-kind'] and 'structure:shed' in tc.expected_tokens(shed)
    rejected = next(c for c in sep28 if c['object_id'] == 'bf328ab9a854953c')
    assert 'rejected outline' in rejected['themes'] and 'degenerate_outline' in tc.expected_tokens(rejected)


def test_expected_tokens_follow_the_versioned_table():
    case = tc._case('s', 'o', ['frame-cut/centring/lot', 'reflect'])
    assert tc.expected_tokens(case) == {'frame_cut', 'lot_incomplete_in_frame', 'lot_unassociated', tc.ANY_REVIEW}
    negative = tc._case('s', 'o', ['no secondary', 'no review hint'], negative=True)
    assert 'secondary_by_lot' in tc.expected_tokens(negative) and tc.ANY_REVIEW in tc.expected_tokens(negative)
    assert len(tc.expectations_sha256()) == 64


def fake_scene(*object_ids, replaces=None):
    objects = [{'id': oid, 'kind': 'building', 'status': 'candidate', 'ring': [[0, 0], [1, 0], [1, 1]]} for oid in object_ids]
    if replaces:
        objects[0]['replaces'] = list(replaces)
    return {'id': 's', 'model_signature': 'm'*64, 'objects': objects}


def test_scoring_with_injected_tokens_and_wilson_bounds():
    cases = [tc._case('s', 'a', ['frame-cut/centring/lot'], source='CP20.json'),
             tc._case('s', 'b', ['noisy roof outline'], source='CP20.json'),
             tc._case('s', 'gone', ['reflect'], source='CP20.json'),
             tc._case('s', 'n', ['no secondary', 'no review hint'], negative=True, source='sep28.json'),
             tc._case('s', 'i', [], informational=True, source='CP3.json')]
    fresh = {'s': {'objects': {'a': {'tokens': ['frame_cut', 'lot_unassociated', tc.ANY_REVIEW], 'metrics': {'area_m2': 8.4}, 'sentences': ['x']},
                               'b': {'tokens': ['kind:building'], 'metrics': {'area_m2': 87}, 'sentences': []},
                               'n': {'tokens': ['kind:building'], 'metrics': {}, 'sentences': []},
                               'i': {'tokens': [], 'metrics': {}, 'sentences': []},
                               'succ': {'tokens': ['frame_cut', tc.ANY_REVIEW], 'metrics': {}, 'sentences': []}}}}
    served = {('s', 'a'): {'state': {'object_hints': 'stale'}, 'tokens': ['frame_cut']}}
    scored = tc.score_cases(cases, fresh, served, {'s': fake_scene('succ', 'a', 'b', 'n', 'i', replaces=['gone'])})
    by = {e['object_id']: e for e in scored}
    assert by['a']['status_check'] == 'scored' and by['a']['caught_fresh'] and by['a']['caught_served']
    assert by['b']['caught_fresh'] is False and by['b']['caught_served'] is False
    assert by['gone']['status_check'] == 'object_missing' and by['gone']['successor'] == 'succ' and 'frame_cut' in by['gone']['successor_fresh']
    assert by['n']['status_check'] == 'negative' and by['n']['over_flag_fresh'] is False
    assert by['i']['status_check'] == 'informational'
    summary = tc.summarise(scored)
    assert summary['overall']['fresh'] == {'count': 1, 'denominator': 2, 'share': .5, 'wilson_95': [.0945, .9055]}
    assert summary['overall']['served']['count'] == 1
    assert summary['per_theme']['frame-cut/centring/lot']['fresh']['count'] == 1
    assert summary['negatives']['over_flag']['fresh'] == {'count': 0, 'denominator': 1, 'share': 0., 'wilson_95': [0., .7935]}
    assert [m['object_id'] for m in summary['misses']] == ['b'] and summary['misses'][0]['metrics'] == {'area_m2': 87}
    assert summary['per_source']['CP20.json']['object_missing'] == 1 and summary['per_source']['CP3.json']['informational'] == 1
    assert tc._interval(16, 16)['wilson_95'] == [.8064, 1.]


def test_report_lands_under_teacher_checks_and_checkpoint_copy_is_bounded(tmp_path):
    scored = tc.score_cases([tc._case('s', 'a', ['reflect'], source='CP20.json')],
                            {'s': {'objects': {'a': {'tokens': [tc.ANY_REVIEW], 'metrics': {}, 'sentences': ['s']}}}}, {}, {'s': fake_scene('a')})
    summary = tc.summarise(scored)
    report = {'version': tc.VERSION, 'date': '2026-09-30', 'summary': summary, 'per_case': scored, 'code_sha256': 'c'*64, 'expectations_sha256': 'e'*64,
              'population_rates': {'frame_cut': {'flagged': 1, 'denominator': 4, 'share': .25}, 'noisy_outline_by_pixel_class': {}, 'squared_candidate_by_pixel_class': {},
                                   'tiny_area_for_kind_by_kind': {}, 'secondary_by_lot_members': {}, 'lot_incomplete_in_frame_buildings_with_lot': {}},
              'served_state_counts': {'object_hints:missing': 1}, 'written': {}, 'reflections': [], 'honesty': tc.HONESTY}
    folder = tc.write_report(report, tmp_path)
    assert folder == tmp_path/'data/reviews/eyes/teacher-checks/2026-09-30'
    assert {p.name for p in folder.iterdir()} == {'report.json', 'report.md', 'cases.jsonl', 'rows.jsonl', 'reflections.json', 'summary.json'}
    text = (folder/'report.md').read_text()
    assert 'never accuracy' in text and 'Overall fresh: 1/1' in text and 'accuracy of' not in text
    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*') if p.is_file())
    assert all(p.startswith('data/reviews/eyes/teacher-checks/') for p in written), written
    target = tc.checkpoint_copy(report, 'CP21', tmp_path)
    assert target == tmp_path/'data/references/eyes-checkpoints/CP21/teacher-checks.json'
    try:
        tc.checkpoint_copy(report, '../evil', tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError('checkpoint ids are bounded')


def test_docstring_disambiguates_teacher():
    assert 'hi-res SAM3 pseudo-label source' in tc.__doc__ and 'scores the hint layer' in tc.__doc__


EVENING = REPO/'data/references/eyes-checkpoints/CP22/user-feedback-2026-09-30-evening.json'


def test_class_error_theme_expects_the_solar_fixture_hint():
    assert 'solar_fixture_like' in tc.THEME_EXPECTATIONS['class error']
    evening = tc.normalise(json.loads(EVENING.read_text()), source=EVENING.name)
    assert [c['themes'] for c in evening] == [['frame-cut/centring/lot']]*2 + [['class error']]*2
    assert all('solar_fixture_like' in tc.expected_tokens(c) for c in evening if c['themes'] == ['class error'])


def test_served_state_reports_older_object_hint_rows_as_stale(tmp_path):
    from contextlib import closing

    from scripts.ml import eyes_index
    from scripts.ml import scene_object_hints as oh
    old = {'codes': ['frame_cut'], 'hints': [{'code': 'frame_cut', 'severity': 'review'}], 'review_count': 1}
    new = {'codes': ['solar_fixture_like'], 'hints': [{'code': 'solar_fixture_like', 'severity': 'review'}], 'review_count': 1}
    rows = [('old', 'sig', 'object-hints-v1', old), ('new', 'sig', oh.VERSION, new), ('both', 'sig', 'object-hints-v1', old),
            ('both', 'sig', oh.VERSION, new), ('moved', 'earlier-sig', oh.VERSION, new)]
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'object_quality', [{'scene_id': 's', 'object_id': oid, 'model_signature': sig, 'version': version, 'payload': payload}
                                                       for oid, sig, version, payload in rows])
    served = tc.served_state(tmp_path, {'s': {'old', 'new', 'both', 'moved', 'none'}}, {'s': 'sig'})
    assert served[('s', 'old')]['state']['object_hints'] == 'stale', 'a v1 row predates the current rules, whatever its signature'
    assert served[('s', 'old')]['versions']['object_hints'] == 'object-hints-v1' and 'frame_cut' in served[('s', 'old')]['tokens']
    assert served[('s', 'new')]['state']['object_hints'] == 'current' and served[('s', 'new')]['versions']['object_hints'] == oh.VERSION
    assert served[('s', 'both')]['state']['object_hints'] == 'current' and 'frame_cut' not in served[('s', 'both')]['tokens'], 'the current version wins'
    assert served[('s', 'moved')]['state']['object_hints'] == 'stale', 'a current-version row under another signature is stale as before'
    assert served[('s', 'none')]['state']['object_hints'] == 'missing' and 'object_hints' not in served[('s', 'none')]['versions']


def test_solar_rates_are_flagged_over_solar_panels_with_wilson_bounds():
    def row(oid, kind, codes, reasons=(), area=5., median=100.):
        return {'object_id': oid, 'payload': {'kind': kind, 'area_m2': area, 'codes': list(codes),
                                              'solar': {'reasons': list(reasons), 'median_luminance': median} if kind == 'solar_panel' else None}}
    rows = {'a': [row('1', 'solar_panel', ['solar_fixture_like'], ['bright']), row('2', 'solar_panel', []), row('3', 'building', ['frame_cut']),
                  row('bad', 'solar_panel', [], area=None)],
            'b': [row('4', 'solar_panel', ['solar_fixture_like'], ['non_rectangular', 'small'], median=None)], 'c': [row('5', 'building', [])]}
    rate = tc.solar_rates(rows)
    assert (rate['count'], rate['denominator'], rate['share']) == (2, 3, .6667), 'invalid outlines are not in the denominator'
    assert rate['wilson_95'] == [round(b, 4) for b in tc.wilson(2, 3)]
    assert rate['by_reason'] == {'bright': 1, 'non_rectangular': 1, 'small': 1}
    assert rate['brightness_not_judged'] == 1 and rate['scenes'] == 2 and rate['invalid_outline'] == 1
    assert 'by_pixel_class' not in rate, 'no scenes, no pixel classes'
    frame = lambda px: {'pixel_to_meters': [[px, 0], [0, -px], [0, 0]]}  # noqa: E731
    classes = tc.solar_rates(rows, {'a': frame(.07), 'b': frame(.236), 'c': frame(.1)})['by_pixel_class']
    assert classes == {'<0.08': {'flagged': 1, 'solar_panels': 2, 'share': .5}, '>=0.2': {'flagged': 1, 'solar_panels': 1, 'share': 1.}}


def test_report_carries_the_solar_population_rates_with_wilson_intervals(tmp_path):
    scored = tc.score_cases([tc._case('s', 'a', ['class error'], source='CP22.json')],
                            {'s': {'objects': {'a': {'tokens': ['solar_fixture_like', tc.ANY_REVIEW], 'metrics': {}, 'sentences': ['s']}}}}, {}, {'s': fake_scene('a')})
    summary = tc.summarise(scored)
    assert summary['overall']['fresh']['count'] == 1, 'the solar hint catches a class-error case'
    solar = {'harness': {**tc._interval(5, 5), 'scenes': 1, 'by_reason': {'bright': 5}, 'brightness_not_judged': 0, 'invalid_outline': 0,
                         'by_pixel_class': {'<0.08': {'flagged': 5, 'solar_panels': 5, 'share': 1.}}},
             'sample': {**tc._interval(3, 40), 'scenes': 12, 'by_reason': {'small': 2, 'bright': 1}, 'brightness_not_judged': 0, 'invalid_outline': 0}}
    report = {'version': tc.VERSION, 'date': '2026-09-30-x', 'summary': summary, 'per_case': scored, 'code_sha256': 'c'*64, 'expectations_sha256': 'e'*64,
              'population_rates': {'noisy_outline_by_pixel_class': {}, 'squared_candidate_by_pixel_class': {}, 'tiny_area_for_kind_by_kind': {},
                                   'secondary_by_lot_members': {}, 'lot_incomplete_in_frame_buildings_with_lot': {}, 'solar_fixture_like': solar},
              'served_state_counts': {}, 'written': {}, 'reflections': [], 'honesty': tc.HONESTY}
    text = (tc.write_report(report, tmp_path)/'report.md').read_text()
    assert '## solar_fixture_like over solar_panel objects' in text
    assert '- harness scenes: 5/5 flagged (share 1.0, Wilson 95% [0.5655, 1.0])' in text
    assert '- seeded sample: 3/40 flagged (share 0.075, Wilson 95% [' in text and 'never accuracy' in text
    assert '- harness scenes by pixel class (m/px; reported per class, not compared across): {"<0.08": {"flagged": 5, "solar_panels": 5, "share": 1.0}}' in text
    assert 'stated before any fit' not in text


def test_write_index_drops_the_harness_scenes_earlier_object_hint_rows(tmp_path):
    from contextlib import closing

    from scripts.ml import eyes_index
    from scripts.ml import scene_object_hints as oh
    px = .1
    scene = {'id': 'tc-write', 'schema_version': 1, 'address_id': 4242, 'title': '1 TEST STREET', 'centroid': {'lon': 145.3155, 'lat': -37.7915},
             'image_width': 512, 'image_height': 512, 'image_sha256': 'a'*64, 'model_signature': 's'*64,
             'pixel_to_meters': [[px, 0], [0, -px], [0, 51.2]], 'meters_to_pixel': [[1/px, 0], [0, -1/px], [0, 512]],
             'objects': [{'id': oid, 'kind': kind, 'label': kind, 'status': 'candidate', 'score': .8, 'ring': ring, 'source': 'sam3-text-onnx', 'model': {}}
                         for oid, kind, ring in (('b', 'building', [[100, 100], [200, 100], [200, 190], [100, 190]]),
                                                 ('p', 'solar_panel', [[300, 300], [330, 300], [330, 320], [300, 320]]))]}
    (tmp_path/'data/scenes/tc-write').mkdir(parents=True)
    (tmp_path/'data/scenes/tc-write/latest.json').write_text(json.dumps(scene))
    old = {'codes': ['frame_cut'], 'hints': [{'code': 'frame_cut', 'severity': 'review'}], 'review_count': 1}
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'object_quality', [{'scene_id': s, 'object_id': o, 'model_signature': 's'*64, 'version': 'object-hints-v1', 'payload': old}
                                                       for s, o in (('tc-write', 'p'), ('elsewhere', 'x'))])
    feedback = tmp_path/'CP99-feedback.json'
    feedback.write_text(json.dumps({'source': 'human:chat', 'decisions': [{'scene': 'tc-write', 'object': 'p', 'status': 'rejected',
                                                                            'notes': '[reviewer 2026-09-30 evening, class error] a test fixture'}]}))
    report = tc.run([feedback], tmp_path, offline=True, write_index=True, sample_size=0, self_review=False)
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        left = sorted((r['scene_id'], r['object_id'], r['version']) for r in eyes_index.rows(db, 'object_quality'))
    assert left == [('elsewhere', 'x', 'object-hints-v1'), ('tc-write', 'b', oh.VERSION), ('tc-write', 'p', oh.VERSION)], left
    assert report['written']['object_hints'] == 2 and report['written']['object_hints_previous_removed'] == 1
    entry = report['per_case'][0]
    assert entry['served_state']['object_hints'] == 'stale' and entry['served_state_after_write']['object_hints'] == 'current'


def test_a_catch_records_how_specific_its_hints_are_in_the_scene(tmp_path):
    cases = [tc._case('s', 'p1', ['class error'], source='CP22.json'), tc._case('s', 'b1', ['frame-cut/centring/lot'], source='CP22.json'),
             tc._case('s', 'pool', ['class error'], source='CP22.json'), tc._case('s', 'b3', ['noisy roof outline'], source='CP22.json')]
    kinds = {'p1': 'solar_panel', 'p2': 'solar_panel', 'b1': 'building', 'b2': 'building', 'b3': 'building', 'pool': 'pool'}
    tokens = {'p1': ['solar_fixture_like', tc.ANY_REVIEW], 'p2': ['solar_fixture_like'], 'b1': ['frame_cut', 'lot_incomplete_in_frame'],
              'b2': ['lot_incomplete_in_frame'], 'b3': [], 'pool': ['pool_not_blue']}
    fresh = {'s': {'objects': {oid: {'tokens': tokens[oid] + [f'kind:{kind}'], 'metrics': {'kind': kind}, 'sentences': []} for oid, kind in kinds.items()}}}
    scene = {'id': 's', 'objects': [{'id': oid, 'kind': kind, 'status': 'candidate', 'ring': [[0, 0], [1, 0], [1, 1]]} for oid, kind in kinds.items()]}
    by = {e['object_id']: e for e in tc.score_cases(cases, fresh, {}, {'s': scene})}
    assert by['p1']['catch_specificity'] == {'solar_fixture_like': {'kind': 'solar_panel', 'fired': 2, 'of': 2, 'share': 1.}}
    assert by['p1']['non_specific_catch'] is True, 'the catching hint fires on every solar panel in the scene'
    assert by['b1']['catch_specificity'] == {'frame_cut': {'kind': 'building', 'fired': 1, 'of': 3, 'share': .3333},
                                             'lot_incomplete_in_frame': {'kind': 'building', 'fired': 2, 'of': 3, 'share': .6667}}
    assert by['b1']['non_specific_catch'] is False
    assert by['pool']['non_specific_catch'] is False, 'the only pool in the scene: nothing to be specific against'
    assert by['b3']['caught_fresh'] is False and by['b3']['catch_specificity'] == {} and by['b3']['non_specific_catch'] is None
    summary = tc.summarise(list(by.values()))
    assert summary['non_specific_catches'] == {'fresh': 1, 'caught_fresh': 3, 'cases': [['s', 'p1']]}
    report = {'version': tc.VERSION, 'date': '2026-09-30-spec', 'summary': summary, 'per_case': list(by.values()), 'code_sha256': 'c'*64,
              'expectations_sha256': 'e'*64, 'population_rates': {'noisy_outline_by_pixel_class': {}, 'squared_candidate_by_pixel_class': {},
                                                                   'tiny_area_for_kind_by_kind': {}, 'secondary_by_lot_members': {},
                                                                   'lot_incomplete_in_frame_buildings_with_lot': {}},
              'served_state_counts': {}, 'written': {}, 'reflections': [], 'honesty': tc.HONESTY}
    text = (tc.write_report(report, tmp_path)/'report.md').read_text()
    assert '- Non-specific fresh catches: 1 of 3 caught fresh' in text
    assert '- s p1 [class error]: solar_fixture_like fires on 2/2 solar_panel in the scene; non-specific catch' in text
    assert '- s b1 [frame-cut/centring/lot]: frame_cut fires on 1/3 building in the scene, lot_incomplete_in_frame on 2/3' in text


def test_image_codes_include_the_solar_hint_and_the_note_names_the_sample_pass():
    assert 'solar_fixture_like' in tc.IMAGE_CODES
    assert set(tc.IMAGE_CODES) == set(tc.IMAGE_CODES_HARNESS_ONLY) | set(tc.IMAGE_CODES_WITH_SAMPLE_PASS)
    note = tc.image_codes_note(solar_scenes=12, without_image=1)
    assert "'pool_not_blue', 'building_blends_into_surroundings'" in note and 'harness scenes only' in note
    assert 'solar_fixture_like' in note and 'loaded each sample scene image' in note and '12 sample scenes' in note and '1 without' in note


def test_version_two_and_the_expectations_hash_tracks_the_theme_table():
    assert tc.VERSION == 'teacher-checks-v2'
    assert 'expectations_sha256' in tc.__doc__ and 'THEME_EXPECTATIONS' in tc.__doc__


def test_honesty_records_the_visual_spot_check_of_bright_solar_fires():
    line = next(line for line in tc.HONESTY if 'spot check' in line)
    assert '>= 0.2 m/px' in line and 'real panel arrays' in line and 'object-hints-v3' in line
