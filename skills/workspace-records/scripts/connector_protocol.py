"""Offline durable intent protocol for connector-managed immutable objects and Docs CAS.

The caller owns connector authorization and must reconcile every unknown response.
No Google API, token, or background worker is used here.
"""

import argparse
import base64
import copy
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone

import core
import git_store
import google_docs
import manifest
import privacy
import routing
from core import RecordError, canonical, ident, object_hash, request_hash, sha


def private_root(value):
    return git_store.safe_dir(value)


def file_bytes(root, value):
    path = pathlib.Path(value)
    if not path.is_absolute():
        raise RecordError("FORBIDDEN")
    parent = git_store.safe_dir(path.parent)
    if parent != root and root not in parent.parents:
        raise RecordError("FORBIDDEN")
    try:
        st = os.lstat(path)
        if (
            stat.S_ISLNK(st.st_mode)
            or getattr(st, "st_file_attributes", 0) & git_store.REPARSE
            or not stat.S_ISREG(st.st_mode)
        ):
            raise RecordError("FORBIDDEN")
        if st.st_size > 64 * 1024 * 1024:
            raise RecordError("INTEGRITY_FAILED")
        return path.read_bytes()
    except RecordError:
        raise
    except OSError:
        raise RecordError("CONTROL_UNAVAILABLE") from None


def session_dir(root, project_id, operation_id, create=False):
    ident(project_id)
    ident(operation_id)
    parent = root / "p3-sessions"
    if not parent.exists():
        if not create:
            raise RecordError("NOT_FOUND")
        parent.mkdir(mode=0o700)
    git_store.safe_dir(parent)
    path = parent / object_hash(
        {"project_id": project_id, "operation_id": operation_id}
    )
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            raise RecordError("ID_REUSE") from None
    return git_store.safe_dir(path)


def load_session(root, x):
    project = ident(x.get("project_id"))
    op = ident(x.get("operation_id"))
    directory = session_dir(root, project, op)
    state = git_store.read_json(directory / "session.json")
    if state.get("project_id") != project or state.get("operation_id") != op:
        raise RecordError("INTEGRITY_FAILED")
    return directory, state


def stage(directory, artifact_id, raw, mime, scanner):
    ident(artifact_id)
    path = directory / (artifact_id + ".upload")
    if raw == b"":
        return {
            "artifact_id": artifact_id,
            "path": None,
            "sha256": sha(raw),
            "size": 0,
            "mime": mime,
            "status": "verified",
            "scanner": scanner,
            "remote_id": manifest.INLINE_EMPTY_ID,
            "encoding": "inline-empty-v1",
        }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        raise RecordError("INTEGRITY_FAILED") from None
    return {
        "artifact_id": artifact_id,
        "path": str(path),
        "sha256": sha(raw),
        "size": len(raw),
        "mime": mime,
        "status": "ready",
        "scanner": scanner,
    }


def scan_bytes(raw, mime, config):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    # Metadata and all UTF-8 content are checked by the P2 privacy adapter.
    receipt = privacy.check(
        {"mime": mime, "sha256": sha(raw), "size": len(raw), "content": text}, config
    )
    if text is None:
        binary = shutil.which(config.get("gitleaks", ""))
        if not binary:
            raise RecordError("SCAN_INCOMPLETE")
        try:
            env = os.environ.copy()
            env.pop("GITLEAKS_CONFIG", None)
            env.pop("GITLEAKS_CONFIG_TOML", None)
            result = subprocess.run(
                [
                    binary,
                    "stdin",
                    "--config",
                    config["gitleaks_config"],
                    "--ignore-gitleaks-allow",
                    "--gitleaks-ignore-path",
                    str(pathlib.Path(config["gitleaks_config"]).parent),
                    "--no-banner",
                    "--no-color",
                    "--redact",
                    "--exit-code",
                    "9",
                    "--timeout",
                    "15",
                ],
                input=raw,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                env=env,
            )
            if result.returncode == 9:
                raise RecordError("SCAN_FINDING")
            if result.returncode != 0:
                raise ValueError()
        except RecordError:
            raise
        except Exception:
            raise RecordError("SCAN_INCOMPLETE") from None
    return {
        "raw_sha256": sha(raw),
        "raw_size": len(raw),
        "policy_sha256": receipt["policy_sha256"],
        "rules_sha256": receipt["rules_sha256"],
        "coverage": "gitleaks_raw_binary+metadata"
        if text is None
        else "gitleaks+fallback+personal_path_utf8",
        "pii_enabled": receipt["pii_enabled"],
    }


def read_graph(root, head, existing):
    if (
        not isinstance(existing, dict)
        or set(existing) != {"manifest_file", "objects"}
        or not isinstance(existing["objects"], dict)
    ):
        raise RecordError("CONTROL_UNAVAILABLE")
    raw = file_bytes(root, existing["manifest_file"])
    prior = manifest.parse_json(manifest.verify_bytes(raw, head["root_manifest"]))
    manifest.validate_manifest(prior, head["project_id"])
    if prior["version"] != head["version"]:
        raise RecordError("INTEGRITY_FAILED")
    refs = (
        prior["history_pages"]
        + [r["object"] for r in prior["records"]]
        + [a["object"] for a in prior["attachments"]]
    )
    if set(existing["objects"]) != {
        r["id"] for r in refs if r.get("encoding") != "inline-empty-v1"
    }:
        raise RecordError("CONTROL_UNAVAILABLE")
    objects = {key: file_bytes(root, path) for key, path in existing["objects"].items()}
    objects[manifest.INLINE_EMPTY_ID] = b""
    for ref in refs:
        manifest.verify_bytes(objects[ref["id"]], ref)
    operations, events, journal = manifest.validate_history(
        prior, {p["id"]: objects[p["id"]] for p in prior["history_pages"]}
    )
    return prior, operations, objects, events, journal


def status_summary(records, objects):
    fields = {
        "state": "",
        "now": "",
        "next": "",
        "needs": "",
        "blocker": "",
        "updated": "",
        "decisions": "",
    }
    statuses = [entry for entry in records if entry["record"]["kind"] == "status"]
    if len(statuses) > 1:
        raise RecordError("INTEGRITY_FAILED")
    if not statuses:
        return fields
    entry = statuses[0]
    raw = objects[entry["object"]["id"]]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RecordError("INTEGRITY_FAILED") from None
    section = "header"
    pending = []
    for line in text.splitlines():
        if line.startswith("## "):
            section = "decisions" if line.strip() == "## 需要你決定" else "other"
            continue
        if section == "header" and ":" in line:
            key, value = line.split(":", 1)
            if key in fields:
                fields[key] = value.strip().strip('"')
        elif section == "decisions" and line.lstrip().startswith("- [ ]"):
            pending.append(line.split("]", 1)[1].strip())
    fields["decisions"] = "、".join(pending)
    if any(len(value) > 1024 for value in fields.values()):
        raise RecordError("INTEGRITY_FAILED")
    return fields


def bootstrap_graph(req, recordset):
    core.validate_request(req)
    ident(req.get("operation_id"))
    if (
        req["actor_role"] != "pm"
        or req.get("action") != "bootstrap"
        or req.get("target_record_id") is not None
        or "replace" not in req["scope"]["actions"]
    ):
        raise RecordError("FORBIDDEN")
    if (
        not isinstance(recordset, dict)
        or not {
            "schema_version",
            "project_id",
            "control",
            "version",
            "records",
            "events",
            "journal",
            "attachments",
        }
        <= set(recordset)
        or recordset["schema_version"] != 1
        or recordset["project_id"] != req["project_id"]
    ):
        raise RecordError("INTEGRITY_FAILED")
    source_control = recordset.get("control")
    if (
        recordset.get("completeness") != "complete"
        or recordset.get("incomplete_operations") != []
        or recordset.get("unselected_paths") != []
        or not isinstance(source_control, dict)
        or set(source_control)
        not in (
            {"mode", "phase", "epoch", "version", "policy_version"},
            {"mode", "phase", "epoch", "version", "policy_version", "record_metadata"},
        )
        or (
            "record_metadata" in source_control
            and source_control["record_metadata"] != "required-v1"
        )
        or source_control["mode"] != "git"
        or source_control["phase"] != "frozen"
        or type(source_control["epoch"]) is not int
        or source_control["epoch"] < 1
        or type(source_control["version"]) is not int
        or source_control["version"] < 0
        or not isinstance(source_control["policy_version"], str)
        or not source_control["policy_version"]
        or recordset["version"] != source_control["version"]
    ):
        raise RecordError("INTEGRITY_FAILED")
    if req.get("expected") != {
        "mode": "google",
        "phase": "frozen",
        "epoch": source_control["epoch"],
        "version": source_control["version"],
    }:
        raise RecordError("INVALID_ARGUMENT")
    if req.get("payload") != {"recordset_sha256": object_hash(recordset)}:
        raise RecordError("INTEGRITY_FAILED")
    if any(
        not isinstance(recordset[k], list)
        for k in ("records", "events", "journal", "attachments")
    ):
        raise RecordError("INTEGRITY_FAILED")
    specs = {r["record_id"]: r for r in req["scope"]["records"]}
    entries = []
    attachments = []
    contents = {}
    seen = set()
    for record in recordset["records"]:
        required = {
            "record_id",
            "kind",
            "created_at",
            "updated_at",
            "source",
            "body",
            "metadata",
            "relations",
        }
        if not isinstance(record, dict) or set(record) != required:
            raise RecordError("INTEGRITY_FAILED")
        rid = ident(record["record_id"])
        spec = specs.get(rid)
        source = record["source"]
        body = record["body"]
        if (
            rid in seen
            or not spec
            or not isinstance(source, dict)
            or spec["path"] != source.get("relative_path")
            or "body" not in spec["fields"]
        ):
            raise RecordError("FORBIDDEN")
        if (
            not isinstance(body, dict)
            or body.get("format") != "utf-8"
            or set(body) != {"format", "content"}
            or not isinstance(body["content"], str)
        ):
            raise RecordError("INTEGRITY_FAILED")
        raw = body["content"].encode("utf-8")
        if source.get("content_sha256") != sha(raw):
            raise RecordError("INTEGRITY_FAILED")
        stored = copy.deepcopy(record)
        stored["body"] = {"format": "utf-8"}
        entries.append({"record": stored, "artifact_id": "record-" + str(len(entries))})
        contents[rid] = raw
        seen.add(rid)
    for item in recordset["attachments"]:
        if not isinstance(item, dict) or not {
            "attachment_id",
            "logical_name",
            "mime",
            "size",
            "sha256",
            "locator_alias",
            "source_relative_path",
            "raw_bytes_b64",
        } <= set(item):
            raise RecordError("INTEGRITY_FAILED")
        rid = ident(item["attachment_id"])
        spec = specs.get(rid)
        if (
            rid in seen
            or not spec
            or spec["path"] != item["source_relative_path"]
            or "body" not in spec["fields"]
        ):
            raise RecordError("FORBIDDEN")
        try:
            raw = base64.b64decode(item["raw_bytes_b64"], validate=True)
        except (ValueError, TypeError):
            raise RecordError("INTEGRITY_FAILED") from None
        if len(raw) != item["size"] or sha(raw) != item["sha256"]:
            raise RecordError("INTEGRITY_FAILED")
        stored = {k: v for k, v in item.items() if k != "raw_bytes_b64"}
        attachments.append(
            {"attachment": stored, "artifact_id": "attachment-" + str(len(attachments))}
        )
        contents[rid] = raw
        seen.add(rid)
    if seen != set(specs):
        raise RecordError("FORBIDDEN")
    try:
        expected_events = sorted(
            [entry["event"] for entry in recordset["journal"]],
            key=lambda event: event["version"],
        )
        if any(
            entry.get("state") != "complete"
            or not isinstance(entry.get("scope"), dict)
            or entry["event"].get("operation_id") != entry.get("operation_id")
            or entry["event"].get("request_hash") != entry.get("request_hash")
            for entry in recordset["journal"]
        ):
            raise ValueError()
        if expected_events != recordset["events"]:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise RecordError("INTEGRITY_FAILED") from None
    roots = {
        k: v
        for k, v in recordset.items()
        if k not in ("records", "events", "journal", "attachments")
    }
    prior = {
        "schema_version": 1,
        "project_id": req["project_id"],
        "version": source_control["version"],
        "records": [],
        "attachments": [],
        "history_pages": [],
        "source_root": roots,
    }
    return (
        prior,
        entries,
        attachments,
        contents,
        recordset["events"],
        recordset["journal"],
    )


def compute_changes(req, prior, objects):
    core.validate_request(req, True)
    if req["action"] in ("freeze", "resume"):
        return []
    actions = (
        req["payload"]["changes"]
        if req["action"] == "batch"
        else [
            {
                "action": req["action"],
                "target_record_id": req["target_record_id"],
                "payload": req["payload"],
            }
        ]
    )
    records = {entry["record"]["record_id"]: entry for entry in prior["records"]}
    changes = []
    for item in actions:
        rid = item["target_record_id"]
        spec = next((r for r in req["scope"]["records"] if r["record_id"] == rid), None)
        current = records.get(rid)
        if spec is None:
            raise RecordError("FORBIDDEN")
        path = spec["path"]
        git_store.relative_path(path)
        action = item["action"]
        payload = item["payload"]
        if (path.casefold() == "status.md" and path != "status.md") or (
            path.casefold().startswith("log/") and not path.startswith("log/")
        ):
            raise RecordError("FORBIDDEN")
        if path == "progress.md" or (
            req["actor_role"] != "pm"
            and (path == "status.md" or path.startswith("log/"))
        ):
            raise RecordError("FORBIDDEN")
        if (path == "status.md") != (action == "status_patch"):
            raise RecordError("FORBIDDEN")
        if action == "append" and not path.startswith("log/"):
            raise RecordError("FORBIDDEN")
        if current is None:
            if action != "replace" or payload.get("base_sha256") is not None:
                raise RecordError("NOT_FOUND")
            if not path.startswith(("tasks/", "acceptance/", "research/")):
                raise RecordError("FORBIDDEN")
            kind = {
                "tasks": "task",
                "acceptance": "acceptance",
                "research": "research",
            }[path.split("/")[0]]
            record = {
                "record_id": rid,
                "kind": kind,
                "created_at": None,
                "updated_at": None,
                "source": {
                    "relative_path": path,
                    "content_sha256": None,
                    "original_time": None,
                },
                "body": {"format": "utf-8"},
                "metadata": {},
                "relations": [],
            }
            raw = b""
        else:
            record = copy.deepcopy(current["record"])
            if path != record["source"]["relative_path"]:
                raise RecordError("FORBIDDEN")
            raw = objects[current["object"]["id"]]
            if payload.get("base_sha256") != sha(raw):
                raise RecordError("REVISION_CONFLICT")
        if action == "status_patch":
            new = core.status_patch(raw, payload["fields"])
        else:
            try:
                previous = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise RecordError("INTEGRITY_FAILED") from None
            new = (
                (previous + payload["content"])
                if action == "append"
                else payload["content"]
            ).encode("utf-8")
        record["source"]["content_sha256"] = sha(new)
        changes.append((record, new))
    return changes


def prepare(x):
    root = private_root(x["private_dir"])
    req = x["request"]
    ident(req.get("project_id"))
    ident(req.get("operation_id"))
    bootstrap = req.get("action") == "bootstrap"
    snap = google_docs.snapshot(
        x["document"],
        x["document_id"],
        x["head_tab_id"],
        x["summary_tab_id"],
        req["project_id"],
        bootstrap=bootstrap,
    )
    rh = request_hash(req)
    if bootstrap:
        prior, record_changes, attachment_changes, contents, events, journal = (
            bootstrap_graph(req, x["recordset"])
        )
        records = [
            (entry["record"], contents[entry["record"]["record_id"]])
            for entry in record_changes
        ]
        attachment_bytes = [
            (entry["attachment"], contents[entry["attachment"]["attachment_id"]])
            for entry in attachment_changes
        ]
        old_objects = {}
        source_control = x["recordset"]["control"]
        control = {
            "mode": "google",
            "phase": "frozen",
            "epoch": source_control["epoch"],
            "policy_version": source_control["policy_version"],
        }
    else:
        core.validate_request(req, True)
        prior, operations, old_objects, events, journal = read_graph(
            root, snap["head"], x["existing"]
        )
        if status_summary(prior["records"], old_objects) != snap["head"]["summary"]:
            raise RecordError("INTEGRITY_FAILED")
        if req["operation_id"] in operations:
            old = operations[req["operation_id"]]
            if old["request_hash"] != rh:
                raise RecordError("ID_REUSE")
            return {"outcome": "no_write", "result": old["result"], "request_hash": rh}
        control = snap["head"]["control"]
        if req["action"] == "resume":
            if (
                req["actor_role"] != "pm"
                or control["phase"] != "frozen"
                or req["expected"]
                != {
                    "mode": "google",
                    "phase": "frozen",
                    "epoch": control["epoch"],
                    "version": snap["head"]["version"],
                }
            ):
                raise RecordError("PHASE_FROZEN")
        else:
            core.check_control(
                {**control, "version": snap["head"]["version"]}, req["expected"]
            )
        records = compute_changes(req, prior, old_objects)
        attachment_bytes = []
    directory = session_dir(root, req["project_id"], req["operation_id"], create=True)
    state = {
        "schema_version": 1,
        "project_id": req["project_id"],
        "operation_id": req["operation_id"],
        "request_hash": rh,
        "phase": "preparing",
        "snapshot": snap,
        "prior": prior,
        "artifacts": [],
        "record_changes": [],
        "attachment_changes": [],
        "history_artifacts": [],
        "route_binding": x.get("_route_binding"),
    }
    git_store.write_json(directory / "session.json", state)
    try:
        raw_by_id = {
            entry["record"]["record_id"]: old_objects[entry["object"]["id"]]
            for entry in prior["records"]
        }
        for ordinal, (record, raw) in enumerate(records):
            scan = scan_bytes(raw, "text/markdown", x["privacy_config"])
            a = stage(directory, "record-" + str(ordinal), raw, "text/markdown", scan)
            state["artifacts"].append(a)
            state["record_changes"].append(
                {"record": record, "artifact_id": a["artifact_id"]}
            )
            raw_by_id[record["record_id"]] = raw
        for ordinal, (attachment, raw) in enumerate(attachment_bytes):
            scan = scan_bytes(raw, attachment["mime"], x["privacy_config"])
            a = stage(
                directory, "attachment-" + str(ordinal), raw, attachment["mime"], scan
            )
            state["artifacts"].append(a)
            state["attachment_changes"].append(
                {"attachment": attachment, "artifact_id": a["artifact_id"]}
            )
        version = prior["version"] + 1
        next_control = dict(control)
        if req["action"] == "freeze":
            next_control["phase"] = "frozen"
            next_control["epoch"] += 1
        elif req["action"] == "resume":
            next_control["phase"] = "steady"
        result = {
            "logical_version": version,
            "epoch": next_control["epoch"],
            "phase": next_control["phase"],
            "affected_ids": [record["record_id"] for record, _ in records]
            + [a["attachment_id"] for a, _ in attachment_bytes],
        }
        if bootstrap:
            historical_ops = set()
            for old in journal:
                if (
                    not isinstance(old, dict)
                    or not {"operation_id", "request_hash", "result"} <= set(old)
                    or old["operation_id"] in historical_ops
                    or old.get("state", "complete") != "complete"
                ):
                    raise RecordError("INTEGRITY_FAILED")
                ident(old["operation_id"])
                historical_ops.add(old["operation_id"])
            if req["operation_id"] in historical_ops:
                raise RecordError("ID_REUSE")
        else:
            events = []
            journal = []
        event = {
            "operation_id": req["operation_id"],
            "request_hash": rh,
            "action": req["action"],
            "target_record_id": req["target_record_id"],
            "actor_role": req["actor_role"],
            "version": version,
        }
        events = list(events) + [event]
        journal = list(journal) + [
            {
                "project_id": req["project_id"],
                "operation_id": req["operation_id"],
                "request_hash": rh,
                "scope": req["scope"],
                "state": "complete",
                "event": event,
                "result": result,
            }
        ]
        pages = max((len(events) + 49) // 50, (len(journal) + 49) // 50)
        for i in range(pages):
            page = {
                "schema_version": 1,
                "events": events[i * 50 : (i + 1) * 50],
                "journal": journal[i * 50 : (i + 1) * 50],
            }
            if i == 0:
                page["previous_root"] = (
                    snap["head"]["root_manifest"] if snap["head"] else None
                )
            raw = canonical(page)
            scan = scan_bytes(raw, "application/json", x["privacy_config"])
            a = stage(directory, "history-" + str(i), raw, "application/json", scan)
            state["artifacts"].append(a)
            state["history_artifacts"].append(a["artifact_id"])
        records_for_summary = [
            entry
            for entry in prior["records"]
            if entry["record"]["record_id"] not in {r["record_id"] for r, _ in records}
        ]
        records_for_summary += [
            {"record": record, "object": {"id": "local-" + str(i)}}
            for i, (record, _) in enumerate(records)
        ]
        summary_objects = dict(old_objects)
        for i, (record, raw) in enumerate(records):
            summary_objects["local-" + str(i)] = raw
        summary = status_summary(records_for_summary, summary_objects)
        state.update(
            {
                "phase": "staged",
                "result": result,
                "control": next_control,
                "version": version,
                "summary": summary,
                "privacy_config": x["privacy_config"],
            }
        )
        git_store.write_json(directory / "session.json", state)
    except Exception:
        state["phase"] = "manual_review"
        git_store.write_json(directory / "session.json", state)
        raise
    return {
        "outcome": "staged",
        "operation_id": req["operation_id"],
        "request_hash": rh,
        "artifacts": [
            {
                "artifact_id": a["artifact_id"],
                "sha256": a["sha256"],
                "size": a["size"],
                "mime": a["mime"],
            }
            for a in state["artifacts"]
            if a["status"] == "ready"
        ],
        "inline_empty_count": sum(
            a.get("encoding") == "inline-empty-v1" for a in state["artifacts"]
        ),
    }


def begin_upload(x):
    root = private_root(x["private_dir"])
    directory, state = load_session(root, x)
    with git_store.locked(directory):
        state = git_store.read_json(directory / "session.json")
        if state["phase"] not in ("staged", "manifest_staged"):
            raise RecordError("MANUAL_REVIEW")
        item = next(
            (a for a in state["artifacts"] if a["artifact_id"] == x.get("artifact_id")),
            None,
        )
        if (
            item is None
            or item["status"] != "ready"
            or any(a["status"] == "sending" for a in state["artifacts"])
        ):
            raise RecordError("MANUAL_REVIEW")
        raw = file_bytes(root, item["path"])
        if sha(raw) != item["sha256"] or len(raw) != item["size"]:
            raise RecordError("INTEGRITY_FAILED")
        if scan_bytes(raw, item["mime"], state["privacy_config"]) != item["scanner"]:
            raise RecordError("SCAN_INCOMPLETE")
        item["status"] = "sending"
        git_store.write_json(directory / "session.json", state)
        return {
            "outcome": "upload_once",
            "artifact_id": item["artifact_id"],
            "file_uri": item["path"],
            "file_name": item["artifact_id"]
            + "-"
            + item["sha256"][:16]
            + (
                ".md"
                if item["mime"] == "text/markdown"
                else ".json"
                if item["mime"] == "application/json"
                else ".bin"
            ),
            "mime_type": item["mime"],
            "sha256": item["sha256"],
            "size": item["size"],
        }


def accept_upload(x):
    root = private_root(x["private_dir"])
    directory, state = load_session(root, x)
    with git_store.locked(directory):
        state = git_store.read_json(directory / "session.json")
        item = next(
            (a for a in state["artifacts"] if a["artifact_id"] == x.get("artifact_id")),
            None,
        )
        if item is None or item["status"] != "sending":
            raise RecordError("MANUAL_REVIEW")
        ident(x.get("remote_id"))
        if x["remote_id"] == manifest.INLINE_EMPTY_ID:
            raise RecordError("FORBIDDEN")
        if x.get("readback_remote_id") != x["remote_id"]:
            raise RecordError("INTEGRITY_FAILED")
        raw = file_bytes(root, x["downloaded_file"])
        if len(raw) != item["size"] or sha(raw) != item["sha256"]:
            raise RecordError("INTEGRITY_FAILED")
        if any(
            a.get("remote_id") == x["remote_id"]
            for a in state["artifacts"]
            if a is not item
        ):
            raise RecordError("INTEGRITY_FAILED")
        item.update({"status": "verified", "remote_id": x["remote_id"]})
        git_store.write_json(directory / "session.json", state)
        return {
            "outcome": "verified",
            "artifact_id": item["artifact_id"],
            "sha256": item["sha256"],
            "size": item["size"],
        }


def prepare_cas(x):
    root = private_root(x["private_dir"])
    directory, state = load_session(root, x)
    with git_store.locked(directory):
        state = git_store.read_json(directory / "session.json")
        if state["phase"] == "staged":
            if any(a["status"] != "verified" for a in state["artifacts"]):
                raise RecordError("IN_FLIGHT")
            refs = {
                a["artifact_id"]: {
                    "id": a["remote_id"],
                    "sha256": a["sha256"],
                    "size": a["size"],
                    **({"encoding": a["encoding"]} if "encoding" in a else {}),
                }
                for a in state["artifacts"]
            }
            prior = copy.deepcopy(state["prior"])
            replaced = {item["record"]["record_id"] for item in state["record_changes"]}
            prior["records"] = [
                r for r in prior["records"] if r["record"]["record_id"] not in replaced
            ]
            for item in state["record_changes"]:
                prior["records"].append(
                    {"record": item["record"], "object": refs[item["artifact_id"]]}
                )
            prior["records"].sort(key=lambda r: r["record"]["record_id"])
            replaced_attachments = {
                item["attachment"]["attachment_id"]
                for item in state["attachment_changes"]
            }
            prior["attachments"] = [
                a
                for a in prior["attachments"]
                if a["attachment"]["attachment_id"] not in replaced_attachments
            ]
            for item in state["attachment_changes"]:
                prior["attachments"].append(
                    {
                        "attachment": item["attachment"],
                        "object": refs[item["artifact_id"]],
                    }
                )
            prior["attachments"].sort(key=lambda a: a["attachment"]["attachment_id"])
            prior["history_pages"].extend(
                refs[aid] for aid in state["history_artifacts"]
            )
            prior["version"] = state["version"]
            manifest.validate_manifest(prior, state["project_id"])
            raw = canonical(prior)
            if len(raw) > manifest.MAX_MANIFEST_BYTES:
                raise RecordError("INTEGRITY_FAILED")
            scan = scan_bytes(raw, "application/json", state["privacy_config"])
            item = stage(directory, "manifest", raw, "application/json", scan)
            state["artifacts"].append(item)
            state["phase"] = "manifest_staged"
            git_store.write_json(directory / "session.json", state)
            return {
                "outcome": "manifest_ready",
                "artifact_id": "manifest",
                "sha256": item["sha256"],
                "size": item["size"],
            }
        if state["phase"] != "manifest_staged":
            raise RecordError("MANUAL_REVIEW")
        item = next(a for a in state["artifacts"] if a["artifact_id"] == "manifest")
        if item["status"] != "verified":
            raise RecordError("IN_FLIGHT")
        head = {
            "schema_version": 1,
            "project_id": state["project_id"],
            "control": state["control"],
            "version": state["version"],
            "root_manifest": {
                "id": item["remote_id"],
                "sha256": item["sha256"],
                "size": item["size"],
            },
            "last_operation": {
                "operation_id": state["operation_id"],
                "request_hash": state["request_hash"],
            },
            "summary": state["summary"],
        }
        google_docs.validate_head(head, state["project_id"])
        batch = google_docs.batch(state["snapshot"], head)
        scan = privacy.check(batch, state["privacy_config"])
        state.update(
            {
                "phase": "cas_prepared",
                "intended_head": head,
                "batch": batch,
                "batch_hash": object_hash(batch),
                "batch_scan": scan,
            }
        )
        git_store.write_json(directory / "session.json", state)
        return {
            "outcome": "cas_prepared",
            "batch_hash": state["batch_hash"],
            "scanner": scan,
        }


def begin_cas(x):
    root = private_root(x["private_dir"])
    directory, state = load_session(root, x)
    with git_store.locked(directory):
        state = git_store.read_json(directory / "session.json")
        if (
            state["phase"] != "cas_prepared"
            or x.get("batch_hash") != state["batch_hash"]
            or object_hash(state["batch"]) != state["batch_hash"]
        ):
            raise RecordError("MANUAL_REVIEW")
        scan = privacy.check(state["batch"], state["privacy_config"])
        if scan != state["batch_scan"]:
            raise RecordError("SCAN_INCOMPLETE")
        state["phase"] = "cas_unknown"
        git_store.write_json(directory / "session.json", state)
        return {
            "outcome": "cas_once",
            "batch": state["batch"],
            "batch_hash": state["batch_hash"],
        }


def reconcile_cas(x):
    root = private_root(x["private_dir"])
    directory, state = load_session(root, x)
    with git_store.locked(directory):
        state = git_store.read_json(directory / "session.json")
        if state["phase"] == "committed":
            return {
                "outcome": "committed",
                "result": state["result"],
                "head": state["intended_head"],
            }
        if state["phase"] != "cas_unknown":
            raise RecordError("MANUAL_REVIEW")
        try:
            snap = google_docs.snapshot(
                x["document"],
                state["snapshot"]["document_id"],
                state["snapshot"]["head_tab_id"],
                state["snapshot"]["summary_tab_id"],
                state["project_id"],
            )
        except (RecordError, KeyError, TypeError):
            return {"outcome": "manual_review", "reason": "HEAD_UNREADABLE"}
        old = state["snapshot"]
        if (
            snap["head"] == state["intended_head"]
            and snap["revision"] != old["revision"]
        ):
            try:
                root_raw = file_bytes(root, x["manifest_readback_file"])
                manifest.verify_bytes(root_raw, state["intended_head"]["root_manifest"])
                manifest.validate_manifest(
                    manifest.parse_json(root_raw), state["project_id"]
                )
            except (RecordError, KeyError, TypeError):
                return {"outcome": "manual_review", "reason": "MANIFEST_UNVERIFIED"}

            def outside(current):
                return {
                    tab: [
                        line
                        for line in lines
                        if not line.startswith((google_docs.HEAD, google_docs.SUMMARY))
                    ]
                    for tab, lines in current["lines"].items()
                }

            if outside(snap) != outside(old):
                return {"outcome": "manual_review", "reason": "OUTSIDE_CHANGED"}
            state["phase"] = "committed"
            state["observed_revision"] = snap["revision"]
            git_store.write_json(directory / "session.json", state)
            return {
                "outcome": "committed",
                "result": state["result"],
                "head": state["intended_head"],
                "docs_revision": snap["revision"],
            }
        return {
            "outcome": "manual_review",
            "reason": "UNKNOWN_OR_CONFLICT",
            "orphan_artifacts": [
                a["artifact_id"]
                for a in state["artifacts"]
                if a["status"] == "verified"
            ],
        }


def inspect_session(x):
    root = private_root(x["private_dir"])
    directory, state = load_session(root, x)
    with git_store.locked(directory):
        state = git_store.read_json(directory / "session.json")
        return {
            "outcome": "session",
            "phase": state["phase"],
            "request_hash": state["request_hash"],
            "artifacts": [
                {key: item[key] for key in ("artifact_id", "sha256", "size", "status")}
                for item in state["artifacts"]
            ],
        }


def dispatch(command, x):
    if not isinstance(x, dict):
        raise RecordError("INVALID_ARGUMENT")
    return {
        "inspect": inspect_session,
        "prepare": prepare,
        "begin-upload": begin_upload,
        "accept-upload": accept_upload,
        "prepare-cas": prepare_cas,
        "begin-cas": begin_cas,
        "reconcile-cas": reconcile_cas,
    }[command](x)


def managed_dispatch(command, x, registry):
    """Guard an existing Google head; blank-head bootstrap remains low-level only."""
    if not isinstance(x, dict):
        raise RecordError("INVALID_ARGUMENT")
    project = (
        x.get("request", {}).get("project_id")
        if command == "prepare"
        else x.get("project_id")
    )
    binding = registry.bind(project)
    if binding["backend"] != "google_head" or binding["route_phase"] != "steady":
        raise RecordError("FORBIDDEN")
    guarded = dict(x)
    if (
        "private_dir" in x
        and pathlib.Path(x["private_dir"]).resolve()
        != pathlib.Path(binding["runtime_directory"]).resolve()
    ):
        raise RecordError("FORBIDDEN")
    guarded["private_dir"] = binding["runtime_directory"]
    if command == "prepare":
        if x.get("request", {}).get("action") == "bootstrap":
            raise RecordError("FORBIDDEN")
        if x.get("document_id") != binding["locator"]["document_id"]:
            raise RecordError("FORBIDDEN")
        guarded["privacy_config"] = {
            "gitleaks": "gitleaks",
            "policy": binding["privacy_policy"],
            "gitleaks_config": str(
                pathlib.Path(__file__).resolve().parents[3] / ".gitleaks.toml"
            ),
        }
        previous = None
        head_tab, summary_tab = x["head_tab_id"], x["summary_tab_id"]
    else:
        _, state = load_session(private_root(guarded["private_dir"]), guarded)
        if state.get("route_binding") != binding:
            raise RecordError("REVISION_CONFLICT")
        previous = state["snapshot"]
        head_tab, summary_tab = previous["head_tab_id"], previous["summary_tab_id"]
    snap = google_docs.snapshot(
        x["document"], binding["locator"]["document_id"], head_tab, summary_tab, project
    )
    authority = {
        "document_id": snap["document_id"],
        "revision_id": snap["revision"],
        "control": {**snap["head"]["control"], "version": snap["head"]["version"]},
    }
    if command == "prepare" and x["request"]["action"] == "resume":
        core.validate_request(x["request"], True)
        _, operations, _, _, _ = read_graph(
            private_root(guarded["private_dir"]), snap["head"], x["existing"]
        )
        old = operations.get(x["request"]["operation_id"])
        if old is None:
            raise RecordError("FORBIDDEN")
        if old["request_hash"] != request_hash(x["request"]):
            raise RecordError("ID_REUSE")
        return {
            "outcome": "no_write",
            "result": old["result"],
            "request_hash": old["request_hash"],
        }
    if command == "reconcile-cas":
        # Reconciliation reads may observe the operation's newly frozen head.
        if (
            snap["head"]["control"]["mode"] != "google"
            or snap["head"]["control"]["policy_version"] != binding["policy_version"]
        ):
            raise RecordError("STALE_MODE")
    else:
        frozen_replay = (
            command == "prepare" and snap["head"]["control"]["phase"] == "frozen"
        )
        if frozen_replay:
            control = authority["control"]
            if (
                control["mode"] != "google"
                or control["policy_version"] != binding["policy_version"]
            ):
                raise RecordError("STALE_MODE")
        else:
            registry.check(binding, authority)
        if previous is not None and (
            snap["revision"] != previous["revision"] or snap["head"] != previous["head"]
        ):
            raise RecordError("REVISION_CONFLICT")
    guarded["_route_binding"] = binding
    return dispatch(command, guarded)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "inspect",
            "prepare",
            "begin-upload",
            "accept-upload",
            "prepare-cas",
            "begin-cas",
            "reconcile-cas",
        ],
    )
    parser.add_argument("--input")
    parser.add_argument("--managed", action="store_true")
    args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding="utf-8-sig") as f:
                value = json.load(f)
        else:
            value = json.load(sys.stdin)
        result = (
            managed_dispatch(
                args.command, value, routing.Registry(routing.discover_workspace())
            )
            if args.managed
            else dispatch(args.command, value)
        )
        print(
            json.dumps(
                {"ok": True, "schema_version": 1, "data": result},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 0
    except RecordError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "schema_version": 1,
                    "error": {
                        "code": exc.code,
                        "message": exc.code,
                        "retryable": False,
                        "details": {},
                    },
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 6 if exc.code in ("MANUAL_REVIEW", "OUTCOME_UNKNOWN", "PARTIAL") else 2
    except (OSError, ValueError, TypeError, KeyError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "schema_version": 1,
                    "error": {
                        "code": "INVALID_ARGUMENT",
                        "message": "INVALID_ARGUMENT",
                        "retryable": False,
                        "details": {},
                    },
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
