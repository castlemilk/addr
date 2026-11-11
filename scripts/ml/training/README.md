# Corner-preserving roof outline training kit

A fixed-budget fine-tune of a building-roof instance segmenter whose masks keep square corners, validated locally
on CPU/MPS and shippable to one rented Linux GPU with one command. Nothing here writes under `data/scenes`, the
eyes index or any production artefact. Outputs go to `data/reviews/eyes/training/`. Nothing is wired into
production: `infer.RoofOutlineSegmenter` is a candidate detector with the same proposal interface as
`Sam3TextSegmenter.proposals`.

Every boundary number this kit prints is agreement with a few dozen human outlines, never accuracy. Validation
scores are agreement with SAM3 pseudo-labels.

## Why: the bottleneck is the mask resolution

SAM3's decoder returns 288 x 288 masks for its 1008 px input (measured from the FPN shapes of the repo's int8
export). On a 1152 px suburb tile at 0.236 m/px, one mask cell therefore covers 4 x 4 pixels, about 0.94 m. The
bilinear upsample and `mask_to_polygons(simplify_px=0.9)` then round every corner at that scale. A 2.4 m shed spans
2.5 cells and comes out as a circle. Measured on the held-out human references (`control-v1/test_summary.json`),
SAM3's production outline is within 0.45 m Hausdorff of 11 of 53 matched same-image references; 5 of 58 are not
matched at all.

## Model choice

| candidate | licence | mask cell for a 20 m roof at 0.236 m/px | ONNX for the CPU path | fits about 2 GPU hours | notes |
| --- | --- | --- | --- | --- | --- |
| SAM2 mask decoder or adapter fine-tune | Apache-2.0 (checkpoints ungated on Hugging Face) | 256 x 256 per 1024 px input: about 0.94 m on a full tile, fine only on per-roof crops | community exports only | decoder yes | needs box or point prompts, so it refines SAM3's boxes rather than replacing the detector; crop-per-roof inference means about 80 image encodes per tile on CPU |
| YOLOv8/11-seg (ultralytics) | **AGPL-3.0** (commercial use needs an Enterprise licence) | prototypes at 1/4 input: about 0.94 m at native size | excellent | yes | fastest to train and export; licence is the blocker for a closed product |
| Mask2Former | Apache/MIT (HF transformers) | 1/4 input, then upsampled: about 0.94 m | painful (deformable attention; pure-PyTorch fallback is slow) | marginal | best published quality, but typically trained for tens of epochs |
| **Mask R-CNN, torchvision R50-FPN v2 (picked)** | **BSD-3-Clause** code | per-box head: 28 x 28 stock, **56 x 56 here**: about 0.36 m (6 m shed: 0.11 m) | **official exporter; verified here** | **yes** (about 1.5 h A10, 1 h A100) | detector and masks in one graph; no prompts |

Pick: torchvision Mask R-CNN (ResNet-50 FPN v2, COCO weights), changed in `model.py` to

- a 56 x 56 mask head (RoIAlign 28 x 28 plus the stock 2x deconvolution, whose COCO filters are kept), so mask
  resolution scales with the roof instead of the tile;
- a native-resolution transform (the image is never rescaled, so the model sees the saved PNG's pixels 1:1);
- anchors from 16 px (3.8 m) up, so garden sheds get proposals;
- a mask loss that up-weights the target boundary band 3x (where corners live) and multiplies each instance by its
  provenance weight (human 8 to 10, pseudo 1).

Trade-offs, stated plainly. Mask R-CNN is not the strongest segmenter on benchmarks, and its masks are still
bilinear upsamples of a 56 x 56 grid, so very large buildings (60 m or more) get about 1 m cells again. It is the
only candidate that is permissively licensed, exports to ONNX with the official tooling (parity measured at 3e-5 px
on boxes), runs in the existing onnxruntime CPU path in the main venv without torch, and fits the budget. YOLO-seg
would be quicker, but it is AGPL. SAM2 would give crisper corners on crops, but it cannot find roofs by itself. The
pasted masks match torchvision's own paste pixel for pixel (numpy paste in `infer.py`, 0 mismatches over 13 M px).

SAM3's PyTorch checkpoint (`facebook/sam3`) is gated behind a manual access request on Hugging Face (checked
2026-09-27), and the repo's int8 ONNX export cannot be fine-tuned. Nothing here depends on either; SAM3 appears only
as the pseudo-label source and the control.

COCO pretrained weights come from download.pytorch.org without a separate licence file (COCO annotations are
CC BY 4.0; the images carry mixed Flickr licences). If that matters for a commercial product, train from
`--pretrained imagenet` or `none` and expect a longer schedule.

## The dataset (`export_dataset.py`)

```sh
scripts/ml/training/.venv/bin/python -m scripts.ml.training.export_dataset --out data/reviews/eyes/training/dataset-v1 [--tarball]
# the main venv works too: scripts/ml/.venv/bin/python -m scripts.ml.training.export_dataset ...
```

It runs in about 30 s and writes `images/`, uint16 `labels/` (0 background, 65535 ignore, else instance index),
`train.json`/`val.json`/`test.json` (COCO polygons with per-instance `provenance`, `tier`, `weight`,
`boundary_reference`, `ref_id`, optional `segmentation_regularised`), `references.json` (the held-out human outlines),
`sam3_control.json` (SAM3's production and raw rings for every test image), `manifest.json` (sha256 of every image,
label and JSON, counts per split and provenance, exclusions, ignore reasons) and `DATASET_CARD.md`.

dataset-v1 (2026-09-27): 562 images (300 train, 49 validation, 213 test), 18,036 SAM3 pseudo-labels and 65 human
outline placements from 61 distinct human outlines, all in test. The split is by z17 tile with seed 20260927, and
every tile holding a human reference is test (18 tiles). 101 machine-provenance review objects are excluded and
their pixels ignored. The card has every denominator.

Consequence to keep in mind: because every human outline sits in test, the model trains on SAM3 pseudo-labels only.
The provenance weights are recorded and applied, but they only bite once human outlines exist on train tiles.

Pixel convention: polygons are rasterised by sampling pixel centres (`common.fill_rings`), the exact inverse of
`segmentation.mask_to_polygons`. PIL's polygon fill includes both boundary pixels and would bias every outline by
one pixel to the right and bottom, about 0.3 m of Hausdorff.

## Training (`train.py`)

```sh
scripts/ml/training/.venv/bin/python -m scripts.ml.training.train --smoke --dataset data/reviews/eyes/training/dataset-v1
scripts/ml/training/.venv/bin/python -m scripts.ml.training.train --config scripts/ml/training/configs/a10.json --run-dir RUN
```

- Budget guard: `--max-minutes` (training wall time, cumulative across resumes, minus `--reserve-minutes` kept for
  the held-out evaluation), `--max-steps`, early stop after `--patience` validation checks without `--min-delta` gain
  in the validation boundary F-score at 0.5 m. The cosine learning-rate schedule follows whichever of steps and time
  binds first (`schedule: budget`).
- Determinism: one seed drives initialisation, and every batch (images, crop offsets, flips, quarter turns, colour
  jitter) is a pure function of (seed, step). A resumed run sees exactly the batches of an uninterrupted one. On CPU,
  two runs give identical losses (checked). On CUDA, roi_align's backward uses atomics, so gradients are not
  bit-identical.
- Checkpoints: `last.pt` every `--checkpoint-minutes` and on exit, `best.pt` on every validation gain, written
  atomically. SIGTERM or SIGINT saves `last.pt` and exits 3. `--resume auto` continues from `RUN/last.pt` and refuses
  a checkpoint made on another dataset export.
- Logs: `RUN/train_log.jsonl` (start, step, eval, checkpoint, warning, stop, test, done), `RUN/summary.json`,
  `RUN/test_summary.json` and `RUN/test_rows.jsonl`.
- `--target regularised` trains on SAM3 outlines snapped to their dominant axis pair (`common.orthogonalise`: square
  corners, gated at IoU 0.85 and 1.2 m Hausdorff, raw where refused; 6,508 of 18,036 pseudo-labels qualify). On the
  held-out references this prior moves per-reference-vertex distance closer for 23 roofs and further for 8, but
  Hausdorff better for 14 and worse for 17. It is mixed, so `raw` stays the default. That comparison looked at test
  references, so choosing `regularised` because of it would be a small test peek; `configs/a10-regularised.json`
  is the optional second arm.
- `--smoke` (CPU, random init, 20 images, 2 steps, 384 px crops) completes in about 55 s on the M5 Max, and on MPS
  as well. It proves the loop, not quality.

## Held-out evaluation (`evaluate.py`, `metrics.py`)

For each boundary reference (human_T1 and human_T2; lines-only rows keep SAM3's outline and are excluded), the
best-IoU prediction is compared (IoU of at least 0.1, else counted as a miss) on mask IoU, boundary F-score within
0.5 m, and corner error after `segmentation.mask_to_polygons(simplify_px=0.9)`: Hausdorff and per-reference-vertex
distance, in metres. Three controls go through the identical path: `sam3_production` (latest.json ring),
`sam3_raw` (`boundary_refinement.raw_ring`) and `sam3_regularised`. Results are reported per stratum with its own
denominator: same-image, outline changed by a person (55 of 58), confirmed as drawn (3; the control scores about 0
there by construction) and cross-image (5 placements of 1 CRM outline drawn over another capture). Paired deltas
come with bootstrap 95% intervals.

```sh
scripts/ml/.venv/bin/python -m scripts.ml.training.evaluate --dataset data/reviews/eyes/training/dataset-v1 --out data/reviews/eyes/training/control-v1          # controls only
scripts/ml/.venv/bin/python -m scripts.ml.training.evaluate --dataset data/reviews/eyes/training/dataset-v1 --onnx RUN/roof_outline.onnx --out RUN/onnx_eval   # a model
```

## ONNX and the proposal interface

```sh
scripts/ml/training/.venv/bin/python -m scripts.ml.training.export_onnx --checkpoint RUN/best.pt --out RUN/roof_outline.onnx --check-image IMAGE.png
scripts/ml/.venv/bin/python -m scripts.ml.training.infer --model RUN/roof_outline.onnx --image IMAGE.png --overlay /tmp/overlay.png
```

The graph takes one image `[3, H, W]` in [0, 1] and returns boxes, scores and unpasted 56 x 56 mask probabilities
(pasting N full-resolution masks inside the graph would cost 425 MB per tile). `RoofOutlineSegmenter.encode_image` runs
the network once. `proposals(features, prompt, image_size, threshold, max_proposals)` returns `Sam3Proposal(mask
bool HxW, score, box normalised xyxy)` for roof prompts and `[]` for anything else, so `scene_isolated` could later
use it for the building scan only. A 1152 px tile takes about 12 s on CPU through onnxruntime in the main venv.

## Rented GPU box (jarvis.ai or any box with Docker or Python 3.12)

```sh
scripts/ml/training/deploy/validate_local.sh                          # free, about 2.5 min: export 3 tiles, smoke, resume, ONNX, infer
scripts/ml/training/deploy/run.sh user@HOST a10 --dry-run             # check tarball and bundle, touch nothing remote
scripts/ml/training/deploy/run.sh user@HOST a10                       # Docker (nvidia/cuda 12.6 runtime base, torch cu126)
scripts/ml/training/deploy/run.sh user@HOST a100 --mode venv          # plain python3.12 venv from requirements-gpu.txt
scripts/ml/training/deploy/run.sh user@HOST a10 --fetch               # re-attach after a dropped ssh and copy results
```

`run.sh` syncs `dataset-v1.tar` (846 MB; the box refuses a sha256 mismatch) and a 200 KB code bundle, then starts
`box.sh` detached. `box.sh` trains under the budget with an external `timeout`, exports ONNX, re-evaluates the ONNX
graph and writes `ARTEFACTS.sha256`. `run.sh` copies everything except `last.pt` back into
`data/reviews/eyes/training/runs-remote/<profile>-<stamp>/` and verifies the hashes. Costs and caps are in
`deploy/COSTS.md`, the step-by-step in `deploy/CHECKLIST.md`.

## Environments

- `scripts/ml/training/.venv`: `python3.12 -m venv`, pins in `requirements-local.txt` (torch 2.14.0 CPU/MPS,
  torchvision 0.29.0, onnx 1.23.0, onnxruntime 1.30.0, plus the export chain's cv2 and friends at the main venv's
  versions). The main ML venv gains nothing.
- GPU: `requirements-gpu.txt` (torch 2.14.0+cu126). torch 2.14.0 is published for cu126, cu130 and cu132, but not
  for cu128.
- Tests (main venv, no torch at collection): `scripts/ml/tests/test_training_export.py`,
  `test_training_metrics.py`, and `test_training_validate_local.py` (runs `validate_local.sh`; skips when the training
  venv is absent).

## Open risks

1. The training signal is SAM3 pseudo-labels only, so a network that fits them well learns SAM3's rounding. The
   higher-resolution head and boundary weighting let it be sharper than its labels only where image evidence
   agrees. The lever that removes this risk is human outlines on train tiles, or the regularised arm.
2. Validation (early stopping) measures agreement with pseudo-labels, not with people.
3. 58 same-image boundary references, 53 matched by the control: a difference of a few roofs is inside the bootstrap
   interval. Report deltas with their intervals and denominators, never a single number.
4. Throughput and minutes on A10/A100 are estimates; the Docker image has not been built here (x86 CUDA image, slow
   link). The time guard caps the cost either way; the first `step` log lines show the real images per second.
5. 213 of 562 images (38%) are held out, because captures cluster around the referenced tiles.
6. COCO weight provenance for commercial use (see above).
