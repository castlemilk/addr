import numpy as np
import pytest
from PIL import Image
from scripts.ml import roof_upscale_eval as experiment
from scripts.ml.sam3 import Sam3Proposal
from shapely.geometry import Polygon, box


def test_all_input_arms_decode_same_native_cells_and_holes_then_only_translate():
    calls = []
    class Detector:
        def encode_image(self, image):
            return image.size

        def proposals(self, features, prompt, image_size, **kwargs):
            calls.append((features, prompt, image_size, kwargs))
            mask = np.zeros((image_size[1], image_size[0]), dtype=bool)
            mask[2:10, 3:12] = True
            mask[4:7, 6:9] = False
            return [Sam3Proposal(mask, .8, (0, 0, 1, 1))]

    crop = {'crop_box': [41, 73, 57, 87], 'crop_size': [16, 14]}
    geometries = []
    for size in ((16, 14), (64, 56), (64, 56)):
        detections, _ = experiment.detect_arm(Detector(), Image.new('RGB', size), crop['crop_size'])
        result = experiment.source_polygons(detections, crop, [[.25, 0], [0, -.25], [0, 0]])
        polygon = Polygon(result[0]['ring'], result[0]['holes'])
        assert polygon.bounds == (44., 75., 53., 83.)
        assert Polygon(polygon.interiors[0]).bounds == (47., 77., 50., 80.)
        geometries.append(polygon)
        lifted = list(experiment.FrozenCropDetections(detections, crop).proposals(None, 'building roof', (100, 100), threshold=.45, max_proposals=80))
        assert lifted[0].mask.shape == (100, 100)
        assert lifted[0].mask[75:83, 44:53].sum() == 63
        assert not lifted[0].mask[77:80, 47:50].any()
    assert all(g.equals_exact(geometries[0], 0) for g in geometries)
    assert all(c[1:] == ('building roof', (16, 14), {'threshold': .45, 'max_proposals': 80}) for c in calls)
    wrong_grid = [Sam3Proposal(np.ones((56, 64), dtype=bool), .8, (0, 0, 1, 1))]
    with pytest.raises(ValueError, match='native ROI'):
        experiment.source_polygons(wrong_grid, crop, [[1, 0], [0, 1], [0, 0]])


def test_matching_uses_iou_not_detector_confidence_and_records_merge_missing():
    def candidate(oid, shape, score):
        return {'id': oid, 'ring': list(map(list, shape.exterior.coords[:-1])), 'score': score}
    target = box(0, 0, 10, 10)
    exact = candidate('exact', target, .46)
    merged = candidate('merged', box(0, 0, 19, 10), .99)
    selected, matches = experiment.match_candidates(target, [merged, exact])
    assert selected['id'] == 'exact'
    assert matches[0]['candidate_inside_target_fraction'] == pytest.approx(100/190)
    assert matches[0]['target_covered_fraction'] == 1
    missing, rows = experiment.match_candidates(target, [candidate('fragment', box(0, 0, 4, 10), .999)])
    assert missing is None and rows[0]['iou'] == .4


def test_roof_request_is_bound_to_original_source_image_and_exact_source_frame(monkeypatch):
    import json
    from types import SimpleNamespace

    candidate = {'ring': [[30, 40], [70, 40], [70, 80], [30, 80]], 'holes': [[[40, 50], [50, 50], [50, 60], [40, 60]]],
                 'model': {'height_m': 3.8}}
    scene = {'pixel_to_meters': [[.2, 0], [0, -.2], [-10, 30]],
             'meters_to_pixel': [[5, 0], [0, -5], [50, 150]]}
    source = {'image_path': '/immutable/original.png', 'image_sha256': 'source-sha'}
    requests = []
    def run(command, **kwargs):
        requests.append(json.loads(command[-1]))
        assert kwargs['timeout'] == 30
        return SimpleNamespace(returncode=0, stdout='{"audit":{"passed":true}}')
    monkeypatch.setattr(experiment.subprocess, 'run', run)
    experiment.roof_proposal(candidate, scene, {'source': source})
    request = requests[0]
    assert request['image_path'] == source['image_path'] and request['image_sha256'] == 'source-sha'
    assert request['ring'] == [[-4., 22.], [4., 22.], [4., 14.], [-4., 14.]]
    assert request['holes'] == [[[-2., 20.], [0., 20.], [0., 18.], [-2., 18.]]]
    assert request['meters_to_pixel'] == scene['meters_to_pixel'] and request['eave'] == 3.8
