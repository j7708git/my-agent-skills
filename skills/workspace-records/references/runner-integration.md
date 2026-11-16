> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。

# 既有 Runner 與受管紀錄整合

本文件適用於將既有 Issue／Actions 派工入口接到 `workspace-records` 的 Windows writer。沿用原收件鏈、人工 ready gate 與 Runner concurrency；工具負責精確紀錄操作，PM 負責理解需求及審核 scope。模式與核心紀錄語意見[路由](routing.md)及[資料契約](contract.md)。

## 部署與事件入口

固定 package 提供 `runner_event_adapter.py`、`dispatch_guard.py`、`canonical_writer.py` 與 `codex_action_transport.py`，並依賴原 managed connector／Git store／scanner。來源檔存在不代表已安裝；部署者須核准並固定完整 package、設定與原 workflow 變更。

固定的 `runner.json` 可選配 `client.codex_executable`，內容只能有 `path`（絕對路徑）、`sha256`（64 位小寫 hex）與 `size`（正整數）。這是部署者核准的主機設定，不能從 Issue、request 或環境變數指定。每次 session factory 建構前重驗檔案及祖先無 symlink／reparse、檔案為一般檔且無 hardlink alias、精確 size/hash；不存在或漂移就拒絕。不設定 `client` 時保留原本 PATH 發現行為，不修改全域 PATH。驗證後透過既有 `CodexActionSession(executable=...)` 傳入；tool metadata、readOnly、精確參數及 approval gates 不變。建構成功不等於事件執行或寫入成功。

既有 Actions job 呼叫：

```text
python <fixed-package>/skills/workspace-records/scripts/runner_event_adapter.py --managed --disable-notifications --route-only
python <fixed-package>/skills/workspace-records/scripts/runner_event_adapter.py --managed --disable-notifications
```

第一條回傳固定分流結果，第二條只執行新版受管路徑。兩者都從原 Actions 環境與 `GITHUB_EVENT_PATH` 讀事件，再由已登入的 `gh` 重新讀取該 run 與精確 Issue／命令留言。repo、actor、workflow path、run attempt、SHA、Issue number/id/state、ready／owner `/run` 必須吻合；Issue 的文字與 marker 不認證 owner、ready 或 authority。讀取 GitHub 失敗時拒絕啟動 writer。

固定分流如下：

- 已登記 Tasks intake marker，或具有 PM operation catalog mapping 的原生 Issue：新版受管入口。
- `legacy_git_issues` 明列的其他 Issue，且 fresh route/control 證明仍是 Git：由原 workflow 保留既有客戶端；新版 adapter 不執行它，也不把其 exit code 當 canonical receipt。
- 未知專案、Google 專案的 legacy 路徑、frozen／activation barrier 或含糊的全批次：拒絕，等待 PM 明確範圍。

opened／owner `/patrol` 對應巡視，ready label／已核對的 owner `/run` 對應 execute。manual workflow_dispatch 需明確 Issue 及 patrol／execute。新版 patrol 讀取 canonical 內容並保存 read receipt，尚未自動取代既有 PM 提案留言與 Project 編輯流程。

此入口沒有通知 transport；`--disable-notifications` 必填。整合驗收須讓該批事件留在此禁通知路徑。原 workflow 的其他通知行為由原部署維護，不由本 helper 重設。

## 固定設定

設定固定為 `.openchamber/workspace-records/runner.json`。其中 `repository` 與 `owner` 是部署者指定的 GitHub locator，與後端 authority 分開；文件 ID 仍只從正式 registry 取得。

```json
{
  "repository": "<owner>/<notes-repo>",
  "owner": "<owner>",
  "workflow_path": ".github/workflows/idea-kanban-trigger.yml",
  "whitelist": {
    "<intake_key lowerhex64>": {
      "project_id": "<project>", "mapping_generation": 1,
      "issue_number": 123, "payload_sha256": "<payload SHA-256>"
    }
  },
  "projects": {
    "<project>": {"google": {"head_tab_id": "<head-tab>", "summary_tab_id": "<summary-tab>", "object_folder_id": "<object-folder>"}},
    "<git-project>": {"google": null}
  },
  "reviewed_operations": {
    "123": {"project_id": "<project>", "plan_sha256": "<PM bundle hash>"}
  },
  "legacy_git_issues": {"456": "<explicit-git-project>"},
  "portfolio_projection": {"enabled": true, "sheet_title": "<fixed source-tab title>"}
}
```

`legacy_git_issues`、`reviewed_operations` 與 `portfolio_projection` 為選配。缺 operation catalog 時 execute 回 `PM_REVIEW_REQUIRED`；缺 projection 設定時保留 canonical receipt 並回 projection pending。Google source 與總表的部署前提見[混合模式總表](portfolio-mixed.md)。

Tasks marker 使用唯一 `workspace-intake-v2` JSON fence，exact keys 為 `{schema_version,project_id,mapping_generation,source,intake_key,payload_sha256}`；拒絕重複 marker／JSON keys。`source` 固定 `google_tasks`，`intake_key` 是 UTF-8 `JSON.stringify([tasklist_id,task_id])` 的 SHA-256；payload hash 是 UTF-8 `JSON.stringify({title:cleanTitle,notes:trimmedNotes})`，鍵依 title、notes 排列。欄位與固定 whitelist、實際 Issue title/body 都要吻合。原生 Issue 可由 PM catalog 明確綁專案，不需偽造 Tasks marker。

## PM 如何產生可執行 bundle

先由 PM 讀取最新 Issue／留言及 current canonical records，確認目標、角色、scope、完整新內容、每個 base hash 與 expected mode/phase/epoch/version。這段自然語言轉譯與範圍裁決目前是 PM 工作，不是本 adapter 的自動 planner。最新留言的語意仍由 PM 審查；title/body hash 不代表已自動理解所有留言。

PM 將已審核 CoreRequest 存成固定 `runner-operations/` 內的草稿 JSON。CoreRequest 沿原 schema，包含 `request_id,project_id,actor_role,scope,operation_id,action,target_record_id,payload,expected`；例子：

```json
{
  "schema_version": 1, "request_id": "<request-id>", "project_id": "<project>", "actor_role": "worker",
  "scope": {"project_id": "<project>", "actions": ["replace"], "records": [{"record_id": "<record-id>", "path": "tasks/<task>.md", "fields": ["body"]}]},
  "operation_id": "<stable-operation-id>", "action": "replace", "target_record_id": "<record-id>",
  "payload": {"content": "<reviewed UTF-8 content>", "base_sha256": "<actual source hash>"},
  "expected": {"mode": "google", "phase": "steady", "epoch": 1, "version": 1}
}
```

在固定 package 執行 PM 包裝入口：

```text
python <fixed-package>/skills/workspace-records/scripts/runner_event_adapter.py --managed --disable-notifications --prepare-operation 123 --request-file reviewed-request.json --review-id <review-id>
```

`--request-file` 相對於固定 `.openchamber/workspace-records/runner-operations/`；先由部署者建立安全目錄。helper 重新 GET 該精確 Issue、驗證 registered project／mode、掃描完整候選，才保存 immutable `<plan_sha256>.json`。它不修改 runner.json、不授予 ready、不寫 canonical，也不自行刷新 request 的 base／expected 去通過衝突。

成功輸出 `catalog_entry`。PM 檢視 bundle、實際 scope/diff 與 scanner 結果後，將該 entry 登記到固定 runner.json.reviewed_operations，再由 owner 加 ready 或送 `/run`。Issue title/body 改變或 bundle/hash 不符時執行拒絕；保留舊 hash 檔供對帳。新紀錄的 `base_sha256:null` 只用於已核准的新目標。

Bundle exact shape 為 `{schema_version:1,review_id,project_id,issue_number,issue_content_sha256,request}`；`plan_sha256` 是 `core.object_hash(plan)`，不是 pretty JSON 檔的 bytes hash。`issue_content_sha256` 是 `{title,body}` 的同一 canonical hash。也可由受信 PM 呼叫純函式 `build_operation_bundle(fresh_issue,reviewed_request,review_id)`，再按相同私有路徑保存；它只打包，不代替 fresh Issue 讀取或人工審核。

replace／append／status_patch／batch 沿既有 role/scope 契約；status/log 仍 PM-only。freeze／resume／authority 切換維持正式 transition 流程。這是一般受管紀錄派工介面，不會替任意程式碼需求生成實作、修改程式碼 repo 或自動執行整個舊 PM 工作。

## Claim 與真實回讀

adapter 先查已存在的來源事件 receipt。相同事件及 action 不再次啟動 writer；patrol 與 execute 分開。新 claim 使用既有 SQLite durable ledger；Google 的 prepare 和 claim 各經 `CanonicalReader` 在同一 Apps runtime 重新 get_document，核 actual tool proof、route、Doc revision、control epoch/version 與 head hash。呼叫者填的 authority_state JSON 或模型 final text 不能取代這次工具結果。

execute 只接受已審的同一 bundle。claim 後的私有 intent 保存 review_id、operation plan hash、operation/request hash；同一事件不能改 bundle 偷換工作。

Git 使用 managed preview/apply，完成後重新讀取實際目標 bytes、operation journal、control 與相關 metadata。Google 使用原 connector_protocol 的 staged intent、immutable upload/raw fetch hash/size、manifest、唯一 Docs requiredRevision CAS、fresh head/current graph 及原 operation reconcile。上傳使用 helper 已掃描的本機 file_uri；回讀使用完整 b64_string bytes，hash/size 不符或只取得未驗 file_uri 時拒絕。

只有本機 claim complete、對應 durable writer receipt 及其 hash 都吻合，才輸出 `canonical_confirmed`。Apps runtime 的 actual tool name/arguments/thread/turn/item、write approval correlation 與 cleanup 都需通過；process exit0、模型敘述或 Sheet projection 不足以完成 canonical 工作。

Google canonical receipt 固定保存於 `.openchamber/workspace-records/canonical-receipts/<receipt_sha256>.json`，包含：

`{schema_version,project_id,operation_id,request_hash,result_sha256,operation_epoch,operation_version,observed_head_hash,observed_head_epoch,observed_head_version,observed_revision,observed_at}`

原 operation 與觀察到的 head 版本分開；同 epoch 且 observed version 不早於 operation。receipt hash 綁完整 metadata；它是之後 fresh head/history 對帳的提示，不是另一份 authority。

## 未知結果與總表事件

upload 結果未知時保留原 sending intent／可能的 orphan，停止自動重送。CAS 後回應未知則依原 operation、fresh head/manifest/history 及原 reconcile-cas 對帳；未確認前不換 ID 或標 complete。guard pending/unknown 不因重新投遞而啟動第二次 canonical writer。人工處理依原[契約](contract.md)，不用清除 ledger 掩蓋未知結果。

Google canonical 完成後，固定 consumer 以正式 repo/notes locator 觀察 trusted pushed-Git checkpoint，呼叫 mixed outbox 的 canonical_receipt queue；同 broker 可再完成受管總表更新。Git checkpoint 事件接在原 workflow 的 push(paths projects/**) 分支，保持原 concurrency，該分支只跑 projection。詳見[混合總表](portfolio-mixed.md)與[事件刷新](portfolio-refresh.md)。

沒有新資料或待處理事件時先在模型外 probe，不建立模型 session。projection 失聯、未知或未配置時，保留 canonical complete 與 receipt，另回 projection pending/error；derived view 不能回滾已確認的 canonical commit，也不能宣稱其已更新。

## 可宣稱的驗證範圍

部署者分開記錄 synthetic protocol tests、實際 Apps read/upload/CAS、真 Actions event、通知副作用與各 client／OS 結果。成功 read 或 upload probe 不代表 Doc CAS／原 Runner 端到端已驗；來源工具、候選 workflow 與本文件也不代表 live deployment。正式接受需以一個已核准 Issue 經原事件鏈取得 actual canonical receipt/readback，再核總表事件及重送／故障案例。


## 可讀詳情、folder/index 與衍生恢復

Google canonical confirmed 後，Runner 使用 `detail_projection_driver.refresh_details(registry, reader, fixed_mapping, project_id, canonical_receipt_sha256)` 更新可讀 Markdown 與索引；`fixed_mapping` 精確為 `folder_id`、`archive_folder_id`，來自已審核 Runner 設定，不接受 Issue 指定。設定可選欄位為 `detail_projection: {"project-id": {"folder_id": "...", "archive_folder_id": "..."}}`。未設定時明示 detail_projection_pending，不宣稱可讀詳情已同步。

Driver 重用 detail_projection 的 prepare/begin/accept；保留原 canonical operation_id／claim，並以 durable canonical receipt 綁定 fresh head/current graph 中的 request/result/epoch/version。衍生 projection operation_id 為 `detail-` 加 receipt hash 與 fresh head hash 的 deterministic SHA256，讓同一 canonical receipt 在較新 generation 下可恢復展示，不重做 canonical。它先將索引標更新中，再逐份核對並更新 Markdown，最後發布 current 索引。既有 Markdown ID 保持不變，縮減分段依既有 protocol 留 tombstone 並移至 archive。它不把 Drive 搜尋索引更新速度當成已確認：search_index_freshness 仍 unknown。

工具讀集合增加 `get_file_metadata`，寫集合增加 `update_file`，同時沿用 get_document/fetch/upload_file。每份既有檔案先以 actual metadata 核精確 ID、MIME 與父資料夾，再 raw fetch 驗 bytes/hash；目前 connector 真實回傳為正規化 `id,mime_type,parent_ids`，雖請求 trashed 欄位仍省略，所以 trash 狀態明確保留 unknown，顯式 true 一律拒絕，絕不從 source_visibility_status 推論 false。這個接點無法證明檔案未在垃圾桶；正式雲端專案可見性須另做真 UI 驗收。begin/accept 的 observed_parent_id 只取真讀回，不能複製 mapping 冒充。所有 broker 結果須有 exact arguments、actual completion 與寫入對應 approval proof。每一階段重新讀 canonical head，最終還會重新讀所有檔案 bytes/parents 與來源 generation，才產生獨立 detail receipt。

Canonical receipt、detail receipt、mixed 總表回執分開保存。任何投影失敗都保留 canonical confirmed，不重跑 canonical 寫入。一般相同事件重送仍為 duplicate_event；PM 可明確執行：

```text
python runner_event_adapter.py --managed --disable-notifications --recover-projections <原 claim_key>
```

此入口讀既有 Runner receipt、核 complete claim 與 canonical receipt，沿固定 project mapping 重新查目前 authority，只恢復 detail/mixed 衍生副本；不 prepare/claim canonical，不換 canonical operation_id；僅依已核 generation 選定衍生 projection intent。它和首次投影共用同一 detail-storage single-writer lock，並行恢復回 IN_FLIGHT。若原 claim 未完成、authority 已退回 Git、舊 unknown 的 remote ID／hash 無法對帳，維持人工對帳，不以歷史 Doc 冒充目前來源。

發送前 durable intent 先標 unknown。已知 remote ID 可 raw fetch 精確 after hash/parent 後 accept，不能重送；create 結果未知且沒有 ID 時回 MANUAL_REVIEW，禁止廣域搜尋猜檔或盲目再 upload。投影中的 canonical head 前進時不將舊 generation 回報 current。這些是合作 writer 的本機序列化保障，不宣稱跨機／惡意 owner 排他性。

目前 driver 合成測試與真工具 metadata 唯讀探測只能證明候選接點；必須在正式試行專案的 fresh Google authority 下，由真 Runner 寫入並讀回新內容、folder/index 與 mixed 總表，再正常退回 Git，才能算整合驗收。歷史 frozen Doc 保留為快照，不由此入口變更。
