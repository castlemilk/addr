"""Read saved roof-line feedback through the canonical, revisioned scene API."""
from __future__ import annotations

import argparse
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def update_draft_roof_feedback(*, scene_id: str, object_id: str, expected_revision: int, model_signature: str,
                               image_sha256: str, outline_pixels: dict, changes: dict, base_url: str | None = None,
                               _object_polygon=False) -> dict:
    """Delegate a bounded source-bound draft patch to the canonical transaction."""
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', scene_id) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', object_id):
        raise ValueError('Use valid scene and roof object ids.')
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValueError('Supply the saved scene revision.')
    if not all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) for value in (model_signature, image_sha256)):
        raise ValueError('Supply the model and image identities from the saved scene.')
    payload = {'scene_id': scene_id, 'object_id': object_id, 'expected_revision': expected_revision,
               'model_signature': model_signature, 'image_sha256': image_sha256,
               'outline_pixels': outline_pixels, 'changes': changes}
    try:
        body = json.dumps(payload, allow_nan=False).encode()
    except (ValueError, TypeError) as error:
        raise ValueError('Use finite JSON draft fields.') from error
    base = (base_url or os.environ.get('ADDR_APP_URL', 'http://localhost:3008')).rstrip('/')
    token = os.environ.get('CRM_TOKEN')
    endpoint = '/api/scenes/object-feedback' if _object_polygon else '/api/scenes/roof-feedback'
    request = Request(base+endpoint, data=body, method='PATCH',
                      headers={'Accept': 'application/json', 'Content-Type': 'application/json',
                               **({'Authorization': 'Bearer '+token} if token else {})})
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            message = json.loads(error.read()).get('error', 'Draft save failed')
        except (ValueError, AttributeError):
            message = 'Draft save failed'
        raise ValueError(f'{error.code}: {message}') from error
    except (URLError, OSError, ValueError) as error:
        raise ValueError('The draft save could not be confirmed. Reload saved feedback before retrying; the save may have completed.') from error
    if (not isinstance(result, dict) or result.get('schema_version') != 1 or result.get('scene_id') != scene_id
            or result.get('object_id') != object_id or result.get('model_signature') != model_signature
            or result.get('image_sha256') != image_sha256 or result.get('revision') != expected_revision + 1
            or result.get('training_eligible') is not False or result.get('human_approval') is not False
            or not isinstance(result.get('object'), dict) or result['object'].get('id') != object_id):
        raise ValueError('The draft save response identity could not be verified. Reload before retrying.')
    return result


def load_roof_line_feedback(scene_id: str, object_id: str | None = None, *, base_url: str | None = None,
                            _object_polygon=False) -> dict:
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', scene_id):
        raise ValueError('Use a valid scene id.')
    if object_id is not None and (not object_id or len(object_id) > 150):
        raise ValueError('Use a valid object id.')
    base = (base_url or os.environ.get('ADDR_APP_URL', 'http://localhost:3008')).rstrip('/')
    params = {'id': scene_id, **({'object': object_id} if object_id else {})}
    token = os.environ.get('CRM_TOKEN')
    endpoint = '/api/scenes/object-feedback' if _object_polygon else '/api/scenes/roof-feedback'
    request = Request(base+endpoint+'?'+urlencode(params), headers={'Accept': 'application/json',
                       **({'Authorization': 'Bearer '+token} if token else {})})
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            message = json.loads(error.read()).get('error', 'Feedback request failed')
        except (ValueError, AttributeError):
            message = 'Feedback request failed'
        raise ValueError(f'{error.code}: {message}') from error
    except URLError as error:
        raise ValueError('The scene API is unavailable. Start the app and CRM service.') from error
    if result.get('schema_version') != 1 or result.get('scene_id') != scene_id:
        raise ValueError('The feedback response identity could not be verified.')
    return result


def load_object_feedback(scene_id: str, object_id: str | None = None, *, base_url: str | None = None) -> dict:
    """Read source-bound current polygons of any scene class, without roof line authority."""
    return load_roof_line_feedback(scene_id, object_id, base_url=base_url, _object_polygon=True)


def update_draft_object_polygon(**kwargs) -> dict:
    """The generic API accepts polygon replacements only and retains canonical guards."""
    return update_draft_roof_feedback(**kwargs, _object_polygon=True)


def apply_object_split(*, preview_id, scene_id, object_id, expected_revision, model_signature, image_sha256,
                       expected_child_ids, expected_kind, base_url=None):
    """Apply only one sealed preview through the canonical atomic split transaction."""
    if not isinstance(preview_id, str) or not re.fullmatch(r'[a-f0-9]{64}', preview_id):
        raise ValueError('Use the immutable split preview id.')
    base = (base_url or os.environ.get('ADDR_APP_URL', 'http://localhost:3008')).rstrip('/')
    token = os.environ.get('CRM_TOKEN')
    request = Request(base+'/api/scenes/object-split', data=json.dumps({'preview_id': preview_id}).encode(), method='POST',
                      headers={'Accept': 'application/json', 'Content-Type': 'application/json',
                               **({'Authorization': 'Bearer '+token} if token else {})})
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            message = json.loads(error.read()).get('error', 'Split save failed')
        except (ValueError, AttributeError):
            message = 'Split save failed'
        raise ValueError(f'{error.code}: {message}') from error
    except (URLError, OSError, ValueError) as error:
        raise ValueError('The split save could not be confirmed. Reload object feedback and lineage before retrying; the save may have completed.') from error
    source = (result.get('source_object') or {}) if isinstance(result, dict) else {}
    children = result.get('children', []) if isinstance(result, dict) else []
    if (not isinstance(result, dict) or result.get('schema_version') != 1 or result.get('preview_id') != preview_id
            or result.get('scene_id') != scene_id or result.get('object_id') != object_id
            or result.get('revision') != expected_revision+1 or result.get('model_signature') != model_signature
            or result.get('image_sha256') != image_sha256 or result.get('human_approval') is not False or result.get('training_eligible') is not False
            or not isinstance(source, dict) or source.get('id') != object_id or source.get('status') != 'superseded' or source.get('kind') != expected_kind
            or source.get('replacement_ids') != expected_child_ids or not isinstance(children, list)
            or not all(isinstance(child, dict) for child in children) or [child.get('id') for child in children] != expected_child_ids
            or any(child.get('kind') != expected_kind or child.get('geometry_reviewed') is not False
                   or child.get('model', {}).get('height_verified') is not False or child.get('derived_from') != [object_id]
                   or child.get('status') != 'needs_adjustment' or child.get('source') != 'machine:mcp-object-split-draft-v1'
                   or child.get('provenance', {}).get('tier') != 'T3' or child.get('provenance', {}).get('human_confirmed') is not False
                   for child in children)):
        raise ValueError('The split save response identity could not be verified. Reload object feedback and lineage before retrying.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scene', required=True)
    parser.add_argument('--object')
    parser.add_argument('--base-url')
    args = parser.parse_args()
    try:
        print(json.dumps(load_roof_line_feedback(args.scene, args.object, base_url=args.base_url), indent=2))
    except ValueError as error:
        parser.exit(1, str(error)+'\n')


if __name__ == '__main__':
    main()
