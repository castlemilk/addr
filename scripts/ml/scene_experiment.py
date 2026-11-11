"""Compare SAM3 prompts on frozen reviewed train/validation image layers.

Uses the production extraction and matching code. Predictions are experiment
artifacts, never annotations. Test images are excluded from prompt selection.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image

from .sam3 import DECODER_FILES, TEXT_FILES, VISION_FILES, Sam3TextSegmenter
from .scene_evaluation import evaluate_objects, ratio, summarize
from .scene_extract import PROMPTS, extract_objects
from .scene_training import atomic_json


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def frozen_layers(dataset, kind):
    dataset = Path(dataset)
    manifest = json.loads((dataset/'manifest.json').read_text())
    encoded = (dataset/'dataset.jsonl').read_text()
    if hashlib.sha256(encoded.encode()).hexdigest()[:16] != manifest['version']:
        raise ValueError('Frozen dataset contents do not match their version')
    evaluation = json.loads((dataset/'evaluation.json').read_text())
    if evaluation['dataset_version'] != manifest['version']:
        raise ValueError('Evaluation belongs to a different dataset')
    rows = [json.loads(line) for line in encoded.splitlines() if line.strip()]
    by_id = {row['scene_id']: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError('Duplicate scene identities in frozen dataset')
    category = next((c for c in evaluation['classes'] if c['kind'] == kind), None)
    layers = [layer for layer in category['layers'] if layer['split'] in ('train', 'validation')] if category else []
    coco_path = dataset/f'{kind}-coco.json'
    export_ids = {image['scene_id'] for image in json.loads(coco_path.read_text())['images']} if coco_path.exists() else set()
    seen = set()
    for layer in layers:
        row = by_id[layer['scene_id']]
        review = row['review']
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', row['scene_id']):
            raise ValueError('Invalid scene identity')
        if layer['scene_id'] in seen:
            raise ValueError('Duplicate evaluation layer')
        seen.add(layer['scene_id'])
        if (row['scene_id'] not in export_ids or row.get('stale') or review is None or kind not in review['reviewed_classes']
                or row['image_geography']['status'] != 'verified'
                or any(o['kind'] == kind and (o['status'] in ('candidate', 'needs_adjustment')
                       or (o['status'] == 'confirmed' and not o['geometry_reviewed'])) for o in review['objects'])
                or review['model_signature'] != row['model_signature']
                or any(layer[key] != row[key] for key in ('model_signature', 'image_sha256', 'spatial_group'))
                or layer['revision'] != review['revision']
                or layer['split'] != ('validation' if row['split'] == 'val' else row['split'])):
            raise ValueError('Frozen evaluation layer does not match its checked reference')
    if not layers:
        raise ValueError('No eligible train/validation image layers for this class')
    return manifest['version'], [(layer, by_id[layer['scene_id']]) for layer in layers]


class PromptView:
    """Reuse one image encoding while retaining production mask filtering."""
    def __init__(self, segmenter, features, prompt):
        self.segmenter, self.features, self.prompt = segmenter, features, prompt

    def encode_image(self, image):
        return self.features

    def proposals(self, features, _prompt, image_size, **kwargs):
        return self.segmenter.proposals(features, self.prompt, image_size, **kwargs)


def evaluate_image(layer, row, kind, prompts, segmenter, root):
    image_path = Path(row['image_path'])
    if not image_path.is_absolute():
        image_path = Path(root)/image_path
    if digest(image_path) != row['image_sha256']:
        raise ValueError(f'Image bytes changed for {row["scene_id"]}')
    with Image.open(image_path) as source:
        image = source.convert('RGB')
    features = segmenter.encode_image(image)
    references = [o for o in row['reference'] if o['kind'] == kind]
    originals = [o for o in row['prediction'] if o['kind'] == kind]
    seeds = [o for o in originals if o.get('source') in ('roof-pipeline', 'crm-roof-pipeline')]
    result = {key: layer[key] for key in ('scene_id', 'split', 'spatial_group', 'revision', 'image_sha256', 'model_signature')}
    result.update(baseline=evaluate_objects(originals, references), prompts={})
    for prompt in prompts:
        objects, _ = extract_objects(image, row['image_sha256'], np.asarray(row['pixel_to_meters']),
                                     PromptView(segmenter, features, prompt), objects=seeds, kinds=[kind])
        # Record the actual wording used by PromptView, not the default wording
        # attached by the production extractor. Seed provenance is unchanged.
        predictions = [{**o, 'prompt': prompt} if o.get('source') == 'sam3-text-onnx' else o for o in objects]
        result['prompts'][prompt] = {'metrics': evaluate_objects(predictions, references), 'predictions': predictions}
    return result


def compare(results, prompts):
    scores = {}
    for prompt in ['saved-baseline', *prompts]:
        layers = [{**(r['baseline'] if prompt == 'saved-baseline' else r['prompts'][prompt]['metrics']),
                   'spatial_group': r['spatial_group'], 'split': r['split']} for r in results]
        scores[prompt] = {}
        for split in ('train', 'validation'):
            metrics = summarize([layer for layer in layers if layer['split'] == split])
            metrics['f1'] = ratio(2*metrics['matched'], metrics['predictions']+metrics['references'])
            scores[prompt][split] = metrics
    # A prompt cannot be ranked for recall without positive validation labels.
    order = sorted(prompts, key=lambda p: (-(scores[p]['validation']['f1'] or 0),
                   -(scores[p]['validation']['mean_outline_iou'] or 0), prompts.index(p))) \
        if scores['saved-baseline']['validation']['references'] else []
    return {'scores': scores, 'validation_order': order,
            'selection_rule': 'Validation instance F1, then mean per-image union outline IoU; original prompt order breaks ties.',
            'scope': 'Prompt experiment on checked train/validation layers. No test images, weight training, deployment or annotation changes. Small image counts are not evidence of general accuracy.'}


def model_fingerprint(models_dir):
    paths = []
    for alternatives in [VISION_FILES, TEXT_FILES, DECODER_FILES, ['tokenizer.json']]:
        found = next((Path(models_dir)/name for name in alternatives if (Path(models_dir)/name).exists()), None)
        if found is None:
            raise ValueError(f'Missing model component: {alternatives}')
        paths.append(found)
    return {path.name: digest(path) for path in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True, help='Frozen scene export directory')
    parser.add_argument('--kind', choices=sorted(PROMPTS), default='solar_panel')
    parser.add_argument('--prompt', action='append', required=True, help='Repeat for each candidate wording')
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--models-dir', type=Path, default=Path('scripts/ml/models/sam3'))
    parser.add_argument('--out', type=Path, default=Path('data/experiments/scenes'))
    args = parser.parse_args()
    prompts = list(dict.fromkeys(prompt.strip() for prompt in args.prompt))
    if not 1 <= len(prompts) <= 8 or any(not p or len(p) > 150 or p == 'saved-baseline' for p in prompts):
        parser.error('Choose one to eight distinct prompts, each 1–150 characters')
    version, layers = frozen_layers(args.dataset, args.kind)
    config = {'schema_version': 1, 'dataset_version': version, 'kind': args.kind, 'prompts': prompts,
              'splits': ['train', 'validation'], 'threshold': .45, 'max_proposals': 80,
              'models': model_fingerprint(args.models_dir),
              'code': {name: digest(Path(__file__).with_name(name+'.py')) for name in
                       ('scene_experiment', 'scene_extract', 'scene_evaluation', 'sam3', 'segmentation')}}
    run_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    out = args.out/run_id
    lock_path = args.root/'data/scenes/.extraction.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(75, 'Another extraction or experiment is running. Retry when it finishes.\n')
        out.mkdir(parents=True, exist_ok=True)
        config_path = out/'config.json'
        if config_path.exists() and json.loads(config_path.read_text()) != config:
            raise ValueError('Existing run has a different configuration')
        atomic_json(config_path, config)
        results, segmenter = [], None
        for index, (layer, row) in enumerate(layers):
            path = out/f'{row["scene_id"]}.json'
            if path.exists():
                cached = json.loads(path.read_text())
                if cached['run_id'] != run_id or cached['result']['image_sha256'] != row['image_sha256']:
                    raise ValueError('Cached prediction belongs to a different experiment')
                result = cached['result']
            else:
                segmenter = segmenter or Sam3TextSegmenter(args.models_dir)
                result = evaluate_image(layer, row, args.kind, prompts, segmenter, args.root)
                atomic_json(path, {'run_id': run_id, 'result': result})
            results.append(result)
            print(json.dumps({'progress': index+1, 'images': len(layers), 'scene': row['scene_id']}), flush=True)
        report = {'run_id': run_id, 'config': config, **compare(results, prompts)}
        atomic_json(out/'report.json', report)
        print(json.dumps({'report': str(out/'report.json'), **report}, indent=2), flush=True)


if __name__ == '__main__':
    main()
