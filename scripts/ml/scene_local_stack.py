"""Wayback dates registered onto the saved scene grid PER WINDOW; the scene_stack contract plus an RGB display median.

Whole-tile similarity registration (scene_stack via temporal_evidence.register) fails for the
cross-provider orthos of tile mooroolbark-50011498-17-118442-80417: orthorectification, building lean
and mosaic seams differ locally, so one similarity transform is the wrong model (21 and 9 fit inliers
against the 25 gate). This module estimates one translation per window instead: phase correlation of
Sobel gradient magnitudes after CLAHE (provider colour and gamma do not matter), rejection of windows
that disagree with their accepted neighbours, piecewise-linear interpolation of the accepted shifts into
a dense field, and cv2.remap of each capture onto the saved grid. The whole-tile outcome is still
computed and recorded per date as `registration_global` so the report shows why local was needed.

The output follows the scene_stack report/asset contract (captures[].registration, aligned-<i>.png,
valid-<i>.png, assets{name:{path,sha256}}, stack{...}) so multidate_agreement.load_stack,
scene_stack.views and eyes_experiments consume it unchanged, plus enhanced-median-rgb.png: a per-channel
median over the saved RGB and the aligned dates for reviewers. That composite is a display view only:
not an evidence image, no resolution increase, no inpainting. The saved PNG stays the sole evidence
gate; lines from any stack view must still pass line_evidence on the original RGB. Nothing is written
under data/scenes and no SAM3 inference runs. Assets and report.json live under
data/roof-review-loop/multidate/stacks/<stack_id>/ (stack_id is content-addressed to THIS module, so a
scene_stack report for the same image is never overwritten).

Pair mode (opt-in, `build(min_accepted=1)` / `--allow-pair`): when exactly one date is accepted the report is
status 'pair' (never 'available'): the saved PNG plus one aligned date. It writes aligned/valid/shift-field for
that date, edge-consensus.png counted over the two views (0..2, same eroded mask and 3 px border exclusion),
valid-count.png and enhanced-pair-rgb.png (a per-channel MEAN of the saved RGB and the brightness-normalised
date where valid; saved RGB elsewhere; display only). No stack median/min/max/disagreement: those need >= 3
views. `views()` dispatches on report status ('available' -> scene_stack.views, 'pair' -> pair_views) so
eyes_experiments and multidate_agreement read either report through one helper. With the option absent
every function behaves exactly as before.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
from scipy.spatial import QhullError

from . import scene_stack
from .imagery_dates import capture as fetch_capture
from .imagery_dates import discover as discover_acquisitions
from .scene_batch import signature
from .scene_stack import (
    CACHE,
    FETCH_ERRORS,
    MIN_ACCEPTED,
    _network,
    edge_consensus,
    edge_mask,
    load_scene,
    local_refinement,
    register_capture,
    stack_path,
    stack_statistics,
)
from .scene_training import atomic_json

VERSION = 'scene-local-stack-v1'
METHOD = 'local-phase-correlation-v1'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MIN_WINDOWS = 6
HOLDOUT_RESIDUAL_PX = scene_stack.MEDIAN_RESIDUAL_PX   # 2 px: the CP3 budget multidate_agreement also applies
COVERAGE_RADII = 1.5           # a pixel is interpolated only within this many window radii of an accepted window
VALID_ERODE_PX = 3             # warp-footprint border: black outside the capture is not imagery
OUTLIER_PASSES = 2             # reject neighbour outliers, then re-check against the reduced neighbourhood
COMPOSITE_MIN_VIEWS = 3
DEFAULTS = {'window_px': 192, 'stride_px': 96, 'min_response': .25, 'max_shift_px': 14., 'neighbour_tolerance_px': 2.5,
            'min_coverage': .35}
STACK_ASSETS = (*scene_stack.STACK_ASSETS, 'enhanced-median-rgb.png')
PAIR = 'pair'                  # report status: the saved PNG plus exactly one accepted date (never 'available')
PAIR_VIEWS = 2
PAIR_ASSETS = ('edge-consensus.png', 'valid-count.png', 'enhanced-pair-rgb.png')
DISPLAY_NOTE = 'display/review view; not an evidence image; no resolution increase; no inpainting'
PAIR_COMPOSITE_NOTE = ('two-view pair composite for display/review; not an evidence image; no median (needs 3 views); '
                       'no resolution increase; no inpainting')
PAIR_STACK_NOTE = ('two views only (saved PNG + 1 date): no stack median/min/max/disagreement, which need >= 3 views; '
                   'edge-consensus counts 0..2')
PAIR_REPORT_NOTE = ('Two-view pair (saved PNG + 1 accepted date), emitted only with min_accepted=1 and never reported '
                    'as an available stack. enhanced-pair-rgb.png is a display/review mean of two views, not an evidence '
                    'image; there is no median and no disagreement raster with two views.')
NOTE = ('Diagnostic views registered onto the saved scene grid window by window (local translations, not one '
        'similarity transform). The saved PNG remains the sole evidence gate: lines from stack views must still '
        'pass line_evidence on the original RGB. enhanced-median-rgb.png is a display/review composite, not an '
        'evidence image; nothing is super-resolution, sharpening or inpainting; no production file is written; '
        'no SAM3 inference.')
PREPROCESSING = 'gray -> 3x3 Gaussian (sigma .8) -> CLAHE (clip 2, 8x8 tiles) -> Sobel gradient magnitude (float32)'
CODES = {'ok': 'accepted', 'low_response': 'response below min_response', 'large_shift': 'shift above max_shift_px',
         'no_peak': 'no finite correlation peak', 'flat': 'flat window (no gradient)',
         'neighbour_outlier': 'shift differs from the accepted 8-neighbourhood median', 'isolated': 'no accepted neighbour'}


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def _round(value, digits=4):
    if value is None:
        return None
    value = float(value)
    return None if not math.isfinite(value) else round(value, digits)


def _rgb(image):
    """HxWx3 uint8 from a PIL image or array."""
    if isinstance(image, Image.Image):
        image = np.asarray(image.convert('RGB'))
    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError('Images must be HxWx3 RGB arrays')
    return np.ascontiguousarray(array, np.uint8)


def matching_signal(image):
    """What the windows are matched on: Sobel gradient magnitude of the CLAHE gray (float32 HxW).

    Provider colour, gamma and global brightness drop out; edges of roofs, kerbs and fences remain.
    """
    gray = cv2.cvtColor(_rgb(image), cv2.COLOR_RGB2GRAY)
    local = cv2.createCLAHE(clipLimit=2., tileGridSize=(8, 8)).apply(cv2.GaussianBlur(gray, (3, 3), .8))
    gx = cv2.Sobel(local, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(local, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def _is_int(value):
    """Python or numpy integer (not bool)."""
    return isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))


def _finite_shift(row):
    """True when the row is accepted and carries a finite (dx, dy); None/NaN shifts never enter a field."""
    if not row.get('accepted'):
        return False
    try:
        return all(math.isfinite(float(row[key])) for key in ('dx', 'dy'))
    except (KeyError, TypeError, ValueError):
        return False


def window_grid(shape, window_px, stride_px):
    """[(row, col, x0, y0)] of a regular window grid that stays inside the image (plain ints)."""
    height, width = shape[:2]
    if not _is_int(window_px) or window_px < 16 or window_px > min(width, height):
        raise ValueError('window_px must be an integer between 16 px and the shorter image side')
    if not _is_int(stride_px) or stride_px < 1:
        raise ValueError('stride_px must be a positive integer')
    window_px, stride_px = int(window_px), int(stride_px)
    xs = range(0, width - window_px + 1, stride_px)
    ys = range(0, height - window_px + 1, stride_px)
    return [(r, c, x0, y0) for r, y0 in enumerate(ys) for c, x0 in enumerate(xs)]


def _neighbours(cells, key):
    r, c = key
    return [cells[(r + i, c + j)] for i in (-1, 0, 1) for j in (-1, 0, 1) if (i or j) and (r + i, c + j) in cells]


def reject_outliers(rows, tolerance_px, passes=OUTLIER_PASSES):
    """Reject accepted windows whose shift is farther than `tolerance_px` from the median shift of their accepted
    8-neighbours, or that have no accepted neighbour at all; repeated so the reduced neighbourhood is re-checked.
    An accepted row without a finite (dx, dy) is demoted first (`no_peak`) so it can neither vote nor be interpolated."""
    for row in rows:
        if row.get('accepted') and not _finite_shift(row):
            row.update(accepted=False, code='no_peak', reason='no finite correlation peak', dx=None, dy=None, shift_px=None)
    cells = {(row['row'], row['col']): row for row in rows}
    for _ in range(passes):
        rejections = {}
        for key, row in cells.items():
            if not row['accepted']:
                continue
            accepted = [n for n in _neighbours(cells, key) if n['accepted']]
            if not accepted:
                rejections[key] = ('isolated', 'no accepted neighbour corroborates the shift')
                continue
            median = np.median([[n['dx'], n['dy']] for n in accepted], axis=0)
            deviation = float(np.hypot(row['dx'] - median[0], row['dy'] - median[1]))
            row['neighbour_deviation_px'] = _round(deviation)
            if deviation > tolerance_px:
                rejections[key] = ('neighbour_outlier',
                                   f'shift differs from accepted neighbours by {deviation:.1f} px (> {tolerance_px:g} px)')
        for key, (code, reason) in rejections.items():
            cells[key].update(accepted=False, code=code, reason=reason)
        if not rejections:
            break
    return rows


def window_shifts(saved_signal, capture_signal, *, window_px, stride_px, min_response, max_shift_px, neighbour_tolerance_px):
    """Per-window translation of the capture relative to the saved image by phase correlation.

    Convention (cv2.phaseCorrelate(saved, capture)): the saved pixel (x, y) appears in the capture at
    (x + dx, y + dy), so aligned = remap(capture, X + dx, Y + dy). Two passes: response/shift budget,
    then neighbour agreement (`reject_outliers`).
    """
    saved = np.asarray(saved_signal, np.float32)
    capture = np.asarray(capture_signal, np.float32)
    if saved.ndim != 2 or saved.shape != capture.shape:
        raise ValueError('saved_signal and capture_signal must be HxW arrays on the same grid')
    grid = window_grid(saved.shape, window_px, stride_px)   # validates the knobs; plain ints from here on
    window_px = int(window_px)
    hann = cv2.createHanningWindow((window_px, window_px), cv2.CV_32F)
    rows = []
    for r, c, x0, y0 in grid:
        a = np.ascontiguousarray(saved[y0:y0 + window_px, x0:x0 + window_px])
        b = np.ascontiguousarray(capture[y0:y0 + window_px, x0:x0 + window_px])
        row = {'row': r, 'col': c, 'centre_px': [x0 + window_px/2, y0 + window_px/2], 'dx': None, 'dy': None,
               'shift_px': None, 'response': None, 'accepted': False, 'code': None, 'reason': None,
               'neighbour_deviation_px': None}
        if a.std() < 1e-6 or b.std() < 1e-6:
            row.update(code='flat', reason='flat window: no gradient to match', response=0.)
        else:
            (dx, dy), response = cv2.phaseCorrelate(a, b, hann)
            norm = float(np.hypot(dx, dy))
            row.update(dx=_round(dx), dy=_round(dy), shift_px=_round(norm), response=_round(response))
            if not all(math.isfinite(v) for v in (dx, dy, response)):
                row.update(code='no_peak', reason='no finite correlation peak', dx=None, dy=None, shift_px=None)
            elif response < min_response:
                row.update(code='low_response', reason=f'response {response:.3f} below {min_response:g}')
            elif norm > max_shift_px:
                row.update(code='large_shift', reason=f'shift {norm:.1f} px exceeds {max_shift_px:g} px')
            else:
                row.update(accepted=True, code='ok', reason='response and shift within budget; agrees with neighbours')
        rows.append(row)
    return reject_outliers(rows, neighbour_tolerance_px)


def _interpolator(points, values):
    """Piecewise-linear (Delaunay) interpolation inside the hull, nearest accepted window outside it."""
    nearest = NearestNDInterpolator(points, values)
    if len(points) < 3:
        return nearest
    try:
        linear = LinearNDInterpolator(points, values)
    except (QhullError, ValueError):
        return nearest

    def call(xy):
        out = np.asarray(linear(xy), float)
        missing = np.isnan(out[:, 0])
        if missing.any():
            out[missing] = nearest(xy[missing])
        return out
    return call


def _accepted_points(rows):
    """Centres and shifts of the accepted windows that carry a finite (dx, dy)."""
    accepted = [row for row in rows if _finite_shift(row)]
    points = np.array([row['centre_px'] for row in accepted], float).reshape(-1, 2)
    values = np.array([[row['dx'], row['dy']] for row in accepted], float).reshape(-1, 2)
    return points, values


def dense_field(rows, shape, window_px, radii=COVERAGE_RADII):
    """(dx, dy, coverage): the accepted window shifts interpolated over the image; NaN outside coverage.

    Coverage: pixels within `radii` window radii (radii * window_px / 2) of at least one accepted window centre.
    """
    height, width = shape[:2]
    dx = np.full((height, width), np.nan, np.float32)
    dy = np.full((height, width), np.nan, np.float32)
    points, values = _accepted_points(rows)
    if not len(points):
        return dx, dy, np.zeros((height, width), bool)
    marks = np.ones((height, width), np.uint8)
    for x, y in points:
        marks[min(height - 1, max(0, int(round(y)))), min(width - 1, max(0, int(round(x))))] = 0
    coverage = cv2.distanceTransform(marks, cv2.DIST_L2, 5) <= radii*window_px/2
    ys, xs = np.nonzero(coverage)
    field = _interpolator(points, values)(np.c_[xs, ys].astype(float))
    dx[ys, xs] = field[:, 0]
    dy[ys, xs] = field[:, 1]
    return dx, dy, coverage


def holdout_residual(rows):
    """(median, residuals): leave-one-out |shift - shift interpolated from the other accepted windows|, in px."""
    points, values = _accepted_points(rows)
    if len(points) < 2:
        return None, []
    residuals = []
    for i in range(len(points)):
        keep = np.arange(len(points)) != i
        predicted = _interpolator(points[keep], values[keep])(points[i:i + 1])[0]
        residuals.append(_round(np.hypot(*(values[i] - predicted))))
    return _round(np.median(residuals)), residuals


def warp_capture(capture, dx, dy, coverage, erode_px=VALID_ERODE_PX):
    """(aligned HxWx3, footprint, valid): the capture resampled onto the saved grid through the dense field.

    footprint: pixels whose source coordinate lies inside the capture; valid: coverage AND footprint eroded
    by `erode_px` so the black warp border never votes.
    """
    capture = _rgb(capture)
    height, width = coverage.shape
    if capture.shape[:2] != (height, width) or dx.shape != (height, width) or dy.shape != (height, width):
        raise ValueError('capture, shift fields and coverage must share one grid')
    xs, ys = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))
    # A non-finite shift is not coverage: nothing is sampled through it and it never becomes valid.
    coverage = np.asarray(coverage, bool) & np.isfinite(dx) & np.isfinite(dy)
    with np.errstate(invalid='ignore'):
        map_x = np.where(coverage, xs + dx, -1.).astype(np.float32)   # -1: outside the capture, samples nothing
        map_y = np.where(coverage, ys + dy, -1.).astype(np.float32)
    aligned = cv2.remap(capture, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    footprint = cv2.remap(np.ones((height, width), np.uint8), map_x, map_y, cv2.INTER_NEAREST,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0).astype(bool)
    kernel = np.ones((2*erode_px + 1,)*2, np.uint8)
    valid = cv2.erode((coverage & footprint).astype(np.uint8), kernel).astype(bool)
    return aligned, footprint, valid


def _percentile(values, q):
    return _round(np.percentile(values, q)) if len(values) else None


def register_local(saved, capture, *, window_px=DEFAULTS['window_px'], stride_px=DEFAULTS['stride_px'],
                   min_response=DEFAULTS['min_response'], max_shift_px=DEFAULTS['max_shift_px'],
                   neighbour_tolerance_px=DEFAULTS['neighbour_tolerance_px'], min_coverage=DEFAULTS['min_coverage']):
    """Per-window registration of one capture onto the saved image.

    Returns {'aligned', 'valid', 'footprint', 'dx', 'dy', 'registration', 'windows'}; `registration` is the
    per-date block written to the report (accepted when coverage >= min_coverage, >= MIN_WINDOWS accepted
    windows and the leave-one-out residual <= HOLDOUT_RESIDUAL_PX).
    """
    saved, capture = _rgb(saved), _rgb(capture)
    if saved.shape != capture.shape:
        raise ValueError('The capture must be on the saved scene grid')
    window_grid(saved.shape[:2], window_px, stride_px)   # validates the knobs before any work
    window_px, stride_px = int(window_px), int(stride_px)
    rows = window_shifts(matching_signal(saved), matching_signal(capture), window_px=window_px, stride_px=stride_px,
                         min_response=min_response, max_shift_px=max_shift_px, neighbour_tolerance_px=neighbour_tolerance_px)
    dx, dy, coverage = dense_field(rows, saved.shape[:2], window_px)
    aligned, footprint, valid = warp_capture(capture, dx, dy, coverage)
    accepted = [row for row in rows if row['accepted']]
    holdout, _ = holdout_residual(rows)
    coverage_fraction = float(valid.mean())
    norms = np.array([row['shift_px'] for row in accepted], float)
    responses = np.array([row['response'] for row in rows if row['response'] is not None], float)
    rejections = {}
    for row in rows:
        if not row['accepted']:
            rejections[row['code']] = rejections.get(row['code'], 0) + 1
    reasons = []
    if len(accepted) < MIN_WINDOWS:
        reasons.append(f'{len(accepted)} accepted window(s) of {len(rows)}; at least {MIN_WINDOWS} required')
    if coverage_fraction < min_coverage:
        reasons.append(f'valid coverage {coverage_fraction:.2f} below {min_coverage:g}')
    if holdout is None:
        reasons.append('leave-one-out residual unavailable')
    elif holdout > HOLDOUT_RESIDUAL_PX:
        reasons.append(f'leave-one-out residual {holdout:g} px exceeds {HOLDOUT_RESIDUAL_PX:g} px')
    ok = not reasons
    registration = {
        'accepted': ok, 'method': METHOD, 'status': 'accepted' if ok else 'rejected',
        'reason': (f'{len(accepted)}/{len(rows)} windows agree; valid coverage {coverage_fraction:.2f}; '
                   f'leave-one-out residual {holdout:g} px') if ok else '; '.join(reasons),
        'windows_total': len(rows), 'windows_accepted': len(accepted), 'coverage': _round(coverage_fraction),
        'interpolated_fraction': _round(coverage.mean()), 'footprint_fraction': _round(footprint.mean()),
        'median_shift_px': _percentile(norms, 50), 'p95_shift_px': _percentile(norms, 95),
        'max_shift_px': _round(norms.max()) if len(norms) else None,
        'median_dx_px': _percentile([row['dx'] for row in accepted], 50),
        'median_dy_px': _percentile([row['dy'] for row in accepted], 50),
        'median_response': _percentile(responses, 50),
        'median_response_accepted': _percentile([row['response'] for row in accepted], 50),
        'holdout_residual_px': holdout, 'rejections': rejections,
        # scene_stack's key names for the same facts, so eyes_experiments.registration_summary and
        # multidate_agreement.date_records read the hold-out residual and the shift of local reports too.
        'median_residual_px': holdout, 'shift_px': _percentile(norms, 50),
        'window_px': window_px, 'stride_px': stride_px, 'preprocessing': PREPROCESSING}
    return {'aligned': aligned, 'valid': valid, 'footprint': footprint, 'dx': dx, 'dy': dy,
            'registration': registration, 'windows': rows}


def enhanced_median_rgb(saved, aligned_images, valids, min_views=COMPOSITE_MIN_VIEWS):
    """Per-pixel, per-channel median over the saved RGB and the aligned dates where >= `min_views` views are valid;
    the saved pixels elsewhere. Each date is first offset per channel by its median difference to the saved image
    over its valid pixels (the stable_composite rule), so provider brightness does not dominate the median.

    A display/review composite only: not an evidence image, no resolution increase, no inpainting.
    """
    base = _rgb(saved).astype(np.float32)
    layers, offsets = [base], []
    for image, valid in zip(aligned_images, valids, strict=True):
        rgb = _rgb(image).astype(np.float32)
        valid = np.asarray(valid, bool)
        if rgb.shape != base.shape or valid.shape != base.shape[:2]:
            raise ValueError('Every aligned image and valid mask must match the saved image grid')
        offset = np.median(base[valid] - rgb[valid], axis=0) if valid.any() else np.zeros(3)
        layers.append(np.where(valid[..., None], np.clip(rgb + offset.astype(np.float32), 0, 255), np.nan))
        offsets.append([_round(o) for o in offset])
    stack = np.stack(layers)
    count = np.sum(~np.isnan(stack[..., 0]), axis=0)
    with np.errstate(all='ignore'):
        median = np.nanmedian(stack, axis=0)
    composited = count >= min_views
    out = np.where(composited[..., None], median, base)
    info = {'offsets': offsets, 'composited_fraction': _round(composited.mean()), 'min_views': min_views,
            'rule': 'per-channel median over saved + aligned dates where valid views >= min_views, saved RGB elsewhere',
            'note': DISPLAY_NOTE}
    return np.clip(np.rint(out), 0, 255).astype(np.uint8), info


def enhanced_pair_rgb(saved, aligned, valid):
    """Two-view display composite: per-pixel, per-channel MEAN of the saved RGB and the aligned date where the date
    is valid, the saved pixels elsewhere. The date is first offset by the median gray difference to the saved image
    over its valid pixels (scene_stack.normalise_gray, the stable_composite rule) so provider brightness cancels.

    A mean cannot reject an outlier the way a median does (that needs 3 views): this is a review view only, not an
    evidence image, no resolution increase, no inpainting.
    """
    base = _rgb(saved)
    rgb = _rgb(aligned)
    valid = np.asarray(valid, bool)
    if rgb.shape != base.shape or valid.shape != base.shape[:2]:
        raise ValueError('The aligned image and valid mask must match the saved image grid')
    _, offset = scene_stack.normalise_gray(cv2.cvtColor(base, cv2.COLOR_RGB2GRAY), cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), valid)
    normalised = np.clip(rgb.astype(np.float32) + np.float32(offset), 0, 255)
    mean = (base.astype(np.float32) + normalised)/2
    out = np.where(valid[..., None], mean, base.astype(np.float32))
    info = {'views': PAIR_VIEWS, 'offset_gray': _round(offset), 'composited_fraction': _round(valid.mean()),
            'rule': 'per-channel mean of the saved RGB and the aligned RGB (offset by its median gray difference to the '
                    'saved image over valid pixels) where the date is valid; saved RGB elsewhere',
            'note': PAIR_COMPOSITE_NOTE}
    return np.clip(np.rint(out), 0, 255).astype(np.uint8), info


def shift_field_image(dx, dy, valid, max_shift_px):
    """HSV visualisation of the dense field (hue = direction, value = magnitude / max_shift_px); black where invalid."""
    with np.errstate(invalid='ignore'):
        magnitude = np.hypot(dx, dy)
        angle = np.degrees(np.arctan2(dy, dx)) % 360.
    hsv = np.zeros((*valid.shape, 3), np.uint8)
    hsv[..., 0] = np.nan_to_num(angle/2.).astype(np.uint8)
    hsv[..., 1] = 255
    hsv[..., 2] = np.clip(np.nan_to_num(magnitude)/max(float(max_shift_px), 1e-6)*255., 0, 255).astype(np.uint8)
    rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    rgb[~np.asarray(valid, bool)] = 0
    return rgb


def stack_id(image_sha256, accepted_ids):
    """Content address: saved image identity + sorted accepted acquisitions + THIS module.

    Deliberately not scene_stack.stack_id: that hashes scene_stack's code, so a not_available local report for the
    same image and the same (empty) accepted set would overwrite the scene_stack report folder.
    """
    return signature([image_sha256, sorted(accepted_ids), CODE_SHA256])[:16]


def _pixel_sha(image):
    return hashlib.sha256(_rgb(image).tobytes()).hexdigest()


def _capture_row(record, index):
    return {'index': index, 'acquisition_id': record.get('acquisition_id'), 'date': record.get('acquisition_date'),
            'sensor': record.get('provider'), 'source': record.get('source'), 'resolution_m': record.get('resolution_m'),
            'accuracy_m': record.get('accuracy_m'), 'release': record.get('release'),
            'release_title': record.get('release_title'), 'imagery_zoom': record.get('imagery_zoom'),
            'attribution': record.get('attribution'), 'pixel_sha256': None, 'combined_fraction': None,
            'status': None, 'registration': {'accepted': False, 'method': METHOD, 'status': None, 'reason': None},
            'registration_global': None, 'windows': []}


def _owned_buildings(scene):
    owned = scene.get('owned_object_ids')
    owned = None if owned is None else set(owned)
    return [o for o in scene.get('objects', []) if o.get('kind') == 'building' and (owned is None or o.get('id') in owned)]


def _write_png(folder, name, array):
    Image.fromarray(np.ascontiguousarray(array, np.uint8)).save(folder/name, format='PNG')
    return {'path': name, 'sha256': hashlib.sha256((folder/name).read_bytes()).hexdigest()}


def _gates(knobs, min_accepted=MIN_ACCEPTED):
    return {**knobs, 'min_windows': MIN_WINDOWS, 'holdout_residual_px_max': HOLDOUT_RESIDUAL_PX,
            'min_accepted': min_accepted, 'coverage_radii': COVERAGE_RADII, 'valid_erode_px': VALID_ERODE_PX,
            'outlier_passes': OUTLIER_PASSES, 'composite_min_views': COMPOSITE_MIN_VIEWS,
            'comparison': 'temporal_evidence.register recorded per date as registration_global (never applied)'}


def build(scene_id, root=Path('.'), *, max_dates=3, fetch=True, window_px=DEFAULTS['window_px'],
          stride_px=DEFAULTS['stride_px'], min_response=DEFAULTS['min_response'], max_shift_px=DEFAULTS['max_shift_px'],
          neighbour_tolerance_px=DEFAULTS['neighbour_tolerance_px'], min_coverage=DEFAULTS['min_coverage'],
          min_accepted=MIN_ACCEPTED):
    """Register up to `max_dates` Wayback captures onto the saved scene per window; write the stack when >= 3 views.

    min_accepted (default 2, unchanged behaviour): accepted dates needed for an 'available' stack. With
    min_accepted=1 and exactly one accepted date the report is status 'pair' (saved PNG + 1 date, see the module
    docstring); two or more accepted dates are still an ordinary 'available' stack.
    """
    started = time.perf_counter()
    root = Path(root)
    if not _is_int(min_accepted) or min_accepted < 1:
        raise ValueError('min_accepted must be a positive integer (2 for a full stack, 1 to allow a two-view pair)')
    min_accepted = int(min_accepted)
    knobs = {'window_px': window_px, 'stride_px': stride_px, 'min_response': min_response, 'max_shift_px': max_shift_px,
             'neighbour_tolerance_px': neighbour_tolerance_px, 'min_coverage': min_coverage}
    scene, saved = load_scene(scene_id, root)
    saved_rgb = _rgb(saved)
    saved_gray = cv2.cvtColor(saved_rgb, cv2.COLOR_RGB2GRAY)
    height, width = saved_gray.shape
    grid = window_grid(saved_gray.shape, window_px, stride_px)
    window_px, stride_px = int(window_px), int(stride_px)   # numpy integers are accepted; the report stays plain JSON
    knobs.update(window_px=window_px, stride_px=stride_px)
    cache = root/CACHE
    report = {'version': VERSION, 'code_sha256': CODE_SHA256, 'computed_at': None, 'stack_id': None, 'status': None,
              'reason': None, 'scene_id': scene_id, 'source_image_sha256': scene['image_sha256'],
              'image_width': width, 'image_height': height,
              'reference': {'provider': scene.get('region', {}).get('provider'),
                            'acquisition_date': scene.get('region', {}).get('acquisition_date'),
                            'role': 'registration reference and first stack view'},
              'method': METHOD, 'gates': _gates(knobs, min_accepted), 'discovery': None, 'captures': [], 'n_accepted': 0, 'n_views': 1,
              'per_roof_local': [], 'per_window': {'window_px': window_px, 'stride_px': stride_px,
                                                   'grid': [len({g[0] for g in grid}), len({g[1] for g in grid})],
                                                   'windows_total': len(grid), 'codes': CODES, 'by_capture': {}},
              'stack': None, 'assets': {}, 'note': NOTE}
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
            # Whole-tile similarity registration, for comparison only (this is what scene_stack would do).
            _, _, row['registration_global'] = register_capture(saved, image)
            local = register_local(saved_rgb, image, **knobs)
            row['registration'], row['windows'] = local['registration'], local['windows']
            row['status'] = 'accepted' if local['registration']['accepted'] else 'rejected'
            if not local['registration']['accepted']:
                continue
            row['combined_fraction'] = _round(local['valid'].mean())
            accepted.append({'row': row, 'image': local['aligned'], 'gray': cv2.cvtColor(local['aligned'], cv2.COLOR_RGB2GRAY),
                             'valid': local['valid'], 'footprint': local['footprint'], 'dx': local['dx'], 'dy': local['dy']})
    for row in report['captures']:
        report['per_window']['by_capture'][row['acquisition_id']] = {
            'windows_total': len(row['windows']), 'windows_accepted': sum(w['accepted'] for w in row['windows']),
            'rejections': row['registration'].get('rejections')}
    ids = [c['row']['acquisition_id'] for c in accepted]
    report['n_accepted'], report['n_views'] = len(accepted), len(accepted) + 1
    report['stack_id'] = stack_id(scene['image_sha256'], ids)
    # Per-roof ECC on the locally aligned grays (scene_stack.local_refinement): recorded, never applied.
    for c in accepted:
        c['edge_valid'] = edge_mask(c['valid'], c['footprint'])
        for obj in _owned_buildings(scene):
            try:
                local = local_refinement(saved_gray, c['gray'], obj['ring'], valid=c['valid'], footprint=c['footprint'])
            except ValueError as error:
                local = {'bbox_px': None, 'local_shift_px': None, 'local_shift_norm_px': None, 'local_corr': None,
                         'within_budget': None, 'valid_fraction': None, 'footprint_fraction': None, 'error': str(error)}
            report['per_roof_local'].append({'object_id': obj.get('id'), 'capture': c['row']['acquisition_id'], **local})
    folder = stack_path(root, report['stack_id'])
    folder.mkdir(parents=True, exist_ok=True)
    if report['status'] is None and len(accepted) < min_accepted:
        report['status'] = 'not_available'
        report['reason'] = (f'{len(accepted)} accepted capture(s); the stack needs at least {min_accepted} '
                            f'in addition to the saved reference')
    if report['status'] is None and len(accepted) == 1:
        # Two views (only reachable with min_accepted=1): no median/min/max/disagreement, never 'available'.
        c, = accepted
        i = c['row']['index']
        consensus = edge_consensus([saved, Image.fromarray(c['image'])], [np.ones((height, width), bool), c['edge_valid']])
        count = 1 + c['valid'].astype(np.uint8)          # the saved view everywhere, the date where valid
        composite, composite_info = enhanced_pair_rgb(saved_rgb, c['image'], c['valid'])
        assets = {'edge-consensus.png': consensus, 'valid-count.png': count, 'enhanced-pair-rgb.png': composite,
                  f'aligned-{i}.png': c['image'], f'valid-{i}.png': c['valid'].astype(np.uint8)*255,
                  f'shift-field-{i}.png': shift_field_image(c['dx'], c['dy'], c['valid'], max_shift_px)}
        for name, array in assets.items():
            report['assets'][name] = _write_png(folder, name, array)
        report['stack'] = {'views': PAIR_VIEWS, 'mode': PAIR, 'gray_only': True, 'registration_method': METHOD,
                           'brightness_offsets': [composite_info['offset_gray']],
                           'combined_fraction': _round((count >= 2).mean()),
                           'edge_consensus_fraction_both': _round((consensus == PAIR_VIEWS).mean()),
                           'edge_consensus_fraction_any': _round((consensus >= 1).mean()),
                           'canny': list(scene_stack.CANNY), 'edge_dilate_px': scene_stack.EDGE_DILATE_PX,
                           'edge_border_px': scene_stack.EDGE_BORDER_PX,
                           'preprocessing': 'roof_line_fit.enhance_roof_detail (bilateral + CLAHE) for edges; '
                                            'median-gray brightness offset for the pair composite',
                           'enhanced_pair_rgb': composite_info,
                           'shift_field_png': 'hue = shift direction, value = |shift| / max_shift_px, black = invalid',
                           'note': PAIR_STACK_NOTE}
        report['status'] = PAIR
        report['reason'] = ('1 accepted capture registered window by window onto the saved scene grid; two-view pair '
                            f'(min_accepted={min_accepted}), not an available stack')
        report['note'] = f'{NOTE} {PAIR_REPORT_NOTE}'
    if report['status'] is None:
        stats = stack_statistics(saved_gray, [c['gray'] for c in accepted], [c['valid'] for c in accepted])
        consensus = edge_consensus([saved, *(Image.fromarray(c['image']) for c in accepted)],
                                   [np.ones((height, width), bool), *(c['edge_valid'] for c in accepted)])
        composite, composite_info = enhanced_median_rgb(saved_rgb, [c['image'] for c in accepted], [c['valid'] for c in accepted])
        assets = {'stack-median.png': stats['median'], 'stack-min.png': stats['min'], 'stack-max.png': stats['max'],
                  'disagreement.png': stats['ptp'], 'edge-consensus.png': consensus, 'valid-count.png': stats['count'],
                  'enhanced-median-rgb.png': composite}
        for name, array in assets.items():
            report['assets'][name] = _write_png(folder, name, array)
        for c in accepted:
            i = c['row']['index']
            report['assets'][f'aligned-{i}.png'] = _write_png(folder, f'aligned-{i}.png', c['image'])
            report['assets'][f'valid-{i}.png'] = _write_png(folder, f'valid-{i}.png', c['valid'].astype(np.uint8)*255)
            report['assets'][f'shift-field-{i}.png'] = _write_png(folder, f'shift-field-{i}.png',
                                                                  shift_field_image(c['dx'], c['dy'], c['valid'], max_shift_px))
        count = stats['count']
        report['stack'] = {'views': len(accepted) + 1, 'gray_only': True, 'registration_method': METHOD,
                           'brightness_offsets': [_round(o) for o in stats['offsets']],
                           'combined_fraction': _round((count >= 2).mean()),
                           'full_fraction': _round((count == len(accepted) + 1).mean()),
                           'disagreement_median': _round(np.median(stats['ptp'][count >= 2])) if (count >= 2).any() else None,
                           'disagreement_p90': _round(np.percentile(stats['ptp'][count >= 2], 90)) if (count >= 2).any() else None,
                           'edge_consensus_fraction_full': _round((consensus == len(accepted) + 1).mean()),
                           'edge_consensus_fraction_any': _round((consensus >= 1).mean()),
                           'canny': list(scene_stack.CANNY), 'edge_dilate_px': scene_stack.EDGE_DILATE_PX,
                           'edge_border_px': scene_stack.EDGE_BORDER_PX,
                           'disagreement_defined_where': 'valid-count.png >= 2 (0 elsewhere means one view, not agreement)',
                           'preprocessing': 'roof_line_fit.enhance_roof_detail (bilateral + CLAHE) for edges; '
                                            'median-gray brightness offset for the gray stack',
                           'enhanced_median_rgb': composite_info,
                           'shift_field_png': 'hue = shift direction, value = |shift| / max_shift_px, black = invalid'}
        report['status'] = 'available'
        report['reason'] = f'{len(accepted)} accepted captures registered window by window onto the saved scene grid'
    report['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    report['computed_at'] = _now()
    atomic_json(folder/'report.json', report)
    return report


def stack_mode(report):
    """'stack' for an available (>= 3 views) report, 'pair' for a two-view pair report, None otherwise.

    The one place consumers (multidate_agreement, eyes_experiments) dispatch on a stack report's status.
    """
    status = (report or {}).get('status') if isinstance(report, dict) else None
    return 'stack' if status == 'available' else PAIR if status == PAIR else None


def _load_report(report_or_stack_id, root):
    """(folder, report.json) for a stack id or a report dict, as scene_stack.views resolves them."""
    identifier = report_or_stack_id.get('stack_id') if isinstance(report_or_stack_id, dict) else report_or_stack_id
    if not isinstance(identifier, str):
        raise ValueError('Invalid stack id')
    folder = stack_path(root, identifier)
    return folder, json.loads((folder/'report.json').read_text())


def _verified_asset(folder, report, name):
    """Pixels of a recorded asset; the path must be a bare name in the stack folder and the bytes must match its sha256."""
    asset = (report.get('assets') or {}).get(name)
    if not isinstance(asset, dict):
        raise ValueError(f'Stack asset {name} is not recorded in the report')
    if asset.get('path') != Path(asset.get('path') or '').name or not asset.get('path'):
        raise ValueError(f'Stack asset {name} must live in the stack folder')
    data = (folder/asset['path']).read_bytes()
    if hashlib.sha256(data).hexdigest() != asset.get('sha256'):
        raise ValueError(f'Stack asset {name} does not match its recorded sha256')
    return np.asarray(Image.open(folder/asset['path']))


def pair_views(report_or_stack_id, root=Path('.')):
    """Extra LSD/edge views of a 'pair' report, else []: [('edge-consensus', counts scaled 0..255 by the two views),
    ('aligned-date-<date>', gray of the aligned date, 0 outside its valid mask)]. Never a 'stack-median' (needs 3 views).
    Lines from these views must still pass line_evidence on the original RGB."""
    folder, report = _load_report(report_or_stack_id, root)
    if report.get('status') != PAIR:
        return []
    n_views = int((report.get('stack') or {}).get('views') or PAIR_VIEWS)
    consensus = _verified_asset(folder, report, 'edge-consensus.png')
    result = [('edge-consensus', np.ascontiguousarray(np.rint(consensus.astype(float)*255/n_views).astype(np.uint8)))]
    for capture in report.get('captures') or []:
        if not (capture.get('registration') or {}).get('accepted'):
            continue
        i = capture['index']
        aligned = _rgb(_verified_asset(folder, report, f'aligned-{i}.png'))
        valid = _verified_asset(folder, report, f'valid-{i}.png') > 127
        gray = cv2.cvtColor(aligned, cv2.COLOR_RGB2GRAY)
        gray[~valid] = 0
        result.append((f'aligned-date-{capture.get("date")}', np.ascontiguousarray(gray, np.uint8)))
    return result


def views(report_or_stack_id, root=Path('.')):
    """Extra LSD/edge views for any stack report: 'available' -> scene_stack.views (stack-median, edge-consensus);
    'pair' -> pair_views; anything else -> []. Consumers call this one helper instead of dispatching themselves."""
    _, report = _load_report(report_or_stack_id, root)
    mode = stack_mode(report)
    if mode == 'stack':
        return scene_stack.views(report, root)
    if mode == PAIR:
        return pair_views(report, root)
    return []


def compact(report):
    """Report without per-window rows, global transforms or per-roof rows: what the CLI prints."""
    captures = []
    for c in report['captures']:
        global_ = c.get('registration_global')
        captures.append({**{k: v for k, v in c.items() if k != 'windows'},
                         'registration_global': {k: v for k, v in global_.items() if k != 'transform'} if global_ else global_})
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
    parser.add_argument('--window-px', type=int, default=DEFAULTS['window_px'])
    parser.add_argument('--stride-px', type=int, default=DEFAULTS['stride_px'])
    parser.add_argument('--min-response', type=float, default=DEFAULTS['min_response'])
    parser.add_argument('--max-shift-px', type=float, default=DEFAULTS['max_shift_px'])
    parser.add_argument('--neighbour-tolerance-px', type=float, default=DEFAULTS['neighbour_tolerance_px'])
    parser.add_argument('--min-coverage', type=float, default=DEFAULTS['min_coverage'])
    parser.add_argument('--allow-pair', action='store_true',
                        help='min_accepted=1: one accepted date yields a two-view "pair" report (never an available stack)')
    args = parser.parse_args(argv)
    report = build(args.scene_id, args.root, max_dates=args.max_dates, fetch=not args.no_fetch, window_px=args.window_px,
                   stride_px=args.stride_px, min_response=args.min_response, max_shift_px=args.max_shift_px,
                   neighbour_tolerance_px=args.neighbour_tolerance_px, min_coverage=args.min_coverage,
                   min_accepted=1 if args.allow_pair else MIN_ACCEPTED)
    print(json.dumps(compact(report), indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
