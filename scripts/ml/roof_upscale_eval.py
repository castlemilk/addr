"""Six-roof paired SAM3 experiment: native ROI, Lanczos4x and Real-ESRGAN4x.

Only detector input changes. All masks decode onto the identical native ROI
grid, then ordinary extraction/refinement and roof fitting use saved original
RGB. Artifacts are experiments, never scene saves, labels or an accuracy claim.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.affinity import translate
from shapely.geometry import Polygon, box

from .roof_candidate_review import evidence_context, inspect
from .roof_review_pack import render_card
from .roof_upscale import UpscaleError, crop_geometry, upscale_roof_crop
from .sam3 import Sam3Proposal, Sam3TextSegmenter
from .scene_extract import extract_objects
from .scene_roof_diagnostics import _jsonable, roof_form_model
from .scene_self_review import local_outline
from .segmentation import mask_to_polygons

VERSION = 'roof-upscale-sam3-eval-v1'
ARMS = ('native', 'lanczos4x', 'realesrgan4x')
SETTINGS = {'prompt': 'building roof', 'threshold': .45, 'max_proposals': 80,
            'padding_px': 64, 'mask_simplify_source_pixels': .9, 'target_match_iou': .5,
            'sam3_input_size': 1008, 'roof_rise_m': 2., 'roof_form': 'auto',
            'roof_timeout_seconds': 30, 'case_limit': 6, 'encode_limit': 18}
NOTE = ('Experimental model applications, not accuracy or labels. Native ROI is the paired baseline; '
        'saved full-scene geometry identifies the target only. Generated pixels never validate roofs.')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    plain = _jsonable(value)
    if path.exists():
        if json.loads(path.read_bytes()) != plain:
            raise ValueError(f'Existing experiment artifact differs: {path}')
        return
    with path.open('x') as stream:
        stream.write(json.dumps(plain, indent=2, allow_nan=False)+'\n')


def source(row):
    binding = row['source']
    model = Path(binding['model_path']).read_bytes()
    data = Path(binding['image_path']).read_bytes()
    if sha(model) != binding['model_sha256'] or sha(data) != binding['image_sha256']:
        raise ValueError('Frozen source model or image changed')
    scene = json.loads(model)
    obj = next(o for o in scene['objects'] if o['id'] == row['object_id'])
    if (scene['id'] != row['scene_id'] or scene['image_sha256'] != binding['image_sha256']
            or obj['kind'] != 'building' or obj.get('source') != 'sam3-text-onnx'
            or obj.get('geometry_reviewed') or obj.get('roof_line_edits')):
        raise ValueError('Use a frozen unreviewed detector building')
    return scene, obj, Image.open(io.BytesIO(data)).convert('RGB')


def prepare_arms(row, obj, image, directory):
    raw = obj.get('boundary_refinement') or {}
    ring = raw.get('raw_ring') or obj['ring']
    crop = crop_geometry(image.size, ring, SETTINGS['padding_px'])
    if row.get('native_roi') != crop:
        raise ValueError('Computed ROI differs from the predeclared frozen ROI')
    native = image.crop(crop['crop_box'])
    images = {'native': native, 'lanczos4x': native.resize(tuple(4*n for n in native.size), Image.Resampling.LANCZOS)}
    upscale = {'status': 'not_run'}
    try:
        upscale = upscale_roof_crop(row['source']['image_path'], ring, padding_px=SETTINGS['padding_px'])
        if upscale['status'] != 'available' or upscale['crop_box'] != crop['crop_box']:
            raise ValueError('Real-ESRGAN did not return the same source ROI')
        images['realesrgan4x'] = Image.open(upscale['output_path']).convert('RGB')
    except UpscaleError as error:
        upscale = {'status': 'unavailable', 'error': str(error)}
    # Explicit absence is not an SR arm or an interpolation substitute.
    metadata = {'crop': crop, 'native_mask_size': list(native.size),
                'mask_to_source': [[1., 0.], [0., 1.], list(map(float, crop['crop_box'][:2]))],
                'coordinate_convention': 'mask_to_polygons uses native pixel-cell edges; [x,y,1] @ mask_to_source; no centre-index or 4x half-pixel shift',
                'encoder_note': 'SAM3 resizes every input to 1008x1008 bilinear; 4x inputs do not increase encoder or decoded-mask resolution.',
                'upscale': {key: value for key, value in upscale.items() if key != 'cache_hit'}, 'inputs': {}}
    for name, arm_image in images.items():
        path = directory/f'{name}-input.png'
        content = io.BytesIO()
        arm_image.save(content, format='PNG')
        data = content.getvalue()
        if path.exists() and path.read_bytes() != data:
            raise ValueError('An existing arm image changed')
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        encoder_image = arm_image.resize((SETTINGS['sam3_input_size'], SETTINGS['sam3_input_size']), Image.Resampling.BILINEAR)
        metadata['inputs'][name] = {'path': str(path), 'sha256': sha(data), 'rgb_sha256': sha(arm_image.tobytes()),
                                    'encoder_rgb_sha256': sha(encoder_image.tobytes()), 'size': list(arm_image.size)}
    write_new(directory/'arms.json', metadata)
    return images, metadata


def source_polygons(detections, crop, frame):
    """Keep all decoded detections and exact source-edge polygons before cleanup."""
    area = abs(float(np.linalg.det(np.asarray(frame)[:2])))
    minimum = max(12., 1./area)
    result = []
    for index, detection in enumerate(detections):
        if detection.mask.shape != (crop['crop_size'][1], crop['crop_size'][0]):
            raise ValueError('Each arm must decode directly onto the common native ROI grid')
        parts = mask_to_polygons(detection.mask, min_area_px=minimum, simplify_px=SETTINGS['mask_simplify_source_pixels'])
        for component, part in enumerate(parts):
            polygon = translate(part, xoff=crop['crop_box'][0], yoff=crop['crop_box'][1])
            result.append({'detection_index': index, 'component': component, 'score': detection.score,
                           'mask_sha256': sha(np.ascontiguousarray(detection.mask, dtype=np.uint8).tobytes()),
                           'ring': list(map(list, polygon.exterior.coords[:-1])),
                           'holes': [list(map(list, hole.coords[:-1])) for hole in polygon.interiors]})
    return result


class FrozenCropDetections:
    """Feed native decoded masks through existing extraction on original RGB."""
    def __init__(self, detections, crop):
        self.detections, self.crop = detections, crop

    def encode_image(self, image):
        return None

    def proposals(self, _features, prompt, image_size, **kwargs):
        if (prompt != SETTINGS['prompt'] or kwargs != {'threshold': SETTINGS['threshold'], 'max_proposals': SETTINGS['max_proposals']}):
            raise ValueError('Extraction prompt/threshold changed between experiment arms')
        left, top, right, bottom = self.crop['crop_box']
        for detection in self.detections:
            if detection.mask.shape != (bottom-top, right-left):
                raise ValueError('Cannot lift a 4x decoded mask into the native source frame')
            full = np.zeros((image_size[1], image_size[0]), dtype=bool)
            full[top:bottom, left:right] = detection.mask
            yield Sam3Proposal(full, detection.score, detection.box)


def detect_arm(segmenter, input_image, native_size, *, encoded=None):
    started = time.monotonic()
    features = segmenter.encode_image(input_image)
    encode_seconds = time.monotonic()-started
    if encoded:
        encoded(encode_seconds)
    decoded = time.monotonic()
    detections = segmenter.proposals(features, SETTINGS['prompt'], tuple(native_size),
                                     threshold=SETTINGS['threshold'], max_proposals=SETTINGS['max_proposals'])
    return detections, {'encode_seconds': encode_seconds, 'decode_seconds': time.monotonic()-decoded}


def polygon_of(obj, *, raw=False):
    shape = obj.get('boundary_refinement', {}) if raw else obj
    return Polygon(shape.get('raw_ring') or obj['ring'], shape.get('raw_holes', obj.get('holes', []))) if raw else Polygon(obj['ring'], obj.get('holes', []))


def overlap(a, b):
    area = a.intersection(b).area
    return {'iou': float(area/a.union(b).area), 'target_covered_fraction': float(area/a.area),
            'candidate_inside_target_fraction': float(area/b.area)}


def match_candidates(target, candidates):
    rows = [{'object_id': candidate['id'], 'score': candidate['score'],
             **overlap(target, polygon_of(candidate, raw=True))} for candidate in candidates]
    eligible = [(row['iou'], row['score'], index) for index, row in enumerate(rows) if row['iou'] >= SETTINGS['target_match_iou']]
    best = max(eligible)[2] if eligible else None
    return (candidates[best] if best is not None else None), rows


def roof_proposal(candidate, scene, row):
    ring, holes = local_outline(candidate, scene)
    request = {'ring': ring, 'holes': holes, 'eave': candidate['model']['height_m'],
               'rise': SETTINGS['roof_rise_m'], 'form': SETTINGS['roof_form'],
               'image_path': row['source']['image_path'], 'image_sha256': row['source']['image_sha256'],
               'meters_to_pixel': scene['meters_to_pixel'], 'optical_guidance': True}
    command = [sys.executable, '-m', 'scripts.ml.scene_roof', '--request', json.dumps(request)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=SETTINGS['roof_timeout_seconds'])
    if completed.returncode:
        raise ValueError(f'Roof inference failed: {completed.stdout[-1000:]} {completed.stderr[-1000:]}')
    return json.loads(completed.stdout)


def run_arm(name, detections, row, obj, scene, image, metadata, context, directory, timing):
    directory.mkdir(parents=True, exist_ok=True)
    raw = source_polygons(detections, metadata['crop'], scene['pixel_to_meters'])
    target = polygon_of(obj, raw=True)
    for candidate in raw:
        candidate.update(overlap(target, polygon_of(candidate)))
    write_new(directory/'detections.json', raw)
    np.savez_compressed(directory/'native-masks.npz', masks=np.array([d.mask for d in detections], dtype=bool),
                        scores=np.array([d.score for d in detections]))
    from .roof_boundary import model_canopies
    canopies = [tree for tree in scene['objects'] if tree.get('kind') == 'tree' and model_canopies([tree])]
    objects, _ = extract_objects(image, scene['image_sha256'], np.asarray(scene['pixel_to_meters']),
                                  FrozenCropDetections(detections, metadata['crop']), objects=canopies, kinds=['building'])
    candidate, matches = match_candidates(target, [o for o in objects if o['kind'] == 'building'])
    result = {'case': row['case'], 'arm': name, 'state': 'matched' if candidate else 'unmatched', 'source': row['source'],
              'input': metadata['inputs'][name], 'native_mask_size': metadata['native_mask_size'],
              'mask_to_source': metadata['mask_to_source'], 'detector': SETTINGS,
              'timing': timing, 'detection_count': len(detections), 'raw_component_count': len(raw), 'matches': matches,
              'all_candidate_objects': [o for o in objects if o['kind'] == 'building'],
              'note': NOTE, 'accepted': False, 'review_required': True}
    if candidate is None:
        write_new(directory/'arm.json', result)
        return result
    # Same unverified source eave for all arms; do not let class-prior reset
    # confound form/fold differences caused by segmentation.
    candidate['model']['height_m'] = obj['model']['height_m']
    candidate['matched_target_object_id'] = obj['id']
    roi = box(*metadata['crop']['crop_box'])
    result['roi_cut'] = polygon_of(candidate, raw=True).distance(roi.boundary) < .01
    result['source_image_cut'] = polygon_of(candidate, raw=True).distance(box(0, 0, *image.size).boundary) < .01
    write_new(directory/'segmentation.json', candidate)
    result['raw_outline'] = {'ring': candidate['boundary_refinement']['raw_ring'], 'holes': candidate['boundary_refinement']['raw_holes']}
    result['fitted_outline'] = {'ring': candidate['ring'], 'holes': candidate.get('holes', [])}
    if result['roi_cut']:
        result.update(state='roi_cut', roof_error='Artificial ROI cuts cannot be interpreted as eaves.')
    else:
        started = time.monotonic()
        try:
            proposal = roof_proposal(candidate, scene, row)
            result['timing']['roof_seconds'] = time.monotonic()-started
            candidate = {**candidate, 'roof_parts': proposal['parts'], 'roof_annotation': proposal['annotation'],
                         'roof_lines': proposal['lines'], 'roof_type': proposal['roof_type'],
                         'roof_form_evidence': proposal.get('form_evidence'), 'roof_form_uncertain': proposal['form_uncertain'],
                         'roof_optical_guidance': proposal.get('optical_guidance')}
            audit = inspect(candidate, scene, image, context)
            if not context[1]:
                audit['retained_observation_fraction'] = None
                if audit.get('cost'):
                    audit['cost']['image_line_mismatch_m'] = None
            result.update(state='complete', proposal=proposal, independent_audit=audit,
                          original_rgb_sha256=sha(image.tobytes()), fixed_observations=len(context[1]))
            write_new(directory/'candidate.json', candidate)
            render_card(obj, scene, image, directory/'source-card.png', observations=context[1], optical_guidance={
                'candidate_object': candidate, 'report': proposal.get('optical_guidance'),
                'form_evidence': proposal.get('form_evidence'),
                'boundary_guidance': {'accepted': False, 'inferred_edges': [], 'reason': 'experimental-segmentation-comparison'}})
        except (ValueError, RuntimeError, ArithmeticError, subprocess.TimeoutExpired) as error:
            result.update(state='roof_failed', roof_error=str(error))
    write_new(directory/'arm.json', result)
    return result


def code_hashes():
    folder = Path(__file__).parent
    files = set(folder.glob('roof_*.py')) | {folder/name for name in (
        'sam3.py', 'segmentation.py', 'scene_extract.py', 'scene_roof.py', 'scene_self_review.py', 'annotation_check.py', 'rooflines.py')}
    return {p.name: sha(p.read_bytes()) for p in sorted(files)}


def run_experiment(dataset_path, output_dir, *, models_dir=Path('scripts/ml/models/sam3'), timeout_s=1200):
    started = time.monotonic()
    dataset_bytes = Path(dataset_path).read_bytes()
    dataset = json.loads(dataset_bytes)
    if len(dataset['cases']) != SETTINGS['case_limit']:
        raise ValueError('Use the six predeclared cases; do not select on outcomes')
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    code = code_hashes()
    model_started = time.monotonic()
    segmenter = Sam3TextSegmenter(models_dir, input_size=SETTINGS['sam3_input_size'], providers=['CPUExecutionProvider'])
    manifest = {'version': VERSION, 'dataset_sha256': sha(dataset_bytes), 'settings': SETTINGS, 'code_sha256': code,
                'models': segmenter.model_identity, 'roof_form_model': roof_form_model(), 'providers': segmenter.vision.get_providers(), 'scope': NOTE}
    write_new(output_dir/'manifest.json', manifest)
    print(json.dumps({'event': 'models_loaded', 'seconds': time.monotonic()-model_started}), flush=True)
    rows, arm_times, budget_stop = [], [], False
    for row in dataset['cases']:
        if time.monotonic()-started >= timeout_s:
            budget_stop = True
            break
        scene, obj, image = source(row)
        directory = output_dir/row['case']
        images, metadata = prepare_arms(row, obj, image, directory)
        print(json.dumps({'event': 'arms_prepared', 'case': row['case'], 'upscale_status': metadata['upscale']['status'],
                          'native_mask_size': metadata['native_mask_size']}), flush=True)
        raw_target = {**obj, 'ring': list(map(list, polygon_of(obj, raw=True).exterior.coords[:-1])),
                      'holes': [list(map(list, h.coords[:-1])) for h in polygon_of(obj, raw=True).interiors]}
        context = evidence_context(raw_target, scene, image)
        write_new(directory/'fixed-evidence.json', {'source': row['source'], 'scope': 'Original RGB on saved raw detector target, fixed across arms.',
                                                   'outline': {'ring': raw_target['ring'], 'holes': raw_target['holes']}, 'observations': context[1]})
        for name in ARMS:
            completed_path = directory/name/'arm.json'
            if completed_path.exists():
                rows.append(json.loads(completed_path.read_bytes()))
                continue
            remaining = SETTINGS['encode_limit']-len(rows)
            elapsed = time.monotonic()-started
            estimate = remaining*max(arm_times, default=0)*1.15
            if elapsed >= timeout_s or (arm_times and estimate > timeout_s-elapsed):
                budget_stop = True
                break
            if name not in images:
                result = {'arm': name, 'state': 'unavailable', 'source': row['source'], 'reason': metadata['upscale'], 'case': row['case']}
                write_new(completed_path, result)
                rows.append(result)
                continue
            arm_started = time.monotonic()
            def encoded(seconds, name=name, row=row, images=images, metadata=metadata):
                print(json.dumps({'event': 'encoded', 'case': row['case'], 'arm': name, 'encode_seconds': seconds,
                                  'input_size': list(images[name].size), 'native_decode_size': metadata['native_mask_size']}), flush=True)
            detections, timing = detect_arm(segmenter, images[name], metadata['native_mask_size'], encoded=encoded)
            result = run_arm(name, detections, row, obj, scene, image, metadata, context, directory/name, timing)
            rows.append({**result, 'case': row['case']})
            arm_times.append(time.monotonic()-arm_started)
            print(json.dumps({'event': 'arm_complete', 'case': row['case'], 'arm': name, 'state': result['state'],
                              'seconds': arm_times[-1], 'detections': len(detections)}), flush=True)
        if code_hashes() != code:
            raise ValueError('Inference code changed during the frozen experiment')
        source(row)
        if budget_stop:
            break
    summary = {'version': VERSION, 'scope': NOTE, 'elapsed_seconds': time.monotonic()-started, 'budget_stopped': budget_stop,
               'completed_arms': len(rows), 'code_unchanged': code_hashes() == code,
               'rows': [{k: value for k, value in row.items() if k not in ('all_candidate_objects', 'proposal')} for row in rows]}
    # A partial interruption may be resumed from immutable per-arm outputs;
    # summaries are distinct snapshots, never replacements of earlier evidence.
    write_new(output_dir/f'summary-{len(rows):02d}-{time.time_ns()}.json', summary)
    print(json.dumps({'event': 'finished', 'completed_arms': len(rows), 'seconds': summary['elapsed_seconds'], 'budget_stopped': budget_stop}), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--models-dir', type=Path, default=Path('scripts/ml/models/sam3'))
    parser.add_argument('--seconds', type=int, default=1200)
    args = parser.parse_args()
    run_experiment(args.dataset, args.out, models_dir=args.models_dir, timeout_s=args.seconds)


if __name__ == '__main__':
    main()
