"""Promote a verified frozen Git runtime into its fixed binding, preserving namespaces.

Only the named control, hashed operation journals, and metadata catalog are
replaced. Route remains frozen. No external fence is independently proven.
"""

import argparse
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import core
import git_store
import restore
import routing
from core import RecordError, ident, object_hash, sha

JOURNAL = re.compile(r"^operation-[0-9a-f]{64}\.json$")


def canonical_files(runtime):
    runtime = git_store.safe_dir(runtime)
    result = {}
    with os.scandir(runtime) as scan:
        entries = list(scan)
    for entry in entries:
        name = entry.name
        if name == "control.json" or JOURNAL.fullmatch(name):
            path = git_store.canonical_child(runtime, name)
            info = os.lstat(path)
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & git_store.REPARSE
            ):
                raise RecordError("FORBIDDEN")
            result[name] = path.read_bytes()
        elif name.lower().endswith(".json"):
            raise RecordError("INTEGRITY_FAILED")
    md = runtime / "record-metadata"
    if os.path.lexists(md):
        md = git_store.safe_dir(md)
        if {item.name for item in os.scandir(md)} != {"records.json"}:
            raise RecordError("INTEGRITY_FAILED")
        path = md / "records.json"
        info = os.lstat(path)
        if (
            not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & git_store.REPARSE
        ):
            raise RecordError("FORBIDDEN")
        result["record-metadata/records.json"] = path.read_bytes()
    return result


def session(root, operation, create=False):
    ident(operation)
    parent = root / "promotions"
    if not os.path.lexists(parent):
        if not create:
            raise RecordError("NOT_FOUND")
        parent.mkdir(mode=0o700)
    parent = git_store.safe_dir(parent)
    path = parent / sha(operation.encode("utf-8"))
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            raise RecordError("ID_REUSE") from None
    return git_store.safe_dir(path)


def desired(x, root):
    source = restore.target_dir(x["approved_root"], x["source_runtime"])
    args = {
        "private_dir": str(root),
        "snapshot_id": x["snapshot_id"],
        "approved_root": x["approved_root"],
        "runtime_dir": str(source),
    }
    preview = restore.runtime_preview(args)
    if preview["plan_hash"] != x["source_plan_hash"]:
        raise RecordError("REVISION_CONFLICT")
    control = git_store.get_control(source)
    if (
        control["mode"] != "git"
        or control["phase"] != "frozen"
        or control.get("record_metadata") != "required-v1"
    ):
        raise RecordError("PHASE_FROZEN")
    git_store.metadata_catalog(source, control, x["project_id"])
    files = canonical_files(source)
    if "control.json" not in files or "record-metadata/records.json" not in files:
        raise RecordError("INTEGRITY_FAILED")
    return source, preview, files


def verify_project(root, snapshot_id, binding, project_id):
    project = git_store.safe_dir(binding["project_directory"])
    _, _, items = restore.material(root, snapshot_id)
    names = [path for path, _ in items]
    git_store.ensure_unique_files(project, names)
    req = {
        "schema_version": 1,
        "request_id": "promotion-inventory",
        "project_id": project_id,
        "actor_role": "pm",
        "scope": {"project_id": project_id, "actions": [], "records": []},
    }
    if set(git_store.inventory(project, req)["paths"]) != set(names):
        raise RecordError("INTEGRITY_FAILED")
    for rel, ref in items:
        data = restore.content(restore.destination(project, rel))
        if data is None or len(data) != ref["size"] or sha(data) != ref["sha256"]:
            raise RecordError("REVISION_CONFLICT")
    return object_hash(
        [
            {"path": path, "sha256": ref["sha256"], "size": ref["size"]}
            for path, ref in sorted(items)
        ]
    )


def binding_for(registry, project):
    registry.require_no_activation(project)
    binding = registry.bind(project)
    if (
        binding["backend"] not in ("git_local", "google_head")
        or binding["route_phase"] != "frozen"
    ):
        raise RecordError("PHASE_FROZEN")
    git_store.safe_dir(binding["runtime_directory"])
    return binding


def verify_transition(
    root,
    transition_id,
    intent_hash,
    project_id,
    snapshot_id,
    binding,
    promotion_operation=None,
    promotion_plan_hash=None,
):
    import transition

    _, state = transition.load(root, transition_id)
    if state.get("project_id") != project_id:
        raise RecordError("FORBIDDEN")
    pending = state.get("pending")
    if (
        pending is not None
        and pending.get("step") == "target_frozen"
        and object_hash(pending) == intent_hash
    ):
        intent = pending
    else:
        matches = [
            receipt
            for receipt in state.get("receipts", [])
            if receipt.get("step") == "target_frozen"
            and receipt.get("intent_hash") == intent_hash
        ]
        if (
            len(matches) != 1
            or promotion_operation is None
            or matches[0].get("observation", {}).get("promotion_operation")
            != promotion_operation
            or matches[0].get("observation", {}).get("promotion_plan_hash")
            != promotion_plan_hash
        ):
            raise RecordError("OUTCOME_UNKNOWN")
        intent = None
    snapshots = [
        receipt
        for receipt in state.get("receipts", [])
        if receipt.get("step") == "snapshot"
    ]
    if (
        len(snapshots) != 1
        or snapshots[0].get("observation", {}).get("snapshot_id") != snapshot_id
    ):
        raise RecordError("INTEGRITY_FAILED")
    import snapshot

    _, snapshot_state = snapshot.load(root, snapshot_id)
    if object_hash(snapshot_state.get("receipt")) != snapshots[0]["observation"].get(
        "receipt_sha256"
    ):
        raise RecordError("INTEGRITY_FAILED")
    source = state["source"]
    source_doc = snapshot_state.get("source", {})
    head = source_doc.get("head", {})
    control = head.get("control", {})
    receipt = snapshot_state["receipt"]
    if (
        snapshot_state.get("phase") != "complete"
        or source.get("mode") != "google"
        or source_doc.get("document_id") != source.get("resource_id")
        or source_doc.get("revision") != receipt.get("source_revision")
        or head.get("root_manifest") != receipt.get("root_manifest")
        or head.get("version") != receipt.get("source_version")
        or control.get("epoch") != receipt.get("source_epoch")
        or control.get("mode") != "google"
        or control.get("phase") != "frozen"
        or control.get("epoch") != source.get("epoch") + 1
        or head.get("version") != source.get("version") + 1
    ):
        raise RecordError("INTEGRITY_FAILED")
    if intent is not None:
        if intent["target"]["mode"] != "git" or intent["source"]["mode"] != "google":
            raise RecordError("FORBIDDEN")
        if (
            binding["backend"] == "google_head"
            and binding["locator"]["document_id"] != intent["source"]["resource_id"]
        ):
            raise RecordError("FORBIDDEN")
    return state


def prepare(x, registry):
    if x.get("actor_role") != "pm":
        raise RecordError("FORBIDDEN")
    project = ident(x["project_id"])
    operation = ident(x["operation_id"])
    root = git_store.safe_dir(x["private_dir"])
    binding = binding_for(registry, project)
    verify_transition(
        root,
        x["transition_id"],
        x["transition_intent_sha256"],
        project,
        x["snapshot_id"],
        binding,
    )
    target = git_store.safe_dir(binding["runtime_directory"])
    source, preview, wanted = desired(x, root)
    if source == target or source in target.parents or target in source.parents:
        raise RecordError("FORBIDDEN")
    with git_store.locked(target):
        if binding_for(registry, project) != binding:
            raise RecordError("REVISION_CONFLICT")
        project_hash = verify_project(root, x["snapshot_id"], binding, project)
        old = canonical_files(target)
        # All old operation IDs and original receipts must survive byte-identically.
        for name, raw in old.items():
            if JOURNAL.fullmatch(name) and (name not in wanted or wanted[name] != raw):
                raise RecordError("INTEGRITY_FAILED")
        if "control.json" in old:
            control = git_store.get_control(target)
            next_control = preview["plan"]["control"]
            if (
                control["mode"] != "git"
                or next_control["epoch"] < control["epoch"]
                or next_control["version"] < control["version"]
            ):
                raise RecordError("STALE_EPOCH")
            git_store.metadata_catalog(target, control, project)
            git_store.journal_entries(target)
        elif any(
            JOURNAL.fullmatch(name) or name.startswith("record-metadata/")
            for name in old
        ):
            raise RecordError("INTEGRITY_FAILED")
        if set(old) - set(wanted):
            raise RecordError("INTEGRITY_FAILED")
        files = [
            {
                "path": name,
                "before_sha256": sha(old[name]) if name in old else None,
                "after_sha256": sha(wanted[name]),
                "size": len(wanted[name]),
            }
            for name in sorted(wanted, key=lambda n: (n == "control.json", n))
        ]
        plan = {
            "schema_version": 1,
            "project_id": project,
            "operation_id": operation,
            "transition_id": x["transition_id"],
            "transition_intent_sha256": x["transition_intent_sha256"],
            "binding": binding,
            "source_runtime": str(source),
            "source_plan_hash": preview["plan_hash"],
            "project_hash": project_hash,
            "snapshot_id": x["snapshot_id"],
            "approved_root": x["approved_root"],
            "files": files,
        }
        directory = session(root, operation, True)
        backup = directory / "backup"
        backup.mkdir(mode=0o700)
        for name, raw in old.items():
            path = backup.joinpath(*name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
        state = {
            "schema_version": 1,
            "operation_id": operation,
            "plan": plan,
            "plan_hash": object_hash(plan),
            "phase": "prepared",
            "completed": [],
        }
        git_store.write_json(directory / "promotion.json", state)
        return {
            "outcome": "prepared",
            "plan": plan,
            "plan_hash": state["plan_hash"],
            "old_files_backed_up": len(old),
            "route_phase": "frozen",
        }


def apply(x, registry):
    if x.get("actor_role") != "pm":
        raise RecordError("FORBIDDEN")
    root = git_store.safe_dir(x["private_dir"])
    directory = session(root, x["operation_id"])
    with git_store.locked(directory):
        state = git_store.read_json(directory / "promotion.json")
        plan = state["plan"]
        if (
            state["operation_id"] != x["operation_id"]
            or state["plan_hash"] != x["plan_hash"]
            or object_hash(plan) != x["plan_hash"]
        ):
            raise RecordError("ID_REUSE")
        binding = binding_for(registry, plan["project_id"])
        if state["phase"] != "complete" and binding != plan["binding"]:
            raise RecordError("REVISION_CONFLICT")
        if state["phase"] == "complete" and (
            binding["runtime_directory"] != plan["binding"]["runtime_directory"]
            or binding["project_directory"] != plan["binding"]["project_directory"]
        ):
            raise RecordError("REVISION_CONFLICT")
        verify_transition(
            root,
            plan["transition_id"],
            plan["transition_intent_sha256"],
            plan["project_id"],
            plan["snapshot_id"],
            binding,
            plan["operation_id"] if state["phase"] == "complete" else None,
            state["plan_hash"] if state["phase"] == "complete" else None,
        )
        target = git_store.safe_dir(binding["runtime_directory"])
        source, preview, wanted = desired(
            {
                "project_id": plan["project_id"],
                "approved_root": plan["approved_root"],
                "source_runtime": plan["source_runtime"],
                "snapshot_id": plan["snapshot_id"],
                "source_plan_hash": plan["source_plan_hash"],
            },
            root,
        )
        with git_store.locked(target):
            if binding_for(registry, plan["project_id"]) != binding:
                raise RecordError("REVISION_CONFLICT")
            if (
                verify_project(root, plan["snapshot_id"], binding, plan["project_id"])
                != plan["project_hash"]
            ):
                raise RecordError("REVISION_CONFLICT")
            actual = canonical_files(target)
            if set(actual) - {item["path"] for item in plan["files"]}:
                raise RecordError("INTEGRITY_FAILED")
            if state["phase"] == "complete":
                if set(actual) != {item["path"] for item in plan["files"]} or any(
                    sha(actual[item["path"]]) != item["after_sha256"]
                    for item in plan["files"]
                ):
                    raise RecordError("REVISION_CONFLICT")
                return {
                    "outcome": "no_write",
                    "plan_hash": state["plan_hash"],
                    "binding": binding,
                    "snapshot_id": plan["snapshot_id"],
                    "route_phase": "frozen",
                    "authority": "not_activated",
                }
            for item in plan["files"]:
                name = item["path"]
                raw = actual.get(name)
                observed = None if raw is None else sha(raw)
                if observed not in (item["before_sha256"], item["after_sha256"]):
                    raise RecordError("REVISION_CONFLICT")
                if item["before_sha256"] is not None:
                    backup = directory / "backup"
                    path = backup.joinpath(*name.split("/"))
                    git_store.safe_dir(path.parent)
                    info = os.lstat(path)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & git_store.REPARSE
                        or sha(path.read_bytes()) != item["before_sha256"]
                    ):
                        raise RecordError("INTEGRITY_FAILED")
                if (
                    sha(wanted[name]) != item["after_sha256"]
                    or len(wanted[name]) != item["size"]
                ):
                    raise RecordError("INTEGRITY_FAILED")
            state["phase"] = "applying"
            git_store.write_json(directory / "promotion.json", state)
            for item in plan["files"]:
                name = item["path"]
                if (
                    name == "control.json"
                    and verify_project(
                        root, plan["snapshot_id"], binding, plan["project_id"]
                    )
                    != plan["project_hash"]
                ):
                    raise RecordError("REVISION_CONFLICT")
                rel = name.split("/")
                parent = target
                for part in rel[:-1]:
                    path = git_store.canonical_child(parent, part, allow_missing=True)
                    if not os.path.lexists(path):
                        path.mkdir(mode=0o700)
                    parent = git_store.safe_dir(path)
                dest = git_store.canonical_child(parent, rel[-1], allow_missing=True)
                existing = dest.read_bytes() if os.path.lexists(dest) else None
                current = None if existing is None else sha(existing)
                if current == item["after_sha256"]:
                    if name not in state["completed"]:
                        state["completed"].append(name)
                        git_store.write_json(directory / "promotion.json", state)
                    continue
                if current != item["before_sha256"]:
                    raise RecordError("REVISION_CONFLICT")
                fd, tmp = tempfile.mkstemp(prefix=".promote-", dir=parent)
                try:
                    with os.fdopen(fd, "wb") as out:
                        out.write(wanted[name])
                        out.flush()
                        os.fsync(out.fileno())
                    existing_components = (
                        dest.read_bytes() if os.path.lexists(dest) else None
                    )
                    if (
                        None
                        if existing_components is None
                        else sha(existing_components)
                    ) != current:
                        raise RecordError("REVISION_CONFLICT")
                    if current is None:
                        os.link(tmp, dest)
                        os.unlink(tmp)
                    else:
                        os.replace(tmp, dest)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                state["completed"].append(name)
                git_store.write_json(directory / "promotion.json", state)
            state["phase"] = "complete"
            git_store.write_json(directory / "promotion.json", state)
        return {
            "outcome": "complete",
            "plan_hash": state["plan_hash"],
            "binding": binding,
            "snapshot_id": plan["snapshot_id"],
            "route_phase": "frozen",
            "authority": "not_activated",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "apply"])
    parser.add_argument("--input")
    args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding="utf-8-sig") as source:
                data = json.load(source)
        else:
            data = json.load(sys.stdin)
        registry = routing.Registry(routing.discover_workspace())
        result = {"prepare": prepare, "apply": apply}[args.command](data, registry)
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
