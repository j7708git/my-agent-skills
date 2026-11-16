import copy, pathlib, sys, unittest
from unittest.mock import patch

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import core, git_store, google_docs, promotion, restore, routing, snapshot, test_snapshot, transition


class Tests(unittest.TestCase):
    def setUp(self):
        self.f = test_snapshot.Tests("test_complete_all_history_and_bytes")
        self.f.setUp()
        self.root = self.f.root
        doc = copy.deepcopy(self.f.document)
        run = doc["tabs"][0]["body"]["content"][1]["paragraph"]["elements"][0][
            "textRun"
        ]
        import json

        head = json.loads(run["content"][len(google_docs.HEAD) :])
        head["control"]["phase"] = "frozen"
        head["control"]["epoch"] = 2
        event = {
            "operation_id": "old",
            "request_hash": "a" * 64,
            "version": 1,
            "action": "replace",
        }
        self.old_entry = {
            "operation_id": "old",
            "request_hash": "a" * 64,
            "state": "complete",
            "scope": {"project_id": "Demo", "actions": ["replace"], "records": []},
            "event": event,
            "result": {"preserved": True},
            "files": [],
        }
        page = self.f.put(
            "old-journal-page",
            core.canonical(
                {"schema_version": 1, "events": [event], "journal": [self.old_entry]}
            ),
        )
        manifest = json.loads(self.f.raw["current"].decode())
        manifest["history_pages"].append(page)
        head["root_manifest"] = self.f.put("promotion-root", core.canonical(manifest))
        run["content"] = google_docs.head_line(head) + "\n"
        self.key = {"private_dir": str(self.root), "snapshot_id": "frozen"}
        snapshot.begin(
            {
                **self.key,
                "document": doc,
                "document_id": "doc",
                "head_tab_id": "machine",
                "summary_tab_id": "human",
                "project_id": "Demo",
            }
        )
        for rid in self.f.refs:
            path = self.root / ("download-" + rid)
            path.write_bytes(self.f.raw[rid])
            snapshot.accept(
                {
                    **self.key,
                    "reference": self.f.refs[rid],
                    "readback_remote_id": rid,
                    "downloaded_file": str(path),
                }
            )
        self.receipt = snapshot.finish({**self.key, "document": doc})["receipt"]
        self.approved = self.root / "approved"
        self.approved.mkdir()
        self.source = self.approved / "source"
        self.source.mkdir()
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.project = workspace / "project-notes/projects/Demo"
        self.project.mkdir(parents=True)
        runtime_root = workspace / routing.RUNTIME
        runtime_root.mkdir(parents=True)
        (workspace / routing.POLICY).write_text("{}", encoding="utf-8")
        self.route_file = workspace / routing.REGISTRY
        self.routes = {
            "schema_version": 1,
            "policy_version": "1",
            "generation": 1,
            "projects": {
                "Demo": {
                    "classification": "managed",
                    "backend": "google_head",
                    "route_phase": "frozen",
                    "locator": {"document_id": "doc"},
                }
            },
        }
        self.route_file.write_bytes(core.canonical(self.routes))
        self.registry = routing.Registry(workspace)
        self.target = pathlib.Path(self.registry.bind("Demo")["runtime_directory"])
        self.target.mkdir()
        args = {
            "private_dir": str(self.root),
            "snapshot_id": "frozen",
            "approved_root": str(self.approved),
            "runtime_dir": str(self.source),
        }
        plan = restore.runtime_preview(args)
        restore.runtime_apply(
            {**args, "plan": plan["plan"], "plan_hash": plan["plan_hash"]}
        )
        self.source_plan = plan
        project_args = {
            "private_dir": str(self.root),
            "snapshot_id": "frozen",
            "approved_root": str(self.root),
            "target_dir": str(self.project),
        }
        project_plan = restore.preview(project_args)
        restore.run(
            {
                **project_args,
                "plan": project_plan["plan"],
                "plan_hash": project_plan["plan_hash"],
            }
        )
        git_store.write_json(
            self.target / "control.json",
            {
                "mode": "git",
                "phase": "steady",
                "epoch": 1,
                "version": 0,
                "policy_version": "1",
            },
        )
        git_store.write_json(
            git_store.operation_path(self.target, "old"), self.old_entry
        )
        (self.target / "p3-sessions").mkdir()
        (self.target / "p3-sessions" / "keep").write_bytes(b"untouched")
        tkey = {"private_dir": str(self.root), "transition_id": "return"}
        transition.begin(
            {
                **tkey,
                "project_id": "Demo",
                "actor_role": "pm",
                "source": {
                    "mode": "google",
                    "phase": "steady",
                    "epoch": 1,
                    "version": 1,
                    "resource_id": "doc",
                },
                "target": {
                    "mode": "git",
                    "resource_id": "git-target",
                    "manifest_sha256": None,
                },
                "writers": ["worker"],
            }
        )

        def step(name, observation):
            sent = transition.issue({**tkey, "operation_id": "transition-" + name})
            transition.observe(
                {**tkey, "intent_hash": sent["intent_hash"], "observation": observation}
            )

        step(
            "freeze",
            {
                "resource_id": "doc",
                "mode": "google",
                "phase": "frozen",
                "epoch": 2,
                "version": 2,
            },
        )
        step(
            "drain",
            {
                "inflight": [],
                "writer_attestations": {
                    "worker": {
                        "state": "caller_verified_stopped",
                        "evidence_sha256": "b" * 64,
                    }
                },
            },
        )
        step(
            "snapshot",
            {"snapshot_id": "frozen", "receipt_sha256": core.object_hash(self.receipt)},
        )
        target_intent = transition.issue({**tkey, "operation_id": "transition-target"})
        self.transition_intent_hash = target_intent["intent_hash"]
        self.req = {
            "actor_role": "pm",
            "project_id": "Demo",
            "operation_id": "promote",
            "private_dir": str(self.root),
            "approved_root": str(self.approved),
            "source_runtime": str(self.source),
            "source_plan_hash": plan["plan_hash"],
            "snapshot_id": "frozen",
            "transition_id": "return",
            "transition_intent_sha256": self.transition_intent_hash,
        }

    def tearDown(self):
        self.f.tearDown()

    def test_control_last_crash_replay_and_preserve_namespace(self):
        prepared = promotion.prepare(self.req, self.registry)
        self.assertEqual(prepared["old_files_backed_up"], 2)
        real = promotion.os.replace

        def crash(src, dst):
            if pathlib.Path(dst) == self.target / "control.json":
                raise OSError("interruption before control")
            return real(src, dst)

        with patch.object(promotion.os, "replace", side_effect=crash):
            with self.assertRaises(OSError):
                promotion.apply(
                    {**self.req, "plan_hash": prepared["plan_hash"]}, self.registry
                )
        self.assertEqual(git_store.get_control(self.target)["version"], 0)
        result = promotion.apply(
            {**self.req, "plan_hash": prepared["plan_hash"]}, self.registry
        )
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(git_store.get_control(self.target)["version"], 2)
        self.assertEqual(git_store.get_control(self.target)["phase"], "frozen")
        self.assertEqual((self.target / "p3-sessions/keep").read_bytes(), b"untouched")
        self.assertEqual(
            promotion.apply(
                {**self.req, "plan_hash": prepared["plan_hash"]}, self.registry
            )["outcome"],
            "no_write",
        )

    def test_changed_target_and_route_fail_closed(self):
        prepared = promotion.prepare(self.req, self.registry)
        git_store.write_json(
            self.target / "control.json",
            {
                "mode": "git",
                "phase": "steady",
                "epoch": 1,
                "version": 9,
                "policy_version": "1",
            },
        )
        with self.assertRaises(core.RecordError):
            promotion.apply(
                {**self.req, "plan_hash": prepared["plan_hash"]}, self.registry
            )
        self.assertEqual(git_store.get_control(self.target)["version"], 9)
        self.routes["projects"]["Demo"]["route_phase"] = "steady"
        self.route_file.write_bytes(core.canonical(self.routes))
        with self.assertRaises(core.RecordError):
            promotion.apply(
                {**self.req, "plan_hash": prepared["plan_hash"]}, self.registry
            )

    def test_cannot_drop_existing_old_journal(self):
        altered = copy.deepcopy(self.old_entry)
        altered["result"] = {"changed": True}
        git_store.write_json(git_store.operation_path(self.target, "old"), altered)
        with self.assertRaises(core.RecordError):
            promotion.prepare(self.req, self.registry)

    def test_wrong_transition_or_google_source_refused(self):
        bad = {**self.req, "transition_intent_sha256": "f" * 64}
        with self.assertRaises(core.RecordError):
            promotion.prepare(bad, self.registry)
        self.routes["projects"]["Demo"]["locator"]["document_id"] = "other-doc"
        self.route_file.write_bytes(core.canonical(self.routes))
        with self.assertRaises(core.RecordError):
            promotion.prepare(self.req, self.registry)

    def test_replaced_snapshot_receipt_refused(self):
        directory, state = snapshot.load(self.root, "frozen")
        state["receipt"]["future_gap"] = "forged"
        git_store.write_json(directory / "snapshot.json", state)
        with self.assertRaises(core.RecordError):
            promotion.prepare(self.req, self.registry)

    def test_snapshot_source_changed_with_same_receipt_refused(self):
        directory, state = snapshot.load(self.root, "frozen")
        state["source"]["head"]["control"]["epoch"] = 99
        git_store.write_json(directory / "snapshot.json", state)
        with self.assertRaises(core.RecordError):
            promotion.prepare(self.req, self.registry)
        self.assertEqual(git_store.get_control(self.target)["version"], 0)

    def test_epoch_and_version_regression_refused(self):
        git_store.write_json(
            self.target / "control.json",
            {
                "mode": "git",
                "phase": "steady",
                "epoch": 3,
                "version": 5,
                "policy_version": "1",
            },
        )
        with self.assertRaisesRegex(core.RecordError, "STALE_EPOCH"):
            promotion.prepare(self.req, self.registry)

    def test_external_project_file_change_refuses_promotion(self):
        prepared = promotion.prepare(self.req, self.registry)
        path = self.project / "research/a.md"
        path.write_bytes(b"external")
        with self.assertRaises(core.RecordError):
            promotion.apply(
                {**self.req, "plan_hash": prepared["plan_hash"]}, self.registry
            )
        self.assertEqual(path.read_bytes(), b"external")
        self.assertEqual(git_store.get_control(self.target)["version"], 0)

    def test_unrelated_root_json_refused(self):
        (self.target / "other.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(core.RecordError):
            promotion.prepare(self.req, self.registry)


if __name__ == "__main__":
    unittest.main()
