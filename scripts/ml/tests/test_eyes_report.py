import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from scripts.ml import eyes_index, eyes_report

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN = 'data/suburbs/mooroolbark/runs/500114985968d40b'
CONFIDENCE = 'data/suburbs/mooroolbark/confidence.sqlite'

# (scene, object, band, mismatch, segments, profile)
ROOFS = [
    ('tile-a', 'r1', 'low', 1.3532, 4, 'P'),
    ('tile-a', 'r2', 'medium', 0.4, 6, 'P'),
    ('tile-a', 'r3', 'medium', 1.5, 3, 'P'),
    ('tile-b', 'r4', 'high', 0.2, 2, 'P'),      # segments < 3 -> excluded from quantiles
    ('tile-b', 'r5', 'medium', None, 0, 'P'),   # zero segments -> null mismatch
    ('house-1', 'h1', 'medium', 0.9, 5, 'Q'),   # not owned (not in run)
]


def _roof_payload(scene, obj, band, mismatch, segments, profile):
    return {'type': 'Feature', 'geometry': None, 'properties': {
        'scene_id': scene, 'object_id': obj, 'band': band, 'image_line_mismatch_m': mismatch,
        'observed_segments': segments, 'profile': profile, 'detector_score': .7, 'clipped': False, 'area_m2': 100.0}}


def make_root(tmp_path, *, run_json=True, sidecars=False):
    root = tmp_path
    conf = root / CONFIDENCE
    conf.parent.mkdir(parents=True)
    with sqlite3.connect(conf) as db:
        db.execute('CREATE TABLE scenes(id TEXT PRIMARY KEY, signature TEXT, mtime INTEGER, bounds TEXT)')
        db.execute('CREATE TABLE roofs(scene_id TEXT, object_id TEXT, west REAL, south REAL, east REAL, north REAL, payload TEXT, PRIMARY KEY(scene_id,object_id))')
        for sid in ('tile-a', 'tile-b', 'house-1'):
            db.execute('INSERT INTO scenes VALUES(?,?,?,?)', (sid, 's', 1, '[0,0,1,1]'))
        for row in ROOFS:
            db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', (row[0], row[1], 0, 0, 1, 1, json.dumps(_roof_payload(*row))))
    code_dir = root / 'scripts/ml'
    code_dir.mkdir(parents=True)
    (code_dir / 'same.py').write_text('x = 1\n')
    (code_dir / 'changed.py').write_text('x = 2\n')
    if run_json:
        run = root / RUN
        run.mkdir(parents=True)
        (run / 'run.json').write_text(json.dumps({'status': 'complete', 'results': {
            'tile-a': {'status': 'complete', 'confidence': {'low': 1, 'medium': 2}},
            'tile-b': {'status': 'complete', 'confidence': {'high': 1, 'medium': 1}}}}))
        (run / 'plan.json').write_text(json.dumps({'version': 1, 'code': {
            'scripts/ml/same.py': hashlib.sha256(b'x = 1\n').hexdigest(),
            'scripts/ml/changed.py': hashlib.sha256(b'old\n').hexdigest(),
            'scripts/ml/gone.py': 'f' * 64}}))
    if sidecars:
        with eyes_index.connection(root) as db:
            eyes_index.ensure_schema(db)
            eyes_index.replace_rows(db, 'shadow_bands', [
                {'scene_id': 'tile-a', 'object_id': 'r1', 'model_signature': 'm', 'version': 'v2', 'payload': {'band_v1': 'low', 'band_v2': 'medium', 'flip': True}},
                {'scene_id': 'tile-a', 'object_id': 'r2', 'model_signature': 'm', 'version': 'v2', 'payload': {'band_v1': 'medium', 'band_v2': 'medium', 'flip': False}},
                {'scene_id': 'tile-b', 'object_id': 'r4', 'model_signature': 'm', 'version': 'v2', 'payload': {'band_v1': 'high', 'band_v2': 'high', 'flip': False}}])
            eyes_index.replace_rows(db, 'cross_capture', [
                {'scene_id': 'tile-a', 'object_id': 'r1', 'other_scene_id': 'house-1', 'other_object_id': 'h1', 'version': 'v', 'payload': {'iou': .8, 'band_disagreement': True, 'hidden_by': 'house-1/h1'}},
                {'scene_id': 'tile-a', 'object_id': 'r2', 'other_scene_id': 'house-1', 'other_object_id': 'h2', 'version': 'v', 'payload': {'iou': .5, 'band_disagreement': False, 'hidden_by': None}}])
            eyes_index.replace_rows(db, 'references', [
                {'ref_id': 'ref-t2', 'tier': 'T2', 'source_kind': 'crm_roof', 'version': 'v', 'payload': {}},
                {'ref_id': 'ref-t3', 'tier': 'T3', 'source_kind': 'agent_trace', 'version': 'v', 'payload': {}}])
            eyes_index.replace_rows(db, 'agreement', [
                {'ref_id': 'ref-t2', 'scene_id': 'tile-a', 'object_id': 'r1', 'model_signature': 'm', 'version': 'v', 'payload': {'iou': .6, 'hausdorff_m': 2.0, 'image_identity': 'different_image'}},
                {'ref_id': 'ref-t2', 'scene_id': 'house-1', 'object_id': 'h1', 'model_signature': 'm', 'version': 'v', 'payload': {'iou': .9, 'hausdorff_m': 1.0, 'image_identity': 'same_image'}},
                {'ref_id': 'ref-t3', 'scene_id': 'tile-a', 'object_id': 'r2', 'model_signature': 'm', 'version': 'v', 'payload': {'iou': .3, 'hausdorff_m': 4.0, 'image_identity': 'unknown'}}])
            eyes_index.replace_rows(db, 'object_quality', [
                {'scene_id': 'tile-a', 'object_id': 't1', 'model_signature': 'm', 'version': 'v', 'payload': {'kind': 'tree', 'unparented': False, 'plausibility_flags': [], 'support_tier': '>=.8'}},
                {'scene_id': 'tile-a', 'object_id': 's1', 'model_signature': 'm', 'version': 'v', 'payload': {'kind': 'solar_panel', 'unparented': True, 'plausibility_flags': ['tiny'], 'support_tier': '<.5'}}])
            eyes_index.replace_rows(db, 'roof_diagnostics', [
                {'scene_id': 'tile-a', 'object_id': 'r1', 'model_signature': 'm', 'version': 'v', 'payload': {'recomputed_matches_stored': True}},
                {'scene_id': 'tile-a', 'object_id': 'r2', 'model_signature': 'm', 'version': 'v', 'payload': {'recomputed_matches_stored': False}},
                {'scene_id': 'tile-a', 'object_id': 'r3', 'model_signature': 'm', 'version': 'v', 'payload': {'error': 'boom'}}])
    return root


def _assert_metrics(node, path='root'):
    """Every leaf metric object carries value, denominator and population."""
    if isinstance(node, dict):
        if 'value' in node:
            assert {'value', 'denominator', 'population'} <= set(node), path
            return
        for key, child in node.items():
            _assert_metrics(child, f'{path}.{key}')


def test_population_is_pinned_to_run_scenes_and_bands_counted(tmp_path):
    root = make_root(tmp_path)
    snap = eyes_report.snapshot(root, persist=False)
    assert snap['version'] == 'eyes-report-v1' and len(snap['code_sha256']) == 64
    pop = snap['population']
    assert pop['source'] == 'run.json'
    assert pop['owned_scan_roofs'] == {'value': 5, 'denominator': 5, 'population': 'owned scan roofs'}
    assert pop['indexed_roofs']['value'] == 6 and pop['indexed_scenes']['value'] == 3
    assert pop['run_reported_roofs']['value'] == 5
    assert pop['profile'] == 'P' and pop['owned_scenes']['value'] == 2
    bands = snap['bands_v1']['indexed']
    assert bands['low'] == {'value': 1, 'denominator': 5, 'population': 'owned scan roofs'}
    assert bands['medium']['value'] == 3 and bands['high']['value'] == 1
    assert snap['bands_v1']['run_reported']['medium']['value'] == 3
    _assert_metrics(snap)


def test_mismatch_quantiles_are_null_and_saturation_aware(tmp_path):
    snap = eyes_report.snapshot(make_root(tmp_path), persist=False)
    mm = snap['mismatch']
    # r1 (1.3532), r2 (0.4), r3 (1.5) eligible; r4 has 2 segments; r5 null; h1 not owned.
    assert mm['eligible']['value'] == 3 and mm['eligible']['denominator'] == 5
    assert mm['null']['value'] == 1 and mm['zero_segments']['value'] == 1
    assert mm['saturated']['value'] == 1 and mm['saturated']['denominator'] == 3
    assert mm['quantiles']['p50']['value'] == pytest.approx(1.3532)
    assert mm['quantiles']['max']['value'] == 1.5
    assert mm['quantiles']['p50']['denominator'] == 3
    assert 'observed_segments>=3' in mm['quantiles']['p50']['population']


def test_population_falls_back_to_dominant_profile_without_run(tmp_path):
    snap = eyes_report.snapshot(make_root(tmp_path, run_json=False), persist=False)
    pop = snap['population']
    assert pop['source'] == 'confidence.sqlite profile' and pop['profile'] == 'P'
    assert pop['owned_scan_roofs']['value'] == 5
    assert pop['run_reported_roofs']['value'] is None
    assert snap['code_drift']['changed']['value'] is None


def test_sidecar_summaries_absent_without_index(tmp_path):
    snap = eyes_report.snapshot(make_root(tmp_path), persist=False)
    assert snap['shadow_v2']['rows']['value'] == 0 and snap['shadow_v2']['bands'] == {}
    assert snap['cross_capture']['rows']['value'] == 0
    assert snap['reference_agreement']['by_tier'] == {}
    assert snap['object_quality']['rows']['value'] == 0
    assert snap['roof_diagnostics']['coverage'] == {'value': 0, 'denominator': 5, 'population': 'owned scan roofs'}
    _assert_metrics(snap)


def test_sidecar_summaries_when_present(tmp_path):
    snap = eyes_report.snapshot(make_root(tmp_path, sidecars=True), persist=False)
    shadow = snap['shadow_v2']
    assert shadow['rows'] == {'value': 3, 'denominator': 5, 'population': 'owned scan roofs'}
    assert shadow['bands']['medium']['value'] == 2 and shadow['bands']['high']['value'] == 1
    assert shadow['flips']['value'] == 1 and shadow['flips']['denominator'] == 3
    assert shadow['flip_matrix']['low->medium']['value'] == 1
    cc = snap['cross_capture']
    assert cc['rows']['value'] == 2 and cc['band_disagreement']['value'] == 1 and cc['hidden']['value'] == 1
    assert cc['band_disagreement']['denominator'] == 2
    agree = snap['reference_agreement']['by_tier']
    assert agree['T2']['rows']['value'] == 2 and agree['T2']['iou_median']['value'] == pytest.approx(.75)
    assert agree['T2']['hausdorff_m_median']['value'] == pytest.approx(1.5)
    assert agree['T2']['image_identity']['same_image']['value'] == 1
    assert agree['T3']['rows']['value'] == 1
    assert snap['reference_agreement']['references']['value'] == 2
    oq = snap['object_quality']
    assert oq['rows']['value'] == 2 and oq['by_kind']['tree']['value'] == 1
    assert oq['unparented']['value'] == 1 and oq['flagged']['value'] == 1
    assert oq['support_tier']['<.5']['value'] == 1
    diag = snap['roof_diagnostics']
    assert diag['coverage']['value'] == 3 and diag['coverage']['denominator'] == 5
    assert diag['errors']['value'] == 1
    assert diag['recomputed_matches_stored'] == {'value': 1, 'denominator': 2, 'population': 'roof_diagnostics rows without error'}
    _assert_metrics(snap)


def test_code_drift_against_plan(tmp_path):
    snap = eyes_report.snapshot(make_root(tmp_path), persist=False)
    drift = snap['code_drift']
    assert drift['changed'] == {'value': 1, 'denominator': 3, 'population': 'plan.json code files'}
    assert drift['missing']['value'] == 1
    assert drift['changed_files'] == ['scripts/ml/changed.py']


def test_protected_reference_checks_come_from_guard(tmp_path):
    snap = eyes_report.snapshot(make_root(tmp_path), persist=False)
    guard = snap['protected_reference_checks']
    assert guard['ok'] is False  # synthetic root has no manifests / CRM db
    assert {c['name'] for c in guard['checks']} >= {'frozen_run', 'crm_feature_geometry', 'confidence_mtimes'}
    assert all(set(c) == {'name', 'ok'} for c in guard['checks'])


def test_snapshot_is_content_addressed_and_persisted(tmp_path):
    root = make_root(tmp_path)
    first = eyes_report.snapshot(root)
    time.sleep(0.01)
    second = eyes_report.snapshot(root)
    assert first['snapshot_id'] == second['snapshot_id'] and len(first['snapshot_id']) == 16
    assert first['created_at'] != second['created_at']
    stored = root / 'data/reviews/eyes/snapshots' / f"{first['snapshot_id']}.json"
    assert json.loads(stored.read_text())['snapshot_id'] == first['snapshot_id']
    with eyes_index.connection(root, readonly=True) as db:
        rows = eyes_index.rows(db, 'snapshots', id=first['snapshot_id'])
    assert len(rows) == 1 and rows[0]['version'] == 'eyes-report-v1'
    assert rows[0]['payload']['population']['owned_scan_roofs']['value'] == 5
    # Content change -> new id.
    with sqlite3.connect(root / CONFIDENCE) as db:
        db.execute("DELETE FROM roofs WHERE object_id='r5'")
    assert eyes_report.snapshot(root, persist=False)['snapshot_id'] != first['snapshot_id']
    assert eyes_report.snapshot_id(first) == first['snapshot_id']


def test_diff_reports_added_removed_changed(tmp_path):
    root = make_root(tmp_path, sidecars=True)
    before = eyes_report.snapshot(root, persist=False)
    with sqlite3.connect(root / CONFIDENCE) as db:
        db.execute("UPDATE roofs SET payload=? WHERE object_id='r1'", (json.dumps(_roof_payload('tile-a', 'r1', 'medium', 1.3532, 4, 'P')),))
    with eyes_index.connection(root) as db:
        db.execute("DELETE FROM shadow_bands WHERE object_id='r4'")
        db.commit()
    after = eyes_report.snapshot(root, persist=False)
    delta = eyes_report.diff(before, after)
    changed = {c['metric']: c for c in delta['changed']}
    assert changed['bands_v1.indexed.low']['before']['value'] == 1 and changed['bands_v1.indexed.low']['after']['value'] == 0
    assert changed['bands_v1.indexed.medium']['after']['value'] == 4
    assert 'shadow_v2.bands.high' in delta['removed'] and delta['added'] == []
    assert all(not m.startswith(('created_at', 'snapshot_id', 'code_sha256')) for m in changed)
    assert eyes_report.diff(before, before) == {'added': [], 'removed': [], 'changed': []}


def test_cli_snapshot_and_diff(tmp_path, capsys):
    root = make_root(tmp_path)
    assert eyes_report.main(['--root', str(root), '--snapshot']) == 0
    first = json.loads(capsys.readouterr().out)
    with sqlite3.connect(root / CONFIDENCE) as db:
        db.execute("DELETE FROM roofs WHERE object_id='r5'")
    assert eyes_report.main(['--root', str(root), '--snapshot']) == 0
    second = json.loads(capsys.readouterr().out)
    assert first['snapshot_id'] != second['snapshot_id']
    assert eyes_report.main(['--root', str(root), '--diff', first['snapshot_id'], second['snapshot_id']]) == 0
    delta = json.loads(capsys.readouterr().out)
    metrics = {c['metric'] for c in delta['changed']}
    assert 'population.owned_scan_roofs' in metrics and 'mismatch.null' in metrics
    assert eyes_report.main(['--root', str(root), '--diff', 'nope', second['snapshot_id']]) == 2


@pytest.mark.skipif(not (REPO_ROOT / CONFIDENCE).exists() or not (REPO_ROOT / RUN / 'run.json').exists(),
                    reason='real repo data not present')
def test_real_repo_snapshot_read_only():
    started = time.perf_counter()
    snap = eyes_report.snapshot(REPO_ROOT, persist=False)
    assert time.perf_counter() - started < 5
    pop = snap['population']
    # 13 of the 257 run tiles detected no buildings.
    assert pop['owned_scan_roofs']['value'] == 10072 and pop['owned_scenes']['denominator'] == 257
    assert pop['signature_matches_run']['value'] == 10072
    assert pop['profile'].startswith('53ea49d5')
    assert pop['indexed_roofs']['value'] >= 10072
    assert snap['mismatch']['eligible']['value'] <= 10072
    assert snap['mismatch']['quantiles']['p50']['value'] is not None
    # Null-aware: every zero-segment roof is null, and the any-segment saturation share excludes them.
    mm = snap['mismatch']
    assert mm['null']['value'] >= mm['zero_segments']['value']
    assert mm['saturated_any_segments']['denominator'] == 10072 - mm['null']['value']
    assert snap['cross_capture']['owned_roofs_hidden']['denominator'] == 10072
    json.dumps(snap, allow_nan=False)
    _assert_metrics(snap)


# --- adversarial-review regressions ---------------------------------------------------------------


def _add_roofs(root, rows):
    with sqlite3.connect(root / CONFIDENCE) as db:
        for scene, obj, properties in rows:
            db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)',
                       (scene, obj, 0, 0, 1, 1, json.dumps({'type': 'Feature', 'geometry': None, 'properties': properties})))


def test_missing_categorical_values_do_not_crash(tmp_path):
    """A roof without a band, or sidecar rows without kind/band_v2, must not break sorting or canonical JSON."""
    root = make_root(tmp_path, sidecars=True)
    _add_roofs(root, [('tile-b', 'r6', {'image_line_mismatch_m': 0.5, 'observed_segments': 4, 'profile': 'P'})])
    with eyes_index.connection(root) as db:
        eyes_index.replace_rows(db, 'object_quality', [
            {'scene_id': 'tile-a', 'object_id': 'x1', 'model_signature': 'm', 'version': 'v', 'payload': {'unparented': False}}])
        eyes_index.replace_rows(db, 'shadow_bands', [
            {'scene_id': 'tile-a', 'object_id': 'r3', 'model_signature': 'm', 'version': 'v2', 'payload': {'band_v1': 'medium', 'flip': False}}])
    snap = eyes_report.snapshot(root, persist=False)
    assert snap['bands_v1']['indexed']['none']['value'] == 1
    assert snap['object_quality']['by_kind']['none']['value'] == 1
    assert snap['shadow_v2']['bands']['none']['value'] == 1
    assert len(snap['snapshot_id']) == 16
    _assert_metrics(snap)


def test_zero_segment_mismatch_is_null_everywhere(tmp_path):
    """The frozen scan stores 0.0 mismatch on zero-segment roofs; those are nulls, never observations."""
    root = make_root(tmp_path)
    _add_roofs(root, [
        ('tile-b', 'z1', {'band': 'medium', 'image_line_mismatch_m': 0.0, 'observed_segments': 0, 'profile': 'P'}),
        ('tile-b', 'n1', {'band': 'medium', 'image_line_mismatch_m': float('nan'), 'observed_segments': 5, 'profile': 'P'})])
    mm = eyes_report.snapshot(root, persist=False)['mismatch']
    # owned: r1..r5 + z1 + n1 = 7; r5 (None, 0 seg), z1 (0.0, 0 seg) and n1 (NaN) are null-aware nulls.
    assert mm['null']['value'] == 3 and mm['null']['denominator'] == 7
    assert mm['null_v1']['value'] == 1
    assert mm['zero_segments']['value'] == 2
    assert mm['zero_segments_non_null_v1']['value'] == 1
    assert mm['eligible']['value'] == 3  # NaN is not eligible
    # saturated_any_segments: r1, r2, r3, r4 (segments >= 1, finite); z1's 0.0 no longer dilutes the share.
    assert mm['saturated_any_segments']['value'] == 1 and mm['saturated_any_segments']['denominator'] == 4
    assert 'observed_segments>=1' in mm['saturated_any_segments']['population']
    assert mm['quantiles']['max']['value'] == 1.5
    json.dumps(mm, allow_nan=False)


def test_cross_capture_reports_owned_roofs_hidden(tmp_path):
    root = make_root(tmp_path, sidecars=True)
    with eyes_index.connection(root) as db:  # the real writer marks every row hidden_by='other'
        eyes_index.replace_rows(db, 'cross_capture', [
            {'scene_id': 'tile-a', 'object_id': 'r1', 'other_scene_id': 'house-1', 'other_object_id': 'h9', 'version': 'v',
             'payload': {'iou': .7, 'band': 'low', 'other_band': 'high', 'band_disagreement': True, 'hidden_by': 'other'}}])
    cc = eyes_report.snapshot(root, persist=False)['cross_capture']
    assert cc['hidden']['value'] == 2  # pairs with a hidden side
    assert cc['hidden_roofs']['value'] == 1  # r1 hidden twice counts once
    assert cc['owned_roofs_hidden'] == {'value': 1, 'denominator': 5, 'population': 'owned scan roofs'}
    assert cc['hidden_low_under_higher']['value'] == 1 and cc['hidden_low_under_higher']['denominator'] == 3
    assert cc['versions'] == ['v']


def test_stale_signature_sidecar_rows_do_not_count_as_coverage(tmp_path):
    root = make_root(tmp_path)
    _add_roofs(root, [('tile-b', 'r7', {'band': 'medium', 'image_line_mismatch_m': 0.5, 'observed_segments': 4,
                                         'profile': 'P', 'model_signature': 'current'})])
    with eyes_index.connection(root) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'roof_diagnostics', [
            {'scene_id': 'tile-b', 'object_id': 'r7', 'model_signature': 'old', 'version': 'v', 'payload': {'recomputed_matches_stored': True}},
            {'scene_id': 'tile-a', 'object_id': 'r1', 'model_signature': 'm', 'version': 'v', 'payload': {'error': 'boom'}},
            {'scene_id': 'house-1', 'object_id': 'h1', 'model_signature': 'm', 'version': 'v', 'payload': {}}])
        eyes_index.replace_rows(db, 'shadow_bands', [
            {'scene_id': 'tile-b', 'object_id': 'r7', 'model_signature': 'old', 'version': 'v2', 'payload': {'band_v1': 'low', 'band_v2': 'low'}}])
    snap = eyes_report.snapshot(root, persist=False)
    diag = snap['roof_diagnostics']
    assert diag['coverage']['value'] == 1  # r1 attempted (error row); r7 row describes another model output
    assert diag['coverage_without_error']['value'] == 0
    assert diag['stale_signature_rows']['value'] == 1
    assert diag['rows_outside_population']['value'] == 1
    assert snap['shadow_v2']['rows']['value'] == 0 and snap['shadow_v2']['stale_signature_rows']['value'] == 1


def test_reference_tiers_carry_label_roles(tmp_path):
    agree = eyes_report.snapshot(make_root(tmp_path, sidecars=True), persist=False)['reference_agreement']['by_tier']
    assert agree['T2']['label_tier'] is True and 'human' in agree['T2']['role']
    assert agree['T3']['label_tier'] is False and 'never a label' in agree['T3']['role']


def test_load_rejects_non_snapshot_json(tmp_path, capsys):
    root = make_root(tmp_path)
    bogus = tmp_path / 'bogus.json'
    bogus.write_text('[1, 2]')
    assert eyes_report.load(root, str(bogus)) is None
    first = eyes_report.snapshot(root)
    assert eyes_report.main(['--root', str(root), '--diff', str(bogus), first['snapshot_id']]) == 2
    capsys.readouterr()
