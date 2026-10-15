"""``academic-paper-search`` 檢索腳本的離線單元測試。

涵蓋範圍
--------
- **查詢建構**：``+`` 正規化成空格、URL 編碼（``:``、``[``、``]``、片語引號）、
  ``sortBy``／``sortOrder`` 對應、不合法排序值。
- **Atom 解析**（合成 XML fixture）：``opensearch:totalResults``、多作者、
  ``arxiv:affiliation`` 不混進姓名、``arxiv:primary_category``、多個 ``<category>``
  （含非 arXiv 的 ACM 代碼）、缺 ``arxiv:doi``／``arxiv:comment`` 的情況、
  標題與摘要的空白正規化、連結缺漏時的推導、``http`` → ``https``、
  0 筆結果、``totalResults`` 缺席時退回 entry 數量、非 feed 與壞 XML、
  arXiv 的 error feed。
- **三種輸出格式**：``json``（欄位與選配欄位）、``text``（含 0 筆）、
  ``bibtex``（cite key、欄位對應、缺值不輸出、轉義）。
- **退出碼**：成功（含 0 筆）為 0；網路錯誤、HTTP 錯誤、無法解析、error feed 為 2；
  參數錯誤交給 argparse（也是 2）。
- **網路層**：以 ``unittest.mock`` 替換 ``urllib.request.urlopen``，確認送出的是
  具名 User-Agent，且整個測試檔**不連網**。

所有 fixture 都是合成資料，不含真實論文內容、憑證或個人資料。
"""

from __future__ import annotations

import importlib.util
import json
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "academic-paper-search" / "scripts" / "arxiv_search.py"


def load_module():
    """以檔案路徑載入 skill 腳本（它不是可安裝的套件）。"""
    spec = importlib.util.spec_from_file_location("academic_paper_search_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


arxiv = load_module()


# ---------------------------------------------------------------------------
# 合成 fixture
# ---------------------------------------------------------------------------

FEED_OPEN = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<feed xmlns="http://www.w3.org/2005/Atom" '
    'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
    'xmlns:arxiv="http://arxiv.org/schemas/atom">\n'
)

ENTRY_ONE = """
  <entry>
    <id>http://arxiv.org/abs/2401.12345v2</id>
    <updated>2024-03-02T09:30:00-05:00</updated>
    <published>2024-01-15T10:00:00-05:00</published>
    <title>An Example Paper About
      Agent Memory</title>
    <summary>  We study agent memory in
      synthetic experiments.
      Second line of the abstract.  </summary>
    <author>
      <name>Ada Example</name>
      <arxiv:affiliation>Example University</arxiv:affiliation>
    </author>
    <author>
      <name>Bo Example</name>
    </author>
    <arxiv:comment>17 pages, 3 figures</arxiv:comment>
    <arxiv:doi>10.1000/example</arxiv:doi>
    <link href="http://arxiv.org/abs/2401.12345v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2401.12345v2" rel="related" type="application/pdf"/>
    <link title="doi" href="http://dx.doi.org/10.1000/example" rel="related"/>
    <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <category term="I.2.6" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
"""

# 舊式 ID（含斜線）、沒有 <link>（連結必須由 arXiv ID 推導）、沒有 doi／comment。
ENTRY_TWO = """
  <entry>
    <id>http://arxiv.org/abs/hep-ex/0307015v1</id>
    <updated>2003-07-07T13:46:39-04:00</updated>
    <published>2003-07-07T13:46:39-04:00</published>
    <title>Multi-Electron Production at High Transverse Momenta</title>
    <summary>Short synthetic abstract.</summary>
    <author>
      <name>H1 Collaboration</name>
    </author>
    <arxiv:primary_category term="hep-ex" scheme="http://arxiv.org/schemas/atom"/>
    <category term="hep-ex" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
"""


def build_feed(total_results: str, entries: str = "") -> str:
    return (
        FEED_OPEN
        + f"  <opensearch:totalResults>{total_results}</opensearch:totalResults>\n"
        + '  <opensearch:startIndex>0</opensearch:startIndex>\n'
        + '  <opensearch:itemsPerPage>2</opensearch:itemsPerPage>\n'
        + entries
        + "</feed>\n"
    )


FIXTURE_FEED = build_feed("42", ENTRY_ONE + ENTRY_TWO)
EMPTY_FEED = build_feed("0")
ERROR_FEED = (
    FEED_OPEN
    + "  <opensearch:totalResults>1</opensearch:totalResults>\n"
    + """  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format_for_2401.1234</id>
    <title>Error</title>
    <summary>incorrect id format for 2401.1234</summary>
    <updated>2007-10-12T00:00:00-04:00</updated>
    <author><name>arXiv api core</name></author>
  </entry>
"""
    + "</feed>\n"
)


class FakeResponse:
    """假的 urlopen 回應：支援 context manager 與 ``read()``。"""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def run_main(argv):
    """執行 ``main``，回傳 (exit code, stdout, stderr)。"""
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = arxiv.main(argv)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# 查詢建構
# ---------------------------------------------------------------------------


class QueryBuildingTests(unittest.TestCase):
    def test_normalize_query_treats_plus_as_space(self):
        self.assertEqual(
            arxiv.normalize_query("submittedDate:[202301010600+TO+202401010600]"),
            "submittedDate:[202301010600 TO 202401010600]",
        )

    def test_normalize_query_trims_whitespace(self):
        self.assertEqual(arxiv.normalize_query("  all:agent memory  "), "all:agent memory")

    def test_build_url_encodes_field_prefix_and_space(self):
        url = arxiv.build_url("all:agent memory")
        self.assertTrue(url.startswith(arxiv.API_ENDPOINT + "?"))
        self.assertIn("search_query=all%3Aagent+memory", url)

    def test_build_url_encodes_grouping_and_phrases(self):
        url = arxiv.build_url('au:"Ada Example" ANDNOT (ti:memory OR ti:recall)')
        self.assertIn("%22Ada+Example%22", url)
        self.assertIn("%28", url)
        self.assertIn("%29", url)

    def test_build_url_date_range_keeps_to_separator(self):
        url = arxiv.build_url("submittedDate:[202301010600+TO+202401010600]")
        self.assertIn("submittedDate%3A%5B202301010600+TO+202401010600%5D", url)
        # ``+`` 是空格，不是字面上的加號編碼。
        self.assertNotIn("%2B", url)

    def test_build_url_includes_paging_and_sort_parameters(self):
        url = arxiv.build_url("all:test", max_results=7, start=14, sort="submittedDate")
        self.assertIn("start=14", url)
        self.assertIn("max_results=7", url)
        self.assertIn("sortBy=submittedDate", url)
        self.assertIn("sortOrder=descending", url)

    def test_build_url_uses_https_endpoint_by_default(self):
        self.assertTrue(arxiv.API_ENDPOINT.startswith("https://export.arxiv.org/"))

    def test_build_url_rejects_unknown_sort(self):
        with self.assertRaises(ValueError):
            arxiv.build_url("all:test", sort="lastUpdatedDate")

    def test_build_params_keeps_search_query_first(self):
        params = arxiv.build_params("all:test")
        self.assertEqual(params[0], ("search_query", "all:test"))


# ---------------------------------------------------------------------------
# Atom 解析
# ---------------------------------------------------------------------------


class AtomParsingTests(unittest.TestCase):
    def setUp(self):
        self.result = arxiv.parse_atom(FIXTURE_FEED)
        self.papers = self.result["papers"]
        self.first, self.second = self.papers

    def test_total_results_is_integer_from_opensearch(self):
        self.assertEqual(self.result["total_results"], 42)
        self.assertEqual(len(self.papers), 2)

    def test_title_and_abstract_whitespace_is_normalised(self):
        self.assertEqual(self.first["title"], "An Example Paper About Agent Memory")
        self.assertEqual(
            self.first["abstract"],
            "We study agent memory in synthetic experiments. Second line of the abstract.",
        )

    def test_authors_are_names_in_order_without_affiliation(self):
        self.assertEqual(self.first["authors"], ["Ada Example", "Bo Example"])

    def test_primary_category_and_categories(self):
        self.assertEqual(self.first["primary_category"], "cs.CL")
        self.assertEqual(self.first["categories"], ["cs.CL", "cs.AI", "I.2.6"])

    def test_timestamps_are_passed_through(self):
        self.assertEqual(self.first["published"], "2024-01-15T10:00:00-05:00")
        self.assertEqual(self.first["updated"], "2024-03-02T09:30:00-05:00")

    def test_optional_fields_present_when_supplied(self):
        self.assertEqual(self.first["doi"], "10.1000/example")
        self.assertEqual(self.first["comment"], "17 pages, 3 figures")

    def test_optional_fields_absent_when_missing(self):
        self.assertNotIn("doi", self.second)
        self.assertNotIn("comment", self.second)

    def test_arxiv_id_keeps_version_and_new_style_id(self):
        self.assertEqual(self.first["arxiv_id"], "2401.12345v2")
        self.assertEqual(
            self.first["abs_url"], "https://arxiv.org/abs/2401.12345v2"
        )
        self.assertEqual(
            self.first["pdf_url"], "https://arxiv.org/pdf/2401.12345v2"
        )

    def test_links_are_https_normalised(self):
        self.assertTrue(self.first["abs_url"].startswith("https://"))
        self.assertTrue(self.first["pdf_url"].startswith("https://"))

    def test_old_style_id_and_missing_links_are_derived(self):
        self.assertEqual(self.second["arxiv_id"], "hep-ex/0307015v1")
        self.assertEqual(self.second["abs_url"], "https://arxiv.org/abs/hep-ex/0307015v1")
        self.assertEqual(self.second["pdf_url"], "https://arxiv.org/pdf/hep-ex/0307015v1")

    def test_paper_keys_match_the_documented_field_list(self):
        for paper in self.papers:
            for field in arxiv.PAPER_FIELDS:
                self.assertIn(field, paper)
            for optional in arxiv.OPTIONAL_PAPER_FIELDS:
                if optional in paper:
                    self.assertTrue(paper[optional])

    def test_empty_result_reports_zero(self):
        result = arxiv.parse_atom(EMPTY_FEED)
        self.assertEqual(result, {"total_results": 0, "papers": []})

    def test_total_results_falls_back_to_entry_count(self):
        feed = FEED_OPEN + ENTRY_ONE + "</feed>\n"
        result = arxiv.parse_atom(feed)
        self.assertEqual(result["total_results"], 1)

    def test_non_feed_root_is_rejected(self):
        with self.assertRaises(arxiv.ParseError):
            arxiv.parse_atom("<rss><channel/></rss>")

    def test_broken_xml_is_rejected(self):
        with self.assertRaises(arxiv.ParseError):
            arxiv.parse_atom(FEED_OPEN + "<entry>")

    def test_non_integer_total_results_is_rejected(self):
        with self.assertRaises(arxiv.ParseError):
            arxiv.parse_atom(build_feed("lots"))

    def test_error_feed_raises_with_summary(self):
        with self.assertRaises(arxiv.ApiErrorFeed) as ctx:
            arxiv.parse_atom(ERROR_FEED)
        self.assertIn("incorrect id format for 2401.1234", str(ctx.exception))

    def test_bytes_input_is_accepted(self):
        self.assertEqual(
            arxiv.parse_atom(FIXTURE_FEED.encode("utf-8")), self.result
        )


# ---------------------------------------------------------------------------
# 輸出格式
# ---------------------------------------------------------------------------


class OutputFormatTests(unittest.TestCase):
    def setUp(self):
        self.result = arxiv.parse_atom(FIXTURE_FEED)
        self.paper = self.result["papers"][0]

    def test_json_round_trips_with_expected_shape(self):
        payload = json.loads(arxiv.format_json(self.result))
        self.assertEqual(payload["total_results"], 42)
        self.assertEqual(len(payload["papers"]), 2)
        self.assertEqual(payload["papers"][0]["arxiv_id"], "2401.12345v2")
        self.assertEqual(payload["papers"][0]["authors"], ["Ada Example", "Bo Example"])
        self.assertNotIn("doi", payload["papers"][1])

    def test_json_is_utf8_friendly(self):
        payload = {"total_results": 1, "papers": [{"title": "代理人記憶"}]}
        text = arxiv.format_json(payload)
        self.assertIn("代理人記憶", text)
        self.assertEqual(json.loads(text), payload)

    def test_text_contains_header_and_fields(self):
        text = arxiv.format_text(
            self.result, query="all:agent+memory", retrieved_at="2026-10-15T00:00:00Z"
        )
        self.assertIn("arXiv 檢索：all:agent memory", text)
        self.assertIn("檢索時間（UTC）：2026-10-15T00:00:00Z", text)
        self.assertIn("總結果數：42", text)
        self.assertIn("2401.12345v2", text)
        self.assertIn("Ada Example, Bo Example", text)
        self.assertIn("https://arxiv.org/pdf/2401.12345v2", text)
        self.assertIn("DOI", text)

    def test_text_omits_optional_lines_when_absent(self):
        text = arxiv.format_text({"total_results": 1, "papers": [self.result["papers"][1]]})
        self.assertNotIn("DOI", text)
        self.assertNotIn("備註", text)

    def test_text_handles_zero_results(self):
        text = arxiv.format_text({"total_results": 0, "papers": []})
        self.assertIn("總結果數：0", text)
        self.assertIn("沒有符合的論文", text)

    def test_bibtex_key_conversion(self):
        self.assertEqual(arxiv.bibtex_key("2401.12345v2"), "arxiv-2401-12345v2")
        self.assertEqual(arxiv.bibtex_key("hep-ex/0307015v1"), "arxiv-hep-ex-0307015v1")
        self.assertEqual(arxiv.bibtex_key("cs/9901002"), "arxiv-cs-9901002")
        self.assertEqual(arxiv.bibtex_key(""), "arxiv-unknown")

    def test_bibtex_entry_fields(self):
        entry = arxiv.bibtex_entry(self.paper)
        self.assertTrue(entry.startswith("@misc{arxiv-2401-12345v2,"))
        self.assertIn("title = {An Example Paper About Agent Memory},", entry)
        self.assertIn("author = {Ada Example and Bo Example},", entry)
        self.assertIn("year = {2024},", entry)
        self.assertIn("eprint = {2401.12345v2},", entry)
        self.assertIn("archivePrefix = {arXiv},", entry)
        self.assertIn("primaryClass = {cs.CL},", entry)
        self.assertIn("doi = {10.1000/example},", entry)
        self.assertIn("url = {https://arxiv.org/abs/2401.12345v2},", entry)
        self.assertIn("note = {17 pages, 3 figures},", entry)
        self.assertTrue(entry.endswith("}"))

    def test_bibtex_entry_omits_missing_optional_fields(self):
        entry = arxiv.bibtex_entry(self.result["papers"][1])
        self.assertIn("@misc{arxiv-hep-ex-0307015v1,", entry)
        self.assertIn("eprint = {hep-ex/0307015v1},", entry)
        self.assertIn("year = {2003},", entry)
        self.assertNotIn("doi =", entry)
        self.assertNotIn("note =", entry)

    def test_bibtex_escape_protects_structure(self):
        self.assertEqual(arxiv.bibtex_escape("a{b}c"), "a\\{b\\}c")
        self.assertEqual(arxiv.bibtex_escape("back\\slash"), "back\\textbackslash{}slash")
        entry = arxiv.bibtex_entry({"arxiv_id": "2401.00001", "title": "Braces {like} this"})
        self.assertIn("title = {Braces \\{like\\} this},", entry)

    def test_bibtex_entry_skips_empty_year(self):
        entry = arxiv.bibtex_entry({"arxiv_id": "2401.00001", "published": ""})
        self.assertNotIn("year =", entry)

    def test_format_bibtex_separates_entries(self):
        text = arxiv.format_bibtex(self.result)
        self.assertEqual(text.count("@misc{"), 2)
        self.assertIn("\n\n", text)
        self.assertTrue(text.endswith("}\n"))

    def test_format_bibtex_empty_result_is_empty(self):
        self.assertEqual(arxiv.format_bibtex({"total_results": 0, "papers": []}), "")


# ---------------------------------------------------------------------------
# CLI 與退出碼
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def test_fetch_atom_sends_named_user_agent(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(b"<feed/>")

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            payload = arxiv.fetch_atom(arxiv.API_ENDPOINT)

        self.assertEqual(payload, "<feed/>")
        headers = {key.lower(): value for key, value in captured["request"].headers.items()}
        self.assertIn("user-agent", headers)
        self.assertIn("academic-paper-search", headers["user-agent"])
        self.assertNotIn("python-urllib", headers["user-agent"])

    def test_main_prints_json_and_returns_zero(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return FakeResponse(FIXTURE_FEED.encode("utf-8"))

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            code, out, err = run_main(["--query", "all:agent memory", "--max-results", "2"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("search_query=all%3Aagent+memory", captured["url"])
        self.assertIn("max_results=2", captured["url"])
        payload = json.loads(out)
        self.assertEqual(payload["total_results"], 42)
        self.assertEqual(len(payload["papers"]), 2)

    def test_main_zero_results_is_success(self):
        with mock.patch.object(
            urllib.request, "urlopen", lambda request, timeout=None: FakeResponse(EMPTY_FEED.encode("utf-8"))
        ):
            code, out, err = run_main(["--query", "all:no such thing"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), {"total_results": 0, "papers": []})

    def test_main_text_format_includes_query_and_timestamp(self):
        with mock.patch.object(
            urllib.request, "urlopen", lambda request, timeout=None: FakeResponse(FIXTURE_FEED.encode("utf-8"))
        ):
            code, out, _ = run_main(["--query", "all:agent memory", "--format", "text"])

        self.assertEqual(code, 0)
        self.assertIn("arXiv 檢索：all:agent memory", out)
        self.assertIn("檢索時間（UTC）：", out)

    def test_main_bibtex_format(self):
        with mock.patch.object(
            urllib.request, "urlopen", lambda request, timeout=None: FakeResponse(FIXTURE_FEED.encode("utf-8"))
        ):
            code, out, _ = run_main(["--query", "all:agent memory", "--format", "bibtex"])

        self.assertEqual(code, 0)
        self.assertIn("@misc{arxiv-2401-12345v2,", out)
        self.assertIn("@misc{arxiv-hep-ex-0307015v1,", out)

    def test_main_bibtex_format_with_no_results_prints_nothing(self):
        with mock.patch.object(
            urllib.request, "urlopen", lambda request, timeout=None: FakeResponse(EMPTY_FEED.encode("utf-8"))
        ):
            code, out, err = run_main(["--query", "all:nothing", "--format", "bibtex"])

        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_main_network_error_returns_two(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("network is unreachable")

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            code, out, err = run_main(["--query", "all:agent memory"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("錯誤", err)
        self.assertIn("network is unreachable", err)

    def test_main_http_error_returns_two(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                arxiv.API_ENDPOINT, 503, "Service Unavailable", None, None
            )

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            code, out, err = run_main(["--query", "all:agent memory"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("503", err)

    def test_main_timeout_returns_two(self):
        def fake_urlopen(request, timeout=None):
            raise TimeoutError("timed out")

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            code, out, err = run_main(["--query", "all:agent memory"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("逾時", err)

    def test_main_unparsable_response_returns_two(self):
        with mock.patch.object(
            urllib.request, "urlopen", lambda request, timeout=None: FakeResponse(b"<html>nope</html>")
        ):
            code, out, err = run_main(["--query", "all:agent memory"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("錯誤", err)

    def test_main_api_error_feed_returns_two(self):
        with mock.patch.object(
            urllib.request, "urlopen", lambda request, timeout=None: FakeResponse(ERROR_FEED.encode("utf-8"))
        ):
            code, out, err = run_main(["--query", "id:2401.1234"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("incorrect id format", err)

    def test_main_requires_query(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["--max-results", "1"])
        self.assertEqual(ctx.exception.code, 2)

    def test_main_rejects_unknown_sort(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["--query", "all:test", "--sort", "lastUpdatedDate"])
        self.assertEqual(ctx.exception.code, 2)

    def test_main_rejects_max_results_over_api_limit(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["--query", "all:test", "--max-results", str(arxiv.MAX_RESULTS_PER_REQUEST + 1)])
        self.assertEqual(ctx.exception.code, 2)

    def test_main_rejects_negative_start(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["--query", "all:test", "--start", "-1"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
