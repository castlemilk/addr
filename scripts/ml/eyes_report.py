"""Content-addressed eyes snapshots (diagnostics, not labels/accuracy).

A snapshot pins the Mooroolbark scan population (owned scan roofs of the frozen
run under one model profile) and summarises v1 bands, shadow v2 bands,
null/saturation-aware line mismatch, cross-capture pairs, reference agreement
by tier, object quality, roof diagnostics coverage, code drift and the
protected-reference guard. Every metric is {value, denominator, population};
bands, agreement and diagnostics are never calibrated accuracy. Mismatch is
null-aware: a roof with zero observed segments has no mismatch, whatever v1
stored (the frozen scan stores 0.0). Sidecar rows count toward the population
only when their model_signature is the owned roof's. Production inputs are
read-only; snapshots are written only to the eyes index and
data/reviews/eyes/snapshots/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import eyes_guard, eyes_index

VERSION = 'eyes-report-v1'
SUBURB = Path('data/suburbs/mooroolbark')
RUN_ID = '500114985968d40b'
RUN = SUBURB / 'runs' / RUN_ID
CONFIDENCE = SUBURB / 'confidence.sqlite'
SNAPSHOTS = Path('data/reviews/eyes/snapshots')
OWNED = 'owned scan roofs'
BANDS = ('high', 'medium', 'low')
SATURATED_M = 1.5
MIN_SEGMENTS = 3
QUANTILES = {'p10': .1, 'p25': .25, 'p50': .5, 'p75': .75, 'p90': .9}
VOLATILE = ('created_at', 'computed_at', 'snapshot_id', 'path')


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def metric(value, denominator, population):
    return {'value': value, 'denominator': denominator, 'population': population}


def _key(value):
    """Category key: missing values become 'none' so sorting and canonical JSON never mix None and str."""
    return 'none' if value is None else str(value)


def _counts(counter, denominator, population, keys=()):
    normalised = Counter()
    for key, count in counter.items():
        normalised[_key(key)] += count
    return {key: metric(normalised.get(key, 0), denominator, population) for key in [*keys, *sorted(set(normalised) - set(keys))]}


def _finite(value):
    """float(value) when finite, else None (NaN/inf/non-numeric are not observations)."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _segments(roof):
    value = roof.get('observed_segments')
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def quantile(values, q):
    """Linear-interpolated quantile (numpy 'linear'); None for an empty sample."""
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 4)


def _median(values):
    return quantile([v for v in map(_finite, values) if v is not None], .5)


def load_roofs(root):
    """Indexed roof properties from confidence.sqlite (read-only)."""
    path = root / CONFIDENCE
    if not path.is_file():
        return [], 0
    with closing(eyes_guard.connect_readonly(path)) as db:
        scenes = db.execute('SELECT COUNT(*) FROM scenes').fetchone()[0]
        roofs, seen = [], set()
        # After a profile promotion (roof-promotion-v1) the live `roofs` table follows production and the frozen
        # population is kept in `profile_roofs`; read both so the frozen run's dominant profile stays reportable.
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources = ['SELECT scene_id, object_id, payload FROM roofs']
        if 'profile_roofs' in tables:
            sources.append('SELECT scene_id, object_id, payload FROM profile_roofs')
        for sql in sources:
            for scene_id, object_id, payload in db.execute(sql):
                properties = (json.loads(payload) or {}).get('properties') or {}
                key = (properties.get('profile'), scene_id, object_id)
                if key in seen:
                    continue
                seen.add(key)
                roofs.append({**properties, 'scene_id': scene_id, 'object_id': object_id})
    return roofs, scenes


def population(root, roofs, indexed_scenes):
    """Pin owned scan roofs: roofs of the frozen run's scenes under its dominant profile."""
    run = _read_json(root / RUN / 'run.json')
    results = (run or {}).get('results') if isinstance(run, dict) else None
    if isinstance(results, dict) and results:
        source = 'run.json'
        candidates = [r for r in roofs if r['scene_id'] in results]
    else:
        source, results = 'confidence.sqlite profile', None
        candidates = roofs
    profiles = Counter(r.get('profile') for r in candidates)
    profile = profiles.most_common(1)[0][0] if profiles else None
    owned = [r for r in candidates if r.get('profile') == profile]
    signature_matches = None
    run_bands = Counter()
    run_objects = Counter()
    if results is not None:
        signature_matches = sum(1 for r in owned if r.get('model_signature') == (results.get(r['scene_id']) or {}).get('model_signature'))
        for result in results.values():
            run_bands.update((result or {}).get('confidence') or {})
            run_objects.update((result or {}).get('objects') or {})
    run_total = sum(run_bands.values()) if results is not None else None
    inventory = None
    for path in sorted((root / SUBURB).glob('rollout-verification-*.json')):
        found = (_read_json(path) or {}).get('suburb_inventory_roofs')
        inventory = found if isinstance(found, int) else inventory
    n = len(owned)
    summary = {
        'source': source, 'run_id': RUN_ID if results is not None else None, 'profile': profile,
        'owned_scan_roofs': metric(n, n, OWNED),
        'owned_scenes': metric(len({r['scene_id'] for r in owned}), len(results) if results is not None else None, 'run scenes'),
        'other_profile_roofs': metric(len(candidates) - n, len(candidates), 'roofs in run scenes' if results is not None else 'indexed roofs'),
        'signature_matches_run': metric(signature_matches, n if results is not None else None, OWNED),
        'run_reported_roofs': metric(run_total, run_total, 'run.json confidence counts'),
        'run_reported_objects': _counts(run_objects, sum(run_objects.values()) or None, 'run.json object counts'),
        'indexed_roofs': metric(len(roofs), len(roofs), 'indexed roofs (confidence.sqlite)'),
        'indexed_scenes': metric(indexed_scenes, indexed_scenes, 'indexed scenes (confidence.sqlite)'),
        'inventory_roofs': metric(inventory, inventory, 'suburb inventory (rollout verification)'),
    }
    return summary, owned, run_bands if results is not None else None


def bands_v1(owned, run_bands):
    n = len(owned)
    return {'indexed': _counts(Counter(r.get('band') for r in owned), n, OWNED, BANDS),
            'run_reported': ({} if run_bands is None else
                             _counts(run_bands, sum(run_bands.values()), 'run.json confidence counts', BANDS))}


def mismatch(owned):
    """Null-aware (zero-segment, null or non-finite mismatch is null) and saturation-aware (== 1.5 m) line mismatch."""
    n = len(owned)
    observed = []  # (segments, mismatch) for roofs with >= 1 observed segment and a finite mismatch
    for r in owned:
        value = _finite(r.get('image_line_mismatch_m'))
        if value is not None and _segments(r) >= 1:
            observed.append((_segments(r), value))
    eligible = [v for segments, v in observed if segments >= MIN_SEGMENTS]
    unsaturated = [v for v in eligible if v < SATURATED_M]
    zero = [r for r in owned if _segments(r) < 1]
    population_label = f'{OWNED} with observed_segments>={MIN_SEGMENTS} and non-null mismatch'
    unsaturated_label = f'{population_label}, mismatch<{SATURATED_M}'
    return {
        'eligible': metric(len(eligible), n, OWNED),
        'null': metric(n - len(observed), n, f'{OWNED} (null-aware: null, non-finite or zero observed segments)'),
        'null_v1': metric(sum(1 for r in owned if r.get('image_line_mismatch_m') is None), n, OWNED),
        'zero_segments': metric(len(zero), n, OWNED),
        'zero_segments_non_null_v1': metric(sum(1 for r in zero if _finite(r.get('image_line_mismatch_m')) is not None),
                                            len(zero), f'{OWNED} with zero observed segments (v1 value ignored)'),
        'saturated': metric(sum(1 for v in eligible if v >= SATURATED_M), len(eligible), population_label),
        'saturated_any_segments': metric(sum(1 for _, v in observed if v >= SATURATED_M), len(observed),
                                         f'{OWNED} with observed_segments>=1 and non-null mismatch'),
        'quantiles': {**{k: metric(quantile(eligible, q), len(eligible), population_label) for k, q in QUANTILES.items()},
                      'max': metric(max(eligible) if eligible else None, len(eligible), population_label)},
        'quantiles_unsaturated': {k: metric(quantile(unsaturated, q), len(unsaturated), unsaturated_label)
                                  for k, q in QUANTILES.items()},
    }


def load_sidecars(root):
    """Every eyes table as rows (read-only); empty when the index does not exist yet."""
    if not eyes_index.path(root).exists():
        return {table: [] for table in eyes_index.TABLES}
    with closing(eyes_index.connection(root, readonly=True)) as db:
        present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: (eyes_index.rows(db, table) if table in present else []) for table in eyes_index.TABLES}


def _payload(row):
    return row.get('payload') if isinstance(row.get('payload'), dict) else {}


def _owned_rows(rows, owned):
    """One row per owned object whose model_signature is the owned roof's (any row when the index has none);
    plus counts of rows outside the population and of stale-signature rows for owned objects."""
    signatures = {(r['scene_id'], r['object_id']): r.get('model_signature') for r in owned}
    chosen, outside, stale = {}, 0, 0
    for row in sorted(rows, key=lambda r: (r.get('version') or '', r.get('model_signature') or '')):
        key = (row.get('scene_id'), row.get('object_id'))
        if key not in signatures:
            outside += 1
        elif signatures[key] is not None and row.get('model_signature') != signatures[key]:
            stale += 1
        else:
            chosen[key] = row
    return list(chosen.values()), outside, stale


def shadow_v2(rows, owned):
    n = len(owned)
    kept, outside, stale = _owned_rows(rows, owned)
    label = 'shadow_bands rows (owned scan roofs)'
    payloads = [_payload(r) for r in kept]
    transitions = Counter(f"{p.get('band_v1')}->{p.get('band_v2')}" for p in payloads)
    return {'rows': metric(len(kept), n, OWNED), 'rows_outside_population': metric(outside, len(rows), 'shadow_bands rows'),
            'stale_signature_rows': metric(stale, len(rows), 'shadow_bands rows'),
            'versions': sorted({r.get('version') for r in kept if r.get('version')}),
            'bands': _counts(Counter(p.get('band_v2') for p in payloads), len(kept), label),
            'flips': metric(sum(1 for p in payloads if p.get('flip')), len(kept), label),
            'flip_matrix': _counts(transitions, len(kept), label)}


def cross_capture(rows, owned):
    """Row (scene_id, object_id) is the roof query() hides behind (other_scene_id, other_object_id) unless hidden_by='self'."""
    keys = {(r['scene_id'], r['object_id']) for r in owned}
    payloads = [_payload(r) for r in rows]
    label = 'cross_capture pairs'
    sides = {key for r in rows for key in ((r['scene_id'], r['object_id']), (r['other_scene_id'], r['other_object_id']))}
    hidden = {((r['other_scene_id'], r['other_object_id']) if p.get('hidden_by') == 'self' else (r['scene_id'], r['object_id']))
              for r, p in zip(rows, payloads, strict=True) if p.get('hidden_by')}
    return {'rows': metric(len(rows), len(rows), label),
            'versions': sorted({r.get('version') for r in rows if r.get('version')}),
            'band_disagreement': metric(sum(1 for p in payloads if p.get('band_disagreement')), len(rows), label),
            'hidden': metric(sum(1 for p in payloads if p.get('hidden_by')), len(rows), label),
            'hidden_roofs': metric(len(hidden), len(sides), 'roofs on either side of a cross_capture pair'),
            'hidden_low_under_higher': metric(sum(1 for p in payloads if p.get('band') == 'low' and p.get('other_band') in ('medium', 'high')),
                                              len(rows), label),
            'iou_median': metric(_median(p.get('iou') for p in payloads), len(rows), label),
            'owned_roofs_with_pair': metric(len(sides & keys), len(owned), OWNED),
            'owned_roofs_hidden': metric(len(hidden & keys), len(owned), OWNED)}


TIER_ROLES = {'1': 'human reference: confirmed + geometry_reviewed (label)',
              '2': 'human edit reference (label)',
              '3': 'agent observation (tier 3): never a label or an accuracy denominator',
              '4': 'context only: never a label'}


def tier_role(tier):
    """(label_tier, role) for 'T1'..'T4' (or 1..4); anything else is not a label."""
    digit = re.search(r'[1-4]', str(tier or ''))
    code = digit.group() if digit else None
    return code in ('1', '2'), TIER_ROLES.get(code, 'unknown tier: never a label')


def reference_agreement(references, agreement):
    """Agreement summary per reference tier; T3 rows are observations, never an accuracy denominator."""
    tiers = {r['ref_id']: r.get('tier') or 'unknown' for r in references}
    grouped = defaultdict(list)
    for row in agreement:
        grouped[tiers.get(row['ref_id'], 'unknown')].append(row)
    by_tier = {}
    for tier, rows in sorted(grouped.items()):
        label = f'agreement rows, reference tier {tier} (agreement, not calibrated accuracy)'
        payloads = [_payload(r) for r in rows]
        label_tier, role = tier_role(tier)
        by_tier[tier] = {
            'label_tier': label_tier, 'role': role,
            'rows': metric(len(rows), len(rows), label),
            'references': metric(len({r['ref_id'] for r in rows}), sum(1 for t in tiers.values() if t == tier), f'tier {tier} references'),
            'iou_median': metric(_median(p.get('iou') for p in payloads), sum(1 for p in payloads if p.get('iou') is not None), label),
            'hausdorff_m_median': metric(_median(p.get('hausdorff_m') for p in payloads),
                                         sum(1 for p in payloads if p.get('hausdorff_m') is not None), label),
            'image_identity': _counts(Counter(p.get('image_identity') for p in payloads if p.get('image_identity')), len(rows), label)}
    return {'references': metric(len(references), len(references), 'references rows'),
            'references_by_tier': _counts(Counter(tiers.values()), len(references), 'references rows'),
            'by_tier': by_tier}


def object_quality(rows):
    label = 'object_quality rows (detector support, not accuracy)'
    payloads = [_payload(r) for r in rows]
    n = len(rows)
    return {'rows': metric(n, n, label),
            'by_kind': _counts(Counter(p.get('kind') for p in payloads), n, label),
            'unparented': metric(sum(1 for p in payloads if p.get('unparented')), n, label),
            'flagged': metric(sum(1 for p in payloads if p.get('plausibility_flags')), n, label),
            'flags': _counts(Counter(f for p in payloads for f in p.get('plausibility_flags') or []), n, label),
            'support_tier': _counts(Counter(p.get('support_tier') for p in payloads if p.get('support_tier')), n, label)}


def roof_diagnostics(rows, owned):
    kept, outside, stale = _owned_rows(rows, owned)
    payloads = [_payload(r) for r in rows]
    clean = [p for p in payloads if not p.get('error')]
    compared = [p for p in clean if p.get('recomputed_matches_stored') is not None]
    return {'coverage': metric(len(kept), len(owned), OWNED),
            'coverage_without_error': metric(sum(1 for r in kept if not _payload(r).get('error')), len(owned), OWNED),
            'rows_outside_population': metric(outside, len(rows), 'roof_diagnostics rows'),
            'stale_signature_rows': metric(stale, len(rows), 'roof_diagnostics rows'),
            'rows': metric(len(rows), len(rows), 'roof_diagnostics rows'),
            'errors': metric(len(payloads) - len(clean), len(payloads), 'roof_diagnostics rows'),
            'recomputed_matches_stored': metric(sum(1 for p in compared if p['recomputed_matches_stored']), len(compared),
                                                'roof_diagnostics rows without error')}


def code_drift(root):
    """Current scripts vs the frozen plan.json code hashes (informational)."""
    plan = _read_json(root / RUN / 'plan.json')
    code = (plan or {}).get('code') if isinstance(plan, dict) else None
    label = 'plan.json code files'
    if not isinstance(code, dict):
        return {'changed': metric(None, None, label), 'missing': metric(None, None, label), 'changed_files': []}
    changed, missing = [], []
    for rel, expected in sorted(code.items()):
        path = root / rel
        if not path.is_file():
            missing.append(rel)
        elif eyes_guard.sha256_file(path) != expected:
            changed.append(rel)
    return {'changed': metric(len(changed), len(code), label), 'missing': metric(len(missing), len(code), label),
            'changed_files': changed, 'missing_files': missing}


def protected_reference_checks(root):
    report = eyes_guard.check(root, write_baseline=False)
    return {'ok': report['ok'], 'version': report['version'],
            'checks': [{'name': c['name'], 'ok': c['ok']} for c in report['checks']]}


def _stable(snapshot):
    return {k: v for k, v in snapshot.items() if k not in VOLATILE}


def snapshot_id(snapshot):
    """sha256 of the canonical snapshot JSON without timestamps/identity keys, first 16 hex."""
    canonical = json.dumps(_stable(snapshot), sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def snapshot(root=Path('.'), *, persist=True):
    root = Path(root)
    roofs, indexed_scenes = load_roofs(root)
    pop, owned, run_bands = population(root, roofs, indexed_scenes)
    sidecars = load_sidecars(root)
    report = {
        'version': VERSION, 'code_sha256': code_sha256(),
        'note': 'Diagnostics, not labels/accuracy: bands, agreement, cross-capture and object quality are never calibrated accuracy.',
        'population': pop,
        'bands_v1': bands_v1(owned, run_bands),
        'shadow_v2': shadow_v2(sidecars['shadow_bands'], owned),
        'mismatch': mismatch(owned),
        'cross_capture': cross_capture(sidecars['cross_capture'], owned),
        'reference_agreement': reference_agreement(sidecars['references'], sidecars['agreement']),
        'object_quality': object_quality(sidecars['object_quality']),
        'roof_diagnostics': roof_diagnostics(sidecars['roof_diagnostics'], owned),
        'code_drift': code_drift(root),
        'protected_reference_checks': protected_reference_checks(root),
    }
    now = datetime.now(UTC).isoformat()
    report.update(snapshot_id=snapshot_id(report), created_at=now, computed_at=now)
    if persist:
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            eyes_index.replace_rows(db, 'snapshots', [{'id': report['snapshot_id'], 'created_at': now,
                                                       'version': VERSION, 'payload': report}])
        path = root / SNAPSHOTS / f"{report['snapshot_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=1, sort_keys=True, allow_nan=False))
    return report


def _flatten(node, prefix=''):
    """Metric objects and scalar leaves by dotted path; lists of named checks are keyed by name."""
    flat = {}
    if isinstance(node, dict) and 'value' in node and 'population' in node:
        flat[prefix] = node
    elif isinstance(node, dict):
        for key, child in node.items():
            if prefix or key not in VOLATILE:
                flat.update(_flatten(child, f'{prefix}.{key}' if prefix else str(key)))
    elif isinstance(node, list) and node and all(isinstance(i, dict) and 'name' in i for i in node):
        for item in node:
            flat.update(_flatten({k: v for k, v in item.items() if k != 'name'}, f"{prefix}.{item['name']}"))
    else:
        flat[prefix] = node
    return flat


def diff(a, b):
    before, after = _flatten(a), _flatten(b)
    return {'added': sorted(set(after) - set(before)),
            'removed': sorted(set(before) - set(after)),
            'changed': [{'metric': k, 'before': before[k], 'after': after[k]}
                        for k in sorted(set(before) & set(after)) if before[k] != after[k]]}


def load(root, ref):
    """A stored snapshot by id (snapshots folder, else eyes index) or by JSON file path."""
    if Path(ref).is_file():
        found = _read_json(ref)
        return found if isinstance(found, dict) else None
    if not re.fullmatch(r'[0-9a-f]{16}', ref or ''):
        return None
    found = _read_json(Path(root) / SNAPSHOTS / f'{ref}.json')
    if found is None and eyes_index.path(root).exists():
        with closing(eyes_index.connection(root, readonly=True)) as db:
            rows = eyes_index.rows(db, 'snapshots', id=ref)
        found = rows[0]['payload'] if rows else None
    return found if isinstance(found, dict) else None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Eyes snapshot report (diagnostics, not labels/accuracy).')
    parser.add_argument('--root', default='.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--snapshot', action='store_true', help='Compute, store and print a snapshot.')
    group.add_argument('--diff', nargs=2, metavar=('A', 'B'), help='Diff two stored snapshots (id or JSON path).')
    parser.add_argument('--no-persist', action='store_true', help='Compute without writing the snapshot.')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.snapshot:
        output = snapshot(root, persist=not args.no_persist)
    else:
        a, b = (load(root, ref) for ref in args.diff)
        if a is None or b is None:
            missing = [ref for ref, found in zip(args.diff, (a, b), strict=True) if found is None]
            json.dump({'error': 'snapshot not found', 'missing': missing}, sys.stderr)
            sys.stderr.write('\n')
            return 2
        output = {'version': VERSION, 'a': a.get('snapshot_id'), 'b': b.get('snapshot_id'), **diff(a, b)}
    json.dump(output, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
