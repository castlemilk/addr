"""The editable harness tree: which paths a proposal may write, validation, read/write and diff.

A harness is a flat `rel -> text` mapping. A proposal edits files in place and inherits every file
it leaves alone. Adapted from Harness² core/tree.py: addr skills carry `references/`, `scripts/`
and `agents/` files, and the tree is executor-neutral until `executor.materialise` stages it.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

Tree = dict[str, str]

ALWAYS_ON = ('AGENTS.md', 'guardrails.md', 'MEMORY.md', 'tool_guidance.md')
ALWAYS_ON_MAX = 24_000
NAME = r'[a-z0-9][a-z0-9_\-]*'
SKILL = re.compile(rf'skills/({NAME})/SKILL\.md')
SKILL_FILE = re.compile(rf'skills/({NAME})/(?:references|scripts|agents)/[A-Za-z0-9_.\-]+')
SUBAGENT = re.compile(rf'sub_agents/({NAME})/AGENT\.md')
SCRIPT = re.compile(r'scripts/[a-z0-9_][a-z0-9_]*\.py')
HOOKS = 'hooks.json'
HOOK_EVENTS = frozenset({'PreToolUse', 'PostToolUse', 'SessionStart', 'Stop', 'SubagentStop',
                         'UserPromptSubmit', 'PreCompact', 'Notification'})
SCRIPT_REF = re.compile(r'python3?\s+(\S*?(?:skills/[\w\-]+/)?scripts/[\w.\-]+\.py)')
LINK = re.compile(r'\]\((?!https?:|mailto:|#|/|\.\./)([^)\s#]+)')
CASE_FACTS = '## Case facts (this case only)'
TRIGGER_LINE = '- When {desc}, load the `{name}` skill.'
STUBS = {
    'scripts/README.md': '<!-- Deterministic helpers, run as `python3 scripts/<name>.py`. Add one as '
                         'scripts/<name>.py. -->\n',
    'sub_agents/README.md': '<!-- Delegated subagents that run in their OWN context and return text. '
                            'Add one as sub_agents/<name>/AGENT.md with `description:` '
                            'frontmatter. -->\n',
    'skills/README.md': '<!-- On-demand workflows. Add one as skills/<name>/SKILL.md with frontmatter '
                        '`name:` matching the directory and a `description:`; its references/, '
                        'scripts/ and agents/ files travel with it. -->\n',
}


def normalize_rel(rel):
    """A relative path inside the harness; escapes are rejected before any join."""
    text = str(rel).strip().replace('\\', '/')
    if text.startswith('/') or '..' in text.split('/'):
        raise ValueError(f'path escapes the harness: {rel!r}')
    while text.startswith('./'):
        text = text[2:]
    return text.strip('/')


def is_allowed(rel):
    try:
        rel = normalize_rel(rel)
    except ValueError:
        return False
    return rel in ALWAYS_ON or rel == HOOKS or any(
        pattern.fullmatch(rel) for pattern in (SKILL, SKILL_FILE, SUBAGENT, SCRIPT))


def skill_names(tree):
    return sorted({m.group(1) for rel in tree if (m := SKILL.fullmatch(rel))})


def subagent_names(tree):
    return sorted({m.group(1) for rel in tree if (m := SUBAGENT.fullmatch(rel))})


def frontmatter(text):
    """`key: value` lines of a leading `---` block; nested YAML is not interpreted."""
    match = re.match(r'\s*---\s*\n(.*?)\n---\s*(?:\n|$)', text or '', re.S)
    fields = {}
    for line in (match.group(1).splitlines() if match else ()):
        key, sep, value = line.partition(':')
        if sep and key.strip() and not key.startswith((' ', '\t', '-')):
            fields[key.strip()] = value.strip().strip('"\'')
    return fields


def read_tree(directory, *, rejected=None):
    """rel -> text for every editable file under `directory`; README stubs are not components."""
    directory = Path(directory)
    out = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.name == '.DS_Store':
            continue
        rel = path.relative_to(directory).as_posix()
        if rel in STUBS:
            continue
        if not is_allowed(rel):
            if rejected is not None:
                rejected.append(rel)
            continue
        out[rel] = path.read_text(encoding='utf-8', errors='replace')
    return out


def is_tree(directory):
    directory = Path(directory)
    return directory.is_dir() and any((directory/name).is_file() for name in ALWAYS_ON)


def write_tree(tree, dest, *, stubs=True):
    """Write exactly `tree`, replacing editable files already at `dest` and keeping the rest."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(dest.rglob('*'), reverse=True):
        rel = path.relative_to(dest).as_posix()
        if path.is_file() and (is_allowed(rel) or rel in STUBS):
            path.unlink()
    for name in ALWAYS_ON:
        if name not in tree:
            (dest/name).write_text('', encoding='utf-8')
    for rel, text in tree.items():
        path = dest/normalize_rel(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    for rel, text in (STUBS.items() if stubs else ()):
        path = dest/rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(text, encoding='utf-8')
    return dest


def always_on_text(tree):
    return '\n\n'.join(tree.get(name, '') for name in ALWAYS_ON)


def autotrigger(tree):
    """Give every skill a trigger line in guardrails.md when the cap allows; return the names."""
    fixed = []
    for name in skill_names(tree):
        if name in always_on_text(tree):
            continue
        desc = frontmatter(tree[f'skills/{name}/SKILL.md']).get('description', '')
        line = TRIGGER_LINE.format(name=name, desc=desc.rstrip('.') or f'the {name} workflow applies')
        if len(always_on_text(tree)) + len(line) + 2 > ALWAYS_ON_MAX:
            continue
        current = tree.get('guardrails.md', '')
        tree['guardrails.md'] = f'{current.rstrip()}\n{line}\n' if current.strip() else f'{line}\n'
        fixed.append(name)
    return fixed


def _check_hooks(text, errors):
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(f'{HOOKS} is not valid JSON: {exc}')
        return
    hooks = doc.get('hooks') if isinstance(doc, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        errors.append(f'{HOOKS} needs a top-level {{"hooks": {{"<Event>": [...]}}}} object')
        return
    for event, entries in hooks.items():
        if event not in HOOK_EVENTS:
            errors.append(f'{HOOKS}: unknown event {event!r}; use one of {", ".join(sorted(HOOK_EVENTS))}')
        elif not isinstance(entries, list):
            errors.append(f'{HOOKS}: {event} must map to a LIST of matcher entries')
        else:
            for index, entry in enumerate(entries):
                commands = entry.get('hooks') if isinstance(entry, dict) else None
                if not isinstance(commands, list):
                    errors.append(f'{HOOKS}: {event}[{index}] needs a `hooks` list')
                elif any(not isinstance(h, dict) or h.get('type') != 'command'
                         or not str(h.get('command', '')).strip() for h in commands):
                    errors.append(f'{HOOKS}: {event}[{index}] hooks must be non-empty "command" entries')


def _script_target(ref):
    return ref[ref.find('skills/'):] if 'skills/' in ref else ref[ref.find('scripts/'):]


def validate(tree, *, allow_subagents=True, allow_hooks=True):
    """Raise ValueError naming every problem (it is fed back verbatim); return a census."""
    errors = []
    body = always_on_text(tree)
    if len(body) > ALWAYS_ON_MAX:
        errors.append(f'always-on content is {len(body)} chars, over the {ALWAYS_ON_MAX} cap; every '
                      'always-on token competes with the agent\'s own inspection budget')
    if not tree.get('AGENTS.md', '').strip():
        errors.append('AGENTS.md is empty or missing; it carries the standing instruction for every run')
    skills = skill_names(tree)
    for name in skills:
        fields = frontmatter(tree[f'skills/{name}/SKILL.md'])
        if not fields.get('description'):
            errors.append(f'skills/{name}/SKILL.md needs a `description:` in its frontmatter, or the '
                          'skill is dropped SILENTLY at load')
        if fields.get('name') != name:
            errors.append(f'skills/{name}/SKILL.md needs frontmatter `name: {name}` matching its directory')
        if name not in body:
            errors.append(f"skill '{name}' is never named in the always-on text, so the agent will not "
                          f'load it. Add a trigger line such as "When <situation>, load the `{name}` '
                          'skill." to AGENTS.md or guardrails.md')
    orphans = sorted({m.group(1) for rel in tree if (m := SKILL_FILE.fullmatch(rel))} - set(skills))
    if orphans:
        errors.append(f'files under skills/{orphans[0]}/ belong to no SKILL.md: {orphans}')
    subagents = subagent_names(tree)
    if subagents and not allow_subagents:
        errors.append(f'sub_agents are not available on this executor: {subagents}')
    for name in subagents:
        if not frontmatter(tree[f'sub_agents/{name}/AGENT.md']).get('description'):
            errors.append(f'sub_agents/{name}/AGENT.md needs a `description:` in its frontmatter')
    if HOOKS in tree:
        if allow_hooks:
            _check_hooks(tree[HOOKS], errors)
        else:
            errors.append(f'{HOOKS} is not available on this executor')
    for rel, text in tree.items():
        if rel.endswith('.py'):
            try:
                compile(text, rel, 'exec')
            except (SyntaxError, ValueError) as exc:
                errors.append(f'{rel} does not compile: {type(exc).__name__}: {exc}'[:200])
    for rel, text in tree.items():
        if not (rel in ALWAYS_ON or (rel.startswith('skills/') and rel.endswith('.md'))):
            continue
        for ref in SCRIPT_REF.findall(text):
            if _script_target(ref) not in tree:
                errors.append(f'{rel} invokes `{ref}` but {_script_target(ref)} is not in the harness')
        if rel.startswith('skills/'):
            for link in LINK.findall(text):
                target = (PurePosixPath(rel).parent/link).as_posix()
                if link.endswith(('.md', '.py', '.json', '.yaml', '.txt')) and target not in tree:
                    errors.append(f'{rel} links to `{link}` but {target} is not in the harness')
    if errors:
        raise ValueError(' | '.join(errors))
    return {'always_on_chars': len(body), 'n_files': len(tree), 'skills': len(skills),
            'sub_agents': len(subagents), 'scripts': sum(1 for rel in tree if rel.endswith('.py')),
            'hooks': int(HOOKS in tree)}


def diff_summary(before, after):
    return {'added': sorted(set(after) - set(before)), 'removed': sorted(set(before) - set(after)),
            'modified': sorted(rel for rel in set(before) & set(after) if before[rel] != after[rel]),
            'always_on_char_delta': len(always_on_text(after)) - len(always_on_text(before))}


def unchanged(before, after):
    change = diff_summary(before, after)
    return not (change['added'] or change['removed'] or change['modified'])


def diversity(trees):
    """Pairwise line-Jaccard over candidate trees, so collapse to one body is visible."""
    names = sorted(trees)
    if len(names) < 2:
        return {'n': len(names), 'n_distinct': len(names), 'mean_line_jaccard': 1.0}
    lines = {name: {f'{rel}:{line}' for rel, text in trees[name].items()
                    for line in text.splitlines() if line.strip()} for name in names}
    sims = [len(lines[a] & lines[b])/len(lines[a] | lines[b]) if lines[a] | lines[b] else 1.0
            for i, a in enumerate(names) for b in names[i + 1:]]
    return {'n': len(names), 'n_distinct': len({tuple(sorted(t.items())) for t in trees.values()}),
            'mean_line_jaccard': round(sum(sims)/len(sims), 4)}


def inventory(tree):
    """A one-line census of the components in a harness."""
    counts = {'skills': len(skill_names(tree)),
              'skill files': sum(1 for rel in tree if SKILL_FILE.fullmatch(rel)),
              'sub_agents': len(subagent_names(tree)),
              'scripts': sum(1 for rel in tree if SCRIPT.fullmatch(rel)), 'hooks': int(HOOKS in tree)}
    parts = [f'{value} {key}' for key, value in counts.items() if value] or ['always-on only']
    return f'{", ".join(parts)}; {len(always_on_text(tree)):,} always-on chars'


def fingerprint(directory):
    """A stable digest of a harness directory; None is the inherited base harness."""
    if directory is None:
        return 'base'
    root = Path(directory)
    if not root.is_dir():
        return 'absent'
    digest = hashlib.sha1()
    for path in sorted(root.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc' \
                and path.name != '.DS_Store':
            digest.update(path.relative_to(root).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()[:16]


def tree_fingerprint(tree):
    digest = hashlib.sha1()
    for rel in sorted(tree):
        digest.update(rel.encode() + b'\0' + tree[rel].encode() + b'\0')
    return digest.hexdigest()[:16]
