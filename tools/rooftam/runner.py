"""Embedded remote orchestrator; CONFIG is supplied as JSON by the Go CLI.

Uses stdlib in Kaggle's host interpreter; ML runs in bootstrap's venv.
Only a run archive is placed in /kaggle/working, not the multi-GB environment.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import traceback
import zipfile


def execute(config):
    root = Path(tempfile.mkdtemp(prefix="addr-rooftam-"))
    source, runtime, run = root / "source", root / "runtime", root / "artifacts"
    run.mkdir()
    output = Path("/kaggle/working")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ("rooftam-" + config["run_id"] + ".zip")
    if archive.exists():
        raise RuntimeError("Run archive already exists; do not reuse a submitted run")
    receipt = {"schema_version": 1, "run_id": config["run_id"],
               "addr_commit": config["commit"], "config_sha256": config["config_sha256"],
               "kernel": config["kernel"], "status": "running", "stages": []}

    def command(stage, args, cwd=None, env=None):
        print("Stage:", stage, flush=True)
        # Never dump credentials or the environment.
        with (run / (stage + ".log")).open("w", encoding="utf-8") as log:
            process = subprocess.Popen([str(x) for x in args], cwd=cwd, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace")
            for line in process.stdout:
                log.write(line)
                print(line, end="", flush=True)
            code = process.wait()
        receipt["stages"].append({"name": stage, "exit_code": code})
        if code:
            raise RuntimeError("Stage %s exited %s; see its log" % (stage, code))

    try:
        command("git-init", ["git", "init", source])
        command("git-origin", ["git", "-C", source, "remote", "add", "origin",
                               "https://github.com/castlemilk/addr.git"])
        command("git-fetch", ["git", "-C", source, "fetch", "--depth", "1", "origin", config["commit"]])
        command("git-checkout", ["git", "-C", source, "checkout", "--detach", "FETCH_HEAD"])
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if revision != config["commit"]:
            raise RuntimeError("Checked-out revision differs from requested commit")
        (run / "addr-revision.txt").write_text(revision + "\n")
        command("bootstrap", ["bash", "experiments/rooftam/kaggle/bootstrap.sh", "tpu"], cwd=source,
                env={**os.environ, "ROOFTAM_WORK": str(runtime)})
        python = runtime / "venv/bin/python"
        checkpoint = (runtime / "checkpoint-path.txt").read_text().strip()
        shutil.copyfile(runtime / "resolved-requirements.txt", run / "resolved-requirements.txt")
        command("tests", [python, "-m", "pytest", "-q", "experiments/rooftam/tests",
                          "--junitxml=" + str(run / "tests.xml")], cwd=source)

        def bench(stage, action, *args):
            command(stage, [python, "-m", "experiments.rooftam.bench", action,
                            "--output", run / stage, *args], cwd=source)

        bench("01-preflight", "preflight", "--device", "tpu")
        bench("fixture", "fixture", "--seed", config["seed"])
        fixture = str(run / "fixture/manifest.jsonl")
        shared = ["--manifest", fixture, "--allow-synthetic", "--device", "tpu", "--seed", config["seed"]]
        bench("02-validation", "validate", *shared)
        bench("03-tiny-control", "train", *shared, "--model", "tiny", "--side", "64", "--steps", "3")
        edge = shared + ["--model", "edgetam", "--side", "1024", "--checkpoint", checkpoint]
        bench("04-edgetam-before", "evaluate", *edge)
        bench("05-edgetam-smoke", "train", *edge, "--steps", "3", "--batch-size", config["batch_size"])
        bench("06-edgetam-after", "evaluate", *edge, "--adapter", run / "05-edgetam-smoke/adapter.pt")
        bench("07-smoke-comparison", "compare", "--reports", run / "04-edgetam-before/evaluation.json",
              run / "06-edgetam-after/evaluation.json")
        if config["manifest"]:
            real = ["--manifest", config["manifest"], "--device", "tpu", "--model", "edgetam",
                    "--side", "1024", "--checkpoint", checkpoint, "--seed", config["seed"], "--split", "val"]
            bench("10-real-validation", "validate", *real)
            bench("11-real-before", "evaluate", *real)
            bench("12-real-train", "train", *real, "--steps", config["real_steps"], "--batch-size", config["batch_size"])
            bench("13-real-after", "evaluate", *real, "--adapter", run / "12-real-train/adapter.pt")
            reports = [run / "11-real-before/evaluation.json", run / "13-real-after/evaluation.json"]
            if config["sam3_predictions"]:
                bench("14-sam3", "score-export", "--manifest", config["manifest"], "--split", "val",
                      "--predictions", config["sam3_predictions"])
                reports.append(run / "14-sam3/evaluation.json")
            bench("15-real-comparison", "compare", "--reports", *reports)
        receipt["status"] = "passed"
    except BaseException:
        receipt["status"] = "failed"
        (run / "orchestrator-failure.txt").write_text(traceback.format_exc())
        raise
    finally:
        # A hard VM termination may prevent this best-effort failure packaging.
        artifacts = {}
        for p in sorted(run.rglob("*")):
            if p.is_symlink():
                raise RuntimeError("Refusing symlink in run artifacts")
            if p.is_file():
                h = hashlib.sha256()
                with p.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        h.update(chunk)
                artifacts[p.relative_to(run).as_posix()] = h.hexdigest()
        receipt["artifacts"] = artifacts
        (run / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as z:
            for p in sorted(run.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(run).as_posix())
        print("Private run archive:", archive, flush=True)
        print("Synthetic results are plumbing only. Review XLA fallbacks before performance claims.", flush=True)


execute(CONFIG)
