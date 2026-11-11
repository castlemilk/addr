"""Torch-free parts of the training kit: boundary metrics, the orthogonal regulariser, mask pasting, evaluation."""
import subprocess
import sys

import numpy as np
from scripts.ml.segmentation import mask_to_polygons
from scripts.ml.training import common, evaluate, infer, metrics
from shapely import affinity
from shapely.geometry import Point, Polygon, box

PX_M = .236


def test_training_modules_import_without_torch():
    code = ('import sys; import scripts.ml.training.common, scripts.ml.training.export_dataset, '
            'scripts.ml.training.metrics, scripts.ml.training.evaluate, scripts.ml.training.infer; '
            "sys.exit(1 if 'torch' in sys.modules else 0)")
    assert subprocess.run([sys.executable, '-c', code], cwd=common.REPO).returncode == 0


def test_boundary_and_corner_metrics_separate_square_from_rounded():
    square = box(40, 40, 120, 120)
    rounded = square.buffer(-12).buffer(12)            # a SAM3-like chamfered square, same size
    target = common.polygon_mask(square, 160, 160)
    exact = metrics.evaluate_mask(target, target, square, PX_M)
    soft = metrics.evaluate_mask(common.polygon_mask(rounded, 160, 160), target, square, PX_M)
    assert exact['iou'] == 1. and exact['boundary_f'] == 1. and exact['hausdorff_m'] < .25
    assert soft['iou'] > .95                            # IoU barely notices a rounded corner ...
    assert soft['hausdorff_m'] > 1. and soft['vertex_distance_median_m'] > .9   # ... the corner metrics do
    assert soft['vertex_within_tolerance'] == 0.


def test_outline_pair_measures_agreement_between_reviewer_and_model_outlines():
    model, human = box(100, 20, 140, 60), box(104, 24, 144, 64)
    pair = metrics.outline_pair(human, model, PX_M)
    assert abs(pair['iou'] - 1296 / 1904) < 1e-9 and abs(pair['hausdorff_m'] - 4 * 2 ** .5 * PX_M) < 1e-9
    assert pair['n_human_vertices'] == 4 and pair['n_model_vertices'] == 4 and pair['vertex_delta'] == 0
    assert abs(pair['centroid_shift_m'] - 4 * 2 ** .5 * PX_M) < 1e-9 and abs(pair['area_delta_m2']) < 1e-9
    assert pair['human_vertex_within_0_45_m'] == 0. and pair['human_vertex_to_model_median_m'] > .9
    same = metrics.outline_pair(model, model, PX_M)
    assert same['iou'] == 1. and same['hausdorff_m'] == 0. and same['human_vertex_within_0_45_m'] == 1.
    assert set(metrics.outline_pair(None, model, PX_M)) == set(metrics.OUTLINE_PAIR_KEYS)
    assert all(v is None for v in metrics.outline_pair(human, Polygon(), PX_M).values())


def test_label_boundary_f_ignores_pixels_near_ignore():
    target = np.zeros((100, 100), dtype=np.int64)
    target[20:60, 20:60] = 1
    pred = target.copy()
    assert metrics.label_boundary_f(pred, target, PX_M)['boundary_f'] == 1.
    shifted = np.roll(target, 6, axis=1)
    assert metrics.label_boundary_f(shifted, target, PX_M)['boundary_f'] < .8
    ignore = np.zeros_like(target, dtype=bool)
    ignore[:, 50:] = True
    partial = target.copy()
    partial[:, 50:] = 0                                  # prediction missing only inside the ignore band
    assert metrics.label_boundary_f(partial, target, PX_M, ignore)['boundary_f'] > .95


def test_paired_delta_direction_depends_on_metric():
    better = [{'hausdorff_m': .2, 'iou': .9}] * 3
    worse = [{'hausdorff_m': .8, 'iou': .7}] * 3
    distance = metrics.paired_delta(better, worse, key='hausdorff_m')
    overlap = metrics.paired_delta(better, worse, key='iou')
    assert distance['n_improved'] == 3 and distance['median_delta'] < 0
    assert overlap['n_improved'] == 3 and overlap['median_delta'] > 0


def test_orthogonalise_restores_square_corners_and_refuses_round_things():
    for degrees in (0, 17, 38):
        truth = affinity.rotate(box(60, 60, 160, 120), degrees, origin='centroid')
        mask = common.polygon_mask(truth.buffer(-8).buffer(8), 240, 220)
        outline = max(mask_to_polygons(mask, 12, .9), key=lambda p: p.area)
        regular = common.orthogonalise(outline, PX_M)
        assert regular is not None and len(regular.exterior.coords) - 1 == 4
        assert regular.hausdorff_distance(truth) < outline.hausdorff_distance(truth)
    ell = Polygon([(20, 20), (120, 20), (120, 60), (60, 60), (60, 120), (20, 120)])
    regular = common.orthogonalise(ell.buffer(-6).buffer(6, quad_segs=2), PX_M)
    assert len(regular.exterior.coords) - 1 == 6 and regular.hausdorff_distance(ell) * PX_M < .1
    circle = max(mask_to_polygons(common.polygon_mask(Point(80, 80).buffer(40), 160, 160), 12, .9), key=lambda p: p.area)
    assert common.orthogonalise(circle, PX_M) is None
    assert common.orthogonalise(Polygon([(0, 0), (100, 0), (0, 100)]), PX_M) is None


def test_resize_bilinear_and_paste_follow_torchvision_conventions():
    source = np.array([[0., 1.], [2., 3.]], dtype=np.float32)
    up = infer.resize_bilinear(source, 4, 4)
    assert np.allclose(up[0], [0., .25, .75, 1.]) and np.allclose(up[:, 0], [0., .5, 1.5, 2.])
    mask = np.ones((28, 28), dtype=np.float32)
    pasted = infer.paste_mask(mask, np.array([10., 20., 49., 59.]), 100, 100)
    ys, xs = np.nonzero(pasted)
    assert pasted.dtype == np.bool_ and pasted.shape == (100, 100)
    assert abs(xs.min() - 10) <= 1 and abs(xs.max() - 49) <= 1 and abs(ys.min() - 20) <= 1 and abs(ys.max() - 59) <= 1
    assert not infer.paste_mask(mask, np.array([-50., -50., -10., -10.]), 100, 100).any()


def test_match_and_score_picks_the_overlapping_candidate():
    reference = {'segmentation': [[40, 40, 120, 40, 120, 120, 40, 120]], 'holes': []}
    near = common.polygon_mask(box(42, 42, 121, 121), 200, 200)
    far = common.polygon_mask(box(150, 150, 190, 190), 200, 200)
    result = evaluate.match_and_score(reference, [(far, None, .9), (near, None, .5)], 200, 200, PX_M)
    assert result['matched'] and result['score'] == .5 and result['iou'] > .9
    missed = evaluate.match_and_score(reference, [(far, None, .9)], 200, 200, PX_M)
    assert missed == {'matched': False, 'iou': None, 'score': None}
    polygon_candidate = evaluate.match_and_score(reference, [(None, box(40, 40, 120, 120), None)], 200, 200, PX_M)
    assert polygon_candidate['iou'] == 1.
