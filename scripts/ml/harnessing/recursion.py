"""Parallel and sequential task-level recursion (Propose–Probe–Compose).

Each mode schedules 4k rollouts and 3k improvement stages, counting the k shared base rollouts.
An invalid proposal gets one retry with the validator's message, then inherits the current
harness. Adapted from Harness² core/recursion.py.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import context, prompts, surface
from .channels import ImproverError, ServiceUnavailable
from .ledger import Ledger
from .spec import RunView, resolve_prompts

STAGE_ATTEMPTS = 2
TRANSPORT_ATTEMPTS = 2
PHASE_WIDTH = 2


@dataclass
class Stage:
    label: str
    dim: str
    tree: dict
    kept_base: bool = False
    fell_back: bool = False
    error: str = ''
    usd: float = 0.0
    attempts: int = 0
    cached: bool = False
    notes: str = ''

    def to_dict(self):
        return {'label': self.label, 'dim': self.dim, 'fell_back': self.fell_back, 'kept_base': self.kept_base,
                'attempts': self.attempts, 'usd': self.usd, 'cached': self.cached, 'notes': self.notes,
                'error': self.error[:400]}


@dataclass
class ModeResult:
    mode: str
    task_id: str
    k: int
    stages: list = field(default_factory=list)
    trees: dict = field(default_factory=dict)
    rollouts: dict = field(default_factory=dict)
    finals: list = field(default_factory=list)
    ledger: Ledger | None = None
    similarity: dict | None = None
    errors: list = field(default_factory=list)

    @property
    def ok(self):
        return bool(self.finals) and not self.errors

    def to_dict(self):
        return {'mode': self.mode, 'task_id': self.task_id, 'k': self.k, 'finals': self.finals, 'ok': self.ok,
                'fell_back': [s.label for s in self.stages if s.fell_back],
                'kept_base': [s.label for s in self.stages if s.kept_base],
                'stages': [s.to_dict() for s in self.stages], 'similarity': self.similarity,
                'ledger': self.ledger.to_dict() if self.ledger else None, 'errors': self.errors}


def accept_tree(adapter, new, fallback, label):
    """Repair then validate a proposed tree: (kept_base, '') when accepted, else the error."""
    fixed = surface.autotrigger(new)
    if fixed:
        print(f'  [{label}] auto-triggered skill(s): {", ".join(fixed)}', flush=True)
    kept = surface.unchanged(fallback, new)
    try:
        surface.validate(new, allow_subagents=adapter.allow_subagents, allow_hooks=adapter.allow_hooks)
        adapter.validate_tree(new)
        context.assert_tree_has_no_verdicts(new)
    except ValueError as exc:
        return kept, str(exc)
    return kept, ''


def run_stage(*, label, dim, job, fallback, adapter, improver, ctx_dir, task_id, rollouts, harness_dir,
              library_dir, pool=(), decisions=None):
    """Build evidence, propose an edit, and retry an invalid proposal once."""
    try:
        tree = context.build(ctx_dir/label, adapter, task_id, rollouts, harness_dir=harness_dir,
                             library_dir=library_dir, pool=pool, decisions=decisions)
        mission = context.mission(tree, job=job)
    except Exception as exc:
        return Stage(label=label, dim=dim, tree=dict(fallback), fell_back=True,
                     error=f'could not build evidence: {type(exc).__name__}: {exc}'[:400])
    error, usd, calls, cached = '', 0.0, 0, True
    for attempt in range(1, STAGE_ATTEMPTS + 1):
        retry = (f'\n\n# YOUR PREVIOUS ATTEMPT WAS REJECTED\n\n{error}\n\nFix it in `context/harness/` and '
                 'reply again.' if error else '')
        try:
            reply = improver.call(mission + retry, tree.root, f'{label}.a{attempt}', harvest='context/harness',
                                  max_attempts=TRANSPORT_ATTEMPTS)
        except ImproverError as exc:
            error, usd, calls, cached = str(exc), usd + exc.cost_usd, calls + (exc.attempts or 1), False
            break
        usd += reply.cost_usd
        calls += 0 if reply.cached else max(1, reply.attempts)
        cached = cached and reply.cached
        if reply.workspace is None:
            error = 'the harness directory did not come back from the session; edit the files in `context/harness/` in place'
            continue
        rejected = []
        new = surface.read_tree(reply.workspace, rejected=rejected)
        if not new:
            error = '`context/harness/` came back empty -- edit the files, do not delete them all'
            continue
        if rejected:
            error = ('these files are off the editable surface and were not applied: '
                     + ', '.join(sorted(rejected)[:6]) + '. Component names are lowercase letters, digits, '
                     '`_` or `-` (e.g. `skills/eave_audit/SKILL.md`, `skills/eave_audit/references/shadow.md`, '
                     '`scripts/intersect_edges.py`).')
            continue
        kept, error = accept_tree(adapter, new, fallback, label)
        if error:
            print(f'  [{task_id}] {label} attempt {attempt} rejected: {error[:300]}', flush=True)
            surface.write_tree(new, tree.root/'harness')
            continue
        notes = re.search(r'<notes>(.*?)</notes>', reply.text, re.S)
        change = surface.diff_summary(fallback, new)
        print(f'  [{task_id}] {label}: {"KEEP_BASE" if kept else change} ${usd:.2f}', flush=True)
        return Stage(label=label, dim=dim, tree=new, kept_base=kept, usd=usd, attempts=calls, cached=cached,
                     notes=notes.group(1).strip() if notes else reply.text.strip())
    print(f'  [{task_id}] {label} FELL BACK to the current harness: {error[:300]}', flush=True)
    return Stage(label=label, dim=dim, tree=dict(fallback), fell_back=True, error=error[:400], usd=usd,
                 attempts=calls, cached=cached)


def roll(adapter, ledger, task_id, label, tree, run_id_of, work_dir, *, record=True):
    """Materialise a harness and roll it once. A dead rollout is recorded, never raised."""
    harness_dir = surface.write_tree(tree, Path(work_dir)/'harnesses'/label) if tree is not None else None
    run_id = run_id_of(label)
    try:
        run_dir = Path(adapter.execute(task_id, harness_dir, run_id))
        exists = run_dir.exists()
        view = RunView(label=label, run_id=run_id, run_dir=run_dir, ok=exists,
                       delivered=exists and adapter.delivered(run_dir),
                       error='' if exists else 'adapter returned a missing run directory')
    except ServiceUnavailable:
        raise
    except Exception as exc:
        view = RunView(label=label, run_id=run_id, run_dir=Path(work_dir)/'missing', ok=False, delivered=False,
                       error=f'{type(exc).__name__}: {exc}')
    print(f'  [{task_id}] rollout {label}: {"submitted" if view.delivered else "NO SUBMISSION"}'
          + (f' ({view.error[:200]})' if view.error else ''), flush=True)
    if record:
        ledger.rollout(label)
    return view


def roll_many(adapter, ledger, task_id, items, run_id_of, work_dir, *, width):
    """Roll independent harnesses concurrently, then record them in the caller's order."""
    if not items:
        return {}
    if min(len(items), width) <= 1:
        return {label: roll(adapter, ledger, task_id, label, tree, run_id_of, work_dir) for label, tree in items}
    with ThreadPoolExecutor(max_workers=min(len(items), width)) as pool:
        views = list(pool.map(lambda item: roll(adapter, ledger, task_id, item[0], item[1], run_id_of,
                                                work_dir, record=False), items))
    for label, _tree in items:
        ledger.rollout(label)
    return {label: view for (label, _tree), view in zip(items, views, strict=True)}


def _record(ledger, stages):
    for stage in stages:
        ledger.session(stage.label, stage.usd, cached=stage.cached, attempts=stage.attempts)


def run_parallel(task_id, k, base, *, adapter, improver, work_dir, run_id_of, base_tree, library_dir=None,
                 propose_workers=PHASE_WIDTH, rollout_workers=PHASE_WIDTH):
    """WIDTH: 2k independent candidates from shared evidence, then k independent compositions."""
    work_dir = Path(work_dir)
    ledger = Ledger(mode='agg', task_id=task_id, k=k)
    for run in base:
        ledger.rollout(run.label)
    result = ModeResult(mode='agg', task_id=task_id, k=k, ledger=ledger, rollouts={r.label: r for r in base})
    work = resolve_prompts(adapter, task_id)
    base_dir = surface.write_tree(base_tree, work_dir/'harnesses'/'base')
    common = {'adapter': adapter, 'improver': improver, 'ctx_dir': work_dir/'context', 'task_id': task_id,
              'rollouts': base, 'harness_dir': base_dir, 'library_dir': library_dir, 'fallback': base_tree}
    specs = [(f'{dim.lower()}{i}', dim, i) for i in range(1, k + 1) for dim in ('INV', 'DOM')]
    with ThreadPoolExecutor(max_workers=max(1, min(len(specs), propose_workers))) as pool:
        stages = list(pool.map(lambda s: run_stage(label=s[0], dim=s[1], job=prompts.propose(work, s[1], k=k, index=s[2]),
                                                   **common), specs))
    _record(ledger, stages)
    result.stages += stages
    result.trees.update({s.label: s.tree for s in stages})
    result.rollouts.update(roll_many(adapter, ledger, task_id, [(s.label, s.tree) for s in stages], run_id_of,
                                     work_dir, width=rollout_workers))
    pool_evidence = [(s.label, work_dir/'harnesses'/s.label, result.rollouts.get(s.label)) for s in stages]
    decisions = {s.label: s.notes or s.error for s in stages}
    labels = [f'agg{i}' for i in range(1, k + 1)]
    with ThreadPoolExecutor(max_workers=max(1, min(len(labels), propose_workers))) as pool:
        finals = list(pool.map(lambda label: run_stage(label=label, dim='AGG', job=prompts.aggregate(work, k=k, index=int(label[3:])),
                                                       pool=pool_evidence, decisions=decisions, **common), labels))
    _record(ledger, finals)
    result.stages += finals
    result.trees.update({s.label: s.tree for s in finals})
    result.finals = labels
    result.rollouts.update(roll_many(adapter, ledger, task_id, [(s.label, s.tree) for s in finals], run_id_of,
                                     work_dir, width=rollout_workers))
    result.similarity = surface.diversity({s.label: s.tree for s in finals})
    ledger.assert_identity()
    return result


def run_sequential(task_id, k, base, *, adapter, improver, work_dir, run_id_of, base_tree, library_dir=None,
                   propose_workers=PHASE_WIDTH, rollout_workers=PHASE_WIDTH):
    """DEPTH: k nested passes over the same shared base evidence."""
    work_dir = Path(work_dir)
    ledger = Ledger(mode='seq', task_id=task_id, k=k)
    for run in base:
        ledger.rollout(run.label)
    result = ModeResult(mode='seq', task_id=task_id, k=k, ledger=ledger, rollouts={r.label: r for r in base})
    work = resolve_prompts(adapter, task_id)
    current, history = dict(base_tree), []
    for t in range(1, k + 1):
        current_dir = surface.write_tree(current, work_dir/'harnesses'/(f'c{t - 1}' if t > 1 else 'base'))
        common = {'adapter': adapter, 'improver': improver, 'ctx_dir': work_dir/'context', 'task_id': task_id,
                  'rollouts': base, 'harness_dir': current_dir, 'library_dir': library_dir, 'fallback': current,
                  'decisions': {s.label: s.notes or s.error for s in result.stages}}
        specs = [(f'inv{t}', 'INV', t, list(history), common), (f'dom{t}', 'DOM', t, list(history), common)]
        with ThreadPoolExecutor(max_workers=max(1, min(2, propose_workers))) as pool:
            stages = list(pool.map(lambda s: run_stage(
                label=s[0], dim=s[1], job=prompts.propose(work, s[1], k=k, index=s[2], round_t=s[2] if k > 1 else None),
                pool=s[3], **s[4]), specs))
        _record(ledger, stages)
        result.stages += stages
        result.trees.update({s.label: s.tree for s in stages})
        result.rollouts.update(roll_many(adapter, ledger, task_id, [(s.label, s.tree) for s in stages], run_id_of,
                                         work_dir, width=rollout_workers))
        round_pool = history + [(s.label, work_dir/'harnesses'/s.label, result.rollouts.get(s.label)) for s in stages]
        label = f'c{t}'
        job = prompts.aggregate(work, k=1, index=1) if k == 1 else prompts.compose_round(work, t=t, k=k)
        common['decisions'] = {s.label: s.notes or s.error for s in result.stages}
        composed = run_stage(label=label, dim='COMPOSE', job=job, pool=round_pool, **common)
        _record(ledger, [composed])
        result.stages.append(composed)
        result.trees[label] = composed.tree
        result.rollouts[label] = roll(adapter, ledger, task_id, label, composed.tree, run_id_of, work_dir)
        history = round_pool + [(label, work_dir/'harnesses'/label, result.rollouts[label])]
        current = composed.tree
    result.finals = [f'c{t}' for t in range(1, k + 1)]
    ledger.assert_identity()
    return result


def run_mode(mode, *args, **kwargs):
    if mode == 'agg':
        return run_parallel(*args, **kwargs)
    if mode == 'seq':
        return run_sequential(*args, **kwargs)
    raise ValueError(f"unknown mode {mode!r}; the method has exactly two: 'agg' (parallel) and 'seq' (sequential)")
