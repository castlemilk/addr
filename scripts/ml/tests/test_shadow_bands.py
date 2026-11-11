import copy
import hashlib
import io
import json
from contextlib import closing

import pytest
from PIL import Image
from scripts.ml import eyes_index, scene_confidence
from scripts.ml import shadow_bands as sb
from scripts.ml.scene_extract import save_scene
from scripts.ml.scene_region import region_frame


def issue(code, severity='review'):
    return {'code': code, 'severity': severity, 'message': f'{code} message'}


def audit(object_id='roof', *, mesh=True, graph=True, mesh_drainage=True, graph_drainage=True,
          segments=8, mismatch=.2, issues=(), editor_mismatch=False):
    """Mirror scene_self_review.audit_object's folding of drainage into geometry_passed."""
    passed = mesh and graph and mesh_drainage is True and graph_drainage is True
    flags = list(issues)
    if not passed:
        flags.append(issue('invalid_surface', 'defect'))
    if editor_mismatch:
        passed = False
        flags.append(issue('editor_mesh_mismatch', 'defect'))
    if segments < 3:
        flags.append(issue('weak_image_evidence', 'review'))
    return {'object_id': object_id, 'kind': 'building', 'issues': flags, 'geometry_passed': passed, 'area_m2': 80.,
            'mesh_audit': {'passed': mesh, 'drainage': {'passed': mesh_drainage}},
            'annotation_audit': {'passed': graph, 'drainage': {'passed': graph_drainage}},
            'cost': {'observed_segments': segments, 'image_line_mismatch_m': mismatch}}


def test_drainage_none_is_info_not_invalid_surface():
    row = audit(mesh_drainage=None)
    assert scene_confidence.evidence({'score': .9}, row)['band'] == 'low'
    v2 = sb.evidence_v2({'score': .9}, row)
    codes = {i['code']: i['severity'] for i in v2['issues']}
    assert 'invalid_surface' not in codes
    assert codes['drainage_not_assessed'] == 'info'
    assert v2['geometry_passed'] is True and v2['drainage_status'] == 'not_assessed'
    assert v2['band'] == 'high'  # info flags never block the highest band
    assert any('not assessed' in r.lower() for r in v2['reasons'])
    assert sb.evidence_v2({'score': .9}, audit(graph_drainage=None))['band'] == 'high'


def test_real_failures_stay_invalid_in_v2():
    for row in (audit(mesh_drainage=False), audit(graph_drainage=False), audit(mesh=False), audit(graph=False),
                audit(mesh_drainage=None, editor_mismatch=True)):
        v2 = sb.evidence_v2({'score': .9}, row)
        assert v2['band'] == 'low' and v2['geometry_passed'] is False
        codes = {i['code'] for i in v2['issues']}
        assert 'drainage_not_assessed' not in codes or row['mesh_audit']['drainage']['passed'] is None
    assert sb.evidence_v2({'score': .9}, audit(mesh_drainage=False))['drainage_status'] == 'failed'
    assert 'invalid_surface' in {i['code'] for i in sb.evidence_v2({'score': .9}, audit(mesh_drainage=False))['issues']}
    missing = {'object_id': 'r', 'kind': 'building', 'geometry_passed': False, 'issues': [issue('missing_surface', 'defect')]}
    v2 = sb.evidence_v2({'score': .9}, missing)
    assert v2['band'] == 'low' and v2['drainage_status'] == 'unavailable' and v2['image_line_mismatch_m'] is None
    broken = {'object_id': 'r', 'kind': 'building', 'geometry_passed': False, 'issues': [issue('uncheckable_geometry', 'defect')]}
    assert sb.evidence_v2({'score': .9}, broken)['band'] == 'low'


def test_zero_segments_gives_null_mismatch_and_never_high():
    row = audit(segments=0, mismatch=0.)
    assert scene_confidence.evidence({'score': .95}, row)['image_line_mismatch_m'] == 0.
    v2 = sb.evidence_v2({'score': .95}, row)
    assert v2['image_line_mismatch_m'] is None and v2['observed_segments'] == 0
    assert v2['band'] == 'medium' and v2['mismatch_saturated'] is False
    # Even with no review flags at all, zero observed segments cannot reach high.
    row['issues'] = []
    assert sb.evidence_v2({'score': .95}, row)['band'] == 'medium'
    assert sb.evidence_v2({'score': .95}, audit(segments=2, mismatch=.1))['image_line_mismatch_m'] == .1


def test_saturation_flag():
    assert sb.evidence_v2({'score': .9}, audit(mismatch=1.5))['mismatch_saturated'] is True
    assert sb.evidence_v2({'score': .9}, audit(mismatch=1.4))['mismatch_saturated'] is False
    assert sb.evidence_v2({'score': .9}, audit(segments=0, mismatch=1.5))['mismatch_saturated'] is False
    assert sb.SATURATION_M == 1.5


def test_band_rule_matches_v1_when_nothing_changes():
    clean = audit()
    for score in (.94, .8, .79, .7, .65, .6, .1):
        assert sb.evidence_v2({'score': score}, clean)['band'] == scene_confidence.evidence({'score': score}, clean)['band']
    for bad in (None, float('nan'), 'high', 1.1, True):
        assert sb.evidence_v2({'score': bad}, clean)['band'] == 'unassessed'
    cut = audit(issues=[issue('image_cut', 'context')])
    assert sb.evidence_v2({'score': .99}, cut)['band'] == 'low'
    review = audit(issues=[issue('hidden_eaves', 'review')])
    assert sb.evidence_v2({'score': .99}, review)['band'] == 'medium'
    far = audit(mismatch=.4)
    assert sb.evidence_v2({'score': .99}, far)['band'] == 'medium'
    assert sb.evidence_v2({'score': .99}, audit(segments=3, mismatch=.35))['band'] == 'high'


def source(root, scene_id='shadow-test'):
    image = Image.new('RGB', (512, 512), '#777777')
    out = io.BytesIO()
    image.save(out, format='PNG')
    data = out.getvalue()
    def roof(oid, x, score):
        return {'id': oid, 'kind': 'building', 'status': 'candidate', 'label': 'Building', 'score': score,
                'ring': [[x, 100], [x+80, 100], [x+80, 200], [x, 200]], 'model': {}}
    scene = {**region_frame([145.31, -37.7905, 145.311, -37.7895]), 'id': scene_id, 'title': 'Shadow',
             'analysis': {'inference_profile': 'f'*64}, 'image_width': 512, 'image_height': 512,
             'image_sha256': hashlib.sha256(data).hexdigest(),
             'objects': [roof('flip', 20, .9), roof('steady', 120, .7), roof('blind', 220, .95), roof('unowned', 320, .9),
                         {'id': 'panel', 'kind': 'solar_panel', 'status': 'candidate', 'label': 'Solar panel', 'score': .8,
                          'ring': [[30, 110], [40, 110], [40, 120], [30, 120]], 'model': {}}],
             'owned_object_ids': ['flip', 'steady', 'blind', 'panel']}
    return save_scene(scene, data, root)


def fake_audit_scene(scene, image):
    return [audit('flip', mesh_drainage=None), audit('steady', mismatch=.6), audit('blind', segments=0, mismatch=0.),
            audit('unowned'), {'object_id': 'panel', 'kind': 'solar_panel', 'issues': []}]


def test_assess_scene_rows_and_flip_matrix(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, 'audit_scene', fake_audit_scene)
    scene = source(tmp_path)
    folder = tmp_path/'data/scenes/shadow-test'
    before = {p.name: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    written = sb.run(tmp_path, ['shadow-test'])
    assert written == 3
    assert {p.name: p.read_bytes() for p in folder.rglob('*') if p.is_file()} == before  # production untouched, no confidence.json
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        rows = {r['object_id']: r for r in eyes_index.rows(db, 'shadow_bands', scene_id='shadow-test')}
    assert set(rows) == {'flip', 'steady', 'blind'}
    for row in rows.values():
        assert row['version'] == sb.VERSION == 'roof-evidence-v2-shadow'
        assert row['model_signature'] == scene['model_signature']
        payload = row['payload']
        assert len(payload['code_sha256']) == 64 and payload['computed_at']
        assert set(payload) >= {'band_v1', 'band_v2', 'flip', 'reasons_v1', 'reasons_v2', 'mismatch_v2',
                                'mismatch_saturated', 'drainage_status', 'observed_segments', 'detector_score'}
    flip = rows['flip']['payload']
    assert (flip['band_v1'], flip['band_v2'], flip['flip']) == ('low', 'high', True)
    assert flip['drainage_status'] == 'not_assessed' and flip['transition'] == 'low->high'
    steady = rows['steady']['payload']
    assert (steady['band_v1'], steady['band_v2'], steady['flip']) == ('medium', 'medium', False)
    blind = rows['blind']['payload']
    assert blind['mismatch_v2'] is None and blind['band_v2'] == 'medium' and blind['flip'] is False
    matrix = sb.flip_matrix(tmp_path)
    assert matrix['denominator'] == 3 and matrix['version'] == sb.VERSION
    assert matrix['counts'] == {'low->high': 1, 'medium->medium': 2}
    assert matrix['flips'] == [{'scene_id': 'shadow-test', 'object_id': 'flip', 'band_v1': 'low', 'band_v2': 'high',
                                'drainage_status': 'not_assessed'}]
    assert matrix['by_band_v1'] == {'low': 1, 'medium': 2} and matrix['by_band_v2'] == {'high': 1, 'medium': 2}
    assert sb.flip_matrix(tmp_path, ['other'])['denominator'] == 0


def test_assess_scene_respects_ownership_and_verifies_image(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, 'audit_scene', fake_audit_scene)
    scene = source(tmp_path)
    image = Image.new('RGB', (512, 512))
    assert {r['object_id'] for r in sb.assess_scene(scene, image)} == {'flip', 'steady', 'blind'}
    assert {r['object_id'] for r in sb.assess_scene(scene, image, only_owned=False)} == {'flip', 'steady', 'blind', 'unowned'}
    plain = copy.deepcopy(scene)
    del plain['owned_object_ids']
    assert len(sb.assess_scene(plain, image)) == 4
    loaded, loaded_image = sb.load_scene(tmp_path, 'shadow-test')
    assert loaded['model_signature'] == scene['model_signature'] and loaded_image.size == (512, 512)
    (tmp_path/'data/scenes/shadow-test'/f"{scene['image_sha256']}.png").write_bytes(b'changed')
    with pytest.raises(ValueError, match='imagery changed'):
        sb.load_scene(tmp_path, 'shadow-test')


def test_flip_matrix_without_index(tmp_path):
    matrix = sb.flip_matrix(tmp_path)
    assert matrix['denominator'] == 0 and matrix['counts'] == {} and matrix['flips'] == []


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sb, 'audit_scene', fake_audit_scene)
    source(tmp_path)
    assert sb.main(['--root', str(tmp_path), '--scenes', 'shadow-test', '--flip-matrix']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['written'] == 3 and out['flip_matrix']['denominator'] == 3


def test_other_geometry_defects_keep_v2_low_even_with_drainage_none():
    # audit_object can record mesh/annotation audits, append invalid_surface for drainage None, and then fail later
    # (uncheckable_geometry); only a drainage-None fold may be reinterpreted, every other defect still fails geometry.
    row = audit(mesh_drainage=None)
    row['issues'].append(issue('uncheckable_geometry', 'defect'))
    assert scene_confidence.evidence({'score': .9}, row)['band'] == 'low'
    v2 = sb.evidence_v2({'score': .9}, row)
    assert v2['geometry_passed'] is False and v2['band'] == 'low'
    assert 'uncheckable_geometry' in {i['code'] for i in v2['issues']}
    editor = audit(mesh_drainage=None, editor_mismatch=True)
    assert sb.evidence_v2({'score': .9}, editor)['geometry_passed'] is False


def test_drainage_blocked_by_invalid_geometry_is_not_reported_as_failed_runoff():
    no_faces = audit()
    no_faces['mesh_audit'] = {'passed': False, 'issues': ['no-faces'], 'coverage': 0.}
    no_faces['geometry_passed'] = False
    assert sb.drainage_status(no_faces) == 'not_assessed_invalid_geometry'
    assert sb.evidence_v2({'score': .9}, no_faces)['band'] == 'low'
    blocked = audit(mesh=False, mesh_drainage=False)
    blocked['mesh_audit']['drainage']['status'] = 'not-assessed-invalid-geometry'
    assert sb.drainage_status(blocked) == 'not_assessed_invalid_geometry'
    assert sb.evidence_v2({'score': .9}, blocked)['geometry_passed'] is False
    assert sb.drainage_status(audit(mesh_drainage=False)) == 'failed'


def test_rerun_after_scene_edit_and_duplicate_ids_do_not_double_count(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, 'audit_scene', fake_audit_scene)
    scene = source(tmp_path)
    sb.run(tmp_path, ['shadow-test'])
    edited = {**scene, 'title': 'Edited'}  # any save changes model_signature
    edited = save_scene(edited, (tmp_path/'data/scenes/shadow-test'/f"{scene['image_sha256']}.png").read_bytes(), tmp_path)
    assert edited['model_signature'] != scene['model_signature']
    assert sb.run(tmp_path, ['shadow-test', 'shadow-test']) == 3
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        rows = eyes_index.rows(db, 'shadow_bands', scene_id='shadow-test')
    assert len(rows) == 3 and {r['model_signature'] for r in rows} == {edited['model_signature']}
    assert sb.flip_matrix(tmp_path)['denominator'] == 3
    assert sb.flip_matrix(tmp_path, ['shadow-test', 'shadow-test'])['denominator'] == 3
    sb.run(tmp_path, ['shadow-test'], only_owned=False)
    sb.run(tmp_path, ['shadow-test'])
    assert sb.flip_matrix(tmp_path)['denominator'] == 3  # a wider earlier run does not leak into the owned population
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        assert {r['payload']['population_scope'] for r in eyes_index.rows(db, 'shadow_bands')} == {'owned_object_ids'}


def test_load_scene_rejects_non_hex_image_identity(tmp_path):
    scene = source(tmp_path)
    folder = tmp_path/'data/scenes/shadow-test'
    broken = {**scene, 'image_sha256': '../../escape'}
    (folder/'latest.json').write_text(json.dumps(broken))
    with pytest.raises(ValueError, match='imagery identity'):
        sb.load_scene(tmp_path, 'shadow-test')
