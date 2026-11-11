import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

import pytest
from scripts.ml import eyes_index
from scripts.ml import reference_agreement as agreement
from scripts.ml import reference_registry as registry
from scripts.ml.scene_confidence import geographic
from scripts.ml.tiles import WORLD_SPAN_M
from shapely.geometry import MultiPolygon, mapping

REPO = Path(__file__).resolve().parents[3]
SHA, OTHER_SHA = 'a' * 64, 'c' * 64
LAT = -37.7903
FRAME = {'pixel_to_meters': [[.1, 0], [0, -.1], [-50, 50]], 'meters_to_pixel': [[10, 0], [0, -10], [500, 500]],
         'image_width': 1000, 'image_height': 1000}
# s2 is centred 10 m east of s1, so s1 local x == s2 local x + 10.
S1 = {'id': 's1', 'centroid': {'lon': 145.3127, 'lat': LAT}, **FRAME, 'image_sha256': SHA, 'model_signature': 'sig1', 'title': 'S1'}
S2 = {'id': 's2', 'centroid': {'lon': 145.3127 + 10 * 360 / (WORLD_SPAN_M * math.cos(math.radians(LAT))), 'lat': LAT}, **FRAME,
      'image_sha256': OTHER_SHA, 'model_signature': 'sig2', 'title': 'S2'}
LINES = [{'line_meters': [[-40, 35], [-30, 35]], 'kind': 'ridge'},
         {'line_meters': [[-40, 30], [-35, 35]], 'kind': 'hip'},
         {'line_meters': [[-30, 30], [-30, 40]], 'kind': 'valley'}]
PARTS = [{'ring_meters': [[-40, 30], [-30, 30], [-30, 40]], 'ring_z': [3.2, 3.2, 5.0]}]


def square(x, y, size=100):
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]


def building(oid, ring, status='candidate', **extra):
    return {'id': oid, 'kind': 'building', 'label': 'Building', 'ring': ring, 'status': status,
            'geometry_reviewed': False, 'source': 'sam3-text-onnx', 'score': .6, **extra}


M1 = building('m1', square(100, 100), roof_type='gabled', roof_lines=LINES, roof_parts=PARTS)
M2 = building('m2', square(110, 100), roof_type='hipped')
FAR = building('far', square(800, 800))
N1 = building('n1', square(0, 100), roof_type='gabled',
              roof_lines=[{'line_meters': [[-50, 35], [-40, 35]], 'kind': 'ridge'}])


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def scene_ref(obj, scene=S1, tier='T2', edges=None, **extra):
    if edges is not None:
        obj = {**obj, 'roof_line_edits': {'vertices': [p for e in edges for p in e[:2]], 'checked': False,
                                          'edges': [{'a': 2 * i, 'b': 2 * i + 1, 'kind': e[2]} for i, e in enumerate(edges)]}}
    return registry.object_reference(scene['id'], scene, {**obj, **extra}, tier=tier, provenance='review:test', pngs={},
                                     source_sha256='x', updated_at=None, independent=True)


def crm_ref(ring=None, props=None, feature_id=9):
    record = {'id': feature_id, 'address_id': 29790, 'kind': 'roof', 'label': 'Visible roof eaves',
              'geometry_json': json.dumps(mapping(geographic({'ring': ring or square(100, 100)}, S1))),
              'properties': json.dumps({'roof_form_source': 'reviewer', 'roof_type': 'gabled', 'eave_height': '2.7',
                                        'boundary_image_sha256': 'b' * 64, **(props or {})}),
              'updated_at': '2026-09-22', 'updated_by': 'local'}
    return registry.crm_reference(record, {})


def make_repo(root, registry_rows=True):
    write_json(root / 'data/scenes/s1/latest.json', {**S1, 'objects': [M1, M2, FAR]})
    write_json(root / 'data/scenes/s1/models/sig1.json', {**S1, 'objects': [M1, M2, FAR]})
    write_json(root / 'data/scenes/s2/latest.json', {**S2, 'objects': [N1]})
    write_json(root / 'data/scenes/s1/review.json', {'model_signature': 'sig1', 'updated_at': 't', 'objects': [
        building('m1', square(100, 100), 'confirmed', geometry_reviewed=True, roof_type='gabled'), FAR]})
    folder = root / 'data/suburbs/mooroolbark'
    folder.mkdir(parents=True)
    db = sqlite3.connect(folder / 'confidence.sqlite')
    db.execute('CREATE TABLE scenes(id TEXT PRIMARY KEY, signature TEXT, mtime INTEGER, bounds TEXT)')
    db.execute('CREATE TABLE roofs(scene_id TEXT, object_id TEXT, west REAL, south REAL, east REAL, north REAL, payload TEXT, PRIMARY KEY(scene_id,object_id))')
    db.execute('INSERT INTO scenes VALUES(?,?,?,?)', ('s1', 'sig1', (root / 'data/scenes/s1/latest.json').stat().st_mtime_ns, '[]'))
    for obj, band in ((M1, 'low'), (FAR, 'high')):
        polygon = geographic(obj, S1)
        props = {'object_id': obj['id'], 'scene_id': 's1', 'band': band, 'detector_score': .58, 'image_line_mismatch_m': 1.35,
                 'observed_segments': 4, 'clipped': False, 'area_m2': 100.0, 'model_signature': 'sig1', 'profile': 'p1',
                 'reasons': ['r']}
        db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', ('s1', obj['id'], *polygon.bounds,
                   json.dumps({'type': 'Feature', 'geometry': mapping(polygon), 'properties': props})))
    db.commit()
    db.close()
    crm = sqlite3.connect(root / 'data/australia_geo.db')
    crm.execute('CREATE TABLE crm_features(id INTEGER PRIMARY KEY, address_id INTEGER, kind TEXT, geometry_json TEXT, '
                'label TEXT, properties TEXT, updated_at TEXT, updated_by TEXT)')
    for feature_id, kind, ring, props in (
            (9, 'roof', square(100, 100), {'roof_form_source': 'reviewer', 'roof_type': 'gabled'}),
            (22, 'boundary', square(0, 0, 900), {}),
            (23, 'roof', square(400, 400), {'boundary_reference_source': 'agent-imagery-trace'})):
        crm.execute('INSERT INTO crm_features VALUES(?,?,?,?,?,?,?,?)', (feature_id, 29790, kind,
                    json.dumps(mapping(geographic({'ring': ring}, S1))), 'label', json.dumps(props), 't', 'local'))
    crm.commit()
    crm.close()
    if registry_rows:
        registry.write(root, registry.build(root))
    return root


def test_lonlat_to_local_inverts_local_to_lonlat():
    local = [[-12.5, 7.25], [30, -40], [0, 0]]
    assert [v for p in agreement.lonlat_to_local(registry.local_to_lonlat(local, S2), S2) for v in p] == \
        pytest.approx([v for p in local for v in p], abs=1e-6)


def test_metrics_identical_scene_bound_reference():
    result = agreement.metrics(scene_ref(M1), M1, S1)
    assert result['iou'] == pytest.approx(1) and result['hausdorff_m'] == pytest.approx(0)
    assert result['area_ratio'] == pytest.approx(1) and result['centroid_shift_m'] == pytest.approx(0)
    assert result['roof_type_match'] is True and result['image_identity'] == 'same_image' and result['frame'] == 'scene_local'
    assert result['n_model_lines'] == 3 and result['n_ref_edges'] == 0
    assert result['line_precision'] is None and result['line_recall'] is None and result['line_kind_precision'] is None
    assert result['eave_delta_m'] is None and result['model_eave_m'] == pytest.approx(3.2)
    assert result['geometry_copy_suspected'] is False


def test_metrics_shifted_outline_in_metres():
    result = agreement.metrics(scene_ref(M1), M2, S1)
    assert result['iou'] == pytest.approx(90 / 110, abs=1e-4)
    assert result['hausdorff_m'] == pytest.approx(1) and result['centroid_shift_m'] == pytest.approx(1)
    assert result['roof_type_match'] is False and result['ref_roof_type'] == 'gabled' and result['model_roof_type'] == 'hipped'
    assert result['geometry_copy_suspected'] is False


def test_metrics_crm_reference_converts_wgs84_and_reports_image_identity():
    result = agreement.metrics(crm_ref(), M1, S1)
    assert result['iou'] == pytest.approx(1, abs=1e-6) and result['hausdorff_m'] < 1e-4
    assert result['frame'] == 'converted_wgs84' and result['image_identity'] == 'different_image'
    assert result['eave_delta_m'] == pytest.approx(.5) and result['ref_eave_m'] == pytest.approx(2.7)
    # Sub-5 cm outline agreement across different images means one was copied from the other.
    assert result['geometry_copy_suspected'] is True
    assert agreement.metrics(crm_ref(props={'boundary_image_sha256': ''}), M1, S1)['image_identity'] == 'unknown'
    assert agreement.metrics(crm_ref(props={'boundary_image_sha256': SHA}), M1, S1)['image_identity'] == 'same_image'


def test_metrics_cross_scene_reference_and_edges():
    ref = scene_ref(M1, edges=[([-40, 35.2], [-30, 35.2], 'ridge')])
    result = agreement.metrics(ref, N1, S2)
    assert result['iou'] == pytest.approx(1, abs=1e-6) and result['frame'] == 'converted_wgs84'
    assert result['image_identity'] == 'different_image'
    assert (result['line_precision'], result['line_recall'], result['line_kind_precision']) == (1, 1, 1)


def test_line_precision_recall_and_kind_precision():
    edges = [([-40, 35.3], [-30, 35.3], 'ridge'),             # matches ridge L1
             ([-40, 30.2], [-35, 35.2], 'valley'),            # matches hip L2 geometrically, wrong kind
             ([-38, 31], [-32, 31], 'ridge'),                 # nothing nearby
             ([-35.5, 34.866], [-34.5, 35.134], 'ridge'),     # inside the L1 corridor but 15 degrees off
             ([-40, 35.6], [-30, 35.6], 'ridge')]             # parallel but 0.6 m away
    result = agreement.metrics(scene_ref(M1, edges=edges), M1, S1)
    assert (result['n_model_lines'], result['n_ref_edges']) == (3, 5)
    assert result['line_precision'] == pytest.approx(2 / 3, abs=1e-4)
    assert result['line_recall'] == pytest.approx(2 / 5)
    assert result['line_kind_precision'] == pytest.approx(1 / 3, abs=1e-4)
    no_lines = agreement.metrics(scene_ref(M1, edges=edges), {**M1, 'roof_lines': []}, S1)
    assert no_lines['line_precision'] is None and no_lines['line_recall'] == 0 and no_lines['n_model_lines'] == 0


def test_line_metrics_defaults_unchanged_and_distance_kwarg_widens_matches():
    model = [([[0, 0], [10, 0]], 'ridge'), ([[0, 3], [10, 3]], 'hip')]
    ref = [([[2, .8], [6, .8]], 'ridge')]                      # parallel, 0.8 m away: outside the 0.5 m repo rule
    default = agreement.line_metrics(model, ref)
    assert default == {'line_precision': 0., 'line_recall': 0., 'line_kind_precision': 0., 'n_model_lines': 2, 'n_ref_edges': 1,
                       'n_matched_model_lines': 0, 'n_matched_ref_edges': 0}
    assert agreement.line_metrics(model, ref, distance=agreement.LINE_DISTANCE_M, angle=agreement.LINE_ANGLE_DEG) == default
    wide = agreement.line_metrics(model, ref, distance=1.)
    assert (wide['line_precision'], wide['line_recall'], wide['line_kind_precision']) == (.5, 1., .5)
    assert (wide['n_matched_model_lines'], wide['n_matched_ref_edges']) == (1, 1)
    assert agreement.line_metrics(model, [], distance=1.)['line_recall'] is None
    assert agreement.line_metrics([], ref, distance=1.) == {'line_precision': None, 'line_recall': 0., 'line_kind_precision': None,
                                                             'n_model_lines': 0, 'n_ref_edges': 1, 'n_matched_model_lines': 0,
                                                             'n_matched_ref_edges': 0}


def test_lines_match_rules():
    assert agreement.lines_match([[0, 0], [10, 0]], [[2, .4], [6, .4]])
    assert agreement.lines_match([[0, 0], [10, 0]], [[10, .1], [0, .1]])
    assert not agreement.lines_match([[0, 0], [10, 0]], [[2, .6], [6, .6]])
    assert not agreement.lines_match([[0, 0], [10, 0]], [[4, -.3], [6, .3]])
    assert not agreement.lines_match([[0, 0], [0, 0]], [[0, 0], [1, 0]])


def test_candidates_bbox_index_plus_explicit_scenes_read_only(tmp_path):
    root = make_repo(tmp_path, registry_rows=False)
    index = root / 'data/suburbs/mooroolbark/confidence.sqlite'
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    found = agreement.candidates(crm_ref(), root, scene_ids=['s2'])
    assert [(c['scene_id'], c['object_id']) for c in found] in ([('s1', 'm1'), ('s2', 'n1')], [('s2', 'n1'), ('s1', 'm1')])
    by = {c['object_id']: c for c in found}
    assert by['m1']['indexed'] is True and by['m1']['confidence']['band'] == 'low' and by['m1']['stale_index'] is False
    assert by['n1']['indexed'] is False and by['n1']['confidence'] is None and by['n1']['model_signature'] == 'sig2'
    assert all(c['iou_wgs84'] >= agreement.MIN_IOU for c in found) and by['m1']['obj']['id'] == 'm1'
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before
    own = agreement.candidates(scene_ref(M1), root)
    assert sorted((c['scene_id'], c['object_id'], c['indexed']) for c in own) == [('s1', 'm1', True), ('s1', 'm2', False)]
    latest = root / 'data/scenes/s1/latest.json'
    stat = latest.stat()
    os.utime(latest, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert agreement.candidates(crm_ref(), root)[0]['stale_index'] is True


def test_build_writes_agreement_rows_and_tier_summary(tmp_path):
    root = make_repo(tmp_path)
    summary = agreement.build(root, scene_ids=['s2'])
    with eyes_index.connection(root, readonly=True) as db:
        rows = eyes_index.rows(db, 'agreement', version=agreement.VERSION)
        refs = {r['ref_id']: r for r in eyes_index.rows(db, 'references')}
    pairs = {(refs[r['ref_id']]['tier'], r['scene_id'], r['object_id']): r for r in rows}
    assert set(pairs) == {('T1', 's1', 'm1'), ('T1', 's1', 'm2'), ('T1', 's2', 'n1'), ('T2', 's1', 'm1'), ('T2', 's2', 'n1')}
    indexed = pairs[('T1', 's1', 'm1')]
    assert indexed['model_signature'] == 'sig1' and pairs[('T1', 's2', 'n1')]['model_signature'] == 'sig2'
    assert indexed['payload']['model']['band'] == 'low' and indexed['payload']['model']['detector_score'] == .58
    assert indexed['payload']['indexed'] is True and pairs[('T1', 's2', 'n1')]['payload']['model'] is None
    assert indexed['payload']['iou'] == pytest.approx(1) and indexed['payload']['code_sha256'] == \
        hashlib.sha256(Path(agreement.__file__).read_bytes()).hexdigest()
    assert pairs[('T1', 's1', 'm2')]['payload']['best_for_ref'] is False
    assert sum(r['payload']['best_for_ref'] for r in rows if refs[r['ref_id']]['tier'] == 'T1') == 1
    t1, t3 = summary['by_tier']['T1'], summary['by_tier']['T3']
    assert (t1['n_refs'], t1['n_with_candidate'], t1['n_matched_iou_ge_0_5'], t1['n_iou_ge_0_85']) == (1, 1, 1, 1)
    assert t1['share_iou_ge_0_85'] == 1 and t1['share_denominator'] == 1 and t1['median_denominator'] == 1
    assert t1['median_iou'] == pytest.approx(1) and t1['median_hausdorff_m'] == pytest.approx(0, abs=1e-6)
    assert t1['anecdotal'] is True and t1['flag'] == 'anecdotal (n<20)' and t1['accuracy_denominator'] is True
    assert (t3['n_refs'], t3['n_with_candidate'], t3['median_iou'], t3['accuracy_denominator']) == (1, 0, None, False)
    assert 'T4' not in summary['by_tier']
    # The fixture CRM row is geographic(m1) itself, so its best match is flagged as copied geometry.
    assert summary['by_tier']['T2']['n_best_copy_suspected'] == 1 and t1['n_best_copy_suspected'] == 0
    folder = root / 'data/reviews/eyes/agreement' / summary['agreement_id']
    assert json.loads((folder / 'summary.json').read_text())['by_tier'] == summary['by_tier']
    assert len((folder / 'agreement.jsonl').read_text().splitlines()) == len(rows)
    write_json(root / 'data/scenes/s2/latest.json', {**S2, 'objects': []})
    agreement.build(root, scene_ids=['s2'])
    with eyes_index.connection(root, readonly=True) as db:
        assert {r['scene_id'] for r in eyes_index.rows(db, 'agreement', version=agreement.VERSION)} == {'s1'}


def test_build_without_written_registry_uses_in_memory_references(tmp_path):
    root = make_repo(tmp_path, registry_rows=False)
    summary = agreement.build(root, tiers=('T1',))
    assert summary['by_tier']['T1']['n_refs'] == 1 and set(summary['by_tier']) == {'T1'}


def test_cli_build_and_summary(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert agreement.main(['--root', str(root), '--build', '--scene-ids', 's2']) == 0
    built = json.loads(capsys.readouterr().out)
    assert agreement.main(['--root', str(root), '--summary', '--scene-ids', 's2']) == 0
    assert json.loads(capsys.readouterr().out)['by_tier'] == built['by_tier']


def test_metrics_ignore_malformed_model_lines_parts_and_reference_edges():
    ref = scene_ref(M1, edges=[([-40, 35.2], [-30, 35.2], 'ridge')])
    junk = {**M1, 'roof_lines': [{'line_meters': [[-40, 35], [-30, 35]], 'kind': 'ridge'}, {'line_meters': [None, [1, 1]]},
                                 {'line_meters': [['a', 'b'], [1, 1]]}, {'line_meters': [[0], [1]]}, 5, {'line_meters': 'x'}],
            'roof_parts': [3, {'eave_m': True}, {'eave_m': '3'}, {'eave_m': 2.5}, {'ring_z': None}]}
    result = agreement.metrics(ref, junk, S1)
    assert (result['n_model_lines'], result['line_precision'], result['line_recall']) == (1, 1, 1)
    assert result['model_eave_m'] == pytest.approx(2.5)
    bare = agreement.metrics(ref, {**M1, 'roof_lines': 'x', 'roof_parts': {'a': 1}}, S1)
    assert bare['n_model_lines'] == 0 and bare['model_eave_m'] is None
    sparse = {**ref, 'payload': {**ref['payload'], 'edges': [{'kind': 'ridge'}, 7, {'a_xy_m': [0, 0], 'b_xy_m': [1, 1]}]}}
    assert agreement.metrics(sparse, M1, S1)['n_ref_edges'] == 1
    foreign = agreement.metrics(sparse, N1, S2)   # another frame: an edge without lon/lat cannot be converted
    assert foreign['n_ref_edges'] == 0 and foreign['line_precision'] is None
    multi = {**ref, 'payload': {**ref['payload'], 'geometry_local_m': None, 'geometry_wgs84': mapping(MultiPolygon(
        [geographic(M1, S1), geographic({'ring': square(700, 700, 10)}, S1)]))}}
    assert agreement.metrics(multi, M1, S1)['iou'] == pytest.approx(1, abs=1e-6)


def test_build_records_metric_failures_as_error_rows_and_skips_corrupt_index_rows(tmp_path):
    root = make_repo(tmp_path)
    broken = building('broken', [[0, 0], [1, 1]])   # indexed with a valid outline, but the model file ring is degenerate
    for name in ('latest.json', 'models/sig1.json'):
        write_json(root / 'data/scenes/s1' / name, {**S1, 'objects': [M1, M2, FAR, broken]})
    polygon = geographic(M1, S1)
    db = sqlite3.connect(root / 'data/suburbs/mooroolbark/confidence.sqlite')
    db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', ('s1', 'broken', *polygon.bounds, json.dumps(
        {'type': 'Feature', 'geometry': mapping(polygon), 'properties': {'band': 'low', 'model_signature': 'sig1'}})))
    db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', ('s1', 'corrupt', *polygon.bounds, '{not json'))
    db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', ('s1', 'nogeom', *polygon.bounds, json.dumps({'type': 'Feature', 'properties': None})))
    db.commit()
    db.close()
    found = agreement.candidates(scene_ref(M1), root, scene_ids=[None, '../../etc', 's1'])
    assert sorted(c['object_id'] for c in found) == ['broken', 'm1', 'm2']   # corrupt/no-geometry index rows are skipped
    summary = agreement.build(root, tiers=('T1',))
    with eyes_index.connection(root, readonly=True) as db:
        rows = {r['object_id']: r for r in eyes_index.rows(db, 'agreement', version=agreement.VERSION)}
    assert set(rows) == {'m1', 'm2', 'broken'}
    error = rows['broken']['payload']
    assert error['iou'] is None and error['hausdorff_m'] is None and 'linearring' in error['error'].lower()
    assert error['best_for_ref'] is False and error['rank_by_iou'] == 2 and error['image_identity'] == 'same_image'
    assert error['model']['band'] == 'low' and error['code_sha256'] == rows['m1']['payload']['code_sha256']
    assert rows['m1']['payload']['best_for_ref'] is True and 'error' not in rows['m1']['payload']
    t1 = summary['by_tier']['T1']
    assert (t1['n_with_candidate'], t1['n_metric_errors'], t1['median_iou'], t1['median_denominator']) == (1, 1, 1, 1)
    assert (t1['n_matched_iou_ge_0_5'], t1['share_iou_ge_0_85'], t1['share_denominator']) == (1, 1, 1)


@pytest.mark.skipif(not (REPO / 'data/scenes/house-30286/review.json').exists() or not (REPO / 'data/australia_geo.db').exists()
                    or not (REPO / 'data/scenes/mooroolbark-50011498-17-118442-80417/latest.json').exists(),
                    reason='real repository data not present')
def test_real_cluster_crm9_and_human_edges_read_only():
    started = time.monotonic()
    with sqlite3.connect(f"file:{REPO / 'data/australia_geo.db'}?mode=ro", uri=True) as db:
        cursor = db.execute('SELECT * FROM crm_features WHERE id=9')
        record = dict(zip([c[0] for c in cursor.description], cursor.fetchone(), strict=True))
    crm9 = registry.crm_reference(record, {})
    tile = json.loads((REPO / 'data/scenes/mooroolbark-50011498-17-118442-80417/latest.json').read_text())
    house = json.loads((REPO / 'data/scenes/house-30286/latest.json').read_text())
    review = json.loads((REPO / 'data/scenes/house-30286/review.json').read_text())
    scan = next(o for o in tile['objects'] if o['id'] == '44c724fd3480c167')
    model = next(o for o in house['objects'] if o['id'] == '37ff5c02f77869d5')
    edited = next(o for o in review['objects'] if o['id'] == '37ff5c02f77869d5')
    for obj, scene in ((scan, tile), (model, house)):
        result = agreement.metrics(crm9, obj, scene)
        assert result['iou'] > .5 and result['image_identity'] == 'different_image'
    ref = registry.object_reference('house-30286', house, edited, tier='T2', provenance='review:roof_line_edits', pngs={},
                                    source_sha256='x', updated_at=None, independent=True)
    own = agreement.metrics(ref, model, house)
    assert own['n_ref_edges'] == 32 and own['n_model_lines'] == len(model['roof_lines']) and own['image_identity'] == 'same_image'
    assert time.monotonic() - started < 5
