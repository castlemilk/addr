"""Export versioned scene references without promoting predictions into labels."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
from shapely import contains_xy
from shapely.geometry import Polygon

from .review_authority import review_authority_blocker
from .scene_evaluation import evaluate_dataset
from .scene_roof_feedback import roof_targets
from .scene_split import image_split_audit

SCENE_CLASSES = ('building', 'tree', 'road', 'car', 'solar_panel', 'skylight', 'chimney',
                 'pool', 'driveway', 'footpath', 'vegetation', 'water', 'other')
SPLITS = ('train', 'validation', 'test')
EXPORT_SCHEMA_VERSION = 6
REVIEW_ISSUES = ('outline', 'class', 'instances', 'shape_3d', 'height', 'placement')
OUTLINE_ISSUES = frozenset(('outline', 'class', 'instances'))


def has_outline_issue(obj):
    return bool(OUTLINE_ISSUES.intersection(obj.get('review_issues', [])))


def mask_rle(polygon, width, height):
    """COCO uncompressed column-major RLE keeps holes as background pixels."""
    mask = contains_xy(polygon, np.arange(width)[None, :] + .5, np.arange(height)[:, None] + .5)
    values = mask.ravel(order='F')
    transitions = np.r_[0, np.flatnonzero(values[1:] != values[:-1]) + 1, len(values)]
    counts = np.diff(transitions).tolist()
    if values[0]:
        counts.insert(0, 0)
    return {'size': [height, width], 'counts': counts}, int(mask.sum())


def normalized_split(value):
    return 'validation' if value == 'val' else value if value in SPLITS else 'unassigned'


def collect_scenes(root):
    # Both the coverage screen and export use this exact eligibility decision.
    rows = []
    models = {}
    for source in sorted((root/'data/scenes').glob('*/latest.json')):
        if source.parent.name.startswith('qa_') or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', source.parent.name):
            continue
        model = json.loads(source.read_text())
        if model['id'] != source.parent.name or not re.fullmatch(r'[a-f0-9]{64}', model['image_sha256']):
            raise ValueError('Invalid scene or image identity')
        models[model['id']] = model
        review_path = source.parent/'review.json'
        review = json.loads(review_path.read_text()) if review_path.exists() else None
        current = review is not None and review['model_signature'] == model['model_signature']
        for obj in review['objects'] if current else []:
            issues = obj.get('review_issues', [])
            if not isinstance(issues, list) or any(not isinstance(issue, str) or issue not in REVIEW_ISSUES for issue in issues) or len(set(issues)) != len(issues):
                raise ValueError(f'Invalid review feedback {obj["id"]}')
        eligible = [o for o in review['objects'] if o['status'] == 'confirmed' and o['geometry_reviewed']
                    and not has_outline_issue(o) and not review_authority_blocker(o)] if current else []
        for obj in eligible:
            if 'visibility' in obj and obj['visibility'] not in ('full', 'occluded', 'truncated', 'occluded_truncated'):
                raise ValueError(f'Invalid visibility label {obj["id"]}')
            polygon = Polygon(obj['ring'], obj.get('holes'))
            if not polygon.is_valid or polygon.area < 1:
                raise ValueError(f'Invalid reviewed polygon {obj["id"]}')
        image_path = source.parent/f"{model['image_sha256']}.png"
        if not image_path.exists() or hashlib.sha256(image_path.read_bytes()).hexdigest() != model['image_sha256']:
            raise ValueError(f'Image bytes do not match scene {model["id"]}')
        row = {'export_schema_version': EXPORT_SCHEMA_VERSION, 'scene_id': model['id'], 'image_path': str(image_path), 'image_sha256': model['image_sha256'],
               'image_width': model['image_width'], 'image_height': model['image_height'],
               'pixel_to_meters': model['pixel_to_meters'], 'centroid': model['centroid'], 'model_signature': model['model_signature'],
               'prediction': model['objects'],
               # A right swipe verifies the class and outline, not roof apexes
               # or height priors. Keep those estimates in the review audit.
               'outline_semantics': 'visible',
               'reference': [{key: obj[key] for key in ('id', 'kind', 'label', 'ring', 'holes', 'visibility', 'group_id', 'parent_id', 'derived_from') if key in obj} for obj in eligible],
               'false_positives': [o for o in review['objects'] if o['status'] == 'rejected'
                                   and not review_authority_blocker(o)] if current else [],
               'feedback': [{'id': o['id'], 'kind': o['kind'], 'issues': o['review_issues']}
                            for o in review['objects'] if o.get('review_issues') and o['status'] != 'superseded'] if current else [],
               'groups': review['groups'] if current else [], 'review': review, 'stale': bool(review and not current)}
        if model.get('region'):
            entry = model['region']
        else:
            cohort_path = root/'data/cohorts'/f"{model['cohort']}.json"
            cohort = json.loads(cohort_path.read_text())
            entry = next(e for e in cohort['entries'] if e['address_id'] == model['address_id'])
        row.update(split=entry.get('split', 'unassigned'), spatial_group=entry.get('spatial_group'))
        row['image_geography'] = image_split_audit(model, row['split'])
        rows.append(row)
    # Repeated views of a physical area must not cross evaluation splits.
    group_splits, image_splits = defaultdict(set), defaultdict(set)
    for row in rows:
        split = normalized_split(row['split'])
        if split != 'unassigned':
            if row['spatial_group']:
                group_splits[row['spatial_group']].add(split)
            image_splits[row['image_sha256']].add(split)
    classes, layers, roof_layers = {}, [], []
    for row in rows:
        model = models[row['scene_id']]
        review = row['review']
        current = review is not None and not row['stale']
        objects = review['objects'] if current else model['objects']
        geography_blockers = []
        if row['image_geography']['status'] != 'verified':
            geography_blockers.append('image_geography')
        if row['stale']:
            geography_blockers.append('stale')
        if normalized_split(row['split']) == 'unassigned':
            geography_blockers.append('unassigned')
        if not row['spatial_group']:
            geography_blockers.append('missing_group')
        if len(group_splits[row['spatial_group']]) > 1 or len(image_splits[row['image_sha256']]) > 1:
            geography_blockers.append('split_conflict')
        row['roof_reference'] = []
        for obj in review['objects'] if review else []:
            if obj['kind'] != 'building' or not obj.get('roof_feedback') or obj['status'] == 'superseded':
                continue
            reference, checked, blocked = roof_targets(obj, model, geography_blockers)
            roof_layers.append({'scene_id': row['scene_id'], 'object_id': obj['id'], 'title': obj['label'],
                                'split': normalized_split(row['split']), 'spatial_group': row['spatial_group'],
                                'review_url': '/scenes?' + urlencode({'scene': row['scene_id'], 'kind': 'building', 'status': 'all', 'object': obj['id']}),
                                'checked': checked, 'eligible': {target: target in reference for target in checked}, 'blockers': blocked})
            if reference:
                row['roof_reference'].append({'id': obj['id'], 'ring': obj['ring'], 'holes': obj.get('holes', []),
                                             **reference, 'supervision': 'reviewed-image-observation', 'height_supervision': 'none'})
        for kind in SCENE_CLASSES:
            selected = [o for o in objects if o['kind'] == kind]
            confirmed = [o for o in row['reference'] if o['kind'] == kind]
            pending = [o for o in selected if not current or o['status'] in ('candidate', 'needs_adjustment')
                       or (o['status'] != 'superseded' and review_authority_blocker(o))
                       or (o['status'] == 'confirmed' and (not o.get('geometry_reviewed') or has_outline_issue(o)))]
            complete = bool(current and kind in review['reviewed_classes'])
            blockers = list(geography_blockers)
            if pending:
                blockers.append('pending')
            if not complete:
                blockers.append('unchecked')
            target = next((o for o in pending if o['status'] == 'needs_adjustment'), None)
            if target is None:
                target = pending[0] if pending else confirmed[0] if confirmed else None
            query = {'scene': row['scene_id'], 'kind': kind, 'status': 'all'}
            if target:
                query['object'] = target['id']
            scan = model.get('scans', {}).get(kind, {})
            layers.append({'scene_id': row['scene_id'], 'kind': kind,
                           'title': (model.get('title', row['scene_id']).split(',')[0] if model.get('address_id')
                                     else model.get('title', row['scene_id'])),
                           'split': normalized_split(row['split']), 'spatial_group': row['spatial_group'],
                           'image_url': '/api/scenes/image?' + urlencode({'id': row['scene_id'], 'version': row['image_sha256']}),
                           'review_url': '/scenes?' + urlencode(query),
                           'revision': review.get('revision', 0) if review else 0,
                           'image_geography': row['image_geography'],
                           'scan': scan.get('status', 'not_scanned'), 'scan_count': scan.get('count', 0),
                           'confirmed': len(confirmed), 'pending': len(pending),
                           'rejected': sum(o['kind'] == kind for o in row['false_positives']),
                           'complete': complete, 'eligible': not blockers, 'blockers': blockers})
            if blockers:
                continue
            dataset = classes.setdefault(kind, {'images': [], 'annotations': [], 'categories': [{'id': 1, 'name': kind}]})
            image_id = len(dataset['images']) + 1
            dataset['images'].append({'id': image_id, 'file_name': row['image_path'], 'width': model['image_width'],
                                      'height': model['image_height'], 'scene_id': model['id'], 'split': row['split'],
                                      'spatial_group': row['spatial_group']})
            for obj in row['reference']:
                if obj['kind'] != kind:
                    continue
                polygon = Polygon(obj['ring'], obj.get('holes'))
                if not polygon.is_valid or polygon.area <= 0:
                    raise ValueError(f'Invalid reviewed polygon {obj["id"]}')
                x1, y1, x2, y2 = polygon.bounds
                segmentation, area = mask_rle(polygon, model['image_width'], model['image_height']) if obj.get('holes') else ([np.asarray(obj['ring']).ravel().tolist()], polygon.area)
                if area <= 0:
                    raise ValueError(f'Reviewed polygon {obj["id"]} has no mask pixels')
                dataset['annotations'].append({'id': len(dataset['annotations'])+1, 'image_id': image_id,
                    'category_id': 1, 'segmentation': segmentation,
                    'area': area, 'bbox': [x1, y1, x2-x1, y2-y1], 'iscrowd': 0,
                    'attributes': {'outline': 'visible', 'visibility': obj.get('visibility', 'unassessed')}})
    encoded = '\n'.join(json.dumps(row, sort_keys=True) for row in rows)+'\n'
    version = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    coverage = []
    for kind in SCENE_CLASSES:
        subset = [layer for layer in layers if layer['kind'] == kind]
        splits = {}
        for split in SPLITS:
            ready = [layer for layer in subset if layer['eligible'] and layer['split'] == split]
            splits[split] = {'images': len(ready), 'masks': sum(layer['confirmed'] for layer in ready),
                             'empty_images': sum(layer['confirmed'] == 0 for layer in ready),
                             'groups': len({layer['spatial_group'] for layer in ready})}
        coverage.append({'kind': kind, 'references': sum(layer['confirmed'] for layer in subset),
                         'pending': sum(layer['pending'] for layer in subset),
                         'withheld': sum(layer['confirmed'] for layer in subset if not layer['eligible']),
                         'splits': splits, 'layers': subset})
    report = {'version': version, 'scenes': len(rows),
              'reviewed_objects': sum(len(row['reference']) for row in rows),
              'feedback_issues': {issue: sum(issue in entry['issues'] for row in rows for entry in row['feedback']) for issue in REVIEW_ISSUES},
              'false_positives': sum(len(row['false_positives']) for row in rows), 'classes': coverage}
    report['roof_feedback'] = {'layers': roof_layers, 'targets': {target: {
        'checked': sum(layer['checked'][target] for layer in roof_layers),
        'withheld': sum(layer['checked'][target] and not layer['eligible'][target] for layer in roof_layers),
        'splits': {split: {'objects': sum(layer['eligible'][target] and layer['split'] == split for layer in roof_layers),
                           'groups': len({layer['spatial_group'] for layer in roof_layers if layer['eligible'][target] and layer['split'] == split})}
                   for split in SPLITS}} for target in ('family', 'linework')}}
    return encoded, classes, report


def scene_coverage(root=Path('.')):
    encoded, _, report = collect_scenes(root)
    report['evaluation'] = evaluate_dataset(encoded, report)
    latest_path = root/'data/training/scenes/latest.json'
    report['latest_export'] = json.loads(latest_path.read_text()) if latest_path.exists() else None
    report['export_current'] = bool(report['latest_export'] and report['latest_export']['version'] == report['version'])
    return report


def atomic_text(path, value):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temp:
        try:
            temp.write(value)
            temp.close()
            os.replace(temp.name, path)
        finally:
            Path(temp.name).unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2))


def export_scenes(root=Path('.')):
    directory = root/'data/training/scenes'
    directory.mkdir(parents=True, exist_ok=True)
    # Serialize snapshots so an older concurrent export cannot publish last.
    with (directory/'.export.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _export_scenes(root)


def _export_scenes(root):
    encoded, classes, coverage = collect_scenes(root)
    version = coverage['version']
    out = root/'data/training/scenes'/version
    out.mkdir(parents=True, exist_ok=True)
    atomic_text(out/'dataset.jsonl', encoded)
    roof_rows = [{'schema_version': 1, 'dataset_version': version,
                  **{key: row[key] for key in ('scene_id', 'image_path', 'image_sha256', 'image_width', 'image_height', 'model_signature', 'split', 'spatial_group')},
                  'revision': row['review']['revision'], 'reference': obj}
                 for row in map(json.loads, encoded.splitlines()) for obj in row['roof_reference']]
    atomic_text(out/'roof-feedback.jsonl', ''.join(json.dumps(row, sort_keys=True) + '\n' for row in roof_rows))
    for kind, dataset in classes.items():
        atomic_json(out/f'{kind}-coco.json', dataset)
    atomic_json(out/'evaluation.json', evaluate_dataset(encoded, coverage))
    manifest = {'version': version, 'scenes': coverage['scenes'], 'reviewed_objects': coverage['reviewed_objects'],
                'feedback_issues': coverage['feedback_issues'],
                'false_positives': coverage['false_positives'],
                'class_exports': {k: len(v['images']) for k, v in classes.items()},
                'class_splits': {c['kind']: c['splits'] for c in coverage['classes'] if c['kind'] in classes},
                'roof_feedback': {'dataset': str(out/'roof-feedback.jsonl'), 'objects': len(roof_rows), 'targets': coverage['roof_feedback']['targets']},
                'outline_semantics': 'Visible portions only. Unassessed visibility does not mean a complete object.',
                'height_supervision': 'Heights and roof apexes are excluded from scene outline targets.',
                'training_status': 'Review required. Exporting labels does not update detector weights.',
                'dataset': str(out/'dataset.jsonl'), 'evaluation': str(out/'evaluation.json')}
    atomic_json(out/'manifest.json', manifest)
    atomic_json(out.parent/'latest.json', manifest)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coverage', action='store_true', help='Read current label coverage without exporting or training')
    args = parser.parse_args()
    print(json.dumps(scene_coverage() if args.coverage else export_scenes(), indent=2))
