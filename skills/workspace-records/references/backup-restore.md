> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時先停止，指出過期段落並交由維護者更新。

# 備份與還原界線

本文件承接 [workspace-records 操作流程](../SKILL.md)，區分 canonical record data 的保存與 Google Docs 原生畫面的完整備份。

## Snapshot 包含什麼

snapshot 從一致的 Google head 開始，沿 root manifest 遞迴取得可達的 immutable objects，包含 canonical records、原始 UTF-8 正文、來源相對路徑與 hash、attachments 的原始 bytes、events 與分頁 operation history。只有開始與結束時的來源 revision/head 一致，且所有引用物件的 size/hash 驗證通過，snapshot 才可標記 complete。0-byte attachment 也須保留為空 bytes，不能省略或改成一般文字檔內容。

這是資料模型層的 snapshot，不代表原生 Google Doc 的完整格式備份。任意 Doc tabs、排版、comments、建議模式、未納入 RecordSet 的畫面內容，以及 Sheet／Drive Project 的格式與歷史都不在此保證內。Sheet 與 Project 是衍生讀取面；其資料應由 canonical head 重建或重新對帳。

## 還原與回復

還原先產生綁定 snapshot、核准目標根、精確路徑和 before/after hashes 的 plan，再核對目標沒有外部變更後套用。metadata catalog 保存穩定 record/attachment IDs、來源相對路徑與原始 hash；原始時間值作 metadata 保留，但目標檔案系統的實際 mtime 會是還原時間。runtime restore 可重建 frozen control、記錄 metadata 與 operation results；還原結果明確保持 `not_activated`。它不會自行切換 registry、啟動 writer 或完成 P6 cutover。

Google head revision 改變、缺少 attachment/object、hash 不符、operation 結果不明或目標在預覽後被修改時，停止還原並交人工處理。中斷的受管套用需以原 plan/journal 對帳，不能重寫已被外部變更的檔案。不可用舊 snapshot 覆蓋新資料，也不可在未知 connector 結果時改用新 operation ID 重送。單一 writer、外部系統沒有待完成的未知寫入，及經核准的隔離目標都是必要條件。

## P4 CLI 與 promotion

來源套件已提供以下受控介面；它們仍須隨 fixed package 部署並經 P6 live writer fence 後，才可用於正式切換：

- `snapshot.py {begin,accept,inspect,finish,git-capture} --input <json-file>`：分階段取得並驗證完整 canonical snapshot。
- `restore.py {preview,apply,runtime-preview,runtime-apply} --input <json-file>`：在核准隔離根還原檔案，或重建保持 frozen 的 runtime。
- `recovery.py --input <json-file>`：以原 request/plan 對帳 P2 partial operation。
- `promotion.py {prepare,apply} --input <json-file>`：核對 snapshot、精確 target 與 pending intent；target 須已 frozen。保存原 canonical files，保留既有 namespace 與 journal，再將已驗證資料導入；control 最後更新。中斷後沿同一 operation 恢復，不改用新 operation ID。
- `transition.py {begin,issue,observe,inspect,resume-git,resume-google} --input <json-file>`：保存 transition intent、階段及 recovery 狀態。
- `route_publish.py {prepare,apply,reconcile} --input <json-file>`：依已核驗來源、target snapshot/receipt、binding 與 version 推進 route；unknown 結果只能先 reconcile 原 operation。

切換順序是 authority freeze、route freeze、writer drain、snapshot 與 target readback、發布 frozen route、恢復 target、發布 steady route。工具保存並核對呼叫者提供的證據，不會自行證明 OS、Runner 或外部 agent 已停止寫入；實體 fence 是 P6 的獨立前置條件。這些 CLI 不代表已部署，隔離 runtime 的成功也不等於正式 pilot 已切換。

以上 snapshot 僅涵蓋 canonical records、events/journal 與被 manifest 引用的 raw objects/attachments。它不備份或還原原生 Google Doc 的任意格式、comments、未納入 schema 的 tabs、Sheet/Project 的視覺格式或檔案系統原始 mtime；原始時間值保存在 metadata catalog。
