"""Publish one fixed workspace route through a durable frozen transition receipt."""

import argparse
import base64
import json
import os
import pathlib
import sys
import core
import git_store
import google_docs
import connector_protocol
import routing
import transition
from core import RecordError, ident, object_hash, sha

STAGES = ("freeze-source", "switch-frozen", "resume-steady")


def publication_dir(runtime, operation, create=False):
    ident(operation)
    parent = runtime / "route-publications"
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


def transition_state(registry, x):
    root = git_store.safe_dir(x["transition_private_dir"])
    if root != registry.workspace and registry.workspace not in root.parents:
        raise RecordError("FORBIDDEN")
    _, state = transition.load(root, x["transition_id"])
    if state["project_id"] != x["project_id"]:
        raise RecordError("FORBIDDEN")
    return root, state


def one_receipt(state, step):
    matches = [item for item in state["receipts"] if item["step"] == step]
    if len(matches) != 1:
        raise RecordError("CONTROL_UNAVAILABLE")
    return matches[0]


def verify_control(
    root,
    binding,
    mode,
    resource,
    phase,
    epoch,
    version,
    observation,
    expected_last=None,
    expected_root=None,
    expected_action="resume",
):
    if mode == "git":
        runtime = git_store.safe_dir(binding["runtime_directory"])
        control = git_store.get_control(runtime)
        if (
            control["mode"] != "git"
            or control["phase"] != phase
            or control["epoch"] != epoch
            or control["version"] != version
            or control["policy_version"] != binding["policy_version"]
        ):
            raise RecordError("REVISION_CONFLICT")
        git_store.metadata_catalog(runtime, control, binding["project_id"])
        git_store.ensure_no_incomplete(runtime)
        if expected_last is not None:
            entry = git_store.read_operation(runtime, expected_last["operation_id"])
            if (
                entry.get("state") != "complete"
                or entry.get("event", {}).get("action") != expected_action
                or entry.get("event", {}).get("operation_id")
                != expected_last["operation_id"]
                or entry.get("event", {}).get("request_hash")
                != expected_last["request_hash"]
                or entry.get("request_hash") != expected_last["request_hash"]
            ):
                raise RecordError("INTEGRITY_FAILED")
    else:
        if not isinstance(observation, dict) or set(observation) != {
            "document",
            "head_tab_id",
            "summary_tab_id",
            "existing",
        }:
            raise RecordError("CONTROL_UNAVAILABLE")
        snap = google_docs.snapshot(
            observation["document"],
            resource,
            observation["head_tab_id"],
            observation["summary_tab_id"],
            binding["project_id"],
        )
        head = snap["head"]
        control = head["control"]
        if (
            control["mode"] != "google"
            or control["phase"] != phase
            or control["epoch"] != epoch
            or head["version"] != version
            or control["policy_version"] != binding["policy_version"]
        ):
            raise RecordError("REVISION_CONFLICT")
        if (
            expected_root is not None
            and head["root_manifest"]["sha256"] != expected_root
        ):
            raise RecordError("INTEGRITY_FAILED")
        if expected_last is not None and head["last_operation"] != expected_last:
            raise RecordError("INTEGRITY_FAILED")
        _, operations, _, _, _ = connector_protocol.read_graph(
            root, head, observation["existing"]
        )
        if expected_last is not None:
            entry = operations.get(expected_last["operation_id"])
            if (
                not entry
                or entry.get("request_hash") != expected_last["request_hash"]
                or entry.get("event", {}).get("operation_id")
                != expected_last["operation_id"]
                or entry.get("event", {}).get("request_hash")
                != expected_last["request_hash"]
                or entry.get("event", {}).get("action") != expected_action
            ):
                raise RecordError("INTEGRITY_FAILED")


def freeze_operation(root, binding, source, observation, operation_id):
    if source["mode"] == "git":
        entry = git_store.read_operation(
            git_store.safe_dir(binding["runtime_directory"]), operation_id
        )
    else:
        if not isinstance(observation, dict) or set(observation) != {
            "document",
            "head_tab_id",
            "summary_tab_id",
            "existing",
        }:
            raise RecordError("CONTROL_UNAVAILABLE")
        snap = google_docs.snapshot(
            observation["document"],
            source["resource_id"],
            observation["head_tab_id"],
            observation["summary_tab_id"],
            binding["project_id"],
        )
        _, operations, _, _, _ = connector_protocol.read_graph(
            root, snap["head"], observation["existing"]
        )
        entry = operations.get(operation_id)
    if not isinstance(entry, dict) or not isinstance(entry.get("scope"), dict):
        raise RecordError("CONTROL_UNAVAILABLE")
    return entry


def verify_target_receipt_intent(state, receipt):
    target = state["target"]
    target_before_pin = {**target, "manifest_sha256": None}
    hashes = set()
    for candidate in (target, target_before_pin):
        intent = {
            "transition_id": state["transition_id"],
            "project_id": state["project_id"],
            "operation_id": ident(receipt.get("operation_id")),
            "step": "target_frozen",
            "source": state["source"],
            "target": candidate,
            "frozen_epoch": state["source"]["epoch"] + 1,
            "writers": state["writers"],
        }
        hashes.add(object_hash(intent))
    if receipt.get("intent_hash") not in hashes:
        raise RecordError("INTEGRITY_FAILED")


def compute(registry, x):
    if x.get("actor_role") != "pm" or x.get("stage") not in STAGES:
        raise RecordError("FORBIDDEN")
    project = ident(x["project_id"])
    ident(x["operation_id"])
    root, state = transition_state(registry, x)
    binding = registry.bind(project)
    data, digest = registry.load()
    if digest != x.get("expected_registry_sha256") or data["generation"] != x.get(
        "expected_generation"
    ):
        raise RecordError("REVISION_CONFLICT")
    route = data["projects"][project]
    stage = x["stage"]
    pending = state.get("pending")
    if pending is not None:
        expected_intent = {
            "transition_id": state["transition_id"],
            "project_id": project,
            "operation_id": pending["operation_id"],
            "step": pending["step"],
            "source": state["source"],
            "target": state["target"],
            "frozen_epoch": state["source"]["epoch"] + 1,
            "writers": state["writers"],
        }
        if pending != expected_intent:
            raise RecordError("INTEGRITY_FAILED")
    if stage == "freeze-source":
        if (
            not pending
            or pending["step"] != "freeze"
            or object_hash(pending) != x.get("transition_intent_sha256")
        ):
            raise RecordError("OUTCOME_UNKNOWN")
        source = state["source"]
        backend = "git_local" if source["mode"] == "git" else "google_head"
        if route["backend"] != backend or route["route_phase"] != "steady":
            raise RecordError("REVISION_CONFLICT")
        if (
            backend == "google_head"
            and route["locator"]["document_id"] != source["resource_id"]
        ):
            raise RecordError("FORBIDDEN")
        if backend == "git_local" and sha(str(pathlib.Path(binding["project_directory"]).resolve()).encode("utf-8")) != source["resource_id"]:
            raise RecordError("FORBIDDEN")
        freeze_entry = freeze_operation(
            root, binding, source, x.get("control_observation"), pending["operation_id"]
        )
        freeze_request = {
            "schema_version": 1,
            "request_id": pending["operation_id"],
            "project_id": project,
            "actor_role": "pm",
            "scope": freeze_entry["scope"],
            "operation_id": pending["operation_id"],
            "action": "freeze",
            "target_record_id": None,
            "payload": {},
            "expected": {
                "mode": source["mode"],
                "phase": "steady",
                "epoch": source["epoch"],
                "version": source["version"],
            },
        }
        core.validate_request(freeze_request, True)
        expected_last = {
            "operation_id": pending["operation_id"],
            "request_hash": core.request_hash(freeze_request),
        }
        verify_control(
            root,
            binding,
            source["mode"],
            source["resource_id"],
            "frozen",
            source["epoch"] + 1,
            source["version"] + 1,
            x.get("control_observation"),
            expected_last=expected_last,
            expected_action="freeze",
        )
        updated = {**route, "route_phase": "frozen"}
    elif stage == "switch-frozen":
        if (
            not pending
            or pending["step"] != "route_frozen"
            or object_hash(pending) != x.get("transition_intent_sha256")
        ):
            raise RecordError("OUTCOME_UNKNOWN")
        source = state["source"]
        source_backend = "git_local" if source["mode"] == "git" else "google_head"
        if route["backend"] != source_backend:
            raise RecordError("REVISION_CONFLICT")
        if source_backend == "google_head" and route["locator"]["document_id"] != source["resource_id"]:
            raise RecordError("FORBIDDEN")
        if source_backend == "git_local" and sha(str(pathlib.Path(binding["project_directory"]).resolve()).encode("utf-8")) != source["resource_id"]:
            raise RecordError("FORBIDDEN")
        target_receipt = one_receipt(state, "target_frozen")
        verify_target_receipt_intent(state, target_receipt)
        obs = target_receipt["observation"]
        target = state["target"]
        if (
            route["route_phase"] != "frozen"
            or target["manifest_sha256"] is None
            or obs.get("manifest_sha256") != target["manifest_sha256"]
            or obs.get("resource_id") != target["resource_id"]
            or obs.get("mode") != target["mode"]
            or obs.get("phase") != "frozen"
            or obs.get("epoch") != state["source"]["epoch"] + 1
            or obs.get("version")
            != state["source"]["version"] + (2 if target["mode"] == "google" else 1)
        ):
            raise RecordError("REVISION_CONFLICT")
        if target["mode"] == "git":
            import promotion

            snapid = one_receipt(state, "snapshot")["observation"]["snapshot_id"]
            if (
                promotion.verify_project(root, snapid, binding, project)
                != target["manifest_sha256"]
            ):
                raise RecordError("INTEGRITY_FAILED")
            if "promotion_operation" in obs:
                directory = promotion.session(root, obs["promotion_operation"])
                promotion_state = git_store.read_json(directory / "promotion.json")
                plan = promotion_state["plan"]
                if (
                    promotion_state["phase"] != "complete"
                    or promotion_state["plan_hash"] != obs.get("promotion_plan_hash")
                    or object_hash(plan) != promotion_state["plan_hash"]
                    or plan["snapshot_id"] != snapid
                    or plan["binding"]["runtime_directory"]
                    != binding["runtime_directory"]
                    or plan["binding"]["project_directory"]
                    != binding["project_directory"]
                ):
                    raise RecordError("INTEGRITY_FAILED")
                actual = promotion.canonical_files(
                    git_store.safe_dir(binding["runtime_directory"])
                )
                if set(actual) != {item["path"] for item in plan["files"]} or any(
                    sha(actual[item["path"]]) != item["after_sha256"]
                    for item in plan["files"]
                ):
                    raise RecordError("INTEGRITY_FAILED")
        verify_control(
            root,
            binding,
            target["mode"],
            target["resource_id"],
            "frozen",
            obs["epoch"],
            obs["version"],
            x.get("control_observation"),
            expected_root=target["manifest_sha256"]
            if target["mode"] == "google"
            else None,
        )
        updated = {
            "classification": "managed",
            "backend": "git_local" if target["mode"] == "git" else "google_head",
            "route_phase": "frozen",
            "locator": None
            if target["mode"] == "git"
            else {"document_id": target["resource_id"]},
        }
    else:
        receipt = one_receipt(state, "resume")
        target = state["target"]
        obs = receipt["observation"]
        if state["phase"] != "complete" or receipt["intent_hash"] != x.get(
            "transition_intent_sha256"
        ):
            raise RecordError("OUTCOME_UNKNOWN")
        backend = "git_local" if target["mode"] == "git" else "google_head"
        if route["backend"] != backend or route["route_phase"] != "frozen":
            raise RecordError("REVISION_CONFLICT")
        if (
            backend == "google_head"
            and route["locator"]["document_id"] != target["resource_id"]
        ):
            raise RecordError("FORBIDDEN")
        # Reconstruct the exact resume hash from the validated local transition intent.
        target_receipt = one_receipt(state, "target_frozen")
        verify_target_receipt_intent(state, target_receipt)
        target_obs = target_receipt["observation"]
        if (
            target_obs.get("resource_id") != target["resource_id"]
            or target_obs.get("mode") != target["mode"]
            or target_obs.get("phase") != "frozen"
            or target_obs.get("manifest_sha256") != target["manifest_sha256"]
            or target_obs.get("epoch") != state["source"]["epoch"] + 1
            or target_obs.get("version")
            != state["source"]["version"] + (2 if target["mode"] == "google" else 1)
            or obs.get("resource_id") != target["resource_id"]
            or obs.get("mode") != target["mode"]
            or obs.get("phase") != "steady"
            or obs.get("epoch") != target_obs["epoch"]
            or obs.get("version") != target_obs["version"] + 1
        ):
            raise RecordError("INTEGRITY_FAILED")
        intent = {
            "transition_id": state["transition_id"],
            "project_id": project,
            "operation_id": receipt["operation_id"],
            "step": "resume",
            "source": state["source"],
            "target": state["target"],
            "frozen_epoch": state["source"]["epoch"] + 1,
            "writers": state["writers"],
        }
        req = {
            "schema_version": 1,
            "request_id": receipt["operation_id"],
            "project_id": project,
            "actor_role": "pm",
            "scope": {"project_id": project, "actions": ["resume"], "records": []},
            "operation_id": receipt["operation_id"],
            "action": "resume",
            "target_record_id": None,
            "payload": {
                "transition_id": state["transition_id"],
                "transition_intent_sha256": object_hash(intent),
            },
            "expected": {
                "mode": target["mode"],
                "phase": "frozen",
                "epoch": obs["epoch"],
                "version": target_obs["version"],
            },
        }
        if object_hash(intent) != receipt["intent_hash"]:
            raise RecordError("INTEGRITY_FAILED")
        expected_last = {
            "operation_id": receipt["operation_id"],
            "request_hash": core.request_hash(req),
        }
        if target["mode"] == "git":
            import promotion

            snapid = one_receipt(state, "snapshot")["observation"]["snapshot_id"]
            if (
                promotion.verify_project(root, snapid, binding, project)
                != target["manifest_sha256"]
            ):
                raise RecordError("INTEGRITY_FAILED")
        verify_control(
            root,
            binding,
            target["mode"],
            target["resource_id"],
            "steady",
            obs["epoch"],
            obs["version"],
            x.get("control_observation"),
            expected_last=expected_last,
        )
        updated = {**route, "route_phase": "steady"}
    after = json.loads(json.dumps(data))
    after["projects"][project] = updated
    after["generation"] += 1
    return binding, data, digest, after


def prepare(x, registry):
    if x.get("actor_role") != "pm":
        raise RecordError("FORBIDDEN")
    current_binding = registry.bind(ident(x["project_id"]))
    current_runtime = git_store.safe_dir(current_binding["runtime_directory"])
    try:
        old_directory = publication_dir(current_runtime, x["operation_id"])
    except RecordError as error:
        if error.code != "NOT_FOUND":
            raise
        old_directory = None
    if old_directory is not None:
        old = git_store.read_json(old_directory / "publication.json")
        plan = old["plan"]
        expected = {
            "project_id": x["project_id"],
            "operation_id": x["operation_id"],
            "stage": x["stage"],
            "transition_id": x["transition_id"],
            "transition_private_dir": x["transition_private_dir"],
            "transition_intent_sha256": x["transition_intent_sha256"],
            "before_sha256": x["expected_registry_sha256"],
            "before_generation": x["expected_generation"],
        }
        if object_hash(plan) != old["plan_hash"] or any(
            plan.get(key) != value for key, value in expected.items()
        ):
            raise RecordError("ID_REUSE")
        if old["phase"] == "complete":
            _, current = registry.load()
            return {
                "outcome": "no_write",
                "plan_hash": old["plan_hash"],
                "registry_sha256": plan["after_sha256"],
                "current_registry_sha256": current,
            }
        if old["phase"] == "prepared":
            return {
                "outcome": "prepared",
                "plan": plan,
                "plan_hash": old["plan_hash"],
                "writes_performed": False,
            }
        raise RecordError("OUTCOME_UNKNOWN")
    binding, before, before_sha, after = compute(registry, x)
    runtime = git_store.safe_dir(binding["runtime_directory"])
    directory = publication_dir(runtime, x["operation_id"], True)
    plan = {
        "schema_version": 1,
        "operation_id": x["operation_id"],
        "project_id": x["project_id"],
        "stage": x["stage"],
        "transition_id": x["transition_id"],
        "transition_private_dir": x["transition_private_dir"],
        "transition_intent_sha256": x["transition_intent_sha256"],
        "before_sha256": before_sha,
        "before_generation": before["generation"],
        "after_sha256": sha(core.canonical(after)),
        "after_generation": after["generation"],
        "binding": binding,
    }
    before_raw = (registry.workspace / routing.REGISTRY).read_bytes()
    if sha(before_raw) != before_sha:
        raise RecordError("REVISION_CONFLICT")
    state = {
        "schema_version": 1,
        "plan": plan,
        "plan_hash": object_hash(plan),
        "before_b64": base64.b64encode(before_raw).decode("ascii"),
        "after": after,
        "phase": "prepared",
    }
    git_store.write_json(directory / "publication.json", state)
    return {
        "outcome": "prepared",
        "plan": plan,
        "plan_hash": state["plan_hash"],
        "writes_performed": False,
    }


def apply(x, registry):
    if x.get("actor_role") != "pm":
        raise RecordError("FORBIDDEN")
    project = ident(x["project_id"])
    binding = registry.bind(project)
    runtime = git_store.safe_dir(binding["runtime_directory"])
    directory = publication_dir(runtime, x["operation_id"])
    with (
        git_store.locked(directory),
        git_store.locked(registry.workspace / ".openchamber/workspace-records"),
    ):
        state = git_store.read_json(directory / "publication.json")
        plan = state["plan"]
        if (
            state["plan_hash"] != x.get("plan_hash")
            or object_hash(plan) != state["plan_hash"]
            or plan["project_id"] != project
            or plan["operation_id"] != x["operation_id"]
        ):
            raise RecordError("ID_REUSE")
        try:
            saved_before = base64.b64decode(state["before_b64"], validate=True)
        except (ValueError, KeyError, TypeError):
            raise RecordError("INTEGRITY_FAILED") from None
        if sha(saved_before) != plan["before_sha256"]:
            raise RecordError("INTEGRITY_FAILED")
        if state["phase"] == "complete":
            _, current = registry.load()
            return {
                "outcome": "no_write",
                "plan_hash": state["plan_hash"],
                "registry_sha256": plan["after_sha256"],
                "current_registry_sha256": current,
            }
        if state["phase"] != "prepared":
            raise RecordError("OUTCOME_UNKNOWN")
        request = {
            **x,
            "stage": plan["stage"],
            "operation_id": plan["operation_id"],
            "transition_id": plan["transition_id"],
            "transition_private_dir": plan["transition_private_dir"],
            "transition_intent_sha256": plan["transition_intent_sha256"],
            "expected_registry_sha256": plan["before_sha256"],
            "expected_generation": plan["before_generation"],
        }
        _, _, _, after = compute(registry, request)
        if (
            sha(core.canonical(after)) != plan["after_sha256"]
            or after != state["after"]
        ):
            raise RecordError("REVISION_CONFLICT")
        state["phase"] = "unknown"
        git_store.write_json(directory / "publication.json", state)
        git_store.write_json(registry.workspace / routing.REGISTRY, after)
        _, actual = registry.load()
        if actual != plan["after_sha256"]:
            return {"outcome": "manual_review", "reason": "READBACK_MISMATCH"}
        state["phase"] = "complete"
        git_store.write_json(directory / "publication.json", state)
        return {
            "outcome": "complete",
            "plan_hash": state["plan_hash"],
            "registry_sha256": actual,
            "generation": plan["after_generation"],
        }


def reconcile(x, registry):
    if x.get("actor_role") != "pm":
        raise RecordError("FORBIDDEN")
    binding = registry.bind(ident(x["project_id"]))
    directory = publication_dir(
        git_store.safe_dir(binding["runtime_directory"]), x["operation_id"]
    )
    with (
        git_store.locked(directory),
        git_store.locked(registry.workspace / ".openchamber/workspace-records"),
    ):
        state = git_store.read_json(directory / "publication.json")
        plan = state["plan"]
        if (
            state["plan_hash"] != x.get("plan_hash")
            or object_hash(plan) != state["plan_hash"]
            or plan["project_id"] != x["project_id"]
            or plan["operation_id"] != x["operation_id"]
        ):
            raise RecordError("ID_REUSE")
        try:
            saved_before = base64.b64decode(state["before_b64"], validate=True)
        except (ValueError, KeyError, TypeError):
            raise RecordError("INTEGRITY_FAILED") from None
        if sha(saved_before) != plan["before_sha256"]:
            raise RecordError("INTEGRITY_FAILED")
        if state["phase"] not in ("unknown", "complete"):
            raise RecordError("OUTCOME_UNKNOWN")
        _, current = registry.load()
        if current == plan["after_sha256"]:
            state["phase"] = "complete"
            git_store.write_json(directory / "publication.json", state)
            return {
                "outcome": "complete",
                "plan_hash": state["plan_hash"],
                "registry_sha256": current,
                "generation": plan["after_generation"],
            }
        return {
            "outcome": "manual_review",
            "reason": "NOT_PROVEN",
            "registry_sha256": current,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "apply", "reconcile"])
    parser.add_argument("--input")
    args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding="utf-8-sig") as source:
                data = json.load(source)
        else:
            data = json.load(sys.stdin)
        registry = routing.Registry(routing.discover_workspace())
        result = {"prepare": prepare, "apply": apply, "reconcile": reconcile}[
            args.command
        ](data, registry)
        print(
            json.dumps(
                {"ok": True, "schema_version": 1, "data": result}, ensure_ascii=True
            )
        )
        return 0
    except (RecordError, KeyError, ValueError, TypeError, OSError) as error:
        code = error.code if isinstance(error, RecordError) else "CONTROL_UNAVAILABLE"
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
