"""Capture a bounded map area with a precise image-to-local-metre frame."""
from __future__ import annotations

import hashlib
import io
import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from .imagery import PROVIDER_MAX_ZOOM, ImageryFetcher
from .tiles import WORLD_SPAN_M, lonlat_to_tile_xy, tile_xy_to_lonlat


def region_frame(bounds, provider='google', *, frame_px=512):
    """The registered frame for a bounds request; frame_px (512, the clicked map-* default, or 1024) is the image side."""
    if provider not in ('google', 'esri'):
        raise ValueError('Choose Google or ESRI aerial imagery')
    if frame_px not in (512, 1024):
        raise ValueError('frame_px must be 512 or 1024')
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4 or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in bounds):
        raise ValueError('Provide west, south, east and north coordinates')
    west, south, east, north = [round(v, 7) for v in bounds]
    if not (-180 < west < east < 180 and -85 < south < north < 85):
        raise ValueError('Choose an area within the supported map bounds')
    left, top = lonlat_to_tile_xy(west, north, 0)
    right, bottom = lonlat_to_tile_xy(east, south, 0)
    cx, cy = (left+right)/2, (top+bottom)/2
    lon, lat = tile_xy_to_lonlat(cx, cy, 0)
    scale = WORLD_SPAN_M * math.cos(math.radians(lat))
    width, height = (right-left)*scale, (bottom-top)*scale
    # Match the API's 2 cm tolerance for seven-decimal coordinate rounding.
    if not (9.98 <= min(width, height) and max(width, height) <= 250.02):
        raise ValueError('Choose an area between 10 and 250 metres on each side')
    # Square context preserves proportions in the 512px review/detector frame.
    span = max(right-left, bottom-top)
    left, top, right, bottom = cx-span/2, cy-span/2, cx+span/2, cy+span/2
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError('The image context crosses the supported map edge')
    west, north = tile_xy_to_lonlat(left, top, 0)
    east, south = tile_xy_to_lonlat(right, bottom, 0)
    # frame_px/256 tile-pixels per frame: 2 for the 512 px frame, so a 1024 px frame keeps the same zoom up to twice the span.
    zoom = min(PROVIDER_MAX_ZOOM[provider], math.ceil(math.log2(frame_px/256/span)))
    span_m = span*scale
    group_x, group_y = map(int, lonlat_to_tile_xy(lon, lat, 17))
    group = f'z17:{group_x}:{group_y}'
    def tile_split(x, y):
        bucket = int(hashlib.sha256(f'z17:{x}:{y}'.encode()).hexdigest()[:8], 16) % 10
        return 'test' if bucket == 0 else 'validation' if bucket == 1 else 'train'
    splits = {tile_split(x, y) for x in range(math.floor(left*2**17), math.ceil(right*2**17))
              for y in range(math.floor(top*2**17), math.ceil(bottom*2**17))}
    region = {'provider': provider, 'requested_bounds': list(bounds), 'bounds': [west, south, east, north],
              'imagery_zoom': zoom, 'span_m': round(span_m, 3),
              'native_meters_per_pixel': scale/(256*2**zoom),
              'spatial_group': group, 'split': next(iter(splits)) if len(splits) == 1 else 'unassigned'}
    # The 512 px identity is unchanged (clicked map-* captures stay byte-identical); a 1024 px frame of the same bounds is another id.
    identity = json.dumps([provider, [round(v, 7) for v in bounds]] if frame_px == 512 else [provider, [round(v, 7) for v in bounds], frame_px])
    half = frame_px//2
    return {'id': 'map-'+hashlib.sha256(identity.encode()).hexdigest()[:16], 'region': region,
            'centroid': {'lon': lon, 'lat': lat},
            'pixel_to_meters': [[span_m/frame_px, 0], [0, -span_m/frame_px], [-span_m/2, span_m/2]],
            'meters_to_pixel': [[frame_px/span_m, 0], [0, -frame_px/span_m], [half, half]]}


def capture_region(bounds, provider='google', *, root=Path('.'), fetcher=None, frame_px=512):
    frame = region_frame(bounds, provider, frame_px=frame_px)
    zoom = frame['region']['imagery_zoom']
    west, south, east, north = frame['region']['bounds']
    left, top = np.array(lonlat_to_tile_xy(west, north, zoom))*256
    right, bottom = np.array(lonlat_to_tile_xy(east, south, zoom))*256
    x0, y0, x1, y1 = math.floor(left/256), math.floor(top/256), math.ceil(right/256), math.ceil(bottom/256)
    tiles = [(x, y) for y in range(y0, y1) for x in range(x0, x1)]
    fetcher = fetcher or ImageryFetcher(provider=provider, cache_dir=root/'data/imagery')
    with ThreadPoolExecutor(max_workers=8) as pool:
        images = list(pool.map(lambda xy: fetcher._child(zoom, *xy), tiles))
    if any(image is None for image in images):
        raise ValueError('Some aerial tiles are unavailable. Retry or choose the other imagery provider.')
    mosaic = Image.new('RGB', ((x1-x0)*256, (y1-y0)*256))
    for (x, y), image in zip(tiles, images, strict=True):
        mosaic.paste(image, ((x-x0)*256, (y-y0)*256))
    image = mosaic.transform((frame_px, frame_px), Image.Transform.EXTENT,
                             (left-x0*256, top-y0*256, right-x0*256, bottom-y0*256), Image.Resampling.BICUBIC)
    output = io.BytesIO()
    image.save(output, format='PNG')
    data = output.getvalue()
    scene = {'schema_version': 1, **frame, 'address_id': None, 'cohort': None,
             'title': f"Map area · {frame['centroid']['lat']:.5f}, {frame['centroid']['lon']:.5f}",
             'image_sha256': hashlib.sha256(data).hexdigest(), 'image_width': frame_px, 'image_height': frame_px,
             'objects': [], 'groups': [], 'scans': {},
             'height_note': 'Heights are class/model estimates, not measured. Dimensions follow the georeferenced outline.'}
    return scene, image, data


def corrected_roofs(scene, image, *, root=Path('.')):
    """Reproject existing CRM outlines and audited roof estimates into a region.

    These remain candidates: a new image/provider can expose registration drift.
    Sharing prior geometry does not transfer approval or measured-height status.
    """
    from shapely.geometry import Polygon, box

    from .scene_extract import polygon_details

    path = root/'public/address-roofs.json'
    if not path.exists():
        return []
    center = scene['centroid']
    origin = np.array(lonlat_to_tile_xy(center['lon'], center['lat'], 0))
    scale = WORLD_SPAN_M * math.cos(math.radians(center['lat']))
    frame = np.array(scene['meters_to_pixel'])
    objects, seen = [], set()
    for record in json.loads(path.read_text()):
        roof = record.get('rooflines', {})
        if record.get('boundary_source') != 'crm' or not roof.get('structure_audit', {}).get('passed') or not roof.get('parts'):
            continue
        source = record['centroid']
        key = (round(source['lon'], 6), round(source['lat'], 6))
        if key in seen:
            continue
        point = np.array(lonlat_to_tile_xy(source['lon'], source['lat'], 0))
        offset = (point-origin)*[scale, -scale]
        ratio = math.cos(math.radians(center['lat']))/math.cos(math.radians(source['lat']))
        def local(points, ratio=ratio, offset=offset):
            return np.array(points)*ratio+offset
        ring = local(record['ring_meters'])
        pixels = np.c_[ring, np.ones(len(ring))] @ frame
        polygon = Polygon(pixels)
        if not polygon.is_valid or not box(0, 0, 512, 512).covers(polygon):
            continue
        seen.add(key)
        parts = [{'ring_meters': local(p.get('ring_meters', p.get('ring'))).tolist(),
                  'ring_z': p.get('ring_z', p.get('z'))} for p in roof['parts']]
        lines = [{**line, 'line_meters': local(line['line_meters']).tolist()} for line in roof.get('lines', [])]
        model = polygon_details([tuple(p) for p in pixels], image, np.array(scene['pixel_to_meters']), 'building')
        model.update(height_m=record['eave_height'], height_source='roof-pipeline-estimate')
        objects.append({'id': f"roof-{record['address_id']}", 'address_id': record['address_id'], 'kind': 'building',
                        'label': record['full_address'].split(',')[0], 'ring': pixels.tolist(), 'status': 'candidate',
                        'geometry_reviewed': False, 'source': 'crm-roof-pipeline', 'model': model,
                        'roof_type': record['roof_type'], 'roof_parts': parts, 'roof_lines': lines})
    return objects
