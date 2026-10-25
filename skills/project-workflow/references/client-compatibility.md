> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 平台中立與 client 相容性

本文件說明 `project-workflow` 對各 agent client 的**已確認**與**未確認**狀態。
重點：**「文件已確認支援」與「實際 runtime 已測試」是兩件不同的事，必須分開寫。**

這個 skill 不綁定任何模型、provider 或特定 agent 工具名稱。
它只是一份可以被任何能讀取檔案的 client 手動載入的流程指引。

## 1. 格式層：Agent Skills

- 標準（<https://agentskills.io/specification>）：一個 skill 是含 `SKILL.md` 的目錄，
  frontmatter 至少 `name` 與 `description`，且 `name` 必須與目錄名一致。
- `description` 的**規格上限是 1024 字元**。部分 client 的 catalog 另有**預設 500 字元**
  上限（例如 DeepSeek Harness）；那是 client 端的預設／建議，**不是規格上限**。
- `project-workflow` 只使用標準欄位（`name`、`description`、`license`、`compatibility`），
  不使用平台專屬的 `allowed-tools`、`context` 等擴充。description 維持在 500 字元內，
  以相容 client catalog 的預設上限。
- 相對資源（`references`、`assets/templates`）以 skill 根目錄為基準，且全部在 skill 目錄內。
  把整個 skill 目錄單獨複製走，也不會依賴本集合 repo 的其他檔案。
- **這是文件層級的確認，不代表任何 client 已實際載入過本 skill。**

## 2. Client 現況（載入位置為文件已確認；OpenCode project 位置已實測，其餘 runtime 未測試）

| Client | 文件已確認的載入位置 | 手動觸發 | runtime |
| --- | --- | --- | --- |
| OpenCode | 原生 global `~/.config/opencode/skills`、project `.opencode/skills`；相容 `~/.agents/skills`、`.agents/skills` | `@project-workflow` | **已實測**（2026-10-21，project 位置） |
| pi | 通用 `~/.agents/skills`、project `.agents/skills`（遞迴目錄，從當前工作目錄往父層搜尋直到 Git 根）；原生 user `~/.pi/agent/skills`、project `.pi/skills` | `/skill:project-workflow`；`/reload` 更新 | **未實測** |
| DeepSeek Harness | project `.dsh/skills`、`.agents/skills`（取最接近的 Git 根）；bundle 只讀一層、不遞迴 | 空白分隔輸入 `/project-workflow` | **未實測** |

### OpenCode

- 來源：<https://opencode.ai/v2/docs/skills>。
- 文件提及原生 global／project 位置，以及相容的通用目錄 `.agents/skills`。
- 手動觸發為 `@project-workflow`。
- 上述載入位置為**文件已記載**；project `.opencode/skills/` 的載入機制已於
  **2026-10-21 實測**（把 `handoff-refresh`、`release-prep` 放進該目錄，皆成功載入
  並讀到新內容），本 skill 本身未逐一重測。

### pi

- 來源：<https://pi.dev/docs/latest/skills>；原生目錄另見 pi-mono 官方 README
  <https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/coding-agent/docs/skills.md>。
- pi 支援通用 `.agents/skills`（遞迴目錄，從當前工作目錄往父層搜尋直到 Git 根），也支援原生
  `~/.pi/agent/skills` 與 project `.pi/skills`。
- 手動觸發 `/skill:project-workflow`；內容更新後用 `/reload`。
- 上述為文件記載；**runtime 未實測**。若官方 README 無法核實原生目錄，
  就只採用雙方文件都同意的通用 `.agents/skills`。

### DeepSeek Harness

- 來源：<https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/skills>。
- project 位置為 `.dsh/skills` 與 `.agents/skills`（取最接近的 Git 根）。
- skill bundle 只讀**一層、不遞迴**：`SKILL.md` 必須在 bundle 頂層，
  不能只放在子目錄裡期待被遞迴找到。
- catalog 的 `description` 預設上限 500 字元（client 端預設，不是規格上限）。
- user 目錄由 `agentsHome` 參數決定，預設 `~/.agents/skills`。官方 filesystem README
  <https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/skill/skill-filesystem/README.md>
  說明 `dshHome` 為 `$DSH_HOME` 或 `~/.dsh`，`agentsHome` 為 `$DSH_AGENTS_HOME` 或 `~/.agents`。
- 使用方式（tool-skill README
  <https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/skill/tool-skill/README.md>）：
  使用者以空白分隔輸入 `/project-workflow` 觸發；需要 registry、filesystem、tool-skill
  外掛為 enabled。**本 skill 不協助安裝任何外掛。**
- DeepSeek Harness 是 harness／client，不是模型 API；其 preview 文件可能變動，
  **runtime 未實測**。

## 3. 手動 fallback（任何 client 都適用）

不論 client 有沒有自動載入 skill，都應該能用以下方式使用本 skill：

1. 直接讀取 `SKILL.md`。
2. 需要細節時，依 `SKILL.md` 的指向讀取 `references` 內的對應文件。
3. 需要空白模板時，複製 `assets/templates` 內的檔案再填寫。

這是刻意設計的：即使 client 不支援 skill 自動載入，人或 agent 仍能照著做。
**但這不保證 fallback 一定會被正確執行**；沒有 runtime 實測。

## 4. 明確不能宣稱的事

- 格式檢查通過、檔案放對位置，**不等於**某個 client 會自動載入或正確觸發。
- 不保證模型會精確遵守本 skill 的每一步，也不保證 client 會因此配置任何權限。
- 「文件已確認支援」只描述標準與已公開的 client 說明，**不是 runtime 保證**。
- 本 skill 不是掃描器，不提供任何安全保證。
