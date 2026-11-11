"""The addr tracing adapter: offline cases, an agent-CLI executor and the held-out judge.

The base harness is a frozen snapshot of the project's `addr-roof-tracing` skill plus short
always-on files describing the offline case environment. Freezing it per runs root keeps a
namespace reproducible after the live skill changes; `--refresh-base` takes a new snapshot.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from . import cases, executor, judge, library, surface, usage
from .spec import WorkPrompts

SKILL = 'addr-roof-tracing'
SKILL_SOURCE = Path('.opencode/skills')/SKILL
BASE_VERSION = 'addr-tracing-base-v1'

AGENTS = """\
# Addr tracing agent

You review one machine-detected scene object on aerial imagery and deliver the best supported outline
for it -- or, in a detection review, a decision for each machine detection of one class family.

Each run is one offline case. `case/case.json` names the scene, object(s), class and source frame;
`case/image.png` is the original image. The `addr-case` tools are bound to this case only: they
inspect, preview and record what you submit. Nothing is saved to a scene, the CMS or anywhere else.

1. Read `case/case.json`.
2. For an outline case, follow the `addr-roof-tracing` skill's full perimeter review with the case tools.
   Applying an inspected preview is authorized here. Call `finish_review` exactly once at the end --
   also when you keep the current outline -- with notes naming every unresolved edge or corner.
3. For a detection review, inspect the detections, decide every one, outline clearly missed objects
   of the family, and call `submit_detection_review` once.
4. For a Fieldwork assessment, answer the review item's question about its PROPOSED outline against the
   imagery -- approved, rejected, needs_adjustment or uncertain -- and call `submit_agent_review` once with
   evidence notes. Assessments are agent history for a human reviewer, never a human decision.
"""

GUARDRAILS = f"""\
- When tracing, refining or judging a roof, tree, vegetation, solar, pool, paving or other scene-object \
outline against imagery, load the `{SKILL}` skill.
- Coordinates are original-image source pixels, x right and y down; rings are open.
- Do not end the run before the case's submission tool (`finish_review`, `submit_detection_review` or \
`submit_agent_review`) has succeeded.
"""

TOOL_GUIDANCE = """\
The `addr-case` tools keep the names and arguments of the production addr-review tools the skill names:

- `roof_line_feedback` / `object_feedback(scene_id, object_id)`: current outline, revision, class.
- `inspect_roof_boundary` / `inspect_object_boundary(scene_id, object_id, edge_index?, preview_id?)`:
  native-pixel cards of the current outline or of an unchecked preview.
- `preview_roof_polygon` / `preview_object_polygon(scene_id, object_id, proposed_outline_pixels, notes,
  uncertainty, reviewer)`: validates a traced ring and returns an immutable `preview_id`.
- `apply_roof_polygon` / `apply_object_polygon(preview_id)`: records that inspected preview as the case
  outline and advances its revision; earlier previews become stale.
- `finish_review(scene_id, object_id, notes, uncertainty)`: ends an outline case (required).
- `inspect_detections(scene_id, kind?)` and `submit_detection_review(scene_id, decisions, additions,
  notes)`: detection reviews only.
- `inspect_review_item(scene_id, object_id)`, the boundary inspectors with `outline="current"|"proposed"`,
  and `submit_agent_review(scene_id, object_id, decision, notes)`: Fieldwork assessments only; the last is
  the case equivalent of the production `save_agent_review`.

Roof tools accept buildings only; use the object tools for other classes. Production-only tools in the
skill (`scene_roof_review`, `low_confidence_roofs`, split tools, `save_agent_review`, `mcp_call.py`) do
not exist in a case; do not try to reach them. Every card is also saved under `run/`; open the PNG
path a tool returns if its image did not display.

The shell runs only commands that start with `python3` (or `python`), from the workspace root, such as
`python3 scripts/<name>.py ...`; any other or chained shell command is refused. Pillow, numpy and
shapely are importable. Write any derived image under `run/` and open it with the file reader.
"""

OUTLINE_WORK = {
    'roof': 'Trace the outer roof outline of one building on an aerial image: correct the machine outline so '
            'each edge follows the visible eave or fascia transition, or keep it where it already does, and '
            'name the segments the imagery cannot resolve.',
    'vegetation': 'Trace one tree crown or vegetation patch on an aerial image: correct the machine outline to '
                  'the visible foliage silhouette, or keep it, without inventing crown splits or ground-cover '
                  'semantics, and name unresolved contacts.',
    'structure': 'Trace one structure (solar bank, pool, water body or similar) on an aerial image: correct the '
                 'machine outline to the visible frame, coping or edge, or keep it, and name unresolved segments.',
    'paving': 'Trace one paved surface (driveway, road or footpath) on an aerial image: correct the machine '
              'outline to the visible paving edge, or keep it, and name occluded or unresolved segments.',
    'vehicle': 'Trace one parked vehicle on an aerial image: correct the machine outline to the visible body '
               'silhouette, or keep it, excluding its shadow.',
}
DETECT_WORK = ('Review the machine detections of one class family in an aerial frame: keep each real object, '
               'reject false detections, relabel a misclassified one within the family, and outline any '
               'clearly missed object of the family.')
YARDSTICK = ('A good run reads the case, opens the clean original imagery, inspects every edge and corner of '
             'the current outline, previews a supported trace, inspects that preview with its preview_id, '
             'applies only an inspected preview, and finishes with notes that name unresolved segments. Keeping '
             'an outline that already fits is a valid result. A few focused passes usually suffice; repeated '
             'unchanged previews spend budget without changing the result.')
REVIEW_WORK = ('Assess one proposed outline correction from a Fieldwork review queue against the original aerial '
               'image: approve it, reject it, mark it as needing adjustment, or say uncertain, answering the question '
               'the human reviewer is asked, with evidence notes a reviewer can check.')
SELECTION_CHECKS = ('An outline is right when every edge follows the class boundary named in the request within '
                    'native-pixel blur, steps and gaps that the imagery shows are kept, and no edge follows a '
                    'wall, cast shadow, paving or a neighbouring object.')


def prompts_for_case(case):
    if case['task'] == 'review':
        return WorkPrompts(work=REVIEW_WORK, domain_word='review family', deliverable_word='assessment',
                           yardstick='A good assessment opens the original imagery and both outlines, checks every '
                                     'edge where they differ, answers the question as the reviewer is asked it, and '
                                     'says uncertain when the imagery cannot settle it.',
                           selection_checks='An assessment is right when its decision follows from what the imagery '
                                            'shows along the edges where the two outlines differ, and its notes name '
                                            'that evidence.')
    if case['task'] == 'detect':
        return WorkPrompts(work=DETECT_WORK, domain_word='class family', deliverable_word='detection review',
                           yardstick='A good run inspects every detection at native resolution before deciding, '
                                     'rejects only what the imagery contradicts, and decides every listed detection.')
    family = case.get('family', 'roof')
    return WorkPrompts(work=OUTLINE_WORK.get(family, OUTLINE_WORK['structure']), domain_word=f'{family} domain',
                       deliverable_word='outline', yardstick=YARDSTICK, selection_checks=SELECTION_CHECKS)


def task_prompt_for(case):
    if case['task'] == 'review':
        return (f'Fieldwork assessment `{case["case_id"]}`: scene `{case["scene_id"]}`, object `{case["object_id"]}`, '
                f'class `{case["kind"]}`, review family `{case["family"]}`.\n\nQuestion: {case["question"]["title"]}\n\n'
                'The case material is in `case/` (case.json, image.png) and the `addr-case` tools are bound to it. Assess '
                'the PROPOSED outline and record it once with `submit_agent_review`.\n')
    if case['task'] == 'detect':
        kinds = ', '.join(case['kinds'])
        return (f'Detection review `{case["case_id"]}`: scene `{case["scene_id"]}`, class family '
                f'`{case["family"]}` ({kinds}), {len(case["detections"])} machine detections.\n\n'
                'The case material is in `case/` (case.json, image.png) and the `addr-case` tools are bound to it. '
                'Decide every detection and submit once with `submit_detection_review`.\n')
    return (f'Outline case `{case["case_id"]}`: scene `{case["scene_id"]}`, object `{case["object_id"]}`, class '
            f'`{case["kind"]}`.\n\nThe case material is in `case/` (case.json, image.png) and the `addr-case` tools '
            'are bound to it. Review the outline against the imagery, apply a refined outline if the evidence '
            'supports one, and end with `finish_review`.\n')


def build_base(dest, *, repo=Path('.')):
    """Snapshot the live tracing skill and the case-environment always-on files into `dest`."""
    tree = {'AGENTS.md': AGENTS, 'guardrails.md': GUARDRAILS, 'MEMORY.md': '', 'tool_guidance.md': TOOL_GUIDANCE}
    source = Path(repo)/SKILL_SOURCE
    for path in sorted(source.rglob('*')):
        rel = f'skills/{SKILL}/{path.relative_to(source).as_posix()}'
        if path.is_file() and surface.is_allowed(rel) and '__pycache__' not in path.parts:
            tree[rel] = path.read_text(encoding='utf-8')
    surface.validate(tree)
    dest = Path(dest)
    shutil.rmtree(dest, ignore_errors=True)
    surface.write_tree(tree, dest)
    # Metadata sits beside the tree: every file inside it is on the editable surface.
    dest.with_suffix('.json').write_text(json.dumps({'version': BASE_VERSION, 'skill_source': str(SKILL_SOURCE),
                                                     'fingerprint': surface.tree_fingerprint(tree)}, indent=2))
    return dest


class TracingAdapter:
    """Case family adapter for addr outline and detection reviews."""

    name = 'tracing'

    def __init__(self, *, runs_root, cases_root=cases.ROOT, spec=None, repo=Path('.'), refresh_base=False,
                 obs_trunc=50_000, keep_workspaces=False, base_dir=None):
        self.runs_root, self.cases_root, self.repo = Path(runs_root), Path(cases_root), Path(repo)
        self.spec = spec or executor.ExecutorSpec()
        self.executor = self.spec.kind
        self.prompts = prompts_for_case({'task': 'outline', 'family': 'roof'})
        self.leak_globs = cases.HELDOUT_GLOBS
        self.allow_subagents, self.allow_hooks = self.spec.allow_subagents, self.spec.allow_hooks
        self.obs_trunc, self.keep_workspaces = obs_trunc, keep_workspaces
        self.index = cases.load_index(self.cases_root)
        self._base = self.runs_root/'base'
        if refresh_base or not surface.is_tree(self._base):
            build_base(self._base, repo=self.repo)
        if base_dir is not None:  # e.g. the lineage head; the snapshot above stays the lineage's v1
            if not surface.is_tree(base_dir):
                raise ValueError(f'{base_dir} is not a harness tree')
            self._base = Path(base_dir)
        # Identities the base skill already cites (its worked examples) are not new case facts.
        self._base_text = '\n'.join(surface.read_tree(self._base).values())
        self._identities = self.split_identities(cases.SPLITS)

    # -- task set ------------------------------------------------------------------------------------
    def tasks(self, split='all'):
        return sorted(case_id for case_id, entry in self.index.items() if split in ('all', entry['split']))

    def domain_of(self, task_id):
        return self.index[task_id]['domain']

    def case(self, task_id):
        return cases.load_case(self.cases_root, task_id)

    def heldout(self, task_id):
        """The held-out answer: for the judge and for TRAIN-split consolidation only."""
        return cases.load_heldout(self.cases_root, task_id)

    def split_identities(self, splits):
        """Case, scene and object ids of the given splits, minus ids the base skill already cites."""
        return {value for case_id, entry in self.index.items() if entry['split'] in splits
                for value in (case_id, entry['scene_id'], *entry.get('object_ids', ()))
                if len(value) >= 8 and value not in self._base_text}

    def case_identities(self):
        return set(self._identities)

    def prompts_for(self, task_id):
        return prompts_for_case(self.case(task_id))

    def task_prompt(self, task_id):
        return task_prompt_for(self.case(task_id))

    def task_files(self, task_id):
        return self.cases_root/task_id/'source'

    def base_harness(self):
        return self._base

    def validate_tree(self, tree):
        """Case identities may appear only in MEMORY.md, where case facts are labelled as such."""
        problems = []
        for rel, text in tree.items():
            if rel == 'MEMORY.md':
                continue
            hits = sorted(value for value in self._identities if value in text)
            if hits:
                problems.append(f'{rel} names case identities {hits[:3]}; case facts belong only under '
                                f'"{surface.CASE_FACTS}" in MEMORY.md, never in a skill, reference, script or hook')
        memory = tree.get('MEMORY.md', '')
        if any(value in memory for value in self._identities) and surface.CASE_FACTS not in memory:
            problems.append(f'MEMORY.md names a case without the "{surface.CASE_FACTS}" heading')
        if problems:
            raise ValueError(' | '.join(problems))

    # -- rollouts ------------------------------------------------------------------------------------
    def run_dir_of(self, run_id):
        namespace, rest = run_id.split('/', 1)
        task_id, label = rest.rsplit('/', 1)
        return self.runs_root/namespace/library.slug(task_id)/'rollouts'/label

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        """Run one rollout in a fresh workspace; idempotent by run_id and harness fingerprint."""
        run_dir = self.run_dir_of(run_id)
        harness_dir = Path(harness_dir or self.base_harness())
        identity = {'harness': surface.fingerprint(harness_dir), 'executor': self.spec.kind, 'model': self.spec.model}
        marker = run_dir/'.complete'
        if (marker.is_file() and {k: json.loads(marker.read_text()).get(k) for k in identity} == identity
                and not executor.outage(run_dir/'stream.jsonl', self.spec.kind, None)):
            return run_dir
        pauses = (0, 60, 180, 420)  # a transient server limit can last minutes; a usage limit stops at once
        for attempt, pause in enumerate(pauses):
            time.sleep(pause)
            run_dir, meta = self._launch(task_id, harness_dir, run_id, run_dir, identity, timeout)
            reason = meta.get('outage') or ''
            if not reason:
                marker.write_text(json.dumps({'run_id': run_id, **identity}), encoding='utf-8')
                return run_dir
            if re.search(r'usage.?limit|session limit|authenticat|unauthori[sz]ed', reason, re.I) or attempt == len(pauses) - 1:
                break
        raise executor.ServiceUnavailable(f'{run_id}: {reason}')

    def _launch(self, task_id, harness_dir, run_id, run_dir, identity, timeout):
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True)
        tree = surface.read_tree(harness_dir)
        workspace = Path(tempfile.mkdtemp(prefix='addr-harness-ws-'))
        try:
            shutil.copytree(self.task_files(task_id), workspace/'case')
            (workspace/'run').mkdir()
            executor.materialise(tree, workspace, self.spec.kind)
            (run_dir/'harness.json').write_text(json.dumps(tree, indent=1), encoding='utf-8')
            prompt = self.task_prompt(task_id)
            (run_dir/'prompt.md').write_text(prompt, encoding='utf-8')
            meta = executor.launch(self.spec, workspace, tree, prompt, stream=run_dir/'stream.jsonl',
                                   stderr=run_dir/'stderr.log', timeout=timeout)
            shutil.copytree(workspace/'run', run_dir/'run')
        finally:
            if not self.keep_workspaces:
                shutil.rmtree(workspace, ignore_errors=True)
        meta.update(harness=identity['harness'], run_id=run_id,
                    workspace=str(workspace) if self.keep_workspaces else None)
        (run_dir/'meta.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
        return run_dir, meta

    def submission(self, run_dir):
        path = Path(run_dir)/'run'/'submission.json'
        return json.loads(path.read_text()) if path.is_file() else None

    def delivered(self, run_dir):
        return self.submission(run_dir) is not None

    def component_usage(self, run_dir):
        """Which harness components this rollout used, from its recorded tree and trajectory."""
        run_dir = Path(run_dir)
        if not (run_dir/'harness.json').is_file():
            return None
        meta = run_dir/'meta.json'
        kind = json.loads(meta.read_text()).get('executor', self.spec.kind) if meta.is_file() else self.spec.kind
        return usage.component_usage(run_dir/'stream.jsonl', kind, json.loads((run_dir/'harness.json').read_text()))

    def trajectory(self, run_dir, *, obs_trunc):
        meta = Path(run_dir)/'meta.json'
        kind = json.loads(meta.read_text()).get('executor', self.spec.kind) if meta.is_file() else self.spec.kind
        return executor.render_trajectory(Path(run_dir)/'stream.jsonl', kind, obs_trunc=obs_trunc)

    def deliverable_text(self, run_dir):
        return render_submission(self.submission(run_dir))

    def deliverable_images(self, run_dir):
        folder = Path(run_dir)/'run'
        return [folder/name for name in ('submission.png', 'submission-indexed.png') if (folder/name).is_file()]

    def score(self, run_dir, task_id):
        submission, heldout = self.submission(run_dir), cases.load_heldout(self.cases_root, task_id)
        if submission is None or heldout is None:
            return None
        case = self.case(task_id)
        scorer = {'detect': judge.score_detect, 'review': judge.score_review}.get(case['task'], judge.score_outline)
        return scorer(case, heldout, submission)


def render_submission(submission):
    """The submitted outline or decisions as text, for improvers and the blind selector."""
    if submission is None:
        return ''
    if submission['task'] == 'review':
        return f'# Submitted assessment\n\n- decision: {submission["decision"]}\n\n## Notes\n\n{submission["notes"]}\n'
    if submission['task'] == 'detect':
        rows = '\n'.join(f'- `{d["object_id"]}`: {d["decision"]}' + (f' -> {d["kind"]}' if d['decision'] == 'relabel'
                                                                      else '') + (f' ({d["note"]})' if d['note'] else '')
                         for d in submission['decisions'])
        added = '\n'.join(f'- {a["kind"]}: {json.dumps(a["outline_pixels"])}' for a in submission['additions'])
        return (f'# Submitted detection review\n\n## Decisions\n{rows}\n\n## Added objects\n{added or "(none)"}\n\n'
                f'## Notes\n{submission.get("notes") or "(none)"}\n')
    outline = submission['outline_pixels']
    return (f'# Submitted outline ({submission["status"]})\n\n'
            f'- kept the machine outline unchanged: {"yes" if submission["kept_start_outline"] else "no"}\n'
            f'- revision {submission["revision"]}; applied previews: {len(submission["applied_previews"])}\n'
            f'- outer ring: {len(outline["ring"])} vertices; holes: {len(outline["holes"])}\n\n'
            f'## Outline (source pixels)\n\n```json\n{json.dumps(outline)}\n```\n\n'
            f'## Notes\n\n{submission.get("notes") or "(none)"}\n\n'
            f'## Remaining uncertainty\n\n{submission.get("uncertainty") or "(none)"}\n')
