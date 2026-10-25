"""scripts/validate_writing.py 的契約測試。

最終檢查器只有 8 條機械規則；其餘規則已降為人工自檢，本檔不再測它們的命中，
只測「降級後不再被報」與人工邊界文件存在。

全部在暫存目錄使用合成資料；不讀寫真實 repo 的 Git index，也不含真實憑證或個資。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate_writing.py"
ASSETS = REPO_ROOT / "skills" / "writing-contract" / "assets"
TIMEOUT = 120

B1 = (
    "> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；"
    "安全邊界、授權與不可逆操作則以本檔為準。"
    "發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。"
)
BASIS = "`docs/other.md`"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git(repo: Path, *args: str) -> None:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Synthetic Test",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Test",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            "GIT_CONFIG_GLOBAL": str(repo / ".gitconfig-global"),
            "GIT_CONFIG_SYSTEM": str(repo / ".gitconfig-system"),
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        env=env,
        timeout=TIMEOUT,
        capture_output=True,
    )


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        timeout=TIMEOUT,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write(repo: Path, rel: str, text: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def a_doc(title: str, body: str, now: str = "讀完後回報結果。", basis: str = BASIS) -> str:
    return f"""# {title}

> **讀者**：熟悉 Git 的維護者。
> **一句話**：這份文件說明 {title}。
> **現在要做**：{now}
> **依據**：{basis}

{body}
"""


def other_doc() -> str:
    return a_doc("其他文件", "## 內容\n\n這是測試用的基準文件，內容保持簡短。\n")


def b_doc(body: str = "## 指令\n\n照著做並回報結果。\n") -> str:
    return f"{B1}\n\n# 指令檔\n\n{body}"


def make_repo(tmp: Path, *, commit: bool = False) -> Path:
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    write(repo, "docs/other.md", other_doc())
    write(repo, ".keep", "")
    git(repo, "init", "-q", ".")
    if commit:
        git(repo, "add", "-A")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic: 初始提交")
    return repo


def make_script_copy(root: Path) -> Path:
    copy_root = root / "checker"
    (copy_root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, copy_root / "scripts" / "validate_writing.py")
    shutil.copytree(ASSETS, copy_root / "skills" / "writing-contract" / "assets")
    return copy_root / "scripts" / "validate_writing.py"


def run_script(script: Path, repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


def run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


def run_json(repo: Path, *args: str) -> tuple[int, dict]:
    result = run(repo, "--format", "json", *args)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"JSON 輸出無法解析：{exc}\nstdout={result.stdout}\nstderr={result.stderr}") from exc
    return result.returncode, payload


def rules(payload: dict) -> list[str]:
    return [item["rule"] for item in payload.get("findings", [])]


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_repo(self.root)


class InputTableTests(BaseCase):
    def test_missing_or_empty_fillers_exits_2(self) -> None:
        for case in ("missing", "empty"):
            with self.subTest(case=case):
                script = make_script_copy(self.root / f"fillers-{case}")
                path = script.parent.parent / "skills" / "writing-contract" / "assets" / "fillers.md"
                if case == "missing":
                    path.unlink()
                else:
                    path.write_text("# 空表\n", encoding="utf-8")
                result = run_script(script, self.repo)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("fillers.md", result.stdout)

    def test_missing_optional_self_check_list_only_hints(self) -> None:
        script = make_script_copy(self.root / "optional")
        path = script.parent.parent / "skills" / "writing-contract" / "assets" / "terms.md"
        path.unlink()
        result = run_script(script, self.repo, "--class", "all")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("人工自檢清單缺少：terms.md", result.stdout)


class EmptyRepoBaseTests(BaseCase):
    def test_empty_repo_with_staged_file_is_checked(self) -> None:
        repo = make_repo(self.root / "emptystaged", commit=False)
        write(repo, "docs/new.md", "# 新文件\n\n沒有四欄。\n")
        git(repo, "add", "-A")
        result = run(repo, "--class", "a", "--format", "text")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("docs/new.md", result.stdout)
        self.assertIn("A1／檢查 1", result.stdout)

    def test_committed_repo_with_staged_file_is_checked(self) -> None:
        repo = make_repo(self.root / "staged", commit=True)
        write(repo, "docs/new.md", "# 新文件\n\n沒有四欄。\n")
        git(repo, "add", "docs/new.md")
        result = run(repo, "--class", "a", "--format", "text")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("docs/new.md", result.stdout)
        self.assertIn("A1／檢查 1", result.stdout)


class A1Tests(BaseCase):
    def test_four_fields_pass(self) -> None:
        write(self.repo, "docs/good.md", a_doc("正常文件", "## 內容\n\n這是正常說明。\n"))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertNotIn("A1", rules(payload))

    def test_missing_fields_are_caught(self) -> None:
        write(self.repo, "docs/bad.md", "# 壞文件\n\n## 內容\n\n沒有四欄。\n")
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertIn("A1", rules(payload))

    def test_placeholder_is_caught_alone(self) -> None:
        text = a_doc("佔位文件", "## 內容\n\n正常內容。\n", now="<要做的事>")
        write(self.repo, "docs/placeholder.md", text)
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        hits = [item for item in payload["findings"] if item["file"] == "docs/placeholder.md"]
        self.assertTrue(any(item["rule"] == "A1" and item["check"] == "2" for item in hits), hits)
        self.assertFalse(any(item["check"] in ("3", "4") for item in hits), hits)

    def test_short_values_and_bare_readme_are_allowed(self) -> None:
        write(self.repo, "README.md", a_doc("README", "## 內容\n\n正常說明。\n"))
        text = """# 短值文件

> **讀者**：初學者
> **一句話**：未知
> **現在要做**：回報
> **依據**：README

## 內容

這是正常內容。
"""
        write(self.repo, "docs/short.md", text)
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertFalse(any(item["file"] == "docs/short.md" and item["rule"] == "A1" for item in payload["findings"]))

    def test_bom_file_is_parsed(self) -> None:
        write(self.repo, "docs/bom.md", "\ufeff" + a_doc("BOM 文件", "## 內容\n\n正常內容。\n"))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertNotIn("A1", {item["rule"] for item in payload["findings"] if item["file"] == "docs/bom.md"})

    def test_basis_path_rules(self) -> None:
        write(self.repo, "scripts/real.sh", "#!/usr/bin/env bash\n")
        cases = {
            "missing": ("`docs/no-such-file.md`", True),
            "section": ("§1", False),
            "command": ("python3 scripts/run-checks.sh", False),
            "line-suffix": ("`scripts/real.sh:2`", False),
            "dot-path": ("`.claude/skills/x/SKILL.md`", True),
            "section-plus-missing": ("§1 `docs/no-such-file.md`", True),
            "slash-command": ("`/wish`", False),
            "decimal-section": ("`§6.3`", False),
            "single-file": ("`nofile.md`", False),
            "two-segment-missing": ("`scripts/nope.sh`", True),
        }
        for name, (basis, should_report) in cases.items():
            with self.subTest(case=name):
                write(self.repo, f"docs/basis-{name}.md", a_doc(f"依據 {name}", "## 內容\n\n正常內容。\n", basis=basis))
                code, payload = run_json(self.repo, "--class", "a")
                self.assertEqual(0, code)
                hit = any(
                    item["file"] == f"docs/basis-{name}.md" and item["rule"] == "A1" and item["check"] == "4"
                    for item in payload["findings"]
                )
                self.assertEqual(should_report, hit)

    def test_notes_root_only_resolves_project_notes_prefix(self) -> None:
        notes = self.root / "notes"
        write(notes, "tasks/x.md", "# 任務\n")
        write(self.repo, "docs/basis-notes.md", a_doc("notes 前綴", "## 內容\n\n正常內容。\n", basis="`tasks/x.md`"))
        code, payload = run_json(self.repo, "--class", "a", "--notes-root", str(notes))
        self.assertEqual(0, code)
        hit = any(
            item["file"] == "docs/basis-notes.md" and item["rule"] == "A1" and item["check"] == "4"
            for item in payload["findings"]
        )
        self.assertTrue(hit, payload["findings"])


class A3Tests(BaseCase):
    def test_predicates_do_not_false_positive(self) -> None:
        body = "## 狀態\n\nM3 修好了。\n\nM3 完成後開始測試。\n\nM3 已修好。\n"
        write(self.repo, "docs/predicate.md", a_doc("述詞文件", body))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertNotIn("A3", rules(payload))

    def test_new_token_patterns_are_caught(self) -> None:
        body = "## 狀態\n\n第 2 階段完成\n\nMilestone 2 done\n\nSprint 3 完成\n\nv2 完成\n"
        write(self.repo, "docs/tokens.md", a_doc("代號樣式", body))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertEqual(4, sum(1 for item in payload["findings"] if item["rule"] == "A3"))

    def test_narrow_form_needs_status_and_sentence_start(self) -> None:
        body = "## Phase 2\n\nM3\n\n見 M3 完成。\n\nPhase 2 已啟動\n"
        write(self.repo, "docs/narrow.md", a_doc("窄樣式", body))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        hits = [item for item in payload["findings"] if item["rule"] == "A3"]
        self.assertEqual(1, len(hits), hits)


class A5FileLengthTests(BaseCase):
    def test_long_file_is_caught_in_full_scan(self) -> None:
        lines = "\n".join(f"內容第 {index} 行" for index in range(1, 401))
        write(self.repo, "docs/long.md", a_doc("長文件", f"## 內容\n\n{lines}\n"))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertTrue(any(item["rule"] == "A5" and item["line"] == 401 for item in payload["findings"]))

    def test_long_file_is_caught_in_modified_file(self) -> None:
        repo = make_repo(self.root / "longmodified", commit=True)
        lines = "\n".join(f"內容第 {index} 行" for index in range(1, 401))
        write(repo, "docs/long.md", a_doc("長文件", f"## 內容\n\n{lines}\n"))
        git(repo, "add", "-A")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic: 長文件")
        with (repo / "docs/long.md").open("a", encoding="utf-8") as handle:
            handle.write("補充。\n")
        code, payload = run_json(repo, "--class", "a", "--base", "HEAD")
        self.assertEqual(0, code)
        self.assertTrue(any(item["rule"] == "A5" and item["line"] == 401 for item in payload["findings"]))


class A9cTests(BaseCase):
    def test_sha_in_code_fence_and_url_is_caught(self) -> None:
        body = """## 內容

`abcdef1234567890`

```text
deadbeef12345678
```

https://example.com/commit/abcdef1234567890
"""
        write(self.repo, "docs/sha-all.md", a_doc("SHA 涵蓋", body))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertGreaterEqual(sum(1 for item in payload["findings"] if item["rule"] == "A9c"), 3)

    def test_action_pin_forms_are_exempt_but_bare_hex_is_not(self) -> None:
        pin = "abcdef1234" + "a" * 30
        bare = "abcdef1234" + "b" * 30
        body = f"""## 內容

uses: actions/checkout@{pin}
actions/checkout@{pin}
| `actions/checkout` | v5.1.0 | `{pin}` | 主要 |
{bare}
uses: actions/checkout@{pin} # abcdef1234567890
"""
        write(self.repo, "docs/pin.md", a_doc("Pin 文件", body))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        hits = [item for item in payload["findings"] if item["rule"] == "A9c"]
        # 裸 hex 一則；action pin 行上的另一個裸 hex 一則。
        self.assertEqual(2, len(hits), hits)
        messages = " ".join(item["message"] for item in hits)
        self.assertIn(bare, messages)
        self.assertIn("abcdef1234567890", messages)
        self.assertNotIn(pin, messages)

    def test_empty_tree_hash_in_code_span_passes(self) -> None:
        write(self.repo, "docs/sha-ok.md", a_doc("SHA 正常文件", f"## 內容\n\n空樹是 `{EMPTY_TREE}`。\n"))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertNotIn("A9c", rules(payload))


class B1Tests(BaseCase):
    def test_missing_b1_is_caught(self) -> None:
        write(self.repo, "AGENTS.md", "# 指令檔\n\n沒有優先序句。\n")
        code, payload = run_json(self.repo, "--class", "b")
        self.assertEqual(0, code)
        self.assertIn("B1", rules(payload))

    def test_present_b1_passes(self) -> None:
        write(self.repo, "AGENTS.md", b_doc())
        code, payload = run_json(self.repo, "--class", "b")
        self.assertEqual(0, code)
        self.assertNotIn("B1", rules(payload))

    def test_b1_is_identical_in_task_brief_pm_and_reference(self) -> None:
        task_brief = REPO_ROOT / "skills" / "project-workflow" / "assets" / "templates" / "task-brief.md"
        pm = REPO_ROOT / "adapters" / "opencode" / "agents" / "pm.md"
        reference = REPO_ROOT / "skills" / "writing-contract" / "references" / "b-agent-docs.md"

        def full_b1_lines(path: Path) -> list[str]:
            return [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if "本檔若與程式碼" in line and "安全邊界" in line
            ]

        template_lines = full_b1_lines(task_brief)
        pm_lines = full_b1_lines(pm)
        reference_lines = full_b1_lines(reference)
        self.assertEqual(2, len(template_lines), template_lines)
        self.assertEqual(2, len(pm_lines), pm_lines)
        self.assertEqual(1, len(reference_lines), reference_lines)
        for line in template_lines + pm_lines + reference_lines:
            self.assertEqual(B1, line)


class BaseAndBaselineTests(BaseCase):
    def test_blob_base_exits_2(self) -> None:
        blob = git_out(self.repo, "hash-object", "-w", "docs/other.md")
        result = run(self.repo, "--base", blob)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("必須是 commit 物件", result.stdout)

    def test_empty_string_base_exits_2(self) -> None:
        result = run(self.repo, "--base", "")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("不可為空字串", result.stdout)

    def test_all_zero_base_uses_empty_tree(self) -> None:
        result = run(self.repo, "--base", "0" * 40, "--format", "json")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(EMPTY_TREE, payload["base"])

    def test_invalid_base_exits_2(self) -> None:
        result = run(self.repo, "--base", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("未完整檢查", result.stdout)

    def test_default_base_uses_head_not_full_scan(self) -> None:
        repo = make_repo(self.root / "defaultbase", commit=True)
        write(repo, "docs/legacy.md", "# 舊文件\n\n沒有四欄。\n")
        git(repo, "add", "-A")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic: 既有違規")
        result = run(repo, "--class", "a", "--format", "text")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("diff 相對於", result.stdout)
        self.assertNotIn("全掃", result.stdout)
        self.assertNotIn("A1／檢查 1", result.stdout)
        write(repo, "docs/legacy.md", "# 舊文件\n\n沒有四欄。\n\nPhase 2 已啟動\n")
        result = run(repo, "--class", "a", "--format", "text")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("A3／檢查 5", result.stdout)

    def test_baseline_tristate(self) -> None:
        bad = "# 壞文件\n\n沒有四欄。\n"
        write(self.repo, "docs/bad.md", bad)
        code, first = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertGreaterEqual(len(first["findings"]), 1)
        baseline = self.root / "baseline.json"
        baseline.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")

        write(self.repo, "docs/bad.md", a_doc("修好的文件", "## 內容\n\n正常內容。\n"))
        code, second = run_json(self.repo, "--class", "a", "--baseline", str(baseline))
        self.assertEqual(0, code)
        self.assertGreaterEqual(second["counts"]["resolved"], 1)

        write(self.repo, "docs/bad.md", bad)
        code, third = run_json(self.repo, "--class", "a", "--baseline", str(baseline))
        self.assertEqual(0, code)
        self.assertGreaterEqual(third["counts"]["persisting"], 1)

    def test_changed_base_marks_out_of_scope(self) -> None:
        repo = make_repo(self.root / "basechange", commit=True)
        write(repo, "docs/legacy.md", "# 舊文件\n\n沒有四欄。\n")
        git(repo, "add", "-A")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic: 既有違規")
        code, baseline = run_json(repo, "--class", "a", "--base", EMPTY_TREE)
        self.assertEqual(0, code)
        path = self.root / "baseline.json"
        path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
        code, payload = run_json(repo, "--class", "a", "--base", "HEAD", "--baseline", str(path))
        self.assertEqual(0, code)
        self.assertGreaterEqual(payload["counts"]["out-of-scope"], 1)
        self.assertTrue(payload["baseChanged"])
        result = run(repo, "--class", "a", "--base", "HEAD", "--baseline", str(path), "--format", "text")
        self.assertIn("範圍已改變", result.stdout)
        self.assertIn("不宣稱收斂", result.stdout)

    def test_baseline_type_error_is_readable_and_exit_1(self) -> None:
        path = self.root / "bad-baseline.json"
        path.write_text(json.dumps({"base": None, "findings": [{"file": "a.md", "line": "x", "rule": "A1", "message": "m"}]}), encoding="utf-8")
        result = run(self.repo, "--baseline", str(path))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("line 必須是整數", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)


class ScopeTests(BaseCase):
    def test_diff_checks_only_added_lines(self) -> None:
        repo = make_repo(self.root / "diff", commit=True)
        legacy = "# 舊文件\n\n## 內容\n\n這份文件沒有四欄。\n"
        write(repo, "docs/legacy.md", legacy)
        git(repo, "add", "-A")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic: 加入舊文件")
        write(repo, "docs/legacy.md", legacy + "\n補充說明。\n")
        code, payload = run_json(repo, "--class", "a", "--base", "HEAD")
        self.assertEqual(0, code)
        self.assertNotIn("A1", rules(payload))

        write(repo, "docs/legacy.md", legacy + "\n## 新章節\n\nPhase 2 已啟動\n")
        code, payload = run_json(repo, "--class", "a", "--base", "HEAD")
        self.assertEqual(0, code)
        self.assertIn("A3", rules(payload))

    def test_untracked_new_file_is_checked(self) -> None:
        repo = make_repo(self.root / "untracked", commit=True)
        write(repo, "docs/new.md", "# 新文件\n\n沒有四欄。\n")
        code, payload = run_json(repo, "--class", "a", "--base", "HEAD")
        self.assertEqual(0, code)
        self.assertIn("A1", rules(payload))

    def test_renamed_file_is_treated_as_new(self) -> None:
        repo = make_repo(self.root / "rename", commit=True)
        write(repo, "docs/old.md", "# 舊檔\n\n沒有四欄。\n")
        git(repo, "add", "-A")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic: 加入舊檔")
        git(repo, "mv", "docs/old.md", "docs/new.md")
        code, payload = run_json(repo, "--class", "a", "--base", "HEAD")
        self.assertEqual(0, code)
        files = {item["file"] for item in payload["findings"]}
        self.assertIn("docs/new.md", files)
        self.assertNotIn("docs/old.md", files)

    def test_deleted_file_is_not_checked(self) -> None:
        repo = make_repo(self.root / "delete", commit=True)
        write(repo, "docs/legacy-bad.md", "# 舊檔\n\n沒有四欄。\n")
        git(repo, "add", "-A")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic: 加入舊檔")
        (repo / "docs/legacy-bad.md").unlink()
        code, payload = run_json(repo, "--class", "a", "--base", "HEAD")
        self.assertEqual(0, code)
        self.assertNotIn("docs/legacy-bad.md", {item["file"] for item in payload["findings"]})

    def test_class_filter_separates_a_and_b(self) -> None:
        write(self.repo, "docs/bad.md", "# 壞文件\n\n沒有四欄。\n")
        write(self.repo, "AGENTS.md", "# 指令檔\n\n沒有優先序句。\n")
        code, payload = run_json(self.repo, "--class", "b")
        self.assertEqual(0, code)
        self.assertIn("B1", rules(payload))
        self.assertNotIn("A1", rules(payload))

    def test_json_findings_have_required_fields(self) -> None:
        write(self.repo, "docs/bad.md", "# 壞文件\n\n沒有四欄。\n")
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        item = payload["findings"][0]
        for key in ("file", "line", "rule", "message", "state"):
            self.assertIn(key, item)


class DowngradedRulesTests(BaseCase):
    def test_downgraded_rules_are_not_reported(self) -> None:
        body = """## 清單

- 第一項
- 第二項
- 第三項
- 第四項
- 第五項
- 第六項
- 第七項
- 第八項
- 第九項

## 空章節

待處理

## 模糊

這大概會通過，而且有相關說明。

## 重複

這是一段長度超過二十五個字的句子，用來測試降級後不再做跨檔重複比對。
"""
        write(self.repo, "docs/downgraded.md", a_doc("降級文件", body))
        write(self.repo, "docs/downgraded2.md", a_doc("降級文件二", "## 重複\n\n這是一段長度超過二十五個字的句子，用來測試降級後不再做跨檔重複比對。\n"))
        code, payload = run_json(self.repo, "--class", "a")
        self.assertEqual(0, code)
        self.assertFalse(any(item["rule"] in ("A2r", "A4r", "A6", "A7") for item in payload["findings"]))
        self.assertFalse(any(item["rule"] == "A5" for item in payload["findings"]))

    def test_notes_root_does_not_create_duplicate_check(self) -> None:
        notes = self.root / "notes"
        sentence = "這是一段長度超過二十五個字的跨 repo 重複句子，降級後不應該再被檢查。"
        write(notes, "projects/demo/progress.md", f"# 進度\n\n{sentence}\n")
        (notes / "projects" / "demo" / "non-utf8.md").write_bytes(b"\xff\xfe")
        write(self.repo, "docs/note-dup.md", a_doc("跨庫重複", f"## 內容\n\n{sentence}\n"))
        code, payload = run_json(self.repo, "--class", "a", "--notes-root", str(notes))
        self.assertEqual(0, code)
        self.assertFalse(any(item["check"] == "14" for item in payload["findings"]))
        self.assertFalse(any(item["rule"] == "A2r" for item in payload["findings"]))

    def test_non_utf8_scanned_file_exits_2(self) -> None:
        (self.repo / "docs" / "bad-bytes.md").write_bytes(b"\xff\xfe")
        result = run(self.repo, "--class", "a")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("不是 UTF-8", result.stdout)


class TemplateTests(unittest.TestCase):
    def test_four_templates_contain_four_field_block(self) -> None:
        templates = REPO_ROOT / "skills" / "project-workflow" / "assets" / "templates"
        for name in ("overview.md", "progress.md", "plan.md", "acceptance.md"):
            with self.subTest(template=name):
                lines = (templates / name).read_text(encoding="utf-8").splitlines()
                positions: list[int] = []
                for label in ("讀者", "一句話", "現在要做", "依據"):
                    matches = [
                        (index, re.match(rf"^\s*>\s*\*\*{label}\*\*：(?P<value>.+)$", line))
                        for index, line in enumerate(lines)
                    ]
                    hits = [(index, match) for index, match in matches if match]
                    self.assertEqual(1, len(hits), f"{name} 的 {label} 欄必須恰好一行")
                    index, match = hits[0]
                    value = match.group("value").strip()
                    self.assertTrue(value, f"{name} 的 {label} 欄不得為空")
                    self.assertNotIn(value, ("<>", "<…>", "<...>"), f"{name} 的 {label} 欄不得是空佔位符")
                    positions.append(index)
                self.assertEqual(list(range(positions[0], positions[0] + 4)), positions, f"{name} 四欄必須相鄰")


class AssetAndDocumentationTests(unittest.TestCase):
    def test_fillers_is_the_only_required_input(self) -> None:
        for name in ("fillers.md", "terms.md", "vague.md", "familiar-terms.md", "duplicate-allowlist.md"):
            with self.subTest(asset=name):
                self.assertTrue((ASSETS / name).is_file(), name)
        self.assertTrue((ASSETS / "LICENSE.zh-tech-writing").is_file())

    def test_terms_keeps_upstream_copyright_and_self_made_tables_say_so(self) -> None:
        terms = (ASSETS / "terms.md").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 jacklondon", terms)
        self.assertIn("LICENSE.zh-tech-writing", terms)
        for name in ("fillers.md", "vague.md"):
            with self.subTest(asset=name):
                text = (ASSETS / name).read_text(encoding="utf-8")
                self.assertIn("專案自擬", text)
                self.assertIn("非逐條翻譯", text)
        familiar = (ASSETS / "familiar-terms.md").read_text(encoding="utf-8")
        self.assertIn("本專案維護", familiar)
        license_text = (ASSETS / "LICENSE.zh-tech-writing").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 jacklondon", license_text)
        self.assertIn("Permission is hereby granted", license_text)

    def test_public_contract_documents_manual_boundaries(self) -> None:
        paths = [
            REPO_ROOT / "docs" / "writing-contract.md",
            REPO_ROOT / "skills" / "writing-contract" / "references" / "a-human-docs.md",
        ]
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("僅人工自檢", text)
                self.assertIn("不得陳述未經確認的行為", text)
                self.assertIn("jacklondon", text)
                self.assertIn("400", text)
                self.assertIn("一句話", text)
                self.assertIn("豁免", text)


class TableCopyrightTests(unittest.TestCase):
    def test_docs_and_reference_keep_attribution(self) -> None:
        for rel in ("docs/writing-contract.md", "skills/writing-contract/references/a-human-docs.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("jacklondon", text, rel)
            self.assertIn("LICENSE.zh-tech-writing", text, rel)


if __name__ == "__main__":
    unittest.main()
