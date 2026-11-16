# 混合來源 portfolio 候選

`portfolio_mixed.collect_mixed(registry, events, reader_factory)` 讀固定 registry，Git 專案做前後兩次讀取；Google 專案只有收到 canonical receipt event 才啟動 `CanonicalReader`，重讀兩次 Doc head，驗 manifest/status bytes/hash、summary、epoch、version、revision 與 receipt 對應 operation/result。reader 由同一受管 `CodexActionSession` 注入，工具回執是實際 App 結果，不是模型最終文字。

未收到 Google 事件時只讀上次已驗證的本機 cache，不啟動 reader；列的 freshness 顯示 `last_observed@<時間>`，不是目前線上狀態保證。沒有 cache 或讀取、receipt、route、head/manifest 不一致時顯示 unknown，該事件保持 pending；不從舊 Git 讀 Google 專案。Git 專案仍雙次讀取並獨立提供來源。登記缺失、binding 變更或 Git 雙讀競態會拒絕本次收集。

`source_digest` 綁定 registry、內容、來源 epoch/version/hash，但不含觀察時間、native revision 或工具 proof id；`coverage_digest` 綁定該 digest 與本次 covered/pending event hashes。`desired_rows` 維持既有 14 欄與每列全域 source_digest，Google 的 `source_time` 是 canonical status 的 updated；freshness 才是觀察時間。這可能因全域 digest 更新多列，不能宣稱只改一列。

## Outbox 與事件

`MixedOutbox` 以來源 provider 與 14 欄 projector 接入原 Outbox；本地 probe 用 Git 雙讀與已驗 Google cache，沒有事件或來源變化時不開 session。canonical event 先排隊，再由 `MixedSession` 在同一 `CodexActionSession` 使用 `get_document`、`fetch`、`get_spreadsheet_cells` 和 `batch_update_spreadsheet`；也可共用已存在且允許這些工具的 `ActionBroker`。保留原 all-Git `collect_git` 與 Outbox 預設行為。

plan 綁 source digest、coverage digest 與 covered/pending event hashes，begin 前再次核來源；Sheet readback 確認同一 plan 後僅 settle 其已驗 covered event。刷新期間新進或尚未覆蓋的事件留待後續；結果未知先 reconcile，不能盲重送。`managed_outbox(checkpoint)` 從固定 workspace 的 portfolio 與 Google 專案設定載入位置，事件內容不能覆寫設定；mixed 的固定狀態目錄為 `runtime/portfolio-refresh-mixed`，與 all-Git 的 `runtime/portfolio-refresh` 分開。

## 啟用 mixed 前

1. 在既有 Runner 的同一 concurrency 內，停止並確認 all-Git consumer 已退出；核對其 outbox 無 pending／unknown，所有待處理事件均已 settle。任何未解項就停止並對帳，不搬移、抹除或覆寫舊狀態。
2. 僅在上述條件成立後，切換唯一 active consumer 到 mixed；維持同一 Sheet 的合作單 writer。新狀態目錄不得繼承 all-Git 的 last_success，先對相同 Sheet 做 fresh read 並驗目標、headers 與行識別，再建立 mixed 確認紀錄。
3. 保留 all-Git 狀態供稽核與回復；回切前對帳 mixed 的 pending／unknown 與在途工作。這些狀態都是衍生投影的控制紀錄，canonical authority 仍由 per-project registry 決定。

目前測試為合成 broker、Docs、manifest 與 Sheet；尚未對混合路由做真 Google／Sheet 驗收。固定 v3 package 與 live Sheet 未改，部署 mixed 須另建固定包並實測。
