import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scripts.ml import eyes_index, roof_review_loop, scene_line_feedback
from scripts.ml import scene_roof_review as review
from scripts.ml.tests.test_scene_roof_diagnostics import (
    LINES,
    RECT_PX,
    _building,
    _image,
    _scene,
    _write_scene,
)


@pytest.fixture
def saved(tmp_path):
    image = _image(step=True)
    obj = _building('roof', RECT_PX, lines=LINES, source='sam3-text-onnx',
                    holes=[[[90, 100], [110, 100], [110, 120], [90, 120]]])
    scene = _scene([obj], image, owned=['roof'])
    _write_scene(tmp_path, scene, image)
    return tmp_path, scene, obj, image


def source_bytes(root):
    return {str(p): p.read_bytes() for p in (root/'data/scenes').rglob('*') if p.is_file()}


def manifest_has_corrections(packet):
    manifest = json.loads(Path(packet['paths']['manifest']).read_text())
    rows = manifest['content']['eyes_snapshot']['line_corrections']
    return len(rows) == 1 and rows[0]['payload']['topology_ref'] == {'n_components': 6}


def test_packet_uses_same_segments_as_card_and_exact_registered_crop(saved, monkeypatch):
    root, scene, obj, image = saved
    before = source_bytes(root)
    segments = [{'points': np.array([[0., -10.], [0., 10.]]), 'length': 20., 'support': .75}]
    calls = []

    def detect(*args):
        calls.append(True)
        return None, segments, None

    monkeypatch.setattr(review, 'evidence_context', detect)
    with eyes_index.connection(root) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'roof_diagnostics', [
            {'scene_id': scene['id'], 'object_id': 'roof', 'model_signature': 'old-model',
             'version': 'old', 'payload': {'image_sha256': scene['image_sha256'],
                                         'topology': {'after': {'observed_segments': 0, 'image_line_mismatch_m': 0.}}}}])
        eyes_index.replace_rows(db, 'line_corrections', [
            {'scene_id': scene['id'], 'object_id': 'roof', 'review_revision': 3, 'model_signature': scene['model_signature'],
             'version': 'line-corrections-v1',
             'payload': {'review_revision': 3, 'ref_checked': False, 'line_precision': 0., 'line_recall': 0., 'count_ratio': 3.8889,
                         'n_model_lines': 35, 'n_ref_edges': 9, 'baseline_matches_snapshot': True, 'image_sha256': scene['image_sha256'],
                         'topology_ref': {'n_components': 6}, 'computed_at': 'now', 'code_sha256': 'c'*64}}])
    packet = review.review_roof(scene['id'], 'roof', root)
    assert len(calls) == 1
    assert packet['geometry']['outline']['holes'] == obj['holes']
    assert packet['geometry']['detected_image_segments'][0]['points'] == [[128., 168.], [128., 88.]]
    assert packet['eyes']['roof_diagnostics'][0]['binding'] == 'stale_model'
    assert packet['eyes']['roof_diagnostics'][0]['topology']['image_line_mismatch_m'] is None
    corrections = packet['eyes']['line_corrections']
    assert len(corrections) == 1 and corrections[0]['binding'] == 'current_model' and corrections[0]['version'] == 'line-corrections-v1'
    assert {k: corrections[0][k] for k in ('review_revision', 'ref_checked', 'line_precision', 'line_recall', 'count_ratio',
                                          'n_model_lines', 'n_ref_edges', 'baseline_matches_snapshot')} == {
        'review_revision': 3, 'ref_checked': False, 'line_precision': 0., 'line_recall': 0., 'count_ratio': 3.8889,
        'n_model_lines': 35, 'n_ref_edges': 9, 'baseline_matches_snapshot': True}
    assert 'topology_ref' not in corrections[0], 'the handoff is compact; the full payload stays in eyes_snapshot'
    assert manifest_has_corrections(packet)
    manifest = json.loads(Path(packet['paths']['manifest']).read_text())
    assert manifest['content']['model_object'] == obj
    assert manifest['content']['eyes_snapshot']['roof_diagnostics'][0]['payload']['topology']['after']['image_line_mismatch_m'] == 0.
    assert manifest['content']['detected_segments_exact'][0]['points_meters'] == segments[0]['points'].tolist()
    render = packet['render']
    points = np.array([[0, 0], [33.2, 99.7], [256, 256]])
    panel = np.c_[points, np.ones(3)] @ np.array(render['source_to_panel'])
    assert np.allclose(np.c_[panel, np.ones(3)] @ np.array(render['panel_to_source']), points)
    expected = image.crop(render['crop_box_pixels']).resize((360, 360))
    with Image.open(packet['paths']['card']) as card:
        assert card.crop((10, 53, 370, 413)).tobytes() == expected.tobytes()
    assert source_bytes(root) == before


def test_queue_keeps_protection_source_identity_and_capture_diversity(saved, monkeypatch):
    root, seed, obj, image = saved
    props = []
    for sid in ('train-a', 'train-b', 'held-out', 'reviewed', 'changed'):
        scene = copy.deepcopy(seed)
        scene['id'] = sid
        scene['objects'].append({**copy.deepcopy(obj), 'id': 'second'})
        scene['owned_object_ids'].append('second')
        _write_scene(root, scene, image)
        if sid == 'reviewed':
            (root/'data/scenes'/sid/'review.json').write_text('{}')
        for building in scene['objects']:
            props.append({'scene_id': sid, 'object_id': building['id'], 'band': 'low',
                          'area_m2': 200, 'model_signature': scene['model_signature'],
                          'observed_segments': 0, 'image_line_mismatch_m': 0.,
                          'geometry_signature': 'stale' if sid == 'changed' else review.geometry_signature(building)})
    monkeypatch.setattr(review, '_confidence_rows', lambda _: props)
    monkeypatch.setattr(roof_review_loop, 'image_split_audit', lambda scene, _: {'status': 'cross_split' if scene['id'] == 'held-out' else 'verified'})
    before = source_bytes(root)
    page = review.low_confidence_roofs(root, limit=1)
    second = review.low_confidence_roofs(root, limit=1, offset=1)
    assert {page['rows'][0]['scene_id'], second['rows'][0]['scene_id']} == {'train-a', 'train-b'}
    assert page['eligible_captures'] == 2 and page['next_offset'] == 1
    assert page['rows'][0]['image_line_mismatch_m'] is None
    assert second['next_offset'] is None
    assert 'Held-out, mixed-split or invalid image frame' in page['selection_skips']
    assert page['selection_skips']['Object geometry changed'] == 2
    assert source_bytes(root) == before


def test_agent_notes_are_immutable_idempotent_discoverable_and_never_labels(saved, monkeypatch):
    root, scene, _, _ = saved
    before = source_bytes(root)
    packet = review.review_roof(scene['id'], 'roof', root)
    fields = {'observed': 'The source contains a brightness step.', 'hypothesis': 'It may support a ridge.',
              'ambiguity': 'Height is unmeasured.', 'next_action': 'Ask a human to inspect the line.'}
    first = review.record_observation(packet['review_id'], root=root, **fields)
    original_bytes = Path(first['path']).read_bytes()
    again = review.record_observation(packet['review_id'], root=root, **fields)
    assert again == first and Path(first['path']).read_bytes() == original_bytes
    note = first['observation']
    assert note['tier'] == 3 and note['training_eligible'] is False and note['human_approval'] is False
    assert review.review_roof(scene['id'], 'roof', root)['observations'] == [note]
    codes = review._codes()
    monkeypatch.setattr(review, '_codes', lambda: {**codes, 'roof_review_pack.py': 'new-renderer-code'})
    refreshed = review.review_roof(scene['id'], 'roof', root)
    assert refreshed['review_id'] != packet['review_id'] and refreshed['observations'] == [note]
    assert not eyes_index.path(root).exists()
    assert source_bytes(root) == before


def test_changed_source_or_review_artifacts_refuse_a_note(saved):
    root, scene, _, _ = saved
    packet = review.review_roof(scene['id'], 'roof', root)
    paths = [root/'data/scenes'/scene['id']/'latest.json', Path(packet['paths']['source_image']),
             Path(packet['paths']['card']), Path(packet['paths']['manifest'])]
    for path in paths:
        original = path.read_bytes()
        if path.suffix == '.json':
            data = json.loads(original)
            data['tampered'] = True
            if path.name == 'manifest.json':
                data['content']['model_object']['kind'] = 'tree'
            path.write_text(json.dumps(data))
        else:
            path.write_bytes(original+b'changed')
        with pytest.raises(ValueError):
            review.record_observation(packet['review_id'], observed='Must not save stale evidence.', root=root)
        path.write_bytes(original)
    assert not list((Path(packet['paths']['manifest']).parent/'observations').glob('*.json'))


def test_saved_line_feedback_is_separate_and_mismatched_identity_unavailable(saved, monkeypatch):
    root, scene, obj, _ = saved
    response = {'schema_version': 1, 'scene_id': scene['id'], 'model_signature': scene['model_signature'],
                'image': {'sha256': scene['image_sha256']}, 'objects': [{'object_id': 'roof', 'feedback': {'edges': []}}]}
    monkeypatch.setattr(scene_line_feedback, 'load_roof_line_feedback', lambda *args: copy.deepcopy(response))
    packet = review.review_roof(scene['id'], 'roof', root, include_feedback=True)
    assert packet['saved_feedback']['state'] == 'available'
    assert len(packet['geometry']['model_roof_lines']) == len(obj['roof_lines'])
    assert hashlib.sha256(Path(packet['paths']['source_image']).read_bytes()).hexdigest() == scene['image_sha256']
    response['model_signature'] = 'another-model'
    stale = review.review_roof(scene['id'], 'roof', root, include_feedback=True)
    assert stale['saved_feedback']['state'] == 'unavailable'
    assert stale['geometry'] == packet['geometry']


def test_boundary_packet_and_card_use_proposed_outline_and_saved_identity(saved, monkeypatch):
    from scripts.ml import roof_boundary_guidance

    root, scene, obj, _ = saved
    before = source_bytes(root)
    candidate = {**obj, 'ring': [[x+10, y] for x, y in obj['ring']], 'roof_type': 'flat',
                 'roof_lines': [], 'roof_annotation': {'vertices': [], 'edges': []}}
    detail = {'changed': True, 'accepted': False, 'reason': 'ambiguous-brightness-edge-comparison',
              'inferred_edges': [], 'photometry_source': 'original_rgb_only'}
    monkeypatch.setattr(roof_boundary_guidance, 'boundary_candidate', lambda *args, **kwargs: (candidate, detail))
    monkeypatch.setattr(review, '_optical_candidate', lambda value, *_: {
        'state': 'available', 'candidate_object': value, 'geometry_audit': {'passed': True},
        'candidate_geometry': {'outline': {'ring': value['ring'], 'holes': value['holes']}}, 'report': None})
    packet = review.review_roof(scene['id'], obj['id'], root, include_boundary_guidance=True)
    assert packet['geometry']['outline']['ring'] == obj['ring']
    assert packet['optical_guidance']['candidate_geometry']['outline']['ring'] == candidate['ring']
    assert packet['boundary_guidance']['roof_geometry_passed']
    assert not packet['boundary_guidance']['accepted']
    manifest = json.loads(Path(packet['paths']['manifest']).read_text())
    assert manifest['content']['model_object'] == obj
    assert manifest['content']['optical_guidance']['candidate_object'] == candidate
    a, b = np.asarray(candidate['ring'][:2])
    panel = np.r_[((a+b)/2), 1.] @ np.asarray(packet['render']['source_to_panel'])
    pixel = np.rint(panel+packet['render']['panel_origins_in_card_pixels'][3]).astype(int)
    with Image.open(packet['paths']['card']) as card:
        assert card.getpixel(tuple(pixel)) == (102, 222, 237)
    assert source_bytes(root) == before


def test_optical_panel_reserves_purple_for_supported_valleys(saved, monkeypatch):
    from PIL import ImageDraw
    from scripts.ml.roof_review_pack import render_card

    root, scene, obj, image = saved
    candidate = {**obj, 'roof_lines': [{'kind': 'valley', 'line_meters': [[x, -6], [x, 6]]} for x in (-8, 8)]}
    optical = {'candidate_object': candidate, 'report': {'accepted': True, 'selected_valleys': [
        {'status': status, 'points_meters': line['line_meters']}
        for status, line in zip(('valley_geometry_only', 'valley_supported'), candidate['roof_lines'], strict=True)]}}
    labels = []
    original_text = ImageDraw.ImageDraw.text

    def draw_text(self, xy, text, *args, **kwargs):
        labels.append(text)
        return original_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', draw_text)
    path = root/'optical.png'
    rendered = render_card(obj, scene, image, path, observations=[], optical_guidance=optical)
    source = np.array([[-8., 0., 1.], [8., 0., 1.]]) @ np.asarray(scene['meters_to_pixel'])
    panel = np.c_[source, np.ones(2)] @ np.asarray(rendered['source_to_panel'])
    pixels = np.rint(panel+rendered['panel_origins_in_card_pixels'][3]).astype(int)
    with Image.open(path) as card:
        assert card.getpixel(tuple(pixels[0])) == (138, 149, 168)
        assert card.getpixel(tuple(pixels[1])) == (212, 160, 255)
    assert any(label.startswith('Fourth panel: grey = valley geometry only; purple = image-supported') for label in labels)


def test_optical_segments_bind_packet_exact_evidence_and_distinct_card_lines(saved, monkeypatch):
    from PIL import ImageDraw

    root, scene, obj, _ = saved
    base = {'points': np.array([[-8., -6.], [-8., 6.]]), 'length': 12., 'support': .72}
    added = {'segment_id': 'optical-1', 'points_meters': [[8., -6.], [8., 6.]],
             'length_m': 12., 'view_origin': 'dog', 'original_rgb_support': .87654321}
    optical = {'state': 'available', 'candidate_object': obj,
               'report': {'accepted': True, 'observations': {'additional_segment_evidence': [added]}}}
    monkeypatch.setattr(review, 'evidence_context', lambda *args: (None, [base], None))
    monkeypatch.setattr(review, '_optical_candidate', lambda *args: optical)
    labels = []
    original_text = ImageDraw.ImageDraw.text

    def draw_text(self, xy, text, *args, **kwargs):
        labels.append(text)
        return original_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', draw_text)
    packet = review.review_roof(scene['id'], obj['id'], root, include_optical_guidance=True)
    projected = packet['geometry']['additional_optical_segments'][0]
    assert projected['points'] == [[160., 152.], [160., 104.]]
    assert projected['view_origin'] == 'dog' and projected['original_rgb_support'] == added['original_rgb_support']
    manifest = json.loads(Path(packet['paths']['manifest']).read_text())
    assert manifest['content']['optical_guidance']['report']['observations']['additional_segment_evidence'] == [added]
    source = np.array([[96., 128., 1.], [160., 128., 1.]])
    render = packet['render']
    pixels = np.rint(source @ np.asarray(render['source_to_panel'])+render['panel_origins_in_card_pixels'][2]).astype(int)
    with Image.open(packet['paths']['card']) as card:
        assert card.getpixel(tuple(pixels[0])) == (255, 210, 89)
        assert card.getpixel(tuple(pixels[1])) == (98, 207, 255)
    assert 'Detected: yellow base / cyan optical' in labels


def test_saved_annotation_graph_precedes_cache_and_flat_fallback_has_no_folds():
    from scripts.ml.roof_review_pack import displayed_roof_lines

    legacy = {'roof_type': 'flat', 'roof_lines': LINES}
    assert displayed_roof_lines(legacy) == []
    annotation = {'vertices': [[1, 2, 3], [4, 5, 3]], 'edges': []}
    assert displayed_roof_lines({**legacy, 'roof_annotation': annotation}) == []
    annotation['edges'] = [{'a': 0, 'b': 1, 'kind': 'ridge'}]
    assert displayed_roof_lines({**legacy, 'roof_annotation': annotation}) == [
        {'kind': 'ridge', 'line_meters': [[1., 2.], [4., 5.]]}]
    assert displayed_roof_lines({**legacy, 'roof_type': 'hipped'}) == LINES
