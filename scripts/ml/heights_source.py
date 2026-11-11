"""Real structure heights from public open data, with a stated source and uncertainty (heights-v1).

Every scene building carries a class-prior height (3.2 m, scene_extract.HEIGHTS). This module finds
measured or independently estimated heights for the Mooroolbark scan and writes them as eyes sidecar
rows (object_quality, version heights-v1); latest.json and every production artifact stay untouched.

Stages
  --fetch         probe and cache every public source for the suburb bbox under data/elevation/, with a
                  manifest (url, sha256, bytes, licence, capture date, http status) for every request,
                  failures included. --from-cache replays the manifest offline and never opens a socket.
  --assign        per building: LiDAR nDSM (DSM minus DEM; median of the top half of pixels inside the
                  outline eroded 1 m = roof height, lower quartile = eave estimate) when an nDSM tile exists;
                  otherwise the Depth Anything V2 relative relief (the depth model served by server.py)
                  converted to metres by a fit against absolute anchor heights, with the fit quality and a
                  per-object uncertainty; otherwise OSM tags; otherwise the class prior. OSM tags, Microsoft
                  ML footprint heights, ICESat-2 photon profiles, shadow lengths and the tile pipeline's ML
                  heights are recorded as cross-checks, never trusted alone.
  --summary       data/reviews/eyes/heights/summary.json (+ contact sheet): coverage per source with
                  denominators, height distributions per structure sub-kind, agreement with the address
                  roof ML heights and CRM heights.

Rows are estimates with provenance, not labels or survey heights.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import gzip
import hashlib
import io
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import numpy as np
from shapely import contains_xy
from shapely.geometry import Polygon, box, shape
from shapely.ops import unary_union

from . import eyes_index

VERSION = 'heights-v1'
CACHE_VERSION = 'elevation-cache-v1'
NOTE = 'height estimate with provenance, not a label or survey height'
TABLE = 'object_quality'
BBOX = (145.298, -37.803, 145.354, -37.758)          # west, south, east, north: Mooroolbark scan
CACHE_DIR = Path('data/elevation')
REPORT_DIR = Path('data/reviews/eyes/heights')
SUBURB_PREFIX = 'mooroolbark-50011498-17-'
USER_AGENT = 'addr-heights-source/1.0 (open-data research; no redistribution)'
SOURCES = ('lidar-ndsm', 'depth-model', 'osm', 'prior')
DEAD_STATUSES = ('rejected', 'superseded')

LICENCES = {
    'ga-elvis': 'CC BY 4.0 (Geoscience Australia ELVIS catalogue; per-dataset licences listed in the response)',
    'vic-open-data': 'CC BY 4.0 (DataVic / Vicmap open data platform)',
    'vicmap-dem-10m': 'CC BY 4.0 (Vicmap Elevation 10m DEM)',
    'srtm-1sec': 'CC BY 4.0 (Geoscience Australia SRTM-derived 1 second products)',
    'osm': 'ODbL 1.0 (OpenStreetMap contributors)',
    'ms-buildings': 'CDLA-Permissive-2.0 (Microsoft Global ML Building Footprints; checked 2026-09-28)',
    'icesat2': 'NASA Earth science data policy: no restrictions on use; cite Neumann et al., ATL03 (NSIDC)',
    'lidar-licensed': 'Licensed (Vicmap Elevation / Coordinated Imagery Program subscription); not open',
    'depth-model': 'Apache-2.0 (Depth Anything V2 Small weights)',
}
URLS = {
    'elvis_downloadables': 'https://api.elevation.fsdf.org.au/elevation/downloadables',
    'vic_wfs': 'https://opendata.maps.vic.gov.au/geoserver/wfs',
    'overpass': ('https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter',
                 'https://overpass.private.coffee/api/interpreter'),
    'ms_links': 'https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv',
    'osm_api_map': 'https://api.openstreetmap.org/api/0.6/map.json',
    'icesat2_tracks': 'https://openaltimetry.earthdatacloud.nasa.gov/data/api/icesat2/getTracks',
    'icesat2_atl03': 'https://openaltimetry.earthdatacloud.nasa.gov/data/api/icesat2/atl03',
}
ELVIS_HEADERS = {'Origin': 'https://elevation.fsdf.org.au', 'Referer': 'https://elevation.fsdf.org.au/',
                 'Accept': 'application/json, text/plain, */*'}
# Sources that exist but cannot be used under the open-data-only rule, or are useless for buildings.
NOT_FETCHED = [
    {'dataset': 'Vicmap Elevation 1m DEM / Greater Melbourne LiDAR 2017-18 (DEM, DSM, point cloud)',
     'url': 'https://www.land.vic.gov.au/maps-and-spatial/imagery/elevation-data/major-lidar-projects/greater-melbourne-lidar-2017-18',
     'licence': LICENCES['lidar-licensed'],
     'reason': 'Covers the whole bbox (open footprint layer: melbourne_2017nov28_dem1m, CIP 01-2017-18, availability "Licensed"); '
               'DEM, DSM and points are sold through the CIP Elevation subscription or Vicmap resellers. Not in ELVIS for this bbox.'},
    {'dataset': 'Digital Twin Victoria LiDAR 2021-24 (DEM 1 m, DHM 1 m, DSM 1 m)',
     'url': 'https://www.land.vic.gov.au/maps-and-spatial/imagery/elevation-data/major-lidar-projects/digital-twin-victoria-lidar-2021-24',
     'licence': LICENCES['lidar-licensed'], 'reason': 'Licensed through the CIP / Vicmap reseller network; not open.'},
    {'dataset': 'Vicmap 3D Buildings (LOD1/LOD2 heights)',
     'url': 'https://www.land.vic.gov.au/maps-and-spatial/spatial-data/vicmap-catalogue/vicmap-buildings',
     'licence': 'Licensed (DALA for the Victorian public sector; Vicmap Data Service Providers otherwise)',
     'reason': 'Viewable in the Digital Twin Victoria platform but not open for download; extracting it from the viewer would breach the licence.'},
    {'dataset': 'SRTM 1 second DEM/DSM (Geoscience Australia) and Copernicus GLO-30',
     'url': 'https://s3.ap-southeast-2.amazonaws.com/ga-elvis/elevation/1sec-srtm/srtm-1sec-dsm-v3-COG.tif',
     'licence': LICENCES['srtm-1sec'],
     'reason': '30 m cells are wider than a house lot; a 10 x 15 m house is under a quarter of one cell and the DSM is smoothed radar. '
               'Useless for building heights; not used.'},
    {'dataset': 'Vicmap Elevation 10m DEM',
     'url': 'https://vic-elvis.s3-ap-southeast-2.amazonaws.com/elevation/10m-dem/vmelev_dem10m.tif',
     'licence': LICENCES['vicmap-dem-10m'],
     'reason': 'Bare earth only (no surface model to subtract), 5 m vertical RMSE; cannot give building heights.'},
    {'dataset': 'NASA GEDI L2A / ICESat-2 via Earthdata Search',
     'url': 'https://search.earthdata.nasa.gov/',
     'licence': 'NASA open data (requires an Earthdata login)',
     'reason': 'Requires an account sign-up; ICESat-2 ATL03 is instead read through the login-free OpenAltimetry API.'},
]

PARAMS = {
    'erode_m': 1.0, 'erode_fallbacks_m': (0.5, 0.0), 'top_fraction': .5, 'eave_quantile': .25, 'min_pixels': 4,
    'lidar_vertical_rmse_m': .10,
    'prior_height_m': 3.2, 'prior_uncertainty_m': 1.5,
    'osm_levels_m': 3.0, 'osm_match_iou': .3, 'ms_match_iou': .3,
    'depth_input': 518, 'depth_ground_ring_m': (1.5, 6.0), 'depth_min_ground_m2': 2.0,
    'depth_primary_area_m2': 80., 'depth_adopt_gain': .10, 'depth_min_anchors': 30,
    'icesat_confidences': ('high', 'medium', 'low'), 'icesat_ground_ring_m': (2.0, 12.0), 'icesat_min_roof': 3,
    'icesat_min_ground': 5, 'icesat_threads': 4, 'icesat_scan_pad_deg': .1,
    'icesat_register_radius_m': 10., 'icesat_register_step_m': 1., 'icesat_register_min_buildings': 8, 'icesat_register_gain_m': .25,
}


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def sha256_bytes(body):
    return hashlib.sha256(body).hexdigest()


def _now():
    return datetime.now(UTC).isoformat()


def _num(value, digits=3):
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


# --- cache and manifest --------------------------------------------------------------------------------------

class CacheMiss(RuntimeError):
    """Offline mode asked for something the manifest does not hold."""


def canonical_url(url, params=None):
    if not params:
        return url
    items = []
    for key in sorted(params):
        value = params[key]
        for v in value if isinstance(value, (list, tuple)) else [value]:
            items.append((key, v))
    return f'{url}?{urlencode(items, quote_via=quote)}'


class Cache:
    """Content-addressed cache under data/elevation with a manifest entry per request (failures included).

    Offline (from_cache) mode never touches the network: a cached success is returned after its sha256 is
    checked, a recorded failure is replayed as None, and anything else raises CacheMiss.
    """

    def __init__(self, root=Path('.'), offline=False, session=None, log=None):
        self.root = Path(root)
        self.dir = self.root/CACHE_DIR
        self.offline = offline
        self.session = session
        self.log = log or (lambda *_: None)
        self.path = self.dir/'manifest.json'
        self.manifest = {'version': CACHE_VERSION, 'bbox': list(BBOX), 'entries': {}}
        if self.path.exists():
            self.manifest = json.loads(self.path.read_text())
            self.manifest.setdefault('entries', {})

    @staticmethod
    def key(url):
        return hashlib.sha256(url.encode()).hexdigest()[:24]

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.manifest, indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    def _session(self):
        if self.session is None:
            import requests
            self.session = requests.Session()
            self.session.headers['User-Agent'] = USER_AGENT
        return self.session

    def _read(self, entry):
        file = self.dir/entry['path']
        if not file.exists():
            return None
        body = file.read_bytes()
        return body if sha256_bytes(body) == entry['sha256'] else None

    def entry(self, url, params=None):
        return self.manifest['entries'].get(self.key(canonical_url(url, params)))

    def get(self, url, *, source, licence, params=None, headers=None, ext='json', capture_date=None, note=None,
            timeout=120, range_bytes=None, method='GET', data=None, refresh=False, save=True, attempts=3):
        """Bytes for the request (cached when possible) or None when the request failed (recorded)."""
        full = canonical_url(url, params)
        if data is not None:
            full = f'{full}#POST:{hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]}'
        key = self.key(full)
        entry = self.manifest['entries'].get(key)
        if entry and entry.get('status') == 'ok' and not refresh:
            body = self._read(entry)
            if body is not None:
                return body
            if self.offline:
                raise CacheMiss(f'cached file missing or sha256 mismatch for {full}')
        if self.offline:
            if entry and entry.get('status') != 'ok':
                return None
            raise CacheMiss(f'not in the elevation cache: {full}')
        request_headers = dict(headers or {})
        if range_bytes:
            request_headers['Range'] = f'bytes=0-{range_bytes-1}'
        started, status, body, error = time.monotonic(), None, None, None
        for attempt in range(attempts):
            try:
                if method == 'POST':
                    response = self._session().post(url, data=data, headers=request_headers, timeout=timeout)
                else:
                    response = self._session().get(full.split('#')[0], headers=request_headers, timeout=timeout)
                status = response.status_code
                if status in (200, 206):
                    body, error = response.content, None
                    break
                error = response.text[:300]
                if status not in (429, 500, 502, 503, 504):
                    break
            except Exception as exc:  # noqa: BLE001 - network errors are recorded, never raised
                error = f'{type(exc).__name__}: {exc}'[:300]
            time.sleep(2*(attempt+1))
        record = {'url': full, 'source': source, 'licence': licence, 'fetched_at': _now(), 'http_status': status,
                  'seconds': round(time.monotonic()-started, 2), 'capture_date': capture_date, 'note': note}
        if body is not None:
            relative = Path('raw')/source/f'{key}.{ext}'
            (self.dir/relative).parent.mkdir(parents=True, exist_ok=True)
            (self.dir/relative).write_bytes(body)
            record.update(status='ok', path=str(relative), sha256=sha256_bytes(body), bytes=len(body),
                          partial=bool(range_bytes))
        else:
            record.update(status='error', error=error)
        self.manifest['entries'][key] = record
        if save:
            self.save()
        self.log(f"  {record['status']:5} {status} {full[:140]}")
        return body

    def put_derived(self, name, body, *, source, licence, urls, note=None, capture_date=None, extra=None):
        relative = Path('derived')/name
        (self.dir/relative).parent.mkdir(parents=True, exist_ok=True)
        (self.dir/relative).write_bytes(body)
        record = {'url': urls if isinstance(urls, str) else list(urls), 'source': source, 'licence': licence,
                  'fetched_at': _now(), 'status': 'ok', 'path': str(relative), 'sha256': sha256_bytes(body),
                  'bytes': len(body), 'capture_date': capture_date, 'note': note, 'derived': True, **(extra or {})}
        self.manifest['entries'][f'derived:{name}'] = record
        self.save()
        return record

    def derived(self, name):
        entry = self.manifest['entries'].get(f'derived:{name}')
        if not entry:
            return None, None
        body = self._read(entry)
        if body is None and self.offline:
            raise CacheMiss(f'derived cache file missing or sha256 mismatch: {name}')
        return body, entry


# --- source probes ---------------------------------------------------------------------------------------------

def bbox_wkt(bbox=BBOX):
    w, s, e, n = bbox
    return f'POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))'


def fetch_elvis(cache):
    """ELVIS downloadables for the bbox; returns the dataset list and whether any LiDAR-grade product exists."""
    url = f"{URLS['elvis_downloadables']}?polygon={quote(bbox_wkt(), safe='(),')}"
    body = cache.get(url, source='ga-elvis', licence=LICENCES['ga-elvis'], headers=ELVIS_HEADERS,
                     note='ELVIS catalogue query for the suburb bbox (CloudFront needs the app Origin header)')
    result = {'url': url, 'ok': body is not None, 'datasets': [], 'lidar_grade': []}
    if body is None:
        return result
    payload = json.loads(body)
    for source in payload.get('available_data') or []:
        for category, groups in (source.get('downloadables') or {}).items():
            for resolution, files in groups.items():
                for item in files:
                    row = {'source': source.get('source'), 'category': category, 'resolution': resolution,
                           'name': item.get('file_name'), 'url': item.get('file_url'),
                           'last_modified': item.get('file_last_modified'), 'metadata': item.get('metadata_url')}
                    result['datasets'].append(row)
                    fine = re.match(r'^\s*(\d+(?:\.\d+)?)\s*(metre|m)\b', resolution, re.I)
                    if 'point cloud' in category.lower() or (fine and float(fine.group(1)) <= 2):
                        result['lidar_grade'].append(row)
    result['sources_without_data'] = [s.get('source') for s in payload.get('available_data') or [] if not s.get('downloadables')]
    return result


def probe_raster_downloads(cache, datasets):
    """One 1 KB ranged GET per ELVIS-listed open raster (they are served only through ELVIS e-mailed jobs)."""
    probes = []
    for row in datasets:
        url = row.get('url') or ''
        if not url.startswith('http') or not url.endswith('.tif'):
            continue
        body = cache.get(url, source='raster-probe', licence=LICENCES['srtm-1sec'] if 'srtm' in url else LICENCES['vicmap-dem-10m'],
                         ext='bin', range_bytes=1024, note='1 KB ranged probe; not a download')
        entry = cache.entry(url)
        probes.append({'name': row['name'], 'url': url, 'ok': body is not None,
                       'http_status': entry.get('http_status') if entry else None})
    return probes


def _wfs(cache, layer, note):
    params = {'service': 'WFS', 'version': '1.1.0', 'request': 'GetFeature', 'typeName': f'open-data-platform:{layer}',
              'outputFormat': 'application/json', 'srsName': 'EPSG:4326', 'bbox': ','.join(map(str, BBOX))+',EPSG:4326'}
    body = cache.get(URLS['vic_wfs'], params=params, source='vic-open-data', licence=LICENCES['vic-open-data'], note=note)
    return (json.loads(body) if body else None), canonical_url(URLS['vic_wfs'], params)


def fetch_vic_footprints(cache):
    """Which 1 m LiDAR DEM collections cover the bbox, with capture date and availability (open CC BY metadata)."""
    data, url = _wfs(cache, 'vicmap_elevation_1m_dem_footprints', 'Vicmap Elevation 1m DEM footprints (coverage metadata)')
    out = {'url': url, 'ok': data is not None, 'collections': []}
    area = box(*BBOX)
    for feature in (data or {}).get('features', []):
        geom = shape(feature['geometry'])
        props = feature['properties']
        out['collections'].append({'filename': props.get('filename'), 'capture_date': (props.get('source_date') or '')[:10],
                                   'resolution': props.get('source_resolution'), 'vertical_accuracy': props.get('vertical_accuracy'),
                                   'availability': props.get('availability'), 'cip_code': props.get('cip_code'),
                                   'bbox_cover_share': round(geom.intersection(area).area/area.area, 4)})
    return out


def fetch_vic_buildings(cache):
    out = {}
    for layer in ('building_polygon', 'building_point'):
        data, url = _wfs(cache, layer, f'Vicmap Features {layer} (checked for height attributes)')
        features = (data or {}).get('features', [])
        filled = sorted({k for f in features for k, v in f['properties'].items() if v not in (None, '')})
        heights = [k for k in filled if re.search(r'height|level|storey|elev', k, re.I)]
        out[layer] = {'url': url, 'ok': data is not None, 'features': len(features), 'filled_attributes': filled,
                      'height_attributes': heights}
    return out


def overpass_query(bbox=BBOX):
    w, s, e, n = bbox
    return (f'[out:json][timeout:180];(way["building"]({s},{w},{n},{e});relation["building"]({s},{w},{n},{e}););'
            'out tags geom;')


def _overpass_key(endpoint):
    return endpoint + '#POST:' + hashlib.sha256(json.dumps({'data': overpass_query()}, sort_keys=True).encode()).hexdigest()[:16]


def osm_api_quadrants(bbox=BBOX, n=2):
    w, s, e, north = bbox
    dx, dy = (e-w)/n, (north-s)/n
    return [(round(w+i*dx, 6), round(s+j*dy, 6), round(w+(i+1)*dx, 6), round(s+(j+1)*dy, 6)) for i in range(n) for j in range(n)]


def osm_api_to_elements(payloads):
    """OSM API 0.6 map.json payloads -> Overpass-like building ways with inline geometry (deduplicated)."""
    nodes, ways = {}, {}
    for payload in payloads:
        for element in payload.get('elements', []):
            if element['type'] == 'node':
                nodes[element['id']] = (element['lat'], element['lon'])
            elif element['type'] == 'way' and 'building' in (element.get('tags') or {}):
                ways[element['id']] = element
    out = []
    for way_id, way in sorted(ways.items()):
        points = [nodes.get(ref) for ref in way.get('nodes', [])]
        if any(p is None for p in points):
            continue   # way crosses a quadrant edge whose nodes were not returned; the neighbouring quadrant supplies it
        out.append({'type': 'way', 'id': way_id, 'tags': way.get('tags', {}), 'geometry': [{'lat': a, 'lon': b} for a, b in points]})
    return out


def fetch_osm(cache):
    query = overpass_query()
    for endpoint in URLS['overpass']:
        body = cache.get(endpoint, method='POST', data={'data': query}, source='osm', licence=LICENCES['osm'],
                         timeout=150, attempts=1, note='Overpass: buildings in the bbox with tags and geometry')
        if body is not None:
            try:
                payload = json.loads(body)
            except ValueError:
                continue
            return {'endpoint': endpoint, 'ok': True, 'query': query, 'timestamp': payload.get('osm3s', {}).get('timestamp_osm_base'),
                    'elements': len(payload.get('elements', []))}
    # Fallback: the OSM editing API map call, split until each request is below its 50,000-node limit.
    payloads, urls = osm_api_tiles(cache, BBOX)
    if payloads is None:
        return {'endpoint': None, 'ok': False, 'query': query, 'fallback': urls, 'error': 'OSM API map requests failed'}
    elements = osm_api_to_elements(payloads)
    return {'endpoint': URLS['osm_api_map'], 'ok': True, 'query': 'map.json tiles', 'fallback': urls,
            'timestamp': max((e.get('timestamp', '') for p in payloads for e in p.get('elements', []) if e.get('type') == 'way'), default=None),
            'elements': len(elements)}


def osm_api_tiles(cache, bbox, depth=0, max_depth=3):
    """(payloads, urls) for the bbox from OSM API map.json, splitting a tile in four on the too-many-nodes 400."""
    url = f"{URLS['osm_api_map']}?bbox={','.join(str(round(v, 6)) for v in bbox)}"
    body = cache.get(url, source='osm', licence=LICENCES['osm'], timeout=300, note='OSM API 0.6 map.json tile (Overpass mirrors timed out)')
    if body is not None:
        return [json.loads(body)], [url]
    entry = cache.entry(url)
    if not entry or entry.get('http_status') != 400 or depth >= max_depth:
        return None, [url]
    payloads, urls = [], [url]   # the refused parent request is part of the provenance
    for quad in osm_api_quadrants(bbox):
        part, part_urls = osm_api_tiles(cache, quad, depth+1, max_depth)
        urls += part_urls
        if part is None:
            return None, urls
        payloads += part
    return payloads, urls


def load_osm(cache):
    for endpoint in URLS['overpass']:
        entry = cache.entry(_overpass_key(endpoint))
        if entry and entry.get('status') == 'ok':
            body = cache._read(entry)
            if body is not None:
                payload = json.loads(body)
                payload['_provenance'] = {'url': endpoint, 'fetched_at': entry.get('fetched_at'), 'sha256': entry.get('sha256'),
                                          'osm_base': payload.get('osm3s', {}).get('timestamp_osm_base')}
                return payload
    try:
        payloads, urls = osm_api_tiles(cache, BBOX)
    except CacheMiss:
        return None
    return {'elements': osm_api_to_elements(payloads), '_provenance': {'url': urls}} if payloads else None


def quadkey(lon, lat, z):
    x = (lon+180)/360
    s = math.sin(math.radians(lat))
    y = .5-math.log((1+s)/(1-s))/(4*math.pi)
    n = 2**z
    tx, ty = int(x*n), int(y*n)
    digits = []
    for i in range(z, 0, -1):
        mask = 1 << (i-1)
        digits.append(str((1 if tx & mask else 0) + (2 if ty & mask else 0)))
    return ''.join(digits)


def _feature_in_bbox(feature, bbox, pad=.002):
    w, s, e, n = bbox
    coords = feature.get('geometry', {}).get('coordinates') or []
    ring = coords[0] if coords and coords[0] and isinstance(coords[0][0], list) else []
    return any(w-pad <= x <= e+pad and s-pad <= y <= n+pad for x, y in ring[:1] + ring[len(ring)//2:len(ring)//2+1])


MS_INNER_MELB_NAME = 'ms-buildings/bbox-inner-melb.geojsonl.gz'
JUDGE_DATASETS = ('data/reviews/eyes/training/dataset-v1', 'data/reviews/eyes/training/dataset-v2')


def pad_bbox_m(bbox, pad_m):
    w, s, e, n = bbox
    lat = (s+n)/2
    dlat = pad_m/111320.
    dlon = pad_m/(111320.*max(math.cos(math.radians(lat)), 1e-6))
    return (w-dlon, s-dlat, e+dlon, n+dlat)


def judge_tiles_bbox(root=Path('.'), datasets=JUDGE_DATASETS, exclude=BBOX, pad_m=200., splits=('test', 'val')):
    """Lon/lat bbox (padded pad_m) around the judged scenes whose region.bounds fall outside `exclude` (the Mooroolbark
    scan): the inner-Melbourne map-* tiles of the dataset test/val splits. Returns (bbox or None, scene ids used)."""
    root = Path(root)
    ids = []
    for dataset in datasets:
        for split in splits:
            path = root/dataset/f'{split}.json'
            if not path.exists():
                continue
            for image in json.loads(path.read_text())['images']:
                if image.get('scene_id') and image['scene_id'] not in ids:
                    ids.append(image['scene_id'])
    outside = {}
    for sid in ids:
        path = root/'data/scenes'/sid/'latest.json'
        if not path.exists():
            continue
        bounds = (json.loads(path.read_text()).get('region') or {}).get('bounds')
        if not bounds or len(bounds) != 4:
            continue
        w, s, e, n = bounds
        inside = exclude[0]-.002 <= w and e <= exclude[2]+.002 and exclude[1]-.002 <= s and n <= exclude[3]+.002
        if not inside:
            outside[sid] = tuple(float(v) for v in bounds)
    if not outside:
        return None, []
    union = (min(b[0] for b in outside.values()), min(b[1] for b in outside.values()),
             max(b[2] for b in outside.values()), max(b[3] for b in outside.values()))
    return tuple(round(v, 6) for v in pad_bbox_m(union, pad_m)), sorted(outside)


def fetch_ms_buildings(cache, refresh=False, bbox=BBOX, name='ms-buildings/bbox.geojsonl.gz'):
    """Microsoft Global ML Building Footprints for the bbox quadkeys (height estimates), filtered to the bbox.

    The default name is the Mooroolbark cache and is never overwritten by another bbox: callers widening the footprint
    check pass their own name (MS_INNER_MELB_NAME for the inner-Melbourne judge tiles)."""
    if bbox != BBOX and name == 'ms-buildings/bbox.geojsonl.gz':
        raise ValueError('a bbox other than the Mooroolbark scan needs its own cache name; the default file is never overwritten')
    body, entry = cache.derived(name)
    if body is not None and not refresh:
        return {'ok': True, 'cached': True, **{k: entry.get(k) for k in ('url', 'features', 'with_height', 'release', 'source_sha256')}}
    if cache.offline:
        return {'ok': False, 'cached': False, 'error': 'not cached'}
    links = cache.get(URLS['ms_links'], source='ms-buildings', licence=LICENCES['ms-buildings'], ext='csv', timeout=(20, 60),
                      note='Microsoft Global ML Building Footprints dataset index')
    if links is None:
        return {'ok': False, 'error': 'dataset-links.csv unavailable'}
    import csv
    wanted = {quadkey(x, y, 9) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])}
    rows = [r for r in csv.DictReader(io.StringIO(links.decode())) if r['Location'] == 'Australia' and r['QuadKey'] in wanted]
    kept, digests, urls = [], {}, []
    for row in rows:
        urls.append(row['Url'])
        file, digests[row['Url']] = download_parallel(cache, row['Url'])
        try:
            with gzip.open(file, 'rb') as stream:
                for line in stream:
                    if line.strip():
                        feature = json.loads(line)
                        if _feature_in_bbox(feature, bbox):
                            kept.append(line.rstrip(b'\n'))
        finally:
            file.unlink(missing_ok=True)   # only the bbox subset is kept; the source sha256 is recorded
    payload = gzip.compress(b'\n'.join(kept)+b'\n', mtime=0)
    heights = sum(1 for line in kept if json.loads(line)['properties'].get('height', -1) > 0)
    release = re.search(r'global-buildings/(\d{4}-\d{2}-\d{2})/', rows[0]['Url']).group(1) if rows else None
    cache.put_derived(name, payload, source='ms-buildings', licence=LICENCES['ms-buildings'], urls=urls,
                      note='features of the listed quadkey files with a vertex inside the bbox (+0.002 deg); heights are ML estimates',
                      extra={'features': len(kept), 'with_height': heights, 'release': release, 'source_sha256': digests, 'bbox': list(bbox)})
    return {'ok': True, 'cached': False, 'url': urls, 'features': len(kept), 'with_height': heights, 'release': release, 'bbox': list(bbox),
            'sha256': sha256_bytes(payload), 'path': str(cache.dir/'derived'/name)}


def fetch_ms_judge_footprints(root=Path('.'), refresh=False, datasets=JUDGE_DATASETS, log=print):
    """Second footprint cache for the inner-Melbourne judge tiles (CDLA-Permissive download, minutes); the Mooroolbark
    cache is untouched. Returns the fetch report with the bbox and the scenes that defined it."""
    bbox, scenes = judge_tiles_bbox(root, datasets)
    if bbox is None:
        return {'ok': False, 'error': 'no judged scene lies outside the Mooroolbark bbox', 'scenes': []}
    cache = Cache(root, offline=False, log=log)
    report = fetch_ms_buildings(cache, refresh=refresh, bbox=bbox, name=MS_INNER_MELB_NAME)
    entry = cache.manifest['entries'].get(f'derived:{MS_INNER_MELB_NAME}') or {}
    return {**report, 'bbox': list(bbox), 'scenes': scenes, 'name': MS_INNER_MELB_NAME, 'sha256': report.get('sha256') or entry.get('sha256'),
            'path': str(cache.dir/'derived'/MS_INNER_MELB_NAME), 'features': report.get('features') or entry.get('features'),
            'licence': LICENCES['ms-buildings'], 'note': 'footprints are ML outputs; backing is agreement with another model'}


def download_parallel(cache, url, parts=12, chunk=1 << 20):
    """Ranged parallel download to a temporary file (the host throttles single connections); returns (path, sha256)."""
    session = cache._session()
    head = session.head(url, timeout=(20, 60), allow_redirects=True)
    head.raise_for_status()
    size = int(head.headers['Content-Length'])
    tmp = cache.dir/'tmp'/(Cache.key(url)+'.part')
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, 'wb') as out:
        out.truncate(size)
    bounds = [(i*size//parts, (i+1)*size//parts-1) for i in range(parts)]

    def fetch_range(span):
        start, end = span
        for attempt in range(4):
            try:
                response = session.get(url, headers={'Range': f'bytes={start}-{end}'}, timeout=(20, 120))
                if response.status_code == 206 and len(response.content) == end-start+1:
                    with open(tmp, 'r+b') as out:
                        out.seek(start)
                        out.write(response.content)
                    return True
            except Exception:  # noqa: BLE001 - retried below
                pass
            time.sleep(2*(attempt+1))
        return False

    with futures.ThreadPoolExecutor(parts) as pool:
        if not all(pool.map(fetch_range, bounds)):
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f'ranged download failed for {url}')
    digest = hashlib.sha256()
    with open(tmp, 'rb') as stream:
        for block in iter(lambda: stream.read(chunk), b''):
            digest.update(block)
    return tmp, digest.hexdigest()


def load_ms_buildings(cache):
    body, _ = cache.derived('ms-buildings/bbox.geojsonl.gz')
    if body is None:
        return []
    return [json.loads(line) for line in gzip.decompress(body).splitlines() if line.strip()]


# --- ICESat-2 ATL03 photon profiles (OpenAltimetry, no login) -----------------------------------------------

def icesat_scan(cache, start=date(2018, 10, 13), end=None, threads=None, log=None):
    """Reference ground tracks passing the bbox per day (OpenAltimetry getTracks), cached as one derived file."""
    log = log or (lambda *_: None)
    name = 'icesat2/track-scan.json'
    body, _ = cache.derived(name)
    scan = json.loads(body) if body else {'days': {}}
    if cache.offline:
        return scan
    end = end or (datetime.now(UTC).date()-timedelta(days=30))
    days = [(start+timedelta(i)).isoformat() for i in range((end-start).days+1)]
    todo = [d for d in days if d not in scan['days'] or isinstance(scan['days'][d], str)]
    session = cache._session()
    pad = PARAMS['icesat_scan_pad_deg']  # beams sit up to ~3.3 km either side of the nominal track, and were off-pointed
    bbox = dict(zip(('minx', 'miny', 'maxx', 'maxy'), (BBOX[0]-pad, BBOX[1]-pad, BBOX[2]+pad, BBOX[3]+pad), strict=True))

    def one(day):
        for attempt in range(3):
            try:
                response = session.get(URLS['icesat2_tracks'], params={'date': day, 'outputFormat': 'json', **bbox}, timeout=60)
                if response.status_code == 200:
                    return day, response.json()['output']['track']
            except Exception:  # noqa: BLE001 - retried, then recorded as an error string
                pass
            time.sleep(1+attempt)
        return day, 'error'

    def checkpoint():
        scan.update(start=start.isoformat(), end=end.isoformat(), bbox=list(BBOX), padded_bbox=list(bbox.values()))
        cache.put_derived(name, json.dumps(scan, sort_keys=True).encode(), source='icesat2', licence=LICENCES['icesat2'],
                          urls=canonical_url(URLS['icesat2_tracks'], {'date': '{YYYY-MM-DD}', 'outputFormat': 'json', **bbox}),
                          note='one getTracks query per day over the bbox padded by icesat_scan_pad_deg; tracks = nominal '
                               'reference ground tracks, so beams may still miss the strict bbox')

    done = 0
    with futures.ThreadPoolExecutor(threads or PARAMS['icesat_threads']) as pool:
        for day, tracks in pool.map(one, todo):
            scan['days'][day] = tracks
            done += 1
            if done % 250 == 0:
                log(f'  icesat2 scan {done}/{len(todo)}')
                checkpoint()
    checkpoint()
    return scan


def atl03_params(day, track):
    return {'date': day, 'trackId': track, 'outputFormat': 'json', 'minx': BBOX[0], 'miny': BBOX[1], 'maxx': BBOX[2], 'maxy': BBOX[3],
            'beamNames': ['gt1l', 'gt1r', 'gt2l', 'gt2r', 'gt3l', 'gt3r'], 'photonConfidence': list(PARAMS['icesat_confidences'])}


def fetch_icesat(cache, scan, log=None, threads=None):
    """ATL03 photons in the strict bbox for every scanned pass (an empty list when no beam crossed it)."""
    passes = sorted({(d, int(t)) for d, tracks in scan.get('days', {}).items() if isinstance(tracks, list) for t in tracks})

    def one(item):
        day, track = item
        body = cache.get(URLS['icesat2_atl03'], params=atl03_params(day, track), source='icesat2', licence=LICENCES['icesat2'],
                         capture_date=day, timeout=300, save=False, note=f'ATL03 photons in the bbox, RGT {track}')
        photons = parse_atl03(body) if body else {}
        return {'date': day, 'track': track, 'ok': body is not None,
                'beams_in_bbox': sorted(photons), 'photons': int(sum(len(v) for v in photons.values()))}

    if cache.offline:
        out = [one(item) for item in passes]
    else:
        with futures.ThreadPoolExecutor(threads or PARAMS['icesat_threads']) as pool:
            out = list(pool.map(one, passes))
        cache.save()
    return out


def parse_atl03(body):
    """{beam: array[n, 4] of lat, lon, h_ph (m, WGS84 ellipsoid), confidence rank (3 high .. 1 low)}."""
    ranks = {'high': 3, 'medium': 2, 'low': 1}
    beams = {}
    for beam in json.loads(body) if body else []:
        rows = []
        for series in beam.get('series') or []:
            rank = ranks.get(str(series.get('name', '')).lower())
            if rank is None or not series.get('data'):
                continue
            data = np.asarray(series['data'], dtype=float)
            rows.append(np.c_[data[:, :3], np.full(len(data), rank)])
        if rows:
            beams[beam.get('beam_name')] = np.concatenate(rows)
    return beams


def _step(report, name, function, *args, **kwargs):
    """Run one probe; offline cache misses and network failures are recorded, never raised."""
    try:
        report[name] = function(*args, **kwargs)
    except CacheMiss as exc:
        report[name] = {'ok': False, 'error': f'not in cache: {exc}'[:300]}
    except Exception as exc:  # noqa: BLE001 - every failure is part of the provenance report
        report[name] = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:300]}
    return report[name]


def fetch(root=Path('.'), offline=False, icesat=True, ms=True, session=None, log=print):
    """Probe every source, cache responses, and return a provenance report (written to the cache when online)."""
    cache = Cache(root, offline=offline, session=session, log=log)
    report = {'version': VERSION, 'computed_at': _now(), 'offline': offline, 'bbox': list(BBOX)}
    log('ELVIS downloadables ...')
    elvis = _step(report, 'elvis', fetch_elvis, cache)
    _step(report, 'raster_probes', probe_raster_downloads, cache, elvis.get('datasets', []))
    log('Vicmap 1m DEM footprints ...')
    _step(report, 'vic_lidar_footprints', fetch_vic_footprints, cache)
    log('Vicmap Features buildings ...')
    _step(report, 'vic_buildings', fetch_vic_buildings, cache)
    log('OSM Overpass ...')
    _step(report, 'osm', fetch_osm, cache)
    if ms:
        log('Microsoft ML footprints ...')
        _step(report, 'ms_buildings', fetch_ms_buildings, cache)
    if icesat:
        log('ICESat-2 pass scan ...')
        scan = _step(report, 'icesat2_scan_raw', icesat_scan, cache, log=log)
        days = scan.get('days', {}) if isinstance(scan, dict) else {}
        report.pop('icesat2_scan_raw', None)
        report['icesat2_scan'] = {'days_scanned': len(days), 'errors': sum(1 for v in days.values() if isinstance(v, str)),
                                  'days_with_track': sum(1 for v in days.values() if isinstance(v, list) and v),
                                  'padded_bbox_deg': PARAMS['icesat_scan_pad_deg']}
        _step(report, 'icesat2_passes', fetch_icesat, cache, scan if isinstance(scan, dict) else {}, log=log)
    report['not_fetched'] = NOT_FETCHED
    report['lidar_ndsm_available'] = bool(lidar_tiles(cache))
    if not offline:
        cache.put_derived('fetch-report.json', json.dumps(report, indent=1, sort_keys=True, default=str).encode(),
                          source='heights-source', licence='derived report', urls='(this module)', note='last online fetch report')
    return report


def lidar_tiles(cache):
    """Manifest entries registered as LiDAR DSM/DEM GeoTIFF pairs (none are openly available for this bbox)."""
    return [e for e in cache.manifest.get('entries', {}).values() if e.get('role') in ('dsm', 'dem') and e.get('status') == 'ok']


# --- raster sampling (LiDAR nDSM and depth maps share it) -------------------------------------------------------

def ndsm(dsm, dem, nodata=None):
    """Normalised DSM (surface minus terrain); nodata or non-finite cells become NaN."""
    dsm = np.asarray(dsm, dtype=float)
    dem = np.asarray(dem, dtype=float)
    out = dsm-dem
    bad = ~np.isfinite(out)
    if nodata is not None:
        bad |= (dsm == nodata) | (dem == nodata)
    out[bad] = np.nan
    return out


def gdal_to_matrix(transform):
    """GDAL geotransform (c, a, b, f, d, e) or affine (a, b, c, d, e, f) tuple -> 3x2 matrix M with [col, row, 1] @ M = (x, y)."""
    if hasattr(transform, 'a'):
        a, b, c, d, e, f = transform.a, transform.b, transform.c, transform.d, transform.e, transform.f
    elif len(transform) == 6 and isinstance(transform, dict):
        raise TypeError('transform dict not supported')
    else:
        a, b, c, d, e, f = transform
    return np.array([[a, d], [b, e], [c, f]], dtype=float)


def pixel_centres_in(polygon, matrix, shape_rc):
    """(rows, cols) of raster cells whose centres lie inside `polygon` (world coordinates of `matrix`)."""
    if polygon is None or polygon.is_empty:
        return np.empty(0, dtype=int), np.empty(0, dtype=int)
    matrix = np.asarray(matrix, dtype=float)
    inverse = np.linalg.inv(np.c_[matrix, [0, 0, 1]])
    w, s, e, n = polygon.bounds
    corners = np.array([[w, s, 1], [w, n, 1], [e, s, 1], [e, n, 1]]) @ inverse
    c0, r0 = np.floor(corners[:, :2].min(axis=0)-1).astype(int)
    c1, r1 = np.ceil(corners[:, :2].max(axis=0)+1).astype(int)
    c0, r0 = max(c0, 0), max(r0, 0)
    c1, r1 = min(c1, shape_rc[1]-1), min(r1, shape_rc[0]-1)
    if c1 < c0 or r1 < r0:
        return np.empty(0, dtype=int), np.empty(0, dtype=int)
    cols, rows = np.meshgrid(np.arange(c0, c1+1), np.arange(r0, r1+1))
    cols, rows = cols.ravel(), rows.ravel()
    xy = np.c_[cols+.5, rows+.5, np.ones(len(cols))] @ matrix
    inside = contains_xy(polygon, xy[:, 0], xy[:, 1])
    return rows[inside], cols[inside]


def top_half_median(values, top_fraction=None):
    """Median of the highest `top_fraction` of values (roof height robust to eave pixels and ground bleed)."""
    values = np.sort(np.asarray(values, dtype=float))
    if values.size == 0:
        return None
    top_fraction = PARAMS['top_fraction'] if top_fraction is None else top_fraction
    start = int(math.floor(values.size*(1-top_fraction)))
    return float(np.median(values[min(start, values.size-1):]))


def sample_polygon(values, matrix, polygon, *, erode_m=None, fallbacks=None, min_pixels=None, eave_quantile=None):
    """Roof statistics of a raster inside an outline eroded by erode_m (falling back to smaller erosions for small roofs).

    Returns {height, eave, samples, erode_m, iqr} in raster units, or None when too few valid cells remain.
    height = median of the top half of the cells, eave = lower quartile (a lower-bound eave estimate).
    """
    erode_m = PARAMS['erode_m'] if erode_m is None else erode_m
    fallbacks = PARAMS['erode_fallbacks_m'] if fallbacks is None else fallbacks
    min_pixels = PARAMS['min_pixels'] if min_pixels is None else min_pixels
    eave_quantile = PARAMS['eave_quantile'] if eave_quantile is None else eave_quantile
    values = np.asarray(values, dtype=float)
    for erosion in (erode_m, *fallbacks):
        inner = polygon.buffer(-erosion) if erosion > 0 else polygon
        rows, cols = pixel_centres_in(inner, matrix, values.shape)
        sample = values[rows, cols]
        sample = sample[np.isfinite(sample)]
        if sample.size >= min_pixels:
            q1, q3 = np.quantile(sample, [.25, .75])
            return {'height': top_half_median(sample), 'eave': float(np.quantile(sample, eave_quantile)), 'samples': int(sample.size),
                    'erode_m': float(erosion), 'iqr': float(q3-q1)}
    return None


def ndsm_height(ndsm_values, matrix, polygon, vertical_rmse=None):
    """Roof and eave heights above ground (m) from an nDSM, with an uncertainty from LiDAR RMSE and roof spread."""
    stats = sample_polygon(ndsm_values, matrix, polygon)
    if stats is None:
        return None
    vertical_rmse = PARAMS['lidar_vertical_rmse_m'] if vertical_rmse is None else vertical_rmse
    top = max(1, stats['samples']//2)
    spread = stats['iqr']/1.349/math.sqrt(top)
    return {'height_m': _num(stats['height'], 2), 'eave_m': _num(stats['eave'], 2), 'samples': stats['samples'],
            'erode_m': stats['erode_m'], 'uncertainty_m': _num(math.sqrt(vertical_rmse**2+spread**2), 2), 'iqr_m': _num(stats['iqr'], 2)}


def load_lidar_pairs(cache):
    """(dsm_path, dem_path, meta) pairs from manifest entries with role dsm/dem sharing a 'pair' id (rasterio needed)."""
    by_pair = {}
    for entry in lidar_tiles(cache):
        by_pair.setdefault(entry.get('pair'), {})[entry['role']] = entry
    return [(cache.dir/p['dsm']['path'], cache.dir/p['dem']['path'], p['dsm']) for p in by_pair.values() if 'dsm' in p and 'dem' in p]


def lidar_for_scene(pairs, scene, buildings):
    """nDSM heights for buildings of a scene from registered DSM/DEM GeoTIFF pairs; {} when none are registered."""
    if not pairs:
        return {}
    try:
        import rasterio
        from rasterio.warp import transform as warp_transform
    except ImportError as exc:  # pragma: no cover - only reached once a LiDAR tile has been registered
        raise RuntimeError('LiDAR tiles are registered but rasterio is not installed: uv pip install rasterio into scripts/ml/.venv '
                           'and pin it in scripts/ml/requirements.txt') from exc
    out = {}
    for dsm_path, dem_path, meta in pairs:
        with rasterio.open(dsm_path) as dsm_file, rasterio.open(dem_path) as dem_file:
            values = ndsm(dsm_file.read(1), dem_file.read(1), dsm_file.nodata)
            matrix = gdal_to_matrix(dsm_file.transform)
            for oid, item in buildings.items():
                if oid in out:
                    continue
                lon, lat = np.asarray(item['lonlat'].exterior.coords).T
                xs, ys = warp_transform('EPSG:4326', dsm_file.crs, lon.tolist(), lat.tolist())
                result = ndsm_height(values, matrix, Polygon(zip(xs, ys, strict=True)), meta.get('vertical_rmse_m'))
                if result:
                    out[oid] = {**result, 'capture_date': meta.get('capture_date'), 'dataset': meta.get('dataset'),
                                'licence': meta.get('licence'), 'url': meta.get('url')}
    return out


# --- scenes --------------------------------------------------------------------------------------------------

def suburb_scenes(root=Path('.')):
    return sorted(p.name for p in (Path(root)/'data/scenes').iterdir() if p.name.startswith(SUBURB_PREFIX))


def validate_scene_id(scene_id):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', scene_id or ''):
        raise ValueError(f'invalid scene id {scene_id!r}')
    return scene_id


def load_scene(root, scene_id):
    return json.loads((Path(root)/'data/scenes'/validate_scene_id(scene_id)/'latest.json').read_text())


def load_scene_image(root, scene):
    """Scene PNG (sha256-verified) as a PIL RGB image, or None."""
    from PIL import Image
    digest = str(scene.get('image_sha256') or '')
    file = Path(root)/'data/scenes'/validate_scene_id(scene['id'])/f'{digest}.png'
    if len(digest) != 64 or not file.exists():
        return None
    body = file.read_bytes()
    if sha256_bytes(body) != digest:
        return None
    image = Image.open(io.BytesIO(body)).convert('RGB')
    return image if image.size == (scene['image_width'], scene['image_height']) else None


def world_xy(lon, lat):
    lon, lat = np.asarray(lon, dtype=float), np.clip(np.asarray(lat, dtype=float), -85.05112878, 85.05112878)
    return (lon+180.)/360., (1.-np.arcsinh(np.tan(np.radians(lat)))/math.pi)/2.


def lonlat_to_local(lon, lat, scene):
    """Vectorised scene_confidence/reference_agreement projection: lon/lat -> scene-local metres (x east, y north)."""
    from .tiles import WORLD_SPAN_M
    centre = scene['centroid']
    x0, y0 = world_xy(centre['lon'], centre['lat'])
    x, y = world_xy(lon, lat)
    scale = WORLD_SPAN_M*math.cos(math.radians(centre['lat']))
    return (x-x0)*scale, -(y-y0)*scale


def local_to_lonlat(x, y, scene):
    from .tiles import WORLD_SPAN_M
    centre = scene['centroid']
    x0, y0 = world_xy(centre['lon'], centre['lat'])
    scale = WORLD_SPAN_M*math.cos(math.radians(centre['lat']))
    wx, wy = x0+np.asarray(x)/scale, y0-np.asarray(y)/scale
    return wx*360.-180., np.degrees(np.arctan(np.sinh(math.pi*(1.-2.*wy))))


def _polygon(points, holes=()):
    shape_ = Polygon(points, holes)
    if not shape_.is_valid:
        shape_ = shape_.buffer(0)
        if shape_.geom_type == 'MultiPolygon':
            shape_ = max(shape_.geoms, key=lambda g: g.area)
    return shape_


def scene_buildings(scene, only_owned=True):
    """{object_id: {obj, local (m), lonlat, owned}} for live buildings; trees as a local union for ground masks."""
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    listed = scene.get('owned_object_ids')      # an explicit empty list owns nothing; a missing list owns everything
    owned = set(listed) if listed is not None else {o['id'] for o in scene['objects']}

    def local(points):
        return np.c_[np.asarray(points, dtype=float), np.ones(len(points))] @ frame

    buildings, trees, everything = {}, [], []
    for obj in scene['objects']:
        if obj.get('status') in DEAD_STATUSES or not obj.get('ring') or len(obj['ring']) < 3:
            continue
        try:
            poly = _polygon(local(obj['ring']), [local(h) for h in obj.get('holes') or [] if len(h) >= 3])
        except (ValueError, TypeError):
            continue
        if poly.is_empty:
            continue
        if obj['kind'] == 'tree':
            trees.append(poly)
        if obj['kind'] != 'building':
            continue
        everything.append(poly)
        if only_owned and obj['id'] not in owned:
            continue
        xy = np.asarray(poly.exterior.coords)
        lon, lat = local_to_lonlat(xy[:, 0], xy[:, 1], scene)
        buildings[obj['id']] = {'obj': obj, 'local': poly, 'lonlat': Polygon(np.c_[lon, lat]), 'owned': obj['id'] in owned}
    return buildings, (unary_union(everything) if everything else None), (unary_union(trees) if trees else None)


# --- depth model (the Depth Anything V2 path served by server.py /depth) ----------------------------------------

class DepthRunner:
    """Relative depth per scene image with the server's model file, cached under data/elevation/depth."""

    def __init__(self, root=Path('.'), input_size=None, model_path=None):
        from .pipeline import DEFAULT_DEPTH_MODEL
        self.root = Path(root)
        self.model_path = Path(model_path or DEFAULT_DEPTH_MODEL)
        self.input_size = input_size or PARAMS['depth_input']
        self._model_sha = None
        self._estimator = None

    @property
    def providers(self):
        return self._estimator.session.get_providers() if self._estimator is not None else None

    @property
    def model_sha(self):
        if self._model_sha is None:
            self._model_sha = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        return self._model_sha

    def estimator(self):
        if self._estimator is None:
            from .depth import OnnxDepthEstimator
            self._estimator = OnnxDepthEstimator(self.model_path, input_size=self.input_size)
        return self._estimator

    def path(self, scene):
        return (self.root/CACHE_DIR/'depth'/validate_scene_id(scene['id'])/
                f"{scene['image_sha256'][:16]}-{self.model_sha[:12]}-{self.input_size}.npz")

    def depth(self, scene):
        """(depth array in 0..1 at input_size, matrix depth-pixel -> scene metres) or (None, reason)."""
        file = self.path(scene)
        if file.exists():
            values = np.load(file)['depth'].astype(float)
        else:
            from PIL import Image
            image = load_scene_image(self.root, scene)
            if image is None:
                return None, 'scene image missing or sha256 mismatch'
            small = image.resize((self.input_size, self.input_size), Image.Resampling.BILINEAR)
            values = self.estimator().estimate(small).astype(float)
            file.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(file, depth=values.astype(np.float16))
        sx, sy = scene['image_width']/values.shape[1], scene['image_height']/values.shape[0]
        frame = np.asarray(scene['pixel_to_meters'], dtype=float)
        matrix = np.array([[sx, 0], [0, sy], [0, 0]]) @ frame[:2]
        matrix[2] += frame[2]
        return values, matrix


def depth_relief(values, matrix, polygon, occupied, trees):
    """Roof-minus-ground relief (depth units) for one outline: roof = top-half median inside the outline eroded 1 m,
    ground = median of a 1.5-6 m ring outside it that excludes every building (+0.5 m) and tree canopy."""
    roof = sample_polygon(values, matrix, polygon)
    if roof is None:
        return None
    inner, outer = PARAMS['depth_ground_ring_m']
    ring = polygon.buffer(outer).difference(polygon.buffer(inner))
    if occupied is not None:
        ring = ring.difference(occupied.buffer(.5))
    if trees is not None:
        ring = ring.difference(trees)
    rows, cols = pixel_centres_in(ring, matrix, values.shape)
    ground = values[rows, cols]
    ground = ground[np.isfinite(ground)]
    pixel_area = abs(np.linalg.det(np.asarray(matrix)[:2]))
    if ground.size < max(3, int(PARAMS['depth_min_ground_m2']/max(pixel_area, 1e-6))):
        return None
    level = float(np.median(ground))
    return {'relief': roof['height']-level, 'relief_eave': roof['eave']-level, 'samples': roof['samples'],
            'ground_samples': int(ground.size), 'erode_m': roof['erode_m']}


def server_depth(url, z, x, y, timeout=120):
    """GET {url}/depth/{z}/{x}/{y}.json from the running ML server (server.GenerationCoordinator.get_depth)."""
    import requests
    response = requests.get(f'{url.rstrip("/")}/depth/{z}/{x}/{y}.json', timeout=timeout)
    response.raise_for_status()
    return response.json()


# --- cross-check sources --------------------------------------------------------------------------------------

def parse_height_tag(value):
    """Metres from an OSM height tag ('7', '7 m', '7.5m'); None for feet/unparseable."""
    if value is None:
        return None
    match = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(m|metres?|meters?)?\s*', str(value))
    if not match:
        return None
    number = float(match.group(1).replace(',', '.'))
    return number if 0 < number < 400 else None


def osm_buildings(payload):
    """[{id, tags, rings: [[lon, lat], ...], height_m, method}] for OSM buildings with geometry."""
    out = []
    for element in (payload or {}).get('elements', []):
        tags = element.get('tags') or {}
        rings = []
        if element.get('type') == 'way' and element.get('geometry'):
            rings.append([(p['lon'], p['lat']) for p in element['geometry']])
        elif element.get('type') == 'relation':
            for member in element.get('members') or []:
                if member.get('role') == 'outer' and member.get('geometry'):
                    rings.append([(p['lon'], p['lat']) for p in member['geometry']])
        rings = [r for r in rings if len(r) >= 4]
        if not rings:
            continue
        height, method = parse_height_tag(tags.get('height') or tags.get('building:height')), None
        if height is not None:
            method = 'height tag'
        else:
            try:
                levels = float(tags.get('building:levels'))
            except (TypeError, ValueError):
                levels = None
            if levels and 0 < levels < 100:
                height, method = levels*PARAMS['osm_levels_m'], f"building:levels x {PARAMS['osm_levels_m']} m"
        out.append({'id': f"{element['type']}/{element['id']}", 'tags': {k: tags[k] for k in tags if k in (
            'building', 'height', 'building:levels', 'roof:shape', 'roof:levels', 'name', 'min_height')},
            'rings': rings, 'height_m': height, 'method': method})
    return out


def ms_features(features):
    out = []
    for index, feature in enumerate(features):
        coords = (feature.get('geometry') or {}).get('coordinates') or []
        if not coords:
            continue
        ring = coords[0] if feature['geometry']['type'] == 'Polygon' else coords[0][0]
        height = (feature.get('properties') or {}).get('height')
        out.append({'id': f'ms/{index}', 'rings': [ring], 'height_m': float(height) if height is not None and height > 0 else None,
                    'confidence': (feature.get('properties') or {}).get('confidence')})
    return out


class LonLatIndex:
    """Candidate lookup for external footprints by lon/lat bounds."""

    def __init__(self, items):
        self.items = items
        bounds = np.array([[min(p[0] for p in r), min(p[1] for p in r), max(p[0] for p in r), max(p[1] for p in r)]
                           for it in items for r in it['rings'][:1]]) if items else np.zeros((0, 4))
        self.bounds = bounds

    def query(self, w, s, e, n):
        if not len(self.bounds):
            return []
        hit = (self.bounds[:, 0] <= e) & (self.bounds[:, 2] >= w) & (self.bounds[:, 1] <= n) & (self.bounds[:, 3] >= s)
        return [self.items[i] for i in np.flatnonzero(hit)]


def match_external(index, scene, buildings, min_iou):
    """{object_id: (item, iou)} best IoU match per building among external lon/lat footprints."""
    if index is None or not buildings:
        return {}
    xs = [b['lonlat'].bounds for b in buildings.values()]
    w, s = min(b[0] for b in xs), min(b[1] for b in xs)
    e, n = max(b[2] for b in xs), max(b[3] for b in xs)
    candidates = []
    for item in index.query(w, s, e, n):
        parts = []
        for ring in item['rings']:
            ring = np.asarray(ring, dtype=float)
            x, y = lonlat_to_local(ring[:, 0], ring[:, 1], scene)
            parts.append(_polygon(np.c_[x, y]))
        geom = unary_union(parts) if len(parts) > 1 else parts[0]
        if not geom.is_empty:
            candidates.append((item, geom))
    out = {}
    if not candidates:
        return out
    from shapely.strtree import STRtree
    tree = STRtree([geom for _, geom in candidates])
    for oid, building in buildings.items():
        poly = building['local']
        best = None
        for position in tree.query(poly, predicate='intersects'):
            item, geom = candidates[int(position)]
            inter = geom.intersection(poly).area
            iou = inter/max(geom.union(poly).area, 1e-9)
            if iou >= min_iou and (best is None or iou > best[1]):
                best = (item, iou)
        if best:
            out[oid] = best
    return out


def icesat_heights(photon_passes, scene, buildings, occupied, trees, shifts=None):
    """{object_id: {...}} roof-minus-ground ATL03 photon heights for buildings crossed by a beam.

    Roof photons: inside the outline eroded 1 m; ground photons: 2-12 m ring outside every building (+1 m) and
    tree canopy. Both are medians (roof over its top half); ATL03 horizontal geolocation is about 3-5 m, so
    small structures are not attributable and the result is a cross-check of scale, not a per-object label.
    """
    out = {}
    if not photon_passes or not buildings:
        return out
    inner, outer = PARAMS['icesat_ground_ring_m']
    frame = unary_union([b['local'] for b in buildings.values()]).buffer(outer).bounds
    blocked = occupied.buffer(1.) if occupied is not None else None
    if trees is not None:
        blocked = trees if blocked is None else blocked.union(trees)
    for key, beams in photon_passes.items():
        shift = (shifts or {}).get(key)
        if shifts is not None and (shift is None or not shift.get('accepted')):
            continue                      # a registration was run and this pass failed it: its photons are not attributable
        dx, dy = (shift['dx_m'], shift['dy_m']) if shift else (0., 0.)
        for beam, photons in beams.items():
            x, y = lonlat_to_local(photons[:, 1], photons[:, 0], scene)
            x, y = x+dx, y+dy
            h = photons[:, 2]
            keep = (photons[:, 3] >= 2) & (x >= frame[0]) & (x <= frame[2]) & (y >= frame[1]) & (y <= frame[3])
            x, y, h = x[keep], y[keep], h[keep]
            if not len(x):
                continue
            for oid, building in buildings.items():
                poly = building['local']
                w, s, e, n = poly.buffer(outer).bounds
                near = (x >= w) & (x <= e) & (y >= s) & (y <= n)
                if near.sum() < PARAMS['icesat_min_roof']+PARAMS['icesat_min_ground']:
                    continue
                px, py, ph = x[near], y[near], h[near]
                inner_poly = poly.buffer(-PARAMS['erode_m'])
                if inner_poly.is_empty:
                    continue
                roof = ph[contains_xy(inner_poly, px, py)]
                ring = poly.buffer(outer).difference(poly.buffer(inner))
                if blocked is not None:
                    ring = ring.difference(blocked)
                ground = ph[contains_xy(ring, px, py)] if not ring.is_empty else np.empty(0)
                if roof.size < PARAMS['icesat_min_roof'] or ground.size < PARAMS['icesat_min_ground']:
                    continue
                level = float(np.median(ground))
                value = top_half_median(roof)-level
                entry = {'height_m': _num(value, 2), 'eave_m': _num(np.quantile(roof, .25)-level, 2), 'roof_photons': int(roof.size),
                         'ground_photons': int(ground.size), 'pass': key, 'beam': beam, 'shift_m': [_num(dx, 1), _num(dy, 1)],
                         'roof_iqr_m': _num(np.subtract(*np.quantile(roof, [.75, .25])), 2)}
                if oid not in out or out[oid]['roof_photons'] < roof.size:
                    out[oid] = entry
    return out


def _scene_label_rasters(scene, pixel_m):
    """Per-scene rasters on a pixel_m grid in scene metres: eroded roof label, ground-ring label (nearest building 2-12 m away,
    outside every building +1 m and tree canopy), plus the grid origin. Labels index scene_buildings(only_owned=False)."""
    from PIL import Image, ImageDraw
    from scipy.ndimage import distance_transform_edt
    buildings, occupied, trees = scene_buildings(scene, only_owned=False)
    ids = list(buildings)
    if not ids:
        return None
    union = unary_union([b['local'] for b in buildings.values()]).buffer(PARAMS['icesat_ground_ring_m'][1]+2)
    x0, y0, x1, y1 = union.bounds
    w, h = int(math.ceil((x1-x0)/pixel_m)), int(math.ceil((y1-y0)/pixel_m))

    def to_px(geom):
        return [((x-x0)/pixel_m, (y1-y)/pixel_m) for x, y in geom.exterior.coords]

    def draw(polys, value_of):
        image = Image.new('I', (w, h), -1)
        canvas = ImageDraw.Draw(image)
        for index, poly in polys:
            for part in getattr(poly, 'geoms', [poly]):
                if not part.is_empty and part.geom_type == 'Polygon':
                    canvas.polygon(to_px(part), fill=value_of(index))
        return np.asarray(image, dtype=np.int32)

    roof = draw([(i, buildings[oid]['local'].buffer(-PARAMS['erode_m'])) for i, oid in enumerate(ids)], lambda i: i)
    footprint = draw([(i, buildings[oid]['local']) for i, oid in enumerate(ids)], lambda i: i)
    blocked = draw([(0, occupied.buffer(1.))] + ([(0, trees)] if trees is not None else []), lambda i: 0) >= 0 if occupied is not None else np.zeros((h, w), bool)
    distance, (iy, ix) = distance_transform_edt(footprint < 0, return_indices=True)
    nearest = footprint[iy, ix]
    inner, outer = PARAMS['icesat_ground_ring_m']
    ground = np.where(~blocked & (distance*pixel_m >= inner) & (distance*pixel_m <= outer), nearest, -1)
    return {'ids': ids, 'roof': roof, 'ground': ground, 'origin': (x0, y1), 'pixel_m': pixel_m, 'shape': (h, w)}


def register_icesat(root, scene_ids, passes, radius_m=None, step_m=None, pixel_m=.5, log=None):
    """Per-pass horizontal shift of ATL03 photons against building outlines (ATL03 geolocation is about 3-5 m).

    For each pass and each candidate shift, every building crossed by its photons gets mean(roof photons) minus
    mean(ground-ring photons); the objective is the mean of those contrasts (clipped to -2..10 m) over buildings in
    split A (object-id hash even). The chosen shift is then scored on split B, which never influenced the choice;
    a pass is accepted only if its B contrast at the chosen shift beats the unshifted B contrast.
    """
    radius_m = radius_m or PARAMS['icesat_register_radius_m']
    step_m = step_m or PARAMS['icesat_register_step_m']
    offsets = np.arange(-radius_m, radius_m+step_m/2, step_m)
    shifts = [(dx, dy) for dx in offsets for dy in offsets]
    stats = {key: np.zeros((len(shifts), 2, 2)) for key in passes}     # [shift, split, (sum, count)]
    for scene_id in scene_ids:
        scene = load_scene(root, scene_id)
        rasters = None
        for key, beams in passes.items():
            photons = np.concatenate([v for v in beams.values()])
            photons = photons[photons[:, 3] >= 2]
            if not len(photons):
                continue
            x, y = lonlat_to_local(photons[:, 1], photons[:, 0], scene)
            half = scene['image_width']*math.sqrt(abs(np.linalg.det(np.asarray(scene['pixel_to_meters'])[:2])))/2+radius_m+15
            near = (np.abs(x) < half) & (np.abs(y) < half)
            if near.sum() < 20:
                continue
            if rasters is None:
                rasters = _scene_label_rasters(scene, pixel_m)
                if rasters is None:
                    break
                split = np.array([int(hashlib.sha256(oid.encode()).hexdigest(), 16) % 2 for oid in rasters['ids']])
            x, y, h = x[near], y[near], photons[near, 2]
            keep = np.abs(h-np.median(h)) < 40
            x, y, h = x[keep], y[keep], h[keep]
            n = len(rasters['ids'])
            (ox, oy), (rows_n, cols_n) = rasters['origin'], rasters['shape']
            for index, (dx, dy) in enumerate(shifts):
                cols = np.floor((x+dx-ox)/pixel_m).astype(int)
                rows = np.floor((oy-(y+dy))/pixel_m).astype(int)
                inside = (cols >= 0) & (cols < cols_n) & (rows >= 0) & (rows < rows_n)
                roof = np.full(len(x), -1)
                ground = np.full(len(x), -1)
                roof[inside] = rasters['roof'][rows[inside], cols[inside]]
                ground[inside] = rasters['ground'][rows[inside], cols[inside]]
                r_ok, g_ok = roof >= 0, ground >= 0
                r_count = np.bincount(roof[r_ok], minlength=n)
                g_count = np.bincount(ground[g_ok], minlength=n)
                valid = (r_count >= PARAMS['icesat_min_roof']) & (g_count >= PARAMS['icesat_min_ground'])
                if not valid.any():
                    continue
                r_mean = np.bincount(roof[r_ok], weights=h[r_ok], minlength=n)/np.maximum(r_count, 1)
                g_mean = np.bincount(ground[g_ok], weights=h[g_ok], minlength=n)/np.maximum(g_count, 1)
                contrast = np.clip(r_mean-g_mean, -2, 10)
                for part in (0, 1):
                    chosen = valid & (split == part)
                    stats[key][index, part] += (contrast[chosen].sum(), chosen.sum())
    zero = shifts.index((0., 0.)) if (0., 0.) in shifts else int(np.argmin([abs(a)+abs(b) for a, b in shifts]))
    out = {}
    for key, table in stats.items():
        counts_a, counts_b = table[:, 0, 1], table[:, 1, 1]
        mean_a = np.where(counts_a >= PARAMS['icesat_register_min_buildings'], table[:, 0, 0]/np.maximum(counts_a, 1), -np.inf)
        if not np.isfinite(mean_a).any():
            out[key] = {'accepted': False, 'reason': 'too few crossed buildings', 'buildings_a': int(counts_a.max(initial=0))}
            continue
        top = float(np.max(mean_a))   # near-ties (within 5 cm) go to the smallest shift: no shift without evidence
        ties = [i for i in range(len(shifts)) if mean_a[i] >= top-.05]
        best = min(ties, key=lambda i: (math.hypot(*shifts[i]), i))
        mean_b = table[:, 1, 0]/np.maximum(counts_b, 1)
        accepted = bool(counts_b[best] >= PARAMS['icesat_register_min_buildings'] and mean_b[best] > mean_b[zero]+PARAMS['icesat_register_gain_m'])
        out[key] = {'dx_m': float(shifts[best][0]), 'dy_m': float(shifts[best][1]), 'accepted': accepted,
                    'contrast_a_m': _num(mean_a[best], 2), 'buildings_a': int(counts_a[best]),
                    'contrast_b_m': _num(mean_b[best], 2), 'contrast_b_unshifted_m': _num(mean_b[zero], 2), 'buildings_b': int(counts_b[best]),
                    'reason': None if accepted else 'hold-out split B not improved by the shift chosen on split A'}
        if log:
            log(f"  {key}: shift ({shifts[best][0]:+.0f}, {shifts[best][1]:+.0f}) m, A {mean_a[best]:.2f} m (n={int(counts_a[best])}), "
                f"B {mean_b[best]:.2f} vs {mean_b[zero]:.2f} unshifted (n={int(counts_b[best])}) -> {'accepted' if accepted else 'rejected'}")
    return out


def load_icesat(cache):
    """{'<first date>/rgt<track>': {beam: photons}} with the same overpass on adjacent UTC dates merged (unique photons)."""
    scan_body, _ = cache.derived('icesat2/track-scan.json')
    if not scan_body:
        return {}
    scan = json.loads(scan_body)
    raw = []
    for day, tracks in sorted(scan.get('days', {}).items()):
        if not isinstance(tracks, list):
            continue
        for track in tracks:
            entry = cache.entry(URLS['icesat2_atl03'], atl03_params(day, int(track)))
            if not entry or entry.get('status') != 'ok':
                continue
            beams = parse_atl03(cache._read(entry))
            if beams:
                raw.append((date.fromisoformat(day), int(track), beams))
    passes = {}
    last = {}
    for day, track, beams in raw:
        previous = last.get(track)
        if previous and (day-previous[0]).days <= 1:
            key = previous[1]
            for beam, photons in beams.items():
                merged = np.concatenate([passes[key][beam], photons]) if beam in passes[key] else photons
                passes[key][beam] = np.unique(np.round(merged, 7), axis=0)
            last[track] = (day, key)
            continue
        key = f'{day.isoformat()}/rgt{track}'
        passes[key] = {beam: np.unique(np.round(photons, 7), axis=0) for beam, photons in beams.items()}
        last[track] = (day, key)
    return passes


def load_shadow_heights(db):
    out = {}
    for row in eyes_index.rows(db, TABLE, version='structure-kinds-optical-v1'):
        height = (row['payload'] or {}).get('height') or {}
        if height.get('height_estimate_m') is not None:
            out[(row['scene_id'], row['object_id'])] = {'height_m': height['height_estimate_m'], 'shadow_m': height.get('shadow_m'),
                                                        'note': height.get('note')}
    return out


def load_sub_kinds(db):
    """{(scene, object): (sub_kind, version)} from structure-kinds-v3, falling back to v2."""
    out = {}
    for version in ('structure-kinds-v2', 'structure-kinds-v3'):
        for scene_id, object_id, payload in db.execute(
                "SELECT scene_id, object_id, json_extract(payload, '$.sub_kind') FROM object_quality WHERE version=?", (version,)):
            if payload:
                out[(scene_id, object_id)] = (payload, version)
    return out


def load_address_roofs(root):
    """ML heights from public/address-roofs.json as lon/lat footprints (depth model x 7 m median anchor per tile)."""
    try:
        entries = json.loads((Path(root)/'public/address-roofs.json').read_text())
    except (OSError, ValueError):
        return []
    out = []
    for entry in entries:
        ring = entry.get('ring_meters') or []
        centre = entry.get('centroid') or {}
        if len(ring) < 3 or 'lat' not in centre:
            continue
        lat0, lon0 = centre['lat'], centre['lon']
        k_lat = 111132.954-559.822*math.cos(2*math.radians(lat0))
        k_lon = 111412.84*math.cos(math.radians(lat0))
        out.append({'id': f"address/{entry['address_id']}", 'address_id': entry['address_id'], 'full_address': entry.get('full_address'),
                    'rings': [[(lon0+x/k_lon, lat0+y/k_lat) for x, y in ring]], 'height_m': entry.get('height'),
                    'eave_m': entry.get('eave_height'), 'ridge_m': entry.get('ridge_height'), 'kind': entry.get('kind'),
                    'quality_flags': entry.get('quality_flags')})
    return out


def load_crm_heights(root):
    """CRM roof overrides that carry eave/ridge heights (read-only open of the geo database)."""
    file = Path(root)/'data/australia_geo.db'
    if not file.exists():
        return []
    out = []
    try:
        with closing(sqlite3.connect(f'file:{file}?mode=ro', uri=True)) as db:
            rows = db.execute("SELECT id, address_id, geometry_json, properties, updated_by FROM crm_features "
                              "WHERE kind='roof' AND (properties LIKE '%ridge_height%' OR properties LIKE '%eave_height%')").fetchall()
    except sqlite3.Error:
        return []
    for fid, address_id, geometry, properties, updated_by in rows:
        props = json.loads(properties or '{}')
        try:
            ridge = float(props.get('ridge_height')) if props.get('ridge_height') not in (None, '') else None
            eave = float(props.get('eave_height')) if props.get('eave_height') not in (None, '') else None
        except (TypeError, ValueError):
            continue
        if ridge is None and eave is None:
            continue
        geom = shape(json.loads(geometry))
        polys = list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [geom]
        out.append({'id': f'crm/{fid}', 'address_id': address_id, 'rings': [list(p.exterior.coords) for p in polys],
                    'height_m': ridge, 'eave_m': eave, 'updated_by': updated_by,
                    'note': 'CRM roof override heights (editor/manual estimates, not surveyed)'})
    return out


# --- depth-to-metres fit ------------------------------------------------------------------------------------------

ANCHORS = {
    'icesat2': {'independent': True, 'note': 'ATL03 photon roof-minus-ground heights (spaceborne LiDAR, 3-5 m geolocation)'},
    'osm-height': {'independent': True, 'note': 'OSM height tags (mapper-entered; levels-only tags excluded)'},
    'ms-ml': {'independent': True, 'note': 'Microsoft ML footprint heights (imagery model; 3.0 m floor values excluded as censored)'},
    'crm': {'independent': True, 'note': 'CRM roof override ridge heights (editor estimates)'},
    'address-roofs-ml': {'independent': False,
                         'note': 'CIRCULAR: tile pipeline heights = the same depth model x (7.0 m / median tile disparity), clipped 2.5-150 m'},
}
ADOPTION_ORDER = ('icesat2', 'osm-height', 'ms-ml')
MS_FLOOR_M = 3.0


def _spearman(x, y):
    from scipy.stats import spearmanr
    if len(x) < 3:
        return None
    value = spearmanr(x, y).statistic
    return _num(value, 3)


def fit_linear(x, y, groups, folds=5, seed=0):
    """Least-squares height = a + b * x with scene-grouped K-fold CV against a constant (training median) baseline."""
    x, y, groups = np.asarray(x, dtype=float), np.asarray(y, dtype=float), np.asarray(groups)
    n = len(x)
    if n < 3 or np.ptp(x) <= 0:
        return {'n': int(n), 'ok': False, 'reason': 'too few anchors or no relief spread'}
    design = np.c_[np.ones(n), x]
    (a, b), *_ = np.linalg.lstsq(design, y, rcond=None)
    fitted = a+b*x
    total = float(np.sum((y-y.mean())**2)) or 1e-9
    unique = sorted(set(groups.tolist()))
    random.Random(seed).shuffle(unique)
    fold_of = {g: i % folds for i, g in enumerate(unique)}
    predicted, baseline = np.full(n, np.nan), np.full(n, np.nan)
    for fold in range(min(folds, len(unique))):
        test = np.array([fold_of[g] == fold for g in groups.tolist()])
        train = ~test
        if train.sum() < 3 or test.sum() == 0 or np.ptp(x[train]) <= 0:
            continue
        (fa, fb), *_ = np.linalg.lstsq(design[train], y[train], rcond=None)
        predicted[test] = fa+fb*x[test]
        baseline[test] = np.median(y[train])
    valid = np.isfinite(predicted)
    residual = y[valid]-predicted[valid]
    base_residual = y[valid]-baseline[valid]
    cv_ss = float(np.sum(residual**2))
    return {'n': int(n), 'ok': bool(valid.sum() >= 3), 'a': _num(a, 4), 'b': _num(b, 4), 'r2_in_sample': _num(1-np.sum((y-fitted)**2)/total, 3),
            'cv_n': int(valid.sum()), 'cv_folds': int(min(folds, len(unique))), 'groups': len(unique),
            'cv_mae_m': _num(np.mean(np.abs(residual)), 3) if valid.any() else None,
            'cv_rmse_m': _num(np.sqrt(np.mean(residual**2)), 3) if valid.any() else None,
            'cv_r2': _num(1-cv_ss/max(float(np.sum((y[valid]-y[valid].mean())**2)), 1e-9), 3) if valid.any() else None,
            'baseline_mae_m': _num(np.mean(np.abs(base_residual)), 3) if valid.any() else None,
            'spearman': _spearman(x, y), 'anchor_median_m': _num(np.median(y), 2), 'anchor_iqr_m': [_num(v, 2) for v in np.quantile(y, [.25, .75])]}


def anchor_values(measure):
    """{anchor: metres} available for one object's measurements."""
    out = {}
    if measure.get('icesat2') and measure['icesat2'].get('registered'):
        out['icesat2'] = measure['icesat2']['height_m']
    osm = measure.get('osm')
    if osm and osm.get('method') == 'height tag':
        out['osm-height'] = osm['height_m']
    ms = measure.get('ms_ml')
    if ms and ms.get('height_m') is not None and ms['height_m'] > MS_FLOOR_M+.05:
        out['ms-ml'] = ms['height_m']
    if measure.get('crm') and measure['crm'].get('height_m') is not None:
        out['crm'] = measure['crm']['height_m']
    if measure.get('address_roofs_ml') and measure['address_roofs_ml'].get('height_m') is not None:
        out['address-roofs-ml'] = measure['address_roofs_ml']['height_m']
    return out


def fit_depth(measures, scene_refs):
    """Fit metres against every anchor for two relief features; adopt the first independent anchor that beats the baseline."""
    fits = {}
    for anchor, meta in ANCHORS.items():
        for feature in ('relief', 'relief_scene_norm'):
            xs, ys, groups = [], [], []
            for (scene_id, _), measure in measures.items():
                depth = measure.get('depth')
                value = anchor_values(measure).get(anchor)
                if depth is None or value is None:
                    continue
                x = depth['relief'] if feature == 'relief' else (depth['relief']/scene_refs[scene_id] if scene_refs.get(scene_id) else None)
                if x is None or not math.isfinite(x):
                    continue
                xs.append(x)
                ys.append(value)
                groups.append(scene_id)
            fits[f'{anchor}:{feature}'] = {'anchor': anchor, 'feature': feature, 'independent': meta['independent'], 'note': meta['note'],
                                           **fit_linear(xs, ys, groups)}
    adopted = None
    for anchor in ADOPTION_ORDER:
        candidates = [f for f in fits.values() if f['anchor'] == anchor and f.get('ok') and f['n'] >= PARAMS['depth_min_anchors']
                      and f['cv_mae_m'] is not None and f['cv_mae_m'] <= (1-PARAMS['depth_adopt_gain'])*f['baseline_mae_m']]
        if candidates:
            adopted = min(candidates, key=lambda f: f['cv_mae_m'])
            break
    rule = (f"first of {list(ADOPTION_ORDER)} with >= {PARAMS['depth_min_anchors']} anchors whose scene-grouped CV MAE is at least "
            f"{int(PARAMS['depth_adopt_gain']*100)} % below a constant (training median) baseline; CRM (too few) and address-roofs "
            'ML heights (circular) are reported, never adopted')
    return {'fits': fits, 'adopted': adopted, 'rule': rule}


def depth_to_metres(depth, fit, scene_ref):
    if not depth or not fit:
        return None, None
    x = depth['relief'] if fit['feature'] == 'relief' else (depth['relief']/scene_ref if scene_ref else None)
    xe = depth['relief_eave'] if fit['feature'] == 'relief' else (depth['relief_eave']/scene_ref if scene_ref else None)
    if x is None:
        return None, None
    height = max(fit['a']+fit['b']*x, 1.0)
    eave = min(max(fit['a']+fit['b']*xe, .5), height) if xe is not None else None
    return height, eave


# --- assignment --------------------------------------------------------------------------------------------------

def measure_scene(root, scene, *, depth_runner, externals, lidar_pairs, photon_passes, icesat_shifts=None, only_owned=True):
    """Raw per-building measurements (no fitting) for one scene."""
    buildings, occupied, trees = scene_buildings(scene, only_owned=only_owned)
    measures = {oid: {'area_m2': _num(b['local'].area, 2), 'owned': b['owned']} for oid, b in buildings.items()}
    for oid, value in lidar_for_scene(lidar_pairs, scene, buildings).items():
        measures[oid]['lidar'] = value
    depth_status = None
    if depth_runner is not None:
        values, matrix = depth_runner.depth(scene)
        if values is None:
            depth_status = matrix
        else:
            for oid, building in buildings.items():
                relief = depth_relief(values, matrix, building['local'], occupied, trees)
                if relief:
                    measures[oid]['depth'] = {k: (_num(v, 5) if isinstance(v, float) else v) for k, v in relief.items()}
    for name, (index, min_iou) in externals.items():
        for oid, (item, iou) in match_external(index, scene, buildings, min_iou).items():
            record = {'id': item['id'], 'iou': _num(iou, 3), 'height_m': _num(item.get('height_m'), 2)}
            for key in ('method', 'tags', 'eave_m', 'ridge_m', 'address_id', 'full_address', 'kind', 'updated_by', 'confidence'):
                if item.get(key) is not None:
                    record[key] = item[key]
            measures[oid][name] = record
    if photon_passes:
        raw = icesat_heights(photon_passes, scene, buildings, occupied, trees)
        registered = icesat_heights(photon_passes, scene, buildings, occupied, trees, shifts=icesat_shifts) if icesat_shifts else {}
        for oid in set(raw) | set(registered):
            value = registered.get(oid) or raw[oid]
            measures[oid]['icesat2'] = {**value, 'registered': oid in registered,
                                        'note': 'co-registered pass accepted on a hold-out split' if oid in registered else
                                                'unregistered ATL03 photons (3-5 m geolocation): cross-check only, never an anchor'}
    primaries = [m['depth']['relief'] for m in measures.values() if m.get('depth') and (m['area_m2'] or 0) >= PARAMS['depth_primary_area_m2']]
    scene_ref = float(np.median(primaries)) if len(primaries) >= 3 and np.median(primaries) > 0 else None
    return measures, scene_ref, depth_status


def choose(measure, fit, scene_ref, provenance):
    """Primary height with source and uncertainty, plus every cross-check (never trusted alone)."""
    checks = {}
    height_d, eave_d = depth_to_metres(measure.get('depth'), fit, scene_ref) if fit else (None, None)
    if measure.get('depth'):
        checks['depth_model'] = {**measure['depth'], 'height_m': _num(height_d, 2), 'eave_m': _num(eave_d, 2),
                                 'fit': fit and f"{fit['anchor']}:{fit['feature']}", 'scene_primary_relief': _num(scene_ref, 5)}
    for key in ('osm', 'ms_ml', 'icesat2', 'shadow', 'address_roofs_ml', 'crm'):
        if measure.get(key):
            checks[key] = measure[key]
    checks['prior'] = {'height_m': PARAMS['prior_height_m'], 'note': "scene_extract.HEIGHTS['building'] class prior"}
    if measure.get('lidar'):
        lidar = measure['lidar']
        return {'source': 'lidar-ndsm', 'height_m': lidar['height_m'], 'eave_m': lidar['eave_m'], 'uncertainty_m': lidar['uncertainty_m'],
                'samples': lidar['samples'], 'capture_date': lidar.get('capture_date'),
                'provenance': {**provenance['lidar'], 'dataset': lidar.get('dataset'), 'url': lidar.get('url'), 'licence': lidar.get('licence'),
                               'erode_m': lidar['erode_m']}}, checks
    if height_d is not None:
        return {'source': 'depth-model', 'height_m': _num(height_d, 2), 'eave_m': _num(eave_d, 2), 'uncertainty_m': fit['cv_rmse_m'],
                'samples': measure['depth']['samples'], 'capture_date': None,
                'provenance': {**provenance['depth'], 'erode_m': measure['depth']['erode_m']}}, checks
    osm = measure.get('osm')
    if osm and osm.get('height_m') is not None:
        uncertainty = 1.0 if osm.get('method') == 'height tag' else 1.5
        return {'source': 'osm', 'height_m': osm['height_m'], 'eave_m': None, 'uncertainty_m': uncertainty, 'samples': 1,
                'capture_date': None, 'provenance': {**provenance['osm'], 'element': osm['id'], 'method': osm.get('method'),
                                                     'match_iou': osm['iou'], 'uncertainty_note': 'assumed, not measured'}}, checks
    return {'source': 'prior', 'height_m': PARAMS['prior_height_m'], 'eave_m': None, 'uncertainty_m': PARAMS['prior_uncertainty_m'],
            'samples': 0, 'capture_date': None, 'provenance': provenance['prior']}, checks


def assign(root=Path('.'), scene_ids=(), *, offline=True, only_owned=True, write=True, refit=None, depth=True, log=print):
    """Measure, fit and write heights-v1 rows for the scenes; returns (rows, fit_report, statuses)."""
    root = Path(root)
    cache = Cache(root, offline=True, log=log)   # assignment never opens a socket; --fetch fills the cache
    scene_ids = list(dict.fromkeys(validate_scene_id(s) for s in scene_ids))
    osm_payload = load_osm(cache)
    externals = {
        'osm': (LonLatIndex(osm_buildings(osm_payload)), PARAMS['osm_match_iou']),
        'ms_ml': (LonLatIndex(ms_features(load_ms_buildings(cache))), PARAMS['ms_match_iou']),
        'address_roofs_ml': (LonLatIndex(load_address_roofs(root)), .3),
        'crm': (LonLatIndex(load_crm_heights(root)), .2),
    }
    photon_passes = load_icesat(cache)
    lidar_pairs = load_lidar_pairs(cache)
    refit = (len(scene_ids) >= 50) if refit is None else refit
    registration_file = root/REPORT_DIR/'icesat-registration.json'
    icesat_shifts = None
    if photon_passes:
        if refit or not registration_file.exists():
            icesat_shifts = register_icesat(root, scene_ids, photon_passes, log=log)
            if refit:
                registration_file.parent.mkdir(parents=True, exist_ok=True)
                registration_file.write_text(json.dumps({'version': VERSION, 'computed_at': _now(), 'scenes': len(scene_ids),
                                                         'passes': icesat_shifts}, indent=1, sort_keys=True))
        else:
            icesat_shifts = json.loads(registration_file.read_text())['passes']
    runner = DepthRunner(root) if depth else None
    shadows = {}
    if path_exists(root):
        with closing(eyes_index.connection(root, readonly=True)) as db:
            shadows = load_shadow_heights(db)
    measures, scene_refs, statuses = {}, {}, {}
    started = time.monotonic()
    for index, scene_id in enumerate(scene_ids):
        scene = load_scene(root, scene_id)
        scene_measures, scene_refs[scene_id], statuses[scene_id] = measure_scene(
            root, scene, depth_runner=runner, externals=externals, lidar_pairs=lidar_pairs, photon_passes=photon_passes,
            icesat_shifts=icesat_shifts, only_owned=only_owned)
        for oid, measure in scene_measures.items():
            if (scene_id, oid) in shadows:
                measure['shadow'] = shadows[(scene_id, oid)]
            measures[(scene_id, oid)] = measure
        if log and (index+1) % 25 == 0:
            log(f'  [{index+1}/{len(scene_ids)}] measured, {time.monotonic()-started:.0f}s')
    fit_file = root/REPORT_DIR/'fit.json'
    if refit or not fit_file.exists():
        fit_report = fit_depth(measures, scene_refs)
        fit_report.update(version=VERSION, computed_at=_now(), scenes=len(scene_ids), objects=len(measures),
                          model_sha256=runner.model_sha if runner else None, params=PARAMS, icesat_registration=icesat_shifts)
        if refit:
            fit_file.parent.mkdir(parents=True, exist_ok=True)
            fit_file.write_text(json.dumps(fit_report, indent=1, sort_keys=True, default=list))
    else:
        fit_report = json.loads(fit_file.read_text())
    fit = fit_report.get('adopted')
    provenance = {
        'lidar': {'method': 'nDSM = DSM - DEM; roof = median of the top half of cells inside the outline eroded 1 m; eave = lower quartile'},
        'depth': {'method': 'Depth Anything V2 Small relative relief (roof top-half median inside the outline eroded 1 m minus the median '
                            'of a 1.5-6 m ground ring without buildings or trees) mapped to metres by a linear fit',
                  'model': str(runner.model_path.relative_to(root)) if runner and runner.model_path.is_relative_to(root) else None,
                  'model_sha256': runner.model_sha if runner else None, 'licence': LICENCES['depth-model'],
                  'input_size': runner.input_size if runner else None,
                  'onnx_providers': runner.providers if runner else None,
                  'depth_cache': str(CACHE_DIR/'depth'),
                  'fit': fit and {k: fit.get(k) for k in ('anchor', 'feature', 'a', 'b', 'n', 'cv_mae_m', 'cv_rmse_m', 'cv_r2', 'baseline_mae_m', 'spearman')},
                  'uncertainty_note': 'scene-grouped CV RMSE of the fit against its anchor (includes the anchor error)',
                  'imagery': 'scene image (Google z19, capture date not recorded in the scene)'},
        'osm': {'dataset': 'OpenStreetMap buildings', 'licence': LICENCES['osm'], **((osm_payload or {}).get('_provenance') or {})},
        'prior': {'method': "class prior scene_extract.HEIGHTS['building']", 'uncertainty_note': 'assumed spread, not measured'},
    }
    stamp, code = _now(), code_sha256()
    rows = []
    scene_cache = {}
    for (scene_id, oid), measure in measures.items():
        scene = scene_cache.get(scene_id) or load_scene(root, scene_id)
        scene_cache = {scene_id: scene}
        primary, checks = choose(measure, fit, scene_refs.get(scene_id), provenance)
        payload = {'version': VERSION, 'note': NOTE, 'kind': 'building', **primary, 'area_m2': measure['area_m2'],
                   'owned': measure['owned'], 'cross_checks': checks, 'computed_at': stamp, 'code_sha256': code}
        rows.append({'scene_id': scene_id, 'object_id': oid, 'model_signature': scene.get('model_signature'),
                     'version': VERSION, 'payload': payload})
    if write and rows:
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            for scene_id in scene_ids:
                db.execute(f'DELETE FROM "{TABLE}" WHERE scene_id=? AND version=?', (scene_id, VERSION))
            eyes_index.replace_rows(db, TABLE, rows)
    return rows, fit_report, statuses


def path_exists(root):
    return eyes_index.path(root).exists()


# --- summary and contact sheet -----------------------------------------------------------------------------------

UNASSESSED = 'primary-unassessed (>=150 m2, no structure-kinds row)'
SHEET_PLAN = (('shipping-container', 3), ('container-stack', 1), ('garage', 4), ('shed', 4), ('attached-cover', 2), ('carport', 1),
              ('outbuilding', 2), ('not-secondary', 3), (UNASSESSED, 4))


def _stats(values):
    values = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not values:
        return {'n': 0}
    q1, med, q3 = np.quantile(values, [.25, .5, .75])
    return {'n': len(values), 'median': _num(med, 2), 'q25': _num(q1, 2), 'q75': _num(q3, 2), 'iqr': _num(q3-q1, 2)}


def _agreement(pairs):
    """pairs of (ours, theirs) -> bias, median absolute difference, spearman, n."""
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if not pairs:
        return {'n': 0}
    ours, theirs = np.array(pairs, dtype=float).T
    diff = ours-theirs
    return {'n': len(pairs), 'median_ours_m': _num(np.median(ours), 2), 'median_theirs_m': _num(np.median(theirs), 2),
            'bias_median_m': _num(np.median(diff), 2), 'median_abs_diff_m': _num(np.median(np.abs(diff)), 2),
            'within_1m': _num(np.mean(np.abs(diff) <= 1), 3), 'spearman': _spearman(ours, theirs)}


def _rank(pairs):
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    return {'n': len(pairs), 'spearman': _spearman(*zip(*pairs, strict=True)) if len(pairs) >= 3 else None}


def suburb_population(root):
    scenes = suburb_scenes(root)
    total = 0
    for scene_id in scenes:
        scene = load_scene(root, scene_id)
        owned = set(scene.get('owned_object_ids') or [])
        total += sum(1 for o in scene['objects'] if o['kind'] == 'building' and o.get('status') not in DEAD_STATUSES and o['id'] in owned)
    return scenes, total


def manifest_sources(cache):
    entries = []
    for _key, entry in sorted(cache.manifest.get("entries", {}).items(), key=lambda kv: (kv[1].get('source') or '', str(kv[1].get('url')))):
        if entry.get('source') == 'icesat2' and not entry.get('derived'):
            continue
        entries.append({k: entry.get(k) for k in ('source', 'url', 'status', 'http_status', 'licence', 'sha256', 'bytes', 'fetched_at',
                                                  'capture_date', 'note', 'features', 'with_height', 'release') if entry.get(k) is not None})
    icesat = [e for e in cache.manifest.get('entries', {}).values() if e.get('source') == 'icesat2' and not e.get('derived')]
    if icesat:
        entries.append({'source': 'icesat2', 'url': URLS['icesat2_atl03'], 'licence': LICENCES['icesat2'],
                        'requests': len(icesat), 'ok': sum(1 for e in icesat if e.get('status') == 'ok'),
                        'bytes': sum(e.get('bytes') or 0 for e in icesat), 'capture_dates': sorted({e.get('capture_date') for e in icesat if e.get('capture_date')})})
    return entries


def summarise(root=Path('.'), fit_report=None, statuses=None, sheet=True, log=print):
    root = Path(root)
    cache = Cache(root, offline=True)
    scenes, population = suburb_population(root)
    scene_set = set(scenes)
    with closing(eyes_index.connection(root, readonly=True)) as db:
        rows = [r for r in eyes_index.rows(db, TABLE, version=VERSION) if r['scene_id'] in scene_set]
        kinds = load_sub_kinds(db)
    owned_rows = [r for r in rows if r['payload'].get('owned')]
    primary = {s: 0 for s in SOURCES}
    measured = {k: 0 for k in ('lidar_ndsm', 'depth_relief', 'depth_metres', 'icesat2', 'osm_match', 'osm_height', 'ms_match', 'ms_height',
                               'shadow', 'address_roofs_ml', 'crm')}
    groups = {}
    for row in owned_rows:
        p, c = row['payload'], row['payload']['cross_checks']
        primary[p['source']] = primary.get(p['source'], 0)+1
        measured['lidar_ndsm'] += p['source'] == 'lidar-ndsm'
        measured['depth_relief'] += 'depth_model' in c
        measured['depth_metres'] += bool(c.get('depth_model', {}).get('height_m') is not None)
        measured['icesat2'] += 'icesat2' in c
        measured['osm_match'] += 'osm' in c
        measured['osm_height'] += bool(c.get('osm', {}).get('height_m') is not None)
        measured['ms_match'] += 'ms_ml' in c
        measured['ms_height'] += bool(c.get('ms_ml', {}).get('height_m') is not None)
        measured['shadow'] += 'shadow' in c
        measured['address_roofs_ml'] += 'address_roofs_ml' in c
        measured['crm'] += 'crm' in c
    all_groups = {}
    for row in rows:
        kind, _ = kinds.get((row['scene_id'], row['object_id']), (UNASSESSED, None))
        all_groups.setdefault(kind, []).append(row)
        if row['payload'].get('owned'):
            groups.setdefault(kind, []).append(row)

    def group_stats(items):
        cs = [r['payload']['cross_checks'] for r in items]
        return {'objects': len(items), 'area_m2': _stats([r['payload']['area_m2'] for r in items]),
                'final': _stats([r['payload']['height_m'] for r in items]),
                'final_sources': {s: sum(1 for r in items if r['payload']['source'] == s) for s in SOURCES},
                'depth_model': _stats([c.get('depth_model', {}).get('height_m') for c in cs]),
                'depth_relief_raw': _stats([c.get('depth_model', {}).get('relief') for c in cs]),
                'ms_ml': _stats([c.get('ms_ml', {}).get('height_m') for c in cs]),
                'ms_ml_above_floor': _stats([h for h in (c.get('ms_ml', {}).get('height_m') for c in cs) if h is not None and h > MS_FLOOR_M+.05]),
                'osm': _stats([c.get('osm', {}).get('height_m') for c in cs]),
                'icesat2': _stats([c.get('icesat2', {}).get('height_m') for c in cs]),
                'shadow': _stats([c.get('shadow', {}).get('height_m') for c in cs]),
                'address_roofs_ml': _stats([c.get('address_roofs_ml', {}).get('height_m') for c in cs])}

    by_kind = {kind: group_stats(items) for kind, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))}
    by_kind_all = {kind: group_stats(items) for kind, items in sorted(all_groups.items(), key=lambda kv: -len(kv[1]))}
    addr = load_address_roofs(root)
    matched = {}
    for row in rows:
        ar = row['payload']['cross_checks'].get('address_roofs_ml')
        if ar and (ar['id'] not in matched or ar['iou'] > matched[ar['id']][1]['cross_checks']['address_roofs_ml']['iou']):
            matched[ar['id']] = (row, row['payload'])
    pairs = list(matched.values())
    agreement = {
        'address_roofs_ml': {
            'entries': len(addr), 'matched_to_suburb_roofs': len(pairs),
            'note': 'these ML heights come from the same depth model with each tile median anchored to 7.0 m; agreement measures '
                    'repeatability across imagery, not accuracy',
            'final_vs_ridge': _agreement([(p['height_m'], p['cross_checks']['address_roofs_ml'].get('height_m')) for _, p in pairs]),
            'depth_model_vs_ridge': _agreement([(p['cross_checks'].get('depth_model', {}).get('height_m'),
                                                 p['cross_checks']['address_roofs_ml'].get('height_m')) for _, p in pairs]),
            'depth_eave_vs_eave': _agreement([(p['cross_checks'].get('depth_model', {}).get('eave_m'),
                                               p['cross_checks']['address_roofs_ml'].get('eave_m')) for _, p in pairs]),
            'ms_ml_vs_ridge': _agreement([(p['cross_checks'].get('ms_ml', {}).get('height_m'),
                                           p['cross_checks']['address_roofs_ml'].get('height_m')) for _, p in pairs]),
            'shadow_vs_ridge': _agreement([(p['cross_checks'].get('shadow', {}).get('height_m'),
                                            p['cross_checks']['address_roofs_ml'].get('height_m')) for _, p in pairs])},
        'ms_ml_vs_depth_model': _agreement([(r['payload']['cross_checks'].get('depth_model', {}).get('height_m'),
                                             r['payload']['cross_checks'].get('ms_ml', {}).get('height_m')) for r in owned_rows]),
        'depth_relief_rank': {
            'note': 'Spearman of the unit-free depth relief against each height source (owned roofs); relief is only mapped to metres '
                    'when a fit is adopted',
            **{name: _rank([(r['payload']['cross_checks'].get('depth_model', {}).get('relief'),
                             (r['payload']['cross_checks'].get(key) or {}).get('height_m')) for r in owned_rows
                            if key != 'icesat2' or (r['payload']['cross_checks'].get(key) or {}).get('registered') is not None])
               for name, key in (('address_roofs_ml', 'address_roofs_ml'), ('ms_ml', 'ms_ml'), ('osm', 'osm'), ('icesat2_unregistered', 'icesat2'),
                                 ('shadow', 'shadow'))},
            'area_m2': _rank([(r['payload']['cross_checks'].get('depth_model', {}).get('relief'), r['payload']['area_m2']) for r in owned_rows])},
        'ms_ml_rank': {name: _rank([((r['payload']['cross_checks'].get('ms_ml') or {}).get('height_m'),
                                     (r['payload']['cross_checks'].get(key) or {}).get('height_m')) for r in owned_rows])
                       for name, key in (('osm', 'osm'), ('address_roofs_ml', 'address_roofs_ml'), ('icesat2_unregistered', 'icesat2'))},
        'crm': [{'crm': r['payload']['cross_checks']['crm'], 'scene_id': r['scene_id'], 'object_id': r['object_id'],
                 'final_m': r['payload']['height_m'], 'source': r['payload']['source'],
                 'depth_model_m': r['payload']['cross_checks'].get('depth_model', {}).get('height_m'),
                 'ms_ml_m': r['payload']['cross_checks'].get('ms_ml', {}).get('height_m')}
                for r in rows if 'crm' in r['payload']['cross_checks']],
        'crm_features_with_heights': len(load_crm_heights(root)),
    }
    fit_report = fit_report or (json.loads((root/REPORT_DIR/'fit.json').read_text()) if (root/REPORT_DIR/'fit.json').exists() else None)
    fetch_body, _ = cache.derived('fetch-report.json')
    fetch_report = json.loads(fetch_body) if fetch_body else None
    summary = {
        'version': VERSION, 'note': NOTE, 'computed_at': _now(), 'code_sha256': code_sha256(), 'bbox': list(BBOX),
        'population': {'suburb_scenes': len(scenes), 'owned_roofs': population, 'rows_owned': len(owned_rows), 'rows_all': len(rows)},
        'coverage_primary_source': {s: {'objects': n, 'of': population} for s, n in primary.items()},
        'coverage_measured': {k: {'objects': n, 'of': population} for k, n in measured.items()},
        'lidar': {'objects_with_lidar_height': primary.get('lidar-ndsm', 0), 'of': population,
                  'status': 'no open LiDAR DSM/DEM covers the bbox; the covering collection is licensed' if not primary.get('lidar-ndsm') else 'ok',
                  'covering_collections': (fetch_report or {}).get('vic_lidar_footprints', {}).get('collections')},
        'depth_fit': fit_report and {'adopted': fit_report.get('adopted'), 'rule': fit_report.get('rule'),
                                     'fits': {k: {kk: v.get(kk) for kk in ('n', 'cv_n', 'groups', 'a', 'b', 'cv_mae_m', 'cv_rmse_m', 'cv_r2',
                                                                           'baseline_mae_m', 'spearman', 'r2_in_sample', 'anchor_median_m',
                                                                           'independent', 'reason')}
                                              for k, v in fit_report.get('fits', {}).items()}},
        'by_sub_kind': by_kind,
        'by_sub_kind_all_live': by_kind_all,
        'by_sub_kind_note': ('by_sub_kind = owned suburb roofs (the 10,072 denominator); by_sub_kind_all_live adds tile-margin objects '
                             'outside owned_object_ids (duplicates of neighbouring tiles; every suburb shipping-container hint is one)'),
        'relief_area_spearman': _spearman(*zip(*[(r['payload']['cross_checks']['depth_model']['relief'], r['payload']['area_m2'])
                                                 for r in owned_rows if r['payload']['cross_checks'].get('depth_model')], strict=True))
                                if any(r['payload']['cross_checks'].get('depth_model') for r in owned_rows) else None,
        'sub_kind_source': 'structure-kinds-v3 rows, falling back to v2; roofs without a row are >= 150 m2 and unflagged',
        'expected': {'shipping-container': 'about 2.6 m (ISO 8 ft 6 in; high cube 2.9 m)', 'garage': '3 to 4 m', 'house': 'higher (single storey ridge 4.5 to 6 m)'},
        'agreement': agreement,
        'sources': manifest_sources(cache),
        'not_fetched': NOT_FETCHED,
        'fetch_report': fetch_report and {k: fetch_report.get(k) for k in ('elvis', 'raster_probes', 'vic_lidar_footprints', 'vic_buildings', 'osm',
                                                                           'ms_buildings', 'icesat2_scan', 'lidar_ndsm_available')},
        'icesat2_passes_with_photons': [p for p in (fetch_report or {}).get('icesat2_passes', []) if p.get('photons')],
        'depth_status': {k: v for k, v in (statuses or {}).items() if v},
    }
    out = root/REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    if sheet:
        picked = sheet_rows(all_groups)
        summary['contact_sheet'] = {'path': str(out/'contact-sheet.png'), 'rows': [
            {'index': i, 'scene_id': r['scene_id'], 'object_id': r['object_id'], 'sub_kind': k, 'height_m': r['payload']['height_m'],
             'source': r['payload']['source'], 'uncertainty_m': r['payload']['uncertainty_m'], 'owned': r['payload'].get('owned'),
             'cross_checks': {name: (r['payload']['cross_checks'].get(name) or {}).get('height_m')
                              for name in ('depth_model', 'ms_ml', 'osm', 'shadow', 'address_roofs_ml', 'icesat2')},
             'depth_relief': (r['payload']['cross_checks'].get('depth_model') or {}).get('relief')}
            for i, (k, r) in enumerate(picked)]}
        render_sheet(root, picked, out/'contact-sheet.png')
    (out/'summary.json').write_text(json.dumps(summary, indent=1, sort_keys=True, default=list))
    return summary


def sheet_rows(groups, seed=7):
    rng = random.Random(seed)
    picked = []
    for kind, count in SHEET_PLAN:
        items = sorted(groups.get(kind, []), key=lambda r: (r['scene_id'], r['object_id']))
        def evidence(r):
            c = r['payload']['cross_checks']
            return sum(1 for k in ('ms_ml', 'osm', 'icesat2', 'address_roofs_ml') if (c.get(k) or {}).get('height_m') is not None)
        best = max((evidence(r) for r in items), default=0)
        preferred = [r for r in items if evidence(r) == best and best > 0] if best else []
        if len(preferred) < count:
            preferred += [r for r in items if r not in preferred and r['payload']['cross_checks'].get('depth_model')][:max(0, count-len(preferred))*20]
        picked.extend((kind, r) for r in rng.sample(preferred, min(count, len(preferred))))
    return picked[:24]


def render_sheet(root, picked, path, columns=6, cell=(300, 250), pad_m=3.):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.load_default(size=17)
    label_h = 6*21+8
    rows_n = max(1, math.ceil(len(picked)/columns))
    sheet = Image.new('RGB', (columns*cell[0], rows_n*(cell[1]+label_h)), (18, 18, 22))
    draw = ImageDraw.Draw(sheet)
    scenes = {}
    for index, (kind, row) in enumerate(picked):
        scene_id = row['scene_id']
        if scene_id not in scenes:
            scene = load_scene(root, scene_id)
            scenes[scene_id] = (scene, load_scene_image(root, scene))
        scene, image = scenes[scene_id]
        cx, cy = (index % columns)*cell[0], (index//columns)*(cell[1]+label_h)
        obj = next((o for o in scene['objects'] if o['id'] == row['object_id']), None)
        if image is not None and obj is not None:
            ring = np.asarray(obj['ring'], dtype=float)
            px_m = math.sqrt(abs(np.linalg.det(np.asarray(scene['pixel_to_meters'])[:2])))
            pad = pad_m/px_m
            x0, y0 = max(int(ring[:, 0].min()-pad), 0), max(int(ring[:, 1].min()-pad), 0)
            x1, y1 = min(int(ring[:, 0].max()+pad)+1, image.width), min(int(ring[:, 1].max()+pad)+1, image.height)
            crop = image.crop((x0, y0, x1, y1))
            k = min(cell[0]/crop.width, cell[1]/crop.height, 4.)
            crop = crop.resize((max(1, int(crop.width*k)), max(1, int(crop.height*k))), Image.Resampling.LANCZOS)
            ImageDraw.Draw(crop).polygon([((x-x0)*k, (y-y0)*k) for x, y in ring], outline=(255, 64, 200), width=2)
            sheet.paste(crop, (cx+(cell[0]-crop.width)//2, cy+(cell[1]-crop.height)//2))
        p, c = row['payload'], row['payload']['cross_checks']

        def fmt(value):
            return '-' if value is None else f'{value:.1f}'
        lines = [f"#{index} {kind[:26]}", f"{scene_id.rsplit('-', 2)[-2]}-{scene_id.rsplit('-', 1)[-1]} {row['object_id'][:8]} {p['area_m2']:.0f} m2",
                 f"{p['height_m']:.1f} +/- {p['uncertainty_m']:.1f} m {p['source']}",
                 f"eave {fmt(p.get('eave_m'))}  depth " + (fmt(c.get('depth_model', {}).get('height_m')) if c.get('depth_model', {}).get('height_m') is not None
                                                            else f"rel {c.get('depth_model', {}).get('relief', float('nan')):.3f}"),
                 f"MS {fmt(c.get('ms_ml', {}).get('height_m'))}  OSM {fmt(c.get('osm', {}).get('height_m'))}  shadow {fmt(c.get('shadow', {}).get('height_m'))}",
                 f"tile-ML {fmt(c.get('address_roofs_ml', {}).get('height_m'))}  ICESat {fmt(c.get('icesat2', {}).get('height_m'))}"]
        for i, text in enumerate(lines):
            draw.text((cx+6, cy+cell[1]+4+i*21), text, fill=(250, 204, 21) if i == 2 else (235, 235, 235), font=font)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return path


# --- CLI ---------------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description='Real structure heights from open data (heights-v1): estimates with provenance, not labels.')
    parser.add_argument('--root', default='.')
    parser.add_argument('--fetch', action='store_true', help='Probe and cache every public source for the suburb bbox.')
    parser.add_argument('--from-cache', action='store_true', help='Offline: replay the cache manifest; never open a socket.')
    parser.add_argument('--no-icesat', action='store_true', help='Skip the ICESat-2 pass scan (about 2,900 day queries on first run).')
    parser.add_argument('--assign', action='store_true', help='Measure, fit and write heights-v1 rows for --scenes / --suburb.')
    parser.add_argument('--scenes', nargs='*', default=[], help='Scene ids.')
    parser.add_argument('--suburb', action='store_true', help=f'All scan tiles ({SUBURB_PREFIX}*).')
    parser.add_argument('--owned-only', action='store_true',
                        help='Only buildings in owned_object_ids (default: every live building, rows flagged owned true/false; '
                             'coverage denominators always use owned roofs).')
    parser.add_argument('--refit', action='store_true', default=None, help='Refit the depth scale on these scenes (default: when >= 50 scenes).')
    parser.add_argument('--no-depth', action='store_true', help='Skip the depth model.')
    parser.add_argument('--dry-run', action='store_true', help='Compute without writing rows.')
    parser.add_argument('--summary', action='store_true', help='Write data/reviews/eyes/heights/summary.json and the contact sheet.')
    parser.add_argument('--no-sheet', action='store_true')
    parser.add_argument('--server-check', type=int, default=0, metavar='N',
                        help='Also compare N scenes against the running ML server /depth grid (server.py get_depth).')
    parser.add_argument('--ml-url', default='http://127.0.0.1:18090')
    parser.add_argument('--fetch-judge-footprints', action='store_true',
                        help='Write the second Microsoft footprint cache around the inner-Melbourne judge tiles (never touches the Mooroolbark file).')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.fetch_judge_footprints:
        report = fetch_ms_judge_footprints(root)
        print(json.dumps({k: report.get(k) for k in ('ok', 'cached', 'bbox', 'features', 'with_height', 'release', 'sha256', 'path', 'error')}, default=str))
        print(json.dumps({'scenes': report.get('scenes')}))
        return 0 if report.get('ok') else 1
    if not (args.fetch or args.assign or args.summary):
        parser.print_help()
        return 2
    if args.fetch:
        report = fetch(root, offline=args.from_cache, icesat=not args.no_icesat)
        print(json.dumps({'elvis_lidar_grade': len(report['elvis']['lidar_grade']), 'elvis_datasets': len(report['elvis']['datasets']),
                          'vic_lidar': report['vic_lidar_footprints']['collections'], 'osm': report['osm'],
                          'ms_buildings': report.get('ms_buildings'), 'icesat2_scan': report.get('icesat2_scan'),
                          'icesat2_passes_with_photons': sum(1 for p in report.get('icesat2_passes', []) if p['photons'])}, default=str))
    statuses, fit_report = None, None
    if args.assign:
        scenes = list(args.scenes) + (suburb_scenes(root) if args.suburb else [])
        if not scenes:
            parser.error('--assign needs --scenes or --suburb')
        rows, fit_report, statuses = assign(root, scenes, only_owned=args.owned_only, write=not args.dry_run,
                                            refit=args.refit, depth=not args.no_depth)
        counts = {}
        for row in rows:
            counts[row['payload']['source']] = counts.get(row['payload']['source'], 0)+1
        print(json.dumps({'rows': len(rows), 'written': 0 if args.dry_run else len(rows), 'sources': counts,
                          'adopted_fit': fit_report.get('adopted') and {k: fit_report['adopted'].get(k) for k in (
                              'anchor', 'feature', 'n', 'cv_mae_m', 'cv_rmse_m', 'baseline_mae_m', 'cv_r2', 'spearman')}}))
        if args.server_check and not args.from_cache:
            print(json.dumps(server_check(root, scenes[:args.server_check], args.ml_url)))
    if args.summary or (args.assign and args.suburb and not args.dry_run):
        summary = summarise(root, fit_report=fit_report, statuses=statuses, sheet=not args.no_sheet)
        print(json.dumps({'summary': str(root/REPORT_DIR/'summary.json'), 'coverage_primary_source': summary['coverage_primary_source'],
                          'lidar': summary['lidar']['objects_with_lidar_height']}))
    return 0


def server_check(root, scene_ids, url):
    """Correlate per-building relief on the scene image with the server's 65 x 65 /depth grid for the scene's analysis tile."""
    from .tiles import tile_bounds
    runner = DepthRunner(root)
    pairs, used = [], []
    for scene_id in scene_ids:
        scene = load_scene(root, scene_id)
        z, x, y = scene['analysis_tile']
        try:
            grid = server_depth(url, z, x, y)
        except Exception as exc:  # noqa: BLE001 - recorded; the server is optional
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:200], 'url': url}
        size = grid['size']
        values = np.asarray(grid['values'], dtype=float).reshape(size, size)
        west, south, east, north = tile_bounds(x, y, z)
        (x0,), (y0,) = (np.atleast_1d(v) for v in lonlat_to_local([west], [north], scene))
        (x1,), (y1,) = (np.atleast_1d(v) for v in lonlat_to_local([east], [south], scene))
        matrix = np.array([[(x1-x0)/size, 0], [0, (y1-y0)/size], [x0, y0]])
        buildings, occupied, trees = scene_buildings(scene)
        mine, own_matrix = runner.depth(scene)
        if mine is None:
            continue
        used.append(scene_id)
        for building in buildings.values():
            a = depth_relief(mine, own_matrix, building['local'], occupied, trees)
            b = depth_relief(values, matrix, building['local'], None, None)
            if a and b:
                pairs.append((a['relief'], b['relief']))
    return {'ok': True, 'url': url, 'scenes': used, 'buildings': len(pairs),
            'spearman_scene_image_vs_server_grid': _spearman(*zip(*pairs, strict=True)) if len(pairs) >= 3 else None,
            'note': 'server grid = pipeline imagery window at z17 resampled to 65 x 65 (about 3.7 m cells); scene image = saved capture'}


if __name__ == '__main__':
    sys.exit(main())
