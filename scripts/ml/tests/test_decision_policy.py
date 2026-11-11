"""decision-policy-v1: synthetic signal sets per tier, the auto gates, reflection arithmetic, dry-run hygiene, index replace."""
import json
from contextlib import closing
from pathlib import Path

import pytest
from scripts.ml import decision_policy as dp
from scripts.ml import eyes_index

SCENE = {'scene_id': 'mooroolbark-1-17-1-1', 'group': 'suburb', 'subgroup': 'suburb', 'model_signature': 'm', 'owned': True, 'title': 't'}


def signals(**over):
    base = {'scene': dict(SCENE), 'object_id': 'o1',
            'detector': {'band': 'high', 'score': .9, 'geometry_passed': True, 'clipped': False, 'stale': False, 'source': 'confidence.json'},
            'class_hints': {'class_hint': 'house', 'flags': [], 'version': dp.HINT_VERSION},
            'structure': {'sub_kind': 'not-secondary', 'confidence': 'weak', 'secondary_flag': False, 'version': dp.STRUCTURE_VERSION},
            'roof': {'roof_type': 'hipped', 'roof_form': 'pitched', 'form_uncertain': False, 'roof_fitted': True},
            'diagnostics': {'present': False}, 'allocation': {'status': 'matched', 'confidence': 'strong', 'version': dp.ALLOCATION_VERSION},
            'cross_capture': {'n_pairs': 0, 'band_disagreement': False}, 'review': {'present': False, 'status': None, 'live': True},
            'reference': None, 'recompute': None}
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def test_payload_has_exactly_the_contract_keys():
    payload = dp.decide(signals())
    assert set(payload) == {'version', 'note', 'tier', 'confidence', 'machine_label', 'reasons', 'signals', 'thresholds_version', 'computed_at', 'code_sha256',
                            'interval', 'score_interval'}
    assert set(payload['machine_label']) == {'kind', 'structure_sub_kind', 'class_hint', 'roof_form', 'roof_type'}
    assert payload['version'] == 'decision-policy-v2' and payload['note'] == dp.NOTE and 0 <= payload['confidence'] <= 1
    assert set(payload['interval']) == {'kind', 'stratum', 'confirmed', 'judged', 'lower', 'upper', 'note'}
    assert payload['interval']['judged'] == 0 and payload['interval']['lower'] is None and payload['interval']['upper'] is None
    assert payload['interval']['stratum'] == 'auto/primary-roof/house' and 'null when nobody has judged it yet' in payload['interval']['note']
    assert payload['score_interval']['lower'] <= payload['confidence'] <= payload['score_interval']['upper'] and 'model estimate' in payload['score_interval']['note']
    assert payload['machine_label'] == {'kind': 'building', 'structure_sub_kind': 'not-secondary', 'class_hint': 'house', 'roof_form': 'pitched', 'roof_type': 'hipped'}


def test_synthetic_signal_sets_land_in_each_tier():
    assert dp.decide(signals())['tier'] == 'auto'
    assist = dp.decide(signals(detector={'band': 'medium', 'score': .8}))
    assert assist['tier'] == 'assist' and any('band_high' in r for r in assist['reasons'])
    review = dp.decide(signals(detector={'band': 'low', 'score': .5, 'clipped': True}))
    assert review['tier'] == 'review' and review['confidence'] < dp.THRESHOLDS['assist_min']
    unassessed = dp.decide(signals(detector={'band': None, 'score': None, 'geometry_passed': None}))
    assert unassessed['tier'] == 'review'


@pytest.mark.parametrize('override, gate', [
    ({'detector': {'clipped': True}}, 'not_clipped'),
    ({'detector': {'geometry_passed': False}}, 'geometry_passed'),
    ({'detector': {'stale': True}}, 'confidence_current'),
    ({'class_hints': {'class_hint': 'non-residential'}}, 'class_hint_primary'),
    ({'class_hints': {'class_hint': 'tree-like'}}, 'not_tree_like'),
    ({'structure': {'sub_kind': 'garage', 'confidence': 'moderate'}}, 'no_conflicting_sub_kind'),
    ({'structure': {'sub_kind': 'fragment', 'confidence': 'moderate'}}, 'not_fragment'),
    ({'allocation': {'status': 'ambiguous', 'confidence': 'weak'}}, 'allocation_matched'),
    ({'allocation': {'status': 'matched', 'confidence': 'weak'}}, 'allocation_matched'),
])
def test_primary_auto_gates_block_auto(override, gate):
    payload = dp.decide(signals(**override))
    assert payload['tier'] != 'auto'
    assert payload['signals']['auto_gates']['path'] == 'primary-roof'
    assert payload['signals']['auto_gates']['gates'][gate] is False


def test_primary_auto_accepts_commercial_and_units_with_moderate_allocation():
    for hint in ('commercial', 'units'):
        payload = dp.decide(signals(class_hints={'class_hint': hint}, allocation={'status': 'matched', 'confidence': 'moderate'}))
        assert payload['tier'] == 'auto', payload['reasons']


def test_secondary_structure_path():
    shed = signals(detector={'band': 'low', 'score': .55, 'geometry_passed': True},
                   class_hints={'class_hint': 'outbuilding', 'flags': ['secondary-structure']},
                   structure={'sub_kind': 'shed', 'confidence': 'strong', 'secondary_flag': True},
                   allocation={'status': 'matched', 'confidence': 'moderate'})
    payload = dp.decide(shed)
    assert payload['signals']['auto_gates']['path'] == 'secondary-structure'
    assert payload['tier'] == 'auto', payload['reasons']
    assert dp.label_text(payload) == 'Shed'
    moderate = dp.decide(signals(**{**shed, 'structure': {**shed['structure'], 'confidence': 'moderate'}}))
    assert moderate['tier'] == 'assist'
    clipped = dp.decide(signals(**{**shed, 'detector': {**shed['detector'], 'clipped': True}}))
    assert clipped['tier'] != 'auto' and clipped['signals']['auto_gates']['gates']['not_clipped'] is False
    without_flag = dp.decide(signals(**{**shed, 'structure': {**shed['structure'], 'secondary_flag': False}}))
    assert without_flag['signals']['auto_gates']['path'] == 'primary-roof' and without_flag['tier'] != 'auto'


def test_human_decisions_never_enter_the_confidence():
    confirmed = dp.decide(signals(review={'present': True, 'status': 'confirmed', 'live': True}, reference={'tier': 'T1'}))
    rejected = dp.decide(signals(review={'present': True, 'status': 'rejected', 'live': False}))
    assert confirmed['confidence'] == rejected['confidence'] == dp.decide(signals())['confidence']


def test_recompute_signals_lower_borderline_roofs():
    plain = dp.decide(signals())
    flat = dp.decide(signals(recompute={'status': 'ok', 'roof_form': 'flat', 'roof_type': 'flat', 'flat_score': .7, 'topology_support': {'accepted': False}}))
    assert flat['confidence'] < plain['confidence'] and any('flat rule' in r for r in flat['reasons'])
    simplified = dp.decide(signals(recompute={'status': 'ok', 'roof_form': 'pitched', 'roof_type': 'hipped',
                                              'topology_support': {'accepted': True, 'accepted_by': 'optical'}}))
    assert simplified['confidence'] == round(plain['confidence'] + dp.THRESHOLDS['topology_simplified'], 4)
    assert any('accepted_by optical' in r for r in simplified['reasons'])
    assert dp.borderline(plain) is True  # auto candidates are always worth the recompute
    assert dp.borderline(dp.decide(signals(detector={'band': 'low', 'score': .3, 'clipped': True}))) is False


def _payload(scene_id, object_id, tier_signals, status=None, **review):
    sig = signals(**tier_signals)
    group, subgroup = dp.scene_group(scene_id)
    sig['scene'] = {**SCENE, 'scene_id': scene_id, 'group': group, 'subgroup': subgroup}
    sig['object_id'] = object_id
    sig['review'] = {'present': status is not None, 'status': status, 'live': status not in ('rejected', 'superseded'), **review}
    return dp.decide(sig)


def _human(scene_id, object_id, status, kind='building', **extra):
    return {'scene_id': scene_id, 'object_id': object_id, 'kind': kind, 'status': status, 'geometry_reviewed': status == 'confirmed',
            'family': None, 'roof_line_edits': False, 'roof_type': 'hipped', 'label': 'x', 'same_model_signature': True, 'provenance': None, **extra}


def test_reflection_arithmetic_and_wilson():
    auto = {}
    assist = {'detector': {'band': 'medium', 'score': .8}}
    payloads = [_payload('s', f'a{i}', auto) for i in range(4)] + [_payload('s', f'b{i}', assist) for i in range(4)]
    humans = [_human('s', 'a0', 'confirmed', family='gabled'), _human('s', 'a1', 'confirmed'), _human('s', 'a2', 'confirmed'), _human('s', 'a3', 'needs_adjustment'),
              _human('s', 'b0', 'confirmed'), _human('s', 'b1', 'rejected'), _human('s', 'b2', 'superseded'), _human('s', 'b3', 'candidate'),
              _human('s', 'c0', 'rejected', kind='solar_panel')]
    report = dp.reflect(payloads, humans)
    tiers = report['per_tier']
    assert tiers['auto']['judged'] == 4 and tiers['auto']['confirmed_share']['confirmed'] == 3
    assert tiers['auto']['confirmed_share']['share'] == .75
    assert tiers['auto']['confirmed_share']['wilson_95'] == pytest.approx([.3006, .9544], abs=1e-3)
    assert tiers['assist']['judged'] == 2 and tiers['assist']['confirmed_share']['share'] == .5 and tiers['assist']['superseded_excluded'] == 1
    assert tiers['review']['confirmed_share']['n'] == 0 and tiers['review']['confirmed_share']['share'] is None
    assert report['ordering']['auto_highest_confirmed_share'] is True
    assert report['human_inventory']['solar_panel'] == {'rejected': 1}
    assert report['decisions_without_machine_row'] == {'solar_panel:rejected': 1}
    populated = [b for b in report['calibration_bins'] if b['n']]
    assert sum(b['n'] for b in populated) == 6 and sum(b['confirmed'] for b in populated) == 4
    assert report['confusion_by_signal'][0]['disagree'] >= 1
    assert report['roof_family'] == {'labels': 1, 'with_decision_row': 1, 'disagree': 1}
    ids = [p['id'] for p in report['proposals']]
    assert 'sample-auto' in ids and 'sample-assist' in ids and 'raise-auto-min' not in ids
    assert all(p['status'] == 'proposed' for p in report['proposals'])
    text = dp.reflection_markdown(report)
    assert '3 of 4 confirmed (75%' in text and 'not accuracy' in text and 'not a random sample' in text


def test_wilson_rejects_bad_counts():
    with pytest.raises(ValueError):
        dp.wilson(5, 4)


def _tmp_scene(tmp_path, scene_id='house-1', status='candidate'):
    folder = tmp_path / 'data/scenes' / scene_id
    folder.mkdir(parents=True)
    latest = {'id': scene_id, 'model_signature': 'sig', 'image_sha256': '0' * 64, 'image_width': 10, 'image_height': 10, 'title': 't',
              'pixel_to_meters': [[1, 0], [0, -1], [0, 0]], 'meters_to_pixel': [[1, 0], [0, -1], [0, 0]],
              'objects': [{'id': 'o1', 'kind': 'building', 'label': 'Building', 'status': 'candidate', 'score': .9, 'roof_type': 'hipped',
                           'ring': [[0, 0], [4, 0], [4, 4], [0, 4]], 'model': {'height_m': 3, 'area_m2': 16},
                           'roof_geometry_note': 'Roof form is uncertain in this image.'},
                          {'id': 't1', 'kind': 'tree', 'label': 'Tree', 'status': 'candidate', 'ring': [[5, 5], [6, 5], [6, 6]]}]}
    (folder / 'latest.json').write_text(json.dumps(latest))
    (folder / 'confidence.json').write_text(json.dumps({'version': 'roof-evidence-v1', 'model_signature': 'sig', 'objects': [
        {'object_id': 'o1', 'band': 'high', 'detector_score': .9, 'geometry_passed': True, 'reasons': []}]}))
    review = {'revision': 1, 'model_signature': 'sig', 'objects': [{'id': 'o1', 'kind': 'building', 'label': 'Building', 'status': status,
                                                                   'geometry_reviewed': False, 'ring': latest['objects'][0]['ring']}]}
    (folder / 'review.json').write_text(json.dumps(review))
    return folder


def test_run_on_a_synthetic_scene_writes_rows_and_summary(tmp_path):
    _tmp_scene(tmp_path)
    summary = dp.run(tmp_path, max_recompute=0, recompute_budget_seconds=0)
    assert summary['objects'] == 1 and summary['rows_written'] == 1 and summary['tiers']['groups']['captures']['total'] == 1
    assert summary['tiers']['groups']['captures:house']['total'] == 1 and summary['tiers']['overall']['total'] == 1
    rows = dp.load_decisions(tmp_path)
    assert len(rows) == 1 and rows[0]['signals']['detector']['band'] == 'high' and rows[0]['signals']['roof']['form_uncertain'] is True
    # No class hint, no allocation, no structure row: never auto without those sidecars.
    assert rows[0]['tier'] != 'auto' and rows[0]['signals']['auto_gates']['gates']['class_hint_primary'] is False
    assert summary['recompute']['skipped_budget'] == summary['recompute']['candidates']
    with pytest.raises(ValueError):
        dp.scene_ids_under(tmp_path, ['nope'])


def test_apply_auto_dry_run_writes_nothing_and_live_refuses(tmp_path):
    folder = _tmp_scene(tmp_path)
    before = (folder / 'review.json').read_bytes()
    payload = _payload('house-1', 'o1', {})
    assert payload['tier'] == 'auto'
    report = dp.apply_auto(tmp_path, dry_run=True, payloads=[payload])
    assert report['mode'] == 'dry-run' and report['would_write'] == 1
    item = report['items'][0]
    assert {k: item[k] for k in ('scene_id', 'object_id', 'label', 'kind', 'roof_type', 'status', 'provenance', 'confidence', 'path', 'existing_human_status')} == {
        'scene_id': 'house-1', 'object_id': 'o1', 'label': 'House roof (hipped)', 'kind': 'building', 'roof_type': 'hipped', 'status': 'confirmed',
        'provenance': 'machine:decision-policy-v2', 'confidence': payload['confidence'], 'path': 'primary-roof', 'existing_human_status': None}
    assert item['interval']['kind'] == 'wilson-95' and item['score_interval']['kind'] == 'design-range'
    assert (folder / 'review.json').read_bytes() == before
    with pytest.raises(SystemExit):
        dp.apply_auto(tmp_path, dry_run=False, acknowledged=False, payloads=[payload])
    assert (folder / 'review.json').read_bytes() == before
    decided = _payload('house-1', 'o1', {}, status='needs_adjustment')
    report = dp.apply_auto(tmp_path, dry_run=True, payloads=[decided])
    assert report['would_write'] == 0 and report['skipped_existing_human_decision'] == 1


def test_cli_apply_auto_defaults_to_dry_run(tmp_path, capsys):
    folder = _tmp_scene(tmp_path)
    before = (folder / 'review.json').read_bytes()
    assert dp.main(['--root', str(tmp_path), '--apply-auto']) == 0
    assert (folder / 'review.json').read_bytes() == before
    assert json.loads(capsys.readouterr().out)['mode'] == 'dry-run'
    assert (tmp_path / dp.REPORT_DIR / 'apply-auto-dry-run.json').exists()


def test_replace_version_touches_only_this_version(tmp_path):
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, dp.TABLE, [{'scene_id': 's', 'object_id': 'old', 'version': dp.VERSION, 'payload': {'tier': 'review'}},
                                               {'scene_id': 's', 'object_id': 'o', 'version': 'other-v0', 'payload': {'tier': 'auto'}}])
        written = dp.replace_version(db, [_payload('s', 'new', {})])
        assert written == 1
        mine = eyes_index.rows(db, dp.TABLE, version=dp.VERSION)
        assert [r['object_id'] for r in mine] == ['new']
        other = eyes_index.rows(db, dp.TABLE, version='other-v0')
        assert len(other) == 1 and other[0]['payload'] == {'tier': 'auto'}


def test_scene_group_and_roof_signals():
    assert dp.scene_group('mooroolbark-50011498-17-118442-80417') == ('suburb', 'suburb')
    assert dp.scene_group('roof_training_250-30371') == ('captures', 'captures:roof_training_250')
    assert dp.roof_signals({'roof_type': 'flat', 'roof_geometry_note': 'Flat roof: large'})['roof_form'] == 'flat'
    assert dp.roof_signals({'roof_type': None, 'roof_geometry_note': 'Roof surface could not be fitted.'})['roof_form'] is None
    assert dp.roof_signals({'roof_type': 'gabled', 'roof_geometry_note': 'Roof form is uncertain in this image.'}) == {
        'roof_type': 'gabled', 'roof_form': 'pitched', 'roof_geometry_source': None, 'form_uncertain': True, 'note_flat': False,
        'roof_fitted': False, 'area_m2': None, 'height_m': None}


def test_intervals_follow_the_reflection_join_and_score_interval_is_a_design_range():
    auto = [_payload('s', f'a{i}', {}) for i in range(4)]
    shed = {'detector': {'band': 'low', 'score': .55}, 'class_hints': {'class_hint': 'outbuilding', 'flags': ['secondary-structure']},
            'structure': {'sub_kind': 'shed', 'confidence': 'strong', 'secondary_flag': True}, 'allocation': {'status': 'matched', 'confidence': 'moderate'}}
    sheds = [_payload('s', f'g{i}', shed) for i in range(2)]
    humans = [_human('s', 'a0', 'confirmed'), _human('s', 'a1', 'confirmed'), _human('s', 'a2', 'needs_adjustment')]
    sheds[0]['signals']['reference'] = {'tier': 'T2', 'provenance': 'review:roof_line_edits'}   # judged through a reference, not confirmed
    strata = dp.attach_intervals(auto + sheds, humans)
    assert strata == {'auto/primary-roof/house': {'confirmed': 2, 'judged': 3}, 'auto/shed': {'confirmed': 0, 'judged': 1}}
    assert auto[3]['interval'] == {'kind': 'wilson-95', 'stratum': 'auto/primary-roof/house', 'confirmed': 2, 'judged': 3,
                                   'lower': pytest.approx(.2077, abs=1e-3), 'upper': pytest.approx(.9385, abs=1e-3), 'note': dp.INTERVAL_NOTE}
    assert sheds[1]['interval']['judged'] == 1 and sheds[1]['interval']['confirmed'] == 0 and sheds[1]['interval']['lower'] == 0.
    # score_interval: band table for primary roofs (high .80, nearest medium .50 -> half gap .15), structure table for secondary structures.
    assert auto[0]['score_interval']['basis'] == 'band' and auto[0]['score_interval']['width'] == .15
    assert sheds[0]['score_interval']['basis'] == 'structure' and sheds[0]['score_interval']['width'] == .125
    assert auto[0]['score_interval']['upper'] <= 1 and auto[0]['score_interval']['lower'] == round(auto[0]['confidence'] - .15, 4)
    bare = dp.decide(signals(class_hints={'version': None}, structure={'version': None}, allocation={'version': None}))
    assert bare['score_interval']['missing_sidecars'] == 3 and bare['score_interval']['width'] == pytest.approx(.15 + .09)
    # A T1 reference counts as confirmed even without a review.json decision.
    ref = _payload('s', 'r0', {})
    ref['signals']['reference'] = {'tier': 'T1', 'provenance': 'review:confirmed'}
    assert dp.strata_counts([ref], []) == {'auto/primary-roof/house': {'confirmed': 1, 'judged': 1}}


def test_human_decisions_exclude_machine_labelled_objects(tmp_path):
    folder = _tmp_scene(tmp_path)
    review = json.loads((folder / 'review.json').read_text())
    review['objects'][0].update(status='confirmed', geometry_reviewed=True, provenance={'source': 'machine:decision-policy-v2', 'tier': 'auto'})
    review['objects'].append({'id': 'h1', 'kind': 'building', 'label': 'x', 'status': 'confirmed', 'geometry_reviewed': True, 'ring': [[0, 0], [1, 0], [1, 1]]})
    (folder / 'review.json').write_text(json.dumps(review))
    humans = dp.human_decisions(tmp_path)
    assert [h['object_id'] for h in humans] == ['h1']
    assert dp.is_machine_labelled({'provenance': 'machine:decision-policy-v1', 'status': 'confirmed'}) and not dp.is_machine_labelled({'provenance': None})
    # A human who changes the status of a machine label turns it into a human decision.
    assert not dp.is_machine_labelled({'provenance': {'source': 'machine:decision-policy-v2'}, 'status': 'rejected'})
    assert dp.has_machine_provenance({'provenance': {'source': 'machine:decision-policy-v2'}, 'status': 'rejected'})


def _live(tmp_path, payloads, **kwargs):
    audit = tmp_path / 'audit.json'
    report = dp.apply_auto(tmp_path, dry_run=False, acknowledged=True, payloads=payloads, audit_path=audit,
                           backup_dir=tmp_path / 'backup', **kwargs)
    return report, audit


def test_live_apply_creates_a_schema_valid_review_json_with_every_model_object(tmp_path):
    folder = _tmp_scene(tmp_path)
    (folder / 'review.json').unlink()
    payload = _payload('house-1', 'o1', {})
    report, audit = _live(tmp_path, [payload])
    assert report['written'] == 1 and report['files_created'] == 1 and report['files_updated'] == 0 and report['skipped_live'] == {}
    review = json.loads((folder / 'review.json').read_text())
    assert review['revision'] == 1 and review['model_signature'] == 'sig' and review['reviewed_classes'] == [] and review['history'] == []
    assert review['basemap_mode'] == 'unknown' and review['updated_by'] == 'machine:decision-policy-v2' and review['groups'] == []
    assert [o['id'] for o in review['objects']] == ['o1', 't1']            # the editor reads review.objects as the whole set
    o1, t1 = review['objects']
    assert o1['status'] == 'confirmed' and o1['geometry_reviewed'] is True and o1['label'] == 'House roof (hipped)' and o1['kind'] == 'building'
    assert o1['roof_type'] == 'hipped' and o1['ring'] == [[0, 0], [4, 0], [4, 4], [0, 4]] and o1['model'] == {'height_m': 3, 'area_m2': 16}
    assert o1['provenance'] == {'source': 'machine:decision-policy-v2', 'tier': 'auto', 'confidence': payload['confidence'], 'interval': payload['interval']}
    assert 'decision-policy-v2 decision-policy agent' in o1['notes']
    assert t1['status'] == 'candidate' and 'provenance' not in t1
    log = json.loads(audit.read_text())
    write = log['writes'][0]
    assert write['existed'] is False and write['before_sha256'] is None
    assert write['changes'][0]['before']['status'] == 'candidate' and write['changes'][0]['before']['provenance'] is None   # the model object's state
    assert write['changes'][0]['after']['status'] == 'confirmed' and (tmp_path / 'backup/house-1/ABSENT').exists()
    assert log['backup_dir'] == str(tmp_path / 'backup') and 'rollback-auto' in log['rollback']
    # Idempotent: a second live run finds the object already machine-labelled and writes nothing.
    again, _ = _live(tmp_path, [payload])
    assert again['written'] == 0 and again['skipped_live'] == {'already_decided_or_machine_labelled': 1}


def test_live_apply_updates_in_place_and_leaves_human_decisions_untouched(tmp_path):
    folder = _tmp_scene(tmp_path)
    review = json.loads((folder / 'review.json').read_text())
    human = {'id': 'h1', 'kind': 'building', 'label': 'Human house', 'status': 'needs_adjustment', 'geometry_reviewed': True,
             'ring': [[6, 6], [8, 6], [8, 8]], 'notes': 'human note'}
    review['objects'].append(human)
    review.update(basemap_mode='raw', updated_by='local', history=[{'revision': 0}])
    (folder / 'review.json').write_text(json.dumps(review))
    payloads = [_payload('house-1', 'o1', {}), _payload('house-1', 'h1', {})]
    report, audit = _live(tmp_path, payloads)
    assert report['written'] == 1 and report['files_updated'] == 1 and report['skipped_live'] == {'human_decided': 1}
    saved = json.loads((folder / 'review.json').read_text())
    assert saved['revision'] == 2 and saved['basemap_mode'] == 'raw' and saved['updated_by'] == 'machine:decision-policy-v2'
    assert len(saved['history']) == 2 and saved['history'][1]['revision'] == 1 and 'history' not in saved['history'][1]
    by_id = {o['id']: o for o in saved['objects']}
    assert by_id['h1'] == human                                            # byte-for-byte the human's object
    assert by_id['o1']['status'] == 'confirmed' and by_id['o1']['provenance']['tier'] == 'auto'
    assert (tmp_path / 'backup/house-1/review.json').read_text() == json.dumps(review)
    write = json.loads(audit.read_text())['writes'][0]
    assert write['existed'] and write['revision_before'] == 1 and write['revision_after'] == 2
    assert write['changes'][0]['before']['status'] == 'candidate' and write['changes'][0]['after']['status'] == 'confirmed'


def test_live_apply_refuses_stale_signature_files(tmp_path):
    folder = _tmp_scene(tmp_path)
    before = (folder / 'review.json').read_bytes()
    review = json.loads(before)
    review['model_signature'] = 'older'
    (folder / 'review.json').write_text(json.dumps(review))
    stale_bytes = (folder / 'review.json').read_bytes()
    report, audit = _live(tmp_path, [_payload('house-1', 'o1', {})])
    assert report['written'] == 0 and report['skipped_live'] == {'stale_review_signature': 1} and report['writes'] == []
    assert (folder / 'review.json').read_bytes() == stale_bytes and not (tmp_path / 'backup/house-1').exists()
    assert not (folder / 'review.lock').exists()


def test_rollback_restores_the_exact_prior_state(tmp_path):
    existing = _tmp_scene(tmp_path, 'house-1')
    created = _tmp_scene(tmp_path, 'house-2')
    (created / 'review.json').unlink()
    before = existing.joinpath('review.json').read_bytes()
    payloads = [_payload('house-1', 'o1', {}), _payload('house-2', 'o1', {})]
    report, audit = _live(tmp_path, payloads)
    assert report['written'] == 2 and (created / 'review.json').exists() and existing.joinpath('review.json').read_bytes() != before
    # A file changed after the machine write is left alone.
    tampered = _tmp_scene(tmp_path, 'house-3')
    (tampered / 'review.json').unlink()
    audit3 = tmp_path / 'audit3.json'
    dp.apply_auto(tmp_path, dry_run=False, acknowledged=True, payloads=[_payload('house-3', 'o1', {})], audit_path=audit3, backup_dir=tmp_path / 'backup3')
    (tampered / 'review.json').write_text('{"changed": true}')
    rolled = dp.rollback_auto(tmp_path, audit)
    assert rolled['restored'] == 1 and rolled['deleted'] == 1 and rolled['skipped'] == {}
    assert existing.joinpath('review.json').read_bytes() == before and not (created / 'review.json').exists()
    assert Path(rolled['report_path']).exists()
    rolled3 = dp.rollback_auto(tmp_path, audit3)
    assert rolled3['skipped'] == {'changed_since_write': 1} and (tampered / 'review.json').read_text() == '{"changed": true}'


def test_cli_live_requires_the_acknowledgement_flag(tmp_path):
    folder = _tmp_scene(tmp_path)
    before = (folder / 'review.json').read_bytes()
    with pytest.raises(SystemExit):
        dp.main(['--root', str(tmp_path), '--apply-auto', '--live'])
    assert (folder / 'review.json').read_bytes() == before


def test_a_reviewer_confirming_a_machine_label_counts_as_a_human_decision():
    from scripts.ml import decision_policy as dp
    machine = {'id': 'm', 'status': 'confirmed', 'geometry_reviewed': True, 'provenance': {'source': 'machine:decision-policy-v2', 'tier': 'auto'}}
    assert dp.is_machine_labelled(machine) is True
    confirmed = {**machine, 'provenance': {**machine['provenance'], 'human_confirmed': True, 'human_confirmed_at': '2026-09-27T10:00:00Z'}}
    assert dp.is_machine_labelled(confirmed) is False
    adjusted = {**machine, 'status': 'needs_adjustment'}
    assert dp.is_machine_labelled(adjusted) is False
