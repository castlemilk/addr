"""Command line: build cases, check, refine (run), judge, report, promote, and the offline demo.

    python -m scripts.ml.harnessing cases build [--tasks outline,detect] [--kinds building,tree]
    python -m scripts.ml.harnessing check [--executor claude] [--live]
    python -m scripts.ml.harnessing run --k 1 --mode parallel --only-domains roof-outline --max-tasks 2
    python -m scripts.ml.harnessing judge --k 1 --mode parallel [--bases]
    python -m scripts.ml.harnessing report --k 1 --mode parallel
    python -m scripts.ml.harnessing promote --k 1 --mode parallel --domain roof-outline [--apply]
    python -m scripts.ml.harnessing demo --output /tmp/addr-harness-demo

Judge, report and promote must use the run's executor, --k, --mode, --seed and --tag. Repeating a
run command resumes it; change models or the base skill under a new --tag.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import cases, evaluate, executor, judge, promote, report, schedule, surface
from .adapter import TracingAdapter
from .channels import CHANNELS, ChannelSpec, Improver

RUNS = Path(os.environ.get('ADDR_HARNESS_RUNS_DIR', 'data/harness/runs'))
CASES = Path(os.environ.get('ADDR_HARNESS_CASES_DIR', str(cases.ROOT)))


def _modes(value):
    return schedule.MODES if value == 'both' else (schedule.mode_token(value),)


def _namespace_args(parser):
    parser.add_argument('--k', type=int, default=1, help='task-level recursion budget')
    parser.add_argument('--mode', default='parallel', choices=('parallel', 'sequential', 'both'))
    parser.add_argument('--executor', default='claude', choices=executor.EXECUTORS)
    parser.add_argument('--seed', type=int, default=None, help='shuffle the case stream reproducibly')
    parser.add_argument('--tag', default='', help='namespace suffix; use a new one when changing models or base')
    parser.add_argument('--runs-dir', type=Path, default=RUNS)
    parser.add_argument('--cases-dir', type=Path, default=CASES)
    parser.add_argument('--base', default=None, help="base harness: a tree path, or 'head' for the lineage head")


def _adapter(args, **kwargs):
    spec = executor.ExecutorSpec(kind=args.executor, model=getattr(args, 'model', '') or '',
                                 effort=getattr(args, 'effort', '') or '', timeout=getattr(args, 'timeout', 1800),
                                 max_budget_usd=getattr(args, 'max_budget_usd', None))
    return TracingAdapter(runs_root=args.runs_dir, cases_root=args.cases_dir, spec=spec,
                          base_dir=resolve_base(args), **kwargs)


def resolve_base(args):
    """--base PATH, or --base head for the lineage head (seeded from the snapshot on first use)."""
    choice = getattr(args, 'base', None)
    if not choice:
        return None
    if choice != 'head':
        return Path(choice)
    from .adapter import build_base
    from .lineage import Lineage

    snapshot = Path(args.runs_dir)/'base'
    if not surface.is_tree(snapshot):
        build_base(snapshot)
    lineage = Lineage(args.runs_dir)
    return (lineage.head() or lineage.seed(snapshot))[1]


def _namespaces(args, adapter):
    return [schedule.Namespace(adapter.name, adapter.executor, mode, args.k, args.seed, args.tag) for mode in _modes(args.mode)]


def guard_config(runs_dir, namespace, config):
    """Refuse to resume a namespace under a different executor model, improver or base harness."""
    path = Path(runs_dir)/str(namespace)/'config.json'
    if path.is_file():
        saved = json.loads(path.read_text())
        changed = sorted(key for key in config if saved.get(key) != config[key])
        if changed:
            raise SystemExit(f'{namespace} was run with different {", ".join(changed)}; '
                             f'use a new --tag or ADDR_HARNESS_RUNS_DIR (saved {path})')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))


def cmd_cases(args):
    if args.action == 'unlabeled':
        index = cases.build_unlabeled(Path('.'), args.cases_dir, kinds=tuple(k for k in (args.kinds or 'building').split(',')
                                                                                if k),
                                      per_domain=args.limit_per_domain or 20, per_scene=args.per_scene,
                                      min_score=args.min_score)
    elif args.action == 'build':
        index = cases.build(Path('.'), args.cases_dir, tasks=tuple(args.tasks.split(',')),
                            kinds=tuple(k for k in args.kinds.split(',') if k), unchanged_ratio=args.unchanged_ratio,
                            scenes=tuple(args.scenes), limit_per_domain=args.limit_per_domain)
    else:
        index = cases.load_index(args.cases_dir)
    print(f'{len(index)} cases in {args.cases_dir}')
    for domain, row in cases.census(index).items():
        print(f'  {domain:<20} {row}')


async def _probe_server(case_dir):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(prefix='addr-harness-check-') as tmp:
        params = StdioServerParameters(command=executor.python(), args=['-m', 'scripts.ml.harnessing.case_server',
                                                                       '--case', str(case_dir), '--run', tmp],
                                       env={**os.environ, 'PYTHONPATH': str(executor.REPO)}, cwd=str(executor.REPO))
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return sorted(tool.name for tool in (await session.list_tools()).tools)


def cmd_check(args):
    adapter = _adapter(args)
    problems = executor.preflight(adapter.spec)
    base = surface.read_tree(adapter.base_harness())
    census = surface.validate(base, allow_subagents=adapter.allow_subagents, allow_hooks=adapter.allow_hooks)
    print(f'base harness {adapter.base_harness()}: {census}')
    tasks = adapter.tasks()
    print(f'cases: {len(tasks)} in {args.cases_dir}')
    if tasks:
        tools = asyncio.run(_probe_server(adapter.task_files(tasks[0])))
        print(f'case server for {tasks[0]}: {", ".join(tools)}')
    if args.live and tasks and not problems:
        with tempfile.TemporaryDirectory(prefix='addr-harness-live-') as tmp:
            workspace = Path(tmp)
            shutil.copytree(adapter.task_files(tasks[0]), workspace/'case')
            (workspace/'run').mkdir()
            executor.materialise(base, workspace, adapter.spec.kind)
            meta = executor.launch(adapter.spec, workspace, base, 'Run exactly this one shell command and no other '
                                   'tool: python3 -c "import PIL, numpy, shapely; print(\'libs ok\')" -- then reply '
                                   'READY, its output, and the names of the addr-case tools you can see.',
                                   stream=workspace/'stream.jsonl', stderr=workspace/'stderr.log', timeout=300)
            print(f'live {adapter.spec.kind}: rc={meta["returncode"]} cost=${meta["cost_usd"]} wall={meta["wall_s"]}s')
            print(executor.render_trajectory(workspace/'stream.jsonl', adapter.spec.kind, obs_trunc=600)[-2400:])
            if meta['returncode'] != 0:
                problems.append(f'live probe failed: {(workspace/"stderr.log").read_text()[-600:]}')
    for problem in problems:
        print(f'PROBLEM: {problem}')
    return 1 if problems else 0


def cmd_rollout(args):
    """One rollout on one case, then the operator-only judge; `--label base1` seeds a later `run`."""
    adapter = _adapter(args, keep_workspaces=args.keep_workspaces)
    if args.case not in adapter.index:
        raise SystemExit(f'unknown case {args.case!r}; see `cases list`')
    problems = executor.preflight(adapter.spec)
    if problems:
        raise SystemExit('; '.join(problems))
    ns = schedule.Namespace(adapter.name, adapter.executor, 'agg', args.k, args.seed, args.tag)
    run_dir = adapter.execute(args.case, args.harness, ns.run_id(args.case, args.label), timeout=args.timeout)
    meta = json.loads((run_dir/'meta.json').read_text())
    print(adapter.trajectory(run_dir, obs_trunc=400)[-args.tail:])
    print(f'\nrun {run_dir}\nmeta: rc={meta["returncode"]} timed_out={meta["timed_out"]} wall={meta["wall_s"]}s '
          f'cost=${meta["cost_usd"]}')
    print(adapter.deliverable_text(run_dir) or 'NO SUBMISSION')
    scored = adapter.score(run_dir, args.case)
    if scored:
        keys = [k for k in scored if k.startswith(('judge_', 'start_', 'delta_')) or k in ('outcome', 'metric')]
        print('operator-only judge (never shown to the loop):', json.dumps({k: scored[k] for k in keys}))
    return 0 if adapter.delivered(run_dir) else 1


def cmd_run(args):
    adapter = _adapter(args, refresh_base=args.refresh_base, keep_workspaces=args.keep_workspaces)
    modes = _modes(args.mode)
    stream = schedule.task_stream(adapter, split=args.split, seed=args.seed, only_domains=args.only_domains,
                                  only_tasks=args.only_tasks, max_tasks=args.max_tasks)
    for domain, tasks in stream.items():
        print(f'{domain}: {len(tasks)} case(s): {", ".join(tasks[:6])}{" ..." if len(tasks) > 6 else ""}')
    rollouts = sum(map(len, stream.values()))*(args.k + 3*args.k*len(modes))
    print(f'plan: {rollouts} rollouts and {sum(map(len, stream.values()))*3*args.k*len(modes)} improver sessions '
          f'(+ selection/reflection) with {args.executor}/{args.model or "default"}')
    if args.dry_run:
        return 0
    problems = executor.preflight(adapter.spec)
    if problems:
        raise SystemExit('; '.join(problems))
    base = surface.tree_fingerprint(surface.read_tree(adapter.base_harness()))
    improver_spec = ChannelSpec(args.improver, args.improver_model, effort=args.improver_effort,
                                max_budget_usd=args.improver_budget_usd)
    for ns in _namespaces(args, adapter):
        guard_config(args.runs_dir, ns, {'executor': args.executor, 'model': args.model, 'effort': args.effort,
                                         'improver': args.improver, 'improver_model': args.improver_model,
                                         'base_fingerprint': base})
    improver = Improver(improver_spec, audit_dir=args.runs_dir/'audit'/f'{adapter.name}-{args.executor}')
    result = schedule.run_sweep(args.k, adapter=adapter, improver=improver, runs_root=args.runs_dir, split=args.split,
                                modes=modes, seed=args.seed, tag=args.tag, workers=args.workers,
                                only_domains=args.only_domains, only_tasks=args.only_tasks, max_tasks=args.max_tasks)
    path = args.runs_dir/f'sweep-{adapter.name}-{args.executor}-k{args.k}{f"-{args.tag}" if args.tag else ""}.json'
    path.write_text(json.dumps(result, indent=2))
    print(f'sweep record: {path}')
    return 0 if all(not d.get('failed') and not d.get('error') for d in result['domains']) else 1


def cmd_eval(args):
    """Replay a harness (case facts stripped) against a baseline on held-out cases, paired per case."""
    adapter = _adapter(args, keep_workspaces=args.keep_workspaces)
    stream = schedule.task_stream(adapter, split=args.split, seed=args.seed, only_domains=args.only_domains,
                                  only_tasks=args.only_tasks, max_tasks=args.max_tasks)
    tasks = [task for group in stream.values() for task in group]
    candidate = surface.read_tree(args.harness)
    if not args.keep_case_facts:
        candidate = evaluate.strip_case_facts(candidate)
    baseline = surface.read_tree(args.baseline or adapter.base_harness())
    if args.skills_only:
        candidate = {**{rel: text for rel, text in baseline.items() if not rel.startswith('skills/')},
                     **{rel: text for rel, text in candidate.items() if rel.startswith('skills/')}}
    print(f'eval-{args.label}: {len(tasks)} case(s) x 2 harnesses x {args.repeats} = {len(tasks)*2*args.repeats} '
          f'rollouts with {args.executor}/{args.model or "default"}; {", ".join(tasks[:8])}')
    if args.dry_run:
        return 0
    problems = executor.preflight(adapter.spec)
    if problems:
        raise SystemExit('; '.join(problems))
    rows = evaluate.run_eval(adapter, candidate, baseline, tasks, label=args.label, repeats=args.repeats,
                             workers=args.workers)
    summary = evaluate.summarise(rows)
    (args.runs_dir/f'eval-{args.label}'/'summary.json').write_text(json.dumps(summary, indent=2, default=str))
    print(evaluate.render(args.label, summary))
    return 0


def cmd_trace(args):
    """Roll one harness (default: the lineage head) `repeats` times per case; `fieldwork queue` ranks the results."""
    args.base = args.base or 'head'
    adapter = _adapter(args, keep_workspaces=args.keep_workspaces)
    stream = schedule.task_stream(adapter, split=args.split, only_domains=args.only_domains, only_tasks=args.only_tasks,
                                  max_tasks=args.max_tasks)
    tasks = [task for group in stream.values() for task in group]
    print(f'trace-{args.label}: {len(tasks)} case(s) x {args.repeats} repeats = {len(tasks)*args.repeats} rollouts of '
          f'{adapter.base_harness()} with {args.executor}/{args.model or "default"}')
    if args.dry_run:
        return 0
    problems = executor.preflight(adapter.spec)
    if problems:
        raise SystemExit('; '.join(problems))
    tree = surface.read_tree(adapter.base_harness())
    rows = evaluate.run_eval(adapter, tree, tree, tasks, label=f'trace-{args.label}', repeats=args.repeats,
                             workers=args.workers, only=('baseline',))
    submitted = sum(1 for r in rows if r.get('run_dir') and adapter.delivered(Path(r['run_dir'])))
    print(f'{submitted}/{len(rows)} rollouts submitted. Rank and stage them for review with:\n'
          f'  python -m scripts.ml.harnessing fieldwork queue --label {args.label} --cases-dir {args.cases_dir} '
          f'--runs-dir {args.runs_dir} --base {args.base}')
    return 0


def cmd_judge(args):
    adapter = _adapter(args)
    for ns in _namespaces(args, adapter):
        rows, path = judge.judge(adapter, args.runs_dir, ns, bases=args.bases, workers=args.workers)
        graded = sum(1 for r in rows if r.get('graded'))
        print(f'{ns}: judged {graded}/{len(rows)} -> {path}')
    return 0


def cmd_report(args):
    adapter = _adapter(args)
    for ns in _namespaces(args, adapter):
        built = report.build(args.runs_dir, ns)
        (args.runs_dir/str(ns)/'report.json').write_text(json.dumps(built, indent=2, default=str))
        print(report.render(built))
    return 0


def cmd_promote(args):
    adapter = _adapter(args)
    ns = _namespaces(args, adapter)[0]
    lib_root = schedule.library_root(args.runs_dir, adapter, args.k, seed=args.seed, tag=args.tag)
    case, tree, lib = promote.adopted(lib_root, ns.mode, args.domain, args.case)
    changes = promote.skill_changes(tree)
    print(f'adopted harness from {case} ({lib.dir}); {len(changes)} skill file(s) differ from the project skill')
    others = promote.other_changes(tree, surface.read_tree(adapter.base_harness()))
    if any(others.values()):
        print(f'not promoted (case-environment or new components): {others}')
    text = promote.patch(changes)
    out = args.out or args.runs_dir/str(ns)/f'promote-{args.domain}.patch'
    out.write_text(text)
    (out.with_suffix('.lessons.md')).write_text(promote.lessons_markdown(lib))
    print(f'patch: {out}\nlessons: {out.with_suffix(".lessons.md")}')
    if args.apply:
        for path in promote.apply(changes):
            print(f'wrote {path}')
    return 0


def cmd_fieldwork(args):
    """Fieldwork review items as cases, and adopted assessments back to Fieldwork as agent reviews."""
    from . import fieldwork

    if args.action == 'queue':
        adapter = _adapter(args)
        rows = json.loads((args.runs_dir/f'eval-trace-{args.label}'/'eval.json').read_text())
        got = fieldwork.queue_traces(adapter, rows, label=args.label, limit=args.limit or 20, prefix=args.prefix,
                                     harness={'executor': adapter.executor})
        print(json.dumps({k: v for k, v in got.items() if k != 'skipped'}, indent=1, default=str))
        print(f'skipped: {len(got["skipped"])} ({sorted(set(got["skipped"].values()))})')
        if got.get('stage'):
            print(f'review the bundle, then: python -m scripts.ml.harnessing fieldwork push --stage {got["stage"]} '
                  f'[--origin URL --receipts-dir DIR] [--live]')
        return 0
    if args.action == 'push':
        if not args.stage:
            raise SystemExit('push needs --stage <bundle> (from `fieldwork queue`)')
        origin = args.origin or fieldwork.fr.DEFAULT_REMOTE_URL
        got = fieldwork.fr.push(root='.', stage_dir=str(args.stage), url=origin, directory=args.receipts_dir,
                                token_file=args.token_file, limit=args.limit, families=('trace',), dry_run=not args.live)
        print(json.dumps({k: got[k] for k in ('status', 'dryRun', 'origin', 'projectId', 'runId', 'stageSha')
                          if k in got}, indent=1))
        return 0
    if args.action == 'pull':
        got = fieldwork.pull(origin=args.origin, token_file=args.token_file)
        print(f'pulled cursor {got["cursor"]}: {got["items"]} items, {got["decisions"]} decisions, '
              f'{got["agentReviews"]} agent reviews -> {got["files"]["decisions"]} (read-only on Fieldwork)')
        args.action = 'cases'
    if args.action == 'cases':
        index = fieldwork.build_cases(Path('.'), args.cases_dir, decisions=args.decisions or None,
                                      agent_reviews=args.agent_reviews or None,
                                      families=tuple(args.families.split(',')), tasks=tuple(args.tasks.split(',')),
                                      include_undecided=args.include_undecided, limit_per_domain=args.limit_per_domain)
        print(f'{len(index)} Fieldwork cases in {args.cases_dir}')
        for domain, row in cases.census(index).items():
            print(f'  {domain:<28} {row}')
        return 0
    adapter = _adapter(args)
    ns = _namespaces(args, adapter)[0]
    origin = args.origin or fieldwork.fr.DEFAULT_REMOTE_URL
    results = fieldwork.post_reviews(adapter, args.runs_dir, ns, origin=origin, reviewer=args.reviewer,
                                     dry_run=not args.live, token_file=args.token_file, limit=args.limit)
    stamp = 'live' if args.live else 'plan'
    out = args.runs_dir/str(ns)/f'fieldwork-post-{stamp}.json'
    out.write_text(json.dumps(results, indent=2, default=str))
    counts = {}
    for row in results:
        counts[row['outcome']] = counts.get(row['outcome'], 0) + 1
    print(f'{ns} -> {origin}: {counts or "no adopted assessments"}; record {out}')
    planned = next((row for row in results if row.get('payload')), None)
    if planned:
        print('first planned payload:', json.dumps({**planned['payload'], 'notes': planned['payload']['notes'][:300]}))
    return 0 if all(row['outcome'] in ('posted', 'planned', 'skip_receipt') for row in results) else 1


def _improver(args, adapter):
    spec = ChannelSpec(args.improver, args.improver_model, effort=args.improver_effort,
                       max_budget_usd=args.improver_budget_usd)
    return Improver(spec, audit_dir=args.runs_dir/'audit'/f'{adapter.name}-{args.executor}')


def cmd_cycle(args):
    """One self-improvement cycle for one domain from the lineage head."""
    from . import cycle

    args.base = 'head'
    adapter = _adapter(args, keep_workspaces=args.keep_workspaces)
    train = schedule.task_stream(adapter, split='train', only_domains=[args.domain], max_tasks=args.train_cases)
    val = schedule.task_stream(adapter, split='val', only_domains=[args.domain], max_tasks=args.val_cases)
    n_train, n_val = len(train.get(args.domain, [])), len(val.get(args.domain, []))
    print(f'cycle {args.domain} from {adapter.base_harness()}: {n_train} train cases (4 rollouts + 3-4 improver sessions '
          f'each), consolidation (1-2 sessions), gate on {n_val} val cases x 2 harnesses x {args.repeats} repeats = '
          f'{n_val*2*args.repeats} rollouts; executor {args.executor}/{args.model or "default"}, budget {args.budget_usd}')
    if args.dry_run:
        return 0
    problems = executor.preflight(adapter.spec)
    if problems:
        raise SystemExit('; '.join(problems))
    try:
        record = cycle.run_cycle(adapter, _improver(args, adapter), runs_root=args.runs_dir, domain=args.domain,
                                 k=args.k, train_cases=args.train_cases, val_cases=args.val_cases, repeats=args.repeats,
                                 min_cases=args.min_cases, budget_usd=args.budget_usd, workers=args.workers,
                                 inner=args.inner)
    except cycle.BudgetExceeded as exc:
        print(f'STOPPED: {exc}')
        return 2
    print(f'cycle outcome: {record["outcome"]}; spent ${record.get("spent")}; record {args.runs_dir}/cycles/')
    return 0


def cmd_gate(args):
    """Gate a candidate base (consolidated or engineered) against the lineage head on validation cases."""
    from . import gate
    from .lineage import Lineage

    args.base = 'head'
    adapter = _adapter(args, keep_workspaces=args.keep_workspaces)
    from .cycle import stratified

    tasks = stratified(adapter, schedule.task_stream(adapter, split='val', only_domains=[args.domain])
                       .get(args.domain, []), args.val_cases)
    candidate, baseline = surface.read_tree(args.candidate), surface.read_tree(adapter.base_harness())
    surface.validate(candidate, allow_subagents=adapter.allow_subagents, allow_hooks=adapter.allow_hooks)
    print(f'gate {args.label}: {args.candidate} vs {adapter.base_harness()} on {len(tasks)} val cases x {args.repeats} '
          f'repeats = {len(tasks)*2*args.repeats} rollouts; diff {surface.diff_summary(baseline, candidate)}')
    if args.dry_run:
        return 0
    result = gate.run_gate(adapter, candidate, baseline, tasks, label=args.label, repeats=args.repeats,
                           workers=args.workers, min_cases=args.min_cases)
    print(gate.render(result))
    admitted = Lineage(args.runs_dir).admit(candidate, source=f'gated {args.label}: {args.candidate}', gate=result)
    print(f'admitted as lineage v{admitted[0]}' if admitted else 'not admitted; recorded as rejected')
    return 0


def cmd_candidate(args):
    """Write an engineered candidate (base + a named component) for `gate`."""
    from .engineered import CANDIDATES

    adapter = _adapter(args)
    base = surface.read_tree(adapter.base_harness())
    tree = CANDIDATES[args.name](base)
    surface.validate(tree, allow_subagents=adapter.allow_subagents, allow_hooks=adapter.allow_hooks)
    adapter.validate_tree(tree)
    surface.write_tree(tree, args.out)
    print(f'{args.name}: {surface.diff_summary(base, tree)} -> {args.out}')
    return 0


def cmd_confirm(args):
    """Confirm a lineage version against its parent on TEST cases; only a confirmed version yields a patch."""
    from . import gate
    from .cycle import stratified
    from .lineage import Lineage

    lineage = Lineage(args.runs_dir)
    row = lineage.version(args.version)
    if not row or row['parent'] is None:
        raise SystemExit(f'v{args.version} is not an admitted version with a parent in {lineage.root}')
    args.base = str(lineage.dir_of(row['parent']))
    adapter = _adapter(args, keep_workspaces=args.keep_workspaces)
    tasks = stratified(adapter, schedule.task_stream(adapter, split='test', only_domains=[args.domain])
                       .get(args.domain, []), args.val_cases)
    candidate, baseline = surface.read_tree(lineage.dir_of(args.version)), surface.read_tree(adapter.base_harness())
    print(f'confirm v{args.version} vs v{row["parent"]} on {len(tasks)} TEST cases x {args.repeats} repeats = '
          f'{len(tasks)*2*args.repeats} rollouts with {args.executor}/{args.model or "default"}')
    if args.dry_run:
        return 0
    result = gate.run_gate(adapter, candidate, baseline, tasks, label=f'confirm-v{args.version}-{args.domain}',
                           repeats=args.repeats, workers=args.workers, min_cases=args.min_cases)
    print(gate.render(result))
    lineage.confirm(args.version, result)
    if result['passed']:
        out = lineage.root/f'v{args.version}-confirmed.patch'
        out.write_text(promote.patch(promote.skill_changes(candidate)))
        print(f'CONFIRMED on test. Reviewable promotion patch for .opencode/skills/addr-roof-tracing: {out}')
    else:
        print('not confirmed on test: keep it out of the project skill (it stays the internal lineage head)')
    return 0


def cmd_lineage(args):
    from .lineage import Lineage

    if args.revoke is not None:
        if not args.reason:
            raise SystemExit('--revoke needs --reason (it is recorded with the invalidated version)')
        target = Lineage(args.runs_dir).revoke(args.revoke, args.reason)
        print(f'revoked v{args.revoke}; its tree is kept at {target}')
    data = Lineage(args.runs_dir).load()
    for row in data['versions']:
        gate = row.get('gate') or {}
        print(f"v{row['version']}  {row['fingerprint']}  parent {row['parent']}  {row['source']}"
              + (f"  gate {'passed' if gate.get('passed') else 'n/a'}" if gate else '')
              + ('  CONFIRMED on test' if Lineage.confirmed(row) else '  (not confirmed on test)' if gate else ''))
    for row in data.get('invalidated', []):
        print(f"invalidated  {row['fingerprint']}  {row['source']}: {row.get('invalidated_reason', '')}")
    for row in data['rejected']:
        print(f"rejected  {row['fingerprint']}  parent v{row['parent']}  {row['source']}")
    return 0


def cmd_demo(args):
    from . import demo

    output = args.output or Path(tempfile.mkdtemp(prefix='addr-harness-demo-'))
    shutil.rmtree(output, ignore_errors=True)
    _sweep, reports = demo.run(output, k=args.k)
    for built in reports.values():
        print(report.render(built))
    print(f'artifacts: {output}')
    return 0


def parser():
    root = argparse.ArgumentParser(prog='python -m scripts.ml.harnessing', description=__doc__.splitlines()[0])
    sub = root.add_subparsers(dest='command', required=True)
    c = sub.add_parser('cases', help='build or list the offline case set')
    c.add_argument('action', choices=('build', 'unlabeled', 'list'),
                   help='unlabeled = answer-free cases from unreviewed candidates (the trace flywheel)')
    c.add_argument('--per-scene', type=int, default=2, help='unlabeled: at most this many objects per scene')
    c.add_argument('--min-score', type=float, default=0.35, help='unlabeled: skip likely false detections')
    c.add_argument('--cases-dir', type=Path, default=CASES)
    c.add_argument('--tasks', default='outline,detect')
    c.add_argument('--kinds', default='', help='comma-separated object kinds (default all)')
    c.add_argument('--unchanged-ratio', type=float, default=.25,
                   help='share of confirmed-unchanged outlines kept as over-editing controls')
    c.add_argument('--limit-per-domain', type=int, default=None)
    c.add_argument('--scenes', nargs='*', default=[])
    c.set_defaults(func=cmd_cases)
    for name, func, help_text in (('check', cmd_check, 'preflight: executor, base harness, case server'),
                                  ('rollout', cmd_rollout, 'one rollout on one case, with the operator-only judge'),
                                  ('run', cmd_run, 'refine harnesses over the case stream'),
                                  ('judge', cmd_judge, 'measure adopted (or --bases) runs against held-out data'),
                                  ('report', cmd_report, 'summarise judged runs'),
                                  ('promote', cmd_promote, 'export adopted skill edits as a patch')):
        p = sub.add_parser(name, help=help_text)
        _namespace_args(p)
        p.add_argument('--model', default='', help='executor model id')
        p.add_argument('--effort', default='', help='executor reasoning effort')
        p.add_argument('--workers', type=int, default=1 if name == 'run' else 4)
        p.set_defaults(func=func)
        if name == 'check':
            p.add_argument('--live', action='store_true', help='one tiny executor call with the case tools attached')
        if name in ('rollout', 'run'):
            p.add_argument('--timeout', type=int, default=1800, help='seconds per rollout')
            p.add_argument('--max-budget-usd', type=float, default=None, help='per-rollout cap (claude)')
            p.add_argument('--keep-workspaces', action='store_true')
        if name == 'rollout':
            p.add_argument('--case', required=True)
            p.add_argument('--harness', type=Path, default=None, help='a harness tree (default: the base snapshot)')
            p.add_argument('--label', default='base1', help='base1 lets a later `run` reuse this rollout')
            p.add_argument('--tail', type=int, default=6000, help='trajectory characters to print')
        if name == 'run':
            p.add_argument('--improver', default='claude', choices=CHANNELS)
            p.add_argument('--improver-model', default='')
            p.add_argument('--improver-effort', default='high')
            p.add_argument('--improver-budget-usd', type=float, default=None, help='per-session cap (claude)')
            p.add_argument('--split', default='all', choices=('all', *cases.SPLITS))
            p.add_argument('--only-domains', nargs='*', default=[])
            p.add_argument('--only-tasks', nargs='*', default=[])
            p.add_argument('--max-tasks', type=int, default=None, help='cases PER DOMAIN')
            p.add_argument('--refresh-base', action='store_true', help='re-snapshot the project skill as base')
            p.add_argument('--dry-run', action='store_true', help='show the case stream and budget only')
        if name == 'judge':
            p.add_argument('--bases', action='store_true', help='judge the base-harness rollouts instead')
        if name == 'promote':
            p.add_argument('--domain', required=True)
            p.add_argument('--case', default=None, help='a specific library entry (default newest)')
            p.add_argument('--out', type=Path, default=None)
            p.add_argument('--apply', action='store_true', help='write adopted skill files into the project skill')
    e = sub.add_parser('eval', help='replay a harness against a baseline on held-out cases (rollouts only)')
    _namespace_args(e)
    e.add_argument('--harness', type=Path, required=True, help='candidate tree, e.g. an adopted harnesses/agg1')
    e.add_argument('--baseline', type=Path, default=None, help='baseline tree (default: the base snapshot)')
    e.add_argument('--label', required=True, help='names data/harness/runs/eval-<label>/')
    e.add_argument('--keep-case-facts', action='store_true', help="keep the candidate's MEMORY.md case facts")
    e.add_argument('--skills-only', action='store_true',
                   help="baseline plus only the candidate's skills/ files: what `promote` would write")
    e.add_argument('--repeats', type=int, default=1)
    e.add_argument('--split', default='test', choices=('all', *cases.SPLITS))
    e.add_argument('--only-domains', nargs='*', default=[])
    e.add_argument('--only-tasks', nargs='*', default=[])
    e.add_argument('--max-tasks', type=int, default=None, help='cases PER DOMAIN')
    e.add_argument('--model', default='')
    e.add_argument('--effort', default='')
    e.add_argument('--workers', type=int, default=2)
    e.add_argument('--timeout', type=int, default=1800)
    e.add_argument('--max-budget-usd', type=float, default=None)
    e.add_argument('--keep-workspaces', action='store_true')
    e.add_argument('--dry-run', action='store_true')
    e.set_defaults(func=cmd_eval)
    f = sub.add_parser('fieldwork', help='Fieldwork review items as cases; adopted assessments as agent reviews')
    f.add_argument('action', choices=('pull', 'cases', 'post', 'queue', 'push'),
                   help='pull = read-only pull of human words, then cases; queue = rank and stage traced outlines; '
                        'push = send a staged trace bundle (a plan unless --live)')
    f.add_argument('--label', default='', help='queue: the `trace` label whose rollouts to rank')
    f.add_argument('--prefix', default='r1019-14', help='queue: item id prefix for this trace set')
    f.add_argument('--stage', type=Path, default=None, help='push: the bundle `fieldwork queue` staged')
    f.add_argument('--receipts-dir', default=None,
                   help='push: receipt folder (default data/fieldwork-sync/<project>, bound to the live origin)')
    _namespace_args(f)
    f.set_defaults(cases_dir=Path(os.environ.get('ADDR_HARNESS_FIELDWORK_CASES_DIR', 'data/harness/cases-fieldwork')),
                   runs_dir=Path(os.environ.get('ADDR_HARNESS_FIELDWORK_RUNS_DIR', 'data/harness/runs-fieldwork')))
    f.add_argument('--decisions', nargs='*', default=[], help='pulled decisions-N.json files (default: on-disk pulls)')
    f.add_argument('--agent-reviews', nargs='*', default=[], help='pulled agent-reviews-N.json (Codex baseline)')
    f.add_argument('--families', default='shadow,cut,corner,lot')
    f.add_argument('--tasks', default='review,outline')
    f.add_argument('--include-undecided', action='store_true', help='pushed items with no human word yet (unscored)')
    f.add_argument('--limit-per-domain', type=int, default=None)
    f.add_argument('--origin', default=None, help='Fieldwork origin (default: the live service)')
    f.add_argument('--reviewer', default=None, help='agent reviewer id (default harness-<executor>[-tag])')
    f.add_argument('--token-file', default=None)
    f.add_argument('--limit', type=int, default=None, help='post at most this many assessments')
    f.add_argument('--live', action='store_true',
                   help='send SaveAgentReview (live origin also needs FIELDWORK_PUSH_ALLOW_LIVE=1); default plans only')
    f.add_argument('--model', default='')
    f.add_argument('--effort', default='')
    f.set_defaults(func=cmd_fieldwork)
    for name, func, help_text in (('cycle', cmd_cycle, 'one self-improvement cycle: run, consolidate, gate, admit'),
                                  ('gate', cmd_gate, 'gate a candidate base against the lineage head on validation')):
        g = sub.add_parser(name, help=help_text)
        _namespace_args(g)
        g.add_argument('--domain', required=True)
        g.add_argument('--val-cases', type=int, default=8, help='validation cases for the gate')
        g.add_argument('--repeats', type=int, default=2)
        g.add_argument('--min-cases', type=int, default=6)
        g.add_argument('--model', default='', help='executor model id (e.g. claude-sonnet-5-5)')
        g.add_argument('--effort', default='')
        g.add_argument('--workers', type=int, default=2)
        g.add_argument('--timeout', type=int, default=1500)
        g.add_argument('--max-budget-usd', type=float, default=None, help='per-rollout cap (claude)')
        g.add_argument('--keep-workspaces', action='store_true')
        g.add_argument('--dry-run', action='store_true')
        g.set_defaults(func=func)
        if name == 'cycle':
            g.add_argument('--train-cases', type=int, default=4, help='train cases refined per cycle')
            g.add_argument('--inner', default='harness2', choices=('harness2', 'rollouts'),
                           help='per-case Propose-Probe-Compose, or one cheap head rollout per train case')
            g.add_argument('--budget-usd', type=float, default=None, help='stop between phases once exceeded')
            g.add_argument('--improver', default='claude', choices=CHANNELS)
            g.add_argument('--improver-model', default='')
            g.add_argument('--improver-effort', default='high')
            g.add_argument('--improver-budget-usd', type=float, default=None)
        else:
            g.add_argument('--candidate', type=Path, required=True, help='candidate harness tree')
            g.add_argument('--label', required=True)
    cand = sub.add_parser('candidate', help='write an engineered candidate harness for the gate')
    _namespace_args(cand)
    cand.add_argument('name', choices=('edge-profile',))
    cand.add_argument('--out', type=Path, required=True)
    cand.set_defaults(func=cmd_candidate)
    tr = sub.add_parser('trace', help='roll the lineage head repeatedly on (unlabeled) cases for the review queue')
    _namespace_args(tr)
    tr.add_argument('--label', required=True)
    tr.add_argument('--repeats', type=int, default=2, help='repeats per case; their disagreement ranks the queue')
    tr.add_argument('--split', default='all', choices=('all', *cases.SPLITS))
    tr.add_argument('--only-domains', nargs='*', default=[])
    tr.add_argument('--only-tasks', nargs='*', default=[])
    tr.add_argument('--max-tasks', type=int, default=None, help='cases PER DOMAIN')
    tr.add_argument('--model', default='')
    tr.add_argument('--effort', default='')
    tr.add_argument('--workers', type=int, default=2)
    tr.add_argument('--timeout', type=int, default=1500)
    tr.add_argument('--max-budget-usd', type=float, default=None)
    tr.add_argument('--keep-workspaces', action='store_true')
    tr.add_argument('--dry-run', action='store_true')
    tr.set_defaults(func=cmd_trace)
    cf = sub.add_parser('confirm', help='confirm a lineage version on TEST cases before promotion')
    _namespace_args(cf)
    cf.add_argument('--version', type=int, required=True)
    cf.add_argument('--domain', required=True)
    cf.add_argument('--val-cases', type=int, default=12, help='TEST cases to use')
    cf.add_argument('--repeats', type=int, default=2)
    cf.add_argument('--min-cases', type=int, default=6)
    cf.add_argument('--model', default='')
    cf.add_argument('--effort', default='')
    cf.add_argument('--workers', type=int, default=2)
    cf.add_argument('--timeout', type=int, default=1500)
    cf.add_argument('--max-budget-usd', type=float, default=None)
    cf.add_argument('--keep-workspaces', action='store_true')
    cf.add_argument('--dry-run', action='store_true')
    cf.set_defaults(func=cmd_confirm)
    lg = sub.add_parser('lineage', help='list base versions and gate decisions; --revoke withdraws a flawed head')
    _namespace_args(lg)
    lg.add_argument('--revoke', type=int, default=None, help='withdraw this head version (its gate was not valid)')
    lg.add_argument('--reason', default='')
    lg.set_defaults(func=cmd_lineage)
    d = sub.add_parser('demo', help='the deterministic, model-free loop on synthetic roofs')
    d.add_argument('--output', type=Path, default=None)
    d.add_argument('--k', type=int, default=2)
    d.set_defaults(func=cmd_demo)
    return root


def main(argv=None):
    from .channels import ServiceUnavailable

    args = parser().parse_args(argv)
    try:
        code = args.func(args) or 0
    except ServiceUnavailable as exc:
        print(f'STOPPED: the model service is unavailable ({exc}). Nothing was cached or recorded for the '
              'interrupted step, and no gate or lineage decision was made; rerun the same command to resume.')
        code = 3
    sys.exit(code)
