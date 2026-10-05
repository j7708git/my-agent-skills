---
name: project-workflow
description: 引導在單一工作區內協調「公開程式碼 repo」與「獨立 private 工作紀錄 repo」的專案流程：確認工作區與兩個 repo 根、規劃許願或先做最小原型、用已知／假設／未知收斂需求、撰寫自足任務簡報、維持派工與並行紀律、實際驗收、以及跨機同步與交接。當使用者要開新專案、把想法變成計畫、先看原型、拆工派工、寫任務卡、做驗收，或討論 Git 同步、可見度與授權邊界時使用。這個 skill 只是流程指引，不是掃描器，也不自行建立 repo、push、安裝 hooks 或搬移檔案。
license: MIT
compatibility: 純文字流程指引，無執行時相依，也不需要特定工具或模型；任何能讀取檔案的 agent client 都可用手動載入的方式使用。
---

# project-workflow

這是一個**流程指引** skill，描述如何在使用者的工作區內，把「公開的程式碼 repo」
與「獨立的 private 工作紀錄 repo」分開管理，並用一致的方式規劃、拆工、驗收與同步。

- 它**不是**掃描器，不做隱私掃描，也不執行任何掃描指令。
- 它是**純文字指引，不是自動腳本**，不會自己執行任何動作。但在使用者已明確授權的
  任務範圍內，執行 agent 可以照本流程寫入與實作，不必每個細節都停下來重問；
  未被授權的動作則不得自行執行。
- 它**不會**自動建立 repo、commit、push、安裝 git hooks、調整可見度，或搬移既有檔案。
- **載入這個 skill 不等於授權任何寫入或不可逆動作。** 需要寫入、同步或發布時，
  一律先取得使用者針對該動作的明確授權（見「授權邊界」）。

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

## 私有筆記的目錄主幹

notes repo 內以 `<project-id>` 命名的專案目錄，主幹為 overview、progress、plan 三個檔案，
以及 research、acceptance、tasks 三個子目錄；notes repo 另有共用的 shared 目錄，其下放 research。

code repo 的 `.gitignore` 應排除暫存；公開的 code repo 放程式碼、測試、README 與對外
docs，**不得**含 private 工作筆記。workspace 或 code repo 內的 `.tmp` 都應被 ignore，
且**絕不自動清理**。
**跨機接手時，先讀 plan、overview、progress**，需要時再讀 research，才開始工作。

## 隱私分流（公開與私有嚴格隔離）

| 內容 | 位置 | 版控 |
| --- | --- | --- |
| 使用說明、產品契約、教學、架構成品 | code repo 的 README 與 docs | 公開版控 |
| 程式碼與測試 | code repo | 公開版控 |
| 完整計畫、進度、研究、驗收、任務卡（overview、progress、plan、research、acceptance、tasks） | notes repo 的專案目錄與 `shared` | private 版控 |
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

1. **任務類型與目標**：要做什麼、怎麼算完成。
2. **兩個 repo 根**：code-root 與 notes-root 的實際路徑，並說明以什麼為基準解析。
3. **必讀背景**：相對路徑清單，以及為什麼要讀。
4. **已知／假設／未知**：把不確定性寫進簡報，不要留給 worker 猜。
5. **範圍與 non-goals**：做什麼、明確不做什麼。
6. **行為、輸入輸出與錯誤處理**：或研究問題與證據標準。
7. **驗收方法與成果**：要用什麼指令或觀察來驗證。
8. **限制與停止條件**：卡住時何時停下來回報。
9. **允許寫入的檔案或目錄**：明確列出。
10. **worker 禁止事項**：尤其不得動 private repo 的 Git index。

紀律：

- 簡報中的「權限說明」只是說明，**不是權限設定**；這個 skill 不能強制 deny 工具。
- 拆分工作時，**只有彼此獨立、且寫入不同檔案**的任務才能並行。
- **同一個任務不要重複派給多個 worker。**
- worker 若發現超出簡報範圍的需要，**回報**，不要自行擴大範圍。

## 私有筆記的索引紀律

- 多個 worker 並行時，**只有 PM 可以整合 private repo 的 Git index**；
  其他 worker 只寫自己簡報指定的檔案，不 `git add`、不 commit、不碰索引，避免搶 index。
- 寫入 private notes 的內容，同樣不得含憑證、真名、私人 email、帳號 ID、
  內網位址或個人絕對路徑。
- 若把任務卡複製到某個本機展示位置（例如 client 的計畫面板），權威仍是 notes repo
  內的任務卡；展示副本不是第二權威，也不要雙寫兩份權威。

## 驗收

- **實際驗證，不要只聲稱。** 能跑的測試就用實際指令跑，附上輸出摘要；
  沒測到的就明說「未測」，不要美化。
- 若驗收發現**需求被誤解**，先修任務簡報，再重做；若是**實作有錯**，直接修正實作。
- 不要把「建立了檔案」或「寫了筆記」當成「已同步」或「已驗收」。

## 交接與同步

每一輪或每個里程碑，更新 notes 的進度，內容至少涵蓋：

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
- commit 與 push 都是**需要明確授權**的動作；建立檔案或寫入內容**不等於**已同步。

## 授權邊界（重要）

以下動作一律需要使用者針對該動作的明確授權，這個 skill 本身**不會**代為決定或執行：

- 建立新 repo、建立 remote、commit、push。
- 安裝或移除 git hooks（新機器要另外安裝，未經授權不做）。
- 變更任何 repo 的可見度。
- 盤點、複製、移動或刪除舊的 `.local`、`.scratch` 或其他既有檔案。
- repo 若裝有隱私掃描器或 hooks，它們的**存在與否不由此 skill 保證**；
  掃描器是選配，缺少時要寫「未檢查」，不要當成安全聲明。
- 公開 repo **絕不** stage notes、本機設定或 `.env`；private 內容是否進版控，
  由獲得明確授權的人以明確的檔案清單處理。

## 平台中立與相容性

這個 skill 不綁定任何模型、provider 或特定 agent 工具名稱；沒有步驟要求某個專有工具。

- 「文件**已確認支援**的載入位置」與「**實際 runtime 尚未測試**」必須分開寫。
- 官方文件已記載的 project 位置（runtime 皆未實測）：OpenCode 用 .opencode/skills、
  pi 用 .pi/skills 與通用 .agents/skills、DeepSeek Harness 用 .dsh/skills 與通用
  .agents/skills。詳細來源、手動觸發與限制見 `references/client-compatibility.md`。
- 任何 client 都應有 fallback：**手動讀取 `SKILL.md` 與 `references`**，並自行複製
  模板；但這不保證 fallback 一定被正確執行。
- 格式通過不保證模型會精確遵守，也不保證會自動配置權限。
- 部分 client 的 catalog 對 description 有預設 500 字元上限（client 端建議，
  不是規格上限 1024）；本 skill 的 description 維持在 500 字元內。

## 深入文件

- `references/workflow-details.md`：規劃、簡報、研究、驗收與交接的細節與檢查清單。
- `references/privacy-and-sync.md`：公開／私有隔離、索引紀律與同步步驟。
- `references/client-compatibility.md`：各 client 的已確認與未確認狀態、手動 fallback。

## 模板

六份可獨立複製的空白模板，每份都含 placeholder 欄位與填寫規則：

- `assets/templates/overview.md`
- `assets/templates/progress.md`
- `assets/templates/plan.md`
- `assets/templates/task-brief.md`
- `assets/templates/research.md`
- `assets/templates/acceptance.md`

## 明確不做與不能宣稱的事

- 不保證任何 repo 已備份、已同步或已正確分類。
- 不保證任何 agent client 會載入、觸發或正確遵守這個 skill。
- 不是隱私掃描器；不代替掃描器，也不提供任何安全保證。
- 不替使用者做任何不可逆的動作。
