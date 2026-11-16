import copy
import json
import shutil
import subprocess
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
sys.path.insert(0, str(SCRIPTS))
import core
import portfolio_refresh as refresh
import routing
import git_store
from test_core import STATUS


class Transport:
    def __init__(self, target):
        self.target = target
        self.rows = {name: {key: name if key == "project_id" else "" for key in refresh.COLUMNS}
                     for name in target["rows"]}
        self.writes = 0
        self.version = 0
        self.failure = None
        self.status = "previous status"

    def read(self, target, nonce):
        self.version += 1
        result = {"request_nonce": nonce, "target_hash": core.object_hash(target),
                  "read_version": str(self.version), "headers": target["columns"],
                  "rows": copy.deepcopy(self.rows)}
        if "status_cell" in target:
            result["status"] = self.status
        return result

    def write(self, payload):
        self.writes += 1
        if self.failure == "429":
            error = RuntimeError()
            error.status = 429
            error.definitely_not_sent = True
            raise error
        for request in payload["requests"]:
            update = request["updateCells"]
            bounds = update["range"]
            if "status_cell" in self.target and bounds["startRowIndex"] == self.target["status_cell"]["row_index"]:
                self.status = update["rows"][0]["values"][0]["userEnteredValue"]["stringValue"]
                continue
            name = next(key for key, index in self.target["rows"].items() if index == bounds["startRowIndex"])
            column = next(key for key, index in self.target["columns"].items() if index == bounds["startColumnIndex"])
            value = update["rows"][0]["values"][0]["userEnteredValue"]
            assert set(value) == {"stringValue"}
            self.rows[name][column] = value["stringValue"]
        if self.failure == "after":
            raise RuntimeError()


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.notes = self.root / "project-notes"
        self.projects = self.notes / "projects"
        (self.projects / "Demo").mkdir(parents=True)
        (self.projects / "Demo/status.md").write_bytes(STATUS)
        (self.root / "private").mkdir()
        (self.root / routing.RUNTIME).mkdir(parents=True)
        (self.root / routing.POLICY).write_text("{}", encoding="utf-8")
        self.routes = {"schema_version": 1, "policy_version": "1", "generation": 1,
                       "projects": {"Demo": {"classification": "unmigrated_git", "backend": "git_local",
                                             "route_phase": "steady", "locator": None}}}
        git_store.write_json(self.root / routing.REGISTRY, self.routes)
        self.source = {"notes_root": str(self.notes), "authority": "git_local",
                       "scope": "all_projects", "generation": 1}
        # Odd columns leave intervening human columns untouched.
        self.target = {"spreadsheet_id": "existing-sheet", "sheet_id": 7,
                       "header_row": 0, "columns": {name: i * 2 for i, name in enumerate(refresh.COLUMNS)},
                       "rows": {"Demo": 2}, "generation": 1}
        self.box = refresh.Outbox(self.root / "private", self.source, self.target, {})
        self.transport = Transport(self.target)
        self.scan = patch.object(refresh.privacy, "check", return_value={"passed": True})
        self.scan.start()
        self.addCleanup(self.scan.stop)
        self.addCleanup(self.temp.cleanup)

    def prepare(self):
        return self.box.prepare(self.transport.read(self.target, "prepare"), "prepare")

    def begin(self, plan):
        return self.box.begin(plan["plan_hash"], self.transport.read(self.target, "begin"), "begin")

    def test_real_bounded_transport_then_no_change(self):
        first = refresh.run_once(self.box, self.transport)
        self.assertEqual(first["freshness"], "current")
        self.assertEqual(refresh.run_once(self.box, self.transport)["outcome"], "no_change")
        self.assertEqual(self.transport.writes, 1)
        self.assertIsNone(self.box.inspect()["pending"])

    def test_pending_error_survives_prepare_and_restart(self):
        plan = self.prepare()
        self.box.record_error("AUTH_UNAVAILABLE")
        restart = refresh.Outbox(self.root / "private", self.source, self.target, {})
        self.assertEqual(restart.prepare(self.transport.read(self.target, "again"), "again")["outcome"], "pending")
        self.assertEqual(restart.inspect()["pending"]["plan_hash"], plan["plan_hash"])
        self.assertEqual(restart.inspect()["error"]["code"], "AUTH_UNAVAILABLE")

    def test_after_write_timeout_reconciles_without_duplicate(self):
        self.transport.failure = "after"
        self.assertEqual(refresh.run_once(self.box, self.transport)["code"], "TRANSPORT_UNKNOWN")
        self.assertEqual(refresh.run_once(self.box, self.transport)["outcome"], "confirmed")
        self.assertEqual(self.transport.writes, 1)

    def test_429_explicit_rejection_can_retry_same_plan(self):
        self.transport.failure = "429"
        self.assertEqual(refresh.run_once(self.box, self.transport)["code"], "RATE_LIMITED")
        digest = self.box.inspect()["pending"]["plan_hash"]
        self.transport.failure = None
        self.assertEqual(refresh.run_once(self.box, self.transport)["plan_hash"], digest)
        self.assertEqual(self.transport.writes, 2)

    def test_unknown_mismatch_never_resends(self):
        plan = self.prepare()
        action = self.begin(plan)
        observed = self.transport.read(self.target, action["read_nonce"])
        self.assertEqual(self.box.reconcile(plan["plan_hash"], observed)["outcome"], "manual_review")
        self.assertEqual(refresh.run_once(self.box, self.transport)["outcome"], "manual_review")
        self.assertEqual(self.transport.writes, 0)

    def test_fresh_plan_binding_required(self):
        plan = self.prepare()
        action = self.begin(plan)
        self.transport.write(action["payload"])
        observed = self.transport.read(self.target, "reused-before-send")
        with self.assertRaises(core.RecordError):
            self.box.reconcile(plan["plan_hash"], observed)
        pending = self.box.inspect()["pending"]
        pending["plan"]["payload"]["requests"] = []
        state = self.box.inspect()
        state["pending"] = pending
        self.box.save(state)
        with self.assertRaises(core.RecordError):
            self.box.reconcile(plan["plan_hash"], self.transport.read(self.target, action["read_nonce"]))

    def test_reordered_identity_and_source_change_refuse_send(self):
        plan = self.prepare()
        observed = self.transport.read(self.target, "begin")
        observed["rows"]["Demo"]["project_id"] = "Other"
        with self.assertRaises(core.RecordError):
            self.box.begin(plan["plan_hash"], observed, "begin")
        (self.projects / "Demo/status.md").write_bytes(STATUS.replace(b"First", b"Second"))
        with self.assertRaises(core.RecordError):
            self.begin(plan)
        self.assertEqual(self.box.inspect()["pending"]["phase"], "prepared")

    def test_missing_explicit_git_configuration_fails(self):
        for field in ("authority", "scope", "generation"):
            config = dict(self.source)
            del config[field]
            with self.assertRaises(core.RecordError):
                refresh.collect_git(config)

    def test_inventory_addition_requires_mapping_removal_retains_unknown_row(self):
        (self.projects / "New").mkdir()
        self.routes["projects"]["New"] = dict(self.routes["projects"]["Demo"])
        git_store.write_json(self.root / routing.REGISTRY, self.routes)
        with self.assertRaises(core.RecordError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, "TARGET_MAPPING_REQUIRED")
        (self.projects / "New").rmdir()
        (self.projects / "Demo/status.md").unlink()
        (self.projects / "Demo").rmdir()
        refresh.run_once(self.box, self.transport)
        self.assertEqual(self.transport.rows["Demo"]["issues"], "PROJECT_REMOVED")
        self.assertEqual(self.transport.rows["Demo"]["availability"], "unknown")

    def test_invalid_status_and_legacy_are_unknown(self):
        (self.projects / "Demo/status.md").write_text("bad", encoding="utf-8")
        row = refresh.collect_git(self.source)["rows"][0]
        self.assertEqual(row["issues"], ["STATUS_FORMAT_INVALID"])
        (self.projects / "Demo/status.md").unlink()
        (self.projects / "Demo/progress.md").write_text("old", encoding="utf-8")
        row = refresh.collect_git(self.source)["rows"][0]
        self.assertEqual(row["issues"], ["LEGACY_PROGRESS_UNVERIFIED"])

    def test_race_rejected(self):
        real = refresh.portfolio.git_row
        count = [0]
        def changing(*args):
            row = real(*args)
            count[0] += 1
            if count[0] == 1:
                (self.projects / "Demo/status.md").write_bytes(STATUS.replace(b"First", b"Second"))
            return row
        with patch.object(refresh.portfolio, "git_row", side_effect=changing):
            with self.assertRaises(core.RecordError) as error:
                refresh.collect_git(self.source)
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")

    def test_source_read_failure_is_unknown_and_not_old_values(self):
        original = pathlib.Path.read_bytes
        def read(path):
            if path.name == "status.md":
                raise PermissionError()
            return original(path)
        with patch.object(pathlib.Path, "read_bytes", read):
            row = refresh.collect_git(self.source)["rows"][0]
        self.assertIsNone(row["values"])
        self.assertEqual(row["issues"], ["SOURCE_UNREADABLE"])

    def test_symlink_inventory_refused(self):
        (self.projects / "Demo/status.md").unlink()
        (self.projects / "Demo").rmdir()
        with patch.object(refresh.os, "scandir") as scan:
            entry = unittest.mock.Mock()
            entry.name = "Demo"
            entry.stat.return_value = unittest.mock.Mock(st_mode=0o120777, st_file_attributes=0)
            scan.return_value.__enter__.return_value = [entry]
            with self.assertRaises(core.RecordError):
                refresh.collect_git(self.source)

    def test_scanner_failure_no_outbox(self):
        with patch.object(refresh.privacy, "check", side_effect=core.RecordError("SCAN_INCOMPLETE")):
            self.assertEqual(refresh.run_once(self.box, self.transport)["code"], "SCAN_INCOMPLETE")
        self.assertIsNone(self.box.inspect()["pending"])
        self.assertEqual(self.transport.writes, 0)

    def test_all_values_strings_human_columns_untouched(self):
        (self.projects / "Demo/status.md").write_bytes(STATUS.replace(b"First", b"=HYPERLINK(unsafe)"))
        self.prepare()
        plan = self.box.inspect()["pending"]["plan"]
        for request in plan["payload"]["requests"]:
            update = request["updateCells"]
            self.assertIn(update["range"]["startColumnIndex"], self.target["columns"].values())
            self.assertEqual(update["fields"], "userEnteredValue")
            self.assertEqual(set(update["rows"][0]["values"][0]["userEnteredValue"]), {"stringValue"})
        self.assertIn("=HYPERLINK(unsafe)", str(plan["payload"]))

    def test_source_after_send_is_stale_not_latest(self):
        plan = self.prepare()
        action = self.begin(plan)
        self.transport.write(action["payload"])
        (self.projects / "Demo/status.md").write_bytes(STATUS.replace(b"First", b"Second"))
        result = self.box.reconcile(plan["plan_hash"], self.transport.read(self.target, action["read_nonce"]))
        self.assertEqual(result["freshness"], "stale")
        self.assertEqual(refresh.run_once(self.box, self.transport)["freshness"], "current")

    def test_probe_no_change_needs_no_transport_and_preserves_pending_error(self):
        self.assertEqual(self.box.probe()["outcome"], "refresh_required")
        refresh.run_once(self.box, self.transport)
        self.assertEqual(self.box.probe()["outcome"], "no_change")
        self.box.record_error("READ_FAILED")
        self.assertEqual(self.box.probe()["outcome"], "error")
        (self.projects / "Demo/status.md").write_bytes(STATUS.replace(b"First", b"Second"))
        self.prepare()
        self.assertEqual(self.box.probe()["outcome"], "pending")

    def test_already_correct_projection_adopts_success_without_write(self):
        source = refresh.collect_git(self.source)
        self.transport.rows = refresh.desired_rows(source, self.target)
        self.assertEqual(refresh.run_once(self.box, self.transport)["outcome"], "no_change")
        self.assertEqual(self.box.probe()["outcome"], "no_change")
        self.assertEqual(self.transport.writes, 0)

    def test_cancel_only_definitely_unsent_intent(self):
        plan = self.prepare()
        self.assertEqual(self.box.discard_prepared(plan["plan_hash"])["outcome"], "discarded")
        plan = self.prepare()
        self.begin(plan)
        with self.assertRaises(core.RecordError):
            self.box.discard_prepared(plan["plan_hash"])

    def test_no_change_retains_existing_error(self):
        refresh.run_once(self.box, self.transport)
        self.box.record_error("DIAGNOSTIC")
        self.assertEqual(refresh.run_once(self.box, self.transport)["outcome"], "no_change")
        self.assertEqual(self.box.inspect()["error"]["code"], "DIAGNOSTIC")


    def test_installed_fixed_package_cli_probe_and_real_checkpoint_queue(self):
        scripts = self.root / ".openchamber/workspace-records/package/skills/workspace-records/scripts"
        scripts.mkdir(parents=True)
        for source in SCRIPTS.glob("*.py"):
            shutil.copyfile(source, scripts / source.name)
        directory = self.root / routing.RUNTIME / "portfolio-refresh"
        directory.mkdir()
        git_store.write_json(self.root / refresh.PORTFOLIO_CONFIG,
                             {"source_config": self.source, "target": self.target})
        command = [sys.executable, "-B", str(scripts / "portfolio_refresh.py")]
        result = subprocess.run(command + ["probe", "--managed"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["data"]["outcome"], "refresh_required")
        for args in (["init"], ["add", "projects/Demo/status.md"],
                     ["-c", "user.name=Synthetic", "-c", "user.email=example@example.com", "commit", "-m", "fixture"]):
            git = subprocess.run(["git", "-C", str(self.notes), *args], capture_output=True, text=True)
            self.assertEqual(git.returncode, 0, git.stderr)
        commit = subprocess.run(["git", "-C", str(self.notes), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        event = {"kind": "git_checkpoint", "commit": commit}
        result = subprocess.run(command + ["queue", "--managed"], input=json.dumps(event), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["data"]["dispatch_required"])
        repeated = subprocess.run(command + ["queue", "--managed"], input=json.dumps(event), capture_output=True, text=True)
        self.assertEqual(json.loads(repeated.stdout)["data"]["outcome"], "duplicate_event")
        self.assertFalse(json.loads(repeated.stdout)["data"]["dispatch_required"])

    def test_official_registry_missing_never_falls_back_to_git(self):
        (self.root / routing.REGISTRY).unlink()
        with self.assertRaises(core.RecordError):
            self.box.probe()
        self.assertEqual(self.transport.writes, 0)

    def test_google_route_and_frozen_route_never_use_local_status(self):
        entry = self.routes["projects"]["Demo"]
        entry.update(classification="managed", backend="google_head", locator={"document_id": "doc"})
        git_store.write_json(self.root / routing.REGISTRY, self.routes)
        with self.assertRaisesRegex(core.RecordError, "GOOGLE_REFRESH_REQUIRED"):
            self.box.probe()
        entry.update(backend="git_local", locator=None, route_phase="frozen")
        git_store.write_json(self.root / routing.REGISTRY, self.routes)
        with self.assertRaisesRegex(core.RecordError, "PHASE_FROZEN"):
            self.box.probe()

    def test_unmigrated_requires_no_runtime_managed_git_requires_control(self):
        self.assertEqual(self.box.probe()["outcome"], "refresh_required")
        self.routes["projects"]["Demo"]["classification"] = "managed"
        git_store.write_json(self.root / routing.REGISTRY, self.routes)
        with self.assertRaises(core.RecordError):
            self.box.probe()
        binding = routing.Registry(self.root).bind("Demo")
        runtime = pathlib.Path(binding["runtime_directory"])
        runtime.mkdir()
        git_store.write_json(runtime / "control.json", {"mode": "git", "phase": "steady", "epoch": 2,
                                                       "version": 1, "policy_version": "1"})
        self.assertEqual(self.box.probe()["outcome"], "refresh_required")

    def test_pending_activation_barrier_blocks_projection_before_transport(self):
        parent = self.root / ".openchamber/workspace-records/activation-barriers"
        parent.mkdir()
        (parent / (core.sha(b"Demo") + ".json")).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(core.RecordError, "PHASE_FROZEN"):
            self.box.probe()
        self.assertEqual(self.transport.writes, 0)

    def test_route_change_after_prepare_blocks_send(self):
        plan = self.prepare()
        self.routes["projects"]["Demo"]["route_phase"] = "frozen"
        self.routes["generation"] += 1
        git_store.write_json(self.root / routing.REGISTRY, self.routes)
        with self.assertRaises(core.RecordError):
            self.begin(plan)
        self.assertEqual(self.box.inspect()["pending"]["phase"], "prepared")

    def checkpoint(self, commit="a" * 40):
        return {"kind": "git_checkpoint", "commit": commit}

    def queue(self, event=None):
        if event is None:
            event = self.checkpoint()
        with patch.object(refresh.subprocess, "run", return_value=unittest.mock.Mock(returncode=0, stdout=event.get("commit", "") + "\n")):
            return refresh.queue_event(self.box, event)

    def test_event_queue_dispatch_once_and_settle(self):
        self.assertTrue(self.queue()["dispatch_required"])
        self.assertFalse(self.queue()["dispatch_required"])
        result = refresh.consume_queued(self.box, lambda box: refresh.run_once(box, self.transport))
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(refresh.queued_events(self.box), [])
        self.assertFalse(self.queue(self.checkpoint("b" * 40))["dispatch_required"])

    def test_checkpoint_must_match_real_current_head(self):
        with patch.object(refresh.subprocess, "run", return_value=unittest.mock.Mock(returncode=0, stdout="b" * 40)):
            with self.assertRaisesRegex(core.RecordError, "REVISION_CONFLICT"):
                refresh.queue_event(self.box, self.checkpoint())
        self.assertEqual(refresh.queued_events(self.box), [])

    def test_no_change_events_do_not_call_consumer(self):
        refresh.run_once(self.box, self.transport)
        result = self.queue()
        self.assertEqual(result["outcome"], "no_change")
        consumer = unittest.mock.Mock(side_effect=AssertionError("must not launch model"))
        self.assertFalse(refresh.consume_queued(self.box, consumer)["model_started"])
        consumer.assert_not_called()

    def test_valid_google_receipt_is_blocked_hint_not_git_fallback(self):
        self.routes["projects"]["Demo"].update(classification="managed", backend="google_head", locator={"document_id": "doc"})
        git_store.write_json(self.root / routing.REGISTRY, self.routes)
        event = {"kind": "canonical_receipt", "project_id": "Demo", "operation_id": "op", "receipt_sha256": "a" * 64}
        result = self.queue(event)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["error"], "GOOGLE_REFRESH_REQUIRED")
        self.assertFalse(result["dispatch_required"])
        consumer = unittest.mock.Mock(side_effect=AssertionError("no archived Git projection"))
        self.assertFalse(refresh.consume_queued(self.box, consumer)["model_started"])

    def metadata_box(self):
        self.target["status_cell"] = {"sheet_id": self.target["sheet_id"], "row_index": 0, "column_index": 0}
        self.target["header_row"] = 1
        self.box = refresh.Outbox(self.root / "private", self.source, self.target, {})
        self.transport = Transport(self.target)

    def test_two_stage_visible_metadata_and_no_change_preserves_confirmation(self):
        self.metadata_box()
        result = refresh.run_once(self.box, self.transport)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(self.transport.writes, 2)
        self.assertIn("最後確認更新：", self.transport.status)
        self.assertIn("1個專案", self.transport.status)
        self.assertIn("write_started_at", result)
        self.assertIn("status_confirmed_at", result)
        confirmed = self.transport.status
        self.assertEqual(self.box.probe()["outcome"], "no_change")
        self.assertEqual(refresh.run_once(self.box, self.transport)["outcome"], "no_change")
        self.assertEqual(self.transport.status, confirmed)
        self.assertEqual(self.transport.writes, 2)

    def test_metadata_cell_cannot_overlap_header_or_data_rows(self):
        for row in (0, 2):
            target = {**self.target, "status_cell": {"sheet_id": 7, "row_index": row, "column_index": 99}}
            with self.assertRaises(core.RecordError):
                refresh.validate_target(target)

    def test_data_readback_required_before_confirmation_and_fresh_baseline_before_ack(self):
        self.metadata_box()
        plan = self.prepare()
        action = self.begin(plan)
        self.assertIn("資料更新中", action["payload"]["requests"][-1]["updateCells"]["rows"][0]["values"][0]["userEnteredValue"]["stringValue"])
        self.transport.write(action["payload"])
        first = self.box.reconcile(plan["plan_hash"], self.transport.read(self.target, action["read_nonce"]))
        self.assertEqual(first["outcome"], "confirmation_required")
        self.assertIsNone(self.box.inspect()["last_success"])
        self.transport.status = "third-party edited status"
        with self.assertRaisesRegex(core.RecordError, "TARGET_CHANGED"):
            self.box.begin_confirmation(plan["plan_hash"], self.transport.read(self.target, "ack"), "ack")
        self.assertEqual(self.transport.writes, 1)
        self.assertEqual(self.transport.status, "third-party edited status")

    def test_unknown_confirmation_reconciles_without_resending_data_or_metadata(self):
        self.metadata_box()
        real = self.transport.write
        def write(payload):
            real(payload)
            if self.transport.writes == 2:
                raise TimeoutError()
        self.transport.write = write
        self.assertEqual(refresh.run_once(self.box, self.transport)["code"], "TRANSPORT_UNKNOWN")
        self.assertEqual(self.box.inspect()["pending"]["phase"], "confirming")
        self.assertEqual(refresh.run_once(self.box, self.transport)["outcome"], "confirmed")
        self.assertEqual(self.transport.writes, 2)

    def test_failed_second_write_keeps_last_confirmation_and_local_error(self):
        self.metadata_box()
        refresh.run_once(self.box, self.transport)
        previous = self.box.inspect()["last_success"]["confirmed_at"]
        (self.projects / "Demo/status.md").write_bytes(STATUS.replace(b"First", b"Second"))
        real = self.transport.write
        def write(payload):
            if len(payload["requests"]) == 1:
                raise TimeoutError()
            real(payload)
        self.transport.write = write
        self.assertEqual(refresh.run_once(self.box, self.transport)["code"], "TRANSPORT_UNKNOWN")
        self.assertIn(previous, self.transport.status)
        self.assertIn("資料更新中", self.transport.status)
        self.assertEqual(self.box.inspect()["last_success"]["confirmed_at"], previous)
        self.assertIsNotNone(self.box.inspect()["error"])


if __name__ == "__main__":
    unittest.main()
