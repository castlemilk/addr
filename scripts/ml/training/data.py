"""torch Dataset over an exported dataset folder (COCO JSON + images + uint16 label PNGs).

Sampling is a pure function of (seed, step): StepBatchSampler draws the image indices and a per-sample seed for
each step, and the dataset derives crop offset, flips, rotation and colour jitter from that seed. A resumed run
therefore replays exactly the batches the interrupted run would have seen, with any number of workers.

IGNORE pixels (65535 in the label PNG) are painted with the image mean before the image reaches the model:
torchvision's Mask R-CNN has no ignore support, and a flat grey patch teaches nothing about roofs either way.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from .common import IGNORE, fill_rings

MIN_INSTANCE_PX = 12   # ~0.7 m2 at 0.236 m/px; smaller crop remnants are not worth a box


def load_split(root, split, max_images=None, require_targets=False):
    """(images, annotations_by_image_id) for a split, training targets only (evaluation-only rows dropped)."""
    coco = json.loads((Path(root) / f'{split}.json').read_text())
    by_image = {}
    for annotation in coco['annotations']:
        if annotation.get('evaluation_only') or annotation.get('label_index') is None:
            continue
        by_image.setdefault(annotation['image_id'], []).append(annotation)
    images = sorted(coco['images'], key=lambda i: i['scene_id'])
    if require_targets:
        images = [i for i in images if by_image.get(i['id'])]
    if max_images:
        images = images[:max_images]
    return images, by_image


def rasterise_annotation(annotation, offset, size, target='raw'):
    """Instance mask (bool, size[1] x size[0]) of one annotation shifted by -offset; holes cleared; pixel centres."""
    width, height = size
    ring = annotation.get('segmentation_regularised') if target == 'regularised' else None
    exterior = np.asarray((ring or annotation['segmentation'])[0], dtype=float).reshape(-1, 2) - offset
    holes = [] if ring else [np.asarray(h, dtype=float).reshape(-1, 2) - offset for h in annotation.get('holes') or []]
    return fill_rings([exterior, *holes], width, height)


def _augment(arrays, rng):
    """Same flips / quarter turns for every array (HxW or HxWxC); rotation keeps rectilinear roofs rectilinear."""
    k = int(rng.integers(4))
    flip_h, flip_v = bool(rng.integers(2)), bool(rng.integers(2))
    out = []
    for array in arrays:
        if flip_h:
            array = array[:, ::-1]
        if flip_v:
            array = array[::-1]
        out.append(np.ascontiguousarray(np.rot90(array, k, axes=(0, 1))))
    return out


class RoofDataset(torch.utils.data.Dataset):
    """Items are (image_index, sample_seed) pairs from StepBatchSampler (train) or plain indices (eval)."""

    def __init__(self, root, split, *, crop=None, train=False, target='raw', max_images=None, colour_jitter=.1):
        self.root, self.crop, self.train, self.target = Path(root), crop, train, target
        self.colour_jitter = colour_jitter
        self.images, self.annotations = load_split(root, split, max_images, require_targets=train)

    def __len__(self):
        return len(self.images)

    def image_weights(self):
        """Sampling weight per image: its largest instance weight (human outlines pull their image up)."""
        return np.array([max([a['weight'] for a in self.annotations.get(i['id'], [])] or [1.]) for i in self.images])

    def load(self, index):
        meta = self.images[index]
        image = np.asarray(Image.open(self.root / meta['file_name']).convert('RGB'))
        label = np.asarray(Image.open(self.root / meta['label_file']))
        return meta, image, label

    def __getitem__(self, item):
        index, seed = item if isinstance(item, tuple) else (item, 0)
        rng = np.random.default_rng(seed)
        meta, image, label = self.load(index)
        height, width = label.shape
        x0 = y0 = 0
        crop_w, crop_h = width, height
        if self.crop and self.train:
            crop_w, crop_h = min(self.crop, width), min(self.crop, height)
            x0 = int(rng.integers(0, width - crop_w + 1))
            y0 = int(rng.integers(0, height - crop_h + 1))
        image = image[y0:y0 + crop_h, x0:x0 + crop_w].astype(np.float32)
        ignore = label[y0:y0 + crop_h, x0:x0 + crop_w] == IGNORE
        annotations = self.annotations.get(meta['id'], [])
        masks, weights = [], []
        for annotation in annotations:
            bx, by, bw, bh = annotation['bbox']
            if bx > x0 + crop_w or by > y0 + crop_h or bx + bw < x0 or by + bh < y0:
                continue
            mask = rasterise_annotation(annotation, (x0, y0), (crop_w, crop_h), self.target) & ~ignore
            if mask.sum() >= MIN_INSTANCE_PX:
                masks.append(mask)
                weights.append(float(annotation['weight']))
        if self.train:
            arrays = _augment([image, ignore, *masks], rng)
            image, ignore, masks = arrays[0], arrays[1], arrays[2:]
            if self.colour_jitter:
                gain = 1. + rng.uniform(-self.colour_jitter, self.colour_jitter)
                bias = rng.uniform(-self.colour_jitter, self.colour_jitter) * 255. * .5
                image = np.clip(image * gain + bias, 0., 255.)
        if ignore.any():
            image = image.copy()
            image[ignore] = image[~ignore].mean(axis=0) if (~ignore).any() else 127.
        tensor = torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1)) / 255.).float()
        h, w = tensor.shape[-2:]
        if masks:
            stack = np.stack(masks)
            boxes = []
            for mask in stack:
                ys, xs = np.nonzero(mask)
                boxes.append([xs.min(), ys.min(), xs.max() + 1, ys.max() + 1])
            target = {'boxes': torch.tensor(boxes, dtype=torch.float32), 'labels': torch.ones(len(masks), dtype=torch.int64),
                      'masks': torch.from_numpy(stack.astype(np.uint8)), 'weights': torch.tensor(weights, dtype=torch.float32)}
        else:
            target = {'boxes': torch.zeros((0, 4), dtype=torch.float32), 'labels': torch.zeros((0,), dtype=torch.int64),
                      'masks': torch.zeros((0, h, w), dtype=torch.uint8), 'weights': torch.zeros((0,), dtype=torch.float32)}
        target['image_index'] = torch.tensor(index)
        return tensor, target


class StepBatchSampler(torch.utils.data.Sampler):
    """Batches for steps [start_step, max_steps): indices ~ weights, each with its own sample seed."""

    def __init__(self, weights, batch_size, seed, start_step, max_steps):
        weights = np.asarray(weights, dtype=float)
        self.p = weights / weights.sum()
        self.batch_size, self.seed = batch_size, seed
        self.start_step, self.max_steps = start_step, max_steps

    def __len__(self):
        return max(0, self.max_steps - self.start_step)

    def __iter__(self):
        for step in range(self.start_step, self.max_steps):
            rng = np.random.default_rng([self.seed, step])
            picks = rng.choice(len(self.p), size=self.batch_size, replace=True, p=self.p)
            yield [(int(i), int(rng.integers(2 ** 31))) for i in picks]


def collate(batch):
    return tuple(zip(*batch, strict=True))


def seed_worker(worker_id):
    # Samples carry their own seeds; this only pins libraries that draw from global state.
    seed = torch.initial_seed() % 2 ** 32
    np.random.seed(seed)  # noqa: NPY002
