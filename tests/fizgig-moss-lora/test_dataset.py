"""資料集 helper 的可觀察行為與原檔保護；全部使用合成資料。"""

import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "skills/fizgig-moss-lora/scripts/dataset.py"
spec = importlib.util.spec_from_file_location("fizgig_dataset", SCRIPT)
dataset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dataset)


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.manifest = self.root / "selection.json"
        self.destination = self.root / "prepared"

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, name, marker=b"one", caption="A photograph of zyvra, a person."):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 640, 960) + marker)
        if caption is not None:
            path.with_suffix(".txt").write_text(caption, encoding="utf-8")
        return path

    def select(self, *items):
        self.manifest.write_text(json.dumps({"version": 1, "items": list(items)}), encoding="utf-8")

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, arguments)], capture_output=True, encoding="utf-8",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    def prepare(self, *extra):
        return self.run_cli("prepare", "--source", self.source, "--manifest", self.manifest,
                            "--destination", self.destination, *extra)

    def test_dry_run_has_no_writes_and_apply_copies_reviewed_split(self):
        original = self.image("session-a/portrait.png")
        self.image("session-b/profile.png", b"two", None)
        self.image("bad.png", b"three", None)
        self.select({"image": "session-a/portrait.png", "split": "train", "group": "a", "caption": "Reviewed caption."},
                    {"image": "session-b/profile.png", "split": "holdout", "group": "b"},
                    {"image": "bad.png", "split": "rejected", "group": "c", "reason": "Manual rejection."})
        before = {p.relative_to(self.source).as_posix(): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        dry_run = self.prepare()
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertEqual(json.loads(dry_run.stdout)["status"], "planned")
        self.assertFalse(self.destination.exists())
        applied = self.prepare("--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((self.destination / "train/portrait.png").read_bytes(), original.read_bytes())
        self.assertEqual((self.destination / "train/portrait.txt").read_text(), "Reviewed caption.\n")
        self.assertTrue((self.destination / "holdout/profile.png").exists())
        self.assertFalse((self.destination / "holdout/profile.txt").exists())
        self.assertEqual(json.loads((self.destination / "manifest.json").read_text())["status"], "prepared")
        after = {p.relative_to(self.source).as_posix(): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_existing_destination_is_unchanged(self):
        self.image("one.png")
        self.select({"image": "one.png", "split": "train", "group": "a"})
        self.destination.mkdir()
        sentinel = self.destination / "existing.txt"
        sentinel.write_text("keep")
        result = self.prepare("--apply")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertEqual(list(self.destination.iterdir()), [sentinel])

    def test_missing_caption_fails_before_writing(self):
        self.image("one.png", caption=None)
        self.select({"image": "one.png", "split": "train", "group": "a"})
        result = self.prepare("--apply")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.destination.exists())

    def test_empty_caption_fails(self):
        self.image("one.png", caption=" \n ")
        self.select({"image": "one.png", "split": "train", "group": "a"})
        self.assertEqual(self.prepare().returncode, 2)

    def test_later_invalid_item_prevents_all_copying(self):
        self.image("one.png")
        self.image("two.png", b"two", None)
        self.select({"image": "one.png", "split": "train", "group": "a"},
                    {"image": "two.png", "split": "train", "group": "b"})
        self.assertEqual(self.prepare("--apply").returncode, 2)
        self.assertFalse(self.destination.exists())

    def test_malformed_manifest_returns_structured_error(self):
        self.image("one.png")
        for split in ([], {}, None, 1):
            with self.subTest(split=split):
                self.select({"image": "one.png", "split": split, "group": "a"})
                result = self.prepare("--apply")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr)["status"], "error")
                self.assertFalse(self.destination.exists())
        self.manifest.write_text(json.dumps({"version": True, "items": []}), encoding="utf-8")
        self.assertEqual(self.prepare().returncode, 2)

    def test_path_traversal_and_absolute_are_rejected(self):
        self.image("one.png")
        for name in ("../outside.png", "/outside.png", "C:/outside.png", "sub\\one.png", "sub/../one.png"):
            with self.subTest(name=name):
                self.select({"image": name, "split": "train", "group": "a"})
                self.assertEqual(self.prepare("--apply").returncode, 2)
                self.assertFalse(self.destination.exists())

    def test_destination_overlap_is_rejected(self):
        self.image("one.png")
        self.select({"image": "one.png", "split": "train", "group": "a"})
        self.destination = self.source / "output"
        self.assertEqual(self.prepare("--apply").returncode, 2)
        self.assertFalse(self.destination.exists())

    def test_group_leakage_is_rejected(self):
        self.image("one.png")
        self.image("two.png", b"two")
        self.select({"image": "one.png", "split": "train", "group": "same-session"},
                    {"image": "two.png", "split": "holdout", "group": "same-session"})
        self.assertEqual(self.prepare().returncode, 2)

    def test_exact_duplicate_leakage_is_rejected(self):
        self.image("one.png")
        self.image("two.png")
        self.select({"image": "one.png", "split": "train", "group": "a"},
                    {"image": "two.png", "split": "holdout", "group": "b"})
        self.assertEqual(self.prepare().returncode, 2)

    def test_casefold_stem_collision_is_rejected(self):
        self.image("a/One.png")
        self.image("b/one.png", b"two")
        self.select({"image": "a/One.png", "split": "train", "group": "a"},
                    {"image": "b/one.png", "split": "train", "group": "b"})
        self.assertEqual(self.prepare().returncode, 2)

    def test_auxiliary_and_masks_are_not_training_images(self):
        for name in ("_contact_sheet.png", "one-masklabel.png"):
            with self.subTest(name=name):
                self.image(name)
                self.select({"image": name, "split": "train", "group": "a"})
                self.assertEqual(self.prepare().returncode, 2)

    def test_hash_locks_selected_content(self):
        self.image("one.png")
        self.select({"image": "one.png", "split": "train", "group": "a", "sha256": "0" * 64})
        self.assertEqual(self.prepare("--apply").returncode, 2)
        self.assertFalse(self.destination.exists())

    def test_symlink_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.source / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("This host cannot create symlinks")
        (outside / "one.png").write_bytes(b"outside")
        self.select({"image": "linked/one.png", "split": "train", "group": "a", "caption": "reviewed"})
        self.assertEqual(self.prepare("--apply").returncode, 2)
        self.assertFalse(self.destination.exists())

    def test_audit_reports_duplicates_captions_helpers_and_header_dimensions(self):
        self.image("one.png")
        self.image("duplicate.png", caption=None)
        self.image("empty.png", b"other", " ")
        self.image("_contact_sheet.png", b"sheet", None)
        self.image("one-masklabel.png", b"mask", None)
        (self.source / "notes.json").write_text("{}")
        result = self.run_cli("audit", "--source", self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        self.assertEqual(len(doc["exact_duplicates"]), 1)
        codes = {finding["code"] for finding in doc["findings"]}
        self.assertTrue({"caption_missing", "caption_empty", "auxiliary_image", "mask", "non_image_file"} <= codes)
        self.assertEqual(doc["items"][0]["dimensions"], [640, 960])
        self.assertNotIn(str(self.source), result.stdout)

    def test_audit_report_does_not_overwrite_or_enter_source(self):
        self.image("one.png")
        report = self.root / "report.json"
        report.write_text("keep")
        self.assertEqual(self.run_cli("audit", "--source", self.source, "--output", report).returncode, 2)
        self.assertEqual(report.read_text(), "keep")
        inside = self.source / "report.json"
        self.assertEqual(self.run_cli("audit", "--source", self.source, "--output", inside).returncode, 2)
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()
