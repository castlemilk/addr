"""Tiered registry of roof references for agreement checks (labels vs observations).

T1 (confirmed + geometry_reviewed buildings) and T2 (human edits: roof_line_edits,
moved needs_adjustment outlines, CRM rows with reviewer provenance) are human
decisions. T3 agent observations (CRM agent-imagery-trace rows, review-loop rows,
untouched candidate/needs_adjustment objects, carried objects the reviewer has not yet
decided on the new image: carried_from + needs_adjustment, whatever else they carry) and
T4 context (superseded objects, CRM parcels) never enter an accuracy denominator. Reads review.json, scene models,
the CRM (read-only) and review-loop reports; writes only data/reviews/eyes/**.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from shapely.errors import ShapelyError
from shapely.geometry import Polygon, mapping, shape

from . import eyes_index
from .review_authority import carried_undecided, review_authority_blocker
from .scene_confidence import geographic
from .scene_split import group_split
from .tiles import WORLD_SPAN_M, lonlat_to_tile_xy, tile_xy_to_lonlat

VERSION = 'reference-registry-v1'
TIERS = {'T1': 'human_confirmed_geometry', 'T2': 'human_edit', 'T3': 'agent_observation', 'T4': 'context_only'}
LABEL_TIERS = ('T1', 'T2')
SPLITS = ('train', 'validation', 'test')
CRM_KINDS = {'roof': 'building', 'tree': 'tree', 'object': 'object'}
FORMS = {'hipped': r'\bhip(?:s|ped)?\b', 'gabled': r'\bgable(?:s|d)?\b', 'flat': r'\bflat\b', 'skillion': r'\bskillion\b'}
SCENE_ID = re.compile(r'[a-zA-Z0-9_-]{1,100}')
RUN_ID = re.compile(r'[a-f0-9]{16}')
SHA256 = re.compile(r'[a-f0-9]{64}')
NOTE = ('T1/T2 are human decisions; T3 agent observations and T4 context never enter an accuracy denominator. '
        'Counts are reference inventory, not accuracy.')
# One malformed record must never abort a registry build: these are counted in `skipped`, not raised.
SKIPPABLE = (KeyError, TypeError, ValueError, IndexError, AttributeError, ShapelyError)


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def ref_id(*parts):
    """Stable id from the source identity (not its content), so edits update in place."""
    return hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:16]


def spatial_group(polygon):
    point = polygon.centroid
    x, y = lonlat_to_tile_xy(point.x, point.y, 17)
    return f'z17:{math.floor(x)}:{math.floor(y)}'


def local_to_lonlat(points, scene):
    """Local scene metres -> [lon, lat]; the same transform as scene_confidence.geographic."""
    center = scene['centroid']
    origin = np.array(lonlat_to_tile_xy(center['lon'], center['lat'], 0))
    scale = WORLD_SPAN_M*math.cos(math.radians(center['lat']))
    return [list(tile_xy_to_lonlat(*(origin + np.asarray(p[:2], dtype=float)*[1, -1]/scale), 0)) for p in points]


def local_polygon(obj, scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    def ring(points):
        return (np.c_[np.asarray(points, dtype=float), np.ones(len(points))] @ frame).tolist()
    return Polygon(ring(obj['ring']), [ring(h) for h in obj.get('holes', []) if len(h) >= 3])


def valid(polygon):
    """Return (polygon, repair) keeping the largest part when buffer(0) splits it."""
    if polygon.geom_type == 'Polygon' and polygon.is_valid:
        return polygon, None
    fixed = polygon.buffer(0)
    if fixed.geom_type == 'MultiPolygon':
        return max(fixed.geoms, key=lambda g: g.area), 'largest_part'
    return fixed, 'buffer0'


def png_index(root=Path('.')):
    """Content-addressed scene PNGs: sha256 -> scene ids holding that exact image."""
    found = {}
    for png in sorted((Path(root)/'data/scenes').glob('*/*.png')):
        if SHA256.fullmatch(png.stem):
            found.setdefault(png.stem, []).append(png.parent.name)
    return found


def label_conflict(label, roof_type):
    """Roof forms named in a label that contradict the stored roof_type ('mixed' never conflicts)."""
    if not label or not roof_type or roof_type == 'mixed':
        return None
    forms = sorted(form for form, pattern in FORMS.items() if re.search(pattern, str(label), re.IGNORECASE))
    if not forms or roof_type in forms:
        return None
    return {'label_forms': forms, 'roof_type': roof_type}


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _row(identity, tier, source_kind, polygon, payload, scene_id=None, object_id=None, crm_feature_id=None, address_id=None):
    group = spatial_group(polygon)
    west, south, east, north = polygon.bounds
    return {'ref_id': ref_id(*identity), 'tier': tier, 'source_kind': source_kind, 'scene_id': scene_id, 'object_id': object_id,
            'crm_feature_id': crm_feature_id, 'address_id': address_id, 'spatial_group': group, 'split': group_split(group),
            'west': west, 'south': south, 'east': east, 'north': north, 'version': VERSION,
            'payload': {'tier_name': TIERS[tier], **payload}}


def _stamp():
    return {'code_sha256': code_sha256(), 'computed_at': datetime.now(UTC).isoformat()}


def object_reference(scene_id, scene, obj, *, tier, provenance, pngs, source_sha256, updated_at, independent,
                     identity=None, extra=None):
    """Reference row for a scene-bound object; the scene dict supplies frame and image identity."""
    polygon, repaired = valid(geographic(obj, scene))
    local, _ = valid(local_polygon(obj, scene))
    edits = line_edits(obj)
    vertices, edges = edits.get('vertices') or [], []
    for edge in edits.get('edges') or []:
        try:
            a, b = [float(v) for v in vertices[edge['a']][:2]], [float(v) for v in vertices[edge['b']][:2]]
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        a_lonlat, b_lonlat = local_to_lonlat([a, b], scene)
        edges.append({'a_xy_m': a, 'b_xy_m': b, 'a_lonlat': a_lonlat, 'b_lonlat': b_lonlat, 'kind': edge.get('kind')})
    image = scene.get('image_sha256')
    notes = str(obj.get('notes') or '')
    payload = {'provenance': provenance, 'object_kind': 'building', 'status': obj.get('status'),
               'geometry_reviewed': bool(obj.get('geometry_reviewed')), 'independent_geometry': independent,
               'geometry_wgs84': mapping(polygon), 'geometry_local_m': mapping(local), 'geometry_repaired': repaired,
               'image_sha256': image, 'boundary_image_sha256': None, 'image_identity_source': 'scene_image_sha256' if image else None,
               'drawn_over': 'scene' if image in pngs else 'unknown', 'drawn_over_scene_ids': pngs.get(image, []),
               'model_signature': scene.get('model_signature'), 'label': obj.get('label'), 'roof_type': obj.get('roof_type'),
               'eave_height_m': None, 'edges': edges, 'n_edges': len(edges), 'checked': edits.get('checked') if edits else None,
               'label_property_conflict': label_conflict(obj.get('label'), obj.get('roof_type')),
               'notes_mention_agent': bool(re.search(r'\b(assistant|agent)\b', notes, re.IGNORECASE)),
               'source_sha256': source_sha256,
               'object_sha256': hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest(),
               'updated_at': updated_at, **(extra or {}), **_stamp()}
    return _row(identity or ('review', scene_id, obj['id']), tier, 'review', polygon, payload, scene_id=scene_id,
                object_id=obj['id'], address_id=obj.get('address_id'))


def _same_ring(a, b):
    try:
        a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    except (TypeError, ValueError):
        return False
    return a.shape == b.shape and bool(np.allclose(a, b, atol=1e-6))


def _same_holes(a, b):
    a, b = list(a or []), list(b or [])
    return len(a) == len(b) and all(_same_ring(x, y) for x, y in zip(a, b, strict=True))


def outline_edited(obj, model_ring, model_holes=None):
    """True when no model ring exists or the ring, or (when model holes are known) the holes, differ."""
    return model_ring is None or not _same_ring(obj.get('ring'), model_ring) or (
        model_holes is not None and not _same_holes(obj.get('holes'), model_holes))


def line_edits(obj):
    """The object's roof_line_edits when it is a non-empty dict, else {} (malformed values are ignored)."""
    edits = obj.get('roof_line_edits')
    return edits if isinstance(edits, dict) and edits else {}


def review_tier(obj, model_ring, model_verified=True, model_holes=None):
    """(tier, provenance, independent_geometry) or (None, skip_reason, None).

    `model_verified` is False when the review's models/<model_signature>.json is missing and the
    rings were compared against a different model: a differing ring then proves nothing about a
    human edit, so the object stays T3 (a manual object absent from any model is still T2).
    `model_holes` (the model object's holes, [] when it has none) makes a hole-only edit count as an
    edit; callers that pass no holes keep ring-only behaviour.
    A carried object (carried_from, status needs_adjustment) is T3 review:carried_undecided: it keeps the
    source object's id, so no model ring exists in the new frame and its re-projected ring and translated
    roof_line_edits would otherwise read as a human edit; the reviewer has not decided it on the new image.
    """
    status = obj.get('status')
    if status == 'rejected':
        return None, 'review_rejected', None
    if status == 'superseded':
        return 'T4', 'review:superseded', True
    edited = outline_edited(obj, model_ring, model_holes)
    # Imported edits and machine decisions do not establish human authority.
    if authority_blocker := review_authority_blocker(obj):
        return 'T3', f'review:{authority_blocker}', False
    # A carried, undecided object is the reviewer's target on the new image, not human geometry.
    if carried_undecided(obj):
        return 'T3', 'review:carried_undecided', False
    if status == 'confirmed' and obj.get('geometry_reviewed'):
        return 'T1', 'review:confirmed', True
    if line_edits(obj):
        return 'T2', 'review:roof_line_edits', True
    if status == 'needs_adjustment' and edited:
        if model_ring is not None and not model_verified:
            return 'T3', 'review:needs_adjustment_unverified_model', True
        return 'T2', 'review:needs_adjustment_edited', True
    return 'T3', f'review:{status}', edited


def _load(path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def scene_model(root, scene_id, model_signature=None, cache=None):
    """The exact model file for a signature, else latest.json when signatures agree (or none given)."""
    cache = {} if cache is None else cache
    key = (scene_id, model_signature)
    if key not in cache:
        model = None
        if isinstance(scene_id, str) and SCENE_ID.fullmatch(scene_id):
            folder = Path(root)/'data/scenes'/scene_id
            if isinstance(model_signature, str) and SHA256.fullmatch(model_signature):
                model = _load(folder/'models'/f'{model_signature}.json')
            if model is None:
                latest = _load(folder/'latest.json')
                if latest and (not model_signature or latest.get('model_signature') == model_signature):
                    model = latest
        cache[key] = model
    return cache[key]


def review_references(root=Path('.'), pngs=None, skipped=None, cache=None):
    root, pngs = Path(root), png_index(root) if pngs is None else pngs
    skipped = Counter() if skipped is None else skipped
    rows = []
    for path in sorted((root/'data/scenes').glob('*/review.json')):
        scene_id, raw = path.parent.name, path.read_bytes()
        try:
            review = json.loads(raw)
        except ValueError:
            skipped['review_unreadable'] += 1
            continue
        scene = scene_model(root, scene_id, review.get('model_signature'), cache) or scene_model(root, scene_id, None, cache)
        if not isinstance(scene, dict):
            skipped['review_scene_unavailable'] += 1
            continue
        objects, model_objects = review.get('objects'), scene.get('objects')
        if not isinstance(objects, list) or not isinstance(model_objects, list):
            skipped['review_objects_not_list'] += 1
            continue
        verified = scene.get('model_signature') == review.get('model_signature')
        model_by_id = {o.get('id'): o for o in model_objects if isinstance(o, dict)}
        for obj in objects:
            if not isinstance(obj, dict):
                skipped['review_object_not_dict'] += 1
                continue
            if obj.get('kind') != 'building':
                skipped['review_non_building'] += 1
                continue
            model_obj = model_by_id.get(obj.get('id'))
            tier, provenance, independent = review_tier(
                obj, model_obj.get('ring') if model_obj else None, verified,
                model_holes=(model_obj.get('holes') or []) if model_obj else None)
            if tier is None:
                skipped[provenance] += 1
                continue
            try:
                rows.append(object_reference(
                    scene_id, scene, obj, tier=tier, provenance=provenance, pngs=pngs, independent=independent,
                    source_sha256=hashlib.sha256(raw).hexdigest(), updated_at=review.get('updated_at'),
                    extra={'frame_signature_matches_review': verified}))
            except SKIPPABLE:
                skipped['review_invalid_geometry'] += 1
    return rows


def crm_tier(kind, label, props):
    reference = str(props.get('boundary_reference_source') or '').lower()
    if kind not in CRM_KINDS:
        return 'T4', 'crm:context'
    if 'agent' in reference:
        # Agent-traced geometry stays an observation even if a reviewer set the roof form.
        return 'T3', f'crm:{reference}'
    if props.get('roof_form_source') == 'reviewer' or 'user' in reference or 'reviewer' in reference:
        return 'T2', 'crm:reviewer'
    if 'review loop' in str(label or '').lower():
        return 'T3', 'crm:review-loop'
    return 'T3', 'crm:unattributed'


def crm_reference(record, pngs):
    """Reference row for one crm_features record (dict of column -> value)."""
    try:
        props = json.loads(record.get('properties') or '{}')
    except ValueError:
        props = {}
    props = props if isinstance(props, dict) else {}
    geometry = shape(json.loads(record['geometry_json']))
    if geometry.geom_type == 'MultiPolygon':
        geometry = max(geometry.geoms, key=lambda g: g.area)
    polygon, repaired = valid(geometry)
    if polygon.geom_type != 'Polygon' or polygon.is_empty or polygon.area <= 0:
        raise ValueError('Empty CRM geometry')
    kind, label = record.get('kind'), record.get('label')
    tier, provenance = crm_tier(kind, label, props)
    image = props.get('boundary_image_sha256') or None
    roof_type = props.get('roof_type') or None
    payload = {'provenance': provenance, 'object_kind': CRM_KINDS.get(kind, kind), 'crm_kind': kind, 'status': None,
               'geometry_reviewed': None, 'independent_geometry': True,
               'geometry_wgs84': mapping(polygon), 'geometry_local_m': None, 'geometry_repaired': repaired,
               'image_sha256': None, 'boundary_image_sha256': image,
               'image_identity_source': 'crm_boundary_image_sha256' if image else None,
               'drawn_over': 'scene' if image in pngs else 'unknown', 'drawn_over_scene_ids': pngs.get(image, []),
               'model_signature': None, 'label': label, 'roof_type': roof_type,
               'eave_height_m': _float(props.get('eave_height')), 'ridge_height_m': _float(props.get('ridge_height')),
               'roof_form_source': props.get('roof_form_source'), 'boundary_reference_source': props.get('boundary_reference_source'),
               'boundary_reference_status': props.get('boundary_reference_status'),
               'edges': [], 'n_edges': 0, 'checked': None, 'label_property_conflict': label_conflict(label, roof_type),
               'notes_mention_agent': 'agent' in str(props.get('boundary_reference_notes') or '').lower(),
               'source_sha256': hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(),
               'updated_at': record.get('updated_at'), 'updated_by': record.get('updated_by'), **_stamp()}
    return _row(('crm', record['id']), tier, 'crm', polygon, payload, crm_feature_id=record['id'],
                address_id=record.get('address_id'))


def crm_references(root=Path('.'), pngs=None, skipped=None):
    root, pngs = Path(root), png_index(root) if pngs is None else pngs
    skipped = Counter() if skipped is None else skipped
    database = root/'data/australia_geo.db'
    if not database.exists():
        return []
    with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crm_features'").fetchone():
            return []
        cursor = db.execute('SELECT * FROM crm_features ORDER BY id')
        columns = [c[0] for c in cursor.description]
        records = [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]
    rows = []
    for record in records:
        try:
            rows.append(crm_reference(record, pngs))
        except SKIPPABLE:
            skipped['crm_invalid_geometry'] += 1
    return rows


def loop_references(root=Path('.'), pngs=None, skipped=None, cache=None):
    """Review-loop rows: agent observations about an unchanged model object (no new geometry)."""
    root, pngs = Path(root), png_index(root) if pngs is None else pngs
    skipped = Counter() if skipped is None else skipped
    rows = []
    for path in sorted((root/'data/roof-review-loop/runs').glob('*/report.json')):
        run = path.parent.name
        raw = path.read_bytes() if RUN_ID.fullmatch(run) else b''
        report = _load(path) if raw else None
        if not report:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        items = report.get('rows') if isinstance(report, dict) else None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                skipped['review_loop_object_unavailable'] += 1
                continue
            scene_id, object_id = item.get('scene_id'), item.get('object_id')
            scene = scene_model(root, scene_id, item.get('source_model'), cache) if isinstance(scene_id, str) else None
            objects = scene.get('objects') if isinstance(scene, dict) else None
            obj = next((o for o in (objects or []) if isinstance(o, dict) and o.get('id') == object_id), None)
            if obj is None or obj.get('kind') != 'building':
                skipped['review_loop_object_unavailable'] += 1
                continue
            extra = {'loop_run': run, 'loop_status': item.get('status'), 'loop_reason': item.get('reason'),
                     'loop_source_band': item.get('source_band')}
            try:
                rows.append(object_reference(
                    scene_id, scene, obj, tier='T3', provenance=f"review-loop:{item.get('status')}", pngs=pngs,
                    source_sha256=digest, updated_at=report.get('updated_at'), independent=False,
                    identity=('review-loop', run, scene_id, object_id), extra=extra))
            except SKIPPABLE:
                skipped['review_loop_invalid_geometry'] += 1
    return rows


def build(root=Path('.'), skipped=None):
    """All reference rows sorted by ref_id; `skipped` (a dict) receives exclusion counts."""
    root = Path(root)
    counter, pngs, cache = Counter(), png_index(root), {}
    rows = review_references(root, pngs, counter, cache) + crm_references(root, pngs, counter)
    rows += loop_references(root, pngs, counter, cache)
    if skipped is not None:
        skipped.update(counter)
    return sorted(rows, key=lambda r: r['ref_id'])


def registry_id(rows):
    """Content address of the registry, ignoring computation timestamps."""
    stable = [{**r, 'payload': {k: v for k, v in r['payload'].items() if k != 'computed_at'}}
              for r in sorted(rows, key=lambda r: r['ref_id'])]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def summarize(rows, skipped=None):
    tiers = {tier: {**dict.fromkeys(SPLITS, 0), 'total': 0} for tier in TIERS}
    kinds = {tier: Counter() for tier in TIERS}
    for row in rows:
        tiers[row['tier']][row['split']] += 1
        tiers[row['tier']]['total'] += 1
        kinds[row['tier']][row['source_kind']] += 1
    return {'version': VERSION, 'n_references': len(rows), 'tiers': tiers, 'tier_names': TIERS,
            'label_tiers': list(LABEL_TIERS), 'by_source_kind': {t: dict(sorted(c.items())) for t, c in kinds.items()},
            'by_provenance': dict(sorted(Counter(r['payload']['provenance'] for r in rows).items())),
            'skipped': dict(sorted((skipped or {}).items())), 'note': NOTE}


def write(root, rows, skipped=None):
    """Replace this version's references in eyes.sqlite and save a content-addressed registry folder."""
    root = Path(root)
    identifier = registry_id(rows)
    folder = root/'data/reviews/eyes/registry'/identifier
    folder.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: r['ref_id'])
    (folder/'references.jsonl').write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in ordered))
    summary = {**summarize(ordered, skipped), 'registry_id': identifier, **_stamp()}
    temporary = folder/'summary.json.tmp'
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True))
    temporary.replace(folder/'summary.json')
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        db.execute('DELETE FROM "references" WHERE version=?', (VERSION,))
        eyes_index.replace_rows(db, 'references', ordered)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build the tiered roof reference registry (labels vs observations, not accuracy).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--build', action='store_true', help='Rebuild from review.json, CRM (read-only) and review-loop rows.')
    parser.add_argument('--summary', action='store_true', help='Print tier x split counts from eyes.sqlite.')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.build:
        skipped = Counter()
        summary = write(root, build(root, skipped), skipped)
    elif args.summary:
        if not eyes_index.path(root).exists():
            parser.error('No eyes index yet; run --build first')
        with closing(eyes_index.connection(root, readonly=True)) as db:
            summary = summarize(eyes_index.rows(db, 'references', version=VERSION))
    else:
        parser.print_help()
        return 2
    json.dump(summary, sys.stdout, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
