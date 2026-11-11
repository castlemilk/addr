import copy

import numpy as np
import pytest
from scripts.ml.train_scene_proposals import FEATURE_SETS, features, probabilities, scores, train


def proposal(identity, x=0, good=True):
    return {'id': identity, 'kind': 'solar_panel', 'ring': [[x, 0], [x+4, 0], [x+4, 4], [x, 4]],
            'score': .95 if good else .5, 'model': {'area_m2': 16 if good else 150,
            'width_m': 4 if good else 10, 'length_m': 4 if good else 15,
            'color': '#405070' if good else '#c0b0a0', 'height_m': 100, 'height_verified': False}}


def cases():
    return [{'scene_id': split, 'split': split, 'spatial_group': split,
             'predictions': [proposal('yes'), proposal('no', 20, False)],
             'references': [proposal('reference')]} for split in ('train', 'validation')]


def test_fit_learns_from_feedback_without_changing_boundaries_or_estimates():
    data = cases()
    before = copy.deepcopy(data)
    report = train(data)
    assert report['training_proposals'] == {'usable': 1, 'negative': 1, 'images': 1, 'groups': 1}
    assert report['baseline']['splits']['validation']['extra'] == 1
    assert report['metrics']['splits']['validation']['extra'] == 0
    assert report['metrics']['splits']['validation']['matched'] == 1
    assert report['status'] == 'experimental-candidate'
    p = probabilities(data[1]['predictions'], report['model'])
    assert p[0] > .5 > p[1]
    assert data == before
    assert probabilities([], report['model']).size == 0


def test_normalization_is_train_only_and_test_objects_are_never_scored():
    first = train(cases())
    data = cases()
    data[1]['predictions'][1]['model']['area_m2'] = 1e12
    data.append({'scene_id': 'test', 'split': 'test', 'predictions': [{'model': 'must not read'}]})
    second = train(data)
    assert first['model']['mean'] == second['model']['mean']
    assert first['model']['scale'] == second['model']['scale']
    assert all(layer['split'] != 'test' for layer in second['metrics']['layers'])


def test_area_group_balancing_resists_repeated_identical_training_views():
    data = cases()
    first = train(data)
    data.insert(1, {**copy.deepcopy(data[0]), 'scene_id': 'repeated-view'})
    second = train(data)
    np.testing.assert_allclose(first['model']['weights'], second['model']['weights'], atol=1e-10)
    assert second['training_proposals']['groups'] == 1


def test_3d_priors_and_reference_metadata_are_not_model_features():
    obj = proposal('p')
    baseline = features(obj)
    obj['model'].update(height_m=1, base_m=200, height_verified=True)
    obj.update(notes='Looks correct', status='confirmed', geometry_reviewed=True, group_id='reviewed')
    np.testing.assert_array_equal(features(obj), baseline)


@pytest.mark.parametrize('field,value', [('area_m2', 0), ('width_m', float('nan')), ('length_m', -1), ('color', '#foo')])
def test_invalid_feature_inputs_are_not_silently_used(field, value):
    obj = proposal('p')
    obj['model'][field] = value
    with pytest.raises(ValueError):
        features(obj)


def test_training_requires_positive_negative_and_validation_examples():
    data = cases()
    data[0]['predictions'] = data[0]['predictions'][:1]
    with pytest.raises(ValueError, match='both usable matches and negatives'):
        train(data)
    data = cases()
    data[1]['references'] = []
    with pytest.raises(ValueError, match='Positive reviewed validation'):
        train(data)


def test_filtered_metrics_still_count_duplicates_and_include_checked_empty_images():
    data = cases()
    data[1]['predictions'].append(proposal('duplicate'))
    data.append({'scene_id': 'negative-image', 'split': 'validation', 'spatial_group': 'negative-area',
                 'predictions': [], 'references': []})
    model = train(data)['model']
    result = scores(data, model)['splits']['validation']
    assert result['predictions'] == 2 and result['matched'] == 1 and result['extra'] == 1
    assert result['images'] == 2 and result['empty_images'] == 1


def test_explicit_partial_image_feedback_fits_without_becoming_an_evaluation_layer():
    data = cases()
    extra = {'scene_id': 'partial', 'split': 'train', 'spatial_group': 'another-training-area',
             'predictions': [proposal('known-positive')], 'labels': [1]}
    report = train(data, [extra])
    assert report['training_proposals'] == {'usable': 2, 'negative': 1, 'images': 2, 'groups': 2}
    assert report['baseline'] == scores(data)
    assert {layer['scene_id'] for layer in report['metrics']['layers']} == {'train', 'validation'}
    for change in [{'split': 'validation'}, {'split': 'test'}, {'spatial_group': 'validation'},
                   {'scene_id': 'train'}, {'labels': []}, {'labels': [2]}]:
        with pytest.raises(ValueError, match='distinct, explicitly labelled'):
            train(data, [{**extra, **change}])


def test_image_context_candidates_require_context_and_keep_train_only_scaling():
    data = cases()
    for case in data:
        case['context'] = {'yes': [0, 0, .2, .1, .2, -.3, .2], 'no': [1, .5, .01, .01, .01, .1, -.1]}
    original = copy.deepcopy(data)
    first = train(data, feature_set='image-context')
    model = first['model']
    assert model['schema_version'] == 2 and model['feature_names'] == FEATURE_SETS['image-context']
    assert len(model['mean']) == 15 and len(model['weights']) == 16
    assert first['metrics']['splits']['validation']['matched'] == 1
    with pytest.raises(ValueError, match='Image-context features are required'):
        probabilities(data[0]['predictions'], model)
    with pytest.raises(ValueError, match='Invalid image-context'):
        probabilities(data[0]['predictions'], model, {'yes': [float('nan')]*7, 'no': [0]*7})
    with pytest.raises(ValueError, match='Unknown proposal feature'):
        probabilities([], {**model, 'feature_names': ['unknown']})
    assert data == original
    data[1]['context']['no'] = [100]*7
    data.append({'scene_id': 'test', 'split': 'test', 'predictions': [{'invalid': True}]})
    second = train(data, feature_set='image-context')
    assert model['mean'] == second['model']['mean'] and model['scale'] == second['model']['scale']
    assert all(layer['split'] != 'test' for layer in second['metrics']['layers'])
