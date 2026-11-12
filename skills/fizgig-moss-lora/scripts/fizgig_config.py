#!/usr/bin/env python3
"""Inspect Fizgig source and prepare local MOSS LoRA run bundles; never train."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import re
import stat
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    tomllib = None


AUDITED_COMMIT = "f1d1bb21a61654d9fc1263ce233f5a04dcaa1af9"
SCRIPTS = {"cache": "src/fizgig/families/cache.py", "train": "src/fizgig/families/train.py"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".avif"}
MUON_ARGS = "adjust_lr_fn=match_rms_adamw weight_decay=0.01 momentum=0.95 nesterov=True ns_steps=5"


class ConfigError(ValueError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def resolved(value, *, file=False, directory=False):
    p = Path(value).expanduser().resolve()
    if file and not p.is_file():
        raise ConfigError(f"檔案不存在：{p}")
    if directory and not p.is_dir():
        raise ConfigError(f"目錄不存在：{p}")
    return p


def no_links(value):
    """Refuse redirected output paths, including Windows junctions."""
    path = Path(value).expanduser().absolute()
    for part in (path, *path.parents):
        try:
            attributes = getattr(part.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        if part.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ConfigError(f"路徑不接受 symlink 或 junction：{part}")


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def git_read(root, *args):
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"無法讀取 Git 版本：{exc}") from exc
    if proc.returncode:
        raise ConfigError("無法讀取 Fizgig Git checkout；未確認版本")
    return proc.stdout.strip()


def extract_options(path):
    """Read literal argparse declarations without importing downloaded Python."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8-sig"))
    options = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        spec = {}
        for kw in node.keywords:
            if kw.arg not in {"action", "nargs", "choices", "required", "default", "type"}:
                continue
            if kw.arg == "type" and isinstance(kw.value, ast.Name):
                spec["type"] = kw.value.id
                continue
            try:
                spec[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, TypeError):
                # Computed choices/defaults remain explicitly unresolved.
                if kw.arg == "choices":
                    spec["choices_unresolved"] = True
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--"):
                options[arg.value] = spec
    return options


def inspect_checkout(root):
    root = resolved(root, directory=True)
    issues = []
    try:
        commit = git_read(root, "rev-parse", "HEAD")
        changes = git_read(root, "status", "--porcelain", "--untracked-files=all", "--", "src", "requirements.txt")
    except ConfigError as exc:
        commit, changes = None, ""
        issues.append(str(exc))
    if commit != AUDITED_COMMIT:
        issues.append("版本不在已核對範圍；先對照現行源碼與 preset，不套用舊配方")
    if changes:
        issues.append("Fizgig src 或 requirements 有本機修改；需重新核對")
    options, hashes = {}, {}
    for name, relative in SCRIPTS.items():
        try:
            path = root / relative
            options[name] = extract_options(path)
            hashes[relative] = digest(path)
        except (OSError, SyntaxError, UnicodeError) as exc:
            options[name] = {}
            issues.append(f"無法解析 {relative}：{exc}")
    return {"schema_version": 1, "status": "needs-review" if issues else "audited",
            "root": str(root), "commit": commit, "audited_commit": AUDITED_COMMIT,
            "issues": issues, "source_sha256": hashes, "cli": options}


def validate_argv(argv, options):
    if not isinstance(argv, list) or len(argv) < 2 or any(not isinstance(x, str) for x in argv):
        return ["argv 必須是至少含 interpreter 與 script 的字串陣列"]
    issues, seen, i = [], set(), 2
    while i < len(argv):
        flag = argv[i]
        spec = options.get(flag)
        if spec is None:
            issues.append(f"未支援的旗標：{flag}")
            i += 1
            continue
        if flag in seen and spec.get("action") != "append":
            issues.append(f"重複的旗標：{flag}")
        seen.add(flag)
        if spec.get("action") in {"store_true", "store_false", "count"}:
            i += 1
            continue
        count = spec.get("nargs", 1)
        if not isinstance(count, int):
            issues.append(f"此 helper 未處理的參數 arity：{flag}")
            break
        values = argv[i + 1:i + 1 + count]
        if len(values) != count or any(v.startswith("--") for v in values):
            issues.append(f"旗標缺值：{flag}")
            i += 1
            continue
        for value in values:
            if "choices" in spec and value not in spec["choices"]:
                issues.append(f"不支援的選項值：{flag}={value}")
            try:
                if spec.get("type") == "int":
                    int(value)
                elif spec.get("type") == "float" and not math.isfinite(float(value)):
                    raise ValueError()
            except ValueError:
                issues.append(f"不正確的數值：{flag}={value}")
        i += 1 + count
    for flag, spec in options.items():
        if spec.get("required") and flag not in seen:
            issues.append(f"缺少必要旗標：{flag}")
    return issues


def scan_dataset(folder, trigger):
    """Check a flat, curated input; visual quality remains a separate review."""
    records, stems, hashes = [], set(), set()
    for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if path.is_dir():
            raise ConfigError("訓練輸入須為乾淨的平面目錄；先整理子目錄與 holdout")
        if path.is_symlink():
            raise ConfigError(f"訓練輸入不接受 symlink：{path.name}")
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            if path.suffix.lower() != ".txt":
                raise ConfigError(f"訓練輸入含非圖文檔：{path.name}")
            continue
        stem = path.stem.casefold()
        if path.name.startswith("_") or any(x in stem for x in ("contact_sheet", "contact-sheet", "contactsheet", "-masklabel")):
            raise ConfigError(f"基準輸入含輔助圖或 mask：{path.name}")
        if stem in stems:
            raise ConfigError(f"大小寫或圖片 stem 衝突：{path.name}")
        stems.add(stem)
        caption_path = path.with_suffix(".txt")
        if not caption_path.is_file() or caption_path.is_symlink():
            raise ConfigError(f"缺少普通同名 caption：{path.name}")
        caption = caption_path.read_text(encoding="utf-8-sig").strip()
        if not caption:
            raise ConfigError(f"空 caption：{caption_path.name}")
        if not re.search(r"(?<![\w-])" + re.escape(trigger) + r"(?![\w-])", caption, re.IGNORECASE):
            raise ConfigError(f"caption 未含觸發詞：{caption_path.name}")
        image_hash = digest(path)
        if image_hash in hashes:
            raise ConfigError(f"完全重複圖片：{path.name}")
        hashes.add(image_hash)
        records.append({"image": path.name, "sha256": image_hash,
                        "caption_sha256": digest(caption_path)})
    if not records:
        raise ConfigError("沒有可訓練圖文配對")
    caption_stems = {p.stem.casefold() for p in folder.iterdir() if p.suffix.lower() == ".txt"}
    if caption_stems != stems:
        raise ConfigError("輸入含沒有對應圖片的 caption")
    return records


def step_counts(n, parameters):
    exposures = n * parameters["repeats"]
    batch = parameters["batch_size"]
    accum = parameters["accumulation"]
    # Without decoded dimensions, batch > 1 cannot determine bucket-tail counts.
    low = math.ceil(exposures / batch)
    high = exposures
    return {"images": n, "exposures_per_epoch": exposures,
            "exact": batch == 1,
            "micro_batches_per_epoch": low if batch == 1 else None,
            "micro_batches_bounds": [low, high],
            "updates_per_epoch": math.ceil(low / accum) if batch == 1 else None,
            "total_updates": parameters["epochs"] * math.ceil(low / accum) if batch == 1 else None,
            "total_updates_bounds": [parameters["epochs"] * math.ceil(low / accum),
                                     parameters["epochs"] * math.ceil(high / accum)],
            "note": "batch > 1 須以 Fizgig bucket log 重算；epoch 尾端 accumulation 會 flush"}


def check_parameters(p):
    for key in ("epochs", "rank", "batch_size", "accumulation", "repeats"):
        if type(p[key]) is not int or p[key] < 1:
            raise ConfigError(f"{key} 必須是正整數")
    for key in ("lr", "alpha"):
        if isinstance(p[key], bool) or not isinstance(p[key], (int, float)) or not math.isfinite(p[key]) or p[key] <= 0:
            raise ConfigError(f"{key} 必須是有限正數")
    if len(p["resolution"]) != 2 or any(type(v) is not int or v <= 0 or v % 16 for v in p["resolution"]):
        raise ConfigError("解析度必須是兩個正的 16 倍數")
    if p["profile"] not in {"baseline", "standard"}:
        raise ConfigError("未知 profile")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", p["trigger"]):
        raise ConfigError("觸發詞需為 2–64 字元的小寫英文、數字、底線或連字號，英文開頭")
    if p["class_word"] not in {"person", "woman", "man"}:
        raise ConfigError("未知類別詞")


def dataset_toml(dataset, cache, p):
    q = lambda value: json.dumps(str(value), ensure_ascii=False)
    return ("[general]\n"
            f"resolution = [{p['resolution'][0]}, {p['resolution'][1]}]\n"
            'caption_extension = ".txt"\n'
            f"batch_size = {p['batch_size']}\nnum_repeats = {p['repeats']}\n"
            "enable_bucket = true\nbucket_no_upscale = true\n\n[[datasets]]\n"
            f"image_directory = {q(dataset)}\ncache_directory = {q(cache)}\n")


def prompts(p):
    subject = f"{p['trigger']}, a {p['class_word']}"
    return "\n".join([
        f"A close-up photo of {subject}, facing the camera with a neutral expression, plain gray wall, soft daylight.",
        f"A waist-up photo of {subject}, wearing a yellow raincoat, sitting at a wooden table, looking to the side.",
        f"A full-body photo of {subject}, wearing a white shirt and blue jeans, walking on a quiet beach.",
        f"A profile photo of {subject}, laughing with hair tied back, outdoors in soft afternoon light.",
        f"A waist-up photo of {subject}, wearing a dark formal jacket with a serious expression, in a busy train station.",
        f"A full-body photo of {subject}, raising one arm, wearing a green sports outfit, in a bright studio.",
    ]) + "\n"


def make_commands(root, python, paths, p):
    dataset_config = str(Path(paths["bundle"]) / "dataset.toml")
    cache_script, train_script = (str(root / SCRIPTS[k]) for k in ("cache", "train"))
    commands = []
    for stage, model in (("latents", paths["vae"]), ("text", paths["text_encoder"])):
        commands.append({"stage": stage, "argv": [python, cache_script, "--family", "moss_image",
                         "--stage", stage, "--dataset_config", dataset_config, "--model", model]})
    train = [python, train_script, "--family", "moss_image", "--dataset_config", dataset_config,
             "--dit", paths["dit"], "--output_dir", paths["output"], "--output_name", p["trigger"],
             "--network_type", "lora", "--network_dim", str(p["rank"]), "--network_alpha", str(p["alpha"]),
             "--learning_rate", str(p["lr"]), "--optimizer_type", "muon", "--optimizer_args", MUON_ARGS,
             "--lr_scheduler", "constant", "--lr_warmup_steps", "0", "--max_train_epochs", str(p["epochs"]),
             "--save_every_n_epochs", "1", "--gradient_accumulation_steps", str(p["accumulation"]),
             "--max_grad_norm", "1.0", "--precision", "auto", "--blocks_to_swap", "-1",
             "--gradient_checkpointing", "on", "--compile_blocks", "auto", "--ema_decay", "0.98",
             "--caption_dropout", "0", "--seed", "42", "--family_option", "lownoise_pct=11:7:40:8:3",
             "--save_state", "--save_state_on_train_end", "--keep_last_n_states", "2",
             "--vae", paths["vae"], "--text_encoder", paths["text_encoder"],
             "--preview_checkpoint", paths["preview_checkpoint"],
             "--sample_prompts", str(Path(paths["bundle"]) / "sample_prompts.txt"),
             "--sample_at_first", "--sample_every_n_epochs", "5", "--sample_seed", "17",
             "--sample_width", "1024", "--sample_height", "1024", "--sample_steps", "30",
             "--sample_cfg_scale", "4", "--sample_negative", "worst quality, bad anatomy, watermark, jpeg artifact",
             "--trigger_word", p["trigger"]]
    if p["profile"] == "standard":
        train += ["--face_weight", "9", "--face_area", "head", "--face_cycle", "3:1", "--face_until", "12:600"]
    commands.append({"stage": "train", "argv": train})
    return commands


def make_plan(args):
    info = inspect_checkout(args.fizgig_root)
    if info["status"] != "audited":
        raise ConfigError("；".join(info["issues"]))
    p = {key: getattr(args, key) for key in ("profile", "epochs", "rank", "alpha", "lr", "batch_size",
                                           "accumulation", "repeats", "resolution", "trigger", "class_word")}
    check_parameters(p)
    dataset = resolved(args.dataset, directory=True)
    no_links(args.destination)
    bundle = resolved(args.destination)
    if bundle.exists() or bundle.is_symlink():
        raise ConfigError("目的地已存在；請給新的 run bundle 目錄")
    if not bundle.parent.is_dir():
        raise ConfigError("目的地的父目錄必須先存在")
    if overlaps(bundle, dataset):
        raise ConfigError("run bundle 與訓練資料目錄不可重疊")
    python = str(resolved(args.python, file=True))
    paths = {"dataset": str(dataset), "bundle": str(bundle), "cache": str(bundle / "cache"),
             "output": str(bundle / "checkpoints")}
    model_stats = {}
    for key in ("dit", "text_encoder", "vae", "preview_checkpoint"):
        path = resolved(getattr(args, key), file=True)
        if overlaps(path, bundle):
            raise ConfigError("run bundle 不可包含或覆蓋模型輸入")
        paths[key] = str(path)
        stat = path.stat()
        model_stats[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if "sft" in Path(paths["dit"]).name.casefold():
        raise ConfigError("這個建構器使用 Base 訓練；SFT 訓練需另核對 attention-only 配方")
    if any(tag in Path(paths["text_encoder"]).name.casefold() for tag in ("gptq", "w4a8")):
        raise ConfigError("此版不支援 GPTQ／w4a8 TE；請使用已核對的編碼器")
    records = scan_dataset(dataset, p["trigger"])
    commands = make_commands(Path(info["root"]), python, paths, p)
    for command in commands:
        kind = "train" if command["stage"] == "train" else "cache"
        issues = validate_argv(command["argv"], info["cli"][kind])
        if issues:
            raise ConfigError("；".join(issues))
    return {"schema_version": 1, "status": "planned", "cwd": info["root"], "python": python,
            "fizgig_commit": info["commit"], "source_sha256": info["source_sha256"],
            "profile": p["profile"], "parameters": p, "paths": paths, "commands": commands,
            "dataset_snapshot": records, "model_file_stats": model_stats,
            "steps": step_counts(len(records), p),
            "limitations": ["只做靜態檢查；未驗證模型權重內容、圖片品質、Python 依賴、GPU 或訓練成果",
                            "standard 保留作者 Element boost；epochs 預設 20 作試訓，可另設 70",
                            "模型以大小及 mtime 偵測變更，不是權重 hash；更換 TE 應建立新 bundle/cache",
                            "此檔案包含本機路徑，留在本機；不提交資料集或 run bundle"]}


def build(args):
    plan = make_plan(args)
    if args.apply:
        destination = Path(plan["paths"]["bundle"])
        destination.mkdir(exist_ok=False)
        payloads = {"dataset.toml": dataset_toml(plan["paths"]["dataset"], plan["paths"]["cache"], plan["parameters"]),
                    "sample_prompts.txt": prompts(plan["parameters"])}
        plan["status"] = "prepared"
        payloads["run.json"] = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
        # Exclusive creation: a failed write leaves a new incomplete bundle, never overwrites originals.
        for name, contents in payloads.items():
            with (destination / name).open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(contents)
    return plan


def validate_bundle(root, bundle):
    info = inspect_checkout(root)
    issues = list(info["issues"])
    no_links(bundle)
    folder = resolved(bundle, directory=True)
    plan = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise ConfigError("run.json 必須是 object")
    if plan.get("schema_version") != 1:
        raise ConfigError("不支援的 run.json schema")
    p, paths = plan["parameters"], plan["paths"]
    check_parameters(p)
    if plan["fizgig_commit"] != info["commit"] or plan["source_sha256"] != info["source_sha256"]:
        issues.append("Fizgig 版本或來源檔已改變；重新核對並產生 bundle")
    if plan["cwd"] != info["root"] or resolved(paths["bundle"]) != folder:
        issues.append("checkout 或 bundle 路徑已改變；重新產生 bundle")
    expected_paths = {"cache": folder / "cache", "output": folder / "checkpoints"}
    for value in expected_paths.values():
        no_links(value)
    if any(resolved(paths[k]) != v for k, v in expected_paths.items()):
        issues.append("cache 或 output 不在這個 bundle 的指定位置")
    dataset = resolved(paths["dataset"], directory=True)
    if overlaps(folder, dataset):
        issues.append("bundle 與資料集重疊")
    if scan_dataset(dataset, p["trigger"]) != plan["dataset_snapshot"]:
        issues.append("圖片或 caption 已變更；需建立新資料版本與快取")
    resolved(plan["python"], file=True)
    for key in ("dit", "text_encoder", "vae", "preview_checkpoint"):
        path = resolved(paths[key], file=True)
        stat = path.stat()
        if {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns} != plan["model_file_stats"][key]:
            issues.append(f"模型檔已變更：{key}；重建 bundle/cache")
    actual_toml = (folder / "dataset.toml").read_text(encoding="utf-8")
    if tomllib is None:
        raise ConfigError("TOML 驗證需要 Python 3.11 以上")
    actual = tomllib.loads(actual_toml)
    expected = tomllib.loads(dataset_toml(dataset, paths["cache"], p))
    if actual != expected:
        issues.append("dataset.toml 與 run.json 不一致；請重新 build")
    if (folder / "sample_prompts.txt").read_text(encoding="utf-8") != prompts(p):
        issues.append("sample prompts 與基準不一致；請記錄測試矩陣後重新建立配置")
    expected_commands = make_commands(Path(info["root"]), plan["python"], paths, p)
    if plan["commands"] != expected_commands:
        issues.append("argv 與基準參數不一致；調參請重新 build，不直接執行改寫的 argv")
    if not isinstance(plan["commands"], list):
        raise ConfigError("commands 必須是陣列")
    for command in plan["commands"]:
        if not isinstance(command, dict) or command.get("stage") not in {"latents", "text", "train"}:
            issues.append("commands 須含支援的 stage 與 argv")
            continue
        kind = "train" if command.get("stage") == "train" else "cache"
        issues.extend(validate_argv(command["argv"], info["cli"][kind]))
    return {"schema_version": 1, "status": "invalid" if issues else "static-valid",
            "issues": issues, "training_executed": False,
            "steps": step_counts(len(plan["dataset_snapshot"]), p)}


def parser():
    p = argparse.ArgumentParser(description="唯讀核對 Fizgig，或建立全新的 MOSS LoRA run bundle；不開訓")
    sub = p.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="AST 解析本機 checkout，不 import Fizgig")
    inspect.add_argument("--fizgig-root", required=True)
    b = sub.add_parser("build", help="預設 dry-run；--apply 才建立新 bundle")
    for arg in ("fizgig-root", "python", "dataset", "dit", "text-encoder", "vae", "preview-checkpoint", "trigger", "destination"):
        b.add_argument("--" + arg, required=True)
    b.add_argument("--profile", choices=("baseline", "standard"), default="baseline")
    b.add_argument("--class-word", choices=("person", "woman", "man"), default="person")
    for key, default in (("epochs", 20), ("rank", 32), ("batch-size", 1), ("accumulation", 1), ("repeats", 1)):
        b.add_argument("--" + key, type=int, default=default)
    b.add_argument("--alpha", type=float, default=32)
    b.add_argument("--lr", type=float, default=1e-4)
    b.add_argument("--resolution", type=int, nargs=2, default=[1024, 1024], metavar=("WIDTH", "HEIGHT"))
    b.add_argument("--apply", action="store_true")
    v = sub.add_parser("validate", help="核對 bundle、來源版本、資料指紋與 CLI 選項")
    v.add_argument("--fizgig-root", required=True)
    v.add_argument("--bundle", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_checkout(args.fizgig_root)
        elif args.command == "build":
            result = build(args)
        else:
            result = validate_bundle(args.fizgig_root, args.bundle)
        code = 2 if result["status"] == "needs-review" else 1 if result["status"] == "invalid" else 0
    except (ConfigError, OSError, ValueError, KeyError, TypeError) as exc:
        result, code = {"status": "error", "error": str(exc)}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
