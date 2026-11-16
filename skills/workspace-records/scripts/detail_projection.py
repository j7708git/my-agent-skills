"""Derived current Markdown copies: durable single-writer intents, no network/auth.

Index is invalidated before detail writes and published current only after a full
verified generation. Drive writes have no CAS; caller must serialize this runtime.
"""
import argparse
import copy
import json
import pathlib
import sys
from datetime import datetime, timezone

import core
import connector_protocol as protocol
import git_store
import google_docs
import manifest
import routing
from core import RecordError, canonical, ident, object_hash, sha

CHUNK_BODY_BYTES = 128 * 1024


def location(registry, project):
    bound = registry.bind(ident(project))
    if bound["backend"] != "google_head" or bound["route_phase"] != "steady":
        raise RecordError("PHASE_FROZEN")
    root = git_store.safe_dir(bound["runtime_directory"])
    return bound, root


def storage(root, create=False):
    path = root / "detail-projection"
    if create and not path.exists():
        path.mkdir(mode=0o700)
    if path.exists():
        git_store.safe_dir(path)
    return path


def state_file(root):
    return storage(root) / "state.json"


def load(root, project):
    path = state_file(root)
    if not path.exists():
        return {"schema_version": 2, "project_id": project, "target": None,
                "files": {}, "index": None, "active": None, "operations": {}, "archived": {}}
    value = git_store.read_json(path)
    if value.get("project_id") != project or value.get("schema_version") not in (1, 2):
        raise RecordError("INTEGRITY_FAILED")
    return value


def source(x, registry, require_current_scope=True):
    bound, root = location(registry, x["project_id"])
    if x.get("actor_role") != "pm":
        raise RecordError("FORBIDDEN")
    snap = google_docs.snapshot(x["document"], bound["locator"]["document_id"],
                               x["head_tab_id"], x["summary_tab_id"], x["project_id"])
    head = snap["head"]
    if head["control"]["policy_version"] != bound["policy_version"]:
        raise RecordError("INTEGRITY_FAILED")
    graph, operations, objects, events, journal = protocol.read_graph(root, head, x["existing"])
    if protocol.status_summary(graph["records"], objects) != head["summary"]:
        raise RecordError("INTEGRITY_FAILED")
    ids = sorted([r["record"]["record_id"] for r in graph["records"]] +
                 [a["attachment"]["attachment_id"] for a in graph["attachments"]])
    scope = x.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"project_id", "record_ids", "folder_id", "archive_folder_id"} or scope["project_id"] != x["project_id"] or not isinstance(scope["record_ids"], list) or (require_current_scope and scope["record_ids"] != ids):
        raise RecordError("FORBIDDEN")
    ident(scope["folder_id"]); ident(scope["archive_folder_id"])
    if scope["archive_folder_id"] == scope["folder_id"]:
        raise RecordError("FORBIDDEN")
    generation = {"epoch": head["control"]["epoch"], "version": head["version"],
                  "root_hash": head["root_manifest"]["sha256"]}
    return bound, root, graph, objects, generation


def split_utf8(raw, maximum=CHUNK_BODY_BYTES):
    if not isinstance(raw, bytes) or maximum < 4:
        raise RecordError("INVALID_ARGUMENT")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RecordError("INTEGRITY_FAILED") from None
    if not raw:
        return [(0, 0, b"")]
    chunks = []; start = 0
    while start < len(raw):
        end = min(start + maximum, len(raw))
        if end < len(raw):
            boundary = raw.rfind(b"\n", start + maximum * 3 // 4, end)
            if boundary >= start:
                end = boundary + 1
            else:
                for _ in range(4):
                    try:
                        raw[start:end].decode("utf-8"); break
                    except UnicodeDecodeError:
                        end -= 1
            if raw[end - 1:end] == b"\r" and raw[end:end + 1] == b"\n":
                end -= 1
        if end <= start:
            raise RecordError("INTEGRITY_FAILED")
        chunk = raw[start:end]
        chunk.decode("utf-8")
        chunks.append((start, end, chunk)); start = end
    if b"".join(chunk for _, _, chunk in chunks) != raw:
        raise RecordError("INTEGRITY_FAILED")
    return chunks


def part_key(record_id, part_number):
    ident(record_id)
    if type(part_number) is not int or part_number < 1:
        raise RecordError("INVALID_ARGUMENT")
    return record_id + ":" + str(part_number)


def render_record(project, generation, entry, raw, updated_at, part=1, total=1, start=0, end=None, attachment=False):
    record = entry["attachment"] if attachment else entry["record"]
    rid = record["attachment_id"] if attachment else record["record_id"]
    path = record.get("source_relative_path") if attachment else record["source"]["relative_path"]
    metadata = {**generation, "canonical_sha256": entry["object"]["sha256"],
                "part_sha256": sha(raw), "part": part, "parts": total,
                "start_byte": start, "end_byte": len(raw) if end is None else end,
                "encoding": entry["object"].get("encoding", "raw"), "updated_at": updated_at}
    title = (path or rid) + ("（第 " + str(part) + "/" + str(total) + " 段）" if not attachment else "")
    lines = ["# " + title, "", "需要修改時，請透過 Codex 更新。",
             "專案：" + project, "來源版本：" + str(generation["version"]),
             "更新時間：" + updated_at,
             "<!-- workspace-records " + canonical(metadata).decode("utf-8") + " -->", ""]
    if attachment:
        lines.append("檔案類型：" + str(record.get("mime", "application/octet-stream")))
        if entry["object"].get("encoding") == "inline-empty-v1":
            lines.append("原附件為空檔，還原時保留。")
        else:
            lines.append("[開啟附件](https://drive.google.com/file/d/" + entry["object"]["id"] + "/view)")
        return ("\n".join(lines) + "\n").encode("utf-8")
    prefix = ("\n".join(lines) + "\n---\n\n<!-- workspace-records-body -->\n").encode("utf-8")
    return prefix + raw


def artifact(root, key, raw, config):
    scan = protocol.scan_bytes(raw, "text/markdown", config)
    path = storage(root, create=True) / ("detail-" + sha(key.encode("utf-8")) + "-" + sha(raw) + ".md")
    if path.exists():
        if protocol.file_bytes(root, str(path)) != raw:
            raise RecordError("INTEGRITY_FAILED")
    else:
        with open(path, "xb") as stream:
            stream.write(raw); stream.flush(); __import__("os").fsync(stream.fileno())
    return {"key": key, "path": str(path), "sha256": sha(raw), "size": len(raw),
            "status": "ready", "scanner": scan}


def config(bound):
    return {"gitleaks": "gitleaks", "policy": bound["privacy_policy"],
            "gitleaks_config": str(pathlib.Path(__file__).resolve().parents[3] / ".gitleaks.toml")}


def index_bytes(project, generation, current, files, document_id, updated_at):
    metadata = {**generation, "freshness": "current" if current else "stale", "updated_at": updated_at}
    text = ["# 專案詳情", "", "專案：" + project,
            "需要修改時，請透過 Codex 更新。",
            "[開啟專案摘要](https://docs.google.com/document/d/" + document_id + "/edit)",
            "閱讀狀態：" + ("已更新" if current else "更新中；連結內容可能混合舊內容"),
            "來源版本：" + str(generation["version"]), "更新時間：" + updated_at,
            "<!-- workspace-records " + canonical(metadata).decode("utf-8") + " -->", ""]
    if current:
        for rid in sorted(files, key=lambda key: (files[key].get("record_id", key), files[key].get("part", 1))):
            label = files[rid].get("label", rid).replace("[", "\\[").replace("]", "\\]")
            text.append("- [" + label + "](https://drive.google.com/file/d/" + files[rid]["file_id"] + "/view)")
    raw = ("\n".join(text) + "\n").encode("utf-8")
    if len(raw) > CHUNK_BODY_BYTES:
        raise RecordError("INTEGRITY_FAILED")
    return raw


def upgrade_legacy(root, state, x, bound, target):
    expected = x.get("legacy_state_sha256")
    if expected != object_hash(state):
        raise RecordError("REVISION_CONFLICT")
    active = state.get("active")
    if not active or active["phase"] != "complete" or any(i["status"] == "unknown" for i in active["items"]):
        raise RecordError("MANUAL_REVIEW")
    if active["binding"] != bound or state["target"] != {"folder_id": target["folder_id"]}:
        raise RecordError("FORBIDDEN")
    backup = storage(root) / ("legacy-" + expected + ".json")
    if backup.exists():
        if git_store.read_json(backup) != state:
            raise RecordError("INTEGRITY_FAILED")
    else:
        git_store.write_json(backup, state)
    result = copy.deepcopy(state)
    result["schema_version"] = 2; result["target"] = target
    result["files"] = {part_key(rid, 1): {**mapped, "record_id": rid, "part": 1, "parts": 1}
                       for rid, mapped in state["files"].items()}
    result["archived"] = {}; result["active"] = None
    result["upgrade_receipts"] = {x["operation_id"]: {"legacy_state_sha256": expected,
                                  "backup_file": backup.name, "scope_sha256": object_hash(x["scope"])}}
    return result


def tombstone_bytes(project, index_id, updated_at):
    return ("# 本段已移除\n\n專案：" + project + "\n更新時間：" + updated_at +
            "\n這段內容已不在目前版本，請從最新索引開啟。\n\n[開啟專案詳情](https://drive.google.com/file/d/" + index_id + "/view)\n").encode("utf-8")


def prepare(x, registry, upgrade=False):
    bound, root, graph, objects, generation = source(x, registry)
    operation = ident(x["operation_id"])
    with git_store.locked(root):
        state = load(root, x["project_id"])
        target = {"folder_id": x["scope"]["folder_id"], "archive_folder_id": x["scope"]["archive_folder_id"]}
        legacy_upgraded = False
        if state["schema_version"] != 2:
            if not upgrade:
                raise RecordError("SCHEMA_UNSUPPORTED")
            state = upgrade_legacy(root, state, x, bound, target)
            legacy_upgraded = True
        elif upgrade:
            receipt = state.get("upgrade_receipts", {}).get(operation)
            if receipt is None or receipt["legacy_state_sha256"] != x.get("legacy_state_sha256") or receipt["scope_sha256"] != object_hash(x["scope"]):
                raise RecordError("FORBIDDEN")
        if state["target"] is not None and state["target"] != target:
            raise RecordError("FORBIDDEN")
        intent_hash = object_hash({"generation": generation, "scope": x["scope"], "binding": bound})
        previous = state.get("operations", {}).get(operation)
        if previous is not None and previous["intent_hash"] != intent_hash:
            raise RecordError("ID_REUSE")
        if not {item["record_id"] for item in state["files"].values()} <= set(x["scope"]["record_ids"]):
            raise RecordError("MANUAL_REVIEW")
        active = state["active"]
        if previous is not None and (not active or active["operation_id"] != operation):
            return {"outcome": "no_write", "generation": previous["generation"], "freshness": "stale"}
        if active:
            if any(i["status"] == "unknown" for i in active["items"]):
                raise RecordError("MANUAL_REVIEW")
            if active["operation_id"] == operation:
                if active["generation"] != generation or active["binding"] != bound:
                    raise RecordError("ID_REUSE")
                return {"outcome": active["phase"], "generation": generation,
                        "freshness": "current" if active["phase"] == "complete" else "stale"}
        if not legacy_upgraded and state["index"] and state["index"].get("generation") == generation and state["index"].get("freshness") == "current":
            return {"outcome": "no_write", "generation": generation, "freshness": "current"}
        updated_at = datetime.now(timezone.utc).isoformat()
        items = [artifact(root, "stale-index", index_bytes(x["project_id"], generation, False, {}, bound["locator"]["document_id"], updated_at), config(bound))]
        expected_keys = []; coverage = {}
        for entry in graph["records"]:
            rid = entry["record"]["record_id"]; raw = objects[entry["object"]["id"]]
            chunks = split_utf8(raw); coverage[rid] = {"sha256": sha(raw), "size": len(raw), "parts": []}
            for ordinal, (start, end, chunk) in enumerate(chunks, 1):
                key = part_key(rid, ordinal); expected_keys.append(key)
                item = artifact(root, "record:" + key, render_record(x["project_id"], generation, entry, chunk, updated_at, ordinal, len(chunks), start, end), config(bound))
                item.update(record_id=rid, file_key=key, part=ordinal, parts=len(chunks),
                            label=entry["record"]["source"]["relative_path"] + "（第 " + str(ordinal) + "/" + str(len(chunks)) + " 段）")
                items.append(item); coverage[rid]["parts"].append({"part": ordinal, "start_byte": start, "end_byte": end, "sha256": sha(chunk)})
        for entry in graph["attachments"]:
            rid = entry["attachment"]["attachment_id"]; key = part_key(rid, 1); expected_keys.append(key)
            raw = objects[entry["object"]["id"]]
            item = artifact(root, "record:" + key, render_record(x["project_id"], generation, entry, raw, updated_at, attachment=True), config(bound))
            item.update(record_id=rid, file_key=key, part=1, parts=1, label=entry["attachment"].get("source_relative_path") or rid)
            items.append(item)
        for key in sorted(set(state["files"]) - set(expected_keys)):
            old = state["files"][key]
            if not state["index"]:
                raise RecordError("MANUAL_REVIEW")
            item = artifact(root, "retire:" + key, tombstone_bytes(x["project_id"], state["index"]["file_id"], updated_at), config(bound))
            item.update(kind="retire", record_id=old["record_id"], file_key=key,
                        part=old["part"], parts=old["parts"], label=old["label"],
                        previous_sha256=old["sha256"])
            items.append(item)
        state.setdefault("operations", {})[operation] = {"intent_hash": intent_hash, "generation": generation}
        state.update(target=target, active={"operation_id": operation, "generation": generation,
                     "binding": bound, "phase": "marking_stale", "items": items,
                     "record_ids": x["scope"]["record_ids"], "file_keys": expected_keys,
                     "scope": copy.deepcopy(x["scope"]), "updated_at": updated_at, "coverage": coverage})
        git_store.write_json(state_file(root), state)
        return {"outcome": "prepared", "freshness": "stale", "generation": generation,
                "parts": len(expected_keys), "coverage_sha256": object_hash(coverage),
                "requires_single_writer": True, "atomicity": "not_cross_resource"}


def begin(x, registry):
    bound, root, graph, objects, generation = source(x, registry, require_current_scope=False)
    with git_store.locked(root):
        state = load(root, x["project_id"]); active = state["active"]
        if not active or active["binding"] != bound:
            raise RecordError("REVISION_CONFLICT")
        if ident(x.get("operation_id")) != active["operation_id"] or x.get("scope") != active["scope"]:
            raise RecordError("FORBIDDEN")
        if any(i["status"] == "unknown" for i in active["items"]):
            raise RecordError("MANUAL_REVIEW")
        if active["generation"] != generation:
            raise RecordError("REVISION_CONFLICT")
        phase = active["phase"]
        if phase == "complete":
            return {"outcome": "no_write", "freshness": "current", "generation": generation}
        if phase == "stale":
            raise RecordError("MANUAL_REVIEW")
        if phase == "details" and all(i["status"] == "verified" for i in active["items"]):
            if set(state["files"]) != set(active["file_keys"]):
                raise RecordError("INTEGRITY_FAILED")
            files = {key: state["files"][key] for key in active["file_keys"]}
            active["items"].append(artifact(root, "current-index", index_bytes(x["project_id"], generation, True, files, bound["locator"]["document_id"], active["updated_at"]), config(bound)))
            active["phase"] = "publishing"; phase = "publishing"
        item = next((i for i in active["items"] if i["status"] == "ready"), None)
        if item is None:
            raise RecordError("MANUAL_REVIEW")
        existing = state["files"].get(item.get("file_key")) if "file_key" in item else state["index"]
        if existing:
            if not any(key in x for key in ("readback_remote_id", "observed_parent_id", "downloaded_file")):
                return {"outcome": "readback_required", "file_id": existing["file_id"],
                        "expected_sha256": existing["sha256"], "parent_folder_id": state["target"]["folder_id"],
                        "key": item["key"], "generation": generation, "freshness": "stale"}
            if x.get("readback_remote_id") != existing["file_id"] or x.get("observed_parent_id") != state["target"]["folder_id"]:
                raise RecordError("FORBIDDEN")
            observed = protocol.file_bytes(root, x["downloaded_file"])
            if sha(observed) != existing["sha256"]:
                raise RecordError("REVISION_CONFLICT")
        raw = protocol.file_bytes(root, item["path"])
        if len(raw) != item["size"] or sha(raw) != item["sha256"]:
            raise RecordError("INTEGRITY_FAILED")
        if protocol.scan_bytes(raw, "text/markdown", config(bound)) != item["scanner"]:
            raise RecordError("SCAN_INCOMPLETE")
        item["status"] = "unknown"
        item["action"] = "update_file" if existing else "upload_file"
        item["expected_id"] = existing["file_id"] if existing else None
        if item.get("kind") == "archive":
            if not existing:
                raise RecordError("INTEGRITY_FAILED")
            payload = {"fileId": existing["file_id"], "addParents": state["target"]["archive_folder_id"], "removeParents": state["target"]["folder_id"]}
        else:
            payload = {"file_uri": item["path"], "mime_type": "text/markdown"}
            if existing:
                payload["fileId"] = existing["file_id"]
            else:
                payload.update(file_name=((item["record_id"] + "-part-" + str(item["part"])) if "record_id" in item else "專案詳情索引") + ".md", parent_folder_id=state["target"]["folder_id"])
        git_store.write_json(state_file(root), state)
        return {"outcome": "send_once", "action": item["action"], "payload": payload,
                "key": item["key"], "sha256": item["sha256"], "generation": generation,
                "freshness": "stale", "requires_single_writer": True}


def accept(x, registry):
    bound, root, graph, objects, generation = source(x, registry, require_current_scope=False)
    with git_store.locked(root):
        state = load(root, x["project_id"]); active = state["active"]
        if not active or active["binding"] != bound:
            raise RecordError("REVISION_CONFLICT")
        if ident(x.get("operation_id")) != active["operation_id"] or x.get("scope") != active["scope"]:
            raise RecordError("FORBIDDEN")
        unknown = [i for i in active["items"] if i["status"] == "unknown"]
        if len(unknown) != 1:
            raise RecordError("MANUAL_REVIEW")
        item = unknown[0]; remote = ident(x["readback_remote_id"])
        expected_parent = state["target"]["archive_folder_id"] if item.get("kind") == "archive" else state["target"]["folder_id"]
        if remote == manifest.INLINE_EMPTY_ID or x.get("observed_parent_id") != expected_parent or (item["expected_id"] is not None and remote != item["expected_id"]):
            raise RecordError("FORBIDDEN")
        for key, mapped in state["files"].items():
            if mapped["file_id"] == remote and item.get("file_key") != key:
                raise RecordError("INTEGRITY_FAILED")
        if state["index"] and state["index"]["file_id"] == remote and "record_id" in item:
            raise RecordError("INTEGRITY_FAILED")
        raw = protocol.file_bytes(root, x["downloaded_file"])
        if len(raw) != item["size"] or sha(raw) != item["sha256"]:
            raise RecordError("INTEGRITY_FAILED")
        item["status"] = "verified"
        mapped = {"file_id": remote, "sha256": item["sha256"], "generation": active["generation"]}
        if item.get("kind") == "archive":
            prior = state["files"].pop(item["file_key"])
            state.setdefault("archived", {})[remote] = {**prior, "previous_sha256": item["previous_sha256"], "archive_folder_id": state["target"]["archive_folder_id"]}
        elif "record_id" in item:
            mapped.update(label=item["label"], record_id=item["record_id"], part=item["part"], parts=item["parts"])
            state["files"][item["file_key"]] = mapped
            if item.get("kind") == "retire":
                active["items"].append({**item, "key": "archive:" + item["file_key"], "kind": "archive", "status": "ready"})
        else:
            mapped["freshness"] = "current" if item["key"] == "current-index" and generation == active["generation"] else "stale"
            state["index"] = mapped
        if generation != active["generation"]:
            active["phase"] = "stale"
        elif item["key"] == "stale-index":
            active["phase"] = "details"
        elif item["key"] == "current-index":
            active["phase"] = "complete"
        git_store.write_json(state_file(root), state)
        return {"outcome": "verified", "phase": active["phase"], "freshness": "current" if active["phase"] == "complete" else "stale",
                "generation": active["generation"], "search_index_freshness": "unknown"}


def dispatch(command, x, registry):
    if command == "prepare": return prepare(x, registry)
    if command == "prepare-upgrade": return prepare(x, registry, upgrade=True)
    if command == "begin": return begin(x, registry)
    if command == "accept": return accept(x, registry)
    if command == "inspect":
        bound, root = location(registry, x["project_id"])
        if x.get("actor_role") != "pm": raise RecordError("FORBIDDEN")
        state = load(root, x["project_id"]); active = state["active"]
        ident(x.get("operation_id"))
        if active and (x["operation_id"] != active["operation_id"] or x.get("scope") != active["scope"]):
            raise RecordError("FORBIDDEN")
        return {"outcome": "state", "phase": active["phase"] if active else "empty",
                "generation": active["generation"] if active else None,
                "state_sha256": object_hash(state), "storage_schema_version": state["schema_version"],
                "legacy_upgrade_required": state["schema_version"] == 1,
                "freshness": "current" if active and active["phase"] == "complete" else "stale",
                "files": state["files"], "index": state["index"],
                "pending": [{k: item[k] for k in ("key", "status", "sha256", "size", "action", "expected_id") if k in item}
                            for item in (active["items"] if active else []) if item["status"] != "verified"],
                "requires_single_writer": True}
    raise RecordError("INVALID_ARGUMENT")


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=["prepare", "prepare-upgrade", "begin", "accept", "inspect"]); parser.add_argument("--input"); args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding="utf-8-sig") as stream: x = json.load(stream)
        else: x = json.load(sys.stdin)
        result = dispatch(args.command, x, routing.Registry(routing.discover_workspace()))
        print(json.dumps({"ok": True, "schema_version": 1, "data": result}, ensure_ascii=True)); return 0
    except (RecordError, OSError, KeyError, ValueError, TypeError) as error:
        code = error.code if isinstance(error, RecordError) else "INVALID_ARGUMENT"
        print(json.dumps({"ok": False, "schema_version": 1, "error": {"code": code, "retryable": False}}, ensure_ascii=True)); return 6 if code == "MANUAL_REVIEW" else 2


if __name__ == "__main__": sys.exit(main())
