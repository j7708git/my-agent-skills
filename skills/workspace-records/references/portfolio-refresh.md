> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。

# Git 總表的事件刷新

`portfolio_refresh.py` 提供可注入 transport 的 Python API。它將明確宣告仍以 Git 為權威的專案集合投影到既有 Sheet，不建立新表、不切 authority、不保存憑證，也不自行安裝常駐服務。這是總表衍生面；Sheet 上的決策或優先序不會因此成為 canonical record。

## 固定設定與 Runner 入口

部署者建立固定的 `source_config`、`target`、privacy configuration 和私有 outbox directory，交給 `Outbox(directory, source_config, target, privacy_config)`。這些位置與目標不可由收件任務指定。設定是部署者提供的合作契約，不是身分認證；呼叫者仍須核對實際可寫 scope。

`source_config` 必須包含且只包含：

- `notes_root`：已核准 notes 根目錄的絕對路徑；實際值留部署環境。
- `authority: "git_local"`、`scope: "all_projects"`：明確宣告整個目錄集合仍由 Git 管理。
- `generation`：正整數設定版本。

缺設定或不符時拒絕。`notes_root` 必須是固定 workspace 下的 `project-notes`；collector 每次從其父目錄的正式 `routing.Registry` 重新讀取 registry 並呼叫 `check`，不從缺少 registry、舊 catalog、目前工作目錄或歷史 runtime 猜測 Git authority。明確註冊的 `unmigrated_git` 在沒有 canonical runtime 時仍可讀；`managed git_local` 必須有有效的 steady control。missing／unreadable、未註冊、Google、frozen 或進行中的 activation barrier 均拒絕投影。兩次來源讀取之間及每次送出前會重新核對 routing；source digest 包含 fresh registry hash，不讀舊 metadata catalog 當現況。有任何專案已切 Google 時，不可使用這份 all-Git 宣告；須更新部署與收集器，不能讀封存的 Git 副本冒充最新資料。

既有 Runner 在 notes checkpoint／commit 事件後呼叫 `outbox.probe()`。它兩次讀取當前 `projects/` 集合與來源，兩次不同則拒絕；來源 digest 不包含讀取時鐘。`no_change` 表示最後確認的投影來源仍相同，Runner 可直接結束，無需喚醒 AI 或讀寫 Google。`pending`、`error`、`refresh_required` 應交既有固定 writer 處理；不得將 pending 或 error 當作 no-change 清掉。這不是 Google 表目前未被人工改動的證明；人工改動檢查需 writer 的 fresh readback。

## 固定現有表格

`target` 必須包含 `spreadsheet_id`、`sheet_id`、`header_row`、`columns`、`rows` 與 `generation`，另可包含唯一的 `status_cell`。列、欄均採零起算；`rows` 為固定 `project_id -> row_index` 對照，`columns` 為下列 managed 欄名到實際欄序的對照：

`project_id, state, needs, now, next, blocker, updated, authority, availability, issues, source_hash, source_time, freshness, source_digest`

managed 欄可不連續；其他欄全部保留，包含人類決策與優先序。這個 adapter 不會驗證人類欄位的內容真偽，也不會把它們寫回 authority。來源新增專案但缺固定列 mapping 時回 `TARGET_MAPPING_REQUIRED`，須由部署者核准更新 manifest；來源移除專案時保留原列並顯示 `PROJECT_REMOVED`／`unknown`，不刪除人類資料。

每項更新提供精確 `write_ranges` 與 `updateCells.range`，只更新 `userEnteredValue`。所有值使用 `stringValue`，以等號開頭的來源也作純文字，不作 formula。缺 status、legacy progress、格式錯誤或來源讀取失敗保留 unknown 與 issue，不填猜測的現況；source hash 與 source time 可用時保留。freshness 的 `current` 指本次已確認來源，不能作持續即時保證。

## Transport 與可重試界線

`run_once(outbox, transport)` 執行 bounded read → prepare → fresh read → begin → write → fresh read → reconcile。transport 實作兩個方法：

- `read(target, nonce)`：重新讀取固定 managed headers、project-ID 實際列位置與全部 managed cells，回 `{request_nonce, target_hash, read_version, headers, rows}`。
- `write(payload)`：只執行傳入的既有 spreadsheet `requests`，不 append、不建立資源。

設定 `status_cell` 時，read envelope 必須額外帶 `status` 字串；它是該精確 cell 的 fresh 內容。`request_nonce` 原樣帶回；`target_hash` 是 `core.object_hash(target)`；`read_version` 是此次讀取的非空識別或 API revision（若可取得）；`headers` 必須是從實際儲存格驗證後得到的 `columns` 對照；`rows` 為 `project_id -> managed 欄字串值`。transport 須依固定實際 row index 讀回，不能先按 project ID 重排後掩蓋插列／換列。空格統一成空字串，所有 managed cells 完整讀取，不依搜尋結果或舊快取補值。Header、project ID 或目標不符時拒絕。

nonce、read_version 與 manifest hash 將讀回綁定特定 plan 和讀取請求，但只是合作式 runtime evidence，不能認證任意 caller 的陳述。Sheets 沒有本工具可用的 cell CAS；單一 writer 及禁止在 managed 範圍同時人工修改仍是部署前提。兩次來源讀取與送出前重查可發現一般 race，不能代替實體 writer fence。

Outbox 先 durable 保存 `prepared`，再於 connector 收到 payload 前保存 `sending`。plan hash 綁定來源 digest、完整 payload、讀取基線、固定 binding 與 privacy receipt；begin 再掃描實際 payload。privacy 失敗或相依缺失拒送。更新後只接受本 plan 的 fresh nonce、完整 expected rows 和版本讀回；結果不符保留 `sending`／`OUTCOME_UNKNOWN`，下次只讀回對帳，不能盲目重送。若來源已再變動，確認舊 plan 後回 `stale`，由下一次事件準備新 plan。

transport 例外可帶 `status`。429 分類 `RATE_LIMITED`，401／403 為 `AUTH_UNAVAILABLE`，其他為 `TRANSPORT_UNKNOWN`。只有 transport 能證明請求未送出／確定被拒絕時，才可設 `definitely_not_sent=True`，讓同 plan 回 `prepared`；429 本身不自動證明未生效。此訊號不得用於 timeout、連線中斷或不明寫入。來源過期、目標變動的 prepared plan 可用 `discard_prepared(plan_hash)` 明確取消後重建；sending 不接受取消。錯誤保存在本機 outbox，Google 失聯時無法保證同步更新 Sheet 上的錯誤欄。

## 部署與驗證範圍

此模組不提供 OAuth、Google SDK 或 Codex connector 的獨立背景執行能力。部署者須將 transport 接上已授權的官方 API runtime 或既有 connector consumer。若 connector 只能在 agent turn 使用，就只能在內容有變更時交該 agent consumer；不能宣稱已做到無 agent 的雲端更新。固定 Runner 的事件註冊、consumer 啟用與實際既有表格讀回須另外驗收；只有 helper 和 fake transport 測試不代表已上線。

合成測試入口：`python -m unittest discover -s tests/workspace-records -p test_portfolio_refresh.py -v`。測試涵蓋 bounded 傳送／讀回、timeout 對帳不重複、429 的確定拒絕、來源變動、inventory removal／addition、unknown、來源讀取失敗、symlink 拒絕、row identity、privacy failure 及 pending／error 保留。


## 原生總表 header 與同步時間

managed header 儲存格使用上述 14 個英文字段名原文；中文解釋可放儲存格 note。transport 必須讀實際 header 的字串與位置，再回傳 canonical 欄名到位置的對照，不可只複誦 target 設定。可將欄位排在 A:O，讓 J 保持未管理，並將 source path 或人類決策放在 J；helper 的讀寫不會將 J 加入 managed scope。

可選 `status_cell` 的精確形狀為 `{sheet_id: <同一 target sheet_id>, row_index: 2, column_index: 0}`，表示來源 tab 的 A3。索引均為零起算。必須與 managed header 及任何 managed data row 分開；不允許設定其他 tab。這個單格不加入 source digest，不因時鐘變動刷新來源。主表可直接以原生公式引用此 cell。

啟用 metadata 後，第一次資料 payload 同時寫入可讀文字 `資料更新中｜此次寫入：<write_started_at>｜前次確認：<time 或 無>`。`write_started_at` 是 begin 已持久保存、準備交給 transport 的時間，表示此次傳送嘗試開始；不是 status.updated，也不是已完成時間。若資料與此文字均經 fresh readback 核對，才進入 `confirmation_prepared`。第二次先 fresh read 比對相同 rows 與 updating 文字，第三方變動則拒絕覆寫；再將 A3 更新為 `最後確認更新：<confirmed_at>｜<數量>個專案`，第二次讀回亦相符才 settle。

`confirmed_at` 是第二次更新前對全部資料與基線 fresh readback 完成的時間；outbox 另存 `status_confirmed_at` 作為確認文字自身讀回的時間。所有時間含 UTC offset。詳細 plan hash、sent payload hash、privacy receipt 與來源 digest 留在 durable outbox，每列的 source_digest 仍可查核。兩階段都先保存可能生效的 intent；資料 unknown 只對帳資料，確認文字 unknown 只對帳確認文字，不能重送資料掩蓋未知結果。

Google 無法寫入時，不能宣稱 Sheet 已同步錯誤資訊。A3 最多保留已成功送出的文字及前次確認時間；失敗保存在本機 outbox。已有 last_success 且 source digest 無變更時，probe 直接 no_change，既不更新時間，也不啟動模型。

## 固定 package 的事件與 queue CLI

固定部署位置與 managed package 同源。設定檔固定為 `<workspace-root>/.openchamber/workspace-records/portfolio.json`，精確包含 `{source_config, target}`；request 不得指定替代路徑或憑證。outbox 目錄固定為 `.openchamber/workspace-records/runtime/portfolio-refresh/`，需由部署步驟先建立。privacy policy、rules 與 Gitleaks 沿用同 package 契約。部署 package 必須包含 `routing.py` 及其既有依賴；只複製單檔不是安裝。

CLI 為 `portfolio_refresh.py {probe,queue,inspect} --managed [--input <event-json-file>]`。`probe` 不讀 Google、不建立模型；`inspect` 回 durable outbox 與尚未 settle 的事件。`queue` 接收且只接收下列其中一種事件：

- Git checkpoint：`{kind: "git_checkpoint", commit: "<完整 HEAD SHA>"}`。核對固定 notes repo 的實際當前 HEAD，避免事件指定任意 checkout。接於既有 notes checkpoint／commit 成功之後，不另裝 hooks 或定時喚醒 AI。
- Google canonical receipt hint：`{kind: "canonical_receipt", project_id: "<id>", operation_id: "<id>", receipt_sha256: "<lowerhex64>"}`。先確認正式 route 是該專案的 steady Google route；事件只是提示，不能把傳入內容當 fresh head 或 owner 驗證。

事件 hash 決定 durable event file；重送不建立第二件 dispatch。無資料變更時保存 no_change 回執，`dispatch_required=false`；有效變更才保存 queued，供既有固定 writer 消費。pending／error 不會被 no-change 清除；來源或 route 無法核對則 blocked，不能從過期 Git 建表。

Python `consume_queued(outbox, consumer)` 在呼叫 consumer 前再 probe，無 queued 或 no-change 時不啟動模型。consumer 可接已授權 runtime 的 `consume_once(outbox, session_factory)`；confirmed／no_change 且 outbox 無 pending 才將相關 queued 事件 settle。未知傳送保留事件及 outbox，等待原操作對帳，不自動反覆排程或建立第二套派工架構。事件 metadata 由既有流程產生，不能替代其 GitHub owner／ready 等驗證。

目前 collector 明確只支援 all-Git。Google receipt 事件可被記錄，但 Google route 會回 `GOOGLE_REFRESH_REQUIRED`／blocked；這是避免切換後沿用封存 Git 的保護，不能宣稱 mixed-route 自動更新已完成。

## 最小 mixed-route 擴充介面（規劃，尚未實作）

後續將同一 collector 擴成 `collect_routes(registry, fresh_google_observations)`：先 fresh inventory／binding；Git 列沿用現在兩次 bytes/hash 讀取，Google 列由固定 consumer 依 canonical receipt 喚醒後對正確 document ID 讀取最新 head，再交既有 `portfolio.collect` 的 snapshot／summary 解析與 registry.check，禁止把 receipt 直接當 canonical head。觀察值須綁 document revision、epoch/version/root hash、route binding 與本次 read nonce。

每專案保存已確認的來源指紋與 route digest；同一 canonical receipt 已確認時不再啟動模型。Google head 版本變動只更新其列，Git 來源仍由同一 collector 檢查；Google 離線的列保留明確 unknown/stale，不能退回 Git，也不應讓其他已驗 Git 列永久停止。這需要定義 per-project freshness／partial projection receipt，並沿用本表固定欄位、A3 兩階段確認及同一 outbox／consumer；不另建 Sheet 或派工架構。此介面及真實 mixed-mode 驗收是後續工作。
