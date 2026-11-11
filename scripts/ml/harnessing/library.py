"""The per-domain experience library: what earlier cases in a domain adopted, and why.

    <root>/<mode>/<domain>/INDEX.md
    <root>/<mode>/<domain>/harnesses/<case>/harness/<rel>   the full adopted tree
    <root>/<mode>/<domain>/harnesses/<case>/EXPERIENCE.md   why it was picked + the lesson

Retrieval is by construction: a domain directory only holds same-domain entries, and the whole
tree is carried, never a summary. Adapted from Harness² core/library.py.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import context, surface

FIELDS = ('cue', 'lesson', 'evidence', 'confidence')


def slug(task_id):
    return task_id.replace('/', '__')


def has_experience(experience):
    return isinstance(experience, dict) and all(
        isinstance(experience.get(key), str) and experience[key].strip() for key in FIELDS[:3])


def domain_dir(domain):
    return re.sub(r'\s*[/\\]\s*', '__', domain).strip() or 'unknown'


@dataclass
class Library:
    root: Path
    domain: str

    @property
    def dir(self):
        return Path(self.root)/domain_dir(self.domain)

    @property
    def index(self):
        return self.dir/'INDEX.md'

    def entry_dir(self, task_id):
        return self.dir/'harnesses'/slug(task_id)

    def context_dir(self):
        """The directory handed to an improver session, or None while this domain is empty."""
        return self.dir if self.index.is_file() and any(self.dir.rglob('EXPERIENCE.md')) else None

    def entries(self):
        folder = self.dir/'harnesses'
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []

    def tree_of(self, task_id):
        return surface.read_tree(self.entry_dir(task_id)/'harness')

    def retain(self, task_ids):
        """Keep only entries for these cases (the ones preceding the current case in its stream)."""
        keep = {slug(task_id) for task_id in task_ids}
        stale = [name for name in self.entries() if name not in keep]
        for name in stale:
            shutil.rmtree(self.dir/'harnesses'/name, ignore_errors=True)
        if stale:
            self.rebuild_index()

    def add(self, task_id, harness, *, why='', label='', run_id='', experience=None):
        """Store one adopted harness with why it was adopted; only a supported lesson is stored."""
        if not has_experience(experience):
            return None
        context.assert_tree_has_no_verdicts(harness)
        context.assert_tree_has_no_verdicts({'EXPERIENCE.md': str(experience) + why})
        entry = self.entry_dir(task_id)
        shutil.rmtree(entry/'harness', ignore_errors=True)
        surface.write_tree(harness, entry/'harness', stubs=False)
        lines = [f'# {task_id}', '', f'- adopted: `{label}`' + (f'  (run `{run_id}`)' if run_id else ''),
                 f'- components: {surface.inventory(harness)}', '']
        if why.strip():
            lines += ['## Why this was chosen', why.strip(), '']
        if experience.get('edits'):
            lines += ['## What each edit did', str(experience['edits']).strip(), '']
        lines += ['## Transferable lesson'] + [f'- **{key}**: {str(experience[key]).strip()}'
                                              for key in FIELDS if experience.get(key)] + ['']
        (entry/'EXPERIENCE.md').write_text('\n'.join(lines), encoding='utf-8')
        self.rebuild_index()
        return entry

    def rebuild_index(self):
        rows = [f'# Library — {self.domain}', '',
                'Each entry is an earlier case in THIS domain whose harness was adopted, stored with its '
                'FULL component tree and why it was picked. Browse this index, then open the entries '
                'that look relevant to the case in front of you.', '']
        names = self.entries()
        if not names:
            rows.append('_(empty — this is the first case in this domain)_')
        for name in names:
            folder = self.dir/'harnesses'/name
            cue = _field(folder/'EXPERIENCE.md', 'cue')
            rows += [f'## `harnesses/{name}/`',
                     f'- harness: `harnesses/{name}/harness/` — {surface.inventory(surface.read_tree(folder/"harness"))}',
                     f'- why:     `harnesses/{name}/EXPERIENCE.md`'] + ([f'- cue:     {cue}'] if cue else []) + ['']
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index.write_text('\n'.join(rows), encoding='utf-8')
        return self.index

    def lessons(self):
        """(case, cue, lesson, evidence, confidence) rows, for reports and promotion."""
        return [(name, *(_field(self.dir/'harnesses'/name/'EXPERIENCE.md', key) for key in FIELDS))
                for name in self.entries()]


def _field(path, name):
    if not path.is_file():
        return ''
    prefix = f'- **{name}**:'
    return next((line[len(prefix):].strip() for line in path.read_text(encoding='utf-8').splitlines()
                 if line.startswith(prefix)), '')
