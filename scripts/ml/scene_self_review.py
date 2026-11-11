"""Audit roof proposals and try bounded, independently checked repairs.

This reviews model output, not human labels. A passing check establishes internal
consistency only. Original imagery, boundaries, edits and model snapshots survive.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import io
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
from PIL import Image
from shapely.errors import GEOSException
from shapely.geometry import Polygon, box

from .annotation_check import check_annotation
from .roof_line_fit import image_segments
from .roof_structure import _parts_from_faces, audit_roof
from .roof_topology import topology_cost
from .scene_batch import signature
from .scene_extract import save_scene
from .scene_roof import propose_roof
from .scene_split import image_split_audit
from .scene_training import atomic_json, atomic_text

VERSION = 'roof-self-review-v1'
POLICY = {'version': VERSION, 'candidate_rises_m': [1.5, 3.0],
          'max_image_mismatch_increase_m': .03, 'minimum_cost_gain': .2,
          'minimum_relative_cost_gain': .08,
          'complex_crease_runs': 16, 'complex_short_links': 2,
          'weak_image_mismatch_m': .75, 'max_repairs_default': 12}
GEOMETRY_ERRORS = (ValueError, TypeError, KeyError, IndexError, ArithmeticError, RuntimeError, GEOSException)
ROOF_KEYS = ('roof_annotation', 'roof_parts', 'roof_lines', 'roof_type',
             'roof_geometry_source', 'roof_geometry_note')


def local_outline(obj, scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    def convert(points):
        return (np.c_[points, np.ones(len(points))] @ frame).tolist()
    return convert(obj['ring']), [convert(h) for h in obj.get('holes', [])]


def mesh_parts(obj):
    return _parts_from_faces([(p['ring_meters'], p['ring_z']) for p in obj['roof_parts']],
                             obj['model']['height_m'], obj['roof_type'])


def issue(code, severity, message):
    return {'code': code, 'severity': severity, 'message': message}


def audit_object(obj, scene, image):
    """Recompute checks from emitted geometry, never trust proposal audit flags."""
    result = {'object_id': obj['id'], 'kind': obj['kind'], 'issues': [], 'geometry_passed': False}
    flags = result['issues']
    try:
        ring, holes = local_outline(obj, scene)
        polygon = Polygon(ring, holes)
        if not polygon.is_valid or polygon.area < 1:
            raise ValueError('Invalid or sub-square-metre roof outline')
        result['area_m2'] = round(polygon.area, 3)
        pixels = Polygon(obj['ring'], obj.get('holes', []))
        if pixels.distance(box(0, 0, scene['image_width'], scene['image_height']).boundary) <= 1:
            flags.append(issue('image_cut', 'context', 'Roof touches the image edge; capture more context before changing its end.'))
        if (obj.get('boundary_refinement', {}).get('construction', {}).get('inferred_edges')):
            flags.append(issue('hidden_eaves', 'review', 'Some eaves are inferred behind tree canopy.'))
        if not obj.get('roof_parts') or not obj.get('roof_annotation'):
            flags.append(issue('missing_surface', 'defect', 'The outline has no complete editable roof surface.'))
            return result
        eave = obj['model']['height_m']
        parts = mesh_parts(obj)
        audit = audit_roof(parts, polygon, eave, pitched=obj['roof_type'] != 'flat')
        graph = check_annotation(obj['roof_annotation'], ring, eave, holes)
        result['mesh_audit'], result['annotation_audit'] = audit, graph
        passed = (audit['passed'] and graph['passed']
                  and audit.get('drainage', {}).get('passed') is True
                  and graph.get('drainage', {}).get('passed') is True)
        if not passed:
            flags.append(issue('invalid_surface', 'defect', 'Surface or editable graph failed coverage, planarity, seam or runoff checks.'))
        # The editor and rendered mesh must describe the same height field.
        vertices = obj['roof_annotation']['vertices']
        graph_parts = _parts_from_faces([([vertices[i][:2] for i in face], [vertices[i][2] for i in face])
                                       for face in obj['roof_annotation']['faces']], eave, obj['roof_type'])
        mismatch = 0.
        for first in parts:
            for second in graph_parts:
                overlap = Polygon(first['coords']).intersection(Polygon(second['coords']))
                if overlap.area < 1e-7:
                    continue
                delta = np.array(list(first['plane'].values())) - np.array(list(second['plane'].values()))
                shapes = [overlap] if overlap.geom_type == 'Polygon' else list(overlap.geoms)
                for shape in shapes:
                    if shape.geom_type == 'Polygon':
                        points = np.array(shape.exterior.coords)
                        mismatch = max(mismatch, float(np.max(np.abs(np.c_[points, np.ones(len(points))] @ delta))))
        if mismatch > .001:
            passed = False
            flags.append(issue('editor_mesh_mismatch', 'defect', 'Rendered roof and editable vertices disagree.'))
        result['geometry_passed'] = passed
        observations = image_segments(image, polygon, scene['meters_to_pixel'])
        cost = topology_cost(parts, observations)
        result['cost'] = cost
        if cost['crease_runs'] > POLICY['complex_crease_runs'] or cost['short_junction_links'] >= POLICY['complex_short_links'] or cost['small_planes'] >= 2:
            flags.append(issue('complex_folds', 'review', 'Many folds, short junctions or tiny planes warrant a simpler candidate comparison.'))
        if len(observations) < 3:
            flags.append(issue('weak_image_evidence', 'review', 'Too few usable image lines to verify the roof topology.'))
        elif cost['image_line_mismatch_m'] > POLICY['weak_image_mismatch_m']:
            flags.append(issue('image_line_disagreement', 'review', 'Detected image lines and roof creases disagree; texture or solar panels may contribute.'))
    except GEOMETRY_ERRORS as error:
        result['geometry_passed'] = False
        flags.append(issue('uncheckable_geometry', 'defect', str(error)))
    return result


def audit_scene(scene, image):
    rows = [audit_object(obj, scene, image) for obj in scene['objects'] if obj['kind'] == 'building' and obj['status'] != 'superseded']
    buildings = {obj['id']: obj for obj in scene['objects'] if obj['kind'] == 'building' and obj['status'] != 'superseded'}
    for obj in scene['objects']:
        if obj['kind'] != 'solar_panel' or obj['status'] == 'superseded':
            continue
        flags = []
        try:
            shape = Polygon(obj['ring'], obj.get('holes', []))
            parent = buildings.get(obj.get('parent_id'))
            coverage = shape.intersection(Polygon(parent['ring'], parent.get('holes', []))).area/shape.area if parent else 0.
            if coverage < .85:
                flags.append(issue('solar_roof_association', 'review', 'Solar region lacks a roof parent covering at least 85% of its outline.'))
        except GEOMETRY_ERRORS:
            flags.append(issue('solar_geometry', 'defect', 'Solar region or its parent has invalid geometry.'))
        rows.append({'object_id': obj['id'], 'kind': obj['kind'], 'issues': flags})
    return rows


def repair_allowed(before, after):
    """Hard validity plus an independent image veto; simplicity alone cannot win."""
    if not after['geometry_passed']:
        return False
    if not before['geometry_passed']:
        # There is no reliable image comparison with a missing/broken mesh.
        # Require usable evidence before publishing a newly fitted surface.
        return after['cost']['observed_segments'] >= 3 and after['cost']['image_line_mismatch_m'] <= POLICY['weak_image_mismatch_m']
    a, b = before['cost'], after['cost']
    return (b['observed_segments'] == a['observed_segments'] and b['observed_segments'] >= 3
            and b['image_line_mismatch_m'] <= a['image_line_mismatch_m'] + POLICY['max_image_mismatch_increase_m']
            and a['score']-b['score'] >= max(POLICY['minimum_cost_gain'], POLICY['minimum_relative_cost_gain']*a['score'])
            and b['construction_complexity'] < a['construction_complexity'])


def repair_object(obj, scene, image, before):
    ring, holes = local_outline(obj, scene)
    attempts, best, best_audit = [], None, before
    for rise in POLICY['candidate_rises_m']:
        try:
            proposal = propose_roof(ring, obj['model']['height_m'], rise, 'auto', image=image,
                                    meters_to_pixel=scene['meters_to_pixel'], holes=holes)
            candidate = {**copy.deepcopy(obj), 'roof_annotation': proposal['annotation'], 'roof_parts': proposal['parts'],
                         'roof_lines': proposal['lines'], 'roof_type': proposal['roof_type'],
                         'roof_geometry_source': proposal['source'], 'roof_geometry_note': proposal['note']}
            checked = audit_object(candidate, scene, image)
            accepted = repair_allowed(before, checked)
            attempts.append({'rise_m': rise, 'eligible': accepted, 'audit': checked})
            if accepted and (best is None or checked['cost']['score'] < best_audit['cost']['score']):
                best, best_audit = candidate, checked
        except GEOMETRY_ERRORS as error:
            attempts.append({'rise_m': rise, 'eligible': False, 'error': str(error)})
    return best, best_audit, attempts


def validate_id(value):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value):
        raise ValueError('Invalid scene ID')
    return value


def publish_repair(scene_id, candidate, original_bytes, image_bytes, root):
    """Use the same review mutex as the API, then compare input identities again."""
    folder = root/'data/scenes'/validate_id(scene_id)
    lock = folder/'review.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        return None, 'review_in_progress'
    try:
        if (folder/'review.json').exists():
            return None, 'saved_review_preserved'
        if (folder/'latest.json').read_bytes() != original_bytes:
            return None, 'model_changed'
        image_path = folder/f"{candidate['image_sha256']}.png"
        if image_path.read_bytes() != image_bytes:
            return None, 'image_changed'
        original = json.loads(original_bytes)
        if candidate['id'] != original['id'] or candidate['id'] != scene_id:
            raise ValueError('Scene identity changed')
        # Verify all non-roof fields, including boundaries, status, frame and
        # every unrelated object. A solver cannot broaden its own write scope.
        def fixed(value):
            result = copy.deepcopy(value)
            result.pop('model_signature', None)
            result.pop('self_review', None)
            for obj in result['objects']:
                for key in ROOF_KEYS:
                    obj.pop(key, None)
            return result
        if fixed(original) != fixed(candidate):
            raise ValueError('Repair changed fields outside the roof surface')
        return save_scene(candidate, image_bytes, root), 'published'
    finally:
        lock.rmdir()


def run_review(scene_ids, root=Path('.'), *, repair=False, max_repairs=12, protected=()):
    if not 0 <= max_repairs <= 50 or not 1 <= len(scene_ids) <= 100:
        raise ValueError('Review 1–100 scenes with at most 50 repair attempts')
    scene_ids = list(dict.fromkeys(validate_id(s) for s in scene_ids))
    frozen = {}
    for scene_id in scene_ids:
        folder = root/'data/scenes'/scene_id
        data = (folder/'latest.json').read_bytes()
        model = json.loads(data)
        if model['id'] != scene_id or not re.fullmatch(r'[a-f0-9]{64}', model['image_sha256']):
            raise ValueError('Invalid scene/image identity')
        payload = (folder/f"{model['image_sha256']}.png").read_bytes()
        if hashlib.sha256(payload).hexdigest() != model['image_sha256']:
            raise ValueError('Source image checksum changed')
        frozen[scene_id] = (data, payload)
    code = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'scripts/ml').glob('*.py'))}
    config = {'policy': POLICY, 'repair': repair, 'max_repairs': max_repairs, 'protected': sorted(protected),
              'inputs': {k: hashlib.sha256(v[0]).hexdigest() for k, v in frozen.items()}, 'code': code}
    directory = root/'data/scene-self-reviews'/signature(config)[:16]
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory/'config.json', config)
    report = {'version': VERSION, 'run_id': directory.name, 'scenes': [], 'repairs_attempted': 0,
              'scope': 'Model consistency checks, not measured accuracy, training labels or neural weight updates.'}
    for scene_id, (original_bytes, image_bytes) in frozen.items():
        scene = json.loads(original_bytes)
        folder = root/'data/scenes'/scene_id
        image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        if image.size != (scene['image_width'], scene['image_height']):
            raise ValueError('Image dimensions changed')
        geography = image_split_audit(scene, scene.get('region', {}).get('split', 'unassigned'))
        forward, inverse = np.asarray(scene['pixel_to_meters']), np.asarray(scene.get('meters_to_pixel'))
        valid_inverse = inverse.shape == (3, 2) and np.isfinite(inverse).all()
        if valid_inverse and forward.shape == (3, 2):
            probes = np.array([[0, 0], [scene['image_width'], 0], [0, scene['image_height']]])
            metres = np.c_[probes, np.ones(3)] @ forward
            valid_inverse = np.max(np.abs(np.c_[metres, np.ones(3)] @ inverse-probes)) < .01
        if geography['status'] == 'invalid_frame' or not valid_inverse:
            report['scenes'].append({'scene_id': scene_id, 'error': 'invalid_frame', 'rows': [], 'repairs': []})
            continue
        rows = audit_scene(scene, image)
        candidate, repairs = copy.deepcopy(scene), []
        blocked = ('saved_review_preserved' if (folder/'review.json').exists() else
                   'protected_baseline' if scene_id in protected else
                   'held_out_or_mixed_geography' if geography['status'] != 'verified' or geography['splits'] != ['train'] else None)
        if repair and not blocked:
            for row in sorted(rows, key=lambda r: (-sum(i['severity'] == 'defect' for i in r['issues']), r['object_id'])):
                codes = {i['code'] for i in row['issues']}
                if (row['kind'] != 'building' or 'image_cut' in codes
                        or not codes.intersection({'missing_surface', 'invalid_surface', 'complex_folds', 'image_line_disagreement', 'editor_mesh_mismatch'})
                        or report['repairs_attempted'] >= max_repairs):
                    continue
                obj = next(o for o in candidate['objects'] if o['id'] == row['object_id'])
                if obj.get('source') != 'sam3-text-onnx' or obj.get('status') != 'candidate' or obj.get('geometry_reviewed'):
                    continue
                report['repairs_attempted'] += 1
                result, after, attempts = repair_object(obj, scene, image, row)
                repairs.append({'object_id': obj['id'], 'selected': result is not None, 'before': row, 'after': after, 'attempts': attempts})
                if result:
                    candidate['objects'][candidate['objects'].index(obj)] = result
        status = blocked or 'unchanged'
        if any(r['selected'] for r in repairs):
            if any(hashlib.sha256((root/'scripts/ml'/name).read_bytes()).hexdigest() != sha for name, sha in code.items()):
                raise ValueError('Inference or review code changed during the run')
            candidate['self_review'] = {'version': VERSION, 'run_id': directory.name,
                                        'baseline_signature': scene['model_signature'],
                                        'changed_objects': [r['object_id'] for r in repairs if r['selected']]}
            (directory/f'{scene_id}.before.json').write_bytes(original_bytes)
            atomic_json(directory/f'{scene_id}.candidate.json', candidate)
            saved, status = publish_repair(scene_id, candidate, original_bytes, image_bytes, root)
            if saved:
                scene = saved
                rows = audit_scene(scene, image)
        for row in rows:
            row['review_url'] = '/scenes?' + urlencode({'scene': scene_id, 'kind': row['kind'], 'status': 'all', 'object': row['object_id'], 'layer': 'model'})
        report['scenes'].append({'scene_id': scene_id, 'title': scene['title'], 'publication': status,
                                 'model_signature': scene['model_signature'], 'geography': geography, 'rows': rows, 'repairs': repairs})
        atomic_json(directory/'report.json', report)
        print(json.dumps({'scene': scene_id, 'status': status, 'objects_with_findings': sum(bool(r['issues']) for r in rows)}), flush=True)
    all_rows = [r for s in report['scenes'] for r in s['rows']]
    report['counts'] = {'scenes': len(scene_ids), 'roof_observations': sum(r['kind'] == 'building' for r in all_rows),
                        'solar_observations': sum(r['kind'] == 'solar_panel' for r in all_rows),
                        'geometry_passed': sum(bool(r.get('geometry_passed')) for r in all_rows),
                        'published_repairs': sum(r['selected'] for s in report['scenes'] if s.get('publication') == 'published' for r in s['repairs']),
                        'findings': dict(Counter(i['code'] for r in all_rows for i in r['issues']))}
    atomic_json(directory/'report.json', report)
    queue = sorted([(s, r) for s in report['scenes'] for r in s['rows'] if r['issues']],
                   key=lambda pair: (-sum(i['severity'] == 'defect' for i in pair[1]['issues']),
                                     any(i['code'] == 'image_cut' for i in pair[1]['issues']),
                                     -len(pair[1]['issues']), pair[0]['scene_id'], pair[1]['object_id']))
    atomic_text(directory/'REVIEW.md', '\n'.join([
        '# Nearby roof self-review', '', report['scope'], '',
        'Counts are observations across overlapping captures, not unique houses. Heights remain estimates.', '',
        '```json', json.dumps(report['counts'], indent=2), '```', '',
        '## Review queue', '',
        '| Capture (may include neighbours) | Object | Findings |', '| --- | --- | --- |',
        *[f'| {s["title"]} | [{r["kind"]} {r["object_id"][-6:]}](http://localhost:3008{r["review_url"]}) | '+
          ', '.join(i['code'] for i in r['issues'])+' |' for s, r in queue], '',
        'Image cuts need a larger capture. Hidden eaves and image disagreement need image review. '
        'Repair candidates change only roof surfaces; saved reviews and held-out/mixed-split captures are excluded. '
        'A simpler candidate must independently pass geometry/runoff and the image-evidence veto. '
        'Every remaining finding stays in this queue; nothing is automatically confirmed for training.', '']))
    return report, directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--scenes', nargs='+')
    group.add_argument('--batch')
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--max-repairs', type=int, default=POLICY['max_repairs_default'])
    parser.add_argument('--protect', nargs='*', default=[])
    args = parser.parse_args()
    root = Path('.')
    if args.batch:
        if not re.fullmatch(r'[a-f0-9]{16}', args.batch):
            parser.error('Choose a valid batch ID')
        state = json.loads((root/'data/scene-batches'/args.batch/'run.json').read_text())
        args.scenes = [sid for sid, row in state['results'].items() if row['status'] == 'complete']
    with (root/'data/scenes/.extraction.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report, directory = run_review(args.scenes, root, repair=args.repair, max_repairs=args.max_repairs, protected=args.protect)
    print(json.dumps({'report': str(directory/'report.json'), 'review_queue': str(directory/'REVIEW.md'), **report['counts']}))


if __name__ == '__main__':
    main()
