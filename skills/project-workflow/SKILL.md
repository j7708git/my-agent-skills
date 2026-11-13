---
name: project-workflow
description: 引導在單一工作區內協調「公開程式碼 repo」與「獨立 private 工作紀錄 repo」的專案流程：以每個專案一份 status.md 管現況、log 管歷史、Git 管分支與 push，依 S／M／L 車道分級，撰寫自足任務簡報、派工與並行、實際驗收、跨機同步與交接。當使用者要開新專案、把想法變成計畫、先看原型、拆工派工、寫任務卡、做驗收，或討論 Git 同步、可見度與授權邊界時使用。這個 skill 只是流程指引，不是掃描器，也不自行建立 repo、push、安裝 hooks 或搬移檔案。
license: MIT
compatibility: 純文字流程指引，無執行時相依，也不需要特定工具或模型；任何能讀取檔案的 agent client 都可用手動載入的方式使用。
---

> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# project-workflow

這是一個**流程指引** skill，描述如何在使用者的工作區內，把「公開的程式碼 repo」
與「獨立的 private 工作紀錄 repo」分開管理，並用一致的方式規劃、拆工、驗收與同步。

- 它**不是**掃描器，不做隱私掃描，也不執行任何掃描指令。
- 它是**純文字指引，不是自動腳本**，不會自己執行任何動作。但在使用者已明確授權的
  任務範圍內，執行 agent 可以照本流程寫入與實作，不必每個細節都停下來重問；
  未被授權的動作則不得自行執行。
- 它**不會自行**建立 repo、安裝 git hooks、調整可見度或搬移既有檔案。commit 依
  索引紀律可在 checkpoint 直接進行；**push 預設仍需明確授權**，只有使用者已於該
  工作區明確授權時，執行 agent 才可照「Ship」一節 push 非預設分支並開 draft PR；
  **PR 合併仍由使用者決定**。
- **載入這個 skill 不等於授權任何寫入或不可逆動作。** 需要寫入、同步或發布時，
  照「授權邊界」一節處理。

## 名詞與路徑基準

先確認以下四個值，後續所有指示都以它們為基準；不確定就先問，不要猜。

| 名詞 | 意義 | 預設／慣例 |
| --- | --- | --- |
| workspace（工作區根） | 所有專案子目錄的上層 | 由使用者指定；子專案各自在底下開子目錄 |
| code-root | 某專案的公開程式碼 repo 根 | `<workspace>/<project-id>`；一專案一子目錄 |
| notes-root | 該專案的私有工作紀錄 repo 根 | 預設目錄名 `project-notes`，可依使用者配置改名 |
| project-id | notes repo 內對應此專案的目錄名 | lowercase kebab-case，與 code 子目錄名對齊 |

要點：

- 一個**專案**對應一個 code 子目錄（不是每個派工子任務都新建 repo）；**研究類任務可能
  沒有程式碼**，此時 code-root 記為 N/A，只保留 notes。
- notes-root 是**獨立的 private Git repo**，不是 code repo 的子目錄；它自己的 repo 內，
  以 `<project-id>` 命名的專案目錄與共用的 `shared` 目錄放內部紀錄。
- notes-root 的目錄名只是**預設值**。使用者若配置成其他名稱，就以使用者配置為準，
  不要在文件或指令裡把它寫死。
- 路徑一律以**工作區根**為基準解析，不要依賴子 session 當下的工作目錄。

## 單一現況，歷史分開

同一個問題只能有一個權威。其他都是投影或提示。

| 問題 | 權威 | 不是權威 |
| --- | --- | --- |
| 現在做什麼、卡在哪、需不需要使用者決定 | `<id>` 下的 `status.md` | `progress.md`、知識面板、dashboard |
| 分支、HEAD、是否已 push、遠端是否存在 | **Git 即時查詢** | `status.md` 的 `branch` 提示 |
| 這次任務的規格與驗收條件 | `<id>` 下的 `tasks` 任務卡 | `plan.md`、對話紀錄 |
| 驗收證據 | `<id>` 下的 `acceptance` 目錄 | `status.md` 的完成宣稱 |
| 歷史 | `<id>` 下的 `log` 當月檔（`YYYY-MM.md`）；舊 `progress.md` 冷凍 | dashboard、知識面板 |
| 長期路線圖 | 舊 `<id>` 下的 `plan.md` 或新 `<id>` 下的 `plans` 目錄，**每專案擇一** | 兩者並存 |
| 公開知識 | 程式碼 repo 的 `docs` 目錄 | private notes |

### `status.md` 的最小 schema

- UTF-8、LF；第 1 行是 `# <專案名稱>`；接著是扁平的 `key: value`。
- 必填：`schema: 1`、`state`（`planning`／`active`／`paused`／`blocked`／`done`）、
  `needs`（`none`／`user`／`external`）、`now`、`next`、`blocker`（沒有就寫 `none`）、
  `updated`（ISO 8601 含時區）、`code_dir`（相對工作區根，或 `no-code`）。
- 選填：`machine`（未指派寫 `unknown`）、`branch`（只是提示）、
  `local_claim_by`／`local_claim_session`／`local_claim_updated`（三個要一起出現；
  **本機提示，不是跨機鎖**；超過 24 小時視為過期）。
- `## 需要你決定` 選填，固定用 `- [ ]`／`- [x]`。
- 白話寫 `now`／`next`，不要只寫代號；`status.md` 不描述歷史。

### Fallback

- 有 `status.md`：先讀 `status.md`＋Git 事實。
- 沒有 `status.md`：才讀 `progress.md` 最新段＋Git 事實（遷移期相容）。
- `status.md` 與 Git 衝突：**Git 事實優先**；`branch`／`HEAD`／push 欄位只是提示。
- `status.md` 與 `progress.md`／`overview.md` 衝突：**停止並對帳**，不要自己挑一份。
- 舊 `progress.md` 不重寫、不刪除；檔頭加一行
  `> 歷史紀錄（冷凍）。最新狀態見 status.md；新日誌寫在 log 目錄。` 之後停止追加。

### `log` 目錄 的格式

新日誌從導入當天開始，檔名 `YYYY-MM.md`。每則 ≤10 行，至少寫：

- 日期與做了什麼（白話）。
- 對應的 code commit／版本；未 push 的要寫「僅本機」。
- 驗收證據（哪份 acceptance、哪個指令、exit code）。
- 下一步或卡點。

## 車道：小事、一般、高風險

安全邊界（隱私掃描、不可逆操作、授權）是地板，任何車道都不豁免。

| 車道 | 判準（可從任務卡與 diff 驗證） | 預期驗收 |
| --- | --- | --- |
| **S** | 單一子系統，0–1 個實作檔，無契約變更 | 1 輪 |
| **M** | 單一子系統，2–10 個實作／測試檔，可有 additive 契約欄位與文件同步 | 1–2 輪 |
| **L** | ≥11 個實作／測試檔，或跨 ≥2 個子系統，或既有契約 breaking 變更（改語意／資料形狀） | ≥2 輪，常含修正輪 |

- **子系統預設**：頂層原始碼模組（排除 `tests`／`docs`／`config`）；專案可另定義，新專案用預設。
- **檔數計算**：S 以實作檔計；M／L 計實作＋測試檔、不計文件；`test-only` 依同檔數帶歸類（0 impl＋1 test＝S、0 impl＋10 tests＝M）；`docs-only`（0 impl＋0 test）＝S。

檔案數以**任務範圍**計，不以合併 commit 計；任務與其他版本合併、無法拆分時，
分類依任務範圍，檔案數記「不可得」，不要拿合併 commit 的檔案數硬套車道。

判準用途是決定**驗收強度與派工數**，不是拿來省掉必要步驟。實際量測顯示
「diff 大小不預測成本，驗收輪數才是主因」：小任務若驗收抓到問題，一樣會多出
修正與複驗。

## 分支、開工預檢與 worktree

**主線只有一條。** 遠端預設分支（多數 repo 是 `main`，也可能是 `master`）是唯一主線，
**不引入 `develop`**。預設分支用 `git ls-remote --symref origin HEAD` 或
`gh repo view --json defaultBranchRef` 偵測，**不要寫死 `main`**。

| 分支 | 用途 | 起點 | PR base |
| --- | --- | --- | --- |
| `<default>` | 唯一主線，永遠可發布 | — | — |
| `feat` 前綴 | 新功能 | `origin` 的 `<default>` | `<default>` |
| `fix` 前綴 | 非緊急修 bug | `origin` 的 `<default>` | `<default>` |
| `chore` 前綴 | 文件、工具、設定 | `origin` 的 `<default>` | `<default>` |
| `hotfix` 前綴 | 正式線緊急修復 | `origin` 的 `<default>` | `<default>` |
| `release` 前綴 | 需要穩定期才開 | `origin` 的 `<default>` | `<default>` |

- 分支名用 `<prefix>/<task-id>-<slug>`；prefix 是 feat／fix／chore／hotfix／release。
- 一律從 `origin` 的 `<default>` 開，不從本機主線開；任務卡記錄 base SHA。
- 分支名稱只用小寫 ASCII、數字、`-`、`/`；同一個任務不要重用不同分支。
- 合併預設 squash；repo 已有慣例時依慣例（看 `.github` 既有 PR，或直接問使用者）。
- 發版標籤與 Release 走 `release-prep` 的較嚴格授權，**不包含在 ship 的 push 授權內**。

### 開工預檢（plan／prepare／ship）

| 模式 | 使用時機 | 可做什麼 |
| --- | --- | --- |
| `plan` | Plan agent 開始規劃前 | **唯讀**：不 fetch、不 pull、不開分支 |
| `prepare` | Build／PM 開始開工、派工前 | 可 fetch、`pull --ff-only`、開分支／worktree |
| `ship` | commit／push／PR 前 | 可 commit、push 非預設分支、開 draft PR |

預檢依序查：目錄存在、遠端預設分支、`git status --short --branch`、
`git worktree list`、目前分支與 upstream、遠端主線 SHA、hooks 是否在
（`git rev-parse --path-format=absolute --git-path hooks`）、目前分支是否已有
open PR。`plan` 需要遠端事實時用 `git ls-remote`，不修改本機 refs。

決策原則（只適用 code repo）：

- 乾淨、在 default、落後：`prepare` 用 `git pull --ff-only` 對齊；`plan` 只回報。
- 乾淨、在 default、領先或 diverged：**停**，不 reset。
- dirty／untracked：`plan` 可讀但標「開工前先清理」；`prepare` **停**，不 stash。
- 在 feature branch 落後：回報，不自動 rebase／merge。
- 分支已在 origin、本機沒有：先確認是否另一台機器在做。
- 已有 open PR：續用同一分支，不重開 PR。
- 沒有 remote：開 local branch，不 push；ship 只到 commit。
- 缺 hooks、detached HEAD：**停**。

### Worktree 政策

| 情境 | 做法 |
| --- | --- |
| S（單一子系統、0–1 個實作檔、無契約變更） | 主 checkout 開分支，不新增 worktree |
| M（單一子系統、2–10 個實作／測試檔、可 additive） | worktree |
| L（≥11 檔、跨 ≥2 子系統、或契約 breaking） | worktree |
| 並行任務 | 每個任務各自 worktree，即使 S |
| 同一任務已有 worktree | 乾淨且分支相符才重用；否則停 |

- 建立：`git worktree add -b <branch> <path> <remote>/<default>`。
- 執行：一般 git 指令可用 `git -C <worktree>`；**push 一律
  `cd <repo 或 worktree> && git push …`**。shell 權限比對的是整串命令文字，
  `git -C … push` 不會命中「`git push -u origin` ＋非預設分支」的 allow。
- 清理：PR 合併後才 `git worktree remove`、刪本地分支；未合併或關閉的 PR 不刪。
  刪遠端分支要三條件齊備：PR 已合併、不是 default、使用者已授權清理。
- **hooks 與 worktree**：hook 目錄由所有 worktree 共用。新 repo 先裝 hooks 再開
  worktree；已有多個 worktree 又缺 hooks 時**停**，由使用者決定先移除多餘
  worktree，或把該 repo 標為暫不適用。不得用 `--no-verify` 或改 hook 繞過。

## Ship：commit → push → PR

### 授權前提

- **預設 push 仍需明確授權。** 這個 skill 本身不授予任何授權；載入 skill、寫了
  任務簡報、或權限設定 allow，都不等於授權。
- 只有**使用者已於該工作區明確授權**（例如 workspace 的 `AGENTS.md` 寫明允許）時，
  執行 agent 才可照本節 ship：push **非預設分支**並開 draft PR。
- 即使已授權，**PR 合併、tag、force push、改歷史、改可見度仍由使用者決定**。
- 沒有 remote、或授權狀態不明：只到 commit，回報「僅本機」。

### 閘門（全部通過才 push）

1. 分支從 `origin` 的 `<default>` 開，任務卡記錄 base SHA；base 落後不擋 push，
   PR 標註「需 rebase」，由使用者決定何時 rebase。
2. 專案檢查與測試已跑，結果可引用。
3. 明確 `git add <檔案>`；commit message 用繁體中文；pre-commit hook exit 0。
4. public 文字檢查：commit message 與 PR body 不得含 private notes 路徑、
   `.local`、`.openchamber`、本機絕對路徑、機器名、內部帳號或 private URL。
   固定 pattern 至少涵蓋 private notes 目錄名、`.local`、`.openchamber` 與
   本機絕對路徑；對 commit range 與 body file 各跑一次，命中即停。
5. pre-push hook exit 0（掃本次要推的 range）。
6. `cd <repo 或 worktree> && git push -u origin <branch>`；只推非預設分支。
7. `gh pr create --draft --base <default> --head <branch> --body-file <tmp>`；
   tmp 放 workspace 的 `.tmp`。
8. PR URL、commit SHA、掃描 exit code 寫進 `status.md` 與 `log`。

### PR 內容

- 目標、變更摘要、驗證指令與結果、隱私掃描 exit code、風險與回滾、已知限制。
- 不得連結 private notes 的具體檔案；任務標題與非敏感 ID 可以寫。
- 先 draft；tester／PM 驗收通過後才 `gh pr ready`。**合併由使用者決定。**
- 公開 repo 不開早期 WIP draft；等任務完成且閘門通過才開。私有 repo 可在第一個
  可運行 checkpoint 先推 draft。

### 停止條件

任何掃描 exit 1／2、push 被拒、分支不符、需要 force、hooks 缺失、repo 可見度與
預期不符、PR body 無法去掉 private 內容、沒有 remote 卻要求 PR：停止回報，
不用 `--no-verify`、不 force、不改歷史。

## Notes 的 push 例外

- notes repo 只在它的預設分支寫入（目前多為 `main`），**不開 notes 分支／worktree**；
  需要隔離時，同一個 checkout 一次只有一個 writer。
- 變更前先 `git status`；無關的 dirty／untracked 只警告、不擋，但不得 stage。
  `git pull --ff-only` 若會覆蓋那些路徑就停。
- 明確檔案 commit、pre-commit hook exit 0 後，才可用受保護的 notes push 工具
  （由 workspace 安裝並限制可改寫性）push notes 的預設分支。
- 工具會確認 repo 是 notes、分支是 notes 的 default、remote 可見度 private、
  已 ff-only；完整性無法驗證時退回人工確認。
- push 被拒通常是 diverged；`pull --ff-only` 無法解決。**停止回報**，
  由使用者決定 merge／rebase；本流程不自動做。
- code 與 notes 的 push 分開回報；未推的寫「僅本機」。

## 私有筆記的目錄主幹

專案目錄的主幹：

- `<id>` 下的 `overview.md`、`status.md`、`plan.md`
- `<id>` 下的 `log` 目錄（每月一檔 `YYYY-MM.md`）
- `<id>` 下的 `research`、`acceptance`、`tasks` 目錄
- shared 下的 `research` 目錄

- code repo 的 `.gitignore` 應排除暫存；公開的 code repo 放程式碼、測試、README 與對外
  docs，**不得**含 private 工作筆記。
- workspace 或 code repo 內的 `.tmp` 都應被 ignore，且**絕不自動清理**。
- **跨機接手時**的閱讀順序：`status.md` → 兩個 repo 的 Git 事實 → `acceptance` 目錄 →
  任務卡 → `log` 目錄 最近幾則；需要時再讀 `plan.md`／`research` 目錄。
  只有在沒有 `status.md` 的舊專案，才退回讀 `progress.md` 最新段。

## 隱私分流（公開與私有嚴格隔離）

| 內容 | 位置 | 版控 |
| --- | --- | --- |
| 使用說明、產品契約、教學、架構成品 | code repo 的 README 與 docs | 公開版控 |
| 程式碼與測試 | code repo | 公開版控 |
| 完整計畫、現況、歷史、研究、驗收、任務卡 | notes repo 的專案目錄與 `shared` | private 版控 |
| 暫存與一次生產物 | workspace 的 `.tmp` | 忽略 |

- **private 只降低公開程度，不是機密保管庫。** 憑證、真名、私人 email、帳號 ID、
  內網位址與個人絕對路徑，一律不得寫進任何 repo（public 或 private）。
- 公開文件必須**自足**：不得連到 private 筆記的具體檔案，也不得要求讀者先擁有
  私有工作紀錄才能理解。
- `.tmp` 是忽略的暫存區，但**臨時不等於可以隨便刪**；要清理前先確認內容。
- 舊的 `.local` 與 `.scratch`（若存在）**不自動遷移、不改名、不刪除**。
  只有使用者明確授權時，才做一次「先盤點、再分類、最後複製」的遷移；
  **未逐項核對並驗收前，不得聲稱已完成**。原件保留，清理另行授權。

## 動手前：狀態與可見度查核

**寫入與同步是兩件事。** 在使用者已明確授權的任務範圍內，可以照流程寫入與實作，
不需要每個細節都停下來重問；但**同步或上傳之前**必須先查核狀態。

1. 對 **code repo 與 notes repo 分別**執行 `git status`（只讀）。若 code-root 是 N/A
   （研究類任務沒有程式碼），就不要對不存在的 repo 跑 git。
2. **同步前**若發現 dirty、diverged，或有不屬於本任務、可能被覆蓋的使用者變更，
   就停下來回報；不要自動 stash、reset、rebase 或覆蓋來源不明的工作。
   **本任務自己授權範圍內產生的變更可以繼續**，只要不覆蓋不確定的內容。
3. 查核 **remote 可見度**：不確定 notes repo 是否為 private 時，**不要上傳 notes**。
   不要把「可見度未知」當成「可以上傳」；也不要自動變更任何 repo 的可見度。
4. notes repo 若本機尚無 remote，可在**取得建立授權後**於本機建立並記錄為 pending；
   **只在上傳前**做嚴格的 private 檢查。
5. 確認 `git remote` 是否存在，以及本機分支相對遠端的狀態；清楚區分「僅本機可見」
   與「遠端已存在」。

需要一眼看懂現況時，可以從 `status.md` 與 Git 產生**唯讀 dashboard**（單檔 HTML，
不寫 repo、不連外、不顯示絕對路徑），固定回答三件事：這台機器看到的「現在」、
誰在做（本機 claim 提示）、哪些已完成（`tasks` 目錄 與 `acceptance` 目錄 的實際檔案）。
Dashboard 是投影，不是第二權威；不能 commit，也不能取代 `status.md`。

技能被載入、讀檔或產生報告，都不構成對上述動作的授權。

## 規劃流程（PM 角色）

以「已知／假設／未知」三欄開始，先把不確定性攤開：

- **已知**：使用者已明說、或可從現況直接查證的。
- **假設**：可逆的預設決定，記下來讓使用者能推翻。
- **未知**：還沒回答、且會影響方向或風險的。

提問紀律：**只有會影響方向、涉及風險或不可逆成本的事才問**，通常一次不超過 3 個問題
（安全例外可再問）。可逆的細節用合理預設處理，並在回覆中標明「這是我的假設」。

使用者若是「許願」，或明說要先看，就**先做 minimal prototype**：

1. 做出可以被實際展示、操作的東西，而不是抽象描述或純規格。
2. 原型內若用了 hardcode、mock 或假資料，**必須明示**，不要假裝是真實功能。
3. 不預設要有 backend、登入或資料庫；先只做能回答問題的最小範圍。
4. 取得回饋：哪些保留、哪些移除、哪裡被誤解、還要新增什麼。
5. 收斂後才做正式拆工與任務卡。

## 研究流程

- 先小樣本、先定報告 shape，再逐步收斂；不要一次做完整份大海撈針的調查。
- 事實、推論與未知分開寫。來源要記**查閱日期**、版本與出處。
- **「未找到」不等於「不存在」。** 查不到就寫未確認，不要推論成否定。
- 若把工作交給子 agent，子 agent 的報告**不是證據**：關鍵結論要能自己指出來源
  或重新查證。沒有可用的委派工具時，就由當前 agent 依角色順序自己執行，
  不要假裝有子 agent。

## 任務簡報（固定模板）

派工或自己執行前，先寫一份**自足**的任務簡報。子 agent 沒有你當下的上下文，
所以簡報必須包含以下固定區塊（空白模板見 `assets/templates/task-brief.md`）：

1. **任務類型與目標**：類型從五選一（實作／研究／文件／驗收／原型）；要做什麼、怎麼算完成。
2. **repo 根與路徑基準**：工作區根、code-root 與 notes-root 的實際路徑、路徑基準，
   以及開工預檢結論；另記分支、base SHA 與 worktree（S 用主 checkout；M／L 或並行
   用 worktree）。
3. **必讀背景**：相對路徑清單，以及為什麼要讀。
4. **已知／假設／未知**：把不確定性寫進簡報，不要留給 worker 猜。
5. **範圍與 non-goals**：做什麼、明確不做什麼。
6. **行為、輸入輸出與錯誤處理**：或研究問題與證據標準。
7. **驗收方法與成果**：要用什麼指令或觀察來驗證，並說明預期成果與 ship 方式
   （commit → 文字檢查 → hooks → push 非預設分支 → draft PR；合併由使用者決定）。
8. **限制與停止條件**：卡住時何時停下來回報。
9. **允許寫入的檔案或目錄**：明確列出。
10. **worker 禁止事項**：不得超出授權範圍、不得 `git add .`／`--no-verify`；
    commit 可在自己的 branch／worktree 進行；push 預設仍需明確授權，只有該工作區
    已明確授權時才照 ship 流程 push 非預設分支並開 PR。

本清單與 `assets/templates/task-brief.md` 的骨架**同源**；改了這邊就要同步那邊，
契約測試會擋住不一致。

紀律：

- 簡報中的「權限說明」只是說明，**不是權限設定**；這個 skill 不能強制 deny 工具。
- 拆分工作時，**只有彼此獨立、且寫入不同檔案**的任務才能並行。
- **同一個任務不要重複派給多個 worker。**
- worker 若發現超出簡報範圍的需要，**回報**，不要自行擴大範圍。

## 私有筆記的索引紀律

- **commit 不需要事前取得使用者授權**；agent 判斷到達 checkpoint 就可以 commit。
  但仍須用**明確檔案清單**、跑 hooks、不得 `--no-verify`。
- 並行的 worker 在**各自的 branch／worktree** commit；共用同一個 notes checkout 時，
  **一次只有一個 writer**，避免搶 index。
- **push 預設仍需明確授權**；只有該 workspace 已明確授權時，才照「Ship」與
  「Notes 的 push 例外」處理。不 `--force`、不改寫歷史、不改可見度。
- 寫入 private notes 的內容，同樣不得含憑證、真名、私人 email、帳號 ID、
  內網位址或個人絕對路徑。
- 若把任務卡複製到某個本機展示位置（例如 client 的計畫面板），權威仍是 notes repo
  內的任務卡；展示副本不是第二權威，也不要雙寫兩份權威。

## 驗收

- **實際驗證，不要只聲稱。** 能跑的測試就用實際指令跑，附上輸出摘要；
  沒測到的就明說「未測」，不要美化。
- 若驗收發現**需求被誤解**，先修任務簡報，再重做；若是**實作有錯**，直接修正實作。
- 不要把「建立了檔案」或「寫了筆記」當成「已同步」或「已驗收」。
- 驗收證據放 `acceptance` 目錄；`status.md` 只寫結論與連結，不重貼整份證據。

## 交接與同步

每一輪或每個里程碑，更新 notes 的**現況與歷史**：

- 更新 `status.md` 的 `now`／`next`／`blocker`／`needs`／`updated`。
- 在 `log` 目錄的當月檔（`YYYY-MM.md`） 追加一則（≤10 行），寫下日期、做了什麼、證據、下一步。
- 不再追加 `progress.md`；它只作舊專案的 fallback 與歷史。

內容至少涵蓋：

- 對應的 **code repo 分支與 commit**；該 commit 必須是**真實已存在**的，不是預期值。
- **哪些只在本機、尚未 push**（未 push 的 commit 無法跨機取得，要寫明）。
- **兩個 repo 各自的同步狀態**，以及仍待決定的問題。
- **下一步可行動任務**。

同步規則：

- 同步前先在**兩個 repo 分別**跑 `git status`。dirty 或 diverged 就停下來回報，
  不要自動 stash、reset、rebase。
- 要拉取更新時只用 `git pull --ff-only`；不要覆蓋未 commit 的工作。
- **push 是兩個 repo 各自獨立、非原子**的動作；成功與失敗要分別回報，
  程式碼未 push 要寫清楚。不 `--force`、不改寫歷史。
- commit 依本流程可在 checkpoint 直接進行；**push 預設仍需明確授權**。
  只有使用者已於該工作區明確授權時，才照「Ship」與「Notes 的 push 例外」處理；
  建立檔案或寫入內容**不等於**已同步。

## 授權邊界（重要）

以下動作需要使用者針對該動作的**明確授權**，這個 skill 本身**不會**代為決定或執行：

- 建立新 repo、建立 remote、**push**（預設仍需明確授權；已授權工作區的 ship
  是唯一例外，見「Ship」與「Notes 的 push 例外」）。
- 安裝或移除 git hooks（新機器要另外安裝，未經授權不做）。
- 變更任何 repo 的可見度。
- 盤點、複製、移動或刪除舊的 `.local`、`.scratch` 或其他既有檔案。
- repo 若裝有隱私掃描器或 hooks，它們的**存在與否不由此 skill 保證**；
  掃描器是選配，缺少時要寫「未檢查」，不要當成安全聲明。
- 公開 repo **絕不** stage notes、本機設定或 `.env`；private 內容是否進版控，
  由獲得明確授權的人以明確的檔案清單處理。

### 權限三段（技術護欄，不是授權）

權限規則固定 `ask → allow → deny`，`deny` 必須放在最後；規則是 last-match-wins。
**base policy 是 `* allow`，沒有規則命中不是 ask**，所以危險命令要逐條列出。
`ask`／`deny` 只有在真正命中時才生效；`git push *` 與 `git * push *` 是 push 的
broad ask，但其他未列出的危險命令仍可能落到 base allow。

push 一律用 `cd` 進 repo／worktree 的標準形式；`git -C … push` 只會停在 ask，
不會命中「`git push -u origin` ＋非預設分支」的 allow。權限 allow **不構成授權**；
沒有 workspace 的明確授權，agent 仍須逐次問。

**commit 是本流程的例外**：依「私有筆記的索引紀律」，commit 不需事前授權，
但仍受明確檔案清單、hooks 與單一 writer 的限制。**push 預設不在例外內**；
只有使用者已於該工作區明確授權時，才可照 ship 流程 push 非預設分支。

## 平台中立與相容性

這個 skill 不綁定任何模型、provider 或特定 agent 工具名稱；沒有步驟要求某個專有工具。

- 「文件**已確認支援**的載入位置」與「**實際 runtime 已測試**」必須分開寫。
- **OpenCode** 的 project 層 skill 目錄已於 **2026-10-21 實測**會載入
  （`handoff-refresh`、`release-prep` 皆成功載入並讀到新內容）。
  各 client 的確切載入路徑見 `references/client-compatibility.md`。
- 其餘 client 仍為文件記載、**未實測**：pi 用 .pi/skills 與通用 .agents/skills、
  DeepSeek Harness 用 .dsh/skills 與通用 .agents/skills。詳細來源、手動觸發與限制
  見 `references/client-compatibility.md`。
- 任何 client 都應有 fallback：**手動讀取 `SKILL.md` 與 `references`**，並自行複製
  模板；但這不保證 fallback 一定被正確執行。
- 格式通過不保證模型會精確遵守，也不保證會自動配置權限。
- 部分 client 的 catalog 對 description 有預設 500 字元上限（client 端建議，
  不是規格上限 1024）；本 skill 的 description 維持在 500 字元內。

## 深入文件

- `references/workflow-details.md`：現況／歷史、車道、開工預檢、worktree、ship、
  規劃、簡報、研究、驗收與交接的細節。
- `references/privacy-and-sync.md`：公開／私有隔離、三段權限、notes push、
  索引紀律與同步步驟。
- `references/client-compatibility.md`：各 client 的已確認與未確認狀態、手動 fallback。

## 模板

可獨立複製的空白模板，每份都含 placeholder 欄位與填寫規則：

- `assets/templates/overview.md`
- `assets/templates/status.md`（新專案預設）
- `assets/templates/log.md`（每月日誌）
- `assets/templates/plan.md`
- `assets/templates/task-brief.md`
- `assets/templates/research.md`
- `assets/templates/acceptance.md`
- `assets/templates/pull-request.md`（PR body；含驗證、掃描 exit code、風險與回滾）
- `assets/templates/progress.md`（**legacy**：只給沒有 `status.md` 的舊專案）

## 明確不做與不能宣稱的事

- 不保證任何 repo 已備份、已同步或已正確分類。
- 不保證任何 agent client 會載入、觸發或正確遵守這個 skill。
- 不是隱私掃描器；不代替掃描器，也不提供任何安全保證。
- 不替使用者做任何不可逆的動作。
