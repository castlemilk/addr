"""Durable, isolated multi-date inference and comparison artifacts for saved scenes."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .scene_batch import signature
from .scene_training import atomic_json

DIRECTORY = Path('data/roof-review-loop/multidate')


def run_path(root, run):
    if not re.fullmatch(r'[a-f0-9]{16}', run or ''):
        raise ValueError('Invalid imagery comparison')
    return root/DIRECTORY/'runs'/run


def status(root, run):
    folder = run_path(root, run)
    report = json.loads((folder/'report.json').read_text())
    if report['status'] == 'running' and time.time()-report['updated_at'] > 15:
        with (folder/'worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                report = {**report, 'status': 'interrupted', 'stage': 'Worker stopped; restart this comparison to resume.'}
            except BlockingIOError:
                pass
    original = root/'data/scenes'/report['scene_id']/'latest.json'
    report['stale'] = hashlib.sha256(original.read_bytes()).hexdigest() != report['source_sha256']
    return report


def start(root, scene_id):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', scene_id or ''):
        raise ValueError('Choose a valid scene')
    source = root/'data/scenes'/scene_id
    raw = (source/'latest.json').read_bytes()
    model = json.loads(raw)
    if not model.get('region', {}).get('bounds') or not 64 <= max(model['image_width'], model['image_height']) <= 1536:
        raise ValueError('Choose a registered map capture of at most 1536 pixels.')
    source_image = source/f'{model["image_sha256"]}.png'
    if hashlib.sha256(source_image.read_bytes()).hexdigest() != model['image_sha256']:
        raise ValueError('Source image identity changed')
    code = signature({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'scripts/ml').glob('*.py')})
    run = signature([scene_id, hashlib.sha256(raw).hexdigest(), code, datetime.now(UTC).date().isoformat()])[:16]
    folder = run_path(root, run)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (folder/'report.json').exists():
            existing = status(root, run)
            if existing['status'] in ('running', 'complete', 'needs_evidence'):
                return existing
        (folder/'input.json').write_bytes(raw)
        (folder/'saved-source.png').write_bytes(source_image.read_bytes())
        report = {'run': run, 'scene_id': scene_id, 'title': model['title'], 'source_sha256': hashlib.sha256(raw).hexdigest(),
                  'source_image_sha256': model['image_sha256'], 'code_sha256': code, 'status': 'running',
                  'stage': 'Discovering independent local acquisitions', 'updated_at': time.time(), 'captures': [], 'rows': [],
                  'reference_date': model.get('region', {}).get('acquisition_date'), 'published': False,
                  'image_width': model['image_width'], 'image_height': model['image_height']}
        atomic_json(folder/'report.json', report)
        with (folder/'worker.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-u', '-m', 'scripts.ml.scene_temporal', '--run', run,
                                        '--worker', '--root', str(root)], cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, stdin=subprocess.DEVNULL)
        (folder/'worker.pid').write_text(str(process.pid))
        return report


def worker(root, run):
    from .imagery_dates import capture, discover
    from .sam3 import Sam3TextSegmenter
    from .scene_extract import extract_objects, polygon_details
    from .scene_house import add_roof_estimates, pipeline_context_hints
    from .temporal_evidence import reflect_objects, register, stable_composite

    folder = run_path(root, run)
    with (folder/'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        report = json.loads((folder/'report.json').read_text())
        def save(stage, **values):
            report.update(stage=stage, updated_at=time.time(), **values)
            atomic_json(folder/'report.json', report)
            print(stage, flush=True)
        try:
            scene = json.loads((folder/'input.json').read_text())
            reference = None
            cache = root/DIRECTORY/'cache'
            captures, skipped = discover(scene, cache)
            save('Registering dated images to the saved scene', skipped=skipped)
            accepted, records, seen = [], [], set()
            for i, record in enumerate(captures):
                try:
                    image = capture(scene, record, cache/'tiles')
                    image.save(folder/f'capture-{i}.png')
                    digest = hashlib.sha256(np.asarray(image).tobytes()).hexdigest()
                    if digest in seen:
                        records.append({**record, 'index': i, 'status': 'duplicate', 'reason': 'Repeated source pixels'})
                        continue
                    seen.add(digest)
                    if reference is None:
                        reference = image
                        reference.save(folder/'reference.png')
                        report['reference_date'] = record['acquisition_date']
                        report['reference_image_sha256'] = hashlib.sha256((folder/'reference.png').read_bytes()).hexdigest()
                        aligned, valid, registration = image, np.ones((image.height, image.width), bool), {'status': 'accepted', 'reason': 'Newest available dated reference', 'transform': [[1, 0, 0], [0, 1, 0]]}
                    else:
                        aligned, valid, registration = register(reference, image)
                    record = {**record, 'index': i, 'pixel_sha256': digest, 'registration': registration, 'status': registration['status']}
                    records.append(record)
                    if aligned is not None:
                        aligned.save(folder/f'aligned-{i}.png')
                        Image.fromarray(valid.astype(np.uint8)*255).save(folder/f'valid-{i}.png')
                        accepted.append({**record, 'image': aligned, 'valid': valid})
                except (ValueError, OSError) as error:
                    records.append({**record, 'index': i, 'status': 'failed', 'reason': str(error)[:300]})
                save(f'Checked {i+1} of {len(captures)} dated captures', captures=records)
            if not accepted:
                save('No alternate image passed registration; original retained', status='needs_evidence')
                return
            fused, used, fusion = stable_composite(reference, [c['image'] for c in accepted[1:]], [c['valid'] for c in accepted[1:]])
            fused.save(folder/'combined.png')
            Image.fromarray(used.astype(np.uint8)*255).save(folder/'combined-mask.png')
            save('Waiting for the shared inference worker', fusion=fusion)
            # Serialize heavyweight SAM loads with the existing scene pipeline.
            with (root/'data/scenes/.extraction.lock').open('a') as extraction:
                fcntl.flock(extraction, fcntl.LOCK_EX)
                save('Loading detector for independent date estimates')
                detector = Sam3TextSegmenter(root/'scripts/ml/models/sam3')
                report['detector_identity'] = detector.model_identity
                for j, c in enumerate(accepted):
                    save(f'Estimating roofs, trees and solar arrays: {c["acquisition_date"]} ({j+1}/{len(accepted)})')
                    objects, scans = extract_objects(c['image'], c['pixel_sha256'], np.asarray(scene['pixel_to_meters']),
                                                     detector, kinds=['building', 'tree', 'solar_panel'])
                    c['objects'] = objects
                    atomic_json(folder/f'objects-{c["index"]}.json', {'objects': objects, 'scans': scans})
                    for record in records:
                        if record['index'] == c['index']:
                            record['counts'] = dict(Counter(o['kind'] for o in objects))
                del detector
            mpp = float(np.sqrt(abs(np.linalg.det(np.asarray(scene['pixel_to_meters'])[:2]))))
            reference_scene = {**copy.deepcopy(scene), 'objects': accepted[0]['objects'], 'groups': [],
                               'image_sha256': report['reference_image_sha256'],
                               'analysis': {'pipeline': 'dated-source-detector', 'detector_identity': report['detector_identity']},
                               'region': {**scene['region'], 'provider': 'esri', 'acquisition_date': report['reference_date']}}
            for key in ('owned_object_ids', 'suburb_run', 'analysis_core', 'property_roofs'):
                reference_scene.pop(key, None)
            reference_scene['model_signature'] = signature(reference_scene)
            atomic_json(folder/'reference.json', reference_scene)
            rows, candidates = reflect_objects(reference_scene['objects'], accepted, reference.size, mpp)
            # Refit a bounded set through the same physical roof modelling path.
            # Remaining proposals stay queued, not falsely counted as fitted.
            candidate_scene = {**copy.deepcopy(reference_scene), 'objects': candidates[:6], 'groups': [], 'scans': {},
                               'image_sha256': hashlib.sha256((folder/'combined.png').read_bytes()).hexdigest()}
            for obj in candidate_scene['objects']:
                obj['model'] = polygon_details([tuple(p) for p in obj['ring']], fused, np.asarray(scene['pixel_to_meters']), 'building', obj.get('holes'))
            save('Fitting roof surfaces from the combined stable image', rows=rows, captures=records)
            add_roof_estimates(candidate_scene, fused, context_hints=pipeline_context_hints(candidate_scene, fused, root))
            candidate_scene['model_signature'] = signature(candidate_scene)
            atomic_json(folder/'candidate.json', candidate_scene)
            fitted = {o['id'] for o in candidate_scene['objects'] if o.get('roof_annotation')}
            for row in rows:
                row['surface_fitted'] = row['object_id'] in fitted
            save('Comparison ready', status='complete', rows=rows, captures=records,
                 summary={'objects_compared': len(rows), 'consistent': sum(r['status'] == 'consistent' for r in rows),
                          'needs_review': sum(r['status'] == 'needs_review' for r in rows),
                          'roof_boundary_candidates': len(candidates), 'roof_surfaces_fitted': len(fitted),
                          'roof_surfaces_queued': max(0, len(candidates)-6)},
                 note='Cross-date agreement is evidence, not calibrated accuracy. Tree outlines remain separate by date. Saved models and feedback are unchanged.')
        except Exception as error:
            save(str(error)[:500], status='failed')
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--start')
    parser.add_argument('--run')
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.start:
        print(json.dumps(start(root, args.start)))
    elif args.worker:
        worker(root, args.run)
    else:
        print(json.dumps(status(root, args.run)))


if __name__ == '__main__':
    main()
