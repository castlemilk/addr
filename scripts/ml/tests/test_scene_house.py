"""add_roof_estimates: default analysis unchanged; evidence views recorded when supplied (CP3)."""
import hashlib

import numpy as np
import pytest
from PIL import Image
from scripts.ml import roof_line_fit
from scripts.ml.scene_extract import polygon_details
from scripts.ml.scene_house import add_roof_estimates

P2M = [[.25, 0], [0, -.25], [-64, 64]]
M2P = [[4, 0], [0, -4], [256, 256]]
RING = [[176, 208], [336, 208], [336, 304], [176, 304]]   # 40 m x 24 m
DEFAULT_KEYS = {'pipeline', 'roof_proposals', 'roof_failures', 'roof_failure_details', 'roof_model', 'boundary_version',
                'topology_version', 'junction_version', 'canopy_version', 'canopy_boundary_code', 'assembly_version',
                'wings_version', 'roof_wings_code', 'roof_assembly_code', 'boundary_code', 'roof_line_code',
                'roof_topology_code', 'roof_junction_code', 'roof_end_code', 'roof_physics_code', 'roof_structure_code',
                'geometry_code', 'roof_optical_guidance', 'roof_optical_guidance_code', 'optical_layers_code'}


def _image():
    pixels = np.full((512, 512, 3), [86, 118, 70], np.uint8)
    pixels[208:304, 176:256] = [205, 200, 196]
    pixels[208:304, 256:336] = [150, 146, 144]
    return Image.fromarray(pixels)


def _scene(image):
    obj = {'id': 'b1', 'kind': 'building', 'ring': RING, 'status': 'candidate', 'geometry_reviewed': False,
           'model': polygon_details(RING, image, np.asarray(P2M), 'building')}
    return {'id': 'synthetic', 'image_width': 512, 'image_height': 512, 'pixel_to_meters': P2M, 'meters_to_pixel': M2P,
            'objects': [obj]}


def test_default_analysis_is_unchanged_without_evidence_views(monkeypatch):
    monkeypatch.chdir(__import__('pathlib').Path(__file__).resolve().parents[3])
    image = _image()
    scene = _scene(image)
    add_roof_estimates(scene, image)
    analysis = scene['analysis']
    assert set(analysis) == DEFAULT_KEYS
    assert analysis['pipeline'] == 'sam3-scene-roof-v12'
    assert analysis['roof_proposals'] == 1 and analysis['roof_failures'] == 0
    assert scene['objects'][0]['roof_form_evidence']['version'] == 'roof-form-flat-v3'
    assert isinstance(scene['objects'][0]['roof_form_uncertain'], bool)
    assert 'evidence_views' not in analysis
    again = _scene(image)
    add_roof_estimates(again, image)
    assert again['analysis'] == analysis
    assert again['objects'][0]['roof_annotation'] == scene['objects'][0]['roof_annotation']


def test_evidence_views_are_recorded_and_flag_the_pipeline(monkeypatch):
    monkeypatch.chdir(__import__('pathlib').Path(__file__).resolve().parents[3])
    image = _image()
    baseline = _scene(image)
    add_roof_estimates(baseline, image)
    view = np.full((512, 512), 90, np.uint8)
    view[256:] = 200
    scene = _scene(image)
    add_roof_estimates(scene, image, evidence_views=[{'name': 'stack-median', 'array': view, 'stack_id': 'stack-1'},
                                                     ('stack-min', view)])
    analysis = scene['analysis']
    assert analysis['pipeline'] == 'sam3-scene-roof-v12+multiview'
    assert analysis['evidence_views'] == [
        {'name': 'stack-median', 'sha256': hashlib.sha256(view.tobytes()).hexdigest(), 'stack_id': 'stack-1'},
        {'name': 'stack-min', 'sha256': hashlib.sha256(view.tobytes()).hexdigest(), 'stack_id': None}]
    assert analysis['extra_views_version'] == roof_line_fit.EXTRA_VIEWS_VERSION
    assert set(analysis) == DEFAULT_KEYS | {'evidence_views', 'extra_views_version'}
    assert {k: v for k, v in analysis.items() if k not in ('pipeline', 'evidence_views', 'extra_views_version')} == \
        {k: v for k, v in baseline['analysis'].items() if k != 'pipeline'}
    assert scene['objects'][0]['roof_annotation'], 'roof still fitted (ungated view lines change nothing)'


@pytest.mark.parametrize('bad', [[('stack-median', np.zeros((64, 64), np.uint8))], ['clahe'], [{'array': np.zeros((512, 512), np.uint8)}]])
def test_invalid_evidence_views_fail_before_any_roof_is_fitted(bad):
    image = _image()
    scene = _scene(image)
    with pytest.raises(ValueError, match='evidence_views|extra_views'):
        add_roof_estimates(scene, image, evidence_views=bad)
    assert 'analysis' not in scene and 'roof_annotation' not in scene['objects'][0]


def test_context_hints_reach_propose_roof_and_are_summarised(monkeypatch):
    monkeypatch.chdir(__import__('pathlib').Path(__file__).resolve().parents[3])
    from scripts.ml import scene_roof
    image = _image()
    calls = []
    original = scene_roof.propose_roof

    def recording(*args, **kwargs):
        calls.append(kwargs.get('context_hint'))
        return original(*args, **kwargs)
    monkeypatch.setattr(scene_roof, 'propose_roof', recording)
    scene = _scene(image)
    hints = {'version': 'building-flags-v2', 'note': 'agent hint, not a label', 'context': {'landuse': 'available'}, 'hints': {'b1': 'commercial'}}
    add_roof_estimates(scene, image, context_hints=hints)
    assert calls == ['commercial']
    summary = scene['analysis']['context_hints']
    assert summary['version'] == 'building-flags-v2' and summary['hinted_objects'] == 1 and summary['by_hint'] == {'commercial': 1}
    assert summary['read_by']['flat_evidence']['hints'] == ['commercial'] and summary['read_by']['flat_evidence']['waived_gates'] == []
    assert summary['read_by']['flat_evidence']['flat_hints'] == ['shipping-container', 'water-tank']
    assert summary['context'] == {'landuse': 'available'} and 'not labels' in summary['note']
    assert scene['analysis']['pipeline'] == 'sam3-scene-roof-v12'
    plain = _scene(image)
    add_roof_estimates(plain, image, context_hints={'b1': 'house'})       # a plain mapping is accepted too
    assert calls[-1] == 'house' and plain['analysis']['context_hints']['by_hint'] == {'house': 1}
    assert plain['objects'][0]['roof_annotation'] == scene['objects'][0]['roof_annotation'], 'a two-tone gable is pitched under either hint'
    default = _scene(image)
    add_roof_estimates(default, image)
    assert calls[-1] is None and 'context_hints' not in default['analysis']


@pytest.mark.parametrize('bad', [{'hints': {'b1': 3}}, {'hints': ['b1']}, 'commercial', {'b1': None}])
def test_malformed_context_hints_are_refused_before_any_roof_is_fitted(bad):
    image = _image()
    scene = _scene(image)
    with pytest.raises(ValueError, match='context_hints'):
        add_roof_estimates(scene, image, context_hints=bad)
    assert 'roof_annotation' not in scene['objects'][0]


def test_pipeline_context_hints_never_raise_and_record_failures(tmp_path, monkeypatch):
    from scripts.ml import scene_house
    image = _image()
    scene = _scene(image)
    result = scene_house.pipeline_context_hints(scene, image, tmp_path)   # no caches, no geo db: an empty context
    assert set(result) >= {'version', 'hints', 'context'} and isinstance(result['hints'], dict)
    assert all(v in ('house', 'units', 'outbuilding', 'non-residential', 'commercial', 'tree-like') for v in result['hints'].values())

    def boom(*args, **kwargs):
        raise RuntimeError('context exploded')
    monkeypatch.setattr('scripts.ml.scene_object_quality.class_hints_for_scene', boom)
    failed = scene_house.pipeline_context_hints(scene, image, tmp_path)
    assert failed['hints'] == {} and failed['error'] == 'RuntimeError: context exploded'
    add_roof_estimates(scene, image, context_hints=failed)
    assert scene['analysis']['context_hints']['error'] == 'RuntimeError: context exploded' and scene['analysis']['roof_proposals'] == 1


def test_pipeline_context_hints_add_structure_sub_kinds_and_apply_flat_hints(tmp_path):
    """A 2.4 x 12 m box on grass (no house nearby) is a shipping container: it replaces the class hint so
    propose_roof selects the flat template; the house keeps its class hint; provenance names both versions."""
    from scripts.ml import scene_house, structure_kinds
    pixels = np.full((512, 512, 3), [86, 118, 70], np.uint8)
    pixels[208:304, 176:256] = [205, 200, 196]            # the 40 x 24 m two-tone gable house
    pixels[208:304, 256:336] = [150, 146, 144]
    pixels[400:410, 60:108] = [150, 150, 150]             # 12 x 2.4 m container (0.25 m/px)
    for x in range(60, 108, 3):
        pixels[400:410, x] = [125, 125, 125]
    image = Image.fromarray(pixels)
    house = [[176, 208], [336, 208], [336, 304], [176, 304]]
    box = [[60, 400], [108, 400], [108, 410], [60, 410]]
    scene = {'id': 'synthetic', 'image_width': 512, 'image_height': 512, 'pixel_to_meters': P2M, 'meters_to_pixel': M2P,
             'objects': [{'id': 'b1', 'kind': 'building', 'ring': house, 'status': 'candidate', 'geometry_reviewed': False,
                          'model': polygon_details(house, image, np.asarray(P2M), 'building')},
                         {'id': 'c1', 'kind': 'building', 'ring': box, 'status': 'candidate', 'geometry_reviewed': False,
                          'model': polygon_details(box, image, np.asarray(P2M), 'building')}]}
    result = scene_house.pipeline_context_hints(scene, image, tmp_path)
    assert result['structure_version'] == structure_kinds.VERSION and 'flag_payloads' not in result
    assert result['structure']['c1']['sub_kind'] == 'shipping-container'
    assert result['hints']['c1'] == 'shipping-container' and result['hints']['b1'] == 'house'
    assert result['structure_hints_applied'] == 1
    add_roof_estimates(scene, image, context_hints=result)
    summary = scene['analysis']['context_hints']
    assert summary['structure_version'] == structure_kinds.VERSION and summary['by_structure'].get('shipping-container') == 1
    assert scene['objects'][1]['roof_type'] == 'flat' and scene['objects'][0]['roof_type'] != 'flat'


def test_structure_failures_leave_class_hints_standing(tmp_path, monkeypatch):
    from scripts.ml import scene_house, structure_kinds

    def boom(*args, **kwargs):
        raise RuntimeError('structure exploded')
    monkeypatch.setattr(structure_kinds, 'assess_scene', boom)
    image = _image()
    scene = _scene(image)
    result = scene_house.pipeline_context_hints(scene, image, tmp_path)
    assert result['hints'] == {'b1': 'house'} and result['structure'] == {} and result['structure_error'] == 'RuntimeError: structure exploded'
