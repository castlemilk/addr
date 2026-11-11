"""The offline case tools: production tool contracts bound to one frozen case snapshot.

Inspection and preview cards are drawn by the production renderers in scene_polygon_feedback, so
a rollout sees what the live addr-review tools show. Applying records the outline in the rollout's
run directory and advances the case revision; nothing reaches a saved scene or the CMS.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Polygon

from ..scene_polygon_feedback import _inspection_layout, _outline, _render, _render_inspection

SCOPE = 'Offline harness case: applying records this outline for the case only; no scene or CMS is changed.'
DECISIONS = ('keep', 'reject', 'relabel')
REVIEW_DECISIONS = ('approved', 'rejected', 'needs_adjustment', 'uncertain')


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _text(value, limit, title):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{title}: use at most {limit} characters.')
    return value.strip()


class CaseTools:
    """State lives in `run/state.json`; every call is appended to `run/tool_calls.jsonl`."""

    def __init__(self, case_dir, run_dir):
        self.case_dir, self.run = Path(case_dir), Path(run_dir)
        self.case = json.loads((self.case_dir/'case.json').read_text())
        self.frame = self.case['source_frame']
        self.run.mkdir(parents=True, exist_ok=True)

    # -- state ---------------------------------------------------------------------------------------
    def state(self):
        path = self.run/'state.json'
        if path.is_file():
            return json.loads(path.read_text())
        start = self.case.get('start_outline_pixels')
        return {'revision': 0, 'outline_pixels': start, 'applied': [], 'finished': False}

    def _save(self, state):
        tmp = self.run/'state.json.tmp'
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, self.run/'state.json')

    def log(self, tool, arguments, result=None, error=None):
        row = {'at': datetime.now(UTC).isoformat(), 'tool': tool, 'arguments': arguments}
        row.update({'error': error} if error else {'result_keys': sorted(result or {})})
        with (self.run/'tool_calls.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, default=str) + '\n')

    def image(self):
        with Image.open(self.case_dir/self.case['image']) as image:
            return image.convert('RGB')

    # -- bindings ------------------------------------------------------------------------------------
    def _bind(self, scene_id, object_id, *, roof):
        if scene_id != self.case['scene_id']:
            raise ValueError(f'This case is scene {self.case["scene_id"]!r}; no other scene is reachable.')
        if self.case['task'] in ('outline', 'review'):
            if object_id != self.case['object_id']:
                raise ValueError(f'This case reviews object {self.case["object_id"]!r} only.')
            kind = self.case['kind']
            outline = self.state()['outline_pixels']
        else:
            detection = next((d for d in self.case['detections'] if d['object_id'] == object_id), None)
            if detection is None:
                raise ValueError('Choose one of the detections listed by inspect_detections.')
            kind, outline = detection['kind'], detection['outline_pixels']
        if roof and kind != 'building':
            raise ValueError('Choose an active building outline; use the object tools for other classes.')
        return {'case_id': self.case['case_id'], 'scene_id': scene_id, 'object_id': object_id, 'object_kind': kind,
                'image_sha256': self.case['image_sha256'], 'revision': self.state()['revision'],
                'outline_pixels': outline}

    def _check(self, value):
        return _outline(value, self.frame['image_width'], self.frame['image_height'])

    # -- outline tools -------------------------------------------------------------------------------
    def feedback(self, scene_id, object_id=None, *, roof=False):
        if object_id is None:
            object_id = self.case.get('object_id') or self.case['detections'][0]['object_id']
        binding = self._bind(scene_id, object_id, roof=roof)
        state = self.state()
        return {'scope': SCOPE, 'binding': binding, 'kind': binding['object_kind'], 'revision': binding['revision'],
                'outline_pixels': binding['outline_pixels'], 'source_frame': self.frame,
                'machine': self.case.get('machine', {}), 'applied_previews': [a['preview_id'] for a in state['applied']],
                'finished': state['finished'], 'can_apply': self.case['task'] == 'outline',
                **({'proposed_outline_pixels': self.case['proposed_outline_pixels'], 'question': self.case['question']}
                   if self.case['task'] == 'review' else {})}

    def inspect(self, scene_id, object_id, edge_index=None, preview_id=None, *, roof=False, outline_role='current'):
        binding = self._bind(scene_id, object_id, roof=roof)
        preview = self._read_preview(preview_id) if preview_id is not None else None
        if preview is not None and preview['binding'] != binding:
            raise ValueError('The polygon preview belongs to another object or an earlier revision. Request a fresh preview.')
        if outline_role not in ('current', 'proposed') or (outline_role == 'proposed' and self.case['task'] != 'review'):
            raise ValueError('outline is "current", or "proposed" for the review proposal of an assessment case.')
        source = (preview['proposed_outline_pixels'] if preview else self.case['proposed_outline_pixels']
                  if outline_role == 'proposed' else binding['outline_pixels'])
        outline, _ = self._check(source)
        render = _inspection_layout(outline, edge_index)
        render['outline_role'] = 'proposed' if preview or outline_role == 'proposed' else 'current'
        rings = [{'ring': 'outer' if i == 0 else 'hole', 'hole_index': None if i == 0 else i - 1,
                  'vertices': [{'index': j, 'source_pixels': p} for j, p in enumerate(ring)],
                  'edges': [{'index': j, 'start_vertex_index': j, 'end_vertex_index': (j + 1) % len(ring),
                             'source_pixels': [p, ring[(j + 1) % len(ring)]]} for j, p in enumerate(ring)]}
                 for i, ring in enumerate([outline['ring'], *outline['holes']])]
        content = {'binding': binding, 'outline_pixels': outline, 'outline_role': render['outline_role'],
                   'preview_id': preview_id, 'coordinate_space': 'source_image_pixels', 'axes': ['right', 'down'],
                   'rings': rings, 'source_frame': self.frame, 'render': render, 'scope': SCOPE}
        inspection_id = _hash(content)
        folder = self.run/'inspections'/inspection_id
        card = folder/'card.png'
        if not card.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            _render_inspection(self.image(), outline, render, card)
            (folder/'manifest.json').write_bytes(_bytes({'inspection_id': inspection_id, 'content': content}))
        return {'inspection_id': inspection_id, **content, 'paths': {'card': str(card)}}

    def preview(self, scene_id, object_id, proposed_outline_pixels, notes='', uncertainty='', reviewer='agent', *,
                roof=False):
        if self.case['task'] != 'outline':
            raise ValueError('Detection-review cases take decisions through submit_detection_review.')
        notes, uncertainty = _text(notes, 2000, 'Agent notes'), _text(uncertainty, 2000, 'Remaining uncertainty')
        reviewer = _text(reviewer, 120, 'Agent reviewer') or 'agent'
        binding = self._bind(scene_id, object_id, roof=roof)
        proposed, polygon = self._check(proposed_outline_pixels)
        current, previous = self._check(binding['outline_pixels'])
        if proposed == current:
            raise ValueError('The proposed polygon matches the current outline; no edit is needed.')
        content = {'binding': binding, 'proposed_outline_pixels': proposed, 'reviewer': reviewer, 'notes': notes,
                   'uncertainty': uncertainty, 'coordinate_space': 'source_image_pixels', 'axes': ['right', 'down'],
                   'source_frame': self.frame, 'scope': SCOPE, 'can_apply': True,
                   'comparison': {'current_area_pixels': previous.area, 'proposed_area_pixels': polygon.area,
                                  'intersection_over_union': previous.intersection(polygon).area/previous.union(polygon).area}}
        preview_id = _hash(content)
        folder = self.run/'previews'/preview_id
        card = folder/'card.png'
        if not card.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            content_render = _render(self.image(), current, proposed, card)
            (folder/'manifest.json').write_bytes(_bytes({'preview_id': preview_id, 'content': content,
                                                         'render': content_render}))
        return {'preview_id': preview_id, **content, 'paths': {'card': str(card)}}

    def _read_preview(self, preview_id):
        if not isinstance(preview_id, str) or len(preview_id) != 64:
            raise ValueError('Use the preview_id returned by the matching polygon preview tool.')
        manifest = self.run/'previews'/preview_id/'manifest.json'
        if not manifest.is_file():
            raise ValueError('Unknown polygon preview; request and inspect a preview first.')
        content = json.loads(manifest.read_bytes())['content']
        if _hash(content) != preview_id:
            raise ValueError('Polygon preview artifacts were altered. Request and inspect a fresh preview.')
        return content

    def apply(self, preview_id, *, roof=False):
        content = self._read_preview(preview_id)
        binding = content['binding']
        if self._bind(binding['scene_id'], binding['object_id'], roof=roof) != binding:
            raise ValueError('The case revision changed since this preview. Inspect a fresh preview before applying.')
        state = self.state()
        state['revision'] += 1
        state['outline_pixels'] = content['proposed_outline_pixels']
        state['applied'].append({'preview_id': preview_id, 'revision': state['revision'], 'notes': content['notes'],
                                 'uncertainty': content['uncertainty']})
        self._save(state)
        self._submit(state, status='applied', notes=content['notes'], uncertainty=content['uncertainty'])
        return {'scope': SCOPE, 'preview_id': preview_id, 'revision': state['revision'],
                'outline_pixels': state['outline_pixels'],
                'next': 'Reload with the feedback tool before any further edit; call finish_review when done.'}

    def finish(self, scene_id, object_id, notes='', uncertainty=''):
        if self.case['task'] != 'outline':
            raise ValueError('Detection-review cases end with submit_detection_review.')
        self._bind(scene_id, object_id, roof=False)
        notes, uncertainty = _text(notes, 4000, 'Agent notes'), _text(uncertainty, 4000, 'Remaining uncertainty')
        state = self.state()
        state['finished'] = True
        self._save(state)
        self._submit(state, status='finished', notes=notes, uncertainty=uncertainty)
        kept = not state['applied']
        return {'scope': SCOPE, 'revision': state['revision'], 'kept_start_outline': kept,
                'outline_pixels': state['outline_pixels'], 'finished': True}

    def _submit(self, state, *, status, notes, uncertainty):
        start = self.case['start_outline_pixels']
        submission = {'case_id': self.case['case_id'], 'task': 'outline', 'status': status,
                      'revision': state['revision'], 'outline_pixels': state['outline_pixels'],
                      'kept_start_outline': not state['applied'], 'notes': notes, 'uncertainty': uncertainty,
                      'applied_previews': [a['preview_id'] for a in state['applied']]}
        card, image = self.run/'submission.png', self.image()
        outline = self._check(state['outline_pixels'])[0]
        if state['outline_pixels'] == start:
            render_outline_card(image, start, card, 'Kept outline (unchanged from the machine outline)')
        else:
            _render(image, self._check(start)[0], outline, card)
        render = _inspection_layout(outline, None)
        render['outline_role'] = 'proposed'
        _render_inspection(image, outline, render, self.run/'submission-indexed.png')
        submission['card'] = card.name
        (self.run/'submission.json').write_text(json.dumps(submission, indent=2), encoding='utf-8')

    # -- assessment tools ----------------------------------------------------------------------------
    def inspect_review(self, scene_id, object_id):
        """The review item as a reviewer sees it: original, current and proposed outlines in one registered card."""
        if self.case['task'] != 'review':
            raise ValueError('Only assessment cases carry a review proposal.')
        binding = self._bind(scene_id, object_id, roof=False)
        current, previous = self._check(self.case['current_outline_pixels'])
        proposed, polygon = self._check(self.case['proposed_outline_pixels'])
        card = self.run/'inspections'/'review-item.png'
        card.parent.mkdir(parents=True, exist_ok=True)
        if not card.is_file():
            _render(self.image(), current, proposed, card)
        union = previous.union(polygon).area
        return {'scope': SCOPE, 'binding': binding, 'question': self.case['question'], 'family': self.case['family'],
                'current_outline_pixels': current, 'proposed_outline_pixels': proposed, 'source_frame': self.frame,
                'comparison': {'current_area_pixels': previous.area, 'proposed_area_pixels': polygon.area,
                               'intersection_over_union': previous.intersection(polygon).area/union if union else None},
                'paths': {'card': str(card)}}

    def submit_review(self, scene_id, object_id, decision, notes):
        if self.case['task'] != 'review':
            raise ValueError('Only assessment cases take a review decision.')
        self._bind(scene_id, object_id, roof=False)
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f'Use a decision of {", ".join(REVIEW_DECISIONS)}.')
        notes = _text(notes, 4000, 'Evidence notes')
        if not notes:
            raise ValueError('Evidence notes are required: say what in the imagery decided it.')
        self.inspect_review(scene_id, object_id)
        submission = {'case_id': self.case['case_id'], 'task': 'review', 'status': 'finished', 'decision': decision,
                      'notes': notes, 'card': 'submission.png'}
        (self.run/'submission.png').write_bytes((self.run/'inspections'/'review-item.png').read_bytes())
        (self.run/'submission.json').write_text(json.dumps(submission, indent=2), encoding='utf-8')
        return {'scope': SCOPE, 'decision': decision, 'finished': True}

    # -- detection tools -----------------------------------------------------------------------------
    def inspect_detections(self, scene_id, kind=None):
        if self.case['task'] != 'detect':
            raise ValueError('This case reviews one outline; use the boundary tools.')
        self._bind(scene_id, self.case['detections'][0]['object_id'], roof=False)
        rows = [d for d in self.case['detections'] if kind in (None, '', d['kind'])]
        card = self.run/'inspections'/f'detections-{kind or "all"}.png'
        card.parent.mkdir(parents=True, exist_ok=True)
        render_detections(self.image(), rows, card)
        return {'scope': SCOPE, 'family': self.case['family'], 'kinds': self.case['kinds'],
                'detections': [{key: d.get(key) for key in ('object_id', 'kind', 'label', 'score', 'outline_pixels')}
                               | {'index': i} for i, d in enumerate(rows)],
                'source_frame': self.frame, 'paths': {'card': str(card)}}

    def submit_detections(self, scene_id, decisions, additions=(), notes=''):
        if self.case['task'] != 'detect':
            raise ValueError('This case reviews one outline; finish it with finish_review.')
        self._bind(scene_id, self.case['detections'][0]['object_id'], roof=False)
        known = {d['object_id']: d for d in self.case['detections']}
        rows, seen = [], set()
        for row in decisions or []:
            object_id, decision = row.get('object_id'), row.get('decision')
            if object_id not in known or object_id in seen:
                raise ValueError(f'Decide each listed detection once; {object_id!r} is unknown or repeated.')
            if decision not in DECISIONS:
                raise ValueError(f'Use a decision of {", ".join(DECISIONS)}.')
            if decision == 'relabel' and row.get('kind') not in self.case['kinds']:
                raise ValueError(f'A relabel needs a kind in this family: {", ".join(self.case["kinds"])}.')
            seen.add(object_id)
            rows.append({'object_id': object_id, 'decision': decision, 'kind': row.get('kind') or known[object_id]['kind'],
                         'note': _text(row.get('note', ''), 500, 'Decision note')})
        missing = sorted(set(known) - seen)
        if missing:
            raise ValueError(f'Decide every listed detection; missing {missing[:6]}.')
        added = []
        for row in additions or []:
            if row.get('kind') not in self.case['kinds']:
                raise ValueError(f'An added object needs a kind in this family: {", ".join(self.case["kinds"])}.')
            outline, _ = self._check(row.get('outline_pixels'))
            added.append({'kind': row['kind'], 'outline_pixels': outline, 'note': _text(row.get('note', ''), 500, 'Note')})
        submission = {'case_id': self.case['case_id'], 'task': 'detect', 'status': 'finished', 'decisions': rows,
                      'additions': added, 'notes': _text(notes, 4000, 'Agent notes'), 'card': 'submission.png'}
        render_detections(self.image(), [{**known[r['object_id']], 'decision': r['decision']} for r in rows],
                          self.run/'submission.png', additions=added)
        (self.run/'submission.json').write_text(json.dumps(submission, indent=2), encoding='utf-8')
        return {'scope': SCOPE, 'decided': len(rows), 'added': len(added), 'finished': True}


def _crop_box(rings, width, height, minimum=48):
    points = [p for ring in rings for p in ring] or [[0, 0], [width, height]]
    left, top = min(p[0] for p in points), min(p[1] for p in points)
    right, bottom = max(p[0] for p in points), max(p[1] for p in points)
    span = max(right - left, bottom - top, minimum)*1.2
    cx, cy = (left + right)/2, (top + bottom)/2
    return [math.floor(cx - span/2), math.floor(cy - span/2), math.ceil(cx + span/2), math.ceil(cy + span/2)]


def render_outline_card(image, outline, target, title):
    box = _crop_box([outline['ring']], *image.size)
    size = 640
    sx = size/(box[2] - box[0])
    tile = image.crop(box).resize((size, size), Image.Resampling.NEAREST)
    canvas = Image.new('RGB', (2*size + 30, size + 50), '#101827')
    draw, font = ImageDraw.Draw(canvas), ImageFont.load_default(size=15)
    for index, label in enumerate(('Original RGB', title)):
        view = tile.copy()
        if index:
            overlay = ImageDraw.Draw(view)
            for ring in [outline['ring'], *outline['holes']]:
                xy = [((p[0] - box[0])*sx, (p[1] - box[1])*sx) for p in [*ring, ring[0]]]
                overlay.line(xy, fill='#67d7e7', width=3)
        canvas.paste(view, (10 + index*(size + 10), 40))
        draw.text((10 + index*(size + 10), 12), label, fill='white', font=font)
    canvas.save(target)


def render_detections(image, detections, target, additions=()):
    """Numbered detections over the frame; decided ones are coloured keep/reject/relabel."""
    colours = {'keep': '#4ade80', 'reject': '#f87171', 'relabel': '#c084fc', None: '#fbbf24'}
    rings = [d['outline_pixels']['ring'] for d in detections] + [a['outline_pixels']['ring'] for a in additions]
    box = _crop_box(rings, *image.size, minimum=96)
    size = 900
    sx = size/(box[2] - box[0])
    view = image.crop(box).resize((size, size), Image.Resampling.NEAREST)
    overlay, font = ImageDraw.Draw(view), ImageFont.load_default(size=15)
    for index, detection in enumerate(detections):
        ring = detection['outline_pixels']['ring']
        xy = [((p[0] - box[0])*sx, (p[1] - box[1])*sx) for p in [*ring, ring[0]]]
        overlay.line(xy, fill=colours.get(detection.get('decision')), width=2)
        centre = Polygon(ring).representative_point() if len(ring) >= 3 else None
        if centre is not None:
            overlay.text(((centre.x - box[0])*sx, (centre.y - box[1])*sx), f'd{index}', font=font, fill='white',
                         stroke_width=2, stroke_fill='#101827')
    for addition in additions:
        ring = addition['outline_pixels']['ring']
        overlay.line([((p[0] - box[0])*sx, (p[1] - box[1])*sx) for p in [*ring, ring[0]]], fill='#38bdf8', width=3)
    canvas = Image.new('RGB', (size + 20, size + 50), '#101827')
    canvas.paste(view, (10, 40))
    ImageDraw.Draw(canvas).text((10, 12), 'Machine detections · d<i> = listed index · source pixels', fill='white',
                                font=font)
    canvas.save(target)
