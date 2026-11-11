"""Confidence-tiered machine labels for building objects (``decision-policy-v2``).

For every owned building object in every scene under ``data/scenes`` (suburb scan tiles
and captures) the policy assembles the signals the eyes sidecars already hold (evidence
band and detector score, building class hint, structure sub-kind, roof form, stored
diagnostics, address allocation, cross-capture agreement) plus, for borderline roofs
only, a budgeted recompute of ``scene_roof.propose_roof`` (flat-form decision and
topology support). It turns them into one ``confidence`` in [0, 1] by the stated rule in
``THRESHOLDS`` and a ``tier``:

* ``auto``   - proceed with the machine label and assessment without a human;
* ``assist`` - show the machine label, a human glances at it;
* ``review`` - a human decides.

Human decisions (review.json statuses, roof family feedback, roof-line edits, T1/T2
references) are recorded in ``signals`` but never enter ``confidence``; ``--reflect``
compares the machine tiers against them. Confidence here is a versioned design rule, not
a calibrated probability; every share below is agreement with human reviewers who saw
what the queues surfaced, not accuracy on a random sample.

Commands (from the repository root)::

    python -m scripts.ml.decision_policy --run [--scenes ...] --report data/reviews/eyes/decision-policy/summary.json
    python -m scripts.ml.decision_policy --reflect --report data/reviews/eyes/decision-policy/reflection.json
    python -m scripts.ml.decision_policy --apply-auto --dry-run

Rows go to the eyes index table ``decision_policy`` (this version's rows replaced, other
versions untouched). Nothing under ``data/scenes`` is written unless ``--apply-auto`` is
run live with ``--i-understand-this-writes-review-json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import eyes_index

VERSION = 'decision-policy-v2'
TABLE = 'decision_policy'
NOTE = 'machine label and assessment; auto tier proceeds without a human, assist and review need one'
PROVENANCE = f'machine:{VERSION}'
REPORT_DIR = Path('data/reviews/eyes/decision-policy')
AUDIT_FILE = REPORT_DIR / 'apply-auto-live.json'
THRESHOLDS_FILE = REPORT_DIR / 'thresholds.json'   # approved proposal overrides; never edited by code without an approval
RECOMPUTE_DIR = REPORT_DIR / 'recompute'
TIERS = ('auto', 'assist', 'review')
HUMAN_STATUSES = ('confirmed', 'rejected', 'needs_adjustment', 'superseded', 'candidate')
PRIMARY_HINTS = frozenset({'house', 'commercial', 'units'})
SECONDARY_KINDS = frozenset({'garage', 'shed', 'outbuilding', 'carport', 'pergola-or-shade', 'shipping-container',
                             'container-stack', 'water-tank', 'portable-classroom', 'pool-cover'})
PITCHED_TYPES = frozenset({'hipped', 'gabled', 'mixed'})
HINT_VERSION = 'building-flags-v2'
STRUCTURE_VERSION = 'structure-kinds-v3'
STRUCTURE_VERSIONS = ('structure-kinds-v1', 'structure-kinds-v2', 'structure-kinds-v3')   # oldest first; newest wins per object
DIAGNOSTICS_VERSION = 'roof-diagnostics-v1'
ALLOCATION_VERSION = 'address-allocation-v1'
IMAGE_CUT_MESSAGE = 'Roof touches the image edge'

# Design values. Stated once, versioned, and never fitted on the evaluation set: --reflect may
# PROPOSE changes to these (proposals.json) but never applies them.
THRESHOLDS = {
    'version': 'decision-policy-thresholds-v2',
    # Primary roofs start from the evidence band of the frozen model (roof-evidence-v1).
    'band_base': {'high': .80, 'medium': .50, 'low': .25, 'unassessed': .10, 'none': .10},
    # Secondary structures start from the structure sub-kind confidence (structure-kinds-v2) instead:
    # a shed's assessment is its identity, not the roof-line evidence a house needs.
    'structure_base': {'strong': .80, 'moderate': .55, 'weak': .30, 'none': .30},
    # Detector score above/below 0.65 moves the confidence by up to this much (score - .65) * weight.
    'score_weight': .30,
    'class_hint': {'house': .05, 'units': .03, 'commercial': .03, 'outbuilding': .0, 'non-residential': -.05,
                   'tree-like': -.40, 'none': -.05},
    'structure_primary_bonus': {'strong': .05, 'moderate': .02, 'weak': .0, 'none': .0},   # a clear "not-secondary" for a primary roof
    'structure_conflict': -.25,      # primary class hint against a secondary sub-kind at moderate or strong confidence
    'fragment': -.35,                # a fragment is not a structure to label
    'allocation': {'matched:strong': .05, 'matched:moderate': .03, 'matched:weak': .0, 'suggested': -.03, 'shared': -.05,
                   'ambiguous': -.08, 'unmatched': -.10, 'none': -.05},
    'clipped': -.30,
    'geometry_failed': -.35,
    'confidence_stale': -.20,        # confidence.json signed by another model than latest.json
    'no_roof_model': -.10,           # no roof surface fitted (primary roofs only)
    'form_uncertain': -.05,
    'flat_disagreement': -.15,       # current image-only flat rule says flat, stored model is pitched (or the reverse)
    'type_disagreement': -.05,       # current fitter picks another roof type than stored
    'topology_simplified': -.05,     # a simpler image-supported plan exists: the stored plan is over-complex
    'outline_unsupported': -.10,     # > 30 % of the outline has no image edge under it
    'outline_unsupported_fraction': .30,
    'diagnostics_error': -.10,
    'cross_capture_agree': .03,
    'cross_capture_disagree': -.10,
    'auto_min': .80,
    'assist_min': .50,
    # Recompute (about 0.2 to 1.5 s per roof) only where it could change the tier.
    'recompute_window': .10,
    # score_interval: design uncertainty of the confidence point (a model estimate, never a probability):
    # half the gap between the base value used and its nearest neighbour in the same table, plus this much per missing sidecar row.
    'score_interval_missing_sidecar': .03,
    # Approved reflection proposals may add penalties per signal state ('band=medium': -0.10); empty by design.
    'signal_penalties': {},
}


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _set_path(mapping, path, value):
    node = mapping
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def load_overrides(root=Path('.')):
    file = Path(root) / THRESHOLDS_FILE
    if not file.exists():
        return []
    try:
        doc = json.loads(file.read_bytes())
    except ValueError:
        return []
    return [o for o in doc.get('overrides', []) if isinstance(o, dict) and isinstance(o.get('path'), list) and o['path']]


def effective_thresholds(root=Path('.')):
    """THRESHOLDS plus the approved overrides in thresholds.json; the version carries a hash suffix when any apply."""
    overrides = load_overrides(root)
    thresholds = json.loads(json.dumps(THRESHOLDS))
    if not overrides:
        return thresholds
    for override in overrides:
        _set_path(thresholds, [str(k) for k in override['path']], override.get('value'))
    digest = hashlib.sha256(json.dumps([[o['path'], o.get('value')] for o in overrides], sort_keys=True).encode()).hexdigest()[:8]
    thresholds['version'] = f"{THRESHOLDS['version']}+{digest}"
    thresholds['overrides'] = [{'path': o['path'], 'value': o.get('value'), 'proposal_id': o.get('proposal_id')} for o in overrides]
    return thresholds


def now():
    return datetime.now(UTC).isoformat()


def clamp(value):
    return max(0., min(1., round(float(value), 4)))


def scene_group(scene_id):
    """'suburb' for the scan tiles, 'captures' otherwise; the sub-group is the id prefix."""
    prefix = scene_id.split('-', 1)[0]
    if prefix == 'mooroolbark':
        return 'suburb', 'suburb'
    return 'captures', f'captures:{prefix}'


# --------------------------------------------------------------------------- decision rule

def _allocation_key(allocation):
    if not allocation or not allocation.get('status'):
        return None
    if allocation['status'] == 'matched':
        return f"matched:{allocation.get('confidence') or 'weak'}"
    return allocation['status']


def is_secondary_candidate(signals):
    structure = signals.get('structure') or {}
    return bool(structure.get('secondary_flag')) and structure.get('sub_kind') in SECONDARY_KINDS


def auto_gates(signals):
    """The user's minimum gates for the auto tier: {gate: passed}; a path name; the human decision never enters."""
    detector, hints = signals.get('detector') or {}, signals.get('class_hints') or {}
    structure, allocation = signals.get('structure') or {}, signals.get('allocation') or {}
    sub_kind, sub_conf = structure.get('sub_kind'), structure.get('confidence')
    secondary = is_secondary_candidate(signals)
    common = {'not_clipped': not detector.get('clipped'), 'not_tree_like': hints.get('class_hint') != 'tree-like',
              'not_fragment': sub_kind != 'fragment', 'confidence_current': not detector.get('stale')}
    if secondary:
        gates = {**common, 'structure_strong': sub_conf == 'strong', 'secondary_flag': bool(structure.get('secondary_flag'))}
        return 'secondary-structure', gates
    gates = {**common,
             'band_high': detector.get('band') == 'high',
             'class_hint_primary': hints.get('class_hint') in PRIMARY_HINTS,
             'no_conflicting_sub_kind': not (sub_kind in SECONDARY_KINDS and sub_conf in ('strong', 'moderate')),
             'allocation_matched': allocation.get('status') == 'matched' and allocation.get('confidence') in ('strong', 'moderate'),
             'geometry_passed': bool(detector.get('geometry_passed'))}
    return 'primary-roof', gates


def confidence_rule(signals, thresholds=THRESHOLDS):
    """Stated rule: base by band (primary) or structure confidence (secondary), then additive terms. Returns (value, reasons)."""
    t = thresholds
    detector, hints = signals.get('detector') or {}, signals.get('class_hints') or {}
    structure, allocation = signals.get('structure') or {}, signals.get('allocation') or {}
    roof, recompute = signals.get('roof') or {}, signals.get('recompute') or {}
    diagnostics, cross = signals.get('diagnostics') or {}, signals.get('cross_capture') or {}
    reasons = []
    secondary = is_secondary_candidate(signals)
    if secondary:
        value = t['structure_base'].get(structure.get('confidence') or 'none', t['structure_base']['none'])
        reasons.append(f"secondary structure: {structure.get('sub_kind')} ({structure.get('confidence') or 'no'} confidence) carries the assessment")
    else:
        value = t['band_base'].get(detector.get('band') or 'none', t['band_base']['none'])
        reasons.append(f"evidence band {detector.get('band') or 'unassessed'} sets the base")
    score = detector.get('score')
    if isinstance(score, (int, float)):
        value += (score - .65) * t['score_weight']
    hint = hints.get('class_hint')
    value += t['class_hint'].get(hint or 'none', t['class_hint']['none'])
    if hint == 'tree-like':
        reasons.append('class hint tree-like')
    sub_kind, sub_conf = structure.get('sub_kind'), structure.get('confidence')
    if not secondary:
        if sub_kind == 'not-secondary':
            value += t['structure_primary_bonus'].get(sub_conf or 'none', .0)
        if hint in PRIMARY_HINTS and sub_kind in SECONDARY_KINDS and sub_conf in ('strong', 'moderate'):
            value += t['structure_conflict']
            reasons.append(f'class hint {hint} conflicts with structure sub-kind {sub_kind} ({sub_conf})')
    if sub_kind == 'fragment':
        value += t['fragment']
        reasons.append('structure sub-kind fragment: not a whole structure')
    value += t['allocation'].get(_allocation_key(allocation) or 'none', t['allocation']['none'])
    if allocation.get('status') and allocation['status'] != 'matched':
        reasons.append(f"address allocation {allocation['status']}")
    if detector.get('clipped'):
        value += t['clipped']
        reasons.append('roof touches the image edge (image_cut)')
    if detector.get('band') is not None and detector.get('geometry_passed') is False:
        value += t['geometry_failed']
        reasons.append('geometry audit failed')
    if detector.get('stale'):
        value += t['confidence_stale']
        reasons.append('confidence.json was signed by another model than latest.json')
    if not secondary and roof.get('roof_type') is None:
        value += t['no_roof_model']
        reasons.append('no roof surface fitted')
    if roof.get('form_uncertain'):
        value += t['form_uncertain']
    if recompute.get('status') == 'ok':
        stored_form, recomputed_form = roof.get('roof_form'), recompute.get('roof_form')
        if stored_form and recomputed_form and stored_form != recomputed_form:
            value += t['flat_disagreement']
            reasons.append(f"current flat rule says {recomputed_form} (score {recompute.get('flat_score')}), stored model is {stored_form}")
        elif recompute.get('roof_type') and roof.get('roof_type') and recompute['roof_type'] != roof['roof_type']:
            value += t['type_disagreement']
            reasons.append(f"current fitter picks {recompute['roof_type']}, stored model is {roof['roof_type']}")
        support = recompute.get('topology_support') or {}
        if support.get('accepted'):
            value += t['topology_simplified']
            reasons.append(f"a simpler image-supported roof plan exists (accepted_by {support.get('accepted_by')})")
    if diagnostics.get('present'):
        if diagnostics.get('error'):
            value += t['diagnostics_error']
            reasons.append('stored diagnostics recorded a fitter error')
        fraction = diagnostics.get('outline_unsupported_fraction')
        if isinstance(fraction, (int, float)) and fraction > t['outline_unsupported_fraction']:
            value += t['outline_unsupported']
            reasons.append(f'{round(fraction * 100)} % of the outline has no image edge under it')
    if cross.get('n_pairs'):
        if cross.get('band_disagreement'):
            value += t['cross_capture_disagree']
            reasons.append('another capture of this roof disagrees on the band')
        else:
            value += t['cross_capture_agree']
            reasons.append(f"{cross['n_pairs']} other capture(s) agree on the band")
    penalties = t.get('signal_penalties') or {}
    if penalties:
        for state in signal_states({**signals, 'auto_gates': {'path': auto_gates(signals)[0]}}):
            if penalties.get(state):
                value += penalties[state]
                reasons.append(f'approved penalty {penalties[state]} for {state}')
    return clamp(value), reasons


def signal_states(signals):
    """'name=value' states the reflection confuses by and approved penalties key on."""
    s = signals
    recompute = s.get('recompute') or {}
    pairs = (('band', (s.get('detector') or {}).get('band')), ('class_hint', (s.get('class_hints') or {}).get('class_hint')),
             ('sub_kind', (s.get('structure') or {}).get('sub_kind')), ('sub_kind_confidence', (s.get('structure') or {}).get('confidence')),
             ('allocation', _allocation_key(s.get('allocation') or {})), ('clipped', (s.get('detector') or {}).get('clipped')),
             ('form_uncertain', (s.get('roof') or {}).get('form_uncertain')), ('roof_type', (s.get('roof') or {}).get('roof_type')),
             ('cross_capture_disagreement', (s.get('cross_capture') or {}).get('band_disagreement')),
             ('recompute_status', recompute.get('status')), ('topology_simplified', (recompute.get('topology_support') or {}).get('accepted')),
             ('auto_gate_path', (s.get('auto_gates') or {}).get('path')))
    return [f'{name}={value}' for name, value in pairs]


def machine_label(signals):
    roof, hints, structure = signals.get('roof') or {}, signals.get('class_hints') or {}, signals.get('structure') or {}
    return {'kind': 'building', 'structure_sub_kind': structure.get('sub_kind'), 'class_hint': hints.get('class_hint'),
            'roof_form': roof.get('roof_form'), 'roof_type': roof.get('roof_type')}


def decide(signals, thresholds=THRESHOLDS, *, computed_at=None, code=None):
    """One decision payload from an assembled signals dict; pure, no I/O. Human signals are carried, not used."""
    value, reasons = confidence_rule(signals, thresholds)
    path, gates = auto_gates(signals)
    failed = [name for name, passed in gates.items() if not passed]
    if value >= thresholds['auto_min'] and not failed:
        tier = 'auto'
        reasons.append(f'auto: every {path} gate passed and confidence {value} >= {thresholds["auto_min"]}')
    elif value >= thresholds['assist_min']:
        tier = 'assist'
        if failed:
            reasons.append(f"assist: {path} gates failed: {', '.join(failed)}")
        else:
            reasons.append(f'assist: confidence {value} below the auto minimum {thresholds["auto_min"]}')
    else:
        tier = 'review'
        reasons.append(f'review: confidence {value} below {thresholds["assist_min"]}')
    reasons.append('human decisions and references are recorded in signals and never enter the confidence')
    payload = {'version': VERSION, 'note': NOTE, 'tier': tier, 'confidence': value, 'machine_label': machine_label(signals),
            'reasons': reasons, 'signals': {**signals, 'auto_gates': {'path': path, 'gates': gates}},
            'thresholds_version': thresholds['version'], 'computed_at': computed_at or now(), 'code_sha256': code or code_sha256()}
    payload['interval'] = interval_for(payload, {})
    payload['score_interval'] = score_interval(payload, thresholds)
    return payload


def borderline(payload, thresholds=THRESHOLDS):
    """Recompute candidates: primary roofs whose confidence sits within the window of a tier boundary, or auto candidates."""
    if is_secondary_candidate(payload['signals']):
        return False
    value, window = payload['confidence'], thresholds['recompute_window']
    if payload['tier'] == 'auto':
        return True
    return abs(value - thresholds['auto_min']) <= window or abs(value - thresholds['assist_min']) <= window


# --------------------------------------------------------------------------- intervals on labels

INTERVAL_NOTE = 'human agreement for this stratum, not accuracy; null when nobody has judged it yet'
SCORE_INTERVAL_NOTE = ('model estimate from the design rule: the confidence point plus or minus half the gap to the neighbouring base value '
                       'in its table, widened by 0.03 per missing sidecar row; not a probability, not accuracy')


def label_kind(payload):
    """Stratum label kind: the structure sub-kind for secondary structures, else primary-roof/<class hint>."""
    label = payload['machine_label']
    if payload['signals']['auto_gates']['path'] == 'secondary-structure' and label['structure_sub_kind']:
        return label['structure_sub_kind']
    return f"primary-roof/{label['class_hint']}"


def stratum_of(payload):
    return f"{payload['tier']}/{label_kind(payload)}"


def human_judgement(human, reference):
    """(judged, confirmed) from a review.json decision and/or a T1/T2 reference; machine rows never reach here."""
    status = human['status'] if human else None
    tier = reference.get('tier') if reference else None
    judged = status in ('confirmed', 'rejected', 'needs_adjustment') or tier in ('T1', 'T2')
    confirmed = status == 'confirmed' or tier == 'T1'
    return judged, confirmed


def strata_counts(payloads, humans):
    """{stratum: {'confirmed': n, 'judged': N}} from the reflection join (review.json decisions plus T1/T2 references)."""
    by_key = {(h['scene_id'], h['object_id']): h for h in humans if h.get('kind') == 'building'}
    counts = defaultdict(lambda: {'confirmed': 0, 'judged': 0})
    for payload in payloads:
        s = payload['signals']
        judged, confirmed = human_judgement(by_key.get((s['scene']['scene_id'], s['object_id'])), s.get('reference'))
        if judged:
            entry = counts[stratum_of(payload)]
            entry['judged'] += 1
            entry['confirmed'] += int(confirmed)
    return dict(counts)


def interval_for(payload, strata):
    stratum = stratum_of(payload)
    counts = strata.get(stratum) or {'confirmed': 0, 'judged': 0}
    bounds = wilson(counts['confirmed'], counts['judged']) if counts['judged'] else None
    return {'kind': 'wilson-95', 'stratum': stratum, 'confirmed': counts['confirmed'], 'judged': counts['judged'],
            'lower': round(bounds[0], 4) if bounds else None, 'upper': round(bounds[1], 4) if bounds else None, 'note': INTERVAL_NOTE}


def score_interval(payload, thresholds=THRESHOLDS):
    s = payload['signals']
    if is_secondary_candidate(s):
        table, basis, used = thresholds['structure_base'], 'structure', (s.get('structure') or {}).get('confidence') or 'none'
    else:
        table, basis, used = thresholds['band_base'], 'band', (s.get('detector') or {}).get('band') or 'none'
    value = table.get(used, table['none'])
    others = [abs(value - v) for k, v in table.items() if k != used and v != value]
    width = (min(others) / 2 if others else .1)
    missing = sum(1 for name in ('class_hints', 'structure', 'allocation') if not (s.get(name) or {}).get('version'))
    width = round(width + missing * thresholds['score_interval_missing_sidecar'], 4)
    point = payload['confidence']
    return {'kind': 'design-range', 'point': point, 'lower': clamp(point - width), 'upper': clamp(point + width), 'width': width,
            'basis': basis, 'missing_sidecars': missing, 'note': SCORE_INTERVAL_NOTE}


def attach_intervals(payloads, humans, thresholds=THRESHOLDS):
    strata = strata_counts(payloads, humans)
    for payload in payloads:
        payload['interval'] = interval_for(payload, strata)
        payload['score_interval'] = score_interval(payload, thresholds)
    return strata


# --------------------------------------------------------------------------- loading signals

def _json_list(text, size):
    try:
        values = json.loads(text) if text is not None else None
    except ValueError:
        values = None
    return values if isinstance(values, list) and len(values) == size else [None] * size


def load_index_signals(root):
    """One read of the eyes index: class hints, structure sub-kinds, diagnostics, allocation, cross-capture, references."""
    out = {'hints': {}, 'structure': {}, 'diagnostics': {}, 'allocation': {}, 'cross': defaultdict(list), 'references': {}}
    if not eyes_index.path(root).exists():
        return out
    with closing(eyes_index.connection(root, readonly=True)) as db:
        db.execute('PRAGMA busy_timeout=2000')
        for sid, oid, hint, flags, score in db.execute(
                "SELECT scene_id, object_id, json_extract(payload,'$.class_hints.class_hint'), json_extract(payload,'$.class_hints.flags'), "
                "json_extract(payload,'$.score') FROM object_quality WHERE version=?", (HINT_VERSION,)):
            try:
                flag_names = [f.get('flag') for f in json.loads(flags or '[]') if isinstance(f, dict)]
            except ValueError:
                flag_names = []
            out['hints'][(sid, oid)] = {'class_hint': hint, 'flags': flag_names, 'score': score, 'version': HINT_VERSION}
        # Oldest version first so the newest row for an object wins; a scene recomputed under v3 keeps v2 rows for the rest.
        for version in STRUCTURE_VERSIONS:
            for sid, oid, sub_kind, confidence, flag, rule in db.execute(
                    "SELECT scene_id, object_id, json_extract(payload,'$.sub_kind'), json_extract(payload,'$.confidence'), "
                    "json_extract(payload,'$.eligibility.secondary_structure_flag'), json_extract(payload,'$.rule') FROM object_quality WHERE version=?",
                    (version,)):
                out['structure'][(sid, oid)] = {'sub_kind': sub_kind, 'confidence': confidence, 'secondary_flag': bool(flag), 'rule': rule,
                                                'version': version}
        paths = ('$.audit.passed', '$.error', '$.recomputed_matches_stored', '$.form_uncertain', '$.crease_contrast.share_ge_threshold',
                 '$.edge_support.original.unsupported_fraction', '$.roof_type_recomputed', '$.topology.support.accepted',
                 '$.topology.support.selected.accepted_by', '$.form_evidence.decision', '$.form_evidence.score')
        for sid, oid, signature, values in db.execute(
                f"SELECT scene_id, object_id, model_signature, json_extract(payload, {','.join(repr(p) for p in paths)}) "
                "FROM roof_diagnostics WHERE version=?", (DIAGNOSTICS_VERSION,)):
            passed, error, matches, uncertain, crease, unsupported, recomputed, sup_acc, sup_by, form_decision, form_score = _json_list(values, len(paths))
            out['diagnostics'][(sid, oid)] = {'present': True, 'version': DIAGNOSTICS_VERSION, 'model_signature': signature,
                                              'audit_passed': passed, 'error': error, 'recomputed_matches_stored': matches,
                                              'form_uncertain': uncertain, 'crease_contrast_share': crease,
                                              'outline_unsupported_fraction': unsupported, 'roof_type_recomputed': recomputed,
                                              'topology_support_accepted': sup_acc, 'topology_support_accepted_by': sup_by,
                                              'form_decision': form_decision, 'form_score': form_score}
        for sid, oid, values in db.execute(
                "SELECT scene_id, object_id, json_extract(payload,'$.status','$.confidence','$.method','$.full_address','$.address_id','$.evidence.secondary') "
                "FROM address_allocation WHERE version=?", (ALLOCATION_VERSION,)):
            status, confidence, method, address, address_id, secondary = _json_list(values, 6)
            out['allocation'][(sid, oid)] = {'status': status, 'confidence': confidence, 'method': method, 'full_address': address,
                                             'address_id': address_id, 'secondary': secondary, 'version': ALLOCATION_VERSION}
        for sid, oid, osid, ooid, values in db.execute(
                "SELECT scene_id, object_id, other_scene_id, other_object_id, json_extract(payload,'$.iou','$.band','$.other_band','$.band_disagreement','$.hidden_by') "
                'FROM cross_capture'):
            iou, band, other_band, disagreement, hidden_by = _json_list(values, 5)
            out['cross'][(sid, oid)].append({'other': f'{osid}/{ooid}', 'other_band': other_band, 'iou': iou,
                                             'band_disagreement': bool(disagreement), 'hidden': hidden_by == 'other'})
            out['cross'][(osid, ooid)].append({'other': f'{sid}/{oid}', 'other_band': band, 'iou': iou,
                                               'band_disagreement': bool(disagreement), 'hidden': hidden_by == 'self'})
        for ref_id, tier, sid, oid, values in db.execute(
                "SELECT ref_id, tier, scene_id, object_id, json_extract(payload,'$.provenance','$.status','$.roof_type') "
                'FROM "references" WHERE tier IN (\'T1\',\'T2\')'):
            provenance, status, roof_type = _json_list(values, 3)
            current = out['references'].get((sid, oid))
            if current is None or tier < current['tier']:
                out['references'][(sid, oid)] = {'ref_id': ref_id, 'tier': tier, 'provenance': provenance, 'status': status, 'roof_type': roof_type}
    return out


def load_suburb_clipped(root):
    """{(scene, object): clipped} from the suburb confidence.sqlite payloads when present."""
    file = Path(root) / 'data/suburbs/mooroolbark/confidence.sqlite'
    if not file.exists():
        return {}
    try:
        with closing(sqlite3.connect(f'file:{file}?mode=ro', uri=True)) as db:
            return {(sid, oid): bool(clipped) for sid, oid, clipped in db.execute(
                "SELECT scene_id, object_id, json_extract(payload,'$.properties.clipped') FROM roofs")}
    except sqlite3.Error:
        return {}


def load_confidence(folder, model_signature):
    """Per-object band rows from confidence.json; stale when it was signed by another model."""
    file = folder / 'confidence.json'
    if not file.exists():
        return {}, None
    try:
        report = json.loads(file.read_bytes())
    except ValueError:
        return {}, None
    stale = report.get('model_signature') != model_signature
    rows = {}
    for row in report.get('objects', []):
        reasons = row.get('reasons') or []
        rows[row['object_id']] = {'band': row.get('band'), 'score': row.get('detector_score'), 'geometry_passed': row.get('geometry_passed'),
                                  'clipped': any(isinstance(r, str) and r.startswith(IMAGE_CUT_MESSAGE) for r in reasons),
                                  'image_line_mismatch_m': row.get('image_line_mismatch_m'), 'observed_segments': row.get('observed_segments'),
                                  'stale': stale, 'source': 'confidence.json', 'version': report.get('version')}
    return rows, stale


def human_decisions(root, scene_ids=None):
    """Every review.json object decision (all kinds), read-only; the reflection's human side."""
    out = []
    for folder in sorted((Path(root) / 'data/scenes').iterdir()):
        if scene_ids is not None and folder.name not in scene_ids:
            continue
        file = folder / 'review.json'
        if not file.exists():
            continue
        try:
            review = json.loads(file.read_bytes())
            latest = json.loads((folder / 'latest.json').read_bytes()) if (folder / 'latest.json').exists() else {}
        except ValueError:
            continue
        same = review.get('model_signature') == latest.get('model_signature')
        for obj in review.get('objects', []):
            if not isinstance(obj, dict) or 'id' not in obj or is_machine_labelled(obj):
                continue
            feedback = obj.get('roof_feedback') if isinstance(obj.get('roof_feedback'), dict) else {}
            out.append({'scene_id': folder.name, 'object_id': obj['id'], 'kind': obj.get('kind'), 'status': obj.get('status'),
                        'geometry_reviewed': bool(obj.get('geometry_reviewed')), 'family': feedback.get('family'),
                        'roof_line_edits': bool(obj.get('roof_line_edits')), 'roof_type': obj.get('roof_type'),
                        'label': obj.get('label'), 'same_model_signature': same, 'provenance': obj.get('provenance')})
    return out


def has_machine_provenance(obj):
    provenance = obj.get('provenance')
    source = provenance.get('source') if isinstance(provenance, dict) else provenance
    return isinstance(source, str) and source.startswith('machine:')


def is_machine_labelled(obj):
    """True when a review object still carries the machine's own decision (machine provenance and status confirmed).

    A human who changes the status afterwards turns it into a human decision; a human who confirms it as-is is
    indistinguishable from the machine write and is therefore never counted as agreement.
    """
    provenance = obj.get('provenance') if isinstance(obj.get('provenance'), dict) else {}
    if provenance.get('human_confirmed') is True:
        return False      # the editor stamps a reviewer's Looks right on a machine label; that is a human confirmation
    return has_machine_provenance(obj) and obj.get('status') == 'confirmed'


def roof_signals(obj):
    roof_type = obj.get('roof_type')
    note = obj.get('roof_geometry_note') or ''
    form = 'flat' if roof_type == 'flat' or note.startswith('Flat roof') else 'pitched' if roof_type in PITCHED_TYPES else None
    return {'roof_type': roof_type, 'roof_form': form, 'roof_geometry_source': obj.get('roof_geometry_source'),
            'form_uncertain': 'form is uncertain' in note, 'note_flat': note.startswith('Flat roof'),
            'roof_fitted': bool(obj.get('roof_annotation')), 'area_m2': (obj.get('model') or {}).get('area_m2'),
            'height_m': (obj.get('model') or {}).get('height_m')}


def assemble(scene_id, scene, obj, index, confidence_rows, clipped_lookup, review_lookup):
    key = (scene_id, obj['id'])
    group, subgroup = scene_group(scene_id)
    detector = dict(confidence_rows.get(obj['id']) or {'band': None, 'score': obj.get('score'), 'geometry_passed': None, 'clipped': False,
                                                      'stale': False, 'source': None, 'version': None})
    if key in clipped_lookup:
        detector['clipped'] = detector['clipped'] or clipped_lookup[key]
        detector['clipped_source'] = 'confidence.sqlite'
    if detector.get('score') is None:
        detector['score'] = obj.get('score')
    hints = index['hints'].get(key) or {'class_hint': None, 'flags': [], 'score': obj.get('score'), 'version': None}
    structure = index['structure'].get(key) or {'sub_kind': None, 'confidence': None, 'secondary_flag': 'secondary-structure' in hints['flags'],
                                                'rule': None, 'version': None}
    structure['secondary_flag'] = bool(structure.get('secondary_flag')) or 'secondary-structure' in hints['flags']
    diagnostics = index['diagnostics'].get(key) or {'present': False}
    if diagnostics.get('present') and diagnostics.get('model_signature') not in (None, scene.get('model_signature')):
        diagnostics = {**diagnostics, 'present': False, 'stale': True}
    pairs = index['cross'].get(key) or []
    cross = {'n_pairs': len(pairs), 'band_disagreement': any(p['band_disagreement'] for p in pairs),
             'hidden_by_other': any(p['hidden'] for p in pairs), 'pairs': pairs[:6]}
    human = review_lookup.get(key)
    human_provenance = (human or {}).get('provenance') if isinstance((human or {}).get('provenance'), dict) else {}
    review = {'present': human is not None, 'status': human['status'] if human else None,
              'human_confirmed_machine_label': bool(human_provenance.get('human_confirmed')),
              'geometry_reviewed': human['geometry_reviewed'] if human else False, 'family': human['family'] if human else None,
              'roof_line_edits': human['roof_line_edits'] if human else False,
              'same_model_signature': human['same_model_signature'] if human else None,
              'live': not (human and human['same_model_signature'] and human['status'] in ('rejected', 'superseded'))}
    return {'scene': {'scene_id': scene_id, 'group': group, 'subgroup': subgroup, 'model_signature': scene.get('model_signature'),
                      'owned': obj['id'] in set(scene.get('owned_object_ids', [o['id'] for o in scene['objects']])), 'title': scene.get('title')},
            'object_id': obj['id'], 'detector': detector, 'class_hints': hints, 'structure': structure, 'roof': roof_signals(obj),
            'diagnostics': diagnostics, 'allocation': index['allocation'].get(key) or {'status': None, 'confidence': None},
            'cross_capture': cross, 'review': review, 'reference': index['references'].get(key), 'recompute': None}


# --------------------------------------------------------------------------- budgeted recompute

def recompute_key(scene, obj):
    from .scene_roof_diagnostics import fitter_sha256
    return hashlib.sha256(json.dumps([scene.get('image_sha256'), obj.get('ring'), obj.get('holes'), (obj.get('model') or {}).get('height_m'),
                                      fitter_sha256()], sort_keys=True).encode()).hexdigest()


def recompute(scene, image, obj, context_hint=None):
    """Fresh propose_roof on the saved outline: flat-form decision/score, roof type and topology support (accepted_by)."""
    import numpy as np

    from .scene_roof import propose_roof
    frame = np.asarray(scene['pixel_to_meters'])

    def local(ring):
        return (np.c_[ring, np.ones(len(ring))] @ frame).tolist()
    started = time.perf_counter()
    try:
        proposal = propose_roof(local(obj['ring']), obj['model']['height_m'], 2., 'auto', image=image, meters_to_pixel=scene['meters_to_pixel'],
                                holes=[local(h) for h in obj.get('holes', [])], **({} if context_hint is None else {'context_hint': context_hint}))
    except Exception as error:  # noqa: BLE001 - the recompute records, never hides, fitter failures
        return {'status': 'error', 'error': f'{type(error).__name__}: {error}'[:200], 'elapsed_seconds': round(time.perf_counter() - started, 3)}
    form = proposal.get('form_evidence') or {}
    topology = proposal.get('topology') or {}
    support = topology.get('support') or {}
    selected = support.get('selected') or {}
    roof_type = proposal.get('roof_type')
    return {'status': 'ok', 'roof_type': roof_type, 'roof_form': 'flat' if roof_type == 'flat' else 'pitched' if roof_type in PITCHED_TYPES else None,
            'form_uncertain': bool(proposal.get('form_uncertain')), 'flat_version': form.get('version'), 'flat_decision': form.get('decision'),
            'flat_score': form.get('score'), 'flat_rule': form.get('rule'), 'context_hint': context_hint,
            'topology': {'accepted': topology.get('accepted'), 'reason': topology.get('reason'), 'method': topology.get('method')},
            'topology_support': {'method': support.get('method'), 'accepted': bool(support.get('accepted')), 'reason': support.get('reason'),
                                 'accepted_by': selected.get('accepted_by'), 'shape': selected.get('shape')},
            'audit_passed': (proposal.get('audit') or {}).get('passed'), 'elapsed_seconds': round(time.perf_counter() - started, 3)}


def cached_recompute(root, scene_id, scene, image, obj, context_hint):
    key = recompute_key(scene, obj)
    file = Path(root) / RECOMPUTE_DIR / scene_id / f"{obj['id']}.json"
    if file.exists():
        try:
            cached = json.loads(file.read_bytes())
            if cached.get('key') == key:
                return {**cached['result'], 'cached': True}
        except ValueError:
            pass
    result = recompute(scene, image, obj, context_hint)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({'key': key, 'scene_id': scene_id, 'object_id': obj['id'], 'computed_at': now(), 'result': result}, sort_keys=True))
    return {**result, 'cached': False}


# --------------------------------------------------------------------------- run

def scene_ids_under(root, wanted=None):
    folder = Path(root) / 'data/scenes'
    if not folder.exists():
        return []
    ids = sorted(p.name for p in folder.iterdir() if (p / 'latest.json').exists())
    if wanted:
        missing = sorted(set(wanted) - set(ids))
        if missing:
            raise ValueError(f'Unknown scenes: {missing}')
        ids = [s for s in ids if s in set(wanted)]
    return ids


def replace_version(db, rows, version=VERSION):
    """Replace-by-version: delete this version's rows, insert the new ones; other versions are untouched."""
    eyes_index.ensure_schema(db)
    db.execute(f'DELETE FROM "{TABLE}" WHERE version=?', (version,))
    db.commit()
    return eyes_index.replace_rows(db, TABLE, [{'scene_id': r['signals']['scene']['scene_id'], 'object_id': r['signals']['object_id'],
                                                'version': version, 'payload': r} for r in rows])


def tier_counts(payloads):
    counts = {'overall': Counter(), 'groups': defaultdict(Counter), 'live': Counter(), 'by_band': defaultdict(Counter),
              'by_path': defaultdict(Counter)}
    for payload in payloads:
        s = payload['signals']
        counts['overall'][payload['tier']] += 1
        counts['groups'][s['scene']['group']][payload['tier']] += 1
        if s['scene']['subgroup'] != s['scene']['group']:
            counts['groups'][s['scene']['subgroup']][payload['tier']] += 1
        if s['review']['live']:
            counts['live'][payload['tier']] += 1
        counts['by_band'][str(s['detector'].get('band'))][payload['tier']] += 1
        counts['by_path'][s['auto_gates']['path']][payload['tier']] += 1

    def full(counter):
        return {**{t: counter.get(t, 0) for t in TIERS}, 'total': sum(counter.values())}
    return {'overall': full(counts['overall']), 'live': full(counts['live']),
            'groups': {g: full(c) for g, c in sorted(counts['groups'].items())},
            'by_band': {b: full(c) for b, c in sorted(counts['by_band'].items())},
            'by_path': {p: full(c) for p, c in sorted(counts['by_path'].items())}}


def run(root=Path('.'), scene_ids=None, *, recompute_budget_seconds=720., max_recompute=600, thresholds=None, log=None):
    """Assemble, decide, recompute borderline primary roofs within budget, write the index rows; returns the summary."""
    root = Path(root)
    thresholds = thresholds or effective_thresholds(root)
    started = time.perf_counter()
    ids = scene_ids_under(root, scene_ids)
    index = load_index_signals(root)
    clipped_lookup = load_suburb_clipped(root)
    review_lookup = {(h['scene_id'], h['object_id']): h for h in human_decisions(root, set(ids))}
    computed_at, code = now(), code_sha256()
    payloads, per_scene, stale_confidence = [], {}, []
    for scene_id in ids:
        folder = root / 'data/scenes' / scene_id
        try:
            scene = json.loads((folder / 'latest.json').read_bytes())
        except ValueError:
            continue
        confidence_rows, stale = load_confidence(folder, scene.get('model_signature'))
        if stale:
            stale_confidence.append(scene_id)
        owned = set(scene.get('owned_object_ids', [o['id'] for o in scene['objects']]))
        scene_payloads = []
        for obj in scene['objects']:
            if obj.get('kind') != 'building' or obj['id'] not in owned:
                continue
            signals = assemble(scene_id, scene, obj, index, confidence_rows, clipped_lookup, review_lookup)
            scene_payloads.append((obj, decide(signals, thresholds, computed_at=computed_at, code=code)))
        per_scene[scene_id] = (scene, scene_payloads)
    # Budgeted recompute, most borderline first, primary roofs only.
    candidates = []
    for scene_id, (_scene, scene_payloads) in per_scene.items():
        for obj, payload in scene_payloads:
            if borderline(payload, thresholds):
                distance = 0. if payload['tier'] == 'auto' else min(abs(payload['confidence'] - thresholds['auto_min']),
                                                                    abs(payload['confidence'] - thresholds['assist_min']))
                candidates.append((distance, scene_id, obj['id']))
    candidates.sort()
    recompute_report = {'candidates': len(candidates), 'recomputed': 0, 'cached': 0, 'errors': 0, 'skipped_budget': 0,
                        'budget_seconds': recompute_budget_seconds, 'max_recompute': max_recompute, 'tier_changes': Counter()}
    loaded = {}
    budget_start = time.perf_counter()
    for i, (_, scene_id, object_id) in enumerate(candidates):
        if i >= max_recompute or time.perf_counter() - budget_start > recompute_budget_seconds:
            recompute_report['skipped_budget'] = len(candidates) - i
            break
        scene, scene_payloads = per_scene[scene_id]
        if scene_id not in loaded:
            try:
                from .scene_roof_diagnostics import load_scene
                loaded[scene_id] = load_scene(scene_id, root)[1]
            except (OSError, ValueError, KeyError) as error:
                loaded[scene_id] = error
        image = loaded[scene_id]
        for position, (obj, payload) in enumerate(scene_payloads):
            if obj['id'] != object_id:
                continue
            if isinstance(image, Exception):
                result = {'status': 'error', 'error': f'scene: {image}'[:200]}
            else:
                result = cached_recompute(root, scene_id, scene, image, obj, payload['signals']['class_hints'].get('class_hint'))
            recompute_report['recomputed' if not result.get('cached') else 'cached'] += 1
            if result.get('status') == 'error':
                recompute_report['errors'] += 1
            signals = {k: v for k, v in payload['signals'].items() if k != 'auto_gates'}
            signals['recompute'] = result
            updated = decide(signals, thresholds, computed_at=computed_at, code=code)
            if updated['tier'] != payload['tier']:
                recompute_report['tier_changes'][f"{payload['tier']}->{updated['tier']}"] += 1
            scene_payloads[position] = (obj, updated)
            if log:
                log(json.dumps({'recompute': f'{scene_id}/{object_id}', 'tier': f"{payload['tier']}->{updated['tier']}",
                                'elapsed': result.get('elapsed_seconds'), 'cached': result.get('cached')}))
    for _, scene_payloads in per_scene.values():
        payloads.extend(p for _, p in scene_payloads)
    strata = attach_intervals(payloads, list(review_lookup.values()), thresholds)
    with closing(eyes_index.connection(root)) as db:
        written = replace_version(db, payloads)
    recompute_report['tier_changes'] = dict(recompute_report['tier_changes'])
    summary = {'version': VERSION, 'thresholds': thresholds, 'code_sha256': code, 'computed_at': computed_at,
               'note': NOTE + '; counts are tier assignments by a design rule, not accuracy',
               'scenes': len(per_scene), 'objects': len(payloads), 'rows_written': written, 'stale_confidence_scenes': stale_confidence,
               'tiers': tier_counts(payloads), 'recompute': recompute_report, 'strata': strata,
               'intervals': {'labels_with_judged_strata': sum(1 for p in payloads if p['interval']['judged']),
                             'labels_without_judged_strata': sum(1 for p in payloads if not p['interval']['judged']),
                             'auto_with_judged_strata': sum(1 for p in payloads if p['tier'] == 'auto' and p['interval']['judged'])},
               'auto_objects': [{'scene_id': p['signals']['scene']['scene_id'], 'object_id': p['signals']['object_id'], 'confidence': p['confidence'],
                                 'path': p['signals']['auto_gates']['path'], 'machine_label': p['machine_label']}
                                for p in payloads if p['tier'] == 'auto'],
               'elapsed_seconds': round(time.perf_counter() - started, 1)}
    return summary


# --------------------------------------------------------------------------- reflection

def wilson(successes, total):
    from .scene_confidence import wilson as _wilson
    return _wilson(successes, total)


def load_decisions(root, version=VERSION):
    if not eyes_index.path(root).exists():
        return []
    with closing(eyes_index.connection(root, readonly=True)) as db:
        try:
            return [r['payload'] for r in eyes_index.rows(db, TABLE, version=version) if isinstance(r['payload'], dict)]
        except sqlite3.Error:
            return []


def _share(successes, total):
    return {'n': total, 'confirmed': successes, 'share': round(successes / total, 4) if total else None,
            'wilson_95': [round(v, 4) for v in wilson(successes, total)] if total else None}


def reflect(payloads, humans, thresholds=THRESHOLDS):
    """Machine tiers against every human decision that exists. Agreement, not accuracy; the sample is what the queues surfaced."""
    by_key = {(p['signals']['scene']['scene_id'], p['signals']['object_id']): p for p in payloads}
    inventory = defaultdict(Counter)
    for h in humans:
        inventory[h['kind'] or 'unknown'][h['status'] or 'unknown'] += 1
    decided = [h for h in humans if h['status'] in ('confirmed', 'rejected', 'needs_adjustment', 'superseded')]
    matched = [(h, by_key[(h['scene_id'], h['object_id'])]) for h in decided if (h['scene_id'], h['object_id']) in by_key]
    unmatched = Counter((h['kind'], h['status']) for h in decided if (h['scene_id'], h['object_id']) not in by_key)
    per_tier = {}
    for tier in TIERS:
        rows = [(h, p) for h, p in matched if p['tier'] == tier]
        counts = Counter(h['status'] for h, _ in rows)
        judged = counts['confirmed'] + counts['rejected'] + counts['needs_adjustment']
        per_tier[tier] = {'status_counts': dict(counts), 'judged': judged, 'confirmed_share': _share(counts['confirmed'], judged),
                          'disagreement_share': _share(counts['rejected'] + counts['needs_adjustment'], judged),
                          'superseded_excluded': counts['superseded'], 'stale_signature': sum(1 for h, _ in rows if not h['same_model_signature']),
                          'geometry_reviewed': sum(1 for h, _ in rows if h['geometry_reviewed']),
                          'band_unassessed': sum(1 for _, p in rows if p['signals']['detector'].get('band') is None)}
    shares = {t: per_tier[t]['confirmed_share']['share'] for t in TIERS}
    known = {t: s for t, s in shares.items() if s is not None}
    ordering = {'auto_highest_confirmed_share': (shares['auto'] is not None and all(shares['auto'] >= s for s in known.values()))
                if known else None, 'shares': shares,
                'note': 'the auto tier must have the highest confirmed share; None means no human decision fell in that tier'}
    # Confusion by signal: which signal states accompany a rejected/needs_adjustment auto or assist decision.
    states = defaultdict(lambda: Counter())
    for h, p in matched:
        if p['tier'] not in ('auto', 'assist') or h['status'] not in ('confirmed', 'rejected', 'needs_adjustment'):
            continue
        s = p['signals']
        outcome = 'agree' if h['status'] == 'confirmed' else 'disagree'
        for state in signal_states(s):
            states[state][outcome] += 1
    confusion = sorted(({'signal': k, 'disagree': c['disagree'], 'agree': c['agree'],
                         'disagree_share': round(c['disagree'] / (c['disagree'] + c['agree']), 4)} for k, c in states.items()),
                       key=lambda r: (-r['disagree'], -r['disagree_share'], r['signal']))
    # Calibration bins of confidence vs human confirmation.
    bins = []
    for lower in range(0, 10):
        low, high = lower / 10, (lower + 1) / 10
        rows = [h for h, p in matched if h['status'] in ('confirmed', 'rejected', 'needs_adjustment') and low <= p['confidence'] < high + (1e-9 if lower == 9 else 0)]
        confirmed = sum(1 for h in rows if h['status'] == 'confirmed')
        bins.append({'bin': [low, high], **_share(confirmed, len(rows))})
    # Roof family labels vs the machine roof type; roof-line edits; references.
    family_rows = [h for h in humans if h['family']]
    family = Counter()
    for h in family_rows:
        p = by_key.get((h['scene_id'], h['object_id']))
        machine = p['machine_label']['roof_type'] if p else h['roof_type']
        family['with_decision_row' if p else 'review_object_roof_type_only'] += 1
        family['agree' if machine == h['family'] else 'disagree' if machine else 'no_machine_type'] += 1
    edits = [(h, by_key.get((h['scene_id'], h['object_id']))) for h in humans if h['roof_line_edits']]
    references = Counter()
    for p in payloads:
        ref = p['signals'].get('reference')
        if ref:
            references[f"{ref['tier']}:{p['tier']}"] += 1
    unassessed_judged = sum(t['band_unassessed'] for t in per_tier.values())
    proposals = propose(per_tier, confusion, bins, thresholds, unassessed_judged=unassessed_judged)
    return {'version': VERSION, 'thresholds_version': thresholds['version'], 'computed_at': now(), 'code_sha256': code_sha256(),
            'note': 'agreement between machine tiers and human review decisions; not accuracy',
            'selection_bias': ('Humans reviewed the roofs the queues surfaced (training captures, flagged roofs, calibration samples), '
                               'not a random sample of the inventory; shares describe that sample only.'),
            'human_inventory': {k: dict(v) for k, v in sorted(inventory.items())},
            'decisions_total': len(decided), 'decisions_matched_to_building_rows': len(matched),
            'decisions_without_machine_row': {f'{k}:{s}': n for (k, s), n in sorted(unmatched.items())},
            'per_tier': per_tier, 'ordering': ordering, 'confusion_by_signal': confusion[:40], 'calibration_bins': bins,
            'roof_family': {'labels': len(family_rows), **dict(family)},
            'roof_line_edits': {'count': len(edits), 'tiers': dict(Counter(p['tier'] if p else 'no_row' for _, p in edits))},
            'references_by_tier': dict(references), 'proposals': proposals}


def propose(per_tier, confusion, bins, thresholds, minimum_n=10, unassessed_judged=0):
    """Threshold change proposals from the reflection; written for the user to accept, never applied."""
    out = []
    if unassessed_judged:
        out.append({'id': 'assess-captures', 'kind': 'pipeline', 'status': 'proposed', 'threshold_path': None,
                    'evidence': {'human_judged_without_band': unassessed_judged},
                    'proposal': f'{unassessed_judged} human-judged roofs have no evidence band (their capture has no confidence.json), so they fall to '
                    'review by construction; run scene_confidence.assess on those captures before reading tier shares as a test of the rule.'})
    auto, assist = per_tier['auto']['confirmed_share'], per_tier['assist']['confirmed_share']
    for tier, share in (('auto', auto), ('assist', assist)):
        if share['n'] < minimum_n:
            out.append({'id': f'sample-{tier}', 'kind': 'sampling', 'status': 'proposed', 'threshold_path': None,
                        'evidence': share, 'proposal': f'Collect at least {minimum_n} human decisions on {tier}-tier roofs before changing '
                        f'thresholds for that tier (currently {share["n"]}).'})
    if auto['n'] >= minimum_n and assist['n'] >= minimum_n and auto['share'] is not None and assist['share'] is not None and auto['share'] <= assist['share']:
        out.append({'id': 'raise-auto-min', 'kind': 'threshold', 'status': 'proposed', 'threshold_path': 'auto_min',
                    'current': thresholds['auto_min'], 'proposed': round(min(.95, thresholds['auto_min'] + .05), 2),
                    'change': {'path': ['auto_min'], 'value': round(min(.95, thresholds['auto_min'] + .05), 2)},
                    'evidence': {'auto': auto, 'assist': assist},
                    'proposal': 'Auto confirmed share does not exceed assist; raise auto_min by 0.05 and re-run the reflection.'})
    for row in confusion:
        if row['disagree'] + row['agree'] >= minimum_n and row['disagree_share'] >= .5:
            name, _, value = row['signal'].partition('=')
            out.append({'id': f'penalise-{name}-{value}', 'kind': 'threshold', 'status': 'proposed', 'threshold_path': f'{name}[{value}]',
                        'current': (thresholds.get('signal_penalties') or {}).get(row['signal'], 0), 'proposed': 'subtract 0.10 when this signal state is present',
                        'change': {'path': ['signal_penalties', row['signal']], 'value': -.10},
                        'evidence': row, 'proposal': f'{row["disagree"]} of {row["disagree"] + row["agree"]} auto/assist roofs with {row["signal"]} '
                        'were rejected or needed adjustment; add a penalty for this state.'})
    for b in bins:
        if b['n'] >= minimum_n and b['wilson_95'] and b['wilson_95'][1] < b['bin'][0]:
            out.append({'id': f'bin-{b["bin"][0]}', 'kind': 'calibration', 'status': 'proposed', 'threshold_path': 'band_base/structure_base',
                        'current': None, 'proposed': f'lower the base so roofs now in [{b["bin"][0]}, {b["bin"][1]}) land one bin lower',
                        'evidence': b, 'proposal': 'Human confirmation in this bin sits wholly below the bin; the rule is over-confident there.'})
    if not out:
        out.append({'id': 'no-change', 'kind': 'none', 'status': 'proposed', 'threshold_path': None,
                    'proposal': 'No threshold change is supported by the current human decisions.'})
    for proposal in out:
        proposal.setdefault('approved', False)   # the user flips this to true; review_cycle --apply-approved applies only those with a 'change'
    return out


def reflection_markdown(report):
    t = report['per_tier']

    def share_text(s):
        if not s['n']:
            return 'no human-judged roofs'
        lo, hi = s['wilson_95']
        return f"{s['confirmed']} of {s['n']} confirmed ({s['share']:.0%}, 95 % Wilson interval {lo:.2f} to {hi:.2f})"
    lines = [f'# Reflection on {VERSION}', '',
             'These numbers are agreement between the machine tiers and the human decisions that exist in review.json and the T1/T2 references. '
             'They are not accuracy. ' + report['selection_bias'], '',
             f"Humans have decided on {report['decisions_total']} objects of every kind; {report['decisions_matched_to_building_rows']} of them are building objects "
             'with a machine decision row. The rest are solar panels, cars, roads and other kinds the policy does not label, or objects no longer in latest.json.', '']
    for tier in TIERS:
        row = t[tier]
        lines.append(f"The {tier} tier holds {row['judged']} human-judged roofs: {share_text(row['confirmed_share'])}; "
                     f"{row['status_counts'].get('rejected', 0)} rejected, {row['status_counts'].get('needs_adjustment', 0)} needing adjustment, "
                     f"{row['superseded_excluded']} superseded (excluded from the share).")
    ordering = report['ordering']
    verdict = ('The auto tier has the highest confirmed share.' if ordering['auto_highest_confirmed_share']
               else 'No human decision fell in the auto tier, so its confirmed share cannot be stated.' if ordering['shares']['auto'] is None
               else 'The auto tier does NOT have the highest confirmed share; see the proposals.')
    unassessed = {tier: t[tier]['band_unassessed'] for tier in TIERS}
    lines += ['', verdict, '',
              f"Human-judged roofs without an evidence band (no confidence.json for the capture), by tier: {unassessed}. "
              'Such roofs start from the unassessed base and land in review whatever their quality, so a high confirmed share in the review tier '
              'says the humans confirmed the training captures, not that the rule sorted them well.', '']
    if report['confusion_by_signal']:
        top = report['confusion_by_signal'][:5]
        lines.append('Signal states that most often accompany a rejected or adjusted auto/assist decision: '
                     + '; '.join(f"{r['signal']} ({r['disagree']} disagree vs {r['agree']} agree)" for r in top) + '.')
    else:
        lines.append('No auto or assist decision was rejected or adjusted, so no signal can be blamed yet.')
    lines.append('')
    populated = [b for b in report['calibration_bins'] if b['n']]
    lines.append('Calibration bins with human-judged roofs: ' + ('; '.join(
        f"[{b['bin'][0]:.1f}, {b['bin'][1]:.1f}) {b['confirmed']} of {b['n']} confirmed" for b in populated) if populated else 'none') + '.')
    fam = report['roof_family']
    lines += ['', f"Roof family labels: {fam['labels']} human labels; the machine roof type agrees on {fam.get('agree', 0)} and disagrees on "
              f"{fam.get('disagree', 0)} ({fam.get('with_decision_row', 0)} compared through a decision row, {fam.get('review_object_roof_type_only', 0)} "
              'through the roof type stored on the review object).',
              f"Roof-line edits: {report['roof_line_edits']['count']} objects, by tier {report['roof_line_edits']['tiers']}. "
              f"References by tier: {report['references_by_tier']}.", '', '## Proposals (not applied)', '']
    for p in report['proposals']:
        lines.append(f"- {p['id']}: {p['proposal']}")
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------- apply-auto

def label_text(payload):
    label = payload['machine_label']
    sub_kind, hint, roof_type = label['structure_sub_kind'], label['class_hint'], label['roof_type']
    if payload['signals']['auto_gates']['path'] == 'secondary-structure' and sub_kind:
        return sub_kind.replace('-', ' ').capitalize()
    base = {'house': 'House roof', 'units': 'Unit roof', 'commercial': 'Commercial roof'}.get(hint, 'Building roof')
    return f'{base} ({roof_type})' if roof_type else base


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _timestamp():
    return datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')


def _object_state(obj):
    """The fields the writer may change, for before/after audit rows."""
    if obj is None:
        return None
    return {k: obj.get(k) for k in ('status', 'label', 'geometry_reviewed', 'roof_type', 'provenance', 'notes')}


def machine_object(obj, item):
    """The auto-labelled copy of a model/review object: confirmed, sentence-case label, provenance, kind unchanged."""
    out = json.loads(json.dumps(obj))
    out.update(status='confirmed', label=item['label'], geometry_reviewed=True,
               provenance={'source': PROVENANCE, 'tier': 'auto', 'confidence': item['confidence'], 'interval': item['interval']})
    if item['roof_type'] and not out.get('roof_type') and not out.get('roof_line_edits'):
        out['roof_type'] = item['roof_type']
    sentence = (f"Machine label by the {VERSION} decision-policy agent (confidence {item['confidence']}); geometry_reviewed is set because the "
                'web save rule requires it for a confirmed object, not because a human checked the outline.')
    notes = out.get('notes')
    out['notes'] = sentence if not notes else (notes if sentence in notes else f'{notes}\n{sentence}')[:5000]
    return out


def protected_scene_ids(root=Path('.')):
    """Scene ids of the eyes_guard checkpoint manifests (protected references); machine labels never touch them."""
    try:
        from . import eyes_guard
    except ImportError:
        return set()
    folder = Path(root) / eyes_guard.MANIFESTS
    ids = set()
    if not folder.is_dir():
        return ids
    for manifest in folder.glob('*/manifest.json'):
        try:
            doc = json.loads(manifest.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get('scene_id'), str):
            ids.add(doc['scene_id'])
    return ids


def apply_items(payloads):
    items = []
    for p in payloads:
        if p['tier'] != 'auto' or not p['signals']['review']['live']:
            continue
        review = p['signals']['review']
        items.append({'scene_id': p['signals']['scene']['scene_id'], 'object_id': p['signals']['object_id'], 'label': label_text(p),
                      'kind': p['machine_label']['kind'], 'roof_type': p['machine_label']['roof_type'], 'status': 'confirmed',
                      'provenance': PROVENANCE, 'confidence': p['confidence'], 'interval': p.get('interval'), 'score_interval': p.get('score_interval'),
                      'path': p['signals']['auto_gates']['path'],
                      'existing_human_status': review['status'] if review['present'] and review['status'] != 'candidate' else None})
    return items


def _write_scene(root, scene_id, group, backup_dir, skipped):
    """Create or update one review.json; returns an audit entry or None when the scene was skipped."""
    folder = Path(root) / 'data/scenes' / scene_id
    file, lock = folder / 'review.json', folder / 'review.lock'
    try:
        latest = json.loads((folder / 'latest.json').read_bytes())
    except (OSError, ValueError):
        skipped['latest_unreadable'] += len(group)
        return None
    try:
        lock.mkdir()
    except FileExistsError:
        skipped['locked_by_another_save'] += len(group)
        return None
    try:
        existed = file.exists()
        before_bytes = file.read_bytes() if existed else None
        if existed:
            try:
                review = json.loads(before_bytes)
            except ValueError:
                skipped['review_unreadable'] += len(group)
                return None
            if review.get('model_signature') != latest.get('model_signature'):
                skipped['stale_review_signature'] += len(group)
                return None
            objects = [o for o in review.get('objects', []) if isinstance(o, dict)]
        else:
            review = None
            objects = json.loads(json.dumps(latest['objects']))  # the editor reads review.objects as the whole set
        by_id = {o.get('id'): i for i, o in enumerate(objects)}
        model_by_id = {o['id']: o for o in latest['objects']}
        changes = []
        for item in group:
            index = by_id.get(item['object_id'])
            current = objects[index] if index is not None else None
            if current is None and item['object_id'] not in model_by_id:
                skipped['object_not_in_latest'] += 1
                continue
            if current is not None and current.get('status') != 'candidate':
                skipped['already_decided_or_machine_labelled' if is_machine_labelled(current) else 'human_decided'] += 1
                continue
            source = current if current is not None else model_by_id[item['object_id']]
            after = machine_object(source, item)
            if index is None:
                objects.append(after)
            else:
                objects[index] = after
            changes.append({'object_id': item['object_id'], 'before': _object_state(current), 'after': _object_state(after),
                            'appended': index is None})
        if not changes:
            return None
        backup = backup_dir / scene_id / 'review.json'
        backup.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            backup.write_bytes(before_bytes)
        else:
            (backup_dir / scene_id / 'ABSENT').write_text('review.json did not exist before the machine write\n')
        stamp = now()
        if review is None:
            saved = {'revision': 1, 'model_signature': latest['model_signature'], 'objects': objects, 'groups': latest.get('groups', []),
                     'reviewed_classes': [], 'basemap_mode': 'unknown', 'updated_at': stamp, 'updated_by': PROVENANCE, 'history': []}
        else:
            history = review.get('history') or []
            old = {k: v for k, v in review.items() if k != 'history'}
            saved = {**review, 'objects': objects, 'revision': int(review.get('revision') or 0) + 1, 'updated_at': stamp,
                     'updated_by': PROVENANCE, 'basemap_mode': review.get('basemap_mode') or 'unknown', 'history': [*history, old]}
        after_bytes = json.dumps(saved, indent=2).encode()
        temp = folder / f'review.{VERSION}.tmp'
        temp.write_bytes(after_bytes)
        temp.replace(file)
        return {'scene_id': scene_id, 'path': str(file), 'existed': existed, 'backup': str(backup if existed else backup.parent / 'ABSENT'),
                'before_sha256': sha256_bytes(before_bytes) if existed else None, 'after_sha256': sha256_bytes(after_bytes),
                'revision_before': review.get('revision') if review else None, 'revision_after': saved['revision'],
                'objects_total': len(objects), 'changes': changes}
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def apply_auto(root=Path('.'), *, dry_run=True, acknowledged=False, payloads=None, audit_path=None, backup_dir=None, skip_scenes=None):
    """List (dry run) or write (live, acknowledged) auto-tier machine labels into review.json as confirmed.

    Live writes snapshot every touched review.json under backup-<timestamp>/ first, never touch objects a human decided,
    refuse stale-signature files, and log every write with before/after into the audit file that --rollback-auto reads.
    """
    root = Path(root)
    payloads = payloads if payloads is not None else load_decisions(root)
    protected = set(skip_scenes) if skip_scenes is not None else protected_scene_ids(root)
    items = apply_items(payloads)
    protected_items = [i for i in items if i['scene_id'] in protected]
    items = [i for i in items if i['scene_id'] not in protected]
    would_write = [i for i in items if i['existing_human_status'] is None]
    skipped_human = [i for i in items if i['existing_human_status'] is not None]
    report = {'version': VERSION, 'mode': 'dry-run' if dry_run else 'live', 'computed_at': now(), 'provenance': PROVENANCE,
              'note': 'auto-tier machine labels written (or, dry-run, that would be written) to review.json as confirmed; existing human decisions are never overwritten',
              'would_write': len(would_write), 'skipped_existing_human_decision': len(skipped_human),
              'by_group': dict(Counter(scene_group(i['scene_id'])[0] for i in would_write)),
              'by_label': dict(Counter(i['label'] for i in would_write)),
              'with_judged_interval': sum(1 for i in would_write if (i.get('interval') or {}).get('judged')),
              'skipped_protected_reference_scene': len(protected_items), 'protected_scenes': sorted(protected),
              'items': would_write, 'skipped': skipped_human, 'skipped_protected': protected_items}
    if dry_run:
        return report
    if not acknowledged:
        raise SystemExit('Refusing to write review.json: pass --i-understand-this-writes-review-json to run --apply-auto live.')
    stamp = _timestamp()
    backup_dir = Path(backup_dir) if backup_dir else root / REPORT_DIR / f'backup-{stamp}'
    backup_dir.mkdir(parents=True, exist_ok=True)
    skipped, writes = Counter(), []
    for scene_id, group in _group_by_scene(would_write).items():
        entry = _write_scene(root, scene_id, group, backup_dir, skipped)
        if entry:
            writes.append(entry)
    written = sum(len(w['changes']) for w in writes)
    report.update(written=written, files_created=sum(1 for w in writes if not w['existed']), files_updated=sum(1 for w in writes if w['existed']),
                  skipped_live=dict(skipped), backup_dir=str(backup_dir), writes=writes,
                  rollback=f'python -m scripts.ml.decision_policy --rollback-auto {audit_path or root / AUDIT_FILE}')
    audit = Path(audit_path) if audit_path else root / AUDIT_FILE
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps(report, indent=1, sort_keys=True, default=str))
    report['audit_path'] = str(audit)
    return report


def rollback_auto(root, audit_path):
    """Restore the exact prior state of every review.json the audit lists (bytes from the backup, or delete a created file)."""
    root = Path(root)
    audit = json.loads(Path(audit_path).read_bytes())
    report = {'version': VERSION, 'mode': 'rollback', 'computed_at': now(), 'audit': str(audit_path), 'restored': 0, 'deleted': 0,
              'skipped': Counter(), 'scenes': []}
    for write in audit.get('writes', []):
        file = Path(write['path'])
        if not file.is_absolute():
            file = root / file
        current = file.read_bytes() if file.exists() else None
        if current is None or sha256_bytes(current) != write['after_sha256']:
            report['skipped']['changed_since_write'] += 1
            report['scenes'].append({'scene_id': write['scene_id'], 'action': 'skipped', 'reason': 'file changed since the machine write'})
            continue
        if write['existed']:
            backup = Path(write['backup'])
            if not backup.exists() or sha256_bytes(backup.read_bytes()) != write['before_sha256']:
                report['skipped']['backup_missing_or_altered'] += 1
                report['scenes'].append({'scene_id': write['scene_id'], 'action': 'skipped', 'reason': 'backup missing or altered'})
                continue
            temp = file.with_suffix('.rollback.tmp')
            temp.write_bytes(backup.read_bytes())
            temp.replace(file)
            report['restored'] += 1
            report['scenes'].append({'scene_id': write['scene_id'], 'action': 'restored', 'sha256': write['before_sha256']})
        else:
            file.unlink()
            report['deleted'] += 1
            report['scenes'].append({'scene_id': write['scene_id'], 'action': 'deleted'})
    report['skipped'] = dict(report['skipped'])
    out = Path(audit_path).with_name(f'apply-auto-rollback-{_timestamp()}.json')
    out.write_text(json.dumps(report, indent=1, sort_keys=True))
    report['report_path'] = str(out)
    return report


def _group_by_scene(items):
    grouped = defaultdict(list)
    for item in items:
        grouped[item['scene_id']].append(item)
    return grouped


# --------------------------------------------------------------------------- CLI

def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--run', action='store_true', help='assemble signals, decide tiers, write decision_policy rows')
    parser.add_argument('--scenes', nargs='*', help='limit --run to these scene ids')
    parser.add_argument('--recompute-budget-seconds', type=float, default=720.)
    parser.add_argument('--max-recompute', type=int, default=600)
    parser.add_argument('--reflect', action='store_true', help='compare machine tiers against every human decision')
    parser.add_argument('--proposals', default=str(REPORT_DIR / 'proposals.json'), help='where --reflect writes threshold proposals (never applied)')
    parser.add_argument('--apply-auto', action='store_true', help='list (or, live, write) auto-tier labels into review.json')
    parser.add_argument('--dry-run', action='store_true', default=True)
    parser.add_argument('--live', action='store_true', help='write review.json (also needs --i-understand-this-writes-review-json)')
    parser.add_argument('--i-understand-this-writes-review-json', dest='acknowledged', action='store_true')
    parser.add_argument('--rollback-auto', metavar='AUDIT_JSON', help='restore every review.json listed in an apply-auto-live audit file')
    parser.add_argument('--report', help='report path (summary.json for --run, reflection.json for --reflect, apply-auto-dry-run.json for --apply-auto)')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.run:
        summary = run(root, args.scenes or None, recompute_budget_seconds=args.recompute_budget_seconds, max_recompute=args.max_recompute,
                      log=lambda line: print(line, file=sys.stderr, flush=True))
        _write_json(root / (args.report or REPORT_DIR / 'summary.json'), summary)
        print(json.dumps({'objects': summary['objects'], 'tiers': summary['tiers']['overall'], 'live': summary['tiers']['live'],
                          'groups': summary['tiers']['groups'], 'recompute': summary['recompute'], 'elapsed_seconds': summary['elapsed_seconds']}))
        return 0
    if args.reflect:
        payloads = load_decisions(root)
        report = reflect(payloads, human_decisions(root), effective_thresholds(root))
        target = root / (args.report or REPORT_DIR / 'reflection.json')
        _write_json(target, report)
        _write_json(root / args.proposals, {'version': VERSION, 'status': 'proposed, not applied', 'thresholds_version': report['thresholds_version'],
                                            'computed_at': report['computed_at'], 'proposals': report['proposals']})
        (target.parent / 'REFLECTION.md').write_text(reflection_markdown(report))
        print(json.dumps({'per_tier': {t: report['per_tier'][t]['confirmed_share'] for t in TIERS}, 'ordering': report['ordering'],
                          'proposals': [p['id'] for p in report['proposals']]}))
        return 0
    if args.rollback_auto:
        report = rollback_auto(root, args.rollback_auto)
        print(json.dumps({k: report[k] for k in ('restored', 'deleted', 'skipped', 'report_path')}))
        return 0
    if args.apply_auto:
        live = args.live
        if live:
            report = apply_auto(root, dry_run=False, acknowledged=args.acknowledged, audit_path=root / (args.report or AUDIT_FILE))
        else:
            report = apply_auto(root, dry_run=True)
            _write_json(root / (args.report or REPORT_DIR / 'apply-auto-dry-run.json'), report)
        keys = ('mode', 'would_write', 'skipped_existing_human_decision', 'skipped_protected_reference_scene', 'by_group', 'by_label', 'with_judged_interval')
        print(json.dumps({k: report[k] for k in keys}
                         | ({k: report.get(k) for k in ('written', 'files_created', 'files_updated', 'skipped_live', 'backup_dir', 'audit_path')} if live else {})))
        return 0
    parser.print_help()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
