"""Durable transition intents; external fencing and storage writes remain caller actions."""

import argparse
import json
import os
import pathlib
import re
import sys
import core
import git_store
import snapshot
from core import RecordError, ident, object_hash, sha

STEPS = ("freeze", "drain", "snapshot", "target_frozen", "route_frozen", "resume")
HEX = re.compile(r"^[a-f0-9]{64}$")


def directory(root, tid, create=False):
    ident(tid)
    parent = root / "transitions"
    if not parent.exists():
        if not create:
            raise RecordError("NOT_FOUND")
        parent.mkdir(mode=0o700)
    git_store.safe_dir(parent)
    d = parent / sha(tid.encode("utf-8"))
    if create:
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            raise RecordError("ID_REUSE") from None
    return git_store.safe_dir(d)


def digest(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise RecordError("INTEGRITY_FAILED")
    return value


def begin(x):
    root = git_store.safe_dir(x["private_dir"])
    tid = ident(x["transition_id"])
    project = ident(x["project_id"])
    src = x["source"]
    target = x["target"]
    writers = x["writers"]
    if (
        x.get("actor_role") != "pm"
        or not isinstance(src, dict)
        or set(src) != {"mode", "phase", "epoch", "version", "resource_id"}
        or src["mode"] not in ("git", "google")
        or src["phase"] != "steady"
        or type(src["epoch"]) is not int
        or src["epoch"] < 1
        or type(src["version"]) is not int
        or src["version"] < 0
    ):
        raise RecordError("FORBIDDEN")
    ident(src["resource_id"])
    if (
        not isinstance(target, dict)
        or set(target) != {"mode", "resource_id", "manifest_sha256"}
        or target["mode"] not in ("git", "google")
        or target["mode"] == src["mode"]
    ):
        raise RecordError("INVALID_ARGUMENT")
    ident(target["resource_id"])
    if target["manifest_sha256"] is not None:
        digest(target["manifest_sha256"])
    if (
        not isinstance(writers, list)
        or not writers
        or len(set(writers)) != len(writers)
    ):
        raise RecordError("INVALID_ARGUMENT")
    for writer in writers:
        ident(writer)
    d = directory(root, tid, True)
    state = {
        "schema_version": 1,
        "transition_id": tid,
        "project_id": project,
        "source": src,
        "target": target,
        "writers": writers,
        "next_index": 0,
        "pending": None,
        "receipts": [],
        "phase": "in_progress",
    }
    git_store.write_json(d / "transition.json", state)
    return {
        "outcome": "prepared",
        "next_step": "freeze",
        "physical_fence_independently_verified": False,
    }


def load(root, tid):
    d = directory(root, tid)
    state = git_store.read_json(d / "transition.json")
    if state.get("transition_id") != tid:
        raise RecordError("INTEGRITY_FAILED")
    return d, state


def issue(x):
    root = git_store.safe_dir(x["private_dir"])
    d, state = load(root, x["transition_id"])
    with git_store.locked(d):
        state = git_store.read_json(d / "transition.json")
        if state["pending"] is not None:
            raise RecordError("OUTCOME_UNKNOWN")
        index = state["next_index"]
        if index >= len(STEPS):
            return {"outcome": "complete", "receipts": state["receipts"]}
        step = STEPS[index]
        op = ident(x["operation_id"])
        if any(r["operation_id"] == op for r in state["receipts"]):
            raise RecordError("ID_REUSE")
        intent = {
            "transition_id": state["transition_id"],
            "project_id": state["project_id"],
            "operation_id": op,
            "step": step,
            "source": state["source"],
            "target": state["target"],
            "frozen_epoch": state["source"]["epoch"] + 1,
            "writers": state["writers"],
        }
        state["pending"] = intent
        git_store.write_json(d / "transition.json", state)
        return {
            "outcome": "issue_once",
            "intent": intent,
            "intent_hash": object_hash(intent),
            "external_action": "caller_managed",
            "physical_fence_independently_verified": False,
        }


def observe(x):
    root = git_store.safe_dir(x["private_dir"])
    d, state = load(root, x["transition_id"])
    with git_store.locked(d):
        state = git_store.read_json(d / "transition.json")
        intent = state["pending"]
        if intent is None or object_hash(intent) != x.get("intent_hash"):
            raise RecordError("OUTCOME_UNKNOWN")
        step = intent["step"]
        observation = x["observation"]
        epoch = intent["frozen_epoch"]
        if not isinstance(observation, dict):
            raise RecordError("INVALID_ARGUMENT")
        if step == "freeze":
            expected = {
                "resource_id": intent["source"]["resource_id"],
                "mode": intent["source"]["mode"],
                "phase": "frozen",
                "epoch": epoch,
                "version": intent["source"]["version"] + 1,
            }
            if observation != expected:
                raise RecordError("REVISION_CONFLICT")
        elif step == "drain":
            if (
                set(observation) != {"inflight", "writer_attestations"}
                or observation["inflight"] != []
                or not isinstance(observation["writer_attestations"], dict)
                or set(observation["writer_attestations"]) != set(state["writers"])
            ):
                raise RecordError("MANUAL_REVIEW")
            for evidence in observation["writer_attestations"].values():
                if (
                    not isinstance(evidence, dict)
                    or set(evidence) != {"state", "evidence_sha256"}
                    or evidence["state"] != "caller_verified_stopped"
                ):
                    raise RecordError("MANUAL_REVIEW")
                digest(evidence["evidence_sha256"])
        elif step == "snapshot":
            if set(observation) != {"snapshot_id", "receipt_sha256"}:
                raise RecordError("INVALID_ARGUMENT")
            sd, ss = snapshot.load(root, observation["snapshot_id"])
            if (
                ss["project_id"] != state["project_id"]
                or object_hash(ss["receipt"]) != observation["receipt_sha256"]
            ):
                raise RecordError("INTEGRITY_FAILED")
            if intent["source"]["mode"] == "google":
                if ss["phase"] != "complete":
                    raise RecordError("INTEGRITY_FAILED")
                source = ss["source"]
                if (
                    source["document_id"] != intent["source"]["resource_id"]
                    or source["head"]["control"]["mode"] != "google"
                    or source["head"]["control"]["phase"] != "frozen"
                    or source["head"]["control"]["epoch"] != epoch
                    or source["head"]["version"] != intent["source"]["version"] + 1
                ):
                    raise RecordError("CONTROL_UNAVAILABLE")
                snapshot.verify_graph(sd, ss)
            else:
                gitstate, recordset = snapshot.read_git(
                    root, observation["snapshot_id"]
                )
                control = recordset["control"]
                if (
                    gitstate["receipt"]["project_identity"]
                    != intent["source"]["resource_id"]
                    or control["epoch"] != epoch
                    or control["version"] != intent["source"]["version"] + 1
                ):
                    raise RecordError("CONTROL_UNAVAILABLE")
        elif step == "target_frozen":
            target_hash = digest(observation.get("manifest_sha256"))
            if (
                intent["target"]["manifest_sha256"] is not None
                and intent["target"]["manifest_sha256"] != target_hash
            ):
                raise RecordError("REVISION_CONFLICT")
            if intent["target"]["mode"] == "google":
                import connector_protocol, google_docs, base64

                expected_keys = {
                    "resource_id",
                    "mode",
                    "phase",
                    "epoch",
                    "version",
                    "manifest_sha256",
                    "snapshot_receipt_sha256",
                    "document",
                    "head_tab_id",
                    "summary_tab_id",
                    "existing",
                }
                if set(observation) != expected_keys:
                    raise RecordError("INVALID_ARGUMENT")
                snap = google_docs.snapshot(
                    observation["document"],
                    intent["target"]["resource_id"],
                    observation["head_tab_id"],
                    observation["summary_tab_id"],
                    state["project_id"],
                )
                head = snap["head"]
                snapid = state["receipts"][-1]["observation"]["snapshot_id"]
                gitstate, recordset = snapshot.read_git(root, snapid)
                if (
                    observation["resource_id"] != intent["target"]["resource_id"]
                    or observation["mode"] != "google"
                    or observation["phase"] != "frozen"
                    or observation["epoch"] != epoch
                    or observation["version"] != intent["source"]["version"] + 2
                    or observation["manifest_sha256"] != target_hash
                    or observation["snapshot_receipt_sha256"]
                    != state["receipts"][-1]["observation"]["receipt_sha256"]
                    or head["control"]["phase"] != "frozen"
                    or head["control"]["epoch"] != epoch
                    or head["version"] != observation["version"]
                    or head["root_manifest"]["sha256"] != target_hash
                ):
                    raise RecordError("REVISION_CONFLICT")
                graph, operations, objects, events, journal = (
                    connector_protocol.read_graph(root, head, observation["existing"])
                )
                if graph["source_root"] != {
                    k: v
                    for k, v in recordset.items()
                    if k not in ("records", "events", "journal", "attachments")
                }:
                    raise RecordError("INTEGRITY_FAILED")
                for record in recordset["records"]:
                    found = [
                        entry
                        for entry in graph["records"]
                        if entry["record"]["record_id"] == record["record_id"]
                    ]
                    if len(found) != 1:
                        raise RecordError("INTEGRITY_FAILED")
                    saved = dict(record)
                    body = saved["body"]
                    saved["body"] = {"format": "utf-8"}
                    if found[0]["record"] != saved or objects[
                        found[0]["object"]["id"]
                    ] != body["content"].encode("utf-8"):
                        raise RecordError("INTEGRITY_FAILED")
                for attachment in recordset["attachments"]:
                    found = [
                        entry
                        for entry in graph["attachments"]
                        if entry["attachment"]["attachment_id"]
                        == attachment["attachment_id"]
                    ]
                    saved = {
                        k: v for k, v in attachment.items() if k != "raw_bytes_b64"
                    }
                    if (
                        len(found) != 1
                        or found[0]["attachment"] != saved
                        or objects[found[0]["object"]["id"]]
                        != base64.b64decode(attachment["raw_bytes_b64"], validate=True)
                    ):
                        raise RecordError("INTEGRITY_FAILED")
                if len(graph["records"]) != len(recordset["records"]) or len(
                    graph["attachments"]
                ) != len(recordset["attachments"]):
                    raise RecordError("INTEGRITY_FAILED")
                if (
                    events[: len(recordset["events"])] != recordset["events"]
                    or journal[: len(recordset["journal"])] != recordset["journal"]
                ):
                    raise RecordError("INTEGRITY_FAILED")
            else:
                expected_keys = {
                    "resource_id",
                    "mode",
                    "phase",
                    "epoch",
                    "version",
                    "manifest_sha256",
                    "snapshot_receipt_sha256",
                    "plan_hash",
                    "approved_root",
                    "target_dir",
                    "runtime_dir",
                    "runtime_plan_hash",
                }
                promoted = {"promotion_operation", "promotion_plan_hash"} <= set(
                    observation
                )
                if set(observation) != expected_keys | (
                    {"promotion_operation", "promotion_plan_hash"}
                    if promoted
                    else set()
                ):
                    raise RecordError("INVALID_ARGUMENT")
                if (
                    observation["resource_id"] != intent["target"]["resource_id"]
                    or observation["mode"] != "git"
                    or observation["phase"] != "frozen"
                    or observation["epoch"] != epoch
                    or observation["version"] != intent["source"]["version"] + 1
                    or observation["manifest_sha256"] != target_hash
                    or observation["snapshot_receipt_sha256"]
                    != state["receipts"][-1]["observation"]["receipt_sha256"]
                ):
                    raise RecordError("REVISION_CONFLICT")
                import restore

                snapid = state["receipts"][-1]["observation"]["snapshot_id"]
                sd, ss, items = restore.material(root, snapid)
                target = restore.target_dir(
                    observation["approved_root"], observation["target_dir"]
                )
                planhash = digest(observation["plan_hash"])
                plan = git_store.read_json(sd / ("preview-" + planhash + ".json"))
                if (
                    object_hash(plan) != planhash
                    or plan["target_dir"] != str(target)
                    or plan["snapshot_id"] != snapid
                ):
                    raise RecordError("INTEGRITY_FAILED")
                journal = sd / (
                    "restore-"
                    + object_hash({"target": str(target), "snapshot": snapid})
                    + ".json"
                )
                progress = git_store.read_json(journal)
                if (
                    progress.get("phase") != "complete"
                    or progress.get("plan_hash") != planhash
                ):
                    raise RecordError("CONTROL_UNAVAILABLE")
                exact_manifest = [
                    {"path": path, "sha256": ref["sha256"], "size": ref["size"]}
                    for path, ref in sorted(items)
                ]
                if object_hash(exact_manifest) != target_hash:
                    raise RecordError("INTEGRITY_FAILED")
                for path, ref in items:
                    raw = restore.content(restore.destination(target, path))
                    if (
                        raw is None
                        or len(raw) != ref["size"]
                        or sha(raw) != ref["sha256"]
                    ):
                        raise RecordError("INTEGRITY_FAILED")
                runtime = restore.target_dir(
                    observation["approved_root"], observation["runtime_dir"]
                )
                if (
                    runtime == target
                    or runtime in target.parents
                    or target in runtime.parents
                ):
                    raise RecordError("FORBIDDEN")
                runtimehash = digest(observation["runtime_plan_hash"])
                runtimeplan = git_store.read_json(
                    sd / ("runtime-preview-" + runtimehash + ".json")
                )
                if (
                    object_hash(runtimeplan) != runtimehash
                    or runtimeplan["snapshot_id"] != snapid
                ):
                    raise RecordError("INTEGRITY_FAILED")
                if promoted:
                    import promotion

                    promotion_dir = promotion.session(
                        root, observation["promotion_operation"]
                    )
                    promotion_state = git_store.read_json(
                        promotion_dir / "promotion.json"
                    )
                    pplan = promotion_state["plan"]
                    if (
                        promotion_state["phase"] != "complete"
                        or promotion_state["plan_hash"]
                        != observation["promotion_plan_hash"]
                        or object_hash(pplan) != promotion_state["plan_hash"]
                        or pplan["source_plan_hash"] != runtimehash
                        or pplan["snapshot_id"] != snapid
                        or pplan["binding"]["runtime_directory"] != str(runtime)
                        or pplan["binding"]["project_directory"] != str(target)
                    ):
                        raise RecordError("INTEGRITY_FAILED")
                    actual = promotion.canonical_files(runtime)
                    if set(actual) != {item["path"] for item in pplan["files"]} or any(
                        sha(actual[item["path"]]) != item["after_sha256"]
                        for item in pplan["files"]
                    ):
                        raise RecordError("INTEGRITY_FAILED")
                elif runtimeplan["runtime_dir"] != str(runtime):
                    raise RecordError("INTEGRITY_FAILED")
                if (
                    git_store.get_control(runtime) != runtimeplan["control"]
                    or git_store.get_control(runtime)["phase"] != "frozen"
                    or git_store.get_control(runtime)["version"]
                    != observation["version"]
                ):
                    raise RecordError("INTEGRITY_FAILED")
                expected = {
                    entry["operation_id"]: entry for entry in runtimeplan["journal"]
                }
                observed = git_store.journal_entries(runtime)
                if {entry["operation_id"]: entry for entry in observed} != expected:
                    raise RecordError("INTEGRITY_FAILED")
            state["target"]["manifest_sha256"] = target_hash
        elif step == "route_frozen":
            expected = {
                "resource_id": intent["target"]["resource_id"],
                "mode": intent["target"]["mode"],
                "route_phase": "frozen",
                "registry_sha256": observation.get("registry_sha256"),
            }
            digest(observation.get("registry_sha256"))
            if observation != expected:
                raise RecordError("REVISION_CONFLICT")
        else:
            target_receipt = next(
                item for item in state["receipts"] if item["step"] == "target_frozen"
            )["observation"]
            expected_version = target_receipt["version"] + 1
            req = {
                "schema_version": 1,
                "request_id": intent["operation_id"],
                "project_id": state["project_id"],
                "actor_role": "pm",
                "scope": {
                    "project_id": state["project_id"],
                    "actions": ["resume"],
                    "records": [],
                },
                "operation_id": intent["operation_id"],
                "action": "resume",
                "target_record_id": None,
                "payload": {
                    "transition_id": state["transition_id"],
                    "transition_intent_sha256": object_hash(intent),
                },
                "expected": {
                    "mode": intent["target"]["mode"],
                    "phase": "frozen",
                    "epoch": epoch,
                    "version": target_receipt["version"],
                },
            }
            expected_hash = core.request_hash(req)
            if intent["target"]["mode"] == "git":
                expected = {
                    "resource_id": intent["target"]["resource_id"],
                    "mode": "git",
                    "phase": "steady",
                    "epoch": epoch,
                    "version": expected_version,
                }
                if observation != expected:
                    raise RecordError("REVISION_CONFLICT")
                runtime = git_store.safe_dir(target_receipt["runtime_dir"])
                control = git_store.get_control(runtime)
                if (
                    control["mode"] != "git"
                    or control["phase"] != "steady"
                    or control["epoch"] != epoch
                    or control["version"] != expected_version
                ):
                    raise RecordError("CONTROL_UNAVAILABLE")
                entry = git_store.read_operation(runtime, intent["operation_id"])
                if (
                    entry.get("state") != "complete"
                    or entry.get("request_hash") != expected_hash
                    or entry.get("event", {}).get("operation_id")
                    != intent["operation_id"]
                    or entry.get("event", {}).get("request_hash") != expected_hash
                    or entry.get("event", {}).get("action") != "resume"
                ):
                    raise RecordError("CONTROL_UNAVAILABLE")
            else:
                import connector_protocol, google_docs

                expected_keys = {
                    "resource_id",
                    "mode",
                    "phase",
                    "epoch",
                    "version",
                    "document",
                    "head_tab_id",
                    "summary_tab_id",
                    "existing",
                }
                if (
                    set(observation) != expected_keys
                    or observation["resource_id"] != intent["target"]["resource_id"]
                    or observation["mode"] != "google"
                    or observation["phase"] != "steady"
                    or observation["epoch"] != epoch
                    or observation["version"] != expected_version
                ):
                    raise RecordError("REVISION_CONFLICT")
                snap = google_docs.snapshot(
                    observation["document"],
                    intent["target"]["resource_id"],
                    observation["head_tab_id"],
                    observation["summary_tab_id"],
                    state["project_id"],
                )
                head = snap["head"]
                if (
                    head["control"]["phase"] != "steady"
                    or head["control"]["epoch"] != epoch
                    or head["version"] != expected_version
                    or head["last_operation"]["operation_id"] != intent["operation_id"]
                    or head["last_operation"]["request_hash"] != expected_hash
                ):
                    raise RecordError("CONTROL_UNAVAILABLE")
                _, operations, _, _, _ = connector_protocol.read_graph(
                    root, head, observation["existing"]
                )
                if (
                    intent["operation_id"] not in operations
                    or operations[intent["operation_id"]].get("request_hash")
                    != expected_hash
                    or operations[intent["operation_id"]]
                    .get("event", {})
                    .get("operation_id")
                    != intent["operation_id"]
                    or operations[intent["operation_id"]]
                    .get("event", {})
                    .get("request_hash")
                    != expected_hash
                    or operations[intent["operation_id"]].get("event", {}).get("action")
                    != "resume"
                ):
                    raise RecordError("CONTROL_UNAVAILABLE")
        receipt = {
            "operation_id": intent["operation_id"],
            "step": step,
            "intent_hash": object_hash(intent),
            "observation": observation,
            "evidence_origin": "caller_supplied",
            "physical_fence_independently_verified": False,
        }
        state["receipts"].append(receipt)
        state["pending"] = None
        state["next_index"] += 1
        if state["next_index"] == len(STEPS):
            state["phase"] = "complete"
        git_store.write_json(d / "transition.json", state)
        return {
            "outcome": "observed",
            "step": step,
            "next_step": STEPS[state["next_index"]]
            if state["next_index"] < len(STEPS)
            else None,
            "physical_fence_independently_verified": False,
        }


def resume_google(x, registry):
    """Run one Google resume stage under the exact frozen transition binding."""
    import connector_protocol
    import google_docs

    root = git_store.safe_dir(x["private_dir"])
    directory, state = load(root, x["transition_id"])
    registry.require_no_activation(state["project_id"])
    with git_store.locked(directory):
        state = git_store.read_json(directory / "transition.json")
        intent = state.get("pending")
        if (
            not intent
            or intent["step"] != "resume"
            or object_hash(intent) != x.get("intent_hash")
        ):
            raise RecordError("OUTCOME_UNKNOWN")
        if intent["target"]["mode"] != "google":
            raise RecordError("STALE_MODE")
        target = next(
            item for item in state["receipts"] if item["step"] == "target_frozen"
        )["observation"]
        route = next(
            item for item in state["receipts"] if item["step"] == "route_frozen"
        )["observation"]
        binding = registry.bind(state["project_id"])
        if binding["backend"] != "google_head" or binding["route_phase"] != "frozen":
            raise RecordError("PHASE_FROZEN")
        if (
            binding["registry_sha256"] != route["registry_sha256"]
            or binding["locator"]["document_id"] != intent["target"]["resource_id"]
        ):
            raise RecordError("REVISION_CONFLICT")
        private = git_store.safe_dir(binding["runtime_directory"])
        command = x["stage"]
        if command not in (
            "prepare",
            "inspect",
            "begin-upload",
            "accept-upload",
            "prepare-cas",
            "begin-cas",
            "reconcile-cas",
        ):
            raise RecordError("INVALID_ARGUMENT")
        key = {
            "private_dir": str(private),
            "project_id": state["project_id"],
            "operation_id": intent["operation_id"],
        }
        if command == "prepare":
            snap = google_docs.snapshot(
                x["document"],
                binding["locator"]["document_id"],
                x["head_tab_id"],
                x["summary_tab_id"],
                state["project_id"],
            )
            head = snap["head"]
            if (
                head["control"]["phase"] != "frozen"
                or head["control"]["epoch"] != intent["frozen_epoch"]
                or head["version"] != target["version"]
                or head["root_manifest"]["sha256"]
                != intent["target"]["manifest_sha256"]
            ):
                raise RecordError("REVISION_CONFLICT")
            req = {
                "schema_version": 1,
                "request_id": intent["operation_id"],
                "project_id": state["project_id"],
                "actor_role": "pm",
                "scope": {
                    "project_id": state["project_id"],
                    "actions": ["resume"],
                    "records": [],
                },
                "operation_id": intent["operation_id"],
                "action": "resume",
                "target_record_id": None,
                "payload": {
                    "transition_id": state["transition_id"],
                    "transition_intent_sha256": object_hash(intent),
                },
                "expected": {
                    "mode": "google",
                    "phase": "frozen",
                    "epoch": head["control"]["epoch"],
                    "version": head["version"],
                },
            }
            privacy_config = {
                "gitleaks": "gitleaks",
                "policy": binding["privacy_policy"],
                "gitleaks_config": str(
                    pathlib.Path(__file__).resolve().parents[3] / ".gitleaks.toml"
                ),
            }
            params = {
                **key,
                "request": req,
                "document": x["document"],
                "document_id": snap["document_id"],
                "head_tab_id": x["head_tab_id"],
                "summary_tab_id": x["summary_tab_id"],
                "existing": x["existing"],
                "privacy_config": privacy_config,
                "_route_binding": binding,
            }
            result = connector_protocol.prepare(params)
        else:
            _, session = connector_protocol.load_session(private, key)
            if session.get("route_binding") != binding:
                raise RecordError("REVISION_CONFLICT")
            if command not in ("inspect", "reconcile-cas"):
                snap = google_docs.snapshot(
                    x["document"],
                    binding["locator"]["document_id"],
                    session["snapshot"]["head_tab_id"],
                    session["snapshot"]["summary_tab_id"],
                    state["project_id"],
                )
                if (
                    snap["revision"] != session["snapshot"]["revision"]
                    or snap["head"] != session["snapshot"]["head"]
                ):
                    raise RecordError("REVISION_CONFLICT")
            params = {**x, **key}
            result = connector_protocol.dispatch(command, params)
        return {
            **result,
            "route_phase": "frozen",
            "physical_fence_independently_verified": False,
        }


def resume_git(x, registry):
    """Resume the exact frozen Git binding; route remains frozen until separate publication."""
    from datetime import datetime, timezone

    root = git_store.safe_dir(x["private_dir"])
    directory, state = load(root, x["transition_id"])
    registry.require_no_activation(state["project_id"])
    with git_store.locked(directory):
        state = git_store.read_json(directory / "transition.json")
        intent = state.get("pending")
        if (
            not intent
            or intent["step"] != "resume"
            or object_hash(intent) != x.get("intent_hash")
        ):
            raise RecordError("OUTCOME_UNKNOWN")
        if intent["target"]["mode"] != "git":
            raise RecordError("STALE_MODE")
        target = next(
            item for item in state["receipts"] if item["step"] == "target_frozen"
        )["observation"]
        binding = registry.bind(state["project_id"])
        if binding["backend"] != "git_local" or binding["route_phase"] != "frozen":
            raise RecordError("PHASE_FROZEN")
        route_receipt = next(
            item for item in state["receipts"] if item["step"] == "route_frozen"
        )["observation"]
        if binding["registry_sha256"] != route_receipt["registry_sha256"]:
            raise RecordError("REVISION_CONFLICT")
        if (
            binding["runtime_directory"] != target["runtime_dir"]
            or binding["project_directory"] != target["target_dir"]
        ):
            raise RecordError("FORBIDDEN")
        runtime = git_store.safe_dir(binding["runtime_directory"])
        expected = {
            "mode": "git",
            "phase": "frozen",
            "epoch": intent["frozen_epoch"],
            "version": target["version"],
        }
        req = {
            "schema_version": 1,
            "request_id": intent["operation_id"],
            "project_id": state["project_id"],
            "actor_role": "pm",
            "scope": {
                "project_id": state["project_id"],
                "actions": ["resume"],
                "records": [],
            },
            "operation_id": intent["operation_id"],
            "action": "resume",
            "target_record_id": None,
            "payload": {
                "transition_id": state["transition_id"],
                "transition_intent_sha256": object_hash(intent),
            },
            "expected": expected,
        }
        core.validate_request(req, True)
        rh = core.request_hash(req)
        privacy_config = {
            "gitleaks": "gitleaks",
            "policy": binding["privacy_policy"],
            "gitleaks_config": str(
                pathlib.Path(__file__).resolve().parents[3] / ".gitleaks.toml"
            ),
        }
        return resume_git_operation(
            req, binding, registry, {"request": req, "transition": intent}, privacy_config
        )

def resume_git_operation(req, binding, registry, scan_payload, privacy_config):
    """Commit a validated frozen Git resume with privacy scan and journal."""
    from datetime import datetime, timezone
    core.validate_request(req, True)
    if (req["action"] != "resume" or req["actor_role"] != "pm"
            or binding["backend"] != "git_local" or binding["route_phase"] != "frozen"
            or scan_payload.get("request") != req):
        raise RecordError("FORBIDDEN")
    scan = git_store.privacy_check(scan_payload, privacy_config)
    runtime = git_store.safe_dir(binding["runtime_directory"])
    expected = req["expected"]
    rh = core.request_hash(req)
    with git_store.locked(runtime):
        if registry.bind(req["project_id"]) != binding:
            raise RecordError("REVISION_CONFLICT")
        entries = git_store.journal_entries(runtime)
        if any(
            item.get("operation_id") != req["operation_id"]
            and item.get("state") != "complete"
            for item in entries
        ):
            raise RecordError("IN_FLIGHT")
        path = git_store.operation_path(runtime, req["operation_id"])
        control = git_store.get_control(runtime)
        git_store.metadata_catalog(runtime, control, req["project_id"])
        original = {**expected, "policy_version": binding["policy_version"]}
        if "record_metadata" in control:
            original["record_metadata"] = control["record_metadata"]
        updated = {
            **original,
            "phase": "steady",
            "version": expected["version"] + 1,
        }
        if os.path.lexists(path):
            entry = git_store.read_operation(runtime, req["operation_id"])
            if entry.get("request_hash") != rh:
                raise RecordError("ID_REUSE")
            if entry.get("state") == "complete":
                if control != updated:
                    raise RecordError("INTEGRITY_FAILED")
                return {
                    "outcome": "no_write",
                    "request": req,
                    "result": entry["result"],
                }
            if entry.get("state") != "files_done" or control not in (
                original,
                updated,
            ):
                raise RecordError("MANUAL_REVIEW")
            for key in ("content_sha256", "policy_sha256", "rules_sha256"):
                if entry.get("scan", {}).get(key) != scan.get(key):
                    raise RecordError("SCAN_INCOMPLETE")
        else:
            if control != original:
                raise RecordError("REVISION_CONFLICT")
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
                "phase": "steady",
                "affected_ids": [],
                "observed_at": now,
            }
            event = {
                "operation_id": req["operation_id"],
                "request_hash": rh,
                "version": updated["version"],
                "action": "resume",
                "target_record_id": None,
                "payload": req["payload"],
                "actor_role": "pm",
                "occurred_at": now,
            }
            entry = {
                "schema_version": 1,
                "operation_id": req["operation_id"],
                "request_hash": rh,
                "state": "files_done",
                "expected_control": original,
                "scope": req["scope"],
                "actor_role": "pm",
                "action": "resume",
                "target_record_id": None,
                "files": [],
                "event": event,
                "result": result,
                "scan": scan,
            }
            git_store.write_json(path, entry)
        if control == original:
            git_store.write_json(runtime / "control.json", updated)
        entry["state"] = "complete"
        git_store.write_json(path, entry)
    return {
        "outcome": "complete",
        "request": req,
        "result": entry["result"],
        "route_phase": "frozen",
    }



def inspect(x):
    root = git_store.safe_dir(x["private_dir"])
    d, state = load(root, x["transition_id"])
    return {
        "phase": state["phase"],
        "next_step": STEPS[state["next_index"]]
        if state["next_index"] < len(STEPS)
        else None,
        "pending": state["pending"],
        "receipts": state["receipts"],
        "physical_fence_independently_verified": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["begin", "issue", "observe", "inspect", "resume-git", "resume-google"],
    )
    parser.add_argument("--input")
    args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding="utf-8-sig") as source:
                data = json.load(source)
        else:
            data = json.load(sys.stdin)
        if args.command in ("resume-git", "resume-google"):
            import routing

            registry = routing.Registry(routing.discover_workspace())
            result = (
                resume_git(data, registry)
                if args.command == "resume-git"
                else resume_google(data, registry)
            )
        else:
            result = {
                "begin": begin,
                "issue": issue,
                "observe": observe,
                "inspect": inspect,
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
