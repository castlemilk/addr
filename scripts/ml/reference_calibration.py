"""Pre-registered sampled-roof calibration protocol: plan, review queue and progress.

One fixed-hash owned scan roof per (evidence band, validation core-tile group), so a bounded
human review budget (>= 20 groups per band) can yield a Wilson interval for boundary success
(IoU >= 0.85 against the human-reviewed ring). The sample is chosen by hash before any outcome
exists and never by band reasons, detector support or review state.

The group is the z17 tile of the OBJECT centroid (`scene_split.group_split` on 'z17:X:Y'),
not the whole 1152 px frame policy (`scene_split.image_split_audit`, unchanged). Because SAM3
encoded 64 px of the neighbouring tiles in every frame, the split is a calibration split only,
never a held-out training claim.

Reads confidence.sqlite, the frozen run.json, latest.json and review.json read-only; writes only
data/reviews/eyes/calibration/<plan_id>/{plan.json, review-queue.md, progress.json}. Nothing here
touches scene_confidence.calibrate() or data/suburbs/mooroolbark/calibration.json. Bands are
evidence (model estimates, not accuracy); only human decisions in review.json enter the count.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from shapely.errors import ShapelyError
from shapely.geometry import Polygon, shape

from . import eyes_guard
from . import reference_registry as registry
from .review_authority import carried_undecided, review_authority_blocker
from .scene_batch import signature
from .scene_confidence import MIN_CALIBRATION_GROUPS, wilson
from .scene_split import group_split
from .scene_training import atomic_json, atomic_text

VERSION = 'reference-calibration-v2'
# v2: detector slivers (fragments) are not sampled; a 1 m² fragment cannot be reviewed as a roof and wastes a labelled slot.
MIN_AREA_M2 = 6.
RUN_ID = '500114985968d40b'
RUN = Path('data/suburbs/mooroolbark/runs') / RUN_ID
CONFIDENCE = Path('data/suburbs/mooroolbark/confidence.sqlite')
CALIBRATION = Path('data/reviews/eyes/calibration')
DEFAULT_PROFILE_PREFIX = '53ea49d5'
BANDS = ('low', 'medium', 'high')
SPLIT_POLICIES = ('core',)
KEEP_SPLIT = 'validation'
IOU_SUCCESS = .85
LABEL_BUDGET_PER_BAND = MIN_CALIBRATION_GROUPS
STATUSES = ('confirmed', 'rejected', 'needs_adjustment', 'pending', 'stale')
POLICY = 'object-centroid core tile; calibration only; SAM3 encoded 64 px of neighbouring tiles'
STATEMENT = ('sampled-roof protocol; boundary IoU >= 0.85 only; not apex/height/recall; '
             'assumes independent representative areas')
SAMPLE_RULE = ('One roof per (band, group): the minimum scene_batch.signature([scene_id, object_id]) '
               '(sha256 of the JSON list), the same fixed-hash rule as scene_confidence.calibrate(); '
               'the pick never depends on outcome, band reasons, detector support or review state.')
SUCCESS_RULE = (
    'Success = human-reviewed boundary with IoU >= 0.85 against the model ring (pixel-space IoU, '
    'affine-invariant). confirmed + geometry_reviewed: success when the reviewed ring is unchanged '
    '(IoU 1.0) or edited with IoU >= 0.85, else failure. rejected: failure. needs_adjustment with an '
    'edited ring: success when IoU >= 0.85, else failure. needs_adjustment with an unedited ring is a '
    'tier-3 observation without a measured correction: excluded (neither success nor failure), which '
    'can bias the rate upward until the ring is edited. pending (no review, candidate, superseded - the '
    'outline was replaced by a human-drawn object and is context only - or confirmed without '
    'geometry_reviewed) and stale (review model_signature differs from latest.json) are excluded from the '
    'count; progress lists the pending reasons per band.')
CAVEATS = (
    'Evidence bands are model estimates, not accuracy; the interval measures only boundary IoU >= 0.85 '
    'against a human-reviewed ring, never apex, height, roof form or recall.',
    'The split is decided by the object centroid z17 tile. SAM3 encoded 64 px of the neighbouring tiles '
    'in every 1152 px frame, so train-tile pixels contributed context: calibration only, never a '
    'held-out training claim. The frame-level image_split_audit policy is unchanged.',
    'One roof per (band, group); groups are assumed independent and representative of the suburb, '
    'which is not verified.',
    'Review from the imagery only; the evidence band shown on /scenes must not steer the decision.',
)
PLAN_ID = re.compile(r'[a-f0-9]{16}')
SCENE_ID = re.compile(r'[a-zA-Z0-9_-]{1,100}')
TILE_SCENE = re.compile(r'.*-17-(\d+)-(\d+)')
SKIPPABLE = (KeyError, TypeError, ValueError, IndexError, AttributeError, ShapelyError)


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _now():
    return datetime.now(UTC).isoformat()


def _load(path):
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return None


def frozen_scenes(root=Path('.')):
    """Scene ids of the frozen run (run.json results keys)."""
    path = Path(root) / RUN / 'run.json'
    run = _load(path)
    results = run.get('results') if isinstance(run, dict) else None
    if not isinstance(results, dict) or not results:
        raise FileNotFoundError(f'Frozen run results unavailable: {path}')
    return set(results)


def core_group(geometry):
    """z17 tile key of a WGS84 polygon centroid: the object's own core tile, not the image frame."""
    polygon = registry.valid(shape(geometry))[0]
    if polygon.is_empty or polygon.area <= 0:
        raise ValueError('Empty roof geometry')
    return registry.spatial_group(polygon)


def scene_tile_group(scene_id):
    match = TILE_SCENE.fullmatch(str(scene_id))
    return f'z17:{int(match.group(1))}:{int(match.group(2))}' if match else None


def review_url(scene_id, object_id):
    return f'/scenes?scene={scene_id}&object={object_id}&kind=building&status=all&layer=model'


def owned_roofs(root=Path('.'), *, profile_prefix=DEFAULT_PROFILE_PREFIX, scenes=None):
    """Owned scan roofs from confidence.sqlite (mode=ro): rows of frozen-run scenes under the profile prefix."""
    path = Path(root) / CONFIDENCE
    if not path.is_file():
        raise FileNotFoundError(path)
    scenes = frozen_scenes(root) if scenes is None else scenes
    roofs, skipped = [], Counter()
    with closing(eyes_guard.connect_readonly(path)) as db:
        # After a profile promotion the live `roofs` table follows production and the frozen population is kept in
        # `profile_roofs` (roof-promotion-v1); the calibration plan is defined on the frozen profile, so read it from there
        # when that table holds rows for the requested prefix, else from the live table as before.
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        records = []
        if 'profile_roofs' in tables:
            records = db.execute('SELECT scene_id, object_id, payload FROM profile_roofs WHERE profile LIKE ? ORDER BY scene_id, object_id',
                                 (f'{profile_prefix}%',)).fetchall()
        if not records:
            records = db.execute('SELECT scene_id, object_id, payload FROM roofs ORDER BY scene_id, object_id').fetchall()
    for scene_id, object_id, payload in records:
        if scene_id not in scenes:
            skipped['scene_not_in_run'] += 1
            continue
        try:
            feature = json.loads(payload)
            props = feature.get('properties')
        except (ValueError, AttributeError):
            props = None
        if not isinstance(props, dict):
            skipped['payload_unreadable'] += 1
            continue
        if not str(props.get('profile') or '').startswith(profile_prefix):
            skipped['other_profile'] += 1
            continue
        roofs.append({'scene_id': scene_id, 'object_id': object_id, 'band': props.get('band'),
                      'detector_score': props.get('detector_score'), 'area_m2': props.get('area_m2'),
                      'review_url': props.get('review_url') or review_url(scene_id, object_id),
                      'geometry': feature.get('geometry')})
    return roofs, skipped


def sample_plan(root=Path('.'), *, profile_prefix=DEFAULT_PROFILE_PREFIX, bands=BANDS, split_policy='core', min_area_m2=MIN_AREA_M2):
    """Fixed-hash sample: one owned roof per (band, validation core-tile group); roofs under min_area_m2 (fragments) are skipped."""
    if split_policy not in SPLIT_POLICIES:
        raise ValueError(f'Unsupported split policy {split_policy!r}; choose one of {SPLIT_POLICIES}')
    requested = set(bands)
    unknown = requested - set(BANDS)
    if unknown or not requested:
        raise ValueError(f'Plan only assessable bands {BANDS}; got {sorted(unknown)}')
    bands = tuple(band for band in BANDS if band in requested)
    root = Path(root)
    scenes = frozen_scenes(root)
    roofs, skipped = owned_roofs(root, profile_prefix=profile_prefix, scenes=scenes)
    picks, candidates, splits, outside = {}, Counter(), Counter(), 0
    for roof in roofs:
        if roof['band'] not in bands:
            skipped['band_not_planned'] += 1
            continue
        try:
            group = core_group(roof['geometry'])
        except SKIPPABLE:
            skipped['invalid_geometry'] += 1
            continue
        if not isinstance(roof.get('area_m2'), (int, float)) or roof['area_m2'] < min_area_m2:
            skipped['fragment_or_unknown_area'] += 1
            continue
        split = group_split(group)
        splits[split] += 1
        if group != scene_tile_group(roof['scene_id']):
            outside += 1
        if split != KEEP_SPLIT:
            continue
        key = (roof['band'], group)
        candidates[key] += 1
        # Fixed hash sample: the smallest signature wins, so the pick never depends on outcome.
        sample_key = signature([roof['scene_id'], roof['object_id']])
        if key not in picks or sample_key < picks[key]['sample_key']:
            picks[key] = {'band': roof['band'], 'group': group, 'scene_id': roof['scene_id'], 'object_id': roof['object_id'],
                          'review_url': roof['review_url'], 'area_m2': roof['area_m2'], 'detector_score': roof['detector_score'],
                          'sample_key': sample_key}
    rows = sorted(picks.values(), key=lambda r: (BANDS.index(r['band']), r['group']))
    for row in rows:
        row['group_candidates'] = candidates[(row['band'], row['group'])]
    expected = {band: sum(1 for r in rows if r['band'] == band) for band in bands}
    plan_id = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]
    return {
        'version': VERSION, 'plan_id': plan_id, 'code_sha256': code_sha256(), 'created_at': _now(),
        'run_id': RUN_ID, 'profile_prefix': profile_prefix, 'split_policy': split_policy, 'split': KEEP_SPLIT,
        'policy': POLICY, 'sample_rule': SAMPLE_RULE, 'success_rule': SUCCESS_RULE, 'statement': STATEMENT, 'min_area_m2': min_area_m2,
        'iou_success': IOU_SUCCESS, 'bands': list(bands), 'expected_groups': expected,
        'label_budget': {'per_band_minimum': LABEL_BUDGET_PER_BAND, 'total_minimum': LABEL_BUDGET_PER_BAND * len(bands),
                         'planned': sum(expected.values()),
                         'shortfall': {band: max(0, LABEL_BUDGET_PER_BAND - n) for band, n in expected.items()}},
        'population': {'run_scenes': len(scenes), 'owned_roofs': len(roofs),
                       'owned_roofs_note': f'owned scan roofs of run {RUN_ID} under profile prefix {profile_prefix} (confidence.sqlite, read-only)',
                       'planned_band_roofs': sum(splits.values()), 'split_roofs': dict(splits),
                       'validation_roofs': splits[KEEP_SPLIT], 'centroid_outside_scene_tile': outside,
                       'skipped': dict(skipped)},
        'caveats': list(CAVEATS), 'rows': rows,
    }


def plan_folder(root, plan_id):
    if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
        raise ValueError('Invalid plan id')
    return Path(root) / CALIBRATION / plan_id


def _score_text(value):
    return f'{value:.2f}' if isinstance(value, (int, float)) and not isinstance(value, bool) else 'n/a'


def review_queue(plan):
    """Markdown queue grouped by band with /scenes links; evidence wording per UX-CONTRACT."""
    lines = [f"# Sampled-roof calibration queue `{plan['plan_id']}`", '',
             f"Version {plan['version']}; run {plan['run_id']}; profile prefix `{plan['profile_prefix']}`; created {plan['created_at']}.", '',
             f"Policy: {plan['policy']}.", '',
             f"Sample rule: {plan['sample_rule']}", '',
             f"Success rule: {plan['success_rule']}", '',
             f"Budget: minimum {plan['label_budget']['per_band_minimum']} reviewed groups per band before any interval; "
             f"planned {plan['label_budget']['planned']} roofs ({', '.join(f'{b} {n}' for b, n in plan['expected_groups'].items())}).", '',
             f"Statement of any interval: {plan['statement']}.", '', 'Caveats:', '']
    lines += [f'- {caveat}' for caveat in plan['caveats']]
    lines += ['', 'Evidence bands below are model estimates, not accuracy. Review each boundary from the imagery '
              'on `/scenes`; confirm with geometry reviewed, edit the ring, or reject.', '']
    for band in plan['bands']:
        rows = [r for r in plan['rows'] if r['band'] == band]
        shortfall = plan['label_budget']['shortfall'].get(band, 0)
        note = f' (short by {shortfall}: no interval possible for this band)' if shortfall else ''
        lines += [f"## {band} evidence band: {len(rows)} groups (minimum {plan['label_budget']['per_band_minimum']}){note}", '']
        for r in rows:
            area = f"{r['area_m2']:.0f} m2" if isinstance(r.get('area_m2'), (int, float)) else 'area n/a'
            lines.append(f"- [ ] `{r['group']}` [{r['scene_id']}/{r['object_id']}]({r['review_url']}) "
                         f"{area}, detector support {_score_text(r.get('detector_score'))} (model estimate, not accuracy), "
                         f"{r.get('group_candidates', 'n/a')} candidate(s) in group")
        lines.append('')
    return '\n'.join(lines)


def write_plan(root, plan):
    folder = plan_folder(root, plan['plan_id'])
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / 'plan.json', plan)
    atomic_text(folder / 'review-queue.md', review_queue(plan))
    return folder


def read_plan(root, plan_id):
    plan = _load(plan_folder(root, plan_id) / 'plan.json')
    if not isinstance(plan, dict) or plan.get('plan_id') != plan_id:
        raise FileNotFoundError(f'No plan {plan_id} under {Path(root) / CALIBRATION}')
    return plan


def _polygon(obj):
    holes = [h for h in (obj.get('holes') or []) if isinstance(h, list) and len(h) >= 3]
    return registry.valid(Polygon(obj['ring'], holes))[0]


def ring_iou(model_obj, reviewed_obj):
    """Pixel-space IoU of two objects' polygons (the same affine maps both, so metres give the same ratio)."""
    a, b = _polygon(model_obj), _polygon(reviewed_obj)
    if a.is_empty or b.is_empty:
        return 0.
    union = a.union(b).area
    return a.intersection(b).area / union if union > 0 else 0.


def _edited(model_obj, reviewed_obj):
    if not registry._same_ring(reviewed_obj.get('ring'), model_obj.get('ring')):
        return True
    holes = [h for h in (model_obj.get('holes') or []) if isinstance(h, list) and len(h) >= 3]
    reviewed = [h for h in (reviewed_obj.get('holes') or []) if isinstance(h, list) and len(h) >= 3]
    return len(holes) != len(reviewed) or not all(registry._same_ring(x, y) for x, y in zip(holes, reviewed, strict=True))


def _find(objects, object_id):
    for obj in objects if isinstance(objects, list) else []:
        if isinstance(obj, dict) and obj.get('id') == object_id:
            return obj
    return None


def classify(root, scene_id, object_id, cache=None):
    """Review state of one planned roof, read-only from latest.json and review.json."""
    cache = {} if cache is None else cache
    out = {'status': 'pending', 'counted': False, 'success': None, 'iou': None, 'ring_edited': None, 'detail': None,
           'model_signature': None, 'review_model_signature': None, 'review_status': None}
    if not isinstance(scene_id, str) or not SCENE_ID.fullmatch(scene_id):
        return {**out, 'detail': 'invalid_scene_id'}
    folder = Path(root) / 'data/scenes' / scene_id
    if scene_id not in cache:
        cache[scene_id] = (_load(folder / 'latest.json'), _load(folder / 'review.json'))
    latest, review = cache[scene_id]
    if not isinstance(latest, dict):
        return {**out, 'detail': 'latest_unavailable'}
    out['model_signature'] = latest.get('model_signature')
    if not isinstance(review, dict):
        return {**out, 'detail': 'no_review'}
    out['review_model_signature'] = review.get('model_signature')
    if review.get('model_signature') != latest.get('model_signature'):
        return {**out, 'status': 'stale', 'detail': 'review_model_signature_differs_from_latest'}
    model_obj, reviewed_obj = _find(latest.get('objects'), object_id), _find(review.get('objects'), object_id)
    if model_obj is None:
        return {**out, 'detail': 'object_not_in_latest'}
    if reviewed_obj is None:
        return {**out, 'detail': 'object_not_in_review'}
    status = reviewed_obj.get('status')
    out['review_status'] = status
    try:
        edited = _edited(model_obj, reviewed_obj)
        iou = 1. if not edited else ring_iou(model_obj, reviewed_obj)
    except SKIPPABLE:
        edited, iou = None, None
    out.update(ring_edited=edited, iou=iou)
    if authority_blocker := review_authority_blocker(reviewed_obj):
        return {**out, 'detail': f'{authority_blocker}: not a human decision, not counted'}
    if carried_undecided(reviewed_obj):
        return {**out, 'status': 'needs_adjustment', 'detail': 'carried_undecided: not decided on the new image, not counted'}
    if status == 'rejected':
        # A rejection is a human decision about the roof, not about the ring geometry: it counts even when
        # the reviewed ring is malformed.
        return {**out, 'status': 'rejected', 'counted': True, 'success': False, 'detail': 'rejected_by_reviewer'}
    if status == 'confirmed' and reviewed_obj.get('geometry_reviewed') is True:
        if iou is None:
            return {**out, 'detail': 'invalid_ring'}
        return {**out, 'status': 'confirmed', 'counted': True, 'success': iou >= IOU_SUCCESS,
                'detail': 'ring_edited' if edited else 'ring_unchanged'}
    if status == 'needs_adjustment':
        if edited is False:
            return {**out, 'status': 'needs_adjustment', 'detail': 'ring_unedited: tier-3 observation, not counted until the ring is edited'}
        if iou is None:
            return {**out, 'detail': 'invalid_ring'}
        return {**out, 'status': 'needs_adjustment', 'counted': True, 'success': iou >= IOU_SUCCESS, 'detail': 'ring_edited'}
    if status == 'confirmed':
        return {**out, 'detail': 'confirmed_without_geometry_reviewed'}
    return {**out, 'detail': f'status:{status}'}


def progress(root, plan):
    """Per-band review progress and Wilson intervals (only at >= MIN_CALIBRATION_GROUPS reviewed groups)."""
    root, cache, rows = Path(root), {}, []
    for planned in plan['rows']:
        base = {k: planned.get(k) for k in ('band', 'group', 'scene_id', 'object_id', 'review_url')}
        rows.append({**base, **classify(root, planned.get('scene_id'), planned.get('object_id'), cache)})
    bands = {}
    for band in plan['bands']:
        mine = [r for r in rows if r['band'] == band]
        counted = [r for r in mine if r['counted']]
        wins, n = sum(1 for r in counted if r['success']), len(counted)
        statuses = Counter(r['status'] for r in mine)
        bands[band] = {
            'groups_total': len(mine), 'groups_reviewed': n, 'successes': wins, 'failures': n - wins,
            'statuses': {status: statuses.get(status, 0) for status in STATUSES},
            'excluded': {'pending': statuses.get('pending', 0), 'stale': statuses.get('stale', 0),
                         'needs_adjustment_unedited': sum(1 for r in mine if r['status'] == 'needs_adjustment' and not r['counted'])},
            'pending_reasons': dict(sorted(Counter(r['detail'] for r in mine if r['status'] == 'pending').items())),
            'minimum_groups': MIN_CALIBRATION_GROUPS, 'remaining_to_interval': max(0, MIN_CALIBRATION_GROUPS - n),
            'interval': wilson(wins, n) if n >= MIN_CALIBRATION_GROUPS else None,
            'statement': STATEMENT,
        }
    return {'version': VERSION, 'plan_id': plan['plan_id'], 'plan_code_sha256': plan.get('code_sha256'),
            'code_sha256': code_sha256(), 'computed_at': _now(), 'confidence_level': .95, 'iou_success': IOU_SUCCESS,
            'statement': STATEMENT, 'success_rule': SUCCESS_RULE, 'policy': plan.get('policy', POLICY),
            'bands': bands, 'rows': rows}


def write_progress(root, plan_id, result):
    folder = plan_folder(root, plan_id)
    if not isinstance(result, dict) or result.get('plan_id') != plan_id:
        raise ValueError(f'Progress does not belong to plan {plan_id}')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'progress.json'
    atomic_json(path, result)
    return path


def _summary(plan):
    return {k: plan[k] for k in ('version', 'plan_id', 'run_id', 'profile_prefix', 'split_policy', 'policy',
                                 'expected_groups', 'label_budget', 'population', 'statement')}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Pre-registered sampled-roof calibration plan and progress (evidence, not accuracy).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--plan', action='store_true', help='Build the fixed-hash plan and write plan.json + review-queue.md.')
    parser.add_argument('--profile', default=DEFAULT_PROFILE_PREFIX, help='Profile prefix of the owned scan roofs.')
    parser.add_argument('--bands', nargs='+', default=list(BANDS), choices=BANDS)
    parser.add_argument('--no-write', action='store_true', help='With --plan: print the summary without writing files.')
    parser.add_argument('--progress', metavar='PLAN_ID', help='Classify reviews for a written plan and write progress.json.')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.plan:
        plan = sample_plan(root, profile_prefix=args.profile, bands=tuple(args.bands))
        summary = _summary(plan)
        if not args.no_write:
            summary['path'] = str(write_plan(root, plan))
        json.dump(summary, sys.stdout)
        sys.stdout.write('\n')
        return 0
    if args.progress:
        try:
            plan = read_plan(root, args.progress)
        except (FileNotFoundError, ValueError) as error:
            sys.stderr.write(f'{error}\n')
            return 1
        result = progress(root, plan)
        path = write_progress(root, plan['plan_id'], result)
        json.dump({k: result[k] for k in ('version', 'plan_id', 'computed_at', 'statement', 'bands')} | {'path': str(path)}, sys.stdout)
        sys.stdout.write('\n')
        return 0
    parser.print_help()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
