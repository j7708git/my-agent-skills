"""Read-only portfolio inventory with explicit authority and unknown states."""

import argparse
import json
import os
import pathlib
import stat
import sys
import core
import git_store
import google_docs
import routing
from core import RecordError, ident, object_hash, sha

FIELDS = ("state", "needs", "now", "next", "blocker", "updated")


def inventory(registry):
    projects = git_store.safe_dir(registry.workspace / "project-notes" / "projects")
    found = []
    for entry in os.scandir(projects):
        if entry.name.startswith("."):
            continue
        info = entry.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & git_store.REPARSE
        ):
            raise RecordError("INTEGRITY_FAILED")
        if stat.S_ISREG(info.st_mode):
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise RecordError("INTEGRITY_FAILED")
        ident(entry.name)
        found.append(entry.name)
    if len({name.casefold() for name in found}) != len(found):
        raise RecordError("INTEGRITY_FAILED")
    return sorted(found)


def git_row(project, project_id):
    for name in ("status.md", "progress.md"):
        try:
            path = git_store.target_path(project, name)
        except RecordError as exc:
            if exc.code == "NOT_FOUND":
                continue
            return unknown(project_id, "git_local", "SOURCE_UNREADABLE")
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeError):
            return unknown(project_id, "git_local", "SOURCE_UNREADABLE")
        if name == "progress.md":
            return {
                **unknown(project_id, "git_local", "LEGACY_PROGRESS_UNVERIFIED"),
                "source": {
                    "kind": "legacy_progress",
                    "sha256": sha(raw),
                    "source_time": None,
                },
            }
        try:
            core.status_patch(raw, {})
            values = {}
            for line in text.splitlines()[1:]:
                if line.startswith("## "):
                    break
                if ":" in line:
                    key, value = line.split(":", 1)
                    if key in FIELDS:
                        values[key] = value.strip().strip('"')
            if set(values) != set(FIELDS):
                raise ValueError()
        except (RecordError, ValueError):
            return {
                **unknown(project_id, "git_local", "STATUS_FORMAT_INVALID"),
                "source": {"kind": "status", "sha256": sha(raw), "source_time": None},
            }
        return {
            "project_id": project_id,
            "authority": "git_local",
            "availability": "known",
            "values": values,
            "source": {
                "kind": "status",
                "sha256": sha(raw),
                "source_time": values["updated"],
            },
            "issues": [],
        }
    return unknown(project_id, "git_local", "NO_STATUS_OR_PROGRESS")


def unknown(project_id, authority, reason):
    return {
        "project_id": project_id,
        "authority": authority,
        "availability": "unknown",
        "values": None,
        "source": None,
        "issues": [reason],
    }


def collect(registry, request):
    if (
        not isinstance(request, dict)
        or set(request) != {"project_ids", "google_observations"}
        or not isinstance(request["project_ids"], list)
        or not isinstance(request["google_observations"], dict)
    ):
        raise RecordError("INVALID_ARGUMENT")
    discovered = inventory(registry)
    try:
        data, registry_sha = registry.load()
        registered = set(data["projects"])
        registry_error = None
    except RecordError as exc:
        registered = set()
        registry_sha = None
        registry_error = exc.code
    all_ids = sorted(set(discovered) | registered)
    supplied = request["project_ids"]
    if (
        any(not isinstance(item, str) for item in supplied)
        or len(supplied) != len(set(supplied))
        or sorted(supplied) != all_ids
    ):
        raise RecordError("FORBIDDEN")
    if registry_error is None and set(request["google_observations"]) - registered:
        raise RecordError("FORBIDDEN")
    rows = []
    for project_id in all_ids:
        if registry_error:
            rows.append(unknown(project_id, "unknown", "REGISTRY_UNAVAILABLE"))
            continue
        if project_id not in registered:
            rows.append(unknown(project_id, "unknown", "NOT_REGISTERED"))
            continue
        if project_id not in discovered:
            rows.append(unknown(project_id, "unknown", "PROJECT_DIRECTORY_MISSING"))
            continue
        entry = data["projects"][project_id]
        if entry["backend"] == "git_local":
            if (
                entry["classification"] != "unmigrated_git"
                or entry["route_phase"] != "steady"
            ):
                rows.append(
                    unknown(project_id, "git_local", "CONTROL_NOT_POSITIVE_UNMIGRATED")
                )
                continue
            try:
                binding = registry.bind(project_id)
                runtime = pathlib.Path(binding["runtime_directory"])
                if os.path.lexists(runtime):
                    control = git_store.get_control(git_store.safe_dir(runtime))
                    if (
                        control["mode"] != "git"
                        or control["policy_version"] != binding["policy_version"]
                    ):
                        raise RecordError("CONTROL_UNAVAILABLE")
                if registry.bind(project_id) != binding:
                    raise RecordError("REVISION_CONFLICT")
                rows.append(
                    git_row(pathlib.Path(binding["project_directory"]), project_id)
                )
            except (RecordError, OSError):
                rows.append(unknown(project_id, "git_local", "CONTROL_UNAVAILABLE"))
            continue
        observation = request["google_observations"].get(project_id)
        if (
            not isinstance(observation, dict)
            or set(observation)
            != {"document", "head_tab_id", "summary_tab_id", "fresh_read"}
            or observation["fresh_read"] is not True
        ):
            rows.append(unknown(project_id, "google_head", "GOOGLE_UNAVAILABLE"))
            continue
        try:
            binding = registry.bind(project_id)
            snap = google_docs.snapshot(
                observation["document"],
                binding["locator"]["document_id"],
                observation["head_tab_id"],
                observation["summary_tab_id"],
                project_id,
            )
            head = snap["head"]
            if (
                head["control"]["mode"] != "google"
                or head["control"]["policy_version"] != binding["policy_version"]
            ):
                raise RecordError("INTEGRITY_FAILED")
            if registry.bind(project_id) != binding:
                raise RecordError("REVISION_CONFLICT")
            values = {key: head["summary"][key] for key in FIELDS}
            rows.append(
                {
                    "project_id": project_id,
                    "authority": "google_head",
                    "availability": "known",
                    "values": values,
                    "source": {
                        "kind": "google_head",
                        "sha256": object_hash(head),
                        "source_time": values["updated"],
                        "revision": snap["revision"],
                        "version": head["version"],
                        "epoch": head["control"]["epoch"],
                    },
                    "issues": [],
                }
            )
        except (RecordError, KeyError, TypeError, OSError):
            rows.append(
                unknown(project_id, "google_head", "GOOGLE_CONTROL_UNAVAILABLE")
            )
    return {
        "schema_version": 1,
        "rows": rows,
        "inventory": all_ids,
        "inventory_hash": object_hash(all_ids),
        "registry_sha256": registry_sha,
        "freshness": "caller_observed",
        "writes_performed": False,
        "registry_error": registry_error,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    args = parser.parse_args()
    try:
        if args.input:
            with open(args.input, encoding="utf-8-sig") as source:
                request = json.load(source)
        else:
            request = json.load(sys.stdin)
        result = collect(routing.Registry(routing.discover_workspace()), request)
        print(
            json.dumps(
                {"ok": True, "schema_version": 1, "data": result}, ensure_ascii=True
            )
        )
        return 0
    except (RecordError, OSError, KeyError, ValueError, TypeError) as error:
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
