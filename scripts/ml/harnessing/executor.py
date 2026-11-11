"""Rollout executors: stage a harness tree in an agent CLI's native layout and run one case.

A rollout runs in a fresh temporary workspace outside the checkout, so no repo AGENTS.md/CLAUDE.md,
project MCP server or skill reaches it; the only tools beyond file reading are the `addr-case`
server for its case. Claude Code is the reference executor (skills, subagents and hooks); Codex
and OpenCode stage skills and the always-on text, without hooks.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import surface
from .channels import (
    OUTAGE,
    ServiceUnavailable,
    api_outage,
    child_env,
    codex_defaults,
    opencode_mcp_names,
)

__all__ = ['ServiceUnavailable']

REPO = Path(__file__).resolve().parents[3]
EXECUTORS = ('claude', 'codex', 'opencode')
SKILL_DIRS = {'claude': '.claude/skills', 'codex': '.agents/skills', 'opencode': '.opencode/skills'}
DOCUMENT = {'claude': 'CLAUDE.md', 'codex': 'AGENTS.md', 'opencode': 'AGENTS.md'}
SECTIONS = (('guardrails.md', 'Guardrails'), ('MEMORY.md', 'Memory'), ('tool_guidance.md', 'Tool guidance'))
CLAUDE_TOOLS = ('Read', 'Glob', 'Grep', 'Skill', 'Bash', 'Write', 'Edit')
CLAUDE_ALLOWED = ('Read', 'Glob', 'Grep', 'Skill', 'Write', 'Edit', 'Agent', 'Bash(python3:*)', 'Bash(python:*)',
                  'mcp__addr-case')
TRUNC = '…(+{n} chars — render truncation)'


@dataclass(frozen=True)
class ExecutorSpec:
    kind: str = 'claude'
    model: str = ''
    effort: str = ''
    timeout: int = 1800
    max_budget_usd: float | None = None

    def __post_init__(self):
        if self.kind not in EXECUTORS:
            raise ValueError(f'unknown executor {self.kind!r}; expected one of {EXECUTORS}')

    @property
    def allow_subagents(self):
        return self.kind in ('claude', 'opencode')

    @property
    def allow_hooks(self):
        return self.kind == 'claude'


def python():
    venv = REPO/'scripts/ml/.venv/bin/python'
    return str(venv if venv.is_file() else sys.executable)


def case_server_argv(workspace):
    return ['-m', 'scripts.ml.harnessing.case_server', '--case', str(workspace/'case'), '--run', str(workspace/'run')]


def always_on_document(tree, *, skills_index=False):
    """The always-on files as one standing document, in a fixed order."""
    parts = [tree.get('AGENTS.md', '').strip()]
    parts += [f'# {title}\n\n{tree[name].strip()}' for name, title in SECTIONS if tree.get(name, '').strip()]
    if skills_index and surface.skill_names(tree):
        rows = [f'- `{name}` (`skills/{name}/SKILL.md`): '
                f'{surface.frontmatter(tree[f"skills/{name}/SKILL.md"]).get("description", "")}'
                for name in surface.skill_names(tree)]
        parts.append('# Skills\n\nRead the SKILL.md of a skill when its situation applies:\n' + '\n'.join(rows))
    return '\n\n---\n\n'.join(part for part in parts if part) + '\n'


def _with_frontmatter(text, required):
    """Ensure leading frontmatter carries `required` keys, keeping every existing line."""
    fields = surface.frontmatter(text)
    extra = ''.join(f'{key}: {value}\n' for key, value in required.items() if key not in fields)
    if text.lstrip().startswith('---'):
        head, sep, rest = text.lstrip()[3:].partition('\n')
        return f'---{head}{sep}{extra}{rest}'
    return f'---\n{extra}---\n\n{text}'


def materialise(tree, workspace, kind):
    """Write the tree into `workspace` in the executor's native layout; return the staged paths."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    staged = {DOCUMENT[kind]: always_on_document(tree, skills_index=kind != 'claude')}
    for rel, text in sorted(tree.items()):
        if rel.startswith('skills/'):
            staged[f'{SKILL_DIRS[kind]}/{rel[len("skills/"):]}'] = text
        elif surface.SCRIPT.fullmatch(rel):
            staged[rel] = text
        elif (match := surface.SUBAGENT.fullmatch(rel)) and kind == 'claude':
            staged[f'.claude/agents/{match.group(1)}.md'] = _with_frontmatter(text, {'name': match.group(1)})
        elif match and kind == 'opencode':
            staged[f'.opencode/agent/{match.group(1)}.md'] = _with_frontmatter(text, {'mode': 'subagent'})
        elif rel == surface.HOOKS and kind == 'claude':
            staged['.claude/settings.json'] = json.dumps({'hooks': json.loads(text)['hooks']}, indent=2)
    for rel, text in staged.items():
        path = workspace/rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    link = workspace/'skills'
    if not link.exists():
        (workspace/SKILL_DIRS[kind]).mkdir(parents=True, exist_ok=True)
        link.symlink_to(SKILL_DIRS[kind], target_is_directory=True)
    return sorted(staged)


def command(spec, workspace, tree):
    """(argv, stdin) for one rollout of `spec.kind` in a materialised workspace."""
    workspace = Path(workspace)
    if spec.kind == 'claude':
        config = workspace/'.case-mcp.json'
        config.write_text(json.dumps({'mcpServers': {'addr-case': {
            'command': python(), 'args': case_server_argv(workspace), 'env': {'PYTHONPATH': str(REPO)}}}}))
        tools = [*CLAUDE_TOOLS, *(['Agent'] if surface.subagent_names(tree) else [])]
        argv = [shutil.which('claude') or 'claude', '-p', '--output-format', 'stream-json', '--verbose',
                '--no-session-persistence', '--setting-sources', 'project,local', '--strict-mcp-config',
                '--mcp-config', str(config), '--tools', ','.join(tools), '--allowedTools', ','.join(CLAUDE_ALLOWED),
                '--permission-mode', 'dontAsk']
        argv += ['--model', spec.model] if spec.model else []
        argv += ['--effort', spec.effort] if spec.effort else []
        argv += ['--max-budget-usd', str(spec.max_budget_usd)] if spec.max_budget_usd else []
        return argv, True
    if spec.kind == 'codex':
        model, effort = codex_defaults()
        server = 'mcp_servers.addr-case'
        argv = [shutil.which('codex') or 'codex', 'exec', '--json', '--skip-git-repo-check', '--ephemeral',
                '--ignore-user-config', '--color', 'never', '-s', 'workspace-write', '-C', str(workspace),
                '-o', str(workspace/'run'/'last_message.md'), '-c', 'sandbox_workspace_write.network_access=false',
                '-c', f'{server}.command={json.dumps(python())}', '-c', f'{server}.args={json.dumps(case_server_argv(workspace))}',
                '-c', f'{server}.env={{PYTHONPATH={json.dumps(str(REPO))}}}', '-c', f'{server}.startup_timeout_sec=60',
                '-c', f'model_reasoning_effort="{spec.effort or effort or "high"}"']
        argv += ['-m', spec.model or model] if spec.model or model else []
        return argv + ['-'], True
    (workspace/'opencode.json').write_text(json.dumps({
        '$schema': 'https://opencode.ai/config.json',
        'mcp': {'addr-case': {'type': 'local', 'command': [python(), *case_server_argv(workspace)],
                              'environment': {'PYTHONPATH': str(REPO)}, 'enabled': True},
                **{name: {'enabled': False} for name in opencode_mcp_names() if name != 'addr-case'}},
        'permission': {'edit': 'allow', 'webfetch': 'deny',
                       'bash': {'python3 *': 'allow', 'python *': 'allow', '*': 'deny'}}}, indent=2))
    argv = [shutil.which('opencode') or 'opencode', 'run', '--standalone', '--format', 'json', '--auto', '--title',
            'addr-harness-rollout']
    return argv + (['--model', spec.model] if spec.model else []), False


def launch(spec, workspace, tree, prompt, *, stream, stderr, timeout=None):
    """Run one rollout, streaming events to `stream`; return run metadata. A timeout is recorded."""
    argv, stdin = command(spec, workspace, tree)
    if not stdin:
        argv = [*argv, prompt]
    started, timed_out, returncode = time.time(), False, None
    # The agent's `python3` is the repo venv (Pillow, numpy, shapely), as the tool guidance promises.
    env = child_env({'PYTHONDONTWRITEBYTECODE': '1',
                     'PATH': f'{Path(python()).parent}{os.pathsep}{os.environ.get("PATH", "")}'})
    with open(stream, 'w', encoding='utf-8') as out, open(stderr, 'w', encoding='utf-8') as err:
        process = subprocess.Popen(argv, cwd=workspace, env=env, text=True,
                                   stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            process.communicate(input=prompt if stdin else None, timeout=timeout or spec.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.communicate()
        returncode = process.returncode
    return {'executor': spec.kind, 'model': spec.model, 'effort': spec.effort, 'returncode': returncode,
            'timed_out': timed_out, 'wall_s': round(time.time() - started, 1),
            'cost_usd': rollout_cost(stream, spec.kind), 'argv': [a if len(a) < 400 else a[:400] + '…' for a in argv],
            'outage': outage(stream, spec.kind, returncode, stderr)}


def outage(stream, kind, returncode, stderr=None):
    """A reason when the executor's service, not the agent, failed this rollout; '' otherwise."""
    acted = False
    for event in _events(stream):
        if kind == 'claude':
            if event.get('type') == 'result' and (reason := api_outage(event)):
                return reason
            acted = acted or event.get('type') == 'assistant'
        elif kind == 'codex' and event.get('type') in ('error', 'turn.failed'):
            text = json.dumps(event.get('error') or event.get('message'))
            if OUTAGE.search(text):
                return f'codex: {text[:200]}'
        elif kind == 'opencode' and isinstance(event.get('error'), dict):
            text = json.dumps(event['error'])
            if OUTAGE.search(text):
                return f'opencode: {text[:200]}'
        acted = acted or kind != 'claude'
    try:
        tail = Path(stderr).read_text(encoding='utf-8', errors='replace')[-600:] if stderr else ''
    except OSError:
        tail = ''
    if returncode not in (0, None) and not acted and OUTAGE.search(tail):
        return f'rc={returncode}: {tail[-200:].strip()}'
    return ''


def _clip(value, limit):
    text = '' if value is None else str(value).replace('\x00', '')
    return text if limit <= 0 or len(text) <= limit else text[:limit] + TRUNC.format(n=len(text) - limit)


def _events(path):
    try:
        raw = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith('{'):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def _block_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict):
            parts.append(block.get('text') if block.get('type') == 'text' else f'({block.get("type", "block")})')
    return '\n'.join(p for p in parts if p)


def _claude_lines(path, obs, args):
    for event in _events(path):
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            servers = ', '.join(f'{s.get("name")}={s.get("status")}' for s in event.get('mcp_servers') or [])
            yield f'[SYSTEM] model={event.get("model")} mcp: {servers} skills: {event.get("skills") or []}'
        elif kind == 'assistant':
            for block in (event.get('message') or {}).get('content') or []:
                if block.get('type') == 'text' and str(block.get('text', '')).strip():
                    yield f'[ASSISTANT] {_clip(block["text"], obs)}'
                elif block.get('type') == 'tool_use':
                    yield f'[ACTION] {block.get("name")}({_clip(json.dumps(block.get("input"), ensure_ascii=False), args)})'
        elif kind == 'user':
            content = (event.get('message') or {}).get('content')
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    tag = '[OBSERVATION ERROR]' if block.get('is_error') else '[OBSERVATION]'
                    yield f'{tag} {_clip(_block_text(block.get("content")), obs)}'
        elif kind == 'result':
            yield (f'[RESULT] {event.get("subtype")} turns={event.get("num_turns")} '
                   f'cost_usd={event.get("total_cost_usd")} {_clip(event.get("result"), 2000)}')


def _codex_lines(path, obs, args):
    for event in _events(path):
        item = event.get('item') if isinstance(event.get('item'), dict) else None
        if event.get('type') == 'item.completed' and item:
            kind = item.get('type')
            if kind == 'agent_message':
                yield f'[ASSISTANT] {_clip(item.get("text"), obs)}'
            elif kind == 'reasoning':
                yield f'[THINKING] {_clip(item.get("text"), 4000)}'
            elif kind == 'command_execution':
                yield f'[ACTION] shell({_clip(item.get("command"), args)})'
                yield f'[OBSERVATION] exit={item.get("exit_code")} {_clip(item.get("aggregated_output"), obs)}'
            elif kind == 'mcp_tool_call':
                yield f'[ACTION] {item.get("server")}.{item.get("tool")}({_clip(json.dumps(item.get("arguments")), args)})'
                if item.get('error'):
                    yield f'[OBSERVATION ERROR] {_clip(json.dumps(item.get("error")), obs)}'
                else:
                    yield f'[OBSERVATION] {_clip(_block_text((item.get("result") or {}).get("content")), obs)}'
            elif kind:
                yield f'[EVENT] {kind} {_clip(json.dumps(item, ensure_ascii=False), args)}'
        elif event.get('type') in ('error', 'turn.failed'):
            yield f'[ERROR] {_clip(json.dumps(event.get("error") or event.get("message")), 600)}'


def _opencode_lines(path, obs, args):
    for event in _events(path):
        part = event.get('part') if isinstance(event.get('part'), dict) else event
        kind = part.get('type', event.get('type'))
        if isinstance(event.get('error'), dict):
            yield f'[ERROR] {_clip((event["error"].get("data") or {}).get("message") or event["error"], 600)}'
        elif kind in ('text', 'reasoning') and str(part.get('text') or '').strip():
            yield f'[{"ASSISTANT" if kind == "text" else "THINKING"}] {_clip(part["text"], obs)}'
        elif kind in ('tool', 'tool_use'):
            state = part.get('state') if isinstance(part.get('state'), dict) else part
            yield f'[ACTION] {part.get("tool") or "tool"}({_clip(json.dumps(state.get("input"), default=str), args)})'
            if state.get('status') == 'error':
                yield f'[OBSERVATION ERROR] {_clip(state.get("error"), obs)}'
            elif state.get('output') is not None:
                yield f'[OBSERVATION] {_clip(state.get("output"), obs)}'


def render_trajectory(path, kind, *, obs_trunc=50_000, max_chars=600_000, args_trunc=2000):
    """One executor's event stream in the shared [ACTION]/[OBSERVATION]/[ERROR] grammar."""
    reader = {'claude': _claude_lines, 'codex': _codex_lines, 'opencode': _opencode_lines}[kind]
    text = '\n'.join(reader(path, obs_trunc, args_trunc)).strip()
    if not text:
        return '(no trajectory: the executor produced no parseable events)'
    return text if len(text) <= max_chars else text[:max_chars] + '\n' + TRUNC.format(n=len(text) - max_chars)


def rollout_cost(path, kind):
    total = 0.0
    for event in _events(path):
        if kind == 'claude' and event.get('type') == 'result':
            total += float(event.get('total_cost_usd') or 0.0)
        elif kind == 'opencode':
            part = event.get('part') if isinstance(event.get('part'), dict) else {}
            if (part.get('type') or event.get('type')) in ('step-finish', 'step_finish'):
                total += float(part.get('cost') or 0.0)
    return round(total, 6)


def preflight(spec):
    """Problems that stop a rollout before money is spent; an empty list is ready."""
    problems = [] if shutil.which(spec.kind) else [f'`{spec.kind}` is not on PATH']
    if not Path(python()).is_file():
        problems.append(f'{python()} is missing; run `task setup:ml`')
    if spec.kind == 'opencode' and not spec.model:
        problems.append('opencode needs --model provider/model (the global default may not be vision-capable)')
    return problems
