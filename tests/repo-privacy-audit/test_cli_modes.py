"""CLI 模式的合成測試：staged / range / history 與退出碼契約。"""

from __future__ import annotations

import os
import sys
import unittest

# run-checks.sh 以 `python3 -m unittest tests/repo-privacy-audit/test_xxx.py`
# 執行；先把自己所在目錄放進 sys.path，才能 import 共用的 helpers 模組。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (  # noqa: E402  （必須在 sys.path 設定之後 import）
    FAKE_AWS_KEY,
    FAKE_GITHUB_TOKEN,
    AuditTestCase,
)


class TestStagedMode(AuditTestCase):
    fake_gitleaks_mode = "none"

    def test_index_differs_from_worktree_is_scanned(self) -> None:
        """--staged 掃 index，不是 worktree。

        情境：index 內容乾淨，但 worktree 已被改成有秘密且沒有重新 stage。
        期望：staged 掃描**不**報出來（因為 index 是乾淨的）。
        """
        repo = self.make_repo("staged-index")
        repo.write("work.txt", "line1\nnothing sensitive here\n")
        repo.git("add", "work.txt")
        # 只改 worktree，不重新 stage。
        repo.write("work.txt", f"line1\n{FAKE_GITHUB_TOKEN}\n")
        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})

        result = self.audit(["--staged", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"], repo=repo)
        report = self.assertStatusMatchesExit(result)
        self.assertContract(report)
        self.assertEqual(report["summary"]["high"], 0, "index 乾淨就不該在 staged 掃描報出秘密")
        self.assertNotIn("github-token", result.rule_ids())
        self.assertNoSecretLeaked(result, [FAKE_GITHUB_TOKEN])

        # 反過來：把有秘密的內容 stage 進 index，就必須被發現。
        repo.git("add", "work.txt")
        result2 = self.audit(["--staged", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"], repo=repo)
        report2 = self.assertStatusMatchesExit(result2)
        self.assertIn("github-token", result2.rule_ids())
        self.assertEqual(report2["status"], "incomplete")
        self.assertEqual(result2.returncode, 2)  # 缺 gitleaks → incomplete
        self.assertNoSecretLeaked(result2, [FAKE_GITHUB_TOKEN])

    def test_env_example_scanned_not_blocked_by_filename(self) -> None:
        """.env.example 這種範本檔不按檔名阻擋，但內容仍要掃。"""
        repo = self.make_repo("env-example")
        repo.write(".env.example", f"API_KEY=\nTOKEN=\n# 範例不會有秘密\nSECRET={FAKE_AWS_KEY}\n", stage=True)
        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})

        result = self.audit(["--staged", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"], repo=repo)
        report = self.assertStatusMatchesExit(result)
        self.assertIn(".env.example", result.paths())
        self.assertIn("aws-access-key-id", result.rule_ids())

    def test_docs_and_agents_md_not_sensitive_by_default(self) -> None:
        """AGENTS.md / docs 不因檔名被視為敏感，但內容裡的秘密仍會被抓。"""
        repo = self.make_repo("docs")
        repo.write("AGENTS.md", "# 合成說明\n這只是文件。\n", stage=True)
        repo.write("docs/plan.md", "合成計畫，沒有秘密。\n", stage=True)
        clean = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}}, "clean.json")
        result = self.audit(["--staged", "--repo", str(repo.root), "--policy", str(clean), "--format", "json"], repo=repo)
        self.assertStatusMatchesExit(result)
        self.assertEqual(result.json["summary"]["high"], 0, "文件本身不該產生 high")

        repo.write("docs/notes.md", f"注意：{FAKE_GITHUB_TOKEN}\n", stage=True)
        result2 = self.audit(["--staged", "--repo", str(repo.root), "--policy", str(clean), "--format", "json"], repo=repo)
        self.assertIn("github-token", result2.rule_ids())
        self.assertIn("docs/notes.md", result2.paths())

    def test_non_ascii_and_blank_filenames(self) -> None:
        """空白與非 ASCII 檔名都要能掃。"""
        repo = self.make_repo("names")
        repo.write("空白 檔名.txt", f"內容\n{FAKE_AWS_KEY}\n", stage=True)
        repo.write("資料/ünïcode-測試.md", f"文字\n{FAKE_GITHUB_TOKEN}\n", stage=True)
        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})

        result = self.audit(["--staged", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"], repo=repo)
        report = self.assertStatusMatchesExit(result)
        paths = result.paths()
        self.assertIn("空白 檔名.txt", paths)
        self.assertIn("資料/ünïcode-測試.md", paths)
        self.assertEqual(report["summary"]["high"], 2)

    def test_missing_index_is_not_error(self) -> None:
        """完全沒有 staged 內容時不該爆。"""
        repo = self.make_repo("empty")
        repo.write("a.txt", "hello\n", stage=True)
        repo.commit("first")
        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})

        result = self.audit(["--staged", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"], repo=repo)
        report = self.assertStatusMatchesExit(result)
        self.assertEqual(report["status"], "incomplete")  # 缺 gitleaks
        self.assertEqual(report["coverage"]["scope"]["paths"], 0)

    def test_binary_files_skipped(self) -> None:
        repo = self.make_repo("binary")
        repo.write_bytes("blob.bin", b"\x00\x01\x02" + FAKE_AWS_KEY.encode(), stage=True)
        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})
        result = self.audit(["--staged", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"], repo=repo)
        report = self.assertStatusMatchesExit(result)
        self.assertEqual(report["summary"]["high"], 0)
        self.assertGreaterEqual(report["coverage"]["scope"]["filesSkipped"], 1)


class TestRangeMode(AuditTestCase):
    fake_gitleaks_mode = "none"

    def test_range_only_scans_in_range(self) -> None:
        repo = self.make_repo("range")
        repo.write("clean.txt", "no secrets\n", stage=True)
        base = repo.commit("base")
        repo.write("clean2.txt", "still clean\n", stage=True)
        mid = repo.commit("mid")
        repo.write("leak.txt", f"{FAKE_GITHUB_TOKEN}\n", stage=True)
        repo.commit("add leak")

        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})
        result = self.audit(
            ["--range", f"{base}..{mid}", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"],
            repo=repo,
        )
        report = self.assertStatusMatchesExit(result)
        self.assertEqual(report["summary"]["high"], 0, "range 內沒有秘密就不該有發現")

        result2 = self.audit(
            ["--range", f"{mid}..HEAD", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"],
            repo=repo,
        )
        self.assertIn("github-token", result2.rule_ids())
        self.assertEqual(result2.returncode, 2)

    def test_range_bad_endpoint_errors(self) -> None:
        repo = self.make_repo("range-bad")
        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})
        result = self.audit(
            ["--range", "HEAD..nope-not-a-ref", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"],
            repo=repo,
        )
        self.assertExitCode(result, 2)
        self.assertEqual(result.json["status"], "error")

    def test_range_without_dots_errors(self) -> None:
        repo = self.make_repo("range-nodots")
        result = self.audit(["--range", "HEAD", "--repo", str(repo.root), "--format", "json"], repo=repo)
        self.assertExitCode(result, 2)
        self.assertEqual(result.json["status"], "error")


class TestHistoryMode(AuditTestCase):
    fake_gitleaks_mode = "none"

    def test_history_scans_all_local_refs(self) -> None:
        repo = self.make_repo("history")
        repo.write("seed.txt", "seed\n", stage=True)
        base = repo.commit("seed")

        # 在一個獨立分支上放秘密，主分支乾淨。
        repo.git("checkout", "-q", "-b", "feature")
        repo.write("feature.txt", f"{FAKE_AWS_KEY}\n", stage=True)
        repo.commit("feature with secret")
        repo.git("checkout", "-q", "main")

        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})
        result = self.audit(["--history", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"], repo=repo)
        report = self.assertStatusMatchesExit(result)
        # --all 會涵蓋 feature 分支，所以應該抓到。
        self.assertIn("aws-access-key-id", result.rule_ids())
        self.assertIn("refs/heads/feature", report["coverage"]["scope"]["refs"])

        # 只掃 main 的 range 就抓不到（證明 feature 只是在 --all 時被涵蓋）。
        result2 = self.audit(
            ["--range", f"{base}..HEAD", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"],
            repo=repo,
        )
        self.assertNotIn("aws-access-key-id", result2.rule_ids())

    def test_history_does_not_claim_reflog_coverage(self) -> None:
        repo = self.make_repo("history-limit")
        repo.write("a.txt", "x\n", stage=True)
        repo.commit("a")
        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})
        result = self.audit(["--history", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"], repo=repo)
        limits = " ".join(result.json["coverage"]["limitations"])
        self.assertIn("reflog", limits, "必須明說不涵蓋 reflog")

    def test_empty_repo_history(self) -> None:
        repo = self.make_repo("history-empty")
        policy = repo.write_policy({"metadata": {"emailSeverity": "off", "scanCommitMessage": False}})
        result = self.audit(["--history", "--repo", str(repo.root), "--policy", str(policy), "--format", "json"], repo=repo)
        report = self.assertStatusMatchesExit(result)
        self.assertEqual(report["coverage"]["scope"]["commits"], 0)


if __name__ == "__main__":
    unittest.main()
