"""Read saved scene layers for the map. Never writes annotations or predictions."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .tiles import WORLD_SPAN_M, lonlat_to_tile_xy, tile_xy_to_lonlat


def local_polygon(obj, scene, origin, scale):
    center = scene['centroid']
    anchor = np.array(lonlat_to_tile_xy(center['lon'], center['lat'], 0))
    ratio = scale/(WORLD_SPAN_M*math.cos(math.radians(center['lat'])))
    frame = np.asarray(scene['pixel_to_meters'])
    def ring(points):
        return (np.c_[points, np.ones(len(points))] @ frame)*ratio+(anchor-origin)*[scale, -scale]
    return Polygon(ring(obj['ring']), [ring(h) for h in obj.get('holes', [])])


def render_snapshot(model, visible_ids):
    """Transport geometry, not inference diagnostics; retain hidden mounting parents."""
    included = set(visible_ids)
    objects = {o['id']: o for o in model['objects']}
    pending = list(included)
    while pending:
        parent = objects[pending.pop()].get('parent_id')
        if parent in objects and parent not in included:
            included.add(parent)
            pending.append(parent)
    fields = {'id', 'kind', 'label', 'ring', 'holes', 'status', 'geometry_reviewed',
              'visibility', 'model', 'parent_id', 'roof_annotation', 'roof_parts'}
    return {**{k: v for k, v in model.items() if k != 'analysis'},
            'objects': [{k: v for k, v in o.items() if k in fields and not (k == 'roof_parts' and o.get('roof_annotation'))}
                        for o in model['objects'] if o['id'] in included]}


def map_layers(bounds, layer='edited', *, root=Path('.'), limit=24, focus=None):
    if layer not in ('edited', 'model') or len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
        raise ValueError('Choose valid map bounds and a layer')
    west, south, east, north = bounds
    if not (-180 < west < east < 180 and -85 < south < north < 85):
        raise ValueError('Unsupported map bounds')
    lon, lat = (west+east)/2, (south+north)/2
    scale = WORLD_SPAN_M*math.cos(math.radians(lat))
    origin = np.array(lonlat_to_tile_xy(lon, lat, 0))
    a = (np.array(lonlat_to_tile_xy(west, south, 0))-origin)*[scale, -scale]
    b = (np.array(lonlat_to_tile_xy(east, north, 0))-origin)*[scale, -scale]
    if max(b-a) > 2500:
        raise ValueError('Zoom in to see analysed objects (up to 2.5 km across)')
    viewport = box(*a, *b)
    if focus is not None and (len(focus) != 2 or not all(math.isfinite(v) for v in focus) or not (-180 < focus[0] < 180 and -85 < focus[1] < 85)):
        raise ValueError('Choose a valid map focus')
    focal_point = Point((np.array(lonlat_to_tile_xy(*focus, 0))-origin)*[scale, -scale]) if focus is not None else viewport.centroid
    scenes, unavailable = [], 0
    indexed = {}
    index_path = root/'data/suburbs/mooroolbark/confidence.sqlite'
    if index_path.exists():
        with closing(sqlite3.connect(f'file:{index_path}?mode=ro', uri=True)) as db:
            indexed = {sid: (stamp, json.loads(bounds)) for sid, stamp, bounds in db.execute('SELECT id,mtime,bounds FROM scenes')}
    def geographic(polygon):
        def ring(points):
            return [list(tile_xy_to_lonlat(*(origin+np.array(p)*[1, -1]/scale), 0)) for p in points]
        return {'type': 'Polygon', 'coordinates': [ring(polygon.exterior.coords), *[ring(h.coords) for h in polygon.interiors]]}
    for directory in sorted((root/'data/scenes').glob('*')):
        if not directory.is_dir() or directory.name.startswith('qa_') or not (directory/'latest.json').exists():
            continue
        try:
            # Frozen suburb captures can be excluded by their small spatial
            # index before reading the much larger mesh JSON. Reviewed frames
            # always take the existing recovery path below.
            cached = indexed.get(directory.name)
            if cached and not (directory/'review.json').exists() and (directory/'latest.json').stat().st_mtime_ns == cached[0]:
                w, s, e, n = cached[1]
                if e < west or w > east or n < south or s > north:
                    continue
            model = json.loads((directory/'latest.json').read_text())
            width, height = model['image_width'], model['image_height']
            image_ring = {'ring': [[0, 0], [width, 0], [width, height], [0, height]]}
            extent = local_polygon(image_ring, model, origin, scale)
            if not extent.intersects(viewport):
                continue
            review_path = directory/'review.json'
            review = json.loads(review_path.read_text()) if review_path.exists() else None
            stale = bool(review and review['model_signature'] != model['model_signature'])
            edited_ids = set()
            if layer == 'edited' and review:
                if stale:
                    signature = review['model_signature']
                    if len(signature) != 64 or any(c not in '0123456789abcdef' for c in signature):
                        raise ValueError('Invalid saved snapshot')
                    model = json.loads((directory/'models'/f'{signature}.json').read_text())
                    image_ring = {'ring': [[0, 0], [model['image_width'], 0], [model['image_width'], model['image_height']], [0, model['image_height']]]}
                    extent = local_polygon(image_ring, model, origin, scale)
                if not extent.intersects(viewport):
                    continue
                originals = {o['id']: o for o in model['objects']}
                edited_ids = {o['id'] for o in review['objects'] if o != originals.get(o['id'])}
                model = {**model, 'objects': review['objects'], 'groups': review['groups']}
            core = extent
            if model.get('analysis_core'):
                geometry = model['analysis_core']
                def convert(points):
                    return [(np.array(lonlat_to_tile_xy(*p, 0))-origin)*[scale, -scale] for p in points]
                polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
                core = unary_union([Polygon(convert(rings[0]), [convert(h) for h in rings[1:]]) for rings in polygons])
            scenes.append({'model': model, 'stale': stale, 'revision': review['revision'] if review and layer == 'edited' else 0,
                           'saved_at': review.get('updated_at', '') if review else '',
                           'edited_ids': edited_ids, 'review_only': False,
                           'extent': extent, 'coverage_extent': core, 'distance': extent.centroid.distance(focal_point), 'visible_ids': []})
        except (ValueError, KeyError, OSError, TypeError):
            unavailable += 1
    # A dense collection of old one-house training crops must not exhaust the
    # budget before the current suburb tiles can render. Saved edits are added
    # separately, without dragging every unchanged neighbour into that budget.
    scenes.sort(key=lambda s: (0 if s['model'].get('suburb_run') else 1,
                               s['distance'], s['model']['id']))
    total = len(scenes)
    scenes = scenes[:limit] + [{**s, 'review_only': True} for s in scenes[limit:] if s['revision']]
    candidates = []
    for index, entry in enumerate(scenes):
        owned = set(entry['model'].get('owned_object_ids', [o['id'] for o in entry['model']['objects']]))
        for obj in entry['model']['objects']:
            if entry['review_only'] and obj['id'] not in entry['edited_ids'] and obj['status'] == 'candidate':
                continue
            if obj['id'] not in owned and not (entry['revision'] and (obj['status'] != 'candidate' or obj['id'] in entry['edited_ids'])):
                continue
            if obj['status'] == 'superseded':
                continue
            polygon = local_polygon(obj, entry['model'], origin, scale)
            if not polygon.is_valid or polygon.area <= 0 or not polygon.intersects(viewport):
                continue
            checked = entry['revision'] and (obj['status'] in ('confirmed', 'needs_adjustment', 'rejected') or obj['id'] in entry['edited_ids'])
            priority = 3 if checked and (obj['status'] == 'rejected' or obj.get('geometry_reviewed')) else 2 if checked else 0
            rank = (priority, entry['saved_at'] if checked else '',
                    bool(entry['model'].get('suburb_run')), -entry['extent'].area)
            candidates.append((rank, index, obj, polygon))
    candidates.sort(key=lambda c: c[0], reverse=True)
    spatial = STRtree([c[3] for c in candidates])
    kept, duplicate_count, features = set(), 0, []
    for candidate_id, (_rank, index, obj, polygon) in enumerate(candidates):
        # Across captures only: retain separate instances in the source scene.
        # Confirmed corrections outrank proposals. Rejections veto duplicate
        # proposals without deleting either source or counting a new label.
        duplicate = False
        for neighbour in spatial.query(polygon):
            if neighbour not in kept:
                continue
            _, other_index, other, shape = candidates[neighbour]
            if index == other_index or obj['kind'] != other['kind'] or not polygon.intersects(shape):
                continue
            overlap = polygon.intersection(shape).area
            if overlap/max(.01, polygon.area+shape.area-overlap) >= .65:
                duplicate = True
                break
        if duplicate:
            duplicate_count += 1
            continue
        kept.add(candidate_id)
        if obj['status'] == 'rejected':
            continue
        entry = scenes[index]
        entry['visible_ids'].append(obj['id'])
        features.append({'type': 'Feature', 'geometry': geographic(polygon), 'properties': {
            'scene_id': entry['model']['id'], 'object_id': obj['id'], 'kind': obj['kind'], 'label': obj['label'],
            'status': obj['status'], 'revision': entry['revision'], 'stale': entry['stale']}})
    # Suppress old tile meshes only where this class has actually been analysed.
    coverage = {}
    for kind in ('building', 'tree'):
        complete = [s['coverage_extent'].intersection(viewport) for s in scenes
                    if not s['review_only'] and s['model'].get('scans', {}).get(kind, {}).get('status') == 'complete']
        corrections = [polygon.intersection(viewport) for _, index, obj, polygon in candidates
                       if scenes[index]['review_only'] and obj['kind'] == kind]
        combined = unary_union(complete + corrections)
        parts = [combined] if combined.geom_type == 'Polygon' else list(combined.geoms)
        coverage[kind] = {'type': 'MultiPolygon', 'coordinates': [geographic(p)['coordinates'] for p in parts if p.geom_type == 'Polygon' and not p.is_empty]}
    return {'scenes': [{**{k: v for k, v in s.items() if k not in ('extent', 'coverage_extent', 'distance', 'saved_at', 'model', 'edited_ids', 'review_only')},
                       'model': render_snapshot(s['model'], s['visible_ids'])} for s in scenes if s['visible_ids']],
            'outlines': {'type': 'FeatureCollection', 'features': features}, 'coverage': coverage,
            'duplicates': duplicate_count, 'available_scenes': total, 'limited': total > limit, 'unavailable': unavailable}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bounds', nargs=4, type=float, required=True)
    parser.add_argument('--layer', choices=['edited', 'model'], default='edited')
    parser.add_argument('--focus', nargs=2, type=float)
    args = parser.parse_args()
    print(json.dumps(map_layers(args.bounds, args.layer, focus=args.focus), separators=(',', ':')))


if __name__ == '__main__':
    main()
