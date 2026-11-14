"""Task integration contracts. Uses real Task and fake Go/Kaggle-facing commands.

No OAuth, network access, real Kaggle client, or compute submission is performed.
Run from the repo: task rooftam:test:tasks. Requires Task v3 on PATH.
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
ROOT_INCLUDE = "includes:\n  rooftam: ./Taskfile.rooftam.yml\n\n"
ACTIONS = (
    "setup", "auth", "doctor", "prepare", "run", "submit", "status",
    "logs", "wait", "download", "resume", "verify",
)
FAKE = '''#!{python}
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["ROOFTAM_TEST_LOG"], "a") as stream:
    stream.write(json.dumps(dict(command=name, args=sys.argv[1:], cwd=os.getcwd(),
                                gowork=os.environ.get("GOWORK"),
                                dotenv=os.environ.get("ROOFTAM_TEST_ENV")))+"\\n")
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
        shutil.copy(ROOT / "Taskfile.rooftam.yml", self.root / "Taskfile.rooftam.yml")
        original = (ROOT / "Taskfile.yml").read_text().replace(ROOT_INCLUDE, "", 1)
        (self.root / "Taskfile.original.yml").write_text(original)
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

    def invoke(self, task, *args, entry="root", dry=False, exit_code=False):
        command = [self.task]
        if entry == "file":
            command += ["--taskfile", "Taskfile.rooftam.yml"]
        if dry:
            command += ["--dry"]
        if exit_code:
            command += ["--exit-code"]
        command += [task if entry == "file" else "rooftam:" + task]
        if args:
            command += ["--", *args]
        return subprocess.run(command, cwd=self.module if entry == "nested" else self.root,
                              text=True, capture_output=True, env=self.env, timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_existing_root_task_names_and_descriptions_unchanged(self):
        def tasks(path):
            result = subprocess.run([self.task, "--dir", str(self.root), "--taskfile", str(self.root / path),
                                     "--list-all", "--json"], text=True, capture_output=True,
                                    env=self.env, timeout=60)
            self.assert_ok(result)
            return {item["name"]: item.get("desc", "") for item in json.loads(result.stdout)["tasks"]}
        original = tasks("Taskfile.original.yml")
        current = tasks("Taskfile.yml")
        self.assertEqual(original, {name: desc for name, desc in current.items()
                                    if not name.startswith("rooftam")})
        for action in (*ACTIONS, "build", "check", "build:all", "clean"):
            self.assertIn("rooftam:" + action, current)
        self.assertEqual(self.calls(), [])

    def test_existing_root_build_commands_unchanged(self):
        for task in ("build:server", "build:web", "test:go"):
            with self.subTest(task=task):
                outputs = []
                for filename in ("Taskfile.original.yml", "Taskfile.yml"):
                    result = subprocess.run([self.task, "--dir", str(self.root), "--taskfile", str(self.root / filename),
                                             "--dry", task], text=True, capture_output=True,
                                            env=self.env, timeout=60)
                    self.assert_ok(result)
                    outputs.append(result.stdout + result.stderr)
                self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(self.calls(), [])

    def test_only_root_level_taskfiles(self):
        self.assertTrue((ROOT / "Taskfile.rooftam.yml").is_file())
        self.assertFalse((HERE / "Taskfile.yml").exists())
        self.assertEqual((ROOT / "Taskfile.yml").read_text().count(ROOT_INCLUDE), 1)
        self.assertNotIn("dotenv:", (ROOT / "Taskfile.rooftam.yml").read_text())

    def test_root_and_standalone_defaults_have_no_side_effects(self):
        for entry in ("root", "file", "nested"):
            with self.subTest(entry=entry):
                result = self.invoke("default", entry=entry)
                self.assert_ok(result)
                self.assertIn("onboard", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_build_dry_runs_match_across_entrypoints(self):
        for name in ("build", "test", "build:all"):
            with self.subTest(task=name):
                outputs = []
                for entry in ("root", "file", "nested"):
                    result = self.invoke(name, entry=entry, dry=True)
                    self.assert_ok(result)
                    outputs.append((result.stdout + result.stderr).replace("[rooftam:", "["))
                self.assertEqual(outputs[0], outputs[1])
                self.assertEqual(outputs[0], outputs[2])
        self.assertEqual(self.calls(), [])

    def test_root_dotenv_loads_for_included_tasks(self):
        (self.root / ".env.local").write_text("ROOFTAM_TEST_ENV=local-value\n")
        for entry in ("root", "nested"):
            self.log.unlink(missing_ok=True)
            self.assert_ok(self.invoke("doctor", entry=entry))
            self.assertEqual(self.calls()[-1]["dotenv"], "local-value")

    def assert_module_cwd(self, cwd):
        # getcwd() may resolve symlinks that TemporaryDirectory retains.
        # Check filesystem identity, not spelling; a different directory fails.
        self.assertTrue(Path(cwd).samefile(self.module),
                        f"Working directory {cwd!r} is not the module directory {str(self.module)!r}")

    def check_actions(self):
        for entry in ("root", "file", "nested"):
            for action in ACTIONS:
                with self.subTest(entry=entry, action=action):
                    self.log.unlink(missing_ok=True)
                    args = ["--run", "./runs/path with spaces", "--kaggle", "/tmp/client with spaces"]
                    self.assert_ok(self.invoke(action, *args, entry=entry))
                    calls = self.calls()
                    self.assertEqual([c["command"] for c in calls], ["go", "rooftam"])
                    self.assertEqual(calls[-1]["args"], [action, *args])
                    for call in calls:
                        self.assert_module_cwd(call["cwd"])
                    self.assertEqual(calls[0]["gowork"], "off")

    def test_every_cli_action_routes_from_all_root_entrypoints(self):
        self.check_actions()

    def test_every_cli_action_routes_through_symlinked_root(self):
        # Reproduce aliased temporary directories (macOS /var vs /private/var)
        # on any POSIX runner. Keep the alias unresolved in the expected path.
        alias = self.root / "repository alias"
        alias.symlink_to(self.root.resolve(), target_is_directory=True)
        self.root = alias
        self.module = alias / "tools" / "rooftam"
        self.assertNotEqual(str(self.module), str(self.module.resolve()))
        self.check_actions()

    def test_working_directory_check_rejects_wrong_directory(self):
        other = self.root / "wrong working directory"
        other.mkdir()
        alias = self.root / "wrong directory alias"
        alias.symlink_to(other.resolve(), target_is_directory=True)
        for cwd in (other, alias):
            with self.subTest(cwd=str(cwd)):
                with self.assertRaises(AssertionError):
                    self.assert_module_cwd(str(cwd))

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
