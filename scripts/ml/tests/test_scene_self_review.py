import copy
import hashlib
import io
import json

import pytest
from PIL import Image
from scripts.ml import scene_self_review as review
from scripts.ml.scene_extract import save_scene
from scripts.ml.scene_roof import propose_roof


@pytest.fixture
def scene(tmp_path):
    image = Image.new('RGB', (200, 200), (130, 100, 80))
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    payload = stream.getvalue()
    ring = [[2, 2], [14, 2], [14, 8], [2, 8]]
    roof = propose_roof(ring, 3, 2, 'gabled')
    obj = {'id': 'roof', 'kind': 'building', 'label': 'Building', 'status': 'candidate',
           'geometry_reviewed': False, 'source': 'sam3-text-onnx', 'ring': [[x*10, y*10] for x, y in ring],
           'model': {'height_m': 3, 'height_verified': False}, 'roof_annotation': roof['annotation'],
           'roof_parts': roof['parts'], 'roof_lines': roof['lines'], 'roof_type': roof['roof_type'],
           'roof_geometry_source': roof['source'], 'roof_geometry_note': roof['note']}
    model = {'id': 'test-house', 'title': 'Test house', 'image_sha256': hashlib.sha256(payload).hexdigest(),
             'image_width': 200, 'image_height': 200, 'pixel_to_meters': [[.1, 0], [0, .1], [0, 0]],
             'meters_to_pixel': [[10, 0], [0, 10], [0, 0]], 'objects': [obj], 'groups': [],
             'centroid': {'lat': -37.7904, 'lon': 145.3128}, 'region': {'split': 'train'}}
    model = save_scene(model, payload, tmp_path)
    return tmp_path, model, image, payload


def codes(row):
    return {i['code'] for i in row['issues']}


def test_plain_image_passes_geometry_but_never_becomes_verified(scene):
    _, model, image, _ = scene
    original = copy.deepcopy(model)
    row = review.audit_object(model['objects'][0], model, image)
    assert row['geometry_passed'] and 'weak_image_evidence' in codes(row)
    assert model == original and not model['objects'][0]['geometry_reviewed']


def test_independent_audit_detects_missing_face_even_if_annotation_is_valid(scene):
    _, model, image, _ = scene
    model['objects'][0]['roof_parts'].pop()
    row = review.audit_object(model['objects'][0], model, image)
    assert not row['geometry_passed'] and 'invalid_surface' in codes(row)


def test_valid_but_different_editor_height_field_is_rejected(scene):
    _, model, image, _ = scene
    for p in model['objects'][0]['roof_annotation']['vertices']:
        if p[2] > 3:
            p[2] += .5
    row = review.audit_object(model['objects'][0], model, image)
    assert row['annotation_audit']['passed'] and row['mesh_audit']['passed']
    assert not row['geometry_passed'] and 'editor_mesh_mismatch' in codes(row)


def test_solar_on_a_neighbour_is_flagged_even_with_parent_id(scene):
    _, model, image, _ = scene
    model['objects'].append({'id': 'solar', 'kind': 'solar_panel', 'status': 'candidate',
                             'parent_id': 'roof', 'ring': [[170, 170], [190, 170], [190, 190], [170, 190]]})
    rows = review.audit_scene(model, image)
    assert 'solar_roof_association' in codes(rows[-1])


def test_simpler_candidate_cannot_erase_observed_lines_or_skip_physics():
    before = {'geometry_passed': True, 'cost': {'score': 4., 'construction_complexity': 3.6,
                                               'image_line_mismatch_m': .4, 'observed_segments': 10}}
    after = {'geometry_passed': True, 'cost': {'score': 2., 'construction_complexity': 1.6,
                                              'image_line_mismatch_m': .4, 'observed_segments': 10}}
    assert review.repair_allowed(before, after)
    after['cost']['image_line_mismatch_m'] = .8
    assert not review.repair_allowed(before, after)
    after['cost']['image_line_mismatch_m'] = .4
    after['geometry_passed'] = False
    assert not review.repair_allowed(before, after)
    after['geometry_passed'] = True
    after['cost']['observed_segments'] = 2
    assert not review.repair_allowed(before, after)


@pytest.mark.parametrize('change,reason', [('review', 'saved_review_preserved'), ('model', 'model_changed'),
                                         ('image', 'image_changed'), ('lock', 'review_in_progress')])
def test_publication_detects_concurrent_edit_and_capture_changes(scene, change, reason):
    root, model, _, payload = scene
    folder = root/'data/scenes'/model['id']
    original = (folder/'latest.json').read_bytes()
    if change == 'review':
        (folder/'review.json').write_text('{"revision":7}')
    elif change == 'model':
        (folder/'latest.json').write_text(original.decode()+'\n')
    elif change == 'image':
        (folder/f'{model["image_sha256"]}.png').write_bytes(b'changed')
    else:
        (folder/'review.lock').mkdir()
    current = (folder/'latest.json').read_bytes()
    result, status = review.publish_repair(model['id'], model, original, payload, root)
    assert result is None and status == reason
    assert (folder/'latest.json').read_bytes() == current
    if change == 'review':
        assert (folder/'review.json').read_text() == '{"revision":7}'


@pytest.mark.parametrize('change', ['outline', 'height', 'approved', 'frame'])
def test_candidate_cannot_change_boundaries_frame_or_review_status(scene, change):
    root, model, _, payload = scene
    original = (root/'data/scenes'/model['id']/'latest.json').read_bytes()
    if change == 'outline':
        model['objects'][0]['ring'][0][0] += 1
    elif change == 'height':
        model['objects'][0]['model']['height_m'] += 1
    elif change == 'approved':
        model['objects'][0]['geometry_reviewed'] = True
    else:
        model['pixel_to_meters'][0][0] *= 2
    with pytest.raises(ValueError, match='outside the roof surface'):
        review.publish_repair(model['id'], model, original, payload, root)


def test_surface_publication_keeps_snapshot_and_outline(scene):
    root, model, _, payload = scene
    folder = root/'data/scenes'/model['id']
    original = (folder/'latest.json').read_bytes()
    model['objects'][0]['roof_geometry_note'] += ' Checked candidate.'
    result, status = review.publish_repair(model['id'], model, original, payload, root)
    assert status == 'published' and result['model_signature'] != json.loads(original)['model_signature']
    assert len(list((folder/'models').glob('*.json'))) == 2
    assert result['objects'][0]['ring'] == json.loads(original)['objects'][0]['ring']
    assert not (folder/'review.json').exists()


@pytest.mark.parametrize('protected', ['review', 'baseline', 'test', 'mixed'])
def test_repair_never_runs_on_reviewed_baselines_or_held_out_images(scene, monkeypatch, protected):
    root, model, _, _ = scene
    folder = root/'data/scenes'/model['id']
    if protected == 'review':
        (folder/'review.json').write_text('{"revision":3}')
    monkeypatch.setattr(review, 'image_split_audit', lambda *args: {
        'status': 'cross_split' if protected == 'mixed' else 'verified',
        'splits': ['test'] if protected == 'test' else ['train']})
    monkeypatch.setattr(review, 'audit_scene', lambda *args: [{
        'object_id': 'roof', 'kind': 'building', 'geometry_passed': False,
        'issues': [review.issue('missing_surface', 'defect', 'No surface')]}])
    monkeypatch.setattr(review, 'repair_object', lambda *args: pytest.fail('Protected input reached repair'))
    original = (folder/'latest.json').read_bytes()
    report, _ = review.run_review([model['id']], root, repair=True,
                                  protected=[model['id']] if protected == 'baseline' else [])
    assert report['repairs_attempted'] == 0 and report['counts']['published_repairs'] == 0
    assert (folder/'latest.json').read_bytes() == original


def test_changed_image_bytes_fail_before_audit(scene):
    root, model, _, _ = scene
    (root/'data/scenes'/model['id']/f'{model["image_sha256"]}.png').write_bytes(b'wrong')
    with pytest.raises(ValueError, match='checksum'):
        review.run_review([model['id']], root)


def test_inconsistent_image_transform_cannot_supply_repair_evidence(scene, monkeypatch):
    root, model, _, payload = scene
    model['meters_to_pixel'][2][0] += 10
    save_scene(model, payload, root)
    monkeypatch.setattr(review, 'repair_object', lambda *args: pytest.fail('Invalid registration reached repair'))
    report, _ = review.run_review([model['id']], root, repair=True)
    assert report['scenes'][0]['error'] == 'invalid_frame'


@pytest.mark.parametrize('error_type', [RuntimeError, review.GEOSException])
def test_one_failed_roof_does_not_discard_other_detected_houses(scene, monkeypatch, error_type):
    from scripts.ml import scene_roof
    from scripts.ml.scene_house import add_roof_estimates
    _, model, image, _ = scene
    first = model['objects'][0]
    for key in review.ROOF_KEYS:
        first.pop(key, None)
    second = copy.deepcopy(first)
    second['id'] = 'second'
    model['objects'].append(second)
    original = scene_roof.propose_roof
    calls = []
    def fail_first(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise error_type('Numerical roof candidate failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(scene_roof, 'propose_roof', fail_first)
    add_roof_estimates(model, image)
    assert len(model['objects']) == 2 and model['objects'][1]['roof_parts']
    assert model['analysis']['roof_failures'] == 1 and model['analysis']['roof_proposals'] == 1
    assert model['analysis']['roof_failure_details'][0]['object_id'] == first['id']
