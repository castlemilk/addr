import copy
import json

import pytest
from PIL import ImageDraw
from scripts.ml import roof_candidate_review as gates
from scripts.ml import roof_review_diagnostics as diagnostics
from scripts.ml import roof_review_loop as loop
from scripts.ml.roof_review_pack import create_pack
from scripts.ml.scene_training import atomic_json
from scripts.ml.tests.test_roof_review_loop import prepared
from scripts.ml.tests.test_scene_self_review import scene as scene
from shapely.geometry import box


def visible_lines(image):
    draw = ImageDraw.Draw(image)
    for a, b in [(20, 53), (63, 96), (106, 139)]:
        draw.line((a, 50, b, 50), fill=(240, 230, 220), width=2)


def test_diagnostics_preserve_source_and_do_not_conclude_blank_roof_is_flat(scene):
    _, model, image, _ = scene
    original = copy.deepcopy(model)
    result = diagnostics.diagnose(model['objects'][0], model, image)
    assert result['primary'] == 'weak_observations'
    assert result['metrics']['image_segments'] == 0
    assert model == original
    assert not {'roof_type', 'geometry_reviewed', 'confidence', 'training_label'}.intersection(result)


def test_clipped_image_routes_to_context_before_topology(scene):
    _, model, image, _ = scene
    model['objects'][0]['ring'][0][0] = 0
    result = diagnostics.diagnose(model['objects'][0], model, image)
    assert result['primary'] == 'capture_context'


def test_reviewer_tree_geometry_does_not_become_model_evidence(scene):
    _, model, image, _ = scene
    roof = model['objects'][0]
    tree = {'id': 'tree', 'kind': 'tree', 'ring': copy.deepcopy(roof['ring']),
            'status': 'candidate', 'source': 'manual'}
    model['objects'].append(tree)
    assert diagnostics.diagnose(roof, model, image)['metrics']['model_canopy_overlap_fraction'] == 0
    tree['source'] = 'sam3-text-onnx'
    result = diagnostics.diagnose(roof, model, image)
    assert result['metrics']['model_canopy_overlap_fraction'] == pytest.approx(1.)
    assert 'occlusion' in [a['code'] for a in result['actions']]


def test_solar_overlap_is_flagged_without_discarding_observations(scene):
    _, model, image, _ = scene
    visible_lines(image)
    model['objects'].append({'id': 'solar', 'kind': 'solar_panel', 'ring': [[20, 45], [140, 45], [140, 55], [20, 55]],
                             'status': 'candidate', 'source': 'sam3-text-onnx'})
    result = diagnostics.diagnose(model['objects'][0], model, image)
    assert result['metrics']['image_segments'] >= 3
    assert result['metrics']['model_solar_line_fraction'] > .9
    assert 'rooftop_objects' in [a['code'] for a in result['actions']]


def test_eave_diagnostic_respects_extractor_margin_and_excludes_centre_ridge(scene):
    _, model, image, _ = scene
    draw = ImageDraw.Draw(image)
    for a, b in [(25, 53), (63, 96), (106, 135)]:
        draw.line((a, 25, b, 25), fill=(240, 230, 220), width=2)
    edge = diagnostics.diagnose(model['objects'][0], model, image)
    assert edge['metrics']['near_eave_line_fraction'] > .8
    _, _, centre, _ = scene
    centre = centre.copy()
    ImageDraw.Draw(centre).rectangle((0, 0, 200, 200), fill=(130, 100, 80))
    visible_lines(centre)
    ridge = diagnostics.diagnose(model['objects'][0], model, centre)
    # The ridge endpoints legitimately enter the boundary band at gable ends;
    # its long interior run must not trigger an uncertain-family diagnosis.
    assert ridge['metrics']['near_eave_line_fraction'] < .2
    assert 'roof_family' not in [a['code'] for a in ridge['actions']]


def test_fuller_capture_requires_containment_and_bounded_area():
    target = box(0, 0, 6, 10)
    assert diagnostics.context_match(target, box(0, 0, 10, 10))['target_coverage'] == 1.
    assert diagnostics.context_match(target, box(5, 0, 15, 10)) is None
    assert diagnostics.context_match(target, box(-100, -100, 100, 100)) is None
    assert diagnostics.context_match(target, target) is None


def test_rejected_preview_never_becomes_a_passing_candidate(scene, monkeypatch):
    _, model, image, _ = scene
    visible_lines(image)
    original = copy.deepcopy(model)
    monkeypatch.setattr(gates, 'candidate_gate', lambda *args: (False, 'Evidence veto'))
    result, candidate = gates.improve(model['objects'][0], model, image)
    assert result['status'] == 'no_safe_gain' and candidate is None
    assert result['after'] is None and result['rejected_preview']['eligible'] is False
    assert not result['rejected_preview']['object']['geometry_reviewed']
    assert model == original
    compact = loop.compact({'rows': [result]})
    assert 'rejected_preview' not in compact['rows'][0]


def test_pack_is_reproducible_and_preserves_models_reviews_and_image(scene, monkeypatch):
    root, model, run, directory = prepared(scene, monkeypatch)
    source = root/'data/scenes'/model['id']
    original = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    report = json.loads((directory/'report.json').read_text())
    report['status'] = 'complete'
    report['rows'][0]['status'] = 'needs_evidence'
    atomic_json(directory/'report.json', report)
    pack = create_pack(root, run)
    result = json.loads((pack/'report.json').read_text())
    assert result['rows'][0]['inspection_status'] == 'ready'
    assert (pack/'roof-00.png').is_file()
    before = {p.name: p.read_bytes() for p in pack.iterdir()}
    assert create_pack(root, run) == pack
    assert {p.name: p.read_bytes() for p in pack.iterdir()} == before
    assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == original
    assert not (source/'review.json').exists()


def test_pack_retains_failed_input_as_visible_record(scene, monkeypatch):
    root, model, run, directory = prepared(scene, monkeypatch)
    report = json.loads((directory/'report.json').read_text())
    report['status'] = 'complete'
    report['rows'][0]['status'] = 'failed'
    atomic_json(directory/'report.json', report)
    (root/'data/scenes'/model['id']/f'{model["image_sha256"]}.png').write_bytes(b'wrong')
    result = json.loads((create_pack(root, run)/'report.json').read_text())
    assert result['rows'][0]['inspection_status'] == 'unavailable'
    assert 'checksum' in result['rows'][0]['error']
