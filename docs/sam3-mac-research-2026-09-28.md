# SAM3 fine-tuning on Apple Silicon: what exists, what breaks, what to expect (28 September 2026)

Research notes behind `scripts/ml/training/sam3_mac/`. Facts were checked on the linked pages or measured on this
machine (M5 Max, 128 GB, macOS 26.5.1, torch 2.14.0, torchvision 0.29.0) on 28 September 2026. Numbers are timings and
memory, not accuracy.

## The fork we vendored

`MaximeLglr/sam3-apple-silicon` is upstream `facebookresearch/sam3` at 84cc43bc (2025-11-19) plus two commits:
packaging (`packages = find`) and "Adapt for Mac inference (Image only)". The second commit makes the Triton EDT
optional with an OpenCV fallback, builds the position-encoding and decoder coordinate caches on CUDA-or-CPU instead of
CUDA, drops `pin_memory()` in the geometry encoder, moves any device string through `model.to(device)`, and stubs the
`decord` import. It targets CPU; nothing mentions MPS; training code is untouched. Issues are disabled, 0 PRs, last
push 2025-11-22. Sources: https://github.com/MaximeLglr/sam3-apple-silicon and its commit
5bf884158da11242420ce100f8f02f1591be25fe.

## Upstream status (to 2026-09-18)

- No MPS or Apple Silicon support merged. README requires CUDA 12.6 and pins torch 2.10 cu128. Open Mac/CPU PRs:
  #173, #188, #258, #264 (MPS image and video), #400 (MPS image; M2 about 6.5 s per inference), #482, #487, #510,
  #588, #592, #627 (MPS/CPU inference for SAM 3.1, 2026-09-25, "training code is unchanged"). A maintainer said a
  backend-agnostic image predictor would be accepted but unsupported (issue #164).
- Training regression on current main: since the SAM 3.1 release the ViT MLP uses `perflib/fused.py::addmm_act`,
  which raises when grad is enabled, and `aten::_addmm_activation` has no MPS kernel in torch 2.14 (issue #509).
  The vendored November 2025 code predates it, which is one reason to stay on the fork for training.
- No official LoRA/PEFT recipe or freeze config (PR #496 adds freezing). Configs: ODinW13, Roboflow-100VL, evals.
- Official config expectations: COCO json whose category names become the text prompts (`COCO_FROM_JSON`),
  `include_negatives: true`, `resolution: 1008` fixed (RoPE table is 72x72, issue #251), `enable_segmentation`
  must be set in four places and the mask loss block uncommented (Masks `loss_mask` 200, `loss_dice` 10, focal
  0.25/2; the `SemanticSegCriterion` path in the comment is wrong, issue #189), bf16 AMP, AdamW, clip 0.1,
  inverse-sqrt schedule, lr transformer 8e-5, vision 2.5e-5, language 5e-6, layer decay 0.9, wd 0.1, batch 1 per
  GPU, `use_act_checkpoint` on in training.
- Memory reports: 8xB200 at batch 1 per GPU, 1.88 s per batch (issue #189); OOM at batch 1 on a 48 GB A6000 and on
  2xT4, with the advice to freeze backbones (issues #189, #343). Issue #580 reports a fine-tuned model worse than
  base despite falling training metrics, so hold-out validation against human references is not optional.

## Community fine-tuning

- Roboflow: hosted, paid, no notebook, no hyperparameters published (blog 2026-05-15).
- Hugging Face transformers `Sam3Model`/`Sam3Processor`: inference only, no loss code; MPS autocast in RoPE fixed by
  PR #49006 (2026-09-22). The FiftyOne notebook freezes the vision encoder and trains the rest with a hand-written
  matcher and Dice+BCE at lr 1e-5.
- `Sompote/SAM3_LoRA` (288 stars, active): custom LoRA on ViT and DETR, text encoder frozen, hard-negative prompts,
  rank 16 / alpha 32 / lr 5e-5, about 0.58 s per image at batch 8 on an RTX 5090 class GPU; its attention LoRA
  materialises the full attention matrix (issue #39). One user trained a single `roof` category on 146 images.
  No Mac mentions.
- Mac inference forks: `JordiSpranger/sam3.1-mps` (M4 Max fp16, about 0.8 s encode, 4 GB), `drduhe/lam` (merges
  open PRs). MLX: `Deekshith-Dade/mlx_sam3` and SAM2 ports are inference only; no MLX port with training exists.

## PyTorch MPS gaps that touch this architecture (torch 2.14)

- bf16 autocast on MPS: supported since 2.6 (macOS 14+); bf16 and fp16 run at the same speed locally; use bf16
  with no GradScaler.
- `grid_sample` (mask point sampling, geometry encoder): forward long supported; backward kernel only since 2.13.
- torchvision `roi_align` (geometry encoder): MPS backward over-accumulated gradients about 100x until torchvision
  0.29.0 (2026-09-02, PRs #9510 and #9563). We have 0.29.0.
- Antialiased interpolate backward only since 2.14 (training transforms run on CPU, so unaffected).
- Complex RoPE (`view_as_complex`, `torch.polar`, complex repeat) works on 2.14; SDPA math kernel and an MPP prefill
  attention kernel for head_dim 64 on macOS 26.2+ (SAM3 head_dim is 64).
- `_assert_async` native since 2.13 (earlier forks needed `PYTORCH_ENABLE_MPS_FALLBACK=1` only for this).
- `PYTORCH_ENABLE_MPS_FALLBACK=1` must be set before importing torch and breaks c10d ops; do not DDP-wrap on MPS.
- `torch.autocast(device_type="cuda", enabled=False)` does not switch bf16 off on MPS: the decoder FFN would run
  in bf16 where upstream runs fp32 (patched in `patches/0002`).
- `torch.compile` on MPS: leave off.

## Throughput measured on this machine

SAM3-shaped ViT trunk (451M params, dim 1024, depth 32, window 24, 4 global blocks, 72x72 tokens, complex RoPE,
SDPA, random weights), batch 1 unless noted. One-off measurement by the research agent with a throwaway script; the
script and its output were not kept, so treat the rows as indicative until re-run from the repo:

| configuration | s per iteration | memory |
|---|---|---|
| forward, no grad, fp32 | 0.67 | |
| forward, no grad, bf16 | 0.23 | 3 GiB |
| forward + backward, full trunk, bf16, no checkpointing | 1.33 | 27.9 GiB |
| forward + backward, full trunk, bf16, activation checkpointing | 1.67 | 12.4 GiB |
| forward + backward, full trunk, fp32 | 2.35 | 31 GiB |
| batch 4, forward + backward, checkpointing | 2.44 per image | 46.7 GiB |

Batch 1 with gradient accumulation is the right shape. Our own dry-run of the whole model with frozen backbones
(random init, fp32, 1008 px) measured 2.1 to 2.3 s per step at 4 GB allocated; see `docs/roof-eyes.md`.

## Recommended path (phase 1 verified where marked)

1. torch 2.14, torchvision 0.29 or later, `PYTORCH_ENABLE_MPS_FALLBACK=0` so any fallback fails loudly [verified].
2. Keep the fork's model code (predates `addmm_act`); patches: coordinate caches on device, FFN autocast
   device-agnostic, Triton optional [verified by dry-run].
3. COCO with category names as prompts, 1008 px, segmentation on in all four places, mask loss enabled
   [verified]; add hard-negative prompts once more than one category exists [inferred].
4. Phase 1: freeze `backbone.vision_backbone` and `backbone.language_backbone`; train transformer, scoring and
   segmentation head at lr 8e-5, AdamW, wd 0.1, clip 0.1, no activation checkpointing [inferred from issues and
   community runs].
5. bf16 autocast, no GradScaler, batch 1 [measured]; gradient accumulation 4 to 8 [inferred, not yet run].
6. Phase-1 step time with frozen backbones: measured 1.3 to 1.5 s in bf16 and 2.1 to 2.3 s in fp32 on an idle machine
   (`run-dry-fb0-bf16`, `run-dry`); phase 2 (unfreeze the last 8 to 16 ViT blocks or LoRA rank 16 on the linear
   layers) about 1.7 to 2.3 s per step at 15 to 30 GB [trunk timings measured, rest inferred].
7. Judge base against fine-tuned on the held-out human references with negative prompts; spot-check MPS against
   CPU outputs on a few images [standing rule].
