> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 從檢索結果產生 BibTeX

`scripts/arxiv_search.py --format bibtex` 會把每篇論文輸出成一個 `@misc` entry。
本文件說明欄位怎麼對應、為什麼用 `@misc`，以及需要注意的地方。

- **查核日期**：2026-10-15
- 欄位來源是 arXiv API 回應的元素（見 `arxiv-query-syntax.md`），不是外部推測。

## 範例輸出

```bibtex
@misc{arxiv-2401-12345v2,
  title = {An Example Paper About Agent Memory},
  author = {Ada Example and Bo Example},
  year = {2024},
  eprint = {2401.12345v2},
  archivePrefix = {arXiv},
  primaryClass = {cs.CL},
  doi = {10.1000/example},
  url = {https://arxiv.org/abs/2401.12345v2},
  note = {17 pages, 3 figures},
}
```

## 欄位對應

| BibTeX 欄位 | 來源 | 出現時機 |
| --- | --- | --- |
| entry type | 固定 `@misc` | 一定有 |
| cite key | `arxiv_id` 轉換（見下） | 一定有 |
| `title` | `title` | 有標題就輸出 |
| `author` | `authors`，以 ` and ` 連接 | 有作者就輸出 |
| `year` | `published` 的前 4 個字元 | `published` 是 ISO 8601 時 |
| `eprint` | `arxiv_id`（**含版本後綴**，如 `2401.12345v2`） | 有 arXiv ID 就輸出 |
| `archivePrefix` | 固定 `arXiv` | 有 arXiv ID 就輸出 |
| `primaryClass` | `primary_category` | 有主要分類就輸出 |
| `doi` | `doi` | 有值才輸出 |
| `url` | `abs_url`（https 摘要頁） | 有值就輸出 |
| `note` | `comment`（作者備註，如頁數、會議） | 有值才輸出 |

沒有值的欄位不會出現，不要假設固定欄位組合。

## cite key 的命名規則

`arxiv_id` 只保留英數字與單一連字號，加上 `arxiv-` 前綴：

| arXiv ID | BibTeX key |
| --- | --- |
| `2401.12345v2` | `arxiv-2401-12345v2` |
| `2401.12345` | `arxiv-2401-12345` |
| `hep-ex/0307015v1` | `arxiv-hep-ex-0307015v1` |
| `cs/9901002` | `arxiv-cs-9901002` |

規則：把非英數字元連續片段換成一個 `-`，去掉頭尾的 `-`，前面加 `arxiv-`。
ID 為空時退回 `arxiv-unknown`（不會產生沒有 key 的 entry）。

同一次檢索中 key 不會重複，因為 arXiv ID 帶版本後綴且本身唯一；
但**跨次檢索**（同一篇論文不同版本）key 會不同，合併清單時請自行決定要保留哪一版。

## 為什麼用 `@misc`（arXiv 慣例）

arXiv 自己在摘要頁提供的引用格式，以及社群常見的 arXiv BibTeX entry，都以
`@misc` 為主，靠 `eprint`＋`archivePrefix`＋`primaryClass` 這三個欄位讓
biblatex／BibTeX 認得這是 arXiv 前置印本：

- `archivePrefix = {arXiv}` 是 biblatex 的標準寫法（有些舊範本寫 `archiveprefix` 或
  `eprinttype = {arXiv}`，語意相同）。
- `primaryClass` 只放主要分類；cross-list 的其他分類不進 BibTeX。
- 論文若有正式出版版本，權威來源是期刊（`journal_ref` 對應的資訊）；
  arXiv entry 只是前置印本，`doi` 有值時可能是已發表版本的 DOI。

要改成 `@article`（已正式發表）時，請把 `journal`、`volume`、`pages` 補上；
本 skill 只輸出 API 提供的 metadata，不會臆測這些欄位。

## 注意事項與已知限制

- **版本後綴會進 key 與 `eprint`**：`2401.12345v2` 指的是第 2 版，
  引用時若要「最新版」建議查一次最新 ID 再引用。
- **標題可能是 LaTeX 數學式**：arXiv 標題常含 `$...$`、`\alpha` 這類片段。
  腳本只轉義會破壞 BibTeX 結構的字元（反斜線與大括號，例如 `\` 變成
  `\textbackslash{}`、`{` 變成 `\{`），**其他 LaTeX 特殊字元原樣保留**，
  因為過度轉義會改壞數學式。放進 LaTeX 文件後若編譯錯誤，請人工確認該筆 entry。
- **`&`、`%`、`_` 未轉義**：BibTeX 不處理它們，但 LaTeX 排版時可能需要。
  這是刻意的取捨，請在文件端處理（放在 `\url{}` 或使用 `biblatex` 的 `\text` 形式）。
- **`year` 取自提交時間**：`published` 是首次提交時間（GMT，含時區位移）。
  接近年底或跨時區時，年份可能與你預期的當地年份差一年。
- **`note` 是作者備註**：內容是作者自填的（頁數、圖表數、會議名稱），
  格式不固定，不要拿它做程式化解析。
- **作者機構不輸出**：Atom 的 `arxiv:affiliation` 不在 BibTeX 欄位裡；
  需要機構資訊請查摘要頁。
- 沒有結果時 `--format bibtex` 不輸出任何內容（含換行），檔案會是空的。

## 把輸出接進檔案

```bash
python3 scripts/arxiv_search.py --query 'cat:cs.CL AND ti:retrieval' --max-results 20 --format bibtex > refs.bib
```

- 輸出每筆 entry 之間以一個空行分隔，結尾有換行；可直接接檔案或貼進既有 `.bib`。
- 腳本不會去重、不會排序：合併多筆輸出時請自行確認 key 沒有衝突。
- 附帶提醒：`--format bibtex` 不會輸出檢索日期，回報時請自行註明查詢日期。
