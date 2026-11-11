"""Export a train.py checkpoint to ONNX for the existing CPU (onnxruntime) inference path, then check parity.

    python -m scripts.ml.training.export_onnx --checkpoint RUN/best.pt --out RUN/roof_outline.onnx \
        [--check-image IMAGE.png] [--opset 17]

Graph: image float32 [3, H, W] in [0, 1] (any H, W) -> boxes [N, 4] pixels xyxy, scores [N], mask_probs [N, 1, M, M].
Masks stay unpasted (infer.paste_mask does it in numpy, mirroring torchvision). A sidecar <out>.json records the
checkpoint, dataset and code hashes, versions and the torch-vs-onnxruntime parity on the check image.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from . import common
from .infer import INPUT_NAME, OUTPUT_NAMES, image_tensor
from .model import MODEL_VERSION, ExportWrapper, load_checkpoint_model


def export(checkpoint_path, out_path, opset=17):
    model, checkpoint = load_checkpoint_model(checkpoint_path)
    wrapper = ExportWrapper(model).eval()
    example = torch.rand(3, 512, 512)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')   # torchvision's tracing notes and the TorchScript-exporter deprecation notice
        torch.onnx.export(wrapper, (example,), str(out_path), dynamo=False, opset_version=opset,
                          input_names=[INPUT_NAME], output_names=list(OUTPUT_NAMES),
                          dynamic_axes={INPUT_NAME: {1: 'height', 2: 'width'}, 'boxes': {0: 'n'}, 'scores': {0: 'n'},
                                        'mask_probs': {0: 'n'}},
                          do_constant_folding=True, external_data=False)
    return model, checkpoint


def parity(model, onnx_path, image, top_k=20):
    """Torch vs onnxruntime on one image: detection counts and top-k box / score / mask differences."""
    import onnxruntime as ort
    tensor = image_tensor(image)
    started = time.monotonic()
    with torch.no_grad():
        t_boxes, t_scores, t_masks = (x.numpy() for x in ExportWrapper(model).eval()(torch.from_numpy(tensor)))
    torch_s = time.monotonic() - started
    session = ort.InferenceSession(str(onnx_path), providers=['CPUExecutionProvider'])
    started = time.monotonic()
    o_boxes, o_scores, o_masks = session.run(list(OUTPUT_NAMES), {INPUT_NAME: tensor})
    onnx_s = time.monotonic() - started
    k = min(top_k, len(t_scores), len(o_scores))
    report = {'image_size': list(image.size), 'n_torch': int(len(t_scores)), 'n_onnx': int(len(o_scores)), 'top_k': k,
              'torch_seconds': round(torch_s, 3), 'onnxruntime_seconds': round(onnx_s, 3),
              'mask_prob_shape_onnx': list(np.shape(o_masks)), 'boxes_shape_onnx': list(np.shape(o_boxes))}
    if k:
        report.update(max_box_diff_px=float(np.abs(t_boxes[:k] - o_boxes[:k]).max()),
                      max_score_diff=float(np.abs(t_scores[:k] - o_scores[:k]).max()),
                      max_mask_prob_diff=float(np.abs(t_masks[:k] - o_masks[:k]).max()))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--opset', type=int, default=17)
    parser.add_argument('--check-image', help='PNG for the parity check (default: a 512 px random image).')
    args = parser.parse_args(argv)
    model, checkpoint = export(args.checkpoint, args.out, args.opset)
    if args.check_image:
        image = Image.open(args.check_image).convert('RGB')
    else:
        image = Image.fromarray(np.random.default_rng(0).integers(0, 255, (512, 512, 3), dtype=np.uint8))
    report = parity(model, args.out, image)
    import onnxruntime
    sidecar = {'model_version': MODEL_VERSION, 'onnx_sha256': common.sha256_file(args.out),
               'onnx_bytes': Path(args.out).stat().st_size, 'checkpoint': str(args.checkpoint),
               'checkpoint_sha256': common.sha256_file(args.checkpoint), 'checkpoint_step': checkpoint['state'].get('step'),
               'dataset_manifest_sha256': checkpoint.get('dataset_manifest_sha256'), 'code_sha256': checkpoint.get('code_sha256'),
               'model_config': checkpoint['model_config'], 'opset': args.opset, 'input': {INPUT_NAME: '[3, H, W] float32 in [0, 1]'},
               'outputs': {'boxes': '[N, 4] xyxy pixels', 'scores': '[N]', 'mask_probs': '[N, 1, M, M] unpasted probabilities'},
               'versions': {'torch': torch.__version__, 'onnxruntime': onnxruntime.__version__},
               'check_image': args.check_image, 'parity': report, 'exported_at': common.now_iso(),
               'note': 'Candidate detector only; not wired into production. ' + common.NOTE}
    common.write_json_atomic(Path(args.out).with_suffix('.json'), sidecar)
    json.dump({k: sidecar[k] for k in ('onnx_sha256', 'onnx_bytes', 'checkpoint_step', 'parity')}, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
