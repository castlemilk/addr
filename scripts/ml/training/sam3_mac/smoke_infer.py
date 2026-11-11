"""SAM3 PyTorch weights on Apple Silicon (MPS) against the production int8 ONNX export, same images, same prompt.

Production runs SAM3 as an int8 ONNX export (scripts/ml/models/sam3, danilobukvic/sam3-text-onnx). This measures what
the released fp32 checkpoint gives on the same 1152 px tiles: instance counts, one-to-one matches at IoU >= 0.5,
IoU of matched pairs, right-corner share of the simplified outlines, and seconds per image on MPS. Nothing here is
accuracy; it is agreement between two runs of the same model family. Writes a JSON summary and a contact sheet.

  scripts/ml/training/.venv-sam3/bin/python -m scripts.ml.training.sam3_mac.smoke_infer --device mps --n 8
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import UTC
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

VERSION = 'sam3-mac-smoke-v1'
REPO = Path(__file__).resolve().parents[4]
VENDOR = REPO / 'scripts/ml/training/vendor/sam3-apple-silicon'
BPE = VENDOR / 'assets/bpe_simple_vocab_16e6.txt.gz'
WEIGHTS = REPO / 'scripts/ml/models/sam3-hf/sam3.pt'
ONNX_MODELS = REPO / 'scripts/ml/models/sam3'
OUT = REPO / 'data/reviews/eyes/training/sam3-mac/smoke'
PROMPT = 'building roof'
THRESHOLD = .45              # scene_extract.extract_objects / hires_teacher.THRESHOLD
MATCH_IOU = .5
SIMPLIFY_M = .25
CORNER_MIN_TURN, RIGHT_TOL = 30., 10.


def now_iso():
    from datetime import datetime
    return datetime.now(UTC).isoformat(timespec='seconds')


def pick_images(n, seed, root=REPO):
    """Seeded saved Mooroolbark tiles (1152 px) plus up to two distill-v1 native windows when present."""
    sys.path.insert(0, str(root))
    from scripts.ml.training.hires_teacher import saved_tiles
    tiles = saved_tiles(root, count=n, seed=seed)
    rows = [{'id': scene, 'path': str(png), 'px_m': px_m, 'kind': 'saved_tile'} for scene, png, px_m in tiles]
    natives = sorted((root / 'data/imagery/com-2018/windows').glob('*.png'))[:2]
    rows += [{'id': p.stem, 'path': str(p), 'px_m': .1, 'kind': 'com2018_native'} for p in natives]
    return rows


def load_torch_model(weights, device, dtype, trainable=None):
    """fp32 weights always; bf16 is applied as autocast at inference (casting the weights breaks the fp32 processor input).
    `trainable` applies a finetune_mps trainable-*.pt on top of the base checkpoint with a strict key check."""
    sys.path.insert(0, str(VENDOR))
    from sam3.model.sam3_image_processor import Sam3Processor
    from sam3.model_builder import build_sam3_image_model
    started = time.monotonic()
    model = build_sam3_image_model(bpe_path=str(BPE), device=device, eval_mode=True, checkpoint_path=str(weights),
                                   load_from_HF=False, enable_segmentation=True)
    if trainable:
        from .finetune_mps import load_trainable
        load_trainable(model, trainable)
        model.eval()
    processor = Sam3Processor(model, resolution=1008, device=device, confidence_threshold=THRESHOLD)
    return model, processor, round(time.monotonic() - started, 2)


def run_torch(processor, image, prompt, device, dtype='fp32'):
    import torch
    autocast = (torch.autocast(device_type=device.split(':')[0], dtype=torch.bfloat16) if dtype == 'bf16'
                else torch.autocast(device_type='cpu', enabled=False))
    started = time.monotonic()
    with torch.inference_mode(), autocast:
        state = processor.set_image(image)
        if device == 'mps':
            torch.mps.synchronize()
        t_image = time.monotonic() - started
        out = processor.set_text_prompt(prompt=prompt, state=state)
        if device == 'mps':
            torch.mps.synchronize()
    total = time.monotonic() - started
    masks = out['masks'].detach().cpu().numpy()
    if masks.ndim == 4:
        masks = masks[:, 0]
    scores = out['scores'].detach().float().cpu().numpy().tolist()
    return [m.astype(bool) for m in masks], scores, {'set_image_s': round(t_image, 3), 'total_s': round(total, 3)}


def run_onnx(image, prompt, root=REPO):
    """Production path when onnxruntime is importable in this venv; otherwise None (recorded)."""
    try:
        sys.path.insert(0, str(root))
        from scripts.ml.sam3 import Sam3TextSegmenter
    except Exception as error:  # noqa: BLE001 - optional comparison arm
        return None, None, {'error': f'{type(error).__name__}: {error}'}
    try:
        seg = run_onnx.segmenter = getattr(run_onnx, 'segmenter', None) or Sam3TextSegmenter(ONNX_MODELS)
    except Exception as error:  # noqa: BLE001 - optional comparison arm (models missing)
        return None, None, {'error': f'{type(error).__name__}: {error}'}
    # same call pattern as hires_teacher.teach_window: full-size boolean masks, presence-free sigmoid scores
    started = time.monotonic()
    features = seg.encode_image(image)
    proposals = seg.proposals(features, prompt, image.size, threshold=THRESHOLD, max_proposals=200)
    total = time.monotonic() - started
    masks = [np.asarray(p.mask).astype(bool) for p in proposals]
    scores = [float(p.score) for p in proposals]
    return masks, scores, {'total_s': round(total, 3)}


def iou(a, b):
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter / union) if union else 0.


def match(masks_a, masks_b, threshold=MATCH_IOU):
    pairs = []
    areas_a = [int(a.sum()) for a in masks_a]
    areas_b = [int(b.sum()) for b in masks_b]
    for i, a in enumerate(masks_a):
        for j, b in enumerate(masks_b):
            if min(areas_a[i], areas_b[j]) < threshold * max(areas_a[i], areas_b[j]):
                continue                                  # IoU <= min/max area, so this pair cannot reach the threshold
            v = iou(a, b)
            if v >= threshold:
                pairs.append((v, i, j))
    pairs.sort(reverse=True)
    used_a, used_b, matched = set(), set(), []
    for v, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matched.append({'a': i, 'b': j, 'iou': round(v, 4)})
    return matched


def corner_stats(mask, px_m, root=REPO):
    sys.path.insert(0, str(root))
    from scripts.ml.segmentation import mask_to_polygons
    polygons = mask_to_polygons(mask.astype(np.uint8), min_area_px=1., simplify_px=.9)
    if not polygons:
        return None
    poly = max(polygons, key=lambda p: p.area).simplify(SIMPLIFY_M / px_m, preserve_topology=True)
    coords = list(poly.exterior.coords)[:-1]
    corners = right = 0
    n = len(coords)
    for k in range(n):
        (x0, y0), (x1, y1), (x2, y2) = coords[k - 1], coords[k], coords[(k + 1) % n]
        a1, a2 = math.atan2(y1 - y0, x1 - x0), math.atan2(y2 - y1, x2 - x1)
        turn = abs(math.degrees((a2 - a1 + math.pi) % (2 * math.pi) - math.pi))
        if turn >= CORNER_MIN_TURN:
            corners += 1
            if abs(turn - 90.) <= RIGHT_TOL:
                right += 1
    rect = poly.minimum_rotated_rectangle.area
    return {'vertices': n, 'corners': corners, 'right_corners': right, 'fill': round(poly.area / rect, 4) if rect else None,
            'polygon': [(float(x), float(y)) for x, y in coords]}


def draw_sheet(rows, out_path, cell=384):
    n = len(rows)
    cols = min(4, n) or 1
    rows_n = int(math.ceil(n / cols))
    sheet = Image.new('RGB', (cols * cell, rows_n * cell), (20, 20, 20))
    for k, row in enumerate(rows):
        image = Image.open(row['path']).convert('RGB')
        scale = cell / max(image.size)
        thumb = image.resize((int(image.width * scale), int(image.height * scale)))
        draw = ImageDraw.Draw(thumb)
        for arm, colour in (('onnx', (255, 80, 80)), ('torch', (80, 255, 120))):
            for poly in row.get(f'{arm}_polygons', []):
                pts = [(x * scale, y * scale) for x, y in poly]
                if len(pts) >= 3:
                    draw.line(pts + [pts[0]], fill=colour, width=2)
        draw.text((6, 6), f"{row['id'][:28]} onnx {row.get('onnx_count')} / torch {row.get('torch_count')}", fill=(255, 255, 0))
        sheet.paste(thumb, ((k % cols) * cell, (k // cols) * cell))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)


def summarise(rows):
    def spread(values):
        values = [v for v in values if v is not None]
        if not values:
            return {'n': 0, 'median': None}
        return {'n': len(values), 'median': round(float(np.median(values)), 4), 'q25': round(float(np.percentile(values, 25)), 4),
                'q75': round(float(np.percentile(values, 75)), 4)}
    pooled = {arm: {'corners': 0, 'right': 0} for arm in ('onnx', 'torch')}
    for row in rows:
        for arm in pooled:
            for c in row.get(f'{arm}_corner_stats', []):
                pooled[arm]['corners'] += c['corners']
                pooled[arm]['right'] += c['right_corners']
    return {'images': len(rows),
            'torch_seconds_per_image': spread([r.get('torch_timing', {}).get('total_s') for r in rows]),
            'onnx_seconds_per_image': spread([r.get('onnx_timing', {}).get('total_s') for r in rows]),
            'torch_count': spread([r.get('torch_count') for r in rows]), 'onnx_count': spread([r.get('onnx_count') for r in rows]),
            'matched_pairs': int(sum(len(r.get('matched', [])) for r in rows)),
            'matched_iou': spread([m['iou'] for r in rows for m in r.get('matched', [])]),
            'right_corner_share': {arm: (round(v['right'] / v['corners'], 4) if v['corners'] else None) for arm, v in pooled.items()},
            'note': ('agreement between the PyTorch checkpoint and the int8 ONNX export on the same tiles; not accuracy. Score '
                     'definitions differ: the PyTorch processor multiplies the sigmoid score by the presence score before '
                     'thresholding, the production ONNX path thresholds the sigmoid score alone, so counts are not like-for-like')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--weights', default=str(WEIGHTS))
    parser.add_argument('--device', default='mps')
    parser.add_argument('--dtype', choices=('fp32', 'bf16'), default='fp32')
    parser.add_argument('--n', type=int, default=8)
    parser.add_argument('--seed', type=int, default=20260928)
    parser.add_argument('--prompt', default=PROMPT)
    parser.add_argument('--images', nargs='*', help='explicit image paths (overrides the seeded pick)')
    parser.add_argument('--px-m', type=float, default=.236, help='metres per pixel for explicit images')
    parser.add_argument('--skip-onnx', action='store_true')
    parser.add_argument('--trainable', help='finetune_mps trainable-*.pt to apply on top of the base checkpoint')
    parser.add_argument('--out', default=str(OUT))
    args = parser.parse_args(argv)
    if not Path(args.weights).exists():
        raise SystemExit(f'weights missing at {args.weights}: run scripts/ml/training/sam3_mac/fetch_weights.sh (needs hf auth login)')
    rows = ([{'id': Path(p).stem, 'path': p, 'px_m': args.px_m, 'kind': 'explicit'} for p in args.images] if args.images
            else pick_images(args.n, args.seed))
    model, processor, load_s = load_torch_model(args.weights, args.device, args.dtype, args.trainable)
    for row in rows:
        image = Image.open(row['path']).convert('RGB')
        masks_t, scores_t, timing_t = run_torch(processor, image, args.prompt, args.device, args.dtype)
        row.update({'torch_count': len(masks_t), 'torch_scores': [round(s, 4) for s in scores_t], 'torch_timing': timing_t})
        stats_t = [s for s in (corner_stats(m, row['px_m']) for m in masks_t) if s]
        row['torch_corner_stats'] = [{k: v for k, v in s.items() if k != 'polygon'} for s in stats_t]
        row['torch_polygons'] = [s['polygon'] for s in stats_t]
        if not args.skip_onnx:
            masks_o, scores_o, timing_o = run_onnx(image, args.prompt)
            row['onnx_timing'] = timing_o
            if masks_o is not None:
                row.update({'onnx_count': len(masks_o), 'onnx_scores': [round(s, 4) for s in scores_o]})
                stats_o = [s for s in (corner_stats(m, row['px_m']) for m in masks_o) if s]
                row['onnx_corner_stats'] = [{k: v for k, v in s.items() if k != 'polygon'} for s in stats_o]
                row['onnx_polygons'] = [s['polygon'] for s in stats_o]
                row['matched'] = match(masks_o, masks_t)
        print(json.dumps({'image': row['id'], 'torch': row['torch_count'], 'onnx': row.get('onnx_count'),
                          'torch_s': timing_t['total_s']}), flush=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = {'version': VERSION, 'computed_at': now_iso(), 'device': args.device, 'dtype': args.dtype, 'prompt': args.prompt,
               'threshold': THRESHOLD, 'weights': args.weights, 'trainable': args.trainable, 'model_load_s': load_s,
               'summary': summarise(rows),
               'rows': [{k: v for k, v in r.items() if not k.endswith('_polygons')} for r in rows]}
    with open(out / 'summary.json', 'w') as handle:
        json.dump(summary, handle, indent=1, sort_keys=True)
    draw_sheet(rows, out / 'contact_sheet.png')
    json.dump({k: summary[k] for k in ('version', 'device', 'dtype', 'model_load_s', 'summary')}, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
