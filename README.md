# my-agent-skills

`j7708git/my-agent-skills` 是一個 [Agent Skills](https://agentskills.io/specification) 集合 repo。
首版提供一個 skill：**`repo-privacy-audit`** —— 對 Git repo 做唯讀的隱私與秘密掃描，
並附上明確安裝的 hooks、CI 範本與修復手冊。

這個 repo 不做自動修復，也不替你決定任何不可逆的動作。它只負責**看清楚**、**講明白**。

> **首版狀態**：授權採 MIT。已驗證項目包括格式與相對資源、四種隔離安裝、
> 真實 hooks 與完整密鑰掃描；CI runner 執行全部測試、無 skipped。
> 另有一件事從未驗證：**agent runtime 是否真的會載入並使用這個 skill**。
> 逐項的可宣稱／不可宣稱範圍見 [docs/compatibility.md](docs/compatibility.md)，
> 產品行為契約見 [docs/specification.md](docs/specification.md)。

GitHub 設定檢查仍可能因方案／權限不足回報 incomplete；這與本機內容掃描及 CI 通過是不同的範圍，
不要把其中一項的通過當成另一項的通過。

## 目錄

- [快速開始](#快速開始)
- [安裝 skill](#安裝-skill)
- [使用掃描器](#使用掃描器)
- [退出碼怎麼讀](#退出碼怎麼讀)
- [hooks 要自己裝](#hooks-要自己裝)
- [在 CI 使用](#在-ci-使用)
- [這個 repo 自己的 CI](#這個-repo-自己的-ci)
- [驗證與品質](#驗證與品質)
- [文件索引](#文件索引)
- [授權](#授權)

## 快速開始

```bash
# 1. 取出這個 repo（會用到你既有的 Git／SSH 授權）
git clone git@github.com:j7708git/my-agent-skills.git
cd my-agent-skills

# 2. 在本 repo 跑完檢查（退出碼 0 才算通過；缺 Gitleaks 時會是 2）
bash scripts/run-checks.sh

# 3. 對你自己的專案做唯讀掃描（在專案根目錄執行，用 . 代表該目錄）
bash <skill>/scripts/repo-privacy-audit.sh --staged --repo .
```

第 1 步的網址是這個 repo 的實際位址。第 3 步的 `<skill>` 代表你安裝 skill 後的
目錄位置（見下一節）；完整參數、退出碼與安全邊界以 skill 內的 `SKILL.md` 為準。

> 第 2 步的 `scripts/run-checks.sh` **只維護這個集合 repo 本身**，
> 它不是 skill 的執行入口。掃描要另外用第 3 步的指令。

## 安裝 skill

這個 skill **不會無縫相容於任何 agent client**。下表是實際驗證過的安裝位置；
「檔案放對位置」只證明格式與路徑可安裝，**不證明某個 agent 會自動載入它**。

### 方式 A：skills CLI

安裝時會用到你**既有的** Git／`gh`／SSH 授權，CLI 不會另外要求你貼 token 給它。
遠端可見度以該 repo 當下的設定為準；可見度只影響「誰能取用」，
不影響下表的路徑與相對資源驗證結果。

```bash
# 列出可安裝的 skill
npx skills add j7708git/my-agent-skills --list

# 安裝到全域（OpenCode、Codex 與 Claude Code 的目錄由 CLI 依 client 決定）
npx skills add j7708git/my-agent-skills --skill repo-privacy-audit -g -a opencode
npx skills add j7708git/my-agent-skills --skill repo-privacy-audit -g -a claude-code
npx skills add j7708git/my-agent-skills --skill repo-privacy-audit -g -a codex
```

可選：關閉遙測（`DISABLE_TELEMETRY=1`）、選擇 copy 或 symlink。
Node.js 只在這個安裝方式需要；**掃描本身不需要 Node.js**。

### 方式 B：手動複製（不依賴 Node.js）

`skills@1.7.0` 的**實測**安裝位置如下：OpenCode 與 Codex 使用通用的 `.agents/skills`，
Claude Code 使用 `.claude/skills`（全域為 `~/.`、專案內為 `./`）。

```bash
# 先取出這個 repo（與快速開始第 1 步相同）
git clone git@github.com:j7708git/my-agent-skills.git
cd my-agent-skills

# 全域（所有專案共用）
mkdir -p ~/.agents/skills            # OpenCode、Codex（通用目錄）
mkdir -p ~/.claude/skills            # Claude Code
cp -R skills/repo-privacy-audit ~/.agents/skills/
cp -R skills/repo-privacy-audit ~/.claude/skills/

# 專案內（只對這個專案生效，建議優先選這個）
mkdir -p .agents/skills              # OpenCode、Codex
mkdir -p .claude/skills              # Claude Code
cp -R skills/repo-privacy-audit .agents/skills/
cp -R skills/repo-privacy-audit .claude/skills/
```

| Client | 全域 | 專案內 |
| --- | --- | --- |
| OpenCode | `~/.agents/skills/` | `.agents/skills/` |
| Codex | `~/.agents/skills/` | `.agents/skills/` |
| Claude Code | `~/.claude/skills/` | `.claude/skills/` |

**覆蓋風險要自己處理**：`mkdir -p` 只補上不存在的目錄，不會動既有檔案；
但如果目標路徑**已經有同名 skill**，`cp -R` 會把檔案寫進既有目錄，造成混雜。
先確認再複製：

```bash
# 目標已存在就停下來，由你決定移除或改名，不要直接覆蓋
if test -e ~/.agents/skills/repo-privacy-audit; then
  echo "已存在，請先手動處理"
else
  cp -R skills/repo-privacy-audit ~/.agents/skills/
fi
```

複製後**必須保留目錄結構**：`SKILL.md` 要在 skill 目錄的第一層，
它引用的 `scripts/`、`references/`、`assets/` 要留在同一個 skill 目錄裡（相對路徑）。
這個 skill 只引用自己目錄內的檔案，安裝後不需要 repo 根目錄。

## 使用掃描器

掃描一律**唯讀**：不改檔案、不改 index、不 commit、不推遠端、不動 GitHub 設定。

| 模式 | 說明 |
| --- | --- |
| `--staged` | 掃描 index（準備 commit 的內容） |
| `--range A..B` | 掃描實際 commits |
| `--history` | 掃描所有本機 refs 可達的 commit metadata 與內容 |
| `--github` | 唯讀檢查 GitHub 專案設定（可見度／安全分析／fork／Dependabot／branch protection） |
| `--pre-push` | 從 Git stdin 讀 refs；不假設分支名 |

常用選項：`--repo PATH`（換掃描目標）、`--policy PATH`（換政策 JSON）、
`--format text|json`、`--out PATH`（輸出 JSON 報告檔）。
完整參數以 skill 的 `SKILL.md` 為準。

**網路與工具**：只有 `--github` 會連網，且只讀。完整的高風險密鑰比對需要 **Gitleaks**；
腳本不會自動下載或安裝它。若 Gitleaks 不在 `PATH`，可用 `--gitleaks /path/to/gitleaks`
或環境變數 `REPO_PRIVACY_AUDIT_GITLEAKS` 指定執行檔。
缺少 Gitleaks 只影響**內容（secret）比對**；`--github` 的設定檢查不需要它。

## 退出碼怎麼讀

| 退出碼 | 意義 | 你該做的事 |
| --- | --- | --- |
| `0` | 掃描**完整**，且在這次宣告的範圍與政策下沒有 high 等級發現 | 可以據此往下走，但這**不是**安全保證：未啟用的政策規則、遠端不可達的歷史與沒掃到的檔案都不在結論內 |
| `1` | 掃描完整，但**有 high 等級發現** | 依 [SECURITY.md](SECURITY.md) 的順序手動處理：先撤銷／輪替憑證 |
| `2` | **未完整掃描**，或操作／工具失敗 | 不可視為通過。輸出會標示 `incomplete` 或 `error`，請看缺少什麼 |

其他輸出欄位：

- `status`：`passed` / `findings` / `incomplete` / `error`。
- JSON 輸出含 `schemaVersion`、`mode`、`status`、`coverage`、`findings`。
- 每個 finding 有 `ruleId`、`severity`、`location` 與**遮罩描述**；
  秘密原文不會印在畫面上，也不會寫進錯誤訊息。

**最重要的四條**：

1. **退出碼 0 不等於安全。** 它只代表「這次掃描完整、且涵蓋範圍內沒有 high 等級發現」。
   規則沒開、範圍沒掃到、遮罩過濾，都不在這個結論裡。
2. **缺 Gitleaks 時，內容掃描不完整。** 沒有 Gitleaks 時可以跑有限模式的 regex 比對，
   但狀態會是 `incomplete`、退出碼 `2`。「沒有發現問題」不等於「沒有秘密」——
   沒掃完就沒有結論。這個限制**只影響內容（secret）比對**：
   `--github` 的專案設定檢查不依賴 Gitleaks，仍然可以做。
3. **本地歷史掃描有邊界。** 它不涵蓋遠端仍存在但本地不可達的物件、已刪除的遠端 ref、
   reflog、fork，以及曾經公開過的歷史；也**不掃尚未 commit 的工作樹內容**
   （那要用 `--staged` 掃 index，或自行對工作樹檢查）。
4. **過大的檔案會被跳過，而且那算 error。** 檔案超過政策的大小上限時，
   該檔案不掃內容並記為 error，整體狀態變成 `incomplete`——不是「掃過了沒問題」。

## hooks 要自己裝

**安裝 skill 不會自動安裝 hooks。** hooks 會改變你每次 commit 的行為，
所以這個 repo 假設你要自己明確選擇：

```bash
# 在目標 repo 內執行
bash <skill>/scripts/rpa-hooks.sh install --repo .      # 只影響這個 repo
bash <skill>/scripts/rpa-hooks.sh status --repo .
bash <skill>/scripts/rpa-hooks.sh uninstall --repo .
```

hooks 安裝需要 **Git 2.31 以上**（要拿絕對路徑的 hooks 目錄），並且：

- `core.hooksPath` 已設定就**拒絕**安裝與移除；這個工具不修改任何 Git 設定值，
  安裝前後 `git config` 的內容完全不變。
- 這個 repo 存在**多個 worktree** 就拒絕，因為 hooks 目錄是共用的。
- 只刪除「本工具建立、且內容未被修改」的 hook；你手動改過的會被保留並回報。

hooks 可以擋住退出碼 `1` 與 `2`（也就是「有發現」與「沒掃完」都擋），
這是刻意的：未完整掃描不該被當成通過。

## 在 CI 使用

skill 內附 CI 範本。它是**範本**，不會自動安裝到你的專案，也不會修改你的 GitHub 設定。
使用前請自行：

1. 把 skill 安裝並提交到你的 repo（範本預設假設路徑為 `.agents/skills/repo-privacy-audit`）。
2. 挑選要擋的情況（範本擋 `1` 與 `2`）。
3. 決定 Gitleaks 怎麼取得（範本使用固定版本 + SHA-256 校驗，不做浮動 `latest`）。
4. 設定最小權限。範本與這個 repo 自己的 CI 都只用 `contents: read`。
5. 需要的話再由你手動設定 branch protection 與 required checks。

## 這個 repo 自己的 CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) 做的事：

- 權限只有 `contents: read`；沒有 secret，也不呼叫會寫入的 API。
- 行動以 **commit SHA** 釘選（不用浮動 tag），runner 固定 `ubuntu-24.04`，
  Python 固定 `3.12`。
- Gitleaks 固定 **8.30.1**，下載後比對 SHA-256，不符即中止。
- 秘密掃描分兩個獨立步驟：**歷史**（`--log-opts='--all'`，所有本機 refs 可達的 commit）
  與 **staged**（index）。checkout 使用 `fetch-depth: 0`。
- 執行 `bash scripts/run-checks.sh`：格式驗證 → 單元測試 → Bash 語法 → Python 語法 → 秘密掃描。

CI **不會**替你改 branch protection、required checks 或任何 repo 設定；那些由維護者手動設定。

## 驗證與品質

```bash
bash scripts/run-checks.sh                                  # 全部
bash scripts/run-checks.sh --skip-gitleaks                  # 明確跳過秘密掃描（輸出會標註未執行）
python3 scripts/validate_skills.py --check-docs --strict    # 只跑格式驗證
```

目前合成測試共 **191 項**，全部通過；fixtures 只含合成資料，
不放入真實秘密、真實 email 或本機路徑。

格式驗證器只用 Python 3 標準函式庫（不引入 PyYAML），並刻意只支援**首版 frontmatter 子集**；
完整規格請用官方的 `skills-ref validate`。細節見 [docs/development.md](docs/development.md)。

這個 repo 對自己的規則也是認真的：文件、說明與 commit message 用繁體中文；
不寫真實 email、真實家目錄路徑或真實 token；不提供 `pip`／`npm audit fix --force`
之類的強制修復捷徑。

## 文件索引

| 文件 | 內容 |
| --- | --- |
| [docs/specification.md](docs/specification.md) | 產品技術契約：CLI、退出碼、輸出格式、安全邊界 |
| [docs/architecture.html](docs/architecture.html) | 架構圖（單一 HTML，可互動瀏覽） |
| [docs/development.md](docs/development.md) | 驗證子集、檢查腳本、文件分流、CI 與固定工具版本 |
| [docs/compatibility.md](docs/compatibility.md) | 可宣稱／不可宣稱的相容性 |
| [docs/research/portability.md](docs/research/portability.md) | 格式與安裝路徑的研究依據 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 貢獻與提交流程 |
| [SECURITY.md](SECURITY.md) | 回報方式與處理順序 |
| [AGENTS.md](AGENTS.md) | coding agent 在本 repo 的工作規範 |
| [LICENSE](LICENSE) | MIT |

skill 自己的文件（安裝後也可用，不依賴本 repo）：

| 文件 | 內容 |
| --- | --- |
| `skills/repo-privacy-audit/SKILL.md` | 安全邊界、CLI、退出碼、hooks 與 CI 用法 |
| `skills/repo-privacy-audit/references/checks.md` | 各模式涵蓋範圍、報告欄位、政策欄位、已知不涵蓋項目 |
| `skills/repo-privacy-audit/references/remediation.md` | 疑似外洩後的處置順序與授權邊界 |
| `skills/repo-privacy-audit/assets/policy.example.json` | 可複製修改的政策起點 |

本 repo 的 `docs/` 只放公開文件。完整計畫、todo、session 日誌、驗收紀錄與研究筆記
是本機工作紀錄，不進版控，也不從公開文件連過去；分流規則見
[AGENTS.md](AGENTS.md) 與 [docs/development.md](docs/development.md)。

## 授權

[MIT](LICENSE) © 2026 j7708git。
