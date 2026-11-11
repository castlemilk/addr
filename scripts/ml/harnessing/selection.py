"""Blind selection of one composed final, then reflection on the whole refinement record.

The selector sees anonymised submitted outlines and their overlays, never harnesses, trajectories
or held-out data. Reflection afterwards reads everything and distils at most one lesson for the
domain library. Adapted from Harness² core/selection.py.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import context, prompts
from .channels import ImproverError
from .spec import resolve_prompts

CHOICE = re.compile(r'```choice\s*\n?\s*([A-Za-z0-9_-]+)\s*\n?\s*```', re.S)
WHY = re.compile(r'<why>(.*?)</why>', re.S)
EXPERIENCE = re.compile(r'<experience>(.*?)</experience>', re.S)
EDITS = re.compile(r'<edits>(.*?)</edits>', re.S)


@dataclass
class Selection:
    task_id: str
    mode: str
    k: int
    chosen_id: str = ''
    chosen_label: str = ''
    chosen_run_id: str = ''
    chosen_run_dir: str = ''
    why: str = ''
    id_map: dict = field(default_factory=dict)
    dropped: list = field(default_factory=list)
    n_candidates: int = 0
    ok: bool = False
    fallback: bool = False
    trivial: bool = False
    error: str = ''
    usd: float = 0.0
    experience: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def anon_id(index):
    """A..Z, AA..AZ, BA... -- always accepted by the choice parser."""
    out, n = '', int(index) + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(ord('A') + rem) + out
    return out


def assign_ids(labels, *, task_id, k, mode):
    """anon id -> label, by canonical sort then a seeded shuffle: stable across reruns."""
    ordered = sorted(labels)
    random.Random(int(hashlib.sha1(f'{task_id}|{k}|{mode}'.encode()).hexdigest()[:12], 16)).shuffle(ordered)
    return {anon_id(i): label for i, label in enumerate(ordered)}


def selection_pool(mode, k, rollouts):
    """Exactly the k composed finals of `mode`; base rollouts are never in the pool."""
    labels = [f'agg{i}' for i in range(1, k + 1)] if mode == 'agg' else [f'c{t}' for t in range(1, k + 1)]
    return {label: rollouts[label] for label in labels if label in rollouts}


def _render_pool(candidates, adapter, sel):
    """Drop runs that submitted nothing before anonymising, recording each drop."""
    pool, artifacts = {}, {}
    for label, run in candidates.items():
        if not (run.ok and run.delivered):
            sel.dropped.append(label)
            continue
        try:
            text = adapter.deliverable_text(run.run_dir) or ''
        except Exception as exc:
            sel.dropped.append(f'{label} (render failed: {type(exc).__name__}: {exc})'[:200])
            continue
        if text.strip():
            pool[label], artifacts[label] = run, text
        else:
            sel.dropped.append(label)
    return pool, artifacts


def _persist(sel, root):
    try:
        (root/'selection.json').write_text(json.dumps(sel.to_dict(), indent=2, default=str), encoding='utf-8')
    except OSError as exc:
        sel.ok = False
        sel.error = f'{sel.error} | could not persist selection: {exc}'[:400]


def trivial_selection(task_id, k, mode, candidates, *, adapter, work_dir):
    """Deliver the single k=1 final without a selector session."""
    if len(candidates) > 1:
        raise ValueError(f'trivial_selection expects at most one candidate, got {sorted(candidates)}')
    sel = Selection(task_id=task_id, mode=mode, k=k, trivial=True)
    pool, _ = _render_pool(candidates, adapter, sel)
    sel.n_candidates = len(pool)
    if not pool:
        sel.error = 'no composed final submitted an outline'
        return sel
    label = next(iter(pool))
    sel.ok, sel.chosen_id, sel.chosen_label, sel.id_map = True, 'A', label, {'A': label}
    sel.chosen_run_id, sel.chosen_run_dir = pool[label].run_id, str(pool[label].run_dir)
    sel.why = 'trivial pool: single final at k=1; delivered without a selector session'
    root = Path(work_dir)/'selection'/f'{mode}{k}'
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    _persist(sel, root)
    return sel


def run_selection(task_id, k, mode, candidates, *, adapter, improver, work_dir):
    """Pick one final. A selector failure falls back to the first final and never raises."""
    sel = Selection(task_id=task_id, mode=mode, k=k)
    pool, artifacts = _render_pool(candidates, adapter, sel)
    sel.n_candidates = len(pool)
    if not pool:
        sel.error = 'no composed final submitted an outline'
        return sel
    sel.id_map = assign_ids(list(pool), task_id=task_id, k=k, mode=mode)
    root = Path(work_dir)/'selection'/f'{mode}{k}'
    shutil.rmtree(root, ignore_errors=True)
    (root/'candidates').mkdir(parents=True)
    (root/'task.md').write_text(adapter.task_prompt(task_id), encoding='utf-8')
    context.copy_source(adapter, task_id, root)
    shown = []
    for anon, label in sel.id_map.items():
        images = [Path(p) for p in adapter.deliverable_images(pool[label].run_dir) or [] if Path(p).is_file()]
        for image in images:
            (root/'candidates'/anon).mkdir(exist_ok=True)
            shutil.copy2(image, root/'candidates'/anon/image.name)
            shown.append(image.name)
        note = (f'\nOverlays for this candidate are in `{anon}/`: {", ".join(i.name for i in images)}. OPEN THEM.\n'
                if images else '')
        (root/'candidates'/f'{anon}.md').write_text(
            f'<!-- candidate {anon}: complete, nothing trimmed.{note}-->\n{artifacts[label]}', encoding='utf-8')
    ids = ', '.join(sorted(sel.id_map))
    assets = ('    context/candidates/<id>/     overlays of that candidate\'s outline on the case imagery\n'
              if shown else '')
    mission = '\n\n'.join([
        prompts.select(resolve_prompts(adapter, task_id), len(pool), assets_note=assets),
        '# YOUR EVIDENCE\n\nIt is on disk. Read it -- do not answer from this message alone.\n\n'
        'context/task.md                 the request\n'
        'context/source/                 the case imagery and the machine outline\n'
        f'context/candidates/<id>.md      one file per candidate: {ids}\n' + assets
        + f'\nOpen every candidate, including its overlays, before choosing. Choose exactly one of: {ids}.'])
    context.assert_no_leak_paths(root, adapter.leak_globs)
    context.assert_no_verdicts(root)
    try:
        reply = improver.call(mission, root, f'select.{mode}{k}')
        sel.usd += reply.cost_usd
        match = CHOICE.search(reply.text)
        if not match:
            raise ValueError('no ```choice block in the reply')
        if match.group(1).strip() not in sel.id_map:
            raise ValueError(f'chose {match.group(1).strip()!r}, which is not one of {ids}')
        why = WHY.search(reply.text)
        sel.why = (why.group(1).strip() if why else '')[:2000]
        sel.chosen_id = match.group(1).strip()
        sel.ok = True
    except (ImproverError, ValueError) as exc:
        # The neutral stand-in is recorded but stays unadopted: a failed selector is not a choice.
        sel.usd += getattr(exc, 'cost_usd', 0.0)
        sel.chosen_id, sel.fallback, sel.error = sorted(sel.id_map)[0], True, str(exc)[:400]
    sel.chosen_label = sel.id_map[sel.chosen_id]
    sel.chosen_run_id, sel.chosen_run_dir = pool[sel.chosen_label].run_id, str(pool[sel.chosen_label].run_dir)
    _persist(sel, root)
    return sel


def reflect_adoption(task_id, sel, candidates, *, adapter, improver, work_dir, decisions=None):
    """Learn from harness edits, decisions, trajectories and outlines after adoption."""
    if not sel.ok or sel.chosen_label not in candidates:
        return {}
    work_dir = Path(work_dir)
    root = work_dir/'reflection'/f'{sel.mode}{sel.k}'
    harnesses = work_dir/'harnesses'
    evidence = context.build(root, adapter, task_id, list(candidates.values()),
                             harness_dir=harnesses/sel.chosen_label,
                             pool=[(label, harnesses/label, run) for label, run in candidates.items()
                                   if not label.startswith('base')], decisions=decisions)
    context.copy_tree(adapter.base_harness(), root/'base_harness')
    (root/'adoption.json').write_text(json.dumps({'chosen_label': sel.chosen_label, 'candidate_ids': sel.id_map,
                                                  'reason': sel.why}, indent=2) + '\n', encoding='utf-8')
    context.assert_no_leak_paths(root, adapter.leak_globs)
    context.assert_no_verdicts(root)
    mission = '\n\n'.join([
        prompts.experience(resolve_prompts(adapter, task_id)),
        f'# WHAT HAPPENED\n\nHarness {sel.chosen_label} was adopted. The selection reasoning was:\n\n{sel.why}\n\n'
        'context/adoption.json maps any anonymous IDs in that reasoning to rollout labels. Compare '
        'context/harness/ with context/base_harness/ and the candidates in context/pool/. Read the decisions '
        'and execution contrasts before writing.\n\n' + evidence.layout])
    try:
        reply = improver.call(mission, root, f'experience.{sel.mode}{sel.k}')
    except ImproverError as exc:
        sel.usd += exc.cost_usd
        return {}
    sel.usd += reply.cost_usd
    edits = EDITS.search(reply.text)
    if edits:
        (root/'edits.md').write_text(edits.group(1).strip() + '\n', encoding='utf-8')
    block = EXPERIENCE.search(reply.text)
    if not block:
        return {}
    out = {}
    for line in block.group(1).strip().splitlines():
        key, sep, value = line.partition(':')
        if sep and key.strip().lower() in ('cue', 'lesson', 'evidence', 'confidence'):
            out[key.strip().lower()] = value.strip()
    if not all(out.get(key) for key in ('cue', 'lesson', 'evidence')):
        return {}
    if edits:
        out['edits'] = edits.group(1).strip()
    context.assert_tree_has_no_verdicts({'EXPERIENCE.md': json.dumps(out)})
    sel.experience = out
    _persist(sel, work_dir/'selection'/f'{sel.mode}{sel.k}')
    return out
