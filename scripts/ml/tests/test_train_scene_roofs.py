import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scripts.ml import roof_form, train_scene_roofs
from scripts.ml.roof_structure import structural_prior
from scripts.ml.scene_roof_dataset import distinct_roofs, frozen_roofs
from scripts.ml.scene_training import export_scenes
from scripts.ml.tiles import tile_xy_to_lonlat
from shapely.geometry import Polygon


@pytest.fixture
def roof_dataset(tmp_path):
    folder = tmp_path/'data/scenes/example'
    folder.mkdir(parents=True)
    pixels = np.full((200, 200, 3), 130, dtype=np.uint8)
    pixels[:100] = 205  # Continuous fold along a two-plane gable ridge.
    image = folder/'image.png'
    Image.fromarray(pixels).save(image)
    sha = hashlib.sha256(image.read_bytes()).hexdigest()
    image.rename(folder/f'{sha}.png')
    ring = [[20, 60], [180, 60], [180, 140], [20, 140]]
    obj = {'id': 'roof', 'kind': 'building', 'label': 'Roof', 'ring': ring, 'holes': [],
           'status': 'confirmed', 'geometry_reviewed': True, 'visibility': 'full',
           'model': {'height_m': 3.2, 'height_verified': False},
           'roof_feedback': {'outline': {'ring': copy.deepcopy(ring), 'holes': []}, 'family': 'gabled'}}
    model = {'id': 'example', 'address_id': None, 'cohort': None, 'image_sha256': sha,
             'image_width': 200, 'image_height': 200, 'pixel_to_meters': [[.1, 0], [0, -.1], [-10, 10]],
             'centroid': dict(zip(('lon', 'lat'), tile_xy_to_lonlat(118443.5, 80419.5, 17), strict=True)),
             'model_signature': 'current', 'objects': [{**obj, 'status': 'candidate', 'geometry_reviewed': False}],
             'groups': [], 'region': {'split': 'train', 'spatial_group': 'z17:118443:80419'}}
    review = {'revision': 1, 'model_signature': 'current', 'objects': [obj], 'groups': [], 'reviewed_classes': []}
    (folder/'latest.json').write_text(json.dumps(model))
    (folder/'review.json').write_text(json.dumps(review))
    manifest = export_scenes(tmp_path)
    return tmp_path, Path(manifest['dataset']).parent, folder


def test_scene_reviews_reach_features_without_masks_heights_or_predictions(roof_dataset, monkeypatch):
    root, dataset, folder = roof_dataset
    version, cases, ignored = frozen_roofs(dataset)
    assert len(cases) == 1 and cases[0]['family'] == 'gabled' and not ignored
    # Current mutable review/model changes do not alter an immutable training run.
    (folder/'review.json').write_text('{}')
    (folder/'latest.json').write_text('{}')
    assert frozen_roofs(dataset)[1] == cases
    monkeypatch.setattr(roof_form, 'learned_choice', lambda *_: pytest.fail('Installed weights entered training features'))
    observed = train_scene_roofs.image_features(cases[0], root)
    assert len(observed['ends']) == 2
    assert all(e['gable_support'] > e['hip_support'] for e in observed['ends'])
    altered = {**cases[0], 'family': 'hipped', 'model': {'height_m': 99},
               'roof_annotation': {'vertices': [[1000, 1000, 999]]}, 'roof_type': 'hipped'}
    assert train_scene_roofs.image_features(altered, root)['ends'] == observed['ends']
    report = train_scene_roofs.run(dataset, root/'runs', root)
    assert report['dataset_version'] == version and report['status'] == 'needs-reviewed-labels'
    assert report['usable_roofs']['train']['gabled'] == {'roofs': 1, 'images': 1, 'areas': 1}
    assert report['test_evaluation'] == 'not-run' and report['model_path'] is None
    assert not list((root/'runs').glob('*/roof-form.json'))
    assert train_scene_roofs.run(dataset, root/'runs', root) == report


def test_inference_and_training_compare_the_same_unchanged_hip_prior(monkeypatch):
    footprint = Polygon([(0, 0), (20, 0), (20, 8), (0, 8)])
    prior = structural_prior(footprint, 3, 6)
    image = np.full((150, 250, 3), 130, dtype=np.uint8)
    image[:80] = 205

    class Frame:
        def metre_to_pixel(self, x, y):
            return x*10+20, 120-y*10

    observations = roof_form.roof_end_evidence(prior, footprint, 3, image, Frame())
    monkeypatch.setattr(roof_form, 'learned_choice', lambda *_: 'gable')
    parts, selection = roof_form.select_roof_ends(prior, footprint, 3, image, Frame(), {})
    assert len(parts) == 2 and selection['gable_ends'] == 2
    for first, second in zip(observations, selection['ends'], strict=True):
        assert all(first[k] == second[k] for k in ('hip_support', 'gable_support', 'hip_lines', 'gable_lines'))


@pytest.mark.parametrize('mutation', ['sidecar', 'version', 'unconfirmed', 'unchecked', 'geography', 'outside_group'])
def test_frozen_reader_independently_checks_labels_and_provenance(roof_dataset, mutation):
    _, dataset, _ = roof_dataset
    manifest = json.loads((dataset/'manifest.json').read_text())
    row = json.loads((dataset/'dataset.jsonl').read_text())
    sidecar = json.loads((dataset/'roof-feedback.jsonl').read_text())
    if mutation == 'sidecar':
        sidecar['reference']['family'] = 'hipped'
    elif mutation == 'version':
        row['scene_id'] = 'changed'
    elif mutation == 'unconfirmed':
        row['review']['objects'][0]['status'] = 'candidate'
    elif mutation == 'unchecked':
        del row['review']['objects'][0]['roof_feedback']
    elif mutation == 'geography':
        row['centroid']['lon'] += 1
    else:
        row['spatial_group'] = 'z17:1:1'
        sidecar['spatial_group'] = row['spatial_group']
    encoded = json.dumps(row, sort_keys=True)+'\n'
    (dataset/'dataset.jsonl').write_text(encoded)
    if mutation != 'version':
        manifest['version'] = hashlib.sha256(encoded.encode()).hexdigest()[:16]
        sidecar['dataset_version'] = manifest['version']
        (dataset/'manifest.json').write_text(json.dumps(manifest))
    (dataset/'roof-feedback.jsonl').write_text(json.dumps(sidecar)+'\n')
    with pytest.raises(ValueError):
        frozen_roofs(dataset)


def test_duplicate_views_count_once_and_conflicting_family_is_withheld(roof_dataset):
    _, dataset, _ = roof_dataset
    case = frozen_roofs(dataset)[1][0]
    second = {**case, 'scene_id': 'other-capture', 'object_id': 'other-id'}
    chosen, excluded = distinct_roofs([case, second])
    assert len(chosen) == 1 and excluded[0]['reason'] == 'repeated_roof'
    chosen, excluded = distinct_roofs([case, {**second, 'family': 'hipped'}])
    assert not chosen and all(e['reason'] == 'conflicting_family' for e in excluded)
    with pytest.raises(ValueError, match='multiple dataset splits'):
        distinct_roofs([case, {**second, 'split': 'test'}])
    moved = copy.deepcopy(second)
    moved['pixel_to_meters'][2][0] += 30
    assert len(distinct_roofs([case, moved])[0]) == 2


def test_changed_images_and_published_results_fail_without_overwriting(roof_dataset):
    root, dataset, _ = roof_dataset
    case = frozen_roofs(dataset)[1][0]
    with pytest.raises(ValueError, match='dimensions'):
        train_scene_roofs.image_features({**case, 'image_width': 201}, root)
    report = train_scene_roofs.run(dataset, root/'runs', root)
    result = Path(report['report_path'])
    before = result.read_bytes()
    features = result.parent/'features.json'
    features.write_text('[]')
    with pytest.raises(ValueError, match='Published roof training run changed'):
        train_scene_roofs.run(dataset, root/'runs', root)
    assert result.read_bytes() == before
    Path(case['image_path']).write_bytes(b'changed bytes')
    with pytest.raises(ValueError, match='image bytes changed'):
        train_scene_roofs.image_features(case, root)


def fit_cases():
    cases = []
    for split, count, areas in [('train', 10, 3), ('validation', 3, 2), ('test', 3, 2)]:
        for family in ('gabled', 'hipped'):
            for i in range(count):
                cases.append({'scene_id': f'{split}-{family}-{i}', 'object_id': 'roof', 'family': family,
                              'image_sha256': f'{split}-{family}-{i}', 'spatial_group': f'{split}-area-{i%areas}', 'split': split})
    return cases


def synthetic_features(case, _root):
    gable = case['family'] == 'gabled'
    return {**case, 'ends': [{'hip_support': .1 if gable else .9, 'gable_support': .9 if gable else .1}]*2}


def test_readiness_never_opens_test_images_and_does_not_count_ends_as_roofs(monkeypatch):
    calls = []

    def observed(case, root):
        calls.append(case['split'])
        out = synthetic_features(case, root)
        out['ends'] *= 100
        return out

    monkeypatch.setattr(train_scene_roofs, 'image_features', observed)
    cases = fit_cases()
    cases = [c for c in cases if c['split'] != 'train' or c['family'] == 'hipped']
    report, model, _ = train_scene_roofs.prepare_and_fit(cases)
    assert model is None and 'test' not in calls
    assert report['usable_roofs']['train']['hipped']['roofs'] == 10
    assert any('train: gabled' in message for message in report['missing'])
    # Counts alone cannot pass when all ten roofs come from one locality.
    cases = [{**c, 'spatial_group': c['split']} for c in fit_cases()]
    report, model, _ = train_scene_roofs.prepare_and_fit(cases)
    assert model is None and any('areas' in message for message in report['missing'])


def test_test_features_cannot_select_weights_and_areas_have_equal_weight(monkeypatch):
    order = []
    real_fit = train_scene_roofs.fit_candidate

    def fitting(cases):
        order.append('weights-frozen')
        return real_fit(cases)

    def observed(case, root):
        if case['split'] == 'test':
            assert 'weights-frozen' in order
        order.append(case['split'])
        return synthetic_features(case, root)

    monkeypatch.setattr(train_scene_roofs, 'image_features', observed)
    monkeypatch.setattr(train_scene_roofs, 'fit_candidate', fitting)
    report, model, feature_cases = train_scene_roofs.prepare_and_fit(fit_cases())
    assert report['status'] == 'experimental-candidate' and model['validated']
    assert report['test']['balanced_accuracy'] == 1
    x, y, w = train_scene_roofs.arrays(feature_cases, 'train')
    assert len(x) == 40 and np.isclose(w[y == 0].sum(), w[y == 1].sum())
    for family in ('gabled', 'hipped'):
        positions, cursor = {}, 0
        for c in [c for c in feature_cases if c['split'] == 'train']:
            if c['family'] == family:
                positions[c['spatial_group']] = positions.get(c['spatial_group'], 0)+w[cursor:cursor+len(c['ends'])].sum()
            cursor += len(c['ends'])
        assert np.ptp(list(positions.values())) < 1e-9

    def wrong_test(case, root):
        return synthetic_features({**case, 'family': 'hipped' if case['family'] == 'gabled' else 'gabled'}, root) | {'family': case['family']} \
            if case['split'] == 'test' else synthetic_features(case, root)

    monkeypatch.setattr(train_scene_roofs, 'image_features', wrong_test)
    changed_report, changed_model, _ = train_scene_roofs.prepare_and_fit(fit_cases())
    assert changed_model['weights'] == model['weights'] and changed_report['regularization'] == report['regularization']
    assert changed_report['test']['balanced_accuracy'] == 0
    assert changed_report['selection_weights_sha256'] == report['selection_weights_sha256']
    with pytest.raises(ValueError, match='Test features'):
        real_fit(feature_cases)


def test_insufficient_test_evidence_keeps_fitted_candidate_unvalidated(monkeypatch):
    def observed(case, root):
        out = synthetic_features(case, root)
        if case['split'] == 'test':
            out.update(ends=[], excluded_reason='insufficient_visible_evidence')
        return out

    monkeypatch.setattr(train_scene_roofs, 'image_features', observed)
    report, model, _ = train_scene_roofs.prepare_and_fit(fit_cases())
    assert model is not None and not model['validated']
    assert report['test_evaluation'] == 'incomplete-visible-evidence'
    assert 'test' not in model and 'test' not in report and report['missing']


def test_published_synthetic_candidate_is_loadable_and_reproducible(roof_dataset, monkeypatch):
    root, dataset, folder = roof_dataset
    version, source, _ = frozen_roofs(dataset)
    cases = [{**source[0], **c, 'image_path': str(next(folder.glob('*.png'))),
              'image_sha256': source[0]['image_sha256']} for c in fit_cases()]
    # Synthetic fitting is isolated from the independently tested export reader
    # and physical-roof deduplication. This proves artifact/inference wiring,
    # not accuracy on real held-out imagery.
    monkeypatch.setattr(train_scene_roofs, 'frozen_roofs', lambda _: (version, cases, {}))
    monkeypatch.setattr(train_scene_roofs, 'distinct_roofs', lambda cases: (cases, []))
    monkeypatch.setattr(train_scene_roofs, 'image_features', synthetic_features)
    report = train_scene_roofs.run(dataset, root/'runs', root)
    assert report['model_path'] and report['promotion_eligible']
    model = json.loads(Path(report['model_path']).read_text())
    assert model['provenance']['dataset_version'] == version
    assert model['feature_protocol'] == 'independent-hip-prior-ends-v1'
    monkeypatch.setenv('ROOF_FORM_MODEL', report['model_path'])
    assert roof_form.learned_choice(.1, .9) == 'gable'
    assert roof_form.learned_choice(.9, .1) == 'hip'
    assert train_scene_roofs.run(dataset, root/'runs', root) == report
