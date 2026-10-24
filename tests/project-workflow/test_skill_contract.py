"""skills/project-workflow 的契約測試。

只用 Python 3 標準函式庫；本檔部分測試會唯讀 repo 內的真實檔案
（`task-brief.md`、`pm.md`、`SKILL.md`、`workflow-details.md`）；
「不碰真實 repo」指**不做破壞性 fixture、不修改 repo 內檔案**，
也不使用真實帳號、真實 email 或個人本機絕對路徑。

測試守住幾件事：

1. 六份模板都存在，且各自具備關鍵欄位與填寫規則。
2. `SKILL.md` 的相對引用自足（都在 skill 目錄內且存在）。
3. 把整個 skill 目錄隔離複製後，格式驗證仍然通過；少一個被引用的檔案就會失敗。
4. 沒有平台工具硬依賴、沒有模型／provider 綁定。
5. 沒有真實個資或個人絕對路徑，placeholder 符合慣例。
6. 安全敘述存在：載入不等於授權，commit／push／hooks 等需要明確授權。
7. 派工簡報的節名、順序與類型列舉在 `task-brief.md`、`pm.md`、`SKILL.md`、
   `workflow-details.md` 四處同源；權威一律即時從 `task-brief.md` 抽取。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "project-workflow"
VALIDATOR = REPO_ROOT / "scripts" / "validate_skills.py"
TEMPLATES_DIR = SKILL_DIR / "assets" / "templates"
TIMEOUT = 120

# 模板 -> 必須出現的關鍵欄位／字樣（皆為本 skill 自己定義的契約）。
EXPECTED_TEMPLATE_FIELDS = {
    "overview.md": (
        "專案 ID",
        "一句話說明",
        "code-root",
        "notes-root",
        "目前階段",
        "目標",
        "範圍",
        "non-goals",
        "關鍵里程碑",
        "主要風險",
        "更新紀錄",
    ),
    "progress.md": (
        "最新摘要",
        "當前目標",
        "程式碼目錄",
        "code repo 分支",
        "code repo commit",
        "是否已 push",
        "remote 可達性",
        "code repo 同步狀態",
        "notes repo 同步狀態",
        "已確認",
        "假設",
        "未知",
        "已完成",
        "待完成",
        "驗收（已測）",
        "驗收（未測）",
        "待決定事項",
        "下一步可行動任務",
    ),
    "plan.md": (
        "目標",
        "背景",
        "已知",
        "假設",
        "未知",
        "里程碑",
        "工作分解",
        "依賴",
        "驗收條件",
        "風險與緩解",
        "授權需求",
        "non-goals",
    ),
    "task-brief.md": (
        "任務類型",
        "目標",
        "code-root",
        "notes-root",
        "路徑基準",
        "必讀背景",
        "已知",
        "假設",
        "未知",
        "範圍",
        "non-goals",
        "輸入輸出",
        "錯誤處理",
        "驗收方法",
        "預期成果",
        "限制",
        "停止條件",
        "允許寫入",
        "worker 禁止事項",
        "權限說明",
    ),
    "research.md": (
        "研究問題",
        "已知",
        "假設",
        "未知",
        "方法",
        "小樣本階段",
        "報告 shape",
        "來源清單",
        "事實",
        "推論",
        "未確認",
        "收斂結論",
        "下一步",
    ),
    "acceptance.md": (
        "驗收條件",
        "驗證方法",
        "實際輸出摘要",
        "未測項目",
        "結論",
        "後續修正",
        "授權與同步狀態",
    ),
}

# 與 scripts/validate_skills.py 相同的相對引用抽取方式（測試獨立實作，避免耦合）。
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

# 模型／provider／平台工具名稱：不得作為本 skill 的硬綁定。
MODEL_ID = re.compile(r"(?i)\b(?:gpt|claude|gemini|deepseek|llama|qwen|mistral|grok)[-_ ]?\d")
PROVIDER_PREFIX = re.compile(r"(?i)\b(?:openrouter|anthropic|openai|google|meta)/")
FORBIDDEN_TOOL_BINDINGS = (
    "tools.openchamber",
    "openchamber_web",
    "openchamber_notify",
    "mcp__",
    "read_mcp_resource",
    "session.create",
    "session.send",
)

HOME_PATH = re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\Users\\)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.invalid")

# 安全敘述：載入不等於授權；不可逆動作需要明確授權。
REQUIRED_SKILL_MARKERS = ("不等於授權", "明確授權", "不會", "絕不")

# 派工簡報契約：四個檔案都以 repo 根為基準解析（保持相對路徑結構，反例才好複製）。
ADAPTERS_AGENTS_DIR = REPO_ROOT / "adapters" / "opencode" / "agents"
BRIEF_RELATIVE_PATHS = {
    "template": Path("skills/project-workflow/assets/templates/task-brief.md"),
    "pm": Path("adapters/opencode/agents/pm.md"),
    "skill": Path("skills/project-workflow/SKILL.md"),
    "details": Path("skills/project-workflow/references/workflow-details.md"),
}
OLD_TYPE_ENUM = "原型／正式實作／研究"
OLD_FOUR_TYPE_ENUM = re.compile(r"實作／研究／文件／驗收(?!／原型)")
TYPE_LINE = re.compile(r"類型：<(?P<enum>[^>]+)>")
SKILL_LIST_ITEM = re.compile(r"^(?P<number>\d+)\. \*\*(?P<name>[^*]+)\*\*")
SKILL_BRIEF_HEADING = "## 任務簡報（固定模板）"
DETAILS_CHECKLIST_HEADING = "## 5. 任務簡報檢查清單"
CHECKLIST_PREFIX = "- [ ] "
KEYWORD_SEPARATORS = re.compile(r"[\s／、與]+")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def skill_text_files() -> dict[Path, str]:
    """回傳 skill 目錄內所有 Markdown 檔（SKILL.md、references、templates）。"""
    files: dict[Path, str] = {}
    for path in sorted(SKILL_DIR.rglob("*.md")):
        files[path] = read_text(path)
    return files


def skill_body() -> str:
    text = read_text(SKILL_DIR / "SKILL.md")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return "\n".join(lines[index + 1 :])
    raise AssertionError("SKILL.md 缺少可解析的 frontmatter 界線")


def iter_references(body: str):
    """依序產生 (token, strict)，strict 代表明確的資源引用。"""
    for match in MARKDOWN_LINK.finditer(body):
        target = match.group("target")
        if target:
            yield target, True
    for match in CODE_SPAN.finditer(body):
        for token in PATH_TOKEN.finditer(match.group("code")):
            yield token.group("token"), True
    for match in PATH_TOKEN.finditer(body):
        yield match.group("token"), False


def looks_like_path(token: str, strict: bool) -> bool:
    if not token:
        return False
    if token.endswith("/"):
        return True
    if strict:
        return True
    return token.lower().endswith(KNOWN_EXTENSIONS)


# ---------------------------------------------------------------------------
# 派工簡報契約的抽取與比較。
#
# 所有 helper 都接受 `root`（repo 根），比對對象是 root 底下的真實檔案；
# 反例測試把檔案以相同相對結構複製到暫存目錄後，用同一組 helper 比對。
# 權威基準（10 節名稱與類型列舉）一律即時從 `task-brief.md` 抽取，不寫死常數。
# ---------------------------------------------------------------------------


def contract_path(root: Path, key: str) -> Path:
    return root / BRIEF_RELATIVE_PATHS[key]


def contract_text(root: Path, key: str) -> str:
    return read_text(contract_path(root, key))


def fenced_blocks(text: str) -> list[tuple[str, list[str]]]:
    """回傳 (info string, 區塊內原始行)；原始行不 strip，保留縮排。"""
    blocks: list[tuple[str, list[str]]] = []
    info: str | None = None
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            if info is None:
                info = stripped[3:].strip()
                lines = []
            else:
                blocks.append((info, lines))
                info = None
            continue
        if info is not None:
            lines.append(raw)
    if info is not None:
        raise AssertionError("fenced code block 沒有關閉，無法抽取派工簡報模板")
    return blocks


def brief_blocks(root: Path, key: str) -> list[list[str]]:
    """回傳含 `## 1. ` 標題的 fenced block（原始行）；正常情況恰好 1 個。"""
    return [
        lines
        for _, lines in fenced_blocks(contract_text(root, key))
        if any(line.strip().startswith("## 1. ") for line in lines)
    ]


def single_brief_block(root: Path, key: str) -> list[str]:
    blocks = brief_blocks(root, key)
    if len(blocks) != 1:
        raise AssertionError(
            f"{key}: 含 `## 1. ` 的 fenced block 應恰好 1 個，實際找到 {len(blocks)} 個"
        )
    return blocks[0]


def headings_from_lines(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip().startswith("## ")]


def heading_names(root: Path) -> list[str]:
    """權威 10 節的名稱（去掉 `## N. ` 前綴），即時從 `task-brief.md` 抽取。"""
    headings = headings_from_lines(single_brief_block(root, "template"))
    return [re.sub(r"^##\s+\d+\.\s*", "", heading) for heading in headings]


def section_lines(text: str, heading: str) -> list[str]:
    """取 Markdown 某個 `## ` 段（到下一個 `## ` 為止）的原始行。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == heading:
            section: list[str] = []
            for candidate in lines[index + 1 :]:
                if candidate.strip().startswith("## "):
                    break
                section.append(candidate)
            return section
    raise AssertionError(f"找不到段落：{heading}")


def skill_brief_names(root: Path) -> list[str]:
    names: list[str] = []
    section = section_lines(contract_text(root, "skill"), SKILL_BRIEF_HEADING)
    for line in section:
        match = SKILL_LIST_ITEM.match(line.strip())
        if match:
            names.append(match.group("name"))
    return names


def details_checklist_items(root: Path) -> list[str]:
    section = section_lines(contract_text(root, "details"), DETAILS_CHECKLIST_HEADING)
    return [
        line.strip()[len(CHECKLIST_PREFIX) :]
        for line in section
        if line.strip().startswith(CHECKLIST_PREFIX)
    ]


def heading_keywords(name: str) -> list[str]:
    """節名拆成關鍵詞：以空白、`／`、`、`、`與` 切開，長度 < 2 的片段忽略。"""
    return [part for part in KEYWORD_SEPARATORS.split(name) if len(part) >= 2]


def type_enumeration(root: Path) -> str:
    """權威類型列舉，即時從 `task-brief.md` §1 的「類型：<...>」抽取。"""
    matches = TYPE_LINE.findall(contract_text(root, "template"))
    if len(matches) != 1:
        raise AssertionError(
            f"task-brief.md 應恰好有 1 個「類型：<...>」，實際找到 {len(matches)} 個：{matches!r}"
        )
    return matches[0]


def compare_pm_headings(root: Path) -> str:
    """T1：`task-brief.md` 骨架與 `pm.md` 內嵌模板的節名序列差異。"""
    template = headings_from_lines(single_brief_block(root, "template"))
    pm = headings_from_lines(single_brief_block(root, "pm"))
    if template == pm:
        return ""
    return f"task-brief 骨架節名序列：{template!r}\npm.md 內嵌模板節名序列：{pm!r}"


def compare_skill_names(root: Path) -> str:
    """T2：`SKILL.md`〈任務簡報（固定模板）〉的項名序列差異。"""
    expected = heading_names(root)
    actual = skill_brief_names(root)
    if expected == actual:
        return ""
    return f"task-brief 節名序列：{expected!r}\nSKILL.md 清單名稱序列：{actual!r}"


def compare_details_checklist(root: Path) -> str:
    """T3：`workflow-details.md` 檢查清單的項目數與關鍵詞差異。"""
    headings = heading_names(root)
    items = details_checklist_items(root)
    problems: list[str] = []
    if len(items) != len(headings):
        problems.append(
            f"項目數 {len(items)} != 節數 {len(headings)}；項目序列：{items!r}"
        )
    for index, (heading, item) in enumerate(zip(headings, items), start=1):
        missing = [keyword for keyword in heading_keywords(heading) if keyword not in item]
        if missing:
            problems.append(
                f"第 {index} 項缺少關鍵詞 {missing!r}（節名 {heading!r}；項目 {item!r}）"
            )
    return "\n".join(problems)


def compare_type_enumeration(root: Path) -> str:
    """T4：類型列舉在三處逐字相同，且舊列舉在四個檔案都不得出現。"""
    enum = type_enumeration(root)
    problems: list[str] = []
    for key in ("template", "pm", "skill"):
        if enum not in contract_text(root, key):
            problems.append(f"{key} 缺少類型列舉 {enum!r}")
    for key in ("template", "pm", "skill", "details"):
        text = contract_text(root, key)
        if OLD_TYPE_ENUM in text:
            problems.append(f"{key} 仍出現舊列舉 {OLD_TYPE_ENUM!r}")
        match = OLD_FOUR_TYPE_ENUM.search(text)
        if match:
            problems.append(
                f"{key} 出現舊四項列舉（新五項的前綴子字串）{match.group(0)!r}"
            )
    return "\n".join(problems)


def compare_pm_indentation(root: Path) -> str:
    """T9：`pm.md` 模板 block（含圍欄行）每個非空行都要以兩個空格開頭。"""
    lines = contract_text(root, "pm").splitlines()
    start: int | None = None
    end: int | None = None
    inside = False
    block: list[str] = []
    for index, raw in enumerate(lines):
        if raw.strip().startswith("```"):
            if not inside:
                inside = True
                block_start = index
                block = []
            else:
                inside = False
                if any(line.strip().startswith("## 1. ") for line in block):
                    start, end = block_start, index
                block = []
            continue
        if inside:
            block.append(raw)
    if start is None or end is None:
        raise AssertionError("pm.md 找不到含 `## 1. ` 的 fenced block")
    bad = [
        f"第 {index} 行：{line!r}"
        for index, line in enumerate(lines[start : end + 1], start=start + 1)
        if line.strip() and not line.startswith("  ")
    ]
    if not bad:
        return ""
    return "pm.md 模板 block 縮排不符（每個非空行須以兩個空格開頭）：\n" + "\n".join(bad)


def copy_contract_tree(root: Path) -> None:
    """把契約檔案以相同相對結構複製到 root，供反例測試突變。"""
    shutil.copytree(SKILL_DIR, root / "skills" / "project-workflow")
    shutil.copytree(ADAPTERS_AGENTS_DIR, root / "adapters" / "opencode" / "agents")


def replace_once(path: Path, old: str, new: str) -> None:
    text = read_text(path)
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"{path} 內 {old!r} 出現 {count} 次，反例突變需要唯一位置")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def swap_lines(path: Path, first: str, second: str) -> None:
    lines = read_text(path).splitlines()
    try:
        i = lines.index(first)
        j = lines.index(second)
    except ValueError as exc:
        raise AssertionError(f"{path} 找不到要交換的行：{exc}") from exc
    lines[i], lines[j] = lines[j], lines[i]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TemplateContractTests(unittest.TestCase):
    def test_all_expected_templates_exist(self) -> None:
        self.assertEqual(
            set(EXPECTED_TEMPLATE_FIELDS),
            {path.name for path in TEMPLATES_DIR.glob("*.md")},
        )

    def test_each_template_has_key_fields_and_fill_rules(self) -> None:
        for name, fields in EXPECTED_TEMPLATE_FIELDS.items():
            with self.subTest(template=name):
                text = read_text(TEMPLATES_DIR / name)
                self.assertIn("填寫規則", text, f"{name} 缺少填寫規則")
                self.assertIn("<", text, f"{name} 缺少 placeholder")
                for field in fields:
                    self.assertIn(field, text, f"{name} 缺少欄位：{field}")

    def test_templates_are_self_contained(self) -> None:
        """模板只描述欄位，不應連到 skill 之外的檔案。"""
        for name in EXPECTED_TEMPLATE_FIELDS:
            text = read_text(TEMPLATES_DIR / name)
            self.assertNotIn("](http", text)
            self.assertNotIn("](../", text)


class SelfContainedReferenceTests(unittest.TestCase):
    def test_skill_references_resolve_inside_skill_dir(self) -> None:
        body = skill_body()
        checked = 0
        for token, strict in iter_references(body):
            if "://" in token or token.startswith("mailto:"):
                continue
            if not looks_like_path(token, strict):
                continue
            clean = token.strip().rstrip(".,;:")
            clean = clean.split("#", 1)[0].split("?", 1)[0]
            if not clean:
                continue
            self.assertFalse(clean.startswith("/"), f"不得使用絕對路徑：{clean}")
            target = clean[2:] if clean.startswith("./") else clean
            resolved = (SKILL_DIR / target).resolve()
            try:
                resolved.relative_to(SKILL_DIR.resolve())
            except ValueError:
                self.fail(f"引用逃出 skill 目錄：{clean}")
            self.assertTrue(resolved.exists(), f"找不到引用的資源：{clean}")
            checked += 1
        self.assertGreater(checked, 0, "沒有檢查到任何相對引用，測試失去意義")

    def test_all_references_and_templates_are_referenced(self) -> None:
        body = skill_body()
        for path in sorted(SKILL_DIR.glob("references/*.md")):
            self.assertIn(path.name, body, f"SKILL.md 未指向 {path.name}")
        for path in sorted(TEMPLATES_DIR.glob("*.md")):
            self.assertIn(path.name, body, f"SKILL.md 未指向模板 {path.name}")


class IsolatedCopyTests(unittest.TestCase):
    def _copy_skill(self, root: Path) -> Path:
        dest = root / "skills" / "project-workflow"
        shutil.copytree(SKILL_DIR, dest)
        return dest

    def _validate(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(VALIDATOR), "--repo", str(root), "--format", "json"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def test_isolated_copy_passes_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self._copy_skill(root)
            for rel in (
                "SKILL.md",
                "references/workflow-details.md",
                "references/privacy-and-sync.md",
                "references/client-compatibility.md",
                "assets/templates/overview.md",
                "assets/templates/progress.md",
                "assets/templates/plan.md",
                "assets/templates/task-brief.md",
                "assets/templates/research.md",
                "assets/templates/acceptance.md",
            ):
                self.assertTrue((dest / rel).is_file(), f"複製後缺少 {rel}")
            result = self._validate(root)
            payload = json.loads(result.stdout)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual([], payload["errors"], payload["errors"])
            self.assertEqual(1, payload["skillsChecked"])

    def test_copy_contains_all_references_and_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self._copy_skill(root)
            for sub in ("references", "assets/templates"):
                src = sorted(p.name for p in (SKILL_DIR / sub).glob("*.md"))
                dst = sorted(p.name for p in (dest / sub).glob("*.md"))
                self.assertEqual(src, dst, f"{sub} 複製不完整")
                self.assertGreater(len(src), 0, f"{sub} 不應為空")

    def test_isolated_copy_missing_resource_fails(self) -> None:
        """拿掉一個被 SKILL.md 引用的檔案後，隔離複製必須驗證失敗。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = self._copy_skill(root)
            (dest / "references" / "workflow-details.md").unlink()
            result = self._validate(root)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)


class PlatformNeutralityTests(unittest.TestCase):
    def test_frontmatter_has_no_platform_extensions(self) -> None:
        text = read_text(SKILL_DIR / "SKILL.md")
        frontmatter = text.split("---", 2)[1]
        self.assertNotIn("allowed-tools", frontmatter)
        self.assertNotIn("context:", frontmatter)

    def test_no_model_or_provider_binding(self) -> None:
        for path, text in skill_text_files().items():
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                self.assertIsNone(MODEL_ID.search(text), f"{path} 出現模型綁定")
                self.assertIsNone(PROVIDER_PREFIX.search(text), f"{path} 出現 provider 綁定")

    def test_no_platform_tool_binding(self) -> None:
        for path, text in skill_text_files().items():
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                for needle in FORBIDDEN_TOOL_BINDINGS:
                    self.assertNotIn(needle, text, f"{path} 綁定了平台工具：{needle}")


class PrivacyPlaceholderTests(unittest.TestCase):
    def test_no_personal_absolute_paths(self) -> None:
        for path, text in skill_text_files().items():
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                self.assertIsNone(HOME_PATH.search(text), f"{path} 出現個人絕對路徑")

    def test_emails_use_placeholder_domains_only(self) -> None:
        for path, text in skill_text_files().items():
            for email in EMAIL.findall(text):
                with self.subTest(path=str(path.relative_to(REPO_ROOT)), email=email):
                    self.assertTrue(
                        email.endswith(ALLOWED_EMAIL_DOMAINS),
                        f"{path} 出現非 placeholder email：{email}",
                    )

    def test_no_specific_remote_repo_links(self) -> None:
        for path, text in skill_text_files().items():
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                self.assertNotIn("github.com", text, f"{path} 出現具體遠端 repo 連結")


class SecurityAuthorizationTests(unittest.TestCase):
    def test_skill_states_loading_is_not_authorization(self) -> None:
        text = read_text(SKILL_DIR / "SKILL.md")
        for marker in REQUIRED_SKILL_MARKERS:
            self.assertIn(marker, text, f"SKILL.md 缺少安全敘述：{marker}")

    def test_skill_lists_actions_requiring_explicit_authorization(self) -> None:
        text = read_text(SKILL_DIR / "SKILL.md")
        for action in ("commit", "push", "hooks", "可見度", ".local", ".scratch"):
            self.assertIn(action, text, f"SKILL.md 未涵蓋需授權的動作：{action}")

    def test_privacy_reference_repeats_authorization_boundary(self) -> None:
        text = read_text(SKILL_DIR / "references" / "privacy-and-sync.md")
        self.assertIn("明確授權", text)
        self.assertIn("不會", text)


class ConsistencyAndAuthorizationTests(unittest.TestCase):
    """修正後的一致性契約：寫入 vs 同步、public vs private、遷移、client 來源。"""

    def test_write_and_sync_are_distinguished(self) -> None:
        text = read_text(SKILL_DIR / "SKILL.md")
        self.assertIn("寫入與同步是兩件事", text)
        self.assertIn("本任務自己授權範圍內產生的變更可以繼續", text)
        self.assertIn("同步或上傳之前", text)

    def test_private_commit_refs_allowed_but_public_embeds_forbidden(self) -> None:
        text = read_text(SKILL_DIR / "references" / "privacy-and-sync.md")
        self.assertIn("任何 repo（public 或 private）都不得出現", text)
        self.assertIn("只有公開內容額外禁止", text)
        self.assertIn("必要的 commit 參照", text)

    def test_migration_requires_itemized_verification(self) -> None:
        for path in (SKILL_DIR / "SKILL.md", SKILL_DIR / "references" / "privacy-and-sync.md"):
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                self.assertIn("未逐項核對並驗收前，不得聲稱已完成", read_text(path))

    def test_client_doc_cites_official_sources_and_layers_runtime(self) -> None:
        text = read_text(SKILL_DIR / "references" / "client-compatibility.md")
        for source in (
            "agentskills.io/specification",
            "opencode.ai/v2/docs/skills",
            "pi.dev/docs/latest/skills",
            "deepseek-harness",
        ):
            self.assertIn(source, text, f"缺少官方來源：{source}")
        self.assertIn("runtime 未實測", text)
        self.assertIn("不是規格上限", text)

    def test_description_within_client_cap_and_spec_max(self) -> None:
        text = read_text(SKILL_DIR / "SKILL.md")
        frontmatter = text.split("---", 2)[1]
        match = re.search(r"^description:\s*(.*)$", frontmatter, re.M)
        self.assertIsNotNone(match, "找不到 description")
        length = len(match.group(1))
        self.assertLessEqual(length, 500, "description 超過 client catalog 的 500 字元預設上限")
        self.assertLessEqual(length, 1024, "description 超過規格上限 1024 字元")

    def test_progress_template_keeps_sync_and_summary_fields(self) -> None:
        text = read_text(TEMPLATES_DIR / "progress.md")
        for field in (
            "最新摘要",
            "當前目標",
            "程式碼目錄",
            "code repo 分支",
            "code repo commit",
            "code repo 同步狀態",
            "notes repo 同步狀態",
            "驗收（已測）",
            "驗收（未測）",
        ):
            self.assertIn(field, text, f"progress 模板缺少欄位：{field}")

    def test_public_code_repo_includes_code_and_tests(self) -> None:
        text = read_text(SKILL_DIR / "SKILL.md")
        self.assertIn("程式碼、測試、README 與對外", text)
        self.assertNotIn("只放 README 與 docs", text)

    def test_one_code_subdir_per_project_not_per_subtask(self) -> None:
        text = read_text(SKILL_DIR / "SKILL.md")
        self.assertIn("一個**專案**對應一個 code 子目錄", text)
        self.assertIn("不是每個派工子任務都新建 repo", text)

    def test_pi_search_wording_matches_official(self) -> None:
        text = read_text(SKILL_DIR / "references" / "client-compatibility.md")
        self.assertIn("從當前工作目錄往父層搜尋直到 Git 根", text)
        self.assertNotIn("由根往 repo 上溯", text)


class BriefTemplateConsistencyTests(unittest.TestCase):
    """派工簡報四份清單同源；權威一律即時從 `task-brief.md` 抽取。"""

    def test_t1_brief_section_names_match_pm_template(self) -> None:
        for key in ("template", "pm"):
            blocks = brief_blocks(REPO_ROOT, key)
            self.assertEqual(
                1,
                len(blocks),
                f"{key} 內含 `## 1. ` 的 fenced block 應恰好 1 個，實際找到 {len(blocks)} 個",
            )
        diff = compare_pm_headings(REPO_ROOT)
        self.assertEqual("", diff, f"節名序列不一致：\n{diff}")

    def test_t2_skill_list_names_match_brief(self) -> None:
        diff = compare_skill_names(REPO_ROOT)
        self.assertEqual("", diff, f"SKILL.md 項名序列與權威骨架不一致：\n{diff}")

    def test_t3_workflow_details_checklist_matches_brief(self) -> None:
        diff = compare_details_checklist(REPO_ROOT)
        self.assertEqual("", diff, f"檢查清單與權威骨架不一致：\n{diff}")

    def test_t4_type_enumeration_matches_in_three_files(self) -> None:
        diff = compare_type_enumeration(REPO_ROOT)
        self.assertEqual("", diff, f"類型列舉不一致：\n{diff}")

    def test_t9_pm_template_block_keeps_two_space_indentation(self) -> None:
        diff = compare_pm_indentation(REPO_ROOT)
        self.assertEqual("", diff, f"pm.md 模板縮排結構被破壞：\n{diff}")


class BriefTemplateAntiRegressionTests(unittest.TestCase):
    """反例：把暫存副本改壞，證明 T1–T4 的比較函式真的抓得到漂移。"""

    def _copy(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        copy_contract_tree(root)
        return tmp, root

    def test_t5_mutated_pm_section_name_is_detected(self) -> None:
        tmp, root = self._copy()
        with tmp:
            clean = compare_pm_headings(root)
            self.assertEqual("", clean, f"乾淨副本應無差異，實際：\n{clean}")
            replace_once(
                contract_path(root, "pm"),
                "  ## 2. repo 根與路徑基準",
                "  ## 2. 專案目錄與路徑基準",
            )
            diff = compare_pm_headings(root)
            self.assertNotEqual("", diff, "改掉 pm.md 一個節名後，T1 應報差異")
            print(f"[T5] 改掉 pm.md 一個節名後，T1 差異：\n{diff}")

    def test_t5b_swapped_pm_section_order_is_detected(self) -> None:
        tmp, root = self._copy()
        with tmp:
            self.assertEqual("", compare_pm_headings(root), "乾淨副本應無差異")
            swap_lines(
                contract_path(root, "pm"),
                "  ## 3. 必讀背景",
                "  ## 4. 已知／假設／未知",
            )
            diff = compare_pm_headings(root)
            self.assertNotEqual("", diff, "交換 pm.md 兩個相鄰節名後，T1 應報差異")
            print(f"[T5b] 交換 pm.md 相鄰節名後，T1 差異：\n{diff}")

    def test_t6_mutated_skill_item_name_is_detected(self) -> None:
        tmp, root = self._copy()
        with tmp:
            self.assertEqual("", compare_skill_names(root), "乾淨副本應無差異")
            replace_once(
                contract_path(root, "skill"),
                "1. **任務類型與目標**",
                "1. **任務種類與目標**",
            )
            diff = compare_skill_names(root)
            self.assertNotEqual("", diff, "改掉 SKILL.md 一個項名後，T2 應報差異")
            print(f"[T6] 改掉 SKILL.md 一個項名後，T2 差異：\n{diff}")

    def test_t7_dropped_checklist_item_is_detected(self) -> None:
        tmp, root = self._copy()
        with tmp:
            self.assertEqual("", compare_details_checklist(root), "乾淨副本應無差異")
            replace_once(
                contract_path(root, "details"),
                "- [ ] 範圍與 non-goals 都寫了。\n",
                "",
            )
            diff = compare_details_checklist(root)
            self.assertNotEqual("", diff, "刪掉檢查清單一項後，T3 應報差異")
            print(f"[T7] 刪掉檢查清單一項後，T3 差異：\n{diff}")

    def test_t8_mutated_type_enumeration_is_detected(self) -> None:
        for key in ("template", "pm", "skill"):
            with self.subTest(file=key):
                tmp, root = self._copy()
                with tmp:
                    clean = compare_type_enumeration(root)
                    self.assertEqual("", clean, f"乾淨副本應無差異，實際：\n{clean}")
                    enum = type_enumeration(root)
                    old = f"<{enum}>" if key == "template" else enum
                    new = old.replace("／原型", "／示範")
                    replace_once(contract_path(root, key), old, new)
                    diff = compare_type_enumeration(root)
                    self.assertNotEqual("", diff, f"突變 {key} 的列舉後，T4 應報差異")
                    print(f"[T8] 突變 {key} 的類型列舉後，T4 差異：\n{diff}")


if __name__ == "__main__":
    unittest.main()
