"""Train an experimental proposal-quality scorer from frozen scene feedback.

The scorer filters candidate masks; it does not alter boundaries or estimate
height. Training uses reviewed train images, selection uses validation only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from .scene_evaluation import evaluate_objects, ratio, summarize
from .scene_experiment import digest, frozen_layers
from .scene_proposal_features import CONTEXT_FEATURES, case_context
from .scene_proposal_feedback import supplemental_cases
from .scene_training import atomic_json
from .train_roof_form import fit_logistic, sigmoid

FEATURES = ['detector_score', 'log_area_m2', 'log_width_m', 'log_aspect_ratio',
            'rectangle_fill', 'median_rgb_brightness', 'red_minus_green', 'blue_minus_green']
PENALTIES = (.1, 1., 10.)
FEATURE_SETS = {'legacy': FEATURES, 'image-context': FEATURES + CONTEXT_FEATURES}


def features(obj):
    model = obj['model']
    area, width, length = (float(model[k]) for k in ('area_m2', 'width_m', 'length_m'))
    score = float(obj['score'])
    color = model['color'].lstrip('#')
    if len(color) != 6:
        raise ValueError('Expected a six-digit median image colour')
    rgb = np.array([int(color[i:i+2], 16)/255 for i in (0, 2, 4)])
    if not np.isfinite([area, width, length, score]).all() or min(area, width, length) <= 0 or not 0 <= score <= 1:
        raise ValueError('Proposal needs finite, positive image dimensions and a detector score')
    return np.array([score, np.log1p(area), np.log1p(width), np.log(max(1, length/width)),
                     min(1, area/(width*length)), rgb.mean(), rgb[0]-rgb[1], rgb[2]-rgb[1]])


def model_feature_set(model):
    names = model.get('feature_names', FEATURES)
    for name, expected in FEATURE_SETS.items():
        if names == expected:
            return name
    raise ValueError('Unknown proposal feature definitions')


def feature_vector(obj, feature_set='legacy', context=None):
    vector = features(obj)
    if feature_set == 'legacy':
        return vector
    if feature_set != 'image-context' or context is None or obj['id'] not in context:
        raise ValueError('Image-context features are required for this candidate')
    extra = np.asarray(context[obj['id']], dtype=float)
    if extra.shape != (len(CONTEXT_FEATURES),) or not np.isfinite(extra).all():
        raise ValueError('Invalid image-context feature values')
    return np.r_[vector, extra]


def probabilities(objects, model, context=None):
    feature_set = model_feature_set(model)
    if not objects:
        return np.array([])
    x = (np.array([feature_vector(obj, feature_set, context) for obj in objects])-model['mean'])/model['scale']
    return sigmoid(np.c_[np.ones(len(x)), x] @ model['weights'])


def checked_cases(dataset, experiment, prompt):
    experiment = Path(experiment)
    config = json.loads((experiment/'config.json').read_text())
    if prompt not in config['prompts']:
        raise ValueError('Prompt is not part of the frozen experiment')
    version, layers = frozen_layers(dataset, config['kind'])
    if config['dataset_version'] != version:
        raise ValueError('Experiment and reviewed dataset differ')
    run_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    cases = []
    for layer, row in layers:
        saved = json.loads((experiment/f'{row["scene_id"]}.json').read_text())
        result = saved['result']
        if saved['run_id'] != run_id or any(result[key] != layer[key] for key in
                ('scene_id', 'split', 'spatial_group', 'revision', 'image_sha256', 'model_signature')):
            raise ValueError('Prediction provenance differs from the reviewed dataset')
        cases.append({**layer, 'image_path': row['image_path'], 'predictions': result['prompts'][prompt]['predictions'],
                      'references': [o for o in row['reference'] if o['kind'] == config['kind']]})
    return config, run_id, cases


def scores(cases, model=None):
    layers = []
    for case in cases:
        predictions = case['predictions']
        if model is not None:
            predictions = [o for o, p in zip(predictions, probabilities(predictions, model, case.get('context')), strict=True) if p >= model['threshold']]
        layers.append({**evaluate_objects(predictions, case['references']),
                       **{k: case[k] for k in ('scene_id', 'split', 'spatial_group')}})
    totals = {}
    for split in ('train', 'validation'):
        m = summarize([layer for layer in layers if layer['split'] == split])
        m['f1'] = ratio(2*m['matched'], m['predictions']+m['references'])
        totals[split] = m
    return {'splits': totals, 'layers': layers}


def train(cases, supplemental=(), *, feature_set='legacy'):
    if feature_set not in FEATURE_SETS:
        raise ValueError('Unknown proposal feature set')
    cases = [case for case in cases if case['split'] in ('train', 'validation')]
    training = [case for case in cases if case['split'] == 'train' and case['predictions']]
    base_ids = {case['scene_id'] for case in cases}
    base_images = {case.get('image_sha256') for case in cases if case.get('image_sha256')}
    held_out_groups = {case['spatial_group'] for case in cases if case['split'] != 'train'}
    for case in supplemental:
        if (case['split'] != 'train' or case['scene_id'] in base_ids or case.get('image_sha256') in base_images
                or case['spatial_group'] in held_out_groups or not case['spatial_group']
                or len(case['predictions']) != len(case['labels']) or any(label not in (0, 1) for label in case['labels'])):
            raise ValueError('Supplemental cases must be distinct, explicitly labelled training images')
        base_ids.add(case['scene_id'])
        if case.get('image_sha256'):
            base_images.add(case['image_sha256'])
        if case['predictions']:
            training.append(case)
    validation = [case for case in cases if case['split'] == 'validation']
    if not any(case['references'] for case in validation):
        raise ValueError('Positive reviewed validation references are required')
    group_images = {group: sum(case['spatial_group'] == group for case in training)
                    for group in {case['spatial_group'] for case in training}}
    x, y, weights = [], [], []
    for case in training:
        for index, obj in enumerate(case['predictions']):
            x.append(feature_vector(obj, feature_set, case.get('context')))
            # Any independently usable match is a positive. The one-to-one
            # evaluation later still counts duplicate retained detections.
            y.append(case['labels'][index] if 'labels' in case else int(evaluate_objects([obj], case['references'])['matched'] > 0))
            weights.append(1/(group_images[case['spatial_group']]*len(case['predictions'])))
    if set(y) != {0, 1}:
        raise ValueError('Reviewed training proposals need both usable matches and negatives')
    x, y, weights = np.array(x), np.array(y), np.array(weights)
    mean = np.average(x, axis=0, weights=weights)
    scale = np.maximum(np.sqrt(np.average((x-mean)**2, axis=0, weights=weights)), .05)
    trials = []
    for penalty in PENALTIES:
        fitted = fit_logistic((x-mean)/scale, y, penalty, weights)
        model = {'schema_version': 1 if feature_set == 'legacy' else 2, 'model_kind': 'regularized-logistic-proposal-quality',
                 'feature_names': FEATURE_SETS[feature_set], 'mean': mean.tolist(), 'scale': scale.tolist(),
                 'weights': fitted.tolist(), 'regularization': penalty, 'threshold': .5}
        metrics = scores(cases, model)
        trials.append({'model': model, 'metrics': metrics})
    # Threshold is fixed before fitting. Do not search for a threshold on the
    # tiny validation set; prefer stronger regularization when scores tie.
    best = max(trials, key=lambda t: (t['metrics']['splits']['validation']['f1'] or 0,
               t['metrics']['splits']['validation']['recall'] or 0, t['model']['regularization']))
    return {'model': best['model'], 'metrics': best['metrics'], 'baseline': scores(cases),
            'training_proposals': {'usable': int(y.sum()), 'negative': int(len(y)-y.sum()),
                                   'images': len(training), 'groups': len(group_images)},
            'trials': [{'regularization': t['model']['regularization'], 'validation': t['metrics']['splits']['validation']} for t in trials],
            'status': 'experimental-candidate',
            'scope': 'Image-reviewed proposal filtering only. No boundary refinement, height supervision, test-set tuning or automatic deployment. A small validation set does not establish general accuracy.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--supplemental', type=Path, help='Frozen schema-5 scene dataset with explicit instance feedback; training only')
    parser.add_argument('--feature-set', choices=list(FEATURE_SETS), default='legacy', help='Use image texture, neighbourhood colour and border context alongside proposal statistics')
    parser.add_argument('--out', type=Path, default=Path('data/training/scene-proposals'))
    args = parser.parse_args()
    config, run_id, cases = checked_cases(args.dataset, args.experiment, args.prompt)
    extra_version, extra, ignored = supplemental_cases(args.supplemental, config['kind'], args.prompt, cases) if args.supplemental else (None, [], {})
    if args.feature_set == 'image-context':
        cases = [{**case, 'context': case_context(case)} for case in cases]
        extra = [{**case, 'context': case_context(case)} for case in extra]
    report = train(cases, extra, feature_set=args.feature_set)
    provenance = {'dataset_version': config['dataset_version'], 'experiment_run': run_id,
                  'kind': config['kind'], 'prompt': args.prompt, 'code': digest(__file__),
                  'optimizer_code': digest(Path(__file__).with_name('train_roof_form.py'))}
    if args.feature_set == 'image-context':
        provenance['image_context_code'] = digest(Path(__file__).with_name('scene_proposal_features.py'))
    if extra_version:
        provenance.update(supplemental_dataset_version=extra_version, supplemental_code=digest(Path(__file__).with_name('scene_proposal_feedback.py')))
        report['supplemental_training'] = {'dataset_version': extra_version, 'images': len(extra),
            'usable': sum(sum(case['labels']) for case in extra), 'negative': sum(len(case['labels'])-sum(case['labels']) for case in extra),
            'ignored': ignored, 'scope': 'Explicit reviewed instance matches only. Unknown proposals are ignored; no supplemental image is evaluated.'}
    model = {**report.pop('model'), 'provenance': provenance}
    identity = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()[:16]
    directory = args.out/identity
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory/'model.json', model)
    report.update(provenance=provenance, candidate_id=identity, model_path=str(directory/'model.json'))
    atomic_json(directory/'report.json', report)
    if args.feature_set == 'image-context':
        atomic_json(directory/'image-context.json', [{'scene_id': case['scene_id'], 'image_sha256': case['image_sha256'],
            'split': case['split'], 'features': case['context']} for case in [*cases, *extra]])
    if extra_version:
        atomic_json(directory/'supplemental.json', [{'scene_id': case['scene_id'], 'image_sha256': case['image_sha256'],
            'model_signature': case['model_signature'], 'revision': case['revision'], 'spatial_group': case['spatial_group'],
            'labels': [{'prediction_id': obj['id'], 'usable': bool(label)} for obj, label in zip(case['predictions'], case['labels'], strict=True)]}
            for case in extra])
    for case in cases:
        p = probabilities(case['predictions'], model, case.get('context'))
        atomic_json(directory/f'{case["scene_id"]}.json', {
            'candidate_id': identity, 'scene_id': case['scene_id'], 'image_sha256': case['image_sha256'],
            'scores': [{'id': obj['id'], 'quality_score': round(float(value), 6), 'retained': bool(value >= model['threshold'])}
                       for obj, value in zip(case['predictions'], p, strict=True)]})
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
