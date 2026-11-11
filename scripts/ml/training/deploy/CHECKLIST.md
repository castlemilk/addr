# GPU run checklist

Before renting (on the Mac, free):

1. `scripts/ml/training/deploy/validate_local.sh` prints `VALIDATE_LOCAL_OK` (export, smoke train, resume, ONNX, infer).
2. The dataset is current: `scripts/ml/.venv/bin/python -m scripts.ml.training.export_dataset --out data/reviews/eyes/training/dataset-v1 --tarball`
   (re-export after any new human review, then re-check `DATASET_CARD.md`; the box refuses a tarball whose sha256
   differs from `dataset-v1.tar.sha256`).
3. `scripts/ml/training/deploy/run.sh user@HOST a10 --dry-run` shows the bundle, tarball size and caps.
4. Decide the profile (a10 or a100) and note the hourly price and the hard cap from COSTS.md.

Renting:

5. Pick a single-GPU instance with an NVIDIA driver 525 or newer (`nvidia-smi`), 50 GB disk, and either Docker with
   the NVIDIA container toolkit (`docker run --gpus all` works) or Python 3.12 with `python3.12 -m venv`.
6. Add your SSH key; confirm `ssh user@HOST nvidia-smi` works. Put any non-default port or key in `SSH_OPTS`.

Running:

7. `scripts/ml/training/deploy/run.sh user@HOST a10` (Docker) or `... a10 --mode venv`.
8. Watch the first `step` lines in the followed `box.log`: `images_per_s` tells you whether the step cap or the
   time cap will bind. Ctrl-C only detaches; `run.sh user@HOST a10 --fetch` re-attaches and copies results.
9. If the box is preempted, rerun step 7: training resumes from `out/run/last.pt` (`--resume auto`).

After:

10. Results land in `data/reviews/eyes/training/runs-remote/<profile>-<stamp>/` with `ARTEFACTS.sha256` verified:
    `summary.json`, `train_log.jsonl`, `test_summary.json` (torch), `onnx_eval/test_summary.json` (ONNX graph),
    `best.pt`, `roof_outline.onnx` + `roof_outline.json`.
11. Stop or destroy the instance. Check the provider's billing page shows it stopped.
12. Read the boundary numbers only as agreement with the human references, stratum by stratum, next to the three SAM3
    controls (`sam3_production`, `sam3_raw`, `sam3_regularised`). The model has to beat `sam3_regularised`, which
    costs nothing, before a GPU-trained detector is worth wiring into `scene_isolated` as a candidate.
13. Do not wire the model into production from this run; `infer.RoofOutlineSegmenter` is a candidate detector only.
