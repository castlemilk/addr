"""Read-only inventory and live queue for the suburb processing workspace.

The worker owns inference and checkpoints. This reader never alters its frozen
snapshot, prediction files or reviewer labels, and never invents a house total.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from shapely.geometry import shape
from shapely.ops import unary_union

from . import eyes_index
from .address_resolution import apply, binding
from .building_addresses import linked, load_index, unresolved
from .property_address_resolution import visible_only
from .scene_addresses import catalog
from .scene_confidence import query
from .scene_suburb import SUBDIR, progress

# Eyes sidecar columns pulled with one json_extract per row so 10k diagnostics payloads never reach Python.
_DIAGNOSTIC_PATHS = {'planes': '$.topology.after.physical_planes', 'crease_runs': '$.topology.after.crease_runs',
                     'short_junction_links': '$.topology.after.short_junction_links',
                     'matched_edges': '$.line_refinement.matched_edges', 'detected_segments': '$.line_refinement.detected_segments',
                     'line_refinement_reason': '$.line_refinement.reason', 'crease_contrast_share': '$.crease_contrast.share_ge_threshold',
                     'edge_unsupported_fraction': '$.edge_support.original.unsupported_fraction',
                     'recomputed_matches_stored': '$.recomputed_matches_stored', 'error': '$.error'}
_REFERENCE_PATHS = {'iou': '$.iou', 'hausdorff_m': '$.hausdorff_m', 'roof_type_match': '$.roof_type_match',
                    'line_precision': '$.line_precision', 'line_recall': '$.line_recall', 'image_identity': '$.image_identity'}
# Reviewer roof-line corrections vs the model's starting estimate (line_corrections.py): agreement, a draft while
# ref_checked is false, never accuracy.
_LINE_CORRECTION_PATHS = {'review_revision': '$.review_revision', 'ref_checked': '$.ref_checked', 'line_precision': '$.line_precision',
                          'line_recall': '$.line_recall', 'n_model_lines': '$.n_model_lines', 'n_ref_edges': '$.n_ref_edges',
                          'count_ratio': '$.count_ratio', 'n_components_ref': '$.topology_ref.n_components'}
REFERENCE_TIERS = ('T1', 'T2')  # Agent observations (T3) and context rows (T4) never anchor an agreement.


def _compact(value):
    return round(value, 3) if isinstance(value, float) else value


def _extract(paths):
    return 'json_extract(payload,' + ','.join(f"'{p}'" for p in paths.values()) + ')'


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _eyes_lookup(root, wanted):
    """Read the eyes sidecar index once, read-only, for the (scene_id, object_id) pairs in wanted.

    Returns a callable props -> compact eyes payload (or None), or None when the index is
    missing, locked or unreadable. Rows are diagnostics and reference agreement, never accuracy;
    the workspace must render without them. Each table is read independently so a NULL or
    malformed payload, or a table an older writer never created, drops only its own signal.
    """
    if not wanted or not eyes_index.path(root).exists():
        return None
    reference, diagnostics, shadow, cross, corrections = {}, {}, {}, {}, {}
    # Only JSON objects are decoded; NULL, malformed text and bare JSON scalars are skipped in SQL.
    usable = "CASE WHEN json_valid({p}) THEN json_type({p}) END = 'object'"

    def references(db):
        for sid, oid, ref_id, tier, values in db.execute(
                'SELECT a.scene_id, a.object_id, a.ref_id, COALESCE(r.tier, json_extract(a.payload, \'$.ref_tier\')), '
                + _extract(_REFERENCE_PATHS).replace('payload', 'a.payload')
                + ' FROM agreement a LEFT JOIN "references" r ON r.ref_id = a.ref_id WHERE ' + usable.format(p='a.payload')):
            if (sid, oid) not in wanted or tier not in REFERENCE_TIERS:
                continue
            row = dict(zip(_REFERENCE_PATHS, json.loads(values), strict=True))
            row['iou'] = _number(row['iou'])
            # Highest IoU wins; a confirmed reviewer decision (T1) beats a human edit (T2) on ties,
            # then ref_id, so the shown reference never depends on row order.
            rank = (row['iou'] if row['iou'] is not None else -1.0, tier == 'T1', str(ref_id))
            best = reference.get((sid, oid))
            if best is None or rank > best[0]:
                reference[(sid, oid)] = (rank, {'tier': tier, **{k: _compact(v) for k, v in row.items()}})

    def roof_diagnostics(db):
        for sid, oid, signature, values in db.execute(
                f'SELECT scene_id, object_id, model_signature, {_extract(_DIAGNOSTIC_PATHS)} FROM roof_diagnostics WHERE ' + usable.format(p='payload')):
            if (sid, oid) in wanted:
                row = dict(zip(_DIAGNOSTIC_PATHS, json.loads(values), strict=True))
                if row['error'] is None:
                    del row['error']
                diagnostics[(sid, oid, signature)] = {k: _compact(v) for k, v in row.items()}

    def shadow_bands(db):
        for sid, oid, signature, band in db.execute(
                "SELECT scene_id, object_id, model_signature, json_extract(payload, '$.band_v2') FROM shadow_bands WHERE " + usable.format(p='payload')):
            if (sid, oid) in wanted and band is not None:
                shadow[(sid, oid, signature)] = band

    def cross_capture(db):
        for sid, oid, other_sid, other_oid, values in db.execute(
                "SELECT scene_id, object_id, other_scene_id, other_object_id, "
                "json_extract(payload, '$.iou', '$.band', '$.other_band', '$.hidden_by') FROM cross_capture WHERE " + usable.format(p='payload')):
            iou, band, other_band, hidden_by = json.loads(values)
            iou = _number(iou)
            if (sid, oid) in wanted:  # this object is the row's subject
                cross.setdefault((sid, oid), []).append((other_sid, other_oid, other_band, iou, hidden_by == 'other'))
            if (other_sid, other_oid) in wanted:  # this object is the capture the subject was compared with
                cross.setdefault((other_sid, other_oid), []).append((sid, oid, band, iou, hidden_by == 'self'))

    def line_corrections(db):
        for sid, oid, revision, values in db.execute(
                f'SELECT scene_id, object_id, review_revision, {_extract(_LINE_CORRECTION_PATHS)} FROM line_corrections WHERE '
                + usable.format(p='payload')):
            if (sid, oid) not in wanted:
                continue
            row = dict(zip(_LINE_CORRECTION_PATHS, json.loads(values), strict=True))
            row['ref_checked'] = None if row['ref_checked'] is None else bool(row['ref_checked'])  # json_extract gives 0/1
            # The latest saved review revision wins; rows for older revisions stay in the index untouched.
            rank = revision if isinstance(revision, int) else -1
            best = corrections.get((sid, oid))
            if best is None or rank > best[0]:
                corrections[(sid, oid)] = (rank, {k: _compact(v) for k, v in row.items()})

    try:
        with closing(eyes_index.connection(root, readonly=True)) as db:
            db.execute('PRAGMA busy_timeout=250')
            for read in (references, roof_diagnostics, shadow_bands, cross_capture, line_corrections):
                try:
                    read(db)
                except (sqlite3.Error, ValueError, TypeError):
                    continue  # one broken table or row set never hides the others
    except (sqlite3.Error, ValueError, TypeError, OSError):
        return None

    def lookup(props):
        key, signed = (props['scene_id'], props['object_id']), (props['scene_id'], props['object_id'], props.get('model_signature'))
        eyes = {}
        if key in reference:
            eyes['reference'] = reference[key][1]
        if signed in diagnostics:
            eyes['diagnostics'] = diagnostics[signed]
        if signed in shadow:
            eyes['shadow_band_v2'] = shadow[signed]
        if key in corrections:
            eyes['line_corrections'] = corrections[key][1]
        if key in cross:
            pairs = cross[key]
            eyes['cross_capture'] = {'n_pairs': len(pairs),
                                     'bands': [{'scene_id': o_sid, 'object_id': o_oid, 'band': band, 'iou': _compact(iou)}
                                               for o_sid, o_oid, band, iou, _ in pairs],
                                     'hidden_by': [f'{o_sid}/{o_oid}' for o_sid, o_oid, _, _, hidden in pairs if hidden]}
        return eyes or None
    return lookup


def _queue(root, suburb):
    """Tile queue and completed-tile object totals from the run checkpoint.

    Reconciles suburb counts, timestamps and coverage statuses in place from the same
    checkpoint used for the queue rows, so every caller shows one consistent snapshot.
    """
    directory = root/SUBDIR/'runs'/suburb['run']
    plan = json.loads((directory/'plan.json').read_text())
    state_file = directory/'run.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {'results': {}}
    results = state.get('results', {})
    queue, objects, counts = [], Counter(), Counter()
    for index, tile in enumerate(plan['tiles']):
        sid = f'mooroolbark-{suburb["run"][:8]}-'+'-'.join(map(str, tile))
        result = results.get(sid, {})
        status = result.get('status', 'pending')
        if status == 'running' and suburb['status'] == 'interrupted':
            status = 'interrupted'
        counts[status] += 1
        if status == 'complete':
            objects.update(result.get('objects', {}))
        queue.append({'id': sid, 'order': index+1, 'tile': tile, 'status': status,
                      'attempts': result.get('attempts', 0), 'elapsed_seconds': result.get('elapsed_seconds'),
                      'objects': result.get('objects', {}), 'confidence': result.get('confidence', {}),
                      'error': result.get('error'),
                      'has_capture': (directory/'captures'/sid/'image.png').exists(),
                      'review_url': f'/scenes?scene={sid}&kind=building&status=all&layer=model' if status == 'complete' else None})
    # Reconcile totals from the same checkpoint used for queue rows.
    suburb.update(counts=dict(counts), updated_at=state.get('updated_at'), started_at=state.get('started_at'),
                  current=state.get('current') if suburb['status'] == 'running' else None)
    for feature, row in zip(suburb['coverage']['features'], queue, strict=True):
        feature['properties'].update(status=row['status'], scene_id=row['id'], order=row['order'])
    return queue, objects


def summary(root=Path('.')):
    """Progress strip, tile queue and object totals only: cheap enough to poll.

    Never runs the roof query, opens the address index or the eyes sidecar index. Band
    counts are summed from each completed tile's checkpoint, so they cover the scan's own
    tiles only (not previously analysed captures) and are model bands, not accuracy.
    """
    suburb = progress(root)
    if not suburb:
        return {'suburb': None, 'queue': [], 'objects': {}, 'counts': {}, 'fetched_at': time.time()}
    queue, objects = _queue(root, suburb)
    counts = Counter()
    for row in queue:
        if row['status'] == 'complete':
            counts.update(row['confidence'])
    return {'suburb': suburb, 'queue': queue, 'objects': dict(objects), 'counts': dict(counts), 'fetched_at': time.time()}


def inventory(root=Path('.')):
    suburb = progress(root)
    if not suburb:
        return {'suburb': None, 'queue': [], 'features': [], 'counts': {}, 'objects': {}, 'fetched_at': time.time()}
    queue, objects = _queue(root, suburb)
    found = query(suburb['bounds'], root)
    boundary = unary_union([shape(f['geometry']) for f in suburb['boundary']['features']])
    found['features'] = [f for f in found['features'] if boundary.covers(shape(f['geometry']).centroid)]
    reviews = {}
    address_index = load_index(root)
    address_names = {}
    for feature in found['features']:
        props = feature['properties']
        sid = props['scene_id']
        if sid not in reviews:
            path = root/'data/scenes'/sid/'review.json'
            saved = json.loads(path.read_text()) if path.exists() else {}
            reviews[sid] = (saved.get('model_signature'), {o['id']: o for o in saved.get('objects', [])})
            address_names.update(catalog([o.get('address_id') for o in saved.get('objects', [])], root=root))
        signature, reviewed = reviews[sid]
        obj = reviewed.get(props['object_id'])
        props['review_status'] = ('stale' if signature != props['model_signature'] else obj['status']) if obj else 'candidate'
        props['issue_count'] = len(obj.get('review_issues', [])) if obj else 0
        props['key'] = sid+'/'+props['object_id']
        # Use the reviewer's current object identity (e.g. an explicitly linked
        # house), while preview geometry and evidence remain the model's.
        if obj and signature == props['model_signature'] and obj.get('label'):
            props['model_label'] = props['label']
            props['label'] = obj['label']
        props['property_address'] = (linked(obj, address_names) if obj and signature == props['model_signature'] else None) or (
            address_index.match(shape(feature['geometry'])) if address_index else unresolved('Property address coverage has not been downloaded.'))
        props['property_address'] = visible_only(props['property_address'], props.get('clipped', False))
        current_binding = binding(shape(feature['geometry']), props['model_signature'], props['property_address'], obj.get('address_id') if obj and signature == props['model_signature'] else None)
        props['property_address'] = apply(props['property_address'], root, sid, props['object_id'], current_binding)
        # The capture's address is image context, never an inferred roof address.
        address = props['property_address']
        props['search_text'] = ' '.join([props['label'], props['capture_title'], sid, props['object_id'],
                                       address.get('full_address', ''), *[a['full_address'] for a in address['candidates']]])
    eyes = _eyes_lookup(root, {(f['properties']['scene_id'], f['properties']['object_id']) for f in found['features']})
    for feature in found['features']:
        props = feature['properties']
        # No observed image lines means no mismatch measurement; the raw field stays for audits.
        props['image_line_mismatch_display'] = props.get('image_line_mismatch_m') if props.get('observed_segments') else None
        sidecar = eyes(props) if eyes else None
        if sidecar:
            props['eyes'] = sidecar
    found['counts'] = dict(Counter(f['properties']['band'] for f in found['features']))
    found['address_counts'] = dict(Counter(f['properties']['property_address']['status'] for f in found['features']))
    return {**found, 'suburb': suburb, 'queue': queue, 'objects': dict(objects), 'fetched_at': time.time()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summary', action='store_true', help='progress, tile queue and object totals only (no roofs)')
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args(argv)
    print(json.dumps((summary if args.summary else inventory)(args.root), separators=(',', ':')))


if __name__ == '__main__':
    main()
