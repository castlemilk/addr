# SAM3 on the Mac (Apple Silicon, MPS)

Goal: run and fine-tune Meta's SAM3 locally on the M5 Max instead of renting a GPU, so the hi-res teacher loop
(`scripts/ml/training/teacher_sources.py`, `hires_teacher.py`) and the outline model can iterate on the same machine.

## Pieces

| file | what it does |
|---|---|
| `VENDOR.json` | pin of the vendored fork (MaximeLglr/sam3-apple-silicon @ 5bf8841), upstream head checked, venv recipe, patches |
| `patches/` | edits applied to the vendored clone (`patches/apply.sh`): Triton optional in the focal loss; decoder coordinate cache on MPS and FFN autocast made device-agnostic |
| `fetch_weights.sh` | downloads the gated `facebook/sam3` checkpoint (3.45 GB) to `scripts/ml/models/sam3-hf/` |
| `prepare_coco.py` | dataset-v1 / distill-* folders -> SAM3 COCO folders with the production prompt as category name |
| `smoke_infer.py` | PyTorch checkpoint on MPS vs the production int8 ONNX on the same tiles: counts, matched IoU, right-corner share, s/image |
| `finetune_mps.py` | single-process fine-tune loop over the fork's dataset, transforms, matcher and loss (no Hydra, no DDP) |
| `reference_eval.py` | judge of checkpoints on the human boundary references (agreement, paired image bootstrap, novel-detection proxy) |
| `round3.sh` | round-3 waiter: one fine-tune at a time (pgrep gate, nohup, bf16, MPS allocator capped), fold and loader gates, judge per block, one row per arm in `round3/results.jsonl`; idempotent (skips recorded arms and finished judge blocks, resumes from `trainable-step*.pt`); `DRY_RUN=1` prints the plan |
| `r3_record.py` | ledger helper for the waiter: fold gate (0 images over the cap), done check, resume candidate, row and `*_error` row writer with git-free code sha256s |
| `round3_report.py` | renders `round3/results.jsonl` as a measurement-first markdown report (`REPORT.md` beside the ledger, or `--out docs/...`) |

Tasks: `task training:sam3-mac:fetch | coco | smoke | dry-run | finetune`.

## One-time setup

1. Create the venv `scripts/ml/training/.venv-sam3` (Python 3.12, torch 2.14, MPS) with the recipe in `VENDOR.json`.
2. `sh scripts/ml/training/sam3_mac/patches/apply.sh` after any fresh clone of the fork.
3. `hf auth login` (the account with facebook/sam3 access), then `task training:sam3-mac:fetch`.

## What the fork gives and does not give

The fork is upstream SAM3 (November 2025) plus a packaging commit and an image-only Mac inference commit; video,
evaluation and test code are still present. It has no MPS-specific code: device strings default to CUDA-or-CPU, the
trainer is CUDA + torch.distributed, the focal loss imports Triton.
`finetune_mps.py` therefore builds the model with `device='mps'`, uses loss normalisation `local`, and keeps the fork's
components for everything else so a later GPU run of the official trainer stays comparable.

## Measurement discipline

Everything `smoke_infer.py` reports is agreement between two runs of the same model family, not accuracy. Fine-tune
losses are training diagnostics. The judge of any fine-tuned checkpoint stays the 58 Mooroolbark human references
through the existing outline evaluation, never the teacher masks.
