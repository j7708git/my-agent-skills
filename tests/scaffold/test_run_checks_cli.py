"""scripts/run-checks.sh 的 CLI 與 Gitleaks 整合測試。

全部在暫存目錄中使用合成資料；不使用真實 repo、不修改任何全域設定。
每個子行程都有 timeout，避免指令陷入無限迴圈時整個測試停住。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_CHECKS = REPO_ROOT / "scripts" / "run-checks.sh"
TIMEOUT = 180

REQUIRED_DOCS = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/development.md",
    "docs/compatibility.md",
)
SKILL_MD = """---
name: demo-skill
description: 合成測試用 skill，僅存在於暫存目錄，用於驗證統一檢查腳本的行為。
---

這是合成內容，請見 [指引](references/GUIDE.md)。
"""


# 合成憑證：固定值、決定性，不在測試執行時隨機產生。
# 隨機抽字元會讓測試結果偶發漂移（低熵抽樣可能不會被 Gitleaks 判定為發現），
# 所以這裡用固定的假值。這些值不是真實憑證，只是形狀像 AWS 金鑰。
# 以分段字串拼接組成，避免原始碼裡出現完整的憑證字面值（否則 repo 自身的秘密掃描會誤報）。
_SYNTHETIC_ID_SEGMENTS = ("AKIA", "R7XQ", "M2VK", "9TWD", "P4HZ")
_SYNTHETIC_SECRET_SEGMENTS = (
    "wJ0", "aQ7", "Lm9", "Zx3", "Kp5",
    "Tr8", "Vn2", "Bd6", "Hs4", "Fy9",
    "Gq1", "Wx7", "Jc3", "Pm8",
)
# 已用 Gitleaks 8.30.1（generic-api-key 規則）確認下列組合會被判定為發現。
SYNTHETIC_SECRET_TEXT = (
    f"aws_access_key_id = {''.join(_SYNTHETIC_ID_SEGMENTS)}\n"
    f"aws_secret_access_key = {''.join(_SYNTHETIC_SECRET_SEGMENTS)}\n"
)


def synthetic_secret() -> str:
    return SYNTHETIC_SECRET_TEXT


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
    subprocess.run(["git", *args], cwd=repo, check=True, env=env, timeout=TIMEOUT, capture_output=True)


def make_fixture_repo(root: Path) -> Path:
    """建立一個能通過格式驗證、且有真實 commit 的最小 repo。"""
    repo = root / "fixture"
    (repo / "skills" / "demo-skill" / "references").mkdir(parents=True)
    (repo / "skills" / "demo-skill" / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (repo / "skills" / "demo-skill" / "references" / "GUIDE.md").write_text("# 合成指引\n", encoding="utf-8")
    for rel in REQUIRED_DOCS:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(f"# 合成文件 {rel}\n", encoding="utf-8")
    (repo / "LICENSE").write_text("合成授權文字\n", encoding="utf-8")
    (repo / "clean.txt").write_text("沒有秘密的合成內容\n", encoding="utf-8")
    (repo / "tests" / "demo").mkdir(parents=True)
    (repo / "tests" / "demo" / "test_ok.py").write_text(
        "import unittest\n\n\nclass Ok(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    git(repo, "init", "-q", ".")
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic: 初始提交")
    return repo


def write_stub_gitleaks(path: Path, exit_code: int, report: list | None) -> Path:
    """建立假的 gitleaks：可控退出碼與報告內容（用來測試映射與 fail closed）。

    report=None → 完全不寫報告檔（模擬工具異常，必須被視為未完成）。
    """
    lines = [
        "#!/usr/bin/env bash",
        "# 合成 stub：只模擬退出碼與報告檔，用於驗證 run-checks.sh 的映射邏輯。",
        'report=""',
        'prev=""',
        'for arg in "$@"; do',
        '  if [ "$prev" = "--report-path" ]; then report="$arg"; fi',
        '  prev="$arg"',
        "done",
    ]
    if report is not None:
        lines.append(f"printf '%s' {json.dumps(json.dumps(report))} > \"$report\"")
    lines.append(f"exit {exit_code}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def run_checks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(RUN_CHECKS), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


class SyntheticFixtureTests(unittest.TestCase):
    def test_fixture_is_deterministic(self) -> None:
        """合成憑證必須是固定值；隨機產生會讓掃描結果偶發漂移。"""
        self.assertEqual(synthetic_secret(), synthetic_secret())
        self.assertEqual(SYNTHETIC_SECRET_TEXT, synthetic_secret())

    def test_fixture_source_has_no_full_credential_literal(self) -> None:
        """原始碼只放分段字串，避免 repo 自身的秘密掃描把這裡當成真憑證。"""
        source = Path(__file__).read_text(encoding="utf-8")
        for segment in _SYNTHETIC_ID_SEGMENTS[1:] + _SYNTHETIC_SECRET_SEGMENTS:
            # 單一分段必須出現（證明是拼接而來）。
            self.assertIn(segment, source)
        # 拼接後的完整值不可出現在原始碼。
        self.assertNotIn("".join(_SYNTHETIC_ID_SEGMENTS), source)
        self.assertNotIn("".join(_SYNTHETIC_SECRET_SEGMENTS), source)


class ArgumentGuardTests(unittest.TestCase):
    def test_missing_repo_value_exits_2_without_looping(self) -> None:
        result = run_checks("--repo")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("--repo 需要一個路徑參數", result.stderr)

    def test_missing_gitleaks_value_exits_2_without_looping(self) -> None:
        result = run_checks("--gitleaks")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("--gitleaks 需要一個執行檔路徑", result.stderr)

    def test_unknown_argument_exits_1(self) -> None:
        result = run_checks("--not-a-flag")
        self.assertEqual(1, result.returncode)
        self.assertIn("未知的參數", result.stderr)


class GitleaksMappingTests(unittest.TestCase):
    """用 stub 驗證退出碼映射與 fail closed 行為，不依賴真實 gitleaks。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_fixture_repo(self.root)

    def _run(self, stub: Path) -> subprocess.CompletedProcess:
        return run_checks("--repo", str(self.repo), "--gitleaks", str(stub))

    def test_clean_report_passes(self) -> None:
        stub = write_stub_gitleaks(self.root / "gitleaks-clean", 0, [])
        result = self._run(stub)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("0 筆發現", result.stdout)

    def test_findings_exit_99_maps_to_failure(self) -> None:
        finding = [{"RuleID": "generic-api-key", "File": "a.txt", "StartLine": 3, "Commit": "0" * 40, "Secret": "REDACTED"}]
        stub = write_stub_gitleaks(self.root / "gitleaks-findings", 99, finding)
        result = self._run(stub)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("generic-api-key", result.stdout)
        self.assertIn("發現疑似秘密", result.stderr)

    def test_other_nonzero_maps_to_incomplete(self) -> None:
        stub = write_stub_gitleaks(self.root / "gitleaks-error", 1, [])
        result = self._run(stub)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        # 非預期退出碼的說明寫在 stderr，彙總表標為「未完成」。
        self.assertIn("以非預期碼結束", result.stderr)
        self.assertIn("未完成（掃描未完成（gitleaks=1））", result.stdout)

    def test_missing_report_fails_closed(self) -> None:
        stub = write_stub_gitleaks(self.root / "gitleaks-noreport", 0, None)
        result = self._run(stub)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        # 缺報告的說明寫在 stderr；彙總表的「未完成」狀態寫在 stdout。
        self.assertIn("沒有產生報告檔", result.stderr)
        self.assertIn("Gitleaks 歷史", result.stdout)

    def test_malformed_report_fails_closed(self) -> None:
        path = self.root / "gitleaks-bad"
        path.write_text(
            "#!/usr/bin/env bash\n"
            'report=""\nprev=""\nfor arg in "$@"; do\n'
            '  if [ "$prev" = "--report-path" ]; then report="$arg"; fi\n  prev="$arg"\ndone\n'
            "printf 'not json' > \"$report\"\nexit 0\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        result = self._run(path)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        # 解析失敗的說明與彙總表都在 stdout：沒有有效報告時不得靜默通過。
        self.assertIn("報告無法解析", result.stdout)
        self.assertIn("未完成（報告不合法）", result.stdout)

    def test_secret_never_printed_from_report(self) -> None:
        finding = [
            {
                "RuleID": "generic-api-key",
                "File": "a.txt",
                "StartLine": 3,
                "Commit": "a" * 40,
                "Secret": "SUPERSECRETVALUE",
            }
        ]
        stub = write_stub_gitleaks(self.root / "gitleaks-secret", 99, finding)
        result = self._run(stub)
        self.assertNotIn("SUPERSECRETVALUE", result.stdout + result.stderr)


class RealGitleaksTests(unittest.TestCase):
    """有真實 gitleaks 時才跑：驗證歷史與 staged 的實際行為。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_fixture_repo(self.root)
        candidates = [
            os.environ.get("GITLEAKS", ""),
            "/tmp/opencode/my-agent-skills-acceptance/tools/gitleaks",
        ]
        self.gitleaks = next((c for c in candidates if c and Path(c).is_file() and os.access(c, os.X_OK)), None)
        if not self.gitleaks:
            self.skipTest("找不到可執行的 gitleaks；略過真實掃描測試")

    def _run(self) -> subprocess.CompletedProcess:
        return run_checks("--repo", str(self.repo), "--gitleaks", self.gitleaks)

    def test_history_scan_detects_committed_secret(self) -> None:
        (self.repo / "leak.txt").write_text(synthetic_secret(), encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic: 歷史中的秘密")
        result = self._run()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("Gitleaks 歷史", result.stdout)
        self.assertIn("leak.txt", result.stdout)

    def test_staged_scan_detects_uncommitted_secret(self) -> None:
        (self.repo / "staged-leak.txt").write_text(synthetic_secret(), encoding="utf-8")
        git(self.repo, "add", "staged-leak.txt")
        result = self._run()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("staged-leak.txt", result.stdout)

    def test_clean_repo_passes_both_scans(self) -> None:
        result = self._run()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("0 筆發現", result.stdout)


if __name__ == "__main__":
    unittest.main()
