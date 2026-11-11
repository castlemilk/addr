"""Source-pixel polygon previews and explicit, revision-bound agent draft saves.

Previews write immutable diagnostic sidecars. Applying a preview delegates to the
canonical scene API; the original image/model and human checks are never edited.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Polygon

from .scene_line_feedback import (
    load_object_feedback,
    load_roof_line_feedback,
    update_draft_object_polygon,
    update_draft_roof_feedback,
)
from .scene_roof_observations import _identity, editor_url

VERSION = 'roof-agent-polygon-preview-v1'
OBJECT_VERSION = 'object-agent-polygon-preview-v1'
DIRECTORY = Path('data/reviews/eyes/polygon-preview')
INSPECTION_VERSION = 'roof-boundary-inspection-v1'
OBJECT_INSPECTION_VERSION = 'object-boundary-inspection-v1'
INSPECTION_DIRECTORY = Path('data/reviews/eyes/boundary-inspection')
SCOPE = 'Agent-traced XY draft, tier 3; not a human approval, measured geometry or training label.'


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _text(value, limit, title):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{title}: use at most {limit} characters.')
    return value.strip()


def _outline(value, width, height):
    if not isinstance(value, dict) or set(value) != {'ring', 'holes'}:
        raise ValueError('Supply an outer ring and holes in original source pixels.')
    holes = value['holes']
    if not isinstance(holes, list) or len(holes) > 32:
        raise ValueError('Use at most 32 interior gaps.')
    rings = [value['ring'], *holes]
    if any(not isinstance(ring, list) or not 3 <= len(ring) <= 200 for ring in rings):
        raise ValueError('Use three to 200 vertices in each open ring; omit its repeated closing point.')
    if sum(map(len, rings)) > 2000:
        raise ValueError('Use at most 2,000 polygon vertices.')
    for ring in rings:
        for point in ring:
            if (not isinstance(point, list) or len(point) != 2
                    or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in point)
                    or not 0 <= point[0] <= width or not 0 <= point[1] <= height):
                raise ValueError('Use finite [x, y] points inside the original image dimensions.')
        if ring[0] == ring[-1]:
            raise ValueError('Use open rings; omit the repeated closing point.')
    polygon = Polygon(value['ring'], holes)
    if not polygon.is_valid or polygon.is_empty or polygon.area < 4:
        raise ValueError('Use a valid polygon with at least four pixels of area; gaps must not overlap or leave the outline.')
    return json.loads(_bytes(value)), polygon


def _current(root, scene_id, object_id, *, object_scope=False):
    """Bind canonical API state to the exact local model/image and saved review."""
    root = Path(root)
    identity = _identity(root, scene_id, object_id, require_building=False) if object_scope else _identity(root, scene_id, object_id)
    folder = root/'data/scenes'/scene_id
    model = json.loads((folder/'latest.json').read_bytes())
    review_path = folder/'review.json'
    if review_path.is_symlink():
        raise ValueError('Use the original saved review.')
    raw_review = review_path.read_bytes() if review_path.exists() else None
    review = json.loads(raw_review) if raw_review is not None else None
    load = load_object_feedback if object_scope else load_roof_line_feedback
    feedback = load(scene_id)
    revision = feedback.get('revision')
    if (not isinstance(revision, int) or isinstance(revision, bool) or revision < 0 or feedback.get('stale')
            or feedback.get('model_signature') != identity['model_signature']
            or feedback.get('image', {}).get('sha256') != identity['image_sha256']
            or feedback.get('image', {}).get('width') != model.get('image_width')
            or feedback.get('image', {}).get('height') != model.get('image_height')):
        raise ValueError('Canonical review state does not match the current source image/model. Reload the scene.')
    canonical_rows = [row for row in feedback.get('objects', []) if row.get('object_id') == object_id]
    if review is not None:
        if (not isinstance(review, dict) or review.get('revision') != revision
                or review.get('model_signature') != identity['model_signature']):
            raise ValueError('The saved review changed or is stale. Reload before proposing a polygon.')
        feedback = load(scene_id, object_id)
        if feedback.get('revision') != revision or feedback.get('model_signature') != identity['model_signature']:
            raise ValueError('The review changed while reading its polygon. Reload the scene.')
        rows = [row for row in feedback.get('objects', []) if row.get('object_id') == object_id]
        canonical_rows = rows
        objects = [obj for obj in review.get('objects', []) if obj.get('id') == object_id]
        if len(rows) != 1 or len(objects) != 1:
            raise ValueError('Choose one saved object in the current review.')
        obj = objects[0]
        if rows[0].get('outline_pixels') != {'ring': obj.get('ring'), 'holes': obj.get('holes', [])}:
            raise ValueError('The canonical outline changed. Reload the scene.')
    else:
        if revision != 0:
            raise ValueError('The API has a saved review that is unavailable locally. Reload before editing.')
        obj = next(obj for obj in model['objects'] if obj.get('id') == object_id)
    obj = dict(obj)
    if len(canonical_rows) == 1:
        if object_scope and canonical_rows[0].get('kind') != obj.get('kind'):
            raise ValueError('The canonical object class changed. Reload the scene.')
        obj['_automated_polygon_edit_allowed'] = canonical_rows[0].get('automated_polygon_edit_allowed')
        obj['_automated_polygon_edit_reason'] = canonical_rows[0].get('automated_polygon_edit_reason')
    if ((not object_scope and obj.get('kind') != 'building') or obj.get('status') in ('rejected', 'superseded')):
        raise ValueError('Choose an active object outline.' if object_scope else 'Choose an active building outline.')
    current_identity = _identity(root, scene_id, object_id, require_building=False) if object_scope else _identity(root, scene_id, object_id)
    if current_identity != identity or (review_path.read_bytes() if review_path.exists() else None) != raw_review:
        raise ValueError('The scene changed while loading its outline. Request a fresh preview.')
    binding = {**identity, 'revision': revision, 'review_sha256': _hash(raw_review) if raw_review is not None else None,
               'outline_pixels': {'ring': obj['ring'], 'holes': obj.get('holes', [])}}
    if object_scope:
        binding['object_kind'] = obj['kind']
    return binding, model, obj


def _layout(current, proposed):
    points = current['ring'] + proposed['ring']
    left, top = min(p[0] for p in points), min(p[1] for p in points)
    right, bottom = max(p[0] for p in points), max(p[1] for p in points)
    span = max(right-left, bottom-top, 24)*1.25
    cx, cy = (left+right)/2, (top+bottom)/2
    box = [math.floor(cx-span/2), math.floor(cy-span/2), math.ceil(cx+span/2), math.ceil(cy+span/2)]
    width, height = 480, 480
    sx, sy = width/(box[2]-box[0]), height/(box[3]-box[1])
    origins = [[10+490*i, 40] for i in range(3)]
    return {'crop_box_pixels': box, 'panel_size_pixels': [width, height], 'panel_origins_in_card_pixels': origins,
            'source_to_panel': [[sx, 0], [0, sy], [-box[0]*sx, -box[1]*sy]],
            'panel_to_source': [[1/sx, 0], [0, 1/sy], box[:2]],
            'affine_convention': '[x, y, 1] @ matrix; panel coordinates exclude title/border'}


def _protection(obj):
    protected = bool(obj.get('geometry_reviewed') or obj.get('status') == 'confirmed' or (obj.get('roof_line_edits') or {}).get('checked')
                     or obj.get('_automated_polygon_edit_allowed') is not True)
    return {'can_apply': not protected,
            'apply_blocked_reason': (obj.get('_automated_polygon_edit_reason') or 'Human-checked geometry or line feedback is preserved; edit it in the CMS.') if protected else None,
            'object_state': {'status': obj.get('status'), 'geometry_reviewed': bool(obj.get('geometry_reviewed')),
                             'roof_lines_checked': bool((obj.get('roof_line_edits') or {}).get('checked')),
                             'automated_polygon_edit_allowed': obj.get('_automated_polygon_edit_allowed') is True}}


def _inspection_layout(outline, edge_index):
    ring = outline['ring']
    if edge_index is not None:
        if isinstance(edge_index, bool) or not isinstance(edge_index, int) or not 0 <= edge_index < len(ring):
            raise ValueError('Use an outer-ring edge_index from zero through vertex count minus one.')
        points = [ring[index % len(ring)] for index in range(edge_index-1, edge_index+3)]
    else:
        points = ring
    left, top = min(p[0] for p in points), min(p[1] for p in points)
    right, bottom = max(p[0] for p in points), max(p[1] for p in points)
    margin = max(8, max(right-left, bottom-top)*0.12)
    box = [math.floor(left-margin), math.floor(top-margin), math.ceil(right+margin), math.ceil(bottom+margin)]
    source_width, source_height = box[2]-box[0], box[3]-box[1]
    scale = min(640/source_width, 640/source_height)
    width, height = max(1, round(source_width*scale)), max(1, round(source_height*scale))
    sx, sy = width/source_width, height/source_height
    # Keep fewer than roughly ten grid lines per dimension; all labels refer to source pixels.
    desired = max(source_width, source_height)/8
    magnitude = 10**math.floor(math.log10(max(1, desired)))
    step = next(value*magnitude for value in (1, 2, 5, 10) if value*magnitude >= desired)
    grid_x = list(range(math.ceil(box[0]/step)*step, box[2], step))
    grid_y = list(range(math.ceil(box[1]/step)*step, box[3], step))
    return {'crop_box_pixels': box, 'source_crop_size_pixels': [source_width, source_height],
            'panel_size_pixels': [width, height], 'panel_origins_in_card_pixels': [[10+652*i, 45] for i in range(3)],
            'source_to_panel': [[sx, 0], [0, sy], [-box[0]*sx, -box[1]*sy]],
            'panel_to_source': [[1/sx, 0], [0, 1/sy], box[:2]],
            'affine_convention': '[x, y, 1] @ matrix; panel coordinates exclude title/border',
            'display_resampling': 'nearest', 'display_scale': [sx, sy],
            'grid': {'coordinate_space': 'source_image_pixels', 'spacing_pixels': step, 'x': grid_x, 'y': grid_y},
            'selected_outer_edge_index': edge_index,
            'resolution_note': 'Original RGB only. Display magnification adds no source detail; black is outside the source image.'}


def _render_inspection(image, outline, render, target):
    box = render['crop_box_pixels']
    width, height = render['panel_size_pixels']
    sx, sy = render['display_scale']
    tile = image.crop(box).resize((width, height), Image.Resampling.NEAREST)
    canvas = Image.new('RGB', (1966, height+83), '#101827')
    draw, font = ImageDraw.Draw(canvas), ImageFont.load_default(size=15)
    proposed = render.get('outline_role') == 'proposed'
    outline_title = 'Proposed outline · unchecked · vertex / edge indices' if proposed else 'Current outline · vertex / edge indices'
    outline_colour = '#fbbf24' if proposed else '#67d7e7'
    for panel, title in enumerate(('Original RGB · original detail', 'RGB · grid labels in source pixels', outline_title)):
        view = tile.copy()
        overlay = ImageDraw.Draw(view)
        if panel == 1:
            for x in render['grid']['x']:
                px = (x-box[0])*sx
                overlay.line([(px, 0), (px, height)], fill='#81929a', width=1)
                overlay.text((px+2, 2), f'x={x}', font=font, fill='white', stroke_width=1, stroke_fill='#101827')
            for y in render['grid']['y']:
                py = (y-box[1])*sy
                overlay.line([(0, py), (width, py)], fill='#81929a', width=1)
                overlay.text((2, py+2), f'y={y}', font=font, fill='white', stroke_width=1, stroke_fill='#101827')
        if panel == 2:
            for ring_index, ring in enumerate([outline['ring'], *outline['holes']]):
                xy = [((p[0]-box[0])*sx, (p[1]-box[1])*sy) for p in ring]
                overlay.line([*xy, xy[0]], fill=outline_colour, width=2)
                for index, point in enumerate(xy):
                    following = xy[(index+1) % len(xy)]
                    prefix = '' if ring_index == 0 else f'h{ring_index-1}:'
                    selected = ring_index == 0 and index == render['selected_outer_edge_index']
                    if selected:
                        overlay.line([point, following], fill='#fb7185' if proposed else '#fbbf24', width=4)
                    overlay.ellipse((point[0]-3, point[1]-3, point[0]+3, point[1]+3), fill=outline_colour)
                    overlay.text((point[0]+4, point[1]+3), f'{prefix}v{index}', font=font, fill='white', stroke_width=1, stroke_fill='#101827')
                    midpoint = ((point[0]+following[0])/2, (point[1]+following[1])/2)
                    overlay.text(midpoint, f'{prefix}e{index}', font=font, fill='#fb7185' if selected and proposed else '#fbbf24' if selected else outline_colour, stroke_width=1, stroke_fill='#101827')
        x, y = render['panel_origins_in_card_pixels'][panel]
        canvas.paste(view, (x, y))
        draw.text((x, 12), title, fill='white', font=font)
    draw.text((10, height+57), render['resolution_note'], fill='#b8c1d1', font=font)
    canvas.save(target)


def inspect_boundary(scene_id, object_id, edge_index=None, preview_id=None, *, root=Path('.'), object_scope=False):
    """Inspect saved or source-bound proposed edges without mutating scene state."""
    root = Path(root).resolve()
    binding, model, obj = _current(root, scene_id, object_id, object_scope=object_scope)
    preview = _read_preview(root, preview_id) if preview_id is not None else None
    if preview is not None and preview.get('binding') != binding:
        raise ValueError('The polygon preview belongs to another object or its source/revision changed. Request a fresh preview.')
    outline_role = 'proposed' if preview is not None else 'current'
    outline, _ = _outline(preview['proposed_outline_pixels'] if preview is not None else binding['outline_pixels'],
                          model['image_width'], model['image_height'])
    render = _inspection_layout(outline, edge_index)
    render['outline_role'] = outline_role
    protection = _protection(obj)
    if preview is not None and preview.get('can_apply') is not True:
        protection.update(can_apply=False, apply_blocked_reason=preview.get('apply_blocked_reason') or 'This polygon preview cannot be applied.')
    rings = []
    for ring_index, ring in enumerate([outline['ring'], *outline['holes']]):
        rings.append({'ring': 'outer' if ring_index == 0 else 'hole', 'hole_index': None if ring_index == 0 else ring_index-1,
                      'vertices': [{'index': i, 'source_pixels': point} for i, point in enumerate(ring)],
                      'edges': [{'index': i, 'start_vertex_index': i, 'end_vertex_index': (i+1) % len(ring),
                                 'source_pixels': [point, ring[(i+1) % len(ring)]]} for i, point in enumerate(ring)]})
    content = {'version': OBJECT_INSPECTION_VERSION if object_scope else INSPECTION_VERSION, 'binding': binding, 'outline_pixels': outline,
               'outline_role': outline_role, 'preview_id': preview_id,
               'coordinate_space': 'source_image_pixels', 'axes': ['right', 'down'], 'rings': rings,
               'source_frame': {key: model[key] for key in ('image_width', 'image_height', 'pixel_to_meters', 'meters_to_pixel')},
               **protection, 'render': render, 'scope': SCOPE, 'tier': 3,
               'training_eligible': False, 'human_approval': False, 'renderer_sha256': _hash(Path(__file__).read_bytes())}
    if object_scope:
        content['object_kind'] = obj['kind']
    inspection_id = _hash(_bytes(content))
    directory = root/INSPECTION_DIRECTORY/inspection_id
    if any(path.is_symlink() for path in (directory, directory.parent, directory.parent.parent)):
        raise ValueError('Use original boundary inspection sidecars.')
    directory.mkdir(parents=True, exist_ok=True)
    card, manifest = directory/'card.png', directory/'manifest.json'
    if card.is_symlink() or manifest.is_symlink():
        raise ValueError('Use original boundary inspection artifacts.')
    if manifest.exists():
        saved = json.loads(manifest.read_bytes())
        if saved.get('content') != content or saved.get('card_sha256') != _hash(card.read_bytes()):
            raise ValueError('Boundary inspection artifacts were altered. Request a fresh inspection.')
    else:
        source_image = root/'data/scenes'/scene_id/f"{binding['image_sha256']}.png"
        with Image.open(source_image) as image:
            _render_inspection(image.convert('RGB'), outline, render, card)
        with manifest.open('xb') as handle:
            handle.write(_bytes({'inspection_id': inspection_id, 'content': content, 'card_sha256': _hash(card.read_bytes()),
                                 'created_at': datetime.now(UTC).isoformat()}))
    if _current(root, scene_id, object_id, object_scope=object_scope)[0] != binding:
        raise ValueError('The scene changed during inspection. Reload its current boundary.')
    if preview_id is not None and _read_preview(root, preview_id) != preview:
        raise ValueError('The polygon preview changed during inspection. Request a fresh preview.')
    return {'inspection_id': inspection_id, **content, 'paths': {'card': str(card), 'manifest': str(manifest)},
            'editor_url': editor_url(scene_id, object_id)}


def _render(image, current, proposed, target):
    """One registered crop; display scaling never changes source coordinates."""
    render = _layout(current, proposed)
    box = render['crop_box_pixels']
    width, height = render['panel_size_pixels']
    sx, sy = render['source_to_panel'][0][0], render['source_to_panel'][1][1]
    canvas = Image.new('RGB', (1480, 540), '#101827')
    draw, font = ImageDraw.Draw(canvas), ImageFont.load_default(size=16)
    origins = render['panel_origins_in_card_pixels']
    for index, title in enumerate(('Original RGB', 'Current saved outline', 'Agent polygon proposal · unchecked')):
        tile = image.crop(box).resize((width, height))
        if index:
            outline = current if index == 1 else proposed
            overlay = ImageDraw.Draw(tile)
            for ring_index, ring in enumerate([outline['ring'], *outline['holes']]):
                xy = [((p[0]-box[0])*sx, (p[1]-box[1])*sy) for p in [*ring, ring[0]]]
                overlay.line(xy, fill='#67d7e7' if index == 1 else '#fbbf24', width=3)
                if ring_index:
                    overlay.text(xy[0], 'gap', fill='white', font=font)
        x, y = origins[index]
        canvas.paste(tile, (x, y))
        draw.text((x, 12), title, fill='white', font=font)
    draw.text((10, 523), 'Source pixels retained. Hidden boundaries, heights and class geometry remain uncertain. Black = outside source.', fill='#b8c1d1', font=font)
    canvas.save(target)
    return render


def _folder(root, preview_id):
    if not isinstance(preview_id, str) or not re.fullmatch(r'[a-f0-9]{64}', preview_id):
        raise ValueError('Use the polygon preview id returned by the matching polygon preview tool.')
    directory = Path(root).resolve()/DIRECTORY/preview_id
    if any(path.is_symlink() for path in (directory, directory.parent, directory.parent.parent)):
        raise ValueError('Use original polygon preview sidecars.')
    return directory


def _read_preview(root, preview_id):
    """Verify immutable proposal content, registered render and card together."""
    directory = _folder(root, preview_id)
    manifest, card = directory/'manifest.json', directory/'card.png'
    if manifest.is_symlink() or card.is_symlink():
        raise ValueError('Use original polygon preview artifacts.')
    saved = json.loads(manifest.read_bytes())
    content = saved.get('content')
    if (not isinstance(content, dict) or saved.get('preview_id') != preview_id or _hash(_bytes(content)) != preview_id
            or content.get('version') not in (VERSION, OBJECT_VERSION) or saved.get('render') != content.get('render')
            or saved.get('card_sha256') != _hash(card.read_bytes())):
        raise ValueError('Polygon preview artifacts were altered. Request and inspect a fresh preview.')
    return content


def preview_polygon(scene_id, object_id, proposed_outline_pixels, *, notes='', uncertainty='', reviewer='codex', root=Path('.'), object_scope=False):
    """Preview a supplied XY trace without saving review state or changing sources."""
    root = Path(root).resolve()
    notes = _text(notes, 2000, 'Agent notes')
    uncertainty = _text(uncertainty, 2000, 'Remaining uncertainty')
    reviewer = _text(reviewer, 120, 'Agent reviewer')
    if not reviewer:
        raise ValueError('Supply the agent reviewer name.')
    binding, model, obj = _current(root, scene_id, object_id, object_scope=object_scope)
    proposed, polygon = _outline(proposed_outline_pixels, model['image_width'], model['image_height'])
    current, previous = _outline(binding['outline_pixels'], model['image_width'], model['image_height'])
    if proposed == current:
        raise ValueError('The proposed polygon matches the current outline; no edit is needed.')
    protected = bool(obj.get('geometry_reviewed') or obj.get('status') == 'confirmed' or (obj.get('roof_line_edits') or {}).get('checked')
                     or obj.get('_automated_polygon_edit_allowed') is not True)
    content = {'version': OBJECT_VERSION if object_scope else VERSION, 'binding': binding, 'proposed_outline_pixels': proposed, 'reviewer': reviewer,
               'notes': notes, 'uncertainty': uncertainty, 'renderer_sha256': _hash(Path(__file__).read_bytes()),
               'coordinate_space': 'source_image_pixels', 'axes': ['right', 'down'],
               'source_frame': {key: model[key] for key in ('image_width', 'image_height', 'pixel_to_meters', 'meters_to_pixel')},
               'scope': SCOPE, 'tier': 3, 'training_eligible': False, 'human_approval': False,
               'can_apply': not protected, 'apply_blocked_reason': (obj.get('_automated_polygon_edit_reason') or 'Human-checked geometry or line feedback is preserved; edit it in the CMS.') if protected else None,
               'render': _layout(current, proposed),
               'comparison': {'current_area_pixels': previous.area, 'proposed_area_pixels': polygon.area,
                              'intersection_over_union': previous.intersection(polygon).area/previous.union(polygon).area}}
    if object_scope:
        content['object_kind'] = obj['kind']
    preview_id = _hash(_bytes(content))
    directory = _folder(root, preview_id)
    directory.mkdir(parents=True, exist_ok=True)
    card, manifest = directory/'card.png', directory/'manifest.json'
    if card.is_symlink() or manifest.is_symlink():
        raise ValueError('Use original polygon preview artifacts.')
    if manifest.exists():
        saved = json.loads(manifest.read_bytes())
        if saved.get('content') != content or saved.get('render') != content['render'] or saved.get('card_sha256') != _hash(card.read_bytes()):
            raise ValueError('Polygon preview artifacts were altered. Request a fresh preview.')
    else:
        source_image = Path(root)/'data/scenes'/scene_id/f"{binding['image_sha256']}.png"
        with Image.open(source_image) as image:
            render = _render(image.convert('RGB'), current, proposed, card)
        saved = {'preview_id': preview_id, 'content': content, 'render': render, 'card_sha256': _hash(card.read_bytes()),
                 'created_at': datetime.now(UTC).isoformat()}
        with manifest.open('xb') as handle:
            handle.write(_bytes(saved))
    if _current(root, scene_id, object_id, object_scope=object_scope)[0] != binding:
        raise ValueError('The scene changed during preview. Request a fresh polygon preview.')
    return {'preview_id': preview_id, **content, 'render': saved['render'],
            'paths': {'card': str(card), 'manifest': str(manifest)}, 'editor_url': editor_url(scene_id, object_id)}


def apply_polygon(preview_id, *, root=Path('.'), object_scope=False):
    """Apply exactly one verified preview through the canonical draft transaction."""
    content = _read_preview(root, preview_id)
    if content.get('version') != (OBJECT_VERSION if object_scope else VERSION):
        raise ValueError('Use the matching object or roof apply tool for this polygon preview.')
    if content.get('can_apply') is not True:
        raise ValueError(content.get('apply_blocked_reason') or 'This polygon preview cannot be applied.')
    binding = content['binding']
    current, model, obj = _current(root, binding['scene_id'], binding['object_id'], object_scope=object_scope)
    if current != binding:
        raise ValueError('The scene, source image or saved revision changed. Inspect a fresh preview before applying.')
    if (obj.get('geometry_reviewed') or obj.get('status') == 'confirmed' or (obj.get('roof_line_edits') or {}).get('checked')
            or obj.get('_automated_polygon_edit_allowed') is not True):
        raise ValueError('Human-checked geometry or line feedback is preserved. Use the CMS for this edit.')
    proposed, _ = _outline(content['proposed_outline_pixels'], model['image_width'], model['image_height'])
    update = update_draft_object_polygon if object_scope else update_draft_roof_feedback
    result = update(scene_id=binding['scene_id'], object_id=binding['object_id'],
        expected_revision=binding['revision'], model_signature=binding['model_signature'], image_sha256=binding['image_sha256'],
        outline_pixels=binding['outline_pixels'], changes={'outline_pixels': proposed, 'polygon_preview_id': preview_id,
                                                         **({'notes': content['notes']} if content['notes'] else {})})
    return {**result, 'preview_id': preview_id, 'scope': SCOPE}
