"""Freeze a roof baseline and analyse new houses without replacing saved scenes.

This is an inference/review batch, not a training export or a weight update.
Captured images, source frames, code and weight hashes are frozen for resume.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from PIL import Image

from .sam3 import Sam3TextSegmenter
from .scene_batch import signature
from .scene_experiment import digest, model_fingerprint
from .scene_extract import PROMPTS, analyse_scene, save_scene
from .scene_house import house_source
from .scene_split import image_split_audit
from .scene_training import atomic_json


def code_files(root):
    return sorted((root/'scripts/ml').glob('*.py'))


def freeze_baseline(scene_id, root=Path('.')):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', scene_id):
        raise ValueError('Invalid baseline scene ID')
    source = root/'data/scenes'/scene_id
    model = json.loads((source/'latest.json').read_text())
    files = code_files(root) + [source/'latest.json', source/f"{model['image_sha256']}.png"]
    if (source/'review.json').exists():
        files.append(source/'review.json')
    # Keep the actual regression inputs and tests with the implementation.
    files += sorted((root/'scripts/ml/tests').rglob('*.py'))
    files += sorted((root/'scripts/ml/tests/fixtures').glob('*.json'))
    roof_form = Path(os.environ.get('ROOF_FORM_MODEL', 'scripts/ml/models/roof-form.json'))
    if not roof_form.is_absolute():
        roof_form = root/roof_form
    weights = model_fingerprint(root/'scripts/ml/models/sam3')
    payloads = {str(path.relative_to(root)): path.read_bytes() for path in files}
    manifest = {'schema_version': 1, 'scene_id': scene_id, 'model_signature': model['model_signature'],
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
                'sam3_weights': weights, 'roof_form_model': digest(roof_form) if roof_form.exists() else None,
                'scope': 'Frozen inference code, reference scene and reviewer observations; no labels approved.'}
    directory = root/'data/references/roof-checkpoints'/signature(manifest)[:16]
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in payloads.items():
        target = directory/'snapshot'/name
        if target.exists() and target.read_bytes() != data:
            raise ValueError('Checkpoint contents changed')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    atomic_json(directory/'manifest.json', manifest)
    return directory


def verify_baseline(directory, root=Path('.'), *, weights=True):
    manifest = json.loads((directory/'manifest.json').read_text())
    if signature(manifest)[:16] != directory.name:
        raise ValueError('Checkpoint manifest changed')
    for name, expected in manifest['files'].items():
        if digest(directory/'snapshot'/name) != expected:
            raise ValueError(f'Checkpoint snapshot changed: {name}')
        if digest(root/name) != expected:
            raise ValueError(f'Baseline or code changed: {name}; create a new batch checkpoint')
    if weights:
        if model_fingerprint(root/'scripts/ml/models/sam3') != manifest['sam3_weights']:
            raise ValueError('SAM3 weights changed')
        roof_form = Path(os.environ.get('ROOF_FORM_MODEL', 'scripts/ml/models/roof-form.json'))
        if not roof_form.is_absolute():
            roof_form = root/roof_form
        if (digest(roof_form) if roof_form.exists() else None) != manifest['roof_form_model']:
            raise ValueError('Roof form model changed')
    return manifest


def prepare_batch(addresses, checkpoint, root=Path('.')):
    if (not 1 <= len(addresses) <= 30 or len(set(addresses)) != len(addresses)
            or any(type(aid) is not int or aid <= 0 for aid in addresses)):
        raise ValueError('Choose 1–30 distinct positive address IDs')
    verify_baseline(checkpoint, root)
    plan = {'schema_version': 1, 'checkpoint': str(checkpoint.relative_to(root)),
            'addresses': addresses, 'classes': list(PROMPTS), 'prompts': PROMPTS,
            'input_geometry': 'imagery-only', 'purpose': 'Unreviewed proposals for human feedback'}
    directory = root/'data/scene-batches'/signature(plan)[:16]
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory/'plan.json', plan)
    return directory


def run_batch(directory, root=Path('.'), *, segmenter_factory=None, source_factory=None):
    plan = json.loads((directory/'plan.json').read_text())
    if signature(plan)[:16] != directory.name or plan['prompts'] != PROMPTS:
        raise ValueError('Batch plan changed')
    checkpoint = root/plan['checkpoint']
    verify_baseline(checkpoint, root)
    state_path = directory/'run.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'results': {}}
    state['status'] = 'running'
    atomic_json(state_path, state)
    segmenter = None
    for aid in plan['addresses']:
        scene_id = f'house-{aid}'
        folder = root/'data/scenes'/scene_id
        if (folder/'latest.json').exists() or (folder/'review.json').exists():
            if state['results'].get(scene_id, {}).get('status') != 'complete':
                state['results'][scene_id] = {'status': 'preserved_existing'}
            continue
        started = time.monotonic()
        print(json.dumps({'scene': scene_id, 'status': 'analysing'}), flush=True)
        try:
            job = directory/scene_id
            job.mkdir(exist_ok=True)
            source_path, image_path = job/'source.json', job/'image.png'
            if source_path.exists():
                source = json.loads(source_path.read_text())
                image_bytes = image_path.read_bytes()
            else:
                source, _, image_bytes = (source_factory or house_source)(aid, root=root)
                image_path.write_bytes(image_bytes)
                atomic_json(source_path, source)
            if source['id'] != scene_id or source['image_sha256'] != hashlib.sha256(image_bytes).hexdigest():
                raise ValueError('Frozen capture identity or image changed')
            frozen = job/'source.sha256'
            if frozen.exists() and frozen.read_text() != signature(source):
                raise ValueError('Frozen source frame changed')
            frozen.write_text(signature(source))
            if segmenter is None:
                segmenter = segmenter_factory() if segmenter_factory else Sam3TextSegmenter(root/'scripts/ml/models/sam3')
            with Image.open(image_path) as image:
                prediction = analyse_scene(source, image.convert('RGB'), segmenter, kinds=plan['classes'],
                                           detector_only=True, roof_models=True, root=root)
            atomic_json(job/'prediction.json', prediction)
            # Never allow this batch to manufacture reviewed labels.
            if any(obj.get('status') != 'candidate' or obj.get('geometry_reviewed') for obj in prediction['objects']):
                raise ValueError('Inference returned reviewed geometry')
            verify_baseline(checkpoint, root, weights=False)
            if (folder/'latest.json').exists() or (folder/'review.json').exists():
                state['results'][scene_id] = {'status': 'preserved_existing'}
            else:
                saved = save_scene(prediction, image_bytes, root)
                state['results'][scene_id] = {'status': 'complete', 'title': saved['title'],
                    'model_signature': saved['model_signature'], 'image_sha256': saved['image_sha256'],
                    'classes': dict(Counter(obj['kind'] for obj in saved['objects'])),
                    'analysis': saved['analysis'], 'image_geography': image_split_audit(saved, saved['region']['split']),
                    'review_url': f'/scenes?scene={scene_id}&kind=building&status=all&layer=model&address={aid}'}
        except Exception as error:
            state['results'][scene_id] = {'status': 'failed', 'error': str(error)}
        state['results'][scene_id]['elapsed_seconds'] = round(time.monotonic()-started, 2)
        atomic_json(state_path, state)
        print(json.dumps({'scene': scene_id, **state['results'][scene_id]}), flush=True)
    state['counts'] = dict(Counter(row['status'] for row in state['results'].values()))
    state['status'] = 'needs_retry' if state['counts'].get('failed') else 'complete'
    atomic_json(state_path, state)
    verify_baseline(checkpoint, root)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--addresses', type=int, nargs='+')
    parser.add_argument('--baseline', default='house-30286')
    parser.add_argument('--resume', help='Previously frozen 16-character batch ID')
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--self-review', action='store_true', help='Independently audit the completed captures')
    parser.add_argument('--repair', action='store_true', help='With --self-review, try bounded repairs on unreviewed training-area roofs')
    args = parser.parse_args()
    if args.repair and not args.self_review:
        parser.error('--repair requires --self-review')
    root = Path('.')
    if args.resume:
        if not re.fullmatch(r'[a-f0-9]{16}', args.resume) or args.addresses:
            parser.error('Resume one batch ID without new addresses')
        directory = root/'data/scene-batches'/args.resume
    else:
        if not args.addresses:
            parser.error('Choose addresses to analyse')
        directory = prepare_batch(args.addresses, freeze_baseline(args.baseline, root), root)
    print(json.dumps({'batch': directory.name, 'plan': str(directory/'plan.json')}), flush=True)
    if args.plan:
        return
    with (root/'data/scenes/.extraction.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(75, 'Another extraction is running. Resume this batch later.\n')
        result = run_batch(directory, root)
        if args.self_review:
            from .scene_self_review import run_review
            complete = [sid for sid, row in result['results'].items() if row['status'] == 'complete']
            if complete:
                report, review_directory = run_review(complete, root, repair=args.repair)
                print(json.dumps({'self_review': str(review_directory/'REVIEW.md'), **report['counts']}), flush=True)
    if result['status'] != 'complete':
        parser.exit(1, 'Some houses need retry. Inspect run.json before resuming.\n')


if __name__ == '__main__':
    main()
