"""The validation gate: a candidate base replaces its parent only on clear, repeated, held-out evidence.

Both harnesses are rolled on the same validation cases, `repeats` times each. Per case the repeats are
averaged, then paired. The baseline's own repeats give the noise floor: how much one harness disagrees
with itself. The rule is deliberately conservative; it must reject a candidate that only reshuffles noise.
"""
from __future__ import annotations

import statistics

from . import evaluate, surface

PRIMARY = {'outline': ('judge_boundary_f', 'judge_iou'), 'review': ('judge_agrees', None),
           'detect': ('judge_decision_accuracy', None)}
# Agreement-type outcomes are near-binary per case: the median paired delta is almost always 0, so these
# use the mean delta and a sign margin of two cases instead.
BINARY = frozenset({'review', 'detect'})


CLASS_TOLERANCE = {'outline': 0.03, 'review': 0.25, 'detect': 0.25}
SEVERE = {'approved', 'rejected'}


def row_class(row):
    return row.get('human_decision') or row.get('change') or 'all'


def per_case_deltas(rows, key):
    """Candidate-minus-baseline per case, each side averaged over its repeats."""
    return list(class_deltas(rows, key).values())


def class_deltas(rows, key):
    """task -> candidate-minus-baseline delta (repeat means), for tasks both harnesses completed."""
    sides = {}
    for row in rows:
        if row.get('graded') and row.get(key) is not None:
            sides.setdefault(row['task_id'], {}).setdefault(row['harness'], []).append(row[key])
    return {task: statistics.fmean(s['candidate']) - statistics.fmean(s['baseline'])
            for task, s in sides.items() if s.get('candidate') and s.get('baseline')}


def by_class(rows, key):
    """answer class -> list of per-case deltas."""
    cls = {row['task_id']: row_class(row) for row in rows if row.get('graded')}
    out = {}
    for task, delta in class_deltas(rows, key).items():
        out.setdefault(cls[task], []).append(delta)
    return out


def noise_floor(rows, key, *, binary=False):
    """Per-case spread of the baseline's repeats on `key`: median, or mean for binary outcomes (None without repeats)."""
    by_case = {}
    for row in rows:
        if row.get('graded') and row['harness'] == 'baseline' and row.get(key) is not None:
            by_case.setdefault(row['task_id'], []).append(row[key])
    spreads = [max(v) - min(v) for v in by_case.values() if len(v) > 1]
    if not spreads:
        return None
    return round(statistics.fmean(spreads) if binary else statistics.median(spreads), 4)


def decide(rows, summary, task, *, min_cases=6, harm=0.01):
    """The gate decision with every condition spelled out, so a reader can audit it."""
    primary, secondary = PRIMARY[task]
    binary = task in BINARY
    noise = noise_floor(rows, primary, binary=binary)
    deltas = per_case_deltas(rows, primary)
    classes = by_class(rows, primary)
    if binary:  # balanced: every human answer class weighs the same, so a shifted bias cannot pass
        stat_name = 'class-balanced mean'
        gain = statistics.fmean(statistics.fmean(v) for v in classes.values()) if classes else 0.0
    else:
        stat_name = 'median'
        gain = statistics.median(deltas) if deltas else 0.0
    better, worse = sum(d > 0 for d in deltas), sum(d < 0 for d in deltas)
    tolerance = CLASS_TOLERANCE.get(task, 0.25)
    class_means = {cls: round(statistics.fmean(v), 4) for cls, v in classes.items()}
    losing = {cls: m for cls, m in class_means.items() if len(classes[cls]) >= 2 and m < -tolerance}
    delivered = {name: sum(1 for r in rows if r['harness'] == name and r.get('graded')) for name in ('candidate', 'baseline')}
    checks = {
        f'at least {min_cases} paired cases': len(deltas) >= min_cases,
        'candidate submits as often as baseline': delivered['candidate'] >= delivered['baseline'],
        f'{stat_name} {primary} gain is positive ({round(gain, 4)})': gain > 0,
        ('at least two more cases improve than worsen' if binary else 'more cases improve than worsen')
        + f' ({better} vs {worse})': better >= worse + (2 if binary else 1),
        f'{stat_name} gain exceeds half the noise floor ({noise})': noise is None or gain > noise/2,
    }
    if binary:
        checks[f'the sample covers at least two human answers ({sorted(classes)})'] = len(classes) >= 2
    if task == 'review':  # approving what the reviewer rejected (or the reverse) is the costliest miss
        severe = {name: sum(1 for r in rows if r.get('graded') and r['harness'] == name
                            and {r.get('human_decision'), r.get('agent_decision')} == SEVERE)
                  for name in ('candidate', 'baseline')}
        checks[f'no more opposite verdicts, approved vs rejected ({severe["candidate"]} vs {severe["baseline"]})'] = \
            severe['candidate'] <= severe['baseline']
    checks[f'no answer class loses more than {tolerance} ({class_means})'] = not losing
    if secondary:
        second = summary['paired'].get(secondary) or {}
        checks[f'no harm on {secondary} (median >= -{harm})'] = (second.get('median_delta') or 0) >= -harm
    return {'passed': all(checks.values()), 'checks': checks, 'primary': primary, 'noise_floor': noise,
            'statistic': stat_name, 'gain': round(gain, 4), 'class_deltas': class_means, 'paired': summary['paired'],
            'delivered': delivered}


def run_gate(adapter, candidate, baseline, tasks, *, label, repeats=2, workers=2, min_cases=6):
    if repeats < 2:
        raise ValueError('the gate needs repeats >= 2 to measure its own noise floor')
    task_kinds = {adapter.index[t]['task'] for t in tasks}
    if len(task_kinds) != 1:
        raise ValueError(f'gate one task kind at a time, got {sorted(task_kinds)}')
    rows = evaluate.run_eval(adapter, candidate, baseline, tasks, label=label, repeats=repeats, workers=workers)
    summary = evaluate.summarise(rows)
    return {**decide(rows, summary, task_kinds.pop(), min_cases=min_cases), 'label': label, 'repeats': repeats,
            'tasks': list(tasks), 'cost_usd': summary['cost_usd'], 'harness': summary['harness'],
            'baseline_fingerprint': surface.tree_fingerprint(baseline),
            'candidate_fingerprint': surface.tree_fingerprint(candidate)}


def render(result):
    lines = [f'gate {result["label"]}: {"PASSED" if result["passed"] else "REJECTED"} '
             f'({len(result["tasks"])} cases x {result["repeats"]} repeats, ${result["cost_usd"]})']
    lines += [f'  [{"x" if ok else " "}] {name}' for name, ok in result['checks'].items()]
    for key, delta in result['paired'].items():
        lines.append(f'  {key}: median Δ {delta["median_delta"]} CI95 {delta["ci95"]} '
                     f'(better {delta.get("n_improved")}, worse {delta.get("n_worse")}, n={delta["n"]})')
    return '\n'.join(lines)
