"""Read-only roof/property address evidence from a complete Vicmap snapshot.

Property polygons, not the capture title or nearest street number, assign a
site address. Multiple unit addresses remain ambiguous. This does not change
model geometry, local CRM IDs, reviews, confidence scores or training labels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from shapely import make_valid
from shapely.geometry import box, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

SERVICE = 'https://services-ap1.arcgis.com/P744lA0wf4LlBZ84/arcgis/rest/services/Vicmap_Property_Address/FeatureServer/0'
DATA = Path('data/address-evidence/vicmap-property-address.json')
SOURCE = {'name': 'Vicmap Property Address', 'url': SERVICE,
          'attribution': 'State of Victoria', 'license': 'CC BY 4.0'}
FIELDS = ('OBJECTID,prop_pfi,prop_status,propv_z_level,add_pfi,add_property_pfi,add_ezi_address,add_is_primary,add_source_verified,'
          'add_blg_unit_type,add_blg_unit_id_1,add_blg_unit_id_2,add_floor_type,add_floor_no_1,add_floor_no_2,'
          'add_building_name,add_complex_name,add_complex_site,add_num_road_address,add_locality_name,add_postcode')
WHERE = "prop_status = 'A' AND propv_z_level IN ('G','S')"
VERSION = 'property-address-v1'
RESOLVER_VERSION = 'property-resolution-v2'


def request(params):
    with urlopen(SERVICE+'/query?'+urlencode(params), timeout=60) as response:
        data = json.load(response)
    if data.get('error'):
        raise ValueError('Vicmap query failed: '+str(data['error']))
    return data


def refresh(bounds, root=Path('.'), fetch=request):
    """Publish only after every page and the provider's total have been checked."""
    w, s, e, n = bounds
    if not all(math.isfinite(v) for v in bounds) or not (0 < e-w <= .2 and 0 < n-s <= .2):
        raise ValueError('Expected a bounded suburb extent')
    base = dict(geometry=','.join(map(str, bounds)), geometryType='esriGeometryEnvelope',
                inSR=4326, outSR=4326, spatialRel='esriSpatialRelIntersects', where=WHERE)
    total = fetch(dict(base, f='json', returnCountOnly='true'))['count']
    if not 0 < total <= 50000:
        raise ValueError('Unexpected property-address record count')
    features, seen = [], set()
    while len(features) < total:
        page = fetch(dict(base, f='geojson', outFields=FIELDS, returnGeometry='true',
                          orderByFields='OBJECTID', resultOffset=len(features), resultRecordCount=2000))
        batch = page.get('features', [])
        if not batch:
            raise ValueError('Incomplete property-address pagination')
        for feature in batch:
            props = feature['properties']
            oid = props.get('OBJECTID')
            geometry = shape(feature['geometry'])
            if oid is None or oid in seen or geometry.is_empty or geometry.geom_type not in ('Polygon', 'MultiPolygon'):
                raise ValueError('Invalid or repeated property-address record')
            if not all(math.isfinite(v) for v in geometry.bounds) or not props.get('prop_pfi'):
                raise ValueError('Invalid property geometry or identity')
            seen.add(oid)
            features.append(feature)
        print(f'Fetched {len(features)}/{total} property-address records', flush=True)
    if len(features) != total or fetch(dict(base, f='json', returnCountOnly='true'))['count'] != total:
        raise ValueError('Property-address count changed during capture; previous snapshot retained')
    payload = dict(type='FeatureCollection', version=VERSION, source=SOURCE, bounds=bounds,
                   fetched_at=datetime.now(UTC).isoformat(), complete=True, features=features)
    payload['dataset_signature'] = hashlib.sha256(json.dumps(features, sort_keys=True).encode()).hexdigest()
    # Validate the index before replacing the previous usable snapshot.
    PropertyIndex(payload)
    destination = root/DATA
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=destination.parent, delete=False) as stream:
        json.dump(payload, stream, separators=(',', ':'))
        temporary = stream.name
    os.replace(temporary, destination)
    return {'records': total, 'dataset_signature': payload['dataset_signature'], 'path': str(destination)}


def unresolved(reason='No property address is available for this roof.'):
    return {'status': 'unmatched', 'method': 'unresolved', 'candidates': [], 'reason': reason}


class PropertyIndex:
    def __init__(self, data):
        if data.get('complete') is not True or data.get('version') != VERSION:
            raise ValueError('Incomplete or unsupported property address snapshot')
        self.coverage = box(*data['bounds'])
        self.provenance = {k: data[k] for k in ('source', 'fetched_at', 'dataset_signature')}
        groups = {}
        for feature in data['features']:
            props = feature['properties']
            if props.get('prop_status') != 'A' or props.get('propv_z_level') not in ('G', 'S'):
                continue
            pfi = str(props['prop_pfi'])
            group = groups.setdefault(pfi, {'geometries': [], 'addresses': {}})
            group['geometries'].append(make_valid(shape(feature['geometry'])))
            address = props.get('add_ezi_address')
            # A joined but unrelated address must never be treated as a property match.
            if address and props.get('add_pfi') and str(props.get('add_property_pfi')) == pfi:
                group['addresses'][str(props['add_pfi'])] = {
                    'full_address': address.strip(), 'address_pfi': str(props['add_pfi']), 'property_pfi': pfi,
                    **{key: props.get('add_'+key) for key in ('blg_unit_type', 'blg_unit_id_1', 'blg_unit_id_2',
                        'floor_type', 'floor_no_1', 'floor_no_2', 'building_name', 'complex_name', 'complex_site',
                        'num_road_address', 'locality_name', 'postcode') if props.get('add_'+key) is not None}}
        self.records, self.geometries = [], []
        for pfi, group in groups.items():
            geometry = unary_union(group['geometries'])
            if geometry.is_empty or geometry.area <= 0:
                continue
            self.records.append({'property_pfi': pfi, 'candidates': list(group['addresses'].values())})
            self.geometries.append(geometry)
        self.tree = STRtree(self.geometries)

    def match(self, roof):
        if roof.is_empty or not roof.is_valid or roof.area <= 0:
            return unresolved('Roof geometry needs review before its address can be mapped.')
        if not self.coverage.covers(roof):
            return unresolved('This roof extends outside the downloaded address coverage.')
        overlaps = sorted([(roof.intersection(self.geometries[i]).area/roof.area, int(i))
                           for i in self.tree.query(roof, predicate='intersects')], reverse=True)
        overlaps = [(ratio, i) for ratio, i in overlaps if ratio > .05]
        result = {**unresolved(), **self.provenance}
        if not overlaps:
            return result
        from .property_address_resolution import refine
        resolution = refine(roof, overlaps, self.records, self.geometries)
        if resolution is not None:
            return {**result, **resolution, 'resolver_version': RESOLVER_VERSION}
        ratio, index = overlaps[0]
        dominant = ratio >= .85 and (len(overlaps) == 1 or overlaps[1][0] <= .15)
        relevant = [(ratio, index)] if dominant else overlaps
        candidates = [candidate for _, i in relevant for candidate in self.records[i]['candidates']]
        candidates = list({(c['property_pfi'], c['address_pfi']): c for c in candidates}.values())
        result.update(method='vicmap-property', candidates=candidates, overlap=round(ratio, 4), resolver_version=RESOLVER_VERSION)
        unique_names = {c['full_address'] for c in candidates}
        if dominant and len(unique_names) == 1:
            result.update(status='matched', full_address=candidates[0]['full_address'],
                          property_pfi=self.records[index]['property_pfi'],
                          reason='Roof overlaps one addressed property. This identifies the site, not an individual tenancy or surveyed roof boundary.')
        elif candidates:
            result.update(status='ambiguous', reason='Several addresses or properties may apply. Review unit boundaries before assigning an individual address.')
        else:
            result['reason'] = 'The intersecting property has no published address in this snapshot.'
        from .property_address_resolution import describe, is_vertical
        if result['status'] == 'matched' and any(is_vertical(c) for c in candidates):
            result.update(status='ambiguous', reason='The address names a floor; the roof alone cannot identify that vertical tenancy.')
            result.pop('full_address', None)
        result.update(describe(candidates, result['status']))
        return result


@lru_cache(maxsize=2)
def _load(filename, mtime, size):
    return PropertyIndex(json.loads(Path(filename).read_text()))


def load_index(root=Path('.')):
    path = root/DATA
    try:
        stat = path.stat()
        return _load(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def linked(obj, names):
    name = names.get(str(obj.get('address_id')))
    if name:
        return {'status': 'linked', 'method': 'saved-link', 'full_address': name,
                'address_id': obj['address_id'], 'candidates': [], 'reason': 'Saved address link for this roof.'}
    return None


def resolve_scene(scene, objects=None, root=Path('.')):
    from .address_resolution import apply, binding
    from .property_address_resolution import image_cut, visible_only
    from .scene_addresses import catalog
    from .scene_confidence import geographic
    objects = scene.get('objects', []) if objects is None else objects
    names = catalog([o.get('address_id') for o in objects], root=root)
    index = load_index(root)
    result = {}
    for obj in objects:
        if obj.get('kind') != 'building':
            continue
        evidence = linked(obj, names)
        if evidence is None:
            evidence = unresolved('Property address coverage has not been downloaded for this area.')
            if index is not None:
                try:
                    evidence = visible_only(index.match(geographic(obj, scene)), image_cut(scene, obj))
                except (ValueError, KeyError, TypeError):
                    evidence = unresolved('This image has no usable geographic registration.')
        try:
            current_binding = binding(geographic(obj, scene), scene.get('model_signature'), evidence, obj.get('address_id'))
        except (KeyError, ValueError, TypeError):
            current_binding = None
        if current_binding is not None and scene.get('id'):
            # A corrupt decision file must fail visibly, not silently fall back
            # to an automatic address as though the reviewer had never saved.
            evidence = apply(evidence, root, scene['id'], obj['id'], current_binding)
        result[obj['id']] = evidence
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-plan', type=Path)
    args = parser.parse_args()
    if args.refresh_plan:
        plan = json.loads(args.refresh_plan.read_text())
        boundary = unary_union([shape(f['geometry']) for f in plan['boundary']['features']])
        # Include border roofs without clipping the returned property geometry.
        print(json.dumps(refresh(list(boundary.buffer(.003).bounds))))
    else:
        import sys
        payload = json.load(sys.stdin)
        print(json.dumps(resolve_scene(payload['scene'], payload.get('objects'))))


if __name__ == '__main__':
    main()
