"""Fit roof-end image evidence from explicit scene roof-family corrections.

The frozen export supplies labels; the shared RGB comparison supplies inputs.
No annotated ridge coordinates, estimated heights or model roof types are input
features. Test imagery is opened only after fitting and validation selection.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

import cv2
import numpy as np
import shapely
from PIL import Image
from shapely.errors import GEOSException
from shapely.geometry import Polygon

from .roof_form import roof_end_evidence
from .roof_structure import audit_roof, structural_prior
from .scene_roof_dataset import digest, distinct_roofs, frozen_roofs, inverse_frame
from .scene_training import atomic_json
from .train_roof_form import FEATURES, features, fit_logistic, metrics, probability_metrics

FAMILIES = ('hipped', 'gabled')
SPLITS = ('train', 'validation', 'test')
REQUIREMENTS = {'train': {'roofs_per_class': 10, 'areas_per_class': 3},
                'validation': {'roofs_per_class': 3, 'areas_per_class': 2},
                'test': {'roofs_per_class': 3, 'areas_per_class': 2}}
MIN_SUPPORT = .2  # Same visibility gate used by learned_choice at inference.
PENALTIES = (.001, .01, .1, 1.)


def image_features(case, root=Path('.')):
    path = Path(case['image_path'])
    if not path.is_absolute():
        path = root/path
    if digest(path) != case['image_sha256']:
        raise ValueError('Roof training image bytes changed')
    with Image.open(path) as source:
        if source.size != (case['image_width'], case['image_height']):
            raise ValueError('Roof training image dimensions changed')
        image = source.convert('RGB')
    frame = np.asarray(case['pixel_to_meters'])

    def transform(points):
        return np.c_[points, np.ones(len(points))] @ frame
    footprint = Polygon(transform(case['ring']), [transform(h) for h in case['holes']])
    inverse = np.asarray(inverse_frame(frame))

    class Frame:
        def metre_to_pixel(self, x, y):
            return np.array([x, y, 1]) @ inverse

    # Constant heights define hypothetical planes only. Do not read the edited
    # graph or guessed building heights when preparing image classifier inputs.
    try:
        prior = structural_prior(footprint, 3, 6, 'hipped', regularize_eaves=False)
        if not audit_roof(prior, footprint, 3)['passed']:
            return {**case, 'ends': [], 'excluded_reason': 'prior_geometry_failed'}
        observations = roof_end_evidence(prior, footprint, 3, image, Frame())
    except (ValueError, GEOSException) as error:
        return {**case, 'ends': [], 'excluded_reason': 'unsupported_geometry', 'detail': str(error)}
    ends = [end for end in observations if 'hip_support' in end and max(end['hip_support'], end['gable_support']) >= MIN_SUPPORT]
    if any(not np.isfinite(features(end)).all() or not 0 <= min(end['hip_support'], end['gable_support']) <= max(end['hip_support'], end['gable_support']) <= 1 for end in ends):
        raise ValueError('Roof evidence features are invalid')
    return {**case, 'ends': ends, 'observations': observations,
            **({'excluded_reason': 'insufficient_visible_evidence'} if not ends else {})}


def counts(cases):
    result = {}
    for split in SPLITS:
        result[split] = {}
        for family in FAMILIES:
            selected = [c for c in cases if c['split'] == split and c['family'] == family]
            result[split][family] = {'roofs': len(selected), 'images': len({c['image_sha256'] for c in selected}),
                                     'areas': len({c['spatial_group'] for c in selected})}
    return result


def missing_labels(coverage, splits=SPLITS):
    missing = []
    for split in splits:
        for family in FAMILIES:
            for target, requirement in (('roofs', 'roofs_per_class'), ('areas', 'areas_per_class')):
                short = REQUIREMENTS[split][requirement] - coverage[split][family][target]
                if short > 0:
                    missing.append(f'{split}: {family} needs {short} more {target}')
    return missing


def arrays(cases, split):
    selected = [c for c in cases if c['split'] == split and c['ends']]
    area_roofs = Counter((c['family'], c['spatial_group']) for c in selected)
    family_areas = Counter(family for family, _ in area_roofs)
    x, y, w = [], [], []
    for case in selected:
        for end in case['ends']:
            x.append(features(end))
            y.append(int(case['family'] == 'gabled'))
            # Balance classes and areas before roofs/ends, so repeated local
            # architecture and complex roofs cannot dominate the optimization.
            w.append(1 / (family_areas[case['family']] * area_roofs[case['family'], case['spatial_group']] * len(case['ends'])))
    return np.asarray(x), np.asarray(y), np.asarray(w)


def fit_candidate(feature_cases):
    """Fit train, select validation; this function receives no test features."""
    if any(c['split'] == 'test' for c in feature_cases):
        raise ValueError('Test features cannot enter roof model selection')
    x, y, w = arrays(feature_cases, 'train')
    vx, vy, vw = arrays(feature_cases, 'validation')
    mean = np.average(x, axis=0, weights=w)
    scale = np.maximum(np.sqrt(np.average((x-mean)**2, axis=0, weights=w)), .05)
    trials = []
    for penalty in PENALTIES:
        weights = fit_logistic((x-mean)/scale, y, penalty, w)
        score = metrics((vx-mean)/scale, vy, weights, vw)
        trials.append((score['log_loss'], -penalty, weights, score))
    _, negative_penalty, weights, validation = min(trials, key=lambda item: (item[0], item[1]))
    baseline = probability_metrics(np.where((vx[:, 1] >= .4) & (vx[:, 2] >= .2), .8,
                                            np.where((vx[:, 0] >= .4) & (vx[:, 2] <= -.2), .2, .5)), vy, vw)
    model = {'schema_version': 1, 'feature_names': FEATURES, 'mean': mean.tolist(), 'scale': scale.tolist(),
             'weights': weights.tolist(), 'validated': False, 'validation': validation,
             'feature_protocol': 'independent-hip-prior-ends-v1'}
    return model, {'regularization': -negative_penalty, 'validation': validation, 'validation_baseline': baseline,
                   'trials': [{'regularization': -p, 'validation': s} for _, p, _, s in trials],
                   'weighting': 'equal class, equal area within class, equal roof within area, equal end within roof'}


def prepare_and_fit(cases, root=Path('.')):
    reviewed = counts(cases)
    feature_cases = [image_features(c, root) for c in cases if c['split'] in ('train', 'validation')]
    usable = [c for c in feature_cases if c['ends']]
    usable_counts = counts(usable)
    missing = missing_labels(usable_counts, ('train', 'validation')) + missing_labels(reviewed, ('test',))
    report = {'status': 'needs-reviewed-labels', 'requirements': REQUIREMENTS, 'reviewed_roofs': reviewed,
              'usable_roofs': usable_counts, 'missing': missing, 'test_evaluation': 'not-run',
              'feature_exclusions': [{'scene_id': c['scene_id'], 'object_id': c['object_id'], 'reason': c['excluded_reason']}
                                     for c in feature_cases if not c['ends']]}
    if missing:
        return report, None, feature_cases
    # All choices freeze here. Test features can only report on these weights.
    model, fit = fit_candidate(usable)
    frozen_weights = json.dumps({k: model[k] for k in ('weights', 'mean', 'scale')}, sort_keys=True)
    report.update(fit, status='experimental-candidate', selection_weights_sha256=hashlib.sha256(frozen_weights.encode()).hexdigest())
    test_cases = [image_features(c, root) for c in cases if c['split'] == 'test']
    feature_cases += test_cases
    report['usable_roofs'] = counts([c for c in feature_cases if c['ends']])
    report['missing'] = missing_labels(report['usable_roofs'], ('test',))
    report['feature_exclusions'] += [{'scene_id': c['scene_id'], 'object_id': c['object_id'], 'reason': c['excluded_reason']}
                                     for c in test_cases if not c['ends']]
    report['test_evaluation'] = 'incomplete-visible-evidence' if report['missing'] else 'frozen-weights-report-only'
    if not report['missing']:
        tx, ty, tw = arrays(test_cases, 'test')
        report['test'] = metrics((tx-model['mean'])/model['scale'], ty, np.asarray(model['weights']), tw)
        model['test'] = report['test']
    validation, baseline = fit['validation'], fit['validation_baseline']
    report['promotion_eligible'] = (not report['missing'] and validation['balanced_accuracy'] >= .75
        and validation['balanced_accuracy'] >= baseline['balanced_accuracy']
        and validation['log_loss'] < min(.60, baseline['log_loss']))
    model['validated'] = report['promotion_eligible']
    assert frozen_weights == json.dumps({k: model[k] for k in ('weights', 'mean', 'scale')}, sort_keys=True)
    return report, model, feature_cases


def checked_result(directory, config, root):
    if not directory.exists():
        return None
    artifacts = json.loads((directory/'artifacts.json').read_text())
    report = json.loads((directory/'training-report.json').read_text())
    expected = {'config.json', 'features.json', 'training-report.json'} | ({'roof-form.json'} if report.get('model_path') else set())
    if (set(artifacts) != expected or {p.name for p in directory.iterdir()} != expected | {'artifacts.json'}
            or json.loads((directory/'config.json').read_text()) != config
            or any(digest(directory/name) != sha for name, sha in artifacts.items())):
        raise ValueError('Published roof training run changed; results were preserved')
    for case in json.loads((directory/'features.json').read_text()):
        path = Path(case['image_path'])
        if digest(path if path.is_absolute() else root/path) != case['image_sha256']:
            raise ValueError('Published roof training image changed')
    return report


def run(dataset, out=Path('data/training/scene-roofs'), root=Path('.')):
    version, all_cases, ignored = frozen_roofs(dataset)
    distinct, duplicates = distinct_roofs(all_cases)
    unsupported = [{'scene_id': c['scene_id'], 'object_id': c['object_id'], 'reason': 'family_not_binary'}
                   for c in distinct if c['family'] not in FAMILIES]
    cases = [c for c in distinct if c['family'] in FAMILIES]
    config = {'schema_version': 1, 'dataset_version': version, 'dataset_sha256': digest(Path(dataset)/'dataset.jsonl'),
              'feature_protocol': 'independent-hip-prior-ends-v1', 'requirements': REQUIREMENTS,
              'minimum_support': MIN_SUPPORT, 'regularization_candidates': list(PENALTIES),
              'code': {name: digest(Path(__file__).with_name(name+'.py')) for name in
                       ('train_scene_roofs', 'scene_roof_dataset', 'scene_roof_feedback', 'roof_form', 'roof_structure', 'train_roof_form', 'scene_split', 'scene_training', 'tiles')},
              'runtime': {'numpy': np.__version__, 'opencv': cv2.__version__, 'shapely': shapely.__version__,
                          'python': sys.version, 'pillow': importlib.metadata.version('Pillow'),
                          'skeleton': importlib.metadata.version('py-straight-skeleton')}}
    identity = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    out, root = Path(out), Path(root)
    out.mkdir(parents=True, exist_ok=True)
    directory = out/identity
    with (out/'.training.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = checked_result(directory, config, root)
        if existing is not None:
            return existing
        report, model, feature_cases = prepare_and_fit(cases, root)
        report.update(schema_version=1, run_id=identity, dataset_version=version,
                      model_kind='regularized-logistic-roof-end-classifier', feature_names=FEATURES,
                      ignored=ignored, instance_exclusions=duplicates+unsupported,
                      family_observations=dict(Counter(c['family'] for c in distinct)),
                      review_links=[{'scene_id': c['scene_id'], 'object_id': c['object_id'], 'family': c['family'], 'split': c['split'],
                                     'url': '/scenes?'+urlencode({'scene': c['scene_id'], 'object': c['object_id'], 'kind': 'building', 'status': 'all'})}
                                    for c in cases],
                      report_path=str(directory/'training-report.json'), model_path=str(directory/'roof-form.json') if model else None,
                      scope='Conditional hip/gable end classification from imagery and reviewed outlines. No boundary, apex, height, solar or neural weight training. No automatic deployment. Local imagery reviews do not establish survey accuracy or regional generalization.')
        if model:
            model['provenance'] = {'run_id': identity, **config}
            model['dataset'] = str(Path(dataset)/'roof-feedback.jsonl')
        temp = Path(tempfile.mkdtemp(prefix='.roof-training-', dir=out))
        try:
            atomic_json(temp/'config.json', config)
            atomic_json(temp/'training-report.json', report)
            atomic_json(temp/'features.json', feature_cases)
            if model:
                atomic_json(temp/'roof-form.json', model)
            atomic_json(temp/'artifacts.json', {p.name: digest(p) for p in sorted(temp.iterdir())})
            temp.rename(directory)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True, help='Frozen schema-6 scene export directory')
    parser.add_argument('--out', type=Path, default=Path('data/training/scene-roofs'))
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    print(json.dumps(run(args.dataset, args.out, args.root), indent=2))


if __name__ == '__main__':
    main()
