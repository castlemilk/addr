"""Export current addr SAM3 predictions on CPU/GPU; ONNX is not a TPU backend."""
from __future__ import annotations

import argparse
from pathlib import Path
import time

import numpy as np
from PIL import Image

from .bench import prepare_output, write_json
from .data import dataset_digest, load_manifest


def box_overlap(mask, box):
    y, x = np.where(mask)
    if not len(x):
        return 0.0
    a = (x.min(), y.min(), x.max()+1, y.max()+1)
    inter = max(0, min(a[2],box[2])-max(a[0],box[0])) * max(0, min(a[3],box[3])-max(a[1],box[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (box[2]-box[0])*(box[3]-box[1]) - inter
    return inter/union if union else 0.0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', required=True)
    p.add_argument('--models-dir', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--allow-synthetic', action='store_true')
    p.add_argument('--provider', choices=('CPUExecutionProvider','CUDAExecutionProvider'), default='CPUExecutionProvider')
    p.add_argument('--prompt', default='roof')
    args = p.parse_args()
    rows = load_manifest(args.manifest, args.allow_synthetic)
    out = prepare_output(args.output)
    import onnxruntime as ort
    if args.provider not in ort.get_available_providers():
        raise RuntimeError(f'Requested ONNX provider unavailable: {args.provider}')
    from scripts.ml.sam3 import Sam3TextSegmenter
    engine = Sam3TextSegmenter(args.models_dir, providers=[args.provider])
    records = []
    for row in rows:
        image = Image.open(row['image_path']).convert('RGB')
        started = time.perf_counter()
        proposals = engine.proposals(engine.encode_image(image), args.prompt, image.size)
        # Associate by independent prompt box; NEVER select using ground-truth IoU.
        ranked = sorted(proposals, key=lambda x:(box_overlap(x.mask,row['box_xyxy']),x.score), reverse=True)
        chosen = ranked[0] if ranked and box_overlap(ranked[0].mask,row['box_xyxy']) > 0 else None
        pred = chosen.mask if chosen else np.zeros((image.height,image.width),bool)
        seconds = time.perf_counter()-started
        name = f"{row['id']}.png"
        Image.fromarray(pred.astype(np.uint8)*255).save(out/name)
        records.append(dict(id=row['id'], image_sha256=row['image_sha256'], mask=name,
                            detected=chosen is not None, end_to_end_seconds=seconds))
    write_json(out/'predictions.json', dict(dataset_sha256=dataset_digest(rows), samples=records,
        source=dict(model='addr Sam3TextSegmenter', model_identity=engine.model_identity,
                    provider=args.provider, prompt=args.prompt, selection='prompt-box overlap, then proposal score',
                    note='Text proposals associated to a box, not geometric-prompt equivalence; ONNX timings include encoding.')))


if __name__ == '__main__':
    main()
