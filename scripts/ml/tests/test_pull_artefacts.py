import gzip
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from scripts import annotation_snapshot
from scripts.ml.tests.test_publish_artefacts import make_repo
from scripts.ml.training import publish_artefacts as publish
from scripts.ml.training import pull_artefacts as pull

TOKEN = 'test-bearer-token'


class FakeFieldwork:
    """Just enough of the Fieldwork API: RPC ListProjects/ListItems/GetItem and content-addressed asset GET."""

    def __init__(self, *, token=None, gzip_assets=False):
        self.items, self.assets, self.log = {}, {}, []
        self.token, self.gzip_assets, self.corrupt = token, gzip_assets, {}
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self):
                if state.token and self.headers.get('Authorization') != f'Bearer {state.token}':
                    self._send(401, json.dumps({'code': 'unauthenticated', 'message': 'bearer required'}).encode(), {'Content-Type': 'application/json'})
                    return False
                return True

            def do_POST(self):
                state.log.append(('POST', self.path))
                if not self._authorised():
                    return
                payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
                method = self.path.rsplit('/', 1)[1]
                if method == 'ListProjects':
                    projects = sorted({i['projectId'] for i in state.items.values()})
                    body = {'projects': [{'id': p} for p in projects], 'nextCursor': ''}
                elif method == 'ListItems':
                    docs = [i for i in state.items.values() if i['projectId'] == payload.get('projectId')
                            and (not payload.get('adapter') or i['adapter'] == payload['adapter'])]
                    body = {'items': sorted(docs, key=lambda i: i['id']), 'nextCursor': ''}
                elif method == 'GetItem':
                    item = state.items.get(payload.get('id'))
                    if not item:
                        return self._send(404, json.dumps({'code': 'not_found', 'message': 'not found'}).encode(), {'Content-Type': 'application/json'})
                    body = {'item': item, 'labels': [], 'labelsNextCursor': ''}
                else:
                    return self._send(404, b'{"message":"unknown method"}', {'Content-Type': 'application/json'})
                self._send(200, json.dumps(body).encode(), {'Content-Type': 'application/json'})

            def do_GET(self):
                state.log.append(('GET', self.path))
                if not self._authorised():
                    return
                sha = self.path.rsplit('/', 1)[1]
                data = state.corrupt.get(sha, state.assets.get(sha))
                if data is None:
                    return self._send(404, json.dumps({'code': 'not_found', 'message': 'asset not found'}).encode(), {'Content-Type': 'application/json'})
                headers = {'Content-Type': 'application/octet-stream', 'X-Content-SHA256': sha}
                if state.gzip_assets and 'gzip' in (self.headers.get('Accept-Encoding') or ''):
                    data, headers['Content-Encoding'] = gzip.compress(data), 'gzip'
                self._send(200, data, headers)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add_bundle(self, staging, *, revision=1):
        staging = Path(staging)
        for blob in (staging / 'blobs').iterdir():
            self.assets[blob.name] = blob.read_bytes()
        manifest = (staging / 'manifest.json').read_bytes()
        self.assets[hashlib.sha256(manifest).hexdigest()] = manifest
        item = json.loads((staging / 'item.json').read_bytes())
        item.update(revision=revision, createdAt=item['metadata']['addr']['publishedAt'], updatedAt=item['metadata']['addr']['publishedAt'])
        self.items[item['id']] = item
        return item

    def add_asset(self, data):
        sha = hashlib.sha256(data).hexdigest()
        self.assets[sha] = data
        return {'url': f'/api/v1/assets/{sha}', 'sha256': sha, 'bytes': len(data), 'contentType': 'application/octet-stream'}

    def gets(self, prefix='/api/v1/assets/'):
        return [path for method, path in self.log if method == 'GET' and path.startswith(prefix)]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = make_repo(tmp_path / 'repo')
    monkeypatch.setattr(publish, 'ROOT', root)
    monkeypatch.setattr(publish, '_git_head', lambda root: 'c' * 40)
    monkeypatch.setattr(pull, 'ROOT', root)
    monkeypatch.setattr(annotation_snapshot, 'CHUNK_BYTES', 1024)
    for key in ('ANNOTATION_URL', 'ANNOTATION_TOKEN', 'FIELDWORK_TOKEN_FILE'):
        monkeypatch.delenv(key, raising=False)
    return root


@pytest.fixture
def server():
    fake = FakeFieldwork()
    yield fake
    fake.close()


def staged_bundle(repo, select, **options):
    staging = repo / '.run/training-bundles/stage'
    return publish.stage(repo, select, staging, now=datetime(2026, 9, 29, 12, tzinfo=UTC), **options), staging


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_list_manifests_reports_published_items_newest_first(repo, server):
    assert pull.list_manifests(server.origin, project='addr-training') == {'origin': server.origin, 'projectId': 'addr-training', 'projectExists': False, 'manifests': []}
    first, staging = staged_bundle(repo, ['sweep'])
    server.add_bundle(staging)
    second = publish.stage(repo, ['edit-diffs/2026-09-29'], repo / '.run/stage2', now=datetime(2026, 9, 30, 12, tzinfo=UTC))
    server.add_bundle(repo / '.run/stage2')
    listed = pull.list_manifests(server.origin, project='addr-training')
    assert listed['projectExists'] and [m['itemId'] for m in listed['manifests']] == [second['item']['id'], first['item']['id']]
    assert listed['manifests'][1] == {'itemId': first['item']['id'], 'revision': 1, 'manifestSha256': first['manifestSha256'],
                                      'publishedAt': '2026-09-29T12:00:00+00:00', 'groups': ['sweep'], 'bytes': first['manifest']['summary']['bytes'],
                                      'files': 4, 'snapshotId': first['manifest']['snapshotId'], 'parents': [], 'codeSha256': first['manifest']['codeSha256'],
                                      'kind': 'training-manifest'}


def test_pull_latest_restores_every_file_with_verified_hashes_and_a_receipt(repo, server, tmp_path):
    staged, staging = staged_bundle(repo, ['dataset-v2', 'edit-diffs/2026-09-29', 'sweep'], with_images=True)
    server.add_bundle(staging)
    out = tmp_path / 'pulled'
    receipt = pull.pull(server.origin, None, out, latest=True)
    manifest = staged['manifest']
    assert receipt['complete'] and receipt['itemId'] == staged['item']['id'] and receipt['manifestSha256'] == staged['manifestSha256']
    assert receipt['groups'] == ['dataset-v2', 'edit-diffs/2026-09-29', 'sweep'] and receipt['codeSha256'] == manifest['codeSha256']
    assert len(receipt['files']) == len(manifest['files']) == 12
    for entry in manifest['files']:
        restored = out / entry['path']
        assert sha_of(restored) == entry['sha256'] == sha_of(repo / entry['path']) and restored.stat().st_size == entry['bytes']
        assert restored.stat().st_mtime_ns == int(entry['mtimeNs'])
    image = next(f for f in manifest['files'] if f['path'].endswith('images/house-1.png'))
    assert len(image['chunks']) == 4 and (out / image['path']).read_bytes() == (repo / image['path']).read_bytes()
    assert (out / 'TRAINING_MANIFEST.json').read_bytes() == (staging / 'manifest.json').read_bytes()
    assert receipt['counts'] == {'blobsDownloaded': sum(len(f['chunks']) for f in manifest['files']), 'blobsReused': 0, 'blobsVerified': 0,
                                 'bytesDownloaded': sum(f['bytes'] for f in manifest['files']), 'filesWritten': 12, 'filesKept': 0}
    assert json.loads((out / '.pull-receipt.json').read_text()) == receipt
    with pytest.raises(pull.PullError, match='already exists'):
        pull.pull(server.origin, None, out, latest=True)


def test_only_selects_groups_by_prefix_and_rejects_unknown_groups(repo, server, tmp_path):
    staged, staging = staged_bundle(repo, ['dataset-v2', 'edit-diffs/2026-09-29', 'line-corrections/2026-09-29'])
    server.add_bundle(staging)
    receipt = pull.pull(server.origin, None, tmp_path / 'pulled', manifest=staged['manifestSha256'], only=['edit-diffs', 'line-corrections/2026-09-29'])
    assert receipt['groups'] == ['edit-diffs/2026-09-29', 'line-corrections/2026-09-29']
    assert sorted(f['path'] for f in receipt['files']) == ['data/reviews/eyes/edit-diffs/2026-09-29/rows.jsonl', 'data/reviews/eyes/edit-diffs/2026-09-29/summary.json',
                                                           'data/reviews/eyes/line-corrections/2026-09-29/rows.jsonl', 'data/reviews/eyes/line-corrections/2026-09-29/summary.json']
    assert not (tmp_path / 'pulled/data/reviews/eyes/training').exists()
    with pytest.raises(pull.PullError, match="group 'coco-v1' is not in this manifest"):
        pull.pull(server.origin, None, tmp_path / 'other', manifest=staged['item']['id'], only=['coco-v1'])
    with pytest.raises(pull.PullError, match='exactly one of'):
        pull.pull(server.origin, None, tmp_path / 'other2')


def test_bad_chunk_hash_aborts_before_any_file_under_out_data_is_written(repo, server, tmp_path):
    staged, staging = staged_bundle(repo, ['dataset-v2', 'sweep'])
    server.add_bundle(staging)
    rows = next(f for f in staged['manifest']['files'] if f['path'].endswith('train.json'))
    server.corrupt[rows['chunks'][0]['sha256']] = b'{"tampered": true}'
    out = tmp_path / 'pulled'
    with pytest.raises(pull.PullError, match='downloaded bytes hash to .*; nothing written'):
        pull.pull(server.origin, None, out, latest=True)
    assert not (out / 'data').exists() and not (out / 'TRAINING_MANIFEST.json').exists() and not (out / '.pull-receipt.json').exists()
    assert all(sha_of(blob) == blob.name for blob in (out / 'blobs').iterdir())
    manifest_ref = staged['item']['source']['manifest']
    server.corrupt.clear()
    server.corrupt[manifest_ref['sha256']] = b'{"schemaVersion": 1}'
    with pytest.raises(pull.PullError, match='downloaded bytes hash to'):
        pull.pull(server.origin, None, tmp_path / 'other', latest=True)
    assert not (tmp_path / 'other/data').exists()


def test_resume_reuses_verified_blobs_keeps_identical_files_and_refuses_changed_ones(repo, server, tmp_path):
    staged, staging = staged_bundle(repo, ['dataset-v2', 'edit-diffs/2026-09-29'])
    server.add_bundle(staging)
    out = tmp_path / 'pulled'
    first = pull.pull(server.origin, None, out, latest=True)
    chunks = sum(len(f['chunks']) for f in staged['manifest']['files'])
    assert first['counts']['blobsDownloaded'] == chunks
    (out / 'data/reviews/eyes/edit-diffs/2026-09-29/summary.json').unlink()
    (out / 'TRAINING_MANIFEST.json').unlink()
    server.log.clear()
    resumed = pull.pull(server.origin, None, out, latest=True, resume=True)
    assert resumed['counts'] == {'blobsDownloaded': 0, 'blobsReused': chunks, 'blobsVerified': 0, 'bytesDownloaded': 0, 'filesWritten': 1, 'filesKept': 6}
    assert server.gets() == [f"/api/v1/assets/{staged['manifestSha256']}"]
    assert (out / 'TRAINING_MANIFEST.json').exists()
    (out / 'data/reviews/eyes/edit-diffs/2026-09-29/summary.json').write_text('{"edited": true}')
    with pytest.raises(pull.PullError, match='exists with different content; refusing to overwrite'):
        pull.pull(server.origin, None, out, latest=True, resume=True)
    assert (out / 'data/reviews/eyes/edit-diffs/2026-09-29/summary.json').read_text() == '{"edited": true}'
    blob = next((out / 'blobs').iterdir())
    blob.write_bytes(b'corrupt blob on disk')
    again = pull.pull(server.origin, None, tmp_path / 'fresh', latest=True)
    assert again['counts']['blobsDownloaded'] == chunks


def test_gzip_encoded_assets_are_decoded_before_verification(repo, tmp_path):
    server = FakeFieldwork(gzip_assets=True)
    try:
        staged, staging = staged_bundle(repo, ['sweep'])
        server.add_bundle(staging)
        receipt = pull.pull(server.origin, None, tmp_path / 'pulled', manifest=staged['item']['id'])
        assert receipt['complete'] and len(receipt['files']) == 4
        assert sha_of(tmp_path / 'pulled/data/reviews/eyes/training/sam3-mac/sweep/results.jsonl') == sha_of(repo / 'data/reviews/eyes/training/sam3-mac/sweep/results.jsonl')
    finally:
        server.close()


def test_token_is_sent_when_required_and_never_stored_in_the_receipt(repo, tmp_path):
    server = FakeFieldwork(token=TOKEN)
    try:
        staged, staging = staged_bundle(repo, ['sweep'])
        server.add_bundle(staging)
        with pytest.raises(pull.PullError, match='HTTP 401'):
            pull.pull(server.origin, None, tmp_path / 'denied', latest=True)
        token_file = repo / 'secrets/token.txt'
        token_file.parent.mkdir()
        token_file.write_text(TOKEN + '\n')
        receipt = pull.pull(server.origin, str(token_file), tmp_path / 'pulled', latest=True)
        assert receipt['complete'] and TOKEN not in json.dumps(receipt) and TOKEN not in (tmp_path / 'pulled/.pull-receipt.json').read_text()
        assert pull.list_manifests(server.origin, str(token_file))['manifests'][0]['itemId'] == staged['item']['id']
    finally:
        server.close()


def scene_item(server, scene_id, *, with_review=True, chunked_image=False):
    model = {'model_signature': '1' * 64, 'image_sha256': None, 'image_width': 512, 'image_height': 512, 'objects': [{'id': 'obj-1', 'kind': 'building'}]}
    image = b'\x89PNG scene image ' * 100
    image_sha = hashlib.sha256(image).hexdigest()
    model['image_sha256'] = image_sha
    model_ref = server.add_asset(json.dumps(model).encode())
    if chunked_image:
        parts = [image[:700], image[700:]]
        image_ref = {'sha256': image_sha, 'bytes': len(image), 'contentType': 'image/png', 'chunks': [server.add_asset(p) for p in parts]}
    else:
        image_ref = server.add_asset(image)
    source = {'sceneId': scene_id, 'image': image_ref, 'model': model_ref, 'modelSignature': model['model_signature'], 'imageSha256': image_sha, 'width': 512, 'height': 512}
    if with_review:
        review = {'revision': 3, 'model_signature': '2' * 64, 'objects': [{'id': 'obj-1', 'status': 'needs_adjustment'}]}
        source['review'] = server.add_asset(json.dumps(review).encode())
        source['reviewModel'] = server.add_asset(json.dumps({**model, 'model_signature': '2' * 64}).encode())
    item = {'id': f'addr-scene-{scene_id}', 'projectId': 'mooroolbark', 'adapter': 'addr-scene', 'revision': 5, 'source': source, 'annotations': [], 'status': 'in_review'}
    server.items[item['id']] = item
    return item, image_sha


def test_with_scenes_requires_eyes_then_materialises_the_scene_layout_export_dataset_expects(repo, server, tmp_path):
    with sqlite3.connect(repo / 'data/reviews/eyes/eyes.sqlite') as db:
        db.execute("INSERT INTO \"references\" VALUES ('house-7', 'T1')")
    item_a, sha_a = scene_item(server, 'house-7')
    item_b, sha_b = scene_item(server, 'house-8', with_review=False, chunked_image=True)
    (repo / 'data/fieldwork-sync/mooroolbark').mkdir(parents=True)
    (repo / 'data/fieldwork-sync/mooroolbark/mirror.json').write_text(json.dumps({'items': [item_a, item_b]}))
    without_eyes, staging = staged_bundle(repo, ['sweep'], with_scenes=True)
    server.add_bundle(staging)
    with pytest.raises(pull.PullError, match="needs the 'eyes' group"):
        pull.pull(server.origin, None, tmp_path / 'no-eyes', manifest=without_eyes['item']['id'], with_scenes=True)
    assert not (tmp_path / 'no-eyes/data').exists()
    with_eyes = publish.stage(repo, ['sweep', 'eyes'], repo / '.run/stage-eyes', with_scenes=True, now=datetime(2026, 9, 29, 13, tzinfo=UTC))
    server.add_bundle(repo / '.run/stage-eyes')
    assert with_eyes['manifest']['scenes'] == [{'sceneId': 'house-7', 'itemId': 'addr-scene-house-7', 'modelSignature': '1' * 64, 'reviewRevision': 0, 'binding': 'frozen'},
                                               {'sceneId': 'house-8', 'itemId': 'addr-scene-house-8', 'modelSignature': '1' * 64, 'reviewRevision': 0, 'binding': 'frozen'}]
    with pytest.raises(pull.PullError, match="needs the 'eyes' group"):
        pull.pull(server.origin, None, tmp_path / 'only-sweep', manifest=with_eyes['item']['id'], with_scenes=True, only=['sweep'])
    out = tmp_path / 'scenes'
    receipt = pull.pull(server.origin, None, out, latest=True, with_scenes=True)
    assert receipt['itemId'] == with_eyes['item']['id']
    scene_a = out / 'data/scenes/house-7'
    assert sorted(p.relative_to(scene_a).as_posix() for p in scene_a.rglob('*') if p.is_file()) == [f'{sha_a}.png', 'latest.json', 'models/' + '2' * 64 + '.json', 'review.json']
    assert json.loads((scene_a / 'latest.json').read_text())['model_signature'] == '1' * 64
    assert json.loads((scene_a / 'models' / ('2' * 64 + '.json')).read_text())['model_signature'] == '2' * 64
    assert json.loads((scene_a / 'review.json').read_text())['revision'] == 3
    scene_b = out / 'data/scenes/house-8'
    assert sorted(p.relative_to(scene_b).as_posix() for p in scene_b.rglob('*') if p.is_file()) == [f'{sha_b}.png', 'latest.json', 'models/' + '1' * 64 + '.json']
    assert hashlib.sha256((scene_b / f'{sha_b}.png').read_bytes()).hexdigest() == sha_b
    with sqlite3.connect(out / 'data/reviews/eyes/eyes.sqlite') as pulled_db:
        assert pulled_db.execute('SELECT scene_id FROM "references"').fetchall() == [('house-7',)]
    assert [s['sceneId'] for s in receipt['scenes']] == ['house-7', 'house-8']
    assert receipt['scenes'][0]['reviewModelSignature'] == '2' * 64 and receipt['scenes'][1]['reviewModelSignature'] is None
    assert receipt['scenes'][0]['revision'] == 5 and receipt['scenes'][0]['itemId'] == 'addr-scene-house-7'


def test_cli_list_and_pull(repo, server, tmp_path, capsys):
    staged, staging = staged_bundle(repo, ['sweep'])
    server.add_bundle(staging)
    assert pull.main(['list', '--url', server.origin]) == 0
    assert json.loads(capsys.readouterr().out)['manifests'][0]['manifestSha256'] == staged['manifestSha256']
    assert pull.main(['pull', '--url', server.origin, '--latest', '--out', str(tmp_path / 'cli'), '--only', 'sweep']) == 0
    assert json.loads(capsys.readouterr().out)['complete'] is True
    assert pull.main(['pull', '--url', server.origin, '--manifest', 'training-manifest-missing', '--out', str(tmp_path / 'cli2')]) == 1
    assert 'HTTP 404' in capsys.readouterr().err
