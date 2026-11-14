# RoofTAM Go CLI

End-to-end controller for `experiments/rooftam`: install the Kaggle client,
authenticate, prepare a pinned notebook, submit a private TPU run, wait, download
artifacts and verify the execution evidence. Go owns orchestration; the existing
Python/PyTorch-XLA code remains the model runtime. This does not start addr's
production web/geo services or promote trained weights.

## Root-level Task workflow

Builds, checks, onboarding and the complete experiment lifecycle are available
through [Task](TASKS.md). Both Taskfiles live at the repository root:
`Taskfile.yml` includes [Taskfile.rooftam.yml](../../Taskfile.rooftam.yml) as `rooftam:`.

```sh
# From the addr repository root; no cd or --dir needed:
task rooftam:build
task rooftam:check
task rooftam:onboard
task --exit-code rooftam:run -- \
  --owner YOUR_KAGGLE_USERNAME --output ./runs/first-tpu

task rooftam:status -- --run ./runs/first-tpu
task --exit-code rooftam:resume -- --run ./runs/first-tpu
```

Relative run paths continue to resolve inside `tools/rooftam`, so saved runs do
not move. Existing addr service tasks and root dotenv loading are preserved.
Build/check never authenticate or allocate compute; `rooftam:clean` removes
only known controller build outputs, not saved runs or credentials.
See [TASKS.md](TASKS.md) for all commands, standalone usage and recovery examples.
Direct CLI use below remains supported; Task is not required for the Go binary.

## First run

Requires Go 1.23+ to build, Python 3.11+ with `venv` for the official Kaggle client,
Internet, and a verified Kaggle account with available TPU quota. No local
PyTorch/CUDA/model installation or ChatGPT plugin is required.

```sh
# From the addr repository:
cd tools/rooftam
go build -o rooftam .
./rooftam setup
./rooftam auth
./rooftam doctor
./rooftam run --owner YOUR_KAGGLE_USERNAME --output ./runs/first-tpu
```

Complete Kaggle's OAuth consent in the browser when `auth` requests it. The CLI
never extracts or saves credentials in a notebook, run directory or repository.
Existing credentials supported by the official client can also be used. Headless
login: `rooftam auth --no-launch-browser`. Never put an API token in chat or Git.

`setup` installs official Kaggle 2.2.4 into an isolated virtual environment under
the OS user-config directory at `addr-rooftam/kaggle-2.2.4`; it does not replace
system packages. Use `--python /path/to/python3` for setup or
`--kaggle /path/to/kaggle` / `ROOFTAM_KAGGLE_BIN` for an existing client.

`run` prepares, submits, waits, downloads and verifies. It requests `TpuV5E8`,
limits remote execution to 2 hours, and waits locally for up to 3 hours including
queueing. These are safety bounds, not runtime estimates or guaranteed quota.
Override with `--max-runtime`, `--timeout`, `--poll` or an available TPU model via
`--accelerator`. Account verification and actual hardware allocation remain
controlled by Kaggle. CPU/GPU substitution is not accepted as TPU evidence.

## What runs remotely

The CLI resolves `--ref main` to an immutable public addr commit. Each run gets a
unique private notebook with matching title/slug; old notebooks are not overwritten.
The generated notebook contains configuration and a stdlib orchestrator, not secrets.

1. Fetch the exact addr revision, run its isolated TPU bootstrap and Python tests.
2. Verify real TPU arithmetic/gradients, then run a tiny synthetic training control.
3. Evaluate pretrained EdgeTAM, train its mask decoder for three steps, reload the
   saved adapter and compare before/after validation outputs.
4. Optionally train on attached human-reviewed roof data and compare a matching
   attached SAM3 prediction export. The existing bench trains only on train and
   evaluates val; it does not automatically tune on test.
5. Package logs, metrics, overlays, weights, dependency versions and a run/config
   receipt with a SHA-256 manifest of the artifacts.

Source, runtime and pretrained weights use temporary session storage. Only the
result ZIP is saved under `/kaggle/working`, rather than a multi-GB environment.
Normal Python failures still package partial evidence and a traceback. A hard VM
kill/quota termination can prevent packaging; missing evidence is never success.

## Prepare, detach and resume

```sh
# Prepare only: no Kaggle write or compute allocation.
./rooftam prepare --owner YOUR_KAGGLE_USERNAME --output ./runs/review
# Inspect upload/kernel-metadata.json and upload/rooftam.ipynb, then:
./rooftam submit --run ./runs/review
./rooftam status --run ./runs/review
./rooftam logs --run ./runs/review
./rooftam resume --run ./runs/review
```

`submit` performs one write and returns. `resume` only polls/downloads/verifies;
it never submits again. `wait`, `download` and `verify` are also standalone
commands; `verify` is offline and needs neither Kaggle credentials nor Python.

**Ctrl-C or a local timeout stops waiting, not the remote job.** Resume the same
run directory afterward. Do not start another `run` merely to reconnect: that
explicitly creates another job and can consume additional quota.

Submission network errors become `submission-unknown`, never an automatic retry:
Kaggle might already have accepted the write. Resume reconciles the unique notebook
using reads. If Kaggle confirms it never existed, prepare a new run deliberately.
A `.lock` prevents concurrent controllers; remove it only after confirming no
other controller is active. Prepared upload hashes prevent unnoticed changes.

## Real roof data and SAM3

Attach a private dataset with the manifest and reviewed/licensed RGB crops and
binary masks described in [the benchmark data contract](../../experiments/rooftam/README.md#real-data-contract).
The CLI does not upload local property files, create datasets, accept licence terms,
or promote agent annotations to approved human labels.

```sh
./rooftam run --owner YOUR_KAGGLE_USERNAME --output ./runs/real-pilot \
  --dataset YOUR_KAGGLE_USERNAME/reviewed-roofs \
  --manifest /kaggle/input/reviewed-roofs/manifest.jsonl \
  --allow-real-data --real-steps 100 --batch-size 1
```

For SAM3, attach its existing export with another `--dataset` and set
`--sam3-predictions /kaggle/input/BASELINE/predictions.json`. Produce that export
using `experiments.rooftam.export_sam3` in addr's CPU/GPU ML environment. ONNX SAM3
is not executed on TPU. The bench checks matching samples, label hashes and splits.
Dataset references attach Kaggle's selected version; retain the resulting content
hashes and manage dataset-version changes explicitly for reproducibility.

## Outputs and verdicts

```text
runs/first-tpu/
  state.json
  upload/kernel-metadata.json
  upload/rooftam.ipynb
  submission.log
  download.log
  downloads/rooftam-<run-id>.zip
  verification.json
```

Owner-only file/directory permissions are requested where supported. Keep outputs
private: real-data overlays expose imagery/masks and checkpoints contain trained
weights. Downloads are filtered to this run's ZIP plus the client's execution log.
Archives are inspected without extraction; traversal, symlink, duplicate and
excessive entries are rejected (2 GiB/file, 4 GiB uncompressed/run).

| Exit | Meaning |
| --- | --- |
| 0 | Command succeeded; run/resume/verify checked the execution evidence. |
| 1 | Failure or missing/invalid evidence. Inspect logs, not a passed run. |
| 3 | TPU/weight-update evidence passed, but XLA fallback counters need review. |

Kaggle `complete` is not enough. Verification checks run/config/commit identity,
artifact hashes, required stages, TPU device/revision, model type, actual weight
updates reported by the bench, checkpoint hashes, val split and dataset matching.
Synthetic output proves plumbing only, not real-roof accuracy. This is a
single-worker compatibility experiment, not distributed TPU performance, survey
accuracy, roof-structure inference or production promotion. Hashes provide artifact
integrity, not an independent hardware attestation. Review XLA fallback counters
before performance claims; cross-device SAM3 timings are not speedup evidence.

## Development

```sh
go test -race ./...
go vet ./...
go build -o rooftam .
```

This stdlib-only Go module is isolated from production dependencies. Tests cover
private/pinned generation, input validation, auth delegation, once-only submission,
unknown responses, cancellation/resume, artifact integrity, CPU rejection, missing
XLA diagnostics, stale adapters, and complete command lifecycles with a fake Kaggle
client. The actual embedded runner's Python syntax and failure packaging are tested
without ML dependencies. These are not live Kaggle/TPU execution receipts.
CI builds Linux/macOS amd64/arm64 controllers and never launches Kaggle compute.

Primary contracts checked during implementation:
- https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md
- https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md
- https://github.com/Kaggle/kaggle-cli/blob/main/skills/references/auth.md

The official client handles authentication/API details rather than duplicating
OAuth token storage or depending on undocumented HTTP endpoints.
