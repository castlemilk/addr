import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from scripts.ml import eyes_guard

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_FROZEN_RUN = eyes_guard.FROZEN_RUN
REAL_PROTECTED = eyes_guard.PROTECTED_MANIFESTS


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Synthetic frozen plan: its code hashes match the snapshot copies and its run id is its own signature.
PLAN = {'version': 1, 'tiles': [[17, 1, 2]],
        'code': {'scripts/ml/a.py': _sha(b'print(1)\n'), 'scripts/ml/b.py': _sha(b'print(2)\n')}}
PLAN_RUN_ID = _sha(json.dumps(PLAN, sort_keys=True).encode())[:16]


@pytest.fixture(autouse=True)
def synthetic_constants(request, monkeypatch):
    """Point the guard at the synthetic run id and manifests; real-repo tests keep the production constants."""
    if request.node.name.startswith('test_real_repo'):
        return
    monkeypatch.setattr(eyes_guard, 'FROZEN_RUN', REAL_FROZEN_RUN.parent / PLAN_RUN_ID)
    monkeypatch.setattr(eyes_guard, 'PROTECTED_MANIFESTS', ('m-full',))


def make_root(tmp_path, *, manifest_files=True):
    """Synthetic protected-reference layout: one scene, two manifests, frozen run, CRM db, confidence index."""
    root = tmp_path
    scene = root / 'data/scenes/house-1'
    scene.mkdir(parents=True)
    png = b'not-really-a-png-1'
    image_sha = _sha(png)
    (scene / f'{image_sha}.png').write_bytes(png)
    latest = json.dumps({'id': 'house-1', 'model_signature': 'sig-1', 'image_sha256': image_sha}).encode()
    (scene / 'latest.json').write_bytes(latest)
    review = json.dumps({'status': 'confirmed'}).encode()
    (scene / 'review.json').write_bytes(review)

    manifests = root / 'data/references/roof-checkpoints'
    full = {'schema_version': 1, 'scene_id': 'house-1', 'model_signature': 'sig-1',
            'files': {'scripts/ml/fake.py': 'deadbeef'}}
    if manifest_files:
        full['files'].update({
            'data/scenes/house-1/latest.json': _sha(latest),
            f'data/scenes/house-1/{image_sha}.png': image_sha,
            'data/scenes/house-1/review.json': _sha(review),
        })
    (manifests / 'm-full').mkdir(parents=True)
    (manifests / 'm-full/manifest.json').write_text(json.dumps(full))
    bare = {'schema_version': 1, 'scene_id': 'house-1', 'model_signature': 'sig-1',
            'files': {'scripts/ml/fake.py': 'deadbeef'}}
    (manifests / 'm-bare').mkdir()
    (manifests / 'm-bare/manifest.json').write_text(json.dumps(bare))

    run = root / eyes_guard.FROZEN_RUN
    (run / 'snapshot/scripts/ml').mkdir(parents=True)
    (run / 'plan.json').write_text(json.dumps(PLAN))
    (run / 'run.json').write_text(json.dumps({'status': 'complete', 'results': {'house-1': {'confidence': {'low': 1}}}}))
    (run / 'snapshot/scripts/ml/a.py').write_text('print(1)\n')
    (run / 'snapshot/scripts/ml/b.py').write_text('print(2)\n')

    crm = root / eyes_guard.CRM_DB
    crm.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(crm) as db:
        db.execute('CREATE TABLE crm_features(id INTEGER PRIMARY KEY, address_id INTEGER, kind TEXT, geometry_json TEXT, '
                   'label TEXT, properties TEXT, updated_at TEXT, updated_by TEXT)')
        db.execute("INSERT INTO crm_features VALUES(9, 29790, 'roof', ?, 'l', '{}', 't', 'u')",
                   (json.dumps({'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 0]]]}),))

    conf = root / eyes_guard.CONFIDENCE
    conf.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(conf) as db:
        db.execute('CREATE TABLE scenes(id TEXT PRIMARY KEY, signature TEXT, mtime INTEGER, bounds TEXT)')
        db.execute('CREATE TABLE roofs(scene_id TEXT, object_id TEXT, west REAL, south REAL, east REAL, north REAL, payload TEXT, PRIMARY KEY(scene_id,object_id))')
        db.execute('INSERT INTO scenes VALUES(?,?,?,?)', ('house-1', 'sig-1', (scene / 'latest.json').stat().st_mtime_ns, '[0,0,1,1]'))
    return root


def _by_name(report):
    return {c['name']: c for c in report['checks']}


def test_first_run_passes_and_records_baseline(tmp_path):
    root = make_root(tmp_path)
    report = eyes_guard.check(root)
    assert report['version'] == 'eyes-guard-v1'
    assert report['ok'] is True, report
    assert all({'name', 'ok', 'detail'} <= set(c) for c in report['checks'])
    names = _by_name(report)
    assert names['manifest:m-full']['detail']['mode'] == 'file_hashes'
    assert names['manifest:m-full']['detail']['files'] == {
        'data/scenes/house-1/latest.json': 'match',
        next(k for k in names['manifest:m-full']['detail']['files'] if k.endswith('.png')): 'match',
        'data/scenes/house-1/review.json': 'match'}
    assert names['manifest:m-bare']['detail']['mode'] == 'model_signature'
    assert names['frozen_run']['detail']['mode'] == 'recorded'
    assert names['crm_feature_geometry']['detail']['mode'] == 'recorded'
    assert names['confidence_mtimes']['detail']['scenes'] == 1
    assert names['scenes_newer_than_baseline']['detail']['informational'] is True
    baseline = json.loads((root / eyes_guard.BASELINE).read_text())
    assert set(baseline['frozen_run']) == {'plan.json', 'run.json', 'snapshot/scripts/ml/a.py', 'snapshot/scripts/ml/b.py'}
    assert len(baseline['crm_feature_geometry_sha256']) == 64
    assert set(baseline['manifests']) == {'m-full', 'm-bare'}
    assert names['frozen_run']['detail']['plan_signature_matches_run_id'] is True
    assert baseline['version'] == 'eyes-guard-v1' and 'created_at' in baseline and 'created_at_ns' in baseline
    assert len(report['code_sha256']) == 64


def test_manifest_review_drift_fails(tmp_path):
    root = make_root(tmp_path)
    eyes_guard.check(root)
    (root / 'data/scenes/house-1/review.json').write_text(json.dumps({'status': 'rejected'}))
    report = eyes_guard.check(root)
    assert report['ok'] is False
    detail = _by_name(report)['manifest:m-full']['detail']
    assert detail['files']['data/scenes/house-1/review.json'] == 'drift'
    assert _by_name(report)['manifest:m-full']['ok'] is False


def test_manifest_missing_scene_file_fails(tmp_path):
    root = make_root(tmp_path)
    eyes_guard.check(root)
    os.remove(root / 'data/scenes/house-1/review.json')
    report = eyes_guard.check(root)
    assert _by_name(report)['manifest:m-full']['detail']['files']['data/scenes/house-1/review.json'] == 'missing'
    assert report['ok'] is False


def test_manifest_without_file_hashes_falls_back_to_model_signature(tmp_path):
    root = make_root(tmp_path, manifest_files=False)
    report = eyes_guard.check(root)
    detail = _by_name(report)['manifest:m-full']['detail']
    assert detail['mode'] == 'model_signature' and detail['model_signature'] == 'sig-1'
    assert report['ok'] is True
    latest = root / 'data/scenes/house-1/latest.json'
    stamp = latest.stat().st_mtime_ns
    latest.write_text(json.dumps({'id': 'house-1', 'model_signature': 'sig-2', 'image_sha256': 'x'}))
    os.utime(latest, ns=(stamp, stamp))
    report = eyes_guard.check(root)
    assert _by_name(report)['manifest:m-full']['ok'] is False
    assert _by_name(report)['manifest:m-full']['detail']['latest_model_signature'] == 'sig-2'


def test_frozen_run_drift_added_and_changed_files(tmp_path):
    root = make_root(tmp_path)
    eyes_guard.check(root)
    (root / eyes_guard.FROZEN_RUN / 'snapshot/scripts/ml/a.py').write_text('print(3)\n')
    (root / eyes_guard.FROZEN_RUN / 'snapshot/scripts/ml/c.py').write_text('new\n')
    report = eyes_guard.check(root)
    detail = _by_name(report)['frozen_run']['detail']
    assert detail['mode'] == 'compared'
    assert detail['changed'] == ['snapshot/scripts/ml/a.py']
    assert detail['added'] == ['snapshot/scripts/ml/c.py']
    assert detail['removed'] == []
    assert report['ok'] is False
    # Baseline is never rewritten after the first run.
    baseline = json.loads((root / eyes_guard.BASELINE).read_text())
    assert 'snapshot/scripts/ml/c.py' not in baseline['frozen_run']


def test_frozen_run_missing_file_fails(tmp_path):
    root = make_root(tmp_path)
    eyes_guard.check(root)
    os.remove(root / eyes_guard.FROZEN_RUN / 'run.json')
    report = eyes_guard.check(root)
    detail = _by_name(report)['frozen_run']['detail']
    assert 'run.json' in detail['removed'] and report['ok'] is False


def test_crm_geometry_drift_and_missing_db(tmp_path):
    root = make_root(tmp_path)
    eyes_guard.check(root)
    with sqlite3.connect(root / eyes_guard.CRM_DB) as db:
        db.execute("UPDATE crm_features SET geometry_json=? WHERE id=9", ('{"type":"Point","coordinates":[0,0]}',))
    report = eyes_guard.check(root)
    check = _by_name(report)['crm_feature_geometry']
    assert check['ok'] is False and check['detail']['mode'] == 'compared'
    assert check['detail']['baseline_sha256'] != check['detail']['sha256']
    os.remove(root / eyes_guard.CRM_DB)
    report = eyes_guard.check(root)
    assert _by_name(report)['crm_feature_geometry']['detail']['mode'] == 'missing'
    assert report['ok'] is False


def test_crm_check_opens_database_read_only(tmp_path):
    root = make_root(tmp_path)
    before = (root / eyes_guard.CRM_DB).read_bytes()
    eyes_guard.check(root)
    assert (root / eyes_guard.CRM_DB).read_bytes() == before
    assert not (root / eyes_guard.CRM_DB).with_name('australia_geo.db-wal').exists()


def test_confidence_mtime_mismatch_fails(tmp_path):
    root = make_root(tmp_path)
    eyes_guard.check(root)
    latest = root / 'data/scenes/house-1/latest.json'
    stamp = latest.stat().st_mtime_ns + 5_000_000_000
    os.utime(latest, ns=(stamp, stamp))
    report = eyes_guard.check(root)
    check = _by_name(report)['confidence_mtimes']
    assert check['ok'] is False and check['detail']['mismatched'] == ['house-1']
    assert report['ok'] is False


def test_confidence_missing_scene_fails(tmp_path):
    root = make_root(tmp_path)
    with sqlite3.connect(root / eyes_guard.CONFIDENCE) as db:
        db.execute("INSERT INTO scenes VALUES('ghost','s',1,'[0,0,1,1]')")
    report = eyes_guard.check(root)
    check = _by_name(report)['confidence_mtimes']
    assert check['detail']['missing'] == ['ghost'] and check['ok'] is False


def test_scenes_newer_than_baseline_is_informational(tmp_path):
    root = make_root(tmp_path)
    first = eyes_guard.check(root)
    assert _by_name(first)['scenes_newer_than_baseline']['detail']['count'] == 0
    time.sleep(0.01)
    (root / 'data/scenes/house-2').mkdir()
    (root / 'data/scenes/house-2/latest.json').write_text('{}')
    report = eyes_guard.check(root)
    check = _by_name(report)['scenes_newer_than_baseline']
    assert check['ok'] is True and check['detail']['count'] >= 1
    # house-1 png + latest.json + review.json, plus house-2/latest.json
    assert check['detail']['files'] == 4 and report['ok'] is True


def test_write_stores_report_and_main_exit_codes(tmp_path, capsys):
    root = make_root(tmp_path)
    report = eyes_guard.write(root, 'cp1-start')
    stored = root / 'data/reviews/eyes/guard-cp1-start.json'
    assert stored.exists() and json.loads(stored.read_text())['ok'] is True
    assert report['path'] == str(stored)
    with pytest.raises(ValueError):
        eyes_guard.write(root, '../escape')
    assert eyes_guard.main(['--root', str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is True and out['version'] == 'eyes-guard-v1'
    (root / 'data/scenes/house-1/review.json').write_text('{"status":"rejected"}')
    assert eyes_guard.main(['--root', str(root), '--write', 'cp1-end']) == 1
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is False and (root / 'data/reviews/eyes/guard-cp1-end.json').exists()


def test_check_without_baseline_write_leaves_tree_untouched(tmp_path):
    root = make_root(tmp_path)
    report = eyes_guard.check(root, write_baseline=False)
    assert report['ok'] is True
    assert not (root / eyes_guard.BASELINE).exists()
    assert _by_name(report)['frozen_run']['detail']['mode'] == 'unrecorded'


@pytest.mark.skipif(not (REPO_ROOT / eyes_guard.MANIFESTS).exists() or not (REPO_ROOT / eyes_guard.CONFIDENCE).exists(),
                    reason='real repo data not present')
def test_real_repo_protected_references_read_only():
    started = time.perf_counter()
    report = eyes_guard.check(REPO_ROOT, write_baseline=False)
    assert time.perf_counter() - started < 5
    names = _by_name(report)
    manifests = {n for n in names if n.startswith('manifest:')}
    assert manifests >= {'manifest:a983ad7a89b19808', 'manifest:e055d6fd61e63b10',
                         'manifest:3322869b48a69d51', 'manifest:d4f3d42547234b76'}
    for name in manifests:
        assert names[name]['ok'] is True, names[name]
        assert names[name]['detail']['mode'] == 'file_hashes'
    assert names['confidence_mtimes']['ok'] is True, names['confidence_mtimes']['detail']
    assert names['crm_feature_geometry']['detail']['mode'] in {'recorded', 'compared', 'unrecorded'}
    assert names['frozen_run']['detail']['count'] >= 100
    # Baseline-independent: plan.json is content-addressed by the run id and its code hashes match snapshot/.
    assert names['frozen_run']['detail']['plan_signature_matches_run_id'] is True
    assert names['frozen_run']['detail']['snapshot_code_mismatch'] == []
    assert names['frozen_run']['detail']['plan_code_files'] >= 70


# --- adversarial-review regressions ---------------------------------------------------------------


def test_deleted_protected_manifest_fails(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(eyes_guard, 'PROTECTED_MANIFESTS', ('m-full', 'm-gone'))
    report = eyes_guard.check(root)
    check = _by_name(report)['manifest:m-gone']
    assert check['ok'] is False and check['detail']['mode'] == 'missing' and check['detail']['protected'] is True
    assert report['ok'] is False


def test_manifest_removed_after_baseline_fails(tmp_path):
    root = make_root(tmp_path)
    eyes_guard.check(root)
    os.remove(root / 'data/references/roof-checkpoints/m-bare/manifest.json')
    report = eyes_guard.check(root)
    check = _by_name(report)['manifest:m-bare']
    assert check['ok'] is False and check['detail']['mode'] == 'missing' and check['detail']['recorded'] is True


def test_rewritten_manifest_fails_against_baseline(tmp_path):
    """A manifest re-pointed at drifted files must not pass: manifest.json itself is pinned by the baseline."""
    root = make_root(tmp_path)
    first = eyes_guard.check(root)
    assert _by_name(first)['manifest:m-full']['detail']['manifest_baseline'] == 'recorded'
    review = root / 'data/scenes/house-1/review.json'
    review.write_text(json.dumps({'status': 'rejected'}))
    path = root / 'data/references/roof-checkpoints/m-full/manifest.json'
    manifest = json.loads(path.read_text())
    manifest['files']['data/scenes/house-1/review.json'] = _sha(review.read_bytes())
    path.write_text(json.dumps(manifest))
    report = eyes_guard.check(root)
    check = _by_name(report)['manifest:m-full']
    assert check['detail']['files']['data/scenes/house-1/review.json'] == 'match'
    assert check['detail']['manifest_baseline'] == 'drift' and check['ok'] is False
    assert report['ok'] is False


def test_corrupt_baseline_fails_and_is_not_overwritten(tmp_path):
    root = make_root(tmp_path)
    (root / eyes_guard.BASELINE).parent.mkdir(parents=True, exist_ok=True)
    (root / eyes_guard.BASELINE).write_text('{not json')
    report = eyes_guard.check(root)
    check = _by_name(report)['guard_baseline']
    assert check['ok'] is False and check['detail']['mode'] == 'unreadable'
    assert report['ok'] is False and report['baseline']['created'] is False
    assert (root / eyes_guard.BASELINE).read_text() == '{not json'


def test_incomplete_baseline_fails(tmp_path):
    root = make_root(tmp_path)
    eyes_guard.check(root)
    baseline = json.loads((root / eyes_guard.BASELINE).read_text())
    del baseline['frozen_run']
    (root / eyes_guard.BASELINE).write_text(json.dumps(baseline))
    report = eyes_guard.check(root)
    check = _by_name(report)['guard_baseline']
    assert check['ok'] is False and check['detail']['mode'] == 'incomplete' and check['detail']['missing'] == ['frozen_run']
    assert report['ok'] is False


def test_unreadable_or_empty_confidence_index_is_a_failed_check(tmp_path):
    root = make_root(tmp_path)
    with sqlite3.connect(root / eyes_guard.CONFIDENCE) as db:
        db.execute('DELETE FROM scenes')
    check = _by_name(eyes_guard.check(root, write_baseline=False))['confidence_mtimes']
    assert check['ok'] is False and check['detail']['mode'] == 'empty'
    with sqlite3.connect(root / eyes_guard.CONFIDENCE) as db:
        db.execute('DROP TABLE scenes')
    report = eyes_guard.check(root, write_baseline=False)  # must not raise
    check = _by_name(report)['confidence_mtimes']
    assert check['ok'] is False and check['detail']['mode'] == 'error' and report['ok'] is False


def test_frozen_plan_is_verified_without_a_baseline(tmp_path):
    root = make_root(tmp_path)
    run = root / eyes_guard.FROZEN_RUN
    (run / 'snapshot/scripts/ml/b.py').write_text('print(22)\n')
    check = _by_name(eyes_guard.check(root, write_baseline=False))['frozen_run']
    assert check['detail']['mode'] == 'unrecorded'
    assert check['detail']['snapshot_code_mismatch'] == ['scripts/ml/b.py'] and check['ok'] is False
    (run / 'snapshot/scripts/ml/b.py').write_text('print(2)\n')
    (run / 'plan.json').write_text(json.dumps({**PLAN, 'tiles': [[17, 9, 9]]}))
    check = _by_name(eyes_guard.check(root, write_baseline=False))['frozen_run']
    assert check['detail']['plan_signature_matches_run_id'] is False and check['ok'] is False


def test_first_run_without_snapshot_files_records_no_baseline(tmp_path):
    root = make_root(tmp_path)
    run = root / eyes_guard.FROZEN_RUN
    for path in (run / 'snapshot/scripts/ml').iterdir():
        path.unlink()
    report = eyes_guard.check(root)
    check = _by_name(report)['frozen_run']
    assert check['ok'] is False and 'snapshot/' in check['detail']['missing']
    assert report['baseline']['created'] is False and not (root / eyes_guard.BASELINE).exists()


def test_write_honours_no_baseline(tmp_path, capsys):
    root = make_root(tmp_path)
    assert eyes_guard.main(['--root', str(root), '--write', 'dry', '--no-baseline']) == 0
    capsys.readouterr()
    assert (root / 'data/reviews/eyes/guard-dry.json').exists()
    assert not (root / eyes_guard.BASELINE).exists()


def test_root_with_uri_metacharacters_opens_sqlite_read_only(tmp_path):
    root = make_root(tmp_path / 'odd?mode=rwc#root%20x')
    report = eyes_guard.check(root, write_baseline=False)
    names = _by_name(report)
    assert names['crm_feature_geometry']['detail']['mode'] == 'unrecorded', names['crm_feature_geometry']
    assert names['confidence_mtimes']['ok'] is True, names['confidence_mtimes']
    assert report['ok'] is True
