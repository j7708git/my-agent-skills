> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 工作流程細節

本文件是 `project-workflow` 的深入說明，補足 `SKILL.md` 沒有展開的細節。
所有內容都是流程指引；本 skill 不會代替使用者執行任何寫入或不可逆動作。

## 1. 起始檢查清單

在開始任何規劃或寫入之前，先把現況查清楚：

- workspace 根在哪裡？code-root 與 notes-root 各自是哪個目錄？
- 這次的 project-id 是什麼？研究類任務若沒有程式碼，明確標成 N/A。
- 兩個 repo 各自的 `git status` 為何？**寫入與同步要分開看**：已授權的任務內可以
  照流程寫入；只有同步或上傳前才要求乾淨，dirty／diverged 或可能被覆蓋的他人變更
  才需要停下回報。本任務自己授權範圍內產生的變更不在此限。
- 開工預檢是哪一種模式？`plan` 只讀；`build`／`pm` 的 `prepare` 才 fetch、
  `pull --ff-only`、開分支／worktree；`ship` 才 commit／push／開 PR。
- 目前分支、base SHA、是否要用 worktree？S 用主 checkout 分支；M／L 或並行用
  worktree；分支一律從 `origin/<default>` 開，不引入 `develop`。
- notes repo 是不是 private？不確定就當作「未知」，不要把 notes 上傳。
- 每個 repo 的 remote、branch、以及本機相對遠端的位置（領先／落後／分歧）。
  沒有 remote 時，notes 可在取得建立授權後於本機建立並記錄 pending。
- 有哪些既有檔案（例如 `.local`、`.scratch`）？**先不動它們**。

把以上結果寫進回報或 notes，讓後續每一步都有依據。

### notes repo 的目錄主幹

```text
projects/<id>/overview.md
projects/<id>/status.md
projects/<id>/plan.md
projects/<id>/log/YYYY-MM.md
projects/<id>/research/
projects/<id>/acceptance/
projects/<id>/tasks/
shared/research/
```

- `status.md` 是現況的唯一權威；`plan.md` 只講長期路線；`log/` 只講歷史。
- 舊 `progress.md` 冷凍：不重寫、不刪除、不再追加；只在沒有 `status.md` 的專案
  作 fallback。檔頭要加「歷史紀錄（冷凍）。最新狀態見 `status.md`；新日誌寫在 `log/`。」
- code repo 的 `.gitignore` 要排除暫存；公開的 code repo 放程式碼、測試、README 與對外
  docs，不得含 private 工作筆記。
- workspace 的 `.tmp` 或 code repo 內的 `.tmp` 都應被 ignore；`.tmp` 絕不自動清理。
- **跨機接手時**先讀 `status.md` ＋ 兩個 repo 的 Git 事實，再看 `acceptance/`、任務卡、
  `log/` 最近幾則；需要時才讀 `plan.md`／`research/`。沒有 `status.md` 的舊專案才退回
  `progress.md` 最新段。

## 2. 已知／假設／未知的用法

這三欄不是形式，而是決定「還能不能往下走」的工具：

- 把**已知**寫成可查證的句子，不要寫成印象。
- **假設**一定要可逆，並標明「若假設不成立要怎麼改」。
- **未知**若會影響方向、風險或不可逆成本，就要問；否則先擱置，用預設往前推。

問問題時，一次通常不超過 3 個，並把選項寫清楚，讓使用者能快速決定。
安全相關的例外可以多問，因為安全問題的錯誤代價通常不可逆。

## 3. 原型（prototype）流程

當使用者說「許願」或「先看看」時，目標是**用最小成本換到真實回饋**：

1. 定義這次原型要回答的那一個問題。
2. 做出能被展示、被操作的版本；能跑就跑，能點就點。
3. hardcode、mock、假資料都要在畫面或說明中明示。
4. 不要為了原型先蓋 backend、登入、資料庫或正式部署流程。
5. 展示後，逐項記錄回饋：保留、移除、誤解、新增。
6. 把回饋收斂成正式範圍，再進拆工。

原型是為了降低不確定性，不是為了偷偷把半成品變成正式實作。

## 4. 研究收斂

- 第一輪只做小樣本，先確認報告的 shape（要有哪些欄位、回答什麼問題）。
- shape 經確認後，再擴大到完整範圍，避免白做。
- 來源欄位至少要有：來源、版本、查閱日期。沒有日期的資訊視為不完整。
- 明確區分「事實」「推論」「未確認」。**未找到不等於不存在。**
- 若使用子 agent 的報告，該報告只能當線索；關鍵結論要能自己複查或指出原始來源。

## 5. 任務簡報檢查清單

一份可用的簡報應該讓「沒有你上下文的人或 agent」能獨立完成。逐項確認：

- [ ] 任務類型與目標清楚，完成定義可判定。
- [ ] repo 根與路徑基準都寫了：工作區根、code-root、notes-root；另附開工預檢、
      分支、base SHA 與 worktree（S 用主 checkout；M／L 或並行用 worktree）。
- [ ] 必讀背景以相對路徑列出，並說明原因。
- [ ] 已知／假設／未知都寫了，沒有把不確定性留給 worker 猜。
- [ ] 範圍與 non-goals 都寫了。
- [ ] 行為、輸入輸出與錯誤處理，或研究問題與證據標準寫了。
- [ ] 驗收方法與成果寫了，每條都附驗證方式；另寫 ship 方式（commit → 文字檢查 →
      hooks → push 非預設分支 → draft PR；合併由使用者決定）。
- [ ] 限制與停止條件寫了。
- [ ] 允許寫入的檔案或目錄明確列出。
- [ ] worker 禁止事項寫了：不得超出範圍、不得 `git add .`／`--no-verify`；
      private notes 的 commit 可在自己的 branch／worktree 進行；push 預設仍需
      明確授權，只有該工作區已明確授權時才照 ship 流程 push 非預設分支並開 PR。

## 6. 並行與派工

- 只有彼此獨立、且寫入不同檔案的任務才能並行。
- 同一個任務不要重複派給多個 worker。
- commit 不需事前授權：worker 可在自己的 branch／worktree commit 簡報指定的檔案，
  但仍須用明確檔案清單、跑 hooks、不得 `--no-verify`。
- 共用同一個 notes checkout 時，**一次只有一個 writer**；並行工作請各自用
  branch／worktree，避免搶 index。push 預設仍需明確授權；只有該工作區已明確
  授權時，才照 ship 流程 push 非預設分支並開 PR。
- worker 遇到範圍外的需要，回報後由 PM 決定，不要自行擴大。
- 任務卡的**權威在 notes repo**；任何本機展示副本（例如 client 的計畫面板）
  都只是派生，不是第二權威，也不要雙寫兩份。

## 7. 驗收與交接

驗收時：

- 能用實際指令驗證的，就實際跑並附輸出摘要。
- 完全沒測到的，明說「未測」；不要用推測代替結果。
- 需求被誤解 → 先修簡報；實作有錯 → 直接修實作。
- 「有檔案產出」不等於「已同步」「已驗收」。

交接時，每一輪或每個里程碑更新 notes，至少包含：

- 更新 `status.md` 的 `now`／`next`／`blocker`／`needs`／`updated`。
- 在 `log/YYYY-MM.md` 追加一則（≤10 行），寫日期、做了什麼、證據與下一步。
- 不再追加 `progress.md`。
- code repo 的分支與**真實已存在的 commit**。
- 哪些僅本機、尚未 push。
- 兩個 repo 各自的同步狀態。
- 仍待決定的問題與下一步可行動任務。

## 8. 停止條件

遇到以下情況就停下來回報，不要硬做：

- 同步前兩個 repo 任一有 dirty／diverged，或有不屬於本任務、可能被覆蓋的他人變更。
  （本任務自己授權範圍內產生的變更可以繼續，只要不覆蓋來源不明的內容。）
- 分支 diverged，或 `git pull --ff-only` 無法乾淨套用。
- notes repo 可見度未知，而工作需要動到 notes 或上傳。
- 需要 push、安裝 hooks、改可見度或搬移舊檔案，但尚未取得明確授權。
  （commit 不需事前授權；但 push 預設仍需明確授權，只有該工作區已明確授權時
  才照 ship 流程處理。仍受明確檔案清單與 hooks 限制。）
- 任何隱私掃描 exit 1／2、push 被拒、分支不符、需要 force、hooks 缺失、
  repo 可見度與預期不符、PR body 無法去掉 private 內容、沒有 remote 卻要求 PR。
- 驗收失敗且原因不明，或錯誤超出本次簡報範圍。
- code-root 為 N/A 時不要對不存在的 repo 跑 git；notes repo 尚無 remote 時，
  取得建立授權後可先在本機建立並記錄 pending，等上傳前再做嚴格 private 檢查。

## 9. 開工預檢與分支

預檢分三種模式，Plan 與 Build／PM 的權限不同：

| 模式 | 使用時機 | 可做什麼 |
| --- | --- | --- |
| `plan` | Plan agent 開始規劃前 | 唯讀；不 fetch、不 pull、不開分支 |
| `prepare` | Build／PM 開始開工、派工前 | 可 fetch、`pull --ff-only`、開分支／worktree |
| `ship` | commit／push／PR 前 | 可 commit、push 非預設分支、開 draft PR |

檢查項目：目錄存在、遠端預設分支與 remote、`git status --short --branch`、
`git worktree list`、目前分支與 upstream、遠端主線 SHA、hooks 路徑與檔案、
目前分支是否已有 open PR。`plan` 需要遠端事實時用 `git ls-remote`，不改本機 refs。

分支模型：遠端預設分支是唯一主線（不引入 `develop`）；`feat`／`fix`／`chore`／
`hotfix`／`release` 一律從 `origin/<default>` 開，PR base 是 `<default>`；任務卡
記錄 base SHA。預設分支用 `git ls-remote --symref origin HEAD` 或
`gh repo view --json defaultBranchRef` 偵測，不寫死 `main`。合併預設 squash；
repo 已有慣例時依慣例。發版 tag／Release 走 `release-prep`，不在 ship 授權內。

## 10. Worktree 與 ship

worktree 的選擇：

| 情境 | 做法 |
| --- | --- |
| S（單側、1 個實作檔、無契約變更） | 主 checkout 開分支，不新增 worktree |
| M（單側、2–10 檔） | worktree |
| L（雙側、契約變更、跨 3 子系統） | worktree |
| 並行任務 | 每個任務各自 worktree，即使 S |
| 同一任務已有 worktree | 乾淨且分支相符才重用；否則停 |

建立用 `git worktree add -b <branch> <path> origin/<default>`。一般 git 指令可用
`git -C <worktree>`；**push 一律 `cd <repo 或 worktree> && git push …`**，因為
shell 權限比對整串命令文字，`git -C … push` 不會命中 `git push -u origin feat/*`
的 allow。PR 合併後才移除 worktree、刪本地分支；未合併或關閉的 PR 不刪。刪遠端
分支需 PR 已合併、不是 default、使用者已授權清理。

hooks 由所有 worktree 共用：新 repo 先裝 hooks 再開 worktree；已有多個 worktree
又缺 hooks 時停下來，由使用者決定先移除多餘 worktree 或標為暫不適用。不得用
`--no-verify` 或改 hook 繞過。

ship 的閘門：

1. 分支從 `origin/<default>` 開，任務卡有 base SHA；base 落後不擋 push，
   PR 標「需 rebase」，由使用者決定何時 rebase。
2. 專案檢查與測試已跑，結果可引用。
3. 明確 `git add <檔案>`；commit message 用繁體中文；pre-commit hook exit 0。
4. public 文字檢查：commit message 與 PR body 不得含 private notes 路徑、
   `.local/`、`.openchamber/`、本機絕對路徑、機器名、內部帳號或 private URL；
   對 commit range 與 body file 各跑一次，命中即停。
5. pre-push hook exit 0（掃本次要推的 range）。
6. `cd <repo 或 worktree> && git push -u origin <branch>`；只推非預設分支。
7. `gh pr create --draft --base <default> --head <branch> --body-file <tmp>`；
   tmp 放 workspace 的 `.tmp/`。
8. PR URL、commit SHA、掃描 exit code 寫進 `status.md`／`log/`。

**預設 push 仍需明確授權**；只有使用者已於該工作區明確授權時，才可照上述流程
push 非預設分支並開 draft PR。PR 合併、tag、force、改歷史、改可見度仍由使用者
決定。沒有 remote 或授權不明時只到 commit，回報「僅本機」。
