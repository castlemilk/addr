"""Multi-date edge/ridge consistency for owned roofs on a shared orthomosaic grid.

Detector-free and network-free: the caller supplies dated images already registered
onto the saved scene grid (arrays plus valid masks from the stack module or a
scene_temporal run) and the stack report with its registration gates. For every
owned building this module asks, per date, whether the stored outline and roof
lines coincide with image edges (Canny, 1.5 px, as scene_roof_diagnostics does on
the saved PNG) and how many dates agree. The saved PNG counts as one date.

Consistency across dates on a shared orthomosaic grid is not accuracy: shared
orthorectification errors persist across dates, and nothing here is a label.
Rows go only to the eyes sidecar index (`multidate`); production is never written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import eyes_index
from .scene_roof_diagnostics import (
    EDGE_TOLERANCE_PX,
    _jsonable,
    _round,
    _sample,
    building_objects,
    canny_edges,
    edge_distance,
    edge_support,
    load_scene,
)

VERSION = 'multidate-agreement-v1'
TABLE = 'multidate'
NOTE = 'consistency across dates on a shared orthomosaic grid; not accuracy'
SAVED = 'saved'
SUPPORT_THRESHOLD = .5
CONSENSUS_DATES = 2
# A registered capture is black outside its valid mask (warpAffine fill); that step is a strong Canny
# edge that is not image content. Samples within BORDER_PX of an invalid pixel are excluded on that date.
BORDER_PX = 3
# Spec CP3: median hold-out residual <= 2 px (scene_stack.MEDIAN_RESIDUAL_PX). Applied defensively here
# too, so an older scene_temporal run cannot feed a looser 'accepted' date into the consensus.
RESIDUAL_GATE_PX = 2.
# scene_local_stack pair mode: the saved PNG plus exactly ONE accepted date (status 'pair', never 'available').
# Rows are saved + 1 date, so dates_with_support is in {0, 1, 2} and boundary consensus is over two views.
PAIR_MODE = 'pair'
PAIR_NOTE = 'two views only'
MULTIDATE = Path('data/roof-review-loop/multidate')
OUTPUT = Path('data/reviews/eyes/multidate')
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
GRID_NOTES = {
    'saved-png': 'dates registered onto the saved scene PNG grid (stack module)',
    'wayback-reference': ('scene_temporal run: dates registered to the newest Wayback capture resampled onto the '
                          'scene grid; alignment to the saved PNG itself is not verified'),
}


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def _is_pair(stack_report):
    """True for a two-view pair stack: a normalised report carrying mode 'pair', or a raw scene_local_stack report whose
    status scene_local_stack.stack_mode (the shared status dispatcher) classifies as 'pair'."""
    stack_report = stack_report or {}
    if stack_report.get('mode') == PAIR_MODE:
        return True
    from .scene_local_stack import stack_mode
    return stack_mode(stack_report) == PAIR_MODE


def gray_view(array):
    """HxW uint8 gray from an HxW / HxWx3 uint8 array or a PIL image; None stays None."""
    if array is None:
        return None
    if isinstance(array, Image.Image):
        array = np.asarray(array.convert('RGB'))
    array = np.asarray(array)
    if array.ndim == 3 and array.shape[2] == 3:
        return cv2.cvtColor(np.ascontiguousarray(array, np.uint8), cv2.COLOR_RGB2GRAY)
    if array.ndim == 3 and array.shape[2] == 4:
        return cv2.cvtColor(np.ascontiguousarray(array, np.uint8), cv2.COLOR_RGBA2GRAY)
    if array.ndim == 2:
        return np.ascontiguousarray(array, np.uint8)
    raise ValueError('A dated view must be an HxW or HxWx3 uint8 array')


def _valid_mask(valid, shape):
    if valid is None:
        return np.ones(shape, bool)
    if isinstance(valid, Image.Image):
        valid = np.asarray(valid.convert('L'))
    valid = np.asarray(valid)
    if valid.shape != shape:
        return None
    return valid.astype(bool) if valid.dtype != bool else valid


def date_records(stack_report):
    """Normalised date list from a stack report (`dates`) or a scene_temporal report (`captures`)."""
    entries = (stack_report or {}).get('dates')
    if entries is None:
        entries = (stack_report or {}).get('captures') or []
    records = []
    for index, entry in enumerate(entries):
        registration = entry.get('registration') or {}
        residual = None
        for key in ('check_median_residual_px', 'median_residual_px', 'holdout_residual_px', 'residual_px'):
            value = registration.get(key, entry.get(key))
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                residual = float(value)
                break
        accepted = registration.get('accepted')
        if isinstance(accepted, bool):
            # scene_stack keeps register()'s raw status and records its own residual gate as `accepted`.
            status = 'accepted' if accepted else 'rejected'
        else:
            status = entry.get('status') or registration.get('status')
        records.append({'date': entry.get('date') or entry.get('acquisition_date'), 'status': status,
                        'residual_px': residual, 'index': entry.get('index', index),
                        'acquisition_id': entry.get('acquisition_id'),
                        'provider': entry.get('provider') or entry.get('sensor')})
    return records


def _edge_view(gray, valid):
    """(edges, distance map, usable mask) for one date.

    Pixels within BORDER_PX of an invalid pixel are not usable: their edges are dropped (the warp border
    is a step edge, not content) and their distance is NaN, so samples there are excluded, never misses.
    """
    edges = canny_edges(gray)
    usable = valid
    if not valid.all():
        kernel = np.ones((2*BORDER_PX+1, 2*BORDER_PX+1), np.uint8)
        usable = cv2.erode(valid.astype(np.uint8), kernel).astype(bool)
        edges = edges.copy()
        edges[~usable] = 0
    distance = edge_distance(edges)
    if not usable.all():
        distance = distance.copy()
        distance[~usable] = np.nan
    return edges, distance, usable


def date_views(image, stack_report, aligned_views):
    """Saved PNG first, then every accepted stack date with a usable view; the rest are skipped with a reason."""
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    shape = gray.shape
    edges, distance, _ = _edge_view(gray, np.ones(shape, bool))
    views = [{'date': SAVED, 'edges': edges, 'distance': distance, 'valid_fraction_frame': 1., 'residual_px': 0.}]
    records = date_records(stack_report)
    known = {record['date'] for record in records if record['date']}
    supplied = {}
    for view in aligned_views or []:
        date = view.get('date') or view.get('acquisition_date')
        supplied.setdefault(date, view)
    skipped = []
    for date in supplied:
        if date not in known:
            skipped.append({'date': date, 'reason': 'not in stack report'})
    used = set()
    for record in records:
        date = record['date']
        if not date:
            skipped.append({'date': date, 'reason': 'undated'})
            continue
        if date == SAVED:
            skipped.append({'date': date, 'reason': 'reserved date name'})
            continue
        if date in used:
            skipped.append({'date': date, 'reason': 'duplicate date in stack report'})   # one date is one vote
            continue
        if record['status'] != 'accepted':
            skipped.append({'date': date, 'reason': 'rejected in stack report'})
            continue
        if record['residual_px'] is not None and record['residual_px'] > RESIDUAL_GATE_PX:
            skipped.append({'date': date, 'reason': 'residual above gate'})
            continue
        view = supplied.get(date)
        if view is None or view.get('image') is None:
            skipped.append({'date': date, 'reason': 'unavailable'})
            continue
        try:
            dated = gray_view(view['image'])
        except ValueError:
            skipped.append({'date': date, 'reason': 'grid mismatch'})
            continue
        valid = _valid_mask(view.get('valid'), shape) if dated.shape == shape else None
        if dated.shape != shape or valid is None:
            skipped.append({'date': date, 'reason': 'grid mismatch'})
            continue
        edges, distance, usable = _edge_view(dated, valid)
        used.add(date)
        views.append({'date': date, 'edges': edges, 'distance': distance, 'valid_fraction_frame': _round(usable.mean()),
                      'residual_px': record['residual_px'], 'acquisition_id': record['acquisition_id'],
                      'provider': record['provider']})
    return views, skipped


def _open_ring(ring):
    points = np.asarray(ring, float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all():
        raise ValueError('Outline ring must be at least two finite pixel points.')
    if len(points) > 2 and np.allclose(points[0], points[-1]):
        points = points[:-1]
    return points


def _segment_samples(a, b):
    length = float(np.linalg.norm(b-a))
    t = np.linspace(0, 1, max(2, math.ceil(length)+1))
    return a+t[:, None]*(b-a)


def ring_samples(ring):
    """1 px-spaced samples along every outline edge (the same spacing edge_support uses)."""
    points = _open_ring(ring)
    return np.concatenate([_segment_samples(a, b) for a, b in zip(points, np.roll(points, -1, axis=0), strict=True)])


def _distances(view, samples):
    return _sample(view['distance'], samples[:, 0], samples[:, 1])


def _support(values):
    """(fraction of valid samples within tolerance | None, valid fraction) for one date's sample distances."""
    valid = ~np.isnan(values)
    if not valid.any():
        return None, 0.
    return _round(np.mean(values[valid] <= EDGE_TOLERANCE_PX)), _round(valid.mean())


def outline_support(ring, views):
    """Per date: edge_support detail (masked by that date's valid pixels) plus sample-level support/consensus."""
    samples = ring_samples(ring)
    by_date, hits = {}, []
    for view in views:
        values = _distances(view, samples)
        support, valid_fraction = _support(values)
        detail = edge_support(ring, view['edges'], distance=view['distance'])
        by_date[view['date']] = {'support': support, 'valid_fraction': valid_fraction, 'per_edge': detail['per_edge'],
                                 'median': detail['median'], 'weighted_mean': detail['weighted_mean'],
                                 'unsupported_fraction': detail['unsupported_fraction'],
                                 'samples_outside_or_invalid': detail['samples_outside_image']}
        hits.append(np.nan_to_num(values, nan=np.inf) <= EDGE_TOLERANCE_PX)
    agreeing = np.sum(np.stack(hits), axis=0) if hits else np.zeros(len(samples), int)
    consensus = _round(np.mean(agreeing >= CONSENSUS_DATES)) if len(samples) else None
    supported_dates = sum(1 for entry in by_date.values() if entry['support'] is not None and entry['support'] >= SUPPORT_THRESHOLD)
    return by_date, consensus, supported_dates, int(len(samples))


def roof_line_support(lines, meters_to_pixel, views):
    """Per date: fraction of 1 px samples along each stored roof line within tolerance of an edge, by kind and overall."""
    frame = np.asarray(meters_to_pixel, float)
    prepared = []
    for line in lines or []:
        try:
            a, b = (np.asarray(p, float) for p in line['line_meters'])
            if a.shape != (2,) or b.shape != (2,) or not (np.isfinite(a).all() and np.isfinite(b).all()):
                raise ValueError('bad line')
            pixels = np.c_[np.stack([a, b]), np.ones(2)] @ frame
            prepared.append((line.get('kind') or 'unknown', _segment_samples(pixels[0], pixels[1])))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            prepared.append((line.get('kind') if isinstance(line, dict) else None, None))
    by_date, overall = {}, {}
    for view in views:
        per_line, kinds, all_values = [], {}, []
        for kind, samples in prepared:
            if samples is None:
                per_line.append(None)
                continue
            values = _distances(view, samples)
            support, _ = _support(values)
            per_line.append(support)
            kinds.setdefault(kind, []).append(values)
            all_values.append(values)
        merged = np.concatenate(all_values) if all_values else np.array([])
        support, valid_fraction = _support(merged) if len(merged) else (None, 0.)
        by_date[view['date']] = {'all': support, 'valid_fraction': valid_fraction, 'per_line': per_line,
                                 'by_kind': {kind: _support(np.concatenate(values))[0] for kind, values in kinds.items()}}
        overall[view['date']] = support
    return by_date, overall, len(prepared)


def assess_object(obj, scene, views, *, stack_id=None, grid_reference=None, skipped=(), stack_image_sha256=None, mode=None):
    """One owned building's multi-date consistency payload (plain JSON; failures recorded as `error`).

    mode (default None: payload unchanged): 'pair' records `mode` and `mode_note` ('two views only') for a
    scene_local_stack pair report, whose rows are the saved PNG plus a single date.
    """
    payload = {'version': VERSION, 'code_sha256': CODE_SHA256, 'note': NOTE, 'object_id': obj.get('id'),
               'image_sha256': scene.get('image_sha256'), 'stack_id': stack_id, 'stack_source_image_sha256': stack_image_sha256,
               'grid_reference': grid_reference, 'grid_note': GRID_NOTES.get(grid_reference),
               'tolerance_px': EDGE_TOLERANCE_PX, 'support_threshold': SUPPORT_THRESHOLD, 'consensus_dates': CONSENSUS_DATES,
               'border_px': BORDER_PX, 'residual_gate_px': RESIDUAL_GATE_PX,
               'dates': [view['date'] for view in views], 'n_dates': len(views), 'skipped_dates': list(skipped),
               'registration_residual_px': {view['date']: view['residual_px'] for view in views},
               'valid_fraction_frame': {view['date']: view['valid_fraction_frame'] for view in views},
               'definitions': {
                   'edge_support_by_date': 'share of 1 px outline samples within tolerance of a Canny edge on that date; '
                                           'samples outside the date\'s valid mask or within border_px of it are excluded '
                                           '(valid_fraction), never counted as misses',
                   'valid_fraction_frame': 'share of the frame assessable on that date (valid mask eroded by border_px)',
                   'boundary_edge_consensus': f'share of outline samples supported on >= {CONSENSUS_DATES} dates (saved PNG is a date)',
                   'dates_with_support': f'dates whose outline support is >= {SUPPORT_THRESHOLD}',
                   'ridge_support_by_date': 'share of 1 px samples along every stored roof line (ridge/hip/valley) within '
                                            'tolerance of an edge on that date; per-kind and per-line detail in roof_line_support_by_date'}}
    if mode == PAIR_MODE:
        payload.update(mode=PAIR_MODE, mode_note=PAIR_NOTE)
    try:
        by_date, consensus, supported_dates, n_samples = outline_support(obj['ring'], views)
        payload.update(edge_support_by_date=by_date, boundary_edge_consensus=consensus, boundary_samples=n_samples,
                       dates_with_support=supported_dates)
    except (ValueError, TypeError, KeyError, IndexError) as error:
        payload.update(error=f'{type(error).__name__}: {error}'[:500], stage='outline_support')
    try:
        lines, overall, n_lines = roof_line_support(obj.get('roof_lines'), scene['meters_to_pixel'], views)
        payload.update(roof_line_support_by_date=lines, ridge_support_by_date=overall, n_roof_lines=n_lines)
    except (ValueError, TypeError, KeyError, IndexError) as error:
        payload.setdefault('error', f'{type(error).__name__}: {error}'[:500])
        payload.setdefault('stage', 'roof_line_support')
    payload['computed_at'] = _now()
    return _jsonable(payload)


def assess_scene(scene, image, stack_report, aligned_views, *, only_owned=True):
    """Rows for the `multidate` table: one per owned building object of the scene.

    stack_report: {stack_id, scene_id, grid_reference, dates: [{date, status, registration: {check_median_residual_px}}]}
      (a scene_temporal report with `captures` is accepted too).
    aligned_views: [{date, image: HxW|HxWx3 uint8 array or PIL image on the scene grid, valid: HxW bool | None}].
    """
    views, skipped = date_views(image, stack_report, aligned_views)
    return _rows(scene, views, skipped, stack_report, only_owned=only_owned)


def _rows(scene, views, skipped, stack_report, *, only_owned=True):
    stack_report = stack_report or {}
    stack_id = stack_report.get('stack_id') or stack_report.get('run')
    grid = stack_report.get('grid_reference') or 'saved-png'
    mode = PAIR_MODE if _is_pair(stack_report) else None
    rows = []
    for obj in building_objects(scene, only_owned=only_owned):
        payload = assess_object(obj, scene, views, stack_id=stack_id, grid_reference=grid, skipped=skipped,
                                stack_image_sha256=stack_report.get('source_image_sha256'), mode=mode)
        rows.append({'scene_id': scene['id'], 'object_id': obj['id'], 'model_signature': scene.get('model_signature'),
                     'version': VERSION, 'payload': payload})
    return rows


def _read_image(path):
    if not path.exists():
        return None
    with Image.open(path) as opened:
        return np.asarray(opened.convert('RGB'))


def _read_mask(path):
    if not path.exists():
        return None
    with Image.open(path) as opened:
        return np.asarray(opened.convert('L')) > 127


def _read_asset(folder, assets, name, reader):
    """A scene_stack asset is read only when the report lists it and its bytes match the recorded sha256."""
    asset = assets.get(name)
    if not isinstance(asset, dict):
        return None
    path = folder/Path(asset.get('path') or name).name
    if not path.exists():
        return None
    if hashlib.sha256(path.read_bytes()).hexdigest() != asset.get('sha256'):
        raise ValueError(f'Stack asset {name} does not match its recorded sha256')
    return reader(path)


def _from_scene_stack(folder, raw, stack_id):
    """scene_stack.build layout: report.json captures[] (date, sensor, registration{accepted, status, reason,
    median_residual_px}) and sha-recorded aligned-<index>.png / valid-<index>.png assets, on the saved PNG grid."""
    report = {'stack_id': raw.get('stack_id') or stack_id, 'scene_id': raw.get('scene_id'), 'grid_reference': 'saved-png',
              'source': 'scene_stack', 'stack_version': raw.get('version'), 'status': raw.get('status'),
              'source_image_sha256': raw.get('source_image_sha256'), 'dates': []}
    if _is_pair(raw):
        # scene_local_stack pair report: the same captures[]/aligned-<i>/valid-<i> contract with exactly one accepted date.
        report.update(mode=PAIR_MODE, mode_note=PAIR_NOTE)
    assets = raw.get('assets') or {}
    views = []
    for entry in raw.get('captures') or []:
        index, date = entry.get('index'), entry.get('date') or entry.get('acquisition_date')
        report['dates'].append({'date': date, 'index': index, 'registration': entry.get('registration') or {},
                                'acquisition_id': entry.get('acquisition_id'),
                                'provider': entry.get('sensor') or entry.get('provider')})
        views.append({'date': date, 'image': _read_asset(folder, assets, f'aligned-{index}.png', _read_image),
                      'valid': _read_asset(folder, assets, f'valid-{index}.png', _read_mask)})
    return report, views


def load_stack(root, stack_id):
    """(stack_report, aligned_views) for data/roof-review-loop/multidate/stacks/<id>/ (scene_stack report.json, or a
    stack.json listing dates with aligned/valid files), else a scene_temporal run under runs/<id>/ (report.json,
    aligned-<i>.png, valid-<i>.png). FileNotFoundError when none exists."""
    if not isinstance(stack_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', stack_id):
        raise ValueError('Invalid stack id')
    root = Path(root)
    stack_dir, run_dir = root/MULTIDATE/'stacks'/stack_id, root/MULTIDATE/'runs'/stack_id
    if (stack_dir/'stack.json').exists():
        report = json.loads((stack_dir/'stack.json').read_text())
        report.setdefault('stack_id', stack_id)
        report.setdefault('grid_reference', 'saved-png')
        views = []
        for entry in report.get('dates') or []:
            date = entry.get('date') or entry.get('acquisition_date')
            aligned = stack_dir/(entry.get('aligned') or f'aligned-{date}.png')
            valid = stack_dir/(entry.get('valid') or f'valid-{date}.png')
            views.append({'date': date, 'image': _read_image(aligned), 'valid': _read_mask(valid)})
        return report, views
    if (stack_dir/'report.json').exists():
        return _from_scene_stack(stack_dir, json.loads((stack_dir/'report.json').read_text()), stack_id)
    if (run_dir/'report.json').exists():
        raw = json.loads((run_dir/'report.json').read_text())
        report = {'stack_id': stack_id, 'scene_id': raw.get('scene_id'), 'grid_reference': 'wayback-reference',
                  'source': 'scene_temporal', 'status': raw.get('status'),
                  'source_image_sha256': raw.get('source_image_sha256'), 'dates': []}
        views = []
        for entry in raw.get('captures') or []:
            index = entry.get('index')
            report['dates'].append({'date': entry.get('acquisition_date'), 'status': entry.get('status'),
                                    'registration': entry.get('registration') or {}, 'index': index,
                                    'acquisition_id': entry.get('acquisition_id'), 'provider': entry.get('provider')})
            views.append({'date': entry.get('acquisition_date'), 'image': _read_image(run_dir/f'aligned-{index}.png'),
                          'valid': _read_mask(run_dir/f'valid-{index}.png')})
        return report, views
    raise FileNotFoundError(f'No multidate stack or run {stack_id} under {MULTIDATE}')


def run(root, scene_id, stack_id, *, dry_run=False, only_owned=True):
    """Assess one scene against one stack and write `multidate` rows (model_signature = scene's).

    Returns a summary with denominators; a missing stack is recorded as status 'unavailable'
    (nothing written) rather than raised, so batch callers can continue offline.
    """
    root = Path(root)
    base = {'version': VERSION, 'code_sha256': CODE_SHA256, 'scene_id': scene_id, 'stack_id': stack_id, 'note': NOTE,
            'dry_run': dry_run}
    try:
        stack_report, aligned_views = load_stack(root, stack_id)
    except FileNotFoundError as error:
        return {**base, 'status': 'unavailable', 'reason': str(error), 'rows': 0, 'computed_at': _now()}
    if stack_report.get('scene_id') not in (None, scene_id):
        raise ValueError(f'Stack {stack_id} belongs to scene {stack_report.get("scene_id")}, not {scene_id}')
    scene, image = load_scene(scene_id, root)
    source = stack_report.get('source_image_sha256')
    if source and source != scene['image_sha256']:
        # The saved PNG is the sole evidence gate: a stack registered onto other pixels says nothing about it.
        raise ValueError(f'Stack {stack_id} was registered onto image {source[:12]}…, not the saved PNG of '
                         f'{scene_id} ({scene["image_sha256"][:12]}…)')
    views, skipped = date_views(image, stack_report, aligned_views)
    rows = _rows(scene, views, skipped, stack_report, only_owned=only_owned)
    owned = set(scene.get('owned_object_ids', [o['id'] for o in scene['objects']]))
    buildings = building_objects(scene, only_owned=False)
    summary = {**base, 'status': 'ok', 'model_signature': scene.get('model_signature'), 'image_sha256': scene['image_sha256'],
               'stack_status': stack_report.get('status'), 'stack_source_image_sha256': source,
               'grid_reference': stack_report.get('grid_reference'), 'dates': [view['date'] for view in views],
               'skipped_dates': skipped, 'registration_residual_px': {view['date']: view['residual_px'] for view in views},
               'rows': len(rows), 'buildings_total': len(buildings), 'buildings_owned': sum(o['id'] in owned for o in buildings),
               'only_owned': only_owned, 'errors': sum('error' in row['payload'] for row in rows),
               'dates_with_support_histogram': _histogram(rows, 'dates_with_support'),
               'boundary_edge_consensus_median': _median(rows, 'boundary_edge_consensus'),
               **({'mode': PAIR_MODE, 'mode_note': PAIR_NOTE} if _is_pair(stack_report) else {}),
               'table': TABLE, 'computed_at': _now()}
    if not dry_run and rows:
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            eyes_index.replace_rows(db, TABLE, rows)
        from .scene_training import atomic_json
        target = root/OUTPUT/scene_id/f'{stack_id}.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(target, summary)
    return summary


def _histogram(rows, key):
    counts = {}
    for row in rows:
        value = row['payload'].get(key)
        counts[str(value)] = counts.get(str(value), 0)+1
    return counts


def _median(rows, key):
    values = [row['payload'].get(key) for row in rows if isinstance(row['payload'].get(key), (int, float))]
    return _round(np.median(values)) if values else None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Multi-date edge/ridge consistency for owned roofs (diagnostics, not accuracy).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--scene', required=True)
    parser.add_argument('--stack', required=True, help='stacks/<id>/stack.json or a scene_temporal runs/<id>')
    parser.add_argument('--dry-run', action='store_true', help='Assess without writing the eyes index or summary.')
    parser.add_argument('--include-unowned', action='store_true')
    args = parser.parse_args(argv)
    summary = run(Path(args.root), args.scene, args.stack, dry_run=args.dry_run, only_owned=not args.include_unowned)
    json.dump(summary, sys.stdout, sort_keys=True)
    sys.stdout.write('\n')
    return 0 if summary['status'] == 'ok' else 3


if __name__ == '__main__':
    raise SystemExit(main())
