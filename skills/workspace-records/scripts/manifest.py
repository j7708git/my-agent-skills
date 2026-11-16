"""Immutable manifest and paged complete events/journal validation."""

import json
import re
from core import RecordError, canonical, ident, sha
from git_store import relative_path

HEX = re.compile(r"^[0-9a-f]{64}$")
INLINE_EMPTY_ID = "inline-empty-v1"
MAX_HEAD_BYTES = 65536
MAX_MANIFEST_BYTES = 64 * 1024 * 1024


def reference(value):
    if not isinstance(value, dict) or set(value) not in ({"id", "sha256", "size"}, {"id", "sha256", "size", "encoding"}):
        raise RecordError("INTEGRITY_FAILED")
    ident(value["id"])
    if value.get("encoding") == "inline-empty-v1":
        if value["id"] != INLINE_EMPTY_ID or value["size"] != 0 or value["sha256"] != sha(b""):
            raise RecordError("INTEGRITY_FAILED")
    elif "encoding" in value or value["id"] == INLINE_EMPTY_ID:
        raise RecordError("INTEGRITY_FAILED")
    if not isinstance(value["sha256"], str) or not HEX.fullmatch(value["sha256"]):
        raise RecordError("INTEGRITY_FAILED")
    if type(value["size"]) is not int or value["size"] < 0:
        raise RecordError("INTEGRITY_FAILED")
    return value


def verify_bytes(raw, ref):
    reference(ref)
    if (
        not isinstance(raw, bytes)
        or len(raw) != ref["size"]
        or sha(raw) != ref["sha256"]
    ):
        raise RecordError("INTEGRITY_FAILED")
    return raw


def parse_json(raw, maximum=MAX_MANIFEST_BYTES):
    if len(raw) > maximum:
        raise RecordError("INTEGRITY_FAILED")
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or canonical(value) != raw:
            raise ValueError()
        return value
    except (UnicodeDecodeError, ValueError, TypeError):
        raise RecordError("INTEGRITY_FAILED") from None


def validate_manifest(value, project_id):
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "project_id",
        "version",
        "records",
        "attachments",
        "history_pages",
        "source_root",
    }:
        raise RecordError("INTEGRITY_FAILED")
    if (
        value["schema_version"] != 1
        or value["project_id"] != project_id
        or type(value["version"]) is not int
        or value["version"] < 0
    ):
        raise RecordError("INTEGRITY_FAILED")
    if not isinstance(value["source_root"], dict):
        raise RecordError("INTEGRITY_FAILED")
    if any(
        not isinstance(value[k], list)
        for k in ("records", "attachments", "history_pages")
    ):
        raise RecordError("INTEGRITY_FAILED")
    seen_ids = set()
    seen_paths = set()
    for entry in value["records"]:
        if not isinstance(entry, dict) or set(entry) != {"record", "object"}:
            raise RecordError("INTEGRITY_FAILED")
        record = entry["record"]
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
        ident(record["record_id"])
        if record["record_id"] in seen_ids:
            raise RecordError("INTEGRITY_FAILED")
        seen_ids.add(record["record_id"])
        source = record["source"]
        body = record["body"]
        if (
            not isinstance(source, dict)
            or not {"relative_path", "content_sha256", "original_time"} <= set(source)
            or body != {"format": "utf-8"}
            or not isinstance(record["metadata"], dict)
            or not isinstance(record["relations"], list)
        ):
            raise RecordError("INTEGRITY_FAILED")
        path = source["relative_path"]
        relative_path(path)
        if path.split("/")[-1] == ".gitkeep":
            raise RecordError("INTEGRITY_FAILED")
        if (
            path.casefold() in seen_paths
            or (path.casefold() == "status.md" and path != "status.md")
            or (path.casefold().startswith("log/") and not path.startswith("log/"))
        ):
            raise RecordError("INTEGRITY_FAILED")
        seen_paths.add(path.casefold())
        reference(entry["object"])
        if source["content_sha256"] != entry["object"]["sha256"]:
            raise RecordError("INTEGRITY_FAILED")
    for entry in value["attachments"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"attachment", "object"}
            or not isinstance(entry["attachment"], dict)
        ):
            raise RecordError("INTEGRITY_FAILED")
        a = entry["attachment"]
        ident(a.get("attachment_id"))
        reference(entry["object"])
        if (
            a["attachment_id"] in seen_ids
            or a.get("sha256") != entry["object"]["sha256"]
            or a.get("size") != entry["object"]["size"]
        ):
            raise RecordError("INTEGRITY_FAILED")
        seen_ids.add(a["attachment_id"])
        path = a.get("source_relative_path")
        if path is not None:
            relative_path(path)
            if path.split("/")[-1] == ".gitkeep" and a["size"] != 0:
                raise RecordError("INTEGRITY_FAILED")
            if path.casefold() in seen_paths:
                raise RecordError("INTEGRITY_FAILED")
            seen_paths.add(path.casefold())
    for page in value["history_pages"]:
        reference(page)
    if len({p["id"] for p in value["history_pages"]}) != len(value["history_pages"]):
        raise RecordError("INTEGRITY_FAILED")
    return value


def validate_history(manifest, page_bytes):
    if not isinstance(page_bytes, dict) or set(page_bytes) != {
        p["id"] for p in manifest["history_pages"]
    }:
        raise RecordError("CONTROL_UNAVAILABLE")
    operations = {}
    events = []
    journal = []
    for ref in manifest["history_pages"]:
        page = parse_json(verify_bytes(page_bytes[ref["id"]], ref))
        if (
            set(page)
            not in (
                {"schema_version", "events", "journal"},
                {"schema_version", "events", "journal", "previous_root"},
            )
            or page["schema_version"] != 1
            or not isinstance(page["events"], list)
            or not isinstance(page["journal"], list)
        ):
            raise RecordError("INTEGRITY_FAILED")
        if "previous_root" in page and page["previous_root"] is not None:
            reference(page["previous_root"])
        events.extend(page["events"])
        for entry in page["journal"]:
            if not isinstance(entry, dict) or not {
                "operation_id",
                "request_hash",
                "result",
            } <= set(entry):
                raise RecordError("INTEGRITY_FAILED")
            ident(entry["operation_id"])
            if (
                entry["operation_id"] in operations
                or not isinstance(entry["request_hash"], str)
                or not HEX.fullmatch(entry["request_hash"])
                or not isinstance(entry["result"], dict)
            ):
                raise RecordError("INTEGRITY_FAILED")
            operations[entry["operation_id"]] = entry
            journal.append(entry)
    return operations, events, journal
