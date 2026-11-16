import copy
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
sys.path.insert(0, str(SCRIPTS))
import core, detail_projection as detail, git_store, google_docs, manifest, routing
from test_core import STATUS
from test_google_docs import doc, head
from test_connector_protocol import scan


class TestDetails(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.tmp.name)
        (self.root / "project-notes/projects/Demo").mkdir(parents=True)
        (self.root / routing.RUNTIME).mkdir(parents=True)
        (self.root / routing.POLICY).write_text("{}", encoding="utf-8")
        self.routes = {"schema_version": 1, "policy_version": "1", "generation": 1,
            "projects": {"Demo": {"classification": "managed", "backend": "google_head", "route_phase": "steady", "locator": {"document_id": "doc"}}}}
        (self.root / routing.REGISTRY).write_text(json.dumps(self.routes), encoding="utf-8")
        self.registry = routing.Registry(self.root)
        self.runtime = pathlib.Path(self.registry.bind("Demo")["runtime_directory"]); self.runtime.mkdir()
        self.scanner = patch.object(detail.protocol, "scan_bytes", side_effect=scan); self.scanner.start()
        self.remote = {}; self.parents = {}; self.counter = 0
        self.make_source(1)

    def tearDown(self):
        self.scanner.stop(); self.tmp.cleanup()

    def make_source(self, version, extra=False, research=None):
        raw = STATUS.replace(b"now: First", ("now: generation " + str(version)).encode())
        ref = {"id": "raw-status-" + str(version), "size": len(raw), "sha256": core.sha(raw)}
        path = self.runtime / (ref["id"] + ".download"); path.write_bytes(raw)
        record = {"record_id": "status", "kind": "status", "created_at": None, "updated_at": None,
                  "source": {"relative_path": "status.md", "content_sha256": ref["sha256"], "original_time": None},
                  "body": {"format": "utf-8"}, "metadata": {}, "relations": []}
        empty = {"id": "inline-empty-v1", "size": 0, "sha256": core.sha(b""), "encoding": "inline-empty-v1"}
        attachment = {"attachment_id": "keep", "source_relative_path": "tasks/.gitkeep", "size": 0,
                      "sha256": empty["sha256"], "mime": "application/octet-stream"}
        graph = {"schema_version": 1, "project_id": "Demo", "version": version,
                 "records": [{"record": record, "object": ref}], "attachments": [{"attachment": attachment, "object": empty}],
                 "history_pages": [], "source_root": {}}
        objects = {ref["id"]: str(path)}
        if extra:
            eraw = b"extra task"; eref = {"id": "extra-raw", "size": len(eraw), "sha256": core.sha(eraw)}
            ep = self.runtime / "extra.download"; ep.write_bytes(eraw); objects[eref["id"]] = str(ep)
            record2 = copy.deepcopy(record); record2.update(record_id="extra", kind="task")
            record2["source"].update(relative_path="tasks/extra.md", content_sha256=eref["sha256"])
            graph["records"].append({"record": record2, "object": eref})
        if research is not None:
            lref = {"id": "large-raw", "size": len(research), "sha256": core.sha(research)}
            lp = self.runtime / "large.download"; lp.write_bytes(research); objects[lref["id"]] = str(lp)
            large = copy.deepcopy(record); large.update(record_id="large", kind="research")
            large["source"].update(relative_path="research/large.md", content_sha256=lref["sha256"])
            graph["records"].append({"record": large, "object": lref})
        rootraw = core.canonical(graph); rootpath = self.runtime / ("manifest-" + str(version)); rootpath.write_bytes(rootraw)
        h = head(); h["version"] = version; h["root_manifest"] = {"id": "root-" + str(version), "sha256": core.sha(rootraw), "size": len(rootraw)}
        h["summary"] = detail.protocol.status_summary(graph["records"], {ref["id"]: raw})
        d = doc(); d["revisionId"] = "rev" + str(version)
        def lines(items): return [{"paragraph": {"elements": [{"textRun": {"content": item + "\n"}}]}} for item in items]
        d["tabs"][0]["body"]["content"][1:1] = lines([google_docs.head_line(h)])
        d["tabs"][1]["body"]["content"][1:1] = lines(google_docs.summary_lines(h))
        self.x = {"project_id": "Demo", "actor_role": "pm", "operation_id": "projection-" + str(version),
                  "scope": {"project_id": "Demo", "record_ids": sorted([e["record"]["record_id"] for e in graph["records"]] + ["keep"]), "folder_id": "current-folder", "archive_folder_id": "archive-folder"},
                  "document": d, "head_tab_id": "machine", "summary_tab_id": "human",
                  "existing": {"manifest_file": str(rootpath), "objects": objects}}

    def observation(self):
        state = detail.load(self.runtime, "Demo"); active = state["active"]
        item = next((i for i in active["items"] if i["status"] == "ready"), None)
        if item is None and active["phase"] == "details": existing = state["index"]
        elif item: existing = state["files"].get(item.get("file_key")) if "file_key" in item else state["index"]
        else: existing = None
        if not existing: return {}
        path = self.runtime / "observed.download"; path.write_bytes(self.remote[existing["file_id"]])
        return {"readback_remote_id": existing["file_id"], "observed_parent_id": self.parents[existing["file_id"]], "downloaded_file": str(path)}

    def send(self):
        result = detail.begin({**self.x, **self.observation()}, self.registry)
        if result["outcome"] == "no_write": return result
        return self.settle(result)

    def settle(self, result):
        payload = result["payload"]
        if result["action"] == "upload_file":
            self.counter += 1; rid = "copy-" + str(self.counter)
            self.parents[rid] = payload["parent_folder_id"]
        else: rid = payload["fileId"]
        if "file_uri" in payload:
            self.remote[rid] = pathlib.Path(payload["file_uri"]).read_bytes()
        else:
            self.assertEqual(self.parents[rid], payload["removeParents"])
            self.parents[rid] = payload["addParents"]
        path = self.runtime / "accepted.download"; path.write_bytes(self.remote[rid])
        return detail.accept({**self.x, "readback_remote_id": rid, "observed_parent_id": self.parents[rid], "downloaded_file": str(path)}, self.registry)

    def finish(self):
        for _ in range(2 * len(detail.load(self.runtime, "Demo")["active"]["items"]) + 4):
            result = self.send()
            if result.get("phase") == "complete": return result
        self.fail("generation did not complete")

    def test_index_stale_first_current_last_and_fixed_ids_across_generation(self):
        detail.prepare(self.x, self.registry)
        self.assertEqual(git_store.journal_entries(self.runtime), [])
        self.assertEqual(self.send()["phase"], "details")
        state = detail.load(self.runtime, "Demo")
        self.assertIn(b"stale", self.remote[state["index"]["file_id"]])
        self.assertEqual(self.finish()["freshness"], "current")
        state = detail.load(self.runtime, "Demo"); ids = {k: v["file_id"] for k, v in state["files"].items()}; indexid = state["index"]["file_id"]
        self.assertIn("原附件為空檔，還原時保留。", self.remote[ids["keep:1"]].decode())
        self.assertNotIn("/inline-empty-v1/", self.remote[ids["keep:1"]].decode())
        self.assertIn("閱讀狀態：已更新", self.remote[indexid].decode())
        import re
        visible = re.sub(r"<!--.*?-->", "", self.remote[indexid].decode(), flags=re.S)
        for internal in ("Doc head", "manifest", "epoch", "root_hash", "current generation", "descriptor"):
            self.assertNotIn(internal, visible)
        self.assertIn("透過 Codex 更新", visible)
        self.make_source(2); detail.prepare(self.x, self.registry)
        required = detail.begin(self.x, self.registry)
        self.assertEqual(required["outcome"], "readback_required")
        self.assertEqual(required["file_id"], indexid)
        self.assertTrue(detail.dispatch("inspect", self.x, self.registry)["pending"])
        self.finish()
        state = detail.load(self.runtime, "Demo")
        self.assertEqual({k: v["file_id"] for k, v in state["files"].items()}, ids)
        self.assertEqual(state["index"]["file_id"], indexid)
        self.assertEqual(self.counter, 3)

    def test_unknown_never_blind_retries_or_starts_next_generation(self):
        detail.prepare(self.x, self.registry); detail.begin(self.x, self.registry)
        with self.assertRaisesRegex(core.RecordError, "MANUAL_REVIEW"): detail.begin(self.x, self.registry)
        self.make_source(2)
        with self.assertRaisesRegex(core.RecordError, "MANUAL_REVIEW"): detail.prepare(self.x, self.registry)

    def test_accept_unknown_after_source_advanced_settles_stale_without_publish(self):
        detail.prepare(self.x, self.registry); sent = detail.begin(self.x, self.registry)
        raw = pathlib.Path(sent["payload"]["file_uri"]).read_bytes(); path = self.runtime / "late.download"; path.write_bytes(raw)
        original_scope = copy.deepcopy(self.x["scope"])
        self.make_source(2, extra=True)
        self.x.update(operation_id="projection-1", scope=original_scope)
        result = detail.accept({**self.x, "readback_remote_id": "late-index", "observed_parent_id": "current-folder", "downloaded_file": str(path)}, self.registry)
        self.assertEqual(result["freshness"], "stale"); self.assertEqual(result["phase"], "stale")
        with self.assertRaises(core.RecordError): detail.begin(self.x, self.registry)

    def test_external_current_copy_edit_not_overwritten(self):
        detail.prepare(self.x, self.registry); self.finish()
        indexid = detail.load(self.runtime, "Demo")["index"]["file_id"]
        self.remote[indexid] = b"human edit"
        self.make_source(2); detail.prepare(self.x, self.registry)
        with self.assertRaisesRegex(core.RecordError, "REVISION_CONFLICT"): detail.begin({**self.x, **self.observation()}, self.registry)
        self.assertEqual(self.remote[indexid], b"human edit")

    def test_scope_folder_generation_and_registry_changes_refused(self):
        bad = copy.deepcopy(self.x); bad["scope"]["record_ids"] = ["status"]
        with self.assertRaises(core.RecordError): detail.prepare(bad, self.registry)
        detail.prepare(self.x, self.registry)
        self.make_source(2); self.x["operation_id"] = "projection-1"
        with self.assertRaisesRegex(core.RecordError, "REVISION_CONFLICT"): detail.begin(self.x, self.registry)
        bad = copy.deepcopy(self.x); bad["scope"]["folder_id"] = "other-folder"
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"): detail.prepare(bad, self.registry)
        self.routes["generation"] += 1; (self.root/routing.REGISTRY).write_text(json.dumps(self.routes))
        with self.assertRaises(core.RecordError): detail.begin(self.x, self.registry)

    def test_wrong_download_and_reserved_remote_id_cannot_settle(self):
        detail.prepare(self.x, self.registry); sent = detail.begin(self.x, self.registry)
        path = self.runtime / "wrong.download"; path.write_bytes(b"wrong")
        request = {**self.x, "readback_remote_id": "new-index", "observed_parent_id": "current-folder", "downloaded_file": str(path)}
        with self.assertRaisesRegex(core.RecordError, "INTEGRITY_FAILED"): detail.accept(request, self.registry)
        path.write_bytes(pathlib.Path(sent["payload"]["file_uri"]).read_bytes()); request["readback_remote_id"] = "inline-empty-v1"
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"): detail.accept(request, self.registry)
        self.assertEqual(detail.load(self.runtime,"Demo")["active"]["items"][0]["status"], "unknown")

    def test_operation_id_reuse_and_missing_graph_fail_closed(self):
        detail.prepare(self.x, self.registry); self.finish()
        self.make_source(2); self.x["operation_id"] = "projection-1"
        with self.assertRaisesRegex(core.RecordError, "ID_REUSE"): detail.prepare(self.x, self.registry)
        self.x["existing"]["objects"] = {}
        with self.assertRaises(core.RecordError): detail.prepare(self.x, self.registry)

    def test_different_operation_cannot_begin_accept_or_inspect_active_intent(self):
        detail.prepare(self.x, self.registry)
        wrong = {**self.x, "operation_id": "other-operation"}
        def tree(): return {str(p.relative_to(self.runtime)): p.read_bytes() for p in self.runtime.rglob("*") if p.is_file()}
        before = tree()
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"): detail.begin(wrong, self.registry)
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"): detail.dispatch("inspect", wrong, self.registry)
        for scope in ({**self.x["scope"], "folder_id": "other-folder"}, {**self.x["scope"], "archive_folder_id": "other-archive"}, {**self.x["scope"], "record_ids": ["status"]}):
            with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"): detail.begin({**self.x, "scope": scope}, self.registry)
        self.assertEqual(tree(), before)
        sent = detail.begin(self.x, self.registry)
        path = self.runtime / "accepted.download"; path.write_bytes(pathlib.Path(sent["payload"]["file_uri"]).read_bytes())
        observation = {"readback_remote_id": "new-copy", "observed_parent_id": "current-folder", "downloaded_file": str(path)}
        before = tree()
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"): detail.accept({**wrong, **observation}, self.registry)
        for scope in ({**self.x["scope"], "folder_id": "other-folder"}, {**self.x["scope"], "archive_folder_id": "other-archive"}, {**self.x["scope"], "record_ids": ["status"]}):
            with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"): detail.accept({**self.x, **observation, "scope": scope}, self.registry)
        badscope = {**self.x, "scope": {**self.x["scope"], "record_ids": ["status"]}}
        with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"): detail.dispatch("inspect", badscope, self.registry)
        self.assertEqual(tree(), before)
        self.assertEqual(detail.accept({**self.x, **observation}, self.registry)["phase"], "details")

    def test_large_utf8_chunks_cover_every_byte_and_index_every_part(self):
        raw = ("開頭記號\r\n" + "中文🧪\r\n" * 150000 + "中段記號\r\n" + "中文🧪\r\n" * 150000 + "紫藤星圖九十二\r\n").encode()
        self.make_source(1, research=raw)
        result = detail.prepare(self.x, self.registry)
        self.assertGreater(result["parts"], 20)
        self.finish()
        state = detail.load(self.runtime, "Demo")
        copies = sorted([v for v in state["files"].values() if v["record_id"] == "large"], key=lambda item: item["part"])
        bodies = [self.remote[item["file_id"]].split(b"<!-- workspace-records-body -->\n", 1)[1] for item in copies]
        self.assertEqual(b"".join(bodies), raw)
        self.assertEqual(core.sha(b"".join(bodies)), core.sha(raw))
        for body in bodies:
            self.assertLessEqual(len(body), 128 * 1024); body.decode("utf-8")
            self.assertFalse(body.endswith(b"\r"))
        index = self.remote[state["index"]["file_id"]].decode()
        for item in copies: self.assertIn("/" + item["file_id"] + "/view", index)
        self.assertIn("紫藤星圖九十二", bodies[-1].decode())
        arbitrary = (b"x" * (detail.CHUNK_BODY_BYTES - 1)) + "🧪".encode() + b"\r\nend"
        self.assertEqual(b"".join(part for _, _, part in detail.split_utf8(arbitrary)), arbitrary)

    def test_shrinking_tombstones_and_archives_obsolete_parts_before_current_index(self):
        old_raw = (b"obsolete-data\r\n" * 30000)
        self.make_source(1, research=old_raw); detail.prepare(self.x, self.registry); self.finish()
        before = detail.load(self.runtime, "Demo")
        large = sorted([v for v in before["files"].values() if v["record_id"] == "large"], key=lambda item: item["part"])
        self.assertGreater(len(large), 2)
        first_id = large[0]["file_id"]; obsolete = {v["file_id"] for v in large[1:]}
        self.make_source(2, research=b"small current text\r\n"); detail.prepare(self.x, self.registry)
        intercepted = False
        for _ in range(40):
            sent = detail.begin({**self.x, **self.observation()}, self.registry)
            if "addParents" in sent.get("payload", {}):
                self.assertIn("本段已移除", self.remote[sent["payload"]["fileId"]].decode())
                with self.assertRaisesRegex(core.RecordError, "MANUAL_REVIEW"): detail.begin(self.x, self.registry)
                with self.assertRaisesRegex(core.RecordError, "MANUAL_REVIEW"): detail.prepare(self.x, self.registry)
                state = detail.load(self.runtime, "Demo")
                self.assertIn("閱讀狀態：更新中", self.remote[state["index"]["file_id"]].decode())
                rid = sent["payload"]["fileId"]
                proof = self.runtime / "before-move.download"; proof.write_bytes(self.remote[rid])
                with self.assertRaisesRegex(core.RecordError, "FORBIDDEN"):
                    detail.accept({**self.x, "readback_remote_id": rid, "observed_parent_id": "current-folder", "downloaded_file": str(proof)}, self.registry)
                self.settle(sent); intercepted = True; break
            self.settle(sent)
        self.assertTrue(intercepted)
        self.finish(); after = detail.load(self.runtime, "Demo")
        self.assertEqual(after["files"]["large:1"]["file_id"], first_id)
        self.assertEqual(set(after["archived"]), obsolete)
        index = self.remote[after["index"]["file_id"]].decode()
        for rid in obsolete:
            self.assertEqual(self.parents[rid], "archive-folder")
            self.assertNotIn(b"obsolete-data", self.remote[rid])
            self.assertNotIn("/" + rid + "/view", index)
        self.assertIn("閱讀狀態：已更新", index)

    def legacy(self):
        detail.prepare(self.x, self.registry); self.finish()
        current = detail.load(self.runtime, "Demo")
        old = {k: copy.deepcopy(current[k]) for k in ("project_id", "index", "active", "operations")}
        old.update(schema_version=1, target={"folder_id": "current-folder"},
                   files={v["record_id"]: {k: copy.deepcopy(v[k]) for k in ("file_id", "sha256", "generation", "label")} for v in current["files"].values()})
        old["active"]["scope"].pop("archive_folder_id")
        git_store.write_json(detail.state_file(self.runtime), old)
        return old

    def test_explicit_settled_legacy_upgrade_preserves_ids_and_receipt(self):
        old = self.legacy(); original_ids = {rid: item["file_id"] for rid, item in old["files"].items()}
        self.x["operation_id"] = "upgrade-legacy"
        with self.assertRaisesRegex(core.RecordError, "SCHEMA_UNSUPPORTED"): detail.prepare(self.x, self.registry)
        self.x["legacy_state_sha256"] = "f" * 64
        before = detail.state_file(self.runtime).read_bytes()
        with self.assertRaisesRegex(core.RecordError, "REVISION_CONFLICT"): detail.dispatch("prepare-upgrade", self.x, self.registry)
        self.assertEqual(detail.state_file(self.runtime).read_bytes(), before)
        self.x["legacy_state_sha256"] = core.object_hash(old)
        result = detail.dispatch("prepare-upgrade", self.x, self.registry)
        self.assertEqual(result["outcome"], "prepared"); self.finish()
        state = detail.load(self.runtime,"Demo")
        self.assertEqual({v["record_id"]:v["file_id"] for v in state["files"].values()}, original_ids)
        receipt = state["upgrade_receipts"]["upgrade-legacy"]
        self.assertEqual(git_store.read_json(detail.storage(self.runtime)/receipt["backup_file"]), old)
        self.assertEqual(detail.dispatch("prepare-upgrade",self.x,self.registry)["freshness"],"current")

    def test_unknown_legacy_cannot_upgrade_or_reset(self):
        old = self.legacy(); old["active"]["phase"] = "details"; old["active"]["items"][0]["status"] = "unknown"
        git_store.write_json(detail.state_file(self.runtime),old)
        self.x.update(operation_id="upgrade-unknown",legacy_state_sha256=core.object_hash(old))
        before = {str(p):p.read_bytes() for p in detail.storage(self.runtime).rglob("*") if p.is_file()}
        with self.assertRaisesRegex(core.RecordError,"MANUAL_REVIEW"):detail.dispatch("prepare-upgrade",self.x,self.registry)
        self.assertEqual({str(p):p.read_bytes() for p in detail.storage(self.runtime).rglob("*") if p.is_file()},before)

    def test_cli_real_package_prepare_ascii_and_actual_scanner(self):
        self.scanner.stop()
        package = self.root / ".openchamber/workspace-records/package"; scripts = package / "skills/workspace-records/scripts"; scripts.mkdir(parents=True)
        for name in ("detail_projection.py", "connector_protocol.py", "core.py", "git_store.py", "privacy.py", "routing.py", "google_docs.py", "manifest.py"):
            shutil.copyfile(SCRIPTS/name, scripts/name)
        scanner = package / "skills/repo-privacy-audit/scripts"; scanner.mkdir(parents=True)
        shutil.copyfile(SCRIPTS.parents[1]/"repo-privacy-audit/scripts/audit_core.py", scanner/"audit_core.py")
        shutil.copyfile(SCRIPTS.parents[2]/".gitleaks.toml", package/".gitleaks.toml")
        (self.root/routing.POLICY).write_text(json.dumps({"schemaVersion":"1.0","paths":{"personalPathSeverity":"high"}}))
        result = subprocess.run([sys.executable,"-B",str(scripts/"detail_projection.py"),"prepare"],input=json.dumps(self.x,ensure_ascii=True),text=True,encoding="utf-8",capture_output=True,timeout=30,cwd=self.root.parent)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr); self.assertTrue(result.stdout.isascii())
        self.assertEqual(json.loads(result.stdout)["data"]["outcome"],"prepared")


if __name__ == "__main__": unittest.main()
