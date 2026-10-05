# 產品技術契約

本文件是這個 repo 對**掃描器產品行為**的公開契約：命令介面、退出碼、輸出格式與安全邊界。
它描述的是 `skills/repo-privacy-audit/` 這個 skill 應該做什麼，不是維護流程，
也不是某次實作的執行紀錄。

> **範圍**：本文件只涵蓋掃描器 `repo-privacy-audit`。
> 第二個 skill `project-workflow` 是流程指引、**不是掃描器**，
> 其公開契約與邊界見 [project-workflow.md](project-workflow.md)。

| 文件 | 回答的問題 |
| --- | --- |
| 本文件 | 掃描器的產品行為契約：CLI、退出碼、輸出、安全邊界 |
| [project-workflow.md](project-workflow.md) | `project-workflow` 流程指引的契約與邊界（非掃描器） |
| [README.md](../README.md) | 我要怎麼開始用 |
| [development.md](development.md) | 維護這個 repo：驗證、測試、CI、工具版本 |
| [compatibility.md](compatibility.md) | 哪些已驗證、哪些不能宣稱 |
| [research/portability.md](research/portability.md) | 格式與安裝路徑的依據 |
| `skills/repo-privacy-audit/SKILL.md` | 這個 skill 的完整使用說明（安裝後即可讀） |

skill 必須自足：`SKILL.md` 與 `references/` 只引用 skill 目錄內的檔案。
安裝只會複製 skill 目錄，所以**任何 skill 內的相對引用都不得指向本 repo 根目錄**。

## 套件配置

- Skill 路徑：`skills/repo-privacy-audit/`。
- `SKILL.md` 的 `name` 必須與父目錄名完全一致（lowercase kebab-case，1–64 字元）。
- `scripts/`、`references/`、`assets/` 以 skill 根目錄為相對基準。
- 不依賴平台專屬擴充（`allowed-tools`、`context` 等）來維持可攜性。
- 不新增執行時相依：掃描核心只用 Bash 與 Python 3 標準函式庫。

## 命令介面

每次選擇一種模式。以下以 staged 為例；其他模式見下表，不用 shell 的管線符號串接模式：

```bash
bash scripts/repo-privacy-audit.sh --staged --repo .
```

| 模式 | 涵蓋範圍 |
| --- | --- |
| `--staged` | 掃描 index（即將進入 commit 的內容） |
| `--range A..B` | 掃描實際 commits |
| `--history` | 掃描所有**本機 refs 可達**的 commit metadata 與內容 |
| `--github` | 唯讀檢查 GitHub 專案設定（可見度／安全分析／fork／Dependabot／branch protection） |
| `--pre-push` | 從 Git stdin 讀 refs；**不固定假設分支名** |

選項：

| 選項 | 意義 |
| --- | --- |
| `--repo PATH` | 換掃描目標（預設為當前目錄） |
| `--policy PATH` | 換政策 JSON |
| `--format text\|json` | 輸出格式 |
| `--out PATH` | JSON 報告輸出檔 |
| `--gitleaks PATH` | 指定 Gitleaks 執行檔（也可用 `REPO_PRIVACY_AUDIT_GITLEAKS`） |
| `--print-default-rules` | 輸出內建規則與所有政策預設值，不必讀文件 |

`--range` 與 `--history` 只宣稱涵蓋本機 refs 可達的 commit：
**不宣稱**涵蓋遠端仍存在但本地不可達的物件、已刪除的遠端 ref、reflog、fork，
或曾經公開過的歷史。三者都不掃尚未 commit 的工作樹內容（那要用 `--staged`）。

## 退出碼

| 退出碼 | 意義 | 使用者該做的事 |
| --- | --- | --- |
| `0` | 掃描**完整**，且在這次宣告的範圍與政策下沒有 high 等級發現 | 可據此往下走，但**不是安全保證** |
| `1` | 掃描完整，但**有 high 等級發現** | 依 [SECURITY.md](../SECURITY.md) 順序手動處理，先撤銷／輪替憑證 |
| `2` | **未完整掃描**，或操作／工具失敗 | 不可視為通過；輸出會標示 `incomplete` 或 `error` |

兩條不可動搖的規則：

1. **警告不中止執行，但「未完整掃描」永遠不能聲稱通過。** 缺少 Gitleaks、
   檔案超過政策大小上限、政策損壞、GitHub 端點無權限讀取——一律是退出碼 `2`，
   狀態為 `incomplete` 或 `error`，不是「掃過了沒問題」。
2. **退出碼 `0` 不是安全保證。** 它只代表「這次掃描完整，且涵蓋範圍內沒有 high 發現」。
   未啟用的政策規則、沒掃到的檔案、遠端不可達的歷史都不在這個結論裡。

## 輸出格式

JSON 報告至少包含：

- `schemaVersion`、`tool.version`
- `mode`
- `status`：`passed` / `findings` / `incomplete` / `error`
- `coverage`
- `findings`

每個 finding 包含 `ruleId`、`severity`、`location` 與**遮罩描述**。

**秘密原文不會印在畫面上，也不會寫進錯誤訊息或日誌。** 報告與錯誤輸出必須遮罩；
遮罩是為了避免「用掃描器找秘密，結果掃描器自己洩漏秘密」。

## 安全邊界

掃描一律**唯讀**：

- 不改檔案內容、不改 Git index、不 commit、不 push 遠端、不動 GitHub 設定。
- `--github` 只呼叫唯讀 API；**只有 `--github` 會連網**，且必須在文件說明。
- 高風險密鑰比對由 Gitleaks 負責。腳本**不會自動下載或安裝** Gitleaks；
  缺少時可退回有限模式的 regex 比對，但狀態是 `incomplete`、退出碼 `2`。
- 政策是 **JSON 資料**，不 source、不 eval；個人 email／PII 規則可設定，
  但預設不寫入使用者真實資料。
- `.env.example` **不按檔名阻擋**，仍然掃內容。
  `AGENTS.md`、`docs/` 與一般文件不是天生機密，是否敏感由政策決定，不按檔名猜。
- 被跳過的檔案（過大、格式不支援）記為 `error` 並讓整體狀態變 `incomplete`，
  不能當成「已檢查且乾淨」。

## 修復與 hooks 的授權邊界

**修復預設不自動執行。** 工具只回報與說明，動作由人決定：

1. 先撤銷／輪替憑證。
2. 再評估歷史清理（歷史重寫、`--force` 推送）。
3. 最後才是可見度調整。

每一步都需要明確授權。工具不得實作自動改寫祕密、歷史重寫或可見度調整。

Hooks 與 CI：

- Hooks 只做 **repo-local** 安裝；不覆蓋既有 hooks、不默默串接、不修改任何全域 Git 設定。
  既有 `core.hooksPath` 或已存在 hooks 時明確拒絕並說明原因，並提供移除方式。
- Hooks 與 CI 可以擋住退出碼 `1` **與** `2`——未完整掃描不該被當成通過。
- 完整秘密掃描依賴 Gitleaks，這一點必須在使用說明裡寫清楚。

## 本 repo 的驗證方式

開發、測試與 CI 的做法見 [development.md](development.md)；
可宣稱與不可宣稱的範圍見 [compatibility.md](compatibility.md)。
本文件不記錄某次驗收的執行日誌、測試環境路徑或版本控制紀錄——
那是本機工作紀錄，不是產品契約。
