import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
from test_core import STATUS, request
from core import sha


class TestCli(unittest.TestCase):
    def test_complete_cli_preview_apply_real_scanner_unicode(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            project = root / "notes" / "projects" / "Legacy_Project"
            project.mkdir(parents=True)
            status = project / "status.md"
            status.write_bytes(STATUS)
            runtime = root / "runtime"
            runtime.mkdir()
            policy = root / "policy.json"
            policy.write_text(
                json.dumps(
                    {"schemaVersion": "1.0", "paths": {"personalPathSeverity": "high"}}
                ),
                encoding="utf-8",
            )
            config = {
                "gitleaks": shutil.which("gitleaks") or "missing",
                "policy": str(policy),
                "gitleaks_config": str(SCRIPTS.parents[2] / ".gitleaks.toml"),
            }
            req = request()
            req["actor_role"] = "pm"
            req["classification"] = "unmigrated_git"
            req["payload"]["base_sha256"] = sha(STATUS)
            req["payload"]["fields"]["now"] = "實際合成 🧪"
            env = {
                "notes_root": str(root / "notes"),
                "runtime_dir": str(runtime),
                "privacy_config": config,
                "request": req,
            }

            def call(command):
                result = subprocess.run(
                    [sys.executable, str(SCRIPTS / "workspace_records.py"), command],
                    input=json.dumps(env, ensure_ascii=True),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                )
                self.assertTrue(result.stdout.isascii())
                return result

            self.assertEqual(call("initialize").returncode, 0)
            planned = call("preview")
            self.assertEqual(planned.returncode, 0, planned.stdout)
            data = json.loads(planned.stdout)["data"]
            env["plan"] = data["plan"]
            env["plan_hash"] = data["plan_hash"]
            completed = call("apply")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("實際合成 🧪", status.read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads(completed.stdout)["data"]["outcome"], "complete"
            )
            self.assertEqual(
                json.loads(call("preview").stdout)["data"]["outcome"], "no_write"
            )

    def test_malformed_input_has_ascii_error_and_no_traceback(self):
        run = subprocess.run(
            [sys.executable, str(SCRIPTS / "workspace_records.py"), "resolve"],
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(run.returncode, 2)
        self.assertFalse(json.loads(run.stdout)["ok"])
        self.assertEqual(run.stderr, "")

    def test_ascii_stdout_and_git_initialization(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            project = root / "notes" / "projects" / "Legacy_Project"
            project.mkdir(parents=True)
            (project / "status.md").write_bytes(STATUS)
            runtime = root / "runtime"
            runtime.mkdir()
            req = request()
            req["actor_role"] = "pm"
            req["classification"] = "unmigrated_git"
            env = {
                "notes_root": str(root / "notes"),
                "runtime_dir": str(runtime),
                "request": req,
            }

            def call(cmd):
                return subprocess.run(
                    [sys.executable, str(SCRIPTS / "workspace_records.py"), cmd],
                    input=json.dumps(env, ensure_ascii=True),
                    text=True,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=20,
                )

            first = call("initialize")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(json.loads(first.stdout)["ok"])
            self.assertTrue(first.stdout.isascii())
            read = call("read")
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertTrue(read.stdout.isascii())
            result = json.loads(read.stdout)
            self.assertEqual(
                result["data"]["records"][0]["source"]["original_time"],
                "2026-01-01T12:00:00+08:00",
            )
            env["request"]["actor_role"] = "reader"
            env["request"]["action"] = "freeze"
            denied = call("preview")
            self.assertNotEqual(denied.returncode, 0)
            self.assertFalse(json.loads(denied.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
