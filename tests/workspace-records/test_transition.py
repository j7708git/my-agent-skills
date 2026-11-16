import copy, pathlib, sys, unittest
from unittest.mock import patch

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import core, google_docs, snapshot, transition, restore, test_snapshot, test_connector_protocol


class Tests(unittest.TestCase):
    def setUp(self):
        self.f = test_snapshot.Tests("test_complete_all_history_and_bytes")
        self.f.setUp()
        self.root = self.f.root
        self.k = {"private_dir": str(self.root), "transition_id": "move"}
        self.src = {
            "mode": "google",
            "phase": "steady",
            "epoch": 1,
            "version": 1,
            "resource_id": "doc",
        }
        self.target = {
            "mode": "git",
            "resource_id": "target",
            "manifest_sha256": core.object_hash(
                [
                    {
                        "path": "research/a.md",
                        "sha256": self.f.refs["newblob"]["sha256"],
                        "size": self.f.refs["newblob"]["size"],
                    },
                    {"path": "tasks/.gitkeep", "sha256": core.sha(b""), "size": 0},
                ]
            ),
        }
        transition.begin(
            {
                **self.k,
                "project_id": "Demo",
                "actor_role": "pm",
                "source": self.src,
                "target": self.target,
                "writers": ["worker"],
            }
        )

    def tearDown(self):
        self.f.tearDown()

    def step(self, n, obs):
        issued = transition.issue({**self.k, "operation_id": "op-" + n})
        self.assertEqual(issued["intent"]["step"], n)
        return transition.observe(
            {**self.k, "intent_hash": issued["intent_hash"], "observation": obs}
        )

    def test_unknown_and_fence_attestation_required(self):
        sent = transition.issue({**self.k, "operation_id": "freeze"})
        with self.assertRaisesRegex(core.RecordError, "OUTCOME_UNKNOWN"):
            transition.issue({**self.k, "operation_id": "another"})
        with self.assertRaises(core.RecordError):
            transition.observe(
                {
                    **self.k,
                    "intent_hash": sent["intent_hash"],
                    "observation": {"phase": "steady"},
                }
            )
        transition.observe(
            {
                **self.k,
                "intent_hash": sent["intent_hash"],
                "observation": {
                    "resource_id": "doc",
                    "mode": "google",
                    "phase": "frozen",
                    "epoch": 2,
                    "version": 2,
                },
            }
        )
        sent = transition.issue({**self.k, "operation_id": "drain"})
        with self.assertRaises(core.RecordError):
            transition.observe(
                {
                    **self.k,
                    "intent_hash": sent["intent_hash"],
                    "observation": {
                        "inflight": [],
                        "writer_attestations": {"worker": True},
                    },
                }
            )
        self.assertFalse(
            transition.inspect(self.k)["physical_fence_independently_verified"]
        )

    def test_google_resume_stages_under_frozen_route(self):
        p = __import__("connector_protocol")
        f = test_connector_protocol.Tests("test_bootstrap_full_graph_and_cas_unknown")
        f.setUp()
        try:
            initial = p.prepare(f.prep)
            for item in initial["artifacts"]:
                f.upload(item["artifact_id"])
            p.prepare_cas(f.key)
            f.upload("manifest")
            ready = p.prepare_cas(f.key)
            batch = p.begin_cas({**f.key, "batch_hash": ready["batch_hash"]})["batch"]
            doc = test_connector_protocol.update_doc(
                test_connector_protocol.doc(), batch
            )
            committed = p.reconcile_cas(
                {
                    **f.key,
                    "document": doc,
                    "manifest_readback_file": f.uploads["remote-manifest"],
                }
            )
            head = committed["head"]
            target = {
                "mode": "google",
                "resource_id": "doc",
                "manifest_sha256": head["root_manifest"]["sha256"],
            }
            source = {
                "mode": "git",
                "phase": "steady",
                "epoch": 1,
                "version": 100,
                "resource_id": "source",
            }
            key = {"private_dir": str(f.root), "transition_id": "google-move"}
            transition.begin(
                {
                    **key,
                    "project_id": "Demo",
                    "actor_role": "pm",
                    "source": source,
                    "target": target,
                    "writers": ["worker"],
                }
            )
            directory, state = transition.load(f.root, "google-move")
            intent = {
                "transition_id": "google-move",
                "project_id": "Demo",
                "operation_id": "resume-google",
                "step": "resume",
                "source": source,
                "target": target,
                "frozen_epoch": 2,
                "writers": ["worker"],
            }
            state.update(
                next_index=5,
                pending=intent,
                receipts=[
                    {
                        "step": "target_frozen",
                        "observation": {"version": head["version"]},
                    },
                    {
                        "step": "route_frozen",
                        "observation": {"registry_sha256": "c" * 64},
                    },
                ],
            )
            transition.git_store.write_json(directory / "transition.json", state)
            binding = {
                "backend": "google_head",
                "route_phase": "frozen",
                "registry_sha256": "c" * 64,
                "locator": {"document_id": "doc"},
                "runtime_directory": str(f.root),
                "privacy_policy": "policy",
                "policy_version": "1",
            }

            class Registry:
                def require_no_activation(self, project):
                    pass

                def bind(self, project):
                    return binding

            base = {
                **key,
                "intent_hash": core.object_hash(intent),
                "document": doc,
                "head_tab_id": "machine",
                "summary_tab_id": "human",
            }
            existing = {
                "manifest_file": f.uploads["remote-manifest"],
                "objects": {
                    k: v for k, v in f.uploads.items() if k != "remote-manifest"
                },
            }
            initial = transition.resume_google(
                {**base, "stage": "prepare", "existing": existing}, Registry()
            )
            self.assertEqual(initial["outcome"], "staged")
            uploads = {}

            def upload(aid):
                sent = transition.resume_google(
                    {**base, "stage": "begin-upload", "artifact_id": aid}, Registry()
                )
                raw = pathlib.Path(sent["file_uri"]).read_bytes()
                path = f.root / ("resume-" + aid)
                path.write_bytes(raw)
                rid = "resume-" + aid
                transition.resume_google(
                    {
                        **base,
                        "stage": "accept-upload",
                        "artifact_id": aid,
                        "remote_id": rid,
                        "readback_remote_id": rid,
                        "downloaded_file": str(path),
                    },
                    Registry(),
                )
                uploads[rid] = str(path)

            for item in initial["artifacts"]:
                upload(item["artifact_id"])
            self.assertEqual(
                transition.resume_google({**base, "stage": "prepare-cas"}, Registry())[
                    "outcome"
                ],
                "manifest_ready",
            )
            upload("manifest")
            plan = transition.resume_google(
                {**base, "stage": "prepare-cas"}, Registry()
            )
            sent = transition.resume_google(
                {**base, "stage": "begin-cas", "batch_hash": plan["batch_hash"]},
                Registry(),
            )
            with self.assertRaises(core.RecordError):
                transition.resume_google(
                    {**base, "stage": "begin-cas", "batch_hash": plan["batch_hash"]},
                    Registry(),
                )
            after = test_connector_protocol.update_doc(doc, sent["batch"])
            after["revisionId"] = "rev2"
            result = transition.resume_google(
                {
                    **base,
                    "stage": "reconcile-cas",
                    "document": after,
                    "manifest_readback_file": uploads["resume-manifest"],
                },
                Registry(),
            )
            self.assertEqual(result["outcome"], "committed")
            self.assertEqual(result["head"]["control"]["phase"], "steady")
            self.assertEqual(result["head"]["control"]["epoch"], 2)
            self.assertEqual(result["head"]["version"], head["version"] + 1)
            import json

            graph = json.loads(
                pathlib.Path(uploads["resume-manifest"]).read_text(encoding="utf-8")
            )
            refs = (
                graph["history_pages"]
                + [r["object"] for r in graph["records"]]
                + [a["object"] for a in graph["attachments"]]
            )
            all_files = {**f.uploads, **uploads}
            existing = {
                "manifest_file": uploads["resume-manifest"],
                "objects": {
                    r["id"]: all_files[r["id"]]
                    for r in refs
                    if r.get("encoding") != "inline-empty-v1"
                },
            }
            req = {
                "schema_version": 1,
                "request_id": "resume-google",
                "project_id": "Demo",
                "actor_role": "pm",
                "scope": {"project_id": "Demo", "actions": ["resume"], "records": []},
                "operation_id": "resume-google",
                "action": "resume",
                "target_record_id": None,
                "payload": {
                    "transition_id": "google-move",
                    "transition_intent_sha256": core.object_hash(intent),
                },
                "expected": {
                    "mode": "google",
                    "phase": "frozen",
                    "epoch": 2,
                    "version": head["version"],
                },
            }
            binding["route_phase"] = "steady"
            replay = {
                "private_dir": str(f.root),
                "request": req,
                "document": after,
                "document_id": "doc",
                "head_tab_id": "machine",
                "summary_tab_id": "human",
                "existing": existing,
            }
            self.assertEqual(
                p.managed_dispatch("prepare", replay, Registry())["outcome"], "no_write"
            )
            forged = copy.deepcopy(replay)
            forged["request"]["payload"]["transition_id"] = "different"
            with self.assertRaisesRegex(core.RecordError, "ID_REUSE"):
                p.managed_dispatch("prepare", forged, Registry())
            forged = copy.deepcopy(replay)
            forged["request"]["operation_id"] = "new-resume"
            with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"):
                p.managed_dispatch("prepare", forged, Registry())
        finally:
            f.tearDown()

    def test_git_to_google_all_steps_without_state_edits(self):
        import json
        import test_git_store
        import git_store

        p = __import__("connector_protocol")
        fixture = test_git_store.TestGitStore("test_round_trip_preview_apply_replay")
        fixture.setUp()
        try:
            root = fixture.root
            project_id = "Legacy_Project"
            source = {
                "mode": "git",
                "phase": "steady",
                "epoch": 1,
                "version": 0,
                "resource_id": core.sha(str(fixture.project.resolve()).encode()),
            }
            target = {"mode": "google", "resource_id": "doc", "manifest_sha256": None}
            key = {"private_dir": str(root), "transition_id": "positive"}
            transition.begin(
                {
                    **key,
                    "project_id": project_id,
                    "actor_role": "pm",
                    "source": source,
                    "target": target,
                    "writers": ["writer"],
                }
            )

            def issue(step):
                sent = transition.issue({**key, "operation_id": "step-" + step})
                self.assertEqual(sent["intent"]["step"], step)
                return sent

            def observe(sent, value):
                return transition.observe(
                    {**key, "intent_hash": sent["intent_hash"], "observation": value}
                )

            frozen = issue("freeze")
            req = copy.deepcopy(fixture.req)
            req.update(
                action="freeze",
                target_record_id=None,
                payload={},
                operation_id="step-freeze",
            )
            req["scope"]["actions"] = ["freeze"]
            req["actor_role"] = "pm"
            plan = git_store.preview(fixture.project, fixture.runtime, req, {})
            git_store.apply(
                fixture.project,
                fixture.runtime,
                req,
                {},
                plan["plan"],
                plan["plan_hash"],
            )
            observe(
                frozen,
                {
                    "resource_id": source["resource_id"],
                    "mode": "git",
                    "phase": "frozen",
                    "epoch": 2,
                    "version": 1,
                },
            )
            drain = issue("drain")
            observe(
                drain,
                {
                    "inflight": [],
                    "writer_attestations": {
                        "writer": {
                            "state": "caller_verified_stopped",
                            "evidence_sha256": "b" * 64,
                        }
                    },
                },
            )
            captured = snapshot.git_capture(
                {
                    "private_dir": str(root),
                    "approved_root": str(root),
                    "project_dir": str(fixture.project),
                    "runtime_dir": str(fixture.runtime),
                    "snapshot_id": "git-source",
                    "request": req,
                }
            )
            snap_intent = issue("snapshot")
            observe(
                snap_intent,
                {
                    "snapshot_id": "git-source",
                    "receipt_sha256": core.object_hash(captured["receipt"]),
                },
            )
            _, rs = snapshot.read_git(root, "git-source")
            scopes = [
                {
                    "record_id": r["record_id"],
                    "path": r["source"]["relative_path"],
                    "fields": ["body"],
                }
                for r in rs["records"]
            ]
            bootstrap = {
                "schema_version": 1,
                "request_id": "boot",
                "project_id": project_id,
                "actor_role": "pm",
                "scope": {
                    "project_id": project_id,
                    "actions": ["replace"],
                    "records": scopes,
                },
                "operation_id": "boot",
                "action": "bootstrap",
                "target_record_id": None,
                "payload": {"recordset_sha256": core.object_hash(rs)},
                "expected": {
                    "mode": "google",
                    "phase": "frozen",
                    "epoch": 2,
                    "version": 1,
                },
            }
            document = test_connector_protocol.doc()
            docargs = {
                "document": document,
                "document_id": "doc",
                "head_tab_id": "machine",
                "summary_tab_id": "human",
            }
            bkey = {
                "private_dir": str(root),
                "project_id": project_id,
                "operation_id": "boot",
            }
            uploads = {}
            with (
                patch.object(p, "scan_bytes", side_effect=test_connector_protocol.scan),
                patch.object(
                    p.privacy, "check", side_effect=test_connector_protocol.batch_scan
                ),
            ):
                staged = p.prepare(
                    {
                        **bkey,
                        **docargs,
                        "request": bootstrap,
                        "recordset": rs,
                        "privacy_config": {},
                    }
                )

                def upload(aid):
                    sent = p.begin_upload({**bkey, "artifact_id": aid})
                    raw = pathlib.Path(sent["file_uri"]).read_bytes()
                    path = root / ("positive-" + aid)
                    path.write_bytes(raw)
                    rid = "positive-" + aid
                    p.accept_upload(
                        {
                            **bkey,
                            "artifact_id": aid,
                            "remote_id": rid,
                            "readback_remote_id": rid,
                            "downloaded_file": str(path),
                        }
                    )
                    uploads[rid] = str(path)

                for item in staged["artifacts"]:
                    upload(item["artifact_id"])
                p.prepare_cas(bkey)
                upload("manifest")
                ready = p.prepare_cas(bkey)
                batch = p.begin_cas({**bkey, "batch_hash": ready["batch_hash"]})[
                    "batch"
                ]
                after = test_connector_protocol.update_doc(document, batch)
                committed = p.reconcile_cas(
                    {
                        **bkey,
                        "document": after,
                        "manifest_readback_file": uploads["positive-manifest"],
                    }
                )
                root_hash = committed["head"]["root_manifest"]["sha256"]
                self.assertIsNone(transition.inspect(key)["pending"])
                target_intent = issue("target_frozen")
                self.assertIsNone(target_intent["intent"]["target"]["manifest_sha256"])
                existing = {
                    "manifest_file": uploads["positive-manifest"],
                    "objects": {
                        k: v for k, v in uploads.items() if k != "positive-manifest"
                    },
                }
                observation = {
                    "resource_id": "doc",
                    "mode": "google",
                    "phase": "frozen",
                    "epoch": 2,
                    "version": 2,
                    "manifest_sha256": root_hash,
                    "snapshot_receipt_sha256": core.object_hash(captured["receipt"]),
                    "document": after,
                    "head_tab_id": "machine",
                    "summary_tab_id": "human",
                    "existing": existing,
                }
                bad = copy.deepcopy(observation)
                bad["manifest_sha256"] = "f" * 64
                with self.assertRaises(core.RecordError):
                    observe(target_intent, bad)
                observe(target_intent, observation)
                route = issue("route_frozen")
                self.assertEqual(
                    route["intent"]["target"]["manifest_sha256"], root_hash
                )
                observe(
                    route,
                    {
                        "resource_id": "doc",
                        "mode": "google",
                        "route_phase": "frozen",
                        "registry_sha256": "c" * 64,
                    },
                )
                pending = issue("resume")
                binding = {
                    "backend": "google_head",
                    "route_phase": "frozen",
                    "registry_sha256": "c" * 64,
                    "locator": {"document_id": "doc"},
                    "runtime_directory": str(root),
                    "privacy_policy": "policy",
                    "policy_version": "1",
                }

                class Registry:
                    def require_no_activation(self, project):
                        pass

                    def bind(self, project):
                        return binding

                base = {
                    **key,
                    "intent_hash": pending["intent_hash"],
                    "document": after,
                    "head_tab_id": "machine",
                    "summary_tab_id": "human",
                }
                staged = transition.resume_google(
                    {**base, "stage": "prepare", "existing": existing}, Registry()
                )
                newuploads = {}

                def resume_upload(aid):
                    sent = transition.resume_google(
                        {**base, "stage": "begin-upload", "artifact_id": aid},
                        Registry(),
                    )
                    raw = pathlib.Path(sent["file_uri"]).read_bytes()
                    path = root / ("resume2-" + aid)
                    path.write_bytes(raw)
                    rid = "resume2-" + aid
                    transition.resume_google(
                        {
                            **base,
                            "stage": "accept-upload",
                            "artifact_id": aid,
                            "remote_id": rid,
                            "readback_remote_id": rid,
                            "downloaded_file": str(path),
                        },
                        Registry(),
                    )
                    newuploads[rid] = str(path)

                for item in staged["artifacts"]:
                    resume_upload(item["artifact_id"])
                transition.resume_google({**base, "stage": "prepare-cas"}, Registry())
                resume_upload("manifest")
                planned = transition.resume_google(
                    {**base, "stage": "prepare-cas"}, Registry()
                )
                sent = transition.resume_google(
                    {**base, "stage": "begin-cas", "batch_hash": planned["batch_hash"]},
                    Registry(),
                )
                final = test_connector_protocol.update_doc(after, sent["batch"])
                final["revisionId"] = "rev2"
                result = transition.resume_google(
                    {
                        **base,
                        "stage": "reconcile-cas",
                        "document": final,
                        "manifest_readback_file": newuploads["resume2-manifest"],
                    },
                    Registry(),
                )
                graph = json.loads(
                    pathlib.Path(newuploads["resume2-manifest"]).read_text(
                        encoding="utf-8"
                    )
                )
                refs = (
                    graph["history_pages"]
                    + [e["object"] for e in graph["records"]]
                    + [e["object"] for e in graph["attachments"]]
                )
                files = {**uploads, **newuploads}
                latest = {
                    "manifest_file": newuploads["resume2-manifest"],
                    "objects": {
                        r["id"]: files[r["id"]]
                        for r in refs
                        if r.get("encoding") != "inline-empty-v1"
                    },
                }
                done = observe(
                    pending,
                    {
                        "resource_id": "doc",
                        "mode": "google",
                        "phase": "steady",
                        "epoch": 2,
                        "version": 3,
                        "document": final,
                        "head_tab_id": "machine",
                        "summary_tab_id": "human",
                        "existing": latest,
                    },
                )
                self.assertEqual(result["outcome"], "committed")
                self.assertIsNone(done["next_step"])
        finally:
            fixture.tearDown()

    def test_full_isolated_transition(self):
        self.step(
            "freeze",
            {
                "resource_id": "doc",
                "mode": "google",
                "phase": "frozen",
                "epoch": 2,
                "version": 2,
            },
        )
        self.step(
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
        doc = copy.deepcopy(self.f.document)
        line = doc["tabs"][0]["body"]["content"][1]["paragraph"]["elements"][0][
            "textRun"
        ]
        h = __import__("json").loads(line["content"][len(google_docs.HEAD) :])
        h["control"]["phase"] = "frozen"
        h["control"]["epoch"] = 2
        line["content"] = google_docs.head_line(h) + "\n"
        key = {"private_dir": str(self.root), "snapshot_id": "frozen"}
        snapshot.begin(
            {
                **key,
                "document": doc,
                "document_id": "doc",
                "head_tab_id": "machine",
                "summary_tab_id": "human",
                "project_id": "Demo",
            }
        )
        for rid in self.f.refs:
            path = self.root / ("fresh-" + rid)
            path.write_bytes(self.f.raw[rid])
            snapshot.accept(
                {
                    **key,
                    "reference": self.f.refs[rid],
                    "readback_remote_id": rid,
                    "downloaded_file": str(path),
                }
            )
        receipt = snapshot.finish({**key, "document": doc})["receipt"]
        rh = core.object_hash(receipt)
        self.step("snapshot", {"snapshot_id": "frozen", "receipt_sha256": rh})
        approved = self.root / "approved"
        approved.mkdir()
        target = approved / "target"
        target.mkdir()
        args = {
            "private_dir": str(self.root),
            "snapshot_id": "frozen",
            "approved_root": str(approved),
            "target_dir": str(target),
        }
        plan = restore.preview(args)
        restore.run({**args, "plan": plan["plan"], "plan_hash": plan["plan_hash"]})
        runtime = approved / "runtime"
        runtime.mkdir()
        runtime_args = {
            "private_dir": str(self.root),
            "snapshot_id": "frozen",
            "approved_root": str(approved),
            "runtime_dir": str(runtime),
        }
        runtimeplan = restore.runtime_preview(runtime_args)
        restore.runtime_apply(
            {
                **runtime_args,
                "plan": runtimeplan["plan"],
                "plan_hash": runtimeplan["plan_hash"],
            }
        )
        import promotion

        source_runtime = runtime
        runtime = approved / "fixed-runtime"
        runtime.mkdir()
        transition.git_store.write_json(
            runtime / "control.json",
            {
                "mode": "git",
                "phase": "steady",
                "epoch": 1,
                "version": 0,
                "policy_version": "1",
            },
        )
        binding = {
            "backend": "git_local",
            "route_phase": "frozen",
            "runtime_directory": str(runtime),
            "project_directory": str(target),
            "privacy_policy": "policy",
            "policy_version": "1",
            "registry_sha256": "c" * 64,
        }

        class PromotionRegistry:
            def require_no_activation(self, project):
                pass

            def bind(self, project):
                return binding

        target_intent = transition.issue({**self.k, "operation_id": "op-target_frozen"})
        promotion_req = {
            "transition_id": self.k["transition_id"],
            "transition_intent_sha256": target_intent["intent_hash"],
            "actor_role": "pm",
            "project_id": "Demo",
            "operation_id": "promote",
            "private_dir": str(self.root),
            "approved_root": str(approved),
            "source_runtime": str(source_runtime),
            "source_plan_hash": runtimeplan["plan_hash"],
            "snapshot_id": "frozen",
        }
        promotion_plan = promotion.prepare(promotion_req, PromotionRegistry())
        promotion.apply(
            {**promotion_req, "plan_hash": promotion_plan["plan_hash"]},
            PromotionRegistry(),
        )
        transition.observe(
            {
                **self.k,
                "intent_hash": target_intent["intent_hash"],
                "observation": {
                    "resource_id": "target",
                    "mode": "git",
                    "phase": "frozen",
                    "epoch": 2,
                    "version": 2,
                    "manifest_sha256": self.target["manifest_sha256"],
                    "snapshot_receipt_sha256": rh,
                    "plan_hash": plan["plan_hash"],
                    "approved_root": str(approved),
                    "target_dir": str(target),
                    "runtime_dir": str(runtime),
                    "runtime_plan_hash": runtimeplan["plan_hash"],
                    "promotion_operation": "promote",
                    "promotion_plan_hash": promotion_plan["plan_hash"],
                },
            },
        )
        self.step(
            "route_frozen",
            {
                "resource_id": "target",
                "mode": "git",
                "route_phase": "frozen",
                "registry_sha256": "c" * 64,
            },
        )
        issued = transition.issue({**self.k, "operation_id": "op-resume"})
        observation = {
            "resource_id": "target",
            "mode": "git",
            "phase": "steady",
            "epoch": 2,
            "version": 3,
        }
        with self.assertRaisesRegex(core.RecordError, "CONTROL_UNAVAILABLE"):
            transition.observe(
                {
                    **self.k,
                    "intent_hash": issued["intent_hash"],
                    "observation": observation,
                }
            )
        binding = {
            "backend": "git_local",
            "route_phase": "frozen",
            "runtime_directory": str(runtime),
            "project_directory": str(target),
            "privacy_policy": "policy",
            "policy_version": "1",
            "registry_sha256": "c" * 64,
        }

        class Registry:
            def require_no_activation(self, project):
                pass

            def bind(self, project):
                return binding

        with patch.object(
            transition.git_store,
            "privacy_check",
            return_value={
                "content_sha256": "a",
                "policy_sha256": "b",
                "rules_sha256": "c",
            },
        ):
            write = transition.git_store.write_json

            def crash_before_control(path, value):
                if pathlib.Path(path).name == "control.json":
                    raise OSError("synthetic interruption")
                return write(path, value)

            with patch.object(
                transition.git_store, "write_json", side_effect=crash_before_control
            ):
                with self.assertRaises(OSError):
                    transition.resume_git(
                        {**self.k, "intent_hash": issued["intent_hash"]}, Registry()
                    )
            self.assertEqual(
                transition.git_store.get_control(runtime)["phase"], "frozen"
            )
            result = transition.resume_git(
                {**self.k, "intent_hash": issued["intent_hash"]}, Registry()
            )
            self.assertEqual(result["outcome"], "complete")
            self.assertEqual(
                transition.resume_git(
                    {**self.k, "intent_hash": issued["intent_hash"]}, Registry()
                )["outcome"],
                "no_write",
            )
        self.assertEqual(result["route_phase"], "frozen")
        self.assertEqual(
            transition.git_store.preview(target, runtime, result["request"], {})[
                "result"
            ],
            result["result"],
        )
        worker = copy.deepcopy(result["request"])
        worker["actor_role"] = "worker"
        with self.assertRaises(core.RecordError):
            core.validate_request(worker, True)
        journal_path = transition.git_store.operation_path(runtime, "op-resume")
        original_entry = transition.git_store.read_operation(runtime, "op-resume")
        forged_entry = copy.deepcopy(original_entry)
        forged_entry["event"]["request_hash"] = "f" * 64
        transition.git_store.write_json(journal_path, forged_entry)
        with self.assertRaisesRegex(core.RecordError, "CONTROL_UNAVAILABLE"):
            transition.observe(
                {
                    **self.k,
                    "intent_hash": issued["intent_hash"],
                    "observation": observation,
                }
            )
        transition.git_store.write_json(journal_path, original_entry)
        done = transition.observe(
            {**self.k, "intent_hash": issued["intent_hash"], "observation": observation}
        )
        self.assertIsNone(done["next_step"])
        self.assertFalse(done["physical_fence_independently_verified"])


if __name__ == "__main__":
    unittest.main()
