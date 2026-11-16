> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# workspace-records 契約

本文件承接 [SKILL.md](../SKILL.md)，定義 canonical records、authority 與寫入界線。managed mode 依固定 workspace registry 路由；Google mode 的 head CAS 與不可變資料物件屬同一 authority，衍生閱讀副本不取得寫入權。

## RecordSet

`RecordSet` 包含 `schema_version`、`project_id`、`control`、`version`、`records`、`events`、`journal` 與 `attachments`；machine-readable schema 見 [record-set.schema.json](../assets/record-set.schema.json)。

每筆 record 帶穩定 `record_id`、`kind`、建立／更新時間、`source`、`body`、`metadata` 與 `relations`。`source` 保存相對來源路徑、原始內容 SHA-256 與原始時間；`body` 保留原始 UTF-8 內容，包括 Markdown 表格與工具不管理的段落。status record 遵守既有 validator：`state` 為 `planning|active|paused|blocked|done`，`needs` 為 `none|user|external`，`updated` 必須含時區，`code_dir` 必填；`blocker` 依 validator 的必填規則處理。

未知原始 sections 與 headers 仍可讀取並保留。工具不能安全解析未知 managed header 時拒絕 mutation；不從 parsed fields 重建整份檔案，也不丟棄未識別內容。

權限分工：`status.md` 與 `log/*.md` 寫入限 `pm`；`worker` 只能改派工明列 scope 的 task、research、acceptance 等記錄；`reader` 唯讀。角色是協作 policy check，不是身份驗證或硬性 ACL。

## Scope and change planning

每個 request 指定單一 project 與明確 actions、record IDs、paths 和 fields；不接受 wildcard。Preview 綁定候選檔案集合、base content hashes、要求變更與 privacy scan result。Apply 只接受同一 plan 與對應 plan hash。套用前檢視完整 diff。

Record operations 使用穩定 `operation_id`。business request hash 只涵蓋以下欄位：

`{schema_version, project_id, action, target_record_id, actor_role, expected:{mode,phase,epoch,version}, payload, scope}`

project 層操作的 `target_record_id` 固定為 `null`。`request_id`、`operation_id`、整份 source snapshot、scanner receipt 與 Google native revision 不進 business hash。相同 operation ID＋相同 hash 回傳原 logical result；相同 ID 換 hash 拒絕為 `ID_REUSE`。

## Local Git writes

Git adapter 保留來源樹，只變更核准 scope 中的路徑；單檔以 atomic replace。多檔操作需 project-scoped cooperative lock、每檔 durable before/after hashes 與 operation journal；中斷後若檔案已被外部改動，列 conflict 並拒絕覆寫。這是同機合作機制，不是 distributed lock。未遷移 Git 專案不依賴 Google；已設定專案的 control state 無法讀取時 fail closed。

Windows 路徑需 canonicalize 後再做 scope／containment 比對；大小寫、尾點、尾空格、保留名稱、alternate data stream 等別名不得繞過權限或 status schema 檢查。`STATUS.md` 與 `status.md` 在不區分大小寫的檔案系統可能指向同檔，必須作為同一目標處理或拒絕非 canonical spelling。

## Privacy and output

Preview 與 apply 必須完整掃描實際送出的內容。scanner 使用 repo `repo-privacy-audit` 實作、Gitleaks 與 caller 明確提供的 policy/configuration。缺相依、缺 Gitleaks、掃描 incomplete、hash mismatch 或發現問題時都拒絕操作；不可繞過 scanner 或以空結果代替缺漏。個資 policy 不是完整 PII detector，敏感正文仍需人工檢視。

CLI 以 UTF-8 JSON 作輸入，並在 stdout 輸出單一 ASCII-escaped JSON envelope；診斷訊息不進 stdout。正式 managed 入口為 `skills/workspace-records/scripts/workspace_records.py`，命令為 `initialize|resolve|inventory|read|preview|apply|reconcile|recover --managed --input <json-file>`；省略 `--input` 時從 stdin 讀取。`--managed` 從固定 registry 取得 project/runtime/privacy policy，不接受 request 改寫這些位置。`initialize` 僅允許 PM 明確初始化 `unmigrated_git` 專案；`preview` 與 `apply` 需使用相同 request、plan、`plan_hash` 及 routing binding。`recover` 只處理已接受且完全相同的 plan；不手改 journal，也不把新的寫入包裝成恢復。

### P2 工具介面

Request 包含 `schema_version`、`request_id`、`project_id`、`actor_role` 與精確 `scope`。寫入另需 `operation_id`、`action`、`target_record_id`、`payload` 及 `expected:{mode,phase,epoch,version}`。`status.md` 與 `log/*.md` 是 PM-only；worker 只能寫派工明列 scope。role 是合作政策檢查，不是身份驗證。輸入錯誤、衝突、禁止、control/scanner unavailable、partial 或 manual review 會以不同錯誤碼與非零退出碼回報；具體分類以 CLI 與測試為準。

managed mode 固定定位 `.openchamber/workspace-records/routes.json` registry、`.openchamber/workspace-records/runtime` runtime、`.openchamber/repo-privacy-policy.json` policy，以及已部署的 skill package。`routing.py` 的 `bind|check|prepare-transition` 檢查位置／路由並產生轉換提案。部署後由 `transition.py`、`promotion.py` 與 `route_publish.py` 執行受管 staged transition；只有全部來源、snapshot、target 與 binding 證據吻合才可發布下一階段。未遷移且 registry 明確登記為 `unmigrated_git` 的專案可在 Google 不可用時使用 local Git；managed Google route 無法讀取或核對時必須拒寫，不能 fallback 到 Git。

## Google mode

Google authority 是有界 native Doc head：它保存 `control`、current summary 與 root manifest，並以 Docs revision CAS 作唯一 commit 點。不可變 Markdown／JSON／attachment objects 以 `{id,sha,size}` references 納入 manifest；完整 records、events 與 operation history可分頁保存，不能截斷。較長的可讀正文可分為有序 parts，index 維持明確來源連結；容量超限時拒絕，不截短 canonical 內容。每個可讀 part 的 UTF-8 正文上限為 128 KiB；更新時核對完整舊來源並保留既有 part IDs。新增或縮減透過受控流程：先將 index 標為更新中，逐一傳送並讀回核驗；縮減時先寫入 tombstone，再移至核准 archive folder 並核對新 parent 與 bytes/hash。全部 parts settled 後才將 index 標為已更新；容量超限拒絕，不截短。受管區域外的人工作品保留；受管區域外部變更與預期 hash 不符時拒絕覆寫並交人工對帳。Project 搜尋可能漏掉長文尾段，使用者需依 index 指定來源 part 並查看引用；不保證一次複合問答取回全文。寫入流程先上傳並下載 objects、驗證實際 bytes 的 hash/size，再用 required revision 更新 head。未知結果先按原 operation 對帳，不可盲目重送。此設計不使跨物件、Sheet 或 Project 多資源操作成為原子交易。

Sheet、Drive Project 閱讀副本與人類可讀索引都是 derived views；即使顯示版本，也須以 fresh head 對帳才能稱為 current。分段可讀副本與完整 canonical bytes/hash 是不同保證：Project 搜尋或複合問答可能漏掉長文尾段，必要時依 index 指定來源 part 並核對原始來源，不把一次查詢當全文完整性的證明。Google managed route 的 authority 或 connector 無法確認時拒絕寫入，不退回 local Git。單一 writer 是協作前提；registry/check 不等同跨機硬鎖。

canonical RecordSet 與操作歷史可用於資料還原，但這不等於完整備份／還原原生 Google Doc 的格式、任意 tabs、comments 或其他未納入 schema 的 UI 資料。實際備份範圍、還原條件與尚未支援的項目見[備份與還原界線](backup-restore.md)。Google connector 設定與 runtime 前提見[Google 連線說明](google-setup.md)。
