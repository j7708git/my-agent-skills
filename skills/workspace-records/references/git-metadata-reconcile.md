# 已推 Git 原始位元組的 metadata 對帳

`git_metadata_reconcile.py` 是既有 managed Git catalog 的 PM 維護入口。它只更新明列 record 的 `source.content_sha256`，保留 stable IDs、建立／更新時間、original_time、metadata、relations、附件與既有 journal；不寫 notes、不 initialize runtime、不變更 route。此維護不代表原文字內容經人工核准，PM 仍須審查精確 scope 與來源內容。

## 前置條件

固定正式 registry 中的專案必須是 managed、git_local、steady，control 為 required-v1，且沒有其他未完成操作或 activation barrier。外部 writer 必須先由操作者停止／排空；本機 registry→runtime 鎖只協調合作中的受管 writer，不證明跨機排他性。

來源必須是目前 notes 預設分支的已推 HEAD：fresh remote HEAD、local HEAD 與 request 的 source_commit 一致，完整 catalog 路徑集合與本機 inventory、該 commit 的 Git tree 完全一致。每份來源雙讀原始 hash，並以 Git builtin clean blob oid 核對 commit；這容許 Git 管理的混合 EOL，但拒絕 external filter、ident 與 working-tree-encoding。不改寫原始位元組。

目前 scanner 要求來源可 UTF-8 解碼；空附件可用，非 UTF-8 二進位附件 fail closed。不得把本輪文字／空附件驗證外推成任意二進位來源支援。

## PM request 與 CLI

使用固定安裝包中的入口，workspace 由既有 routing discovery 決定。下例僅示意相對檔名：

```text
python git_metadata_reconcile.py preview --managed --input request.json
python git_metadata_reconcile.py apply --managed --input approved-plan.json
python git_metadata_reconcile.py inspect --managed --input operation.json
python git_metadata_reconcile.py recover --managed --input approved-plan.json
```

preview 輸入為 `{"request": {...}}`，request 精確欄位：

- `schema_version: 1`、固定 `project_id`、全新 `operation_id`、`actor_role: "pm"`。
- `expected`：正式 `registry_sha256`、control 原始檔 `control_sha256`、catalog 原始檔 `catalog_sha256`、已推 `source_commit`。
- `records`：每筆明列 `record_id`、專案內 `path`、`expected_catalog_content_sha256` 與 `current_content_sha256`；不得多選、漏選或包含未變更項目。

preview 會掃描完整 current source 與候選 metadata，產生 plan/hash；不改 catalog。PM 核准後，apply 輸入為 preview 回應 `data.plan`／`data.plan_hash` 加相同 `request`，即 `{"request": ..., "plan": ..., "plan_hash": ...}`。不要直接把整份 CLI envelope 當輸入。inspect 輸入為 `{"project_id": ..., "operation_id": ...}`。

## 持久狀態與中斷

apply 在同一鎖內重新 preview，比對完整 plan，先寫 durable `accepted` journal，再原子替換 catalog，記錄 `metadata_done`，更新 control version（epoch 不變），記錄 `control_done`。實際 catalog/control/source 與完整 inventory、原 journal 均核對後才寫 `complete`。所有來源檔的 hash 保持不變。

中斷後普通 apply 回 `MANUAL_REVIEW`；其他 managed writer 被既有 incomplete operation gate 擋住。先 inspect 對帳，確認 catalog/control 只在計畫的 before/after 狀態、來源仍是相同已推 commit，再由 PM 對同一 plan 明確 recover。它不自動重試、不 rollback、不接受第三方 catalog/control 變更；來源 commit 前進也拒絕，需要先人工處理原 pending 操作。`complete` 的重送只回 historical receipt，不宣稱當前重新驗證。

這個 helper 不執行 freeze、snapshot 或 Google 切換。完成後應以既有完整 inventory→freeze／writer 排空→snapshot→正常遷移流程繼續；隔離 copy 的 snapshot 成功也不等於 live writer 排他性或 Google Runner 驗收成功。

完整 maintenance plan 含本機 binding，固定留在 runtime 的 metadata-reconcile-plans 子目錄，以 hash 與根 operation journal 綁定；根 journal 只保存已掃描的相對 scope、catalog／控制審計資料與 plan hash。完整 journal 可隨既有 snapshot/history 遷移，但本機 plan 不輸出到 Google。復原需要保留這份 local plan；缺檔或 hash 不符即拒絕，不從歷史快照猜本機路徑。
