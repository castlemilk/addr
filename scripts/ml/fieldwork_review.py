"""Fieldwork review queue for the 8 Oct 2026 review: stage, push, pull and status.

The reviewer asked (chat, 8 Oct ~21:15 AEDT) to review the 8 Oct proposals in the Fieldwork CMS instead of the static
pages: tree-shadow trims, cut fixes, vegetation types, building joins and the three round-5 choices. This module puts
them in a separate Fieldwork project (default ``mooroolbark-review``) built only from item types the deployed UI shows,
and reads the reviewer's clicks back into answer lines and decision files. Design:
.run/session-2026-10-08-fieldwork-review/design/DESIGN.md (plus the design-skeptic fixes noted below).

- ``stage`` (local, read-only on data/): re-checks every proposal against the live scene files, crops the scene image
  around each shadow / cut / join line, writes a small review frame JSON per item and a frozen bundle under
  .run/fieldwork-review/<set>/<stageSha12>/.
- ``push`` (the only writer): uploads the bundle's assets, then creates the project, labels and items with
  expectedRevision 0. Only a plain ASCII http(s) origin is accepted (guard_origin; the live check compares the host urllib
  would connect to). Rerun-safe: an item with a receipt is skipped; a 409 is compared with the server copy and is
  either ``skip_identical`` or ``conflict_id_reuse`` (stopped, never overwritten). Never deletes, never saves an existing
  item, refuses project ``mooroolbark`` (and any project not ending in -review) and refuses ``addr-scene-`` ids.
  The live origin needs FIELDWORK_PUSH_ALLOW_LIVE=1.
- ``pull`` (read-only on Fieldwork): lists the project's items, diffs each against its push receipt and writes, under
  --out only: decisions, chat answer lines (checked by this module's own grammar check, not by answer_check.py, which
  binds only the page's suggested answers), preview requests for outlines marked right, the vegetation-type file
  record-chat-feedback.mjs takes, and the joins / round-5 decisions. Every human decision is ``human:fieldwork`` and carries the
  change actor(s). On a shadow / cut / join item the item-level Review > Status counts too: Approved = right, Rejected = wrong,
  unless the PROPOSED (JOINED) outline says otherwise; then nothing is recorded and the item is listed under ``conflicts``. On
  a vegetation item the item status alone stores nothing and is listed under ``needs_words``; Rejected next to typed outlines
  is a listed conflict and stores none of their types. The decision bar's writes are answers: Right = confirmed +
  geometry_reviewed true, Partly = needs_adjustment + false, Wrong = rejected + false on the PROPOSED / JOINED (or a
  vegetation) outline. A vegetation type is a Label word or the type picker's vegetation_type override (same words; both set
  and different = a listed conflict); on a shadow / cut / join outline it is listed under ``not_stored``. The Inspector
  controls that are not words (Review issues, Visibility, and 'Geometry reviewed in this layer' other than the bar's pair, or
  on any other outline) make a conflict (no decision) next to confirmed / Approved / a typed outline, and on their own are
  listed under ``unread`` with what changed. Every changed item or outline nothing could be read from is listed under
  ``unread`` (or ``exceptions``). It never writes data/scenes, review.json or Fieldwork.
- ``status`` (no network): receipts per family and the last pull's counts.

Agents append independent reviews through SaveAgentReview (save_agent_review over MCP), with a server-stamped
agent:fieldwork:<reviewer> change actor and item.agentReviews history. These assessments never alter the human
status or annotation overrides. Pull exports agent assessments separately and excludes agent events from human
attribution. Ordinary agent edits to human review fields in a -review project remain refused by the clients and
MCP guards; push still creates proposals only.

Design-skeptic fixes built in: a confirm on a whole-roof cut copy (lines 66-116) is held, never turned into
``cuts: whole roofs too`` and never previewed (the card needs a shape check and that separate word); several cut lines
marked right on one outline are held for one composed preview (2 and 3 resolve to 2, as the card says); a confirm on a
"tree (or shrub)" or "no type proposed" outline stores nothing until a type word is typed in the Label.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import fcntl
import hashlib
import io
import json
import math
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from scripts.ml.fieldwork_feedback import (
    DEFAULT_REMOTE_URL,
    DEFAULT_TOKEN_FILE,
    is_live_origin,
    is_loopback,
)

TOOL = 'fieldwork_review/1'
SET = 'review-2026-10-08'
DEFAULT_PROJECT = 'mooroolbark-review'
SESSION = Path('.run/session-2026-10-08-fieldwork-review')
DEFAULT_ITEMS = SESSION / 'understand/items.json'
REVIEW_DIR = Path('data/reviews/eyes/experiments/review-2026-10-08')
DEFAULT_CONTEXT = REVIEW_DIR / 'answer-context.json'
DEFAULT_PULL_OUT = REVIEW_DIR / 'fieldwork'
DEFAULT_STAGE_ROOT = Path('.run/fieldwork-review') / SET
FAMILIES = ('shadow', 'cut', 'veg', 'join', 'round5', 'corner', 'unit', 'lot', 'split', 'trace')
FAMILY_TAG = {'shadow': 'shadows', 'cut': 'cuts', 'veg': 'veg', 'join': 'joins', 'round5': 'round5', 'corner': 'corners', 'unit': 'units', 'lot': 'lots', 'split': 'splits', 'trace': 'traces'}
UNITS_TAG = 'units-2026-10-12'  # units card 2026-10-12 item 2: split proposals for outlines spanning several Vicmap lots
CORNER_TAG = 'corners-2026-10-10'  # roof corners card 2026-10-10 item 2: right-angle redraws of confirmed roofs
VEG_TYPES = ('grass', 'hedge', 'shrub', 'tree')
VEG_GROUPS = ('grass', 'hedge', 'shrub', 'tree', 'tree-or-shrub', 'unclear')
VEG_GROUP_NAMES = {'grass': 'Proposed grass', 'hedge': 'Proposed hedge', 'shrub': 'Proposed shrub', 'tree': 'Proposed tree',
                   'tree-or-shrub': 'Proposed tree (or shrub)', 'unclear': 'No type proposed'}
ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
MAX_DOCUMENT_BYTES = 2 << 20
SCENE_KINDS = ('building', 'tree', 'road', 'car', 'solar_panel', 'skylight', 'chimney', 'pool', 'driveway', 'footpath',
               'vegetation', 'water', 'other')
CROP_MIN, CROP_MAX, CROP_PAD_M = 320, 1024, 10.0
ROUND5_CENTER = [145.313, -37.783]

PROJECT = {'name': 'Mooroolbark review queue',
           'description': 'Agent proposals from the 8 Oct 2026 review for the reviewer to mark. Nothing here is training '
                          'data or a scene; decisions are read back by scripts/ml/fieldwork_review.py pull.'}


def _label(suffix, name, color):
    return {'suffix': suffix, 'name': name, 'color': color}


LABELS = [
    _label('current', 'Current (saved outline)', '#94A3B8'),
    _label('proposed', 'Proposed', '#22D3EE'),
    _label('join-piece', 'Join piece', '#94A3B8'),
    _label('joined', 'Joined outline', '#22D3EE'),
    _label('unit', 'Unit (Vicmap lot)', '#F59E0B'),
    _label('lot', 'Lot boundary (Vicmap)', '#EF4444'),
    _label('context', 'Neighbouring saved building', '#64748B'),
    _label('veg-grass', 'Proposed grass', '#84CC16'),
    _label('veg-hedge', 'Proposed hedge', '#15803D'),
    _label('veg-shrub', 'Proposed shrub', '#65A30D'),
    _label('veg-tree', 'Proposed tree', '#166534'),
    _label('veg-tree-or-shrub', 'Proposed tree (or shrub)', '#4D7C0F'),
    _label('veg-unclear', 'No type proposed', '#A8A29E'),
]

# Round 5 (INDEX.html part 3): Approved = the default, Rejected = the alternative.
ROUND5 = [
    {'n': 1, 'question': 'Keep the new fold as the round-5 candidate (deploys nothing; it is the checkpoint later rounds are compared against)?',
     'approved': 'keep the new fold', 'rejected': 'stay on round 4',
     'context': 'The new fold trained overnight on two seeds. It is no worse than round 4 on either seed and slightly better on one '
                '(+0.022, clearing zero). On your 31 confirmed roofs and 9 rejected objects it matches round 4. The 16 px floor '
                'made no measurable difference.'},
    {'n': 2, 'question': 'Prepare a short list of small objects on training tiles for you to reject in the editor?',
     'approved': 'make the list', 'rejected': 'no list',
     'context': 'The list drops tile-edge fragments and is judged on the imagery. These would be the first hard negatives the '
                'model can learn from; all 9 of your rejections sit on test tiles. Nothing trains until you have rejected them.'},
    {'n': 3, 'question': 'Skip the optional pair run for now?',
     'approved': 'skip the pair', 'rejected': 'run the pair',
     'context': 'The pair separates the floor change from the keep-set change, and the floor showed no effect. Running it is '
                'about 9 hours of training when memory allows.'},
]


class ReviewError(Exception):
    """A refusal or failure the CLI reports without a traceback."""


# ----------------------------------------------------------------------------------------------------------------- utils

def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def ring_sha(ring, holes=None):
    """Same as scripts/ml/experiments/{shadow_trim,cut_fix}.py: sha256 of compact json [ring, holes], first 16 hex."""
    return hashlib.sha256(json.dumps([ring, holes or []], separators=(',', ':')).encode()).hexdigest()[:16]


def now_iso():
    return _dt.datetime.now(_dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def run_id():
    return _dt.datetime.now(_dt.UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + os.urandom(4).hex()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + '\n')
    tmp.replace(path)


def expand_lines(text):
    out = set()
    for token in re.split(r'[,\s]+', text.strip()):
        if not token:
            continue
        match = re.fullmatch(r'(\d+)-(\d+)', token)
        if match:
            out |= set(range(int(match.group(1)), int(match.group(2)) + 1))
        elif token.isdigit():
            out.add(int(token))
        else:
            raise ValueError(f'bad line number {token!r}')
    return out


def compact_lines(numbers):
    """[1,2,3,5] -> '1-3, 5' (the review pages' range form)."""
    numbers = sorted(set(numbers))
    parts, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            parts.append(str(start) if start == n else f'{start}-{n}')
            start = None
    return ', '.join(parts)


def _repo_path(root, path):
    path = Path(path)
    return path if path.is_absolute() else Path(root) / path


# ------------------------------------------------------------------------------------------------------------- geometry

def transform(point, frame):
    return [point[0] * frame[0][0] + point[1] * frame[1][0] + frame[2][0],
            point[0] * frame[0][1] + point[1] * frame[1][1] + frame[2][1]]


def _ring_area(points):
    area = 0.0
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        area += a[0] * b[1] - b[0] * a[1]
    return abs(area) / 2


def footprint_metrics(ring, pixel_to_meters, holes=()):
    """Port of src/lib/scene-types.ts footprintMetrics (area, pixel area, min-rectangle length / width / rotation)."""
    meters = [transform(p, pixel_to_meters) for p in ring]
    pts = sorted({(p[0], p[1]) for p in meters})

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def half(points):
        hull = []
        for p in points:
            while len(hull) > 1 and cross(hull[-2], hull[-1], p) <= 0:
                hull.pop()
            hull.append(p)
        return hull[:-1]

    hull = half(pts) + half(list(reversed(pts)))
    best = (math.inf, 0.0, 0.0, 0.0)
    for i, a in enumerate(hull):
        b = hull[(i + 1) % len(hull)]
        angle = math.atan2(b[1] - a[1], b[0] - a[0])
        c, s = math.cos(angle), math.sin(angle)
        xs = [p[0] * c + p[1] * s for p in meters]
        ys = [-p[0] * s + p[1] * c for p in meters]
        width, length = max(ys) - min(ys), max(xs) - min(xs)
        if width * length < best[0]:
            best = (width * length, max(width, length), min(width, length), angle + (math.pi / 2 if width > length else 0))

    def rnd(n):
        return round(n * 100) / 100

    area_m = _ring_area(meters) - sum(_ring_area([transform(p, pixel_to_meters) for p in h]) for h in holes)
    area_px = _ring_area(ring) - sum(_ring_area(h) for h in holes)
    return {'area_m2': rnd(area_m), 'footprint_area_px': rnd(area_px), 'length_m': rnd(best[1]), 'width_m': rnd(best[2]),
            'rotation_deg': rnd(((best[3] * 180) / math.pi + 180) % 180)}


def bbox(rings):
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def crop_box(rings, meters_to_pixel, width, height):
    """Square crop around the rings: bbox padded by max(10 m, half the larger side), 320-1024 px, clamped to the frame.
    Returns (x0, y0, w, h) in integer frame pixels; the full frame when the square cannot hold the rings."""
    x_min, y_min, x_max, y_max = bbox(rings)
    w, h = x_max - x_min, y_max - y_min
    px_per_m = math.hypot(meters_to_pixel[0][0], meters_to_pixel[0][1])
    pad = max(CROP_PAD_M * px_per_m, max(w, h) / 2)
    side = int(math.ceil(max(w, h) + 2 * pad))
    side = max(CROP_MIN, min(CROP_MAX, side))
    side = max(side, int(math.ceil(max(w, h))) + 2)
    side = min(side, int(width), int(height))
    cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2
    x0 = int(min(max(round(cx - side / 2), 0), int(width) - side))
    y0 = int(min(max(round(cy - side / 2), 0), int(height) - side))
    if not (x0 <= x_min and y0 <= y_min and x_max <= x0 + side and y_max <= y0 + side):
        return 0, 0, int(width), int(height)
    return x0, y0, side, side


def shift_ring(ring, dx, dy):
    return [[p[0] - dx, p[1] - dy] for p in ring]


def normalized_polygon(rings, width, height):
    """Rings (outer first, then holes) in pixels -> closed normalized Polygon coordinates (projection.go normalizedRing)."""
    out = []
    for ring in rings:
        points = [[p[0] / width, p[1] / height] for p in ring]
        if points[0] != points[-1]:
            points.append(list(points[0]))
        out.append(points)
    return out


def crop_frame(pixel_to_meters, meters_to_pixel, x0, y0):
    """Frame transforms of a crop at (x0, y0): pixel->metre translation t + x0*row0 + y0*row1, metre->pixel t - (x0, y0)."""
    p2m = copy.deepcopy(pixel_to_meters)
    p2m[2] = [pixel_to_meters[2][0] + x0 * pixel_to_meters[0][0] + y0 * pixel_to_meters[1][0],
              pixel_to_meters[2][1] + x0 * pixel_to_meters[0][1] + y0 * pixel_to_meters[1][1]]
    m2p = copy.deepcopy(meters_to_pixel)
    m2p[2] = [meters_to_pixel[2][0] - x0, meters_to_pixel[2][1] - y0]
    return p2m, m2p


def crop_png(path, box):
    from PIL import Image
    x0, y0, w, h = box
    with Image.open(path) as image:
        image.load()
        cropped = image.crop((x0, y0, x0 + w, y0 + h))
        buffer = io.BytesIO()
        cropped.save(buffer, format='PNG', optimize=False, compress_level=6)
    return buffer.getvalue()


def png_size(data: bytes):
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise ReviewError('not a PNG')
    return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')


# ---------------------------------------------------------------------------------------------------------------- scenes

class Scenes:
    """Read-only view of data/scenes/<S>/ (review layer over its bound model frame)."""

    def __init__(self, root):
        self.root = Path(root)
        self._cache = {}

    def get(self, scene):
        if scene in self._cache:
            return self._cache[scene]
        folder = self.root / 'data/scenes' / scene
        latest = json.loads((folder / 'latest.json').read_text())
        review_path = folder / 'review.json'
        review = json.loads(review_path.read_text()) if review_path.exists() else None
        signature = review['model_signature'] if review else latest['model_signature']
        model = latest if latest.get('model_signature') == signature else json.loads((folder / 'models' / f'{signature}.json').read_text())
        if model.get('image_sha256') != latest.get('image_sha256'):
            raise ReviewError(f'{scene}: the review is bound to a different image frame')
        image = folder / f"{model['image_sha256']}.png"
        if not image.exists():
            raise ReviewError(f'{scene}: scene image {image.name} is missing')
        objects = {o['id']: o for o in (review['objects'] if review else latest['objects'])}
        data = {'scene': scene, 'model': model, 'review': review, 'objects': objects, 'image': image,
                'revision': (review or {}).get('revision', 0), 'title': latest.get('title') or scene}
        self._cache[scene] = data
        return data

    def verify_image(self, scene):
        data = self.get(scene)
        if 'image_bytes' not in data:
            digest = sha_file(data['image'])
            if digest != data['model']['image_sha256']:
                raise ReviewError(f'{scene}: scene image bytes do not match image_sha256')
            with open(data['image'], 'rb') as handle:
                head = handle.read(32)
            size = png_size(head)
            if size != (data['model']['image_width'], data['model']['image_height']):
                raise ReviewError(f'{scene}: PNG size {size} differs from the frame')
            data['image_bytes'] = data['image'].stat().st_size
        return data


def address_of(title, scene):
    for part in title.split(' · '):
        if ',' in part and 'VIC' in part:
            return part.split(',')[0].strip().title()
    return scene


# ----------------------------------------------------------------------------------------------------------------- stage

def _hint(line):
    hint = line.get('agent_hint') or {}
    verdict = hint.get('verdict') or 'none'
    return verdict, hint.get('note') or ''


def _hint_tag(verdict):
    return {'right': 'right', 'partly right': 'partly', 'wrong': 'wrong', 'unclear': 'unclear'}.get(verdict, 'none' if verdict == 'none' else 'unclear')


def _freshness(scene_data, objects):
    """objects: [(object_id, ring_sha, status)] -> list of reasons the live scene differs (empty when fresh)."""
    reasons = []
    for object_id, expected_sha, expected_status in objects:
        live = scene_data['objects'].get(object_id)
        if live is None:
            reasons.append(f'{object_id[:8]} is gone')
            continue
        if ring_sha(live.get('ring'), live.get('holes')) != expected_sha:
            reasons.append(f'{object_id[:8]} outline changed')
        if expected_status is not None and live.get('status') != expected_status:
            reasons.append(f"{object_id[:8]} status {expected_status} -> {live.get('status')}")
    return reasons


def _clip_ring(ring, bounds):
    """The largest piece of ring inside bounds (x0, y0, x1, y1), as an open ring; [] when nothing is inside."""
    from shapely.geometry import Polygon, box
    g = Polygon(ring).buffer(0).intersection(box(*bounds))
    if g.is_empty or g.area <= 0:
        return []
    if g.geom_type != 'Polygon':
        g = max((p for p in getattr(g, 'geoms', []) if p.geom_type == 'Polygon'), key=lambda p: p.area, default=None)
        if g is None:
            return []
    return [[round(x, 3), round(y, 3)] for x, y in g.exterior.coords[:-1]]


def _object_copy(live, object_id, kind, label, ring, holes, status, notes, source=None, extra_model=None, group_id=None):
    obj = {'id': object_id, 'kind': kind, 'label': label[:300], 'ring': ring, 'status': status,
           'geometry_reviewed': False, 'source': (source or live.get('source') or 'unknown')[:300],
           'score': live.get('score'), 'model': {**copy.deepcopy(live.get('model') or {}), **(extra_model or {})},
           'notes': notes}
    if holes:
        obj['holes'] = holes
    if group_id:
        obj['group_id'] = group_id
    if obj['score'] is None:
        del obj['score']
    return obj


def _annotation(obj, label_id, width, height):
    rings = [obj['ring'], *(obj.get('holes') or [])]
    return {'id': obj['id'], 'labelId': label_id,
            'geometry': {'type': 'Polygon', 'coordinates': normalized_polygon(rings, width, height)},
            'properties': {'addr': {'objectId': obj['id'], 'layer': 'review-proposal', 'status': obj['status'],
                                    'geometry_reviewed': obj['geometry_reviewed'], 'source': obj['source']}}}


def _frame_doc(item_id, title, scene_model, width, height, image_sha, p2m, m2p, objects, groups):
    doc = {'schema_version': 1, 'id': item_id, 'title': title, 'image_sha256': image_sha, 'image_width': width,
           'image_height': height, 'pixel_to_meters': p2m, 'meters_to_pixel': m2p, 'objects': objects, 'groups': groups,
           'review_frame': {'set': SET, 'sceneId': scene_model.get('id'), 'sceneModelSignature': scene_model.get('model_signature'),
                            'note': 'A review copy for Fieldwork. Not a scene, not training data.'},
           'model_signature': ''}
    doc['model_signature'] = sha256(canonical(doc))
    return doc


class Stager:
    def __init__(self, root, items_path, context_path, project, drop_stale=False, full_frame=False):
        self.root = Path(root)
        self.items_path = _repo_path(root, items_path)
        self.context_path = _repo_path(root, context_path)
        self.project = project
        self.drop_stale = drop_stale
        self.full_frame = full_frame
        self.items = json.loads(self.items_path.read_text())
        self.context = json.loads(self.context_path.read_text())
        self.scenes = Scenes(root)
        self.blobs = {}          # sha -> {'bytes': b, 'contentType', 'path'} or {'file': Path, ...}
        self.out_items = []
        self.dropped = []
        self.excluded = []

    def label_id(self, suffix):
        return f'{self.project}-{suffix}'

    def _blob(self, data, content_type, path):
        digest = sha256(data)
        self.blobs.setdefault(digest, {'bytes': data, 'contentType': content_type, 'path': path, 'size': len(data)})
        return {'url': f'/api/v1/assets/{digest}', 'sha256': digest, 'bytes': len(data), 'contentType': content_type, 'path': path}

    def _scene_image_ref(self, scene):
        data = self.scenes.verify_image(scene)
        digest = data['model']['image_sha256']
        path = f"data/scenes/{scene}/{digest}.png"
        self.blobs.setdefault(digest, {'file': data['image'], 'contentType': 'image/png', 'path': path, 'size': data['image_bytes']})
        return {'url': f'/api/v1/assets/{digest}', 'sha256': digest, 'bytes': data['image_bytes'], 'contentType': 'image/png', 'path': path}

    def _check_fresh(self, family, line, scene_data, objects):
        reasons = _freshness(scene_data, objects)
        if reasons:
            if not self.drop_stale:
                raise ReviewError(f'{family} line {line} is stale ({"; ".join(reasons)}); rerun with --drop-stale to leave it out')
            self.dropped.append({'family': family, 'line': line, 'scene': scene_data['scene'], 'reasons': reasons})
            return False
        return True

    def _base(self, scene_data):
        review = scene_data['review'] or {}
        return {'reviewRevision': scene_data['revision'], 'reviewUpdatedAt': review.get('updated_at'),
                'modelSignature': scene_data['model']['model_signature'], 'imageSha256': scene_data['model']['image_sha256']}

    def _scene_item(self, item_id, title, family, line_meta, scene_data, objects_with_labels, groups, crop_rings, tags, notes):
        model = scene_data['model']
        W, H = int(model['image_width']), int(model['image_height'])
        if crop_rings is not None and not self.full_frame:
            x0, y0, w, h = crop_box(crop_rings, model['meters_to_pixel'], W, H)
        else:
            x0, y0, w, h = 0, 0, W, H
        full = (x0, y0, w, h) == (0, 0, W, H)
        scene = scene_data['scene']
        if full:
            image_ref = self._scene_image_ref(scene)
        else:
            self.scenes.verify_image(scene)
            png = crop_png(scene_data['image'], (x0, y0, w, h))
            if png_size(png) != (w, h):
                raise ReviewError(f'{item_id}: crop size mismatch')
            image_ref = self._blob(png, 'image/png', f'{SET}/crops/{item_id}.png')
        p2m, m2p = crop_frame(model['pixel_to_meters'], model['meters_to_pixel'], x0, y0)
        objects, annotations = [], []
        for obj, label_suffix in objects_with_labels:
            obj = copy.deepcopy(obj)
            if obj.pop('_clip', False):  # context outlines (lots, neighbours) are cut to the crop; dropped when outside it
                obj['ring'] = _clip_ring(obj['ring'], (x0, y0, x0 + w, y0 + h))
                if not obj['ring']:
                    continue
            obj['ring'] = shift_ring(obj['ring'], x0, y0)
            if obj.get('holes'):
                obj['holes'] = [shift_ring(h, x0, y0) for h in obj['holes']]
            objects.append(obj)
            annotations.append(_annotation(obj, self.label_id(label_suffix), w, h))
        frame = _frame_doc(item_id, title, model, w, h, image_ref['sha256'], p2m, m2p, objects, groups)
        frame_ref = self._blob(canonical(frame), 'application/json', f'{SET}/frames/{item_id}.json')
        meta = {**line_meta, 'schemaVersion': 1, 'kind': 'review-proposal', 'set': SET, 'family': family,
                'sceneId': scene, 'base': self._base(scene_data),
                'crop': {'x0': x0, 'y0': y0, 'width': w, 'height': h, 'sourceImageSha256': model['image_sha256'],
                         'sourceWidth': W, 'sourceHeight': H, 'fullFrame': full},
                'frameSignature': frame['model_signature']}
        item = {'id': item_id, 'projectId': self.project, 'title': title[:300], 'adapter': 'addr-scene',
                'source': {'sceneId': scene, 'image': image_ref, 'model': frame_ref, 'modelSignature': frame['model_signature'],
                           'imageSha256': image_ref['sha256'], 'width': w, 'height': h},
                'metadata': {'addr': meta}, 'tags': tags, 'status': 'unreviewed', 'notes': notes, 'annotations': annotations}
        self.out_items.append(item)
        return item

    # -- families --------------------------------------------------------------------------------------------------------
    def stage_shadows(self):
        lines = {x['line']: x for x in self.items['shadow']}
        for x in self.items['shadow']:
            if x.get('excluded'):
                self.excluded.append({'family': 'shadow', 'line': x['line'], 'reason': x.get('excluded_reason', '')[:200]})
                continue
            sd = self.scenes.get(x['scene'])
            if not self._check_fresh('shadow', x['line'], sd, [(x['object_id'], x['current_ring_sha'], x['status_at_compute'])]):
                continue
            live = sd['objects'][x['object_id']]
            verdict, note = _hint(x)
            n = x['line']
            same = sorted(int(s) for s in re.findall(r'\d+', str(x.get('same_tree_as') or '')) if int(s) in lines and int(s) != x['line'])
            addr = address_of(sd['title'], x['scene'])
            title = f"Shadows {n:03d} · {x['kind']} {x['object_id'][:8]} · {addr} · agent: {verdict}"
            if same:
                title += f" · same tree as Shadows {', '.join(f'{s:03d}' for s in same)}"
            cur = _object_copy(live, f"cur-{x['object_id']}", 'other',
                               f"CURRENT saved outline · {live['kind']} · {live['status']} · leave as is", live['ring'], live.get('holes') or [],
                               live['status'], 'The outline saved in your scene now (drawn in the "other" colour). Leave it as it is; '
                               'answer on the PROPOSED outline.')
            metrics = footprint_metrics(x['proposed_ring'], sd['model']['pixel_to_meters'], x.get('proposed_holes') or [])
            new = _object_copy(live, f"new-{x['object_id']}", live['kind'], f'PROPOSED trim · line {n:03d}', x['proposed_ring'],
                               x.get('proposed_holes') or [], 'candidate',
                               f"Agent hint: {verdict}. {note} Removes {x['removed_m2']:.2f} of {x['area_before_m2']:.2f} m² "
                               f"({x['removed_share'] * 100:.0f} %).".strip(),
                               source='agent-proposal:shadow-trim-v1', extra_model={**metrics, 'height_verified': False})
            self._scene_item(
                f"r1008-1-shadow-{n:03d}-p{x['proposal_id'][:8]}", title, 'shadow',
                {'line': n, 'objectIds': [x['object_id']], 'proposalId': x['proposal_id'], 'objectKind': live['kind'],
                 'currentRingSha': x['current_ring_sha'], 'currentStatus': live['status'],
                 'proposedRingSha': ring_sha(x['proposed_ring'], x.get('proposed_holes')),
                 'agentHint': {'verdict': verdict, 'note': note, 'source': (x.get('agent_hint') or {}).get('source')},
                 'proposalSource': x.get('render')},
                sd, [(cur, 'current'), (new, 'proposed')], [], [x['current_ring'], x['proposed_ring']],
                [SET, f'{SET}-shadows', f'agent-hint-{_hint_tag(verdict)}'],
                '# How to answer: select PROPOSED (Find box or click), set Object review status: confirmed = right, '
                'rejected = wrong, needs_adjustment = partly (a strip of shadow left in; the agent trims again). Leave '
                'candidate = no answer. Then Save changes and Next item. Lines starting with # are ignored.')

    def stage_corners(self):
        # queue 1 (2026-10-10) has no corner_set block; later queues name their own item prefix and tag
        cset = self.items.get('corner_set') or {'prefix': 'r1010-6', 'tag': CORNER_TAG}
        for x in self.items.get('corner', []):
            sd = self.scenes.get(x['scene'])
            sha = ring_sha(x['current_ring'], [])
            if not self._check_fresh('corner', x['line'], sd, [(x['object_id'], sha, x['status_at_compute'])]):
                continue
            live = sd['objects'][x['object_id']]
            n = x['line']
            addr = address_of(sd['title'], x['scene'])
            verdict, note = _hint(x)
            title = f"Corners {n:03d} · roof {x['object_id'][:8]} · {x['current_corners']} -> {x['proposed_corners']} corners · {addr}"
            unit = x.get('unit_of')
            if unit:  # parcel_units split of a queue item that spans several Vicmap lots
                title = (f"Corners {n:03d} · unit of Corners {unit['line']:03d} · lot {unit['lot']} {unit['plan']} · "
                         f"{x['proposed_corners']} corners · {addr}")
            if verdict != 'none':
                title += f" · agent: {verdict}"
            cur = _object_copy(live, f"cur-{x['object_id']}", 'other',
                               f"CURRENT saved outline · {x['current_corners']} corners · leave as is", live['ring'], [],
                               live['status'], 'The outline saved in your scene now (drawn in the "other" colour). Leave it as it is; '
                               'answer on the PROPOSED outline.')
            metrics = footprint_metrics(x['proposed_ring'], sd['model']['pixel_to_meters'], [])
            new = _object_copy(live, f"new-{x['object_id']}", 'building', f"PROPOSED right-angle redraw · line {n:03d}",
                               x['proposed_ring'], [], 'candidate',
                               f"{x['proposed_corners']} corners instead of {x['current_corners']}; overlaps the saved outline "
                               f"{x['iou'] * 100:.0f} %. Right = a good training example of this roof."
                               + (f" One unit of Corners {x['unit_of']['line']:03d}, cut at the Vicmap lot line (lot "
                                  f"{x['unit_of']['lot']} {x['unit_of']['plan']}); the cut is the title boundary, not a measured "
                                  "party wall - Partly if it misses the wall." if x.get('unit_of') else '')
                               + (f" Agent hint: {verdict}. {note}".rstrip() if verdict != 'none' else ''),
                               source='agent-proposal:corner-redraw-v1', extra_model={**metrics, 'height_verified': False})
            self._scene_item(
                f"{cset['prefix']}-corner-{n:03d}-p{x['proposal_id'][:8]}", title, 'corner',
                {'line': n, 'objectIds': [x['object_id']], 'proposalId': x['proposal_id'], 'objectKind': 'building',
                 'currentRingSha': sha, 'currentStatus': live['status'],
                 'proposedRingSha': ring_sha(x['proposed_ring'], []), 'method': x['method'], 'iou': x['iou'],
                 'agentHint': {'verdict': verdict, 'note': note, 'source': (x.get('agent_hint') or {}).get('source')},
                 **({'split': x['split']} if x.get('split') else {}),
                 **({'unitOf': x['unit_of']} if x.get('unit_of') else {})},
                sd, [(cur, 'current'), (new, 'proposed')], [], [x['current_ring'], x['proposed_ring']],
                [SET, f'{SET}-corners', cset['tag'], f'agent-hint-{_hint_tag(verdict)}'],
                '# Corners: is the cyan PROPOSED outline a good simple drawing of this roof? Right = yes (it becomes a training '
                'example; your saved outline is not changed). Partly = close, but a corner or edge is off. Wrong = not this roof. '
                'Lines starting with # are ignored.')

    def stage_units(self):
        """One item per unit of a saved outline that spans several numbered Vicmap lots (units card 2026-10-12; reviewer 2026-10-13:
        always bounded by the legal lot). CURRENT = the saved outline (context, grey); PROPOSED = this unit, the saved outline cut
        to its own (aligned) lot. Right = a correct outline of this dwelling; Partly = the right unit, an edge is off; Wrong = not a
        separate dwelling. Items carry unitsTag (default UNITS_TAG) and an id prefix from units_set."""
        uset = self.items.get('units_set') or {'prefix': 'r1013-9', 'tag': 'units-2026-10-13'}
        for x in self.items.get('unit', []):
            sd = self.scenes.get(x['scene'])
            holes = x.get('current_holes') or []
            sha = ring_sha(x['current_ring'], holes)
            if not self._check_fresh('unit', x['line'], sd, [(x['object_id'], sha, x['status_at_compute'])]):
                continue
            live = sd['objects'][x['object_id']]
            n, u = x['line'], x['unit_of']
            title = (f"Units {n:03d} · lot {u['lot']} {u['plan']} · unit {u['index']}/{u['of']} of {live['status']} roof "
                     f"{x['object_id'][:8]} · {address_of(sd['title'], x['scene'])}" + (f" · {x['why']}" if x.get('why') else ''))
            cur = _object_copy(live, f"cur-{x['object_id']}", 'other',
                               f"CURRENT saved outline · spans {u['of']} lots · leave as is", live['ring'], holes, live['status'],
                               'The outline saved in your scene now (drawn in the "other" colour). It crosses lot lines; '
                               'answer on the PROPOSED unit.')
            metrics = footprint_metrics(x['proposed_ring'], sd['model']['pixel_to_meters'], [])
            new = _object_copy(live, f"new-{x['proposal_id']}", 'building', f"PROPOSED unit · lot {u['lot']} {u['plan']} · line {n:03d}",
                               x['proposed_ring'], [], 'candidate',
                               f"This dwelling's part of your saved outline, bounded by its legal lot ({u['lot']} {u['plan']}, Vicmap "
                               f"aligned to this photo). Right = a correct outline of this dwelling; Partly = right unit, an edge is "
                               f"off; Wrong = not a separate dwelling." + (f" Flagged: {x['why']}." if x.get('why') else ''),
                               source='agent-proposal:parcel-units-v2', extra_model={**metrics, 'height_verified': False})
            self._scene_item(
                f"{uset['prefix']}-unit-{n:03d}-p{x['proposal_id'][:8]}", title, 'unit',
                {'line': n, 'objectIds': [x['object_id']], 'proposalId': x['proposal_id'], 'objectKind': live['kind'],
                 'currentRingSha': sha, 'currentStatus': live['status'], 'proposedRingSha': ring_sha(x['proposed_ring'], []),
                 'unitOf': u, 'route': x.get('route'), 'why': x.get('why'), 'agentHint': {'verdict': 'none', 'note': '', 'source': None}},
                sd, [(cur, 'current'), (new, 'proposed')], [], [x['current_ring'], x['proposed_ring']],
                [SET, f'{SET}-units', uset['tag'], 'agent-hint-none'],
                '# Units: is the cyan PROPOSED outline a correct outline of this one dwelling, inside its lot? Right / Partly / '
                'Wrong. Lines starting with # are ignored.')

    def stage_lots(self):
        """Lot-bound review (2026-10-13, reviewer: "always be bounded based off the actual legal property boundaries"): a saved,
        reviewer-touched outline of one dwelling that runs outside its legal lot (Vicmap aligned to the photo). CURRENT = the saved
        outline; PROPOSED = the same outline clipped to the lot. Right = use the clipped outline; Partly = clip, but the lot line
        is off here; Wrong = keep the outline as it is (e.g. a structure built over the boundary)."""
        for x in self.items.get('lot', []):
            sd = self.scenes.get(x['scene'])
            holes = x.get('current_holes') or []
            sha = ring_sha(x['current_ring'], holes)
            if not self._check_fresh('lot', x['line'], sd, [(x['object_id'], sha, x['status_at_compute'])]):
                continue
            live = sd['objects'][x['object_id']]
            n = x['line']
            title = (f"Lots {n:03d} · {live['status']} {live['kind']} {x['object_id'][:8]} · {x['clipped_m2']:.1f} m² outside lot "
                     f"{x['lot']} {x['plan']} · {address_of(sd['title'], x['scene'])}")
            cur = _object_copy(live, f"cur-{x['object_id']}", 'other', f"CURRENT saved outline · {x['clipped_m2']:.1f} m² outside its lot",
                               live['ring'], holes, live['status'],
                               'The outline saved in your scene now (drawn in the "other" colour). Answer on the PROPOSED outline.')
            metrics = footprint_metrics(x['proposed_ring'], sd['model']['pixel_to_meters'], [])
            new = _object_copy(live, f"new-{x['object_id']}", live['kind'], f"PROPOSED · clipped to lot {x['lot']} {x['plan']} · line {n:03d}",
                               x['proposed_ring'], [], 'candidate',
                               f"Your outline clipped to its legal lot ({x['lot']} {x['plan']}, Vicmap aligned to this photo): "
                               f"{x['clipped_m2']:.1f} m² removed. Right = use it; Partly = clip, but the lot line is off here; "
                               "Wrong = keep the outline as it is.",
                               source='agent-proposal:lot-bound-v1', extra_model={**metrics, 'height_verified': False})
            self._scene_item(
                f"r1013-10-lot-{n:03d}-p{x['proposal_id'][:8]}", title, 'lot',
                {'line': n, 'objectIds': [x['object_id']], 'proposalId': x['proposal_id'], 'objectKind': live['kind'],
                 'currentRingSha': sha, 'currentStatus': live['status'], 'proposedRingSha': ring_sha(x['proposed_ring'], []),
                 'lot': {k: x[k] for k in ('lot', 'plan', 'pfi', 'clipped_m2')}, 'agentHint': {'verdict': 'none', 'note': '', 'source': None}},
                sd, [(cur, 'current'), (new, 'proposed')], [], [x['current_ring'], x['proposed_ring']],
                [SET, f'{SET}-lots', 'lot-bound-2026-10-13', 'agent-hint-none'],
                '# Lots: should this outline be clipped to its legal lot (PROPOSED)? Right / Partly / Wrong. Lines starting with # are ignored.')

    def stage_splits(self):
        """One question per roof (cut-check card 2026-10-17): should this saved outline be split along its lot lines? PROPOSED = the
        largest unit (inside its lot), the other units are context (amber), CURRENT = the saved outline (grey). Right = split into
        the units shown; Wrong = one building on several titles. The planner's own leaning is not shown. Answers train the cut check."""
        sset = self.items.get('split_set') or {'prefix': 'r1017-12', 'tag': 'splits-2026-10-17'}
        for x in self.items.get('split', []):
            sd = self.scenes.get(x['scene'])
            holes = x.get('current_holes') or []
            sha = ring_sha(x['current_ring'], holes)
            if not self._check_fresh('split', x['line'], sd, [(x['object_id'], sha, x['status_at_compute'])]):
                continue
            live = sd['objects'][x['object_id']]
            n, k = x['line'], len(x['units'])
            lots = ', '.join(str(u['lot']) for u in x['units'])
            where = ' + '.join(x['addresses']) if x.get('addresses') else address_of(sd['title'], x['scene'])
            if x.get('addresses') and len(f"Splits {n:03d} · {where} · split into {k} units? · roof {x['object_id'][:8]}") > 280:
                where = ' + '.join(x['addresses'][:2]) + f" + {len(x['addresses']) - 2} more"  # the server caps titles at 300
            title = (f"Splits {n:03d} · {where} · split into {k} units? · roof {x['object_id'][:8]}" if x.get('addresses') else
                     f"Splits {n:03d} · split into {k} units along lot lines {lots} {x['units'][0]['plan']}? · roof {x['object_id'][:8]} · {where}")
            cur = _object_copy(live, f"cur-{x['object_id']}", 'other', f"CURRENT saved outline · {live['status']} · leave as is",
                               live['ring'], holes, live['status'], 'The outline saved in your scene now. Answer on the PROPOSED unit.')
            big = max(x['units'], key=lambda u: u['m2'])
            metrics = footprint_metrics(x['proposed_ring'], sd['model']['pixel_to_meters'], [])
            new = _object_copy(live, f"new-{x['proposal_id']}", 'building', f"PROPOSED split · unit on lot {big['lot']} · line {n:03d}",
                               x['proposed_ring'], [], 'candidate',
                               f"Should this roof be split into the {k} units shown (cyan + amber), one per lot? Right = yes, these are "
                               "separate dwellings or buildings. Wrong = no, it is one building on several titles (keep it whole).",
                               source='agent-proposal:split-check-v1', extra_model={**metrics, 'height_verified': False})
            others = [(_object_copy(live, f"unit-{u['id']}", 'other', f"UNIT · lot {u['lot']} {u['plan']} · {u['m2']:.0f} m²", u['ring'], [],
                                    'candidate', 'Another unit of the PROPOSED split (context).', source='parcel-units-v3'), 'unit')
                      for u in x['units'] if u['id'] != big['id']]
            # context: Vicmap lot boundaries (the cut's source of truth, drawn as used) and neighbouring saved buildings
            ctx = [(_object_copy(live, f"lot-{l['pfi']}", 'other', l['label'], l['ring'], [], 'candidate',
                                 'Legal lot boundary (Vicmap Parcel, aligned to this photo). Context: not answered.', source='vicmap-parcel'), 'lot')
                   for l in x.get('lot_rings', [])]
            ctx += [(_object_copy(live, f"nb-{b['id']}", 'other', f"Saved building {b['id'][:8]} · {b['status']}", b['ring'], [], b['status'],
                                  'Another saved building nearby (context, not answered).'), 'context') for b in x.get('neighbours', [])]
            for obj, _ in ctx:
                obj['_clip'] = True
            crop = [x['current_ring']]  # framed on the roof; lot lines and neighbours are clipped to that view
            self._scene_item(
                f"{sset['prefix']}-split-{n:03d}-p{x['proposal_id'][:8]}", title, 'split',
                {'line': n, 'objectIds': [x['object_id']], 'proposalId': x['proposal_id'], 'objectKind': live['kind'],
                 'currentRingSha': sha, 'currentStatus': live['status'], 'proposedRingSha': ring_sha(x['proposed_ring'], []),
                 'units': [{k2: u[k2] for k2 in ('id', 'lot', 'plan', 'pfi', 'm2')} for u in x['units']],
                 'agentHint': {'verdict': 'none', 'note': '', 'source': None}},
                sd, [*ctx, (cur, 'current'), *others, (new, 'proposed')], [], crop,
                [SET, f'{SET}-splits', sset['tag'], 'agent-hint-none'],
                '# Splits: should this roof be split into the units shown? Right = split; Wrong = one building on several titles. '
                'Lines starting with # are ignored.')

    def stage_traces(self):
        """Agent-trace review (Harness² tracing agent, scripts/ml/harnessing): CURRENT = the saved outline; PROPOSED = the
        outline the tracing agent traced against the original image, with its notes on unresolved edges. Right = use the
        trace; Partly = close, but an edge is off (fix the PROPOSED outline in place and save: an edit); Wrong = keep the
        saved outline. Answers become reference outlines for the agent's next round; nothing here edits a scene."""
        tset = self.items.get('trace_set') or {}
        prefix, set_tag = tset.get('prefix', 'r1019-14'), tset.get('tag', 'harness-traces')
        for x in self.items.get('trace', []):
            sd = self.scenes.get(x['scene'])
            holes = x.get('current_holes') or []
            sha = ring_sha(x['current_ring'], holes)
            if not self._check_fresh('trace', x['line'], sd, [(x['object_id'], sha, x['status_at_compute'])]):
                continue
            live = sd['objects'][x['object_id']]
            n, proposed_holes = x['line'], x.get('proposed_holes') or []
            title = (f"Traces {n:03d} · {live['status']} {live['kind']} {x['object_id'][:8]} · agent trace, "
                     f"{len(x['proposed_ring'])} vertices · {address_of(sd['title'], x['scene'])}")
            cur = _object_copy(live, f"cur-{x['object_id']}", 'other', 'CURRENT saved outline', live['ring'], holes,
                               live['status'], 'The outline saved in your scene now (drawn in the "other" colour). Answer on '
                               'the PROPOSED outline.')
            notes = (x.get('agent_notes') or '').strip()
            new = _object_copy(live, f"new-{x['object_id']}", live['kind'], f"PROPOSED · agent trace · line {n:03d}",
                               x['proposed_ring'], proposed_holes, 'candidate',
                               "The tracing agent's outline against the original image. Right = use it; Partly = close, but "
                               "an edge is off (fix it here and save); Wrong = keep your saved outline."
                               + (f"\nAgent notes: {notes[:1500]}" if notes else ''),
                               source=f"agent-proposal:{x.get('source') or 'harness-trace-v1'}",
                               extra_model={**footprint_metrics(x['proposed_ring'], sd['model']['pixel_to_meters'],
                                                                proposed_holes), 'height_verified': False})
            self._scene_item(
                f"{prefix}-trace-{n:03d}-p{x['proposal_id'][:8]}", title, 'trace',
                {'line': n, 'objectIds': [x['object_id']], 'proposalId': x['proposal_id'], 'objectKind': live['kind'],
                 'currentRingSha': sha, 'currentStatus': live['status'],
                 'proposedRingSha': ring_sha(x['proposed_ring'], proposed_holes), 'harness': x.get('harness') or {},
                 'agentHint': {'verdict': 'none', 'note': '', 'source': None}},
                sd, [(cur, 'current'), (new, 'proposed')], [], [x['current_ring'], x['proposed_ring']],
                [SET, f'{SET}-traces', set_tag, 'agent-hint-none'],
                '# Traces: is the PROPOSED agent trace a better outline of this object than the CURRENT one? Right = use the '
                'trace; Partly = close, but an edge is off (fix it in place and save); Wrong = keep the CURRENT outline. Lines '
                'starting with # are ignored.')

    def stage_cuts(self):
        whole = {int(n) for n in self.context['cuts']['whole_other']}
        for x in self.items['cut']:
            sd = self.scenes.get(x['scene'])
            if not self._check_fresh('cut', x['line'], sd, [(x['object_id'], x['current_ring_sha'], x['status_at_compute'])]):
                continue
            live = sd['objects'][x['object_id']]
            verdict, note = _hint(x)
            n = x['line']
            partners = sorted(x.get('same_outline_as') or [])
            title = f"Cuts {n:03d} · {x['type_words']} · {live['kind']} {x['object_id'][:8]} · agent: {verdict}"
            if partners:
                title += f" · one outline with Cuts {', '.join(f'{p:03d}' for p in partners)}"
            held = ''
            if n in whole:
                title += ' · whole-roof copy (held)'
                held = (' Whole-roof copy (lines 66-116): your mark is recorded, but nothing is written until a shape check '
                        'exists and you also choose "cuts: whole roofs too".')
            cur = _object_copy(live, f"cur-{x['object_id']}", 'other',
                               f"CURRENT saved outline · {live['kind']} · {live['status']} · leave as is", live['ring'], live.get('holes') or [],
                               live['status'], 'The outline saved in your scene now (drawn in the "other" colour). Leave it as it is; '
                               'answer on the PROPOSED outline.')
            metrics = footprint_metrics(x['proposed_ring'], sd['model']['pixel_to_meters'], x.get('proposed_holes') or [])
            new = _object_copy(live, f"new-{x['object_id']}", live['kind'], f"PROPOSED {x['type_words'].lower()} · line {n:03d}",
                               x['proposed_ring'], x.get('proposed_holes') or [], 'candidate',
                               f"Agent hint: {verdict}. {note}{held}".strip(), source=f"agent-proposal:{x['type']}",
                               extra_model={**metrics, 'height_verified': False})
            self._scene_item(
                f"r1008-2-cut-{n:03d}-p{x['proposal_id'][:8]}", title, 'cut',
                {'line': n, 'objectIds': [x['object_id']], 'proposalId': x['proposal_id'], 'objectKind': live['kind'], 'type': x['type'],
                 'currentRingSha': x['current_ring_sha'], 'currentStatus': live['status'], 'sameOutlineAs': partners,
                 'wholeRoof': n in whole, 'proposedRingSha': ring_sha(x['proposed_ring'], x.get('proposed_holes')),
                 'agentHint': {'verdict': verdict, 'note': note, 'source': (x.get('agent_hint') or {}).get('source')},
                 'proposalSource': x.get('render')},
                sd, [(cur, 'current'), (new, 'proposed')], [], [x['current_ring'], x['proposed_ring']],
                [SET, f'{SET}-cuts', f'agent-hint-{_hint_tag(verdict)}'],
                '# How to answer: select PROPOSED, set Object review status: confirmed = right, rejected = wrong, '
                'needs_adjustment = needs work (nothing written; add a note). Leave candidate = no answer. Save changes, '
                f'Next item. Lines starting with # are ignored.{(" #" + held) if held else ""}')

    def stage_veg(self):
        ctx = self.context['veg']
        must_skip = {int(n) for n in ctx['bulk_must_skip']}
        by_scene = {}
        for x in self.items['vegetation']:
            sd = self.scenes.get(x['scene'])
            obj = x['freshness']['objects'][0]
            if not self._check_fresh('veg', x['line'], sd, [(x['object_id'], obj['base_ring_sha'], obj['base_status'])]):
                continue
            by_scene.setdefault(x['scene'], []).append(x)
        for scene in sorted(by_scene):
            sd = self.scenes.get(scene)
            lines = sorted(by_scene[scene], key=lambda v: v['line'])
            objects, line_map, sheets, counts = [], {}, {}, {}
            for x in lines:
                live = sd['objects'][x['object_id']]
                display = ctx['display'][str(x['line'])]
                counts[display] = counts.get(display, 0) + 1
                n = x['line']
                word = {'unclear': 'type', 'tree-or-shrub': 'tree or shrub'}.get(display, display)
                hint = x.get('agent_hint') or {}
                read = f" Agent read: {hint.get('verdict')}{' (' + hint['note'] + ')' if hint.get('note') else ''}." if hint else ''
                need = (' Confirm stores nothing here: type tree or shrub in the Label.' if display == 'tree-or-shrub'
                        else ' Confirm stores nothing here: type grass, hedge, shrub or tree in the Label.' if display == 'unclear' else '')
                notes = (f"Agent: {x.get('reason') or 'no reason recorded'}.{read} Outline in your scene: {live['status']}."
                         f"{need}")
                objects.append((_object_copy(live, x['object_id'], live['kind'], f'{word}? · {n:04d}', live['ring'], live.get('holes') or [],
                                             'candidate', notes, group_id=f'veg-{display}'), f'veg-{display}'))
                line_map[x['object_id']] = n
                if x.get('sheets'):
                    sheets[str(n)] = x['sheets']
            groups = [{'id': f'veg-{g}', 'label': f'{VEG_GROUP_NAMES[g]} · {counts[g]}'} for g in VEG_GROUPS if counts.get(g)]
            features = sha256(canonical([[o['id'], o['ring'], o.get('holes'), o['label'], o['group_id']] for o, _ in objects]))
            total = len(objects)
            title = (f"Veg types · {scene} · {total} outlines · confirmed = proposed type right · Label word (grass, hedge, shrub, tree) "
                     f"= other type · Fieldwork notes: 'grass right except 12'")
            self._scene_item(
                f'r1008-3-veg-{scene}-v{features[:8]}', title, 'veg',
                {'lines': line_map, 'sheets': sheets, 'bulkMustSkip': sorted(n for n in line_map.values() if n in must_skip),
                 'liveStatus': {o['id']: sd['objects'][o['id']]['status'] for o, _ in objects}, 'featuresSha256': features},
                sd, objects, groups, None, [SET, f'{SET}-veg'],
                '# Per outline: confirmed = the proposed type is right (only the type is stored; outline and status in your '
                'scene stay as they are); rejected = not that type; to store another type replace the Label with one word: '
                'grass, hedge, shrub or tree.\n# A whole group: add a line below such as "grass right", "tree right except 233, '
                '313" or "all right" (grass, hedge, shrub and tree groups only). It skips outlines you confirmed in your '
                'scenes, lines the agent did not read as right, outlines you marked here, and the tree (or shrub) and no-type '
                'groups; the pull lists what it skipped. Lines starting with # are ignored.\n')

    def stage_joins(self):
        for x in self.items['join']:
            sd = self.scenes.get(x['scene'])
            fresh = [(o['object_id'], o['base_ring_sha'], o['base_status']) for o in x['freshness']['objects']]
            if not self._check_fresh('join', x['line'], sd, fresh):
                continue
            verdict, note = _hint(x)
            n = x['line']
            pieces = []
            for i, member in enumerate(x['members'], 1):
                live = sd['objects'][member]
                area = footprint_metrics(live['ring'], sd['model']['pixel_to_meters'], live.get('holes') or [])['area_m2']
                pieces.append((_object_copy(live, f'piece-{member}', 'other',
                                            f'PIECE {i}/{len(x["members"])} · {member[:8]} · {area:.1f} m² · {live["status"]}',
                                            live['ring'], live.get('holes') or [], live['status'],
                                            'One saved piece in your scene (drawn in the "other" colour). Leave it as it is; '
                                            'answer on the JOINED outline.'), 'join-piece'))
            first = sd['objects'][x['members'][0]]
            metrics = footprint_metrics(x['joined_ring'], sd['model']['pixel_to_meters'], x.get('joined_holes') or [])
            joined = _object_copy(first, f"join-{x['feature_id']}", 'building', f'JOINED outline · join {n:02d}', x['joined_ring'],
                                  x.get('joined_holes') or [], 'candidate',
                                  f"Agent hint: {verdict}. {note} Joined {x['area_m2']:.1f} m² from {x['n_members']} pieces "
                                  f"(largest {x['largest_member_m2']:.1f} m²); rules {', '.join(x['rules'])}; cover {x['cover_max']:.2f}, "
                                  f"gap {x['gap_max_m']:.2f} m, absorbed {x['absorbed_m2']:.1f} m², bridge {x['bridge_m2']:.1f} m².",
                                  source='agent-proposal:roof-join-run-v2', extra_model={**metrics, 'height_verified': False})
            rings = [sd['objects'][m]['ring'] for m in x['members']] + [x['joined_ring']]
            title = f"Joins {n:02d} · {x['n_members']} pieces → {x['area_m2']:.0f} m² · {address_of(sd['title'], x['scene'])} · agent: {verdict}"
            self._scene_item(
                f"r1008-4-join-{n:02d}-f{x['feature_id'][:8]}", title, 'join',
                {'line': n, 'featureId': x['feature_id'], 'members': x['members'], 'rules': x['rules'],
                 'objectIds': x['members'], 'memberRingSha': {o[0]: o[1] for o in fresh},
                 'joinedRingSha': ring_sha(x['joined_ring'], x.get('joined_holes')),
                 'agentHint': {'verdict': verdict, 'note': note, 'source': (x.get('agent_hint') or {}).get('source')},
                 'proposalSource': x.get('render')},
                sd, [*pieces, (joined, 'joined')], [], rings, [SET, f'{SET}-joins', f'agent-hint-{_hint_tag(verdict)}'],
                '# How to answer: select JOINED (Find box or click), set Object review status: confirmed = right, '
                'rejected = wrong, needs_adjustment = needs work (add a note). Leave candidate = no answer. Save changes, '
                'Next item. Lines starting with # are ignored.')

    def stage_round5(self):
        for q in ROUND5:
            title = (f"Round 5 · {q['n']} of 3 · {q['question']} Status Approved = {q['approved']} (default) · "
                     f"Rejected = {q['rejected']}")
            self.out_items.append({
                'id': f"r1008-5-round5-{q['n']}", 'projectId': self.project, 'title': title[:300], 'adapter': 'map',
                'source': {'center': ROUND5_CENTER, 'zoom': 14},
                'metadata': {'addr': {'schemaVersion': 1, 'kind': 'review-proposal', 'set': SET, 'family': 'round5', 'line': q['n'],
                                      'approved': q['approved'], 'rejected': q['rejected']}},
                'tags': [SET, f'{SET}-round5', 'agent-hint-none'], 'status': 'unreviewed',
                'notes': f"# {q['context']}\n# Set Status to Approved ({q['approved']}, the default) or Rejected ({q['rejected']}), then "
                         'save. Write anything else on a line below. Lines starting with # are ignored.\n',
                'annotations': []})

    def run(self, families=FAMILIES):
        steps = {'shadow': self.stage_shadows, 'cut': self.stage_cuts, 'veg': self.stage_veg, 'join': self.stage_joins,
                 'round5': self.stage_round5, 'corner': self.stage_corners, 'unit': self.stage_units, 'lot': self.stage_lots, 'split': self.stage_splits,
                 'trace': self.stage_traces}
        for family in FAMILIES:
            if family in families and (family not in ('corner', 'unit', 'lot', 'split', 'trace') or family in self.items):
                steps[family]()
        self.out_items.sort(key=lambda item: item['id'])
        for item in self.out_items:
            check_item(item, {self.label_id(label['suffix']) for label in LABELS})
        return self.out_items


def check_item(item, label_ids):
    """The Go adapter rules (adapters.go, addr_adapters.go, store.go) a create must pass, checked before any network."""
    problems = []
    if not ID_PATTERN.match(item['id']) or item['id'].startswith('addr-scene-'):
        problems.append('id')
    if not 1 <= len(item['title'].strip()) <= 300 or len(item['title']) > 300:
        problems.append('title length')
    if len(item['tags']) > 100 or any(not 1 <= len(t) <= 100 for t in item['tags']):
        problems.append('tags')
    if len(item['notes']) > 100000:
        problems.append('notes')
    if len(canonical(item)) > MAX_DOCUMENT_BYTES - 512:
        problems.append('document over 2 MiB - 512 B')
    if len(item['annotations']) > 5000:
        problems.append('over 5000 annotations')
    if item['adapter'] == 'addr-scene':
        if item.get('crop') is not None:
            problems.append('crop on addr-scene')
        source = item['source']
        for key in ('image', 'model'):
            ref = source.get(key) or {}
            if not re.fullmatch(r'[a-f0-9]{64}', ref.get('sha256', '')) or ref.get('url') != f"/api/v1/assets/{ref.get('sha256')}":
                problems.append(f'source.{key}')
        if not ID_PATTERN.match(source.get('sceneId', '')):
            problems.append('sceneId')
        seen = set()
        for mark in item['annotations']:
            addr = mark['properties']['addr']
            if mark['id'] in seen or addr['objectId'] != mark['id'] or not ID_PATTERN.match(mark['id']):
                problems.append(f"annotation id {mark['id']}")
            seen.add(mark['id'])
            if mark['labelId'] not in label_ids:
                problems.append(f"label {mark['labelId']}")
            if mark['geometry']['type'] != 'Polygon':
                problems.append('non-polygon')
            for ring in mark['geometry']['coordinates']:
                if len(ring) < 4 or ring[0] != ring[-1] or any(not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in ring):
                    problems.append(f"ring of {mark['id']}")
                elif _ring_area(ring[:-1]) < 1e-15:
                    problems.append(f"zero-area ring of {mark['id']}")
    elif item['adapter'] == 'map':
        if item['annotations']:
            problems.append('map annotations')
    else:
        problems.append('adapter')
    if problems:
        raise ReviewError(f"{item['id']}: {', '.join(sorted(set(problems)))}")


def request_sha(item):
    """Hash of the create request without the per-run push block (the 409 identity rule)."""
    doc = copy.deepcopy(item)
    doc.get('metadata', {}).get('addr', {}).pop('push', None)
    for key in ('revision', 'createdAt', 'updatedAt'):
        doc.pop(key, None)
    return sha256(canonical(doc))


def stage(root='.', items=DEFAULT_ITEMS, context=DEFAULT_CONTEXT, out=None, project=DEFAULT_PROJECT, drop_stale=False,
          full_frame=False, families=FAMILIES):
    _check_project(project)
    stager = Stager(root, items, context, project, drop_stale=drop_stale, full_frame=full_frame)
    built = stager.run(families)
    # Round trip: each crop ring + (x0, y0) reproduces the proposal ring.
    for item in built:
        meta = item['metadata']['addr']
        if item['adapter'] != 'addr-scene' or meta['family'] == 'veg':
            continue
        frame = json.loads(stager.blobs[item['source']['model']['sha256']]['bytes'])
        proposed = frame['objects'][-1]
        back = shift_ring(proposed['ring'], -meta['crop']['x0'], -meta['crop']['y0'])
        expected_sha = meta.get('proposedRingSha') or meta.get('joinedRingSha')
        source_line = _source_line(stager.items, meta)
        expected = source_line.get('proposed_ring') or source_line.get('joined_ring')
        if len(back) != len(expected) or any(abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6 for a, b in zip(back, expected, strict=True)):
            raise ReviewError(f"{item['id']}: crop round trip changed the proposal ring")
        holes = source_line.get('proposed_holes') or source_line.get('joined_holes')
        if ring_sha(expected, holes) != expected_sha:
            raise ReviewError(f"{item['id']}: proposal ring sha mismatch")
    stage_key = sha256(canonical([[i['id'], request_sha(i)] for i in built]))[:12]
    out_root = _repo_path(root, out or DEFAULT_STAGE_ROOT)
    bundle = out_root / stage_key
    for digest, blob in stager.blobs.items():
        if 'bytes' in blob:
            target = bundle / 'blobs' / digest
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob['bytes'])
    for item in built:
        write_json(bundle / 'items' / f"{item['id']}.json", item)
    write_json(bundle / 'project.json', {'id': project, **PROJECT})
    write_json(bundle / 'labels.json', [{'id': f"{project}-{label['suffix']}", 'projectId': project, 'name': label['name'], 'color': label['color']}
                                        for label in LABELS])
    counts = {}
    for item in built:
        counts[item['metadata']['addr']['family']] = counts.get(item['metadata']['addr']['family'], 0) + 1
    blobs = {d: {'contentType': b['contentType'], 'path': b['path'], 'bytes': b['size'],
                 **({'file': str(Path(b['file']).resolve().relative_to(Path(root).resolve()))} if 'file' in b else {})}
             for d, b in sorted(stager.blobs.items())}
    used = {}
    for item in built:
        if item['adapter'] == 'addr-scene':
            used[item['id']] = [item['source']['image']['sha256'], item['source']['model']['sha256']]
    manifest = {'schemaVersion': 1, 'tool': TOOL, 'set': SET, 'projectId': project, 'stageSha': stage_key, 'stagedAt': now_iso(),
                'inputs': {'items': {'path': str(items), 'sha256': sha_file(stager.items_path)},
                           'answerContext': {'path': str(context), 'sha256': sha_file(stager.context_path)}},
                'counts': counts, 'items': [i['id'] for i in built], 'requestSha256': {i['id']: request_sha(i) for i in built},
                'itemAssets': used, 'blobs': blobs, 'dropped': stager.dropped, 'excluded': stager.excluded,
                'bytes': {'new': sum(b['size'] for b in stager.blobs.values() if 'bytes' in b),
                          'sceneImages': sum(b['size'] for b in stager.blobs.values() if 'file' in b)}}
    write_json(bundle / 'manifest.json', manifest)
    (out_root / 'LATEST').write_text(stage_key + '\n')
    return {'stage': str(bundle), 'stageSha': stage_key, 'counts': counts, 'items': len(built), 'blobs': len(blobs),
            'newBytes': manifest['bytes']['new'], 'dropped': stager.dropped, 'excluded': stager.excluded}


def _source_line(items, meta):
    family = {'shadow': 'shadow', 'cut': 'cut', 'join': 'join', 'corner': 'corner', 'unit': 'unit', 'lot': 'lot', 'split': 'split', 'trace': 'trace'}[meta['family']]
    return next(x for x in items[family] if x['line'] == meta['line'])


# ------------------------------------------------------------------------------------------------------------------ HTTP

# The live origin sits behind Cloudflare, which answers urllib's default 'Python-urllib/3.x' User-Agent with 403 'error code: 1010'
# (browser-signature ban) before the Worker sees the request. Any named agent gets through.
USER_AGENT = f'{TOOL} (addr fieldwork review queue)'


class Client:
    def __init__(self, origin, token=None, timeout=180):
        self.origin = origin.rstrip('/')
        self.token = token
        self.timeout = timeout

    def request(self, method, path, body=None, headers=None):
        request = urllib.request.Request(self.origin + path, data=body, method=method,
                                         headers={'User-Agent': USER_AGENT, **dict(headers or {})})
        if self.token:
            request.add_header('Authorization', f'Bearer {self.token}')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers or {}), error.read()

    def rpc(self, method, data):
        guard_write(method, data)
        status, _, body = self.request('POST', f'/api/v1/rpc/{method}', canonical(data), {'Content-Type': 'application/json'})
        try:
            parsed = json.loads(body or b'{}')
        except ValueError:
            parsed = {'raw': body[:200].decode('utf8', 'replace')}
        return status, parsed

    def head_asset(self, digest):
        status, headers, _ = self.request('HEAD', f'/api/v1/assets/{digest}')
        lower = {k.lower(): v for k, v in headers.items()}
        return status, int(lower.get('content-length') or -1), lower.get('x-content-sha256')

    def put_asset(self, digest, data, content_type):
        status, _, body = self.request('PUT', f'/api/v1/assets/{digest}', data,
                                       {'Content-Type': content_type, 'Content-Length': str(len(data))})
        return status, body


READ_METHODS = frozenset({'GetInfo', 'GetProject', 'ListProjects', 'GetItem', 'ListItems', 'SearchItems', 'ListLabels', 'ListChanges',
                          'ExportProject'})
CREATE_KINDS = {'SaveProject': 'project', 'SaveLabel': 'label', 'SaveItem': 'item'}


# Request fields the review guards read. The server's encoding/json decoder matches keys case-insensitively (strings.EqualFold, so
# 'ProjectID', 'PROJECTID' and 'expectedReviſion' are the same fields) and keeps whichever spelling it decodes last.
GUARDED_FIELDS = ('project', 'item', 'label', 'expectedRevision', 'projectId', 'itemId', 'id')
_FOLDED_FIELDS = {name.lower(): name for name in GUARDED_FIELDS}
# Whitespace and control characters trimmed from both ends of a project id.
_ID_EDGES = re.compile(r'^[\s\x00-\x1f\x7f-\x9f]+|[\s\x00-\x1f\x7f-\x9f]+$')


def _go_fold(key):
    """A key as Go's strings.EqualFold compares it, for the guarded ASCII names: case-folded, with the long s (U+017F) and the
    Kelvin sign (U+212A) read as s and k."""
    return key.replace('\u017f', 's').replace('\u212a', 'k').lower()


def _spelled_fields(doc):
    """Keys in ``doc`` the server decodes as a guarded field but spelled differently (e.g. 'ProjectId'); empty when none."""
    return sorted(k for k in doc if isinstance(k, str) and k not in GUARDED_FIELDS and _go_fold(k) in _FOLDED_FIELDS)


def normalize_project_id(project):
    """The spelling every review guard compares, so no spelling of a review project slips past them: Unicode NFKC (fullwidth and
    other compatibility forms become ASCII), format characters (zero-width spaces, soft hyphens, a BOM) dropped, every dash
    (Unicode Pd and the minus sign) read as '-', case-folded, and whitespace or control characters trimmed from both ends. Same
    steps as migration.NormalizeProjectID (Go) and normalizeProjectId (deploy/annotation/src/mcp-guard.ts)."""
    text = unicodedata.normalize('NFKC', project)
    text = ''.join('-' if unicodedata.category(c) == 'Pd' or c == '\u2212' else c for c in text if unicodedata.category(c) != 'Cf')
    return _ID_EDGES.sub('', text.casefold())


def is_review_project(project):
    """Whether a project id names a review project (ends in -review), in any spelling (normalize_project_id)."""
    return isinstance(project, str) and normalize_project_id(project).endswith('-review')


def guard_write(method, data):
    """Allow independent SaveAgentReview assessments and the push's own creates: SaveProject / SaveLabel / SaveItem at
    expectedRevision 0 into a project whose id ends in -review. Ordinary updates, deletes, annotation writes and creates elsewhere
    are refused before any request, preserving the human decisions read back as human:fieldwork.
    A request field spelled any other way the server decodes (e.g. 'ProjectId' next to 'projectId') is refused too."""
    if method in READ_METHODS or method == 'SaveAgentReview':
        return
    data = data if isinstance(data, dict) else {}
    kind = CREATE_KINDS.get(method)
    spelled = _spelled_fields(data) + (_spelled_fields(data[kind]) if kind and isinstance(data.get(kind), dict) else [])
    if spelled:
        raise ReviewError(f"refusing {method}: field(s) {', '.join(map(repr, spelled))} spelled differently from "
                          f"{', '.join(_FOLDED_FIELDS[_go_fold(k)] for k in spelled)}; the server reads any spelling, so the guard "
                          'cannot tell which project or revision it would write')
    doc = data.get(kind) if kind else None
    project = (doc.get('id') if kind == 'project' else doc.get('projectId')) if isinstance(doc, dict) else None
    revision = data.get('expectedRevision')
    if kind and type(revision) is int and revision == 0 and isinstance(project, str):
        _check_project(project)
        return
    raise ReviewError(f'refusing {method} (expectedRevision {revision!r}): agents never edit a review project; this tool only creates '
                      'documents (expectedRevision 0) in a project whose id ends in -review')


def _check_project(project):
    """A review project id in its canonical spelling. 'mooroolbark' and every id not ending in -review are refused in any spelling
    (normalize_project_id), and so is a review id not already spelled the normalised way (e.g. 'Mooroolbark-Review',
    'mooroolbark-review ' or a fullwidth hyphen): a look-alike twin of the reviewer's project is never created or read."""
    normal = normalize_project_id(project) if isinstance(project, str) else ''
    if normal == 'mooroolbark' or not normal.endswith('-review') or project != normal or not ID_PATTERN.match(project):
        hint = f'; its normalised spelling is {normal!r}' if normal.endswith('-review') and project != normal else ''
        raise ReviewError(f'refusing project {project!r}: review items go only to a separate project whose id ends in -review, '
                          f'in lowercase ASCII{hint}')


LIVE_HOST = urllib.parse.urlsplit(DEFAULT_REMOTE_URL).hostname
_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
# A plain origin: ASCII http or https, a host of letters / digits / hyphens / dots (or [::1]), an optional port, an optional '/'.
PLAIN_ORIGIN = re.compile(rf'(?:http|https)://({_LABEL}(?:\.{_LABEL})*|\[::1\])(?::([0-9]{{1,5}}))?/?')


def request_host(origin):
    """The host urllib.request would connect to for ``origin``, normalised for comparison, or None when urllib refuses the URL.

    Computed the way the request is made: Request() unwraps '<URL:...>' / 'URL:' and percent-decodes the host; http.client drops
    the port; the socket layer IDNA-encodes the name (folding the ideographic, fullwidth and halfwidth dots to '.' and fullwidth
    letters to ASCII); DNS ignores case and a trailing dot."""
    try:
        host = urllib.request.Request(origin.rstrip('/') + '/api/v1/rpc/GetInfo').host
    except Exception:  # noqa: BLE001 - any spelling urllib cannot parse is no request
        return None
    if not host:
        return None
    host = host.rpartition('@')[2]
    if host.startswith('['):
        host = host[1:host.find(']')] if ']' in host else host[1:]
    elif host.count(':') == 1:
        host = host.partition(':')[0]
    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError:
        host = re.sub('[\u3002\uff0e\uff61]', '.', host)
    return host.rstrip('.').lower() or None


def guard_origin(origin, dry_run, token_file=None, root='.'):
    """Only a plain ASCII http(s) origin is accepted. Live writes need FIELDWORK_PUSH_ALLOW_LIVE=1; loopback needs no token; any
    other origin needs an explicit one. The live check compares the host urllib would actually connect to (request_host)."""
    live = request_host(origin) == LIVE_HOST or is_live_origin(origin)
    match = PLAIN_ORIGIN.fullmatch(origin) if isinstance(origin, str) and origin.isascii() else None
    if not match or int(match.group(2) or 0) > 65535 or request_host(origin) != match.group(1).strip('[]').lower():
        raise ReviewError(f'Refusing origin {origin!r}: not a plain ASCII http:// or https:// origin (letters, digits, hyphens and '
                          f"dots, an optional port){'; it names the live Fieldwork host' if live else ''}.")
    if live:
        if not dry_run and os.environ.get('FIELDWORK_PUSH_ALLOW_LIVE') != '1':
            raise ReviewError('Refusing to write to the live Fieldwork origin. Plan with --dry-run, or set FIELDWORK_PUSH_ALLOW_LIVE=1 '
                              'for the authorised push (start with --limit 1).')
        path = token_file or os.environ.get('FIELDWORK_TOKEN_FILE') or DEFAULT_TOKEN_FILE
        return os.environ.get('ANNOTATION_TOKEN') or _repo_path(root, path).read_text().strip()
    if is_loopback(origin):
        return None
    if token_file:
        return _repo_path(root, token_file).read_text().strip()
    if os.environ.get('ANNOTATION_TOKEN'):
        return os.environ['ANNOTATION_TOKEN']
    raise ReviewError('A custom Fieldwork URL needs --token-file or ANNOTATION_TOKEN (the live token is never sent elsewhere).')


def _default_dir(project):
    return Path('data/fieldwork-sync') / project


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReviewError(f'another review push or pull holds {path}') from error
        yield


def _bind_origin(directory, origin, project):
    path = directory / 'review' / 'origin.json'
    if path.exists():
        bound = json.loads(path.read_text())
        if bound.get('origin') != origin or bound.get('projectId') != project:
            raise ReviewError(f"{directory} holds receipts for {bound.get('origin')} / {bound.get('projectId')}, not {origin} / {project}; "
                              'pass a different --dir')
    else:
        write_json(path, {'origin': origin, 'projectId': project, 'boundAt': now_iso(), 'tool': TOOL})


def strip_volatile(doc):
    doc = copy.deepcopy(doc)
    for key in ('revision', 'createdAt', 'updatedAt'):
        doc.pop(key, None)
    doc.get('metadata', {}).get('addr', {}).pop('push', None)
    return doc


def same_document(a, b):
    """Equal as JSON values (numbers compared as floats: Go re-encodes 1.0 as 1)."""
    return json.loads(canonical(strip_volatile(a)), parse_int=float) == json.loads(canonical(strip_volatile(b)), parse_int=float)


def _latest_stage(root):
    latest = _repo_path(root, DEFAULT_STAGE_ROOT) / 'LATEST'
    if not latest.exists():
        raise ReviewError('no staged bundle; run stage first (task fieldwork:review:stage)')
    return _repo_path(root, DEFAULT_STAGE_ROOT) / latest.read_text().strip()


def _project_present(client, project):
    """Whether the project exists. The live server predates GetProject and answers an unknown method with the same 404 not_found
    as a missing project, so a 404 is confirmed with ListProjects (which every server has)."""
    status, body = client.rpc('GetProject', {'id': project})
    if status == 200:
        return True
    if status != 404:
        raise ReviewError(f'GetProject failed: {status} {body}')
    return any(doc.get('id') == project for doc in _list_all(client, 'ListProjects', {}, 'projects'))


def push(root='.', stage_dir=None, url=None, directory=None, project=DEFAULT_PROJECT, token_file=None, limit=None,
         families=None, dry_run=False, client=None, log=None):
    _check_project(project)
    origin = (url or os.environ.get('ANNOTATION_URL') or DEFAULT_REMOTE_URL).rstrip('/')
    token = guard_origin(origin, dry_run, token_file, root) if client is None else None
    client = client or Client(origin, token)
    bundle = _repo_path(root, stage_dir) if stage_dir else _latest_stage(root)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest['projectId'] != project:
        raise ReviewError(f"bundle was staged for {manifest['projectId']}, not {project}")
    directory = _repo_path(root, directory or _default_dir(project))
    receipts = directory / 'review'
    selected = [i for i in manifest['items'] if not families or json.loads((bundle / 'items' / f'{i}.json').read_text())['metadata']['addr']['family'] in families]
    if limit:
        selected = selected[:int(limit)]
    rid = run_id()
    result = {'runId': rid, 'origin': origin, 'projectId': project, 'stageSha': manifest['stageSha'], 'dryRun': dry_run,
              'counts': {}, 'conflicts': [], 'assets': {'present': 0, 'uploaded': 0, 'missing': 0}, 'items': {}}

    def count(key):
        result['counts'][key] = result['counts'].get(key, 0) + 1

    def write(method, data):
        guard_write(method, data)
        return client.rpc(method, data)

    def note(event):
        if log:
            log(event)
        if not dry_run:
            with (receipts / 'runs' / rid / 'progress.ndjson').open('a') as handle:
                handle.write(json.dumps({'at': now_iso(), **event}) + '\n')

    if not dry_run:
        _bind_origin(directory, origin, project)
        (receipts / 'runs' / rid).mkdir(parents=True, exist_ok=True)
        write_json(receipts / 'runs' / rid / 'plan.json', {'items': selected, 'stage': str(bundle), 'stageSha': manifest['stageSha']})
    with _lock(receipts / 'push.lock') if not dry_run else _nullcontext():
        # 1. assets (global, content-addressed): HEAD, PUT when missing, HEAD again.
        needed = []
        for item_id in selected:
            for digest in manifest['itemAssets'].get(item_id, []):
                if digest not in needed:
                    needed.append(digest)
        for digest in needed:
            blob = manifest['blobs'][digest]
            status, length, header = client.head_asset(digest)
            if status == 200 and length == blob['bytes'] and header in (None, digest):
                result['assets']['present'] += 1
                continue
            if dry_run:
                result['assets']['missing'] += 1
                continue
            data = (bundle / 'blobs' / digest).read_bytes() if 'file' not in blob else _repo_path(root, blob['file']).read_bytes()
            if sha256(data) != digest:
                raise ReviewError(f'blob {digest[:12]} bytes do not match their hash')
            status, body = client.put_asset(digest, data, blob['contentType'])
            if status not in (200, 201):
                raise ReviewError(f'asset {digest[:12]} upload failed: {status} {body[:200]!r}')
            status, length, header = client.head_asset(digest)
            if status != 200 or length != blob['bytes']:
                raise ReviewError(f'asset {digest[:12]} not verified after upload ({status}, {length})')
            result['assets']['uploaded'] += 1
            note({'event': 'asset', 'sha256': digest, 'bytes': blob['bytes']})
        # 2. project and labels (create once; an existing equal document is fine).
        project_doc = json.loads((bundle / 'project.json').read_text())
        if _project_present(client, project):
            count('project_present')
        elif dry_run:
            count('project_create')
        else:
            status, body = write('SaveProject', {'project': project_doc, 'expectedRevision': 0})
            if status != 200:
                raise ReviewError(f'SaveProject failed: {status} {body}')
            note({'event': 'project', 'id': project})
            count('project_created')
        labels = json.loads((bundle / 'labels.json').read_text())
        status, body = client.rpc('ListLabels', {'projectId': project, 'limit': 500})
        existing = {doc['id']: doc for doc in (body.get('labels') or [])} if status == 200 else {}
        for label in labels:
            if label['id'] in existing:
                if existing[label['id']]['name'] != label['name'] or existing[label['id']]['color'].lower() != label['color'].lower():
                    result['conflicts'].append({'label': label['id'], 'reason': 'label exists with another name or colour (left as is)'})
                count('label_present')
                continue
            if dry_run:
                count('label_create')
                continue
            status, body = write('SaveLabel', {'label': label, 'expectedRevision': 0})
            if status != 200:
                raise ReviewError(f"SaveLabel {label['id']} failed: {status} {body}")
            count('label_created')
        # 3. items in id order, create only.
        for item_id in selected:
            item = json.loads((bundle / 'items' / f'{item_id}.json').read_text())
            if item_id.startswith('addr-scene-') or item['projectId'] != project:
                raise ReviewError(f'refusing item {item_id}')
            req_sha = request_sha(item)
            receipt_path = receipts / 'items' / f'{item_id}.json'
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text())
                if receipt['requestSha256'] == req_sha:
                    count('skip_receipt')
                    result['items'][item_id] = 'skip_receipt'
                    continue
                result['conflicts'].append({'item': item_id, 'reason': 'conflict_changed_since_receipt: the staged item differs from '
                                            'the pushed one; it is never overwritten (a recomputed proposal gets a new id)'})
                count('conflict_changed_since_receipt')
                result['items'][item_id] = 'conflict_changed_since_receipt'
                continue
            if dry_run:
                status, body = client.rpc('GetItem', {'id': item_id})
                key = 'create' if status == 404 else 'exists_remote'
                count(key)
                result['items'][item_id] = key
                continue
            item['metadata']['addr']['push'] = {'runId': rid, 'pushedAt': now_iso(), 'tool': TOOL}
            status, body = write('SaveItem', {'item': item, 'expectedRevision': 0})
            outcome = 'created'
            if status == 409:
                status, body = client.rpc('GetItem', {'id': item_id})
                if status != 200:
                    raise ReviewError(f'GetItem {item_id} after 409 failed: {status}')
                remote = body['item']
                if same_document(remote, item):
                    outcome = 'skip_identical'
                    body = {'item': remote}
                else:
                    result['conflicts'].append({'item': item_id, 'reason': 'conflict_id_reuse: an item with this id exists with other '
                                                'content; nothing was overwritten'})
                    count('conflict_id_reuse')
                    result['items'][item_id] = 'conflict_id_reuse'
                    note({'event': 'conflict', 'item': item_id})
                    continue
            elif status != 200:
                raise ReviewError(f'SaveItem {item_id} failed: {status} {body}')
            document = body['item']
            meta = item['metadata']['addr']
            write_json(receipt_path, {'schemaVersion': 1, 'itemId': item_id, 'origin': origin, 'projectId': project, 'runId': rid,
                                      'pushedAt': now_iso(), 'revision': document['revision'], 'document': document,
                                      'requestSha256': req_sha, 'family': meta['family'], 'line': meta.get('line'),
                                      'stageSha': manifest['stageSha'], 'outcome': outcome})
            count(outcome)
            result['items'][item_id] = outcome
            note({'event': outcome, 'item': item_id, 'revision': document['revision']})
    result['status'] = 'planned' if dry_run else ('partial' if result['conflicts'] else 'pushed')
    if not dry_run:
        write_json(receipts / 'runs' / rid / 'result.json', {k: v for k, v in result.items() if k != 'items'})
    return result


@contextmanager
def _nullcontext():
    yield


# ------------------------------------------------------------------------------------------------------------------ pull

def _comment_free(text):
    return [line.strip() for line in (text or '').splitlines() if line.strip() and not line.strip().startswith('#')]


def _overrides(mark):
    return ((mark or {}).get('properties') or {}).get('addr', {}).get('overrides') or {}


def _coords(mark):
    return json.loads(canonical((mark or {}).get('geometry', {}).get('coordinates')), parse_int=float)


def _mark_changes(base, cur):
    """What the reviewer changed on one annotation: status / label / notes / kind / geometry and other override keys. An empty
    Review issues list counts as none ticked (ticked and unticked again); a null override is no change (the scene editor nulls
    roof_annotation on every edit of a non-building object)."""
    if base is None:
        return {'new_object': True}
    before, after = _overrides(base), _overrides(cur)
    changes = {}
    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if key == 'review_issues':
            old, new = old or None, new or None
        if json.dumps(old, sort_keys=True) != json.dumps(new, sort_keys=True):
            changes[key] = new
    if _coords(base) != _coords(cur):
        changes['geometry'] = True
    if (base.get('labelId') or '') != (cur.get('labelId') or ''):
        changes['labelId'] = cur.get('labelId')
    for key in ('removedFields', 'fieldworkReview'):
        changes.pop(key, None)
    return changes


# Object feedback controls this queue reads as a word or an exception. vegetation_type (the type picker) is read on vegetation
# items like a Label type word; on a shadow / cut / join outline it is listed under notStored and never stored.
READ_KEYS = frozenset({'status', 'notes', 'label', 'kind', 'geometry', 'new_object', 'vegetation_type'})
# Inspector controls that are never read as a word (AddrSceneEditor: Review issues, Visibility, 'Geometry reviewed in this layer').
# Next to confirmed / Approved they make a conflict (no decision); on their own they are listed unread; never dropped.
FLAG_KEYS = ('review_issues', 'visibility', 'geometry_reviewed')
# Keys the scene editor rewrites as a side effect of an outline or Class edit (updateSceneObject in src/lib/annotations/addr-scene.ts):
# an outline edit rewrites ring / holes / model and clears geometry_reviewed and the roof fields; a Class change clears
# geometry_reviewed and the roof feedback (and sets needs_adjustment, read as the status).
SIDE_EFFECTS = {
    'geometry': frozenset({'ring', 'holes', 'model', 'geometry_reviewed', 'roof_parts', 'roof_lines', 'roof_annotation',
                           'roof_geometry_source', 'roof_geometry_note'}),
    'kind': frozenset({'geometry_reviewed', 'roof_feedback', 'roof_line_edits', 'roof_parts', 'roof_lines', 'roof_annotation'}),
}


def _side_effects(changes):
    return frozenset().union(*(keys for cause, keys in SIDE_EFFECTS.items() if changes.get(cause)))


# The outline decision bar (AddrSceneEditor) saves the status and 'Geometry reviewed in this layer' together, as per-object overrides
# through updateSceneObject: Right = confirmed + geometry_reviewed true, Partly = needs_adjustment + false, Wrong = rejected + false.
# On the outline being answered (PROPOSED / JOINED, or a vegetation outline) that pair is the answer, not an Inspector flag.
BAR_GEOMETRY_REVIEWED = {'confirmed': True, 'needs_adjustment': False, 'rejected': False}


def _bar_write(changes, status):
    """Whether an outline's geometry_reviewed change is the decision bar's own write next to the status the outline now has."""
    return ('geometry_reviewed' in changes and status in BAR_GEOMETRY_REVIEWED
            and changes['geometry_reviewed'] is BAR_GEOMETRY_REVIEWED[status])


def _inspector_flags(changes, bar=False):
    """The Inspector controls changed on one outline that are not read as a word, in words (e.g. 'Review issues ticked: outline').
    bar: the geometry_reviewed change is the decision bar's write on the outline being answered (_bar_write), so it is no flag."""
    side = _side_effects(changes)
    flags = []
    if 'review_issues' in changes:
        issues = changes['review_issues']
        issues = issues if isinstance(issues, list) else [issues]
        flags.append('Review issues ticked: ' + ', '.join(str(x) for x in issues) if issues and issues != [None]
                     else 'Review issues cleared')
    if 'visibility' in changes:
        flags.append(f"Visibility set to {changes['visibility']}" if changes['visibility'] not in (None, '') else 'Visibility cleared')
    if 'geometry_reviewed' in changes and 'geometry_reviewed' not in side and not bar:
        flags.append("'Geometry reviewed in this layer' ticked" if changes['geometry_reviewed'] is True
                     else f"'Geometry reviewed in this layer' set to {json.dumps(changes['geometry_reviewed'])}")
    return flags


def _other_keys(changes):
    """Changed keys this queue neither reads nor lists as an Inspector flag (e.g. a 3D height edit: model): never dropped."""
    return sorted(set(changes) - READ_KEYS - set(FLAG_KEYS) - _side_effects(changes))


def _describe(changes):
    """One outline's changes, in words (unread lists)."""
    side = _side_effects(changes)
    words = [f'status={value}' if key == 'status' else f'Label={value!r}' if key == 'label'
             else f'vegetation_type={value!r}' if key == 'vegetation_type' else key
             for key, value in sorted(changes.items()) if key not in FLAG_KEYS or key in side]
    return words + _inspector_flags(changes)


def _base_status(mark):
    return ((mark or {}).get('properties') or {}).get('addr', {}).get('status')


def _edited_ring_frame(mark, meta):
    crop = meta['crop']
    rings = mark['geometry']['coordinates']
    return [[[p[0] * crop['width'] + crop['x0'], p[1] * crop['height'] + crop['y0']] for p in ring[:-1]] for ring in rings]


WORDS = {
    'shadow': {'confirmed': 'right', 'rejected': 'wrong', 'needs_adjustment': 'partly'},
    'cut': {'confirmed': 'right', 'rejected': 'wrong', 'needs_adjustment': 'unclear'},
    'join': {'confirmed': 'right', 'rejected': 'wrong', 'needs_adjustment': 'unclear'},
    'corner': {'confirmed': 'right', 'rejected': 'wrong', 'needs_adjustment': 'partly'},
    'unit': {'confirmed': 'right', 'rejected': 'wrong', 'needs_adjustment': 'partly'},
    'lot': {'confirmed': 'right', 'rejected': 'wrong', 'needs_adjustment': 'partly'},
    'split': {'confirmed': 'right', 'rejected': 'wrong', 'needs_adjustment': 'partly'},
    'trace': {'confirmed': 'right', 'rejected': 'wrong', 'needs_adjustment': 'partly'},
}
VEG_LABEL = re.compile(r'^\s*(grass|hedge|shrub|tree)\s*(?:[·.:\-]?\s*\d{1,4})?\s*$', re.I)


def _vegetation_type(value):
    """A vegetation_type override (the type picker) as a type word: the same words as a Label (grass, hedge, shrub, tree, any case),
    else None."""
    word = value.strip().lower() if isinstance(value, str) else None
    return word if word in VEG_TYPES else None


# Item-level Review > Status on a one-proposal item (shadows, cuts, joins): Approved = right, Rejected = wrong, read only when the
# PROPOSED (joins: JOINED) outline carries no contrary status. Vegetation items need per-outline words (listed as needs_words).
ITEM_VERDICT = {'approved': ('right', 'confirmed'), 'rejected': ('wrong', 'rejected')}
# Browser saves retain the shared workspace actor ('local-dev' on a --dev rehearsal server).
# SaveAgentReview stamps a distinct actor and cannot change human fields. Guards continue to refuse ordinary agent edits.
# Unknown actors are not credited to the human reviewer.
SHARED_ACTORS = ('workspace', 'local-dev')
ATTRIBUTION = ('Human decisions use the browser shared actor and existing status/annotation fields. Agent assessments use '
               'SaveAgentReview, append item.agentReviews with actor agent:fieldwork:<reviewer>, and are exported separately; '
               'they never enter human answer lines, polygon preview requests or training eligibility.')
BULK = re.compile(r'^(grass|hedge|shrub|tree|tree-or-shrub|tree or shrub|unclear|all) right(?: except ([\d,\s-]+))?$', re.I)


def _item_verdict(item, base_doc):
    """The item-level Review > Status the reviewer set ('approved' / 'rejected'; every item is pushed unreviewed), else None."""
    status = item.get('status')
    return status if status in ITEM_VERDICT and status != base_doc.get('status') else None


def _same(a, b):
    return json.loads(canonical(a), parse_int=float) == json.loads(canonical(b), parse_int=float)


def _what_changed(item, base_doc):
    """What differs from the pushed document, in words (listed for an item nothing could be read from)."""
    out = []
    if item.get('status') != base_doc.get('status'):
        out.append(f"item status {base_doc.get('status')} -> {item.get('status')}")
    out += [f'item {key}' for key in ('notes', 'title', 'tags', 'source', 'metadata') if not _same(item.get(key), base_doc.get(key))]
    base_marks = {m['id']: m for m in base_doc.get('annotations', [])}
    for mark in item.get('annotations', []):
        changes = _mark_changes(base_marks.get(mark['id']), mark)
        if changes:
            out.append(f"{mark['id']}: " + ', '.join(_describe(changes)))
    out += [f'{oid}: removed' for oid in sorted(set(base_marks) - {m['id'] for m in item.get('annotations', [])})]
    return out or ['revision only (no field differs)']


UNREAD_WHY = {
    'shadow': 'nothing to read: the PROPOSED outline has no status, notes or edit, and the item status is not Approved or Rejected',
    'cut': 'nothing to read: the PROPOSED outline has no status, notes or edit, and the item status is not Approved or Rejected',
    'join': 'nothing to read: the JOINED outline has no status, notes or edit, and the item status is not Approved or Rejected',
    'veg': 'nothing to read: no outline status, Label type word or Fieldwork notes line, and the item status is not Approved or Rejected',
    'round5': 'nothing to read: Status is not Approved or Rejected and there are no Review notes',
    'corner': 'nothing to read: the PROPOSED outline has no status, notes or edit, and the item status is not Approved or Rejected',
    'unit': 'nothing to read: the PROPOSED split has no status or notes, and the item status is not Approved or Rejected',
    'lot': 'nothing to read: the PROPOSED clipped outline has no status or notes, and the item status is not Approved or Rejected',
    'split': 'nothing to read: the PROPOSED split has no status or notes, and the item status is not Approved or Rejected',
    'trace': 'nothing to read: the PROPOSED trace has no status, notes or edit, and the item status is not Approved or Rejected',
}


def _unread(item, base_doc, meta, fw, why):
    return {'family': meta.get('family'), 'line': meta.get('line'), 'itemId': item['id'], 'sceneId': meta.get('sceneId'), 'word': None,
            'unread': {'why': why, 'changed': _what_changed(item, base_doc), 'itemStatus': item.get('status')}, 'fieldwork': fw}


def read_item(item, receipt, provenance):
    """One review item against its push receipt -> list of records (dicts). Pure.

    A changed item always yields at least one record: a decision, a conflict, a needs-words note or an ``unread`` entry that
    says what changed and why nothing was read; nothing the reviewer changed is dropped silently. ``provenance['changeActors']``
    (set by pull) exclude append-only agent review events. Human UI writes use SHARED_ACTORS; unknown actors or
    changes with no change record are listed unread and are not credited to the reviewer. Agent assessments are exported separately."""
    base_doc = receipt['document']
    if item['revision'] == base_doc['revision'] and same_document(item, base_doc):
        return []
    meta = item['metadata']['addr']
    fw = {'source': 'human:fieldwork', **provenance, 'itemId': item['id'], 'revision': item['revision']}
    actors = provenance.get('changeActors')
    if actors is not None:
        human = copy.deepcopy(item)
        human.pop('agentReviews', None)
        base_human = copy.deepcopy(base_doc)
        base_human.pop('agentReviews', None)
        if actors and all(a.startswith('agent:fieldwork:') for a in actors) and same_document(human, base_human):
            return []
        others = [a for a in actors if a not in SHARED_ACTORS and not a.startswith('agent:fieldwork:')]
        if not actors:
            return [_unread(item, base_doc, meta, fw, 'no change record after the push says who changed it; not credited to the reviewer')]
        if others:
            return [_unread(item, base_doc, meta, fw, f"changed by {', '.join(others)}, not the shared workspace token the reviewer's "
                            'UI writes with; not credited to the reviewer')]
    return _read_family(item, base_doc, meta, fw) or [_unread(item, base_doc, meta, fw, UNREAD_WHY[meta['family']])]


def _read_family(item, base_doc, meta, fw):
    family = meta['family']
    base_marks = {m['id']: m for m in base_doc.get('annotations', [])}
    notes_lines = _comment_free(item.get('notes'))
    base_notes = _comment_free(base_doc.get('notes'))
    reviewer_notes = [line for line in notes_lines if line not in base_notes]
    out = []
    if family == 'round5':
        status = item.get('status')
        word = {'approved': meta['approved'], 'rejected': meta['rejected']}.get(status)
        if word or reviewer_notes:
            out.append({'family': 'round5', 'line': meta['line'], 'itemId': item['id'], 'word': word, 'itemStatus': status,
                        'notes': '\n'.join(reviewer_notes), 'fieldwork': fw})
        return out
    if family in ('shadow', 'cut', 'join', 'corner', 'unit', 'lot', 'split', 'trace'):
        target_prefix = 'join-' if family == 'join' else 'new-'
        read_name = 'JOINED' if family == 'join' else 'PROPOSED'
        exceptions, target, flags, not_stored = [], None, [], []
        for mark in item.get('annotations', []):
            changes = _mark_changes(base_marks.get(mark['id']), mark)
            if not changes:
                continue
            is_target = mark['id'].startswith(target_prefix)
            name = read_name if is_target else 'outline'
            # The decision bar's Right / Partly / Wrong on the PROPOSED (JOINED) outline writes geometry_reviewed with the status:
            # the answer, not a flag. 'Geometry reviewed' on any other outline stays a listed flag.
            bar = is_target and _bar_write(changes, changes.get('status', _base_status(mark)))
            flags += [f"{name} {mark['id']}: {flag}" for flag in _inspector_flags(changes, bar)]
            if 'vegetation_type' in changes:
                not_stored.append(f"{name} {mark['id']}: vegetation type {changes['vegetation_type']!r} (types are read only on "
                                  'vegetation items; not stored)')
            if is_target:
                target = (mark, changes, bar)
                other = _other_keys(changes)
                if other:
                    exceptions.append(f"the {read_name} outline also changed {', '.join(other)} (not read here)")
                continue
            rest = sorted(key for key in changes if (key not in FLAG_KEYS or key in _side_effects(changes)) and key != 'vegetation_type')
            if rest:
                exceptions.append(f"{mark['id']} changed ({', '.join(rest)}); only the {read_name} outline is read")
        present = {m['id'] for m in item.get('annotations', [])}
        exceptions += [f'{oid} removed; only the {read_name} outline is read' for oid in sorted(set(base_marks) - present)]
        decision = {'family': family, 'line': meta['line'], 'itemId': item['id'], 'sceneId': meta['sceneId'],
                    'objectIds': meta['objectIds'], 'notes': '\n'.join(reviewer_notes), 'fieldwork': fw}
        if family == 'join':
            decision.update(featureId=meta['featureId'], members=meta['members'], joinedRingSha=meta['joinedRingSha'])
        else:
            decision.update(proposalId=meta['proposalId'], proposedRingSha=meta['proposedRingSha'], kind=meta['objectKind'])
        if family == 'cut':
            decision.update(wholeRoof=meta.get('wholeRoof', False), sameOutlineAs=meta.get('sameOutlineAs', []))
        verdict = _item_verdict(item, base_doc)
        if verdict:
            decision['itemStatus'] = verdict
        word, status, edited = None, None, False
        if target:
            mark, changes, bar = target
            status = changes.get('status', _base_status(mark))
            decision['status'] = status
            if bar:
                decision['geometryReviewed'] = changes['geometry_reviewed']
            decision['objectNotes'] = changes.get('notes')
            if changes.get('label') is not None:
                decision['label'] = changes['label']
            if changes.get('kind'):
                exceptions.append(f"class changed to {changes['kind']}")
            if changes.get('new_object'):
                exceptions.append('a new object was drawn')
            if changes.get('geometry'):
                edited = True
                decision['edited'] = True
                decision['editedRingFrame'] = _edited_ring_frame(mark, meta)
                word = 'edited'
            elif status == 'superseded':
                exceptions.append('set to superseded')
            else:
                word = WORDS[family].get(status)
        conflicts = []
        if verdict:
            # Item Approved = right, Rejected = wrong, unless the outline says otherwise: then no decision, a conflict.
            item_word, agreeing = ITEM_VERDICT[verdict]
            if edited or status not in (None, 'candidate', agreeing):
                conflicts.append(f"item Review status {verdict.title()} but the {read_name} outline "
                                 f"{'was edited' if edited else f'is {status}'}: no decision recorded; make the two agree")
                word = None
            else:
                decision['how'] = 'item status' if status in (None, 'candidate') else 'object status and item status'
                word = item_word
        if flags:
            # A ticked Review issue, a Visibility or 'Geometry reviewed' change next to confirmed / Approved: no decision, a conflict.
            decision['flags'] = flags
            positive = [p for p in (status == 'confirmed' and f'the {read_name} outline is confirmed',
                                    verdict == 'approved' and 'item Review status Approved') if p]
            if positive:
                conflicts.append(f"{' and '.join(positive)} but {'; '.join(flags)}: no decision recorded; untick it, or set the "
                                 'status that says what is wrong')
                word = None
        if conflicts:
            decision['conflict'] = '; '.join(conflicts)
        if exceptions:
            decision['exceptions'] = exceptions
            word = None if word != 'edited' else word
        decision['word'] = word
        if not_stored:
            decision['notStored'] = not_stored
        if flags and not word and not conflicts:
            decision['unread'] = {'why': f"{'; '.join(flags)}, with no status to read: nothing recorded",
                                  'changed': _what_changed(item, base_doc), 'itemStatus': item.get('status')}
        elif not_stored and not word and not conflicts and not exceptions:
            decision['unread'] = {'why': f"{'; '.join(not_stored)}, with no status to read: nothing recorded",
                                  'changed': _what_changed(item, base_doc), 'itemStatus': item.get('status')}
        if word or exceptions or reviewer_notes or decision.get('objectNotes') or conflicts or flags or not_stored:
            out.append(decision)
        return out
    # vegetation
    lines = {k: int(v) for k, v in meta['lines'].items()}
    by_line = {v: k for k, v in lines.items()}
    groups = {m['id']: (m['labelId'] or '').split('-veg-')[-1] for m in base_doc['annotations']}
    must_skip = set(meta.get('bulkMustSkip') or [])
    live_status = meta.get('liveStatus') or {}
    verdict = _item_verdict(item, base_doc)
    touched = set()
    for mark in item.get('annotations', []):
        base_mark = base_marks.get(mark['id'])
        changes = _mark_changes(base_mark, mark)
        if not changes:
            continue
        touched.add(mark['id'])
        n = lines.get(mark['id'])
        group = groups.get(mark['id'], 'unclear')
        decision = {'family': 'veg', 'line': n, 'itemId': item['id'], 'sceneId': meta['sceneId'], 'objectId': mark['id'],
                    'proposed': group, 'fieldwork': fw}
        status = changes.get('status', _base_status(mark))
        decision['status'] = status
        if changes.get('notes') is not None:
            decision['objectNotes'] = changes['notes']
        exceptions = []
        if changes.get('new_object') or n is None:
            exceptions.append('a new object was drawn (not read)')
        if changes.get('geometry') or changes.get('kind'):
            exceptions.append('outline or class changed here; this queue stores types only')
        other = _other_keys(changes)
        if other:
            exceptions.append(f"also changed {', '.join(other)} (not read here)")
        # A type comes from a Label word or from the type picker (vegetation_type), read the same way; both set and different is a
        # listed conflict that stores nothing.
        typed, how, sources, type_conflict = None, None, [], None
        if changes.get('label') is not None:
            match = VEG_LABEL.match(changes['label'])
            if match:
                sources.append(('label', match.group(1).lower(), f'Label typed {changes["label"]!r}'))
                decision['label'] = changes['label']
            else:
                exceptions.append(f"Label {changes['label']!r} not understood (type one word: grass, hedge, shrub or tree)")
        if changes.get('vegetation_type') is not None:
            picked = _vegetation_type(changes['vegetation_type'])
            if picked:
                sources.append(('vegetation_type', picked, f"vegetation type {changes['vegetation_type']!r}"))
                decision['vegetationType'] = changes['vegetation_type']
            else:
                exceptions.append(f"vegetation type {changes['vegetation_type']!r} not understood (one of grass, hedge, shrub or tree)")
        if len({word for _, word, _ in sources}) > 1:
            type_conflict = (f"veg line {n} ({mark['id']}): {' but '.join(text for _, _, text in sources)}: nothing stored; make the "
                             'Label and the vegetation type agree')
        elif sources:
            typed, how = sources[0][1], ' and '.join(key for key, _, _ in sources)
        if exceptions:
            decision.update(exceptions=exceptions, word=None)
        elif type_conflict:
            decision.update(word=None, conflict=type_conflict)
        elif typed and status in ('rejected', 'needs_adjustment'):
            # A type next to Wrong or Not sure contradicts itself: the reviewer picked a type, then said the outline is not that or is
            # unclear. Neither word is stored; the line is listed and the agent asks.
            said = {'rejected': 'status rejected (Wrong)', 'needs_adjustment': 'status needs_adjustment (Not sure)'}[status]
            decision.update(word=None, conflict=(f"veg line {n} ({mark['id']}): {' and '.join(text for _, _, text in sources)} but {said}: "
                                                 'nothing stored; keep the type and answer Right, or undo the type'))
        elif typed:
            decision.update(word=typed, how=how)
        elif status == 'confirmed':
            if group in VEG_TYPES:
                decision.update(word=group, how='confirmed')
            else:
                decision.update(word=None, typeNeeded=True,
                                exceptions=[f"confirmed on a '{VEG_GROUP_NAMES[group]}' outline: no type stored; type a word in the Label"])
        elif status == 'rejected':
            decision.update(word='wrong')
        elif status == 'needs_adjustment':
            decision.update(word='unclear')
        elif status == 'superseded':
            decision.update(word=None, exceptions=['set to superseded'])
        else:
            decision.update(word=None)
        # The decision bar's Right (confirmed + geometry_reviewed true) is read as confirmed, not as a flag.
        bar = _bar_write(changes, status)
        if bar:
            decision['geometryReviewed'] = changes['geometry_reviewed']
        flags = _inspector_flags(changes, bar)
        if flags:
            # Per outline as on the one-proposal items: a typed / confirmed outline (or an Approved item) with a Review issue ticked,
            # a Visibility or 'Geometry reviewed' change stores nothing and is a listed conflict.
            decision['flags'] = flags
            positive = [p for p in (typed and ' and '.join(text for _, _, text in sources), status == 'confirmed' and 'status confirmed',
                                    verdict == 'approved' and 'item Review status Approved') if p]
            if positive:
                flagged = (f"veg line {n} ({mark['id']}): {' and '.join(positive)} but {'; '.join(flags)}: nothing stored; untick it, "
                           'or type the right word in the Label')
                decision.update(word=None, conflict='; '.join(c for c in (decision.get('conflict'), flagged) if c))
        if not (decision.get('word') or decision.get('exceptions') or decision.get('objectNotes') or decision.get('conflict')):
            # Changed but nothing read (a flag alone, a status set back to candidate): listed, never dropped.
            decision['unread'] = {'why': (f"{'; '.join(flags)}, with no type or status to read: nothing stored" if flags
                                          else 'changed, but no type, status or notes to read: nothing stored'),
                                  'changed': [f"{mark['id']}: " + ', '.join(_describe(changes))], 'itemStatus': item.get('status')}
        out.append(decision)
    # bulk lines in Fieldwork notes
    for text in reviewer_notes:
        match = BULK.match(text.strip())
        if not match:
            out.append({'family': 'veg', 'line': None, 'itemId': item['id'], 'sceneId': meta['sceneId'], 'word': None,
                        'notUnderstood': text, 'fieldwork': fw})
            continue
        which = match.group(1).lower().replace('tree or shrub', 'tree-or-shrub')
        try:
            excepted = expand_lines(match.group(2)) if match.group(2) else set()
        except ValueError:
            out.append({'family': 'veg', 'line': None, 'itemId': item['id'], 'sceneId': meta['sceneId'], 'word': None,
                        'notUnderstood': text, 'fieldwork': fw})
            continue
        if which in ('tree-or-shrub', 'unclear'):
            out.append({'family': 'veg', 'line': None, 'itemId': item['id'], 'sceneId': meta['sceneId'], 'word': None,
                        'notUnderstood': text, 'reason': f"'{VEG_GROUP_NAMES[which]}' has no storable type; type a word in each Label",
                        'fieldwork': fw})
            continue
        wanted = VEG_TYPES if which == 'all' else (which,)
        skipped = []
        for n in sorted(excepted - set(by_line)):
            skipped.append({'line': n, 'why': 'not in this item'})
        for n in sorted(lines.values()):
            oid = by_line[n]
            group = groups.get(oid)
            if group not in wanted:
                if which == 'all' and group in ('tree-or-shrub', 'unclear'):
                    skipped.append({'line': n, 'why': f'{VEG_GROUP_NAMES[group]}: no storable type'})
                continue
            why = ('you named it in except' if n in excepted else 'you marked this outline here' if oid in touched
                   else f"outline is {live_status.get(oid)} in your scenes" if live_status.get(oid, 'candidate') != 'candidate'
                   else 'the agent did not read it as right' if n in must_skip else None)
            if why:
                skipped.append({'line': n, 'why': why})
                continue
            out.append({'family': 'veg', 'line': n, 'itemId': item['id'], 'sceneId': meta['sceneId'], 'objectId': oid,
                        'proposed': group, 'word': group, 'how': f'bulk: {text}', 'fieldwork': fw})
        out.append({'family': 'veg', 'line': None, 'itemId': item['id'], 'sceneId': meta['sceneId'], 'word': None,
                    'bulk': text, 'skipped': skipped, 'fieldwork': fw})
    present = {m['id'] for m in item.get('annotations', [])}
    for oid in sorted(set(base_marks) - present):
        out.append({'family': 'veg', 'line': lines.get(oid), 'itemId': item['id'], 'sceneId': meta['sceneId'], 'objectId': oid,
                    'proposed': groups.get(oid), 'word': None, 'removed': True, 'exceptions': ['outline removed (not read)'],
                    'fieldwork': fw})
    if verdict:
        # A vegetation item has many outlines and no single proposal: the item status alone stores nothing.
        answered = {d['objectId'] for d in out if d.get('objectId') and d.get('word')}
        untyped = sorted(n for oid, n in lines.items() if oid not in answered)
        read = sum(1 for d in out if (d.get('objectId') and not d.get('removed') and not d.get('unread')) or d.get('bulk'))
        typed_marks = [d for d in out if d.get('objectId') and d.get('word') in VEG_TYPES]
        needed = ("per-outline types (Object review status, or a type word in the Label) or a Fieldwork notes line such as "
                  "'all right' or 'grass right except 12' are needed")
        if verdict == 'rejected' and typed_marks:
            # Rejected next to typed outlines contradicts them: no type stored for this item, one listed conflict (never dropped).
            counts = {}
            for d in typed_marks:
                counts[d['word']] = counts.get(d['word'], 0) + 1
                d.update(typedWord=d['word'], word=None, withheld='item Review status Rejected: see the item conflict')
            numbers = compact_lines([d['line'] for d in typed_marks if d.get('line') is not None])
            out.append({'family': 'veg', 'line': None, 'itemId': item['id'], 'sceneId': meta['sceneId'], 'word': None,
                        'itemStatus': verdict, 'conflictLines': numbers,
                        'conflict': (f"item Review status Rejected but {len(typed_marks)} outline mark(s) here are typed "
                                     f"({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}; lines {numbers}): no type stored for "
                                     'this item; make the two agree (set the item status back, or clear the types)'),
                        'fieldwork': fw})
        elif verdict == 'approved':
            for d in typed_marks:
                d['itemStatus'] = verdict
        if not read:
            why = f'item Review status {verdict.title()} with no per-outline marks: nothing stored; {needed}'
        elif untyped:
            why = (f"item Review status {verdict.title()} next to {read} outline mark(s) or notes line(s), "
                   f"{'held as a conflict' if verdict == 'rejected' and typed_marks else 'which were read'}; the item status itself stores "
                   f'nothing and {len(untyped)} outline(s) here still have no word: {needed}')
        else:
            why = None
        if why:
            out.append({'family': 'veg', 'line': None, 'itemId': item['id'], 'sceneId': meta['sceneId'], 'word': None,
                        'itemStatus': verdict, 'needsWords': why, 'untypedLines': compact_lines(untyped), 'fieldwork': fw})
    return out


def resolve(decisions, context):
    """Apply the card's rules to the per-line words: what is written, held or only recorded."""
    whole = {int(n) for n in context['cuts']['whole_other']}
    shadow_right = sorted(d['line'] for d in decisions if d['family'] == 'shadow' and d.get('word') == 'right')
    shadow_partly = sorted(d['line'] for d in decisions if d['family'] == 'shadow' and d.get('word') == 'partly')
    cut_right = {d['line']: d for d in decisions if d['family'] == 'cut' and d.get('word') == 'right'}
    held, resolved = [], []
    for n in sorted(cut_right):
        if n in whole:
            held.append({'line': n, 'why': 'whole-roof copy: needs the shape check and the separate "cuts: whole roofs too" choice'})
    writable = {n: d for n, d in cut_right.items() if n not in whole}
    if 2 in writable and 3 in writable:
        resolved.append({'line': 3, 'why': 'lines 2 and 3 both right on roof 55ca4c5c: line 2 is written (card rule)'})
        writable.pop(3)
    by_outline = {}
    for n, d in writable.items():
        by_outline.setdefault((d['sceneId'], d['objectIds'][0]), []).append(n)
    cut_written = []
    for numbers in sorted(by_outline.values(), key=min):
        if len(numbers) > 1:
            held.append({'lines': sorted(numbers), 'why': 'several fixes right on one outline: held for one composed preview '
                         '(applied in line order), never one at a time'})
        else:
            cut_written.append(numbers[0])
    return {'shadows': {'write': shadow_right, 'retrim': shadow_partly},
            'cuts': {'write': sorted(cut_written), 'held': held, 'resolved': resolved}}


def answer_lines(decisions):
    """The review pages' chat grammar, one line per part that has marks."""
    lines = []

    def marks(family, words):
        parts = []
        for word in words:
            numbers = [d['line'] for d in decisions if d['family'] == family and d.get('word') == word]
            if numbers:
                parts.append(f'{word}: {compact_lines(numbers)}')
        return parts

    for family, part, words in (('shadow', 'shadows', ('right', 'partly', 'wrong')), ('cut', 'cuts', ('right', 'wrong', 'unclear')),
                                ('veg', 'veg', (*VEG_TYPES, 'wrong', 'unclear')), ('join', 'joins', ('right', 'wrong', 'unclear')), ('corner', 'corners', ('right', 'partly', 'wrong')),
                                ('unit', 'units', ('right', 'partly', 'wrong')), ('lot', 'lots', ('right', 'partly', 'wrong')),
                                ('split', 'splits', ('right', 'partly', 'wrong')), ('trace', 'traces', ('right', 'partly', 'wrong'))):
        parts = marks(family, words)
        if parts:
            lines.append(f"{part}: {' / '.join(parts)}")
    round5 = sorted((d for d in decisions if d['family'] == 'round5' and d.get('word')), key=lambda d: d['line'])
    if round5:
        defaults = {q['n']: q['approved'] for q in ROUND5}
        if len(round5) == 3 and all(d['word'] == defaults[d['line']] for d in round5):
            lines.append('round5: yes to defaults')
        else:
            lines.append('round5: ' + ' / '.join(d['word'] for d in round5))
    return lines


def check_answers(text, context, pushed_lines):
    """Grammar and card-rule check for answers written from Fieldwork clicks (not the page's suggested-answer check).
    pushed_lines: {family: set(line numbers that have a Fieldwork item)}. Returns what each line writes; raises on a break."""
    out = []
    whole = {int(n) for n in context['cuts']['whole_other']}
    for raw in text.splitlines():
        line = ' '.join(raw.split())
        if not line or line.startswith('#'):
            continue
        part, _, rest = line.partition(':')
        segments = [s.strip() for s in rest.strip().split(' / ')]
        if part == 'round5':
            allowed = {q['approved'] for q in ROUND5} | {q['rejected'] for q in ROUND5}
            assert segments == ['yes to defaults'] or all(s in allowed for s in segments), (line, 'unknown round5 word')
            out.append(f'round5: {" / ".join(segments)}')
            continue
        family = {'shadows': 'shadow', 'cuts': 'cut', 'veg': 'veg', 'joins': 'join', 'corners': 'corner', 'units': 'unit', 'lots': 'lot', 'splits': 'split', 'traces': 'trace'}.get(part)
        assert family, (line, 'unknown part')
        words = {'shadow': ('right', 'partly', 'wrong', 'unclear'), 'cut': ('right', 'wrong', 'unclear'),
                 'join': ('right', 'wrong', 'unclear'), 'veg': (*VEG_TYPES, 'wrong', 'unclear'),
                 'corner': ('right', 'partly', 'wrong'), 'unit': ('right', 'partly', 'wrong'),
                 'lot': ('right', 'partly', 'wrong'), 'split': ('right', 'partly', 'wrong'),
                 'trace': ('right', 'partly', 'wrong')}[family]
        seen, marked = {}, {}
        for seg in segments:
            m = re.fullmatch(r'([a-z]+): ([\d,\s-]+)', seg)
            assert m and m.group(1) in words, (line, f'unparsed segment {seg!r}')
            numbers = expand_lines(m.group(2))
            assert numbers <= pushed_lines[family], (line, 'line numbers with no Fieldwork item', sorted(numbers - pushed_lines[family]))
            for n in numbers:
                assert n not in seen, (line, f'line {n} marked twice')
                seen[n] = m.group(1)
            marked.setdefault(m.group(1), set()).update(numbers)
        if family == 'cut':
            right = marked.get('right', set())
            assert not (right & whole), (line, 'whole-roof copies are held, never in a cuts line', sorted(right & whole))
            written = right - ({3} if {2, 3} <= right else set())
            out.append(f'cuts: {len(written)} right written (or held per outline), {len(right) - len(written)} resolved to line 2')
        elif family == 'veg':
            typed = sum(len(marked.get(t, ())) for t in VEG_TYPES)
            out.append(f'veg: {typed} typed, {len(marked.get("wrong", ()))} wrong, {len(marked.get("unclear", ()))} unclear')
        else:
            out.append(f'{part}: ' + ', '.join(f'{len(v)} {k}' for k, v in sorted(marked.items())))
    return out


def _list_all(client, method, data, key):
    docs, cursor = [], ''
    while True:
        status, body = client.rpc(method, {**data, 'cursor': cursor, 'limit': 500})
        if status != 200:
            raise ReviewError(f'{method} failed: {status} {body}')
        docs += body.get(key) or []
        cursor = body.get('nextCursor') or ''
        if not cursor:
            return docs


def _changes(client, project):
    changes, cursor = [], ''
    while True:
        status, body = client.rpc('ListChanges', {'projectId': project, 'cursor': cursor, 'limit': 500})
        if status != 200:
            raise ReviewError(f'ListChanges failed: {status} {body}')
        changes += body.get('changes') or []
        cursor = body.get('nextCursor') or cursor
        if not body.get('hasMore'):
            return changes, cursor


def agent_review_subject(item, annotation_id=None):
    """The canonical SaveAgentReview evidence binding; human status/checks are independent."""
    marks = []
    for mark in item.get('annotations', []):
        if annotation_id and mark['id'] != annotation_id:
            continue
        addr = (mark.get('properties') or {}).get('addr') or {}
        overrides = addr.get('overrides') or {}
        entry = {'id': mark['id'], 'labelId': mark.get('labelId') or '', 'geometry': mark['geometry']}
        for key in ('kind', 'label', 'structure_kind', 'vegetation_type'):
            entry[key] = overrides[key] if key in overrides else addr.get(key)
        marks.append(entry)
    subject = {'adapter': item['adapter'], 'source': item['source'], 'title': item['title'], 'annotations': marks,
               'crop': item.get('crop')}
    if not annotation_id:
        subject['notes'] = item.get('notes', '')
    return subject


def read_agent_reviews(item, history):
    """Keep agent assessments and history separate, with their server-stamped event and current evidence state."""
    reviews = item.get('agentReviews') or []
    superseded = {r.get('supersedes') for r in reviews}
    out = []
    for review in reviews:
        revision = review.get('basedOnRevision', 0) + 1
        event = next((c for c in history if c.get('revision') == revision and
                      c.get('actor') == f"agent:fieldwork:{review.get('reviewer')}" and
                      any(r == review for r in (c.get('document') or {}).get('agentReviews', []))), None)
        verified = review.get('source') == 'agent:fieldwork' and review.get('humanApproval') is False and \
            review.get('trainingEligible') is False and event is not None
        out.append({**review, 'itemId': item['id'], 'family': item.get('metadata', {}).get('addr', {}).get('family'),
                    'current': verified and _same(review.get('subject'), agent_review_subject(item, review.get('annotationId'))),
                    'superseded': review['id'] in superseded, 'provenanceVerified': verified,
                    'actor': event.get('actor') if event else None, 'cursor': event.get('cursor') if event else None,
                    'humanItemStatus': item.get('status')})
    return out


def pull(root='.', url=None, directory=None, project=DEFAULT_PROJECT, out=None, token_file=None, context=DEFAULT_CONTEXT,
         client=None):
    _check_project(project)
    origin = (url or os.environ.get('ANNOTATION_URL') or DEFAULT_REMOTE_URL).rstrip('/')
    token = guard_origin(origin, True, token_file, root) if client is None else None
    client = client or Client(origin, token)
    directory = _repo_path(root, directory or _default_dir(project))
    out = _repo_path(root, out or DEFAULT_PULL_OUT)
    resolved_out = out.resolve()
    for forbidden in ('data/scenes', 'data/fieldwork-sync/mooroolbark'):
        guarded = (Path(root) / forbidden).resolve()
        if resolved_out == guarded or guarded in resolved_out.parents:
            raise ReviewError(f'refusing to write pull output under {forbidden}')
    bound = directory / 'review' / 'origin.json'
    if not bound.exists():
        raise ReviewError(f'{directory} has no review receipts; nothing was pushed from here')
    binding = json.loads(bound.read_text())
    if binding['origin'] != origin or binding['projectId'] != project:
        raise ReviewError(f"receipts in {directory} are for {binding['origin']} / {binding['projectId']}")
    ctx = json.loads(_repo_path(root, context).read_text())
    receipts = {p.stem: json.loads(p.read_text()) for p in (directory / 'review' / 'items').glob('*.json')}
    items = _list_all(client, 'ListItems', {'projectId': project}, 'items')
    changes, cursor = _changes(client, project)
    history = {}
    for change in changes:
        if change.get('kind') == 'item':
            history.setdefault(change['id'], []).append(change)
    with _lock(directory / 'review' / 'pull.lock'):
        decisions, agent_reviews, unknown, missing = [], [], [], sorted(set(receipts) - {i['id'] for i in items})
        for item in sorted(items, key=lambda i: i['id']):
            meta = (item.get('metadata') or {}).get('addr') or {}
            if meta.get('kind') != 'review-proposal' or meta.get('set') != SET:
                unknown.append(item['id'])
                continue
            receipt = receipts.get(item['id'])
            if not receipt:
                unknown.append(item['id'])
                continue
            agent_reviews += read_agent_reviews(item, history.get(item['id'], []))
            # Agent writes append only agentReviews. Exclude those events from
            # human provenance, even when the latest revision is an agent save.
            all_after = [c for c in history.get(item['id'], []) if int(c.get('revision') or 0) > int(receipt['revision'])]
            after = [c for c in all_after if not (c.get('actor') or '').startswith('agent:fieldwork:')]
            human_item = copy.deepcopy(item)
            human_item.pop('agentReviews', None)
            base_human = copy.deepcopy(receipt['document'])
            base_human.pop('agentReviews', None)
            if not after and all_after and same_document(human_item, base_human):
                continue
            last = after[-1] if after else {}
            provenance = {'origin': origin, 'projectId': project, 'pullCursor': cursor, 'cursor': last.get('cursor'),
                          'actor': last.get('actor'), 'timestamp': last.get('timestamp'),
                          'changeActors': sorted({c.get('actor') or '(none)' for c in after}), 'humanRevision': last.get('revision')}
            decisions += read_item(item, receipt, provenance)
        tag = cursor or '0'
        resolution = resolve(decisions, ctx)
        answers = final_answers(decisions, resolution)
        pushed_lines = {f: {r['line'] for r in receipts.values() if r['family'] == f and r.get('line') is not None}
                        for f in ('shadow', 'cut', 'join', 'corner', 'unit', 'lot', 'split', 'trace')}
        pushed_lines['veg'] = {int(n) for r in receipts.values() if r['family'] == 'veg'
                               for n in r['document']['metadata']['addr']['lines'].values()}
        check = check_answers('\n'.join(answers), ctx, pushed_lines)
        by_line = {(d['family'], d['line']): d for d in decisions if d.get('line') is not None and d.get('word')}
        previews = []
        for family, numbers in (('shadow', resolution['shadows']['write']), ('cut', resolution['cuts']['write'])):
            for n in numbers:
                d = by_line[(family, n)]
                receipt = receipts[d['itemId']]
                src = receipt['document']['metadata']['addr']
                previews.append({'family': family, 'line': n, 'scene': d['sceneId'], 'object': d['objectIds'][0],
                                 'proposal_id': d['proposalId'], 'proposed_ring_sha': d['proposedRingSha'],
                                 'expect_current_ring_sha': src['currentRingSha'], 'object_scope': d['kind'] != 'building',
                                 'proposal_source': src.get('proposalSource'),
                                 'sentence': _sentence(d, project), 'fieldwork': d['fieldwork']})
        veg_decisions = [{'scene': d['sceneId'], 'object': d['objectId'], 'vegetation_type': d['word'],
                          'sentence': _sentence(d, project), 'fieldwork': {**d['fieldwork'], 'line': d['line'], 'how': d.get('how')}}
                         for d in decisions if d['family'] == 'veg' and d.get('word') in VEG_TYPES]
        joins = [{'line': d['line'], 'featureId': d['featureId'], 'members': d['members'], 'joinedRingSha': d['joinedRingSha'],
                  'verdict': d.get('word'), 'notes': d.get('notes'), 'objectNotes': d.get('objectNotes'), 'exceptions': d.get('exceptions'),
                  'conflict': d.get('conflict'), 'flags': d.get('flags'), 'fieldwork': d['fieldwork']}
                 for d in decisions if d['family'] == 'join' and not d.get('unread')]
        out.mkdir(parents=True, exist_ok=True)
        files = {
            'decisions': out / f'decisions-{tag}.json',
            'answers': out / f'answers-{tag}.txt',
            'previews': out / f'preview-requests-{tag}.json',
            'veg': out / f'veg-type-decisions.fieldwork-{tag}.json',
            'joins': out / f'joins-decisions-{tag}.json',
            'agents': out / f'agent-reviews-{tag}.json',
        }
        summary = _summary(decisions, resolution)
        listed = _listed(decisions)
        write_json(files['decisions'], {'schemaVersion': 1, 'tool': TOOL, 'source': 'human:fieldwork', 'origin': origin, 'projectId': project,
                                        'cursor': cursor, 'pulledAt': now_iso(), 'summary': summary, 'resolution': resolution,
                                        'answerCheck': check, 'unknownItems': unknown, 'missingItems': missing, 'attribution': ATTRIBUTION,
                                        **listed, 'decisions': decisions})
        held_note = [f"# held: {json.dumps(h, ensure_ascii=False)}" for h in resolution['cuts']['held'] + resolution['cuts']['resolved']]
        held_note += [f"# conflict: {c['itemId']}: {c['why']}" for c in listed['conflicts']]
        held_note += [f"# needs words: {n['itemId']}: {n['why']}" for n in listed['needs_words']]
        held_note += [f"# unread: {u['itemId']}: {u['why']} ({'; '.join(u['changed'])})" for u in listed['unread']]
        held_note += [f"# exception: {e['itemId']}{_line_tag(e)}: {'; '.join(e['why'])}" for e in listed['exceptions']]
        held_note += [f"# not stored: {n['itemId']}{_line_tag(n)}: {'; '.join(n['why'])}" for n in listed['not_stored']]
        files['answers'].write_text('\n'.join(answers + held_note) + '\n')
        write_json(files['previews'], {'source': 'human:fieldwork', 'note': 'Make one polygon preview per request '
                                       '(scene_polygon_feedback.preview_polygon), check its ring sha against proposed_ring_sha, then '
                                       'record-chat-feedback.mjs with preview_id. Partly-right shadows go back to the agent (retrim).',
                                       'requests': previews, 'retrim': resolution['shadows']['retrim']})
        write_json(files['veg'], {'source': 'human:fieldwork', 'decisions': veg_decisions})
        write_json(files['joins'], {'source': 'human:fieldwork', 'decisions': joins})
        write_json(files['agents'], {'schemaVersion': 1, 'source': 'agent:fieldwork', 'humanApproval': False,
                                    'trainingEligible': False, 'cursor': cursor, 'reviews': agent_reviews})
    return {'cursor': cursor, 'items': len(items), 'decisions': len(decisions), 'summary': summary, 'answers': answers,
            'answerCheck': check, 'resolution': resolution, 'files': {k: str(v) for k, v in files.items()},
            'unknownItems': unknown, 'missingItems': missing, 'agentReviews': len(agent_reviews), **listed}


def _listed(decisions):
    """The pull report's lists: item/outline conflicts (no decision), vegetation items that need words, changed-but-unread items,
    outlines / items whose change was an exception (no word), and vegetation types set outside a vegetation item (not stored)."""
    return {
        'not_stored': [{'itemId': d['itemId'], 'family': d['family'], 'line': d.get('line'), 'word': d.get('word'), 'why': d['notStored']}
                       for d in decisions if d.get('notStored')],
        'conflicts': [{'itemId': d['itemId'], 'family': d['family'], 'line': d.get('line'), 'itemStatus': d.get('itemStatus'),
                       'objectStatus': d.get('status'), 'flags': d.get('flags'), 'why': d['conflict']} for d in decisions if d.get('conflict')],
        'needs_words': [{'itemId': d['itemId'], 'family': d['family'], 'sceneId': d.get('sceneId'), 'itemStatus': d.get('itemStatus'),
                         'untypedLines': d.get('untypedLines'), 'why': d['needsWords']} for d in decisions if d.get('needsWords')],
        'unread': [{'itemId': d['itemId'], 'family': d['family'], 'line': d.get('line'), **d['unread']} for d in decisions if d.get('unread')],
        'exceptions': [{'itemId': d['itemId'], 'family': d['family'], 'line': d.get('line'), 'objectId': d.get('objectId'),
                        'why': d['exceptions'] + (d.get('flags') or [])} for d in decisions
                       if d.get('exceptions') and not d.get('word') and not d.get('conflict') and not d.get('unread')],
    }


def _line_tag(entry):
    return '' if entry.get('line') is None else f" line {entry['line']}"


def final_answers(decisions, resolution):
    """The answer lines a pull writes: held cut lines (whole-roof copies, several lines on one outline) and the 2-and-3 rule's
    line 3 never sit in the cuts line; they stay in resolution['cuts'] held / resolved (and the answers file's # held lines)."""
    cuts = resolution['cuts']
    held = {n for h in cuts['held'] + cuts['resolved'] for n in h.get('lines') or [h['line']]}
    answers = [a if not a.startswith('cuts:') else _drop_cut_lines(a, held) for a in answer_lines(decisions)]
    return [a for a in answers if a]


def _drop_cut_lines(answer, held):
    """Held cut lines (whole-roof copies, several lines on one outline, the 2-and-3 rule's line 3) never appear in a cuts answer
    line; they are listed as held instead."""
    if not held:
        return answer
    parts = []
    for seg in answer.split(':', 1)[1].strip().split(' / '):
        word, numbers = seg.split(': ')
        keep = sorted(expand_lines(numbers) - held) if word == 'right' else sorted(expand_lines(numbers))
        if keep:
            parts.append(f'{word}: {compact_lines(keep)}')
    return f"cuts: {' / '.join(parts)}" if parts else ''


def _sentence(d, project):
    fw = d['fieldwork']
    revision = fw.get('humanRevision') or fw['revision']
    what = {'shadow': 'PROPOSED trim', 'cut': 'PROPOSED fix', 'join': 'JOINED outline', 'veg': 'outline'}[d['family']]
    if d['family'] == 'veg':
        how = d.get('how') or ''
        typed = {'label': f"Label set to {d.get('label')!r}", 'vegetation_type': f"vegetation type set to {d.get('vegetationType')!r}"}
        detail = (' and '.join(typed[key] for key in how.split(' and ')) if set(how.split(' and ')) <= set(typed)
                  else f"status set to confirmed (proposed {d['proposed']})" if how == 'confirmed' else f'Fieldwork notes line {how[6:]!r}')
        text = f"Fieldwork {project} {fw['itemId']} rev {revision}: veg line {d['line']} {what} {detail}."
    elif d.get('how') == 'item status':
        text = (f"Fieldwork {project} {fw['itemId']} rev {revision}: item Review status set to {d['itemStatus'].title()} "
                f"({what} line {d['line']} left {d.get('status') or 'candidate'}).")
    else:
        text = f"Fieldwork {project} {fw['itemId']} rev {revision}: {what} line {d['line']} set to {d.get('status')}."
        if d.get('itemStatus'):
            text = text[:-1] + f"; item Review status {d['itemStatus'].title()}."
    notes = ' '.join(x for x in (d.get('objectNotes'), d.get('notes')) if x)
    if notes:
        text += f' Notes: "{notes[:1500]}"'
    return text[:2000]


def _summary(decisions, resolution):
    def kind(d):
        for key, name in (('conflict', 'conflict'), ('needsWords', 'needs_words'), ('unread', 'unread'), ('exceptions', 'exception'),
                          ('notUnderstood', 'not_understood'), ('withheld', 'withheld')):
            if d.get(key) and not d.get('word'):
                return name
        return d.get('word') or 'note'

    out = {}
    for d in decisions:
        key = f"{d['family']}:{kind(d)}"
        out[key] = out.get(key, 0) + 1
    out['cuts_held'] = len(resolution['cuts']['held'])
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------------------------------------------- status

def status(root='.', directory=None, project=DEFAULT_PROJECT, out=None):
    directory = _repo_path(root, directory or _default_dir(project))
    receipts = [json.loads(p.read_text()) for p in (directory / 'review' / 'items').glob('*.json')]
    families = {}
    for r in receipts:
        families[r['family']] = families.get(r['family'], 0) + 1
    out = _repo_path(root, out or DEFAULT_PULL_OUT)
    pulls = sorted(out.glob('decisions-*.json'), key=lambda p: p.stat().st_mtime) if out.exists() else []
    last = json.loads(pulls[-1].read_text()) if pulls else {}
    origin = json.loads((directory / 'review' / 'origin.json').read_text()) if (directory / 'review' / 'origin.json').exists() else None
    return {'projectId': project, 'origin': origin, 'pushed': families, 'items': len(receipts), 'lastPull': str(pulls[-1]) if pulls else None,
            'lastPullSummary': last.get('summary'), 'conflicts': last.get('conflicts', []), 'needs_words': last.get('needs_words', []),
            'unread': last.get('unread', []), 'exceptions': last.get('exceptions', []), 'not_stored': last.get('not_stored', [])}


# ------------------------------------------------------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    s = commands.add_parser('stage', help='Build the frozen review bundle (local only)')
    s.add_argument('--items', default=str(DEFAULT_ITEMS))
    s.add_argument('--context', default=str(DEFAULT_CONTEXT))
    s.add_argument('--out', default=str(DEFAULT_STAGE_ROOT))
    s.add_argument('--drop-stale', action='store_true', help='Leave out lines whose object changed since the proposal (listed)')
    s.add_argument('--full-frame', action='store_true', help='No crops: reuse the scene images')
    p = commands.add_parser('push', help='Create the staged review items in Fieldwork (create only, receipts)')
    p.add_argument('--stage', dest='stage_dir', help='Stage bundle folder (default: the latest staged)')
    p.add_argument('--limit', type=int, help='Push only the first N items (canary)')
    p.add_argument('--dry-run', action='store_true', help='HEAD / Get probes only; no writes, no receipts')
    q = commands.add_parser('pull', help='Read the reviewer\'s clicks into answer lines and decision files (read-only on Fieldwork)')
    q.add_argument('--out', help=f'Output folder (default {DEFAULT_PULL_OUT})')
    q.add_argument('--context', default=str(DEFAULT_CONTEXT))
    t = commands.add_parser('status', help='Receipts and the last pull (no network)')
    t.add_argument('--out', help=f'Pull output folder (default {DEFAULT_PULL_OUT})')
    for command in (s, p, q, t):
        command.add_argument('--project', default=os.environ.get('FIELDWORK_REVIEW_PROJECT', DEFAULT_PROJECT))
        command.add_argument('--families', help='Comma-separated: shadow,cut,veg,join,round5,corner,unit,lot,split,trace')
    for command in (p, q, t):
        command.add_argument('--dir', dest='directory', help='Receipt folder (default data/fieldwork-sync/<project>)')
    for command in (p, q):
        command.add_argument('--url', help='Fieldwork origin (ANNOTATION_URL; default the live CMS)')
        command.add_argument('--token-file', help='Bearer token file (live default deploy/annotation/admin-token.txt)')
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    families = tuple(f.strip() for f in args.pop('families').split(',')) if args.get('families') else None
    try:
        if command == 'stage':
            result = stage(items=args['items'], context=args['context'], out=args['out'], project=args['project'],
                           drop_stale=args['drop_stale'], full_frame=args['full_frame'], families=families or FAMILIES)
        elif command == 'push':
            result = push(stage_dir=args['stage_dir'], url=args['url'], directory=args['directory'], project=args['project'],
                          token_file=args['token_file'], limit=args['limit'], families=families, dry_run=args['dry_run'])
            result.pop('items', None)
        elif command == 'pull':
            result = pull(url=args['url'], directory=args['directory'], project=args['project'], out=args['out'],
                          token_file=args['token_file'], context=args['context'])
        else:
            result = status(directory=args['directory'], project=args['project'], out=args['out'])
    except ReviewError as error:
        print(str(error), file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, indent=1, sort_keys=True, ensure_ascii=False)
    print()
    return 0 if result.get('status') not in ('partial',) else 2


if __name__ == '__main__':
    sys.exit(main())
