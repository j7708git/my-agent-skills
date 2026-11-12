---
name: workspace-sync
description: 開工同步 project-notes、my-agent-skills 與目前或指定專案，偵測遠端預設主線並安全快轉。換電腦、換 agent 後要求對齊最新主線，或明確呼叫 workspace-sync 時使用；只檢查模式保留本機狀態。
license: MIT
---

> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# workspace-sync

把三個角色的 repo 對齊各自遠端預設分支，保留未完成的本機工作，最後回報每個
repo 的結果與專案下一步。這是給 agent 使用現有 Git 與 shell 執行的流程，
不綁定作業系統或 client，沒有背景服務或獨立 CLI。

## 呼叫與範圍

- 使用者呼叫 `$workspace-sync` 或要求「開工同步／對齊最新主線」，即執行本次範圍內的
  偵測、fetch、安全切換預設分支與 `pull --ff-only`，不逐步重問已授權的操作。
- 使用者說「只檢查」「check-only」或目前是 plan 唯讀模式時，只讀本機狀態與
  `ls-remote`：不 fetch、pull、切分支或寫檔；物件不足時標示無法比較，不以快取冒充最新。
- 載入此文件不會擴大任務。同步不包含 commit、push、PR、安裝工具、更新所有已安裝
  skills 或改 Git 設定；也不自動 stash、reset、rebase、cherry-pick、建立 merge commit、
  刪分支、clean 或強制覆蓋。阻礙同步的工作保留原地，列出下一步供使用者選擇。

## 1. 找到三個 repo

先讀現有工作區與各 repo 適用的 `AGENTS.md`，依使用者明示路徑、目前工作目錄與
工作區配置判斷目標。使用 `git rev-parse --show-toplevel` 取得 checkout 根；linked
worktree 再用 `git rev-parse --path-format=absolute --git-common-dir` 與
`git worktree list --porcelain` 找出共用 repo 與主 checkout，不能把 worktree 的父目錄
直接當工作區。Git 不支援所需選項時，使用已核對的等價查詢，無法確認就回報缺少資訊。

| 角色 | 尋找方式 |
| --- | --- |
| 專案 | 使用者指定的專案，否則目前 checkout；不能把 notes／skills 自動當成業務專案 |
| notes | 已知工作區的 `project-notes`，或現有配置明確指定的位置 |
| skills | 同工作區的 `my-agent-skills`，或現有配置明確指定的位置 |

三者不必在同一個父目錄；從已知配置找不到才問缺少的路徑，不掃描整顆磁碟或猜 repo。
若目前在 notes／skills 且沒有指定專案，先處理可確定的兩個共用 repo，只問缺少的專案。
使用者明確指定 skills repo 為本次專案時合併其角色。以解析後的 checkout 與 common dir
去重；同一 repo 的多個 worktree 共享 refs，僅操作選定 checkout 且序列處理。
缺目錄／不是 Git repo 時標示缺少，不自動 clone、init、建立 remote 或替代目錄。

## 2. 逐 repo 取得現況與遠端主線

先盤點全部目標，逐 repo 保留同步前的分支、HEAD、工作樹與 index 狀態。Git 命令在
目標 checkout 執行；以 `git status --porcelain=v1 -z --untracked-files=all` 解析完整路徑，
不要以空白切割檔名。另讀 `git worktree list --porcelain` 與目前分支。
使用 Git 回傳的 git-dir／git-path 檢查 merge、rebase、cherry-pick、revert、sequencer、
bisect 是否進行中；不能只看 `.git` 是否為目錄。進行中操作、detached／unborn HEAD、
工作樹衝突或無法確認狀態時，停止該 repo 的變更並回報。

remote 使用已明示的名稱，否則以存在且用途可確認的 `origin` 為準；有多個候選且
用途不明時只問該 repo。線上查 `git ls-remote --symref <remote> HEAD`，取得
refs/heads/DEFAULT_BRANCH 與 SHA。沒有明確 symref 就回報無法判定，不猜 `main`／`master`，
也不單靠本機 origin/HEAD。網路或驗證失敗標示「遠端未確認」，保留快取狀態的日期限制。
只檢查模式以本次 `ls-remote` 公告的完整 SHA 比較：先用 `git cat-file -e <sha>^{commit}`
確認本機有該 commit，才對 HEAD／default 計算 ahead／behind；缺物件就標示無法比較。
若另外顯示舊 tracking ref 的計數，明確標為快取，不代表遠端最新。

同步模式明確抓取該分支，讓 single-branch clone 也取得主線：

```text
git fetch --no-prune --no-tags --no-recurse-submodules REMOTE refs/heads/DEFAULT_BRANCH:refs/remotes/REMOTE/DEFAULT_BRANCH
git rev-list --left-right --count HEAD...refs/remotes/REMOTE/DEFAULT_BRANCH
git rev-list --left-right --count refs/heads/DEFAULT_BRANCH...refs/remotes/REMOTE/DEFAULT_BRANCH
```

REMOTE 與 DEFAULT_BRANCH 代表偵測到的 remote 與分支名稱；以上是參數示意，
不是可直接貼上的 shell 腳本。實際呼叫將每個已驗證值作為獨立參數；
PowerShell 用參數陣列、Bash 用正確引號，路徑／ref 不經 `eval` 或拼成可執行字串。
fetch 不加 force／`+` refspec，不 prune；失敗就停該 repo，不採舊 tracking ref 下結論。
本機 default 不存在時略過第二個比較，記為待建立；其他查詢失敗不能當作不存在。

比較值左邊是本機獨有 commit 數，右邊是遠端獨有 commit 數。**目前分支與本機
default 都要比較**；只看目前 upstream 可能會漏掉未進主線的 feature commits。

## 3. 決定是否能快轉

| 觀察 | 動作 |
| --- | --- |
| 目前 HEAD 與本機 default 都等於或落後遠端 default | 通過其餘檢查後切換 default 並快轉 |
| 任一比較左邊大於 0，包括已 push 到 feature 的 commits | 停該 repo，列出獨有 commits 與 ahead／behind；由使用者決定接續原分支或另案整合 |
| default 在另一個 worktree 使用 | 停該 repo，回報持有它的 checkout；不搶分支或擅自改另一份 checkout |
| code／skills 有 staged、unstaged 或 untracked 變更 | 停該 repo，列出路徑，保留內容與 index |
| notes 有 dirty／untracked | 只在下述 notes 例外全部成立時繼續，否則停該 repo |

**notes 例外**：適用的 notes 規範明確允許無關 dirty／untracked；目前已在 default；
沒有本機獨有 commits；遠端更新與本機變更互不重疊。以下列參數取得更新路徑：

```text
git diff --name-status -z --no-renames HEAD refs/remotes/REMOTE/DEFAULT_BRANCH
```

與 staged、unstaged、untracked 的完整路徑比對，包含檔案相同、祖先／子路徑與檔案／目錄
轉換；Windows 另考慮大小寫衝突。有重疊、無法證明、submodule 狀態不明就停。
保留原有 staged 差異與 dirty／untracked 檔案的內容證據，成功後核對這些工作不變；
快轉新增的正常 index 項目不算本機工作。此例外不允許帶著 dirty 切分支。

乾淨狀態也可能有 ignored 檔：切換／拉取前確認即將寫入的路徑不會覆蓋既存 ignored
內容；無法排除覆蓋時停下。不要把「Git 可以切換」當作保留全部本機工作的證明。

一個 repo 被阻擋時，繼續其他可安全處理的 repo。notes 未同步時，其本機現況只能
標為「可能落後」，不能據此宣布整個工作區已對齊。

## 4. 執行並確認結果

1. 操作前再查 status、HEAD、分支、worktree 使用情況與遠端 default SHA。
   若有人改動現場，停該 repo；若遠端前進，重新 fetch 並重做路徑與 ancestry 檢查。
2. 已在 default 就保留；本機 default 存在時安全
   `git switch --no-recurse-submodules --no-overwrite-ignore <default>`；不存在時用
   `git switch --no-recurse-submodules --no-overwrite-ignore --no-track -c <default> <remote-tracking-ref>` 建立；
   最後一個參數是已取得的完整 remote tracking ref。single-branch clone 的 fetch mapping
   可能沒有 default，因此不要求建立 tracking；本流程用明確 SHA 拉取，不改 remote.fetch
   或 upstream 設定。不強制切換、不重設任何舊分支。
3. 記錄已 fetch 且通過 ancestry／路徑檢查的完整 commit SHA 為 VERIFIED_SHA。
   從本機 repo `.` 拉取這個固定 SHA，避免 pull 再次連網取得未經路徑檢查的新版本：

   ```text
   git -c pull.rebase=false -c merge.autoStash=false -c rebase.autoStash=false pull --ff-only --no-rebase --no-autostash --no-recurse-submodules --no-prune --no-tags . VERIFIED_SHA
   ```

   這仍是 ff-only pull，不依賴 upstream，也不遞迴更新 submodule。notes 的 dirty
   例外尤其需要停用 autostash。命令失敗即停，不改設定或換整合策略重試。
4. 重新查遠端 default 的 symref／SHA，核對本機 HEAD、default 與 tracking ref 相同，
   ahead／behind 均為 0，且 status 符合預期（乾淨，或 notes 的原有變更完整保留）。
   預設分支改名或遠端再前進，最多重跑一次完整判斷；仍變動就標示待同步與已確認 SHA。
   全部成功才稱「截至本次遠端查詢已對齊」，fetch 成功本身不代表對齊。
   有 submodule 時另查初始化與 commit 是否一致；缺少或不一致要回報，superproject
   SHA 相同不能代表完整開工就緒，不順手遞迴更新未知範圍的 submodule。

## 5. 重新載入規範與接手脈絡

同步後重讀各 repo 更新過的 `AGENTS.md`；skills repo 成功同步後，直接讀其最新
`workspace-sync` 原始 `SKILL.md`，後續依新版仍在授權範圍內的規則行動。
盤點本次相關 skill 的實際載入位置：指向來源的 link、獨立 copy、或位置未知。
link 要解析確認目標；copy 比較內容／hash 並回報差異；未知就明說未確認。
**repo 更新不等於已安裝副本或目前 session 已更新**；只報差異，不覆蓋所有已裝 skills、
重載 client 或修改全域設定。需要安裝更新時，另行明確指定目標與範圍。

從 notes 的專案索引確認對應 project-id，再讀其 `status.md` 的現況、下一步、卡點與
待決事項；存在時優先於舊 `progress.md`。只有缺少 status 才讀 progress 最新段加 Git。
branch／HEAD／是否已 push 以實際 Git 為準；若進度宣稱與 Git 矛盾，保留兩側證據，
停在對帳，不自選其中一份或順手改筆記。需要任務細節才讀對應任務卡／驗收紀錄。

## 完成回報

用一張表列出每個角色的 checkout、remote/default、原分支 → 現分支、前後 HEAD、
ahead／behind、工作樹與結果（已對齊／保留本機工作／缺少／遠端未確認／僅檢查）。
重複角色共用一列；被阻擋者列具體原因與最小下一步，不把部分成功寫成全部完成。
另用短段說明 skill 副本是否新鮮、專案 now／next／blocker，以及真正需要使用者決定的項目。
