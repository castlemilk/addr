"""Harnessing-level output for people: adopted skill edits as a reviewable patch, plus the lessons.

A domain library's newest entry is the harness the loop would carry into the next case. Only its
`skills/addr-roof-tracing/` files map onto the project skill; always-on files describe the offline
case environment and are reported, never written. `--apply` writes the mapped files and leaves
deletions to the reviewer.
"""
from __future__ import annotations

import difflib
from pathlib import Path

from . import library as library_store
from . import surface
from .adapter import SKILL, SKILL_SOURCE


def adopted(lib_root, mode, domain, case=None):
    """(case slug, tree) of a specific library entry, or of the newest one in the domain."""
    lib = library_store.Library(root=Path(lib_root)/mode, domain=domain)
    entries = lib.entries()
    if not entries:
        raise ValueError(f'no adopted harness with a lesson in {lib.dir}')
    name = library_store.slug(case) if case else max(entries, key=lambda e: (lib.dir/'harnesses'/e).stat().st_mtime)
    if name not in entries:
        raise ValueError(f'{case!r} is not in {lib.dir}; entries: {entries[:8]}')
    return name, surface.read_tree(lib.dir/'harnesses'/name/'harness'), lib


def skill_changes(tree, repo=Path('.')):
    """{repo path: (before, after)} for every adopted skill file that differs from the project skill."""
    source, prefix = Path(repo)/SKILL_SOURCE, f'skills/{SKILL}/'
    live = {f'{prefix}{p.relative_to(source).as_posix()}': p.read_text(encoding='utf-8')
            for p in sorted(source.rglob('*')) if p.is_file() and surface.is_allowed(f'{prefix}{p.relative_to(source).as_posix()}')}
    changes = {}
    for rel in sorted(set(live) | {r for r in tree if r.startswith(prefix)}):
        before, after = live.get(rel), tree.get(rel)
        if before != after:
            changes[str(SKILL_SOURCE/rel[len(prefix):])] = (before, after)
    return changes


def patch(changes):
    return ''.join(line for path, (before, after) in changes.items() for line in difflib.unified_diff(
        (before or '').splitlines(keepends=True), (after or '').splitlines(keepends=True),
        fromfile=f'a/{path}' if before is not None else '/dev/null',
        tofile=f'b/{path}' if after is not None else '/dev/null'))


def other_changes(tree, base_tree):
    """Adopted edits outside the skill: case-environment text and new components, for the reviewer."""
    change = surface.diff_summary(base_tree, tree)
    return {key: [rel for rel in change[key] if not rel.startswith(f'skills/{SKILL}/')]
            for key in ('added', 'removed', 'modified')}


def apply(changes, repo=Path('.')):
    """Write added/modified skill files; deletions stay with the reviewer. Returns the paths written."""
    written = []
    for path, (_before, after) in changes.items():
        if after is None:
            continue
        target = Path(repo)/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(after, encoding='utf-8')
        written.append(path)
    return written


def lessons_markdown(lib):
    rows = [f'# Lessons — {lib.domain}', '']
    for case, cue, lesson, evidence, confidence in lib.lessons():
        rows += [f'## {case}', f'- cue: {cue}', f'- lesson: {lesson}', f'- evidence: {evidence}',
                 f'- confidence: {confidence or "unstated"}', '']
    return '\n'.join(rows)
