"""The reflection loop as one idempotent workflow (``review-cycle-v1``).

``python -m scripts.ml.review_cycle --run`` does, in order:

1. band-assess captures that have no confidence.json (``scene_confidence.assess(index=False)``:
   the file is written beside latest.json, the suburb confidence.sqlite is untouched, tiles are
   never assessed here and an existing confidence.json is never overwritten);
2. refresh building class hints (``building-flags-v2``) and structure sub-kinds
   (``structure-kinds-v2``) only for scenes whose rows are missing or signed by another model,
   or when the context caches changed since the previous cycle (code drift with the same rule
   version is reported, not acted on);
3. ``decision_policy.run`` (machine tiers with intervals) and ``decision_policy.reflect``
   (agreement with human decisions), writing ``proposals.json`` for the user to approve;
4. compare with the previous cycle: per-tier agreement deltas, new human decisions since the
   last cycle by the queue that surfaced them (machine-labelled table, calibration plan,
   structure cards, other), auto-tier strata that now have judgements;
5. write ``cycle-<timestamp>.json`` and ``CYCLE.md`` (plain sentences with denominators) ending
   with the exact next review actions.

The cycle never changes thresholds and never writes labels. ``--apply-approved PROPOSALS.json``
applies only entries the user marked ``"approved": true`` that carry a concrete ``change`` into
``thresholds.json`` (backup, audit, ``--rollback``). ``--auto-labels`` is an opt-in step that runs
the live machine-label apply for new auto-tier roofs only, skipping the eyes_guard protected
reference scenes by rule. Every number here is agreement or a design-rule count, never accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import decision_policy as dp
from . import eyes_index

VERSION = 'review-cycle-v1'
CYCLE_DIR = dp.REPORT_DIR
CONTEXT_DIR = Path('data/reviews/eyes/context')
CALIBRATION_DIR = Path('data/reviews/eyes/calibration')
TILE_PREFIX = 'mooroolbark-'
STRUCTURE_AREA_M2 = 150.
STRUCTURE_REVIEW_VERSIONS = ('structure-kinds-agent-review-v1', 'structure-kinds-human-review-v1')
SOURCES = ('machine-labelled table', 'calibration plan', 'structure cards', 'other queue')
ASSIST_SAMPLE = 20


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def now():
    return datetime.now(UTC).isoformat()


def timestamp():
    return datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- 1. capture assessment

def assess_captures(root=Path('.'), *, log=None, assess=None):
    """confidence.json for captures that have none; new files only, tiles never, suburb index untouched."""
    root = Path(root)
    assess = assess or (lambda sid: __import__('scripts.ml.scene_confidence', fromlist=['assess']).assess(sid, root, index=False))
    created, errors, skipped = [], [], Counter()
    scenes = root / 'data/scenes'
    for folder in sorted(scenes.iterdir()) if scenes.is_dir() else []:
        if not (folder / 'latest.json').exists():
            continue
        if folder.name.startswith(TILE_PREFIX):
            skipped['tile_never_assessed_here'] += 1
            continue
        if (folder / 'confidence.json').exists():
            skipped['already_has_confidence'] += 1
            continue
        try:
            report = assess(folder.name)
        except Exception as error:  # noqa: BLE001 - one capture's failure is a row, never an abort
            errors.append({'scene_id': folder.name, 'error': f'{type(error).__name__}: {error}'[:200]})
            continue
        bands = Counter(o['band'] for o in report['objects'])
        created.append({'scene_id': folder.name, 'path': str(folder / 'confidence.json'), 'objects': len(report['objects']), 'bands': dict(bands),
                        'model_signature': report.get('model_signature')})
        if log:
            log(json.dumps({'assessed': folder.name, 'objects': len(report['objects']), 'bands': dict(bands)}))
    bands = Counter()
    for row in created:
        bands.update(row['bands'])
    summary = {'version': VERSION, 'computed_at': now(), 'note': 'new confidence.json files only (backup-free: nothing existed before); suburb confidence.sqlite untouched',
               'scenes_assessed': len(created), 'objects_banded': sum(r['objects'] for r in created), 'band_distribution': dict(bands),
               'skipped': dict(skipped), 'errors': errors, 'created': created}
    if created or errors:
        _write_json(root / CYCLE_DIR / f'capture-assessment-{timestamp()}.json', summary)
    return summary


# --------------------------------------------------------------------------- 2. refresh plan

def context_fingerprint(root=Path('.')):
    folder = Path(root) / CONTEXT_DIR
    if not folder.is_dir():
        return None
    parts = []
    for file in sorted(folder.glob('*.json')):
        info = file.stat()
        parts.append(f'{file.name}:{info.st_size}:{info.st_mtime_ns}')
    return _sha('\n'.join(parts).encode())


def refresh_plan(root=Path('.'), previous=None):
    """Which scenes need class hints / structure sub-kinds recomputed, from coverage and model signatures; code drift is reported only."""
    root = Path(root)
    from . import scene_object_quality, structure_kinds
    scenes, buildings = {}, {}
    folder = root / 'data/scenes'
    for scene_dir in sorted(folder.iterdir()) if folder.is_dir() else []:
        file = scene_dir / 'latest.json'
        if not file.exists():
            continue
        try:
            scene = json.loads(file.read_bytes())
        except ValueError:
            continue
        owned = set(scene.get('owned_object_ids', [o['id'] for o in scene['objects']]))
        scenes[scene_dir.name] = scene.get('model_signature')
        buildings[scene_dir.name] = {o['id']: (o.get('model') or {}).get('area_m2') for o in scene['objects']
                                     if o.get('kind') == 'building' and o['id'] in owned}
    hints, structure, code = {}, {}, {'hints': Counter(), 'structure': Counter()}
    if eyes_index.path(root).exists():
        with closing(eyes_index.connection(root, readonly=True)) as db:
            for sid, oid, signature, sha, flags in db.execute(
                    "SELECT scene_id, object_id, model_signature, json_extract(payload,'$.code_sha256'), json_extract(payload,'$.class_hints.flags') "
                    'FROM object_quality WHERE version=?', (scene_object_quality.BUILDING_VERSION,)):
                try:
                    secondary = any(isinstance(f, dict) and f.get('flag') == 'secondary-structure' for f in json.loads(flags or '[]'))
                except ValueError:
                    secondary = False
                hints[(sid, oid)] = (signature, secondary)
                code['hints'][sha] += 1
            for sid, oid, signature, sha in db.execute(
                    "SELECT scene_id, object_id, model_signature, json_extract(payload,'$.code_sha256') FROM object_quality WHERE version=?",
                    (structure_kinds.VERSION,)):
                structure[(sid, oid)] = signature
                code['structure'][sha] += 1
    hint_scenes, structure_scenes, reasons = set(), set(), Counter()
    for sid, objects in buildings.items():
        for oid, area in objects.items():
            row = hints.get((sid, oid))
            if row is None:
                hint_scenes.add(sid)
                reasons['hints_missing'] += 1
            elif row[0] != scenes[sid]:
                hint_scenes.add(sid)
                reasons['hints_other_model'] += 1
            eligible = (isinstance(area, (int, float)) and area < STRUCTURE_AREA_M2) or bool(row and row[1])
            if eligible:
                signature = structure.get((sid, oid))
                if signature is None:
                    structure_scenes.add(sid)
                    reasons['structure_missing'] += 1
                elif signature != scenes[sid]:
                    structure_scenes.add(sid)
                    reasons['structure_other_model'] += 1
    fingerprint = context_fingerprint(root)
    previous_fingerprint = (previous or {}).get('refresh', {}).get('context_fingerprint') if previous else None
    context_changed = bool(previous_fingerprint) and fingerprint != previous_fingerprint
    if context_changed:
        hint_scenes = set(buildings)
        reasons['context_changed'] = len(buildings)
    drift = {'hints': {'module_sha256': scene_object_quality.code_sha256(), 'row_sha256': dict(code['hints']),
                       'drift': any(sha != scene_object_quality.code_sha256() for sha in code['hints'])},
             'structure': {'module_sha256': structure_kinds.code_sha256(), 'row_sha256': dict(code['structure']),
                           'drift': any(sha != structure_kinds.code_sha256() for sha in code['structure'])}}
    return {'hint_scenes': sorted(hint_scenes), 'structure_scenes': sorted(structure_scenes), 'reasons': dict(reasons),
            'context_fingerprint': fingerprint, 'context_changed': context_changed, 'code_drift': drift,
            'note': 'refresh only where rows are missing, signed by another model or the context caches changed; code drift with the same rule version is reported, not recomputed',
            'scenes': len(scenes), 'buildings': sum(len(v) for v in buildings.values())}


def default_refreshers():
    from . import scene_object_quality, structure_kinds

    def hints(root, scene_ids):
        return scene_object_quality.run(root, scene_ids, buildings=True, offline=True)

    def structure(root, scene_ids):
        rows, written = structure_kinds.run(root, scene_ids)
        return {'rows': len(rows), 'written': written}
    return {'hints': hints, 'structure': structure}


def refresh(root, plan, refreshers=None, log=None):
    refreshers = refreshers or default_refreshers()
    executed = {}
    for name, key in (('hints', 'hint_scenes'), ('structure', 'structure_scenes')):
        scene_ids = plan[key]
        if not scene_ids:
            executed[name] = {'status': 'skipped', 'reason': 'rows present for the current models', 'scenes': 0}
            continue
        started = time.perf_counter()
        try:
            result = refreshers[name](root, scene_ids)
            executed[name] = {'status': 'refreshed', 'scenes': len(scene_ids), 'elapsed_seconds': round(time.perf_counter() - started, 1),
                              'result': result if isinstance(result, (dict, int, str)) else str(type(result).__name__)}
        except Exception as error:  # noqa: BLE001 - the cycle records the failure and continues with the rows it has
            executed[name] = {'status': 'error', 'scenes': len(scene_ids), 'error': f'{type(error).__name__}: {error}'[:200]}
        if log:
            log(json.dumps({'refresh': name, **{k: v for k, v in executed[name].items() if k != 'result'}}))
    return executed


# --------------------------------------------------------------------------- 4. human decisions by source

def machine_labelled_keys(root=Path('.')):
    """(scene, object) pairs the machine labelled: audit items plus review objects with machine provenance."""
    keys = set()
    for audit in sorted((Path(root) / CYCLE_DIR).glob('apply-auto-live*.json')):
        try:
            doc = json.loads(audit.read_bytes())
        except (OSError, ValueError):
            continue
        for item in doc.get('items', []):
            keys.add((item['scene_id'], item['object_id']))
    scenes = Path(root) / 'data/scenes'
    for review in scenes.glob('*/review.json') if scenes.is_dir() else []:
        try:
            doc = json.loads(review.read_bytes())
        except (OSError, ValueError):
            continue
        for obj in doc.get('objects', []):
            if isinstance(obj, dict) and dp.has_machine_provenance(obj):
                keys.add((review.parent.name, obj.get('id')))
    return keys


def calibration_plans(root=Path('.')):
    """{plan_id: plan} for every written calibration plan."""
    plans = {}
    for file in sorted((Path(root) / CALIBRATION_DIR).glob('*/plan.json')):
        try:
            plan = json.loads(file.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(plan, dict) and isinstance(plan.get('rows'), list):
            plans[file.parent.name] = plan
    return plans


def calibration_keys(plans):
    return {(r.get('scene_id'), r.get('object_id')) for plan in plans.values() for r in plan['rows'] if isinstance(r, dict)}


def structure_card_rows(root=Path('.')):
    """Structure-card observations: {(scene, object): {'agent': n, 'human': n, 'agrees': bool|None}}."""
    out = defaultdict(lambda: {'agent': 0, 'human': 0, 'agrees': None})
    if not eyes_index.path(root).exists():
        return dict(out)
    with closing(eyes_index.connection(root, readonly=True)) as db:
        for version in STRUCTURE_REVIEW_VERSIONS:
            for sid, oid, tier, agrees in db.execute(
                    "SELECT scene_id, object_id, json_extract(payload,'$.tier'), json_extract(payload,'$.agrees') FROM object_quality WHERE version=?",
                    (version,)):
                entry = out[(sid, oid)]
                entry['human' if isinstance(tier, int) and tier <= 2 else 'agent'] += 1
                if agrees is not None:
                    entry['agrees'] = bool(agrees)
    return dict(out)


def classify_sources(decisions, machine_keys, calibration, cards):
    """Attach the queue that surfaced each human decision (precedence: machine-labelled, calibration plan, structure cards, other)."""
    out = []
    for h in decisions:
        key = (h['scene_id'], h['object_id'])
        source = ('machine-labelled table' if key in machine_keys else 'calibration plan' if key in calibration
                  else 'structure cards' if key in cards else 'other queue')
        out.append({**h, 'source': source})
    return out


def decision_keys(decisions):
    return {f"{h['scene_id']}/{h['object_id']}/{h['status']}" for h in decisions if h['status'] in ('confirmed', 'rejected', 'needs_adjustment', 'superseded')}


def diff_cycles(previous, current):
    """Per-tier agreement deltas and new human decisions by source between two cycle records (pure)."""
    if not previous:
        return {'previous': None, 'note': 'first cycle: baseline only, nothing to compare against',
                'new_decisions': {'total': 0, 'by_source': {}}, 'per_tier': {}, 'auto_strata_newly_judged': []}
    per_tier = {}
    for tier in dp.TIERS:
        before = (previous.get('reflection') or {}).get('per_tier', {}).get(tier, {}).get('confirmed_share') or {}
        after = (current.get('reflection') or {}).get('per_tier', {}).get(tier, {}).get('confirmed_share') or {}
        per_tier[tier] = {'judged_before': before.get('n', 0), 'judged_after': after.get('n', 0), 'judged_delta': after.get('n', 0) - before.get('n', 0),
                          'confirmed_before': before.get('confirmed', 0), 'confirmed_after': after.get('confirmed', 0),
                          'share_before': before.get('share'), 'share_after': after.get('share'),
                          'share_delta': (round(after['share'] - before['share'], 4) if before.get('share') is not None and after.get('share') is not None else None)}
    previous_keys = set(previous.get('human_decision_keys') or [])
    new = [h for h in current.get('human_decisions', []) if f"{h['scene_id']}/{h['object_id']}/{h['status']}" not in previous_keys
           and h['status'] in ('confirmed', 'rejected', 'needs_adjustment', 'superseded')]
    by_source = defaultdict(Counter)
    for h in new:
        by_source[h['source']][h['status']] += 1
    before_judged = {s['stratum'] for s in previous.get('auto_strata_judged', [])}
    newly = [s for s in current.get('auto_strata_judged', []) if s['stratum'] not in before_judged]
    return {'previous': previous.get('cycle_id'), 'per_tier': per_tier,
            'new_decisions': {'total': len(new), 'by_source': {k: dict(v) for k, v in sorted(by_source.items())},
                              'items': [{k: h[k] for k in ('scene_id', 'object_id', 'kind', 'status', 'source')} for h in new][:200]},
            'auto_strata_newly_judged': newly}


# --------------------------------------------------------------------------- status of the review surfaces

def calibration_status(root, plans):
    from . import reference_calibration
    out = {}
    for plan_id, plan in plans.items():
        try:
            progress = reference_calibration.progress(root, plan)
        except Exception as error:  # noqa: BLE001 - one plan's failure never stops the cycle
            out[plan_id] = {'error': f'{type(error).__name__}: {error}'[:200]}
            continue
        bands = {band: {k: v[k] for k in ('groups_total', 'groups_reviewed', 'successes', 'failures', 'remaining_to_interval', 'interval')}
                 for band, v in progress['bands'].items()}
        out[plan_id] = {'version': plan.get('version'), 'rows': len(plan['rows']), 'bands': bands,
                        'reviewed': sum(b['groups_reviewed'] for b in bands.values()),
                        'pending': sum(v['excluded'].get('pending', 0) for v in progress['bands'].values()),
                        'queue': str(CALIBRATION_DIR / plan_id / 'review-queue.md')}
    return out


def structure_cards_status(cards):
    agent_only = [k for k, v in cards.items() if v['agent'] and not v['human']]
    return {'objects_with_observations': len(cards), 'human_observed': sum(1 for v in cards.values() if v['human']),
            'agent_only': len(agent_only), 'agent_disagreements_without_human': sum(1 for k in agent_only if cards[k]['agrees'] is False)}


def auto_strata_judged(rows):
    seen = {}
    for p in rows:
        if p['tier'] == 'auto' and p['interval']['judged']:
            seen[p['interval']['stratum']] = {'stratum': p['interval']['stratum'], 'confirmed': p['interval']['confirmed'], 'judged': p['interval']['judged'],
                                              'lower': p['interval']['lower'], 'upper': p['interval']['upper']}
    return sorted(seen.values(), key=lambda s: s['stratum'])


def assist_sample(rows, n=ASSIST_SAMPLE):
    """Fixed-hash sample of live assist roofs without a human decision: the cheapest way to give the assist tier a denominator."""
    candidates = []
    for p in rows:
        s = p['signals']
        if p['tier'] != 'assist' or not s['review']['live'] or s['review']['status'] not in (None, 'candidate'):
            continue
        key = _sha(f"{s['scene']['scene_id']}/{s['object_id']}".encode())
        candidates.append((key, {'scene_id': s['scene']['scene_id'], 'object_id': s['object_id'], 'confidence': p['confidence'],
                                 'label': dp.label_text(p), 'stratum': p['interval']['stratum'],
                                 'review_url': f"/scenes?scene={s['scene']['scene_id']}&object={s['object_id']}&kind=building&status=all&layer=model"}))
    candidates.sort(key=lambda c: c[0])
    return [c[1] for c in candidates[:n]]


def next_actions(cycle):
    tiers = cycle['decision']['tiers']['live']
    reflection = cycle['reflection']['per_tier']
    machine = cycle['machine_labels']
    actions = []
    if machine['total']:
        actions.append({'table': 'machine-labelled roofs (decision_policy auto tier; links in the apply-auto audit items and /queues decision badges)',
                        'count': machine['total'] - machine['human_decided'],
                        'action': f"review the {machine['total'] - machine['human_decided']} machine-labelled roofs that no human has re-decided "
                                  f"(of {machine['total']} written); change the status on any that are wrong so the auto tier gets its first denominator "
                                  f"(currently {reflection['auto']['confirmed_share']['n']} judged)"})
    else:
        actions.append({'table': 'machine-labelled roofs', 'count': tiers['auto'],
                        'action': f"{tiers['auto']} live auto-tier roofs carry no written label yet; run --auto-labels if you want them written"})
    sample = cycle['assist_sample']
    actions.append({'table': 'assist-tier sample (fixed hash, listed in this cycle file)', 'count': len(sample),
                    'action': f"review these {len(sample)} assist roofs (of {tiers['assist']} live assist roofs) so the assist tier reaches the 10 judged "
                              f"the proposals need (currently {reflection['assist']['confirmed_share']['n']})"})
    for plan_id, status in cycle['calibration'].items():
        if 'error' in status:
            continue
        remaining = status['rows'] - status['reviewed']
        if remaining:
            actions.append({'table': f'calibration plan {plan_id} ({status["queue"]})', 'count': remaining,
                            'action': f"review the remaining {remaining} of {status['rows']} sampled roofs; "
                                      + ', '.join(f"{b} needs {v['remaining_to_interval']} more groups for an interval" for b, v in status['bands'].items())})
    cards = cycle['structure_cards']
    if cards['agent_only']:
        actions.append({'table': 'structure cards (task eyes:structure-review record --tier human-observed)', 'count': cards['agent_only'],
                        'action': f"record human observations for the {cards['agent_only']} card objects that only an agent has looked at "
                                  f"({cards['agent_disagreements_without_human']} where the agent disagreed with the hint)"})
    for proposal in cycle['proposals']:
        if proposal.get('change') and not proposal.get('approved'):
            actions.append({'table': 'proposals.json', 'count': 1,
                            'action': f"decide proposal {proposal['id']} (set approved: true, then --apply-approved) or leave it"})
    return actions


# --------------------------------------------------------------------------- the cycle

def previous_cycle(root, before=None):
    files = sorted((Path(root) / CYCLE_DIR).glob('cycle-*.json'))
    files = [f for f in files if before is None or f.name < before]
    for file in reversed(files):
        try:
            doc = json.loads(file.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get('version') == VERSION:
            return doc
    return None


def run_cycle(root=Path('.'), *, recompute_budget_seconds=720., max_recompute=600, auto_labels=False, refreshers=None, assess=None, log=None):
    root = Path(root)
    started = time.perf_counter()
    stamp = timestamp()
    cycle_id = f'cycle-{stamp}'
    counter = 1
    while (root / CYCLE_DIR / f'{cycle_id}.json').exists():      # two cycles within one second keep distinct, ordered ids
        counter += 1
        cycle_id = f'cycle-{stamp}{counter}'
    previous = previous_cycle(root, before=f'{cycle_id}.json')
    captures = assess_captures(root, log=log, assess=assess)
    plan = refresh_plan(root, previous)
    executed = refresh(root, plan, refreshers, log)
    summary = dp.run(root, recompute_budget_seconds=recompute_budget_seconds, max_recompute=max_recompute, log=log)
    rows = dp.load_decisions(root)
    humans = dp.human_decisions(root)
    thresholds = dp.effective_thresholds(root)
    reflection = dp.reflect(rows, humans, thresholds)
    _write_json(root / dp.REPORT_DIR / 'summary.json', summary)
    _write_json(root / dp.REPORT_DIR / 'reflection.json', reflection)
    (root / dp.REPORT_DIR / 'REFLECTION.md').write_text(dp.reflection_markdown(reflection))
    proposals_path = root / dp.REPORT_DIR / 'proposals.json'
    existing = {}
    if proposals_path.exists():
        try:
            existing = {p['id']: p for p in json.loads(proposals_path.read_bytes()).get('proposals', []) if isinstance(p, dict)}
        except ValueError:
            existing = {}
    for proposal in reflection['proposals']:   # keep an approval the user already wrote on an unchanged proposal id
        if existing.get(proposal['id'], {}).get('approved') is True and existing[proposal['id']].get('change') == proposal.get('change'):
            proposal['approved'] = True
    _write_json(proposals_path, {'version': dp.VERSION, 'status': 'proposed, not applied; set "approved": true and run review_cycle --apply-approved',
                                 'thresholds_version': reflection['thresholds_version'], 'computed_at': reflection['computed_at'],
                                 'proposals': reflection['proposals']})
    machine_keys = machine_labelled_keys(root)
    plans = calibration_plans(root)
    cards = structure_card_rows(root)
    decisions = classify_sources(humans, machine_keys, calibration_keys(plans), set(cards))
    machine_rows = [p for p in rows if (p['signals']['scene']['scene_id'], p['signals']['object_id']) in machine_keys]
    machine = {'total': len(machine_keys), 'with_decision_row': len(machine_rows),
               'human_decided': sum(1 for h in decisions if h['source'] == 'machine-labelled table' and h['status'] != 'candidate'),
               'by_status': dict(Counter(h['status'] for h in decisions if h['source'] == 'machine-labelled table'))}
    auto_result = None
    if auto_labels:
        auto_result = dp.apply_auto(root, dry_run=False, acknowledged=True, payloads=rows, audit_path=root / CYCLE_DIR / f'apply-auto-live-{stamp}.json')
        auto_result = {k: auto_result.get(k) for k in ('written', 'files_created', 'files_updated', 'skipped_live', 'skipped_protected_reference_scene',
                                                        'protected_scenes', 'backup_dir', 'audit_path', 'by_label')}
    cycle = {'version': VERSION, 'cycle_id': cycle_id, 'computed_at': now(), 'code_sha256': code_sha256(), 'thresholds_version': thresholds['version'],
             'note': 'agreement and design-rule counts, never accuracy; the cycle applies no thresholds and writes no labels unless --auto-labels was passed',
             'capture_assessment': {k: v for k, v in captures.items() if k != 'created'},
             'refresh': {**plan, 'executed': executed},
             'decision': {'objects': summary['objects'], 'tiers': summary['tiers'], 'recompute': summary['recompute'], 'intervals': summary['intervals'],
                          'strata': summary['strata']},
             'reflection': {'per_tier': reflection['per_tier'], 'ordering': reflection['ordering'], 'calibration_bins': reflection['calibration_bins'],
                            'confusion_top': reflection['confusion_by_signal'][:10], 'roof_family': reflection['roof_family'],
                            'decisions_total': reflection['decisions_total'], 'matched': reflection['decisions_matched_to_building_rows']},
             'proposals': reflection['proposals'], 'proposals_path': str(proposals_path),
             'auto_strata_judged': auto_strata_judged(rows),
             'human_decisions': [{k: h[k] for k in ('scene_id', 'object_id', 'kind', 'status', 'source')} for h in decisions if h['status'] != 'candidate'],
             'human_decision_keys': sorted(decision_keys(decisions)),
             'machine_labels': machine, 'calibration': calibration_status(root, plans), 'structure_cards': structure_cards_status(cards),
             'assist_sample': assist_sample(rows), 'protected_scenes': sorted(dp.protected_scene_ids(root)), 'auto_labels': auto_result}
    cycle['diff'] = diff_cycles(previous, cycle)
    cycle['next_actions'] = next_actions(cycle)
    cycle['elapsed_seconds'] = round(time.perf_counter() - started, 1)
    _write_json(root / CYCLE_DIR / f'{cycle_id}.json', cycle)
    (root / CYCLE_DIR / 'CYCLE.md').write_text(render_markdown(cycle))
    return cycle


def _pct(share):
    return 'no share' if share is None else f'{share:.0%}'


def render_markdown(cycle):
    d, r, diff = cycle['decision'], cycle['reflection'], cycle['diff']
    live = d['tiers']['live']
    cap = cycle['capture_assessment']
    lines = [f"# Review cycle {cycle['cycle_id']}", '',
             'Every number is agreement with human reviewers or a design-rule count, never accuracy. The humans reviewed what the queues surfaced, not a random sample. '
             f"Thresholds {cycle['thresholds_version']}. The cycle applied no thresholds and wrote no labels"
             + ('.' if not cycle['auto_labels'] else f"; --auto-labels wrote {cycle['auto_labels']['written']} new machine labels."), '',
             '## What ran', '']
    lines.append(f"Capture assessment: {cap['scenes_assessed']} capture scenes gained a confidence.json ({cap['objects_banded']} objects; bands {cap['band_distribution'] or 'none'}); "
                 f"{cap['skipped'].get('already_has_confidence', 0)} scenes already had one and {cap['skipped'].get('tile_never_assessed_here', 0)} scan tiles are never assessed here; "
                 f"{len(cap['errors'])} errors.")
    ref = cycle['refresh']
    for name in ('hints', 'structure'):
        e = ref['executed'][name]
        lines.append(f"Refresh of {'class hints' if name == 'hints' else 'structure sub-kinds'}: {e['status']} ({e.get('reason') or f'{e['scenes']} scenes'}); "
                     f"code drift since the rows were computed: {'yes, reported only' if ref['code_drift'][name]['drift'] else 'no'}.")
    lines.append(f"Decision policy: {d['objects']} owned building objects; live tiers auto {live['auto']}, assist {live['assist']}, review {live['review']} of {live['total']}; "
                 f"{d['recompute']['recomputed']} roofs recomputed within the budget ({d['recompute']['skipped_budget']} borderline roofs left for later cycles).")
    lines += ['', '## Agreement with human decisions', '']
    for tier in dp.TIERS:
        s = r['per_tier'][tier]['confirmed_share']
        if s['n']:
            lines.append(f"{tier}: {s['confirmed']} of {s['n']} judged roofs confirmed ({_pct(s['share'])}, Wilson 95 % {s['wilson_95'][0]:.2f} to {s['wilson_95'][1]:.2f}); "
                         f"{r['per_tier'][tier]['band_unassessed']} of them had no evidence band.")
        else:
            lines.append(f'{tier}: no human-judged roofs yet, so no share can be stated.')
    ordering = r['ordering']
    lines.append('The auto tier has the highest confirmed share.' if ordering['auto_highest_confirmed_share']
                 else 'The auto tier cannot yet be shown to have the highest confirmed share.')
    strata = cycle['auto_strata_judged']
    lines.append('Auto-tier strata with at least one judgement: ' + (', '.join(f"{s['stratum']} {s['confirmed']} of {s['judged']}" for s in strata) if strata else 'none') + '.')
    lines += ['', '## Since the previous cycle', '']
    if not diff.get('previous'):
        lines.append(f"This is the first cycle: it records {len(cycle['human_decision_keys'])} human decisions as the baseline; the next cycle will report what is new.")
    else:
        new = diff['new_decisions']
        lines.append(f"{new['total']} new human decisions since {diff['previous']}: " + (', '.join(f"{src} {dict(c)}" for src, c in new['by_source'].items()) or 'none') + '.')
        for tier in dp.TIERS:
            t = diff['per_tier'][tier]
            lines.append(f"{tier}: judged {t['judged_before']} to {t['judged_after']} ({t['judged_delta']:+d}), confirmed share {_pct(t['share_before'])} to {_pct(t['share_after'])}.")
        lines.append('Auto strata newly judged: ' + (', '.join(s['stratum'] for s in diff['auto_strata_newly_judged']) or 'none') + '.')
    m = cycle['machine_labels']
    lines += ['', f"Machine labels written so far: {m['total']}; a human has re-decided {m['human_decided']} of them (statuses {m['by_status'] or 'none'}). "
              'A human who confirms a machine label as-is leaves no trace the cycle can count.', '', '## Proposals (not applied)', '']
    for p in cycle['proposals']:
        lines.append(f"- {p['id']}{' (approved, pending --apply-approved)' if p.get('approved') else ''}: {p['proposal']}")
    lines += ['', '## Next review actions', '']
    for i, a in enumerate(cycle['next_actions'], 1):
        lines.append(f"{i}. {a['table']}: {a['count']}. {a['action'][0].upper() + a['action'][1:]}.")
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------- approved proposals

def apply_approved(root, proposals_path, *, backup_dir=None, audit_path=None):
    """Apply only proposals marked approved: true that carry a concrete change, into thresholds.json (backup, audit, rollback)."""
    root = Path(root)
    doc = json.loads(Path(proposals_path).read_bytes())
    proposals = [p for p in doc.get('proposals', []) if isinstance(p, dict)]
    approved = [p for p in proposals if p.get('approved') is True]
    applicable = [p for p in approved if isinstance(p.get('change'), dict) and isinstance(p['change'].get('path'), list) and p['change']['path']]
    acknowledged = [p['id'] for p in approved if p not in applicable]
    stamp = timestamp()
    report = {'version': VERSION, 'mode': 'apply-approved', 'computed_at': now(), 'proposals_file': str(proposals_path),
              'proposals_total': len(proposals), 'approved': [p['id'] for p in approved], 'applied': [], 'acknowledged_without_change': acknowledged,
              'ignored_not_approved': [p['id'] for p in proposals if p.get('approved') is not True], 'writes': []}
    if not applicable:
        return report
    file = root / dp.THRESHOLDS_FILE
    existed = file.exists()
    before = file.read_bytes() if existed else None
    current = {'version': dp.VERSION, 'base_version': dp.THRESHOLDS['version'], 'overrides': []}
    if existed:
        try:
            current = json.loads(before)
        except ValueError:
            current = {'version': dp.VERSION, 'base_version': dp.THRESHOLDS['version'], 'overrides': []}
    overrides = [o for o in current.get('overrides', []) if isinstance(o, dict)]
    for p in applicable:
        path = [str(k) for k in p['change']['path']]
        overrides = [o for o in overrides if [str(k) for k in o.get('path', [])] != path]
        overrides.append({'path': path, 'value': p['change'].get('value'), 'proposal_id': p['id'], 'applied_at': now()})
        report['applied'].append({'id': p['id'], 'path': path, 'value': p['change'].get('value')})
    current.update(version=dp.VERSION, base_version=dp.THRESHOLDS['version'], overrides=overrides, updated_at=now())
    after = json.dumps(current, indent=1, sort_keys=True).encode()
    backup_dir = Path(backup_dir) if backup_dir else root / CYCLE_DIR / f'backup-{stamp}'
    backup = backup_dir / 'thresholds.json'
    backup.parent.mkdir(parents=True, exist_ok=True)
    if existed:
        backup.write_bytes(before)
    else:
        (backup_dir / 'thresholds.ABSENT').write_text('thresholds.json did not exist before the apply\n')
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_suffix('.tmp')
    temp.write_bytes(after)
    temp.replace(file)
    report['writes'].append({'scene_id': 'thresholds', 'path': str(file), 'existed': existed, 'backup': str(backup if existed else backup_dir / 'thresholds.ABSENT'),
                             'before_sha256': _sha(before) if existed else None, 'after_sha256': _sha(after)})
    report['thresholds_version'] = dp.effective_thresholds(root)['version']
    audit = Path(audit_path) if audit_path else root / CYCLE_DIR / f'apply-approved-{stamp}.json'
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps(report, indent=1, sort_keys=True, default=str))
    report['audit_path'] = str(audit)
    report['rollback'] = f'python -m scripts.ml.review_cycle --rollback {audit}'
    return report


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--run', action='store_true', help='run one full cycle (assess captures, refresh if needed, decide, reflect, diff, CYCLE.md)')
    parser.add_argument('--assess-captures', action='store_true', help='only band-assess captures without confidence.json')
    parser.add_argument('--auto-labels', action='store_true', help='with --run: also write NEW auto-tier machine labels (protected scenes skipped)')
    parser.add_argument('--apply-approved', metavar='PROPOSALS_JSON', help='apply proposals marked "approved": true into thresholds.json')
    parser.add_argument('--rollback', metavar='AUDIT_JSON', help='restore the files an apply audit lists')
    parser.add_argument('--recompute-budget-seconds', type=float, default=720.)
    parser.add_argument('--max-recompute', type=int, default=600)
    args = parser.parse_args(argv)
    root = Path(args.root)
    log = lambda line: print(line, file=sys.stderr, flush=True)  # noqa: E731
    if args.assess_captures:
        summary = assess_captures(root, log=log)
        print(json.dumps({k: summary[k] for k in ('scenes_assessed', 'objects_banded', 'band_distribution', 'skipped')} | {'errors': len(summary['errors'])}))
        return 0
    if args.apply_approved:
        report = apply_approved(root, args.apply_approved)
        print(json.dumps({k: report.get(k) for k in ('applied', 'acknowledged_without_change', 'ignored_not_approved', 'thresholds_version', 'audit_path', 'rollback')}))
        return 0
    if args.rollback:
        report = dp.rollback_auto(root, args.rollback)
        print(json.dumps({k: report[k] for k in ('restored', 'deleted', 'skipped', 'report_path')}))
        return 0
    if args.run:
        cycle = run_cycle(root, recompute_budget_seconds=args.recompute_budget_seconds, max_recompute=args.max_recompute,
                          auto_labels=args.auto_labels, log=log)
        print(json.dumps({'cycle_id': cycle['cycle_id'], 'tiers': cycle['decision']['tiers']['live'],
                          'per_tier': {t: cycle['reflection']['per_tier'][t]['confirmed_share'] for t in dp.TIERS},
                          'new_decisions': cycle['diff']['new_decisions']['total'], 'next_actions': len(cycle['next_actions']),
                          'elapsed_seconds': cycle['elapsed_seconds']}))
        return 0
    parser.print_help()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
