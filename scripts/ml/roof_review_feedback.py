"""Bind agent review notes and current CRM feedback to frozen review inputs.

Read-only: a scene review file alone does not mean this roof was corrected.
Agent notes never become training labels or human review decisions.
"""
from __future__ import annotations

import hashlib
import json

from .building_addresses import resolve_scene

FIELDS = ('ring', 'holes', 'roof_line_edits', 'roof_annotation', 'roof_type',
          'visibility', 'notes', 'review_issues', 'kind', 'status', 'geometry_reviewed', 'roof_feedback')
IDENTITY = ('scene_id', 'object_id', 'source_model', 'input_sha256')
DEFAULTS = {'holes': [], 'review_issues': [], 'notes': '', 'geometry_reviewed': False}


def enrich(root, directory, rows):
    notes_path = directory/'reflections.json'
    notes, notes_error = [], None
    if notes_path.exists():
        try:
            payload = json.loads(notes_path.read_text())
            if payload.get('schema_version') != 1 or not isinstance(payload.get('rows'), list):
                raise ValueError('Invalid reflection schema')
            notes = payload['rows']
        except (OSError, ValueError, AttributeError):
            notes_error = 'Review notes are unavailable; model results are unchanged.'
    scenes = {}
    addresses = {}
    result = []
    for index, original in enumerate(rows):
        row = dict(original)
        matches = [n for n in notes if isinstance(n, dict) and all(n.get(k) == row.get(k) for k in IDENTITY)]
        if len(matches) == 1 and all(isinstance(matches[0].get(k), str) and len(matches[0][k]) <= 2000
                                     for k in ('title', 'observed', 'question', 'next_experiment', 'category')):
            row['reflection'] = {k: matches[0][k] for k in ('title', 'observed', 'question', 'next_experiment', 'category')}
        if notes_error:
            row['reflection_warning'] = notes_error
        feedback = {'state': 'awaiting_review', 'changed_fields': []}
        try:
            sid = row['scene_id']
            if sid not in scenes:
                folder = root/'data/scenes'/sid
                live = (folder/'latest.json').read_bytes()
                path = folder/'review.json'
                scenes[sid] = (hashlib.sha256(live).hexdigest(), json.loads(path.read_text()) if path.exists() else None)
                scene = json.loads(live)
                saved = scenes[sid][1]
                objects = saved.get('objects', []) if saved and saved.get('model_signature') == scene.get('model_signature') else scene.get('objects', [])
                addresses[sid] = resolve_scene(scene, objects, root)
            current_hash, review = scenes[sid]
            row['property_address'] = addresses.get(sid, {}).get(row['object_id'])
            if current_hash != row['input_sha256']:
                feedback['state'] = 'source_changed'
            elif review is not None:
                if review.get('model_signature') != row['source_model']:
                    feedback['state'] = 'needs_recovery'
                else:
                    source = json.loads((directory/f'input-{index}.json').read_text())
                    before = next(o for o in source['objects'] if o['id'] == row['object_id'])
                    edited = next((o for o in review.get('objects', []) if o['id'] == row['object_id']), None)
                    if edited:
                        changed = [k for k in FIELDS if before.get(k, DEFAULTS.get(k)) != edited.get(k, DEFAULTS.get(k))]
                        feedback.update(changed_fields=changed, revision=review.get('revision'), updated_at=review.get('updated_at'))
                        if changed:
                            feedback['state'] = 'feedback_saved'
        except (OSError, ValueError, KeyError, StopIteration, TypeError, AttributeError):
            feedback['state'] = 'unavailable'
        row['feedback'] = feedback
        result.append(row)
    return result
