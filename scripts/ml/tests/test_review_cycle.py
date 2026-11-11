"""review-cycle-v1: idempotence, protected-scene skip, approved-only proposal application, cycle diff arithmetic."""
import json
from contextlib import closing
from pathlib import Path

import pytest
from scripts.ml import decision_policy as dp
from scripts.ml import eyes_index, structure_kinds
from scripts.ml import review_cycle as rc

RING = [[0, 0], [4, 0], [4, 4], [0, 4]]


def make_scene(root, scene_id, *, with_review=False, confidence=False, groups=None):
    folder = root / 'data/scenes' / scene_id
    folder.mkdir(parents=True)
    latest = {'id': scene_id, 'model_signature': 'sig', 'image_sha256': '0' * 64, 'image_width': 10, 'image_height': 10, 'title': scene_id,
              'pixel_to_meters': [[1, 0], [0, -1], [0, 0]], 'meters_to_pixel': [[1, 0], [0, -1], [0, 0]], 'groups': groups or [],
              'objects': [{'id': 'o1', 'kind': 'building', 'label': 'Building', 'status': 'candidate', 'score': .9, 'roof_type': 'hipped',
                           'ring': RING, 'model': {'height_m': 3, 'area_m2': 16}, 'geometry_reviewed': False, 'source': 'sam3',
                           'roof_geometry_note': 'Roof form is uncertain in this image.'}]}
    (folder / 'latest.json').write_text(json.dumps(latest))
    if confidence:
        (folder / 'confidence.json').write_text(json.dumps({'version': 'roof-evidence-v1', 'model_signature': 'sig', 'objects': [
            {'object_id': 'o1', 'band': 'high', 'detector_score': .9, 'geometry_passed': True, 'reasons': []}]}))
    if with_review:
        (folder / 'review.json').write_text(json.dumps({'revision': 1, 'model_signature': 'sig', 'groups': [], 'reviewed_classes': [],
                                                          'objects': [{**latest['objects'][0]}]}))
    return folder


def fake_assess(root):
    calls = []

    def assess(scene_id):
        calls.append(scene_id)
        folder = root / 'data/scenes' / scene_id
        report = {'version': 'roof-evidence-v1', 'model_signature': 'sig', 'objects': [
            {'object_id': 'o1', 'band': 'high', 'detector_score': .9, 'geometry_passed': True, 'reasons': []}]}
        (folder / 'confidence.json').write_text(json.dumps(report))
        return report
    assess.calls = calls
    return assess


def fake_refreshers(root):
    calls = {'hints': [], 'structure': []}

    def hints(_root, scene_ids):
        calls['hints'].append(list(scene_ids))
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            eyes_index.replace_rows(db, 'object_quality', [
                {'scene_id': s, 'object_id': 'o1', 'model_signature': 'sig', 'version': 'building-flags-v2',
                 'payload': {'code_sha256': 'x', 'class_hints': {'class_hint': 'house', 'flags': []}, 'score': .9}} for s in scene_ids])
        return {'scenes': len(scene_ids)}

    def structure(_root, scene_ids):
        calls['structure'].append(list(scene_ids))
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            eyes_index.replace_rows(db, 'object_quality', [
                {'scene_id': s, 'object_id': 'o1', 'model_signature': 'sig', 'version': structure_kinds.VERSION,
                 'payload': {'code_sha256': 'y', 'sub_kind': 'not-secondary', 'confidence': 'weak', 'eligibility': {'secondary_structure_flag': False}}}
                for s in scene_ids])
        return {'scenes': len(scene_ids)}
    return {'hints': hints, 'structure': structure}, calls


def allocate(root, scene_ids):
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'address_allocation', [
            {'scene_id': s, 'object_id': 'o1', 'version': 'address-allocation-v1',
             'payload': {'status': 'matched', 'confidence': 'strong', 'method': 'saved-link', 'full_address': '1 Test St'}} for s in scene_ids])


def run(root, refreshers, assess, **kwargs):
    return rc.run_cycle(root, recompute_budget_seconds=0, max_recompute=0, refreshers=refreshers, assess=assess, **kwargs)


def test_cycle_is_idempotent_and_refreshes_only_when_rows_are_missing(tmp_path):
    make_scene(tmp_path, 'house-1')
    make_scene(tmp_path, 'mooroolbark-1-17-1-1')          # a tile: never assessed by the cycle
    allocate(tmp_path, ['house-1', 'mooroolbark-1-17-1-1'])
    refreshers, calls = fake_refreshers(tmp_path)
    assess = fake_assess(tmp_path)
    first = run(tmp_path, refreshers, assess)
    assert assess.calls == ['house-1']
    assert first['capture_assessment']['scenes_assessed'] == 1 and first['capture_assessment']['skipped'] == {'tile_never_assessed_here': 1}
    assert first['refresh']['reasons'] == {'hints_missing': 2, 'structure_missing': 2}
    assert first['refresh']['executed']['hints']['status'] == 'refreshed' and calls['hints'] == [['house-1', 'mooroolbark-1-17-1-1']]
    assert first['decision']['objects'] == 2 and first['diff']['previous'] is None and 'first cycle' in first['diff']['note']
    assert (tmp_path / rc.CYCLE_DIR / f"{first['cycle_id']}.json").exists() and (tmp_path / rc.CYCLE_DIR / 'CYCLE.md').exists()
    assert (tmp_path / rc.CYCLE_DIR / 'proposals.json').exists() and (tmp_path / rc.CYCLE_DIR / 'REFLECTION.md').exists()
    assert not (tmp_path / dp.THRESHOLDS_FILE).exists()                       # the cycle applies nothing
    assert first['auto_labels'] is None and not (tmp_path / 'data/scenes/house-1/review.json').exists()   # and writes no labels
    second = run(tmp_path, refreshers, assess)
    assert assess.calls == ['house-1'] and len(calls['hints']) == 1 and len(calls['structure']) == 1
    assert second['capture_assessment']['scenes_assessed'] == 0 and second['capture_assessment']['skipped']['already_has_confidence'] == 1
    assert second['refresh']['reasons'] == {} and second['refresh']['executed'] == {
        'hints': {'status': 'skipped', 'reason': 'rows present for the current models', 'scenes': 0},
        'structure': {'status': 'skipped', 'reason': 'rows present for the current models', 'scenes': 0}}
    assert second['diff']['previous'] == first['cycle_id'] and second['diff']['new_decisions']['total'] == 0
    assert second['decision']['tiers'] == first['decision']['tiers']
    assert len(list((tmp_path / rc.CYCLE_DIR).glob('cycle-*.json'))) == 2
    text = (tmp_path / rc.CYCLE_DIR / 'CYCLE.md').read_text()
    assert 'never accuracy' in text and '## Next review actions' in text and 'applied no thresholds and wrote no labels.' in text


def test_refresh_plan_flags_rows_signed_by_another_model(tmp_path):
    make_scene(tmp_path, 'house-1')
    refreshers, _ = fake_refreshers(tmp_path)
    refreshers['hints'](tmp_path, ['house-1'])
    refreshers['structure'](tmp_path, ['house-1'])
    assert rc.refresh_plan(tmp_path)['reasons'] == {}
    latest = tmp_path / 'data/scenes/house-1/latest.json'
    doc = json.loads(latest.read_text())
    doc['model_signature'] = 'sig2'
    latest.write_text(json.dumps(doc))
    plan = rc.refresh_plan(tmp_path)
    assert plan['reasons'] == {'hints_other_model': 1, 'structure_other_model': 1} and plan['hint_scenes'] == ['house-1']
    assert plan['code_drift']['hints']['drift'] is True                        # fake rows carry code sha 'x'


def test_protected_scenes_are_skipped_by_rule(tmp_path):
    make_scene(tmp_path, 'house-1', confidence=True)
    make_scene(tmp_path, 'house-2', confidence=True)
    allocate(tmp_path, ['house-1', 'house-2'])
    manifest = tmp_path / 'data/references/roof-checkpoints/abcd1234/manifest.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'scene_id': 'house-1', 'model_signature': 'sig', 'files': {}}))
    assert dp.protected_scene_ids(tmp_path) == {'house-1'}
    refreshers, _ = fake_refreshers(tmp_path)
    cycle = run(tmp_path, refreshers, fake_assess(tmp_path), auto_labels=True)
    assert cycle['decision']['tiers']['live']['auto'] == 2
    assert cycle['protected_scenes'] == ['house-1']
    assert cycle['auto_labels']['written'] == 1 and cycle['auto_labels']['skipped_protected_reference_scene'] == 1
    assert not (tmp_path / 'data/scenes/house-1/review.json').exists() and (tmp_path / 'data/scenes/house-2/review.json').exists()
    # A direct live apply obeys the same rule without being told the list.
    rows = dp.load_decisions(tmp_path)
    report = dp.apply_auto(tmp_path, dry_run=False, acknowledged=True, payloads=rows, audit_path=tmp_path / 'audit2.json', backup_dir=tmp_path / 'b2')
    assert report['skipped_protected_reference_scene'] == 1 and report['written'] == 0
    assert report['skipped_live'] == {'already_decided_or_machine_labelled': 1}      # never re-labels
    dry = dp.apply_auto(tmp_path, dry_run=True, payloads=rows)
    assert dry['skipped_protected_reference_scene'] == 1 and dry['protected_scenes'] == ['house-1']


def test_apply_approved_applies_only_approved_changes_and_rolls_back(tmp_path):
    proposals = tmp_path / 'proposals.json'
    proposals.write_text(json.dumps({'proposals': [
        {'id': 'raise-auto-min', 'kind': 'threshold', 'approved': True, 'change': {'path': ['auto_min'], 'value': .99}},
        {'id': 'penalise-band-medium', 'kind': 'threshold', 'approved': False, 'change': {'path': ['signal_penalties', 'band=medium'], 'value': -.1}},
        {'id': 'sample-auto', 'kind': 'sampling', 'approved': True},
        {'id': 'penalise-clipped-True', 'kind': 'threshold', 'approved': True, 'change': {'path': ['signal_penalties', 'clipped=True'], 'value': -.2}},
    ]}))
    report = rc.apply_approved(tmp_path, proposals, backup_dir=tmp_path / 'backup', audit_path=tmp_path / 'audit.json')
    assert [a['id'] for a in report['applied']] == ['raise-auto-min', 'penalise-clipped-True']
    assert report['acknowledged_without_change'] == ['sample-auto'] and report['ignored_not_approved'] == ['penalise-band-medium']
    doc = json.loads((tmp_path / dp.THRESHOLDS_FILE).read_text())
    assert [o['path'] for o in doc['overrides']] == [['auto_min'], ['signal_penalties', 'clipped=True']]
    effective = dp.effective_thresholds(tmp_path)
    assert effective['auto_min'] == .99 and effective['signal_penalties'] == {'clipped=True': -.2} and effective['version'].startswith('decision-policy-thresholds-v2+')
    assert effective['band_base'] == dp.THRESHOLDS['band_base'] and 'signal_penalties' not in dp.THRESHOLDS or dp.THRESHOLDS['signal_penalties'] == {}
    assert (tmp_path / 'backup/thresholds.ABSENT').exists() and report['writes'][0]['existed'] is False
    # Penalties and the raised minimum take effect through decide().
    base = dp.decide(_signals(), dp.THRESHOLDS)
    tuned = dp.decide(_signals(), effective)
    assert base['tier'] == 'auto' and tuned['tier'] == 'assist' and tuned['thresholds_version'] == effective['version']
    clipped = dp.decide(_signals(detector={'clipped': True}), effective)
    assert clipped['confidence'] == round(max(0., dp.decide(_signals(detector={'clipped': True}), dp.THRESHOLDS)['confidence'] - .2), 4)
    # Re-applying replaces the override for the same path instead of stacking; rollback restores the prior state exactly.
    second = rc.apply_approved(tmp_path, proposals, backup_dir=tmp_path / 'backup2', audit_path=tmp_path / 'audit2.json')
    assert len(json.loads((tmp_path / dp.THRESHOLDS_FILE).read_text())['overrides']) == 2 and second['writes'][0]['existed'] is True
    rolled = dp.rollback_auto(tmp_path, tmp_path / 'audit2.json')
    assert rolled['restored'] == 1 and json.loads((tmp_path / dp.THRESHOLDS_FILE).read_text())['overrides'] == doc['overrides']
    rolled_first = dp.rollback_auto(tmp_path, tmp_path / 'audit.json')
    assert rolled_first['deleted'] == 1 and not (tmp_path / dp.THRESHOLDS_FILE).exists()
    assert dp.effective_thresholds(tmp_path)['version'] == dp.THRESHOLDS['version']
    # Nothing approved: nothing written.
    proposals.write_text(json.dumps({'proposals': [{'id': 'x', 'approved': False, 'change': {'path': ['auto_min'], 'value': .5}}]}))
    none = rc.apply_approved(tmp_path, proposals)
    assert none['applied'] == [] and none['writes'] == [] and not (tmp_path / dp.THRESHOLDS_FILE).exists()


def _signals(**over):
    base = {'scene': {'scene_id': 'house-9', 'group': 'captures', 'subgroup': 'captures:house', 'model_signature': 'm', 'owned': True, 'title': 't'},
            'object_id': 'o1',
            'detector': {'band': 'high', 'score': .9, 'geometry_passed': True, 'clipped': False, 'stale': False, 'source': 'confidence.json'},
            'class_hints': {'class_hint': 'house', 'flags': [], 'version': dp.HINT_VERSION},
            'structure': {'sub_kind': 'not-secondary', 'confidence': 'weak', 'secondary_flag': False, 'version': dp.STRUCTURE_VERSION},
            'roof': {'roof_type': 'hipped', 'roof_form': 'pitched', 'form_uncertain': False, 'roof_fitted': True},
            'diagnostics': {'present': False}, 'allocation': {'status': 'matched', 'confidence': 'strong', 'version': dp.ALLOCATION_VERSION},
            'cross_capture': {'n_pairs': 0, 'band_disagreement': False}, 'review': {'present': False, 'status': None, 'live': True},
            'reference': None, 'recompute': None}
    for key, value in over.items():
        base[key] = {**base[key], **value} if isinstance(value, dict) else value
    return base


def _cycle(cycle_id, per_tier, decisions, strata):
    return {'cycle_id': cycle_id,
            'reflection': {'per_tier': {t: {'confirmed_share': {'n': n, 'confirmed': c, 'share': (round(c / n, 4) if n else None)}} for t, (c, n) in per_tier.items()}},
            'human_decisions': decisions, 'human_decision_keys': sorted(rc.decision_keys(decisions)),
            'auto_strata_judged': [{'stratum': s} for s in strata]}


def test_cycle_diff_arithmetic():
    old = [{'scene_id': 's', 'object_id': 'a', 'kind': 'building', 'status': 'confirmed', 'source': 'other queue'}]
    new = old + [{'scene_id': 's', 'object_id': 'b', 'kind': 'building', 'status': 'rejected', 'source': 'machine-labelled table'},
                 {'scene_id': 's', 'object_id': 'c', 'kind': 'building', 'status': 'confirmed', 'source': 'calibration plan'},
                 {'scene_id': 's', 'object_id': 'd', 'kind': 'building', 'status': 'candidate', 'source': 'structure cards'}]
    previous = _cycle('cycle-1', {'auto': (0, 0), 'assist': (6, 9), 'review': (40, 48)}, old, [])
    current = _cycle('cycle-2', {'auto': (1, 2), 'assist': (7, 11), 'review': (40, 48)}, new, ['auto/garage'])
    diff = rc.diff_cycles(previous, current)
    assert diff['previous'] == 'cycle-1'
    assert diff['per_tier']['auto'] == {'judged_before': 0, 'judged_after': 2, 'judged_delta': 2, 'confirmed_before': 0, 'confirmed_after': 1,
                                        'share_before': None, 'share_after': .5, 'share_delta': None}
    assert diff['per_tier']['assist']['judged_delta'] == 2 and diff['per_tier']['assist']['share_delta'] == pytest.approx(round(7 / 11 - 6 / 9, 4))
    assert diff['per_tier']['review']['share_delta'] == 0
    assert diff['new_decisions']['total'] == 2                                # the candidate is not a decision
    assert diff['new_decisions']['by_source'] == {'calibration plan': {'confirmed': 1}, 'machine-labelled table': {'rejected': 1}}
    assert diff['auto_strata_newly_judged'] == [{'stratum': 'auto/garage'}]
    assert rc.diff_cycles(None, current)['new_decisions']['total'] == 0


def test_classify_sources_precedence_and_assess_captures_audit(tmp_path):
    decisions = [{'scene_id': 's', 'object_id': o, 'kind': 'building', 'status': 'confirmed'} for o in 'abcd']
    out = rc.classify_sources(decisions, {('s', 'a'), ('s', 'b')}, {('s', 'b'), ('s', 'c')}, {('s', 'c'), ('s', 'd')})
    assert [h['source'] for h in out] == ['machine-labelled table', 'machine-labelled table', 'calibration plan', 'structure cards']
    make_scene(tmp_path, 'map-1')
    make_scene(tmp_path, 'house-1', confidence=True)
    existing = (tmp_path / 'data/scenes/house-1/confidence.json').read_bytes()
    make_scene(tmp_path, 'mooroolbark-1-17-1-1')
    summary = rc.assess_captures(tmp_path, assess=fake_assess(tmp_path))
    assert summary['scenes_assessed'] == 1 and summary['objects_banded'] == 1 and summary['band_distribution'] == {'high': 1}
    assert summary['skipped'] == {'already_has_confidence': 1, 'tile_never_assessed_here': 1}
    assert (tmp_path / 'data/scenes/house-1/confidence.json').read_bytes() == existing        # never overwritten
    assert not (tmp_path / 'data/scenes/mooroolbark-1-17-1-1/confidence.json').exists()          # tiles untouched
    audits = list((tmp_path / rc.CYCLE_DIR).glob('capture-assessment-*.json'))
    assert len(audits) == 1 and json.loads(audits[0].read_text())['created'][0]['scene_id'] == 'map-1'


def test_cli_run_and_apply_approved(tmp_path, capsys, monkeypatch):
    make_scene(tmp_path, 'house-1', confidence=True)
    refreshers, _ = fake_refreshers(tmp_path)
    monkeypatch.setattr(rc, 'default_refreshers', lambda: refreshers)
    assert rc.main(['--root', str(tmp_path), '--run', '--max-recompute', '0', '--recompute-budget-seconds', '0']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['cycle_id'].startswith('cycle-') and out['tiers']['total'] == 1
    assert Path(out['cycle_id']) and (tmp_path / rc.CYCLE_DIR / 'CYCLE.md').exists()
