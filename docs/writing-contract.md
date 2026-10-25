# 寫作契約

> **讀者**：專案維護者與要套用寫作契約的人；熟悉 Markdown 與 Git，不必讀過設計過程。
> **一句話**：把寫作要求分成「可機械檢查的 8 條」與「僅人工自檢的規則」，並如實寫出兩者的涵蓋範圍。
> **現在要做**：先讀〈8 條機械檢查〉與〈僅人工自檢〉，再依〈不能宣稱什麼〉調整預期。
> **依據**：`skills/writing-contract/references/a-human-docs.md`、`skills/writing-contract/references/b-agent-docs.md` 與 `scripts/validate_writing.py`。

這份契約處理兩類文件。A 類是**本專案列入 allowlist 的敘述文件**；B 類是
**樣式分類的 agent 指令檔**：任何 `skills/*/SKILL.md`、`skills/*/references/*.md`、
根 `AGENTS.md`、`task-brief.md` 與 `pm.md` 都自動納入；目前實際命中 **20 份**。

## 適用範圍（allowlist）

| 類別 | 路徑 |
| --- | --- |
| A 類 | `docs/**/*.md`、`README.md`、`CONTRIBUTING.md`、`SECURITY.md` |
| A 類模板 | `skills/project-workflow/assets/templates/{overview,progress,plan,acceptance}.md` |
| B 類 | `skills/*/SKILL.md`（目前 10 份） |
| B 類 references | `skills/*/references/*.md`（目前 7 份） |
| B 類其他 | 根 `AGENTS.md`、`skills/project-workflow/assets/templates/task-brief.md`、`adapters/opencode/agents/pm.md` |

四個 A 類模板的四欄放在 fenced skeleton 內，因此 A1 的檔案開頭檢查對模板豁免；
模板 skeleton 的四欄由 `tests/scaffold/test_validate_writing.py` 的獨立測試守護，不是既有 `project-workflow` 契約測試。`project-notes/` 的內容**不由本檢查器掃描**；
`--notes-root` 只用於解析
`project-notes/...` 路徑是否存在，不會讀取檔案內容。`adapters/opencode/agents/`
的其他 agent 定義也不在本輪 B 類範圍。

## 8 條機械檢查

| # | 檢查 | 層級 |
| --- | --- | --- |
| 1 | A 類前 40 行內有四欄 blockquote | warning |
| 2 | 四欄不得為空、不得留 `<…>` 佔位符 | warning |
| 3 | 「現在要做」欄不得命中 `fillers.md` | warning |
| 4 | 「依據」欄內反引號樣路徑必須存在 | warning |
| 5 | 整句只有「代號 ＋ 狀態詞」 | warning |
| 6 | 單檔 > 400 行（只套 A 類，檔案層級） | warning |
| 7 | 公開 A 類不得含 commit SHA | warning |
| 8 | B 類第一段缺少優先序句 B1（只比對句首前綴） | warning |

檢查 1 只要求前 40 行內找得到四欄；四欄可分散在不同段落區塊，不要求同一個
blockquote。

檢查 4 只處理「依據」欄內的反引號路徑樣 token；`§`、章節描述、指令與單段
檔名不交由這條檢查。行號後綴 `path:2` 會先剝成 `path`。點開頭的安裝路徑會檢查，
但 `~` 路徑、單段名稱（含通用目錄名）、斜線指令、`actions/` 與 `<…>` 佔位符跳過。

檢查 7 會看反引號、fenced code 與 URL 中的 SHA。例外不限 `uses:` 行：
任何 `owner/repo@<40 hex>` 形態，以及表格列中同一 action 的 40 位 hex 都豁免；
**裸的 40 位 hex 仍會報**，因為那就是 commit SHA；全 0 的空樹值（`0000…`）視為佔位符、不報。同一行其他位置的裸 SHA 也會報。
檢查 8 只比對 B1 的句首前綴；尾巴不同也算通過。

## 僅人工自檢（不進機械檢查）

以下規則保留在 reference 與本節，但**沒有機械檢查**。交付前由人逐條核對。

| 規則 | 涵蓋範圍 | 已知繞過／限制 |
| --- | --- | --- |
| A5 單節條列 > 8、連續散文 > 3 段 | 單一 H2 的結構 | 跨行、嵌套與 diff 行號歸屬不可靠；只能人工看整節 |
| A6 章節下限 | H2 不得只剩標題與狀態詞 | 完整句、只有指令、明寫未知／待查都算合格；判斷需人看 |
| A7 模糊詞 | 正文散文；表格列、章節標題、引號內引用豁免 | 條件型 hedge 需同句有可觀察量或條件才報；表格與標題不查 |
| A2r 引用完整性 | 同一份詳細定義／指令只留一處 | **A2r 已無機械檢查（僅人工自檢）** |
| A4r 縮寫與術語 | 承重術語第一次出現給白話 | 縮寫不由機械檢查；簡體、插入空白、同義詞式註解都抓不到 |
| A9r 不陳述未確認行為 | 不得陳述未經確認的行為（無法從程式碼或結果確認的行為） | 自動偵測不一致的 recall 只有 0.48，弱檢查只會製造噪音 |
| B2 只寫三類 | 指令／慣例／禁止事項，每條附「怎麼驗」 | 語意判斷，無機械檢查 |
| B3 不寫概觀 | 不寫專案概觀與逐檔說明；安全與授權例外保留 | 語意判斷，無機械檢查 |

**「一句話」欄對 A4r 豁免**：它是壓縮摘要，強制展開會牴觸這個欄位的用途。

## 怎麼驗

```text
python3 scripts/validate_writing.py --repo . [--notes-root PATH] [--base REF]
                                     [--baseline PREV.json] [--class a|b|all]
                                     [--format text|json]
```

- `--base` 必須是 commit 物件；空字串是格式錯誤（退出碼 2）；40 位全 0 視為
  CI 首次 push 的空樹。缺席時依 CI 事件、merge-base、HEAD 決定；HEAD 已在
  merge-base 時用 HEAD。
- 新增檔檢查全文；修改檔只檢查 `git diff -U0 <base>` 的新增行。被刪的檔不再檢查，
  rename 的新路徑視為新增。**單檔 > 400 行是檔案層級，修改檔也會報。**
- 既有公開 A 類檔在全檔掃描時可能出現存量命中（例如沒有四欄、README 超過 400 行）；
  這是**預期行為**。CI 的 diff 範圍只會報被改動的檔。
- 沒有 `--baseline` 時，所有命中都標 `new`。有 `--baseline` 時輸出
  `new`／`persisting`／`resolved`／`out-of-scope`；本次 base 與 baseline 記錄的
  base 不同時，上次有、本次不在範圍內的項標 `out-of-scope`，不計入收斂。
  只有 `new` 為 0 且沒有 `out-of-scope` 才視為收斂。
- 所有文件問題都是 warning，不阻擋。退出碼只表示檢查器是否完整跑完。

| 退出碼 | 意義 |
| --- | --- |
| 0 | 完整檢查（可以有形式問題） |
| 1 | 檢查器自身規格或輸入格式錯誤 |
| 2 | 未完整檢查（repo、base、fillers 或檔案編碼有問題） |

## 輸入表與清單

| 檔案 | 角色 | 是否影響退出碼 |
| --- | --- | --- |
| `skills/writing-contract/assets/fillers.md` | 檢查 3 的輸入 | **缺檔或空表 → 退出碼 2** |
| `skills/writing-contract/assets/terms.md` | A4r 人工自檢清單 | 否；缺失只提示 |
| `skills/writing-contract/assets/vague.md` | A7 人工自檢清單 | 否；缺失只提示 |
| `skills/writing-contract/assets/familiar-terms.md` | 哪些縮寫免展開的人工清單 | 否；缺失只提示 |
| `skills/writing-contract/assets/duplicate-allowlist.md` | 跨檔重複句的人工白名單 | 否；缺失只提示 |
| `skills/writing-contract/assets/LICENSE.zh-tech-writing` | 上游 MIT 許可全文 | 否 |

只有 `fillers.md` 是機械檢查的輸入；其他清單是給人看的，空表不會被寫成
「檢查已通過」。詞表沒有命中等於沒有檢查到，不等於文字沒有問題。

## 本專案自訂門檻

| 門檻 | 數字 | 性質 |
| --- | --- | --- |
| 單檔行數 | 400 行 | 機械檢查 6；**本專案自訂慣例，無文獻依據** |
| A5 單節條列 | 8 條 | 人工自檢參考；**本專案自訂慣例，無文獻依據** |
| A5 連續散文 | 3 段 | 人工自檢參考；**本專案自訂慣例，無文獻依據** |
| A4r 註解長度 | 6–120 字 | 人工自檢參考；**本專案自訂慣例，無文獻依據** |

## 中文門檻

| 項目 | 門檻 | 出處 |
| --- | --- | --- |
| 逗號分句 | 以 20 字為目標；超過 30 字就拆開 | `leter/zh-tech-writing`（MIT） |
| 紅線 | 多於 40 字的中文句子不接受 | 阮一峰《中文技术文档的写作规范》；香港律政司《法律草擬指引》9.1.4 |
| 整句 | 不超過 100 字 | `leter/zh-tech-writing`（MIT） |
| 段落 | 最佳 ≤4 行、最多 7 行 | `leter/zh-tech-writing`（MIT） |
| 長度建議 | SKILL.md 500 行、CLAUDE.md 200 行 | Agent Skills 規格；Claude Code 官方文件 |

「30–39 字須語義明確」屬語意判斷，機器做不到，只列為寫作建議。中文門檻是
社群規範加一條官方數字，不是國家標準；引用時要附出處，不要寫成「研究證明」。

## 五種文件異味

| 異味 | 可指認的例子 |
| --- | --- |
| bloated（冗長離題） | 一段 80 字能講完的變更說明，被擴寫成含背景、動機與願景的三段 |
| fragmented（資訊散落） | 同一個安裝步驟分別寫在 README、維護指南與 skill 主文，三處版本不同 |
| tangled（敘述糾纏） | 一段同時講安裝、授權與回滾，讀者無法判斷哪一句屬於哪個動作 |
| excess structural info（樣板壓過內容） | 每個小節都套「目標／範圍／風險」三個標題，但內容只有一句話 |
| lazy（描述不足） | 章節只有「M3 完成」或「見程式碼」 |

這些例子是本專案的設計判斷，不是論文發現。文獻只提供五類異味的名稱與出現頻率；
例子的用途是讓審查者能指認，而不是當成統計證據。

## 優先序與衝突

規則優先序是 **A6 > A5 > A1 > 其餘**；衝突時以優先序靠前者為準，並在改版紀錄
寫下「哪條規則因為哪條而讓位」。

| 對 | 裁決 |
| --- | --- |
| A1 × A2r | 開頭摘要、結論段、TL;DR 不算重複定義 |
| A1 × A4r | 「一句話」欄豁免 A4r 的展開要求 |
| A1 × A5 | 四欄是 blockquote，不計入條列數 |
| A3 × A4r | 註解可以放在下一句；A3 只要求同句有述詞 |
| A4r × A5 | 縮短時不得刪掉白話註解 |
| A4r × A7 | 術語註解是定義上下文，不套用模糊詞黑名單 |
| A5 × A6 | 兩者都是觸發器；A6 優先，寧可補內容，不可直接刪 |
| B1 × B3 | 安全例外與「安全邊界以本檔為準」是同一件事的兩面 |
| B2 × B1 | B1 的回報要求就是 B2「怎麼驗」的一種 |

## 不能宣稱什麼

1. **通過檢查不等於好文件。** 檢查器只證明 8 條形式規則沒有命中；文件是否正確、
   是否適合讀者，仍要人工判斷。
2. **不得宣稱可讀性變好。** 可讀性公式量不到內容選擇、組織與讀者適配
   （Redish 2000；ISO 24495-1:2023 明文排除公式）。本契約沒有做讀者測試。
3. **不得宣稱機械檢查能保證內容正確。** AI（人工智慧）生成的註解有 2.7% 整段錯誤
   （arXiv:2408.14007）；語意錯誤不在形式檢查的範圍。
4. **LLM judge 不能當放行關卡。** judge 偏長、會高估遵循率
   （arXiv:2404.04475；arXiv:2509.21051）；它只能當第二意見或候選清單。
5. **未掃到的歷史違規不算沒有。** 檢查器只查新增檔與被改動的行；既有檔案的
   歷史違規不在報告內。全檔掃描時會看到存量，那是存量，不是本輪新增。
6. **規則數不是越多越安全。** 可同時滿足的規則從 5 條增加到 10 條時，遵循率從
   0.574 掉到 0.213（arXiv:2509.21051）。本契約沒有 A/B 實測，也沒有在
   agent runtime 實測遵循率。
7. **只覆蓋 8 條列出的形式層。** 語意重複、同義改寫型重複、跨行拆開的重複句、
   表格與標題中的模糊詞、縮寫展開、章節太空、複合名稱中的術語，都不在機械檢查範圍。

## 研究說明與限制

| 限制 | 說明 |
| --- | --- |
| 預印本比例高 | 多條依據來自 2026 年的 arXiv 預印本，未必有同儕審查或獨立複現 |
| 小樣本 | 部分研究的受試者只有 7 到 21 人，效果量不穩 |
| 文體錯配 | 有些研究測的是程式問答、程式註解或短指令，不是長篇技術文件 |
| 不可外推 | 模型世代、語言與任務都不同；本契約不宣稱同一數字會出現在其他環境 |
| 沒有 local 實驗 | 本契約沒有跑 A/B 實驗，也沒有量測真實讀者的理解或任務完成率 |

因此，本契約只能承諾「8 條形式規則可重複檢查」，不能承諾「文件品質已被證明變好」。

## 出處與授權

- `leter/zh-tech-writing`（MIT，Copyright (c) 2026 jacklondon，
  <https://github.com/leter/zh-tech-writing>）：句子與段落門檻、黑話詞列。
  上游許可全文見 `skills/writing-contract/assets/LICENSE.zh-tech-writing`。
- `terms.md` 的 11 條黑話詞取自上游黑話清單（MIT）；`fillers.md` 與 `vague.md`
  是**專案自擬**，參考同類公開清單的分類，但不是逐條翻譯。
- 阮一峰《中文技术文档的写作规范》（<https://github.com/ruanyf/document-style-guide>）：
  20–29 可接受、30–39 須語意明確、多於 40 字不接受；縮寫先展開。只引用短句。
- 香港律政司《法律草擬指引》2012 第 9.1.4 段：中文句子以連續 40 字為限。
