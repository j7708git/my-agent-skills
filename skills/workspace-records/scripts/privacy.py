"""Exact-content privacy checks; scanner failure is never a pass."""

import hashlib
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
from core import RecordError, canonical, sha

sys.dont_write_bytecode = True


def check(payload, config):
    if not isinstance(config, dict) or set(config) != {
        "gitleaks",
        "policy",
        "gitleaks_config",
    }:
        raise RecordError("SCAN_INCOMPLETE")
    try:
        binary = shutil.which(config["gitleaks"])
        policy = pathlib.Path(config["policy"]).resolve(strict=True)
        rules = pathlib.Path(config["gitleaks_config"]).resolve(strict=True)
        if not binary or not policy.is_file() or not rules.is_file():
            raise ValueError()
        core_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "repo-privacy-audit"
            / "scripts"
            / "audit_core.py"
        )
        spec = importlib.util.spec_from_file_location(
            "workspace_records_audit", core_path
        )
        audit = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = audit
        spec.loader.exec_module(audit)
        policy_obj = audit.load_policy(str(policy))
        engine = audit.FallbackEngine(policy_obj)
        if (
            not engine.available
            or policy_obj.paths.get("personalPathSeverity") != "high"
        ):
            raise ValueError()
        raw = canonical(payload)
        strings = []

        def visit(obj):
            if isinstance(obj, str):
                strings.append(obj)
            elif isinstance(obj, list):
                for x in obj:
                    visit(x)
            elif isinstance(obj, dict):
                for x in obj.values():
                    visit(x)

        visit(payload)
        data = raw + b"\n" + "\n".join(strings).encode("utf-8")
        if len(data) > 64 * 1024 * 1024:
            raise ValueError()
        text = data.decode("utf-8")
        if engine.scan_text_full(
            text, source="content", kind="content"
        ) or engine.scan_personal_paths(text, kind="content"):
            raise RecordError("SCAN_FINDING")
        env = os.environ.copy()
        env.pop("GITLEAKS_CONFIG", None)
        env.pop("GITLEAKS_CONFIG_TOML", None)
        result = subprocess.run(
            [
                binary,
                "stdin",
                "--config",
                str(rules),
                "--ignore-gitleaks-allow",
                "--gitleaks-ignore-path",
                str(rules.parent),
                "--no-banner",
                "--no-color",
                "--redact",
                "--exit-code",
                "9",
                "--timeout",
                "15",
            ],
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            env=env,
        )
        if result.returncode == 9:
            raise RecordError("SCAN_FINDING")
        if result.returncode != 0:
            raise ValueError()
        return {
            "state": "passed",
            "content_sha256": sha(raw),
            "policy_sha256": sha(policy.read_bytes()),
            "rules_sha256": sha(rules.read_bytes()),
            "coverage": "gitleaks+fallback+personal_path",
            "pii_enabled": bool(engine.pii_available),
        }
    except RecordError:
        raise
    except Exception:
        raise RecordError("SCAN_INCOMPLETE") from None
