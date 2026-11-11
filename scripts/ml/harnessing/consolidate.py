"""Cross-case consolidation: a domain's adopted harnesses, lessons and TRAIN-split feedback -> one general edit.

Task-level refinement is answer-free by design. Consolidation is the outer loop, where labelled training
cases are fair evidence: for train cases only, each rollout is shown against the human outline or decision.
Validation and test cases never enter this context; the gate (gate.py) judges the result on validation.

    context/harness/            the current base: the tree to edit
    context/library/            the per-case loop's adopted harnesses and lessons for this domain
    context/feedback/<case>/    train cases: feedback.md (outcomes vs the human) and card.png (overlays)
    context/usage.md            which components the rollouts actually used
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import library as library_store
from . import prompts, surface, usage
from .channels import ImproverError
from .evaluate import strip_case_facts
from .spec import resolve_prompts

COLOURS = {'machine start': '#22d3ee', 'human': '#4ade80', 'base run': '#fb923c', 'adapted run': '#e879f9'}

JOB = """\
You are consolidating the harness of an aerial-imagery tracing agent for the {domain}. A per-case loop
already adapted the harness case by case; its adopted harnesses and transferable lessons are in
`context/library/`. Those adaptations were judged only by the agent's own evidence. You have what that
loop never saw: for TRAINING cases, the human reviewer's saved answer and how each rollout compared with
it, in `context/feedback/`. The harness you write is judged afterwards on held-out cases you cannot see.

Edit `context/harness/` (the current base) into ONE improved base for the whole {domain}:

- GENERAL ONLY. No case facts, identities or coordinates. `MEMORY.md` must not carry a case-facts section.
- EVERY RULE NEEDS TWO TRAINING CASES. Cite at least two `context/feedback/` cases (or one case plus a library
  lesson) behind each change; a pattern seen once is noise.
- FIX WHAT THE HUMANS CORRECTED. Read each feedback card: where the runs differ from the human answer, find
  the decision the agent got wrong (wrong boundary followed, over- or under-editing, a missed shadow band,
  invented detail, a skipped audit) and change the instruction that governs that decision.
- RESPECT THE REVIEWERS' TARGET. The human answer is the authority for what is saved. When reviewers replace
  jagged machine rings with straight eaves, teach regularisation that keeps supported steps; never teach
  invented detail, and never teach the agent to copy a reviewer's habit the imagery does not support.
- INLINE, CHECKABLE STEPS. `context/usage.md` shows which components runs actually opened. Put a decision's
  check inside the SKILL step where the decision is made; a linked reference nobody reads changes nothing.
- KEEP_BASE is valid when nothing generalises.

Reply with ONE `<notes>` block: one line per change -> the training cases and library lessons behind it."""


def _crop(rings, width, height, pad=24):
    points = [p for ring in rings for p in ring]
    left, top = max(0, min(p[0] for p in points) - pad), max(0, min(p[1] for p in points) - pad)
    right, bottom = min(width, max(p[0] for p in points) + pad), min(height, max(p[1] for p in points) + pad)
    return [int(left), int(top), int(right) + 1, int(bottom) + 1]


def feedback_card(image_path, outlines, target):
    """Original RGB and an overlay of every named outline, registered on one crop."""
    with Image.open(image_path) as source:
        image = source.convert('RGB')
    rings = [o['ring'] for o in outlines.values() if o]
    box = _crop(rings, *image.size)
    scale = 560/max(box[2] - box[0], box[3] - box[1])
    size = (max(1, round((box[2] - box[0])*scale)), max(1, round((box[3] - box[1])*scale)))
    tile = image.crop(box).resize(size, Image.Resampling.NEAREST)
    canvas = Image.new('RGB', (2*size[0] + 30, size[1] + 70), '#101827')
    draw, font = ImageDraw.Draw(canvas), ImageFont.load_default(size=14)
    canvas.paste(tile, (10, 40))
    view = tile.copy()
    overlay = ImageDraw.Draw(view)
    for name, outline in outlines.items():
        for ring in [outline['ring'], *outline.get('holes', [])] if outline else []:
            overlay.line([((x - box[0])*scale, (y - box[1])*scale) for x, y in [*ring, ring[0]]],
                         fill=COLOURS[name], width=2)
    canvas.paste(view, (20 + size[0], 40))
    draw.text((10, 12), 'Original RGB', fill='white', font=font)
    draw.text((20 + size[0], 12), 'Overlay', fill='white', font=font)
    x = 10
    for name in outlines:
        draw.text((x, size[1] + 48), f'— {name}', fill=COLOURS[name], font=font)
        x += 150
    canvas.save(target)


def _fmt(value):
    return '-' if value is None else f'{value:.3f}'


def write_feedback(adapter, case_id, runs, dest):
    """feedback.md and card.png for one TRAIN case: each run against the held-out human answer."""
    case = adapter.case(case_id)
    folder = Path(dest)/case_id
    folder.mkdir(parents=True, exist_ok=True)
    scored = {name: (adapter.score(run_dir, case_id), adapter.submission(run_dir)) for name, run_dir in runs.items()}
    lines = [f'# {case_id}', '', f'- class: {case.get("kind") or case.get("family")}; task: {case["task"]}', '']
    if case['task'] == 'review':
        heldout = adapter.heldout(case_id)
        lines += [f'- human reviewer answered: **{heldout["human_decision"]}** (word: {heldout.get("human_word")})', '']
        for name, (row, sub) in scored.items():
            lines += [f'## {name}: {sub["decision"] if sub else "no submission"}'
                      f'{" -- MATCHES the human" if row and row["judge_agrees"] else " -- differs from the human"}',
                      '', (sub or {}).get('notes', '')[:1500], '']
        card = next((Path(d)/'run'/'submission.png' for d in runs.values() if (Path(d)/'run'/'submission.png').is_file()),
                    None)
        shutil.copy2(card or adapter.task_files(case_id)/'image.png', folder/'card.png')
        lines.insert(2, f'- question asked of the reviewer: {case["question"]["title"]}')
    else:
        heldout = adapter.heldout(case_id)
        reference = heldout['reference_outline_pixels']
        start = case['start_outline_pixels']
        lines += [f'- machine start: {len(start["ring"])} vertices; human answer: {len(reference["ring"])} vertices '
                  f'({heldout.get("change")})', '',
                  '| outline | IoU with human | boundary match within 0.5 m | worst distance (m) | outcome |',
                  '|---|---|---|---|---|']
        first = next((row for row, _ in scored.values() if row), None)
        if first:
            lines.append(f'| machine start | {_fmt(first["start_iou"])} | {_fmt(first["start_boundary_f"])} | '
                         f'{_fmt(first["start_hausdorff_m"])} | - |')
        for name, (row, _sub) in scored.items():
            if row:
                lines.append(f'| {name} | {_fmt(row["judge_iou"])} | {_fmt(row["judge_boundary_f"])} | '
                             f'{_fmt(row["judge_hausdorff_m"])} | {row["outcome"]} |')
        for name, (_row, sub) in scored.items():
            lines += ['', f'## {name} notes', '', (sub or {}).get('notes', '(no submission)')[:1500]]
        outlines = {'machine start': start, 'human': reference}
        outlines.update({name: sub['outline_pixels'] for name, (_r, sub) in scored.items() if sub})
        feedback_card(adapter.task_files(case_id)/'image.png', outlines, folder/'card.png')
    (folder/'feedback.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {name: (row or {}).get('outcome') for name, (row, _s) in scored.items()}


def assert_split_clean(root, forbidden):
    """Refuse a context naming any validation/test case, scene or object."""
    hits = []
    for path in Path(root).rglob('*'):
        if path.is_file() and path.suffix in ('.md', '.json', '.py', '.txt', ''):
            text = path.read_text(encoding='utf-8', errors='replace')
            hits += [f'{path.relative_to(root)}: {token}' for token in forbidden if token in text]
    if hits:
        raise ValueError(f'held-out identities would enter the consolidation context: {hits[:5]}')


def build_context(dest, adapter, base_dir, train_runs, library_dir, forbidden):
    dest = Path(dest)
    shutil.rmtree(dest, ignore_errors=True)
    surface.write_tree(surface.read_tree(base_dir), dest/'harness', stubs=False)
    if library_dir and Path(library_dir).is_dir():
        shutil.copytree(library_dir, dest/'library')
    outcomes, usages = {}, {}
    for case_id, runs in sorted(train_runs.items()):
        outcomes[case_id] = write_feedback(adapter, case_id, runs, dest/'feedback')
        for name, run_dir in runs.items():
            found = adapter.component_usage(run_dir)
            if found:
                usages[f'{case_id[:24]}/{name}'] = found
    if usages:
        (dest/'usage.md').write_text(usage.render(usages), encoding='utf-8')
    rows = ['# Training feedback', '', 'One folder per TRAINING case. Validation and test cases are not here.', '',
            '| case | ' + ' | '.join(sorted({n for o in outcomes.values() for n in o})) + ' |']
    names = sorted({n for o in outcomes.values() for n in o})
    rows.append('|---|' + '---|'*len(names))
    rows += [f'| `{case_id}` | ' + ' | '.join(str(o.get(n) or '-') for n in names) + ' |' for case_id, o in outcomes.items()]
    (dest/'feedback'/'INDEX.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    assert_split_clean(dest, forbidden)
    return outcomes


def consolidate(adapter, improver, *, domain, base_dir, train_runs, library_dir, out_dir, forbidden, label):
    """One consolidation session with one validation retry; returns (tree or None, notes, error)."""
    work = Path(out_dir)
    ctx = work/'context'
    build_context(ctx, adapter, base_dir, train_runs, library_dir, forbidden)
    sample = next(iter(train_runs))
    work_prompts = resolve_prompts(adapter, sample)
    mission = '\n\n'.join([JOB.format(domain=work_prompts.domain_word), prompts.EVIDENCE_BOUNDARY, prompts.SURFACE,
                           '# YOUR EVIDENCE\n\nRead `context/feedback/INDEX.md`, every feedback card, `context/usage.md`, '
                           'the library INDEX and the current harness before editing.'])
    base = surface.read_tree(base_dir)
    error = ''
    for attempt in (1, 2):
        retry = f'\n\n# YOUR PREVIOUS ATTEMPT WAS REJECTED\n\n{error}\n\nFix it in `context/harness/`.' if error else ''
        try:
            reply = improver.call(mission + retry, ctx, f'consolidate.{label}.a{attempt}',
                                  harvest='context/harness')
        except ImproverError as exc:
            return None, '', str(exc)
        tree = surface.read_tree(reply.workspace) if reply.workspace else {}
        problems = []
        if not tree:
            problems.append('context/harness/ came back empty')
        elif surface.CASE_FACTS in tree.get('MEMORY.md', '') and strip_case_facts(tree) != tree:
            problems.append(f'MEMORY.md still has a "{surface.CASE_FACTS}" section; a consolidated base is general')
        hits = sorted(token for token in adapter.case_identities() if any(token in text for text in tree.values()))
        if hits:
            problems.append(f'the harness names case identities {hits[:3]}; write general rules only')
        if not problems:
            surface.autotrigger(tree)
            try:
                surface.validate(tree, allow_subagents=adapter.allow_subagents, allow_hooks=adapter.allow_hooks)
            except ValueError as exc:
                problems.append(str(exc))
        if not problems:
            notes = re.search(r'<notes>(.*?)</notes>', reply.text, re.S)
            surface.write_tree(tree, work/'candidate')
            (work/'notes.md').write_text((notes.group(1) if notes else reply.text).strip() + '\n', encoding='utf-8')
            (work/'diff.json').write_text(json.dumps(surface.diff_summary(base, tree), indent=2))
            return tree, (notes.group(1) if notes else reply.text).strip(), ''
        error = ' | '.join(problems)
    return None, '', error


def train_runs_for(adapter, runs_root, ns, domain):
    """{case: {'base run': dir, 'adapted run': dir}} for judged TRAIN cases of `domain` in a namespace."""
    out = {}
    root = Path(runs_root)/str(ns)
    for path in sorted(root.glob('*/summary.json')):
        summary = json.loads(path.read_text())
        task_id = summary['task_id']
        entry = adapter.index.get(task_id)
        if not entry or entry['split'] != 'train' or entry['domain'] != domain:
            continue
        runs = {}
        base = next((b for b in summary.get('base', []) if b.get('delivered')), None)
        if base:
            runs['base run'] = Path(base['run_dir'])
        sel = (summary.get('selections') or {}).get(ns.mode) or {}
        if sel.get('ok') and sel.get('chosen_run_dir') and not sel.get('chosen_tree_is_base'):
            runs['adapted run'] = Path(sel['chosen_run_dir'])
        if runs and adapter.heldout(task_id):
            out[task_id] = runs
    return out


def library_dir_for(runs_root, adapter, ns, domain):
    from .schedule import library_root
    lib = library_store.Library(root=library_root(runs_root, adapter, ns.k, seed=ns.seed, tag=ns.tag)/ns.mode,
                                domain=domain)
    return lib.context_dir()
