"""Read-only, stable MCP references to generated addresses and saved scene models.

The composer and review app share this catalogue. It never runs inference, reads
credentials, or interprets model geometry as approved review/training labels.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from mcp.types import ResourceLink

from .address_review import REPO_ROOT, load_records

SCENES_ROOT = REPO_ROOT / 'data' / 'scenes'
SEARCH_LIMIT = 20
SCENE_ID = re.compile(r'[a-zA-Z0-9_-]{1,100}')
MODEL_NOTE = 'Model estimates only. Saved review feedback and human approval remain separate.'
ADDRESS_NOTE = ('Generated address record; boundary_source and roof_form_source retain model, CRM or reviewer '
                'provenance. Inferred geometry is an estimate; approval and training eligibility remain separate.')


def _records() -> list[dict]:
    try:
        return load_records()
    except FileNotFoundError:
        return []


def address_resource(record: dict) -> ResourceLink:
    address_id = int(record['address_id'])
    return ResourceLink(
        type='resource_link', uri=f'addr://addresses/{address_id}', name=str(address_id),
        title=record['full_address'], mime_type='application/json',
        description=f"Address {address_id} · {record.get('kind', 'roof')} · {ADDRESS_NOTE}",
    )


def _scene_path(scene_id: str) -> Path:
    if not SCENE_ID.fullmatch(scene_id):
        raise ValueError('Choose a valid scene ID.')
    directory = SCENES_ROOT / scene_id
    path = directory / 'latest.json'
    # References cannot turn an arbitrary symlink into filesystem access.
    if directory.is_symlink() or path.is_symlink():
        raise ValueError('Scene references must point to saved scene models.')
    return path


@lru_cache(maxsize=4096)
def _scene_title(path: Path, mtime_ns: int, size: int) -> str:
    # Cache only the title, never the full geometry, keyed by the saved version.
    model = json.loads(path.read_text())
    if not isinstance(model, dict) or model.get('id') != path.parent.name:
        raise ValueError('The saved scene identity does not match this reference.')
    return str(model.get('title') or path.parent.name)


def search_resources(query: str) -> list[ResourceLink]:
    """Return bounded, deterministic address and scene choices, including for ''."""
    needle = query.strip().casefold()
    records = [record for record in _records()
               if needle in record['full_address'].casefold()
               or needle in str(record['address_id'])]
    records.sort(key=lambda record: (record['full_address'].casefold(), int(record['address_id'])))
    # Reserve space for scene results when both domains match the same query.
    items = [address_resource(record) for record in records[:SEARCH_LIMIT // 2]]
    if SCENES_ROOT.exists():
        for directory in sorted(SCENES_ROOT.iterdir(), key=lambda path: path.name):
            if not directory.is_dir() or not SCENE_ID.fullmatch(directory.name) or directory.name.startswith('qa_'):
                continue
            try:
                path = _scene_path(directory.name)
                stat = path.stat()
                title = _scene_title(path, stat.st_mtime_ns, stat.st_size)
            except (FileNotFoundError, ValueError, OSError):
                # Extraction directories can exist before their first snapshot.
                continue
            if needle not in directory.name.casefold() and needle not in title.casefold():
                continue
            items.append(ResourceLink(
                type='resource_link', uri=f'addr://scenes/{directory.name}', name=directory.name,
                title=title, mime_type='application/json', description=f'Saved scene model · {MODEL_NOTE}',
            ))
            if len(items) >= SEARCH_LIMIT:
                break
    remaining = SEARCH_LIMIT - len(items)
    items.extend(address_resource(record) for record in records[SEARCH_LIMIT // 2:SEARCH_LIMIT // 2 + remaining])
    return items


def read_address(address_id: str) -> str:
    if not re.fullmatch(r'[1-9][0-9]{0,15}', address_id):
        raise ValueError('Choose a positive address ID.')
    record = next((record for record in _records() if str(record['address_id']) == address_id), None)
    if record is None:
        raise ValueError('This address record is unavailable.')
    return json.dumps({'record': record, 'provenance': ADDRESS_NOTE}, ensure_ascii=False)


def read_scene(scene_id: str) -> str:
    try:
        model = json.loads(_scene_path(scene_id).read_text())
    except FileNotFoundError as error:
        raise ValueError('This scene has not been extracted yet.') from error
    if not isinstance(model, dict) or model.get('id') != scene_id:
        raise ValueError('The saved scene identity does not match this reference.')
    return json.dumps({'model': model, 'provenance': MODEL_NOTE}, ensure_ascii=False)
