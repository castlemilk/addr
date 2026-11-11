"""Per-object lot tags: which Vicmap lot an object sits on, whether the frame cut it, whether that lot is the address lot.

The reviewer's 30 September set was dominated by objects cut by the image edge on a
neighbour's lot, unit-site blocks with no visible sub-division cuts, and an object
labelled primary when a larger roof shares its lot. This module answers those with
cadastral evidence only: for every live object of every kind it reports the dominant
lot (share of the object's area), every lot with >= 2% of it, whether the frame
covers that lot, whether the lot belongs to the address (CRM seed roof, else the
address point), the live buildings on the same lot with their areas, and a small set
of flags with one plain sentence each. Rows are sidecars in the eyes index (table
object_quality, version lot-tags-v1) keyed by the current model_signature; the
scene-level row (object_id '_scene') carries the unit-site coverage sentence and the
lot-frame plan sentence from scene_lot. Every row is an agent hint, never a label:
nothing here rewrites latest.json, review.json or confidence.json, and no flag
changes an object's status or kind.

Carried review-layer objects (data/scenes/<id>/review.json objects with a non-empty
carried_from, status != superseded, whose id is not a live latest.json object) are
tagged from the same geometry as rows with layer 'review' (model rows carry layer
'model'); they never join lot members, lot_primary, primary_ratio or the replaced set,
so no model row changes, and review.json is only read.

Parcel evidence, in order: the scene's own property_roofs.parcels, the frozen
data/scene-parcels file for its registered bounds, any frozen units/scene-parcels
payload whose bounds cover the frame (address_allocation.parcel_caches), a frozen
copy under data/reviews/eyes/parcels/, and (unless --offline) one bounded Vicmap
read frozen there. Vicmap Parcel carries no fence lines, party walls, building
envelopes, easements or stacked-strata cuts; stacked lots share one polygon and
collapse to one group here, as parcelGeometryGroups does in the editor.

Cohort frames (roof_training_250-*) have no registered capture: their affines are
fitted from the CRM roof by least squares, so lot positions there may sit a few
pixels off the imagery. The tag says which source and registration it used.
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
from shapely import make_valid
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import transform, unary_union

from . import eyes_index
from .roof_block_context import fetch_parcels
from .scene_addresses import WORLD, image_projection
from .scene_batch import signature
from .scene_object_quality import _live, _row, replace_scene_rows, scene_lonlat_bounds
from .scene_self_review import GEOMETRY_ERRORS, validate_id
from .scene_training import atomic_json
from .tiles import lonlat_to_tile_xy, tile_xy_to_lonlat

VERSION = 'lot-tags-v1'
TABLE = 'object_quality'
NOTE = 'agent hint, not a label'
SCENE_OBJECT_ID = '_scene'
PARCELS_DIR = Path('data/reviews/eyes/parcels')
EYES_DIR = Path('data/reviews/eyes')
DEAD_STATUSES = ('rejected', 'superseded')
LAYERS = ('model', 'review')  # payload 'layer': a live latest.json object, or a carried review.json object
KIND_NOUNS = {'building': 'building', 'tree': 'tree', 'vegetation': 'vegetation', 'pool': 'pool', 'car': 'car',
              'solar_panel': 'panel', 'driveway': 'driveway', 'footpath': 'path', 'road': 'road', 'water': 'water'}
# Design values, stated before the first scored run and not tuned on the reviewer's set.
DESIGN = {
    'edge_px': 1.,            # scene_self_review image_cut: outline within one pixel of the frame boundary
    'lot_share_min': .02,     # a lot is listed when it holds >= 2% of the object's area
    'spans_lot_share': .10,   # spans_lots: >= 2 numbered lots with distinct polygons each holding >= 10%
    'member_share': .5,       # a building is a lot member when >= 50% of its area lies in the lot polygon
    'seed_overlap': .10,      # the address plan comes from lots covering > 10% of the CRM seed roof
    'seed_dominant': .50,     # ... when one lot holds more than half of the seed; otherwise the address point decides
    'fetch_pad_deg': .0012,   # about 105 m of padding around the frame for the one bounded Vicmap read
    'coordinate_round': 7,    # geometry key rounding (1e-7 deg), the twin of parcelGeometryGroups in the editor
    'full_coverage': .995,
}
RULES = {
    'frame_cut': 'outline within 1 px of the frame boundary, or clipped_by_frame set with no live object listing it in replaces',
    'lot_incomplete_in_frame': 'the dominant lot polygon is not covered by the frame (context, not a defect)',
    'lot_unassociated': ('the dominant lot is not the address lot, nor another lot of the address plan when that plan has '
                         'common property (a unit-site block), and the lot leaves the frame'),
    'spans_lots': '>= 2 numbered (non-CM) lots with distinct polygons each hold >= 10% of the object',
    'common_property': "the dominant lot number starts with 'CM'",
    'unit_site': 'the address plan has >= 2 numbered lots and a common-property lot (scene level)',
    'no_lot': 'no lot holds >= 2% of the object (outside the parcel coverage or on a road)',
    'review_layer': ("review.json objects with a non-empty carried_from, status != superseded and an id that is not a live "
                     "latest.json object are tagged from the same geometry as layer 'review'; they never join lot members, "
                     "lot_primary, primary_ratio or the replaced set, so no model row changes"),
}


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# --- Projections ----------------------------------------------------------------------------------------------

def inverse_projection(scene):
    """Pixel (x, y) -> (lon, lat): the exact inverse of scene_addresses.image_projection."""
    lon0, lat0 = scene['centroid']['lon'], scene['centroid']['lat']
    scale = WORLD * math.cos(math.radians(lat0))
    y0 = math.asinh(math.tan(math.radians(lat0))) / (2*math.pi)
    a = np.asarray(scene['pixel_to_meters'], dtype=float)

    def unproject(x, y, z=None):
        mx = x*a[0][0] + y*a[1][0] + a[2][0]
        my = x*a[0][1] + y*a[1][1] + a[2][1]
        lon = lon0 + mx/scale*360
        lat = math.degrees(math.atan(math.sinh(2*math.pi*(y0 + my/scale))))
        return lon, lat
    return unproject


def frame_lonlat_bounds(scene):
    """WGS84 bounds of the whole frame from its four pixel corners (west, south, east, north)."""
    unproject = inverse_projection(scene)
    w, h = scene['image_width'], scene['image_height']
    corners = [unproject(x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h))]
    return [min(c[0] for c in corners), min(c[1] for c in corners), max(c[0] for c in corners), max(c[1] for c in corners)]


def registration(scene):
    """'region' for a registered tile capture, 'fitted' for a cohort frame whose affines were fitted from the CRM roof."""
    return 'region' if (scene.get('region') or {}).get('bounds') else 'fitted'


def crm_ring_lonlat(record):
    """A CRM record's ring_meters (local metres about its centroid) as WGS84 points."""
    centre = record['centroid']
    scale = WORLD * math.cos(math.radians(centre['lat']))
    tx, ty = lonlat_to_tile_xy(centre['lon'], centre['lat'], 0)
    return [list(tile_xy_to_lonlat(tx + x/scale, ty - y/scale, 0)) for x, y in record['ring_meters']]


_RECORDS = {}


def crm_record(root, address_id):
    """The public/address-roofs.json record for an address, or None (cached per file mtime)."""
    path = Path(root)/'public/address-roofs.json'
    if not path.exists() or not isinstance(address_id, int):
        return None
    stamp = path.stat().st_mtime_ns
    cached = _RECORDS.get(str(path))
    if not cached or cached[0] != stamp:
        try:
            records = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        cached = (stamp, {r.get('address_id'): r for r in records if isinstance(r, dict)})
        _RECORDS[str(path)] = cached
    return cached[1].get(address_id)


def address_row(root, address_id):
    """(full_address, lon, lat) from the local address index, or None."""
    path = Path(root)/'data/australia_geo.db'
    if not path.exists() or isinstance(address_id, bool) or not isinstance(address_id, int):
        return None
    try:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
            row = db.execute('SELECT full_address, lon, lat FROM addresses WHERE id=?', (address_id,)).fetchone()
    except sqlite3.Error:
        return None
    return tuple(row) if row else None


def address_seed(scene, root):
    """The address evidence for a scene: the CRM seed ring (pixels) and the address point (pixels), whichever exist."""
    address_id = scene.get('address_id')
    if not isinstance(address_id, int):
        return None
    project = image_projection(scene)
    seed = {'address_id': address_id, 'ring': None, 'point': None, 'title': scene.get('title')}
    record = crm_record(root, address_id)
    if record and record.get('ring_meters') and record.get('centroid'):
        seed['ring'] = [list(project(lon, lat)) for lon, lat in crm_ring_lonlat(record)]
        seed['crm_distance_m'] = record.get('distance_meters')
    row = address_row(root, address_id)
    if row:
        seed['title'] = row[0]
        seed['point'] = list(project(row[1], row[2]))
    return seed


# --- Parcel evidence ------------------------------------------------------------------------------------------

def _valid_payload(data):
    return isinstance(data, dict) and isinstance(data.get('features'), list)


def frozen_parcels_path(scene, root):
    key = signature([scene['image_sha256'], scene.get('centroid'), scene['pixel_to_meters']])
    return Path(root)/PARCELS_DIR/f'{key}.json', key


def _read_frozen(path, key):
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if saved.get('key') != key or saved.get('sha256') != signature(saved.get('data')) or not _valid_payload(saved.get('data')):
        return None
    return saved['data']


def pad_bounds(bounds, pad):
    w, s, e, n = bounds
    return [w-pad, s-pad, e+pad, n+pad]


def fetch_frozen(bounds, path, key):
    """One bounded Vicmap read frozen as {key, sha256, data} (the scene_property_roofs.parcel_input envelope)."""
    data = fetch_parcels(bounds)
    atomic_json(path, {'key': key, 'sha256': signature(data), 'data': data})
    return data


def parcels_for_scene(scene, root, *, offline=False):
    """(parcels, parcels_sha256, source): the first parcel payload that covers the scene, or (None, None, 'unavailable').

    Order: latest.json property_roofs.parcels; data/scene-parcels/<registered key>.json; a frozen units or
    scene-parcels payload whose bounds cover the frame; a frozen copy under data/reviews/eyes/parcels/; and, unless
    offline, one bounded Vicmap read frozen there. Errors are never cached.
    """
    root = Path(root)
    inherited = (scene.get('property_roofs') or {}).get('parcels')
    if _valid_payload(inherited) and inherited['features']:
        return inherited, signature(inherited), 'latest.json'
    region_bounds = (scene.get('region') or {}).get('bounds')
    if region_bounds:
        key = signature([scene['image_sha256'], region_bounds, scene.get('centroid'), scene['pixel_to_meters']])
        data = _read_frozen(root/'data/scene-parcels'/f'{key}.json', key)
        if data is not None:
            return data, signature(data), 'scene-parcels'
    try:
        frame = box(*frame_lonlat_bounds(scene))
    except (KeyError, TypeError, ValueError):
        frame = None
    if frame is not None:
        from .address_allocation import parcel_caches
        for _, data in parcel_caches(root):
            bounds = data.get('bounds') if isinstance(data, dict) else None
            if _valid_payload(data) and bounds and len(bounds) == 4 and box(*bounds).covers(frame):
                return data, signature(data), 'parcel-caches'
    path, key = frozen_parcels_path(scene, root)
    data = _read_frozen(path, key)
    if data is not None:
        return data, signature(data), 'eyes-parcels'
    if offline or frame is None:
        return None, None, 'unavailable'
    data = fetch_frozen(pad_bounds(scene_lonlat_bounds(scene), DESIGN['fetch_pad_deg']), path, key)
    return data, signature(data), 'vicmap'


# --- Lots -----------------------------------------------------------------------------------------------------

def lot_text(props):
    lot = props.get('lot')
    return '' if lot is None else str(lot)


def is_common(lot):
    return lot.upper().startswith('CM')


def is_numbered(lot):
    return re.fullmatch(r'\d+[A-Za-z]?', lot) is not None


def lot_order(lot):
    match = re.match(r'\d+', lot)
    return (int(match.group()) if match else math.inf, lot)


def _polygon_coordinates(geometry):
    coordinates = geometry.get('coordinates') or []
    return [coordinates] if geometry.get('type') == 'Polygon' else coordinates


def geometry_key(geometry, digits=DESIGN['coordinate_round']):
    """Rounded-coordinate key so stacked strata lots (one polygon, several lots) collapse to one group."""
    factor = 10**digits
    return json.dumps([[[[round(p[0]*factor), round(p[1]*factor)] for p in ring] for ring in polygon]
                       for polygon in _polygon_coordinates(geometry)])


def lot_label(lots):
    """'Lot 7', 'Common property CM1', 'Lots 1-4 + CM1' (stacked or grouped)."""
    names = sorted((lot_text(entry) if isinstance(entry, dict) else str(entry) for entry in lots), key=lot_order)
    if not names:
        return 'unnumbered parcel'
    if len(names) == 1:
        return f'Common property {names[0]}' if is_common(names[0]) else f'Lot {names[0]}' if names[0] else 'unnumbered parcel'
    numbered = [n for n in names if is_numbered(n)]
    others = [n or 'unnumbered' for n in names if not is_numbered(n)]
    text = ''
    if len(numbered) == 1:
        text = f'Lot {numbered[0]}'
    elif numbered:
        integers = [int(re.match(r'\d+', n).group()) for n in numbered]
        consecutive = all(b-a == 1 for a, b in zip(integers, integers[1:], strict=False)) and all(n.isdigit() for n in numbered)
        text = f'Lots {numbered[0]}-{numbered[-1]}' if consecutive and len(numbered) > 2 else 'Lots '+', '.join(numbered)
    if others:
        text = f"{text} + {', '.join(others)}" if text else 'Parcels '+', '.join(others)
    return text


def parcel_groups(scene, parcels):
    """Distinct approved parcel polygons in image pixels, each with the lots sharing it (lead lot first: numbered, then CM)."""
    if not _valid_payload(parcels):
        return []
    project = image_projection(scene)
    groups = {}
    for feature in parcels['features']:
        props = feature.get('properties') or {}
        geometry = feature.get('geometry') or {}
        if props.get('status', 'approved') != 'approved' or not geometry:
            continue
        try:
            geo = shape(geometry)
        except (ValueError, TypeError, AttributeError):
            continue
        if geo.is_empty or not geo.is_valid:
            continue
        key = geometry_key(geometry)
        entry = groups.get(key)
        if entry is None:
            entry = groups[key] = {'key': key, 'lots': [], 'polygon': transform(project, geo), 'geometry': geo}
        entry['lots'].append({'pfi': str(props.get('pfi')), 'lot': lot_text(props), 'plan': props.get('plan')})
    for entry in groups.values():
        entry['lots'].sort(key=lambda lot: lot_order(lot['lot']))
        entry['lead'] = entry['lots'][0]
        entry['plans'] = sorted({lot['plan'] for lot in entry['lots'] if lot['plan']})
    return list(groups.values())


def _pixel_polygon(obj):
    polygon = Polygon(obj['ring'], obj.get('holes') or [])
    if polygon.is_valid:
        return polygon
    fixed = make_valid(polygon)
    parts = [g for g in getattr(fixed, 'geoms', [fixed]) if g.geom_type in ('Polygon', 'MultiPolygon')]
    return unary_union(parts) if parts else Polygon()


def _share(polygon, other):
    return float(polygon.intersection(other).area/polygon.area) if polygon.area > 0 else 0.


def _pct(share):
    return f'{round(share*100)}%'


def address_context(groups, seed, frame):
    """The address lot: lots covering > 10% of the CRM seed roof, else the lot containing the address point."""
    context = {'plan': None, 'group_key': None, 'lot': None, 'method': None, 'unit_number': None}
    if seed and seed.get('title'):
        match = re.search(r'\bUNIT\s+(\d+[A-Za-z]?)\b', str(seed['title']), re.I)
        if match:
            context['unit_number'] = match.group(1)
    if not groups or not seed:
        return context
    def choose(group, method, **extra):
        numbered = [lot for lot in group['lots'] if is_numbered(lot['lot'])] or group['lots']
        context.update(plan=numbered[0]['plan'], group_key=group['key'], lot=numbered[0]['lot'], method=method, **extra)
        return context

    overlaps = []
    ring = seed.get('ring')
    if ring and len(ring) >= 3:
        try:
            polygon = _pixel_polygon({'ring': ring})
        except GEOMETRY_ERRORS:
            polygon = Polygon()
        if polygon.area > 0:
            overlaps = sorted(((_share(polygon, g['polygon']), g) for g in groups if polygon.intersects(g['polygon'])),
                              key=lambda item: -item[0])
            overlaps = [(share, g) for share, g in overlaps if share > DESIGN['seed_overlap']]
    # A CRM roof that straddles several lots (a shared roof over three units) names no lot on its own: the address
    # point decides when one lot holds less than half of the seed, and the straddle is recorded.
    if overlaps and overlaps[0][0] > DESIGN['seed_dominant']:
        return choose(overlaps[0][1], 'crm-seed', seed_share=round(overlaps[0][0], 4))
    point = seed.get('point')
    if point:
        pin = Point(point)
        containing = [g for g in groups if g['polygon'].covers(pin)]
        if containing:
            straddle = [g['lead']['lot'] for _, g in overlaps]
            return choose(containing[0], 'address-point', seed_straddles=straddle)
    if overlaps:
        return choose(overlaps[0][1], 'crm-seed', seed_share=round(overlaps[0][0], 4),
                      seed_straddles=[g['lead']['lot'] for _, g in overlaps])
    return context


def plan_lots(groups, plan):
    """Groups carrying a lot of this plan, with their lots restricted to the plan."""
    found = []
    for g in groups:
        lots = [lot for lot in g['lots'] if lot['plan'] == plan]
        if lots:
            found.append({**g, 'lots': lots})
    return found


def unit_site_sentence(groups, plan, frame):
    """'Plan PS824596: Lots 1-4 + CM1 (5 polygons). This image covers Lot 1 fully, Lot 2 82%, ...' or None."""
    members = plan_lots(groups, plan) if plan else []
    lots = [lot for g in members for lot in g['lots']]
    numbered = [lot for lot in lots if is_numbered(lot['lot'])]
    if len(numbered) < 2 or not any(is_common(lot['lot']) for lot in lots):
        return None
    coverage = sorted(((_share(g['polygon'], frame), g) for g in members), key=lambda item: -item[0])
    parts, outside = [], []
    for share, g in coverage:
        label = lot_label(g['lots'])
        if share <= 0:
            outside.append(label)
        else:
            parts.append(f'{label} fully' if share >= DESIGN['full_coverage'] else f'{label} {_pct(share)}')
    text = f"Plan {plan}: {lot_label(lots)} ({len(members)} polygon{'s' if len(members) != 1 else ''}). This image covers {', '.join(parts) or 'none of it'}."
    if outside:
        text += f" Outside the image: {', '.join(outside)}."
    return text


def title_mismatch_sentence(context):
    unit, lot, plan = context.get('unit_number'), context.get('lot'), context.get('plan')
    if not unit or not lot or context.get('method') != 'crm-seed' or lot.upper() == unit.upper():
        return None
    return f'Title says Unit {unit}; the CRM roof sits on Lot {lot} of {plan}. A hint to check the plan, not a correction.'


def analyse(scene, parcels, *, seed=None, review_objects=None):
    """(object_tags, scene_tag) for every live object from latest.json geometry and parcel polygons, plus every carried
    review-layer object in review_objects (review_layer filter applied; tagged from the same geometry, never an input to
    another object's tag); pure."""
    w, h = scene['image_width'], scene['image_height']
    frame = box(0, 0, w, h)
    groups = parcel_groups(scene, parcels)
    area_scale = abs(float(np.linalg.det(np.asarray(scene['pixel_to_meters'], dtype=float)[:2])))
    live = _live(scene)
    review = review_layer(review_objects, {o['id'] for o in live})
    replaced = {r for o in live for r in (o.get('replaces') or []) if o.get('status') not in DEAD_STATUSES}
    context = address_context(groups, seed, frame)
    address_plan_common = bool(context['plan']) and any(is_common(lot['lot']) for g in plan_lots(groups, context['plan']) for lot in g['lots'])
    polygons = {}
    for obj in [*live, *review]:
        try:
            polygon = _pixel_polygon(obj)
        except GEOMETRY_ERRORS:
            polygon = None
        polygons[obj['id']] = polygon if polygon is not None and not polygon.is_empty and polygon.area > 0 else None
    members = {g['key']: [] for g in groups}
    for obj in live:
        polygon = polygons.get(obj['id'])
        if obj.get('kind') != 'building' or obj.get('status') in DEAD_STATUSES or polygon is None:
            continue
        for g in groups:
            if polygon.intersects(g['polygon']) and _share(polygon, g['polygon']) >= DESIGN['member_share']:
                members[g['key']].append({'object_id': obj['id'], 'area_m2': round(polygon.area*area_scale, 2)})
    for entries in members.values():
        entries.sort(key=lambda m: (-m['area_m2'], m['object_id']))
    tags, counts = {}, Counter()
    for layer, obj in [*(('model', o) for o in live), *(('review', o) for o in review)]:
        polygon = polygons.get(obj['id'])
        counted = counts if layer == 'model' else Counter()  # review-layer flags never enter the scene's flag_counts
        tag = {'kind': obj.get('kind'), 'status': obj.get('status'), 'registration': registration(scene),
               'dominant': None, 'lots': [], 'lot_complete_in_image': None, 'address_plan': context['plan'],
               'on_address_lot': None, 'lot_members': [], 'lot_primary': None, 'flags': [], 'sentences': []}
        if polygon is None:
            tag['flags'].append('uncheckable_geometry')
            tag['area_m2'] = None
            tags[obj['id']] = tag
            counted['uncheckable_geometry'] += 1
            continue
        tag['area_m2'] = round(polygon.area*area_scale, 3)
        edge_px = float(polygon.distance(frame.boundary))
        tag['edge_px'] = round(edge_px, 2)
        clipped = obj.get('clipped_by_frame')
        remnant = bool(clipped) and obj['id'] not in replaced
        frame_cut = edge_px <= DESIGN['edge_px'] or remnant
        shares = sorted(((_share(polygon, g['polygon']), g) for g in groups if polygon.intersects(g['polygon'])),
                        key=lambda item: (-item[0], lot_order(item[1]['lead']['lot'])))
        for share, g in shares:
            if share < DESIGN['lot_share_min']:
                continue
            tag['lots'].append({'pfi': g['lead']['pfi'], 'lot': g['lead']['lot'], 'plan': g['lead']['plan'], 'share': round(share, 4),
                                'kind': 'common_property' if is_common(g['lead']['lot']) else 'lot', 'key': g['key'],
                                'stacked_lots': [lot['lot'] for lot in g['lots']] if len(g['lots']) > 1 else [],
                                'complete_in_image': bool(frame.covers(g['polygon']))})
        noun = KIND_NOUNS.get(obj.get('kind'), 'object')
        if frame_cut:
            tag['flags'].append('frame_cut')
            tag['sentences'].append(f'Touches the image edge; the frame cut it, not the {noun}.' if edge_px <= DESIGN['edge_px']
                                    else 'Cut by the earlier 512 px frame; nothing replaced it when the scene was widened.')
            if remnant:
                tag['clipped_by_frame'] = list(clipped)
        if not tag['lots']:
            if groups:
                tag['flags'].append('no_lot')
            tags[obj['id']] = tag
            counted.update(tag['flags'])
            continue
        dominant = tag['lots'][0]
        tag['dominant'] = {k: dominant[k] for k in ('pfi', 'lot', 'plan', 'share', 'kind')}
        tag['lot_complete_in_image'] = dominant['complete_in_image']
        same_group = dominant['key'] == context['group_key']
        same_block = bool(context['plan']) and dominant['plan'] == context['plan'] and address_plan_common
        tag['on_address_lot'] = same_group if context['plan'] else None
        tag['lot_members'] = list(members.get(dominant['key'], []))
        tag['lot_primary'] = tag['lot_members'][0]['object_id'] if tag['lot_members'] else None
        label = f"{lot_label([dominant['lot']])} {dominant['plan']}".strip()
        if not dominant['complete_in_image']:
            tag['flags'].append('lot_incomplete_in_frame')
        if context['plan'] and not same_group and not same_block and not dominant['complete_in_image']:
            tag['flags'].append('lot_unassociated')
            tag['sentences'].append(f"On neighbouring {label}, not the address lot; " + ('cut by the frame.' if frame_cut else 'the lot leaves this image.'))
        elif same_block and not same_group and dominant['kind'] == 'lot':
            # Another numbered lot of the address plan on a unit site: the block is the address's, the unit is a neighbour's.
            tag['neighbouring_unit'] = True
            tag['sentences'].append(f"On Lot {dominant['lot']} of the address plan {dominant['plan']}: a neighbouring unit of the same block.")
        numbered = [lot for lot in tag['lots'] if is_numbered(lot['lot']) and lot['share'] >= DESIGN['spans_lot_share']]
        if len(numbered) >= 2:
            tag['flags'].append('spans_lots')
            plans = sorted({lot['plan'] for lot in numbered if lot['plan']})
            parts = ' and '.join(f"Lot {lot['lot']} ({_pct(lot['share'])})" for lot in numbered)
            ending = 'a cadastral cut, not a roof fold.' if obj.get('kind') == 'building' else f'the lot boundary crosses this {noun}.'
            tag['sentences'].append(f"Spans {parts} of {', '.join(plans) or 'plan unknown'}; {ending}")
        if dominant['kind'] == 'common_property':
            tag['flags'].append('common_property')
            tag['sentences'].append(f"On common property {dominant['lot']} of {dominant['plan']}.")
        if obj.get('kind') == 'building' and tag['lot_primary'] and tag['lot_primary'] != obj['id']:
            primary = tag['lot_members'][0]
            if primary['area_m2'] > 0:
                tag['primary_ratio'] = round(tag['area_m2']/primary['area_m2'], 3)
        tags[obj['id']] = tag
        counted.update(tag['flags'])
    scene_tag = {'address_id': scene.get('address_id'), 'address_plan': context['plan'], 'address_lot': context['lot'],
                 'address_method': context['method'], 'address_group_key': context['group_key'], 'registration': registration(scene),
                 'unit_site': False, 'unit_site_sentence': None, 'title_mismatch_sentence': title_mismatch_sentence(context),
                 'groups': len(groups), 'objects': len(live), 'review_objects': len(review), 'flag_counts': dict(sorted(counts.items()))}
    sentence = unit_site_sentence(groups, context['plan'], frame)
    if sentence:
        scene_tag.update(unit_site=True, unit_site_sentence=sentence)
    return tags, scene_tag


def lot_tags(scene, parcels, *, seed=None):
    """{object_id: tag} for every live object of every kind (see analyse)."""
    return analyse(scene, parcels, seed=seed)[0]


# --- Rows -----------------------------------------------------------------------------------------------------

def load_scene(root, scene_id):
    return json.loads((Path(root)/'data/scenes'/validate_id(scene_id)/'latest.json').read_text())


def review_layer(objects, live_ids):
    """The review-layer subset of review.json objects: dicts with a non-empty carried_from, a status other than superseded
    and a string id that is not a live latest.json object; one object per id (first wins)."""
    chosen = {}
    for obj in objects or []:
        identity = obj.get('id') if isinstance(obj, dict) else None
        if (isinstance(identity, str) and identity not in live_ids and identity not in chosen
                and obj.get('carried_from') and obj.get('status') != 'superseded'):
            chosen[identity] = obj
    return list(chosen.values())


def review_layer_objects(scene, root):
    """Carried review-layer objects of data/scenes/<id>/review.json (read only; [] when absent or unreadable)."""
    path = Path(root)/'data/scenes'/validate_id(scene['id'])/'review.json'
    try:
        review = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    objects = review.get('objects') if isinstance(review, dict) else None
    return review_layer(objects, {o['id'] for o in _live(scene)})


def scene_rows(scene, root, *, offline=False, parcels=None, seed=None, with_lot_frame=True, review_objects=None):
    """(rows, summary) for one scene: one lot-tags-v1 row per live object (layer 'model') and per carried review-layer object
    (layer 'review'; review_objects, else read from the scene's review.json) plus the '_scene' row; nothing written."""
    root = Path(root)
    if parcels is None:
        parcels, parcels_sha256, source = parcels_for_scene(scene, root, offline=offline)
    else:
        parcels_sha256, source = signature(parcels), 'injected'
    if seed is None:
        seed = address_seed(scene, root)
    if review_objects is None:
        review_objects = review_layer_objects(scene, root)
    live = _live(scene)
    review = review_layer(review_objects, {o['id'] for o in live})
    tags, scene_tag = analyse(scene, parcels, seed=seed, review_objects=review)
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    common = {'version': VERSION, 'note': NOTE, 'parcels_sha256': parcels_sha256, 'parcels_source': source,
              'code_sha256': code, 'computed_at': stamp}
    rows = [_row(scene, obj, {**common, 'layer': layer, 'tag': tags[obj['id']]}, VERSION)
            for layer, objects in zip(LAYERS, (live, review), strict=True) for obj in objects if obj['id'] in tags]
    scene_tag.update(parcels_source=source, parcels_sha256=parcels_sha256)
    scene_tag['lot_frame'] = None
    if with_lot_frame and parcels is not None and isinstance(scene.get('address_id'), int):
        try:
            from .scene_lot import lot_frame
            scene_tag['lot_frame'] = lot_frame(scene['address_id'], root=root, parcels=parcels, source_scene=scene)
        except (OSError, *GEOMETRY_ERRORS) as error:
            scene_tag['lot_frame'] = {'error': str(error)}
    rows.append(_row(scene, {'id': SCENE_OBJECT_ID}, {**common, 'scene': scene_tag}, VERSION))
    return rows, scene_tag


def run(scene_ids, root=Path('.'), *, offline=False, write=False, log=None):
    """Tag scenes and (write=True) replace their lot-tags-v1 rows in the eyes index; returns (rows, summary)."""
    root, scene_ids = Path(root), list(dict.fromkeys(scene_ids))
    rows, summary = [], {}
    for index, scene_id in enumerate(scene_ids):
        scene = load_scene(root, scene_id)
        scene_rows_, scene_tag = scene_rows(scene, root, offline=offline)
        rows.extend(scene_rows_)
        plan = scene_tag.get('lot_frame') or {}
        summary[scene_id] = {'objects': scene_tag['objects'], 'review_objects': scene_tag['review_objects'],
                             'parcels_source': scene_tag['parcels_source'],
                             'registration': scene_tag['registration'], 'flag_counts': scene_tag['flag_counts'],
                             'address_plan': scene_tag['address_plan'], 'address_lot': scene_tag['address_lot'],
                             'unit_site_sentence': scene_tag['unit_site_sentence'],
                             'title_mismatch_sentence': scene_tag['title_mismatch_sentence'],
                             'lot_frame_sentence': plan.get('sentence') or plan.get('error')}
        if log:
            log(f"[{index+1}/{len(scene_ids)}] {scene_id}: {scene_tag['objects']} objects + {scene_tag['review_objects']} review-layer, "
                f"parcels={scene_tag['parcels_source']}, flags={scene_tag['flag_counts']}")
    written = 0
    if write and rows:
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            written = replace_scene_rows(db, TABLE, VERSION, scene_ids, rows)
    return rows, {'version': VERSION, 'written': written, 'scenes': summary}


def _report_path(root, report):
    path = Path(report)
    allowed = (Path(root)/EYES_DIR).resolve()
    if not path.resolve().is_relative_to(allowed):
        raise ValueError(f'--report must be under {EYES_DIR}')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Per-object lot tags from Vicmap Parcel: frame cuts, neighbouring lots, unit sites (agent hints, not labels).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--scenes', nargs='+', required=True)
    parser.add_argument('--offline', action='store_true', help='Never read Vicmap; report parcels unavailable instead.')
    parser.add_argument('--write', action='store_true', help='Replace the scenes\' lot-tags-v1 rows in the eyes index (sidecar only).')
    parser.add_argument('--report', help='Also write the full tags as JSON to this path (under data/reviews/eyes).')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    root = Path(args.root)
    report = _report_path(root, args.report) if args.report else None
    log = None if args.quiet else (lambda text: print(text, file=sys.stderr, flush=True))
    rows, summary = run(args.scenes, root, offline=args.offline, write=args.write, log=log)
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({**summary, 'rows': rows}, indent=1, sort_keys=True))
    json.dump(summary, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
