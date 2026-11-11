"""Prepare a resumable scene review batch without replacing existing predictions or reviews.

Only whole-image frames within their declared train/validation split are selected.
Every extracted object remains an unreviewed proposal; no labels or weights change.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

import numpy as np

from .sam3 import Sam3TextSegmenter
from .scene_experiment import digest, model_fingerprint
from .scene_extract import (
    PROMPTS,
    cohort_scene_source,
    cohort_sources,
    extract_objects,
    group_objects,
    save_scene,
)
from .scene_split import image_split_audit
from .scene_training import atomic_json


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def plan_batch(cohort, kinds, limit, root=Path('.')):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,70}', cohort):
        raise ValueError('Choose a valid cohort name')
    if not kinds or len(kinds) != len(set(kinds)) or any(k not in PROMPTS for k in kinds):
        raise ValueError('Choose distinct supported object classes')
    if not 1 <= limit <= 300:
        raise ValueError('Choose a batch size from 1 to 300 images')
    cohort_path = root/'data/cohorts'/f'{cohort}.json'
    entries = json.loads(cohort_path.read_text())['entries']
    sources = cohort_sources(cohort, root)
    queues, excluded = defaultdict(deque), []
    seen = set()
    for entry in entries:
        aid = entry['address_id']
        if not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0 or aid in seen:
            raise ValueError('Cohort contains invalid or duplicate address identities')
        seen.add(aid)
        scene_id = f'{cohort}-{aid}'
        folder = root/'data/scenes'/scene_id
        reason = ('existing' if (folder/'latest.json').exists() or (folder/'review.json').exists() else
                  'held_out_test' if entry.get('split') == 'test' else
                  'unassigned_split' if entry.get('split') not in ('train', 'validation', 'val') else None)
        if reason:
            excluded.append({'scene_id': scene_id, 'reason': reason})
            continue
        try:
            scene, _, image_bytes = cohort_scene_source(aid, cohort, root=root, sources=sources)
            audit = image_split_audit(scene, entry['split'])
            if audit['status'] != 'verified':
                excluded.append({'scene_id': scene_id, 'reason': audit['status']})
                continue
            if entry.get('spatial_group') not in {group['group'] for group in audit['groups']}:
                raise ValueError('Declared area group is outside the source image')
            if hashlib.sha256(image_bytes).hexdigest() != scene['image_sha256']:
                raise ValueError('Source image changed while preparing its frame')
            item = {'scene_id': scene_id, 'address_id': aid, 'split': 'validation' if entry['split'] == 'val' else entry['split'],
                    'spatial_group': entry['spatial_group'], 'image_sha256': scene['image_sha256'],
                    'source_signature': signature(scene), 'image_geography': audit}
            queues[(item['split'], item['spatial_group'])].append(item)
        except (KeyError, OSError, ValueError) as error:
            excluded.append({'scene_id': scene_id, 'reason': 'source_error', 'detail': str(error)})
    jobs = []
    # Spread the first review pages across areas instead of exhausting one street.
    keys = sorted(queues, key=lambda key: (key[0] != 'validation', key[1]))
    while any(queues.values()):
        for key in keys:
            if queues[key]:
                item = queues[key].popleft()
                if len(jobs) < limit:
                    jobs.append(item)
                else:
                    excluded.append({'scene_id': item['scene_id'], 'reason': 'batch_limit'})
    return {'schema_version': 1, 'cohort': cohort, 'cohort_sha256': digest(cohort_path),
            'classes': kinds, 'prompts': {kind: PROMPTS[kind] for kind in kinds},
            'jobs': jobs, 'excluded': excluded}


def run_batch(plan, directory, root=Path('.'), segmenter_factory=None):
    state_path = directory/'run.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'results': {}}
    state['status'] = 'running'
    atomic_json(state_path, state)
    sources = cohort_sources(plan['cohort'], root)
    segmenter = None
    detector_failed = False
    for job in plan['jobs']:
        scene_id = job['scene_id']
        folder = root/'data/scenes'/scene_id
        previous = state['results'].get(scene_id)
        if (folder/'latest.json').exists() or (folder/'review.json').exists():
            # Published snapshots and reviewer work always win over a batch resume.
            if not previous or previous['status'] != 'complete':
                state['results'][scene_id] = {'status': 'preserved_existing'}
                atomic_json(state_path, state)
            continue
        started = time.monotonic()
        print(json.dumps({'scene': scene_id, 'status': 'extracting', 'completed': sum(v['status'] == 'complete' for v in state['results'].values()), 'total': len(plan['jobs'])}), flush=True)
        try:
            scene, image, image_bytes = cohort_scene_source(job['address_id'], plan['cohort'], root=root, sources=sources)
            if signature(scene) != job['source_signature'] or hashlib.sha256(image_bytes).hexdigest() != job['image_sha256']:
                raise ValueError('The frozen source changed; create a new batch plan')
            if image_split_audit(scene, job['split']) != job['image_geography']:
                raise ValueError('Image split geography changed')
            if segmenter is None:
                try:
                    segmenter = segmenter_factory() if segmenter_factory else Sam3TextSegmenter(root/'scripts/ml/models/sam3')
                except Exception:
                    detector_failed = True
                    raise
            scene['objects'], scene['scans'] = extract_objects(image, scene['image_sha256'], np.array(scene['pixel_to_meters']),
                segmenter, objects=scene['objects'], kinds=plan['classes'])
            if len(scene['objects']) > 1000:
                raise ValueError('More than 1,000 proposals; use a smaller image capture')
            scene['groups'] = group_objects(scene['objects'])
            saved = save_scene(scene, image_bytes, root)
            state['results'][scene_id] = {'status': 'complete', 'model_signature': saved['model_signature'],
                'image_sha256': saved['image_sha256'], 'objects': len(saved['objects']),
                'classes': dict(Counter(o['kind'] for o in saved['objects'])), 'elapsed_seconds': round(time.monotonic()-started, 2)}
        except Exception as error:
            state['results'][scene_id] = {'status': 'failed', 'error': str(error), 'elapsed_seconds': round(time.monotonic()-started, 2)}
        atomic_json(state_path, state)
        print(json.dumps({'scene': scene_id, **state['results'][scene_id]}), flush=True)
        if detector_failed:
            break
    state['counts'] = dict(Counter(result['status'] for result in state['results'].values()))
    state['status'] = 'needs_retry' if state['counts'].get('failed') else 'complete'
    atomic_json(state_path, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cohort', default='roof_training_250')
    parser.add_argument('--classes', default=','.join(PROMPTS))
    parser.add_argument('--limit', type=int, default=250)
    parser.add_argument('--plan', action='store_true', help='Save a frozen plan without running inference')
    parser.add_argument('--resume', help='Resume a previously saved batch ID')
    args = parser.parse_args()
    root = Path('.')
    code = {name: digest(Path(__file__).with_name(name+'.py')) for name in ('scene_batch', 'scene_extract', 'scene_split', 'sam3', 'segmentation')}
    models = model_fingerprint(root/'scripts/ml/models/sam3')
    if args.resume:
        if not re.fullmatch(r'[a-f0-9]{16}', args.resume):
            parser.error('Choose a valid batch ID')
        directory = root/'data/scene-batches'/args.resume
        plan = json.loads((directory/'plan.json').read_text())
        if signature(plan)[:16] != args.resume or plan['code'] != code or plan['models'] != models or plan['prompts'] != {k: PROMPTS[k] for k in plan['classes']}:
            parser.error('The batch inputs or extractor changed. Create a new plan; existing scenes will be preserved.')
        if digest(root/'data/cohorts'/f"{plan['cohort']}.json") != plan['cohort_sha256']:
            parser.error('The cohort changed. Create a new batch plan.')
    else:
        plan = plan_batch(args.cohort, args.classes.split(','), args.limit, root)
        plan.update(code=code, models=models)
        directory = root/'data/scene-batches'/signature(plan)[:16]
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory/'plan.json', plan)
    print(json.dumps({'batch': directory.name, 'selected': len(plan['jobs']), 'splits': dict(Counter(j['split'] for j in plan['jobs'])),
        'excluded': dict(Counter(j['reason'] for j in plan['excluded'])), 'plan': str(directory/'plan.json')}), flush=True)
    if args.plan:
        return
    lock_path = root/'data/scenes/.extraction.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(75, 'Another scene extraction is running. Resume this batch when it finishes.\n')
        result = run_batch(plan, directory, root)
    if result['status'] != 'complete':
        parser.exit(1, 'Some images need retry; inspect run.json and resume the batch.\n')


if __name__ == '__main__':
    main()
