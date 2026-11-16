> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時先停止，指出過期段落並交由維護者更新。

# Google mode 設定

本文件承接 [workspace-records 操作流程](../SKILL.md)，說明接上 Google authority 前必須成立的條件。

## 固定路由與執行環境

managed 專案需由固定 workspace package 與 registry 對應到單一 authority。registry、runtime 與 privacy policy 使用固定位置：

- registry：`.openchamber/workspace-records/routes.json`
- runtime：`.openchamber/workspace-records/runtime`
- privacy policy：`.openchamber/repo-privacy-policy.json`
- package：`.openchamber/workspace-records/package/skills/workspace-records/`

managed CLI 入口為 `skills/workspace-records/scripts/workspace_records.py {initialize,resolve,inventory,read,preview,apply,reconcile,recover} --managed --input <json-file>`。`routing.py bind|check|prepare-transition` 可核對 binding 或產生轉換提案；`prepare-transition` 不會安裝、啟用或切換 route。request 不可自行指定 workspace root、runtime、privacy policy 或 Google document ID。請勿把隔離候選 package 的存在當作已完成 live deployment。

## Google authority

Google mode 以一個 native Doc 的 bounded head 作唯一 CAS authority。head 包含 control、current summary 與 root manifest；完整 Markdown／JSON records、附件 bytes 及分頁的 events/journal 由 immutable objects 保存，head 只引用已驗證的 `{id,sha,size}`。寫入需先掃描實際 payload、上傳物件、重新讀回驗證 hash/size，最後以 Docs revision CAS 更新 head。

Sheet、Drive Project 摘要和人類可讀索引是衍生讀取面，不提供寫入 authority。它們可能落後於 head；只有讀取最新來源並核對版本與 hash 後，才可標示為 current。一次更新橫跨多個物件或衍生面時不是跨資源原子交易。每個專案採單一 writer 協作前提。

## 連線與拒絕條件

helper 不接收或保存 Google OAuth token、密碼或 connector secret。授權由執行端的 Google connector/runtime 提供；不要把 token 放進 request、policy、workspace 檔案或專案內容。若 connector 沒有可用讀寫能力、route 無法核對、head revision 已改變、privacy scan 未完成或結果未知，停止並依原 operation 對帳；managed Google route 不得 fallback 到 Git。

來源套件已包含 transition、promotion 與 route publication 介面，但它們不會因為文件或套件存在而自動部署或啟用 route。正式切換需先核准 exact project manifest，依 authority freeze → route freeze → writer drain → snapshot/target readback → frozen route → resume target → steady route 完成，並由 P6 獨立驗證實體 writer fence。執行時仍使用 connector/runtime 的授權；helper 不提供 OAuth/token 管理。

備份 snapshot 與還原的資料範圍見[備份與還原界線](backup-restore.md)。
