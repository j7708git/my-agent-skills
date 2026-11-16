import copy, pathlib, sys, unittest
from unittest.mock import patch

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import core, git_store, recovery, test_git_store


class Tests(unittest.TestCase):
    def setUp(self):
        self.f = test_git_store.TestGitStore("test_round_trip_preview_apply_replay")
        self.f.setUp()
        tasks = self.f.project / "tasks"
        tasks.mkdir()
        self.one = tasks / "one.md"
        self.two = tasks / "two.md"
        self.one.write_bytes(b"one")
        self.two.write_bytes(b"two")
        self.req = copy.deepcopy(self.f.req)
        self.req.update(operation_id="batch", action="batch", target_record_id=None)
        self.req["scope"]["actions"] = ["batch", "replace"]
        self.req["scope"]["records"] = [
            {"record_id": "one", "path": "tasks/one.md", "fields": ["body"]},
            {"record_id": "two", "path": "tasks/two.md", "fields": ["body"]},
        ]
        self.req["payload"] = {
            "changes": [
                {
                    "action": "replace",
                    "target_record_id": n,
                    "payload": {
                        "content": n.upper(),
                        "base_sha256": core.sha(n.encode()),
                    },
                }
                for n in ("one", "two")
            ]
        }
        self.plan = git_store.preview(self.f.project, self.f.runtime, self.req, {})
        real = git_store.os.replace

        def fail(src, dst):
            if pathlib.Path(dst) == self.two:
                raise OSError("injected")
            return real(src, dst)

        with patch.object(git_store.os, "replace", side_effect=fail):
            with self.assertRaisesRegex(core.RecordError, "PARTIAL"):
                git_store.apply(
                    self.f.project,
                    self.f.runtime,
                    self.req,
                    {},
                    self.plan["plan"],
                    self.plan["plan_hash"],
                )

    def tearDown(self):
        self.f.tearDown()

    def execute(self):
        return recovery.recover(
            self.f.project,
            self.f.runtime,
            self.req,
            {},
            self.plan["plan"],
            self.plan["plan_hash"],
        )

    def test_resume_and_replay(self):
        result = self.execute()
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(self.one.read_bytes(), b"ONE")
        self.assertEqual(self.two.read_bytes(), b"TWO")
        self.assertEqual(self.execute()["outcome"], "no_write")
        self.assertEqual(git_store.get_control(self.f.runtime)["version"], 1)

    def test_external_change_not_overwritten(self):
        self.one.write_bytes(b"human")
        with self.assertRaisesRegex(core.RecordError, "REVISION_CONFLICT"):
            self.execute()
        self.assertEqual(self.one.read_bytes(), b"human")
        self.assertEqual(self.two.read_bytes(), b"two")


if __name__ == "__main__":
    unittest.main()
