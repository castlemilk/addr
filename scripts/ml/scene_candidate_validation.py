"""Check a frozen proposal filter on additional reviewed validation images.

No fitting, threshold selection or test-set inspection. Results are separate
from the original candidate evaluation and never change annotations.
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

from .sam3 import Sam3TextSegmenter
from .scene_evaluation import evaluate_objects, summarize
from .scene_experiment import digest, evaluate_image, frozen_layers, model_fingerprint
from .scene_proposal_features import case_context
from .scene_split import image_split_audit
from .scene_training import atomic_json, atomic_text
from .train_scene_proposals import FEATURE_SETS, checked_cases, model_feature_set, probabilities


def validation_layers(pairs, used):
    """Previously selected views stay separate; training areas cannot be reused."""
    seen_ids = {c['scene_id'] for c in used}
    seen_images = {c['image_sha256'] for c in used}
    train_groups = {c['spatial_group'] for c in used if c['split'] == 'train'}
    selected = []
    for layer, row in pairs:
        if layer['split'] != 'validation' or row['scene_id'] in seen_ids or row['image_sha256'] in seen_images:
            continue
        if not row['spatial_group'] or row['spatial_group'] in train_groups:
            raise ValueError('Additional validation overlaps a training area')
        with Image.open(row['image_path']) as image:
            geography = image_split_audit({**row, 'image_width': image.width, 'image_height': image.height}, 'validation')
        if geography['status'] != 'verified' or row['spatial_group'] not in {g['group'] for g in geography['groups']}:
            raise ValueError('Additional validation needs verified whole-image geography')
        selected.append((layer, row))
        seen_ids.add(row['scene_id'])
        seen_images.add(row['image_sha256'])
    return selected


def check_image(layer, row, model, segmenter, root):
    kind, prompt = model['provenance']['kind'], model['provenance']['prompt']
    result = evaluate_image(layer, row, kind, [prompt], segmenter, root)
    predictions = result['prompts'][prompt]['predictions']
    context = case_context({**row, 'predictions': predictions}, root) if model_feature_set(model) == 'image-context' else None
    values = probabilities(predictions, model, context)
    kept = [o for o, score in zip(predictions, values, strict=True) if score >= model['threshold']]
    return {'result': result,
            **({'image_context': context} if context is not None else {}),
            'scores': [{'id': o['id'], 'quality_score': round(float(score), 6), 'retained': bool(score >= model['threshold'])}
                       for o, score in zip(predictions, values, strict=True)],
            'filtered': evaluate_objects(kept, [o for o in row['reference'] if o['kind'] == kind])}


def published_check(directory, encoded, scene_ids):
    """Never silently overwrite, or report success for, a damaged frozen check."""
    if not (directory/'report.json').exists():
        return False
    report = json.loads((directory/'report.json').read_text())
    if ((directory/'config.json').read_text() != encoded
            or report['check_id'] != hashlib.sha256(encoded.encode()).hexdigest()[:16]
            or set(report['scene_files']) != set(scene_ids)
            or any(digest(directory/f'{scene_id}.json') != report['scene_files'][scene_id] for scene_id in scene_ids)):
        raise ValueError('Published validation check changed; existing results were preserved')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-f0-9]{16}', args.candidate):
        parser.error('Choose a valid candidate ID')
    root = Path('.')
    directory = root/'data/training/scene-proposals'/args.candidate
    model = json.loads((directory/'model.json').read_text())
    identity = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()[:16]
    if identity != args.candidate or model['feature_names'] not in FEATURE_SETS.values() or model['model_kind'] != 'regularized-logistic-proposal-quality':
        parser.error('Candidate weights or feature definitions changed')
    count = len(model['feature_names'])
    if (len(model['mean']) != count or len(model['scale']) != count or len(model['weights']) != count+1
            or not np.isfinite([*model['mean'], *model['scale'], *model['weights'], model['threshold']]).all()
            or min(model['scale']) <= 0 or not 0 <= model['threshold'] <= 1):
        parser.error('Candidate parameters are invalid')
    p = model['provenance']
    if model_feature_set(model) == 'image-context' and p.get('image_context_code') != digest(Path(__file__).with_name('scene_proposal_features.py')):
        parser.error('Image-context implementation differs from the trained candidate')
    original, _, used = checked_cases(root/'data/training/scenes'/p['dataset_version'], root/'data/experiments/scenes'/p['experiment_run'], p['prompt'])
    if p.get('supplemental_dataset_version'):
        extra = root/'data/training/scenes'/p['supplemental_dataset_version']/'dataset.jsonl'
        if digest(extra)[:16] != p['supplemental_dataset_version']:
            parser.error('Supplemental training dataset changed')
        used += [{**row, 'split': 'train'} for row in json.loads((directory/'supplemental.json').read_text())]
    models = model_fingerprint(root/'scripts/ml/models/sam3')
    if models != original['models']:
        parser.error('Detector components differ from the trained candidate')
    version, pairs = frozen_layers(args.dataset, p['kind'])
    selected = validation_layers(pairs, used)
    if not selected:
        parser.error('No additional fully reviewed validation images are available')
    config = {'schema_version': 1, 'candidate_id': args.candidate, 'candidate_model_sha256': digest(directory/'model.json'),
              'dataset_version': version, 'kind': p['kind'], 'prompt': p['prompt'], 'models': models,
              'layers': [{k: layer[k] for k in ('scene_id', 'split', 'spatial_group', 'revision', 'image_sha256', 'model_signature')}
                         for layer, _ in selected],
              'code': {name: digest(Path(__file__).with_name(name+'.py')) for name in
                       ('scene_candidate_validation', 'scene_experiment', 'scene_extract', 'scene_evaluation', 'scene_split', 'train_scene_proposals', 'sam3', 'segmentation')}}
    if model_feature_set(model) == 'image-context':
        config['code']['scene_proposal_features'] = p['image_context_code']
    encoded = json.dumps(config, sort_keys=True, indent=2)+'\n'
    check_id = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    out = directory/'checks'/check_id
    with (root/'data/scenes/.extraction.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(75, 'Another extraction is running. Retry when it finishes.\n')
        out.mkdir(parents=True, exist_ok=True)
        if published_check(out, encoded, [row['scene_id'] for _, row in selected]):
            print(json.dumps({'check_id': check_id, 'report': str(out/'report.json'), 'status': 'already_complete'}))
            return
        atomic_text(out/'config.json', encoded)
        engine, results = None, []
        for layer, row in selected:
            engine = engine or Sam3TextSegmenter(root/'scripts/ml/models/sam3')
            result = check_image(layer, row, model, engine, root)
            atomic_json(out/f'{row["scene_id"]}.json', {'check_id': check_id, **result})
            results.append(result)
            print(json.dumps({'check': check_id, 'scene': row['scene_id'], 'complete': len(results), 'total': len(selected)}), flush=True)
        before = [{**r['result']['prompts'][p['prompt']]['metrics'], **{k: r['result'][k] for k in ('scene_id', 'split', 'spatial_group')}} for r in results]
        after = [{**r['filtered'], **{k: r['result'][k] for k in ('scene_id', 'split', 'spatial_group')}} for r in results]
        atomic_json(out/'report.json', {'check_id': check_id, 'candidate_id': args.candidate, 'before': summarize(before), 'after': summarize(after),
            'scene_files': {r['result']['scene_id']: digest(out/f'{r["result"]["scene_id"]}.json') for r in results},
            'layers': [{'before': a, 'after': b} for a, b in zip(before, after, strict=True)],
            'scope': 'Additional validation images, absent from fitting and original model selection. Frozen weights and threshold; no test data or annotation changes.'})
    print(json.dumps({'check_id': check_id, 'candidate_id': args.candidate, 'report': str(out/'report.json')}))


if __name__ == '__main__':
    main()
