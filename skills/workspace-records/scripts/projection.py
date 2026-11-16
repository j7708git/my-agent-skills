"""Derived-only Sheet and current-detail source projection plans."""

import core
import google_docs
import manifest
import privacy
from core import RecordError, canonical, object_hash


def sheet_prepare(x):
    project = core.ident(x["project_id"])
    snap = google_docs.snapshot(
        x["document"], x["document_id"], x["head_tab_id"], x["summary_tab_id"], project
    )
    head = snap["head"]
    target = x["target"]
    if not isinstance(target, dict) or set(target) != {
        "spreadsheet_id",
        "sheet_id",
        "row_index",
    }:
        raise RecordError("INVALID_ARGUMENT")
    core.ident(target["spreadsheet_id"])
    if (
        type(target["sheet_id"]) is not int
        or target["sheet_id"] < 0
        or type(target["row_index"]) is not int
        or target["row_index"] < 0
    ):
        raise RecordError("INVALID_ARGUMENT")
    row = [
        project,
        head["summary"]["state"],
        head["summary"]["now"],
        head["summary"]["next"],
        head["summary"]["needs"],
        head["summary"]["blocker"],
        head["summary"]["updated"],
        str(head["control"]["epoch"]),
        str(head["version"]),
        head["root_manifest"]["sha256"],
        snap["revision"],
    ]
    request = {
        "updateCells": {
            "start": {
                "sheetId": target["sheet_id"],
                "rowIndex": target["row_index"],
                "columnIndex": 0,
            },
            "rows": [
                {
                    "values": [
                        {"userEnteredValue": {"stringValue": value}} for value in row
                    ]
                }
            ],
            "fields": "userEnteredValue",
        }
    }
    payload = {"spreadsheet_id": target["spreadsheet_id"], "requests": [request]}
    scan = privacy.check(payload, x["privacy_config"])
    return {
        "outcome": "prepared",
        "authoritative": False,
        "source": {
            "epoch": head["control"]["epoch"],
            "version": head["version"],
            "root_hash": head["root_manifest"]["sha256"],
            "docs_revision": snap["revision"],
        },
        "row": row,
        "payload": payload,
        "payload_hash": object_hash(payload),
        "scanner": scan,
        "requires_single_writer": True,
    }


def sheet_reconcile(x):
    # Compare only after a fresh, complete head snapshot and full observed row.
    try:
        latest = sheet_prepare(x)
        if x.get("observed_row") is None:
            return {"outcome": "manual_review", "freshness": "unknown"}
        if x["observed_row"] == latest["row"]:
            return {
                "outcome": "confirmed",
                "freshness": "current",
                "source": latest["source"],
            }
        if x.get("previous_write_settled") is not True:
            return {"outcome": "manual_review", "freshness": "stale"}
        return {
            "outcome": "reprepare_from_latest",
            "freshness": "stale",
            "requires_single_writer": True,
            "source": latest["source"],
        }
    except (RecordError, KeyError, TypeError):
        return {"outcome": "manual_review", "freshness": "unknown"}


def detail_expected(head, value, folder_id):
    core.ident(folder_id)
    manifest.validate_manifest(value, head["project_id"])
    raw = canonical(value)
    manifest.verify_bytes(raw, head["root_manifest"])
    if value["version"] != head["version"]:
        raise RecordError("INTEGRITY_FAILED")
    files = [
        {
            "record_id": r["record"]["record_id"],
            "id": r["object"]["id"],
            "sha256": r["object"]["sha256"],
        }
        for r in value["records"] if r["object"].get("encoding") != "inline-empty-v1"
    ]
    files += [
        {
            "record_id": a["attachment"]["attachment_id"],
            "id": a["object"]["id"],
            "sha256": a["object"]["sha256"],
        }
        for a in value["attachments"] if a["object"].get("encoding") != "inline-empty-v1"
    ]
    return {
        "authoritative": False,
        "folder_id": folder_id,
        "generation": head["root_manifest"]["sha256"],
        "expected": files,
        "inline_empty_records": [
            r["record"]["record_id"] for r in value["records"] if r["object"].get("encoding") == "inline-empty-v1"
        ] + [
            a["attachment"]["attachment_id"] for a in value["attachments"] if a["object"].get("encoding") == "inline-empty-v1"
        ],
        "visibility": "not_verified",
        "limitation": "single writer; staging/archive excluded; indexing delay unknown",
    }


def detail_reconcile(expected, observed, settled):
    if not isinstance(observed, list):
        return {"outcome": "manual_review", "freshness": "unknown"}
    expected_ids = {entry["id"] for entry in expected["expected"]}
    if set(observed) == expected_ids and len(observed) == len(expected_ids):
        return {
            "outcome": "confirmed",
            "freshness": "current_set_only",
            "search_index_freshness": "unknown",
        }
    if settled is not True:
        return {"outcome": "manual_review", "freshness": "stale"}
    return {
        "outcome": "reprepare_from_latest",
        "freshness": "stale",
        "requires_single_writer": True,
    }
