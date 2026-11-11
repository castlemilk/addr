"""LINZ 1 m DSM/DEM as the teacher ledger's second evidence leg (teacher-elevation-v1).

The ledger (teacher_judges.ledger, ledger-policy-v1) recommends keep / part / ignore / review for every 2025 teacher
instance from image-side signals only: the open outlines, edge support and the second capture. None of them sees
height. This module adds a surface-model leg from the LINZ Christchurch 2024-2025 LiDAR (1 m DSM and DEM, CC BY 4.0,
producer Aerial Surveys, licensor Environment Canterbury) and judges the ledger by leaving one signal out at a time:

  catalog   read the DSM and DEM STAC collections, keep the 1:10,000 tiles whose lon/lat bbox meets a teacher window,
            read their COG headers for exact NZTM bounds                           -> <cache>/<epoch>/catalog.json
  fetch     download the tiles (thread pool), verify every sha256 against the STAC file:checksum multihash; a mismatch
            or a failed transfer is unlinked and recorded as status 'error', never raised; reruns skip verified files
                                                                                   -> <cache>/manifest.json
  measure   per teacher instance: nDSM = DSM - DEM from rasterio window reads padded 4 m (rasterio.merge for windows
            that straddle a tile edge; whole tiles are never read), interior height (heights_source.sample_polygon),
            share above 2 m, edge step at -1.5 / +1.5 m, outside ring, vegetation share on the 7.5 cm pixels, nodata
            share, ndsm_class, epoch_suspect; 50 control discs per window outside every teacher polygon, same measures
                                                                                   -> judges/ndsm_<epoch>{.json,_rows.jsonl}, ndsm_control_<epoch>_rows.jsonl
  judge     the 18 judge cells (teacher_judges.cell_split): A. nDSM class shares per recommendation and area band with
            the control discs beside; B. leave-one-signal-out (recommend_without mirrors teacher_judges.recommend);
            C. nDSM class against cross-capture and outline kind; recommendation_v2 for the candidate ledger-policy-v2
                                                                                   -> judges/ledger_loso_<epoch>{.json,_rows.jsonl}, ledger_v2_<epoch>_rows.jsonl

Wording: "elevated surface present", never "roof confirmed": a first-return DSM includes canopy (veg_share and the
control discs are shown beside). Every figure is agreement between a teacher instance and the surface model, or between
two models, never accuracy. Hints are never labels; nothing is promoted; ledger-policy-v1, ledger_rows.jsonl,
ledger.json and the built folds are never written. The rules are pre-registered in judges/ndsm_rules.json: NDSM below
equals that file and measure / judge refuse to run when it is absent or different.

Provenance: every output records module_sha256 (this file, hashed at import) and code_sha256 (the dependency sources the
stage ran with); the measure cache reuses a window only when every input is unchanged (code, rules, record.json,
crops.json and the window's geometry, the native PNG, the judge files the joins read, the tile checksums). The control
discs are drawn outside every teacher polygon (2 m buffer), so they are off the teacher's polygons, not necessarily off
roofs: an elevated control disc is canopy or a structure the teacher did not map (the elevated share is split by
veg_share in the outputs). The LiDAR's 0.2 m / 1.0 m (95 percent) figures are LINZ's product specification, never a
figure about this judge.

Venvs: catalog / fetch / measure / judge run in scripts/ml/training/.venv (rasterio lives only there); the unit tests
run in scripts/ml/.venv (pytest; no rasterio), so every rasterio import is inside a function. fetch and measure are
host jobs: run `. .run/jobgate.sh && jobgate 500` (fetch) or `jobgate 2000` (measure) first and copy its header line
into the job log (measure records it in ndsm_<epoch>.json).

  scripts/ml/training/.venv/bin/python -m scripts.ml.training.teacher_elevation catalog --source linz-chc-2025-0075 --epoch christchurch_2024-2025
  scripts/ml/training/.venv/bin/python -m scripts.ml.training.teacher_elevation fetch --source linz-chc-2025-0075 --epoch christchurch_2024-2025 --workers 8
  scripts/ml/training/.venv/bin/python -m scripts.ml.training.teacher_elevation measure --source linz-chc-2025-0075 --epoch christchurch_2024-2025
  scripts/ml/training/.venv/bin/python -m scripts.ml.training.teacher_elevation judge --source linz-chc-2025-0075 --epoch christchurch_2024-2025 --cells judge

Taskfile task for the integrator (W4-INT adds it; this module never edits Taskfile.yml):

  eyes:teacher-elevation:
    desc: 'LINZ 1 m nDSM as the second evidence leg of the teacher ledger (teacher-elevation-v1), agreement never accuracy: CLI_ARGS="catalog --source linz-chc-2025-0075 --epoch christchurch_2024-2025" (STAC tiles meeting the windows) | "fetch --source linz-chc-2025-0075 --epoch christchurch_2024-2025 --workers 8" (host job, jobgate 500 first; sha256-verified, resumable) | "measure --source linz-chc-2025-0075 --epoch christchurch_2024-2025" (host job, jobgate 2000 first; nDSM class, edge step, control discs outside every teacher polygon) | "judge --source linz-chc-2025-0075 --epoch christchurch_2024-2025 --cells judge" (class shares per recommendation, leave-one-signal-out, candidate ledger-policy-v2 rows); elevated surface present, never roof confirmed; writes data/elevation/linz-chc-1m/ and <teacher>/judges/ndsm_*, ledger_loso_*, ledger_v2_*; never ledger_rows.jsonl'
    cmds:
      - 'scripts/ml/training/.venv/bin/python -m scripts.ml.training.teacher_elevation {{.CLI_ARGS}}'

.gitignore line for the integrator: data/elevation/linz-chc-1m/
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from . import common

VERSION = 'teacher-elevation-v1'
SOURCE_ID = 'linz-chc-2025-0075'
OTHER_CAPTURE = 'linz-chc-2020-0075'          # the second capture behind the ledger's cross_capture signal
STAC_ROOT = 'https://nz-elevation.s3.ap-southeast-2.amazonaws.com/catalog.json'
BUCKET = 'nz-elevation'
CACHE = Path('data/elevation/linz-chc-1m')
MEASURE_LOG = '.run/teacher-elevation-measure.log'
REC_CLASSES = ('keep', 'review', 'ignore', 'part')
NDSM_CLASSES = ('elevated', 'flat', 'mixed', 'unjudged')
IDENTITY = np.array([[1., 0.], [0., 1.], [0., 0.]])     # native px matrix: [col, row, 1] @ M = (x, y)

LICENCE = {'licence': 'CC BY 4.0', 'licence_url': 'https://creativecommons.org/licenses/by/4.0/',
           'attribution': 'Sourced from Toitu Te Whenua LINZ, Aerial Surveys and Environment Canterbury, CC BY 4.0; used as an '
                          'evidence signal on teacher instances, not as a label'}

ELEVATION = {
    'christchurch_2024-2025': {
        'epoch': 'christchurch_2024-2025', 'in_scope': True, 'bucket': BUCKET, 'region': 'canterbury',
        'title': 'Canterbury - Christchurch LiDAR 1m DSM and DEM (2024-2025)',
        'layers': {
            'dsm': {'prefix': 'canterbury/christchurch_2024-2025/dsm_1m/2193/', 'collection_id': '01K7D5GDMHRECR47H88R9ABDB2',
                    'koordinates_layer': 123194},
            'dem': {'prefix': 'canterbury/christchurch_2024-2025/dem_1m/2193/', 'collection_id': '01K7D5G9JKZZW6KRZ3ACWMD0T5',
                    'koordinates_layer': 123193},
        },
        'temporal': ['2024-12-09T11:00:00Z', '2025-01-19T11:00:00Z'],
        'host': 'Toitu Te Whenua Land Information New Zealand', 'producer': 'Aerial Surveys', 'licensor': 'Environment Canterbury',
        'product_spec_95': {'vertical_m': 0.2, 'horizontal_m': 1.0}, 'vertical_datum': 'NZVD2016',
        'crs': 'EPSG:2193', 'gsd_m': 1.0, 'tile_m': [4800., 7200.], 'tile_px': [4800, 7200], 'dtype': 'float32', 'nodata': -9999.0,
        'format': 'cloud-optimised GeoTIFF, LERC, 512 px blocks', 'imagery_source': 'linz-chc-2025-0075',
    },
    'christchurch_2020-2021': {
        'epoch': 'christchurch_2020-2021', 'in_scope': False, 'bucket': BUCKET, 'region': 'canterbury',
        'layers': {'dsm': {'prefix': 'canterbury/christchurch_2020-2021/dsm_1m/2193/'},
                   'dem': {'prefix': 'canterbury/christchurch_2020-2021/dem_1m/2193/'}},
        'imagery_source': 'linz-chc-2020-0075',
        'status': 'recorded, out of scope: the 2020-21 nDSM epoch is the user decision (round-5 plan non-goal); never run here',
    },
}
FORBIDDEN_EPOCHS = {'canterbury_2020-2023': 'the regional canterbury_2020-2023 product is never used for these windows (round-5 plan rule)'}
EPOCH_FOR_SOURCE = {'linz-chc-2025-0075': 'christchurch_2024-2025', 'linz-chc-2020-0075': 'christchurch_2020-2021'}
PRODUCT_SPEC_TEXT = 'LINZ product specification of the LiDAR (95 percent), not a figure about this judge or the teacher'
CONTROL_VEG_SPLIT = 0.2      # reporting split of elevated control discs by veg_share: not a registered rule, never deciding
CODE_FILES = {'teacher_elevation': 'training/teacher_elevation.py', 'teacher_sources': 'training/teacher_sources.py',
              'teacher_judges': 'training/teacher_judges.py', 'hires_teacher': 'training/hires_teacher.py',
              'distill_dataset': 'training/distill_dataset.py', 'heights_source': 'heights_source.py',
              'segmentation': 'segmentation.py'}                             # under scripts/ml/
CATALOG_CODE = ('teacher_sources',)                              # to_lonlat, read_header, s3_json, multihash_sha256
MEASURE_CODE = ('teacher_judges', 'hires_teacher', 'segmentation', 'heights_source')   # teacher_polygons (rle_piece,
                                          # exterior_polygon -> mask_to_polygons), ring_samples, _band; sample_polygon, ndsm
JUDGE_CODE = ('teacher_judges', 'hires_teacher', 'distill_dataset')    # recommend, cell_split (load_records, assign_splits)
JOIN_FILES = ('outlines_rows.jsonl', 'edge_support_rows.jsonl', f'cross_capture_{OTHER_CAPTURE}.json',
              f'cross_capture_{OTHER_CAPTURE}_rows.jsonl')
MODULE_SHA256 = common.sha256_file(Path(__file__).resolve())     # the source this process imported

# Pre-registered rules: must equal distill-linz-chc-2025-0075/teacher/judges/ndsm_rules.json (written first).
NDSM = {'version': 'teacher-elevation-v1',
        'rules': 'ndsm_leg',
        'written_at': '2026-09-30T12:08:30Z',
        'written_before': 'any nDSM catalog, fetch, measure or judge output (round-5 plan section 5 ndsm_leg); outputs embed this '
                          "file's sha256",
        'source': 'linz-chc-2025-0075',
        'epoch': 'christchurch_2024-2025',
        'epoch_facts': 'LINZ nz-elevation canterbury/christchurch_2024-2025 dsm_1m / dem_1m (EPSG:2193, 1 m); STAC '
                       '01K7D5GDMHRECR47H88R9ABDB2 (DSM) / 01K7D5G9JKZZW6KRZ3ACWMD0T5 (DEM); Koordinates 123194 / 123193; producer '
                       'Aerial Surveys, licensor Environment Canterbury; CC BY 4.0; vertical +/-0.2 m and horizontal +/-1.0 m at 95 '
                       'percent; NZVD2016; never canterbury_2020-2023; the 2020-21 epoch is not run',
        'venvs': 'tests under scripts/ml/.venv only (the training venv has no pytest); catalog, fetch, measure and judge under '
                 'scripts/ml/training/.venv (rasterio); rasterio imports lazy; no boto3',
        'classes': {'elevated': 'height_m >= 2.0 and share_above_2m >= 0.5',
                    'flat': 'height_m < 1.0 and share_above_2m < 0.1',
                    'mixed': 'else',
                    'unjudged': 'samples < 4 after the erosion fallbacks 1.0 / 0.5 / 0.0 m, or nodata_share > 0.2',
                    'precedence': ['unjudged', 'elevated', 'flat', 'mixed']},
        'thresholds': {'elevated_height_m': 2.0,
                       'elevated_share_above_2m': 0.5,
                       'flat_height_m': 1.0,
                       'flat_share_above_2m': 0.1,
                       'above_m': 2.0,
                       'min_samples': 4,
                       'max_nodata_share': 0.2},
        'measures': {'height_m': 'heights_source.sample_polygon on the 1 m nDSM (DSM minus DEM): median of the top half of the cells '
                                 'whose centres lie inside the outline eroded by erode_m',
                     'eave_m': 'lower quartile of the same cells',
                     'iqr_m': 'interquartile range of the same cells',
                     'samples': 'finite cells used, at the erosion that succeeded (else at the last fallback, 0.0 m)',
                     'erode_m': 'the erosion that succeeded (1.0, then 0.5, then 0.0 m); the last fallback when unjudged',
                     'share_above_2m': 'share of the sampled cells with nDSM >= 2.0 m',
                     'nodata_share': 'share of the cells whose centres lie inside the uneroded outline that are nodata in the DSM or '
                                     'the DEM',
                     'edge_step_m': 'median over ring samples (every 1 m along the outline; outward normals from '
                                    'teacher_judges.ring_samples) of nDSM at -1.5 m minus nDSM at +1.5 m',
                     'edge_step_share': 'share of ring samples whose step is >= 1.5 m',
                     'outside_ring_m': 'median nDSM of the cells between buffer(1.5) and buffer(4.0) of the outline, minus every other '
                                       'teacher polygon',
                     'veg_share': 'share of 7.5 cm native pixels inside the outline with ExG > 0.06 (the teacher_judges J1 vegetation '
                                  'rule)',
                     'epoch_suspect': 'flat while edge_support >= 0.8 and cross_capture matched, or elevated while outline_kind '
                                      'unmatched and cross_capture only_here (joined from edge_support_rows.jsonl, '
                                      'cross_capture_linz-chc-2020-0075_rows.jsonl and outlines_rows.jsonl on (window, index)); '
                                      'recorded per instance, never deciding'},
        'sampling': {'erode_m': [1.0, 0.5, 0.0],
                     'min_pixels': 4,
                     'top_fraction': 0.5,
                     'eave_quantile': 0.25,
                     'read_pad_m': 4.0,
                     'nodata': -9999.0,
                     'straddling': 'rasterio.merge.merge(nodata=-9999, resampling=nearest)',
                     'whole_tiles': 'never read into memory: rasterio window reads only'},
        'probes': {'edge_step_offsets_m': [-1.5, 1.5],
                   'edge_step_min_m': 1.5,
                   'edge_step_every_m': 1.0,
                   'outside_ring_m': [1.5, 4.0],
                   'veg_exg': 0.06},
        'epoch_suspect': {'edge_support_min': 0.8},
        'control': {'discs_per_window': 50,
                    'disc_area_m2': 20.0,
                    'polygon_buffer_m': 2.0,
                    'inside_window': True,
                    'seed': 20260928,
                    'max_draws_per_window': 20000,
                    'text': '50 random 20 m2 discs per window outside every teacher polygon (2 m buffer), same measures, shares beside '
                            'the keep and ignore shares'},
        'interval': {'method': 'window bootstrap (teacher_judges.window_bootstrap_share)',
                     'draws': 1000,
                     'seed': 20260928,
                     'unit': 'window',
                     'cells': "cell_split(root, source) == 'judge' (18 cells); ledger rows carry no split key"},
        'measure_gate': {'rows': 12679, 'text': 'every 2025 teacher instance'},
        'loso': {'population': "the ledger rows of the 18 judge cells (cell_split == 'judge')",
                 'drops': {'outline': "outline_kind := 'unmatched' (the no-outline-touched state; None never reaches the ignore branch)",
                           'cross': 'cross_capture := None',
                           'edge': 'edge_support := 1.0 (passing)',
                           'area': 'area_m2 := 0'},
                 'values': {'outline': ['outline_kind', 'unmatched'],
                            'cross': ['cross_capture', None],
                            'edge': ['edge_support', 1.0],
                            'area': ['area_m2', 0.0]},
                 'pre_registered_counts_on_4132_judge_rows': {'full_policy': {'keep': 3234, 'review': 426, 'ignore': 253, 'part': 219},
                                                              'outline': {'keep': 2965, 'review': 810, 'ignore': 357, 'part': 0},
                                                              'cross': {'keep': 2460, 'review': 634, 'ignore': 420, 'part': 618},
                                                              'edge': {'keep': 3300, 'review': 384, 'ignore': 253, 'part': 195},
                                                              'area': {'keep': 3234, 'review': 205, 'ignore': 474, 'part': 219}},
                 'semantics_control': {'drop': 'outline_kind := None',
                                       'values': ['outline_kind', None],
                                       'counts': {'keep': 2965, 'review': 1167, 'ignore': 0, 'part': 0},
                                       'use': 'reported beside, never used as the drop'}},
        'read_conditions': {'ignore_band': 'readable only if the unjudged share among ignore rows < 0.5; else the edge-step measure is '
                                           'the read',
                            'ignore_band_unjudged_share_max': 0.5},
        'policy_v2': {'version': 'ledger-policy-v2-candidate',
                      'rules': ['keep: a ledger-policy-v1 keep whose ndsm_class is not flat (a flat v1 keep becomes review)',
                                'part: as ledger-policy-v1',
                                'ignore: a ledger-policy-v1 ignore whose ndsm_class is flat or unjudged (an elevated or mixed v1 ignore '
                                'becomes review)',
                                'review: everything else'],
                      'writes': 'ledger_v2_<epoch>_rows.jsonl only; candidate, never switched on'},
        'policy': 'ledger-policy-v1 unchanged; recommendation_v2 in ledger_v2_*_rows.jsonl only; ledger_rows.jsonl, ledger.json and the '
                  'built folds untouched',
        'wording': 'elevated surface present, never roof confirmed; canopy is elevated too (veg_share and control discs shown beside); '
                   'every figure is agreement between a teacher instance and the surface model, or between two models, never accuracy; '
                   'hints are never labels; nothing is promoted',
        'amendments': [],
        'plan_text_corrections': [{'at': '2026-09-30T12:08:30Z',
                                   'field': 'loso.pre_registered_counts_on_4132_judge_rows.outline.part',
                                   'plan_sections_3_and_5': 219,
                                   'registered': 0,
                                   'reason': 'plan sections 3 and 5 print the drop-outline tuple as keep 2965 / review 810 / ignore 357 '
                                             '/ part 219, which sums to 4351 on 4132 rows; plan section 0 (LOSO semantics row) and '
                                             'section 9 finding 2 record the verified result 2965 / 810 / 357, and outline_kind := '
                                             "'unmatched' leaves no row for the part branch (teacher_judges.recommend returns part only "
                                             "when outline_kind == 'part'); registered before any nDSM measurement; these counts depend "
                                             'on ledger rows only'}]}

LEDGER_POLICY_V2 = {
    'version': NDSM['policy_v2']['version'],
    'status': 'candidate; recommendations only; never switched on; ledger-policy-v1 stays the ledger policy',
    'rules': NDSM['policy_v2']['rules'],
    'note': ('the nDSM leg may only demote: a flat keep and an elevated or mixed ignore become review; written only to '
             'ledger_v2_<epoch>_rows.jsonl; an elevated surface may be canopy, so the leg never promotes'),
}
DROPS = {name: tuple(value) for name, value in NDSM['loso']['values'].items()}
SEMANTICS_CONTROL = tuple(NDSM['loso']['semantics_control']['values'])

TILE_RE = re.compile(r'^(?P<sheet>[A-Z]{2}\d{2})_10000_(?P<row>\d{2})(?P<col>\d{2})$')
ROW_LETTERS = 'ABCDEFGHJKLMNPQRSTUVWXYZ'                  # Topo50 sheet rows: letter pairs without I and O
TOPO50_LEFT_M, TOPO50_TOP_M = 1_084_000., 6_234_000.      # left of sheet column 04, top of sheet row AS (NZTM)
SHEET_E_M, SHEET_N_M = 24_000., 36_000.
TILE_E_M, TILE_N_M = 4_800., 7_200.                       # 1:10,000 tiles: 5 x 5 per sheet


def _num(value, digits=4):
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _spread(values):
    values = np.array([v for v in values if v is not None], dtype=float)
    if not values.size:
        return {'n': 0, 'median': None}
    return {'n': int(values.size), 'median': _num(np.median(values)), 'q25': _num(np.percentile(values, 25)),
            'q75': _num(np.percentile(values, 75)), 'mean': _num(values.mean())}


def _stable_int(text):
    return int(hashlib.sha256(str(text).encode()).hexdigest()[:12], 16)


def _write_jsonl_atomic(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    os.replace(tmp, path)


def code_sha256(*dependencies):
    """{name: sha256} of this module (hashed at import) and of the named dependency sources (CODE_FILES, hashed now), so
    every output records the code that produced it."""
    ml = Path(__file__).resolve().parents[1]
    return {'teacher_elevation': MODULE_SHA256, **{name: common.sha256_file(ml / CODE_FILES[name]) for name in dependencies}}


def rules_file_times(path):
    """File-system birth and modification times of the rules file, read at run time (UTC)."""
    stat = Path(path).stat()

    def iso(seconds):
        return None if seconds is None else datetime.fromtimestamp(seconds, tz=UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {'file_birth_utc': iso(getattr(stat, 'st_birthtime', None)), 'file_mtime_utc': iso(stat.st_mtime),
            'file_times_note': 'read from the file system at run time; the written_at field inside the file is text, not '
                               'a computed time'}


def epoch_spec(epoch):
    """Registry entry of an in-scope epoch; SystemExit for a forbidden, unknown or out-of-scope epoch."""
    if epoch in FORBIDDEN_EPOCHS:
        raise SystemExit(f'epoch {epoch!r} is never used: {FORBIDDEN_EPOCHS[epoch]}')
    spec = ELEVATION.get(epoch)
    if spec is None:
        raise SystemExit(f'unknown elevation epoch {epoch!r}; known: {", ".join(sorted(ELEVATION))}')
    if not spec.get('in_scope'):
        raise SystemExit(f'epoch {epoch!r}: {spec.get("status")}')
    return spec


# --- 1:10,000 tile arithmetic (Topo50 sheets, 4,800 x 7,200 m tiles) --------------------------------------------------

def _row_index(letters):
    """0 for sheet row AS, counting south (letter pairs skip I and O)."""
    return ROW_LETTERS.index(letters[0]) * len(ROW_LETTERS) + ROW_LETTERS.index(letters[1]) - ROW_LETTERS.index('S')


def _row_letters(index):
    pair = index + ROW_LETTERS.index('S')
    return ROW_LETTERS[pair // len(ROW_LETTERS)] + ROW_LETTERS[pair % len(ROW_LETTERS)]


def sheet_origin(sheet):
    """(left, top) of a Topo50 sheet in NZTM metres: BX24 -> (1,564,000; 5,190,000)."""
    return TOPO50_LEFT_M + (int(sheet[2:]) - 4) * SHEET_E_M, TOPO50_TOP_M - _row_index(sheet[:2]) * SHEET_N_M


def parse_tile(name):
    """('BX24', 1, 2) from 'BX24_10000_0102' (row from the sheet's top, col from its left, 1-based); None otherwise."""
    match = TILE_RE.match(Path(str(name)).stem)
    return (match['sheet'], int(match['row']), int(match['col'])) if match else None


def tile_name(sheet, row, col):
    return f'{sheet}_10000_{row:02d}{col:02d}'


def tile_bounds(name):
    """(left, bottom, right, top) of a 1:10,000 tile in NZTM metres from its name."""
    sheet, row, col = parse_tile(name)
    sheet_left, sheet_top = sheet_origin(sheet)
    left, top = sheet_left + (col - 1) * TILE_E_M, sheet_top - (row - 1) * TILE_N_M
    return left, top - TILE_N_M, left + TILE_E_M, top


def tiles_for_bounds(bounds, pad_m=0.):
    """Sorted names of the 1:10,000 tiles meeting (left, bottom, right, top) grown by pad_m; touching is not meeting."""
    w, s, e, n = (float(v) for v in bounds)
    w, s, e, n = w - pad_m, s - pad_m, e + pad_m, n + pad_m
    per_e, per_n = int(round(SHEET_E_M / TILE_E_M)), int(round(SHEET_N_M / TILE_N_M))
    gx0, gx1 = math.floor((w - TOPO50_LEFT_M) / TILE_E_M), math.ceil((e - TOPO50_LEFT_M) / TILE_E_M) - 1
    gy0, gy1 = math.floor((TOPO50_TOP_M - n) / TILE_N_M), math.ceil((TOPO50_TOP_M - s) / TILE_N_M) - 1
    names = []
    for gy in range(gy0, gy1 + 1):
        for gx in range(gx0, gx1 + 1):
            sheet = f'{_row_letters(gy // per_n)}{gx // per_e + 4:02d}'
            names.append(tile_name(sheet, gy % per_n + 1, gx % per_e + 1))
    return sorted(names)


def window_tiles(order, pad_m=0.):
    """{window id: tile names} for windows.json order[] entries (bounds in EPSG:2193 metres)."""
    return {w['id']: tiles_for_bounds(w['bounds'], pad_m) for w in order}


def _meets(a, b):
    """Open-interval overlap of two (left, bottom, right, top) boxes."""
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


# --- rules --------------------------------------------------------------------------------------------------------------

def rules_path(root, source_id=SOURCE_ID):
    from . import teacher_judges as tj
    from . import teacher_sources as ts
    return tj.judges_dir(root, ts.source(source_id)) / 'ndsm_rules.json'


def load_rules(root, source_id=SOURCE_ID):
    """(path, sha256) of the pre-registered rules; refuses when absent or different from NDSM."""
    from .. import heights_source as hs
    path = rules_path(root, source_id)
    if not path.exists():
        raise SystemExit(f'{path} is absent: the nDSM rules are pre-registered before any measure or judge run')
    if common.read_json(path) != NDSM:
        raise SystemExit(f'{path} differs from teacher_elevation.NDSM: record an amendment in both before running')
    if hs.PARAMS['top_fraction'] != NDSM['sampling']['top_fraction']:
        raise SystemExit('heights_source.PARAMS top_fraction differs from the registered sampling rule')
    return path, common.sha256_file(path)


# --- per-instance measures -------------------------------------------------------------------------------------------

def ndsm_class(height_m, share_above_2m, samples, nodata_share, rules=NDSM):
    t = rules['thresholds']
    if (height_m is None or share_above_2m is None or samples is None or samples < t['min_samples']
            or nodata_share is None or nodata_share > t['max_nodata_share']):
        return 'unjudged'
    if height_m >= t['elevated_height_m'] and share_above_2m >= t['elevated_share_above_2m']:
        return 'elevated'
    if height_m < t['flat_height_m'] and share_above_2m < t['flat_share_above_2m']:
        return 'flat'
    return 'mixed'


def _cells(values, matrix, polygon):
    from .. import heights_source as hs
    rows, cols = hs.pixel_centres_in(polygon, matrix, values.shape)
    return values[rows, cols]


def interior(ndsm, matrix, polygon, rules=NDSM):
    """Raw interior statistics: sample_polygon with the registered erosions, share above 2 m, nodata share."""
    from .. import heights_source as hs
    s, t = rules['sampling'], rules['thresholds']
    erosions = [float(e) for e in s['erode_m']]
    every = _cells(ndsm, matrix, polygon)
    nodata_share = float(np.mean(~np.isfinite(every))) if every.size else None
    stats = hs.sample_polygon(ndsm, matrix, polygon, erode_m=erosions[0], fallbacks=tuple(erosions[1:]),
                              min_pixels=s['min_pixels'], eave_quantile=s['eave_quantile'])
    if stats is None:
        last = erosions[-1]
        inner = polygon.buffer(-last) if last > 0 else polygon
        values = _cells(ndsm, matrix, inner)
        return {'height_m': None, 'eave_m': None, 'iqr_m': None, 'samples': int(np.isfinite(values).sum()), 'erode_m': last,
                'share_above_2m': None, 'nodata_share': nodata_share}
    erosion = float(stats['erode_m'])
    inner = polygon.buffer(-erosion) if erosion > 0 else polygon
    values = _cells(ndsm, matrix, inner)
    values = values[np.isfinite(values)]
    return {'height_m': stats['height'], 'eave_m': stats['eave'], 'iqr_m': stats['iqr'], 'samples': int(stats['samples']),
            'erode_m': erosion, 'share_above_2m': float(np.mean(values >= t['above_m'])) if values.size else None,
            'nodata_share': nodata_share}


def _sample_points(values, matrix, xy):
    """Value of the cell containing each (x, y); NaN outside the raster."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    inverse = np.linalg.inv(np.c_[np.asarray(matrix, float), [0, 0, 1]])
    colrow = np.c_[xy, np.ones(len(xy))] @ inverse
    cols, rows = np.floor(colrow[:, 0]).astype(int), np.floor(colrow[:, 1]).astype(int)
    h, w = values.shape
    out = np.full(len(xy), np.nan)
    inside = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
    out[inside] = values[rows[inside], cols[inside]]
    return out


def edge_step(ndsm, matrix, polygon, rules=NDSM):
    """(median step inside minus outside, share of steps >= edge_step_min_m, finite probe pairs) along ring normals."""
    from . import teacher_judges as tj
    p = rules['probes']
    points, normals = tj.ring_samples(polygon, float(p['edge_step_every_m']))
    if not len(points):
        return None, None, 0
    inner_m, outer_m = p['edge_step_offsets_m']
    inside = _sample_points(ndsm, matrix, points + normals * inner_m)
    outside = _sample_points(ndsm, matrix, points + normals * outer_m)
    ok = np.isfinite(inside) & np.isfinite(outside)
    if not ok.any():
        return None, None, 0
    steps = inside[ok] - outside[ok]
    return _num(np.median(steps)), _num(np.mean(steps >= p['edge_step_min_m'])), int(ok.sum())


def outside_ring(ndsm, matrix, polygon, others=(), rules=NDSM):
    """Median nDSM between buffer(1.5) and buffer(4.0) of the outline, other teacher polygons removed."""
    from shapely.ops import unary_union
    near, far = rules['probes']['outside_ring_m']
    ring = polygon.buffer(far).difference(polygon.buffer(near))
    if others:
        ring = ring.difference(unary_union(list(others)))
    if ring.is_empty:
        return None
    values = _cells(ndsm, matrix, ring)
    values = values[np.isfinite(values)]
    return _num(np.median(values)) if values.size else None


def veg_map(rgb, rows_per_chunk=512, exg_min=None):
    """Boolean ExG > veg_exg map of an RGB image (teacher_judges J1 rule), computed in row chunks."""
    exg_min = NDSM['probes']['veg_exg'] if exg_min is None else exg_min
    rgb = np.asarray(rgb)
    out = np.zeros(rgb.shape[:2], bool)
    for y0 in range(0, rgb.shape[0], rows_per_chunk):
        block = rgb[y0:y0 + rows_per_chunk, :, :3].astype(np.float32)
        exg = (2 * block[..., 1] - block[..., 0] - block[..., 2]) / (block.sum(axis=2) + 1.)
        out[y0:y0 + rows_per_chunk] = exg > exg_min
    return out


def veg_share(veg, polygon_px):
    """Share of native pixels (centres) inside the polygon that are vegetation; None when no pixel centre is inside."""
    from .. import heights_source as hs
    rows, cols = hs.pixel_centres_in(polygon_px, IDENTITY, veg.shape)
    return _num(np.mean(veg[rows, cols])) if rows.size else None


def measure_instance(ndsm, matrix, polygon_m, others_m=(), veg=None, polygon_px=None, rules=NDSM):
    """Every registered measure of one outline (metres) on the nDSM, plus veg_share on native pixels, and its class."""
    raw = interior(ndsm, matrix, polygon_m, rules)
    step, step_share, _ = edge_step(ndsm, matrix, polygon_m, rules)
    return {'height_m': _num(raw['height_m']), 'eave_m': _num(raw['eave_m']), 'iqr_m': _num(raw['iqr_m']), 'samples': raw['samples'],
            'erode_m': raw['erode_m'], 'share_above_2m': _num(raw['share_above_2m']), 'edge_step_m': step,
            'edge_step_share': step_share, 'outside_ring_m': outside_ring(ndsm, matrix, polygon_m, others_m, rules),
            'veg_share': veg_share(veg, polygon_px) if veg is not None and polygon_px is not None else None,
            'nodata_share': _num(raw['nodata_share']),
            'ndsm_class': ndsm_class(raw['height_m'], raw['share_above_2m'], raw['samples'], raw['nodata_share'], rules)}


def epoch_suspect(cls, join, rules=NDSM):
    """Flat while the image calls it a stable edge-supported building, or elevated while nothing else backs it."""
    join = join or {}
    edge, cross = join.get('edge_support'), join.get('cross_capture')
    if cls == 'flat' and edge is not None and edge >= rules['epoch_suspect']['edge_support_min'] and cross == 'matched':
        return True
    return bool(cls == 'elevated' and join.get('outline_kind') == 'unmatched' and cross == 'only_here')


def control_discs(bounds, polygons, key, n=None, rules=NDSM):
    """(discs, draws): seeded random 20 m2 discs inside the window, disjoint from every polygon buffered by 2 m."""
    from shapely.geometry import Point
    from shapely.ops import unary_union
    from shapely.prepared import prep
    c = rules['control']
    n = c['discs_per_window'] if n is None else n
    segments = 32
    vertices = 4 * segments                              # a polygonal disc whose area is exactly disc_area_m2
    radius = math.sqrt(2. * c['disc_area_m2'] / (vertices * math.sin(2. * math.pi / vertices)))
    w, s, e, north = (float(v) for v in bounds)
    blocked = unary_union([p.buffer(c['polygon_buffer_m']) for p in polygons]) if polygons else None
    blocked = prep(blocked) if blocked is not None and not blocked.is_empty else None
    rng = np.random.default_rng([int(c['seed']), _stable_int(key)])
    discs, draws = [], 0
    while len(discs) < n and draws < c['max_draws_per_window']:
        draws += 1
        x, y = rng.uniform(w + radius, e - radius), rng.uniform(s + radius, north - radius)
        disc = Point(x, y).buffer(radius, quad_segs=segments)
        if blocked is not None and blocked.intersects(disc):
            continue
        discs.append(disc)
    return discs, draws


def measure_window(wid, polygons, affine, gsd, ndsm, matrix, veg, tiles, epoch, joins, window_bounds=None, rules=NDSM):
    """Rows for every teacher polygon (native px) of one window, its control discs and the window's own statistics."""
    from shapely import affinity
    from shapely.geometry import box
    from shapely.strtree import STRtree

    from . import teacher_judges as tj
    e0, n0 = float(affine[0]), float(affine[3])
    to_m = [gsd, 0., 0., -gsd, e0, n0]                   # x = E0 + px * gsd, y = N0 - py * gsd
    to_px = [1. / gsd, 0., 0., -1. / gsd, -e0 / gsd, n0 / gsd]
    items = [(row, poly_px, affinity.affine_transform(poly_px, to_m)) for row, poly_px in polygons]
    geoms = [m for _, _, m in items]
    tree = STRtree(geoms) if geoms else None
    reach = float(rules['probes']['outside_ring_m'][1])
    rows = []
    for k, (row, poly_px, poly_m) in enumerate(items):
        others = [geoms[i] for i in tree.query(poly_m.buffer(reach)) if i != k] if tree is not None else []
        measured = measure_instance(ndsm, matrix, poly_m, others, veg, poly_px, rules)
        area_m2 = poly_px.area * gsd * gsd
        rows.append({'window': wid, 'index': row['index'], 'area_m2': _num(area_m2, 2), 'band': tj._band(area_m2), 'epoch': epoch,
                     **measured, 'epoch_suspect': epoch_suspect(measured['ndsm_class'], joins.get((wid, row['index'])), rules),
                     'tiles': list(tiles)})
    if window_bounds is None:
        size = veg.shape[1] * gsd
        window_bounds = (e0, n0 - size, e0 + size, n0)
    discs, draws = control_discs(window_bounds, geoms, wid, rules=rules)
    control = []
    for j, disc in enumerate(discs):
        others = [geoms[i] for i in tree.query(disc.buffer(reach))] if tree is not None else []
        measured = measure_instance(ndsm, matrix, disc, others, veg, affinity.affine_transform(disc, to_px), rules)
        control.append({'window': wid, 'index': j, 'kind': 'control', 'x': _num(disc.centroid.x, 2), 'y': _num(disc.centroid.y, 2),
                        'area_m2': _num(disc.area, 2), 'epoch': epoch, **measured, 'tiles': list(tiles)})
    cells = _cells(ndsm, matrix, box(*window_bounds))
    stats = {'window': wid, 'instances': len(rows), 'control_discs': len(control), 'control_draws': draws,
             'nodata_share': _num(np.mean(~np.isfinite(cells))) if cells.size else None, 'tiles': list(tiles),
             'classes': {c: sum(r['ndsm_class'] == c for r in rows) for c in NDSM_CLASSES}}
    return rows, control, stats


# --- leave-one-signal-out and the candidate policy -------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def ledger_policy_thresholds():
    """(min_edge, small_m2): the defaults of teacher_judges.recommend (ledger-policy-v1), read from its signature so the
    mirror below never carries its own copy of the policy's numbers."""
    import inspect

    from . import teacher_judges as tj
    params = inspect.signature(tj.recommend).parameters
    return float(params['min_edge'].default), float(params['small_m2'].default)


def recommend_without(entry, drop=None, min_edge=None, small_m2=None):
    """teacher_judges.recommend (ledger-policy-v1) mirrored line for line, with one signal replaced by its
    pre-registered no-information value ('outline' | 'cross' | 'edge' | 'area'; 'outline_none' is the semantics
    control). drop=None reproduces recommend(); the thresholds default to recommend()'s own (ledger_policy_thresholds);
    the entry itself is never modified."""
    default_edge, default_small = ledger_policy_thresholds()
    min_edge = default_edge if min_edge is None else min_edge
    small_m2 = default_small if small_m2 is None else small_m2
    e = dict(entry)
    if drop is not None:
        field, value = SEMANTICS_CONTROL if drop == 'outline_none' else DROPS[drop]
        e[field] = value
    kind = e.get('outline_kind')
    cross = e.get('cross_capture')
    edge = e.get('edge_support') or 0.
    if kind == 'matched' or (cross == 'matched' and edge >= min_edge):
        return 'keep'
    if kind == 'part':
        return 'part'
    if kind == 'unmatched' and (e.get('area_m2') or 0.) < small_m2 and cross != 'matched':
        return 'ignore'
    return 'review'


def policy_v2(recommendation, cls):
    """Candidate ledger-policy-v2: ignore requires flat or unjudged; keep requires not flat; the leg only demotes."""
    cls = cls or 'unjudged'
    if recommendation == 'keep' and cls == 'flat':
        return 'review'
    if recommendation == 'ignore' and cls not in ('flat', 'unjudged'):
        return 'review'
    return recommendation


def _four(values):
    return {c: sum(v == c for v in values) for c in REC_CLASSES}


def loso_counts(rows):
    """Four-class counts of the full policy, each registered drop and the outline None semantics control."""
    out = {'full_policy': _four([recommend_without(r) for r in rows])}
    for name in DROPS:
        out[name] = _four([recommend_without(r, name) for r in rows])
    out['outline_none'] = _four([recommend_without(r, 'outline_none') for r in rows])
    return out


def _class_block(rows, cls_key='ndsm_class', classes=NDSM_CLASSES):
    """n, counts, shares and window-bootstrap intervals of each class (windows with rows are the units)."""
    from . import teacher_judges as tj
    n = len(rows)
    counts = {c: sum(r.get(cls_key) == c for r in rows) for c in classes}
    per_window = {}
    for r in rows:
        slot = per_window.setdefault(r['window'], {c: 0 for c in classes})
        if r.get(cls_key) in slot:
            slot[r.get(cls_key)] += 1
    wins = sorted(per_window)
    den = [sum(per_window[w].values()) for w in wins]
    return {'n': n, 'windows': len(wins), 'counts': counts, 'shares': {c: _num(counts[c] / n) if n else None for c in classes},
            'ci95_window_bootstrap': {c: tj.window_bootstrap_share([per_window[w][c] for w in wins], den) if n else None
                                      for c in classes}}


def veg_split(rows, cls='elevated', cut=CONTROL_VEG_SPLIT):
    """Rows of one nDSM class split by veg_share (< cut, >= cut): how much of an elevated share is green canopy.
    Reporting only: not a registered rule, never deciding."""
    sub = [r for r in rows if r.get('ndsm_class') == cls]
    low = [r for r in sub if r.get('veg_share') is not None and r['veg_share'] < cut]
    high = [r for r in sub if r.get('veg_share') is not None and r['veg_share'] >= cut]
    n = len(sub)

    def part(group):
        return {'n': len(group), 'share': _num(len(group) / n) if n else None,
                'height_m': _spread([r.get('height_m') for r in group])}
    return {'class': cls, 'veg_share_cut': cut, 'n': n, 'veg_lt_cut': part(low), 'veg_ge_cut': part(high),
            'veg_share_none': n - len(low) - len(high), 'veg_share': _spread([r.get('veg_share') for r in sub]),
            'note': 'reporting only, not a registered rule: an elevated disc with little vegetation is a structure the teacher '
                    'did not map or non-green canopy; elevated is not roof-specific'}


def _share_block(rows, predicate):
    from . import teacher_judges as tj
    per_window = {}
    for r in rows:
        num, den = per_window.get(r['window'], (0, 0))
        per_window[r['window']] = (num + bool(predicate(r)), den + 1)
    wins = sorted(per_window)
    num, den = [per_window[w][0] for w in wins], [per_window[w][1] for w in wins]
    return {'n': sum(den), 'share': _num(sum(num) / sum(den)) if sum(den) else None,
            'ci95_window_bootstrap': tj.window_bootstrap_share(num, den) if sum(den) else None}


def _edge_read(rows, rules=NDSM):
    step_min = rules['probes']['edge_step_min_m']
    measured = [r for r in rows if r.get('edge_step_m') is not None]
    return {'n': len(rows), 'with_probes': len(measured), 'edge_step_m': _spread([r['edge_step_m'] for r in measured]),
            'edge_step_share': _spread([r.get('edge_step_share') for r in measured]),
            'stepped': _share_block(measured, lambda r: r['edge_step_m'] >= step_min) if measured else None}


def _drop_block(joined, full, alt, semantics, registered):
    from . import teacher_judges as tj
    counts = _four(alt)
    moved = [i for i in range(len(joined)) if alt[i] != full[i]]
    transitions, moved_classes, totals = {}, {}, {c: 0 for c in NDSM_CLASSES}
    for i in moved:
        t = f'{full[i]}->{alt[i]}'
        transitions[t] = transitions.get(t, 0) + 1
        cls = joined[i]['ndsm_class']
        moved_classes.setdefault(t, {c: 0 for c in NDSM_CLASSES})[cls] += 1
        totals[cls] += 1
    per_window = {}
    for i, j in enumerate(joined):
        num, den = per_window.get(j['window'], (0, 0))
        per_window[j['window']] = (num + (alt[i] == full[i]), den + 1)
    wins = sorted(per_window)
    num, den = [per_window[w][0] for w in wins], [per_window[w][1] for w in wins]
    return {'semantics': semantics, 'counts': counts, 'pre_registered': registered, 'reproduced': counts == registered,
            'moved': len(moved), 'agreement_share': _num(sum(num) / sum(den)) if sum(den) else None,
            'agreement_ci95_window_bootstrap': tj.window_bootstrap_share(num, den) if sum(den) else None,
            'transitions': dict(sorted(transitions.items())), 'moved_ndsm_classes': dict(sorted(moved_classes.items())),
            'moved_ndsm_class_totals': totals}


def judge_report(ledger_rows, ndsm_rows, control_rows, split, rules=NDSM):
    """(payload, loso_rows, v2_rows) for the judge cells; pure: the caller reads and writes files."""
    from . import teacher_judges as tj
    bands = [name for name, _, _ in tj.AREA_BANDS]
    by_key = {(r['window'], r['index']): r for r in ndsm_rows}
    judge_rows = [r for r in ledger_rows if split.get(r['window']) == 'judge']
    missing = [(r['window'], r['index']) for r in judge_rows if (r['window'], r['index']) not in by_key]
    if missing:
        raise SystemExit(f'{len(missing)} judge-cell ledger rows have no nDSM row (first {missing[:3]}): run measure first')
    joined = []
    for r in judge_rows:
        m = by_key[(r['window'], r['index'])]
        joined.append({'window': r['window'], 'index': r['index'], 'band': r.get('band'), 'area_m2': r.get('area_m2'),
                       'recommendation': r['recommendation'], 'outline_kind': r.get('outline_kind'),
                       'cross_capture': r.get('cross_capture'), 'edge_support': r.get('edge_support'),
                       **{k: m.get(k) for k in ('ndsm_class', 'height_m', 'share_above_2m', 'edge_step_m', 'edge_step_share',
                                                'outside_ring_m', 'veg_share', 'nodata_share', 'epoch_suspect')}})
    control_judge = [c for c in control_rows if split.get(c['window']) == 'judge']
    registered = rules['loso']['pre_registered_counts_on_4132_judge_rows']

    # A. nDSM class shares per recommendation class and area band, unjudged always visible, control discs beside
    block_a = {'by_recommendation': {},
               'control': {'discs': len(control_judge), **_class_block(control_judge), 'elevated_by_veg': veg_split(control_judge),
                           'placement': rules['control']['text']},
               'control_all_windows': {'discs': len(control_rows), **_class_block(control_rows),
                                       'elevated_by_veg': veg_split(control_rows)},
               'wording': rules['wording']}
    for rec in REC_CLASSES:
        sub = [j for j in joined if j['recommendation'] == rec]
        block_a['by_recommendation'][rec] = {
            'all': {**_class_block(sub), 'veg_share': _spread([j['veg_share'] for j in sub]),
                    'height_m': _spread([j['height_m'] for j in sub]), 'edge_step_m': _spread([j['edge_step_m'] for j in sub]),
                    'epoch_suspect': sum(bool(j['epoch_suspect']) for j in sub)},
            **{band: _class_block([j for j in sub if j['band'] == band]) for band in bands}}

    # B. leave one signal out
    full = [recommend_without(r) for r in judge_rows]
    stored_mismatches = sum(f != r['recommendation'] for f, r in zip(full, judge_rows, strict=True))
    stored_mismatches_all = sum(recommend_without(r) != r['recommendation'] for r in ledger_rows)
    min_edge, small_m2 = ledger_policy_thresholds()
    block_b = {'population': len(judge_rows), 'windows': len({r['window'] for r in judge_rows}),
               'policy_thresholds': {'min_edge': min_edge, 'small_m2': small_m2,
                                     'from': 'inspect.signature(teacher_judges.recommend) defaults'},
               'full_policy': _four(full), 'full_policy_pre_registered': registered['full_policy'],
               'full_policy_reproduced': _four(full) == registered['full_policy'],
               'stored_recommendations_reproduced': stored_mismatches == 0 and stored_mismatches_all == 0,
               'stored_recommendation_mismatches': stored_mismatches,
               'stored_recommendation_mismatches_all_cells': stored_mismatches_all,
               'drops': {}}
    for name in DROPS:
        alt = [recommend_without(r, name) for r in judge_rows]
        block_b['drops'][name] = _drop_block(joined, full, alt, rules['loso']['drops'][name], registered[name])
    control_alt = [recommend_without(r, 'outline_none') for r in judge_rows]
    block_b['semantics_control'] = {'name': 'outline_none', 'use': rules['loso']['semantics_control']['use'],
                                    **_drop_block(joined, full, control_alt, rules['loso']['semantics_control']['drop'],
                                                  rules['loso']['semantics_control']['counts'])}
    block_b['pre_registered_reproduced'] = bool(block_b['full_policy_reproduced'] and block_b['stored_recommendations_reproduced']
                                                and block_b['semantics_control']['reproduced']
                                                and all(d['reproduced'] for d in block_b['drops'].values()))

    # C. the leg against signals it never saw
    block_c = {'note': 'the nDSM leg never saw the outlines or the second capture; these are agreement tables, not accuracy',
               'by_cross_capture': {state: _class_block([j for j in joined if (j['cross_capture'] or 'none') == state])
                                    for state in ('matched', 'only_here', 'none')},
               'by_outline_kind': {kind: _class_block([j for j in joined if (j['outline_kind'] or 'none') == kind])
                                   for kind in sorted({j['outline_kind'] or 'none' for j in joined})}}

    # read condition on the ignore band
    ignore = [j for j in joined if j['recommendation'] == 'ignore']
    unjudged = sum(j['ndsm_class'] == 'unjudged' for j in ignore) / len(ignore) if ignore else None
    limit = rules['read_conditions']['ignore_band_unjudged_share_max']
    readable = bool(unjudged is not None and unjudged < limit)
    reads = {'ignore_band_readable': readable, 'unjudged_share_among_ignore': _num(unjudged), 'threshold_lt': limit,
             'rule': rules['read_conditions']['ignore_band'], 'read': 'ndsm_class' if readable else 'edge_step',
             'edge_step_read': {'ignore': _edge_read(ignore, rules),
                                'keep': _edge_read([j for j in joined if j['recommendation'] == 'keep'], rules),
                                'control': _edge_read(control_judge, rules)}}

    # candidate ledger-policy-v2 on every ledger row (judge cells summarised)
    v2_rows = []
    for r in ledger_rows:
        m = by_key.get((r['window'], r['index'])) or {}
        cls = m.get('ndsm_class')
        v2 = policy_v2(r['recommendation'], cls)
        v2_rows.append({'window': r['window'], 'index': r['index'], 'area_m2': r.get('area_m2'), 'band': r.get('band'),
                        'cells': split.get(r['window']), 'recommendation': r['recommendation'], 'ndsm_class': cls,
                        'epoch_suspect': m.get('epoch_suspect'), 'recommendation_v2': v2, 'v2_changed': v2 != r['recommendation']})

    def v2_summary(rows):
        transitions = {}
        for r in rows:
            if r['v2_changed']:
                t = f"{r['recommendation']}->{r['recommendation_v2']}"
                transitions[t] = transitions.get(t, 0) + 1
        return {'rows': len(rows), 'v1': _four([r['recommendation'] for r in rows]), 'v2': _four([r['recommendation_v2'] for r in rows]),
                'changed': sum(r['v2_changed'] for r in rows), 'transitions': dict(sorted(transitions.items()))}

    loso_rows = [{'window': j['window'], 'index': j['index'], 'band': j['band'], 'area_m2': j['area_m2'], 'ndsm_class': j['ndsm_class'],
                  'recommendation': full[i], 'by_drop': {name: recommend_without(judge_rows[i], name) for name in DROPS},
                  'outline_none': control_alt[i]} for i, j in enumerate(joined)]
    for row in loso_rows:
        row['moved_by'] = [name for name, rec in row['by_drop'].items() if rec != row['recommendation']]
    payload = {'population': {'cells': 'judge', 'windows': block_b['windows'], 'rows': len(judge_rows),
                              'ledger_rows_total': len(ledger_rows), 'ndsm_rows_total': len(ndsm_rows),
                              'control_discs_judge_cells': len(control_judge)},
               'blocks': {'A': block_a, 'B': block_b, 'C': block_c}, 'read_conditions': reads,
               'policy_v2': {'policy': LEDGER_POLICY_V2, 'judge_cells': v2_summary([r for r in v2_rows if r['cells'] == 'judge']),
                             'all_cells': v2_summary(v2_rows)},
               'epoch_suspect_judge_cells': sum(bool(j['epoch_suspect']) for j in joined)}
    return payload, loso_rows, v2_rows


def checkpoint_sentence(payload, epoch, gap):
    """The plan's checkpoint sentence with every number read from the payload (never typed). The control discs are named
    for where they are drawn (outside every teacher polygon), not 'off roof', and their elevated share is split by
    veg_share because canopy is not the only elevated surface there."""
    a, b = payload['blocks']['A'], payload['blocks']['B']
    keep, ignore, control = a['by_recommendation']['keep']['all'], a['by_recommendation']['ignore']['all'], a['control']

    def pct(value):
        return 'n/a' if value is None else f'{100 * value:.1f}'
    interval = keep['ci95_window_bootstrap']['elevated'] or [None, None]
    gap_text = f"{gap['min']:.0f}-{gap['max']:.0f}" if gap and gap.get('min') is not None else 'n/a'
    split = control.get('elevated_by_veg') or {}
    low = (split.get('veg_lt_cut') or {}).get('share')
    return (f"The LINZ 1 m nDSM ({epoch}, CC BY 4.0, {gap_text} days before the imagery by the STAC interval bounds) now sits "
            f"beside every 2025 teacher instance: keep rows read elevated in {pct(keep['shares']['elevated'])} percent "
            f"[{pct(interval[0])}, {pct(interval[1])}] against {pct(control['shares']['elevated'])} percent of control discs drawn "
            f"outside every teacher polygon (canopy or structures the teacher did not map: {pct(low)} percent of those elevated "
            f"discs have veg_share < {split.get('veg_share_cut')}), ignore rows flat in {pct(ignore['shares']['flat'])} percent "
            f"with {pct(ignore['shares']['unjudged'])} percent unjudged at 1 m; leave-one-signal-out (drop outline = outline_kind "
            f"'unmatched') moved {b['drops']['outline']['moved']} rows; the policy stays recommendations only.")


# --- catalog -----------------------------------------------------------------------------------------------------------

def cache_dir(root):
    return Path(root) / CACHE


def load_catalog(root, epoch):
    path = cache_dir(root) / epoch / 'catalog.json'
    if not path.exists():
        raise SystemExit(f'no catalog for {epoch}: run `catalog` first')
    return common.read_json(path)


def catalog(root, source_id, epoch):
    """Tiles of both layers meeting the teacher windows, with exact header bounds and STAC checksums."""
    from concurrent.futures import ThreadPoolExecutor

    from . import teacher_sources as ts
    root = Path(root)
    spec = epoch_spec(epoch)
    code = code_sha256(*CATALOG_CODE)
    src = ts.source(source_id)
    windows = common.read_json(ts.teacher_dir(root, src) / 'windows.json')
    order = windows['order']
    crs = (windows.get('geo') or {}).get('crs') or src['crs']
    xs, ys = [], []
    for w in order:
        left, bottom, right, top = w['bounds']
        xs += [left, right, right, left]
        ys += [bottom, bottom, top, top]
    lons, lats = ts.to_lonlat(crs, xs, ys)
    envelopes = [(min(lons[4 * k:4 * k + 4]), min(lats[4 * k:4 * k + 4]), max(lons[4 * k:4 * k + 4]), max(lats[4 * k:4 * k + 4]))
                 for k in range(len(order))]
    client = ts.s3_client()
    layers = {}
    for layer, meta in spec['layers'].items():
        collection = ts.s3_json(client, spec['bucket'], meta['prefix'] + 'collection.json')
        if collection.get('id') != meta['collection_id']:
            raise SystemExit(f"{layer} collection id {collection.get('id')} is not the registered {meta['collection_id']}")
        links = [link for link in collection.get('links', []) if link.get('rel') == 'item']

        def read_item(link, meta=meta):
            item_key = meta['prefix'] + Path(link['href']).name
            return link, item_key, ts.s3_json(client, spec['bucket'], item_key)

        with ThreadPoolExecutor(max_workers=8) as pool:
            items = list(pool.map(read_item, links))
        tiles, meeting = {}, []
        for link, item_key, item in items:
            name = Path(link['href']).stem
            bbox = item.get('bbox')
            if parse_tile(name) is None or not bbox:
                continue
            if not any(bbox[0] <= env[2] and bbox[2] >= env[0] and bbox[1] <= env[3] and bbox[3] >= env[1] for env in envelopes):
                continue
            meeting.append(name)
            asset = (item.get('assets') or {}).get('visual') or {}
            key = meta['prefix'] + Path(asset.get('href', f'./{name}.tiff')).name
            header = ts.read_header(f"s3://{spec['bucket']}/{key}")
            bounds = [header['left'], header['bottom'], header['right'], header['top']]
            if not any(_meets(bounds, w['bounds']) for w in order):
                continue
            predicted = tile_bounds(name)
            props = item.get('properties') or {}
            tiles[name] = {'key': key, 'item_key': item_key, 'bounds': bounds, 'item_checksum': link.get('file:checksum'),
                           'asset_checksum': asset.get('file:checksum'), 'expected_sha256': ts.multihash_sha256(asset.get('file:checksum')),
                           'start': props.get('start_datetime'), 'end': props.get('end_datetime'), 'lonlat_bbox': bbox,
                           'name_bounds_error_m': round(max(abs(a - b) for a, b in zip(bounds, predicted, strict=True)), 6),
                           'header': {k: header[k] for k in ('width', 'height', 'gsd_x_m', 'gsd_y_m', 'crs', 'dtype', 'compress', 'count')}}
            if header['crs'] != spec['crs'] or abs(header['gsd_x_m'] - spec['gsd_m']) > 1e-9:
                raise SystemExit(f'{layer} tile {name}: header {header["crs"]} {header["gsd_x_m"]} m is not {spec["crs"]} {spec["gsd_m"]} m')
        layers[layer] = {'collection_id': collection.get('id'), 'title': collection.get('title'), 'license': collection.get('license'),
                         'providers': collection.get('providers'), 'extent': collection.get('extent'), 'prefix': meta['prefix'],
                         'koordinates_layer': meta.get('koordinates_layer'), 'items': len(links),
                         'items_meeting_window_envelopes': sorted(meeting), 'tiles': dict(sorted(tiles.items()))}
    wt = {}
    for w in order:
        names = {layer: sorted(n for n, t in layers[layer]['tiles'].items() if _meets(t['bounds'], w['bounds'])) for layer in layers}
        if len({tuple(v) for v in names.values()}) != 1:
            raise SystemExit(f"window {w['id']}: DSM and DEM tiles differ {names}")
        wt[w['id']] = names['dsm']
    arithmetic = window_tiles(order)
    temporal = {layer: ((layers[layer]['extent'] or {}).get('temporal') or {}).get('interval', [[None, None]])[0] for layer in layers}
    result = {'version': VERSION, 'epoch': epoch, 'source': source_id, 'bucket': spec['bucket'], 'stac_root': STAC_ROOT,
              'crs': spec['crs'], 'gsd_m': spec['gsd_m'], 'nodata': spec['nodata'], 'layers': layers, 'window_tiles': wt,
              'straddling_windows': {w: n for w, n in wt.items() if len(n) > 1},
              'name_arithmetic_agrees': arithmetic == wt,
              'max_name_bounds_error_m': max((t['name_bounds_error_m'] for layer in layers.values() for t in layer['tiles'].values()),
                                             default=None),
              'temporal': temporal, 'temporal_matches_registry': all(v == spec['temporal'] for v in temporal.values()),
              'licence': LICENCE, 'module_sha256': code['teacher_elevation'], 'code_sha256': code, 'catalog_at': common.now_iso()}
    folder = cache_dir(root) / epoch
    folder.mkdir(parents=True, exist_ok=True)
    common.write_json_atomic(folder / 'catalog.json', result)
    return {'epoch': epoch, 'tiles': {layer: sorted(layers[layer]['tiles']) for layer in layers}, 'windows': len(wt),
            'straddling_windows': result['straddling_windows'], 'name_arithmetic_agrees': result['name_arithmetic_agrees'],
            'max_name_bounds_error_m': result['max_name_bounds_error_m'], 'temporal': temporal,
            'temporal_matches_registry': result['temporal_matches_registry'], 'module_sha256': result['module_sha256']}


# --- fetch -------------------------------------------------------------------------------------------------------------

def manifest_key(url):
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def manifest_entry(url, path, *, role, pair, epoch, capture_date, expected_sha256, http_status, status, sha256, nbytes, seconds,
                   note):
    return {'url': url, 'source': 'linz-elevation', 'licence': LICENCE['licence'], 'fetched_at': common.now_iso(),
            'http_status': http_status, 'status': status, 'path': str(path), 'sha256': sha256, 'bytes': nbytes, 'seconds': seconds,
            'capture_date': capture_date, 'role': role, 'pair': pair, 'epoch': epoch, 'expected_sha256': expected_sha256, 'note': note}


def _download(url, tmp, chunk=1 << 20, timeout=120):
    """Stream url to tmp in 1 MB chunks (no whole-file buffer): (http_status, bytes, sha256)."""
    import urllib.request
    digest, nbytes = hashlib.sha256(), 0
    request = urllib.request.Request(url, headers={'User-Agent': f'addr-{VERSION} (open-data research)'})
    with urllib.request.urlopen(request, timeout=timeout) as response, open(tmp, 'wb') as handle:
        status = response.status
        while block := response.read(chunk):
            handle.write(block)
            digest.update(block)
            nbytes += len(block)
    return status, nbytes, digest.hexdigest()


def fetch_one(url, path, rel, expected_sha256, meta, manifest):
    """One tile: skip a verified file, else download, verify against the STAC sha256, keep or unlink. Never raises."""
    path = Path(path)
    have = manifest.get(manifest_key(url)) or {}
    if expected_sha256 and path.exists() and have.get('status') == 'ok' and have.get('sha256') == expected_sha256:
        if common.sha256_file(path) == expected_sha256:
            return {**have, 'note': f'already verified {common.now_iso()}: sha256 matches the STAC checksum; not downloaded again'}
    tmp = Path(str(path) + '.part')
    started = time.monotonic()
    http_status, nbytes, sha, status = None, None, None, 'error'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        http_status, nbytes, sha = _download(url, tmp)
    except Exception as error:  # noqa: BLE001 - recorded per tile, never raised
        http_status = getattr(error, 'code', None)
        note = f'download failed: {type(error).__name__}: {str(error)[:160]}'
    else:
        if not expected_sha256:
            note = 'no STAC checksum to verify against: not kept'
        elif sha != expected_sha256:
            note = f'sha256 mismatch: got {sha}, the STAC multihash says {expected_sha256}; unlinked'
        else:
            os.replace(tmp, path)
            status, note = 'ok', 'sha256 verified against the STAC file:checksum multihash'
    if status != 'ok':
        tmp.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    return manifest_entry(url, rel, expected_sha256=expected_sha256, http_status=http_status, status=status, sha256=sha,
                          nbytes=nbytes, seconds=round(time.monotonic() - started, 2), note=note, **meta)


def fetch(root, source_id, epoch, workers=8, passes=3):
    """Download every catalogued tile of both layers; failed tiles are retried in later passes; never raises."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from . import teacher_sources as ts
    root = Path(root)
    epoch_spec(epoch)
    cat = load_catalog(root, epoch)
    client = ts.s3_client()
    cache = cache_dir(root)
    manifest_path = cache / 'manifest.json'
    manifest = common.read_json(manifest_path) if manifest_path.exists() else {}
    manifest.setdefault('version', VERSION)
    manifest.setdefault('entries', {})
    manifest['licence'] = LICENCE
    code = code_sha256(*CATALOG_CODE)
    manifest['module_sha256'], manifest['code_sha256'], manifest['last_run_at'] = code['teacher_elevation'], code, common.now_iso()
    jobs = []
    for layer in ('dsm', 'dem'):
        for name, tile in sorted(cat['layers'][layer]['tiles'].items()):
            url = client.url(cat['bucket'], tile['key'])
            rel = f'{epoch}/{layer}/{name}.tiff'
            meta = {'role': layer, 'pair': name, 'epoch': epoch, 'capture_date': f"{tile['start']}..{tile['end']}"}
            jobs.append((url, cache / rel, rel, tile['expected_sha256'], meta))
    lock = threading.Lock()
    started = time.monotonic()
    for attempt in range(1, passes + 1):
        todo = [j for j in jobs if (manifest['entries'].get(manifest_key(j[0])) or {}).get('status') != 'ok' or not j[1].exists()
                or attempt == 1]
        if not todo:
            break
        snapshot = dict(manifest['entries'])
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_one, *job, snapshot): job for job in todo}
            for k, future in enumerate(as_completed(futures), start=1):
                job = futures[future]
                try:
                    entry = future.result()
                except Exception as error:  # noqa: BLE001 - fetch_one never raises; recorded if it ever does
                    entry = manifest_entry(job[0], job[2], expected_sha256=job[3], http_status=None, status='error', sha256=None,
                                           nbytes=None, seconds=None, note=f'unexpected {type(error).__name__}: {error}', **job[4])
                with lock:
                    manifest['entries'][manifest_key(job[0])] = entry
                    common.write_json_atomic(manifest_path, manifest)
                print(json.dumps({'pass': attempt, 'done': k, 'of': len(todo), 'tile': job[4]['pair'], 'role': job[4]['role'],
                                  'status': entry['status'], 'mb': round((entry.get('bytes') or 0) / 2 ** 20, 1),
                                  'seconds': round(time.monotonic() - started, 1)}), file=sys.stderr, flush=True)
        if all((manifest['entries'].get(manifest_key(j[0])) or {}).get('status') == 'ok' for j in jobs):
            break
    entries = [manifest['entries'][manifest_key(j[0])] for j in jobs]
    ok = [e for e in entries if e['status'] == 'ok']
    return {'epoch': epoch, 'tiles': len(jobs), 'ok': len(ok), 'error': len(entries) - len(ok),
            'errors': {f"{e['role']}/{e['pair']}": e['note'] for e in entries if e['status'] != 'ok'},
            'bytes': {layer: sum(e['bytes'] or 0 for e in ok if e['role'] == layer) for layer in ('dsm', 'dem')},
            'seconds': round(time.monotonic() - started, 1), 'module_sha256': code['teacher_elevation']}


# --- measure -----------------------------------------------------------------------------------------------------------

def _check_dataset(ds, spec):
    if str(ds.crs) != spec['crs'] or abs(ds.res[0] - spec['gsd_m']) > 1e-9 or ds.nodata != spec['nodata']:
        raise SystemExit(f'{ds.name}: {ds.crs} {ds.res} nodata {ds.nodata} differs from the registry')


def read_layer(paths, bounds, spec):
    """(float32 array, affine) over integer-metre bounds: a window read of one tile, rasterio.merge across tiles."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.merge import merge
    from rasterio.windows import Window, from_bounds
    left, bottom, right, top = bounds
    nodata = spec['nodata']
    height, width = int(round(top - bottom)), int(round(right - left))
    if len(paths) == 1:
        with rasterio.open(paths[0]) as ds:
            _check_dataset(ds, spec)
            win = from_bounds(left, bottom, right, top, transform=ds.transform)
            win = Window(int(round(win.col_off)), int(round(win.row_off)), width, height)
            array = ds.read(1, window=win, boundless=True, fill_value=nodata)
            transform = ds.window_transform(win)
        return array.astype(np.float32), transform
    datasets = [rasterio.open(p) for p in paths]
    try:
        for ds in datasets:
            _check_dataset(ds, spec)
        res = datasets[0].res
        # rasterio.merge can leave the last row / column of the box unfilled; pad one cell east and south, cut back
        array, transform = merge(datasets, bounds=(left, bottom - res[1], right + res[0], top), res=res, nodata=nodata,
                                 resampling=Resampling.nearest, indexes=[1])
    finally:
        for ds in datasets:
            ds.close()
    return array[0, :height, :width].astype(np.float32), transform


def load_joins(folder):
    """{(window, index): outline_kind, edge_support, cross_capture} for teacher instances, as the ledger joins them."""
    from . import teacher_judges as tj
    joins = {}
    for r in tj._read_rows(folder / 'outlines_rows.jsonl'):
        if r.get('arm') == 'teacher':
            joins.setdefault((r['window'], r['index']), {})['outline_kind'] = r.get('kind')
    for r in tj._read_rows(folder / 'edge_support_rows.jsonl'):
        if r.get('arm') == 'teacher':
            joins.setdefault((r['window'], r['index']), {})['edge_support'] = r.get('edge_support')
    meta = folder / f'cross_capture_{OTHER_CAPTURE}.json'
    windows = {w['window'] for w in common.read_json(meta).get('windows', [])} if meta.exists() else set()
    matched = {(r['window'], r['index']) for r in tj._read_rows(folder / f'cross_capture_{OTHER_CAPTURE}_rows.jsonl')}
    for key, join in joins.items():
        join['cross_capture'] = ('matched' if key in matched else 'only_here') if key[0] in windows else None
    return joins


def join_files_sha256(folder):
    """{file: sha256 or None} of the judge files load_joins reads (part of the measure cache key)."""
    folder = Path(folder)
    return {name: common.sha256_file(folder / name) if (folder / name).exists() else None for name in JOIN_FILES}


def window_inputs(window, record_sha256, native_sha256, tiles, *, rules_sha256, crops_sha256, joins_sha256, code):
    """The per-window measure cache key: every input the window's rows depend on. A cached window is reused only when all
    of them are unchanged, so a re-cut PNG, a changed crops.json geometry, changed judge joins or any code change
    recompute it."""
    return {'version': VERSION, 'module_sha256': code['teacher_elevation'], 'code_sha256': dict(code),
            'rules_sha256': rules_sha256, 'record_sha256': record_sha256, 'crops_sha256': crops_sha256,
            'window': {k: window.get(k) for k in ('bounds', 'native_affine', 'native_file', 'sha256')},
            'native_sha256': native_sha256, 'joins_sha256': dict(joins_sha256), 'tiles': tiles}


def _swapusage():
    import subprocess
    try:
        return subprocess.run(['sysctl', '-n', 'vm.swapusage'], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:  # noqa: BLE001 - informational only
        return None


def jobgate_line(log_path):
    path = Path(log_path) if log_path else None
    if not path or not path.exists():
        return None
    for line in path.read_text(errors='replace').splitlines():
        if line.startswith('jobgate '):
            return line.strip()
    return None


def _max_rss_mb():
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / 2 ** 20 if sys.platform == 'darwin' else rss / 2 ** 10, 1)


def _interval(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def epoch_gap_days(lidar, imagery):
    """Days between the LiDAR interval and the imagery interval: imagery start - LiDAR end .. imagery end - LiDAR start."""
    ls, le = (_interval(v) for v in lidar)
    im_s, im_e = (_interval(v) for v in imagery)
    if None in (ls, le, im_s, im_e):
        return None
    return {'min': round((im_s - le).total_seconds() / 86400., 1), 'max': round((im_e - ls).total_seconds() / 86400., 1),
            'method': 'imagery interval start minus LiDAR interval end, and imagery end minus LiDAR start, from the STAC '
                      'interval bounds in UTC; LINZ encodes both intervals as local midnight (11:00Z under NZDT), so the '
                      'counts hold in either convention as long as both intervals use the same one (reading the LiDAR as '
                      'local dates and the imagery as UTC dates shortens both ends by one day); the imagery follows the LiDAR'}


def measure(root, source_id, epoch, log_path=MEASURE_LOG, limit=None):
    """nDSM rows for every teacher instance and control disc; per-window results cached for an idempotent rerun."""
    from PIL import Image

    from .. import heights_source as hs
    from . import teacher_judges as tj
    from . import teacher_sources as ts
    root = Path(root)
    spec = epoch_spec(epoch)
    rules_file, rules_sha = load_rules(root, source_id)
    swap = _swapusage()
    cat = load_catalog(root, epoch)
    manifest_path = cache_dir(root) / 'manifest.json'
    manifest = common.read_json(manifest_path) if manifest_path.exists() else {'entries': {}}
    by_path = {e.get('path'): e for e in manifest['entries'].values()}
    for layer in ('dsm', 'dem'):
        for name, tile in cat['layers'][layer]['tiles'].items():
            rel = f'{epoch}/{layer}/{name}.tiff'
            entry = by_path.get(rel) or {}
            if entry.get('status') != 'ok' or entry.get('sha256') != tile['expected_sha256'] or not (cache_dir(root) / rel).exists():
                raise SystemExit(f'{rel} is not a verified download: run `fetch` first')
    src = ts.source(source_id)
    folder = tj.judges_dir(root, src)
    joins = load_joins(folder)
    teacher = ts.teacher_dir(root, src)          # the folder hires_teacher.set_source points at, without rebinding its globals
    crops_path = teacher / 'crops.json'
    crops = common.read_json(crops_path)
    gsd = float(crops['gsd_m'])
    code = code_sha256(*MEASURE_CODE)
    shared = {'rules_sha256': rules_sha, 'crops_sha256': common.sha256_file(crops_path),
              'joins_sha256': join_files_sha256(folder), 'code': code}
    partial_dir = cache_dir(root) / epoch / 'measure'
    partial_dir.mkdir(parents=True, exist_ok=True)
    pad = float(NDSM['sampling']['read_pad_m'])
    started = time.monotonic()
    windows = crops['windows'][:limit] if limit else crops['windows']
    results, reused = [], 0
    for k, window in enumerate(windows, start=1):
        wid = window['id']
        names = cat['window_tiles'][wid]
        record_path = teacher / 'windows' / wid / 'record.json'
        tiles = {layer: {n: cat['layers'][layer]['tiles'][n]['expected_sha256'] for n in names} for layer in ('dsm', 'dem')}
        inputs = window_inputs(window, common.sha256_file(record_path), common.sha256_file(root / window['native_file']), tiles,
                               **shared)
        part = partial_dir / f'{wid}.json'
        if part.exists():
            cached = common.read_json(part)
            if cached.get('inputs') == inputs:
                results.append(cached)
                reused += 1
                continue
        record = common.read_json(record_path)
        polygons = tj.teacher_polygons(record)
        left, bottom, right, top = window['bounds']
        read_bounds = (math.floor(left - pad), math.floor(bottom - pad), math.ceil(right + pad), math.ceil(top + pad))
        layer_arrays = {}
        for layer in ('dsm', 'dem'):
            paths = [cache_dir(root) / epoch / layer / f'{n}.tiff' for n in names]
            layer_arrays[layer] = read_layer(paths, read_bounds, spec)
        (dsm, transform), (dem, transform_dem) = layer_arrays['dsm'], layer_arrays['dem']
        if dsm.shape != dem.shape or tuple(transform) != tuple(transform_dem):
            raise SystemExit(f'{wid}: DSM and DEM windows differ ({dsm.shape} {transform} vs {dem.shape} {transform_dem})')
        values = hs.ndsm(dsm, dem, nodata=spec['nodata'])
        del dsm, dem, layer_arrays
        matrix = hs.gdal_to_matrix(transform)
        rgb = np.asarray(Image.open(root / window['native_file']).convert('RGB'))
        veg = veg_map(rgb)
        del rgb
        rows, control, stats = measure_window(wid, polygons, window['native_affine'], gsd, values, matrix, veg, names, epoch, joins,
                                              window_bounds=tuple(window['bounds']))
        stats['read'] = 'merge' if len(names) > 1 else 'window'
        stats['read_bounds'] = list(read_bounds)
        result = {'inputs': inputs, 'window': wid, 'rows': rows, 'control': control, 'stats': stats}
        common.write_json_atomic(part, result)
        results.append(result)
        del veg, values, record, polygons
        print(json.dumps({'window': k, 'of': len(windows), 'id': wid, 'instances': len(rows), 'read': stats['read'],
                          'reused': reused, 'seconds': round(time.monotonic() - started, 1), 'max_rss_mb': _max_rss_mb()}),
              file=sys.stderr, flush=True)
    if limit:
        return {'limited': True, 'windows': len(results), 'reused': reused,
                'rows': sum(len(r['rows']) for r in results), 'note': 'per-window cache only; no judge file written'}
    rows = [row for r in results for row in r['rows']]
    control = [row for r in results for row in r['control']]
    keys = [(r['window'], r['index']) for r in rows]
    if len(set(keys)) != len(keys):
        raise SystemExit('duplicate (window, index) rows')
    _write_jsonl_atomic(folder / f'ndsm_{epoch}_rows.jsonl', rows)
    _write_jsonl_atomic(folder / f'ndsm_control_{epoch}_rows.jsonl', control)
    payload = summarise_measure(root, source_id, epoch, spec, cat, rows, control, [r['stats'] for r in results], rules_file,
                                rules_sha, jobgate_line(log_path), swap,
                                provenance={'module_sha256': code['teacher_elevation'], 'code_sha256': code,
                                            'inputs': {'crops_sha256': shared['crops_sha256'],
                                                       'joins_sha256': shared['joins_sha256'],
                                                       'manifest_sha256': common.sha256_file(manifest_path)}})
    payload['seconds'] = round(time.monotonic() - started, 1)
    payload['windows_reused_from_cache'] = reused
    payload['max_rss_mb'] = _max_rss_mb()
    common.write_json_atomic(folder / f'ndsm_{epoch}.json', payload)
    return {k: payload[k] for k in ('version', 'epoch', 'instances', 'control_discs', 'epoch_gap_days', 'epoch_suspect', 'seconds',
                                    'module_sha256')}


def summarise_measure(root, source_id, epoch, spec, cat, rows, control, per_window, rules_file, rules_sha, jobgate, swap,
                      provenance=None):
    from . import teacher_judges as tj
    from . import teacher_sources as ts
    bands = [name for name, _, _ in tj.AREA_BANDS]
    imagery_catalog = Path(root) / ts.source(source_id)['cache'] / 'catalog.json'
    imagery = None
    if imagery_catalog.exists():
        imagery = ((common.read_json(imagery_catalog).get('extent') or {}).get('temporal') or {}).get('interval', [None])[0]
    lidar = cat['temporal']['dsm']
    tiles = cat['layers']
    starts = [t['start'] for layer in tiles.values() for t in layer['tiles'].values() if t.get('start')]
    ends = [t['end'] for layer in tiles.values() for t in layer['tiles'].values() if t.get('end')]
    suspects = [r for r in rows if r['epoch_suspect']]
    return {
        'version': VERSION, 'judge': 'ndsm', 'source': source_id, 'epoch': epoch, 'computed_at': common.now_iso(),
        'rules': {'path': str(Path(rules_file).relative_to(root)) if Path(rules_file).is_relative_to(root) else str(rules_file),
                  'sha256': rules_sha, 'version': NDSM['version'], **rules_file_times(rules_file)},
        **(provenance or {}),
        'licence': LICENCE,
        'lidar': {'title': spec['title'], 'interval': lidar, 'interval_dem': cat['temporal'].get('dem'),
                  'item_interval': [min(starts) if starts else None, max(ends) if ends else None],
                  'host': spec['host'], 'producer': spec['producer'], 'licensor': spec['licensor'],
                  'collections': {layer: {'stac_id': tiles[layer]['collection_id'], 'title': tiles[layer]['title'],
                                          'koordinates_layer': tiles[layer]['koordinates_layer'], 'prefix': tiles[layer]['prefix'],
                                          'license': tiles[layer]['license']} for layer in tiles}},
        'linz_product_spec_95': {'vertical_m_95': spec['product_spec_95']['vertical_m'],
                                 'horizontal_m_95': spec['product_spec_95']['horizontal_m'],
                                 'vertical_datum': spec['vertical_datum'], 'gsd_m': spec['gsd_m'], 'crs': spec['crs'],
                                 'text': PRODUCT_SPEC_TEXT},
        'stac_ids': {layer: tiles[layer]['collection_id'] for layer in tiles},
        'imagery': {'source': source_id, 'interval': imagery, 'from': str(imagery_catalog.relative_to(root)) + ' extent.temporal'},
        'epoch_gap_days': epoch_gap_days(lidar, imagery) if imagery else None,
        'item_checksums': {layer: {n: t['expected_sha256'] for n, t in tiles[layer]['tiles'].items()} for layer in tiles},
        'tiles': sorted(tiles['dsm']['tiles']), 'straddling_windows': cat['straddling_windows'],
        'instances': len(rows), 'windows': len(per_window), 'control_discs': len(control),
        'measure_gate': {'rows_expected': NDSM['measure_gate']['rows'], 'rows': len(rows), 'holds': len(rows) == NDSM['measure_gate']['rows']},
        'per_window': [{k: pw.get(k) for k in ('window', 'tiles', 'read', 'nodata_share', 'instances', 'control_discs', 'control_draws')}
                       for pw in per_window],
        'per_window_nodata_share': _spread([pw.get('nodata_share') for pw in per_window]),
        'class_shares': {'all': _class_block(rows), 'by_band': {band: _class_block([r for r in rows if r['band'] == band]) for band in bands}},
        'control_shares': {'discs': len(control), **_class_block(control), 'elevated_by_veg': veg_split(control),
                           'placement': NDSM['control']['text']},
        'epoch_suspect': {'count': len(suspects), 'flat_while_edge_and_cross_matched': sum(r['ndsm_class'] == 'flat' for r in suspects),
                          'elevated_while_unmatched_and_only_here': sum(r['ndsm_class'] == 'elevated' for r in suspects),
                          'note': 'recorded per instance, never deciding'},
        'distributions': {cls: {'height_m': _spread([r['height_m'] for r in rows if r['ndsm_class'] == cls]),
                                'veg_share': _spread([r['veg_share'] for r in rows if r['ndsm_class'] == cls]),
                                'edge_step_m': _spread([r['edge_step_m'] for r in rows if r['ndsm_class'] == cls])} for cls in NDSM_CLASSES},
        'erode_m_used': {str(e): sum(r['erode_m'] == e for r in rows) for e in NDSM['sampling']['erode_m']},
        'jobgate': jobgate, 'swapusage_at_start': swap,
        'note': ('nDSM = LINZ 1 m DSM minus DEM (first-return surface: canopy is elevated too); elevated surface present, never '
                 'roof confirmed; every figure is agreement between a teacher instance and the surface model, never accuracy; '
                 'epoch_suspect is recorded, never deciding; nothing here is a label'),
    }


# --- judge -------------------------------------------------------------------------------------------------------------

def judge(root, source_id, epoch, cells='judge'):
    from . import teacher_judges as tj
    from . import teacher_sources as ts
    if cells != 'judge':
        raise SystemExit('the pre-registered judge runs on the judge cells only')
    root = Path(root)
    epoch_spec(epoch)
    rules_file, rules_sha = load_rules(root, source_id)
    code = code_sha256(*JUDGE_CODE)
    folder = tj.judges_dir(root, ts.source(source_id))
    ledger_path, ledger_json = folder / 'ledger_rows.jsonl', folder / 'ledger.json'
    before = {p.name: common.sha256_file(p) for p in (ledger_path, ledger_json) if p.exists()}
    ndsm_path, control_path = folder / f'ndsm_{epoch}_rows.jsonl', folder / f'ndsm_control_{epoch}_rows.jsonl'
    summary_path = folder / f'ndsm_{epoch}.json'
    if not ndsm_path.exists() or not summary_path.exists():
        raise SystemExit('run `measure` first')
    ledger_rows, ndsm_rows, control_rows = tj._read_rows(ledger_path), tj._read_rows(ndsm_path), tj._read_rows(control_path)
    split = tj.cell_split(root, source_id)
    payload, loso_rows, v2_rows = judge_report(ledger_rows, ndsm_rows, control_rows, split)
    measured = common.read_json(summary_path)
    gap = measured.get('epoch_gap_days')
    payload = {'version': VERSION, 'judge': 'ledger-loso', 'source': source_id, 'epoch': epoch, 'cells': cells,
               'computed_at': common.now_iso(), 'module_sha256': code['teacher_elevation'], 'code_sha256': code,
               'rules': {'path': str(rules_file.relative_to(root)) if rules_file.is_relative_to(root) else str(rules_file), 'sha256': rules_sha,
                         **rules_file_times(rules_file)},
               'inputs': {'ledger_rows': {'path': ledger_path.name, 'sha256': before.get(ledger_path.name), 'rows': len(ledger_rows)},
                          'ledger_json': {'path': ledger_json.name, 'sha256': before.get(ledger_json.name)},
                          'ndsm_rows': {'path': ndsm_path.name, 'sha256': common.sha256_file(ndsm_path), 'rows': len(ndsm_rows)},
                          'control_rows': {'path': control_path.name, 'sha256': common.sha256_file(control_path),
                                           'rows': len(control_rows)},
                          'ndsm_summary': {'path': summary_path.name, 'sha256': common.sha256_file(summary_path)}},
               'licence': LICENCE, 'epoch_gap_days': gap, 'split': 'teacher_judges.cell_split(root, source) == judge',
               **payload,
               'wording': NDSM['wording'], 'policy_v1': 'ledger-policy-v1 unchanged; ledger_rows.jsonl and ledger.json never written'}
    payload['checkpoint_sentence'] = checkpoint_sentence(payload, epoch, gap)
    _write_jsonl_atomic(folder / f'ledger_loso_{epoch}_rows.jsonl', loso_rows)
    _write_jsonl_atomic(folder / f'ledger_v2_{epoch}_rows.jsonl', v2_rows)
    common.write_json_atomic(folder / f'ledger_loso_{epoch}.json', payload)
    after = {p.name: common.sha256_file(p) for p in (ledger_path, ledger_json) if p.exists()}
    if after != before:
        raise SystemExit('ledger_rows.jsonl or ledger.json changed during the judge run')
    b = payload['blocks']['B']
    return {'full_policy': b['full_policy'], 'drops': {name: d['counts'] for name, d in b['drops'].items()},
            'moved': {name: d['moved'] for name, d in b['drops'].items()}, 'semantics_control': b['semantics_control']['counts'],
            'pre_registered_reproduced': b['pre_registered_reproduced'],
            'stored_recommendations_reproduced': b['stored_recommendations_reproduced'],
            'module_sha256': payload['module_sha256'], 'read_conditions': {
                k: payload['read_conditions'][k] for k in ('ignore_band_readable', 'unjudged_share_among_ignore', 'read')},
            'checkpoint_sentence': payload['checkpoint_sentence']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', choices=('catalog', 'fetch', 'measure', 'judge'))
    parser.add_argument('--source', default=SOURCE_ID)
    parser.add_argument('--epoch', default=None, help='elevation epoch (default: the registered epoch of --source)')
    parser.add_argument('--root', default='.')
    parser.add_argument('--workers', type=int, default=8, help='fetch threads')
    parser.add_argument('--cells', choices=('judge',), default='judge', help='judge: the pre-registered judge cells only')
    parser.add_argument('--limit', type=int, help='measure: first N windows into the per-window cache only (no judge file)')
    parser.add_argument('--log', default=MEASURE_LOG, help='measure: job log whose jobgate header line is recorded')
    args = parser.parse_args(argv)
    epoch = args.epoch or EPOCH_FOR_SOURCE.get(args.source)
    if epoch is None:
        raise SystemExit(f'no elevation epoch registered for {args.source}')
    epoch_spec(epoch)
    if EPOCH_FOR_SOURCE.get(args.source) != epoch:
        raise SystemExit(f'{args.source} pairs with {EPOCH_FOR_SOURCE.get(args.source)}, not {epoch}')
    exit_code = 0
    if args.stage == 'catalog':
        result = catalog(args.root, args.source, epoch)
    elif args.stage == 'fetch':
        result = fetch(args.root, args.source, epoch, workers=args.workers)
    elif args.stage == 'measure':
        result = measure(args.root, args.source, epoch, log_path=args.log, limit=args.limit)
    else:
        result = judge(args.root, args.source, epoch, cells=args.cells)
        if not result['pre_registered_reproduced']:
            print('LOSO counts differ from the pre-registered tuples, or recommend_without no longer reproduces the stored '
                  'ledger-policy-v1 recommendations (outputs written for inspection)', file=sys.stderr)
            exit_code = 1
    json.dump(result, sys.stdout, indent=1, sort_keys=True, default=str)
    sys.stdout.write('\n')
    sys.stdout.flush()                                   # the done line must be the log's last line
    print(f'{args.stage} done {common.now_iso()}', file=sys.stderr, flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
