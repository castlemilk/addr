# RoofTAM through Task

Use **Task v3** (`task`; CI pins 3.52.0), Go 1.23+, and Python 3.11+ with venv.
Task is available from https://taskfile.dev/docs/installation; on macOS use
`brew install go-task`. Task is an entrypoint to the existing Go controller,
not a second orchestration implementation.

## Build and onboard

Run these from the **addr repository root**:

```sh
task --dir tools/rooftam build
task --dir tools/rooftam check                 # lint, race tests, Task integration tests

task --dir tools/rooftam onboard               # setup -> OAuth login -> doctor; no job launch
# Or perform the same steps individually:
task --dir tools/rooftam setup -- --python python3
task --dir tools/rooftam auth -- --no-launch-browser
task --dir tools/rooftam doctor
```

`onboard` is sequential and stops on failure. Use the individual tasks when you
need flags specific to one step. Credentials stay with the official Kaggle
client, outside the repository. Do not put secrets in Task arguments or files.
The setup/auth/doctor tasks are never dependencies of build, check or run.

## Start one experiment

```sh
# Show the CLI's flags:
task --dir tools/rooftam help

# Inspect the commands without building, authenticating or submitting:
task --dir tools/rooftam --dry run -- --owner YOUR_KAGGLE_USERNAME --output ./runs/first-tpu

# Submit, wait, retrieve and verify one private experiment:
task --dir tools/rooftam --exit-code run -- \
  --owner YOUR_KAGGLE_USERNAME --output ./runs/first-tpu
```

Each CLI task automatically builds the native controller first. Go's compilation
cache makes repeated builds inexpensive while keeping changes to the embedded
`runner.py`, compiler, build flags and host architecture effective.

**Relative paths resolve inside `tools/rooftam`, even from the repository root.**
The example saves to `tools/rooftam/runs/first-tpu`. An absolute path also works.
Existing run-directory protection, once-only submission and private/synthetic
first-run defaults are unchanged. Passing `--` forwards CLI flags and quoted
paths; there are no duplicate Task-level owner, dataset, accelerator or run defaults.
All existing CLI options remain available, including repeated `--dataset`,
`--ref`, `--accelerator`, `--timeout`, `--poll`, and `--max-runtime`.

## Detached submission and recovery

```sh
task --dir tools/rooftam prepare -- --owner YOUR_KAGGLE_USERNAME --output ./runs/review
# Review tools/rooftam/runs/review/upload before deliberately submitting it:
task --dir tools/rooftam submit -- --run ./runs/review

task --dir tools/rooftam status -- --run ./runs/review
task --dir tools/rooftam logs -- --run ./runs/review
task --dir tools/rooftam wait -- --run ./runs/review --timeout 3h
task --dir tools/rooftam download -- --run ./runs/review
task --dir tools/rooftam --exit-code verify -- --run ./runs/review
# Or reconnect, wait, download and verify in one step (never resubmits):
task --dir tools/rooftam --exit-code resume -- --run ./runs/review
```

`run` and `submit` are the only tasks that submit a Kaggle job. `prepare` reads
GitHub and creates a local package; it does not allocate compute. `verify` is
local/offline after Go is available. Closing the terminal, interrupting Task,
cleaning builds or reaching the local timeout does **not** cancel remote compute.
Use `resume` with the same directory, not a new `run`, to reconnect.

Use Task's **`--exit-code`** flag when automating run/resume/verify so the CLI's
review-required exit code **3** is preserved; without it Task still fails but may
translate the child exit code. Exit 3 is not an unconditional pass. See
[CLI outputs and verdicts](README.md#outputs-and-verdicts).

## Real roof training

```sh
task --dir tools/rooftam --exit-code run -- \
  --owner YOUR_KAGGLE_USERNAME --output ./runs/real-pilot \
  --dataset YOUR_KAGGLE_USERNAME/reviewed-roofs \
  --manifest /kaggle/input/reviewed-roofs/manifest.jsonl \
  --allow-real-data --real-steps 100 --batch-size 1
```

Only attach approved, licensed data. These tasks do not upload local imagery or
promote agent annotations. SAM3 remains an optional pre-exported CPU/GPU baseline:
attach its dataset and pass `--sam3-predictions` as described in [the CLI guide](README.md#real-roof-data-and-sam3).

## Development and cleanup

```sh
task --dir tools/rooftam fmt                    # modifies Go formatting only
task --dir tools/rooftam lint
task --dir tools/rooftam test -- -run TestName  # optional Go test filter
task --dir tools/rooftam test:tasks            # fake lifecycle commands; no Kaggle requests
task --dir tools/rooftam build:all             # four Linux/macOS binaries + SHA256SUMS in dist/
task --dir tools/rooftam install               # optional Go install to GOBIN/GOPATH/bin
task --dir tools/rooftam clean                 # ONLY known controller binaries + coverage
```

`clean` never removes saved runs, receipts, downloads, logs, credentials or the
Kaggle environment. Unknown files in `dist/` remain untouched. It is distinct
from the pre-existing **root `task clean`**, which still cleans addr services.
Set `ROOFTAM_PYTHON=/path/to/python3` to choose Python for lint, Task tests and
release checksums. For Kaggle-client setup, use `--python` instead.

The same tasks work standalone: `task --dir tools/rooftam build` or, from that
directory, `task run -- --owner ...`. The existing root `Taskfile.yml` is unchanged: service lifecycle commands
remain separate from experiments. Integration tests exercise both entrypoints,
quoted arguments, dry runs, failure propagation and conservative cleanup. CI runs `task --dir tools/rooftam check` and
`task --dir tools/rooftam build:all`, never onboarding or submission.

Passing these checks is controller/Task evidence, not a live Kaggle or TPU run.
