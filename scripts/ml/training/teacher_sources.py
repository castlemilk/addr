"""Teacher imagery sources for the hi-res SAM3 teacher loop (teacher-sources-v1).

The teacher loop (hires_teacher.py) cuts windows the size of a pipeline tile (1152 px at 0.236 m, about 272 m) out of
a fine-pixel orthophoto, runs SAM3 on the native pixels and area-averages the masks onto the student grid. That module
was written around one file (the City of Melbourne 2018 JPEG2000). This module is the registry of every teacher
source and the reader for the open cloud-optimised GeoTIFF sources, so any CC BY orthophoto can become a teacher:

  registry     SOURCES: id -> licence, publisher, CRS, GSD, cache and output folders, window prefix, reader kind
  linz-stac    LINZ's public s3://nz-imagery bucket (CC BY 4.0, COG, EPSG:2193): collection.json lists the 1:500
               tiles; tile bounds follow from the sheet name once one tile per Topo50 sheet has been read
  geotiff      any local georeferenced raster (a drone orthomosaic from OpenDroneMap, a council GeoTIFF)

Stages, each resumable, all in the training venv (rasterio):

  catalog   read the STAC collection, index the tiles, fit sheet origins          -> cache/catalog.json
  select    seeded sample of window cells over a bbox, classified from overviews  -> teacher/windows.json
  fetch     download the tiles the selected windows touch, verify checksums       -> cache/tiles/
  crop      mosaic each window at native GSD, alpha must be full                  -> cache/windows/, teacher/crops.json

After `crop`, hires_teacher runs unchanged with `--source <id>`: calibrate | teach | finalize | measure, then
distill_dataset with the same flag. Nothing here writes under data/scenes or the eyes index; masks are model outputs,
never labels from people; every comparison is agreement between two model runs, never accuracy.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np

from . import common

VERSION = 'teacher-sources-v1'
STUDENT_PX = 1152
PIPELINE_PX_M = 0.23595384232187258     # saved scan tiles: z19 pixel at Mooroolbark (latest.json pixel_to_meters)
DEFAULT_N = 60
DEFAULT_SEED = 20260928
DEFAULT_CANDIDATES = 400                # window cells sampled for overview classification before the rule is applied
OVERVIEW_M = 1.                         # metres per overview pixel used for the selection rule
NZ_BUCKET = 'nz-imagery'
NZ_LICENCE = {'licence': 'CC BY 4.0', 'licence_url': 'https://creativecommons.org/licenses/by/4.0/',
              'attribution_owner': 'Toitu Te Whenua Land Information New Zealand (LINZ)'}

# Selection rule: hires_teacher.OVERVIEW_RULE plus a vegetation floor so leafy suburbs win over CBD and industrial
# land (the point of these sources is housing like Mooroolbark's). 'reduce' is the overview factor relative to the
# native pixel that the 1 m classification pixel implies; hires_teacher.finalize reads it back from windows.json.
SUBURB_RULE = {'nodata_alpha_min': 255, 'inside_min': .995, 'water_std_max': 3., 'water_g_minus_r': 15.,
               'water_b_minus_r': 10., 'water_max': .05, 'exg_veg': .06, 'veg_min': .08, 'veg_max': .45,
               'edge_grad_min': 15., 'structure_min': .30, 'structure_max': .75}

SOURCES = {
    'com-2018': {
        'id': 'com-2018', 'kind': 'com-jp2', 'title': 'City of Melbourne 2018 Aerial Imagery (True Ortho)',
        'publisher': 'City of Melbourne', 'place': 'Melbourne CBD and inner suburbs, VIC, Australia', 'capture': 'May 2018',
        'gsd_m': 0.1, 'crs': 'EPSG:28355', 'licence': 'CC BY 4.0', 'licence_url': 'https://creativecommons.org/licenses/by/4.0/',
        'url': 'https://s3-ap-southeast-2.amazonaws.com/opendata-aerial-imagery/mga55_gda94_10cm_2018_CoM_true_ortho.zip',
        'cache': 'data/imagery/com-2018', 'out': 'data/reviews/eyes/training/distill-v1', 'window_prefix': 'com2018',
        'version': 'hires-teacher-com-2018-v1', 'provenance_tag': 'hires-teacher-com-2018',
        'similarity': 'medium (inner-city terraces, apartments, industrial roofs)',
        'grid_note': 'MGA grid north: about 1.3 degrees from true north here; grid scale factor about 1.0000, so grid metres '
                     'are ground metres to 1e-4',
    },
    'linz-chc-2025-0075': {
        'id': 'linz-chc-2025-0075', 'kind': 'linz-stac', 'title': 'Christchurch 0.075m Urban Aerial Photos (2025)',
        'publisher': 'Toitu Te Whenua Land Information New Zealand', 'place': 'Christchurch city and suburbs, New Zealand',
        'capture': 'March 2025', 'gsd_m': 0.075, 'crs': 'EPSG:2193', **NZ_LICENCE,
        'url': f's3://{NZ_BUCKET}/canterbury/christchurch_2025_0.075m/rgb/2193/',
        'catalogue': 'https://data.linz.govt.nz/layer/', 'cache': 'data/imagery/linz-chc-2025-0075',
        'out': 'data/reviews/eyes/training/distill-linz-chc-2025-0075', 'window_prefix': 'chc25a',
        'version': 'hires-teacher-linz-chc-2025-0075-v1', 'provenance_tag': 'hires-teacher-linz-chc-2025-0075',
        'similarity': 'high (detached 1950s-2010s housing on quarter-acre and infill lots, tiled and metal roofs)',
        'grid_note': 'NZTM2000 grid north: about 1.5 degrees from true north at Christchurch; grid scale factor 0.9996 '
                     'at the central meridian and about 1.0002 here, so grid metres are ground metres to 2e-4',
        # whole-city bbox excluding the CBD square and the port hills; overridden with --bbox
        'default_bbox_lonlat': [172.50, -43.58, 172.72, -43.47],
        'exclude_bbox_lonlat': [[172.615, -43.545, 172.655, -43.520]],
    },
    'linz-chc-2025-005': {
        'id': 'linz-chc-2025-005', 'kind': 'linz-stac', 'title': 'Christchurch 0.05m Urban Aerial Photos (2025)',
        'publisher': 'Toitu Te Whenua Land Information New Zealand', 'place': 'Christchurch central and inner suburbs, New Zealand',
        'capture': 'March 2025', 'gsd_m': 0.05, 'crs': 'EPSG:2193', **NZ_LICENCE,
        'url': f's3://{NZ_BUCKET}/canterbury/christchurch_2025_0.05m/rgb/2193/',
        'catalogue': 'https://data.linz.govt.nz/layer/', 'cache': 'data/imagery/linz-chc-2025-005',
        'out': 'data/reviews/eyes/training/distill-linz-chc-2025-005', 'window_prefix': 'chc25b',
        'version': 'hires-teacher-linz-chc-2025-005-v1', 'provenance_tag': 'hires-teacher-linz-chc-2025-005',
        'similarity': 'medium-high (inner suburbs around the CBD: villas, townhouses, some commercial)',
        'grid_note': 'NZTM2000 grid north: about 1.5 degrees from true north at Christchurch; grid scale factor about '
                     '1.0002 here, so grid metres are ground metres to 2e-4',
        'default_bbox_lonlat': [172.604, -43.547, 172.662, -43.513],
        'exclude_bbox_lonlat': [[172.625, -43.540, 172.648, -43.525]],
    },
    'linz-chc-2020-0075': {
        'id': 'linz-chc-2020-0075', 'kind': 'linz-stac', 'title': 'Christchurch 0.075m Urban Aerial Photos (2020-2021)',
        'publisher': 'Toitu Te Whenua Land Information New Zealand', 'place': 'Christchurch city and suburbs, New Zealand',
        'capture': '2020-2021', 'gsd_m': 0.075, 'crs': 'EPSG:2193', **NZ_LICENCE,
        'url': f's3://{NZ_BUCKET}/canterbury/christchurch_2020-2021_0.075m/rgb/2193/',
        'catalogue': 'https://data.linz.govt.nz/layer/', 'cache': 'data/imagery/linz-chc-2020-0075',
        'out': 'data/reviews/eyes/training/distill-linz-chc-2020-0075', 'window_prefix': 'chc20',
        'version': 'hires-teacher-linz-chc-2020-0075-v1', 'provenance_tag': 'hires-teacher-linz-chc-2020-0075',
        'similarity': 'high (same housing stock as the 2025 capture; a second season for cross-capture checks)',
        'grid_note': 'NZTM2000 grid north: about 1.5 degrees from true north at Christchurch; grid scale factor about '
                     '1.0002 here, so grid metres are ground metres to 2e-4',
        'default_bbox_lonlat': [172.50, -43.58, 172.72, -43.47],
        'exclude_bbox_lonlat': [[172.615, -43.545, 172.655, -43.520]],
    },
    'linz-chc-2021-005': {
        'id': 'linz-chc-2021-005', 'kind': 'linz-stac', 'title': 'Christchurch 0.05m Urban Aerial Photos (2021)',
        'publisher': 'Toitu Te Whenua Land Information New Zealand', 'place': 'Christchurch, New Zealand', 'capture': '2021',
        'gsd_m': 0.05, 'crs': 'EPSG:2193', **NZ_LICENCE, 'url': f's3://{NZ_BUCKET}/canterbury/christchurch_2021_0.05m/rgb/2193/',
        'catalogue': 'https://data.linz.govt.nz/layer/', 'cache': 'data/imagery/linz-chc-2021-005',
        'out': 'data/reviews/eyes/training/distill-linz-chc-2021-005', 'window_prefix': 'chc21',
        'version': 'hires-teacher-linz-chc-2021-005-v1', 'provenance_tag': 'hires-teacher-linz-chc-2021-005',
        'similarity': 'high', 'grid_note': 'NZTM2000 grid north: about 1.5 degrees from true north at Christchurch',
        'default_bbox_lonlat': [172.50, -43.58, 172.72, -43.47], 'exclude_bbox_lonlat': [[172.615, -43.545, 172.655, -43.520]],
    },
    'linz-akl-2024-0075': {
        'id': 'linz-akl-2024-0075', 'kind': 'linz-stac', 'title': 'Auckland 0.075m Urban Aerial Photos (2024-2025)',
        'publisher': 'Toitu Te Whenua Land Information New Zealand', 'place': 'Auckland, New Zealand', 'capture': '2024-2025',
        'gsd_m': 0.075, 'crs': 'EPSG:2193', **NZ_LICENCE, 'url': f's3://{NZ_BUCKET}/auckland/auckland_2024_0.075m/rgb/2193/',
        'catalogue': 'https://data.linz.govt.nz/layer/', 'cache': 'data/imagery/linz-akl-2024-0075',
        'out': 'data/reviews/eyes/training/distill-linz-akl-2024-0075', 'window_prefix': 'akl24',
        'version': 'hires-teacher-linz-akl-2024-0075-v1', 'provenance_tag': 'hires-teacher-linz-akl-2024-0075',
        'similarity': 'high (detached and infill housing; 4.9 TB, sample by suburb)',
        'grid_note': 'NZTM2000 grid north: about 0.5 degrees from true north at Auckland',
        'default_bbox_lonlat': [174.70, -36.95, 174.90, -36.85], 'exclude_bbox_lonlat': [[174.755, -36.860, 174.780, -36.840]],
    },
    'linz-wlg-2025-0075': {
        'id': 'linz-wlg-2025-0075', 'kind': 'linz-stac', 'title': 'Wellington 0.075m Urban Aerial Photos (2025)',
        'publisher': 'Toitu Te Whenua Land Information New Zealand', 'place': 'Wellington, New Zealand', 'capture': 'October 2025',
        'gsd_m': 0.075, 'crs': 'EPSG:2193', **NZ_LICENCE, 'url': f's3://{NZ_BUCKET}/wellington/wellington_2025_0.075m/rgb/2193/',
        'catalogue': 'https://data.linz.govt.nz/layer/', 'cache': 'data/imagery/linz-wlg-2025-0075',
        'out': 'data/reviews/eyes/training/distill-linz-wlg-2025-0075', 'window_prefix': 'wlg25',
        'version': 'hires-teacher-linz-wlg-2025-0075-v1', 'provenance_tag': 'hires-teacher-linz-wlg-2025-0075',
        'similarity': 'medium-high (hilly suburbs: more lean and occlusion)',
        'grid_note': 'NZTM2000 grid north: about 1.0 degree from true north at Wellington',
        'default_bbox_lonlat': [174.72, -41.35, 174.85, -41.25], 'exclude_bbox_lonlat': [[174.770, -41.295, 174.785, -41.275]],
    },
    'com-2020-2cm': {
        'id': 'com-2020-2cm', 'kind': 'geotiff', 'title': 'City of Melbourne 2020 Aerial Imagery (True Ortho, 2 cm)',
        'publisher': 'City of Melbourne', 'place': 'Melbourne CBD and inner suburbs, VIC, Australia', 'capture': 'May 2020',
        'gsd_m': 0.019, 'crs': 'EPSG:7855', 'licence': 'CC BY 4.0', 'licence_url': 'https://creativecommons.org/licenses/by/4.0/',
        'url': 'https://opendata-aerial-imagery.s3.ap-southeast-2.amazonaws.com/Taylors_May2020_2cm_GDA2020z55.jp2',
        'catalogue': 'https://data.melbourne.vic.gov.au/explore/dataset/2020-aerial-imagery-true-ortho/',
        'cache': 'data/imagery/com-2020-2cm', 'out': 'data/reviews/eyes/training/distill-com-2020-2cm', 'window_prefix': 'com2020',
        'version': 'hires-teacher-com-2020-2cm-v1', 'provenance_tag': 'hires-teacher-com-2020-2cm',
        'similarity': 'medium (same municipality as com-2018, two years later, five times finer)',
        'teacher_gsd_m': 0.05, 'remote': True,
        'grid_note': 'MGA2020 zone 55 grid north: about 1.3 degrees from true north here',
        'default_bbox_lonlat': [144.92, -37.82, 144.99, -37.77], 'exclude_bbox_lonlat': [[144.950, -37.822, 144.975, -37.805]],
    },
    'act-2023-urban-75mm': {
        'id': 'act-2023-urban-75mm', 'kind': 'arcgis-image', 'title': 'ACT Aerial Imagery 2023-09 urban 75 mm (ACTmapi ImageServer)',
        'publisher': 'ACT Government (ACTmapi) and Aerometrex', 'place': 'Canberra urban area, ACT, Australia', 'capture': 'September 2023',
        'gsd_m': 0.075, 'crs': 'EPSG:7855', 'licence': 'CC BY 4.0 (Australian Capital Territory and Aerometrex)',
        'licence_url': 'https://www.actmapi.act.gov.au/terms-and-conditions',
        'url': 'https://data4.actmapi.act.gov.au/arcgis/rest/services/ACT_IMAGERY_MGA2020/2023_09_urban_75mm/ImageServer',
        'catalogue': 'https://data4.actmapi.act.gov.au/arcgis/rest/services/ACT_IMAGERY_MGA2020',
        'cache': 'data/imagery/act-2023-urban-75mm', 'out': 'data/reviews/eyes/training/distill-act-2023-urban-75mm',
        'window_prefix': 'act23', 'version': 'hires-teacher-act-2023-urban-75mm-v1', 'provenance_tag': 'hires-teacher-act-2023-urban-75mm',
        'similarity': 'high (Australian detached suburbs; ACT LiDAR 2020 CC BY 4.0 pairs for heights)',
        'grid_note': 'MGA2020 zone 55 grid north: about 1.6 degrees from true north at Canberra',
        'default_bbox_lonlat': [149.02, -35.42, 149.20, -35.20], 'exclude_bbox_lonlat': [[149.120, -35.290, 149.140, -35.270]],
    },
    'drone-ortho': {
        'id': 'drone-ortho', 'kind': 'geotiff', 'title': 'Own drone orthomosaic (OpenDroneMap odm_orthophoto.tif)',
        'publisher': 'addr project', 'place': 'set per capture (27 Cambridge Road, Mooroolbark first)', 'capture': 'per file',
        'gsd_m': None, 'crs': None, 'licence': 'own capture', 'licence_url': None, 'url': None, 'teacher_gsd_m': 0.05,
        'status': 'registered; select needs a raster at least two 272 m windows wide (no lot-sized window mode yet)',
        'cache': 'data/imagery/drone-ortho', 'out': 'data/reviews/eyes/training/distill-drone-ortho', 'window_prefix': 'drone',
        'version': 'hires-teacher-drone-ortho-v1', 'provenance_tag': 'hires-teacher-drone-ortho',
        'similarity': 'exact (the pipeline suburb itself)', 'grid_note': 'orthomosaic CRS as written by OpenDroneMap',
    },
}

TILE_RE = re.compile(r'^(?P<sheet>[A-Z]{2}\d{2})_(?P<scale>\d+)_(?P<row>\d{3})(?P<col>\d{3})$')
SHEET_E_M, SHEET_N_M = 24000., 36000.   # Topo50 sheet size in NZTM metres
TILE_E_M, TILE_N_M = 240., 360.         # 1:500 tile size (100 x 100 tiles per sheet)


# --- registry helpers ----------------------------------------------------------------------------------------------

def source(source_id):
    try:
        return SOURCES[source_id]
    except KeyError:
        raise SystemExit(f'unknown teacher source {source_id!r}; known: {", ".join(sorted(SOURCES))}') from None


def cache_dir(root, spec):
    return Path(root) / spec['cache']


def teacher_dir(root, spec):
    return Path(root) / spec['out'] / 'teacher'


def native_window_px(gsd_m, student_px=STUDENT_PX, student_m=PIPELINE_PX_M):
    return int(round(student_px * student_m / gsd_m))


def _num(value, digits=4):
    return None if value is None else round(float(value), digits)


# --- LINZ tile naming ----------------------------------------------------------------------------------------------

def parse_tile_name(name):
    """('BX24', 500, 23, 18) from 'BX24_500_023018' (row from the sheet's top edge, col from its left edge, 1-based)."""
    match = TILE_RE.match(Path(name).stem)
    if not match or int(match['scale']) != 500:      # only the 1:500 tile grid (240 x 360 m) is modelled here
        return None
    return match['sheet'], int(match['scale']), int(match['row']), int(match['col'])


def sheet_origin_from_tile(name, left, top):
    """(sheet_left, sheet_top) implied by one tile's bounds; every other tile of the sheet follows from the name."""
    parsed = parse_tile_name(name)
    if parsed is None:
        raise ValueError(f'not a LINZ 1:500 tile name: {name}')
    _, _, row, col = parsed
    return left - (col - 1) * TILE_E_M, top + (row - 1) * TILE_N_M


def tile_bounds(name, sheet_origins):
    """(left, bottom, right, top) in NZTM metres from the name and the sheet's fitted origin."""
    sheet, _, row, col = parse_tile_name(name)
    origin_left, origin_top = sheet_origins[sheet]
    left = origin_left + (col - 1) * TILE_E_M
    top = origin_top - (row - 1) * TILE_N_M
    return left, top - TILE_N_M, left + TILE_E_M, top


def multihash_sha256(value):
    """Hex sha256 from a STAC file:checksum multihash (0x12 sha2-256, 0x20 length) or None."""
    if not value or not value.startswith('1220') or len(value) != 68:
        return None
    return value[4:]


# --- geodesy (rasterio/pyproj live in the training venv) -----------------------------------------------------------

def to_lonlat(crs, xs, ys):
    from rasterio.warp import transform
    lon, lat = transform(crs, 'EPSG:4326', list(xs), list(ys))
    return list(lon), list(lat)


def from_lonlat(crs, lons, lats):
    from rasterio.warp import transform
    xs, ys = transform('EPSG:4326', crs, list(lons), list(lats))
    return list(xs), list(ys)


def bbox_to_crs(crs, bbox_lonlat):
    """(minx, miny, maxx, maxy) in the source CRS enclosing a lon/lat bbox."""
    w, s, e, n = bbox_lonlat
    xs, ys = from_lonlat(crs, [w, e, e, w], [s, s, n, n])
    return min(xs), min(ys), max(xs), max(ys)


def bbox_contains(bbox, x, y):
    w, s, e, n = bbox
    return w <= x <= e and s <= y <= n


# --- catalog -------------------------------------------------------------------------------------------------------

S3_REGION = 'ap-southeast-2'


def s3_client():
    """Anonymous HTTPS access to a public bucket (no boto3): returns a small object with get_json/download."""
    return _AnonymousS3()


class _AnonymousS3:
    def url(self, bucket, key):
        return f'https://{bucket}.s3.{S3_REGION}.amazonaws.com/{key}'

    def get_bytes(self, bucket, key, attempts=4):
        import urllib.request
        last = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(self.url(bucket, key), timeout=120) as response:
                    return response.read()
            except Exception as error:  # noqa: BLE001 - retried, then raised
                last = error
                time.sleep(1.5 * (attempt + 1))
        raise last

    def get_json(self, bucket, key):
        return json.loads(self.get_bytes(bucket, key))

    def download_file(self, bucket, key, path):
        data = self.get_bytes(bucket, key)
        tmp = Path(str(path) + '.part')
        tmp.write_bytes(data)
        tmp.replace(path)


def s3_parts(url):
    assert url.startswith('s3://')
    bucket, _, key = url[5:].partition('/')
    return bucket, key


def s3_json(client, bucket, key):
    return client.get_json(bucket, key)


def tile_path(root, spec, cat, name):
    """Local path of a tile: the registered raster itself for local sources, else the fetched copy."""
    tile = cat['tiles'][name]
    if tile.get('local'):
        return Path(tile['key'])
    return cache_dir(root, spec) / 'tiles' / f'{name}.tiff'


GDAL_ENV = {'AWS_NO_SIGN_REQUEST': 'YES', 'GDAL_DISABLE_READDIR_ON_OPEN': 'EMPTY_DIR', 'CPL_VSIL_CURL_USE_HEAD': 'NO',
            'GDAL_HTTP_MULTIRANGE': 'YES', 'GDAL_HTTP_MERGE_CONSECUTIVE_RANGES': 'YES', 'VSI_CACHE': 'TRUE',
            'VSI_CACHE_SIZE': str(64 << 20), 'GDAL_HTTP_TIMEOUT': '120', 'GDAL_HTTP_MAX_RETRY': '3', 'GDAL_HTTP_RETRY_DELAY': '2'}


def gdal_path(path_or_url):
    """rasterio-openable path: /vsis3 for s3://, /vsicurl for http(s)://, else the path itself."""
    text = str(path_or_url)
    if text.startswith('s3://'):
        return '/vsis3/' + text[5:]
    if text.startswith(('http://', 'https://')):
        return '/vsicurl/' + text
    return text


def effective_gsd(spec):
    """Pixel the teacher is read at: finer sources are area-averaged to teacher_gsd_m (SAM3 cost grows with px^2)."""
    return spec.get('teacher_gsd_m') or spec['gsd_m']


def read_header(path_or_url):
    """Bounds, size and GSD of a raster; S3 URLs are read unsigned through GDAL's /vsis3, http(s) through /vsicurl."""
    import rasterio
    with rasterio.Env(**GDAL_ENV), rasterio.open(gdal_path(path_or_url)) as ds:
        left, bottom, right, top = ds.bounds
        return {'left': left, 'bottom': bottom, 'right': right, 'top': top, 'width': ds.width, 'height': ds.height,
                'gsd_x_m': abs(ds.res[0]), 'gsd_y_m': abs(ds.res[1]), 'crs': str(ds.crs), 'count': ds.count,
                'dtype': ds.dtypes[0], 'overviews': ds.overviews(1), 'compress': ds.profile.get('compress')}


def catalog(root, spec, limit=None):
    """Catalogue by kind: LINZ STAC tiles, an ArcGIS ImageServer, or a (remote) single raster named in the registry."""
    if spec['kind'] == 'arcgis-image':
        root = Path(root)
        folder = cache_dir(root, spec)
        folder.mkdir(parents=True, exist_ok=True)
        result = arcgis_catalog_from_info(spec, arcgis_service_info(spec['url']))
        common.write_json_atomic(folder / 'catalog.json', result)
        common.write_json_atomic(folder / 'provenance.json', {
            **{k: spec.get(k) for k in ('id', 'title', 'publisher', 'place', 'capture', 'gsd_m', 'crs', 'licence', 'licence_url', 'url')},
            'service_copyright': result['service'].get('copyrightText'),
            'attribution': f"{spec['title']}, {spec['publisher']}, {spec['licence']}. Changes: exported at the teacher pixel in "
                           f"{STUDENT_PX} px windows and area-averaged to {PIPELINE_PX_M:.3f} m; building masks are SAM3 model outputs.",
            'catalog_at': result['fetched_at']})
        return {k: v for k, v in result.items() if k != 'tiles'}
    if spec['kind'] == 'geotiff':
        if not spec.get('url'):
            raise SystemExit(f"{spec['id']} is a local raster source: run `register --path FILE`")
        return register_geotiff(root, spec, spec['url'], spec.get('capture'), spec.get('place'))
    if spec['kind'] != 'linz-stac':
        raise SystemExit(f"catalog is for linz-stac, arcgis-image or geotiff sources; {spec['id']} is {spec['kind']}")
    root = Path(root)
    folder = cache_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    client = s3_client()
    bucket, prefix = s3_parts(spec['url'])
    collection = s3_json(client, bucket, prefix + 'collection.json')
    items = [link for link in collection.get('links', []) if link.get('rel') == 'item']
    if limit:
        items = items[:limit]
    tiles, sheets, checked = {}, {}, []
    for link in items:
        name = Path(link['href']).stem
        parsed = parse_tile_name(name)
        if parsed is None:
            continue
        sheet = parsed[0]
        if sheet not in sheets:
            header = read_header(f's3://{bucket}/{prefix}{name}.tiff')
            sheets[sheet] = list(sheet_origin_from_tile(name, header['left'], header['top']))
            checked.append({'tile': name, 'header': header})
        tiles[name] = {'key': f'{prefix}{name}.tiff', 'item_key': f'{prefix}{name}.json', 'sheet': sheet,
                       'item_checksum': link.get('file:checksum')}
    for name, tile in tiles.items():
        tile['bounds'] = list(tile_bounds(name, sheets))
    # verify the name-derived bounds against a second header per sheet (the first tile of each sheet in reverse order)
    verify = {}
    for sheet in sheets:
        candidates = [n for n, t in tiles.items() if t['sheet'] == sheet]
        name = candidates[-1]
        header = read_header(f's3://{bucket}/{prefix}{name}.tiff')
        predicted = tiles[name]['bounds']
        verify[sheet] = {'tile': name, 'max_abs_error_m': max(abs(header['left'] - predicted[0]), abs(header['top'] - predicted[3])),
                         'gsd_m': header['gsd_x_m'], 'size_px': [header['width'], header['height']]}
        if verify[sheet]['max_abs_error_m'] > .01:
            raise SystemExit(f'tile grid mismatch on sheet {sheet}: {verify[sheet]}')
    extent = collection.get('extent', {})
    result = {'version': VERSION, 'source': spec['id'], 'title': collection.get('title'), 'license': collection.get('license'),
              'description': collection.get('description'), 'extent': extent, 'crs': spec['crs'], 'gsd_m': spec['gsd_m'],
              'bucket': bucket, 'prefix': prefix, 'n_tiles': len(tiles), 'sheets': sheets, 'sheet_checks': verify,
              'headers_read': checked, 'tile_m': [TILE_E_M, TILE_N_M], 'tiles': tiles, 'fetched_at': common.now_iso()}
    common.write_json_atomic(folder / 'catalog.json', result)
    common.write_json_atomic(folder / 'provenance.json', {
        **{k: spec[k] for k in ('id', 'title', 'publisher', 'place', 'capture', 'gsd_m', 'crs', 'licence', 'licence_url', 'url')},
        'collection_license': collection.get('license'), 'bucket_license': f's3://{bucket}/LICENSE',
        'attribution': f"Sourced from {spec['publisher']} and licensed for reuse under CC BY 4.0: {collection.get('title')}. "
                       f"Changes: cropped to {STUDENT_PX} px windows at the pipeline's tile size and area-averaged to "
                       f"{PIPELINE_PX_M:.3f} m; building masks are SAM3 model outputs.",
        'catalog_at': result['fetched_at']})
    return {k: v for k, v in result.items() if k not in ('tiles', 'headers_read')}


def load_catalog(root, spec):
    path = cache_dir(root, spec) / 'catalog.json'
    if not path.exists():
        raise SystemExit(f'no catalog for {spec["id"]}: run `catalog` first')
    return common.read_json(path)


# --- selection -----------------------------------------------------------------------------------------------------

def tiles_covering(cat, bounds):
    """Names of catalogued tiles that intersect (left, bottom, right, top)."""
    w, s, e, n = bounds
    hits = []
    for name, tile in cat['tiles'].items():
        tl, tb, tr, tt = tile['bounds']
        if tl < e and tr > w and tb < n and tt > s:
            hits.append(name)
    return sorted(hits)


def fully_covered(cat, bounds):
    """True when the union of intersecting tiles covers the window (tiles are an axis-aligned grid, so area suffices)."""
    w, s, e, n = bounds
    covered = 0.
    for name in tiles_covering(cat, bounds):
        tl, tb, tr, tt = cat['tiles'][name]['bounds']
        covered += max(0., min(e, tr) - max(w, tl)) * max(0., min(n, tt) - max(s, tb))
    return covered >= (e - w) * (n - s) - 1e-6


def window_grid(bounds_crs, window_m):
    """Origin-aligned cell lattice over a CRS bbox: (rows, cols, origin_x, origin_top)."""
    w, s, e, n = bounds_crs
    origin_x = math.floor(w / window_m) * window_m
    origin_top = math.ceil(n / window_m) * window_m
    cols = int(math.ceil((e - origin_x) / window_m))
    rows = int(math.ceil((origin_top - s) / window_m))
    return rows, cols, origin_x, origin_top


def cell_bounds(row, col, origin_x, origin_top, window_m):
    left = origin_x + col * window_m
    top = origin_top - row * window_m
    return left, top - window_m, left + window_m, top


def window_id(prefix, row, col):
    return f'{prefix}-r{row:03d}-c{col:03d}'


def classify_window(rgba, rule=SUBURB_RULE):
    """hires_teacher's overview classes on one window's low-resolution RGBA mosaic, as fractions."""
    from .hires_teacher import classify_overview
    rgba = np.asarray(rgba)
    inside_alpha = rgba[..., 3] >= rule['nodata_alpha_min']
    masks = classify_overview(np.dstack([rgba[..., :3], np.where(inside_alpha, 255, 0).astype(np.uint8)]),
                              {**rule, 'nodata_rgb': (-1000, -1000, -1000), 'nodata_tol': 0.})
    inside = float(masks['inside'].mean())
    valid = max(int(masks['inside'].sum()), 1)
    stats = {'inside': round(inside, 4), 'veg': round(float(masks['veg'].sum()) / valid, 4),
             'water': round(float(masks['water'].sum()) / valid, 4), 'structure': round(float(masks['structure'].sum()) / valid, 4)}
    reasons = []
    if inside < rule['inside_min']:
        reasons.append('outside_imagery')
    if stats['veg'] > rule['veg_max']:
        reasons.append('vegetation')
    if stats['veg'] < rule['veg_min']:
        reasons.append('bare_or_commercial')
    if stats['water'] > rule['water_max']:
        reasons.append('water')
    if stats['structure'] < rule['structure_min']:
        reasons.append('low_structure')
    if stats['structure'] > rule['structure_max']:
        reasons.append('dense_or_industrial')
    stats['eligible'] = not reasons
    stats['reasons'] = reasons
    return stats


def read_mosaic(paths, bounds, res, nodata=0, resampling='average'):
    """RGBA mosaic of the rasters over (left, bottom, right, top) at `res` metres per pixel (rasterio.merge).

    COG access rules (GDAL_ENV): no bucket listing on open (a 16k-object prefix costs 17 paged requests per file
    otherwise), anonymous S3, cached range reads. Average resampling makes a coarser teacher pixel an exact area mean;
    at the native pixel it is the identity."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.merge import merge
    left, bottom, right, top = bounds
    height, width = int(round((top - bottom) / res)), int(round((right - left) / res))
    with rasterio.Env(**GDAL_ENV):
        datasets = [rasterio.open(gdal_path(p)) for p in paths]
        try:
            count = min(ds.count for ds in datasets)
            indexes = [1, 2, 3, 4] if count >= 4 else [1, 2, 3]
            # rasterio.merge rounds each tile window (floor of offset + 0.1, floor of width + 0.5) and can leave the last
            # row or column of the requested box unfilled; pad one pixel east and south and cut back afterwards
            array, transform = merge(datasets, bounds=(left, bottom - res, right + res, top), res=(res, res), nodata=nodata,
                                     indexes=indexes, resampling=Resampling[resampling], masked=count < 4)
        finally:
            for ds in datasets:
                ds.close()
    if count < 4:                # no alpha band: derive it from the dataset masks (nodata / mask band), not a constant
        alpha = np.where(np.ma.getmaskarray(array).all(axis=0), 0, 255).astype(np.uint8)
        array = np.dstack([np.moveaxis(np.ma.filled(array, nodata), 0, -1), alpha])
    else:
        array = np.moveaxis(array, 0, -1)
    return array[:height, :width], transform


def arcgis_service_info(url):
    import urllib.request
    with urllib.request.urlopen(f'{url}?f=json', timeout=60) as response:
        return json.loads(response.read())


def arcgis_catalog_from_info(spec, info):
    """One pseudo-tile covering the service extent; pixel size and export limits recorded."""
    extent = info['extent']
    wkid = extent.get('spatialReference', {}).get('latestWkid') or extent.get('spatialReference', {}).get('wkid')
    bounds = [extent['xmin'], extent['ymin'], extent['xmax'], extent['ymax']]
    return {'version': VERSION, 'source': spec['id'], 'title': info.get('name') or spec['title'], 'crs': f'EPSG:{wkid}' if wkid else spec['crs'],
            'gsd_m': round(float(info.get('pixelSizeX', spec['gsd_m'] or 0)), 6) or spec['gsd_m'], 'bucket': None, 'prefix': None,
            'n_tiles': 1, 'sheets': {}, 'header': {'left': bounds[0], 'bottom': bounds[1], 'right': bounds[2], 'top': bounds[3]},
            'export_limits': {'max_width': info.get('maxImageWidth'), 'max_height': info.get('maxImageHeight')},
            'service': {k: info.get(k) for k in ('name', 'description', 'copyrightText', 'bandCount', 'pixelType', 'pixelSizeX', 'pixelSizeY',
                                                  'minScale', 'maxScale', 'capabilities')},
            'tiles': {'service': {'key': spec['url'], 'arcgis': True, 'bounds': bounds}}, 'fetched_at': common.now_iso()}


ARCGIS_MAX_EXPORT_PX = 1812      # the ACT server answered 500 for a 3624 px TIFF and 200 for 1812 and 2048 px (2026-09-28)


def _arcgis_export_one(url, bounds, size_px, epsg, fmt):
    import urllib.parse
    import urllib.request

    import rasterio
    from rasterio.io import MemoryFile
    w, s_, e, n = bounds
    query = urllib.parse.urlencode({'bbox': f'{w},{s_},{e},{n}', 'bboxSR': epsg, 'imageSR': epsg, 'size': f'{size_px},{size_px}',
                                    'format': fmt, 'f': 'image'})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f'{url}/exportImage?{query}', timeout=300) as response:
                data = response.read()
            break
        except Exception as error:  # noqa: BLE001 - retried, then raised
            last = error
            time.sleep(3 * (attempt + 1))
    else:
        raise last
    with rasterio.Env(**GDAL_ENV), MemoryFile(data) as memory, memory.open() as ds:
        count = min(ds.count, 4)
        array = ds.read(list(range(1, count + 1)))
        mask = ds.dataset_mask()
        transform = ds.transform
    array = np.moveaxis(array, 0, -1)
    if array.shape[-1] == 3:     # 3-band services: alpha from the dataset mask (nodata / mask band), not a constant
        alpha = np.where(mask > 0, 255, 0).astype(array.dtype)
        # a rendered export with no mask band reports every pixel valid; treat pure black as no-data too
        alpha[(array == 0).all(axis=-1)] = 0
        array = np.dstack([array, alpha])
    return array, transform


def arcgis_export(url, bounds, size_px, epsg, fmt='tiff', max_px=ARCGIS_MAX_EXPORT_PX):
    """RGBA array of `bounds` (source CRS) rendered by the ImageServer at size_px x size_px.

    Exports larger than the server tolerates are split into a k x k grid of equal sub-boxes and stitched; the
    transform is that of the top-left piece."""
    k = max(1, int(math.ceil(size_px / max_px)))
    if k == 1:
        return _arcgis_export_one(url, bounds, size_px, epsg, fmt)
    w, s_, e, n = bounds
    res = (e - w) / size_px
    edges = [int(round(j * size_px / k)) for j in range(k + 1)]     # integer pixel edges: pieces tile the window exactly
    rows = []
    transform = None
    for i in range(k):
        cols = []
        for j in range(k):
            sub = (w + edges[j] * res, n - edges[i + 1] * res, w + edges[j + 1] * res, n - edges[i] * res)
            piece_px = edges[j + 1] - edges[j]
            array, t = _arcgis_export_one(url, sub, piece_px, epsg, fmt)
            transform = transform or t
            cols.append(array[:edges[i + 1] - edges[i], :piece_px])
        rows.append(np.concatenate(cols, axis=1))
    return np.concatenate(rows, axis=0), transform


def read_window(root, spec, cat, bounds, res, names, remote):
    """RGBA of one window at `res` m/px from whichever reader the source kind needs."""
    if spec['kind'] == 'arcgis-image':
        size_px = int(round((bounds[2] - bounds[0]) / res))
        epsg = int(str(cat.get('crs') or spec['crs']).split(':')[-1])
        return arcgis_export(spec['url'], bounds, size_px, epsg)
    if spec['kind'] == 'linz-stac' and remote:
        paths = [f"s3://{cat['bucket']}/{cat['tiles'][t]['key']}" for t in names]
    else:
        paths = [cat['tiles'][t]['key'] if cat['tiles'][t].get('remote') else tile_path(root, spec, cat, t) for t in names]
    return read_mosaic(paths, tuple(bounds), res)


def select_from(root, spec, other_id):
    """Reuse another source's selected windows (same CRS): the same cells, re-tiled against this source's catalogue.

    For cross-capture work (Christchurch 2020-21 against 2025) the teacher must see the same ground; cells this
    source does not fully cover are dropped and listed. The cells (bounds) are theirs; the pixel geometry is this
    source's own: a 5 cm source sharing 7.5 cm cells is cut at 5436 px, never at the other source's 3624 px."""
    root = Path(root)
    cat = load_catalog(root, spec)
    other = source(other_id)
    if other.get('crs') != spec.get('crs'):
        raise SystemExit(f"{other_id} is in {other.get('crs')}, {spec['id']} in {spec.get('crs')}: cells cannot be shared")
    theirs = common.read_json(teacher_dir(root, other) / 'windows.json')
    gsd = effective_gsd(spec)
    size = native_window_px(gsd)
    if abs(size * gsd - theirs['window_m']) > 1e-6:
        raise SystemExit(f"{spec['id']}: {size} px x {gsd} m = {size * gsd:.6f} m does not tile {other_id}'s "
                         f"{theirs['window_m']} m windows; the cells cannot be shared at this pixel")
    kept, dropped = [], []
    for cell in theirs['order']:
        names = tiles_covering(cat, cell['bounds'])
        if fully_covered(cat, cell['bounds']):
            kept.append({**cell, 'id': window_id(spec['window_prefix'], cell['row'], cell['col']), 'tiles': names,
                         'shared_with': other_id, 'shared_id': cell['id']})
        else:
            dropped.append(cell['id'])
    geo = {**theirs['geo'], 'gsd_m': gsd, 'gsd_x_m': gsd, 'gsd_y_m': gsd, 'native_gsd_m': spec['gsd_m']}
    rule = {**theirs['rule'], 'reduce': int(round(OVERVIEW_M / gsd))}
    result = {**theirs, 'version': spec['version'], 'source': spec['id'], 'order': kept, 'rejected': [],
              'native_window_px': size, 'geo': geo, 'rule': rule,
              'shared_from': other_id, 'dropped_not_covered': dropped,
              'rule_text': theirs.get('rule_text', '') + f'; cells copied from {other_id} and re-tiled for {spec["id"]}',
              'counts': {**theirs.get('counts', {}), 'shared_kept': len(kept), 'shared_dropped': len(dropped)}}
    folder = teacher_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    common.write_json_atomic(folder / 'windows.json', result)
    reduce = result['rule']['reduce']
    common.write_json_atomic(cache_dir(root, spec) / f'overview-r{reduce}.json', {
        'version': VERSION, 'source': spec['id'], 'reduce': reduce, 'overview_m': OVERVIEW_M, 'jp2': None,
        'geo_box': {'crs': spec['crs'], 'bounds': theirs.get('bounds_crs'), 'bbox_lonlat': theirs.get('bbox_lonlat')},
        'shared_from': other_id, 'computed_at': common.now_iso()})
    return {'kept': len(kept), 'dropped_not_covered': dropped, 'shared_from': other_id}


def select(root, spec, n=DEFAULT_N, seed=DEFAULT_SEED, bbox_lonlat=None, candidates=DEFAULT_CANDIDATES, rule=None,
           remote=True):
    """Seeded sample of window cells over the bbox, classified from COG overviews, first N eligible kept in order."""
    root = Path(root)
    cat = load_catalog(root, spec)
    rule = {**SUBURB_RULE, **(rule or {})}
    gsd = effective_gsd(spec)
    size = native_window_px(gsd)
    window_m = size * gsd
    bbox_lonlat = bbox_lonlat or spec.get('default_bbox_lonlat')
    if bbox_lonlat is None and cat.get('header'):
        h = cat['header']
        bounds_crs = (h['left'], h['bottom'], h['right'], h['top'])
        lon, lat = to_lonlat(spec['crs'], [h['left'], h['right']], [h['bottom'], h['top']])
        bbox_lonlat = [round(lon[0], 7), round(lat[0], 7), round(lon[1], 7), round(lat[1], 7)]
    elif bbox_lonlat is None:
        raise SystemExit('no bbox: pass --bbox W S E N (lon/lat)')
    else:
        bounds_crs = bbox_to_crs(spec['crs'], bbox_lonlat)
    excludes = [bbox_to_crs(spec['crs'], b) for b in spec.get('exclude_bbox_lonlat', [])]
    rows, cols, origin_x, origin_top = window_grid(bounds_crs, window_m)
    cells = []
    for row in range(rows):
        for col in range(cols):
            b = cell_bounds(row, col, origin_x, origin_top, window_m)
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            if not bbox_contains(bounds_crs, cx, cy) or any(bbox_contains(x, cx, cy) for x in excludes):
                continue
            if fully_covered(cat, b):
                cells.append({'row': row, 'col': col, 'bounds': list(b)})
    if not cells:
        raise SystemExit(f"{spec['id']}: no {window_m:.0f} m window cell lies fully inside the imagery (bounds {bounds_crs}); "
                         'lot-sized orthomosaics need a raster at least two windows wide, or a local-window mode')
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(cells)).tolist()
    sampled = [cells[i] for i in order[:candidates]]
    overview_px = int(round(window_m / OVERVIEW_M))
    reduce = int(round(OVERVIEW_M / gsd))
    chosen, rejected, counts = [], [], {}
    started = time.monotonic()
    for cell in sampled:
        if len(chosen) >= n:
            break
        names = tiles_covering(cat, cell['bounds'])
        try:
            rgba, _ = read_window(root, spec, cat, cell['bounds'], OVERVIEW_M, names, remote)
        except Exception as error:  # noqa: BLE001 - a tile that fails to read is a rejection, recorded
            rejected.append({**cell, 'reasons': [f'read_error: {type(error).__name__}']})
            counts['read_error'] = counts.get('read_error', 0) + 1
            continue
        stats = classify_window(rgba, rule)
        entry = {**cell, **stats, 'id': window_id(spec['window_prefix'], cell['row'], cell['col']), 'tiles': names,
                 'overview_px': list(rgba.shape[:2])}
        classified = len(chosen) + len(rejected) + 1
        if classified % 10 == 0 or stats['eligible']:
            print(json.dumps({'classified': classified, 'eligible': len(chosen) + int(stats['eligible']), 'cell': entry['id'],
                              'reasons': stats['reasons'], 'seconds': round(time.monotonic() - started, 1)}), file=sys.stderr, flush=True)
        if stats['eligible']:
            chosen.append(entry)
        else:
            rejected.append(entry)
            for reason in stats['reasons']:
                counts[reason] = counts.get(reason, 0) + 1
    if chosen:
        lon, lat = to_lonlat(spec['crs'], [(c['bounds'][0] + c['bounds'][2]) / 2 for c in chosen],
                             [(c['bounds'][1] + c['bounds'][3]) / 2 for c in chosen])
        for c, x, y in zip(chosen, lon, lat, strict=True):
            c['centre_lonlat'] = [round(x, 7), round(y, 7)]
    geo = {'gsd_m': gsd, 'gsd_x_m': gsd, 'gsd_y_m': gsd, 'native_gsd_m': spec['gsd_m'], 'origin_e': origin_x, 'origin_n': origin_top,
           'crs': spec['crs'], 'source': 'window lattice aligned to whole window multiples in the source CRS'}
    result = {'version': spec['version'], 'source': spec['id'], 'native_window_px': size, 'window_m': round(window_m, 3), 'geo': geo,
              'n': n, 'seed': seed, 'rule': {**rule, 'reduce': reduce, 'overview_m': OVERVIEW_M},
              'rule_text': ('seeded permutation of window cells fully covered by tiles inside the bbox (minus excluded boxes); '
                            'each sampled cell classified on a 1 m overview mosaic: inside >= inside_min, veg in [veg_min, veg_max], '
                            'water <= water_max, structure in [structure_min, structure_max]; first n eligible kept'),
              'bbox_lonlat': bbox_lonlat, 'bounds_crs': list(bounds_crs), 'grid': {'rows': rows, 'cols': cols, 'origin_x': origin_x,
                                                                                    'origin_top': origin_top},
              'counts': {'cells_covered': len(cells), 'sampled': len(sampled), 'classified': len(chosen) + len(rejected),
                         'eligible': len(chosen), 'rejected_by_reason': counts, 'overview_px': overview_px},
              'order': chosen, 'rejected': rejected[:200], 'seconds': round(time.monotonic() - started, 1)}
    folder = teacher_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    common.write_json_atomic(folder / 'windows.json', result)
    common.write_json_atomic(cache_dir(root, spec) / f'overview-r{reduce}.json', {
        'version': VERSION, 'source': spec['id'], 'reduce': reduce, 'overview_m': OVERVIEW_M, 'jp2': None,
        'geo_box': {'crs': spec['crs'], 'bounds': list(bounds_crs), 'bbox_lonlat': bbox_lonlat}, 'computed_at': common.now_iso()})
    return {k: v for k, v in result.items() if k not in ('order', 'rejected')}


# --- fetch ---------------------------------------------------------------------------------------------------------

def fetch(root, spec, limit=None, workers=8):
    """Download the tiles the selected windows touch (skips verified files); verifies the STAC sha256 when present."""
    root = Path(root)
    cat = load_catalog(root, spec)
    windows = common.read_json(teacher_dir(root, spec) / 'windows.json')
    wanted = sorted({t for w in windows['order'][:limit or windows['n']] for t in w['tiles']})
    folder = cache_dir(root, spec) / 'tiles'
    folder.mkdir(parents=True, exist_ok=True)
    client = s3_client()
    done, downloaded, mismatched = [], [], []
    manifest_path = folder / 'manifest.json'
    manifest = common.read_json(manifest_path) if manifest_path.exists() else {}

    def one(name):
        tile = cat['tiles'][name]
        path = folder / f'{name}.tiff'
        expected, tile_props = None, {}
        try:
            item = s3_json(client, cat['bucket'], tile['item_key'])
            asset = item.get('assets', {}).get('visual', {})
            expected = multihash_sha256(asset.get('file:checksum'))
            tile_props = {k: item.get('properties', {}).get(k) for k in ('start_datetime', 'end_datetime')}
        except Exception:  # noqa: BLE001 - the item JSON is optional provenance
            pass
        have = manifest.get(name, {}).get('sha256')
        if path.exists() and have and (expected is None or have == expected):
            return name, 'already', None
        started = time.monotonic()
        try:
            client.download_file(cat['bucket'], tile['key'], str(path))
        except Exception as error:  # noqa: BLE001 - recorded per tile; the run continues
            return name, 'error', {'error': f'{type(error).__name__}: {str(error)[:120]}'}
        sha = common.sha256_file(path)
        if expected and sha != expected:
            path.unlink(missing_ok=True)
            return name, 'mismatch', None
        row = {'sha256': sha, 'bytes': path.stat().st_size, 'seconds': round(time.monotonic() - started, 2), 'key': tile['key'],
               'expected_sha256': expected, **tile_props, 'fetched_at': common.now_iso()}
        return name, 'downloaded', row

    from concurrent.futures import ThreadPoolExecutor, as_completed
    started_all = time.monotonic()
    errors = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, name) for name in wanted]
        for k, future in enumerate(as_completed(futures), start=1):
            name, status, row = future.result()
            if status == 'already':
                done.append(name)
            elif status == 'mismatch':
                mismatched.append(name)
            elif status == 'error':
                errors[name] = row['error']
            else:
                manifest[name] = row
                downloaded.append(name)
                common.write_json_atomic(manifest_path, manifest)
            if k % 10 == 0 or k == len(wanted):
                print(json.dumps({'fetched': k, 'of': len(wanted), 'downloaded': len(downloaded), 'already': len(done),
                                  'mb': round(sum(manifest[n]['bytes'] for n in wanted if n in manifest) / 2 ** 20, 1),
                                  'seconds': round(time.monotonic() - started_all, 1)}), file=sys.stderr, flush=True)
    return {'wanted': len(wanted), 'already': len(done), 'downloaded': len(downloaded), 'checksum_mismatch': mismatched,
            'errors': errors, 'bytes_total': sum(manifest[n]['bytes'] for n in wanted if n in manifest),
            'seconds': round(time.monotonic() - started_all, 1)}


# --- crop ----------------------------------------------------------------------------------------------------------

def window_affines(bounds, gsd, size):
    left, _, _, top = bounds
    student_m = size * gsd / STUDENT_PX
    return [left, gsd, 0., top, 0., -gsd], [left, student_m, 0., top, 0., -student_m], student_m


def crop(root, spec, limit=None):
    """Mosaic each selected window at native GSD from the fetched tiles; accept only fully opaque windows."""
    from .hires_teacher import save_png
    root = Path(root)
    cat = load_catalog(root, spec)
    windows = common.read_json(teacher_dir(root, spec) / 'windows.json')
    gsd, size = windows['geo']['gsd_m'], windows['native_window_px']
    out = cache_dir(root, spec) / 'windows'
    out.mkdir(parents=True, exist_ok=True)
    accepted, skipped = [], []
    n = limit or windows['n']
    for entry in windows['order']:
        if len(accepted) >= n:
            break
        path = out / f"{entry['id']}.png"
        meta_path = path.with_suffix('.json')
        keys = [cat['tiles'][t]['key'] for t in entry['tiles']]
        meta = common.read_json(meta_path) if path.exists() and meta_path.exists() else None
        # window ids count from a bbox-dependent lattice origin: a cached PNG is only this window if its tiles,
        # georeference and shape match (a re-select with another bbox, a re-registered ortho, or a window cut at
        # another pixel must re-read)
        fresh = (meta is not None and meta.get('tiles') == entry['tiles'] and meta.get('tile_keys', keys) == keys
                 and meta.get('shape') == [size, size, 3]
                 and 'transform' in meta and abs(meta['transform'][0] - entry['bounds'][0]) < 1e-3
                 and abs(meta['transform'][3] - entry['bounds'][3]) < 1e-3)
        if not fresh:
            local = [tile_path(root, spec, cat, t) for t in entry['tiles']
                     if not cat['tiles'][t].get('remote') and not cat['tiles'][t].get('arcgis')]
            missing = [str(p.name) for p in local if not p.exists()]
            if missing:
                skipped.append({'id': entry['id'], 'missing_tiles': missing})
                continue
            started = time.monotonic()
            rgba, transform = read_window(root, spec, cat, entry['bounds'], gsd, entry['tiles'], remote=False)
            seconds = time.monotonic() - started
            rgba = rgba[:size, :size]
            inside = float((rgba[..., 3] >= 255).mean()) if rgba.shape[0] == size and rgba.shape[1] == size else 0.
            meta = {'id': entry['id'], 'row': entry['row'], 'col': entry['col'], 'shape': [size, size, 3],
                    'inside_fraction': round(inside, 6), 'read_seconds': round(seconds, 2), 'tiles': entry['tiles'], 'tile_keys': keys,
                    'transform': [transform.c, transform.a, transform.b, transform.f, transform.d, transform.e]}
            if inside >= .999:
                meta['sha256'] = save_png(path, np.ascontiguousarray(rgba[..., :3]))
            common.write_json_atomic(meta_path, meta)
        if meta['inside_fraction'] >= .999 and path.exists():
            native_affine, student_affine, student_m = window_affines(entry['bounds'], gsd, size)
            accepted.append({**meta, 'native_affine': native_affine, 'student_affine': student_affine, 'student_px_m': student_m,
                             'native_file': str(path.relative_to(root)), 'centre_lonlat': entry.get('centre_lonlat'),
                             'crs': spec['crs'], 'bounds': entry['bounds'], 'shared_id': entry.get('shared_id'),
                             'shared_with': entry.get('shared_with')})
        else:
            skipped.append({'id': entry['id'], 'inside_fraction': meta['inside_fraction']})
    reader = {'arcgis-image': 'ArcGIS ImageServer exportImage at the teacher pixel',
              'geotiff': 'rasterio windowed read (average resampling when the teacher pixel is coarser than native)'}
    result = {'version': spec['version'], 'source': spec['id'], 'native_window_px': size, 'gsd_m': gsd, 'native_gsd_m': spec.get('gsd_m'),
              'windows': accepted, 'skipped_partial': skipped,
              'reader': reader.get(spec['kind'], 'rasterio.merge over cloud-optimised GeoTIFF tiles')}
    common.write_json_atomic(teacher_dir(root, spec) / 'crops.json', result)
    return {'accepted': len(accepted), 'skipped': len(skipped)}


# --- local GeoTIFF (drone orthomosaic) -----------------------------------------------------------------------------

def register_geotiff(root, spec, path, capture=None, place=None):
    """Point a geotiff-kind source at one raster (local path or http(s)/s3 URL): cache/catalog.json like `catalog`."""
    root = Path(root)
    text = str(path)
    remote = text.startswith(('http://', 'https://', 's3://'))
    header = read_header(text)
    folder = cache_dir(root, spec)
    folder.mkdir(parents=True, exist_ok=True)
    gsd = round((header['gsd_x_m'] + header['gsd_y_m']) / 2, 6)
    name = Path(text.split('?')[0]).stem
    result = {'version': VERSION, 'source': spec['id'], 'title': spec['title'], 'crs': header['crs'], 'gsd_m': gsd,
              'teacher_gsd_m': spec.get('teacher_gsd_m'), 'bucket': None, 'prefix': None, 'n_tiles': 1, 'sheets': {}, 'header': header,
              'capture': capture, 'place': place,
              'tiles': {name: {'key': text if remote else str(Path(text).resolve()), 'local': not remote, 'remote': remote,
                               'bounds': [header['left'], header['bottom'], header['right'], header['top']]}},
              'fetched_at': common.now_iso()}
    common.write_json_atomic(folder / 'catalog.json', result)
    resample = (f" and area-averaged from {gsd} m to a {spec['teacher_gsd_m']} m teacher pixel" if spec.get('teacher_gsd_m') else '')
    common.write_json_atomic(folder / 'provenance.json', {
        **{k: spec.get(k) for k in ('id', 'title', 'publisher', 'place', 'capture', 'crs', 'licence', 'licence_url', 'url')},
        'gsd_m': gsd, 'teacher_gsd_m': spec.get('teacher_gsd_m'), 'raster': text, 'capture_text': capture, 'place_text': place,
        'attribution': f"{spec['title']}, {spec['publisher']}, {spec['licence']}. Changes: cropped to {STUDENT_PX} px windows at "
                       f"the pipeline's tile size{resample}, then area-averaged to {PIPELINE_PX_M:.3f} m; building masks are SAM3 "
                       'model outputs.', 'registered_at': result['fetched_at']})
    return {k: v for k, v in result.items() if k != 'tiles'}


def resolved(spec, root):
    """Source spec with gsd/crs filled from a registered local raster when the registry leaves them open."""
    if spec.get('gsd_m') is None or spec.get('crs') is None:
        path = cache_dir(root, spec) / 'catalog.json'
        if path.exists():
            cat = common.read_json(path)
            spec = {**spec, 'gsd_m': spec.get('gsd_m') or cat.get('gsd_m'), 'crs': spec.get('crs') or cat.get('crs')}
    return spec


# --- CLI -----------------------------------------------------------------------------------------------------------

def registry_table():
    rows = []
    for spec in SOURCES.values():
        rows.append({k: spec.get(k) for k in ('id', 'kind', 'title', 'gsd_m', 'crs', 'licence', 'similarity', 'cache', 'out')})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', choices=('list', 'catalog', 'select', 'fetch', 'crop', 'register'))
    parser.add_argument('--source', default='linz-chc-2025-0075')
    parser.add_argument('--root', default='.')
    parser.add_argument('--n', type=int, default=DEFAULT_N)
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED)
    parser.add_argument('--bbox', type=float, nargs=4, metavar=('W', 'S', 'E', 'N'), help='lon/lat bbox for select')
    parser.add_argument('--candidates', type=int, default=DEFAULT_CANDIDATES)
    parser.add_argument('--rule', type=json.loads, default=None, help='JSON overrides for the selection rule')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--workers', type=int, default=8, help='parallel downloads for fetch')
    parser.add_argument('--path', help='local raster for `register`')
    parser.add_argument('--windows-from', help='select: copy the selected cells of another source with the same CRS (cross-capture)')
    parser.add_argument('--capture', help='capture date text for `register`')
    parser.add_argument('--place', help='place text for `register`')
    args = parser.parse_args(argv)
    if args.stage == 'list':
        result = registry_table()
    else:
        spec = resolved(source(args.source), args.root)
        if args.stage == 'catalog':
            result = catalog(args.root, spec, args.limit)
        elif args.stage == 'register':
            target = args.path or spec.get('url')
            if not target:
                raise SystemExit('register needs --path (or a registry url)')
            result = register_geotiff(args.root, spec, target, args.capture or spec.get('capture'), args.place or spec.get('place'))
        elif args.stage == 'select' and args.windows_from:
            result = select_from(args.root, spec, args.windows_from)
        elif args.stage == 'select':
            result = select(args.root, spec, args.n, args.seed, args.bbox, args.candidates, args.rule,
                            remote=spec['kind'] in ('linz-stac', 'arcgis-image') or bool(spec.get('remote')))
        elif args.stage == 'fetch':
            result = fetch(args.root, spec, args.limit, args.workers) if spec['kind'] == 'linz-stac' else {'skipped': 'not a tiled source'}
        else:
            result = crop(args.root, spec, args.limit)
    json.dump(result, sys.stdout, indent=1, sort_keys=True, default=str)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
