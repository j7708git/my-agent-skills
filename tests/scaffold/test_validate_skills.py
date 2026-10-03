"""scripts/validate_skills.py 的合成測試。

只用 Python 3 標準函式庫；所有 fixture 都在暫存目錄中建立，不碰真實 repo。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = REPO_ROOT / "scripts" / "validate_skills.py"
TIMEOUT = 60


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_skills_under_test", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"無法載入 {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = load_validator()


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_skill(root: Path, name: str = "demo-skill", frontmatter: str | None = None, body: str | None = None) -> Path:
    skill_dir = root / "skills" / name
    front = frontmatter if frontmatter is not None else (
        "---\n"
        "name: demo-skill\n"
        "description: 驗證用合成 skill，僅在暫存目錄中建立，用於格式驗證器的單元測試。\n"
        "---\n"
    )
    text = front + (body if body is not None else "\n執行步驟請見 [指引](references/GUIDE.md)。\n")
    write(skill_dir / "SKILL.md", text)
    write(skill_dir / "references" / "GUIDE.md", "# 合成指引\n")
    return skill_dir


def make_required_repo_docs(root: Path) -> None:
    """建立 validate_docs 要求的必要公開維護文件（內容皆為合成文字）。"""
    for rel in (
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "docs/specification.md",
        "docs/development.md",
        "docs/compatibility.md",
    ):
        write(root / rel, f"# 合成文件 {rel}\n")
    write(root / "LICENSE", "合成授權文字\n")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """在暫存 repo 內執行唯讀／索引用的 git 指令；不碰任何真實設定。"""
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
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, env=env, timeout=TIMEOUT, capture_output=True, text=True
    )


def run(root: Path, *extra: str) -> tuple[int, dict]:
    argv = ["--repo", str(root), "--format", "json", *extra]
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = validator.main(argv)
    return code, json.loads(buffer.getvalue())


class ValidateSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_valid_skill_passes(self) -> None:
        make_skill(self.root)
        code, payload = run(self.root)
        self.assertEqual(0, code, payload)
        self.assertEqual("passed", payload["status"])
        self.assertEqual(1, payload["skillsChecked"])
        self.assertEqual([], payload["errors"])

    def test_missing_skills_dir_is_incomplete_not_pass(self) -> None:
        code, payload = run(self.root)
        self.assertEqual(2, code)
        self.assertEqual("incomplete", payload["status"])
        self.assertIsNotNone(payload["incompleteReason"])

    def test_empty_skills_dir_is_incomplete(self) -> None:
        (self.root / "skills").mkdir()
        code, payload = run(self.root)
        self.assertEqual(2, code)
        self.assertEqual("incomplete", payload["status"])

    def test_name_must_match_directory(self) -> None:
        make_skill(self.root, frontmatter="---\nname: other-name\ndescription: 合成描述文字，长度足夠通過檢查。\n---\n")
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("父目錄名" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_name_rejects_uppercase_and_double_hyphen(self) -> None:
        skill_dir = self.root / "skills" / "Bad--Name"
        write(skill_dir / "SKILL.md", "---\nname: Bad--Name\ndescription: 合成描述文字，長度足夠通過檢查。\n---\n內容\n")
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("kebab-case" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_description_length_limit(self) -> None:
        long_desc = "描" * (validator.DESCRIPTION_MAX + 1)
        make_skill(self.root, frontmatter=f"---\nname: demo-skill\ndescription: {long_desc}\n---\n")
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("description" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_missing_required_field(self) -> None:
        make_skill(self.root, frontmatter="---\nname: demo-skill\n---\n內容\n")
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("description" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_nested_frontmatter_is_rejected(self) -> None:
        make_skill(
            self.root,
            frontmatter="---\nname: demo-skill\ndescription: 合成描述文字，長度足夠通過檢查。\nmetadata:\n  author: demo\n---\n",
        )
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("巢狀" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_unknown_key_is_rejected(self) -> None:
        make_skill(
            self.root,
            frontmatter="---\nname: demo-skill\ndescription: 合成描述文字，長度足夠通過檢查。\nversion: 1.0.0\n---\n",
        )
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("允許清單" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_missing_relative_resource_is_reported(self) -> None:
        skill_dir = make_skill(self.root)
        (skill_dir / "references" / "GUIDE.md").unlink()
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("找不到引用的資源" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_code_span_script_reference_must_exist(self) -> None:
        make_skill(self.root, body="\n執行 `bash scripts/run.sh --staged`。\n")
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("scripts/run.sh" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_external_urls_are_ignored(self) -> None:
        make_skill(
            self.root,
            body="\n參考 https://agentskills.io/specification 與 <https://example.com/a/b>。\n",
        )
        code, payload = run(self.root)
        self.assertEqual(0, code, payload["errors"])
        self.assertEqual([], payload["errors"])

    def test_directory_without_skill_md_is_error(self) -> None:
        (self.root / "skills" / "empty-skill").mkdir(parents=True)
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("SKILL.md" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_skill_reference_cannot_fall_back_to_repo_root(self) -> None:
        """安裝時只會複製 skill 目錄，因此引用 repo 根檔案必須被拒絕。"""
        write(self.root / "docs" / "plan.md", "# 根層文件\n")
        write(self.root / "README.md", "# 根層 README\n")
        make_skill(self.root, body="\n請先讀 [計畫](../docs/plan.md) 與 [說明](../README.md)。\n")
        code, payload = run(self.root)
        self.assertEqual(1, code)
        messages = " ".join(e["message"] for e in payload["errors"])
        self.assertIn("指向skill 目錄之外", messages)
        self.assertIn("../docs/plan.md", messages)
        self.assertIn("../README.md", messages)

    def test_root_level_reference_without_dotdot_is_rejected(self) -> None:
        """即使檔案存在於 repo 根，skill 也不得引用它。"""
        write(self.root / "docs" / "plan.md", "# 根層文件\n")
        skill_dir = make_skill(self.root)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: demo-skill\n"
            "description: 驗證用合成 skill，僅在暫存目錄中建立，用於格式驗證器的單元測試。\n"
            "---\n"
            "細節見 [計畫](docs/plan.md)。\n",
            encoding="utf-8",
        )
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(
            any("找不到引用的資源：docs/plan.md" in e["message"] for e in payload["errors"]),
            payload["errors"],
        )

    def test_reference_may_use_dotdot_inside_skill(self) -> None:
        """skill 內部可以用 ../ 回到 skill 根，不算逃出。"""
        skill_dir = make_skill(self.root)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: demo-skill\n"
            "description: 驗證用合成 skill，僅在暫存目錄中建立，用於格式驗證器的單元測試。\n"
            "---\n"
            "指引在 [references 子目錄](references/../references/GUIDE.md)。\n",
            encoding="utf-8",
        )
        code, payload = run(self.root)
        self.assertEqual(0, code, payload["errors"])

    def test_absolute_path_reference_is_rejected(self) -> None:
        make_skill(self.root, body="\n請看 [/etc/passwd](/etc/passwd)。\n")
        code, payload = run(self.root)
        self.assertEqual(1, code)
        self.assertTrue(any("絕對路徑" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_strict_turns_warnings_into_failure(self) -> None:
        make_skill(self.root, frontmatter="---\nname: demo-skill\ndescription: 短\n---\n內容\n")
        code, payload = run(self.root)
        self.assertEqual(0, code)
        self.assertTrue(payload["warnings"])
        strict_code, _ = run(self.root, "--strict")
        self.assertEqual(1, strict_code)

    def test_check_docs_detects_broken_link(self) -> None:
        make_skill(self.root)
        make_required_repo_docs(self.root)
        write(self.root / "README.md", "見 [開發說明](docs/development.md)。\n")
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(0, code, payload["errors"])
        write(self.root / "README.md", "見 [開發說明](docs/missing.md)。\n")
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(1, code)
        self.assertTrue(any("docs/missing.md" in e["message"] for e in payload["errors"]), payload["errors"])

    def test_check_docs_allows_repo_root_relative_link(self) -> None:
        """repo 文件本來就可用 repo 根為基準解析（不同於 skill）。"""
        make_skill(self.root)
        make_required_repo_docs(self.root)
        write(self.root / "docs" / "development.md", "規則見 [AGENTS](../AGENTS.md) 與 [根 README](../README.md)。\n")
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(0, code, payload["errors"])

    def test_check_docs_requires_repo_maintenance_files(self) -> None:
        make_skill(self.root)
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(1, code)
        missing = {e["path"] for e in payload["errors"]}
        self.assertIn("README.md", missing)
        self.assertIn("LICENSE", missing)

    def test_json_output_has_no_absolute_paths(self) -> None:
        make_skill(self.root)
        _, payload = run(self.root)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.root), blob)


class DocumentSplitTests(unittest.TestCase):
    """文件分流：公開文件自足，內部工作紀錄不進版控。

    這組測試守住三件事：
    1. 沒有內部計畫／驗收紀錄時，公開文件仍然可以被完整驗證。
    2. 公開文件引用 .local/ 或 .openchamber/ 一律被拒絕（即使那些檔案在本機存在）。
    3. .local/ 被 Git 追蹤時被拒絕；沒有 Git 工作樹時跳過且不宣稱已檢查。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        make_skill(self.root)
        make_required_repo_docs(self.root)

    def test_check_docs_passes_without_internal_plan_or_reports(self) -> None:
        """沒有 docs/plan.md、驗收紀錄或發布清單，公開文件仍應通過。"""
        for rel in ("docs/plan.md", "docs/acceptance.md", "docs/release-checklist.md"):
            self.assertFalse((self.root / rel).exists())
        code, payload = run(self.root, "--check-docs", "--strict")
        self.assertEqual(0, code, payload["errors"] + payload["warnings"])

    def test_specification_is_a_required_public_contract(self) -> None:
        """docs/specification.md 是必要公開契約：缺席必須明確報錯。"""
        self.assertIn("docs/specification.md", validator.REQUIRED_REPO_FILES)
        (self.root / "docs" / "specification.md").unlink()
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(1, code)
        errors = [e for e in payload["errors"] if e["path"] == "docs/specification.md"]
        self.assertEqual(1, len(errors), payload["errors"])
        self.assertIn("缺少必要的 repo 維護文件", errors[0]["message"])

    def test_specification_empty_file_is_rejected(self) -> None:
        """契約存在但空白等於沒有契約，與缺席同樣不可接受。"""
        write(self.root / "docs" / "specification.md", "   \n\n")
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(1, code)
        self.assertTrue(
            any(e["path"] == "docs/specification.md" and "為空檔" in e["message"] for e in payload["errors"]),
            payload["errors"],
        )

    def test_this_repo_ships_public_specification_linked_from_public_docs(self) -> None:
        """本 repo 必須真的提供產品契約，且公開文件把它連出來。"""
        spec = REPO_ROOT / "docs" / "specification.md"
        self.assertTrue(spec.is_file(), "docs/specification.md 必須存在")
        for rel in ("README.md", "AGENTS.md", "CONTRIBUTING.md"):
            self.assertIn("docs/specification.md", (REPO_ROOT / rel).read_text(encoding="utf-8"))

    def test_public_doc_must_not_link_into_local_dir(self) -> None:
        """.local/ 的檔案在本機存在也一樣要拒絕：對外部讀者是死連結。"""
        write(self.root / ".local" / "plans" / "development.md", "# 內部計畫\n")
        write(
            self.root / "README.md",
            "見 [內部計畫](.local/plans/development.md)。\n",
        )
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(1, code)
        messages = " ".join(e["message"] for e in payload["errors"])
        self.assertIn("不可引用本機專屬路徑", messages)
        self.assertIn(".local", messages)

    def test_docs_dir_link_with_dotdot_into_local_dir_is_rejected(self) -> None:
        """docs/ 內的文件用 ../ 回到根再進 .local/，同樣要拒絕。"""
        write(self.root / ".local" / "reports" / "acceptance.md", "# 內部驗收紀錄\n")
        write(
            self.root / "docs" / "development.md",
            "細節見 [驗收紀錄](../.local/reports/acceptance.md)。\n",
        )
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(1, code)
        self.assertTrue(
            any("不可引用本機專屬路徑" in e["message"] for e in payload["errors"]),
            payload["errors"],
        )

    def test_public_doc_must_not_link_into_openchamber_plan(self) -> None:
        """派工任務卡同樣不在版控內，公開文件不得引用。"""
        write(self.root / ".openchamber" / "plans" / "task.md", "# 內部任務卡\n")
        write(self.root / "AGENTS.md", "見 [任務卡](.openchamber/plans/task.md)。\n")
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(1, code)
        self.assertTrue(
            any("不可引用本機專屬路徑" in e["message"] for e in payload["errors"]),
            payload["errors"],
        )

    @unittest.skipUnless(shutil.which("git"), "需要 git 才能測試被追蹤的本機路徑")
    def test_tracked_local_file_is_rejected(self) -> None:
        """.local/ 被 Git 追蹤就是分流失效，必須報錯。"""
        write(self.root / ".local" / "plans" / "secret-notes.md", "# 合成內部筆記\n")
        git(self.root, "init", "-q", ".")
        git(self.root, "add", "-A", "--", ".local")
        code, payload = run(self.root, "--check-docs")
        self.assertEqual(1, code)
        self.assertIn("local-dirs-not-tracked", payload["coverage"])
        messages = " ".join(e["message"] for e in payload["errors"])
        self.assertIn("本機專屬路徑被 Git 追蹤", messages)
        self.assertIn(".local", messages)

    @unittest.skipUnless(shutil.which("git"), "需要 git 才能測試未追蹤的本機路徑")
    def test_untracked_local_file_is_allowed(self) -> None:
        """未被追蹤的 .local/ 是預期狀態（被 .gitignore 排除），不該報錯。"""
        write(self.root / ".local" / "plans" / "notes.md", "# 合成內部筆記\n")
        write(self.root / ".gitignore", ".local/\n.openchamber/\n")
        git(self.root, "init", "-q", ".")
        code, payload = run(self.root, "--check-docs", "--strict")
        self.assertEqual(0, code, payload["errors"] + payload["warnings"])
        self.assertIn("local-dirs-not-tracked", payload["coverage"])

    def test_tracked_check_is_skipped_and_not_claimed_without_git_repo(self) -> None:
        """沒有 Git 工作樹時跳過這項檢查，而且不得宣稱已檢查。"""
        self.assertIsNone(validator.tracked_paths(self.root))
        code, payload = run(self.root, "--check-docs", "--strict")
        self.assertEqual(0, code, payload["errors"] + payload["warnings"])
        self.assertNotIn("local-dirs-not-tracked", payload["coverage"])

    def test_local_dirs_are_git_ignored_in_this_repo(self) -> None:
        """本 repo 的 .gitignore 必須真的排除兩個本機目錄。"""
        patterns = [
            line.strip()
            for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for pattern in (".local/", ".openchamber/"):
            self.assertIn(pattern, patterns)


if __name__ == "__main__":
    unittest.main()
