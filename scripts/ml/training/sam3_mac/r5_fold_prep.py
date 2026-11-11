"""Round-5 fold preparation (sam3-mac-r5-fold-prep-v1): keep set, hard negatives, leak check, prepare_coco dry run.

No training and no inference. Everything here is bookkeeping over files that already exist: the 2025 teacher export
(distill_dataset, policy none), the nDSM-informed ledger rows (teacher_elevation, recommendation_v2), the reviewer's
small-roof decisions as the human precision batch recorded them (r5_precision block files) and the frozen dataset splits.
Wording: a teacher instance is a model estimate; a ledger recommendation is agreement between signals; a reviewer decision is
a human decision; nothing here is accuracy and nothing is promoted.

keep-set   The round-4 fold took the 2025 set from the ledger-v1 export, where distill_dataset._policy_ignored painted every
           ledger row recommended 'ignore' as IGNORE (no polygon). prepare_coco never reads a ledger and the policy-v2 export
           needs a distill_dataset change plus a host job (.run/round5-readiness-2026-10-01.md section 5). The v2 ignore set is
           a subset of the v1 ignore set (602 rows demoted ignore -> review, none promoted), so the v2 keep set is exactly the
           policy-none export minus the rows recommendation_v2 == 'ignore'. This command writes that selection as a new
           dataset-v1-format folder prepare_coco consumes (train/val/test.json, images symlink, manifest.json with the dropped
           keys, counts by class and window, the delta against the v1 export and the counts under candidate area floors).
           ledger_rows.jsonl, ledger.json and the exports are never written.

negatives  The reviewer's rejected small objects (RULES human_precision class not_roof) read from a human-precision batch block
           file, joined to the frozen split rows the batch judged (ring, pixel area, m2, tile). SAM3's instance loss has no
           negative instance or ignore region (prepare_coco known_differences): the only way an object is a hard negative is
           to be ABSENT from the positives of a training image. So a negative is applied by removing its annotation from a
           TRAIN source; a negative on a frozen judged tile (dataset-v2 test / val, dataset-v1 test) is refused by name and
           never enters training. The trainclean train tiles are scanned (read-only, r5_precision.load_decisions) for
           reviewer rejections whose pseudo label is still a positive there: those are the admissible negatives.

apply      The proposed mechanism, implemented outside prepare_coco as a derived dataset folder: the admissible negatives'
           annotations are removed from a copy of the source split json (images symlinked, manifest listing every removal),
           so prepare_coco's own 'dropped annotations become background' rule turns them into hard negatives. A
           prepare_coco --negatives flag would do the same inside convert (see .run/session-2026-10-06-fold/plan-read.md).

dry-run    prepare_coco.convert over the round-5 sources into an output folder (json only; images are never copied), then the
           leak check: no train image shares scene_id, tile_group or image sha256 with dataset-v2 test / val or dataset-v1
           test, the frozen split files still carry their recorded sha256, and r3_record.fold_gate passes with the
           zero-annotation gate. Per split: images, annotations, negatives applied, ignore regions (always 0 for SAM3).

  scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_fold_prep keep-set \\
      --source data/reviews/eyes/training/distill-linz-chc-2025-0075-squared \\
      --v1-export data/reviews/eyes/training/distill-linz-chc-2025-0075-squared-ledger \\
      --ledger-rows data/reviews/eyes/training/distill-linz-chc-2025-0075/teacher/judges/ledger_v2_christchurch_2024-2025_rows.jsonl \\
      --out <prep>/distill-linz-chc-2025-0075-squared-ledger-v2
  ... negatives --batch-dir <round5>/batches/<batch> --trainclean data/reviews/eyes/training/dataset-v2-trainclean --out <prep>
  ... dry-run --dataset ... --out <prep>/coco-dry --min-area-px-for NAME=PX ... --judge-dataset ... [--negatives <prep>/negatives.jsonl]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from . import prepare_coco
from . import r3_record as rec

VERSION = 'sam3-mac-r5-fold-prep-v1'
REPO = Path(__file__).resolve().parents[4]
TRAINING = REPO / 'data/reviews/eyes/training'
SPLITS = ('train', 'val', 'test')
JUDGED = (('dataset-v2', 'test'), ('dataset-v2', 'val'), ('dataset-v1', 'test'))   # frozen judged splits: never train
FLOORS = (16, 72, 216)              # candidate area floors in student pixels: 0.89 / 4.0 / 12.0 m2 at 0.235938 m
LEDGER_KEY, LEDGER_DROP = 'recommendation_v2', 'ignore'
KIND_TAG = re.compile(r'\[reviewer [^\]]*chat,\s*([a-z][a-z _-]*)\]', re.I)   # the transcription's kind word (CP26 way)
KIND_WORDS = {'shade': 'tree_shadow', 'shadow': 'tree_shadow', 'road': 'road', 'pond': 'pond', 'tree': 'tree'}
ANN_FIELDS = ('id', 'image_id', 'object_id', 'bbox', 'area', 'area_m2', 'segmentation', 'provenance')


def now_iso():
    return datetime.now(UTC).isoformat(timespec='seconds')


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path):
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def write_json(path, payload, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as handle:
        json.dump(payload, handle, indent=indent, sort_keys=indent is not None)


def teacher_key(ann):
    """(window, teacher index) of an export annotation, or None when the object id is not 'teacher-<index>'."""
    oid = str(ann.get('object_id') or '')
    if not oid.startswith('teacher-') or not oid[8:].isdigit():
        return None
    return ann.get('scene_id'), int(oid[8:])


def link_images(source, out):
    """Relative symlink out/images -> source/images (make_v2_trainclean precedent: no image copies)."""
    source_images = Path(source) / 'images'
    link = Path(out) / 'images'
    if link.is_symlink() or link.exists():
        if link.is_symlink() and os.readlink(link) == os.path.relpath(source_images, out):
            return str(link)
        raise FileExistsError(f'{link} exists and is not the expected symlink')
    os.symlink(os.path.relpath(source_images, out), link)
    return str(link)


# --- keep set ----------------------------------------------------------------------------------------------------------

def floor_counts(annotations, floors=FLOORS):
    return {str(f): sum(1 for a in annotations if float(a.get('area', 0.)) >= f) for f in floors}


def keep_set(source, ledger_rows, out, v1_export=None, key=LEDGER_KEY, drop_value=LEDGER_DROP, floors=FLOORS):
    """Write the keep set of `source` under `out`: every annotation whose (window, index) the ledger rows recommend
    `drop_value` under `key` is removed; images are unchanged (symlink). Returns the manifest (also written)."""
    source, out = Path(source), Path(out)
    rows = read_jsonl(ledger_rows)
    by_key = {(r['window'], int(r['index'])): r for r in rows}
    dropped_keys = {k for k, r in by_key.items() if r.get(key) == drop_value}
    out.mkdir(parents=True, exist_ok=True)
    link_images(source, out)
    manifest = {'version': VERSION, 'created_at': now_iso(), 'kind': 'keep_set',
                'source_export': {'path': str(source), 'manifest_sha256': sha256_file(source / 'manifest.json') if (source / 'manifest.json').exists() else None,
                                  'policy': (read_json(source / 'manifest.json').get('policy') if (source / 'manifest.json').exists() else None)},
                'ledger_rows': {'path': str(ledger_rows), 'sha256': sha256_file(ledger_rows), 'rows': len(rows), 'key': key, 'drop_value': drop_value,
                                'drop_keys': len(dropped_keys), 'by_value': dict(Counter(r.get(key) for r in rows))},
                'v1_export': {'path': str(v1_export)} if v1_export else None,
                'floors_px': list(floors), 'splits': {}, 'not_written': ['ledger_rows.jsonl', 'ledger.json', 'the source exports', 'label PNGs']}
    for split in SPLITS:
        path = source / f'{split}.json'
        if not path.exists():
            manifest['splits'][split] = None
            continue
        coco = read_json(path)
        kept, dropped, unkeyed = [], [], 0
        for ann in coco['annotations']:
            k = teacher_key(ann)
            if k is None:
                unkeyed += 1
                kept.append(ann)
                continue
            if k in dropped_keys:
                row = by_key[k]
                dropped.append({'window': k[0], 'index': k[1], 'ann_id': ann['id'], 'area_px': ann.get('area'), 'area_m2': ann.get('area_m2'),
                                'ndsm_class': row.get('ndsm_class'), 'band': row.get('band'), 'v1': row.get('recommendation'), 'v2': row.get(key)})
                continue
            kept.append({**ann, 'ledger_v2': by_key[k].get(key) if k in by_key else None})
        payload = {**coco, 'annotations': kept}
        write_json(out / f'{split}.json', payload)
        by_class = Counter(a.get('ledger_v2') for a in kept)
        by_window = defaultdict(Counter)
        for a in kept:
            by_window[a.get('scene_id')][a.get('ledger_v2')] += 1
        row = {'images': len(coco['images']), 'annotations_in': len(coco['annotations']), 'dropped': len(dropped), 'annotations_out': len(kept),
               'unkeyed_kept': unkeyed, 'by_class': dict(sorted(by_class.items(), key=lambda kv: str(kv[0]))),
               'by_window': {w: dict(sorted(c.items(), key=lambda kv: str(kv[0]))) for w, c in sorted(by_window.items())},
               'dropped_rows': dropped, 'dropped_by_ndsm_class': dict(Counter(d['ndsm_class'] for d in dropped)),
               'floor_counts': {'keep_set': floor_counts(kept, floors)}}
        if v1_export and (Path(v1_export) / f'{split}.json').exists():
            v1 = read_json(Path(v1_export) / f'{split}.json')
            v1_keys = {teacher_key(a) for a in v1['annotations']} - {None}
            v2_keys = {teacher_key(a) for a in kept} - {None}
            added = sorted(v2_keys - v1_keys)
            row['delta_vs_v1'] = {'v1_annotations': len(v1['annotations']), 'keep_set_annotations': len(kept), 'added': len(added),
                                  'removed': len(v1_keys - v2_keys), 'images_equal': [i['sha256'] for i in v1['images']] == [i['sha256'] for i in coco['images']],
                                  'added_keys': [list(k) for k in added],
                                  'added_floor_counts': floor_counts([a for a in kept if teacher_key(a) in set(added)], floors)}
            row['floor_counts']['v1_export'] = floor_counts(v1['annotations'], floors)
        manifest['splits'][split] = row
    write_json(out / 'manifest.json', manifest, indent=1)
    return manifest


# --- negatives ---------------------------------------------------------------------------------------------------------

def judged_index(training=TRAINING, judged=JUDGED):
    """{(dataset, split): {'scene_id': set, 'tile_group': set, 'sha256': set, 'sha256_file': str}} over the frozen judged splits."""
    out = {}
    for dataset, split in judged:
        path = Path(training) / dataset / f'{split}.json'
        if not path.exists():
            continue
        images = read_json(path)['images']
        out[f'{dataset} {split}'] = {'scene_id': {i.get('scene_id') for i in images} - {None}, 'tile_group': {i.get('tile_group') for i in images} - {None},
                                    'sha256': {i.get('sha256') for i in images} - {None}, 'sha256_file': sha256_file(path), 'path': str(path)}
    return out


def refusals(tile, judged):
    """Names of the frozen judged splits a tile (scene_id / tile_group / sha256 dict) touches."""
    out = []
    for name, index in judged.items():
        hits = [f for f in ('scene_id', 'tile_group', 'sha256') if tile.get(f) and tile[f] in index[f]]
        if hits:
            out.append(f"frozen_judged_tile:{name}:{'+'.join(hits)}")
    return out


def kind_from_notes(notes):
    """('tree_shadow' | 'road' | 'pond' | 'tree' | <tag word>, tag) from a chat transcription's bracket tag, else (None, None)."""
    match = KIND_TAG.search(str(notes or ''))
    if not match:
        return None, None
    word = match.group(1).strip().lower()
    return KIND_WORDS.get(word, word.replace(' ', '_')), match.group(0)


def review_notes(scene_id, object_id, scenes_root):
    path = Path(scenes_root) / str(scene_id) / 'review.json'
    if not path.exists():
        return None
    for obj in read_json(path).get('objects') or ():
        if isinstance(obj, dict) and obj.get('id') == object_id:
            return obj.get('notes')
    return None


def negatives_from_block(block_file, judged, scenes_root=None, training=TRAINING):
    """One row per reviewer-rejected object of a human-precision block file, joined to the frozen split it was judged on."""
    block = read_json(block_file)
    dataset, split = Path(block['dataset']), block['split']
    dataset = dataset if dataset.is_absolute() else (REPO / dataset if (REPO / dataset).exists() else Path(training) / dataset.name)
    frozen = read_json(dataset / f'{split}.json')
    images = {i['id']: i for i in frozen['images']}
    anns = {a['id']: a for a in frozen['annotations']}
    rows = []
    for fact in block['facts']:
        if fact.get('class') != 'not_roof':
            continue
        image, ann = images.get(fact['image_id']), anns.get(fact['ann_id'])
        tile = {'scene_id': image.get('scene_id'), 'tile_group': image.get('tile_group'), 'sha256': image.get('sha256')} if image else {}
        notes = review_notes(fact['scene_id'], fact['object_id'], scenes_root) if scenes_root else None
        kind, tag = kind_from_notes(notes)
        refused = refusals(tile, judged) if image else ['frozen_image_missing']
        rows.append({'source': 'human_precision_block', 'block_file': str(block_file), 'batch_id': block.get('batch_id'), 'decision_scene': fact['scene_id'],
                     'object_id': fact['object_id'], 'frozen_key': [tile.get('scene_id'), fact['object_id']], 'path': fact.get('path'),
                     'provenance_kind': fact.get('provenance_kind'), 'status': fact.get('status'), 'dataset': str(dataset), 'split': split,
                     'image_id': fact['image_id'], 'ann_id': fact['ann_id'], 'tile': tile, 'pixel_size_m': image.get('pixel_size_m') if image else None,
                     'area_px': ann.get('area') if ann else None, 'area_m2': fact.get('snapshot_area_m2'), 'bbox': ann.get('bbox') if ann else None,
                     'ring': (ann.get('segmentation') or [None])[0] if ann else None, 'kind': kind or 'unstated_ui_decision', 'kind_tag': tag,
                     'detected_by_base_at_045': (fact.get('arms') or {}).get('base', {}).get('detected'),
                     'admissible': not refused, 'refused': refused})
    for row in block.get('unjoined') or ():
        if row.get('status') != 'rejected':
            continue
        rows.append({'source': 'human_precision_block_unjoined', 'block_file': str(block_file), 'batch_id': block.get('batch_id'),
                     'decision_scene': row['scene_id'], 'object_id': row['object_id'], 'frozen_key': None, 'provenance_kind': row.get('provenance_kind'),
                     'status': 'rejected', 'area_m2': row.get('review_area_m2'), 'tile': {}, 'ring': None, 'kind': 'unstated_unjoined',
                     'admissible': False, 'refused': [f"unjoined:{r}" for r in row.get('unjoined_reasons') or ()]})
    return rows


def trainclean_rejections(trainclean, judged, scenes_root):
    """Reviewer rejections (r5_precision.classify not_roof, production object) on the trainclean TRAIN tiles, joined to the
    pseudo annotation that is still a positive there: admissible hard negatives. Scenes without a review.json yield nothing."""
    from . import r5_precision as r5  # heavy imports (shapely, numpy); only here
    train = read_json(Path(trainclean) / 'train.json')
    images = {i['id']: i for i in train['images']}
    by_key = {(a.get('scene_id') or images[a['image_id']].get('scene_id'), a.get('object_id')): a for a in train['annotations']}
    scene_ids = sorted({i.get('scene_id') for i in train['images']} - {None})
    decisions = r5.load_decisions(root=Path(scenes_root), scene_ids=scene_ids)
    rows = []
    for d in decisions['rows']:
        if d.get('cls') != 'not_roof' or not d.get('production'):
            continue
        ann = by_key.get((d['scene_id'], d['object_id']))
        image = images.get(ann['image_id']) if ann else None
        tile = {'scene_id': image.get('scene_id'), 'tile_group': image.get('tile_group'), 'sha256': image.get('sha256')} if image else {'scene_id': d['scene_id']}
        kind, tag = kind_from_notes(d.get('notes'))
        refused = refusals(tile, judged) + ([] if ann else ['no_train_annotation (under the 4 m2 export floor or not exported)'])
        rows.append({'source': 'trainclean_review_scan', 'decision_scene': d['scene_id'], 'object_id': d['object_id'], 'frozen_key': [d['scene_id'], d['object_id']],
                     'provenance_kind': d.get('provenance_kind'), 'status': d.get('status'), 'dataset': str(trainclean), 'split': 'train',
                     'source_dataset': Path(trainclean).name, 'image_id': ann['image_id'] if ann else None, 'ann_id': ann['id'] if ann else None,
                     'tile': tile, 'area_px': ann.get('area') if ann else None, 'area_m2': ann.get('area_m2') if ann else d.get('review_area_m2'),
                     'ring': (ann.get('segmentation') or [None])[0] if ann else None, 'kind': kind or 'unstated_ui_decision', 'kind_tag': tag,
                     'admissible': not refused, 'refused': refused})
    return rows, {'scenes_scanned': len(scene_ids), 'reviews_read': decisions['reviews_read'], 'review_snapshot_sha256': decisions['review_snapshot_sha256']}


def negatives_summary(rows, judged):
    adm = [r for r in rows if r['admissible']]
    return {'version': VERSION, 'computed_at': now_iso(), 'rows': len(rows), 'admissible': len(adm), 'refused': len(rows) - len(adm),
            'by_kind': dict(Counter(r['kind'] for r in rows)), 'by_tile': dict(Counter(str(r['tile'].get('scene_id')) for r in rows)),
            'by_source': dict(Counter(r['source'] for r in rows)), 'by_provenance_kind': dict(Counter(str(r.get('provenance_kind')) for r in rows)),
            'refused_by_reason': dict(Counter(reason for r in rows for reason in r['refused'])),
            'on_frozen_v2_test': sum(1 for r in rows if any(x.startswith('frozen_judged_tile:dataset-v2 test') for x in r['refused'])),
            'admissible_by_source_dataset': dict(Counter(r.get('source_dataset') for r in adm)),
            'judged_splits': {name: {'images_scene_ids': len(v['scene_id']), 'sha256_file': v['sha256_file'], 'path': v['path']} for name, v in judged.items()},
            'mechanism': 'absent from the positives of a train image (prepare_coco known_differences: dropped annotations become background); '
                         'no negative instance and no ignore region exist in SAM3 instance training; a negative on a frozen judged tile is refused'}


# --- apply (the proposed mechanism, as a derived dataset) ---------------------------------------------------------------

def apply_negatives(dataset, negatives, out, split='train'):
    """Derived dataset folder: `split`.json of `dataset` without the admissible negatives' annotations (matched on
    (scene_id, object_id) or ann id), images symlinked, manifest listing every removal and every image left empty."""
    dataset, out = Path(dataset), Path(out)
    coco = read_json(dataset / f'{split}.json')
    images = {i['id']: i for i in coco['images']}
    wanted = {}
    for n in negatives:
        if not n.get('admissible') or n.get('split', split) != split or (n.get('source_dataset') and n['source_dataset'] != dataset.name):
            continue
        wanted[(n['frozen_key'][0], n['frozen_key'][1])] = n
    removed, kept = [], []
    for ann in coco['annotations']:
        key = (ann.get('scene_id') or images[ann['image_id']].get('scene_id'), ann.get('object_id'))
        if key in wanted:
            removed.append({'ann_id': ann['id'], 'image_id': ann['image_id'], 'scene_id': key[0], 'object_id': key[1], 'area_px': ann.get('area'),
                            'area_m2': ann.get('area_m2'), 'kind': wanted[key]['kind']})
        else:
            kept.append(ann)
    out.mkdir(parents=True, exist_ok=True)
    link_images(dataset, out)
    write_json(out / f'{split}.json', {**coco, 'annotations': kept})
    per_image = Counter(a['image_id'] for a in kept)
    manifest = {'version': VERSION, 'created_at': now_iso(), 'kind': 'negatives_applied', 'source_dataset': str(dataset),
                'source_manifest_sha256': sha256_file(dataset / 'manifest.json') if (dataset / 'manifest.json').exists() else None,
                'split': split, 'requested': len(wanted), 'removed': len(removed), 'not_found': [list(k) for k in wanted if k not in {(r['scene_id'], r['object_id']) for r in removed}],
                'removed_rows': removed, 'annotations_in': len(coco['annotations']), 'annotations_out': len(kept),
                'images_left_empty': [i for i in images if per_image.get(i, 0) == 0 and any(r['image_id'] == i for r in removed)]}
    write_json(out / 'manifest.json', manifest, indent=1)
    return manifest


# --- dry run -----------------------------------------------------------------------------------------------------------

def leak_check(coco_root, datasets, judged):
    """Every train image of the fold against the frozen judged splits on scene_id, tile_group and image sha256 (sha from the
    source dataset's own image row, since prepare_coco writes none)."""
    train = read_json(Path(coco_root) / 'train' / '_annotations.coco.json')
    source_rows = {}
    for d in datasets:
        path = Path(d) / 'train.json'
        if path.exists():
            source_rows[Path(d).name] = {i['id']: i for i in read_json(path)['images']}
    hits, checked = [], 0
    for image in train['images']:
        src = (source_rows.get(image.get('source_dataset')) or {}).get(image.get('source_image_id')) or {}
        tile = {'scene_id': image.get('scene_id'), 'tile_group': image.get('tile_group'), 'sha256': src.get('sha256')}
        checked += 1
        for r in refusals(tile, judged):
            hits.append({'image_id': image['id'], 'source_dataset': image.get('source_dataset'), 'scene_id': image.get('scene_id'), 'refusal': r})
    return {'train_images_checked': checked, 'leaks': len(hits), 'leak_rows': hits, 'ok': not hits}


def dry_run(datasets, out, judge_datasets, per_dataset_floor=None, min_area_px=16, cap=200, negatives=None, judged=None, training=TRAINING,
            expected_split_sha=None):
    judged = judged if judged is not None else judged_index(training)
    before = {name: v['sha256_file'] for name, v in judged.items()}
    manifest = prepare_coco.convert(datasets, out, min_area_px=min_area_px, max_ann_per_image=cap, judge_datasets=judge_datasets,
                                    per_dataset_floor=per_dataset_floor or {})
    after = {name: sha256_file(v['path']) for name, v in judged.items()}
    applied = [n for n in negatives or () if n.get('admissible')]
    refused = [n for n in negatives or () if not n.get('admissible')]
    summary = {'version': VERSION, 'computed_at': now_iso(), 'out': str(out), 'sources': [str(d) for d in datasets],
               'floor_px_per_source': manifest['floor_px_per_source'], 'cap': cap, 'splits': {}}
    for split, row in manifest['splits'].items():
        summary['splits'][split] = {'images': row['images'], 'annotations': row['annotations'], 'images_over_fork_max': row['images_over_fork_max'],
                                    'flipped_to_background': row['flipped_to_background'],
                                    'negatives_applied': sum(1 for n in applied if n.get('split') == split), 'ignore_regions': 0,
                                    'per_source': {k: {kk: vv for kk, vv in v.items() if kk in ('images', 'annotations', 'ann_tiny', 'ann_over_cap')} for k, v in row['per_source'].items()}}
        if 'distance_check' in row:
            summary['splits'][split]['distance_check'] = {k: v for k, v in row['distance_check'].items() if k not in ('flagged', 'min_distance_m_per_image')}
    summary['negatives'] = {'applied': len(applied), 'refused': len(refused), 'ignore_regions_note': 'SAM3 instance training has no ignore region; 0 by construction'}
    summary['leak_check'] = leak_check(out, datasets, judged)
    summary['frozen_splits_unchanged'] = {name: {'before': before[name], 'after': after[name], 'unchanged': before[name] == after[name],
                                                 'expected': (expected_split_sha or {}).get(name),
                                                 'matches_expected': (expected_split_sha or {}).get(name) is None or after[name].startswith((expected_split_sha or {})[name])}
                                          for name in judged}
    summary['fold_gate'] = rec.fold_gate(out, cap, require_annotations=True)
    summary['ok'] = bool(summary['leak_check']['ok'] and all(v['unchanged'] and v['matches_expected'] for v in summary['frozen_splits_unchanged'].values())
                         and summary['fold_gate']['ok'])
    write_json(Path(out) / 'dry-run-summary.json', summary, indent=1)
    return summary


# --- CLI ---------------------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='mode', required=True)
    k = sub.add_parser('keep-set')
    k.add_argument('--source', required=True, help='policy-none export folder (dataset-v1 format)')
    k.add_argument('--ledger-rows', required=True)
    k.add_argument('--v1-export', help='the ledger-v1 export the round-4 fold used (delta)')
    k.add_argument('--key', default=LEDGER_KEY)
    k.add_argument('--drop-value', default=LEDGER_DROP)
    k.add_argument('--out', required=True)
    n = sub.add_parser('negatives')
    n.add_argument('--block-file', action='append', default=[], help='human-precision block file(s) (facts rows)')
    n.add_argument('--trainclean', help='dataset-v2-trainclean folder to scan for admissible rejections')
    n.add_argument('--scenes', default=str(REPO / 'data/scenes'))
    n.add_argument('--training', default=str(TRAINING))
    n.add_argument('--out', required=True, help='folder for negatives.jsonl and negatives-summary.json')
    d = sub.add_parser('dry-run')
    d.add_argument('--dataset', action='append', required=True)
    d.add_argument('--out', required=True)
    d.add_argument('--min-area-px', type=int, default=16)
    d.add_argument('--min-area-px-for', action='append', default=None, metavar='NAME=PX')
    d.add_argument('--max-ann-per-image', type=int, default=200)
    d.add_argument('--judge-dataset', action='append', default=None)
    d.add_argument('--negatives', help='negatives.jsonl (only admissible rows are counted as applied)')
    d.add_argument('--training', default=str(TRAINING))
    d.add_argument('--expect-sha', action='append', default=[], metavar='"NAME=PREFIX"', help='e.g. "dataset-v2 test=1ba87eed"')
    args = parser.parse_args(argv)
    if args.mode == 'keep-set':
        m = keep_set(args.source, args.ledger_rows, args.out, args.v1_export, args.key, args.drop_value)
        brief = {s: ({k: v for k, v in row.items() if k not in ('dropped_rows', 'by_window')} if row else None) for s, row in m['splits'].items()}
        json.dump({'ledger_rows': m['ledger_rows'], 'splits': brief}, sys.stdout, indent=1, sort_keys=True)
    elif args.mode == 'negatives':
        judged = judged_index(args.training)
        rows, scan = [], None
        for block_file in args.block_file:
            rows.extend(negatives_from_block(block_file, judged, scenes_root=args.scenes, training=args.training))
        if args.trainclean:
            more, scan = trainclean_rejections(args.trainclean, judged, args.scenes)
            rows.extend(more)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / 'negatives.jsonl', 'w') as handle:
            for row in rows:
                handle.write(json.dumps(row) + '\n')
        summary = negatives_summary(rows, judged)
        summary['trainclean_scan'] = scan
        write_json(out / 'negatives-summary.json', summary, indent=1)
        json.dump({k: v for k, v in summary.items() if k != 'judged_splits'}, sys.stdout, indent=1, sort_keys=True)
    else:
        negatives = read_jsonl(args.negatives) if args.negatives else None
        expected = dict(spec.split('=', 1) for spec in args.expect_sha)
        summary = dry_run(args.dataset, args.out, args.judge_dataset, prepare_coco.parse_floor_spec(args.min_area_px_for), args.min_area_px,
                          args.max_ann_per_image, negatives, training=args.training, expected_split_sha=expected)
        brief = {k: v for k, v in summary.items() if k not in ('leak_check',)}
        brief['leak_check'] = {k: v for k, v in summary['leak_check'].items() if k != 'leak_rows'}
        json.dump(brief, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write('\n')
        return 0 if summary['ok'] else 1
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
