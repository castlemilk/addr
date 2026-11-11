"""teacher-check scores the hint layer: do the agent hints catch the themes a reviewer flagged?

('teacher' in scripts/ml/training/teacher_*.py means the hi-res SAM3 pseudo-label source; this module is the
check that the review-time hint layer would have caught what the reviewer caught.)

Every number here is agreement between a fired hint and the reviewer's theme, never accuracy: the reviewer's
notes are observations, the hints are model estimates, and n is small (the report says so with Wilson 95%
intervals). Fresh hints are recomputed from latest.json and the scene PNG (review.json is never read for the
hint side); the served state is read from the eyes index with the same query the editor uses. Nothing under
data/scenes is written; the report lands under data/reviews/eyes/teacher-checks/<date>/ and, with
--write-index, object-hints (scene_object_hints.VERSION) and lot-tags-v1 sidecar rows replace their scenes' rows of
those versions in the eyes index, and the harness scenes' object-hints rows of an earlier version are dropped
(scene_object_hints.write_scene_rows). Object-hints rows of an earlier version are still read as the served state
wherever they remain (the editor's route serves them where no current row exists) and are reported stale.

VERSION counts changes to the scoring and the report's shape (teacher-checks-v2 adds per-catch specificity: how many
objects of the caught object's kind in its scene the catching hint also fires on). Changes to THEME_EXPECTATIONS or
NEGATIVE_EXPECTATION are tracked by expectations_sha256 in every report, not by VERSION.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from . import eyes_index, scene_lot_tags, scene_object_hints, structure_kinds
from .scene_confidence import wilson
from .scene_object_quality import BUILDING_VERSION, class_hints_for_scene, load_image
from .scene_self_review import GEOMETRY_ERRORS, audit_scene, validate_id

VERSION = 'teacher-checks-v2'
REPORT_ROOT = Path('data/reviews/eyes/teacher-checks')
CHECKPOINTS_ROOT = Path('data/references/eyes-checkpoints')
CHECKPOINT_ID = re.compile(r'^[A-Z0-9][A-Z0-9-]{0,31}$')
SAMPLE_SEED = 20260930
SAMPLE_SIZE = 50
SECONDARY_KINDS = ('shed', 'garage', 'carport', 'attached-cover', 'pergola-or-shade', 'outbuilding', 'fragment')
# Codes that need the scene image. The first two are computed on harness scenes only (the sample runs without images to keep CPU
# short); solar_fixture_like's 'bright' reason is also judged on the sample, by a solar pass that loads each sample scene image.
IMAGE_CODES_HARNESS_ONLY = ('pool_not_blue', 'building_blends_into_surroundings')
IMAGE_CODES_WITH_SAMPLE_PASS = ('solar_fixture_like',)
IMAGE_CODES = IMAGE_CODES_HARNESS_ONLY + IMAGE_CODES_WITH_SAMPLE_PASS
ANY_REVIEW = 'any_review_hint'
# Versioned expectation table (hashed into the report): theme -> fired tokens that count as the hint layer catching it.
# Tokens: object-hints codes, lot-tags flags, self-review issue codes, 'class:<hint>' / 'flag:<flag>' (building-flags-v2),
# 'structure:<sub_kind>' (structure-kinds), 'kind:<kind>' (latest.json), 'roof_form:<decision>' and ANY_REVIEW.
THEME_EXPECTATIONS = {
    'frame-cut/centring/lot': ['frame_cut', 'lot_incomplete_in_frame', 'lot_unassociated'],
    'sub-division block': ['unit_site', 'common_property', 'spans_lots', 'lot_unassociated', 'secondary_by_lot'],
    'tree over-split': ['canopy_fragment_inside', 'tiny_area_for_kind'],
    'class error': ['pool_not_blue', 'building_blends_into_surroundings', 'tiny_area_for_kind', 'structure:fragment', 'solar_fixture_like'],
    'primary vs secondary': ['secondary_by_lot', *[f'structure:{kind}' for kind in SECONDARY_KINDS], 'flag:secondary-structure'],
    'noisy roof outline': ['noisy_outline', 'degenerate_outline'],
    'reflect': [ANY_REVIEW],
    # CP3 (2026-09-26) issue vocabulary
    'tree as building': ['class:tree-like', 'flag:tree-like', 'kind:tree'],
    'over-complex topology': ['complex_folds', 'image_line_disagreement', 'noisy_outline'],
    'flat roof modelled pitched': ['roof_form:flat', 'roof_type:flat'],
    'units should split': ['unit_site', 'spans_lots'],
    'non-residential': ['class:commercial', 'class:non-residential', 'flag:non-residential-context', 'flag:commercial'],
    # 2026-09-28 chat decisions
    'rejected outline': ['structure:fragment', 'degenerate_outline', 'tiny_area_for_kind', 'pool_not_blue', 'building_blends_into_surroundings'],
    'structure sub-kind': ['secondary_by_lot', *[f'structure:{kind}' for kind in SECONDARY_KINDS], 'flag:secondary-structure'],
}
NEGATIVE_EXPECTATION = {'no secondary': ['secondary_by_lot', *[f'structure:{kind}' for kind in SECONDARY_KINDS], 'flag:secondary-structure'],
                        'no review hint': [ANY_REVIEW]}
CP20_TAGS = {'frame-cut': 'frame-cut/centring/lot', 'centring': 'frame-cut/centring/lot', 'lot association': 'frame-cut/centring/lot',
             'lot': 'frame-cut/centring/lot', 'sub-division block': 'sub-division block', 'tree over-split': 'tree over-split',
             'class error': 'class error', 'primary vs secondary': 'primary vs secondary', 'noisy roof outline': 'noisy roof outline',
             'reflect': 'reflect'}
CP3_ISSUES = {'secondary-structure': 'primary vs secondary', 'primary-roof-exists': 'primary vs secondary', 'tree-as-building': 'tree as building',
              'over-complex-topology': 'over-complex topology', 'lines-clash-with-image': 'over-complex topology',
              'flat-roof-modelled-pitched': 'flat roof modelled pitched', 'units-should-split': 'units should split',
              'commercial': 'non-residential', 'non-residential': 'non-residential'}
CP3_INFORMATIONAL = ('user-edited', 'large-difference', 'next-to-oval', 'boundaries-not-rendered')


def code_sha256():
    files = [Path(__file__), Path(__file__).with_name('scene_object_hints.py'), Path(__file__).with_name('scene_lot_tags.py')]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def expectations_sha256():
    return hashlib.sha256(json.dumps({'themes': THEME_EXPECTATIONS, 'negatives': NEGATIVE_EXPECTATION}, sort_keys=True).encode()).hexdigest()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- Normalisation ----------------------------------------------------------------------------------------------

def _case(scene_id, object_id, themes, *, negative=False, source='', note='', informational=False, **extra):
    validate_id(scene_id)
    return {'scene_id': scene_id, 'object_id': str(object_id), 'themes': list(dict.fromkeys(themes)), 'negative': bool(negative),
            'source': source, 'note': note, 'informational': bool(informational), **extra}


def normalise(doc, source='feedback'):
    """[{scene_id, object_id, themes[], negative, source, note, ...}] over the three feedback schemas."""
    cases = []
    if isinstance(doc.get('decisions'), list) and doc.get('source') == 'human:chat':
        for decision in doc['decisions']:   # CP20: bracket tag in the notes names the theme(s)
            note = str(decision.get('notes') or '')
            match = re.search(r'\[reviewer [^,\]]*,\s*([^\]]+)\]', note)
            tag = match.group(1) if match else ''
            themes = [CP20_TAGS[part.strip()] for part in tag.split('/') if part.strip() in CP20_TAGS]
            cases.append(_case(decision['scene'], decision['object'], themes or ['reflect'], source=source, note=note, tag=tag,
                               status=decision.get('status'), structure_kind=decision.get('structure_kind')))
    elif isinstance(doc.get('items'), list):
        for item in doc['items']:   # CP3: issues[] vocabulary
            issues = [str(i) for i in item.get('issues') or []]
            themes = [CP3_ISSUES[i] for i in issues if i in CP3_ISSUES]
            informational = not themes and all(i in CP3_INFORMATIONAL for i in issues)
            cases.append(_case(item['scene_id'], item['object_id'], themes, source=source, note=str(item.get('note') or ''),
                               issues=issues, informational=informational))
    elif isinstance(doc.get('decisions'), list):
        for decision in doc['decisions']:   # 2026-09-28: structure_kind and/or status
            kind, status = decision.get('structure_kind'), decision.get('status')
            note = str(decision.get('notes') or '')
            if status == 'confirmed' or kind == 'not-secondary':
                cases.append(_case(decision['scene'], decision['object'], ['no secondary', 'no review hint'], negative=True, source=source,
                                   note=note, status=status, structure_kind=kind))
                continue
            themes = []
            if kind:
                themes.append('structure sub-kind')
            if status == 'rejected':
                themes.append('rejected outline')
            cases.append(_case(decision['scene'], decision['object'], themes, source=source, note=note, status=status, structure_kind=kind))
    else:
        raise ValueError('Unknown feedback schema')
    return cases


def load_sets(paths):
    cases, files = [], []
    for path in paths:
        path = Path(path)
        doc = json.loads(path.read_text())
        files.append({'path': str(path), 'sha256': sha256_file(path)})
        cases.extend(normalise(doc, source=path.name))
    return cases, files


def expected_tokens(case):
    tokens = set()
    if case['negative']:
        for theme in case['themes']:
            tokens.update(NEGATIVE_EXPECTATION.get(theme, ()))
        return tokens
    for theme in case['themes']:
        tokens.update(THEME_EXPECTATIONS.get(theme, ()))
    if case.get('structure_kind') and case.get('structure_kind') != 'not-secondary':
        tokens.add(f"structure:{case['structure_kind']}")
    return tokens


# --- Fresh computation ----------------------------------------------------------------------------------------------

def load_scene(root, scene_id):
    return json.loads((Path(root)/'data/scenes'/validate_id(scene_id)/'latest.json').read_text())


def fresh_scene(scene, root, *, image=None, offline=False, self_review=True, class_hints=True, structure=True):
    """Every fresh signal of one scene, image loaded once: {'objects': {id: {tokens, review_count, metrics}}, 'lot_rows', 'hint_rows', ...}."""
    root = Path(root)
    image = load_image(root, scene) if image is None else image
    parcels, digest, source = scene_lot_tags.parcels_for_scene(scene, root, offline=offline)
    if parcels:
        lot_tags, scene_tag = scene_lot_tags.analyse(scene, parcels, seed=scene_lot_tags.address_seed(scene, root))
    else:
        lot_tags, scene_tag = {}, None
    flags = {}
    if class_hints:
        try:
            flags = class_hints_for_scene(scene, image=image, root=root).get('flag_payloads') or {}
        except (OSError, ValueError, KeyError, TypeError, *GEOMETRY_ERRORS) as error:
            flags = {'_error': str(error)}
    structure_rows = {}
    if structure:
        try:
            structure_rows = {r['object_id']: r['payload'] for r in structure_kinds.assess_scene(scene, image, {(scene['id'], k): v for k, v in flags.items() if k != '_error'})}
        except (OSError, ValueError, KeyError, TypeError, *GEOMETRY_ERRORS) as error:
            structure_rows = {'_error': str(error)}
    hint_rows = scene_object_hints.hints_for_scene(scene, image, lot_tags=lot_tags, scene_tag=scene_tag, parcels_sha256=digest,
                                                   structure_rows={k: v for k, v in structure_rows.items() if k != '_error'},
                                                   flags={k: v for k, v in flags.items() if k != '_error'}, root=root)
    audit = {}
    if self_review and image is not None:
        try:
            for row in audit_scene(scene, Image.fromarray(image)):
                audit[row['object_id']] = row
        except (OSError, ValueError, KeyError, TypeError, *GEOMETRY_ERRORS) as error:
            audit = {'_error': str(error)}
    objects = {}
    for row in hint_rows:
        oid, payload = row['object_id'], row['payload']
        obj = next((o for o in scene['objects'] if o['id'] == oid), {})
        tokens = set(payload.get('codes') or [])
        if payload.get('review_count'):
            tokens.add(ANY_REVIEW)
        tag = lot_tags.get(oid) or {}
        tokens.update(tag.get('flags') or [])
        for issue in (audit.get(oid) or {}).get('issues') or []:
            tokens.add(issue['code'])
            if issue.get('severity') == 'review':
                tokens.add(ANY_REVIEW)
        payload_flags = flags.get(oid) or {}
        hints = (payload_flags.get('class_hints') or {}) if isinstance(payload_flags, dict) else {}
        if hints.get('class_hint'):
            tokens.add(f"class:{hints['class_hint']}")
        for flag in hints.get('flags') or []:
            if isinstance(flag, dict) and flag.get('flag'):
                tokens.add(f"flag:{flag['flag']}")
        structure_row = structure_rows.get(oid) if isinstance(structure_rows.get(oid), dict) else None
        if structure_row and structure_row.get('sub_kind'):
            tokens.add(f"structure:{structure_row['sub_kind']}")
        tokens.add(f"kind:{obj.get('kind')}")
        if obj.get('roof_type'):
            tokens.add(f"roof_type:{obj['roof_type']}")
        decision = (obj.get('roof_form_evidence') or {}).get('decision')
        if decision:
            tokens.add(f'roof_form:{decision}')
        metrics = {'kind': obj.get('kind'), 'status': obj.get('status'), 'area_m2': payload.get('area_m2'), 'edge_px': tag.get('edge_px'),
                   'lot': scene_object_hints.lot_name(tag.get('dominant')) if tag.get('dominant') else None,
                   'lot_complete_in_image': tag.get('lot_complete_in_image'), 'primary_ratio': tag.get('primary_ratio'),
                   'vertices': len(obj.get('ring') or []), 'roof_parts': len(obj.get('roof_parts') or []),
                   'colour': payload.get('colour'), 'outline': {k: (payload.get('outline') or {}).get(k) for k in ('svpm', 'wonky_share', 'rectilinearity')} if payload.get('outline') else None,
                   'solar': payload.get('solar'),
                   'structure_hint': structure_row.get('sub_kind') if structure_row else None,
                   'class_hint': hints.get('class_hint'), 'roof_form_decision': decision}
        objects[oid] = {'tokens': sorted(tokens), 'review_count': payload.get('review_count') or 0, 'metrics': metrics,
                        'sentences': [h['sentence'] for h in payload.get('hints') or [] if h['severity'] == 'review']}
    return {'objects': objects, 'hint_rows': hint_rows, 'lot_tags': lot_tags, 'scene_tag': scene_tag, 'parcels_source': source,
            'parcels_sha256': digest, 'image': image is not None, 'audit_error': audit.get('_error'), 'flags_error': flags.get('_error'),
            'structure_error': structure_rows.get('_error'), 'parcels': parcels}


# --- Served state ---------------------------------------------------------------------------------------------------

SERVED_VERSIONS = {'class_hints': [BUILDING_VERSION], 'structure': [structure_kinds.VERSION, 'structure-kinds-v2', 'structure-kinds-v1'],
                   'object_hints': [scene_object_hints.VERSION, *scene_object_hints.PREVIOUS_VERSIONS], 'lot': [scene_lot_tags.VERSION]}
# Parts whose earlier-version rows are served (as the editor's route does) but always reported stale: an object-hints row of an
# earlier version predates the current rules (object-hints-v1 has no solar_fixture_like), whatever its model signature.
STALE_WHEN_OLDER = ('object_hints',)
SERVED_SQL = ('SELECT payload, model_signature FROM object_quality WHERE version = ? AND scene_id = ? AND object_id = ? '
              'ORDER BY (model_signature = ?) DESC LIMIT 1')


def served_state(root, cases_by_scene, signatures):
    """Per object and part: 'current' | 'stale' | 'missing' with the served tokens and the version served, using the editor's query."""
    root = Path(root)
    out = {}
    if not eyes_index.path(root).exists():
        return out
    with closing(eyes_index.connection(root, readonly=True)) as db:
        present = eyes_index.tables_present(db)
        if 'object_quality' not in present:
            return out
        for scene_id, object_ids in cases_by_scene.items():
            current = signatures.get(scene_id) or ''
            for oid in object_ids:
                state, tokens, served_versions = {}, set(), {}
                for part, versions in SERVED_VERSIONS.items():
                    state[part] = 'missing'
                    for version in versions:
                        row = db.execute(SERVED_SQL, (version, scene_id, oid, current)).fetchone()
                        if not row:
                            continue
                        try:
                            payload = json.loads(row[0])
                        except (TypeError, ValueError):
                            continue
                        older = part in STALE_WHEN_OLDER and version != versions[0]
                        state[part] = 'current' if row[1] == current and not older else 'stale'
                        served_versions[part] = version
                        if part == 'object_hints':
                            tokens.update(payload.get('codes') or [h.get('code') for h in payload.get('hints') or [] if isinstance(h, dict)])
                            if payload.get('review_count') or any(isinstance(h, dict) and h.get('severity') == 'review' for h in payload.get('hints') or []):
                                tokens.add(ANY_REVIEW)
                        elif part == 'lot':
                            tokens.update((payload.get('tag') or {}).get('flags') or [])
                        elif part == 'structure' and payload.get('sub_kind'):
                            tokens.add(f"structure:{payload['sub_kind']}")
                        elif part == 'class_hints':
                            hints = payload.get('class_hints') or {}
                            if hints.get('class_hint'):
                                tokens.add(f"class:{hints['class_hint']}")
                            tokens.update(f"flag:{f['flag']}" for f in hints.get('flags') or [] if isinstance(f, dict) and f.get('flag'))
                        break
                out[(scene_id, oid)] = {'state': state, 'tokens': sorted(tokens), 'versions': served_versions}
    return out


# --- Scoring --------------------------------------------------------------------------------------------------------

def _interval(successes, total):
    bounds = wilson(int(successes), int(total)) if total else None
    return {'count': int(successes), 'denominator': int(total), 'share': round(successes/total, 4) if total else None,
            'wilson_95': [round(b, 4) for b in bounds] if bounds else None}


def successor_of(scene, object_id):
    for obj in scene.get('objects', []):
        if object_id in (obj.get('replaces') or []) or object_id in (obj.get('replacement_ids') or []):
            return obj['id']
    return None


def catch_specificity(case, hits, fresh_objects, scene):
    """{token: {kind, fired, of, share}} per catching token: how many objects of the caught object's kind in its scene (latest.json
    kinds over the objects the fresh pass scored) fire the same token. A share of 1.0 over two or more objects means the hint agreed
    with the reviewer's theme without singling the object out in its scene."""
    kinds = {o['id']: o.get('kind') for o in scene.get('objects', [])}
    kind = kinds.get(case['object_id'])
    same = [oid for oid in fresh_objects if kinds.get(oid) == kind]
    out = {}
    for token in hits:
        fired = sum(token in (fresh_objects[oid].get('tokens') or []) for oid in same)
        out[token] = {'kind': kind, 'fired': fired, 'of': len(same), 'share': round(fired/len(same), 4) if same else None}
    return out


def score_cases(cases, fresh, served, scenes):
    """Per case: expected tokens, fired (fresh and served), caught flags, and for a fresh catch how specific the catching hints are
    in the scene (catch_specificity; non_specific_catch when each fires on every object of that kind, two or more); object_missing
    when latest.json lacks the object."""
    scored = []
    for case in cases:
        scene = scenes.get(case['scene_id'])
        expected = sorted(expected_tokens(case))
        entry = {**case, 'expected': expected}
        present = bool(scene) and any(o['id'] == case['object_id'] for o in scene.get('objects', []))
        if not present:
            successor = successor_of(scene, case['object_id']) if scene else None
            entry.update(status_check='object_missing', successor=successor,
                         successor_fresh=(fresh.get(case['scene_id']) or {}).get('objects', {}).get(successor, {}).get('tokens') if successor else None)
            scored.append(entry)
            continue
        fresh_objects = (fresh.get(case['scene_id']) or {}).get('objects', {})
        fresh_obj = fresh_objects.get(case['object_id']) or {}
        fresh_tokens = set(fresh_obj.get('tokens') or [])
        served_obj = served.get((case['scene_id'], case['object_id'])) or {}
        served_tokens = set(served_obj.get('tokens') or [])
        hit_fresh, hit_served = sorted(fresh_tokens & set(expected)), sorted(served_tokens & set(expected))
        if case['negative']:
            entry.update(status_check='negative', over_flag_fresh=bool(hit_fresh), over_flag_served=bool(hit_served))
        elif case.get('informational') or not expected:
            entry.update(status_check='informational')
        else:
            specificity = catch_specificity(case, hit_fresh, fresh_objects, scene) if hit_fresh else {}
            entry.update(status_check='scored', caught_fresh=bool(hit_fresh), caught_served=bool(hit_served), catch_specificity=specificity,
                         non_specific_catch=all(v['share'] == 1. and v['of'] >= 2 for v in specificity.values()) if hit_fresh else None)
        entry.update(fired_fresh=sorted(fresh_tokens), fired_served=sorted(served_tokens), hit_fresh=hit_fresh, hit_served=hit_served,
                     served_state=served_obj.get('state'), served_versions=served_obj.get('versions'), metrics=fresh_obj.get('metrics'),
                     sentences=fresh_obj.get('sentences'))
        scored.append(entry)
    return scored


def summarise(scored):
    per_theme = defaultdict(lambda: {'fresh': [0, 0], 'served': [0, 0]})
    overall = {'fresh': [0, 0], 'served': [0, 0]}
    negatives = {'fresh': [0, 0], 'served': [0, 0]}
    per_source = defaultdict(lambda: {'fresh': [0, 0], 'served': [0, 0], 'negatives': 0, 'informational': 0, 'object_missing': 0})
    misses = []
    non_specific = {'fresh': 0, 'caught_fresh': 0, 'cases': []}
    for entry in scored:
        source = per_source[entry['source']]
        if entry['status_check'] == 'scored':
            for mode in ('fresh', 'served'):
                caught = entry[f'caught_{mode}']
                overall[mode][1] += 1
                overall[mode][0] += caught
                source[mode][1] += 1
                source[mode][0] += caught
                for theme in entry['themes']:
                    per_theme[theme][mode][1] += 1
                    per_theme[theme][mode][0] += caught
            if not entry['caught_fresh']:
                misses.append({k: entry.get(k) for k in ('scene_id', 'object_id', 'themes', 'expected', 'fired_fresh', 'metrics', 'note', 'source')})
            else:
                non_specific['caught_fresh'] += 1
                if entry.get('non_specific_catch'):
                    non_specific['fresh'] += 1
                    non_specific['cases'].append([entry['scene_id'], entry['object_id']])
        elif entry['status_check'] == 'negative':
            source['negatives'] += 1
            for mode in ('fresh', 'served'):
                negatives[mode][1] += 1
                negatives[mode][0] += entry[f'over_flag_{mode}']
        else:
            source[entry['status_check']] += 1
    return {'overall': {mode: _interval(*overall[mode]) for mode in overall},
            'per_theme': {theme: {mode: _interval(*values[mode]) for mode in values} for theme, values in sorted(per_theme.items())},
            'negatives': {'over_flag': {mode: _interval(*negatives[mode]) for mode in negatives},
                          'note': 'n is the number of reviewer negatives; the interval is wide by construction'},
            'per_source': {source: {'fresh': _interval(*v['fresh']), 'served': _interval(*v['served']), 'negatives': v['negatives'],
                                    'informational': v['informational'], 'object_missing': v['object_missing']} for source, v in per_source.items()},
            'misses': misses, 'non_specific_catches': non_specific}


# --- Population rates ---------------------------------------------------------------------------------------------

def pixel_class(scene):
    px = scene_object_hints._pixel_scale(scene)
    return '<0.08' if px < .08 else '0.08-0.2' if px < .2 else '>=0.2'


# Codes whose population rate is reported on its own, per source: solar_fixture_like needs the scene image for its brightness reason
# and the JSON-only sample rows carry none, so solar_rates reads the harness rows and a sample pass with images instead.
SEPARATE_RATES = ('solar_fixture_like',)


def solar_rates(rows_by_scene, scenes=None):
    """solar_fixture_like over live solar_panel rows with a valid outline: flagged / solar panels with a Wilson 95% interval, the
    count per reason, the rows whose brightness was not judged (no image, or no pixel inside the outline), the invalid outlines left
    out and, given the scenes, the same count per pixel class (reported per class, never compared across)."""
    flagged = total = invalid = unjudged = 0
    reasons, seen, classes = Counter(), set(), defaultdict(lambda: [0, 0])
    for scene_id, rows in rows_by_scene.items():
        klass = pixel_class(scenes[scene_id]) if scenes and scene_id in scenes else None
        for row in rows:
            payload = row['payload']
            if payload.get('kind') != 'solar_panel':
                continue
            if payload.get('area_m2') is None:
                invalid += 1
                continue
            total += 1
            seen.add(scene_id)
            solar = payload.get('solar') or {}
            if solar.get('median_luminance') is None:
                unjudged += 1
            hit = 'solar_fixture_like' in (payload.get('codes') or [])
            if hit:
                flagged += 1
                reasons.update(solar.get('reasons') or [])
            if klass:
                classes[klass][0] += hit
                classes[klass][1] += 1
    out = {**_interval(flagged, total), 'scenes': len(seen), 'by_reason': dict(sorted(reasons.items())), 'brightness_not_judged': unjudged,
           'invalid_outline': invalid}
    if scenes:
        out['by_pixel_class'] = {k: {'flagged': v[0], 'solar_panels': v[1], 'share': round(v[0]/v[1], 4) if v[1] else None} for k, v in sorted(classes.items())}
    return out


def population_rates(rows_by_scene, scenes):
    """Per code: flagged / denominator over the objects the code applies to, plus noisy_outline and squared coverage per pixel class."""
    kinds_for = {'pool_not_blue': ('pool',), 'canopy_fragment_inside': ('tree', 'vegetation'), 'building_blends_into_surroundings': ('building',),
                 'secondary_by_lot': ('building',), 'noisy_outline': ('building',), 'roof_fit_on_fragment': ('building',),
                 'solar_fixture_like': ('solar_panel',)}
    flagged, denominators = Counter(), Counter()
    per_kind_tiny = defaultdict(lambda: [0, 0])
    noisy, squared = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0, 0])
    lot_members, lot_secondary, buildings_with_lot, lot_incomplete = 0, 0, 0, 0
    for scene_id, rows in rows_by_scene.items():
        scene = scenes[scene_id]
        klass = pixel_class(scene)
        for row in rows:
            payload = row['payload']
            kind, codes, area = payload.get('kind'), set(payload.get('codes') or []), payload.get('area_m2')
            for code in codes:
                flagged[code] += 1
            denominators['objects'] += 1
            if kind == 'building':
                denominators['buildings'] += 1
                for code in ('frame_cut', 'lot_unassociated', 'spans_lots', 'degenerate_outline', 'lot_incomplete_in_frame'):
                    if code in codes:
                        flagged[f'{code}@buildings'] += 1
                lot = payload.get('lot') or {}
                if lot.get('dominant'):
                    buildings_with_lot += 1
                    if lot.get('lot_complete_in_image') is False:
                        lot_incomplete += 1
                members = lot.get('lot_members') or []
                if members and any(m['object_id'] == row['object_id'] for m in members):
                    lot_members += 1
                    if 'secondary_by_lot' in codes:
                        lot_secondary += 1
                if area is not None and area >= scene_object_hints.DESIGN['noisy_outline']['area_m2']:
                    noisy[klass][1] += 1
                    noisy[klass][0] += 'noisy_outline' in codes
                    candidate = payload.get('squared_candidate') or {}
                    squared[klass][1] += 1
                    squared[klass][0] += bool(candidate.get('ring'))
                    squared[klass][2] += bool(candidate.get('refused'))
            for code, kinds in kinds_for.items():
                if kind in kinds:
                    denominators[code] += 1
            if kind in scene_object_hints.DESIGN['tiny_area_for_kind']:
                per_kind_tiny[kind][1] += 1
                per_kind_tiny[kind][0] += 'tiny_area_for_kind' in codes
    rates = {}
    for code in sorted(flagged):
        if '@' in code or code in SEPARATE_RATES:
            continue
        denominator = denominators.get(code) or denominators['objects']
        rates[code] = {'flagged': flagged[code], 'denominator': denominator, 'share': round(flagged[code]/denominator, 4) if denominator else None,
                       'denominator_kind': 'objects of the applicable kinds' if code in kinds_for else 'live objects'}
        if f'{code}@buildings' in flagged or code in ('frame_cut', 'lot_unassociated', 'spans_lots', 'degenerate_outline', 'lot_incomplete_in_frame'):
            rates[code]['buildings'] = {'flagged': flagged.get(f'{code}@buildings', 0), 'denominator': denominators['buildings'],
                                        'share': round(flagged.get(f'{code}@buildings', 0)/denominators['buildings'], 4) if denominators['buildings'] else None}
    rates['tiny_area_for_kind_by_kind'] = {kind: {'flagged': v[0], 'denominator': v[1], 'share': round(v[0]/v[1], 4) if v[1] else None} for kind, v in sorted(per_kind_tiny.items())}
    rates['noisy_outline_by_pixel_class'] = {k: {'flagged': v[0], 'buildings_20m2': v[1], 'share': round(v[0]/v[1], 4) if v[1] else None} for k, v in sorted(noisy.items())}
    rates['squared_candidate_by_pixel_class'] = {k: {'with_candidate': v[0], 'refused': v[2], 'buildings_20m2': v[1], 'coverage': round(v[0]/v[1], 4) if v[1] else None}
                                                 for k, v in sorted(squared.items())}
    rates['secondary_by_lot_members'] = {'secondary': lot_secondary, 'lot_members': lot_members, 'share': round(lot_secondary/lot_members, 4) if lot_members else None}
    rates['lot_incomplete_in_frame_buildings_with_lot'] = {'incomplete': lot_incomplete, 'buildings_with_lot': buildings_with_lot,
                                                          'share': round(lot_incomplete/buildings_with_lot, 4) if buildings_with_lot else None}
    rates['denominators'] = dict(denominators)
    rates['note'] = 'Population flag rates are the over-flag proxy until reviewer negatives accumulate; they compare nothing across pixel classes.'
    return rates


def sample_scenes(root, exclude, size=SAMPLE_SIZE, seed=SAMPLE_SEED):
    folder = Path(root)/'data/scenes'
    ids = sorted(p.name for p in folder.iterdir() if p.is_dir() and (p/'latest.json').exists() and not p.name.startswith(('.', 'qa_'))
                 and p.name not in exclude and p.name not in ('house-30286', 'house-30296'))
    rng = random.Random(seed)
    return rng.sample(ids, min(size, len(ids)))


# --- Reflections (Step 16) -------------------------------------------------------------------------------------------

REFLECTIONS = [
    {'scene_id': 'house-25910', 'object_id': '816ff673852a41c9', 'theme': 'reflect',
     'reflection': ('A 4.8 m² building with a hipped roof of 8 parts, clipped_by_frame [256, 256, 768, 768]: a sliver of the neighbour\'s roof '
                    'left by the old 512 px frame. The widened scene re-detected the full roof, but the .95 building takeover in '
                    'scene_extract did not fire because the fuller detection abuts rather than covers it; then a roof model was fitted '
                    'to 4.8 m². Caught now by frame_cut, roof_fit_on_fragment and secondary_by_lot. Measured, not applied: a roof-fitting '
                    'gate for buildings < 6 m² or with a fragment hint, and a remnant takeover in scene_expand when a clipped seed < 6 m² '
                    'is >= .8 covered by a new candidate.')},
    {'scene_id': 'roof_training_250-29501', 'object_id': '4fb089fc5f936f20', 'theme': 'noisy roof outline',
     'reflection': ('87 m², 54 vertices; the mask followed shadow and the two-level roof line. noisy_outline and lot_unassociated say so; '
                    'no measured evidence for a shadow-edge detector exists, so the sentence says "check the shadow side" and nothing more. '
                    'The squared candidate is offered as a dashed overlay only; the repair path is the reviewer\'s redraw.')},
    {'scene_id': 'roof_training_250-29501', 'object_id': '587e522a2986ccea', 'theme': 'noisy roof outline',
     'reflection': ('0.04 m², 4 vertices, 0.24 x 0.26 m: a degenerate ring that survives the pixel-area floor at 0.059 m/px. Today only '
                    'uncheckable_geometry (audit_object raises below 1 m²); now degenerate_outline and tiny_area_for_kind. The post-assembly '
                    'building floor in scene_extract removes it from new extractions.')},
]


def reflections(fresh):
    out = []
    for record in REFLECTIONS:
        obj = (fresh.get(record['scene_id']) or {}).get('objects', {}).get(record['object_id']) or {}
        out.append({**record, 'measured': obj.get('metrics'), 'fired_fresh': obj.get('tokens'), 'sentences': obj.get('sentences'),
                    'note': 'Reflection record, never written to review.json.'})
    return out


# --- Report -----------------------------------------------------------------------------------------------------------

def _md(report):
    lines = [f"# Teacher-check {report['date']} ({VERSION})", '',
             'Agreement between fired agent hints and the reviewer\'s themes; never accuracy. Hints are never labels. n is small; Wilson 95% intervals are shown.', '']
    for mode in ('fresh', 'served'):
        value = report['summary']['overall'][mode]
        lines.append(f"- Overall {mode}: {value['count']}/{value['denominator']} caught (share {value['share']}, Wilson 95% {value['wilson_95']})")
    specific = report['summary'].get('non_specific_catches')
    if specific:
        lines.append(f"- Non-specific fresh catches: {specific['fresh']} of {specific['caught_fresh']} caught fresh (every catching hint also fires on "
                     f"all of the two or more objects of that kind in the scene; see Catches)")
    lines += ['', '## Per theme', '', '| theme | fresh | served | Wilson 95% (fresh) |', '|---|---|---|---|']
    for theme, values in report['summary']['per_theme'].items():
        f, s = values['fresh'], values['served']
        lines.append(f"| {theme} | {f['count']}/{f['denominator']} | {s['count']}/{s['denominator']} | {f['wilson_95']} |")
    lines += ['', '## Per feedback set', '']
    for source, values in report['summary']['per_source'].items():
        f, s = values['fresh'], values['served']
        lines.append(f"- {source}: fresh {f['count']}/{f['denominator']}, served {s['count']}/{s['denominator']}, negatives {values['negatives']}, "
                     f"informational {values['informational']}, object_missing {values['object_missing']}")
    neg = report['summary']['negatives']['over_flag']
    lines += ['', '## Negatives (over-flag on reviewer negatives)', '',
              f"- fresh {neg['fresh']['count']}/{neg['fresh']['denominator']} over-flagged (Wilson 95% {neg['fresh']['wilson_95']}); served {neg['served']['count']}/{neg['served']['denominator']}",
              f"- {report['summary']['negatives']['note']}", '', '## Misses (fresh)', '']
    for miss in report['summary']['misses'] or []:
        lines.append(f"- {miss['scene_id']} {miss['object_id']} themes {miss['themes']}: expected {miss['expected']}, fired {miss['fired_fresh']}; metrics {json.dumps(miss['metrics'], default=str)}")
    if not report['summary']['misses']:
        lines.append('- none')
    lines += ['', '## Catches (fresh) and how specific they are', '',
              'Per caught case: how many objects of its kind in the same scene the catching hint also fires on. A non-specific catch is one '
              'where every catching hint fires on all of them (two or more): the hint agreed with the reviewer\'s theme without singling '
              'the object out.', '']
    caught = [e for e in report['per_case'] if e.get('status_check') == 'scored' and e.get('caught_fresh')]
    for entry in caught:
        parts = [f"{token} fires on {v['fired']}/{v['of']} {v['kind']} in the scene" if i == 0 else f"{token} on {v['fired']}/{v['of']}"
                 for i, (token, v) in enumerate((entry.get('catch_specificity') or {}).items())]
        lines.append(f"- {entry['scene_id']} {entry['object_id']} [{', '.join(entry['themes'])}]: {', '.join(parts)}"
                     f"{'; non-specific catch' if entry.get('non_specific_catch') else ''}")
    if not caught:
        lines.append('- none')
    lines += ['', '## Population flag rates (harness scenes + seeded sample; over-flag proxy)', '']
    for code, value in report['population_rates'].items():
        if isinstance(value, dict) and 'share' in value and 'flagged' in value:
            extra = f"; buildings {value['buildings']['flagged']}/{value['buildings']['denominator']} ({value['buildings']['share']})" if 'buildings' in value else ''
            lines.append(f"- {code}: {value['flagged']}/{value['denominator']} ({value['share']}){extra}")
    lines.append(f"- noisy_outline by pixel class (reported per class, not compared across): {json.dumps(report['population_rates']['noisy_outline_by_pixel_class'])}")
    lines.append(f"- squared candidate coverage by pixel class: {json.dumps(report['population_rates']['squared_candidate_by_pixel_class'])}")
    lines.append(f"- tiny_area_for_kind by kind: {json.dumps(report['population_rates']['tiny_area_for_kind_by_kind'])}")
    lines.append(f"- secondary_by_lot among lot members: {json.dumps(report['population_rates']['secondary_by_lot_members'])}")
    lines.append(f"- lot_incomplete_in_frame among buildings with a lot: {json.dumps(report['population_rates']['lot_incomplete_in_frame_buildings_with_lot'])}")
    solar = report['population_rates'].get('solar_fixture_like')
    if solar:
        lines += ['', '## solar_fixture_like over solar_panel objects (population flag rate; over-flag proxy)', '']
        for key, label in (('harness', 'harness scenes'), ('sample', 'seeded sample')):
            value = solar.get(key)
            if value:
                lines.append(f"- {label}: {value['count']}/{value['denominator']} flagged (share {value['share']}, Wilson 95% {value['wilson_95']}); "
                             f"by reason {json.dumps(value.get('by_reason') or {})}; {value.get('scenes')} scenes; "
                             f"brightness not judged (no image or no pixel inside) {value.get('brightness_not_judged')}; invalid outlines left out {value.get('invalid_outline')}")
        for key, label in (('harness', 'harness scenes'), ('sample', 'seeded sample')):
            classes = (solar.get(key) or {}).get('by_pixel_class')
            if classes:
                lines.append(f"- {label} by pixel class (m/px; reported per class, not compared across): {json.dumps(classes)}")
        if solar.get('design'):
            lines.append(f"- design values (rectangle fill floor, scene grey percentile ceiling, area floor in m²; not fitted to these rates, see Honesty): "
                         f"{json.dumps(solar['design'])}")
        lines.append('- A flag rate says how often the hint fires, never accuracy; flagged objects are agent hints for a reviewer, not labels.')
    lines += ['', '## Served state (index rows vs latest.json)', '']
    for key, value in sorted(report['served_state_counts'].items()):
        lines.append(f"- {key}: {json.dumps(value)}")
    lines += ['', '## Honesty', '']
    lines += [f'- {line}' for line in report['honesty']]
    return '\n'.join(lines)+'\n'


HONESTY = [
    'Thresholds are design values chosen with the reviewer objects visible (16 for object-hints-v1; the two 2026-09-30 evening solar cases for '
    'solar_fixture_like, object-hints-v2); population rates are the only over-flag measure; any change is object-hints-v3.',
    'solar_fixture_like compares the median grey inside a solar_panel outline with the scene\'s own 60th-percentile grey, not an absolute 150: '
    'at 150 the reviewer\'s hot-water unit 5bac1a267bec9d1f (median grey 148) stays quiet; that choice was made with the two positives in view. '
    'The ceiling is frame-relative (83 to 186 grey levels over the 524 scenes with a live solar panel, median 131), so the same object can '
    'flag in one frame and not in another. The sentence names what to check (hot-water unit, air-conditioner, skylight), never what the '
    'object is; nothing is suggested or applied.',
    'Visual spot check of bright-only solar_fixture_like fires (read-only crops, 2026-09-30 evening): at >= 0.2 m/px the review\'s 8 crops '
    'were all real panel arrays, and a second sample of 6 held 4 real arrays, one whole white roof and one blue pool-like object; at 0.08-0.2 '
    'm/px 4 of 8 and 1 to 3 of 6 (two unclear) were real arrays; below 0.08 m/px mostly rooftop fixtures, dark strips on white roofs (darker '
    'than the roof, brighter than the frame) or whole-roof class errors. The flag rate therefore counts many real panels at coarse pixels, '
    'and the bright reason, the weakest of the three, is the one that caught both reviewer cases; refining it (bright only with a neutral '
    'colour, a ceiling from roof pixels, or no bright reason at >= 0.2 m/px) is an object-hints-v3 decision.',
    'A catch is agreement with the reviewer\'s theme, not discrimination: the Catches section marks a non-specific catch when every catching '
    'hint fires on all the objects of that kind in the scene (solar_fixture_like fires on all five solar panels of roof_training_250-29761, '
    'the two the reviewer rejected and three saved as needs_adjustment with no recorded provenance).',
    'Object-hints rows of an earlier version are served where no current row exists and reported stale until --write-index rewrites the '
    'scene, which drops them.',
    'Cohort frames are registered by least squares from the CRM roof; boundaries there carry "alignment unverified".',
    'Vicmap Parcel has no fence lines, party walls, envelopes, easements or stacked-strata cuts; cutting blocks by fence lines is imagery model work, not a data download.',
    'pool_not_blue is a statement about the swimming-pool prompt\'s output; the sentence says "reads like grass", never "is grass".',
    'Google z20 lot frames (0.14-0.17 m/px) are coarser than the 0.065 m/px cohort crops; the 1024 px expansion is upsampled.',
    'The Unit 4 title vs Lot 1 seed on 63 Cambridge Rd is surfaced as a hint, never corrected.',
]


def image_codes_note(solar_scenes, without_image):
    return (f'{IMAGE_CODES_HARNESS_ONLY} were computed on harness scenes only (the sample ran without images to keep CPU short). '
            f'{IMAGE_CODES_WITH_SAMPLE_PASS} needs the image for its bright reason: the solar pass loaded each sample scene image for '
            f'solar_panel rows only ({solar_scenes} sample scenes with a solar panel, {without_image} without a usable image, where '
            f'brightness is not judged).')


def run(sets, root=Path('.'), *, offline=False, write_index=False, sample_size=SAMPLE_SIZE, self_review=True, log=None, date=None):
    root = Path(root)
    started = time.monotonic()
    cases, files = load_sets(sets)
    scene_ids = list(dict.fromkeys(c['scene_id'] for c in cases))
    scenes, fresh, timings = {}, {}, {}
    for scene_id in scene_ids:
        path = root/'data/scenes'/validate_id(scene_id)/'latest.json'
        if not path.exists():
            if log:
                log(f'{scene_id}: no latest.json; its cases are object_missing')
            continue
        scenes[scene_id] = json.loads(path.read_text())
        t0 = time.monotonic()
        fresh[scene_id] = fresh_scene(scenes[scene_id], root, offline=offline, self_review=self_review)
        timings[scene_id] = round(time.monotonic()-t0, 1)
        if log:
            counts = Counter(c for o in fresh[scene_id]['objects'].values() for c in o['tokens'] if ':' not in c and c != ANY_REVIEW)
            log(f"{scene_id}: {timings[scene_id]} s, parcels {fresh[scene_id]['parcels_source']}, image {fresh[scene_id]['image']}, "
                f"{len(fresh[scene_id]['objects'])} objects, {dict(counts.most_common(8))}")
    cases_by_scene = defaultdict(set)
    for case in cases:
        cases_by_scene[case['scene_id']].add(case['object_id'])
    signatures = {sid: s.get('model_signature') for sid, s in scenes.items()}
    served_before = served_state(root, cases_by_scene, signatures)
    scored = score_cases(cases, fresh, served_before, scenes)
    summary = summarise(scored)
    # Population rates: harness scenes (image codes included) plus a seeded JSON-only sample.
    rows_by_scene = {sid: f['hint_rows'] for sid, f in fresh.items()}
    sample_ids = sample_scenes(root, set(scene_ids), size=sample_size) if sample_size else []
    sample_rows, sample_scenes_loaded, sample_solar, sample_solar_no_image = {}, {}, {}, 0
    for scene_id in sample_ids:
        try:
            scene = load_scene(root, scene_id)
            parcels, digest, _ = scene_lot_tags.parcels_for_scene(scene, root, offline=True)
            sample_rows[scene_id] = scene_object_hints.hints_for_scene(scene, None, parcels=parcels, parcels_sha256=digest, root=root)
            sample_scenes_loaded[scene_id] = scene
            # The solar pass: the scene image for solar_panel rows only, so the brightness reason is judged on the sample too.
            if any(o.get('kind') == 'solar_panel' for o in scene_object_hints._live(scene)):
                image = load_image(root, scene)
                sample_solar_no_image += image is None
                sample_solar[scene_id] = (scene_object_hints.hints_for_scene(scene, image, root=root, kinds=('solar_panel',)) if image is not None
                                          else [r for r in sample_rows[scene_id] if r['payload'].get('kind') == 'solar_panel'])
        except (OSError, ValueError, KeyError, TypeError, *GEOMETRY_ERRORS) as error:
            if log:
                log(f'sample {scene_id}: skipped ({error})')
    rates = population_rates({**rows_by_scene, **sample_rows}, {**scenes, **sample_scenes_loaded})
    rates['harness_scenes'] = len(rows_by_scene)
    rates['sample_scenes'] = len(sample_rows)
    rates['image_codes_note'] = image_codes_note(len(sample_solar), sample_solar_no_image)
    rates['solar_fixture_like'] = {
        'harness': solar_rates(rows_by_scene, scenes), 'sample': solar_rates(sample_solar, sample_scenes_loaded),
        'design': scene_object_hints.DESIGN['solar_fixture_like'],
        'note': ('How often solar_fixture_like fires over solar_panel objects (over-flag proxy), never accuracy. The sample loads each '
                 'scene image for this pass only; the other image codes stay harness-only.')}
    served_counts = Counter()
    for value in served_before.values():
        for part, state in value['state'].items():
            served_counts[f'{part}:{state}'] += 1
        if (value.get('versions') or {}).get('object_hints'):
            served_counts[f"object_hints@{value['versions']['object_hints']}"] += 1
    written = {}
    if write_index:
        hint_rows = [row for f in fresh.values() for row in f['hint_rows']]
        lot_rows = []
        for scene_id, f in fresh.items():
            if f['parcels'] is not None:
                rows, _ = scene_lot_tags.scene_rows(scenes[scene_id], root, offline=True, parcels=f['parcels'])
                lot_rows.extend(rows)
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            written['object_hints'], written['object_hints_previous_removed'] = scene_object_hints.write_scene_rows(db, list(fresh), hint_rows)
            written['lot_tags'] = scene_lot_tags.replace_scene_rows(db, scene_lot_tags.TABLE, scene_lot_tags.VERSION,
                                                                     [sid for sid, f in fresh.items() if f['parcels'] is not None], lot_rows)
        served_after = served_state(root, cases_by_scene, signatures)
        scored_after = score_cases(cases, fresh, served_after, scenes)
        summary_after = summarise(scored_after)
        summary['served_after_write'] = summary_after['overall']['served']
        summary['per_theme_served_after_write'] = {theme: v['served'] for theme, v in summary_after['per_theme'].items()}
        for entry, after in zip(scored, scored_after, strict=True):
            entry['caught_served_after_write'] = after.get('caught_served')
            entry['served_state_after_write'] = after.get('served_state')
    date = date or datetime.now(UTC).strftime('%Y-%m-%d')
    report = {'version': VERSION, 'date': date, 'computed_at': datetime.now(UTC).isoformat(), 'code_sha256': code_sha256(),
              'expectations_sha256': expectations_sha256(), 'feedback_files': files, 'theme_expectations': THEME_EXPECTATIONS,
              'negative_expectations': NEGATIVE_EXPECTATION,
              'thresholds': {'object_hints': scene_object_hints.DESIGN, 'lot_tags': scene_lot_tags.DESIGN},
              'cases': len(cases), 'scenes': scene_ids, 'timings_s': timings, 'offline': offline,
              'parcels_sources': {sid: f['parcels_source'] for sid, f in fresh.items()},
              'errors': {sid: {k: f[k] for k in ('audit_error', 'flags_error', 'structure_error') if f.get(k)} for sid, f in fresh.items() if any(f.get(k) for k in ('audit_error', 'flags_error', 'structure_error'))},
              'summary': summary, 'per_case': scored, 'population_rates': rates, 'sample_scene_ids': sample_ids,
              'served_state_counts': dict(served_counts), 'written': written, 'reflections': reflections(fresh), 'honesty': HONESTY,
              'wording': 'agreement with the reviewer\'s theme, never accuracy; hints are never labels',
              'elapsed_s': round(time.monotonic()-started, 1)}
    return report


def write_report(report, root=Path('.')):
    folder = Path(root)/REPORT_ROOT/report['date']
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'report.json').write_text(json.dumps(report, indent=1, default=str))
    (folder/'summary.json').write_text(json.dumps({k: report[k] for k in ('version', 'date', 'code_sha256', 'expectations_sha256', 'summary',
                                                                           'population_rates', 'served_state_counts', 'written')}, indent=1, default=str))
    (folder/'report.md').write_text(_md(report))
    with (folder/'cases.jsonl').open('w') as handle:
        for entry in report['per_case']:
            handle.write(json.dumps(entry, default=str)+'\n')
    shutil.copyfile(folder/'cases.jsonl', folder/'rows.jsonl')
    (folder/'reflections.json').write_text(json.dumps(report['reflections'], indent=1, default=str))
    return folder


def checkpoint_copy(report, checkpoint, root=Path('.')):
    if not CHECKPOINT_ID.match(checkpoint):
        raise ValueError('Checkpoint ids are like CP21')
    folder = Path(root)/CHECKPOINTS_ROOT/checkpoint
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/'teacher-checks.json'
    target.write_text(json.dumps(report, indent=1, default=str))
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', default='.')
    parser.add_argument('--sets', nargs='+', required=True, help='Feedback JSON files (CP20, CP3, 2026-09-28 schemas).')
    parser.add_argument('--offline', action='store_true', help='Frozen parcel caches only; scenes without them report parcels unavailable.')
    parser.add_argument('--write-index', action='store_true',
                        help=f'Replace {scene_object_hints.VERSION} and {scene_lot_tags.VERSION} rows of the harness scenes in the eyes index '
                             f'(their {", ".join(scene_object_hints.PREVIOUS_VERSIONS)} rows are dropped).')
    parser.add_argument('--require-all', metavar='SET', help='Exit 1 when any scored case of this set (file name prefix, e.g. CP20) is uncaught fresh.')
    parser.add_argument('--checkpoint', help='Copy report.json to data/references/eyes-checkpoints/<ID>/teacher-checks.json (no eyes checkpoints row, no CRM publish).')
    parser.add_argument('--sample', type=int, default=SAMPLE_SIZE, help='Seeded JSON-only sample size for population rates (0 to skip).')
    parser.add_argument('--no-self-review', action='store_true', help='Skip scene_self_review.audit_scene (faster).')
    parser.add_argument('--date', help='Report folder name (default UTC today).')
    args = parser.parse_args(argv)
    root = Path(args.root)
    log = lambda line: print(line, file=sys.stderr, flush=True)  # noqa: E731
    report = run(args.sets, root, offline=args.offline, write_index=args.write_index, sample_size=args.sample,
                 self_review=not args.no_self_review, log=log, date=args.date)
    folder = write_report(report, root)
    if args.checkpoint:
        checkpoint_copy(report, args.checkpoint, root)
    summary = report['summary']
    print(json.dumps({'version': VERSION, 'report': str(folder), 'overall': summary['overall'], 'per_source': summary['per_source'],
                      'negatives': summary['negatives']['over_flag'], 'served_after_write': summary.get('served_after_write'),
                      'misses': [(m['scene_id'], m['object_id']) for m in summary['misses']], 'written': report['written']}, indent=1))
    if args.require_all:
        prefix = args.require_all.upper()
        uncaught = [e for e in report['per_case'] if e['status_check'] == 'scored' and not e['caught_fresh']
                    and (e['source'].upper().startswith(prefix) or prefix in e['source'].upper() or prefix in str(next((f['path'] for f in report['feedback_files'] if f['path'].endswith(e['source'])), '')).upper())]
        if uncaught:
            print(f"--require-all {args.require_all}: {len(uncaught)} uncaught fresh: {[(e['scene_id'], e['object_id']) for e in uncaught]}", file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
