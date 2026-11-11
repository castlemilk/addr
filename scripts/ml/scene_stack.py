"""Esri Wayback dates registered onto the saved scene grid; stack statistics and edge consensus.

The saved scene PNG is the registration reference (the pilot used the newest dated
capture; this reverses it so every raster shares the saved image's pixel grid and
affine). Each dated capture is resampled to the scene grid by `imagery_dates.capture`
exactly as `scene_temporal.worker` does, then registered with
`temporal_evidence.register(reference=saved, candidate=capture)`. Its existing
RANSAC gates apply unchanged, plus one acceptance term: the median hold-out residual
(`check_median_residual_px`) must be <= 2 px.

Stack statistics (per-pixel median/min/max/count/ptp after median-gray brightness
normalisation, the `stable_composite` rule) and the edge-consensus raster are only
computed when at least two captures are accepted in addition to the saved reference
(>= 3 views). Per owned roof, an ECC translation between the saved gray and each
aligned gray is recorded (`local_shift_px`, `local_corr`) and never applied.

These rasters are diagnostic *views*. The saved PNG stays the sole evidence gate:
any line detected on a stack view must still pass `line_evidence` on the original
RGB. Nothing here is super-resolution, sharpening or inpainting, nothing is written
under data/scenes, and no SAM3 inference runs. Assets and the report live under
data/roof-review-loop/multidate/stacks/<stack_id>/.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import requests
from PIL import Image

from . import imagery_dates
from .imagery_dates import capture as fetch_capture
from .imagery_dates import discover as discover_acquisitions
from .roof_line_fit import enhance_roof_detail
from .scene_batch import signature
from .scene_temporal import DIRECTORY
from .scene_training import atomic_json
from .temporal_evidence import register

VERSION = 'scene-stack-v1'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
STACKS = DIRECTORY/'stacks'
CACHE = DIRECTORY/'cache'
MEDIAN_RESIDUAL_PX = 2.
LOCAL_SHIFT_PX = 3.
LOCAL_PAD_PX = 8
LOCAL_MIN_SIDE_PX = 16
MIN_ACCEPTED = 2
CANNY = (50, 150)
EDGE_DILATE_PX = 1
EDGE_BORDER_PX = 3   # aligned views: ignore edges this close to the warp footprint (the black border is not imagery)
STACK_ASSETS = ('stack-median.png', 'stack-min.png', 'stack-max.png', 'disagreement.png', 'edge-consensus.png',
                'valid-count.png')
VIEW_NAMES = ('stack-median', 'edge-consensus')
GATES = {'median_residual_px_max': MEDIAN_RESIDUAL_PX, 'local_shift_px_max': LOCAL_SHIFT_PX,
         'min_accepted': MIN_ACCEPTED, 'existing': 'temporal_evidence.register'}
NOTE = ('Diagnostic views registered onto the saved scene grid. The saved PNG remains the sole evidence gate: '
        'lines from stack views must still pass line_evidence on the original RGB. Not super-resolution, '
        'sharpening or inpainting; no production file is written; no SAM3 inference.')
FETCH_ERRORS = (requests.RequestException, OSError, ValueError, KeyError, TypeError)
_ECC_CRITERIA = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def _round(value, digits=4):
    if value is None:
        return None
    value = float(value)
    return None if not math.isfinite(value) else round(value, digits)


def _gray(image):
    return cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)


def stack_id(image_sha256, accepted_ids):
    """Content address: saved image identity + sorted accepted acquisitions + this module."""
    return signature([image_sha256, sorted(accepted_ids), CODE_SHA256])[:16]


def stack_path(root, identifier):
    if not re.fullmatch(r'[a-f0-9]{16}', identifier or ''):
        raise ValueError('Invalid stack id')
    return Path(root)/STACKS/identifier


def load_scene(scene_id, root):
    """Saved latest.json + PNG, sha256-verified; read-only."""
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', scene_id or ''):
        raise ValueError('Choose a valid scene')
    folder = Path(root)/'data/scenes'/scene_id
    scene = json.loads((folder/'latest.json').read_text())
    data = (folder/f'{scene["image_sha256"]}.png').read_bytes()
    if hashlib.sha256(data).hexdigest() != scene['image_sha256']:
        raise ValueError('Source image identity changed')
    image = Image.open(folder/f'{scene["image_sha256"]}.png').convert('RGB')
    if image.size != (scene['image_width'], scene['image_height']):
        raise ValueError('Saved image size does not match latest.json')
    return scene, image


def registration_gate(report):
    """Existing register() gates first, then the median hold-out residual budget."""
    if report.get('status') != 'accepted':
        return False, report.get('reason') or report.get('status') or 'Registration rejected'
    residual = report.get('check_median_residual_px')
    if isinstance(residual, bool) or not isinstance(residual, (int, float)) or not math.isfinite(residual):
        return False, 'Median hold-out residual unavailable'
    if residual > MEDIAN_RESIDUAL_PX:
        return False, f'Median hold-out residual {residual:g} px exceeds {MEDIAN_RESIDUAL_PX:.1f} px'
    return True, f'Existing register() gates and median hold-out residual {residual:g} px <= {MEDIAN_RESIDUAL_PX:.1f} px'


def _registration(report, accepted, reason):
    check = report.get('check_matches') or 0
    inliers = report.get('check_inliers')
    return {'accepted': bool(accepted), 'status': report.get('status'), 'reason': reason,
            'matches': report.get('matches'), 'fit_inliers': report.get('fit_inliers'),
            'holdout_matches': check or None, 'holdout_inliers': inliers,
            'holdout_ratio': _round(inliers/check) if check and inliers is not None else None,
            'median_residual_px': _round(report.get('check_median_residual_px')),
            'coverage': _round(report.get('check_coverage')), 'scale': _round(report.get('scale'), 6),
            'rotation_deg': _round(report.get('rotation_deg')), 'shift_px': _round(report.get('center_shift_px')),
            'transform': report.get('transform')}


def register_capture(saved, capture):
    """register(reference=saved, candidate=capture) with the extra residual gate applied."""
    try:
        aligned, valid, report = register(saved, capture)
    except cv2.error as error:
        report = {'status': 'rejected', 'reason': f'Registration failed: {str(error)[:200]}'}
        aligned = valid = None
    accepted, reason = registration_gate(report)
    if not accepted:
        aligned = valid = None
    return aligned, valid, _registration(report, accepted, reason)


def _bbox(ring, shape, pad):
    """Padded, image-clipped bbox and the unpadded ring extent (width, height) in px."""
    points = np.asarray(ring, float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
        raise ValueError('Roof ring must be at least three finite pixel points')
    height, width = shape[:2]
    x0 = max(0, int(math.floor(points[:, 0].min())) - pad)
    y0 = max(0, int(math.floor(points[:, 1].min())) - pad)
    x1 = min(width, int(math.ceil(points[:, 0].max())) + pad)
    y1 = min(height, int(math.ceil(points[:, 1].max())) + pad)
    return [x0, y0, x1, y1], tuple(float(v) for v in np.ptp(points, axis=0))


def local_refinement(saved_gray, aligned_gray, ring, *, valid=None, footprint=None, pad=LOCAL_PAD_PX):
    """ECC translation of one roof between saved and aligned gray; recorded, never applied.

    `local_shift_px` maps saved pixel coordinates to where the roof appears in the aligned
    capture (saved + shift = aligned). `within_budget` is None when ECC did not converge.
    """
    shapes = {np.shape(a)[:2] for a in (saved_gray, aligned_gray, valid, footprint) if a is not None}
    if len(shapes) != 1 or np.ndim(saved_gray) != 2:
        raise ValueError('saved_gray, aligned_gray and masks must be HxW arrays on the same grid')
    box, extent = _bbox(ring, saved_gray.shape, pad)
    x0, y0, x1, y1 = box
    row = {'bbox_px': box, 'local_shift_px': None, 'local_shift_norm_px': None, 'local_corr': None,
           'within_budget': None, 'valid_fraction': None, 'footprint_fraction': None, 'error': None}
    if valid is not None:
        row['valid_fraction'] = _round(np.asarray(valid, bool)[y0:y1, x0:x1].mean()) if (x1 > x0 and y1 > y0) else 0.
    if footprint is not None:
        row['footprint_fraction'] = _round(np.asarray(footprint, bool)[y0:y1, x0:x1].mean()) if (x1 > x0 and y1 > y0) else 0.
    if min(extent) < LOCAL_MIN_SIDE_PX or x1 - x0 < LOCAL_MIN_SIDE_PX or y1 - y0 < LOCAL_MIN_SIDE_PX:
        row['error'] = 'Roof too small for local refinement'
        return row
    if row['footprint_fraction'] is not None and row['footprint_fraction'] < .9:
        row['error'] = 'Aligned capture does not cover the roof'
        return row
    template = np.ascontiguousarray(saved_gray[y0:y1, x0:x1], np.float32)
    moving = np.ascontiguousarray(aligned_gray[y0:y1, x0:x1], np.float32)
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        corr, warp = cv2.findTransformECC(template, moving, warp, cv2.MOTION_TRANSLATION, _ECC_CRITERIA, None, 5)
    except cv2.error as error:
        row['error'] = f'ECC did not converge: {str(error).strip().splitlines()[-1][:160]}'
        return row
    shift = [float(warp[0, 2]), float(warp[1, 2])]
    norm = float(np.hypot(*shift))
    if not math.isfinite(corr) or not math.isfinite(norm):
        row['error'] = 'ECC returned a non-finite result'
        return row
    row.update(local_shift_px=[_round(shift[0]), _round(shift[1])], local_shift_norm_px=_round(norm),
               local_corr=_round(corr), within_budget=bool(norm <= LOCAL_SHIFT_PX))
    return row


def normalise_gray(reference_gray, gray, valid):
    """Median-gray brightness offset over valid pixels (the stable_composite rule)."""
    reference = np.asarray(reference_gray, float)
    source = np.asarray(gray, float)
    offset = float(np.median(reference[valid] - source[valid])) if np.any(valid) else 0.
    return np.clip(source + offset, 0, 255), offset


def stack_statistics(saved_gray, aligned_grays, valids):
    """Per-pixel median/min/max/ptp/count over the saved view and every valid aligned pixel."""
    saved = np.asarray(saved_gray, float)
    if saved.ndim != 2:
        raise ValueError('saved_gray must be an HxW array')
    layers, offsets = [saved], []
    for gray, valid in zip(aligned_grays, valids, strict=True):
        valid = np.asarray(valid, bool)
        if np.shape(gray) != saved.shape or valid.shape != saved.shape:
            raise ValueError('Every aligned gray and valid mask must match the saved image grid')
        normalised, offset = normalise_gray(saved, gray, valid)
        layer = np.where(valid, normalised, np.nan)
        layers.append(layer)
        offsets.append(offset)
    stack = np.stack(layers)
    count = np.sum(~np.isnan(stack), axis=0)
    with np.errstate(all='ignore'):
        median = np.nanmedian(stack, axis=0)
        low = np.nanmin(stack, axis=0)
        high = np.nanmax(stack, axis=0)
    to_uint8 = lambda a: np.clip(np.rint(a), 0, 255).astype(np.uint8)  # noqa: E731
    return {'median': to_uint8(median), 'min': to_uint8(low), 'max': to_uint8(high), 'ptp': to_uint8(high - low),
            'count': count.astype(np.uint8), 'offsets': offsets}


def edge_map(image):
    """Canny 50/150 on the bilateral+CLAHE gray (roof_line_fit.enhance_roof_detail), dilated by 1 px."""
    edges = cv2.Canny(enhance_roof_detail(image), *CANNY)
    kernel = np.ones((2*EDGE_DILATE_PX + 1,)*2, np.uint8)
    return (cv2.dilate(edges, kernel) > 0).astype(np.uint8)


def edge_mask(valid, footprint, border=EDGE_BORDER_PX):
    """Validity for edge voting: the registration mask minus `border` px along the warp footprint.

    An aligned capture is black outside its warp footprint; Canny fires along that boundary
    although no imagery edge exists there, so those pixels never vote.
    """
    valid = np.asarray(valid, bool)
    inner = cv2.erode(np.asarray(footprint, bool).astype(np.uint8), np.ones((2*border + 1,)*2, np.uint8))
    return valid & inner.astype(bool)


def edge_consensus(images, valids):
    """Per pixel: how many valid views have an edge within 1 px."""
    if not images:
        raise ValueError('edge_consensus needs at least one view')
    total = None
    for image, valid in zip(images, valids, strict=True):
        valid = np.asarray(valid, bool)
        edges = edge_map(image)
        if valid.shape != edges.shape:
            raise ValueError('Every valid mask must match its view (HxW)')
        edges &= valid.astype(np.uint8)
        total = edges.astype(np.uint16) if total is None else total + edges
    return np.clip(total, 0, 255).astype(np.uint8)


@contextlib.contextmanager
def _network(enabled):
    """fetch=False: cache hits still work; any HTTP request is refused as a ConnectionError."""
    if enabled:
        yield
        return

    def refuse(*args, **kwargs):
        raise requests.ConnectionError('Network fetch disabled (fetch=False) and the cache had no entry')
    with mock.patch.object(imagery_dates.requests, 'get', refuse):
        yield


def _pixel_sha(image):
    return hashlib.sha256(np.asarray(image.convert('RGB')).tobytes()).hexdigest()


def _capture_row(record, index):
    return {'index': index, 'acquisition_id': record.get('acquisition_id'), 'date': record.get('acquisition_date'),
            'sensor': record.get('provider'), 'source': record.get('source'), 'resolution_m': record.get('resolution_m'),
            'accuracy_m': record.get('accuracy_m'), 'release': record.get('release'),
            'release_title': record.get('release_title'), 'imagery_zoom': record.get('imagery_zoom'),
            'attribution': record.get('attribution'), 'pixel_sha256': None, 'combined_fraction': None,
            'status': None, 'registration': {'accepted': False, 'status': None, 'reason': None}}


def _owned_buildings(scene):
    owned = scene.get('owned_object_ids')
    owned = None if owned is None else set(owned)
    return [o for o in scene.get('objects', []) if o.get('kind') == 'building' and (owned is None or o.get('id') in owned)]


def _write_png(folder, name, array):
    Image.fromarray(np.ascontiguousarray(array, np.uint8)).save(folder/name, format='PNG')
    return {'path': name, 'sha256': hashlib.sha256((folder/name).read_bytes()).hexdigest()}


def build(scene_id, root=Path('.'), *, max_dates=3, fetch=True):
    """Register up to `max_dates` Wayback captures onto the saved scene; write the stack when >= 3 views."""
    started = time.perf_counter()
    root = Path(root)
    scene, saved = load_scene(scene_id, root)
    saved_gray = _gray(saved)
    height, width = saved_gray.shape
    cache = root/CACHE
    report = {'version': VERSION, 'code_sha256': CODE_SHA256, 'computed_at': None, 'stack_id': None, 'status': None,
              'reason': None, 'scene_id': scene_id, 'source_image_sha256': scene['image_sha256'],
              'image_width': width, 'image_height': height,
              'reference': {'provider': scene.get('region', {}).get('provider'),
                            'acquisition_date': scene.get('region', {}).get('acquisition_date'),
                            'role': 'registration reference and first stack view'},
              'gates': dict(GATES), 'discovery': None, 'captures': [], 'n_accepted': 0, 'n_views': 1,
              'per_roof_local': [], 'stack': None, 'assets': {}, 'note': NOTE}
    accepted = []
    with _network(fetch):
        try:
            records, skipped = discover_acquisitions(scene, cache, count=max_dates)
            report['discovery'] = {'status': 'ok', 'requested': max_dates, 'found': len(records), 'skipped': skipped}
        except FETCH_ERRORS as error:
            records = []
            report['discovery'] = {'status': 'unavailable', 'requested': max_dates, 'found': 0, 'skipped': [],
                                   'error': f'{type(error).__name__}: {str(error)[:300]}'}
            report['status'], report['reason'] = 'unavailable', 'Imagery discovery unavailable'
        seen = set()
        for index, record in enumerate(records):
            row = _capture_row(record, index)
            report['captures'].append(row)
            try:
                image = fetch_capture(scene, record, cache/'tiles')
            except FETCH_ERRORS as error:
                row['status'] = 'failed'
                row['registration'].update(status='failed', reason=f'{type(error).__name__}: {str(error)[:300]}')
                continue
            if image.size != (width, height):
                row['status'] = 'rejected'
                row['registration'].update(status='rejected', reason='Capture is not on the saved scene grid')
                continue
            row['pixel_sha256'] = _pixel_sha(image)
            if row['pixel_sha256'] in seen:
                row['status'] = 'duplicate'
                row['registration'].update(status='duplicate', reason='Repeated source pixels across releases')
                continue
            seen.add(row['pixel_sha256'])
            aligned, valid, row['registration'] = register_capture(saved, image)
            # `status` is the final decision (existing gates AND the residual budget);
            # registration.status keeps register()'s own verdict for diagnosis.
            row['status'] = 'accepted' if aligned is not None else \
                ('duplicate' if row['registration']['status'] == 'duplicate' else 'rejected')
            if aligned is None:
                continue
            row['combined_fraction'] = _round(valid.mean())
            accepted.append({'row': row, 'image': aligned, 'gray': _gray(aligned), 'valid': valid})
    ids = [c['row']['acquisition_id'] for c in accepted]
    report['n_accepted'], report['n_views'] = len(accepted), len(accepted) + 1
    report['stack_id'] = stack_id(scene['image_sha256'], ids)
    # Per-roof ECC refinement for every accepted capture; recorded only.
    for c in accepted:
        transform = np.asarray(c['row']['registration']['transform'], np.float32)
        footprint = cv2.warpAffine(np.ones((height, width), np.uint8), transform, (width, height),
                                   flags=cv2.INTER_NEAREST).astype(bool)
        c['edge_valid'] = edge_mask(c['valid'], footprint)
        for obj in _owned_buildings(scene):
            try:
                local = local_refinement(saved_gray, c['gray'], obj['ring'], valid=c['valid'], footprint=footprint)
            except ValueError as error:
                local = {'bbox_px': None, 'local_shift_px': None, 'local_shift_norm_px': None, 'local_corr': None,
                         'within_budget': None, 'valid_fraction': None, 'footprint_fraction': None, 'error': str(error)}
            report['per_roof_local'].append({'object_id': obj.get('id'), 'capture': c['row']['acquisition_id'], **local})
    folder = stack_path(root, report['stack_id'])
    folder.mkdir(parents=True, exist_ok=True)
    if report['status'] is None and len(accepted) < MIN_ACCEPTED:
        report['status'] = 'not_available'
        report['reason'] = (f'{len(accepted)} accepted capture(s); the stack needs at least {MIN_ACCEPTED} '
                            f'in addition to the saved reference')
    if report['status'] is None:
        stats = stack_statistics(saved_gray, [c['gray'] for c in accepted], [c['valid'] for c in accepted])
        consensus = edge_consensus([saved, *(c['image'] for c in accepted)],
                                   [np.ones((height, width), bool), *(c['edge_valid'] for c in accepted)])
        assets = {'stack-median.png': stats['median'], 'stack-min.png': stats['min'], 'stack-max.png': stats['max'],
                  'disagreement.png': stats['ptp'], 'edge-consensus.png': consensus, 'valid-count.png': stats['count']}
        for name, array in assets.items():
            report['assets'][name] = _write_png(folder, name, array)
        for c in accepted:
            i = c['row']['index']
            report['assets'][f'aligned-{i}.png'] = _write_png(folder, f'aligned-{i}.png', np.asarray(c['image']))
            report['assets'][f'valid-{i}.png'] = _write_png(folder, f'valid-{i}.png', c['valid'].astype(np.uint8)*255)
        count = stats['count']
        report['stack'] = {'views': len(accepted) + 1, 'gray_only': True,
                           'brightness_offsets': [_round(o) for o in stats['offsets']],
                           'combined_fraction': _round((count >= 2).mean()),
                           'full_fraction': _round((count == len(accepted) + 1).mean()),
                           'disagreement_median': _round(np.median(stats['ptp'][count >= 2])) if (count >= 2).any() else None,
                           'disagreement_p90': _round(np.percentile(stats['ptp'][count >= 2], 90)) if (count >= 2).any() else None,
                           'edge_consensus_fraction_full': _round((consensus == len(accepted) + 1).mean()),
                           'edge_consensus_fraction_any': _round((consensus >= 1).mean()),
                           'canny': list(CANNY), 'edge_dilate_px': EDGE_DILATE_PX, 'edge_border_px': EDGE_BORDER_PX,
                           'disagreement_defined_where': 'valid-count.png >= 2 (0 elsewhere means one view, not agreement)',
                           'preprocessing': 'roof_line_fit.enhance_roof_detail (bilateral + CLAHE)'}
        report['status'] = 'available'
        report['reason'] = f'{len(accepted)} accepted captures registered onto the saved scene grid'
    report['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    report['computed_at'] = _now()
    atomic_json(folder/'report.json', report)
    return report


def views(report_or_stack_id, root=Path('.')):
    """Extra LSD/edge views [(name, HxW uint8)] when the stack is available, else []."""
    identifier = report_or_stack_id.get('stack_id') if isinstance(report_or_stack_id, dict) else report_or_stack_id
    if not isinstance(identifier, str):
        raise ValueError('Invalid stack id')
    folder = stack_path(root, identifier)
    report = json.loads((folder/'report.json').read_text())
    if report.get('status') != 'available':
        return []
    result = []
    for name in VIEW_NAMES:
        asset = report['assets'][f'{name}.png']
        if asset['path'] != Path(asset['path']).name:
            raise ValueError(f'Stack asset {name} must live in the stack folder')
        data = (folder/asset['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != asset['sha256']:
            raise ValueError(f'Stack asset {name} does not match its recorded sha256')
        array = np.asarray(Image.open(folder/asset['path']))
        if name == 'edge-consensus':
            array = np.rint(array.astype(float)*255/report['stack']['views']).astype(np.uint8)
        result.append((name, np.ascontiguousarray(array, np.uint8)))
    return result


def compact(report):
    """Report without transforms and per-roof rows: what the CLI prints."""
    captures = [{**c, 'registration': {k: v for k, v in c['registration'].items() if k != 'transform'}}
                for c in report['captures']]
    rows = report['per_roof_local']
    corr = [r['local_corr'] for r in rows if r['local_corr'] is not None]
    norms = [r['local_shift_norm_px'] for r in rows if r['local_shift_norm_px'] is not None]
    summary = {'rows': len(rows), 'converged': len(corr), 'within_budget': sum(bool(r['within_budget']) for r in rows),
               'median_corr': _round(np.median(corr)) if corr else None,
               'median_shift_px': _round(np.median(norms)) if norms else None,
               'max_shift_px': _round(max(norms)) if norms else None}
    return {**{k: v for k, v in report.items() if k != 'per_roof_local'}, 'captures': captures,
            'per_roof_local_summary': summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('scene_id')
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--max-dates', type=int, default=3)
    parser.add_argument('--no-fetch', action='store_true', help='cache only; refuse network requests')
    args = parser.parse_args(argv)
    report = build(args.scene_id, args.root, max_dates=args.max_dates, fetch=not args.no_fetch)
    print(json.dumps(compact(report), indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
