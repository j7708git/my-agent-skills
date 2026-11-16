---
name: workspace-records
description: 在已明確設定的專案中，透過 workspace-records 檢視、預覽或更新精確 scope 的 status、task、plan、research、acceptance 與 log 紀錄。
---

> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。


# workspace-records

本 skill 的工作紀錄概念源自 `project-workflow` 的專案與授權規範；若工作紀錄行為不在明確設定的專案或指派 scope 內，遵循既有流程並先停止。使用者要求在已設定專案中查閱或更新結構化工作紀錄時，使用本 skill。目標專案與記錄一律限制在明確 scope 內。

先讀取[路由與停止條件](references/routing.md)；要規劃寫入時再讀[資料與寫入契約](references/contract.md)。需要設定 Google 連線或備份時，另讀[Google 連線說明](references/google-setup.md)及[備份與還原界線](references/backup-restore.md)。

## 操作步驟

1. 透過固定 managed package 與專案 registry 解析已設定的專案及 authority；mapping 缺漏、無效或不可讀時停止，不可從工作目錄或鄰近資料夾猜目標。
2. 只盤點並讀取請求所需的紀錄；保留來源 bytes、時間、未知段落與連結。
3. 使用者要求變更時，先產生 scoped preview，檢查精確路徑、記錄與 privacy 結果；只套用同一 scope 與 plan hash 的已檢視計畫。
4. 寫入後讀回核對。結果不確定時，先用原 operation ID 對帳；工具無法確認寫入狀態就停止並交人工處理。

## 權限與範圍

- 使用者只要求讀取，不代表授權寫入；每項寫入必須符合使用者指定的專案與欄位。
- status 與 log 是 PM-only baseline；worker 只能變更派工明列 scope 的 task、research、acceptance 等記錄。角色名稱只是協作規則檢查，不是身份驗證。
- privacy scanner 缺漏、未完成、內容過期或掃描失敗時拒絕寫入。不可繞過 scanner 或放寬 policy 以強行通過。
- helper 依賴本 repo 的 privacy scanner、Gitleaks 與本機提供的 policy/configuration。獨立複製 skill 而缺少相依時必須 fail closed；不要宣稱完整 PII 偵測。
- Google managed mode 以同一份 Doc 的 bounded head/control 作為唯一 CAS authority；不可變物件保存完整 canonical records、附件與歷史，長文可採有序分段。閱讀副本、Drive Project 內容與 Sheet 都是衍生資料，不得當作第二份 authority；Project 搜尋可能漏段，必要時直接指定 index 中的來源 part。
- managed Google route 無法確認或 connector 離線時拒絕寫入，不退回 Git。專案需單一 writer；跨 Doc、附件、Sheet 或 Project 的多項更新不是原子交易。transition/promotion/route publication source tools 不等於部署；正式使用前需核准 exact manifest 並完成 P6 writer fence。
- 本 skill 的 helper 不保存 connector 憑證。連線授權由執行環境提供；沒有可用連線時停止，不改走另一種帳號或後端。
- 紀錄操作不會順便 commit、push、安裝 skill、修改 hooks 或切換專案 authority。

詳細 schema 與操作語意見[契約](references/contract.md)；模式選擇與停止條件見[路由](references/routing.md)。備份內容範圍及不能還原的 Google Docs UI 資料見[備份與還原界線](references/backup-restore.md)。
