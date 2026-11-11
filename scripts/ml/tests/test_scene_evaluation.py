import copy
import json

import numpy as np
import pytest
from scripts.ml.scene_evaluation import (
    evaluate_dataset,
    evaluate_objects,
    match_instances,
    summarize,
)


def square(identity, x=0, size=10, **kwargs):
    return {'id': identity, 'kind': 'car', 'ring': [[x, 0], [x+size, 0], [x+size, size], [x, size]], **kwargs}


def test_matching_prioritizes_count_and_then_overlap():
    # Greedy highest-IoU would take [0, 0] and miss the second reference.
    assert set(match_instances([[.95, .6], [.6, 0]])) == {(0, 1), (1, 0)}
    assert set(match_instances([[.7, .6], [.6, .9]])) == {(0, 0), (1, 1)}
    assert match_instances(np.zeros((0, 2))) == []
    assert match_instances([[.499]]) == []
    assert match_instances([[.5]]) == [(0, 0)]


def test_duplicate_predictions_miss_and_invalid_outline_are_counted():
    invalid = square('invalid', 50)
    invalid['ring'] = [[0, 0], [10, 10], [10, 0], [0, 10]]
    result = evaluate_objects([square('a'), square('b'), invalid], [square('target'), square('missing', 20)])
    assert (result['matched'], result['extra'], result['missed'], result['invalid_predictions']) == (1, 2, 1, 1)
    assert result['precision'] == pytest.approx(1/3, abs=1e-6) and result['recall'] == .5
    assert result['outline_iou'] == .5
    assert result['missed_ids'] == ['missing'] and 'invalid' in result['extra_ids']


def test_empty_images_and_missing_detections_do_not_invent_accuracy():
    empty = evaluate_objects([], [])
    assert empty['precision'] is empty['recall'] is empty['outline_iou'] is None
    missed = evaluate_objects([], [square('target')])
    assert missed['precision'] is None and missed['recall'] == missed['outline_iou'] == 0
    extra = evaluate_objects([square('extra')], [])
    assert extra['precision'] == 0 and extra['recall'] is None and extra['outline_iou'] == 0
    for item in (empty, missed, extra):
        item['spatial_group'] = 'one-area'
    total = summarize([empty, missed, extra])
    assert total['images'] == 3 and total['groups'] == 1 and total['empty_images'] == 2
    assert total['mean_outline_iou'] == 0 and total['precision'] == total['recall'] == 0


def test_holes_affect_iou_and_input_order_does_not():
    gap = [[2, 2], [8, 2], [8, 8], [2, 8]]
    reference = square('target', holes=[gap])
    result = evaluate_objects([square('p')], [reference])
    assert result['matches'][0]['iou'] == result['outline_iou'] == .64
    assert evaluate_objects([reference], [reference])['outline_iou'] == 1
    proposals = [square('z'), square('a', 20)]
    assert evaluate_objects(proposals, [square('one'), square('two', 20)]) == evaluate_objects(proposals[::-1], [square('two', 20), square('one')])
    reference['ring'] = []
    with pytest.raises(ValueError, match='Invalid evaluation reference'):
        evaluate_objects([], [reference])


def test_dataset_inherits_all_coverage_gates_and_scan_requirement():
    row = {'scene_id': 'scene', 'prediction': [square('prediction', source='sam3')], 'reference': [square('target')],
           'model_signature': 'model-v1', 'image_sha256': 'image-v1'}
    layer = {'scene_id': 'scene', 'title': 'Example', 'blockers': [], 'scan': 'complete', 'split': 'validation',
             'spatial_group': 'area-1', 'review_url': '/scenes?scene=scene', 'revision': 7}
    coverage = {'version': 'dataset-v1', 'classes': [{'kind': 'car', 'layers': [layer]}, {'kind': 'tree', 'layers': [layer]}]}
    before = copy.deepcopy((row, coverage))
    report = evaluate_dataset(json.dumps(row), coverage)
    car, tree = report['classes']
    assert car['splits']['validation']['matched'] == 1 and car['splits']['train']['images'] == 0
    assert tree['splits']['validation']['references'] == 0, 'Never match across classes'
    assert car['layers'][0]['model_signature'] == 'model-v1' and car['layers'][0]['revision'] == 7
    assert (row, coverage) == before
    for blocker in ['image_geography', 'stale', 'unassigned', 'missing_group', 'split_conflict', 'pending', 'unchecked']:
        layer['blockers'] = [blocker]
        report = evaluate_dataset(json.dumps(row), coverage)
        assert not report['classes'][0]['layers'] and blocker in report['skipped'][0]['reasons']
    layer.update(blockers=[], scan='not_scanned')
    assert not evaluate_dataset(json.dumps(row), coverage)['classes'][0]['layers']
