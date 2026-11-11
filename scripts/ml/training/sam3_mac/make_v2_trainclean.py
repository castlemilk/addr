"""Student-domain fold input from dataset-v2's train split (sam3-mac-v2-trainclean-v1).

Keeps the dataset-v2 TRAIN images whose saved tile carries no ignored test tile (test_tiles_ignored == 0) and whose
ignore-pixel share is below --max-ignore-share (default 1 percent), and writes beside dataset-v2:

  <out>/train.json      the kept images and their annotations (all production SAM3 pseudo labels, provenance machine_pseudo)
  <out>/images          a symlink to dataset-v2/images (no copies)
  <out>/manifest.json   counts kept / excluded by reason and the disjointness check against dataset-v1 test and dataset-v2
                        test on tile_group, scene_id and image sha256 (all three must be 0)

This is a fold input only, not a dataset-v1 kit: no val or test split, no labels folder, no references.json, so
prepare_coco copies no human reference into a fold that lists it. The pseudo labels are production SAM3 outputs on
Google tiles (rounded corners; segmentation_regularised is present on many rows and is NOT used); they teach tone and
density of the student domain, not geometry.

  scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.make_v2_trainclean \
      --dataset data/reviews/eyes/training/dataset-v2 --out data/reviews/eyes/training/dataset-v2-trainclean
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import UTC
from pathlib import Path

VERSION = 'sam3-mac-v2-trainclean-v1'
STATEMENT = ('fold input only, not a dataset-v1 kit; pseudo labels are production SAM3 outputs (rounded corners; '
             'segmentation_regularised not used)')


def now_iso():
    from datetime import datetime
    return datetime.now(UTC).isoformat(timespec='seconds')


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def load(path):
    with open(path) as handle:
        return json.load(handle)


def select(train, max_ignore_share=.01):
    """(kept images, excluded {reason: count}, excluded ids) by the two rules."""
    kept, reasons, excluded = [], Counter(), []
    for image in train['images']:
        share = (image.get('ignore_pixels') or 0) / float(image['width'] * image['height'])
        if image.get('test_tiles_ignored', 0) != 0:
            reasons['test_tiles_ignored'] += 1
            excluded.append({'id': image['id'], 'scene_id': image.get('scene_id'), 'reason': 'test_tiles_ignored',
                             'test_tiles_ignored': image.get('test_tiles_ignored')})
        elif share >= max_ignore_share:
            reasons['ignore_share'] += 1
            excluded.append({'id': image['id'], 'scene_id': image.get('scene_id'), 'reason': 'ignore_share', 'ignore_share': round(share, 4)})
        else:
            kept.append(image)
    return kept, dict(reasons), excluded


def disjointness(kept, other_images):
    """Overlap counts of tile_group, scene_id and sha256 between the kept images and another split's images."""
    keys = {}
    for field in ('tile_group', 'scene_id', 'sha256'):
        ours = {i.get(field) for i in kept if i.get(field)}
        theirs = {i.get(field) for i in other_images if i.get(field)}
        keys[field] = sorted(ours & theirs)
    return {field: {'overlap': len(v), 'examples': v[:5]} for field, v in keys.items()}


def build(dataset, out, max_ignore_share=.01, other_tests=()):
    dataset, out = Path(dataset), Path(out)
    train = load(dataset / 'train.json')
    kept, reasons, excluded = select(train, max_ignore_share)
    kept_ids = {i['id'] for i in kept}
    annotations = [a for a in train['annotations'] if a['image_id'] in kept_ids]
    provenance = Counter(a.get('provenance') for a in annotations)
    if set(provenance) - {'machine_pseudo'}:
        raise SystemExit(f'dataset-v2 train is expected to hold machine_pseudo labels only, found {dict(provenance)}')
    out.mkdir(parents=True, exist_ok=True)
    link = out / 'images'
    target = os.path.relpath((dataset / 'images').resolve(), out.resolve())
    if link.is_symlink() or link.exists():
        if link.is_symlink():
            link.unlink()
        else:
            raise SystemExit(f'{link} exists and is not a symlink; refusing to replace it')
    os.symlink(target, link)
    payload = {'info': {**(train.get('info') or {}), 'version': VERSION, 'split': 'train', 'created_at': now_iso(),
                        'derived_from': str(dataset), 'statement': STATEMENT},
               'images': kept, 'annotations': annotations, 'categories': train['categories']}
    with open(out / 'train.json', 'w') as handle:
        json.dump(payload, handle)
    checks = {}
    for name, path in [('dataset-v2/test', dataset / 'test.json'), ('dataset-v2/val', dataset / 'val.json')] + \
            [(f'{Path(p).name}/test', Path(p) / 'test.json') for p in other_tests]:
        if path.exists():
            checks[name] = disjointness(kept, load(path)['images'])
    must_be_zero = {k: v for k, v in checks.items() if k.endswith('/test')}
    overlap_total = sum(f['overlap'] for c in must_be_zero.values() for f in c.values())
    ha = [i['width'] * i['height'] * (i.get('pixel_size_m') or 0) ** 2 / 10000. for i in kept]
    per_image = Counter(a['image_id'] for a in annotations)
    density = sorted(per_image[i['id']] / h for i, h in zip(kept, ha, strict=True) if h)
    areas = sorted(float(a['area_m2']) for a in annotations if a.get('area_m2') is not None)
    manifest = {'version': VERSION, 'created_at': now_iso(), 'source_dataset': str(dataset),
                'source_manifest_sha256': sha256_file(dataset / 'manifest.json') if (dataset / 'manifest.json').exists() else None,
                'source_train_sha256': sha256_file(dataset / 'train.json'), 'statement': STATEMENT,
                'rules': {'test_tiles_ignored': 0, 'max_ignore_share': max_ignore_share},
                'counts': {'source_images': len(train['images']), 'kept_images': len(kept), 'excluded': reasons,
                           'kept_annotations': len(annotations), 'provenance': dict(provenance),
                           'sizes': dict(Counter(f"{i['width']}x{i['height']}" for i in kept)),
                           'scene_prefixes': dict(Counter(str(i.get('scene_id', '')).split('-')[0] for i in kept)),
                           'density_per_ha_median': round(density[len(density) // 2], 2) if density else None,
                           'area_m2_median': round(areas[len(areas) // 2], 1) if areas else None,
                           'segmentation_regularised_present': sum(1 for a in annotations if a.get('segmentation_regularised'))},
                'disjointness': checks, 'disjointness_ok': overlap_total == 0,
                'excluded_images': excluded, 'images_symlink': target,
                'files': {'train.json': sha256_file(out / 'train.json')}}
    with open(out / 'manifest.json', 'w') as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dataset', default='data/reviews/eyes/training/dataset-v2')
    parser.add_argument('--out', default='data/reviews/eyes/training/dataset-v2-trainclean')
    parser.add_argument('--max-ignore-share', type=float, default=.01)
    parser.add_argument('--other-test', action='append', default=['data/reviews/eyes/training/dataset-v1'],
                        help='other datasets whose test split must be disjoint (default dataset-v1)')
    args = parser.parse_args(argv)
    manifest = build(args.dataset, args.out, args.max_ignore_share, args.other_test)
    json.dump({k: v for k, v in manifest.items() if k != 'excluded_images'}, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write('\n')
    return 0 if manifest['disjointness_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
