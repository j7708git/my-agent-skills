#!/usr/bin/env python3
"""my-agent-skills 格式驗證器（首版，只使用 Python 3 標準函式庫）。

檢查範圍
--------
1. ``skills/<skill-name>/SKILL.md`` 存在，且 YAML frontmatter 可被首版子集解析。
2. ``name``：1–64 字元、lowercase kebab-case、不以連字號開頭或結尾、無連續連字號，
   且與父目錄名一致（Agent Skills 規格要求）。
3. ``description``：非空且不超過 1024 字元。
4. ``SKILL.md`` 內引用的相對資源（Markdown 連結、程式碼片段中的路徑）確實存在，
   且不會指到 repo 之外。
5. ``--check-docs``：必要的 repo 維護文件存在，且其中的 Markdown 相對連結確實指向存在的檔案。

首版 frontmatter 子集（刻意限制，詳見 docs/development.md）
-----------------------------------------------------------
只接受「單行純量」的頂層欄位：``name``（必填）、``description``（必填）、
``license``、``compatibility``、``allowed-tools``。不支援巢狀映射、串列、
區塊純量（``|`` / ``>``）與多行值；遇到這些寫法會報錯並指向 docs/development.md，
不會猜測或忽略。規格中的 ``metadata`` 映射屬於已知限制，完整規格請改用
``skills-ref validate``（https://agentskills.io/specification）。

文件分流（.local/ 與 .openchamber/ 不進版控）
----------------------------------------------
本檢查只驗證**公開**文件：公開文件必須自給自足，不得引用 ``.local/`` 或
``.openchamber/`` 裡的本機工作紀錄（計畫、任務卡、session 日誌、驗收紀錄、
研究筆記），否則讀者拿到的是死連結或等於要求外部使用者擁有本機工作區。
內部文件本身不被檢查（不在版控內，新鮮 clone 不存在），但若被 Git 追蹤則報錯。

退出碼
------
0：通過（可能有警告；``--strict`` 會把警告當成失敗）。
1：發現錯誤。
2：缺少可驗證的輸入（例如 ``skills/`` 不存在或沒有任何 skill），無法宣告通過。
    若同時有其他錯誤，仍以 2 為主（未完成的驗證優先回報），錯誤明細照常列出。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

TOOL = "validate-skills"
SCHEMA_VERSION = 1

NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500
MAX_BODY_LINES = 500

REQUIRED_KEYS = ("name", "description")
OPTIONAL_KEYS = ("license", "compatibility", "allowed-tools")
ALLOWED_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS

FRONTMATTER_DELIMITER = "---"
KEY_LINE = re.compile(r"(?P<key>[A-Za-z0-9][A-Za-z0-9_.-]*)[ \t]*:(?P<rest>.*)\Z")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\([ \t]*<?(?P<target>[^)\s>]+)>?[ \t]*(?:\"[^\"]*\")?[ \t]*\)")
CODE_SPAN = re.compile(r"`(?P<code>[^`\n]+)`")
PATH_TOKEN = re.compile(
    r"(?<![\w./@-])(?P<token>(?:[\w.-]+/)+[\w.-]*|SKILL\.md|README\.md|LICENSE(?:\.txt|\.md)?|CLAUDE\.md)(?![\w/])"
)
KNOWN_EXTENSIONS = (
    ".md",
    ".sh",
    ".py",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".txt",
    ".js",
    ".mjs",
    ".ts",
    ".cfg",
    ".ini",
    ".example",
)
SKIP_TOKEN_CHARS = set("*?<>{}|$%&;,()[]'\"`\\")
REQUIRED_REPO_FILES = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "docs/specification.md",
    "docs/development.md",
    "docs/compatibility.md",
)
# 只列公開文件。內部計畫與驗收紀錄（.local/）與任務卡（.openchamber/）不進版控，
# 新鮮 clone 不存在，因此不得列為必要文件，也不得因為它們缺席而報錯。
#
# docs/specification.md 是產品契約，屬於必要公開文件：沒有它，讀者無從得知
# CLI、退出碼與安全邊界，公開文件就不自足。--repo 指向其他 repo 時若沒有這份契約，
# 該 repo 應確實先建立它，而不是讓驗證器放行。
DOC_FILES = REQUIRED_REPO_FILES + (
    "docs/research/portability.md",
    "docs/writing-contract.md",
)

# 本機專屬目錄：不進版控，因此公開文件不可引用（否則對外部讀者是死連結）。
LOCAL_ONLY_DIRS = (".local", ".openchamber")


@dataclass
class Finding:
    severity: str
    path: str
    message: str
    line: int | None = None

    def as_dict(self) -> dict:
        payload = {"severity": self.severity, "path": self.path, "message": self.message}
        if self.line is not None:
            payload["line"] = self.line
        return payload

    def render(self) -> str:
        location = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"  [{self.severity}] {location} — {self.message}"


@dataclass
class Result:
    repo_name: str = "repo"
    skills_checked: int = 0
    coverage: list[str] = field(
        default_factory=lambda: [
            "skill-directory-layout",
            "frontmatter-subset",
            "name-rules",
            "description-limits",
            "relative-resource-references",
        ]
    )
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    incomplete_reason: str | None = None

    @property
    def status(self) -> str:
        if self.incomplete_reason is not None:
            return "incomplete"
        return "failed" if self.errors else "passed"

    def error(self, path: str, message: str, line: int | None = None) -> None:
        self.errors.append(Finding("error", path, message, line))

    def warn(self, path: str, message: str, line: int | None = None) -> None:
        self.warnings.append(Finding("warning", path, message, line))

    def as_dict(self) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "tool": TOOL,
            "status": self.status,
            "repo": self.repo_name,
            "skillsChecked": self.skills_checked,
            "coverage": list(self.coverage),
            "incompleteReason": self.incomplete_reason,
            "errors": [f.as_dict() for f in self.errors],
            "warnings": [f.as_dict() for f in self.warnings],
        }


def relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix() or "."
    except ValueError:
        return path.as_posix()


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner.replace("''", "'")
    return value


def parse_frontmatter(lines: list[str], rel_path: str, result: Result) -> tuple[dict[str, tuple[str, int]], str]:
    """回傳 (欄位 -> (值, 行號), 內文)。無法解析時以錯誤回報，不做猜測。"""
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        result.error(rel_path, "SKILL.md 必須以單行 '---' 開頭的 YAML frontmatter 起頭")
        return {}, ""

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_DELIMITER:
            end = index
            break
    if end is None:
        result.error(rel_path, "找不到 frontmatter 結尾的 '---'")
        return {}, ""

    fields: dict[str, tuple[str, int]] = {}
    for offset, raw in enumerate(lines[1:end], start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1] in (" ", "\t"):
            result.error(
                rel_path,
                "首版驗證器不支援巢狀結構（縮排行）；請改用單行純量欄位，詳見 docs/development.md",
                offset,
            )
            continue
        if "\t" in raw.split(":", 1)[0]:
            result.error(rel_path, "frontmatter 的鍵名不可含 Tab", offset)
            continue
        match = KEY_LINE.match(raw.rstrip())
        if match is None:
            result.error(rel_path, f"無法解析的 frontmatter 行：{raw.strip()!r}", offset)
            continue
        key = match.group("key")
        rest = match.group("rest").strip()
        if key in fields:
            result.error(rel_path, f"重複的 frontmatter 欄位：{key}", offset)
            continue
        if rest == "":
            result.error(rel_path, f"欄位 {key} 沒有值（首版僅接受單行純量）", offset)
            continue
        if rest[0] in "|>":
            result.error(rel_path, f"欄位 {key} 使用區塊純量（| 或 >），首版不支援", offset)
            continue
        if rest[0] in "[{":
            result.error(rel_path, f"欄位 {key} 使用串列／映射常值，首版不支援", offset)
            continue
        fields[key] = (unquote(rest), offset)

    body = "\n".join(lines[end + 1 :])
    return fields, body


def check_name(value: str, directory: str, rel_path: str, line: int, result: Result) -> None:
    if not 1 <= len(value) <= NAME_MAX:
        result.error(rel_path, f"name 長度必須介於 1–{NAME_MAX} 字元，目前 {len(value)}", line)
    if not NAME_PATTERN.match(value):
        result.error(
            rel_path,
            "name 必須是 lowercase kebab-case：僅允許 a-z、0-9 與單一連字號分隔，不得以連字號開頭／結尾或連續出現",
            line,
        )
    if value != directory:
        result.error(rel_path, f"name（{value}）必須與父目錄名（{directory}）一致", line)


def check_description(value: str, rel_path: str, line: int, result: Result) -> None:
    if not value.strip():
        result.error(rel_path, "description 不可為空白", line)
        return
    if len(value) > DESCRIPTION_MAX:
        result.error(rel_path, f"description 長度 {len(value)} 超過 {DESCRIPTION_MAX} 字元上限", line)
    if len(value.strip()) < 20:
        result.warn(rel_path, "description 過短；建議同時說明「做什麼」與「何時使用」", line)


def looks_like_path(token: str, strict: bool) -> bool:
    if not token or token in SKIP_TOKEN_CHARS:
        return False
    if any(char in SKIP_TOKEN_CHARS for char in token):
        return False
    if token.endswith("/"):
        return True
    if strict:
        return True
    return token.lower().endswith(KNOWN_EXTENSIONS)


def candidate_tokens(body: str, mode: str) -> Iterable[tuple[str, bool]]:
    """回傳 (token, strict)；strict 代表明確是資源引用（連結或程式碼片段）。

    mode="skill"：額外比對程式碼片段與帶副檔名的路徑，確保 skill 的相對資源自包含。
    mode="docs" ：只比對 Markdown 連結目標，避免把文件中的指令片段誤判成檔案。
    """
    for match in MARKDOWN_LINK.finditer(body):
        target = match.group("target")
        if target:
            yield target, True
    if mode != "skill":
        return
    for match in CODE_SPAN.finditer(body):
        for token in PATH_TOKEN.finditer(match.group("code")):
            yield token.group("token"), True
    for match in PATH_TOKEN.finditer(body):
        yield match.group("token"), False


def check_references(
    body: str,
    base_dirs: list[Path],
    rel_path: str,
    repo_root: Path,
    result: Result,
    mode: str = "skill",
    boundary: Path | None = None,
    boundary_label: str = "repo",
) -> None:
    """檢查文件內的相對資源引用。

    base_dirs  : 依序嘗試的解析基準目錄。
    boundary   : 解析結果不得離開的根目錄。skill 用 skill 根目錄，
                 因為安裝時只會複製 skill 目錄，引用 repo 根檔案會失效；
                 repo 文件則以 repo 根目錄為界。
    """
    limit = (boundary or repo_root).resolve()
    seen: set[str] = set()
    for token, strict in candidate_tokens(body, mode):
        if not looks_like_path(token, strict):
            continue
        clean = token.strip().rstrip(".,;:")
        clean = clean.split("#", 1)[0].split("?", 1)[0]
        if not clean or clean in seen:
            continue
        seen.add(clean)
        if "://" in clean or clean.startswith("mailto:"):
            continue
        target = clean[2:] if clean.startswith("./") else clean
        if target.startswith("/"):
            result.error(rel_path, f"相對資源引用不可為絕對路徑：{clean}")
            continue
        # 本機專屬目錄不在版控內，公開文件引用它等於給外部讀者一個死連結。
        # 先於存在性檢查擋下：這些路徑在本機確實存在，但對 clone 出去的人不存在。
        local_hit = next(
            (name for name in LOCAL_ONLY_DIRS if name in target.split("/")),
            None,
        )
        if local_hit is not None:
            result.error(
                rel_path,
                f"不可引用本機專屬路徑：{clean}"
                f"（{local_hit}/ 不進版控，公開文件必須自給自足）",
            )
            continue
        # 先做邊界檢查（含不存在也要擋），再做存在性檢查。
        # skill 的邊界是 skill 目錄：安裝時只會複製 skill 目錄，引用 repo 根檔案會失效。
        resolved = None
        escaped = False
        for base in base_dirs:
            candidate = (base / target).resolve()
            try:
                candidate.relative_to(limit)
            except ValueError:
                escaped = True
                continue
            if candidate.exists():
                resolved = candidate
                break
        if escaped:
            result.error(
                rel_path,
                f"相對資源引用指向{boundary_label}之外：{clean}"
                f"（{boundary_label}內必須自給自足；請改用{boundary_label}內的路徑）",
            )
            continue
        if resolved is None:
            tried = "、".join(relative(b, repo_root) for b in base_dirs)
            result.error(rel_path, f"找不到引用的資源：{clean}（已嘗試以 {tried} 為基準解析）")


def check_scripts(skill_dir: Path, repo_root: Path, result: Result) -> None:
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return
    for script in sorted(scripts_dir.rglob("*.sh")):
        head = script.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        first = head[0] if head else ""
        if not first.startswith("#!") or ("bash" not in first and "/sh" not in first):
            result.warn(relative(script, repo_root), "Bash 腳本缺少明確的 shebang（#! /usr/bin/env bash）")


def validate_skill(skill_dir: Path, repo_root: Path, result: Result) -> None:
    rel_dir = relative(skill_dir, repo_root)
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        result.error(rel_dir, "skill 目錄缺少必要的 SKILL.md")
        return

    try:
        text = skill_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        result.error(relative(skill_file, repo_root), f"SKILL.md 不是有效的 UTF-8：{exc}")
        return
    except OSError as exc:
        result.error(relative(skill_file, repo_root), f"無法讀取 SKILL.md：{exc}")
        return

    rel_path = relative(skill_file, repo_root)
    if not text.strip():
        result.error(rel_path, "SKILL.md 為空檔")
        return

    fields, body = parse_frontmatter(text.splitlines(), rel_path, result)

    for key in REQUIRED_KEYS:
        if key not in fields:
            result.error(rel_path, f"缺少必要的 frontmatter 欄位：{key}")

    for key in fields:
        if key not in ALLOWED_KEYS:
            allowed = "、".join(ALLOWED_KEYS)
            result.error(rel_path, f"frontmatter 欄位 {key} 不在首版允許清單（允許：{allowed}）", fields[key][1])

    if "name" in fields:
        value, line = fields["name"]
        check_name(value, skill_dir.name, rel_path, line, result)
    if "description" in fields:
        value, line = fields["description"]
        check_description(value, rel_path, line, result)
    if "compatibility" in fields:
        value, line = fields["compatibility"]
        if len(value) > COMPATIBILITY_MAX:
            result.error(rel_path, f"compatibility 長度 {len(value)} 超過 {COMPATIBILITY_MAX} 字元上限", line)

    body_lines = body.splitlines()
    if not body.strip():
        result.warn(rel_path, "frontmatter 之後沒有內文，skill 無法提供可執行指引")
    elif len(body_lines) > MAX_BODY_LINES:
        result.warn(rel_path, f"SKILL.md 內文 {len(body_lines)} 行，超過建議的 {MAX_BODY_LINES} 行；請把細節移到 references/")

    check_references(
        body,
        [skill_dir],
        rel_path,
        repo_root,
        result,
        mode="skill",
        boundary=skill_dir,
        boundary_label="skill 目錄",
    )
    check_scripts(skill_dir, repo_root, result)
    result.skills_checked += 1


def validate_skills(repo_root: Path, result: Result) -> None:
    skills_dir = repo_root / "skills"
    if not skills_dir.is_dir():
        result.incomplete_reason = f"repo 根目錄下沒有 skills/ 目錄（檢查 {relative(skills_dir, repo_root)}）"
        return
    entries = sorted(p for p in skills_dir.iterdir() if not p.name.startswith(".") and not p.name.startswith("_"))
    skill_dirs = [p for p in entries if p.is_dir()]
    if not skill_dirs:
        result.incomplete_reason = "skills/ 之下沒有任何 skill 目錄"
        return
    for skill_dir in skill_dirs:
        validate_skill(skill_dir, repo_root, result)


def tracked_paths(repo_root: Path) -> list[str] | None:
    """回傳 Git 已追蹤的路徑（相對 repo_root）；無法判斷時回傳 None。

    這是唯讀查詢（git ls-files），不改 index、不改工作樹、不寫任何設定。
    目標不是 Git 工作樹、或 git 不可用時回傳 None：呼叫端必須把「無法檢查」
    如實當成未驗證，而不是當成通過。
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout.decode("utf-8", errors="replace")
    return [item for item in raw.split("\0") if item]


def check_local_dirs_not_tracked(repo_root: Path, result: Result) -> None:
    """.local/ 與 .openchamber/ 不進版控：被 Git 追蹤就是分流失效。

    沒有 Git 工作樹時（暫存測試 fixture、未安裝 git 的環境）不報錯也不宣稱檢查過；
    本 repo 自己就是 Git 工作樹，所以 CI 與 run-checks.sh 一定會實際執行這項檢查。
    """
    tracked = tracked_paths(repo_root)
    if tracked is None:
        return
    result.coverage.append("local-dirs-not-tracked")
    offenders = [
        path
        for path in tracked
        if path.split("/")[0] in LOCAL_ONLY_DIRS
    ]
    if not offenders:
        return
    shown = "、".join(sorted(offenders)[:10])
    more = f"（另有 {len(offenders) - 10} 個）" if len(offenders) > 10 else ""
    result.error(
        ".gitignore",
        f"本機專屬路徑被 Git 追蹤，必須移出版控：{shown}{more}"
        "（.local/ 與 .openchamber/ 應由 .gitignore 排除）",
    )


def validate_docs(repo_root: Path, result: Result) -> None:
    result.coverage.extend(["required-repo-files", "markdown-relative-links"])
    for rel in REQUIRED_REPO_FILES:
        target = repo_root / rel
        if not target.is_file():
            result.error(rel, "缺少必要的 repo 維護文件")
        elif not target.read_text(encoding="utf-8", errors="replace").strip():
            result.error(rel, "維護文件為空檔")

    for rel in DOC_FILES:
        doc = repo_root / rel
        if not doc.is_file():
            continue
        check_references(
            doc.read_text(encoding="utf-8", errors="replace"),
            [doc.parent, repo_root],
            rel,
            repo_root,
            result,
            mode="docs",
            boundary=repo_root,
            boundary_label="repo",
        )

    check_local_dirs_not_tracked(repo_root, result)


def default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="驗證 my-agent-skills 的 skill 格式（Agent Skills 首版 frontmatter 子集）。",
    )
    parser.add_argument("--repo", type=Path, default=None, help="要驗證的 repo 根目錄（預設：腳本所在 repo）")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="輸出格式（預設 text）")
    parser.add_argument(
        "--check-docs",
        action="store_true",
        help=(
            "一併檢查公開文件（根層與 docs/）的相對連結、必要的維護文件，"
            "並確認 .local/、.openchamber/ 未被 Git 追蹤"
        ),
    )
    parser.add_argument("--strict", action="store_true", help="把警告也視為失敗（退出碼 1）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo.resolve() if args.repo else default_repo_root()
    result = Result(repo_name=repo_root.name)

    if not repo_root.is_dir():
        result.incomplete_reason = f"repo 路徑不存在：{repo_root}"
    else:
        validate_skills(repo_root, result)
        if args.check_docs:
            validate_docs(repo_root, result)

    failed = bool(result.errors) or (args.strict and bool(result.warnings))
    exit_code = 2 if result.incomplete_reason else (1 if failed else 0)

    if args.format == "json":
        payload = result.as_dict()
        payload["exitCode"] = exit_code
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[{TOOL}] repo：{repo_root.name}")
        print(f"[{TOOL}] 驗證的 skill 數：{result.skills_checked}")
        if result.incomplete_reason:
            print(f"[{TOOL}] 狀態：未完成（{result.incomplete_reason}）")
            print(f"[{TOOL}] 這不是通過結果：缺少可驗證的輸入，不宣告驗收完成；下列錯誤仍需修正。")
        else:
            suffix = "（--strict：警告視為失敗）" if args.strict and result.warnings else ""
            print(f"[{TOOL}] 狀態：{'失敗' if failed else '通過'}{suffix}")
        for warning in result.warnings:
            print(warning.render())
        for error in result.errors:
            print(error.render())
        print(f"[{TOOL}] 錯誤 {len(result.errors)} 項、警告 {len(result.warnings)} 項；退出碼 {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
