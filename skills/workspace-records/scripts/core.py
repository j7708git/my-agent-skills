"""Workspace records: validation, canonical hashes, and scoped requests."""

import hashlib
import json
import re
from datetime import datetime


class RecordError(Exception):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def object_hash(value):
    return sha(canonical(value))


ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
FIELDS = (
    "schema_version",
    "project_id",
    "action",
    "target_record_id",
    "actor_role",
    "expected",
    "payload",
    "scope",
)
ACTIONS = {"replace", "append", "status_patch", "freeze", "resume", "batch"}
STATUS_REQUIRED = {
    "schema",
    "state",
    "needs",
    "now",
    "next",
    "blocker",
    "updated",
    "code_dir",
}
STATUS_OPTIONAL = {
    "machine",
    "branch",
    "local_claim_by",
    "local_claim_session",
    "local_claim_updated",
}


def ident(value):
    if (
        not isinstance(value, str)
        or not ID.fullmatch(value)
        or value in (".", "..")
        or ".." in value
    ):
        raise RecordError("INVALID_ARGUMENT")
    return value


def request_hash(req):
    try:
        return object_hash({k: req[k] for k in FIELDS})
    except (KeyError, TypeError, ValueError):
        raise RecordError("INVALID_ARGUMENT") from None


def validate_request(req, write=False):
    if not isinstance(req, dict) or req.get("schema_version") != 1:
        raise RecordError("SCHEMA_UNSUPPORTED")
    ident(req.get("request_id"))
    ident(req.get("project_id"))
    role = req.get("actor_role")
    scope = req.get("scope")
    if (
        role not in ("pm", "worker", "reader")
        or not isinstance(scope, dict)
        or scope.get("project_id") != req["project_id"]
    ):
        raise RecordError("FORBIDDEN")
    actions = scope.get("actions")
    records = scope.get("records")
    if (
        not isinstance(actions, list)
        or not isinstance(records, list)
        or any(not isinstance(a, str) or a not in ACTIONS for a in actions)
    ):
        raise RecordError("INVALID_ARGUMENT")
    ids = set()
    paths = set()
    for r in records:
        if not isinstance(r, dict) or set(r) != {"record_id", "path", "fields"}:
            raise RecordError("INVALID_ARGUMENT")
        ident(r["record_id"])
        path = r["path"]
        fields = r["fields"]
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(fields, list)
            or not fields
            or any(not isinstance(f, str) or not f or f == "*" for f in fields)
        ):
            raise RecordError("INVALID_ARGUMENT")
        portable_path = path.casefold()
        if r["record_id"] in ids or portable_path in paths:
            raise RecordError("INVALID_ARGUMENT")
        ids.add(r["record_id"])
        paths.add(portable_path)
    if not write:
        return
    ident(req.get("operation_id"))
    action = req.get("action")
    if (
        action not in ACTIONS
        or action not in actions
        or role == "reader"
        or (action in ("freeze", "resume") and role != "pm")
    ):
        raise RecordError("FORBIDDEN")
    expected = req.get("expected")
    if (
        not isinstance(expected, dict)
        or set(expected) != {"mode", "phase", "epoch", "version"}
        or expected["mode"] not in ("git", "google")
        or expected["phase"] not in ("steady", "frozen", "importing", "restoring")
        or type(expected["epoch"]) is not int
        or expected["epoch"] < 1
        or type(expected["version"]) is not int
        or expected["version"] < 0
    ):
        raise RecordError("INVALID_ARGUMENT")
    target = req.get("target_record_id")
    if action == "batch":
        if (
            target is not None
            or set(req.get("payload", {})) != {"changes"}
            or not isinstance(req["payload"]["changes"], list)
            or not req["payload"]["changes"]
        ):
            raise RecordError("INVALID_ARGUMENT")
        seen = set()
        for item in req["payload"]["changes"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"action", "target_record_id", "payload"}
                or item["action"] not in ("replace", "append", "status_patch")
                or item["target_record_id"] in seen
            ):
                raise RecordError("INVALID_ARGUMENT")
            seen.add(item["target_record_id"])
            child = dict(req)
            child.update(item)
            validate_request(child, True)
        return
    if action == "freeze":
        if target is not None or req.get("payload") != {}:
            raise RecordError("INVALID_ARGUMENT")
        return
    if action == "resume":
        payload = req.get("payload")
        if (
            target is not None
            or records != []
            or expected["phase"] != "frozen"
            or not isinstance(payload, dict)
            or set(payload) != {"transition_id", "transition_intent_sha256"}
        ):
            raise RecordError("INVALID_ARGUMENT")
        ident(payload["transition_id"])
        if not isinstance(payload["transition_intent_sha256"], str) or not re.fullmatch(
            r"[a-f0-9]{64}", payload["transition_intent_sha256"]
        ):
            raise RecordError("INVALID_ARGUMENT")
        return
    ident(target)
    matches = [r for r in records if r["record_id"] == target]
    if len(matches) != 1 or not isinstance(req.get("payload"), dict):
        raise RecordError("FORBIDDEN")
    fields = matches[0]["fields"]
    payload = req["payload"]
    if action == "status_patch":
        patch = payload.get("fields")
        if (
            set(payload) != {"fields", "base_sha256"}
            or not isinstance(patch, dict)
            or not patch
            or any(k not in fields for k in patch)
        ):
            raise RecordError("FORBIDDEN")
    else:
        if (
            set(payload) != {"content", "base_sha256"}
            or "body" not in fields
            or not isinstance(payload["content"], str)
        ):
            raise RecordError("FORBIDDEN")
    digest = payload.get("base_sha256")
    if digest is not None and (
        not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        raise RecordError("INVALID_ARGUMENT")


def check_control(control, expected):
    if control["mode"] != expected["mode"]:
        raise RecordError("STALE_MODE")
    if control["epoch"] != expected["epoch"]:
        raise RecordError("STALE_EPOCH")
    if control["phase"] != "steady" or expected["phase"] != "steady":
        raise RecordError("PHASE_FROZEN")
    if control["version"] != expected["version"]:
        raise RecordError("REVISION_CONFLICT")


def status_patch(original, patch):
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        raise RecordError("INTEGRITY_FAILED") from None
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].startswith("# "):
        raise RecordError("INTEGRITY_FAILED")
    data = {}
    indices = {}
    for i, line in enumerate(lines[1:], 1):
        if line.startswith("## "):
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        match = re.match(r"^([a-z_]+):\s*(.*?)(\r?\n)?$", line)
        if not match:
            raise RecordError("INTEGRITY_FAILED")
        key = match.group(1)
        if key in data or key not in STATUS_REQUIRED | STATUS_OPTIONAL:
            raise RecordError("INTEGRITY_FAILED")
        data[key] = match.group(2).strip().strip('"')
        indices[key] = i
    if not STATUS_REQUIRED <= set(data):
        raise RecordError("INTEGRITY_FAILED")
    if not set(patch) <= STATUS_REQUIRED | STATUS_OPTIONAL or any(
        not isinstance(v, str) or not v or "\n" in v or "\r" in v
        for v in patch.values()
    ):
        raise RecordError("INVALID_ARGUMENT")
    if not set(patch) <= set(indices):
        raise RecordError("INTEGRITY_FAILED")
    data.update(patch)
    if any(not data.get(k) for k in STATUS_REQUIRED):
        raise RecordError("INTEGRITY_FAILED")
    if (
        data.get("schema") != "1"
        or data.get("state") not in ("planning", "active", "paused", "blocked", "done")
        or data.get("needs") not in ("none", "user", "external")
    ):
        raise RecordError("INTEGRITY_FAILED")
    if data["state"] == "blocked" and data["blocker"].strip().lower() in (
        "none",
        "無",
        "沒有",
        "",
    ):
        raise RecordError("INTEGRITY_FAILED")
    try:
        dt = datetime.fromisoformat(data["updated"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise RecordError("INTEGRITY_FAILED") from None
    if any(ord(c) < 32 or ord(c) == 127 for c in data["code_dir"]):
        raise RecordError("INTEGRITY_FAILED")
    machine = data.get("machine")
    if machine is not None and not re.fullmatch(r"unknown|[A-Z]", machine):
        raise RecordError("INTEGRITY_FAILED")
    branch = data.get("branch")
    if branch is not None and (
        not branch
        or any(c.isspace() for c in branch)
        or branch.startswith(("-", "/"))
        or branch.endswith("/")
        or ".." in branch
        or "@{" in branch
        or any(c in branch for c in "~^:?*[\\")
    ):
        raise RecordError("INTEGRITY_FAILED")
    code = data["code_dir"]
    if code != "no-code" and (
        code.startswith(("/", "\\", "~"))
        or "\\" in code
        or re.match(r"^[A-Za-z]:", code)
        or any(p in ("", ".", "..") for p in code.split("/"))
    ):
        raise RecordError("INTEGRITY_FAILED")
    claim = {"local_claim_by", "local_claim_session", "local_claim_updated"}
    if set(data) & claim and not claim <= set(data):
        raise RecordError("INTEGRITY_FAILED")
    if data.get("local_claim_by", "agent") not in ("agent", "human") or (
        "local_claim_session" in data and not data["local_claim_session"]
    ):
        raise RecordError("INTEGRITY_FAILED")
    if "local_claim_updated" in data:
        try:
            claim_time = datetime.fromisoformat(
                data["local_claim_updated"].replace("Z", "+00:00")
            )
            if claim_time.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise RecordError("INTEGRITY_FAILED") from None
    for key, value in patch.items():
        i = indices[key]
        newline = (
            "\r\n"
            if lines[i].endswith("\r\n")
            else "\n"
            if lines[i].endswith("\n")
            else ""
        )
        lines[i] = key + ": " + value + newline
    return "".join(lines).encode("utf-8")
