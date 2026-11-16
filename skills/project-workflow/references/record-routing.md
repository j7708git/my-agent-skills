> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 可選工作紀錄路由

本節延伸 `project-workflow` 的單一紀錄權威與授權概念，只在工作區已明確啟用 `workspace-records` 路由政策時適用。未啟用的 Git 工作區沿用原流程；載入本 skill 不會建立 registry、部署程式或切換專案。

## 每次讀寫的步驟

1. 從工作區內已核對的 package 執行 `routing.py bind`，取得指定 project 的 binding；再用 `check` 核對同一份 registry digest。固定 package 位於 `<workspace-root>/.openchamber/workspace-records/package/skills/workspace-records/scripts/`。不從 cwd、全域 skill 副本或 request 自帶的 notes/runtime 路徑猜目標。
2. registry 固定在 `<workspace-root>/.openchamber/workspace-records/routes.json`，private runtime 固定在同目錄的 `runtime/`；只保存定位、policy version 與 routing phase。mode／epoch／logical version 仍由實際權威核對。缺檔、損壞、未知 project、binding 改變或非 steady 時停止寫入。
3. positive `unmigrated_git` 走原 Git 紀錄與 fallback，不需要 Google 帳號／網路；若已有 local control，仍須核其 phase／epoch。managed Git 由固定 local control 管理。managed Google 必須由已驗 connector 讀完整 head，經 adapter 驗證後核 mode／phase／epoch／version，再以同一 binding 提交 Doc CAS。失聯、權限失效或結果未知時保留未送出候選、原 ID 對帳；封存 notes 不是 fallback。
4. scope 沿用派工的 project／record／field／role。PM 維護 status／log，並把人類決策寫入 Doc authority 的受管正文；worker 只寫指定 task／research／acceptance，reader 唯讀。Sheet 只是衍生 view。Google 工具不可用或該 client 未驗時，交 PM 處理，維持既有 permissions，不自動擴大全帳號寫權。
5. 寫入先掃實際 payload、核 preview/hash，再送出與讀回。Doc commit 與 Sheet projection 分列；stale Sheet 由最新 authority 重建，不能反向補寫權威。完整 records 可採 immutable raw files 與 manifest，由 Doc CAS head 發佈；本指引不把小型單 Doc 試驗當大型 storage 或 restore 驗收。

下文所有 `status.md`／tasks／acceptance／log 路徑是 Git 分支的實體映射；Google 分支使用同種 logical records 與明列的 scope，不另寫一份本機現況。程式碼 Git、公開 docs、hooks、提交／推送授權沿原規則。

## 轉換與交接

registration／transition 只能先 prepare。精確 target manifest、snapshot 與 writer receipt 的 digest 是待驗資料，不是停止 writer 的證明；`writers_stopped` 字串或本機旗標不能啟用另一權威。

轉換順序為 routing phase 停兩邊新寫入 → 原 authority freeze／drain → 完整 snapshot 與 target readback → 發佈精確 frozen route → 核回執後 resume target。registry 與 authority 是兩個 store，不能視為同一交易；中斷保持拒寫，依回執 reconcile。P4 完整往返、P5 隔離安裝與同 runtime 整合、P6 指定 pilot 的實際 writer fence／切換／退回，各自保留驗收。

Project 問答與 human-readable view 是閱讀入口；以 head／manifest 為寫入依據。current／archive 移動和 Project 索引不與 CAS 原子，不能宣稱來源集合永遠只有同一版本。
