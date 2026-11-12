> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 任務簡報（task-brief）

> 使用方式：把整份檔案複製一份，針對單一任務填寫，交給 worker。
> 子 agent 沒有你的上下文，所以簡報必須**自足**；所有 `<...>` 都要換成實際內容。
> 「權限說明」只是說明，不是權限設定；不要把簡報當成授權。
>
> 這份骨架是**派工簡報的唯一權威**；其他清單（PM 的 agent 定義、本 skill 的主文
> 與細節文件）都必須與它同源，節名與順序逐字相同，並由本集合 repo 的契約測試
> 機械比對。

## 骨架

```markdown
> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 任務簡報：<任務名稱>

## 1. 任務類型與目標

- 類型：<實作／研究／文件／驗收／原型>
- 目標：<要做什麼、怎麼算完成>

## 2. repo 根與路徑基準

- 工作區根：<路徑；預設所有相對路徑以此為基準>
- code-root：<路徑；研究任務寫 N/A>
- notes-root：<路徑>
- 路徑基準：<通常填「工作區根」；若本任務改用其他目錄為基準，在這裡寫明並說明原因>
- 開工預檢：<plan 唯讀／prepare 才 fetch、pull --ff-only、開分支／worktree>
- 分支與 base SHA：<branch；一律從 origin/<default> 開；base SHA>
- worktree：<S 用主 checkout 開分支；M／L 或並行用 worktree；worktree 路徑；
  push 一律 cd 進 repo／worktree，不用 git -C … push>

## 3. 必讀背景

- <相對路徑>：<為什麼要讀>

## 4. 已知／假設／未知

- 已知：<可查證的事實>
- 假設：<可逆的預設>
- 未知：<不確定之處>

## 5. 範圍與 non-goals

- 範圍：<本任務要做的>
- non-goals：<本任務明確不做的>

## 6. 行為、輸入輸出與錯誤處理

<輸入、輸出、錯誤處理；研究任務改寫「研究問題與證據標準」>

## 7. 驗收方法與成果

- 驗收方法：<實際指令或觀察方式>
- 預期成果：<產出檔案／報告>
- ship：<commit → public 文字檢查 → pre-commit／pre-push → push 非預設分支 →
  draft PR；合併由使用者決定；無 remote 只到 commit>

## 8. 限制與停止條件

- 限制：<不可使用的工具、不可修改的檔案>
- 停止條件：<卡住時何時停下回報>

## 9. 允許寫入的檔案或目錄

- <明確清單>

## 10. worker 禁止事項

- 不得超出本簡報範圍；有需要請回報。
- 不要 `git add .`／`git add -A`；不得 `--no-verify`。
- private notes 的 commit 可在**自己的 branch／worktree** 進行；共用同一個
  checkout 時一次只有一個 writer。
- private notes 只在它的 **default 分支**寫入；push 只能經由受保護的 push 工具，
  先 ff-only，diverged 就停，不自動 merge／rebase。
- 簡報不構成 push／安裝／發布的授權；**push 預設仍需使用者明確授權**。
  只有使用者已於該工作區明確授權時，才可照 ship 流程 push 非預設分支並開 PR；
  PR 合併仍由使用者決定。
- 權限模型固定 `ask → allow → deny`，`deny` 必須最後；base policy 是 `* allow`，
  **未命中不是 ask**。權限 allow 只是技術護欄，不構成授權。
```

## 欄位填寫規則

| 欄位 | 填寫規則 |
| --- | --- |
| 任務類型與目標 | 類型從五選一：實作／研究／文件／驗收／原型；目標要可判定 |
| repo 根與路徑基準 | 工作區根、code-root、notes-root 都寫實際路徑（研究任務的 code-root 寫 N/A）；路徑基準明說以哪個目錄為準，避免 worker 猜 |
| 開工預檢 | 寫明 plan／prepare／ship 哪一種；plan 唯讀，prepare 才 fetch、pull --ff-only、開分支／worktree |
| 分支與 base SHA | 分支一律從 origin/<default> 開，不引入 develop；記 base SHA，PR base 是 default |
| worktree | S 用主 checkout 分支；M／L 或並行用 worktree；寫出 worktree 路徑與分支；push 一律 cd 進 repo／worktree，不用 git -C … push |
| 必讀背景 | 用相對路徑，並說明為什麼要讀 |
| 已知／假設／未知 | 把不確定性寫進簡報，不要留給 worker 猜 |
| 範圍／non-goals | 兩者都要寫，避免 worker 自行擴大 |
| 行為與輸入輸出 | 實作任務寫清楚；研究任務改成研究問題與證據標準 |
| 驗收方法 | 要能實際執行或觀察，每條都要附驗證方式，不要只寫「確認正確」 |
| ship | 寫明 commit → 文字檢查 → hooks → push 非預設分支 → draft PR；合併由使用者決定；無 remote 只到 commit |
| 限制與停止條件 | 限制與停止條件都要寫，卡住時才知道何時回報 |
| 允許寫入 | 明確列出檔案或目錄，不用「專案目錄」這種模糊範圍 |
| worker 禁止事項 | 必含不得動 private repo index、不得擴大範圍；簡報不構成授權；push 預設需明確授權，已授權工作區才照 ship 流程；權限三段是 ask → allow → deny，deny 最後、未命中不是 ask |
