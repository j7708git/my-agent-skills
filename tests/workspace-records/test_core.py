import pathlib
import sys
import unittest

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
from core import RecordError, ident, request_hash, status_patch, validate_request

STATUS = (
    "# Example\r\n\r\nschema: 1\r\nstate: active\r\nneeds: none\r\nnow: First\r\nnext: Next\r\nblocker: none\r\nupdated: 2026-01-01T12:00:00+08:00\r\ncode_dir: no-code\r\n\r\n## 需要你決定\r\n\r\n- [ ] One\r\n\r\n## Extra\r\nKeep | table\r\n"
).encode()


def request():
    return {
        "schema_version": 1,
        "request_id": "r1",
        "project_id": "Legacy_Project",
        "actor_role": "pm",
        "scope": {
            "project_id": "Legacy_Project",
            "actions": ["status_patch"],
            "records": [
                {"record_id": "status", "path": "status.md", "fields": ["now"]}
            ],
        },
        "operation_id": "op1",
        "action": "status_patch",
        "target_record_id": "status",
        "payload": {"fields": {"now": "Updated"}, "base_sha256": "a" * 64},
        "expected": {"mode": "git", "phase": "steady", "epoch": 1, "version": 0},
    }


class TestCore(unittest.TestCase):
    def test_uppercase_id_and_traversal(self):
        self.assertEqual(ident("Legacy_Project"), "Legacy_Project")
        for v in ("../bad", "a..b", "a/b", ""):
            with self.subTest(v=v), self.assertRaises(RecordError):
                ident(v)

    def test_hash_excludes_transport_ids_and_binds_role_scope(self):
        r = request()
        a = request_hash(r)
        r["operation_id"] = "op2"
        r["request_id"] = "r2"
        self.assertEqual(a, request_hash(r))
        r["scope"]["records"][0]["fields"].append("next")
        self.assertNotEqual(a, request_hash(r))

    def test_status_patch_preserves_unmanaged_sections_and_crlf(self):
        updated = status_patch(STATUS, {"now": "你好"})
        self.assertIn("now: 你好\r\n", updated.decode())
        self.assertIn("## Extra\r\nKeep | table\r\n", updated.decode())
        self.assertIn("updated: 2026-01-01T12:00:00+08:00", updated.decode())

    def test_unknown_header_and_invalid_needs_refused(self):
        with self.assertRaises(RecordError):
            status_patch(
                STATUS.replace(b"now: First", b"unknown: hidden\r\nnow: First"),
                {"now": "x"},
            )
        with self.assertRaises(RecordError):
            status_patch(STATUS, {"needs": "[]"})

    def test_role_and_scope_refused(self):
        r = request()
        r["actor_role"] = "reader"
        with self.assertRaises(RecordError):
            validate_request(r, True)
        r = request()
        r["payload"]["fields"] = {"next": "x"}
        with self.assertRaises(RecordError):
            validate_request(r, True)


if __name__ == "__main__":
    unittest.main()
