"""City of Melbourne 2018 true ortho as a hi-res SAM3 teacher for 0.236 m student images (hires-teacher-com-2018-v1).

The pipeline sees 0.236 m pixels (the saved scene grid). The City of Melbourne publishes a CC BY 4.0 true orthophoto of
its municipality (May 2018, JPEG2000, MGA Zone 55 GDA94). This module cuts windows of the pipeline's tile size
(1152 px at 0.236 m, about 272 m) out of that ortho and writes, per window:

  native crop    the ortho at its own pixel, read tile by tile with openjpeg (never the whole file); the true GSD comes
                 from the georeferencing (ERS header in the zip, cross-checked against the JP2 geo boxes)
  student image  the same window area-averaged onto 1152 px (exact area weights), then a Gaussian blur whose sigma
                 matches the saved tiles' gradient-energy ratio (see `calibrate`)
  teacher masks  SAM3 'building roof' on the NATIVE crop in overlapping 1152 px sub-tiles; masks are stitched (each
                 pixel decided by the sub-tile whose core holds it, pieces linked across sub-tiles when they agree in
                 the shared strip at IoU >= 0.5) and deduplicated with the production building rule, then area-averaged
                 onto the student grid (kept where the covered fraction is >= 0.5)
  control masks  SAM3 on the student image itself, same prompt, threshold and dedupe

Stages, each resumable. The JP2 stages need glymur (training venv); the SAM3 stages need onnxruntime + tokenizers
(main venv):

  scripts/ml/training/.venv/bin/python -m scripts.ml.training.hires_teacher extract|overview|select|crop
  scripts/ml/.venv/bin/python -m scripts.ml.training.hires_teacher calibrate|teach [--shard K/N]|finalize|measure

Outputs: native crops and the overview under data/imagery/com-2018/ (cache); everything else under
data/reviews/eyes/training/distill-v1/teacher/. Nothing here writes under data/scenes or the eyes index. Masks from
either run are SAM3 model outputs, never labels from people; every comparison is agreement between two model runs,
never accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import warnings
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely import affinity

from ..segmentation import mask_to_polygons
from . import common

VERSION = 'hires-teacher-com-2018-v1'
PROVENANCE_TAG = 'hires-teacher-com-2018'
CACHE = Path('data/imagery/com-2018')
OUT = Path('data/reviews/eyes/training/distill-v1')
TEACHER = OUT / 'teacher'
ZIP_NAME = 'mga55_gda94_10cm_2018_CoM_true_ortho.zip'
STEM = 'mga55_gda94_10cm_2018_CoM_true_ortho'
MODELS = Path('scripts/ml/models/sam3')
WINDOW_PREFIX = 'com2018'

PIPELINE_PX_M = 0.23595384232187258     # saved scan tiles: z19 pixel at Mooroolbark (latest.json pixel_to_meters)
STUDENT_PX = 1152
DEFAULT_N = 60
DEFAULT_SEED = 20260928
PROMPT = 'building roof'
THRESHOLD = .45                          # scene_extract.extract_objects
MAX_PROPOSALS = 200                      # every decoder query (production caps at 80; recorded per window)
PRODUCTION_CAP = 80
SUBTILE = 1152
MIN_OVERLAP = 256                        # native px shared by neighbouring sub-tiles (at least)
MIN_AREA_M2 = 1.                         # scene_extract: max(12 px, 1 m2)
MERGE_IOU = .5                           # cross-sub-tile link: IoU inside the shared strip
MERGE_MIN_M2 = 1.                        # ... with at least this much of each piece in the strip
DUP_IOU, DUP_CONTAIN = .8, .95           # scene_extract building duplicate rule
MATCH_IOU = .5
SIMPLIFY_PX = .9                         # scene_extract vectoriser setting
COMMON_TOLERANCE_M = .25                 # same metric simplification for the corner analysis in every arm
COARSE_TOLERANCE_M = .5                  # sensitivity arm: removes 2-student-pixel staircases (exact 90 degree steps)
CORNER_MIN_TURN = 30.                    # a vertex turning at least this much is a corner
RIGHT_TOL = 10.                          # a corner within this of 90 degrees is a right corner
SIGMA_GRID = tuple(float(round(s, 2)) for s in np.arange(0., 1.61, .05))
CALIBRATION_SAVED = 48
CALIBRATION_STUDENTS = 24
SHEET_PAIRS = 24
SHEET_MIN_AREA_M2 = 20.

SOURCE = {
    'title': 'City of Melbourne 2018 Aerial Imagery (True Ortho)',
    'publisher': 'City of Melbourne',
    'url': 'https://s3-ap-southeast-2.amazonaws.com/opendata-aerial-imagery/mga55_gda94_10cm_2018_CoM_true_ortho.zip',
    'catalogue': 'https://discover.data.vic.gov.au/dataset/2018-aerial-imagery-true-ortho',
    'licence': 'CC BY 4.0',
    'licence_url': 'https://creativecommons.org/licenses/by/4.0/',
    'capture': 'May 2018',
    'crs': 'EPSG:28355 (GDA94 / MGA zone 55)',
    'attribution': ('Contains City of Melbourne 2018 Aerial Imagery (True Ortho), (c) City of Melbourne, licensed under '
                    'CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Changes: cropped to 272 m windows and '
                    'area-averaged to 0.236 m (blur calibrated against other imagery); building masks are SAM3 model '
                    'outputs.'),
}
JP2_STAGES = ('extract', 'overview', 'select', 'crop')     # City of Melbourne JPEG2000 only; other sources use teacher_sources
_COM_DEFAULTS = {'SOURCE': SOURCE, 'VERSION': VERSION, 'PROVENANCE_TAG': PROVENANCE_TAG, 'CACHE': CACHE, 'OUT': OUT,
                 'TEACHER': TEACHER, 'WINDOW_PREFIX': WINDOW_PREFIX}     # import-time City of Melbourne values, restored for com-2018


def set_source(source_id, root='.'):
    """Rebind the source-specific globals (paths, version, provenance tag, window prefix) to a teacher_sources entry.

    The City of Melbourne source keeps the module defaults. Every other source has its windows cut by
    teacher_sources.py into the same windows.json / crops.json schema, after which calibrate, teach, finalize and
    measure run here unchanged."""
    global SOURCE, VERSION, PROVENANCE_TAG, CACHE, OUT, TEACHER, WINDOW_PREFIX
    from .teacher_sources import resolved
    from .teacher_sources import source as lookup
    spec = resolved(lookup(source_id), root)
    if spec['kind'] == 'com-jp2':
        SOURCE, VERSION, PROVENANCE_TAG = _COM_DEFAULTS['SOURCE'], _COM_DEFAULTS['VERSION'], _COM_DEFAULTS['PROVENANCE_TAG']
        CACHE, OUT, TEACHER, WINDOW_PREFIX = (_COM_DEFAULTS['CACHE'], _COM_DEFAULTS['OUT'], _COM_DEFAULTS['TEACHER'],
                                              _COM_DEFAULTS['WINDOW_PREFIX'])
        return spec
    provenance_path = Path(root) / spec['cache'] / 'provenance.json'
    cached = common.read_json(provenance_path) if provenance_path.exists() else {}
    SOURCE = {'title': spec['title'], 'publisher': spec['publisher'], 'url': spec.get('url'), 'catalogue': spec.get('catalogue'),
              'licence': spec['licence'], 'licence_url': spec.get('licence_url'), 'capture': spec.get('capture'),
              'crs': spec.get('crs'), 'place': spec.get('place'), 'similarity': spec.get('similarity'),
              'attribution': cached.get('attribution') or f"{spec['title']}, {spec['publisher']}, {spec['licence']}. Changes: "
                                                          'cropped to pipeline-sized windows and area-averaged to 0.236 m; '
                                                          'building masks are SAM3 model outputs.',
              'grid_note': spec.get('grid_note'), 'file': spec.get('url'),
              'processing': ({'linz-stac': 'windows mosaicked with rasterio from cloud-optimised GeoTIFF tiles',
                              'arcgis-image': 'windows exported from the ArcGIS ImageServer at the teacher pixel in 1812 px pieces',
                              'geotiff': 'windows read from the raster with rasterio (area-averaged to the teacher pixel when coarser '
                                         'than native)'}.get(spec['kind'], 'windows read with rasterio') +
                             '; student = exact area average onto 1152 px then Gaussian blur; teacher = SAM3 text ONNX (int8, '
                             'scripts/ml/models/sam3) on native 1152 px sub-tiles; control = the same SAM3 on the student image')}
    VERSION = spec['version']
    PROVENANCE_TAG = spec['provenance_tag']
    CACHE = Path(spec['cache'])
    OUT = Path(spec['out'])
    TEACHER = OUT / 'teacher'
    WINDOW_PREFIX = spec['window_prefix']
    return spec


# Set from pixel samples of the 1/32 overview (docks water, Royal Park, Fitzroy Gardens, Kings Domain, North Melbourne,
# Carlton, Kensington, East Melbourne, CBD, Southbank, rail yard, container terminal, Fishermans Bend) before any SAM3
# run. Outside the municipality the file holds a constant fill colour; its 1-bit alpha band is 1 everywhere.
NODATA_RGB = (127, 129, 253)
OVERVIEW_RULE = {'reduce': 5, 'nodata_rgb': NODATA_RGB, 'nodata_tol': 12., 'inside_min': .995, 'water_std_max': 3.,
                 'water_g_minus_r': 15., 'water_b_minus_r': 10., 'water_max': .05, 'exg_veg': .06, 'veg_max': .35,
                 'edge_grad_min': 15., 'structure_min': .40}


# --- small helpers -------------------------------------------------------------------------------------------------

def _num(value, digits=4):
    return None if value is None or (isinstance(value, float) and not math.isfinite(value)) else round(float(value), digits)


def save_png(path, array):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp.png')
    Image.fromarray(array).save(temporary)
    os.replace(temporary, path)
    return common.sha256_file(path)


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# --- source, georeference ------------------------------------------------------------------------------------------

def parse_ers(text):
    """The ER Mapper header shipped in the zip: GSD, size, bands and the registration corner (EN, metres)."""
    def value(key, cast=float):
        match = re.search(rf'\b{key}\s*=\s*"?([^"\n]+)"?', text)
        return cast(match.group(1).strip()) if match else None
    return {'gsd_x_m': value('Xdimension'), 'gsd_y_m': value('Ydimension'), 'height': value('NrOfLines', int),
            'width': value('NrOfCellsPerLine', int), 'origin_e': value('Eastings'), 'origin_n': value('Northings'),
            'bands': value('NrOfBands', int), 'datum': value('Datum', str), 'projection': value('Projection', str),
            'registration': 'top-left corner of cell (0, 0) (ER Mapper default, no RegistrationCellX/Y)'}


def zip_member_text(zip_path, suffix):
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.filename.endswith(suffix) and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                return archive.read(info).decode(errors='replace')
    return None


def georeference(root):
    """Georeference from the extracted .ers (or the zip), GSD rounded to the micrometre."""
    folder = Path(root) / CACHE / STEM
    ers = folder / f'{STEM}.ers'
    text = ers.read_text(errors='replace') if ers.exists() else zip_member_text(Path(root) / CACHE / ZIP_NAME, '.ers')
    if text is None:
        raise FileNotFoundError('no .ers header: run `extract` first')
    geo = parse_ers(text)
    geo['gsd_m'] = round((geo['gsd_x_m'] + geo['gsd_y_m']) / 2, 6)
    geo['source'] = 'ERS header in the zip'
    return geo


def native_window_px(gsd_m, student_px=STUDENT_PX, student_m=PIPELINE_PX_M):
    """Native pixels of one window: the pipeline tile's ground width in the ortho's pixels."""
    return int(round(student_px * student_m / gsd_m))


def window_affines(geo, row, col, size):
    """GDAL-style geotransforms (E0, px, 0, N0, 0, -px) of a grid cell at native and student resolution."""
    e0 = geo['origin_e'] + col * size * geo['gsd_x_m']
    n0 = geo['origin_n'] - row * size * geo['gsd_y_m']
    student_m = size * geo['gsd_m'] / STUDENT_PX
    return ([e0, geo['gsd_x_m'], 0., n0, 0., -geo['gsd_y_m']], [e0, student_m, 0., n0, 0., -student_m], student_m)


# GRS80 (GDA94) transverse Mercator, zone 55, Krueger n-series (Karney 2011; GDA2020 technical manual form).
_A_AXIS, _FLAT = 6378137., 1 / 298.257222101
_K0, _FE, _FN, _LON0 = .9996, 500000., 10000000., 147.


def _tm_constants():
    n = _FLAT / (2 - _FLAT)
    big_a = _A_AXIS / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64)
    alpha = (n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16, 13 * n ** 2 / 48 - 3 * n ** 3 / 5, 61 * n ** 3 / 240)
    beta = (n / 2 - 2 * n ** 2 / 3 + 37 * n ** 3 / 96, n ** 2 / 48 + n ** 3 / 15, 17 * n ** 3 / 480)
    delta = (2 * n - 2 * n ** 2 / 3 - 2 * n ** 3, 7 * n ** 2 / 3 - 8 * n ** 3 / 5, 56 * n ** 3 / 15)
    return n, big_a, alpha, beta, delta


def lonlat_to_mga55(lon, lat):
    n, big_a, alpha, _, _ = _tm_constants()
    phi, dlam = math.radians(lat), math.radians(lon - _LON0)
    c = 2 * math.sqrt(n) / (1 + n)
    t = math.sinh(math.atanh(math.sin(phi)) - c * math.atanh(c * math.sin(phi)))
    xi_p = math.atan2(t, math.cos(dlam))
    eta_p = math.atanh(math.sin(dlam) / math.sqrt(1 + t * t))
    xi = xi_p + sum(a * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p) for j, a in enumerate(alpha, 1))
    eta = eta_p + sum(a * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p) for j, a in enumerate(alpha, 1))
    return _FE + _K0 * big_a * eta, _FN + _K0 * big_a * xi


def mga55_to_lonlat(easting, northing):
    """GDA94 MGA zone 55 -> (lon, lat) degrees (GDA94 ~ WGS84 within about 1.8 m in 2018; informational only)."""
    _, big_a, _, beta, delta = _tm_constants()
    xi_p = (northing - _FN) / (_K0 * big_a)
    eta_p = (easting - _FE) / (_K0 * big_a)
    xi = xi_p - sum(b * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p) for j, b in enumerate(beta, 1))
    eta = eta_p - sum(b * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p) for j, b in enumerate(beta, 1))
    chi = math.asin(math.sin(xi) / math.cosh(eta))
    phi = chi + sum(d * math.sin(2 * j * chi) for j, d in enumerate(delta, 1))
    return _LON0 + math.degrees(math.atan2(math.sinh(eta), math.cos(xi))), math.degrees(phi)


# --- JPEG2000 access (training venv) -------------------------------------------------------------------------------

def jp2_path(root):
    return Path(root) / CACHE / STEM / f'{STEM}.jp2'


def extract(root='.'):
    """Unzip the ortho (Deflate64, which Python's zipfile cannot read) with the system unzip; CRC checked by unzip."""
    root = Path(root)
    archive = root / CACHE / ZIP_NAME
    if not (root / CACHE / 'download.log').exists():
        raise SystemExit('download.log is missing: the download has not finished')
    target = jp2_path(root)
    if target.exists() and target.stat().st_size > 0:
        return {'jp2': str(target), 'skipped': True}
    started = time.monotonic()
    subprocess.run(['unzip', '-o', str(archive), '-d', str(root / CACHE)], check=True, stdout=subprocess.DEVNULL)
    return {'jp2': str(target), 'seconds': round(time.monotonic() - started, 1), 'bytes': target.stat().st_size}


def open_glymur(root='.'):
    """glymur with openjpeg; falls back to Pillow's bundled libopenjp2 through a cache-local glymurrc."""
    import ctypes.util
    if ctypes.util.find_library('openjp2') is None and 'XDG_CONFIG_HOME' not in os.environ:
        import PIL
        base = Path(PIL.__file__).parent
        libraries = sorted((base / '.dylibs').glob('libopenjp2*')) + sorted((base.parent / 'pillow.libs').glob('libopenjp2*'))
        if libraries:
            config = (Path(root) / CACHE / '.glymur').resolve()
            (config / 'glymur').mkdir(parents=True, exist_ok=True)
            (config / 'glymur' / 'glymurrc').write_text(f'[library]\nopenjp2: {libraries[0]}\n')
            os.environ['XDG_CONFIG_HOME'] = str(config)
    try:
        import glymur
    except ImportError as error:
        raise SystemExit('glymur is missing: run this stage with scripts/ml/training/.venv/bin/python') from error
    if glymur.version.openjpeg_version in ('0.0.0', None):
        raise SystemExit('glymur found no openjpeg library')
    glymur.set_option('lib.num_threads', max(1, (os.cpu_count() or 2) - 2))
    warnings.filterwarnings('ignore', message='The file type compatibility list', category=UserWarning)
    return glymur


def parse_geojp2(raw):
    """GeoJP2 uuid box (a 1 x 1 GeoTIFF): pixel scale, tiepoint, EPSG code and raster type, read with PIL's TIFF reader."""
    try:
        tags = Image.open(io.BytesIO(raw)).tag_v2
    except Exception:  # noqa: BLE001 - an unreadable box is reported as absent
        return None
    keys = list(tags.get(34735) or [])
    entries = {keys[k]: keys[k + 3] for k in range(4, len(keys) - 3, 4)}
    scale, tie = tags.get(33550), tags.get(33922)
    return {'pixel_scale': [float(v) for v in scale[:2]] if scale else None,
            'tiepoint_pixel': [float(v) for v in tie[:2]] if tie else None,
            'tiepoint_en': [float(v) for v in tie[3:5]] if tie else None,
            'epsg': entries.get(3072), 'raster_type': {1: 'PixelIsArea', 2: 'PixelIsPoint'}.get(entries.get(1025)),
            'citation': str(tags.get(34737) or '')[:200]}


def jp2_info(jp2):
    siz = jp2.codestream.segment[1]
    cod = next(s for s in jp2.codestream.segment if s.marker_id == 'COD')
    boxes, geojp2 = [], None
    for item in jp2.box:
        if item.box_id == 'uuid':        # glymur prints GeoJP2 boxes through GDAL, which is not installed
            raw = getattr(item, 'raw_data', b'') or b''
            geojp2 = geojp2 or parse_geojp2(raw)
            boxes.append({'id': 'uuid', 'summary': f'uuid {item.uuid}, {len(raw)} bytes'})
            continue
        try:
            text = str(item)
        except Exception as error:  # noqa: BLE001
            text = f'unprintable: {error}'
        boxes.append({'id': item.box_id, 'summary': text[:600]})
    return {'shape': list(jp2.shape), 'tile': [int(siz.ytsiz), int(siz.xtsiz)], 'decomposition_levels': int(cod.num_res),
            'layers': int(cod.layers), 'progression': int(cod.prog_order), 'precinct_size': str(cod.precinct_size),
            'code_block_size': str(cod.code_block_size), 'openjpeg': jp2_openjpeg_version(), 'boxes': boxes,
            'geojp2': geojp2}


def jp2_openjpeg_version():
    import glymur
    return glymur.version.openjpeg_version


def geo_box_gsd(info, geo=None):
    """The GeoJP2 box's georeference and whether it agrees with the ERS header (GSD to 1 um, origin to 1 mm)."""
    box = info.get('geojp2')
    if not box or not box.get('pixel_scale'):
        return None
    result = {'box': 'uuid GeoJP2', 'gsd_x_m': box['pixel_scale'][0], 'gsd_y_m': box['pixel_scale'][1],
              'origin_en': box['tiepoint_en'], 'epsg': box['epsg'], 'raster_type': box['raster_type']}
    if geo:
        result['agrees_with_ers'] = bool(abs(box['pixel_scale'][0] - geo['gsd_x_m']) < 1e-6
                                         and abs(box['pixel_scale'][1] - geo['gsd_y_m']) < 1e-6
                                         and abs(box['tiepoint_en'][0] - geo['origin_e']) < 1e-3
                                         and abs(box['tiepoint_en'][1] - geo['origin_n']) < 1e-3)
    return result


def overview(root='.', reduce=OVERVIEW_RULE['reduce']):
    """Whole-ortho RGBA at 1/2**reduce (openjpeg decodes only the low-resolution subbands)."""
    glymur = open_glymur(root)
    jp2 = glymur.Jp2k(str(jp2_path(root)))
    info = jp2_info(jp2)
    reduce = min(reduce, info['decomposition_levels'])
    step = 2 ** reduce
    started = time.monotonic()
    image = np.ascontiguousarray(jp2[::step, ::step])
    seconds = time.monotonic() - started
    folder = Path(root) / CACHE
    digest = save_png(folder / f'overview-r{reduce}.png', image)
    geo = georeference(root)
    meta = {'version': VERSION, 'reduce': reduce, 'step': step, 'shape': list(image.shape), 'sha256': digest,
            'seconds': round(seconds, 1), 'jp2': info, 'geo': geo, 'geo_box': geo_box_gsd(info, geo),
            'jp2_bytes': jp2_path(root).stat().st_size}
    common.write_json_atomic(folder / f'overview-r{reduce}.json', meta)
    return meta


def read_native(jp2, row, col, size):
    y0, x0 = row * size, col * size
    return np.ascontiguousarray(jp2[y0:y0 + size, x0:x0 + size])


# --- window selection ----------------------------------------------------------------------------------------------

def classify_overview(rgba, rule=OVERVIEW_RULE):
    """Per-pixel inside / vegetation / water-like / structure masks of the low-resolution overview."""
    import cv2
    rgba = np.asarray(rgba)
    rgb = rgba[..., :3].astype(np.float32)
    inside = rgba[..., 3] > 0 if rgba.shape[-1] == 4 else np.ones(rgba.shape[:2], bool)
    inside &= inside_mask(rgb, rule)
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    exg = (2 * green - red - blue) / (red + green + blue + 1.)
    lum = .299 * red + .587 * green + .114 * blue
    mean = cv2.blur(lum, (3, 3))
    std = np.sqrt(np.maximum(cv2.blur(lum * lum, (3, 3)) - mean * mean, 0.))
    gx = cv2.Sobel(lum, cv2.CV_32F, 1, 0, ksize=3) / 8
    gy = cv2.Sobel(lum, cv2.CV_32F, 0, 1, ksize=3) / 8
    gradient = np.hypot(gx, gy)
    water = inside & (std < rule['water_std_max']) & (green - red > rule['water_g_minus_r']) & (
        blue - red > rule['water_b_minus_r'])
    veg = inside & ~water & (exg > rule['exg_veg'])
    structure = inside & ~veg & ~water & (gradient >= rule['edge_grad_min'])
    return {'inside': inside, 'veg': veg, 'water': water, 'structure': structure}


def inside_mask(rgb, rule=OVERVIEW_RULE):
    """Pixels that are imagery, not the constant no-data fill (max channel difference above the tolerance)."""
    rgb = np.asarray(rgb, dtype=np.float32)
    return np.abs(rgb - np.asarray(rule['nodata_rgb'], np.float32)).max(axis=-1) > rule['nodata_tol']


def grid_cells(masks, cell, rule=OVERVIEW_RULE):
    """Stats and eligibility of every full grid cell (cell = window side in overview pixels, float)."""
    height, width = masks['inside'].shape
    rows, cols = int(height // cell), int(width // cell)
    cells = []
    for row in range(rows):
        for col in range(cols):
            y0, y1 = int(round(row * cell)), int(round((row + 1) * cell))
            x0, x1 = int(round(col * cell)), int(round((col + 1) * cell))
            block = {k: v[y0:y1, x0:x1] for k, v in masks.items()}
            total = block['inside'].size
            inside = float(block['inside'].mean())
            valid = max(int(block['inside'].sum()), 1)
            stats = {'row': row, 'col': col, 'inside': round(inside, 4),
                     'veg': round(float(block['veg'].sum()) / valid, 4),
                     'water': round(float(block['water'].sum()) / valid, 4),
                     'structure': round(float(block['structure'].sum()) / valid, 4), 'pixels': total}
            reasons = []
            if inside < rule['inside_min']:
                reasons.append('outside_imagery')
            if stats['veg'] > rule['veg_max']:
                reasons.append('vegetation')
            if stats['water'] > rule['water_max']:
                reasons.append('water')
            if stats['structure'] < rule['structure_min']:
                reasons.append('low_structure')
            stats['eligible'] = not reasons
            stats['reasons'] = reasons
            cells.append(stats)
    return cells


def window_id(row, col):
    return f'{WINDOW_PREFIX}-r{row:03d}-c{col:03d}'


def select_order(cells, seed):
    """Seeded permutation of the eligible cells; the first N that pass the native alpha check become windows."""
    eligible = sorted((c['row'], c['col']) for c in cells if c['eligible'])
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(eligible)) if eligible else []
    return [{'row': eligible[i][0], 'col': eligible[i][1], 'id': window_id(*eligible[i])} for i in order]


def select(root='.', n=DEFAULT_N, seed=DEFAULT_SEED, rule=None):
    root = Path(root)
    rule = {**OVERVIEW_RULE, **(rule or {})}
    meta = common.read_json(root / CACHE / f"overview-r{rule['reduce']}.json")
    rgba = np.asarray(Image.open(root / CACHE / f"overview-r{meta['reduce']}.png"))
    geo = meta['geo']
    size = native_window_px(geo['gsd_m'])
    cell = size / meta['step']
    masks = classify_overview(rgba, rule)
    cells = grid_cells(masks, cell, rule)
    order = select_order(cells, seed)
    reasons = {}
    for c in cells:
        for r in c['reasons']:
            reasons[r] = reasons.get(r, 0) + 1
    result = {'version': VERSION, 'seed': seed, 'n': n, 'rule': rule, 'native_window_px': size,
              'overview_cell_px': cell, 'overview_sha256': meta['sha256'], 'geo': geo,
              'rule_text': ('fixed grid of window-sized cells from the ortho top-left; on the 1/2**reduce overview (3.2 m '
                            f"pixels) a cell is eligible when >= {rule['inside_min']:.1%} of it is imagery (not the no-data fill "
                            f"{tuple(rule['nodata_rgb'])} within {rule['nodata_tol']:g}), at most {rule['water_max']:.0%} of that "
                            f"is water (3x3 luminance std < {rule['water_std_max']:g} and teal: G - R > "
                            f"{rule['water_g_minus_r']:g}, B - R > {rule['water_b_minus_r']:g}), at most {rule['veg_max']:.0%} "
                            f"is vegetation (ExG > {rule['exg_veg']}), and at least {rule['structure_min']:.0%} is structure "
                            f"(neither, with Sobel gradient >= {rule['edge_grad_min']:g} grey levels per overview pixel). "
                            'Thresholds were set from overview pixel samples of named land covers before any SAM3 run. Rail '
                            'yards and industrial land pass; parks, water and container stacks mostly fail. Windows are the '
                            'eligible cells in a seeded permutation; a cell whose native crop is not at least 99.9 % imagery '
                            'is skipped for the next one.'),
              'counts': {'cells': len(cells), 'eligible': sum(c['eligible'] for c in cells), 'rejected_by_reason': reasons},
              'order': order, 'cells': cells}
    common.write_json_atomic(root / TEACHER / 'windows.json', result)
    selection_map(root, rgba, cells, cell, order[:n])
    return {k: v for k, v in result.items() if k not in ('cells', 'order')}


def selection_map(root, rgba, cells, cell, chosen):
    image = Image.fromarray(np.asarray(rgba)[..., :3]).convert('RGB')
    scale = min(1., 2400 / max(image.size))
    if scale < 1:
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.BILINEAR)
    draw = ImageDraw.Draw(image)
    picked = {(c['row'], c['col']) for c in chosen}
    for c in cells:
        x0, y0 = c['col'] * cell * scale, c['row'] * cell * scale
        box = [x0, y0, x0 + cell * scale, y0 + cell * scale]
        if (c['row'], c['col']) in picked:
            draw.rectangle(box, outline=(0, 255, 0), width=3)
        elif c['eligible']:
            draw.rectangle(box, outline=(255, 255, 255), width=1)
        elif c['inside'] > .5:
            draw.rectangle(box, outline=(200, 40, 40), width=1)
    path = Path(root) / TEACHER / 'selection_map.png'
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def native_path(root, wid):
    return Path(root) / CACHE / 'windows' / f'{wid}.png'


def crop(root='.', limit=None):
    """Read native crops for the first N selected cells whose crop is fully inside the imagery (alpha everywhere)."""
    root = Path(root)
    windows = common.read_json(root / TEACHER / 'windows.json')
    glymur = open_glymur(root)
    jp2 = glymur.Jp2k(str(jp2_path(root)))
    size, geo, n = windows['native_window_px'], windows['geo'], windows['n'] if limit is None else limit
    accepted, skipped = [], []
    for entry in windows['order']:
        if len(accepted) >= n:
            break
        path = native_path(root, entry['id'])
        meta_path = path.with_suffix('.json')
        if path.exists() and meta_path.exists():
            meta = common.read_json(meta_path)
        else:
            started = time.monotonic()
            array = read_native(jp2, entry['row'], entry['col'], size)
            seconds = time.monotonic() - started
            inside = float(inside_mask(array[..., :3], windows['rule']).mean())
            meta = {'id': entry['id'], 'row': entry['row'], 'col': entry['col'], 'shape': list(array.shape),
                    'inside_fraction': round(inside, 6), 'read_seconds': round(seconds, 2)}
            if inside >= .999:
                meta['sha256'] = save_png(path, np.ascontiguousarray(array[..., :3]))
            common.write_json_atomic(meta_path, meta)
        if meta['inside_fraction'] >= .999 and path.exists():
            native_affine, student_affine, student_m = window_affines(geo, entry['row'], entry['col'], size)
            accepted.append({**meta, 'native_affine': native_affine, 'student_affine': student_affine,
                             'student_px_m': student_m, 'native_file': str(path.relative_to(root))})
        else:
            skipped.append({'id': entry['id'], 'inside_fraction': meta['inside_fraction']})
    result = {'version': VERSION, 'native_window_px': size, 'gsd_m': geo['gsd_m'], 'windows': accepted,
              'skipped_partial': skipped, 'openjpeg': jp2_openjpeg_version()}
    common.write_json_atomic(root / TEACHER / 'crops.json', result)
    return {'accepted': len(accepted), 'skipped_partial': len(skipped)}


# --- student image -------------------------------------------------------------------------------------------------

def area_matrix(n_in, n_out):
    """(n_out, n_in) exact area weights: output pixel j covers input span [j f, (j + 1) f), f = n_in / n_out."""
    f = n_in / n_out
    starts = np.arange(n_out)[:, None] * f
    edges = np.arange(n_in)[None, :]
    overlap = np.clip(np.minimum(edges + 1, starts + f) - np.maximum(edges, starts), 0., None)
    return (overlap / f).astype(np.float64)


def area_downsample(array, out_h, out_w, matrices=None):
    """Area-averaged resample of an HxW or HxWxC array (float64)."""
    array = np.asarray(array, dtype=np.float64)
    dy, dx = matrices or (area_matrix(array.shape[0], out_h), area_matrix(array.shape[1], out_w))
    if array.ndim == 2:
        return dy @ array @ dx.T
    return np.stack([dy @ array[..., c] @ dx.T for c in range(array.shape[-1])], axis=-1)


def gaussian_blur(image, sigma):
    import cv2
    image = np.asarray(image, dtype=np.float32)
    if sigma <= 0:
        return image
    return cv2.GaussianBlur(image, (0, 0), sigmaX=float(sigma), sigmaY=float(sigma), borderType=cv2.BORDER_REFLECT_101)


def make_student(native_rgb, sigma, size=STUDENT_PX, matrices=None):
    down = area_downsample(native_rgb, size, size, matrices)
    return np.clip(np.rint(gaussian_blur(down, sigma)), 0, 255).astype(np.uint8)


def gradient_energy_ratio(gray):
    """imagery_sources.gradient_energy of the image over that of its 1 px Gaussian blur.

    Contrast cancels; for 1/f^2 scene statistics the ratio depends on the image's own blur, not on what is in it
    (Gaussian blur s px: ratio ~ (s^2 + 1) / s^2 before the Nyquist cut-off), so a Mooroolbark tile and an inner-city
    window can be compared. Higher is sharper."""
    from ..imagery_sources import gradient_energy
    gray = np.asarray(gray, dtype=np.float32)
    sharp = gradient_energy(gray, 1.)['per_px2']
    soft = gradient_energy(gaussian_blur(gray, 1.), 1.)['per_px2']
    return sharp / soft if soft else None


def unsharp(image, amount, sigma=1.):
    image = np.asarray(image, dtype=np.float32)
    return np.clip(np.rint(image + amount * (image - gaussian_blur(image, sigma))), 0, 255).astype(np.uint8)


def to_gray(rgb):
    from ..imagery_sources import to_gray as convert
    return convert(np.asarray(rgb, dtype=np.uint8))


def match_sigma(curve, target):
    """Sigma where a decreasing (sigma, ratio) curve crosses target, linearly interpolated; clamped to the grid."""
    sigmas = np.array([s for s, _ in curve], dtype=float)
    ratios = np.array([r for _, r in curve], dtype=float)
    if target >= ratios[0]:
        return float(sigmas[0]), 'clamped_low (student already softer than target at sigma 0)'
    if target <= ratios[-1]:
        return float(sigmas[-1]), 'clamped_high'
    i = int(np.nonzero(ratios <= target)[0][0])
    t = (ratios[i - 1] - target) / (ratios[i - 1] - ratios[i])
    return float(sigmas[i - 1] + t * (sigmas[i] - sigmas[i - 1])), 'interpolated'


def saved_tiles(root, count=CALIBRATION_SAVED, seed=DEFAULT_SEED):
    """Seeded sample of 1152 px suburb scan tiles (the saved Google grid) with their PNG path."""
    paths = []
    for latest in sorted((Path(root) / 'data/scenes').glob('mooroolbark-*-17-*/latest.json')):
        scene = common.read_json(latest)
        if scene.get('image_width') != STUDENT_PX or scene.get('image_height') != STUDENT_PX:
            continue
        png = latest.parent / f"{scene.get('image_sha256')}.png"
        if png.exists():
            paths.append((scene['id'], png, common.pixel_size_m(scene)))
    rng = np.random.default_rng(seed)
    pick = sorted(rng.choice(len(paths), min(count, len(paths)), replace=False)) if paths else []
    return [paths[i] for i in pick]


def _spread(values):
    values = np.array([v for v in values if v is not None], dtype=float)
    if not values.size:
        return {'n': 0, 'median': None, 'q25': None, 'q75': None}
    return {'n': int(values.size), 'median': _num(np.median(values)), 'q25': _num(np.percentile(values, 25)),
            'q75': _num(np.percentile(values, 75))}


def calibrate(root='.', seed=DEFAULT_SEED, n_saved=CALIBRATION_SAVED, n_students=CALIBRATION_STUDENTS):
    """Blur sigma (student px) matching the saved tiles' median gradient-energy ratio; edge width cross-check."""
    from ..imagery_sources import edge_spread, gradient_energy
    root = Path(root)
    crops = common.read_json(root / TEACHER / 'crops.json')
    saved_rows = []
    for scene_id, png, px_m in saved_tiles(root, n_saved, seed):
        gray = to_gray(np.asarray(Image.open(png).convert('RGB')))
        edges = edge_spread(gray, px_m)
        saved_rows.append({'scene_id': scene_id, 'ratio': _num(gradient_energy_ratio(gray)),
                           'ge_per_m2': gradient_energy(gray, px_m)['per_m2'], 'edge_width_m': edges.get('edge_width_10_90_m')})
    windows = crops['windows'][:n_students]
    size = crops['native_window_px']
    matrices = (area_matrix(size, STUDENT_PX), area_matrix(size, STUDENT_PX))
    downs, natives = [], []
    for window in windows:
        native = np.asarray(Image.open(root / window['native_file']).convert('RGB'))
        natives.append({'id': window['id'], 'edges': edge_spread(to_gray(native), crops['gsd_m'])})
        downs.append((window, area_downsample(native, STUDENT_PX, STUDENT_PX, matrices)))
    curve, per_sigma = [], {}
    for sigma in SIGMA_GRID:
        ratios = [gradient_energy_ratio(to_gray(np.clip(np.rint(gaussian_blur(d, sigma)), 0, 255).astype(np.uint8)))
                  for _, d in downs]
        per_sigma[str(sigma)] = _spread(ratios)
        curve.append((sigma, float(np.median(ratios))))
    target = float(np.median([r['ratio'] for r in saved_rows if r['ratio'] is not None]))
    sigma, how = match_sigma(curve, target)
    sigma = round(sigma, 3)
    check = []
    for window, down in downs:
        student = np.clip(np.rint(gaussian_blur(down, sigma)), 0, 255).astype(np.uint8)
        gray = to_gray(student)
        check.append({'id': window['id'], 'ratio': _num(gradient_energy_ratio(gray)),
                      'ge_per_m2': gradient_energy(gray, window['student_px_m'])['per_m2'],
                      'edge_width_m': edge_spread(gray, window['student_px_m']).get('edge_width_10_90_m')})
    sharpening = None
    if how.startswith('clamped_low'):
        # No blur can make the student sharper. Report (never apply) the unsharp mask that would match the target.
        amounts = [round(a, 2) for a in np.arange(0., 3.01, .1)]
        sharp_curve = []
        for amount in amounts:
            ratios = [gradient_energy_ratio(to_gray(unsharp(d, amount))) for _, d in downs]
            sharp_curve.append((amount, float(np.median(ratios))))
        flipped = [(a, -r) for a, r in sharp_curve]       # increasing ratio -> decreasing for match_sigma
        amount, amount_how = match_sigma(flipped, -target)
        widths = [edge_spread(to_gray(unsharp(d, amount)), w['student_px_m']).get('edge_width_10_90_m') for w, d in downs]
        sharpening = {'applied': False, 'kernel': 'unsharp mask, Gaussian sigma 1 px: I + a (I - G*I)',
                      'amount_matching_ratio': round(amount, 3), 'match': amount_how,
                      'edge_width_m_at_amount': _spread(widths), 'curve': [[a, _num(r)] for a, r in sharp_curve]}
    unblurred = [{'ge_per_m2': gradient_energy(to_gray(np.clip(np.rint(d), 0, 255).astype(np.uint8)),
                                               w['student_px_m'])['per_m2']} for w, d in downs]
    result = {'version': VERSION, 'method': ('gradient-energy ratio: imagery_sources.gradient_energy of the image over '
                                             'that of the same image after a 1 px Gaussian (contrast- and largely '
                                             'content-invariant); sigma (student px) interpolated where the median student '
                                             'ratio over the sigma grid crosses the median ratio of the saved tiles'),
              'sigma_px': sigma, 'sigma_m': _num(sigma * windows[0]['student_px_m'] if windows else None),
              'match': how, 'target_ratio': _num(target), 'curve': [[s, _num(r)] for s, r in curve],
              'per_sigma': per_sigma, 'saved': {'n': len(saved_rows), 'ratio': _spread([r['ratio'] for r in saved_rows]),
                                                'ge_per_m2': _spread([r['ge_per_m2'] for r in saved_rows]),
                                                'edge_width_m': _spread([r['edge_width_m'] for r in saved_rows]),
                                                'rows': saved_rows},
              'student_at_sigma': {'n': len(check), 'ratio': _spread([r['ratio'] for r in check]),
                                   'ge_per_m2': _spread([r['ge_per_m2'] for r in check]),
                                   'edge_width_m': _spread([r['edge_width_m'] for r in check]), 'rows': check},
              'student_unblurred_ge_per_m2': _spread([r['ge_per_m2'] for r in unblurred]),
              'sharpening_that_would_match': sharpening,
              'native_edge_width_m': _spread([n['edges'].get('edge_width_10_90_m') for n in natives]),
              'native_smallest_bar_m': _spread([n['edges'].get('smallest_resolvable_bar_m') for n in natives]),
              'note': ('Absolute gradient energy per m2 differs between inner-city windows and suburb tiles because content '
                       'differs; it is reported for transparency, the ratio is what is matched. JPEG block artefacts of the '
                       'saved tiles are not simulated and add to their gradient energy. OpenCV samples the Gaussian kernel, so '
                       'sigmas below about 0.3 px leave the image unchanged (flat start of the curve).')}
    common.write_json_atomic(root / TEACHER / 'blur_calibration.json', result)
    return {k: v for k, v in result.items() if k not in ('saved', 'student_at_sigma', 'per_sigma')}


# --- masks: pieces, stitching, dedupe ------------------------------------------------------------------------------

@dataclass
class Piece:
    """A bool mask living at (x0, y0) of a canvas; tile is the sub-tile (row, col) it came from."""
    x0: int
    y0: int
    mask: np.ndarray
    score: float
    tile: tuple | None = None
    members: list = field(default_factory=list)

    @property
    def x1(self):
        return self.x0 + self.mask.shape[1]

    @property
    def y1(self):
        return self.y0 + self.mask.shape[0]

    @property
    def area(self):
        return int(self.mask.sum())


def piece_from_mask(mask, x0=0, y0=0, score=0., tile=None):
    """Crop a full (or offset) bool mask to its bounding box; None when empty."""
    mask = np.asarray(mask, dtype=bool)
    ys, xs = np.nonzero(mask.any(axis=1))[0], np.nonzero(mask.any(axis=0))[0]
    if not len(ys):
        return None
    crop = mask[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]
    return Piece(int(x0 + xs[0]), int(y0 + ys[0]), np.ascontiguousarray(crop), float(score), tile)


def _window(piece, x0, y0, x1, y1):
    """The piece's mask restricted to canvas box [x0, x1) x [y0, y1), as a full box-sized array."""
    out = np.zeros((max(y1 - y0, 0), max(x1 - x0, 0)), bool)
    ix0, iy0 = max(piece.x0, x0), max(piece.y0, y0)
    ix1, iy1 = min(piece.x1, x1), min(piece.y1, y1)
    if ix1 > ix0 and iy1 > iy0:
        out[iy0 - y0:iy1 - y0, ix0 - x0:ix1 - x0] = piece.mask[iy0 - piece.y0:iy1 - piece.y0, ix0 - piece.x0:ix1 - piece.x0]
    return out


def intersection(a, b):
    x0, y0, x1, y1 = max(a.x0, b.x0), max(a.y0, b.y0), min(a.x1, b.x1), min(a.y1, b.y1)
    if x1 <= x0 or y1 <= y0:
        return 0
    return int((a.mask[y0 - a.y0:y1 - a.y0, x0 - a.x0:x1 - a.x0] & b.mask[y0 - b.y0:y1 - b.y0, x0 - b.x0:x1 - b.x0]).sum())


def iou(a, b, inter=None):
    inter = intersection(a, b) if inter is None else inter
    union = a.area + b.area - inter
    return inter / union if union else 0.


def subtile_origins(size, tile=SUBTILE, min_overlap=MIN_OVERLAP):
    """Evenly spaced sub-tile origins covering [0, size) with at least min_overlap px shared by neighbours."""
    if size <= tile:
        return [0]
    count = math.ceil((size - tile) / (tile - min_overlap)) + 1
    return [int(round(v)) for v in np.linspace(0, size - tile, count)]


def core_bounds(origins, tile, size):
    """1-D core interval per sub-tile: the overlap between neighbours is split at its midpoint."""
    cuts = [0] + [int(round((origins[k] + origins[k - 1] + tile) / 2)) for k in range(1, len(origins))] + [size]
    return [(cuts[k], cuts[k + 1]) for k in range(len(origins))]


class _Union:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def stitch(pieces, origins, tile, size, merge_iou=MERGE_IOU, merge_min_px=1):
    """Merge sub-tile pieces of one building into one mask. Returns (pieces, stats).

    Two pieces from different sub-tiles link when both have >= merge_min_px inside the strip their sub-tiles share
    and their IoU inside that strip is >= merge_iou. In a linked group each pixel is decided by the member whose
    sub-tile core holds it (so an overlap is never double counted); pixels in the core of a sub-tile without a member
    take the union of the members that saw them (a building SAM3 missed in one sub-tile is kept, not cut)."""
    cores = core_bounds(origins, tile, size)
    extent = {(r, c): (origins[c], origins[r], origins[c] + tile, origins[r] + tile)
              for r in range(len(origins)) for c in range(len(origins))}
    union = _Union(len(pieces))
    links = 0
    by_tile = {}
    for i, p in enumerate(pieces):
        by_tile.setdefault(p.tile, []).append(i)
    tiles = sorted(by_tile)
    for a_index, ta in enumerate(tiles):
        for tb in tiles[a_index + 1:]:
            ea, eb = extent[ta], extent[tb]
            box = (max(ea[0], eb[0]), max(ea[1], eb[1]), min(ea[2], eb[2]), min(ea[3], eb[3]))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            clipped_a = [(i, _window(pieces[i], *box)) for i in by_tile[ta]
                         if pieces[i].x0 < box[2] and pieces[i].x1 > box[0] and pieces[i].y0 < box[3] and pieces[i].y1 > box[1]]
            clipped_b = [(j, _window(pieces[j], *box)) for j in by_tile[tb]
                         if pieces[j].x0 < box[2] and pieces[j].x1 > box[0] and pieces[j].y0 < box[3] and pieces[j].y1 > box[1]]
            clipped_a = [(i, m, int(m.sum())) for i, m in clipped_a]
            clipped_b = [(j, m, int(m.sum())) for j, m in clipped_b]
            for i, ma, na in clipped_a:
                if na < merge_min_px:
                    continue
                for j, mb, nb in clipped_b:
                    if nb < merge_min_px:
                        continue
                    inter = int((ma & mb).sum())
                    if inter and inter / (na + nb - inter) >= merge_iou:
                        union.join(i, j)
                        links += 1
    groups = {}
    for i in range(len(pieces)):
        groups.setdefault(union.find(i), []).append(i)
    merged = []
    for members in groups.values():
        if len(members) == 1:
            p = pieces[members[0]]
            merged.append(Piece(p.x0, p.y0, p.mask, p.score, p.tile, [p.tile]))
            continue
        group = [pieces[i] for i in members]
        x0, y0 = min(p.x0 for p in group), min(p.y0 for p in group)
        x1, y1 = max(p.x1 for p in group), max(p.y1 for p in group)
        value = np.zeros((y1 - y0, x1 - x0), bool)
        decided = np.zeros_like(value)
        anyone = np.zeros_like(value)
        for p in group:
            seen = _window(p, x0, y0, x1, y1)
            anyone |= seen
            (cy0, cy1), (cx0, cx1) = cores[p.tile[0]], cores[p.tile[1]]
            core = np.zeros_like(value)
            core[max(cy0 - y0, 0):max(cy1 - y0, 0), max(cx0 - x0, 0):max(cx1 - x0, 0)] = True
            value |= seen & core
            decided |= core
        value |= anyone & ~decided
        piece = piece_from_mask(value, x0, y0, max(p.score for p in group))
        if piece is not None:
            piece.members = sorted({p.tile for p in group})
            merged.append(piece)
    return merged, {'pieces_in': len(pieces), 'links': links, 'groups_merged': sum(len(m) > 1 for m in groups.values()),
                    'pieces_out': len(merged)}


def suppress(pieces, dup_iou=DUP_IOU, contain=DUP_CONTAIN):
    """scene_extract's building rule: largest first; drop IoU > .8 with a kept piece or > 95 % inside one."""
    kept, dropped = [], 0
    for p in sorted(pieces, key=lambda q: (-q.area, -q.score, q.y0, q.x0)):
        duplicate = False
        for k in kept:
            if p.x0 >= k.x1 or k.x0 >= p.x1 or p.y0 >= k.y1 or k.y0 >= p.y1:
                continue
            inter = intersection(p, k)
            if not inter:
                continue
            if iou(p, k, inter) > dup_iou or inter / max(p.area, 1) > contain:
                duplicate = True
                break
        if duplicate:
            dropped += 1
        else:
            kept.append(p)
    return kept, dropped


def proposals_to_pieces(proposals, x0=0, y0=0, tile=None, min_px=1):
    pieces = []
    for proposal in proposals:
        piece = piece_from_mask(proposal.mask, x0, y0, proposal.score, tile)
        if piece is not None and piece.area >= min_px:
            pieces.append(piece)
    return pieces


def downsample_piece(piece, dy, dx, threshold=.5):
    """Area-average a native piece onto the student grid (dy, dx: area matrices); keep cells covered >= threshold."""
    rows = np.nonzero(dy[:, piece.y0:piece.y1].sum(axis=1) > 0)[0]
    cols = np.nonzero(dx[:, piece.x0:piece.x1].sum(axis=1) > 0)[0]
    soft = dy[rows[0]:rows[-1] + 1, piece.y0:piece.y1] @ piece.mask.astype(np.float64) @ dx[cols[0]:cols[-1] + 1, piece.x0:piece.x1].T
    return piece_from_mask(soft >= threshold - 1e-9, int(cols[0]), int(rows[0]), piece.score, piece.tile)


def index_map(pieces, height, width):
    """uint16 instance map (larger first, so a smaller overlapping instance keeps its own pixels)."""
    label = np.zeros((height, width), np.uint16)
    for index, p in sorted(enumerate(pieces, start=1), key=lambda item: -item[1].area):
        region = label[p.y0:p.y1, p.x0:p.x1]
        region[p.mask] = index
    return label


# --- COCO RLE (uncompressed, column-major) -------------------------------------------------------------------------

def rle_encode(piece, height, width):
    """COCO uncompressed RLE of a piece on a (height, width) canvas: counts alternate 0-runs and 1-runs, Fortran order."""
    block = np.zeros((height, piece.mask.shape[1]), bool)
    block[piece.y0:piece.y1] = piece.mask
    flat = block.flatten(order='F')
    changes = np.flatnonzero(np.diff(np.r_[0, flat.astype(np.int8), 0]))
    offset = piece.x0 * height
    boundaries = np.r_[0, changes + offset, height * width]
    counts = np.diff(boundaries).astype(int).tolist()
    return {'size': [height, width], 'counts': counts}


def rle_decode(rle):
    height, width = rle['size']
    counts = np.asarray(rle['counts'], dtype=np.int64)
    values = np.zeros(len(counts), bool)
    values[1::2] = True
    return np.repeat(values, counts).reshape((width, height)).T


def rle_piece(rle, bbox=None, score=0.):
    if bbox is None:
        return piece_from_mask(rle_decode(rle), score=score)
    x0, y0, w, h = bbox
    return Piece(x0, y0, np.ascontiguousarray(rle_decode(rle)[y0:y0 + h, x0:x0 + w]), score)


# --- per-window teaching -------------------------------------------------------------------------------------------

def run_config(sigma, gsd_m):
    return {'version': VERSION, 'prompt': PROMPT, 'threshold': THRESHOLD, 'max_proposals': MAX_PROPOSALS,
            'subtile': SUBTILE, 'min_overlap': MIN_OVERLAP, 'min_area_m2': MIN_AREA_M2, 'merge_iou': MERGE_IOU,
            'merge_min_m2': MERGE_MIN_M2, 'dup_iou': DUP_IOU, 'dup_contain': DUP_CONTAIN, 'sigma_px': sigma,
            'gsd_m': gsd_m, 'student_px': STUDENT_PX, 'downsample_threshold': .5}


def sam3_pieces(segmenter, image, x0=0, y0=0, tile=None, min_px=1):
    features = segmenter.encode_image(Image.fromarray(image))
    proposals = segmenter.proposals(features, PROMPT, (image.shape[1], image.shape[0]), threshold=THRESHOLD,
                                    max_proposals=MAX_PROPOSALS)
    pieces = proposals_to_pieces(proposals, x0, y0, tile, min_px)
    return pieces, len(proposals)


def teach_window(native, sigma, gsd_m, segmenter, matrices=None):
    """Teacher and control pieces for one window. Returns (student, teacher_native, teacher_student, control, stats)."""
    size = native.shape[0]
    student_m = size * gsd_m / STUDENT_PX
    matrices = matrices or (area_matrix(size, STUDENT_PX), area_matrix(size, STUDENT_PX))
    timings = {}
    started = time.monotonic()
    student = make_student(native, sigma, STUDENT_PX, matrices)
    timings['student_s'] = time.monotonic() - started
    started = time.monotonic()
    origins = subtile_origins(size)
    raw, per_tile = [], []
    min_native = max(1, int(round(MIN_AREA_M2 / gsd_m ** 2)))
    for r, oy in enumerate(origins):
        for c, ox in enumerate(origins):
            tile = np.ascontiguousarray(native[oy:oy + SUBTILE, ox:ox + SUBTILE])
            pieces, count = sam3_pieces(segmenter, tile, ox, oy, (r, c), min_native)
            raw.extend(pieces)
            per_tile.append({'tile': [r, c], 'origin': [ox, oy], 'proposals': count, 'kept': len(pieces),
                             'at_cap': count >= MAX_PROPOSALS})
    timings['teacher_sam3_s'] = time.monotonic() - started
    started = time.monotonic()
    merged, stitch_stats = stitch(raw, origins, SUBTILE, size, MERGE_IOU, max(1, int(round(MERGE_MIN_M2 / gsd_m ** 2))))
    teacher, teacher_dups = suppress(merged)
    teacher.sort(key=lambda p: (p.y0, p.x0, -p.area))
    min_student = max(1, int(round(MIN_AREA_M2 / student_m ** 2)))
    dy, dx = matrices
    downs = [downsample_piece(p, dy, dx) for p in teacher]
    teacher_student = [(i, d) for i, d in enumerate(downs) if d is not None and d.area >= min_student]
    timings['stitch_s'] = time.monotonic() - started
    started = time.monotonic()
    control_raw, control_count = sam3_pieces(segmenter, student, 0, 0, None, min_student)
    control, control_dups = suppress(control_raw)
    control.sort(key=lambda p: (p.y0, p.x0, -p.area))
    ranks = sorted(range(len(control_raw)), key=lambda i: -control_raw[i].score)
    beyond_cap = {id(control_raw[i]) for i in ranks[PRODUCTION_CAP:]}
    timings['control_sam3_s'] = time.monotonic() - started
    stats = {'subtiles': per_tile, 'subtile_origins': origins, 'teacher_raw_pieces': len(raw), **stitch_stats,
             'teacher_duplicates_dropped': teacher_dups, 'teacher_instances': len(teacher),
             'teacher_student_instances': len(teacher_student),
             'teacher_lost_in_downsample': len(teacher) - len(teacher_student),
             'control_proposals': control_count, 'control_at_cap': control_count >= MAX_PROPOSALS,
             'control_duplicates_dropped': control_dups, 'control_instances': len(control),
             'control_kept_beyond_production_cap': sum(id(p) in beyond_cap for p in control),
             'min_area_px': {'native': min_native, 'student': min_student},
             'timings': {k: round(v, 2) for k, v in timings.items()}}
    return student, teacher, teacher_student, control, stats


def window_dir(root, wid):
    return Path(root) / TEACHER / 'windows' / wid


def write_window(root, window, config, student, teacher, teacher_student, control, stats):
    folder = window_dir(root, window['id'])
    size = window['shape'][0]
    shas = {'student': save_png(folder / 'student.png', student),
            'teacher_native_mask': save_png(folder / 'teacher_native.png', index_map(teacher, size, size)),
            'teacher_student_mask': save_png(folder / 'teacher_student.png',
                                             index_map([d for _, d in teacher_student], STUDENT_PX, STUDENT_PX)),
            'control_mask': save_png(folder / 'control.png', index_map(control, STUDENT_PX, STUDENT_PX))}
    downs = dict(teacher_student)
    teacher_rows = []
    for i, p in enumerate(teacher):
        row = {'index': i + 1, 'score': round(p.score, 4), 'bbox_native': [p.x0, p.y0, p.mask.shape[1], p.mask.shape[0]],
               'area_native_px': p.area, 'subtiles': [list(t) for t in p.members], 'rle_native': rle_encode(p, size, size)}
        d = downs.get(i)
        row['student'] = None if d is None else {'bbox': [d.x0, d.y0, d.mask.shape[1], d.mask.shape[0]], 'area_px': d.area,
                                                 'rle': rle_encode(d, STUDENT_PX, STUDENT_PX)}
        teacher_rows.append(row)
    control_rows = [{'index': i + 1, 'score': round(p.score, 4), 'bbox': [p.x0, p.y0, p.mask.shape[1], p.mask.shape[0]],
                     'area_px': p.area, 'rle': rle_encode(p, STUDENT_PX, STUDENT_PX)} for i, p in enumerate(control)]
    record = {'version': VERSION, 'config': config, 'config_hash': config_hash(config), 'window': window,
              'files': {'native': window['native_file'], 'student': 'student.png', 'teacher_native_mask': 'teacher_native.png',
                        'teacher_student_mask': 'teacher_student.png', 'control_mask': 'control.png'},
              'sha256': {'native': window.get('sha256'), **shas}, 'stats': stats,
              'teacher': teacher_rows, 'control': control_rows, 'computed_at': common.now_iso()}
    common.write_json_atomic(folder / 'record.json', record)
    return record


def load_segmenter(root):
    from ..sam3 import Sam3TextSegmenter
    return Sam3TextSegmenter(Path(root) / MODELS)


def teach(root='.', shard=(0, 1), limit=None, segmenter=None):
    root = Path(root)
    crops = common.read_json(root / TEACHER / 'crops.json')
    calibration = common.read_json(root / TEACHER / 'blur_calibration.json')
    sigma, gsd = calibration['sigma_px'], crops['gsd_m']
    config = run_config(sigma, gsd)
    windows = crops['windows'][:limit] if limit else crops['windows']
    size = crops['native_window_px']
    matrices = (area_matrix(size, STUDENT_PX), area_matrix(size, STUDENT_PX))
    done, skipped = [], []
    k, n = shard
    for index, window in enumerate(windows):
        if index % n != k:
            continue
        record_path = window_dir(root, window['id']) / 'record.json'
        cached = common.read_json(record_path) if record_path.exists() else None
        if (cached and cached.get('config_hash') == config_hash(config)
                and cached.get('sha256', {}).get('native') == window.get('sha256')
                and cached.get('window', {}).get('native_affine') == window.get('native_affine')):
            skipped.append(window['id'])
            continue
        segmenter = segmenter or load_segmenter(root)
        native = np.asarray(Image.open(root / window['native_file']).convert('RGB'))
        started = time.monotonic()
        student, teacher, teacher_student, control, stats = teach_window(native, sigma, gsd, segmenter, matrices)
        stats['timings']['window_total_s'] = round(time.monotonic() - started, 2)
        stats['timings']['native_read_s'] = window.get('read_seconds')
        write_window(root, window, config, student, teacher, teacher_student, control, stats)
        done.append(window['id'])
        print(json.dumps({'window': window['id'], 'teacher': stats['teacher_instances'], 'control': stats['control_instances'],
                          'seconds': stats['timings']['window_total_s']}), flush=True)
    return {'done': len(done), 'skipped_existing': len(skipped)}


# --- finalize: manifest, COCO, provenance --------------------------------------------------------------------------

def load_records(root):
    crops = common.read_json(Path(root) / TEACHER / 'crops.json')
    records = []
    for window in crops['windows']:
        path = window_dir(root, window['id']) / 'record.json'
        if path.exists():
            records.append(common.read_json(path))
    return crops, records


def exterior_polygon(piece, simplify_px=SIMPLIFY_PX):
    """Largest part of mask_to_polygons (repo vectoriser), shifted into canvas pixels; None when empty."""
    polygons = mask_to_polygons(piece.mask, min_area_px=1., simplify_px=simplify_px)
    if not polygons:
        return None
    return affinity.translate(max(polygons, key=lambda p: p.area), piece.x0, piece.y0)


def provenance(root, geo=None, jp2=None):
    root = Path(root)
    downloaded = root / CACHE / 'provenance.json'
    log = root / CACHE / 'download.log'
    return {**SOURCE, 'download_provenance': common.read_json(downloaded) if downloaded.exists() else None,
            'download_log': log.read_text().strip() if log.exists() else None,
            'file': SOURCE.get('file', ZIP_NAME), 'georeference': geo, 'jp2': jp2,
            'processing': SOURCE.get('processing') or (
                'windows read with openjpeg (glymur); student = exact area average onto 1152 px then Gaussian '
                'blur; teacher = SAM3 text ONNX (int8, scripts/ml/models/sam3) on native 1152 px sub-tiles; '
                'control = the same SAM3 on the student image'),
            'labels_are': 'SAM3 model outputs, not human labels (tier T3)'}


def finalize(root='.'):
    root = Path(root)
    crops, records = load_records(root)
    windows = common.read_json(root / TEACHER / 'windows.json')
    calibration = common.read_json(root / TEACHER / 'blur_calibration.json')
    overview_path = root / CACHE / f"overview-r{windows['rule']['reduce']}.json"
    overview_meta = common.read_json(overview_path) if overview_path.exists() else {}
    geo = windows['geo']
    size = crops['native_window_px']
    student_m = size * geo['gsd_m'] / STUDENT_PX
    category = common.CATEGORY
    coco = {name: {'info': {'version': VERSION, 'kind': name, 'created_at': common.now_iso()}, 'images': [],
                   'annotations': [], 'categories': [category]} for name in ('teacher_native', 'teacher_student', 'control')}
    manifest_windows, timings = [], []
    current = {w['id']: w for w in crops['windows']}       # crops.json may carry fields the record predates (shared cell ids)
    for image_id, record in enumerate(records, start=1):
        w = {**record['window'], **{k: v for k, v in current.get(record['window']['id'], {}).items()
                                    if k in ('shared_id', 'shared_with', 'centre_lonlat', 'crs', 'bounds')}}
        east, north = w['native_affine'][0] + size * geo['gsd_m'] / 2, w['native_affine'][3] - size * geo['gsd_m'] / 2
        crs = str(w.get('crs') or geo.get('crs') or SOURCE.get('crs') or 'EPSG:28355')
        epsg = re.search(r'(\d{4,5})', crs)
        if w.get('centre_lonlat'):
            lon, lat = w['centre_lonlat']
        elif epsg and epsg.group(1) in ('28355', '7855'):
            lon, lat = mga55_to_lonlat(east, north)     # the module's own inverse transverse Mercator, zone 55 only
        else:
            raise SystemExit(f"window {w['id']} in {crs} has no centre_lonlat: re-run teacher_sources crop for this source")
        folder = f"windows/{w['id']}"
        coco['teacher_native']['images'].append({'id': image_id, 'file_name': w['native_file'], 'width': size, 'height': size,
                                                 'window_id': w['id'], 'pixel_size_m': geo['gsd_m'], 'sha256': w.get('sha256')})
        for name in ('teacher_student', 'control'):
            coco[name]['images'].append({'id': image_id, 'file_name': f'{folder}/student.png', 'width': STUDENT_PX,
                                         'height': STUDENT_PX, 'window_id': w['id'], 'pixel_size_m': round(student_m, 6),
                                         'sha256': record['sha256']['student']})
        for row in record['teacher']:
            piece = rle_piece(row['rle_native'], row['bbox_native'])
            polygon = exterior_polygon(piece)
            coco['teacher_native']['annotations'].append(_coco_row(len(coco['teacher_native']['annotations']) + 1, image_id,
                                                                   row['rle_native'], row['bbox_native'], row['area_native_px'],
                                                                   row['score'], polygon, geo['gsd_m'], w['id'], row['index']))
            if row['student']:
                spiece = rle_piece(row['student']['rle'], row['student']['bbox'])
                coco['teacher_student']['annotations'].append(_coco_row(
                    len(coco['teacher_student']['annotations']) + 1, image_id, row['student']['rle'], row['student']['bbox'],
                    row['student']['area_px'], row['score'], exterior_polygon(spiece), student_m, w['id'], row['index']))
        for row in record['control']:
            piece = rle_piece(row['rle'], row['bbox'])
            coco['control']['annotations'].append(_coco_row(len(coco['control']['annotations']) + 1, image_id, row['rle'],
                                                            row['bbox'], row['area_px'], row['score'], exterior_polygon(piece),
                                                            student_m, w['id'], row['index']))
        t = record['stats']['timings']
        timings.append(t)
        manifest_windows.append({'id': w['id'], 'grid': [w['row'], w['col']], 'shared_id': w.get('shared_id'),
                                 'native_affine': w['native_affine'], 'student_affine': w['student_affine'],
                                 # 'mga55_bounds' is kept for readers of the first dataset; it holds bounds in `crs`
                                 'mga55_bounds': [w['native_affine'][0], w['native_affine'][3] - size * geo['gsd_m'],
                                                  w['native_affine'][0] + size * geo['gsd_m'], w['native_affine'][3]],
                                 'bounds_crs': [w['native_affine'][0], w['native_affine'][3] - size * geo['gsd_m'],
                                                w['native_affine'][0] + size * geo['gsd_m'], w['native_affine'][3]], 'crs': crs,
                                 'centre_lonlat': [round(lon, 7), round(lat, 7)], 'native_px': size,
                                 'student_px': STUDENT_PX, 'student_px_m': round(student_m, 6),
                                 'sha256': record['sha256'], 'files': {k: (v if k == 'native' else f'{folder}/{v}')
                                                                       for k, v in record['files'].items()},
                                 'counts': {k: record['stats'][k] for k in (
                                     'teacher_raw_pieces', 'links', 'groups_merged', 'teacher_duplicates_dropped',
                                     'teacher_instances', 'teacher_student_instances', 'teacher_lost_in_downsample',
                                     'control_proposals', 'control_at_cap', 'control_duplicates_dropped', 'control_instances',
                                     'control_kept_beyond_production_cap')},
                                 'subtiles_at_cap': sum(s['at_cap'] for s in record['stats']['subtiles']),
                                 'timings': t, 'config_hash': record['config_hash']})
    for name, payload in coco.items():
        common.write_json_atomic(root / TEACHER / f'{name}_coco.json', payload)
    prov = provenance(root, geo, overview_meta.get('jp2'))
    prov['geo_box'] = overview_meta.get('geo_box')
    common.write_json_atomic(root / TEACHER / 'provenance.json', prov)
    totals = {name: len(payload['annotations']) for name, payload in coco.items()}

    def timing(key):
        return _spread([t.get(key) for t in timings])
    manifest = {'version': VERSION, 'created_at': common.now_iso(), 'code_sha256': code_sha256(),
                'config': records[0]['config'] if records else None, 'n_windows': len(records),
                'native_window_px': size, 'gsd_m': geo['gsd_m'], 'student_px_m': round(student_m, 6),
                'window_ground_m': round(size * geo['gsd_m'], 3), 'blur_sigma_px': calibration['sigma_px'],
                'blur_sigma_m': calibration.get('sigma_m'), 'selection': {k: windows[k] for k in ('seed', 'n', 'rule', 'rule_text', 'counts')},
                'skipped_partial': crops.get('skipped_partial'), 'instances': totals,
                'timings_s': {k: timing(k) for k in ('native_read_s', 'student_s', 'teacher_sam3_s', 'stitch_s',
                                                     'control_sam3_s', 'window_total_s')},
                'grid_note': SOURCE.get('grid_note') or ('MGA grid north: about 1.3 degrees from true north here; grid scale '
                                                         'factor about 1.0000, so grid metres are ground metres to 1e-4'),
                'crs': SOURCE.get('crs'), 'teacher_source': SOURCE.get('title'),
                'provenance': {k: prov.get(k) for k in ('title', 'licence', 'attribution', 'url', 'capture', 'crs', 'place')},
                'windows': manifest_windows,
                'files': {f'{name}_coco.json': common.sha256_file(root / TEACHER / f'{name}_coco.json') for name in coco}}
    common.write_json_atomic(root / TEACHER / 'manifest.json', manifest)
    return {k: v for k, v in manifest.items() if k != 'windows'}


def _coco_row(annotation_id, image_id, rle, bbox, area, score, polygon, px_m, wid, index):
    row = {'id': annotation_id, 'image_id': image_id, 'category_id': common.CATEGORY['id'], 'iscrowd': 0,
           'segmentation': rle, 'bbox': list(bbox), 'area': int(area), 'score': score, 'window_id': wid,
           'instance_index': index, 'area_m2': round(area * px_m * px_m, 2), 'provenance': PROVENANCE_TAG,
           'tier': 'T3', 'source': 'sam3-text-onnx'}
    if polygon is not None:
        row['polygon_simplified'] = common.flat_coords(list(polygon.exterior.coords)[:-1])
    return row


# --- premise measurement -------------------------------------------------------------------------------------------

def turning_angles(polygon):
    """Absolute turning angle (degrees) at every exterior vertex."""
    points = np.asarray(polygon.exterior.coords, dtype=float)[:-1]
    if len(points) < 3:
        return np.zeros(0)
    before = points - np.roll(points, 1, axis=0)
    after = np.roll(points, -1, axis=0) - points
    cross = before[:, 0] * after[:, 1] - before[:, 1] * after[:, 0]
    dot = (before * after).sum(axis=1)
    return np.abs(np.degrees(np.arctan2(cross, dot)))


def corner_stats(polygon, min_turn=CORNER_MIN_TURN, right_tol=RIGHT_TOL):
    turns = turning_angles(polygon)
    corners = turns[turns >= min_turn]
    right = int((np.abs(corners - 90.) <= right_tol).sum())
    return {'corners': int(len(corners)), 'right': right, 'turns': [round(float(t), 1) for t in corners]}


def shape_metrics(piece, px_m, tolerance_m=COMMON_TOLERANCE_M):
    """Vertex counts (production 0.9 px and common metric tolerance), rectangle fill and corners, in metres."""
    production = exterior_polygon(piece, SIMPLIFY_PX)
    exact = exterior_polygon(piece, 0.)
    if production is None or exact is None:
        return None
    to_m = [px_m, 0., 0., px_m, 0., 0.]
    production_m = affinity.affine_transform(production, to_m)
    exact_m = affinity.affine_transform(exact, to_m)
    common_m = exact_m.simplify(tolerance_m, preserve_topology=True)
    corners = corner_stats(common_m)
    coarse = corner_stats(exact_m.simplify(COARSE_TOLERANCE_M, preserve_topology=True))
    return {'vertices_production': len(production.exterior.coords) - 1,
            'vertices_common': len(common_m.exterior.coords) - 1, 'fill': round(common.rectilinearity(production_m), 4),
            'corners': corners['corners'], 'right': corners['right'], 'turns': corners['turns'],
            'corners_coarse': coarse['corners'], 'right_coarse': coarse['right'],
            'area_m2': round(production_m.area, 2), 'polygon_m': production_m}


def mean_boundary_distance(a, b, step_m=.1):
    """Symmetric mean distance (metres) between two exterior rings, sampled every step_m along each."""
    def one_way(src, dst):
        ring = src.exterior
        count = max(int(ring.length / step_m), 4)
        return [ring.interpolate(k / count, normalized=True).distance(dst.exterior) for k in range(count)]
    values = one_way(a, b) + one_way(b, a)
    return float(np.mean(values)) if values else None


def match_pieces(first, second, threshold=MATCH_IOU):
    """Greedy one-to-one matching by IoU (descending) at >= threshold; returns [(i, j, iou)]."""
    candidates = []
    for i, a in enumerate(first):
        for j, b in enumerate(second):
            if a.x0 >= b.x1 or b.x0 >= a.x1 or a.y0 >= b.y1 or b.y0 >= a.y1:
                continue
            value = iou(a, b)
            if value >= threshold:
                candidates.append((value, i, j))
    used_a, used_b, pairs = set(), set(), []
    for value, i, j in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((i, j, value))
    return pairs


def _cluster_bootstrap(rows, statistic, seed, samples=2000):
    windows = sorted({r['window'] for r in rows})
    if len(windows) < 2:
        return None
    grouped = {w: [r for r in rows if r['window'] == w] for w in windows}
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(samples):
        pick = rng.choice(len(windows), len(windows), replace=True)
        sample = [r for k in pick for r in grouped[windows[k]]]
        value = statistic(sample)
        if value is not None:
            values.append(value)
    return [_num(np.percentile(values, 2.5)), _num(np.percentile(values, 97.5))] if values else None


def _pooled_right(rows, arm, suffix=''):
    corners = sum(r[arm]['corners' + suffix] for r in rows)
    return sum(r[arm]['right' + suffix] for r in rows) / corners if corners else None


def _median_delta(rows, key, arm='teacher_native', control='control'):
    values = [r[arm][key] - r[control][key] for r in rows]
    return float(np.median(values)) if values else None


def premise_rows(root):
    root = Path(root)
    crops, records = load_records(root)
    gsd = crops['gsd_m']
    rows, counts = [], {'windows': len(records), 'teacher_instances': 0, 'teacher_student_instances': 0,
                        'control_instances': 0, 'matched_pairs': 0, 'shape_failures': 0}
    for record in records:
        size = record['window']['shape'][0]
        student_m = size * gsd / STUDENT_PX
        teacher = [(rle_piece(row['student']['rle'], row['student']['bbox']), row) for row in record['teacher']
                   if row['student']]
        control = [rle_piece(row['rle'], row['bbox'], row['score']) for row in record['control']]
        counts['teacher_instances'] += len(record['teacher'])
        counts['teacher_student_instances'] += len(teacher)
        counts['control_instances'] += len(control)
        pairs = match_pieces([t[0] for t in teacher], control)
        counts['matched_pairs'] += len(pairs)
        for i, j, value in pairs:
            row = teacher[i][1]
            native = rle_piece(row['rle_native'], row['bbox_native'])
            arms = {'teacher_native': shape_metrics(native, gsd), 'teacher_student': shape_metrics(teacher[i][0], student_m),
                    'control': shape_metrics(control[j], student_m)}
            if any(v is None for v in arms.values()):
                counts['shape_failures'] += 1
                continue
            polygons = {k: v.pop('polygon_m') for k, v in arms.items()}
            rows.append({'window': record['window']['id'], 'teacher_index': row['index'],
                         'control_index': record['control'][j]['index'], 'iou_student_grid': round(value, 4),
                         **arms, 'hausdorff_m': {
                             'teacher_native_vs_control': round(polygons['teacher_native'].exterior.hausdorff_distance(
                                 polygons['control'].exterior), 3),
                             'teacher_native_vs_teacher_student': round(polygons['teacher_native'].exterior.hausdorff_distance(
                                 polygons['teacher_student'].exterior), 3),
                             'teacher_student_vs_control': round(polygons['teacher_student'].exterior.hausdorff_distance(
                                 polygons['control'].exterior), 3)},
                         'mean_boundary_distance_m': {
                             'teacher_native_vs_control': round(mean_boundary_distance(polygons['teacher_native'],
                                                                                       polygons['control']), 3),
                             'teacher_native_vs_teacher_student': round(mean_boundary_distance(
                                 polygons['teacher_native'], polygons['teacher_student']), 3)},
                         'bounds_m': list(polygons['teacher_native'].union(polygons['control']).bounds),
                         'polygons_m': {k: [[round(x, 3), round(y, 3)] for x, y in p.exterior.coords[:-1]]
                                        for k, p in polygons.items() if k != 'teacher_student'}})
    return crops, records, rows, counts


def summarise_premise(rows, counts, seed=DEFAULT_SEED):
    arms = ('teacher_native', 'teacher_student', 'control')
    summary = {'counts': counts, 'pairs_measured': len(rows), 'arms': {}}
    for arm in arms:
        corners = sum(r[arm]['corners'] for r in rows)
        right = sum(r[arm]['right'] for r in rows)
        histogram = np.histogram([t for r in rows for t in r[arm]['turns']], bins=np.arange(30, 190, 10))[0].tolist()
        summary['arms'][arm] = {
            'vertices_production': _spread([r[arm]['vertices_production'] for r in rows]),
            'vertices_common_0_25m': _spread([r[arm]['vertices_common'] for r in rows]),
            'fill': _spread([r[arm]['fill'] for r in rows]),
            'corners_total': corners, 'right_corners': right,
            'right_corners_per_building': _spread([r[arm]['right'] for r in rows]),
            'corners_per_building': _spread([r[arm]['corners'] for r in rows]),
            'buildings_with_4plus_right_corners': int(sum(r[arm]['right'] >= 4 for r in rows)),
            'coarse_0_5m': {'corners_total': sum(r[arm]['corners_coarse'] for r in rows),
                            'right_corners': sum(r[arm]['right_coarse'] for r in rows),
                            'right_share_pooled': _num(_pooled_right(rows, arm, '_coarse')),
                            'right_share_ci95_window_bootstrap': _cluster_bootstrap(
                                rows, lambda s, a=arm: _pooled_right(s, a, '_coarse'), seed),
                            'corners_per_building': _spread([r[arm]['corners_coarse'] for r in rows]),
                            'right_corners_per_building': _spread([r[arm]['right_coarse'] for r in rows])},
            'right_share_pooled': _num(right / corners if corners else None),
            'right_share_ci95_window_bootstrap': _cluster_bootstrap(rows, lambda s, a=arm: _pooled_right(s, a), seed),
            'turn_histogram_deg': {'bins': list(range(30, 190, 10)), 'counts': histogram}}
    summary['paired'] = {}
    for key in ('fill', 'vertices_production', 'vertices_common', 'right', 'corners', 'right_coarse', 'corners_coarse'):
        for arm in ('teacher_native', 'teacher_student'):
            values = [r[arm][key] - r['control'][key] for r in rows]
            summary['paired'][f'{arm}_minus_control.{key}'] = {
                'n': len(values), 'median': _num(np.median(values) if values else None),
                'ci95_window_bootstrap': _cluster_bootstrap(rows, lambda s, k=key, a=arm: _median_delta(s, k, a), seed),
                'n_higher': int(sum(v > 0 for v in values)), 'n_lower': int(sum(v < 0 for v in values)),
                'n_equal': int(sum(v == 0 for v in values))}
    for arm in ('teacher_native', 'teacher_student'):
        higher = sum(r[arm]['right'] / max(r[arm]['corners'], 1) > r['control']['right'] / max(r['control']['corners'], 1)
                     for r in rows)
        lower = sum(r[arm]['right'] / max(r[arm]['corners'], 1) < r['control']['right'] / max(r['control']['corners'], 1)
                    for r in rows)
        summary['paired'][f'{arm}_minus_control.right_share_per_building'] = {'n': len(rows), 'n_higher': int(higher),
                                                                              'n_lower': int(lower),
                                                                              'n_equal': len(rows) - int(higher) - int(lower)}
    summary['hausdorff_m'] = {key: {**_spread([r['hausdorff_m'][key] for r in rows]),
                                    'n_le_0_45': int(sum(r['hausdorff_m'][key] <= .45 for r in rows))}
                              for key in ('teacher_native_vs_control', 'teacher_native_vs_teacher_student',
                                          'teacher_student_vs_control')}
    summary['mean_boundary_distance_m'] = {key: _spread([r['mean_boundary_distance_m'][key] for r in rows])
                                           for key in ('teacher_native_vs_control', 'teacher_native_vs_teacher_student')}
    strata = {'lt_50m2': lambda a: a < 50, '50_200m2': lambda a: 50 <= a < 200, 'ge_200m2': lambda a: a >= 200}
    summary['by_area'] = {}
    for name, test in strata.items():
        subset = [r for r in rows if test(r['teacher_native']['area_m2'])]
        summary['by_area'][name] = {'n': len(subset), **{
            f'{arm}_right_share': _num(_pooled_right(subset, arm)) for arm in arms}, **{
            f'{arm}_fill_median': _num(np.median([r[arm]['fill'] for r in subset]) if subset else None) for arm in arms},
            'hausdorff_teacher_native_vs_control_median': _num(np.median(
                [r['hausdorff_m']['teacher_native_vs_control'] for r in subset]) if subset else None)}
    summary['definitions'] = {
        'match': f'greedy one-to-one, IoU >= {MATCH_IOU} on the student grid (teacher mask area-averaged, >= 0.5)',
        'vertices_production': f'mask_to_polygons(simplify_px={SIMPLIFY_PX}) at each arm\'s own pixel, largest part',
        'vertices_common': f'exact pixel outline simplified at {COMMON_TOLERANCE_M} m in every arm',
        'fill': 'polygon area / minimum rotated rectangle area (production polygon)',
        'corner': f'vertex of the {COMMON_TOLERANCE_M} m polygon turning >= {CORNER_MIN_TURN:g} degrees; right corner: '
                  f'turn within {RIGHT_TOL:g} degrees of 90',
        'coarse_0_5m': (f'the same corner count on the exact outline simplified at {COARSE_TOLERANCE_M} m: pixel staircases '
                        'of up to two student pixels are exact 90 degree steps at 0.25 m and inflate right corners on the '
                        '0.236 m grid; the coarse arm removes most of them'),
        'hausdorff': 'between exterior rings of the production polygons, metres',
        'mean_boundary_distance': 'symmetric mean distance between the exterior rings, sampled every 0.1 m, metres',
        'right_share_caveat': ('a strongly rounded corner can split into turns under the corner threshold and leave the '
                               'denominator, so read right corners per building beside the pooled share'),
        'teacher_student': 'the training label: teacher mask area-averaged onto the student grid'}
    summary['note'] = ('Agreement between two SAM3 runs on the same buildings and a shape description of each; no human '
                       'reference is involved, so none of this is accuracy.')
    return summary


def measure(root='.', seed=DEFAULT_SEED, sheet_pairs=SHEET_PAIRS):
    root = Path(root)
    crops, records, rows, counts = premise_rows(root)
    summary = summarise_premise(rows, counts, seed)
    folder = root / TEACHER / 'premise'
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / 'pairs.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps({k: v for k, v in row.items() if k != 'polygons_m'}) + '\n')
    eligible = [r for r in rows if r['teacher_native']['area_m2'] >= SHEET_MIN_AREA_M2]
    rng = np.random.default_rng(seed)
    pick = sorted(rng.choice(len(eligible), min(sheet_pairs, len(eligible)), replace=False).tolist()) if eligible else []
    chosen = [eligible[i] for i in pick]
    summary['contact_sheet'] = {'path': str((folder / 'contact_sheet.png').relative_to(root)), 'pairs': len(chosen),
                                'rule': f'seeded ({seed}) random sample of matched pairs with teacher area >= '
                                        f'{SHEET_MIN_AREA_M2:g} m2 (n eligible {len(eligible)})',
                                'pairs_shown': [{'window': r['window'], 'teacher_index': r['teacher_index'],
                                                 'control_index': r['control_index']} for r in chosen]}
    if chosen:
        contact_sheet(root, crops, chosen, folder / 'contact_sheet.png')
    summary['version'] = VERSION
    summary['computed_at'] = common.now_iso()
    common.write_json_atomic(folder / 'summary.json', summary)
    return summary


def _panel(image, bounds_m, px_m, polygon_m, colour, size, resample):
    x0, y0, x1, y1 = bounds_m
    box = [int(math.floor(x0 / px_m)), int(math.floor(y0 / px_m)), int(math.ceil(x1 / px_m)), int(math.ceil(y1 / px_m))]
    crop = image.crop(box).resize((size, size), resample)
    scale_x, scale_y = size / (box[2] - box[0]), size / (box[3] - box[1])
    points = [((x / px_m - box[0]) * scale_x, (y / px_m - box[1]) * scale_y) for x, y in polygon_m]
    draw = ImageDraw.Draw(crop)
    draw.line(points + points[:1], fill=colour, width=2)
    return crop


def contact_sheet(root, crops, rows, path, panel=300, per_row=4):
    """Native crop + teacher outline (green) beside student crop + control outline (orange), one pair per cell."""
    gsd = crops['gsd_m']
    size = crops['native_window_px']
    student_m = size * gsd / STUDENT_PX
    caption = 36
    cols = min(per_row, len(rows))
    lines = math.ceil(len(rows) / per_row)
    sheet = Image.new('RGB', (cols * (2 * panel + 16), lines * (panel + caption + 12) + 40), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((8, 10), f'left: native {gsd:g} m crop, teacher (SAM3 on native sub-tiles) green | right: {student_m:.3f} m student, '
                       'control (SAM3 on student) orange. Model outputs, not labels.', fill=(230, 230, 230), font=font)
    cache = {}
    windows = {w['id']: w for w in crops['windows']}
    for k, row in enumerate(rows):
        wid = row['window']
        if wid not in cache:
            cache = {wid: (Image.open(Path(root) / windows[wid]['native_file']).convert('RGB'),
                           Image.open(window_dir(root, wid) / 'student.png').convert('RGB'))}
        native, student = cache[wid]
        x0, y0, x1, y1 = row['bounds_m']
        pad = 3.
        half = max(x1 - x0, y1 - y0) / 2 + pad
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        bounds = (cx - half, cy - half, cx + half, cy + half)
        left = _panel(native, bounds, gsd, row['polygons_m']['teacher_native'], (0, 255, 0), panel, Image.Resampling.LANCZOS)
        right = _panel(student, bounds, student_m, row['polygons_m']['control'], (255, 140, 0), panel,
                       Image.Resampling.NEAREST)
        gx, gy = (k % per_row) * (2 * panel + 16) + 8, 40 + (k // per_row) * (panel + caption + 12)
        sheet.paste(left, (gx, gy))
        sheet.paste(right, (gx + panel, gy))
        t, c = row['teacher_native'], row['control']
        text = (f"{wid.split('-', 1)[-1]} #{row['teacher_index']} IoU {row['iou_student_grid']:.2f} {t['area_m2']:.0f} m2  H "
                f"{row['hausdorff_m']['teacher_native_vs_control']:.2f} m\nfill {t['fill']:.2f}/{c['fill']:.2f}  right "
                f"{t['right']}/{t['corners']} vs {c['right']}/{c['corners']}  verts {t['vertices_production']}/"
                f"{c['vertices_production']}")
        draw.text((gx, gy + panel + 2), text, fill=(230, 230, 230), font=font)
    sheet.save(path)
    return path


# --- CLI -----------------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', choices=('extract', 'overview', 'select', 'crop', 'calibrate', 'teach', 'finalize',
                                          'measure', 'provenance'))
    parser.add_argument('--root', default='.')
    parser.add_argument('--n', type=int, default=DEFAULT_N)
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED)
    parser.add_argument('--reduce', type=int, default=OVERVIEW_RULE['reduce'])
    parser.add_argument('--rule', type=json.loads, default=None, help='JSON overrides for the overview rule')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--shard', default='0/1', help='K/N: teach every N-th window starting at K')
    parser.add_argument('--source', default='com-2018', help='teacher_sources registry id (com-2018 keeps the JP2 stages)')
    args = parser.parse_args(argv)
    spec = set_source(args.source, args.root)
    if spec['kind'] != 'com-jp2' and args.stage in JP2_STAGES:
        raise SystemExit(f"{args.stage} is JPEG2000-specific; for {spec['id']} run "
                         f"`python -m scripts.ml.training.teacher_sources catalog|select|fetch|crop --source {spec['id']}`")
    if args.stage == 'extract':
        result = extract(args.root)
    elif args.stage == 'overview':
        result = overview(args.root, args.reduce)
    elif args.stage == 'select':
        result = select(args.root, args.n, args.seed, {**(args.rule or {}), 'reduce': args.reduce})
    elif args.stage == 'crop':
        result = crop(args.root, args.limit)
    elif args.stage == 'calibrate':
        result = calibrate(args.root, args.seed)
    elif args.stage == 'teach':
        k, n = (int(v) for v in args.shard.split('/'))
        result = teach(args.root, (k, n), args.limit)
    elif args.stage == 'finalize':
        result = finalize(args.root)
    elif args.stage == 'measure':
        result = measure(args.root, args.seed)
    else:
        result = provenance(args.root)
    json.dump(result, sys.stdout, indent=1, sort_keys=True, default=str)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
