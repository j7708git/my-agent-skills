"""Native Docs CAS head: exact managed lines, preserving other paragraphs."""

import json
import re
from core import RecordError, canonical, ident
from manifest import MAX_HEAD_BYTES, reference

HEAD = "[[WFG-P3-HEAD]]"
SUMMARY = "[[WFG-P3-SUMMARY-"


def head_line(head):
    raw = HEAD + canonical(head).decode("utf-8")
    if len(raw.encode("utf-8")) > MAX_HEAD_BYTES:
        raise RecordError("INTEGRITY_FAILED")
    return raw


SUMMARY_FIELDS = (
    ("state", "專案現況"),
    ("now", "現在"),
    ("next", "下一步"),
    ("needs", "待決"),
    ("blocker", "阻礙"),
    ("updated", "來源時間"),
    ("decisions", "未完成決策"),
)


def summary_lines(head):
    summary = head.get("summary")
    if not isinstance(summary, dict) or set(summary) != {
        key for key, _ in SUMMARY_FIELDS
    }:
        raise RecordError("INVALID_ARGUMENT")
    lines = []
    for key, label in SUMMARY_FIELDS:
        value = summary[key]
        if (
            not isinstance(value, str)
            or len(value) > 4096
            or any(mark in value for mark in ("\n", "\r", HEAD, SUMMARY))
        ):
            raise RecordError("INVALID_ARGUMENT")
        if key == "state":
            value = {
                "active": "進行中",
                "planning": "規劃中",
                "paused": "暫停",
                "blocked": "受阻",
                "done": "完成",
            }.get(value, value)
        if key in ("needs", "blocker") and value.lower() == "none":
            value = "無"
        lines.append(SUMMARY + key + "]]" + label + "：" + (value or "尚未確認"))
    lines.append(SUMMARY + "version]]來源版本：" + str(head["version"]))
    return lines


def validate_head(head, project_id):
    keys = {
        "schema_version",
        "project_id",
        "control",
        "version",
        "root_manifest",
        "last_operation",
        "summary",
    }
    if (
        not isinstance(head, dict)
        or set(head) != keys
        or head["schema_version"] != 1
        or head["project_id"] != project_id
    ):
        raise RecordError("INTEGRITY_FAILED")
    c = head["control"]
    if (
        not isinstance(c, dict)
        or set(c) != {"mode", "phase", "epoch", "policy_version"}
        or c["mode"] != "google"
        or c["phase"] not in ("steady", "frozen", "importing", "restoring")
        or type(c["epoch"]) is not int
        or c["epoch"] < 1
    ):
        raise RecordError("INTEGRITY_FAILED")
    if type(head["version"]) is not int or head["version"] < 0:
        raise RecordError("INTEGRITY_FAILED")
    reference(head["root_manifest"])
    if head["root_manifest"].get("encoding") is not None:
        raise RecordError("INTEGRITY_FAILED")
    op = head["last_operation"]
    if not isinstance(op, dict) or set(op) != {"operation_id", "request_hash"}:
        raise RecordError("INTEGRITY_FAILED")
    ident(op["operation_id"])
    if not isinstance(op["request_hash"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", op["request_hash"]
    ):
        raise RecordError("INTEGRITY_FAILED")
    head_line(head)
    summary_lines(head)
    return head


def snapshot(
    document, document_id, head_tab_id, summary_tab_id, project_id, bootstrap=False
):
    ident(document_id)
    ident(project_id)
    for tab_id in (head_tab_id, summary_tab_id):
        if not isinstance(tab_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}", tab_id
        ):
            raise RecordError("INVALID_ARGUMENT")
    if head_tab_id == summary_tab_id:
        raise RecordError("INVALID_ARGUMENT")
    if (
        not isinstance(document, dict)
        or document.get("documentId") != document_id
        or not isinstance(document.get("revisionId"), str)
        or not document["revisionId"]
    ):
        raise RecordError("INTEGRITY_FAILED")
    tabs = document.get("tabs")
    if not isinstance(tabs, list) or not tabs:
        raise RecordError("INTEGRITY_FAILED")
    selected = {}
    for tab in tabs:
        if not isinstance(tab, dict) or tab.get("childTabs"):
            raise RecordError("INTEGRITY_FAILED")
        if "documentTab" in tab and "body" not in tab:
            tid = tab.get("tabProperties", {}).get("tabId")
            value = tab["documentTab"]
        elif "body" in tab and "documentTab" not in tab:
            tid = tab.get("tabId")
            value = tab
        else:
            raise RecordError("INTEGRITY_FAILED")
        if tid not in (head_tab_id, summary_tab_id):
            continue
        if tid in selected or not isinstance(value, dict):
            raise RecordError("INTEGRITY_FAILED")
        body = value.get("body", {}).get("content")
        if not isinstance(body, list) or not body or "sectionBreak" not in body[0]:
            raise RecordError("INTEGRITY_FAILED")
        lines = []
        for item in body[1:]:
            if not isinstance(item, dict) or "paragraph" not in item:
                raise RecordError("INTEGRITY_FAILED")
            pieces = []
            for element in item["paragraph"].get("elements", []):
                run = element.get("textRun") if isinstance(element, dict) else None
                if not isinstance(run, dict) or not isinstance(run.get("content"), str):
                    raise RecordError("INTEGRITY_FAILED")
                pieces.append(run["content"])
            text = "".join(pieces)
            if not text.endswith("\n") or "\n" in text[:-1]:
                raise RecordError("INTEGRITY_FAILED")
            lines.append(text[:-1])
        if not lines:
            raise RecordError("INTEGRITY_FAILED")
        selected[tid] = lines
    if set(selected) != {head_tab_id, summary_tab_id}:
        raise RecordError("INTEGRITY_FAILED")
    heads = [line for line in selected[head_tab_id] if HEAD in line]
    summaries = [line for line in selected[summary_tab_id] if SUMMARY in line]
    if bootstrap:
        if heads or summaries:
            raise RecordError("ID_REUSE")
        return {
            "document_id": document_id,
            "head_tab_id": head_tab_id,
            "summary_tab_id": summary_tab_id,
            "revision": document["revisionId"],
            "head": None,
            "lines": selected,
        }
    if (
        len(heads) != 1
        or not heads[0].startswith(HEAD)
        or len(summaries) != len(SUMMARY_FIELDS) + 1
        or not summaries[0].startswith(SUMMARY)
    ):
        raise RecordError("INTEGRITY_FAILED")
    try:
        head = json.loads(heads[0][len(HEAD) :])
        validate_head(head, project_id)
    except (ValueError, TypeError, KeyError):
        raise RecordError("INTEGRITY_FAILED") from None
    if head_line(head) != heads[0] or summary_lines(head) != summaries:
        raise RecordError("INTEGRITY_FAILED")
    return {
        "document_id": document_id,
        "head_tab_id": head_tab_id,
        "summary_tab_id": summary_tab_id,
        "revision": document["revisionId"],
        "head": head,
        "lines": selected,
    }


def batch(old, new_head):
    new_head_line = head_line(new_head)
    new_summary = summary_lines(new_head)
    if old["head"] is None:
        requests = [
            {
                "insertText": {
                    "location": {"index": 1, "tabId": old["head_tab_id"]},
                    "text": new_head_line + "\n",
                }
            },
            {
                "insertText": {
                    "location": {"index": 1, "tabId": old["summary_tab_id"]},
                    "text": "\n".join(new_summary) + "\n",
                }
            },
        ]
    else:
        requests = []
        simulated = {tab: "\n".join(lines) for tab, lines in old["lines"].items()}
        changes = [(old["head_tab_id"], head_line(old["head"]), new_head_line)]
        changes.extend(
            (old["summary_tab_id"], before, after)
            for before, after in zip(summary_lines(old["head"]), new_summary)
        )
        for tab, before, after in changes:
            if before == after:
                continue
            if simulated[tab].count(before) != 1:
                raise RecordError("INTEGRITY_FAILED")
            simulated[tab] = simulated[tab].replace(before, after, 1)
            requests.append(
                {
                    "replaceAllText": {
                        "containsText": {"text": before, "matchCase": True},
                        "replaceText": after,
                        "tabsCriteria": {"tabIds": [tab]},
                    }
                }
            )
    return {
        "document_id": old["document_id"],
        "requests": requests,
        "write_control": {"requiredRevisionId": old["revision"]},
    }
