"""Namespaces, the seeded case stream, and the drivers: one case, one domain, a sweep.

A namespace is <adapter>-<executor>-<mode><k>[-s<seed>][-<tag>]. Repeating a command resumes it:
a case whose summary covers every requested mode, with intact adopted artifacts, is skipped and
replayed into its libraries. Adapted from Harness² core/schedule.py.
"""
from __future__ import annotations

import fcntl
import json
import os
import random
import re
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import library as library_store
from . import recursion, selection, surface
from .channels import ServiceUnavailable
from .spec import RunView

MODES = ('agg', 'seq')
MODE_NAMES = {'agg': 'parallel', 'seq': 'sequential'}
ALIASES = {'agg': 'agg', 'seq': 'seq', 'parallel': 'agg', 'sequential': 'seq'}


def mode_token(name):
    key = str(name or '').strip().lower()
    if key not in ALIASES:
        raise ValueError(f'unknown mode {name!r}; expected parallel | sequential')
    return ALIASES[key]


def validate_tag(tag):
    if tag and (not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_.\-]*', tag) or re.match(r's\d+(-|$)', tag)):
        raise ValueError(f'tag {tag!r} must be [A-Za-z0-9_.-], must not start with "-" and must not spell a '
                         'seed segment (use --seed)')


@dataclass(frozen=True)
class Namespace:
    adapter: str
    executor: str
    mode: str
    k: int
    seed: int | None = None
    tag: str = ''

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f'unknown mode {self.mode!r}; the method has exactly two: {MODES}')
        if int(self.k) < 1:
            raise ValueError(f'k must be positive, got {self.k!r}')
        validate_tag(self.tag)

    def __str__(self):
        out = f'{self.adapter}-{self.executor}-{self.mode}{int(self.k)}'
        out += f'-s{int(self.seed)}' if self.seed is not None else ''
        return out + (f'-{self.tag}' if self.tag else '')

    def run_id(self, task_id, label):
        return f'{self}/{task_id}/{label}'

    def task_dir(self, root, task_id):
        return Path(root)/str(self)/library_store.slug(task_id)


def library_root(runs_root, adapter, k, *, seed=None, tag=''):
    name = f'{adapter.name}-{adapter.executor}-k{int(k)}' + (f'-s{int(seed)}' if seed is not None else '')
    return Path(runs_root)/'library'/(name + (f'-{tag}' if tag else ''))


def stream_order(tasks_by_domain, seed):
    """The (domain, case) stream. Without a seed: domains by descending size then name, cases sorted.

    A seed shuffles each from its SORTED order, so dropping one domain leaves every other identical.
    """
    domains = sorted(tasks_by_domain, key=lambda d: (-len(tasks_by_domain[d]), d))
    if seed is not None:
        domains = sorted(tasks_by_domain)
        random.Random(f'{seed}:__domains__').shuffle(domains)
    out = []
    for domain in domains:
        tasks = sorted(tasks_by_domain[domain])
        if seed is not None:
            random.Random(f'{seed}:{domain}').shuffle(tasks)
        out.extend((domain, task) for task in tasks)
    return out


def task_stream(adapter, *, split='all', seed=None, only_domains=(), only_tasks=(), max_tasks=None):
    """The ordered case stream per domain, after filters; `max_tasks` caps cases PER DOMAIN."""
    by_domain = defaultdict(list)
    for task in adapter.tasks(split):
        by_domain[adapter.domain_of(task)].append(task)
    if only_domains:
        missing = sorted(set(only_domains) - set(by_domain))
        if missing:
            raise ValueError(f'unknown domain(s) {missing}; split {split!r} has {sorted(by_domain)}')
        by_domain = {d: v for d, v in by_domain.items() if d in set(only_domains)}
    if only_tasks:
        missing = sorted(set(only_tasks) - {t for v in by_domain.values() for t in v})
        if missing:
            raise ValueError(f'unknown case id(s) {missing} for split {split!r}')
    if max_tasks is not None and int(max_tasks) < 1:
        raise ValueError(f'max_tasks must be positive, got {max_tasks!r}')
    ordered = defaultdict(list)
    for domain, task in stream_order(by_domain, seed):
        if (not only_tasks or task in set(only_tasks)) and (max_tasks is None or len(ordered[domain]) < int(max_tasks)):
            ordered[domain].append(task)
    return dict(ordered)


@dataclass
class TaskResult:
    task_id: str
    domain: str
    k: int
    seed: int | None = None
    status: str = 'running'
    base: list = field(default_factory=list)
    modes: dict = field(default_factory=dict)
    selections: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def merged_status(record):
    modes, selections = record.get('modes') or {}, record.get('selections') or {}
    ok = bool(modes) and not record.get('errors') and all(
        (value or {}).get('ok') and (selections.get(mode) or {}).get('ok')
        and (selections.get(mode) or {}).get('chosen_label') for mode, value in modes.items())
    return 'ok' if ok else 'partial'


def write_summary(path, result):
    """Merge `result` into summary.json under an flock; modes may be written by separate processes."""
    lock = path.with_suffix('.lock')
    with open(lock, 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            merged = result.to_dict()
            if path.is_file():
                try:
                    old = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    old = {}
                for key in ('modes', 'selections'):
                    merged[key] = {**(old.get(key) or {}), **(merged.get(key) or {})}
                done = {m for m in result.modes if (result.selections.get(m) or {}).get('ok')}
                merged['errors'] = list(merged['errors']) + [
                    e for e in old.get('errors') or []
                    if e not in merged['errors'] and str(e).partition(':')[0] not in done
                    and e != 'no base rollout produced anything']
                if merged['status'] in ('ok', 'partial'):
                    merged['status'] = merged_status(merged)
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(merged, indent=2, default=str))
            os.replace(tmp, path)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _restore_libraries(result, ns_of, runs_root, libraries):
    """Replay a skipped case into its libraries from the saved adopted harness."""
    for mode, lib in libraries.items():
        sel = result.selections.get(mode) or {}
        if not sel.get('ok') or not sel.get('chosen_label') or not library_store.has_experience(sel.get('experience')):
            continue
        tree = surface.read_tree(ns_of(mode).task_dir(runs_root, result.task_id)/'harnesses'/sel['chosen_label'])
        if tree:
            lib.add(result.task_id, tree, why=sel.get('why') or '', label=sel['chosen_label'],
                    run_id=sel.get('chosen_run_id') or '', experience=sel['experience'])


def _resume(path, result, modes, adapter, ns_of, runs_root, libraries):
    try:
        got = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f'  [{result.task_id}] unreadable summary.json ({exc}); re-running', flush=True)
        return False
    if got.get('status') != 'ok':
        return False
    for mode in modes:
        sel = (got.get('selections') or {}).get(mode) or {}
        if not ((got.get('modes') or {}).get(mode) or {}).get('ok') or not sel.get('ok') or not sel.get('chosen_label'):
            return False
        run_dir = Path(sel.get('chosen_run_dir') or '')
        harness_dir = ns_of(mode).task_dir(runs_root, result.task_id)/'harnesses'/sel['chosen_label']
        if not (run_dir.is_dir() and adapter.delivered(run_dir) and surface.is_tree(harness_dir)) or (
                sel.get('harness_fingerprint') and surface.fingerprint(harness_dir) != sel['harness_fingerprint']):
            print(f'  [{result.task_id}] cannot resume {mode}: adopted artifacts changed; re-running', flush=True)
            return False
    result.status, result.base, result.errors = 'skipped', got.get('base', []), got.get('errors', [])
    result.modes, result.selections = got.get('modes') or {}, got.get('selections') or {}
    _restore_libraries(result, ns_of, runs_root, libraries)
    return True


def _roll_bases(task_id, adapter, pending, dead_dir, width):
    def one(item):
        label, run_id = item
        try:
            run_dir = Path(adapter.execute(task_id, None, run_id))
            return RunView(label=label, run_id=run_id, run_dir=run_dir, ok=run_dir.exists(),
                           delivered=run_dir.exists() and adapter.delivered(run_dir))
        except ServiceUnavailable:
            raise
        except Exception as exc:
            return RunView(label=label, run_id=run_id, run_dir=dead_dir, ok=False, delivered=False,
                           error=f'{type(exc).__name__}: {exc}')
    with ThreadPoolExecutor(max_workers=max(1, min(len(pending), width))) as pool:
        views = list(pool.map(one, pending))
    for view in views:
        print(f'  [{task_id}] rollout {view.label}: {"submitted" if view.delivered else "NO SUBMISSION"}'
              + (f' ({view.error[:200]})' if view.error else ''), flush=True)
    return views


def _run_one_mode(mode, task_id, k, base, *, adapter, improver, selector, ns, runs_root, lib, base_tree, result):
    """One recursion mode over the shared bases, blind selection, then reflection into the library."""
    work_dir = ns.task_dir(runs_root, task_id)
    run = recursion.run_mode(mode, task_id, k, base, adapter=adapter, improver=improver, work_dir=work_dir,
                             run_id_of=lambda label: ns.run_id(task_id, label), base_tree=base_tree,
                             library_dir=lib.context_dir() if lib else None)
    result.modes[mode] = run.to_dict()
    candidates = selection.selection_pool(mode, k, run.rollouts)
    if k == 1:
        sel = selection.trivial_selection(task_id, k, mode, candidates, adapter=adapter, work_dir=work_dir)
    else:
        sel = selection.run_selection(task_id, k, mode, candidates, adapter=adapter, improver=selector,
                                      work_dir=work_dir)
    if lib and sel.ok:
        selection.reflect_adoption(task_id, sel, run.rollouts, adapter=adapter, improver=selector, work_dir=work_dir,
                                   decisions={s.label: s.notes or s.error for s in run.stages})
    record = sel.to_dict()
    chosen_tree = run.trees.get(sel.chosen_label, base_tree)
    record['chosen_tree_is_base'] = surface.unchanged(base_tree, chosen_tree)
    if sel.chosen_label:
        record['harness_fingerprint'] = surface.fingerprint(work_dir/'harnesses'/sel.chosen_label)
    result.selections[mode] = record
    if not sel.ok:
        result.errors.append(f'{mode}: selection failed: {sel.error}')
    if lib and sel.ok and library_store.has_experience(sel.experience):
        lib.add(task_id, chosen_tree, why=sel.why, label=sel.chosen_label, run_id=sel.chosen_run_id,
                experience=sel.experience)
    print(f'  [{task_id}] {mode}: adopted {sel.chosen_label or "nothing"}'
          f'{" (base tree)" if record["chosen_tree_is_base"] else ""}; '
          f'lesson {"stored" if library_store.has_experience(sel.experience) else "none"}'
          + (f'; {sel.error[:200]}' if sel.error else ''), flush=True)


def run_task(task_id, k, *, adapter, improver, runs_root, modes=MODES, seed=None, tag='', libraries=None,
             selector=None, rollout_width=recursion.PHASE_WIDTH):
    """k shared base rollouts -> every mode -> blind selection per mode -> summary.json."""
    if int(k) < 1:
        raise ValueError(f'k must be positive, got {k!r}')
    if not modes or len(set(modes)) != len(modes) or set(modes) - set(MODES):
        raise ValueError(f'modes must be distinct members of {MODES}, got {modes!r}')
    libraries = libraries or {}
    selector = selector or improver

    def ns_of(mode):
        return Namespace(adapter.name, adapter.executor, mode, k, seed, tag)

    result = TaskResult(task_id=task_id, domain=adapter.domain_of(task_id), k=k, seed=seed)
    work = ns_of('agg').task_dir(runs_root, task_id)
    work.mkdir(parents=True, exist_ok=True)
    summary = work/'summary.json'
    if summary.is_file() and _resume(summary, result, modes, adapter, ns_of, runs_root, libraries):
        return result
    base_tree = surface.read_tree(adapter.base_harness())
    labels = [f'base{i}' for i in range(1, k + 1)]
    base = _roll_bases(task_id, adapter, [(label, ns_of('agg').run_id(task_id, label)) for label in labels],
                       work/'missing', rollout_width)
    result.base = [{'label': b.label, 'run_id': b.run_id, 'run_dir': str(b.run_dir), 'delivered': b.delivered,
                    'error': b.error} for b in base]
    if not any(b.ok for b in base):
        result.status = 'base-failed'
        result.errors.append('no base rollout produced anything')
        write_summary(summary, result)
        return result
    for mode in modes:
        try:
            _run_one_mode(mode, task_id, k, base, adapter=adapter, improver=improver, selector=selector,
                          ns=ns_of(mode), runs_root=runs_root, lib=libraries.get(mode), base_tree=base_tree,
                          result=result)
        except ServiceUnavailable:
            raise
        except Exception as exc:
            result.modes[mode] = {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                                  'traceback': traceback.format_exc()[-1500:]}
            result.errors.append(f'{mode}: {type(exc).__name__}: {exc}')
        result.status = 'partial'
        write_summary(summary, result)
    result.status = merged_status(result.to_dict())
    write_summary(summary, result)
    return result


def run_domain(domain, task_ids, k, *, adapter, improver, runs_root, lib_root, modes=MODES, seed=None, tag='',
               selector=None):
    """Every case of one domain in stream order, threading the library forward."""
    libraries = {mode: library_store.Library(root=Path(lib_root)/mode, domain=domain) for mode in modes}
    out = {'domain': domain, 'n_tasks': len(task_ids), 'done': 0, 'failed': 0, 'tasks': []}
    for index, task_id in enumerate(task_ids, 1):
        try:
            for lib in libraries.values():
                lib.retain(task_ids[:index - 1])
            before = {mode: len(lib.entries()) for mode, lib in libraries.items()}
            result = run_task(task_id, k, adapter=adapter, improver=improver, runs_root=runs_root, modes=modes,
                              seed=seed, tag=tag, libraries=libraries, selector=selector)
            ok = result.status in ('ok', 'skipped')
            row = {'task': task_id, 'status': result.status, 'library_entries_before': before}
            if not ok:
                row['error'] = '; '.join(result.errors) or result.status
        except ServiceUnavailable:
            raise
        except Exception as exc:
            ok, row = False, {'task': task_id, 'status': 'exception', 'error': f'{type(exc).__name__}: {exc}'}
        out['done' if ok else 'failed'] += 1
        out['tasks'].append(row)
        print(f'  [{domain}] {index}/{len(task_ids)} {row["status"]:<10} {task_id}'
              + (f'  :: {row["error"]}' if row.get('error') else ''), flush=True)
    return out


def run_sweep(k, *, adapter, improver, runs_root, split='all', modes=MODES, seed=None, tag='', workers=1,
              only_domains=(), only_tasks=(), max_tasks=None, selector=None):
    """Domains run concurrently; cases within a domain stay ordered so libraries only read earlier cases."""
    ordered = task_stream(adapter, split=split, seed=seed, only_domains=only_domains, only_tasks=only_tasks,
                          max_tasks=max_tasks)
    lib_root = library_root(runs_root, adapter, k, seed=seed, tag=tag)
    started, rows = datetime.now(UTC).isoformat(), []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run_domain, domain, tasks, k, adapter=adapter, improver=improver, runs_root=runs_root,
                               lib_root=lib_root, modes=modes, seed=seed, tag=tag, selector=selector): domain
                   for domain, tasks in ordered.items()}
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except ServiceUnavailable:
                pool.shutdown(wait=True, cancel_futures=True)
                raise
            except Exception as exc:
                rows.append({'domain': futures[future], 'error': f'{type(exc).__name__}: {exc}'})
    return {'adapter': adapter.name, 'executor': adapter.executor, 'k': k, 'modes': list(modes), 'seed': seed,
            'tag': tag, 'split': split, 'library_root': str(lib_root), 'domain_order': list(ordered),
            'started_at': started, 'finished_at': datetime.now(UTC).isoformat(), 'domains': rows}
