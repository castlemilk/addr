import hashlib
import json
import sqlite3
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import scripts.ml
from scripts.ml import crm_checkpoint, eyes_index

TOKEN = 'test-token'
META = {'addressId': '29790', 'fullAddress': '27 CAMBRIDGE ROAD, Mooroolbark VIC 3138', 'status': 'quoted',
        'ownerName': 'Jane Roofer', 'ownerEmail': 'jane@example.com', 'ownerPhone': '0400 000 000',
        'notes': 'Call owner Monday\nGate code 1234', 'tags': 'null', 'updatedAt': '2026-09-16T12:34:00Z', 'updatedBy': 'local'}
FEATURES = [{'id': '9', 'addressId': '29790', 'kind': 'FEATURE_KIND_ROOF', 'updatedAt': '2026-09-16T00:00:00Z'},
            {'id': '22', 'addressId': '29790', 'kind': 'FEATURE_KIND_OBJECT', 'updatedAt': '2026-09-17T00:00:00Z'}]
CP = {'id': 'CP1', 'title': 'CP1 27 Cambridge cluster', 'address_id': 29790, 'latitude': -37.79044, 'longitude': 145.31252,
      'address_text': '27 CAMBRIDGE ROAD, Mooroolbark VIC 3138',
      'summary': ('Registry tiers listed; agreement CRM 9 vs 44c724fd3480c167 vs 37ff5c02f77869d5; '
                  'cross-capture marks the hidden low; diagnostics for 104 cluster roofs; shadow flips 3. ' * 2),
      'tags': {'checkpoint': 'CP1', 'band': 'low'},
      'sections': [{'title': 'Reference agreement', 'text': 'Diagnostics, not accuracy.',
                    'table': {'columns': ['object', 'band', 'iou'],
                              'rows': [['44c724fd3480c167', 'low', 0.91], ['37ff5c02f77869d5', 'medium', 0.88]]}},
                   {'title': 'Open questions', 'items': ['Promote band v2?', 'Hidden low in UI']}]}


class CrmMock:
    """Threaded stand-in for the Connect CRM: records requests, overwrites meta columns like the Go store."""

    def __init__(self, meta):
        self.meta, self.features, self.pois = dict(meta), [dict(f) for f in FEATURES], []
        self.requests, self.unknown = [], []
        self.principal, self.fail_update, self.drop_tags = 'pipeline', set(), False
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def methods(self):
        return [r['method'] for r in self.requests]

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                method = self.path.rsplit('/', 1)[-1]
                mock.requests.append({'method': method, 'path': self.path, 'body': body,
                                      'authorization': self.headers.get('Authorization'),
                                      'content_type': self.headers.get('Content-Type'),
                                      'connect_version': self.headers.get('Connect-Protocol-Version')})
                if self.headers.get('Authorization') != f'Bearer {TOKEN}':
                    status, payload = 401, {'code': 'unauthenticated', 'message': 'bad token'}
                else:
                    status, payload = mock.dispatch(method, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        return Handler

    def dispatch(self, method, body):
        if method == 'WhoAmI':
            return 200, {'principal': self.principal, 'roles': ['editor'], 'keyLabel': 'eyes-checkpoints',
                         'scopes': ['crm.address.write', 'crm.poi.write', 'crm.read']}
        if method == 'GetAddressCrm':
            return 200, {'meta': dict(self.meta), 'features': list(self.features), 'pois': list(self.pois)}
        if method == 'UpdateAddressMeta':
            if int(body['addressId']) in self.fail_update:
                return 500, {'code': 'internal', 'message': 'boom'}
            if self.drop_tags:
                body = {**body, 'tags': []}
            self.meta = {'addressId': str(body['addressId']), 'fullAddress': self.meta.get('fullAddress', ''),
                         'status': body.get('status', ''), 'ownerName': body.get('ownerName', ''),
                         'ownerEmail': body.get('ownerEmail', ''), 'ownerPhone': body.get('ownerPhone', ''),
                         'notes': body.get('notes', ''), 'tags': list(body.get('tags') or []),
                         'updatedAt': '2026-09-25T00:00:00Z', 'updatedBy': 'pipeline'}
            return 200, {'meta': dict(self.meta)}
        if method == 'CreatePoi':
            poi = {'id': str(len(self.pois) + 7), **body, 'updatedAt': '2026-09-25T00:00:00Z', 'updatedBy': 'pipeline'}
            self.pois.append(poi)
            return 200, {'poi': poi}
        if method == 'ListPois':
            wanted = body.get('addressId')
            return 200, {'pois': [p for p in self.pois if wanted is None or str(p.get('addressId')) == str(wanted)]}
        self.unknown.append(method)
        return 404, {'code': 'unimplemented', 'message': method}


@pytest.fixture
def crm():
    mock = CrmMock(META)
    yield mock
    mock.close()


@pytest.fixture
def client(crm):
    return crm_checkpoint.Client(crm.base, TOKEN)


def test_module_never_emits_feature_override_rpcs():
    source = Path(crm_checkpoint.__file__).read_text()
    assert 'UpsertFeature' not in source and 'DeleteFeature' not in source
    assert crm_checkpoint.WRITE_METHODS == frozenset({'CreatePoi', 'UpdateAddressMeta'})
    assert crm_checkpoint.ALLOWED_METHODS == frozenset({'WhoAmI', 'GetAddressCrm', 'ListPois', 'CreatePoi', 'UpdateAddressMeta'})


def test_config_reads_env_local_and_token_file(tmp_path, monkeypatch):
    monkeypatch.delenv('CRM_TOKEN', raising=False)
    assert crm_checkpoint.base_url(tmp_path) == 'http://localhost:8091'
    assert crm_checkpoint.token(tmp_path) is None
    (tmp_path / '.env.local').write_text('NEXT_PUBLIC_X=1\nGEO_SERVICE_URL="http://127.0.0.1:9999/"\n# comment\n')
    assert crm_checkpoint.base_url(tmp_path) == 'http://127.0.0.1:9999'
    monkeypatch.setenv('CRM_TOKEN', 'from-env')
    assert crm_checkpoint.token(tmp_path) == 'from-env'
    (tmp_path / '.run').mkdir()
    (tmp_path / '.run/eyes-crm-token').write_text('addr_file_token\n')
    assert crm_checkpoint.token(tmp_path) == 'addr_file_token'


def test_parse_tags_tolerates_null_none_and_encoded_strings():
    assert crm_checkpoint.parse_tags(None) == []
    assert crm_checkpoint.parse_tags('null') == []
    assert crm_checkpoint.parse_tags('') == []
    assert crm_checkpoint.parse_tags(['a', 'b']) == ['a', 'b']
    assert crm_checkpoint.parse_tags('["a", "b"]') == ['a', 'b']
    assert crm_checkpoint.parse_tags(json.dumps(json.dumps(['a']))) == ['a']
    assert crm_checkpoint.parse_tags('plain') == ['plain']


def test_merge_tags_replaces_same_eyes_key_and_keeps_others():
    merged = crm_checkpoint.merge_tags(['vip', 'eyes:checkpoint=CP0', 'eyes:band=low', 'vip'],
                                       ['eyes:checkpoint=CP1', 'eyes:band=low'])
    assert merged == ['vip', 'eyes:band=low', 'eyes:checkpoint=CP1']


def test_merge_notes_prepends_newest_first_caps_eyes_lines_and_keeps_other_text():
    existing = 'eyes:CP0 old line\n\nCall owner Monday\nGate code 1234'
    merged = crm_checkpoint.merge_notes(existing, 'eyes:CP1 new line')
    assert merged.split('\n') == ['eyes:CP1 new line', 'eyes:CP0 old line', '', 'Call owner Monday', 'Gate code 1234']
    many = '\n'.join(f'eyes:CP{i} line' for i in range(15)) + '\nkeep me'
    lines = crm_checkpoint.merge_notes(many, 'eyes:CP99 newest\nsecond').split('\n')
    eyes = [line for line in lines if line.startswith('eyes:')]
    assert len(eyes) == crm_checkpoint.NOTE_CAP and eyes[0] == 'eyes:CP99 newest second' and lines[-1] == 'keep me'
    assert crm_checkpoint.merge_notes(None, 'no prefix').split('\n') == ['eyes: no prefix']
    assert crm_checkpoint.merge_notes('eyes:same', 'eyes:same') == 'eyes:same'


def test_whoami_posts_json_with_bearer(crm, client):
    assert crm_checkpoint.whoami(client)['principal'] == 'pipeline'
    request = crm.requests[0]
    assert request['path'] == '/crm.v1.CrmService/WhoAmI' and request['body'] == {}
    assert request['authorization'] == f'Bearer {TOKEN}' and request['content_type'].startswith('application/json')
    assert request['connect_version'] == '1'


def test_client_refuses_unknown_methods_and_reports_http_errors(crm):
    client = crm_checkpoint.Client(crm.base, 'wrong')
    with pytest.raises(ValueError):
        client.call('SearchAddresses', {'query': 'x'})
    with pytest.raises(crm_checkpoint.CrmError) as info:
        client.call('WhoAmI', {})
    assert info.value.status == 401 and 'unauthenticated' in str(info.value)
    assert crm.unknown == []


def test_create_poi_caps_notes_and_points_website_at_review(crm, client):
    record = crm_checkpoint.create_poi(client, CP)
    body = crm.requests[-1]['body']
    assert body['category'] == 'pipeline-checkpoint' and body['addressId'] == 29790 and body['name'] == CP['title']
    assert body['latitude'] == CP['latitude'] and body['longitude'] == CP['longitude']
    assert body['website'] == 'data/references/eyes-checkpoints/CP1/REVIEW.md'
    assert len(body['notes']) <= crm_checkpoint.POI_NOTE_CAP and body['notes'].startswith('Registry tiers')
    assert record['poi']['id'] == '7' and record['method'] == 'CreatePoi'


def test_update_address_meta_preserves_owner_fields_and_merges_null_tags(crm, client):
    record = crm_checkpoint.update_address_meta(client, 29790, add_tags=['eyes:checkpoint=CP1'], note_line='eyes:CP1 report filed')
    assert crm.methods() == ['GetAddressCrm', 'UpdateAddressMeta', 'GetAddressCrm']
    body = crm.requests[1]['body']
    assert body['addressId'] == 29790 and body['status'] == 'quoted'
    assert (body['ownerName'], body['ownerEmail'], body['ownerPhone']) == ('Jane Roofer', 'jane@example.com', '0400 000 000')
    assert body['tags'] == ['eyes:checkpoint=CP1']
    assert body['notes'].split('\n') == ['eyes:CP1 report filed', '', 'Call owner Monday', 'Gate code 1234']
    assert record['preserved_ok'] and record['features_unchanged']
    assert record['diff']['preserved']['ownerName'] == {'before': 'Jane Roofer', 'after': 'Jane Roofer', 'same': True}
    assert record['diff']['tags'] == {'before': [], 'after': ['eyes:checkpoint=CP1']}
    assert crm.meta['ownerName'] == 'Jane Roofer' and crm.meta['status'] == 'quoted'


def test_update_address_meta_replaces_same_key_and_missing_optional_fields_stay_absent(client, crm):
    crm.meta = {'addressId': '29790', 'fullAddress': 'x', 'tags': ['eyes:checkpoint=CP0', 'vip']}
    crm_checkpoint.update_address_meta(client, 29790, add_tags=['eyes:checkpoint=CP1'], note_line='eyes:CP1 n')
    body = crm.requests[1]['body']
    assert body['tags'] == ['vip', 'eyes:checkpoint=CP1']
    assert not ({'status', 'ownerName', 'ownerEmail', 'ownerPhone'} & set(body))


def test_update_address_meta_refuses_suppressed_unless_allowed(crm, client):
    crm.meta = {**META, 'status': 'suppressed'}
    with pytest.raises(crm_checkpoint.CheckpointRefused):
        crm_checkpoint.update_address_meta(client, 29790, add_tags=['eyes:checkpoint=CP1'], note_line='eyes:CP1 n')
    assert crm.methods() == ['GetAddressCrm']
    record = crm_checkpoint.update_address_meta(client, 29790, add_tags=['eyes:checkpoint=CP1'], note_line='eyes:CP1 n',
                                                allow_suppressed=True)
    assert crm.requests[-2]['body']['status'] == 'suppressed' and record['preserved_ok']


def test_writes_refused_without_token(crm):
    client = crm_checkpoint.Client(crm.base, None)
    with pytest.raises(crm_checkpoint.CheckpointRefused):
        crm_checkpoint.create_poi(client, CP)
    with pytest.raises(crm_checkpoint.CheckpointRefused):
        crm_checkpoint.update_address_meta(client, 29790, add_tags=[], note_line='eyes:x')
    assert crm.requests == []


def test_file_checkpoint_writes_report_review_and_writes(tmp_path):
    files = crm_checkpoint.file_checkpoint(tmp_path, CP, writes=[{'method': 'CreatePoi'}])
    folder = tmp_path / 'data/references/eyes-checkpoints/CP1'
    report = json.loads((folder / 'report.json').read_text())
    assert files['report_sha256'] == hashlib.sha256((folder / 'report.json').read_bytes()).hexdigest()
    assert report['version'] == 'crm-checkpoint-v1' and report['checkpoint']['id'] == 'CP1'
    assert len(report['code_sha256']) == 64 and report['computed_at']
    review = (folder / 'REVIEW.md').read_text()
    assert review.startswith('# CP1 27 Cambridge cluster') and '## Reference agreement' in review
    assert '| object | band | iou |' in review and '| 44c724fd3480c167 | low | 0.91 |' in review
    assert '- Promote band v2?' in review and 'Diagnostics, not accuracy.' in review
    writes = json.loads((folder / 'crm-writes.json').read_text())
    assert writes['report_sha256'] == files['report_sha256'] and writes['writes'] == [{'method': 'CreatePoi'}]
    with pytest.raises(ValueError):
        crm_checkpoint.file_checkpoint(tmp_path, {**CP, 'id': '../evil'})


def test_publish_dry_run_writes_files_but_no_http(tmp_path, crm, client):
    receipt = crm_checkpoint.publish(tmp_path, CP, dry_run=True, client=client)
    assert crm.requests == [] and receipt['dry_run'] and receipt['ok'] and receipt['writes'] == []
    folder = tmp_path / 'data/references/eyes-checkpoints/CP1/dry-run'
    assert {p.name for p in folder.iterdir()} >= {'report.json', 'REVIEW.md', 'crm-writes.json'}
    assert not (folder.parent / 'crm-writes.json').exists()
    assert receipt['planned']['tags'] == ['eyes:checkpoint=CP1', 'eyes:band=low']
    assert receipt['planned']['note_line'].startswith('eyes:CP1 ') and '\n' not in receipt['planned']['note_line']
    with sqlite3.connect(f'file:{eyes_index.path(tmp_path)}?mode=ro', uri=True) as db:
        row = eyes_index.rows(db, 'checkpoints', id='CP1.dry-run')[0]
        assert eyes_index.rows(db, 'checkpoints', id='CP1') == []
    assert row['version'] == 'crm-checkpoint-v1' and row['payload']['dry_run'] is True
    assert row['payload']['report_sha256'] == receipt['report_sha256']


def test_publish_creates_poi_updates_meta_and_records_receipt(tmp_path, crm, client):
    guard_calls = []

    def guard(root):
        guard_calls.append(root)
        return {'ok': True, 'checks': [{'name': 'manifest', 'ok': True}]}

    receipt = crm_checkpoint.publish(tmp_path, {**CP, 'addresses': [{'address_id': 30286}, 29790, '30286']},
                                     client=client, guard=guard)
    assert crm.methods() == ['WhoAmI', 'ListPois', 'CreatePoi', 'GetAddressCrm', 'UpdateAddressMeta', 'GetAddressCrm',
                             'GetAddressCrm', 'UpdateAddressMeta', 'GetAddressCrm']
    assert crm.unknown == [] and set(crm.methods()) <= crm_checkpoint.ALLOWED_METHODS
    assert receipt['ok'] and receipt['whoami']['principal'] == 'pipeline' and receipt['poi']['id'] == '7'
    assert [a['address_id'] for a in receipt['addresses']] == [29790, 30286]
    assert all(a['preserved_ok'] for a in receipt['addresses'])
    assert receipt['guard'] == {'before': True, 'after': True, 'failed_after': []}
    assert guard_calls == [tmp_path, tmp_path]
    assert crm.meta['tags'] == ['eyes:checkpoint=CP1', 'eyes:band=low'] and crm.meta['notes'].startswith('eyes:CP1 ')
    assert crm.pois[0]['website'] == 'data/references/eyes-checkpoints/CP1/REVIEW.md'
    writes = json.loads((tmp_path / 'data/references/eyes-checkpoints/CP1/crm-writes.json').read_text())
    assert [w['method'] for w in writes['writes']] == ['CreatePoi', 'UpdateAddressMeta', 'UpdateAddressMeta']
    assert writes['writes'][1]['diff']['preserved']['status']['same']
    with sqlite3.connect(f'file:{eyes_index.path(tmp_path)}?mode=ro', uri=True) as db:
        payload = eyes_index.rows(db, 'checkpoints', id='CP1')[0]['payload']
    assert payload['poi_id'] == '7' and payload['dry_run'] is False and payload['principal'] == 'pipeline'
    report_sha = hashlib.sha256((tmp_path / 'data/references/eyes-checkpoints/CP1/report.json').read_bytes()).hexdigest()
    assert report_sha == receipt['report_sha256'] == payload['report_sha256'] == writes['report_sha256']
    assert all(w.get('applied_ok', True) for w in writes['writes'])


def test_publish_refuses_when_guard_fails_or_token_missing(tmp_path, crm):
    client = crm_checkpoint.Client(crm.base, TOKEN)
    with pytest.raises(crm_checkpoint.CheckpointRefused):
        crm_checkpoint.publish(tmp_path, CP, client=client, guard=lambda root: {'ok': False, 'checks': []})
    with pytest.raises(crm_checkpoint.CheckpointRefused):
        crm_checkpoint.publish(tmp_path, CP, client=crm_checkpoint.Client(crm.base, None))
    assert crm.requests == []
    with pytest.raises(ValueError):
        crm_checkpoint.publish(tmp_path, {**CP, 'address_id': None}, dry_run=True, client=client)


def test_cli_dry_run_and_whoami(tmp_path, crm, capsys, monkeypatch):
    (tmp_path / '.env.local').write_text(f'GEO_SERVICE_URL={crm.base}\n')
    monkeypatch.setenv('CRM_TOKEN', TOKEN)
    cp_file = tmp_path / 'cp.json'
    cp_file.write_text(json.dumps(CP))
    assert crm_checkpoint.main(['--root', str(tmp_path), '--id', 'CP1', '--cp', str(cp_file), '--dry-run']) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['dry_run'] and crm.requests == []
    assert crm_checkpoint.main(['--root', str(tmp_path), '--whoami']) == 0
    assert json.loads(capsys.readouterr().out)['principal'] == 'pipeline'
    assert crm_checkpoint.main(['--root', str(tmp_path), '--id', 'CP2', '--cp', str(cp_file), '--dry-run']) == 2


def test_address_location_reads_production_db_readonly():
    root = Path(__file__).resolve().parents[3]
    if not (root / 'data/australia_geo.db').exists():
        pytest.skip('production db missing')
    location = crm_checkpoint.address_location(root, 29790)
    assert location['full_address'].startswith('27 CAMBRIDGE ROAD')
    assert abs(location['latitude'] + 37.7904) < 1e-3 and abs(location['longitude'] - 145.3125) < 1e-3
    assert crm_checkpoint.address_location(root, -1) is None


def _live_folder(root):
    return root / 'data/references/eyes-checkpoints/CP1'


def test_checkpoint_id_rejects_trailing_newline(tmp_path):
    for bad in ('CP1\n', 'CP1.dry-run', '.hidden', 'a/b', ''):
        with pytest.raises(ValueError):
            crm_checkpoint.file_checkpoint(tmp_path, {**CP, 'id': bad})
    assert not (tmp_path / 'data').exists()


def test_eyes_tags_keep_one_value_per_key_on_one_line():
    tags = crm_checkpoint.eyes_tags({**CP, 'tags': {'checkpoint': 'CP9', 'band': 'low\nmedium', 'eyes:band': 'high'}})
    assert tags == ['eyes:checkpoint=CP1', 'eyes:band=low medium']


def test_merge_notes_replaces_the_same_checkpoint_line():
    existing = 'eyes:CP1 2026-09-24 old | x\neyes:CP0 y\n\nkeep'
    merged = crm_checkpoint.merge_notes(existing, 'eyes:CP1 2026-09-25 new | x')
    assert merged.split('\n') == ['eyes:CP1 2026-09-25 new | x', 'eyes:CP0 y', '', 'keep']


def test_render_review_orders_dict_rows_by_columns_and_marks_null():
    cp = {**CP, 'sections': [{'title': 'Agreement\nsplit', 'table': {'columns': ['object', 'iou', 'mismatch_m'],
          'rows': [{'mismatch_m': None, 'iou': 0.5, 'object': 'a', 'extra': 1}]}}]}
    review = crm_checkpoint.render_review(cp)
    assert '| a | 0.5 | n/a |' in review and '## Agreement split' in review


def test_update_address_meta_refuses_suppressed_regardless_of_case(crm, client):
    crm.meta = {**META, 'status': ' Suppressed '}
    with pytest.raises(crm_checkpoint.CheckpointRefused):
        crm_checkpoint.update_address_meta(client, 29790, add_tags=['eyes:checkpoint=CP1'], note_line='eyes:CP1 n')
    assert crm.methods() == ['GetAddressCrm']


def test_update_address_meta_checks_the_write_landed_and_other_notes_intact(crm, client):
    record = crm_checkpoint.update_address_meta(client, 29790, add_tags=['eyes:checkpoint=CP1'], note_line='eyes:CP1 n')
    assert record['applied_ok'] and record['diff']['notes']['other_text_same']
    crm.drop_tags = True
    record = crm_checkpoint.update_address_meta(client, 29790, add_tags=['eyes:band=low'], note_line='eyes:CP1 m')
    assert record['applied_ok'] is False and record['preserved_ok']


def test_publish_records_partial_writes_when_the_crm_fails_midway(tmp_path, crm, client):
    crm.fail_update = {30286}
    receipt = crm_checkpoint.publish(tmp_path, {**CP, 'addresses': [30286, 30296]}, client=client)
    assert not receipt['ok'] and any('30286' in e for e in receipt['errors'])
    assert [a['address_id'] for a in receipt['addresses']] == [29790, 30296]
    writes = json.loads((_live_folder(tmp_path) / 'crm-writes.json').read_text())
    assert [w['method'] for w in writes['writes']] == ['CreatePoi', 'UpdateAddressMeta', 'UpdateAddressMeta']
    with sqlite3.connect(f'file:{eyes_index.path(tmp_path)}?mode=ro', uri=True) as db:
        payload = eyes_index.rows(db, 'checkpoints', id='CP1')[0]['payload']
    assert payload['ok'] is False and payload['poi_id'] == '7' and payload['errors'] == receipt['errors']


def test_publish_records_connection_loss_instead_of_losing_the_receipt(tmp_path, crm, client):
    real_call = client.call

    def flaky(method, body):
        if method == 'UpdateAddressMeta':
            raise ConnectionRefusedError('geo service went away')
        return real_call(method, body)

    client.call = flaky
    receipt = crm_checkpoint.publish(tmp_path, CP, client=client)
    assert not receipt['ok'] and 'geo service went away' in receipt['errors'][0]
    writes = json.loads((_live_folder(tmp_path) / 'crm-writes.json').read_text())
    assert [w['method'] for w in writes['writes']] == ['CreatePoi']


def test_publish_after_guard_drift_is_recorded_not_raised(tmp_path, crm, client):
    results = iter([{'ok': True, 'checks': []}, {'ok': False, 'checks': [{'name': 'crm_geometry', 'ok': False}]}])
    receipt = crm_checkpoint.publish(tmp_path, CP, client=client, guard=lambda root: next(results))
    assert not receipt['ok'] and receipt['guard']['before'] is True and receipt['guard']['after'] is False
    assert receipt['guard']['failed_after'] == ['crm_geometry'] and any('crm_geometry' in e for e in receipt['errors'])
    writes = json.loads((_live_folder(tmp_path) / 'crm-writes.json').read_text())
    assert [w['method'] for w in writes['writes']] == ['CreatePoi', 'UpdateAddressMeta']


def test_publish_rerun_reuses_poi_replaces_note_and_archives_previous_receipt(tmp_path, crm, client):
    crm_checkpoint.publish(tmp_path, CP, client=client)
    first = json.loads((_live_folder(tmp_path) / 'crm-writes.json').read_text())
    receipt = crm_checkpoint.publish(tmp_path, CP, client=client)
    assert len(crm.pois) == 1 and crm.methods().count('CreatePoi') == 1 and receipt['poi']['id'] == '7'
    assert receipt['poi_reused'] is True and receipt['ok']
    assert [line for line in crm.meta['notes'].split('\n') if line.startswith('eyes:CP1')] == [receipt['planned']['note_line']]
    archived = _live_folder(tmp_path) / 'history' / first['report_sha256'][:12] / 'crm-writes.json'
    assert json.loads(archived.read_text())['writes'] == first['writes']


def test_dry_run_after_live_publish_keeps_the_live_receipt(tmp_path, crm, client):
    crm_checkpoint.publish(tmp_path, CP, client=client)
    live = (_live_folder(tmp_path) / 'crm-writes.json').read_bytes()
    crm_checkpoint.publish(tmp_path, CP, dry_run=True)
    assert (_live_folder(tmp_path) / 'crm-writes.json').read_bytes() == live
    with sqlite3.connect(f'file:{eyes_index.path(tmp_path)}?mode=ro', uri=True) as db:
        assert eyes_index.rows(db, 'checkpoints', id='CP1')[0]['payload']['dry_run'] is False
        assert eyes_index.rows(db, 'checkpoints', id='CP1.dry-run')[0]['payload']['dry_run'] is True


def test_filed_writes_redact_owner_fields_and_other_note_text(tmp_path, crm, client):
    receipt = crm_checkpoint.publish(tmp_path, CP, client=client)
    text = (_live_folder(tmp_path) / 'crm-writes.json').read_text() + json.dumps(receipt)
    for secret in ('Jane Roofer', 'jane@example.com', '0400 000 000', 'Gate code 1234', 'Call owner Monday'):
        assert secret not in text
    record = json.loads((_live_folder(tmp_path) / 'crm-writes.json').read_text())['writes'][1]
    assert record['diff']['preserved']['ownerName']['same'] and record['diff']['preserved']['status']['after'] == 'quoted'
    assert record['diff']['notes']['other_text_same'] and record['diff']['notes']['after']['eyes_lines'][0].startswith('eyes:CP1')
    assert crm.meta['ownerName'] == 'Jane Roofer' and crm.meta['notes'].endswith('Call owner Monday\nGate code 1234')


def test_publish_refuses_a_token_that_is_not_the_pipeline_principal(tmp_path, crm, client):
    crm.principal = 'admin'
    with pytest.raises(crm_checkpoint.CheckpointRefused):
        crm_checkpoint.publish(tmp_path, CP, client=client)
    assert crm.methods() == ['WhoAmI'] and not (tmp_path / 'data').exists()


@pytest.mark.parametrize('bad', [{'addresses': [{'id': 30286}]}, {'addresses': [0]}, {'latitude': float('nan')},
                                 {'longitude': 200.0}, {'sections': ['not a dict']}, {'address_id': 'abc'}])
def test_publish_validates_inputs_before_any_io(tmp_path, crm, client, bad):
    with pytest.raises(ValueError):
        crm_checkpoint.publish(tmp_path, {**CP, **bad}, client=client)
    assert crm.requests == [] and not (tmp_path / 'data').exists()


def test_note_line_and_poi_name_stay_single_line_without_none_text(crm, client):
    cp = {**CP, 'summary': None, 'title': 'CP1\n27 Cambridge'}
    assert 'None' not in crm_checkpoint.note_line(cp, when='2026-09-25T00:00:00')
    crm_checkpoint.create_poi(client, cp)
    body = crm.requests[-1]['body']
    assert body['name'] == 'CP1 27 Cambridge' and 'notes' not in body


def test_cli_live_publish_needs_a_passing_guard(tmp_path, crm, capsys, monkeypatch):
    (tmp_path / '.env.local').write_text(f'GEO_SERVICE_URL={crm.base}\n')
    monkeypatch.setenv('CRM_TOKEN', TOKEN)
    cp_file = tmp_path / 'cp.json'
    cp_file.write_text(json.dumps(CP))
    stub = types.ModuleType('scripts.ml.eyes_guard')
    stub.check = lambda root: {'ok': False, 'checks': [{'name': 'manifest:a983ad7a89b19808', 'ok': False}]}
    monkeypatch.setitem(sys.modules, 'scripts.ml.eyes_guard', stub)
    monkeypatch.setattr(scripts.ml, 'eyes_guard', stub, raising=False)
    assert crm_checkpoint.main(['--root', str(tmp_path), '--id', 'CP1', '--cp', str(cp_file)]) == 2
    assert crm.requests == [] and 'eyes_guard failed before' in capsys.readouterr().err
    assert crm_checkpoint.main(['--root', str(tmp_path), '--id', 'CP1', '--cp', str(cp_file), '--skip-guard']) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['ok'] and receipt['guard'] is None and receipt['poi']['category'] == 'pipeline-checkpoint'
    assert 'Jane Roofer' not in json.dumps(receipt)
