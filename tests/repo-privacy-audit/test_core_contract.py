"""掃描核心的契約測試（合成資料）。

涵蓋範圍
--------
- pre-push：真的呼叫 Gitleaks（可用 ``RPA_REAL_GITLEAKS`` 指定）、缺 Gitleaks 一律退出 2、
  多 ref 逐 ref 計算再 union、merge commit 的 diff、遠端 SHA 不在本機。
- metadata：author + committer 的 name／email／訊息；電話型 email、noreply、allowedEmails；
  關掉身分規則不會把高風險秘密規則一起藏起來。
- checksum：台灣身分證字號演算法（可手算的合成向量）。
- paths：.env／私鑰檔名 high、.env.example 不按檔名擋、AGENTS.md／docs 不敏感、個人本機路徑。
- --github：只用 gh API GET 的**設定**檢查；權限不足 / 端點未啟用一律記為未檢查（incomplete），
  不推論「未啟用」；不需要 Gitleaks。
- 政策驗證：JSON 布林、severity、正規表示式、不支援的 checksum 驗證器。
- 輸出安全：秘密原文不會出現在報告、stderr 或錯誤訊息（含出現在檔名裡的情況）。

所有 fixture 都建在 /tmp/opencode 底下的暫存目錄；內容全是合成資料。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (  # noqa: E402  （必須在 sys.path 設定之後 import）
    REPO_ROOT,
    AuditTestCase,
    CliResult,
    GitRepoFixture,
    write_script,
)

CORE_PY = REPO_ROOT / "skills" / "repo-privacy-audit" / "scripts" / "audit_core.py"

# 真的 Gitleaks（PM 授權、已驗過 SHA-256 的 8.30.1）。找不到就跳過相關測試，
# 絕不用假的通過結果冒充真實工具驗證。
REAL_GITLEAKS = Path(
    os.environ.get("RPA_REAL_GITLEAKS", "/tmp/opencode/my-agent-skills-acceptance/tools/gitleaks")
)
HAVE_REAL_GITLEAKS = REAL_GITLEAKS.is_file() and os.access(REAL_GITLEAKS, os.X_OK)

# 會被真實 Gitleaks 預設規則命中的合成字串（不是真實憑證；形狀公開可見）。
# 一律分段組合，原始碼不留完整字面值：這些 fixture 必須放在產品原始碼裡，
# 讓 repo 自己的秘密掃描在**沒有任何豁免**的情況下也是乾淨的。
# 執行期組出來的值與分段前完全相同，所以斷言與偵測行為不變。
# 注意：雲端廠商官方範例那類金鑰會被 gitleaks 預設 allowlist 放行，所以不用它。
SYNTHETIC_GITUB_PAT = "ghp_" + "Zt7X" + "kQ2m" + "NvB8" + "yLpR" + "4wS6" + "dF0g" + "H3jK" + "9nL1" + "qT5vC2xA"
# AKIA + 剛好 16 個大寫／數字，才會被 \b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b 命中。
SYNTHETIC_AWS_KEY = "AKIA" + "QWER" + "TYUI" + "OPAS" + "DFGH"

# 假的 gh：只回傳本機 JSON 對照表裡的內容，不發出任何網路請求。
# FAKE_GH_MAP 指向一個 JSON 檔：{"<endpoint>": {"rc": 0, "json": <payload>}}
FAKE_GH = '''#!/usr/bin/env python3
"""測試用的假 gh：只讀 FAKE_GH_MAP，完全不連網。"""
import json
import os
import sys

args = sys.argv[1:]
endpoint = ""
for index, value in enumerate(args):
    if value == "-X" and index + 1 < len(args):
        continue
    if value in ("GET", "api", "-X"):
        continue
    endpoint = value
    break
if endpoint == "api":
    endpoint = ""
    for index, value in enumerate(args):
        if value.startswith("repos/"):
            endpoint = value
            break

table = {}
map_path = os.environ.get("FAKE_GH_MAP", "")
if map_path and os.path.isfile(map_path):
    with open(map_path, encoding="utf-8") as handle:
        table = json.load(handle)

entry = table.get(endpoint)
if entry is None:
    sys.stderr.write("gh: Not Found (HTTP 404) (synthetic)\\n")
    sys.exit(1)
sys.stderr.write(entry.get("stderr", ""))
sys.stdout.write(json.dumps(entry.get("json")))
sys.exit(int(entry.get("rc", 0)))
'''


def load_core() -> Any:
    """直接載入掃描核心，用於單元層級的斷言。"""
    spec = importlib.util.spec_from_file_location("rpa_audit_core_contract", CORE_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoreTestCase(AuditTestCase):
    """共用工具：本測試檔一律使用沒有假 gitleaks 的環境（真實需求自己帶 --gitleaks）。"""

    fake_gitleaks_mode = "none"

    def setUp(self) -> None:
        super().setUp()
        self.repo_scratch = Path(self.tool_dir)

    # -- 小工具 -----------------------------------------------------------
    def quiet_policy(self, **extra: Any) -> Dict[str, Any]:
        policy = {
            "schemaVersion": "1.0",
            "id": "contract-test",
            "metadata": {"emailSeverity": "off", "scanCommitMessage": False},
        }
        for key, value in extra.items():
            policy[key] = value
        return policy

    def audit_with(
        self,
        args: Sequence[str],
        repo: GitRepoFixture,
        policy: Dict[str, Any],
        *,
        stdin: str = "",
        gitleaks: Optional[str] = None,
    ) -> CliResult:
        path = repo.write_policy(policy)
        argv = list(args) + ["--repo", str(repo.root), "--policy", str(path), "--format", "json"]
        if gitleaks:
            argv += ["--gitleaks", gitleaks]
        return self.audit(argv, stdin=stdin, repo=repo)

    def install_fake_gh(self, table: Dict[str, Any]) -> str:
        """每次呼叫都用新的檔名，避免同一個測試裡覆寫時互相影響。"""
        index = self._gh_counter = getattr(self, "_gh_counter", 0) + 1
        map_path = self.tool_dir / f"gh-map-{index}.json"
        map_path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        script = write_script(self.tool_dir / f"gh-{index}", FAKE_GH)
        if not os.access(script, os.X_OK):  # pragma: no cover - 環境異常
            raise AssertionError(f"假的 gh 不可執行：{script}")
        self.env["FAKE_GH_MAP"] = str(map_path)
        return str(script)

    def secrets_in(self, result: CliResult, secrets: Sequence[str]) -> None:
        combined = result.stdout + result.stderr
        for secret in secrets:
            self.assertNotIn(secret, combined, f"秘密原文出現在輸出：{secret[:6]}…")


# ---------------------------------------------------------------------------
# pre-push：一定要真的跑 Gitleaks
# ---------------------------------------------------------------------------


class TestPrePushGitleaks(CoreTestCase):
    def push_line(self, local_sha: str, remote_sha: str, ref: str = "refs/heads/main") -> str:
        return f"{ref} {local_sha} {ref} {remote_sha}\n"

    def test_prepush_uses_real_gitleaks_and_blocks_high(self) -> None:
        """把會被真實 Gitleaks 命中的 token 推出去，必須有 high 發現且退出 1。"""
        if not HAVE_REAL_GITLEAKS:
            self.skipTest(f"找不到可執行的真實 Gitleaks：{REAL_GITLEAKS}")
        repo = self.make_repo("prepush-real")
        repo.write("seed.txt", "seed\n", stage=True)
        base = repo.commit("seed")
        repo.write("leak.txt", f"token={SYNTHETIC_GITUB_PAT}\n", stage=True)
        head = repo.commit("add token")

        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=self.push_line(head, base),
            gitleaks=str(REAL_GITLEAKS),
        )
        report = result.json
        engines = {engine["id"]: engine for engine in report["coverage"]["engines"]}
        self.assertTrue(engines["gitleaks"]["available"], engines["gitleaks"])
        self.assertEqual(engines["gitleaks"]["version"], "8.30.1")
        self.assertEqual(engines["gitleaks"]["commits"], 1, "Gitleaks 必須只掃實際選取的 commit")
        self.assertEqual(engines["gitleaks"]["batches"], 1)
        self.assertTrue(
            any(f["ruleId"] == "github-pat" for f in report["findings"]),
            f"真實 Gitleaks 應該用 github-pat 規則命中：{report['findings']}",
        )
        self.assertEqual(report["status"], "findings")
        self.assertEqual(result.returncode, 1)
        self.secrets_in(result, [SYNTHETIC_GITUB_PAT])

    def test_prepush_clean_passes_with_real_gitleaks(self) -> None:
        if not HAVE_REAL_GITLEAKS:
            self.skipTest(f"找不到可執行的真實 Gitleaks：{REAL_GITLEAKS}")
        repo = self.make_repo("prepush-clean")
        repo.write("clean.txt", "nothing sensitive\n", stage=True)
        base = repo.commit("seed")
        repo.write("more.txt", "still clean\n", stage=True)
        head = repo.commit("second")

        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=self.push_line(head, base),
            gitleaks=str(REAL_GITLEAKS),
        )
        report = result.json
        self.assertEqual(report["status"], "passed", report["errors"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["summary"]["findings"], 0)
        self.assertEqual(report["coverage"]["scope"]["commits"], 1)

    def test_prepush_without_gitleaks_always_exits_2(self) -> None:
        """缺 Gitleaks 時，不管有沒有新 commit 都是 incomplete / 退出 2。"""
        repo = self.make_repo("prepush-missing")
        repo.write("clean.txt", "nothing sensitive\n", stage=True)
        head = repo.commit("only commit")
        zero = "0" * 40

        with_new_commit = self.audit_with(
            ["--pre-push"], repo, self.quiet_policy(), stdin=self.push_line(head, zero)
        )
        self.assertEqual(with_new_commit.json["status"], "incomplete")
        self.assertEqual(with_new_commit.returncode, 2)

        # 沒有任何新 commit（推送集合為空）也不能假裝通過
        empty = self.audit_with(
            ["--pre-push"], repo, self.quiet_policy(), stdin=self.push_line(head, head)
        )
        self.assertEqual(empty.json["status"], "incomplete")
        self.assertEqual(empty.returncode, 2)
        self.assertEqual(empty.json["coverage"]["scope"]["commits"], 0)
        self.assertTrue(
            any("Gitleaks" in message for message in empty.json["errors"]),
            empty.json["errors"],
        )

    def test_prepush_empty_stdin_is_not_a_pass(self) -> None:
        repo = self.make_repo("prepush-empty-stdin")
        repo.write("a.txt", "a\n", stage=True)
        repo.commit("a")
        result = self.audit_with(["--pre-push"], repo, self.quiet_policy(), stdin="")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.json["status"], "incomplete")
        self.assertEqual(result.json["coverage"]["scope"]["refs"], [])

    def test_prepush_multi_ref_union_is_not_swallowed_by_other_ref(self) -> None:
        """逐 ref 計算再 union：A ref 的遠端進度不能把 B ref 的新 commit 排除掉。

        情境：遠端 main 只有 c1、但遠端 feature 已經走到 c3；這次要推 main=c3、
        feature=f1。若用單一 `rev-list main feature ^c1 ^c3`，c2 會被 ^c3 排除，
        c2 裡的秘密就漏掉了。
        """
        if not HAVE_REAL_GITLEAKS:
            self.skipTest(f"找不到可執行的真實 Gitleaks：{REAL_GITLEAKS}")
        repo = self.make_repo("prepush-multi")
        repo.write("f1.txt", "c1\n", stage=True)
        c1 = repo.commit("c1")
        repo.write("mainsecret.txt", f"token={SYNTHETIC_GITUB_PAT}\n", stage=True)
        c2 = repo.commit("c2 with secret")
        repo.write("f3.txt", "c3\n", stage=True)
        c3 = repo.commit("c3")
        repo.git("checkout", "-q", "-b", "feature")
        repo.write("feature1.txt", "f1\n", stage=True)
        feature = repo.commit("f1")

        # 對照組：如果用錯的單一 rev-list，c2 會消失
        wrong = repo.git("rev-list", f"{c3}", f"{feature}", f"^{c1}", f"^{c3}").split()
        self.assertNotIn(c2, wrong, "這個 fixture 必須能讓錯誤做法漏掉 c2，否則測試沒有意義")

        stdin = (
            f"refs/heads/main {c3} refs/heads/main {c1}\n"
            f"refs/heads/feature {feature} refs/heads/feature {c3}\n"
        )
        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=stdin,
            gitleaks=str(REAL_GITLEAKS),
        )
        report = result.json
        self.assertEqual(report["coverage"]["scope"]["commits"], 3)
        self.assertEqual(
            report["coverage"]["scope"]["commitsPerRef"],
            {"refs/heads/main -> refs/heads/main": 2, "refs/heads/feature -> refs/heads/feature": 1},
        )
        self.assertIn("mainsecret.txt", result.paths())
        self.assertEqual(result.returncode, 1)
        self.secrets_in(result, [SYNTHETIC_GITUB_PAT])

    def test_prepush_missing_remote_sha_scans_full_history(self) -> None:
        repo = self.make_repo("prepush-missing-remote")
        repo.write("seed.txt", "seed\n", stage=True)
        repo.commit("seed")
        repo.write("leak.txt", f"token={SYNTHETIC_GITUB_PAT}\n", stage=True)
        head = repo.commit("add token")
        unknown_remote = "1234567890abcdef1234567890abcdef12345678"

        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=f"refs/heads/main {head} refs/heads/main {unknown_remote}\n",
        )
        scope = result.json["coverage"]["scope"]
        self.assertEqual(scope["commits"], 2, "遠端物件不在本機時要掃整條 local 歷史")
        self.assertEqual(scope["remoteShaMissing"], ["refs/heads/main -> refs/heads/main"])
        self.assertIn("leak.txt", result.paths())

    def test_prepush_deleted_ref_is_counted_and_scans_nothing(self) -> None:
        repo = self.make_repo("prepush-deleted")
        repo.write("a.txt", "a\n", stage=True)
        head = repo.commit("a")
        zero = "0" * 40
        result = self.audit_with(
            ["--pre-push"], repo, self.quiet_policy(), stdin=f"refs/heads/gone {zero} refs/heads/gone {head}\n"
        )
        scope = result.json["coverage"]["scope"]
        self.assertEqual(scope["deletedRefs"], 1)
        self.assertEqual(scope["pushedRefs"], 0)
        self.assertEqual(scope["commits"], 0)
        self.assertEqual(result.returncode, 2, "缺 Gitleaks 仍然是不完整")

    def test_prepush_broken_gitleaks_is_incomplete(self) -> None:
        repo = self.make_repo("prepush-broken-gl")
        repo.write("seed.txt", "seed\n", stage=True)
        base = repo.commit("seed")
        repo.write("a.txt", "a\n", stage=True)
        head = repo.commit("a")
        broken = write_script(
            self.tool_dir / "gitleaks-broken",
            "#!/usr/bin/env bash\n"
            'if [ "${1:-}" = "version" ]; then echo "8.99.0-fake"; exit 0; fi\n'
            'echo "unknown flag (synthetic)" >&2\n'
            "exit 126\n",
        )
        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=f"refs/heads/main {head} refs/heads/main {base}\n",
            gitleaks=str(broken),
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.json["status"], "incomplete")
        self.assertTrue(any("gitleaks" in message.lower() for message in result.json["errors"]))

    def test_prepush_gitleaks_non_dict_report_entries_are_incomplete(self) -> None:
        """報告裡有非物件項目時不能被濾掉後當成「沒有發現」。"""
        repo = self.make_repo("prepush-gl-nondict")
        repo.write("seed.txt", "seed\n", stage=True)
        base = repo.commit("seed")
        repo.write("a.txt", "a\n", stage=True)
        head = repo.commit("a")
        liar = write_script(
            self.tool_dir / "gitleaks-nondict",
            "#!/usr/bin/env bash\n"
            'if [ "${1:-}" = "version" ]; then echo "8.99.0-fake"; exit 0; fi\n'
            'report=""\n'
            "while [ $# -gt 0 ]; do\n"
            '  case $1 in --report-path) report=$2; shift 2 ;; --exit-code) shift 2 ;; *) shift ;; esac\n'
            "done\n"
            'printf "[42]\\n" > "$report"\n'
            "exit 0\n",
        )
        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=f"refs/heads/main {head} refs/heads/main {base}\n",
            gitleaks=str(liar),
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.json["status"], "incomplete")
        self.assertEqual(result.json["summary"]["findings"], 0)
        self.assertTrue(any("報告" in message for message in result.json["errors"]), result.json["errors"])

    def test_prepush_gitleaks_leak_exit_with_empty_report_is_incomplete(self) -> None:
        """退出碼宣稱「找到洩漏」但報告是空的 → 不可視為乾淨。"""
        repo = self.make_repo("prepush-gl-empty99")
        repo.write("seed.txt", "seed\n", stage=True)
        base = repo.commit("seed")
        repo.write("a.txt", "a\n", stage=True)
        head = repo.commit("a")
        liar = write_script(
            self.tool_dir / "gitleaks-empty99",
            "#!/usr/bin/env bash\n"
            'if [ "${1:-}" = "version" ]; then echo "8.99.0-fake"; exit 0; fi\n'
            'report=""\n'
            "while [ $# -gt 0 ]; do\n"
            '  case $1 in --report-path) report=$2; shift 2 ;; --exit-code) leak=$2; shift 2 ;; *) shift ;; esac\n'
            "done\n"
            'printf "[]\\n" > "$report"\n'
            'exit "${leak:-99}"\n',
        )
        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=f"refs/heads/main {head} refs/heads/main {base}\n",
            gitleaks=str(liar),
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.json["status"], "incomplete")
        self.assertEqual(result.json["summary"]["findings"], 0)
        self.assertTrue(
            any("空" in message or "洩漏" in message for message in result.json["errors"]),
            result.json["errors"],
        )

    def test_prepush_gitleaks_version_probe_failure_is_incomplete(self) -> None:
        repo = self.make_repo("prepush-gl-probe")
        repo.write("a.txt", "a\n", stage=True)
        head = repo.commit("a")
        broken = write_script(
            self.tool_dir / "gitleaks-probe",
            '#!/usr/bin/env bash\nif [ "${1:-}" = "version" ]; then echo "no version here"; exit 0; fi\nexit 0\n',
        )
        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=f"refs/heads/main {head} refs/heads/main {head}\n",
            gitleaks=str(broken),
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(any("版本" in message for message in result.json["errors"]), result.json["errors"])

    def test_prepush_gitleaks_missing_report_is_incomplete(self) -> None:
        """Gitleaks 回 0 卻沒寫報告 → 不可視為乾淨。"""
        repo = self.make_repo("prepush-gl-noreport")
        repo.write("seed.txt", "seed\n", stage=True)
        base = repo.commit("seed")
        repo.write("a.txt", "a\n", stage=True)
        head = repo.commit("a")
        liar = write_script(
            self.tool_dir / "gitleaks-liar",
            '#!/usr/bin/env bash\n'
            'if [ "${1:-}" = "version" ]; then echo "8.99.0-fake"; exit 0; fi\n'
            "exit 0\n",  # 不寫任何報告
        )
        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=f"refs/heads/main {head} refs/heads/main {base}\n",
            gitleaks=str(liar),
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(any("報告" in message for message in result.json["errors"]), result.json["errors"])

    def test_prepush_gitleaks_invalid_report_is_incomplete(self) -> None:
        repo = self.make_repo("prepush-gl-badreport")
        repo.write("seed.txt", "seed\n", stage=True)
        base = repo.commit("seed")
        repo.write("a.txt", "a\n", stage=True)
        head = repo.commit("a")
        bad = write_script(
            self.tool_dir / "gitleaks-badreport",
            "#!/usr/bin/env bash\n"
            'if [ "${1:-}" = "version" ]; then echo "8.99.0-fake"; exit 0; fi\n'
            'report=""\n'
            'while [ $# -gt 0 ]; do\n'
            '  case $1 in --report-path) report=$2; shift 2 ;; *) shift ;; esac\n'
            "done\n"
            'printf "not json at all" > "$report"\n'
            "exit 0\n",
        )
        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=f"refs/heads/main {head} refs/heads/main {base}\n",
            gitleaks=str(bad),
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(any("報告" in message for message in result.json["errors"]), result.json["errors"])


# ---------------------------------------------------------------------------
# merge / root commit
# ---------------------------------------------------------------------------


class TestCommitCoverage(CoreTestCase):
    def test_root_commit_content_is_scanned(self) -> None:
        repo = self.make_repo("root-commit")
        repo.write("root.txt", f"token={SYNTHETIC_GITUB_PAT}\n", stage=True)
        repo.commit("root with secret")
        result = self.audit_with(["--history"], repo, self.quiet_policy())
        self.assertEqual(result.json["coverage"]["scope"]["commits"], 1)
        self.assertIn("github-token", result.rule_ids())
        self.assertIn("root.txt", result.paths())
        self.secrets_in(result, [SYNTHETIC_GITUB_PAT])

    def test_merge_commit_uses_a_real_diff(self) -> None:
        """merge commit 必須真的用 diff 掃描。

        秘密只出現在 merge 的衝突解決結果裡，其他 commit 的 diff 都沒有那行。
        """
        repo = self.make_repo("merge-diff")
        repo.write("merged.txt", "main side\n", stage=True)
        repo.commit("base")
        repo.git("checkout", "-q", "-b", "side")
        repo.write("merged.txt", "side side\n", stage=True)
        repo.commit("side edits merged.txt")
        repo.git("checkout", "-q", "main")
        repo.git("merge", "side")
        repo.write("merged.txt", f"resolved token={SYNTHETIC_GITUB_PAT}\n")
        repo.git("add", "merged.txt")
        repo.git("commit", "-q", "-m", "merge side")
        merge_sha = repo.head_sha()

        result = self.audit_with(["--history"], repo, self.quiet_policy())
        merge_findings = [f for f in result.json["findings"] if f["location"].get("commit") == merge_sha]
        self.assertTrue(
            merge_findings,
            f"merge commit {merge_sha[:8]} 的 diff 沒有被掃到（只有 merge 才有那行秘密）",
        )
        self.assertIn("github-token", result.rule_ids())
        self.assertEqual(result.json["coverage"]["scope"]["commits"], 3)
        self.secrets_in(result, [SYNTHETIC_GITUB_PAT])

    def test_treesame_merge_is_still_scanned(self) -> None:
        """樹與某個 parent 相同的 merge（git 預設會略過）也要有 diff 可掃。

        git 只有在 merge 的樹與所有 parent 都不同時才輸出 diff；把長期分支合回來的
        merge 常常與其中一個 parent 的樹相同，預設會整顆沒有內容可比對。
        """
        repo = self.make_repo("merge-treesame")
        repo.write("base.txt", "base\n", stage=True)
        repo.commit("base")
        repo.git("checkout", "-q", "-b", "side")
        repo.write("side_secret.txt", f"token={SYNTHETIC_GITUB_PAT}\n", stage=True)
        repo.commit("side secret")
        repo.git("checkout", "-q", "main")
        repo.git("merge", "-q", "--no-ff", "side", "-m", "merge side")
        merge_sha = repo.head_sha()
        side_tree = repo.git("rev-parse", "side^{tree}").strip()
        merge_tree = repo.git("rev-parse", f"{merge_sha}^{{tree}}").strip()
        self.assertEqual(merge_tree, side_tree, "這個 fixture 必須是 TREESAME merge 才測得到重點")

        result = self.audit_with(["--history"], repo, self.quiet_policy())
        commits_with_findings = {
            f["location"].get("commit") for f in result.json["findings"] if f["ruleId"] == "github-token"
        }
        self.assertIn(merge_sha, commits_with_findings, "TREESAME merge 本身也要被掃到")
        self.assertIn("side_secret.txt", result.paths())
        self.secrets_in(result, [SYNTHETIC_GITUB_PAT])

    def test_range_uses_explicit_commit_list(self) -> None:
        if not HAVE_REAL_GITLEAKS:
            self.skipTest(f"找不到可執行的真實 Gitleaks：{REAL_GITLEAKS}")
        repo = self.make_repo("range-list")
        repo.write("a.txt", "a\n", stage=True)
        base = repo.commit("a")
        repo.write("b.txt", "b\n", stage=True)
        head = repo.commit("b")
        result = self.audit_with(
            ["--range", f"{base}..{head}"],
            repo,
            self.quiet_policy(),
            gitleaks=str(REAL_GITLEAKS),
        )
        engines = {e["id"]: e for e in result.json["coverage"]["engines"]}
        self.assertEqual(engines["gitleaks"]["commits"], 1)
        self.assertEqual(result.json["coverage"]["scope"]["commits"], 1)


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


class TestMetadata(CoreTestCase):
    def test_committer_only_phone_email_is_high(self) -> None:
        repo = self.make_repo("meta-phone")
        repo.write("a.txt", "a\n", stage=True)
        repo.git(
            "commit",
            "-q",
            "-m",
            "plain message",
            env={
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
                "GIT_COMMITTER_NAME": "Synthetic Committer",
                "GIT_COMMITTER_EMAIL": "0912345678@example.invalid",
            },
        )
        result = self.audit_with(["--history"], repo, self.quiet_policy())
        rules = result.rule_ids()
        self.assertIn("commit-metadata-phone-email", rules)
        phone = [f for f in result.json["findings"] if f["ruleId"] == "commit-metadata-phone-email"]
        self.assertEqual(phone[0]["severity"], "high", "電話型 email 預設 high")
        # 沒有 Gitleaks 時掃描不完整 → 退出 2 優先於 1（不可假稱完整掃描）。
        self.assertEqual(result.json["status"], "incomplete")
        self.assertEqual(result.returncode, 2)
        self.secrets_in(result, ["0912345678@example.invalid"])

    def test_noreply_and_allowed_emails_are_not_reported(self) -> None:
        repo = self.make_repo("meta-noreply")
        repo.write("a.txt", "a\n", stage=True)
        repo.git(
            "commit",
            "-q",
            "-m",
            "plain",
            env={
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": "1234+synthetic@users.noreply.github.com",
                "GIT_COMMITTER_NAME": "Synthetic Committer",
                "GIT_COMMITTER_EMAIL": "keeper@example.invalid",
            },
        )
        policy = self.quiet_policy()
        policy["metadata"] = {
            "emailSeverity": "low",
            "scanCommitMessage": False,
            "allowedEmails": ["keeper@*"],
        }
        result = self.audit_with(["--history"], repo, policy)
        self.assertNotIn("commit-metadata-email", result.rule_ids())
        self.assertNotIn("commit-metadata-phone-email", result.rule_ids())

    # 合成 fixture 用字串：只放 demo 用的假號碼，不含任何真實使用者資料。
    PHONE_PREFIX_EMAILS = [
        "demo0912345678@example.invalid",
        "j0912345678@example.invalid",
        "phone0912345678work@example.invalid",
        "09-1234-5678@example.invalid",
    ]

    def test_staged_gitvar_detects_alphabetic_prefix_phone(self) -> None:
        """--staged 讀 git var 時，字母前綴的電話型 email 也要擋（PM e2e 失敗案例）。"""
        for email in self.PHONE_PREFIX_EMAILS:
            with self.subTest(email=email):
                repo = self.make_repo("staged-prefix")
                repo.write("a.txt", "a\n", stage=True)
                result = self._audit_staged_with_ident(
                    repo,
                    self.quiet_policy(),
                    {"GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email},
                )
                phone = [f for f in result.json["findings"] if f["ruleId"] == "commit-metadata-phone-email"]
                self.assertTrue(phone, f"{email} 應被判定為電話型 email")
                self.assertEqual(phone[0]["severity"], "high", email)
                self.secrets_in(result, [email])

    def test_staged_gitvar_detects_committer_only_prefix_phone(self) -> None:
        """只有 committer 是電話型（author 正常）時也要擋。"""
        repo = self.make_repo("staged-committer-prefix")
        repo.write("a.txt", "a\n", stage=True)
        result = self._audit_staged_with_ident(
            repo,
            self.quiet_policy(),
            {
                "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
                "GIT_COMMITTER_EMAIL": "demo0912345678@example.invalid",
            },
        )
        phone = [f for f in result.json["findings"] if f["ruleId"] == "commit-metadata-phone-email"]
        self.assertTrue(phone)
        self.assertEqual(phone[0]["severity"], "high")
        self.secrets_in(result, ["demo0912345678@example.invalid"])

    def test_history_detects_prefix_phone_in_commit_metadata(self) -> None:
        """已落地的 commit metadata 同樣要擋（author 或 committer 皆可）。"""
        repo = self.make_repo("history-prefix")
        repo.write("a.txt", "a\n", stage=True)
        repo.git(
            "commit",
            "-q",
            "-m",
            "synthetic demo",
            env={
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": "phone0912345678work@example.invalid",
                "GIT_COMMITTER_NAME": "Synthetic Author",
                "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            },
        )
        result = self.audit_with(["--history"], repo, self.quiet_policy())
        phone = [f for f in result.json["findings"] if f["ruleId"] == "commit-metadata-phone-email"]
        self.assertTrue(phone, "字母前綴的電話型 email 必須被判定")
        self.assertEqual(phone[0]["severity"], "high")
        self.secrets_in(result, ["phone0912345678work@example.invalid"])

    def test_prepush_detects_prefix_phone(self) -> None:
        """pre-push 掃描將要推送的 commit metadata 時也要擋。"""
        repo = self.make_repo("prepush-prefix")
        repo.write("seed.txt", "seed\n", stage=True)
        base = repo.commit("seed")
        repo.git(
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "synthetic demo",
            env={
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": "demo0912345678@example.invalid",
                "GIT_COMMITTER_NAME": "Synthetic Author",
                "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            },
        )
        head = repo.head_sha()
        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=f"refs/heads/main {head} refs/heads/main {base}\n",
        )
        phone = [f for f in result.json["findings"] if f["ruleId"] == "commit-metadata-phone-email"]
        self.assertTrue(phone, "pre-push 也要擋字母前綴的電話型 email")
        self.assertEqual(phone[0]["severity"], "high")
        self.secrets_in(result, ["demo0912345678@example.invalid"])

    def test_short_digit_runs_are_not_phone(self) -> None:
        """短年份與 4 位數字不能被誤判成 high。"""
        core = load_core()
        for local in ("user1234", "birth1987", "a1b2c3", "demo123456"):
            self.assertFalse(
                core.looks_like_phone_email(f"{local}@example.invalid"),
                f"{local} 只有 6 位以下數字，不該算電話",
            )
        repo = self.make_repo("short-digits")
        repo.write("a.txt", "a\n", stage=True)
        repo.git(
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "synthetic short digits",
            env={
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": "user1234@example.invalid",
                "GIT_COMMITTER_NAME": "Synthetic Author",
                "GIT_COMMITTER_EMAIL": "birth1987@example.invalid",
            },
        )
        result = self.audit_with(["--history"], repo, self.quiet_policy())
        self.assertNotIn("commit-metadata-phone-email", result.rule_ids())

    def test_unknown_noreply_domain_cannot_bypass(self) -> None:
        """『字串裡有 noreply』不能當成繞過；只有已知 domain 才略過。"""
        core = load_core()
        self.assertTrue(core.is_known_noreply_email("1234+demo@users.noreply.github.com", core.DEFAULT_KNOWN_NOREPLY_DOMAINS))
        for email in (
            "demo0912345678@noreply-example.invalid",
            "noreply0912345678@example.invalid",
            "demo@noreply.github.com.evil.invalid",
        ):
            self.assertFalse(
                core.is_known_noreply_email(email, core.DEFAULT_KNOWN_NOREPLY_DOMAINS),
                email,
            )

        for email in ("demo0912345678@noreply-example.invalid", "noreply0912345678@example.invalid"):
            with self.subTest(email=email):
                repo = self.make_repo("fake-noreply")
                repo.write("a.txt", "a\n", stage=True)
                repo.git(
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "synthetic demo",
                    env={
                        "GIT_AUTHOR_NAME": "Synthetic Author",
                        "GIT_AUTHOR_EMAIL": email,
                        "GIT_COMMITTER_NAME": "Synthetic Author",
                        "GIT_COMMITTER_EMAIL": email,
                    },
                )
                result = self.audit_with(["--history"], repo, self.quiet_policy())
                phone = [f for f in result.json["findings"] if f["ruleId"] == "commit-metadata-phone-email"]
                self.assertTrue(phone, f"{email} 不得被 no-reply 規則跳過")
                self.assertEqual(phone[0]["severity"], "high")
                self.secrets_in(result, [email])

    def test_known_noreply_domain_still_skipped(self) -> None:
        """已知的 GitHub 匿名提交 domain 仍然照舊略過。"""
        repo = self.make_repo("known-noreply")
        repo.write("a.txt", "a\n", stage=True)
        repo.git(
            "commit",
            "-q",
            "-m",
            "plain",
            env={
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": "1234+synthetic@users.noreply.github.com",
                "GIT_COMMITTER_NAME": "Synthetic Author",
                "GIT_COMMITTER_EMAIL": "1234+synthetic@users.noreply.github.com",
            },
        )
        result = self.audit_with(["--history"], repo, self.quiet_policy())
        self.assertNotIn("commit-metadata-email", result.rule_ids())
        self.assertNotIn("commit-metadata-phone-email", result.rule_ids())

    def test_github_noreply_machine_id_is_not_a_phone(self) -> None:
        """GitHub 匿名身份 <數字ID>+<login>：機器 ID 不算電話，login 段照常檢查。"""
        cases = [
            ("123456789+demo0912345678@users.noreply.github.com", True),  # login 段含電話
            ("0912345678@users.noreply.github.com", True),  # 沒有 '+' → 整串檢查
            ("123456789+demo@users.noreply.github.com", False),  # 合法 GitHub 身份
            ("123456789+demo@noreply-example.invalid", True),  # 非白名單 domain
        ]
        for email, expected in cases:
            with self.subTest(email=email):
                repo = self.make_repo("gh-noreply-id")
                repo.write("a.txt", "a\n", stage=True)
                repo.git(
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "synthetic demo",
                    env={
                        "GIT_AUTHOR_NAME": "Synthetic Author",
                        "GIT_AUTHOR_EMAIL": email,
                        "GIT_COMMITTER_NAME": "Synthetic Author",
                        "GIT_COMMITTER_EMAIL": email,
                    },
                )
                result = self.audit_with(["--history"], repo, self.quiet_policy())
                phone = [f for f in result.json["findings"] if f["ruleId"] == "commit-metadata-phone-email"]
                self.assertEqual(bool(phone), expected, f"{email} 應判定為 phone={expected}")
                if expected:
                    self.assertEqual(phone[0]["severity"], "high", email)
                self.secrets_in(result, [email])

    def test_github_noreply_login_shape_is_validated(self) -> None:
        """只有合法 GitHub login 形狀才走機器 ID 例外；其餘一律整串檢查。"""
        core = load_core()
        # 合法：字母數字連字號，長度 1..39
        self.assertEqual(core._phone_scan_target("123456789+demo@users.noreply.github.com"), "demo")
        self.assertEqual(
            core._phone_scan_target("123456789+a-b-c-9@users.noreply.github.com"), "a-b-c-9"
        )
        # 太長（40 字元）、含底線/開頭結尾連字號 → 不符形狀 → 整串（含 ID）都檢查
        too_long = "a" * 40
        self.assertEqual(
            core._phone_scan_target(f"123456789+{too_long}@users.noreply.github.com"),
            f"123456789+{too_long}",
        )
        self.assertEqual(
            core._phone_scan_target("123456789+demo_name@users.noreply.github.com"),
            "123456789+demo_name",
        )
        # 換行不得被當成合法 login
        self.assertEqual(
            core._phone_scan_target("123456789+demo\n@users.noreply.github.com"),
            "123456789+demo\n",
        )
        # 非 GitHub domain 沒有這個例外
        self.assertEqual(core._phone_scan_target("123456789+demo@example.invalid"), "123456789+demo")

    def test_known_noreply_domain_still_flags_phone(self) -> None:
        """即使是已知 no-reply domain，含電話型數字仍然要 high（no-reply 不得蓋掉電話）。"""
        email = "0912345678@users.noreply.github.com"
        repo = self.make_repo("known-noreply-phone")
        repo.write("a.txt", "a\n", stage=True)
        repo.git(
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "synthetic demo",
            env={
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": "Synthetic Author",
                "GIT_COMMITTER_EMAIL": email,
            },
        )
        result = self.audit_with(["--history"], repo, self.quiet_policy())
        phone = [f for f in result.json["findings"] if f["ruleId"] == "commit-metadata-phone-email"]
        self.assertTrue(phone, "no-reply 規則不得跳過含電話型數字的信箱")
        self.assertEqual(phone[0]["severity"], "high")
        self.secrets_in(result, [email])

    def test_allowed_emails_can_exempt_phone_email(self) -> None:
        """政策可用 allowedEmails 明確免除（PM 指定的逃生門）。"""
        repo = self.make_repo("allowed-phone")
        repo.write("a.txt", "a\n", stage=True)
        email = "demo0912345678@example.invalid"
        repo.git(
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "synthetic demo",
            env={
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": "Synthetic Author",
                "GIT_COMMITTER_EMAIL": email,
            },
        )
        policy = self.quiet_policy()
        policy["metadata"] = {"emailSeverity": "low", "scanCommitMessage": False, "allowedEmails": [email]}
        result = self.audit_with(["--history"], repo, policy)
        self.assertNotIn("commit-metadata-phone-email", result.rule_ids())
        self.assertNotIn("commit-metadata-email", result.rule_ids())

    def test_metadata_off_still_reports_high_secret_in_commit_message(self) -> None:
        """--no-metadata 不能把 commit 訊息裡的高風險秘密一起藏起來。"""
        repo = self.make_repo("meta-off")
        repo.write("a.txt", "a\n", stage=True)
        repo.git("commit", "-q", "-m", f"rotated token {SYNTHETIC_AWS_KEY}")
        policy = self.quiet_policy()
        policy["metadata"] = {"enabled": False, "emailSeverity": "off", "scanCommitMessage": True}
        result = self.audit_with(["--history"], repo, policy)
        self.assertIn("aws-access-key-id", result.rule_ids())
        # 缺 Gitleaks → 不完整掃描（2）優先於有 high 發現（1）；重點是規則沒被藏起來。
        self.assertEqual(result.json["status"], "incomplete")
        self.assertEqual(result.returncode, 2)
        self.assertGreaterEqual(result.json["summary"]["high"], 1)
        self.assertFalse(result.json["policy"]["metadataEnabled"])
        self.secrets_in(result, [SYNTHETIC_AWS_KEY])

    def test_no_metadata_flag_still_scans_index_content(self) -> None:
        """--no-metadata 只是關掉身分規則，index 內容裡的 high 秘密照樣要報。"""
        repo = self.make_repo("meta-off-staged")
        repo.write("a.txt", f"token={SYNTHETIC_GITUB_PAT}\n", stage=True)
        policy = self.quiet_policy()
        policy["metadata"] = {"enabled": False, "emailSeverity": "off", "scanCommitMessage": False}
        policy_path = repo.write_policy(policy)
        result = self.audit(
            [
                "--staged",
                "--repo",
                str(repo.root),
                "--policy",
                str(policy_path),
                "--no-metadata",
                "--format",
                "json",
            ],
            repo=repo,
        )
        self.assertIn("github-token", result.rule_ids())
        self.assertFalse(result.json["policy"]["metadataEnabled"])
        self.assertEqual(result.json["summary"]["high"], 1)
        self.secrets_in(result, [SYNTHETIC_GITUB_PAT])

    def test_commit_message_scanning_can_be_disabled_by_policy(self) -> None:
        """政策明確關掉訊息掃描時允許，但要在限制裡說清楚，不當成完整掃描。"""
        repo = self.make_repo("meta-msg-off")
        repo.write("a.txt", "a\n", stage=True)
        repo.git("commit", "-q", "-m", f"rotated token {SYNTHETIC_AWS_KEY}")
        result = self.audit_with(["--history"], repo, self.quiet_policy())
        limits = " ".join(result.json["coverage"]["limitations"])
        self.assertIn("scanCommitMessage=false", limits)
        self.assertNotIn("aws-access-key-id", result.rule_ids())

    def test_staged_pending_ident_is_scanned(self) -> None:
        """pre-commit 時還沒落地的 author email 也要能被擋下來。"""
        repo = self.make_repo("meta-pending")
        repo.write("a.txt", "a\n", stage=True)
        policy = self.quiet_policy()
        policy["metadata"] = {"emailSeverity": "off", "scanCommitMessage": False}
        result = self._audit_staged_with_ident(repo, policy)
        self.assertIn("commit-metadata-phone-email", result.rule_ids())
        phone = [f for f in result.json["findings"] if f["ruleId"] == "commit-metadata-phone-email"]
        self.assertEqual(phone[0]["severity"], "high")
        self.secrets_in(result, ["0911222333@example.invalid"])

    def _audit_staged_with_ident(
        self,
        repo: GitRepoFixture,
        policy: Dict[str, Any],
        ident: Optional[Dict[str, str]] = None,
    ) -> CliResult:
        policy_path = repo.write_policy(policy)
        base = dict(os.environ)
        for key in ("REPO_PRIVACY_AUDIT_GITLEAKS", "REPO_PRIVACY_AUDIT_GH", "REPO_PRIVACY_AUDIT_POLICY"):
            base.pop(key, None)
        base.update(self.env)
        base.update(
            {
                "HOME": str(repo.root / "home"),
                "XDG_CONFIG_HOME": str(repo.root / "home" / ".config"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": "0911222333@example.invalid",
                "GIT_COMMITTER_NAME": "Synthetic Committer",
                "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            }
        )
        if ident:
            base.update(ident)
        proc = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "skills" / "repo-privacy-audit" / "scripts" / "repo-privacy-audit.sh"),
                "--staged",
                "--repo",
                str(repo.root),
                "--policy",
                str(policy_path),
                "--format",
                "json",
            ],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=base,
            check=False,
        )
        return CliResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
            ["staged"],
        )


# ---------------------------------------------------------------------------
# paths / policy
# ---------------------------------------------------------------------------


class TestPathPolicy(CoreTestCase):
    def test_sensitive_filenames_flag_but_templates_and_docs_are_not(self) -> None:
        repo = self.make_repo("paths")
        repo.write(".env", "DB_PASSWORD=synthetic-placeholder\n", stage=True)
        repo.write(".env.example", f"SECRET={SYNTHETIC_AWS_KEY}\n", stage=True)
        repo.write("server.key", "synthetic\n", stage=True)
        repo.write("AGENTS.md", "# 合成說明\n", stage=True)
        repo.write("docs/plan.md", "合成計畫。\n", stage=True)
        result = self.audit_with(["--staged"], repo, self.quiet_policy())
        report = result.json

        flagged = [f["location"]["path"] for f in report["findings"] if f["ruleId"] == "sensitive-filename"]
        self.assertIn(".env", flagged)
        self.assertIn("server.key", flagged)
        self.assertNotIn(".env.example", flagged, "範本檔不該按檔名擋")
        self.assertNotIn("AGENTS.md", flagged)
        self.assertNotIn("docs/plan.md", flagged)
        for finding in report["findings"]:
            if finding["ruleId"] == "sensitive-filename":
                self.assertEqual(finding["severity"], "high")

        # .env.example 的內容仍然要掃
        self.assertIn("aws-access-key-id", result.rule_ids())
        self.assertIn(".env.example", result.paths())
        self.assertIn("credential-assignment", result.rule_ids())
        self.assertIn(".env", result.paths())
        self.secrets_in(result, [SYNTHETIC_AWS_KEY])

    def test_personal_local_path_is_medium(self) -> None:
        repo = self.make_repo("personal-path")
        repo.write("notes.md", "失敗紀錄：/Users/synthetic-user/project/log.txt\n", stage=True)
        repo.write("ci.md", "runner: /home/runner/work/x\n", stage=True)
        result = self.audit_with(["--staged"], repo, self.quiet_policy())
        findings = [f for f in result.json["findings"] if f["ruleId"] == "personal-local-path"]
        self.assertTrue(findings)
        self.assertEqual(findings[0]["severity"], "medium")
        self.assertNotIn("ci.md", result.paths(), "允許清單裡的 CI 路徑不算個資")
        self.secrets_in(result, ["/Users/synthetic-user/"])

    def test_secret_in_filename_is_scrubbed_from_report(self) -> None:
        repo = self.make_repo("filename-scrub")
        folder = repo.root / SYNTHETIC_GITUB_PAT
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "notes.md").write_text(f"token={SYNTHETIC_GITUB_PAT}\n", encoding="utf-8")
        repo.git("add", "-A", "-f", ".")
        result = self.audit_with(["--staged"], repo, self.quiet_policy())
        self.assertTrue(result.json["findings"])
        self.assertIn("github-token", result.rule_ids())
        self.secrets_in(result, [SYNTHETIC_GITUB_PAT])
        paths = result.paths()
        self.assertTrue(any("[REDACTED]" in str(path) for path in paths), paths)

    def test_staged_large_file_is_incomplete_not_a_warning(self) -> None:
        repo = self.make_repo("large-file")
        repo.write("big.txt", "x" * 5000 + "\n", stage=True)
        policy = self.quiet_policy()
        policy["paths"] = {"maxFileBytes": 1024}
        result = self.audit_with(["--staged"], repo, policy)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.json["status"], "incomplete")
        self.assertTrue(any("maxFileBytes" in message for message in result.json["errors"]))


class TestInvalidPolicy(CoreTestCase):
    def assertPolicyRejected(self, policy: Dict[str, Any], needle: str) -> None:
        repo = self.make_repo("bad-policy")
        repo.write("a.txt", "a\n", stage=True)
        result = self.audit_with(["--staged"], repo, policy)
        self.assertExitCode(result, 2)
        self.assertEqual(result.json["status"], "error")
        blob = json.dumps(result.json, ensure_ascii=False)
        self.assertIn(needle, blob, f"政策錯誤訊息應說明原因：{result.json['errors']}")

    def test_string_false_is_not_accepted_as_boolean(self) -> None:
        repo = self.make_repo("bad-bool")
        repo.write("a.txt", "a\n", stage=True)
        result = self.audit_with(
            ["--staged"], repo, {"metadata": {"scanCommitMessage": "false"}}
        )
        self.assertExitCode(result, 2)
        self.assertEqual(result.json["status"], "error")
        self.assertIn("true 或 false", " ".join(result.json["errors"]))

    def test_pii_enabled_must_be_boolean(self) -> None:
        self.assertPolicyRejected({"pii": {"enabled": "no"}}, "true 或 false")

    def test_bad_severity_rejected(self) -> None:
        self.assertPolicyRejected({"metadata": {"emailSeverity": "urgent"}}, "severity")

    def test_bad_regex_rejected(self) -> None:
        self.assertPolicyRejected(
            {"secrets": {"fallbackRules": [{"id": "broken", "regex": "([unclosed"}]}}, "正規表示式"
        )

    def test_uniform_number_validator_is_rejected_with_reason(self) -> None:
        self.assertPolicyRejected(
            {
                "pii": {
                    "enabled": True,
                    "rules": [
                        {"id": "ubn", "pattern": "[0-9]{8}", "severity": "medium", "validate": "tw_uniform_number"}
                    ],
                }
            },
            "不支援",
        )


class TestChecksums(CoreTestCase):
    def test_national_id_letter_codes_match_public_table(self) -> None:
        """首字母驗證代碼必須逐字等於公開對照表（含 I/O/W/X/Y/Z 例外）。"""
        core = load_core()
        public_table = {
            "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17,
            "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22, "O": 35, "P": 23,
            "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28, "V": 29, "W": 32, "X": 30,
            "Y": 31, "Z": 33,
        }
        self.assertEqual(core.TW_NATIONAL_ID_LETTER_CODES, public_table)
        # 公開表裡的例外映射（I=34、O=35、W=32、X=30、Y=31、Z=33），其中 W 與 Y 恰好
        # 落在順序值上，不能只靠「不等於順序值」判定，所以這裡逐字比對整張表。
        exceptional = {"I": 34, "O": 35, "W": 32, "X": 30, "Y": 31, "Z": 33}
        for letter, code in exceptional.items():
            self.assertEqual(public_table[letter], code, letter)
        # 完整列出「順序值會出錯」的字母，避免測試只看幾個特例而漏掉其他錯位。
        sequential_mismatch = sorted(
            letter for letter, code in public_table.items() if code != 10 + ord(letter) - ord("A")
        )
        self.assertEqual(
            sequential_mismatch,
            ["I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "X", "Y", "Z"],
        )

    def test_national_id_public_worked_examples(self) -> None:
        """公開算例：A123456789 通過、A123456788 失敗；A122478969 亦為算例。"""
        core = load_core()
        # A123456789: 1 + 0 + (8+14+18+20+20+18+14+8) + 9 = 130 → 可整除 10
        self.assertTrue(core.validate_tw_national_id("A123456789"))
        self.assertFalse(core.validate_tw_national_id("A123456788"))
        # 另一個公開算例: 1 + (8+14+12+20+28+24+18+6) + 9 = 140 → 可整除 10
        self.assertTrue(core.validate_tw_national_id("A122478969"))
        # 檢查碼算錯時必須拒絕
        self.assertFalse(core.validate_tw_national_id("A122478968"))

    def test_national_id_vectors_for_all_letters(self) -> None:
        """前 8 碼固定 10000000，依公開對照表推出的 26 組向量都必須成立。

        預期值是從公開表格（TW_NATIONAL_ID_LETTER_CODES 的來源）推導後寫死的字面值，
        不是拿被測函式自己算出來的結果當預期。
        """
        core = load_core()
        expected_valid = [
            "A100000001", "B100000002", "C100000003", "D100000004", "E100000005",
            "F100000006", "G100000007", "H100000008", "I100000003", "J100000009",
            "K100000000", "L100000000", "M100000001", "N100000002", "O100000004",
            "P100000003", "Q100000004", "R100000005", "S100000006", "T100000007",
            "U100000008", "V100000009", "W100000001", "X100000009", "Y100000000",
            "Z100000002",
        ]
        self.assertEqual(len(expected_valid), 26)
        self.assertEqual(len(set(expected_valid)), 26, "向量必須互不相同才能分辨例外映射")
        for value in expected_valid:
            self.assertEqual(len(value), 10, value)
            self.assertTrue(core.validate_tw_national_id(value), value)
            wrong = value[:-1] + str((int(value[-1]) + 1) % 10)
            self.assertFalse(core.validate_tw_national_id(wrong), wrong)

    def test_national_id_rejects_wrong_shape(self) -> None:
        core = load_core()
        for value in ("A10000002", "A1000000010", "A300000001", "a100000001", "A10000000X"):
            self.assertFalse(core.validate_tw_national_id(value), value)

    def test_uniform_number_validator_is_not_registered(self) -> None:
        core = load_core()
        self.assertNotIn("tw_uniform_number", core.VALIDATORS)
        self.assertIn("tw_uniform_number", core.UNSUPPORTED_VALIDATORS)

    def test_luhn(self) -> None:
        core = load_core()
        self.assertTrue(core.validate_luhn("4111111111111111"))
        self.assertFalse(core.validate_luhn("4111111111111112"))


# ---------------------------------------------------------------------------
# staged index 的讀取失敗不能只是警告
# ---------------------------------------------------------------------------


class TestStagedIndexFailure(CoreTestCase):
    def test_cat_file_failure_is_incomplete(self) -> None:
        repo = self.make_repo("cat-file-fail")
        repo.write("a.txt", "a\n", stage=True)
        core = load_core()
        git = core.Git(str(repo.root))
        real_run = git.run

        def fake_run(args, check=True, stdin=None):  # type: ignore[no-untyped-def]
            if args and args[0] == "cat-file":
                return subprocess.CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"")
            return real_run(args, check=check, stdin=stdin)

        git.run = fake_run  # type: ignore[assignment]
        policy = core.default_policy()
        result = core.ScanResult(policy)
        core.scan_staged(git, policy, result, core.Engines(policy, str(self.tool_dir / "absent")))
        self.assertTrue(result.errors, "讀不到 index 內容必須是 error，不能只是 warning")
        report, exit_code = core.build_report(mode="staged", policy=policy, result=result, target=str(repo.root))
        self.assertEqual(exit_code, core.EXIT_INCOMPLETE)
        self.assertEqual(report["status"], "incomplete")


# ---------------------------------------------------------------------------
# --github：設定檢查（不是抓 blob）
# ---------------------------------------------------------------------------


class TestGithubSettingsMode(CoreTestCase):
    REPO = "synthetic-owner/synthetic-repo"

    def full_table(self) -> Dict[str, Any]:
        return {
            f"repos/{self.REPO}": {
                "rc": 0,
                "json": {
                    "visibility": "private",
                    "private": True,
                    "fork": False,
                    "security_and_analysis": {
                        "secret_scanning": {"status": "enabled"},
                        "secret_scanning_push_protection": {"status": "enabled"},
                    },
                    "advanced_security": {"status": "enabled"},
                    "dependabot_security_updates": {"status": "disabled"},
                },
            },
            f"repos/{self.REPO}/forks?per_page=1": {"rc": 0, "json": []},
            f"repos/{self.REPO}/dependabot/alerts?per_page=1": {"rc": 0, "json": []},
            f"repos/{self.REPO}/branches/main/protection": {
                "rc": 0,
                "json": {
                    "required_status_checks": {"contexts": ["ci"]},
                    "required_pull_request_reviews": {},
                },
            },
        }

    def test_all_checks_ok_passes_without_gitleaks(self) -> None:
        gh = self.install_fake_gh(self.full_table())
        repo = self.make_repo("gh-ok")
        result = self.audit(
            [
                "--github",
                "--github-repo",
                self.REPO,
                "--github-branch",
                "main",
                "--gh",
                gh,
                "--repo",
                str(repo.root),
                "--format",
                "json",
            ],
            repo=repo,
        )
        report = result.json
        self.assertEqual(report["status"], "passed", report["errors"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["coverage"]["scope"]["scope"], "settings")
        self.assertEqual(report["coverage"]["scope"]["kind"], "github-settings")
        states = {check["id"]: check["state"] for check in report["coverage"]["scope"]["checks"]}
        self.assertEqual(
            states,
            {
                "repository": "ok",
                "security-and-analysis": "ok",
                "forks": "ok",
                "dependabot-alerts": "ok",
                "branch-protection": "ok",
            },
        )
        limits = " ".join(report["coverage"]["limitations"])
        self.assertIn("不抓取 blob", limits)

    def test_disabled_security_feature_is_reported_from_explicit_value(self) -> None:
        table = self.full_table()
        table[f"repos/{self.REPO}"]["json"]["security_and_analysis"]["secret_scanning_push_protection"] = {
            "status": "disabled"
        }
        gh = self.install_fake_gh(table)
        repo = self.make_repo("gh-disabled-feature")
        result = self.audit(
            [
                "--github",
                "--github-repo",
                self.REPO,
                "--github-branch",
                "main",
                "--gh",
                gh,
                "--repo",
                str(repo.root),
                "--format",
                "json",
            ],
            repo=repo,
        )
        self.assertIn("github-security-feature-disabled", result.rule_ids())
        self.assertEqual(result.json["status"], "findings")
        self.assertEqual(result.returncode, 0, "medium 發現不阻擋，但也不能宣稱完全沒有問題")

    def run_github(self, table: Dict[str, Any], name: str) -> CliResult:
        gh = self.install_fake_gh(table)
        repo = self.make_repo(name)
        return self.audit(
            [
                "--github",
                "--github-repo",
                self.REPO,
                "--github-branch",
                "main",
                "--gh",
                gh,
                "--repo",
                str(repo.root),
                "--format",
                "json",
            ],
            repo=repo,
        )

    def test_missing_security_and_analysis_is_incomplete(self) -> None:
        """payload 沒有 security_and_analysis 時不得聲稱已查完，必須 incomplete。"""
        table = self.full_table()
        table[f"repos/{self.REPO}"]["json"].pop("security_and_analysis")
        result = self.run_github(table, "gh-no-security")
        report = result.json
        self.assertEqual(report["status"], "incomplete", report["errors"])
        self.assertEqual(result.returncode, 2)
        states = {check["id"]: check["state"] for check in report["coverage"]["scope"]["checks"]}
        self.assertEqual(states["security-and-analysis"], "unknown")
        self.assertIn("security_and_analysis", " ".join(report["errors"]))
        self.assertIn("不推論為未啟用", " ".join(report["coverage"]["limitations"]))

    def test_empty_security_and_analysis_is_incomplete(self) -> None:
        """空物件等於沒查到，不等於「全部啟用」。"""
        table = self.full_table()
        table[f"repos/{self.REPO}"]["json"]["security_and_analysis"] = {}
        result = self.run_github(table, "gh-empty-security")
        self.assertEqual(result.json["status"], "incomplete", result.json["errors"])
        self.assertEqual(result.returncode, 2)
        states = {c["id"]: c["state"] for c in result.json["coverage"]["scope"]["checks"]}
        self.assertEqual(states["security-and-analysis"], "unknown")
        self.assertIn("secret_scanning", " ".join(result.json["errors"]))

    def test_partial_security_and_analysis_is_incomplete(self) -> None:
        """只回傳其中一個 feature → 另一個必須算未檢查。"""
        table = self.full_table()
        table[f"repos/{self.REPO}"]["json"]["security_and_analysis"] = {
            "secret_scanning": {"status": "enabled"}
        }
        result = self.run_github(table, "gh-partial-security")
        report = result.json
        self.assertEqual(report["status"], "incomplete", report["errors"])
        self.assertEqual(result.returncode, 2)
        check = next(c for c in report["coverage"]["scope"]["checks"] if c["id"] == "security-and-analysis")
        self.assertEqual(check["state"], "unknown")
        self.assertEqual(check["detail"]["featureStatus"]["secret_scanning"], "enabled")
        self.assertIsNone(check["detail"]["featureStatus"]["secret_scanning_push_protection"])
        self.assertIn("secret_scanning_push_protection", " ".join(report["errors"]))

    def test_complete_security_and_analysis_passes(self) -> None:
        """兩個必要欄位都有明確狀態時才算查完，可以 passed。"""
        result = self.run_github(self.full_table(), "gh-full-security")
        report = result.json
        check = next(c for c in report["coverage"]["scope"]["checks"] if c["id"] == "security-and-analysis")
        self.assertEqual(check["state"], "ok")
        self.assertEqual(
            check["detail"]["featureStatus"],
            {"secret_scanning": "enabled", "secret_scanning_push_protection": "enabled"},
        )
        self.assertEqual(report["status"], "passed", report["errors"])

    def test_unknown_permission_is_incomplete_not_disabled(self) -> None:
        repo = self.make_repo("gh-denied")
        table = self.full_table()
        table.pop(f"repos/{self.REPO}/branches/main/protection")
        table[f"repos/{self.REPO}/dependabot/alerts?per_page=1"] = {"rc": 1}
        gh = self.install_fake_gh(table)
        result = self.audit(
            [
                "--github",
                "--github-repo",
                self.REPO,
                "--github-branch",
                "main",
                "--gh",
                gh,
                "--repo",
                str(repo.root),
                "--format",
                "json",
            ],
            repo=repo,
        )
        report = result.json
        self.assertEqual(report["status"], "incomplete", "未檢查的項目不可視為通過")
        self.assertEqual(result.returncode, 2)
        states = {check["id"]: check["state"] for check in report["coverage"]["scope"]["checks"]}
        self.assertEqual(states["branch-protection"], "unknown")
        self.assertEqual(states["dependabot-alerts"], "unknown")
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("HTTP 404", blob, "不應把 gh 的原始 stderr 帶進報告")
        errors = " ".join(report["errors"])
        self.assertIn("無法區分", errors)

    def test_public_visibility_is_a_finding(self) -> None:
        table = self.full_table()
        table[f"repos/{self.REPO}"] = {
            "rc": 0,
            "json": {"visibility": "public", "private": False, "fork": False, "security_and_analysis": None},
        }
        gh = self.install_fake_gh(table)
        repo = self.make_repo("gh-public")
        result = self.audit(
            [
                "--github",
                "--github-repo",
                self.REPO,
                "--github-branch",
                "main",
                "--gh",
                gh,
                "--repo",
                str(repo.root),
                "--format",
                "json",
            ],
            repo=repo,
        )
        self.assertIn("github-repo-public", result.rule_ids())
        self.assertEqual(result.json["summary"]["medium"], 1)
        # security_and_analysis 為 null 時必須算未檢查（不是「全部啟用」）。
        self.assertEqual(result.json["status"], "incomplete", result.json["errors"])
        states = {c["id"]: c["state"] for c in result.json["coverage"]["scope"]["checks"]}
        self.assertEqual(states["security-and-analysis"], "unknown")
        self.assertIn("視為未檢查", " ".join(result.json["coverage"]["limitations"]))

    def test_slug_can_be_derived_from_origin(self) -> None:
        gh = self.install_fake_gh(self.full_table())
        repo = self.make_repo("gh-origin")
        repo.git("remote", "add", "origin", f"https://github.example.invalid/{self.REPO}.git")
        result = self.audit(
            ["--github", "--github-branch", "main", "--gh", gh, "--repo", str(repo.root), "--format", "json"],
            repo=repo,
        )
        self.assertEqual(result.json["coverage"]["scope"]["repo"], self.REPO)

    def test_github_mode_works_without_git_repo(self) -> None:
        """--repo 指向非 Git 目錄時，仍可用 --github-repo 做設定檢查。"""
        gh = self.install_fake_gh(self.full_table())
        plain = Path(make_plain_dir())
        self.addCleanup(shutil.rmtree, plain, True)
        result = self.audit(
            [
                "--github",
                "--github-repo",
                self.REPO,
                "--github-branch",
                "main",
                "--gh",
                gh,
                "--repo",
                str(plain),
                "--format",
                "json",
            ],
        )
        self.assertEqual(result.json["status"], "passed", result.json["errors"])

    def test_missing_gh_is_an_error(self) -> None:
        repo = self.make_repo("gh-absent")
        absent = self.tool_dir / "definitely-absent-gh"
        result = self.audit(
            [
                "--github",
                "--github-repo",
                self.REPO,
                "--gh",
                str(absent),
                "--repo",
                str(repo.root),
                "--format",
                "json",
            ],
            repo=repo,
        )
        self.assertExitCode(result, 2)
        self.assertEqual(result.json["status"], "error")


def make_plain_dir() -> str:
    """一個刻意「不是 Git repo」的暫存目錄。"""
    import tempfile

    from helpers import SCRATCH_ROOT

    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="rpa-not-a-repo-", dir=str(SCRATCH_ROOT))


class TestToolIsolation(CoreTestCase):
    """環境變數不得默默改掉退出碼、遮罩或掃描範圍。"""

    NEVER_MATCH_TOML = (
        'title = "synthetic config that cannot match anything"\n'
        "[[rules]]\n"
        'id = "never-matches"\n'
        'description = "synthetic"\n'
        "regex = '''ZZZZZZZZZZZZNEVERMATCHZZZZZZZZ'''\n"
    )

    def test_env_cannot_override_gitleaks_scope_or_report_path(self) -> None:
        if not HAVE_REAL_GITLEAKS:
            self.skipTest(f"找不到可執行的真實 Gitleaks：{REAL_GITLEAKS}")
        repo = self.make_repo("env-isolation")
        repo.write("seed.txt", "seed\n", stage=True)
        base = repo.commit("seed")
        repo.write("leak.txt", f"token={SYNTHETIC_GITUB_PAT}\n", stage=True)
        head = repo.commit("leak")
        evil_report = self.tool_dir / "evil-report.json"
        self.env["GITLEAKS_CONFIG_TOML"] = self.NEVER_MATCH_TOML
        self.env["GITLEAKS_CONFIG"] = str(self.tool_dir / "does-not-exist.toml")
        self.env["REPO_PRIVACY_AUDIT_GITLEAKS_ARGS"] = f"--exit-code 1 --report-path {evil_report}"

        result = self.audit_with(
            ["--pre-push"],
            repo,
            self.quiet_policy(),
            stdin=f"refs/heads/main {head} refs/heads/main {base}\n",
            gitleaks=str(REAL_GITLEAKS),
        )
        self.assertTrue(
            any(f["ruleId"] == "github-pat" for f in result.json["findings"]),
            "環境變數不得改掉掃描範圍（規則集必須維持 Gitleaks 預設）",
        )
        self.assertEqual(result.returncode, 1, "額外參數不得改變退出碼語意")
        self.assertFalse(evil_report.exists(), "額外參數不得改掉報告輸出位置")
        self.secrets_in(result, [SYNTHETIC_GITUB_PAT])

    def test_secret_preview_is_disabled_by_default(self) -> None:
        repo = self.make_repo("no-preview")
        repo.write("leak.txt", f"token={SYNTHETIC_GITUB_PAT}\n", stage=True)
        result = self.audit_with(["--staged"], repo, self.quiet_policy())
        self.assertTrue(result.json["findings"])
        for finding in result.json["findings"]:
            self.assertIsNone(finding["masked"]["preview"], finding["ruleId"])
            self.assertTrue(finding["masked"]["redacted"])
        self.assertEqual(result.json["policy"]["maskingPreviewChars"], 0)

    def test_preview_chars_must_be_integer(self) -> None:
        repo = self.make_repo("bad-preview")
        repo.write("a.txt", "a\n", stage=True)
        result = self.audit_with(["--staged"], repo, {"masking": {"previewChars": "4"}})
        self.assertExitCode(result, 2)
        self.assertEqual(result.json["status"], "error")


if __name__ == "__main__":
    unittest.main()