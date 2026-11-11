"""Turn the repo's outline datasets (dataset-v1 format) into SAM3 fine-tuning COCO folders (sam3-mac-coco-v2).

SAM3's trainer (sam3/train/data/sam3_image_dataset.py) reads a COCO json per split whose category names are the text
prompts. Ours use the category name 'building_roof'; the production prompt is 'building roof', so the category is
renamed and every other field SAM3 does not know is dropped from annotations (kept in a sidecar for traceability).
Polygons stay polygons (SAM3 converts them to RLE with pycocotools); 'ignore' pixels of our label PNGs have no
equivalent in SAM3's instance loss and are recorded in the manifest as a known difference.

Sources are merged in the order given; image ids and annotation ids are re-numbered; file_name points at the
original image (no copies) unless --copy is passed. Split names map 1:1 (train/val/test).

Round 4 addition: --min-area-px-for NAME=PX (repeatable) sets the area floor for one source dataset (NAME = its folder
name as it appears in per_source), so a combined fold can put the 12 m2 teacher floor on the teacher sets while the
product-scale dataset-v2 pseudo labels keep the default floor; manifest.options.min_area_px_for and
manifest.floor_px_per_source record what applied to every source.

Round 3 additions: each image's annotations are sorted by area (largest first) BEFORE the per-image cap, so "the cap
keeps the N largest" no longer depends on the export order; the manifest carries per-image density (annotations per
hectare from pixel_size_m and size) and median area, per-source provenance counts, the number of annotations flipped
to background by every drop rule, the count of train images still over the cap (must be 0 before training: the fork's
FilterFindQueriesWithTooManyOut silently drops images with more than 200 objects), and, with --judge-dataset, the
minimum ground distance from every train image to the judged test/val scenes of those datasets (< 300 m flagged).

  scripts/ml/training/.venv-sam3/bin/python -m scripts.ml.training.sam3_mac.prepare_coco \
      --dataset data/reviews/eyes/training/dataset-v1 --dataset data/reviews/eyes/training/distill-v1 \
      --max-ann-per-image 200 --judge-dataset data/reviews/eyes/training/dataset-v2 \
      --out data/reviews/eyes/training/sam3-mac/coco-v1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from collections import Counter, defaultdict
from datetime import UTC
from pathlib import Path

VERSION = 'sam3-mac-coco-v2'
PROMPT = 'building roof'
SPLITS = ('train', 'val', 'test')
KEEP_ANN = ('id', 'image_id', 'category_id', 'bbox', 'area', 'iscrowd', 'segmentation')
KEEP_IMG = ('id', 'file_name', 'width', 'height')
FORK_MAX_OBJECTS = 200          # finetune_mps.build_transforms max_objects: images above it are dropped by the loader
NEAR_M = 300.                   # a train image closer than this to a judged scene is flagged
JUDGE_SPLITS = ('test', 'val')  # every image of these splits in a --judge-dataset counts as judged
SCENES = Path('data/scenes')
REPO = Path(__file__).resolve().parents[4]


def now_iso():
    from datetime import datetime
    return datetime.now(UTC).isoformat(timespec='seconds')


def load_split(dataset, split):
    path = Path(dataset) / f'{split}.json'
    if not path.exists():
        return None
    with open(path) as handle:
        return json.load(handle)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def image_hectares(image):
    px = image.get('pixel_size_m')
    if not px:
        return None
    return image['width'] * px * image['height'] * px / 10000.


def area_m2(ann, image):
    if ann.get('area_m2') is not None:
        return float(ann['area_m2'])
    px = image.get('pixel_size_m')
    return float(ann.get('area', 0.)) * px * px if px else None


# --- ground distance between train images and judged scenes -----------------------------------------------------------

def bounds_from_centre(centre_lonlat, half_m):
    lon, lat = centre_lonlat
    dlat = half_m / 111320.
    dlon = half_m / (111320. * max(math.cos(math.radians(lat)), 1e-6))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def scene_bounds(scene_id, scenes=SCENES):
    """region.bounds (w, s, e, n) of a scene's latest.json, or None (17+ roof_training_250 scenes carry none)."""
    path = Path(scenes) / str(scene_id) / 'latest.json'
    if not scene_id or not path.exists():
        return None
    with open(path) as handle:
        scene = json.load(handle)
    bounds = (scene.get('region') or {}).get('bounds')
    return tuple(float(v) for v in bounds) if bounds and len(bounds) == 4 else None


def image_bounds(image, scenes=SCENES):
    """Lon/lat bounds of a dataset image: its own centre_lonlat and ground size when present (teacher windows), else the
    scene's region.bounds; None when neither exists."""
    if image.get('centre_lonlat') and image.get('pixel_size_m'):
        half = max(image['width'], image['height']) * image['pixel_size_m'] / 2.
        return bounds_from_centre(image['centre_lonlat'], half)
    return scene_bounds(image.get('scene_id'), scenes)


def bounds_distance_m(a, b):
    """Ground distance (metres) between two lon/lat boxes; 0 when they overlap."""
    lat = (a[1] + a[3] + b[1] + b[3]) / 4.
    kx = 111320. * math.cos(math.radians(lat))
    ky = 111320.
    dx = max(0., max(a[0], b[0]) - min(a[2], b[2])) * kx
    dy = max(0., max(a[1], b[1]) - min(a[3], b[3])) * ky
    return math.hypot(dx, dy)


def judged_scenes(judge_datasets, splits=JUDGE_SPLITS, scenes=SCENES):
    """{scene_id: bounds or None} over every image of the judged splits of the given datasets."""
    out = {}
    for dataset in judge_datasets or ():
        for split in splits:
            coco = load_split(dataset, split)
            if coco is None:
                continue
            for image in coco['images']:
                sid = image.get('scene_id')
                if sid and sid not in out:
                    out[sid] = scene_bounds(sid, scenes)
    return out


def distance_check(train_images, judged, near_m=NEAR_M, scenes=SCENES):
    """Minimum ground distance from each train image to the judged scenes with bounds; flags < near_m."""
    with_bounds = {sid: b for sid, b in judged.items() if b}
    flagged, unchecked_train, per_image = [], 0, {}
    for image in train_images:
        bounds = image_bounds(image, scenes)
        if bounds is None:
            unchecked_train += 1
            continue
        best, best_scene = None, None
        for sid, jb in with_bounds.items():
            d = bounds_distance_m(bounds, jb)
            if best is None or d < best:
                best, best_scene = d, sid
        per_image[image['id']] = None if best is None else round(best, 1)
        if best is not None and best < near_m:
            flagged.append({'image_id': image['id'], 'source_dataset': image.get('source_dataset'), 'scene_id': image.get('scene_id'),
                            'judged_scene': best_scene, 'distance_m': round(best, 1)})
    return {'near_m': near_m, 'judged_scenes': len(judged), 'judged_scenes_without_bounds': sum(1 for b in judged.values() if not b),
            'train_images_checked': len(train_images) - unchecked_train, 'train_images_unchecked': unchecked_train,
            'train_images_flagged': len(flagged), 'flagged': flagged, 'min_distance_m_per_image': per_image}


# --- conversion --------------------------------------------------------------------------------------------------------

def parse_floor_spec(specs):
    """['name=216', ...] -> {'name': 216}; malformed or repeated names raise ValueError."""
    out = {}
    for spec in specs or ():
        name, sep, px = str(spec).rpartition('=')
        if not sep or not name or not px.strip().isdigit():
            raise ValueError(f'--min-area-px-for expects NAME=PX (integer pixels), got {spec!r}')
        if name in out:
            raise ValueError(f'--min-area-px-for given twice for {name!r}')
        out[name] = int(px)
    return out


def convert(datasets, out, copy_images=False, min_area_px=16, drop_weight_below=0., max_ann_per_image=None, judge_datasets=None,
            scenes=SCENES, per_dataset_floor=None):
    per_dataset_floor = {str(k): int(v) for k, v in (per_dataset_floor or {}).items()}
    names = [Path(d).name for d in datasets]
    unknown = sorted(set(per_dataset_floor) - set(names))
    if unknown:
        raise ValueError(f'per-dataset floor names not among the datasets: {unknown} (datasets: {names})')
    floor_per_source = {name: per_dataset_floor.get(name, min_area_px) for name in names}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {'version': VERSION, 'created_at': now_iso(), 'prompt': PROMPT, 'sources': [str(d) for d in datasets],
                'options': {'copy_images': copy_images, 'min_area_px': min_area_px, 'min_area_px_for': per_dataset_floor,
                            'drop_weight_below': drop_weight_below,
                            'max_ann_per_image': max_ann_per_image, 'cap_order': 'area descending (largest kept)',
                            'judge_datasets': [str(d) for d in judge_datasets or ()]},
                'floor_px_per_source': floor_per_source,
                'splits': {}, 'known_differences': [
                    'ignore pixels (65535 in the label PNGs) are not representable in SAM3 instance training and are dropped',
                    'per-annotation weights are not used by SAM3 loss; annotations with weight below the floor are dropped',
                    'iscrowd annotations are filtered by SAM3 (FilterCrowds transform) and kept here unchanged',
                    'interior rings (holes) are not carried: only the exterior polygon is written, so SAM3 targets fill courtyards',
                    'annotations dropped for weight, size or the per-image cap become background for SAM3 (hard negatives), '
                    'including real roofs: see flipped_to_background per split and per source',
                    f'the fork drops any train image with more than {FORK_MAX_OBJECTS} objects (FilterFindQueriesWithTooManyOut), '
                    'so images_over_fork_max must be 0 before training',
                    'the cap keeps the largest annotations of an image (sorted by area before capping), independent of export order',
                    'the area floor is per source when --min-area-px-for names it (floor_px_per_source); every other source uses '
                    'options.min_area_px, so an annotation under one floor is background on that source only']}
    source_manifest_shas = {}
    for dataset in datasets:
        path = Path(dataset) / 'manifest.json'
        source_manifest_shas[Path(dataset).name] = sha256_file(path) if path.exists() else None
    manifest['source_manifest_sha256'] = source_manifest_shas
    for split in SPLITS:
        images, annotations, sidecar = [], [], []
        counts = Counter()
        per_source = {}
        per_image_manifest = []
        next_image, next_ann = 1, 1
        for dataset in datasets:
            name = Path(dataset).name
            src_counts = per_source.setdefault(name, Counter())
            coco = load_split(dataset, split)
            if coco is None:
                counts[f'missing:{name}'] += 1
                continue
            floor = floor_per_source[name]
            image_ids, image_rows = {}, {}
            for image in coco['images']:
                src = Path(dataset) / image['file_name']
                if not src.exists():
                    counts['image_missing'] += 1
                    src_counts['image_missing'] += 1
                    continue
                if copy_images:
                    dest = out / split / 'images' / f"{name}--{Path(image['file_name']).name}"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        shutil.copyfile(src, dest)
                    file_name = str(dest.relative_to(out / split))
                else:
                    file_name = str(src.resolve())
                image_ids[image['id']] = next_image
                row = {'id': next_image, 'file_name': file_name, 'width': image['width'], 'height': image['height'],
                       'source_dataset': name, 'source_image_id': image['id'],
                       'pixel_size_m': image.get('pixel_size_m'), 'scene_id': image.get('scene_id')}
                if image.get('centre_lonlat'):
                    row['centre_lonlat'] = image['centre_lonlat']
                if image.get('tile_group'):
                    row['tile_group'] = image['tile_group']
                images.append(row)
                image_rows[next_image] = (image, row)
                src_counts['images'] += 1
                next_image += 1
            # first pass: every filter that does not depend on the other annotations of the image
            kept_by_image = defaultdict(list)
            for ann in coco['annotations']:
                if ann['image_id'] not in image_ids:
                    counts['ann_orphan'] += 1
                    src_counts['ann_orphan'] += 1
                    continue
                if ann.get('weight', 1.) < drop_weight_below:
                    counts['ann_low_weight'] += 1
                    src_counts['ann_low_weight'] += 1
                    continue
                if ann.get('area', 0) < floor:
                    counts['ann_tiny'] += 1
                    src_counts['ann_tiny'] += 1
                    continue
                seg = ann.get('segmentation')
                if not seg or (isinstance(seg, list) and (not seg[0] or len(seg[0]) < 6)):
                    counts['ann_no_segmentation'] += 1
                    src_counts['ann_no_segmentation'] += 1
                    continue
                kept_by_image[image_ids[ann['image_id']]].append(ann)
            # second pass: sort by area (largest first) then cap; the dropped ones become background
            for new_image_id in sorted(kept_by_image):
                anns = sorted(kept_by_image[new_image_id], key=lambda a: (-float(a.get('area', 0.)), a['id']))
                image, row = image_rows[new_image_id]
                if max_ann_per_image and len(anns) > max_ann_per_image:
                    over = len(anns) - max_ann_per_image
                    counts['ann_over_cap'] += over
                    src_counts['ann_over_cap'] += over
                    counts['images_capped'] += 1
                    src_counts['images_capped'] += 1
                    anns = anns[:max_ann_per_image]
                areas = []
                for ann in anns:
                    coco_row = {k: ann[k] for k in KEEP_ANN if k in ann}
                    coco_row['id'], coco_row['image_id'], coco_row['category_id'] = next_ann, new_image_id, 1
                    coco_row.setdefault('iscrowd', 0)
                    annotations.append(coco_row)
                    sidecar.append({'id': next_ann, 'source_dataset': name, 'source_ann_id': ann['id'],
                                    'provenance': ann.get('provenance'), 'tier': ann.get('tier'), 'weight': ann.get('weight'),
                                    'area_m2': ann.get('area_m2'), 'object_id': ann.get('object_id'), 'scene_id': ann.get('scene_id'),
                                    'label_variant': ann.get('label_variant'), 'merged_from': ann.get('merged_from')})
                    counts[f"prov:{ann.get('provenance', '?')}"] += 1
                    src_counts[f"prov:{ann.get('provenance', '?')}"] += 1
                    m2 = area_m2(ann, image)
                    if m2 is not None:
                        areas.append(m2)
                    next_ann += 1
                src_counts['annotations'] += len(anns)
                ha = image_hectares(row)
                areas.sort()
                per_image_manifest.append({'id': new_image_id, 'source_dataset': name, 'scene_id': row.get('scene_id'),
                                           'annotations': len(anns), 'hectares': round(ha, 4) if ha else None,
                                           'density_per_ha': round(len(anns) / ha, 2) if ha else None,
                                           'median_area_m2': round(areas[len(areas) // 2], 1) if areas else None})
            for new_image_id, (_image, row) in image_rows.items():
                if new_image_id not in kept_by_image:
                    ha = image_hectares(row)
                    per_image_manifest.append({'id': new_image_id, 'source_dataset': name, 'scene_id': row.get('scene_id'),
                                               'annotations': 0, 'hectares': round(ha, 4) if ha else None, 'density_per_ha': 0. if ha else None,
                                               'median_area_m2': None})
        payload = {'info': {'version': VERSION, 'split': split, 'created_at': manifest['created_at'], 'prompt': PROMPT},
                   'images': images, 'annotations': annotations,
                   'categories': [{'id': 1, 'name': PROMPT, 'supercategory': 'building'}]}
        folder = out / split
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / '_annotations.coco.json', 'w') as handle:
            json.dump(payload, handle)
        with open(folder / 'sidecar.json', 'w') as handle:
            json.dump(sidecar, handle)
        per_image_counts = Counter(a['image_id'] for a in annotations)
        ordered = sorted(per_image_counts.values())
        densities = sorted(p['density_per_ha'] for p in per_image_manifest if p['density_per_ha'] is not None)
        medians = sorted(p['median_area_m2'] for p in per_image_manifest if p['median_area_m2'] is not None)
        flipped = {k: counts.get(k, 0) for k in ('ann_over_cap', 'ann_tiny', 'ann_low_weight')}
        split_row = {'images': len(images), 'annotations': len(annotations), 'dropped_or_notes': dict(counts),
                     'annotations_per_image_median': ordered[len(ordered) // 2] if ordered else 0,
                     'annotations_per_image_max': ordered[-1] if ordered else 0,
                     'images_over_fork_max': sum(1 for n in per_image_counts.values() if n > FORK_MAX_OBJECTS),
                     'images_at_cap': sum(1 for n in per_image_counts.values() if max_ann_per_image and n >= max_ann_per_image),
                     'flipped_to_background': {**flipped, 'total': sum(flipped.values())},
                     'density_per_ha_median': densities[len(densities) // 2] if densities else None,
                     'median_area_m2_median': medians[len(medians) // 2] if medians else None,
                     'per_source': {name: dict(sorted(c.items())) for name, c in per_source.items()},
                     'per_image': per_image_manifest}
        if split == 'train' and judge_datasets:
            split_row['distance_check'] = distance_check(images, judged_scenes(judge_datasets, scenes=scenes), scenes=scenes)
        manifest['splits'][split] = split_row
    with open(out / 'manifest.json', 'w') as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
    return manifest


def gate(coco_root, max_objects=FORK_MAX_OBJECTS):
    """(ok, detail) for the pre-train gate: no train image above the fork's object limit."""
    with open(Path(coco_root) / 'train' / '_annotations.coco.json') as handle:
        coco = json.load(handle)
    per_image = Counter(a['image_id'] for a in coco['annotations'])
    over = {i: n for i, n in per_image.items() if n > max_objects}
    return not over, {'train_images': len(coco['images']), 'annotations': len(coco['annotations']),
                      'max_annotations_per_image': max(per_image.values()) if per_image else 0, 'images_over': len(over)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dataset', action='append', help='dataset-v1 format folder (repeatable, merged in order)')
    parser.add_argument('--out', help='output folder')
    parser.add_argument('--copy', action='store_true', help='copy images under the output instead of absolute paths')
    parser.add_argument('--min-area-px', type=int, default=16)
    parser.add_argument('--min-area-px-for', action='append', default=None, metavar='NAME=PX',
                        help='area floor for one source dataset by folder name (repeatable); others use --min-area-px')
    parser.add_argument('--drop-weight-below', type=float, default=0.)
    parser.add_argument('--max-ann-per-image', type=int, default=None)
    parser.add_argument('--judge-dataset', action='append', default=None,
                        help='dataset whose test/val scenes are judged; train images within 300 m are flagged in the manifest')
    parser.add_argument('--gate', help='only check an existing fold: exit 1 when a train image exceeds the fork object limit')
    args = parser.parse_args(argv)
    if args.gate:
        ok, detail = gate(args.gate)
        json.dump({'ok': ok, **detail}, sys.stdout)
        sys.stdout.write('\n')
        return 0 if ok else 1
    if not args.dataset or not args.out:
        parser.error('--dataset and --out are required unless --gate is given')
    try:
        per_dataset_floor = parse_floor_spec(args.min_area_px_for)
        manifest = convert(args.dataset, args.out, args.copy, args.min_area_px, args.drop_weight_below, args.max_ann_per_image,
                           args.judge_dataset, per_dataset_floor=per_dataset_floor)
    except ValueError as error:
        parser.error(str(error))
    brief = {k: v for k, v in manifest.items() if k != 'splits'}
    brief['splits'] = {s: {k: v for k, v in row.items() if k not in ('per_image',) and not (k == 'distance_check')}
                       for s, row in manifest['splits'].items()}
    for s, row in manifest['splits'].items():
        if 'distance_check' in row:
            brief['splits'][s]['distance_check'] = {k: v for k, v in row['distance_check'].items() if k not in ('flagged', 'min_distance_m_per_image')}
    json.dump(brief, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
