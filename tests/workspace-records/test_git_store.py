import base64
import copy
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import core
import git_store
from test_core import STATUS, request


def clean_scan(payload, config):
    return {
        "state": "passed",
        "content_sha256": core.sha(core.canonical(payload)),
        "policy_sha256": "p",
        "rules_sha256": "r",
        "coverage": "synthetic",
        "pii_enabled": False,
    }


class TestGitStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.notes = self.root / "notes"
        self.project = self.notes / "projects" / "Legacy_Project"
        self.project.mkdir(parents=True)
        self.runtime_root = self.root / "runtime"
        self.runtime_root.mkdir()
        self.runtime = git_store.runtime_dir(
            self.runtime_root, self.project, "Legacy_Project"
        )
        self.status = self.project / "status.md"
        self.status.write_bytes(STATUS)
        self.req = request()
        init = copy.deepcopy(self.req)
        init["actor_role"] = "pm"
        init["classification"] = "unmigrated_git"
        git_store.initialize(self.runtime, init)
        self.req["payload"]["base_sha256"] = core.sha(STATUS)
        self.scanner = patch.object(git_store, "privacy_check", side_effect=clean_scan)
        self.scanner.start()

    def tearDown(self):
        self.scanner.stop()
        self.tmp.cleanup()

    def test_round_trip_preview_apply_replay(self):
        plan = git_store.preview(self.project, self.runtime, self.req, {})
        result = git_store.apply(
            self.project, self.runtime, self.req, {}, plan["plan"], plan["plan_hash"]
        )
        self.assertEqual(result["outcome"], "complete")
        self.assertIn("now: Updated\r\n", self.status.read_bytes().decode())
        self.assertEqual(
            git_store.preview(self.project, self.runtime, self.req, {})["outcome"],
            "no_write",
        )
        changed = copy.deepcopy(self.req)
        changed["payload"]["fields"]["now"] = "other"
        with self.assertRaisesRegex(core.RecordError, "ID_REUSE"):
            git_store.preview(self.project, self.runtime, changed, {})

    def test_external_change_after_preview_not_overwritten(self):
        plan = git_store.preview(self.project, self.runtime, self.req, {})
        self.status.write_bytes(STATUS + b"\r\nExternal\r\n")
        with self.assertRaisesRegex(core.RecordError, "REVISION_CONFLICT"):
            git_store.apply(
                self.project,
                self.runtime,
                self.req,
                {},
                plan["plan"],
                plan["plan_hash"],
            )
        self.assertTrue(self.status.read_bytes().endswith(b"External\r\n"))

    def test_gitkeep_inventory_lossless_and_hidden_refusal(self):
        for folder in ("tasks", "research", "acceptance"):
            (self.project / folder).mkdir()
            (self.project / folder / ".gitkeep").write_bytes(b"")
        scope = copy.deepcopy(self.req)
        scope["scope"]["records"] += [
            {"record_id": folder, "path": folder + "/.gitkeep", "fields": ["body"]}
            for folder in ("tasks", "research", "acceptance")
        ]
        inv = git_store.inventory(self.project, scope)
        for folder in ("tasks", "research", "acceptance"):
            self.assertIn(folder + "/.gitkeep", inv["paths"])
        recordset = git_store.enumerate_records(self.project, self.runtime, scope)
        self.assertEqual(recordset["completeness"], "complete")
        self.assertEqual(len(recordset["attachments"]), 3)
        for item in recordset["attachments"]:
            self.assertEqual(base64.b64decode(item["raw_bytes_b64"]), b"")
            self.assertEqual(item["sha256"], core.sha(b""))
        (self.project / "tasks" / ".gitkeep").write_bytes(b"not empty")
        with self.assertRaises(core.RecordError):
            git_store.inventory(self.project, scope)
        (self.project / "tasks" / ".gitkeep").write_bytes(b"")
        (self.project / "tasks" / ".hidden").write_bytes(b"")
        with self.assertRaises(core.RecordError):
            git_store.inventory(self.project, scope)
        with self.assertRaises(core.RecordError):
            git_store.relative_path("tasks/.hidden")
        with self.assertRaises(core.RecordError):
            git_store.relative_path(".gitkeep/child")

    def test_windows_alias_reserved_and_case_identity_refused(self):
        for rel in (
            "STATUS.md",
            "status.md.",
            "status.md ",
            "tasks/CON.md",
            "tasks/file:stream",
            "tasks/.. ",
        ):
            with self.subTest(path=rel), self.assertRaises(core.RecordError):
                git_store.target_path(self.project, rel, allow_missing=True)
        with self.assertRaises(core.RecordError):
            git_store.project_dir(self.notes, "legacy_project")
        for rel in ("STATUS.md", "status.md."):
            r = copy.deepcopy(self.req)
            r["scope"]["records"][0]["path"] = rel
            r["scope"]["records"][0]["fields"] = ["body"]
            r["scope"]["actions"] = ["replace"]
            r.update(
                {
                    "action": "replace",
                    "payload": {"content": "INVALID", "base_sha256": core.sha(STATUS)},
                }
            )
            with self.subTest(preview=rel), self.assertRaises(core.RecordError):
                git_store.preview(self.project, self.runtime, r, {})
            self.assertEqual(self.status.read_bytes(), STATUS)

    def test_worker_cannot_write_status_or_log_but_can_write_assigned_task(self):
        status = copy.deepcopy(self.req)
        status["actor_role"] = "worker"
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"):
            git_store.preview(self.project, self.runtime, status, {})
        (self.project / "log").mkdir()
        (self.project / "log" / "2026-01.md").write_text("old", encoding="utf-8")
        log = copy.deepcopy(status)
        log.update(
            {
                "action": "append",
                "target_record_id": "log",
                "payload": {"content": "new", "base_sha256": core.sha(b"old")},
            }
        )
        log["scope"]["actions"] = ["append"]
        log["scope"]["records"] = [
            {"record_id": "log", "path": "log/2026-01.md", "fields": ["body"]}
        ]
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"):
            git_store.preview(self.project, self.runtime, log, {})
        (self.project / "tasks").mkdir()
        task = copy.deepcopy(status)
        task.update(
            {
                "action": "replace",
                "target_record_id": "task",
                "payload": {"content": "assigned", "base_sha256": None},
            }
        )
        task["scope"]["actions"] = ["replace"]
        task["scope"]["records"] = [
            {"record_id": "task", "path": "tasks/work.md", "fields": ["body"]}
        ]
        plan = git_store.preview(self.project, self.runtime, task, {})
        self.assertEqual(
            git_store.apply(
                self.project, self.runtime, task, {}, plan["plan"], plan["plan_hash"]
            )["outcome"],
            "complete",
        )
        self.assertEqual((self.project / "tasks" / "work.md").read_text(), "assigned")

    def test_two_paths_to_same_hardlinked_file_refused(self):
        (self.project / "tasks").mkdir()
        first = self.project / "tasks" / "first.md"
        first.write_bytes(b"synthetic")
        second = self.project / "tasks" / "second.md"
        os.link(first, second)
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"):
            git_store.ensure_unique_files(
                self.project, ["tasks/first.md", "tasks/second.md"]
            )

    def test_scope_and_traversal_rejected(self):
        for bad in ("../escape", "tasks/../../escape", "C:/escape", "tasks\\escape"):
            with self.subTest(path=bad), self.assertRaises(core.RecordError):
                git_store.target_path(self.project, bad, True)
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.project / "tasks").symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("native symlink unavailable")
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if not powershell:
                self.skipTest("junction creation unavailable")
            env = os.environ.copy()
            env["P2_TEST_JUNCTION"] = str(self.project / "tasks")
            env["P2_TEST_TARGET"] = str(outside)
            created = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:P2_TEST_JUNCTION -Target $env:P2_TEST_TARGET | Out-Null",
                ],
                env=env,
                capture_output=True,
                timeout=15,
            )
            if created.returncode:
                self.skipTest("junction creation unavailable")
        with self.assertRaises(core.RecordError):
            git_store.target_path(self.project, "tasks/file.md", True)

    def test_inventory_recordset_original_bytes_and_attachment(self):
        (self.project / "overview.md").write_bytes("概覽\r\n".encode())
        (self.project / "progress.md").write_bytes(b"old\n")
        (self.project / "image.bin").write_bytes(b"\xff\x00")
        r = copy.deepcopy(self.req)
        r["scope"]["records"] = [
            {"record_id": "overview", "path": "overview.md", "fields": ["body"]},
            {"record_id": "history", "path": "progress.md", "fields": ["body"]},
            {"record_id": "image", "path": "image.bin", "fields": ["body"]},
        ]
        inv = git_store.inventory(self.project, r)
        self.assertEqual(
            set(inv["paths"]), {"status.md", "overview.md", "progress.md", "image.bin"}
        )
        data = git_store.enumerate_records(self.project, self.runtime, r)
        self.assertEqual(data["records"][0]["body"]["content"], "概覽\r\n")
        self.assertIsNone(data["records"][0]["source"]["original_time"])
        self.assertEqual(
            data["records"][1]["metadata"]["semantic_role"], "frozen_progress"
        )
        self.assertEqual(
            base64.b64decode(data["attachments"][0]["raw_bytes_b64"]), b"\xff\x00"
        )

    def test_freeze_rejects_old_epoch(self):
        r = copy.deepcopy(self.req)
        r.update(
            {
                "actor_role": "pm",
                "action": "freeze",
                "target_record_id": None,
                "payload": {},
            }
        )
        r["scope"]["actions"].append("freeze")
        p = git_store.preview(self.project, self.runtime, r, {})
        git_store.apply(self.project, self.runtime, r, {}, p["plan"], p["plan_hash"])
        old = copy.deepcopy(self.req)
        old["operation_id"] = "old-op"
        with self.assertRaisesRegex(core.RecordError, "STALE_EPOCH"):
            git_store.preview(self.project, self.runtime, old, {})

    def test_batch_partial_external_edit_not_overwritten(self):
        tasks = self.project / "tasks"
        tasks.mkdir()
        one = tasks / "one.md"
        two = tasks / "two.md"
        one.write_text("one", encoding="utf-8")
        two.write_text("two", encoding="utf-8")
        r = copy.deepcopy(self.req)
        r.update(
            {"operation_id": "batch1", "action": "batch", "target_record_id": None}
        )
        r["scope"]["actions"] = ["batch", "replace"]
        r["scope"]["records"] = [
            {"record_id": "one", "path": "tasks/one.md", "fields": ["body"]},
            {"record_id": "two", "path": "tasks/two.md", "fields": ["body"]},
        ]
        r["payload"] = {
            "changes": [
                {
                    "action": "replace",
                    "target_record_id": "one",
                    "payload": {"content": "ONE", "base_sha256": core.sha(b"one")},
                },
                {
                    "action": "replace",
                    "target_record_id": "two",
                    "payload": {"content": "TWO", "base_sha256": core.sha(b"two")},
                },
            ]
        }
        plan = git_store.preview(self.project, self.runtime, r, {})
        real = git_store.os.replace

        def fail_second(src, dst):
            if pathlib.Path(dst) == two:
                raise OSError("synthetic failure")
            return real(src, dst)

        with (
            patch.object(git_store.os, "replace", side_effect=fail_second),
            self.assertRaisesRegex(core.RecordError, "PARTIAL"),
        ):
            git_store.apply(
                self.project, self.runtime, r, {}, plan["plan"], plan["plan_hash"]
            )
        self.assertEqual(one.read_text(), "ONE")
        self.assertEqual(two.read_text(), "two")
        one.write_text("human later", encoding="utf-8")
        obs = git_store.reconcile(self.project, self.runtime, r)
        self.assertEqual(obs["outcome"], "manual_review")
        self.assertFalse(obs["files"][0]["matches_after"])
        self.assertEqual(one.read_text(), "human later")

    def test_lock_blocks_and_crash_releases_for_reconcile(self):
        with git_store.locked(self.runtime):
            with self.assertRaisesRegex(core.RecordError, "IN_FLIGHT"):
                git_store.preview(self.project, self.runtime, self.req, {})
        child = """import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import git_store
runtime=pathlib.Path(sys.argv[2])
with git_store.locked(runtime):
    git_store.write_json(git_store.operation_path(runtime,'crash'), {'operation_id':'crash','state':'accepted','scope':__import__('json').loads(sys.argv[3]),'files':[]})
    os._exit(19)
"""
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(SCRIPTS),
                str(self.runtime),
                __import__("json").dumps(self.req["scope"]),
            ],
            timeout=15,
            capture_output=True,
        )
        self.assertEqual(done.returncode, 19)
        r = copy.deepcopy(self.req)
        r["operation_id"] = "crash"
        self.assertEqual(
            git_store.reconcile(self.project, self.runtime, r)["outcome"],
            "manual_review",
        )

    def test_partial_operation_blocks_later_write_and_read_is_partial(self):
        entry = {
            "operation_id": "pending-op",
            "state": "partial",
            "scope": self.req["scope"],
            "files": [
                {
                    "path": "status.md",
                    "before_sha256": core.sha(STATUS),
                    "after_sha256": "a" * 64,
                    "state": "writing",
                }
            ],
        }
        git_store.write_json(git_store.operation_path(self.runtime, "pending-op"), entry)
        other = copy.deepcopy(self.req)
        other["operation_id"] = "next-op"
        with self.assertRaisesRegex(core.RecordError, "IN_FLIGHT"):
            git_store.preview(self.project, self.runtime, other, {})
        recordset = git_store.enumerate_records(self.project, self.runtime, self.req)
        self.assertEqual(recordset["completeness"], "partial")
        self.assertEqual(recordset["incomplete_operations"], ["pending-op"])

    def test_mode_changed_after_preview_rejected(self):
        plan = git_store.preview(self.project, self.runtime, self.req, {})
        c = git_store.get_control(self.runtime)
        c["epoch"] = 2
        git_store.write_json(self.runtime / "control.json", c)
        with self.assertRaisesRegex(core.RecordError, "STALE_EPOCH"):
            git_store.apply(
                self.project,
                self.runtime,
                self.req,
                {},
                plan["plan"],
                plan["plan_hash"],
            )
        self.assertEqual(self.status.read_bytes(), STATUS)

    def test_recordset_includes_journal_and_partial_scope(self):
        p = git_store.preview(self.project, self.runtime, self.req, {})
        git_store.apply(
            self.project, self.runtime, self.req, {}, p["plan"], p["plan_hash"]
        )
        (self.project / "overview.md").write_text("hello", encoding="utf-8")
        out = git_store.enumerate_records(self.project, self.runtime, self.req)
        self.assertEqual(out["completeness"], "partial")
        self.assertEqual(out["unselected_paths"], ["overview.md"])
        self.assertEqual(out["journal"][0]["operation_id"], "op1")
        self.assertEqual(out["events"][0]["action"], "status_patch")

    def test_plan_bound_project_and_tampering(self):
        plan = git_store.preview(self.project, self.runtime, self.req, {})
        plan["plan"]["project_identity"] = "a" * 64
        plan["plan_hash"] = core.object_hash(plan["plan"])
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"):
            git_store.apply(
                self.project,
                self.runtime,
                self.req,
                {},
                plan["plan"],
                plan["plan_hash"],
            )


    def test_exact_operation_ids_case_and_reserved_names(self):
        first = copy.deepcopy(self.req)
        first["operation_id"] = "CaseSensitive"
        planned = git_store.preview(self.project, self.runtime, first, {})
        result = git_store.apply(self.project, self.runtime, first, {}, planned["plan"], planned["plan_hash"])
        different = copy.deepcopy(first)
        different["operation_id"] = "casesensitive"
        with self.assertRaisesRegex(core.RecordError, "REVISION_CONFLICT"):
            git_store.preview(self.project, self.runtime, different, {})
        self.assertNotEqual(git_store.operation_path(self.runtime, first["operation_id"]), git_store.operation_path(self.runtime, different["operation_id"]))
        different["expected"]["version"] = 1
        different["payload"]["base_sha256"] = core.sha(self.status.read_bytes())
        different["payload"]["fields"]["now"] = "Second independent operation"
        planned = git_store.preview(self.project, self.runtime, different, {})
        second = git_store.apply(self.project, self.runtime, different, {}, planned["plan"], planned["plan_hash"])
        self.assertEqual(second["result"]["operation_id"], "casesensitive")
        self.assertEqual(second["result"]["authority"]["logical_version"], 2)
        replay = git_store.preview(self.project, self.runtime, first, {})
        self.assertEqual(replay["outcome"], "no_write")
        self.assertEqual(replay["result"], result["result"])
        for version, name in enumerate(("CON", "NUL"), 2):
            req = copy.deepcopy(self.req)
            req["operation_id"] = name
            req["expected"]["version"] = version
            req["payload"]["base_sha256"] = core.sha(self.status.read_bytes())
            req["payload"]["fields"]["now"] = "Reserved ID " + name
            plan = git_store.preview(self.project, self.runtime, req, {})
            done = git_store.apply(self.project, self.runtime, req, {}, plan["plan"], plan["plan_hash"])
            self.assertEqual(done["result"]["operation_id"], name)
            self.assertTrue(git_store.operation_path(self.runtime, name).is_file())

    def test_legacy_or_mismatched_journal_is_fail_closed(self):
        legacy = self.runtime / "legacy-id.json"
        legacy.write_bytes(b'{"operation_id":"legacy-id","state":"partial"}')
        before = self.status.read_bytes()
        with self.assertRaisesRegex(core.RecordError, "INTEGRITY_FAILED"):
            git_store.preview(self.project, self.runtime, self.req, {})
        self.assertEqual(self.status.read_bytes(), before)
        self.assertTrue(legacy.is_file())
        legacy.unlink()  # 只清除本案例建立的 synthetic legacy marker。
        plan = git_store.preview(self.project, self.runtime, self.req, {})
        git_store.apply(self.project, self.runtime, self.req, {}, plan["plan"], plan["plan_hash"])
        path = git_store.operation_path(self.runtime, self.req["operation_id"])
        entry = git_store.read_json(path)
        entry["operation_id"] = "OP1"
        git_store.write_json(path, entry)
        with self.assertRaisesRegex(core.RecordError, "INTEGRITY_FAILED"):
            git_store.preview(self.project, self.runtime, self.req, {})
        with self.assertRaisesRegex(core.RecordError, "INTEGRITY_FAILED"):
            git_store.reconcile(self.project, self.runtime, self.req)


if __name__ == "__main__":
    unittest.main()
