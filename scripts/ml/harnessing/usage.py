"""Which harness components a rollout actually used: reference-free evidence for improvers.

A component in the always-on files is in context by construction; every other component is used
only if the trajectory shows it: a skill invoked or its SKILL.md read, a reference or helper file
read, a script run. An edit to a component no rollout touched changed nothing, whatever it says.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import surface


def tool_inputs(stream, kind):
    """(tool name, input as text) for every tool call recorded in an executor event stream."""
    try:
        lines = Path(stream).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return
    for line in lines:
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if kind == 'claude' and event.get('type') == 'assistant':
            for block in (event.get('message') or {}).get('content') or []:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    yield str(block.get('name')), json.dumps(block.get('input'), ensure_ascii=False)
        elif kind == 'codex' and event.get('type') == 'item.completed':
            item = event.get('item') or {}
            if item.get('type') == 'command_execution':
                yield 'shell', str(item.get('command'))
            elif item.get('type') == 'mcp_tool_call':
                yield f'{item.get("server")}.{item.get("tool")}', json.dumps(item.get('arguments'))
        elif kind == 'opencode':
            part = event.get('part') if isinstance(event.get('part'), dict) else event
            if part.get('type') in ('tool', 'tool_use'):
                state = part.get('state') if isinstance(part.get('state'), dict) else part
                yield str(part.get('tool')), json.dumps(state.get('input'), default=str)


def component_usage(stream, kind, tree):
    """rel -> {'how': 'always-on'|'used'|'unused', 'via': [...]} for every component in `tree`."""
    calls = list(tool_inputs(stream, kind))
    out = {}
    for rel in sorted(tree):
        if rel in surface.ALWAYS_ON:
            out[rel] = {'how': 'always-on', 'via': []}
            continue
        via = []
        skill = surface.SKILL.fullmatch(rel)
        needle = rel[len('skills/'):] if rel.startswith('skills/') else rel
        for name, text in calls:
            if skill and name == 'Skill' and re.search(rf'"{re.escape(skill.group(1))}"', text):
                via.append('invoked')
            elif needle in text or (rel.startswith('sub_agents/') and rel.split('/')[1] in text and name in
                                    ('Agent', 'Task', 'task')):
                via.append('ran' if rel.endswith('.py') and name in ('Bash', 'shell', 'bash') else 'read')
        out[rel] = {'how': 'used' if via else 'unused', 'via': sorted(set(via))}
    return out


def render(usages):
    """A component x rollout matrix: label -> component usage, as markdown."""
    labels = sorted(usages)
    if not labels:
        return ''
    rels = sorted({rel for usage in usages.values() for rel in usage})
    rows = ['# Component usage by rollout', '',
            'Derived from each trajectory: `invoked` / `read` / `ran` = the rollout used it; `-` = present but '
            'never touched; blank = not in that rollout\'s harness. Always-on files are in context on every '
            'run. An edit to a component its rollout never touched cannot explain a behaviour change.', '',
            '| component | ' + ' | '.join(labels) + ' |', '|---|' + '---|'*len(labels)]
    for rel in rels:
        cells = []
        for label in labels:
            entry = usages[label].get(rel)
            cells.append('' if entry is None else 'always-on' if entry['how'] == 'always-on'
                         else '/'.join(entry['via']) if entry['via'] else '-')
        rows.append(f'| `{rel}` | ' + ' | '.join(cells) + ' |')
    return '\n'.join(rows) + '\n'
