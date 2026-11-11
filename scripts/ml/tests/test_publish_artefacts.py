import fcntl
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import urllib.request
from datetime import UTC, datetime

import pytest
from scripts import annotation_snapshot
from scripts.ml import fieldwork_feedback as feedback
from scripts.ml.training import publish_artefacts as publish

LOOPBACK = 'http://127.0.0.1:18999'


def make_repo(root):
    ds = root / 'data/reviews/eyes/training/dataset-v2'
    (ds / 'images').mkdir(parents=True)
    (ds / 'labels').mkdir()
    (ds / 'manifest.json').write_text('{"category": {"name": "building_roof"}}')
    (ds / 'DATASET_CARD.md').write_text('# dataset-v2\n')
    (ds / 'train.json').write_text(json.dumps({'images': [{'id': 1, 'file_name': 'images/house-1.png'}], 'annotations': []}))
    (ds / 'images/house-1.png').write_bytes(b'\x89PNG image bytes ' * 200)
    (ds / 'labels/house-1.png').write_bytes(b'\x89PNG label bytes')
    (ds / 'labels/scratch.json.partial').write_bytes(b'ignored')
    (ds.parent / 'dataset-v2.export.json').write_text('{"n_scenes": 1}')
    for folder in ('edit-diffs', 'line-corrections'):
        day = root / 'data/reviews/eyes' / folder / '2026-09-29'
        day.mkdir(parents=True)
        (day / 'rows.jsonl').write_text('{"scene_id": "house-1", "iou": 0.9}\n' * 60)
        (day / 'summary.json').write_text('{"rows": 60}')
    sweep = root / 'data/reviews/eyes/training/sam3-mac/sweep'
    run = sweep / 's1-raw-lr8e-5-seed1'
    (run / 'reference-eval').mkdir(parents=True)
    (sweep / 'results.jsonl').write_text('{"run": "s1-raw-lr8e-5-seed1", "agreement": 0.7}\n')
    (run / 'run.json').write_text('{"args": {"lr": 8e-05}}')
    (run / 'summary.json').write_text('{"steps": 1500}')
    (run / 'steps.jsonl').write_text('{"step": 1}\n' * 100)
    (run / 'trainable-final.pt').write_bytes(b'\x00' * 4096)
    (run / 'reference-eval/summary.json').write_text('{"agreement": 0.7}')
    (sweep / 's1-raw-lr8e-5-seed1.train.log').write_text('log\n')
    coco = root / 'data/reviews/eyes/training/sam3-mac/coco-v1'
    (coco / 'train/images').mkdir(parents=True)
    (coco / 'manifest.json').write_text('{"version": "sam3-mac-coco-v1"}')
    (coco / 'train/_annotations.coco.json').write_text('{"images": [], "annotations": []}')
    (coco / 'train/sidecar.json').write_text('[]')
    (coco / 'train/images/dataset-v2--house-1.png').write_bytes(b'\x89PNG copy')
    eyes = root / 'data/reviews/eyes/eyes.sqlite'
    with sqlite3.connect(eyes) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE "references" (scene_id TEXT, tier TEXT)')
        db.commit()
    return root


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = make_repo(tmp_path / 'repo')
    monkeypatch.setattr(publish, 'ROOT', root)
    monkeypatch.setattr(feedback, 'ROOT', root)
    for key in ('ANNOTATION_URL', 'ANNOTATION_TOKEN', 'FIELDWORK_TOKEN_FILE', 'FIELDWORK_PUSH_ALLOW_LIVE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(publish, '_git_head', lambda root: 'a' * 40)
    return root


def test_group_selection_excludes_images_logs_and_checkpoints_unless_asked(repo):
    planned = publish.plan(repo, ['dataset-v2', 'edit-diffs/2026-09-29', 'line-corrections/2026-09-29', 'sweep', 'coco-v1', 'eyes'])
    groups = planned['groups']
    dataset = publish.resolve_group(repo, 'dataset-v2')
    assert dataset['files'] == ['data/reviews/eyes/training/dataset-v2.export.json', 'data/reviews/eyes/training/dataset-v2/DATASET_CARD.md',
                                'data/reviews/eyes/training/dataset-v2/labels/house-1.png', 'data/reviews/eyes/training/dataset-v2/manifest.json',
                                'data/reviews/eyes/training/dataset-v2/train.json']
    assert groups['dataset-v2']['excluded'][0]['path'].endswith('dataset-v2/images') and groups['dataset-v2']['excluded'][0]['files'] == 1
    assert 'data/reviews/eyes/training/dataset-v2/images/house-1.png' in publish.resolve_group(repo, 'dataset-v2', with_images=True)['files']
    assert publish.resolve_group(repo, 'dataset-v2', tarball=True)['excluded'][-1]['reason'].startswith('tarball not present')
    sweep = publish.resolve_group(repo, 'sweep')
    assert sweep['files'] == ['data/reviews/eyes/training/sam3-mac/sweep/results.jsonl',
                              'data/reviews/eyes/training/sam3-mac/sweep/s1-raw-lr8e-5-seed1/reference-eval/summary.json',
                              'data/reviews/eyes/training/sam3-mac/sweep/s1-raw-lr8e-5-seed1/run.json',
                              'data/reviews/eyes/training/sam3-mac/sweep/s1-raw-lr8e-5-seed1/summary.json']
    assert {e['reason'].split(';')[0] for e in sweep['excluded']} == {'checkpoints omitted', 'training logs stay local'}
    assert any(p.endswith('trainable-final.pt') for p in publish.resolve_group(repo, 'sweep', include_checkpoints=True)['files'])
    coco = publish.resolve_group(repo, 'coco-v1')
    assert [p.rsplit('/', 1)[1] for p in coco['files']] == ['manifest.json', '_annotations.coco.json', 'sidecar.json']
    assert coco['excluded'][0]['files'] == 1
    assert groups['edit-diffs/2026-09-29']['files'] == 2 and groups['line-corrections/2026-09-29']['files'] == 2
    assert groups['eyes']['method'].startswith('VACUUM INTO') and groups['eyes']['files'] == 1
    assert planned['totals']['files'] == 5 + 2 + 2 + 4 + 3 + 1
    with pytest.raises(feedback.FeedbackError, match='Unknown artefact group'):
        publish.resolve_group(repo, '../../etc')
    with pytest.raises(feedback.FeedbackError, match='not a directory'):
        publish.resolve_group(repo, 'dataset-v9')


def test_eyes_is_staged_with_vacuum_into_including_uncheckpointed_wal(repo, monkeypatch):
    eyes = repo / 'data/reviews/eyes/eyes.sqlite'
    keeper = sqlite3.connect(eyes)
    keeper.execute("INSERT INTO \"references\" VALUES ('house-1', 'T1')")
    keeper.commit()
    assert (repo / 'data/reviews/eyes/eyes.sqlite-wal').stat().st_size > 0
    for name in ('copy', 'copy2', 'copyfile'):
        monkeypatch.setattr(shutil, name, lambda *a, **k: pytest.fail('raw copy of a live database'))
    try:
        staged = publish.stage(repo, ['eyes'], repo / '.run/staging')
    finally:
        keeper.close()
    manifest = staged['manifest']
    assert [f['path'] for f in manifest['files']] == ['data/reviews/eyes/eyes.sqlite']
    assert manifest['summary']['databases']['data/reviews/eyes/eyes.sqlite']['method'] == 'VACUUM INTO'
    blob = repo / '.run/staging/blobs' / manifest['files'][0]['chunks'][0]['sha256']
    with sqlite3.connect(blob) as copy:
        assert copy.execute('SELECT scene_id, tier FROM "references"').fetchall() == [('house-1', 'T1')]
        assert copy.execute('PRAGMA journal_mode').fetchone()[0] != 'wal'
    assert eyes.stat().st_size > 0 and not list((repo / '.run/staging').glob('**/*-wal'))


def test_stage_chunks_large_files_dedupes_paths_and_writes_valid_item(repo, monkeypatch):
    monkeypatch.setattr(annotation_snapshot, 'CHUNK_BYTES', 1024)
    now = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    staged = publish.stage(repo, ['dataset-v2', 'edit-diffs/2026-09-29', 'dataset-v2'], repo / '.run/staging', with_images=True,
                           parent='b' * 64, now=now)
    manifest, item = staged['manifest'], staged['item']
    by_path = {f['path']: f for f in manifest['files']}
    image = by_path['data/reviews/eyes/training/dataset-v2/images/house-1.png']
    raw = (repo / image['path']).read_bytes()
    assert [c['bytes'] for c in image['chunks']] == [1024, 1024, 1024, len(raw) - 3072]
    assert image['sha256'] == hashlib.sha256(raw).hexdigest() and image['bytes'] == len(raw)
    assert b''.join((repo / '.run/staging/blobs' / c['sha256']).read_bytes() for c in image['chunks']) == raw
    rows = by_path['data/reviews/eyes/edit-diffs/2026-09-29/rows.jsonl']
    assert len(rows['chunks']) == 3 and rows['contentType'] == 'application/octet-stream'
    assert [f['path'] for f in manifest['files']] == sorted(by_path) and len(manifest['files']) == 8
    assert manifest['schemaVersion'] == 1 and manifest['sceneIds'] == [] and re.fullmatch(r'[0-9a-f]{64}', manifest['snapshotId'])
    stable = {'schemaVersion': 1, 'sceneIds': [], 'files': manifest['files']}
    assert manifest['snapshotId'] == hashlib.sha256(annotation_snapshot.encoded(stable)).hexdigest()
    assert manifest['kind'] == 'addr-training-manifest' and manifest['parents'] == ['b' * 64]
    assert manifest['groups']['dataset-v2']['paths'] == ['data/reviews/eyes/training/dataset-v2']
    assert set(manifest['groups']['edit-diffs/2026-09-29']['files']) == {rows['path'], 'data/reviews/eyes/edit-diffs/2026-09-29/summary.json'}
    assert re.fullmatch(r'[0-9a-f]{64}', manifest['codeSha256']) and manifest['gitHead'] == 'a' * 40
    manifest_bytes = (repo / '.run/staging/manifest.json').read_bytes()
    assert json.loads(manifest_bytes) == manifest
    sha = hashlib.sha256(manifest_bytes).hexdigest()
    assert staged['manifestSha256'] == sha
    assert item['id'] == f'training-manifest-20260929-{sha[:12]}' and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', item['id'])
    assert item['adapter'] == 'addr-report' and item['annotations'] == [] and item['status'] == 'unreviewed' and 'crop' not in item
    assert item['source']['manifest'] == {'url': f'/api/v1/assets/{sha}', 'sha256': sha, 'bytes': len(manifest_bytes), 'contentType': 'application/json'}
    assert item['source']['kind'] == 'training-manifest' and item['source']['groups'] == {'dataset-v2': ['data/reviews/eyes/training/dataset-v2'],
                                                                                          'edit-diffs/2026-09-29': ['data/reviews/eyes/edit-diffs/2026-09-29']}
    assert item['metadata']['addr']['counts'] == {'files': 8, 'bytes': sum(f['bytes'] for f in manifest['files']), 'scenes': 0}
    assert item['tags'] == ['training-manifest', 'dataset-v2', 'edit-diffs/2026-09-29'] and 1 <= len(item['title']) <= 300
    assert 'human' not in json.dumps(item).lower().replace('not human labels', '')
    assert json.loads((repo / '.run/staging/item.json').read_bytes()) == item
    assert json.loads((repo / '.run/staging/project.json').read_bytes())['id'] == 'addr-training'


def test_dry_run_reads_local_files_only(repo, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('dry run must not start a process'))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('dry run must not use the network'))
    result = publish.publish(['dataset-v2', 'sweep'], dry_run=True, url=LOOPBACK)
    assert result['status'] == 'dry_run' and result['origin'] == LOOPBACK
    assert result['groups']['dataset-v2']['files'] == 5 and result['groups']['sweep']['files'] == 4
    assert result['totals']['bytes'] == result['groups']['dataset-v2']['bytes'] + result['groups']['sweep']['bytes']
    assert not (repo / '.run').exists() and not (repo / 'data/fieldwork-sync').exists()
    result = publish.publish(['dataset-v2'], dry_run=True)
    assert result['origin'] == feedback.DEFAULT_REMOTE_URL


def test_apply_refuses_live_origin_and_implicit_token_for_custom_origins(repo, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('guard must fire before any process'))
    with pytest.raises(feedback.FeedbackError, match='FIELDWORK_PUSH_ALLOW_LIVE=1'):
        publish.publish(['dataset-v2'], dry_run=False)
    with pytest.raises(feedback.FeedbackError, match='custom Fieldwork URL requires'):
        publish.publish(['dataset-v2'], dry_run=False, url='https://other.example')
    assert publish.token_flags(LOOPBACK) == []
    assert publish.token_flags(feedback.DEFAULT_REMOTE_URL) == ['--token-file', str(repo / feedback.DEFAULT_TOKEN_FILE)]
    monkeypatch.setenv('ANNOTATION_TOKEN', 'env-token')
    assert publish.token_flags('https://other.example') == []


@pytest.mark.parametrize('origin', ['https://fieldwork-annotations.ben-ebsworth.workers.dev:443',
                                    'https://FIELDWORK-ANNOTATIONS.ben-ebsworth.workers.dev/',
                                    'https://fieldwork-annotations.ben-ebsworth.workers.dev:8443'])
def test_live_origin_spellings_are_refused_before_any_process(repo, monkeypatch, origin):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('guard must fire before any process'))
    with pytest.raises(feedback.FeedbackError, match='FIELDWORK_PUSH_ALLOW_LIVE=1'):
        publish.publish(['dataset-v2'], dry_run=False, url=origin, token_file=feedback.DEFAULT_TOKEN_FILE)
    with pytest.raises(feedback.FeedbackError, match='FIELDWORK_PUSH_ALLOW_LIVE=1'):
        publish.guard_live_origin(origin, dry_run=False)
    assert publish.guard_live_origin(origin, dry_run=True) is None
    assert publish.token_flags(origin) == ['--token-file', str(repo / feedback.DEFAULT_TOKEN_FILE)]


def fake_runners(monkeypatch, *, project_exists=False, save_item=None):
    calls = []

    def run(command, flags):
        calls.append((command, list(flags)))
        if command == 'projects':
            return {'projects': [{'id': 'addr-training'}] if project_exists else [{'id': 'mooroolbark'}], 'nextCursor': ''}
        if command == 'save-project':
            return {'project': {'id': 'addr-training', 'revision': 1}}
        if command == 'save-item':
            if isinstance(save_item, Exception):
                raise save_item
            document = json.loads(open(flags[flags.index('--file') + 1]).read())
            return {'item': {**document, 'revision': 1}}
        if command == 'items':
            return save_item() if callable(save_item) else {'items': [], 'nextCursor': ''}
        raise AssertionError(command)

    def migrate(flags):
        calls.append(('annotation-migrate', list(flags)))
        bundle = flags[flags.index('--bundle') + 1]
        assert (publish.Path(bundle) / 'manifest.json').is_file() and (publish.Path(bundle) / 'blobs').is_dir()
        return {'snapshotId': 'x', 'uploadedAssets': 7, 'existingAssets': 1, 'uploadedBytes': 1234, 'existingBytes': 10}

    monkeypatch.setattr(publish, '_run', run)
    monkeypatch.setattr(publish, '_run_migrate', migrate)
    return calls


def test_apply_creates_project_uploads_then_saves_item_and_receipt_without_exposing_token(repo, monkeypatch):
    token = repo / 'secrets/token.txt'
    token.parent.mkdir()
    token.write_text('secret-bearer-value\n')
    calls = fake_runners(monkeypatch)
    result = publish.publish(['dataset-v2', 'edit-diffs/2026-09-29'], dry_run=False, url=LOOPBACK + '/', token_file=str(token))
    assert [c[0] for c in calls] == ['projects', 'save-project', 'annotation-migrate', 'save-item']
    remote = ['--url', LOOPBACK, '--token-file', str(token)]
    assert calls[0][1] == ['--limit', '500', *remote]
    staging = repo / '.run/training-bundles' / result['manifestSha256'][:12]
    assert calls[1][1] == ['--file', str(staging / 'project.json'), '--revision', '0', *remote]
    assert calls[2][1] == ['upload', '--bundle', str(staging), *remote]
    assert calls[3][1] == ['--file', str(staging / 'item.json'), '--revision', '0', *remote]
    assert result['status'] == 'published' and result['project'] == {'created': True, 'projectId': 'addr-training'}
    assert result['upload'] == {'uploadedAssets': 7, 'existingAssets': 1, 'uploadedBytes': 1234, 'existingBytes': 10}
    assert result['itemId'].startswith('training-manifest-') and result['revision'] == 1 and result['files'] == 7
    receipt = json.loads(open(result['receipt']).read())
    assert result['receipt'] == str(repo / 'data/fieldwork-sync/addr-training/publish' / (result['manifestSha256'][:12] + '.json'))
    assert receipt['itemId'] == result['itemId'] and receipt['manifestSha256'] == result['manifestSha256'] and receipt['uploadedBytes'] == 1234
    assert receipt['groups'] == ['dataset-v2', 'edit-diffs/2026-09-29'] and receipt['origin'] == LOOPBACK
    assert 'secret-bearer-value' not in json.dumps(result) + json.dumps(receipt) + json.dumps(calls)
    assert not list((repo / '.run/training-bundles').glob('.staging-*'))


def test_apply_409_is_idempotent_only_when_the_remote_item_references_the_same_manifest(repo, monkeypatch):
    conflict = feedback.FeedbackError('Fieldwork save-item failed (exit 1): annotate: HTTP 409: aborted: revision conflict')
    holder = {}

    def items():
        return {'items': [{'id': 'training-manifest-x', 'source': {'manifest': {'sha256': holder['sha']}}, 'revision': 1}], 'nextCursor': ''}

    calls = fake_runners(monkeypatch, project_exists=True, save_item=conflict)
    monkeypatch.setattr(publish, '_find_existing', lambda project, sha, flags: holder.__setitem__('sha', sha) or items()['items'][0])
    result = publish.publish(['sweep'], dry_run=False, url=LOOPBACK)
    assert result['status'] == 'already_published' and result['revision'] == 1 and [c[0] for c in calls] == ['projects', 'annotation-migrate', 'save-item']
    assert holder['sha'] == result['manifestSha256']
    fake_runners(monkeypatch, project_exists=True, save_item=conflict)
    monkeypatch.setattr(publish, '_find_existing', lambda project, sha, flags: None)
    with pytest.raises(feedback.FeedbackError, match='different content; nothing overwritten'):
        publish.publish(['sweep'], dry_run=False, url=LOOPBACK)


def test_live_origin_allowed_only_with_explicit_override(repo, monkeypatch):
    monkeypatch.setenv('FIELDWORK_PUSH_ALLOW_LIVE', '1')
    (repo / feedback.DEFAULT_TOKEN_FILE).parent.mkdir(parents=True)
    (repo / feedback.DEFAULT_TOKEN_FILE).write_text('live-token')
    calls = fake_runners(monkeypatch, project_exists=True)
    result = publish.publish(['sweep'], dry_run=False)
    assert result['status'] == 'published' and calls[0][1][-2:] == ['--token-file', str(repo / feedback.DEFAULT_TOKEN_FILE)]
    assert 'live-token' not in json.dumps(result)


def test_publish_lock_blocks_a_second_writer(repo, monkeypatch):
    fake_runners(monkeypatch, project_exists=True)
    lock = repo / 'data/fieldwork-sync/addr-training/publish.lock'
    lock.parent.mkdir(parents=True)
    with lock.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(feedback.FeedbackError, match='Another publish is running'):
            publish.publish(['sweep'], dry_run=False, url=LOOPBACK)


def test_training_scenes_prefer_push_receipts_over_the_frozen_mirror(repo):
    sync = repo / 'data/fieldwork-sync/mooroolbark'
    (sync / 'push/scenes').mkdir(parents=True)
    (sync / 'push/runs/20260929T100000Z-abcdef01').mkdir(parents=True)
    (sync / 'mirror.json').write_text(json.dumps({'items': [
        {'id': 'addr-scene-house-1', 'adapter': 'addr-scene', 'revision': 1, 'source': {'sceneId': 'house-1', 'modelSignature': 'f' * 64}},
        {'id': 'addr-scene-house-2', 'adapter': 'addr-scene', 'revision': 1, 'source': {'sceneId': 'house-2', 'modelSignature': 'e' * 64},
         'metadata': {'addr': {'sourceReviewRevision': 4}}},
        {'id': 'addr-report-mooroolbark', 'adapter': 'addr-report', 'source': {'manifest': {}}}]}))
    (sync / 'push/scenes/house-1.json').write_text(json.dumps({'latestItemId': 'addr-scene-house-1-m0123456789ab-r7',
                                                               'generation': {'modelSignature': '0' * 64, 'reviewRevision': 7}}))
    scenes = publish.training_scenes(repo)
    assert scenes == [{'sceneId': 'house-1', 'itemId': 'addr-scene-house-1-m0123456789ab-r7', 'modelSignature': '0' * 64, 'reviewRevision': 7, 'binding': 'push'},
                      {'sceneId': 'house-2', 'itemId': 'addr-scene-house-2', 'modelSignature': 'e' * 64, 'reviewRevision': 4, 'binding': 'frozen'}]
    staged = publish.stage(repo, ['sweep'], repo / '.run/staging', with_scenes=True)
    assert staged['manifest']['scenes'] == scenes and staged['item']['source']['scenes'] == scenes
    assert staged['item']['metadata']['addr']['pushRunIds'] == ['20260929T100000Z-abcdef01'] and staged['item']['metadata']['addr']['counts']['scenes'] == 2


def test_cli_dry_run_prints_plan_and_apply_errors_go_to_stderr(repo, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('no process in dry run'))
    assert publish.main(['--select', 'dataset-v2', 'eyes', '--url', LOOPBACK]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'dry_run' and set(out['groups']) == {'dataset-v2', 'eyes'}
    assert publish.main(['--select', 'dataset-v2', '--apply']) == 1
    assert 'FIELDWORK_PUSH_ALLOW_LIVE=1' in capsys.readouterr().err
