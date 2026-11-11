import copy
import json
from pathlib import Path

import numpy as np
import pytest
from scripts.ml.roof_boundary import model_canopies, regularize_eaves
from shapely.affinity import rotate, translate
from shapely.geometry import Polygon, box

FRAME = np.array([[.1, 0], [0, -.1], [0, 24.]])


@pytest.mark.parametrize('angle', [0, 29, 87])
@pytest.mark.parametrize('reverse', [False, True])
def test_canopy_cut_is_completed_from_visible_eaves(angle, reverse):
    expected = box(30, 30, 190, 170)
    source = Polygon([(30, 30), (190, 30), (190, 65), (181, 80), (176, 100),
                      (182, 112), (190, 130), (190, 170), (30, 170)])
    canopy = box(165, 60, 210, 135)
    source, canopy, expected = [translate(rotate(p, angle, origin=(110, 100)), 10, 20) for p in (source, canopy, expected)]
    if reverse:
        source = Polygon(list(source.exterior.coords)[::-1])
    result, report = regularize_eaves(source, (240, 240), FRAME, [canopy])
    assert report['accepted'] and report['inferred_edges']
    assert len(result.exterior.coords) == 5
    assert result.boundary.hausdorff_distance(expected.boundary) < .8  # < 8 cm
    assert report['max_canopy_deviation_m'] > .8
    unsupported, without_canopy = regularize_eaves(source, (240, 240), FRAME)
    assert not without_canopy['accepted']
    assert unsupported.equals(source), 'A missing visible side is not automatically a tree'


def test_visible_diagonal_and_deep_recess_are_not_forced_into_a_box():
    shape = Polygon([(25, 25), (150, 25), (180, 55), (180, 110), (100, 110), (100, 200), (25, 200)])
    hole = box(50, 60, 75, 85)
    shape = Polygon(shape.exterior, [hole.exterior])
    result, report = regularize_eaves(shape, (240, 240), FRAME)
    assert report['accepted']
    assert result.boundary.hausdorff_distance(shape.boundary) < .01
    assert result.convex_hull.area/result.area > 1.15
    assert [list(h.coords) for h in result.interiors] == [list(h.coords) for h in shape.interiors]


def test_entirely_hidden_or_clipped_roof_is_not_completed():
    for shape, canopy in [(box(30, 30, 190, 170), box(20, 20, 200, 180)),
                          (box(0, 30, 190, 170), box(165, 60, 210, 135))]:
        result, report = regularize_eaves(shape, (240, 240), FRAME, [canopy])
        assert not report['accepted'] and result.equals(shape)


def test_canopy_inputs_exclude_ground_vegetation_and_reviewer_geometry():
    tree = {'id': 'tree', 'kind': 'tree', 'source': 'sam3-text-onnx', 'ring': list(box(1, 1, 5, 5).exterior.coords)}
    inputs = [tree, {**tree, 'kind': 'vegetation'}, {**tree, 'source': 'reviewer'},
              {**tree, 'geometry_reviewed': True}, {**tree, 'status': 'rejected'},
              {**tree, 'status': 'superseded'}, {**tree, 'roof_line_edits': {'vertices': []}}]
    original = copy.deepcopy(inputs)
    assert len(model_canopies(inputs)) == 1
    assert inputs == original


def test_detector_class_order_supplies_canopies_before_roof_inference():
    from PIL import Image, ImageDraw
    from scripts.ml.sam3 import Sam3Proposal
    from scripts.ml.scene_extract import extract_objects
    roof = [(30, 30), (190, 30), (190, 65), (181, 80), (176, 100),
            (182, 112), (190, 130), (190, 170), (30, 170)]
    calls = []
    class Detector:
        def encode_image(self, image):
            return None
        def proposals(self, features, prompt, size, **kwargs):
            calls.append(prompt)
            mask = Image.new('L', size)
            ImageDraw.Draw(mask).polygon(roof if prompt == 'building roof' else [(165, 60), (210, 60), (210, 135), (165, 135)], fill=1)
            return [Sam3Proposal(np.array(mask, dtype=bool), .9, (0, 0, *size))]
    objects, _ = extract_objects(Image.new('RGB', (240, 240), 'gray'), 'image', FRAME,
                                 Detector(), kinds=['building', 'tree'])
    assert calls == ['tree canopy', 'building roof']
    building = next(o for o in objects if o['kind'] == 'building')
    assert building['boundary_refinement']['construction']['inferred_edges']
    assert not building['geometry_reviewed'] and building['status'] == 'candidate'


@pytest.mark.parametrize('angle', [0, 29])
@pytest.mark.parametrize('reverse', [False, True])
def test_attached_roof_uses_shared_angles_and_straight_continuation(angle, reverse):
    fixture = json.loads((Path(__file__).parent/'fixtures/canopy-stepped-roof.json').read_text())
    source = Polygon(fixture['ring'], fixture['holes'])
    center = source.centroid.coords[0]
    source = rotate(source, angle, origin=center)
    canopies = [rotate(Polygon(p['ring'], p['holes']), angle, origin=center) for p in fixture['canopies']]
    if reverse:
        source = Polygon(list(source.exterior.coords)[::-1], [h.coords for h in source.interiors])
    result, report = regularize_eaves(source, fixture['image_size'], fixture['pixel_to_meters'], canopies)
    assert report['accepted'] and report['review_required']
    assert 14 <= len(result.exterior.coords)-1 <= 18
    assert report['p95_visible_deviation_m'] < .45
    assert report['max_canopy_deviation_m'] < 2
    assert report['visible_area_change_fraction'] < .04
    assert report['area_change_fraction'] < .10
    xy = np.asarray(result.exterior.coords) @ np.asarray(fixture['pixel_to_meters'])[:2]
    vectors = np.diff(xy, axis=0)
    angles = np.arctan2(vectors[:, 1], vectors[:, 0])
    errors = abs((angles-np.deg2rad(report['axis_angle_deg'])+np.pi/4) % (np.pi/2)-np.pi/4)
    assert np.count_nonzero(errors < 1e-6) >= len(vectors)-2
    assert sum(np.linalg.norm(vectors, axis=1) > 15) >= 2
    assert result.convex_hull.area/result.area > 1.3
