"""roof-promotion-v1: promote the sidecar-evaluated roof fitter into a new inference profile.

The frozen Mooroolbark run (500114985968d40b, 257 tiles) and the house captures carry roof fields
(roof_annotation, roof_parts, roof_lines, roof_type, roof_geometry_source, roof_geometry_note and
the scene analysis) from the fitter as it was when they were extracted. The fitter has since gained
roof-form-flat-v3 (flat hypothesis with structure flat hints), roof-topology-support-v3 and the
class hints building-flags-v2 / structure-kinds-v3, all evaluated only as sidecars. This module
refits every roof exactly as the pipeline does (scene_house.add_roof_estimates with
scene_house.pipeline_context_hints) on the saved PNG. SAM3 is never run: outlines, trees, solar and
every other non-roof field are the production file's, byte for byte.

Modes
  --plan      the new profile identity (the analysis code hashes add_roof_estimates writes today
              plus the context-hint provenance) and, per scene, the fields that would change ->
              data/reviews/eyes/promotion/plan.json. Nothing else is written.
  --dry-run   refit every eligible scene into the isolated root data/reviews/eyes/promotion/candidate/
              (worker processes, resumable), compare candidate vs production per object and write
              dry-run.json, dry-run.txt, object-diffs.json and a contact sheet of random changed
              roofs (before orange, after green). Nothing under data/scenes is touched.
  --apply --i-understand-this-rewrites-latest-json
              rewrite latest.json (plus its models/<signature>.json snapshot) from the fresh
              candidate for scenes whose production bytes and model_signature still match the
              candidate's source (scan tiles: also the frozen run.json), preserving every non-roof
              field byte for byte, leaving review.json untouched, backing everything up under
              backup-<timestamp>/, then scene_confidence.assess per touched scene. confidence.sqlite
              live rows follow production; the frozen-profile rows are kept intact in the
              profile-keyed tables profile_roofs / profile_scenes next to the new-profile rows.
              Writes an audit with before/after sha256 and per-object field diffs.
  --rollback AUDIT
              restore every rewritten latest.json (bytes and mtime), snapshot, confidence.json,
              live confidence rows and the profile-table rows this apply added.

Skipped by rule: scenes without roof models, protected reference captures (eyes_guard manifests),
T1/T2 reference scenes of the eyes registry, and scenes whose review.json holds any human decision
(a new model_signature would make that review read as stale). Machine-labelled review files stay
byte-identical and keep binding to their retained previous snapshot. The optical-guidance stage is
pinned off by default (it is not one of the evaluated items; --optical-guidance includes it).
Everything here is a model estimate, never accuracy, and no human decision is created or changed.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import random
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

VERSION = 'roof-promotion-v1'
FROZEN_RUN = '500114985968d40b'
PIPELINE_PREFIX = 'sam3-scene-roof'
# Every key add_roof_estimates writes on a building; everything else on an object is a non-roof field.
GEOMETRY_FIELDS = ('roof_annotation', 'roof_parts', 'roof_lines', 'roof_type', 'roof_geometry_source')
FITTER_FIELDS = (*GEOMETRY_FIELDS, 'roof_geometry_note', 'roof_form_evidence', 'roof_form_uncertain', 'roof_form_comparison',
                 'roof_optical_guidance')
COUNT_KEYS = ('roof_proposals', 'roof_failures', 'roof_failure_details', 'context_hints')
OUT_DIR = Path('data/reviews/eyes/promotion')
CANDIDATE_DIR = OUT_DIR / 'candidate'
FROZEN_RUN_DIR = Path('data/suburbs/mooroolbark/runs') / FROZEN_RUN
CONFIDENCE_DB = Path('data/suburbs/mooroolbark/confidence.sqlite')
EYES_INDEX = Path('data/reviews/eyes/eyes.sqlite')
CONTEXT_SOURCES = {'inventory_properties': Path('data/reviews/eyes/context/inventory-properties.json'),
                   'osm_landuse': Path('data/reviews/eyes/context/osm-landuse.json'),
                   'address_kinds': Path('public/address-roofs.json')}
HINT_CODE = ('scene_house.py', 'scene_object_quality.py', 'structure_kinds.py', 'context_landuse.py')
NOT_INPUTS = {'address-allocation-v1': 'address_allocation is an address-matching sidecar; neither '
              'pipeline_context_hints nor add_roof_estimates reads it (class hints read address kinds from '
              'public/address-roofs.json), so it cannot change a roof field and is not part of this profile.'}
PROFILE_DDL = ('CREATE TABLE IF NOT EXISTS profile_scenes(profile TEXT, id TEXT, signature TEXT, mtime INTEGER, bounds TEXT, '
               'source TEXT, PRIMARY KEY(profile, id))',
               'CREATE TABLE IF NOT EXISTS profile_roofs(profile TEXT, scene_id TEXT, object_id TEXT, west REAL, south REAL, '
               'east REAL, north REAL, payload TEXT, source TEXT, PRIMARY KEY(profile, scene_id, object_id))')
EXIT_BUSY = 75
NOTE = ('Model estimates from the promoted roof fitter, not accuracy. Detection outlines are the frozen '
        "run's (SAM3 was not re-run) and no human decision was created or changed.")


# ----------------------------------------------------------------------------- small helpers

def now():
    return datetime.now(UTC).isoformat()


def stamp():
    return datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    path = Path(path)
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


def signature(value):
    """scene_batch.signature without importing the detector stack."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def model_signature(scene):
    """Exactly scene_extract.save_scene: sha256 of the sorted JSON without model_signature."""
    return signature({key: value for key, value in scene.items() if key != 'model_signature'})


def scene_bytes(scene):
    """save_scene's serialisation: json.dumps(scene, indent=2), insertion order kept."""
    return json.dumps(scene, indent=2).encode()


def profile_of(scene):
    """scene_confidence.profile, restated so --plan stays light (a test pins equality)."""
    analysis = scene.get('analysis') or {}
    return signature({k: v for k, v in analysis.items()
                      if k.endswith('_code') or k in ('pipeline', 'roof_model', 'input_geometry', 'inference_profile')})


def validate_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value):
        raise ValueError('Invalid scene ID')
    return value


def owned_ids(scene):
    """scene_confidence.assess semantics: owned_object_ids when the key exists (an empty list owns nothing), else every object."""
    if 'owned_object_ids' in scene:
        return set(scene['owned_object_ids'] or [])
    return {o['id'] for o in scene.get('objects', [])}


def rate(n, of):
    return {'n': n, 'of': of, 'share': round(n / of, 4) if of else None}


def _dump(value):
    return json.dumps(value, sort_keys=True)


def _median(values):
    return statistics.median(values) if values else None


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str))
    os.replace(temporary, path)


def _write_bytes(path, data):
    """Atomic replace (save_scene writes latest.tmp then replaces latest.json)."""
    path = Path(path)
    temporary = path.with_name(f'.{path.name}.{VERSION}.{os.getpid()}.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def connect_readonly(path):
    return sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True)


def non_roof_bytes(scene):
    """The scene without analysis, model_signature and fitter fields, serialised in file order: must never change."""
    view = {k: v for k, v in scene.items() if k not in ('analysis', 'model_signature')}
    if 'objects' in view:
        view['objects'] = [{k: v for k, v in o.items() if k not in FITTER_FIELDS} if o.get('kind') == 'building' else o
                           for o in scene['objects']]
    return json.dumps(view, indent=2).encode()


# ----------------------------------------------------------------------------- profile identity

def fit_kwargs(optical_guidance):
    return {'optical_guidance': bool(optical_guidance)}


def fitter_analysis(optical_guidance=False):
    """analysis exactly as add_roof_estimates writes it today (empty stub scene, no image), counts removed."""
    from . import scene_house
    stub = {'id': 'promotion-identity-stub', 'objects': [], 'pixel_to_meters': [[1, 0], [0, -1], [0, 0]],
            'meters_to_pixel': [[1, 0], [0, -1], [0, 0]]}
    scene_house.add_roof_estimates(stub, None, context_hints={'version': None, 'hints': {}}, **fit_kwargs(optical_guidance))
    return {k: v for k, v in stub['analysis'].items() if k not in COUNT_KEYS}


def frozen_plan(root):
    path = Path(root) / FROZEN_RUN_DIR / 'plan.json'
    return json.loads(path.read_text()) if path.exists() else None


def detection_identity(plan):
    """The frozen run's detection identity; its inference_profile is computed as scene_suburb.run does."""
    if not plan:
        return None
    return {'inference_profile': signature({'code': plan['code'], 'weights': plan['weights'],
                                            'roof_form': plan.get('roof_form'), 'prompts': plan['prompts']}),
            'weights': plan['weights'], 'prompts': plan['prompts'], 'roof_form': plan.get('roof_form'),
            'note': "SAM3 detection, prompts and outlines are the frozen run's; not re-run."}


def poi_digest(root):
    """sha256 of the read-only POI rows class hints load (suburb bounds), or None when unavailable."""
    try:
        from .scene_object_quality import load_pois, suburb_bounds
        bounds = suburb_bounds(Path(root))
        rows = load_pois(Path(root), bounds) if bounds else []
    except Exception:  # noqa: BLE001 - provenance only; a missing source is recorded as None
        return None
    return {'rows': len(rows), 'sha256': signature(sorted(rows, key=lambda r: str(r.get('id'))))} if rows else None


def identity(root=Path('.'), *, optical_guidance=False):
    """The new profile identity: today's fitter hashes plus the context-hint provenance that feeds them."""
    from . import structure_kinds
    from .roof_form import FLAT_RULES, FLAT_VERSION
    from .roof_topology import SUPPORT_RULES, TOPOLOGY_SUPPORT_VERSION
    from .scene_object_quality import BUILDING_VERSION
    root = Path(root)
    here = Path(__file__).parent
    components = {
        'version': VERSION, 'frozen_run': FROZEN_RUN,
        'detection': detection_identity(frozen_plan(root)),
        'roof_fitter': fitter_analysis(optical_guidance),
        'fitter_call': {'entry': 'scene_house.add_roof_estimates(scene, image, context_hints=scene_house.pipeline_context_hints('
                                 f'scene, image, root), optical_guidance={bool(optical_guidance)})',
                        'optical_guidance': bool(optical_guidance)},
        'flat_rules': {'version': FLAT_VERSION, 'rules': FLAT_RULES},
        'topology_support': {'version': TOPOLOGY_SUPPORT_VERSION, 'rules': SUPPORT_RULES},
        'context_hints': {'building_version': BUILDING_VERSION, 'structure_version': structure_kinds.VERSION,
                          'read_by': FLAT_RULES.get('context'), 'mode': 'cached context only (pipeline_context_hints)',
                          'code': {name: sha256_file(here / name) for name in HINT_CODE},
                          'sources': {name: sha256_file(root / path) for name, path in CONTEXT_SOURCES.items()},
                          'pois': poi_digest(root)}}
    return {'profile_id': signature(components), 'components': components}


def expected_profile(ident, group, input_geometry='imagery-only'):
    """scene_confidence.profile() of a promoted scene of this group, before any roof is computed."""
    analysis = dict(ident['components']['roof_fitter'])
    if input_geometry is not None:
        analysis['input_geometry'] = input_geometry
    if group == 'scan':
        analysis['inference_profile'] = ident['profile_id']
    return profile_of({'analysis': analysis})


def analysis_changes(previous, fitter):
    """Which analysis keys the promotion changes, adds or leaves (counts and context summary excluded)."""
    previous = {k: v for k, v in (previous or {}).items() if k not in COUNT_KEYS}
    return {'changed': sorted(k for k in fitter if k in previous and previous[k] != fitter[k]),
            'added': sorted(k for k in fitter if k not in previous),
            'kept': sorted(k for k in fitter if k in previous and previous[k] == fitter[k])}


# ----------------------------------------------------------------------------- inventory

def protected_scene_ids(root):
    """{scene_id: [manifest ids]} for every eyes_guard checkpoint manifest (protected references)."""
    from .eyes_guard import MANIFESTS, PROTECTED_MANIFESTS
    protected = {}
    for path in sorted((Path(root) / MANIFESTS).glob('*/manifest.json')):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        scene_id = manifest.get('scene_id') if isinstance(manifest, dict) else None
        if isinstance(scene_id, str):
            protected.setdefault(scene_id, []).append(path.parent.name)
    for manifest_id in PROTECTED_MANIFESTS:  # a protected manifest that went missing is the guard's failure, not a licence
        if not (Path(root) / MANIFESTS / manifest_id / 'manifest.json').exists():
            protected.setdefault('__missing_protected_manifest__', []).append(manifest_id)
    return protected


def reference_scene_ids(root):
    """{scene_id: [tiers]} of T1/T2 human references in the eyes registry (read-only); {} when unavailable."""
    path = Path(root) / EYES_INDEX
    if not path.exists():
        return {}
    try:
        with closing(connect_readonly(path)) as db:
            rows = db.execute('SELECT DISTINCT scene_id, tier FROM "references" WHERE tier IN (\'T1\', \'T2\') AND scene_id IS NOT NULL').fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for scene_id, tier in rows:
        out.setdefault(scene_id, []).append(tier)
    return {k: sorted(v) for k, v in out.items()}


def frozen_signatures(root):
    path = Path(root) / FROZEN_RUN_DIR / 'run.json'
    if not path.exists():
        return {}
    results = json.loads(path.read_text()).get('results', {})
    return {sid: r.get('model_signature') for sid, r in results.items() if r.get('status') == 'complete'}


def is_machine_labelled(obj):
    """decision_policy.is_machine_labelled: machine provenance, status confirmed, never stamped by a reviewer."""
    provenance = obj.get('provenance') if isinstance(obj.get('provenance'), dict) else {}
    if provenance.get('human_confirmed') is True:
        return False
    source = provenance.get('source')
    return isinstance(source, str) and source.startswith('machine:') and obj.get('status') == 'confirmed'


def review_state(folder, latest):
    """review.json summary: present, current (same model_signature), human-touched objects, machine labels, sha256."""
    path = Path(folder) / 'review.json'
    if not path.exists():
        return {'present': False, 'current': None, 'human_touched': 0, 'machine_labelled': 0, 'sha256': None}
    data = path.read_bytes()
    try:
        review = json.loads(data)
    except ValueError:
        return {'present': True, 'current': None, 'human_touched': None, 'machine_labelled': None, 'sha256': sha256_bytes(data),
                'error': 'unreadable'}
    model = {o.get('id'): o for o in latest.get('objects', []) if isinstance(o, dict)}
    objects = [o for o in (review.get('objects') or []) if isinstance(o, dict)]
    machine = [o for o in objects if is_machine_labelled(o)]
    others = [o for o in objects if not is_machine_labelled(o)]
    decisions = sum(1 for o in others if o.get('status') not in (None, 'candidate') or o.get('geometry_reviewed')
                    or o.get('roof_line_edits') or o.get('roof_feedback'))
    # Any other object that is not an exact copy of the model object is also treated as human work (edits, drawn objects).
    edited = sum(1 for o in others if _dump(o) != _dump(model.get(o.get('id'))))
    return {'present': True, 'current': review.get('model_signature') == latest.get('model_signature'),
            'human_touched': max(decisions, edited), 'human_decisions': decisions, 'objects_not_model_copies': edited,
            'machine_labelled': len(machine), 'objects': len(objects), 'sha256': sha256_bytes(data)}


def scene_kind(scene_id, scene):
    if scene.get('suburb_run') == FROZEN_RUN:
        return 'scan'
    return 'map' if scene_id.startswith('map-') else scene_id.split('-')[0]


def scene_record(root, scene_id, *, frozen, protected, references, fitter, skip_machine_labelled=False):
    folder = Path(root) / 'data/scenes' / validate_id(scene_id)
    source = (folder / 'latest.json').read_bytes()
    scene = json.loads(source)
    analysis = scene.get('analysis') or {}
    group = 'scan' if scene.get('suburb_run') == FROZEN_RUN else 'capture'
    buildings = [o for o in scene.get('objects', []) if o.get('kind') == 'building']
    owned = owned_ids(scene)
    fitted = [o for o in buildings if o.get('roof_parts') or o.get('roof_annotation')]
    unexpected = sorted({k for o in buildings for k in o if k.startswith('roof_') and k not in FITTER_FIELDS})
    review = review_state(folder, scene)
    has_roof_models = str(analysis.get('pipeline') or '').startswith(PIPELINE_PREFIX)
    record = {'scene_id': scene_id, 'group': group, 'kind': scene_kind(scene_id, scene), 'title': scene.get('title'),
              'model_signature': scene.get('model_signature'), 'latest_sha256': sha256_bytes(source), 'latest_bytes': len(source),
              'pipeline': analysis.get('pipeline'), 'inference_profile': analysis.get('inference_profile'),
              'profile': profile_of(scene), 'input_geometry': analysis.get('input_geometry'),
              'has_roof_models': has_roof_models, 'objects': len(scene.get('objects', [])), 'buildings': len(buildings),
              'buildings_fitted': len(fitted), 'buildings_unfitted': len(buildings) - len(fitted),
              'owned_buildings': sum(1 for o in buildings if o['id'] in owned),
              'unexpected_roof_keys': unexpected, 'protected': protected.get(scene_id, []),
              'reference_tiers': references.get(scene_id, []), 'review': review,
              'frozen_signature_match': (frozen.get(scene_id) == scene.get('model_signature')) if group == 'scan' else None}
    reason = None
    if not has_roof_models:
        reason = 'no_roof_models'
    elif record['protected']:
        reason = 'protected_reference'
    elif record['reference_tiers']:
        reason = 'reference_scene'
    elif review['present'] and review.get('error'):
        reason = 'review_unreadable'
    elif review['present'] and review['human_touched']:
        reason = 'human_decided_review'
    elif group == 'scan' and not record['frozen_signature_match']:
        reason = 'frozen_signature_mismatch'
    elif skip_machine_labelled and review['present'] and review['machine_labelled']:
        reason = 'machine_labelled_review'
    elif unexpected:
        reason = 'unexpected_roof_keys'
    changes = analysis_changes(analysis, fitter) if has_roof_models else None
    record.update(eligible=has_roof_models, apply=reason is None, skip_reason=reason,
                  fields_that_would_change={
                      'top_level': ['analysis', 'model_signature'],
                      'analysis': {**changes, 'inference_profile': 'replaced' if group == 'scan' else 'absent (captures have no frozen detection identity)'},
                      'building_fields': list(FITTER_FIELDS), 'buildings': len(buildings),
                      'note': 'every building is refit; fields that come back byte-identical are left as they are',
                  } if has_roof_models else None)
    return record


def inventory(root=Path('.'), scene_ids=None, *, fitter=None, optical_guidance=False, skip_machine_labelled=False):
    root = Path(root)
    fitter = fitter if fitter is not None else fitter_analysis(optical_guidance)
    frozen, protected, references = frozen_signatures(root), protected_scene_ids(root), reference_scene_ids(root)
    if scene_ids is None:
        scene_ids = sorted(p.parent.name for p in (root / 'data/scenes').glob('*/latest.json')
                           if not p.parent.name.startswith('qa_') and re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', p.parent.name))
    return [scene_record(root, sid, frozen=frozen, protected=protected, references=references, fitter=fitter,
                         skip_machine_labelled=skip_machine_labelled) for sid in scene_ids]


def inventory_counts(records):
    eligible = [r for r in records if r['eligible']]
    applying = [r for r in records if r['apply']]
    return {'scenes': len(records), 'eligible': len(eligible), 'apply': len(applying),
            'by_group': {g: {'eligible': sum(1 for r in eligible if r['group'] == g), 'apply': sum(1 for r in applying if r['group'] == g)}
                         for g in ('scan', 'capture')},
            'by_kind': dict(sorted(Counter(r['kind'] for r in records).items())),
            'skip_reasons': dict(sorted(Counter(r['skip_reason'] for r in records if r['skip_reason']).items())),
            'buildings': {'eligible': sum(r['buildings'] for r in eligible), 'apply': sum(r['buildings'] for r in applying)},
            'owned_buildings': {'eligible': sum(r['owned_buildings'] for r in eligible), 'apply': sum(r['owned_buildings'] for r in applying),
                                'scan_apply': sum(r['owned_buildings'] for r in applying if r['group'] == 'scan')},
            'reviews': {'present': sum(1 for r in eligible if r['review']['present']),
                        'machine_only_in_apply': sum(1 for r in applying if r['review']['present']),
                        'machine_labels_in_apply': sum(r['review'].get('machine_labelled') or 0 for r in applying),
                        'human_touched_skipped': sum(1 for r in eligible if r['skip_reason'] == 'human_decided_review')}}


# ----------------------------------------------------------------------------- plan

def plan(root=Path('.'), scene_ids=None, *, optical_guidance=False, skip_machine_labelled=False, write=True):
    root = Path(root)
    ident = identity(root, optical_guidance=optical_guidance)
    records = inventory(root, scene_ids, fitter=ident['components']['roof_fitter'], skip_machine_labelled=skip_machine_labelled)
    scan_profiles = {r['inference_profile'] for r in records if r['group'] == 'scan'}
    detection = (ident['components'].get('detection') or {}).get('inference_profile')
    for record in records:
        record['candidate'] = candidate_status(root, record, ident) if record['eligible'] else None
    report = {'version': VERSION, 'code_sha256': code_sha256(), 'computed_at': now(), 'note': NOTE,
              'profile_id': ident['profile_id'], 'components': ident['components'], 'not_inputs': NOT_INPUTS,
              'expected_profile': {'scan': expected_profile(ident, 'scan'), 'capture': expected_profile(ident, 'capture')},
              'current_profile': {'scan': dict(Counter(r['profile'] for r in records if r['group'] == 'scan')),
                                  'capture': dict(Counter(r['profile'] for r in records if r['group'] == 'capture' and r['eligible']))},
              'detection_profile_matches_production': (scan_profiles == {detection}) if scan_profiles else None,
              'skip_machine_labelled': bool(skip_machine_labelled), 'counts': inventory_counts(records), 'scenes': records}
    if write:
        _write_json(root / OUT_DIR / 'plan.json', report)
        report['path'] = str(root / OUT_DIR / 'plan.json')
    return report


# ----------------------------------------------------------------------------- candidate recompute

def candidate_paths(root, scene_id):
    folder = Path(root) / CANDIDATE_DIR / 'data/scenes' / validate_id(scene_id)
    return folder / 'latest.json', folder / 'promotion.json'


def candidate_status(root, record, ident):
    """'fresh' when the stored candidate was computed from the current production bytes under this identity."""
    latest, diag_path = candidate_paths(root, record['scene_id'])
    if not latest.exists() or not diag_path.exists():
        return {'status': 'missing'}
    try:
        diag = json.loads(diag_path.read_bytes())
    except ValueError:
        return {'status': 'unreadable'}
    if diag.get('source_sha256') != record['latest_sha256'] or diag.get('source_model_signature') != record['model_signature']:
        return {'status': 'stale_source', 'computed_at': diag.get('computed_at')}
    if diag.get('profile_id') != ident['profile_id']:
        return {'status': 'stale_profile', 'computed_at': diag.get('computed_at'), 'profile_id': diag.get('profile_id')}
    return {'status': 'fresh', 'computed_at': diag.get('computed_at'), 'candidate_model_signature': diag.get('candidate_model_signature'),
            'objects_changed': diag.get('objects_changed'), 'geometry_changed': diag.get('geometry_changed'), 'elapsed_s': diag.get('elapsed_s')}


def strip_fitter_fields(scene):
    """The scene as the pipeline hands it to the fitter: no analysis, no signature, no fitter output on any building."""
    working = copy.deepcopy(scene)
    for obj in working.get('objects', []):
        if obj.get('kind') == 'building':
            for key in FITTER_FIELDS:
                obj.pop(key, None)
    working.pop('analysis', None)
    working.pop('model_signature', None)
    return working


def merge_promoted(production, fitted, analysis):
    """Production with only fitter fields replaced in place (new keys appended), so every other byte is kept."""
    promoted = copy.deepcopy(production)
    by_id = {o['id']: o for o in fitted.get('objects', []) if o.get('kind') == 'building'}
    for obj in promoted.get('objects', []):
        if obj.get('kind') != 'building':
            continue
        new = by_id[obj['id']]
        for key in FITTER_FIELDS:
            if key in new:
                obj[key] = copy.deepcopy(new[key])
            else:
                obj.pop(key, None)
    promoted['analysis'] = analysis
    promoted.pop('model_signature', None)
    promoted['model_signature'] = model_signature(promoted)
    return promoted


def promoted_analysis(production, fitted_analysis, ident, group):
    """The fitter's analysis, plus production keys the fitter does not write (input_geometry, capture history), plus provenance."""
    analysis = dict(fitted_analysis)
    previous = production.get('analysis') or {}
    carried = sorted(k for k in previous if k not in analysis and k not in ('inference_profile', 'promotion'))
    for key in carried:
        analysis[key] = previous[key]
    if group == 'scan':
        analysis['inference_profile'] = ident['profile_id']
    analysis['promotion'] = {'version': VERSION, 'profile_id': ident['profile_id'], 'detection_unchanged': True,
                             'previous_model_signature': production.get('model_signature'),
                             'previous_inference_profile': previous.get('inference_profile'),
                             'previous_pipeline': previous.get('pipeline'),
                             'previous_profile': profile_of(production), 'carried_analysis_keys': carried, 'note': NOTE}
    return analysis


def _proposal_diagnostics(proposal):
    topology = proposal.get('topology') or {}
    support = topology.get('support') or {}
    selected = support.get('selected') or {}
    form = proposal.get('form_evidence') or {}
    context = form.get('context') if isinstance(form.get('context'), dict) else {}
    guidance = proposal.get('optical_guidance')
    accepted = bool(support.get('accepted'))
    return {'ok': True, 'roof_type': proposal.get('roof_type'), 'source': proposal.get('source'),
            'form_uncertain': bool(proposal.get('form_uncertain')), 'audit_passed': (proposal.get('audit') or {}).get('passed'),
            'form': {'version': form.get('version'), 'decision': form.get('decision'), 'rule': form.get('rule'), 'score': form.get('score'),
                     'representation': form.get('representation'), 'preview': form.get('flat_low_slope_preview'),
                     'class_hint': context.get('class_hint'), 'structure_flat': context.get('structure_flat')},
            'topology': {'method': topology.get('method'), 'accepted': topology.get('accepted'), 'reason': topology.get('reason'),
                         'before': topology.get('before'), 'after': topology.get('after')} if topology else None,
            'support': {'method': support.get('method'), 'accepted': accepted, 'reason': support.get('reason'),
                        'shape': selected.get('shape') or ('rectangle' if accepted else None),
                        'accepted_by': selected.get('accepted_by') or ('contrast-share' if accepted else None)} if support else None,
            'optical_guidance': {k: guidance.get(k) for k in ('method', 'accepted', 'reason')} if isinstance(guidance, dict) else None}


def fit_scene(scene, image, root, *, optical_guidance=False):
    """pipeline_context_hints + add_roof_estimates exactly as analyse_scene calls them, with per-roof diagnostics.

    propose_roof is wrapped for the duration of the call; the outermost call per building is keyed by the local
    ring add_roof_estimates computes (identical arithmetic), so diagnostics never depend on call order.
    """
    import numpy as np

    from . import scene_house, scene_roof
    hints = scene_house.pipeline_context_hints(scene, image, Path(root))
    frame = np.asarray(scene['pixel_to_meters'])
    queue = {}
    for obj in scene['objects']:
        if obj.get('kind') == 'building' and not obj.get('roof_parts') and not obj.get('roof_annotation'):
            key = json.dumps((np.c_[obj['ring'], np.ones(len(obj['ring']))] @ frame).tolist())
            queue.setdefault(key, []).append(obj['id'])
    diagnostics, depth, original = {}, [0], scene_roof.propose_roof

    def store(ring, payload):
        ids = queue.get(json.dumps(ring)) or []
        target = next((oid for oid in ids if oid not in diagnostics), None)
        if target is not None:
            diagnostics[target] = payload

    def recording(ring, *args, **kwargs):
        outer = depth[0] == 0
        depth[0] += 1
        try:
            proposal = original(ring, *args, **kwargs)
        except Exception as error:
            if outer:
                store(ring, {'ok': False, 'error': f'{type(error).__name__}: {error}'[:300]})
            raise
        finally:
            depth[0] -= 1
        if outer:
            store(ring, _proposal_diagnostics(proposal))
        return proposal

    scene_roof.propose_roof = recording
    try:
        scene_house.add_roof_estimates(scene, image, context_hints=hints, **fit_kwargs(optical_guidance))
    finally:
        scene_roof.propose_roof = original
    summary = {k: v for k, v in hints.items() if k not in ('hints', 'structure', 'flag_payloads')}
    summary.update(hinted_objects=len(hints.get('hints') or {}), by_hint=dict(sorted(Counter((hints.get('hints') or {}).values()).items())),
                   by_structure=dict(sorted(Counter(str(v.get('sub_kind')) for v in (hints.get('structure') or {}).values()).items())))
    return diagnostics, summary


def _fitted(obj):
    return bool(obj.get('roof_parts') or obj.get('roof_annotation'))


def object_diff(before, after, diag=None):
    """Per-object promotion diff; 'identical' means every fitter field serialises identically (and none is added or removed)."""
    changed = [k for k in FITTER_FIELDS if (k in before) != (k in after) or _dump(before.get(k)) != _dump(after.get(k))]
    geometry = [k for k in changed if k in GEOMETRY_FIELDS]
    diag = diag or {}
    form = diag.get('form') or {}
    support = diag.get('support') or {}
    topology = diag.get('topology') or {}
    guidance = diag.get('optical_guidance') or {}
    flat_fired = form.get('decision') == 'flat' or form.get('representation') == 'unresolved_coplanar'
    fitted = [_fitted(before), _fitted(after)]
    if not geometry:
        attribution = None if not changed else 'note-or-evidence-only'
    elif fitted == [False, True]:
        attribution = 'fit-gained'
    elif fitted == [True, False]:
        attribution = 'fit-lost'
    elif flat_fired:
        attribution = 'flat-v3'
    elif support.get('accepted'):
        attribution = 'topology-support-v3'
    elif guidance.get('accepted'):
        attribution = 'optical-guidance'
    else:
        attribution = 'other-fitter-code'
    return {'object_id': before['id'], 'identical': not changed, 'fields_changed': changed, 'geometry_changed': bool(geometry),
            'roof_type': [before.get('roof_type'), after.get('roof_type')],
            'source': [before.get('roof_geometry_source'), after.get('roof_geometry_source')],
            'planes': [len(before.get('roof_parts') or []), len(after.get('roof_parts') or [])],
            'creases': [len(before.get('roof_lines') or []), len(after.get('roof_lines') or [])],
            'fitted': fitted, 'flat_fired': flat_fired,
            'flat': {k: form.get(k) for k in ('decision', 'rule', 'representation', 'preview', 'class_hint', 'structure_flat', 'score')},
            'comparison_added': 'roof_form_comparison' in after and 'roof_form_comparison' not in before,
            'topology_accepted': bool(topology.get('accepted')), 'topology_method': topology.get('method'),
            'support_accepted': bool(support.get('accepted')), 'support_accepted_by': support.get('accepted_by'),
            'support_shape': support.get('shape'), 'support_reason': support.get('reason'),
            'guidance_accepted': bool(guidance.get('accepted')) if guidance else None,
            'form_uncertain': diag.get('form_uncertain'), 'attribution': attribution,
            'diagnosed': bool(diag), 'error': diag.get('error')}


def recompute_scene(scene_id, root, *, ident, optical_guidance=False):
    """Worker: refit one scene into the candidate root; nothing under data/scenes is written."""
    started = time.perf_counter()
    root = Path(root)
    from PIL import Image
    folder = root / 'data/scenes' / validate_id(scene_id)
    source = (folder / 'latest.json').read_bytes()
    production = json.loads(source)
    group = 'scan' if production.get('suburb_run') == FROZEN_RUN else 'capture'
    result = {'scene_id': scene_id, 'group': group, 'status': 'failed', 'source_model_signature': production.get('model_signature')}
    try:
        before = identity(root, optical_guidance=optical_guidance)
        if before['profile_id'] != ident['profile_id']:
            return {**result, 'status': 'code_drift', 'error': 'fitter code or hint inputs changed since the plan; re-run --dry-run',
                    'elapsed_s': round(time.perf_counter() - started, 3)}
        sha = production['image_sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError('Invalid imagery identity')
        image_path = folder / f'{sha}.png'
        if sha256_bytes(image_path.read_bytes()) != sha:
            raise ValueError('Source imagery changed')
        with Image.open(image_path) as opened:
            if opened.size != (production['image_width'], production['image_height']):
                raise ValueError('Imagery dimensions changed')
            image = opened.convert('RGB')
        working = strip_fitter_fields(production)
        input_keys = {o['id']: set(o) for o in working['objects'] if o.get('kind') == 'building'}
        diagnostics, hints = fit_scene(working, image, root, optical_guidance=optical_guidance)
        for obj in working['objects']:
            if obj.get('kind') == 'building':
                unknown = set(obj) - input_keys[obj['id']] - set(FITTER_FIELDS)
                if unknown:
                    raise RuntimeError(f'add_roof_estimates wrote keys this promotion does not know: {sorted(unknown)}')
        after = identity(root, optical_guidance=optical_guidance)
        if after['profile_id'] != ident['profile_id']:
            return {**result, 'status': 'code_drift', 'error': 'fitter code or hint inputs changed while this scene was refit',
                    'elapsed_s': round(time.perf_counter() - started, 3)}
        analysis = promoted_analysis(production, working['analysis'], ident, group)
        promoted = merge_promoted(production, working, analysis)
        if non_roof_bytes(promoted) != non_roof_bytes(production):
            raise RuntimeError('Non-roof fields changed during the refit')
        data = scene_bytes(promoted)
        owned = owned_ids(production)
        after_by_id = {o['id']: o for o in promoted['objects']}
        diffs = []
        for obj in production['objects']:
            if obj.get('kind') == 'building':
                diff = object_diff(obj, after_by_id[obj['id']], diagnostics.get(obj['id']))
                diff['owned'] = obj['id'] in owned
                diffs.append(diff)
        latest_path, diag_path = candidate_paths(root, scene_id)
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes(latest_path, data)
        record = {'version': VERSION, 'code_sha256': code_sha256(), 'scene_id': scene_id, 'group': group, 'note': NOTE,
                  'source_model_signature': production['model_signature'], 'source_sha256': sha256_bytes(source),
                  'candidate_model_signature': promoted['model_signature'], 'candidate_sha256': sha256_bytes(data),
                  'profile_id': ident['profile_id'], 'profile_before': profile_of(production), 'profile_after': profile_of(promoted),
                  'optical_guidance': bool(optical_guidance), 'hints': hints,
                  'roof_proposals': analysis.get('roof_proposals'), 'roof_failures': analysis.get('roof_failures'),
                  'roof_failures_before': (production.get('analysis') or {}).get('roof_failures'),
                  'buildings': len(diffs), 'objects_changed': sum(not d['identical'] for d in diffs),
                  'geometry_changed': sum(d['geometry_changed'] for d in diffs), 'undiagnosed': sum(not d['diagnosed'] for d in diffs),
                  'object_diffs': diffs, 'elapsed_s': round(time.perf_counter() - started, 3), 'computed_at': now()}
        _write_json(diag_path, record)
        return {**result, 'status': 'complete', 'candidate_model_signature': promoted['model_signature'], 'buildings': len(diffs),
                'objects_changed': record['objects_changed'], 'geometry_changed': record['geometry_changed'],
                'roof_failures': record['roof_failures'], 'elapsed_s': record['elapsed_s']}
    except Exception as error:  # noqa: BLE001 - one scene's failure is recorded; the batch continues
        return {**result, 'error': f'{type(error).__name__}: {error}'[:500], 'elapsed_s': round(time.perf_counter() - started, 3)}


def _init_worker(root):
    os.chdir(root)  # add_roof_estimates resolves scripts/ml/models/roof-form.json from the working directory


def _fit_worker(scene_id, root, ident, optical_guidance):
    return recompute_scene(scene_id, root, ident=ident, optical_guidance=optical_guidance)


def _pool(workers, root):
    import multiprocessing
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn'),
                               initializer=_init_worker, initargs=(str(root),))


def _module():
    """This module under its package name, so spawned workers can import the submitted functions."""
    import importlib
    return importlib.import_module('scripts.ml.promote_profile')


def compute_candidates(root, records, ident, *, workers=1, optical_guidance=False, limit=None, log=None):
    """Refit every eligible scene without a fresh candidate (largest first); resumable; inline when workers <= 1."""
    root = Path(root)
    todo = [r for r in records if r['eligible'] and candidate_status(root, r, ident)['status'] != 'fresh']
    todo.sort(key=lambda r: -r['buildings'])
    todo = [r['scene_id'] for r in todo][:limit]
    run_path = root / CANDIDATE_DIR / 'run.json'
    state = {'version': VERSION, 'profile_id': ident['profile_id'], 'started_at': now(), 'workers': workers, 'total': len(todo),
             'optical_guidance': bool(optical_guidance), 'results': {}}
    _write_json(run_path, state)
    started = time.perf_counter()

    def record(result):
        state['results'][result['scene_id']] = result
        state['counts'] = dict(Counter(r['status'] for r in state['results'].values()))
        state['updated_at'] = now()
        _write_json(run_path, state)
        if log:
            done = len(state['results'])
            log(json.dumps({'done': f'{done}/{len(todo)}', 'elapsed_s': round(time.perf_counter() - started, 1),
                            **{k: result.get(k) for k in ('scene_id', 'status', 'buildings', 'objects_changed', 'geometry_changed',
                                                          'elapsed_s', 'error')}}, default=str))

    if workers <= 1:
        for scene_id in todo:
            record(recompute_scene(scene_id, root, ident=ident, optical_guidance=optical_guidance))
    elif todo:
        module = _module()
        with _pool(workers, root) as pool:
            futures = {pool.submit(module._fit_worker, sid, str(root), ident, optical_guidance): sid for sid in todo}
            for future in as_completed(futures):
                try:
                    record(future.result())
                except Exception as error:  # noqa: BLE001 - a crashed worker is a recorded failure
                    record({'scene_id': futures[future], 'status': 'failed', 'error': f'{type(error).__name__}: {error}'[:500]})
    state.update(finished_at=now(), elapsed_s=round(time.perf_counter() - started, 1))
    _write_json(run_path, state)
    return state


# ----------------------------------------------------------------------------- compare

def summarise(diffs):
    """Counts with denominators over a list of per-object diffs."""
    total = len(diffs)
    changed = [d for d in diffs if not d['identical']]
    geometry = [d for d in diffs if d['geometry_changed']]
    fitted_after = [d for d in diffs if d['fitted'][1]]
    flat_after = [d for d in diffs if d['flat_fired']]
    quiet = [d for d in diffs if d['diagnosed'] and not d['flat_fired'] and not d['support_accepted'] and not d['guidance_accepted']
             and d['fitted'] == [True, True]]
    type_changed = [d for d in diffs if d['roof_type'][0] != d['roof_type'][1]]
    return {
        'buildings': total,
        'identical_all_fitter_fields': rate(total - len(changed), total),
        'geometry_changed': rate(len(geometry), total),
        'note_or_evidence_only': rate(len(changed) - len(geometry), total),
        'roof_type_changed': rate(len(type_changed), total),
        'roof_type_transitions': dict(Counter(f'{d["roof_type"][0]}->{d["roof_type"][1]}' for d in type_changed).most_common()),
        'roof_type_before': dict(Counter(str(d['roof_type'][0]) for d in diffs).most_common()),
        'roof_type_after': dict(Counter(str(d['roof_type'][1]) for d in diffs).most_common()),
        'attribution_of_geometry_changes': dict(Counter(d['attribution'] for d in geometry).most_common()),
        'flat': {'fired': rate(len(flat_after), total),
                 'decisions': dict(Counter(str(d['flat']['decision']) for d in diffs).most_common()),
                 'representation': dict(Counter(str(d['flat']['representation']) for d in diffs).most_common()),
                 'rules_when_fired': dict(Counter(str(d['flat']['rule']) for d in flat_after).most_common()),
                 'class_hint_when_fired': dict(Counter(str(d['flat']['class_hint']) for d in flat_after).most_common()),
                 'structure_flat_fired': sum(1 for d in flat_after if d['flat']['structure_flat']),
                 'level_surface_comparison_added': sum(1 for d in diffs if d['comparison_added']),
                 'roof_type_flat_before': sum(1 for d in diffs if d['roof_type'][0] == 'flat'),
                 'roof_type_flat_after': sum(1 for d in diffs if d['roof_type'][1] == 'flat'),
                 'pitched_to_flat': sum(1 for d in diffs if d['roof_type'][0] not in (None, 'flat') and d['roof_type'][1] == 'flat'),
                 'flat_to_pitched': sum(1 for d in diffs if d['roof_type'][0] == 'flat' and d['roof_type'][1] not in (None, 'flat'))},
        'topology': {'support_accepted': rate(sum(1 for d in diffs if d['support_accepted']), total),
                     'support_accepted_by': dict(Counter(str(d['support_accepted_by']) for d in diffs if d['support_accepted']).most_common()),
                     'support_shape': dict(Counter(str(d['support_shape']) for d in diffs if d['support_accepted']).most_common()),
                     'stage_one_simplification_accepted': sum(1 for d in diffs if d['topology_accepted'] and not d['support_accepted']),
                     'support_reasons': dict(Counter(str(d['support_reason']) for d in diffs if d['support_reason']).most_common(8))},
        'optical_guidance_accepted': sum(1 for d in diffs if d['guidance_accepted']),
        'nothing_fired': {'objects': len(quiet),
                          'geometry_identical': rate(sum(1 for d in quiet if not d['geometry_changed']), len(quiet)),
                          'all_fitter_fields_identical': rate(sum(1 for d in quiet if d['identical']), len(quiet)),
                          'note': 'fitted before and after; neither flat, topology support nor optical guidance fired'},
        'fits': {'fitted_before': sum(1 for d in diffs if d['fitted'][0]), 'fitted_after': len(fitted_after),
                 'gained': sum(1 for d in diffs if d['fitted'] == [False, True]), 'lost': sum(1 for d in diffs if d['fitted'] == [True, False]),
                 'errors_after': sum(1 for d in diffs if d['error'])},
        'planes': {'median_before': _median([d['planes'][0] for d in diffs if d['fitted'][0]]),
                   'median_after': _median([d['planes'][1] for d in fitted_after]),
                   'median_before_geometry_changed': _median([d['planes'][0] for d in geometry if d['fitted'][0]]),
                   'median_after_geometry_changed': _median([d['planes'][1] for d in geometry if d['fitted'][1]]),
                   'total_before': sum(d['planes'][0] for d in diffs), 'total_after': sum(d['planes'][1] for d in diffs)},
        'creases': {'median_before': _median([d['creases'][0] for d in diffs if d['fitted'][0]]),
                    'median_after': _median([d['creases'][1] for d in fitted_after]),
                    'median_before_geometry_changed': _median([d['creases'][0] for d in geometry if d['fitted'][0]]),
                    'median_after_geometry_changed': _median([d['creases'][1] for d in geometry if d['fitted'][1]]),
                    'total_before': sum(d['creases'][0] for d in diffs), 'total_after': sum(d['creases'][1] for d in diffs)},
        'form_uncertain_after': rate(sum(1 for d in diffs if d['form_uncertain']), total)}


def compare(root, records, ident):
    """Candidate vs production per object for every fresh candidate (reads promotion.json); writes nothing under data/scenes."""
    root = Path(root)
    rows, diffs, statuses = [], [], Counter()
    for record in records:
        if not record['eligible']:
            continue
        status = candidate_status(root, record, ident)
        statuses[status['status']] += 1
        if status['status'] != 'fresh':
            rows.append({'scene_id': record['scene_id'], 'group': record['group'], 'candidate': status['status'],
                         'apply': record['apply'], 'skip_reason': record['skip_reason']})
            continue
        diag = json.loads(candidate_paths(root, record['scene_id'])[1].read_bytes())
        scene_diffs = diag['object_diffs']
        diffs.extend((record['scene_id'], record['group'], record['apply'], d) for d in scene_diffs)
        rows.append({'scene_id': record['scene_id'], 'group': record['group'], 'kind': record['kind'], 'candidate': 'fresh',
                     'apply': record['apply'], 'skip_reason': record['skip_reason'], 'buildings': len(scene_diffs),
                     'objects_changed': diag['objects_changed'], 'geometry_changed': diag['geometry_changed'],
                     'roof_type_changed': sum(d['roof_type'][0] != d['roof_type'][1] for d in scene_diffs),
                     'flat_fired': sum(d['flat_fired'] for d in scene_diffs), 'support_accepted': sum(d['support_accepted'] for d in scene_diffs),
                     'undiagnosed': diag.get('undiagnosed'), 'model_signature_before': diag['source_model_signature'],
                     'model_signature_after': diag['candidate_model_signature'], 'profile_before': diag['profile_before'],
                     'profile_after': diag['profile_after'], 'roof_failures_before': diag.get('roof_failures_before'),
                     'roof_failures_after': diag.get('roof_failures'), 'elapsed_s': diag.get('elapsed_s'), 'hints': diag.get('hints')})

    def pick(**where):
        return [d for sid, group, applying, d in diffs
                if all({'group': group, 'apply': applying, 'owned': d.get('owned')}[k] == v for k, v in where.items())]
    summary = {'scenes': {'eligible': sum(1 for r in records if r['eligible']), 'candidates': dict(statuses),
                          'compared': sum(1 for r in rows if r['candidate'] == 'fresh')},
               'all_buildings': summarise(pick()),
               'owned_scan_roofs': summarise(pick(group='scan', owned=True)),
               'by_group': {g: summarise(pick(group=g)) for g in ('scan', 'capture')},
               'applying_scenes': summarise(pick(apply=True)),
               'profiles_after': dict(Counter(f"{r['group']}:{r['profile_after']}" for r in rows if r.get('profile_after'))),
               'profiles_before': dict(Counter(f"{r['group']}:{r['profile_before']}" for r in rows if r.get('profile_before'))),
               'elapsed_s': {'scenes': len([r for r in rows if r.get('elapsed_s')]),
                             'sum': round(sum(r['elapsed_s'] for r in rows if r.get('elapsed_s')), 1),
                             'per_building': round(sum(r['elapsed_s'] for r in rows if r.get('elapsed_s')) /
                                                   max(1, sum(r['buildings'] for r in rows if r.get('elapsed_s'))), 3)}}
    return summary, rows, [(sid, d) for sid, _, _, d in diffs]


def _crop_tile(image, obj, ring, to_pixel, colour, title, lines):
    import numpy as np
    from PIL import ImageDraw
    x0, y0 = np.maximum(ring.min(0) - 16, 0).astype(int)
    x1, y1 = np.minimum(ring.max(0) + 16, [image.width, image.height]).astype(int)
    scale = min(4., 300 / max(x1 - x0, y1 - y0, 1))
    size = (max(1, int((x1 - x0) * scale)), max(1, int((y1 - y0) * scale)))
    from PIL import Image
    crop = image.crop((x0, y0, x1, y1)).resize(size, Image.LANCZOS)
    draw = ImageDraw.Draw(crop)

    def px(points):
        xy = np.c_[np.asarray(points, dtype=float)[:, :2], np.ones(len(points))] @ to_pixel
        return [((x - x0) * scale, (y - y0) * scale) for x, y in xy]
    draw.polygon([((x - x0) * scale, (y - y0) * scale) for x, y in ring], outline=(235, 235, 235), width=1)
    roof_lines = obj.get('roof_lines') or []
    if not roof_lines:
        for part in obj.get('roof_parts') or []:
            draw.polygon(px(part['ring_meters']), outline=colour, width=2)
    for line in roof_lines:
        draw.line(px(line['line_meters']), fill=colour, width=3)
    draw.rectangle((0, 0, size[0], 14 * len(lines) + 4), fill=(0, 0, 0))
    for i, text in enumerate([title, *lines]):
        draw.text((4, 2 + 14 * i), text, fill=colour if i else (255, 255, 255))
    return crop


def contact_sheet(root, diffs, out_path, *, n=24, seed=20260927):
    """Random roofs whose geometry changed: production roof lines (orange) beside candidate roof lines (green) on the saved PNG."""
    import numpy as np
    from PIL import Image, ImageDraw
    root = Path(root)
    pool = sorted(((sid, d) for sid, d in diffs if d['geometry_changed'] and d['fitted'] == [True, True]),
                  key=lambda item: (item[0], item[1]['object_id']))
    picks = random.Random(seed).sample(pool, min(n, len(pool)))
    picks.sort(key=lambda item: (item[0], item[1]['object_id']))
    cache, pairs = {}, []
    for sid, diff in picks:
        if sid not in cache:
            folder = root / 'data/scenes' / sid
            production = json.loads((folder / 'latest.json').read_bytes())
            candidate = json.loads(candidate_paths(root, sid)[0].read_bytes())
            with Image.open(folder / f"{production['image_sha256']}.png") as opened:
                cache[sid] = (production, candidate, opened.convert('RGB'))
        production, candidate, image = cache[sid]
        to_pixel = np.asarray(production['meters_to_pixel'], dtype=float)
        before = next(o for o in production['objects'] if o['id'] == diff['object_id'])
        after = next(o for o in candidate['objects'] if o['id'] == diff['object_id'])
        ring = np.asarray(before['ring'], dtype=float)
        label = f"{sid.replace('mooroolbark-50011498-17-', 'tile ')} {diff['object_id'][:8]}"
        tag = diff['attribution'] + (f" ({diff['support_accepted_by']})" if diff['support_accepted'] else '') + \
            (f" rule {diff['flat']['rule']}" if diff['flat_fired'] else '')
        pairs.append((_crop_tile(image, before, ring, to_pixel, (255, 150, 0), label,
                                 [f"before {before.get('roof_type')} {diff['planes'][0]}p/{diff['creases'][0]}l"]),
                      _crop_tile(image, after, ring, to_pixel, (60, 235, 110), label,
                                 [f"after {after.get('roof_type')} {diff['planes'][1]}p/{diff['creases'][1]}l", tag])))
    if not pairs:
        return None
    cell_w = max(max(a.width, b.width) for a, b in pairs)
    cell_h = max(max(a.height, b.height) for a, b in pairs)
    columns = 4  # two roofs (before|after) per row
    rows = (len(pairs) + 1) // 2
    header = 40
    sheet = Image.new('RGB', (columns * cell_w, header + rows * cell_h), (18, 18, 26))
    draw = ImageDraw.Draw(sheet)
    draw.text((8, 6), f'{VERSION}: {len(pairs)} random roofs whose geometry changed (seed {seed}, pool {len(pool)}). '
                      'Orange = production (frozen fitter), green = promoted candidate, white = detector outline (unchanged).', fill=(255, 255, 255))
    draw.text((8, 22), 'Model estimates, not accuracy.', fill=(200, 200, 200))
    for i, (a, b) in enumerate(pairs):
        x, y = (i % 2) * 2 * cell_w, header + (i // 2) * cell_h
        sheet.paste(a, (x, y))
        sheet.paste(b, (x + cell_w, y))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return {'path': str(out_path), 'roofs': len(pairs), 'pool': len(pool), 'seed': seed,
            'picks': [{'scene_id': sid, 'object_id': d['object_id'], 'attribution': d['attribution'],
                       'roof_type': d['roof_type'], 'planes': d['planes'], 'creases': d['creases']} for sid, d in picks],
            'legend': 'orange = production roof lines, green = candidate roof lines (faces when a roof has no fold lines), white = detector outline'}


def render_dry_run(out):
    s = out['summary']
    a, o = s['all_buildings'], s['owned_scan_roofs']

    def line(name, block):
        return (f"{name}: {block['buildings']} buildings; geometry changed {block['geometry_changed']['n']} "
                f"({block['geometry_changed']['share']}), note/evidence only {block['note_or_evidence_only']['n']}, "
                f"identical {block['identical_all_fitter_fields']['n']}; roof_type changed {block['roof_type_changed']['n']}; "
                f"flat fired {block['flat']['fired']['n']}; topology support accepted {block['topology']['support_accepted']['n']} "
                f"{block['topology']['support_accepted_by']}")
    lines = [f'Roof fitter promotion dry run ({VERSION}) - {out["computed_at"]}', NOTE, '',
             f"profile_id: {out['profile_id']}", f"expected confidence profile: {out['expected_profile']}",
             f"scenes: {s['scenes']}", f"refit run: {out['run'].get('counts')} in {out['run'].get('elapsed_s')} s "
             f"({s['elapsed_s']['per_building']} s per building summed over workers)", '',
             line('all buildings', a), line('owned scan roofs', o),
             line('scan', s['by_group']['scan']), line('captures', s['by_group']['capture']),
             line('scenes that --apply would rewrite', s['applying_scenes']), '',
             f"roof_type transitions (all): {a['roof_type_transitions']}",
             f"attribution of geometry changes (all): {a['attribution_of_geometry_changes']}",
             f"flat (all): {a['flat']}", f"topology (all): {a['topology']}",
             f"nothing fired (all): {a['nothing_fired']}", f"fits (all): {a['fits']}",
             f"planes (all): {a['planes']}", f"creases (all): {a['creases']}",
             f"form uncertain after (all): {a['form_uncertain_after']}", '',
             f"contact sheet: {(out.get('contact_sheet') or {}).get('path')}", f"elapsed: {out['elapsed_s']} s"]
    return '\n'.join(lines) + '\n'


def dry_run(root=Path('.'), scene_ids=None, *, workers=1, optical_guidance=False, limit=None, sheet_size=24, seed=20260927, log=None):
    root = Path(root)
    started = time.perf_counter()
    report = plan(root, scene_ids, optical_guidance=optical_guidance)
    ident = {'profile_id': report['profile_id'], 'components': report['components']}
    records = report['scenes']
    run = compute_candidates(root, records, ident, workers=workers, optical_guidance=optical_guidance, limit=limit, log=log)
    summary, rows, diffs = compare(root, records, ident)
    sheet = contact_sheet(root, diffs, root / OUT_DIR / f'changed-{sheet_size}.png', n=sheet_size, seed=seed) if diffs else None
    out = {'version': VERSION, 'code_sha256': code_sha256(), 'computed_at': now(), 'note': NOTE, 'mode': 'dry-run',
           'profile_id': ident['profile_id'], 'expected_profile': report['expected_profile'], 'plan': report.get('path'),
           'candidate_root': str(root / CANDIDATE_DIR), 'run': {k: v for k, v in run.items() if k != 'results'},
           'run_failures': {sid: r for sid, r in run['results'].items() if r.get('status') != 'complete'},
           'summary': summary, 'contact_sheet': sheet, 'scenes': rows, 'elapsed_s': round(time.perf_counter() - started, 1),
           'wrote_under_data_scenes': False}
    _write_json(root / OUT_DIR / 'dry-run.json', out)
    _write_json(root / OUT_DIR / 'object-diffs.json', [{'scene_id': sid, **d} for sid, d in diffs])
    (root / OUT_DIR / 'dry-run.txt').write_text(render_dry_run(out))
    out['path'] = str(root / OUT_DIR / 'dry-run.json')
    return out


# ----------------------------------------------------------------------------- apply

def _profile_connection(root):
    from .scene_confidence import connection
    db = connection(Path(root))
    for ddl in PROFILE_DDL:
        db.execute(ddl)
    return db


def _live_rows(root, scene_id):
    path = Path(root) / CONFIDENCE_DB
    if not path.exists():
        return {'present': False, 'scene': None, 'roofs': []}
    with closing(connect_readonly(path)) as db:
        scene = db.execute('SELECT id, signature, mtime, bounds FROM scenes WHERE id=?', (scene_id,)).fetchone()
        roofs = db.execute('SELECT scene_id, object_id, west, south, east, north, payload FROM roofs WHERE scene_id=? ORDER BY object_id',
                           (scene_id,)).fetchall()
    return {'present': True, 'scene': list(scene) if scene else None, 'roofs': [list(r) for r in roofs]}


def _row_profile(payload):
    try:
        return json.loads(payload)['properties']['profile']
    except (ValueError, KeyError, TypeError):
        return None


def archive_profile_rows(root, rows, scene_profile, source, *, replace=False):
    """Copy live rows under their own profile into profile_roofs/profile_scenes. Frozen rows are archived with
    replace=False (an existing row is never overwritten); the new-profile rows use replace=True. Returns rows written."""
    if not rows['roofs'] and not rows['scene']:
        return {'roofs': 0, 'scene': 0}
    verb = 'INSERT OR REPLACE' if replace else 'INSERT OR IGNORE'
    added = {'roofs': 0, 'scene': 0}
    with closing(_profile_connection(root)) as db, db:
        for scene_id, object_id, west, south, east, north, payload in rows['roofs']:
            cursor = db.execute(f'{verb} INTO profile_roofs VALUES(?,?,?,?,?,?,?,?,?)',
                                (_row_profile(payload) or scene_profile, scene_id, object_id, west, south, east, north, payload, source))
            added['roofs'] += cursor.rowcount
        if rows['scene']:
            cursor = db.execute(f'{verb} INTO profile_scenes VALUES(?,?,?,?,?,?)', (scene_profile, *rows['scene'], source))
            added['scene'] += cursor.rowcount
    return added


def archive_all_live_rows(root, tag):
    """Copy every live confidence row (all scenes, under each row's own profile) into the profile tables; never overwrites.

    Run once before the first write so the frozen-profile population stays intact and queryable by profile after
    the live rows of promoted scenes are replaced. Rows are tagged with this apply's source tag for --rollback.
    """
    path = Path(root) / CONFIDENCE_DB
    if not path.exists():
        return {'roofs': 0, 'scenes': 0, 'by_profile': {}}
    with closing(connect_readonly(path)) as db:
        scenes = db.execute('SELECT id, signature, mtime, bounds FROM scenes ORDER BY id').fetchall()
        roofs = db.execute('SELECT scene_id, object_id, west, south, east, north, payload FROM roofs ORDER BY scene_id, object_id').fetchall()
    profiles = {}
    for row in roofs:
        profiles.setdefault(row[0], _row_profile(row[6]))
    added, by_profile = {'roofs': 0, 'scenes': 0}, Counter()
    with closing(_profile_connection(root)) as db, db:
        for row in roofs:
            profile = _row_profile(row[6]) or profiles.get(row[0]) or _scene_profile(root, row[0])
            count = db.execute('INSERT OR IGNORE INTO profile_roofs VALUES(?,?,?,?,?,?,?,?,?)', (profile, *row, tag)).rowcount
            added['roofs'] += count
            by_profile[profile[:16]] += count
        for scene_id, sig, mtime, bounds in scenes:
            profile = profiles.get(scene_id) or _scene_profile(root, scene_id)
            added['scenes'] += db.execute('INSERT OR IGNORE INTO profile_scenes VALUES(?,?,?,?,?,?)',
                                          (profile, scene_id, sig, mtime, bounds, tag)).rowcount
    return {**added, 'by_profile': dict(by_profile)}


def _scene_profile(root, scene_id):
    try:
        return profile_of(json.loads((Path(root) / 'data/scenes' / validate_id(scene_id) / 'latest.json').read_bytes()))
    except (OSError, ValueError):
        return 'unknown'


def drop_tagged_profile_rows(root, tag):
    if not (Path(root) / CONFIDENCE_DB).exists():
        return {'roofs': 0, 'scenes': 0}
    with closing(_profile_connection(root)) as db, db:
        return {'roofs': db.execute('DELETE FROM profile_roofs WHERE source=?', (tag,)).rowcount,
                'scenes': db.execute('DELETE FROM profile_scenes WHERE source=?', (tag,)).rowcount}


def _restore_live_rows(root, scene_id, saved):
    from .scene_confidence import connection
    with closing(connection(Path(root))) as db, db:
        db.execute('DELETE FROM roofs WHERE scene_id=?', (scene_id,))
        db.execute('DELETE FROM scenes WHERE id=?', (scene_id,))
        db.executemany('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', [tuple(r) for r in saved.get('roofs', [])])
        if saved.get('scene'):
            db.execute('INSERT INTO scenes VALUES(?,?,?,?)', tuple(saved['scene']))


def _drop_profile_rows(root, scene_id, profile, source):
    with closing(_profile_connection(root)) as db, db:
        roofs = db.execute('DELETE FROM profile_roofs WHERE profile=? AND scene_id=? AND source=?', (profile, scene_id, source)).rowcount
        scenes = db.execute('DELETE FROM profile_scenes WHERE profile=? AND id=? AND source=?', (profile, scene_id, source)).rowcount
    return {'roofs': roofs, 'scene': scenes}


def _indexed_scene_ids(root):
    path = Path(root) / CONFIDENCE_DB
    if not path.exists():
        return set()
    with closing(connect_readonly(path)) as db:
        return {r[0] for r in db.execute('SELECT id FROM scenes')}


def _restore_scene(root, entry):
    """Undo one scene's promotion from its backup; returns a list of problems (empty when restored)."""
    root = Path(root)
    folder = root / 'data/scenes' / entry['scene_id']
    backup = Path(entry['backup_dir'])
    problems = []
    data = (backup / 'latest.json').read_bytes()
    if sha256_bytes(data) != entry['before_sha256']:
        return ['backup latest.json altered']
    _write_bytes(folder / 'latest.json', data)
    os.utime(folder / 'latest.json', ns=(time.time_ns(), entry['before_mtime_ns']))
    snapshot = Path(entry['snapshot'])
    if entry.get('snapshot_created') and snapshot.exists() and sha256_file(snapshot) == entry['after_sha256']:
        snapshot.unlink()
    confidence = folder / 'confidence.json'
    if entry['confidence']['before_sha256'] is None:
        if confidence.exists():
            confidence.unlink()
    else:
        saved = (backup / 'confidence.json').read_bytes()
        if sha256_bytes(saved) != entry['confidence']['before_sha256']:
            problems.append('backup confidence.json altered')
        else:
            _write_bytes(confidence, saved)
    rows = json.loads((backup / 'confidence-rows.json').read_bytes())
    if entry['confidence'].get('indexed'):
        _restore_live_rows(root, entry['scene_id'], rows)
    if entry['confidence'].get('indexed'):
        _drop_profile_rows(root, entry['scene_id'], entry['profile_after'], entry['profile_rows']['source'])
    return problems


def promote_scene(root, record, ident, backup_dir, *, indexed, assess=None, tag=VERSION):
    """Rewrite one production latest.json from its fresh candidate, then assess. Returns (entry | None, skip reason | None)."""
    root = Path(root)
    scene_id = record['scene_id']
    folder = root / 'data/scenes' / scene_id
    latest_path, lock = folder / 'latest.json', folder / 'review.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        return None, 'review_locked_by_another_save'
    try:
        source = latest_path.read_bytes()
        production = json.loads(source)
        if (production.get('model_signature') != record['model_signature'] or sha256_bytes(source) != record['latest_sha256']):
            return None, 'signature_mismatch'
        if record['group'] == 'scan' and frozen_signatures(root).get(scene_id) != production.get('model_signature'):
            return None, 'signature_mismatch'
        status = candidate_status(root, record, ident)
        if status['status'] != 'fresh':
            return None, f"candidate_{status['status']}"
        candidate_path, diag_path = candidate_paths(root, scene_id)
        data, diag = candidate_path.read_bytes(), json.loads(diag_path.read_bytes())
        candidate = json.loads(data)
        if (sha256_bytes(data) != diag['candidate_sha256'] or candidate.get('model_signature') != model_signature(candidate)
                or non_roof_bytes(candidate) != non_roof_bytes(production) or scene_bytes(candidate) != data):
            return None, 'candidate_mismatch'
        review_before = review_state(folder, production)
        if review_before['present'] and (review_before.get('error') or review_before['human_touched']):
            return None, 'human_decided_review'
        scene_backup = Path(backup_dir) / scene_id
        scene_backup.mkdir(parents=True, exist_ok=True)
        (scene_backup / 'latest.json').write_bytes(source)
        confidence_path = folder / 'confidence.json'
        confidence_before = confidence_path.read_bytes() if confidence_path.exists() else None
        if confidence_before is not None:
            (scene_backup / 'confidence.json').write_bytes(confidence_before)
        rows_before = _live_rows(root, scene_id)
        _write_json(scene_backup / 'confidence-rows.json', rows_before)
        snapshot = folder / 'models' / f"{candidate['model_signature']}.json"
        entry = {'scene_id': scene_id, 'group': record['group'], 'kind': record['kind'], 'path': str(latest_path),
                 'backup_dir': str(scene_backup), 'before_sha256': sha256_bytes(source), 'after_sha256': sha256_bytes(data),
                 'before_mtime_ns': latest_path.stat().st_mtime_ns, 'model_signature_before': production['model_signature'],
                 'model_signature_after': candidate['model_signature'], 'profile_before': profile_of(production),
                 'profile_after': profile_of(candidate),
                 'inference_profile_before': (production.get('analysis') or {}).get('inference_profile'),
                 'inference_profile_after': (candidate.get('analysis') or {}).get('inference_profile'),
                 'snapshot': str(snapshot), 'snapshot_created': not snapshot.exists(),
                 'confidence': {'indexed': indexed, 'before_sha256': sha256_bytes(confidence_before) if confidence_before is not None else None,
                                'rows_before': len(rows_before['roofs'])},
                 'profile_rows': {'source': tag, 'added': {'roofs': 0, 'scene': 0}},
                 'review_json': {'present': review_before['present'], 'sha256_before': review_before['sha256'],
                                 'machine_labelled': review_before['machine_labelled']},
                 'status': 'pending'}
        _write_json(scene_backup / 'entry.json', entry)  # journal first: a crash from here on is recoverable by --rollback
        if entry['snapshot_created']:
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes(snapshot, data)
        _write_bytes(latest_path, data)
        entry['after_mtime_ns'] = latest_path.stat().st_mtime_ns
        entry['status'] = 'written'
        _write_json(scene_backup / 'entry.json', entry)
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass
    if assess is None:
        from .scene_confidence import assess
    try:
        report = assess(scene_id, root, index=indexed)
        if (folder / 'latest.json').read_bytes() != data:
            raise ValueError('latest.json changed during assessment')
    except Exception as error:  # noqa: BLE001 - keep production consistent: undo this scene, record the failure
        problems = _restore_scene(root, entry)
        entry.update(status='restored_after_assess_failure', restore_problems=problems)
        _write_json(Path(entry['backup_dir']) / 'entry.json', entry)
        return None, f'assess_failed: {type(error).__name__}: {error}'[:300]
    try:
        return _finish_entry(root, entry, report, diag, production, confidence_before, review_before, indexed, tag), None
    except Exception as error:  # noqa: BLE001 - production is consistent (written and assessed); keep the entry for the audit
        entry.update(status='complete', post_assess_error=f'{type(error).__name__}: {error}'[:300], object_diffs=[],
                     buildings=len(diag['object_diffs']), objects_changed=diag['objects_changed'], geometry_changed=diag['geometry_changed'])
        entry['confidence'].setdefault('owned_band_transitions', {})
        entry['review_json'].update(sha256_after=sha256_file(folder / 'review.json'), now_bound_to_previous_snapshot=bool(review_before['present']))
        entry['review_json']['untouched'] = entry['review_json']['sha256_after'] == review_before['sha256']
        _write_json(Path(entry['backup_dir']) / 'entry.json', entry)
        return entry, None


def _finish_entry(root, entry, report, diag, production, confidence_before, review_before, indexed, tag):
    folder = Path(root) / 'data/scenes' / entry['scene_id']
    scene_id = entry['scene_id']
    if indexed:
        live = _live_rows(root, scene_id)
        entry['profile_rows']['added'] = archive_profile_rows(root, live, entry['profile_after'], tag, replace=True)
    review_after = sha256_file(folder / 'review.json')
    before_bands = {}
    if confidence_before is not None:
        before_bands = {o['object_id']: o.get('band') for o in json.loads(confidence_before).get('objects', [])}
    after_bands = {o['object_id']: o.get('band') for o in report.get('objects', [])}
    owned = owned_ids(production)
    diffs = []
    for d in diag['object_diffs']:
        if d['identical'] and before_bands.get(d['object_id']) == after_bands.get(d['object_id']):
            continue
        diffs.append({k: d.get(k) for k in ('object_id', 'owned', 'fields_changed', 'geometry_changed', 'roof_type', 'source', 'planes', 'creases',
                                          'fitted', 'flat_fired', 'attribution', 'support_accepted', 'support_accepted_by')}
                     | {'flat_rule': d['flat']['rule'], 'band': [before_bands.get(d['object_id']), after_bands.get(d['object_id'])]})
    transitions = Counter(f'{before_bands.get(oid)}->{band}' for oid, band in after_bands.items() if oid in owned)
    entry.update(status='complete', confidence={**entry['confidence'], 'after_sha256': sha256_file(folder / 'confidence.json'),
                                                'version': report.get('version'), 'profile': report.get('profile'),
                                                'rows_after': len(_live_rows(root, scene_id)['roofs']) if indexed else 0,
                                                'owned_band_transitions': dict(transitions)},
                 review_json={**entry['review_json'], 'sha256_after': review_after,
                              'untouched': review_after == review_before['sha256'],
                              'now_bound_to_previous_snapshot': bool(review_before['present'])},
                 buildings=len(diag['object_diffs']), objects_changed=diag['objects_changed'], geometry_changed=diag['geometry_changed'],
                 object_diffs=diffs)
    _write_json(Path(entry['backup_dir']) / 'entry.json', entry)
    return entry


def _apply_worker(root, record, ident, backup_dir, indexed, tag):
    try:
        return record['scene_id'], *promote_scene(root, record, ident, backup_dir, indexed=indexed, tag=tag)
    except Exception as error:  # noqa: BLE001 - reported per scene; the journal in the backup folder allows rollback
        return record['scene_id'], None, f'failed: {type(error).__name__}: {error}'[:300]


def _acquire_extraction_lock(root):
    lock_path = Path(root) / 'data/scenes/.extraction.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise SystemExit(EXIT_BUSY) from error
    return handle


def run_guard(root, name):
    """eyes_guard.check (never records a baseline), stored under the promotion folder."""
    from . import eyes_guard
    report = eyes_guard.check(Path(root), write_baseline=False)
    path = Path(root) / OUT_DIR / f'guard-{name}.json'
    report['path'] = str(path)
    _write_json(path, report)
    return report


def guard_summary(report):
    return {'ok': report['ok'], 'path': report.get('path'), 'checks': {c['name']: c['ok'] for c in report['checks']},
            'failing': [{'name': c['name'], 'detail': {k: v for k, v in c['detail'].items() if k != 'files'}}
                        for c in report['checks'] if not c['ok']]}


def apply(root=Path('.'), scene_ids=None, *, acknowledged=False, optical_guidance=False, workers=1, guard=True, log=None,
          assess=None, skip_machine_labelled=False):
    """Live promotion: rewrite latest.json + analysis for every applicable scene with a fresh candidate, then assess."""
    if not acknowledged:
        raise SystemExit('Refusing to rewrite latest.json: pass --i-understand-this-rewrites-latest-json to run --apply live.')
    root = Path(root)
    started = time.perf_counter()
    lock = _acquire_extraction_lock(root)
    try:
        when = stamp()
        start_guard = None
        if guard:
            start_guard = run_guard(root, f'{when}-start')
            if not start_guard['ok']:
                raise SystemExit(f"eyes_guard fails before the promotion ({start_guard.get('path')}); nothing was written.")
        ident = identity(root, optical_guidance=optical_guidance)
        records = inventory(root, scene_ids, fitter=ident['components']['roof_fitter'], skip_machine_labelled=skip_machine_labelled)
        indexed = _indexed_scene_ids(root)
        tag = f'{VERSION}:{when}'
        backup_dir = root / OUT_DIR / f'backup-{when}'
        backup_dir.mkdir(parents=True, exist_ok=True)
        audit_path = root / OUT_DIR / f'apply-live-{when}.json'
        writes, skipped, skipped_scenes, processed = [], Counter(), [], [0]
        for record in records:
            if record['eligible'] and not record['apply']:
                skipped[record['skip_reason']] += 1
                skipped_scenes.append({'scene_id': record['scene_id'], 'group': record['group'], 'reason': record['skip_reason'],
                                       'protected_by': record['protected'] or None, 'reference_tiers': record['reference_tiers'] or None})
        todo = [r for r in records if r['apply']]
        todo.sort(key=lambda r: -r['buildings'])
        archived = archive_all_live_rows(root, tag) if todo else {'roofs': 0, 'scenes': 0, 'by_profile': {}}
        audit = {'version': VERSION, 'code_sha256': code_sha256(), 'mode': 'live', 'started_at': now(), 'note': NOTE,
                 'profile_id': ident['profile_id'], 'components': ident['components'], 'not_inputs': NOT_INPUTS,
                 'backup_dir': str(backup_dir), 'start_guard': guard_summary(start_guard) if start_guard else None,
                 'rollback': f'python -m scripts.ml.promote_profile --rollback {audit_path}', 'status': 'running', 'writes': writes,
                 'profile_rows': {'source': tag, 'archived_live_rows': archived}}

        def done(scene_id, entry, reason):
            processed[0] += 1
            if entry is None:
                skipped[reason.split(':')[0]] += 1
                skipped_scenes.append({'scene_id': scene_id, 'reason': reason})
            else:
                writes.append(entry)
            audit.update(files_rewritten=len(writes), skipped=dict(skipped), updated_at=now())
            # progress snapshot without per-object diffs (each scene's full entry is journalled in its backup folder)
            _write_json(audit_path, {**audit, 'writes': [{k: v for k, v in w.items() if k != 'object_diffs'} for w in writes],
                                     'skipped_scenes': skipped_scenes})
            if log:
                log(json.dumps({'done': f'{processed[0]}/{len(todo)}',
                                'scene_id': scene_id, 'status': 'rewritten' if entry else 'skipped', 'reason': reason,
                                'geometry_changed': (entry or {}).get('geometry_changed'),
                                'elapsed_s': round(time.perf_counter() - started, 1)}))
        if workers <= 1 or assess is not None:
            for record in todo:
                try:
                    entry, reason = promote_scene(root, record, ident, backup_dir, indexed=record['scene_id'] in indexed, assess=assess,
                                                  tag=tag)
                except Exception as error:  # noqa: BLE001 - the journal allows rollback
                    entry, reason = None, f'failed: {type(error).__name__}: {error}'[:300]
                done(record['scene_id'], entry, reason)
        elif todo:
            module = _module()
            with _pool(workers, root) as pool:
                futures = {pool.submit(module._apply_worker, str(root), r, ident, str(backup_dir), r['scene_id'] in indexed, tag):
                           r['scene_id'] for r in todo}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as error:  # noqa: BLE001 - a crashed worker; its journal (if any) allows rollback
                        result = (futures[future], None, f'failed: {type(error).__name__}: {error}'[:300])
                    done(*result)
    finally:
        lock.close()
    diffs = [d for w in writes for d in w['object_diffs']]
    audit.update(status='complete', finished_at=now(), elapsed_s=round(time.perf_counter() - started, 1),
                 by_group=dict(Counter(w['group'] for w in writes)),
                 profile_prefix={'scan': expected_profile(ident, 'scan')[:16], 'capture': expected_profile(ident, 'capture')[:16]},
                 profiles_after=dict(Counter(f"{w['group']}:{w['profile_after']}" for w in writes)),
                 profiles_before=dict(Counter(f"{w['group']}:{w['profile_before']}" for w in writes)),
                 buildings=sum(w['buildings'] for w in writes), objects_changed=sum(w['objects_changed'] for w in writes),
                 geometry_changed=sum(w['geometry_changed'] for w in writes),
                 roof_type_changed=sum(1 for d in diffs if d['roof_type'][0] != d['roof_type'][1]),
                 roof_type_transitions=dict(Counter(f'{d["roof_type"][0]}->{d["roof_type"][1]}' for d in diffs
                                                    if d['roof_type'][0] != d['roof_type'][1]).most_common()),
                 flat_fired=sum(1 for d in diffs if d['flat_fired']),
                 flat_rules=dict(Counter(str(d['flat_rule']) for d in diffs if d['flat_fired']).most_common()),
                 topology_support_accepted=sum(1 for d in diffs if d['support_accepted']),
                 topology_support_accepted_by=dict(Counter(str(d['support_accepted_by']) for d in diffs if d['support_accepted']).most_common()),
                 attribution_of_geometry_changes=dict(Counter(d['attribution'] for d in diffs if d['geometry_changed']).most_common()),
                 owned_band_transitions=dict(sum((Counter(w['confidence']['owned_band_transitions']) for w in writes), Counter()).most_common()),
                 reviews_untouched=all(w['review_json']['untouched'] for w in writes),
                 reviews_now_bound_to_previous_snapshot=sum(1 for w in writes if w['review_json']['now_bound_to_previous_snapshot']),
                 machine_labels_in_rewritten_reviews=sum(w['review_json']['machine_labelled'] or 0 for w in writes),
                 profile_rows={**audit['profile_rows'], 'added_new_roofs': sum(w['profile_rows']['added']['roofs'] for w in writes)},
                 skipped=dict(skipped), skipped_scenes=skipped_scenes)
    _write_json(audit_path, audit)
    if guard:
        audit['guard'] = guard_summary(run_guard(root, f'{when}-end'))
        _write_json(audit_path, audit)
    audit['audit_path'] = str(audit_path)
    report_path = root / OUT_DIR / f'report-{when}.txt'
    report_path.write_text(render_report(audit))
    audit['report_path'] = str(report_path)
    return audit


def rollback(root, audit_path, *, guard=True):
    """Restore every scene the audit (and its backup journal) lists: bytes, mtime, snapshot, confidence.json and index rows."""
    root = Path(root)
    audit = json.loads(Path(audit_path).read_bytes())
    entries = {w['scene_id']: w for w in audit.get('writes', [])}
    backup_dir = Path(audit.get('backup_dir', ''))
    for journal in sorted(backup_dir.glob('*/entry.json')) if backup_dir.is_dir() else []:
        entry = json.loads(journal.read_bytes())
        if entry['scene_id'] not in entries and entry.get('status') != 'restored_after_assess_failure':
            entries[entry['scene_id']] = entry  # journalled but never recorded in the audit (interrupted apply); sha decides below
    report = {'version': VERSION, 'mode': 'rollback', 'computed_at': now(), 'audit': str(audit_path), 'restored': 0,
              'skipped': Counter(), 'scenes': []}
    for scene_id, entry in sorted(entries.items()):
        latest = root / 'data/scenes' / scene_id / 'latest.json'
        current = latest.read_bytes() if latest.exists() else None
        if current is None or sha256_bytes(current) != entry['after_sha256']:
            report['skipped']['changed_since_write'] += 1
            report['scenes'].append({'scene_id': scene_id, 'action': 'skipped', 'reason': 'latest.json changed since the promotion'})
            continue
        problems = _restore_scene(root, entry)
        if problems:
            report['skipped']['backup_altered'] += 1
            report['scenes'].append({'scene_id': scene_id, 'action': 'partial', 'problems': problems})
            continue
        report['restored'] += 1
        report['scenes'].append({'scene_id': scene_id, 'action': 'restored', 'sha256': entry['before_sha256'],
                                 'review_json_sha256': sha256_file(latest.parent / 'review.json')})
    report['skipped'] = dict(report['skipped'])
    tag = (audit.get('profile_rows') or {}).get('source')
    if tag and not report['skipped']:
        report['profile_rows_dropped'] = drop_tagged_profile_rows(root, tag)
    elif tag:
        report['profile_rows_dropped'] = 'kept: some scenes were not restored, so the archived rows stay as the frozen record'
    if guard:
        report['guard'] = guard_summary(run_guard(root, f'rollback-{stamp()}'))
    out = Path(audit_path).with_name(f'rollback-{stamp()}.json')
    _write_json(out, report)
    report['report_path'] = str(out)
    return report


# ----------------------------------------------------------------------------- report

def render_report(audit):
    lines = [f"Roof fitter promotion ({VERSION}) - live apply {audit.get('started_at')} to {audit.get('finished_at')}", NOTE, '',
             f"profile_id (analysis.inference_profile of promoted scan tiles): {audit.get('profile_id')}",
             f"confidence profile prefix after: scan {audit['profile_prefix']['scan']}  capture {audit['profile_prefix']['capture']}",
             f"profiles before: {audit.get('profiles_before')}", f"profiles after: {audit.get('profiles_after')}", '',
             f"latest.json files rewritten: {audit.get('files_rewritten')} {audit.get('by_group')}",
             f"skipped: {audit.get('skipped')}",
             f"buildings in rewritten files: {audit.get('buildings')}; fitter fields changed on {audit.get('objects_changed')}; "
             f"geometry changed on {audit.get('geometry_changed')}",
             f"roof_type changes: {audit.get('roof_type_changed')} {audit.get('roof_type_transitions')}",
             f"flat hypothesis fired: {audit.get('flat_fired')} {audit.get('flat_rules')}",
             f"topology support accepted: {audit.get('topology_support_accepted')} {audit.get('topology_support_accepted_by')}",
             f"attribution of geometry changes: {audit.get('attribution_of_geometry_changes')}",
             f"owned roof band transitions (confidence re-assessed): {audit.get('owned_band_transitions')}",
             f"confidence.sqlite profile tables: {audit.get('profile_rows')}",
             f"review.json untouched: {audit.get('reviews_untouched')}; review files now bound to their previous snapshot: "
             f"{audit.get('reviews_now_bound_to_previous_snapshot')} (machine labels in them: {audit.get('machine_labels_in_rewritten_reviews')})",
             f"backup: {audit.get('backup_dir')}", f"elapsed: {audit.get('elapsed_s')} s",
             f"guard: ok={(audit.get('guard') or {}).get('ok')} failing={(audit.get('guard') or {}).get('failing')}",
             f"rollback: {audit.get('rollback')}"]
    for row in audit.get('skipped_scenes', []):
        extra = row.get('protected_by') or row.get('reference_tiers')
        lines.append(f"  skipped {row['scene_id']}: {row['reason']}" + (f' ({extra})' if extra else ''))
    return '\n'.join(lines) + '\n'


# ----------------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--rollback', metavar='AUDIT_JSON')
    parser.add_argument('--i-understand-this-rewrites-latest-json', dest='acknowledged', action='store_true')
    parser.add_argument('--scenes', nargs='+', help='Restrict to these scene ids (default: every saved scene).')
    parser.add_argument('--workers', type=int, default=max(1, min(8, (os.cpu_count() or 2) // 2)))
    parser.add_argument('--limit', type=int, help='Dry-run: refit at most this many scenes lacking a fresh candidate.')
    parser.add_argument('--optical-guidance', action='store_true',
                        help='Include the roof_optical_guidance stage in the promoted fitter (default: pinned off).')
    parser.add_argument('--skip-machine-labelled', action='store_true',
                        help='Also skip scenes whose review.json holds machine labels (default: rewrite them; the review file stays '
                             'byte-identical and binds to its retained previous snapshot).')
    parser.add_argument('--sheet-size', type=int, default=24)
    parser.add_argument('--seed', type=int, default=20260927)
    parser.add_argument('--no-guard', action='store_true')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    def log(line):
        print(line, flush=True)
    if args.rollback:
        report = rollback(root, args.rollback, guard=not args.no_guard)
        print(json.dumps({k: report[k] for k in ('restored', 'skipped', 'guard', 'report_path') if k in report}))
        return 0 if not report['skipped'] else 1
    if args.apply:
        try:
            audit = apply(root, args.scenes, acknowledged=args.acknowledged, optical_guidance=args.optical_guidance,
                          workers=args.workers, guard=not args.no_guard, log=log, skip_machine_labelled=args.skip_machine_labelled)
        except SystemExit as error:
            if error.code == EXIT_BUSY:
                sys.stderr.write('Another scene extraction is running. Retry when it finishes.\n')
                return EXIT_BUSY
            raise
        print(render_report(audit))
        print(json.dumps({'audit': audit['audit_path'], 'report': audit['report_path'], 'guard_ok': (audit.get('guard') or {}).get('ok')}))
        return 0
    if args.dry_run:
        out = dry_run(root, args.scenes, workers=args.workers, optical_guidance=args.optical_guidance, limit=args.limit,
                      sheet_size=args.sheet_size, seed=args.seed, log=log)
        print(render_dry_run(out))
        return 0
    report = plan(root, args.scenes, optical_guidance=args.optical_guidance, skip_machine_labelled=args.skip_machine_labelled)
    print(json.dumps({k: report[k] for k in ('profile_id', 'expected_profile', 'current_profile', 'detection_profile_matches_production',
                                             'counts', 'path')}, indent=1, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
