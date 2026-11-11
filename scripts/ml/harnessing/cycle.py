"""The self-improving outer loop: refine on train, consolidate with train feedback, gate on validation.

    1. run          Propose–Probe–Compose per TRAIN case from the lineage head (answer-free)
    2. consolidate  one general edit from the adopted harnesses, lessons and train-split feedback
    3. gate         candidate vs head on VALIDATION cases, repeated; conservative rule (gate.py)
    4. admit        a passing candidate becomes the next lineage version; promote.py exports its skill diff

Test cases are never used here. A spend budget is checked before each phase.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from . import consolidate, evaluate, gate, promote, schedule, surface
from .lineage import Lineage


def spend(runs_root):
    """Dollars recorded so far under a runs root: rollouts plus improver sessions."""
    root = Path(runs_root)
    rollouts = sum(json.loads(p.read_text()).get('cost_usd') or 0.0 for p in root.rglob('rollouts/*/meta.json'))
    sessions = sum(json.loads(p.read_text()).get('cost_usd') or 0.0 for p in (root/'audit').rglob('*.meta.json'))
    return round(rollouts + sessions, 4)


class BudgetExceeded(RuntimeError):
    pass


def labelled(adapter, tasks):
    """Only cases with a held-out answer can feed consolidation or the gate."""
    return [task for task in tasks if adapter.heldout(task) is not None]


def answer_class(adapter, task):
    """The held-out answer class a gate sample must cover: the human decision, or edited/unchanged outlines."""
    heldout = adapter.heldout(task) or {}
    if heldout.get('change'):
        return heldout['change'] + ('' if heldout.get('boundary_reference', True) else ' (area-only reference)')
    return heldout.get('human_decision') or 'detect'


def stratified(adapter, tasks, n):
    """Up to n labelled tasks, round-robin across answer classes so minority answers are always represented.

    A sample drawn in index order can hold a single answer class, and a gate on it cannot tell a better
    criterion from a shifted bias (e.g. approving more)."""
    groups = {}
    for task in labelled(adapter, tasks):
        groups.setdefault(answer_class(adapter, task), []).append(task)
    queues, out = [list(group) for _cls, group in sorted(groups.items())], []
    while queues and len(out) < n:
        for queue in list(queues):
            if len(out) < n:
                out.append(queue.pop(0))
            if not queue:
                queues.remove(queue)
    return out


def run_cycle(adapter, improver, *, runs_root, domain, k=1, train_cases=4, val_cases=8, repeats=2, min_cases=6,
              budget_usd=None, workers=2, inner='harness2', log=print):
    """`inner='harness2'` refines each train case with Propose–Probe–Compose (rich lessons, costly);
    `inner='rollouts'` runs the head once per train case (cheap) so consolidation learns from feedback alone."""
    if inner not in ('harness2', 'rollouts'):
        raise ValueError(f'inner must be harness2 or rollouts, got {inner!r}')
    runs_root = Path(runs_root)
    lineage = Lineage(runs_root)
    version, base_dir = lineage.head() or lineage.seed(adapter.base_harness())
    if Path(adapter.base_harness()).resolve() != Path(base_dir).resolve():
        raise ValueError(f'run the cycle from the lineage head v{version}: pass --base head')
    tag = f'v{version}'
    ns = schedule.Namespace(adapter.name, adapter.executor, 'agg', k, None, tag)
    started = spend(runs_root)
    record = {'domain': domain, 'from_version': version, 'namespace': str(ns), 'started_at': datetime.now(UTC).isoformat(),
              'spend_at_start': started, 'phases': {}}
    path = runs_root/'cycles'/f'{tag}-{domain}.json'
    path.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint(phase, payload, *, enforce=True):
        record['phases'][phase] = payload
        record['spent'] = round(spend(runs_root) - started, 4)
        path.write_text(json.dumps(record, indent=2, default=str))
        if enforce and budget_usd is not None and record['spent'] > budget_usd:
            raise BudgetExceeded(f'spent ${record["spent"]} > budget ${budget_usd} after {phase}; stopping')

    train = stratified(adapter, schedule.task_stream(adapter, split='train', only_domains=[domain]).get(domain, []),
                       train_cases)
    val = stratified(adapter, schedule.task_stream(adapter, split='val', only_domains=[domain]).get(domain, []),
                     val_cases)
    if not train or len(val) < min_cases:
        raise ValueError(f'{domain}: {len(train)} train and {len(val)} val cases; need train and >= {min_cases} val')
    log(f'cycle {tag} {domain}: {inner} on {len(train)} train cases, gate on {len(val)} val cases x {repeats} repeats')
    if inner == 'harness2':
        sweep = schedule.run_sweep(k, adapter=adapter, improver=improver, runs_root=runs_root, split='train',
                                   modes=('agg',), tag=tag, only_tasks=train)
        checkpoint('run', {'tasks': train, 'domains': sweep['domains']})
        train_runs = consolidate.train_runs_for(adapter, runs_root, ns, domain)
    else:
        rows = evaluate.run_eval(adapter, surface.read_tree(base_dir), surface.read_tree(base_dir), train,
                                 label=f'{tag}-{domain}-train', repeats=1, workers=workers, only=('baseline',))
        checkpoint('run', {'tasks': train, 'graded': sum(1 for r in rows if r.get('graded'))})
        train_runs = {r['task_id']: {'base run': Path(r['run_dir'])} for r in rows if r.get('run_dir')}
    forbidden = adapter.split_identities(('val', 'test'))
    candidate, notes, error = consolidate.consolidate(
        adapter, improver, domain=domain, base_dir=base_dir, train_runs=train_runs,
        library_dir=consolidate.library_dir_for(runs_root, adapter, ns, domain),
        out_dir=runs_root/'consolidated'/f'{tag}-{domain}', forbidden=forbidden, label=f'{tag}-{domain}')
    base_tree = surface.read_tree(base_dir)
    checkpoint('consolidate', {'train_cases': sorted(train_runs), 'error': error, 'notes': notes,
                               'diff': surface.diff_summary(base_tree, candidate) if candidate else None})
    if candidate is None or surface.unchanged(base_tree, candidate):
        record['outcome'] = 'no candidate' if candidate is None else 'KEEP_BASE'
        path.write_text(json.dumps(record, indent=2, default=str))
        return record

    result = gate.run_gate(adapter, candidate, base_tree, val, label=f'{tag}-{domain}-gate', repeats=repeats,
                           workers=workers, min_cases=min_cases)
    log(gate.render(result))
    admitted = lineage.admit(candidate, source=f'consolidated {tag} {domain}', gate=result)
    payload = {'passed': result['passed'], 'checks': result['checks'], 'paired': result['paired'],
               'noise_floor': result['noise_floor']}
    if admitted:
        changes = promote.skill_changes(candidate)
        patch = runs_root/'cycles'/f'v{admitted[0]}-{domain}.patch'
        patch.write_text(promote.patch(changes))
        payload.update(admitted_version=admitted[0], promote_patch=str(patch))
    record['outcome'] = f'admitted v{admitted[0]}' if admitted else 'rejected by the gate'
    checkpoint('gate', payload, enforce=False)
    return record
