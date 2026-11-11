import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

import pytest
from scripts.ml import eyes_index
from scripts.ml import reference_registry as registry
from scripts.ml.scene_confidence import geographic
from scripts.ml.scene_split import group_split
from scripts.ml.tiles import lonlat_to_tile_xy
from shapely.geometry import mapping, shape

REPO = Path(__file__).resolve().parents[3]
SHA = 'a' * 64
FRAME = {'centroid': {'lon': 145.3127, 'lat': -37.7903}, 'pixel_to_meters': [[.1, 0], [0, -.1], [-50, 50]],
         'meters_to_pixel': [[10, 0], [0, -10], [500, 500]], 'image_width': 1000, 'image_height': 1000}
SCENE = {'id': 's1', **FRAME, 'image_sha256': SHA, 'model_signature': 'sig1', 'title': 'Scene 1'}
EDITS = {'schema_version': 1, 'outline': {'ring': [[300, 100], [400, 100], [400, 200], [300, 200]], 'holes': []},
         'baseline': {'vertices': [], 'edges': []}, 'vertices': [[-20, 38], [-10, 38], [-15, 34]],
         'edges': [{'a': 0, 'b': 1, 'kind': 'ridge'}, {'a': 1, 'b': 2, 'kind': 'hip'}, {'a': 0, 'b': 9, 'kind': 'hip'}],
         'checked': False, 'notes': ''}


def square(x, y, size=100):
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]


def building(oid, ring, status='candidate', **extra):
    return {'id': oid, 'kind': 'building', 'label': 'Building', 'ring': ring, 'status': status,
            'geometry_reviewed': False, 'source': 'sam3-text-onnx', 'score': .9, **extra}


@pytest.mark.parametrize('status,lines,tier', [('confirmed', False, 'T1'), ('needs_adjustment', False, 'T2'),
                                              ('needs_adjustment', True, 'T2')])
@pytest.mark.parametrize('authority,reason', [('fieldwork_import', 'fieldwork_pending'), ('provenance', 'machine_label')])
def test_pending_fieldwork_and_machine_edits_are_not_human_tiers(status, lines, tier, authority, reason):
    obj = building('test', square(105, 100), status, geometry_reviewed=status == 'confirmed')
    if lines:
        obj['roof_line_edits'] = EDITS
    obj[authority] = {'pending': True} if authority == 'fieldwork_import' else {'source': 'machine:test'}
    assert registry.review_tier(obj, square(100, 100)) == ('T3', f'review:{reason}', False)
    if authority == 'fieldwork_import':
        obj['fieldwork_import']['pending'] = False
        assert registry.review_tier(obj, square(100, 100))[0] == tier


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def crm_geometry(ring):
    return json.dumps(mapping(geographic({'ring': ring}, SCENE)))


def make_repo(root, crm=True, loop=True):
    model = {**SCENE, 'objects': [building(i, square(x, y)) for i, x, y in (
        ('conf', 100, 100), ('edit', 300, 100), ('moved', 500, 100), ('same', 700, 100),
        ('cand', 100, 300), ('old', 300, 300), ('loop', 500, 500))]}
    write_json(root / 'data/scenes/s1/latest.json', model)
    write_json(root / 'data/scenes/s1/models/sig1.json', model)
    (root / 'data/scenes/s1' / f'{SHA}.png').write_bytes(b'png')
    write_json(root / 'data/scenes/s1/review.json', {
        'revision': 3, 'model_signature': 'sig1', 'updated_at': '2026-09-24T00:00:00Z', 'objects': [
            building('conf', square(100, 100), 'confirmed', geometry_reviewed=True, roof_type='gabled', label='Metal gable'),
            building('edit', square(300, 100), 'needs_adjustment', roof_line_edits=EDITS, roof_type='hipped', address_id=29790),
            building('moved', square(505, 100), 'needs_adjustment'),
            building('same', square(700, 100), 'needs_adjustment'),
            building('cand', square(100, 300)),
            building('old', square(300, 300), 'superseded', roof_line_edits=EDITS),
            building('manual', square(600, 600), 'needs_adjustment'),
            building('gone', square(800, 800), 'rejected'),
            {'id': 'tree1', 'kind': 'tree', 'ring': square(800, 800), 'status': 'confirmed', 'geometry_reviewed': True},
        ]})
    if crm:
        db = sqlite3.connect(root / 'data/australia_geo.db')
        db.execute('CREATE TABLE crm_features(id INTEGER PRIMARY KEY, address_id INTEGER, kind TEXT, geometry_json TEXT, '
                   'label TEXT, properties TEXT, updated_at TEXT, updated_by TEXT)')
        db.executemany('INSERT INTO crm_features VALUES(?,?,?,?,?,?,?,?)', [
            (9, 29790, 'roof', crm_geometry(square(100, 100)), 'Visible roof eaves — straight hip ends 2026-09-22',
             json.dumps({'roof_form_source': 'reviewer', 'boundary_reference_source': 'user-confirmed-roof-form-and-imagery',
                         'roof_type': 'gabled', 'boundary_image_sha256': 'b' * 64, 'eave_height': '2.7'}),
             '2026-09-22T11:01:58Z', 'local'),
            (22, 29790, 'boundary', crm_geometry(square(0, 0, 900)), 'Cadastral property parcel boundary', '{}', '2026-09-21', 'local'),
            (23, 25024, 'roof', crm_geometry(square(300, 300)), 'Visible roof eaves — imagery review',
             json.dumps({'boundary_reference_source': 'agent-imagery-trace', 'boundary_image_sha256': SHA}), '2026-09-22', 'local'),
            (10, 25892, 'roof', crm_geometry(square(500, 300)), 'canopy carve (review loop)', json.dumps({'roof_type': 'hipped'}),
             '2026-09-16', 'editor'),
            (30, 1, 'roof', None, 'no geometry', 'null', '2026-09-16', 'editor'),
        ])
        db.commit()
        db.close()
    if loop:
        write_json(root / 'data/roof-review-loop/runs/0123456789abcdef/report.json', {
            'run': '0123456789abcdef', 'updated_at': 1790297710.0, 'rows': [
                {'scene_id': 's1', 'object_id': 'loop', 'source_model': 'sig1', 'image_sha256': SHA, 'status': 'no_safe_gain',
                 'reason': 'No alternative passed every check.', 'source_band': 'low'},
                {'scene_id': 's1', 'object_id': 'missing', 'source_model': 'sig1', 'image_sha256': SHA,
                 'status': 'needs_evidence', 'reason': 'x', 'source_band': 'low'}]})
    return root


def by_key(rows):
    return {(r['source_kind'], r['payload']['provenance'].split(':')[0], r['object_id'] or r['crm_feature_id']): r for r in rows}


def test_tiers_follow_human_decision_rules(tmp_path):
    rows = registry.build(make_repo(tmp_path))
    tiers = {k: r['tier'] for k, r in by_key(rows).items()}
    assert tiers == {
        ('review', 'review', 'conf'): 'T1',
        ('review', 'review', 'edit'): 'T2', ('review', 'review', 'moved'): 'T2', ('review', 'review', 'manual'): 'T2',
        ('crm', 'crm', 9): 'T2',
        ('review', 'review', 'same'): 'T3', ('review', 'review', 'cand'): 'T3',
        ('crm', 'crm', 23): 'T3', ('crm', 'crm', 10): 'T3', ('review', 'review-loop', 'loop'): 'T3',
        ('review', 'review', 'old'): 'T4', ('crm', 'crm', 22): 'T4',
    }
    keyed = by_key(rows)
    assert keyed[('review', 'review', 'same')]['payload']['independent_geometry'] is False
    assert keyed[('review', 'review', 'cand')]['payload']['independent_geometry'] is False
    assert keyed[('review', 'review-loop', 'loop')]['payload']['independent_geometry'] is False
    assert keyed[('crm', 'crm', 23)]['payload']['provenance'] == 'crm:agent-imagery-trace'
    assert keyed[('crm', 'crm', 10)]['payload']['provenance'] == 'crm:review-loop'
    assert keyed[('crm', 'crm', 22)]['payload']['object_kind'] == 'boundary'
    assert all(r['version'] == registry.VERSION for r in rows)
    assert [r['ref_id'] for r in rows] == sorted(r['ref_id'] for r in rows)


def test_scene_bound_row_fields(tmp_path):
    root = make_repo(tmp_path)
    row = by_key(registry.build(root))[('review', 'review', 'edit')]
    polygon = geographic({'ring': square(300, 100)}, SCENE)
    x, y = lonlat_to_tile_xy(polygon.centroid.x, polygon.centroid.y, 17)
    group = f'z17:{math.floor(x)}:{math.floor(y)}'
    assert row['ref_id'] == hashlib.sha256(b'review:s1:edit').hexdigest()[:16] == registry.ref_id('review', 's1', 'edit')
    assert (row['source_kind'], row['scene_id'], row['object_id'], row['crm_feature_id'], row['address_id']) == ('review', 's1', 'edit', None, 29790)
    assert row['spatial_group'] == group and row['split'] == group_split(group)
    assert [row['west'], row['south'], row['east'], row['north']] == pytest.approx(list(polygon.bounds))
    payload = row['payload']
    assert shape(payload['geometry_wgs84']).equals_exact(polygon, 1e-12)
    assert payload['geometry_local_m']['coordinates'][0][0] == pytest.approx([-20, 40])
    assert payload['image_sha256'] == SHA and payload['drawn_over'] == 'scene' and payload['drawn_over_scene_ids'] == ['s1']
    assert payload['image_identity_source'] == 'scene_image_sha256'
    assert payload['roof_type'] == 'hipped' and payload['checked'] is False and payload['tier_name'] == 'human_edit'
    assert [(e['a_xy_m'], e['b_xy_m'], e['kind']) for e in payload['edges']] == [([-20, 38], [-10, 38], 'ridge'), ([-10, 38], [-15, 34], 'hip')]
    ridge = payload['edges'][0]
    assert ridge['a_lonlat'] == pytest.approx(registry.local_to_lonlat([[-20, 38]], SCENE)[0])
    assert payload['source_sha256'] == hashlib.sha256((root / 'data/scenes/s1/review.json').read_bytes()).hexdigest()
    assert payload['code_sha256'] == hashlib.sha256(Path(registry.__file__).read_bytes()).hexdigest()
    assert payload['updated_at'] == '2026-09-24T00:00:00Z' and payload['computed_at']
    assert payload['model_signature'] == 'sig1' and payload['independent_geometry'] is True


def test_local_to_lonlat_matches_scene_confidence_geographic():
    local = [[.1 * x - 50, -.1 * y + 50] for x, y in square(120, 340, 37)]
    expected = list(geographic({'ring': square(120, 340, 37)}, SCENE).exterior.coords)[:4]
    got = registry.local_to_lonlat(local, SCENE)
    assert [v for p in got for v in p] == pytest.approx([v for p in expected for v in p], abs=1e-12)


def test_crm_rows_are_read_only_and_flag_conflicts(tmp_path):
    root = make_repo(tmp_path)
    before = hashlib.sha256((root / 'data/australia_geo.db').read_bytes()).hexdigest()
    skipped = {}
    keyed = by_key(registry.build(root, skipped=skipped))
    assert hashlib.sha256((root / 'data/australia_geo.db').read_bytes()).hexdigest() == before
    crm9 = keyed[('crm', 'crm', 9)]
    assert (crm9['scene_id'], crm9['object_id'], crm9['crm_feature_id'], crm9['address_id']) == (None, None, 9, 29790)
    assert crm9['ref_id'] == registry.ref_id('crm', 9)
    p9 = crm9['payload']
    assert p9['provenance'] == 'crm:reviewer' and p9['geometry_local_m'] is None and p9['image_sha256'] is None
    assert p9['boundary_image_sha256'] == 'b' * 64 and p9['drawn_over'] == 'unknown'
    assert p9['image_identity_source'] == 'crm_boundary_image_sha256'
    assert p9['label_property_conflict'] == {'label_forms': ['hipped'], 'roof_type': 'gabled'}
    assert p9['eave_height_m'] == 2.7 and p9['roof_type'] == 'gabled' and p9['edges'] == []
    assert len(p9['source_sha256']) == 64
    assert keyed[('crm', 'crm', 23)]['payload']['drawn_over'] == 'scene'
    assert keyed[('review', 'review', 'conf')]['payload']['label_property_conflict'] is None
    assert skipped['crm_invalid_geometry'] == 1 and skipped['review_rejected'] == 1
    assert skipped['review_non_building'] == 1 and skipped['review_loop_object_unavailable'] == 1


def test_build_without_crm_or_loop_sources(tmp_path):
    rows = registry.build(make_repo(tmp_path, crm=False, loop=False))
    assert {r['source_kind'] for r in rows} == {'review'}
    assert not (tmp_path / 'data/australia_geo.db').exists()


def test_write_index_registry_folder_and_stale_rows(tmp_path):
    root = make_repo(tmp_path)
    rows = registry.build(root)
    summary = registry.write(root, rows)
    assert summary['registry_id'] == registry.registry_id(rows) and len(summary['registry_id']) == 16
    folder = root / 'data/reviews/eyes/registry' / summary['registry_id']
    lines = (folder / 'references.jsonl').read_text().splitlines()
    assert len(lines) == len(rows) and json.loads(lines[0])['ref_id'] == rows[0]['ref_id']
    saved = json.loads((folder / 'summary.json').read_text())
    assert saved['n_references'] == len(rows) and saved['tiers']['T1']['total'] == 1
    assert sum(saved['tiers']['T2'][s] for s in registry.SPLITS) == saved['tiers']['T2']['total'] == 4
    assert saved['label_tiers'] == ['T1', 'T2']
    with eyes_index.connection(root, readonly=True) as db:
        indexed = eyes_index.rows(db, 'references', version=registry.VERSION)
    assert len(indexed) == len(rows) and {r['ref_id'] for r in indexed} == {r['ref_id'] for r in rows}
    time.sleep(.001)
    again = registry.build(root)
    assert registry.registry_id(again) == summary['registry_id']
    registry.write(root, again[1:])
    with eyes_index.connection(root, readonly=True) as db:
        assert len(eyes_index.rows(db, 'references', version=registry.VERSION)) == len(rows) - 1


def test_cli_build_and_summary(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert registry.main(['--root', str(root), '--build']) == 0
    built = json.loads(capsys.readouterr().out)
    assert built['n_references'] == 12 and built['skipped']['review_rejected'] == 1
    assert registry.main(['--root', str(root), '--summary']) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['tiers'] == built['tiers']


def test_malformed_review_records_are_counted_not_fatal(tmp_path):
    root = make_repo(tmp_path, crm=False, loop=False)
    write_json(root / 'data/scenes/s1/review.json', {'model_signature': 'sig1', 'objects': [
        building('conf', square(100, 100), 'confirmed', geometry_reviewed=True, label=7, roof_type='gabled'),
        building('edit', square(300, 100), 'needs_adjustment', roof_line_edits=True),   # not a dict: no human edges
        building('nan', [[float('nan'), 0], [1, 0], [1, 1], [0, 1]], 'confirmed', geometry_reviewed=True),
        'not-an-object', 5]})
    skipped = {}
    rows = {r['object_id']: r for r in registry.build(root, skipped)}
    assert set(rows) == {'conf', 'edit'}
    assert rows['conf']['tier'] == 'T1' and rows['conf']['payload']['label_property_conflict'] is None
    edit = rows['edit']
    assert edit['tier'] == 'T3' and edit['payload']['edges'] == [] and edit['payload']['checked'] is None
    assert skipped == {'review_invalid_geometry': 1, 'review_object_not_dict': 2}
    write_json(root / 'data/scenes/s1/review.json', {'model_signature': 'sig1', 'objects': {'conf': 1}})
    skipped = {}
    assert registry.build(root, skipped) == [] and skipped == {'review_objects_not_list': 1}
    assert registry.scene_model(root, None) is None and registry.scene_model(root, '../s1') is None
    assert registry.review_tier({'status': 'needs_adjustment', 'roof_line_edits': [1], 'ring': []}, []) == ('T3', 'review:needs_adjustment', False)


def test_hole_only_edit_is_a_human_edit_when_model_holes_are_known(tmp_path):
    """Add interior gap -> Save outline · Still needs work leaves the ring alone; the hole is the reviewer's edit."""
    hole = [[120, 120], [140, 120], [140, 140], [120, 140]]
    obj = building('gap', square(100, 100), 'needs_adjustment', holes=[hole])
    assert registry.review_tier(obj, square(100, 100), model_holes=[]) == ('T2', 'review:needs_adjustment_edited', True)
    assert registry.review_tier(obj, square(100, 100), model_holes=[hole]) == ('T3', 'review:needs_adjustment', False)
    assert registry.review_tier(obj, square(100, 100)) == ('T3', 'review:needs_adjustment', False)   # ring-only callers
    assert registry.review_tier(obj, square(100, 100), False, model_holes=[]) == ('T3', 'review:needs_adjustment_unverified_model', True)
    assert registry.outline_edited(obj, square(100, 100), []) and not registry.outline_edited(obj, square(100, 100), [hole])
    assert registry.outline_edited(obj, None) and not registry.outline_edited(obj, square(100, 100))
    # review_references passes the model object's holes, so the built row grades the same way.
    root = make_repo(tmp_path, crm=False, loop=False)
    review = json.loads((root / 'data/scenes/s1/review.json').read_text())
    for o in review['objects']:
        if o['id'] == 'same':
            o['holes'] = [hole]
    write_json(root / 'data/scenes/s1/review.json', review)
    same = by_key(registry.build(root))[('review', 'review', 'same')]
    assert same['tier'] == 'T2' and same['payload']['provenance'] == 'review:needs_adjustment_edited'
    assert same['payload']['independent_geometry'] is True


def test_needs_adjustment_needs_a_verified_model_ring_to_be_a_human_edit(tmp_path):
    root = make_repo(tmp_path, crm=False, loop=False)
    review = json.loads((root / 'data/scenes/s1/review.json').read_text())
    write_json(root / 'data/scenes/s1/review.json', {**review, 'model_signature': 'f' * 64})   # that model file is missing
    keyed = by_key(registry.build(root))
    moved, manual = keyed[('review', 'review', 'moved')], keyed[('review', 'review', 'manual')]
    assert moved['tier'] == 'T3' and moved['payload']['provenance'] == 'review:needs_adjustment_unverified_model'
    assert moved['payload']['frame_signature_matches_review'] is False and moved['payload']['independent_geometry'] is True
    assert manual['tier'] == 'T2'   # absent from every model: only a human could have drawn it
    assert keyed[('review', 'review', 'edit')]['tier'] == 'T2' and keyed[('review', 'review', 'conf')]['tier'] == 'T1'
    assert keyed[('review', 'review', 'same')]['tier'] == 'T3'


@pytest.mark.skipif(not (REPO / 'data/australia_geo.db').exists() or not (REPO / 'data/scenes/house-30286/review.json').exists(),
                    reason='real repository data not present')
def test_real_repo_cluster_references_read_only():
    started = time.monotonic()
    pngs = registry.png_index(REPO)
    crm = {r['crm_feature_id']: r for r in registry.crm_references(REPO, pngs)}
    review = {(r['scene_id'], r['object_id']): r for r in registry.review_references(REPO, pngs)}
    assert crm[9]['tier'] == 'T2' and crm[9]['payload']['label_property_conflict']
    assert crm[22]['tier'] == 'T4'
    assert all(crm[i]['tier'] == 'T3' for i in (23, 24, 25))
    edit = review[('house-30286', '37ff5c02f77869d5')]
    assert edit['tier'] == 'T2' and edit['payload']['checked'] is False and len(edit['payload']['edges']) == 32
    assert sum(r['tier'] == 'T1' for r in review.values()) >= 40
    # 20 s: the registry now carries about 5,400 tier-3 rows (agent observations and machine labels); this guards
    # the read-only path against pathological slowness, not a 5 s target on a loaded machine.
    assert time.monotonic() - started < 20


def test_machine_confirmed_labels_are_never_human_reference_tiers():
    """decision_policy --apply-auto writes confirmed objects with machine provenance; the registry keeps them at T3."""
    from scripts.ml.reference_registry import review_tier
    machine = {'status': 'confirmed', 'geometry_reviewed': True, 'provenance': {'source': 'machine:decision-policy-v2', 'tier': 'auto'}}
    human = {'status': 'confirmed', 'geometry_reviewed': True}
    assert review_tier(machine, None)[0] == 'T3' and review_tier(machine, None)[1] == 'review:machine_label'
    assert review_tier(human, None)[0] == 'T1'


# --- carried objects (recapture carry path; docs/recapture-carry-2026-09-30.md section 6) -------------------------------

CARRIED = {'scene_id': 'house-1', 'model_signature': 'b' * 64, 'object_id': 'src-1', 'status': 'needs_adjustment',
           'geometry_reviewed': True, 'iou_with_new': .71, 'matched_id': 'new-1', 'method': 'georeferenced-affine'}


ODD_SHAPES = ('house-1', ['house-1'], True, 1, json.dumps(CARRIED))   # non-dict but non-empty: malformed, still a carry
EMPTY_SHAPES = ({}, None, '', [], 0, False)                            # falsy: not carried


def test_carried_undecided_predicate():
    """RULES human_precision.classes undecided: 'any object carrying carried_from with status needs_adjustment whatever its
    other fields say'. The contract writes a dict, but nothing on the save path rejects another shape yet, so the predicate
    follows the frozen text: any non-empty carried_from carries (review_authority.carried), not only a dict."""
    from scripts.ml.review_authority import carried, carried_undecided
    assert carried_undecided({'carried_from': CARRIED, 'status': 'needs_adjustment'}) is True
    assert carried_undecided({'carried_from': {'scene_id': 'house-1'}, 'status': 'needs_adjustment'}) is True
    for status in ('confirmed', 'rejected', 'superseded', 'candidate', None):
        assert carried({'carried_from': CARRIED, 'status': status}) is True
        assert carried_undecided({'carried_from': CARRIED, 'status': status}) is False
    for odd in ODD_SHAPES:
        assert carried({'carried_from': odd}) is True, odd
        assert carried_undecided({'carried_from': odd, 'status': 'needs_adjustment'}) is True, odd
        assert carried_undecided({'carried_from': odd, 'status': 'confirmed'}) is False, odd
    for not_carried in EMPTY_SHAPES:
        assert carried({'carried_from': not_carried}) is False, not_carried
        assert carried_undecided({'carried_from': not_carried, 'status': 'needs_adjustment'}) is False, not_carried
    assert carried({}) is False and carried_undecided({'status': 'needs_adjustment'}) is False


def test_carried_objects_are_not_human_geometry_until_decided_on_the_new_image():
    """A carried object keeps the source object's id, so no model ring exists in the new frame and outline_edited is
    True; with roof_line_edits travelling too, the pre-fix rules graded it T2. It is a target the reviewer still has
    to decide (round-5 RULES human_precision.classes undecided), never human geometry."""
    obj = building('src-1', square(105, 100), 'needs_adjustment', carried_from=CARRIED, roof_line_edits=EDITS)
    assert registry.review_tier(obj, None) == ('T3', 'review:carried_undecided', False)
    assert registry.review_tier(obj, square(100, 100)) == ('T3', 'review:carried_undecided', False)
    assert registry.review_tier(obj, square(100, 100), False, model_holes=[]) == ('T3', 'review:carried_undecided', False)
    # review_authority_blocker still comes first: a carried machine label is excluded as a machine label.
    assert registry.review_tier({**obj, 'provenance': {'source': 'machine:test'}}, None) == ('T3', 'review:machine_label', False)
    assert registry.review_tier({**obj, 'fieldwork_import': {'pending': True}}, None) == ('T3', 'review:fieldwork_pending', False)
    # The reviewer's own decision on the new image keeps its tier.
    confirmed = building('src-1', square(105, 100), 'confirmed', geometry_reviewed=True, carried_from=CARRIED)
    assert registry.review_tier(confirmed, None) == ('T1', 'review:confirmed', True)
    assert registry.review_tier(building('src-1', square(105, 100), 'rejected', carried_from=CARRIED), None) == (None, 'review_rejected', None)
    assert registry.review_tier(building('src-1', square(105, 100), 'superseded', carried_from=CARRIED), None) == ('T4', 'review:superseded', True)
    # The carry never writes a candidate (unedited candidates are not carried); the existing candidate rule is unchanged.
    assert registry.review_tier(building('src-1', square(105, 100), 'candidate', carried_from=CARRIED), None) == ('T3', 'review:candidate', True)
    # A malformed, non-dict carried_from is still a carry (RULES: 'any object carrying carried_from'): never a human tier.
    for odd in ODD_SHAPES:
        odd_obj = building('src-1', square(105, 100), 'needs_adjustment', carried_from=odd)
        assert registry.review_tier(odd_obj, None) == ('T3', 'review:carried_undecided', False), odd
        assert registry.review_tier({**odd_obj, 'roof_line_edits': EDITS}, square(100, 100)) == ('T3', 'review:carried_undecided', False), odd
    # An empty carried_from is not a carried object: the existing needs_adjustment rule applies.
    for empty in EMPTY_SHAPES:
        plain = building('src-1', square(105, 100), 'needs_adjustment', carried_from=empty)
        assert registry.review_tier(plain, None) == ('T2', 'review:needs_adjustment_edited', True), empty


def test_built_registry_grades_a_carried_undecided_object_as_t3(tmp_path):
    root = make_repo(tmp_path, crm=False, loop=False)
    lot = {**SCENE, 'id': 'lot-29790', 'model_signature': 'sig2', 'objects': [building('new-1', square(100, 100))]}
    write_json(root / 'data/scenes/lot-29790/latest.json', lot)
    write_json(root / 'data/scenes/lot-29790/models/sig2.json', lot)
    write_json(root / 'data/scenes/lot-29790/review.json', {
        'revision': 1, 'model_signature': 'sig2', 'updated_at': '2026-10-01T00:00:00Z', 'objects': [
            building('src-1', square(103, 101), 'needs_adjustment', carried_from=CARRIED, roof_line_edits=EDITS),
            building('src-2', square(300, 300), 'confirmed', geometry_reviewed=True, carried_from={**CARRIED, 'object_id': 'src-2'}),
            building('src-3', square(500, 500), 'needs_adjustment', carried_from='house-1', roof_line_edits=EDITS)]})
    rows = {r['object_id']: r for r in registry.build(root) if r['scene_id'] == 'lot-29790'}
    assert (rows['src-1']['tier'], rows['src-1']['payload']['provenance'], rows['src-1']['payload']['independent_geometry']) == \
        ('T3', 'review:carried_undecided', False)
    assert rows['src-1']['tier'] not in registry.LABEL_TIERS
    assert (rows['src-2']['tier'], rows['src-2']['payload']['provenance']) == ('T1', 'review:confirmed')
    assert (rows['src-3']['tier'], rows['src-3']['payload']['provenance'], rows['src-3']['payload']['independent_geometry']) == \
        ('T3', 'review:carried_undecided', False), 'a non-dict carried_from is still a carry'
