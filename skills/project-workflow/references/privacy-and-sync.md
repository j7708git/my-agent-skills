> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 隱私分流與同步

本文件說明 `project-workflow` 的公開／私有隔離規則與跨機同步步驟。
它是流程指引；本 skill 不會自行 commit、push、改可見度或搬移檔案。

## 1. 兩條線：公開與私有

| 內容 | 位置 | 版控 | 讀者 |
| --- | --- | --- | --- |
| 使用說明、產品契約、教學、架構成品 | code repo 的 README 與 docs | 公開 | 所有人 |
| 程式碼與測試 | code repo | 公開 | 所有人 |
| 完整計畫、現況、歷史、研究、驗收、任務卡 | notes repo 的專案目錄與 `shared` | private | 僅協作者 |
| 暫存與一次生產物 | workspace 的 `.tmp` | 忽略 | 無 |

判準很簡單：這是「專案對外的知識」還是「我怎麼做出來的過程」？
前者蒸餾成**自足**內容放公開 repo；後者放 private notes。

## 2. 資料分級：所有 repo 都禁 vs 只有公開禁

**任何 repo（public 或 private）都不得出現**：

- 憑證、token、密碼、私鑰。
- 真名、私人 email、電話、帳號 ID。
- 內網位址、主機名。
- 個人絕對路徑（例如本機家目錄的完整路徑）。

**只有公開內容額外禁止**（private 可保留必要的工作過程）：

- 把內部工作過程（派工紀錄、session 日誌、未公開維運細節）整份搬進公開文件。
- 指向 private 筆記具體檔案、某次特定 CI 執行或舊 commit 的連結。

private notes **可以**記錄必要的 commit 參照與**相對的內部引用**（例如進度指向同一個
notes repo 內的計畫或任務卡），這是交接所需；但仍不得寫入上面第一組憑證與 PII。

需要舉例時，一律用 placeholder，例如 `<workspace>`、`example@example.invalid`。

## 3. 舊目錄不自動處理

- 既有的 `.local`、`.scratch` 或其他舊工作紀錄目錄：**原地保留、不移動、不刪除、不改名**。
- 只有使用者明確授權時，才做一次搬遷，且流程必須是：
  1. 先盤點（列出有什麼）。
  2. 再分類（公開知識／私有工作紀錄／可丟棄）。
  3. 最後才複製到新位置。
- **未逐項核對並驗收前，不得聲稱已完成**；原件保留，清理另行授權。

## 4. 索引紀律

- commit **不需要事前取得使用者授權**；agent 判斷到達 checkpoint 就可以 commit。
  但仍須用**明確檔案清單**、跑 hooks、不得 `--no-verify`。
- 並行的 worker 在**各自的 branch／worktree** commit；共用同一個 notes checkout 時，
  **一次只有一個 writer**，避免搶 index。
- 公開 repo 絕不 stage notes、本機設定或 `.env` 之類的檔案。
- 一個 worker 一個明確檔案清單；不要用「全部加入」的方式整理索引。
- **push 預設仍需明確授權**；只有該 workspace 已明確授權時，才照 ship 流程
  push 非預設分支並開 PR。不 `--force`、不改寫歷史、不改可見度。

## 5. 同步步驟

**寫入與同步分開**：使用者已授權的任務內可以照流程寫入，不必因為 repo 有變更就停；
但**同步或上傳之前**必須先查核。

同步前，先在**兩個 repo 分別**做只讀查核：

1. `git status`：同步前若有 dirty、diverged，或可能被覆蓋的他人變更，就停下來回報；
   本任務自己授權範圍內產生的變更可以繼續，只要不覆蓋來源不明的內容。
2. `git remote`：確認遠端，以及本機分支相對遠端的領先／落後狀態。
3. 需要更新時只用 `git pull --ff-only`。
4. 若 dirty／diverged **且需要同步**，就停下回報，不自動 stash、reset、rebase
   （本任務授權範圍內產生的變更不受此限）。

其他情況：

- code-root 是 N/A（研究類任務沒有程式碼）時，不要對不存在的 repo 跑 git。
- notes repo 本機尚無 remote 時，可在取得建立授權後於本機建立並記錄為 pending；
  **只在上傳前**做嚴格的 private 檢查。
- 不確定可見度時，不要上傳；也不要自動變更任何 repo 的可見度。

push 的注意事項：

- 兩個 repo 的 commit **無法原子化**，各自獨立成功或失敗。
- push 時要**分別回報** public 與 private 的結果；程式碼未 push 要寫清楚。
- 未 push 的 commit 只在本機可見，不能假設協作者已能取得。
- 不 `--force`、不改寫歷史。
- commit 依本流程可在 checkpoint 直接進行；**push 預設仍需明確授權**。
  只有使用者已於該工作區明確授權時，才照 ship 流程 push 非預設分支與 notes 的
  default 分支。建立檔案或寫筆記不等於已同步。

## 6. 交接要寫什麼

每一輪或每個里程碑，在 notes 更新：

- `status.md` 的 `now`／`next`／`blocker`／`needs`／`updated`。
- `log/YYYY-MM.md` 追加一則（≤10 行），寫日期、做了什麼、證據與下一步。
- 不再追加 `progress.md`；它只作舊專案的 fallback。
- code repo 的分支與 commit（真實已存在，不是預期值）。
- 哪些只在本機、尚未 push。
- 兩個 repo 各自的同步狀態、仍待決定的問題與下一步。

## 7. 常見錯誤

- 把 private notes 的具體連結放進公開文件（死連結，且可能洩漏內部資訊）。
- 把「建了檔案」說成「已同步」。
- 在可見度未知時就把 notes 上傳。
- 用 `git add .` 或 `git add -A`，把 `.env`、暫存檔或私有內容一起帶進索引。
- 未經授權就安裝 hooks、改可見度或改寫歷史。
- 把「權限 allow」當成「已授權」；兩者是不同層次的事。

## 8. 權限三段與 ship 閘門

權限規則的順序固定是 **`ask → allow → deny`**，deny 必須放在最後；OpenCode
的規則是 last-match-wins。**base policy 是 `* allow`，未命中不是 ask**：
沒列出的命令會落到 allow，所以危險命令要逐條列 deny。`ask` 與 `deny` 只有在
真正命中時才生效。

- **ask**：裸 `git push`、`git push *`、`git * push *`（旗標前置或 `-C` 形式）、
  `gh pr merge *`、`gh pr close *`、`gh pr reopen *`。
- **allow**：`git push -u origin feat/*`（以及 fix／chore／hotfix／release）、
  `gh pr create *`、`gh pr view *`、`gh pr checks *`、`gh pr ready *`、
  `gh pr list *`、`gh pr edit *`、`gh pr comment *`。
- **deny**（最後）：推 main／master／develop 與其變體、force、`--mirror`、
  `--all`、`--tags`、`--prune`、`--no-verify`、`core.hooksPath`、`gh repo delete`、
  `gh repo edit`。

`gh pr` 要逐個子命令寫，不可寫 `gh pr *`，否則會連 `gh pr merge` 一起放行。
push 一律用 `cd` 進 repo／worktree 的標準形式；`git -C … push` 只會停在 ask，
不會命中 allow。**權限 allow 只是技術護欄，不構成授權**；沒有 workspace 的明確
授權，agent 仍須逐次問。

ship 的閘門：分支從 `origin/<default>` 開且任務卡有 base SHA；測試可引用；
明確 `git add`、繁中 commit、pre-commit exit 0；public 文字檢查通過；
pre-push exit 0；只推非預設分支；開 draft PR。base 落後不擋 push，但 PR 要標
「需 rebase」，由使用者決定何時 rebase。任何掃描 exit 1／2、push 被拒、分支不符、
需要 force、hooks 缺失、可見度不符或 PR body 去不掉 private 內容，都要停止回報。

PR body 至少要有：目標、變更摘要、驗證指令與結果、隱私掃描 exit code、
風險與回滾、已知限制。不得連結 private notes 的具體檔案。

## 9. Notes 的自動 push 例外

- notes repo 只在它的預設分支寫入（目前多為 `main`），不開 notes 分支／worktree；
  需要隔離時，同一個 checkout 一次只有一個 writer。
- 變更前先 `git status`。無關的 dirty／untracked 只警告、不擋，但不得 stage；
  `git pull --ff-only` 若會覆蓋那些路徑就停。
- 明確檔案 commit、pre-commit hook exit 0 後，才可用受保護的 notes push 工具
  （由 workspace 安裝，安裝副本不可被 agent 隨意改寫）push notes 的預設分支。
- 工具要確認 repo 是 notes、分支是 notes 的 default、remote 可見度 private、
  已 ff-only；完整性無法驗證（副本不存在、雜湊不符、被改寫）時退回 `ask`。
- push 被拒通常是 diverged；`pull --ff-only` 無法解決。**停止並回報**，
  由使用者決定 merge／rebase；本流程不自動做。
- code 與 notes 的 push 分開回報；未推的寫「僅本機」。
