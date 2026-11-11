"""Read-only address evidence for scene roofs; capture centres are not addresses.

Explicit object links win. Parcel suggestions require a dominant parcel and one
address point on that parcel. Shared lots, partial coverage and conflicts remain
ambiguous. Suggestions never become review labels or training targets here.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import transform

WORLD = 40075016.68557849


def catalog(ids, *, root=Path('.')):
    ids = sorted({v for v in ids if type(v) is int and v > 0})
    if not ids:
        return {}
    with sqlite3.connect(f'file:{root / "data/australia_geo.db"}?mode=ro', uri=True) as db:
        rows = db.execute(f'SELECT id, full_address FROM addresses WHERE id IN ({",".join("?" for _ in ids)})', ids).fetchall()
    return {str(key): title for key, title in rows}


def nearby(bounds, *, root=Path('.')):
    w, s, e, n = bounds
    with sqlite3.connect(f'file:{root / "data/australia_geo.db"}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT a.id AS address_id, a.full_address, a.lon, a.lat
            FROM addresses a JOIN addresses_rtree r ON r.id=a.id
            WHERE r.minX<=? AND r.maxX>=? AND r.minY<=? AND r.maxY>=?
            AND a.lon BETWEEN ? AND ? AND a.lat BETWEEN ? AND ? LIMIT 5001''',
            (e, w, n, s, w, e, s, n)).fetchall()
    if len(rows) > 5000:
        raise ValueError('Address coverage exceeds the bounded lookup')
    return [dict(row) for row in rows]


def image_projection(scene):
    lon, lat = scene['centroid']['lon'], scene['centroid']['lat']
    scale = WORLD * math.cos(math.radians(lat))
    y0 = math.asinh(math.tan(math.radians(lat))) / (2 * math.pi)
    m = scene['meters_to_pixel']

    def project(x, y, z=None):
        x = (x-lon) / 360 * scale
        y = (math.asinh(math.tan(math.radians(y))) / (2*math.pi)-y0) * scale
        return x*m[0][0]+y*m[1][0]+m[2][0], x*m[0][1]+y*m[1][1]+m[2][1]
    return project


def match_addresses(scene, objects, addresses, parcels, names, bounds):
    project = image_projection(scene)
    pins = [(a, Point(project(a['lon'], a['lat']))) for a in addresses]
    features = []
    coverage = box(*bounds) if bounds else None
    for feature in (parcels or {}).get('features', []):
        geo = shape(feature['geometry'])
        if geo.is_valid and not geo.is_empty:
            features.append((feature['properties'], transform(project, geo), bool(coverage is not None and coverage.covers(geo))))
    results = []
    for obj in objects:
        if obj['kind'] != 'building' or obj['status'] in ('rejected', 'superseded'):
            continue
        result = {'object_id': obj['id'], 'status': 'unmatched', 'method': 'unresolved', 'candidates': [],
                  'reason': 'No unique parcel or address-point match. Choose the address after checking the property.'}
        linked = obj.get('address_id')
        if linked and str(linked) in names:
            result.update(status='linked', method='saved-link', address_id=linked, full_address=names[str(linked)],
                          reason='Address linked to this roof in the saved scene.')
            results.append(result)
            continue
        if linked:
            result['reason'] = 'The saved address ID is missing from the address index. Review this link.'
            results.append(result)
            continue
        roof = Polygon(obj['ring'], obj.get('holes') or [])
        if not roof.is_valid or roof.area <= 0:
            results.append(result)
            continue
        overlaps = sorted(((roof.intersection(poly).area/roof.area, props, poly, complete)
                           for props, poly, complete in features if roof.intersects(poly)), key=lambda v: -v[0])
        if overlaps and overlaps[0][0] >= .2:
            ratio, props, parcel, complete = overlaps[0]
            candidates = [a for a, pin in pins if parcel.covers(pin)]
            result.update(status='ambiguous', method='parcel', parcel_pfi=props['pfi'],
                          reason='This roof spans parcels, shares an address lot, or has incomplete parcel evidence.')
            common = str(props.get('lot', '')).upper().startswith(('CM', 'CP'))
            boundary_pin = any(parcel.boundary.distance(pin) < 1e-5 for a, pin in pins if a in candidates)
            # Pin precision is not ownership proof. Never break shared-lot ties by distance.
            unique = ratio >= .85 and (len(overlaps) < 2 or overlaps[1][0] <= .15)
            if unique and complete and not common and not boundary_pin and len(candidates) == 1:
                candidate = candidates[0]
                result.update(status='suggested', address_id=candidate['address_id'], full_address=candidate['full_address'],
                              reason='Roof overlaps one parcel with a single address point. Confirm before saving the link.')
        else:
            candidates = [a for a, pin in pins if roof.contains(pin)]
            if len(candidates) == 1:
                candidate = candidates[0]
                result.update(status='suggested', method='address-point', address_id=candidate['address_id'], full_address=candidate['full_address'],
                              reason='One address point is inside this roof. Parcel ownership is unverified; confirm the link.')
            elif candidates:
                result.update(status='ambiguous', method='address-point', reason='Multiple address points fall inside this roof. Select the correct unit or property.')
        result['candidates'] = [{'address_id': a['address_id'], 'full_address': a['full_address']} for a in candidates]
        results.append(result)
    return results


def resolve(payload, *, root=Path('.')):
    scene, objects = payload['scene'], payload['objects']
    names = catalog([o.get('address_id') for o in objects], root=root)
    bounds = payload.get('bounds')
    # Unregistered images can retain explicit links but cannot infer geography.
    registered = bool(scene.get('region') and bounds)
    addresses = nearby(bounds, root=root) if registered else []
    return match_addresses(scene, objects, addresses, payload.get('parcels') if registered else None, names, bounds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', help='Comma-separated address IDs; reads the local index only')
    parser.add_argument('--properties', action='store_true', help='Resolve cached official property-address evidence')
    args = parser.parse_args()
    if args.properties:
        from .building_addresses import resolve_scene
        payload = json.load(sys.stdin)
        result = resolve_scene(payload['scene'], payload.get('objects'))
    else:
        result = catalog([int(v) for v in args.catalog.split(',') if v]) if args.catalog is not None else resolve(json.load(sys.stdin))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
