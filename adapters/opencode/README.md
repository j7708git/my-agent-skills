# OpenCode adapters：PM 工作流的 agent 與指令定義

這裡收錄 `skills/project-workflow/` 作者實際在用的 OpenCode 設定：
十個 agent（工作流配套、計畫審查、獨立審查與翻譯官）、一個獨立的網路研究 agent，以及四個斜線指令。
agent 定義與 skill 分開發行（原因見
[docs/research/portability.md](../../docs/research/portability.md)）：
agent 是平台專屬的角色、權限與模型設定，skill 是純文字流程指引。

## 目錄結構

| 檔案 | 說明 |
| --- | --- |
| `agents/pm.md` | 專案 PM（技術長）：接收願望、安排研究、規劃、派工與驗收 |
| `agents/researcher.md` | 研究員：調查主題並把結論寫成 `project-notes/` 內部知識筆記 |
| `agents/implementer.md` | 實作者：依自包含任務簡報實作、自我驗收並回報 |
| `agents/tester.md` | 測試員：依任務卡的驗證方式執行驗收並回報，不改程式碼 |
| `agents/tester-verify.md` | 獨立複驗測試員（選配）：不讀第一份 tester 報告，重跑關鍵驗收並加負向／對照組，不改程式碼 |
| `agents/plan-reviewer.md` | 計畫審查員（選配）：派工前讀計畫與任務卡，抓矛盾、漏項與不可驗證的驗收；不改檔、不跑測試 |
| `agents/reviewer.md` | 獨立審查者（日常階段審查）：不改檔、不跑測試、不啟動服務 |
| `agents/reviewer-deep.md` | 獨立審查者（重要節點審查，如公開到 GitHub 前）：不改檔、不跑測試、不啟動服務 |
| `agents/workflow-optimizer.md` | 工作流優化：專案階段健檢流程並探勘 skill 提案（提案制、唯讀，只寫報告與提案） |
| `agents/translator.md` | 翻譯官（選配）：把抽象願望問成自包含交接單，核可後交給 `pm`，並把 PM 的回覆翻成三段式白話 |
| `agents/web-search.md` | 網路研究 agent（獨立功能，與 project-workflow 無關） |
| `agents/web-search-modules/` | web-search 的場景策略模組（除錯、學術、中文社群等） |
| `commands/wish.md` | `/wish <願望>`：啟動 PM 的規劃先行流程 |
| `commands/proto.md` | `/proto <願望>`：啟動原型優先流程 |
| `commands/research.md` | `/research <主題>`：直接派研究員調查 |
| `commands/optimize.md` | `/optimize <專案id[,專案id...]>`：一專案派一個 `workflow-optimizer` 平行健檢並彙整（缺省回問） |

## 安裝

先進到 `adapters/opencode/`（以下指令用相對路徑，從 repo 根目錄跑會找不到檔案），
再把檔案複製到 OpenCode 的使用者設定目錄（不存在就先建立）：

```bash
cd adapters/opencode
mkdir -p ~/.config/opencode/agents/web-search-modules ~/.config/opencode/commands
cp agents/*.md ~/.config/opencode/agents/
cp agents/web-search-modules/*.md ~/.config/opencode/agents/web-search-modules/
cp commands/*.md ~/.config/opencode/commands/
```

之後在 OpenCode 裡即可用 `/wish`、`/proto`、`/research`、`/optimize` 指令，
或直接指定 `agent: pm`／`researcher`／`implementer`／`workflow-optimizer`／`translator`。

> **選配：一併安裝 `academic-paper-search` skill**：`agents/researcher.md` 與
> `agents/web-search-modules/academic-papers.md` 在環境**已安裝**本 repo 發行的
> `academic-paper-search` skill（`skills/academic-paper-search/`）時，會優先載入
> 它、用它內附的 arXiv API 腳本做結構化論文檢索；未安裝時自動退回原本的網路
> 搜尋流程，不會失效。要走這條路徑就一併安裝該 skill（安裝方式同其他 skill，
> 見根目錄 [README](../../README.md) 的〈安裝 skill〉一節）。

> **模組檔為什麼帶 frontmatter**：OpenCode 會把 `agents/` 底下巢狀目錄的
> `.md` 一律當成 agent，五個 web-search 模組檔因此會以 `web-search-modules/*`
> 的名義出現在你的 agent 清單（實測 v2.0.27 會列為可選的 primary agent）。
> 發行版本在每個模組檔加了 `hidden: true`——OpenCode 實測會解析此欄位
> （與內建隱藏 agent 同一標記），讓它們不該出現在 agent 選擇裡。
> 刪掉 frontmatter 不影響 web-search 讀取模組，只會讓清單多五個雜項。

## 模型設定（刻意移除）

發行版本**移除了所有 `model:` 綁定**：作者的 provider 與模型選擇是個人環境
設定，寫死對其他使用者沒有意義。安裝後請依自己的環境加上 `model:` 行
（或用 OpenCode 的模型設定 UI 選）。選擇建議：

- **researcher／implementer／tester**：世界知識有限但便宜快速的 flash 級模型
  即可，它們的工作方式是「以 docs 內的知識為準、不憑記憶猜 API」，正好抵消
  小模型知識不足的弱點。
- **tester-verify**：只在獨立複驗時使用，**必須與 `tester` 用不同模型**，
  否則同一模型的盲點會一起漏掉；發行版同樣不綁型號，由你依環境選擇。
- **plan-reviewer**：只在派工前的選配計畫審查使用，**必須與 PM 用不同模型**，
  否則跨模型獨立性會消失；它只讀文件，flash 級模型即可，發行版不綁型號。
- **pm**：負責規劃、派工與驗收判斷，用你能取得的較強模型。
- **translator**：以理解口語、追問關鍵細節與寫清楚白話為主，可用比 flash 級稍強
  一點的模型；實際型號由你依環境選擇。它的產出是 `pm` 的輸入，交接單的忠實度
  比文采重要；模型太小容易漏掉假設或問錯問題。
- **workflow-optimizer**：健檢以讀文件與分析為主（唯讀、不改檔、不跑測試），
  flash 級模型即可；它的價值在對照既有紀錄找證據，而非知識廣度。
- **reviewer／reviewer-deep**：審查以讀碼與分析為主（不改檔、不跑測試），
  不用太強也能抓大部分問題；
  `reviewer` 供日常階段審查、`reviewer-deep` 供重要節點（如公開到 GitHub 前），
  後者可用比前者高一級的模型。

## 與作者本機設定的差異

發行副本與作者的本機原檔有下列**刻意差異**；日後同步新版本時請保留：

- 所有 agent 定義移除 `model:` 綁定（作者的 provider／模型是個人設定）。
- `agents/web-search-modules/*.md` 加上 `hidden: true` frontmatter，並移除
  指向未收錄來源檔（`web-search-agent.md`）的標題註記——否則 OpenCode 會把
  這五個模組檔當成可選 agent 列出。
- `commands/research.md` 的無效鍵 `subagent: true` 改為文件記載的 `subtask: true`
  （本 repo 未做 runtime 對照實測）。
- `agents/web-search.md` 保留 legacy 欄位 `temperature:`／`tools:`（作者現行
  設定原樣；OpenCode V2 文件標示不建議新設定使用，實測 v2.0.27 仍正確轉譯）。
- `agents/researcher.md` 與 `agents/web-search-modules/academic-papers.md` 增列
  `academic-paper-search` skill 的條件式入口（先載入該 skill、再依其 `SKILL.md`
  執行 `scripts/arxiv_search.py` 做 arXiv 結構化檢索；環境未安裝該 skill 時退回
  一般網路搜尋）。這是配合本 repo 發行 skill 的刻意增補，同步作者新版本時請保留。
- `agents/implementer.md`／`agents/researcher.md`／`agents/tester.md` 增列成本前提
  （「成本信號」「證據下限」「驗收看外部世界」），`agents/pm.md` 增列「工具使用義務」
  與兩句成本界定。起因是觀察到部分 flash 級模型在離開原生 agent 框架後，對「行動
  成本」過度保守（原生框架的文件明說推理便宜、且「跳過不是成本信號」）。這些是通用
  規則、不綁型號也不綁版本；行為若改變，整段可刪，不影響其他規則。

## 寫作契約的 B1 注入

本 repo 的 `writing-contract` 要求 B 類指令檔第一段有優先序句。這次沒有新增或移除
agent；注入範圍與排除理由如下：

- `agents/pm.md` 與 `skills/project-workflow/assets/templates/task-brief.md` 的
  檔案開頭與內嵌骨架前導句都加了同一句；`pm.md` 骨架保留兩個空格的縮排。
- 其餘 B 類檔（10 份 `SKILL.md`、7 份 `references/*.md`、根 `AGENTS.md`）只在
  檔案開頭加 B1；`skills/writing-contract/` 的規則檔本身豁免，因為它們定義規則，
  不描述程式行為。
- 其他 adapters agent／command 檔不在本次 B 類清單內，沒有注入；若日後納入，
  要同步更新檢查器的類別清單與契約測試，不要只改文件。

## 相依關係

- `pm.md`／`researcher.md`／`implementer.md`／`tester.md`／`tester-verify.md`／
  `plan-reviewer.md` 與 `commands/*.md`
  預設採用 [skills/project-workflow](../../skills/project-workflow/SKILL.md) 的
  慣例（內部紀錄放 `project-notes/`、公開知識進程式碼 repo 的 `docs/`、
  自包含任務簡報等）。**建議一併安裝該 skill**，否則這些 agent 的指引
  會引用到不存在的流程文件。
- `reviewer.md`／`reviewer-deep.md` 審查時以任務卡的驗收條件逐條對照，
  同樣引用該 workflow 的慣例；獨立使用時可不安裝 skill，但對照會失準。
- `researcher.md` 的 edit 權限只允許寫 `project-notes/` 下的研究筆記；
  若你的工作區不用這個慣例，需自行調整它的 permissions。
- `workflow-optimizer.md` 的 edit 權限只允許寫健檢報告
  （`project-notes/shared/workflow/`）與 skill 提案（程式碼 repo 的
  `.local/proposals/`），shell 只開放唯讀 git；它同樣採用 project-workflow 的
  紀錄慣例，建議一併安裝該 skill，否則健檢會找不到可對照的紀錄。
- `translator.md` 是意圖層的選配 primary agent：它把使用者的口語願望整理成
  自包含交接單，經核可後交給 `pm`。edit 權限只開偏好檔
  （`project-notes/shared/preferences/`），不寫程式、不跑 shell、不派其他 subagent
  （只允許呼叫 `pm`）；
  偏好檔與交接單沿用 `project-notes/` 慣例。要完整走「交接給 PM」的流程，
  需同時安裝 `pm`，把 `pm` 設為 `mode: all`，並在 workspace 設定
  `experimental.subagent_depth: 2`；翻譯官用 `subagent` 呼叫 `pm`，不使用
  session 工具。
- `web-search.md` 是自足的，不依賴 project-workflow；它會在執行期讀取
  安裝位置的 `~/.config/opencode/agents/web-search-modules/` 下的模組檔，
  所以模組檔必須與 agent 檔一起安裝。
- `pm.md` 的 permissions 允許呼叫名為 `researcher`／`implementer`／`tester`／
  `tester-verify`／`plan-reviewer`／`explore` 的 subagent；`explore` 是 OpenCode
  內建 subagent，不需另裝。
- **選配工具（GitHub CLI）**：建議安裝並登入 `gh`（`gh auth login`）。`researcher.md`
  在尋找現成的 Agent Skills、開源工具、範例 repo 或查閱 GitHub Issues 時，會優先使用
  `gh` CLI 直接查詢結構化資料；若環境未安裝或未認證 `gh`，則自動退回一般網路搜尋。
- **選配 skill（學術論文檢索）**：本 repo 另發行 `academic-paper-search` skill
  （`skills/academic-paper-search/`）：以 arXiv API 做結構化論文檢索
  （關鍵字、作者、分類、日期範圍），輸出含標題、作者、摘要、arXiv ID、分類、
  連結與 `total_results` 的 JSON，並可輸出 BibTeX。`researcher.md` 與
  `agents/web-search-modules/academic-papers.md` 在環境**已安裝**該 skill 時，
  會先載入它、執行其 `scripts/arxiv_search.py` 做 arXiv 檢索，再輔以一般網路搜尋；
  **未安裝時兩者維持原本的網路搜尋流程**，不會失效。**建議一併安裝**
  （安裝方式見根目錄 [README](../../README.md) 的〈安裝 skill〉一節）。

## 相容性範圍

這些檔案使用 OpenCode 的 agent／command frontmatter 格式
（`description`、`mode`、`permissions`、`tools`、`agent`、`$ARGUMENTS`），
**格式層面只保證 OpenCode**；Claude Code、Codex 等其他平台的格式不同，
本 repo 不宣稱相容。詳細的來源依據見
[docs/research/portability.md](../../docs/research/portability.md)。

另外要注意：`pm.md` 引用 `tools.openchamber` 的模型查詢與 session 工具及
`openchamber_knowledge` 專案知識面板工具、`pm.md` 與 `implementer.md` 提到用
瀏覽器面板驗證成果——這些是 **OpenChamber**（以 OpenCode 為底的工作台）提供的
工具，純 OpenCode（TUI）沒有。`translator.md` 用 `subagent` 呼叫 `pm`；需要
`pm` 設為 `mode: all` 與 workspace 的 `experimental.subagent_depth: 2`。
沒有這兩個條件時，它會退回「輸出交接單，請使用者切到 `pm` agent 貼上」。agent 在純 OpenCode 可以載入並運作，但走到這些指引時模型需要
自行變通（例如改用 CLI 指令或口頭回報）；`/wish` 的完整 PM 流程以 OpenChamber
環境為預設。
