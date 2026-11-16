"""Durable, offline acquisition of a complete immutable graph snapshot."""

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
import core
import git_store
import google_docs
import manifest
from core import RecordError, canonical, ident, object_hash, sha


def session(root, snapshot_id, create=False):
    ident(snapshot_id)
    parent = root / "snapshots"
    if not parent.exists():
        if not create:
            raise RecordError("NOT_FOUND")
        parent.mkdir(mode=0o700)
    git_store.safe_dir(parent)
    target = parent / sha(snapshot_id.encode("utf-8"))
    if create:
        try:
            target.mkdir(mode=0o700)
        except FileExistsError:
            raise RecordError("ID_REUSE") from None
    return git_store.safe_dir(target)


def begin(x):
    root = git_store.safe_dir(x["private_dir"])
    sid = ident(x["snapshot_id"])
    snap = google_docs.snapshot(
        x["document"],
        x["document_id"],
        x["head_tab_id"],
        x["summary_tab_id"],
        x["project_id"],
    )
    directory = session(root, sid, True)
    state = {
        "schema_version": 1,
        "snapshot_id": sid,
        "project_id": x["project_id"],
        "phase": "collecting",
        "source": snap,
        "objects": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    git_store.write_json(directory / "snapshot.json", state)
    return {
        "outcome": "collecting",
        "root_manifest": snap["head"]["root_manifest"],
        "source_revision": snap["revision"],
    }


def load(root, sid):
    directory = session(root, sid)
    state = git_store.read_json(directory / "snapshot.json")
    if state.get("snapshot_id") != sid:
        raise RecordError("INTEGRITY_FAILED")
    return directory, state


def accept(x):
    root = git_store.safe_dir(x["private_dir"])
    directory, state = load(root, x["snapshot_id"])
    with git_store.locked(directory):
        state = git_store.read_json(directory / "snapshot.json")
        if state["phase"] != "collecting":
            raise RecordError("MANUAL_REVIEW")
        ref = manifest.reference(x["reference"])
        if ref.get("encoding") == "inline-empty-v1":
            raise RecordError("FORBIDDEN")
        if x.get("readback_remote_id") != ref["id"]:
            raise RecordError("INTEGRITY_FAILED")
        # Connector runtime binds this ID to the downloaded file; only bytes are checked here.
        import connector_protocol

        raw = connector_protocol.file_bytes(root, x["downloaded_file"])
        manifest.verify_bytes(raw, ref)
        previous = state["objects"].get(ref["id"])
        if previous:
            if previous["reference"] != ref:
                raise RecordError("INTEGRITY_FAILED")
            return {"outcome": "no_write", "reference": ref}
        filename = "object-" + sha(ref["id"].encode("utf-8"))
        path = directory / filename
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
        except FileExistsError:
            st = os.lstat(path)
            if (
                not __import__("stat").S_ISREG(st.st_mode)
                or getattr(st, "st_file_attributes", 0) & git_store.REPARSE
            ):
                raise RecordError("FORBIDDEN")
            manifest.verify_bytes(path.read_bytes(), ref)
        except OSError:
            raise RecordError("INTEGRITY_FAILED") from None
        state["objects"][ref["id"]] = {"reference": ref, "filename": filename}
        git_store.write_json(directory / "snapshot.json", state)
        return {"outcome": "verified", "reference": ref}


def inspect(x):
    root = git_store.safe_dir(x["private_dir"])
    directory, state = load(root, x["snapshot_id"])
    with git_store.locked(directory):
        state = git_store.read_json(directory / "snapshot.json")
        return {
            "outcome": state["phase"],
            "verified_ids": sorted(state["objects"]),
            "source_revision": state["source"]["revision"],
        }


def verify_graph(directory, state):
    objects = state["objects"]
    refs = {}
    seen = set()
    active = set()
    graph = []
    versions = {}

    def get(ref):
        manifest.reference(ref)
        rid = ref["id"]
        if rid in refs and refs[rid] != ref:
            raise RecordError("INTEGRITY_FAILED")
        refs[rid] = ref
        if ref.get("encoding") == "inline-empty-v1":
            return b""
        entry = objects.get(rid)
        if not entry:
            raise RecordError("CONTROL_UNAVAILABLE")
        if entry["reference"] != ref:
            raise RecordError("INTEGRITY_FAILED")
        path = directory / entry["filename"]
        if path.parent != directory or path.name != "object-" + sha(
            rid.encode("utf-8")
        ):
            raise RecordError("INTEGRITY_FAILED")
        info = os.lstat(path)
        if (
            not __import__("stat").S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & git_store.REPARSE
        ):
            raise RecordError("FORBIDDEN")
        return manifest.verify_bytes(path.read_bytes(), ref)

    def visit(ref, upper_version=None):
        rid = ref["id"]
        if rid in active:
            raise RecordError("INTEGRITY_FAILED")
        if rid in seen:
            get(ref)
            if upper_version is not None and versions[rid] >= upper_version:
                raise RecordError("INTEGRITY_FAILED")
            return
        active.add(rid)
        value = manifest.parse_json(get(ref))
        manifest.validate_manifest(value, state["project_id"])
        if upper_version is not None and value["version"] >= upper_version:
            raise RecordError("INTEGRITY_FAILED")
        versions[rid] = value["version"]
        if (
            ref == state["source"]["head"]["root_manifest"]
            and value["version"] != state["source"]["head"]["version"]
        ):
            raise RecordError("INTEGRITY_FAILED")
        pages = {p["id"]: get(p) for p in value["history_pages"]}
        manifest.validate_history(value, pages)
        for entry in value["records"]:
            get(entry["object"])
        for entry in value["attachments"]:
            get(entry["object"])
        for page in pages.values():
            prior = manifest.parse_json(page).get("previous_root")
            if prior is not None:
                visit(prior, value["version"])
        graph.append(ref)
        active.remove(rid)
        seen.add(rid)

    visit(state["source"]["head"]["root_manifest"])
    return refs, graph


def finish(x):
    root = git_store.safe_dir(x["private_dir"])
    directory, state = load(root, x["snapshot_id"])
    with git_store.locked(directory):
        state = git_store.read_json(directory / "snapshot.json")
        if state["phase"] == "complete":
            return {"outcome": "complete", "receipt": state["receipt"]}
        if state["phase"] != "collecting":
            raise RecordError("MANUAL_REVIEW")
        old = state["source"]
        new = google_docs.snapshot(
            x["document"],
            old["document_id"],
            old["head_tab_id"],
            old["summary_tab_id"],
            state["project_id"],
        )
        if new["revision"] != old["revision"] or new["head"] != old["head"]:
            raise RecordError("REVISION_CONFLICT")
        refs, graph = verify_graph(directory, state)
        receipt = {
            "schema_version": 1,
            "snapshot_id": state["snapshot_id"],
            "project_id": state["project_id"],
            "source_revision": old["revision"],
            "source_epoch": old["head"]["control"]["epoch"],
            "source_version": old["head"]["version"],
            "root_manifest": old["head"]["root_manifest"],
            "objects": [refs[k] for k in sorted(refs)],
            "manifests": graph,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "completeness": "complete",
            "external_exclusivity": "not_proven",
            "future_gap": "unknown",
        }
        state.update(phase="complete", receipt=receipt)
        git_store.write_json(directory / "snapshot.json", state)
        return {"outcome": "complete", "receipt": receipt}


def git_capture(x):
    """Capture complete P2 RecordSet twice from an isolated frozen Git source."""
    root = git_store.safe_dir(x["private_dir"])
    approved = git_store.safe_dir(x["approved_root"])
    project = git_store.safe_dir(x["project_dir"])
    runtime = git_store.safe_dir(x["runtime_dir"])
    if (
        project == approved
        or approved not in project.parents
        or runtime == approved
        or approved not in runtime.parents
    ):
        raise RecordError("FORBIDDEN")
    req = x["request"]
    if req.get("actor_role") != "pm":
        raise RecordError("FORBIDDEN")
    first = git_store.enumerate_records(project, runtime, req)
    second = git_store.enumerate_records(project, runtime, req)
    if first != second:
        raise RecordError("REVISION_CONFLICT")
    if (
        first.get("completeness") != "complete"
        or first.get("incomplete_operations") != []
        or first.get("unselected_paths") != []
    ):
        raise RecordError("INTEGRITY_FAILED")
    control = first["control"]
    if control["mode"] != "git" or control["phase"] != "frozen":
        raise RecordError("PHASE_FROZEN")
    sid = ident(x["snapshot_id"])
    d = session(root, sid, True)
    raw = canonical(first)
    file = d / "git-recordset.json"
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    receipt = {
        "schema_version": 1,
        "snapshot_id": sid,
        "project_id": req["project_id"],
        "source_mode": "git",
        "project_identity": sha(str(project.resolve()).encode("utf-8")),
        "runtime_identity": sha(str(runtime.resolve()).encode("utf-8")),
        "source_epoch": control["epoch"],
        "source_version": control["version"],
        "recordset_sha256": sha(raw),
        "recordset_size": len(raw),
        "completeness": "complete",
        "external_exclusivity": "not_proven",
        "future_gap": "unknown",
    }
    state = {
        "schema_version": 1,
        "snapshot_id": sid,
        "project_id": req["project_id"],
        "phase": "complete_git",
        "receipt": receipt,
        "recordset_filename": "git-recordset.json",
    }
    git_store.write_json(d / "snapshot.json", state)
    return {"outcome": "complete", "receipt": receipt}


def read_git(root, sid):
    d, state = load(root, sid)
    if (
        state["phase"] != "complete_git"
        or state.get("recordset_filename") != "git-recordset.json"
    ):
        raise RecordError("INTEGRITY_FAILED")
    import connector_protocol

    raw = connector_protocol.file_bytes(root, str(d / "git-recordset.json"))
    if (
        len(raw) != state["receipt"]["recordset_size"]
        or sha(raw) != state["receipt"]["recordset_sha256"]
    ):
        raise RecordError("INTEGRITY_FAILED")
    value = manifest.parse_json(raw)
    if (
        value.get("project_id") != state["project_id"]
        or value.get("completeness") != "complete"
        or value.get("control", {}).get("phase") != "frozen"
    ):
        raise RecordError("INTEGRITY_FAILED")
    return state, value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["begin", "accept", "inspect", "finish", "git-capture"]
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
            "begin": begin,
            "accept": accept,
            "inspect": inspect,
            "finish": finish,
            "git-capture": git_capture,
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
