"""Offline case snapshots built from human-reviewed scene objects; the loop's task set.

    data/harness/cases/index.json               case id -> task, domain, split, change
    data/harness/cases/<case>/source/case.json  what the agent may see: frame, class, MACHINE outline(s)
    data/harness/cases/<case>/source/image.png  the original source frame the reviewer saw
    data/harness/cases/<case>/heldout/*.json    the human outline or decisions; read only by the judge

An outline case starts from the model outline the reviewer saw (`models/<signature>.json`); its
held-out reference is the reviewer's saved outline. A detection case shows one class family's
machine detections; its held-out decisions are the reviewer's confirm/reject/relabel calls and the
objects the reviewer added. Machine-provenance, pending and carried-undecided objects are never
references. Building cases reads scenes only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path

from ..review_authority import carried_undecided, review_authority_blocker
from ..scene_polygon_feedback import _outline

VERSION = 'addr-harness-case-v1'
ROOT = Path('data/harness/cases')
FAMILIES = {'building': 'roof', 'tree': 'vegetation', 'vegetation': 'vegetation', 'solar_panel': 'structure',
            'pool': 'structure', 'water': 'structure', 'driveway': 'paving', 'road': 'paving', 'footpath': 'paving',
            'car': 'vehicle'}
MACHINE_FIELDS = ('label', 'score', 'prompt', 'source', 'model', 'clipped_by_frame')
HELDOUT_GLOBS = ('heldout/*', 'heldout', 'reference.json', 'decisions.json')
# Checkpoint reference scenes (roof_review_loop.PROTECTED): never a harness case or a review-queue item.
PROTECTED_SCENES = frozenset({'house-30286', 'house-30296'})
MAX_DETECTIONS = 40
# Notes on many confirmed outlines record that an assistant drew them before a reviewer confirmed them.
AGENT_DRAWN = re.compile(r'\b(assistant|codex)\b', re.I)
SIMPLIFIED_MAX_VERTICES = 5


def reference_provenance(obj):
    """Who drew a held-out outline, and whether it is a simplification rather than a boundary trace."""
    agent = bool(AGENT_DRAWN.search(str(obj.get('notes') or '')))
    vertices = len(obj.get('ring') or [])
    return {'reference_origin': 'agent-drawn, human-confirmed' if agent else 'human', 'reference_vertices': vertices,
            'boundary_reference': not (agent and vertices <= SIMPLIFIED_MAX_VERTICES)}


def family_of(kind):
    return FAMILIES.get(kind, kind or 'unknown')


SPLITS = ('train', 'val', 'test')


def split_of(scene_id):
    """Scene-held-out split. `test` is the repo's split (roof_corner_model.split_of: one scene in five);
    `val` is a second fifth carved out of its train, used only to gate harness changes."""
    bucket = int(hashlib.sha256(scene_id.encode()).hexdigest(), 16) % 5
    return 'test' if bucket == 0 else 'val' if bucket == 1 else 'train'


def pixel_size_m(frame):
    (a, b), (c, d) = frame['pixel_to_meters'][0], frame['pixel_to_meters'][1]
    return float(math.sqrt(abs(a*d - b*c)))


def outline_of(obj):
    return {'ring': obj['ring'], 'holes': obj.get('holes') or []}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _keep(token, ratio):
    """Deterministic thinning: keep `ratio` of the items whose token hashes below it."""
    return int(hashlib.sha256(token.encode()).hexdigest()[:8], 16)/0xFFFFFFFF < ratio


def _usable(obj):
    return review_authority_blocker(obj) is None and not carried_undecided(obj) and len(obj.get('ring') or []) >= 3


def _traceable(obj, model):
    """Whether the production polygon tools accept this outline (vertex caps, bounds, validity)."""
    try:
        _outline(outline_of(obj), model['image_width'], model['image_height'])
    except (ValueError, TypeError):
        return False
    return True


def scene_sources(root):
    """Yield (scene_id, model, review, image path) for every scene whose reviewed model and image exist."""
    for folder in sorted((Path(root)/'data/scenes').iterdir()):
        if folder.name in PROTECTED_SCENES:
            continue
        review_path, latest_path = folder/'review.json', folder/'latest.json'
        if not review_path.is_file() or review_path.is_symlink() or not latest_path.is_file():
            continue
        review = json.loads(review_path.read_text())
        signature = review.get('model_signature')
        model_path = folder/'models'/f'{signature}.json'
        if model_path.is_file():
            model = json.loads(model_path.read_text())
        else:
            model = json.loads(latest_path.read_text())
            if model.get('model_signature') != signature:
                continue
        image = folder/f'{model.get("image_sha256")}.png'
        if image.is_file() and model.get('image_width') and model.get('pixel_to_meters'):
            yield folder.name, model, review, image


def frame_of(model):
    return {key: model[key] for key in ('image_width', 'image_height', 'pixel_to_meters', 'meters_to_pixel')}


def outline_cases(scene_id, model, review):
    """(case, heldout) pairs for human-checked outlines whose machine source the reviewer saw."""
    machine = {obj['id']: obj for obj in model.get('objects', []) if obj.get('id')}
    for obj in review.get('objects', []):
        source = machine.get(obj.get('id'))
        if (source is None or not obj.get('geometry_reviewed') or obj.get('status') in ('rejected', 'superseded')
                or not _usable(obj) or not _traceable(source, model)):
            continue
        start, reference = outline_of(source), outline_of(obj)
        family = family_of(obj.get('kind'))
        case_id = f'o-{scene_id}-{obj["id"]}'
        case = {'version': VERSION, 'case_id': case_id, 'task': 'outline', 'scene_id': scene_id, 'object_id': obj['id'],
                'kind': obj.get('kind'), 'family': family, 'domain': f'{family}-outline', 'image': 'image.png',
                'image_sha256': model['image_sha256'], 'source_frame': frame_of(model),
                'pixel_size_m': pixel_size_m(model), 'start_outline_pixels': start,
                'machine': {key: source[key] for key in MACHINE_FIELDS if key in source}}
        heldout = {'version': VERSION, 'case_id': case_id, 'reference_outline_pixels': reference,
                   'change': 'unchanged' if reference == start else 'edited', 'human_status': obj.get('status'),
                   'review_revision': review.get('revision'), 'model_kind': source.get('kind'),
                   **reference_provenance(obj)}
        yield case, heldout


def detection_cases(scene_id, model, review):
    """(case, heldout) per class family with at least one human rejection, relabel or addition."""
    machine = {obj['id']: obj for obj in model.get('objects', []) if obj.get('id')}
    reviewed = {obj['id']: obj for obj in review.get('objects', []) if obj.get('id')}
    families = sorted({family_of(o.get('kind')) for o in [*machine.values(), *reviewed.values()]})
    for family in families:
        detections, decisions, added = [], {}, []
        for object_id, source in machine.items():
            human = reviewed.get(object_id)
            if (family_of(source.get('kind')) != family or human is None or not _usable(human)
                    or not _traceable(source, model)):
                continue
            if human.get('status') == 'rejected':
                decision = 'reject'
            elif human.get('status') == 'confirmed':
                decision = 'keep' if human.get('kind') == source.get('kind') else f'relabel:{human.get("kind")}'
            else:
                continue
            decisions[object_id] = decision
            detections.append({'object_id': object_id, 'kind': source.get('kind'), 'outline_pixels': outline_of(source),
                               **{key: source[key] for key in MACHINE_FIELDS if key in source}})
        for object_id, human in reviewed.items():
            if (object_id not in machine and family_of(human.get('kind')) == family and human.get('geometry_reviewed')
                    and human.get('status') not in ('rejected', 'superseded') and _usable(human)):
                added.append({'object_id': object_id, 'kind': human.get('kind'), 'outline_pixels': outline_of(human)})
        if not detections or not (added or any(d != 'keep' for d in decisions.values())):
            continue
        detections = sorted(detections, key=lambda d: d['object_id'])[:MAX_DETECTIONS]
        decisions = {d['object_id']: decisions[d['object_id']] for d in detections}
        case_id = f'd-{scene_id}-{family}'
        case = {'version': VERSION, 'case_id': case_id, 'task': 'detect', 'scene_id': scene_id, 'family': family,
                'domain': f'{family}-detect', 'kinds': sorted(k for k, f in FAMILIES.items() if f == family) or [family],
                'image': 'image.png', 'image_sha256': model['image_sha256'], 'source_frame': frame_of(model),
                'pixel_size_m': pixel_size_m(model), 'detections': detections}
        heldout = {'version': VERSION, 'case_id': case_id, 'decisions': decisions, 'added': added,
                   'review_revision': review.get('revision')}
        yield case, heldout


def write_case(dest_root, case, heldout, image):
    """Write one snapshot; the image is verified against its content hash first."""
    if _sha(image) != case['image_sha256']:
        raise ValueError(f'{image}: image bytes do not match image_sha256 {case["image_sha256"][:12]}')
    folder = Path(dest_root)/case['case_id']
    shutil.rmtree(folder, ignore_errors=True)
    (folder/'source').mkdir(parents=True)
    try:  # scene images are content-addressed and never rewritten, so cases share their bytes
        os.link(image, folder/'source'/'image.png')
    except OSError:
        shutil.copy2(image, folder/'source'/'image.png')
    _json(folder/'source'/'case.json', case)
    if heldout is not None:  # an undecided case refines answer-free and stays unscored
        _json(folder/'heldout'/'reference.json', heldout)
    return folder


def build(root=Path('.'), dest=None, *, tasks=('outline', 'detect'), kinds=(), unchanged_ratio=0.25, scenes=(),
          limit_per_domain=None):
    """Rebuild the case set from saved reviews; returns the index that was written."""
    root = Path(root)
    dest = Path(dest) if dest else root/ROOT
    entries = {}
    for scene_id, model, review, image in scene_sources(root):
        if scenes and scene_id not in scenes:
            continue
        pairs = [*(outline_cases(scene_id, model, review) if 'outline' in tasks else ()),
                 *(detection_cases(scene_id, model, review) if 'detect' in tasks else ())]
        for case, heldout in pairs:
            kind_ok = not kinds or case.get('kind') in kinds or set(case.get('kinds', ())) & set(kinds)
            if not kind_ok or (heldout.get('change') == 'unchanged' and not _keep(case['case_id'], unchanged_ratio)):
                continue
            entries[case['case_id']] = (case, heldout, image)
    by_domain = {}
    for case_id in sorted(entries):
        by_domain.setdefault(entries[case_id][0]['domain'], []).append(case_id)
    keep = {case_id for ids in by_domain.values() for case_id in (ids[:limit_per_domain] if limit_per_domain else ids)}
    if dest.is_dir():
        for folder in dest.iterdir():
            if folder.is_dir() and folder.name not in keep and (folder/'source'/'case.json').is_file():
                shutil.rmtree(folder)
    index = {}
    for case_id in sorted(keep):
        case, heldout, image = entries[case_id]
        write_case(dest, case, heldout, image)
        index[case_id] = {'task': case['task'], 'domain': case['domain'], 'scene_id': case['scene_id'],
                          'object_ids': [case['object_id']] if case['task'] == 'outline' else
                          [d['object_id'] for d in case['detections']],
                          'split': split_of(case['scene_id']), 'change': heldout.get('change', 'decisions'),
                          'kind': case.get('kind') or case.get('family')}
    _json(dest/'index.json', {'version': VERSION, 'cases': index})
    return index


def unlabeled_cases(scene_id, model, review, kinds, min_score):
    """Answer-free outline cases for objects no human has reviewed: the saved (live) outline is the start."""
    live = (review or {}).get('objects') if review else model.get('objects')
    machine = {obj['id']: obj for obj in model.get('objects', []) if obj.get('id')}
    for obj in live or []:
        source = machine.get(obj.get('id'), {})
        score = obj.get('score', source.get('score'))
        if (obj.get('status') != 'candidate' or obj.get('geometry_reviewed') or obj.get('kind') not in kinds
                or not _usable(obj) or not _traceable(obj, model) or not isinstance(score, int | float)
                or score < min_score):
            continue
        family = family_of(obj['kind'])
        case_id = f'u-{scene_id}-{obj["id"]}'
        yield {'version': VERSION, 'case_id': case_id, 'task': 'outline', 'scene_id': scene_id, 'object_id': obj['id'],
               'kind': obj['kind'], 'family': family, 'domain': f'{family}-outline', 'image': 'image.png',
               'image_sha256': model['image_sha256'], 'source_frame': frame_of(model),
               'pixel_size_m': pixel_size_m(model), 'start_outline_pixels': outline_of(obj),
               'machine': {key: obj[key] for key in MACHINE_FIELDS if key in obj}}, score


SCORE_BANDS = (0.6, 0.75, 0.85)


def build_unlabeled(root=Path('.'), dest=None, *, kinds=('building',), per_domain=20, per_scene=2, min_score=0.35,
                    min_area_m2=25.0, bands=SCORE_BANDS):
    """Answer-free cases for the trace flywheel: an even share from each machine-score band, objects of at least
    `min_area_m2`, in hash order within a band and spread across scenes. The agent's own disagreement across
    repeats ranks the review queue afterwards (fieldwork.rank_traces), so selection here stays broad."""
    root = Path(root)
    dest = Path(dest) if dest else root/'data/harness/cases-unlabeled'
    pool = {}
    for scene_id, model, review, image in scene_sources(root):
        for case, score in unlabeled_cases(scene_id, model, review, set(kinds), min_score):
            area = (case['machine'].get('model') or {}).get('area_m2')
            if isinstance(area, int | float) and area < min_area_m2:
                continue
            band = sum(score >= edge for edge in bands)
            order = hashlib.sha256(case['case_id'].encode()).hexdigest()
            pool.setdefault(case['domain'], {}).setdefault(band, []).append((order, case['case_id'], case, image))
    chosen = {}
    for by_band in pool.values():
        per, queues = {}, [sorted(rows) for _band, rows in sorted(by_band.items())]
        while queues and sum(per.values()) < per_domain:
            for queue in list(queues):  # round-robin across bands keeps their shares even
                while queue and per.get(queue[0][2]['scene_id'], 0) >= per_scene:
                    queue.pop(0)
                if not queue:
                    queues.remove(queue)
                    continue
                _order, case_id, case, image = queue.pop(0)
                per[case['scene_id']] = per.get(case['scene_id'], 0) + 1
                chosen[case_id] = (case, image)
                if sum(per.values()) >= per_domain:
                    break
    if dest.is_dir():
        for folder in dest.iterdir():
            if folder.is_dir() and folder.name not in chosen and (folder/'source'/'case.json').is_file():
                shutil.rmtree(folder)
    index = {}
    for case_id, (case, image) in sorted(chosen.items()):
        write_case(dest, case, None, image)
        index[case_id] = {'task': 'outline', 'domain': case['domain'], 'scene_id': case['scene_id'],
                          'object_ids': [case['object_id']], 'split': split_of(case['scene_id']),
                          'change': 'unlabeled', 'kind': case['kind']}
    _json(dest/'index.json', {'version': VERSION, 'cases': index})
    return index


def load_index(dest):
    path = Path(dest)/'index.json'
    if not path.is_file():
        raise FileNotFoundError(f'no case index at {path}; build it with `python -m scripts.ml.harnessing cases build`')
    return json.loads(path.read_text())['cases']


def load_case(dest, case_id):
    return json.loads((Path(dest)/case_id/'source'/'case.json').read_text())


def load_heldout(dest, case_id):
    path = Path(dest)/case_id/'heldout'/'reference.json'
    return json.loads(path.read_text()) if path.is_file() else None


def census(index):
    """Counts per domain, split and change, for the CLI."""
    rows = {}
    for entry in index.values():
        row = rows.setdefault(entry['domain'], {'train': 0, 'val': 0, 'test': 0, 'edited': 0, 'unchanged': 0, 'decisions': 0})
        row[entry['split']] += 1
        row[entry['change']] = row.get(entry['change'], 0) + 1
    return dict(sorted(rows.items()))
