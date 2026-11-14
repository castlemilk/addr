"""Task integration contracts. Uses real Task and fake Go/Kaggle-facing commands.

No OAuth, network access, real Kaggle client, or compute submission is performed.
Run from the repo: task --dir tools/rooftam test:tasks. Requires Task v3 on PATH.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ACTIONS = (
    "setup", "auth", "doctor", "prepare", "run", "submit", "status",
    "logs", "wait", "download", "resume", "verify",
)
FAKE = '''#!{python}
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["ROOFTAM_TEST_LOG"], "a") as stream:
    stream.write(json.dumps(dict(command=name, args=sys.argv[1:], cwd=os.getcwd(),
                                gowork=os.environ.get("GOWORK")))+"\\n")
sys.exit(int(os.environ.get("ROOFTAM_TEST_GO_EXIT" if name == "go" else "ROOFTAM_TEST_EXIT", "0")))
'''


class TaskfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task = shutil.which("task")
        if cls.task is None:
            raise RuntimeError("Install Task v3 to execute Taskfile integration tests")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rooftam task tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = self.root / "tools" / "rooftam"
        self.module.mkdir(parents=True)
        shutil.copy(ROOT / "Taskfile.yml", self.root / "Taskfile.yml")
        shutil.copy(HERE / "Taskfile.yml", self.module / "Taskfile.yml")
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        for target in (fake_bin / "go", self.module / "rooftam"):
            target.write_text(FAKE.format(python=sys.executable))
            target.chmod(0o755)
        self.log = self.root / "calls.jsonl"
        self.env = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                    "ROOFTAM_TEST_LOG": str(self.log)}
        # Do not inherit caller Task mode or experimental variable precedence.
        for key in tuple(self.env):
            if key.startswith("TASK_"):
                del self.env[key]

    def invoke(self, task, *args, local=False, dry=False, exit_code=False):
        command = [self.task] if local else [self.task, "--dir", "tools/rooftam"]
        if dry:
            command += ["--dry"]
        if exit_code:
            command += ["--exit-code"]
        command += [task]
        if args:
            command += ["--", *args]
        return subprocess.run(command, cwd=self.module if local else self.root,
                              text=True, capture_output=True, env=self.env, timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_module_task_list_and_default_have_no_side_effects(self):
        result = subprocess.run([self.task, "--dir", str(self.module), "--list-all", "--json"],
                                text=True, capture_output=True, env=self.env, timeout=60)
        self.assert_ok(result)
        names = {item["name"] for item in json.loads(result.stdout)["tasks"]}
        for action in (*ACTIONS, "build", "check", "build:all", "clean"):
            self.assertIn(action, names)
        self.assert_ok(self.invoke("default"))
        self.assertEqual(self.calls(), [])

    def test_root_dir_and_module_build_commands_match(self):
        for task in ("build", "test", "build:all"):
            with self.subTest(task=task):
                root = self.invoke(task, dry=True)
                module = self.invoke(task, local=True, dry=True)
                self.assert_ok(root)
                self.assert_ok(module)
                self.assertEqual(root.stdout + root.stderr, module.stdout + module.stderr)
        self.assertEqual(self.calls(), [])

    def check_actions(self, local):
        for action in ACTIONS:
            with self.subTest(local=local, action=action):
                self.log.unlink(missing_ok=True)
                args = ["--run", "./runs/path with spaces", "--kaggle", "/tmp/client with spaces"]
                self.assert_ok(self.invoke(action, *args, local=local))
                calls = self.calls()
                self.assertEqual([c["command"] for c in calls], ["go", "rooftam"])
                self.assertEqual(calls[-1]["args"], [action, *args])
                self.assertEqual(calls[-1]["cwd"], str(self.module))
                self.assertEqual(calls[0]["gowork"], "off")

    def test_every_cli_action_routes_from_repo_root(self):
        self.check_actions(local=False)

    def test_every_cli_action_routes_from_module(self):
        self.check_actions(local=True)

    def test_arguments_are_not_reinterpreted_as_shell_commands(self):
        payload = "user$(touch SHOULD_NOT_EXIST); echo nope"
        self.assert_ok(self.invoke("prepare", "--owner", payload, "--output", "runs/one's roof"))
        self.assertEqual(self.calls()[-1]["args"], ["prepare", "--owner", payload,
                                                  "--output", "runs/one's roof"])
        self.assertFalse((self.module / "SHOULD_NOT_EXIST").exists())

    def test_onboard_is_ordered_and_never_submits(self):
        self.assert_ok(self.invoke("onboard"))
        calls = self.calls()
        self.assertEqual([c["args"][0] for c in calls if c["command"] == "rooftam"],
                         ["setup", "auth", "doctor"])
        self.assertEqual(sum(c["command"] == "go" for c in calls), 1)

    def test_failure_stops_onboarding(self):
        self.env["ROOFTAM_TEST_EXIT"] = "1"
        result = self.invoke("onboard")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c["args"][0] for c in self.calls() if c["command"] == "rooftam"], ["setup"])

    def test_failed_build_never_calls_cli(self):
        self.env["ROOFTAM_TEST_GO_EXIT"] = "1"
        self.assertNotEqual(self.invoke("run", "--owner", "testuser").returncode, 0)
        self.assertEqual([c["command"] for c in self.calls()], ["go"])

    def test_review_exit_code_is_preserved_with_exit_code_flag(self):
        self.env["ROOFTAM_TEST_EXIT"] = "3"
        result = self.invoke("verify", "--run", "runs/existing", exit_code=True)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_dry_run_does_not_build_or_submit(self):
        self.assert_ok(self.invoke("run", "--owner", "testuser", "--output", "runs/preview", dry=True))
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.module / "runs").exists())

    def test_clean_preserves_saved_runs_credentials_and_unknown_files(self):
        preserved = ["runs/first/state.json", "rooftam-runs/second/receipt.json", "dist/keep.txt",
                     ".config/kaggle/access_token", ".venv/keep", "runner.py", "submission.log"]
        removed = ["rooftam", "rooftam.exe", "coverage.out", "dist/SHA256SUMS"]
        removed += ["dist/rooftam-" + osname + "-" + arch
                    for osname in ("linux", "darwin") for arch in ("amd64", "arm64")]
        for name in (*preserved, *removed):
            path = self.module / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep-or-delete fixture")
        self.assert_ok(self.invoke("clean"))
        for name in preserved:
            self.assertTrue((self.module / name).exists(), name)
        for name in removed:
            self.assertFalse((self.module / name).exists(), name)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
