# 相容性說明

這份文件的目的，是把「可以宣稱」和「不能宣稱」分開寫清楚。
任何驗證結果都要落在下面某一格裡；沒驗過的就寫未驗證。
產品行為契約見 [specification.md](specification.md)；維護與驗證方式見 [development.md](development.md)。

## 已驗證的環境

| 項目 | 狀態 | 依據 |
| --- | --- | --- |
| Linux（x86_64）+ Bash 5.1 + Python 3.10 + Git 2.34 | 已驗證 | 本 repo 的 `scripts/run-checks.sh` 與 `tests/`（掃描核心合成測試 191 項；另有 project-workflow 契約測試，見下方「project-workflow 的狀態」） |
| Gitleaks 8.30.1（linux x64） | 已驗證 | 版本與校驗和見 [development.md](development.md)；歷史與 staged 掃描皆實跑過 |
| skills@1.7.0 安裝（global／project × copy／symlink） | 已驗證（僅安裝） | 四個 case 全部退出碼 0 且資源齊全，詳見下方「安裝路徑」 |
| hooks 安裝／移除／實際觸發 | 已驗證 | `tests/repo-privacy-audit/test_hooks.py`：68 項，含真 `git commit`／`git push` 的擋下行為 |
| macOS + Bash 3.2 | **未驗證** | 掃描腳本設計為相容，但沒有實際跑過本 repo 檢查 |
| Windows 原生 shell | **不支援** | 未測試，不宣稱 |
| WSL | **未驗證** | 推測可用，但沒有實測 |
| Git 2.30 及更早 | **不支援 hooks** | hooks 需要 `--path-format=absolute`（Git 2.31 引入）；拿不到絕對路徑時明確拒絕而不是猜。掃描模式本身未在該版本實測 |

## 掃描器的實際版本

| 項目 | 值 |
| --- | --- |
| 工具版本（報告中的 `tool.version`） | `0.2.0` |
| 報告 `schemaVersion` | `1.0` |
| 政策 `schemaVersion` | `1.0` |

## Skill 格式

- 依 <https://agentskills.io/specification>：`SKILL.md` 帶 YAML frontmatter，
  `name` 與 `description` 必填，`name` 與父目錄名一致。
- 本 repo 的驗證器只支援**首版 frontmatter 子集**（單行純量欄位），規格中的
  `metadata` 映射等寫法會報錯。完整規格請用官方的 `skills-ref validate`。
- SKILL.md 內的資源引用以 skill 根目錄為基準；本 repo 的驗證器會確認這些檔案存在。

## 安裝路徑

下表是 **skills@1.7.0 的實測結果**（在隔離的暫存 HOME／暫存專案中進行，四個 case 全部
CLI 退出碼 0，且 `SKILL.md` 與其相對資源都存在於安裝目標）。

| case | 範圍 | 方式 | CLI 退出碼 | 資源存在 | 實際安裝位置 |
| --- | --- | --- | --- | --- | --- |
| 1 | global | copy | 0 | 是 | `~/.agents/skills/repo-privacy-audit`（OpenCode、Codex 通用）＋ `~/.claude/skills/repo-privacy-audit` |
| 2 | global | symlink | 0 | 是 | 同上（Claude Code 為 symlink） |
| 3 | project | copy | 0 | 是 | `<project>/.agents/skills/repo-privacy-audit` ＋ `<project>/.claude/skills/repo-privacy-audit` |
| 4 | project | symlink | 0 | 是 | 同上（Claude Code 為 symlink） |

要點：

- skills@1.7.0 對 **OpenCode 與 Codex 使用通用的 `.agents/skills`**；
  **Claude Code 使用 `.claude/skills`**（全域為 `~/.`、專案內為 `./`）。
- copy 與 symlink 兩種方式在四個 case 都成功安裝，資源檔皆可讀。
- 這些是 skills CLI 的安裝結果，**不等於 agent runtime 驗證**。
  下表列出的 client 行為仍屬未實測。

| Client | 手動安裝路徑（未實測） | 狀態 |
| --- | --- | --- |
| OpenCode | `~/.agents/skills/`、`.agents/skills/` | 安裝位置已由 skills CLI 實測；**client 端載入未實測** |
| Codex | `~/.agents/skills/`、`.agents/skills/` | 安裝位置已由 skills CLI 實測；**client 端載入未實測** |
| Claude Code | `~/.claude/skills/`、`.claude/skills/` | 安裝位置已由 skills CLI 實測；**client 端載入未實測** |

**這個 skill 不會無縫相容於任何 agent client。** 上表只證明「檔案可以被放到這些位置，
且相對資源可解析」；沒有一個 client 通過實際載入測試。

**安裝驗證不等於 agent runtime 驗證。** 把檔案放對位置，只能證明「格式與路徑可安裝」，
不能證明某個 agent 會自動載入、會正確觸發、或在真實對話中表現正確。
後者需要該 agent 的實際 runtime 與真實模型，這部分目前標為**待驗證**。
本次安裝測試也**不等於**掃描核心（`skills/repo-privacy-audit/`）最終快照的驗收。

## Skill 自身的自足性

- `SKILL.md` 與 `references/` 只引用 skill 目錄內的檔案。
  安裝時只複製 skill 目錄，不需要這個集合 repo 的 `docs/` 或 `scripts/`。
- 掃描腳本只用 Bash 與 Python 3 標準函式庫；安裝 `skills` CLI（Node.js）
  與執行掃描是兩件事，Node.js 不是執行時相依。
- `scripts/run-checks.sh` **只維護這個集合 repo**，不是 skill 的執行入口。
  把 skill 安裝到你的專案後，那個腳本不會跟著走。

## 工具

| 工具 | 版本 | 角色 |
| --- | --- | --- |
| Python | 3.9+ | 掃描核心與驗證器（只用標準函式庫） |
| Bash | 4+（掃描腳本 3.2 可跑） | 入口與 hooks |
| Git | 2.31+（hooks）；掃描模式未設下限 | 讀取 index／歷史，僅唯讀 |
| Gitleaks | 8.30.1 | 完整秘密掃描；**缺少時掃描不完整** |
| GitHub CLI | 選用 | 只有 `--github` 模式需要；只呼叫唯讀 API |
| skills CLI | 選用（實測 1.7.0） | 只用於安裝，不是執行時相依 |

`--print-default-rules` 可以在不讀文件的情況下直接輸出內建規則與所有政策預設值，
用來確認實際行為而不是依賴文件轉述。

## 明確不能宣稱的事

1. **缺 Gitleaks 不等於沒有秘密。** 沒有 Gitleaks 時**內容比對**的狀態是 `incomplete`、
   退出碼 2，報告不得描述為通過。（`--github` 的專案設定檢查不依賴 Gitleaks，
   缺 Gitleaks 時那一部分仍可完成——但它本來就不代表內容比對過。）
2. **安裝成功不等於 agent 會自動使用。** 上表的四個安裝 case 只證明 skills CLI
   能把 skill 放進正確位置且資源齊全。**沒有任何 client 通過實際載入測試。**
3. **退出碼 0 不是安全保證。** 它只代表該次掃描完整、且在宣告範圍與政策下沒有 high 發現。
4. **被跳過的檔案不是「沒問題」。** 超過大小上限的檔案不掃內容並記為 error，
   該次掃描因此不完整。
5. **本地歷史掃描不涵蓋**遠端仍存在但本地不可達的物件、已刪除的遠端 ref、reflog、
   fork 或曾公開過的歷史；也**不涵蓋尚未 commit 的工作樹內容**（index 由 `--staged` 掃描）。
6. **本 repo 的 CI 不證明** GitHub 設定（branch protection、自訂掃描排程）已就緒。
7. **合成 fixtures 通過掃描不等於**掃描器對真實洩漏模式的召回率已驗證。
8. **格式驗證通過不等於** skill 的說明正確、步驟可執行。
9. **CI 通過不代表所有 GitHub 安全設定可查。**
   已確認的是：CI runner 執行全部測試且無 skip，且 `scripts/run-checks.sh` 完整通過。
   secret scanning／Dependabot／branch protection 若讀不到，仍回報 incomplete，
   不會從缺漏推論成未啟用，也不代表那些設定已就緒。
10. **統一編號不會被比對。** 該規則未實作（特例未處理，硬做會給出錯誤判斷），
    政策寫入 `validate: "tw_uniform_number"` 會被明確拒絕。
11. **「未啟用」與「無權限」在 GitHub 設定檢查中無法區分。**
    查不到的端點一律記為未檢查，不會從缺漏推論成未啟用。
12. **本文件不記錄發布過程。** 遠端可見度、安裝授權與歷史改寫這類維運事實
    會隨時間改變，也不屬於產品契約；以當下的 GitHub 設定與本機 Git 狀態為準。

## project-workflow 的狀態

`skills/project-workflow/` 是**流程指引**，不是掃描器：沒有掃描核心、
不依賴 Gitleaks，也沒有 Bash／Python 執行時相依。

| 項目 | 狀態 | 依據 |
| --- | --- | --- |
| Agent Skills 格式與相對資源 | 已驗證 | `python3 scripts/validate_skills.py --check-docs --strict` 與 `tests/project-workflow/` |
| 隔離複製後的自足性 | 已驗證 | 契約測試把 skill 目錄複製到暫存 repo 後重新驗證 |
| 文件已確認的載入位置（OpenCode／pi／DeepSeek Harness） | 文件已確認；**runtime 未實測** | 各 client 官方文件，整理於 skill 內 `references/client-compatibility.md` |
| 安裝實測（本 skill） | **未實測** | 先前的四種隔離安裝只針對 `repo-privacy-audit`，**不繼承給本 skill** |
| agent runtime 實際載入與觸發 | **未驗證** | 沒有在任何真實 client 實測 |

要點：

- 上面「已驗證的環境」表中的 191 項與四種安裝實測，**對象是掃描器**；
  不要把它當成 `project-workflow` 的 runtime 或安裝實測。
- `project-workflow` 只有契約測試（模板欄位、自足引用、隔離複製、平台中立、
  隱私 placeholder、授權敘述），證明的是靜態性質，不是 agent 行為。
- `description` 的**規格上限是 1024 字元**；部分 client（例如 DeepSeek Harness）的
  catalog 另有**預設 500 字元**上限。那是 client 端建議，**不是規格上限**。
- 任何 client 都應提供手動 fallback：直接讀取 `SKILL.md` 與 `references`；
  各 client 的已確認／未確認狀態見 skill 內的 `references/client-compatibility.md`。

## 其他流程指引 skill 的狀態

`web-ui-e2e-smoke`、`independent-rerun-verify`、`review-fix-round`、`native-crash-triage`
與 `release-prep` 同樣是**純文字流程指引**，沒有掃描核心、沒有執行時相依，
也不依賴 Gitleaks 或 Bash／Python。

| 項目 | 狀態 | 依據 |
| --- | --- | --- |
| Agent Skills 格式與相對資源 | 已驗證 | `python3 scripts/validate_skills.py --check-docs --strict` |
| 專屬契約測試 | **無** | `tests/` 目前只有 `repo-privacy-audit`、`project-workflow` 與 `academic-paper-search` 的測試 |
| agent runtime 實際載入與觸發 | **未驗證** | 沒有在任何真實 client 實測 |

要點：

- 格式驗證只證明 frontmatter、命名與相對資源可解析，**不證明流程步驟正確或可執行**。
- 這些 skill 的驗證結果**不繼承** `repo-privacy-audit` 的四種安裝實測或 runtime 行為；
  反向亦然。

## OpenCode adapters（`adapters/opencode/`）

`adapters/opencode/` 收錄作者實際在用的 OpenCode agent／command 定義：
`project-workflow` 配套的 `pm`／`researcher`／`implementer`／`tester`、
可選的獨立複驗 `tester-verify`、獨立
審查者 `reviewer`／`reviewer-deep`、`workflow-optimizer` 健檢 agent、
`translator` 翻譯官、獨立的 `web-search` agent，以及
`/wish`／`/proto`／`/research`／`/optimize` 指令。
與 skill 的差別：

- 這些是**平台專屬設定檔**，使用 OpenCode 的 agent／command frontmatter 格式
  （實測版本 OpenCode v2.0.27）；不宣稱 Claude Code、Codex 或其他平台可用。
  另外 `pm.md` 引用 `tools.openchamber` 工具、`pm.md`／`implementer.md` 提到
  瀏覽器面板，這些是 **OpenChamber** 提供的工具：純 OpenCode（TUI）可載入
  這些 agent，但相關指引在該環境沒有對應工具，需自行變通。`translator.md`
  用 `subagent` 呼叫 `pm`；需要 `pm` 設為 `mode: all` 與 workspace 的
  `experimental.subagent_depth: 2`。沒有這兩個條件時，它會退回「輸出交接單，
  請使用者切到 `pm` agent 貼上」。
- 發行版本**移除所有 `model:` 綁定**（作者的 provider／模型是個人設定）；
  安裝者需依自己環境補上模型選擇，選擇建議見 `adapters/opencode/README.md`。
- 這些定義在作者的 OpenCode 環境中日常使用；但**對其他使用者的安裝與執行
  未實測**——「作者的日常使用」不等於「任何環境裝上就會正常運作」。
- `pm`／`researcher`／`implementer`／`tester`／`tester-verify` 預設採用 `project-workflow` 的
  慣例（`project-notes/`、自包含簡報等），建議與該 skill 一併安裝；
  `reviewer`／`reviewer-deep` 審查時對照任務卡驗收條件，同樣引用該慣例；
  `web-search` 為自足，不依賴任何 skill。
- `translator` 是意圖層的選配 primary agent：把使用者的口語願望整理成自包含
  brief，經使用者核可後交給 `pm`；它不寫程式、不跑 shell、不派其他 subagent
  （只允許呼叫 `pm`），
  edit 權限只開偏好檔（`project-notes/shared/preferences/`）。偏好檔與
  `project-notes/` 慣例沿用 `project-workflow`，建議一併安裝該 skill 與 `pm`。
  **runtime 行為未實測**：本 repo 只人工複查 frontmatter 與文件，沒有在真實
  agent runtime 載入並跑完整對話。
- `agents/web-search-modules/*.md` 帶 `hidden: true` frontmatter：OpenCode 會把
  `agents/` 下巢狀目錄的 `.md` 當成 agent，此欄位（v2.0.27 實測會解析）讓
  模組檔不進使用者的 agent 選擇。
- 驗證器（`scripts/validate_skills.py`）**不掃描 `adapters/`**：它只檢查
  `skills/` 與 `docs/` 的指定檔案。adapters 內 Markdown 的連結正確性
  靠人工複查，這是已知未自動化的檢查項。

## 升級與維護

- 行動與工具版本升級時，同步更新 [development.md](development.md) 的表格與校驗和。
- 新增支援環境時，先在本文件把該格從「未驗證」改成「已驗證」，並附上實際執行的命令。
