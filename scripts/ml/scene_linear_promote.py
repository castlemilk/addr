"""Promote the linear-continuity merge into ONE window's model layer, carry its review forward, record the lineage.

The join itself is scene_extract.merge_linear_fragments (authorised 2026-10-03 15:10 and 19:40 "yes to defaults"); this
module is the promotion path for WINDOW scenes (win-* / lot-*; qa_* scratch copies for proofs). Per window, in order:

1. Read latest.json and review.json. The merge is judged on the review layer's view of the model layer (each model object
   as the review holds it, so a reviewer-decided piece is never a candidate and blocks a bridge, exactly the layer the
   marked run-v2 sheet rendered); members must be untouched candidates in both layers. The new snapshot keeps every
   non-object field byte-identical, drops the members from the object list and appends each feature as a candidate carrying
   replaces + merged_from and NO derived_from (the canopy precedent, merge_canopy_fragments: the model layer records only
   `replaces`; derived_from / replacement_ids are the review layer's lineage pair, src/lib/scene-instances.ts lineageIssue); the
   model_signature is recomputed exactly as scene_extract.save_scene does and models/<sig>.json + latest.json are written
   the same way (indent=2, temp + replace). The old models file is never deleted.
2. The review is carried with the editor's own recoverSceneReview through scripts/scene-linear-carry.mjs (Node, ts.transpileModule,
   as scripts/window-review-carry-forward.mjs): every reviewer-touched non-member object byte-identical (restored verbatim by
   default and listed in the plan - the takeover detection of every takeover window carries replaces / derived_from in its
   review copy and the recovery would demote it; --no-restore-verbatim refuses instead), members mapped onto the feature the
   way the editor maps replaced seeds and kept in the review as superseded -> feature, the review's copy of the feature given
   derived_from = replaces (the lineage pair the app's save requires), validated on a qa_ scratch copy
   through the app (GET stale false, POST /api/scenes 200 at revision + 1), then written under the review lock at revision + 1
   with updated_by 'scripts.ml.scene_linear_promote'. Byte copies of latest.json and review.json go to the run directory first.
3. One registry row (role linear_merge) is appended; 4. scene_lot_tags --write re-keys the window's lot-tags-v1 rows under
   the new signature; 5. <run-dir>/<id>/promote.json records every number and sha.

A window with nothing to merge is left untouched (reported). --dry-run writes only under the run directory. --rollback <id>
restores the byte copies and removes the registry row it wrote; the new models file and the lot-tags rows stay (documented).
Refusals are named; nothing is written on a refusal. No model is loaded (CPU geometry only).

--batch <ids...> promotes windows one at a time: the counter (r5_precision count --json --block v2) is read before and after
every window and its six fields (primary_n, primary_by_class, primary_rows, targets_remaining, takeover_rows, carried_rows;
carried_rows compared with the design_sha_match hop label ignored) must be identical, else that window is rolled back
automatically (lot tags re-keyed under the restored signature) and the batch stops; a window whose review names another
signature than latest.json, has no review or has nothing to merge is skipped (reported, not failed); <run-dir>/batch/<id>.json
per window, <run-dir>/batch/summary.json at the end. --dry-run prints a one-line plan per window and writes only under the run
directory. --quiet-since <ISO> stops the batch before a window when a review.json this program did not write (qa_ scratch, this
batch's windows and bytes a registry linear_merge row names as new_review_sha256 are its own) changed after it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .scene_extract import LINEAR_MERGE, merge_linear_fragments

REPO = Path(__file__).resolve().parents[2]
NODE_CARRY = REPO/'scripts/scene-linear-carry.mjs'
VERSION = 'linear-promote-v1'
UPDATED_BY = 'scripts.ml.scene_linear_promote'
ROLE = 'linear_merge'
REGISTRY_VERSION = 'windows-registry-v1'
REGISTRY = Path('data/reviews/eyes/recapture/windows-2026-10-01.jsonl')
DESIGN = Path('data/reviews/eyes/recapture/DESIGN.json')
RULES = Path('data/reviews/eyes/training/sam3-mac/round5/RULES.json')
RUN_DIR = Path('.run/session-2026-10-03-promote')
BATCH_RUN_DIR = Path('.run/session-2026-10-03-batch')
COUNTER_CMD = [sys.executable, '-m', 'scripts.ml.training.sam3_mac.r5_precision', 'count', '--json', '--block', 'v2']
COUNTER_FIELDS = ('primary_n', 'primary_by_class', 'primary_rows', 'targets_remaining', 'takeover_rows', 'carried_rows')
COUNTER_HOP_LABEL = 'design_sha_match'
MODEL_LAYER_LINEAGE = ('replaces', 'merged_from')      # what the model layer records about a feature's members; never derived_from
LOT_TAGS_REPORT = 'data/reviews/eyes/recapture/lot-tags-{id}-{date}.json'
DECIDED = ('confirmed', 'needs_adjustment', 'rejected', 'superseded')
GEOMETRY_KEYS = ('ring', 'holes', 'kind', 'geometry_reviewed')
# 19:40 "yes to defaults" (2): a merged feature must reach 1 m2 AND its largest member must reach 1 m2, else refuse. Checked
# here as well as wherever the module enforces it, so a promotion never depends on the module's state alone.
RESULT_FLOOR_M2 = 1.
# The extraction path's caps (scene_extract.extract_objects): a candidate over 200 ring vertices, 32 holes, 200 vertices in a
# hole or 2000 vertices in all is dropped. A merged feature over those caps is refused here, never simplified here.
RING_MAX, HOLES_MAX, TOTAL_MAX, SIMPLIFY_ABOVE = 200, 32, 2000, 150
SCENE_ID = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')
WINDOW_ID = re.compile(r'^(win-|lot-|qa_)')
HEX64 = re.compile(r'^[a-f0-9]{64}$')


class Refusal(Exception):
    def __init__(self, reason, detail=''):
        super().__init__(f'{reason}: {detail}' if detail else reason)
        self.reason, self.detail = reason, detail


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_stamp():
    return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _canon(value):
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canon(v) for v in value]
    return value


def canonical(value) -> str:
    """JSON with sorted keys and integral floats as ints: equality as JSON.stringify sees it (Python writes 409.0, JS 409)."""
    return json.dumps(_canon(value), sort_keys=True, separators=(',', ':'))


def signature(scene: dict) -> str:
    """scene_extract.save_scene's formula: sha256 of the sorted-key JSON of everything but model_signature."""
    return hashlib.sha256(json.dumps({k: v for k, v in scene.items() if k != 'model_signature'}, sort_keys=True).encode()).hexdigest()


def snapshot_bytes(scene: dict) -> bytes:
    return json.dumps(scene, indent=2).encode()


def validate_id(scene_id):
    if not isinstance(scene_id, str) or not SCENE_ID.match(scene_id) or not WINDOW_ID.match(scene_id):
        raise Refusal('not_a_window_id', repr(scene_id))
    return scene_id


# --- step 1: the merge view, the plan, the snapshot ------------------------------------------------------------------

def merge_view(latest_objects, review_objects):
    """The objects merge_linear_fragments judges: the review layer's view of the model layer.

    Every model object is taken as the review holds it (its decision, its edits), so a decided piece is never joinable and
    blocks a bridge; a review-only object (reviewer-drawn or carried) is appended as reviewer evidence that blocks and never
    joins (a review-only candidate is shown to the merge as needs_adjustment for that reason). A model object the review
    does not hold is a refusal: the carry would bring it back as a fresh candidate.
    """
    review_by_id = {o['id']: o for o in review_objects}
    missing = [o['id'] for o in latest_objects if o['id'] not in review_by_id]
    if missing:
        raise Refusal('review_missing_model_objects', ','.join(missing))
    model_ids = {o['id'] for o in latest_objects}
    view = [review_by_id[o['id']] for o in latest_objects]
    blocked = []
    for o in review_objects:
        if o['id'] in model_ids:
            continue
        if o.get('status') == 'candidate':
            blocked.append(o['id'])
            o = {**o, 'status': 'needs_adjustment'}
        view.append(o)
    return view, {'review_only': [o['id'] for o in review_objects if o['id'] not in model_ids], 'review_only_candidates_blocked': blocked}


def model_layer_feature(feature):
    """The feature as the model layer records it: replaces + merged_from, never derived_from.

    The canopy precedent (scene_extract.merge_canopy_fragments) records only `replaces` on the container; derived_from and
    replacement_ids are the review layer's lineage pair (src/lib/scene-instances.ts lineageIssue applies to reviews), so a
    model-layer derived_from would name objects no longer in the model layer.
    """
    return {k: v for k, v in feature.items() if k != 'derived_from'}


def review_layer_feature(feature):
    """The review layer's copy of a model-layer feature: derived_from = replaces, inserted after replaces (the members stay in
    the review as superseded with replacement_ids = [feature], and the app requires both ends of that pair)."""
    out = {}
    for key, value in feature.items():
        if key == 'derived_from':
            continue
        out[key] = value
        if key == 'replaces':
            out['derived_from'] = list(value)
    if 'derived_from' not in out:
        out['derived_from'] = list(feature['replaces'])
    return out


def plan_merge(latest, review, *, merge=merge_linear_fragments):
    """Run the join on the merge view and keep the features the promotion accepts; pure, nothing written."""
    objects = latest['objects']
    review_objects = review['objects'] if review else objects
    view, view_info = merge_view(objects, review_objects)
    # the app's bounds rule needs the image size (scene_extract.app_ring_issues, 2026-10-03 rings); None skips that one rule
    size = (latest.get('image_width'), latest.get('image_height'))
    merged, report = merge(view, latest['pixel_to_meters'], image_size=size if all(isinstance(v, (int, float)) and v > 0 for v in size) else None)
    input_ids = {o['id'] for o in view}
    model_by_id = {o['id']: o for o in objects}
    review_by_id = {o['id']: o for o in review_objects}
    accepted, refused = [], []
    for feature in merged:
        if feature['id'] in input_ids:
            continue
        members = list(feature.get('replaces') or [])
        reasons = []
        if not members or any(m not in model_by_id for m in members):
            reasons.append('member_not_in_model')
        else:
            if any(model_by_id[m].get('status') != 'candidate' for m in members):
                reasons.append('member_not_candidate_in_model')
            # the merge saw the review's ring: a member whose outline / kind the reviewer edited is not the model's piece any more
            # (reviewer words such as notes are not an edit: the editor's inheritance rule carries them onto the feature)
            if any(canonical({k: review_by_id[m].get(k) for k in GEOMETRY_KEYS}) != canonical({k: model_by_id[m].get(k) for k in GEOMETRY_KEYS}) for m in members):
                reasons.append('member_edited_in_review')
            largest = max(float((model_by_id[m].get('model') or {}).get('area_m2') or 0.) for m in members)
            if float(feature['model']['area_m2']) < RESULT_FLOOR_M2 or largest < RESULT_FLOOR_M2:
                reasons.append('below_result_floor')
        ring, holes = feature['ring'], feature.get('holes') or []
        if len(ring) > RING_MAX or len(holes) > HOLES_MAX or any(len(h) > RING_MAX for h in holes) or len(ring)+sum(map(len, holes)) > TOTAL_MAX:
            reasons.append('too_many_vertices')
        row = {'id': feature['id'], 'kind': feature['kind'], 'members': members, 'area_m2': feature['model']['area_m2'],
               'largest_member_m2': max((float((model_by_id[m].get('model') or {}).get('area_m2') or 0.) for m in members if m in model_by_id), default=None),
               'vertices': len(ring), 'holes': len(holes), 'vertices_over_simplify_threshold': len(ring) > SIMPLIFY_ABOVE,
               'refused': reasons}
        detail = next((r for r in report['features'] if r['id'] == feature['id']), {})
        row.update({'pieces_union_m2': detail.get('pieces_union_m2'), 'bridge_m2': detail.get('bridge_m2')})
        if reasons:
            refused.append(row)
        else:
            accepted.append((feature, row))
    features = [model_layer_feature(f) for f, _ in accepted]
    members = sorted(m for f in features for m in f['replaces'])
    if len(set(members)) != len(members):
        raise Refusal('member_in_two_features', ','.join(sorted({m for m in members if members.count(m) > 1})))
    return {'features': features, 'feature_rows': [r for _, r in accepted], 'members': members, 'refused_features': refused,
            'kinds': report['kinds'], 'refusals_by_reason': _refusal_counts(report), 'design': report['design'], 'view_info': view_info,
            'nothing_to_merge': not features}


def _refusal_counts(report):
    counts = {}
    for record in report.get('refusals', []):
        counts[record['reason']] = counts.get(record['reason'], 0)+1
    return counts


def build_snapshot(latest, features, member_ids):
    """The promoted snapshot: every non-object field as loaded, members dropped, features appended, signature recomputed."""
    members = set(member_ids)
    scene = {k: v for k, v in latest.items() if k != 'model_signature'}
    scene['objects'] = [o for o in latest['objects'] if o['id'] not in members] + sorted(features, key=lambda f: (f['kind'], f['id']))
    scene['model_signature'] = signature(scene)
    for key in latest:
        if key not in ('objects', 'model_signature') and json.dumps(latest[key], sort_keys=True) != json.dumps(scene[key], sort_keys=True):
            raise Refusal('snapshot_field_changed', key)
    if scene['model_signature'] == latest.get('model_signature'):
        raise Refusal('signature_unchanged', 'the promoted snapshot has the same signature as the current one')
    return scene


# --- step 2: the carry --------------------------------------------------------------------------------------------------

def carry_review(previous_path, current_path, saved_path, out_path, *, restore_verbatim=True, updated_by=UPDATED_BY, node='node'):
    cmd = [node, str(NODE_CARRY), '--previous', str(previous_path), '--current', str(current_path), '--saved', str(saved_path),
           '--out', str(out_path), '--updated-by', updated_by, *(['--restore-verbatim'] if restore_verbatim else [])]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(REPO))
    lines = [line for line in proc.stdout.strip().splitlines() if line.startswith('{')]
    try:
        stats = json.loads(lines[-1])
    except (IndexError, ValueError):
        raise Refusal('carry_failed', (proc.stderr or proc.stdout)[-2000:]) from None
    if proc.returncode != 0 or not stats.get('ok'):
        raise Refusal(stats.get('reason', 'carry_failed'), '; '.join(stats.get('problems') or []) or proc.stderr[-1000:])
    return stats


def check_carry(saved, merged, members, features, new_scene):
    """Python's own reading of the carried review (JS-canonical equality); every finding is a refusal."""
    member_set = set(members)
    merged_by_id = {o['id']: o for o in merged['objects']}
    problems = []
    feature_of = {m: f['id'] for f in features for m in f['replaces']}
    for o in saved['objects']:
        if o['id'] in member_set:
            out = merged_by_id.get(o['id'])
            expected = {**o, 'status': 'superseded', 'geometry_reviewed': False, 'replacement_ids': [feature_of[o['id']]]}
            if out is None or canonical(out) != canonical(expected):
                problems.append(f"member {o['id']} is not kept as superseded -> {feature_of[o['id']]}")
            if o.get('status') in DECIDED or o.get('geometry_reviewed'):
                problems.append(f"decided member {o['id']} merged")
            continue
        out = merged_by_id.get(o['id'])
        if out is None or canonical(out) != canonical(o):
            problems.append(f"non-member {o['id']} ({o.get('status')}) changed or missing")
    feature_by_id = {f['id']: f for f in features}
    for fid, feature in feature_by_id.items():
        if 'derived_from' in feature:
            problems.append(f'feature {fid} carries derived_from in the model layer (the model layer records {"+".join(MODEL_LAYER_LINEAGE)} only)')
        out = merged_by_id.get(fid)
        if out is None:
            problems.append(f'feature {fid} missing from the review')
            continue
        if list(out.get('derived_from') or []) != list(feature['replaces']):
            problems.append(f'feature {fid} derived_from in the review is not its replaces')
        if out.get('status') == 'candidate' and canonical(out) != canonical(review_layer_feature(feature)):
            problems.append(f'feature {fid} differs from the model layer beyond derived_from')
        elif out.get('status') not in ('candidate', 'needs_adjustment', 'rejected'):
            problems.append(f"feature {fid} reads {out.get('status')}")
    if merged.get('model_signature') != new_scene['model_signature']:
        problems.append('model_signature')
    if merged.get('revision') != saved.get('revision', 0)+1:
        problems.append(f"revision {merged.get('revision')} != {saved.get('revision', 0)}+1")
    if merged.get('updated_by') != UPDATED_BY:
        problems.append('updated_by')
    saved_groups = {g['id']: g for g in saved.get('groups') or []}
    merged_groups = {g['id']: g for g in merged.get('groups') or []}
    for gid, g in saved_groups.items():
        if gid not in merged_groups or canonical(merged_groups[gid]) != canonical(g):
            problems.append(f'group {gid}')
    if len(merged_by_id) != len(merged['objects']):
        problems.append('duplicate ids')
    if len(merged['objects']) != len(saved['objects'])+len(features):
        problems.append(f"objects {len(merged['objects'])} != saved {len(saved['objects'])} + features {len(features)}")
    if problems:
        raise Refusal('carry_check_failed', '; '.join(problems[:12]))
    return {'decided_in_review': sum(1 for o in merged['objects'] if o.get('status') in DECIDED),
            'statuses_after': _status_counts(merged['objects']), 'statuses_before': _status_counts(saved['objects'])}


def _status_counts(objects):
    counts = {}
    for o in objects:
        counts[o.get('status')] = counts.get(o.get('status'), 0)+1
    return dict(sorted(counts.items()))


def _http(method, url, body=None, token=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={'Content-Type': 'application/json', **({'Authorization': f'Bearer {token}'} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b'{}')
        except ValueError:
            return error.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise Refusal('app_unreachable', f'{url}: {error}') from None


def validate_through_app(root, window_id, latest_bytes, model_files, review_bytes, *, api, token=None):
    """The carried review on a qa_ scratch copy through the app: GET reads stale false, POST saves it at revision + 1."""
    scratch_id = f"qa_lm_{re.sub(r'^(win-|lot-|qa_)', '', window_id)[:8]}_{secrets.token_hex(3)}"
    folder = Path(root)/'data/scenes'/scratch_id
    out = json.loads(review_bytes)
    result = {'scratch_id': scratch_id}
    try:
        (folder/'models').mkdir(parents=True)
        (folder/'latest.json').write_bytes(latest_bytes)
        for name, data in model_files.items():
            (folder/'models'/name).write_bytes(data)
        (folder/'review.json').write_bytes(review_bytes)
        status, got = _http('GET', f'{api}/api/scenes?id={scratch_id}', token=token)
        result['get'] = {'status': status, 'stale': got.get('stale'), 'review_revision': (got.get('review') or {}).get('revision'),
                         'model_signature': (got.get('model') or {}).get('model_signature'), 'error': got.get('error')}
        if status != 200 or got.get('stale') is not False or (got.get('review') or {}).get('revision') != out['revision']:
            raise Refusal('scratch_get_unexpected', json.dumps(result['get']))
        status, body = _http('POST', f'{api}/api/scenes', {'id': scratch_id, 'review': out}, token=token)
        saved = body.get('review') or {}
        result['post'] = {'status': status, 'error': body.get('error'), 'saved_revision': saved.get('revision'), 'saved_objects': len(saved.get('objects') or []),
                          'saved_model_signature': saved.get('model_signature')}
        if status != 200:
            raise Refusal('scratch_save_failed', f"POST /api/scenes on {scratch_id} returned {status} {body.get('error') or ''}")
        if saved.get('revision') != out['revision']+1 or saved.get('model_signature') != out['model_signature'] or len(saved.get('objects') or []) != len(out['objects']):
            raise Refusal('scratch_save_unexpected', json.dumps(result['post']))
        after = {o['id']: o for o in saved['objects']}
        result['keys_changed_by_app_save'] = sorted({k for o in out['objects'] for k in set(o) | set(after.get(o['id']) or {})
                                                     if canonical(o.get(k)) != canonical((after.get(o['id']) or {}).get(k))})
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            raise Refusal('scratch_cleanup', f'{scratch_id} still exists')
    return result


def write_review(folder, review_bytes, expected_previous_sha, *, temp_name='review.linear-promote.tmp'):
    """review.json under the app's review.lock (mkdir), temp + replace; refuses if the file changed since it was read."""
    folder = Path(folder)
    lock = folder/'review.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise Refusal('review_locked', f'{folder.name} is saving in the app (review.lock present); nothing written') from None
    temporary = folder/temp_name
    try:
        current = (folder/'review.json').read_bytes() if (folder/'review.json').exists() else b''
        if sha(current) != expected_previous_sha:
            raise Refusal('changed_underfoot', f'{folder.name} review.json changed since it was read; nothing written')
        temporary.write_bytes(review_bytes)
        temporary.replace(folder/'review.json')
        return (folder/'review.json').read_bytes()
    finally:
        temporary.unlink(missing_ok=True)
        lock.rmdir()


def write_snapshot(folder, scene_bytes, sig):
    folder = Path(folder)
    target = folder/'models'/f'{sig}.json'
    if target.exists() and target.read_bytes() != scene_bytes:
        raise Refusal('snapshot_collision', f'models/{sig[:12]}.json exists with other bytes')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(scene_bytes)
    temporary = folder/'latest.tmp'
    temporary.write_bytes(scene_bytes)
    temporary.replace(folder/'latest.json')
    return (folder/'latest.json').read_bytes()


def _file_sha(path):
    path = Path(path)
    return sha(path.read_bytes()) if path.exists() else None


def run_lot_tags(root, window_id, report_path):
    cmd = [sys.executable, '-m', 'scripts.ml.scene_lot_tags', '--root', str(root), '--scenes', window_id, '--offline', '--write',
           '--report', str(report_path), '--quiet']
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(REPO))
    summary = None
    try:
        summary = json.loads(proc.stdout)
    except ValueError:
        pass
    return {'command': ' '.join(cmd[1:]), 'returncode': proc.returncode, 'written': (summary or {}).get('written'),
            'scene': ((summary or {}).get('scenes') or {}).get(window_id), 'stderr_tail': proc.stderr[-600:] if proc.returncode else ''}


# --- the promotion of one window ----------------------------------------------------------------------------------------

def promote(window_id, *, root=Path('.'), run_dir=RUN_DIR, api='http://localhost:3008', token=None, registry=REGISTRY,
            lot_tags_report=LOT_TAGS_REPORT, lot_tags=True, scratch=True, restore_verbatim=True, dry_run=False,
            merge=merge_linear_fragments, log=lambda text: print(text, file=sys.stderr, flush=True)):
    root, run_dir = Path(root), Path(run_dir)
    validate_id(window_id)
    folder = root/'data/scenes'/window_id
    out_dir = run_dir/window_id
    record = {'version': VERSION, 'window_scene_id': window_id, 'started_at': now_stamp(), 'dry_run': dry_run, 'outcome': None,
              'design_sha256': _file_sha(root/DESIGN), 'rules_sha256': _file_sha(root/RULES), 'scene_extract_sha256': _file_sha(REPO/'scripts/ml/scene_extract.py'),
              'carry_script_sha256': _file_sha(NODE_CARRY), 'linear_design': {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, tuple) else v) for k, v in LINEAR_MERGE.items()},
              'result_floor_m2': RESULT_FLOOR_M2, 'restore_verbatim': restore_verbatim, 'model_layer_lineage': list(MODEL_LAYER_LINEAGE)}
    if not (folder/'latest.json').exists():
        raise Refusal('no_latest', f'{window_id} has no latest.json')
    if (folder/'review.lock').exists():
        raise Refusal('review_locked', f'{window_id} is saving in the app (review.lock present); nothing written')
    latest_bytes = (folder/'latest.json').read_bytes()
    latest = json.loads(latest_bytes)
    if latest.get('id') != window_id:
        raise Refusal('id_mismatch', f"latest.json id is {latest.get('id')!r}")
    if not HEX64.match(latest.get('model_signature') or ''):
        raise Refusal('invalid_signature', window_id)
    if snapshot_bytes(latest) != latest_bytes:
        raise Refusal('latest_not_round_trip', 'latest.json does not round-trip byte-identically through json.dumps(indent=2); refusing to rewrite it')
    previous_sig = latest['model_signature']
    previous_path = folder/'models'/f'{previous_sig}.json'
    if not previous_path.exists():
        raise Refusal('previous_snapshot_missing', f'models/{previous_sig[:12]}.json')
    review_bytes = (folder/'review.json').read_bytes() if (folder/'review.json').exists() else None
    review = json.loads(review_bytes) if review_bytes else None
    if review is not None and review.get('model_signature') != previous_sig:
        raise Refusal('review_stale', f"review names {str(review.get('model_signature'))[:12]}, latest is {previous_sig[:12]}; carry it forward first")
    record.update({'previous_model_signature': previous_sig, 'previous_latest_sha256': sha(latest_bytes), 'previous_review_sha256': sha(review_bytes) if review_bytes else None,
                   'review_revision_before': review.get('revision') if review else None, 'objects_before': len(latest['objects']),
                   'review_objects_before': len(review['objects']) if review else None})

    plan = plan_merge(latest, review, merge=merge)
    record.update({'merge': {k: plan[k] for k in ('feature_rows', 'members', 'refused_features', 'kinds', 'refusals_by_reason', 'view_info')},
                   'features': plan['feature_rows'], 'replaced_member_ids': plan['members']})
    out_dir.mkdir(parents=True, exist_ok=True)
    if plan['nothing_to_merge']:
        record.update({'outcome': 'nothing_to_merge', 'finished_at': now_stamp(),
                       'note': 'no feature accepted on the review layer\'s view of the model; latest.json, models/, review.json, the registry and the lot tags untouched'})
        (out_dir/('promote.dry.json' if dry_run else 'promote.json')).write_text(json.dumps(record, indent=1)+'\n')
        log(f"NOTHING TO MERGE {window_id}: refusals {plan['refusals_by_reason']}, refused features {len(plan['refused_features'])}; nothing written")
        return record

    scene = build_snapshot(latest, plan['features'], plan['members'])
    new_bytes = snapshot_bytes(scene)
    new_sig = scene['model_signature']
    record.update({'new_model_signature': new_sig, 'new_latest_sha256': sha(new_bytes), 'objects_after': len(scene['objects']),
                   'previous_models_file_sha256': _file_sha(previous_path)})
    # byte copies first (the rollback reads these)
    (out_dir/'latest.before.json').write_bytes(latest_bytes)
    if review_bytes is not None:
        (out_dir/'review.before.json').write_bytes(review_bytes)
    planned_latest = out_dir/('latest.dry.json' if dry_run else 'latest.after.json')
    planned_latest.write_bytes(new_bytes)

    carry = None
    merged_bytes = None
    if review is not None:
        planned_review = out_dir/('review.dry.json' if dry_run else 'review.after.json')
        carry = carry_review(previous_path, planned_latest, folder/'review.json', planned_review, restore_verbatim=restore_verbatim)
        merged_bytes = planned_review.read_bytes()
        merged = json.loads(merged_bytes)
        carry['python_check'] = check_carry(review, merged, plan['members'], plan['features'], scene)
        record.update({'carry': carry, 'new_review_sha256': sha(merged_bytes), 'review_revision_after': merged['revision'],
                       'review_objects_after': len(merged['objects']), 'restored_verbatim': list(carry.get('restored_verbatim') or [])})
    if dry_run:
        record.update({'outcome': 'dry_run', 'finished_at': now_stamp(), 'note': f'nothing written under data/; planned files under {out_dir}'})
        (out_dir/'promote.dry.json').write_text(json.dumps(record, indent=1)+'\n')
        log(plan_line(record))
        return record

    if scratch and merged_bytes is not None:
        record['scratch'] = validate_through_app(root, window_id, new_bytes, {previous_path.name: previous_path.read_bytes(), f'{new_sig}.json': new_bytes},
                                                 merged_bytes, api=api, token=token)
    # --- writes --------------------------------------------------------------------------------------------------------
    if sha((folder/'latest.json').read_bytes()) != record['previous_latest_sha256']:
        raise Refusal('changed_underfoot', 'latest.json changed since it was read; nothing written')
    written_latest = write_snapshot(folder, new_bytes, new_sig)
    record['written'] = {'models_file': f'models/{new_sig}.json', 'latest_json_sha256': sha(written_latest)}
    if merged_bytes is not None:
        try:
            written_review = write_review(folder, merged_bytes, record['previous_review_sha256'])
        except Refusal:
            # leave no half-promoted window: latest.json back to its bytes (models/<new>.json stays, harmless and documented)
            temporary = folder/'latest.tmp'
            temporary.write_bytes(latest_bytes)
            temporary.replace(folder/'latest.json')
            record['written']['latest_restored_after_review_refusal'] = True
            raise
        record['written']['review_json_sha256'] = sha(written_review)
    stamp = now_stamp()
    row = {'version': REGISTRY_VERSION, 'role': ROLE, 'window_scene_id': window_id, 'previous_model_signature': previous_sig, 'new_model_signature': new_sig,
           'previous_latest_sha256': record['previous_latest_sha256'], 'new_latest_sha256': record['new_latest_sha256'],
           'previous_review_sha256': record['previous_review_sha256'], 'new_review_sha256': record.get('new_review_sha256'),
           'review_revision_before': record['review_revision_before'], 'review_revision_after': record.get('review_revision_after'),
           'features': [{k: r[k] for k in ('id', 'kind', 'members', 'area_m2', 'pieces_union_m2', 'bridge_m2', 'vertices')} for r in plan['feature_rows']],
           'replaced_member_ids': plan['members'], 'members_inherited': (carry or {}).get('members_inherited') or {},
           'restored_verbatim': record.get('restored_verbatim') or [], 'model_layer_lineage': list(MODEL_LAYER_LINEAGE),
           'design_sha256': record['design_sha256'], 'rules_sha256': record['rules_sha256'], 'scene_extract_sha256': record['scene_extract_sha256'],
           'linear_design': {k: record['linear_design'][k] for k in ('d_m', 'bridge_max_ratio', 'union_ratio_max', 'blocker_kinds', 'source')},
           'result_floor_m2': RESULT_FLOOR_M2, 'promote_record': str((out_dir/'promote.json').as_posix()), 'written_at': stamp}
    line = json.dumps(row, sort_keys=True)+'\n'
    registry = Path(registry)
    registry.parent.mkdir(parents=True, exist_ok=True)
    with registry.open('a') as handle:
        handle.write(line)
    record['registry'] = {'path': str(registry), 'row': row, 'line_sha256': sha(line.encode())}
    if lot_tags:
        report = Path(lot_tags_report.format(id=window_id, date=datetime.now().strftime('%Y-%m-%d')))
        record['lot_tags'] = run_lot_tags(root, window_id, report)
        record['lot_tags']['report'] = str(report)
    record.update({'outcome': 'promoted', 'finished_at': stamp})
    (out_dir/'promote.json').write_text(json.dumps(record, indent=1)+'\n')
    log(plan_line(record)+f"; registry row {ROLE}; lot tags written {(record.get('lot_tags') or {}).get('written')}")
    return record


def plan_line(record):
    """One line per window: outcome, each feature = its members (m2, bridge, vertices), signatures, revisions, restored ids."""
    wid, outcome = record.get('window_scene_id'), record.get('outcome')
    if outcome == 'refused':
        return f"REFUSED {wid}: {record.get('reason')}: {record.get('detail')}"
    if outcome in ('skipped', 'blocked', 'failed'):
        return f"{outcome.upper()} {wid}: {record.get('reason')}" + (f": {record['detail']}" if record.get('detail') else '')
    if outcome == 'nothing_to_merge':
        merge = record.get('merge') or {}
        return f"NOTHING TO MERGE {wid}: refusals {merge.get('refusals_by_reason')}, refused features {len(merge.get('refused_features') or [])}; nothing written"
    rows = record.get('features') or []
    features = '; '.join(f"{r['kind']} {r['id']} = {'+'.join(r['members'])} {r['area_m2']} m2 (bridge {r.get('bridge_m2')} m2, {r['vertices']} v)" for r in rows)
    label = {'dry_run': 'PLAN', 'promoted': 'PROMOTED', 'rolled_back': 'ROLLED BACK'}.get(outcome, str(outcome).upper())
    restored = record.get('restored_verbatim') or []
    return (f"{label} {wid}: {len(rows)} feature(s): {features}; model {str(record.get('previous_model_signature'))[:12]} -> {str(record.get('new_model_signature'))[:12]}; "
            f"review rev {record.get('review_revision_before')} -> {record.get('review_revision_after')}; restored verbatim {len(restored)} {restored}; "
            f"model-layer lineage {'+'.join(MODEL_LAYER_LINEAGE)}")


def rollback(window_id, *, root=Path('.'), run_dir=RUN_DIR, registry=REGISTRY, log=lambda text: print(text, file=sys.stderr, flush=True)):
    """Restore latest.json and review.json from the byte copies and remove the registry row this promotion wrote.

    models/<new signature>.json stays (a content-addressed snapshot is never deleted) and the lot-tags-v1 rows stay under the
    new signature (scene_lot_tags replaced the window's rows; re-run it to key rows under the restored signature). Both are
    documented in rollback.json. Refuses when the window changed since the promotion.
    """
    root, run_dir = Path(root), Path(run_dir)
    validate_id(window_id)
    out_dir = run_dir/window_id
    record_path = out_dir/'promote.json'
    if not record_path.exists():
        raise Refusal('no_promotion_record', str(record_path))
    record = json.loads(record_path.read_text())
    if record.get('outcome') != 'promoted':
        raise Refusal('not_promoted', f"outcome {record.get('outcome')!r}")
    if record.get('rolled_back_at'):
        raise Refusal('already_rolled_back', record['rolled_back_at'])
    folder = root/'data/scenes'/window_id
    if (folder/'review.lock').exists():
        raise Refusal('review_locked', window_id)
    if _file_sha(folder/'latest.json') != record['new_latest_sha256']:
        raise Refusal('changed_since_promotion', 'latest.json is not the promoted bytes')
    if record.get('new_review_sha256') and _file_sha(folder/'review.json') != record['new_review_sha256']:
        raise Refusal('changed_since_promotion', 'review.json is not the promoted bytes (the reviewer saved?)')
    before_latest = (out_dir/'latest.before.json').read_bytes()
    if sha(before_latest) != record['previous_latest_sha256']:
        raise Refusal('byte_copy_mismatch', 'latest.before.json')
    before_review = (out_dir/'review.before.json').read_bytes() if record.get('previous_review_sha256') else None
    if before_review is not None and sha(before_review) != record['previous_review_sha256']:
        raise Refusal('byte_copy_mismatch', 'review.before.json')
    registry = Path(record['registry']['path']) if record.get('registry') else Path(registry)
    lines = registry.read_text().splitlines(keepends=True) if registry.exists() else []
    matches = [i for i, line in enumerate(lines) if sha(line.encode()) == record['registry']['line_sha256']]
    if len(matches) != 1:
        raise Refusal('registry_row_not_found_once', f'{len(matches)} matching line(s) in {registry}')
    keep = out_dir/'rollback'
    keep.mkdir(exist_ok=True)
    (keep/'latest.rolled-back.json').write_bytes((folder/'latest.json').read_bytes())
    if before_review is not None:
        (keep/'review.rolled-back.json').write_bytes((folder/'review.json').read_bytes())
    (keep/'registry.before.jsonl').write_bytes(registry.read_bytes())
    temporary = folder/'latest.tmp'
    temporary.write_bytes(before_latest)
    temporary.replace(folder/'latest.json')
    if before_review is not None:
        write_review(folder, before_review, record['new_review_sha256'], temp_name='review.linear-rollback.tmp')
    del lines[matches[0]]
    registry_tmp = registry.with_suffix('.jsonl.tmp')
    registry_tmp.write_text(''.join(lines))
    registry_tmp.replace(registry)
    stamp = now_stamp()
    result = {'version': VERSION, 'window_scene_id': window_id, 'rolled_back_at': stamp,
              'restored': {'latest_json_sha256': _file_sha(folder/'latest.json'), 'review_json_sha256': _file_sha(folder/'review.json')},
              'registry_row_removed': record['registry']['line_sha256'], 'registry_path': str(registry),
              'stays': {'models_file': record['written']['models_file'], 'note': 'models/<new signature>.json stays (content-addressed, never deleted); '
                        'the lot-tags-v1 rows stay keyed by the new signature (scene_lot_tags replaced the window\'s rows: re-run '
                        '`scene_lot_tags --write` for rows under the restored signature; until then the lots route reads them stale)'}}
    if result['restored']['latest_json_sha256'] != record['previous_latest_sha256'] or (before_review is not None and result['restored']['review_json_sha256'] != record['previous_review_sha256']):
        raise Refusal('rollback_verify_failed', json.dumps(result['restored']))
    record['rolled_back_at'] = stamp
    record_path.write_text(json.dumps(record, indent=1)+'\n')
    (out_dir/'rollback.json').write_text(json.dumps(result, indent=1)+'\n')
    log(f"ROLLED BACK {window_id}: latest.json and review.json restored, registry row removed; {result['stays']['models_file']} and the lot-tags rows stay")
    return result


# --- the batch: one window at a time, the counter identical after each one ---------------------------------------------

def run_counter():
    """r5_precision count --json --block v2 as a subprocess (reads only); the parsed JSON."""
    proc = subprocess.run(COUNTER_CMD, capture_output=True, text=True, timeout=900, cwd=str(REPO))
    if proc.returncode != 0:
        raise Refusal('counter_failed', proc.stderr[-1000:])
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise Refusal('counter_unreadable', proc.stdout[-500:]) from None


def _without_hop(value):
    if isinstance(value, dict):
        return {k: _without_hop(v) for k, v in value.items() if k != COUNTER_HOP_LABEL}
    if isinstance(value, list):
        return [_without_hop(v) for v in value]
    return value


def counter_digest(reading):
    """The six fields as sha256 of their sorted-key JSON (every row without the design_sha_match hop label) plus the count / value."""
    out = {}
    for field in COUNTER_FIELDS:
        value = _without_hop(reading.get(field))
        out[field] = {'sha256': sha(json.dumps(value, sort_keys=True).encode()), **({'rows': len(value)} if isinstance(value, list) else {'value': value})}
    return out


def compare_counters(before, after):
    a, b = counter_digest(before), counter_digest(after)
    differing = [f for f in COUNTER_FIELDS if a[f]['sha256'] != b[f]['sha256']]
    return {'identical': not differing, 'differing_fields': differing, 'before': a, 'after': b}


def program_written(registry):
    """{window: {sha256 of every review.json this program wrote}}, from the registry's linear_merge rows."""
    out = {}
    path = Path(registry)
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('role') == ROLE and row.get('new_review_sha256') and row.get('window_scene_id'):
                out.setdefault(row['window_scene_id'], set()).add(row['new_review_sha256'])
    return out


def quiet_intruders(root, since, own=(), written=None):
    """review.json files modified after `since` (an aware datetime) that this program did not write: not qa_, not a window of
    this batch, not bytes a registry linear_merge row names as new_review_sha256 (`written`, see program_written)."""
    found = []
    for path in sorted((Path(root)/'data/scenes').glob('*/review.json')):
        wid = path.parent.name
        if wid.startswith('qa_') or wid in own:
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if mtime <= since or sha(path.read_bytes()) in (written or {}).get(wid, ()):
            continue
        found.append({'window_scene_id': wid, 'mtime': mtime.strftime('%Y-%m-%dT%H:%M:%SZ')})
    return found


def preflight(window_id, *, root, merge=merge_linear_fragments):
    """(reason, detail) when the batch skips a window - no latest, no review, a review naming another signature, nothing to merge -
    else None; reads only. Any other Refusal of the plan propagates (a failure, not a skip)."""
    folder = Path(root)/'data/scenes'/window_id
    if not (folder/'latest.json').exists():
        return 'no_latest', f'{window_id} has no latest.json'
    if not (folder/'review.json').exists():
        return 'no_review', f'{window_id} has no review.json; the batch does not start reviews'
    latest = json.loads((folder/'latest.json').read_text())
    review = json.loads((folder/'review.json').read_text())
    if review.get('model_signature') != latest.get('model_signature'):
        return 'review_stale', f"review names {str(review.get('model_signature'))[:12]}, latest is {str(latest.get('model_signature'))[:12]}"
    plan = plan_merge(latest, review, merge=merge)
    if plan['nothing_to_merge']:
        return 'nothing_to_merge', f"refusals {plan['refusals_by_reason']}, refused features {len(plan['refused_features'])}"
    return None


def batch(ids, *, root=Path('.'), run_dir=BATCH_RUN_DIR, batch_dir=None, counter=run_counter, quiet_since=None, continue_on_error=False,
          registry=REGISTRY, lot_tags=True, lot_tags_report=LOT_TAGS_REPORT, merge=merge_linear_fragments,
          log=lambda text: print(text, file=sys.stderr, flush=True), **promote_kwargs):
    """Promote windows one at a time; after each the counter's six fields must be identical to the reading taken just before it,
    else that window is rolled back and the batch stops. Skips are reported; a Refusal stops the batch unless continue_on_error."""
    root, run_dir = Path(root), Path(run_dir)
    batch_dir = Path(batch_dir) if batch_dir else run_dir/'batch'
    batch_dir.mkdir(parents=True, exist_ok=True)
    ids = [validate_id(i) for i in ids]
    summary = {'version': VERSION, 'started_at': now_stamp(), 'ids': ids, 'counter_fields': list(COUNTER_FIELDS), 'hop_label_ignored': COUNTER_HOP_LABEL,
               'quiet_since': quiet_since.strftime('%Y-%m-%dT%H:%M:%SZ') if quiet_since else None, 'promoted': [], 'skipped': [], 'failed': [], 'rolled_back': [],
               'stopped_at': None, 'stop_reason': None, 'windows': [], 'batch_dir': str(batch_dir), 'run_dir': str(run_dir)}
    own = set()
    written = program_written(registry) if quiet_since is not None else {}
    silent = lambda _text: None      # noqa: E731  one line per window comes from record_window, not from promote / rollback

    def record_window(entry):
        entry['finished_at'] = now_stamp()
        (batch_dir/f"{entry['window_scene_id']}.json").write_text(json.dumps(entry, indent=1)+'\n')
        summary['windows'].append({k: entry.get(k) for k in ('window_scene_id', 'outcome', 'reason', 'detail', 'counter_identical', 'differing_fields',
                                                              'new_model_signature', 'review_revision_after', 'restored_verbatim', 'features')})
        log(plan_line(entry) if entry['outcome'] not in ('promoted', 'rolled_back') else
            f"{plan_line(entry)}; counter {'identical' if entry.get('counter_identical') else 'DIFFERS ' + ','.join(entry.get('differing_fields') or [])}")

    def finish(stopped_at=None, reason=None):
        summary.update({'stopped_at': stopped_at, 'stop_reason': reason, 'finished_at': now_stamp()})
        (batch_dir/'summary.json').write_text(json.dumps(summary, indent=1)+'\n')
        log(f"BATCH {'stopped at '+stopped_at+' ('+str(reason)+')' if stopped_at else 'complete'}: promoted {len(summary['promoted'])}, skipped {len(summary['skipped'])}, "
            f"failed {len(summary['failed'])}, rolled back {len(summary['rolled_back'])} of {len(ids)}; {batch_dir/'summary.json'}")
        return summary

    def reading(window_id, name):
        data = counter()
        path = batch_dir/f'{window_id}.counter.{name}.json'
        path.write_text(json.dumps(data, indent=1)+'\n')
        return data, str(path)

    for window_id in ids:
        entry = {'version': VERSION, 'window_scene_id': window_id, 'started_at': now_stamp(), 'outcome': None}
        if quiet_since is not None:
            intruders = quiet_intruders(root, quiet_since, own, written)
            if intruders:
                entry.update({'outcome': 'blocked', 'reason': 'quiet_window_broken', 'detail': ', '.join(f"{i['window_scene_id']} {i['mtime']}" for i in intruders), 'intruders': intruders})
                record_window(entry)
                return finish(window_id, 'quiet_window_broken')
        try:
            skip = preflight(window_id, root=root, merge=merge)
        except Refusal as refusal:
            skip = None
            entry.update({'outcome': 'failed', 'reason': refusal.reason, 'detail': refusal.detail, 'stage': 'preflight'})
        if skip:
            entry.update({'outcome': 'skipped', 'reason': skip[0], 'detail': skip[1]})
            summary['skipped'].append(window_id)
            record_window(entry)
            continue
        if entry['outcome'] != 'failed':
            before, entry['counter_before'] = reading(window_id, 'before')
            try:
                record = promote(window_id, root=root, run_dir=run_dir, registry=registry, lot_tags=lot_tags, lot_tags_report=lot_tags_report, merge=merge, log=silent, **promote_kwargs)
            except Refusal as refusal:
                entry.update({'outcome': 'failed', 'reason': refusal.reason, 'detail': refusal.detail, 'stage': 'promote'})
        if entry['outcome'] == 'failed':
            summary['failed'].append(window_id)
            record_window(entry)
            if continue_on_error:
                continue
            return finish(window_id, f"refused: {entry['reason']}")
        entry.update({k: record.get(k) for k in ('previous_model_signature', 'new_model_signature', 'review_revision_before', 'review_revision_after',
                                                 'features', 'replaced_member_ids', 'restored_verbatim')})
        entry['promote_record'] = str(run_dir/window_id/'promote.json')
        if record['outcome'] != 'promoted':      # the plan changed between preflight and promote: nothing written
            entry.update({'outcome': 'skipped', 'reason': record['outcome']})
            summary['skipped'].append(window_id)
            record_window(entry)
            continue
        after, entry['counter_after'] = reading(window_id, 'after')
        comparison = compare_counters(before, after)
        entry.update({'counter': comparison, 'counter_identical': comparison['identical'], 'differing_fields': comparison['differing_fields']})
        if comparison['identical']:
            entry['outcome'] = 'promoted'
            own.add(window_id)
            summary['promoted'].append(window_id)
            record_window(entry)
            continue
        # the bar moved: this window goes back to its bytes, the lot tags back under the restored signature, the batch stops
        entry['outcome'] = 'rolled_back'
        try:
            entry['rollback'] = rollback(window_id, root=root, run_dir=run_dir, registry=registry, log=silent)
            if lot_tags:
                entry['rollback']['lot_tags_rekeyed'] = run_lot_tags(root, window_id, Path(lot_tags_report.format(id=window_id, date=datetime.now().strftime('%Y-%m-%d'))))
            restored, entry['counter_after_rollback'] = reading(window_id, 'after-rollback')
            entry['counter_restored_to_before'] = compare_counters(before, restored)['identical']
        except Refusal as refusal:
            entry['rollback'] = {'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail}
        summary['rolled_back'].append(window_id)
        record_window(entry)
        return finish(window_id, f"counter differs: {','.join(comparison['differing_fields'])}")
    return finish()


def _parse_since(text):
    value = datetime.fromisoformat(text)
    return value.astimezone(UTC) if value.tzinfo else value.astimezone()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('ids', nargs='*', help='window scene ids (win-* / lot-*; qa_* scratch copies)')
    parser.add_argument('--root', default='.')
    parser.add_argument('--run-dir', default=None, help=f'byte copies and records; default {RUN_DIR} (or {BATCH_RUN_DIR} with --batch)')
    parser.add_argument('--api', default='http://localhost:3008')
    parser.add_argument('--token-file')
    parser.add_argument('--registry', default=str(REGISTRY))
    parser.add_argument('--lot-tags-report', default=LOT_TAGS_REPORT, help='path template with {id} and {date}, under data/reviews/eyes')
    parser.add_argument('--no-lot-tags', action='store_true')
    parser.add_argument('--no-scratch', action='store_true', help='skip the qa_ scratch validation through the app')
    parser.add_argument('--no-restore-verbatim', action='store_true',
                        help='refuse when the recovery would change a reviewer-touched non-member object (default: restore it verbatim and list it in the plan)')
    parser.add_argument('--dry-run', action='store_true', help='compute the plan, print one line per window, write under --run-dir only')
    parser.add_argument('--batch', action='store_true', help='promote the ids one at a time, the counter identical after each (else rollback + stop)')
    parser.add_argument('--batch-dir', default=None, help='per-window batch records; default <run-dir>/batch')
    parser.add_argument('--quiet-since', metavar='ISO', help='stop the batch before a window when a review.json this program did not write changed after this time')
    parser.add_argument('--rollback', metavar='ID')
    parser.add_argument('--continue', dest='continue_on_error', action='store_true')
    args = parser.parse_args(argv)
    token = Path(args.token_file).read_text().strip() if args.token_file else None
    run_dir = Path(args.run_dir) if args.run_dir else (BATCH_RUN_DIR if args.batch else RUN_DIR)
    args.run_dir = str(run_dir)
    restore_verbatim = not args.no_restore_verbatim
    if args.rollback:
        if args.ids:
            parser.error('--rollback takes one id and no promotions')
        try:
            result = rollback(args.rollback, root=Path(args.root), run_dir=Path(args.run_dir), registry=Path(args.registry))
        except Refusal as refusal:
            print(json.dumps({'window_scene_id': args.rollback, 'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail}, indent=1))
            return 1
        print(json.dumps(result, indent=1))
        return 0
    if not args.ids:
        parser.error('give at least one window id or --rollback ID')
    if args.batch and not args.dry_run:
        try:
            summary = batch(args.ids, root=Path(args.root), run_dir=run_dir, batch_dir=Path(args.batch_dir) if args.batch_dir else None,
                            quiet_since=_parse_since(args.quiet_since) if args.quiet_since else None, continue_on_error=args.continue_on_error,
                            registry=Path(args.registry), lot_tags=not args.no_lot_tags, lot_tags_report=args.lot_tags_report,
                            api=args.api, token=token, scratch=not args.no_scratch, restore_verbatim=restore_verbatim)
        except Refusal as refusal:
            print(json.dumps({'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail}, indent=1))
            return 1
        print(json.dumps({k: summary.get(k) for k in ('promoted', 'skipped', 'failed', 'rolled_back', 'stopped_at', 'stop_reason', 'batch_dir')}, indent=1))
        return 1 if summary['stopped_at'] or summary['failed'] else 0
    results, failed = [], 0
    for window_id in args.ids:
        try:
            results.append(promote(window_id, root=Path(args.root), run_dir=run_dir, api=args.api, token=token, registry=Path(args.registry),
                                   lot_tags_report=args.lot_tags_report, lot_tags=not args.no_lot_tags, scratch=not args.no_scratch,
                                   restore_verbatim=restore_verbatim, dry_run=args.dry_run, log=(lambda _: None) if args.dry_run else promote.__kwdefaults__['log']))
        except Refusal as refusal:
            failed += 1
            results.append({'window_scene_id': window_id, 'outcome': 'refused', 'reason': refusal.reason, 'detail': refusal.detail})
            if not args.dry_run:
                print(f'REFUSED {window_id}: {refusal}', file=sys.stderr, flush=True)
            if not args.continue_on_error and not args.dry_run:
                break
        if args.dry_run:
            print(plan_line(results[-1]), flush=True)
    if args.dry_run:
        return 1 if failed else 0
    print(json.dumps([{k: r.get(k) for k in ('window_scene_id', 'outcome', 'reason', 'detail', 'previous_model_signature', 'new_model_signature',
                                             'review_revision_before', 'review_revision_after', 'replaced_member_ids')} | {'features': [f['id'] for f in r.get('features') or []]}
                      for r in results], indent=1))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
