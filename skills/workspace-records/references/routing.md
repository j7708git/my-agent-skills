> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# workspace-records 路由

本文件承接 [SKILL.md](../SKILL.md)，補充 mode 路由與停止條件。只有專案已明確設定，且使用者要求查閱或變更工作紀錄時，才使用 `workspace-records`。一般程式碼工作、Git 操作與未設定專案都沿用既有流程。

## Select a mode

1. 經固定 managed package 解析專案與 registry binding。不可從目前工作目錄或 request 推斷／覆寫 project ID、workspace root、runtime directory、privacy policy 或 Google document ID。
2. 在 Git mode 先 inventory，再只讀取請求需要的紀錄。本機 Git 專案在沒有 Google 帳號或網路時仍可用，前提是 registry 明確登記為 `unmigrated_git` 且 control 可讀。
3. 在 Google mode 先讀完整 Doc head/manifest 與所需 immutable objects，驗證 scope、hash、revision 和 current route。若 authority 或 connector 不可用，拒絕寫入；絕不可 fallback 到 Git。
4. 未初始化的 local project，只有在使用者明確要求開始管理且核准設定可用時才能初始化；純讀取不得順便建立 control state。

## Read and change records

- 依 inventory 結果建立精確 record scope；保留來源路徑、bytes、時間、stable IDs 與 relationships。
- 寫入前先 preview，檢視精確目標路徑、差異、base hashes 與 privacy 結果；只套用相同 scope/plan hash 的已檢視計畫。
- 重試沿用原 operation ID。結果未知時先 reconcile 原 operation，再判斷是否可重試；不可換新 ID 隱藏不確定寫入。
- 遇到外部作用未知、mode/epoch/version 過期、privacy failure、schema 不明或來源檔後續變更時，停止並交人工處理。
- `status.md`／`log/*.md` 由 `pm` 寫入；`worker` 僅能寫派工 scope 中的 task、research、acceptance 等紀錄；`reader` 唯讀。這些 role labels 是合作規則，並非身份驗證。

## Fixed routing package

managed CLI 透過固定套件位置載入路由，不使用 global skill copy 或目前工作目錄尋找 authority。路由程式的預定部署位置為 `<workspace-root>/.openchamber/workspace-records/package/skills/workspace-records/scripts/routing.py`；registry 路徑固定，request 不得覆寫。隔離驗收與正式安裝是不同階段；除非完成明確部署與 P6 驗收，不要把候選安裝視為 live rollout。

registry 固定為 `.openchamber/workspace-records/routes.json`，runtime 為 `.openchamber/workspace-records/runtime`，privacy policy 為 `.openchamber/repo-privacy-policy.json`。`workspace_records.py --managed` 每次依 registry 核對 route；binding 變更會使已預覽 plan 失效。`routing.py bind|check|prepare-transition` 的 `bind` 只回 location-only binding；`check` 重讀 registry 並核對 digest/generation，再核對觀察到的 authority state；Google route 必須先讀完整 Doc snapshot/head，核對 `{document_id,revision_id,control:{...head.control,version:head.version}}`。`prepare-transition` 只產生 transition proposal。部署後另由 `transition.py {begin,issue,observe,inspect,resume-git,resume-google} --input <json-file>` 記錄受控階段，並由 `route_publish.py {prepare,apply,reconcile} --input <json-file>` 核對及發布 route；`promotion.py {prepare,apply} --input <json-file>` 處理 frozen target 的受管導入。這些 CLI 存在於來源套件，不代表已部署或已完成 live rollout。

## Transition boundary

正式轉換需由 PM 依序凍結來源 authority、凍結 route、停止並排空 writer、取得一致 snapshot 並讀回 frozen target、發布 frozen route、恢復 target writer，最後發布 steady route。`transition.py` 保存 intent、階段與對帳結果；`route_publish.py` 核對來源 authority、snapshot/target receipt、route binding 與版本後才推進 route。`promotion.py` 在 target frozen 時保留舊 canonical 檔案，再導入已驗證 snapshot；control 最後寫入，既有 namespace 與 operation history 保留，中斷時沿原 operation 恢復。

工具不會自行證明所有外部 writer 已停止，也不是 OS/GitHub 的強制鎖。P6 部署前須由呼叫者完成並驗證實體 writer fence；route 狀態、使用者提供的 `writers_stopped` 字串或空的 CI queue 都不能單獨替代此步驟。

本機 helper 依賴同一 repo 的 privacy scanner、Gitleaks 與固定位置的 local privacy configuration。缺少任一 runtime dependency 時必須 fail closed。helper 不持有 Google credentials 或 connector tokens，不提供帳號分享、跨機硬鎖或完整 PII 保證。單一 writer 是合作前提；Google managed mode 離線時拒絕寫入。
