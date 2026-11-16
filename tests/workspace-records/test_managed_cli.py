"""Real isolated fixed-package CLI; no live registry, Google, or credentials."""
import copy
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_core import STATUS, request
import git_store
import core

REPO = pathlib.Path(__file__).resolve().parents[2]


class TestManagedCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.project = self.root / "project-notes/projects/Legacy_Project"
        self.project.mkdir(parents=True)
        self.status = self.project / "status.md"
        self.status.write_bytes(STATUS)
        self.package = self.root / ".openchamber/workspace-records/package"
        self.scripts = self.package / "skills/workspace-records/scripts"
        self.scripts.mkdir(parents=True)
        for name in ("core.py", "git_store.py", "privacy.py", "routing.py", "workspace_records.py", "recovery.py"):
            shutil.copyfile(REPO / "skills/workspace-records/scripts" / name, self.scripts / name)
        scanner = self.package / "skills/repo-privacy-audit/scripts"
        scanner.mkdir(parents=True)
        shutil.copyfile(REPO / "skills/repo-privacy-audit/scripts/audit_core.py", scanner / "audit_core.py")
        shutil.copyfile(REPO / ".gitleaks.toml", self.package / ".gitleaks.toml")
        self.runtime_root = self.root / ".openchamber/workspace-records/runtime"
        self.runtime_root.mkdir()
        self.registry = self.root / ".openchamber/workspace-records/routes.json"
        self.routes = {"schema_version": 1, "policy_version": "1", "generation": 1,
                       "projects": {"Legacy_Project": {"classification": "unmigrated_git",
                       "backend": "git_local", "route_phase": "steady", "locator": None}}}
        self.save()
        (self.root / ".openchamber/repo-privacy-policy.json").write_text(json.dumps(
            {"schemaVersion": "1.0", "paths": {"personalPathSeverity": "high"}}), encoding="utf-8")
        self.req = request()
        import hashlib
        self.req["payload"]["base_sha256"] = hashlib.sha256(STATUS).hexdigest()
        self.req["payload"]["fields"]["now"] = "合成更新"
        # Deliberately untrusted paths/config must never become the managed target.
        self.env = {"request": self.req, "notes_root": str(self.root / "wrong-notes"),
                    "runtime_dir": str(self.root / "wrong-runtime"),
                    "privacy_config": {"gitleaks": "missing", "policy": "wrong", "gitleaks_config": "wrong"}}

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        self.registry.write_text(json.dumps(self.routes), encoding="utf-8")

    def call(self, command, env=None):
        run = subprocess.run([sys.executable, "-B", str(self.scripts / "workspace_records.py"),
                              command, "--managed"], input=json.dumps(env or self.env, ensure_ascii=True),
                             text=True, encoding="utf-8", capture_output=True, cwd=self.root.parent, timeout=30)
        self.assertTrue(run.stdout.isascii(), run.stdout)
        self.assertEqual(run.stderr, "")
        return run.returncode, json.loads(run.stdout)

    def initialize(self):
        code, result = self.call("initialize")
        self.assertEqual(code, 0, result)
        return pathlib.Path(result["data"]["routing_binding"]["runtime_directory"])

    def plan(self):
        code, result = self.call("preview")
        self.assertEqual(code, 0, result)
        self.env.update(plan=result["data"]["plan"], plan_hash=result["data"]["plan_hash"])
        return result["data"]

    def test_real_fixed_package_initialize_preview_apply_and_no_shadow_paths(self):
        runtime = self.initialize()
        planned = self.plan()
        self.assertIn("routing_binding", planned["plan"])
        code, result = self.call("apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["outcome"], "complete")
        self.assertIn("合成更新", self.status.read_text(encoding="utf-8"))
        self.assertTrue((runtime / "control.json").is_file())
        self.assertFalse((self.root / "wrong-runtime").exists())
        self.assertFalse((self.root / "wrong-notes").exists())
        self.assertEqual(self.call("preview")[1]["data"]["outcome"], "no_write")

    def test_registry_changes_after_preview_prevent_apply(self):
        self.initialize(); self.plan()
        self.routes["generation"] += 1; self.save()
        code, result = self.call("apply")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["error"]["code"], "REVISION_CONFLICT")
        self.assertEqual(self.status.read_bytes(), STATUS)

    def test_google_unknown_corrupt_never_initialize_or_fallback(self):
        entry = self.routes["projects"]["Legacy_Project"]
        entry.update(classification="managed", backend="google_head", locator={"document_id": "synthetic-head"})
        self.save()
        code, result = self.call("initialize")
        self.assertNotEqual(code, 0); self.assertEqual(result["error"]["code"], "STALE_MODE")
        self.assertEqual(list(self.runtime_root.iterdir()), [])
        self.env["request"] = copy.deepcopy(self.req); self.env["request"]["project_id"] = "Unknown"
        self.env["request"]["scope"]["project_id"] = "Unknown"
        self.assertNotEqual(self.call("inventory")[0], 0)
        self.registry.write_bytes(b"not-json")
        self.env["request"] = self.req
        self.assertNotEqual(self.call("initialize")[0], 0)
        self.assertEqual(list(self.runtime_root.iterdir()), [])
        self.assertEqual(self.status.read_bytes(), STATUS)

    def test_frozen_route_and_control_allow_receipts_but_no_new_write(self):
        runtime = self.initialize(); self.plan()
        code, completed = self.call("apply"); self.assertEqual(code, 0)
        control = json.loads((runtime / "control.json").read_text(encoding="utf-8"))
        control.update(phase="frozen", epoch=2)
        (runtime / "control.json").write_text(json.dumps(control), encoding="utf-8")
        self.routes["projects"]["Legacy_Project"]["route_phase"] = "restoring"; self.save()
        self.assertEqual(self.call("preview")[1]["data"]["outcome"], "no_write")
        self.assertEqual(self.call("apply")[1]["data"]["outcome"], "no_write")
        self.assertEqual(self.call("reconcile")[1]["data"]["result"], completed["data"]["result"])
        self.assertEqual(self.call("resolve")[1]["data"]["control"]["phase"], "frozen")
        self.req["operation_id"] = "new-operation"
        code, result = self.call("preview")
        self.assertNotEqual(code, 0); self.assertEqual(result["error"]["code"], "PHASE_FROZEN")

    def test_missing_package_scanner_cannot_use_request_supplied_config(self):
        self.initialize()
        (self.package / "skills/repo-privacy-audit/scripts/audit_core.py").unlink()
        code, result = self.call("preview")
        self.assertNotEqual(code, 0); self.assertEqual(result["error"]["code"], "SCAN_INCOMPLETE")
        self.assertEqual(self.status.read_bytes(), STATUS)

    def partial(self):
        tasks = self.project / "tasks"; tasks.mkdir()
        self.one = tasks / "one.md"; self.two = tasks / "two.md"
        self.one.write_bytes(b"one"); self.two.write_bytes(b"two")
        self.req.update(action="batch", target_record_id=None)
        self.req["scope"]["actions"] = ["batch", "replace"]
        self.req["scope"]["records"] = [{"record_id": name, "path": "tasks/" + name + ".md", "fields": ["body"]} for name in ("one", "two")]
        self.req["payload"] = {"changes": [{"action": "replace", "target_record_id": name,
            "payload": {"content": name.upper(), "base_sha256": core.sha(name.encode())}} for name in ("one", "two")]}
        runtime = self.initialize(); self.plan()
        original = git_store.os.replace
        def interrupt(src, dst):
            if pathlib.Path(dst) == self.two: raise OSError("synthetic interruption")
            return original(src, dst)
        privacy = {"gitleaks": "gitleaks", "policy": str(self.root / ".openchamber/repo-privacy-policy.json"), "gitleaks_config": str(self.package / ".gitleaks.toml")}
        with patch.object(git_store.os, "replace", side_effect=interrupt):
            with self.assertRaisesRegex(core.RecordError, "PARTIAL"):
                git_store.apply(self.project, runtime, self.req, privacy, self.env["plan"], self.env["plan_hash"])
        self.assertEqual(self.one.read_bytes(), b"ONE"); self.assertEqual(self.two.read_bytes(), b"two")
        return runtime

    def test_managed_recover_completes_exact_partial_and_unblocks_next_operation(self):
        runtime = self.partial()
        code, result = self.call("recover")
        self.assertEqual(code, 0, result); self.assertEqual(result["data"]["outcome"], "complete")
        self.assertEqual(self.two.read_bytes(), b"TWO")
        self.assertEqual(self.call("recover")[1]["data"]["outcome"], "no_write")
        self.req["operation_id"] = "next-operation"; self.req["expected"]["version"] = 1
        for item in self.req["payload"]["changes"]: item["payload"]["base_sha256"] = core.sha(item["payload"]["content"].encode())
        self.assertEqual(self.call("preview")[0], 0)
        self.assertEqual(git_store.get_control(runtime)["version"], 1)

    def test_managed_recover_refuses_external_changes(self):
        self.partial(); self.one.write_bytes(b"external")
        code, result = self.call("recover")
        self.assertNotEqual(code, 0); self.assertEqual(result["error"]["code"], "REVISION_CONFLICT")
        self.assertEqual(self.one.read_bytes(), b"external"); self.assertEqual(self.two.read_bytes(), b"two")

    def test_managed_recover_refuses_changed_route(self):
        self.partial(); self.routes["generation"] += 1; self.save()
        code, result = self.call("recover")
        self.assertNotEqual(code, 0); self.assertEqual(result["error"]["code"], "REVISION_CONFLICT")
        self.assertEqual(self.one.read_bytes(), b"ONE"); self.assertEqual(self.two.read_bytes(), b"two")

    def test_reader_initialize_has_no_runtime_side_effect(self):
        self.req["actor_role"] = "reader"
        code, result = self.call("initialize")
        self.assertNotEqual(code, 0); self.assertEqual(result["error"]["code"], "FORBIDDEN")
        self.assertEqual(list(self.runtime_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
