"""The evidence tree an improver session reads; the only way evidence reaches a model.

    context/task.md                  the task as the agent received it
    context/source/                  the case material (imagery, machine outline), never a held-out one
    context/rollouts/<label>.md      full trajectories -- no prompt-budget truncation
    context/deliverables/<label>/    the submitted outline, its notes and rendered overlays
    context/harness/<rel>            the harness tree being edited
    context/library/INDEX.md         earlier cases in this domain with their full harnesses
    context/pool/<label>/            candidate harness, diff, rollout and overlays
    context/decisions/<label>.md     earlier edits and composition notes

Evidence is served as files and the prompt is a short pointer, so nothing is truncated to fit.
Adapted from Harness² core/context.py.
"""
from __future__ import annotations

import difflib
import fnmatch
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import surface
from .usage import render as usage_render

# The judge's own output; finding one of these in model context is a leak, not a false positive.
VERDICT_MARKERS = ('"judge_iou"', '"judge_boundary_f"', '"judge_hausdorff_m"', '"judge_agrees"',
                   '"judge_decision_accuracy"', '"human_decision"', 'judged.json')
SCAN_SUFFIXES = frozenset({'.md', '.txt', '.json', '.jsonl', '.csv', '.yaml', '.yml', '.html', '.log',
                           '.py', '.js', '.ts', '.sh', '.toml', '.diff', ''})


@dataclass
class ContextTree:
    root: Path
    task_id: str
    rollouts: list[str]
    deliverables: list[str]
    harness_files: int
    library: bool
    pool: list[str]
    failed: list[str] = field(default_factory=list)
    source_files: int = 0
    decisions: int = 0
    usage: bool = False

    @property
    def layout(self):
        """The evidence listing that goes in the mission; it only mentions what exists."""
        lines = ['context/task.md                  the task as the agent received it']
        if self.source_files:
            lines.append('context/source/                 the case material: imagery and the machine outline')
        if self.decisions:
            lines.append('context/decisions/              earlier edits and composition decisions')
        if self.rollouts:
            names = ', '.join(f'{label}.md' for label in self.rollouts[:6])
            more = f' (+{len(self.rollouts) - 6} more)' if len(self.rollouts) > 6 else ''
            lines.append(f'context/rollouts/               FULL trajectories: {names}{more}')
        if self.deliverables:
            lines.append('context/deliverables/<label>/    what each rollout submitted: outline.md and '
                         'overlay images (open them)')
        if self.harness_files:
            lines.append(f'context/harness/                the harness you are editing '
                         f'({self.harness_files} files)')
        if self.pool:
            lines.append(f'context/pool/                   {len(self.pool)} candidates, each with its '
                         'harness, harness.diff, rollout.md, produced.md and images/')
        if self.library:
            lines.append('context/library/                earlier cases in this domain -- browse INDEX.md '
                         'first, then open what looks relevant')
        if self.usage:
            lines.append('context/usage.md                which harness components each rollout actually used')
        return '\n'.join(lines)


def _matches(rel, patterns):
    return any(fnmatch.fnmatch(rel, p.lstrip('./')) or fnmatch.fnmatch(rel, f"*/{p.lstrip('./')}")
               for p in patterns)


def assert_no_leak_paths(root, patterns):
    """Reject adapter-declared held-out paths before a context reaches a model."""
    root = Path(root)
    if not patterns or not root.is_dir():
        return
    hits = sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                  if p.is_file() and _matches(p.relative_to(root).as_posix(), patterns))
    if hits:
        raise ValueError(f'held-out path(s) would enter model context: {", ".join(hits[:8])}')


def assert_no_verdicts(root, markers=VERDICT_MARKERS):
    """Refuse a context tree containing the judge's output, by file name and by content."""
    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        hit = next((m for m in markers if m in rel), None)
        if hit is None and path.suffix.lower() in SCAN_SUFFIXES:
            body = path.read_text(encoding='utf-8', errors='replace')
            hit = next((m for m in markers if m in body), None)
        if hit:
            raise ValueError(f'judge verdict would enter model context: {rel!r} matched {hit!r}. '
                             'Find the read path that produced it rather than widening the markers.')


def assert_tree_has_no_verdicts(tree, markers=VERDICT_MARKERS):
    """The same tripwire on an agent-authored harness before it can be rolled or stored."""
    for rel in sorted(tree):
        hit = next((m for m in markers if m in rel or m in str(tree[rel])), None)
        if hit:
            raise ValueError(f'harness file {rel!r} carries judge marker {hit!r}; remove that string, '
                             'files carrying it cannot be served as evidence to later cases')


def copy_tree(src, dst):
    """Copy a directory without byte-compiled files; return the file count."""
    count = 0
    for path in sorted(Path(src).rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        target = Path(dst)/path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def copy_source(adapter, task_id, root):
    """Copy the case material under `root/source`, minus held-out paths."""
    src = adapter.task_files(task_id) if callable(getattr(adapter, 'task_files', None)) else None
    if not src or not Path(src).is_dir():
        return 0
    count = 0
    for path in sorted(Path(src).rglob('*')):
        rel = path.relative_to(src).as_posix()
        if path.is_file() and '__pycache__' not in path.parts and not _matches(rel, adapter.leak_globs):
            target = Path(root)/'source'/rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            count += 1
    if count:
        assert_no_leak_paths(Path(root)/'source', adapter.leak_globs)
    return count


def _copy_images(adapter, run_dir, dest, limit=8):
    names = []
    for image in list(adapter.deliverable_images(run_dir) or [])[:limit]:
        image = Path(image)
        if image.is_file():
            target = dest/'images'/image.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(image, target)
            names.append(image.name)
    return names


def _write_rollout(dest, run, adapter, trunc):
    failure = ''
    try:
        text = adapter.trajectory(run.run_dir, obs_trunc=trunc)
    except Exception as exc:
        text, failure = f'(trajectory unavailable: {type(exc).__name__}: {exc})', f'{run.label}: {exc}'
    (dest/'rollouts'/f'{run.label}.md').write_text(text, encoding='utf-8')
    out = dest/'deliverables'/run.label
    out.mkdir(parents=True, exist_ok=True)
    try:
        produced = adapter.deliverable_text(run.run_dir) or '(no outline submitted)'
    except Exception as exc:
        produced = f'(deliverable unavailable: {type(exc).__name__}: {exc})'
    (out/'outline.md').write_text(produced, encoding='utf-8')
    images = _copy_images(adapter, run.run_dir, out)
    if images:
        (out/'INDEX.md').write_text(f'# {run.label}\n\n- `outline.md`: the submitted outline and notes\n'
                                    + ''.join(f'- `images/{name}`\n' for name in images)
                                    + '\nThe images render the submitted outline on the case imagery; '
                                    'judge the fit from them and from context/source/, not from the notes.\n',
                                    encoding='utf-8')
    return failure


def _write_pool(dest, pool, adapter, trunc):
    labels = []
    reference = surface.read_tree(dest/'harness')
    for label, candidate_dir, run in pool:
        out = dest/'pool'/label
        out.mkdir(parents=True, exist_ok=True)
        if candidate_dir and Path(candidate_dir).is_dir():
            assert_no_leak_paths(candidate_dir, adapter.leak_globs)
            copy_tree(candidate_dir, out/'harness')
            candidate = surface.read_tree(candidate_dir)
            (out/'harness.diff').write_text(''.join(
                line for name in sorted(reference.keys() | candidate.keys())
                for line in difflib.unified_diff(reference.get(name, '').splitlines(keepends=True),
                                                 candidate.get(name, '').splitlines(keepends=True),
                                                 fromfile=f'current/{name}', tofile=f'{label}/{name}')),
                encoding='utf-8')
        if run is not None:
            try:
                (out/'rollout.md').write_text(adapter.trajectory(run.run_dir, obs_trunc=trunc),
                                              encoding='utf-8')
                (out/'produced.md').write_text(adapter.deliverable_text(run.run_dir) or '(nothing submitted)',
                                               encoding='utf-8')
                _copy_images(adapter, run.run_dir, out)
            except Exception as exc:
                (out/'rollout.md').write_text(f'(unavailable: {exc})', encoding='utf-8')
        labels.append(label)
    return labels


def build(dest, adapter, task_id, rollouts=(), *, harness_dir=None, library_dir=None, pool=(),
          decisions=None, obs_trunc=None):
    """Write the evidence tree; return what landed so the mission describes only that."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    trunc = obs_trunc if obs_trunc is not None else adapter.obs_trunc
    (dest/'task.md').write_text(adapter.task_prompt(task_id), encoding='utf-8')
    source_files = copy_source(adapter, task_id, dest)
    for label, notes in (decisions or {}).items():
        (dest/'decisions').mkdir(exist_ok=True)
        (dest/'decisions'/f'{label}.md').write_text(notes or 'No decision notes returned.', encoding='utf-8')
    got, failed = [], []
    if rollouts:
        (dest/'rollouts').mkdir()
        (dest/'deliverables').mkdir()
    for run in rollouts:
        failure = _write_rollout(dest, run, adapter, trunc)
        (failed if failure else got).append(failure or run.label)
    harness_files = 0
    if harness_dir and Path(harness_dir).is_dir():
        assert_no_leak_paths(harness_dir, adapter.leak_globs)
        harness_files = copy_tree(harness_dir, dest/'harness')
    labels = _write_pool(dest, pool, adapter, trunc)
    library = bool(library_dir and Path(library_dir).is_dir() and any(Path(library_dir).iterdir()))
    if library:
        assert_no_leak_paths(library_dir, adapter.leak_globs)
        shutil.copytree(library_dir, dest/'library', dirs_exist_ok=True)
    usage = write_usage(dest, adapter, [*rollouts, *(run for _label, _dir, run in pool if run is not None)])
    assert_no_leak_paths(dest, adapter.leak_globs)
    assert_no_verdicts(dest)
    if rollouts and not got:
        raise RuntimeError('no trajectory could be rendered for ANY rollout -- the improver would be '
                           'adapting on nothing: ' + '; '.join(failed[:4]))
    return ContextTree(root=dest, task_id=task_id, rollouts=got, deliverables=[r.label for r in rollouts],
                       harness_files=harness_files, library=library, pool=labels, failed=failed,
                       source_files=source_files, decisions=len(decisions or {}), usage=usage)


def write_usage(dest, adapter, runs):
    """context/usage.md: the component x rollout usage matrix, when the adapter can read it."""
    fn = getattr(adapter, 'component_usage', None)
    if not callable(fn):
        return False
    usages = {}
    for run in runs:
        try:
            found = fn(run.run_dir)
        except Exception:
            found = None
        if found:
            usages[run.label] = found
    if not usages:
        return False
    (Path(dest)/'usage.md').write_text(usage_render(usages), encoding='utf-8')
    return True


def mission(tree, *, job):
    """The short pointer to the evidence on disk: the job, where the evidence is, and what to open."""
    return '\n'.join([
        job.strip(), '', '# YOUR EVIDENCE', '',
        'It is on disk, in `context/`. Read it -- do not answer from this message alone.', '',
        tree.layout, '',
        'Every trajectory EVENT is present: the trajectory was not clipped to fit this mission. An '
        'individual observation may contain an explicit renderer-truncation marker; that marker is '
        'ours, not behaviour by the agent. Do not diagnose it as an agent failure.', '',
        'Read `task.md`, view the case imagery in `source/`, and read the current harness first. '
        'Inventory every available rollout or candidate and inspect each at least once -- including '
        'its overlay images -- before deciding. For a candidate, compare what changed from its '
        'editing baseline, what behaviour changed in its rollout, what improved, and what regressed '
        'or remained unresolved. Cite the specific trajectory moment behind every change; one you '
        'cannot trace to a moment is speculation.'])
