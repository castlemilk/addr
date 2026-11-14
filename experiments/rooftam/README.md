# RoofTAM experimental bench

Isolated **static-image roof segmentation** experiments for issues #8, #9 and #15.
No production model, service, Fieldwork record, approved label or roof geometry is changed.
This is a runnable experimental adapter, **not a validated roof-specialised model**.

## First run on Kaggle

1. Import `kaggle/rooftam_tpu.ipynb` into a **private** Kaggle notebook. Select an available TPU accelerator and enable Internet. The notebook cannot grant TPU quota or authenticate your account.
2. Run the notebook cells in order. Installation uses an isolated virtual environment and pinned EdgeTAM revision. It does not replace the notebook's Python packages. An arithmetic/gradient preflight must prove a real TPU before proceeding.
3. The default run creates 12 synthetic examples, runs a tiny training control, then executes pretrained EdgeTAM evaluation, three decoder-training steps, re-evaluation and a comparison. Synthetic results are **plumbing only**, never Australian-roof accuracy evidence.
4. Save a Kaggle version to retain outputs. Run directories and a ZIP are created under `/kaggle/working`. Keep outputs private; real-data overlays can expose property imagery and labels.

The first adapter is **single-process/single-worker**, not an all-core TPU throughput implementation. XLA compilation is real work: inspect first-step time separately from warm steps and inspect `aten_counters` / `metrics_report` before making accelerator-performance claims. A nonzero fallback counter requires investigation; a completed run does not prove every operator ran on TPU.

Kaggle's CLI documents TPU metadata but a reported issue describes CPU provisioning despite a TPU request. The bench deliberately errors rather than silently calling a CPU run a TPU result. Select/verify the accelerator in the editor if API provisioning fails.

### Optional authenticated CLI upload

This only prepares a private upload. It does not create an account, request extra capacity or expose credentials.

```bash
python -m experiments.rooftam.kaggle.prepare_upload \
  --owner YOUR_KAGGLE_USERNAME --addr-ref FULL_40_CHARACTER_ADDR_COMMIT \
  --output /tmp/addr-rooftam-upload
# Review kernel-metadata.json, authenticate locally with Kaggle, then:
kaggle kernels push -p /tmp/addr-rooftam-upload --accelerator TpuV5E8
```

The prepared notebook uses the supplied immutable addr commit. The repository notebook initially resolves the experimental branch, and every run records the exact commit. EdgeTAM is pinned to `7711e012a30a2402c4eaab637bdb00a521302c91`; `bootstrap.sh` checks out that revision and verifies any hydrated Git LFS checkpoint hash. Torch 2.8.0, torchvision 0.23.0 and PyTorch/XLA 2.8.0 form the isolated TPU environment. Transitive versions are saved per run; this is not a fully hash-locked wheelhouse. No untested JAX rewrite is included.

## Local CPU plumbing check

Use an environment with PyTorch, NumPy, Pillow, SciPy, pytest and nbformat installed. No pretrained download is needed for these checks.

```bash
python -m pytest -q experiments/rooftam/tests
python -m experiments.rooftam.bench fixture --output /tmp/roof-fixture
python -m experiments.rooftam.bench train --model tiny --device cpu --side 64 \
  --manifest /tmp/roof-fixture/manifest.jsonl --allow-synthetic --steps 3 \
  --output /tmp/roof-control
python -m experiments.rooftam.bench evaluate --model tiny --device cpu --side 64 \
  --manifest /tmp/roof-fixture/manifest.jsonl --allow-synthetic \
  --adapter /tmp/roof-control/adapter.pt --output /tmp/roof-control-eval
```

Use a fresh output directory for each command. Existing results are never silently overwritten.
`tiny` is a small convolutional plumbing control, **not EdgeTAM and not a proposed competing roof model**.
CI has a CPU contract job plus a separate pinned, pretrained EdgeTAM CPU smoke job. The latter downloads upstream weights and checks one actual forward/backward step plus validation on synthetic shapes. Neither job is TPU evidence; a workflow definition is not proof that its run passed.

## Real data contract

Provide a JSONL manifest and native RGB image/binary mask files under one dataset directory. Attach that directory as a private Kaggle input and set `REAL_MANIFEST` in the notebook. No Google/ESRI imagery is scraped or uploaded by these tools.

```json
{"id":"roof_001","split":"train","geo_group":"spatial-block-001","image":"images/roof_001.png","mask":"masks/roof_001.png","box_xyxy":[10,12,240,180],"prompt_source":"detector","gsd_m":0.10,"label_kind":"visible_roof_outline","label_status":"human_verified","training_eligible":true,"training_allowed":true,"cloud_upload_allowed":true,"license":"YOUR VERIFIED IMAGERY AND ANNOTATION LICENCE","source_ref":"YOUR CAPTURE AND HUMAN-REVIEW RECORD"}
```

This is a schema example, not evidence that an image or licence exists. Replace every placeholder with reviewed metadata. `training_allowed` and `cloud_upload_allowed` record independently verified rights, not rights inferred from public visibility. `training_eligible` must come from the human approval record for train samples; an agent review does not grant it. Agent drafts, pseudo-labels and structural priors are not accepted ground truth. Do not silently promote existing addr/Fieldwork review geometry into training data.

`geo_group` must group neighbouring/overlapping captures, the same property and repeated acquisition views into a single split. The validator detects group and identical-image leakage; **it cannot prove geographic independence from an arbitrary group name**. Audit the actual spatial blocking before evaluation. Freeze test areas; tune on train/validation only. The notebook never automatically evaluates test.

Each sample is one prompted roof. `box_xyxy` is an independent detector or human prompt in native pixels, with exclusive upper coordinates. Do not compute validation/test prompts from ground-truth masks and present that as automatic detection. Prompts, labels and provenance are hashed with the manifest. Images and labels must have identical dimensions. `gsd_m` assumes square, isotropic orthophoto pixels; this scalar is not a substitute for a geospatial transform or survey precision.

## Adaptation and evaluation

`EdgeRoof` uses upstream's **differentiable model components**, not `SAM2ImagePredictor`'s no-gradient convenience methods. It freezes the image/prompt encoders and keeps encoder BatchNorm in evaluation mode, while training the mask decoder (including its high-resolution projections). It uses fixed 1024-square letterboxing with inverse mapping, one box/roof, FP32 initially, boundary-weighted BCE plus Dice, and no video-memory tracking or CUDA connected-components postprocessing. The caller must pass an actual pretrained checkpoint; random-weight fallback is forbidden.

`train` uses only the train split. `--adapter` is a warm start, **not exact training resumption**; an optimizer state is saved for later resume support. Long-running distributed training, periodic recovery checkpoints, BF16/quantisation, dedicated roofline heads and downstream polygon/3D topology are out of scope for this first compatibility bench.

Outputs include configuration, source/model/data hashes, dependency/hardware information, per-step losses, actual parameter-update check, trained adapter, per-roof binary masks, original-resolution boundary overlays (truth orange, prediction cyan), IoU, Dice, boundary F1 at 0.25/0.5/1 m, signed area error, and per-sample boundary-distance P95. Reports aggregate macro mask metrics and mean absolute area error. This is **not** instance-detection recall, polygon validity, ridge/valley quality or hidden truss inference.

Train timing separates first step from subsequent synchronized compute; evaluation warmup precedes synchronized model latency. Data loading, inverse mapping and scoring are excluded from model latency. Use sufficient samples/repetitions on the **same hardware, precision, prompts and input resolution** before claiming a speedup. Three-step smoke runs are not performance studies.

## Existing SAM3 baseline

Keep SAM3 ONNX on its existing CPU/GPU execution provider. It is not a PyTorch/XLA TPU model. In the normal addr ML environment, with the existing SAM3 model export and its `onnxruntime`/`tokenizers` dependencies installed:

```bash
python -m experiments.rooftam.export_sam3 \
  --manifest /path/to/manifest.jsonl --models-dir /path/to/sam3-models \
  --output /tmp/sam3-export
python -m experiments.rooftam.bench score-export \
  --manifest /path/to/manifest.jsonl --split val \
  --predictions /tmp/sam3-export/predictions.json --output /tmp/sam3-scores
python -m experiments.rooftam.bench compare \
  --reports /tmp/edge-scores/evaluation.json /tmp/sam3-scores/evaluation.json \
  --output /tmp/roof-comparison
```

The exporter uses current `scripts/ml/sam3.py`, saves no-detection as an empty mask, and associates text proposals to the independent prompt box, **never to the ground-truth mask**. This is not identical prompt semantics to EdgeTAM's box prompting and not the full production polygon-merging pipeline; name the baseline accordingly. Scoring rejects missing/stale predictions and comparisons require the exact same dataset, samples, label hashes and split. Set `SAM3_PREDICTIONS` in Kaggle only when those exports are available; missing baseline results are not fabricated.

## Execution status and remaining gates

Local tests exercise contracts, metrics, notebook syntax, the tiny train/evaluate loop and a test-double gradient/frozen-encoder check. The latter is explicitly **not execution of upstream EdgeTAM**. A real pretrained EdgeTAM load, TPU forward/backward and Kaggle session must be demonstrated with their own run artifacts before marking compatibility verified. No approved real-roof dataset or Kaggle credentials are bundled.

## Primary references

- EdgeTAM: https://github.com/facebookresearch/EdgeTAM/tree/7711e012a30a2402c4eaab637bdb00a521302c91
- PyTorch/XLA version/runtime diagnostics: https://docs.pytorch.org/xla/release/r2.8/learn/troubleshoot.html
- Kaggle TPU setup: https://www.kaggle.com/docs/tpu
- Kaggle kernel metadata: https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md
- Reported provisioning issue: https://github.com/Kaggle/kaggle-cli/issues/1197

EdgeTAM's upstream code/checkpoints carry Apache-2.0 licensing; retain its licence/notices. This adapter does not relicense upstream code or confer imagery/training rights. No upstream source or weights are vendored here.
