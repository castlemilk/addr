"""A deterministic, model-free run of the whole loop on synthetic roofs.

Three synthetic cases: two machine outlines spill over a dark shadow band and one already fits.
The scripted agent drives the real case tools and only trims a dark band when its harness carries
the shadow-band reference; the scripted improver proposes that reference (DOM) and an inert helper
script (INV), composes from the observed contrast, selects by imagery, and writes a lesson. The
judge then measures base versus adopted harness against held-out outlines.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import cases, judge, report, schedule, surface
from .adapter import SKILL, TracingAdapter
from .casetools import CaseTools
from .channels import ChannelSpec, Improver, Reply
from .executor import ExecutorSpec

PIXEL = .125
SIZE = 320
DEMO_CASES = {  # case id: (roof box, shadow box or None)
    'o-demo-a-roof': ((80, 80, 220, 180), (80, 180, 220, 194)),
    'o-demo-b-roof': ((100, 60, 200, 220), (200, 60, 212, 220)),
    'o-demo-c-roof': ((90, 90, 230, 200), None),
}
VAL_CASES = {  # held-out validation roofs for the cycle demo
    'o-demo-d-roof': ((70, 90, 210, 190), (70, 190, 210, 203)),
    'o-demo-e-roof': ((110, 70, 210, 230), (210, 70, 221, 230)),
    'o-demo-f-roof': ((80, 100, 220, 210), None),
}
SHADOW_REFERENCE = """\
# Shadow bands

When a bright roof ends above a darker parallel band, the band is cast shadow or wall, not roof.
Trace the bright-to-dark transition and leave the band outside the outline.
"""


def _ring(box):
    left, top, right, bottom = box
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _image(roof, shadow, seed):
    rng = np.random.default_rng(seed)
    pixels = np.clip(rng.normal((96, 128, 80), 10, (SIZE, SIZE, 3)), 0, 255).astype('uint8')
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    draw.rectangle([roof[0], roof[1], roof[2] - 1, roof[3] - 1], fill=(206, 204, 210))
    if shadow:
        draw.rectangle([shadow[0], shadow[1], shadow[2] - 1, shadow[3] - 1], fill=(38, 44, 40))
    return image


def write_cases(dest, *, val=False):
    """Synthetic outline cases in the real snapshot layout, with held-out references."""
    dest = Path(dest)
    index = {}
    specs = {**{c: (r, s, 'train') for c, (r, s) in DEMO_CASES.items()},
             **({c: (r, s, 'val') for c, (r, s) in VAL_CASES.items()} if val else {})}
    frame = {'image_width': SIZE, 'image_height': SIZE, 'pixel_to_meters': [[PIXEL, 0], [0, -PIXEL], [-20, 20]],
             'meters_to_pixel': [[1/PIXEL, 0], [0, -1/PIXEL], [160, 160]]}
    for seed, (case_id, (roof, shadow, split)) in enumerate(sorted(specs.items())):
        start = (roof[0], roof[1], max(roof[2], (shadow or roof)[2]), max(roof[3], (shadow or roof)[3]))
        scene_id = f'demo-scene-{case_id[7]}'
        image_path = dest/f'{case_id}.png'
        dest.mkdir(parents=True, exist_ok=True)
        _image(roof, shadow, seed).save(image_path)
        sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
        case = {'version': cases.VERSION, 'case_id': case_id, 'task': 'outline', 'scene_id': scene_id,
                'object_id': f'roof{seed}', 'kind': 'building', 'family': 'roof', 'domain': 'roof-outline',
                'image': 'image.png', 'image_sha256': sha, 'source_frame': frame, 'pixel_size_m': PIXEL,
                'start_outline_pixels': {'ring': _ring(start), 'holes': []}, 'machine': {'label': 'roof', 'score': .8}}
        heldout = {'version': cases.VERSION, 'case_id': case_id, 'reference_outline_pixels': {'ring': _ring(roof), 'holes': []},
                   'change': 'edited' if shadow else 'unchanged', 'human_status': 'confirmed'}
        cases.write_case(dest, case, heldout, image_path)
        image_path.unlink()
        index[case_id] = {'task': 'outline', 'domain': 'roof-outline', 'scene_id': scene_id,
                          'object_ids': [case['object_id']], 'split': split, 'change': heldout['change'],
                          'kind': 'building'}
    (dest/'index.json').write_text(json.dumps({'version': cases.VERSION, 'cases': index}, indent=2))
    return index


def base_tree():
    return {'AGENTS.md': '# Demo tracing agent\n\nRead `case/case.json`, review the outline with the case tools, '
                         'and end with `finish_review`.\n',
            'guardrails.md': f'- When tracing a roof outline, load the `{SKILL}` skill.\n',
            'MEMORY.md': '', 'tool_guidance.md': 'The `addr-case` tools inspect, preview, apply and finish.\n',
            f'skills/{SKILL}/SKILL.md': f'---\nname: {SKILL}\ndescription: Trace roof outlines against imagery.\n'
                                        '---\n\n# Roof tracing\n\nInspect every edge, preview, inspect the preview, '
                                        'apply, then finish.\n'}


def _bright_box(image, ring):
    """The bounding box of bright roof pixels inside the start ring (the scripted agent's 'eye')."""
    grey = np.asarray(image.convert('L'), dtype=float)
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    window = grey[min(ys):max(ys), min(xs):max(xs)] > 150
    rows, cols = np.where(window.any(axis=1))[0], np.where(window.any(axis=0))[0]
    return [min(xs) + int(cols[0]), min(ys) + int(rows[0]), min(xs) + int(cols[-1]) + 1, min(ys) + int(rows[-1]) + 1]


class DemoAdapter(TracingAdapter):
    """The real adapter with a scripted agent in place of an agent CLI."""

    def __init__(self, root, base_dir=None):
        root = Path(root)
        surface.write_tree(base_tree(), root/'runs'/'base')
        super().__init__(runs_root=root/'runs', cases_root=root/'cases', spec=ExecutorSpec('claude'), base_dir=base_dir)
        self.executor = 'demo'

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        run_dir = self.run_dir_of(run_id)
        harness_dir = Path(harness_dir or self.base_harness())
        tree = surface.read_tree(harness_dir)
        if (run_dir/'.complete').is_file() and json.loads((run_dir/'.complete').read_text())['harness'] == surface.fingerprint(harness_dir):
            return run_dir
        shutil.rmtree(run_dir, ignore_errors=True)
        tools = CaseTools(self.task_files(task_id), run_dir/'run')
        case, events = tools.case, []

        def act(name, arguments, function):
            events.append({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': name, 'input': arguments}]}})
            try:
                result = function()
                events.append({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': json.dumps(
                    {k: v for k, v in result.items() if k in ('preview_id', 'revision', 'finished', 'outline_role')})}]}})
                return result
            except ValueError as error:
                events.append({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': True,
                                                                         'content': str(error)}]}})
                return None

        scene, obj = case['scene_id'], case['object_id']
        act('mcp__addr-case__inspect_roof_boundary', {'scene_id': scene, 'object_id': obj},
            lambda: tools.inspect(scene, obj, roof=True))
        ring = case['start_outline_pixels']['ring']
        trims = f'skills/{SKILL}/references/shadow-bands.md' in tree
        box = _bright_box(tools.image(), ring) if trims else None
        proposed = _ring(box) if box else ring
        notes = 'Kept the outline; every edge follows the roof.'
        if proposed != ring:
            outline = {'ring': proposed, 'holes': []}
            preview = act('mcp__addr-case__preview_roof_polygon', {'scene_id': scene, 'object_id': obj,
                                                                  'proposed_outline_pixels': outline},
                          lambda: tools.preview(scene, obj, outline, notes='Excluded the dark shadow band.', roof=True))
            if preview:
                act('mcp__addr-case__inspect_roof_boundary', {'scene_id': scene, 'object_id': obj,
                                                             'preview_id': preview['preview_id']},
                    lambda: tools.inspect(scene, obj, preview_id=preview['preview_id'], roof=True))
                act('mcp__addr-case__apply_roof_polygon', {'preview_id': preview['preview_id']},
                    lambda: tools.apply(preview['preview_id'], roof=True))
                notes = 'Trimmed the dark band beyond the bright roof edge.'
        act('mcp__addr-case__finish_review', {'scene_id': scene, 'object_id': obj, 'notes': notes},
            lambda: tools.finish(scene, obj, notes))
        events.append({'type': 'result', 'subtype': 'success', 'num_turns': len(events), 'total_cost_usd': 0,
                       'result': notes})
        (run_dir/'stream.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
        (run_dir/'harness.json').write_text(json.dumps(tree), encoding='utf-8')
        (run_dir/'meta.json').write_text(json.dumps({'executor': 'claude', 'cost_usd': 0.0, 'run_id': run_id}))
        (run_dir/'.complete').write_text(json.dumps({'run_id': run_id, 'harness': surface.fingerprint(harness_dir)}))
        return run_dir


class DemoImprover(Improver):
    """Scripted proposals, composition, selection and reflection; no model service is used."""

    def __init__(self, audit_dir):
        super().__init__(ChannelSpec('claude', 'demo/deterministic'), audit_dir=Path(audit_dir))

    def call(self, mission, context_dir, label, *, timeout=None, max_attempts=None, harvest=None):
        context = Path(context_dir)
        workspace = None
        if label.startswith('select.'):
            text = self._select(context)
        elif label.startswith('experience.'):
            text = self._experience(context)
        else:
            workspace = Path(self.audit_dir)/f'{label}.{random.Random(mission).getrandbits(32):08x}.workspace'
            shutil.rmtree(workspace, ignore_errors=True)
            shutil.copytree(context/'harness', workspace)
            text = self._edit(label, context, workspace)
        reply = Reply(label=label, text=text, ok=True, attempts=1, channel='demo', workspace=workspace)
        self.calls.append(reply)
        return reply

    def _edit(self, label, context, harness):
        skill = harness/'skills'/SKILL
        if label.startswith('consolidate.'):
            evidence = sorted(p.name for p in (context/'feedback').iterdir() if p.is_dir())
            body = (skill/'SKILL.md').read_text()
            if 'bright-to-dark' not in body:
                (skill/'SKILL.md').write_text(body + '\nWhen a bright roof ends above a darker parallel band, trace '
                                              'the bright-to-dark transition and leave the band outside.\n')
            (skill/'references').mkdir(exist_ok=True)
            (skill/'references'/'shadow-bands.md').write_text(SHADOW_REFERENCE)
            return f'<notes>kept -> inline shadow-band check in SKILL.md -> training cases {", ".join(evidence)}</notes>'
        if label.startswith('inv'):
            (harness/'scripts').mkdir(exist_ok=True)
            (harness/'scripts'/'ring_area.py').write_text(
                'import json\nimport sys\n\nring = json.loads(sys.argv[1])\n'
                'print(abs(sum(a[0]*b[1] - b[0]*a[1] for a, b in zip(ring, ring[1:] + ring[:1])))/2)\n')
            guide = harness/'tool_guidance.md'
            guide.write_text(guide.read_text() + 'Check a ring area with `python3 scripts/ring_area.py RING_JSON`.\n')
            return '<notes>Added scripts/ring_area.py: the base rollouts estimated ring areas by hand.</notes>'
        dom_wins = label.startswith('dom') or any(
            'Trimmed the dark band' in (p/'produced.md').read_text() for p in (context/'pool').glob('dom*')
            if (p/'produced.md').is_file())
        if not dom_wins:
            return '<notes>KEEP_BASE: no candidate changed the outline.</notes>'
        if label.startswith(('agg', 'c')):
            (harness/'scripts'/'ring_area.py').unlink(missing_ok=True)
            guide = harness/'tool_guidance.md'
            guide.write_text(re.sub(r'Check a ring area.*\n', '', guide.read_text()))
        (skill/'references').mkdir(exist_ok=True)
        (skill/'references'/'shadow-bands.md').write_text(SHADOW_REFERENCE)
        body = (skill/'SKILL.md').read_text()
        if 'shadow-bands.md' not in body:
            (skill/'SKILL.md').write_text(body + '\nBefore moving an edge, read [shadow-bands.md](references/shadow-bands.md).\n')
        source = 'copied from the library' if (context/'library').is_dir() else 'base rollouts kept the dark band'
        return (f'<notes>kept -> references/shadow-bands.md -> {source}; the DOM probe trimmed the band.\n'
                'dropped -> scripts/ring_area.py -> its probe outline was identical to base.</notes>')

    def _select(self, context):
        image = Image.open(context/'source'/'image.png').convert('L')
        grey = np.asarray(image, dtype=float)
        best, why = None, ''
        for path in sorted((context/'candidates').glob('*.md')):
            ring = json.loads(re.search(r'```json\n(.*?)\n```', path.read_text(), re.S).group(1))['ring']
            mask = Image.new('1', image.size, 0)
            ImageDraw.Draw(mask).polygon([tuple(p) for p in ring], fill=1)
            mean = float(grey[np.asarray(mask, dtype=bool)].mean())
            if best is None or mean > best[0]:
                best, why = (mean, path.stem), f'Candidate {path.stem} keeps only bright roof pixels.'
        return f'<why>{why}</why>\n```choice\n{best[1]}\n```'

    def _experience(self, context):
        changed = (context/'harness'/'skills'/SKILL/'references'/'shadow-bands.md').is_file()
        edits = ('<edits>\ncomponent: skills/addr-roof-tracing/references/shadow-bands.md\ncandidates: dom1\n'
                 'change: added a shadow-band reference linked from SKILL.md\nobserved: the probe excluded the dark '
                 'band the base rollouts kept\nverdict: helped\n</edits>\n\n')
        if not changed:
            return edits + 'NO_ENTRY'
        return edits + ('<experience>\ncue: a bright roof ends above a darker parallel band\nlesson: carry a '
                        'skills/addr-roof-tracing reference that tells the agent to trace the bright-to-dark '
                        'transition and leave the band outside\nevidence: the shadow-bands row; the probe trimmed '
                        'the band and its outline excluded the dark pixels\nconfidence: high for this scripted '
                        'contrast\n</experience>')


def run(output, *, k=2, modes=('agg', 'seq')):
    """Build the synthetic cases, run the loop, judge adopted and base runs; return the reports."""
    output = Path(output)
    write_cases(output/'cases')
    adapter = DemoAdapter(output)
    improver = DemoImprover(output/'audit')
    sweep = schedule.run_sweep(k, adapter=adapter, improver=improver, runs_root=adapter.runs_root, modes=modes)
    reports = {}
    for mode in modes:
        ns = schedule.Namespace(adapter.name, adapter.executor, mode, k)
        judge.judge(adapter, adapter.runs_root, ns)
        judge.judge(adapter, adapter.runs_root, ns, bases=True)
        reports[mode] = report.build(adapter.runs_root, ns)
    return sweep, reports
