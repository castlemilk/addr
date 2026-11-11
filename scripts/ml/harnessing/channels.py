"""Improver sessions: one agent CLI working inside an isolated copy of an evidence tree.

The session runs in a temporary directory holding `context/`; edits under `context/harness/` are
harvested before it is removed. Calls are audited and content-addressed, so an identical rerun is
served from the audit. Improvers get file tools only: no shell, no web and no MCP servers, so no
session can reach the saved scenes. Adapted from Harness² core/proposer.py for the installed
claude, codex and opencode CLIs.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CHANNELS = ('claude', 'codex', 'opencode')
CONCURRENCY = int(os.environ.get('ADDR_HARNESS_IMPROVER_CONCURRENCY', '4'))
_SEM = threading.BoundedSemaphore(CONCURRENCY) if CONCURRENCY > 0 else None
# A nested CLI must not inherit the parent agent session's identity.
DROP_ENV = ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SSE_PORT')
POINTER = ('Read MISSION.md first. Do what it asks. Reply with the requested output as your final '
           'message AND save it verbatim to output/answer.md.')


OUTAGE = re.compile(r'usage.?limit|session limit|rate.?limit|overloaded|quota|\b(?:429|5\d\d)\b|ECONNRESET|ENOTFOUND|'
                    r'ETIMEDOUT|authenticat|unauthori[sz]ed|invalid api key', re.I)
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})


class ServiceUnavailable(RuntimeError):
    """The model service could not serve a session or rollout (usage limit, overload, auth, network).

    Never evidence about a harness: it aborts the phase without caching or recording anything, and
    repeating the command resumes. Never caught as an ordinary failure.
    """


def api_outage(payload):
    """A reason when a claude CLI result payload is a service error rather than the model's answer."""
    status = payload.get('api_error_status')
    if payload.get('is_error') and (status in TRANSIENT_STATUS or payload.get('api_error')):
        return f'API {status} {payload.get("api_error") or ""}: {str(payload.get("result") or "")[:160]}'.strip()
    return ''


class ImproverError(RuntimeError):
    """A session did not produce a usable reply; never swallowed into an empty string."""

    def __init__(self, message, *, attempts=0, cost_usd=0.0):
        super().__init__(message)
        self.attempts = attempts
        self.cost_usd = cost_usd


@dataclass(frozen=True)
class ChannelSpec:
    kind: str = 'claude'
    model: str = ''
    effort: str = 'high'
    timeout: int = 3600
    max_budget_usd: float | None = None

    def __post_init__(self):
        if self.kind not in CHANNELS:
            raise ValueError(f'unknown channel {self.kind!r}; expected one of {CHANNELS}')


@dataclass
class Reply:
    label: str
    text: str
    ok: bool
    cost_usd: float = 0.0
    wall_s: float = 0.0
    attempts: int = 0
    cached: bool = False
    channel: str = ''
    error: str = ''
    audit: Path | None = None
    workspace: Path | None = None

    def __bool__(self):
        return self.ok


def child_env(extra=None):
    env = {key: value for key, value in os.environ.items() if key not in DROP_ENV}
    env.update(extra or {})
    return env


def codex_defaults():
    """The user's configured codex model and effort; `--ignore-user-config` would drop them."""
    path = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'config.toml'
    try:
        config = tomllib.loads(path.read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError):
        return '', ''
    return str(config.get('model') or ''), str(config.get('model_reasoning_effort') or '')


def opencode_mcp_names():
    """Globally configured OpenCode MCP servers, so a session can disable each by name."""
    names = set()
    for name in ('opencode.json', 'opencode.jsonc'):
        path = Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'opencode'/name
        try:
            text = re.sub(r'(?m)^\s*//.*$', '', path.read_text(encoding='utf-8'))
            text = re.sub(r',(\s*[}\]])', r'\1', text)
            names |= set((json.loads(text).get('mcp') or {}).keys())
        except (OSError, ValueError, AttributeError):
            continue
    return sorted(names)


def parse_json(stdout):
    """The claude CLI's single JSON object, tolerating banners around it."""
    text = (stdout or '').strip()
    if not text:
        return {}
    for candidate in [text, *reversed(text.splitlines())]:
        candidate = candidate.strip()
        if candidate.startswith('{') and candidate.endswith('}'):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return {'result': text}


def jsonl_text(stdout):
    """Fold an opencode/codex JSONL stream into (final assistant text, error text, cost)."""
    texts, errors, cost = [], [], 0.0
    for line in (stdout or '').splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get('part') if isinstance(event.get('part'), dict) else {}
        item = event.get('item') if isinstance(event.get('item'), dict) else {}
        kind = part.get('type') or item.get('type') or event.get('type')
        if kind == 'text' and isinstance(part.get('text'), str):
            texts.append(part['text'])
        elif kind == 'agent_message' and isinstance(item.get('text'), str):
            texts.append(item['text'])
        elif kind in ('step-finish', 'step_finish') and isinstance(part.get('cost'), int | float):
            cost += float(part['cost'])
        elif kind == 'error' or event.get('type') == 'error':
            error = event.get('error') or event.get('message') or ''
            errors.append(str(error.get('message', error) if isinstance(error, dict) else error))
    return '\n\n'.join(t for t in texts if t.strip()), '; '.join(e for e in errors if e)[-300:], round(cost, 6)


def fingerprint_call(label, mission, context_dir):
    """Content-address one call: label, mission, the evidence bytes and its workspace path."""
    digest = hashlib.sha256()
    for value in (label, mission):
        digest.update(value.encode('utf-8', errors='replace') + b'\0')
    if context_dir:
        root = Path(context_dir)
        digest.update(str(root.resolve()).encode() + b'\0')
        for path in sorted(root.rglob('*')) if root.is_dir() else ():
            if path.is_file() and '__pycache__' not in path.parts:
                digest.update(path.relative_to(root).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()[:16]


@dataclass
class Improver:
    """Runs agentic improver sessions on one channel, with an audit trail and a cache."""

    spec: ChannelSpec
    audit_dir: Path | None = None
    attempts: int = 2
    calls: list[Reply] = field(default_factory=list)

    def call(self, mission, context_dir, label, *, timeout=None, max_attempts=None, harvest=None):
        """One session; raise ImproverError when no usable reply came back after the attempts.

        `harvest` names a subpath of the session directory to keep (e.g. "context/harness").
        """
        key = fingerprint_call(label, mission, context_dir)
        cached = self._cached(label, key, require_workspace=bool(harvest))
        if cached is not None:
            self.calls.append(cached)
            return cached
        wall = int(timeout or self.spec.timeout)
        tries = max(1, int(self.attempts if max_attempts is None else max_attempts))
        last, spent, started, outages = '', 0.0, time.time(), 0
        for attempt in range(1, tries + 1):
            if attempt > 1:
                time.sleep(10*(attempt - 1))
            tmp = Path(tempfile.mkdtemp(prefix='addr-harness-improver-'))
            try:
                if context_dir and Path(context_dir).is_dir():
                    shutil.copytree(context_dir, tmp/'context')
                with _SEM if _SEM is not None else contextlib.nullcontext():
                    text, cost, error = getattr(self, f'_{self.spec.kind}')(tmp, mission, wall)
                spent += cost
                if not text.strip():
                    last = error or 'empty reply'
                    outages += bool(error.startswith('OUTAGE '))
                    if 'usage limit' in error.lower() or 'session limit' in error.lower():
                        break  # waiting seconds cannot clear a usage limit
                    continue
                reply = Reply(label=label, text=text, ok=True, cost_usd=spent, attempts=attempt,
                              wall_s=round(time.time() - started, 1), channel=self.spec.kind)
                if harvest:
                    reply.workspace = self._harvest(tmp/harvest, label, key)
                self._write_audit(reply, mission, key)
                self.calls.append(reply)
                return reply
            except subprocess.TimeoutExpired:
                last = f'timeout after {wall}s'
            except Exception as exc:
                last = f'{type(exc).__name__}: {exc}'
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if outages and last.startswith('OUTAGE '):
            raise ServiceUnavailable(f'[{label}] {self.spec.kind}: {last[len("OUTAGE "):]}')
        reply = Reply(label=label, text='', ok=False, attempts=tries, channel=self.spec.kind, error=last,
                      cost_usd=spent, wall_s=round(time.time() - started, 1))
        self.calls.append(reply)
        self._write_audit(reply, mission, key)
        raise ImproverError(f'[{label}] {self.spec.kind}/{self.spec.model or "default"}: {last}',
                            attempts=tries, cost_usd=spent)

    def _claude(self, cwd, mission, wall):
        command = [shutil.which('claude') or 'claude', '-p', '--output-format', 'json',
                   '--permission-mode', 'acceptEdits', '--strict-mcp-config', '--no-session-persistence',
                   '--setting-sources', 'project,local', '--tools', 'Read,Edit,Write,Glob,Grep',
                   '--allowedTools', 'Read,Edit,Write,Glob,Grep', '--effort', self.spec.effort]
        if self.spec.model:
            command += ['--model', self.spec.model]
        if self.spec.max_budget_usd:
            command += ['--max-budget-usd', str(self.spec.max_budget_usd)]
        result = subprocess.run(command, input=mission, cwd=cwd, env=child_env(), text=True,
                                capture_output=True, timeout=wall)
        payload = parse_json(result.stdout)
        text = str(payload.get('result') or '')
        outage = api_outage(payload) or (OUTAGE.search(result.stderr or '') and not text.strip()
                                         and f'rc={result.returncode}: {(result.stderr or "")[-200:]}')
        if outage:
            return '', float(payload.get('total_cost_usd') or 0.0), f'OUTAGE {outage}'
        if payload.get('is_error') or payload.get('subtype') not in (None, 'success'):
            return '', float(payload.get('total_cost_usd') or 0.0), f'CLI error: {payload.get("subtype")} {text[:200]}'
        error = '' if text.strip() or result.returncode == 0 else f'rc={result.returncode}: {result.stderr[-300:]}'
        return text, float(payload.get('total_cost_usd') or 0.0), error

    def _codex(self, cwd, mission, wall):
        (cwd/'MISSION.md').write_text(mission, encoding='utf-8')
        (cwd/'output').mkdir(exist_ok=True)
        model, effort = codex_defaults()
        command = [shutil.which('codex') or 'codex', 'exec', '--json', '--skip-git-repo-check', '--ephemeral',
                   '--ignore-user-config', '--color', 'never', '-s', 'workspace-write', '-C', str(cwd),
                   '-o', str(cwd/'output'/'last.md'), '-c', 'sandbox_workspace_write.network_access=false',
                   '-c', f'model_reasoning_effort="{self.spec.effort or effort or "high"}"']
        if self.spec.model or model:
            command += ['-m', self.spec.model or model]
        result = subprocess.run(command + [POINTER], cwd=cwd, env=child_env(), text=True,
                                capture_output=True, timeout=wall)
        text, error, cost = jsonl_text(result.stdout)
        for name in ('answer.md', 'last.md'):
            path = cwd/'output'/name
            if path.is_file() and path.read_text(errors='replace').strip():
                return path.read_text(errors='replace'), cost, ''
        return text, cost, error or ('' if text else f'rc={result.returncode}: {result.stderr[-300:]}')

    def _opencode(self, cwd, mission, wall):
        (cwd/'MISSION.md').write_text(mission, encoding='utf-8')
        (cwd/'output').mkdir(exist_ok=True)
        (cwd/'opencode.json').write_text(json.dumps({
            '$schema': 'https://opencode.ai/config.json',
            'permission': {'edit': 'allow', 'bash': 'deny', 'webfetch': 'deny'},
            'mcp': {name: {'enabled': False} for name in opencode_mcp_names()}}), encoding='utf-8')
        command = [shutil.which('opencode') or 'opencode', 'run', '--standalone', '--format', 'json', '--auto',
                   '--title', 'addr-harness-improver']
        if self.spec.model:
            command += ['--model', self.spec.model]
        result = subprocess.run(command + [POINTER], cwd=cwd, env=child_env(), text=True,
                                capture_output=True, timeout=wall)
        text, error, cost = jsonl_text(result.stdout)
        answer = cwd/'output'/'answer.md'
        if answer.is_file() and answer.read_text(errors='replace').strip():
            return answer.read_text(errors='replace'), cost, ''
        return text, cost, error or ('' if text else f'rc={result.returncode}: {result.stderr[-300:]}')

    def _paths(self, label, key):
        if not self.audit_dir:
            return None
        root = Path(self.audit_dir)
        root.mkdir(parents=True, exist_ok=True)
        stem = f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', label)}.{key}"
        return {'mission': root/f'{stem}.mission.md', 'response': root/f'{stem}.response.md',
                'meta': root/f'{stem}.meta.json', 'workspace': root/f'{stem}.workspace'}

    def _harvest(self, src, label, key):
        if not src.is_dir():
            return None
        paths = self._paths(label, key)
        dest = paths['workspace'] if paths else Path(tempfile.mkdtemp(prefix='addr-harness-harvest-'))/'workspace'
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        return dest

    def _cached(self, label, key, *, require_workspace=False):
        paths = self._paths(label, key)
        if not paths or not paths['response'].is_file():
            return None
        text = paths['response'].read_text(encoding='utf-8', errors='replace')
        if not text.strip() or (require_workspace and not paths['workspace'].is_dir()):
            return None
        return Reply(label=label, text=text, ok=True, cached=True, channel=self.spec.kind,
                     audit=paths['response'], workspace=paths['workspace'] if paths['workspace'].is_dir() else None)

    def _write_audit(self, reply, mission, key):
        paths = self._paths(reply.label, key)
        if not paths:
            return
        paths['mission'].write_text(mission, encoding='utf-8')
        paths['response'].write_text(reply.text, encoding='utf-8')
        paths['meta'].write_text(json.dumps({
            'label': reply.label, 'ok': reply.ok, 'channel': reply.channel, 'model': self.spec.model,
            'effort': self.spec.effort, 'attempts': reply.attempts, 'wall_s': reply.wall_s,
            'cost_usd': reply.cost_usd, 'error': reply.error}, indent=2), encoding='utf-8')
        reply.audit = paths['response']
