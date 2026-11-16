"""Scoped Git-record store with a cooperative lock and durable operation journal."""

import base64
import contextlib
import copy
import difflib
import json
import os
import pathlib
import re
import stat
import tempfile
import time
from datetime import datetime, timezone
from core import (
    RecordError,
    canonical,
    check_control,
    ident,
    object_hash,
    request_hash,
    sha,
    status_patch,
    validate_request,
)
from privacy import check as privacy_check

REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
KINDS = {
    "status.md": "status",
    "plan.md": "plan",
    "overview.md": "plan",
    "progress.md": "log",
}
DIR_KINDS = {
    "tasks": "task",
    "research": "research",
    "acceptance": "acceptance",
    "log": "log",
    "attachments": "attachment",
}


def safe_dir(path):
    p = pathlib.Path(path)
    if not p.is_absolute():
        raise RecordError("INVALID_ARGUMENT")
    cur = pathlib.Path(p.anchor)
    for part in p.parts[1:]:
        if part in ("..", "."):
            raise RecordError("INVALID_ARGUMENT")
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError:
            raise RecordError("NOT_FOUND") from None
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
            raise RecordError("FORBIDDEN")
    if not p.is_dir():
        raise RecordError("NOT_FOUND")
    return p


WINDOWS_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)


def relative_path(path):
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or ":" in path
        or path.startswith("/")
    ):
        raise RecordError("FORBIDDEN")
    parts = path.split("/")
    if any(
        not part
        or part in (".", "..")
        or (
            part.startswith(".")
            and not (part == ".gitkeep" and index == len(parts) - 1)
        )
        or part.endswith((".", " "))
        or WINDOWS_RESERVED.fullmatch(part)
        for index, part in enumerate(parts)
    ):
        raise RecordError("FORBIDDEN")
    return parts


def canonical_child(parent, part, allow_missing=False):
    """Reject aliases, including Windows case, trailing-dot and 8.3 paths."""
    try:
        with os.scandir(parent) as entries:
            names = [entry.name for entry in entries]
    except OSError:
        raise RecordError("NOT_FOUND") from None
    same = [name for name in names if name.casefold() == part.casefold()]
    candidate = parent / part
    if len(same) > 1 or (same and same[0] != part):
        raise RecordError("FORBIDDEN")
    if not same:
        # Existing under another spelling includes NTFS 8.3 aliases.
        if os.path.lexists(candidate):
            raise RecordError("FORBIDDEN")
        if not allow_missing:
            raise RecordError("NOT_FOUND")
    return candidate


def target_path(project, relative, allow_missing=False):
    parts = relative_path(relative)
    cur = project
    for index, part in enumerate(parts):
        cur = canonical_child(
            cur, part, allow_missing=allow_missing and index == len(parts) - 1
        )
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if allow_missing and index == len(parts) - 1:
                return cur
            raise RecordError("NOT_FOUND") from None
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
            raise RecordError("FORBIDDEN")
    if cur.exists() and not cur.is_file():
        raise RecordError("FORBIDDEN")
    if parts[-1] == ".gitkeep" and (not cur.exists() or cur.stat().st_size != 0):
        raise RecordError("INTEGRITY_FAILED")
    return cur


def project_dir(notes_root, project_id):
    root = safe_dir(notes_root)
    ident(project_id)
    projects = safe_dir(root / "projects")
    return safe_dir(canonical_child(projects, project_id))


def ensure_unique_files(project, paths):
    identities = set()
    for relative in paths:
        path = target_path(project, relative, allow_missing=True)
        if path.exists():
            st = os.stat(path, follow_symlinks=False)
            identity = (st.st_dev, st.st_ino)
            if identity in identities:
                raise RecordError("FORBIDDEN")
            identities.add(identity)


def runtime_dir(path, project, project_id):
    root = safe_dir(path)
    if root == project or project in root.parents:
        raise RecordError("FORBIDDEN")
    name = sha((str(project.resolve()) + "\0" + project_id).encode())
    sub = root / name
    if sub.exists():
        return safe_dir(sub)
    try:
        sub.mkdir(mode=0o700)
    except FileExistsError:
        pass
    return safe_dir(sub)


def write_json(path, obj):
    if os.path.lexists(path):
        st = os.lstat(path)
        if (
            stat.S_ISLNK(st.st_mode)
            or getattr(st, "st_file_attributes", 0) & REPARSE
            or not stat.S_ISREG(st.st_mode)
        ):
            raise RecordError("FORBIDDEN")
    raw = canonical(obj)
    fd, name = tempfile.mkstemp(prefix=".records-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
            raise RecordError("FORBIDDEN")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except RecordError:
        raise
    except (OSError, ValueError):
        raise RecordError("CONTROL_UNAVAILABLE") from None


def operation_path(runtime, operation_id):
    """以 exact ID 的 digest 定位，避免 Windows case/device-name 別名。"""
    ident(operation_id)
    return runtime / ("operation-" + sha(operation_id.encode("utf-8")) + ".json")


def journal_entries(runtime):
    """只接受新 journal layout；舊 raw-ID 檔案保留並拒絕，不默默重送。"""
    entries = []
    for path in sorted(runtime.glob("*.json")):
        if path.name == "control.json":
            continue
        if not path.name.startswith("operation-"):
            raise RecordError("INTEGRITY_FAILED")
        entry = read_json(path)
        operation_id = entry.get("operation_id")
        try:
            expected = operation_path(runtime, operation_id)
        except RecordError:
            raise RecordError("INTEGRITY_FAILED") from None
        if path.name != expected.name:
            raise RecordError("INTEGRITY_FAILED")
        entries.append(entry)
    return entries


def read_operation(runtime, operation_id):
    entry = read_json(operation_path(runtime, operation_id))
    if entry.get("operation_id") != operation_id:
        raise RecordError("INTEGRITY_FAILED")
    return entry


@contextlib.contextmanager
def locked(runtime):
    """OS-released local cooperative lock; a crash must not leave a stale lock."""
    path = runtime / "writer.lock"
    if os.path.lexists(path):
        st = os.lstat(path)
        if (
            stat.S_ISLNK(st.st_mode)
            or getattr(st, "st_file_attributes", 0) & REPARSE
            or not stat.S_ISREG(st.st_mode)
        ):
            raise RecordError("FORBIDDEN")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        raise RecordError("IN_FLIGHT") from None
    acquired = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RecordError("IN_FLIGHT") from None
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RecordError("IN_FLIGHT") from None
        acquired = True
        yield
    finally:
        if acquired:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def get_control(runtime):
    c = read_json(runtime / "control.json")
    if (
        set(c)
        not in (
            {"mode", "phase", "epoch", "version", "policy_version"},
            {"mode", "phase", "epoch", "version", "policy_version", "record_metadata"},
        )
        or ("record_metadata" in c and c["record_metadata"] != "required-v1")
        or c["mode"] not in ("git", "google")
        or c["phase"] not in ("steady", "frozen", "importing", "restoring")
        or type(c["epoch"]) is not int
        or c["epoch"] < 1
        or type(c["version"]) is not int
        or c["version"] < 0
    ):
        raise RecordError("INTEGRITY_FAILED")
    return c


def metadata_catalog(runtime, control, project_id):
    """Load the imported stable-ID catalog, or reject a missing/corrupt required catalog."""
    directory = runtime / "record-metadata"
    required = control.get("record_metadata") == "required-v1"
    if not required:
        if os.path.lexists(directory):
            raise RecordError("INTEGRITY_FAILED")
        return None
    safe_dir(directory)
    value = read_json(directory / "records.json")
    if (
        set(value) != {"schema_version", "project_id", "records", "attachments"}
        or value["schema_version"] != 1
        or value["project_id"] != project_id
        or not isinstance(value["records"], list)
        or not isinstance(value["attachments"], list)
    ):
        raise RecordError("INTEGRITY_FAILED")
    seen_ids, seen_paths = set(), set()
    for item in value["records"]:
        if (
            not isinstance(item, dict)
            or not {
                "record_id",
                "kind",
                "created_at",
                "updated_at",
                "source",
                "body",
                "metadata",
                "relations",
            }
            == set(item)
            or item["body"] != {"format": "utf-8"}
        ):
            raise RecordError("INTEGRITY_FAILED")
        ident(item["record_id"])
        source = item.get("source")
        if not isinstance(source, dict) or not {
            "relative_path",
            "content_sha256",
            "original_time",
        } <= set(source):
            raise RecordError("INTEGRITY_FAILED")
        rel = source["relative_path"]
        relative_path(rel)
        if item["record_id"] in seen_ids or rel.casefold() in seen_paths:
            raise RecordError("INTEGRITY_FAILED")
        seen_ids.add(item["record_id"])
        seen_paths.add(rel.casefold())
    for item in value["attachments"]:
        if not isinstance(item, dict):
            raise RecordError("INTEGRITY_FAILED")
        ident(item.get("attachment_id"))
        rel = item.get("source_relative_path")
        relative_path(rel)
        if item["attachment_id"] in seen_ids or rel.casefold() in seen_paths:
            raise RecordError("INTEGRITY_FAILED")
        seen_ids.add(item["attachment_id"])
        seen_paths.add(rel.casefold())
    return value


def metadata_for_changes(catalog, changes):
    if catalog is None:
        return None
    updated = copy.deepcopy(catalog)
    for change in changes:
        rid, rel = change["record_id"], change["path"]
        path_matches = [
            r
            for r in updated["records"]
            if r["source"]["relative_path"].casefold() == rel.casefold()
        ]
        id_matches = [r for r in updated["records"] if r["record_id"] == rid]
        if any(
            a["source_relative_path"].casefold() == rel.casefold()
            or a["attachment_id"] == rid
            for a in updated["attachments"]
        ):
            raise RecordError("FORBIDDEN")
        if path_matches or id_matches:
            if (
                len(path_matches) != 1
                or len(id_matches) != 1
                or path_matches[0] is not id_matches[0]
                or path_matches[0]["source"]["relative_path"] != rel
            ):
                raise RecordError("FORBIDDEN")
            item = path_matches[0]
            if item["source"]["content_sha256"] != change["before_sha256"]:
                raise RecordError("INTEGRITY_FAILED")
        else:
            if change["before_sha256"] is not None:
                raise RecordError("INTEGRITY_FAILED")
            item = {
                "record_id": rid,
                "kind": KINDS.get(rel, DIR_KINDS.get(rel.split("/")[0], "research")),
                "created_at": None,
                "updated_at": None,
                "source": {
                    "relative_path": rel,
                    "content_sha256": None,
                    "original_time": None,
                },
                "body": {"format": "utf-8"},
                "metadata": {},
                "relations": [],
            }
            updated["records"].append(item)
        item["source"]["content_sha256"] = change["after_sha256"]
    return updated


def write_catalog_update(runtime, entry):
    change = entry.get("metadata_update")
    if change is None:
        return
    path = safe_dir(runtime / "record-metadata") / "records.json"
    current = read_json(path)
    current_hash = object_hash(current)
    if current_hash == change["after_sha256"]:
        return
    if (
        current_hash != change["before_sha256"]
        or object_hash(change["after"]) != change["after_sha256"]
    ):
        raise RecordError("INTEGRITY_FAILED")
    write_json(path, change["after"])


def initialize(runtime, req):
    validate_request(req)
    if req["actor_role"] != "pm" or req.get("classification") != "unmigrated_git":
        raise RecordError("FORBIDDEN")
    with locked(runtime):
        if os.path.lexists(runtime / "control.json"):
            raise RecordError("ID_REUSE")
        if journal_entries(runtime):
            raise RecordError("INTEGRITY_FAILED")
        c = {
            "mode": "git",
            "phase": "steady",
            "epoch": 1,
            "version": 0,
            "policy_version": "1",
        }
        write_json(runtime / "control.json", c)
        return c


def inventory(project, req):
    validate_request(req)
    found = []

    def walk_error(error):
        raise RecordError("INTEGRITY_FAILED") from error

    for root, dirs, files in os.walk(project, followlinks=False, onerror=walk_error):
        for name in list(dirs) + files:
            path = pathlib.Path(root) / name
            st = os.lstat(path)
            if (
                stat.S_ISLNK(st.st_mode)
                or getattr(st, "st_file_attributes", 0) & REPARSE
            ):
                raise RecordError("FORBIDDEN")
            rel = path.relative_to(project).as_posix()
            parts = pathlib.PurePosixPath(rel).parts
            if any(
                part.startswith(".")
                and not (part == ".gitkeep" and index == len(parts) - 1)
                for index, part in enumerate(parts)
            ):
                raise RecordError("INTEGRITY_FAILED")
            if name == ".gitkeep" and (not stat.S_ISREG(st.st_mode) or st.st_size != 0):
                raise RecordError("INTEGRITY_FAILED")
        for name in files:
            found.append((pathlib.Path(root) / name).relative_to(project).as_posix())
    return {
        "paths": sorted(found),
        "note": "inventory only; scope must explicitly name records",
    }


def enumerate_records(project, runtime, req):
    with locked(runtime):
        return _enumerate_records(project, runtime, req)


def _enumerate_records(project, runtime, req):
    validate_request(req)
    control = get_control(runtime)
    if control["mode"] != "git":
        raise RecordError("STALE_MODE")
    ensure_unique_files(project, [spec["path"] for spec in req["scope"]["records"]])
    records = []
    attachments = []
    catalog = metadata_catalog(runtime, control, req["project_id"])
    metadata_stale = []
    for spec in req["scope"]["records"]:
        path = target_path(project, spec["path"])
        raw = path.read_bytes()
        if catalog is not None:
            matches = [
                r for r in catalog["records"] if r["record_id"] == spec["record_id"]
            ]
            matches += [
                a
                for a in catalog["attachments"]
                if a["attachment_id"] == spec["record_id"]
            ]
            by_path = [
                r["record_id"]
                for r in catalog["records"]
                if r["source"]["relative_path"].casefold() == spec["path"].casefold()
            ]
            by_path += [
                a["attachment_id"]
                for a in catalog["attachments"]
                if a["source_relative_path"].casefold() == spec["path"].casefold()
            ]
            if by_path and by_path != [spec["record_id"]]:
                raise RecordError("FORBIDDEN")
            if len(matches) == 1 and "record_id" in matches[0]:
                item = copy.deepcopy(matches[0])
                source = item["source"]
                if source["relative_path"] != spec["path"]:
                    raise RecordError("FORBIDDEN")
                if source["content_sha256"] == sha(raw):
                    try:
                        item["body"] = {
                            "format": "utf-8",
                            "content": raw.decode("utf-8"),
                        }
                    except UnicodeDecodeError:
                        raise RecordError("INTEGRITY_FAILED") from None
                    records.append(item)
                    continue
            elif len(matches) == 1:
                item = copy.deepcopy(matches[0])
                if item["source_relative_path"] != spec["path"]:
                    raise RecordError("FORBIDDEN")
                if item.get("sha256") == sha(raw) and item.get("size") == len(raw):
                    item["raw_bytes_b64"] = base64.b64encode(raw).decode("ascii")
                    attachments.append(item)
                    continue
            metadata_stale.append(spec["path"])
        kind = KINDS.get(
            spec["path"], DIR_KINDS.get(spec["path"].split("/")[0], "research")
        )
        fs_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = None
        if content is None or not spec["path"].lower().endswith(".md"):
            attachments.append(
                {
                    "attachment_id": spec["record_id"],
                    "logical_name": path.name,
                    "mime": "application/octet-stream",
                    "size": len(raw),
                    "sha256": sha(raw),
                    "locator_alias": None,
                    "source_relative_path": spec["path"],
                    "raw_bytes_b64": base64.b64encode(raw).decode("ascii"),
                    "metadata": {
                        "filesystem_mtime": fs_time,
                        "interpretation": "not_decoded",
                    },
                }
            )
            continue
        original_time = None
        if spec["path"] == "status.md":
            for line in content.splitlines():
                if line.startswith("## "):
                    break
                if line.startswith("updated:"):
                    original_time = line.split(":", 1)[1].strip().strip('"')
                    break
        records.append(
            {
                "record_id": spec["record_id"],
                "kind": kind,
                "created_at": None,
                "updated_at": original_time,
                "source": {
                    "relative_path": spec["path"],
                    "content_sha256": sha(raw),
                    "original_time": original_time,
                },
                "body": {"format": "utf-8", "content": content},
                "metadata": {
                    "filesystem_mtime": fs_time,
                    "semantic_role": "overview"
                    if spec["path"] == "overview.md"
                    else "frozen_progress"
                    if spec["path"] == "progress.md"
                    and (project / "status.md").is_file()
                    else "legacy_fallback"
                    if spec["path"] == "progress.md"
                    else "source",
                },
                "relations": [],
            }
        )
    expected_paths = {entry["path"] for entry in req["scope"]["records"]}
    discovered = set(inventory(project, req)["paths"])
    journal = []
    for entry in journal_entries(runtime):
        if {item["path"] for item in entry.get("files", [])} <= expected_paths:
            journal.append(entry)
    incomplete = [
        entry["operation_id"] for entry in journal if entry.get("state") != "complete"
    ]
    return {
        "schema_version": 1,
        "project_id": req["project_id"],
        "control": control,
        "version": control["version"],
        "records": records,
        "events": sorted(
            (
                entry["event"]
                for entry in journal
                if entry.get("state") == "complete" and "event" in entry
            ),
            key=lambda event: event["version"],
        ),
        "journal": journal,
        "attachments": attachments,
        "completeness": "complete"
        if expected_paths == discovered and not incomplete and not metadata_stale
        else "partial",
        "incomplete_operations": incomplete,
        **({"metadata_stale": sorted(metadata_stale)} if catalog is not None else {}),
        "unselected_paths": sorted(discovered - expected_paths),
        "status_authority": "status"
        if (project / "status.md").is_file()
        else "progress_fallback_unverified",
    }


def ensure_no_incomplete(runtime):
    for entry in journal_entries(runtime):
        if entry.get("state") != "complete":
            raise RecordError("IN_FLIGHT")


def spec_for(req, record_id):
    for item in req["scope"]["records"]:
        if item["record_id"] == record_id:
            return item
    raise RecordError("FORBIDDEN")


def changes_for(req, project):
    action = req["action"]
    payload = req["payload"]
    if action == "freeze":
        return []
    if action == "batch":
        result = []
        for item in payload["changes"]:
            child = dict(req)
            child.update(item)
            result.extend(changes_for(child, project))
        if len({c["path"] for c in result}) != len(result):
            raise RecordError("INVALID_ARGUMENT")
        ensure_unique_files(project, [change["path"] for change in result])
        return result
    spec = spec_for(req, req["target_record_id"])
    rel = spec["path"]
    relative_path(rel)
    if rel.split("/")[-1] == ".gitkeep":
        raise RecordError("FORBIDDEN")
    if req["actor_role"] != "pm" and (rel == "status.md" or rel.startswith("log/")):
        raise RecordError("FORBIDDEN")
    if rel == "progress.md":
        raise RecordError("FORBIDDEN")
    if action == "status_patch" and rel != "status.md":
        raise RecordError("FORBIDDEN")
    if rel == "status.md" and action != "status_patch":
        raise RecordError("FORBIDDEN")
    if action == "append" and not rel.startswith("log/"):
        raise RecordError("FORBIDDEN")
    path = target_path(project, rel, allow_missing=True)
    try:
        before = path.read_bytes() if path.exists() else None
    except OSError:
        raise RecordError("INTEGRITY_FAILED") from None
    old = sha(before) if before is not None else None
    if payload["base_sha256"] != old:
        raise RecordError("REVISION_CONFLICT")
    if action == "status_patch":
        if before is None:
            raise RecordError("NOT_FOUND")
        after = status_patch(before, payload["fields"])
    else:
        if action == "append" and before is None:
            before = b""
        try:
            previous = (before or b"").decode("utf-8")
        except UnicodeDecodeError:
            raise RecordError("INTEGRITY_FAILED") from None
        text = payload["content"]
        after = ((previous + text) if action == "append" else text).encode("utf-8")
    return [
        {
            "record_id": spec["record_id"],
            "path": rel,
            "before_sha256": old,
            "after_sha256": sha(after),
            "content": base64.b64encode(after).decode("ascii"),
        }
    ]


def preview(project, runtime, req, privacy):
    validate_request(req, True)
    rh = request_hash(req)
    with locked(runtime):
        control = get_control(runtime)
        if control["mode"] != "git":
            raise RecordError("STALE_MODE")
        journal_entries(runtime)
        journal_path = operation_path(runtime, req["operation_id"])
        if os.path.lexists(journal_path):
            old = read_operation(runtime, req["operation_id"])
            if old.get("request_hash") != rh:
                raise RecordError("ID_REUSE")
            if old.get("state") == "complete":
                return {
                    "outcome": "no_write",
                    "result": old["result"],
                    "request_hash": rh,
                }
            raise RecordError("MANUAL_REVIEW")
        ensure_no_incomplete(runtime)
        check_control(control, req["expected"])
        changes = changes_for(req, project)
        catalog = metadata_catalog(runtime, control, req["project_id"])
        catalog_after = metadata_for_changes(catalog, changes)
        safe_payload = {
            "request": {
                k: req[k]
                for k in (
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
            "decoded_content": [
                base64.b64decode(c["content"]).decode("utf-8") for c in changes
            ],
        }
        scan = privacy_check(safe_payload, privacy)
        public = []
        for change in changes:
            path = target_path(project, change["path"], allow_missing=True)
            before = path.read_bytes().decode("utf-8") if path.exists() else ""
            after = base64.b64decode(change["content"]).decode("utf-8")
            public.append(
                {k: v for k, v in change.items() if k != "content"}
                | {
                    "diff": "".join(
                        difflib.unified_diff(
                            before.splitlines(True),
                            after.splitlines(True),
                            fromfile="before",
                            tofile="after",
                        )
                    )
                }
            )
        plan = {
            "schema_version": 1,
            "operation_id": req["operation_id"],
            "request_hash": rh,
            "project_identity": sha(str(project.resolve()).encode("utf-8")),
            "runtime_identity": sha(str(runtime.resolve()).encode("utf-8")),
            "control": control,
            "changes": changes,
            "scan": scan,
            "metadata_before_sha256": object_hash(catalog)
            if catalog is not None
            else None,
            "metadata_after_sha256": object_hash(catalog_after)
            if catalog_after is not None
            else None,
        }
        return {
            "outcome": "planned",
            "plan": plan,
            "plan_hash": object_hash(plan),
            "files": public,
        }


def apply(project, runtime, req, privacy, plan, plan_hash):
    validate_request(req, True)
    rh = request_hash(req)
    if (
        not isinstance(plan, dict)
        or object_hash(plan) != plan_hash
        or plan.get("request_hash") != rh
        or plan.get("operation_id") != req["operation_id"]
    ):
        raise RecordError("INTEGRITY_FAILED")
    if plan.get("project_identity") != sha(
        str(project.resolve()).encode("utf-8")
    ) or plan.get("runtime_identity") != sha(str(runtime.resolve()).encode("utf-8")):
        raise RecordError("FORBIDDEN")
    with locked(runtime):
        journal_entries(runtime)
        journal_path = operation_path(runtime, req["operation_id"])
        if os.path.lexists(journal_path):
            entry = read_operation(runtime, req["operation_id"])
            if entry.get("request_hash") != rh:
                raise RecordError("ID_REUSE")
            if entry.get("state") == "complete":
                return {"outcome": "no_write", "result": entry["result"]}
            raise RecordError("MANUAL_REVIEW")
        ensure_no_incomplete(runtime)
        c = get_control(runtime)
        if c["mode"] != "git":
            raise RecordError("STALE_MODE")
        check_control(c, req["expected"])
        if plan.get("control") != c:
            raise RecordError("REVISION_CONFLICT")
        fresh = changes_for(req, project)
        if fresh != plan.get("changes"):
            raise RecordError("REVISION_CONFLICT")
        catalog = metadata_catalog(runtime, c, req["project_id"])
        catalog_after = metadata_for_changes(catalog, fresh)
        if plan.get("metadata_before_sha256") != (
            object_hash(catalog) if catalog is not None else None
        ) or plan.get("metadata_after_sha256") != (
            object_hash(catalog_after) if catalog_after is not None else None
        ):
            raise RecordError("REVISION_CONFLICT")
        safe_payload = {
            "request": {
                k: req[k]
                for k in (
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
            "changes": fresh,
            "decoded_content": [
                base64.b64decode(c["content"]).decode("utf-8") for c in fresh
            ],
        }
        current_scan = privacy_check(safe_payload, privacy)
        if (
            current_scan["content_sha256"] != plan.get("scan", {}).get("content_sha256")
            or current_scan["policy_sha256"]
            != plan.get("scan", {}).get("policy_sha256")
            or current_scan["rules_sha256"] != plan.get("scan", {}).get("rules_sha256")
        ):
            raise RecordError("SCAN_INCOMPLETE")
        entry = {
            "schema_version": 1,
            "operation_id": req["operation_id"],
            "request_hash": rh,
            "state": "accepted",
            "expected_control": c,
            "scope": req["scope"],
            "actor_role": req["actor_role"],
            "action": req["action"],
            "target_record_id": req["target_record_id"],
            "metadata_update": (
                {
                    "before_sha256": object_hash(catalog),
                    "after_sha256": object_hash(catalog_after),
                    "after": catalog_after,
                }
                if catalog is not None
                else None
            ),
            "files": [
                {
                    "path": v["path"],
                    "before_sha256": v["before_sha256"],
                    "after_sha256": v["after_sha256"],
                    "state": "pending",
                }
                for v in fresh
            ],
        }
        write_json(journal_path, entry)
        try:
            for file, change in zip(entry["files"], fresh):
                path = target_path(project, change["path"], allow_missing=True)
                actual = sha(path.read_bytes()) if path.exists() else None
                if actual != change["before_sha256"]:
                    raise RecordError("REVISION_CONFLICT")
                file["state"] = "writing"
                entry["state"] = "partial"
                write_json(journal_path, entry)
                fd, name = tempfile.mkstemp(
                    prefix=".workspace-records-", dir=path.parent
                )
                try:
                    with os.fdopen(fd, "wb") as out:
                        out.write(base64.b64decode(change["content"], validate=True))
                        out.flush()
                        os.fsync(out.fileno())
                    # Recheck after staging, immediately before replacement.
                    path = target_path(project, change["path"], allow_missing=True)
                    if (sha(path.read_bytes()) if path.exists() else None) != change[
                        "before_sha256"
                    ]:
                        raise RecordError("REVISION_CONFLICT")
                    if change["before_sha256"] is None:
                        os.link(name, path)
                        os.unlink(name)
                    else:
                        os.replace(name, path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
                if sha(path.read_bytes()) != change["after_sha256"]:
                    raise RecordError("INTEGRITY_FAILED")
                file["state"] = "written"
                write_json(journal_path, entry)
            write_catalog_update(runtime, entry)
            updated = dict(c)
            updated["version"] += 1
            if req["action"] == "freeze":
                updated["phase"] = "frozen"
                updated["epoch"] += 1
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
                "affected_ids": [v["record_id"] for v in fresh],
                "observed_at": datetime.now(timezone.utc).isoformat(),
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
                "occurred_at": result["observed_at"],
            }
            entry["state"] = "files_done"
            write_json(journal_path, entry)
            write_json(runtime / "control.json", updated)
            entry["state"] = "complete"
            write_json(journal_path, entry)
            return {"outcome": "complete", "result": result}
        except Exception:
            raise RecordError("PARTIAL") from None


def reconcile(project, runtime, req):
    validate_request(req)
    ident(req.get("operation_id"))
    with locked(runtime):
        journal_entries(runtime)
        entry = read_operation(runtime, req["operation_id"])
        if entry.get("scope") != req["scope"]:
            raise RecordError("FORBIDDEN")
        if entry.get("state") == "complete":
            return {"outcome": "complete", "result": entry["result"]}
        observations = []
        for f in entry.get("files", []):
            try:
                p = target_path(project, f["path"], allow_missing=True)
                actual = sha(p.read_bytes()) if p.exists() else None
                observations.append(
                    {
                        "path": f["path"],
                        "matches_before": actual == f["before_sha256"],
                        "matches_after": actual == f["after_sha256"],
                    }
                )
            except (OSError, RecordError):
                observations.append({"path": f["path"], "unreadable": True})
        return {
            "outcome": "manual_review",
            "files": observations,
            "journal_state": entry.get("state"),
        }
