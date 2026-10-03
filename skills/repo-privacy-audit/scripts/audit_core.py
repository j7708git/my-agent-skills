#!/usr/bin/env python3
"""repo-privacy-audit 掃描核心（唯讀）。

設計約束
--------
- 只讀。不寫入受檢 repo、不改 Git 設定、不 commit/push、不自動修復。
- 只用 Python 3 標準函式庫；不自動下載或安裝任何相依套件。
- 高風險密鑰以 Gitleaks 為正式檢查。Gitleaks 與內建引擎掃的是**同一組
  commit**（gitleaks git --no-walk <sha...> --diff-merges=first-parent），
  因此報告宣稱的範圍與實際掃描一致。缺 Gitleaks 一律 incomplete / 退出 2。
- 所有外部工具（git / gitleaks / gh）的 stderr 都不回傳，只回傳 rc 與類型，
  避免路徑、作者名或秘密原文被帶到報告或錯誤訊息裡。
- 報告輸出前會再做一次整體 scrub：已比對到的秘密字串（密鑰、電話型 email、
  個人本機路徑、作者姓名）一律從所有欄位移除，避免出現在檔名或描述中。

退出碼
------
- 0 = 完整掃描且沒有 high 等級發現
- 1 = 完整掃描且有 high 等級發現
- 2 = 未完整掃描（缺 Gitleaks、工具失敗、套用上限、未檢查的項目）或操作錯誤

政策（純 JSON 資料，不 source、不 eval）頂層鍵
---------------------------------------------
secrets.fallbackRules / secrets（內建有限 regex）
pii.enabled / pii.rules（台灣個資格式規則；內建不含統一編號）
paths.skipGlobs / paths.maxFileBytes
paths.sensitiveGlobs / paths.sensitiveSeverity / paths.templateGlobs
paths.personalPathPatterns / paths.personalPathSeverity / paths.personalPathAllow
masking.previewChars（預設 0＝不顯示任何秘密字元）
limits.maxCommits / limits.maxFiles / limits.maxFileBytes / limits.maxTotalBytes
metadata.enabled            是否套用 email／姓名等身分規則（預設 true）
metadata.emailSeverity      一般 email 嚴重度（預設 low）
metadata.phoneEmailSeverity local part 含 7–15 位電話型數字（預設 high）
metadata.nameSeverity       author／committer 姓名（預設 off）
metadata.scanCommitMessage  是否用秘密/PII 規則掃 commit 訊息（預設 true）
metadata.allowedEmails      允許的 email（完全相同或 glob），可明確免除電話型判定
metadata.ignoreNoReply      略過已知機器帳號（預設 true）
metadata.knownNoReplyDomains 視為機器帳號的 domain 白名單（預設僅 GitHub／GitLab 匿名）
gitleaks.ruleSeverity / gitleaks.config
githubSettings.visibilitySeverity（--github 模式，預設 medium）
githubSettings.securitySeverity（--github 模式，預設 medium）
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

SCHEMA_VERSION = "1.0"
TOOL_NAME = "repo-privacy-audit"
TOOL_VERSION = "0.2.0"
POLICY_SCHEMA_VERSION = "1.0"

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_INCOMPLETE = 2

STATUS_PASSED = "passed"
STATUS_FINDINGS = "findings"
STATUS_INCOMPLETE = "incomplete"
STATUS_ERROR = "error"

SEVERITIES = ("low", "medium", "high")
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}

ZERO_OID = "0" * 40

# Gitleaks 用 --exit-code 指定「找到洩漏」的退出碼，避免和它「錯誤也回 1」的
# 預設行為混淆（見 https://github.com/gitleaks/gitleaks README 的 Exit Codes）。
GITLEAKS_LEAK_EXIT = 99
# 每次呼叫 Gitleaks 帶入多少個 commit。逐 commit 呼叫在大量推送時太慢，
# 因此分批；--no-walk 保證只掃這批明確的 commit（已實測 gitleaks 8.30.1）。
GITLEAKS_BATCH_SIZE = 50
# Gitleaks stderr 出現這些字樣代表掃描其實失敗，但它仍可能回 0（假的乾淨）。
GITLEAKS_ERROR_MARKERS = re.compile(r"\[git\]|fatal:|error=|Error:|panic")

REPORT_SCRUB_LIMIT = 2000


class AuditError(Exception):
    """操作錯誤：參數、政策、Git 失敗等。對應 status=error / 退出碼 2。"""


def tool_failure(tool: str, rc: int, note: str) -> str:
    """外部工具失敗的固定句式：只帶 rc 與類型，絕不回傳 stderr 原文。"""
    return f"{tool} 以 rc={rc} 結束（{note}）；不回傳原始 stderr 以免帶出敏感內容"


# ---------------------------------------------------------------------------
# 秘密遮罩
# ---------------------------------------------------------------------------

# 記住本回合實際比對到的敏感字串，所有對外文字（含例外訊息與整份報告）都先 scrub。
_SECRET_SINK: List[str] = []


def _remember_secret(value: Optional[str]) -> None:
    if not value or len(value) < 6:
        return
    if value.strip().upper().startswith("REDACTED"):
        return  # Gitleaks --redact 產生的佔位字串，不是秘密
    if value not in _SECRET_SINK:
        _SECRET_SINK.append(value)


def scrub(text: str, limit: Optional[int] = 600) -> str:
    """把已知秘密從任意文字（含例外訊息、報告欄位）中移除。"""
    if text is None:
        return ""
    for secret in _SECRET_SINK:
        if secret and secret in text:
            text = text.replace(secret, "[REDACTED]")
    text = text.replace("\r", " ")
    if limit is not None and len(text) > limit:
        text = text[:limit] + f"…（已截斷，共 {len(text)} 字元）"
    return text


def scrub_value(value: Any, limit: Optional[int] = REPORT_SCRUB_LIMIT) -> Any:
    """遞迴 scrub 整個 JSON 結構，避免秘密出現在檔名／作者名等欄位。"""
    if isinstance(value, str):
        return scrub(value, limit)
    if isinstance(value, dict):
        return {key: scrub_value(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_value(item, limit) for item in value]
    return value


def mask_description(value: Optional[str], preview_chars: int = 0) -> Dict[str, Any]:
    """產生遮罩描述。預設（preview_chars=0）不顯示任何秘密字元。"""
    if value is None:
        return {
            "redacted": True,
            "length": 0,
            "sha256Prefix": None,
            "preview": None,
            "description": "無可遮罩內容",
        }
    raw = value.encode("utf-8", "surrogateescape")
    digest = hashlib.sha256(raw).hexdigest()
    preview = value[:preview_chars] if preview_chars > 0 else None
    parts = [f"已遮罩（長度 {len(value)}"]
    if preview:
        parts.append(f"顯示開頭 {len(preview)} 字元「{preview}…」")
    parts.append(f"sha256 前綴 {digest[:12]}）")
    return {
        "redacted": True,
        "length": len(value),
        "sha256Prefix": digest[:12],
        "preview": preview,
        "description": " ".join(parts),
    }


# ---------------------------------------------------------------------------
# Git 介面
# ---------------------------------------------------------------------------


class Git:
    """薄封裝，只讀取用。cwd 固定在 repo 根目錄。"""

    def __init__(self, repo: str) -> None:
        self.repo = os.path.abspath(repo)
        self.top = self.repo
        if not os.path.isdir(self.repo):
            raise AuditError(f"repo 路徑不存在：{self.repo}")
        probe = self._exec(self.repo, ["rev-parse", "--show-toplevel"], check=False)
        if probe.returncode != 0 or not probe.stdout.strip():
            raise AuditError(f"不是 Git repo 或無法讀取：{self.repo}")
        top = probe.stdout.decode("utf-8", "surrogateescape").strip().splitlines()[0]
        self.top = top or self.repo
        self._diff_merges: Optional[bool] = None

    @staticmethod
    def _exec(
        cwd: str, args: Sequence[str], check: bool, stdin: Optional[bytes] = None
    ) -> subprocess.CompletedProcess:
        cmd = ["git", "-C", cwd, "-c", "core.quotePath=false"] + list(args)
        try:
            proc = subprocess.run(
                cmd,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as exc:  # pragma: no cover - 環境無 git
            raise AuditError("找不到 git 執行檔") from exc
        if check and proc.returncode != 0:
            raise AuditError(tool_failure("git", proc.returncode, f"{' '.join(args[:2])} 失敗"))
        return proc

    def run(
        self, args: Sequence[str], check: bool = True, stdin: Optional[bytes] = None
    ) -> subprocess.CompletedProcess:
        return self._exec(self.top, args, check, stdin)

    def text(self, args: Sequence[str], check: bool = True) -> str:
        proc = self.run(args, check=check)
        return proc.stdout.decode("utf-8", "surrogateescape")

    def rev_parse(self, rev: str) -> Optional[str]:
        proc = self.run(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], check=False)
        out = proc.stdout.decode("ascii", "replace").strip()
        return out or None

    def rev_list(self, rev_args: Sequence[str]) -> List[str]:
        proc = self.run(["rev-list", *rev_args], check=False)
        if proc.returncode != 0:
            raise AuditError(tool_failure("git rev-list", proc.returncode, "無法列出 commit"))
        return proc.stdout.decode("ascii", "replace").split()

    def supports_diff_merges(self) -> bool:
        """git >= 2.31 才有 --diff-merges；沒有時退回 -m。"""
        if self._diff_merges is None:
            ok = False
            for revs in (["HEAD"], ["--all"]):
                proc = self.run(
                    ["log", "-1", "--diff-merges=first-parent", "--format=%H", *revs], check=False
                )
                if proc.returncode == 0:
                    ok = True
                    break
            self._diff_merges = ok
        return self._diff_merges


def try_git(repo: str) -> Optional[Git]:
    """Git 開啟失敗時回 None（--github 只需要 origin，不需要 Git 歷史）。"""
    try:
        return Git(repo)
    except AuditError:
        return None


def split_nul(blob: bytes) -> List[str]:
    return [chunk.decode("utf-8", "surrogateescape") for chunk in blob.split(b"\0") if chunk]


def is_zero_oid(sha: str) -> bool:
    return len(sha) >= 4 and set(sha) == {"0"}


# ---------------------------------------------------------------------------
# 政策載入（純 JSON 資料，不 source、不 eval）
# ---------------------------------------------------------------------------

DEFAULT_PATHS_SKIP = [
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.pdf",
    "*.zip",
    "*.gz",
    "*.tgz",
    "*.bz2",
    "*.xz",
    "*.7z",
    "*.rar",
    "*.tar",
    "*.jar",
    "*.class",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.bin",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.otf",
    "*.eot",
    "*.mp3",
    "*.mp4",
    "*.mov",
    "*.sqlite",
    "*.db",
]

# 檔名層級的敏感樣式（high）。只是標記，內容照樣掃描。
DEFAULT_SENSITIVE_GLOBS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.ppk",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".netrc",
    ".pgpass",
    ".npmrc",
    "credentials",
    "secrets.*",
    "*.secret",
]

# 範本檔：不被上面的規則按檔名判為敏感（內容仍然照掃）。
DEFAULT_TEMPLATE_GLOBS = [
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.dist",
    "*.example",
    "*.sample",
    "*.template",
    "*.dist",
    "*.tmpl",
    "*.example.*",
    "example.env",
    "sample.env",
]

DEFAULT_PERSONAL_PATH_PATTERNS = [
    r"/Users/[^/\s\"']+/",
    r"/home/[^/\s\"']+/",
    r"[A-Za-z]:\\Users\\[^\\\s\"']+\\",
]

DEFAULT_PERSONAL_PATH_ALLOW = [
    "/home/runner/",
    "/home/ci/",
    "/Users/runner/",
    "/Users/ci/",
]

DEFAULT_LIMITS = {
    "maxCommits": 2000,
    "maxFiles": 4000,
    "maxFileBytes": 1048576,
    "maxTotalBytes": 67108864,
}

DEFAULT_MASKING = {"previewChars": 0}

# 只信任這些已知的匿名／機器提交 domain（GitHub 公開的 commit 匿名 identity）。
# 不能用「字串裡有 noreply」判斷：那等於給了繞過（noreply-example.invalid、
# noreply0912345678@example.invalid 這類都會被跳過）。
DEFAULT_KNOWN_NOREPLY_DOMAINS = [
    "users.noreply.github.com",
    "noreply.github.com",
    "users.noreply.gitlab.com",
]

DEFAULT_METADATA = {
    "enabled": True,
    "emailSeverity": "low",
    "phoneEmailSeverity": "high",
    "nameSeverity": "off",
    "scanCommitMessage": True,
    "allowedEmails": [],
    "ignoreNoReply": True,
    "knownNoReplyDomains": list(DEFAULT_KNOWN_NOREPLY_DOMAINS),
}

DEFAULT_PATHS = {
    "sensitiveGlobs": list(DEFAULT_SENSITIVE_GLOBS),
    "sensitiveSeverity": "high",
    "templateGlobs": list(DEFAULT_TEMPLATE_GLOBS),
    "personalPathPatterns": list(DEFAULT_PERSONAL_PATH_PATTERNS),
    "personalPathSeverity": "medium",
    "personalPathAllow": list(DEFAULT_PERSONAL_PATH_ALLOW),
}

DEFAULT_GITHUB_SETTINGS = {"visibilitySeverity": "medium", "securitySeverity": "medium"}

# 內建有限 regex（沒有 Gitleaks，或使用者未自訂 secrets.fallbackRules 時使用）。
# 一律是公開文件的假範例格式或明顯的樣式前綴，不代表任何真實憑證。
BUILTIN_FALLBACK_RULES: List[Dict[str, Any]] = [
    {
        "id": "private-key-block",
        "severity": "high",
        "description": "私鑰區塊標頭",
        "regex": r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----",
    },
    {
        "id": "aws-access-key-id",
        "severity": "high",
        "description": "AWS access key ID 樣式（AWS 官方公開範例格式）",
        "regex": r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b",
    },
    {
        "id": "github-token",
        "severity": "high",
        "description": "GitHub personal access / OAuth / app token 樣式",
        "regex": r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b",
    },
    {
        "id": "github-fine-grained-pat",
        "severity": "high",
        "description": "GitHub fine-grained PAT 樣式",
        "regex": r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b",
    },
    {
        "id": "google-api-key",
        "severity": "high",
        "description": "Google API key 樣式",
        "regex": r"\bAIza[0-9A-Za-z_\-]{35}\b",
    },
    {
        "id": "slack-token",
        "severity": "high",
        "description": "Slack token 樣式",
        "regex": r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",
    },
    {
        "id": "npm-token",
        "severity": "high",
        "description": "npm access token 樣式",
        "regex": r"\bnpm_[A-Za-z0-9]{36}\b",
    },
    {
        "id": "json-web-token",
        "severity": "medium",
        "description": "JWT 三段式樣式（測試 token 也會命中，需人工確認）",
        "regex": r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    },
    {
        "id": "credential-assignment",
        "severity": "medium",
        "description": "把關鍵字直接賦值給字串的樣式（誤報率較高）",
        "regex": r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*[\"']?[^\s\"'<>]{8,}",
    },
]

# 台灣個資規則（選用，pii.enabled 預設 false）。validate 用 checksum 降低誤判。
# 注意：內建「不」提供統一編號規則——第 7 碼為 7 的特例未實作，硬做會產生
# 錯誤的通過/不通過判斷；需要時請自行寫政策規則（validate 只支援下列清單）。
BUILTIN_TW_PII_RULES: List[Dict[str, Any]] = [
    {
        "id": "tw-national-id",
        "severity": "medium",
        "description": "台灣身分證字號（含 checksum 驗證）",
        "pattern": r"(?<![0-9A-Z])[A-Z][12][0-9]{8}(?![0-9A-Z])",
        "validate": "tw_national_id",
    },
    {
        "id": "tw-mobile-number",
        "severity": "low",
        "description": "台灣手機號碼（無 checksum，誤報率較高）",
        "pattern": r"(?<![0-9])09[0-9]{8}(?![0-9])",
        "validate": None,
    },
    {
        "id": "credit-card-number",
        "severity": "high",
        "description": "信用卡號（Visa/Mastercard/Amex/JCB/Diners，含 Luhn 驗證）",
        "pattern": r"(?<![0-9])(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})(?![0-9])",
        "validate": "luhn",
    },
]

UNSUPPORTED_VALIDATORS = {"tw_uniform_number"}


def _validate_severity(value: Any, where: str, allow_off: bool = False) -> str:
    if value == "off" and allow_off:
        return "off"
    if value not in SEVERITIES:
        suffix = '、off' if allow_off else ""
        raise AuditError(f"政策錯誤：{where} 的 severity 必須是 {SEVERITIES}{suffix} 之一，得到 {value!r}")
    return value


def _policy_bool(value: Any, where: str, default: bool) -> bool:
    """JSON 的 true/false 必須是真正的布林；字串 "false" 不當成 False。"""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AuditError(f"政策錯誤：{where} 必須是 JSON 的 true 或 false，得到 {value!r}")
    return value


def _string_list(value: Any, where: str, default: Sequence[str]) -> List[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AuditError(f"政策錯誤：{where} 必須是字串陣列")
    return list(value)


def compile_regex(pattern: str, where: str) -> "re.Pattern[str]":
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise AuditError(f"政策錯誤：{where} 的正規表示式無效：{exc}") from exc


class Policy:
    """載入後的掃描政策。所有欄位都是 JSON 資料，永遠不被當成程式碼執行。"""

    def __init__(
        self,
        *,
        source: str,
        path: Optional[str],
        digest: Optional[str],
        policy_id: str,
        fallback_rules: List[Dict[str, Any]],
        pii_enabled: bool,
        pii_rules: List[Dict[str, Any]],
        paths_skip: List[str],
        paths: Dict[str, Any],
        max_file_bytes: int,
        limits: Dict[str, int],
        masking: Dict[str, int],
        metadata: Dict[str, Any],
        gitleaks_rule_severity: Dict[str, str],
        gitleaks_config: Optional[str],
        github_settings: Dict[str, str],
        warnings: List[str],
    ) -> None:
        self.source = source
        self.path = path
        self.digest = digest
        self.id = policy_id
        self.fallback_rules = fallback_rules
        self.pii_enabled = pii_enabled
        self.pii_rules = pii_rules
        self.paths_skip = paths_skip
        self.paths = paths
        self.max_file_bytes = max_file_bytes
        self.limits = limits
        self.masking = masking
        self.metadata = metadata
        self.gitleaks_rule_severity = gitleaks_rule_severity
        self.gitleaks_config = gitleaks_config
        self.github_settings = github_settings
        self.warnings = warnings
        self._personal_path_res = [
            compile_regex(pattern, f"paths.personalPathPatterns[{idx}]")
            for idx, pattern in enumerate(paths.get("personalPathPatterns") or [])
        ]

    def describe(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "id": self.id,
            "sha256": self.digest,
            "fallbackRuleCount": len(self.fallback_rules),
            "piiEnabled": self.pii_enabled,
            "piiRuleCount": len(self.pii_rules) if self.pii_enabled else 0,
            "metadataEnabled": bool(self.metadata.get("enabled", True)),
            "maskingPreviewChars": self.masking.get("previewChars", 0),
        }


def default_policy() -> Policy:
    fallback = []
    for rule in BUILTIN_FALLBACK_RULES:
        item = dict(rule)
        item["_compiled"] = compile_regex(item["regex"], f"builtin:{item['id']}")
        fallback.append(item)
    pii = []
    for rule in BUILTIN_TW_PII_RULES:
        item = dict(rule)
        item["_compiled"] = compile_regex(item["pattern"], f"builtin:{item['id']}")
        pii.append(item)
    paths = {
        "sensitiveGlobs": list(DEFAULT_SENSITIVE_GLOBS),
        "sensitiveSeverity": "high",
        "templateGlobs": list(DEFAULT_TEMPLATE_GLOBS),
        "personalPathPatterns": list(DEFAULT_PERSONAL_PATH_PATTERNS),
        "personalPathSeverity": "medium",
        "personalPathAllow": list(DEFAULT_PERSONAL_PATH_ALLOW),
    }
    return Policy(
        source="builtin-defaults",
        path=None,
        digest=None,
        policy_id="builtin-defaults",
        fallback_rules=fallback,
        pii_enabled=False,
        pii_rules=pii,
        paths_skip=list(DEFAULT_PATHS_SKIP),
        paths=paths,
        max_file_bytes=DEFAULT_LIMITS["maxFileBytes"],
        limits=dict(DEFAULT_LIMITS),
        masking=dict(DEFAULT_MASKING),
        metadata=dict(DEFAULT_METADATA),
        gitleaks_rule_severity={},
        gitleaks_config=None,
        github_settings=dict(DEFAULT_GITHUB_SETTINGS),
        warnings=[],
    )


def _positive_int(value: Any, where: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AuditError(f"政策錯誤：{where} 必須是正整數，得到 {value!r}")
    return value


def load_policy(path: Optional[str]) -> Policy:
    if path is None:
        return default_policy()
    if not os.path.isfile(path):
        raise AuditError(f"政策檔不存在：{path}")
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise AuditError(f"政策檔無法讀取：{scrub(str(exc))}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuditError(f"政策檔不是合法 UTF-8 JSON：{scrub(str(exc))}") from exc
    if not isinstance(data, dict):
        raise AuditError("政策檔最外層必須是 JSON 物件")

    warnings: List[str] = []
    schema = data.get("schemaVersion", POLICY_SCHEMA_VERSION)
    if schema != POLICY_SCHEMA_VERSION:
        warnings.append(f"政策 schemaVersion 為 {schema!r}，本工具針對 {POLICY_SCHEMA_VERSION} 撰寫")

    known_top = {
        "schemaVersion",
        "id",
        "description",
        "secrets",
        "pii",
        "paths",
        "masking",
        "limits",
        "metadata",
        "gitleaks",
        "githubSettings",
    }
    for key in sorted(set(data) - known_top):
        warnings.append(f"忽略不認知的政策欄位：{key}")

    secrets = data.get("secrets") or {}
    if not isinstance(secrets, dict):
        raise AuditError("政策錯誤：secrets 必須是物件")
    if "fallbackRules" in secrets:
        rules_in = secrets["fallbackRules"]
        if not isinstance(rules_in, list):
            raise AuditError("政策錯誤：secrets.fallbackRules 必須是陣列")
        fallback = []
        for idx, rule in enumerate(rules_in):
            where = f"secrets.fallbackRules[{idx}]"
            if not isinstance(rule, dict):
                raise AuditError(f"政策錯誤：{where} 必須是物件")
            rid = rule.get("id")
            pattern = rule.get("regex")
            if not isinstance(rid, str) or not rid:
                raise AuditError(f"政策錯誤：{where}.id 必須是非空字串")
            if not isinstance(pattern, str) or not pattern:
                raise AuditError(f"政策錯誤：{where}.regex 必須是非空字串")
            severity = _validate_severity(rule.get("severity", "medium"), where)
            fallback.append(
                {
                    "id": rid,
                    "severity": severity,
                    "description": str(rule.get("description", rid)),
                    "regex": pattern,
                    "_compiled": compile_regex(pattern, where),
                }
            )
    else:
        fallback = []
        for rule in BUILTIN_FALLBACK_RULES:
            item = dict(rule)
            item["_compiled"] = compile_regex(item["regex"], f"builtin:{item['id']}")
            fallback.append(item)

    pii = data.get("pii") or {}
    if not isinstance(pii, dict):
        raise AuditError("政策錯誤：pii 必須是物件")
    pii_enabled = _policy_bool(pii.get("enabled"), "pii.enabled", False)
    if "rules" in pii:
        if not isinstance(pii["rules"], list):
            raise AuditError("政策錯誤：pii.rules 必須是陣列")
        pii_rules_in = pii["rules"]
    else:
        pii_rules_in = BUILTIN_TW_PII_RULES
    pii_rules: List[Dict[str, Any]] = []
    for idx, rule in enumerate(pii_rules_in):
        where = f"pii.rules[{idx}]"
        if not isinstance(rule, dict):
            raise AuditError(f"政策錯誤：{where} 必須是物件")
        rid = rule.get("id")
        pattern = rule.get("pattern")
        if not isinstance(rid, str) or not rid:
            raise AuditError(f"政策錯誤：{where}.id 必須是非空字串")
        if not isinstance(pattern, str) or not pattern:
            raise AuditError(f"政策錯誤：{where}.pattern 必須是非空字串")
        severity = _validate_severity(rule.get("severity", "medium"), where)
        validator = rule.get("validate")
        if validator is not None and not callable(VALIDATORS.get(validator)):
            if isinstance(validator, str) and validator in UNSUPPORTED_VALIDATORS:
                raise AuditError(
                    f"政策錯誤：{where}.validate='{validator}' 不支援：統一編號第 7 碼為 7 的特例"
                    "未實作，硬做會給出錯誤判斷；請改用自訂規則或移除該規則"
                )
            raise AuditError(
                f"政策錯誤：{where}.validate 必須是 {sorted(VALIDATORS)} 之一或 null，得到 {validator!r}"
            )
        pii_rules.append(
            {
                "id": rid,
                "severity": severity,
                "description": str(rule.get("description", rid)),
                "pattern": pattern,
                "validate": validator,
                "_compiled": compile_regex(pattern, where),
            }
        )

    paths_raw = data.get("paths") or {}
    if not isinstance(paths_raw, dict):
        raise AuditError("政策錯誤：paths 必須是物件")
    paths_skip = _string_list(paths_raw.get("skipGlobs"), "paths.skipGlobs", DEFAULT_PATHS_SKIP)
    max_file_bytes = _positive_int(
        paths_raw.get("maxFileBytes"), "paths.maxFileBytes", DEFAULT_LIMITS["maxFileBytes"]
    )
    paths = {
        "sensitiveGlobs": _string_list(
            paths_raw.get("sensitiveGlobs"), "paths.sensitiveGlobs", DEFAULT_SENSITIVE_GLOBS
        ),
        "sensitiveSeverity": _validate_severity(
            paths_raw.get("sensitiveSeverity", DEFAULT_PATHS["sensitiveSeverity"]),
            "paths.sensitiveSeverity",
        ),
        "templateGlobs": _string_list(
            paths_raw.get("templateGlobs"), "paths.templateGlobs", DEFAULT_TEMPLATE_GLOBS
        ),
        "personalPathPatterns": _string_list(
            paths_raw.get("personalPathPatterns"),
            "paths.personalPathPatterns",
            DEFAULT_PERSONAL_PATH_PATTERNS,
        ),
        "personalPathSeverity": _validate_severity(
            paths_raw.get("personalPathSeverity", DEFAULT_PATHS["personalPathSeverity"]),
            "paths.personalPathSeverity",
        ),
        "personalPathAllow": _string_list(
            paths_raw.get("personalPathAllow"), "paths.personalPathAllow", DEFAULT_PERSONAL_PATH_ALLOW
        ),
    }

    raw_limits = data.get("limits") or {}
    if not isinstance(raw_limits, dict):
        raise AuditError("政策錯誤：limits 必須是物件")
    limits = {
        key: _positive_int(raw_limits.get(key), f"limits.{key}", DEFAULT_LIMITS[key])
        for key in DEFAULT_LIMITS
        if key != "maxFileBytes"
    }
    limits["maxFileBytes"] = max_file_bytes

    masking_raw = data.get("masking") or {}
    if not isinstance(masking_raw, dict):
        raise AuditError("政策錯誤：masking 必須是物件")
    preview_chars = masking_raw.get("previewChars", DEFAULT_MASKING["previewChars"])
    if isinstance(preview_chars, bool) or not isinstance(preview_chars, int) or not 0 <= preview_chars <= 8:
        raise AuditError("政策錯誤：masking.previewChars 必須是 0..8 的整數")
    masking = {"previewChars": preview_chars}
    if preview_chars > 0:
        warnings.append(
            f"政策啟用了秘密原文預覽（masking.previewChars={preview_chars}），"
            "輸出會包含前幾個字元，請確認報告的流傳範圍"
        )

    metadata_raw = data.get("metadata") or {}
    if not isinstance(metadata_raw, dict):
        raise AuditError("政策錯誤：metadata 必須是物件")
    metadata = {
        "enabled": _policy_bool(metadata_raw.get("enabled"), "metadata.enabled", DEFAULT_METADATA["enabled"]),
        "emailSeverity": _validate_severity(
            metadata_raw.get("emailSeverity", DEFAULT_METADATA["emailSeverity"]),
            "metadata.emailSeverity",
            allow_off=True,
        ),
        "phoneEmailSeverity": _validate_severity(
            metadata_raw.get("phoneEmailSeverity", DEFAULT_METADATA["phoneEmailSeverity"]),
            "metadata.phoneEmailSeverity",
            allow_off=True,
        ),
        "nameSeverity": _validate_severity(
            metadata_raw.get("nameSeverity", DEFAULT_METADATA["nameSeverity"]),
            "metadata.nameSeverity",
            allow_off=True,
        ),
        "scanCommitMessage": _policy_bool(
            metadata_raw.get("scanCommitMessage"),
            "metadata.scanCommitMessage",
            DEFAULT_METADATA["scanCommitMessage"],
        ),
        "allowedEmails": _string_list(
            metadata_raw.get("allowedEmails"), "metadata.allowedEmails", DEFAULT_METADATA["allowedEmails"]
        ),
        "ignoreNoReply": _policy_bool(
            metadata_raw.get("ignoreNoReply"),
            "metadata.ignoreNoReply",
            DEFAULT_METADATA["ignoreNoReply"],
        ),
        "knownNoReplyDomains": _string_list(
            metadata_raw.get("knownNoReplyDomains"),
            "metadata.knownNoReplyDomains",
            DEFAULT_METADATA["knownNoReplyDomains"],
        ),
    }

    gl = data.get("gitleaks") or {}
    if not isinstance(gl, dict):
        raise AuditError("政策錯誤：gitleaks 必須是物件")
    raw_rule_sev = gl.get("ruleSeverity") or {}
    if not isinstance(raw_rule_sev, dict):
        raise AuditError("政策錯誤：gitleaks.ruleSeverity 必須是物件")
    rule_severity = {}
    for key, value in raw_rule_sev.items():
        if not isinstance(key, str):
            raise AuditError("政策錯誤：gitleaks.ruleSeverity 的鍵必須是字串")
        rule_severity[key] = _validate_severity(value, f"gitleaks.ruleSeverity[{key!r}]")
    gitleaks_config = gl.get("config")
    if gitleaks_config is not None and not isinstance(gitleaks_config, str):
        raise AuditError("政策錯誤：gitleaks.config 必須是路徑字串或 null")

    gh_raw = data.get("githubSettings") or {}
    if not isinstance(gh_raw, dict):
        raise AuditError("政策錯誤：githubSettings 必須是物件")
    github_settings = {
        "visibilitySeverity": _validate_severity(
            gh_raw.get("visibilitySeverity", DEFAULT_GITHUB_SETTINGS["visibilitySeverity"]),
            "githubSettings.visibilitySeverity",
            allow_off=True,
        ),
        "securitySeverity": _validate_severity(
            gh_raw.get("securitySeverity", DEFAULT_GITHUB_SETTINGS["securitySeverity"]),
            "githubSettings.securitySeverity",
            allow_off=True,
        ),
    }

    return Policy(
        source="file",
        path=os.path.abspath(path),
        digest=digest,
        policy_id=str(data.get("id", os.path.basename(path))),
        fallback_rules=fallback,
        pii_enabled=pii_enabled,
        pii_rules=pii_rules,
        paths_skip=paths_skip,
        paths=paths,
        max_file_bytes=max_file_bytes,
        limits=limits,
        masking=masking,
        metadata=metadata,
        gitleaks_rule_severity=rule_severity,
        gitleaks_config=gitleaks_config,
        github_settings=github_settings,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# checksum 驗證器（降低 PII 誤判）
# ---------------------------------------------------------------------------


# 身分證首字母 → 驗證代碼（兩位數）。**不是** A=10…Z=35 的順序對應：
# I=34、O=35（避免與數字 1、0 混淆）、W=32、Z=33 是特例。
# 逐字抄錄自公開說明：
#   https://qoopio.com/idcard-number
#   https://people.cs.nycu.edu.tw/~tsaiwn/oop/online/idcheck/idrule.htm
TW_NATIONAL_ID_LETTER_CODES: Dict[str, int] = {
    "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17,
    "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22, "O": 35, "P": 23,
    "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28, "V": 29, "W": 32, "X": 30,
    "Y": 31, "Z": 33,
}

# 身分證字號 = 首字母 + 8 個數字 + 1 個檢查碼；第二碼是性別碼（1 男 / 2 女）。
TW_NATIONAL_ID_SHAPE = re.compile(r"[A-Z][12][0-9]{7}[0-9]")


def validate_tw_national_id(value: str) -> bool:
    """台灣身分證字號檢查碼。

    規則（依上述公開說明）：
    1. 首字母查表換算成兩位數驗證代碼（I=34、O=35、W=32、Z=33 為特例）。
    2. 代碼的十位數乘 1、個位數乘 9。
    3. 其後 8 個數字依序乘 8、7、6、5、4、3、2、1。
    4. 再加上末碼檢查碼，總和必須是 10 的倍數。

    公開算例：A123456789 → 1 + 0 + 8+14+18+20+20+18+14+8 + 9 = 130（可整除 10）。
    """
    if not TW_NATIONAL_ID_SHAPE.fullmatch(value):
        return False
    code = TW_NATIONAL_ID_LETTER_CODES.get(value[0])
    if code is None:  # pragma: no cover - 表格已涵蓋全部 26 個字母
        return False
    total = (code // 10) * 1 + (code % 10) * 9
    for index, weight in enumerate(range(8, 0, -1), start=1):
        total += int(value[index]) * weight
    total += int(value[9])
    return total % 10 == 0


def validate_luhn(value: str) -> bool:
    if not value.isdigit():
        return False
    total = 0
    for index, ch in enumerate(reversed(value)):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


VALIDATORS: Dict[str, Any] = {
    "tw_national_id": validate_tw_national_id,
    "luhn": validate_luhn,
}


# ---------------------------------------------------------------------------
# metadata 規則（email / 電話型 email / 姓名）
# ---------------------------------------------------------------------------

IDENT_RE = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^<>]*)>")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")

# local part 裡「一段連續的電話號碼」：數字之間可夾電話常見分隔符，但字母會切斷。
# 用比對「整串」而非「整個 local part」，才抓得到 demo0912345678 這種前綴寫法。
PHONE_RUN_RE = re.compile(r"[0-9][0-9\-+.()\s]*")
PHONE_MIN_DIGITS = 7
PHONE_MAX_DIGITS = 15


def parse_ident(ident: str) -> Tuple[str, str]:
    """把 `Name <email> 1700000000 +0800` 拆成 (name, email)。"""
    match = IDENT_RE.match(ident.strip())
    if not match:
        return "", ""
    return match.group("name").strip(), match.group("email").strip()


# GitHub 公開的匿名 commit identity：<numeric-account-id>+<login>@users.noreply.github.com。
# 前段數字是 provider 發給帳號的機器 ID，不是個人資料，不該被當成電話；
# 但 login 段仍要照常檢查（demo0912345678 這種 login 還是 high）。
# login 依 GitHub 公開規格：字母／數字／連字號，最長 39 字元，且不得含換行。
GITHUB_NOREPLY_DOMAIN = "users.noreply.github.com"
GITHUB_NOREPLY_LOCAL_RE = re.compile(
    r"[0-9]{1,20}\+(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)"
)


def _phone_scan_target(email: str) -> str:
    """回傳要做電話比對的那一段 local part。"""
    local = email.split("@", 1)[0]
    domain = email.rsplit("@", 1)[-1].strip().lower().rstrip(".") if "@" in email else ""
    if domain != GITHUB_NOREPLY_DOMAIN:
        return local
    match = GITHUB_NOREPLY_LOCAL_RE.fullmatch(local)
    if not match:
        return local  # 形狀不符 → 一律當成整串檢查，不給例外
    return match.group("login")


def looks_like_phone_email(email: str) -> bool:
    """local part 裡是否含一段 7–15 位的電話型數字 → 個人可識別資訊，等級預設 high。

    比對「數字區段」而不是整個 local part，所以字母前綴/後綴都蓋不掉：
    demo0912345678、j0912345678、phone0912345678work、09-1234-5678 都算。
    字母會切斷區段（a1b2c3 不算），短年份與 4 位數字也不算。

    唯一例外是 GitHub 匿名 identity 的 <數字ID>+<login> 形狀：只檢查 login 段，
    因為前面的數字是 provider 的機器 ID。這是窄例外，不是整個 email 放行。
    """
    target = _phone_scan_target(email)
    for match in PHONE_RUN_RE.finditer(target):
        digits = len(re.sub(r"\D", "", match.group(0)))
        if PHONE_MIN_DIGITS <= digits <= PHONE_MAX_DIGITS:
            return True
    return False


def is_known_noreply_email(email: str, known_domains: Sequence[str]) -> bool:
    """是否為**已知**匿名提交 domain 的信箱；只比對 domain，不看 local part 內容。"""
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1].strip().lower().rstrip(".")
    return domain in {d.strip().lower() for d in known_domains}


def email_allowed(email: str, policy: Policy) -> bool:
    lowered = email.lower()
    for pattern in policy.metadata.get("allowedEmails") or []:
        if pattern.lower() == lowered or fnmatch.fnmatch(lowered, pattern.lower()):
            return True
    return False


# ---------------------------------------------------------------------------
# 找尋結果
# ---------------------------------------------------------------------------


def make_finding(
    *,
    rule_id: str,
    severity: str,
    source: str,
    kind: str,
    raw: Optional[str],
    policy: Policy,
    path: Optional[str] = None,
    line: Optional[int] = None,
    ref: Optional[str] = None,
    commit: Optional[str] = None,
    extra_description: Optional[str] = None,
    extra_mask: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _remember_secret(raw)
    location: Dict[str, Any] = {"kind": kind}
    if ref:
        location["ref"] = ref
    if commit:
        location["commit"] = commit
    if path is not None:
        location["path"] = path
    if line is not None:
        location["line"] = line
    masked = mask_description(raw, policy.masking.get("previewChars", 0))
    if extra_mask:
        masked = dict(masked)
        masked["additional"] = extra_mask
    return {
        "ruleId": rule_id,
        "severity": severity,
        "source": source,
        "location": location,
        "masked": masked,
        "description": extra_description or "內容比對到規則，秘密原文已遮罩",
    }


def make_policy_finding(
    *,
    rule_id: str,
    severity: str,
    source: str,
    kind: str,
    policy: Policy,
    description: str,
    path: Optional[str] = None,
    commit: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """不帶原文的發現（只由政策判定，例如檔名規則）。"""
    location: Dict[str, Any] = {"kind": kind}
    if commit:
        location["commit"] = commit
    if path is not None:
        location["path"] = path
    masked: Dict[str, Any] = mask_description(None, policy.masking.get("previewChars", 0))
    if extra:
        masked = dict(masked)
        masked["additional"] = extra
    return {
        "ruleId": rule_id,
        "severity": severity,
        "source": source,
        "location": location,
        "masked": masked,
        "description": description,
    }


def worst_severity(findings: Sequence[Dict[str, Any]]) -> Optional[str]:
    best = None
    for finding in findings:
        sev = finding.get("severity", "low")
        if best is None or SEVERITY_RANK.get(sev, 1) > SEVERITY_RANK.get(best, 1):
            best = sev
    return best


# ---------------------------------------------------------------------------
# 內建 regex 引擎
# ---------------------------------------------------------------------------


class FallbackEngine:
    """有限 regex + 選用 PII 比對。

    沒有 Gitleaks 時是唯一訊號（仍不足以取代）；有 Gitleaks 時補足它不掃的
    地方：commit metadata、commit 訊息、政策檔名與個人路徑。
    """

    name = "fallback-regex"

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.rules: List[Dict[str, Any]] = list(policy.fallback_rules)

    @property
    def available(self) -> bool:
        return bool(self.rules)

    @property
    def pii_available(self) -> bool:
        return bool(self.policy.pii_enabled and self.policy.pii_rules)

    def scan_text(
        self,
        text: str,
        *,
        source: str,
        kind: str,
        path: Optional[str] = None,
        commit: Optional[str] = None,
        ref: Optional[str] = None,
        line_offset: int = 0,
    ) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        for rule in self.rules:
            findings.extend(self._apply(rule, text, source, kind, path, commit, ref, line_offset))
        return findings[:500]

    def scan_pii(
        self,
        text: str,
        *,
        kind: str,
        path: Optional[str] = None,
        commit: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self.pii_available:
            return []
        findings: List[Dict[str, Any]] = []
        for rule in self.policy.pii_rules:
            findings.extend(self._apply(rule, text, "content", kind, path, commit, None, 0))
        return findings

    def scan_text_full(
        self,
        text: str,
        *,
        source: str,
        kind: str,
        path: Optional[str] = None,
        commit: Optional[str] = None,
        ref: Optional[str] = None,
        line_offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """secret 規則 + 啟用中的 PII 規則。"""
        findings = self.scan_text(
            text, source=source, kind=kind, path=path, commit=commit, ref=ref, line_offset=line_offset
        )
        if self.pii_available:
            for finding in self.scan_pii(text, kind=kind, path=path, commit=commit):
                finding["source"] = f"{source}:pii-tw"
                findings.append(finding)
        return findings[:500]

    def scan_personal_paths(
        self,
        text: str,
        *,
        kind: str,
        path: Optional[str] = None,
        commit: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        severity = self.policy.paths.get("personalPathSeverity", "medium")
        if severity == "off":
            return []
        allow = self.policy.paths.get("personalPathAllow") or []
        findings: List[Dict[str, Any]] = []
        for pattern in self.policy._personal_path_res:
            for match in pattern.finditer(text):
                raw = match.group(0)
                if any(token and token in raw for token in allow):
                    continue
                findings.append(
                    make_finding(
                        rule_id="personal-local-path",
                        severity=severity,
                        source="metadata:personal-path" if kind.endswith("metadata") else "content:personal-path",
                        kind=kind,
                        raw=raw,
                        policy=self.policy,
                        path=path,
                        commit=commit,
                        extra_description=(
                            "內容含個人本機路徑（政策 paths.personalPathPatterns），"
                            "可能暴露使用者帳號名，已遮罩"
                        ),
                    )
                )
                if len(findings) >= 50:
                    return findings
        return findings

    def _apply(
        self,
        rule: Dict[str, Any],
        text: str,
        source: str,
        kind: str,
        path: Optional[str],
        commit: Optional[str],
        ref: Optional[str],
        line_offset: int,
    ) -> List[Dict[str, Any]]:
        validator = VALIDATORS.get(rule.get("validate") or "")
        pattern: "re.Pattern[str]" = rule["_compiled"]
        findings: List[Dict[str, Any]] = []
        for match in pattern.finditer(text):
            raw = match.group(0)
            if validator is not None and not validator(raw):
                continue
            line = line_offset + text.count("\n", 0, match.start()) + 1
            findings.append(
                make_finding(
                    rule_id=rule["id"],
                    severity=rule["severity"],
                    source=f"{source}:{self.name}",
                    kind=kind,
                    raw=raw,
                    policy=self.policy,
                    path=path,
                    line=line,
                    ref=ref,
                    commit=commit,
                    extra_description=f"{rule['description']}（內建規則比對，秘密原文已遮罩）",
                )
            )
            if len(findings) >= 200:
                break
        return findings


# ---------------------------------------------------------------------------
# Gitleaks 引擎
# ---------------------------------------------------------------------------


class GitleaksEngine:
    """以 Gitleaks 為高風險密鑰的正式檢查。

    以 --exit-code 99 表示「找到洩漏」，這樣可以區分：
      0  = 沒有發現
      99 = 有發現
      其他非 0 = Gitleaks 自身錯誤（它預設「發現或錯誤」都回 1）

    掃描範圍與內建引擎一致：以 `git --no-walk <commit...>` 逐批帶入明確的
    commit 清單（實測 8.30.1：`--no-walk` 只掃列出的 commit，不會被其他 ref 帶走）。
    `--diff-merges=first-parent` 讓每個 merge commit 都產生 diff；git 預設會略過
    「樹與某個 parent 相同」的 merge，那種 merge 會整顆沒有內容可比對。
    """

    name = "gitleaks"

    def __init__(self, policy: Policy, override: Optional[str] = None) -> None:
        self.policy = policy
        self.binary = override or os.environ.get("REPO_PRIVACY_AUDIT_GITLEAKS") or "gitleaks"
        self.path: Optional[str] = None
        self.version: Optional[str] = None
        self.probe_error: Optional[str] = None
        self._probe()

    def _resolve(self) -> Optional[str]:
        if os.path.sep in self.binary:
            candidate = os.path.abspath(self.binary)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
            return None
        return shutil.which(self.binary)

    def _probe(self) -> None:
        resolved = self._resolve()
        if resolved is None:
            self.probe_error = (
                f"找不到 {self.binary}。本工具不會自動下載或安裝它；"
                "請自行安裝 Gitleaks 後再執行，或接受退出碼 2 的不完整結果。"
            )
            return
        try:
            proc = subprocess.run(
                [resolved, "version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.probe_error = tool_failure("gitleaks version", -1, f"無法執行（{type(exc).__name__}）")
            return
        if proc.returncode != 0:
            self.probe_error = tool_failure("gitleaks version", proc.returncode, "無法確認工具版本")
            return
        raw = proc.stdout.decode("utf-8", "replace").strip()
        match = re.search(r"\d+\.\d+\.\d+", raw)
        if not match:
            self.probe_error = "無法從 `gitleaks version` 輸出解析出版本號；視為不可用（退出碼 2）"
            return
        self.path = resolved
        self.version = match.group(0)

    @property
    def available(self) -> bool:
        return self.path is not None and self.probe_error is None

    def _base_args(self) -> List[str]:
        args = [
            self.path or self.binary,
            "git",
            "--exit-code",
            str(GITLEAKS_LEAK_EXIT),
            "--no-banner",
            "--redact=100",
            "--log-level",
            "error",
            "--report-format",
            "json",
        ]
        if self.policy.gitleaks_config:
            args.extend(["--config", self.policy.gitleaks_config])
        # 刻意不讀任何「額外參數」環境變數：那會默默改掉退出碼、遮罩或掃描範圍。
        return args

    @staticmethod
    def _env() -> Dict[str, str]:
        env = dict(os.environ)
        # 使用者既有的 Gitleaks 設定會改變規則集（= 改變掃描範圍），一律隔離。
        env.pop("GITLEAKS_CONFIG_TOML", None)
        env.pop("GITLEAKS_CONFIG", None)
        return env

    def scan(
        self,
        repo: str,
        *,
        staged: bool = False,
        commits: Optional[Sequence[str]] = None,
        diff_merges: bool = True,
    ) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
        """回傳 (findings, errors, meta)。errors 非空代表掃描不完整。"""
        errors: List[str] = []
        meta: Dict[str, Any] = {"mode": "staged" if staged else "commits", "commits": 0, "batches": 0}
        batch_list: List[Optional[List[str]]]
        if not staged:
            commit_list = list(commits or [])
            meta["commits"] = len(commit_list)
            meta["diffMerges"] = bool(diff_merges)
            if not commit_list:
                meta["note"] = "沒有要掃描的 commit，未呼叫 Gitleaks"
                return [], errors, meta
            batch_list = [
                commit_list[index : index + GITLEAKS_BATCH_SIZE]
                for index in range(0, len(commit_list), GITLEAKS_BATCH_SIZE)
            ]
        else:
            batch_list = [None]

        findings: List[Dict[str, Any]] = []
        seen: Set[Tuple[Any, ...]] = set()
        for batch in batch_list:
            meta["batches"] += 1
            if staged:
                log_opts = None
            else:
                parts = ["--no-walk", *(batch or [])]
                if diff_merges:
                    parts.append("--diff-merges=first-parent")
                log_opts = " ".join(parts)
            batch_findings, batch_errors = self._run_once(repo, staged=staged, log_opts=log_opts)
            if batch_errors:
                errors.extend(batch_errors)
                break
            for finding in batch_findings:
                key = (
                    finding["ruleId"],
                    finding["location"].get("commit"),
                    finding["location"].get("path"),
                    finding["location"].get("line"),
                    finding["masked"].get("sha256Prefix"),
                )
                if key in seen:
                    continue
                seen.add(key)
                findings.append(finding)
        return findings, errors, meta

    def _run_once(
        self, repo: str, *, staged: bool, log_opts: Optional[str]
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        tmpdir = tempfile.mkdtemp(prefix="rpa-gitleaks-")
        report = os.path.join(tmpdir, "report.json")
        args = self._base_args() + ["--report-path", report]
        if staged:
            args.append("--staged")
        if log_opts:
            args.extend(["--log-opts", log_opts])
        args.append(repo)
        try:
            proc = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=self._env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return [], [tool_failure("gitleaks", -1, f"無法執行（{type(exc).__name__}）")]

        stderr_text = proc.stderr.decode("utf-8", "replace")
        rc = proc.returncode
        raw: List[Dict[str, Any]] = []
        if rc in (EXIT_OK, GITLEAKS_LEAK_EXIT):
            data, read_error = self._read_report(report)
            if read_error:
                shutil.rmtree(tmpdir, ignore_errors=True)
                return [], [read_error]
            raw = data
            if rc == GITLEAKS_LEAK_EXIT and not raw:
                # 宣稱找到洩漏卻交出空報告 → 無法確認結果，不當成乾淨。
                shutil.rmtree(tmpdir, ignore_errors=True)
                return [], [
                    "Gitleaks 以「找到洩漏」的退出碼結束，但報告是空的；"
                    "無法確認結果（視為未完整掃描）"
                ]
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return [], [
                tool_failure(
                    "gitleaks",
                    rc,
                    "掃描未完成（Gitleaks 自身錯誤或未支援的參數；非 0 且非 99）",
                )
            ]

        # Gitleaks 在非 Git 目錄等情況會把錯誤寫到 stderr 卻仍回 0（假的乾淨）。
        if GITLEAKS_ERROR_MARKERS.search(stderr_text):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return [], [
                tool_failure("gitleaks", rc, "Gitleaks 回報內部錯誤但退出碼正常；結果不可視為乾淨")
            ]
        shutil.rmtree(tmpdir, ignore_errors=True)
        return self._convert(raw), []

    def _read_report(self, report: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if not os.path.isfile(report):
            return [], "Gitleaks 沒有產生報告檔；無法確認掃描結果（視為未完整掃描）"
        try:
            with open(report, "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return [], "Gitleaks 報告無法解析；無法確認掃描結果（視為未完整掃描）"
        if not isinstance(data, list):
            return [], "Gitleaks 報告格式不是陣列；無法確認掃描結果"
        # 不能默默把非物件項目濾掉：那會讓格式壞掉的報告看起來像「沒有發現」。
        bad_index = next((index for index, item in enumerate(data) if not isinstance(item, dict)), None)
        if bad_index is not None:
            return [], (
                f"Gitleaks 報告第 {bad_index} 項不是物件（格式異常）；"
                "無法確認掃描結果（視為未完整掃描）"
            )
        return list(data), None

    def _convert(self, raw: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        for item in raw:
            rule_id = str(item.get("RuleID") or "gitleaks-unknown")
            severity = self.policy.gitleaks_rule_severity.get(rule_id, "high")
            # --redact=100 之後 Secret/Match 已是 REDACTED；仍不把 Match/Message/
            # Author/Email 放進報告（可能含秘密或個人資料）。
            secret = item.get("Secret")
            location_kind = "commit" if item.get("Commit") else "content"
            start_line = item.get("StartLine")
            line = start_line if isinstance(start_line, int) else None
            findings.append(
                make_finding(
                    rule_id=rule_id,
                    severity=severity,
                    source="gitleaks",
                    kind=location_kind,
                    raw=secret if isinstance(secret, str) else None,
                    policy=self.policy,
                    path=str(item.get("File")) if item.get("File") else None,
                    line=line,
                    commit=str(item.get("Commit")) if item.get("Commit") else None,
                    extra_description=(
                        f"Gitleaks 規則 {rule_id}（高風險密鑰正式檢查，秘密原文已遮罩）"
                    ),
                    extra_mask={
                        "entropy": item.get("Entropy"),
                        "fingerprint": scrub(str(item.get("Fingerprint") or ""), 120) or None,
                        "commitDate": item.get("Date") or None,
                    },
                )
            )
        return findings


# ---------------------------------------------------------------------------
# 掃描結果累積器
# ---------------------------------------------------------------------------


class ScanResult:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.findings: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.warnings: List[str] = list(policy.warnings)
        self.limitations: List[str] = []
        self.scope: Dict[str, Any] = {}
        self.engines: List[Dict[str, Any]] = []
        self.truncated = False
        self._path_findings: Set[Tuple[str, str]] = set()

    def add(self, findings: Iterable[Dict[str, Any]]) -> None:
        self.findings.extend(findings)

    def note_limit(self, note: str) -> None:
        if note not in self.limitations:
            self.limitations.append(note)

    def note_error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def add_path_finding(self, finding: Dict[str, Any]) -> None:
        """檔名/路徑類發現每個路徑只報一次，避免歷史掃描爆量。"""
        key = (str(finding["ruleId"]), str(finding["location"].get("path")))
        if key in self._path_findings:
            return
        self._path_findings.add(key)
        self.add([finding])


# ---------------------------------------------------------------------------
# 檔案內容掃描
# ---------------------------------------------------------------------------

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def is_skipped_path(path: str, policy: Policy) -> bool:
    base = os.path.basename(path)
    for glob in policy.paths_skip:
        if fnmatch.fnmatch(base, glob) or fnmatch.fnmatch(path, glob):
            return True
    return False


def matches_any(path: str, globs: Sequence[str]) -> bool:
    base = os.path.basename(path)
    for glob in globs:
        if fnmatch.fnmatch(base, glob) or fnmatch.fnmatch(path, glob):
            return True
    return False


def is_template_path(path: str, policy: Policy) -> bool:
    return matches_any(path, policy.paths.get("templateGlobs") or [])


def sensitive_path_finding(path: str, policy: Policy, *, commit: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """檔名層級的敏感判定。回傳 None 代表只是範本檔（內容仍會掃）。"""
    severity = policy.paths.get("sensitiveSeverity", "high")
    if severity == "off":
        return None
    if is_template_path(path, policy):
        return None
    if not matches_any(path, policy.paths.get("sensitiveGlobs") or []):
        return None
    extra = {
        "templateExempt": False,
        "note": "只標記檔名；內容仍交由 Gitleaks 與內建規則掃描",
    }
    return make_policy_finding(
        rule_id="sensitive-filename",
        severity=severity,
        source="policy:paths.sensitiveGlobs",
        kind="filename",
        policy=policy,
        path=path,
        commit=commit,
        description=(
            "檔名符合政策 paths.sensitiveGlobs 的敏感樣式；"
            "範本檔（.env.example 之類）不在此判定內"
        ),
        extra=extra,
    )


def decode_text(raw: bytes) -> Optional[str]:
    """把 blob 解碼成文字；明顯是二進位就回 None。"""
    if b"\x00" in raw[:8192]:
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def scan_blob(
    raw: bytes,
    path: str,
    engine: FallbackEngine,
    result: ScanResult,
    *,
    kind: str,
    commit: Optional[str] = None,
    ref: Optional[str] = None,
) -> None:
    result.scope["bytesScanned"] = result.scope.get("bytesScanned", 0) + len(raw)
    limit = result.policy.limits["maxTotalBytes"]
    if result.scope["bytesScanned"] > limit:
        result.truncated = True
        result.note_error(
            f"掃描量超過 limits.maxTotalBytes={limit}，後續檔案未掃描（結果不完整）"
        )
        return
    path_finding = sensitive_path_finding(path, result.policy, commit=commit)
    if path_finding is not None:
        result.add_path_finding(path_finding)
    text = decode_text(raw)
    if text is None:
        result.scope["filesSkipped"] = result.scope.get("filesSkipped", 0) + 1
        return
    if not engine.available and not engine.pii_available:
        return
    result.add(
        engine.scan_text_full(text, source="content", kind=kind, path=path, commit=commit, ref=ref)
    )
    result.add(engine.scan_personal_paths(text, kind=kind, path=path, commit=commit))


# ---------------------------------------------------------------------------
# metadata 掃描（author + committer + 訊息）
# ---------------------------------------------------------------------------


def scan_metadata_record(
    engine: FallbackEngine,
    result: ScanResult,
    *,
    fields: Sequence[Tuple[str, str]],
    commit: Optional[str],
    where: str,
    source_kind: str = "commit-metadata",
    scan_message: bool = True,
) -> None:
    """掃一筆 metadata。

    分成兩層，確保「關掉身分規則」不會把 high 等級的秘密規則一起藏起來：
    - 內容規則（secrets + PII + 個人路徑）：一律套用到所有欄位。
    - 身分規則（email / 電話型 email / 姓名）：受 metadata.enabled 與各 severity 控制。
    """
    policy = engine.policy
    metadata = policy.metadata
    for name, value in fields:
        if not value:
            continue
        is_message = name == "message"
        if is_message and not scan_message:
            continue
        result.add(
            engine.scan_text_full(
                value, source="metadata", kind="commit-message" if is_message else source_kind, commit=commit
            )
        )
        result.add(
            engine.scan_personal_paths(
                value,
                kind="commit-message" if is_message else source_kind,
                commit=commit,
            )
        )
        if not metadata.get("enabled", True):
            continue
        if name in ("author-email", "committer-email"):
            result.add(_identity_email_findings(engine, value, commit, where, name))
        elif name in ("author-name", "committer-name"):
            severity = metadata.get("nameSeverity", "off")
            if severity != "off":
                _remember_secret(value)
                result.add(
                    [
                        make_finding(
                            rule_id="commit-metadata-name",
                            severity=severity,
                            source="metadata:commit-ident",
                            kind=source_kind,
                            raw=value,
                            policy=policy,
                            commit=commit,
                            path=where,
                            extra_description=f"{name}（{source_kind}，已遮罩；預設 off 不報）",
                        )
                    ]
                )


def _identity_email_findings(
    engine: FallbackEngine,
    email: str,
    commit: Optional[str],
    where: str,
    field: str,
) -> List[Dict[str, Any]]:
    policy = engine.policy
    metadata = policy.metadata
    if not EMAIL_RE.match(email):
        return []
    if email_allowed(email, policy):
        return []
    # 順序有意義：先判電話。no-reply 只能略過「機器帳號」，
    # 不能讓任何含電話型數字的信箱因為字串裡有 noreply 就被跳過。
    findings: List[Dict[str, Any]] = []
    if looks_like_phone_email(email):
        severity = metadata.get("phoneEmailSeverity", "high")
        if severity != "off":
            findings.append(
                make_finding(
                    rule_id="commit-metadata-phone-email",
                    severity=severity,
                    source="metadata:commit-ident",
                    kind="commit-metadata",
                    raw=email,
                    policy=policy,
                    commit=commit,
                    path=where,
                    extra_description=(
                        f"{field} 的 local part 含 {PHONE_MIN_DIGITS}–{PHONE_MAX_DIGITS} 位"
                        f"電話型數字（metadata.phoneEmailSeverity={severity}，預設 high）；已遮罩"
                    ),
                )
            )
        return findings
    if metadata.get("ignoreNoReply", True) and is_known_noreply_email(
        email, metadata.get("knownNoReplyDomains") or DEFAULT_KNOWN_NOREPLY_DOMAINS
    ):
        return []
    severity = metadata.get("emailSeverity", "low")
    if severity != "off":
        findings.append(
            make_finding(
                rule_id="commit-metadata-email",
                severity=severity,
                source="metadata:commit-ident",
                kind="commit-metadata",
                raw=email,
                policy=policy,
                commit=commit,
                path=where,
                extra_description=(
                    f"{field}（commit metadata，已遮罩；預設 low 不阻擋，"
                    "可在政策 metadata.emailSeverity 調整）"
                ),
            )
        )
    return findings


def scan_commit_metadata(
    git: Git,
    policy: Policy,
    result: ScanResult,
    engines: Engines,
    commits: Sequence[str],
) -> None:
    """讀 author/committer 的 name、email 與 commit 訊息。"""
    fmt = "%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e"
    out = git.run(["log", "--no-walk", "--no-color", f"--format={fmt}", *commits], check=False)
    if out.returncode != 0:
        result.note_error(tool_failure("git log", out.returncode, "無法讀取 commit metadata"))
        return
    scan_message = bool(policy.metadata.get("scanCommitMessage", True))
    if not scan_message:
        result.note_limit(
            "commit 訊息未用秘密/PII 規則掃描（政策 metadata.scanCommitMessage=false，"
            "明確選擇；但訊息裡若有高風險密鑰，本工具不會因此報出）"
        )
    text = out.stdout.decode("utf-8", "surrogateescape")
    for record in text.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split("\x1f")
        if len(fields) < 6:
            continue
        sha, an, ae, cn, ce, message = fields[:6]
        scan_metadata_record(
            engines.fallback,
            result,
            fields=[
                ("author-name", an),
                ("author-email", ae),
                ("committer-name", cn),
                ("committer-email", ce),
                ("message", message),
            ],
            commit=sha,
            where=f"<commit {sha[:12]} metadata>",
            scan_message=scan_message,
        )


# ---------------------------------------------------------------------------
# 共用：掃描一組明確的 commit
# ---------------------------------------------------------------------------


def scan_commit_set(
    git: Git,
    policy: Policy,
    result: ScanResult,
    engines: Engines,
    commits: Sequence[str],
    label: str,
    extra_scope: Optional[Dict[str, Any]] = None,
) -> None:
    """metadata + patch + Gitleaks 都掃同一組 commit，範圍不會互相不一致。"""
    selected = list(dict.fromkeys(commits))
    result.scope = {
        "kind": "history",
        "revision": label,
        "commits": len(selected),
        "filesSkipped": 0,
        "bytesScanned": 0,
    }
    if extra_scope:
        result.scope.update(extra_scope)
    max_commits = policy.limits["maxCommits"]
    if len(selected) > max_commits:
        result.scope["commitsTotal"] = len(selected)
        selected = selected[:max_commits]
        result.scope["commits"] = len(selected)
        result.truncated = True
        result.note_error(
            f"{label} 有 {result.scope['commitsTotal']} 個 commit，超過 limits.maxCommits="
            f"{max_commits}，只掃描前 {max_commits} 個（結果不完整）"
        )

    diff_merges = git.supports_diff_merges()
    _run_gitleaks(result, engines, git.top, commits=selected, diff_merges=diff_merges)
    if selected:
        scan_commit_metadata(git, policy, result, engines, selected)
        scan_commit_patches(git, policy, result, engines, selected, diff_merges)


def scan_commit_patches(
    git: Git,
    policy: Policy,
    result: ScanResult,
    engines: Engines,
    commits: Sequence[str],
    diff_merges: bool,
) -> None:
    # 根 commit 也要掃：git log -p 對根 commit 會輸出「相對空樹」的 diff。
    # merge commit：git 預設只在 merge 的樹與任一 parent 不同時才輸出 diff；與某個
    # parent 樹相同的 merge（例如把長期分支合回來的那顆）會被完全略過。
    # --diff-merges=first-parent 讓每個 merge 都用第一個 parent 的 diff 掃描；
    # 舊版 git 沒這個選項時退回 -m（對每個 parent 各出一份 diff）。
    diff_arg = ["--diff-merges=first-parent"] if diff_merges else ["-m"]
    fmt = "%x1e@@@COMMIT %H%n%an <%ae>%n%B"
    out = git.run(
        [
            "log",
            "--no-walk",
            "-p",
            "-U0",
            "--no-color",
            "--no-renames",
            *diff_arg,
            f"--format={fmt}",
            *commits,
        ],
        check=False,
    )
    if out.returncode != 0:
        raise AuditError(tool_failure("git log -p", out.returncode, "無法讀取 commit patch"))
    _scan_patch_stream(out.stdout.decode("utf-8", "surrogateescape"), engines.fallback, result)


def _scan_patch_stream(text: str, engine: FallbackEngine, result: ScanResult) -> None:
    commit: Optional[str] = None
    path: Optional[str] = None
    line_no = 0
    in_hunk = False
    scanned_files = 0

    for raw_line in text.split("\n"):
        if raw_line.startswith("\x1e@@@COMMIT "):
            commit = raw_line[len("\x1e@@@COMMIT ") :].strip() or None
            path = None
            in_hunk = False
            continue
        if raw_line.startswith("diff --git "):
            path = None
            in_hunk = False
            scanned_files += 1
            continue
        if raw_line.startswith("+++ "):
            candidate = raw_line[4:].split("\t")[0].rstrip()
            if candidate == "/dev/null":
                path = None
            else:
                path = candidate[2:] if candidate.startswith("b/") else candidate
            if path is not None:
                finding = sensitive_path_finding(path, result.policy, commit=commit)
                if finding is not None:
                    result.add_path_finding(finding)
            continue
        if raw_line.startswith("@@"):
            match = HUNK_RE.match(raw_line)
            in_hunk = bool(match)
            line_no = int(match.group(1)) if match else 0
            continue
        if not in_hunk or path is None:
            continue
        if raw_line.startswith("+"):
            if is_skipped_path(path, result.policy):
                continue
            if not engine.available and not engine.pii_available:
                continue
            chunk = raw_line[1:] + "\n"
            result.add(
                engine.scan_text_full(
                    chunk,
                    source="content",
                    kind="commit-patch",
                    path=path,
                    commit=commit,
                    line_offset=line_no - 1,
                )
            )
            result.add(
                engine.scan_personal_paths(chunk, kind="commit-patch", path=path, commit=commit)
            )
            line_no += 1
        elif raw_line.startswith("\\"):
            continue
        else:
            line_no += 1

    result.scope["filesScanned"] = scanned_files


# ---------------------------------------------------------------------------
# 模式：--staged（掃 index，不看 worktree）
# ---------------------------------------------------------------------------


def scan_staged(git: Git, policy: Policy, result: ScanResult, engines: Engines) -> None:
    diff = git.run(["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT"], check=False)
    if diff.returncode != 0:
        raise AuditError(tool_failure("git diff --cached", diff.returncode, "無法讀取 staged 清單"))
    paths = split_nul(diff.stdout)
    result.scope = {"kind": "index", "paths": len(paths), "filesSkipped": 0, "bytesScanned": 0}

    max_files = policy.limits["maxFiles"]
    if len(paths) > max_files:
        result.truncated = True
        result.note_error(
            f"staged 檔案數 {len(paths)} 超過 limits.maxFiles={max_files}，"
            f"只掃描前 {max_files} 個（結果不完整）"
        )
        paths = paths[:max_files]

    for path in paths:
        if is_skipped_path(path, policy):
            result.scope["filesSkipped"] = result.scope.get("filesSkipped", 0) + 1
            continue
        blob = git.run(["cat-file", "blob", f":{path}"], check=False)
        if blob.returncode != 0:
            result.scope["filesSkipped"] = result.scope.get("filesSkipped", 0) + 1
            result.note_error(
                tool_failure("git cat-file", blob.returncode, f"無法從 index 讀取 {scrub(path, 120)}")
            )
            continue
        if len(blob.stdout) > policy.max_file_bytes:
            result.scope["filesSkipped"] = result.scope.get("filesSkipped", 0) + 1
            result.note_error(
                f"{scrub(path, 120)} 超過 limits.maxFileBytes={policy.max_file_bytes}，未掃描內容"
            )
            continue
        scan_blob(blob.stdout, path, engines.fallback, result, kind="index")

    _run_gitleaks(result, engines, git.top, staged=True)
    # 無論 metadata 設定如何都檢查即將寫入的 ident：author/committer 的 email 可能
    # 是電話號碼（high），而 pending commit 的訊息還不存在，不會被漏掃。
    scan_pending_ident(git, result, engines.fallback)


def scan_pending_ident(git: Git, result: ScanResult, engine: FallbackEngine) -> None:
    """檢查即將寫入 commit 的 author / committer metadata（git var）。

    這正是 pre-commit 時「還沒落地」的 metadata：此時阻止比事後改歷史便宜。
    """
    scan_message = bool(result.policy.metadata.get("scanCommitMessage", True))
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        proc = git.run(["var", var], check=False)
        if proc.returncode != 0:
            result.note_error(
                tool_failure("git var", proc.returncode, f"無法讀取 {var}（可能尚未設定 user.name / user.email）")
            )
            continue
        ident = proc.stdout.decode("utf-8", "surrogateescape").strip()
        if not ident:
            continue
        name, email = parse_ident(ident)
        fields: List[Tuple[str, str]] = []
        if var == "GIT_AUTHOR_IDENT":
            fields += [("author-name", name), ("author-email", email)]
        else:
            fields += [("committer-name", name), ("committer-email", email)]
        scan_metadata_record(
            engine,
            result,
            fields=fields,
            commit=None,
            where=f"<git var {var}>",
            scan_message=scan_message,
        )


# ---------------------------------------------------------------------------
# 模式：--range / --history
# ---------------------------------------------------------------------------


def _parse_range(spec: str) -> Tuple[str, str]:
    if ".." not in spec:
        raise AuditError(f"--range 需要 A..B 形式，得到 {scrub(spec, 80)}")
    left, _, right = spec.partition("..")
    return left.strip(), right.strip()


def scan_range(
    git: Git,
    policy: Policy,
    result: ScanResult,
    engines: Engines,
    spec: str,
) -> None:
    left, right = _parse_range(spec)
    if not left or not right:
        raise AuditError(f"--range 需要 A..B 形式，得到 {scrub(spec, 80)}")
    for rev in (left, right):
        if git.rev_parse(rev) is None:
            raise AuditError(f"range 端點無法解析成 commit：{scrub(rev, 80)}")
    commits = git.rev_list([f"{left}^{{commit}}..{right}^{{commit}}"])
    scan_commit_set(
        git,
        policy,
        result,
        engines,
        commits,
        f"range {spec}",
        extra_scope={"kind": "range", "range": spec},
    )


def scan_history(git: Git, policy: Policy, result: ScanResult, engines: Engines) -> None:
    refs = [line for line in git.text(["for-each-ref", "--format=%(refname)"]).splitlines() if line.strip()]
    commits = git.rev_list(["--all"])
    scan_commit_set(
        git,
        policy,
        result,
        engines,
        commits,
        "all local refs",
        extra_scope={"kind": "history", "refs": refs},
    )
    result.note_limit(
        "--all 只涵蓋 refs/ 下可達的 commit；不包含 reflog、遠端未 fetch 的物件與 dangling object"
    )


# ---------------------------------------------------------------------------
# 模式：--pre-push（從 Git stdin 讀 refs）
# ---------------------------------------------------------------------------


def scan_prepush(
    git: Git,
    policy: Policy,
    result: ScanResult,
    engines: Engines,
    stdin_text: str,
) -> None:
    records = parse_prepush_stdin(stdin_text)
    result.scope = {
        "kind": "pre-push",
        "refs": [f"{r['localRef']} -> {r['remoteRef']}" for r in records],
        "commits": 0,
        "bytesScanned": 0,
        "filesSkipped": 0,
        "pushedRefs": 0,
        "deletedRefs": 0,
        "commitsPerRef": {},
        "remoteShaMissing": [],
    }
    if not records:
        result.note_limit("pre-push stdin 為空：沒有任何 ref 被推送，本次沒有內容可掃描")

    # 逐 ref 計算要推送的 commit，再 union。不能用一組多 ref 的全域排除，
    # 那會讓「B ref 已推送」誤把「A ref 尚未推送」的 commit 排除掉。
    ordered: List[str] = []
    seen: Set[str] = set()
    for record in records:
        label = f"{record['localRef']} -> {record['remoteRef']}"
        local_sha = record["localSha"]
        remote_sha = record["remoteSha"]
        if is_zero_oid(local_sha):
            result.scope["deletedRefs"] = int(result.scope["deletedRefs"]) + 1
            continue
        if git.rev_parse(local_sha) is None:
            raise AuditError(f"pre-push 的本機物件無法解析：{scrub(local_sha, 80)}")
        result.scope["pushedRefs"] = int(result.scope["pushedRefs"]) + 1

        rev_args: List[str] = [local_sha]
        if is_zero_oid(remote_sha):
            result.note_limit(f"{label}：遠端尚無此 ref，會掃描整條 local 歷史")
        elif git.rev_parse(remote_sha) is None:
            result.note_limit(
                f"{label}：遠端物件 {scrub(remote_sha, 12)} 不在本機，無法做增量比較，"
                "改掃描整條 local 歷史"
            )
            result.scope["remoteShaMissing"].append(label)
        else:
            rev_args = [local_sha, f"^{remote_sha}"]
        commits = git.rev_list(rev_args)
        result.scope["commitsPerRef"][label] = len(commits)
        for commit in commits:
            if commit not in seen:
                seen.add(commit)
                ordered.append(commit)

    result.scope["commits"] = len(ordered)
    max_commits = policy.limits["maxCommits"]
    if len(ordered) > max_commits:
        result.scope["commitsTotal"] = len(ordered)
        ordered = ordered[:max_commits]
        result.scope["commits"] = len(ordered)
        result.truncated = True
        result.note_error(
            f"將推送 {result.scope['commitsTotal']} 個 commit，超過 limits.maxCommits="
            f"{max_commits}，只掃描前 {max_commits} 個（結果不完整）"
        )

    if not ordered:
        result.note_limit(
            "沒有任何新 commit 會被推送（推送集合為空）：這代表沒有東西可洩漏，"
            "不代表既有歷史已被檢查過"
        )

    _run_gitleaks(
        result,
        engines,
        git.top,
        commits=ordered,
        diff_merges=git.supports_diff_merges(),
    )
    if ordered:
        scan_commit_metadata(git, policy, result, engines, ordered)
        scan_commit_patches(git, policy, result, engines, ordered, git.supports_diff_merges())


def parse_prepush_stdin(stdin_text: str) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    for line in stdin_text.splitlines():
        line = line.strip("\r\n")
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            raise AuditError("無法解析 pre-push stdin 行（需要 4 個空白分隔欄位）")
        local_ref, local_sha, remote_ref, remote_sha = parts
        records.append(
            {"localRef": local_ref, "localSha": local_sha, "remoteRef": remote_ref, "remoteSha": remote_sha}
        )
    return records


# ---------------------------------------------------------------------------
# 模式：--github（唯讀「設定」檢查，只用 gh API GET）
# ---------------------------------------------------------------------------


class GithubClient:
    def __init__(self, override: Optional[str] = None) -> None:
        self.binary = override or os.environ.get("REPO_PRIVACY_AUDIT_GH") or "gh"
        resolved: Optional[str] = None
        if os.path.sep in self.binary:
            candidate = os.path.abspath(self.binary)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                resolved = candidate
        if resolved is None:
            resolved = shutil.which(self.binary)
        self.path = resolved
        self.calls: List[str] = []

    @property
    def available(self) -> bool:
        return self.path is not None

    def get(self, endpoint: str) -> Tuple[Optional[Any], int]:
        """回傳 (payload, rc)。rc != 0 時不解析內容，也不回傳 stderr 原文。"""
        assert self.path
        # 只使用 GET；不帶 --method、不寫入、不上傳。
        proc = subprocess.run(
            [self.path, "api", "-X", "GET", endpoint],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.calls.append(endpoint)
        if proc.returncode != 0:
            return None, proc.returncode
        try:
            return json.loads(proc.stdout.decode("utf-8")), 0
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, -2


def derive_repo_slug(git: Optional[Git]) -> Optional[str]:
    if git is None:
        return None
    remote = git.run(["remote", "get-url", "origin"], check=False)
    if remote.returncode != 0:
        return None
    url = remote.stdout.decode("utf-8", "replace").strip()
    match = re.search(r"[:/]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?$", url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def local_branch(git: Optional[Git]) -> Optional[str]:
    if git is None:
        return None
    proc = git.run(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    if proc.returncode != 0:
        return None
    name = proc.stdout.decode("utf-8", "replace").strip()
    if not name or name == "HEAD":
        return None
    return name


def _branch_from_ref(ref: Optional[str]) -> Optional[str]:
    """把 refs/heads/main 轉成 main；tag 或其他型態的 ref 回 None。"""
    if not ref:
        return None
    name = ref.strip()
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/") :]
    if not name or "/" in name:
        return None
    return name


def scan_github(
    git: Optional[Git],
    policy: Policy,
    result: ScanResult,
    engines: Engines,
    slug: Optional[str],
    ref: Optional[str],
    branch: Optional[str],
    gh_override: Optional[str],
) -> None:
    result.scope = {
        "kind": "github-settings",
        "scope": "settings",
        "filesScanned": 0,
        "commits": 0,
        "bytesScanned": 0,
        "checks": [],
    }
    slug = slug or derive_repo_slug(git)
    if not slug:
        raise AuditError("無法判斷 GitHub repo，請用 --github-repo OWNER/NAME 指定")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", slug):
        raise AuditError("--github-repo 格式應為 OWNER/NAME")
    branch = branch or _branch_from_ref(ref) or local_branch(git) or "main"
    result.scope["repo"] = slug
    result.scope["branch"] = branch

    client = GithubClient(gh_override)
    if not client.available:
        raise AuditError(
            f"找不到 {client.binary}。GitHub 唯讀設定檢查需要 GitHub CLI（只會呼叫 GET API）。"
            "本工具不會自動安裝。"
        )
    result.engines.append(
        {"id": "gh-api", "available": True, "mode": "read-only GET", "note": "只讀設定檢查"}
    )
    result.scope["ghBinary"] = client.binary

    # 1) repository：visibility / fork / security_and_analysis
    payload, rc = client.get(f"repos/{slug}")
    if rc != 0 or not isinstance(payload, dict):
        result.scope["checks"].append(
            _github_check("repository", f"repos/{slug}", rc, "無法讀取 repository 資訊")
        )
        result.note_error(
            tool_failure("gh api", rc, f"無法讀取 repos/{slug}（repo 不存在、或權限不足）")
        )
        result.note_limit("設定無法確認時不會推論「未啟用」，該項目視為未檢查")
        return
    visibility = str(payload.get("visibility") or "")
    is_fork = bool(payload.get("fork"))
    security = payload.get("security_and_analysis")
    detail = {
        "visibility": visibility or None,
        "private": payload.get("private"),
        "archived": payload.get("archived"),
        "fork": is_fork,
        "securityAndAnalysis": security if isinstance(security, dict) else None,
    }
    result.scope["checks"].append(
        _github_check("repository", f"repos/{slug}", 0, "已讀取", detail=detail)
    )
    security_map: Dict[str, Any] = security if isinstance(security, dict) else {}
    # 只對「明確回傳 status」的欄位下結論；欄位缺失或空物件都算未檢查。
    missing_features = [
        feature
        for feature in GITHUB_REQUIRED_SECURITY_FEATURES
        if not (
            isinstance(security_map.get(feature), dict)
            and isinstance(security_map[feature].get("status"), str)
            and security_map[feature]["status"]
        )
    ]
    feature_states = {
        feature: (security_map[feature].get("status") if isinstance(security_map.get(feature), dict) else None)
        for feature in GITHUB_REQUIRED_SECURITY_FEATURES
    }
    result.scope["checks"].append(
        _github_check(
            "security-and-analysis",
            f"repos/{slug}",
            0,
            "已讀取" if not missing_features else f"未取得明確狀態：{sorted(missing_features)}",
            detail={"featureStatus": feature_states, "raw": security_map or None},
            state="ok" if not missing_features else "unknown",
        )
    )
    if missing_features:
        field_note = (
            "GitHub 未回傳 security_and_analysis（多為權限不足或方案不支援）"
            if not isinstance(security, dict) or not security_map
            else "security_and_analysis 未涵蓋所有必要欄位"
        )
        result.note_error(
            tool_failure(
                "gh api",
                0,
                f"{field_note}：{sorted(missing_features)} 無法確認是否啟用；視為未檢查（不推論為未啟用）",
            )
        )
        result.note_limit(
            f"secret scanning 等設定無法確認（{sorted(missing_features)}）：視為未檢查，不推論為未啟用"
        )
    if visibility:
        severity = policy.github_settings.get("visibilitySeverity", "medium")
        if visibility == "public" and severity != "off":
            result.add(
                [
                    make_policy_finding(
                        rule_id="github-repo-public",
                        severity=severity,
                        source="github:settings",
                        kind="github-settings",
                        policy=policy,
                        path=slug,
                        description=(
                            "GitHub 回報此 repo 為 public；本工具不會自動改可見度，"
                            "請人工確認後再處理"
                        ),
                        extra={"visibility": visibility},
                    )
                ]
            )
    if isinstance(security, dict):
        severity = policy.github_settings.get("securitySeverity", "medium")
        if severity != "off":
            for key, value in sorted(security.items()):
                if isinstance(value, dict) and value.get("status") == "disabled":
                    result.add(
                        [
                            make_policy_finding(
                                rule_id="github-security-feature-disabled",
                                severity=severity,
                                source="github:settings",
                                kind="github-settings",
                                policy=policy,
                                path=f"{slug}#{key}",
                                description=(
                                    f"GitHub 回報 {key}.status=disabled（明確回報值，不是推測）；"
                                    "本工具不會自動修改設定"
                                ),
                                extra={"feature": key, "status": "disabled"},
                            )
                        ]
                    )

    # 2) forks：是否有 fork（fork 代表歷史已在別處）
    payload, rc = client.get(f"repos/{slug}/forks?per_page=1")
    if rc != 0 or not isinstance(payload, list):
        result.scope["checks"].append(
            _github_check("forks", f"repos/{slug}/forks?per_page=1", rc, "無法列出 forks")
        )
        result.note_error(tool_failure("gh api", rc, "無法讀取 forks 端點"))
    else:
        result.scope["checks"].append(
            _github_check(
                "forks",
                f"repos/{slug}/forks?per_page=1",
                0,
                "已讀取",
                detail={"count": len(payload), "atLeastOne": bool(payload)},
            )
        )
        if payload:
            result.note_limit(
                "此 repo 有 fork：歷史可能已存在於其他帳號，本工具無法也不會檢查那些地方"
            )

    # 3) Dependabot alerts：端點可用性只反映「能否讀取」，不推論有沒有漏洞
    payload, rc = client.get(f"repos/{slug}/dependabot/alerts?per_page=1")
    if rc != 0 or not isinstance(payload, list):
        result.scope["checks"].append(
            _github_check(
                "dependabot-alerts",
                f"repos/{slug}/dependabot/alerts?per_page=1",
                rc,
                "無法讀取（可能是未啟用或權限不足，兩者無法區分）",
            )
        )
        result.note_error(
            tool_failure("gh api", rc, "無法確認 Dependabot alerts 端點狀態（未啟用與權限不足無法區分）")
        )
    else:
        result.scope["checks"].append(
            _github_check(
                "dependabot-alerts",
                f"repos/{slug}/dependabot/alerts?per_page=1",
                0,
                "已讀取（端點可用）",
                detail={"alertsReturned": len(payload)},
            )
        )
        result.note_limit("本檢查只看端點是否可讀，不解析或評估漏洞內容")

    # 4) branch protection：只查一個分支
    endpoint = f"repos/{slug}/branches/{branch}/protection"
    payload, rc = client.get(endpoint)
    if rc != 0 or not isinstance(payload, dict):
        result.scope["checks"].append(
            _github_check(
                "branch-protection",
                endpoint,
                rc,
                "無法讀取（可能是未保護或權限不足，兩者無法區分）",
            )
        )
        result.note_error(
            tool_failure(
                "gh api",
                rc,
                f"無法確認 {branch} 的 branch protection（未保護與權限不足無法區分）",
            )
        )
    else:
        detail = {
            "requiredStatusChecks": bool((payload.get("required_status_checks") or {}).get("contexts"))
            if isinstance(payload.get("required_status_checks"), dict)
            else bool(payload.get("required_status_checks")),
            "enforceAdmins": (payload.get("enforce_admins") or {}).get("enabled")
            if isinstance(payload.get("enforce_admins"), dict)
            else None,
            "requiredPullRequestReviews": bool(payload.get("required_pull_request_reviews")),
        }
        result.scope["checks"].append(
            _github_check("branch-protection", endpoint, 0, "已讀取", detail=detail)
        )

    result.note_limit(
        "--github 只檢查 GitHub 設定，不抓取 blob、不掃歷史內容；內容秘密掃描請用 --staged／--range／--history"
    )
    result.note_limit("branch protection 只檢查一個分支（scope.branch）")


# 這兩個欄位是本工具會對「有沒有啟用」下結論的項目；缺任何一個都算未檢查。
GITHUB_REQUIRED_SECURITY_FEATURES = ("secret_scanning", "secret_scanning_push_protection")


def _github_check(
    check_id: str,
    endpoint: str,
    rc: int,
    note: str,
    detail: Optional[Dict[str, Any]] = None,
    state: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": check_id,
        "endpoint": endpoint,
        "state": state or ("ok" if rc == 0 else "unknown"),
        "rc": rc,
        "note": note,
        "detail": detail or {},
    }


# ---------------------------------------------------------------------------
# 引擎協調
# ---------------------------------------------------------------------------


class Engines:
    def __init__(self, policy: Policy, gitleaks_override: Optional[str]) -> None:
        self.policy = policy
        self.fallback = FallbackEngine(policy)
        self.gitleaks = GitleaksEngine(policy, gitleaks_override)


def _run_gitleaks(
    result: ScanResult,
    engines: Engines,
    repo: str,
    *,
    staged: bool = False,
    commits: Optional[Sequence[str]] = None,
    diff_merges: bool = True,
) -> None:
    engine = engines.gitleaks
    if not engine.available:
        result.note_error(
            engine.probe_error
            or "Gitleaks 不可用：高風險密鑰沒有做正式檢查，結果不完整"
        )
        result.engines.append(
            {
                "id": engine.name,
                "available": False,
                "required": True,
                "version": engine.version,
                "note": "缺少 Gitleaks，只執行有限 regex；這不是完整的高風險密鑰掃描",
            }
        )
        return
    findings, errors, meta = engine.scan(repo, staged=staged, commits=commits, diff_merges=diff_merges)
    result.add(findings)
    for error in errors:
        result.note_error(error)
    result.engines.append(
        {
            "id": engine.name,
            "available": not errors,
            "required": True,
            "version": engine.version,
            "commits": meta.get("commits"),
            "batches": meta.get("batches"),
            "diffMerges": meta.get("diffMerges"),
            "findings": len(findings),
            "note": "高風險密鑰正式檢查（與內建引擎掃同一組 commit；秘密原文已遮罩）",
        }
    )
    if errors:
        result.note_limit("Gitleaks 未成功完成掃描")
    result.note_limit(
        "未啟用 Gitleaks 的 base64/hex 解碼與壓縮檔遞迴掃描（需在 Gitleaks 端開啟 --max-decode-depth/--max-archive-depth）"
    )


# ---------------------------------------------------------------------------
# 報告組裝
# ---------------------------------------------------------------------------


def build_report(
    *,
    mode: str,
    policy: Policy,
    result: ScanResult,
    target: str,
    fatal: Optional[str] = None,
) -> Tuple[Dict[str, Any], int]:
    findings = result.findings
    has_high = any(f.get("severity") == "high" for f in findings)
    if fatal is not None:
        status = STATUS_ERROR
        exit_code = EXIT_INCOMPLETE
    elif result.errors or result.truncated:
        status = STATUS_INCOMPLETE
        exit_code = EXIT_INCOMPLETE
    elif findings:
        status = STATUS_FINDINGS
        exit_code = EXIT_FINDING if has_high else EXIT_OK
    else:
        status = STATUS_PASSED
        exit_code = EXIT_OK

    if result.truncated and status == STATUS_INCOMPLETE:
        result.warnings.append("掃描因套用上限而截斷，結果不代表全部內容")

    engines = list(result.engines)
    if not any(e.get("id") == "fallback-regex" for e in engines):
        engines.insert(
            0,
            {
                "id": "fallback-regex",
                "available": True,
                "required": False,
                "ruleCount": len(policy.fallback_rules) + (len(policy.pii_rules) if policy.pii_enabled else 0),
                "note": "有限 regex 規則，永遠不足以取代 Gitleaks",
            },
        )
    if policy.pii_enabled:
        engines.append(
            {
                "id": "pii-tw",
                "available": True,
                "required": False,
                "ruleCount": len(policy.pii_rules),
                "note": "台灣個資格式規則（含 checksum 驗證降低誤判；不含統一編號）",
            }
        )
    else:
        engines.append({"id": "pii-tw", "available": False, "required": False, "note": "政策未啟用 pii.enabled"})

    report = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "mode": mode,
        "status": status,
        "exitCode": exit_code,
        "target": target,
        "policy": policy.describe(),
        "coverage": {
            "scope": result.scope,
            "engines": engines,
            "limits": dict(policy.limits),
            "truncated": result.truncated,
            "limitations": result.limitations,
        },
        "summary": {
            "findings": len(findings),
            "high": sum(1 for f in findings if f.get("severity") == "high"),
            "medium": sum(1 for f in findings if f.get("severity") == "medium"),
            "low": sum(1 for f in findings if f.get("severity") == "low"),
            "worst": worst_severity(findings),
        },
        "findings": findings,
        "errors": result.errors,
        "warnings": result.warnings,
    }
    if fatal is not None:
        report["errors"] = [fatal] + list(result.errors)
    # 整份報告再 scrub 一次：已比對到的秘密／姓名／本機路徑不允許出現在任何欄位。
    report = scrub_value(report)
    return report, exit_code


STATUS_LABEL = {
    STATUS_PASSED: "通過（完整掃描，未發現問題）",
    STATUS_FINDINGS: "發現問題",
    STATUS_INCOMPLETE: "未完整掃描（結果不可視為通過）",
    STATUS_ERROR: "操作錯誤",
}


def _render_scope(scope: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key, value in scope.items():
        if isinstance(value, (list, tuple, dict)):
            parts.append(f"{key}={len(value)}")
        elif value is not None:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def render_text(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    status = report["status"]
    lines.append(f"{TOOL_NAME} v{report['tool']['version']}  mode={report['mode']}  status={status}")
    lines.append(f"目標：{report['target']}")
    lines.append(f"政策：{report['policy']['id']}（{report['policy']['source']}）")
    scope = report["coverage"]["scope"]
    if scope:
        lines.append("範圍：" + _render_scope(scope))
    for check in scope.get("checks", []) if isinstance(scope, dict) else []:
        lines.append(f"  檢查 {check['id']}: {check['state']}（{check['note']}）")
    for engine in report["coverage"]["engines"]:
        mark = "有" if engine.get("available") else "無"
        version = f" v{engine['version']}" if engine.get("version") else ""
        required = "必要" if engine.get("required") else "補充"
        lines.append(f"引擎：{engine['id']} {mark}{version} [{required}] — {engine.get('note', '')}")
    lines.append("")
    lines.append(f"結論：{STATUS_LABEL[status]}")
    summary = report["summary"]
    lines.append(
        f"發現：共 {summary['findings']} 筆（high {summary['high']} / medium {summary['medium']} / low {summary['low']}）"
    )
    if report["findings"]:
        lines.append("")
        for index, finding in enumerate(report["findings"], 1):
            loc = finding["location"]
            where = " ".join(
                str(loc[key]) for key in ("kind", "ref", "commit", "path", "line") if loc.get(key) is not None
            )
            lines.append(f"[{index}] {finding['severity'].upper()} {finding['ruleId']}  {where}")
            lines.append(f"     {finding['description']}")
            lines.append(f"     {finding['masked']['description']}")
    for message in report["errors"]:
        lines.append(f"[錯誤] {message}")
    for message in report["warnings"]:
        lines.append(f"[警告] {message}")
    for note in report["coverage"]["limitations"]:
        lines.append(f"[限制] {note}")
    if report["coverage"]["truncated"]:
        lines.append("[限制] 掃描因上限被截斷")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="唯讀掃描 Git repo 的機密／個資風險，或唯讀檢查 GitHub 設定（不修復、不寫入、不推動）。",
        epilog="退出碼：0=完整且無 high；1=有 high；2=未完整掃描或操作錯誤。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="掃描 index（staged）內容，不看 worktree")
    mode.add_argument("--range", metavar="A..B", help="掃描 A..B 之間實際的 commit 內容與 metadata")
    mode.add_argument("--history", action="store_true", help="掃描所有本機 refs 可達的 commit")
    mode.add_argument("--github", action="store_true", help="唯讀檢查 GitHub 設定（只用 gh API GET）")
    mode.add_argument("--pre-push", action="store_true", dest="pre_push", help="從 Git stdin 讀 refs（pre-push hook 用）")

    parser.add_argument("--repo", default=".", help="目標 Git repo 路徑（預設目前目錄）")
    parser.add_argument("--policy", help="JSON 政策檔路徑（不 source、不 eval）")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="輸出格式（預設 text）")
    parser.add_argument("--out", help="把報告寫到檔案（JSON）")
    parser.add_argument("--gitleaks", help="Gitleaks 執行檔路徑（覆蓋 PATH 與環境變數）")
    parser.add_argument("--gh", help="GitHub CLI 執行檔路徑（覆蓋 PATH 與環境變數）")
    parser.add_argument("--github-repo", help="OWNER/NAME；未指定時從 origin 推導")
    parser.add_argument("--github-ref", help="GitHub ref（僅用來推斷要檢查的分支名稱）")
    parser.add_argument("--github-branch", help="要檢查 branch protection 的分支（預設由本機分支推導）")
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="不套用 email／姓名等身分規則（commit 訊息與內容的秘密、PII 規則仍會掃描）",
    )
    parser.add_argument(
        "--print-default-rules", action="store_true", help="輸出內建規則與政策預設（JSON）後結束"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.print_default_rules:
        print(
            json.dumps(
                {
                    "fallbackRules": [
                        {k: v for k, v in rule.items() if not k.startswith("_")} for rule in BUILTIN_FALLBACK_RULES
                    ],
                    "twPiiRules": [
                        {k: v for k, v in rule.items() if not k.startswith("_")} for rule in BUILTIN_TW_PII_RULES
                    ],
                    "unsupportedValidators": sorted(UNSUPPORTED_VALIDATORS),
                    "defaultPathsSkip": DEFAULT_PATHS_SKIP,
                    "defaultPaths": DEFAULT_PATHS,
                    "defaultLimits": DEFAULT_LIMITS,
                    "defaultMasking": DEFAULT_MASKING,
                    "defaultMetadata": DEFAULT_METADATA,
                    "defaultGithubSettings": DEFAULT_GITHUB_SETTINGS,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    if not (args.staged or args.range or args.history or args.github or args.pre_push):
        parser.error("必須指定掃描模式：--staged / --range A..B / --history / --github / --pre-push")

    mode = "staged"
    if args.staged:
        mode = "staged"
    elif args.range:
        mode = "range"
    elif args.history:
        mode = "history"
    elif args.github:
        mode = "github"
    else:
        mode = "pre-push"

    def emit(report: Dict[str, Any], exit_code: int) -> int:
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
        else:
            print(render_text(report))
        if args.out:
            try:
                with open(args.out, "w", encoding="utf-8") as handle:
                    json.dump(report, handle, ensure_ascii=False, indent=2)
            except OSError as exc:
                sys.stderr.write(f"[{TOOL_NAME}] 無法寫入報告檔（{type(exc).__name__}）\n")
                return EXIT_INCOMPLETE
        return exit_code

    try:
        policy = load_policy(args.policy)
    except AuditError as exc:
        report, exit_code = _fatal_report(mode, args, str(exc), policy=None)
        return emit(report, exit_code)

    if args.no_metadata:
        policy.metadata = dict(policy.metadata)
        policy.metadata["enabled"] = False
        policy.warnings.append(
            "已停用 metadata 身分規則（--no-metadata）；commit 訊息與內容的秘密／PII 規則仍會套用"
        )

    git: Optional[Git] = None
    if mode != "github":
        try:
            git = Git(args.repo)
        except AuditError as exc:
            report, exit_code = _fatal_report(mode, args, str(exc), policy=policy)
            return emit(report, exit_code)
    else:
        # 設定檢查只需要 origin／目前分支，不是 Git repo 也能執行。
        git = try_git(args.repo)

    engines = Engines(policy, args.gitleaks)
    result = ScanResult(policy)

    try:
        if mode == "staged":
            scan_staged(git, policy, result, engines)  # type: ignore[arg-type]
        elif mode == "range":
            scan_range(git, policy, result, engines, args.range)  # type: ignore[arg-type]
        elif mode == "history":
            scan_history(git, policy, result, engines)  # type: ignore[arg-type]
        elif mode == "pre-push":
            stdin_text = ""
            if not sys.stdin.isatty():
                stdin_text = sys.stdin.read()
            scan_prepush(git, policy, result, engines, stdin_text)  # type: ignore[arg-type]
        else:
            if engines.gitleaks.probe_error:
                result.engines.append(
                    {
                        "id": "gitleaks",
                        "available": False,
                        "required": False,
                        "version": engines.gitleaks.version,
                        "note": "設定檢查模式不需要 Gitleaks（不掃內容）",
                    }
                )
            scan_github(
                git,
                policy,
                result,
                engines,
                args.github_repo,
                args.github_ref,
                args.github_branch,
                args.gh,
            )
    except AuditError as exc:
        report, exit_code = _fatal_report(mode, args, str(exc), policy=policy, result=result)
        return emit(report, exit_code)
    except KeyboardInterrupt:
        sys.stderr.write(f"[{TOOL_NAME}] 已中止\n")
        return EXIT_INCOMPLETE
    except Exception as exc:  # pragma: no cover - 防止原始例外把秘密帶出去
        detail = scrub(f"{type(exc).__name__}: {exc}")
        report, exit_code = _fatal_report(
            mode, args, f"未預期錯誤（{detail}）；原始內容已隱藏", policy=policy, result=result
        )
        return emit(report, exit_code)

    target = git.top if git is not None else f"github:{args.github_repo or 'auto'}"
    report, exit_code = build_report(mode=mode, policy=policy, result=result, target=target)
    return emit(report, exit_code)


def _fatal_report(
    mode: str,
    args: argparse.Namespace,
    message: str,
    policy: Optional[Policy],
    result: Optional[ScanResult] = None,
) -> Tuple[Dict[str, Any], int]:
    if result is None:
        result = ScanResult(policy or default_policy())
    if policy is None:
        policy = result.policy
    return build_report(
        mode=mode, policy=policy, result=result, target=str(getattr(args, "repo", ".")), fatal=message
    )


if __name__ == "__main__":
    sys.exit(main())