"""Bounded paired optical-guidance evaluation on frozen model outlines and pixels.

Prepare snapshots before evaluation; run both arms without passing human drafts
to inference. Outputs stay in the selected artifact directory. No scene saves,
training, parameter search, or annotation approval occurs.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

VERSION = 'optical-valley-paired-eval-v1'
DEFAULT_OUT = Path('data/reviews/eyes/optical-valleys-20260927')
QUEUE = Path('data/reviews/eyes/mcp-low-confidence-20260927/queue/00-low_confidence_roofs/text-00.txt')
HUMAN_DRAFTS = [('house-30286', '37ff5c02f77869d5'), ('house-30296', '0ead4a2d63f58042'),
                ('mooroolbark-50011498-17-118443-80412', '19e7744ed31b7627')]
COLORS = {'ridge': '#65ef92', 'hip': '#62d8f0', 'valley': '#f58cf8'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def plain(value):
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, np.generic):
        return plain(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plain(data), indent=2, allow_nan=False) + '\n')


def verified(path, expected):
    data = path.read_bytes()
    if sha(data) != expected:
        raise ValueError(f'Frozen source changed: {path}')
    return data


def prepare(root, out):
    if (out / 'dataset.json').exists():
        raise ValueError('Dataset already exists; preserve it and run the prepared dataset.')
    queue_raw = (root / QUEUE).read_bytes()
    queued = json.loads(queue_raw)['rows']
    targets = [(r['scene_id'], r['object_id'], 'mcp-low-confidence', r) for r in queued]
    targets += [(s, o, 'human-line-draft', None) for s, o in HUMAN_DRAFTS]
    rows = []
    for index, (scene_id, object_id, group, selected) in enumerate(targets, 1):
        folder = root / 'data/scenes' / scene_id
        model_path = folder / 'latest.json'
        model_raw = model_path.read_bytes()
        scene = json.loads(model_raw)
        obj = next(o for o in scene['objects'] if o['id'] == object_id)
        image_path = folder / f"{scene['image_sha256']}.png"
        image_raw = verified(image_path, scene['image_sha256'])
        if selected and (selected['source_model_sha256'] != sha(model_raw) or selected['image_sha256'] != sha(image_raw)):
            raise ValueError(f'Original MCP selection no longer matches {scene_id}/{object_id}')
        case = f'{index:02d}-{object_id[:8]}'
        frame = {key: scene[key] for key in ('id', 'model_signature', 'image_sha256', 'image_width',
                                            'image_height', 'pixel_to_meters', 'meters_to_pixel')}
        source = {'model_path': str(model_path), 'model_sha256': sha(model_raw),
                  'image_path': str(image_path), 'image_sha256': sha(image_raw)}
        snapshot = {'scene_id': scene_id, 'object_id': object_id, 'source': source, 'frame': frame,
                    'object': obj, 'context_hint': None, 'rise_m': 2., 'form': 'auto'}
        input_path = out / case / 'input.json'
        write(input_path, snapshot)
        row = {'case': case, 'group': group, 'scene_id': scene_id, 'object_id': object_id,
               'input_path': str(input_path), 'input_sha256': sha(input_path.read_bytes()), 'source': source}
        if group == 'human-line-draft':
            review_path = folder / 'review.json'
            review_raw = review_path.read_bytes()
            review = json.loads(review_raw)
            observed = next(o for o in review['objects'] if o['id'] == object_id)
            edits = observed['roof_line_edits']
            outline = edits.get('outline', {})
            binding = {'review_model_signature_current': review['model_signature'] == scene['model_signature'],
                       'outline_matches_saved_review': outline.get('ring') == observed['ring'] and (outline.get('holes') or []) == (observed.get('holes') or []),
                       'outline_matches_model': outline.get('ring') == obj['ring'] and (outline.get('holes') or []) == (obj.get('holes') or []),
                       'checked': edits.get('checked', False), 'review_revision': review['revision'],
                       'reference_kind_counts': dict(Counter(edge['kind'] for edge in edits['edges']))}
            reference = {'scope': 'Saved human line draft, not a checked label. Scoring only; never an inference input.',
                         'review_path': str(review_path), 'review_sha256': sha(review_raw),
                         'binding': binding, 'roof_line_edits': edits}
            reference_path = out / case / 'reference.json'
            write(reference_path, reference)
            row.update(reference_path=str(reference_path), reference_sha256=sha(reference_path.read_bytes()),
                       reference_binding=binding)
        rows.append(row)
    dataset = {'version': VERSION, 'prepared_at': datetime.now(UTC).isoformat(),
               'scope': 'Paired inference on fixed latest-model outlines, source pixels, stored eave, 2 m rise, auto form, no context hints. Human drafts score outputs only; no labels, accuracy claims or saved-geometry changes.',
               'queue_path': str(root / QUEUE), 'queue_sha256': sha(queue_raw), 'rows': rows}
    write(out / 'dataset.json', dataset)
    print(json.dumps({'prepared': len(rows), 'dataset': str(out / 'dataset.json'),
                      'human_draft_bindings': [r['reference_binding'] for r in rows if 'reference_binding' in r]}), flush=True)


def arm(input_path, output_path, mode):
    from shapely.geometry import Polygon

    from .roof_line_fit import image_segments
    from .scene_roof import propose_roof

    snapshot = json.loads(input_path.read_text())
    raw = verified(Path(snapshot['source']['image_path']), snapshot['source']['image_sha256'])
    verified(Path(snapshot['source']['model_path']), snapshot['source']['model_sha256'])
    image = Image.open(io.BytesIO(raw)).convert('RGB')
    scene, obj = snapshot['frame'], snapshot['object']
    frame = np.asarray(scene['pixel_to_meters'])

    def local(ring):
        return (np.c_[ring, np.ones(len(ring))] @ frame).tolist()

    ring, holes = local(obj['ring']), [local(h) for h in obj.get('holes', [])]
    began = time.monotonic()
    try:
        proposal = propose_roof(ring, obj['model']['height_m'], snapshot['rise_m'], snapshot['form'],
                                image=image, meters_to_pixel=scene['meters_to_pixel'], holes=holes,
                                optical_guidance=mode == 'candidate')
        elapsed = time.monotonic() - began
        observations = image_segments(image, Polygon(ring, holes), scene['meters_to_pixel'])
        original = [{'points': plain(s['points']), 'support': float(s['support']), 'length': float(s['length'])} for s in observations]
        result = {'arm': mode, 'input_sha256': sha(input_path.read_bytes()), 'elapsed_seconds': elapsed,
                  'proposal': proposal, 'original_segments': original,
                  'original_segments_sha256': sha(json.dumps(original, sort_keys=True).encode())}
    except Exception as error:  # noqa: BLE001 - preserve a failure as paired evidence
        result = {'arm': mode, 'input_sha256': sha(input_path.read_bytes()),
                  'elapsed_seconds': time.monotonic() - began, 'error': type(error).__name__ + ': ' + str(error)}
    write(output_path, result)


def line_comparison(proposal, reference):
    from .reference_agreement import line_metrics

    edits = reference['roof_line_edits']
    expected = [(np.asarray([edits['vertices'][edge['a']][:2], edits['vertices'][edge['b']][:2]], float), edge['kind']) for edge in edits['edges']]
    actual = [(np.asarray(line['line_meters'], float), line['kind']) for line in proposal['lines']]
    by_kind = {kind: line_metrics([(line, k) for line, k in actual if k == kind], [(line, k) for line, k in expected if k == kind])
               for kind in COLORS}
    return {'scope': 'Agreement with saved unchecked draft; native line matcher, not accuracy.',
            'binding': reference['binding'], 'all': line_metrics(actual, expected), 'by_kind': by_kind}


def card(snapshot, baseline, candidate, reference, path):
    raw = verified(Path(snapshot['source']['image_path']), snapshot['source']['image_sha256'])
    image = Image.open(io.BytesIO(raw)).convert('RGB')
    ring = np.asarray(snapshot['object']['ring'], float)
    low, high = ring.min(axis=0), ring.max(axis=0)
    pad = max(16., float(np.max(high - low)) * .1)
    x0, y0 = np.maximum(0, np.floor(low - pad)).astype(int)
    x1, y1 = np.minimum(image.size, np.ceil(high + pad)).astype(int)
    crop = image.crop((x0, y0, x1, y1))
    size = 410
    scale = min(size / crop.width, size / crop.height)
    resized = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.Resampling.LANCZOS)
    dx, dy = (size - resized.width) // 2, (size - resized.height) // 2
    matrix = np.asarray(snapshot['frame']['meters_to_pixel'])

    def pixel(point, metric=False):
        p = np.r_[point[:2], 1.] @ matrix if metric else point
        return (float(p[0] - x0) * scale + dx, float(p[1] - y0) * scale + dy + 32)

    panes = [('Original RGB', None), ('Baseline: optical off', baseline), ('Candidate: optical on', candidate)]
    if reference:
        panes.append(('Human draft: unchecked', {'reference': reference}))
    sheet = Image.new('RGB', (size * len(panes), size + 76), '#15191d')
    for index, (title, result) in enumerate(panes):
        tile = Image.new('RGB', (size, size + 76), '#15191d')
        tile.paste(resized, (dx, dy + 32))
        draw = ImageDraw.Draw(tile)
        draw.text((9, 8), title, fill='white')
        if result:
            if 'reference' in result:
                edits = result['reference']['roof_line_edits']
                draw.line([pixel(p) for p in edits['outline']['ring'] + edits['outline']['ring'][:1]], fill='#e8e8e8', width=1)
                lines = [{'kind': edge['kind'], 'line_meters': [edits['vertices'][edge['a']], edits['vertices'][edge['b']]]} for edge in edits['edges']]
                label = 'Reference outline may differ from model'
            else:
                draw.line([pixel(p) for p in snapshot['object']['ring'] + snapshot['object']['ring'][:1]], fill='#e8e8e8', width=1)
                lines = result.get('proposal', {}).get('lines', [])
                label = result.get('error') or f"{len(lines)} lines; {result.get('elapsed_seconds', 0):.2f} s"
            for line in lines:
                draw.line([pixel(p, True) for p in line['line_meters']], fill=COLORS.get(line.get('kind'), '#ffc267'), width=2)
            draw.text((9, size + 38), label[:61], fill='white')
        sheet.paste(tile, (size * index, 0))
    sheet.save(path)


def code_hashes(root):
    paths = set((root / 'scripts/ml').glob('roof_*.py')) | set((root / 'scripts/ml').glob('*optical*.py'))
    paths = {path for path in paths if path.name != 'roof_review_pack.py'}
    paths |= {root / 'scripts/ml' / name for name in ('scene_roof.py', 'scene_house.py', 'rooflines.py', 'reference_agreement.py')}
    return {str(path.relative_to(root)): sha(path.read_bytes()) for path in sorted(paths)}


def report_markdown(summary, out):
    rows = summary['rows']
    paired = [r for r in rows if 'annotation_changed' in r]
    changed = sum(r['annotation_changed'] for r in paired)
    accepted = sum(bool((r.get('optical_guidance') or {}).get('accepted')) for r in paired)
    retained = [r.get('original_retention_passed') for r in paired if 'original_retention_passed' in r]
    stage_counts = dict(Counter(r.get('optical_stage_status', 'model-error') for r in rows))
    proposal_times = [r[mode]['elapsed_seconds'] for r in rows for mode in ('baseline', 'candidate') if r.get(mode, {}).get('elapsed_seconds') is not None]
    worker_times = [r[mode]['worker_wall_seconds'] for r in rows for mode in ('baseline', 'candidate') if r.get(mode, {}).get('worker_wall_seconds') is not None]
    lines = ['# Paired optical-guidance evaluation', '', summary['scope'], '',
             f"Completed {len(rows)} cases; {len(paired)} paired successes, {changed} changed annotations, {accepted} accepted optical updates. Elapsed {summary['elapsed_seconds']:.1f} seconds.",
             f"Source bytes unchanged: {summary['sources_unchanged']}. Code unchanged during run: {summary['code_unchanged']}. Original-segment retention: {sum(retained)}/{len(retained)} reported cases passed.", '',
             f'Optical-stage outcomes: {json.dumps(stage_counts, sort_keys=True)}.',
             f"Maximum proposal time: {max(proposal_times, default=0):.2f} s; maximum worker wall time including imports and diagnostics: {max(worker_times, default=0):.2f} s. Proposal calls over 20 s: {sum(t > 20 for t in proposal_times)}; workers over 20 s: {sum(t > 20 for t in worker_times)}.", '',
             '| Case | Group | Baseline → candidate | Valleys | Changed / optical stage | Card |', '| --- | --- | --- | --- | --- | --- |']
    for row in rows:
        before, after = row.get('baseline', {}), row.get('candidate', {})
        forms = str(before.get('roof_type', before.get('error'))) + ' → ' + str(after.get('roof_type', after.get('error')))
        valleys = str(before.get('line_kinds', {}).get('valley', 0)) + ' → ' + str(after.get('line_kinds', {}).get('valley', 0))
        lines.append(f"| {row['case']} | {row['group']} | {forms} | {valleys} | {row.get('annotation_changed', 'error')} / {row.get('optical_stage_status', 'model-error')} | [Compare]({row['card']}) |")
    lines += ['', '## Saved human drafts', '',
              'All three references are unchecked saved human drafts. The house-30286 and 19e7744e outlines differ from current model outlines; agreement is reported in the same metric frame without rebinding or feeding these drafts into inference.', '',
              '| Case | Draft outline matches model | All-line P/R before → after | Valley P/R before → after |',
              '| --- | --- | --- | --- |']

    def pr(value):
        return '/'.join('n/a' if value.get(key) is None else f'{value[key]:.3f}' for key in ('line_precision', 'line_recall'))

    for row in rows:
        before = row.get('baseline', {}).get('draft_agreement')
        after = row.get('candidate', {}).get('draft_agreement')
        if before and after:
            lines.append(f"| {row['case']} | {before['binding']['outline_matches_model']} | {pr(before['all'])} → {pr(after['all'])} | {pr(before['by_kind']['valley'])} → {pr(after['by_kind']['valley'])} |")
    lines += ['', 'No 12-case agent observation is a human reference. Roof form, line fit and drainage remain model diagnostics. See full per-arm JSON and summary.json for contradictions, valley hypotheses, gates and failures.', '']
    (out / 'REPORT.md').write_text('\n'.join(lines))


def run(root, out, seconds, arm_seconds):
    dataset = json.loads((out / 'dataset.json').read_text())
    code = code_hashes(root)
    code_path = out / 'run-code.json'
    if code_path.exists() and json.loads(code_path.read_text()) != code:
        raise ValueError('Evaluation code changed; preserve these outputs and use a separate run directory.')
    write(code_path, code)
    for relative in code:
        destination = out / 'code' / Path(relative).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((root / relative).read_bytes())
    began = time.monotonic()
    summary = {'version': VERSION, 'scope': dataset['scope'], 'started_at': datetime.now(UTC).isoformat(),
               'wall_budget_seconds': seconds, 'arm_budget_seconds': arm_seconds, 'rows': []}
    for case in dataset['rows']:
        if code_hashes(root) != code:
            summary['stopped'] = 'Code changed during evaluation'
            break
        input_path = Path(case['input_path'])
        snapshot = json.loads(verified(input_path, case['input_sha256']))
        reference = json.loads(verified(Path(case['reference_path']), case['reference_sha256'])) if case.get('reference_path') else None
        results = {}
        for mode in ('baseline', 'candidate'):
            result_path = input_path.with_name(mode + '.json')
            if not result_path.exists():
                remaining = seconds - (time.monotonic() - began)
                if remaining <= 1:
                    results[mode] = {'error': 'Overall wall budget exhausted'}
                    continue
                worker_started = time.monotonic()
                with input_path.with_name(mode + '.log').open('w') as log:
                    try:
                        subprocess.run([sys.executable, '-m', 'scripts.ml.optical_valley_eval', 'arm',
                                        '--input', str(input_path), '--output', str(result_path), '--mode', mode],
                                       cwd=root, stdout=log, stderr=subprocess.STDOUT, check=False,
                                       timeout=min(arm_seconds, remaining))
                    except subprocess.TimeoutExpired:
                        write(result_path, {'arm': mode, 'error': 'Per-arm or remaining wall budget exceeded', 'timed_out': True})
                if not result_path.exists():
                    write(result_path, {'arm': mode, 'error': 'Worker exited without a result; inspect arm log'})
                saved = json.loads(result_path.read_text())
                saved['worker_wall_seconds'] = time.monotonic() - worker_started
                write(result_path, saved)
            results[mode] = json.loads(result_path.read_text())
        row = {key: case[key] for key in ('case', 'scene_id', 'object_id', 'group')}
        for mode, result in results.items():
            proposal = result.get('proposal')
            if not proposal:
                row[mode] = {key: result.get(key) for key in ('error', 'elapsed_seconds', 'worker_wall_seconds')}
                continue
            row[mode] = {'elapsed_seconds': result['elapsed_seconds'], 'worker_wall_seconds': result.get('worker_wall_seconds'), 'roof_type': proposal['roof_type'],
                         'planes': len(proposal['annotation']['faces']), 'line_kinds': dict(Counter(line['kind'] for line in proposal['lines'])),
                         'geometry_passed': proposal['audit']['passed'], 'drainage': proposal['audit'].get('drainage'),
                         'original_segments': len(result['original_segments']), 'matches_stored_annotation': proposal['annotation'] == snapshot['object'].get('roof_annotation')}
            if reference:
                row[mode]['draft_agreement'] = line_comparison(proposal, reference)
        before, after = results['baseline'].get('proposal'), results['candidate'].get('proposal')
        if before and after:
            row['annotation_changed'] = before['annotation'] != after['annotation']
            row['baseline_original_segments_equal'] = results['baseline']['original_segments_sha256'] == results['candidate']['original_segments_sha256']
            row['optical_guidance'] = after.get('optical_guidance')
            guidance = after.get('optical_guidance') or {}
            if guidance.get('reason') == 'guidance-unavailable-baseline-retained' or guidance.get('error'):
                row['optical_stage_status'] = 'guidance-error-baseline-retained'
            elif guidance:
                row['optical_stage_status'] = 'evaluated'
            else:
                row['optical_stage_status'] = 'not-applied-flat' if after['roof_type'] == 'flat' else 'missing-optical-report'
            observations = (after.get('optical_guidance') or {}).get('observations', {})
            if 'preserved_original_segments' in observations:
                row['original_retention_passed'] = (
                    observations.get('original_segments') == len(results['baseline']['original_segments'])
                    and observations.get('preserved_original_segments') == observations.get('original_segments')
                    and observations.get('original_segment_ids') == observations.get('preserved_original_segment_ids'))
        card_path = input_path.with_name('comparison.png')
        card(snapshot, results['baseline'], results['candidate'], reference, card_path)
        row['card'] = str(card_path)
        summary['rows'].append(row)
        summary['elapsed_seconds'] = round(time.monotonic() - began, 3)
        write(out / 'summary.json', summary)
        print(json.dumps({'case': row['case'], 'changed': row.get('annotation_changed'),
                          'baseline': row.get('baseline', {}).get('error', row.get('baseline', {}).get('elapsed_seconds')),
                          'candidate': row.get('candidate', {}).get('error', row.get('candidate', {}).get('elapsed_seconds')),
                          'elapsed_seconds': summary['elapsed_seconds']}), flush=True)
    changes = []
    for case in dataset['rows']:
        for name in ('model', 'image'):
            source = case['source']
            if sha(Path(source[name + '_path']).read_bytes()) != source[name + '_sha256']:
                changes.append(source[name + '_path'])
        if case.get('reference_path'):
            reference = json.loads(Path(case['reference_path']).read_text())
            if sha(Path(reference['review_path']).read_bytes()) != reference['review_sha256']:
                changes.append(reference['review_path'])
    summary.update(completed_at=datetime.now(UTC).isoformat(), sources_unchanged=not changes,
                   changed_source_paths=sorted(set(changes)), code_unchanged=code_hashes(root) == code,
                   elapsed_seconds=round(time.monotonic() - began, 3))
    write(out / 'summary.json', summary)
    report_markdown(summary, out)
    print(json.dumps({'summary': str(out / 'summary.json'), 'completed_pairs': len(summary['rows']),
                      'sources_unchanged': not changes, 'code_unchanged': summary['code_unchanged'],
                      'elapsed_seconds': summary['elapsed_seconds']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'arm'))
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--seconds', type=float, default=420)
    parser.add_argument('--arm-seconds', type=float, default=30)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--mode', choices=('baseline', 'candidate'))
    args = parser.parse_args()
    root = args.root.resolve()
    out = args.out.resolve()
    if args.command == 'prepare':
        prepare(root, out)
    elif args.command == 'run':
        run(root, out, args.seconds, args.arm_seconds)
    else:
        if not (args.input and args.output and args.mode):
            parser.error('arm requires --input, --output and --mode')
        arm(args.input, args.output, args.mode)


if __name__ == '__main__':
    main()
