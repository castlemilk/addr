"""Distillation dataset distill-v1: 0.236 m student images labelled by hi-res SAM3 teacher masks (dataset-v1 format).

Reads the teacher run written by hires_teacher.py under data/reviews/eyes/training/distill-v1/teacher/ and writes,
beside it, the training kit's dataset-v1 layout so train.py and evaluate.py read it unchanged:

  images/<window>.png        the student image (the City of Melbourne 2018 true ortho, area-averaged to 0.236 m, blurred
                             to match the saved tiles)
  labels/<window>.png        uint16: 0 background, 65535 ignore (the smaller parts of a teacher roof whose student-grid mask
                             split into several pieces: known roof, not background), else
                             the instance index (larger instances first so a nested smaller one keeps its pixels)
  train.json / val.json / test.json
                             COCO polygons (holes under `holes`) with per-instance provenance 'hires-teacher-com-2018',
                             tier T3 (machine), weight 1.0, boundary_reference false; `segmentation_regularised` where
                             common.orthogonalise accepts; `segmentation_subpixel`: the teacher's native-resolution outline
                             in student pixel coordinates (corner detail finer than one student pixel)
  references.json            empty: no human outline exists for these windows
  sam3_control.json          SAM3 run on the student image itself, for every window (the control)
  manifest.json, DATASET_CARD.md

Labels: each teacher instance's student-grid mask (area-averaged native mask, covered fraction >= 0.5) is vectorised
exactly (segmentation.mask_to_polygons with simplify 0; common.fill_rings is its inverse), keeping the largest part.
Split: by window, ordered by sha256(seed:window id); the first round(15 %) are test, the next round(15 %) validation.
Windows sit on a non-overlapping grid, so no pixel is in two splits (a building cut by a window edge can appear
partly on both sides). Nothing here writes under data/scenes, the eyes index, or dataset-v1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image
from shapely import affinity

from ..segmentation import mask_to_polygons
from . import common
from . import hires_teacher as teacher

VERSION = 'roof-outline-distill-v1'
WEIGHT = 1.
TIER = 'T3'
SPLITS = ('train', 'val', 'test')
DEFAULT_SEED = teacher.DEFAULT_SEED
FRACTIONS = {'val': .15, 'test': .15}


def assign_splits(window_ids, seed=DEFAULT_SEED, fractions=None, key=None):
    """window id -> split. Order by sha256(seed:key); first round(test * n) test, next round(val * n) val, rest train.

    `key` maps a window id to the string that is hashed (default the id itself). Cross-capture runs pass the shared
    cell id, so two captures of the same ground land in the same split and never leak across train and test."""
    fractions = {**FRACTIONS, **(fractions or {})}
    key = key or (lambda w: w)
    ordered = sorted(window_ids, key=lambda w: hashlib.sha256(f'{seed}:{key(w)}'.encode()).hexdigest())
    n_test = int(round(fractions['test'] * len(ordered)))
    n_val = int(round(fractions['val'] * len(ordered)))
    splits = {}
    for index, wid in enumerate(ordered):
        splits[wid] = 'test' if index < n_test else 'val' if index < n_test + n_val else 'train'
    return splits


def label_polygon(piece):
    """Exact outline of the largest part of a student-grid mask (holes kept); (polygon, other parts, dropped_px).

    The other parts are the same roof per the teacher, so the caller paints them IGNORE, never background."""
    polygons = mask_to_polygons(piece.mask, min_area_px=1., simplify_px=0.)
    if not polygons:
        return None, [], piece.area
    order = sorted(polygons, key=lambda p: -p.area)
    polygon = common.valid_polygon(affinity.translate(order[0], piece.x0, piece.y0))
    others = [affinity.translate(p, piece.x0, piece.y0) for p in order[1:]]
    dropped = int(round(sum(p.area for p in order[1:])))
    return polygon, others, dropped


def subpixel_polygon(native_piece, scale):
    """The teacher's native outline (production 0.9 native px simplification) scaled into student pixels."""
    polygon = teacher.exterior_polygon(native_piece, teacher.SIMPLIFY_PX)
    if polygon is None:
        return None
    return affinity.scale(polygon, xfact=scale, yfact=scale, origin=(0, 0))


def control_rows(record):
    rows = []
    for row in record['control']:
        piece = teacher.rle_piece(row['rle'], row['bbox'], row['score'])
        polygons = mask_to_polygons(piece.mask, min_area_px=1., simplify_px=teacher.SIMPLIFY_PX)
        if not polygons:
            continue
        polygon = common.valid_polygon(affinity.translate(max(polygons, key=lambda p: p.area), piece.x0, piece.y0))
        if polygon is None:
            continue
        coco = common.polygon_to_coco(polygon)
        rows.append({'object_id': f"control-{row['index']}", 'score': row['score'], 'production': coco, 'raw': coco,
                     'raw_retained': False})
    return rows


LABEL_TARGETS = ('raw', 'squared_gated', 'whole_squared_gated')
POLICIES = ('none', 'ledger-v1', 'outline-v1')
LABEL_SUFFIX = {'raw': '', 'squared_gated': '-squared', 'whole_squared_gated': '-squared-merged'}
POLICY_SUFFIX = {'none': '', 'ledger-v1': '-ledger', 'outline-v1': '-outline'}
SQUARED_TARGETS = ('squared_gated', 'whole_squared_gated')
MERGE_INSIDE = .8           # a teacher piece joins an open outline when this share of its area lies inside it
OUTLINE_SMALL_M2 = 25.      # outline-v1: unmatched teacher instances under this area are painted IGNORE


def _read_jsonl(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in open(path) if line.strip()]


def _policy_ignored(teacher_dir, policy):
    """(window id, teacher index) pairs a policy removes from the labels (painted IGNORE, never background).

    ledger-v1: every ledger row recommended 'ignore' (no outline touched, under 12 m2, not matched in the other capture).
    outline-v1: the ledger 'ignore' rows plus every teacher instance the outlines judge classed 'unmatched' under
    OUTLINE_SMALL_M2 that the other capture did not match (where a ledger with cross-capture exists); 'part' rows and
    only-here instances of 25 m2 or more are never dropped. Reads outlines_rows.jsonl as well as ledger_rows.jsonl."""
    if policy == 'none':
        return set()
    judges = Path(teacher_dir) / 'judges'
    ledger = _read_jsonl(judges / 'ledger_rows.jsonl')
    ignored = {(r['window'], r['index']) for r in ledger if r.get('recommendation') == 'ignore'}
    if policy == 'ledger-v1':
        return ignored
    outlines = [r for r in _read_jsonl(judges / 'outlines_rows.jsonl') if r.get('arm', 'teacher') == 'teacher']
    if not outlines:
        return ignored
    cross = {(r['window'], r['index']): r.get('cross_capture') for r in ledger}
    for row in outlines:
        key = (row['window'], row['index'])
        small_unmatched = row.get('kind') == 'unmatched' and (row.get('area_m2') or 0.) < OUTLINE_SMALL_M2
        if small_unmatched and (not ledger or cross.get(key) != 'matched'):
            ignored.add(key)
    return ignored


def _ledger_ignored(teacher_dir):
    """(window id, teacher index) pairs the evidence ledger recommends ignoring; empty when no ledger exists."""
    return _policy_ignored(teacher_dir, 'ledger-v1')


def window_outlines(root, spec, record, size, gsd):
    """[(outline id, polygon in native px clipped to the window)] for one window; None when no outlines were fetched."""
    from shapely.geometry import box as shapely_box

    from . import teacher_judges as tj
    from . import teacher_outlines as outlines
    rows = outlines.load(root, spec, record['window']['id'])
    if rows is None:
        return None
    window_box = shapely_box(0., 0., float(size), float(size))
    native = outlines.to_window_pixels([r['geometry'] for r in rows], record['window']['native_affine'])
    out = []
    for row, polygon in zip(rows, native, strict=True):
        clipped = polygon.intersection(window_box)
        if clipped.is_empty or clipped.geom_type not in ('Polygon', 'MultiPolygon'):
            continue
        if clipped.geom_type == 'MultiPolygon':
            clipped = max(clipped.geoms, key=lambda g: g.area)
        if clipped.area * gsd * gsd < tj.OUTLINE_MIN_M2:
            continue
        out.append((row.get('building_id', row.get('id')), clipped))
    return out


def merge_groups(pieces, outlines, inside=MERGE_INSIDE):
    """Group teacher pieces by the open outline that holds at least `inside` of each piece's area.

    pieces: [(teacher index, polygon in native px)]; outlines: [(outline id, polygon)]. A piece covering half of two
    or more outlines (the judge's 'merged' kind) and a piece mostly outside every outline stay single. Returns
    ({outline id: [indices]} for outlines holding two or more pieces, stats)."""
    from shapely.strtree import STRtree

    from . import teacher_judges as tj
    polys = [p for _, p in outlines]
    tree = STRtree(polys) if polys else None
    groups, stats = {}, Counter()
    for index, polygon in pieces:
        if tree is None or polygon is None or polygon.area <= 0:
            stats['pieces_no_outline'] += 1
            continue
        best_id, best_inside, covered = None, 0., 0
        for i in tree.query(polygon):
            outline = polys[i]
            inter = polygon.intersection(outline).area
            if inter <= 0:
                continue
            share = inter / polygon.area
            if share > best_inside:
                best_inside, best_id = share, outlines[i][0]
            if outline.area and inter / outline.area >= tj.MERGED_COVER:
                covered += 1
        if covered >= 2:
            stats['pieces_spanning_two_outlines'] += 1
        elif best_inside >= inside:
            groups.setdefault(best_id, []).append(index)
        else:
            stats['pieces_no_outline'] += 1
    merged = {oid: sorted(idx) for oid, idx in groups.items() if len(idx) >= 2}
    stats['pieces_in_one_outline_alone'] = sum(1 for idx in groups.values() if len(idx) == 1)
    stats['groups_merged'] = len(merged)
    stats['pieces_merged'] = sum(len(v) for v in merged.values())
    return merged, stats


def teacher_units(record, groups):
    """Teacher rows regrouped into label units: one row each, or the member rows of a merged outline group."""
    by_index = {row['index']: row for row in record['teacher']}
    owner = {index: oid for oid, members in groups.items() for index in members}
    units, seen = [], set()
    for row in record['teacher']:
        oid = owner.get(row['index'])
        if oid is None:
            units.append({'rows': [row], 'merged_from': None, 'outline_id': None})
        elif oid not in seen:
            seen.add(oid)
            members = [by_index[i] for i in groups[oid]]
            units.append({'rows': members, 'merged_from': [m['index'] for m in members], 'outline_id': oid})
    return units


def unit_pieces(rows, native_size, student_size=teacher.STUDENT_PX):
    """(student-grid piece, native piece) of a label unit: the rows' masks, unioned when there are several."""
    if len(rows) == 1:
        row = rows[0]
        if not row['student']:
            return None, None
        return (teacher.rle_piece(row['student']['rle'], row['student']['bbox'], row['score']),
                teacher.rle_piece(row['rle_native'], row['bbox_native'], row['score']))
    score = max(r['score'] for r in rows)
    student = np.zeros((student_size, student_size), bool)
    native = np.zeros((native_size, native_size), bool)
    for row in rows:
        if row['student']:
            p = teacher.rle_piece(row['student']['rle'], row['student']['bbox'], row['score'])
            student[p.y0:p.y1, p.x0:p.x1] |= p.mask
        p = teacher.rle_piece(row['rle_native'], row['bbox_native'], row['score'])
        native[p.y0:p.y1, p.x0:p.x1] |= p.mask
    return teacher.piece_from_mask(student, score=score), teacher.piece_from_mask(native, score=score)


def unit_row(rows):
    """The row a label unit is recorded under: the single row, or a synthesis of the merged members."""
    if len(rows) == 1:
        return rows[0]
    return {'index': min(r['index'] for r in rows), 'score': max(r['score'] for r in rows),
            'subtiles': sorted({tuple(s) if isinstance(s, list) else s for r in rows for s in r.get('subtiles', [])})}


def export(root='.', out=None, seed=DEFAULT_SEED, fractions=None, copy_images=True, label_target='raw', policy='none', source_id=None):
    """Write the dataset. label_target 'squared_gated' replaces a teacher outline with its orthogonalised form when the
    source pixels' own edge support accepts it (teacher_judges.gated_orthogonalise), raw otherwise; 'whole_squared_gated'
    first unions the teacher pieces that lie inside the same open building outline (LINZ NZ Building Outlines fetched by
    teacher_outlines) into one instance, then squares as above. Policy 'ledger-v1' removes instances the evidence ledger
    recommends ignoring and paints their pixels IGNORE; 'outline-v1' adds small unmatched instances (see
    _policy_ignored). Defaults keep the export byte-identical to the first release."""
    if label_target not in LABEL_TARGETS or policy not in POLICIES:
        raise SystemExit(f'label_target in {LABEL_TARGETS}, policy in {POLICIES}')
    root = Path(root)
    suffix = LABEL_SUFFIX[label_target] + POLICY_SUFFIX[policy]
    out = Path(out) if out else root / (str(teacher.OUT) + suffix)
    teacher_dir = root / teacher.TEACHER
    ledger_ignored = _policy_ignored(teacher_dir, policy)
    if policy != 'none' and not ledger_ignored:
        raise SystemExit(f'policy {policy} needs teacher_judges rows for this source (none found under {teacher_dir / "judges"})')
    spec = None
    if label_target == 'whole_squared_gated':
        from .teacher_sources import resolved
        from .teacher_sources import source as lookup
        if not source_id:
            raise SystemExit('whole_squared_gated needs the teacher source id (export(source_id=...) / --source) to find its outlines')
        spec = resolved(lookup(source_id), root)
    merge_stats = Counter()
    manifest_path = teacher_dir / 'manifest.json'
    source = common.read_json(manifest_path)
    provenance = common.read_json(teacher_dir / 'provenance.json')
    _, records = teacher.load_records(root)
    records = [r for r in records if r['window']['id'] in {w['id'] for w in source['windows']}]
    windows = {w['id']: w for w in source['windows']}
    splits = assign_splits(sorted(windows), seed, fractions, key=lambda wid: windows[wid].get('shared_id') or wid)
    for folder in ('images', 'labels'):
        (out / folder).mkdir(parents=True, exist_ok=True)
    coco = {split: {'info': {'version': VERSION, 'split': split, 'seed': seed, 'created_at': common.now_iso(),
                             'source': 'hires-teacher-com-2018'},
                    'images': [], 'annotations': [], 'categories': [common.CATEGORY]} for split in SPLITS}
    counts = {split: Counter() for split in SPLITS}
    control, manifest_images, multipart = {}, [], Counter()
    image_id = annotation_id = 0
    for record in records:
        wid = record['window']['id']
        meta = windows[wid]
        split = splits[wid]
        size = meta['native_px']
        width = height = teacher.STUDENT_PX
        px_m = meta['student_px_m']
        student_path = teacher.window_dir(root, wid) / 'student.png'
        digest = common.sha256_file(student_path)
        if digest != record['sha256']['student']:
            counts[split]['_image_sha_mismatch'] += 1
            continue
        instances, ignored = [], []
        gradients = None
        if label_target in SQUARED_TARGETS:
            from . import teacher_judges as tj
            native_rgb = np.asarray(Image.open(root / record['window']['native_file']).convert('RGB'))
            gradients = (tj,) + tj.sobel(tj.gray_f32(native_rgb)) + (native_rgb,)
        groups = {}
        if spec is not None:
            outlines_here = window_outlines(root, spec, record, size, source['gsd_m'])
            if outlines_here is None:
                merge_stats['windows_without_outlines'] += 1
            else:
                merge_stats['windows_with_outlines'] += 1
                merge_stats['outlines_in_windows'] += len(outlines_here)
                pieces = [(row['index'], teacher.exterior_polygon(teacher.rle_piece(row['rle_native'], row['bbox_native'], row['score'])))
                          for row in record['teacher'] if row['student'] and (wid, row['index']) not in ledger_ignored]
                groups, stats = merge_groups(pieces, outlines_here)
                merge_stats.update(stats)
        queue = list(teacher_units(record, groups))
        while queue:
            unit = queue.pop(0)
            # policy first: an ignored member is painted IGNORE on its own and leaves the unit
            kept_rows = []
            for row in unit['rows']:
                if (wid, row['index']) in ledger_ignored:       # policy: the ledger says ignore, so nobody learns this as roof or ground
                    counts[split]['_policy_ignored'] += 1
                    if row['student']:
                        polygon, others, _ = label_polygon(teacher.rle_piece(row['student']['rle'], row['student']['bbox'], row['score']))
                        ignored.extend([p for p in [polygon] + others if p is not None])
                    continue
                kept_rows.append(row)
            if not kept_rows:
                continue
            if len(kept_rows) == 1 and not kept_rows[0]['student']:
                multipart['lost_in_downsample'] += 1
                continue
            row = unit_row(kept_rows)
            piece, native = unit_pieces(kept_rows, size)
            if piece is None:
                multipart['lost_in_downsample'] += 1
                continue
            polygon, others, dropped = label_polygon(piece)
            if polygon is None:
                multipart['no_polygon'] += 1
                continue
            if len(kept_rows) > 1 and dropped:
                # the members do not touch on the student grid: a merged label would paint the smaller part IGNORE (background
                # for SAM3), so the group reverts to its single pieces and is counted
                merge_stats['merged_union_multipart_reverted'] += 1
                queue.extend({'rows': [r], 'merged_from': None, 'outline_id': None} for r in kept_rows)
                continue
            if dropped:
                multipart['instances_with_dropped_parts'] += 1
                multipart['dropped_px'] += dropped
                ignored.extend(others)
            merged_from = [r['index'] for r in kept_rows] if len(kept_rows) > 1 else None
            if merged_from:
                counts[split]['_merged_instances'] += 1
            variant, raw_polygon = 'raw', None
            if gradients is not None:
                tj, gx, gy, mag, native_rgb = gradients
                native_polygon = teacher.exterior_polygon(native)
                squared = tj.gated_orthogonalise(native_polygon, source['gsd_m'], gx, gy, mag, native_rgb) if native_polygon is not None else None
                if squared is not None:
                    scaled = affinity.scale(squared, xfact=width / size, yfact=width / size, origin=(0, 0))
                    scaled = common.valid_polygon(scaled)
                    if scaled is not None and not scaled.is_empty:
                        raw_polygon, polygon, variant = polygon, scaled, 'squared_gated'
            counts[split][f'_label_{variant}'] += 1
            instances.append({'row': row, 'polygon': polygon, 'subpixel': subpixel_polygon(native, width / size),
                              'variant': variant, 'raw_polygon': raw_polygon, 'merged_from': merged_from,
                              'outline_id': unit['outline_id'] if merged_from else None})
        instances.sort(key=lambda i: (-i['polygon'].area, i['row']['index']))
        label = np.zeros((height, width), np.uint16)
        for other in ignored:               # extra parts of a teacher roof: ignore first, instances paint over
            valid = common.valid_polygon(other)
            if valid is not None:
                common.rasterise(valid, width, height, common.IGNORE, label)
        for index, instance in enumerate(instances, start=1):
            common.rasterise(instance['polygon'], width, height, index, label)
            instance['label_index'] = index
        image_id += 1
        file_name, label_name = f'images/{wid}.png', f'labels/{wid}.png'
        if copy_images:
            shutil.copyfile(student_path, out / file_name)
        Image.fromarray(label).save(out / label_name)
        label_sha = common.sha256_file(out / label_name)
        ignore_px = int((label == common.IGNORE).sum())
        counts[split]['_ignore_pixels'] += ignore_px
        grid = f"{teacher.WINDOW_PREFIX}-grid:{meta['grid'][0]}:{meta['grid'][1]}"
        source_tag = teacher.PROVENANCE_TAG.removeprefix('hires-teacher-')
        coco[split]['images'].append({'id': image_id, 'file_name': file_name, 'width': width, 'height': height,
                                      'scene_id': wid, 'window_id': wid, 'sha256': digest, 'label_file': label_name,
                                      'label_sha256': label_sha, 'pixel_size_m': round(px_m, 6), 'tile_group': grid,
                                      'split': split, 'model_signature': record['config_hash'], 'test_tiles_ignored': 0,
                                      'ignore_pixels': ignore_px, 'imagery': 'com-2018-true-ortho' if source_tag == 'com-2018' else source_tag,
                                      'mga55_bounds': meta['mga55_bounds'], 'bounds_crs': meta.get('bounds_crs', meta['mga55_bounds']),
                                      'crs': meta.get('crs') or teacher.SOURCE.get('crs'), 'centre_lonlat': meta['centre_lonlat']})
        for instance in instances:
            annotation_id += 1
            row, polygon = instance['row'], instance['polygon']
            record_row = {'id': annotation_id, 'image_id': image_id, 'category_id': common.CATEGORY['id'], 'iscrowd': 0,
                          **common.polygon_to_coco(polygon), 'label_index': instance['label_index'], 'scene_id': wid,
                          'object_id': f"teacher-{row['index']}", 'provenance': teacher.PROVENANCE_TAG, 'tier': TIER, 'weight': WEIGHT,
                          'boundary_reference': False, 'registry_provenance': f'model:sam3-text-onnx@{source_tag}-native',
                          'review_status': None, 'outline_edited': False, 'source': 'hires_teacher',
                          'image_identity': 'same_image', 'ref_id': None, 'detector_score': row['score'],
                          'area_m2': round(polygon.area * px_m * px_m, 2),
                          'rectilinearity': round(common.rectilinearity(polygon), 4),
                          'teacher_subtiles': row['subtiles'], 'label_variant': instance.get('variant', 'raw'),
                          'merged_from': instance.get('merged_from'), 'outline_id': instance.get('outline_id')}
            if instance.get('raw_polygon') is not None:
                record_row['segmentation_raw'] = common.polygon_to_coco(instance['raw_polygon'])['segmentation']
            regularised = common.orthogonalise(polygon, px_m)
            if regularised is not None:
                record_row['segmentation_regularised'] = common.polygon_to_coco(regularised)['segmentation']
            if instance['subpixel'] is not None:
                record_row['segmentation_subpixel'] = [common.flat_coords(list(instance['subpixel'].exterior.coords)[:-1])]
            coco[split]['annotations'].append(record_row)
            counts[split][teacher.PROVENANCE_TAG] += 1
        control[wid] = control_rows(record)
        counts[split]['_images'] += 1
        counts[split]['_instances'] += len(instances)
        manifest_images.append({'scene_id': wid, 'split': split, 'file': file_name, 'sha256': digest,
                                'label_file': label_name, 'label_sha256': label_sha, 'width': width, 'height': height,
                                'pixel_size_m': round(px_m, 6), 'tile_group': grid, 'n_instances': len(instances),
                                'n_control': len(control[wid]), 'test_tiles_ignored': 0, 'ignore_pixels': ignore_px})
    for split in SPLITS:
        common.write_json_atomic(out / f'{split}.json', coco[split])
    common.write_json_atomic(out / 'references.json', {'version': VERSION, 'note': 'No human outline exists for these '
                                                       'windows; every label is a hi-res SAM3 teacher mask.', 'references': []})
    common.write_json_atomic(out / 'sam3_control.json', {'version': VERSION, 'note': (
        'SAM3 (same prompt, threshold and dedupe) run on the 0.236 m student image of every window; production and raw '
        'are the same outline here (no production refinement ran).'), 'images': control})
    premise = teacher_dir / 'premise' / 'summary.json'
    manifest = {'version': VERSION, 'teacher_source': teacher.SOURCE.get('title'), 'teacher_version': teacher.VERSION,
                'label_target': label_target, 'policy': policy,
                'label_target_note': ({'raw': 'raw teacher outline',
                                       'squared_gated': ('squared_gated: teacher outline replaced by common.orthogonalise at the native pixel when the '
                                                         'source image\'s own edge support accepts it (teacher_judges.gated_orthogonalise), raw otherwise; '
                                                         'judged on the other capture in docs/teacher-enhancement-plan-2026-09-28.md'),
                                       'whole_squared_gated': ('whole_squared_gated: teacher pieces lying at least '
                                                               f'{MERGE_INSIDE:g} inside the same open building outline (teacher_outlines, '
                                                               'metre-class, may predate the imagery) are unioned into one instance (merged_from, '
                                                               'outline_id per annotation), then squared as squared_gated; pieces touching no '
                                                               'outline or covering half of two outlines stay single, and a group whose union '
                                                               'is disconnected on the student grid reverts to single pieces (never IGNORE)')}[label_target]),
                'created_at': common.now_iso(), 'code_sha256': common.code_sha256(), 'seed': seed,
                'fractions': {**FRACTIONS, **(fractions or {})},
                'split_policy': (f"by window (grid cell of the {teacher.SOURCE.get('title')}), ordered by sha256(seed:window id, or the shared cell id for a second capture of the same cells); "
                                 'first round(test fraction * n) test, next round(val fraction * n) validation, rest train'),
                'weights': {teacher.PROVENANCE_TAG: WEIGHT}, 'category': common.CATEGORY, 'ignore_value': common.IGNORE,
                'n_windows': len(manifest_images), 'counts': {s: dict(sorted(counts[s].items())) for s in SPLITS},
                'multipart': dict(multipart), 'merge': dict(sorted(merge_stats.items())) if label_target == 'whole_squared_gated' else None,
                'policy_note': ({'none': None, 'ledger-v1': 'ledger rows recommended ignore are removed from the labels and painted IGNORE',
                                 'outline-v1': f'ledger ignore rows plus outlines-judge unmatched instances under {OUTLINE_SMALL_M2} m2 not matched '
                                               'in the other capture are removed and painted IGNORE; part rows and only-here instances '
                                               'of 25 m2 or more are never dropped'}[policy]),
                'source': {k: provenance.get(k) for k in (
                    'title', 'publisher', 'url', 'catalogue', 'licence', 'licence_url', 'capture', 'crs', 'attribution')},
                'gsd_m': source['gsd_m'], 'student_px_m': source['student_px_m'], 'blur_sigma_px': source['blur_sigma_px'],
                'teacher': {'manifest_sha256': common.sha256_file(manifest_path), 'config': source['config'],
                            'code_sha256': source['code_sha256']},
                'premise_summary_sha256': common.sha256_file(premise) if premise.exists() else None,
                'images': manifest_images}
    manifest['files'] = {name: common.sha256_file(out / name) for name in
                         ('train.json', 'val.json', 'test.json', 'references.json', 'sam3_control.json')}
    common.write_json_atomic(out / 'manifest.json', manifest)
    summary = common.read_json(premise) if premise.exists() else None
    calibration_path = teacher_dir / 'blur_calibration.json'
    calibration = common.read_json(calibration_path) if calibration_path.exists() else None
    (out / 'DATASET_CARD.md').write_text(dataset_card(manifest, source, summary, calibration))
    return manifest


def _premise_lines(summary):
    if not summary:
        return ['The premise measurement has not been run (`hires_teacher measure`).']
    arms = summary['arms']
    counts = summary['counts']
    haus = summary['hausdorff_m']['teacher_native_vs_control']
    paired = summary['paired']
    fill = paired['teacher_native_minus_control.fill']
    return [
        f"On {counts['windows']} windows, {counts['teacher_student_instances']} teacher and {counts['control_instances']} "
        f"control instances, {counts['matched_pairs']} matched at IoU >= 0.5 ({summary['pairs_measured']} measured; "
        f"{counts['matched_pairs']} of {counts['control_instances']} control and {counts['matched_pairs']} of "
        f"{counts['teacher_student_instances']} teacher instances):",
        f"- right corners (turn within 10 degrees of 90): teacher native {arms['teacher_native']['right_corners']} of "
        f"{arms['teacher_native']['corners_total']} corners ({arms['teacher_native']['right_share_pooled']}), teacher on the "
        f"student grid {arms['teacher_student']['right_corners']} of {arms['teacher_student']['corners_total']} "
        f"({arms['teacher_student']['right_share_pooled']}), control {arms['control']['right_corners']} of "
        f"{arms['control']['corners_total']} ({arms['control']['right_share_pooled']});",
        f"- right corners per building, median: teacher native {arms['teacher_native']['right_corners_per_building']['median']}, "
        f"control {arms['control']['right_corners_per_building']['median']}; paired difference median "
        f"{paired['teacher_native_minus_control.right']['median']} (95% window bootstrap "
        f"{paired['teacher_native_minus_control.right']['ci95_window_bootstrap']}; more on "
        f"{paired['teacher_native_minus_control.right']['n_higher']}, fewer on "
        f"{paired['teacher_native_minus_control.right']['n_lower']} of {paired['teacher_native_minus_control.right']['n']});",
        "- the same at a 0.5 m simplification (removes the two-pixel staircases that count as right corners on the 0.236 m "
        f"grid): teacher native {arms['teacher_native']['coarse_0_5m']['right_corners']} of "
        f"{arms['teacher_native']['coarse_0_5m']['corners_total']} ({arms['teacher_native']['coarse_0_5m']['right_share_pooled']}), "
        f"teacher on the student grid {arms['teacher_student']['coarse_0_5m']['right_corners']} of "
        f"{arms['teacher_student']['coarse_0_5m']['corners_total']} "
        f"({arms['teacher_student']['coarse_0_5m']['right_share_pooled']}), control "
        f"{arms['control']['coarse_0_5m']['right_corners']} of {arms['control']['coarse_0_5m']['corners_total']} "
        f"({arms['control']['coarse_0_5m']['right_share_pooled']});",
        f"- rectangle fill, median paired difference teacher native minus control {fill['median']} (95% window bootstrap "
        f"{fill['ci95_window_bootstrap']}; higher on {fill['n_higher']}, lower on {fill['n_lower']} of {fill['n']});",
        f"- boundary Hausdorff teacher native vs control: median {haus['median']} m (IQR {haus['q25']} to {haus['q75']}), "
        f"{haus['n_le_0_45']} of {haus['n']} within 0.45 m; symmetric mean boundary distance median "
        f"{summary['mean_boundary_distance_m']['teacher_native_vs_control']['median']} m (the Hausdorff tail is extent: "
        'merged or split terrace rows and attached wings).',
        'See teacher/premise/summary.json and the contact sheet. This is agreement between two SAM3 runs, not accuracy.']


def _image_lines(manifest, calibration):
    base = (f"One window of the ortho, {teacher.STUDENT_PX} px square at {manifest['student_px_m']} m per pixel (the "
            f"pipeline's 0.236 m scale, about {teacher.STUDENT_PX * manifest['student_px_m']:.0f} m), made from the native "
            f"{manifest['gsd_m']} m crop by exact area averaging")
    if not calibration or 'saved' not in calibration or 'student_at_sigma' not in calibration:
        return [base + f", then a Gaussian blur of sigma {manifest['blur_sigma_px']} px."]
    saved, student = calibration['saved'], calibration['student_at_sigma']
    lines = [base + f", then a Gaussian blur of sigma {calibration['sigma_px']} px ({calibration['match']}).",
             f"The blur was calibrated on the gradient-energy ratio (imagery_sources.gradient_energy of the image over that of "
             f"its 1 px Gaussian): saved Mooroolbark tiles median {saved['ratio']['median']} over {saved['n']} tiles, "
             f"student windows at the chosen sigma {student['ratio']['median']} over {student['n']} windows; edge width "
             f"(10-90 %, imagery_sources.edge_spread) {saved['edge_width_m']['median']} m saved against "
             f"{student['edge_width_m']['median']} m student."]
    sharpen = calibration.get('sharpening_that_would_match')
    if sharpen:
        lines.append(f"The area-averaged windows are already softer than the saved tiles, so no blur can match them. An "
                     f"unsharp mask of amount {sharpen['amount_matching_ratio']} (sigma 1 px) would match the ratio (edge "
                     f"width {sharpen['edge_width_m_at_amount']['median']} m); it was NOT applied. The saved tiles' JPEG "
                     'blocks and provider sharpening add gradient energy the students do not have.')
    lines.append('Colour, season, sun angle and JPEG artefacts are not matched.')
    return [' '.join(lines)]


def dataset_card(manifest, source, summary=None, calibration=None):
    counts = manifest['counts']
    images = {s: counts[s].get('_images', 0) for s in SPLITS}
    instances = {s: counts[s].get('_instances', 0) for s in SPLITS}
    src = manifest['source']
    config = manifest['teacher']['config'] or {}
    lines = [f"# Roof outline distillation dataset ({manifest['version']})", '',
             f"Built {manifest['created_at']} with seed {manifest['seed']} and code sha256 {manifest['code_sha256'][:16]}. "
             f"{manifest['n_windows']} windows: {images['train']} train, {images['val']} validation, {images['test']} test; "
             f"{sum(instances.values())} instances ({instances['train']} / {instances['val']} / {instances['test']}).", '',
             '## Source and licence', '',
             f"{src['title']} ({src['publisher']}), captured {src['capture']}, {src['crs']}, licence {src['licence']} "
             f"({src['licence_url']}). Catalogue: {src['catalogue']}.", '',
             f"Attribution (required by {src['licence']}): {src['attribution']}", '',
             '## What an image is', '', *_image_lines(manifest, calibration), '',
             '## What a label is', '',
             f"`{teacher.PROVENANCE_TAG}`, tier {TIER}, weight {WEIGHT:g}, never a boundary reference. SAM3 (text ONNX int8, prompt "
             f"'{config.get('prompt')}', threshold {config.get('threshold')}) ran on the NATIVE crop in overlapping "
             f"{config.get('subtile')} px sub-tiles (resized to 1008 px by the encoder, so about "
             f"{manifest['gsd_m'] * (config.get('subtile') or 1152) / 1008:.3f} m per model pixel and about "
             f"{manifest['gsd_m'] * (config.get('subtile') or 1152) / 288:.2f} m per mask cell, against 0.94 m per mask cell on the "
             'student image). Pieces were stitched across '
             'sub-tiles, deduplicated with the production building rule, area-averaged onto the student grid and kept '
             'where at least half a student pixel was covered. The label PNG is the exact rasterisation (pixel centres) '
             'of the COCO polygon. `segmentation_subpixel` keeps the native outline in student coordinates. These are '
             'model outputs: finer pixels do not make SAM3 right, and nothing here was checked by a person.', '',
             '## Measured premise (does the fine pixel give SAM3 squarer corners?)', '', *_premise_lines(summary), '',
             '## Split', '', manifest['split_policy'] + '. Validation and test scores measure agreement with the hi-res '
             'teacher, not with people and not accuracy.', '']
    for split in SPLITS:
        lines.append(f"- {split}: {images[split]} images, {instances[split]} instances, "
                     f"{counts[split].get('_ignore_pixels', 0)} ignore pixels")
    multipart = manifest['multipart']
    lines += ['', '## Left out', '',
              f"- teacher instances too small on the student grid (under 1 m2 after area averaging): "
              f"{multipart.get('lost_in_downsample', 0)}",
              f"- instances whose student-grid mask had several parts (only the largest is the label; the other parts are "
              f"ignore pixels, not background): {multipart.get('instances_with_dropped_parts', 0)} "
              f"({multipart.get('dropped_px', 0)} px)", '',
              '## Caveats', '',
              f"- Domain gap: {teacher.SOURCE.get('place', 'the source area')}, {teacher.SOURCE.get('capture', 'capture date unknown')} "
              f"(similarity to the pipeline suburb: {teacher.SOURCE.get('similarity', 'not rated')}), versus detached Mooroolbark "
              'houses in the Google imagery the pipeline sees. Tone, season and sensor differ.',
              '- The teacher is still SAM3: its masks are bilinear upsamples of a 288 x 288 grid per sub-tile, so corners '
              'round at about 0.40 m instead of 0.94 m; merged terrace rows and missed roofs carry over as label noise.',
              "- Do not merge into dataset-v1 blindly: its pseudo-labels are SAM3 on Google imagery; train with both only "
              'as an explicit mixed-source run with this provenance kept per instance.',
              '- Every window lies wholly inside the imagery (checked on the native crop: alpha band for tiled sources, the '
              'no-data fill colour for the City of Melbourne JPEG2000), so the only ignore pixels are the extra parts of '
              'multi-part teacher roofs.',
              '- Overlapping teacher instances are painted larger first in the label PNG (a smaller one keeps its pixels); '
              'the COCO polygons are complete, as in dataset-v1.',
              '- Stitching seams: where two sub-tiles disagree on a roof\'s extent, the pixel is decided by the sub-tile whose '
              'core holds it, which can leave a jagged step along a core line (visible on the contact sheet).', '']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--out', default=None)
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED)
    parser.add_argument('--tarball', action='store_true')
    parser.add_argument('--source', default='com-2018', help='teacher_sources registry id; sets the teacher run and output folder')
    parser.add_argument('--label-target', choices=LABEL_TARGETS, default='raw')
    parser.add_argument('--policy', choices=POLICIES, default='none')
    args = parser.parse_args(argv)
    teacher.set_source(args.source, args.root)
    manifest = export(args.root, args.out, seed=args.seed, label_target=args.label_target, policy=args.policy, source_id=args.source)
    summary = {k: v for k, v in manifest.items() if k != 'images'}
    if args.tarball:
        summary['tarball'] = common.write_tarball(Path(args.out or Path(args.root) / teacher.OUT))
    json.dump(summary, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
