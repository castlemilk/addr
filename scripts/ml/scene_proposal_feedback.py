"""Explicit proposal labels from partially reviewed training images.

Unmatched/unreviewed masks are ignored, never inferred negatives. These images
contribute fitting examples only; full-image evaluation remains unchanged.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from PIL import Image

from .review_authority import review_authority_blocker
from .scene_evaluation import evaluate_objects
from .scene_experiment import digest
from .scene_split import image_split_audit
from .scene_training import has_outline_issue


def supplemental_cases(dataset, kind, prompt, base_cases):
    dataset = Path(dataset)
    manifest = json.loads((dataset/'manifest.json').read_text())
    encoded = (dataset/'dataset.jsonl').read_text()
    version = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    if manifest['version'] != version:
        raise ValueError('Supplemental dataset differs from its frozen version')
    rows = [json.loads(line) for line in encoded.splitlines() if line.strip()]
    if len({row['scene_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate supplemental scene identities')
    # Include metadata from held-out rows solely to block geographic/image reuse.
    forbidden = [row for row in rows if row['split'] != 'train'] + [case for case in base_cases if case['split'] != 'train']
    forbidden_groups = {row.get('spatial_group') for row in forbidden}
    forbidden_images = {row.get('image_sha256') for row in forbidden}
    seen_ids = {case['scene_id'] for case in base_cases}
    seen_images = {case.get('image_sha256') for case in base_cases}
    cases, ignored = [], Counter()
    for row in rows:
        if row['split'] != 'train':
            continue
        if (row['scene_id'] in seen_ids or row['image_sha256'] in seen_images):
            ignored['already_used_image'] += 1
            continue
        review = row.get('review')
        if (row.get('export_schema_version', 0) < 5 or row.get('stale') or not review
                or review['model_signature'] != row['model_signature']):
            ignored['no_current_review'] += 1
            continue
        if (not row.get('spatial_group') or row['spatial_group'] in forbidden_groups
                or row['image_sha256'] in forbidden_images or row.get('image_geography', {}).get('status') != 'verified'):
            ignored['split_conflict'] += 1
            continue
        references = [o for o in review['objects'] if o['kind'] == kind and o['status'] == 'confirmed'
                      and o.get('geometry_reviewed') and not has_outline_issue(o) and not review_authority_blocker(o)]
        negatives = [o for o in review['objects'] if o['kind'] == kind and o['status'] == 'rejected'
                     and not review_authority_blocker(o)]
        if not references and not negatives:
            continue
        image_path = Path(row['image_path'])
        if digest(image_path) != row['image_sha256']:
            raise ValueError('Supplemental image bytes changed')
        with Image.open(image_path) as image:
            geography = image_split_audit({**row, 'image_width': image.width, 'image_height': image.height}, 'train')
        if geography['status'] != 'verified' or row['spatial_group'] not in {g['group'] for g in geography['groups']}:
            ignored['split_conflict'] += 1
            continue
        predictions, labels = [], []
        for obj in row['prediction']:
            if obj['kind'] != kind or obj.get('prompt') != prompt or obj.get('source') != 'sam3-text-onnx':
                continue
            positive = evaluate_objects([obj], references)['matched'] > 0
            negative = evaluate_objects([obj], negatives)['matched'] > 0
            if positive == negative:
                ignored['conflicting_proposal' if positive else 'unreviewed_proposal'] += 1
                continue
            predictions.append(obj)
            labels.append(int(positive))
        if predictions:
            cases.append({key: row[key] for key in ('scene_id', 'split', 'spatial_group', 'image_sha256', 'model_signature', 'image_path')}
                         | {'revision': review['revision'], 'predictions': predictions, 'labels': labels})
            seen_ids.add(row['scene_id'])
            seen_images.add(row['image_sha256'])
    return version, cases, dict(ignored)
