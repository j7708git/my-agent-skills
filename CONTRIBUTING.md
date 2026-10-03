# 貢獻指南

感謝協助。這個 repo 是 private 的標準 Agent Skills 集合，維護方式是「小步、帶驗證、不自動動手修」。

## 環境需求

| 需求 | 版本／說明 |
| --- | --- |
| Python | 3.9 以上（只用標準函式庫，無需 pip 安裝） |
| Bash | 4 以上（macOS 內建 3.2 亦可執行掃描腳本，repo 檢查腳本需要 4 以上） |
| Git | 需要支援 `git rev-list`、`git diff --cached` 等一般功能 |
| Gitleaks | 完整秘密掃描需要；版本固定見 [docs/development.md](docs/development.md) |

沒有 Gitleaks 時可以跑其他檢查，但**不得**宣稱秘密掃描通過：相關步驟會以退出碼 2 結束。

## 開發流程

1. 先讀 [AGENTS.md](AGENTS.md) 與 [docs/plan.md](docs/plan.md) 的共用技術契約。
2. 改動前先讀被改的檔案；刪除別人的資料成本很高。
3. 一次改一件事：格式驗證、掃描核心、hooks、測試、文件分開改。
4. 提交前跑完整檢查：

   ```bash
   bash scripts/run-checks.sh
   ```

5. 只跑其中一項時：

   ```bash
   python3 scripts/validate_skills.py --check-docs --strict   # 格式
   python3 -m unittest discover -s tests -p 'test_*.py'       # 單元測試
   find . -name '*.sh' -not -path './.git/*' -exec bash -n {} +  # Bash 語法
   ```

## 新增或修改 skill

每個 skill 是 `skills/<name>/` 目錄，內含 `SKILL.md`（必要）與選用的
`scripts/`、`references/`、`assets/`。

必備條件：

- `SKILL.md` 第一行為 `---` 起的 YAML frontmatter，且 `name` 與目錄名完全一致。
- `name` 為 lowercase kebab-case，1–64 字元，不以連字號開頭／結尾，無連續連字號。
- `description` 非空、不超過 1024 字元，並同時說明「做什麼」與「何時使用」。
- frontmatter 只用首版允許的單行純量欄位（`name`、`description`、`license`、
  `compatibility`、`allowed-tools`）；完整規則見 [docs/development.md](docs/development.md)。
- `SKILL.md` 提到的每個相對資源都必須存在，且不得指向 skill 目錄之外。
- 內文超過 500 行時把細節搬到 `references/`，由 SKILL.md 相對引用。

## 測試與 fixtures

- 測試只能使用**合成資料**：假的 email、假的 token 格式、隨機產生的檔名。
- 不得把真實專案、真實使用者路徑或真實秘密放進 fixtures 或報告。
- 需要在暫存 Git repo 演練時，在測試內用 `tempfile` 建立，不要在真實專案上操作。
- 檢查腳本以 repo 根目錄為工作目錄執行測試，因此**測試不可把檔案寫進 CWD**；
  所有輸出路徑都必須落在 `tempfile` 建立的暫存目錄裡。
- 新的分支案例（缺依賴、既有 hooks、多 refs、遮罩等）都要有對應測試。

## 修復政策

這個 repo **不提供自動修復**，以下方式都不要寫進工具或文件建議：

- `pip install --force-reinstall`、`npm audit fix --force` 之類強制覆蓋安裝。
- 自動改寫檔案以「清除」秘密、歷史重寫、`--force` 推送、把 repo 或 fork 轉 public。
- 靜默安裝 hooks 或修改 `core.hooksPath`。

正確順序是：先撤銷／輪替憑證，再評估歷史清理，最後才是可見度調整；每一步都需要明確授權。

## 提交訊息

- 繁體中文，先寫做了什麼，再寫怎麼驗證。
- 範例：`文件：補上退出碼與 hooks 安裝說明（已跑 scripts/run-checks.sh）`。
- 不要在提交訊息內貼秘密原文；需要舉例時用遮罩形式。

## 提交流程

未經使用者明確授權，任何 agent 都不自行 `git add`／`commit`／`push`，也不建立 remote
或修改分支保護。使用者已明確同意的範圍內（例如已授權的 private repo 首次發布）則照做；
範圍之外的動作（例如轉 public、`--force` 推送）需要另外取得授權。

## 授權

貢獻內容以 [LICENSE](LICENSE) 的 MIT 授權釋出。
