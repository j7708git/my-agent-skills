"""Offline contract tests; no model weights, GPU work, or Fizgig imports."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib


SCRIPT = Path(__file__).resolve().parents[2] / "skills/fizgig-moss-lora/scripts/fizgig_config.py"
SPEC = importlib.util.spec_from_file_location("fizgig_config_tested", SCRIPT)
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


def png(value):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, value, 100, 200]))) + chunk(b"IEND", b""))


TRAIN_STRINGS = """family dataset_config dit output_dir output_name network_type optimizer_type
optimizer_args lr_scheduler precision gradient_checkpointing compile_blocks family_option vae
text_encoder preview_checkpoint sample_prompts sample_negative trigger_word face_area face_cycle face_until""".split()
TRAIN_INTS = """network_dim lr_warmup_steps max_train_epochs save_every_n_epochs
gradient_accumulation_steps blocks_to_swap seed keep_last_n_states sample_every_n_epochs
sample_seed sample_width sample_height sample_steps""".split()
TRAIN_FLOATS = "network_alpha learning_rate max_grad_norm ema_decay caption_dropout sample_cfg_scale face_weight".split()
TRAIN_BOOLEANS = "save_state save_state_on_train_end sample_at_first".split()


class ConfigContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / "checkout"
        source = self.checkout / "src/fizgig/families"
        source.mkdir(parents=True)
        # Invalid at runtime on purpose: inspect must only parse the AST.
        lines = ["raise RuntimeError('Downloaded source must not execute')"]
        for name in TRAIN_STRINGS:
            required = ", required=True" if name in {"family", "dit", "output_dir", "output_name"} else ""
            lines.append(f"parser.add_argument('--{name}'{required})")
        for name in TRAIN_INTS:
            lines.append(f"parser.add_argument('--{name}', type=int)")
        for name in TRAIN_FLOATS:
            lines.append(f"parser.add_argument('--{name}', type=float)")
        for name in TRAIN_BOOLEANS:
            lines.append(f"parser.add_argument('--{name}', action='store_true')")
        (source / "train.py").write_text("\n".join(lines), encoding="utf-8")
        (source / "cache.py").write_text("\n".join([
            "raise RuntimeError('Must not execute')",
            "parser.add_argument('--family', required=True, choices=['moss_image'])",
            "parser.add_argument('--stage', required=True, choices=['latents', 'text'])",
            "parser.add_argument('--dataset_config', required=True)",
            "parser.add_argument('--model', required=True)",
        ]), encoding="utf-8")
        self.dataset = self.root / "dataset's 人物"
        self.dataset.mkdir()
        for n in range(2):
            (self.dataset / f"image{n}.png").write_bytes(png(n))
            (self.dataset / f"image{n}.txt").write_text("A photo of mssperson, a person, wearing a jacket.", encoding="utf-8")
        self.models = {}
        for key in ("dit", "text_encoder", "vae", "preview_checkpoint"):
            path = self.root / f"{key} example.safetensors"
            path.write_bytes(b"synthetic model placeholder")
            self.models[key] = str(path)
        self.commit = config.AUDITED_COMMIT
        self.changes = ""
        patcher = patch.object(config, "git_read", side_effect=self.fake_git)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_git(self, root, *args):
        return self.commit if args == ("rev-parse", "HEAD") else self.changes

    def arguments(self, **overrides):
        args = config.parser().parse_args([
            "build", "--fizgig-root", str(self.checkout), "--python", sys.executable,
            "--dataset", str(self.dataset), "--dit", self.models["dit"],
            "--text-encoder", self.models["text_encoder"], "--vae", self.models["vae"],
            "--preview-checkpoint", self.models["preview_checkpoint"], "--trigger", "mssperson",
            "--destination", str(self.root / "new run's 人物"),
        ])
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def prepared(self, **overrides):
        return config.build(self.arguments(apply=True, **overrides))

    def test_inspection_reads_ast_without_executing_source(self):
        result = config.inspect_checkout(self.checkout)
        self.assertEqual(result["status"], "audited")
        self.assertEqual(result["cli"]["cache"]["--stage"]["choices"], ["latents", "text"])
        self.assertEqual(result["cli"]["train"]["--network_dim"]["type"], "int")

    def test_unresolved_choices_are_marked_not_executed(self):
        source = self.checkout / config.SCRIPTS["cache"]
        source.write_text("parser.add_argument('--family', choices=get_family_names())", encoding="utf-8")
        self.assertTrue(config.extract_options(source)["--family"]["choices_unresolved"])

    def test_new_version_blocks_writing_but_still_inspects(self):
        self.commit = "0" * 40
        self.assertEqual(config.inspect_checkout(self.checkout)["status"], "needs-review")
        with self.assertRaises(config.ConfigError):
            self.prepared()
        self.assertFalse(Path(self.arguments().destination).exists())

    def test_dirty_source_blocks_recipe(self):
        self.changes = " M src/fizgig/families/train.py"
        with self.assertRaises(config.ConfigError):
            self.prepared()

    def test_missing_git_information_stays_needs_review(self):
        with patch.object(config, "git_read", side_effect=config.ConfigError("unavailable")):
            result = config.inspect_checkout(self.checkout)
        self.assertEqual(result["status"], "needs-review")
        self.assertIsNone(result["commit"])

    def test_dry_run_produces_argv_without_writes(self):
        plan = config.build(self.arguments())
        self.assertEqual(plan["status"], "planned")
        self.assertFalse(Path(plan["paths"]["bundle"]).exists())
        self.assertEqual([c["stage"] for c in plan["commands"]], ["latents", "text", "train"])
        self.assertTrue(all(isinstance(c["argv"], list) for c in plan["commands"]))
        self.assertEqual(plan["steps"]["total_updates"], 40)
        self.assertNotIn("--max_train_steps", plan["commands"][-1]["argv"])

    def test_new_bundle_roundtrips_toml_chinese_and_spaces(self):
        plan = self.prepared()
        folder = Path(plan["paths"]["bundle"])
        data = config.tomllib.loads((folder / "dataset.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["datasets"][0]["image_directory"], str(self.dataset))
        self.assertEqual(data["datasets"][0]["cache_directory"], str(folder / "cache"))
        self.assertEqual(config.validate_bundle(self.checkout, folder)["status"], "static-valid")
        self.assertEqual(set(p.name for p in folder.iterdir()), {"dataset.toml", "run.json", "sample_prompts.txt"})
        self.assertIn(str(folder / "dataset.toml"), plan["commands"][0]["argv"])

    def test_toml_escapes_quotes_backslashes_and_controls(self):
        path = 'C:\\example "quoted"\\人物\tline\n'
        actual = config.tomllib.loads(config.dataset_toml(path, path + "cache", vars(self.arguments())))
        self.assertEqual(actual["datasets"][0]["image_directory"], path)

    def test_build_never_overwrites_existing_bundle(self):
        plan = self.prepared()
        before = (Path(plan["paths"]["bundle"]) / "run.json").read_bytes()
        with self.assertRaises(config.ConfigError):
            self.prepared()
        self.assertEqual((Path(plan["paths"]["bundle"]) / "run.json").read_bytes(), before)

    def test_profile_difference_is_explicit(self):
        base = config.build(self.arguments())
        standard = config.build(self.arguments(profile="standard"))
        self.assertEqual(base["parameters"]["epochs"], 20)
        self.assertEqual(standard["parameters"]["epochs"], 20)
        self.assertNotIn("--face_weight", base["commands"][-1]["argv"])
        self.assertEqual(standard["commands"][-1]["argv"][-8:],
                         ["--face_weight", "9", "--face_area", "head", "--face_cycle", "3:1", "--face_until", "12:600"])

    def test_invalid_values_and_path_overlap_refused(self):
        for override in ({"epochs": 0}, {"rank": -1}, {"alpha": 0}, {"lr": float("nan")},
                         {"lr": float("inf")}, {"repeats": 0}, {"accumulation": 0},
                         {"batch_size": 0}, {"resolution": [1023, 1024]}, {"trigger": "space word"},
                         {"destination": str(self.dataset / "run")}):
            with self.subTest(override=override), self.assertRaises(config.ConfigError):
                config.build(self.arguments(**override))

    def test_all_model_and_python_paths_must_exist(self):
        for key in ("python", "dit", "text_encoder", "vae", "preview_checkpoint"):
            with self.subTest(key=key), self.assertRaises(config.ConfigError):
                config.build(self.arguments(**{key: str(self.root / "missing")}))

    def test_unsupported_model_recipe_names_refused(self):
        for key, name in (("dit", "MOSS-SFT.safetensors"), ("text_encoder", "Qwen-GPTQ.safetensors")):
            path = self.root / name
            path.write_bytes(b"placeholder")
            with self.subTest(key=key), self.assertRaises(config.ConfigError):
                config.build(self.arguments(**{key: str(path)}))

    def test_unknown_flags_and_invalid_choices_reported(self):
        options = config.inspect_checkout(self.checkout)["cli"]["cache"]
        argv = ["python", "cache.py", "--family", "moss_image", "--stage", "unknown",
                "--dataset_config", "data.toml", "--model", "model", "--max_train_steps", "800"]
        issues = config.validate_argv(argv, options)
        self.assertTrue(any("--max_train_steps" in x for x in issues))
        self.assertTrue(any("--stage=unknown" in x for x in issues))
        self.assertTrue(config.validate_argv(["python", "cache.py", "--stage"], options))
        self.assertTrue(config.validate_argv(["python", "cache.py", 3], options))

    def test_missing_supported_flag_blocks_build(self):
        path = self.checkout / config.SCRIPTS["train"]
        path.write_text(path.read_text(encoding="utf-8").replace("'--network_dim'", "'--other_dim'"), encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.build(self.arguments())

    def test_step_counts_are_exact_only_without_bucket_uncertainty(self):
        parameters = vars(self.arguments(epochs=20, repeats=1, batch_size=1, accumulation=1))
        self.assertEqual(config.step_counts(40, parameters)["total_updates"], 800)
        parameters.update(repeats=3, accumulation=7)
        self.assertEqual(config.step_counts(40, parameters)["total_updates"], 360)
        parameters.update(batch_size=4)
        steps = config.step_counts(40, parameters)
        self.assertFalse(steps["exact"])
        self.assertIsNone(steps["total_updates"])
        self.assertEqual(steps["micro_batches_bounds"], [30, 120])

    def test_dataset_change_invalidates_cache_plan(self):
        plan = self.prepared()
        (self.dataset / "image0.txt").write_text("mssperson, changed caption", encoding="utf-8")
        result = config.validate_bundle(self.checkout, plan["paths"]["bundle"])
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("caption" in x for x in result["issues"]))

    def test_model_or_source_change_invalidates_plan(self):
        plan = self.prepared()
        Path(self.models["text_encoder"]).write_bytes(b"changed model placeholder contents")
        path = self.checkout / config.SCRIPTS["train"]
        path.write_text(path.read_text(encoding="utf-8") + "\n# changed", encoding="utf-8")
        result = config.validate_bundle(self.checkout, plan["paths"]["bundle"])
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("text_encoder" in x for x in result["issues"]))
        self.assertTrue(any("Fizgig" in x for x in result["issues"]))

    def test_edited_toml_or_argv_cannot_silently_validate(self):
        plan = self.prepared()
        folder = Path(plan["paths"]["bundle"])
        toml = folder / "dataset.toml"
        toml.write_text(toml.read_text(encoding="utf-8").replace("batch_size = 1", "batch_size = 7"), encoding="utf-8")
        plan["commands"][-1]["argv"] += ["--invented_flag", "yes"]
        (folder / "run.json").write_text(json.dumps(plan), encoding="utf-8")
        result = config.validate_bundle(self.checkout, folder)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("dataset.toml" in x for x in result["issues"]))
        self.assertTrue(any("--invented_flag" in x for x in result["issues"]))

    def test_modified_sampling_or_cache_path_invalidates_plan(self):
        plan = self.prepared()
        folder = Path(plan["paths"]["bundle"])
        (folder / "sample_prompts.txt").write_text("an unrelated prompt", encoding="utf-8")
        plan["paths"]["cache"] = str(self.dataset)
        (folder / "run.json").write_text(json.dumps(plan), encoding="utf-8")
        result = config.validate_bundle(self.checkout, folder)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("cache" in x for x in result["issues"]))
        self.assertTrue(any("sample prompts" in x for x in result["issues"]))

    def test_duplicate_auxiliary_and_missing_captions_refused(self):
        for name in ("contact_sheet.png", "contact-sheet.png", "contactsheet.png", "_preview.png", "face-masklabel.png"):
            path = self.dataset / name
            path.write_bytes(png(3))
            with self.subTest(name=name), self.assertRaises(config.ConfigError):
                config.scan_dataset(self.dataset, "mssperson")
            path.unlink()
        (self.dataset / "image1.png").write_bytes(png(0))
        with self.assertRaises(config.ConfigError):
            config.scan_dataset(self.dataset, "mssperson")
        (self.dataset / "image1.png").write_bytes(png(1))
        (self.dataset / "image1.txt").unlink()
        with self.assertRaises(config.ConfigError):
            config.scan_dataset(self.dataset, "mssperson")

    def test_missing_trigger_and_orphan_caption_refused(self):
        caption = self.dataset / "image0.txt"
        caption.write_text("msspersonextra is not the trigger", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.scan_dataset(self.dataset, "mssperson")
        caption.write_text("mssperson, a person", encoding="utf-8")
        (self.dataset / "orphan.txt").write_text("mssperson", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.scan_dataset(self.dataset, "mssperson")

    def test_symlink_destination_parent_refused(self):
        target = self.root / "redirected"
        target.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("This platform account cannot create directory symlinks")
        with self.assertRaises(config.ConfigError):
            config.build(self.arguments(destination=str(link / "run")))
        self.assertFalse((target / "run").exists())

    def test_inspection_cli_and_error_exit_codes(self):
        self.commit = "1" * 40
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = config.main(["inspect", "--fizgig-root", str(self.checkout)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "needs-review")
        self.commit = config.AUDITED_COMMIT
        plan = self.prepared()
        folder = Path(plan["paths"]["bundle"])
        (folder / "run.json").write_text("[]", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = config.main(["validate", "--fizgig-root", str(self.checkout), "--bundle", str(folder)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")


if __name__ == "__main__":
    unittest.main()
