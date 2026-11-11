"""Read current source-bound agent notes without rendering or changing reviews."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from PIL import Image

# The immutable v1 storage contract is deliberately independent of vision imports.
DIRECTORY = Path('data/reviews/eyes/roof-review')
SCOPE = 'Diagnostic review; agent observations are tier 3, never human approvals, measured heights or training labels.'
GEOMETRY_FIELDS = ('ring', 'holes', 'roof_parts', 'roof_annotation', 'roof_lines', 'roof_type', 'model')


def _identity(root: Path, scene_id: str, object_id: str, *, require_building=True) -> dict:
    if not isinstance(scene_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', scene_id):
        raise ValueError('Use a valid scene id')
    folder = root/'data/scenes'/scene_id
    model_path = folder/'latest.json'
    if folder.is_symlink() or model_path.is_symlink():
        raise ValueError('Use an original saved scene model')
    source = model_path.read_bytes()
    scene = json.loads(source)
    if not isinstance(scene, dict) or scene.get('id') != scene_id:
        raise ValueError('Source scene identity does not match its directory')
    digest = scene.get('image_sha256', '')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Invalid imagery identity')
    image_path = folder/f'{digest}.png'
    if image_path.is_symlink() or hashlib.sha256(image_path.read_bytes()).hexdigest() != digest:
        raise ValueError('Source imagery changed')
    with Image.open(image_path) as image:
        if image.size != (scene['image_width'], scene['image_height']):
            raise ValueError('Imagery dimensions changed')
        image.verify()
    objects = scene.get('objects', [])
    matches = [obj for obj in objects if isinstance(obj, dict) and obj.get('id') == object_id]
    identity_origin = 'source-model'
    if not matches and not require_building:
        # Generic inspectors can bind review-created instances without inventing source predictions.
        review_path = folder/'review.json'
        if review_path.is_symlink() or not review_path.is_file():
            raise ValueError('Choose one current object in this scene')
        review_bytes = review_path.read_bytes()
        review = json.loads(review_bytes)
        if review.get('model_signature') != scene.get('model_signature'):
            raise ValueError('The saved object belongs to a stale source model')
        matches = [obj for obj in review.get('objects', []) if isinstance(obj, dict) and obj.get('id') == object_id]
        if len(matches) != 1:
            raise ValueError('Choose one current machine-derived object in this scene')
        obj = matches[0]
        provenance = obj.get('provenance') or {}
        ancestors = obj.get('derived_from') or []
        parent = next((item for item in review.get('objects', []) if len(ancestors) == 1 and item.get('id') == ancestors[0]), None)
        if (obj.get('source') != 'machine:mcp-object-split-draft-v1'
                or provenance.get('source') not in ('machine:mcp-object-split-draft-v1', 'machine:mcp-polygon-draft-v1', 'machine:mcp-roof-feedback-draft-v1')
                or provenance.get('source_scene_id') != scene_id or provenance.get('source_model_signature') != scene.get('model_signature')
                or provenance.get('source_image_sha256') != digest or not isinstance(provenance.get('draft_fingerprint'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', provenance.get('split_preview_id', ''))
                or parent is None or parent.get('kind') != obj.get('kind') or obj['id'] not in parent.get('replacement_ids', [])
                or obj.get('fieldwork_import') or obj.get('geometry_reviewed') or obj.get('status') == 'confirmed'
                or (obj.get('model') or {}).get('height_verified') or (obj.get('roof_line_edits') or {}).get('checked')
                or provenance.get('human_approval') or provenance.get('human_confirmed')):
            raise ValueError('This review-only object has no trusted unchecked machine split lineage; use the scene editor')
        if review_path.read_bytes() != review_bytes:
            raise ValueError('The saved object changed while binding its source')
        identity_origin = 'saved-review'
    if len(matches) != 1 or (require_building and matches[0].get('kind') != 'building'):
        raise ValueError('Choose one current building object in this scene' if require_building else 'Choose one current object in this scene')
    # Match scene_confidence.geometry_signature/scene_batch.signature exactly.
    # The compatibility test compares this identity to an actual rendered handoff.
    geometry = {key: matches[0].get(key) for key in GEOMETRY_FIELDS}
    geometry_digest = hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()
    if model_path.read_bytes() != source:
        raise ValueError('Source model changed while loading observations')
    result = {'scene_id': scene_id, 'object_id': object_id, 'source_model_sha256': hashlib.sha256(source).hexdigest(),
              'model_signature': scene['model_signature'], 'image_sha256': digest, 'geometry_signature': geometry_digest}
    if identity_origin == 'saved-review':
        result['identity_origin'] = identity_origin
    return result


def editor_url(scene_id: str, object_id: str) -> str:
    base = os.environ.get('ADDR_APP_URL', 'http://localhost:3008').rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('ADDR_APP_URL must be an HTTP(S) app origin without credentials, query or fragment')
    return base + '/scenes?' + urlencode({'scene': scene_id, 'object': object_id})


def list_observations(scene_id: str, object_id: str, *, root=Path('.')) -> dict:
    root = Path(root).resolve()
    identity = _identity(root, scene_id, object_id)
    notes = []
    for path in sorted((root/DIRECTORY).glob('*/observations/*.json')):
        if (path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink()
                or not re.fullmatch(r'[a-f0-9]{64}', path.parent.parent.name) or path.stat().st_size > 65536):
            raise ValueError('Use original immutable observation sidecars')
        note = json.loads(path.read_bytes())
        if not isinstance(note, dict):
            raise ValueError('An agent observation is invalid')
        if note.get('identity') != identity:
            continue
        payload = {key: value for key, value in note.items() if key not in ('observation_id', 'recorded_at')}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if (note.get('version') != 'roof-agent-observation-v1' or note.get('tier') != 3
                or note.get('training_eligible') is not False or note.get('human_approval') is not False
                or note.get('observation_id') != digest or path.stem != digest
                or note.get('review_id') != path.parent.parent.name):
            raise ValueError('An agent observation was altered; inspect its immutable sidecar')
        notes.append(note)
    if _identity(root, scene_id, object_id) != identity:
        raise ValueError('Source changed while reading observations; reload the scene')
    return {'schema_version': 1, 'identity': identity, 'observations': sorted(notes, key=lambda note: (note['recorded_at'], note['observation_id'])),
            'scope': SCOPE, 'training_eligible': False, 'human_approval': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scene_id')
    parser.add_argument('object_id')
    args = parser.parse_args()
    print(json.dumps(list_observations(args.scene_id, args.object_id), allow_nan=False))
