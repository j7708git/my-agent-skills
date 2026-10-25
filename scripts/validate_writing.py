#!/usr/bin/env python3
"""my-agent-skills 寫作契約檢查器（只使用 Python 3 標準函式庫）。

最終分層：只有機械上可靠、誤報低的 8 條規則做成自動檢查。

1. A 類前 40 行內有四欄 blockquote
2. 四欄不得為空、不得留 `<…>` 佔位符
3. 「現在要做」欄不得命中 `fillers.md`
4. 「依據」欄內反引號樣路徑必須存在
5. 整句只有「代號 ＋ 狀態詞」
6. 單檔 > 400 行
7. 公開 A 類不得含 commit SHA
8. B 類第一段缺少優先序句 B1

其餘規則（A5 條列／散文、A6、A7、A2r、A4r、B2、B3、A9r 的行為一致性）
已降為人工自檢，不在本檢查器內。所有命中都是 warning，不阻擋提交；
退出碼只表示檢查器是否完整跑完。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

TOOL = "validate-writing"
SCHEMA_VERSION = 1
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
RULES = {"A1", "A3", "A5", "A9c", "B1"}
B1_PREFIX = "> 本檔若與程式碼、測試或實際指令不符，以程式碼為準"
B1_SENTENCE = (
    "> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；"
    "安全邊界、授權與不可逆操作則以本檔為準。"
    "發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。"
)

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
FIELD = re.compile(
    r"^\s*>\s*(?:\*\*)?\s*(讀者|一句話|現在要做|依據)(?:\*\*)?\s*[：:]\s*(.*)$"
)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
CODE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(M\d+|T\d+|P\d+|Q\d+|v\d+|Phase\s*\d+|"
    r"(?:Milestone|Sprint)\s*\d+|"
    r"第\s*\d+\s*階段|第[一二三四五六七八九十]+階段|"
    r"階段[一二三四五六七八九十]+|里程碑\s*[A-Z]?\d+)"
    r"(?![A-Za-z0-9])"
)
STATUS_WORD = re.compile(
    r"(?:已完成|進行中|待處理|未完成|已啟動|已結束|已上線|已合併|已部署|"
    r"已就緒|已通過|已失敗|已取消|已暫停|開始|完成|上線|就緒|持續|暫停|"
    r"取消|通過|失敗|待定|阻塞|進行|結束|已|"
    r"(?i:done|completed|finished|started|blocked|pending|in\s+progress))"
)
SHA_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
ACTION_PIN = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}")
TABLE_ROW = re.compile(r"^\s*\|")
TABLE_CELL_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
TABLE_CELL_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA_ALLOW = {
    "deadbeef",
    "cafebabe",
    "abcdef0",
    "1234567",
    "0000000",
    EMPTY_TREE,
}
SKILL_TEMPLATES = {
    "skills/project-workflow/assets/templates/overview.md",
    "skills/project-workflow/assets/templates/progress.md",
    "skills/project-workflow/assets/templates/plan.md",
    "skills/project-workflow/assets/templates/acceptance.md",
}
B_EXTRA = {
    "skills/project-workflow/assets/templates/task-brief.md",
    "adapters/opencode/agents/pm.md",
}
A_EXACT = {"README.md", "CONTRIBUTING.md", "SECURITY.md"} | SKILL_TEMPLATES
B_EXEMPT_PREFIX = "skills/writing-contract/"
OPTIONAL_TABLES = ("terms.md", "vague.md", "familiar-terms.md", "duplicate-allowlist.md")


class SpecError(Exception):
    """檢查器自身規格或輸入格式錯誤（退出碼 1）。"""


class IncompleteError(Exception):
    """環境或輸入缺失，無法完整檢查（退出碼 2）。"""


@dataclass
class Finding:
    file: str
    line: int
    rule: str
    check: str
    message: str
    state: str = "new"

    def key(self) -> tuple[str, int, str, str]:
        return (self.file, self.line, self.rule, self.message)

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "rule": self.rule,
            "check": self.check,
            "message": self.message,
            "state": self.state,
        }


@dataclass
class Doc:
    path: Path
    rel: str
    cls: str
    lines: list[str]
    added: set[int] | None = None  # None = 全文

    def in_scope(self, line: int) -> bool:
        return self.added is None or line in self.added


@dataclass
class Context:
    repo_root: Path
    notes_root: Path | None
    docs: list[Doc]
    findings: list[Finding] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)

    def emit(self, doc: Doc, line: int, rule: str, check: str, message: str, force: bool = False) -> None:
        if rule not in RULES:
            raise SpecError(f"未知的規則 id：{rule}")
        line = max(1, line)
        if not force and not doc.in_scope(line):
            return
        self.findings.append(Finding(doc.rel, line, rule, check, message))


# ---------------------------------------------------------------------------
# 讀檔與 Markdown 基本解析
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def frontmatter_end(lines: list[str]) -> int | None:
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index
    return None


def fence_mask(lines: list[str]) -> list[bool]:
    mask = [False] * len(lines)
    inside = False
    marker = ""
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not inside and (stripped.startswith("```") or stripped.startswith("~~~")):
            inside = True
            marker = stripped[:3]
            mask[index] = True
            continue
        if inside:
            mask[index] = True
            if stripped.startswith(marker):
                inside = False
    return mask


def opening_block(doc: Doc) -> tuple[int, list[tuple[int, str, str]]] | None:
    """在前 40 行內找包含四欄標籤的 blockquote。"""
    start = 0
    end = frontmatter_end(doc.lines)
    if end is not None:
        start = end + 1
    block: list[tuple[int, str, str]] = []
    for index in range(start, min(len(doc.lines), start + 40)):
        if not doc.lines[index].lstrip().startswith(">"):
            continue
        match = FIELD.match(doc.lines[index])
        if match:
            block.append((index + 1, match.group(1), match.group(2).strip()))
        elif block:
            break
    if not block:
        return None
    return block[0][0], block


def load_fillers(assets: Path) -> list[str]:
    path = assets / "fillers.md"
    if not path.is_file():
        raise IncompleteError("缺少輸入表：fillers.md")
    entries: list[str] = []
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if line.startswith("- "):
            item = line[2:].strip()
            if item:
                entries.append(item)
    if not entries:
        raise IncompleteError("輸入表為空：fillers.md")
    return entries


def optional_table_hint(assets: Path, name: str) -> str | None:
    path = assets / name
    if not path.is_file():
        return f"人工自檢清單缺少：{name}（不影響機械檢查）"
    entries = [line for line in read_text(path).splitlines() if line.strip().startswith("- ")]
    if not entries:
        return f"人工自檢清單為空：{name}（不影響機械檢查）"
    return None


def resolve_dirs(repo_root: Path, notes_root: Path | None) -> list[Path]:
    dirs = [repo_root, repo_root.parent]
    skills = repo_root / "skills"
    if skills.is_dir():
        dirs.extend(sorted(p for p in skills.iterdir() if p.is_dir()))
    if notes_root is not None:
        dirs.extend([notes_root, notes_root.parent])
    return dirs


def path_exists(token: str, repo_root: Path, notes_root: Path | None) -> bool | None:
    """回傳 True/False；None 表示 notes-root 缺席，未檢查。"""
    clean = token.strip().strip("`").rstrip(".,;:")
    clean = re.sub(r":\d+$", "", clean)  # G-e：剝掉路徑行號後綴
    if not clean:
        return True
    if clean.startswith(("/", "§")):
        return True  # F1 斜線指令、F2 章節描述
    if any(ch in clean for ch in "<>*{}$|\\") or ".." in clean or " " in clean:
        return True
    if clean.startswith("~") or "://" in clean or clean.startswith("mailto:"):
        return True
    if clean.startswith("actions/"):
        return True
    parts = [part for part in clean.split("/") if part]
    if not parts:
        return True
    if len(parts) == 1:
        return True  # F3 單段檔名／目錄名不查
    repo_dirs = [repo_root, repo_root.parent]
    skills = repo_root / "skills"
    if skills.is_dir():
        repo_dirs.extend(sorted(p for p in skills.iterdir() if p.is_dir()))
    if clean.startswith("project-notes/"):
        if notes_root is None:
            return None
        candidates = [d / clean for d in repo_dirs]
        candidates.append(notes_root / clean[len("project-notes/") :])
    else:
        # F6 notes-root 只解析 project-notes/ 前綴；其他 token 不拿 notes 當基準。
        candidates = [d / clean for d in repo_dirs]
    if any(c.exists() for c in candidates):
        return True
    # repo slug：兩段、最後一段沒有副檔名、第一段不是既有目錄。
    if len(parts) == 2 and "." not in parts[1] and not any(
        (d / parts[0]).is_dir() for d in repo_dirs
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# 8 條機械檢查
# ---------------------------------------------------------------------------


def check_a1_fields(ctx: Context, doc: Doc) -> tuple[int, list[tuple[int, str, str]]] | None:
    if doc.rel in SKILL_TEMPLATES:
        return None
    found = opening_block(doc)
    if found is None:
        ctx.emit(doc, 1, "A1", "1", "前 40 行內缺少四欄 blockquote（讀者／一句話／現在要做／依據）")
        return None
    line, block = found
    labels = {label for _, label, _ in block}
    for label in ("讀者", "一句話", "現在要做", "依據"):
        if label not in labels:
            ctx.emit(doc, line, "A1", "1", f"開頭四欄缺少「{label}」")
    return found


def check_a1_content(ctx: Context, doc: Doc, block: list[tuple[int, str, str]]) -> None:
    if doc.rel in SKILL_TEMPLATES:
        return
    for line, label, text in block:
        if not text.strip():
            ctx.emit(doc, line, "A1", "2", f"「{label}」欄沒有內容")
        elif re.search(r"<[^>\n]{1,40}>", text):
            ctx.emit(doc, line, "A1", "2", f"「{label}」欄仍留有 <…> 佔位符")


def check_a1_fillers(ctx: Context, doc: Doc, block: list[tuple[int, str, str]], fillers: list[str]) -> None:
    if doc.rel in SKILL_TEMPLATES:
        return
    entry = next((item for item in block if item[1] == "現在要做"), None)
    if entry is None:
        return
    line, _, text = entry
    for filler in sorted(set(fillers), key=len, reverse=True):
        if filler and filler in text:
            ctx.emit(doc, line, "A1", "3", f"現在要做欄含填充詞「{filler}」")
            return


def check_a1_basis(ctx: Context, doc: Doc, block: list[tuple[int, str, str]]) -> None:
    if doc.rel in SKILL_TEMPLATES:
        return
    entry = next((item for item in block if item[1] == "依據"), None)
    if entry is None:
        return
    line, _, text = entry
    for match in CODE_SPAN.finditer(text):
        token = match.group(1).strip()
        if not token:
            continue
        if "/" not in token and "." not in token and not (
            any((base / token).is_file() for base in resolve_dirs(ctx.repo_root, ctx.notes_root))
        ):
            continue
        exists = path_exists(token, ctx.repo_root, ctx.notes_root)
        if exists is None:
            ctx.not_checked.append(f"{doc.rel} 的依據路徑 {token}（notes-root 未提供）")
        elif exists is False:
            ctx.emit(doc, line, "A1", "4", f"依據欄引用的路徑不存在：{token}")


def check_a3_code_alone(ctx: Context, doc: Doc) -> None:
    mask = fence_mask(doc.lines)
    for index, raw in enumerate(doc.lines):
        if mask[index] or not raw.strip():
            continue
        for sentence in re.split(r"[。！？!?；;\n]", raw):
            leading = sentence.lstrip(" \t>*#-+|")
            match = CODE_TOKEN.match(leading)
            if match is None:
                continue
            if not STATUS_WORD.search(leading):
                continue
            rest = leading[match.end() :]
            rest = STATUS_WORD.sub("", rest)
            rest = re.sub(r"[\s，。、；：！？,.;:!?（）()\[\]「」『』`*_#>|\-—…]+", "", rest)
            if len(rest) < 2:
                ctx.emit(doc, index + 1, "A3", "5", "代號單獨承載資訊；同句要有不依賴代號的述詞")
                break


def check_a5_file_length(ctx: Context, doc: Doc) -> None:
    if len(doc.lines) > 400:
        # 檔案層級：修改檔也要報，因此不受 diff 新增行過濾。
        ctx.emit(doc, 401, "A5", "6", f"單檔 {len(doc.lines)} 行，超過 400 行上限；請拆檔或移附錄", force=True)


def check_a9c_sha(ctx: Context, doc: Doc) -> None:
    for index, raw in enumerate(doc.lines):
        stripped = raw.lstrip()
        if stripped.startswith(("```", "~~~")):
            continue
        # action pin 豁免兩種等價形態：`owner/repo@<40 hex>` 與
        # 表格列中同一 action 的 40 位 hex。裸的 40 位 hex 仍要報。
        text = ACTION_PIN.sub(lambda m: " " * len(m.group(0)), raw)
        if TABLE_ROW.match(raw):
            cells = [cell.strip().strip("`") for cell in raw.strip().strip("|").split("|")]
            if any(TABLE_CELL_REPO.fullmatch(cell) for cell in cells):
                for match in re.finditer(r"(?<![0-9A-Za-z])[0-9a-f]{40}(?![0-9A-Za-z])", raw):
                    start, end = match.start(), match.end()
                    text = text[:start] + " " * (end - start) + text[end:]
        for match in SHA_TOKEN.finditer(text):
            token = match.group(0)
            if token in SHA_ALLOW:
                continue
            if not (re.search(r"[0-9]", token) and re.search(r"[a-f]", token)):
                continue
            ctx.emit(doc, index + 1, "A9c", "7", f"公開文件出現 commit 樣式的 SHA：{token}；請改用相對描述")
            break


def check_b1(ctx: Context, doc: Doc) -> None:
    end = frontmatter_end(doc.lines)
    index = 0 if end is None else end + 1
    while index < len(doc.lines) and not doc.lines[index].strip():
        index += 1
    if index >= len(doc.lines) or not doc.lines[index].lstrip().startswith(B1_PREFIX):
        ctx.emit(doc, (index + 1) if index < len(doc.lines) else 1, "B1", "8", "第一段缺少優先序句（以程式碼為準，安全與授權以本檔為準）")


# ---------------------------------------------------------------------------
# Git 範圍
# ---------------------------------------------------------------------------


def git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=120,
        check=False,
    )


def git_ok(repo: Path, args: list[str]) -> bool:
    return git(repo, args).returncode == 0


def is_git_repo(repo: Path) -> bool:
    return git_ok(repo, ["rev-parse", "--git-dir"])


def event_base() -> tuple[str | None, str | None, bool]:
    """回傳 (base, error, event_claimed)。

    event_claimed=True 代表「有 CI 事件變數，但取不到可用 base」；呼叫端要
    fallback 並自曝。完全沒有事件變數時 event_claimed=False，維持安靜。
    """
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not name:
        return None, None, False
    if not path or not Path(path).is_file():
        return None, None, True
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"CI 事件 payload 無法讀取：{exc}", True
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return None, f"CI 事件 payload 不是合法 JSON：{exc}", True
    if not isinstance(payload, dict):
        return None, "CI 事件 payload 不是 JSON 物件，無法取得 base", True
    if name == "pull_request":
        pull = payload.get("pull_request")
        base_obj = pull.get("base") if isinstance(pull, dict) else None
        sha = base_obj.get("sha") if isinstance(base_obj, dict) else None
        if not isinstance(sha, str) or not sha:
            return None, "CI 事件 payload 缺少 pull_request.base.sha", True
    elif name == "push":
        sha = payload.get("before")
        if sha is None or sha == "":
            return EMPTY_TREE, None, False
        if not isinstance(sha, str):
            return None, "CI 事件 payload 的 before 必須是字串", True
    else:
        return None, None, False
    if re.fullmatch(r"0{40}", sha):
        return EMPTY_TREE, None, False
    return sha, None, False


def merge_base(repo: Path) -> str | None:
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        if not git_ok(repo, ["rev-parse", "--verify", "--quiet", ref]):
            continue
        result = git(repo, ["merge-base", "HEAD", ref])
        if result.returncode == 0:
            sha = result.stdout.decode("utf-8", "replace").strip()
            if sha:
                return sha
    return None


def object_type(repo: Path, ref: str) -> str | None:
    result = git(repo, ["cat-file", "-t", ref])
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def commit_base(repo: Path, ref: str, origin: str) -> tuple[str | None, str | None]:
    kind = object_type(repo, ref)
    if kind != "commit":
        return None, f"{origin} 必須是 commit 物件：{ref}（實際為 {kind or '不存在'}）"
    resolved = git(repo, ["rev-parse", ref]).stdout.decode("utf-8", "replace").strip()
    return (resolved or ref), None


def fallback_base(repo: Path) -> tuple[str | None, str | None]:
    found = merge_base(repo)
    if found:
        base, error = commit_base(repo, found, "merge-base")
        if not error:
            return base, None
    head_result = git(repo, ["rev-parse", "HEAD"])
    if head_result.returncode == 0:
        head = head_result.stdout.decode("utf-8", "replace").strip()
        if head and object_type(repo, head) == "commit":
            return head, None
    return EMPTY_TREE, None


def resolve_base(repo: Path, explicit: str | None) -> tuple[str | None, str | None, str | None]:
    if explicit is not None:
        if explicit == "":
            return None, "--base 不可為空字串", None
        if re.fullmatch(r"0{40}", explicit):
            return EMPTY_TREE, None, None
        if explicit == EMPTY_TREE:
            return EMPTY_TREE, None, None
        if not git_ok(repo, ["cat-file", "-e", explicit]):
            return None, f"--base 指向不存在的 ref／物件：{explicit}", None
        base, error = commit_base(repo, explicit, "--base")
        return base, error, None
    env, event_error, event_claimed = event_base()
    if event_error:
        return None, event_error, None
    if env:
        if env == EMPTY_TREE:
            return EMPTY_TREE, None, None
        kind = object_type(repo, env)
        if kind == "commit":
            base, error = commit_base(repo, env, "CI 事件提供的 base")
            return base, error, None
        if kind is not None:
            # 存在但不是 commit：環境異常，維持 fail-closed。
            return None, f"CI 事件提供的 base 必須是 commit 物件：{env}（實際為 {kind}）", None
        # CI 事件 base 不存在於這個 repo：這通常是測試用 fixture，不是環境異常。
        # fallback 到 merge-base 時範圍通常比事件 base 大；fallback 到 HEAD 時，
        # 乾淨工作樹的 diff 為空（只掃工作樹變更）；只有空 repo 才用空樹，
        # 全部檔案視為新增。不會把尚未檢查的內容誤報成已檢查。
        base, error = fallback_base(repo)
        if error:
            return None, error, None
        return base, None, f"CI 事件提供的 base 不存在於本 repo，已改用 {base}"
    if event_claimed:
        base, error = fallback_base(repo)
        if error:
            return None, error, None
        return base, None, f"CI 事件無法提供 base，已改用 {base}"
    base, error = fallback_base(repo)
    return base, error, None


def tracked_markdown(repo: Path, include_untracked: bool) -> list[str]:
    result = git(repo, ["-c", "core.quotepath=false", "ls-files", "-z", "--", "*.md"])
    paths = [item for item in result.stdout.decode("utf-8", "replace").split("\0") if item]
    if include_untracked:
        other = git(repo, ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard", "-z", "--", "*.md"])
        paths.extend(item for item in other.stdout.decode("utf-8", "replace").split("\0") if item)
    return sorted(set(paths))


def walk_markdown(repo: Path) -> list[str]:
    paths: list[str] = []
    for path in repo.rglob("*.md"):
        rel = path.relative_to(repo).as_posix()
        if rel.split("/")[0] in {".git", ".local", ".openchamber", ".tmp", ".scratch"}:
            continue
        paths.append(rel)
    return sorted(paths)


def parse_name_status(repo: Path, base: str) -> tuple[set[str], set[str], set[str]]:
    """回傳 (added, modified, deleted)。rename 的新路徑算 added。"""
    result = git(repo, ["-c", "core.quotepath=false", "diff", "--name-status", "-z", "-M", base, "--"])
    if result.returncode != 0:
        raise IncompleteError(f"無法讀取 git diff：{base}")
    parts = [item for item in result.stdout.decode("utf-8", "replace").split("\0") if item]
    added: set[str] = set()
    modified: set[str] = set()
    deleted: set[str] = set()
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(parts):
                break
            old, new = parts[index], parts[index + 1]
            index += 2
            added.add(new)
            deleted.add(old)
        elif status.startswith("A"):
            if index >= len(parts):
                break
            added.add(parts[index])
            index += 1
        elif status.startswith("M"):
            if index >= len(parts):
                break
            modified.add(parts[index])
            index += 1
        elif status.startswith("D"):
            if index >= len(parts):
                break
            deleted.add(parts[index])
            index += 1
        else:
            index += 1
    return added, modified, deleted


def added_lines(repo: Path, base: str, rel: str) -> set[int]:
    result = git(repo, ["diff", "-U0", base, "--", rel])
    if result.returncode != 0:
        raise IncompleteError(f"無法讀取 git diff：{base} -- {rel}")
    text = result.stdout.decode("utf-8", "replace")
    lines: set[int] = set()
    for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", text, re.M):
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        lines.update(range(start, start + count))
    return lines


# ---------------------------------------------------------------------------
# 類別與檔案載入
# ---------------------------------------------------------------------------


def classify(rel: str) -> str | None:
    if rel.startswith(B_EXEMPT_PREFIX):
        return None
    if rel == "AGENTS.md" or rel in B_EXTRA:
        return "b"
    if rel.startswith("skills/") and rel.endswith("/SKILL.md"):
        return "b"
    if rel.startswith("skills/") and "/references/" in rel:
        return "b"
    if rel.startswith("docs/") and rel.endswith(".md"):
        return "a"
    if rel in A_EXACT:
        return "a"
    return None


def load_docs(repo: Path, base: str | None, class_filter: str, ctx: Context) -> tuple[list[Doc], str]:
    if is_git_repo(repo):
        if base is None:
            candidates = tracked_markdown(repo, include_untracked=True)
            added: set[str] = set(candidates)
            modified: set[str] = set()
            scope = "全掃（未指定 --base）"
        else:
            added, modified, _ = parse_name_status(repo, base)
            all_untracked = set(tracked_markdown(repo, include_untracked=True))
            tracked = set(tracked_markdown(repo, include_untracked=False))
            added |= all_untracked - tracked
            candidates = sorted(added | modified)
            scope = f"diff 相對於 {base}（新增檔全文、修改檔僅新增行）"
    else:
        candidates = walk_markdown(repo)
        added = set(candidates)
        modified = set()
        scope = "全掃（非 Git 工作樹）"

    docs: list[Doc] = []
    skipped = 0
    for rel in candidates:
        cls = classify(rel)
        if cls is None:
            skipped += 1
            continue
        if class_filter != "all" and cls != class_filter:
            skipped += 1
            continue
        path = repo / rel
        if not path.is_file():
            continue
        try:
            text = read_text(path)
        except UnicodeDecodeError as exc:
            raise IncompleteError(f"檔案不是 UTF-8，無法檢查：{rel}（{exc}）") from exc
        lines = text.splitlines()
        if base is None or rel in added:
            line_set = None
        else:
            line_set = added_lines(repo, base, rel)
        docs.append(Doc(path=path, rel=rel, cls=cls, lines=lines, added=line_set))
    if skipped:
        scope += f"；{skipped} 份非 A/B 類檔案未檢查"
    return docs, scope


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_checks(ctx: Context, fillers: list[str]) -> None:
    for doc in ctx.docs:
        if doc.cls == "b":
            check_b1(ctx, doc)
            continue
        found = check_a1_fields(ctx, doc)
        if found is not None:
            _, block = found
            check_a1_content(ctx, doc, block)
            check_a1_fillers(ctx, doc, block, fillers)
            check_a1_basis(ctx, doc, block)
        check_a3_code_alone(ctx, doc)
        check_a5_file_length(ctx, doc)
        check_a9c_sha(ctx, doc)


def finding_state(current: list[Finding], baseline: list[Finding] | None, base_changed: bool) -> list[Finding]:
    if baseline is None:
        for item in current:
            item.state = "new"
        return current
    old = {item.key(): item for item in baseline}
    new_keys = {item.key() for item in current}
    result: list[Finding] = []
    for item in current:
        item.state = "persisting" if item.key() in old else "new"
        result.append(item)
    for item in baseline:
        if item.key() not in new_keys:
            item.state = "out-of-scope" if base_changed else "resolved"
            result.append(item)
    return result


def load_baseline(path: Path | None) -> tuple[list[Finding] | None, str | None]:
    if path is None:
        return None, None
    if not path.is_file():
        raise FileNotFoundError(f"--baseline 檔案不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpecError(f"--baseline 不是合法 JSON：{exc}") from exc
    baseline_base = payload.get("base") if isinstance(payload, dict) else None
    raw = payload.get("findings") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise SpecError("--baseline 格式錯誤：找不到 findings 陣列")
    findings: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SpecError("--baseline 格式錯誤：findings 內含非物件項目")
        for key in ("file", "line", "rule", "message"):
            if key not in item:
                raise SpecError(f"--baseline 格式錯誤：finding 缺少 {key}")
        if not isinstance(item["file"], str) or not isinstance(item["rule"], str) or not isinstance(item["message"], str):
            raise SpecError("--baseline 格式錯誤：file／rule／message 必須是字串")
        if not isinstance(item["line"], int) or isinstance(item["line"], bool):
            raise SpecError("--baseline 格式錯誤：line 必須是整數")
        findings.append(
            Finding(
                file=item["file"],
                line=item["line"],
                rule=item["rule"],
                check=str(item.get("check", "")),
                message=item["message"],
            )
        )
    return findings, str(baseline_base) if baseline_base is not None else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="檢查 my-agent-skills 的 8 條寫作契約機械規則；所有文件問題都是 warning，不阻擋。",
    )
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent, help="repo 根目錄")
    parser.add_argument("--notes-root", type=Path, default=None, help="private notes repo 根目錄（只用於路徑解析；不讀內容）")
    parser.add_argument(
        "--base",
        default=None,
        help="diff 基準，必須是 commit 物件（空字串視為格式錯誤；40 位全 0 視為空樹）；缺席時依 CI 事件、merge-base、HEAD 決定",
    )
    parser.add_argument("--baseline", type=Path, default=None, help="前一次輸出的 JSON，用來標示 new／persisting／resolved／out-of-scope")
    parser.add_argument("--class", dest="class_filter", choices=("a", "b", "all"), default="all", help="只檢查某一類（預設 all）")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="輸出格式（預設 text）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    notes_root = args.notes_root.resolve() if args.notes_root else None
    script_dir = Path(__file__).resolve().parent
    assets = script_dir.parent / "skills" / "writing-contract" / "assets"

    if not repo.is_dir():
        return emit_incomplete(f"repo 路徑不存在：{repo}", args.format, repo)
    if notes_root is not None and not notes_root.is_dir():
        return emit_incomplete(f"--notes-root 路徑不存在：{notes_root}", args.format, repo)
    try:
        fillers = load_fillers(assets)
    except IncompleteError as exc:
        return emit_incomplete(str(exc), args.format, repo)

    try:
        baseline, baseline_base = load_baseline(args.baseline)
    except FileNotFoundError as exc:
        return emit_incomplete(str(exc), args.format, repo)
    except SpecError as exc:
        return emit_spec_error(str(exc), args.format, repo)

    base: str | None = None
    base_note: str | None = None
    scope = ""
    if is_git_repo(repo):
        base, error, base_note = resolve_base(repo, args.base)
        if error:
            return emit_incomplete(error, args.format, repo, base=args.base)
    elif args.base is not None:
        return emit_incomplete(f"repo 不是 Git 工作樹，無法使用 --base：{args.base}", args.format, repo)

    ctx = Context(repo_root=repo, notes_root=notes_root, docs=[])
    for name in OPTIONAL_TABLES:
        hint = optional_table_hint(assets, name)
        if hint:
            ctx.not_checked.append(hint)
    try:
        ctx.docs, scope = load_docs(repo, base, args.class_filter, ctx)
        run_checks(ctx, fillers)
    except IncompleteError as exc:
        return emit_incomplete(str(exc), args.format, repo)
    except SpecError as exc:
        return emit_spec_error(str(exc), args.format, repo)

    base_changed = baseline is not None and baseline_base != base
    findings = finding_state(ctx.findings, baseline, base_changed)
    findings.sort(key=lambda item: (item.file, item.line, item.rule, item.message))
    counts = {
        state: sum(1 for item in findings if item.state == state)
        for state in ("new", "persisting", "resolved", "out-of-scope")
    }
    not_checked = list(dict.fromkeys(ctx.not_checked))

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "status": "passed",
        "repo": repo.name,
        "base": base,
        "baseNote": base_note,
        "baselineBase": baseline_base,
        "baseChanged": base_changed,
        "scope": scope,
        "class": args.class_filter,
        "notChecked": not_checked,
        "counts": counts,
        "findings": [item.as_dict() for item in findings],
        "exitCode": 0,
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        suffix = "；".join(not_checked)
        print(f"本輪 {len(findings)} 個形式問題（不阻擋）／未檢查：{scope}" + (f"；{suffix}" if suffix else ""))
        if base_note:
            print(f"注意：{base_note}")
        for item in findings:
            print(f"- {item.file}:{item.line} [{item.rule}／檢查 {item.check}] {item.message}（{item.state}）")
        if baseline is not None:
            print(
                f"收斂狀態：new {counts['new']}、persisting {counts['persisting']}、"
                f"resolved {counts['resolved']}、out-of-scope {counts['out-of-scope']}"
            )
            if counts["out-of-scope"]:
                print(
                    f"範圍已改變（baseline base={baseline_base}、本次 base={base}）；"
                    "out-of-scope 不計入收斂，不宣稱收斂。"
                )
            else:
                print("沒有 new 且沒有 out-of-scope 即視為收斂。")
    return 0


def emit_incomplete(reason: str, fmt: str, repo: Path, base: str | None = None) -> int:
    if fmt == "json":
        print(json.dumps({
            "schemaVersion": SCHEMA_VERSION,
            "tool": TOOL,
            "status": "incomplete",
            "repo": repo.name,
            "base": base,
            "incompleteReason": reason,
            "findings": [],
            "exitCode": 2,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"未完整檢查：{reason}")
    return 2


def emit_spec_error(reason: str, fmt: str, repo: Path) -> int:
    if fmt == "json":
        print(json.dumps({
            "schemaVersion": SCHEMA_VERSION,
            "tool": TOOL,
            "status": "spec-error",
            "repo": repo.name,
            "error": reason,
            "findings": [],
            "exitCode": 1,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"檢查器規格錯誤：{reason}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
