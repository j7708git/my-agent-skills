# project-workflow 產品契約

本文件是 `skills/project-workflow/` 這個 skill 的公開契約：它做什麼、不做什麼，
以及檔案位置與使用邊界。產品行為的完整步驟寫在
[skills/project-workflow/SKILL.md](../skills/project-workflow/SKILL.md)；
本文件只描述可對外承諾的部分。

與掃描器 `repo-privacy-audit` 的契約分開：那一個看
[specification.md](specification.md)。**project-workflow 不是掃描器。**

## 這個 skill 是什麼

`project-workflow` 是一份**流程指引**，描述如何在使用者的工作區內協調兩條線：

- **公開程式碼 repo**：程式碼、測試、公開文件與架構成品。
- **獨立 private 工作紀錄 repo**：計畫、進度、研究、驗收與任務卡。

它涵蓋：確認工作區與兩個 repo 根、規劃（含先做最小原型）、用已知／假設／未知收斂需求、
撰寫自足任務簡報、派工與並行紀律、實際驗收，以及跨機同步與交接。

## 這個 skill 不是什麼

- **不是掃描器。** 它不做隱私或秘密掃描，不執行任何掃描指令，也不提供安全保證。
  隱私掃描請用 [specification.md](specification.md) 描述的 `repo-privacy-audit`。
- **是純文字指引，不是自動腳本。** 它不會自己執行任何動作；但在使用者已明確授權的
  任務範圍內，執行 agent 可以照本流程寫入與實作，不必每個細節都停下來重問。
- **不會自動動手。** 不建立 repo、不建立 remote、不 commit、不 push、不安裝 git hooks、
  不變更可見度、不盤點或搬移既有檔案。
- **載入不等於授權。** 讀取或被載入這個 skill，不構成對任何寫入或不可逆動作的授權。
  commit、push、安裝 hooks、改可見度與搬移舊檔案，都需要使用者針對該動作的明確授權。
- **不能強制權限。** skill 內對權限的敘述只是說明，不是權限設定；這個 skill 無法
  強制任何 client 的 allow／deny 行為。

## 檔案配置

skill 自足：`SKILL.md` 與其相對資源只引用 skill 目錄內的檔案，單獨複製整個 skill
目錄就能使用，不需要本集合 repo 的其他檔案。

| 路徑 | 內容 |
| --- | --- |
| [skills/project-workflow/SKILL.md](../skills/project-workflow/SKILL.md) | 流程主文與固定任務簡報區塊 |
| [references/workflow-details.md](../skills/project-workflow/references/workflow-details.md) | 規劃、簡報、研究、驗收與交接的細節 |
| [references/privacy-and-sync.md](../skills/project-workflow/references/privacy-and-sync.md) | 公開／私有隔離、索引紀律與同步步驟 |
| [references/client-compatibility.md](../skills/project-workflow/references/client-compatibility.md) | client 的已確認與未確認狀態、手動 fallback |
| [assets/templates/overview.md](../skills/project-workflow/assets/templates/overview.md) | 專案總覽空白模板 |
| [assets/templates/progress.md](../skills/project-workflow/assets/templates/progress.md) | 進度空白模板（最新摘要、兩 repo 同步欄位與驗收狀態） |
| [assets/templates/plan.md](../skills/project-workflow/assets/templates/plan.md) | 計畫空白模板 |
| [assets/templates/task-brief.md](../skills/project-workflow/assets/templates/task-brief.md) | 任務簡報空白模板 |
| [assets/templates/research.md](../skills/project-workflow/assets/templates/research.md) | 研究空白模板 |
| [assets/templates/acceptance.md](../skills/project-workflow/assets/templates/acceptance.md) | 驗收空白模板 |

模板是**空白起點**：每份都含 placeholder 欄位與填寫規則，複製後再填。
所有模板都不含真實帳號、真實 repo、真實電腦路徑或特定 CI／session 紀錄。

## 安裝（預設 project scope）

預設在**目標專案目錄**安裝，讓每個 client 有獨立副本，**不預設安裝到全域**。
不要在 skills 集合 repo 自己的根目錄安裝，避免無意把檔案加進集合 repo。

| Client | project 位置（文件已確認；runtime 未實測） |
| --- | --- |
| OpenCode | `.opencode/skills/project-workflow` |
| pi | `.pi/skills/project-workflow`（原生；亦支援通用 `.agents/skills`） |
| DeepSeek Harness | `.dsh/skills/project-workflow`（bundle 只讀一層） |

- OpenCode 可用 skills CLI（已確認的指令形式，本 skill 不代為執行）：
  `npx skills add <owner>/<repo> --skill project-workflow -a opencode`（**不加 `-g`**）。
- pi／DeepSeek Harness 的 CLI agent ID 尚未核實，不要猜。可先用 `--list`，
  再以互動方式選 `project-workflow` 且不加 `-g`；若工具不支援該 client，就改用手動複製。
- 本 skill 不執行 `npx`、不 `npm install`、也不修改任何真實 agent 路徑。
- 手動複製：目標同名目錄已存在就**拒絕**，不要覆蓋；要整體複製 skill bundle
  （含 `references` 與 `assets`）。

## 公開與私有的契約

- 公開知識放程式碼 repo 的 README 與 `docs/`：使用說明、產品契約、教學、可分享研究、架構成品。
- 內部工作紀錄放**獨立的 private repo**：完整計畫、進度、研究、驗收與任務卡。
- **private 只降低公開程度，不是機密保管庫。** 憑證、真名、私人 email、帳號 ID、
  內網位址與個人絕對路徑，在任何 repo 都不應出現。
- **只有公開內容額外禁止**把內部工作過程（派工紀錄、session 日誌、舊 commit、
  指向 private 筆記具體檔案的連結）整份搬進去。private notes 可以保留**必要的
  commit 參照**與**相對的內部引用**，這是交接所需。
- 公開文件必須自足，不得連到 private 筆記的具體檔案，也不得要求讀者先擁有私有資料。
- 舊的工作紀錄目錄（例如 `.local`、`.scratch`）不自動遷移、不改名、不刪除；
  只有明確授權時才做「先盤點、再分類、最後複製」的遷移，**未逐項核對並驗收前
  不得聲稱已完成**；原件保留，清理另行授權。

### notes repo 的目錄主幹

```text
projects/<id>/overview.md
projects/<id>/progress.md
projects/<id>/plan.md
projects/<id>/research/
projects/<id>/acceptance/
projects/<id>/tasks/
shared/research/
```

公開的 code repo 放程式碼、測試、README 與對外 `docs/`，**不得**含 private 工作筆記；
code repo 的
`.gitignore` 應排除暫存。workspace 的 `.tmp` 或 code repo 內的 `.tmp` 都應被 ignore，
且 `.tmp` **絕不自動清理**。跨機接手時，先讀 plan、overview、progress，需要時再讀 research。

## 交接與同步的契約

- 交接內容至少包含：對應的分支與**真實已存在的 commit**、哪些僅本機尚未 push、
  兩個 repo 各自的同步狀態、待決定事項，以及下一步可行動任務。
- **寫入與同步分開**：已授權的任務內可以照流程寫入；只有同步或上傳前才要求乾淨。
- 同步前先分別對兩個 repo 做 `git status`；若 dirty、diverged，或有不屬於本任務、
  可能被覆蓋的他人變更，就停下回報，不自動 stash／reset／rebase。本任務自己授權
  範圍內產生的變更可以繼續。拉取更新只用 `git pull --ff-only`。
- code-root 為 N/A 時不要對不存在的 repo 跑 git。notes repo 尚無 remote 時，可在取得
  建立授權後於本機建立並記錄 pending，只在上傳前做嚴格 private 檢查。
- 不確定可見度時不要上傳，也不要自動變更可見度。
- 兩個 repo 的 commit 與 push **無法原子化**，各自獨立；成功與失敗要分別回報。
  不 `--force`、不改寫歷史。
- 建立檔案或寫入筆記，不等於已同步。

## 平台中立

- 這個 skill 不綁定任何模型、provider 或特定 agent 工具名稱。
- 「文件已確認支援的載入位置」與「實際 runtime 尚未測試」必須分開寫。
  各 client 的來源、手動觸發與限制見
  [references/client-compatibility.md](../skills/project-workflow/references/client-compatibility.md)。
- 官方文件已記載的 project 位置（runtime 皆未實測）：OpenCode 用 `.opencode/skills`、
  pi 用 `.pi/skills` 與通用 `.agents/skills`、DeepSeek Harness 用 `.dsh/skills` 與通用
  `.agents/skills`。三者也可共用通用 `.agents/skills`，但**預設採 project scope**，
  讓每個 client 擁有獨立副本，不預設安裝到全域。
- `description` 規格上限是 1024 字元；部分 client catalog 另有預設 500 字元上限
  （client 端建議，不是規格上限）。本 skill 的 description 維持在 500 字元內。
- 任何 client 都能用手動 fallback：直接讀取 `SKILL.md`，再依指向讀取 `references`
  並複製 `assets/templates`；但這**不保證 fallback 一定被正確執行**。
- 格式通過不保證模型會精確遵守步驟，也不保證 client 會自動配置權限。

## 明確不能宣稱的事

1. 不保證任何 agent client 會載入、觸發或正確遵守這個 skill。
2. 不保證任何 repo 已備份、已同步或已正確分類。
3. 不是掃描器，不提供任何安全保證，也不能取代 `repo-privacy-audit`。
4. 不替使用者做任何不可逆的動作；授權範圍外的動作一律交回使用者決定。
