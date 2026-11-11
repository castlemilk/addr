"""Immutable native-image previews and canonical unchecked, same-class instance splits."""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .scene_instances import partition_outlines
from .scene_polygon_feedback import (
    SCOPE,
    _bytes,
    _current,
    _hash,
    _inspection_layout,
    _outline,
    _protection,
    _render_inspection,
    _text,
)
from .scene_roof_observations import _identity, editor_url

VERSION = 'object-agent-split-preview-v1'
DIRECTORY = Path('data/reviews/eyes/object-split')
COLORS = ['#fbbf24', '#34d399', '#60a5fa', '#c084fc', '#f472b6', '#fb923c', '#2dd4bf', '#a3e635']


def child_id(binding, key):
    return 'split_'+_hash(_identity_bytes([binding['scene_id'], binding['object_id'], binding['model_signature'], binding['object_kind'], key]))[:32]


def _identity_bytes(value):
    """Match JavaScript JSON.stringify for cross-language stable string identities."""
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def _children(value, binding, model, parent):
    if not isinstance(value, list) or not 2 <= len(value) <= 8:
        raise ValueError('Supply two to eight child parts, including explicit unresolved residual parts.')
    keys, result = set(), []
    for index, child in enumerate(value):
        if not isinstance(child, dict) or not {'key', 'outline_pixels'} <= set(child) or set(child)-{'key', 'label', 'outline_pixels', 'notes', 'role'}:
            raise ValueError('Children accept key, label, outline_pixels, notes and role only; their class and IDs are server-controlled.')
        key = child['key']
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', key) or key in keys:
            raise ValueError('Use unique stable child keys of one to 60 letters, numbers, underscores or hyphens.')
        keys.add(key)
        role = child.get('role', 'object')
        if role not in ('object', 'unresolved-residual'):
            raise ValueError('A child role is object or unresolved-residual; it does not approve or change its source class.')
        label = _text(child.get('label', f"{parent['label']} · part {index+1}"), 150, 'Child label')
        if not label:
            raise ValueError('Every child needs a visible label.')
        notes = _text(child.get('notes', ''), 2000, 'Child notes')
        outline, _ = _outline(child['outline_pixels'], model['image_width'], model['image_height'])
        result.append({'key': key, 'object_id': child_id(binding, key), 'label': label, 'outline_pixels': outline, 'notes': notes, 'role': role})
    return result


def _folder(root, preview_id):
    if not isinstance(preview_id, str) or not re.fullmatch(r'[a-f0-9]{64}', preview_id):
        raise ValueError('Use the preview_id returned by preview_object_split.')
    directory = Path(root).resolve()/DIRECTORY/preview_id
    if any(path.is_symlink() for path in (directory, directory.parent, directory.parent.parent)):
        raise ValueError('Use original object split sidecars.')
    return directory


def read_preview(preview_id, *, root=Path('.')):
    directory = _folder(root, preview_id)
    manifest, card = directory/'manifest.json', directory/'card.png'
    if manifest.is_symlink() or card.is_symlink():
        raise ValueError('Use original immutable split artifacts.')
    saved = json.loads(manifest.read_bytes())
    content = saved.get('content')
    if (not isinstance(content, dict) or content.get('version') != VERSION or saved.get('preview_id') != preview_id
            or _hash(_bytes(content)) != preview_id or content.get('card_sha256') != _hash(card.read_bytes())):
        raise ValueError('The split preview or image was altered. Request and inspect a fresh split preview.')
    return content


def _render(image, source, children, render, target):
    box, size = render['crop_box_pixels'], render['panel_size_pixels']
    width, height = size
    sx, sy = render['display_scale']
    rgb = image.crop(box).resize(tuple(size), Image.Resampling.NEAREST)
    canvas = Image.new('RGB', (1966, height+110), '#101827')
    font = ImageFont.load_default(size=14)
    title = ('Original RGB · no generated detail', 'Native-pixel grid · source coordinates', 'Proposed pure partition · unchecked child / edge indices')
    for panel in range(3):
        view = rgb.copy()
        draw = ImageDraw.Draw(view)
        if panel == 1:
            for x in render['grid']['x']:
                px = (x-box[0])*sx
                draw.line([(px, 0), (px, height)], fill='#81929a', width=1)
                draw.text((px+2, 2), f'x={x}', font=font, fill='white', stroke_width=1, stroke_fill='#101827')
            for y in render['grid']['y']:
                py = (y-box[1])*sy
                draw.line([(0, py), (width, py)], fill='#81929a', width=1)
                draw.text((2, py+2), f'y={y}', font=font, fill='white', stroke_width=1, stroke_fill='#101827')
        elif panel == 2:
            for ring in [source['ring'], *source['holes']]:
                points = [((p[0]-box[0])*sx, (p[1]-box[1])*sy) for p in ring]
                draw.line([*points, points[0]], fill='#67d7e7', width=1)
            for index, child in enumerate(children):
                color = '#fb7185' if child['role'] == 'unresolved-residual' else COLORS[index]
                shape = child['outline_pixels']
                for hole_index, ring in enumerate([shape['ring'], *shape['holes']]):
                    points = [((p[0]-box[0])*sx, (p[1]-box[1])*sy) for p in ring]
                    draw.line([*points, points[0]], fill=color, width=3)
                    for vertex, point in enumerate(points):
                        following = points[(vertex+1) % len(points)]
                        prefix = f'c{index}:'+(f'h{hole_index-1}:' if hole_index else '')
                        draw.ellipse((point[0]-2, point[1]-2, point[0]+2, point[1]+2), fill=color)
                        draw.text((point[0]+3, point[1]+3), f'{prefix}v{vertex}', font=font, fill=color, stroke_width=1, stroke_fill='#101827')
                        draw.text(((point[0]+following[0])/2, (point[1]+following[1])/2), f'{prefix}e{vertex}', font=font, fill=color, stroke_width=1, stroke_fill='#101827')
        x, y = render['panel_origins_in_card_pixels'][panel]
        canvas.paste(view, (x, y))
        ImageDraw.Draw(canvas).text((x, 12), title[panel], fill='white', font=font)
    draw = ImageDraw.Draw(canvas)
    legend = ' | '.join(f"c{i} {child['key']}"+(' RESIDUAL' if child['role'] == 'unresolved-residual' else '') for i, child in enumerate(children))
    draw.text((10, height+54), legend, font=font, fill='white')
    draw.text((10, height+78), 'All source coverage retained. Internal seams/subtypes remain agent hypotheses; class, heights and visibility are unapproved.', font=font, fill='#b8c1d1')
    canvas.save(target)


def preview_split(scene_id, object_id, children, *, group_label='', notes='', uncertainty='', reviewer='codex', root=Path('.')):
    root = Path(root).resolve()
    binding, model, parent = _current(root, scene_id, object_id, object_scope=True)
    normalized = _children(children, binding, model, parent)
    geometry = partition_outlines(binding['outline_pixels'], [child['outline_pixels'] for child in normalized])
    group_label = _text(group_label, 150, 'Shared flat group label')
    notes = _text(notes, 2000, 'Split notes')
    uncertainty = _text(uncertainty, 2000, 'Remaining split uncertainty')
    reviewer = _text(reviewer, 120, 'Agent reviewer')
    if not reviewer:
        raise ValueError('Supply the agent reviewer name.')
    folder = root/'data/scenes'/scene_id
    review_path = folder/'review.json'
    review = json.loads(review_path.read_bytes()) if review_path.exists() else None
    current_objects = (review or model)['objects']
    attached = [obj['id'] for obj in current_objects if obj.get('parent_id') == object_id and obj.get('status') not in ('rejected', 'superseded')]
    protection = _protection(parent)
    if attached:
        protection.update(can_apply=False, apply_blocked_reason='This object has active physically attached children. Resolve their parent assignments in the scene editor before splitting.')
    if any(obj['id'] in {child['object_id'] for child in normalized} for obj in current_objects):
        protection.update(can_apply=False, apply_blocked_reason='A stable child identity already exists. Preserve it and choose fresh child keys or inspect its lineage.')
    group = {'id': 'splitgroup_'+_hash(_identity_bytes([scene_id, object_id, binding['model_signature'], group_label]))[:32], 'label': group_label} if group_label else None
    render = _inspection_layout(binding['outline_pixels'], None)
    directory = root/DIRECTORY
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ValueError('Use original split preview storage.')
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix='.png') as temp:
        with Image.open(folder/f"{binding['image_sha256']}.png") as image:
            _render(image.convert('RGB'), binding['outline_pixels'], normalized, render, temp.name)
        card_bytes = Path(temp.name).read_bytes()
    content = {'version': VERSION, 'binding': binding, 'object_kind': parent['kind'], 'children': normalized,
        'group': group, 'inherited_group_id': parent.get('group_id'), 'notes': notes, 'uncertainty': uncertainty, 'reviewer': reviewer,
        'coverage': geometry, 'render': render, 'card_sha256': _hash(card_bytes), 'renderer_sha256': _hash(Path(__file__).read_bytes()),
        'source_frame': {key: model[key] for key in ('image_width', 'image_height', 'pixel_to_meters', 'meters_to_pixel')},
        'coordinate_space': 'source_image_pixels', 'axes': ['right', 'down'], 'attached_object_ids': attached,
        **protection, 'scope': SCOPE, 'tier': 3, 'training_eligible': False, 'human_approval': False}
    preview_id = _hash(_bytes(content))
    target = _folder(root, preview_id)
    target.mkdir(parents=True, exist_ok=True)
    manifest, card = target/'manifest.json', target/'card.png'
    if manifest.is_symlink() or card.is_symlink():
        raise ValueError('Use original split artifacts.')
    if manifest.exists():
        if read_preview(preview_id, root=root) != content:
            raise ValueError('Split preview artifacts were altered.')
    else:
        with card.open('xb') as handle:
            handle.write(card_bytes)
        with manifest.open('xb') as handle:
            handle.write(_bytes({'preview_id': preview_id, 'content': content, 'created_at': datetime.now(UTC).isoformat()}))
    if _current(root, scene_id, object_id, object_scope=True)[0] != binding:
        raise ValueError('The object or source changed during split preview. Request a fresh preview.')
    return {'preview_id': preview_id, **content, 'paths': {'card': str(card), 'manifest': str(manifest)}, 'editor_url': editor_url(scene_id, object_id)}


def inspect_split(preview_id, child_key=None, edge_index=None, *, root=Path('.')):
    root = Path(root).resolve()
    content = read_preview(preview_id, root=root)
    binding = content['binding']
    if _current(root, binding['scene_id'], binding['object_id'], object_scope=True)[0] != binding:
        raise ValueError('The source or saved object changed. Inspect a fresh split preview.')
    if child_key is None:
        if edge_index is not None:
            raise ValueError('Select a child_key before selecting its edge_index.')
        return {'inspection_id': preview_id, 'preview_id': preview_id, **content, 'outline_role': 'proposed_partition',
            'paths': {'card': str(_folder(root, preview_id)/'card.png')}, 'editor_url': editor_url(binding['scene_id'], binding['object_id'])}
    matches = [child for child in content['children'] if child['key'] == child_key]
    if len(matches) != 1:
        raise ValueError('Use one child_key from this immutable split preview.')
    child = matches[0]
    render = _inspection_layout(child['outline_pixels'], edge_index)
    render['outline_role'] = 'proposed'
    data = {'version': 'object-split-child-inspection-v1', 'binding': binding, 'preview_id': preview_id, 'child': child,
        'outline_pixels': child['outline_pixels'], 'outline_role': 'proposed_split_child', 'render': render,
        'can_apply': content['can_apply'], 'source_frame': content['source_frame'], 'tier': 3,
        'training_eligible': False, 'human_approval': False, 'renderer_sha256': _hash(Path(__file__).read_bytes())}
    inspection_id = _hash(_bytes(data))
    target = _folder(root, preview_id)/'inspections'/inspection_id
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError('Use original split inspection artifacts.')
    target.mkdir(parents=True, exist_ok=True)
    manifest, card = target/'manifest.json', target/'card.png'
    if manifest.is_symlink() or card.is_symlink():
        raise ValueError('Use original split inspection artifacts.')
    if manifest.exists():
        saved = json.loads(manifest.read_bytes())
        if saved.get('content') != data or saved.get('card_sha256') != _hash(card.read_bytes()):
            raise ValueError('Split child inspection artifacts were altered.')
    else:
        with Image.open(root/'data/scenes'/binding['scene_id']/f"{binding['image_sha256']}.png") as image:
            _render_inspection(image.convert('RGB'), child['outline_pixels'], render, card)
        with manifest.open('xb') as handle:
            handle.write(_bytes({'inspection_id': inspection_id, 'content': data, 'card_sha256': _hash(card.read_bytes())}))
    if _current(root, binding['scene_id'], binding['object_id'], object_scope=True)[0] != binding or read_preview(preview_id, root=root) != content:
        raise ValueError('The split or source changed during inspection. Request a fresh preview.')
    return {'inspection_id': inspection_id, **data, 'paths': {'card': str(card), 'manifest': str(manifest)}}


def verify_for_api(preview_id, *, root=Path('.')):
    """Pure local proof inside the canonical API lock; never calls back into that API."""
    root = Path(root).resolve()
    content = read_preview(preview_id, root=root)
    binding = content['binding']
    if content.get('can_apply') is not True:
        raise ValueError(content.get('apply_blocked_reason') or 'This split preview cannot be applied.')
    identity = _identity(root, binding['scene_id'], binding['object_id'], require_building=False)
    if any(binding.get(key) != value for key, value in identity.items()):
        raise ValueError('The source changed. Request and inspect a fresh split preview.')
    folder = root/'data/scenes'/binding['scene_id']
    review_path = folder/'review.json'
    if review_path.is_symlink():
        raise ValueError('Use the original saved scene review.')
    raw = review_path.read_bytes() if review_path.exists() else None
    if (_hash(raw) if raw is not None else None) != binding['review_sha256']:
        raise ValueError('The saved revision changed. Request and inspect a fresh split preview.')
    model = json.loads((folder/'latest.json').read_bytes())
    review = json.loads(raw) if raw is not None else None
    parent = next(obj for obj in (review or model)['objects'] if obj['id'] == binding['object_id'])
    children = _children([{key: value for key, value in child.items() if key != 'object_id'} for child in content['children']], binding, model, parent)
    if children != content['children'] or partition_outlines(binding['outline_pixels'], [child['outline_pixels'] for child in children]) != content['coverage']:
        raise ValueError('The split identities or source coverage were altered.')
    return {'preview_id': preview_id, 'content': content}


def apply_split(preview_id, *, root=Path('.')):
    content = read_preview(preview_id, root=root)
    binding = content['binding']
    current = _current(Path(root).resolve(), binding['scene_id'], binding['object_id'], object_scope=True)[0]
    if current != binding:
        raise ValueError('The source or object revision changed. Request and inspect a fresh split preview.')
    verify_for_api(preview_id, root=root)
    from .scene_line_feedback import apply_object_split

    result = apply_object_split(preview_id=preview_id, scene_id=binding['scene_id'], object_id=binding['object_id'],
        expected_revision=binding['revision'], model_signature=binding['model_signature'], image_sha256=binding['image_sha256'],
        expected_child_ids=[child['object_id'] for child in content['children']], expected_kind=binding['object_kind'])
    return {**result, 'scope': SCOPE}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['verify'])
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    try:
        value = json.load(sys.stdin)
        if not isinstance(value, dict) or set(value) != {'preview_id'}:
            raise ValueError('Use only a source-bound split preview_id.')
        print(json.dumps(verify_for_api(value['preview_id'], root=args.root), allow_nan=False))
    except (ValueError, TypeError, KeyError, OSError, StopIteration) as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(2) from error


if __name__ == '__main__':
    main()
