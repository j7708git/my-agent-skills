# 開發與驗證說明

本文件說明這個 repo 自己的格式驗證、檢查腳本、CI 與工具版本。
產品行為契約（CLI、退出碼、輸出、安全邊界）請看 [specification.md](specification.md)；
使用 skill 請看 [README.md](../README.md)；相容性範圍請看 [compatibility.md](compatibility.md)。

## 目錄一覽

| 路徑 | 用途 |
| --- | --- |
| `docs/specification.md` | 產品技術契約：CLI、退出碼、輸出格式、安全邊界 |
| `scripts/validate_skills.py` | 格式驗證器（Python 3 標準函式庫，無外部相依） |
| `scripts/run-checks.sh` | 統一檢查入口：格式 → 測試 → Bash 語法 → Python 語法 → Gitleaks（歷史＋staged） |
| `.github/workflows/ci.yml` | 本 repo 的 CI，權限 `contents: read` |
| `.gitleaks.toml` | 使用 Gitleaks 預設規則，沒有路徑或值豁免 |
| `tests/scaffold/` | 驗證器與檢查腳本自身的測試 |
| `tests/repo-privacy-audit/` | 掃描核心與 hooks 的測試（全部使用合成 fixture） |
| `tests/project-workflow/` | `project-workflow` 的契約測試（模板欄位、自足引用、隔離複製、平台中立、隱私 placeholder、授權敘述） |
| `tests/academic-paper-search/` | `academic-paper-search` 檢索腳本的離線契約測試（查詢建構、Atom 解析、輸出格式、退出碼） |

`scripts/run-checks.sh` **只維護這個集合 repo**，不是 skill 的執行入口；
skill 安裝到使用者的專案後，這個腳本不會跟著走。

### 公開文件與本機工作紀錄

`docs/` 只放**公開**文件：使用說明、產品契約、維護指南、相容性、可分享研究與架構成品。

本機工作紀錄不進版控，兩條路：

| 路徑 | 內容 | 版控 |
| --- | --- | --- |
| `.local/` | 完整計畫、todo、session 日誌、驗收紀錄、研究筆記、視覺檢查 sidecar | 忽略 |
| `.openchamber/` | 派工用的任務計畫卡 | 忽略 |

預設工作筆記不發布；要把某份筆記變成公開文件，先依用途審核
（讀者是誰、會不會連到本機路徑或尚未公開的資訊），再改寫成自足文件放入 `docs/`。
公開文件不可引用 `.local/` 或 `.openchamber/` 的路徑——對外部讀者那是死連結。

### 測試現況

掃描核心既有合成測試全部通過：

| 測試檔 | 項目數 | 覆蓋對象 |
| --- | --- | --- |
| `tests/repo-privacy-audit/test_core_contract.py` | 64 | 掃描核心契約：模式、退出碼、遮罩、政策、政策欄位 |
| `tests/repo-privacy-audit/test_hooks.py` | 68 | hooks 安裝／移除／實際 `git commit` 與 `git push` 擋下行為 |
| `tests/repo-privacy-audit/test_cli_modes.py` | 12 | CLI 模式與輸入處理 |
| `tests/scaffold/test_validate_skills.py` | 33 | 驗證器自身與文件分流：公開文件自足、禁止引用本機路徑、禁止追蹤本機目錄 |
| `tests/scaffold/test_run_checks_cli.py` | 14 | 檢查腳本的 CLI 與失敗聚合 |
| `tests/project-workflow/test_skill_contract.py` | 契約測試 | `project-workflow`：模板欄位、自足引用、隔離複製、平台中立、隱私 placeholder、授權敘述 |
| `tests/academic-paper-search/test_arxiv_search.py` | 54 | arXiv 檢索腳本契約：查詢建構與 URL 編碼、Atom 解析、json/text/bibtex 輸出、退出碼 |

上面掃描核心的項目數是既有數字；`tests/project-workflow/` 的契約測試**不硬寫項目數**，
以測試檔為準，避免文件數字過期。新增測試時同步更新這張表的「覆蓋對象」即可。

fixtures 全部是合成資料；完整假憑證只在暫存測試 repo 的執行期組合，原始碼保留分段字串。
秘密掃描設定不放行任何值或目錄（理由見下方 `.gitleaks.toml`）。

## 為什麼自己寫驗證器

不引入 PyYAML：CI 與使用者的機器上不應該為了驗證 metadata 而多一個相依套件。
代價是只支援**首版 frontmatter 子集**；完整規格請改用官方參考實作
`skills-ref validate`（<https://agentskills.io/specification>）。

## 首版 frontmatter 子集

只接受「單行純量」的頂層欄位：

| 欄位 | 必填 | 規則 |
| --- | --- | --- |
| `name` | 是 | 1–64 字元；`[a-z0-9]` 與單一連字號；不得以連字號開頭／結尾；不得連續連字號；必須等於父目錄名 |
| `description` | 是 | 1–1024 字元 |
| `license` | 否 | 單行純量 |
| `compatibility` | 否 | 單行純量，最長 500 字元 |
| `allowed-tools` | 否 | 單行純量（規格標示為實驗性） |

**不支援**（遇到會報錯，不會猜測或忽略）：巢狀映射、串列、區塊純量（`|` 與 `>`）、
多行值、Tab 縮排、YAML 錨點與別名、重複鍵、未列於上表的鍵。
規格中的 `metadata` 映射屬於已知限制；若真的需要，請改用 `skills-ref validate` 驗證，
並在 issue 中提出需求。

## 驗證器做了什麼

`python3 scripts/validate_skills.py [--repo PATH] [--check-docs] [--strict] [--format text|json]`

1. `skills/<name>/SKILL.md` 存在且可解析 frontmatter。
2. `name` 規則與目錄名一致。
3. `description` 長度與非空。
4. `SKILL.md` 的相對資源存在：Markdown 連結目標、程式碼片段中的路徑、帶副檔名的路徑。
   **只以 skill 根目錄為基準解析**，且不得離開 skill 目錄——安裝時只會複製 skill 目錄，
   引用 repo 根檔案（例如 `docs/specification.md`）在安裝後會失效，一律報錯。
   skill 內部可以用 `references/../references/x.md` 這種不逃出的相對路徑。
5. `--check-docs`：
   - 必要的公開維護文件存在且非空：README、AGENTS、CONTRIBUTING、SECURITY、LICENSE，
     以及三份契約與維護文件——**`docs/specification.md`**（產品契約：CLI、退出碼、
     輸出、安全邊界）、本文件、`docs/compatibility.md`。
     契約缺席或空白都算錯誤：沒有它，公開文件就沒有說明產品怎麼用、怎麼讀退出碼。
   - 公開文件（含 `docs/research/portability.md`）的 Markdown 相對連結指向存在的檔案，
     依序以「文件所在目錄、repo 根目錄」為基準解析，邊界是 repo 根。
   - 公開文件**不得引用** `.local/` 或 `.openchamber/`：這些路徑不在版控內，
     對外部讀者是死連結。這項檢查先於存在性檢查，因為這些路徑在本機確實存在。
   - `.local/` 與 `.openchamber/` **不得被 Git 追蹤**（唯讀 `git ls-files`）。
     目標不是 Git 工作樹或 `git` 不可用時，這一項**跳過且不宣稱已檢查**
     （`coverage` 不會列出 `local-dirs-not-tracked`）；本 repo 自己就是 Git 工作樹，
     所以 CI 與 `run-checks.sh` 一定會實際執行。
6. 警告（不影響退出碼，除非 `--strict`）：description 過短、內文超過 500 行、`.sh` 缺 shebang。

`--check-docs` 只驗證**公開**文件。內部計畫與驗收紀錄（`.local/`）與任務卡
（`.openchamber/`）不在版控內，新鮮 clone 不存在，所以驗證器不要求它們存在，
也不會因為它們缺席而報錯。用 `--repo` 檢查其他 repo 時，同樣要求對方具備
自己的產品契約（`docs/specification.md`）。

### 退出碼

| 退出碼 | 意義 |
| --- | --- |
| 0 | 通過（可能有警告） |
| 1 | 發現錯誤 |
| 2 | **未完成**：缺少可驗證的輸入（例如 `skills/` 不存在或沒有 skill），無法宣告通過 |

若同時有其他錯誤，仍以 2 為主（未完成的驗證優先回報），錯誤明細照常列出。
`skills/` 尚未產出時回傳 2 是刻意的「暫時等待」，不是通過。

## 統一檢查腳本

```bash
bash scripts/run-checks.sh                 # 全部檢查
bash scripts/run-checks.sh --skip-gitleaks # 明確跳過秘密掃描（輸出會標註未執行）
bash scripts/run-checks.sh --repo PATH     # 檢查另一個 repo（驗證器仍來自本 checkout）
bash scripts/run-checks.sh --gitleaks /path/to/gitleaks
```

依序執行：

1. `python3 scripts/validate_skills.py --repo <目標> --check-docs --strict`
2. `python3 -m unittest`（測試檔以 `find tests -name 'test_*.py'` 收集，避免 discover 漏掉非套件目錄；
   逐檔執行並把該檔目錄加入 `PYTHONPATH`，讓同目錄的 `helpers.py` 可匯入）
3. `bash -n` 檢查所有 `.sh`
4. `python3 -m py_compile` 檢查 Python 語法
5. Gitleaks 兩個獨立步驟：
   - **歷史**：`gitleaks git . --log-opts='--all'`（所有本機 refs 可達的 commit）
   - **staged**：`gitleaks git . --staged`（index 中即將進入 commit 的內容）

聚合規則：任一步未完成 → 2；否則任一步失敗 → 1；全過 → 0。
腳本不下載、不安裝任何東西，也不修改任何全域設定。
沒有 Gitleaks 時兩個秘密掃描步驟都以未完成（退出碼 2）收尾，並明說掃描不完整。
`--repo`／`--gitleaks` 缺參數值時立即以退出碼 2 結束（不會陷入參數解析迴圈）。

### 秘密掃描的邊界與失敗處理

- **歷史掃描不涵蓋未 commit 的內容。** index 由 `--staged` 涵蓋；
  未 staged 的工作樹變更不在本腳本範圍內，發布前請另外確認。
- gitleaks 以 `--exit-code 99` 執行，腳本再映射：`0` → 通過、`99` → 有發現（失敗 1）、
  其它非 0 → 未完成（2）。
- **fail closed**：掃描後若報告檔不存在、不是合法 JSON 或不是陣列，一律記為未完成（2），
  不會因為「沒有輸出錯誤」就算通過。
- 報告與輸出都帶 `--redact`；原始 gitleaks 輸出只寫入暫存 log，畫面上只顯示
  規則、檔案與行號，不顯示 `Secret` 欄位。

## CI

`.github/workflows/ci.yml`：

- 觸發：`push`（所有分支）、`pull_request`、`workflow_dispatch`。
- 權限：`contents: read`（僅讀取）。沒有 `pull-requests: write`，因此不會在 PR 上留言，
  也不需要 `GITHUB_TOKEN` 之外的 secret。
- Runner：`ubuntu-24.04`（不用 `ubuntu-latest`）。
- 行動以 commit SHA 釘選，不使用浮動 tag：
  | 行動 | 版本 | Commit SHA |
  | --- | --- | --- |
  | `actions/checkout` | v5.1.0 | `fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09` |
  | `actions/setup-python` | v5.6.0 | `a26af69be951a213d495a4c3e4e4022e16d87065` |
- Python 固定 `3.12`。
- `actions/checkout` 設 `persist-credentials: false`：token 不留在工作目錄的 `.git/config`，
  後續步驟也沒有寫入需求。
- Gitleaks 固定 `v8.30.1`（linux x64），下載後比對 SHA-256
  `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`，
  不符即中止。校驗和值取自該版本的官方 `gitleaks_8.30.1_checksums.txt`。
  安裝步驟用絕對路徑 `"$HOME/.local/bin/gitleaks" version` 驗證
  （`GITHUB_PATH` 只對後續步驟生效），同時寫入 `GITHUB_PATH` 給下一步的檢查腳本使用。
- 秘密掃描由 `scripts/run-checks.sh` 執行（歷史 `--log-opts='--all'` ＋ staged），
  checkout 使用 `fetch-depth: 0`，報告寫入暫存目錄且 `--redact` 遮罩，
  不會上傳 artifact、不呼叫 GitHub API。

更新工具版本時要同時更新 CI 內的版本、SHA 與校驗和，並在本文件更新表格。

### 沒有自動化的部分

CI 不會也不應修改 GitHub 設定：branch protection、required checks、Dependabot、
自訂掃描排程都由維護者手動設定。

## `.gitleaks.toml`

使用 Gitleaks 預設規則（`[extend] useDefault = true`），**沒有任何路徑、值或 stopword 豁免**。

測試原始碼以分段字串在執行期組合合成憑證；生成後的完整值與既有 fixtures 相同，
仍由真實 Gitleaks 與內建引擎驗證偵測能力。這不是掃描器對編碼／分段秘密的全面保證。

原先針對兩個假值的豁免已移除。完整假 PAT 直接寫入使用這份設定的測試 repo 時，
仍會被 Gitleaks 攔截；集合 repo 本身的工作樹、staged 與歷史則不應包含完整憑證字面值。

不得為了讓檢查通過而免掃整個 `tests/` 目錄；真正新增例外需要維護者審查與明確理由。

## 架構圖

`docs/architecture.html` 是單一檔案、可互動的架構圖；`docs/architecture.json` 是它的來源規格
（不含任何本機絕對路徑，可安全進版控）。重新產生時：

```bash
node <archify>/bin/archify.mjs validate architecture docs/architecture.json --quality showcase --json
node <archify>/bin/archify.mjs deliver   architecture docs/architecture.json docs/architecture.html --quality showcase --json
node <archify>/bin/archify.mjs visual-check docs/architecture.html --json
```

`deliver` 會凍結當次規格並輸出 spec／artifact 的 SHA-256；`visual-check` 只讀取已交付的 HTML，
產生的截圖與收據（檔名含 `visual-check`）含本機絕對路徑，**不進版控**：
它們被 `.gitignore` 排除，並集中放在本機工作紀錄目錄（本 repo 為 `.local/reports/architecture/`）
而不是 `docs/`，避免與公開文件混在一起。成品 `docs/architecture.html` 與來源規格
`docs/architecture.json` 仍然進版控。

重新產生 sidecar 時**不要覆蓋既有收據**：那是上一版的瀏覽器證據，覆蓋等於毀掉原始資料。
本 repo 的做法是先移到 `before/` 子目錄留檔，再把新版放進同一層，便於對照。

語言說明：圖的**內容**（節點、關係、卡片）以繁體中文撰寫。Archify 的檢視器介面
（Light／Dark、Export、Legend、PATH／MAP／LENS 等固定 UI）**維持英文**，
因為 `meta.locale` 只支援 `en` 與 `zh-CN`，繁體中文不在支援清單內，故依規格省略該欄位。
`<html lang>` 同樣是英文。這是渲染器的固定行為，不是內容翻譯失敗。

## 新增 skill 的檢查清單

- [ ] `skills/<name>/SKILL.md` 存在，`name` 與目錄名一致。
- [ ] frontmatter 只用首版允許的單行純量欄位。
- [ ] `description` 說明「做什麼」與「何時使用」，長度在 1–1024。
- [ ] SKILL.md 引用的每個相對資源都存在，且在 skill 目錄內。
- [ ] 內文不超過 500 行，細節放 `references/`。
- [ ] 腳本有 shebang，`bash -n` 通過。
- [ ] 測試涵蓋新的分支案例，fixtures 全是合成資料。
- [ ] 公開文件沒有引用 `.local/`、`.openchamber/`、本機絕對路徑或特定歷史執行紀錄。
- [ ] `bash scripts/run-checks.sh` 全綠；若缺 Gitleaks，在回報中明說未完成。
