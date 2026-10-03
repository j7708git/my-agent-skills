# 在 my-agent-skills 內工作的 agent 須知

本檔是這個 repo 對 coding agent 的工作規範。人類維護者請看 [CONTRIBUTING.md](CONTRIBUTING.md)；
使用這個 repo 的 skill 的人請看 [README.md](README.md)。

## 這個 repo 是什麼

- 標準 [Agent Skills](https://agentskills.io/specification) 的集合 repo，repo 名稱 `j7708git/my-agent-skills`（private）。
- 首版只有一個 skill：`skills/repo-privacy-audit/`，做唯讀的 repo 隱私與秘密掃描。
- 平台專屬 agent（OpenCode／Claude Code／Codex 的 agent 定義）屬於選配，首版不發行；
  `skills/` 之外的 adapters 區域刻意留白，等有真實需求再新增，不要先放空殼。

## 硬性規則

1. **語言**：文件、說明、commit message 用繁體中文；識別字、API 名稱、指令維持英文。
2. **不寫真實個資**：不得寫入真實 email、真實家目錄路徑、真實 repo 路徑、真實 token。
   一律用 placeholder（例如 `<你的-repo>`、`example@example.com`）。
3. **不自動修復**：不得實作自動改寫祕密、歷史重寫、`--force` 推送、可見度調整。
   發現問題只回報與寫手冊，動作由人決定。
4. **不碰全域設定**：不改 `~/.gitconfig`、`core.hooksPath`、全域 Git 設定，
   不把 hooks 裝到使用者的其他專案。（明確的 **repo-local** hooks 安裝可以使用，
   但必須使用者明確指定目標 repo，且不得順手改全域設定。）
5. **不下載、不安裝外部工具，除非使用者明確授權**：不下載、不安裝 Gitleaks 等工具；
   專案已有的暫存工具（例如驗收用的 Gitleaks）可直接使用。需要新工具時說明缺少什麼、
   退出碼為何，由使用者決定是否取得。
6. **不新增相依套件**：掃描核心只用 Bash 與 Python 3 標準函式庫。安裝工具（skills CLI）只是可選安裝路徑。
7. **不自動改 GitHub 設定**：CI 只用唯讀權限；branch protection、required checks 等由維護者手動設定。
8. **不自行 commit／push，除非使用者明確授權**：
   - 未獲明確授權時，不 commit、不 push、不建立 remote、不改分支保護設定。
   - 已獲明確授權的範圍內則照做，例如：使用者已同意的 **private** repo 首次發布
     （建立 private repo、初始提交、push），或使用者已同意的驗收流程步驟。
   - 授權範圍之外的動作（例如把 repo 轉 public、`--force` 推送）仍需另外取得授權。

## 目錄职责

| 路徑 | 內容 |
| --- | --- |
| `skills/<name>/` | 可安裝的 skill；`SKILL.md` 的 `name` 必須與目錄名一致 |
| `tests/` | 合成測試；fixtures 只能是合成資料，不得含真實秘密 |
| `scripts/` | repo 層工具（格式驗證、統一檢查腳本） |
| `.github/workflows/` | 本 repo 自己的 CI（唯讀、固定版本） |
| `docs/` | 公開文件：計畫、研究、相容性、架構圖 |

## 動手前必讀

- [docs/plan.md](docs/plan.md)：共用技術契約（CLI、退出碼、安全邊界）。
- [docs/research/portability.md](docs/research/portability.md)：格式與安裝路徑依據。
- [docs/development.md](docs/development.md)：frontmatter 首版子集、驗證指令、CI 與工具版本。
- [docs/compatibility.md](docs/compatibility.md)：可宣稱的相容性範圍與不能宣稱的事。

## 提交前必跑

```bash
bash scripts/run-checks.sh
```

這一條會依序跑格式驗證、單元測試、Bash 語法檢查、Python 語法檢查，
以及 Gitleaks 的**歷史**（所有本機 refs 可達的 commit）與 **staged**（index）掃描。
任一步失敗就回報失敗，不要略過或改寫檢查腳本來讓它變綠。
缺 Gitleaks 時腳本會以退出碼 2 結束並說明掃描不完整——那是誠實的結果，不是可忽略的雜訊。

回報時要分清哪些失敗是自己的、哪些是別人範圍內尚未完成的工作；
兩者都要如實寫出，但不要為了讓自己看起來乾淨而略過或修飾別人的失敗。

## 誠實回報

- 沒有實際跑過的命令不要寫成已驗證。
- 「安裝／格式驗證通過」不等於「agent runtime 已實測」。
- 掃描不完整就不能說沒有問題。
