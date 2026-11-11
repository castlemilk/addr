"""Audited human address decisions, bound to source geometry and address evidence."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import mapping

from .building_addresses import RESOLVER_VERSION, load_index, unresolved


class ResolutionError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def binding(roof, model_signature, evidence, address_id=None):
    def rounded(value):
        if isinstance(value, float):
            return round(value, 9)
        if isinstance(value, (tuple, list)):
            return [rounded(v) for v in value]
        if isinstance(value, dict):
            return {k: rounded(v) for k, v in value.items()}
        return value
    return digest({'roof': rounded(mapping(roof.normalize())), 'model': model_signature,
                   'dataset': evidence.get('dataset_signature'), 'policy': RESOLVER_VERSION,
                   'partial': evidence.get('partial', False), 'address_id': address_id})


def location(root, scene, obj):
    if not all(isinstance(v, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', v) for v in (scene, obj)):
        raise ResolutionError('Invalid roof identity.')
    return root/'data/address-evidence/decisions'/(digest([scene, obj])+'.json')


def read(root, scene, obj):
    path = location(root, scene, obj)
    return json.loads(path.read_text()) if path.exists() else {'revision': 0, 'history': [], 'current': None}


def apply(evidence, root, scene, obj, current_binding):
    saved = read(root, scene, obj)
    current = saved['current']
    result = dict(evidence)
    result['decision_revision'] = saved['revision']
    result['evidence_binding'] = current_binding
    if not current:
        return result
    if current['binding'] != current_binding:
        return {**result, 'decision_state': 'stale', 'next_action': 'A saved address decision needs rechecking because the roof or official evidence changed.'}
    if current['action'] == 'defer':
        return {**result, 'decision_state': 'deferred', 'next_action': current['note']}
    selected = current['addresses']
    resolved = {**result, 'status': 'reviewed', 'method': 'reviewer-evidence', 'scope': current['scope'],
            'candidates': selected, 'automatic_status': evidence['status'], 'decision_state': 'confirmed',
            'reason': current['note'], 'review_source': current['source_kind'],
            'reviewed_at': current['at'], 'next_action': 'Saved address association only; roof geometry and training approvals are unchanged.'}
    if len(selected) == 1:
        resolved['full_address'] = selected[0]['full_address']
    else:
        resolved.pop('full_address', None)
    return resolved


def context(root, scene_id, object_id):
    from .building_addresses import linked
    from .property_address_resolution import image_cut, visible_only
    from .scene_addresses import catalog
    from .scene_confidence import geographic
    location(root, scene_id, object_id)
    directory = root/'data/scenes'/scene_id
    try:
        scene = json.loads((directory/'latest.json').read_text())
    except FileNotFoundError as exc:
        raise ResolutionError('Scene not found.', 404) from exc
    path = directory/'review.json'
    review = json.loads(path.read_text()) if path.exists() else None
    if review and review.get('model_signature') != scene['model_signature']:
        raise ResolutionError('Recover the changed scene review before resolving its address.', 409)
    objects = review.get('objects', []) if review else scene['objects']
    obj = next((o for o in objects if o['id'] == object_id and o['kind'] == 'building' and o.get('status') not in ('rejected', 'superseded')), None)
    if obj is None:
        raise ResolutionError('Active roof not found.', 404)
    roof = geographic(obj, scene)
    index = load_index(root)
    evidence = index.match(roof) if index else unresolved('Download official address evidence before resolving this roof.')
    evidence = visible_only(evidence, image_cut(scene, obj))
    evidence = linked(obj, catalog([obj.get('address_id')], root=root)) or evidence
    saved = read(root, scene_id, object_id)
    current_binding = binding(roof, scene['model_signature'], evidence, obj.get('address_id'))
    return {'scene_id': scene_id, 'object_id': object_id, 'binding': current_binding,
            'geometry_layer': 'saved_edits' if review else 'model',
            'model_signature': scene['model_signature'], 'dataset_signature': evidence.get('dataset_signature'),
            'revision': saved['revision'], 'evidence': evidence, 'decision': saved['current'],
            'resolved': apply(evidence, root, scene_id, object_id, current_binding),
            'decision_stale': bool(saved['current'] and saved['current']['binding'] != current_binding),
            'has_saved_local_link': bool(obj.get('address_id')),
            'history_count': len(saved['history'])}


def save(root, payload, actor):
    scene, obj = payload.get('scene_id'), payload.get('object_id')
    path = location(root, scene, obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Cross-process compare-and-swap: two tabs cannot silently overwrite each other.
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = context(root, scene, obj)
        if type(payload.get('revision')) is not int or payload['revision'] != current['revision'] or payload.get('binding') != current['binding']:
            raise ResolutionError('The roof, address evidence or saved decision changed. Reload before saving.', 409)
        action = payload.get('action')
        if action not in ('confirm', 'defer', 'clear'):
            raise ResolutionError('Choose confirm, defer or clear.')
        if current['has_saved_local_link'] and action == 'confirm':
            raise ResolutionError('This roof already has a saved CRM address. Review that link in the scene editor first.', 409)
        note = payload.get('note', '')
        if not isinstance(note, str) or not 10 <= len(note.strip()) <= 2000:
            raise ResolutionError('Record the supporting evidence or the reason for deferring (10–2000 characters).')
        note = note.strip()
        selected = payload.get('address_keys', [])
        candidates = {c['property_pfi']+'/'+c['address_pfi']: c for c in current['evidence']['candidates']}
        scope, source = payload.get('scope'), payload.get('source_kind')
        addresses = []
        if action == 'confirm':
            if not isinstance(selected, list) or not selected or len(selected) > 200 or any(not isinstance(k, str) or k not in candidates for k in selected) or len(set(selected)) != len(selected):
                raise ResolutionError('Choose current official address candidates for this roof.')
            if scope not in ('unit', 'shared_roof', 'site') or (scope == 'unit' and len(selected) != 1):
                raise ResolutionError('An individual unit needs one address; choose shared roof or site for several.')
            if source not in ('title_or_site_plan', 'verified_entrance', 'local_knowledge'):
                raise ResolutionError('Choose the source of the address evidence.')
            addresses = [candidates[key] for key in selected]
        saved = read(root, scene, obj)
        event = {'revision': saved['revision']+1, 'binding': current['binding'], 'action': action,
                 'model_signature': current['model_signature'], 'dataset_signature': current['dataset_signature'],
                 'scope': scope if action == 'confirm' else None, 'addresses': addresses,
                 'source_kind': source if action == 'confirm' else None, 'note': note, 'actor': actor,
                 'at': datetime.now(UTC).isoformat()}
        output = {'revision': event['revision'], 'current': event if action != 'clear' else None,
                  'history': [*saved['history'], event], 'scene_id': scene, 'object_id': obj}
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            json.dump(output, stream)
            temporary = stream.name
        os.replace(temporary, path)
        return context(root, scene, obj)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    try:
        result = save(Path('.'), payload, payload['actor']) if args.save else context(Path('.'), payload['scene_id'], payload['object_id'])
        print(json.dumps(result))
    except ResolutionError as exc:
        print(json.dumps({'error': str(exc), 'status': exc.status}))


if __name__ == '__main__':
    main()
