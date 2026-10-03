---
name: repo-privacy-audit
description: 對 Git repo 與 GitHub 專案做唯讀的隱私與機密稽核，找出 token、憑證、電話型 email、個人本機路徑與台灣個資風險。掃描 staged 內容、指定 commit 範圍、本機 refs 可達的歷史與 pre-push 真正會推送的 commit，並以唯讀 API 檢查 GitHub 的可見度與安全設定。當使用者要確認 repo 是否外洩憑證或個資、要決定是否安裝 hooks 或 CI 阻擋、或問到疑似外洩後該怎麼處理時使用。輸出遮罩後的發現與明確退出碼。這個 skill 只讀與回報，不自動修復、不改寫歷史、不推送、不修改任何 GitHub 設定。
license: MIT
compatibility: Bash、Python 3.8 以上、Git 2.31 以上（hooks 需要）。完整的高風險密鑰比對另需使用者自行提供的 Gitleaks 8.x；GitHub 唯讀設定檢查另需 GitHub CLI。缺少 Gitleaks 時內容掃描為不完整，退出碼 2。Windows 原生 shell 未測試。
---

# repo-privacy-audit

對 Git repo 與 GitHub 專案做**唯讀**的隱私與機密稽核：找出 token、憑證、個人資料與
不該公開的 metadata，回報位置與嚴重度，並給出明確的退出碼。

這個 skill 的定位是**看清楚、講明白**。它不修復、不改寫、不推送，也不替你做決定。

## 安全邊界（不可協商的設計）

- **只讀。** 不寫入受檢 repo、不改 index、不 commit、不push、不動任何 GitHub 設定。
- **不自動修復。** 不改寫檔案、不重寫歷史、不提供一鍵 force push。
- **不碰 Git 設定。** 安裝與移除 hooks 都不修改任何 Git 設定值——
  `core.hooksPath` 已設定時直接拒絕，不覆蓋也不移除它。
- **不自動安裝外部工具。** Gitleaks 必須由你自己提供；缺少時照實回報為不完整。
- **不洩漏秘密原文。** 報告只輸出遮罩描述；秘密原文不會出現在報告、錯誤訊息或日誌裡。
  比對到的秘密字串還會從檔名、作者名與描述中整體移除。
- **政策是資料。** JSON 政策檔永遠不被當成程式碼執行（不 source、不 eval）。
- **不自動安裝 hooks。** 安裝 hooks 會改變你每次 commit 的行為，必須由你明確執行。

## 前置條件

| 需求 | 用途 | 缺少時的結果 |
| --- | --- | --- |
| Bash、Python 3 | 執行掃描 | 腳本直接失敗並回報，不假裝掃過 |
| Git 2.31 以上 | 掃描模式與 hooks 安裝 | hooks 需要絕對路徑的 hooks 目錄；拿不到就拒絕 |
| Gitleaks（自行安裝） | 高風險密鑰的正式比對 | 退出碼 2，只做有限 regex 比對 |
| GitHub CLI（僅 GitHub 模式） | 唯讀 API 查詢 | 退出碼 2，報告標示缺少工具 |

完整的高風險密鑰比對**永遠需要 Gitleaks**。本 skill 不會替你下載或安裝它。
你也可以用 `--gitleaks` 指定執行檔，或設定 `REPO_PRIVACY_AUDIT_GITLEAKS` 環境變數。

## CLI

```bash
bash scripts/repo-privacy-audit.sh --staged
bash scripts/repo-privacy-audit.sh --range A..B
bash scripts/repo-privacy-audit.sh --history
bash scripts/repo-privacy-audit.sh --pre-push
bash scripts/repo-privacy-audit.sh --github --github-repo OWNER/REPO
```

| 參數 | 說明 |
| --- | --- |
| `--staged` | 掃 index（即將進入 commit 的內容），不看工作目錄現況 |
| `--range A..B` | 掃描 A 到 B 之間實際存在的 commit |
| `--history` | 掃描所有本機 refs 可達的 commit metadata 與內容 |
| `--pre-push` | 從 Git 的 stdin 讀 refs；不假設分支名、不假設有 origin |
| `--github` | 唯讀檢查 GitHub 專案設定（見下文） |
| `--repo` | 目標 Git repo 路徑，預設為目前目錄（用 `.` 即可） |
| `--policy` | JSON 政策檔；範例見 `assets/policy.example.json` |
| `--format` | `text`（人看）或 `json`（工具看） |
| `--out` | 把 JSON 報告寫到檔案 |
| `--gitleaks` | Gitleaks 執行檔路徑 |
| `--gh` | GitHub CLI 執行檔路徑（僅 GitHub 模式） |
| `--github-repo` | `OWNER` 與 `REPO` 名稱以斜線分隔；未指定時從 origin 推導 |
| `--github-branch` | 要檢查 branch protection 的分支（預設由本機分支推導） |
| `--no-metadata` | 不套用 email／姓名等身分規則（秘密與台灣 PII 規則仍會掃描） |

入口腳本只負責找到 Python 3 並轉交參數，所有邏輯在 `scripts/audit_core.py`
（只用 Python 3 標準函式庫，無外部相依）。

## 退出碼

| 退出碼 | 意義 | 你該做的事 |
| --- | --- | --- |
| `0` | 掃描**完整**，且沒有 high 等級發現 | 這是唯一一種可以說「通過」的情況 |
| `1` | 掃描完整，但**有 high 等級發現** | 依 `references/remediation.md` 手動處理 |
| `2` | **未完整掃描**，或操作／工具失敗 | 不可視為通過；先看報告缺什麼 |

退出碼 2 常見原因：沒裝 Gitleaks、政策檔有錯、Git 指令失敗、**檔案超過大小上限
而未掃描**、超過政策上限而被截斷、查不到 GitHub 設定所需權限。
**沒有掃完就沒有結論**，不要把 2 說成「大致沒問題」。

## 政策檔

政策是純 JSON 資料，決定規則、嚴重度、路徑判定、上限與遮罩行為。
`assets/policy.example.json` 是可以直接複製改的起點，完整欄位說明見 `references/checks.md`。

```bash
bash scripts/repo-privacy-audit.sh --staged --policy assets/policy.example.json
```

三組預設值要分清楚，它們互相獨立：

- **電話型 email 預設就是 `high`**（local part 像電話號碼，例如
  `0912345678@example.invalid`），屬於身分規則，預設**啟用**。
- **一般 author／committer email 預設 `low`**，不阻擋；要阻擋請明確調高。
- **台灣個資格式規則（身分證、信用卡號、手機號）預設關閉**，
  需要時才在政策裡開啟 `pii.enabled`，並自行評估誤報與揭露風險。

另有兩個預設啟用的路徑規則：敏感**檔名**（如 `id_rsa`、`*.pem`、`credentials`）
預設 `high`；個人**本機路徑**（如 `/home/<user>/`、`/Users/<user>/`）預設 `medium`。
範本檔（`.env.example`、`*.template` 等）不因檔名被判敏感，但**內容照掃**。

## GitHub 唯讀設定檢查

`--github` 只做**唯讀查詢**，用於回答「這個專案會不會被別人看到、保護夠不夠」：

- 可見度（private／internal／public；public 依政策回報一筆 medium 發現）
- 安全分析（`security_and_analysis`；`secret_scanning` 與
  `secret_scanning_push_protection` 缺任一項就記為**未檢查**並退出 2，
  不會從缺漏推論成「未啟用」）
- fork（只確認端點可讀與是否至少有一個 fork，不抓 fork 內容）
- Dependabot alerts 端點可讀性（不解析、不評估漏洞內容）
- branch protection 與 required checks（只查一個分支，預設由本機分支推導）

它**只呼叫讀取用的 API**，不寫入、不改設定、不抓取檔案內容，也不代替 Gitleaks 比對秘密。
查不到的端點一律標示為「無法確認」，並記錄「未啟用與權限不足無法區分」。
未指定 `--github-repo` 時會從 `origin` 推導；推導不到就明確要求你指定。
需要 GitHub CLI 且已完成登入。

## hooks（必須明確安裝）

需要 **Git 2.31 以上**。

```bash
bash scripts/rpa-hooks.sh install --repo .      # 只影響這個 repo
bash scripts/rpa-hooks.sh status --repo .
bash scripts/rpa-hooks.sh uninstall --repo .
```

安裝的是 `assets/hook-pre-commit` 與 `assets/hook-pre-push`。護欄：

- `core.hooksPath` 已設定就**拒絕**安裝與移除；本工具不修改它，也不移除它。
- 這個 repo 存在**多個 worktree** 就拒絕（不論你在主 worktree 或 linked worktree 操作），
  因為 hooks 目錄是共用的，會影響其他 worktree。讀不到 worktree 清單也拒絕。
- hooks 目錄或狀態路徑是**符號連結**就拒絕，寫入不會跟著連結跑到 repo 之外。
- 發現既有 hook 或 dangling symlink 一律拒絕，不覆蓋、不默默串接。
- 「檔案裡有本工具標記」不足以覆蓋或刪除：必須與設定檔記錄的雜湊一致，
  你手動改過的內容會被保留並明確回報。設定檔缺失或損壞時同樣拒絕。
- 安裝中途失敗會回滾，只刪除「本次建立、且原本不存在」的精確路徑。
- **不修改任何 Git 設定值**，安裝後 `git config` 的內容完全不變。
- hooks 會擋住退出碼 `1` 與 `2`：沒掃完不該被當成通過。

## CI 範本

`assets/github-security.yml` 是**範本**，不會自動安裝到你的專案，也不會修改你的
GitHub 設定。使用前必須先滿足前提：skill 已安裝並提交在你的 repo（常見位置如下），
且你自己決定 Gitleaks 怎麼取得。

```text
.agents/skills/repo-privacy-audit
```

範本的重點：權限只有 `contents: read`、以 commit SHA 釘選 action、`fetch-depth: 0`
掃完整歷史、Gitleaks 固定版本並校驗 SHA-256、只擋退出碼 `1` 與 `2`、
不上傳報告 artifact。詳見 `references/checks.md`。

> 這個 skill 內**沒有**「維護集合 repo 用的檢查腳本」。安裝到你自己的專案後，
> skill 的執行入口只有上面列出的指令；本集合 repo 自己的 CI 與檢查腳本不隨 skill 一起安裝。

## 給 agent 的使用流程

1. 先問清楚要保護的是什麼（私人 repo？含個資？只是預防？），再選模式。
2. 確認工具是否齊備；缺 Gitleaks 就先說明結果會是不完整。
3. 執行掃描，讀 `status` 與 `coverage`，不要只看有沒有發現。
4. 回報時**照抄退出碼語意**，不要把 `2` 說成通過，也不要暗示「掃描過就沒有秘密」。
5. 有 high 等級發現時，先走撤銷與輪替，不要先想清理歷史。
6. 不可逆的動作（重寫歷史、force push、調整可見度）一律交回使用者決定。

## 深入文件

- `references/checks.md`：各模式涵蓋範圍、報告欄位、政策欄位、已知不涵蓋的項目。
- `references/remediation.md`：發現疑似外洩秘密後的處置順序與授權邊界。

## 明確不做與不能宣稱的事

- 不保證「沒有秘密」。退出碼 `0` 只代表在這次宣告的範圍與政策下沒有 high 等級發現。
- 本機歷史掃描不涵蓋遠端仍存在但本地不可達的物件、已刪除的遠端 ref、reflog、
  fork，以及曾經公開過的歷史。
- 缺 Gitleaks 時結果不完整；政策未啟用台灣 PII 規則時，該類問題不會被回報。
- 超過大小上限的檔案會被跳過並記為 error，這次掃描因此不完整。
- 掃描通過不等於 agent 會自動使用這個 skill，也不等於格式驗證已通過。