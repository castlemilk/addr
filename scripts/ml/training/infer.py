"""ONNX inference for the roof outline model with the same proposal interface as Sam3TextSegmenter.

    segmenter = RoofOutlineSegmenter('roof_outline.onnx')
    features = segmenter.encode_image(image)                       # runs the whole network once per image
    proposals = segmenter.proposals(features, 'building roof', image.size, threshold=.45, max_proposals=80)
    # -> list[Sam3Proposal(mask=bool HxW, score=float, box=(x0, y0, x1, y1) normalised to [0, 1] like SAM3)]

The model is class-agnostic (roofs only): prompts outside ROOF_PROMPTS return no proposals, so a caller can put it
in place of SAM3 for the 'building' scan and keep SAM3 for everything else. Nothing here is wired into production.
Needs numpy, Pillow and onnxruntime only (both the main ML venv and the training venv have them).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from ..sam3 import Sam3Proposal

ROOF_PROMPTS = ('building roof', 'building', 'roof', 'house', 'building roof outline')
INPUT_NAME, OUTPUT_NAMES = 'image', ('boxes', 'scores', 'mask_probs')


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def resize_bilinear(array, height, width):
    """torch.nn.functional.interpolate(mode='bilinear', align_corners=False) for one 2-D float array."""
    source_h, source_w = array.shape
    def axis(n_out, n_in):
        coords = np.maximum((np.arange(n_out, dtype=np.float64) + .5) * (n_in / n_out) - .5, 0.)
        low = np.minimum(np.floor(coords).astype(np.int64), n_in - 1)
        high = np.minimum(low + 1, n_in - 1)
        return low, high, (coords - low).astype(np.float32)
    y0, y1, wy = axis(height, source_h)
    x0, x1, wx = axis(width, source_w)
    top = array[y0][:, x0] * (1 - wx) + array[y0][:, x1] * wx
    bottom = array[y1][:, x0] * (1 - wx) + array[y1][:, x1] * wx
    return top * (1 - wy)[:, None] + bottom * wy[:, None]


def paste_mask(mask_prob, box, height, width, threshold=.5):
    """One M x M probability map into an H x W bool mask, as torchvision's paste_masks_in_image does (padding 1)."""
    size = mask_prob.shape[-1]
    padded = np.pad(mask_prob.astype(np.float32), 1)
    scale = (size + 2) / size
    cx, cy = (box[0] + box[2]) * .5, (box[1] + box[3]) * .5
    half_w, half_h = (box[2] - box[0]) * .5 * scale, (box[3] - box[1]) * .5 * scale
    x0, y0, x1, y1 = (int(v) for v in (cx - half_w, cy - half_h, cx + half_w, cy + half_h))  # trunc, like .to(int64)
    w, h = max(x1 - x0 + 1, 1), max(y1 - y0 + 1, 1)
    resized = resize_bilinear(padded, h, w)
    out = np.zeros((height, width), dtype=bool)
    ix0, ix1 = max(x0, 0), min(x1 + 1, width)
    iy0, iy1 = max(y0, 0), min(y1 + 1, height)
    if ix1 > ix0 and iy1 > iy0:
        out[iy0:iy1, ix0:ix1] = resized[iy0 - y0:iy1 - y0, ix0 - x0:ix1 - x0] > threshold
    return out


def paste_masks(mask_probs, boxes, height, width, threshold=.5):
    return [paste_mask(m, b, height, width, threshold) for m, b in zip(mask_probs, boxes, strict=True)]


def image_tensor(image):
    array = np.asarray(image.convert('RGB'), dtype=np.float32) / 255.
    return np.ascontiguousarray(array.transpose(2, 0, 1))


class RoofOutlineSegmenter:
    """Drop-in proposal source for the 'building' scan (mask, score, box), backed by the exported ONNX model."""

    def __init__(self, model_path, providers=None, mask_threshold=.5, prompts=ROOF_PROMPTS):
        import onnxruntime as ort

        self.model_path = Path(model_path)
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(self.model_path), sess_options=options,
                                            providers=providers or ['CPUExecutionProvider'])
        self.mask_threshold, self.prompts = mask_threshold, tuple(prompts)
        sidecar = self.model_path.with_suffix('.json')
        self.meta = json.loads(sidecar.read_text()) if sidecar.exists() else {}
        self.model_identity = {self.model_path.name: _file_sha256(self.model_path), 'mask_threshold': mask_threshold,
                               'model_version': self.meta.get('model_version')}

    def encode_image(self, image):
        boxes, scores, masks = self.session.run(list(OUTPUT_NAMES), {INPUT_NAME: image_tensor(image)})
        return {'boxes': np.asarray(boxes, dtype=np.float32).reshape(-1, 4), 'scores': np.asarray(scores, dtype=np.float32).reshape(-1),
                'mask_probs': np.asarray(masks, dtype=np.float32).reshape(len(scores), *np.shape(masks)[-2:]),
                'size': image.size}

    def proposals(self, features, prompt, image_size, threshold=.4, max_proposals=128):
        if prompt not in self.prompts:
            return []
        width, height = features['size']
        order = np.argsort(-features['scores'], kind='stable')
        found = []
        for index in order:
            score = float(features['scores'][index])
            if score < threshold or len(found) >= max_proposals:
                break
            box = features['boxes'][index]
            mask = paste_mask(features['mask_probs'][index], box, height, width, self.mask_threshold)
            if tuple(image_size) != (width, height):
                mask = np.asarray(Image.fromarray(mask).resize(tuple(image_size), Image.Resampling.NEAREST), dtype=bool)
            found.append(Sam3Proposal(mask=mask, score=score,
                                      box=(float(box[0]) / width, float(box[1]) / height, float(box[2]) / width, float(box[3]) / height)))
        return found


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the exported roof outline ONNX model on one image.')
    parser.add_argument('--model', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--prompt', default='building roof')
    parser.add_argument('--threshold', type=float, default=.45)
    parser.add_argument('--max-proposals', type=int, default=80)
    parser.add_argument('--overlay', help='Write a PNG with proposal outlines.')
    args = parser.parse_args(argv)
    image = Image.open(args.image).convert('RGB')
    segmenter = RoofOutlineSegmenter(args.model)
    features = segmenter.encode_image(image)
    proposals = segmenter.proposals(features, args.prompt, image.size, threshold=args.threshold, max_proposals=args.max_proposals)
    summary = {'image': args.image, 'image_size': list(image.size), 'raw_detections': int(len(features['scores'])),
               'mask_prob_shape': list(features['mask_probs'].shape), 'n_proposals': len(proposals),
               'mask_shapes': sorted({list(p.mask.shape).__repr__() for p in proposals}),
               'mask_dtype': str(proposals[0].mask.dtype) if proposals else None,
               'scores': [round(p.score, 4) for p in proposals[:10]], 'boxes': [list(map(lambda v: round(v, 4), p.box)) for p in proposals[:5]],
               'model_identity': segmenter.model_identity}
    if args.overlay:
        from PIL import ImageDraw

        from ..segmentation import mask_to_polygons
        canvas = image.copy()
        draw = ImageDraw.Draw(canvas)
        for proposal in proposals:
            for polygon in mask_to_polygons(proposal.mask, min_area_px=12., simplify_px=.9):
                draw.line(list(polygon.exterior.coords), fill=(0, 255, 120), width=2)
        canvas.save(args.overlay)
        summary['overlay'] = args.overlay
    json.dump(summary, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
