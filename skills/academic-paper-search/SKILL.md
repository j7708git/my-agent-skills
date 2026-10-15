---
name: academic-paper-search
description: 以 arXiv API 做結構化學術論文檢索——依關鍵字、作者、分類、日期範圍查詢，回傳標題、作者、摘要、arXiv ID、分類、連結等完整 metadata（JSON）。適用於找論文、查 arXiv 前置印本、建立文獻清單或產生 BibTeX。需要 Hugging Face Hub 的熱門論文與社群訊號時改用 hf-cli skill 的 hf papers 子命令。
license: MIT
compatibility: Python 3.8 以上（僅標準庫 urllib 與 xml.etree），需可連外存取 https://export.arxiv.org/api/query。
---

# academic-paper-search

用 arXiv 官方 API 做**結構化**的論文檢索：給一段查詢，拿回每篇論文的標題、作者、摘要、
arXiv ID、分類、發表／更新時間與連結。輸出三種格式：`json`（工具串接）、
`text`（人看）、`bibtex`（文獻清單）。

這個 skill 是**檢索工具**，不是論文閱讀器、不做全文分析，也不下載 PDF 內容。

## 何時使用

- 使用者要找論文、查 arXiv 前置印本、確認某篇論文是否存在或取得它的 metadata。
- 要建立文獻清單、產生 BibTeX、或依作者／分類／日期範圍做清單式檢索。
- 要用程式取得可重複、可排序的檢索結果（JSON）。

**不要用**這個 skill 的情況：

- 需要 Hugging Face Hub 的熱門論文、社群訊號（按讚、下載數）或 Hub 上的模型卡引用：
  改用 `hf-cli` skill 的 `hf papers info`／`list`／`search`／`read` 子命令。
  本 skill 只管 arXiv 本身的檢索結果，兩者資料來源不同。
- 需要全文問答、章節摘要或跨論文比較：先用本 skill 取得候選清單，再用其他工具取得全文。

## 前置條件

| 需求 | 說明 | 缺少時 |
| --- | --- | --- |
| Python 3.8 以上 | 腳本只用標準庫 `urllib` 與 `xml.etree` | 無法執行 |
| 可連外到 `https://export.arxiv.org/api/query` | 唯一的連外點 | 腳本以退出碼 2 回報，不會假裝有結果 |

不需要任何第三方套件、不需要 API key、不會下載 PDF。

## 安裝位置與資源

skill 安裝後是一個自足的目錄，只會複製 skill 目錄本身。以下路徑都相對於 skill 根目錄：

```text
SKILL.md
scripts/arxiv_search.py
references/arxiv-query-syntax.md
references/bibtex-generation.md
```

執行時的工作目錄不影響解析——請直接以 skill 目錄為基準呼叫腳本。
常見的安裝位置是專案底下的 .agents/skills/academic-paper-search 目錄：

```bash
cd <skill 目錄>
python3 scripts/arxiv_search.py --query "all:agent memory" --format text
```

## 用法

```bash
python3 scripts/arxiv_search.py --query "all:agent memory" --max-results 10 --start 0 --sort relevance --format json
python3 scripts/arxiv_search.py --query 'au:"Yoshua Bengio" AND cat:cs.LG' --format text
python3 scripts/arxiv_search.py --query "submittedDate:[202501010000+TO+202506302359]" --max-results 20 --format bibtex
```

| 參數 | 必填 | 預設 | 說明 |
| --- | --- | --- | --- |
| `--query` | 是 | 無 | arXiv 查詢字串；語法見 `references/arxiv-query-syntax.md` |
| `--max-results` | 否 | `10` | 本次回傳筆數；單次上限 2000，要更多請用 `--start` 分頁 |
| `--start` | 否 | `0` | 起始索引（0 為基準） |
| `--sort` | 否 | `relevance` | `relevance` 或 `submittedDate`（皆為遞減） |
| `--format` | 否 | `json` | `json`、`text` 或 `bibtex` |

查詢字串由腳本做 URL 編碼後送出，直接寫 arXiv 語法即可：

- 欄位前綴：`all:`、`ti:`（標題）、`au:`（作者）、`abs:`（摘要）、`co:`（備註）、
  `jr:`（期刊引用）、`cat:`（分類）、`id:`。
- 布林運算：`AND`、`OR`、`ANDNOT`；用括號分組（例如 `ti:(agent OR "language model")`）。
- 日期範圍：`submittedDate:[YYYYMMDDHHMM+TO+YYYYMMDDHHMM]`（GMT）。`+` 與空格等價，
  兩種寫法都能用。

更完整的語法、分類代碼與分頁細節見 `references/arxiv-query-syntax.md`。

## 輸出

### `--format json`（預設）

```json
{
  "total_results": 1523,
  "papers": [
    {
      "arxiv_id": "2401.12345v2",
      "title": "範例論文標題",
      "authors": ["Ada Example", "Bo Example"],
      "abstract": "論文摘要……",
      "primary_category": "cs.CL",
      "categories": ["cs.CL", "cs.AI"],
      "published": "2024-01-15T10:00:00-05:00",
      "updated": "2024-03-02T09:30:00-05:00",
      "pdf_url": "https://arxiv.org/pdf/2401.12345v2",
      "abs_url": "https://arxiv.org/abs/2401.12345v2",
      "doi": "10.1000/example"
    }
  ]
}
```

- `total_results` 是符合查詢的**總數**，不是本次回傳的筆數（本次筆數看 `papers` 長度）。
- 每篇固定有 `arxiv_id`、`title`、`authors`（字串陣列）、`abstract`、
  `primary_category`、`categories`、`published`、`updated`、`pdf_url`、`abs_url`。
- `doi` 與 `comment`（作者備註，如頁數、會議）**有值才出現**，不要假設一定有。
- 時間欄位沿用 Atom 的 ISO 8601（含時區）；連結一律為 `https`。
  作者所屬機構（`arxiv:affiliation`）不輸出。

### `--format text`

給人看的條列輸出，開頭含查詢字串、檢索時間（UTC）與總結果數。

### `--format bibtex`

每篇一個 `@misc` entry，BibTeX key 由 arXiv ID 轉換：`2401.12345v2` →
`arxiv-2401-12345v2`。欄位對應與注意事項（例如標題可能含 LaTeX 數學式）
見 `references/bibtex-generation.md`。沒有結果時不輸出任何內容。

## 退出碼

| 退出碼 | 意義 | 你該做的事 |
| --- | --- | --- |
| `0` | 成功。**含 0 筆結果**（`total_results` 為 0） | 正常使用結果；0 筆就照實說「查無結果」 |
| `2` | 網路／HTTP 錯誤、回應無法解析，或 arXiv 以 error feed 回報查詢錯誤 | 讀 stderr 的訊息，不要說成「查無結果」 |

參數錯誤（缺少 `--query`、`--sort` 值不合法、`--max-results` 超過上限等）由 argparse
處理，退出碼同樣是 `2`，訊息會寫到 stderr。

「查無結果」（退出碼 0）與「查詢失敗」（退出碼 2）是兩件不同的事，回報時必須分清。

## 給 agent 的使用流程

1. 先確認使用者要的是什麼：關鍵字檢索、特定作者、某段時間的新論文，還是某篇的 metadata？
   據此選欄位前綴與 `--sort`（要「最新」就用 `submittedDate`）。
2. 從寬查詢開始（`--max-results 5`～`10`）確認結果方向，需要更多再用 `--start` 分頁。
3. **多次請求之間間隔至少 3 秒**（arXiv 的禮貌規範）。一次執行只發一次請求；
   不要用迴圈連續打 API，同一查詢一天內也不會變（arXiv 一天只更新一次），請快取結果。
4. 請求一律帶具名 User-Agent；腳本已內建，**不要**改成瀏覽器偽裝或匿名字串。
5. **回報時附上查詢日期**（例如「檢索日期：2026-10-15」），並寫出實際使用的查詢字串。
   論文的 metadata 會隨新版本改變，沒有日期的清單無法判斷新鮮度。`text` 格式已自動
   輸出 UTC 檢索時間；用 `json` 時請自己把日期寫在回報裡。
6. 附上 `abs_url`（摘要頁）與 `pdf_url`，讓使用者能直接點開；引用時優先給 arXiv ID。
7. 結果為 0 筆時，先檢查查詢語法是否被拆錯（引號、括號、`+TO+`），再回報查無結果。
8. 要產生參考文獻清單時用 `--format bibtex`，並在回報中說明 key 的命名規則。

## 禮貌規範與邊界

- 間隔至少 3 秒、具名 User-Agent、優先小量請求：這是 arXiv API 的明確要求。
- `max_results` 單次上限 2000、總量上限 30000；超過會被拒絕。需要大量資料請改用
  arXiv 的 OAI-PMH 或 bulk data 介面，不要對 API 反覆抓取。
- 這個 skill 不做：全文下載與解析、引用網路分析、非 arXiv 來源的檢索、
  快取或資料庫、結果去重與評分排序。

## 深入文件

- `references/arxiv-query-syntax.md`：欄位前綴、布林運算、日期範圍、分類代碼、
  分頁與排序參數、禮貌規範，以及官方文件出處與查核日期。
- `references/bibtex-generation.md`：`--format bibtex` 的欄位對應、arXiv 論文的
  常見 entry 慣例與注意事項。

## 來源與致謝

本 skill 的 arXiv 查詢流程改寫自 Hermes Agent 的 research/arxiv skill（MIT 授權）。
實作、查詢建構與輸出格式依 arXiv 官方 API 文件重寫，並改為純 Python 3 標準庫、
不引入第三方相依。
