"""Bounded, resumable self-review batches. Writes experiment artifacts only.

No scene model, confidence index, saved edit or training target is replaced.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.errors import GEOSException

from .scene_batch import signature
from .scene_confidence import geographic
from .scene_split import group_split, image_split_audit
from .scene_training import atomic_json
from .tiles import lonlat_to_tile_xy

DIRECTORY = Path('data/roof-review-loop')
PROTECTED = {'house-30286', 'house-30296'}
TERMINAL = {'candidate_ready', 'no_safe_gain', 'needs_evidence', 'skipped', 'failed'}
# 'frame': the whole pixel frame must audit as train (default, unchanged).
# 'core': opt-in; the object's own centroid z17 tile must be train and the object owned by the tile core.
SPLIT_POLICIES = ('frame', 'core')
FRAME_REASON = 'Held-out, mixed-split or invalid image frame'


def fingerprint(root):
    files = sorted((root/'scripts/ml').glob('*.py'))
    files.extend(sorted((root/DIRECTORY/'equipment').glob('*.json')))
    files.extend(sorted((root/DIRECTORY/'lot-segmentation').glob('*.json')))
    model = root/'scripts/ml/models/roof-form.json'
    if model.exists():
        files.append(model)
    return signature({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


def snapshot_equipment(root, directory):
    for folder in ('equipment', 'lot-segmentation'):
        for source in (root/DIRECTORY/folder).glob('*.json'):
            target = directory/'code'/folder/source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())


def run_path(root, run):
    if not re.fullmatch('[a-f0-9]{16}', run or ''):
        raise ValueError('Invalid self-review run')
    return root/DIRECTORY/'runs'/run


def object_core_group(obj, scene):
    """z17 tile key of the object's centroid, in the same lon/lat frame as the confidence index."""
    point = geographic(obj, scene).centroid
    x, y = lonlat_to_tile_xy(point.x, point.y, 17)
    return f'z17:{math.floor(x)}:{math.floor(y)}'


def split_reason(scene, obj, split_policy):
    """Why the split step protects this object, or None. Only this step differs between policies."""
    owned = scene.get('owned_object_ids')
    audit = image_split_audit(scene, 'train')['status']
    if split_policy == 'core' and isinstance(owned, list):
        if audit == 'invalid_frame':
            return 'Invalid image registration'  # Frame validity is not a split question; both policies keep it.
        if obj.get('id') not in owned:
            return 'Not owned by this tile core'
        try:
            if group_split(object_core_group(obj, scene)) != 'train':
                return 'Held-out core tile'
        except (KeyError, TypeError, ValueError, OverflowError, GEOSException):
            return 'Invalid image registration'
        return None
    if audit != 'verified':
        if split_policy == 'core':
            return FRAME_REASON + ' (core policy unavailable: scene has no owned_object_ids list)'
        return FRAME_REASON
    return None


def protected_reason(root, scene, obj, *, split_policy='frame'):
    if split_policy not in SPLIT_POLICIES:
        raise ValueError('Choose a split policy: frame or core')
    if scene['id'] in PROTECTED or (root/'data/scenes'/scene['id']/'review.json').exists():
        return 'Saved feedback or protected reference'
    if (obj.get('source') != 'sam3-text-onnx' or obj.get('status') != 'candidate'
            or obj.get('geometry_reviewed') or obj.get('roof_line_edits')):
        return 'Not an untouched detector candidate'
    reason = split_reason(scene, obj, split_policy)
    if reason:
        return reason
    try:
        forward, inverse = np.asarray(scene['pixel_to_meters']), np.asarray(scene['meters_to_pixel'])
        if forward.shape != (3, 2) or inverse.shape != (3, 2) or not np.isfinite(inverse).all():
            return 'Invalid image registration'
        probes = np.array([[0, 0], [scene['image_width'], 0], [0, scene['image_height']]])
        metres = np.c_[probes, np.ones(3)] @ forward
        if not np.max(np.abs(np.c_[metres, np.ones(3)] @ inverse-probes)) < .01:
            return 'Invalid image registration'
    except (KeyError, TypeError, ValueError):
        return 'Invalid image registration'
    return None


def compact(report):
    result = {k: v for k, v in report.items() if k != 'rows'}
    result['rows'] = [{k: v for k, v in row.items() if k not in ('attempts', 'before', 'after', 'rejected_preview', 'family_review', 'unit_review', 'imagery_review', 'property_review')} for row in report['rows']]
    result['counts'] = dict(Counter(r['status'] for r in report['rows']))
    result['completed'] = sum(r['status'] in TERMINAL for r in report['rows'])
    return result


def status(root, run=None):
    from .roof_review_feedback import enrich
    base = root/DIRECTORY
    active = base/'active.json'
    if run is None and not active.exists():
        return {'run': None, 'rows': [], 'counts': {}, 'status': 'idle'}
    run = run or json.loads(active.read_text())['run']
    directory = run_path(root, run)
    report = json.loads((directory/'report.json').read_text())
    if report['status'] in ('running', 'pending'):
        with (directory/'worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if time.time()-report['updated_at'] > 10:
                    report['status'] = 'interrupted'
            except BlockingIOError:
                pass
    result = compact(report)
    result['rows'] = enrich(root, directory, result['rows'])
    return result


def prepare(root, limit=12, split_policy='frame'):
    from .scene_processing import inventory
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 24:
        raise ValueError('Use a batch of 1–24 roofs')
    if split_policy not in SPLIT_POLICIES:
        raise ValueError('Choose a split policy: frame or core')
    code = fingerprint(root)
    done = set()
    for path in (root/DIRECTORY/'runs').glob('*/report.json'):
        previous = json.loads(path.read_text())
        if previous.get('fingerprint') == code:
            done.update(r['input_key'] for r in previous['rows'] if r['status'] in TERMINAL)
    props = [f['properties'] for f in inventory(root)['features'] if f['properties']['band'] == 'low']
    # Human-sized roofs with topology defects first, but do not hide small roofs.
    props.sort(key=lambda p: (not p['scene_id'].startswith('mooroolbark-'), not 40 <= p['area_m2'] <= 700,
                              not any('folds' in r or 'vertices disagree' in r for r in p['reasons']),
                              hashlib.sha256(p['key'].encode()).hexdigest()))
    rows, scenes, decoded, skipped, used = [], {}, {}, Counter(), set()
    for p in props:
        sid = p['scene_id']
        key = signature([sid, p['object_id'], p['model_signature'], code])
        if key in done:
            skipped['Already checked with this model and reviewer'] += 1
            continue
        if sid in used:
            continue  # Diversity: one roof per capture per batch.
        if sid not in scenes:
            scenes[sid] = (root/'data/scenes'/sid/'latest.json').read_bytes()
            decoded[sid] = json.loads(scenes[sid])
        scene = decoded[sid]
        obj = next((o for o in scene['objects'] if o['id'] == p['object_id']), None)
        reason = protected_reason(root, scene, obj, split_policy=split_policy) if obj else 'Object changed'
        if scene['model_signature'] != p['model_signature']:
            reason = 'Model changed'
        if reason:
            skipped[reason] += 1
            continue
        rows.append({'scene_id': sid, 'object_id': obj['id'], 'input_key': key, 'source_model': p['model_signature'],
                     'input_sha256': hashlib.sha256(scenes[sid]).hexdigest(),
                     'image_sha256': scene['image_sha256'], 'label': p['label'], 'capture_title': p['capture_title'],
                     'review_url': p['review_url'], 'source_band': p['band'], 'status': 'pending'})
        used.add(sid)
        if len(rows) >= limit:
            break
    # Default batches keep their historical run identity; only core batches add the policy.
    core = {'split_policy': 'core'} if split_policy == 'core' else {}
    run = signature({'code': code, 'inputs': [r['input_key'] for r in rows], 'limit': limit, **core})[:16]
    directory = run_path(root, run)
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory/'report.json').exists():
        code_directory = directory/'code'
        code_directory.mkdir(exist_ok=True)
        for source in (root/'scripts/ml').glob('*.py'):
            (code_directory/source.name).write_bytes(source.read_bytes())
        form_model = root/'scripts/ml/models/roof-form.json'
        if form_model.exists():
            (code_directory/'roof-form.json').write_bytes(form_model.read_bytes())
        snapshot_equipment(root, directory)
        for i, row in enumerate(rows):
            (directory/f'input-{i}.json').write_bytes(scenes[row['scene_id']])
        scope = ('Training core tiles only (object-centroid split policy). Candidate hypotheses, no automatic model replacement or labels.'
                 if core else 'Training geography only. Candidate hypotheses, no automatic model replacement or labels.')
        report = {'run': run, 'fingerprint': code, 'status': 'pending' if rows else 'complete',
                  'created_at': time.time(), 'updated_at': time.time(), 'total': len(rows), 'rows': rows,
                  'selection_skips': dict(skipped), 'scope': scope, **core}
        atomic_json(directory/'report.json', report)
    atomic_json(root/DIRECTORY/'active.json', {'run': run})
    return run


def check_input(root, directory, index, row, split_policy='frame'):
    source = (directory/f'input-{index}.json').read_bytes()
    if hashlib.sha256(source).hexdigest() != row['input_sha256']:
        raise ValueError('Frozen input checksum changed')
    scene = json.loads(source)
    if scene['model_signature'] != row['source_model'] or scene['image_sha256'] != row['image_sha256']:
        raise ValueError('Frozen input identity changed')
    folder = root/'data/scenes'/row['scene_id']
    current = json.loads((folder/'latest.json').read_text())
    if current['model_signature'] != row['source_model']:
        raise ValueError('Source model changed; candidate is stale')
    if current != scene:
        raise ValueError('Source contents changed without a new model identity')
    obj = next(o for o in scene['objects'] if o['id'] == row['object_id'])
    reason = protected_reason(root, current, obj, split_policy=split_policy)
    if reason:
        raise ValueError(reason)
    payload = (folder/f'{scene["image_sha256"]}.png').read_bytes()
    if hashlib.sha256(payload).hexdigest() != row['image_sha256']:
        raise ValueError('Source image checksum changed')
    image = Image.open(io.BytesIO(payload)).convert('RGB')
    if image.size != (scene['image_width'], scene['image_height']):
        raise ValueError('Source image dimensions changed')
    return scene, obj, image


def prepare_comparison(root, reference):
    """Freeze the same eligible inputs to compare a changed reviewer fairly."""
    folder = run_path(root, reference)
    prior = json.loads((folder/'report.json').read_text())
    if prior['status'] != 'complete' or not 1 <= len(prior['rows']) <= 24:
        raise ValueError('Compare a completed bounded batch')
    code, rows = fingerprint(root), []
    if code == prior['fingerprint']:
        raise ValueError('Review code is unchanged; use the existing batch')
    policy = prior.get('split_policy', 'frame')  # A comparison inherits the batch it re-checks.
    for i, old in enumerate(prior['rows']):
        check_input(root, folder, i, old, split_policy=policy)
        row = {k: old[k] for k in ('scene_id', 'object_id', 'source_model', 'input_sha256', 'image_sha256',
                                  'label', 'capture_title', 'review_url', 'source_band')}
        row.update(input_key=signature([row['scene_id'], row['object_id'], row['source_model'], code]), status='pending')
        rows.append(row)
    run = signature({'code': code, 'comparison': reference, 'inputs': [r['input_key'] for r in rows]})[:16]
    target = run_path(root, run)
    target.mkdir(parents=True, exist_ok=True)
    if not (target/'report.json').exists():
        snapshot = target/'code'
        snapshot.mkdir(exist_ok=True)
        for path in (root/'scripts/ml').glob('*.py'):
            (snapshot/path.name).write_bytes(path.read_bytes())
        form = root/'scripts/ml/models/roof-form.json'
        if form.exists():
            (snapshot/form.name).write_bytes(form.read_bytes())
        snapshot_equipment(root, target)
        for i in range(len(rows)):
            (target/f'input-{i}.json').write_bytes((folder/f'input-{i}.json').read_bytes())
            parcels = folder/f'parcels-{i}.json'
            if parcels.exists():
                (target/parcels.name).write_bytes(parcels.read_bytes())
        if (folder/'reflections.json').exists():
            (target/'reflections.json').write_bytes((folder/'reflections.json').read_bytes())
        atomic_json(target/'report.json', {'run': run, 'comparison_run': reference, 'fingerprint': code,
                    'status': 'pending', 'created_at': time.time(), 'updated_at': time.time(),
                    'total': len(rows), 'rows': rows, 'scope': 'Paired input comparison; no model or review publication.',
                    **({'split_policy': policy} if policy == 'core' else {})})
    return run


def review_one(root, directory, index):
    from .roof_block_context import lot_context, parcel_snapshot
    from .roof_candidate_review import improve
    from .roof_family_review import flat_alternative
    from .roof_lot_review import lot_review
    from .roof_review_diagnostics import diagnose, fuller_captures
    from .roof_unit_patterns import cached_equipment
    from .scene_confidence import evidence
    from .scene_property_roofs import inherited_review
    report = json.loads((directory/'report.json').read_text())
    row = report['rows'][index]
    policy = report.get('split_policy', 'frame')
    if fingerprint(root) != report['fingerprint']:
        return {'status': 'skipped', 'reason': 'Review code changed; start a new candidate batch.'}
    try:
        scene, obj, image = check_input(root, directory, index, row, split_policy=policy)
    except ValueError as error:
        return {'status': 'skipped', 'reason': str(error)}
    result, candidate = improve(obj, scene, image, equipment=cached_equipment(root, obj, scene, directory/'code/equipment'))
    parcels = parcel_snapshot(directory, index, scene)
    block = lot_context(scene, obj, parcels['data'])
    block['parcel_snapshot'] = f'parcels-{index}.json'
    block['parcel_sha256'] = hashlib.sha256((directory/f'parcels-{index}.json').read_bytes()).hexdigest()
    if parcels.get('error'):
        block['lookup_error'] = parcels['error']
    for member in block['members']:
        neighbour = next(o for o in scene['objects'] if o['id'] == member['object_id'])
        if protected_reason(root, scene, neighbour, split_policy=policy):
            member['protected'] = True
            continue
        family = (result['family_review'] if neighbour['id'] == obj['id'] else flat_alternative(neighbour, scene, image))
        member['flat_hypothesis'] = next((a['object'] for a in family['alternatives'] if a['geometry_passed']), None)
        member['boundary_review'] = family.get('boundary_review')
    block['lot_review'] = inherited_review(scene, block) or lot_review(root, scene, block, image, directory/'code/lot-segmentation')
    result['property_review'] = block
    result['triage'] = diagnose(obj, scene, image, result['before'])
    if result['triage']['primary'] == 'capture_context':
        result['triage']['fuller_captures'] = fuller_captures(root, scene, obj)
    if fingerprint(root) != report['fingerprint']:
        return {'status': 'skipped', 'reason': 'Review code changed during candidate search.'}
    try:
        check_input(root, directory, index, row, split_policy=policy)
    except ValueError as error:
        return {'status': 'skipped', 'reason': str(error)}
    if candidate:
        result['candidate_band'] = evidence(candidate, result['after'])['band']
        result['baseline_band'] = evidence(obj, result['before'])['band']
    atomic_json(directory/f'object-{index}.json', {'result': result, 'candidate': candidate})
    return result


def work(root, run):
    directory = run_path(root, run)
    with (directory/'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = json.loads((directory/'report.json').read_text())
        if report['fingerprint'] != fingerprint(root):
            report.update(status='failed', error='Review code changed; start a new candidate batch.', updated_at=time.time())
            atomic_json(directory/'report.json', report)
            return
        report.update(status='running', pid=os.getpid(), updated_at=time.time())
        atomic_json(directory/'report.json', report)
        for index, row in enumerate(report['rows']):
            if row['status'] in TERMINAL:
                continue
            if report['fingerprint'] != fingerprint(root):
                report.update(status='failed', error='Review code changed; start a new candidate batch.', updated_at=time.time())
                atomic_json(directory/'report.json', report)
                return
            row.update(status='running', started_at=time.time())
            report['updated_at'] = time.time()
            atomic_json(directory/'report.json', report)
            try:
                result = subprocess.run([sys.executable, '-m', 'scripts.ml.roof_review_loop', '--run', run, '--object', str(index),
                                         '--root', str(root)], cwd=root, capture_output=True, text=True, timeout=120, check=True)
                row.update(json.loads(result.stdout))
            except (subprocess.SubprocessError, ValueError) as error:
                row.update(status='failed', reason='Candidate search failed or exceeded the 120-second roof budget.',
                           error=str(error)[:1000], error_detail=str(getattr(error, 'stderr', '') or '')[-2000:])
            row['elapsed_seconds'] = round(time.time()-row['started_at'], 2)
            report['updated_at'] = time.time()
            atomic_json(directory/'report.json', report)
        report.update(status='complete', updated_at=time.time())
        atomic_json(directory/'report.json', report)


def start(root, limit=12, resume=None, compare=None, split_policy='frame'):
    base = root/DIRECTORY
    base.mkdir(parents=True, exist_ok=True)
    with (base/'launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = status(root)
        if current['status'] in ('running', 'pending'):
            return current
        run = resume or (prepare_comparison(root, compare) if compare else prepare(root, limit, split_policy))
        directory = run_path(root, run)
        atomic_json(base/'active.json', {'run': run})
        if status(root, run)['status'] == 'complete':
            return status(root, run)
        with (directory/'worker.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-u', '-m', 'scripts.ml.roof_review_loop', '--run', run,
                                        '--root', str(root)], cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                       env={**os.environ, 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'})
        (directory/'worker.pid').write_text(str(process.pid))
        return status(root, run)


def detail(root, run, index):
    from .roof_review_feedback import enrich
    directory = run_path(root, run)
    report = json.loads((directory/'report.json').read_text())
    if not 0 <= index < len(report['rows']):
        raise ValueError('Unknown roof')
    row = enrich(root, directory, report['rows'])[index]
    scene = json.loads((directory/f'input-{index}.json').read_text())
    ids = {row['object_id'], *(m['object_id'] for m in row.get('property_review', {}).get('members', []))}
    scene['objects'] = sorted([o for o in scene['objects'] if o['id'] in ids], key=lambda o: o['id'] != row['object_id'])
    scene['groups'] = []
    result = directory/f'object-{index}.json'
    saved = json.loads(result.read_text()) if result.exists() else {}
    current = json.loads((root/'data/scenes'/row['scene_id']/'latest.json').read_text())
    return {'row': row, 'scene': scene, 'candidate': saved.get('candidate'),
            'stale': current != json.loads((directory/f'input-{index}.json').read_text()),
            'image_url': f'/api/scenes/image?id={scene["id"]}&version={scene["image_sha256"]}'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--run')
    parser.add_argument('--resume')
    parser.add_argument('--compare', help='Recheck the same completed batch with changed review code')
    parser.add_argument('--split-policy', choices=SPLIT_POLICIES, default='frame',
                        help="New batches only: 'frame' audits the whole image frame (default); "
                             "'core' admits owned roofs whose own centroid tile is train. Resume/compare inherit.")
    parser.add_argument('--object', type=int)
    parser.add_argument('--detail', type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.start:
        if args.resume and args.compare:
            parser.error('Choose resume or compare')
        if args.split_policy != 'frame' and (args.resume or args.compare):
            parser.error('--split-policy applies to new batches only; resume and compare inherit the stored policy')
        value = start(root, args.limit, args.resume, args.compare, args.split_policy)
    elif args.detail is not None:
        value = detail(root, args.run, args.detail)
    elif args.object is not None:
        value = review_one(root, run_path(root, args.run), args.object)
    elif args.run:
        work(root, args.run)
        value = status(root, args.run)
    else:
        value = status(root)
    print(json.dumps(value))


if __name__ == '__main__':
    main()
