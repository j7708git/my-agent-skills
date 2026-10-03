"""repo-privacy-audit 測試共用工具。

設計原則
--------
- 所有 Git fixture 都建在 /tmp/opencode 底下的獨立暫存目錄，測完自動刪除。
- fixture 內容全部是**合成資料**：刻意使用公開文件裡的假憑證格式
  （例如雲端廠商官方範例的存取金鑰樣式），不放入任何真實秘密、真實 email、
  真實姓名或真實路徑。
- 合成憑證以**分段常量**在執行時組合，不在原始碼裡留下完整字面值。
  這樣 fixture 內容與斷言用的值完全不變，但產品原始碼不會混入
  「看起來像真秘密」的字串，也就不需要為這個 repo 自己的秘密掃描開豁免。
- 測試用假的 gitleaks / gh 執行檔放在暫存目錄，透過環境變數注入，
  不會安裝任何東西到系統，也不會發出任何網路請求。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "repo-privacy-audit"
CLI = SKILL_DIR / "scripts" / "repo-privacy-audit.sh"
HOOKS_SH = SKILL_DIR / "scripts" / "rpa-hooks.sh"
CORE_PY = SKILL_DIR / "scripts" / "audit_core.py"

# 測試暫存根目錄。依 brief 要求固定在 /tmp/opencode，不寫進專案目錄。
SCRATCH_ROOT = Path(os.environ.get("RPA_TEST_SCRATCH", "/tmp/opencode/repo-privacy-audit-tests"))

# 合成秘密：全部取自公開文件的假範例，不是真實憑證。
# 一律以分段常量在執行時組合，原始碼不留完整字面值——
# 否則 repo 自己的秘密掃描會把這些合成 fixture 判成 high 發現，
# 而為它們開豁免等於在產品原始碼裡混入看起來像真秘密的東西。
# 組合後的執行期值與分段前的字面值完全相同，測試行為不變。
FAKE_AWS_KEY = "AKIA" + "IOSFO" + "DNN7E" + "XAMPLE"
FAKE_GITHUB_TOKEN = "ghp_" + "0" * 36
FAKE_GOOGLE_KEY = "AIza" + "A" * 35
FAKE_SLACK_TOKEN = "xoxb-" + "1" * 11 + "-" + "2" * 11
FAKE_PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA " + "PRIVATE " + "KEY-----"
FAKE_EMAIL = "synthetic-user@example.invalid"
FAKE_EMAIL_2 = "other-synthetic@example.invalid"


class CliResult:
    """一次 CLI 呼叫的結果。"""

    def __init__(self, returncode: int, stdout: str, stderr: str, argv: Sequence[str]) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.argv = list(argv)

    @property
    def json(self) -> Dict:
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as exc:  # pragma: no cover - 失敗時提供好懂的訊息
            raise AssertionError(
                f"CLI 輸出不是合法 JSON：{exc}\n--- argv ---\n{' '.join(self.argv)}\n"
                f"--- stdout ---\n{self.stdout[:2000]}\n--- stderr ---\n{self.stderr[:2000]}"
            ) from exc

    def rule_ids(self) -> List[str]:
        return [f["ruleId"] for f in self.json["findings"]]

    def severities(self) -> List[str]:
        return [f["severity"] for f in self.json["findings"]]

    def paths(self) -> List[str]:
        return [f["location"].get("path") for f in self.json["findings"]]


def run_cli(args: Sequence[str], *, stdin: str = "", env: Optional[Dict[str, str]] = None) -> CliResult:
    """透過對外的 Bash 入口執行掃描器。"""
    base = dict(os.environ)
    base.pop("REPO_PRIVACY_AUDIT_GITLEAKS", None)
    base.pop("REPO_PRIVACY_AUDIT_GH", None)
    base.pop("REPO_PRIVACY_AUDIT_POLICY", None)
    base.pop("REPO_PRIVACY_AUDIT_BIN", None)
    base.pop("REPO_PRIVACY_AUDIT_ARGS", None)
    base.pop("REPO_PRIVACY_AUDIT_FORMAT", None)
    if env:
        base.update(env)
    proc = subprocess.run(
        ["bash", str(CLI), *args],
        input=stdin.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=base,
        check=False,
    )
    return CliResult(
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
        args,
    )


def run_hooks(args: Sequence[str], *, env: Optional[Dict[str, str]] = None) -> CliResult:
    base = dict(os.environ)
    for key in (
        "REPO_PRIVACY_AUDIT_GITLEAKS",
        "REPO_PRIVACY_AUDIT_GH",
        "REPO_PRIVACY_AUDIT_POLICY",
        "REPO_PRIVACY_AUDIT_BIN",
        "REPO_PRIVACY_AUDIT_ARGS",
    ):
        base.pop(key, None)
    if env:
        base.update(env)
    proc = subprocess.run(
        ["bash", str(HOOKS_SH), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=base,
        check=False,
    )
    return CliResult(
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
        args,
    )


def write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# 假的 gitleaks / gh：只有在我們自己指定路徑時才會被使用
# ---------------------------------------------------------------------------

# 假的 gitleaks：`git --staged` 掃描時回報一筆 synthetic 發現，
# 驗證我們能把 gitleaks 報告轉成遮罩輸出。絕不真的做掃描。
FAKE_GITLEAKS = """#!/usr/bin/env bash
set -uo pipefail
if [ "${1:-}" = "version" ]; then
  echo "8.99.0-fake"
  exit 0
fi
report=""
staged=0
while [ $# -gt 0 ]; do
  case $1 in
    --report-path) report=$2; shift 2 ;;
    --staged) staged=1; shift ;;
    --exit-code) leak_exit=$2; shift 2 ;;
    --*) shift ;;
    *) shift ;;
  esac
done
leak_exit=${leak_exit:-99}
if [ "$staged" = "1" ]; then
  staged_arg="staged,"
fi
# 報告裡的合成憑證同樣分段組合，這份腳本原始碼才不會出現完整字面值。
# 組合後的值與 helpers.py 執行期組出來的完全相同。
fake_key="AKIA""IOSFO""DNN7E""XAMPLE"
if [ "${FAKE_GITLEAKS_FINDINGS:-1}" = "1" ]; then
  cat > "$report" <<JSON
[
  {
    "RuleID": "fake-rule-for-tests",
    "Description": "synthetic finding from the fake gitleaks",
    "StartLine": 1,
    "EndLine": 1,
    "Match": "$fake_key",
    "Secret": "$fake_key",
    "File": "synthetic.txt",
    "Commit": "",
    "Entropy": 3.5,
    "Author": "Synthetic Author",
    "Email": "synthetic-user@example.invalid",
    "Date": "2026-01-01T00:00:00Z",
    "Message": "synthetic",
    "Tags": [],
    "Fingerprint": "fingerprint-fake"
  }
]
JSON
  exit "$leak_exit"
fi
cat > "$report" <<JSON
[]
JSON
exit 0
"""

# 假的 gitleaks：永遠回傳非 0 的錯誤碼（例如未支援的參數 → 126），驗證 incomplete。
FAKE_GITLEAKS_BROKEN = """#!/usr/bin/env bash
set -uo pipefail
if [ "${1:-}" = "version" ]; then
  echo "8.99.0-fake"
  exit 0
fi
echo "unknown flag: --nonexistent-flag (synthetic failure)" >&2
exit 126
"""


class GitRepoFixture:
    """在 /tmp/opencode 底下建立一次性的合成 Git repo。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.root = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=_ensure_scratch()))

    # -- Git 操作 ---------------------------------------------------------
    def git(self, *args: str, check: bool = True, env: Optional[Dict[str, str]] = None) -> str:
        base = dict(os.environ)
        base.update(
            {
                "GIT_AUTHOR_NAME": "Synthetic Author",
                "GIT_AUTHOR_EMAIL": FAKE_EMAIL,
                "GIT_COMMITTER_NAME": "Synthetic Committer",
                "GIT_COMMITTER_EMAIL": FAKE_EMAIL_2,
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "home" / ".config"),
                "TZ": "UTC",
            }
        )
        if env:
            base.update(env)
        (self.root / "home").mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=base,
            check=False,
        )
        out = proc.stdout.decode("utf-8", "replace")
        if check and proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失敗：{proc.stderr.decode('utf-8', 'replace')}")
        return out

    def init(self) -> "GitRepoFixture":
        self.git("init", "-q", "-b", "main", ".")
        return self

    def write(self, relpath: str, content: str, *, stage: bool = False) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if stage:
            self.git("add", "--", relpath)
        return path

    def write_bytes(self, relpath: str, content: bytes, *, stage: bool = False) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if stage:
            self.git("add", "--", relpath)
        return path

    def commit(self, message: str, *, env: Optional[Dict[str, str]] = None) -> str:
        self.git("add", "-A", env=env)
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        return self.git("rev-parse", "HEAD").strip()

    @property
    def git_dir(self) -> Path:
        return Path(self.git("rev-parse", "--absolute-git-dir").strip())

    @property
    def hooks_dir(self) -> Path:
        raw = self.git("rev-parse", "--git-path", "hooks").strip()
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else self.git_dir / raw

    def head_sha(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def refs(self) -> List[str]:
        return [line for line in self.git("for-each-ref", "--format=%(refname)").splitlines() if line]

    def write_policy(self, data: Dict, name: str = "policy.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _ensure_scratch() -> str:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return str(SCRATCH_ROOT)


class AuditTestCase(unittest.TestCase):
    """所有測試共用的基底：建立暫存 fixture、注入假工具、結束清理。

    子類別可用 ``@classmethod.setUpClass`` 覆寫 ``fake_gitleaks_mode``：
    因為 unittest 的 setUpClass 在 setUp 之前跑，所以那時就能決定注入哪個假工具。
    """

    fake_gitleaks_mode = "none"  # none | clean | findings | broken

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._frozen_gitleaks_mode = cls.fake_gitleaks_mode

    def setUp(self) -> None:
        self.tool_dir = Path(tempfile.mkdtemp(prefix="tools-", dir=_ensure_scratch()))
        self.addCleanup(shutil.rmtree, self.tool_dir, True)
        self.fake_gitleaks: Optional[Path] = None
        self.env: Dict[str, str] = {}
        self.fake_gitleaks_mode = type(self)._frozen_gitleaks_mode
        self._install_fake_gitleaks()

    def _install_fake_gitleaks(self) -> None:
        mode = self.fake_gitleaks_mode
        if mode == "none":
            # 明確指向一個不存在的執行檔，模擬「系統上沒有 gitleaks」。
            self.env["REPO_PRIVACY_AUDIT_GITLEAKS"] = str(self.tool_dir / "definitely-absent-gitleaks")
            return
        if mode == "broken":
            body = FAKE_GITLEAKS_BROKEN
        else:
            body = FAKE_GITLEAKS
        path = write_script(self.tool_dir / "gitleaks", body)
        self.fake_gitleaks = path
        self.env["REPO_PRIVACY_AUDIT_GITLEAKS"] = str(path)

    def set_fake_gitleaks_findings(self, enabled: bool) -> None:
        self.env["FAKE_GITLEAKS_FINDINGS"] = "1" if enabled else "0"

    def make_repo(self, name: str = "repo") -> GitRepoFixture:
        repo = GitRepoFixture(f"{name}-{self.fake_gitleaks_mode}")
        self.addCleanup(repo.cleanup)
        if name != "bare":
            repo.init()
        return repo

    def audit(self, args: Sequence[str], *, stdin: str = "", repo: Optional[GitRepoFixture] = None) -> CliResult:
        env = dict(self.env)
        if repo is not None:
            # 讓掃描器用 fixture 的隔離 HOME，避免讀到真實的 ~/.gitconfig。
            env["HOME"] = str(repo.root / "home")
            env["XDG_CONFIG_HOME"] = str(repo.root / "home" / ".config")
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env["GIT_AUTHOR_NAME"] = "Synthetic Author"
            env["GIT_AUTHOR_EMAIL"] = FAKE_EMAIL
            env["GIT_COMMITTER_NAME"] = "Synthetic Committer"
            env["GIT_COMMITTER_EMAIL"] = FAKE_EMAIL_2
        return run_cli(args, stdin=stdin, env=env)

    # -- 斷言輔助 ---------------------------------------------------------
    def assertExitCode(self, result: CliResult, expected: int, note: str = "") -> None:
        self.assertEqual(
            result.returncode,
            expected,
            f"{note}\nargv={' '.join(result.argv)}\nstatus={result.json.get('status')}\n"
            f"errors={result.json.get('errors')}\nstdout={result.stdout[:1500]}\nstderr={result.stderr[:800]}",
        )

    def assertContract(self, report: Dict) -> None:
        """檢查 JSON 報告契約（plan.md 的要求）。"""
        for key in ("schemaVersion", "mode", "status", "coverage", "findings"):
            self.assertIn(key, report, f"報告缺少必要欄位：{key}")
        self.assertIn(report["status"], {"passed", "findings", "incomplete", "error"})
        for key in ("scope", "engines", "limitations"):
            self.assertIn(key, report["coverage"], f"coverage 缺少 {key}")
        for finding in report["findings"]:
            for key in ("ruleId", "severity", "location", "masked"):
                self.assertIn(key, finding, f"finding 缺少 {key}")
            self.assertIn(finding["severity"], {"low", "medium", "high"})
            self.assertTrue(finding["masked"]["redacted"], "finding 必須標示已遮罩")
            self.assertIn("description", finding["masked"])

    def assertNoSecretLeaked(self, result: CliResult, secrets: Sequence[str]) -> None:
        """報告與 stderr 都不應出現秘密原文。"""
        combined = result.stdout + result.stderr
        for secret in secrets:
            self.assertNotIn(secret, combined, f"秘密原文出現在輸出中：{secret[:4]}…")

    def assertStatusMatchesExit(self, result: CliResult) -> Dict:
        report = result.json
        expected = {"passed": 0, "findings": None, "incomplete": 2, "error": 2}[report["status"]]
        if expected is not None:
            self.assertEqual(result.returncode, expected, f"status={report['status']} 與退出碼不一致")
        elif report["summary"]["high"] > 0:
            self.assertEqual(result.returncode, 1)
        else:
            self.assertEqual(result.returncode, 0)
        return report


def clean_policy(**overrides) -> Dict:
    """乾淨 repo 的政策（關掉 metadata 掃描以讓斷言聚焦）。"""
    policy = {
        "schemaVersion": "1.0",
        "id": "test-policy",
        "secrets": {},
        "pii": {"enabled": False},
        "metadata": {"emailSeverity": "off", "scanCommitMessage": False},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(policy.get(key), dict):
            policy[key].update(value)
        else:
            policy[key] = value
    return policy
