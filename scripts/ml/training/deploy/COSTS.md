# Cost table (one fixed-budget run, no purchase made)

Prices are typical on-demand marketplace ranges for a single GPU in September 2026, not quotes: check the provider's
page at booking time (jarvis.ai or any other box with Docker or Python 3.12). Nothing here has been bought or run on
a GPU yet; the minutes are estimates from the step budget, not measurements.

| | A10 (24 GB) | A100 (40 or 80 GB) |
| --- | --- | --- |
| Typical hourly price | $0.45 to $0.90 | $0.90 to $1.60 (40 GB), $1.20 to $2.20 (80 GB) |
| Config | `configs/a10.json`: batch 4, 1024 px crops, bf16 | `configs/a100.json`: batch 8, 1024 px crops, bf16 |
| Setup (image build or pip, dataset sync of 846 MB) | about 10 min, capped at 30 | about 10 min, capped at 30 |
| Training (budget guard `--max-minutes` minus reserve) | stops by 88 min (4,000 step cap may bind first) | stops by 65 min (5,000 step cap may bind first) |
| Final validation + held-out evaluation (inside the reserve) | about 5 min | about 4 min |
| ONNX export + ONNX re-evaluation | about 3 min, capped at 15 + 15 | about 3 min, capped at 15 + 15 |
| Expected wall time | about 1.8 h | about 1.4 h |
| Expected cost | about $0.80 to $1.60 | about $1.25 to $3.10 |
| Hard cap (setup 30 + external `timeout` on training + 30 post) | 180 min: at most $1.35 to $2.70 | 155 min: at most $2.35 to $5.70 |

How the cap is enforced:

- `train.py` stops itself at `max_minutes - reserve_minutes` of training time (cumulative across resumes) or at
  `max_steps`, whichever comes first, then evaluates within the reserve.
- `box.sh` wraps training in `timeout --signal=TERM` at `HARD_CAP_MINUTES` (A10 120, A100 95). SIGTERM makes
  `train.py` save `last.pt` and exit 3; the checkpoint is still exported.
- Image build or pip install, ONNX export and ONNX evaluation each have their own `timeout`.
- The one cost no script can cap is an instance left running after the job: stop or destroy it (see CHECKLIST.md),
  or set `AUTO_SHUTDOWN_MINUTES=20` when the box grants passwordless sudo.

A second arm (`configs/a10-regularised.json`) doubles the A10 figures. Run it only if the raw arm's corners are
still rounded against the human references.
