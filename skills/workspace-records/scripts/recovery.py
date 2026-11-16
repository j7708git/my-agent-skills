"""Reconcile a partially completed Git operation using its exact original plan."""

import argparse
import base64
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timezone
import core
import git_store
from core import RecordError, object_hash, request_hash, sha


def recover(project, runtime, req, privacy, plan, plan_hash):
    project = git_store.safe_dir(project)
    runtime = git_store.safe_dir(runtime)
    core.validate_request(req, True)
    rh = request_hash(req)
    if (
        object_hash(plan) != plan_hash
        or plan.get("request_hash") != rh
        or plan.get("operation_id") != req["operation_id"]
        or plan.get("project_identity") != sha(str(project.resolve()).encode())
        or plan.get("runtime_identity") != sha(str(runtime.resolve()).encode())
    ):
        raise RecordError("INTEGRITY_FAILED")
    with git_store.locked(runtime):
        git_store.journal_entries(runtime)
        entry = git_store.read_operation(runtime, req["operation_id"])
        if entry.get("request_hash") != rh:
            raise RecordError("ID_REUSE")
        if entry.get("scope") != req["scope"]:
            raise RecordError("FORBIDDEN")
        if entry.get("state") == "complete":
            return {"outcome": "no_write", "result": entry["result"]}
        changes = plan.get("changes")
        if not isinstance(changes, list) or len(changes) != len(entry.get("files", [])):
            raise RecordError("INTEGRITY_FAILED")
        decoded = []
        for change, file in zip(changes, entry["files"]):
            if any(
                change.get(key) != file.get(key)
                for key in ("path", "before_sha256", "after_sha256")
            ):
                raise RecordError("INTEGRITY_FAILED")
            try:
                raw = base64.b64decode(change["content"], validate=True)
                decoded.append(raw.decode("utf-8"))
            except (ValueError, TypeError, UnicodeError):
                raise RecordError("INTEGRITY_FAILED") from None
            if sha(raw) != file["after_sha256"]:
                raise RecordError("INTEGRITY_FAILED")
        payload = {
            "request": {
                key: req[key]
                for key in (
                    "schema_version",
                    "project_id",
                    "actor_role",
                    "scope",
                    "action",
                    "target_record_id",
                    "payload",
                    "expected",
                )
            },
            "changes": changes,
            "decoded_content": decoded,
        }
        scan = git_store.privacy_check(payload, privacy)
        for key in ("content_sha256", "policy_sha256", "rules_sha256"):
            if scan[key] != plan.get("scan", {}).get(key):
                raise RecordError("SCAN_INCOMPLETE")
        original = entry["expected_control"]
        control = git_store.get_control(runtime)
        if original.get("record_metadata") == "required-v1":
            update = entry.get("metadata_update")
            if not isinstance(update, dict) or set(update) != {
                "before_sha256",
                "after_sha256",
                "after",
            }:
                raise RecordError("INTEGRITY_FAILED")
            current = git_store.metadata_catalog(runtime, original, req["project_id"])
            current_hash = object_hash(current)
            if current_hash == update["before_sha256"]:
                if git_store.metadata_for_changes(current, changes) != update["after"]:
                    raise RecordError("INTEGRITY_FAILED")
            elif current_hash != update["after_sha256"]:
                raise RecordError("INTEGRITY_FAILED")
        updated = dict(original)
        updated["version"] += 1
        if req["action"] == "freeze":
            updated["phase"] = "frozen"
            updated["epoch"] += 1
        if control not in (original, updated) or plan.get("control") != original:
            raise RecordError("REVISION_CONFLICT")
        for change, file in zip(changes, entry["files"]):
            path = git_store.target_path(project, file["path"], allow_missing=True)
            observed = sha(path.read_bytes()) if path.exists() else None
            if observed == file["after_sha256"]:
                file["state"] = "written"
                continue
            if observed != file["before_sha256"] or control != original:
                raise RecordError("REVISION_CONFLICT")
            raw = base64.b64decode(change["content"], validate=True)
            file["state"] = "writing"
            entry["state"] = "partial"
            git_store.write_json(
                git_store.operation_path(runtime, req["operation_id"]), entry
            )
            fd, name = tempfile.mkstemp(prefix=".workspace-recovery-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(raw)
                    out.flush()
                    os.fsync(out.fileno())
                check = git_store.target_path(project, file["path"], allow_missing=True)
                if (sha(check.read_bytes()) if check.exists() else None) != observed:
                    raise RecordError("REVISION_CONFLICT")
                if observed is None:
                    os.link(name, check)
                    os.unlink(name)
                else:
                    os.replace(name, check)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            file["state"] = "written"
            git_store.write_json(
                git_store.operation_path(runtime, req["operation_id"]), entry
            )
        git_store.write_catalog_update(runtime, entry)
        if entry.get("state") != "files_done":
            if control != original:
                raise RecordError("INTEGRITY_FAILED")
            now = datetime.now(timezone.utc).isoformat()
            result = {
                "authority": {
                    "state": "complete",
                    "logical_version": updated["version"],
                    "docs_revision": None,
                    "readback_verified": True,
                },
                "projection": {
                    "state": "not_applicable",
                    "source_version": updated["version"],
                    "source_hash": None,
                },
                "operation_id": req["operation_id"],
                "request_hash": rh,
                "epoch": updated["epoch"],
                "phase": updated["phase"],
                "affected_ids": [c["record_id"] for c in changes],
                "observed_at": now,
            }
            entry["result"] = result
            entry["event"] = {
                "operation_id": req["operation_id"],
                "request_hash": rh,
                "version": updated["version"],
                "action": req["action"],
                "target_record_id": req["target_record_id"],
                "payload": req["payload"],
                "actor_role": req["actor_role"],
                "occurred_at": now,
            }
            entry["state"] = "files_done"
            git_store.write_json(
                git_store.operation_path(runtime, req["operation_id"]), entry
            )
        if control == original:
            git_store.write_json(runtime / "control.json", updated)
        entry["state"] = "complete"
        git_store.write_json(
            git_store.operation_path(runtime, req["operation_id"]), entry
        )
        return {"outcome": "complete", "result": entry["result"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding="utf-8-sig") as source:
                data = json.load(source)
        else:
            data = json.load(sys.stdin)
        result = recover(
            data["project_dir"],
            data["runtime_dir"],
            data["request"],
            data["privacy_config"],
            data["plan"],
            data["plan_hash"],
        )
        print(
            json.dumps(
                {"ok": True, "schema_version": 1, "data": result}, ensure_ascii=True
            )
        )
        return 0
    except (RecordError, KeyError, ValueError, TypeError, OSError) as error:
        code = error.code if isinstance(error, RecordError) else "INVALID_ARGUMENT"
        print(
            json.dumps(
                {
                    "ok": False,
                    "schema_version": 1,
                    "error": {"code": code, "retryable": False},
                },
                ensure_ascii=True,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
