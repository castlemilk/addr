"""Cadastral review groups: several roof surfaces can belong to one block.

Grouping never unions roof footprints, assigns ownership or declares unit walls.
Public parcel responses are frozen alongside the experiment that consumed them.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform, unary_union

from .scene_addresses import image_projection
from .scene_training import atomic_json

VERSION = 'roof-block-context-v1'
SERVICE = 'https://services-ap1.arcgis.com/P744lA0wf4LlBZ84/ArcGIS/rest/services/Vicmap_Parcel/FeatureServer/0'
SOURCE = {'name': 'Vicmap Parcel', 'url': 'https://discover.data.vic.gov.au/dataset/vicmap-property-rest-api',
          'attribution': '© State of Victoria (Department of Transport and Planning)', 'license': 'CC BY 4.0'}


def fetch_parcels(bounds):
    w, s, e, n = bounds
    if not (140.9 <= w < e <= 150.1 and -39.3 <= s < n <= -33.9 and max(e-w, n-s) < .02):
        raise ValueError('A bounded Victorian parcel lookup is required.')
    features, ids = [], set()
    for offset in range(0, 2000, 500):
        params = {'f': 'geojson', 'geometry': ','.join(map(str, bounds)), 'geometryType': 'esriGeometryEnvelope',
                  'inSR': 4326, 'outSR': 4326, 'spatialRel': 'esriSpatialRelIntersects',
                  'where': "parcel_status='A' AND parv_status='A' AND parv_z_level IN ('G','S')",
                  'outFields': 'OBJECTID,parcel_pfi,parcel_spi,parcel_lot_number,parcel_plan_number,parcel_status,parv_status,parv_z_level,parv_horiz_pos_uncertainty',
                  'returnGeometry': 'true', 'orderByFields': 'OBJECTID', 'resultOffset': offset, 'resultRecordCount': 500}
        with urlopen(SERVICE+'/query?'+urlencode(params), timeout=20) as response:
            data = json.load(response)
        if data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
            raise ValueError('Invalid cadastral response')
        for feature in data['features']:
            p = feature['properties']
            identity = str(p['parcel_pfi'])
            if identity in ids or p['parcel_status'] != 'A' or p['parv_status'] != 'A' or p['parv_z_level'] not in ('G', 'S'):
                raise ValueError('Incomplete or invalid parcel response')
            ids.add(identity)
            features.append({'type': 'Feature', 'geometry': feature['geometry'], 'properties': {
                'pfi': identity, 'spi': p['parcel_spi'], 'plan': p['parcel_plan_number'],
                'lot': p['parcel_lot_number'], 'status': 'approved',
                'horizontal_uncertainty_m': p.get('parv_horiz_pos_uncertainty')}})
        if not data.get('exceededTransferLimit') and not data.get('properties', {}).get('exceededTransferLimit') and len(data['features']) < 500:
            return {'type': 'FeatureCollection', 'features': features, 'bounds': bounds, 'source': SOURCE,
                    'retrieved_at': datetime.now(UTC).isoformat()}
    raise ValueError('Parcel response exceeded the bounded lookup')


def parcel_snapshot(directory, index, scene):
    path = directory/f'parcels-{index}.json'
    if path.exists():
        value = json.loads(path.read_text())
        if value['image_sha256'] != scene['image_sha256'] or value['scene_id'] != scene['id']:
            raise ValueError('Parcel snapshot belongs to another image')
        return value
    try:
        from .scene_property_roofs import inherited_parcels
        data = inherited_parcels(scene) or fetch_parcels(scene['region']['bounds'])
        value = {'data': data}
    except (OSError, ValueError, KeyError, TypeError) as error:
        value = {'data': None, 'error': str(error)}
    value.update(scene_id=scene['id'], image_sha256=scene['image_sha256'])
    atomic_json(path, value)
    return value


def polygons(geometry):
    if geometry.is_empty:
        return []
    return [geometry] if geometry.geom_type == 'Polygon' else [p for p in getattr(geometry, 'geoms', []) if p.geom_type == 'Polygon' and not p.is_empty]


def rings(geometry):
    return [[[list(p) for p in poly.exterior.coords[:-1]],
             *[[list(p) for p in hole.coords[:-1]] for hole in poly.interiors]] for poly in polygons(geometry)]


def plan_component(pool, seeds, gap_px=.5):
    """Indices of the spatially connected component of `pool` (list of (props, polygon, ...)) grown from `seeds`.

    Two lots connect when their polygons lie within gap_px of each other; growth stops at the component's edge so a
    plan number that spans an estate never pulls in disconnected lots. Pure: nothing about the lots is decided.
    """
    component = set(seeds)
    while True:
        more = {i for i, entry in enumerate(pool) if any(entry[1].distance(pool[j][1]) < gap_px for j in component)}
        if more <= component:
            return component
        component |= more


def block_context(scene, obj, parcels):
    empty = {'version': VERSION, 'status': 'unavailable', 'members': [], 'perimeter': [], 'lots': [],
             'reason': 'Cadastral evidence is unavailable; nearby buildings are not automatically grouped.'}
    if not parcels:
        return empty
    project = image_projection(scene)
    target = Polygon(obj['ring'], obj.get('holes', []))
    coverage = box(*parcels['bounds'])
    features = []
    for f in parcels['features']:
        geo, props = shape(f['geometry']), f['properties']
        if geo.is_valid and not geo.is_empty and props.get('status') == 'approved':
            features.append((props, transform(project, geo), coverage.covers(geo)))
    # A plan number can span an entire estate: use only a spatially connected
    # component that includes common property, with explicit size limits.
    candidates = []
    plans = {p.get('plan') for p, poly, _ in features if p.get('plan') and poly.intersection(target).area/target.area > .1}
    for plan in sorted(plans):
        pool = [f for f in features if f[0].get('plan') == plan]
        seeds = [i for i, (_, p, _) in enumerate(pool) if p.intersection(target).area/target.area > .1]
        group = [pool[i] for i in sorted(plan_component(pool, seeds))]
        options = ([group] if any(str(p.get('lot', '')).upper().startswith('CM') for p, _, _ in group)
                   else [[feature] for feature in group])
        for option in options:
            union = unary_union([p for _, p, _ in option])
            ratio = union.intersection(target).area/target.area
            scale = abs(float(np.linalg.det(np.asarray(scene['pixel_to_meters'])[:2])))
            if ratio >= .8 and len(option) <= 32 and union.area*scale <= 12000:
                candidates.append((ratio, option, union, plan))
    if len(candidates) != 1:
        return {**empty, 'reason': 'No unique bounded parcel or common-property plan contains this roof. Check the site boundary.'}
    _, group, perimeter, plan = candidates[0]
    image = box(0, 0, scene['image_width'], scene['image_height'])
    members, ambiguous = [], []
    for roof in scene['objects']:
        if roof.get('kind') != 'building' or roof.get('source') != 'sam3-text-onnx' or roof.get('geometry_reviewed') or roof.get('roof_line_edits') or roof.get('status') in ('rejected', 'superseded'):
            continue
        poly = Polygon(roof['ring'], roof.get('holes', []))
        if not poly.is_valid or poly.area <= 0:
            continue
        ratio = poly.intersection(perimeter).area/poly.area
        if ratio >= .8:
            members.append({'object_id': roof['id'], 'overlap_fraction': round(ratio, 5)})
        elif ratio > .1:
            ambiguous.append(roof['id'])
    members.sort(key=lambda member: (member['object_id'] != obj['id'], member['object_id']))
    complete = all(covered for _, _, covered in group) and image.covers(perimeter)
    return {'version': VERSION, 'status': 'grouped' if complete else 'partial_block', 'plan': plan,
            'reason': 'Separate roof surfaces grouped by cadastral block. Lot boundaries are context, not measured roof seams or confirmed dwelling counts.',
            'members': members, 'ambiguous_object_ids': ambiguous, 'perimeter': rings(perimeter),
            'lots': [{**props, 'polygons': rings(poly)} for props, poly, _ in group],
            'numbered_lots': sum(not str(p.get('lot', '')).upper().startswith('CM') for p, _, _ in group),
            'complete_in_image': complete, 'unit_count': None, 'roof_surfaces_merged': False,
            'source': parcels.get('source', SOURCE), 'retrieved_at': parcels.get('retrieved_at')}


def lot_context(scene, obj, parcels):
    """A shared roof can span numbered lots without a common-property polygon.

Only lots directly intersecting this roof become search regions. This fallback
does not claim an estate-wide development group from a plan number.
"""
    context = block_context(scene, obj, parcels)
    if context['lots'] or not parcels:
        return context
    target = Polygon(obj['ring'], obj.get('holes', []))
    project = image_projection(scene)
    area_scale = abs(float(np.linalg.det(np.asarray(scene['pixel_to_meters'])[:2])))
    selected = []
    for feature in parcels['features']:
        props, geo = feature['properties'], shape(feature['geometry'])
        if (props.get('status') != 'approved' or not props.get('plan')
                or not re.fullmatch(r'\d+[A-Za-z]?', str(props.get('lot', ''))) or not geo.is_valid):
            continue
        polygon = transform(project, geo)
        overlap = polygon.intersection(target).area
        if overlap*area_scale >= 8 and overlap/target.area >= .03:
            selected.append((props, polygon, geo))
    if not 2 <= len(selected) <= 32 or len({p.get('plan') for p, _, _ in selected}) != 1:
        return context
    perimeter = unary_union([p for _, p, _ in selected])
    if perimeter.intersection(target).area/target.area < .8 or perimeter.area*area_scale > 12000:
        return context
    # Overlapping plans/strata cannot be resolved by nearest-area selection.
    if sum(p.area for _, p, _ in selected)-perimeter.area > perimeter.area*.02:
        return context
    members = []
    for roof in scene['objects']:
        if (roof.get('kind') != 'building' or roof.get('source') != 'sam3-text-onnx'
                or roof.get('status') != 'candidate' or roof.get('geometry_reviewed') or roof.get('roof_line_edits') or roof.get('roof_feedback')):
            continue
        polygon = Polygon(roof['ring'], roof.get('holes', []))
        if polygon.is_valid and polygon.area > 0 and polygon.intersection(perimeter).area/polygon.area >= .8:
            members.append({'object_id': roof['id'], 'overlap_fraction': round(polygon.intersection(perimeter).area/polygon.area, 5)})
    complete = box(0, 0, scene['image_width'], scene['image_height']).covers(perimeter) and all(box(*parcels['bounds']).covers(geo) for _, _, geo in selected)
    return {**context, 'status': 'individual_lots', 'plan': selected[0][0].get('plan'),
            'reason': 'Numbered lots intersect this shared roof. Each is a separate search region; no development-wide property group is inferred.',
            'members': members, 'perimeter': rings(perimeter),
            'lots': [{**props, 'polygons': rings(polygon)} for props, polygon, _ in selected],
            'numbered_lots': len(selected), 'complete_in_image': complete, 'unit_count': None,
            'roof_surfaces_merged': False, 'source': parcels.get('source', SOURCE), 'retrieved_at': parcels.get('retrieved_at')}
