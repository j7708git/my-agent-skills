import copy, pathlib, sys, tempfile, unittest

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "skills/workspace-records/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import core, google_docs, portfolio, routing
from test_core import STATUS
from test_google_docs import doc, head


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.projects = self.root / "project-notes/projects"
        self.projects.mkdir(parents=True)
        (self.projects / "README.md").write_text("project index", encoding="utf-8")
        self.ids = ["Git", "Cloud", "Legacy"] + [f"Extra{i:02}" for i in range(15)]
        for name in self.ids:
            (self.projects / name).mkdir()
        (self.projects / "Git/status.md").write_bytes(STATUS)
        (self.projects / "Cloud/status.md").write_text(
            "ARCHIVED_SECRET_MARKER", encoding="utf-8"
        )
        (self.projects / "Legacy/progress.md").write_text(
            "# old progress", encoding="utf-8"
        )
        (self.root / routing.RUNTIME).mkdir(parents=True)
        (self.root / routing.POLICY).write_text("{}", encoding="utf-8")
        git = {
            "classification": "unmigrated_git",
            "backend": "git_local",
            "route_phase": "steady",
            "locator": None,
        }
        cloud = {
            "classification": "managed",
            "backend": "google_head",
            "route_phase": "steady",
            "locator": {"document_id": "doc"},
        }
        data = {
            "schema_version": 1,
            "policy_version": "1",
            "generation": 1,
            "projects": {"Git": git, "Legacy": git, "Cloud": cloud},
        }
        (self.root / routing.REGISTRY).write_bytes(core.canonical(data))
        self.registry = routing.Registry(self.root)
        self.req = {"project_ids": self.ids, "google_observations": {}}

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_18_and_never_google_fallback(self):
        result = portfolio.collect(self.registry, self.req)
        self.assertEqual(len(result["rows"]), 18)
        rows = {r["project_id"]: r for r in result["rows"]}
        self.assertEqual(rows["Git"]["values"]["now"], "First")
        self.assertEqual(rows["Legacy"]["issues"], ["LEGACY_PROGRESS_UNVERIFIED"])
        self.assertEqual(rows["Cloud"]["issues"], ["GOOGLE_UNAVAILABLE"])
        self.assertEqual(rows["Extra00"]["issues"], ["NOT_REGISTERED"])
        self.assertNotIn("ARCHIVED_SECRET_MARKER", str(result))
        self.assertFalse(result["writes_performed"])
        self.assertEqual(result["inventory_hash"], core.object_hash(sorted(self.ids)))

    def test_fresh_google_head_and_untrusted_marker(self):
        document = doc()
        h = head()
        h["project_id"] = "Cloud"
        h["summary"]["now"] = "最新"

        def lines(values):
            return [
                {"paragraph": {"elements": [{"textRun": {"content": v + "\n"}}]}}
                for v in values
            ]

        document["tabs"][0]["body"]["content"][1:1] = lines([google_docs.head_line(h)])
        document["tabs"][1]["body"]["content"][1:1] = lines(
            google_docs.summary_lines(h)
        )
        req = copy.deepcopy(self.req)
        req["google_observations"]["Cloud"] = {
            "document": document,
            "head_tab_id": "machine",
            "summary_tab_id": "human",
            "fresh_read": True,
        }
        row = next(
            r
            for r in portfolio.collect(self.registry, req)["rows"]
            if r["project_id"] == "Cloud"
        )
        self.assertEqual(row["values"]["now"], "最新")
        self.assertEqual(row["source"]["revision"], "rev0")
        req["google_observations"]["Cloud"]["fresh_read"] = False
        self.assertEqual(
            next(
                r
                for r in portfolio.collect(self.registry, req)["rows"]
                if r["project_id"] == "Cloud"
            )["availability"],
            "unknown",
        )

    def test_scope_and_bad_status_or_registry(self):
        with self.assertRaises(core.RecordError):
            portfolio.collect(self.registry, {**self.req, "project_ids": self.ids[:-1]})
        (self.projects / "Git/status.md").write_bytes(b"# bad\nnow: hi\n")
        rows = {
            r["project_id"]: r
            for r in portfolio.collect(self.registry, self.req)["rows"]
        }
        self.assertEqual(rows["Git"]["issues"], ["STATUS_FORMAT_INVALID"])
        (self.root / routing.REGISTRY).write_bytes(b"bad")
        result = portfolio.collect(self.registry, self.req)
        self.assertTrue(all(row["availability"] == "unknown" for row in result["rows"]))

    def test_stable_read_does_not_create_runtime(self):
        a = portfolio.collect(self.registry, self.req)
        b = portfolio.collect(self.registry, self.req)
        self.assertEqual(a, b)
        self.assertEqual(list((self.root / routing.RUNTIME).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
