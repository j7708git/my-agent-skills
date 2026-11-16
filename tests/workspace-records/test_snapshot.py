import copy, pathlib, sys, tempfile, unittest

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import core, git_store, google_docs, snapshot, test_git_store
from test_google_docs import doc, head


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.refs = {}
        self.raw = {}

        def put(rid, raw):
            ref = {"id": rid, "sha256": core.sha(raw), "size": len(raw)}
            self.refs[rid] = ref
            self.raw[rid] = raw
            return ref

        self.put = put
        data = ("中文\r\n" * 270000).encode()
        old = put("oldblob", b"old\r\n")
        new = put("newblob", data)
        keep = {
            "id": "inline-empty-v1",
            "sha256": core.sha(b""),
            "size": 0,
            "encoding": "inline-empty-v1",
        }

        def record(ref):
            return {
                "record": {
                    "record_id": "record",
                    "kind": "research",
                    "created_at": None,
                    "updated_at": None,
                    "source": {
                        "relative_path": "research/a.md",
                        "content_sha256": ref["sha256"],
                        "original_time": None,
                    },
                    "body": {"format": "utf-8"},
                    "metadata": {},
                    "relations": [],
                },
                "object": ref,
            }

        attachment = {
            "attachment": {
                "attachment_id": "keep",
                "source_relative_path": "tasks/.gitkeep",
                "sha256": keep["sha256"],
                "size": 0,
            },
            "object": keep,
        }
        oldpage = put(
            "oldpage",
            core.canonical(
                {
                    "schema_version": 1,
                    "events": [],
                    "journal": [],
                    "previous_root": None,
                }
            ),
        )
        oldmanifest = {
            "schema_version": 1,
            "project_id": "Demo",
            "version": 1,
            "records": [record(old)],
            "attachments": [attachment],
            "history_pages": [oldpage],
            "source_root": {},
        }
        oldroot = put("oldroot", core.canonical(oldmanifest))
        page = put(
            "page",
            core.canonical(
                {
                    "schema_version": 1,
                    "events": [],
                    "journal": [],
                    "previous_root": oldroot,
                }
            ),
        )
        newmanifest = {
            "schema_version": 1,
            "project_id": "Demo",
            "version": 2,
            "records": [record(new)],
            "attachments": [attachment],
            "history_pages": [oldpage, page],
            "source_root": {},
        }
        current = put("current", core.canonical(newmanifest))
        h = head()
        h["root_manifest"] = current
        h["version"] = 2
        self.document = doc()

        def lines(values):
            return [
                {"paragraph": {"elements": [{"textRun": {"content": v + "\n"}}]}}
                for v in values
            ]

        self.document["tabs"][0]["body"]["content"][1:1] = lines(
            [google_docs.head_line(h)]
        )
        self.document["tabs"][1]["body"]["content"][1:1] = lines(
            google_docs.summary_lines(h)
        )
        self.key = {"private_dir": str(self.root), "snapshot_id": "snap"}
        snapshot.begin(
            {
                **self.key,
                "document": self.document,
                "document_id": "doc",
                "head_tab_id": "machine",
                "summary_tab_id": "human",
                "project_id": "Demo",
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def accept(self, rid):
        path = self.root / (rid + ".dat")
        path.write_bytes(self.raw[rid])
        return snapshot.accept(
            {
                **self.key,
                "reference": self.refs[rid],
                "readback_remote_id": rid,
                "downloaded_file": str(path),
            }
        )

    def test_complete_all_history_and_bytes(self):
        for rid in self.refs:
            self.accept(rid)
        self.assertEqual(self.accept("oldblob")["outcome"], "no_write")
        receipt = snapshot.finish({**self.key, "document": self.document})["receipt"]
        self.assertEqual(len(receipt["objects"]), len(self.refs) + 1)
        self.assertEqual(len(receipt["manifests"]), 2)
        self.assertEqual(receipt["future_gap"], "unknown")

    def test_missing_or_changed_source_never_complete(self):
        for rid in self.refs:
            if rid != "oldblob":
                self.accept(rid)
        with self.assertRaises(core.RecordError):
            snapshot.finish({**self.key, "document": self.document})
        changed = copy.deepcopy(self.document)
        changed["revisionId"] = "other"
        with self.assertRaisesRegex(core.RecordError, "REVISION_CONFLICT"):
            snapshot.finish({**self.key, "document": changed})
        self.assertEqual(snapshot.inspect(self.key)["outcome"], "collecting")

    def test_500_distinct_synthetic_files(self):
        records = []
        for i in range(500):
            data = (f"# 任務 {i}\r\n|狀態|完成|\r\n").encode("utf-8")
            ref = self.put(f"bulk{i}", data)
            rec = {
                "record_id": f"r{i}",
                "kind": "task",
                "created_at": None,
                "updated_at": None,
                "source": {
                    "relative_path": f"tasks/{i}.md",
                    "content_sha256": ref["sha256"],
                    "original_time": None,
                },
                "body": {"format": "utf-8"},
                "metadata": {},
                "relations": [],
            }
            records.append({"record": rec, "object": ref})
        value = {
            "schema_version": 1,
            "project_id": "Demo",
            "version": 3,
            "records": records,
            "attachments": [],
            "history_pages": [],
            "source_root": {},
        }
        rootref = self.put("bulkroot", core.canonical(value))
        document = copy.deepcopy(self.document)
        run = document["tabs"][0]["body"]["content"][1]["paragraph"]["elements"][0][
            "textRun"
        ]
        h = __import__("json").loads(run["content"][len(google_docs.HEAD) :])
        h["root_manifest"] = rootref
        h["version"] = 3
        run["content"] = google_docs.head_line(h) + "\n"
        for item in document["tabs"][1]["body"]["content"][1:]:
            run = item["paragraph"]["elements"][0]["textRun"]
            if run["content"].startswith(google_docs.SUMMARY + "version]]"):
                run["content"] = google_docs.summary_lines(h)[-1] + "\n"
        key = {"private_dir": str(self.root), "snapshot_id": "bulk"}
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
        for rid in ["bulkroot"] + [f"bulk{i}" for i in range(500)]:
            path = self.root / (rid + ".download")
            path.write_bytes(self.raw[rid])
            snapshot.accept(
                {
                    **key,
                    "reference": self.refs[rid],
                    "readback_remote_id": rid,
                    "downloaded_file": str(path),
                }
            )
        receipt = snapshot.finish({**key, "document": document})["receipt"]
        self.assertEqual(len(receipt["objects"]), 501)
        import restore

        approved = self.root / "bulk-approved"
        approved.mkdir()
        target = approved / "output"
        target.mkdir()
        args = {
            "private_dir": str(self.root),
            "snapshot_id": "bulk",
            "approved_root": str(approved),
            "target_dir": str(target),
        }
        plan = restore.preview(args)
        result = restore.run(
            {**args, "plan": plan["plan"], "plan_hash": plan["plan_hash"]}
        )
        self.assertEqual(result["files"], 500)
        for i in range(500):
            self.assertEqual(
                (target / f"tasks/{i}.md").read_bytes(), self.raw[f"bulk{i}"]
            )

    def test_head_manifest_version_mismatch_refused(self):
        for rid in self.refs:
            self.accept(rid)
        directory, state = snapshot.load(self.root, "snap")
        state["source"]["head"]["version"] = 3
        with self.assertRaises(core.RecordError):
            snapshot.verify_graph(directory, state)

    def test_frozen_git_capture_and_double_read(self):
        fixture = test_git_store.TestGitStore("test_round_trip_preview_apply_replay")
        fixture.setUp()
        try:
            req = copy.deepcopy(fixture.req)
            req.update(
                action="freeze",
                target_record_id=None,
                payload={},
                operation_id="freeze",
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
            args = {
                "private_dir": str(fixture.root),
                "approved_root": str(fixture.root),
                "project_dir": str(fixture.project),
                "runtime_dir": str(fixture.runtime),
                "snapshot_id": "git-snap",
                "request": req,
            }
            result = snapshot.git_capture(args)
            self.assertEqual(result["receipt"]["source_epoch"], 2)
            state, recordset = snapshot.read_git(fixture.root, "git-snap")
            self.assertEqual(recordset["control"]["phase"], "frozen")
            self.assertEqual(recordset["version"], 1)
            self.assertEqual(recordset["journal"][0]["operation_id"], "freeze")
        finally:
            fixture.tearDown()

    def test_badhash_rejected(self):
        path = self.root / "bad"
        path.write_bytes(b"changed")
        with self.assertRaises(core.RecordError):
            snapshot.accept(
                {
                    **self.key,
                    "reference": self.refs["oldblob"],
                    "readback_remote_id": "oldblob",
                    "downloaded_file": str(path),
                }
            )


if __name__ == "__main__":
    unittest.main()
