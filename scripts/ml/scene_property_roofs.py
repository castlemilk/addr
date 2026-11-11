"""Default parcel-aware output for new scene inference.

Physical roof surfaces keep their identities and topology. Numbered-lot roof
allocations are a second model output, with explicit source and uncertainty,
never a replacement for measured walls or approved training labels.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from shapely.errors import GEOSException
from shapely.ops import unary_union

from .roof_block_context import fetch_parcels, lot_context, rings
from .roof_lot_review import (
    POLICY,
    POLICY_VERSION,
    cache_key,
    detect_lot,
    geometry,
    lot_review,
    pixel_scale,
    searches,
)
from .scene_batch import signature
from .scene_training import atomic_json

VERSION = 'parcel-aware-scene-v1'
UNSET = object()


def input_identity(scene):
    """Exclude output metadata: re-analysis cannot validate stale allocations."""
    return signature({
        'image': scene['image_sha256'], 'size': [scene.get('image_width'), scene.get('image_height')],
        'frame': scene['pixel_to_meters'], 'inverse': scene.get('meters_to_pixel'),
        'centroid': scene.get('centroid'), 'bounds': scene.get('region', {}).get('bounds'),
        'objects': [{key: obj.get(key) for key in ('id', 'source', 'kind', 'status', 'ring', 'holes',
                    'geometry_reviewed', 'roof_line_edits', 'roof_feedback', 'boundary_refinement')}
                    for obj in sorted(scene.get('objects', []), key=lambda obj: obj['id'])]})


def inherited_parcels(scene):
    output = scene.get('property_roofs', {})
    inputs = output.get('inputs', {})
    if not output.get('parcels') or not inputs.get('geometry'):
        return None
    if (inputs.get('geometry') == input_identity(scene) and output.get('parcels')
            and inputs.get('parcels') == signature(output['parcels'])):
        return output['parcels']
    return None


def inherited_review(scene, block):
    output = scene.get('property_roofs', {})
    if output.get('version') != VERSION or inherited_parcels(scene) is None:
        return None
    for prior in output.get('blocks', []):
        if signature(prior.get('lots')) != signature(block.get('lots')):
            continue
        review = prior.get('lot_review', {})
        if review.get('version') == POLICY_VERSION and review.get('policy') == POLICY:
            return copy.deepcopy(review)
    return None


def parcel_input(root, scene):
    """Freeze successful public lookups per registered image; do not cache errors."""
    bounds = scene.get('region', {}).get('bounds')
    if not bounds:
        raise ValueError('No geographic bounds are available for this image.')
    key = signature([scene['image_sha256'], bounds, scene.get('centroid'), scene['pixel_to_meters']])
    folder = root/'data/scene-parcels'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/f'{key}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder/f'{key}.json'
        if path.exists():
            saved = json.loads(path.read_text())
            if saved.get('key') != key or saved.get('sha256') != signature(saved.get('data')):
                raise ValueError('Frozen parcel evidence changed.')
            return saved['data']
        data = fetch_parcels(bounds)
        atomic_json(path, {'key': key, 'sha256': signature(data), 'data': data})
        return data


def canonical_blocks(groups, mpp):
    """Coalesce overlapping search contexts, never count or analyse a lot twice.

    Only an identical parcel identity connects contexts. A plan number or nearby
    roof alone cannot connect them. Oversized/contradictory contexts are deferred.
    """
    result = []
    for group in groups:
        merged = copy.deepcopy(group)
        while True:
            identities = {lot['pfi'] for lot in merged['lots']}
            match = next((old for old in result if identities & {lot['pfi'] for lot in old['lots']}), None)
            if match is None:
                break
            result.remove(match)
            lots = {lot['pfi']: lot for lot in match['lots']}
            conflict = any(lot['pfi'] in lots and signature(lot) != signature(lots[lot['pfi']]) for lot in merged['lots'])
            lots.update({lot['pfi']: lot for lot in merged['lots']})
            merged['lots'] = sorted(lots.values(), key=lambda lot: lot['pfi'])
            merged['members'] = list({m['object_id']: m for m in match['members']+merged['members']}.values())
            perimeter = unary_union([geometry(lot['polygons']) for lot in merged['lots']])
            merged.update(perimeter=rings(perimeter), complete_in_image=match['complete_in_image'] and merged['complete_in_image'],
                          numbered_lots=sum(bool(re.fullmatch(r'\d+[A-Za-z]?', str(lot['lot']))) for lot in merged['lots']),
                          reason='Shared parcel identities give one bounded set of independent lot searches. Physical roof surfaces remain separate.')
            if conflict or match.get('plan') != merged.get('plan') or len(lots) > 32 or perimeter.area*mpp*mpp > 12000 or match.get('search_error'):
                merged['search_error'] = 'Overlapping search contexts exceed the bounded or unambiguous cadastral scope.'
        result.append(merged)
    return result


def infer_property_roofs(scene, image, segmenter, *, root=Path('.'), parcels=UNSET):
    result = {'version': VERSION, 'policy_version': POLICY_VERSION, 'policy': dict(POLICY),
              'status': 'unavailable', 'blocks': [], 'counts': {}, 'sam_searches': 0,
              'physical_roofs_unchanged': True, 'human_approved': False, 'training_eligible': False,
              'inputs': {'geometry': input_identity(scene)},
              'note': 'Cadastral cuts allocate detected roof area; they do not create physical roof folds or verified unit addresses.'}
    buildings = [obj for obj in scene.get('objects', []) if obj.get('kind') == 'building'
                 and obj.get('source') == 'sam3-text-onnx' and obj.get('status') == 'candidate'
                 and not obj.get('geometry_reviewed') and not obj.get('roof_line_edits') and not obj.get('roof_feedback')]
    if not buildings:
        return {**result, 'status': 'not_applicable', 'reason': 'No untouched detector roofs to allocate.'}
    try:
        mpp = pixel_scale(scene)
        if image.size != (scene['image_width'], scene['image_height']):
            raise ValueError('Image dimensions differ from its registered frame.')
        data = parcel_input(root, scene) if parcels is UNSET else parcels
        if not data:
            raise ValueError('Cadastral evidence is unavailable.')
        result['parcels'] = data
        result['inputs']['parcels'] = signature(data)
        weights = getattr(segmenter, 'model_identity', None)
        if not weights:
            raise ValueError('The active segmenter has no verifiable model identity.')
        result['inputs']['detector_weights'] = weights
        result['inputs']['inference_code'] = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                             for name in ('scene_property_roofs.py', 'roof_lot_review.py', 'roof_block_context.py', 'roof_boundary.py', 'roof_family_review.py')}
        groups, seen = [], set()
        for obj in sorted(buildings, key=lambda obj: obj['id']):
            block = lot_context(scene, obj, data)
            if not block['lots']:
                continue
            key = signature(sorted(lot['pfi'] for lot in block['lots']))
            if key not in seen:
                groups.append(block)
                seen.add(key)
        groups = canonical_blocks(groups, mpp)
        # Prioritise complete attached-unit sites over incidental parcel overlap.
        groups.sort(key=lambda block: (not block['complete_in_image'], block['numbered_lots'] < 2,
                                      -block['numbered_lots'], block.get('plan', ''), signature(block['lots'])))
        work_scene = {**scene, 'model_signature': result['inputs']['geometry']}
        for block in groups:
            planned = searches(scene, block)
            payload = {'key': cache_key(work_scene, block), 'scene_id': scene['id'],
                       'image_sha256': scene['image_sha256'], 'model_signature': work_scene['model_signature'],
                       'detector_weights': weights, 'lots': []}
            for search in planned:
                if block.get('search_error') or search['status'] != 'ready' or result['sam_searches'] >= POLICY['max_lots_per_scene']:
                    continue
                row = {'pfi': search['pfi'], 'lot': search['lot'], 'crop': search['crop']}
                result['sam_searches'] += 1
                try:
                    row['detections'] = detect_lot(image, search, segmenter)
                except (RuntimeError, ValueError, GEOSException) as error:
                    row['error'] = str(error)[:500]
                payload['lots'].append(row)
            block['lot_review'] = lot_review(root, work_scene, block, image, observations=payload)
            block['lot_observations'] = payload
            if block.get('search_error'):
                for lot in block['lot_review']['lots']:
                    lot.update(status='needs_context', reason=block['search_error'], polygons=[])
                block['lot_review']['status'] = 'partial'
            result['blocks'].append(block)
        states = [lot['status'] for block in result['blocks'] for lot in block['lot_review']['lots']]
        result['counts'] = dict(Counter(states))
        result['status'] = ('not_applicable' if not states else 'partial' if any(state in ('not_run', 'failed', 'needs_context') for state in states) else 'complete')
        result['reason'] = ('No uniquely matched numbered lots.' if not states else
                            'Bounded per-lot inference completed; unresolved searches and model disagreements remain visible.')
    except (OSError, KeyError, TypeError, ValueError, GEOSException) as error:
        result.update(status='unavailable', reason=str(error)[:500])
    return result


def add_property_roofs(scene, image, segmenter, *, root=Path('.'), parcels=UNSET):
    output = infer_property_roofs(scene, image, segmenter, root=root, parcels=parcels)
    scene['property_roofs'] = output
    scene.setdefault('analysis', {}).update(parcel_roof_model=VERSION, parcel_roof_policy=POLICY_VERSION,
                                             parcel_roof_status=output['status'], parcel_roof_searches=output['sam_searches'])
