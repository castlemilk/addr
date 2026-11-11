"""Recompute roof proposals for chosen objects with the CURRENT code and compare.

A fast, SAM3-free harness for roof-form and topology changes: it reruns
``scene_roof.propose_roof`` on the saved outline (via
``scene_roof_diagnostics.diagnose_object``) and reports roof type, planes, crease
runs, image-contrast support and, where a reviewer saved roof-line edits, line
precision/recall against those edges. ``--compare BASELINE.json`` pairs two runs.
Outputs are diagnostics and reference agreement, never labels or accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

VERSION = 'roof-recompute-eval-v1'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _objects_from_feedback(path):
    doc = json.loads(Path(path).read_text())
    return [(item['scene_id'], item['object_id'], item.get('issues', [])) for item in doc.get('items', [])]


def _review_edges(root, scene_id, object_id, scene):
    """Reviewer roof-line edges in local metres, or None."""
    path = root / 'data/scenes' / scene_id / 'review.json'
    if not path.exists():
        return None
    review = json.loads(path.read_text())
    objects = review.get('objects')
    obj = objects.get(object_id) if isinstance(objects, dict) else next((o for o in objects or [] if o.get('id') == object_id), None)
    edits = (obj or {}).get('roof_line_edits') or {}
    vertices, edges = edits.get('vertices') or [], edits.get('edges') or []
    if not vertices or not edges:
        return None
    out = []
    for edge in edges:
        try:
            a, b = vertices[edge['a']], vertices[edge['b']]
            out.append({'a': [float(a[0]), float(a[1])], 'b': [float(b[0]), float(b[1])], 'kind': edge.get('kind')})
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out or None


def _line_pr(model_lines, ref_edges):
    """Precision/recall of roof lines against reviewer edges (reference_agreement.line_metrics)."""
    from .reference_agreement import line_metrics
    if not ref_edges:
        return None
    model = [(np.asarray(ln['line_meters'], dtype=float), ln.get('kind')) for ln in model_lines or [] if len(ln.get('line_meters') or []) == 2]
    refs = [(np.asarray([e['a'], e['b']], dtype=float), e.get('kind')) for e in ref_edges]
    m = line_metrics(model, refs)
    return {'precision': m.get('line_precision'), 'recall': m.get('line_recall'), 'kind_precision': m.get('line_kind_precision'),
            'n_model_lines': m.get('n_model_lines'), 'n_ref_edges': m.get('n_ref_edges')}


def _recomputed_lines(obj, scene, image, context_hint=None):
    """Roof lines from a fresh propose_roof call (same arguments as scene_house.add_roof_estimates)."""
    from .scene_roof import propose_roof
    frame = np.asarray(scene['pixel_to_meters'])
    def local(ring):
        return (np.c_[ring, np.ones(len(ring))] @ frame).tolist()
    try:
        proposal = propose_roof(local(obj['ring']), obj['model']['height_m'], 2., 'auto', image=image,
                                meters_to_pixel=scene['meters_to_pixel'], holes=[local(h) for h in obj.get('holes', [])],
                                **({} if context_hint is None else {'context_hint': context_hint}))
    except Exception as error:  # noqa: BLE001 - the harness reports, never hides, fitter failures
        return None, str(error)[:200]
    return proposal['lines'], None


def evaluate(root, targets, *, context_hints=True):
    from .scene_object_quality import class_hints_for_scene
    from .scene_roof_diagnostics import diagnose_object, load_scene
    rows, cache, hint_cache = [], {}, {}
    for scene_id, object_id, issues in targets:
        if scene_id not in cache:
            try:
                cache[scene_id] = load_scene(scene_id, root)
            except (OSError, ValueError, KeyError) as error:
                cache[scene_id] = error
        loaded = cache[scene_id]
        if isinstance(loaded, Exception):
            rows.append({'scene_id': scene_id, 'object_id': object_id, 'issues': issues, 'error': f'scene: {loaded}'})
            continue
        scene, image = loaded
        obj = next((o for o in scene['objects'] if o['id'] == object_id), None)
        if obj is None:
            rows.append({'scene_id': scene_id, 'object_id': object_id, 'issues': issues, 'error': 'object not in latest.json'})
            continue
        if scene_id not in hint_cache:
            try:
                hint_cache[scene_id] = class_hints_for_scene(scene, image=image, root=root)['hints'] if context_hints else {}
            except Exception as error:  # noqa: BLE001 - hints are optional; the row records their absence
                hint_cache[scene_id] = {'__error__': str(error)[:200]}
        hint = hint_cache[scene_id].get(object_id)
        payload = diagnose_object(obj, scene, image, context_hint=hint)
        form = payload.get('form_evidence') or {}
        before = (payload.get('topology') or {}).get('before') or {}
        row = {'scene_id': scene_id, 'object_id': object_id, 'issues': issues, 'area_m2': (obj.get('model') or {}).get('area_m2'),
               'roof_type_stored': obj.get('roof_type'), 'roof_type_recomputed': payload.get('roof_type_recomputed'),
               'recomputed_matches_stored': payload.get('recomputed_matches_stored'),
               'planes': before.get('physical_planes'), 'crease_runs': before.get('crease_runs'), 'short_junction_links': before.get('short_junction_links'),
               'observed_segments': before.get('observed_segments'), 'image_line_mismatch_m': before.get('image_line_mismatch_m'),
               'crease_contrast_share': (payload.get('crease_contrast') or {}).get('share_ge_threshold'),
               'form_uncertain': payload.get('form_uncertain'), 'context_hint': hint, 'context_hint_error': hint_cache[scene_id].get('__error__'),
               'form_decision': form.get('decision'), 'form_waived_gates': (form.get('context') or {}).get('waived_failed_gates'),
               'elapsed_seconds': payload.get('elapsed_seconds'), 'error': payload.get('error')}
        edges = _review_edges(root, scene_id, object_id, scene)
        row['line_pr_stored'] = _line_pr(obj.get('roof_lines'), edges)
        row['line_pr_recomputed'] = None
        if edges:
            lines, error = _recomputed_lines(obj, scene, image, hint)
            row['line_pr_recomputed'] = _line_pr(lines, edges) if lines is not None else None
            if error:
                row['recompute_error'] = error
        rows.append(row)
    return {'version': VERSION, 'code_sha256': CODE_SHA256, 'rows': rows,
            'note': 'Recomputed with the current fitter on saved outlines; diagnostics and reference agreement, not labels or accuracy.'}


def compare(baseline, candidate):
    def key(r):
        return (r['scene_id'], r['object_id'])
    before = {key(r): r for r in baseline['rows']}
    out = []
    for row in candidate['rows']:
        prior = before.get(key(row))
        if not prior:
            continue
        delta = {'scene_id': row['scene_id'], 'object_id': row['object_id'], 'issues': row.get('issues')}
        for field in ('roof_type_recomputed', 'planes', 'crease_runs', 'short_junction_links', 'crease_contrast_share', 'image_line_mismatch_m'):
            delta[field] = {'before': prior.get(field), 'after': row.get(field)}
        pb, pa = prior.get('line_pr_recomputed'), row.get('line_pr_recomputed')
        if pb and pa:
            delta['line_precision'] = {'before': pb.get('precision'), 'after': pa.get('precision')}
            delta['line_recall'] = {'before': pb.get('recall'), 'after': pa.get('recall')}
        out.append(delta)
    flips = sum(1 for d in out if d['roof_type_recomputed']['before'] != d['roof_type_recomputed']['after'])
    fewer = sum(1 for d in out if (d['crease_runs']['after'] or 0) < (d['crease_runs']['before'] or 0))
    return {'version': VERSION, 'paired': len(out), 'roof_type_changed': flips, 'fewer_creases': fewer, 'rows': out,
            'note': 'Paired recompute on identical outlines and pixels; counts describe the fitter change, not accuracy.'}


def table(rows):
    head = 'scene | object | issues | type stored->recomputed | planes | creases | short links | segs | contrast | line P/R (recomputed)'
    lines = [head]
    for r in rows:
        pr = r.get('line_pr_recomputed') or {}
        lines.append(' | '.join(str(x) for x in [r['scene_id'][-12:], r['object_id'][:8], ','.join(r.get('issues') or []), f"{r.get('roof_type_stored')}->{r.get('roof_type_recomputed')}",
                                                  r.get('planes'), r.get('crease_runs'), r.get('short_junction_links'), r.get('observed_segments'), r.get('crease_contrast_share'),
                                                  f"{pr.get('precision')}/{pr.get('recall')}" if pr else '-', r.get('error') or '']))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--feedback', type=Path, help='user-feedback JSON with items[{scene_id, object_id, issues}]')
    parser.add_argument('--objects', nargs='*', default=[], help='SCENE:OBJECT pairs')
    parser.add_argument('--out', type=Path, help='write the evaluation JSON here (inside data/reviews/eyes)')
    parser.add_argument('--compare', type=Path, help='baseline evaluation JSON to pair against')
    parser.add_argument('--table', action='store_true')
    parser.add_argument('--no-context-hints', action='store_true', help='image-only propose_roof calls (no building-class hints); the paired control')
    args = parser.parse_args(argv)
    targets = []
    if args.feedback:
        targets += _objects_from_feedback(args.feedback)
    for pair in args.objects:
        scene_id, object_id = pair.split(':', 1)
        targets.append((scene_id, object_id, []))
    if not targets:
        parser.error('give --feedback or --objects')
    result = evaluate(args.root, targets, context_hints=not args.no_context_hints)
    if args.out:
        if args.root.resolve() in args.out.resolve().parents and 'data/reviews/eyes' not in str(args.out.resolve()):
            parser.error('--out inside the repo must be under data/reviews/eyes')
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=1))
    if args.compare:
        diff = compare(json.loads(args.compare.read_text()), result)
        json.dump(diff, sys.stdout, indent=1)
        sys.stdout.write('\n')
    elif args.table:
        print(table(result['rows']))
    else:
        json.dump(result, sys.stdout, indent=1)
        sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
