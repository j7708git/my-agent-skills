"""Preview and resumably restore verified snapshots into a caller-approved isolated root."""

import argparse
import json
import os
import pathlib
import stat
import sys
import tempfile
import core
import git_store
import manifest
import snapshot
from core import RecordError, object_hash, sha


def destination(root, relative, create_parents=False):
    parts = git_store.relative_path(relative)
    current = root
    for part in parts[:-1]:
        child = git_store.canonical_child(current, part, allow_missing=True)
        if not os.path.lexists(child):
            if not create_parents:
                return None
            child.mkdir(mode=0o700)
        current = git_store.safe_dir(child)
    path = git_store.canonical_child(current, parts[-1], allow_missing=True)
    if os.path.lexists(path):
        st = os.lstat(path)
        if (
            stat.S_ISLNK(st.st_mode)
            or getattr(st, "st_file_attributes", 0) & git_store.REPARSE
            or not stat.S_ISREG(st.st_mode)
        ):
            raise RecordError("FORBIDDEN")
    return path


def target_dir(approved, target):
    base = git_store.safe_dir(approved)
    dest = git_store.safe_dir(target)
    if dest == base or base not in dest.parents:
        raise RecordError("FORBIDDEN")
    return dest


def content(path):
    if path is None or not os.path.lexists(path):
        return None
    return path.read_bytes()


def material(root, sid):
    directory, state = snapshot.load(root, sid)
    if state["phase"] != "complete":
        raise RecordError("INTEGRITY_FAILED")
    refs, _ = snapshot.verify_graph(directory, state)
    current = state["source"]["head"]["root_manifest"]
    raw = (directory / state["objects"][current["id"]]["filename"]).read_bytes()
    graph = manifest.parse_json(manifest.verify_bytes(raw, current))
    result = []
    for entry in graph["records"]:
        result.append((entry["record"]["source"]["relative_path"], entry["object"]))
    for entry in graph["attachments"]:
        result.append((entry["attachment"]["source_relative_path"], entry["object"]))
    if any(not isinstance(path, str) for path, _ in result):
        raise RecordError("INTEGRITY_FAILED")
    return directory, state, result


def preview(x):
    root = git_store.safe_dir(x["private_dir"])
    target = target_dir(x["approved_root"], x["target_dir"])
    directory, state, items = material(root, x["snapshot_id"])
    approved = x.get("approved_existing", {})
    if not isinstance(approved, dict):
        raise RecordError("INVALID_ARGUMENT")
    files = []
    identities = set()
    for rel, ref in sorted(items):
        path = destination(target, rel)
        raw = content(path)
        if path is not None and raw is not None:
            st = os.stat(path, follow_symlinks=False)
            identity = (st.st_dev, st.st_ino)
            if identity in identities:
                raise RecordError("FORBIDDEN")
            identities.add(identity)
        before = None if raw is None else sha(raw)
        if (
            before is not None
            and before != ref["sha256"]
            and approved.get(rel) != before
        ):
            raise RecordError("REVISION_CONFLICT")
        files.append(
            {
                "path": rel,
                "before_sha256": before,
                "after_sha256": ref["sha256"],
                "size": ref["size"],
                "object_id": ref["id"],
            }
        )
    if set(approved) - {f["path"] for f in files}:
        raise RecordError("FORBIDDEN")
    plan = {
        "schema_version": 1,
        "snapshot_id": x["snapshot_id"],
        "snapshot_receipt_hash": object_hash(state["receipt"]),
        "approved_root": str(git_store.safe_dir(x["approved_root"])),
        "target_dir": str(target),
        "files": files,
    }
    digest = object_hash(plan)
    with git_store.locked(directory):
        stored = directory / ("preview-" + digest + ".json")
        if os.path.lexists(stored):
            if git_store.read_json(stored) != plan:
                raise RecordError("INTEGRITY_FAILED")
        else:
            git_store.write_json(stored, plan)
    return {
        "outcome": "preview",
        "plan": plan,
        "plan_hash": digest,
        "unrelated_files": "preserved",
        "atomicity": "single_file_with_durable_journal",
    }


def run(x):
    root = git_store.safe_dir(x["private_dir"])
    plan = x["plan"]
    if object_hash(plan) != x["plan_hash"]:
        raise RecordError("INTEGRITY_FAILED")
    target = target_dir(x["approved_root"], x["target_dir"])
    if (
        str(target) != plan["target_dir"]
        or str(git_store.safe_dir(x["approved_root"])) != plan["approved_root"]
    ):
        raise RecordError("FORBIDDEN")
    directory, state, items = material(root, plan["snapshot_id"])
    if object_hash(state["receipt"]) != plan["snapshot_receipt_hash"]:
        raise RecordError("INTEGRITY_FAILED")
    stored = directory / ("preview-" + x["plan_hash"] + ".json")
    if git_store.read_json(stored) != plan:
        raise RecordError("INTEGRITY_FAILED")
    expected = {path: ref for path, ref in items}
    if set(expected) != {f["path"] for f in plan["files"]} or len(plan["files"]) != len(
        expected
    ):
        raise RecordError("INTEGRITY_FAILED")
    for item in plan["files"]:
        ref = expected[item["path"]]
        if (item["after_sha256"], item["size"], item["object_id"]) != (
            ref["sha256"],
            ref["size"],
            ref["id"],
        ):
            raise RecordError("INTEGRITY_FAILED")
    session_id = "restore-" + object_hash(
        {"target": str(target), "snapshot": plan["snapshot_id"]}
    )
    journal = directory / (session_id + ".json")
    with git_store.locked(directory):
        if os.path.lexists(journal):
            progress = git_store.read_json(journal)
            if progress["plan_hash"] != x["plan_hash"]:
                raise RecordError("ID_REUSE")
        else:
            progress = {
                "schema_version": 1,
                "plan_hash": x["plan_hash"],
                "phase": "writing",
                "completed": [],
            }
            git_store.write_json(journal, progress)
        for item in plan["files"]:
            path = destination(target, item["path"], create_parents=True)
            existing = content(path)
            observed = None if existing is None else sha(existing)
            if observed == item["after_sha256"]:
                if item["path"] not in progress["completed"]:
                    progress["completed"].append(item["path"])
                    git_store.write_json(journal, progress)
                continue
            if (
                item["path"] in progress["completed"]
                or observed != item["before_sha256"]
            ):
                raise RecordError("REVISION_CONFLICT")
            ref = expected[item["path"]]
            raw = (
                b""
                if ref.get("encoding") == "inline-empty-v1"
                else (
                    directory / state["objects"][item["object_id"]]["filename"]
                ).read_bytes()
            )
            if len(raw) != item["size"] or sha(raw) != item["after_sha256"]:
                raise RecordError("INTEGRITY_FAILED")
            fd, tmp = tempfile.mkstemp(prefix=".restore-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                if content(path) != existing:
                    raise RecordError("REVISION_CONFLICT")
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            progress["completed"].append(item["path"])
            git_store.write_json(journal, progress)
        progress["phase"] = "complete"
        git_store.write_json(journal, progress)
    return {
        "outcome": "complete",
        "plan_hash": x["plan_hash"],
        "files": len(plan["files"]),
        "authority": "not_activated",
        "single_writer_required": True,
    }


def runtime_preview(x):
    """Create a frozen Git runtime preserving every operation and exact result."""
    from collections import Counter

    root = git_store.safe_dir(x["private_dir"])
    runtime = target_dir(x["approved_root"], x["runtime_dir"])
    directory, state, unused = material(root, x["snapshot_id"])
    head = state["source"]["head"]
    if head["control"]["phase"] != "frozen":
        raise RecordError("PHASE_FROZEN")
    rootref = head["root_manifest"]
    value = manifest.parse_json(
        (directory / state["objects"][rootref["id"]]["filename"]).read_bytes()
    )
    pages = {
        ref["id"]: (directory / state["objects"][ref["id"]]["filename"]).read_bytes()
        for ref in value["history_pages"]
    }
    operations, events, journal = manifest.validate_history(value, pages)
    try:
        if Counter(core.canonical(item["event"]) for item in journal) != Counter(
            core.canonical(event) for event in events
        ):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise RecordError("INTEGRITY_FAILED") from None
    entries = []
    for entry in journal:
        if (
            not isinstance(entry.get("scope"), dict)
            or entry.get("state", "complete") != "complete"
        ):
            raise RecordError("INTEGRITY_FAILED")
        if entry.get("project_id", state["project_id"]) != state["project_id"]:
            raise RecordError("INTEGRITY_FAILED")
        event = entry["event"]
        if (
            event.get("operation_id") != entry["operation_id"]
            or event.get("request_hash") != entry["request_hash"]
        ):
            raise RecordError("INTEGRITY_FAILED")
        converted = dict(entry)
        converted["state"] = "complete"
        entries.append(converted)
    control = {
        "mode": "git",
        "phase": "frozen",
        "epoch": head["control"]["epoch"],
        "version": head["version"],
        "policy_version": head["control"]["policy_version"],
        "record_metadata": "required-v1",
    }
    metadata = {
        "schema_version": 1,
        "project_id": state["project_id"],
        "records": [item["record"] for item in value["records"]],
        "attachments": [item["attachment"] for item in value["attachments"]],
    }
    plan = {
        "schema_version": 1,
        "snapshot_id": x["snapshot_id"],
        "snapshot_receipt_hash": object_hash(state["receipt"]),
        "approved_root": str(git_store.safe_dir(x["approved_root"])),
        "runtime_dir": str(runtime),
        "control": control,
        "metadata_catalog": metadata,
        "journal": entries,
    }
    plan_hash = object_hash(plan)
    expected = {"control.json": core.canonical(control)}
    for entry in entries:
        name = git_store.operation_path(runtime, entry["operation_id"]).name
        if name in expected:
            raise RecordError("INTEGRITY_FAILED")
        expected[name] = core.canonical(entry)
    actual = {entry.name for entry in os.scandir(runtime)}
    if actual - set(expected) - {"writer.lock", "record-metadata"}:
        raise RecordError("FORBIDDEN")
    if "record-metadata" in actual:
        md = git_store.safe_dir(runtime / "record-metadata")
        if {entry.name for entry in os.scandir(md)} != {
            "records.json"
        } or git_store.read_json(md / "records.json") != metadata:
            raise RecordError("REVISION_CONFLICT")
    if "writer.lock" in actual:
        info = os.lstat(runtime / "writer.lock")
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & git_store.REPARSE
            or not stat.S_ISREG(info.st_mode)
        ):
            raise RecordError("FORBIDDEN")
    for name in actual - {"writer.lock", "record-metadata"}:
        path = git_store.canonical_child(runtime, name)
        st = os.lstat(path)
        if (
            stat.S_ISLNK(st.st_mode)
            or getattr(st, "st_file_attributes", 0) & git_store.REPARSE
            or not stat.S_ISREG(st.st_mode)
            or path.read_bytes() != expected[name]
        ):
            raise RecordError("REVISION_CONFLICT")
    with git_store.locked(directory):
        stored = directory / ("runtime-preview-" + plan_hash + ".json")
        if os.path.lexists(stored):
            if git_store.read_json(stored) != plan:
                raise RecordError("INTEGRITY_FAILED")
        else:
            git_store.write_json(stored, plan)
    return {
        "outcome": "preview",
        "plan": plan,
        "plan_hash": plan_hash,
        "operation_count": len(entries),
        "authority": "not_activated",
    }


def runtime_apply(x):
    root = git_store.safe_dir(x["private_dir"])
    plan = x["plan"]
    plan_hash = x["plan_hash"]
    if object_hash(plan) != plan_hash:
        raise RecordError("INTEGRITY_FAILED")
    runtime = target_dir(x["approved_root"], x["runtime_dir"])
    if (
        str(runtime) != plan["runtime_dir"]
        or str(git_store.safe_dir(x["approved_root"])) != plan["approved_root"]
    ):
        raise RecordError("FORBIDDEN")
    directory, state, unused = material(root, plan["snapshot_id"])
    if (
        object_hash(state["receipt"]) != plan["snapshot_receipt_hash"]
        or git_store.read_json(directory / ("runtime-preview-" + plan_hash + ".json"))
        != plan
    ):
        raise RecordError("INTEGRITY_FAILED")
    expected = {
        git_store.operation_path(runtime, entry["operation_id"]).name: core.canonical(
            entry
        )
        for entry in plan["journal"]
    }
    if len(expected) != len(plan["journal"]):
        raise RecordError("INTEGRITY_FAILED")
    expected["control.json"] = core.canonical(plan["control"])
    with git_store.locked(directory), git_store.locked(runtime):
        actual = {entry.name for entry in os.scandir(runtime)}
        if actual - set(expected) - {"writer.lock", "record-metadata"}:
            raise RecordError("FORBIDDEN")
        md = runtime / "record-metadata"
        if not os.path.lexists(md):
            md.mkdir(mode=0o700)
        md = git_store.safe_dir(md)
        mdfile = md / "records.json"
        existing = {entry.name for entry in os.scandir(md)}
        if existing - {"records.json"}:
            raise RecordError("FORBIDDEN")
        if os.path.lexists(mdfile):
            if git_store.read_json(mdfile) != plan["metadata_catalog"]:
                raise RecordError("REVISION_CONFLICT")
        else:
            git_store.write_json(mdfile, plan["metadata_catalog"])
        for name in sorted(expected, key=lambda name: name == "control.json"):
            path = git_store.canonical_child(runtime, name, allow_missing=True)
            raw = expected[name]
            if os.path.lexists(path):
                st = os.lstat(path)
                if (
                    stat.S_ISLNK(st.st_mode)
                    or getattr(st, "st_file_attributes", 0) & git_store.REPARSE
                    or not stat.S_ISREG(st.st_mode)
                    or path.read_bytes() != raw
                ):
                    raise RecordError("REVISION_CONFLICT")
                continue
            fd, tmp = tempfile.mkstemp(prefix=".runtime-restore-", dir=runtime)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(raw)
                    out.flush()
                    os.fsync(out.fileno())
                os.link(tmp, path)
                os.unlink(tmp)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    return {
        "outcome": "complete",
        "plan_hash": plan_hash,
        "operation_count": len(plan["journal"]),
        "control": plan["control"],
        "authority": "not_activated",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["preview", "apply", "runtime-preview", "runtime-apply"]
    )
    parser.add_argument("--input")
    args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding="utf-8-sig") as source:
                data = json.load(source)
        else:
            data = json.load(sys.stdin)
        result = {
            "preview": preview,
            "apply": run,
            "runtime-preview": runtime_preview,
            "runtime-apply": runtime_apply,
        }[args.command](data)
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
