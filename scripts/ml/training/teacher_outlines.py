"""Open building outlines as a second opinion for teacher windows (teacher-outlines-v1).

LINZ publishes NZ Building Outlines under CC BY 4.0 (95 percent of outlines within 1 m of the roofline, with the
imagery each outline was captured from). For every selected window of a New Zealand teacher source this module pulls
the outlines that intersect the window from the public ArcGIS FeatureServer, keeps them in the source CRS (EPSG:2193)
with provenance, and offers the matching helpers the teacher judges use: a teacher instance that overlaps no outline
is a candidate roof part or non-building; an outline no teacher instance covers is a candidate miss.

Outlines are a weak reference (metre-class edges, capture dates that may differ from the imagery), so they judge
counts and coverage, never corner geometry.

  scripts/ml/training/.venv/bin/python -m scripts.ml.training.teacher_outlines fetch --source linz-chc-2025-0075
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import common
from . import teacher_sources as ts

VERSION = 'teacher-outlines-v1'
LINZ_OUTLINES = {
    'url': 'https://services.arcgis.com/xdsHIIxuCWByZiCB/arcgis/rest/services/LINZ_NZ_Building_Outlines/FeatureServer/0/query',
    'title': 'NZ Building Outlines', 'publisher': 'Toitu Te Whenua Land Information New Zealand', 'licence': 'CC BY 4.0',
    'licence_url': 'https://www.linz.govt.nz/data/licensing-and-using-data/attributing-linz-data',
    'catalogue': 'https://data.linz.govt.nz/layer/101290-nz-building-outlines/',
    'fields': 'building_id,name,use_,capture_method,capture_source_group,capture_source_name,capture_source_from,capture_source_to,last_modified',
    'crs': 'EPSG:2193', 'max_records': 2000,
}
PROVIDERS = {'EPSG:2193': LINZ_OUTLINES}
PAD_DEG = 0.0002        # about 20 m around the lon/lat envelope so outlines cut by the window edge are still fetched


def outlines_dir(root, spec):
    return ts.cache_dir(root, spec) / 'outlines'


def envelope_lonlat(spec, bounds):
    """(w, s, e, n) lon/lat envelope of a window's CRS bounds, padded."""
    lon, lat = ts.to_lonlat(spec['crs'], [bounds[0], bounds[2]], [bounds[1], bounds[3]])
    return min(lon) - PAD_DEG, min(lat) - PAD_DEG, max(lon) + PAD_DEG, max(lat) + PAD_DEG


def query(provider, envelope, out_epsg, attempts=3):
    params = {'where': '1=1', 'geometry': ','.join(f'{v:.7f}' for v in envelope), 'geometryType': 'esriGeometryEnvelope',
              'inSR': 4326, 'outSR': out_epsg, 'outFields': provider['fields'], 'returnGeometry': 'true', 'f': 'geojson'}
    body = urllib.parse.urlencode(params).encode()
    last = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(provider['url'], data=body), timeout=120) as response:
                payload = json.loads(response.read())
            if 'error' in payload:
                raise RuntimeError(payload['error'])
            return payload
        except Exception as error:  # noqa: BLE001 - retried, then raised
            last = error
            time.sleep(2 * (attempt + 1))
    raise last


def fetch(root, source_id, limit=None):
    """One GeoJSON per selected window plus a manifest; skips windows already fetched."""
    root = Path(root)
    spec = ts.resolved(ts.source(source_id), root)
    provider = PROVIDERS.get(spec.get('crs'))
    if provider is None:
        raise SystemExit(f"no open building-outline provider registered for {spec.get('crs')} ({source_id})")
    windows = common.read_json(ts.teacher_dir(root, spec) / 'windows.json')
    folder = outlines_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / 'manifest.json'
    manifest = common.read_json(manifest_path) if manifest_path.exists() else {'version': VERSION, 'provider': provider, 'windows': {}}
    epsg = int(str(spec['crs']).split(':')[-1])
    done, fetched, exceeded = 0, 0, []
    for entry in windows['order'][:limit or len(windows['order'])]:
        path = folder / f"{entry['id']}.geojson"
        if path.exists() and entry['id'] in manifest['windows']:
            done += 1
            continue
        payload = query(provider, envelope_lonlat(spec, entry['bounds']), epsg)
        features = payload.get('features', [])
        if payload.get('properties', {}).get('exceededTransferLimit') or len(features) >= provider['max_records']:
            exceeded.append(entry['id'])
        common.write_json_atomic(path, payload)
        manifest['windows'][entry['id']] = {'features': len(features), 'bounds': entry['bounds'], 'fetched_at': common.now_iso(),
                                            'sha256': common.sha256_file(path)}
        fetched += 1
        common.write_json_atomic(manifest_path, manifest)
    manifest['attribution'] = (f"{provider['title']}, {provider['publisher']}, {provider['licence']} ({provider['licence_url']}); "
                               'used as a weak second opinion on teacher instances, not as a label')
    common.write_json_atomic(manifest_path, manifest)
    return {'windows': len(windows['order']), 'already': done, 'fetched': fetched, 'exceeded_transfer_limit': exceeded,
            'features_total': sum(w['features'] for w in manifest['windows'].values())}


def load(root, spec, window_id):
    """Shapely polygons (source CRS) and properties of the outlines fetched for one window, or None when absent."""
    from shapely.geometry import shape
    path = outlines_dir(root, spec) / f'{window_id}.geojson'
    if not path.exists():
        return None
    rows = []
    for feature in common.read_json(path).get('features', []):
        geometry = shape(feature['geometry'])
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty:
            continue
        rows.append({'geometry': geometry, **feature.get('properties', {})})
    return rows


def to_window_pixels(polygons, native_affine):
    """Outline polygons from CRS metres into native window pixel coordinates (affine E0, px, 0, N0, 0, -px)."""
    from shapely import affinity
    e0, px, _, n0, _, _ = native_affine
    return [affinity.affine_transform(p, (1 / px, 0., 0., -1 / px, -e0 / px, n0 / px)) for p in polygons]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', choices=('fetch',))
    parser.add_argument('--source', default='linz-chc-2025-0075')
    parser.add_argument('--root', default='.')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args(argv)
    result = fetch(args.root, args.source, args.limit)
    json.dump(result, sys.stdout, indent=1, sort_keys=True, default=str)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
