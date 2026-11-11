import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scripts.ml import eyes_index
from scripts.ml import scene_roof_diagnostics as diag

REPO_ROOT = Path(__file__).resolve().parents[3]
# Identity-like frame: 0.25 m/px, origin at the image centre, y up in metres.
P2M = [[.25, 0], [0, -.25], [-32, 32]]
M2P = [[4, 0], [0, -4], [128, 128]]
RECT_PX = [[64, 80], [192, 80], [192, 176], [64, 176]]          # 32 m x 24 m in local metres
SMALL_PX = [[20, 200], [60, 200], [60, 240], [20, 240]]          # 10 m x 10 m on blank background
# The brightness step drawn by _image(step=True) sits at pixel x=128 -> local x=0.
LINES = [{'line_meters': [[0, -10], [0, 10]], 'kind': 'ridge'},
         {'line_meters': [[-12, 0], [-4, 0]], 'kind': 'hip'}]


def _image(step=False):
    pixels = np.full((256, 256, 3), 60, np.uint8)
    pixels[80:176, 64:192] = 200
    if step:
        pixels[80:176, 128:192] = 150
    return Image.fromarray(pixels, 'RGB')


def _building(object_id, ring, lines=None, **extra):
    return {'id': object_id, 'kind': 'building', 'label': 'building', 'status': 'candidate', 'ring': ring,
            'model': {'height_m': 3.2}, **({'roof_lines': lines} if lines else {}), **extra}


def _scene(objects, image, owned=None):
    scene = {'id': 'synthetic', 'title': 'synthetic', 'schema_version': 1, 'model_signature': 'sig-1',
             'image_sha256': hashlib.sha256(_png(image)).hexdigest(), 'image_width': 256, 'image_height': 256,
             'pixel_to_meters': P2M, 'meters_to_pixel': M2P, 'centroid': {'lon': 145.31, 'lat': -37.79}, 'objects': objects}
    if owned is not None:
        scene['owned_object_ids'] = owned
    return scene


def _png(image):
    import io
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _write_scene(root, scene, image):
    folder = root / 'data/scenes' / scene['id']
    folder.mkdir(parents=True)
    (folder / f"{scene['image_sha256']}.png").write_bytes(_png(image))
    (folder / 'latest.json').write_text(json.dumps(scene))
    return root


def _strip_volatile(payload):
    payload = dict(payload)
    payload.pop('computed_at')
    payload.pop('elapsed_seconds')
    return payload


def test_diagnose_object_is_deterministic_and_compact():
    image = _image(step=True)
    obj = _building('b1', RECT_PX, lines=LINES)
    scene = _scene([obj], image)
    first = diag.diagnose_object(obj, scene, image)
    second = diag.diagnose_object(obj, scene, image)
    assert _strip_volatile(first) == _strip_volatile(second)
    assert 'error' not in first
    assert first['version'] == diag.VERSION == 'roof-diagnostics-v1'
    assert first['code_sha256'] == hashlib.sha256(Path(diag.__file__).read_bytes()).hexdigest()
    assert first['elapsed_seconds'] >= 0 and first['computed_at']
    assert set(first['audit']) == {'passed', 'issues', 'coverage', 'max_planarity_error_m', 'max_seam_error_m', 'drainage'}
    assert set(first['audit']['drainage']) == {'status', 'passed', 'sample_count'}
    assert first['audit']['passed'] is True
    assert set(first['line_refinement']) == {'accepted', 'reason', 'detected_segments', 'matched_edges', 'weighted_residual_before',
                                             'weighted_residual_after', 'max_shift_m', 'conflicting_matches_removed'}
    assert set(first['topology']) == {'accepted', 'reason', 'before', 'after', 'candidates'}
    cost_keys = {'score', 'image_line_mismatch_m', 'observed_segments', 'physical_planes', 'crease_runs', 'junctions',
                 'short_junction_links', 'small_planes'}
    assert set(first['topology']['before']) == cost_keys and set(first['topology']['after']) == cost_keys
    assert isinstance(first['ends'], list) and first['ends']
    assert all(set(end) == {'hip_support', 'gable_support', 'image_choice', 'reason', 'selected'} for end in first['ends'])
    assert first['ends_source'] in ('topology', 'auto-captured')
    assert first['roof_type_recomputed'] in ('hipped', 'gabled', 'mixed')
    assert isinstance(first['form_uncertain'], bool)
    assert first['recomputed_matches_stored'] is None       # nothing stored on the synthetic object
    text = json.dumps(first)
    assert len(text) < 20000
    for heavy in ('"vertices"', '"faces"', '"edge_meters"', '"hip_lines"', '"gable_lines"', '"geometry_audit"'):
        assert heavy not in text


def test_edge_support_high_on_rectangle_and_low_on_blank():
    gray = np.asarray(_image().convert('L'))
    edges = diag.canny_edges(gray)
    on = diag.edge_support(RECT_PX, edges)
    assert len(on['per_edge']) == 4 and len(on['vertex_distance_px']) == 4
    assert on['median'] >= .9 and on['unsupported_fraction'] == 0
    assert max(on['vertex_distance_px']) <= diag.EDGE_TOLERANCE_PX
    off = diag.edge_support(SMALL_PX, edges)
    assert off['median'] == 0 and off['unsupported_fraction'] == 1
    assert min(off['vertex_distance_px']) > diag.EDGE_TOLERANCE_PX
    # Through the public entry point both views are reported separately.
    image = _image()
    payload = diag.diagnose_object(_building('b1', RECT_PX), _scene([], image), image)
    assert payload['edge_support']['original']['median'] >= .9
    assert payload['edge_support']['clahe']['median'] >= .9
    assert payload['edge_support']['tolerance_px'] == 1.5 and payload['edge_support']['unsupported_threshold'] == .3


def test_crease_contrast_detects_brightness_step():
    result = diag.crease_contrast(LINES, M2P, _image(step=True))
    assert result['n_lines'] == 2 and result['threshold_l'] == 6 and result['band_m'] == 1
    step, flat = result['per_line']
    assert step['kind'] == 'ridge' and step['delta_l'] >= 6
    assert flat['kind'] == 'hip' and flat['delta_l'] < 1
    assert result['share_ge_threshold'] == .5
    assert diag.crease_contrast([], M2P, _image())['share_ge_threshold'] is None


def test_degenerate_ring_records_error_row(tmp_path):
    image = _image()
    obj = _building('bad', [[64, 80], [192, 80]])
    scene = _scene([obj], image, owned=['bad'])
    payload = diag.diagnose_object(obj, scene, image)
    assert payload['error'] and payload['stage'] == 'propose_roof'
    assert 'edge_support' in payload and 'audit' not in payload
    root = _write_scene(tmp_path, scene, image)
    coverage = diag.run(root, ['synthetic'])
    assert {k: coverage[k] for k in ('attempted', 'succeeded', 'failed', 'recomputed_matches_stored')} == \
        {'attempted': 1, 'succeeded': 0, 'failed': 1, 'recomputed_matches_stored': 0}
    with eyes_index.connection(root, readonly=True) as db:
        rows = eyes_index.rows(db, 'roof_diagnostics', scene_id='synthetic')
    assert len(rows) == 1 and rows[0]['payload']['error'] and rows[0]['model_signature'] == 'sig-1'


def test_run_skips_non_building_respects_owned_and_writes_only_under_root(tmp_path):
    image = _image()
    objects = [_building('own', RECT_PX), _building('other', SMALL_PX),
               {'id': 't1', 'kind': 'tree', 'label': 'tree', 'ring': SMALL_PX, 'model': {'height_m': 6}}]
    scene = _scene(objects, image, owned=['own', 't1'])
    root = _write_scene(tmp_path, scene, image)
    before = set(root.rglob('*'))
    coverage = diag.run(root, ['synthetic'])
    assert coverage['attempted'] == 1 and coverage['succeeded'] == 1 and coverage['failed'] == 0
    with eyes_index.connection(root, readonly=True) as db:
        rows = eyes_index.rows(db, 'roof_diagnostics')
    assert {r['object_id'] for r in rows} == {'own'}
    assert all(r['version'] == diag.VERSION and r['model_signature'] == 'sig-1' for r in rows)
    coverage = diag.run(root, ['synthetic'], only_owned=False)
    assert coverage['attempted'] == 2
    with eyes_index.connection(root, readonly=True) as db:
        rows = eyes_index.rows(db, 'roof_diagnostics')
    assert {r['object_id'] for r in rows} == {'own', 'other'}
    created = {p.relative_to(root).parts for p in set(root.rglob('*')) - before}
    eyes = ('data', 'reviews', 'eyes')
    assert ('data', 'reviews', 'eyes', 'eyes.sqlite') in created
    assert all(parts[:3] == eyes or (p.is_dir() and eyes[:len(parts)] == parts)
               for parts in created for p in [root.joinpath(*parts)])
    # Opening only root-relative databases is asserted by test_run_opens_sqlite_only_under_the_root
    # (a repo-index stat comparison is flaky while other writers share data/reviews/eyes).


def test_recomputed_matches_stored_uses_the_production_path(tmp_path):
    from scripts.ml.scene_house import add_roof_estimates
    image = _image()
    scene = _scene([_building('b1', RECT_PX)], image)
    add_roof_estimates(scene, image)
    obj = json.loads(json.dumps(scene['objects'][0]))
    assert obj['roof_annotation']
    payload = diag.diagnose_object(obj, scene, image)
    assert payload['recomputed_matches_stored'] is True
    assert payload['roof_type_recomputed'] == obj['roof_type'] == payload['roof_type_stored']
    assert payload['crease_contrast']['n_lines'] == len(obj['roof_lines'])
    obj['roof_annotation']['vertices'][0][2] += .01
    assert diag.diagnose_object(obj, scene, image)['recomputed_matches_stored'] is False


def test_missing_scene_is_reported_without_rows(tmp_path):
    coverage = diag.run(tmp_path, ['absent'])
    assert coverage['attempted'] == 0 and coverage['scenes']['absent']['error']
    assert not eyes_index.path(tmp_path).exists() or not eyes_index.fetch_object(tmp_path, 'absent', 'x')['roof_diagnostics']


def test_cli_writes_coverage_json(tmp_path, capsys):
    image = _image()
    root = _write_scene(tmp_path, _scene([_building('b1', RECT_PX)], image, owned=['b1']), image)
    assert diag.main(['--root', str(root), '--scenes', 'synthetic']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['version'] == diag.VERSION and report['attempted'] == 1 and report['succeeded'] == 1


REAL_TILE = REPO_ROOT / 'data/scenes/mooroolbark-50011498-17-118442-80417/latest.json'


@pytest.mark.skipif(not REAL_TILE.exists(), reason='cluster tile not present')
def test_real_tile_object_loads_read_only():
    scene, image = diag.load_scene('mooroolbark-50011498-17-118442-80417', REPO_ROOT)
    assert image.size == (scene['image_width'], scene['image_height'])
    owned = diag.building_objects(scene, only_owned=True)
    assert {o['id'] for o in owned} <= set(scene['owned_object_ids'])
    assert all(o['kind'] == 'building' for o in owned)
    assert len(diag.building_objects(scene, only_owned=False)) >= len(owned)


# --- Adversarial-review regressions -------------------------------------------------------------

def test_numpy_scalars_in_fitter_reports_are_serialised(tmp_path, monkeypatch):
    from scripts.ml import scene_roof
    original = scene_roof.select_roof_ends

    def numpy_ends(*args, **kwargs):
        parts, decisions = original(*args, **kwargs)
        ends = [{**end, 'hip_support': np.float32(.25), 'gable_support': np.float64(.5), 'selected': end.get('selected'),
                 'flag': np.bool_(True), 'edge': np.array([1, 2])} for end in decisions['ends']]
        return parts, {**decisions, 'ends': ends}
    monkeypatch.setattr(scene_roof, 'select_roof_ends', numpy_ends)
    # The uniform 768 m² fixture reads as a flat deck (roof-form-flat-v1); keep the pitched path so end decisions exist.
    monkeypatch.setattr(scene_roof, 'flat_evidence', lambda *a, **k: {'decision': 'pitched', 'score': 0, 'features': {}, 'reasons': []})
    image = _image()
    root = _write_scene(tmp_path, _scene([_building('b1', RECT_PX)], image), image)
    coverage = diag.run(root, ['synthetic'])
    assert coverage['succeeded'] == 1
    with eyes_index.connection(root, readonly=True) as db:
        payload = eyes_index.rows(db, 'roof_diagnostics')[0]['payload']
    assert payload['ends'] and payload['ends'][0]['hip_support'] == pytest.approx(.25)
    assert scene_roof.select_roof_ends is numpy_ends          # the capture wrapper restored the binding


def test_unexpected_fitter_exception_is_an_error_row_not_a_crash(tmp_path, monkeypatch):
    from scripts.ml import scene_roof

    def broken(*args, **kwargs):
        raise IndexError('synthetic fitter bug')
    monkeypatch.setattr(scene_roof, 'propose_roof', broken)
    image = _image()
    root = _write_scene(tmp_path, _scene([_building('b1', RECT_PX), _building('b2', SMALL_PX)], image), image)
    coverage = diag.run(root, ['synthetic'])
    assert (coverage['attempted'], coverage['failed']) == (2, 2)
    with eyes_index.connection(root, readonly=True) as db:
        rows = eyes_index.rows(db, 'roof_diagnostics')
    assert len(rows) == 2
    assert all(r['payload']['stage'] == 'propose_roof' and r['payload']['errors'][0]['unexpected'] is True for r in rows)
    assert all('edge_support' in r['payload'] for r in rows)


def test_scene_without_model_signature_is_a_scene_error(tmp_path):
    image = _image()
    scene = _scene([_building('b1', RECT_PX)], image)
    scene.pop('model_signature')
    root = _write_scene(tmp_path, scene, image)
    coverage = diag.run(root, ['synthetic', 'absent'])
    assert coverage['attempted'] == 0 and coverage['scenes_requested'] == 2 and coverage['scenes_failed'] == 2
    assert 'model_signature' in coverage['scenes']['synthetic']['error']


def test_crease_contrast_isolates_a_malformed_line():
    bad = [{'line_meters': [[0, 0, 1], [1, 1, 1]], 'kind': 'ridge'}, {'kind': 'hip'}]
    result = diag.crease_contrast(bad + LINES, M2P, _image(step=True))
    assert result['n_lines'] == 4 and result['n_measured'] == 2 and result['n_errors'] == 2
    assert result['per_line'][0]['error'] and result['per_line'][1]['error']
    assert result['share_ge_threshold'] == .5


def test_failed_audit_is_kept_on_the_error_row(monkeypatch):
    from scripts.ml import scene_roof
    original = scene_roof.audit_roof

    def complexity_limited(*args, **kwargs):
        return {**original(*args, **kwargs), 'drainage': {'status': 'not-assessed-complexity-limit', 'passed': None}}
    monkeypatch.setattr(scene_roof, 'audit_roof', complexity_limited)
    # The uniform 768 m² fixture reads as a flat deck (roof-form-flat-v1); runoff is only assessed on the pitched path.
    monkeypatch.setattr(scene_roof, 'flat_evidence', lambda *a, **k: {'decision': 'pitched', 'score': 0, 'features': {}, 'reasons': []})
    image = _image()
    payload = diag.diagnose_object(_building('b1', RECT_PX), _scene([], image), image)
    assert payload['stage'] == 'propose_roof' and 'runoff' in payload['error']
    assert payload['audit']['drainage'] == {'status': 'not-assessed-complexity-limit', 'passed': None, 'sample_count': None}
    assert scene_roof.audit_roof is complexity_limited


def test_roof_form_model_input_is_recorded(tmp_path, monkeypatch):
    image = _image()
    monkeypatch.setenv('ROOF_FORM_MODEL', str(tmp_path / 'absent.json'))
    call = diag.diagnose_object(_building('b1', RECT_PX), _scene([], image), image)['call']
    assert call['roof_form_model'] == {'path': str(tmp_path / 'absent.json'), 'active': False, 'sha256': None}
    model = tmp_path / 'roof-form.json'
    model.write_text(json.dumps({'validated': True, 'feature_names': ['hip_support', 'gable_support', 'support_difference'],
                                 'weights': [0, 1, 1, 1], 'mean': [0, 0, 0], 'scale': [1, 1, 1]}))
    monkeypatch.setenv('ROOF_FORM_MODEL', str(model))
    call = diag.diagnose_object(_building('b1', RECT_PX), _scene([], image), image)['call']
    assert call['roof_form_model'] == {'path': str(model), 'active': True, 'sha256': hashlib.sha256(model.read_bytes()).hexdigest()}


def test_rows_carry_owned_flag_review_state_and_building_denominators(tmp_path):
    image = _image()
    objects = [_building('own', RECT_PX), _building('other', SMALL_PX)]
    scene = _scene(objects, image, owned=['own'])
    root = _write_scene(tmp_path, scene, image)
    review = {'model_signature': 'sig-1', 'objects': [
        {'id': 'own', 'kind': 'building', 'status': 'needs_adjustment', 'geometry_reviewed': False,
         'roof_line_edits': {'vertices': [[0, 0, 3]], 'edges': []}}]}
    (root / 'data/scenes/synthetic/review.json').write_text(json.dumps(review))
    coverage = diag.run(root, ['synthetic'], only_owned=False)
    assert coverage['buildings_total'] == 2 and coverage['scenes']['synthetic']['buildings_total'] == 2
    with eyes_index.connection(root, readonly=True) as db:
        payloads = {r['object_id']: r['payload'] for r in eyes_index.rows(db, 'roof_diagnostics')}
    assert payloads['own']['owned'] is True and payloads['other']['owned'] is False
    assert payloads['own']['review'] == {'status': 'needs_adjustment', 'geometry_reviewed': False,
                                         'roof_line_edits_present': True, 'same_model_signature': True}
    assert payloads['other']['review'] is None
    assert payloads['own']['roof_line_edits_present'] is False    # the model object itself carries no edits


def _confidence_index(root, roofs):
    import sqlite3
    folder = root / 'data/suburbs/mooroolbark'
    folder.mkdir(parents=True)
    db = sqlite3.connect(folder / 'confidence.sqlite')
    db.execute('CREATE TABLE scenes(id TEXT PRIMARY KEY, signature TEXT, mtime INTEGER, bounds TEXT)')
    db.execute('CREATE TABLE roofs(scene_id TEXT, object_id TEXT, west REAL, south REAL, east REAL, north REAL, payload TEXT)')
    for scene_id in dict.fromkeys(s for s, _ in roofs):
        db.execute('INSERT INTO scenes VALUES(?,?,?,?)', (scene_id, 'sig', 0, '[]'))
    for index, (scene_id, profile) in enumerate(roofs):
        feature = {'type': 'Feature', 'properties': {'profile': profile}, 'geometry': None}
        db.execute('INSERT INTO roofs VALUES(?,?,0,0,0,0,?)', (scene_id, f'o{index}', json.dumps(feature)))
    db.commit()
    db.close()


def test_owned_scene_ids_filters_by_profile_and_cli_reports_population(tmp_path, capsys):
    _confidence_index(tmp_path, [('tile-a', '53ea49d5aa'), ('tile-a', '53ea49d5aa'), ('house-1', '27cd210e')])
    assert diag.owned_scene_ids(tmp_path) == ['house-1', 'tile-a']
    assert diag.owned_scene_ids(tmp_path, profile='53ea49d5') == ['tile-a']
    assert diag.main(['--root', str(tmp_path), '--all-owned', '--profile', '53ea49d5']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['population'] == {'selection': 'confidence-index', 'profile': '53ea49d5', 'scenes': 1,
                                    'indexed_roofs_by_profile': {'53ea49d5aa': 2}, 'only_owned': True}
    assert report['scenes_failed'] == 1                     # tile-a has no scene folder in this synthetic root
    assert diag.main(['--root', str(tmp_path), '--all-owned']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['population']['indexed_roofs_by_profile'] == {'53ea49d5aa': 2, '27cd210e': 1}


def test_run_opens_sqlite_only_under_the_root(tmp_path, monkeypatch):
    import sqlite3
    opened, connect = [], sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append(str(database))
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', spy)
    image = _image()
    root = _write_scene(tmp_path, _scene([_building('b1', RECT_PX)], image), image)
    diag.run(root, ['synthetic'])
    assert opened and all(str(tmp_path) in database for database in opened)
    assert all('data/reviews/eyes/eyes.sqlite' in database for database in opened)


def test_worker_pool_matches_sequential_rows(tmp_path):
    image = _image()
    for name in ('pool-a', 'pool-b'):
        scene = {**_scene([_building('b1', RECT_PX)], image), 'id': name}
        _write_scene(tmp_path, scene, image)
    coverage = diag.run(tmp_path, ['pool-a', 'pool-b', 'pool-a'], workers=2)
    assert (coverage['scenes_requested'], coverage['attempted'], coverage['succeeded']) == (2, 2, 2)
    with eyes_index.connection(tmp_path, readonly=True) as db:
        pooled = {r['scene_id']: _strip_volatile(r['payload']) for r in eyes_index.rows(db, 'roof_diagnostics')}
    assert pooled['pool-a'] == pooled['pool-b']
    diag.run(tmp_path, ['pool-a'])
    with eyes_index.connection(tmp_path, readonly=True) as db:
        sequential = _strip_volatile(eyes_index.rows(db, 'roof_diagnostics', scene_id='pool-a')[0]['payload'])
    assert sequential == pooled['pool-a']
