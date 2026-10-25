> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# arXiv API 查詢語法

本文件說明 `scripts/arxiv_search.py` 的 `--query` 可以寫什麼，以及底層 API 的參數、
分頁與禮貌規範。

- **查核日期**：2026-10-15
- **來源**（皆於查核日期實際取得）：
  - arXiv API User's Manual：<https://info.arxiv.org/help/api/user-manual.html>
  - Terms of Use for arXiv APIs：<https://info.arxiv.org/help/api/tou.html>
  - arXiv Category Taxonomy：<https://arxiv.org/category_taxonomy>
- 官方手冊另有連到 arXiv Identifier 與 Bulk Data 頁面；本文件**未**逐一查核那些頁面，
  需要時請自行確認。

## 端點與基本形式

```text
https://export.arxiv.org/api/query?search_query=<查詢>&start=0&max_results=10
```

- 一律使用 `https://export.arxiv.org`；`http://` 會多一次 301 轉址。
- 回應是 **Atom 1.0 XML**（`application/atom+xml`），不是 JSON。
- 可用 HTTP GET 或 POST；參數很長時 POST 較合適。本 skill 的腳本用 GET。

## 查詢參數

| 參數 | 型別 | 預設 | 說明 |
| --- | --- | --- | --- |
| `search_query` | string | 無 | 查詢字串（本文件主要內容） |
| `id_list` | 逗號分隔字串 | 無 | 直接指定 arXiv ID；要取特定論文時優先用它 |
| `start` | int | 0 | 起始索引（0 為基準） |
| `max_results` | int | 10 | 本次回傳筆數 |
| `sortBy` | string | `relevance` | `relevance`、`lastUpdatedDate`、`submittedDate` |
| `sortOrder` | string | `descending` | `ascending` 或 `descending` |

`search_query` 與 `id_list` 的組合邏輯：只給 `search_query` 時回傳符合查詢的論文；
只給 `id_list` 時回傳清單中的論文；兩者都給時回傳**兩者交集**（可當過濾器用）。

> 本 skill 的 `--sort` 只開放 `relevance` 與 `submittedDate`；官方另有
> `lastUpdatedDate`，需要時請直接呼叫 API。

## 欄位前綴

每個前綴後面接冒號，再接要搜尋的字詞：

| 前綴 | 搜尋範圍 |
| --- | --- |
| `ti` | 標題 |
| `au` | 作者 |
| `abs` | 摘要 |
| `co` | 作者備註（comment） |
| `jr` | 期刊引用（journal reference） |
| `cat` | 分類（subject category） |
| `rn` | 報告編號（report number） |
| `id` | arXiv ID（官方建議改用 `id_list`，才能正確處理版本） |
| `all` | 上述全部欄位 |

`all:` 會同時搜尋每一個欄位；不知道該用哪個前綴時從 `all:` 開始。

### 作者名稱的寫法

作者欄位用的是姓與名之間以底線連接的形式，例如 `au:del_maestro` 對應
「Adrian Del Maestro」。名字含縮寫或連字號時，建議先從寬查詢（`au:` 加姓氏）確認結果。

## 布林運算與分組

| 運算子／符號 | 意義 |
| --- | --- |
| `AND` | 兩者皆符合 |
| `OR` | 任一符合 |
| `ANDNOT` | 前者符合且後者不符合 |
| `( )` | 布林優先序分組 |
| `"..."` | 片語（多個字視為一個詞組） |
| 空白 | 擴充 `search_query` 的多個欄位條件（URL 中以 `+` 表示） |

範例（本 skill 的 `--query` 直接寫 arXiv 語法即可，編碼由腳本處理）：

```text
au:del_maestro AND ti:checkerboard
au:del_maestro ANDNOT (ti:checkerboard OR ti:Pyrochlore)
ti:"quantum criticality"
cat:cs.CL AND abs:retrieval
```

### URL 編碼對照（官方手冊）

| 字元 | 編碼 |
| --- | --- |
| 空白 | `+`（或 `%20`） |
| `(` `)` | `%28` `%29` |
| `"` | `%22` |

腳本以 `urllib.parse.urlencode` 產生查詢字串，因此 `:`、`[`、`]`、`(`、`)`、`"` 都會
被百分號編碼、空白會變成 `+`。arXiv 在解析前會解碼，所以 `--query` 直接寫原始語法就好。

**腳本對 `+` 的處理**：`--query` 內的 `+` 一律視為空格（URL 中 `+` 代表空格），
所以 `submittedDate:[202501010000+TO+202506302359]` 與
`submittedDate:[202501010000 TO 202506302359]` 等價。副作用是無法用 `+` 搜尋字面上的
加號（例如 `ti:C++`）：請改寫成 `ti:"C plus plus"` 之類的替代查法。

## 日期範圍

只有一個日期過濾欄位 `submittedDate`，格式為
`[YYYYMMDDTTTT+TO+YYYYMMDDTTTT]`，`TTTT` 是 24 小時制的時與分，**格林威治時間（GMT）**：

```text
au:del_maestro AND submittedDate:[202301010600+TO+202401010600]
```

- 兩端都含端點。
- 這是**提交時間**，不是更新時間；要追蹤更新請改用 `sortBy=lastUpdatedDate`。
- 要「最近三天」這種相對範圍，請自行算出 GMT 的起訖時間再帶入。

## 分類代碼

分類以 `cat:` 指定，常見例子（完整清單見 arXiv Category Taxonomy）：

| 分類 | 領域 |
| --- | --- |
| `cs.AI` | 人工智慧 |
| `cs.CL` | 自然語言處理 |
| `cs.LG` | 機器學習 |
| `cs.CV` | 電腦視覺 |
| `cs.SE` | 軟體工程 |
| `cs.CR` | 密碼學與安全 |
| `cs.IR` | 資訊檢索 |
| `stat.ML` | 統計機器學習（偏理論） |
| `eess.AS` | 語音與音訊處理 |
| `math.OC` | 最佳化與控制 |
| `quant-ph` | 量子物理 |
| `hep-ex` | 高能物理實驗 |
| `astro-ph.GA` | 星系天文物理 |
| `cond-mat.stat-mech` | 統計力學 |
| `q-bio.NC` | 神經與認知 |
| `econ.EM` | 計量經濟 |

- 分類 ID 大小寫敏感：寫 `cs.LG`，不要寫 `cs.lg`。
- 有些分類是別名（例如 `cs.NA` 對應 `math.NA`、`q-fin.EC` 對應 `econ.GN`），
  查詢時兩種可能都會命中，清單請以官方 taxonomy 為準。
- 論文只會有一個主要分類（`arxiv:primary_category`），但可有多個分類
  （`<category>` 元素，含 cross-list）；本 skill 的 JSON 分別輸出 `primary_category`
  與 `categories`。
- 一篇論文的分類清單也可能包含 ACM／MSC 代碼（如 `I.2.6`），它們與 arXiv 分類
  共用 `<category>` 元素，因此會一起出現在 `categories` 裡。

## 分頁與結果規模上限

- `start` 以 0 為基準；第 1 頁是 `start=0`，第 2 頁是 `start=max_results`。
- 單次請求 `max_results` 上限 **2000**；一次呼叫最多回傳 **30000** 筆。
  `max_results` 超過 30000 會得到 HTTP 400。
- 官方建議：超過 1000 筆結果時先收斂查詢，或縮小每次的切片；
  大量 metadata 彙整請改用 OAI-PMH 或 bulk data 介面。

## 回應結構（解析時看這些）

| 元素 | 內容 |
| --- | --- |
| `opensearch:totalResults` | 符合查詢的總筆數（做分頁時看這個） |
| `opensearch:startIndex` | 對應請求的 `start` |
| `opensearch:itemsPerPage` | 對應請求的 `max_results` |
| `<entry>` | 每篇論文一組，0 到多個 |
| `entry/id` | 摘要頁 URL，去掉 `https://arxiv.org/abs/` 就是 arXiv ID（含版本後綴） |
| `entry/title`、`entry/summary` | 標題與摘要（值內含換行與縮排，需正規化空白） |
| `entry/author/name` | 作者姓名，依作者順序 |
| `entry/author/arxiv:affiliation` | 作者機構（選配，常常缺席） |
| `entry/arxiv:primary_category@term` | 主要分類 |
| `entry/category@term` | 分類清單（可能含 ACM／MSC） |
| `entry/published`、`entry/updated` | 首次提交時間與本次版本時間（ISO 8601 含時區） |
| `entry/link` | `rel="alternate"` 是摘要頁；`rel="related"`＋`title="pdf"` 是 PDF；`title="doi"` 是 DOI 連結 |
| `entry/arxiv:doi`、`entry/arxiv:comment`、`entry/arxiv:journal_ref` | 選配欄位，有值才出現 |

排序提醒：**API 的預設排序是 relevancy，與網頁介面的「最新在前」不同**。
要最新論文請用 `sortBy=submittedDate`（本 skill 的 `--sort submittedDate`）。

## 錯誤回應

錯誤也是 Atom feed，但只有一個 entry，其 `id` 結尾形如
`arxiv.org/api/errors#<訊息代碼>`（官方手冊的範例是 `http://arxiv.org/api/errors#...`，
實際可能為 `https://`），`summary` 是人看得懂的說明。常見原因：

| 情況 | 說明 |
| --- | --- |
| `start` 不是整數或為負 | `start must be an integer` / `must be >= 0` |
| `max_results` 不是整數或為負 | `max_results must be an integer` / `must be >= 0` |
| ID 格式錯誤 | `malformed id`（例如把 `2401.1234` 寫成不存在的格式） |
| `max_results` 超過 30000 | HTTP 400 |

腳本會把 error feed 視為失敗（退出碼 2）並在 stderr 顯示 `summary`，
不會把它當成一篇論文輸出。

## 禮貌規範（官方明文）

Terms of Use 對 legacy API（含 arXiv API）的要求：

- **每 3 秒最多一個請求**（`make no more than one request every three seconds`）。
- **同一時間只維持一條連線**（`limit requests to a single connection at a time`），
  也不能靠增加機器來繞過限制。
- 使用可辨識的 User-Agent；不要偽裝成瀏覽器或匿名字串。
- 官方手冊補充：arXiv 一天只更新一次，**同一查詢一天內不會變**，請快取結果，
  不要反覆查詢。
- 官方要求的回饋方式：連回 arXiv 摘要頁（本 skill 的 `abs_url`）而不是自行轉載 PDF；
  metadata 屬 CC0，但 e-print 全文的著作權仍屬作者或出版者。

## 不同來源的分工

- arXiv 本身（本 skill）：結構化檢索、完整 metadata、`id_list` 精確取用。
- Hugging Face Hub 的熱門論文與社群訊號：改用 `hf-cli` skill 的 `hf papers` 子命令。
- 大量 metadata 或全文：arXiv OAI-PMH 與 bulk data 介面，不是本 skill 的範圍。
