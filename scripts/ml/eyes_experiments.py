"""Declarative paired isolated experiments with claims and cannot_claim (CP3).

An experiment changes one declared variable between a baseline and a candidate
computed from identical input bytes, joins objects by lineage and reports
paired deltas with bootstrap intervals and their denominators. Re-inference
experiments (code_drift, extra_views, sam3_on_median) run scene_isolated.run
into data/reviews/eyes/experiments/<experiment>/<run>/ (never data/scenes);
join experiments (band_v2, split_policy_core) only read existing sidecar rows.
Bands, agreement, lineage and multi-date consistency are diagnostics; only
T1/T2 human references anchor IoU and line precision/recall, and small
reference counts are reported as anecdotal. Heights are class priors. Every
report carries version, code_sha256 and computed_at. The real SAM3 runner is
never started from the CLI without --allow-inference.

Stack inputs for extra_views / sam3_on_median come from a loader with the contract
    stack_loader(root, scene_id) -> {'views': list, 'median': HxWx3 uint8 | None,
                                     'registration': dict} | None
The default loader reads the scene's newest scene_stack report under
data/roof-review-loop/multidate/stacks/<stack_id>/ (no network; the orchestrator
runs `scene_stack --build`) and records 'unavailable' when there is none or it
has fewer than two accepted dates. Views are passed as (name, array, stack_id).
A scene_local_stack 'pair' report (saved PNG + exactly one accepted date, built
with --allow-pair) is accepted for extra_views through the shared dispatcher
scene_local_stack.views (edge-consensus + the aligned date's gray; no
stack-median); its median is None, so sam3_on_median records 'unavailable:
needs >= 3 views'. An 'available' stack always outranks a pair.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import numpy as np
from PIL import Image

from . import (
    eyes_eligibility,
    eyes_guard,
    eyes_index,
    paired_compare,
    scene_isolated,
    scene_lineage,
)
from .scene_self_review import validate_id
from .scene_training import atomic_json
from .shadow_bands import VERSION as SHADOW_VERSION

VERSION = 'eyes-experiments-v1'
REPO = Path(__file__).resolve().parents[2]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
EXPERIMENTS_DIR = Path('data/reviews/eyes/experiments')
CONFIDENCE = Path('data/suburbs/mooroolbark/confidence.sqlite')
PROFILE_PREFIX = eyes_eligibility.PROFILE_PREFIX  # frozen Mooroolbark scan profile 53ea49d5…
SAMPLE_SIZE = 10
SAMPLES = ('cluster', 'cluster+10')
MIN_CLAIM_N = paired_compare.MIN_CLAIM_N
REFERENCE_TIERS = list(paired_compare.REFERENCE_TIERS)
LOW_HEAVY_SHARE = .25
EXPERIMENTS = {
    'code_drift': {'variable': 'current code vs frozen snapshot output on identical inputs', 'mode': 'reinference',
                   'candidate_kwargs': {}, 'candidate': 'scene_isolated.run on the saved PNG with current code and no other change'},
    'band_v2': {'variable': 'shadow evidence rule v2 vs v1', 'mode': 'join',
                'candidate': 'shadow_bands rows joined to references (no re-inference)'},
    'extra_views': {'variable': 'gated multi-date evidence views in the line fitter', 'mode': 'reinference',
                    'candidate_kwargs': {'evidence_views': '<from scene_stack.views>'},
                    'stack': {'key': 'views', 'kwarg': 'evidence_views'},
                    'candidate': 'scene_isolated.run(evidence_views=stack views); lines must still pass line_evidence on the original RGB'},
    'split_policy_core': {'variable': 'review-loop eligibility under frame vs core split', 'mode': 'join',
                          'candidate': 'eyes_eligibility.count'},
    'sam3_on_median': {'variable': 'SAM3 detection on stack-median image', 'mode': 'reinference',
                       'candidate_kwargs': {'image_variant': '<stack-median RGB>'},
                       'stack': {'key': 'median', 'kwarg': 'image_variant'},
                       'candidate': 'scene_isolated.run(image_variant=stack median); only the whole-frame detector encode changes'},
}
NOTE = ('Paired isolated diagnostics, not labels/accuracy, except T1/T2 reference IoU and line precision/recall '
        'with their denominators.')
PAIR_STATUS = 'pair'                          # scene_local_stack two-view report: usable for views, never a median
STACK_RANK = {'available': 2, PAIR_STATUS: 1}  # newest-first within a rank; a full stack always beats a pair
MEDIAN_NEEDS = 'unavailable: needs >= 3 views (a pair stack is the saved PNG plus one date; no stack-median.png)'


def _now():
    return datetime.now(UTC).isoformat()


def _stamp(**extra):
    return {'version': VERSION, 'code_sha256': CODE_SHA256, 'computed_at': _now(), **extra}


def _root(root):
    return Path(root or REPO).resolve()


def _hash16(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)


def protected_reference_checks(root):
    report = eyes_guard.check(root, write_baseline=False)
    return {'ok': report['ok'], 'version': report['version'], 'checks': [{'name': c['name'], 'ok': c['ok']} for c in report['checks']]}


def registry_rows(root):
    """T1/T2 reference rows with a usable polygon from the root's eyes index, or None without an index."""
    if not eyes_index.path(root).exists():
        return None
    return [row for row, _ in paired_compare.references(paired_compare.load_registry(root))]


# --- planning ---------------------------------------------------------------------------------------

def band_mix_stratum(counts):
    """Scene stratum from its owned-roof band mix: any high roof, else the low share against 0.25."""
    total = sum(counts.values())
    if counts.get('high'):
        return 'has_high'
    return 'low_heavy' if total and counts.get('low', 0)/total >= LOW_HEAVY_SHARE else 'medium_heavy'


def allocate(sizes, k):
    """Largest-remainder proportional allocation of k slots, one slot at least per non-empty stratum, capped by size."""
    total = sum(sizes.values())
    if not sizes or total <= 0:
        return {}
    quotas = {s: k*n/total for s, n in sizes.items()}
    taken = {s: min(sizes[s], math.floor(q)) for s, q in quotas.items()}
    for s in sorted(sizes):
        if taken[s] == 0 and sizes[s] > 0 and sum(taken.values()) < k:
            taken[s] = 1
    order = sorted(sizes, key=lambda s: (-(quotas[s]-math.floor(quotas[s])), s))
    while sum(taken.values()) < k and any(taken[s] < sizes[s] for s in sizes):
        for s in order:
            if sum(taken.values()) >= k:
                break
            if taken[s] < sizes[s]:
                taken[s] += 1
    return taken


def scene_band_mix(root, profile_prefix=PROFILE_PREFIX):
    """Owned scan roofs per scene by band from the production confidence index (read-only)."""
    path = root/CONFIDENCE
    if not path.exists():
        return {}
    with closing(sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True)) as db:
        rows = db.execute("SELECT scene_id, json_extract(payload, '$.properties.band'), json_extract(payload, '$.properties.profile') "
                          'FROM roofs').fetchall()
    mix = {}
    for scene_id, band, profile in rows:
        if not str(profile or '').startswith(profile_prefix):
            continue
        try:
            validate_id(scene_id)
        except (TypeError, ValueError):
            continue
        if not (root/'data/scenes'/scene_id/'latest.json').exists():
            continue
        mix.setdefault(scene_id, Counter())[band or 'unassessed'] += 1
    return mix


def stratified_sample(mix, exclude, k, salt=VERSION):
    """Hash-fixed sample: within each band-mix stratum, scenes ordered by sha256(salt:scene_id)."""
    strata = {}
    for scene_id, counts in mix.items():
        if scene_id in exclude:
            continue
        strata.setdefault(band_mix_stratum(counts), []).append(scene_id)
    quota = allocate({s: len(v) for s, v in strata.items()}, k)
    chosen = []
    for stratum in sorted(strata):
        ordered = sorted(strata[stratum], key=lambda s: hashlib.sha256(f'{salt}:{s}'.encode()).hexdigest())
        chosen += ordered[:quota.get(stratum, 0)]
    return chosen, {s: quota.get(s, 0) for s in sorted(strata)}, {s: len(v) for s, v in sorted(strata.items())}


def plan(experiment_id, scene_ids, *, sample='cluster', root=None, run_id=None):
    """Pure: a spec dict with roots under data/reviews/eyes/experiments/<experiment>/<run_id>/ (nothing written)."""
    experiment = EXPERIMENTS[experiment_id]
    if sample not in SAMPLES:
        raise ValueError(f'Choose a sample in {SAMPLES}')
    root = _root(root)
    given = list(dict.fromkeys(validate_id(s) for s in scene_ids))
    added, strata, available = [], {}, {}
    if sample == 'cluster+10':
        added, strata, available = stratified_sample(scene_band_mix(root), set(given), SAMPLE_SIZE)
    ids = given + added
    identity = {'experiment_id': experiment_id, 'scene_ids': ids, 'sample': sample,
                'candidate_kwargs': experiment.get('candidate_kwargs', {}), 'code_sha256': CODE_SHA256}
    run_id = run_id or _hash16(identity)
    out_root = root/EXPERIMENTS_DIR/experiment_id/run_id
    return {**_stamp(), 'experiment_id': experiment_id, 'run_id': run_id, 'variable': experiment['variable'],
            'mode': experiment['mode'], 'candidate': experiment.get('candidate'),
            'candidate_kwargs': dict(experiment.get('candidate_kwargs', {})), 'stack': experiment.get('stack'),
            'scene_ids': ids,
            'sample': {'mode': sample, 'requested': SAMPLE_SIZE if sample == 'cluster+10' else 0, 'added': len(added),
                       'strata': strata, 'available_by_stratum': available, 'given': len(given),
                       'rule': (f'hash-fixed: within each band-mix stratum (has_high; low share >= {LOW_HEAVY_SHARE} low_heavy; else '
                                f'medium_heavy) over owned scan roofs under profile {PROFILE_PREFIX}…, scenes ordered by '
                                f'sha256("{VERSION}:" + scene_id); largest-remainder proportional allocation, one per stratum')},
            'roots': {'root': str(root), 'out_root': str(out_root), 'baseline_root': str(out_root/'baseline'),
                      'candidate_root': str(out_root)},
            'lineage_experiment_id': f'{experiment_id}-{run_id}', 'note': NOTE}


def write_spec(spec):
    path = Path(spec['roots']['out_root'])/'spec.json'
    _write(path, spec)
    return path


# --- running ----------------------------------------------------------------------------------------

def registration_summary(report):
    """Compact registration facts from a scene_stack report (no transforms, no per-roof rows)."""
    captures = []
    for c in report.get('captures') or []:
        r = c.get('registration') or {}
        captures.append({'acquisition_id': c.get('acquisition_id'), 'date': c.get('date'), 'sensor': c.get('sensor'),
                         'accepted': r.get('accepted'), 'status': r.get('status'), 'reason': r.get('reason'),
                         'median_residual_px': r.get('median_residual_px'), 'holdout_ratio': r.get('holdout_ratio'),
                         'coverage': r.get('coverage'), 'shift_px': r.get('shift_px'), 'combined_fraction': c.get('combined_fraction')})
    local = report.get('per_roof_local') or []
    norms = [r.get('local_shift_norm_px') for r in local if _finite(r.get('local_shift_norm_px'))]
    return {'stack_id': report.get('stack_id'), 'status': report.get('status'), 'reason': report.get('reason'),
            **({'mode': PAIR_STATUS} if report.get('status') == PAIR_STATUS else {}),
            'n_accepted': report.get('n_accepted'), 'n_views': report.get('n_views'), 'gates': report.get('gates'),
            'discovery': {k: (report.get('discovery') or {}).get(k) for k in ('status', 'found', 'error')},
            'captures': captures, 'stack': report.get('stack'),
            'per_roof_local': {'rows': len(local), 'within_budget': sum(bool(r.get('within_budget')) for r in local),
                               'median_shift_px': round(float(np.median(norms)), 4) if norms else None},
            'version': report.get('version'), 'code_sha256': report.get('code_sha256'), 'computed_at': report.get('computed_at')}


def stack_reports(root, scene_id):
    """Every scene_stack report for this scene whose source image is the saved PNG, newest first."""
    try:
        from . import scene_stack
    except ImportError:
        return None, None
    root = Path(root)
    latest = root/'data/scenes'/scene_id/'latest.json'
    sha = json.loads(latest.read_text()).get('image_sha256') if latest.exists() else None
    found = []
    folder = root/scene_stack.STACKS
    for path in sorted(folder.glob('*/report.json')) if folder.exists() else []:
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if report.get('scene_id') == scene_id and (sha is None or report.get('source_image_sha256') == sha):
            found.append(report)
    found.sort(key=lambda r: (STACK_RANK.get(r.get('status'), 0), r.get('computed_at') or ''), reverse=True)
    return scene_stack, found


def stack_views(scene_stack, report, root):
    """The shared status dispatcher: scene_local_stack.views ('available' -> scene_stack.views, 'pair' -> pair_views).
    Falls back to scene_stack.views when scene_local_stack cannot be imported (then a pair report has no views)."""
    try:
        from . import scene_local_stack
    except ImportError:
        return scene_stack.views(report, Path(root)) if report.get('status') == 'available' else []
    return scene_local_stack.views(report, Path(root))


def saved_grid(root, scene_id):
    """(width, height) of the saved scene PNG from latest.json, or None when the scene is absent."""
    latest = Path(root)/'data/scenes'/scene_id/'latest.json'
    if not latest.exists():
        return None
    scene = json.loads(latest.read_text())
    width, height = scene.get('image_width'), scene.get('image_height')
    return (int(width), int(height)) if isinstance(width, int) and isinstance(height, int) else None


def default_stack_loader(root, scene_id):
    """{'views': [(name, HxW uint8, stack_id)], 'median': HxWx3 uint8 | None, 'registration': {…}, 'stack_id'} |
    {'error': …, 'registration': …} when the newest stack is not available | None without scene_stack."""
    scene_stack, reports = stack_reports(root, scene_id)
    if scene_stack is None:
        return None
    if not reports:
        return {'error': 'no scene_stack report for this scene (run scene_stack --build first)'}
    report = reports[0]
    registration = registration_summary(report)
    pair = report.get('status') == PAIR_STATUS
    if report.get('status') != 'available' and not pair:
        return {'error': f"stack {report.get('stack_id')} {report.get('status')}: {report.get('reason')}", 'registration': registration}
    grid = saved_grid(root, scene_id)
    stack_grid = (report.get('image_width'), report.get('image_height'))
    if grid and None not in stack_grid and tuple(stack_grid) != grid:
        return {'error': f'stack {report.get("stack_id")} grid {stack_grid[0]}x{stack_grid[1]} differs from the saved PNG '
                         f'{grid[0]}x{grid[1]}; views are not on the scene grid', 'registration': registration}
    try:
        views = [(name, array, report['stack_id']) for name, array in stack_views(scene_stack, report, root)]
        for name, array, _ in views:
            if grid and tuple(array.shape[:2]) != (grid[1], grid[0]):
                raise ValueError(f'view {name} is {array.shape[1]}x{array.shape[0]}, not the saved PNG grid {grid[0]}x{grid[1]}')
        median = None
        asset = (report.get('assets') or {}).get('stack-median.png')
        if asset:
            data = (scene_stack.stack_path(Path(root), report['stack_id'])/asset['path']).read_bytes()
            if hashlib.sha256(data).hexdigest() != asset.get('sha256'):
                raise ValueError('stack-median.png does not match its recorded sha256')
            gray = np.asarray(Image.open(io.BytesIO(data)).convert('L'))
            # The stack median is gray-only; the detector encode needs HxWx3, so the gray is replicated (no colour invented).
            median = np.ascontiguousarray(np.repeat(gray[:, :, None], 3, axis=2), dtype=np.uint8)
    except (OSError, ValueError, KeyError) as error:
        return {'error': f'stack assets unreadable: {str(error)[:300]}', 'registration': registration}
    if pair:
        # Two views: evidence views only. There is no median to hand to the detector, and none is synthesised.
        return {'views': views, 'median': None, 'registration': registration, 'stack_id': report['stack_id'],
                'mode': PAIR_STATUS, 'median_source': None, 'median_unavailable': MEDIAN_NEEDS}
    return {'views': views, 'median': median, 'registration': registration, 'stack_id': report['stack_id'],
            'median_source': 'stack-median.png (gray median over accepted dates + saved reference, replicated to 3 channels)'}


def variable_applied(experiment_id, record):
    variables = record.get('variables') or {}
    if experiment_id == 'extra_views':
        return bool((variables.get('evidence_views') or {}).get('applied'))
    if experiment_id == 'sam3_on_median':
        substitutions = variables.get('detector_frame_substitutions')
        return variables.get('image_variant') is not None and (substitutions is None or substitutions > 0)
    return True


def stack_input(stack, value, inputs):
    """What the runner received from the stack loader: names/shape and provenance, never the arrays."""
    facts = {'key': stack['key'], 'kwarg': stack['kwarg'], 'stack_id': inputs.get('stack_id')}
    if inputs.get('mode') is not None:
        facts['mode'] = inputs['mode']
    if stack['key'] == 'views':
        facts['views'] = [{'name': v[0] if isinstance(v, (list, tuple)) else (v.get('name') if isinstance(v, dict) else str(v)),
                           'shape': list(np.shape(v[1])) if isinstance(v, (list, tuple)) and len(v) > 1 and hasattr(v[1], 'shape') else None}
                          for v in value]
    else:
        facts['shape'] = list(np.shape(value)) if hasattr(value, 'shape') else None
        facts['median_source'] = inputs.get('median_source')
    return facts


def _record_subset(record):
    keys = ('version', 'scene_id', 'baseline_model_signature', 'candidate_model_signature', 'signature_reproduced',
            'baseline_diff', 'baseline_profile', 'candidate_profile', 'variables', 'objects', 'confidence_objects',
            'assess_error', 'baseline_confidence', 'warnings', 'elapsed', 'computed_at', 'module_sha256')
    return {k: record.get(k) for k in keys if k in record}


def _lineage(root, key, scene_id, base, cand, *, write=True):
    """Lineage rows for one scene; written to the root's eyes index only when write is set.

    Returns (rows, write_error). The index is shared with other sidecar writers, so a
    locked database is recorded on the scene instead of losing a finished run.
    """
    rows = scene_lineage.link(base, cand)
    if not write:
        return rows, None
    try:
        scene_lineage.write(root, key, scene_id, rows, baseline_model_signature=base.get('model_signature'),
                            candidate_model_signature=cand.get('model_signature'))
    except sqlite3.Error as error:
        return rows, f'{type(error).__name__}: {str(error)[:300]}'
    return rows, None


def lineage_totals(by_scene, write_errors=None):
    rows = [r for rows in by_scene.values() for r in rows]
    return {**scene_lineage.stats(rows), 'by_scene': {sid: scene_lineage.stats(rows) for sid, rows in sorted(by_scene.items())},
            'version': scene_lineage.VERSION, 'rule': 'id equality, else one-to-one raw-ring IoU >= 0.8 within kind; all kinds',
            'write_errors': dict(sorted((write_errors or {}).items()))}


def _bootstrap_deltas(pairs, key):
    return paired_compare.bootstrap_median([paired_compare._delta(b.get(key), a.get(key)) for b, a in pairs])


def metrics_section(compare):
    roofs = compare.get('roofs', [])
    paired = [r for r in roofs if r['match'] in ('id', 'iou')]
    referenced = [r for r in paired if r.get('reference_iou_delta') is not None]
    lined = [(r['line_pr_before'], r['line_pr_after']) for r in paired if r.get('line_pr_before') and r.get('line_pr_after')]
    return {'median_deltas': compare.get('bootstrap', {}), 'sign_counts': compare.get('sign_counts', {}),
            'band_transition_matrix': compare.get('band_transition_matrix', {}),
            'referenced': {'n': len(referenced), 'tiers': REFERENCE_TIERS, 'bootstrap': compare.get('bootstrap', {}).get('reference_iou_delta'),
                           'roofs': [{'scene_id': r['scene_id'], 'baseline_object_id': r['baseline_object_id'],
                                      'candidate_object_id': r['candidate_object_id'], 'ref_id': r['reference']['ref_id'],
                                      'tier': r['reference']['tier'], 'iou_before': r['reference_iou_before'],
                                      'iou_after': r['reference_iou_after'], 'delta': r['reference_iou_delta']} for r in referenced],
                           'denominator': 'paired owned roofs overlapping a T1/T2 reference at IoU >= 0.3'},
            'line_pr': {'n': len(lined), 'precision_delta': _bootstrap_deltas(lined, 'precision'), 'recall_delta': _bootstrap_deltas(lined, 'recall'),
                        'rule': 'lines match within 0.5 m / 10 deg against T1/T2 roof_line_edits edges (reference_agreement.lines_match)',
                        'denominator': 'paired referenced roofs with human roof-line edges on both sides'}}


def _directional(boot):
    lo, hi = boot.get('ci95') or (None, None)
    return boot.get('n', 0) >= MIN_CLAIM_N and lo is not None and (lo > 0 or hi < 0)


def _ci(boot):
    lo, hi = boot['ci95']
    return f'95% CI {lo:+.4g}..{hi:+.4g}'


def statements(experiment_id, compare, scenes, line_pr, guard):
    """Experiment-level claims / cannot_claim from denominators; paired_compare's own lines are merged in."""
    claims, cannot = list(compare.get('claims', [])), list(compare.get('cannot_claim', []))
    compared = [s for s in scenes if s.get('status') == 'compared']
    experiment = EXPERIMENTS.get(experiment_id, {})
    if experiment.get('mode') == 'reinference' and compared:
        reproduced = sum(bool(s.get('signature_reproduced')) for s in compared)
        claims.append(f'signature_reproduced for {reproduced} of {len(compared)} compared scenes (identical input bytes; '
                      'a reproduced model_signature means byte-identical model output).')
    if not compared:
        cannot.append('no scene was compared; every count is 0 and no delta exists.')
    for s in scenes:
        if s.get('status') != 'compared':
            cannot.append(f"scene {s.get('scene_id')} {s.get('status')}: {s.get('reason')}")
        elif experiment_id == 'extra_views' and not s.get('variable_applied'):
            cannot.append(f"evidence_views requested but not applied by scene_isolated for scene {s.get('scene_id')} "
                          '(add_roof_estimates hook TODO CP3); this candidate equals a code_drift run, not an extra_views result.')
        elif experiment_id == 'sam3_on_median' and not s.get('variable_applied'):
            cannot.append(f"image_variant was never substituted into the detector encode for scene {s.get('scene_id')} "
                          '(detector_frame_substitutions = 0); this candidate equals a code_drift run, not a sam3_on_median result.')
        if s.get('lineage_write_error'):
            cannot.append(f"lineage rows for scene {s.get('scene_id')} were not written to the eyes index ({s['lineage_write_error']}); "
                          'the report keeps them, the index does not.')
    profiles = (compare.get('population') or {}).get('profiles') or {}
    if profiles.get('before') and profiles.get('after') and set(profiles['before']) != set(profiles['after']):
        cannot.append('profiles differ: candidate is another model generation; its counts are never pooled with the '
                      'production population (owned scan roofs under the frozen profile).')
    boot = (compare.get('bootstrap') or {}).get('reference_iou_delta') or {'n': 0}
    n = boot.get('n', 0)
    if not n:
        cannot.append('n referenced roofs = 0: no reference-anchored (T1/T2) statement is possible.')
    elif n < MIN_CLAIM_N:
        cannot.append(f'n referenced roofs = {n}: anecdotal; reference IoU changes are case notes, not accuracy.')
    elif _directional(boot):
        claims.append(f"median reference IoU delta {boot['median']:+.4g} ({_ci(boot)}) over n = {n} T1/T2-referenced paired roofs.")
    else:
        cannot.append(f'reference IoU: n = {n}, 95% CI includes 0; no directional claim.')
    ln = (line_pr or {}).get('n', 0)
    if not ln:
        cannot.append('line precision/recall vs roof_line_edits: n = 0 referenced roofs with human roof lines; no line statement.')
    elif ln < MIN_CLAIM_N:
        cannot.append(f'line precision/recall vs roof_line_edits: n = {ln} referenced roofs with human roof lines; anecdotal.')
    else:
        for key, label in (('precision_delta', 'line precision'), ('recall_delta', 'line recall')):
            b = line_pr.get(key) or {'n': 0}
            if _directional(b):
                claims.append(f"{label} delta {b['median']:+.4g} ({_ci(b)}, n = {b['n']} referenced roofs with roof_line_edits).")
            else:
                cannot.append(f"{label} delta: n = {b.get('n', 0)}, 95% CI includes 0; no directional claim.")
    if guard and not guard.get('ok', False):
        failed = ', '.join(c['name'] for c in guard.get('checks', []) if not c.get('ok')) or 'unknown'
        cannot.append(f'protected reference checks failed ({failed}); results are not trustworthy until eyes_guard passes.')
    cannot += ['Bands, cross-capture agreement and multi-date consistency are diagnostics; agreement is not accuracy.',
               'All roof heights are class priors, not measurements; heights are class priors and never enter a delta.',
               'Roofs within one tile share imagery and are not independent; roof-level bootstrap intervals are optimistic.']
    return list(dict.fromkeys(claims)), list(dict.fromkeys(cannot))


def _registration(experiment_id, per_scene, used=()):
    if not EXPERIMENTS[experiment_id].get('stack'):
        return {'status': 'not_applicable', 'reason': f'{experiment_id} changes no imagery input.'}
    return {'status': 'available' if used else 'unavailable', 'scenes': per_scene}


def build_report(spec, compare, scenes, lineage, registration, guard, runs):
    line_pr = metrics_section(compare)['line_pr']
    claims, cannot = statements(spec['experiment_id'], compare, scenes, line_pr, guard)
    compared = [s for s in scenes if s['status'] == 'compared']
    population = compare.get('population') or {}
    return {**_stamp(), 'experiment_id': spec['experiment_id'], 'run_id': spec['run_id'], 'variable': spec['variable'],
            'mode': spec['mode'], 'candidate': spec.get('candidate'), 'candidate_kwargs': spec.get('candidate_kwargs', {}),
            'lineage_experiment_id': spec['lineage_experiment_id'], 'roots': spec['roots'],
            'population': {'scenes': scenes, 'scenes_requested': len(spec['scene_ids']), 'scenes_compared': len(compared),
                           'roofs': compare.get('counts', {'paired': 0, 'gained': 0, 'lost': 0}),
                           'excluded_unowned': population.get('excluded_unowned', 0),
                           'profiles': population.get('profiles', {'before': [], 'after': []}),
                           'kind': 'building', 'owned_only': True,
                           'denominator': population.get('denominator', 'paired roofs (lineage id equality, else raw-ring IoU >= 0.8)')},
            'metrics': metrics_section(compare), 'lineage': lineage, 'registration': registration,
            'claims': claims, 'cannot_claim': cannot, 'protected_reference_checks': guard,
            'paired_compare': {'version': compare.get('version'), 'code_sha256': compare.get('code_sha256'),
                               'settings': compare.get('settings'), 'scenes': compare.get('scenes')},
            'isolated_runs': runs, 'roofs': compare.get('roofs', []), 'note': NOTE}


def run(spec, *, runner=None, root=None, registry_rows=None, stack_loader=None, write=True):
    """Execute a planned experiment. Re-inference calls runner(scene_id, out_root, **candidate_kwargs) per scene
    (default scene_isolated.run), links lineage into the eyes index, then paired_compare over the compared scenes."""
    root = _root(root)
    experiment_id = spec['experiment_id']
    experiment = EXPERIMENTS[experiment_id]
    planned_root = (spec.get('roots') or {}).get('root')
    if planned_root and Path(planned_root).resolve() != root:
        raise ValueError(f'spec was planned for root {planned_root}; run it with that root (got {root}).')
    # Every mode writes under out_root: never the repository root or data/scenes, inside the repo only data/reviews/eyes.
    out_root = scene_isolated.validate_out_root(Path(spec['roots']['out_root']), root)
    if experiment['mode'] == 'join':
        out = out_root/'report.json' if write else None
        if experiment_id == 'band_v2':
            report = band_v2_report(root, spec['scene_ids'] or None, write=write, out=out)
        else:
            report = split_policy_report(root, write=write, out=out)
        report['run_id'] = spec['run_id']
        if write:
            write_spec(spec)
            _write(out, report)
        return report
    if runner is None:
        def runner(scene_id, out_root, **kwargs):  # the real isolated runner reads the saved scene from this root
            return scene_isolated.run(scene_id, out_root, repo=root, **kwargs)
    loader = stack_loader or default_stack_loader
    registry = registry_rows if registry_rows is not None else registry_rows_or_none(root)
    scenes, runs, links, registration, compared, used, write_errors = [], {}, {}, {}, [], [], {}
    for scene_id in spec['scene_ids']:
        entry = {'scene_id': scene_id, 'status': None, 'reason': None}
        scenes.append(entry)
        kwargs = {}
        stack = experiment.get('stack')
        if stack:
            inputs = loader(root, scene_id)
            value = inputs.get(stack['key']) if isinstance(inputs, dict) else None
            if isinstance(value, (list, tuple)) and not value:
                value = None  # an empty view list is no experiment variable at all
            if value is None:
                inputs_ = inputs if isinstance(inputs, dict) else {}
                error = inputs_.get('error') or inputs_.get(f"{stack['key']}_unavailable")
                entry.update(status='unavailable', reason='multi-date stack unavailable for this scene'
                             + (f' ({error})' if error else ' (scene_stack not built, no accepted dates or no views)'))
                registration[scene_id] = inputs.get('registration') or 'unavailable' if isinstance(inputs, dict) else 'unavailable'
                continue
            kwargs = {stack['kwarg']: value}
            registration[scene_id] = inputs.get('registration') or 'unavailable'
            entry['stack_input'] = stack_input(stack, value, inputs)
            used.append(scene_id)
        try:
            record = runner(scene_id, out_root, **kwargs)
        except scene_isolated.ExtractionBusy as error:
            entry.update(status='busy', reason=str(error))
            continue
        except (ValueError, FileNotFoundError, OSError, RuntimeError, TypeError, KeyError) as error:
            entry.update(status='failed', reason=str(error)[:500])
            continue
        runs[scene_id] = _record_subset(record)
        base, cand = paired_compare.load_scene(out_root/'baseline', scene_id), paired_compare.load_scene(out_root, scene_id)
        if base is None or cand is None:
            entry.update(status='failed', reason='latest.json missing in the baseline or candidate root after the run.')
            continue
        links[scene_id], write_error = _lineage(root, spec['lineage_experiment_id'], scene_id, base, cand, write=write)
        if write_error:
            write_errors[scene_id] = write_error
            entry['lineage_write_error'] = write_error
        entry.update(status='compared', signature_reproduced=record.get('signature_reproduced'),
                     baseline_model_signature=base.get('model_signature'), candidate_model_signature=cand.get('model_signature'),
                     variable_applied=variable_applied(experiment_id, record), elapsed=record.get('elapsed'),
                     warnings=list(record.get('warnings') or []), lineage=scene_lineage.stats(links[scene_id]))
        compared.append(scene_id)
    compare = paired_compare.compare(out_root/'baseline', out_root, compared, registry_rows=registry)
    guard = protected_reference_checks(root)
    report = build_report(spec, compare, scenes, lineage_totals(links, write_errors), _registration(experiment_id, registration, used),
                          guard, runs)
    report['registry'] = {'source': 'eyes.sqlite references (T1/T2 with a WGS84 polygon)' if registry is not None else None,
                          'rows': None if registry is None else len(registry)}
    report['planned_code_sha256'] = spec.get('code_sha256')
    if spec.get('code_sha256') and spec['code_sha256'] != CODE_SHA256:
        report['cannot_claim'].insert(0, (f"spec was planned under eyes_experiments code {spec['code_sha256'][:12]}… and run under "
                                          f'{CODE_SHA256[:12]}…; run_id {spec["run_id"]} no longer content-addresses the code that ran.'))
    if write:
        report['path'] = str(out_root/'report.json')
        write_spec(spec)
        _write(out_root/'report.json', report)
    return report


def registry_rows_or_none(root):
    return registry_rows(root)


# --- summarise an existing isolated run directory ---------------------------------------------------

def _experiment_of(record):
    variables = record.get('variables') or {}
    if variables.get('image_variant'):
        return 'sam3_on_median'
    if (variables.get('evidence_views') or {}).get('applied'):
        return 'extra_views'
    return 'code_drift'


def summarise_isolated(run_dir, root=None, *, registry_rows=None, write=True):
    """Lineage + paired comparison for a finished scene_isolated run directory; writes <run_dir>/experiment-report.json."""
    root = _root(root)
    run_dir = scene_isolated.validate_out_root(Path(run_dir).resolve(), root)
    record_path = run_dir/'isolated-run.json'
    if not record_path.exists():
        raise FileNotFoundError(f'No isolated-run.json in {run_dir}')
    record = json.loads(record_path.read_text())
    scene_id = validate_id(record['scene_id'])
    baseline_root, candidate_root = run_dir/'baseline', run_dir
    base, cand = paired_compare.load_scene(baseline_root, scene_id), paired_compare.load_scene(candidate_root, scene_id)
    if base is None or cand is None:
        raise FileNotFoundError('latest.json is missing in the baseline or candidate root of the run directory.')
    key = 'isolated-' + re.sub(r'[^A-Za-z0-9_-]', '-', run_dir.name)[:90]
    rows, write_error = _lineage(root, key, scene_id, base, cand, write=write)
    links = {scene_id: rows}
    registry = registry_rows if registry_rows is not None else registry_rows_or_none(root)
    compare = paired_compare.compare(baseline_root, candidate_root, [scene_id], registry_rows=registry)
    production = None
    if (root/'data/scenes'/scene_id/'latest.json').exists():
        production = paired_compare.compare(root, candidate_root, [scene_id], registry_rows=registry)
    experiment_id = _experiment_of(record)
    scene = {'scene_id': scene_id, 'status': 'compared', 'reason': None, 'signature_reproduced': record.get('signature_reproduced'),
             'baseline_model_signature': base.get('model_signature'), 'candidate_model_signature': cand.get('model_signature'),
             'variable_applied': variable_applied(experiment_id, record), 'elapsed': record.get('elapsed'),
             'warnings': list(record.get('warnings') or []), 'lineage': scene_lineage.stats(links[scene_id])}
    if write_error:
        scene['lineage_write_error'] = write_error
    spec = {'experiment_id': experiment_id, 'run_id': run_dir.name, 'variable': EXPERIMENTS[experiment_id]['variable'],
            'mode': 'reinference', 'candidate': EXPERIMENTS[experiment_id].get('candidate'),
            'candidate_kwargs': {k: v for k, v in (record.get('variables') or {}).items() if k in ('image_variant', 'evidence_views')},
            'lineage_experiment_id': key, 'scene_ids': [scene_id],
            'roots': {'root': str(root), 'out_root': str(run_dir), 'baseline_root': str(baseline_root), 'candidate_root': str(candidate_root)}}
    guard = protected_reference_checks(root)
    report = build_report(spec, compare, [scene], lineage_totals(links, {scene_id: write_error} if write_error else None),
                          _registration(experiment_id, {}), guard, {scene_id: record})
    stats = scene_lineage.stats(links[scene_id])
    report['scene_id'] = scene_id
    report['isolated_run'] = record
    report['registry'] = {'source': 'eyes.sqlite references (T1/T2 with a WGS84 polygon)' if registry is not None else None,
                          'rows': None if registry is None else len(registry)}
    report['headline'] = {'scene_id': scene_id, 'paired': compare['counts']['paired'], 'gained': compare['counts']['gained'],
                          'lost': compare['counts']['lost'], 'id_stability_rate': stats['id_stability_rate'],
                          'lineage_all_kinds': {k: stats[k] for k in ('paired', 'paired_by_id', 'paired_by_iou', 'gained', 'lost', 'baseline_objects', 'candidate_objects')},
                          'band_transitions': compare['band_transition_matrix'],
                          'band_transitions_vs_production': production['band_transition_matrix'] if production else None,
                          'signature_reproduced': record.get('signature_reproduced'),
                          'baseline_model_signature': base.get('model_signature'), 'candidate_model_signature': cand.get('model_signature'),
                          'excluded_unowned': compare['population']['excluded_unowned'],
                          'denominator': 'owned roofs (buildings) paired by lineage; band transitions count paired roofs with bands on both sides'}
    if production is not None:
        drift = ((record.get('baseline_confidence') or {}).get('production') or {})
        report['production_compare'] = {
            'note': ('Bands on the production side are the frozen production confidence.json; differences from the isolated '
                     'baseline include assess-code drift, not only the experiment variable.'),
            'counts': production['counts'], 'band_transition_matrix': production['band_transition_matrix'],
            'confidence_status': [{k: s.get(k) for k in ('scene_id', 'confidence_before', 'confidence_after', 'paired', 'reason')} for s in production['scenes']],
            'baseline_reassessment': drift}
        report['cannot_claim'].insert(0, (f"band changes vs production include assess-code drift ({drift.get('band_changes', 'unknown')} of "
                                          f"{drift.get('compared', 'unknown')} baseline bands moved under current assess code); only the isolated "
                                          'baseline (re-assessed by the same code) isolates the variable.'))
    if write:
        report['path'] = str(run_dir/'experiment-report.json')
        _write(run_dir/'experiment-report.json', report)
    return report


# --- join experiments (no re-inference) ------------------------------------------------------------

def _distribution(values):
    data = sorted(float(v) for v in values if _finite(v))
    if not data:
        return {'n': 0, 'median': None, 'q25': None, 'q75': None, 'min': None, 'max': None}
    def q(p):
        i = (len(data)-1)*p
        lo, hi = math.floor(i), math.ceil(i)
        return round(data[lo] + (data[hi]-data[lo])*(i-lo), 4)
    return {'n': len(data), 'median': q(.5), 'q25': q(.25), 'q75': q(.75), 'min': data[0], 'max': data[-1]}


def _join_path(root, experiment_id, scene_ids):
    run_id = _hash16({'experiment_id': experiment_id, 'scene_ids': list(scene_ids or []), 'sample': 'cluster',
                      'candidate_kwargs': EXPERIMENTS[experiment_id].get('candidate_kwargs', {}), 'code_sha256': CODE_SHA256})
    return root/EXPERIMENTS_DIR/experiment_id/run_id/'report.json'


def band_v2_report(root=None, scene_ids=None, *, write=True, out=None):
    """Flip matrix over owned roofs from shadow_bands rows, IoU vs T1/T2 references for flipped vs non-flipped."""
    root = _root(root)
    experiment = EXPERIMENTS['band_v2']
    base = {**_stamp(), 'experiment_id': 'band_v2', 'variable': experiment['variable'], 'mode': 'join', 'candidate': experiment['candidate'],
            'shadow_version': SHADOW_VERSION, 'note': NOTE}
    if not eyes_index.path(root).exists():
        return {**base, 'status': 'unavailable', 'population': {'roofs': 0, 'scenes': [], 'excluded_non_owned_rows': 0},
                'metrics': {}, 'claims': [], 'cannot_claim': ['eyes index unavailable: no shadow_bands rows to join.'],
                'protected_reference_checks': protected_reference_checks(root)}
    with closing(eyes_index.connection(root, readonly=True)) as db:
        if scene_ids is None:
            rows = eyes_index.rows(db, 'shadow_bands', version=SHADOW_VERSION)
        else:
            rows = [r for sid in dict.fromkeys(scene_ids) for r in eyes_index.rows(db, 'shadow_bands', scene_id=validate_id(sid), version=SHADOW_VERSION)]
        human = {r['ref_id'] for r in eyes_index.rows(db, 'references') if str(r.get('tier') or '').upper() in REFERENCE_TIERS}
        agreement, any_signature = {}, set()
        for a in eyes_index.rows(db, 'agreement'):
            payload = a.get('payload')
            if a['ref_id'] in human and isinstance(payload, dict) and _finite(payload.get('iou')):
                key = (a['scene_id'], a['object_id'], a['model_signature'])
                agreement[key] = max(agreement.get(key, -1.), float(payload['iou']))
                any_signature.add((a['scene_id'], a['object_id']))
    readable = [r for r in rows if isinstance(r.get('payload'), dict) and r['payload'].get('band_v1') and r['payload'].get('band_v2')]
    unreadable = len(rows) - len(readable)
    owned = [r for r in readable if r['payload'].get('population_scope') == 'owned_object_ids']
    excluded = len(readable) - len(owned)
    counts, by_v1, by_v2, flips, flipped_iou, steady_iou = Counter(), Counter(), Counter(), [], [], []
    signatures, mismatched = {}, 0
    for r in sorted(owned, key=lambda r: (r['scene_id'], r['object_id'])):
        p = r['payload']
        counts[f"{p['band_v1']}->{p['band_v2']}"] += 1
        by_v1[p['band_v1']] += 1
        by_v2[p['band_v2']] += 1
        signatures.setdefault(r['scene_id'], set()).add(r['model_signature'])
        iou = agreement.get((r['scene_id'], r['object_id'], r['model_signature']))
        if iou is None and (r['scene_id'], r['object_id']) in any_signature:
            mismatched += 1  # a human agreement row exists, but for another model generation of this object
        if p.get('flip'):
            flips.append({'scene_id': r['scene_id'], 'object_id': r['object_id'], 'band_v1': p['band_v1'], 'band_v2': p['band_v2'],
                          'drainage_status': p.get('drainage_status'), 'reference_iou': iou})
            if iou is not None:
                flipped_iou.append(iou)
        elif iou is not None:
            steady_iou.append(iou)
    scenes = sorted(signatures)
    n, nf = len(owned), len(flips)
    claims = [f'{nf} of {n} owned roofs flip band under rule v2 (shadow column; diagnostic, not accuracy).'] if n else []
    cannot = [] if n else [f'no shadow_bands rows ({SHADOW_VERSION}) for the requested population; nothing to join.']
    if flipped_iou and len(flipped_iou) < MIN_CLAIM_N:
        cannot.append(f'n referenced flipped roofs = {len(flipped_iou)}: anecdotal; IoU against T1/T2 references is a case note.')
    elif not flipped_iou and nf:
        cannot.append('n referenced flipped roofs = 0: no reference-anchored statement about flipped roofs is possible.')
    if unreadable:
        cannot.append(f'{unreadable} shadow_bands row(s) had no readable band_v1/band_v2 payload and were skipped.')
    cannot += ['Band flips are a rule change, not accuracy; a flip only says which roofs the v2 rule would band differently.',
               'Rows keep the model_signature they were computed under; a scene re-inferred since carries a different signature.',
               f'Rows with population_scope other than owned_object_ids ({excluded}) are outside the owned scan population and excluded.',
               'Agreement is not accuracy; heights are class priors.']
    guard = protected_reference_checks(root)
    if not guard['ok']:
        failed = ', '.join(c['name'] for c in guard['checks'] if not c['ok']) or 'unknown'
        cannot.insert(0, f'protected reference checks failed ({failed}); results are not trustworthy until eyes_guard passes.')
    report = {**base, 'status': 'ok',
              'population': {'roofs': n, 'scenes': scenes, 'excluded_non_owned_rows': excluded, 'rows_read': len(rows), 'unreadable_rows': unreadable,
                             'model_signatures': {s: sorted(v) for s, v in sorted(signatures.items())},
                             'denominator': f'owned roofs with a {SHADOW_VERSION} row' + (f' in {len(scenes)} requested scene(s)' if scene_ids is not None else ' (every indexed scene)')},
              'metrics': {'flip_matrix': {'counts': dict(sorted(counts.items())), 'denominator': n, 'by_band_v1': dict(sorted(by_v1.items())),
                                          'by_band_v2': dict(sorted(by_v2.items())), 'flips': flips, 'flipped': nf, 'non_flipped': n-nf},
                          'reference_iou': {'tiers': REFERENCE_TIERS, 'rule': 'best (max) IoU over T1/T2 agreement rows with the row\'s model_signature',
                                            'flipped': _distribution(flipped_iou), 'non_flipped': _distribution(steady_iou),
                                            'signature_mismatched': mismatched, 'human_reference_ids': len(human)}},
              'claims': claims, 'cannot_claim': cannot, 'protected_reference_checks': guard}
    if write:
        path = out or _join_path(root, 'band_v2', scene_ids)
        report['path'] = str(path)
        _write(path, report)
    return report


def split_policy_report(root=None, *, counter=None, write=True, out=None):
    """Review-loop reach under frame vs core split from eyes_eligibility.count (read-only confidence index)."""
    root = _root(root)
    experiment = EXPERIMENTS['split_policy_core']
    base = {**_stamp(), 'experiment_id': 'split_policy_core', 'variable': experiment['variable'], 'mode': 'join',
            'candidate': experiment['candidate'], 'note': NOTE}
    guard = protected_reference_checks(root)
    try:
        counted = (counter or eyes_eligibility.count)(root)
    except FileNotFoundError as error:
        return {**base, 'status': 'unavailable', 'population': {}, 'metrics': {}, 'claims': [],
                'cannot_claim': [f'confidence index unavailable ({error}); eligibility cannot be counted.'],
                'protected_reference_checks': guard}
    d = counted['denominators']
    low = d['low_roofs']
    frame, core = counted['frame']['eligible'], counted['core']['eligible']
    held = counted.get('held_out_core_roofs', 0)
    def rate(v):
        return round(v/low, 4) if low else None
    claims = [f'core policy reaches {core} of {low} low-band owned scan roofs vs {frame} of {low} under the frame policy '
              f'(loop reach; opt-in; default batches unchanged).']
    cannot = ['Loop reach is a gate diagnostic, not accuracy; reaching a roof does not improve it.',
              f'{held} low roofs sit in held-out core tiles (validation/test) and stay protected under both policies; they never enter training.',
              'Eligibility counts describe the confidence index at computation time; they change as scenes are re-assessed.',
              'Agreement is not accuracy; heights are class priors.']
    if not guard['ok']:
        failed = ', '.join(c['name'] for c in guard['checks'] if not c['ok']) or 'unknown'
        cannot.insert(0, f'protected reference checks failed ({failed}); results are not trustworthy until eyes_guard passes.')
    report = {**base, 'status': 'ok',
              'population': {'owned_roofs': d['owned_roofs'], 'low_roofs': low, 'scenes_with_low_roofs': d['scenes_with_low_roofs'],
                             'scenes_unavailable': d['scenes_unavailable'], 'indexed_roofs': d['indexed_roofs'],
                             'profile_prefix': counted.get('profile_prefix'), 'denominator': counted.get('population')},
              'metrics': {'eligible': {'frame': frame, 'core': core}, 'eligible_delta': core-frame,
                          'reach_rate': {'frame': rate(frame), 'core': rate(core), 'denominator': low, 'population': 'low-band owned scan roofs'},
                          'reasons': {'frame': counted['frame']['reasons'], 'core': counted['core']['reasons']},
                          'by_core_split': counted.get('by_core_split'), 'held_out_core_roofs': held},
              'eligibility': counted, 'claims': claims, 'cannot_claim': cannot, 'protected_reference_checks': guard}
    if write:
        path = out or _join_path(root, 'split_policy_core', None)
        report['path'] = str(path)
        _write(path, report)
    return report


# --- CLI --------------------------------------------------------------------------------------------

def _print(value):
    json.dump(value, sys.stdout, sort_keys=True)
    sys.stdout.write('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=REPO)
    parser.add_argument('--plan', metavar='EXPERIMENT', choices=sorted(EXPERIMENTS), help='Write <out_root>/spec.json and print the spec.')
    parser.add_argument('--scenes', nargs='*', default=[], help='Scene ids (required with --plan; optional scope for --band-v2).')
    parser.add_argument('--sample', choices=SAMPLES, default='cluster')
    parser.add_argument('--run', type=Path, metavar='SPEC.json', help='Execute a planned experiment (real scene_isolated runner).')
    parser.add_argument('--allow-inference', action='store_true', help='Required for --run of a re-inference experiment (starts SAM3).')
    parser.add_argument('--summarise-isolated', type=Path, metavar='RUN_DIR', help='Lineage + paired comparison of an existing isolated run.')
    parser.add_argument('--band-v2', action='store_true', help='Shadow band v2 flip matrix joined to T1/T2 references (read-only join).')
    parser.add_argument('--split-policy', action='store_true', help='Review-loop reach under frame vs core split (read-only join).')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.plan:
            if not args.scenes:
                parser.error('--plan needs --scenes')
            spec = plan(args.plan, args.scenes, sample=args.sample, root=root)
            write_spec(spec)
            _print(spec)
            return 0
        if args.run:
            spec = json.loads(args.run.read_text())
            if EXPERIMENTS[spec['experiment_id']]['mode'] == 'reinference' and not args.allow_inference:
                sys.stderr.write('Refusing to start SAM3 re-inference: pass --allow-inference to run this experiment.\n')
                return 2
            _print(run(spec, root=root))
            return 0
        if args.summarise_isolated:
            _print(summarise_isolated(args.summarise_isolated, root=root))
            return 0
        if args.band_v2:
            _print(band_v2_report(root, args.scenes or None))
            return 0
        if args.split_policy:
            _print(split_policy_report(root))
            return 0
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    parser.print_help()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
