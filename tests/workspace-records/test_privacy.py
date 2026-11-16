import json
import pathlib
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import core
import privacy


class TestPrivacy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        policy = root / "policy.json"
        policy.write_text(
            json.dumps(
                {"schemaVersion": "1.0", "paths": {"personalPathSeverity": "high"}}
            ),
            encoding="utf-8",
        )
        self.config = {
            "gitleaks": shutil.which("gitleaks") or "missing",
            "policy": str(policy),
            "gitleaks_config": str(SCRIPTS.parents[2] / ".gitleaks.toml"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_content_digest_and_clean(self):
        payload = {"content": "合成 UTF-8"}
        receipt = privacy.check(payload, self.config)
        self.assertEqual(receipt["content_sha256"], core.sha(core.canonical(payload)))
        self.assertEqual(receipt["state"], "passed")

    def test_secret_and_personal_path_refused(self):
        for item in (
            "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyzAB",
            "C:" + chr(92) + "Users" + chr(92) + "person" + chr(92) + "x",
        ):
            with (
                self.subTest(),
                self.assertRaisesRegex(core.RecordError, "SCAN_FINDING"),
            ):
                privacy.check({"content": item}, self.config)

    def test_isolated_copy_requires_sibling_scanner(self):
        root = pathlib.Path(self.tmp.name) / "isolated"
        scripts = root / "skills" / "workspace-records" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("core.py", "privacy.py"):
            shutil.copyfile(SCRIPTS / name, scripts / name)
        program = """import json,sys
sys.path.insert(0,sys.argv[1])
import privacy
try:
    result=privacy.check({'content':'synthetic safe'},json.loads(sys.argv[2]))
    print(result['state'])
except Exception as exc:
    print(getattr(exc,'code','unexpected'))
"""
        args = [sys.executable, "-c", program, str(scripts), json.dumps(self.config)]
        missing = subprocess.run(args, capture_output=True, text=True, timeout=20)
        self.assertEqual(missing.stdout.strip(), "SCAN_INCOMPLETE")
        scanner = root / "skills" / "repo-privacy-audit" / "scripts"
        scanner.mkdir(parents=True)
        shutil.copyfile(
            SCRIPTS.parents[1] / "repo-privacy-audit" / "scripts" / "audit_core.py",
            scanner / "audit_core.py",
        )
        present = subprocess.run(args, capture_output=True, text=True, timeout=20)
        self.assertEqual(present.stdout.strip(), "passed", present.stderr)

    def test_missing_scanner_or_sibling_fails_closed(self):
        cfg = dict(self.config)
        cfg["gitleaks"] = "absent-scanner-candidate"
        with self.assertRaisesRegex(core.RecordError, "SCAN_INCOMPLETE"):
            privacy.check({"content": "safe"}, cfg)
        with (
            patch.object(
                privacy.importlib.util, "spec_from_file_location", side_effect=OSError()
            ),
            self.assertRaisesRegex(core.RecordError, "SCAN_INCOMPLETE"),
        ):
            privacy.check({"content": "safe"}, self.config)


if __name__ == "__main__":
    unittest.main()
