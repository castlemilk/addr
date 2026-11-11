"""Shadow evidence bands (v2 view) computed beside, never instead of, production bands.

Diagnostics, not labels or accuracy. `evidence_v2` re-reads the mesh and
annotation audits that `scene_self_review.audit_object` already emits and
changes exactly three things versus `scene_confidence.evidence`: drainage that
was not assessed (passed is None) becomes an info issue instead of an invalid
surface; the image-line mismatch is None when no image segment was observed;
and a saturated mismatch (the 1.5 m residual cap) is flagged. Info flags do not
block the highest band. Production `evidence()`/`audit_object` are untouched and
rows land only in the eyes sidecar index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from . import eyes_index
from .scene_confidence import VERSION as V1_VERSION
from .scene_confidence import evidence
from .scene_self_review import audit_scene, issue, validate_id

VERSION = 'roof-evidence-v2-shadow'
SATURATION_M = 1.5  # roof_topology caps each line residual here
INFO_MESSAGE = 'Roof runoff was not assessed (complexity limit); treated as unknown, not as a defect.'


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _drainage_state(audit):
    drainage = audit.get('drainage')
    if drainage is None or drainage.get('status') == 'not-assessed-invalid-geometry':
        return 'blocked'  # audit_roof never ran runoff because the surface itself is invalid
    return {True: 'passed', False: 'failed'}.get(drainage.get('passed'), 'not_assessed')


def drainage_status(audit):
    """passed / failed / not_assessed / not_assessed_invalid_geometry / unavailable from the mesh and annotation audits."""
    if 'mesh_audit' not in audit or 'annotation_audit' not in audit:
        return 'unavailable'
    states = {_drainage_state(audit['mesh_audit']), _drainage_state(audit['annotation_audit'])}
    if 'failed' in states:
        return 'failed'
    if 'blocked' in states:
        return 'not_assessed_invalid_geometry'
    return 'not_assessed' if 'not_assessed' in states else 'passed'


def issues_v2(audit):
    """v2 issue list and geometry verdict; only the drainage-None fold is reinterpreted.

    v1 geometry fails on any defect (invalid_surface, editor_mesh_mismatch, uncheckable_geometry, missing_surface).
    v2 passes a v1 failure only when the mesh and editable graph pass, runoff was merely not assessed, and no
    defect other than the drainage-induced invalid_surface remains.
    """
    issues = [dict(i) for i in audit.get('issues', [])]
    if audit.get('geometry_passed'):
        return issues, True
    if drainage_status(audit) != 'not_assessed' or not (audit['mesh_audit'].get('passed') and audit['annotation_audit'].get('passed')):
        return issues, False
    issues = [i for i in issues if i['code'] != 'invalid_surface'] + [issue('drainage_not_assessed', 'info', INFO_MESSAGE)]
    return issues, not any(i.get('severity') == 'defect' for i in issues)


def evidence_v2(obj, audit):
    """Same shape and rule as scene_confidence.evidence, with the three v2 reinterpretations."""
    issues, passed = issues_v2(audit)
    blocking = {i['code'] for i in issues if i.get('severity') != 'info'}
    reasons = [i['message'] for i in issues]
    score = obj.get('score')
    cost = audit.get('cost', {})
    segments = cost.get('observed_segments', 0)
    mismatch = cost.get('image_line_mismatch_m') if segments > 0 else None
    saturated = mismatch is not None and mismatch == SATURATION_M
    common = {'geometry_passed': passed, 'image_line_mismatch_m': mismatch, 'observed_segments': segments,
              'mismatch_saturated': saturated, 'drainage_status': drainage_status(audit), 'issues': issues}
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 1:
        return {'band': 'unassessed', 'reasons': ['Detector support is unavailable.'], 'detector_score': None, **common}
    if not passed or 'image_cut' in blocking or score < .65:
        band = 'low'
        if score < .65:
            reasons.append('Detector support is below 0.65; this score is not calibrated accuracy.')
    elif score >= .8 and segments >= 3 and mismatch is not None and mismatch <= .35 and not blocking:
        band = 'high'
        reasons = ['Geometry checks pass and multiple registered image lines support the model.'] + [i['message'] for i in issues]
    else:
        band = 'medium'
        if not reasons:
            reasons = ['Geometry checks pass, but image or detector support is not strong enough for the highest band.']
    return {'band': band, 'reasons': reasons, 'detector_score': round(score, 4), **common}


def load_scene(root, scene_id):
    """Read-only scene load with the same imagery identity check as production assessment."""
    folder = Path(root)/'data/scenes'/validate_id(scene_id)
    scene = json.loads((folder/'latest.json').read_text())
    if not re.fullmatch(r'[0-9a-f]{64}', str(scene.get('image_sha256', ''))):
        raise ValueError('Invalid imagery identity')
    image_path = folder/f"{scene['image_sha256']}.png"
    if hashlib.sha256(image_path.read_bytes()).hexdigest() != scene['image_sha256']:
        raise ValueError('Source imagery changed')
    with Image.open(image_path) as image:
        if image.size != (scene['image_width'], scene['image_height']):
            raise ValueError('Imagery dimensions changed')
        return scene, image.convert('RGB')


def assess_scene(scene, image, *, only_owned=True):
    """v1 and v2 bands side by side for each live building; the v1 audit is computed once."""
    audits = {r['object_id']: r for r in audit_scene(scene, image) if r['kind'] == 'building'}
    owned = set(scene['owned_object_ids']) if only_owned and scene.get('owned_object_ids') is not None else None
    scope = 'owned_object_ids' if owned is not None else 'all_live'
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    rows = []
    for obj in scene['objects']:
        audit = audits.get(obj['id'])
        if audit is None or (owned is not None and obj['id'] not in owned):
            continue
        v1, v2 = evidence(obj, audit), evidence_v2(obj, audit)
        payload = {'band_v1': v1['band'], 'band_v2': v2['band'], 'flip': v1['band'] != v2['band'],
                   'transition': f"{v1['band']}->{v2['band']}", 'reasons_v1': v1['reasons'], 'reasons_v2': v2['reasons'],
                   'issues_v2': [{'code': i['code'], 'severity': i.get('severity')} for i in v2['issues']],
                   'mismatch_v1': v1.get('image_line_mismatch_m'), 'mismatch_v2': v2['image_line_mismatch_m'],
                   'mismatch_saturated': v2['mismatch_saturated'], 'drainage_status': v2['drainage_status'],
                   'geometry_passed_v1': v1.get('geometry_passed', False), 'geometry_passed_v2': v2['geometry_passed'],
                   'observed_segments': v2['observed_segments'], 'detector_score': v2['detector_score'],
                   'population_scope': scope, 'v1_version': V1_VERSION, 'version': VERSION, 'code_sha256': code, 'computed_at': stamp}
        rows.append({'scene_id': scene['id'], 'object_id': obj['id'], 'model_signature': scene.get('model_signature'),
                     'version': VERSION, 'payload': payload})
    return rows


def replace_scene_rows(db, table, version, scene_ids, rows):
    """A run defines each scene's current population: drop its older rows (stale model_signature or wider scope)."""
    for scene_id in scene_ids:
        db.execute(f'DELETE FROM "{table}" WHERE scene_id=? AND version=?', (scene_id, version))
    return eyes_index.replace_rows(db, table, rows)  # commits the deletes and inserts together


def run(root=Path('.'), scene_ids=(), only_owned=True):
    root, scene_ids = Path(root), list(dict.fromkeys(scene_ids))
    rows = []
    for scene_id in scene_ids:
        scene, image = load_scene(root, scene_id)
        rows.extend(assess_scene(scene, image, only_owned=only_owned))
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        return replace_scene_rows(db, 'shadow_bands', VERSION, scene_ids, rows)


def flip_matrix(root=Path('.'), scene_ids=None):
    """Counts by (band_v1, band_v2) over stored shadow rows, with the denominator and the flip list."""
    found = []
    if eyes_index.path(root).exists():
        with closing(eyes_index.connection(root, readonly=True)) as db:
            if scene_ids is None:
                found = eyes_index.rows(db, 'shadow_bands', version=VERSION)
            else:
                for scene_id in dict.fromkeys(scene_ids):
                    found.extend(eyes_index.rows(db, 'shadow_bands', scene_id=scene_id, version=VERSION))
    counts, by_v1, by_v2, flips = {}, {}, {}, []
    for row in found:
        p = row['payload']
        key = f"{p['band_v1']}->{p['band_v2']}"
        counts[key] = counts.get(key, 0)+1
        by_v1[p['band_v1']] = by_v1.get(p['band_v1'], 0)+1
        by_v2[p['band_v2']] = by_v2.get(p['band_v2'], 0)+1
        if p['flip']:
            flips.append({'scene_id': row['scene_id'], 'object_id': row['object_id'], 'band_v1': p['band_v1'],
                          'band_v2': p['band_v2'], 'drainage_status': p['drainage_status']})
    return {'version': VERSION, 'v1_version': V1_VERSION, 'denominator': len(found), 'counts': dict(sorted(counts.items())),
            'by_band_v1': dict(sorted(by_v1.items())), 'by_band_v2': dict(sorted(by_v2.items())),
            'flips': sorted(flips, key=lambda f: (f['scene_id'], f['object_id'])),
            'note': 'Band transitions are diagnostics of a rule change, not accuracy.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Shadow roof evidence bands v2 (diagnostics, not labels).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--scenes', nargs='+', required=True)
    parser.add_argument('--all-objects', action='store_true', help='Include roofs outside owned_object_ids.')
    parser.add_argument('--flip-matrix', action='store_true')
    args = parser.parse_args(argv)
    root = Path(args.root)
    out = {'version': VERSION, 'written': run(root, args.scenes, only_owned=not args.all_objects)}
    if args.flip_matrix:
        out['flip_matrix'] = flip_matrix(root, args.scenes)
    json.dump(out, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
