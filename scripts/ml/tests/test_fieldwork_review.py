"""scripts/ml/fieldwork_review.py: stage / push / pull of the 8 Oct review queue. Temp folders and an in-memory
Fieldwork only; nothing here reads data/scenes or touches the network."""
import copy
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from scripts.ml import fieldwork_review as fr

SCENE = 'win-fixture0001'
P2M = [[0.1, 0], [0, -0.1], [-25.6, 25.6]]
M2P = [[10.0, 0], [0, -10.0], [256, 256]]


def _png(size=512):
    image = Image.new('RGB', (size, size))
    for x in range(0, size, 16):
        for y in range(0, size, 16):
            image.putpixel((x, y), (x % 256, y % 256, 99))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _obj(oid, kind, ring, status='candidate'):
    return {'id': oid, 'kind': kind, 'label': kind.title(), 'ring': ring, 'status': status, 'geometry_reviewed': status == 'confirmed',
            'source': 'sam3-text-onnx', 'score': 0.9,
            'model': {'height_m': 3, 'base_m': 0, 'height_source': 'class-prior', 'height_verified': False, 'width_m': 1,
                      'length_m': 1, 'area_m2': 1, 'rotation_deg': 0, 'color': '#fff', 'footprint_area_px': 1}}


RING_T = [[100, 100], [160, 100], [160, 170], [100, 170]]
RING_T_TRIM = [[100, 100], [160, 100], [160, 150], [100, 150]]
RING_B = [[300, 300], [380, 300], [380, 360], [300, 360]]
RING_B_FIX = [[300, 300], [390, 300], [390, 360], [300, 360]]
RING_B2 = [[380, 300], [420, 300], [420, 360], [380, 360]]
RING_G = [[20, 400], [80, 400], [80, 460], [20, 460]]
RING_S = [[200, 20], [230, 20], [230, 50], [200, 50]]


@pytest.fixture
def repo(tmp_path):
    png = _png()
    sha = hashlib.sha256(png).hexdigest()
    folder = tmp_path / 'data/scenes' / SCENE
    folder.mkdir(parents=True)
    (folder / f'{sha}.png').write_bytes(png)
    objects = [_obj('tree0001', 'tree', RING_T), _obj('bldg0001', 'building', RING_B), _obj('bldg0002', 'building', RING_B2),
               _obj('veg00001', 'vegetation', RING_G), _obj('veg00002', 'tree', RING_S, status='confirmed')]
    model = {'id': SCENE, 'title': 'Recapture window · x · 9 TEST STREET, Mooroolbark VIC 3138 · t', 'image_sha256': sha,
             'image_width': 512, 'image_height': 512, 'pixel_to_meters': P2M, 'meters_to_pixel': M2P, 'objects': objects,
             'groups': [], 'model_signature': 'a' * 64}
    (folder / 'latest.json').write_text(json.dumps(model))
    (folder / 'review.json').write_text(json.dumps({'revision': 3, 'model_signature': 'a' * 64, 'objects': objects, 'groups': [],
                                                     'updated_at': '2026-10-08T00:00:00Z', 'updated_by': 'local'}))

    def fresh(oid, ring, status='candidate'):
        return {'objects': [{'object_id': oid, 'base_ring_sha': fr.ring_sha(ring), 'base_status': status}]}

    common = {'scene': SCENE, 'scene_image_sha256': sha}
    items = {
        'shadow': [
            {**common, 'line': 1, 'object_id': 'tree0001', 'kind': 'tree', 'excluded': True, 'excluded_reason': 'applied'},
            {**common, 'line': 2, 'object_id': 'tree0001', 'kind': 'tree', 'proposal_id': 'abcdef0123456789', 'status_at_compute': 'candidate',
             'current_ring': RING_T, 'current_ring_sha': fr.ring_sha(RING_T), 'proposed_ring': RING_T_TRIM, 'proposed_holes': [],
             'removed_m2': 6.0, 'area_before_m2': 42.0, 'removed_share': 0.14, 'same_tree_as': None,
             'agent_hint': {'verdict': 'right', 'note': 'band'}}],
        'cut': [
            {**common, 'line': n, 'object_id': 'bldg0001', 'kind': 'building', 'type': 'spike_fill', 'type_words': 'Notch filled',
             'proposal_id': f'{n:02d}cdef0123456789', 'status_at_compute': 'candidate', 'current_ring': RING_B,
             'current_ring_sha': fr.ring_sha(RING_B), 'proposed_ring': RING_B_FIX, 'proposed_holes': [],
             'same_outline_as': [m for m in (2, 3) if m != n], 'agent_hint': {'verdict': 'right', 'note': 'n'}}
            for n in (2, 3)] + [
            {**common, 'line': 70, 'object_id': 'bldg0002', 'kind': 'building', 'type': 'body_carry', 'type_words': 'Whole roof copied',
             'proposal_id': '70cdef0123456789', 'status_at_compute': 'candidate', 'current_ring': RING_B2,
             'current_ring_sha': fr.ring_sha(RING_B2), 'proposed_ring': RING_B2, 'proposed_holes': [], 'same_outline_as': [],
             'agent_hint': {'verdict': 'wrong', 'note': 'w'}}],
        'vegetation': [
            {**common, 'line': 10, 'object_id': 'veg00001', 'reason': 'smooth', 'freshness': fresh('veg00001', RING_G), 'sheets': []},
            {**common, 'line': 11, 'object_id': 'veg00002', 'reason': 'crown', 'freshness': fresh('veg00002', RING_S, 'confirmed'), 'sheets': []}],
        'join': [
            {**common, 'line': 1, 'feature_id': 'feat000011112222', 'members': ['bldg0001', 'bldg0002'], 'rules': ['cover'], 'n_members': 2,
             'area_m2': 72.0, 'largest_member_m2': 48.0, 'cover_max': 0.7, 'gap_max_m': 0.0, 'absorbed_m2': 0.0, 'bridge_m2': 0.0,
             'joined_ring': [[300, 300], [420, 300], [420, 360], [300, 360]], 'joined_holes': [],
             'freshness': {'objects': fresh('bldg0001', RING_B)['objects'] + fresh('bldg0002', RING_B2)['objects']},
             'agent_hint': {'verdict': 'right', 'note': 'j'}}],
    }
    (tmp_path / 'items.json').write_text(json.dumps(items))
    context = {'cuts': {'whole_other': list(range(66, 117)), 'confirmed': [], 'hint': {}, 'notch': [], 'pictured': [2, 3, 70]},
               'shadows': {'confirmed': [], 'hint': {}, 'pictured': [2]},
               'veg': {'display': {'10': 'grass', '11': 'tree-or-shrub'}, 'status': {'10': 'candidate', '11': 'confirmed'},
                       'bulk_must_skip': [], 'agent_type': {}, 'pictured': [], 'confirmed_pictured': [], 'sheets': {}}}
    (tmp_path / 'context.json').write_text(json.dumps(context))
    return tmp_path


def _stage(repo, **kw):
    return fr.stage(root=repo, items='items.json', context='context.json', out='stage', **kw)


class FakeFieldwork:
    """In-memory stand-in for the annotation server: create at revision 0, 409 on a revision mismatch, assets by sha, and a
    change feed whose actor is the one shared token ('local-dev' as on a --dev server)."""

    def __init__(self):
        self.docs, self.assets, self.writes, self.changes = {}, {}, [], []

    def _log(self, kind, doc, actor='local-dev'):
        self.changes.append({'cursor': str(len(self.changes) + 1), 'kind': kind, 'id': doc['id'], 'projectId': doc.get('projectId', doc['id']),
                             'operation': 'create' if doc['revision'] == 1 else 'update', 'revision': doc['revision'], 'actor': actor,
                             'timestamp': '2026-10-09T00:00:00Z'})

    def head_asset(self, digest):
        data = self.assets.get(digest)
        return (200, len(data), digest) if data is not None else (404, -1, None)

    def put_asset(self, digest, data, content_type):
        assert hashlib.sha256(data).hexdigest() == digest
        self.assets[digest] = data
        self.writes.append(('asset', digest))
        return 201, b'{}'

    def rpc(self, method, data):
        kind = {'Project': 'project', 'Item': 'item', 'Label': 'label'}
        if method.startswith('Save'):
            k = kind[method[4:]]
            doc = copy.deepcopy(data[k])
            key = (k, doc['id'])
            current = self.docs.get(key)
            if (current['revision'] if current else 0) != data['expectedRevision']:
                return 409, {'code': 'aborted'}
            doc['revision'] = data['expectedRevision'] + 1
            doc['createdAt'] = doc['updatedAt'] = '2026-10-09T00:00:00Z'
            self.docs[key] = doc
            self.writes.append((k, doc['id']))
            self._log(k, doc)
            return 200, {k: doc}
        if method in ('GetProject', 'GetItem'):
            k = 'project' if method == 'GetProject' else 'item'
            doc = self.docs.get((k, data['id']))
            return (200, {k: doc}) if doc else (404, {'code': 'not_found'})
        if method == 'ListProjects':
            return 200, {'projects': [d for (kk, _), d in sorted(self.docs.items()) if kk == 'project'], 'nextCursor': ''}
        if method in ('ListLabels', 'ListItems'):
            k = 'label' if method == 'ListLabels' else 'item'
            docs = [d for (kk, _), d in sorted(self.docs.items()) if kk == k and d.get('projectId') == data['projectId']]
            return 200, {k + 's': docs, 'nextCursor': ''}
        if method == 'ListChanges':
            return 200, {'changes': [c for c in self.changes if c['projectId'] == data['projectId']], 'nextCursor': str(len(self.changes)),
                         'hasMore': False}
        raise AssertionError(method)

    def edit(self, item_id, change, actor='local-dev', record=True):
        doc = copy.deepcopy(self.docs[('item', item_id)])
        change(doc)
        doc['revision'] += 1
        self.docs[('item', item_id)] = doc
        if record:
            self._log('item', doc, actor)


# ------------------------------------------------------------------------------------------------------------- geometry

def test_crop_transform_round_trip_and_bounds():
    rings = [RING_T, RING_T_TRIM]
    x0, y0, w, h = fr.crop_box(rings, M2P, 512, 512)
    assert w == h and fr.CROP_MIN <= w <= 512
    assert x0 <= 100 and y0 <= 100 and x0 + w >= 160 and y0 + h >= 170
    p2m, m2p = fr.crop_frame(P2M, M2P, x0, y0)
    for p in RING_T:
        crop_p = [p[0] - x0, p[1] - y0]
        assert fr.transform(crop_p, p2m) == pytest.approx(fr.transform(p, P2M))
        assert fr.transform(fr.transform(p, P2M), m2p) == pytest.approx(crop_p)
    back = fr.shift_ring(fr.shift_ring(RING_T_TRIM, x0, y0), -x0, -y0)
    assert back == RING_T_TRIM
    polygon = fr.normalized_polygon([fr.shift_ring(RING_T, x0, y0)], w, h)
    assert polygon[0][0] == polygon[0][-1] and all(0 <= c <= 1 for p in polygon[0] for c in p)


def test_footprint_metrics_matches_rectangle():
    m = fr.footprint_metrics(RING_B, P2M)
    assert m['area_m2'] == pytest.approx(48.0) and m['length_m'] == pytest.approx(8.0) and m['width_m'] == pytest.approx(6.0)


# ----------------------------------------------------------------------------------------------------------------- stage

def test_stage_is_deterministic_and_checks_items(repo):
    first = _stage(repo)
    second = _stage(repo)
    assert first['stageSha'] == second['stageSha']
    assert first['counts'] == {'shadow': 1, 'cut': 3, 'veg': 1, 'join': 1, 'round5': 3}
    manifest = json.loads((Path(first['stage']) / 'manifest.json').read_text())
    ids = manifest['items']
    assert ids == sorted(ids) and not any(i.startswith('addr-scene-') for i in ids)
    assert ids[0].startswith('r1008-1-shadow-002-pabcdef01') and ids[-1] == 'r1008-5-round5-3'
    assert manifest['excluded'][0]['line'] == 1
    shadow = json.loads((Path(first['stage']) / 'items' / f'{ids[0]}.json').read_text())
    assert shadow['adapter'] == 'addr-scene' and 'crop' not in shadow and shadow['status'] == 'unreviewed'
    assert [a['id'] for a in shadow['annotations']] == ['cur-tree0001', 'new-tree0001']
    assert [a['labelId'] for a in shadow['annotations']] == ['mooroolbark-review-current', 'mooroolbark-review-proposed']
    frame = json.loads((Path(first['stage']) / 'blobs' / shadow['source']['model']['sha256']).read_bytes())
    assert frame['image_width'] == shadow['source']['width'] and frame['objects'][0]['kind'] == 'other'
    assert frame['objects'][1]['label'] == 'PROPOSED trim · line 002' and frame['objects'][1]['status'] == 'candidate'
    crop = Image.open(Path(first['stage']) / 'blobs' / shadow['source']['image']['sha256'])
    assert crop.size == (shadow['source']['width'], shadow['source']['height'])
    veg = json.loads((Path(first['stage']) / 'items' / f"{next(i for i in ids if '-veg-' in i)}.json").read_text())
    assert veg['source']['width'] == 512 and veg['metadata']['addr']['crop']['fullFrame'] is True
    assert {a['labelId'] for a in veg['annotations']} == {'mooroolbark-review-veg-grass', 'mooroolbark-review-veg-tree-or-shrub'}
    assert veg['metadata']['addr']['liveStatus'] == {'veg00001': 'candidate', 'veg00002': 'confirmed'}


def test_stale_line_is_refused_unless_dropped(repo):
    folder = repo / 'data/scenes' / SCENE
    review = json.loads((folder / 'review.json').read_text())
    review['objects'][0]['ring'] = [[100, 100], [161, 100], [161, 170], [100, 170]]
    (folder / 'review.json').write_text(json.dumps(review))
    with pytest.raises(fr.ReviewError, match='shadow line 2 is stale'):
        _stage(repo)
    result = _stage(repo, drop_stale=True)
    assert result['counts'].get('shadow') is None and result['dropped'][0]['line'] == 2


def test_item_checks_refuse_crop_and_unknown_label(repo):
    staged = _stage(repo)
    item = json.loads(next((Path(staged['stage']) / 'items').glob('r1008-2-*.json')).read_text())
    labels = {f"mooroolbark-review-{label['suffix']}" for label in fr.LABELS}
    fr.check_item(item, labels)
    with pytest.raises(fr.ReviewError, match='crop'):
        fr.check_item({**item, 'crop': {'x': 0, 'y': 0, 'width': 1, 'height': 1}}, labels)
    with pytest.raises(fr.ReviewError, match='label'):
        fr.check_item(item, labels - {'mooroolbark-review-proposed'})
    with pytest.raises(fr.ReviewError, match='id'):
        fr.check_item({**item, 'id': 'addr-scene-x'}, labels)


# ------------------------------------------------------------------------------------------------------------ guards

def test_guards(repo, monkeypatch):
    monkeypatch.delenv('FIELDWORK_PUSH_ALLOW_LIVE', raising=False)
    monkeypatch.delenv('ANNOTATION_TOKEN', raising=False)
    with pytest.raises(fr.ReviewError, match='live'):
        fr.guard_origin(fr.DEFAULT_REMOTE_URL, dry_run=False)
    with pytest.raises(fr.ReviewError, match='live'):
        fr.guard_origin('https://FIELDWORK-annotations.ben-ebsworth.workers.dev:443/', dry_run=False)
    assert fr.guard_origin('http://127.0.0.1:18131', dry_run=False) is None
    with pytest.raises(fr.ReviewError, match='custom'):
        fr.guard_origin('https://example.org', dry_run=False)
    for project in ('mooroolbark', 'other-project'):
        with pytest.raises(fr.ReviewError, match='refusing project'):
            fr.push(root=repo, stage_dir='x', url='http://127.0.0.1:1', directory='d', project=project, client=FakeFieldwork())
    with pytest.raises(fr.ReviewError, match='live'):
        fr.push(root=repo, stage_dir='x', url=fr.DEFAULT_REMOTE_URL, directory='d')


# -------------------------------------------------------------------------------------------------------------- push

def test_push_creates_then_skips_and_never_overwrites(repo):
    staged = _stage(repo)
    server = FakeFieldwork()
    first = fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    assert first['counts']['created'] == 9 and first['counts']['project_created'] == 1 and first['counts']['label_created'] == 13
    assert first['status'] == 'pushed' and not first['conflicts']
    writes = len(server.writes)
    again = fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    assert again['counts'] == {'project_present': 1, 'label_present': 13, 'skip_receipt': 9}
    assert len(server.writes) == writes
    # Without receipts (a fresh folder) the 409 path compares with the server copy.
    fresh = fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror2', client=server)
    assert fresh['counts']['skip_identical'] == 9 and len(server.writes) == writes
    # A changed staged item with an existing id is stopped, in both folders, and nothing is overwritten.
    path = next((Path(staged['stage']) / 'items').glob('r1008-1-*.json'))
    item = json.loads(path.read_text())
    before = copy.deepcopy(server.docs[('item', item['id'])])
    item['notes'] = 'changed'
    path.write_text(json.dumps(item))
    changed = fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror3', client=server)
    assert changed['items'][item['id']] == 'conflict_id_reuse' and changed['status'] == 'partial'
    receipted = fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    assert receipted['items'][item['id']] == 'conflict_changed_since_receipt'
    assert server.docs[('item', item['id'])] == before and len(server.writes) == writes
    with pytest.raises(fr.ReviewError, match='holds receipts'):
        fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:2', directory='mirror', client=server)


def test_push_finds_the_project_on_a_server_without_get_project(repo):
    """The live server has no GetProject: like any unknown method it answers 404 not_found. A rerun after the canary must see the
    project through ListProjects instead of creating it again (SaveProject at revision 0 would be refused with 409)."""
    staged = _stage(repo)

    class OldServer(FakeFieldwork):
        def rpc(self, method, data):
            if method == 'GetProject':
                return 404, {'code': 'not_found', 'message': 'not found'}
            return super().rpc(method, data)

    server = OldServer()
    plan = fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='m', client=server, dry_run=True)
    assert plan['counts']['project_create'] == 1 and server.writes == []
    canary = fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='m', client=server, limit=1)
    assert canary['counts'] == {'project_created': 1, 'label_created': 13, 'created': 1}
    rest = fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='m', client=server)
    assert rest['counts'] == {'project_present': 1, 'label_present': 13, 'skip_receipt': 1, 'created': 8} and rest['status'] == 'pushed'
    assert sum(1 for w in server.writes if w[0] == 'project') == 1


def test_push_limit_is_a_canary(repo):
    staged = _stage(repo)
    server = FakeFieldwork()
    result = fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='m', client=server, limit=1)
    assert result['counts']['created'] == 1 and sum(1 for k, _ in server.docs if k == 'item') == 1


# -------------------------------------------------------------------------------------------------------------- pull

@pytest.fixture
def pushed(repo):
    staged = _stage(repo)
    server = FakeFieldwork()
    fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    ids = {doc['metadata']['addr']['family'] + str(doc['metadata']['addr'].get('line', '')): doc['id']
           for (k, _), doc in server.docs.items() if k == 'item'}
    return repo, server, ids


def _set(mark_id, **overrides):
    def change(doc):
        for mark in doc['annotations']:
            if mark['id'] == mark_id:
                mark['properties']['addr'].setdefault('overrides', {}).update(overrides)
        doc['status'] = 'in_review'
    return change


def _pull(repo, server):
    return fr.pull(root=repo, url='http://127.0.0.1:1', directory='mirror', out='out', context='context.json', client=server)


def test_pull_untouched_writes_no_decisions(pushed):
    repo, server, _ = pushed
    result = _pull(repo, server)
    assert result['decisions'] == 0 and result['answers'] == [] and not result['unknownItems']


def _agent_assessment(server, item_id, annotation_id):
    item = server.docs[('item', item_id)]
    review = {'id': 'ar-fixture', 'annotationId': annotation_id, 'reviewer': 'codex', 'decision': 'approved',
              'notes': 'Visible boundary supported, shadow excluded.', 'source': 'agent:fieldwork', 'humanApproval': False,
              'trainingEligible': False, 'basedOnRevision': item['revision'], 'createdAt': '2026-10-09T00:00:00Z',
              'subject': fr.agent_review_subject(item, annotation_id)}
    server.edit(item_id, lambda doc: doc.setdefault('agentReviews', []).append(copy.deepcopy(review)), actor='agent:fieldwork:codex')
    server.changes[-1]['document'] = copy.deepcopy(server.docs[('item', item_id)])
    return review


def test_agent_only_pull_never_creates_human_answers_or_previews(pushed):
    repo, server, ids = pushed
    _agent_assessment(server, ids['shadow2'], 'new-tree0001')
    result = _pull(repo, server)
    assert result['decisions'] == 0 and result['answers'] == [] and result['unread'] == []
    assert json.loads(Path(result['files']['previews']).read_text())['requests'] == []
    agents = json.loads(Path(result['files']['agents']).read_text())
    assert agents['source'] == 'agent:fieldwork' and agents['humanApproval'] is False
    assert agents['reviews'][0]['current'] and agents['reviews'][0]['provenanceVerified']
    assert agents['reviews'][0]['actor'] == 'agent:fieldwork:codex'


@pytest.mark.parametrize('agent_first', [True, False])
def test_human_and_agent_reviews_coexist_in_either_order(pushed, agent_first):
    repo, server, ids = pushed
    item_id = ids['shadow2']
    if agent_first:
        _agent_assessment(server, item_id, 'new-tree0001')
    server.edit(item_id, _set('new-tree0001', status='rejected'))
    human_revision = server.docs[('item', item_id)]['revision']
    if not agent_first:
        _agent_assessment(server, item_id, 'new-tree0001')
    result = _pull(repo, server)
    decisions = json.loads(Path(result['files']['decisions']).read_text())['decisions']
    assert len(decisions) == 1 and decisions[0]['word'] == 'wrong'
    assert decisions[0]['fieldwork']['actor'] == 'local-dev'
    assert decisions[0]['fieldwork']['humanRevision'] == human_revision
    assert decisions[0]['fieldwork']['changeActors'] == ['local-dev']
    assert f' rev {human_revision}:' in fr._sentence(decisions[0], fr.DEFAULT_PROJECT)
    agent = json.loads(Path(result['files']['agents']).read_text())['reviews'][0]
    assert agent['decision'] == 'approved' and agent['current']
    # Source geometry edits invalidate the assessment; a human decision alone does not.
    server.edit(item_id, lambda doc: doc['annotations'][-1]['geometry']['coordinates'][0][1].__setitem__(0, 0.9))
    result = _pull(repo, server)
    assert not json.loads(Path(result['files']['agents']).read_text())['reviews'][0]['current']


@pytest.mark.parametrize('status,word', [('confirmed', 'right'), ('rejected', 'wrong'), ('needs_adjustment', 'partly'), ('candidate', None)])
def test_shadow_status_words(pushed, status, word):
    repo, server, ids = pushed
    server.edit(ids['shadow2'], _set('new-tree0001', status=status))
    result = _pull(repo, server)
    decisions = json.loads(Path(result['files']['decisions']).read_text())['decisions']
    assert [d.get('word') for d in decisions] == [word]
    if word is None:
        assert decisions[0]['unread']['changed'] == ['item status unreviewed -> in_review', 'new-tree0001: status=candidate']
        assert [u['itemId'] for u in result['unread']] == [ids['shadow2']]
    if word == 'right':
        requests = json.loads(Path(result['files']['previews']).read_text())['requests']
        assert requests[0]['object'] == 'tree0001' and requests[0]['proposed_ring_sha'] == fr.ring_sha(RING_T_TRIM)
        assert requests[0]['object_scope'] is True and requests[0]['fieldwork']['source'] == 'human:fieldwork'
        assert result['answers'] == ['shadows: right: 2']


def test_edited_outline_maps_back_to_frame_pixels(pushed):
    repo, server, ids = pushed
    item = server.docs[('item', ids['shadow2'])]
    crop = item['metadata']['addr']['crop']

    def change(doc):
        mark = next(m for m in doc['annotations'] if m['id'] == 'new-tree0001')
        mark['geometry']['coordinates'] = fr.normalized_polygon([[[p[0] - crop['x0'], p[1] - crop['y0']] for p in
                                                                   [[100, 100], [160, 100], [160, 140], [100, 140]]]], crop['width'], crop['height'])
        mark['properties']['addr']['overrides'] = {'status': 'needs_adjustment', 'geometry_reviewed': False}
    server.edit(ids['shadow2'], change)
    decision = json.loads(Path(_pull(repo, server)['files']['decisions']).read_text())['decisions'][0]
    assert decision['word'] == 'edited'
    assert [c for p in decision['editedRingFrame'][0] for c in p] == pytest.approx([100, 100, 160, 100, 160, 140, 100, 140])


def test_change_to_current_object_is_an_exception(pushed):
    repo, server, ids = pushed
    server.edit(ids['shadow2'], _set('cur-tree0001', status='rejected'))
    server.edit(ids['shadow2'], _set('new-tree0001', status='confirmed'))
    result = _pull(repo, server)
    decision = json.loads(Path(result['files']['decisions']).read_text())['decisions'][0]
    assert decision['word'] is None and 'cur-tree0001' in decision['exceptions'][0] and result['answers'] == []


def test_cut_rules_whole_roof_held_and_one_outline(pushed):
    repo, server, ids = pushed
    for line in ('cut2', 'cut3', 'cut70'):
        oid = 'bldg0002' if line == 'cut70' else 'bldg0001'
        server.edit(ids[line], _set(f'new-{oid}', status='confirmed'))
    result = _pull(repo, server)
    cuts = result['resolution']['cuts']
    assert cuts['write'] == [2] and cuts['resolved'][0]['line'] == 3
    assert cuts['held'][0]['line'] == 70 and 'shape check' in cuts['held'][0]['why']
    assert result['answers'] == ['cuts: right: 2']  # line 3 (the 2-and-3 rule) stays in resolved, not in the answer line
    assert not any('whole roofs too' in a for a in result['answers'])
    requests = json.loads(Path(result['files']['previews']).read_text())['requests']
    assert [r['line'] for r in requests] == [2]


def test_several_lines_on_one_outline_are_held_for_a_composed_preview():
    decisions = [{'family': 'cut', 'line': n, 'word': 'right', 'sceneId': 's', 'objectIds': ['o']} for n in (31, 40)]
    context = {'cuts': {'whole_other': list(range(66, 117))}}
    cuts = fr.resolve(decisions, context)['cuts']
    assert cuts['write'] == [] and cuts['held'][0]['lines'] == [31, 40]


def test_cut_needs_adjustment_is_unclear_with_notes(pushed):
    repo, server, ids = pushed
    server.edit(ids['cut2'], _set('new-bldg0001', status='needs_adjustment', notes='Agent hint: right. n Corner too far.'))
    decision = json.loads(Path(_pull(repo, server)['files']['decisions']).read_text())['decisions'][0]
    assert decision['word'] == 'unclear' and 'Corner too far' in decision['objectNotes']


def test_veg_confirm_label_and_tree_or_shrub(pushed):
    repo, server, ids = pushed
    server.edit(ids['veg'], _set('veg00001', status='confirmed'))
    server.edit(ids['veg'], _set('veg00002', status='confirmed'))
    result = _pull(repo, server)
    decisions = {d['objectId']: d for d in json.loads(Path(result['files']['decisions']).read_text())['decisions']}
    assert decisions['veg00001']['word'] == 'grass'
    assert decisions['veg00002']['word'] is None and decisions['veg00002']['typeNeeded']
    veg = json.loads(Path(result['files']['veg']).read_text())
    assert veg['source'] == 'human:fieldwork'
    assert [(d['object'], d['vegetation_type']) for d in veg['decisions']] == [('veg00001', 'grass')]
    server.edit(ids['veg'], _set('veg00002', label='shrub · 0011'))
    veg = json.loads(Path(_pull(repo, server)['files']['veg']).read_text())
    assert sorted((d['object'], d['vegetation_type']) for d in veg['decisions']) == [('veg00001', 'grass'), ('veg00002', 'shrub')]


def test_veg_bulk_notes_expand_and_report_skips(pushed):
    repo, server, ids = pushed

    def notes(doc):
        doc['notes'] = doc['notes'] + 'grass right except 99\ntree-or-shrub right\nall right\nplease look again\n'
    server.edit(ids['veg'], notes)
    result = _pull(repo, server)
    decisions = json.loads(Path(result['files']['decisions']).read_text())['decisions']
    typed = [(d['line'], d['word']) for d in decisions if d.get('word')]
    assert typed == [(10, 'grass'), (10, 'grass')]
    bulk = [d for d in decisions if d.get('bulk')]
    assert {s['why'] for d in bulk for s in d['skipped']} >= {'not in this item', 'Proposed tree (or shrub): no storable type'}
    not_understood = [d['notUnderstood'] for d in decisions if d.get('notUnderstood')]
    assert not_understood == ['tree-or-shrub right', 'please look again']
    assert result['answers'] == ['veg: grass: 10']


def test_veg_bulk_skips_confirmed_and_must_skip_and_touched(pushed):
    repo, server, ids = pushed
    item = server.docs[('item', ids['veg'])]
    meta = item['metadata']['addr']
    receipt = json.loads((repo / 'mirror/review/items' / f"{ids['veg']}.json").read_text())
    doc = copy.deepcopy(item)
    doc['revision'] += 1
    doc['notes'] += 'all right\n'
    meta2 = doc['metadata']['addr']
    meta2['liveStatus'] = {**meta['liveStatus'], 'veg00001': 'confirmed'}
    receipt['document']['metadata']['addr']['liveStatus'] = meta2['liveStatus']
    out = fr.read_item(doc, receipt, {'origin': 'o', 'projectId': 'p', 'cursor': '1'})
    skipped = next(d for d in out if d.get('bulk'))['skipped']
    assert {'line': 10, 'why': 'outline is confirmed in your scenes'} in skipped


def test_joins_and_round5(pushed):
    repo, server, ids = pushed
    server.edit(ids['join1'], _set('join-feat000011112222', status='rejected'))
    for n in (1, 2, 3):
        def approve(doc):
            doc['status'] = 'approved'
        server.edit(ids[f'round5{n}'], approve)
    result = _pull(repo, server)
    assert result['answers'] == ['joins: wrong: 1', 'round5: yes to defaults']
    joins = json.loads(Path(result['files']['joins']).read_text())['decisions']
    assert joins[0]['verdict'] == 'wrong' and joins[0]['members'] == ['bldg0001', 'bldg0002']

    def reject(doc):
        doc['status'] = 'rejected'
        doc['notes'] += 'not yet\n'
    server.edit(ids['round52'], reject)
    result = _pull(repo, server)
    assert result['answers'][-1] == 'round5: keep the new fold / no list / skip the pair'


def test_check_answers_rules():
    context = {'cuts': {'whole_other': list(range(66, 117))}}
    pushed = {'shadow': {2, 3}, 'cut': {2, 3, 70}, 'veg': {10, 11}, 'join': {1}}
    assert fr.check_answers('shadows: right: 2 / partly: 3\ncuts: right: 2, 3\nveg: grass: 10 / wrong: 11\njoins: right: 1', context, pushed)
    with pytest.raises(AssertionError, match='whole-roof'):
        fr.check_answers('cuts: right: 70', context, pushed)
    with pytest.raises(AssertionError, match='no Fieldwork item'):
        fr.check_answers('shadows: right: 9', context, pushed)
    with pytest.raises(AssertionError, match='unparsed'):
        fr.check_answers('cuts: partly: 2', context, pushed)
    with pytest.raises(AssertionError, match='twice'):
        fr.check_answers('veg: grass: 10 / tree: 10', context, pushed)


def test_pull_refuses_scene_folders(pushed):
    repo, server, _ = pushed
    with pytest.raises(fr.ReviewError, match='refusing to write'):
        fr.pull(root=repo, url='http://127.0.0.1:1', directory='mirror', out='data/scenes/x', context='context.json', client=server)


# ------------------------------------------------------------------------------------- skeptic fixes (9 Oct): item status

def _item_status(status, notes=None):
    def change(doc):
        doc['status'] = status
        if notes:
            doc['notes'] += notes
    return change


def _both(item_status, **overrides):
    def change(doc):
        _set(next(m['id'] for m in doc['annotations'] if m['id'].startswith(('new-', 'join-'))), **overrides)(doc)
        doc['status'] = item_status
    return change


@pytest.mark.parametrize('key,status,answer', [('shadow2', 'approved', 'shadows: right: 2'), ('shadow2', 'rejected', 'shadows: wrong: 2'),
                                               ('cut2', 'approved', 'cuts: right: 2'), ('join1', 'rejected', 'joins: wrong: 1')])
def test_item_level_status_answers_one_proposal_items(pushed, key, status, answer):
    repo, server, ids = pushed
    server.edit(ids[key], _item_status(status))
    result = _pull(repo, server)
    decisions = json.loads(Path(result['files']['decisions']).read_text())['decisions']
    assert [(d['word'], d['how'], d['itemStatus']) for d in decisions] == [(answer.split(': ')[1], 'item status', status)]
    assert result['answers'] == [answer] and not result['conflicts'] and not result['unread']
    if answer.endswith('right: 2'):
        request = json.loads(Path(result['files']['previews']).read_text())['requests'][0]
        assert request['line'] == 2 and 'item Review status set to Approved' in request['sentence']


def test_item_and_outline_agreeing_is_one_decision(pushed):
    repo, server, ids = pushed
    server.edit(ids['shadow2'], _both('approved', status='confirmed'))
    decision = json.loads(Path(_pull(repo, server)['files']['decisions']).read_text())['decisions'][0]
    assert (decision['word'], decision['how']) == ('right', 'object status and item status')


@pytest.mark.parametrize('key,item_status,overrides', [('shadow2', 'rejected', {'status': 'confirmed'}),
                                                       ('shadow2', 'approved', {'status': 'rejected'}),
                                                       ('cut2', 'approved', {'status': 'needs_adjustment'}),
                                                       ('join1', 'rejected', {'status': 'superseded'})])
def test_item_and_outline_disagreeing_is_a_listed_conflict(pushed, key, item_status, overrides):
    repo, server, ids = pushed
    server.edit(ids[key], _both(item_status, **overrides))
    result = _pull(repo, server)
    decisions = json.loads(Path(result['files']['decisions']).read_text())
    assert [d['word'] for d in decisions['decisions']] == [None] and result['answers'] == []
    assert json.loads(Path(result['files']['previews']).read_text())['requests'] == []
    assert [(c['itemId'], c['itemStatus'], c['objectStatus']) for c in result['conflicts']] == [(ids[key], item_status, overrides['status'])]
    assert decisions['conflicts'] == result['conflicts'] and decisions['summary'][f"{key.rstrip('0123456789')}:conflict"] == 1
    assert f"# conflict: {ids[key]}: item Review status {item_status.title()}" in Path(result['files']['answers']).read_text()
    status = fr.status(root=repo, directory='mirror', out='out')
    assert [c['itemId'] for c in status['conflicts']] == [ids[key]]


def test_item_approved_on_an_edited_outline_is_a_conflict(pushed):
    repo, server, ids = pushed

    def change(doc):
        mark = next(m for m in doc['annotations'] if m['id'] == 'new-tree0001')
        mark['geometry']['coordinates'][0][0] = [0.0, 0.0]
        mark['geometry']['coordinates'][0][-1] = [0.0, 0.0]
        mark['properties']['addr']['overrides'] = {'status': 'needs_adjustment'}
        doc['status'] = 'approved'
    server.edit(ids['shadow2'], change)
    result = _pull(repo, server)
    assert result['answers'] == [] and 'was edited' in result['conflicts'][0]['why']


def test_veg_item_status_alone_needs_words(pushed):
    repo, server, ids = pushed
    server.edit(ids['veg'], _item_status('approved'))
    result = _pull(repo, server)
    assert json.loads(Path(result['files']['veg']).read_text())['decisions'] == [] and result['answers'] == []
    [needs] = result['needs_words']
    assert needs['itemId'] == ids['veg'] and needs['itemStatus'] == 'approved' and needs['untypedLines'] == '10-11'
    assert 'with no per-outline marks: nothing stored' in needs['why'] and 'per-outline types' in needs['why'] and 'notes line' in needs['why']
    assert '# needs words: ' + ids['veg'] in Path(result['files']['answers']).read_text()
    assert [n['itemId'] for n in fr.status(root=repo, directory='mirror', out='out')['needs_words']] == [ids['veg']]
    # Next to a per-outline mark: Approved agrees (the mark is read); Rejected contradicts the typed mark (a listed conflict, no
    # type stored). Either way the outline still without a word is named.

    def mark_and(status):
        def change(doc):
            _set('veg00001', status='confirmed')(doc)
            doc['status'] = status
        return change
    server.edit(ids['veg'], mark_and('approved'))
    result = _pull(repo, server)
    assert result['answers'] == ['veg: grass: 10'] and [n['untypedLines'] for n in result['needs_words']] == ['11']
    veg = json.loads(Path(result['files']['veg']).read_text())['decisions']
    assert [(d['object'], d['vegetation_type']) for d in veg] == [('veg00001', 'grass')] and not result['conflicts']
    server.edit(ids['veg'], mark_and('rejected'))
    result = _pull(repo, server)
    assert result['answers'] == [] and json.loads(Path(result['files']['veg']).read_text())['decisions'] == []
    [conflict] = result['conflicts']
    assert conflict['itemStatus'] == 'rejected' and 'grass 1; lines 10' in conflict['why']
    assert [n['untypedLines'] for n in result['needs_words']] == ['11'] and 'held as a conflict' in result['needs_words'][0]['why']


def test_changed_but_unread_items_are_listed(pushed):
    repo, server, ids = pushed
    server.edit(ids['round51'], _item_status('in_review'))
    server.edit(ids['veg'], _item_status('in_review', notes='# looked at the hedges\n'))
    result = _pull(repo, server)
    unread = {u['itemId']: u for u in result['unread']}
    assert set(unread) == {ids['round51'], ids['veg']} and result['answers'] == []
    assert unread[ids['veg']]['changed'] == ['item status unreviewed -> in_review', 'item notes']
    assert unread[ids['round51']]['changed'] == ['item status unreviewed -> in_review'] and 'Status is not Approved' in unread[ids['round51']]['why']
    assert result['summary']['round5:unread'] == 1
    assert '# unread: ' + ids['round51'] in Path(result['files']['answers']).read_text()
    assert ids['round51'] in [u['itemId'] for u in fr.status(root=repo, directory='mirror', out='out')['unread']]


def test_actor_is_recorded_and_other_actors_are_not_credited(pushed):
    repo, server, ids = pushed
    server.edit(ids['shadow2'], _set('new-tree0001', status='confirmed'))
    server.edit(ids['cut2'], _set('new-bldg0001', status='confirmed'), actor='agent-bot')
    server.edit(ids['join1'], _set('join-feat000011112222', status='confirmed'), record=False)
    result = _pull(repo, server)
    decisions = json.loads(Path(result['files']['decisions']).read_text())
    shadow = next(d for d in decisions['decisions'] if d['family'] == 'shadow')
    assert shadow['word'] == 'right' and shadow['fieldwork']['actor'] == 'local-dev' and shadow['fieldwork']['changeActors'] == ['local-dev']
    assert 'shared actor' in decisions['attribution']
    unread = {u['itemId']: u['why'] for u in result['unread']}
    assert 'changed by agent-bot' in unread[ids['cut2']] and 'no change record' in unread[ids['join1']]
    assert result['answers'] == ['shadows: right: 2']


def test_held_cut_lines_leave_the_answer_line():
    decisions = [{'family': 'cut', 'line': n, 'word': 'right', 'sceneId': 's', 'objectIds': [o]}
                 for n, o in ((5, 'a'), (31, 'b'), (40, 'b'), (72, 'c'), (2, 'd'), (3, 'd'))]
    resolution = fr.resolve(decisions, {'cuts': {'whole_other': list(range(66, 117))}})
    assert fr.final_answers(decisions, resolution) == ['cuts: right: 2, 5']
    assert [h.get('lines') or [h['line']] for h in resolution['cuts']['held']] == [[72], [31, 40]]
    assert [r['line'] for r in resolution['cuts']['resolved']] == [3]
    only_held = [d for d in decisions if d['line'] in (31, 40, 72)]
    assert fr.final_answers(only_held, fr.resolve(only_held, {'cuts': {'whole_other': [72]}})) == []


def test_several_right_lines_on_one_outline_leave_the_pulled_answer(repo):
    items = json.loads((repo / 'items.json').read_text())
    for cut, n in zip(items['cut'][:2], (31, 40), strict=True):
        cut.update(line=n, proposal_id=f'{n:02d}cdef0123456789', same_outline_as=[m for m in (31, 40) if m != n])
    (repo / 'items.json').write_text(json.dumps(items))
    staged = _stage(repo)
    server = FakeFieldwork()
    fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    for (k, _), doc in list(server.docs.items()):
        if k == 'item' and doc['metadata']['addr']['family'] == 'cut' and doc['metadata']['addr']['line'] in (31, 40):
            server.edit(doc['id'], _set('new-bldg0001', status='confirmed'))
    result = _pull(repo, server)
    assert result['answers'] == [] and result['resolution']['cuts']['held'][0]['lines'] == [31, 40]
    assert '# held: ' in Path(result['files']['answers']).read_text()


def test_guard_write_allows_only_the_push_creates(monkeypatch):
    item = {'id': 'r1008-x', 'projectId': 'mooroolbark-review'}
    refused = [('SaveItem', {'item': item, 'expectedRevision': 2}), ('SaveItem', {'item': item, 'expectedRevision': False}),
               ('SaveItem', {'item': {**item, 'projectId': 'mooroolbark'}, 'expectedRevision': 0}),
               ('DeleteItem', {'id': 'r1008-x', 'expectedRevision': 2}), ('SaveAnnotation', {'itemId': 'r1008-x', 'expectedRevision': 2}),
               ('DeleteAnnotation', {'itemId': 'r1008-x', 'id': 'new-x', 'expectedRevision': 2}),
               ('SaveLabel', {'label': {'id': 'l', 'projectId': 'mooroolbark-review'}, 'expectedRevision': 1})]
    sent = []
    monkeypatch.setattr(fr.Client, 'request', lambda self, *a, **k: sent.append(a) or (200, {}, b'{}'))
    client = fr.Client('http://127.0.0.1:1')
    for method, data in refused:
        with pytest.raises(fr.ReviewError, match='agents never edit a review project|refusing project'):
            client.rpc(method, data)
    assert sent == []
    client.rpc('SaveItem', {'item': item, 'expectedRevision': 0})
    client.rpc('ListItems', {'projectId': 'mooroolbark-review'})
    assert len(sent) == 2


def test_client_names_itself_so_cloudflare_does_not_answer_1010(monkeypatch):
    seen = []

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{}'

    monkeypatch.setattr(fr.urllib.request, 'urlopen', lambda request, timeout=None: seen.append(request) or Response())
    client = fr.Client('http://127.0.0.1:1')
    client.rpc('GetInfo', {})
    client.head_asset('0' * 64)
    client.request('GET', '/x', headers={'User-Agent': 'caller-choice'})
    agents = [r.get_header('User-agent') for r in seen]
    assert agents[:2] == [fr.USER_AGENT, fr.USER_AGENT] and agents[2] == 'caller-choice'
    assert all('python-urllib' not in a.lower() for a in agents)


def test_live_gate_normalises_the_hostname(monkeypatch):
    monkeypatch.delenv('FIELDWORK_PUSH_ALLOW_LIVE', raising=False)
    monkeypatch.setenv('ANNOTATION_TOKEN', 'not-the-live-token')
    for origin in ('http://fieldwork-annotations.ben-ebsworth.workers.dev.', 'HTTPS://Fieldwork-Annotations.Ben-Ebsworth.workers.dev.:8443/x',
                   'fieldwork-annotations.ben-ebsworth.workers.dev..'):
        with pytest.raises(fr.ReviewError, match='live'):
            fr.guard_origin(origin, dry_run=False)
    assert fr.guard_origin('https://example.org', dry_run=False) == 'not-the-live-token'


# ------------------------------------------------------------- skeptic fixes round 2 (9 Oct): Inspector controls, veg, live gate

def _ui(mark_id, **overrides):
    """An Object feedback edit the way the addr-scene Inspector saves it: overrides on properties.addr (roof_annotation nulled on a
    non-building object), item status in_review."""
    return _set(mark_id, roof_annotation=None, **overrides)


def _decisions(result):
    return json.loads(Path(result['files']['decisions']).read_text())['decisions']


@pytest.mark.parametrize('key,mark,item_status,overrides,ticked', [
    ('shadow2', 'new-tree0001', None, {'status': 'confirmed', 'review_issues': ['outline']}, 'Review issues ticked: outline'),
    ('shadow2', 'new-tree0001', 'approved', {'review_issues': ['placement']}, 'Review issues ticked: placement'),
    ('shadow2', 'cur-tree0001', 'approved', {'review_issues': ['height']}, 'Review issues ticked: height'),
    ('shadow2', 'new-tree0001', None, {'status': 'confirmed', 'visibility': 'occluded'}, 'Visibility set to occluded'),
    # 'Geometry reviewed' next to Approved, on CURRENT or on a PROPOSED outline left candidate (not the decision bar's write).
    ('cut2', 'cur-bldg0001', 'approved', {'geometry_reviewed': True}, "'Geometry reviewed in this layer' ticked"),
    ('shadow2', 'new-tree0001', 'approved', {'geometry_reviewed': True}, "'Geometry reviewed in this layer' ticked"),
    # The decision bar's Right next to a ticked Review issue or a Visibility change is still a conflict.
    ('cut2', 'new-bldg0001', None, {'status': 'confirmed', 'geometry_reviewed': True, 'review_issues': ['outline']},
     'Review issues ticked: outline'),
    ('join1', 'join-feat000011112222', None, {'status': 'confirmed', 'geometry_reviewed': True, 'visibility': 'occluded'},
     'Visibility set to occluded'),
    ('join1', 'join-feat000011112222', 'approved', {'status': 'confirmed', 'review_issues': ['instances', 'shape_3d']},
     'Review issues ticked: instances, shape_3d'),
])
def test_inspector_flag_next_to_confirmed_or_approved_is_a_conflict(pushed, key, mark, item_status, overrides, ticked):
    repo, server, ids = pushed

    def change(doc):
        _ui(mark, **overrides)(doc)
        if item_status:
            doc['status'] = item_status
    server.edit(ids[key], change)
    result = _pull(repo, server)
    [decision] = _decisions(result)
    assert decision['word'] is None and result['answers'] == []
    assert json.loads(Path(result['files']['previews']).read_text())['requests'] == []
    [conflict] = result['conflicts']
    assert conflict['itemId'] == ids[key] and any(ticked in f and mark in f for f in conflict['flags']) and ticked in conflict['why']
    assert 'no decision recorded' in conflict['why'] and f'# conflict: {ids[key]}' in Path(result['files']['answers']).read_text()
    assert [c['itemId'] for c in fr.status(root=repo, directory='mirror', out='out')['conflicts']] == [ids[key]]
    if key == 'join1':
        assert json.loads(Path(result['files']['joins']).read_text())['decisions'][0]['flags'] == conflict['flags']


@pytest.mark.parametrize('overrides,notes,ticked', [({'review_issues': ['outline']}, None, 'Review issues ticked: outline'),
                                                    ({'visibility': 'occluded'}, '\nlooks off\n', 'Visibility set to occluded'),
                                                    ({'geometry_reviewed': True}, None, "'Geometry reviewed in this layer' ticked")])
def test_inspector_flag_on_its_own_is_listed_unread(pushed, overrides, notes, ticked):
    repo, server, ids = pushed

    def change(doc):
        _ui('new-tree0001', **overrides)(doc)
        if notes:
            doc['notes'] += notes
    server.edit(ids['shadow2'], change)
    result = _pull(repo, server)
    [unread] = result['unread']
    assert unread['itemId'] == ids['shadow2'] and ticked in unread['why'] and any(ticked in c for c in unread['changed'])
    assert result['answers'] == [] and not result['conflicts'] and result['summary'] == {'cuts_held': 0, 'shadow:unread': 1}
    assert f'# unread: {ids["shadow2"]}' in Path(result['files']['answers']).read_text()
    if notes:
        assert _decisions(result)[0]['notes'] == 'looks off'


def test_inspector_flag_next_to_wrong_keeps_the_word_and_lists_the_flag(pushed):
    repo, server, ids = pushed
    server.edit(ids['shadow2'], _ui('new-tree0001', status='rejected', review_issues=['outline']))
    result = _pull(repo, server)
    [decision] = _decisions(result)
    assert decision['word'] == 'wrong' and decision['flags'] == ['PROPOSED new-tree0001: Review issues ticked: outline']
    assert result['answers'] == ['shadows: wrong: 2'] and not result['conflicts']


def test_unticked_issues_and_the_nulled_roof_field_are_not_flags(pushed):
    repo, server, ids = pushed
    server.edit(ids['shadow2'], _ui('new-tree0001', status='confirmed', review_issues=[]))
    result = _pull(repo, server)
    assert result['answers'] == ['shadows: right: 2'] and not result['conflicts'] and 'flags' not in _decisions(result)[0]


def test_other_unread_change_on_the_proposed_outline_is_an_exception(pushed):
    repo, server, ids = pushed
    server.edit(ids['cut2'], _ui('new-bldg0001', status='confirmed', model={'height_m': 9}))
    result = _pull(repo, server)
    [decision] = _decisions(result)
    assert decision['word'] is None and 'also changed model' in decision['exceptions'][0] and result['answers'] == []
    assert [e['itemId'] for e in result['exceptions']] == [ids['cut2']]
    assert f'# exception: {ids["cut2"]} line 2: ' in Path(result['files']['answers']).read_text()


def test_veg_flag_per_outline(pushed):
    repo, server, ids = pushed
    # A confirmed outline is read; the other outline's flag alone is listed unread (never absent).
    server.edit(ids['veg'], _ui('veg00001', status='confirmed'))
    server.edit(ids['veg'], _ui('veg00002', review_issues=['class']))
    result = _pull(repo, server)
    assert result['answers'] == ['veg: grass: 10']
    [unread] = result['unread']
    assert unread['line'] == 11 and 'Review issues ticked: class' in unread['why'] and not result['conflicts']
    # geometry_reviewed + visibility alone: listed too.
    server.edit(ids['veg'], _ui('veg00002', review_issues=[], geometry_reviewed=True, visibility='occluded'))
    unread = _pull(repo, server)['unread']
    assert [u['line'] for u in unread] == [11] and 'Visibility set to occluded' in unread[0]['why']
    # A typed outline (confirmed, or a Label type word) with an issue ticked stores nothing and is a listed conflict.
    server.edit(ids['veg'], _ui('veg00001', review_issues=['class']))
    server.edit(ids['veg'], _ui('veg00002', label='shrub', review_issues=['outline'], geometry_reviewed=False, visibility=None))
    result = _pull(repo, server)
    assert result['answers'] == [] and json.loads(Path(result['files']['veg']).read_text())['decisions'] == []
    assert sorted(c['line'] for c in result['conflicts']) == [10, 11]
    assert all('nothing stored' in c['why'] for c in result['conflicts'])


def test_veg_item_rejected_with_every_outline_typed_is_a_conflict(pushed):
    repo, server, ids = pushed

    def change(doc):
        for mark in doc['annotations']:
            _ui(mark['id'], label='grass')(doc)
        doc['status'] = 'rejected'
    server.edit(ids['veg'], change)
    result = _pull(repo, server)
    assert result['answers'] == [] and json.loads(Path(result['files']['veg']).read_text())['decisions'] == []
    [conflict] = result['conflicts']
    assert conflict['itemStatus'] == 'rejected' and 'grass 2; lines 10-11' in conflict['why'] and not result['needs_words']
    assert {d.get('typedWord') for d in _decisions(result) if d.get('objectId')} == {'grass'}
    assert result['summary'] == {'cuts_held': 0, 'veg:conflict': 1, 'veg:withheld': 2}


def test_veg_item_approved_with_every_outline_typed_is_recorded(pushed):
    repo, server, ids = pushed

    def change(doc):
        for mark in doc['annotations']:
            _ui(mark['id'], label='hedge')(doc)
        doc['status'] = 'approved'
    server.edit(ids['veg'], change)
    result = _pull(repo, server)
    assert result['answers'] == ['veg: hedge: 10-11'] and not result['conflicts'] and not result['needs_words']
    assert {d.get('itemStatus') for d in _decisions(result)} == {'approved'}


LIVE_HOST = 'fieldwork-annotations.ben-ebsworth.workers.dev'
UNICODE_DOTS = ('\u3002', '\uff0e', '\uff61')  # ideographic, fullwidth and halfwidth full stops: IDNA folds them to '.'
FULLWIDTH_F = '\uff26'


@pytest.mark.parametrize('origin', [
    f'URL:https://{LIVE_HOST}', f'<URL:https://{LIVE_HOST}>', f'https://{LIVE_HOST}%2e', f'https://{LIVE_HOST}%2E',
    'https://fieldwork-annotations%2eben-ebsworth.workers.dev', *('https://' + LIVE_HOST.replace('.', dot) for dot in UNICODE_DOTS),
    'https://' + LIVE_HOST.upper().replace('F', FULLWIDTH_F, 1), f'https://{LIVE_HOST} ', f'https://{LIVE_HOST}\t', f'//{LIVE_HOST}',
    f'https://u:p@{LIVE_HOST}', f'https://{LIVE_HOST}.', f'HTTPS://{LIVE_HOST}', f'https://{LIVE_HOST}/x', f'ftp://{LIVE_HOST}',
    LIVE_HOST, f'https://{LIVE_HOST}:443',
])
def test_live_gate_refuses_every_spelling_urllib_sends_to_the_live_host(monkeypatch, origin):
    monkeypatch.delenv('FIELDWORK_PUSH_ALLOW_LIVE', raising=False)
    monkeypatch.setenv('ANNOTATION_TOKEN', 'not-the-live-token')
    with pytest.raises(fr.ReviewError, match='live|plain'):
        fr.guard_origin(origin, dry_run=False)


def test_request_host_matches_urllib(monkeypatch):
    for origin in (f'URL:https://{LIVE_HOST}', f'https://{LIVE_HOST}%2e', 'https://' + LIVE_HOST.replace('.', UNICODE_DOTS[0]),
                   'https://' + LIVE_HOST.replace('.', UNICODE_DOTS[1]) + '.:8443', 'https://' + LIVE_HOST.upper().replace('F', FULLWIDTH_F, 1)):
        assert fr.request_host(origin) == LIVE_HOST, origin
    assert fr.request_host('http://127.0.0.1:18131') == '127.0.0.1' and fr.request_host('http://[::1]:1') == '::1'
    assert fr.request_host('not a url') is None


def test_live_gate_needs_a_plain_origin_even_when_live_is_allowed(monkeypatch):
    monkeypatch.setenv('FIELDWORK_PUSH_ALLOW_LIVE', '1')
    monkeypatch.setenv('ANNOTATION_TOKEN', 'not-the-live-token')
    for origin in (f'URL:https://{LIVE_HOST}', f'https://{LIVE_HOST}%2e', 'URL:http://127.0.0.1:18131', 'http://127%2e0.0.1:1',
                   'http://' + '127.0.0.1'.replace('.', UNICODE_DOTS[0]) + ':1', 'http://localhost:99999'):
        with pytest.raises(fr.ReviewError, match='plain'):
            fr.guard_origin(origin, dry_run=False)
    assert fr.guard_origin(fr.DEFAULT_REMOTE_URL, dry_run=False) == 'not-the-live-token'
    assert fr.guard_origin('http://127.0.0.1:18131', dry_run=False) is None
    assert fr.guard_origin('http://localhost:3008/', dry_run=False) is None
    assert fr.guard_origin('http://[::1]:3008', dry_run=False) is None
    assert fr.guard_origin('https://example.org', dry_run=False) == 'not-the-live-token'


# ---------------------------------------------- 9 Oct UI: decision bar, vegetation type picker, project id spelling guards

# What the outline decision bar writes (AddrSceneEditor through updateSceneObject): Right / Partly / Wrong.
BAR = {'right': {'status': 'confirmed', 'geometry_reviewed': True}, 'partly': {'status': 'needs_adjustment', 'geometry_reviewed': False},
       'wrong': {'status': 'rejected', 'geometry_reviewed': False}}


def _scene_edit(server, mark_id, **fields):
    """One save the way updateSceneObject (src/lib/annotations/addr-scene.ts) writes it: next = the frame object with the current
    overrides and ``fields`` applied (roof fields cleared and roof_annotation nulled on a non-building); overrides = every field of
    next that differs from the frame object (JSON compare; a field the frame lacks counts as different); removedFields = fields next
    drops; properties.addr keeps the pushed keys and gains objectId / overrides / removedFields / fieldworkReview; item status
    in_review."""
    def change(doc):
        frame = json.loads(server.assets[doc['source']['model']['sha256']])
        original = next(o for o in frame['objects'] if o['id'] == mark_id)
        mark = next(m for m in doc['annotations'] if m['id'] == mark_id)
        addr = mark['properties']['addr']
        nxt = {**original, **(addr.get('overrides') or {}), **fields}
        if nxt['kind'] != 'building':
            for key in ('roof_feedback', 'roof_line_edits', 'roof_parts', 'roof_lines'):
                nxt.pop(key, None)
            nxt['roof_annotation'] = None
        same = lambda k: k in original and json.dumps(nxt[k], sort_keys=True) == json.dumps(original[k], sort_keys=True)  # noqa: E731
        overrides = {k: v for k, v in nxt.items() if not same(k)}
        removed = sorted(k for k in original if k not in nxt)
        mark['properties']['addr'] = {**addr, 'objectId': mark_id, 'overrides': overrides, 'removedFields': removed,
                                      'fieldworkReview': {'origin': 'fieldwork-user-feedback', 'sourceModelSignature': frame['model_signature']}}
        doc['status'] = 'in_review'
    return change


def _overrides_of(server, item_id, mark_id):
    mark = next(m for m in server.docs[('item', item_id)]['annotations'] if m['id'] == mark_id)
    return mark['properties']['addr']['overrides']


@pytest.mark.parametrize('key,mark,choice,answer', [
    ('shadow2', 'new-tree0001', 'right', 'shadows: right: 2'), ('shadow2', 'new-tree0001', 'partly', 'shadows: partly: 2'),
    ('shadow2', 'new-tree0001', 'wrong', 'shadows: wrong: 2'), ('cut2', 'new-bldg0001', 'right', 'cuts: right: 2'),
    ('cut2', 'new-bldg0001', 'partly', 'cuts: unclear: 2'), ('join1', 'join-feat000011112222', 'right', 'joins: right: 1'),
    ('join1', 'join-feat000011112222', 'wrong', 'joins: wrong: 1'),
])
def test_decision_bar_answers_the_proposed_outline(pushed, key, mark, choice, answer):
    repo, server, ids = pushed
    server.edit(ids[key], _scene_edit(server, mark, **BAR[choice]))
    written = _overrides_of(server, ids[key], mark)
    # Right writes geometry_reviewed true; Partly / Wrong write false, which equals the frame's own false and so is no override.
    assert written['status'] == BAR[choice]['status'] and written.get('geometry_reviewed') is (True if choice == 'right' else None)
    result = _pull(repo, server)
    [decision] = _decisions(result)
    assert result['answers'] == [answer] and not result['conflicts'] and not result['unread'] and not result['exceptions']
    assert 'flags' not in decision and decision['status'] == BAR[choice]['status']
    assert decision.get('geometryReviewed') is (True if choice == 'right' else None)
    if answer == 'shadows: right: 2':
        [request] = json.loads(Path(result['files']['previews']).read_text())['requests']
        assert request['object'] == 'tree0001' and 'PROPOSED trim line 2 set to confirmed' in request['sentence']


def test_decision_bar_explicit_false_is_no_flag_but_a_manual_tick_is(pushed):
    repo, server, ids = pushed
    # The bar's own pair written explicitly (geometry_reviewed false next to rejected / needs_adjustment): the answer, no flag.
    server.edit(ids['shadow2'], _ui('new-tree0001', status='rejected', geometry_reviewed=False))
    server.edit(ids['cut2'], _ui('new-bldg0001', status='needs_adjustment', geometry_reviewed=False))
    result = _pull(repo, server)
    assert result['answers'] == ['shadows: wrong: 2', 'cuts: unclear: 2'] and not result['conflicts']
    assert all('flags' not in d for d in _decisions(result))
    # 'Geometry reviewed' ticked next to Partly is not the bar's write: the word is read and the tick listed as a flag.
    server.edit(ids['shadow2'], _ui('new-tree0001', status='needs_adjustment', geometry_reviewed=True))
    result = _pull(repo, server)
    shadow = next(d for d in _decisions(result) if d['family'] == 'shadow')
    assert shadow['word'] == 'partly' and shadow['flags'] == ["PROPOSED new-tree0001: 'Geometry reviewed in this layer' ticked"]
    assert 'shadows: partly: 2' in result['answers'] and not result['conflicts']


def test_decision_bar_with_item_status(pushed):
    repo, server, ids = pushed

    def right_and(status):
        def change(doc):
            _scene_edit(server, 'new-tree0001', **BAR['right'])(doc)
            doc['status'] = status
        return change
    server.edit(ids['shadow2'], right_and('approved'))
    [decision] = _decisions(_pull(repo, server))
    assert (decision['word'], decision['how'], decision['geometryReviewed']) == ('right', 'object status and item status', True)
    server.edit(ids['shadow2'], right_and('rejected'))
    result = _pull(repo, server)
    assert result['answers'] == [] and 'item Review status Rejected but the PROPOSED outline is confirmed' in result['conflicts'][0]['why']


def test_geometry_reviewed_on_a_non_proposal_outline_stays_a_listed_flag(pushed):
    repo, server, ids = pushed
    server.edit(ids['shadow2'], _scene_edit(server, 'cur-tree0001', geometry_reviewed=True))
    result = _pull(repo, server)
    [unread] = result['unread']
    assert "outline cur-tree0001: 'Geometry reviewed in this layer' ticked" in unread['why'] and result['answers'] == []
    # Next to the bar's Right on PROPOSED it is still listed, as a conflict (no decision).
    server.edit(ids['shadow2'], _scene_edit(server, 'new-tree0001', **BAR['right']))
    result = _pull(repo, server)
    [conflict] = result['conflicts']
    assert conflict['flags'] == ["outline cur-tree0001: 'Geometry reviewed in this layer' ticked"] and result['answers'] == []


def test_decision_bar_on_vegetation_outlines(pushed):
    repo, server, ids = pushed
    server.edit(ids['veg'], _scene_edit(server, 'veg00001', **BAR['right']))
    server.edit(ids['veg'], _scene_edit(server, 'veg00002', **BAR['right']))
    result = _pull(repo, server)
    decisions = {d['objectId']: d for d in _decisions(result) if d.get('objectId')}
    assert result['answers'] == ['veg: grass: 10'] and not result['conflicts']
    assert decisions['veg00001']['geometryReviewed'] is True and 'flags' not in decisions['veg00001']
    assert decisions['veg00002']['typeNeeded'] and decisions['veg00002']['word'] is None
    server.edit(ids['veg'], _scene_edit(server, 'veg00001', **BAR['wrong']))
    server.edit(ids['veg'], _scene_edit(server, 'veg00002', **BAR['partly']))
    assert _pull(repo, server)['answers'] == ['veg: wrong: 10 / unclear: 11']


@pytest.mark.parametrize('choice', ['wrong', 'partly'])
@pytest.mark.parametrize('typed', [{'label': 'tree · 0001'}, {'vegetation_type': 'tree'}])
def test_veg_type_then_wrong_or_partly_is_a_conflict(pushed, choice, typed):
    # 'Pick Tree, then press X' (or Shift+Enter): the type and the status contradict each other, so neither is stored.
    repo, server, ids = pushed
    server.edit(ids['veg'], _scene_edit(server, 'veg00001', **typed, **BAR[choice]))
    result = _pull(repo, server)
    assert result['answers'] == [] and json.loads(Path(result['files']['veg']).read_text())['decisions'] == []
    [conflict] = result['conflicts']
    assert conflict['itemId'] == ids['veg'] and 'nothing stored' in conflict['why']
    assert ('rejected (Wrong)' if choice == 'wrong' else 'needs_adjustment (Not sure)') in conflict['why']


# What the vegetation type picker writes: properties.addr.overrides.vegetation_type (or a type word in the Label).

def test_vegetation_type_is_read_like_a_label_word(pushed):
    repo, server, ids = pushed
    server.edit(ids['veg'], _scene_edit(server, 'veg00002', vegetation_type='shrub'))
    assert _overrides_of(server, ids['veg'], 'veg00002') == {'vegetation_type': 'shrub', 'roof_annotation': None}
    result = _pull(repo, server)
    [decision] = [d for d in _decisions(result) if d.get('objectId')]
    assert (decision['word'], decision['how'], decision['vegetationType']) == ('shrub', 'vegetation_type', 'shrub')
    assert result['answers'] == ['veg: shrub: 11'] and not result['conflicts']
    [veg] = json.loads(Path(result['files']['veg']).read_text())['decisions']
    assert (veg['object'], veg['vegetation_type'], veg['fieldwork']['how']) == ('veg00002', 'shrub', 'vegetation_type')
    assert "veg line 11 outline vegetation type set to 'shrub'" in veg['sentence']
    # Next to the same Label word: one answer. The type wins over a plain confirm, as a Label word does.
    server.edit(ids['veg'], _scene_edit(server, 'veg00002', label='shrub'))
    server.edit(ids['veg'], _scene_edit(server, 'veg00001', vegetation_type='hedge', **BAR['right']))
    result = _pull(repo, server)
    decisions = {d['objectId']: d for d in _decisions(result) if d.get('objectId')}
    assert decisions['veg00002']['how'] == 'label and vegetation_type' and decisions['veg00001']['word'] == 'hedge'
    assert result['answers'] == ['veg: hedge: 10 / shrub: 11'] and not result['conflicts']


@pytest.mark.parametrize('fields,listed,why', [
    ({'vegetation_type': 'tree', 'label': 'grass'}, 'conflicts', "Label typed 'grass' but vegetation type 'tree'"),
    ({'vegetation_type': 'bush'}, 'exceptions', "vegetation type 'bush' not understood"),
    ({'vegetation_type': 'tree-or-shrub'}, 'exceptions', "vegetation type 'tree-or-shrub' not understood"),
    ({'vegetation_type': 'tree', 'review_issues': ['class']}, 'conflicts', "vegetation type 'tree' but Review issues ticked: class"),
    ({'vegetation_type': 'grass', 'kind': 'building', 'geometry_reviewed': False, 'status': 'needs_adjustment'}, 'exceptions',
     'outline or class changed here'),
])
def test_vegetation_type_conflicts_and_exceptions_store_nothing(pushed, fields, listed, why):
    repo, server, ids = pushed
    server.edit(ids['veg'], _scene_edit(server, 'veg00001', **fields))
    result = _pull(repo, server)
    assert result['answers'] == [] and json.loads(Path(result['files']['veg']).read_text())['decisions'] == []
    [entry] = result[listed]
    assert entry['itemId'] == ids['veg'] and why in (entry['why'] if listed == 'conflicts' else '; '.join(entry['why']))


def test_vegetation_type_next_to_item_rejected_is_the_item_conflict(pushed):
    repo, server, ids = pushed

    def change(doc):
        _scene_edit(server, 'veg00001', vegetation_type='grass')(doc)
        doc['status'] = 'rejected'
    server.edit(ids['veg'], change)
    result = _pull(repo, server)
    assert result['answers'] == [] and 'grass 1; lines 10' in result['conflicts'][0]['why']
    assert [d.get('typedWord') for d in _decisions(result) if d.get('objectId')] == ['grass']


def test_vegetation_type_outside_a_vegetation_item_is_listed_not_stored(pushed):
    repo, server, ids = pushed
    # The type picker on the PROPOSED tree trim (kind tree) next to the bar's Right: the answer is read, the type listed.
    server.edit(ids['shadow2'], _scene_edit(server, 'new-tree0001', vegetation_type='tree', **BAR['right']))
    # On CURRENT (kind other) next to a confirmed cut: listed too, and the cut answer is still read.
    server.edit(ids['cut2'], _scene_edit(server, 'cur-bldg0001', vegetation_type='shrub'))
    server.edit(ids['cut2'], _scene_edit(server, 'new-bldg0001', **BAR['right']))
    # On its own (no status): listed unread, nothing recorded.
    server.edit(ids['join1'], _scene_edit(server, 'join-feat000011112222', vegetation_type='hedge'))
    result = _pull(repo, server)
    assert result['answers'] == ['shadows: right: 2', 'cuts: right: 2']
    assert json.loads(Path(result['files']['veg']).read_text())['decisions'] == []
    listed = {n['itemId']: n for n in result['not_stored']}
    assert set(listed) == {ids['shadow2'], ids['cut2'], ids['join1']}
    assert listed[ids['shadow2']]['word'] == 'right' and "PROPOSED new-tree0001: vegetation type 'tree'" in listed[ids['shadow2']]['why'][0]
    assert "outline cur-bldg0001: vegetation type 'shrub'" in listed[ids['cut2']]['why'][0]
    [unread] = result['unread']
    assert unread['itemId'] == ids['join1'] and "vegetation type 'hedge'" in unread['why'] and not result['exceptions']
    answers = Path(result['files']['answers']).read_text()
    assert f'# not stored: {ids["shadow2"]} line 2: ' in answers
    assert [n['itemId'] for n in fr.status(root=repo, directory='mirror', out='out')['not_stored']] == sorted(listed)


# Project id spellings never bypass the guards.

@pytest.mark.parametrize('spelled,normal', [
    ('mooroolbark-review', 'mooroolbark-review'), ('MOOROOLBARK-REVIEW', 'mooroolbark-review'), ('Mooroolbark-Review', 'mooroolbark-review'),
    ('mooroolbark-review ', 'mooroolbark-review'), ('\tmooroolbark-review\n', 'mooroolbark-review'),
    ('mooroolbark－review', 'mooroolbark-review'), ('Ｍｏｏｒｏｏｌｂａｒｋ-review', 'mooroolbark-review'),
    ('mooroolbark‐review', 'mooroolbark-review'), ('mooroolbark−review', 'mooroolbark-review'),
    ('mooroolbark-review​', 'mooroolbark-review'), ('mooroolbark-review ', 'mooroolbark-review'),
    ('mooroolbark-review　', 'mooroolbark-review'), ('Mooroolbark', 'mooroolbark'), ('Kelvinſ', 'kelvins'),
])
def test_normalize_project_id(spelled, normal):
    assert fr.normalize_project_id(spelled) == normal
    assert fr.is_review_project(spelled) is normal.endswith('-review')


@pytest.mark.parametrize('project', ['Mooroolbark-Review', 'MOOROOLBARK-REVIEW', 'mooroolbark-review ', ' mooroolbark-review',
                                     'mooroolbark－review', 'mooroolbark-review​', 'MOOROOLBARK', 'Mooroolbark ', 'x-reviews'])
def test_push_and_pull_refuse_every_other_spelling_of_a_project(repo, project):
    with pytest.raises(fr.ReviewError, match='refusing project'):
        fr.push(root=repo, stage_dir='x', url='http://127.0.0.1:1', directory='d', project=project, client=FakeFieldwork())
    with pytest.raises(fr.ReviewError, match='refusing project'):
        fr.pull(root=repo, url='http://127.0.0.1:1', directory='d', project=project, client=FakeFieldwork())


def test_guard_write_refuses_spelled_projects_and_fields(monkeypatch):
    item = {'id': 'r1008-x', 'projectId': 'mooroolbark-review'}
    refused = [
        ('SaveItem', {'item': {**item, 'projectId': 'MOOROOLBARK-REVIEW'}, 'expectedRevision': 0}, 'refusing project'),
        ('SaveItem', {'item': {**item, 'projectId': 'mooroolbark-review '}, 'expectedRevision': 0}, 'refusing project'),
        ('SaveItem', {'item': {**item, 'projectId': 'mooroolbark－review'}, 'expectedRevision': 0}, 'refusing project'),
        ('SaveLabel', {'label': {'id': 'l', 'projectId': 'Mooroolbark-Review'}, 'expectedRevision': 0}, 'refusing project'),
        ('SaveProject', {'project': {'id': 'MOOROOLBARK'}, 'expectedRevision': 0}, 'refusing project'),
        # The server's decoder reads these keys as projectId / expectedRevision / item too.
        ('SaveItem', {'item': {**item, 'ProjectId': 'mooroolbark'}, 'expectedRevision': 0}, 'spelled differently'),
        ('SaveItem', {'item': {**item, 'PROJECTID': 'mooroolbark-review'}, 'expectedRevision': 0}, 'spelled differently'),
        ('SaveItem', {'item': item, 'expectedRevision': 0, 'ExpectedRevision': 3}, 'spelled differently'),
        ('SaveItem', {'item': item, 'expectedRevision': 0, 'expectedReviſion': 3}, 'spelled differently'),
        ('SaveItem', {'item': item, 'Item': {**item, 'projectId': 'mooroolbark'}, 'expectedRevision': 0}, 'spelled differently'),
        ('SaveProject', {'project': {'id': 'mooroolbark-review', 'ID': 'mooroolbark'}, 'expectedRevision': 0}, 'spelled differently'),
    ]
    sent = []
    monkeypatch.setattr(fr.Client, 'request', lambda self, *a, **k: sent.append(a) or (200, {}, b'{}'))
    client = fr.Client('http://127.0.0.1:1')
    for method, data, match in refused:
        with pytest.raises(fr.ReviewError, match=match):
            client.rpc(method, data)
    assert sent == []
    client.rpc('SaveItem', {'item': item, 'expectedRevision': 0})
    client.rpc('SaveLabel', {'label': {'id': 'mooroolbark-review-x', 'projectId': 'mooroolbark-review', 'name': 'x', 'color': '#ffffff'},
                             'expectedRevision': 0})
    assert len(sent) == 2


def test_type_picker_label_then_decision_bar(pushed):
    """The UI's picker (AddrSceneEditor pickType) writes the Label the pull reads, keeping the proposal's number
    (vegetationLabel('grass? · 0010', 'tree') == 'tree · 0010'), as a draft; the decision bar then saves it with its status pair."""
    repo, server, ids = pushed
    frame = json.loads(server.assets[server.docs[('item', ids['veg'])]['source']['model']['sha256']])
    assert {o['id']: o['label'] for o in frame['objects']} == {'veg00001': 'grass? · 0010', 'veg00002': 'tree or shrub? · 0011'}
    server.edit(ids['veg'], _scene_edit(server, 'veg00001', label='tree · 0010', **BAR['right']))
    server.edit(ids['veg'], _scene_edit(server, 'veg00002', label='shrub · 0011', **BAR['right']))
    assert _overrides_of(server, ids['veg'], 'veg00001') == {'label': 'tree · 0010', 'status': 'confirmed', 'geometry_reviewed': True,
                                                             'roof_annotation': None}
    result = _pull(repo, server)
    assert result['answers'] == ['veg: shrub: 11 / tree: 10'] and not result['conflicts'] and not result['unread']
    veg = json.loads(Path(result['files']['veg']).read_text())['decisions']
    assert sorted((d['object'], d['vegetation_type'], d['fieldwork']['how']) for d in veg) == [('veg00001', 'tree', 'label'),
                                                                                                ('veg00002', 'shrub', 'label')]


# ------------------------------------------------------------------------------------------------- corners (card 2026-10-10)

RING_B_BOX = [[300, 300], [380, 300], [380, 360], [300, 360]]


@pytest.fixture
def corner_pushed(repo):
    items = json.loads((repo / 'items.json').read_text())
    items['corner'] = [{'scene': SCENE, 'line': n, 'object_id': oid, 'kind': 'building', 'status_at_compute': 'candidate',
                        'current_ring': ring, 'current_corners': len(ring), 'proposed_ring': RING_B_BOX, 'proposed_corners': 4,
                        'iou': 0.95, 'method': 'rectangle', 'area_m2': 30.0, 'proposal_id': f'{n:02d}c0rner0123456'}
                       for n, oid, ring in ((1, 'bldg0001', RING_B), (2, 'bldg0002', RING_B2), (3, 'bldg0001', RING_B))]
    (repo / 'items.json').write_text(json.dumps(items))
    staged = _stage(repo, families=('corner',))
    assert staged['counts'] == {'corner': 3}
    server = FakeFieldwork()
    fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    ids = {doc['metadata']['addr']['line']: doc['id'] for (k, _), doc in server.docs.items() if k == 'item'}
    return repo, server, ids


def test_corner_items_carry_both_outlines_and_the_queue_tag(corner_pushed):
    _, server, ids = corner_pushed
    doc = server.docs[('item', ids[1])]
    assert doc['id'].startswith('r1010-6-corner-001-p') and fr.CORNER_TAG in doc['tags']
    marks = {m['id']: m for m in doc['annotations']}
    assert set(marks) == {'cur-bldg0001', 'new-bldg0001'}
    assert marks['new-bldg0001']['properties']['addr']['source'] == 'agent-proposal:corner-redraw-v1'
    assert marks['new-bldg0001']['properties']['addr']['status'] == 'candidate'


def test_corner_bar_answers_become_a_corners_line(corner_pushed):
    repo, server, ids = corner_pushed
    for line, choice in ((1, 'right'), (2, 'partly'), (3, 'wrong')):
        mark = f"new-{'bldg0002' if line == 2 else 'bldg0001'}"
        server.edit(ids[line], _scene_edit(server, mark, **BAR[choice]))
    result = _pull(repo, server)
    assert 'corners: right: 1 / partly: 2 / wrong: 3' in result['answers']
    assert not result['conflicts'] and not result['unread']
    words = {d['line']: d['word'] for d in _decisions(result) if d['family'] == 'corner'}
    assert words == {1: 'right', 2: 'partly', 3: 'wrong'}


# ------------------------------------------------------------------------------------------------- units (card 2026-10-12)

@pytest.fixture
def unit_pushed(repo):
    items = json.loads((repo / 'items.json').read_text())
    halves = [[[300, 300], [340, 300], [340, 360], [300, 360]], [[340, 300], [380, 300], [380, 360], [340, 360]]]
    items['unit'] = [{'scene': SCENE, 'line': n, 'object_id': 'bldg0001', 'kind': 'building', 'status_at_compute': 'candidate',
                      'current_ring': RING_B, 'proposed_ring': halves[n - 1], 'proposal_id': f'{n:02d}un1t0123456',
                      'why': 'small piece' if n == 2 else None,
                      'unit_of': {'line': 1, 'lot': str(n), 'plan': 'PS1', 'pfi': f'p{n}', 'index': n, 'of': 2}}
                     for n in (1, 2)]
    (repo / 'items.json').write_text(json.dumps(items))
    staged = _stage(repo, families=('unit',))
    assert staged['counts'] == {'unit': 2}
    server = FakeFieldwork()
    fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    ids = {doc['metadata']['addr']['line']: doc['id'] for (k, _), doc in server.docs.items() if k == 'item'}
    return repo, server, ids


def test_unit_items_propose_one_lot_bounded_unit_against_the_current_outline(unit_pushed):
    _, server, ids = unit_pushed
    doc = server.docs[('item', ids[1])]
    assert doc['id'].startswith('r1013-9-unit-001-p') and 'units-2026-10-13' in doc['tags']
    marks = {m['id']: m for m in doc['annotations']}
    assert set(marks) == {'cur-bldg0001', 'new-01un1t0123456'}
    assert [m for m in marks if marks[m]['properties']['addr']['source'].startswith('agent-proposal')] == ['new-01un1t0123456']
    assert 'small piece' in server.docs[('item', ids[2])]['title']


def test_unit_bar_answers_become_a_units_line(unit_pushed):
    repo, server, ids = unit_pushed
    server.edit(ids[1], _scene_edit(server, 'new-01un1t0123456', **BAR['right']))
    server.edit(ids[2], _scene_edit(server, 'new-02un1t0123456', **BAR['wrong']))
    result = _pull(repo, server)
    assert 'units: right: 1 / wrong: 2' in result['answers']
    assert not result['conflicts'] and not result['unread']


# ------------------------------------------------------------------------------------------------- lot bound (2026-10-13)

def test_lot_items_propose_the_clipped_outline_and_read_back_as_a_lots_line(repo):
    items = json.loads((repo / 'items.json').read_text())
    clipped = [[300, 300], [370, 300], [370, 360], [300, 360]]
    items['lot'] = [{'scene': SCENE, 'line': 1, 'object_id': 'bldg0001', 'kind': 'building', 'status_at_compute': 'candidate',
                     'current_ring': RING_B, 'proposed_ring': clipped, 'proposal_id': '01l0t0123456789', 'lot': '4', 'plan': 'PS1',
                     'pfi': 'p4', 'clipped_m2': 3.2}]
    (repo / 'items.json').write_text(json.dumps(items))
    staged = _stage(repo, families=('lot',))
    assert staged['counts'] == {'lot': 1}
    server = FakeFieldwork()
    fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    item_id = next(doc['id'] for (k, _), doc in server.docs.items() if k == 'item')
    assert item_id.startswith('r1013-10-lot-001-p') and 'lot-bound-2026-10-13' in server.docs[('item', item_id)]['tags']
    server.edit(item_id, _scene_edit(server, 'new-bldg0001', **BAR['partly']))
    result = _pull(repo, server)
    assert 'lots: partly: 1' in result['answers'] and not result['conflicts'] and not result['unread']


# ------------------------------------------------------------------------------------------------- splits (2026-10-17)

def test_split_items_ask_once_per_roof_and_read_back_as_a_splits_line(repo):
    items = json.loads((repo / 'items.json').read_text())
    halves = [[[300, 300], [340, 300], [340, 360], [300, 360]], [[340, 300], [380, 300], [380, 360], [340, 360]]]
    items['split'] = [{'scene': SCENE, 'line': 1, 'object_id': 'bldg0001', 'kind': 'building', 'status_at_compute': 'candidate',
                       'current_ring': RING_B, 'proposed_ring': halves[0], 'proposal_id': '01sp1it01234567',
                       'units': [{'id': f'u{i}', 'lot': str(i + 1), 'plan': 'PS1', 'pfi': f'p{i}', 'm2': 40.0 - i, 'ring': h}
                                 for i, h in enumerate(halves)]}]
    (repo / 'items.json').write_text(json.dumps(items))
    staged = _stage(repo, families=('split',))
    assert staged['counts'] == {'split': 1}
    server = FakeFieldwork()
    fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    item_id = next(doc['id'] for (k, _), doc in server.docs.items() if k == 'item')
    doc = server.docs[('item', item_id)]
    assert item_id.startswith('r1017-12-split-001-p') and 'splits-2026-10-17' in doc['tags']
    assert {m['id'] for m in doc['annotations']} == {'cur-bldg0001', 'unit-u1', 'new-01sp1it01234567'}
    server.edit(item_id, _scene_edit(server, 'new-01sp1it01234567', **BAR['wrong']))
    result = _pull(repo, server)
    assert 'splits: wrong: 1' in result['answers'] and not result['conflicts'] and not result['unread']


def test_split_v2_items_carry_addresses_lot_boundaries_and_neighbours_clipped_to_the_crop(repo):
    items = json.loads((repo / 'items.json').read_text())
    halves = [[[300, 300], [340, 300], [340, 360], [300, 360]], [[340, 300], [380, 300], [380, 360], [340, 360]]]
    items['split_set'] = {'prefix': 'r1017-13', 'tag': 'splits-2026-10-17-v2'}
    items['split'] = [{'scene': SCENE, 'line': 321, 'object_id': 'bldg0001', 'kind': 'building', 'status_at_compute': 'candidate',
                       'current_ring': RING_B, 'proposed_ring': halves[0], 'proposal_id': '01sp1it01234567',
                       'addresses': ['1/12 Example St', '2/12 Example St'],
                       'lot_rings': [{'pfi': 'p0', 'label': 'LOT 1 PS1 · 1/12 Example St', 'ring': [[290, 290], [340, 290], [340, 370], [290, 370]], 'involved': True},
                                     {'pfi': 'far', 'label': 'LOT 9', 'ring': [[2000, 2000], [2010, 2000], [2010, 2010]]}],
                       'neighbours': [{'id': 'bldg0002', 'status': 'candidate', 'ring': RING_B2}],
                       'units': [{'id': f'u{i}', 'lot': str(i + 1), 'plan': 'PS1', 'pfi': f'p{i}', 'm2': 40.0 - i, 'ring': h}
                                 for i, h in enumerate(halves)]}]
    (repo / 'items.json').write_text(json.dumps(items))
    staged = _stage(repo, families=('split',))
    server = FakeFieldwork()
    fr.push(root=repo, stage_dir=staged['stage'], url='http://127.0.0.1:1', directory='mirror', client=server)
    doc = next(doc for (k, _), doc in server.docs.items() if k == 'item')
    assert doc['id'].startswith('r1017-13-split-321-p') and 'splits-2026-10-17-v2' in doc['tags'] and '1/12 Example St' in doc['title']
    ids = {m['id'] for m in doc['annotations']}
    assert 'lot-p0' in ids and 'lot-far' not in ids, 'lots outside the crop are dropped'
    for m in doc['annotations']:
        assert all(0 <= v <= 1 for ring in m['geometry']['coordinates'] for p in ring for v in p)
    proposals = [m['id'] for m in doc['annotations'] if m['properties']['addr']['source'].startswith('agent-proposal')]
    assert proposals == ['new-01sp1it01234567']
