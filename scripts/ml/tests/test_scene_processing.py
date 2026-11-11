import copy
import hashlib
import json
import sqlite3
import time

from scripts.ml import eyes_index
from scripts.ml import scene_processing as s
from scripts.ml.scene_suburb import SUBDIR
from shapely.geometry import box, mapping


def setup_run(tmp_path, monkeypatch, status='running'):
    run_id = '1'*16
    tiles = [[17, 118442+i, 80414] for i in range(3)]
    ids = [f'mooroolbark-{run_id[:8]}-'+'-'.join(map(str, t)) for t in tiles]
    folder = tmp_path/SUBDIR/'runs'/run_id
    folder.mkdir(parents=True)
    (folder/'plan.json').write_text(json.dumps({'tiles': tiles}))
    (folder/'run.json').write_text(json.dumps({'started_at': 1, 'updated_at': 10, 'current': ids[1], 'results': {
        ids[0]: {'status': 'complete', 'objects': {'building': 4, 'solar_panel': 2}, 'confidence': {'medium': 3, 'low': 1}, 'attempts': 1},
        ids[1]: {'status': 'running', 'attempts': 2},
    }}))
    boundary = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'geometry': mapping(box(145, -38, 146, -37)), 'properties': {}}]}
    progress = {'run': run_id, 'status': status, 'counts': {}, 'total': 3, 'bounds': [145, -38, 146, -37],
                'boundary': boundary, 'coverage': {'features': [copy.deepcopy(boundary['features'][0]) for _ in tiles]}}
    monkeypatch.setattr(s, 'progress', lambda root: copy.deepcopy(progress))
    return folder, ids


def feature(sid, oid='a', inside=True):
    return {'type': 'Feature', 'geometry': mapping(box(145.1 if inside else 150, -37.8, 145.2 if inside else 151, -37.7)),
            'properties': {'scene_id': sid, 'object_id': oid, 'model_signature': 'model', 'label': 'Roof',
                           'capture_title': '27 Cambridge Road', 'band': 'low'}}


def test_queue_counts_completed_only_and_does_not_invent_house_total(tmp_path, monkeypatch):
    folder, ids = setup_run(tmp_path, monkeypatch)
    capture = folder/'captures'/ids[1]
    capture.mkdir(parents=True)
    (capture/'image.png').write_bytes(b'imagery')
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0]), feature(ids[0], 'outside', False)]})
    before = (folder/'run.json').read_bytes()
    result = s.inventory(tmp_path)
    assert result['suburb']['counts'] == {'complete': 1, 'running': 1, 'pending': 1}
    assert result['objects'] == {'building': 4, 'solar_panel': 2}
    assert len(result['features']) == 1
    assert result['features'][0]['properties']['review_status'] == 'candidate'
    assert result['queue'][1]['has_capture']
    assert result['queue'][1]['review_url'] is None
    assert result['queue'][2]['elapsed_seconds'] is None
    assert 'houses_total' not in result
    assert (folder/'run.json').read_bytes() == before


def test_interrupted_worker_is_not_shown_as_processing(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch, status='interrupted')
    monkeypatch.setattr(s, 'query', lambda *args: {'features': []})
    result = s.inventory(tmp_path)
    assert result['suburb']['current'] is None
    assert result['queue'][1]['status'] == 'interrupted'
    assert result['suburb']['coverage']['features'][1]['properties']['scene_id'] == ids[1]
    assert result['suburb']['coverage']['features'][1]['properties']['status'] == 'interrupted'


def test_reviews_stay_separate_and_stale_approval_is_not_reused(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    model_feature = feature(ids[0])
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [copy.deepcopy(model_feature), feature(ids[1])]})
    for sid, signature in [(ids[0], 'model'), (ids[1], 'older-model')]:
        folder = tmp_path/'data/scenes'/sid
        folder.mkdir(parents=True)
        (folder/'review.json').write_text(json.dumps({'model_signature': signature,
            'objects': [{'id': 'a', 'label': '27 Cambridge Road · house', 'status': 'confirmed', 'ring': [[1, 2]], 'review_issues': []}]}))
    paths = list((tmp_path/'data/scenes').glob('*/review.json'))
    before = {p: p.read_bytes() for p in paths}
    result = s.inventory(tmp_path)
    assert [f['properties']['review_status'] for f in result['features']] == ['confirmed', 'stale']
    assert result['features'][0]['properties']['label'] == '27 Cambridge Road · house'
    assert result['features'][1]['properties']['label'] == 'Roof'
    assert result['features'][0]['geometry'] == model_feature['geometry']
    assert all(p.read_bytes() == value for p, value in before.items())


def test_no_run_is_an_empty_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(s, 'progress', lambda root: None)
    result = s.inventory(tmp_path)
    assert result['suburb'] is None
    assert result['queue'] == result['features'] == []


# --- eyes sidecar enrichment (diagnostics and reference agreement, never accuracy) ---

def write_eyes(root, sid, oid='a', signature='model'):
    db = eyes_index.connection(root)
    eyes_index.ensure_schema(db)
    ref = {'source_kind': 'review', 'scene_id': sid, 'version': 'v', 'payload': {}}
    eyes_index.replace_rows(db, 'references', [{**ref, 'ref_id': 'r1', 'tier': 'T1', 'object_id': 'ref-1'},
                                                {**ref, 'ref_id': 'r2', 'tier': 'T2', 'object_id': 'ref-2'},
                                                {**ref, 'ref_id': 'r3', 'tier': 'T3', 'object_id': 'ref-3'}])
    row = {'scene_id': sid, 'object_id': oid, 'model_signature': signature, 'version': 'v'}
    eyes_index.replace_rows(db, 'agreement', [
        {**row, 'ref_id': 'r1', 'payload': {'iou': 0.71234, 'hausdorff_m': 2.34567, 'roof_type_match': True, 'line_precision': None,
                                            'line_recall': None, 'image_identity': 'same_image', 'ref_tier': 'T1'}},
        {**row, 'ref_id': 'r2', 'payload': {'iou': 0.82094, 'hausdorff_m': 1.98765, 'roof_type_match': False, 'line_precision': 0.12345,
                                            'line_recall': 0.5, 'image_identity': 'different_image', 'ref_tier': 'T2'}},
        # An agent observation (T3) with the best IoU must never anchor the shown agreement.
        {**row, 'ref_id': 'r3', 'payload': {'iou': 0.99, 'hausdorff_m': 0.1, 'roof_type_match': True, 'line_precision': 1,
                                            'line_recall': 1, 'image_identity': 'same_image', 'ref_tier': 'T3'}}])
    diagnostics = {'topology': {'after': {'physical_planes': 26, 'crease_runs': 51, 'short_junction_links': 16}},
                   'line_refinement': {'matched_edges': 3, 'detected_segments': 7, 'reason': 'complexity-limit'},
                   'crease_contrast': {'share_ge_threshold': 0.6412}, 'edge_support': {'original': {'unsupported_fraction': 0.00791}},
                   'recomputed_matches_stored': True}
    eyes_index.replace_rows(db, 'roof_diagnostics', [{**row, 'payload': diagnostics},
                                                      {**row, 'model_signature': 'older-model', 'payload': {**diagnostics, 'topology': {'after': {'physical_planes': 99}}}}])
    eyes_index.replace_rows(db, 'shadow_bands', [{**row, 'payload': {'band_v1': 'low', 'band_v2': 'medium', 'flip': True}},
                                                  {**row, 'model_signature': 'older-model', 'payload': {'band_v1': 'low', 'band_v2': 'high'}}])
    eyes_index.replace_rows(db, 'cross_capture', [
        {'scene_id': sid, 'object_id': oid, 'other_scene_id': 'house-1', 'other_object_id': 'h1', 'version': 'v',
         'payload': {'iou': 0.90171, 'band': 'low', 'other_band': 'medium', 'hidden_by': 'other'}},
        {'scene_id': 'house-2', 'object_id': 'h2', 'other_scene_id': sid, 'other_object_id': oid, 'version': 'v',
         'payload': {'iou': 0.7, 'band': 'high', 'other_band': 'low', 'hidden_by': 'other'}}])
    # Reviewer line corrections: two saved revisions; the latest wins, the older row stays in the index.
    correction = {'scene_id': sid, 'object_id': oid, 'model_signature': signature, 'version': 'line-corrections-v1'}
    eyes_index.replace_rows(db, 'line_corrections', [
        {**correction, 'review_revision': 34, 'payload': {'review_revision': 34, 'ref_checked': False, 'line_precision': 0.5, 'line_recall': 0.5,
                                                          'n_model_lines': 4, 'n_ref_edges': 4, 'count_ratio': 1.0, 'topology_ref': {'n_components': 1}}},
        {**correction, 'review_revision': 35, 'payload': {'review_revision': 35, 'ref_checked': False, 'line_precision': 0.0, 'line_recall': 0.0,
                                                          'n_model_lines': 35, 'n_ref_edges': 9, 'count_ratio': 3.88889, 'topology_ref': {'n_components': 6}}}])
    db.close()
    return eyes_index.path(root)


def test_inventory_enriches_features_from_the_eyes_index_read_only(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    observed = feature(ids[0])
    observed['properties'].update(observed_segments=4, image_line_mismatch_m=1.3532)
    silent = feature(ids[1], 'b')
    silent['properties'].update(observed_segments=0, image_line_mismatch_m=1.5)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [observed, silent]})
    index = write_eyes(tmp_path, ids[0])
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    result = s.inventory(tmp_path)
    first, second = (f['properties'] for f in result['features'])
    assert first['image_line_mismatch_display'] == first['image_line_mismatch_m'] == 1.3532
    assert second['image_line_mismatch_display'] is None and second['image_line_mismatch_m'] == 1.5, 'Zero observed lines is not a mismatch'
    assert first['eyes']['reference'] == {'tier': 'T2', 'iou': 0.821, 'hausdorff_m': 1.988, 'roof_type_match': False,
                                          'line_precision': 0.123, 'line_recall': 0.5, 'image_identity': 'different_image'}
    assert first['eyes']['diagnostics'] == {'planes': 26, 'crease_runs': 51, 'short_junction_links': 16, 'matched_edges': 3,
                                            'detected_segments': 7, 'line_refinement_reason': 'complexity-limit',
                                            'crease_contrast_share': 0.641, 'edge_unsupported_fraction': 0.008,
                                            'recomputed_matches_stored': True}
    assert first['eyes']['shadow_band_v2'] == 'medium'
    assert first['eyes']['cross_capture'] == {'n_pairs': 2, 'hidden_by': ['house-1/h1'],
                                              'bands': [{'scene_id': 'house-1', 'object_id': 'h1', 'band': 'medium', 'iou': 0.902},
                                                        {'scene_id': 'house-2', 'object_id': 'h2', 'band': 'high', 'iou': 0.7}]}
    assert first['eyes']['line_corrections'] == {'review_revision': 35, 'ref_checked': False, 'line_precision': 0.0, 'line_recall': 0.0,
                                                 'n_model_lines': 35, 'n_ref_edges': 9, 'count_ratio': 3.889, 'n_components_ref': 6}
    assert 'eyes' not in second, 'Objects without sidecar rows carry no eyes payload'
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before
    assert not list(index.parent.glob('*.tmp'))


def test_inventory_without_an_eyes_index_is_unchanged_and_creates_nothing(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0])]})
    result = s.inventory(tmp_path)
    assert 'eyes' not in result['features'][0]['properties']
    assert result['features'][0]['properties']['image_line_mismatch_display'] is None
    assert not eyes_index.path(tmp_path).exists()


def test_inventory_tolerates_a_corrupt_eyes_index(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0])]})
    index = eyes_index.path(tmp_path)
    index.parent.mkdir(parents=True)
    index.write_bytes(b'not a sqlite file' * 64)
    result = s.inventory(tmp_path)
    assert len(result['features']) == 1 and 'eyes' not in result['features'][0]['properties']
    assert index.read_bytes() == b'not a sqlite file' * 64


def test_inventory_tolerates_a_locked_eyes_index(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0])]})
    index = write_eyes(tmp_path, ids[0])
    writer = sqlite3.connect(index, isolation_level=None)
    writer.execute('PRAGMA journal_mode=DELETE')
    writer.execute('BEGIN EXCLUSIVE')
    writer.execute("INSERT INTO checkpoints(id, created_at, version, payload) VALUES('lock', 'now', 'v', '{}')")
    try:
        started = time.monotonic()
        result = s.inventory(tmp_path)
        assert time.monotonic() - started < 3, 'A locked index must not stall the workspace'
    finally:
        writer.execute('ROLLBACK')
        writer.close()
    assert len(result['features']) == 1 and 'eyes' not in result['features'][0]['properties']


def raw_row(index, table, payload, **columns):
    """Insert one row exactly as a misbehaving writer might: NULL or non-JSON payload text."""
    db = sqlite3.connect(index)
    names = eyes_index._columns(table)
    row = {name: None for name in names}
    row.update(columns, payload=payload)
    db.execute(f'INSERT OR REPLACE INTO "{table}"({",".join(names)}) VALUES({",".join("?"*len(names))})', [row[n] for n in names])
    db.commit()
    db.close()


def test_null_or_malformed_sidecar_rows_never_break_the_inventory(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0])]})
    index = write_eyes(tmp_path, ids[0])
    key = {'scene_id': ids[0], 'object_id': 'a', 'model_signature': 'model', 'version': 'v'}
    # A writer that forgets the payload column stores NULL; a crashed writer can leave non-JSON text.
    raw_row(index, 'roof_diagnostics', None, **{**key, 'model_signature': 'other-model'})
    raw_row(index, 'cross_capture', None, scene_id=ids[0], object_id='a', other_scene_id='house-9', other_object_id='h9', version='v')
    raw_row(index, 'shadow_bands', '{not json', **{**key, 'model_signature': 'broken-model'})
    raw_row(index, 'agreement', '"a string, not an object"', ref_id='r9', **key)
    result = s.inventory(tmp_path)
    eyes = result['features'][0]['properties']['eyes']
    # Healthy rows in every table still surface; only the broken rows are skipped.
    assert eyes['reference']['tier'] == 'T2' and eyes['reference']['iou'] == 0.821
    assert eyes['diagnostics']['planes'] == 26
    assert eyes['shadow_band_v2'] == 'medium'
    assert eyes['cross_capture']['n_pairs'] == 2
    assert all(pair['iou'] is not None and pair['band'] for pair in eyes['cross_capture']['bands'])


def test_a_broken_sidecar_table_does_not_hide_the_other_tables(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0])]})
    index = write_eyes(tmp_path, ids[0])
    db = sqlite3.connect(index)
    db.execute('DROP TABLE shadow_bands')  # an index built by an older writer lacks a table
    db.commit()
    db.close()
    eyes = s.inventory(tmp_path)['features'][0]['properties']['eyes']
    assert 'shadow_band_v2' not in eyes
    assert eyes['diagnostics']['planes'] == 26 and eyes['reference']['tier'] == 'T2' and eyes['cross_capture']['n_pairs'] == 2
    assert eyes['line_corrections']['review_revision'] == 35


def test_an_index_without_the_line_corrections_table_drops_only_that_signal(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0])]})
    index = write_eyes(tmp_path, ids[0])
    db = sqlite3.connect(index)
    db.execute('DROP TABLE line_corrections')  # an index last written before the table existed
    db.commit()
    db.close()
    eyes = s.inventory(tmp_path)['features'][0]['properties']['eyes']
    assert 'line_corrections' not in eyes
    assert eyes['diagnostics']['planes'] == 26 and eyes['reference']['tier'] == 'T2' and eyes['shadow_band_v2'] == 'medium'
    # A NULL payload or a non-integer revision never hides the healthy row.
    index = write_eyes(tmp_path, ids[0])
    key = {'scene_id': ids[0], 'object_id': 'a', 'model_signature': 'model', 'version': 'line-corrections-v1'}
    raw_row(index, 'line_corrections', None, review_revision=36, **key)
    raw_row(index, 'line_corrections', json.dumps({'review_revision': 'x', 'ref_checked': True}), review_revision=None, **key)
    eyes = s.inventory(tmp_path)['features'][0]['properties']['eyes']
    assert eyes['line_corrections']['review_revision'] == 35 and eyes['line_corrections']['ref_checked'] is False


def test_reference_choice_is_deterministic_and_ignores_non_numeric_iou(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0])]})
    index = write_eyes(tmp_path, ids[0])
    key = {'scene_id': ids[0], 'object_id': 'a', 'model_signature': 'model', 'version': 'v'}
    # Same IoU as the T2 row: the confirmed reviewer decision (T1) wins the tie, regardless of row order.
    raw_row(index, 'agreement', json.dumps({'iou': 0.82094, 'hausdorff_m': 0.5, 'ref_tier': 'T1', 'image_identity': 'same_image'}), ref_id='r1', **key)
    raw_row(index, 'agreement', json.dumps({'iou': '0.99', 'ref_tier': 'T1'}), ref_id='r-text', **key)
    reference = s.inventory(tmp_path)['features'][0]['properties']['eyes']['reference']
    assert reference['tier'] == 'T1' and reference['iou'] == 0.821 and reference['hausdorff_m'] == 0.5


def test_no_features_means_no_sidecar_scan(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': []})
    write_eyes(tmp_path, ids[0])
    monkeypatch.setattr(eyes_index, 'connection', lambda *a, **k: (_ for _ in ()).throw(AssertionError('index opened for zero features')))
    assert s.inventory(tmp_path)['features'] == []


# --- summary: progress strip, queue and object totals without the expensive inventory sources ---

def forbidden(name):
    def raise_(*args, **kwargs):
        raise AssertionError(f'{name} must not be used by summary()')
    return raise_


def test_summary_reads_progress_and_queue_without_inventory_sources(tmp_path, monkeypatch):
    folder, ids = setup_run(tmp_path, monkeypatch)
    capture = folder/'captures'/ids[1]
    capture.mkdir(parents=True)
    (capture/'image.png').write_bytes(b'imagery')
    write_eyes(tmp_path, ids[0])
    monkeypatch.setattr(s, 'query', forbidden('scene_confidence.query'))
    monkeypatch.setattr(s, 'load_index', forbidden('the building address index'))
    monkeypatch.setattr(s, 'catalog', forbidden('the address catalog'))
    monkeypatch.setattr(s, 'apply', forbidden('address resolution'))
    monkeypatch.setattr(eyes_index, 'connection', forbidden('the eyes index'))
    monkeypatch.setattr(sqlite3, 'connect', forbidden('sqlite'))
    before = (folder/'run.json').read_bytes()
    result = s.summary(tmp_path)
    assert set(result) == {'suburb', 'queue', 'objects', 'counts', 'fetched_at'}
    assert 'features' not in result and 'address_counts' not in result
    assert result['suburb']['counts'] == {'complete': 1, 'running': 1, 'pending': 1}
    assert result['suburb']['current'] == ids[1] and result['suburb']['updated_at'] == 10
    assert result['objects'] == {'building': 4, 'solar_panel': 2}
    # Band counts come from each completed tile's checkpoint, never from the roof query.
    assert result['counts'] == {'medium': 3, 'low': 1}
    assert result['queue'][1]['has_capture'] and result['queue'][1]['review_url'] is None
    assert result['queue'][0]['review_url'].startswith('/scenes?scene='+ids[0])
    assert result['suburb']['coverage']['features'][1]['properties'] == {'status': 'running', 'scene_id': ids[1], 'order': 2}
    assert (folder/'run.json').read_bytes() == before


def test_summary_matches_the_inventory_progress_queue_and_objects(tmp_path, monkeypatch):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0])]})
    full = s.inventory(tmp_path)
    brief = s.summary(tmp_path)
    for key in ('suburb', 'queue', 'objects'):
        assert brief[key] == full[key], key
    assert list(full) == ['features', 'counts', 'address_counts', 'suburb', 'queue', 'objects', 'fetched_at'], 'inventory shape is unchanged'


def test_summary_without_a_run_is_an_empty_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(s, 'progress', lambda root: None)
    result = s.summary(tmp_path)
    assert result['suburb'] is None
    assert result['queue'] == [] and result['objects'] == {} and result['counts'] == {}


def test_cli_summary_flag_prints_the_summary_and_the_default_prints_the_inventory(tmp_path, monkeypatch, capsys):
    _, ids = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(s, 'query', lambda *args: {'features': [feature(ids[0])]})
    s.main(['--summary', '--root', str(tmp_path)])
    brief = json.loads(capsys.readouterr().out)
    assert set(brief) == {'suburb', 'queue', 'objects', 'counts', 'fetched_at'}
    s.main(['--root', str(tmp_path)])
    out = capsys.readouterr().out
    assert ', ' not in out.split('"capture_title"')[0].split('"features"')[0], 'compact separators are unchanged'
    assert len(json.loads(out)['features']) == 1
