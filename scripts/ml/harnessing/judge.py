"""Measurement of the outline or detection review that selection chose, after it chose it.

Nothing in the method imports this module. Outline rows compare the submission and the machine
start outline with the held-out human outline (IoU, boundary F within 0.5 m, Hausdorff, vertex
distances from training.metrics); detection rows compare decisions with the reviewer's calls.
A failed judge is a row with an error, never a zero, and is excluded from every mean.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import Polygon

from ..training.metrics import boundary_f_score, outline_pair
from .schedule import Namespace

TOLERANCE_M = .5
IOU_BAND, BF_BAND = .01, .02


def polygon_of(outline):
    polygon = Polygon(outline['ring'], outline.get('holes') or [])
    return polygon if polygon.is_valid else polygon.buffer(0)


def _mask(polygon, box):
    image = Image.new('1', (box[2] - box[0], box[3] - box[1]), 0)
    draw = ImageDraw.Draw(image)
    for part in getattr(polygon, 'geoms', [polygon]):
        draw.polygon([(x - box[0], y - box[1]) for x, y in part.exterior.coords], fill=1)
        for hole in part.interiors:
            draw.polygon([(x - box[0], y - box[1]) for x, y in hole.coords], fill=0)
    return np.array(image, dtype=bool)


def outline_metrics(reference, candidate, pixel_size_m, tolerance_m=TOLERANCE_M):
    """Agreement of `candidate` with `reference`, both in source pixels; distances in metres."""
    pair = outline_pair(reference, candidate, pixel_size_m)
    left, top, right, bottom = reference.union(candidate).bounds
    box = [int(left) - 4, int(top) - 4, int(right) + 5, int(bottom) + 5]
    boundary = boundary_f_score(_mask(candidate, box), _mask(reference, box), pixel_size_m, tolerance_m)
    return {'iou': pair['iou'], 'boundary_f': boundary['boundary_f'],
            'boundary_precision': boundary['boundary_precision'], 'boundary_recall': boundary['boundary_recall'],
            'hausdorff_m': pair['hausdorff_m'], 'vertex_to_reference_median_m': pair['model_vertex_to_human_median_m'],
            'reference_vertex_within_0_45_m': pair['human_vertex_within_0_45_m'],
            'area_delta_m2': pair['area_delta_m2'], 'n_vertices': pair['n_model_vertices']}


def _delta(after, before, *, lower_is_better=False):
    if after is None or before is None:
        return None
    return round((before - after) if lower_is_better else (after - before), 6)


def outcome(delta_iou, delta_bf):
    """improved | neutral | regressed against the machine start, by IoU and boundary F bands (IoU alone when
    the reference is not a boundary trace)."""
    if delta_iou is None:
        return 'unscored'
    if delta_bf is None:
        return 'improved' if delta_iou >= IOU_BAND else 'regressed' if delta_iou <= -IOU_BAND else 'neutral'
    if (delta_bf >= BF_BAND and delta_iou > -IOU_BAND/2) or (delta_iou >= IOU_BAND and delta_bf > -BF_BAND/4):
        return 'improved'
    if delta_bf <= -BF_BAND or delta_iou <= -IOU_BAND:
        return 'regressed'
    return 'neutral'


def score_outline(case, heldout, submission):
    reference = polygon_of(heldout['reference_outline_pixels'])
    submitted = outline_metrics(reference, polygon_of(submission['outline_pixels']), case['pixel_size_m'])
    start = outline_metrics(reference, polygon_of(case['start_outline_pixels']), case['pixel_size_m'])
    row = {'metric': 'outline', 'change': heldout.get('change'), 'kept_start': bool(submission.get('kept_start_outline')),
           **{f'judge_{key}': value for key, value in submitted.items()},
           **{f'start_{key}': value for key, value in start.items()},
           'delta_iou': _delta(submitted['iou'], start['iou']),
           'delta_boundary_f': _delta(submitted['boundary_f'], start['boundary_f']),
           'delta_hausdorff_m': _delta(submitted['hausdorff_m'], start['hausdorff_m'], lower_is_better=True)}
    row.update(reference_origin=heldout.get('reference_origin', 'human'),
               boundary_reference=heldout.get('boundary_reference', True))
    if not row['boundary_reference']:  # an agent-drawn simplification: area agreement only
        for key in [k for k in row if k.endswith(('boundary_f', 'boundary_precision', 'boundary_recall'))]:
            row[key] = None
    row['outcome'] = outcome(row['delta_iou'], row['delta_boundary_f'])
    return row


def _match(additions, references, threshold=.5):
    """Greedy one-to-one matches of added outlines to reviewer-added objects at IoU >= threshold."""
    pairs = []
    for i, added in enumerate(additions):
        for j, reference in enumerate(references):
            a, b = polygon_of(added['outline_pixels']), polygon_of(reference['outline_pixels'])
            union = a.union(b).area
            if union and a.intersection(b).area/union >= threshold:
                pairs.append((a.intersection(b).area/union, i, j))
    used_i, used_j = set(), set()
    for _iou, i, j in sorted(pairs, reverse=True):
        if i not in used_i and j not in used_j:
            used_i.add(i)
            used_j.add(j)
    return len(used_i)


def score_detect(case, heldout, submission):
    expected = heldout['decisions']
    predicted = {row['object_id']: row['decision'] if row['decision'] != 'relabel' else f'relabel:{row["kind"]}'
                 for row in submission.get('decisions', [])}
    ids = sorted(expected)
    accuracy = sum(predicted.get(i) == expected[i] for i in ids)/len(ids)
    keep_all = sum(expected[i] == 'keep' for i in ids)/len(ids)
    rejects = {i for i in ids if expected[i] == 'reject'}
    flagged = {i for i in ids if predicted.get(i) == 'reject'}
    matched = _match(submission.get('additions', []), heldout.get('added', []))
    delta = round(accuracy - keep_all, 6)
    return {'metric': 'detect', 'judge_decision_accuracy': accuracy, 'keep_all_accuracy': keep_all,
            'delta_accuracy': delta, 'n_detections': len(ids),
            'reject_precision': len(rejects & flagged)/len(flagged) if flagged else None,
            'reject_recall': len(rejects & flagged)/len(rejects) if rejects else None,
            'addition_precision': matched/len(submission['additions']) if submission.get('additions') else None,
            'addition_recall': matched/len(heldout['added']) if heldout.get('added') else None,
            'outcome': 'improved' if delta > 0 else 'regressed' if delta < 0 else 'neutral'}


def score_review(case, heldout, submission):
    """Agreement with the human word; `uncertain` abstains. The Codex review on the item is a comparison only."""
    human, agent, codex = heldout['human_decision'], submission['decision'], heldout.get('codex_decision')
    decided = agent != 'uncertain'
    return {'metric': 'review', 'family': case.get('family'), 'human_decision': human, 'agent_decision': agent,
            'judge_agrees': float(agent == human), 'judge_decided': float(decided),
            'judge_agrees_decided': float(agent == human) if decided else None,
            'codex_decision': codex, 'codex_agrees': None if codex is None else float(codex == human),
            'outcome': 'agree' if agent == human else 'abstain' if not decided else 'disagree'}


def _summaries(runs_root, ns):
    root = Path(runs_root)/str(Namespace(ns.adapter, ns.executor, 'agg', ns.k, ns.seed, ns.tag))
    for path in sorted(root.glob('*/summary.json')):
        try:
            yield json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue


def _row(adapter, task_id, domain, mode, k, label, run_id, run_dir, extra=None):
    row = {'task_id': task_id, 'domain': domain, 'mode': mode, 'k': k, 'label': label, 'run_id': run_id, **(extra or {})}
    try:
        got = adapter.score(Path(run_dir), task_id) if run_dir else None
    except Exception as exc:
        return {**row, 'graded': False, 'error': f'{type(exc).__name__}: {exc}'}
    if not got:
        return {**row, 'graded': False, 'error': 'judge returned nothing (no submission or no held-out reference)'}
    return {**row, **got, 'graded': True}


def judge(adapter, runs_root, ns, *, bases=False, workers=4):
    """Judge every case's adopted run for `ns` (or its base rollouts) and write the rows."""
    jobs = []
    for summary in _summaries(runs_root, ns):
        task_id, domain = summary['task_id'], summary.get('domain', '')
        if bases:
            jobs += [(task_id, domain, 'base', ns.k, b['label'], b['run_id'], b.get('run_dir'), None)
                     for b in summary.get('base', [])]
            continue
        sel = (summary.get('selections') or {}).get(ns.mode) or {}
        if not sel.get('ok'):
            jobs.append((task_id, domain, ns.mode, ns.k, sel.get('chosen_label', ''), sel.get('chosen_run_id', ''), None,
                         {'selection_error': sel.get('error') or 'selection did not complete'}))
            continue
        jobs.append((task_id, domain, ns.mode, ns.k, sel['chosen_label'], sel['chosen_run_id'], sel['chosen_run_dir'],
                     {'chosen_tree_is_base': sel.get('chosen_tree_is_base')}))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = list(pool.map(lambda job: _row(adapter, *job), jobs))
    path = Path(runs_root)/str(ns)/('base_judged.json' if bases else 'judged.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, default=str), encoding='utf-8')
    return rows, path
