"""Deterministic Git portfolio outbox; caller supplies the authorized transport.

Fixed configuration is trusted deployment input, not a request or authentication.
No credentials, background process, append operation or authority mutation lives here.
"""

import argparse
import copy
import json
import pathlib
import subprocess
import sys
import os
import stat
import uuid
from datetime import datetime, timezone

import core
import git_store
import portfolio
import privacy
import routing
from core import RecordError, object_hash

COLUMNS = (
    "project_id", "state", "needs", "now", "next", "blocker", "updated",
    "authority", "availability", "issues", "source_hash", "source_time",
    "freshness", "source_digest",
)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def collect_git(config):
    """Fresh two-pass inventory under an explicit, fixed all-Git declaration."""
    if (
        not isinstance(config, dict)
        or set(config) != {"notes_root", "authority", "scope", "generation"}
        or config["authority"] != "git_local"
        or config["scope"] != "all_projects"
        or type(config["generation"]) is not int
        or config["generation"] < 1
    ):
        raise RecordError("FORBIDDEN")
    notes = git_store.safe_dir(config["notes_root"])
    if notes.name != "project-notes":
        raise RecordError("FORBIDDEN")
    registry = routing.Registry(notes.parent)

    def route_snapshot(names):
        data, digest = registry.load()
        if set(names) - set(data["projects"]):
            raise RecordError("NOT_REGISTERED")
        # all_projects is an explicit all-Git deployment. A missing local
        # directory never grants permission to overlook a Google registration.
        for entry in data["projects"].values():
            if entry["backend"] != "git_local":
                raise RecordError("GOOGLE_REFRESH_REQUIRED")
            if entry["route_phase"] != "steady":
                raise RecordError("PHASE_FROZEN")
        for name in names:
            binding = registry.bind(name)
            # routing.check permits positively registered unmigrated Git with
            # no runtime; managed Git must have valid steady control. It also
            # applies the same active disaster barrier as canonical writers.
            registry.check(binding)
        if registry.load()[1] != digest:
            raise RecordError("REVISION_CONFLICT")
        return digest

    def capture():
        projects = git_store.safe_dir(notes / "projects")
        names = []
        with os.scandir(projects) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & git_store.REPARSE:
                    raise RecordError("INTEGRITY_FAILED")
                if stat.S_ISREG(info.st_mode):
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    raise RecordError("INTEGRITY_FAILED")
                core.ident(entry.name)
                names.append(entry.name)
        if len({name.casefold() for name in names}) != len(names):
            raise RecordError("INTEGRITY_FAILED")
        registry_hash = route_snapshot(names)
        rows = []
        for name in sorted(names):
            project = git_store.project_dir(notes, name)
            row = portfolio.git_row(project, name)
            # Recheck directory and spelling after reads; a reader cannot fence
            # an arbitrary noncooperating writer, so capture again below.
            if git_store.project_dir(notes, name) != project:
                raise RecordError("REVISION_CONFLICT")
            rows.append(row)
        if route_snapshot(names) != registry_hash:
            raise RecordError("REVISION_CONFLICT")
        return {"rows": rows, "registry_sha256": registry_hash}

    first = capture()
    second = capture()
    if first != second:
        raise RecordError("REVISION_CONFLICT")
    value = {"schema_version": 1, "config_hash": object_hash(config), "rows": second["rows"],
             "registry_sha256": second["registry_sha256"]}
    value["source_digest"] = object_hash(value)
    return value


def validate_target(target):
    required = {"spreadsheet_id", "sheet_id", "header_row", "columns", "rows", "generation"}
    if not isinstance(target, dict) or not required <= set(target) or set(target) - required - {"status_cell"}:
        raise RecordError("INVALID_ARGUMENT")
    core.ident(target["spreadsheet_id"])
    for field in ("sheet_id", "header_row", "generation"):
        if type(target[field]) is not int or target[field] < 0:
            raise RecordError("INVALID_ARGUMENT")
    if set(target["columns"]) != set(COLUMNS):
        raise RecordError("INVALID_ARGUMENT")
    indices = list(target["columns"].values())
    if any(type(index) is not int or index < 0 for index in indices) or len(set(indices)) != len(indices):
        raise RecordError("INVALID_ARGUMENT")
    if not isinstance(target["rows"], dict) or len(target["rows"]) * len(COLUMNS) > 10000:
        raise RecordError("INVALID_ARGUMENT")
    for project, index in target["rows"].items():
        core.ident(project)
        if type(index) is not int or index <= target["header_row"]:
            raise RecordError("INVALID_ARGUMENT")
    if len(set(target["rows"].values())) != len(target["rows"]):
        raise RecordError("INVALID_ARGUMENT")
    if "status_cell" in target:
        cell = target["status_cell"]
        if (not isinstance(cell, dict) or set(cell) != {"sheet_id", "row_index", "column_index"}
                or any(type(value) is not int or value < 0 for value in cell.values())
                or cell["sheet_id"] != target["sheet_id"]
                or cell["row_index"] == target["header_row"]
                or cell["row_index"] in target["rows"].values()):
            raise RecordError("INVALID_ARGUMENT")


def desired_rows(source, target):
    ids = {row["project_id"] for row in source["rows"]}
    if ids - set(target["rows"]):
        raise RecordError("TARGET_MAPPING_REQUIRED")
    incoming = {row["project_id"]: row for row in source["rows"]}
    result = {}
    for project in target["rows"]:
        row = incoming.get(project, portfolio.unknown(project, "git_local", "PROJECT_REMOVED"))
        values = row["values"] or {}
        original = row["source"] or {}
        result[project] = {
            "project_id": project,
            **{field: str(values.get(field, "")) for field in portfolio.FIELDS},
            "authority": row["authority"], "availability": row["availability"],
            "issues": ",".join(row["issues"]), "source_hash": original.get("sha256", ""),
            "source_time": original.get("source_time") or "",
            "freshness": "current" if row["availability"] == "known" else "unknown",
            "source_digest": source["source_digest"],
        }
    return result


def check_observation(observed, target, nonce):
    """Validate bounded readback supplied by a cooperative authorized transport."""
    required = {"request_nonce", "target_hash", "read_version", "headers", "rows"}
    if "status_cell" in target:
        required.add("status")
    if not isinstance(observed, dict) or set(observed) != required:
        raise RecordError("READBACK_UNVERIFIED")
    if "status_cell" in target and not isinstance(observed["status"], str):
        raise RecordError("READBACK_UNVERIFIED")
    if (
        observed["request_nonce"] != nonce
        or observed["target_hash"] != object_hash(target)
        or not isinstance(observed["read_version"], str)
        or not observed["read_version"]
        or observed["headers"] != target["columns"]
        or set(observed["rows"]) != set(target["rows"])
    ):
        raise RecordError("READBACK_UNVERIFIED")
    for project, row in observed["rows"].items():
        if (
            not isinstance(row, dict) or set(row) != set(COLUMNS)
            or any(not isinstance(value, str) for value in row.values())
            or row["project_id"] != project
        ):
            raise RecordError("TARGET_CHANGED")


def status_request(target, text):
    cell = target["status_cell"]
    return {"updateCells": {"range": {"sheetId": cell["sheet_id"],
        "startRowIndex": cell["row_index"], "endRowIndex": cell["row_index"] + 1,
        "startColumnIndex": cell["column_index"], "endColumnIndex": cell["column_index"] + 1},
        "rows": [{"values": [{"userEnteredValue": {"stringValue": text}}]}],
        "fields": "userEnteredValue"}}


class Outbox:
    """Single-writer durable state; methods lock the same private directory.

    Transport read versions/nonces are evidence supplied by the caller, not
    cryptographic attestation. Sheets provides no cell compare-and-swap.
    """

    def __init__(self, directory, source_config, target, privacy_config, source_provider=None, row_projector=None, mixed=False):
        self.directory = git_store.safe_dir(directory)
        validate_target(target)
        self.source_config = copy.deepcopy(source_config)
        self.target = copy.deepcopy(target)
        self.privacy_config = copy.deepcopy(privacy_config)
        self.binding = object_hash({"source": source_config, "target": target})
        self.path = self.directory / "portfolio-outbox.json"
        self.source_provider = source_provider
        self.row_projector = row_projector
        if type(mixed) is not bool or (mixed and (source_provider is None or row_projector is None)):
            raise RecordError("INVALID_ARGUMENT")
        self.is_mixed = mixed

    def collect_source(self):
        return self.source_provider() if self.source_provider is not None else collect_git(self.source_config)

    def project_rows(self, source):
        return self.row_projector(source, self.target) if self.row_projector is not None else desired_rows(source, self.target)

    def load(self):
        if not self.path.exists():
            return {"schema_version": 1, "binding": self.binding, "pending": None, "last_success": None, "error": None}
        value = git_store.read_json(self.path)
        if value.get("schema_version") != 1 or value.get("binding") != self.binding:
            raise RecordError("REVISION_CONFLICT")
        return value

    def save(self, value):
        git_store.write_json(self.path, value)

    def inspect(self):
        with git_store.locked(self.directory):
            return self.load()

    def probe(self):
        """Cheap runner entry: no transport/AI needed when confirmed input matches."""
        with git_store.locked(self.directory):
            state = self.load()
            source = self.collect_source()
            self.project_rows(source)  # New projects require explicit mapping.
            if state["pending"]:
                outcome = "pending"
            elif state["error"]:
                outcome = "error"
            elif state["last_success"] and state["last_success"]["source_digest"] == source["source_digest"]:
                outcome = "no_change"
            else:
                outcome = "refresh_required"
            return {"outcome": outcome, "source_digest": source["source_digest"],
                    "last_success": state["last_success"], "error": state["error"]}

    def discard_prepared(self, plan_hash):
        """Explicit cancellation only before any possibly effective request."""
        with git_store.locked(self.directory):
            state = self.load()
            pending = state["pending"]
            if not pending or pending["phase"] != "prepared" or pending["plan_hash"] != plan_hash:
                raise RecordError("MANUAL_REVIEW")
            state["pending"] = None
            state["error"] = {"code": "PREPARED_DISCARDED", "at": timestamp(), "freshness": "unknown"}
            self.save(state)
            return {"outcome": "discarded", "plan_hash": plan_hash}

    def prepare(self, observed, nonce):
        with git_store.locked(self.directory):
            state = self.load()
            if state["pending"] is not None:
                return {"outcome": "pending", "state": state}
            source = self.collect_source()
            check_observation(observed, self.target, nonce)
            expected = self.project_rows(source)
            metadata_matches = ("status_cell" not in self.target or
                                state["last_success"] is not None and
                                observed["status"] == state["last_success"].get("status_value"))
            if observed["rows"] == expected and metadata_matches:
                # A fresh full read can adopt an already-correct projection.
                # Keep diagnostics; matching data alone does not settle them.
                if "status_cell" not in self.target:
                    state["last_success"] = {"plan_hash": None, "source_digest": source["source_digest"],
                                             "read_version": observed["read_version"],
                                             "confirmed_at": timestamp(), "freshness": "current"}
                if self.is_mixed:
                    if state["last_success"] is None:
                        raise RecordError("INTEGRITY_FAILED")
                    state["last_success"]["source_commit_proof"] = source["source_commit_proof"]
                    state["last_success"]["covered_events"] = source["covered_events"]
                    state["last_success"]["pending_events"] = source["pending_events"]
                    state["last_success"]["coverage_digest"] = source["coverage_digest"]
                self.save(state)
                return {"outcome": "no_change", "state": state, "source_digest": source["source_digest"],
                        **({"covered_events": source["covered_events"], "pending_events": source["pending_events"],
                            "coverage_digest": source["coverage_digest"]} if self.is_mixed else {})}
            requests, ranges = [], []
            for project, row in expected.items():
                index = self.target["rows"][project]
                for name, column in self.target["columns"].items():
                    if observed["rows"][project][name] == row[name]:
                        continue
                    bounds = {"sheetId": self.target["sheet_id"], "startRowIndex": index,
                              "endRowIndex": index + 1, "startColumnIndex": column,
                              "endColumnIndex": column + 1}
                    ranges.append(bounds)
                    requests.append({"updateCells": {"range": bounds,
                        "rows": [{"values": [{"userEnteredValue": {"stringValue": row[name]}}]}],
                        "fields": "userEnteredValue"}})
            payload = {"spreadsheet_id": self.target["spreadsheet_id"], "requests": requests}
            scanner = privacy.check(payload, self.privacy_config)
            plan = {"binding": self.binding, "source_digest": source["source_digest"],
                    "payload": payload, "write_ranges": ranges, "expected_rows": expected,
                    "base_rows_hash": object_hash(observed["rows"]),
                    "base_read_version": observed["read_version"], "scanner": scanner}
            if self.is_mixed:
                plan["source_commit_proof"] = source["source_commit_proof"]
                plan["covered_events"] = source["covered_events"]
                plan["pending_events"] = source["pending_events"]
                plan["coverage_digest"] = source["coverage_digest"]
            if "status_cell" in self.target:
                plan["base_status"] = observed["status"]
            digest = object_hash(plan)
            state["pending"] = {"phase": "prepared", "plan_hash": digest, "plan": plan,
                                "created_at": timestamp(), "attempt": 0}
            self.save(state)
            return {"outcome": "prepared", "plan_hash": digest, "write_ranges": ranges}

    def begin(self, plan_hash, observed, nonce):
        with git_store.locked(self.directory):
            state = self.load()
            pending = state["pending"]
            if not pending or pending["phase"] != "prepared" or pending["plan_hash"] != plan_hash:
                raise RecordError("MANUAL_REVIEW")
            plan = pending["plan"]
            if object_hash(plan) != plan_hash or plan["binding"] != self.binding:
                raise RecordError("INTEGRITY_FAILED")
            check_observation(observed, self.target, nonce)
            if (object_hash(observed["rows"]) != plan["base_rows_hash"] or
                    "status_cell" in self.target and observed["status"] != plan["base_status"]):
                raise RecordError("TARGET_CHANGED")
            current_source = self.collect_source()
            if (current_source["source_digest"] != plan["source_digest"]
                    or self.is_mixed and current_source["coverage_digest"] != plan["coverage_digest"]):
                raise RecordError("REVISION_CONFLICT")
            if privacy.check(plan["payload"], self.privacy_config) != plan["scanner"]:
                raise RecordError("SCAN_INCOMPLETE")
            sent_at = timestamp()
            payload = copy.deepcopy(plan["payload"])
            if "status_cell" in self.target:
                last = state["last_success"]
                previous = last["confirmed_at"] if last else "無"
                status = "資料更新中｜此次寫入：" + sent_at + "｜前次確認：" + previous
                payload["requests"].append(status_request(self.target, status))
                pending["updating_status"] = status
            pending.update({"phase": "sending", "attempt": pending["attempt"] + 1,
                            "read_nonce": "verify-" + plan_hash + "-" + uuid.uuid4().hex,
                            "sent_at": sent_at, "sent_payload": payload,
                            "sent_payload_hash": object_hash(payload),
                            "sent_scan": privacy.check(payload, self.privacy_config)})
            self.save(state)  # Persist before transport can observe the payload.
            return {"outcome": "send_once", "plan_hash": plan_hash,
                    "payload": copy.deepcopy(payload), "read_nonce": pending["read_nonce"]}

    def begin_confirmation(self, plan_hash, observed, nonce):
        with git_store.locked(self.directory):
            state = self.load()
            pending = state["pending"]
            if (not pending or pending["phase"] != "confirmation_prepared"
                    or pending["plan_hash"] != plan_hash or object_hash(pending["plan"]) != plan_hash):
                raise RecordError("MANUAL_REVIEW")
            check_observation(observed, self.target, nonce)
            if (observed["rows"] != pending["plan"]["expected_rows"]
                    or observed["status"] != pending["updating_status"]):
                raise RecordError("TARGET_CHANGED")
            current_source = self.collect_source()  # Recheck actual authority before metadata.
            if self.is_mixed and (current_source["source_digest"] != pending["plan"]["source_digest"]
                                  or current_source["coverage_digest"] != pending["plan"]["coverage_digest"]):
                raise RecordError("REVISION_CONFLICT")
            confirmed_at = timestamp()  # Data was freshly read and confirmed now.
            status = "最後確認更新：" + confirmed_at + "｜" + str(len(self.target["rows"])) + "個專案"
            payload = {"spreadsheet_id": self.target["spreadsheet_id"],
                       "requests": [status_request(self.target, status)]}
            pending.update({"phase": "confirming", "confirmed_status": status,
                            "data_confirmed_at": confirmed_at,
                            "confirmation_payload": payload,
                            "confirmation_payload_hash": object_hash(payload),
                            "confirmation_scan": privacy.check(payload, self.privacy_config),
                            "read_nonce": "confirm-" + plan_hash + "-" + uuid.uuid4().hex})
            self.save(state)
            return {"outcome": "send_confirmation_once", "payload": payload,
                    "plan_hash": plan_hash, "read_nonce": pending["read_nonce"]}

    def reconcile(self, plan_hash, observed):
        with git_store.locked(self.directory):
            state = self.load()
            pending = state["pending"]
            if not pending or pending["phase"] not in ("sending", "confirming") or pending["plan_hash"] != plan_hash:
                raise RecordError("MANUAL_REVIEW")
            plan = pending["plan"]
            if object_hash(plan) != plan_hash:
                raise RecordError("INTEGRITY_FAILED")
            check_observation(observed, self.target, pending["read_nonce"])
            status_matches = True
            if "status_cell" in self.target:
                field = "confirmed_status" if pending["phase"] == "confirming" else "updating_status"
                status_matches = observed["status"] == pending[field]
                payload_field = "confirmation_payload" if pending["phase"] == "confirming" else "sent_payload"
                if object_hash(pending[payload_field]) != pending[payload_field + "_hash"]:
                    raise RecordError("INTEGRITY_FAILED")
            if observed["rows"] != plan["expected_rows"] or not status_matches:
                state["error"] = {"code": "OUTCOME_UNKNOWN", "at": timestamp(), "freshness": "unknown"}
                self.save(state)
                return {"outcome": "manual_review", "freshness": "unknown"}
            if "status_cell" in self.target and pending["phase"] == "sending":
                pending["phase"] = "confirmation_prepared"
                pending["data_read_version"] = observed["read_version"]
                self.save(state)
                return {"outcome": "confirmation_required", "plan_hash": plan_hash}
            latest = self.collect_source()
            freshness = "current" if latest["source_digest"] == plan["source_digest"] else "stale"
            receipt = {"plan_hash": plan_hash, "source_digest": plan["source_digest"],
                       "read_version": observed["read_version"], "confirmed_at": timestamp(),
                       "freshness": freshness}
            if self.is_mixed:
                receipt.update({"source_commit_proof": plan["source_commit_proof"],
                                "covered_events": plan["covered_events"],
                                "pending_events": plan["pending_events"],
                                "coverage_digest": plan["coverage_digest"]})
            if "status_cell" in self.target:
                receipt.update({"write_started_at": pending["sent_at"],
                                "confirmed_at": pending["data_confirmed_at"],
                                "status_confirmed_at": timestamp(),
                                "status_value": pending["confirmed_status"]})
            state.update({"pending": None, "last_success": receipt, "error": None})
            self.save(state)
            return {"outcome": "confirmed", **receipt}

    def record_error(self, code, definitely_not_sent=False):
        with git_store.locked(self.directory):
            state = self.load()
            if definitely_not_sent and state["pending"]:
                state["pending"]["phase"] = ("confirmation_prepared" if state["pending"]["phase"] == "confirming" else "prepared")
            state["error"] = {"code": code, "at": timestamp(), "freshness": "unknown"}
            self.save(state)
            return {"outcome": "error", "code": code}


def run_once(outbox, transport):
    """Bounded data and optional status acknowledgement via cooperative transport."""
    def send(action):
        try:
            transport.write(action["payload"])
        except Exception as error:
            status = getattr(error, "status", None)
            code = "RATE_LIMITED" if status == 429 else "AUTH_UNAVAILABLE" if status in (401, 403) else "TRANSPORT_UNKNOWN"
            return outbox.record_error(code, getattr(error, "definitely_not_sent", False) is True)
        return outbox.reconcile(action["plan_hash"], transport.read(outbox.target, action["read_nonce"]))

    try:
        state = outbox.inspect()
        pending = state["pending"]
        if pending and pending["phase"] in ("sending", "confirming"):
            result = outbox.reconcile(pending["plan_hash"], transport.read(outbox.target, pending["read_nonce"]))
        elif pending and pending["phase"] == "confirmation_prepared":
            result = {"outcome": "confirmation_required", "plan_hash": pending["plan_hash"]}
        else:
            nonce = uuid.uuid4().hex
            result = outbox.prepare(transport.read(outbox.target, nonce), nonce)
            if result["outcome"] == "no_change":
                return result
            pending = outbox.inspect()["pending"]
            nonce = uuid.uuid4().hex
            result = send(outbox.begin(pending["plan_hash"], transport.read(outbox.target, nonce), nonce))
        if result["outcome"] == "confirmation_required":
            nonce = uuid.uuid4().hex
            action = outbox.begin_confirmation(result["plan_hash"], transport.read(outbox.target, nonce), nonce)
            return send(action)
        return result
    except RecordError as error:
        return outbox.record_error(error.code)
    except Exception:
        return outbox.record_error("TRANSPORT_UNAVAILABLE")


PORTFOLIO_CONFIG = pathlib.Path(".openchamber/workspace-records/portfolio.json")


def managed_outbox():
    """Load only the deployed package's fixed workspace configuration."""
    workspace = routing.discover_workspace()
    config = git_store.read_json(routing.private_file(workspace, PORTFOLIO_CONFIG))
    if set(config) != {"source_config", "target"}:
        raise RecordError("INVALID_ARGUMENT")
    if config["source_config"]["notes_root"] != str(workspace / "project-notes"):
        raise RecordError("FORBIDDEN")
    directory = git_store.safe_dir(workspace / routing.RUNTIME / "portfolio-refresh")
    policy = routing.private_file(workspace, routing.POLICY)
    privacy_config = {"gitleaks": "gitleaks", "policy": str(policy),
                      "gitleaks_config": str(pathlib.Path(__file__).resolve().parents[3] / ".gitleaks.toml")}
    return Outbox(directory, config["source_config"], config["target"], privacy_config)


def validate_event(outbox, event):
    """Events are wake hints, never canonical content or authority evidence."""
    if not isinstance(event, dict):
        raise RecordError("INVALID_ARGUMENT")
    notes = git_store.safe_dir(outbox.source_config["notes_root"])
    registry = routing.Registry(notes.parent)
    registry.load()  # Missing/unreadable is not an unmanaged-Git fallback.
    if event.get("kind") == "git_checkpoint":
        if set(event) != {"kind", "commit"} or not isinstance(event["commit"], str):
            raise RecordError("INVALID_ARGUMENT")
        result = subprocess.run(["git", "-C", str(notes), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=15)
        if result.returncode or result.stdout.strip() != event["commit"]:
            raise RecordError("REVISION_CONFLICT")
        if len(event["commit"]) not in (40, 64) or any(c not in "0123456789abcdef" for c in event["commit"]):
            raise RecordError("INVALID_ARGUMENT")
    elif event.get("kind") == "canonical_receipt":
        if set(event) != {"kind", "project_id", "operation_id", "receipt_sha256"}:
            raise RecordError("INVALID_ARGUMENT")
        core.ident(event["operation_id"])
        binding = registry.bind(core.ident(event["project_id"]))
        if binding["backend"] != "google_head" or binding["route_phase"] != "steady":
            raise RecordError("REVISION_CONFLICT")
        digest = event["receipt_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RecordError("INVALID_ARGUMENT")
    else:
        raise RecordError("INVALID_ARGUMENT")
    return object_hash(event)


def queue_event(outbox, event):
    """Post-checkpoint entry; deterministic no-change never launches a model."""
    event_hash = validate_event(outbox, event)
    path = outbox.directory / ("portfolio-event-" + event_hash + ".json")
    with git_store.locked(outbox.directory):
        if os.path.lexists(path):
            previous = git_store.read_json(path)
            if previous.get("event") != event or previous.get("binding") != outbox.binding:
                raise RecordError("INTEGRITY_FAILED")
            return {"outcome": "duplicate_event", "dispatch_required": False, "event_hash": event_hash,
                    "phase": previous["phase"]}
    try:
        probe = outbox.probe()
        error = None
    except RecordError as failure:
        # A valid receipt can notify the all-Git candidate of a mode change,
        # but does not authorize reading archived Git or using receipt fields.
        probe = {"outcome": "blocked", "source_digest": None}
        error = failure.code
    phase = ("queued" if outbox.is_mixed and event["kind"] == "canonical_receipt" and probe["outcome"] not in ("blocked", "error")
             else "no_change" if probe["outcome"] == "no_change"
             else "blocked" if probe["outcome"] in ("blocked", "error") else "queued")
    item = {"schema_version": 1, "binding": outbox.binding, "event": event,
            "event_hash": event_hash, "phase": phase, "source_digest": probe["source_digest"],
            "error": error or probe.get("error"), "created_at": timestamp()}
    with git_store.locked(outbox.directory):
        if os.path.lexists(path):
            return {"outcome": "duplicate_event", "dispatch_required": False, "event_hash": event_hash}
        git_store.write_json(path, item)
    return {"outcome": phase, "dispatch_required": phase == "queued", "event_hash": event_hash,
            "source_digest": probe["source_digest"], "error": item["error"]}


def queued_events(outbox):
    with git_store.locked(outbox.directory):
        result = []
        for path in sorted(outbox.directory.glob("portfolio-event-*.json")):
            value = git_store.read_json(path)
            if value.get("binding") != outbox.binding or value.get("event_hash") != object_hash(value.get("event")):
                raise RecordError("INTEGRITY_FAILED")
            if path.name != "portfolio-event-" + value["event_hash"] + ".json":
                raise RecordError("INTEGRITY_FAILED")
            if value["phase"] in ("queued", "blocked"):
                result.append(value)
        return result


def _consume_queued(outbox, consumer):
    """An existing fixed writer calls this on an event, never an AI timer."""
    events = queued_events(outbox)
    if not any(item["phase"] == "queued" for item in events):
        return {"outcome": "idle", "model_started": False}
    if outbox.is_mixed:
        outbox.source_provider.select_events([item["event"] for item in events if item["phase"] == "queued"])
    probe = outbox.probe()  # Fresh route guard before invoking any consumer.
    if probe["outcome"] == "error":
        return {"outcome": "blocked", "model_started": False, "error": probe["error"]}
    if probe["outcome"] == "no_change":
        result = {"outcome": "no_change", **({"covered_events": probe.get("covered_events", [])} if outbox.is_mixed else {})}
    else:
        result = consumer(outbox)
    if result.get("outcome") in ("confirmed", "no_change"):
        with git_store.locked(outbox.directory):
            state = outbox.load()
            if state["pending"] is not None or not state["last_success"]:
                raise RecordError("OUTCOME_UNKNOWN")
            if outbox.is_mixed and state["error"] is not None:
                raise RecordError("OUTCOME_UNKNOWN")
            for item in events:
                if item["phase"] != "queued":
                    continue
                if (outbox.is_mixed and item["event"]["kind"] == "canonical_receipt"
                        and item["event_hash"] not in result.get("covered_events", [])):
                    continue
                item["phase"] = "settled"
                item["settled_at"] = timestamp()
                item["confirmed_source_digest"] = state["last_success"]["source_digest"]
                git_store.write_json(outbox.directory / ("portfolio-event-" + item["event_hash"] + ".json"), item)
    return result


def consume_queued(outbox, consumer):
    try:
        return _consume_queued(outbox, consumer)
    finally:
        if outbox.is_mixed:
            outbox.source_provider.select_events([])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["probe", "queue", "inspect"])
    parser.add_argument("--managed", required=True, action="store_true")
    parser.add_argument("--input")
    args = parser.parse_args()
    try:
        outbox = managed_outbox()
        if args.command == "queue":
            if args.input:
                with open(args.input, encoding="utf-8-sig") as source:
                    event = json.load(source, object_pairs_hook=routing.unique_object)
            else:
                event = json.load(sys.stdin, object_pairs_hook=routing.unique_object)
            result = queue_event(outbox, event)
        elif args.command == "probe":
            result = outbox.probe()
        else:
            result = {"outbox": outbox.inspect(), "events": queued_events(outbox)}
        print(json.dumps({"ok": True, "schema_version": 1, "data": result}, ensure_ascii=True))
        return 0
    except (RecordError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        code = error.code if isinstance(error, RecordError) else "CONTROL_UNAVAILABLE"
        print(json.dumps({"ok": False, "schema_version": 1, "error": {"code": code, "retryable": False}}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
