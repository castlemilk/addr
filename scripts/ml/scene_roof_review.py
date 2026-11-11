"""Small MCP handoff for existing roof eyes: source images, lines and tier-3 notes.

The default read uses the existing review-pack renderer and line-evidence helper;
optional optical guidance previews a fresh unapplied proposal. No scene edits,
labels or new diagnostics pipeline. Eyes rows are read-only snapshots. Notes are
immutable files, outside all label stores and the object-quality index.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from . import eyes_index
from .roof_candidate_review import evidence_context
from .roof_review_loop import protected_reason
from .roof_review_pack import displayed_roof_lines, render_card
from .scene_confidence import geometry_signature
from .scene_roof_diagnostics import load_scene
from .scene_self_review import validate_id

VERSION = 'scene-roof-review-v1'
OBSERVATION_VERSION = 'roof-agent-observation-v1'
DIRECTORY = Path('data/reviews/eyes/roof-review')
CONFIDENCE = Path('data/suburbs/mooroolbark/confidence.sqlite')
SCOPE = 'Diagnostic review; agent observations are tier 3, never human approvals, measured heights or training labels.'


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _codes():
    folder = Path(__file__).parent
    return {name: _hash((folder/name).read_bytes()) for name in (
        'scene_roof_review.py', 'roof_review_pack.py', 'roof_candidate_review.py', 'roof_line_fit.py',
        'roof_optical_guidance.py', 'optical_layers.py', 'scene_roof.py', 'roof_topology.py', 'roof_form.py',
        'roof_boundary_guidance.py', 'roof_boundary.py', 'roof_surface.py', 'roof_upscale.py')}


def _identity(root, scene_id, object_id):
    """Verify bytes before/after the shared loader, including its PNG hash check."""
    folder = Path(root)/'data/scenes'/validate_id(scene_id)
    source = (folder/'latest.json').read_bytes()
    scene, image = load_scene(scene_id, root)
    if (folder/'latest.json').read_bytes() != source or scene != json.loads(source):
        raise ValueError('Source model changed while loading the review')
    if scene.get('id') != scene_id:
        raise ValueError('Source scene identity does not match its directory')
    matches = [obj for obj in scene['objects'] if obj.get('id') == object_id]
    if len(matches) != 1 or matches[0].get('kind') != 'building':
        raise ValueError('Choose one current building object in this scene')
    obj = matches[0]
    identity = {'scene_id': scene_id, 'object_id': object_id,
                'source_model_sha256': _hash(source), 'model_signature': scene['model_signature'],
                'image_sha256': scene['image_sha256'], 'geometry_signature': geometry_signature(obj)}
    return identity, scene, obj, image


def _confidence_rows(root):
    path = Path(root)/CONFIDENCE
    if not path.exists():
        return []
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        return [json.loads(payload)['properties'] for (payload,) in db.execute(
            "SELECT payload FROM roofs WHERE json_extract(payload, '$.properties.band')='low'")]


def low_confidence_roofs(root=Path('.'), *, limit=12, offset=0):
    """Read the normal candidate population with existing strict frame gates."""
    root = Path(root)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 24:
        raise ValueError('Use a limit of 1–24 roofs')
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('Use a non-negative offset')
    props = _confidence_rows(root)
    props.sort(key=lambda p: (not p['scene_id'].startswith('mooroolbark-'),
                             not 40 <= p.get('area_m2', 0) <= 700,
                             not any('folds' in r or 'vertices disagree' in r for r in p.get('reasons', [])),
                             _hash(f"{p['scene_id']}/{p['object_id']}".encode())))
    scenes, used, eligible, skipped = {}, set(), [], Counter()
    for p in props:
        sid = p['scene_id']
        if sid in used:
            continue
        try:
            if sid not in scenes:
                source = (root/'data/scenes'/validate_id(sid)/'latest.json').read_bytes()
                scene = json.loads(source)
                scenes[sid] = (scene, _hash(source), {o['id']: o for o in scene['objects']})
            scene, source_hash, objects = scenes[sid]
            obj = objects.get(p['object_id'])
            reason = protected_reason(root, scene, obj, split_policy='frame') if obj else 'Object changed'
            if scene.get('id') != sid or scene['model_signature'] != p['model_signature']:
                reason = 'Model changed'
            elif obj and geometry_signature(obj) != p.get('geometry_signature'):
                reason = 'Object geometry changed'
            elif obj and obj['id'] not in scene.get('owned_object_ids', objects):
                reason = 'Not owned by this capture'
            if reason:
                skipped[reason] += 1
                continue
            row = {k: p.get(k) for k in (
                'scene_id', 'object_id', 'capture_title', 'label', 'band', 'area_m2', 'detector_score',
                'reasons', 'observed_segments', 'image_line_mismatch_m', 'review_url', 'model_signature', 'geometry_signature')}
            if row['observed_segments'] == 0:
                row['image_line_mismatch_m'] = None
            eligible.append(row | {'source_model_sha256': source_hash, 'image_sha256': scene['image_sha256']})
            used.add(sid)
        except (OSError, ValueError, KeyError, TypeError):
            skipped['Unavailable source'] += 1
    rows = eligible[offset:offset+limit]
    return {'version': VERSION, 'scope': SCOPE, 'split_policy': 'frame',
            'selection': 'Low-evidence owned detector buildings; one roof per capture; protected or reviewed scenes and held-out/mixed frames excluded. Capture titles do not assign addresses.',
            'indexed_low_roofs': len(props), 'eligible_captures': len(eligible), 'offset': offset, 'limit': limit,
            'next_offset': offset+len(rows) if offset+len(rows) < len(eligible) else None,
            'selection_skips': dict(skipped), 'rows': rows}


def _pixels(points, scene):
    points = np.asarray(points, dtype=float)
    return np.c_[points, np.ones(len(points))] @ np.asarray(scene['meters_to_pixel'])


def _rounded(points):
    return np.asarray(points, dtype=float).round(3).tolist()


def _eyes(root, identity):
    """Retain every row, explicitly mark stale/unbound rows, compact only the handoff."""
    raw = eyes_index.fetch_object(root, identity['scene_id'], identity['object_id'])
    summaries = {}
    keys = {
        'agreement': ('ref_id', 'ref_tier', 'iou', 'hausdorff_m', 'line_precision', 'line_recall', 'image_identity'),
        'cross_capture': ('band', 'other_band', 'iou', 'hidden_by'),
        'roof_diagnostics': ('recomputed_matches_stored', 'roof_type_stored', 'roof_type_recomputed', 'line_refinement', 'form_uncertain', 'error'),
        'object_quality': ('sub_kind', 'confidence', 'class_hints', 'plausibility_flags', 'support_tier', 'observed', 'observer_note', 'tier'),
        'shadow_bands': ('band_v1', 'band_v2', 'reasons_v2', 'mismatch_v2', 'observed_segments', 'mismatch_saturated'),
        'multidate': ('mode', 'n_dates', 'dates_with_support', 'boundary_edge_consensus', 'note'),
        'decision_policy': ('tier', 'machine_label', 'confidence', 'interval', 'score_interval'),
        # Reviewer line corrections vs the model's starting estimate: agreement, a draft while ref_checked is false.
        'line_corrections': ('review_revision', 'ref_checked', 'line_precision', 'line_recall', 'count_ratio', 'n_model_lines',
                             'n_ref_edges', 'baseline_matches_snapshot'),
    }
    for table, rows in raw.items():
        for row in rows:
            p = row.get('payload') or {}
            signature = row.get('model_signature') or p.get('model_signature')
            binding = ('current_model' if signature == identity['model_signature'] else
                       'stale_model' if signature else 'object_only_unverified')
            if p.get('image_sha256') and p['image_sha256'] != identity['image_sha256']:
                binding = 'stale_image'
            summary = {'version': row.get('version'), 'binding': binding,
                       'computed_at': p.get('computed_at'), 'code_sha256': p.get('code_sha256')}
            for key in keys.get(table, ()):
                if key in p or key in row:
                    summary[key] = p.get(key, row.get(key))
            if table == 'roof_diagnostics':
                summary['edge_unsupported_fraction'] = ((p.get('edge_support') or {}).get('original') or {}).get('unsupported_fraction')
                summary['crease_contrast_share'] = (p.get('crease_contrast') or {}).get('share_ge_threshold')
                summary['topology'] = dict((p.get('topology') or {}).get('after') or {}) or None
                if summary['topology'] and summary['topology'].get('observed_segments') == 0:
                    summary['topology']['image_line_mismatch_m'] = None
            if table == 'shadow_bands' and p.get('observed_segments') == 0:
                summary['mismatch_v2'] = None
            if table == 'cross_capture':
                summary.update(other_scene_id=row.get('other_scene_id'), other_object_id=row.get('other_object_id'))
            if table in keys:
                summaries.setdefault(table, []).append(summary)
    return raw, summaries


def _read_observations(directory, identity):
    """A new renderer/code/eyes snapshot must not hide notes on the same source."""
    notes = []
    for path in sorted(directory.parent.glob('*/observations/*.json')):
        note = json.loads(path.read_bytes())
        if note.get('identity') == identity:
            notes.append(note)
    return sorted(notes, key=lambda note: (note.get('recorded_at', ''), note['observation_id']))


def _new_json(path, value):
    """Create once; identical retries preserve the original timestamp and bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.pending-', delete=False) as temporary:
        temporary.write(json.dumps(value, indent=2, allow_nan=False)+'\n')
        temporary_path = Path(temporary.name)
    try:
        os.link(temporary_path, path)
    except FileExistsError:
        return json.loads(path.read_bytes())
    finally:
        temporary_path.unlink()
    return value


def _review_directory(root, review_id):
    if not isinstance(review_id, str) or not re.fullmatch(r'[0-9a-f]{64}', review_id):
        raise ValueError('Use the review_id returned by scene_roof_review')
    return Path(root)/DIRECTORY/review_id


def _optical_candidate(obj, scene, image):
    from .scene_roof import propose_roof
    frame = np.asarray(scene['pixel_to_meters'])

    def local(points):
        return (np.c_[points, np.ones(len(points))] @ frame).tolist()

    proposal = propose_roof(local(obj['ring']), obj['model']['height_m'], 2., 'auto', image=image,
                            meters_to_pixel=scene['meters_to_pixel'], holes=[local(h) for h in obj.get('holes', [])])
    candidate = {**obj, 'roof_parts': proposal['parts'], 'roof_annotation': proposal['annotation'],
                 'roof_lines': proposal['lines'], 'roof_type': proposal['roof_type'],
                 'roof_form_evidence': proposal.get('form_evidence'), 'roof_form_uncertain': proposal['form_uncertain']}
    comparison = proposal.get('form_comparison')
    level = comparison.get('alternative') if comparison else None
    comparison_object = ({**obj, 'roof_parts': level['parts'], 'roof_annotation': level['annotation'],
                          'roof_lines': level['lines'], 'roof_type': level['roof_type']} if level else None)
    report = proposal.get('optical_guidance')
    if report:
        report = {**report, 'contradictions_scope': 'comparison_baseline',
                  'valley_candidates_scope': 'pre_refit_candidate_plans', 'selected_valleys_scope': 'selected_proposal'}
        candidate['roof_optical_guidance'] = report
    return {'state': 'available', 'report': report, 'candidate_object': candidate,
            'geometry_audit': proposal['audit'],
            'form_evidence': proposal.get('form_evidence'), 'form_uncertain': proposal['form_uncertain'],
            **({'form_comparison': {k: v for k, v in comparison.items() if k != 'alternative'},
                'family_comparison_object': comparison_object,
                'family_comparison_geometry': {'roof_type': level['roof_type'], 'coordinate_space': 'source_image_pixels',
                                               'roof_lines': [], 'geometry_passed': level['audit']['passed']}} if comparison else {}),
            'candidate_geometry': {'roof_type': candidate['roof_type'], 'coordinate_space': 'source_image_pixels',
                                   'outline': {'ring': _rounded(candidate['ring']), 'holes': [_rounded(h) for h in candidate.get('holes', [])]},
                                   'geometry_passed': proposal['audit']['passed'],
                                   'roof_lines': [{'kind': line['kind'], 'points': _rounded(_pixels(line['line_meters'], scene))}
                                                  for line in candidate['roof_lines']]},
            'candidate_matches_saved_geometry': geometry_signature(candidate) == geometry_signature(obj),
            'note': 'Fresh automatic proposal, not applied. Guidance accepted/after compares against its current unguided baseline; other code changes may also make this proposal differ from the saved model.'}


def review_roof(scene_id, object_id, root=Path('.'), *, include_feedback=False, include_optical_guidance=False,
                include_boundary_guidance=False, boundary_upscale=False):
    root = Path(root).resolve()
    if boundary_upscale and not include_boundary_guidance:
        raise ValueError('boundary_upscale requires include_boundary_guidance=true')
    identity, scene, obj, image = _identity(root, scene_id, object_id)
    _, observations, _ = evidence_context(obj, scene, image)
    lines = [{'kind': line['kind'], 'points': _rounded(_pixels(line['line_meters'], scene))}
             for line in displayed_roof_lines(obj)]
    segments = [{'points': _rounded(_pixels(line['points'], scene)), 'length_m': round(float(line['length']), 3),
                 'support': round(float(line['support']), 4), 'view_origin': line.get('view_origin', 'gray_or_clahe')}
                for line in observations]
    raw_eyes, eyes = _eyes(root, identity)
    optical = {'state': 'not_requested'}
    boundary = {'state': 'not_requested'}
    if include_boundary_guidance:
        from .roof_boundary_guidance import boundary_candidate
        from .roof_upscale import UpscaleError, upscale_roof_crop
        upscale = {'status': 'not_requested'}
        if boundary_upscale:
            try:
                upscale = upscale_roof_crop(root/'data/scenes'/scene_id/f"{scene['image_sha256']}.png",
                                             (obj.get('boundary_refinement') or {}).get('raw_ring') or obj['ring'])
            except UpscaleError as error:
                upscale = {'status': 'unavailable', 'reason': str(error),
                           'note': 'Original RGB boundary discovery remains available; no interpolation substitute.'}
        candidate, boundary = boundary_candidate(obj, scene, image, upscale=upscale)
        boundary = {'state': 'available', **boundary}
        try:
            optical = _optical_candidate(candidate, scene, image)
            boundary['roof_geometry_passed'] = bool(optical['geometry_audit']['passed'])
            if not boundary['roof_geometry_passed']:
                boundary.update(accepted=False, changed=False, reason='fresh-roof-geometry-failed')
                optical = {'state': 'unavailable', 'reason': 'Boundary comparison roof failed physical geometry checks.'}
            else:
                optical['boundary_guidance'] = boundary
                optical['candidate_matches_saved_geometry'] = geometry_signature(optical['candidate_object']) == geometry_signature(obj)
                optical['note'] = 'Fresh boundary and automatic roof comparison, unapplied. Original RGB supplies material, family and line evidence; generated detail nominates positions only.'
        except (ValueError, ArithmeticError, RuntimeError, KeyError) as error:
            boundary.update(accepted=False, changed=False, reason='fresh-roof-unavailable', roof_error=str(error))
            optical = {'state': 'unavailable', 'reason': str(error)}
    elif include_optical_guidance:
        try:
            optical = _optical_candidate(obj, scene, image)
        except (ValueError, ArithmeticError, RuntimeError, KeyError) as error:
            optical = {'state': 'unavailable', 'reason': str(error), 'note': 'Saved model and imagery are unchanged.'}
    elif obj.get('roof_optical_guidance'):
        optical = {'state': 'stored', 'report': obj['roof_optical_guidance'],
                   'note': 'Diagnostics persisted with this model; not recomputed by this review.'}
    optical_segments = [
        {**{key: value for key, value in line.items() if key != 'points_meters'},
         'points': _rounded(_pixels(line['points_meters'], scene))}
        for line in ((optical.get('report') or {}).get('observations') or {}).get('additional_segment_evidence', [])]
    feedback = {'state': 'not_requested', 'note': 'Use include_feedback=true for separately labelled saved line edits.'}
    if include_feedback:
        from .scene_line_feedback import load_roof_line_feedback
        try:
            saved = load_roof_line_feedback(scene_id, object_id)
            if saved.get('model_signature') != identity['model_signature'] or saved.get('image', {}).get('sha256') != identity['image_sha256']:
                raise ValueError('Saved feedback is bound to another model or source image')
            feedback = {'state': 'available', 'source': 'canonical_scene_roof_feedback_api', 'data': saved,
                        'note': 'Saved reviewer XY observations, separate from model lines and detected image segments; training eligibility is not implied.'}
        except ValueError as error:
            feedback = {'state': 'unavailable', 'reason': str(error)}
    geometry = {'coordinate_space': 'source_image_pixels', 'axes': ['right', 'down'], 'display_rounding_decimals': 3,
                'outline': {'ring': _rounded(obj['ring']), 'holes': [_rounded(h) for h in obj.get('holes', [])]},
                'model_roof_lines': lines, 'detected_image_segments': segments,
                **({'suppressed_legacy_flat_lines': len(obj['roof_lines'])}
                   if obj.get('roof_type') == 'flat' and obj.get('roof_lines') and not lines else {}),
                'additional_optical_segments': optical_segments,
                'note': 'Model lines are estimates. Detected segments may be shadows, texture or solar edges; neither is a human roof-line label. Exact model geometry is preserved in manifest.model_object.'}
    source_frame = {k: scene[k] for k in ('image_width', 'image_height', 'pixel_to_meters', 'meters_to_pixel', 'centroid')}
    source_frame['units'] = 'pixels for outlines/returned lines; local east/north metres for affine transforms and original model lines'
    source_frame['affine_convention'] = '[x, y, 1] @ matrix'
    content = {'version': VERSION, 'identity': identity, 'code_sha256': _codes(), 'source_frame': source_frame,
               'model_object': obj, 'geometry': geometry,
               'detected_segments_exact': [{'points_meters': np.asarray(line['points']).tolist(),
                                            'length_m': float(line['length']), 'support': float(line['support']),
                                            'view_origin': line.get('view_origin', 'gray_or_clahe')} for line in observations],
               'eyes_snapshot': raw_eyes, 'saved_feedback': feedback, 'optical_guidance': optical, 'boundary_guidance': boundary}
    review_id = _hash(_json_bytes(content))
    directory = _review_directory(root, review_id)
    directory.mkdir(parents=True, exist_ok=True)
    card = directory/'card.png'
    manifest_path = directory/'manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get('content') != content or _hash(card.read_bytes()) != manifest.get('card_sha256'):
            raise ValueError('Saved review artifacts changed; refusing to overwrite the handoff')
    else:
        transform = render_card(obj, scene, image, card, observations=observations,
                                optical_guidance=optical if optical['state'] == 'available' else None)
        manifest = _new_json(manifest_path, {'review_id': review_id, 'content': content,
                            'created_at': datetime.now(UTC).isoformat(), 'render': transform,
                            'card_sha256': _hash(card.read_bytes()), 'scope': SCOPE})
    # A long line-detection/API request must not bind a card to a changed model.
    if _identity(root, scene_id, object_id)[0] != identity:
        raise ValueError('Source changed while preparing the review; request a fresh review')
    return {'review_id': review_id, 'version': VERSION, 'identity': identity, 'scope': SCOPE,
            'capture_title': scene.get('title'), 'model_roof_type': obj.get('roof_type'),
            'source_frame': source_frame, 'render': manifest['render'], 'geometry': geometry,
            'eyes': eyes, 'eyes_status': {'rows': sum(len(rows) for rows in eyes.values()),
                                        'bindings': dict(Counter(row['binding'] for rows in eyes.values() for row in rows)),
                                        'note': 'Cached sidecars, not recomputed by this tool; model/image binding does not establish current-code diagnostics or accuracy.'},
            'saved_feedback': feedback, 'optical_guidance': {k: v for k, v in optical.items() if k not in ('candidate_object', 'family_comparison_object')},
            'boundary_guidance': boundary,
            'paths': {'card': str(card), 'manifest': str(manifest_path),
                      'source_image': str(root/'data/scenes'/scene_id/f"{scene['image_sha256']}.png")},
            'observations': _read_observations(directory, identity)}


def record_observation(review_id, *, observed, hypothesis='', ambiguity='', next_action='', reviewer='codex', root=Path('.')):
    """Record only an agent note against a verified immutable review manifest."""
    root = Path(root).resolve()
    directory = _review_directory(root, review_id)
    manifest = json.loads((directory/'manifest.json').read_bytes())
    content = manifest['content']
    if manifest.get('review_id') != review_id or _hash(_json_bytes(content)) != review_id:
        raise ValueError('Review manifest identity changed')
    if _hash((directory/'card.png').read_bytes()) != manifest['card_sha256']:
        raise ValueError('Review image changed')
    identity = content['identity']
    if _identity(root, identity['scene_id'], identity['object_id'])[0] != identity:
        raise ValueError('Source model, image or geometry changed; fetch and inspect a new review')
    fields = {'observed': observed, 'hypothesis': hypothesis, 'ambiguity': ambiguity, 'next_action': next_action, 'reviewer': reviewer}
    for key, value in fields.items():
        if not isinstance(value, str) or len(value) > (120 if key == 'reviewer' else 2000):
            raise ValueError(f'{key} must be a bounded text string')
    if not observed.strip() or not reviewer.strip():
        raise ValueError('State the visible observation and reviewer identity')
    payload = {'version': OBSERVATION_VERSION, 'review_id': review_id, 'identity': identity,
               'tier': 3, 'training_eligible': False, 'human_approval': False, 'scope': SCOPE,
               **fields, 'code_sha256': _codes()['scene_roof_review.py']}
    observation_id = _hash(_json_bytes(payload))
    path = directory/'observations'/f'{observation_id}.json'
    saved = _new_json(path, {**payload, 'observation_id': observation_id, 'recorded_at': datetime.now(UTC).isoformat()})
    return {'observation_id': observation_id, 'review_id': review_id, 'path': str(path), 'observation': saved}
