"""Discover independent local Wayback acquisitions, not just basemap releases."""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlparse

import numpy as np
import requests
from PIL import Image

from .scene_training import atomic_json
from .tiles import lonlat_to_tile_xy

CONFIG = 'https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json'
SERVICE = 'https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer'


# Esri's Wayback services answer 403 to python-requests' default agent; identify as a browser-like client.
USER_AGENT = 'GeoAU-addr/1.0 (+Mozilla/5.0 compatible; roof review imagery dates)'
HEADERS = {'User-Agent': USER_AGENT}


def get_json(url, params, cache):
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
    file = cache/f'{key}.json'
    if file.exists():
        return json.loads(file.read_text())
    response = requests.get(url, params=params, timeout=20, headers=HEADERS)
    response.raise_for_status()
    value = response.json()
    if 'error' in value:
        raise ValueError(f'Imagery metadata unavailable: {value["error"].get("message", "service error")}')
    atomic_json(file, value)
    return value


def acquisition(features):
    """Mixed-date mosaics cannot stand in for one independently dated flight."""
    records = []
    for feature in features:
        a = feature['attributes']
        stamp = a.get('SRC_DATE2')
        if not isinstance(stamp, (int, float)) or stamp <= 0:
            return None
        records.append({'acquisition_date': datetime.fromtimestamp(stamp/1000, UTC).date().isoformat(),
                        'provider': a.get('NICE_DESC'), 'source': a.get('SRC_DESC'),
                        'resolution_m': a.get('SRC_RES'), 'accuracy_m': a.get('SRC_ACC')})
    unique = {json.dumps(r, sort_keys=True) for r in records}
    if len(unique) != 1:
        return None
    result = records[0]
    identity = {k: result[k] for k in ('acquisition_date', 'provider', 'source')}
    result['acquisition_id'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    return result


def discover(scene, cache, count=3):
    # Refresh the public catalogue daily; release metadata is immutable in this cache.
    catalog = get_json(CONFIG, {'day': datetime.now(UTC).date().isoformat()}, cache)
    releases = sorted(catalog, key=lambda k: catalog[k]['itemTitle'], reverse=True)
    center = scene['centroid']
    zoom = min(19, scene['region']['imagery_zoom'])
    x, y = map(int, lonlat_to_tile_xy(center['lon'], center['lat'], zoom))
    index, result, seen, skipped = 0, [], set(), []
    for _ in range(20):
        if index >= len(releases) or len(result) >= count:
            break
        release = releases[index]
        tile = get_json(f'{SERVICE}/tilemap/{release}/{zoom}/{y}/{x}', {}, cache)
        if not tile.get('data', [0])[0]:
            break
        chosen = str(tile.get('select', [release])[0])
        if chosen not in catalog or chosen not in releases[index:]:
            raise ValueError('Imagery archive returned an unexpected local release.')
        index = releases.index(chosen)+1
        item = catalog[chosen]
        url = item.get('metadataLayerUrl', '')
        if urlparse(url).hostname != 'metadata.maptiles.arcgis.com':
            skipped.append({'release': chosen, 'reason': 'Unsupported metadata host'})
            continue
        metadata = get_json(f'{url}/{23-zoom}/query', {
            'f': 'json', 'where': '1=1', 'outFields': 'SRC_DATE2,NICE_DESC,SRC_DESC,SRC_RES,SRC_ACC',
            'geometry': ','.join(map(str, scene['region']['bounds'])), 'geometryType': 'esriGeometryEnvelope',
            'inSR': 4326, 'returnGeometry': 'false', 'spatialRel': 'esriSpatialRelIntersects'}, cache)
        record = acquisition(metadata.get('features', [])) if not metadata.get('exceededTransferLimit') else None
        if not record or record['acquisition_id'] in seen:
            skipped.append({'release': chosen, 'reason': 'Mixed, missing or repeated acquisition metadata'})
            continue
        seen.add(record['acquisition_id'])
        result.append({**record, 'release': chosen, 'release_title': item['itemTitle'], 'metadata_url': url,
                       'imagery_zoom': zoom, 'attribution': 'Esri World Imagery Wayback / '+str(record['provider'])})
    return sorted(result, key=lambda c: c['acquisition_date'], reverse=True), skipped


def capture(scene, record, cache):
    release, zoom = record['release'], record['imagery_zoom']
    if not re.fullmatch(r'\d{1,8}', release):
        raise ValueError('Invalid imagery release')
    west, south, east, north = scene['region']['bounds']
    left, top = np.array(lonlat_to_tile_xy(west, north, zoom))*256
    right, bottom = np.array(lonlat_to_tile_xy(east, south, zoom))*256
    x0, y0, x1, y1 = math.floor(left/256), math.floor(top/256), math.ceil(right/256), math.ceil(bottom/256)
    if not 0 < (x1-x0)*(y1-y0) <= 100:
        raise ValueError('Choose a smaller image context for date comparison.')
    def tile(xy):
        x, y = xy
        file = cache/f'{release}-{zoom}-{x}-{y}.jpg'
        if not file.exists():
            response = requests.get(f'{SERVICE}/tile/{release}/{zoom}/{y}/{x}', timeout=20, headers=HEADERS)
            response.raise_for_status()
            with Image.open(io.BytesIO(response.content)) as image:
                if image.size != (256, 256):
                    raise ValueError('Imagery tile is unavailable at this resolution.')
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(response.content)
        with Image.open(file) as image:
            return image.convert('RGB')
    coords = [(x, y) for y in range(y0, y1) for x in range(x0, x1)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        images = list(pool.map(tile, coords))
    mosaic = Image.new('RGB', ((x1-x0)*256, (y1-y0)*256))
    for (x, y), image in zip(coords, images, strict=True):
        mosaic.paste(image, ((x-x0)*256, (y-y0)*256))
    return mosaic.transform((scene['image_width'], scene['image_height']), Image.Transform.EXTENT,
                            (left-x0*256, top-y0*256, right-x0*256, bottom-y0*256), Image.Resampling.BICUBIC)
