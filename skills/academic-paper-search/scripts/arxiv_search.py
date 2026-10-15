#!/usr/bin/env python3
"""arXiv API 結構化檢索（純 Python 3 標準函式庫）。

這個腳本只用 ``urllib.request`` 與 ``xml.etree.ElementTree``，不引入任何第三方相依。
它把 arXiv 的 Atom 回應轉成三種格式：``json``（工具用）、``text``（人看）、
``bibtex``（文獻清單用）。

設計要點
--------
- **查詢建構、Atom 解析、輸出格式都是可直接 import 的純函式**：``normalize_query``、
  ``build_url``、``parse_atom``、``format_json``／``format_text``／``format_bibtex``。
  單元測試以合成 XML 離線驗證，不需要連網。
- **網路層只有一個連外點**：``fetch_atom``（``urllib.request.urlopen``）。解析與輸出
  不碰網路。
- 一律使用 ``https://export.arxiv.org/api/query``；``http://`` 會多一次 301。

退出碼
------
- ``0``：成功（含 0 筆結果，``total_results`` 為 0）。
- ``2``：網路／HTTP 錯誤、回應無法解析，或 arXiv 以 error feed 回報查詢錯誤；
  原因寫到 stderr。參數錯誤由 argparse 處理（預設退出碼也是 2）。

arXiv 的禮貌規範
---------------
- 多次請求之間請間隔 **至少 3 秒**；這個腳本每次執行只發一次請求。
- 使用具名 User-Agent，讓對方能辨識流量來源。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

#: 官方 API 端點。務必用 https；http:// 會 301 轉址。
API_ENDPOINT = "https://export.arxiv.org/api/query"

#: 具名 User-Agent（arXiv 要求可辨識的來源）。
USER_AGENT = "academic-paper-search/1.0 (arXiv API client; my-agent-skills skill)"

#: ``max_results`` 預設值與單次請求上限（arXiv 的實作限制）。
DEFAULT_MAX_RESULTS = 10
MAX_RESULTS_PER_REQUEST = 2000

#: ``--sort`` 可選值對應的 arXiv ``sortBy``。
SORT_BY_MAP = {"relevance": "relevance", "submittedDate": "submittedDate"}

SORT_ORDER_CHOICES = ("descending", "ascending")

TIMEOUT_SECONDS = 30

NS_ATOM = "http://www.w3.org/2005/Atom"
NS_OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"
NS_ARXIV = "http://arxiv.org/schemas/atom"
NS = {"atom": NS_ATOM, "opensearch": NS_OPENSEARCH, "arxiv": NS_ARXIV}

#: Atom ``<id>`` 對應摘要頁；舊式 ID（如 ``hep-ex/0307015v1``）也在這個前綴之後。
ABS_URL_PREFIX_RE = re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/")
#: arXiv 的錯誤回應是單一 entry，其 ``<id>`` 形如 ``http://arxiv.org/api/errors#...``。
ERROR_ID_MARKER = "/api/errors#"
#: BibTeX key 只用英數與單一連字號。
BIBTEX_KEY_RE = re.compile(r"[^0-9A-Za-z]+")

#: 每篇論文的欄位順序（JSON 與內部 dict 共用）。
PAPER_FIELDS = (
    "arxiv_id",
    "title",
    "authors",
    "abstract",
    "primary_category",
    "categories",
    "published",
    "updated",
    "pdf_url",
    "abs_url",
)
#: 只在有值時才出現的欄位。
OPTIONAL_PAPER_FIELDS = ("doi", "comment")


class ArxivError(Exception):
    """本腳本對外回報的錯誤基底（一律對應退出碼 2）。"""


class ParseError(ArxivError):
    """回應不是預期的 Atom feed。"""


class ApiErrorFeed(ArxivError):
    """arXiv 以 error feed 回報查詢本身有問題。"""


# ---------------------------------------------------------------------------
# 查詢建構（純函式）
# ---------------------------------------------------------------------------


def normalize_query(query: str) -> str:
    """把查詢字串正規化成 arXiv 查詢語法的形式。

    使用者可能直接貼官方文件中的 URL 片段（``+`` 代表空格），例如
    ``submittedDate:[202301010600+TO+202401010600]``。這裡把 ``+`` 一律視為空格，
    兩種寫法因此等價；其他字元原樣保留，交由 URL 編碼處理。
    """
    return re.sub(r"\+", " ", query).strip()


def build_params(
    query: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    start: int = 0,
    sort: str = "relevance",
    sort_order: str = "descending",
) -> List[tuple]:
    """回傳查詢參數（保持 ``search_query`` 在最前的穩定順序）。"""
    return [
        ("search_query", normalize_query(query)),
        ("start", str(int(start))),
        ("max_results", str(int(max_results))),
        ("sortBy", SORT_BY_MAP[sort]),
        ("sortOrder", sort_order),
    ]


def build_url(
    query: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    start: int = 0,
    sort: str = "relevance",
    sort_order: str = "descending",
    endpoint: str = API_ENDPOINT,
) -> str:
    """組出完整查詢 URL（含 URL 編碼）。

    空白以 ``+`` 表示（官方文件的慣例），``:``、``[``、``]``、``(``、``)`` 等字元
    以百分號編碼；arXiv 伺服器會在解析前解碼回查詢語法。
    """
    if sort not in SORT_BY_MAP:
        raise ValueError(f"不支援的 sort：{sort}（可用：{', '.join(SORT_BY_MAP)}）")
    if sort_order not in SORT_ORDER_CHOICES:
        raise ValueError(f"不支援的 sort_order：{sort_order}（可用：{', '.join(SORT_ORDER_CHOICES)}）")
    encoded = urllib.parse.urlencode(build_params(query, max_results, start, sort, sort_order))
    return f"{endpoint}?{encoded}"


# ---------------------------------------------------------------------------
# Atom 解析（純函式）
# ---------------------------------------------------------------------------


def _localname(tag: str) -> str:
    """去掉 XML namespace 的 ``{...}`` 前綴。"""
    return tag.rsplit("}", 1)[-1]


def _clean_text(value: Optional[str]) -> str:
    """Atom 的值常有換行與縮排；壓成單行空白並去除前後空白。"""
    if not value:
        return ""
    return " ".join(value.split())


def _text(element: ET.Element, path: str) -> str:
    return _clean_text(element.findtext(path, default=None, namespaces=NS))


def _https(url: str) -> str:
    """arXiv 回傳 ``http://`` 連結，這裡改成 ``https://`` 以免多一次 301。"""
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def extract_arxiv_id(atom_id: str) -> str:
    """從 Atom ``<id>`` 取出 arXiv ID（保留版本後綴，如 ``2401.12345v2``）。"""
    raw = (atom_id or "").strip()
    without_prefix = ABS_URL_PREFIX_RE.sub("", raw)
    if without_prefix and without_prefix != raw:
        return without_prefix.strip("/")
    return raw.rstrip("/")


def _entry_links(entry: ET.Element) -> Dict[str, str]:
    """取出 ``<link>``：``alternate`` 是摘要頁，``related``＋``title=pdf`` 是 PDF。"""
    links: Dict[str, str] = {}
    for link in entry.findall("atom:link", NS):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        rel = (link.get("rel") or "").strip()
        title = (link.get("title") or "").strip().lower()
        if rel == "alternate" and "abs_url" not in links:
            links["abs_url"] = _https(href)
        elif rel == "related" and title == "pdf" and "pdf_url" not in links:
            links["pdf_url"] = _https(href)
    return links


def _parse_authors(entry: ET.Element) -> List[str]:
    """作者名稱清單（依作者順序）。

    ``arxiv:affiliation`` 是 ``<author>`` 的子元素；本腳本只輸出作者姓名，
    不把 affiliation 混進姓名，也不額外輸出 affiliation 欄位。
    """
    authors: List[str] = []
    for author in entry.findall("atom:author", NS):
        name = _clean_text(author.findtext("atom:name", default=None, namespaces=NS))
        if name:
            authors.append(name)
    return authors


def _parse_entry(entry: ET.Element) -> Dict[str, Any]:
    entry_id = _text(entry, "atom:id")
    if ERROR_ID_MARKER in entry_id:
        detail = _text(entry, "atom:summary") or "arXiv API 回報查詢錯誤（未提供說明）"
        raise ApiErrorFeed(detail)

    arxiv_id = extract_arxiv_id(entry_id)
    links = _entry_links(entry)
    abs_url = links.get("abs_url") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")
    pdf_url = links.get("pdf_url") or (f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "")

    primary = entry.find("arxiv:primary_category", NS)
    primary_category = _clean_text(primary.get("term")) if primary is not None else ""
    categories = [
        term
        for term in (
            _clean_text(category.get("term"))
            for category in entry.findall("atom:category", NS)
        )
        if term
    ]

    paper: Dict[str, Any] = {
        "arxiv_id": arxiv_id,
        "title": _text(entry, "atom:title"),
        "authors": _parse_authors(entry),
        "abstract": _text(entry, "atom:summary"),
        "primary_category": primary_category,
        "categories": categories,
        "published": _text(entry, "atom:published"),
        "updated": _text(entry, "atom:updated"),
        "pdf_url": pdf_url,
        "abs_url": abs_url,
    }
    doi = _text(entry, "arxiv:doi")
    if doi:
        paper["doi"] = doi
    comment = _text(entry, "arxiv:comment")
    if comment:
        paper["comment"] = comment
    return paper


def parse_atom(xml_text: str) -> Dict[str, Any]:
    """把 Atom 回應解析成 ``{"total_results": int, "papers": [...]}``。

    ``total_results`` 取自 ``opensearch:totalResults``；該元素缺席時退回本次回傳的
    entry 數量（並在文件中註明這個行為）。
    """
    if isinstance(xml_text, bytes):  # 方便呼叫端直接餵原始位元組
        xml_text = xml_text.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ParseError(f"arXiv 回應不是合法 XML：{exc}") from exc
    if _localname(root.tag) != "feed":
        raise ParseError(f"arXiv 回應的最上層元素不是 feed，而是 {_localname(root.tag)}")

    papers = [_parse_entry(entry) for entry in root.findall("atom:entry", NS)]

    total_raw = _text(root, "opensearch:totalResults")
    if total_raw:
        try:
            total_results = int(total_raw)
        except ValueError as exc:
            raise ParseError(f"opensearch:totalResults 不是整數：{total_raw!r}") from exc
    else:
        total_results = len(papers)
    return {"total_results": total_results, "papers": papers}


# ---------------------------------------------------------------------------
# 輸出格式（純函式）
# ---------------------------------------------------------------------------


def format_json(result: Dict[str, Any]) -> str:
    """JSON 輸出：``{"total_results": <int>, "papers": [...]}``。"""
    return json.dumps(result, ensure_ascii=False, indent=2)


def format_text(
    result: Dict[str, Any],
    query: Optional[str] = None,
    retrieved_at: Optional[str] = None,
) -> str:
    """人類可讀輸出。

    ``query`` 與 ``retrieved_at`` 由呼叫端傳入（預設不輸出），讓這個函式保持決定性；
    檢索日期由 agent 附在回報中，也是這裡顯示它的原因。
    """
    papers = result.get("papers", [])
    lines: List[str] = []
    if query is not None:
        lines.append(f"arXiv 檢索：{normalize_query(query)}")
    if retrieved_at is not None:
        lines.append(f"檢索時間（UTC）：{retrieved_at}")
    lines.append(
        f"總結果數：{result.get('total_results', 0)}；本次回傳 {len(papers)} 筆"
    )
    if not papers:
        lines.append("（沒有符合的論文）")
        return "\n".join(lines)

    for index, paper in enumerate(papers, start=1):
        lines.append("")
        lines.append(f"{index}. {paper.get('title', '')}")
        lines.append(f"   arXiv ID  ：{paper.get('arxiv_id', '')}")
        lines.append(f"   作者      ：{', '.join(paper.get('authors', []))}")
        lines.append(f"   主要分類  ：{paper.get('primary_category', '')}")
        lines.append(f"   分類      ：{', '.join(paper.get('categories', []))}")
        lines.append(f"   發表      ：{paper.get('published', '')}")
        lines.append(f"   更新      ：{paper.get('updated', '')}")
        lines.append(f"   PDF       ：{paper.get('pdf_url', '')}")
        lines.append(f"   摘要頁    ：{paper.get('abs_url', '')}")
        if paper.get("doi"):
            lines.append(f"   DOI       ：{paper['doi']}")
        if paper.get("comment"):
            lines.append(f"   備註      ：{paper['comment']}")
        lines.append(f"   摘要      ：{paper.get('abstract', '')}")
    return "\n".join(lines)


def bibtex_key(arxiv_id: str) -> str:
    """arXiv ID 轉 BibTeX key：``2401.12345v2`` → ``arxiv-2401-12345v2``。"""
    slug = BIBTEX_KEY_RE.sub("-", (arxiv_id or "").strip()).strip("-")
    slug = slug or "unknown"
    return f"arxiv-{slug}"


def bibtex_escape(value: str) -> str:
    """轉義會破壞 BibTeX 結構的字元（反斜線與大括號）。

    其他 LaTeX 特殊字元（``%``、``&``、``_`` 等）原樣保留：arXiv 標題常含 LaTeX
    數學式，過度轉義反而會改壞內容。細節見 ``references/bibtex-generation.md``。

    以單次掃描替換，避免把替換結果中的大括號再轉義一次。
    """
    replacements = {"\\": "\\textbackslash{}", "{": "\\{", "}": "\\}"}
    return re.sub(r"[\\{}]", lambda match: replacements[match.group(0)], value)


def _bibtex_field(name: str, value: str) -> str:
    return f"  {name} = {{{bibtex_escape(value)}}},"


def bibtex_entry(paper: Dict[str, Any]) -> str:
    """單篇論文的 ``@misc`` entry（arXiv 前置印本的常見慣例）。"""
    arxiv_id = paper.get("arxiv_id", "")
    fields: List[tuple] = []
    if paper.get("title"):
        fields.append(("title", paper["title"]))
    if paper.get("authors"):
        fields.append(("author", " and ".join(paper["authors"])))
    published = paper.get("published", "")
    year = published[:4] if len(published) >= 4 and published[:4].isdigit() else ""
    if year:
        fields.append(("year", year))
    if arxiv_id:
        fields.append(("eprint", arxiv_id))
        fields.append(("archivePrefix", "arXiv"))
    if paper.get("primary_category"):
        fields.append(("primaryClass", paper["primary_category"]))
    if paper.get("doi"):
        fields.append(("doi", paper["doi"]))
    if paper.get("abs_url"):
        fields.append(("url", paper["abs_url"]))
    if paper.get("comment"):
        fields.append(("note", paper["comment"]))

    body = "\n".join(_bibtex_field(name, value) for name, value in fields)
    return f"@misc{{{bibtex_key(arxiv_id)},\n{body}\n}}"


def format_bibtex(result: Dict[str, Any]) -> str:
    """每篇論文一個 ``@misc`` entry，以空行分隔。"""
    entries = [bibtex_entry(paper) for paper in result.get("papers", [])]
    if not entries:
        return ""
    return "\n\n".join(entries) + "\n"


# ---------------------------------------------------------------------------
# 網路層（唯一的連外點）
# ---------------------------------------------------------------------------


def fetch_atom(url: str, timeout: int = TIMEOUT_SECONDS) -> str:
    """對 arXiv API 發一次 GET，回傳 Atom XML 字串。

    只用具名 User-Agent；錯誤以 :class:`ArxivError` 往上拋，由 ``main`` 轉成退出碼 2。
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise ArxivError(f"HTTP {exc.code} {exc.reason}（{url}）") from exc
    except urllib.error.URLError as exc:
        raise ArxivError(f"無法連線 arXiv API：{exc.reason}（{url}）") from exc
    except TimeoutError as exc:
        raise ArxivError(f"請求逾時（{timeout} 秒）：{url}") from exc
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(payload)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必須是整數：{value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"必須大於等於 0：{number}")
    return number


def _positive_int(value: str) -> int:
    number = _non_negative_int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必須大於等於 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arxiv_search.py",
        description=(
            "以 arXiv API 做結構化論文檢索。查詢語法見 "
            "references/arxiv-query-syntax.md（欄位前綴、布林運算、日期範圍）。"
        ),
    )
    parser.add_argument("--query", required=True, help="arXiv 查詢字串，例如 'all:agent memory'")
    parser.add_argument(
        "--max-results",
        type=_positive_int,
        default=DEFAULT_MAX_RESULTS,
        help=f"本次回傳的論文數上限（預設 {DEFAULT_MAX_RESULTS}，單次上限 {MAX_RESULTS_PER_REQUEST}）",
    )
    parser.add_argument("--start", type=_non_negative_int, default=0, help="起始索引（0 為基準，預設 0）")
    parser.add_argument(
        "--sort",
        choices=sorted(SORT_BY_MAP),
        default="relevance",
        help="排序依據（預設 relevance）",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text", "bibtex"),
        default="json",
        help="輸出格式（預設 json）",
    )
    return parser


def retrieved_at_utc() -> str:
    """本次檢索時間（UTC，秒級），供 text 輸出標示查詢日期。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.max_results > MAX_RESULTS_PER_REQUEST:
        parser.error(
            f"--max-results 不得超過 {MAX_RESULTS_PER_REQUEST}"
            "（arXiv 單次請求的實作上限；請以 --start 分頁）"
        )

    url = build_url(args.query, args.max_results, args.start, args.sort)
    try:
        xml_text = fetch_atom(url)
        result = parse_atom(xml_text)
    except ArxivError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(format_json(result))
    elif args.format == "text":
        print(format_text(result, query=args.query, retrieved_at=retrieved_at_utc()))
    else:
        output = format_bibtex(result)
        if output:
            sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
