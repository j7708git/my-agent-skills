"""Local JSON CLI. Does not call Google or use credentials."""

import argparse
import json
import os
import pathlib
import sys
from core import RecordError, validate_request, object_hash, request_hash
import git_store
import routing


def dispatch(command, env):
    if not isinstance(env, dict) or not isinstance(env.get("request"), dict):
        raise RecordError("INVALID_ARGUMENT")
    req = env["request"]
    validate_request(req)
    project = git_store.project_dir(env.get("notes_root", ""), req["project_id"])
    if command == "inventory":
        return git_store.inventory(project, req)
    runtime = git_store.runtime_dir(
        env.get("runtime_dir", ""), project, req["project_id"]
    )
    if command == "read":
        return git_store.enumerate_records(project, runtime, req)
    if command == "initialize":
        return git_store.initialize(runtime, req)
    if command == "resolve":
        return git_store.get_control(runtime)
    if command == "preview":
        return git_store.preview(project, runtime, req, env.get("privacy_config"))
    if command == "apply":
        return git_store.apply(
            project,
            runtime,
            req,
            env.get("privacy_config"),
            env.get("plan"),
            env.get("plan_hash"),
        )
    if command == "reconcile":
        return git_store.reconcile(project, runtime, req)
    raise RecordError("INVALID_ARGUMENT")


def managed_dispatch(command, env, registry):
    """Fixed-location cooperative Git entrance; low-level dispatch is fixture-only.

    Route checks do not constitute a cross-store transaction or a global writer fence.
    Completed receipt reads remain possible while frozen; new writes require steady.
    """
    if not isinstance(env, dict) or not isinstance(env.get("request"), dict):
        raise RecordError("INVALID_ARGUMENT")
    req = env["request"]
    validate_request(req)
    binding = registry.bind(req["project_id"])
    if binding["backend"] != "git_local":
        raise RecordError("STALE_MODE")
    supplied = env.get("routing_binding")
    if supplied is not None and supplied != binding:
        raise RecordError("REVISION_CONFLICT")
    project = git_store.safe_dir(binding["project_directory"])
    runtime = pathlib.Path(binding["runtime_directory"])
    exists = os.path.lexists(runtime)
    if exists:
        git_store.safe_dir(runtime)
        control = git_store.get_control(runtime)
        if control["mode"] != "git":
            raise RecordError("STALE_MODE")
        if control["policy_version"] != binding["policy_version"]:
            raise RecordError("INTEGRITY_FAILED")
    privacy_config = {
        "gitleaks": "gitleaks",
        "policy": binding["privacy_policy"],
        "gitleaks_config": str(pathlib.Path(__file__).resolve().parents[3] / ".gitleaks.toml"),
    }
    if command in ("initialize", "preview", "apply", "recover"):
        registry.require_no_activation(req["project_id"])
    if command in ("preview", "apply", "recover"):
        validate_request(req, True)
        # This branch only reads a completed receipt; it cannot resume a partial write.
        if exists:
            with git_store.locked(runtime):
                git_store.journal_entries(runtime)
                if os.path.lexists(git_store.operation_path(runtime, req["operation_id"])):
                    old = git_store.read_operation(runtime, req["operation_id"])
                    if old.get("request_hash") != request_hash(req):
                        raise RecordError("ID_REUSE")
                    if old.get("state") != "complete":
                        if command != "recover":
                            raise RecordError("MANUAL_REVIEW")
                    elif registry.bind(req["project_id"]) != binding:
                        raise RecordError("REVISION_CONFLICT")
                    elif old.get("state") == "complete":
                        return {"outcome": "no_write", "result": old["result"], "routing_binding": binding}
    if command in ("initialize", "preview", "apply"):
        registry.check(binding)
        if command == "initialize":
            if binding["classification"] != "unmigrated_git" or req["actor_role"] != "pm":
                raise RecordError("FORBIDDEN")
            # This is the sole explicit creation path, after the authoritative route check.
            runtime = git_store.runtime_dir(registry.workspace / routing.RUNTIME, project, req["project_id"])
            if str(runtime) != binding["runtime_directory"]:
                raise RecordError("INTEGRITY_FAILED")
            initialized = git_store.initialize(runtime, {**req, "classification": "unmigrated_git"})
            return {"control": initialized, "routing_binding": binding}
        if not exists:
            raise RecordError("CONTROL_UNAVAILABLE")
    elif command == "recover":
        # Continue an already accepted exact plan, including a crash after freeze's control write.
        # This does not authorize a new writer or bypass a changed/frozen route binding.
        if binding["route_phase"] != "steady":
            raise RecordError("PHASE_FROZEN")
        if not exists:
            raise RecordError("CONTROL_UNAVAILABLE")
    elif command not in ("inventory", "resolve", "read", "reconcile"):
        raise RecordError("INVALID_ARGUMENT")
    # Read commands may observe frozen control, but must still use the same exact route.
    if registry.bind(req["project_id"]) != binding:
        raise RecordError("REVISION_CONFLICT")
    if command == "inventory":
        result = git_store.inventory(project, req)
    elif command == "resolve":
        if not exists:
            raise RecordError("CONTROL_UNAVAILABLE")
        result = {"control": git_store.get_control(runtime)}
    elif command == "read":
        if not exists:
            raise RecordError("CONTROL_UNAVAILABLE")
        result = git_store.enumerate_records(project, runtime, req)
    elif command == "reconcile":
        if not exists:
            raise RecordError("CONTROL_UNAVAILABLE")
        result = git_store.reconcile(project, runtime, req)
    elif command == "preview":
        result = git_store.preview(project, runtime, req, privacy_config)
        if result.get("outcome") == "planned":
            result["plan"]["routing_binding"] = binding
            result["plan_hash"] = object_hash(result["plan"])
    else:
        plan = env.get("plan")
        if not isinstance(plan, dict) or plan.get("routing_binding") != binding:
            raise RecordError("REVISION_CONFLICT")
        if command == "recover":
            import recovery
            result = recovery.recover(project, runtime, req, privacy_config, plan, env.get("plan_hash"))
        else:
            result = git_store.apply(project, runtime, req, privacy_config, plan, env.get("plan_hash"))
    return {**result, "routing_binding": binding}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "command",
        choices=[
            "initialize",
            "resolve",
            "inventory",
            "read",
            "preview",
            "apply",
            "reconcile",
            "recover",
        ],
    )
    ap.add_argument("--input")
    ap.add_argument("--managed", action="store_true")
    args = ap.parse_args()
    request_id = "unknown"
    try:
        if args.input:
            with open(args.input, encoding="utf-8-sig") as f:
                env = json.load(f)
        else:
            env = json.load(sys.stdin)
        if isinstance(env, dict) and isinstance(env.get("request"), dict):
            request_id = env["request"].get("request_id", "unknown")
        data = managed_dispatch(args.command, env, routing.Registry(routing.discover_workspace())) if args.managed else dispatch(args.command, env)
        if isinstance(data, dict) and data.get("outcome") == "manual_review":
            out = {
                "ok": False,
                "schema_version": 1,
                "request_id": request_id,
                "error": {
                    "code": "MANUAL_REVIEW",
                    "message": "MANUAL_REVIEW",
                    "retryable": False,
                    "details": data,
                },
            }
            print(json.dumps(out, ensure_ascii=True, separators=(",", ":")))
            return 6
        out = {
            "ok": True,
            "schema_version": 1,
            "request_id": request_id,
            "data": data,
        }
        print(
            json.dumps(out, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        )
        return 0
    except RecordError as e:
        out = {
            "ok": False,
            "schema_version": 1,
            "request_id": request_id,
            "error": {
                "code": e.code,
                "message": e.code,
                "retryable": False,
                "details": {},
            },
        }
        print(json.dumps(out, ensure_ascii=True, separators=(",", ":")))
        if e.code in ("PARTIAL", "OUTCOME_UNKNOWN", "MANUAL_REVIEW"):
            return 6
        if e.code in ("AUTH_EXPIRED", "FORBIDDEN"):
            return 4
        if e.code in ("CONTROL_UNAVAILABLE", "SCAN_INCOMPLETE", "IN_FLIGHT"):
            return 5
        if e.code in (
            "STALE_MODE",
            "STALE_EPOCH",
            "PHASE_FROZEN",
            "REVISION_CONFLICT",
            "ID_REUSE",
            "INTEGRITY_FAILED",
            "SCAN_FINDING",
        ):
            return 3
        return 2
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        out = {
            "ok": False,
            "schema_version": 1,
            "request_id": request_id,
            "error": {
                "code": "INVALID_ARGUMENT",
                "message": "INVALID_ARGUMENT",
                "retryable": False,
                "details": {},
            },
        }
        print(json.dumps(out, ensure_ascii=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    sys.exit(main())
