import copy
import hashlib
import json

import numpy as np
import pytest
from PIL import Image
from scripts.ml.sam3 import Sam3Proposal
from scripts.ml.scene_evaluation import evaluate_objects
from scripts.ml.scene_experiment import compare, evaluate_image, frozen_layers, model_fingerprint


@pytest.fixture
def frozen(tmp_path):
    image = tmp_path/'image.png'
    Image.new('RGB', (20, 20), 'green').save(image)
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    reference = {'id': 'target', 'kind': 'tree', 'ring': [[2, 2], [18, 2], [18, 18], [2, 18]],
                 'holes': [[[6, 6], [14, 6], [14, 14], [6, 14]]]}
    rows, layers = [], []
    for split in ('train', 'validation', 'test'):
        row = {'scene_id': split, 'image_sha256': image_hash, 'image_path': str(image),
               'pixel_to_meters': [[1, 0], [0, 1], [0, 0]], 'split': split, 'spatial_group': split,
               'image_geography': {'status': 'verified'}, 'model_signature': 'original', 'stale': False,
               'reference': [reference], 'prediction': [], 'review': {'revision': 2, 'model_signature': 'original',
               'reviewed_classes': ['tree'], 'objects': [{**reference, 'status': 'confirmed', 'geometry_reviewed': True}]}}
        rows.append(row)
        layers.append({k: row[k] for k in ('scene_id', 'image_sha256', 'model_signature', 'split', 'spatial_group')} | {'revision': 2})
    encoded = '\n'.join(json.dumps(row) for row in rows)+'\n'
    version = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    (tmp_path/'dataset.jsonl').write_text(encoded)
    (tmp_path/'manifest.json').write_text(json.dumps({'version': version}))
    (tmp_path/'evaluation.json').write_text(json.dumps({'dataset_version': version, 'classes': [{'kind': 'tree', 'layers': layers}]}))
    (tmp_path/'tree-coco.json').write_text(json.dumps({'images': [{'scene_id': row['scene_id']} for row in rows]}))
    return tmp_path, rows, layers


def test_frozen_selection_excludes_test_and_refuses_changed_dataset(frozen):
    path, _, _ = frozen
    _, pairs = frozen_layers(path, 'tree')
    assert [layer['scene_id'] for layer, row in pairs] == ['train', 'validation']
    with pytest.raises(ValueError, match='No eligible'):
        frozen_layers(path, 'solar_panel')
    (path/'dataset.jsonl').write_text((path/'dataset.jsonl').read_text()+' ')
    with pytest.raises(ValueError, match='contents do not match'):
        frozen_layers(path, 'tree')


@pytest.mark.parametrize('failure', ['version', 'revision', 'duplicate', 'withheld'])
def test_frozen_evaluation_requires_the_same_export_and_checked_layer(frozen, failure):
    path, _, _ = frozen
    evaluation = json.loads((path/'evaluation.json').read_text())
    if failure == 'version':
        evaluation['dataset_version'] = 'other'
    elif failure == 'revision':
        evaluation['classes'][0]['layers'][0]['revision'] = 99
    elif failure == 'duplicate':
        evaluation['classes'][0]['layers'].append(evaluation['classes'][0]['layers'][0])
    else:
        (path/'tree-coco.json').write_text('{"images": []}')
    (path/'evaluation.json').write_text(json.dumps(evaluation))
    with pytest.raises(ValueError):
        frozen_layers(path, 'tree')


def test_prompt_comparison_uses_production_masks_one_encoding_and_no_annotation_writes(frozen):
    path, rows, layers = frozen
    before = copy.deepcopy(rows[0])
    class Segmenter:
        encodes = 0
        calls = []
        def encode_image(self, image):
            self.encodes += 1
            return ['features']
        def proposals(self, features, prompt, size, **options):
            assert features == ['features'] and size == (20, 20)
            assert options == {'threshold': .45, 'max_proposals': 80}
            self.calls.append(prompt)
            if prompt == 'none':
                return []
            mask = np.zeros((20, 20), dtype=bool)
            mask[2:18, 2:18] = True
            mask[6:14, 6:14] = False
            return [Sam3Proposal(mask, .9, (2, 2, 18, 18))] * 2
    engine = Segmenter()
    result = evaluate_image(layers[0], rows[0], 'tree', ['canopy', 'none'], engine, path)
    assert engine.encodes == 1 and engine.calls == ['canopy', 'none']
    good = result['prompts']['canopy']
    assert good['metrics']['matched'] == good['metrics']['predictions'] == 1
    assert good['metrics']['outline_iou'] == 1 and len(good['predictions'][0]['holes']) == 1
    assert good['predictions'][0]['prompt'] == 'canopy'
    assert not good['predictions'][0]['geometry_reviewed']
    assert result['prompts']['none']['metrics']['missed'] == 1
    assert rows[0] == before
    Image.new('RGB', (20, 20), 'red').save(rows[0]['image_path'])
    with pytest.raises(ValueError, match='Image bytes changed'):
        evaluate_image(layers[0], rows[0], 'tree', ['canopy'], engine, path)


def test_ranking_uses_validation_only_and_preserves_empty_denominators(frozen):
    _, rows, _ = frozen
    ref = rows[0]['reference']
    good, missed, empty = evaluate_objects(ref, ref), evaluate_objects([], ref), evaluate_objects([], [])
    results = [{'split': 'train', 'spatial_group': 'train', 'baseline': missed,
                'prompts': {'training-winner': {'metrics': good}, 'validation-winner': {'metrics': missed}}},
               {'split': 'validation', 'spatial_group': 'validation', 'baseline': missed,
                'prompts': {'training-winner': {'metrics': missed}, 'validation-winner': {'metrics': good}}}]
    report = compare(results, ['training-winner', 'validation-winner'])
    assert report['validation_order'][0] == 'validation-winner'
    negative = compare([{'split': 'validation', 'spatial_group': 'empty', 'baseline': empty,
                         'prompts': {'none': {'metrics': empty}}}], ['none'])
    assert negative['validation_order'] == []
    assert negative['scores']['none']['validation']['f1'] is None


def test_model_fingerprint_changes_with_weights_and_requires_every_component(tmp_path):
    files = ['vision_encoder_int8.onnx', 'text_encoder_int8.onnx', 'decoder_int8.onnx', 'tokenizer.json']
    for name in files:
        (tmp_path/name).write_bytes(b'first')
    first = model_fingerprint(tmp_path)
    (tmp_path/files[0]).write_bytes(b'second')
    assert model_fingerprint(tmp_path) != first
    (tmp_path/files[-1]).unlink()
    with pytest.raises(ValueError, match='Missing model component'):
        model_fingerprint(tmp_path)
