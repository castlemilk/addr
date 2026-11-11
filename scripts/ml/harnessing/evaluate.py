"""Held-out comparison of two harnesses: rollouts only, paired per case, judged after the fact.

Per-case adaptation is what the loop adopts; what transfers is the harness without its case facts.
`run_eval` replays a candidate harness (an adopted tree, case facts stripped) and a baseline
(default: the base snapshot) on the same cases and repeats, then reports paired deltas.
"""
from __future__ import annotations

import json
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..training.metrics import paired_delta
from . import surface
from .channels import ServiceUnavailable

KEYS = {'judge_iou': 'iou', 'judge_boundary_f': 'boundary_f', 'judge_hausdorff_m': 'hausdorff_m',
        'judge_decision_accuracy': 'iou', 'judge_agrees': 'iou'}


def strip_case_facts(tree):
    """Drop the MEMORY.md case-facts section: an earlier case's facts are wrong anchors elsewhere."""
    memory = tree.get('MEMORY.md', '')
    pattern = re.escape(surface.CASE_FACTS) + r'.*?(?=^## |\Z)'
    return {**tree, 'MEMORY.md': re.sub(pattern, '', memory, flags=re.S | re.M).strip() + '\n' if memory else ''}


def stage(tree, dest):
    surface.validate(tree)
    return surface.write_tree(tree, dest)


def run_eval(adapter, candidate, baseline, tasks, *, label, repeats=1, workers=2, only=('candidate', 'baseline')):
    """Roll both harnesses on every case and repeat; return judged rows (idempotent by run id)."""
    root = Path(adapter.runs_root)/f'eval-{label}'
    dirs = {'candidate': stage(candidate, root/'harnesses'/'candidate'),
            'baseline': stage(baseline, root/'harnesses'/'baseline')}
    jobs = [(task, name, r) for task in tasks for name in dirs if name in only for r in range(1, repeats + 1)]

    def one(job):
        task, name, r = job
        row = {'task_id': task, 'domain': adapter.domain_of(task), 'harness': name, 'repeat': r}
        try:
            run_dir = adapter.execute(task, dirs[name], f'eval-{label}/{task}/{name}-r{r}')
            scored = adapter.score(run_dir, task)
        except ServiceUnavailable:
            raise
        except Exception as exc:
            return {**row, 'graded': False, 'error': f'{type(exc).__name__}: {exc}'}
        meta = json.loads((run_dir/'meta.json').read_text()) if (run_dir/'meta.json').is_file() else {}
        row['cost_usd'], row['run_dir'] = meta.get('cost_usd'), str(run_dir)
        delivered = adapter.delivered(run_dir)
        print(f'  [eval {label}] {task} {name}-r{r}: {"submitted" if delivered else "NO SUBMISSION"}'
              f'{"" if scored or not delivered else " (no held-out answer: unscored)"}', flush=True)
        return {**row, **(scored or {}), 'graded': bool(scored)}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, job) for job in jobs]
        try:
            rows = [future.result() for future in futures]
        except ServiceUnavailable:
            pool.shutdown(wait=True, cancel_futures=True)
            raise
    (root/'eval.json').write_text(json.dumps(rows, indent=2, default=str))
    return rows


def summarise(rows):
    """Means per harness and paired candidate-minus-baseline deltas over per-case repeat means."""
    def mean(values):
        values = [v for v in values if isinstance(v, int | float)]
        return round(statistics.fmean(values), 4) if values else None

    graded = [r for r in rows if r.get('graded')]
    out = {'n_rows': len(rows), 'n_graded': len(graded), 'harness': {}, 'paired': {},
           'cost_usd': round(sum(r.get('cost_usd') or 0 for r in rows), 4)}
    for name in ('candidate', 'baseline'):
        group = [r for r in graded if r['harness'] == name]
        out['harness'][name] = {'n': len(group), **{k: mean(r.get(k) for r in group) for k in KEYS},
                                'outcomes': {o: sum(1 for r in group if r.get('outcome') == o)
                                             for o in ('improved', 'neutral', 'regressed')}}
    per_case = {}
    for row in graded:
        per_case.setdefault(row['task_id'], {}).setdefault(row['harness'], []).append(row)
    pairs = [(sides['candidate'], sides['baseline']) for sides in per_case.values() if len(sides) == 2]
    for key, alias in KEYS.items():
        usable = [({alias: mean(r.get(key) for r in a)}, {alias: mean(r.get(key) for r in b)}) for a, b in pairs]
        usable = [(a, b) for a, b in usable if a[alias] is not None and b[alias] is not None]
        if usable:
            out['paired'][key] = paired_delta([a for a, _ in usable], [b for _, b in usable], key=alias)
    return out


def render(label, summary):
    lines = [f'eval-{label}: {summary["n_graded"]}/{summary["n_rows"]} graded rollouts, ${summary["cost_usd"]}']
    for name, block in summary['harness'].items():
        figures = ' | '.join(f'{k} {v}' for k, v in block.items() if k not in ('n', 'outcomes') and v is not None)
        lines.append(f'  {name:<9} n={block["n"]} {figures} | {block["outcomes"]}')
    for key, delta in summary['paired'].items():
        lines.append(f'  candidate - baseline {key}: median Δ {delta["median_delta"]} CI95 {delta["ci95"]} '
                     f'(better {delta.get("n_improved")}, worse {delta.get("n_worse")}, n={delta["n"]})')
    return '\n'.join(lines)
