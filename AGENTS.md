# 在 my-agent-skills 內工作的 agent 須知

本檔是這個 repo 對 coding agent 的工作規範。人類維護者請看 [CONTRIBUTING.md](CONTRIBUTING.md)；
使用這個 repo 的 skill 的人請看 [README.md](README.md)。

## 這個 repo 是什麼

- 標準 [Agent Skills](https://agentskills.io/specification) 的集合 repo，名稱 `j7708git/my-agent-skills`。
- 目前有九個 skill：
  - `skills/repo-privacy-audit/`：唯讀的 repo 隱私與秘密掃描。它的**唯讀安全邊界不變**，
    不自動修復、不改寫歷史、不推送、不動任何 GitHub 設定。
  - `skills/project-workflow/`：公開程式碼 repo 與獨立 private 工作紀錄 repo 的流程指引。
    它**只提供指引**，不會自動授權或執行任何寫入、commit、push、安裝 hooks、調整可見度
    或搬移檔案；載入它不等於授權。
  - `skills/web-ui-e2e-smoke/`：Web UI 端到端冒煙驗收的流程指引（真實建置的產物、
    可達後端、真實瀏覽器驅動、截圖留證）。
  - `skills/independent-rerun-verify/`：獨立重跑驗證的流程指引（以現行原始碼自行建置、
    修前重現、對照組證明斷言抓得到 bug、還原一致）。
  - `skills/review-fix-round/`：審查修復輪協定的流程指引（findings 分級、逐項裁決、
    限定範圍修復、含對照組的驗收、複審與裁決紀錄）。
  - `skills/academic-paper-search/`：以 arXiv API 做結構化論文檢索的**選配**工具
    （純 Python 3 標準庫，附 `scripts/arxiv_search.py` 與兩份 references）。
    它只提供檢索，不下載或解析全文、不自動 commit／push，也不提供安全保證；
    由 `adapters/opencode/` 的 `researcher` 與 `web-search` **條件式引用**
    （環境已安裝本 skill 才載入，未安裝退回一般網路搜尋）。
  - `skills/native-crash-triage/`：原生崩潰報告的證據處理流程（去識別、以官方符號
    逐格還原、跨案比對、單變數對照實驗、分層筆記）；缺符號或 frames 被截斷時
    要求停在原步回報，不用位址猜函式名。
  - `skills/release-prep/`：發版整備清單（版本號同源、文件同步、本地基準比對、
    推送前掃描、標籤、CI 監看、Release 驗證與紀錄）；提交、推送、標籤、Release
    各自需要明確授權。
  - `skills/handoff-refresh/`：交接與現況文件的重生對帳（以進度紀錄與 Git 事實逐條比對
    宣稱、只改現況段、回報差異與未解項）；它不改歷史段，也不 commit／push。
  - `project-workflow` 與 `web-ui-e2e-smoke`／`independent-rerun-verify`／
    `review-fix-round`／`native-crash-triage`／`release-prep`／`handoff-refresh`
    都是**純文字流程指引**：不下載或安裝工具、不自行 commit／push，
    也不提供任何安全保證；載入它們不等於授權。
- 平台專屬 agent（OpenCode 的 agent／command 定義）屬於**選配**，收錄在
  `adapters/opencode/`：目前包含 `project-workflow` 配套的 `pm`／`researcher`／
  `implementer`／`tester`／`tester-verify`、派工前的選配計畫審查 `plan-reviewer`、
  獨立審查者 `reviewer`／`reviewer-deep`、
  `workflow-optimizer` 健檢 agent、`translator` 翻譯官（把抽象願望問成 brief、
  核可後交給 PM，並把 PM 的回覆翻成白話）、獨立的 `web-search` agent，與
  `/wish`／`/proto`／`/research`／`/optimize` 指令；發行版本刻意移除所有
  `model:` 綁定，安裝者依自己環境自選模型。安裝說明見
  `adapters/opencode/README.md`。其他平台
  （Claude Code／Codex）的 adapters 尚未收錄，等有真實需求再新增，不要先放空殼。

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
   專案已有的暫存工具可以直接使用。需要新工具時說明缺少什麼、退出碼為何，
   由使用者決定是否取得。
6. **不新增相依套件**：掃描核心只用 Bash 與 Python 3 標準函式庫。安裝工具（skills CLI）只是可選安裝路徑。
7. **不自動改 GitHub 設定**：CI 只用唯讀權限；branch protection、required checks 等由維護者手動設定。
8. **不自行 commit／push，除非使用者明確授權**：
   - 未獲明確授權時，不 commit、不 push、不建立 remote、不改分支保護設定。
   - 已獲明確授權的範圍內則照做，例如：使用者已同意的首次發布流程
     （建立 remote、初始提交、push），或使用者已同意的驗收流程步驟。
   - 授權範圍之外的動作（例如把 repo 轉 public、`--force` 推送、歷史改寫）仍需另外取得授權。
9. **不改 repo 的可見度**：可見度是維護者的決定。文件不要把「private」或「public」
   寫死成永久事實，也不要聲稱已轉為某個可見度；以當下 GitHub 設定為準。
10. **流程指引不等於授權，也不是掃描器**：`skills/project-workflow/` 只提供工作流程指引；
    載入它不構成任何寫入、commit、push、hooks 安裝、可見度調整或搬移既有檔案的授權。
    它不做隱私掃描，也不提供安全保證。舊的 `.local`／`.scratch` 不因此 skill 而自動遷移。

## 文件分流

**公開知識進 `docs/`，工作過程留在本機。** 兩條路互不混用：

| 內容 | 位置 | 版控 |
| --- | --- | --- |
| 使用說明、產品契約、維護指南、相容性、可分享研究、架構成品 | `docs/` | 進版控 |
| 派工用的任務計畫卡 | `.openchamber/plans/` | 忽略 |
| 完整計畫、todo、session 日誌、驗收紀錄、研究筆記、視覺檢查 sidecar | `.local/` | 忽略 |

規則：

1. **預設工作筆記不發布。** 把筆記變成公開文件前先依用途審核：讀者是誰、
   會不會連到本機路徑、會不會揭露尚未公開的資訊。然後改寫成**自足**的文件再放進 `docs/`。
2. **公開文件不得連結 `.local/` 或 `.openchamber/` 裡的某一份內部筆記**，
   也不得內嵌派工紀錄、個人主機路徑、session 日誌片段、舊 commit SHA
   或指向特定歷史執行的 CI 連結。
   **但公開文件可以談論 `.local/` 這個目錄名本身**——例如說明分流政策、
   說明 sidecar 放哪裡。這是解釋規則，不是連到某份內部文件；
   兩者的差別在於有沒有指向一個具體檔案。驗證器會擋下任何指向這兩個目錄的
   Markdown 連結，所以具體連結一定會被抓到。
   產品行為要公開，就蒸餾成契約寫進 [docs/specification.md](docs/specification.md)，
   不要把執行日誌整份搬過去。
3. **維護測試步驟是公開產品文件**，不要為了分流把它藏起來：怎麼跑檢查、
   需要哪些工具版本、退出碼怎麼解讀、哪些不能宣稱——這些留在 `docs/`。
4. **未來要升級 agent 規則的提案留在 `.local/`**，不要動全域 agent 設定檔；
   提案成熟後再由維護者決定要不要發布或上移。
5. **外部使用者不該被要求擁有本機工作區。** 公開文件不能說「先讀 `.local/` 的某檔案」；
   需要引用公開知識時，引用 `docs/` 裡對應的公開文件。
6. `.local/` 與 `.openchamber/` 已被 `.gitignore` 排除；被 Git 追蹤就是分流失效，
   `python3 scripts/validate_skills.py --check-docs --strict` 會報錯。

## 目錄职责

| 路徑 | 內容 |
| --- | --- |
| `skills/<name>/` | 可安裝的 skill；`SKILL.md` 的 `name` 必須與目錄名一致 |
| `tests/` | 合成測試；fixtures 只能是合成資料，不得含真實秘密 |
| `scripts/` | repo 層工具（格式驗證、統一檢查腳本） |
| `.github/workflows/` | 本 repo 自己的 CI（唯讀、固定版本） |
| `docs/` | 公開文件：規格、維護指南、相容性、架構圖、可分享研究 |
| `.local/` | 本機工作紀錄（計畫、todo、session 日誌、驗收紀錄、研究筆記），不進版控 |
| `.openchamber/` | 派工任務計畫卡，不進版控 |

## 動手前必讀

- [docs/specification.md](docs/specification.md)：產品技術契約（CLI、退出碼、輸出、安全邊界）。
- [docs/project-workflow.md](docs/project-workflow.md)：`project-workflow` 流程指引的公開契約與邊界。
- [docs/research/portability.md](docs/research/portability.md)：格式與安裝路徑依據。
- [docs/development.md](docs/development.md)：frontmatter 首版子集、驗證指令、文件分流、CI 與工具版本。
- [docs/compatibility.md](docs/compatibility.md)：可宣稱的相容性範圍與不能宣稱的事。

這些是公開文件，足以開始工作；不需要（也不應該）讀本機工作紀錄才能理解產品。

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
