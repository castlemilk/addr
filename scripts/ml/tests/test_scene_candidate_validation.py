import copy
import hashlib
import json

import numpy as np
import pytest
from PIL import Image
from scripts.ml.sam3 import Sam3Proposal
from scripts.ml.scene_candidate_validation import check_image, published_check, validation_layers
from scripts.ml.tiles import tile_xy_to_lonlat
from scripts.ml.train_scene_proposals import FEATURE_SETS


@pytest.fixture
def reference(tmp_path):
    path = tmp_path/'image.png'
    Image.new('RGB', (32, 32), 'grey').save(path)
    lon, lat = tile_xy_to_lonlat(118441.5, 80418.5, 17)
    row = {'scene_id': 'new', 'split': 'validation', 'spatial_group': 'z17:118441:80418',
           'image_path': str(path), 'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
           'centroid': {'lon': lon, 'lat': lat}, 'pixel_to_meters': [[.1, 0], [0, -.1], [0, 0]],
           'model_signature': 'original', 'revision': 1,
           'reference': [{'id': 'label', 'kind': 'solar_panel', 'ring': [[4, 4], [20, 4], [20, 20], [4, 20]]}], 'prediction': []}
    return row


def test_later_checks_exclude_training_test_and_previously_selected_views(reference):
    new = reference
    old = {**new, 'scene_id': 'old', 'image_sha256': 'different'}
    forbidden = [{**new, 'scene_id': split, 'split': split, 'image_path': 'must-not-read'} for split in ('train', 'test')]
    used = [old]
    pairs = [(r, r) for r in [*forbidden, old, new, {**new, 'scene_id': 'duplicate-frame'}]]
    assert [row['scene_id'] for _, row in validation_layers(pairs, used)] == ['new']
    assert validation_layers([(new, new)], [{**old, 'image_sha256': new['image_sha256']}]) == []


def test_later_checks_refuse_training_geography_and_false_validation_metadata(reference):
    with pytest.raises(ValueError, match='training area'):
        validation_layers([(reference, reference)], [{**reference, 'scene_id': 'trained', 'image_sha256': 'other', 'split': 'train'}])
    lon, lat = tile_xy_to_lonlat(118443.5, 80418.5, 17)
    wrong = {**reference, 'centroid': {'lon': lon, 'lat': lat}}
    with pytest.raises(ValueError, match='whole-image'):
        validation_layers([(wrong, wrong)], [])


def test_frozen_prompt_scores_and_lost_matches_are_preserved_without_fitting(reference, tmp_path):
    class Segmenter:
        calls = []
        def encode_image(self, image):
            return 'encoded'
        def proposals(self, features, prompt, size, **kwargs):
            self.calls.append(prompt)
            mask = np.zeros((32, 32), dtype=bool)
            mask[4:21, 4:21] = True
            return [Sam3Proposal(mask, .9, (4, 4, 20, 20))]
    engine = Segmenter()
    model = {'provenance': {'kind': 'solar_panel', 'prompt': 'exact frozen wording'},
             'mean': [0]*8, 'scale': [1]*8, 'weights': [-2]+[0]*8, 'threshold': .5}
    original = copy.deepcopy((reference, model))
    result = check_image(reference, reference, model, engine, tmp_path)
    assert engine.calls == ['exact frozen wording']
    assert result['result']['prompts']['exact frozen wording']['metrics']['matched'] == 1
    assert result['filtered']['matched'] == 0 and result['filtered']['missed_ids'] == ['label']
    assert not result['scores'][0]['retained']
    assert original == (reference, model)
    contextual = {**model, 'feature_names': FEATURE_SETS['image-context'],
                  'mean': [0]*15, 'scale': [1]*15, 'weights': [-2]+[0]*15}
    checked = check_image(reference, reference, contextual, engine, tmp_path)
    assert len(checked['image_context'][checked['scores'][0]['id']]) == 7
    assert checked['filtered']['matched'] == 0
    assert original == (reference, model)
    Image.new('RGB', (32, 32), 'red').save(reference['image_path'])
    with pytest.raises(ValueError, match='Image bytes changed'):
        check_image(reference, reference, model, engine, tmp_path)


def test_published_checks_are_immutable_and_changed_predictions_fail(tmp_path):
    encoded = '{"candidate": "frozen"}'
    assert not published_check(tmp_path, encoded, ['scene'])
    (tmp_path/'config.json').write_text(encoded)
    (tmp_path/'scene.json').write_text('original')
    report = {'check_id': hashlib.sha256(encoded.encode()).hexdigest()[:16],
              'scene_files': {'scene': hashlib.sha256(b'original').hexdigest()}}
    (tmp_path/'report.json').write_text(json.dumps(report))
    assert published_check(tmp_path, encoded, ['scene'])
    with pytest.raises(ValueError, match='Published validation check changed'):
        published_check(tmp_path, encoded, ['different'])
    (tmp_path/'scene.json').write_text('changed')
    with pytest.raises(ValueError, match='Published validation check changed'):
        published_check(tmp_path, encoded, ['scene'])
    assert (tmp_path/'scene.json').read_text() == 'changed'
