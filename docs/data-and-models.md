# Data & model file management

This repo keeps **code, docs and small manifests in git; bulk data, model
weights and build outputs stay on disk only**. As of the 2026-10 cleanup the
`data/` tree is ~78 GB of local pipeline state — it is produced by the
pipeline, never by hand, and is safe to regenerate or delete.

## The rule

- `data/` — **everything is gitignored except `data/imagery/<source>/provenance.json`**.
  When you register a new teacher-imagery source, commit only its
  `provenance.json`; the tiles under the source folder stay local.
- `scripts/ml/models/` — ONNX weights (`*.onnx`, `*.data`) and the gated
  `sam3-hf/` snapshot are gitignored; small configs (`models/sam3/tokenizer*.json`)
  are tracked.
- `scripts/ml/training/` — the vendored SAM3 fork (`vendor/`) and the venvs
  (`.venv/`, `.venv-sam3/`) are gitignored; the pin lives in
  `sam3_mac/VENDOR.json`.
- `/bin/`, `/crmctl`, `/annotationctl`, `/annotation-server` — compiled Go
  binaries; rebuild, don't commit.
- `public/maplibre/`, `public/address-roofs.json`, `apps/annotation-web/dist/`,
  `deploy/annotation/node_modules/`, `.run/`, `.task/` — generated, gitignored.
- `docs/Roofline-extraction-from-orthophotos/` — a local clone of
  <https://github.com/tudelft3d/Roofline-extraction-from-orthophotos> kept for
  reference only (see README "TU Delft roofline port"). Re-clone on demand.
- `.serena/`, `.playwright-mcp/`, `.pnpm-store/`, `.claude/settings.local.json`,
  `*.ses` — agent/editor local state, gitignored.

## What's under `data/` and where it comes from

Top-level files:

| Path | What | Produced by |
| :--- | :--- | :--- |
| `australia_geo.db` | SQLite address/geo store used by the Go service (`DB_PATH`) | address ingestion / `task addresses` |
| `australia_tiles.mbtiles` | Overlay MVT archive: building + tree layers (`TILES_PATH`) | `task tiles` (`scripts.ml.pipeline --out`) |
| `australia_tiles_{dedup,iso,sam3,sam3f}.mbtiles` | Variant archives from refinement/dedup/isolation/SAM3 passes | e.g. `scripts.ml.refine_conflicts --in … --out …` |
| `*.db-shm`, `*.db-wal`, `*.mbtiles-shm`, `*.mbtiles-wal` | SQLite WAL sidecars | created/removed automatically by open handles — never commit or hand-edit |

Subdirectories:

| Path | What | Produced by |
| :--- | :--- | :--- |
| `scenes/` | Per-scene workspaces (`latest.json`, `review.json`, `models/`, imagery) — the core review corpus | scene capture/extract tasks (`scripts.ml.scene_*`) |
| `scene-batches/` | Named batches of scenes for bulk runs | `scripts.ml.scene_batch` |
| `scene-parcels/` | Frozen parcel snapshots keyed by content hash (with `.lock` files) | parcel fetch in scene/address tasks |
| `scene-parcel-alignment/` | Per-scene parcel alignment results | `scripts.ml.parcel_align` |
| `scene-self-reviews/` | Self-review run outputs | `scripts.ml.scene_self_review` |
| `suburbs/` | Per-suburb run state: boundary, active.json, confidence.sqlite, capture imagery | suburb expansion tasks (`scripts/ml/after_house_expansion.sh` etc.) |
| `imagery/` | Teacher/ortho imagery caches per source. **Only `provenance.json` is tracked** | imagery fetch tasks; `task eyes:teacher-*` |
| `elevation/` | Elevation sources; `linz-chc-1m/` is the LINZ 1 m DSM/DEM tile cache (CC BY 4.0, sha256-verified vs STAC) | `task eyes:teacher-elevation` (`raw/`, `derived/`, `depth/` hold other fetched rasters) |
| `reviews/` | Review outputs: performance reports, contact sheets, eyes/ ledgers, edit diffs | `task perf`, `task eyes:*`, review-cycle tasks |
| `references/` | Human/agent reference bundles: `eyes-checkpoints/CP*`, roof-checkpoint snapshots, probe images | checkpoint publish (`task eyes:checkpoint`) and eyes tasks |
| `cohorts/` | Named scene cohorts (`roof_training_250.json`, `suburban_benchmark.json`) | curated lists consumed by eval/training tasks |
| `experiments/` | Experiment run dirs (config + report + per-scene results) | `scripts.ml.scene_experiment` |
| `training/` | Exported training datasets (corrections.jsonl, images, splits) | `task training:export`, `scripts.ml.export_training` |
| `roof-review-loop/` | Roof review loop state (active.json, inspections, equipment) | `scripts.ml.roof_review_loop` |
| `address-evidence/` | Address-verification evidence (Vicmap joins, decision logs) | `scripts.ml.address_*` |
| `annotation-snapshots/` | Frozen annotation mirrors | `scripts/annotation_snapshot.py` |
| `fieldwork-sync/` | Push receipts/mirrors for the hosted Fieldwork service | `task fieldwork:*` |
| `sam3-review/` | SAM3 proposal previews + report | `task sam3:review` (`scripts.ml.sam3_review --out`) |
| `harness/` | Harness² case snapshots and runs | `task harness:*` |
| `previews/` | Tile-preview renders | `task tiles -- --preview-dir data/previews` |

Regeneration: every row above is pipeline output. The exact producer is the
`task` line in `Taskfile.yml` — search the desc fields for the directory name.
Nothing under `data/` needs a backup beyond the pipelines that made it; the
exception is human review effort inside `data/scenes/*/review.json` and
`data/references/` — export those (`task fieldwork:review:pull`, checkpoint
publish) before wiping.

## Model weights

| Location | What | How to get |
| :--- | :--- | :--- |
| `scripts/ml/models/*.onnx` | Segmentation/depth weights (dinov3s_buildings, geobase_unet, segformer_b0, depth_anything_v2_small, sam3 int8) | `task models -- --presets hot-buildings` or `scripts.ml.fetch_models` |
| `scripts/ml/models/sam3/` | SAM3 tokenizer/config — tracked; weights live alongside as ignored `*.onnx`/`*.data` | `fetch_models` sam3 preset |
| `scripts/ml/models/sam3-hf/` | Gated Hugging Face SAM3 snapshot (requires HF auth) | `scripts/ml/training/sam3_mac/fetch_weights.sh`; pin + license notes in `scripts/ml/training/sam3_mac/VENDOR.json` |
| `scripts/ml/training/vendor/` | Vendored SAM3 fork + local patches (`sam3_mac/patches/`) | `sam3_mac` setup scripts; pin in `VENDOR.json` |
| `data/training/`, `data/harness/` | Local fine-tune/eval outputs (never raw weights in git) | training tasks |

## Binaries

| Path | Rebuild |
| :--- | :--- |
| `bin/geoserver` | `task build` (`go build -trimpath -o bin/geoserver ./cmd/server`) |
| `bin/annotate`, `bin/annotation-migrate` | `go build -o bin/<name> ./cmd/<name>` |
| `crmctl`, `annotationctl`, `annotation-server` | `go build -o <name> ./cmd/<name>` (usually run via `go run` / task) |

## Checking what's ignored

`git status --ignored` shows what each rule catches. To confirm a new dump
directory is covered: `git check-ignore -v data/<dir>/somefile`.
