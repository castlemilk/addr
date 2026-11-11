"""Export the roof-outline instance dataset (COCO-style JSON + uint16 label PNGs + manifest + card).

Sources, per scene under data/scenes (read-only):
  latest.json   kind 'building' objects from the SAM3 detector (source sam3-text-onnx) -> machine_pseudo, weight 1.
  review.json   human outlines graded with reference_registry.review_tier against the model snapshot the reviewer saw:
                confirmed + geometry_reviewed (human_T1); needs_adjustment whose ring differs from the model ring
                (human_T2); roof_line_edits with an unchanged ring (human_T2_lines_only, kept but never a boundary
                reference because its outline is SAM3's). Objects with provenance.source machine:* are excluded.
  eyes.sqlite   'references' rows of tier T1/T2, joined by (scene_id, object_id) or, for WGS84-only rows (CRM),
                placed in every scene frame that holds their centroid; projected with reference_agreement helpers.

Pixels are never taught as background when a building is known but unusable: machine labels, unedited
needs_adjustment outlines, superseded objects without a human replacement, unreviewed review-only drafts,
roof-pipeline seeds nobody else covers, seeds cut by an earlier smaller frame (clipped_by_frame), sub-floor slivers
and invalid rings are written as IGNORE (65535). On window scenes (latest.json carries a window_frame dict: lot-*, win-*)
any building outline, human or pseudo, within FRAME_CUT_EDGE_PX of the image edge or not covered by it is frame_cut:
the straight edge is the 512 px window, not the roof, so it is IGNORE too (exclusions frame_cut_human / frame_cut).

Split policy (fixed seed, by z17 tile of the image centre): every tile that holds a human reference is held-out
test. Train/val images carry up to 64 px of neighbouring tiles, so pixels inside a test tile are IGNORE and
instances touching one are dropped (their pixels IGNORE too). Remaining tiles hash into val/train.

Nothing here writes under data/scenes or the eyes index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import box, shape

from .. import eyes_index
from .. import reference_agreement as agreement
from .. import reference_registry as registry
from . import common, metrics
from .common import (
    BOUNDARY_PROVENANCES,
    CATEGORY,
    HUMAN_PROVENANCES,
    IGNORE,
    PROVENANCE_WEIGHTS,
    VERSION,
    centre_group,
    frame_tile_groups,
    now_iso,
    pixel_size_m,
    polygon_to_coco,
    rasterise,
    rectilinearity,
    ring_polygon,
    tile_polygon_pixels,
    write_json_atomic,
)

SPLITS = ('train', 'val', 'test')
PSEUDO_SOURCES = ('sam3-text-onnx',)
DEFAULT_SEED = 20260927
DEFAULT_VAL_FRACTION = .15
REPLACE_IOU = .3            # a pseudo-label this close to a human outline is the same roof
REPLACE_COVER = .5          # ... or when half of it lies under the human outline
SEED_COVER = .5             # a roof-pipeline seed covered this much by a kept instance needs no IGNORE
SCENE_ID = registry.SCENE_ID
# One edge tolerance for "the frame cut it": scene_lot_tags.DESIGN['edge_px'] (that module pulls cv2/scipy and the
# scene pipeline, so the value is restated here; test_training_export asserts the two stay equal).
FRAME_CUT_EDGE_PX = 1.


def frame_cut(polygon, width, height, edge_px=FRAME_CUT_EDGE_PX):
    """True when the outline touches the image frame (within edge_px of its boundary) or is not covered by it."""
    frame = box(0, 0, width, height)
    return polygon.distance(frame.boundary) <= edge_px or not frame.covers(polygon)


def window_scene(scene):
    """A frame-cut window capture (lot-*, win-*): its edges cut roofs, so edge-touching outlines are not labels."""
    return isinstance(scene.get('window_frame'), dict)


def find_png(folder, image_sha256):
    """The saved scene image: <sha256>.png, else a single <sha[:8]>*.png."""
    exact = folder / f'{image_sha256}.png'
    if exact.exists():
        return exact
    short = sorted(folder.glob(f'{image_sha256[:8]}*.png'))
    return short[0] if len(short) == 1 else None


def load_scene(root, scene_id):
    folder = Path(root) / 'data/scenes' / scene_id
    latest = folder / 'latest.json'
    if not latest.exists() or not SCENE_ID.fullmatch(scene_id) or scene_id.startswith('qa_'):
        return None
    scene = common.read_json(latest)
    if scene.get('id') != scene_id or not registry.SHA256.fullmatch(str(scene.get('image_sha256'))):
        return None
    png = find_png(folder, scene['image_sha256'])
    if png is None:
        return None
    review_path = folder / 'review.json'
    review = common.read_json(review_path) if review_path.exists() else None
    return {'scene_id': scene_id, 'folder': folder, 'scene': scene, 'png': png,
            'review': review if isinstance(review, dict) else None}


def list_scene_ids(root, scene_ids=None, limit=None):
    if scene_ids:
        ids = list(dict.fromkeys(scene_ids))
    else:
        ids = sorted(p.parent.name for p in (Path(root) / 'data/scenes').glob('*/latest.json'))
    return ids[:limit] if limit else ids


def eyes_references(root):
    """T1/T2 building rows from the eyes 'references' table keyed by (scene_id, object_id); WGS84-only rows apart."""
    keyed, orphans = {}, []
    if not eyes_index.path(root).exists():
        return keyed, orphans
    with closing(eyes_index.connection(root, readonly=True)) as db:
        try:
            rows = eyes_index.rows(db, 'references', version=registry.VERSION)
        except sqlite3.OperationalError:
            rows = []
    for row in rows:
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        if row.get('tier') not in registry.LABEL_TIERS or payload.get('object_kind') != 'building':
            continue
        if not payload.get('independent_geometry'):
            continue
        if row.get('scene_id') and row.get('object_id'):
            keyed[(row['scene_id'], row['object_id'])] = row
        else:
            orphans.append(row)
    return keyed, orphans


def review_model(entry):
    """({id: model object the reviewer saw}, verified) or (None, False) when the review's frame is not this image."""
    scene, review = entry['scene'], entry['review']
    if not review or not isinstance(review.get('objects'), list):
        return None, False
    snapshot = entry['folder'] / 'models' / f"{review.get('model_signature')}.json"
    model = None
    if snapshot.exists():
        try:
            model = common.read_json(snapshot)
        except ValueError:
            model = None
        if model is not None and model.get('image_sha256') != scene.get('image_sha256'):
            return None, False
    verified = model is not None or review.get('model_signature') == scene.get('model_signature')
    model = model or scene
    return {o.get('id'): o for o in model.get('objects', []) if isinstance(o, dict)}, verified


def _same_holes(a, b):
    return registry._same_holes(a, b)


def _instance(oid, polygon, provenance, *, tier, source, registry_provenance=None, review_status=None,
              outline_edited=False, image_identity='same_image', ref_id=None, score=None, model_polygon=None,
              model_pair=None, edit_kind=None, holes_changed=None):
    return {'object_id': oid, 'polygon': polygon, 'provenance': provenance, 'tier': tier,
            'weight': PROVENANCE_WEIGHTS[provenance], 'boundary_reference': provenance in BOUNDARY_PROVENANCES,
            'registry_provenance': registry_provenance, 'review_status': review_status,
            'outline_edited': bool(outline_edited), 'source': source, 'image_identity': image_identity,
            'ref_id': ref_id, 'score': score, 'model_polygon': model_polygon, 'model_pair': model_pair,
            'edit_kind': edit_kind, 'holes_changed': holes_changed}


def review_instances(entry, exclusions):
    """Human outlines from review.json; plus IGNORE polygons, model ids they settle, and every reviewed id."""
    humans, ignored, settled, reviewed = {}, [], set(), set()
    review = entry['review']
    if not review:
        return humans, ignored, settled, reviewed
    model_objects, verified = review_model(entry)
    if model_objects is None:
        exclusions['review_frame_mismatch'] += 1
        return humans, ignored, settled, reviewed
    scene = entry['scene']
    is_window, width, height = window_scene(scene), scene['image_width'], scene['image_height']
    for obj in review['objects']:
        if not isinstance(obj, dict) or obj.get('kind') != 'building' or not obj.get('id'):
            continue
        oid = obj['id']
        reviewed.add(oid)
        model_obj = model_objects.get(oid)
        model_ring = model_obj.get('ring') if isinstance(model_obj, dict) else None
        model_holes = (model_obj.get('holes') or []) if isinstance(model_obj, dict) else None
        tier, provenance, _ = registry.review_tier(obj, model_ring, verified, model_holes=model_holes)
        status = obj.get('status')
        polygon = ring_polygon(obj.get('ring'), obj.get('holes'))
        if provenance == 'review:machine_label':
            exclusions['machine_provenance'] += 1
            settled.add(oid)
            if polygon is not None:
                ignored.append((polygon, 'machine_provenance'))
            continue
        if tier is None:  # rejected: a human said this is not a roof, so its pixels stay background
            exclusions[f'review_{status}'] += 1
            settled.add(oid)
            continue
        if tier == 'T4':  # superseded; replacements are separate objects
            exclusions['review_superseded'] += 1
            settled.add(oid)
            if polygon is not None:
                ignored.append((polygon, 'superseded'))
            continue
        if tier not in registry.LABEL_TIERS:
            if status == 'needs_adjustment':
                exclusions['needs_adjustment_unedited'] += 1
                settled.add(oid)
                if polygon is not None:
                    ignored.append((polygon, 'needs_adjustment_unedited'))
            elif model_ring is None and polygon is not None:
                # A review-only draft (trace, split part) nobody approved: real roof, unknown outline.
                exclusions['review_draft_unapproved'] += 1
                ignored.append((polygon, 'review_draft_unapproved'))
            elif str(provenance).startswith('review:fieldwork'):
                exclusions['fieldwork_pending'] += 1
            continue
        if polygon is None:
            exclusions['invalid_geometry_human'] += 1
            settled.add(oid)
            continue
        if is_window and frame_cut(polygon, width, height):
            # The window frame cut this roof: the human confirmed what is visible, not the straight frame edge.
            exclusions['frame_cut_human'] += 1
            settled.add(oid)
            ignored.append((polygon, 'frame_cut'))
            continue
        edited = registry.outline_edited(obj, model_ring, model_holes)
        if tier == 'T1':
            label = 'human_T1'
        elif edited:
            label = 'human_T2'
        else:
            label = 'human_T2_lines_only'
        # The model estimate the reviewer edited from travels with the instance (segmentation_model, model_iou,
        # model_hausdorff_m) so a consumer can measure how far the outline moved; only when it is the snapshot the
        # reviewer actually saw. The numbers are agreement between the two outlines, not accuracy.
        model_polygon = ring_polygon(model_obj.get('ring'), model_obj.get('holes')) if model_ring is not None and verified else None
        holes_changed = None if model_ring is None else not _same_holes(obj.get('holes'), model_holes)
        if model_ring is None:
            edit_kind = 'manual'
        elif not registry._same_ring(obj.get('ring'), model_ring):
            edit_kind = 'outline'
        elif holes_changed:
            edit_kind = 'holes_only'
        else:
            edit_kind = 'none'
        model_pair = metrics.outline_pair(polygon, model_polygon, pixel_size_m(entry['scene'])) if model_polygon is not None else None
        humans[oid] = _instance(oid, polygon, label, tier=tier, source='review.json', registry_provenance=provenance,
                                review_status=status, outline_edited=edited, model_polygon=model_polygon,
                                model_pair=model_pair, edit_kind=edit_kind, holes_changed=holes_changed)
        settled.add(oid)
    return humans, ignored, settled, reviewed


def reference_pixels(row, scene):
    """A registry row's outline in this scene's pixels via reference_agreement.reference_polygon (local metres)."""
    local = agreement.reference_polygon(row, scene)
    ring = common.local_to_pixel(local.exterior.coords, scene)
    holes = [common.local_to_pixel(h.coords, scene) for h in local.interiors]
    return ring_polygon(ring, holes)


def merge_eyes_references(entry, humans, reviewed, eyes_by_key, exclusions):
    """Attach eyes ref_ids; add T1/T2 rows whose object the current review.json no longer holds."""
    scene_id, scene = entry['scene_id'], entry['scene']
    for (sid, oid), row in sorted(eyes_by_key.items()):
        if sid != scene_id:
            continue
        if oid in humans:
            humans[oid]['ref_id'] = row['ref_id']
            continue
        if oid in reviewed:
            # The review file is newer than the registry and no longer calls this a human outline.
            exclusions['eyes_ref_superseded_by_review'] += 1
            continue
        payload = row['payload']
        if payload.get('image_sha256') != scene.get('image_sha256'):
            exclusions['eyes_ref_frame_mismatch'] += 1
            continue
        try:
            polygon = reference_pixels(row, scene)
        except registry.SKIPPABLE:
            polygon = None
        if polygon is None:
            exclusions['invalid_geometry_human'] += 1
            continue
        label = 'human_T1' if row['tier'] == 'T1' else 'human_T2'
        humans[oid] = _instance(oid, polygon, label, tier=row['tier'], source='eyes.references',
                                registry_provenance=payload.get('provenance'), review_status=payload.get('status'),
                                outline_edited=True, ref_id=row['ref_id'])
    return humans


def place_orphan_references(orphans, entries, exclusions):
    """WGS84-only references (CRM rows) land in every scene frame whose pixels hold their centroid."""
    placed = defaultdict(list)
    for row in orphans:
        payload = row['payload']
        try:
            registry.valid(shape(payload['geometry_wgs84']))
        except (KeyError, *registry.SKIPPABLE):
            exclusions['reference_invalid_wgs84'] += 1
            continue
        hits = 0
        for entry in entries:
            scene = entry['scene']
            if row.get('spatial_group') not in frame_tile_groups(scene):
                continue
            try:
                polygon = reference_pixels(row, scene)
            except registry.SKIPPABLE:
                continue
            if polygon is None:
                continue
            centre = polygon.centroid
            if not (0 <= centre.x < scene['image_width'] and 0 <= centre.y < scene['image_height']):
                continue
            source = payload.get('boundary_image_sha256') or payload.get('image_sha256')
            identity = 'same_image' if source == scene.get('image_sha256') else 'different_image'
            label = 'human_T1' if row['tier'] == 'T1' else 'human_T2'
            placed[entry['scene_id']].append(_instance(
                f"ref-{row['ref_id']}", polygon, label, tier=row['tier'], source='eyes.references.wgs84',
                registry_provenance=payload.get('provenance'), outline_edited=True, image_identity=identity,
                ref_id=row['ref_id']))
            hits += 1
        if not hits:
            exclusions['reference_without_scene'] += 1
    return placed


def _replaced(polygon, human_polygons):
    for human in human_polygons:
        overlap = polygon.intersection(human).area
        if overlap <= 0:
            continue
        if overlap / max(polygon.area, 1e-9) >= REPLACE_COVER or overlap / polygon.union(human).area >= REPLACE_IOU:
            return True
    return False


def pseudo_instances(entry, settled, human_polygons, exclusions, min_area_m2):
    """SAM3 building pseudo-labels from latest.json, plus IGNORE polygons for unusable or unknown roofs."""
    scene = entry['scene']
    px_m = pixel_size_m(scene)
    min_area_px = min_area_m2 / (px_m * px_m)
    is_window, width, height = window_scene(scene), scene['image_width'], scene['image_height']
    found, ignored, seeds = [], [], []
    for obj in scene.get('objects', []):
        if not isinstance(obj, dict) or obj.get('kind') != 'building':
            continue
        oid = obj.get('id')
        if oid in settled:
            continue
        polygon = ring_polygon(obj.get('ring'), obj.get('holes'))
        if obj.get('source') not in PSEUDO_SOURCES:
            exclusions['non_sam3_source'] += 1
            if polygon is not None:
                seeds.append(polygon)
            continue
        if obj.get('status') in ('rejected', 'superseded'):
            exclusions[f"model_{obj['status']}"] += 1
            continue
        if polygon is None:
            exclusions['invalid_geometry_pseudo'] += 1
            continue
        if _replaced(polygon, human_polygons):
            exclusions['pseudo_replaced_by_human'] += 1
            continue
        if obj.get('clipped_by_frame'):
            # The straight edge is the earlier, smaller capture frame, not the roof: a real roof of unknown extent.
            exclusions['clipped_by_frame'] += 1
            ignored.append((polygon, 'clipped_by_frame'))
            continue
        if is_window and frame_cut(polygon, width, height):
            # The window frame cut this roof: a real roof of unknown extent, never a label with a fake straight edge.
            exclusions['frame_cut'] += 1
            ignored.append((polygon, 'frame_cut'))
            continue
        if polygon.area < min_area_px:
            exclusions['too_small'] += 1
            ignored.append((polygon, 'too_small'))
            continue
        instance = _instance(oid, polygon, 'machine_pseudo', tier='T3', source='latest.json',
                             registry_provenance='model:sam3-text-onnx', score=obj.get('score'))
        instance['rectilinearity'] = rectilinearity(polygon)
        regularised = common.orthogonalise(polygon, px_m)
        if regularised is not None:
            instance['regularised'] = regularised
        found.append(instance)
    kept = [i['polygon'] for i in found] + list(human_polygons)
    for seed in seeds:
        covered = sum(seed.intersection(k).area for k in kept if seed.intersects(k))
        if covered / max(seed.area, 1e-9) < SEED_COVER:
            ignored.append((seed, 'non_sam3_seed_uncovered'))
    return found, ignored


def sam3_control(scene):
    """SAM3's own outlines for the control: the production ring and the raw detector ring where retained."""
    rows = []
    for obj in scene.get('objects', []):
        if not isinstance(obj, dict) or obj.get('kind') != 'building' or obj.get('source') not in PSEUDO_SOURCES:
            continue
        if obj.get('status') in ('rejected', 'superseded'):
            continue
        production = ring_polygon(obj.get('ring'), obj.get('holes'))
        refinement = obj.get('boundary_refinement') if isinstance(obj.get('boundary_refinement'), dict) else {}
        raw = ring_polygon(refinement.get('raw_ring'), refinement.get('raw_holes')) if refinement.get('raw_ring') else None
        if production is None:
            continue
        rows.append({'object_id': obj.get('id'), 'score': obj.get('score'),
                     'production': polygon_to_coco(production), 'raw': polygon_to_coco(raw or production),
                     'raw_retained': raw is not None})
    return rows


def assign_splits(entries, reference_groups, seed, val_fraction):
    """scene_id -> split. Test: centre tile holds a human reference. Else hash(seed, tile) -> val/train."""
    splits = {}
    for entry in entries:
        group = centre_group(entry['scene'])
        if group in reference_groups:
            splits[entry['scene_id']] = 'test'
            continue
        bucket = int(hashlib.sha256(f'{seed}:{group}'.encode()).hexdigest()[:8], 16) % 10_000
        splits[entry['scene_id']] = 'val' if bucket < int(round(val_fraction * 10_000)) else 'train'
    return splits


def _label_index_order(instances):
    # Larger outlines first so a shed nested in a house outline keeps its own index in the label map.
    return sorted(instances, key=lambda i: (-i['polygon'].area, i['object_id']))


def _paint_ignore(label, polygons, width, height):
    frame = box(0, 0, width, height)
    for polygon in polygons:
        clipped = common.valid_polygon(polygon.intersection(frame))
        if clipped is not None:
            rasterise(clipped, width, height, IGNORE, label)


def human_groups(entry, humans):
    """z17 tiles a scene's human outlines touch: the image centre and each outline centroid."""
    groups = {centre_group(entry['scene'])}
    for human in humans:
        centre = human['polygon'].centroid
        groups.add(common.pixel_group([centre.x, centre.y], entry['scene']))
    return groups


def reference_tiles(root, entries, eyes_by_key, orphans):
    """Every z17 tile holding a human reference, independent of which scenes are exported.

    Registry rows give their own spatial_group; review.json outlines newer than the registry are found by
    re-reading every reviewed scene, so a restricted export assigns the same splits as the full one.
    """
    groups = {row['spatial_group'] for row in [*eyes_by_key.values(), *orphans] if row.get('spatial_group')}
    seen = {e['scene_id'] for e in entries}
    others = [load_scene(root, p.parent.name) for p in sorted((Path(root) / 'data/scenes').glob('*/review.json'))
              if p.parent.name not in seen]
    for entry in [*entries, *(o for o in others if o)]:
        humans = review_instances(entry, Counter())[0]
        if humans:
            groups |= human_groups(entry, humans.values())
    return groups


def export(root, out, *, seed=DEFAULT_SEED, val_fraction=DEFAULT_VAL_FRACTION, scene_ids=None, limit=None,
           min_area_m2=4., copy_images=True):
    root, out = Path(root), Path(out)
    exclusions, ignore_reasons = Counter(), Counter()
    entries = [e for e in (load_scene(root, sid) for sid in list_scene_ids(root, scene_ids, limit)) if e]
    eyes_by_key, orphans = eyes_references(root)
    orphan_placements = place_orphan_references(orphans, entries, exclusions)
    reference_groups = reference_tiles(root, entries, eyes_by_key, orphans)

    targets, cross_image, per_scene_ignored = {}, {}, {}
    for entry in entries:
        sid = entry['scene_id']
        humans, ignored, settled, reviewed = review_instances(entry, exclusions)
        humans = merge_eyes_references(entry, humans, reviewed, eyes_by_key, exclusions)
        human_list = list(humans.values()) + orphan_placements.get(sid, [])
        same = [h for h in human_list if h['image_identity'] == 'same_image']
        # Only same-image human outlines replace a pseudo-label; a cross-image reference is evaluation-only.
        pseudo, pseudo_ignored = pseudo_instances(entry, settled, [h['polygon'] for h in same], exclusions, min_area_m2)
        targets[sid] = same + pseudo
        cross_image[sid] = [h for h in human_list if h['image_identity'] != 'same_image']
        per_scene_ignored[sid] = ignored + pseudo_ignored
        if human_list:
            reference_groups |= human_groups(entry, human_list)
    splits = assign_splits(entries, reference_groups, seed, val_fraction)

    for folder in ('images', 'labels'):
        (out / folder).mkdir(parents=True, exist_ok=True)
    coco = {split: {'info': {'version': VERSION, 'split': split, 'seed': seed, 'created_at': now_iso()},
                    'images': [], 'annotations': [], 'categories': [CATEGORY]} for split in SPLITS}
    manifest_images, counts = [], {split: Counter() for split in SPLITS}
    references, control = [], {}
    image_id = annotation_id = 0
    for entry in entries:
        scene, sid = entry['scene'], entry['scene_id']
        split = splits[sid]
        width, height = scene['image_width'], scene['image_height']
        instances = targets[sid]
        ignored = list(per_scene_ignored[sid])
        test_tiles = []
        if split != 'test':
            touched = [g for g in frame_tile_groups(scene) if g in reference_groups]
            test_tiles = [tile_polygon_pixels(g, scene) for g in touched]
            kept = []
            for instance in instances:
                if any(instance['polygon'].intersects(t) for t in test_tiles):
                    exclusions['test_tile_overlap'] += 1
                    ignored.append((instance['polygon'], 'test_tile_overlap'))
                    continue
                kept.append(instance)
            instances = kept
        data = entry['png'].read_bytes()
        digest = common.sha256_bytes(data)
        if digest != scene['image_sha256']:
            exclusions['image_sha_mismatch'] += 1
            continue
        image_id += 1
        file_name, label_name = f'images/{sid}.png', f'labels/{sid}.png'
        if copy_images:
            shutil.copyfile(entry['png'], out / file_name)
        label = np.zeros((height, width), dtype=np.uint16)
        _paint_ignore(label, test_tiles, width, height)
        _paint_ignore(label, [p for p, _ in ignored], width, height)
        ignore_reasons.update(reason for _, reason in ignored)
        ordered = _label_index_order(instances)
        for index, instance in enumerate(ordered, start=1):
            rasterise(instance['polygon'], width, height, index, label)
            instance['label_index'] = index
        Image.fromarray(label).save(out / label_name)
        label_sha = common.sha256_file(out / label_name)
        px_m = pixel_size_m(scene)
        ignore_px = int((label == IGNORE).sum())
        coco[split]['images'].append({'id': image_id, 'file_name': file_name, 'width': width, 'height': height,
                                      'scene_id': sid, 'sha256': digest, 'label_file': label_name, 'label_sha256': label_sha,
                                      'pixel_size_m': round(px_m, 6), 'tile_group': centre_group(scene), 'split': split,
                                      'model_signature': scene.get('model_signature'), 'test_tiles_ignored': len(test_tiles),
                                      'ignore_pixels': ignore_px})
        cross = cross_image[sid] if split == 'test' else []
        for instance in ordered + cross:
            annotation_id += 1
            record = {'id': annotation_id, 'image_id': image_id, 'category_id': CATEGORY['id'], 'iscrowd': 0,
                      **polygon_to_coco(instance['polygon']), 'label_index': instance.get('label_index'),
                      'scene_id': sid, 'object_id': instance['object_id'], 'provenance': instance['provenance'],
                      'tier': instance['tier'], 'weight': instance['weight'], 'boundary_reference': instance['boundary_reference'],
                      'registry_provenance': instance['registry_provenance'], 'review_status': instance['review_status'],
                      'outline_edited': instance['outline_edited'], 'source': instance['source'],
                      'image_identity': instance['image_identity'], 'ref_id': instance['ref_id'],
                      'detector_score': instance['score'], 'area_m2': round(instance['polygon'].area * px_m * px_m, 2)}
            model_polygon = instance.get('model_polygon')
            model_coco = polygon_to_coco(model_polygon) if model_polygon is not None else None
            pair = instance.get('model_pair') or {}
            record['edit_kind'] = instance.get('edit_kind')
            record['segmentation_model'] = model_coco['segmentation'] if model_coco else None
            record['holes_model'] = model_coco['holes'] if model_coco else None
            record['model_iou'] = round(pair['iou'], 4) if pair.get('iou') is not None else None
            record['model_hausdorff_m'] = round(pair['hausdorff_m'], 3) if pair.get('hausdorff_m') is not None else None
            record['model_vertex_delta'] = pair.get('vertex_delta')
            record['holes_changed'] = instance.get('holes_changed')
            if 'rectilinearity' in instance:
                record['rectilinearity'] = round(instance['rectilinearity'], 4)
            if 'regularised' in instance:
                record['segmentation_regularised'] = polygon_to_coco(instance['regularised'])['segmentation']
            if instance.get('label_index') is None:
                record['evaluation_only'] = True   # drawn over another image: never a training target
                counts[split]['_evaluation_only'] += 1
            coco[split]['annotations'].append(record)
            counts[split][instance['provenance']] += 1
            if split == 'test' and instance['provenance'] in HUMAN_PROVENANCES:
                references.append({**record, 'file_name': file_name, 'width': width, 'height': height,
                                   'pixel_size_m': round(px_m, 6)})
        if split == 'test':
            control[sid] = sam3_control(scene)
        counts[split]['_images'] += 1
        counts[split]['_instances'] += len(ordered)
        if any(i['provenance'] in HUMAN_PROVENANCES for i in ordered + cross):
            counts[split]['_images_with_human_reference'] += 1
        manifest_images.append({'scene_id': sid, 'split': split, 'file': file_name, 'sha256': digest, 'label_file': label_name,
                                'label_sha256': label_sha, 'width': width, 'height': height, 'pixel_size_m': round(px_m, 6),
                                'tile_group': centre_group(scene), 'n_instances': len(ordered),
                                'test_tiles_ignored': len(test_tiles), 'ignore_pixels': ignore_px})

    for split in SPLITS:
        write_json_atomic(out / f'{split}.json', coco[split])
    edited_pairs = [a for split in SPLITS for a in coco[split]['annotations']
                    if a['segmentation_model'] is not None and a['edit_kind'] in ('outline', 'holes_only')]
    write_json_atomic(out / 'references.json', {'version': VERSION, 'note': common.NOTE, 'references': references})
    write_json_atomic(out / 'sam3_control.json', {'version': VERSION, 'note': 'SAM3 building outlines of every test image: '
                                                  'production ring (latest.json) and raw detector ring (boundary_refinement.raw_ring).',
                                                  'images': control})
    manifest = {'version': VERSION, 'created_at': now_iso(), 'code_sha256': common.code_sha256(), 'seed': seed,
                'val_fraction': val_fraction, 'min_area_m2': min_area_m2,
                'orthogonal_regulariser': common.ORTHO,
                'split_policy': ('by z17 tile of the image centre; every tile holding a human T1/T2 reference is test; '
                                 'train/val pixels inside a test tile are IGNORE and instances touching one are dropped'),
                'weights': PROVENANCE_WEIGHTS, 'category': CATEGORY, 'ignore_value': IGNORE,
                'n_scenes_seen': len(entries), 'reference_groups': sorted(reference_groups),
                'counts': {split: dict(sorted(counts[split].items())) for split in SPLITS},
                'n_regularised': sum('segmentation_regularised' in a for split in SPLITS for a in coco[split]['annotations']),
                'n_boundary_references': sum(r['boundary_reference'] for r in references),
                'n_human_references_distinct': len({_reference_key(r) for r in references}),
                'n_boundary_references_distinct': len({_reference_key(r) for r in references if r['boundary_reference']}),
                'n_boundary_references_same_image': sum(r['boundary_reference'] and r['image_identity'] == 'same_image'
                                                        for r in references),
                'exclusions': dict(sorted(exclusions.items())), 'ignore_reasons': dict(sorted(ignore_reasons.items())),
                'images': manifest_images, 'eyes_references_joined': len(eyes_by_key),
                'eyes_references_wgs84_only': len(orphans),
                'n_human_with_model_pair': sum(a['segmentation_model'] is not None for split in SPLITS for a in coco[split]['annotations']),
                'n_outline_edited_with_model_pair': sum(a['segmentation_model'] is not None and a['edit_kind'] == 'outline'
                                                        for split in SPLITS for a in coco[split]['annotations']),
                # Over the pairs whose outline or holes the reviewer changed: how far the outline moved, not accuracy.
                'model_iou_median': _median(a['model_iou'] for a in edited_pairs),
                'model_hausdorff_m_median': _median(a['model_hausdorff_m'] for a in edited_pairs),
                'schema_additions': ['segmentation_model', 'holes_model', 'model_iou', 'model_hausdorff_m',
                                     'model_vertex_delta', 'holes_changed', 'edit_kind']}
    manifest['files'] = {name: common.sha256_file(out / name) for name in
                         ('train.json', 'val.json', 'test.json', 'references.json', 'sam3_control.json')}
    write_json_atomic(out / 'manifest.json', manifest)
    (out / 'DATASET_CARD.md').write_text(dataset_card(manifest))
    return manifest


def _median(values):
    values = [v for v in values if v is not None]
    return float(np.median(values)) if values else None


def _reference_key(record):
    """One physical reference: a registry row, else the (scene, object) it came from."""
    return record.get('ref_id') or f"{record['scene_id']}:{record['object_id']}"


def _fmt(value, digits):
    return 'n/a (no edited pair)' if value is None else f'{value:.{digits}f}'


def _share(part, whole):
    return f'{part} of {whole}' if whole else f'{part} of 0'


def dataset_card(manifest):
    counts = manifest['counts']
    images = {s: counts[s].get('_images', 0) for s in SPLITS}
    total_images = sum(images.values())
    humans = {s: sum(v for k, v in counts[s].items() if k in HUMAN_PROVENANCES) for s in SPLITS}
    pseudo = {s: counts[s].get('machine_pseudo', 0) for s in SPLITS}
    total_humans, total_pseudo = sum(humans.values()), sum(pseudo.values())
    excl = manifest['exclusions']
    lines = [f"# Roof outline dataset ({manifest['version']})", '',
             f"Built {manifest['created_at']} from {manifest['n_scenes_seen']} scenes under data/scenes with seed "
             f"{manifest['seed']} and code sha256 {manifest['code_sha256'][:16]}. It holds {total_images} images: "
             f"{images['train']} train, {images['val']} validation and {images['test']} held-out test.", '',
             '## What an instance is', '',
             'An instance is one building roof outline in the pixel frame of its scene image. It is stored as a COCO polygon '
             '(holes under `holes`) and as an index in the uint16 label PNG, where 0 is background and 65535 is ignore. Every '
             'instance records `provenance`, `tier`, `weight`, `boundary_reference`, `source` and, where one exists, the eyes '
             'registry `ref_id`.', '',
             '## Where the labels come from', '',
             f"{_share(total_pseudo, total_pseudo + total_humans)} instances are SAM3 pseudo-labels (`machine_pseudo`, weight "
             f"{PROVENANCE_WEIGHTS['machine_pseudo']:g}): the detector outlines saved in latest.json. They are model outputs, not "
             'labels, and their corners are already rounded, so a model trained on them alone learns to round corners. '
             f"{manifest['n_regularised']} of them also carry `segmentation_regularised`: the outline snapped to its dominant "
             'axis pair with square corners, kept only where it stays within IoU 0.85 and 1.2 m Hausdorff of the SAM3 outline. '
             'It is a machine prior (roofs are mostly rectilinear), not a label, and it is the `--target regularised` option.',
             f"The other {total_humans} are human outline placements ({manifest['n_human_references_distinct']} distinct outlines): {sum(counts[s].get('human_T1', 0) for s in SPLITS)} confirmed with "
             f"reviewed geometry (`human_T1`, weight {PROVENANCE_WEIGHTS['human_T1']:g}), "
             f"{sum(counts[s].get('human_T2', 0) for s in SPLITS)} human edits (`human_T2`, weight "
             f"{PROVENANCE_WEIGHTS['human_T2']:g}: needs-adjustment outlines a person moved, and eyes T2 rows such as a "
             f"reviewer CRM outline, which counts once per capture it is placed in) and {sum(counts[s].get('human_T2_lines_only', 0) for s in SPLITS)} with edited "
             f"roof lines but an unchanged outline (`human_T2_lines_only`, weight {PROVENANCE_WEIGHTS['human_T2_lines_only']:g}; "
             'never a boundary reference because the outline is SAM3\'s).',
             f"Of the human outlines, {_share(humans['test'], total_humans)} are in the held-out test split, because every tile "
             'holding a human reference is test. The training signal is therefore SAM3 pseudo-labels only; the human weights '
             'take effect only when future human outlines land on train tiles.',
             f"{manifest['n_boundary_references']} test outline placements are boundary references, coming from "
             f"{manifest['n_boundary_references_distinct']} distinct human outlines (a WGS84-only reference is placed in every "
             f"capture that holds it); {manifest['n_boundary_references_same_image']} placements were drawn over the same image "
             'the model sees. The rest were drawn over another capture and carry registration error, so they are reported apart.',
             f"{manifest.get('n_human_with_model_pair', 0)} human outlines carry the model estimate the reviewer edited from "
             f"(`segmentation_model`, `holes_model`, `model_iou`, `model_hausdorff_m`, `model_vertex_delta`, `holes_changed`) "
             f"and an `edit_kind` (outline, holes_only, manual, none); {manifest.get('n_outline_edited_with_model_pair', 0)} of "
             f"those pairs have a moved outline. Over the edited pairs the median agreement is IoU "
             f"{_fmt(manifest.get('model_iou_median'), 3)} and Hausdorff {_fmt(manifest.get('model_hausdorff_m_median'), 2)} m: "
             'how far the reviewer moved the outline, agreement between the model estimate and the reviewer\'s outline, '
             'not accuracy.',
             '',
             '## What was left out', '',
             f"{excl.get('machine_provenance', 0)} review objects with machine provenance (provenance.source machine:*) were excluded; "
             'machine labels never count as human decisions and their pixels are ignore, not background. '
             f"{excl.get('needs_adjustment_unedited', 0)} needs-adjustment outlines nobody moved, "
             f"{excl.get('review_superseded', 0)} superseded objects and {excl.get('review_draft_unapproved', 0)} unapproved "
             'review drafts are ignore as well. '
             f"{excl.get('review_rejected', 0)} rejected objects stay background. "
             f"{excl.get('non_sam3_source', 0)} roof-pipeline seeds were not used as targets (ignore where no kept outline "
             f"covers half of them), {excl.get('too_small', 0)} slivers under {manifest['min_area_m2']:g} m2 are ignore, "
             f"{excl.get('pseudo_replaced_by_human', 0)} pseudo-labels were replaced by the human outline of the same roof, and "
             f"{excl.get('test_tile_overlap', 0)} train or validation instances touching a test tile were dropped with their "
             'pixels set to ignore. '
             f"On window scenes (latest.json window_frame; lot-*, win-*) {excl.get('frame_cut_human', 0)} human outlines and "
             f"{excl.get('frame_cut', 0)} pseudo-labels lie within {FRAME_CUT_EDGE_PX:g} px of the image edge or outside it "
             '(frame_cut): the straight edge is the window frame, not the roof, so they are ignore, never labels or background.', '',
             '## Split', '', manifest['split_policy'] + '. ' +
             f"{len(manifest['reference_groups'])} z17 tiles are held out. The validation split holds no human outline, so its "
             'boundary score measures agreement with SAM3 pseudo-labels, not with people.', '']
    for split in SPLITS:
        c = counts[split]
        extra = c.get('_evaluation_only', 0)
        lines.append(f"- {split}: {c.get('_images', 0)} images, {c.get('_instances', 0)} training-target instances "
                     f"({pseudo[split]} pseudo, {humans[split] - extra} human)"
                     + (f", plus {extra} evaluation-only human placements drawn over another image" if extra else '')
                     + f"; {c.get('_images_with_human_reference', 0)} images hold a human outline.")
    lines += ['', '## Ignore pixels by reason', '']
    lines += [f'- {k}: {v} polygons' for k, v in manifest['ignore_reasons'].items()] or ['- none']
    lines += ['', '## All exclusion counters', '']
    lines += [f'- {k}: {v}' for k, v in manifest['exclusions'].items()] or ['- none']
    lines += ['', 'No number here is an accuracy. Boundary statements may be made only against the human references, and '
              'those all sit in the test split.']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--out', default=str(common.DEFAULT_OUT / 'dataset-v1'))
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED)
    parser.add_argument('--val-fraction', type=float, default=DEFAULT_VAL_FRACTION)
    parser.add_argument('--scene-ids', nargs='*', help='Restrict to these scene ids (validation runs).')
    parser.add_argument('--limit', type=int, help='Only the first N scene ids (sorted).')
    parser.add_argument('--min-area-m2', type=float, default=4.)
    parser.add_argument('--tarball', action='store_true', help='Also write <out>.tar (for the GPU box) and its sha256.')
    args = parser.parse_args(argv)
    manifest = export(args.root, args.out, seed=args.seed, val_fraction=args.val_fraction, scene_ids=args.scene_ids,
                      limit=args.limit, min_area_m2=args.min_area_m2)
    summary = {k: v for k, v in manifest.items() if k not in ('images', 'reference_groups')}
    if args.tarball:
        summary['tarball'] = common.write_tarball(Path(args.out))
    json.dump(summary, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
