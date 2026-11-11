"""OpenStreetMap land-use context (parks, pitches, schools, commercial zones) for building hints.

One Overpass fetch per bounding box, cached under data/reviews/eyes/context; offline the
document is `unavailable` and every consumer must say so. Land use is context for a
reviewer, never a label: a roof next to an oval is not thereby non-residential.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import Polygon, mapping, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .reference_agreement import lonlat_to_local

VERSION = 'context-landuse-v1'
CACHE = Path('data/reviews/eyes/context/osm-landuse.json')
TAGS = {'leisure': ('pitch', 'park', 'sports_centre', 'golf_course', 'playground'),
        'landuse': ('recreation_ground', 'commercial', 'industrial', 'retail', 'education'),
        'amenity': ('school', 'place_of_worship', 'community_centre')}
ENDPOINTS = ('https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter')
USER_AGENT = 'addr-roof-eyes/1.0 (+https://github.com/benebsworth/addr)'


def build_query(bbox, timeout=90):
    west, south, east, north = bbox
    box = f'({south},{west},{north},{east})'
    parts = []
    for key, values in TAGS.items():
        selector = f'["{key}"~"^({"|".join(values)})$"]'
        parts.append(f'way{selector}{box};')
        parts.append(f'relation{selector}{box};')
    return f'[out:json][timeout:{timeout}];(' + ''.join(parts) + ');out geom;'


def _ring(points):
    coords = [(p['lon'], p['lat']) for p in points if 'lon' in p and 'lat' in p]
    if len(coords) < 4 or coords[0] != coords[-1]:
        return None
    polygon = Polygon(coords)
    return polygon if not polygon.is_empty and polygon.area > 0 else None


def _wanted(tags):
    for key, values in TAGS.items():
        if tags.get(key) in values:
            return key, tags[key]
    return None


def parse(payload):
    """Closed ways and multipolygon relation outers carrying a wanted tag, as GeoJSON polygons (WGS84)."""
    features = []
    elements = payload.get('elements') if isinstance(payload, dict) else None
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        tags = element.get('tags') or {}
        match = _wanted(tags)
        if match is None:
            continue
        if element.get('type') == 'way':
            polygon = _ring(element.get('geometry') or [])
        elif element.get('type') == 'relation':
            outers = [_ring(m.get('geometry') or []) for m in element.get('members', []) if m.get('type') == 'way' and m.get('role', 'outer') in ('outer', '')]
            outers = [o for o in outers if o is not None]
            polygon = unary_union(outers).buffer(0) if outers else None
        else:
            polygon = None
        if polygon is None or polygon.is_empty:
            continue
        features.append({'osm_type': element['type'], 'osm_id': element.get('id'), 'tag_key': match[0], 'tag_value': match[1],
                         'name': tags.get('name'), 'geometry': json.loads(json.dumps(mapping(polygon)))})
    return features


def fetch(bbox, timeout=90, endpoints=ENDPOINTS):
    import requests
    query = build_query(bbox, timeout)
    last = None
    for endpoint in endpoints:
        try:
            response = requests.post(endpoint, data={'data': query}, timeout=timeout+30, headers={'User-Agent': USER_AGENT})
            response.raise_for_status()
            return response.json()
        except Exception as error:  # noqa: BLE001 - every endpoint failure is reported to the caller
            last = error
    raise OSError(f'Overpass unavailable: {last}')


def document(features, bbox, status='available', error=None):
    return {'version': VERSION, 'status': status, 'bbox': list(bbox), 'fetched_at': datetime.now(UTC).isoformat(),
            'source': 'OpenStreetMap via Overpass, ODbL', 'tags': {k: list(v) for k, v in TAGS.items()},
            'features': features, **({'error': error} if error else {})}


def load(root=Path('.'), bbox=None, fetch=fetch, refresh=False):
    """Cached land-use document for bbox; `fetch=None` or a failing fetch yields status 'unavailable' (never cached)."""
    root = Path(root)
    cache = root/CACHE
    if not refresh and cache.exists():
        try:
            cached = json.loads(cache.read_text())
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get('version') == VERSION and cached.get('status') == 'available' \
                and (bbox is None or cached.get('bbox') == list(bbox)):
            return cached
    if bbox is None:
        return document([], (0, 0, 0, 0), status='unavailable', error='no bounding box and no cache')
    if fetch is None:
        return document([], bbox, status='unavailable', error='network disabled')
    try:
        payload = fetch(bbox)
    except Exception as error:  # noqa: BLE001 - offline is an expected, reported state
        return document([], bbox, status='unavailable', error=str(error))
    doc = document(parse(payload), bbox)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(doc, separators=(',', ':')))
    return doc


class Landuse:
    """Land-use polygons with per-scene local-metre projections (cached by scene id)."""

    def __init__(self, doc):
        self.status = doc.get('status', 'unavailable')
        bbox = doc.get('bbox')
        self.bbox = tuple(float(v) for v in bbox) if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None
        self.features = [f for f in doc.get('features', []) if f.get('geometry')]
        self.shapes = [shape(f['geometry']) for f in self.features]
        self._local = {}

    def _for_scene(self, scene):
        key = scene.get('id')
        if key not in self._local:
            polygons = []
            for feature, geom in zip(self.features, self.shapes, strict=True):
                parts = getattr(geom, 'geoms', [geom])
                local = unary_union([Polygon(lonlat_to_local(list(p.exterior.coords), scene),
                                             [lonlat_to_local(list(i.coords), scene) for i in p.interiors]) for p in parts]).buffer(0)
                polygons.append((feature, local))
            self._local[key] = (polygons, STRtree([p for _, p in polygons]) if polygons else None)
        return self._local[key]

    def covers(self, bounds, pad=.002):
        """Whether a WGS84 (west, south, east, north) box lies inside the fetched bbox (padded); no bbox means unknown -> True."""
        if self.bbox is None:
            return True
        west, south, east, north = bounds
        return west >= self.bbox[0]-pad and south >= self.bbox[1]-pad and east <= self.bbox[2]+pad and north <= self.bbox[3]+pad

    def near(self, local_shape, scene, within_m=20.):
        """Features intersecting or within `within_m` of a local-metre polygon, nearest first."""
        polygons, tree = self._for_scene(scene)
        if tree is None:
            return []
        out = []
        for index in tree.query(local_shape.buffer(within_m), predicate='intersects'):
            feature, polygon = polygons[int(index)]
            distance = float(local_shape.distance(polygon))
            if distance <= within_m:
                out.append({'osm_type': feature['osm_type'], 'osm_id': feature['osm_id'], 'tag_key': feature['tag_key'],
                            'tag_value': feature['tag_value'], 'name': feature.get('name'), 'distance_m': round(distance, 2)})
        return sorted(out, key=lambda m: (m['distance_m'], str(m['osm_id'])))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Cache OSM land-use context for building hints (context, not labels).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--bbox', nargs=4, type=float, metavar=('WEST', 'SOUTH', 'EAST', 'NORTH'))
    parser.add_argument('--offline', action='store_true', help='Never touch the network; report the cache or unavailable.')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args(argv)
    doc = load(Path(args.root), tuple(args.bbox) if args.bbox else None, fetch=None if args.offline else fetch, refresh=args.refresh)
    json.dump({k: v for k, v in doc.items() if k != 'features'} | {'features': len(doc['features'])}, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
