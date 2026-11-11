import copy

import cv2
import numpy as np
import pytest
from PIL import Image
from scripts.ml.roof_candidate_review import candidate_gate, evidence_context, improve, inspect
from scripts.ml.roof_family_review import flat_alternative
from scripts.ml.roof_image_evidence import fuse_registered, quality_report
from scripts.ml.roof_unit_patterns import (
    detect_equipment,
    fixture_candidates,
    partition_hypothesis,
    seam_partitions,
)
from scripts.ml.tests.test_scene_self_review import scene as scene
from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.ops import unary_union


def test_flat_surface_has_no_invented_folds_and_does_not_imply_classification(scene):
    _, model, image, _ = scene
    obj = model['objects'][0]
    original = copy.deepcopy(obj)
    result, candidate = improve(obj, model, image)
    flat = result['family_review']['alternatives'][0]
    assert result['status'] == 'needs_evidence' and candidate is None
    assert flat['geometry_passed'] and not flat['eligible'] and not flat['drainage_assessed']
    assert not flat['object']['roof_lines'] and not flat['object']['roof_annotation']['edges']
    assert flat['object']['ring'] == obj['ring'] and obj == original
    audit = inspect(flat['object'], model, image, evidence_context(obj, model, image))
    assert audit['geometry_passed']
    assert 'unsupported_fold_extent' not in {i['code'] for i in audit['issues']}
    assert not candidate_gate(result['before'], audit)[0]


def test_flat_hypothesis_preserves_courtyard(scene):
    _, model, _, _ = scene
    obj = model['objects'][0]
    obj['holes'] = [[[50, 40], [70, 40], [70, 60], [50, 60]]]
    alternative = flat_alternative(obj, model)['alternatives'][0]['object']
    union = unary_union([Polygon(p['ring_meters']) for p in alternative['roof_parts']])
    assert union.area == pytest.approx(68)
    assert not union.intersects(box(5.01, 4.01, 6.99, 5.99))


def fixtures(xs=(3, 9, 15, 21), y=4):
    return [{'point': [x, y], 'area_m2': 1., 'kind': 'air_conditioner'} for x in xs]


@pytest.mark.parametrize('angle', [0, 31, 90])
def test_unit_partition_preserves_rotated_continuous_roof(angle):
    polygon = affinity.rotate(box(0, 0, 24, 8), angle, origin=(0, 0))
    row = fixtures()
    for fixture in row:
        p = affinity.rotate(Polygon([fixture['point'], [fixture['point'][0]+.1, 4], [fixture['point'][0], 4.1]]).centroid,
                            angle, origin=(0, 0))
        fixture['point'] = [p.x, p.y]
    result = partition_hypothesis(list(polygon.exterior.coords[:-1]), [], row)
    assert result['status'] == 'candidate_partition' and result['estimated_sections'] == 4
    assert result['unit_count'] is None and result['confidence'] == 'unverified'
    parts = [Polygon(p['ring'], p['holes']) for p in result['partitions']]
    assert unary_union(parts).symmetric_difference(polygon).area < 1e-6
    assert sum(p.area for p in parts) == pytest.approx(polygon.area)
    assert result['roof_surface_unchanged']


def test_two_fixture_rows_do_not_double_unit_count():
    result = partition_hypothesis(list(box(0, 0, 24, 8).exterior.coords[:-1]), [], fixtures(y=2)+fixtures(y=6))
    assert result['estimated_sections'] == 4


@pytest.mark.parametrize('row', [fixtures((3, 9)), fixtures((3, 5, 20)),
    [{**f, 'kind': 'solar_panel'} for f in fixtures()]])
def test_sparse_irregular_or_solar_patterns_do_not_split(row):
    result = partition_hypothesis(list(box(0, 0, 24, 8).exterior.coords[:-1]), [], row)
    assert result['status'] == 'insufficient_pattern' and not result['partitions']


def test_unit_cut_does_not_bridge_a_courtyard():
    result = partition_hypothesis(list(box(0, 0, 24, 8).exterior.coords[:-1]),
                                 [list(box(11, 3, 13, 5).exterior.coords[:-1])], fixtures(y=2))
    assert not result['partitions']


def test_repeated_transverse_seams_propose_sections_without_equating_them_to_units():
    ring = list(box(0, 0, 24, 8).exterior.coords[:-1])
    lines = [{'points': [[x, 1], [x, 7]]} for x in (6, 12, 18)]
    result = seam_partitions(ring, [], lines)
    assert result['estimated_sections'] == 4 and result['unit_count'] is None
    assert result['evidence_kind'] == 'repeated_transverse_seams'
    assert sum(Polygon(p['ring']).area for p in result['partitions']) == pytest.approx(192)
    assert seam_partitions(ring, [], lines[:1]*5) is None  # Duplicates are not repetitions.
    assert seam_partitions(ring, [], [{'points': [[1, y], [23, y]]} for y in (2, 4, 6)]) is None


def test_detector_excludes_model_solar_pixels(scene):
    _, model, image, _ = scene
    from PIL import ImageDraw
    draw = ImageDraw.Draw(image)
    for x in (40, 70, 100):
        draw.rectangle((x, 40, x+5, 45), fill='white')
    obj = model['objects'][0]
    assert len(fixture_candidates(obj, model, image)) == 3
    model['objects'].append({'kind': 'solar_panel', 'source': 'sam3-text-onnx', 'status': 'candidate',
                             'ring': [[30, 30], [120, 30], [120, 60], [30, 60]]})
    assert not fixture_candidates(obj, model, image)


def test_semantic_equipment_is_registered_and_duplicate_prompts_do_not_add_units(scene):
    from types import SimpleNamespace
    _, model, image, _ = scene

    class Detector:
        calls = 0

        def encode_image(self, crop):
            self.calls += 1
            return crop.size

        def proposals(self, features, prompt, size, **kwargs):
            assert features == size
            mask = np.zeros((size[1], size[0]), dtype=np.uint8)
            mask[25:35, 25:35] = 1
            return [SimpleNamespace(mask=mask, score=.8)]

    detector = Detector()
    rows = detect_equipment(model['objects'][0], model, image, detector)
    assert detector.calls == 1 and len(rows) == 1
    assert rows[0]['kind'] == 'air_conditioner' and rows[0]['source'] == 'sam3-equipment-candidate'
    assert rows[0]['point'] == pytest.approx([42., 42.])


def test_equipment_inputs_are_fingerprinted_and_frozen(scene):
    import json

    from scripts.ml.roof_review_loop import fingerprint, snapshot_equipment
    from scripts.ml.roof_unit_patterns import cached_equipment, equipment_key
    root, model, _, _ = scene
    obj = model['objects'][0]
    before = fingerprint(root)
    cache = root/'data/roof-review-loop/equipment'
    cache.mkdir(parents=True)
    key = equipment_key(obj, model)
    path = cache/f'{key}.json'
    path.write_text(json.dumps({'key': key, 'fixtures': fixtures()}))
    assert fingerprint(root) != before
    target = root/'frozen'
    snapshot_equipment(root, target)
    path.unlink()
    assert cached_equipment(root, obj, model, target/'code/equipment') == fixtures()


def capture_stack():
    rng = np.random.default_rng(19)
    base = cv2.GaussianBlur(rng.integers(30, 220, (128, 128, 3), dtype=np.uint8), (5, 5), 1)
    images = [Image.fromarray(np.clip(base.astype(float)+rng.normal(0, 2, base.shape), 0, 255).astype(np.uint8)) for _ in range(3)]
    captures = [{'acquisition_id': f'flight-{i}', 'acquisition_date': f'2026-09-0{i+1}', 'frame_id': 'same-ortho'} for i in range(3)]
    return base, images, captures


def test_fusion_reduces_independent_noise_without_changing_grid():
    base, images, captures = capture_stack()
    fused, audit = fuse_registered(images, captures)
    assert fused.size == images[0].size and audit['fused_fraction'] > .9
    def mse(image):
        return np.mean((np.asarray(image).astype(float)-base)**2)
    assert mse(fused) < mse(images[0])
    assert not audit['new_resolution'] and not audit['filled_hidden_regions']


@pytest.mark.parametrize('bad', ['duplicate_pixels', 'same_acquisition', 'unknown_date', 'different_frame', 'old_capture'])
def test_fusion_rejects_unsupported_evidence(bad):
    _, images, captures = capture_stack()
    if bad == 'duplicate_pixels':
        images[1] = images[0].copy()
    if bad == 'same_acquisition':
        captures[1]['acquisition_id'] = captures[0]['acquisition_id']
    if bad == 'unknown_date':
        captures[1].pop('acquisition_date')
    if bad == 'different_frame':
        captures[1]['frame_id'] = 'another-grid'
    if bad == 'old_capture':
        captures[1]['acquisition_date'] = '2020-01-01'
    with pytest.raises(ValueError):
        fuse_registered(images, captures)


def test_changed_pixels_remain_reference_not_blended():
    _, images, captures = capture_stack()
    changed = np.asarray(images[1]).copy()
    changed[40:50, 40:50] = 255
    images[1] = Image.fromarray(changed)
    result, _ = fuse_registered(images, captures)
    np.testing.assert_array_equal(np.asarray(result)[42:48, 42:48], np.asarray(images[0])[42:48, 42:48])


def test_fusion_rejects_large_displacement():
    _, images, captures = capture_stack()
    images[1] = Image.fromarray(np.roll(np.asarray(images[1]), 8, axis=1))
    with pytest.raises(ValueError):
        fuse_registered(images, captures)


def test_image_report_does_not_invent_capture_dates_or_accuracy(scene):
    _, model, image, _ = scene
    report = quality_report(model['objects'][0], model, image)
    assert report['acquisition_date'] is None and report['ground_pixel_m'] == .1
    assert 'accuracy' not in report and report['fusion_status'] == 'needs_independent_registered_captures'
