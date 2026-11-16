import copy, pathlib, sys, unittest

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import core, git_store, google_docs, restore, snapshot
import test_snapshot
import test_git_store
import recovery
from unittest.mock import patch


class Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_snapshot.Tests("test_complete_all_history_and_bytes")
        self.fixture.setUp()
        for rid in self.fixture.refs:
            self.fixture.accept(rid)
        snapshot.finish({**self.fixture.key, "document": self.fixture.document})
        self.approved = self.fixture.root / "isolated"
        self.approved.mkdir()
        self.target = self.approved / "project"
        self.target.mkdir()
        self.args = {
            "private_dir": str(self.fixture.root),
            "snapshot_id": "snap",
            "approved_root": str(self.approved),
            "target_dir": str(self.target),
        }

    def tearDown(self):
        self.fixture.tearDown()

    def test_restore_roundtrip_and_repeat(self):
        plan = restore.preview(self.args)
        args = {**self.args, "plan": plan["plan"], "plan_hash": plan["plan_hash"]}
        self.assertEqual(restore.run(args)["outcome"], "complete")
        self.assertEqual(restore.run(args)["outcome"], "complete")
        self.assertEqual(
            (self.target / "research/a.md").read_bytes(), self.fixture.raw["newblob"]
        )
        self.assertEqual((self.target / "tasks/.gitkeep").read_bytes(), b"")
        (self.target / "research/a.md").write_bytes(b"external")
        with self.assertRaisesRegex(core.RecordError, "REVISION_CONFLICT"):
            restore.run(args)
        self.assertEqual((self.target / "research/a.md").read_bytes(), b"external")

    def test_git_runtime_preserves_old_operation_replay(self):
        import json

        req = {
            "schema_version": 1,
            "request_id": "r",
            "project_id": "Demo",
            "actor_role": "pm",
            "scope": {
                "project_id": "Demo",
                "actions": ["replace"],
                "records": [
                    {"record_id": "record", "path": "research/a.md", "fields": ["body"]}
                ],
            },
            "operation_id": "old-op",
            "action": "replace",
            "target_record_id": "record",
            "payload": {"content": "past", "base_sha256": "a" * 64},
            "expected": {"mode": "google", "phase": "steady", "epoch": 1, "version": 1},
        }
        h = core.request_hash(req)
        event = {
            "operation_id": "old-op",
            "request_hash": h,
            "version": 1,
            "action": "replace",
        }
        event2 = {
            "operation_id": "new-op",
            "request_hash": "b" * 64,
            "version": 2,
            "action": "replace",
        }
        old = {
            "operation_id": "old-op",
            "request_hash": h,
            "state": "complete",
            "scope": req["scope"],
            "event": event,
            "result": {"original": "unchanged"},
            "files": [],
        }
        new = {
            "project_id": "Demo",
            "operation_id": "new-op",
            "request_hash": "b" * 64,
            "state": "complete",
            "scope": req["scope"],
            "event": event2,
            "result": {"logical_version": 2},
        }
        page = core.canonical(
            {"schema_version": 1, "events": [event, event2], "journal": [old, new]}
        )
        self.fixture.put("runtimepage", page)
        current = json.loads(self.fixture.raw["current"].decode())
        current["history_pages"] = [self.fixture.refs["runtimepage"]]
        original_record = current["records"][0]["record"]
        original_record["created_at"] = "2020-01-01T00:00:00Z"
        original_record["updated_at"] = "2021-02-03T04:05:06Z"
        original_record["source"]["original_time"] = "2019-01-01T00:00:00Z"
        original_record["metadata"] = {
            "custom": {"nested": "保留"},
            "filesystem_mtime": "2020-01-01T00:00:00Z",
        }
        original_record["relations"] = [{"kind": "evidence", "target": "task-1"}]
        current["attachments"][0]["attachment"]["extra"] = {"preserve": True}
        self.fixture.put("runtimeroot", core.canonical(current))
        document = copy.deepcopy(self.fixture.document)
        run = document["tabs"][0]["body"]["content"][1]["paragraph"]["elements"][0][
            "textRun"
        ]
        head = json.loads(run["content"][len(google_docs.HEAD) :])
        head["root_manifest"] = self.fixture.refs["runtimeroot"]
        head["control"]["phase"] = "frozen"
        head["control"]["epoch"] = 2
        run["content"] = google_docs.head_line(head) + "\n"
        key = {"private_dir": str(self.fixture.root), "snapshot_id": "runtime-snap"}
        snapshot.begin(
            {
                **key,
                "document": document,
                "document_id": "doc",
                "head_tab_id": "machine",
                "summary_tab_id": "human",
                "project_id": "Demo",
            }
        )
        for rid in ("runtimeroot", "runtimepage", "newblob"):
            path = self.fixture.root / (rid + ".for-runtime")
            path.write_bytes(self.fixture.raw[rid])
            snapshot.accept(
                {
                    **key,
                    "reference": self.fixture.refs[rid],
                    "readback_remote_id": rid,
                    "downloaded_file": str(path),
                }
            )
        snapshot.finish({**key, "document": document})
        runtime = self.approved / "runtime"
        runtime.mkdir()
        args = {
            "private_dir": str(self.fixture.root),
            "snapshot_id": "runtime-snap",
            "approved_root": str(self.approved),
            "runtime_dir": str(runtime),
        }
        plan = restore.runtime_preview(args)
        self.assertEqual(plan["operation_count"], 2)
        restore.runtime_apply(
            {**args, "plan": plan["plan"], "plan_hash": plan["plan_hash"]}
        )
        self.assertEqual(git_store.get_control(runtime)["version"], 2)
        self.assertEqual(git_store.get_control(runtime)["phase"], "frozen")
        files_args = {
            "private_dir": str(self.fixture.root),
            "snapshot_id": "runtime-snap",
            "approved_root": str(self.approved),
            "target_dir": str(self.target),
        }
        files_plan = restore.preview(files_args)
        restore.run(
            {
                **files_args,
                "plan": files_plan["plan"],
                "plan_hash": files_plan["plan_hash"],
            }
        )
        read_req = copy.deepcopy(req)
        read_req["scope"]["records"].append(
            {"record_id": "keep", "path": "tasks/.gitkeep", "fields": ["body"]}
        )
        rs = git_store.enumerate_records(self.target, runtime, read_req)
        actual = rs["records"][0]
        body = actual.pop("body")
        self.assertEqual(
            actual, {k: v for k, v in original_record.items() if k != "body"}
        )
        self.assertEqual(body["content"].encode("utf-8"), self.fixture.raw["newblob"])
        self.assertEqual(rs["attachments"][0]["extra"], {"preserve": True})
        self.assertEqual(rs["completeness"], "complete")
        alias = copy.deepcopy(read_req)
        alias["scope"]["records"][0]["record_id"] = "renamed"
        with self.assertRaises(core.RecordError):
            git_store.enumerate_records(self.target, runtime, alias)
        path = self.target / "research/a.md"
        raw = path.read_bytes()
        path.write_bytes(b"external")
        stale = git_store.enumerate_records(self.target, runtime, read_req)
        self.assertEqual(stale["completeness"], "partial")
        self.assertEqual(stale["metadata_stale"], ["research/a.md"])
        path.write_bytes(raw)
        self.assertEqual(
            git_store.preview(self.target, runtime, req, {})["result"],
            {"original": "unchanged"},
        )
        changed = copy.deepcopy(req)
        changed["payload"]["content"] = "other"
        with self.assertRaisesRegex(core.RecordError, "ID_REUSE"):
            git_store.preview(self.target, runtime, changed, {})
        self.assertEqual(
            restore.runtime_apply(
                {**args, "plan": plan["plan"], "plan_hash": plan["plan_hash"]}
            )["outcome"],
            "complete",
        )
        # Isolated managed-write probe; actual resume is tested separately.
        control = git_store.get_control(runtime)
        control["phase"] = "steady"
        git_store.write_json(runtime / "control.json", control)
        update = copy.deepcopy(req)
        update.update(operation_id="update-metadata", request_id="update-metadata")
        update["expected"] = {
            "mode": "git",
            "phase": "steady",
            "epoch": 2,
            "version": 2,
        }
        update["payload"] = {"content": "新內容", "base_sha256": core.sha(raw)}
        with patch.object(
            git_store, "privacy_check", side_effect=test_git_store.clean_scan
        ):
            update_plan = git_store.preview(self.target, runtime, update, {})
            with patch.object(
                git_store,
                "write_catalog_update",
                side_effect=OSError("injected metadata crash"),
            ):
                with self.assertRaisesRegex(core.RecordError, "PARTIAL"):
                    git_store.apply(
                        self.target,
                        runtime,
                        update,
                        {},
                        update_plan["plan"],
                        update_plan["plan_hash"],
                    )
            partial = git_store.enumerate_records(self.target, runtime, read_req)
            self.assertEqual(partial["completeness"], "partial")
            self.assertIn("research/a.md", partial["metadata_stale"])
            completed = recovery.recover(
                self.target,
                runtime,
                update,
                {},
                update_plan["plan"],
                update_plan["plan_hash"],
            )
            self.assertEqual(completed["outcome"], "complete")
        after = git_store.enumerate_records(self.target, runtime, read_req)
        self.assertEqual(after["completeness"], "complete")
        self.assertEqual(after["records"][0]["metadata"], original_record["metadata"])
        self.assertEqual(after["records"][0]["relations"], original_record["relations"])
        self.assertEqual(
            after["records"][0]["source"]["content_sha256"],
            core.sha("新內容".encode("utf-8")),
        )
        (runtime / "record-metadata/records.json").unlink()
        with self.assertRaises(core.RecordError):
            git_store.enumerate_records(self.target, runtime, read_req)

    def test_existing_file_requires_exact_approval(self):
        (self.target / "research").mkdir()
        path = self.target / "research/a.md"
        path.write_bytes(b"local")
        with self.assertRaises(core.RecordError):
            restore.preview(self.args)
        plan = restore.preview(
            {**self.args, "approved_existing": {"research/a.md": core.sha(b"local")}}
        )
        path.write_bytes(b"changed")
        with self.assertRaises(core.RecordError):
            restore.run(
                {**self.args, "plan": plan["plan"], "plan_hash": plan["plan_hash"]}
            )
        self.assertEqual(path.read_bytes(), b"changed")

    def test_forged_plan_and_outside_root_refused(self):
        preview = restore.preview(self.args)
        forged = copy.deepcopy(preview["plan"])
        forged["files"][0]["before_sha256"] = "a" * 64
        with self.assertRaises(core.RecordError):
            restore.run(
                {**self.args, "plan": forged, "plan_hash": core.object_hash(forged)}
            )
        with self.assertRaises(core.RecordError):
            restore.preview({**self.args, "target_dir": str(self.fixture.root)})


if __name__ == "__main__":
    unittest.main()
