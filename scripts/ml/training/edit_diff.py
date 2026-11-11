"""Per-object model-estimate vs reviewer-outline diff for every reviewed building.

Read-only over data/scenes; writes only under --out. Numbers are agreement with the reviewer's outline, not accuracy.

For every review.json building with status confirmed or needs_adjustment, one row pairing the reviewer's outline with
the same-id object of the model the reviewer saw (models/<review.model_signature>.json, else latest.json when the
signatures agree, else an unverified pair with null geometry). Confirmed-as-drawn objects are rows too
(`edit_kind: none`): they are the denominator for "the estimate needed no change". Machine-provenance objects a
reviewer confirmed stay T3 rows with `authority_blocker` set; nothing here turns them into human decisions.

    python -m scripts.ml.training.edit_diff [--root .] [--out data/reviews/eyes/training/edit-diff] [--scene-ids ID ...]
                                            [--summary]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np

from .. import reference_registry as registry
from ..review_edit_diffs import first_revision_with_outline
from . import common, export_dataset, metrics

DEFAULT_OUT = Path('data/reviews/eyes/training/edit-diff')
ROW_STATUSES = ('confirmed', 'needs_adjustment')
RECTANGLE_RECTILINEARITY = .98
WORST_N = 10
NOTE = 'Agreement between the model estimate the reviewer saw and the reviewer outline; not accuracy. ' + common.NOTE
SKIPPABLE = registry.SKIPPABLE


def pair_model(entry):
    """({id: model object}, pair) with pair snapshot | latest | unverified, or (None, 'frame_mismatch').

    Mirrors export_dataset.review_model: the snapshot the reviewer saw when it exists and shows this image; latest.json
    when the review's signature is the current model; otherwise an unverified pair (a differing ring proves nothing).
    """
    scene, review, folder = entry['scene'], entry['review'], entry['folder']
    signature = review.get('model_signature')
    snapshot = folder / 'models' / f'{signature}.json' if isinstance(signature, str) and registry.SHA256.fullmatch(signature) else None
    if snapshot is not None and snapshot.exists():
        try:
            model = common.read_json(snapshot)
        except ValueError:
            model = None
        if isinstance(model, dict):
            if model.get('image_sha256') != scene.get('image_sha256'):
                return None, 'frame_mismatch'
            return _by_id(model), 'snapshot'
    if signature == scene.get('model_signature'):
        return _by_id(scene), 'latest'
    return _by_id(scene), 'unverified'


def _by_id(model):
    return {o.get('id'): o for o in model.get('objects') or [] if isinstance(o, dict) and o.get('id')}


def _edit_kind(obj, model_obj):
    if model_obj is None:
        return 'manual', True, False
    ring_changed = not registry._same_ring(obj.get('ring'), model_obj.get('ring'))
    holes_changed = not registry._same_holes(obj.get('holes'), model_obj.get('holes') or [])
    kind = 'outline' if ring_changed else 'holes_only' if holes_changed else 'none'
    return kind, ring_changed, holes_changed


def object_row(entry, obj, model_obj, pair):
    scene, review = entry['scene'], entry['review']
    width, height = int(scene['image_width']), int(scene['image_height'])
    px_m = common.pixel_size_m(scene)
    model_ring = model_obj.get('ring') if model_obj else None
    model_holes = (model_obj.get('holes') or []) if model_obj else None
    verified = pair in ('snapshot', 'latest')
    tier, provenance, _ = registry.review_tier(obj, model_ring, verified, model_holes=model_holes)
    edit_kind, ring_changed, holes_changed = _edit_kind(obj, model_obj)
    stamp = obj.get('provenance') if isinstance(obj.get('provenance'), dict) else {}
    human = common.ring_polygon(obj.get('ring'), obj.get('holes'))
    model = common.ring_polygon(model_ring, model_holes) if model_obj else None
    row = {'scene_id': entry['scene_id'], 'object_id': obj['id'], 'status': obj.get('status'),
           'geometry_reviewed': bool(obj.get('geometry_reviewed')), 'tier': tier, 'registry_provenance': provenance,
           'authority_blocker': registry.review_authority_blocker(obj) or None,
           'provenance_source': stamp.get('source'), 'human_confirmed': bool(stamp.get('human_confirmed')),
           'structure_kind': obj.get('structure_kind'), 'roof_line_edits_present': bool(registry.line_edits(obj)),
           'pair': pair, 'model_signature': review.get('model_signature'), 'image_sha256': scene.get('image_sha256'),
           'pixel_size_m': px_m, 'ring_changed': ring_changed, 'holes_changed': holes_changed, 'edit_kind': edit_kind,
           'edited': edit_kind != 'none', 'geometry': 'ok', **dict.fromkeys(metrics.OUTLINE_PAIR_KEYS),
           'exceeds_vertex_tolerance': None, 'boundary_f_0_5m': None, 'is_rectangle': None,
           'n_holes_human': len(obj.get('holes') or []), 'n_holes_model': len(model_holes) if model_obj else None,
           'edited_at_revision': None, 'edited_at': None, 'edited_by': None,
           'review_revision': review.get('revision'), 'review_updated_at': review.get('updated_at'),
           'review_updated_by': review.get('updated_by'),
           'history_length': len(review.get('history') or []) if isinstance(review.get('history'), list) else 0,
           'object_sha256': hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()}
    if human is None or (model_obj is not None and model is None):
        row['geometry'] = 'invalid'
    elif model_obj is None:
        row['geometry'] = 'model_absent'
    elif not verified:
        row['geometry'] = 'model_unverified'
    else:
        try:
            row.update(metrics.outline_pair(human, model, px_m))
            row['boundary_f_0_5m'] = metrics.boundary_f_score(common.polygon_mask(model, width, height),
                                                              common.polygon_mask(human, width, height), px_m)['boundary_f']
        except SKIPPABLE:
            row['geometry'] = 'invalid'
            row.update(dict.fromkeys(metrics.OUTLINE_PAIR_KEYS))
    if row['hausdorff_m'] is not None:
        row['exceeds_vertex_tolerance'] = bool(row['hausdorff_m'] > metrics.VERTEX_TOLERANCE_M)
    if human is not None:
        row['is_rectangle'] = bool(len(human.exterior.coords) - 1 == 4
                                   and common.rectilinearity(human) >= RECTANGLE_RECTILINEARITY)
    if edit_kind != 'none':
        row['edited_at_revision'], row['edited_at'], row['edited_by'] = first_revision_with_outline(review, obj)
    return row


def scene_rows(entry, counters):
    review = entry['review']
    if not review or not isinstance(review.get('objects'), list):
        return []
    counters['n_scenes_with_review'] += 1
    model_objects, pair = pair_model(entry)
    if model_objects is None:
        counters['n_scenes_frame_mismatch'] += 1
        return []
    rows = []
    for obj in review['objects']:
        if not isinstance(obj, dict) or obj.get('kind') != 'building' or not obj.get('id'):
            continue
        status = obj.get('status')
        if status not in ROW_STATUSES:
            counters[f'excluded:{status}'] += 1
            continue
        model_obj = model_objects.get(obj['id'])
        try:
            rows.append(object_row(entry, obj, model_obj, pair if model_obj is not None else 'manual'))
        except SKIPPABLE:
            counters['excluded:row_error'] += 1
    return rows


def build(root='.', scene_ids=None):
    """(rows, counters) over every scene with latest.json (or the given ids); qa_* scenes are skipped."""
    root, counters, rows = Path(root), Counter(), []
    for scene_id in export_dataset.list_scene_ids(root, scene_ids):
        entry = export_dataset.load_scene(root, scene_id)
        if entry is None:
            continue
        counters['n_scenes_seen'] += 1
        rows.extend(scene_rows(entry, counters))
    return rows, counters


def _median(values):
    values = [v for v in values if v is not None and np.isfinite(v)]
    return float(np.median(values)) if values else None


def summarize(rows, counters):
    edited = [r for r in rows if r['edited'] and r['pair'] in ('snapshot', 'latest') and r['tier'] in registry.LABEL_TIERS]
    paired = [r for r in edited if r['geometry'] == 'ok']
    worst = sorted((r for r in paired if r['hausdorff_m'] is not None), key=lambda r: -r['hausdorff_m'])[:WORST_N]
    return {'note': NOTE, 'created_at': common.now_iso(), 'code_sha256': common.code_sha256(),
            'vertex_tolerance_m': metrics.VERTEX_TOLERANCE_M,
            'n_scenes_seen': counters.get('n_scenes_seen', 0), 'n_scenes_with_review': counters.get('n_scenes_with_review', 0),
            'n_scenes_frame_mismatch': counters.get('n_scenes_frame_mismatch', 0),
            'excluded': {k.split(':', 1)[1]: v for k, v in sorted(counters.items()) if k.startswith('excluded:')},
            'n_rows': len(rows),
            'by_tier': dict(sorted(Counter(str(r['tier']) for r in rows).items())),
            'by_registry_provenance': dict(sorted(Counter(r['registry_provenance'] for r in rows).items())),
            'by_pair': dict(sorted(Counter(r['pair'] for r in rows).items())),
            'by_edit_kind': dict(sorted(Counter(r['edit_kind'] for r in rows).items())),
            'n_edited': sum(r['edited'] for r in rows), 'n_confirmed_as_drawn': sum(not r['edited'] for r in rows),
            'n_ring_changed': sum(r['ring_changed'] for r in rows),
            'n_holes_only_changed': sum(r['edit_kind'] == 'holes_only' for r in rows),
            'n_rectangle_after_edit': sum(bool(r['is_rectangle']) and r['edited'] for r in rows),
            'n_machine_label_rows': sum(r['authority_blocker'] == 'machine_label' for r in rows),
            'edited_pairs': {'n': len(paired), 'iou_median': _median(r['iou'] for r in paired),
                             'hausdorff_m_median': _median(r['hausdorff_m'] for r in paired),
                             'human_vertex_to_model_median_m_median': _median(r['human_vertex_to_model_median_m'] for r in paired),
                             'boundary_f_0_5m_median': _median(r['boundary_f_0_5m'] for r in paired),
                             'vertex_delta_median': _median(r['vertex_delta'] for r in paired),
                             'n_hausdorff_ge_0_45_m': sum(r['hausdorff_m'] >= metrics.VERTEX_TOLERANCE_M for r in paired
                                                          if r['hausdorff_m'] is not None),
                             'n_iou_lt_0_9': sum(r['iou'] < .9 for r in paired if r['iou'] is not None)},
            'worst_by_hausdorff': [{k: r[k] for k in ('scene_id', 'object_id', 'tier', 'hausdorff_m', 'iou')} for r in worst]}


def write(out, rows, summary):
    """rows.jsonl (temp file then os.replace) and summary.json under `out`; returns the summary with file hashes."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / 'rows.jsonl'
    handle, temporary = tempfile.mkstemp(dir=out, prefix='rows', suffix='.tmp')
    with os.fdopen(handle, 'w') as fh:
        fh.write(''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows))
    os.replace(temporary, rows_path)
    summary = {**summary, 'out': str(out), 'files': {'rows.jsonl': common.sha256_file(rows_path)}}
    common.write_json_atomic(out / 'summary.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    parser.add_argument('--scene-ids', nargs='*', help='Restrict to these scene ids.')
    parser.add_argument('--summary', action='store_true', help='Print the summary JSON.')
    args = parser.parse_args(argv)
    rows, counters = build(args.root, args.scene_ids)
    summary = write(Path(args.root) / args.out if not Path(args.out).is_absolute() else args.out, rows, summarize(rows, counters))
    if args.summary:
        json.dump(summary, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
