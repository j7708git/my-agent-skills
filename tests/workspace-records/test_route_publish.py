import copy, pathlib, sys, unittest
from unittest.mock import patch

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import core, git_store, route_publish, routing, test_routing, transition, test_connector_protocol, test_git_store, promotion, workspace_records


class Tests(unittest.TestCase):
    def setUp(self):
        self.f = test_routing.TestRouting(
            "test_positive_unmigrated_git_offline_and_fixed_runtime"
        )
        self.f.setUp()
        self.registry = self.f.registry
        self.root = self.f.root
        self.project = "Legacy_Project"
        self.runtime = git_store.runtime_dir(
            self.root / routing.RUNTIME, self.f.project, self.project
        )
        git_store.write_json(
            self.runtime / "control.json",
            {
                "mode": "git",
                "phase": "steady",
                "epoch": 1,
                "version": 0,
                "policy_version": "1",
            },
        )
        self.tkey = {"private_dir": str(self.runtime), "transition_id": "transition"}
        self.source = {
            "mode": "git",
            "phase": "steady",
            "epoch": 1,
            "version": 0,
            "resource_id": core.sha(str(self.f.project.resolve()).encode("utf-8")),
        }
        self.target = {"mode": "google", "resource_id": "doc", "manifest_sha256": None}
        transition.begin(
            {
                **self.tkey,
                "project_id": self.project,
                "actor_role": "pm",
                "source": self.source,
                "target": self.target,
                "writers": ["writer"],
            }
        )
        self.intent = transition.issue({**self.tkey, "operation_id": "freeze-op"})
        freeze = {
            "schema_version": 1,
            "request_id": "freeze-op",
            "project_id": self.project,
            "actor_role": "pm",
            "scope": {
                "project_id": self.project,
                "actions": ["freeze"],
                "records": [
                    {"record_id": "status", "path": "status.md", "fields": ["now"]}
                ],
            },
            "operation_id": "freeze-op",
            "action": "freeze",
            "target_record_id": None,
            "payload": {},
            "expected": {"mode": "git", "phase": "steady", "epoch": 1, "version": 0},
        }
        with patch.object(
            git_store, "privacy_check", side_effect=test_git_store.clean_scan
        ):
            plan = workspace_records.managed_dispatch(
                "preview", {"request": freeze}, self.registry
            )
            workspace_records.managed_dispatch(
                "apply",
                {
                    "request": freeze,
                    "plan": plan["plan"],
                    "plan_hash": plan["plan_hash"],
                },
                self.registry,
            )
        self.freeze_request = freeze

    def tearDown(self):
        self.f.tearDown()

    def req(self, op="route-freeze"):
        binding = self.registry.bind(self.project)
        return {
            "actor_role": "pm",
            "project_id": self.project,
            "operation_id": op,
            "stage": "freeze-source",
            "transition_id": "transition",
            "transition_private_dir": str(self.runtime),
            "transition_intent_sha256": self.intent["intent_hash"],
            "expected_registry_sha256": binding["registry_sha256"],
            "expected_generation": binding["registry_generation"],
        }

    def test_freeze_atomic_preserves_other_projects_and_replay(self):
        self.f.data["projects"]["Other"] = {
            "classification": "unmigrated_git",
            "backend": "git_local",
            "route_phase": "steady",
            "locator": None,
        }
        self.f.save()
        before = copy.deepcopy(self.f.data["projects"]["Other"])
        req = self.req()
        # Before the registry flips, frozen authority already rejects new managed writes.
        new_request = copy.deepcopy(self.freeze_request)
        new_request["operation_id"] = "other-freeze"
        with self.assertRaisesRegex(core.RecordError, "STALE_EPOCH|PHASE_FROZEN"):
            workspace_records.managed_dispatch(
                "preview", {"request": new_request}, self.registry
            )
        plan = route_publish.prepare(req, self.registry)
        with self.assertRaisesRegex(core.RecordError, "OUTCOME_UNKNOWN"):
            route_publish.reconcile(
                {**req, "plan_hash": plan["plan_hash"]}, self.registry
            )
        result = route_publish.apply(
            {**req, "plan_hash": plan["plan_hash"]}, self.registry
        )
        self.assertEqual(result["outcome"], "complete")
        data, _ = self.registry.load()
        self.assertEqual(data["projects"]["Other"], before)
        self.assertEqual(data["projects"][self.project]["route_phase"], "frozen")
        self.assertEqual(
            route_publish.apply({**req, "plan_hash": plan["plan_hash"]}, self.registry)[
                "outcome"
            ],
            "no_write",
        )
        self.assertEqual(
            route_publish.prepare(req, self.registry)["outcome"], "no_write"
        )
        with self.assertRaisesRegex(core.RecordError, "ID_REUSE"):
            route_publish.prepare(
                {**req, "transition_intent_sha256": "a" * 64}, self.registry
            )
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"):
            route_publish.reconcile(
                {**req, "actor_role": "worker", "plan_hash": plan["plan_hash"]},
                self.registry,
            )

    def test_unknown_response_only_reconciles(self):
        req = self.req()
        plan = route_publish.prepare(req, self.registry)
        actual = git_store.write_json

        def injected(path, value):
            actual(path, value)
            if pathlib.Path(path) == self.f.registry_path:
                raise OSError("response lost")

        with patch.object(git_store, "write_json", side_effect=injected):
            with self.assertRaises(OSError):
                route_publish.apply(
                    {**req, "plan_hash": plan["plan_hash"]}, self.registry
                )
        with self.assertRaisesRegex(core.RecordError, "OUTCOME_UNKNOWN"):
            route_publish.apply({**req, "plan_hash": plan["plan_hash"]}, self.registry)
        result = route_publish.reconcile(
            {**req, "plan_hash": plan["plan_hash"]}, self.registry
        )
        self.assertEqual(result["outcome"], "complete")

    def test_wrong_fixed_git_source_identity_fails_closed(self):
        directory, state = transition.load(self.runtime, "transition")
        state["source"]["resource_id"] = "wrong-source"
        state["pending"]["source"]["resource_id"] = "wrong-source"
        git_store.write_json(directory / "transition.json", state)
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"):
            route_publish.prepare({**self.req(), "transition_intent_sha256": core.object_hash(state["pending"])}, self.registry)

    def test_tampered_parallel_transition_state_fails_closed(self):
        directory, state = transition.load(self.runtime, "transition")
        state["source"]["version"] = 99
        git_store.write_json(directory / "transition.json", state)
        with self.assertRaisesRegex(core.RecordError, "INTEGRITY_FAILED"):
            route_publish.prepare(self.req(), self.registry)
        self.assertEqual(
            self.registry.load()[0]["projects"][self.project]["route_phase"], "steady"
        )

    def test_registry_changed_pm_and_intent_fail_closed(self):
        req = self.req()
        with self.assertRaises(core.RecordError):
            route_publish.prepare({**req, "actor_role": "worker"}, self.registry)
        with self.assertRaises(core.RecordError):
            route_publish.prepare(
                {**req, "transition_intent_sha256": "a" * 64}, self.registry
            )
        plan = route_publish.prepare(req, self.registry)
        self.f.data["generation"] += 1
        self.f.save()
        with self.assertRaises(core.RecordError):
            route_publish.apply({**req, "plan_hash": plan["plan_hash"]}, self.registry)
        self.assertEqual(
            self.registry.load()[0]["projects"][self.project]["route_phase"], "steady"
        )

    def test_google_managed_freeze_is_verified_before_route_freeze(self):
        import json

        p = __import__("connector_protocol")
        fixture = test_connector_protocol.Tests(
            "test_bootstrap_full_graph_and_cas_unknown"
        )
        fixture.setUp()
        try:
            initial = p.prepare(fixture.prep)
            for item in initial["artifacts"]:
                fixture.upload(item["artifact_id"])
            p.prepare_cas(fixture.key)
            fixture.upload("manifest")
            ready = p.prepare_cas(fixture.key)
            batch = p.begin_cas({**fixture.key, "batch_hash": ready["batch_hash"]})[
                "batch"
            ]
            frozen = test_connector_protocol.update_doc(
                test_connector_protocol.doc(), batch
            )
            committed = p.reconcile_cas(
                {
                    **fixture.key,
                    "document": frozen,
                    "manifest_readback_file": fixture.uploads["remote-manifest"],
                }
            )
            # Isolated test fixture supplies a steady head to exercise the freeze path.
            head = copy.deepcopy(committed["head"])
            head["control"]["phase"] = "steady"
            before = copy.deepcopy(frozen)
            before["tabs"][0]["body"]["content"][1]["paragraph"]["elements"][0][
                "textRun"
            ]["content"] = __import__("google_docs").head_line(head) + "\n"
            existing = {
                "manifest_file": fixture.uploads["remote-manifest"],
                "objects": {
                    k: v for k, v in fixture.uploads.items() if k != "remote-manifest"
                },
            }
            binding = {
                "backend": "google_head",
                "route_phase": "steady",
                "runtime_directory": str(fixture.root),
                "locator": {"document_id": "doc"},
                "privacy_policy": "policy",
                "policy_version": "1",
            }

            class Registry:
                def require_no_activation(self, project):
                    pass

                def bind(self, project):
                    return binding

                def check(self, bound, authority):
                    if authority["control"]["phase"] != "steady":
                        raise core.RecordError("PHASE_FROZEN")

            freeze = {
                "schema_version": 1,
                "request_id": "freeze-id",
                "project_id": "Demo",
                "actor_role": "pm",
                "scope": {
                    "project_id": "Demo",
                    "actions": ["freeze"],
                    "records": [
                        {"record_id": "status", "path": "status.md", "fields": ["now"]}
                    ],
                },
                "operation_id": "freeze-id",
                "action": "freeze",
                "target_record_id": None,
                "payload": {},
                "expected": {
                    "mode": "google",
                    "phase": "steady",
                    "epoch": 2,
                    "version": 102,
                },
            }
            key = {
                "private_dir": str(fixture.root),
                "project_id": "Demo",
                "operation_id": "freeze-id",
            }
            base = {
                **key,
                "document": before,
                "document_id": "doc",
                "head_tab_id": "machine",
                "summary_tab_id": "human",
            }
            staged = p.managed_dispatch(
                "prepare", {**base, "request": freeze, "existing": existing}, Registry()
            )
            uploaded = {}

            def upload(aid):
                sent = p.managed_dispatch(
                    "begin-upload", {**base, "artifact_id": aid}, Registry()
                )
                raw = pathlib.Path(sent["file_uri"]).read_bytes()
                path = fixture.root / ("freeze-" + aid)
                path.write_bytes(raw)
                rid = "freeze-" + aid
                p.managed_dispatch(
                    "accept-upload",
                    {
                        **base,
                        "artifact_id": aid,
                        "remote_id": rid,
                        "readback_remote_id": rid,
                        "downloaded_file": str(path),
                    },
                    Registry(),
                )
                uploaded[rid] = str(path)

            for item in staged["artifacts"]:
                upload(item["artifact_id"])
            p.managed_dispatch("prepare-cas", base, Registry())
            upload("manifest")
            planned = p.managed_dispatch("prepare-cas", base, Registry())
            sent = p.managed_dispatch(
                "begin-cas", {**base, "batch_hash": planned["batch_hash"]}, Registry()
            )
            after = test_connector_protocol.update_doc(before, sent["batch"])
            after["revisionId"] = "rev2"
            result = p.managed_dispatch(
                "reconcile-cas",
                {
                    **base,
                    "document": after,
                    "manifest_readback_file": uploaded["freeze-manifest"],
                },
                Registry(),
            )
            graph = json.loads(
                pathlib.Path(uploaded["freeze-manifest"]).read_text(encoding="utf-8")
            )
            refs = (
                graph["history_pages"]
                + [r["object"] for r in graph["records"]]
                + [a["object"] for a in graph["attachments"]]
            )
            files = {**fixture.uploads, **uploaded}
            latest = {
                "manifest_file": uploaded["freeze-manifest"],
                "objects": {
                    r["id"]: files[r["id"]]
                    for r in refs
                    if r.get("encoding") != "inline-empty-v1"
                },
            }
            observation = {
                "document": after,
                "head_tab_id": "machine",
                "summary_tab_id": "human",
                "existing": latest,
            }
            imported = route_publish.freeze_operation(
                fixture.root,
                {"project_id": "Demo"},
                {"mode": "google", "resource_id": "doc"},
                observation,
                "freeze-id",
            )
            self.assertEqual(imported["scope"]["records"][0]["record_id"], "status")
            route_publish.verify_control(
                fixture.root,
                {"project_id": "Demo", "policy_version": "1"},
                "google",
                "doc",
                "frozen",
                3,
                103,
                observation,
                expected_last={
                    "operation_id": "freeze-id",
                    "request_hash": core.request_hash(freeze),
                },
                expected_action="freeze",
            )
            self.assertEqual(result["head"]["control"]["phase"], "frozen")
            new = copy.deepcopy(freeze)
            new["operation_id"] = "other"
            with self.assertRaises(core.RecordError):
                p.managed_dispatch(
                    "prepare",
                    {**base, "document": after, "request": new, "existing": latest},
                    Registry(),
                )
        finally:
            fixture.tearDown()

    def test_google_target_requires_full_verified_graph_and_exact_head(self):
        p = __import__("connector_protocol")
        fixture = test_connector_protocol.Tests(
            "test_bootstrap_full_graph_and_cas_unknown"
        )
        fixture.setUp()
        try:
            initial = p.prepare(fixture.prep)
            for item in initial["artifacts"]:
                fixture.upload(item["artifact_id"])
            p.prepare_cas(fixture.key)
            fixture.upload("manifest")
            ready = p.prepare_cas(fixture.key)
            batch = p.begin_cas({**fixture.key, "batch_hash": ready["batch_hash"]})[
                "batch"
            ]
            document = test_connector_protocol.update_doc(
                test_connector_protocol.doc(), batch
            )
            committed = p.reconcile_cas(
                {
                    **fixture.key,
                    "document": document,
                    "manifest_readback_file": fixture.uploads["remote-manifest"],
                }
            )
            head = committed["head"]
            binding = {"project_id": "Demo", "policy_version": "1"}
            existing = {
                "manifest_file": fixture.uploads["remote-manifest"],
                "objects": {
                    k: v for k, v in fixture.uploads.items() if k != "remote-manifest"
                },
            }
            observation = {
                "document": document,
                "head_tab_id": "machine",
                "summary_tab_id": "human",
                "existing": existing,
            }
            route_publish.verify_control(
                fixture.root,
                binding,
                "google",
                "doc",
                "frozen",
                2,
                102,
                observation,
                expected_root=head["root_manifest"]["sha256"],
            )
            with self.assertRaises(core.RecordError):
                route_publish.verify_control(
                    fixture.root,
                    binding,
                    "google",
                    "doc",
                    "frozen",
                    2,
                    102,
                    observation,
                    expected_root="f" * 64,
                )
            missing = copy.deepcopy(observation)
            missing["existing"]["objects"].pop(
                next(iter(missing["existing"]["objects"]))
            )
            with self.assertRaises(core.RecordError):
                route_publish.verify_control(
                    fixture.root,
                    binding,
                    "google",
                    "doc",
                    "frozen",
                    2,
                    102,
                    missing,
                    expected_root=head["root_manifest"]["sha256"],
                )
        finally:
            fixture.tearDown()

    def test_target_switch_and_resume_require_actual_git_control(self):
        self.f.google()
        self.f.data["projects"][self.project]["route_phase"] = "frozen"
        self.f.save()
        source = {
            "mode": "google",
            "phase": "steady",
            "epoch": 1,
            "version": 1,
            "resource_id": "synthetic-head",
        }
        target = {"mode": "git", "resource_id": "target", "manifest_sha256": "a" * 64}
        state = {
            "project_id": self.project,
            "transition_id": "return",
            "source": source,
            "target": target,
            "writers": ["writer"],
            "phase": "in_progress",
            "pending": None,
            "receipts": [
                {"step": "snapshot", "observation": {"snapshot_id": "snap"}},
                {
                    "step": "target_frozen",
                    "observation": {
                        "resource_id": "target",
                        "mode": "git",
                        "phase": "frozen",
                        "epoch": 2,
                        "version": 2,
                        "manifest_sha256": "a" * 64,
                    },
                },
            ],
        }
        target_intent = {
            "transition_id": "return",
            "project_id": self.project,
            "operation_id": "target-op",
            "step": "target_frozen",
            "source": source,
            "target": {**target, "manifest_sha256": None},
            "frozen_epoch": 2,
            "writers": ["writer"],
        }
        state["receipts"][-1]["operation_id"] = "target-op"
        state["receipts"][-1]["intent_hash"] = core.object_hash(target_intent)
        pending = {
            "transition_id": "return",
            "project_id": self.project,
            "operation_id": "route-step",
            "step": "route_frozen",
            "source": source,
            "target": target,
            "frozen_epoch": 2,
            "writers": ["writer"],
        }
        state["pending"] = pending
        binding = self.registry.bind(self.project)
        base = {
            "actor_role": "pm",
            "project_id": self.project,
            "operation_id": "publish-switch",
            "stage": "switch-frozen",
            "transition_id": "return",
            "transition_private_dir": str(self.runtime),
            "transition_intent_sha256": core.object_hash(pending),
            "expected_registry_sha256": binding["registry_sha256"],
            "expected_generation": binding["registry_generation"],
        }
        wrong_source = copy.deepcopy(state)
        wrong_source["source"]["resource_id"] = "wrong-doc"
        wrong_source["pending"]["source"]["resource_id"] = "wrong-doc"
        with patch.object(route_publish, "transition_state", return_value=(self.runtime, wrong_source)):
            with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"):
                route_publish.prepare({**base, "transition_intent_sha256": core.object_hash(wrong_source["pending"])}, self.registry)
        with (
            patch.object(
                route_publish, "transition_state", return_value=(self.runtime, state)
            ),
            patch.object(promotion, "verify_project", return_value="a" * 64),
        ):
            valid_hash = state["receipts"][-1]["intent_hash"]
            state["receipts"][-1]["intent_hash"] = "f" * 64
            with self.assertRaisesRegex(core.RecordError, "INTEGRITY_FAILED"):
                route_publish.prepare(base, self.registry)
            state["receipts"][-1]["intent_hash"] = valid_hash
            state["receipts"][-1]["observation"]["version"] = 99
            with self.assertRaises(core.RecordError):
                route_publish.prepare(base, self.registry)
            state["receipts"][-1]["observation"]["version"] = 2
            with self.assertRaises(core.RecordError):
                route_publish.prepare(base, self.registry)
            git_store.write_json(
                self.runtime / "control.json",
                {
                    "mode": "git",
                    "phase": "frozen",
                    "epoch": 2,
                    "version": 2,
                    "policy_version": "1",
                },
            )
            planned = route_publish.prepare(base, self.registry)
            route_publish.apply(
                {**base, "plan_hash": planned["plan_hash"]}, self.registry
            )
        binding = self.registry.bind(self.project)
        self.assertEqual(binding["backend"], "git_local")
        self.assertEqual(binding["route_phase"], "frozen")
        intent = {
            "transition_id": "return",
            "project_id": self.project,
            "operation_id": "resume-op",
            "step": "resume",
            "source": source,
            "target": target,
            "frozen_epoch": 2,
            "writers": ["writer"],
        }
        req = {
            "schema_version": 1,
            "request_id": "resume-op",
            "project_id": self.project,
            "actor_role": "pm",
            "scope": {"project_id": self.project, "actions": ["resume"], "records": []},
            "operation_id": "resume-op",
            "action": "resume",
            "target_record_id": None,
            "payload": {
                "transition_id": "return",
                "transition_intent_sha256": core.object_hash(intent),
            },
            "expected": {"mode": "git", "phase": "frozen", "epoch": 2, "version": 2},
        }
        h = core.request_hash(req)
        event = {
            "operation_id": "resume-op",
            "request_hash": h,
            "action": "resume",
            "version": 3,
        }
        state.update(phase="complete", pending=None)
        state["receipts"].append(
            {
                "step": "resume",
                "operation_id": "resume-op",
                "intent_hash": core.object_hash(intent),
                "observation": {
                    "resource_id": "target",
                    "mode": "git",
                    "phase": "steady",
                    "epoch": 2,
                    "version": 3,
                },
            }
        )
        git_store.write_json(
            git_store.operation_path(self.runtime, "resume-op"),
            {
                "operation_id": "resume-op",
                "request_hash": h,
                "state": "complete",
                "event": event,
                "scope": req["scope"],
                "files": [],
            },
        )
        resume = {
            "actor_role": "pm",
            "project_id": self.project,
            "operation_id": "publish-steady",
            "stage": "resume-steady",
            "transition_id": "return",
            "transition_private_dir": str(self.runtime),
            "transition_intent_sha256": core.object_hash(intent),
            "expected_registry_sha256": binding["registry_sha256"],
            "expected_generation": binding["registry_generation"],
        }
        with (
            patch.object(
                route_publish, "transition_state", return_value=(self.runtime, state)
            ),
            patch.object(promotion, "verify_project", return_value="a" * 64),
        ):
            with self.assertRaises(core.RecordError):
                route_publish.prepare(resume, self.registry)
            git_store.write_json(
                self.runtime / "control.json",
                {
                    "mode": "git",
                    "phase": "steady",
                    "epoch": 2,
                    "version": 3,
                    "policy_version": "1",
                },
            )
            planned = route_publish.prepare(resume, self.registry)
            route_publish.apply(
                {**resume, "plan_hash": planned["plan_hash"]}, self.registry
            )
        self.assertEqual(self.registry.bind(self.project)["route_phase"], "steady")


if __name__ == "__main__":
    unittest.main()
