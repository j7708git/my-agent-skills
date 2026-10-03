# 檢查與報告判讀

這份文件說明各模式實際涵蓋什麼、報告欄位怎麼讀、政策能調什麼，以及哪些事
這個工具**做不到**。需要執行時的回報與處置順序，請先看 `SKILL.md`。

## 各模式的涵蓋範圍

| 模式 | 讀什麼 | 明確不含 |
| --- | --- | --- |
| `--staged` | index 內準備進入 commit 的檔案內容；作者／提交者 metadata | 工作目錄尚未 staged 的變更 |
| `--range A..B` | A 到 B 之間實際存在的 commit 訊息、作者 metadata、patch 新增行 | A 之前的歷史 |
| `--history` | 所有本機 refs 可達的 commit metadata 與 patch | reflog、遠端未 fetch 的物件、dangling object、已刪除的遠端 ref |
| `--pre-push` | 從 Git stdin 讀到的 refs，只掃這次真的會推上去的 commit | 沒有變動的 ref；刪除推送只標示不掃內容 |
| `--github` | GitHub 專案的可見度與安全類設定 | 遠端檔案內容、歷史 commit、secret 比對（不代替 Gitleaks） |

判讀時先問一句：**這次的結論適用範圍是什麼？** 把模式與範圍講清楚，
比列出多少筆發現更重要。

## 引擎分工

| 引擎 | 角色 | 不可取代之處 |
| --- | --- | --- |
| Gitleaks | 高風險密鑰的正式比對 | 缺少時整體狀態為不完整（退出碼 2） |
| 內建有限 regex | 常見格式樣式比對 | 覆蓋率遠低於 Gitleaks，只能補充 |
| 身分規則（預設啟用） | author／committer 的 email、電話型 email、姓名 | 可用 `metadata.enabled` 或 `--no-metadata` 關閉 |
| 路徑規則（預設啟用） | 敏感檔名、範本檔豁免、個人本機路徑 | 依 `paths.*` 設定 |
| 台灣 PII 規則（預設關閉） | 身分證、信用卡號、手機號 | `pii.enabled` 為 false 時完全不跑 |
| GitHub API（僅 GitHub 模式） | 唯讀設定查詢 | 不做內容比對 |

Gitleaks 與內建引擎掃的是**同一組 commit**，所以報告宣稱的範圍與實際掃描一致；
缺少 Gitleaks 時報告會明確標示引擎不可用，並把整體狀態降為不完整。

## 嚴重度與退出碼

| 嚴重度 | 意義 | 預設是否阻擋 |
| --- | --- | --- |
| `high` | 疑似可用憑證、電話型 email、敏感檔名（依政策） | 是（退出碼 1） |
| `medium` | 個人本機路徑、GitHub 可見度／安全設定、需人工確認的樣式 | 否 |
| `low` | 一般 author email、台灣手機號樣式等 | 否 |

嚴重度是政策決定的，不是工具硬編碼。退出碼只看「有沒有 high」與「掃描是否完整」：

- 有 high 且完整 → `1`
- 沒有 high、完整 → `0`
- 不完整、被上限截斷、**檔案因過大未掃**、工具失敗 → `2`（不論有沒有發現）

## 報告欄位（JSON）

```bash
bash scripts/repo-privacy-audit.sh --staged --format json
```

| 欄位 | 內容 |
| --- | --- |
| `schemaVersion` | 報告格式版本（目前 `1.0`） |
| `tool` | 工具名稱與版本（目前 `0.2.0`） |
| `mode` | 本次模式（staged／range／history／pre-push／github） |
| `status` | `passed` / `findings` / `incomplete` / `error` |
| `exitCode` | 與實際退出碼一致，方便腳本直接判讀 |
| `target` | 被掃描的 repo 或 GitHub 專案 |
| `policy` | 政策來源、id、雜湊、規則數、PII 是否啟用、遮罩預覽字數、身分規則是否啟用 |
| `coverage` | `scope`（實際掃了什麼）、`engines`、`limits`、`truncated`、`limitations` |
| `summary` | 總數與各嚴重度數量、最嚴重等級 |
| `findings` | 逐筆發現（已遮罩） |
| `errors` / `warnings` | 為什麼不完整、哪裡被略過 |

`coverage.limitations` 是這個工具的誠實清單，**回報時不要略過它**。它是
「不可視為通過」的具體理由。

## 單筆發現怎麼讀

每筆發現包含：

- `ruleId`：命中的規則代號。常見的有 `commit-metadata-phone-email`、
  `commit-metadata-email`、`commit-metadata-name`、`sensitive-filename`、
  `personal-local-path`、`github-repo-public`、`github-security-feature-disabled`，
  以及各 Gitleaks 與內建 regex 的規則代號。
- `severity`：`low` / `medium` / `high`。
- `source`：引擎與來源類型。
- `location`：`kind`（如 content、commit、commit-metadata、github-settings）、
  `ref`、`commit`、`path`、`line`，依模式而定。
- `masked`：遮罩描述。預設 `previewChars` 為 `0`，只給長度與 SHA-256 前綴，
  **不含任何秘密字元**。
- `description`：規則說明與來源說明。

發現裡的 `path` 與 `line` 是**你要自己開檔確認的線索**，不是可以直接複製貼上的秘密。
回報給使用者時，貼遮罩描述，不要要求使用者把秘密貼進聊天、issue 或公開 gist。

掃描到的秘密字串、電話型 email 與個人路徑會被整體從報告中移除，
包括出現在檔名、作者名與描述文字裡的情形。

## 政策欄位

範例見 `assets/policy.example.json`。政策是純 JSON 資料，不會被當成程式碼執行。

### 頂層

| 欄位 | 作用 | 建議 |
| --- | --- | --- |
| `schemaVersion` | 政策格式版本 | 保持 `1.0` |
| `id` | 政策識別，會出現在報告裡 | 取一個能分辨用途的名稱 |
| `description` | 自由說明 | 可留空白 |

### `paths`（路徑與檔名判定）

| 欄位 | 作用 | 預設 |
| --- | --- | --- |
| `paths.skipGlobs` | 直接跳過的路徑樣式 | 二進位與封裝檔等 |
| `paths.maxFileBytes` | 單一檔案大小上限 | `1048576`（1 MiB） |
| `paths.sensitiveGlobs` | 檔名符合即視為敏感 | 含 `id_rsa`、`*.pem`、`credentials`、`secrets.*` 等 |
| `paths.sensitiveSeverity` | 敏感檔名的嚴重度 | `high` |
| `paths.templateGlobs` | 範本檔豁免（只豁免**檔名**判定，內容照掃） | `.env.example`、`*.template`、`*.sample` 等 |
| `paths.personalPathPatterns` | 個人本機路徑樣式 | `/Users/<user>/`、`/home/<user>/`、`C:\Users\<user>\` |
| `paths.personalPathSeverity` | 個人路徑的嚴重度 | `medium` |
| `paths.personalPathAllow` | 允許的路徑前綴 | `/home/runner/`、`/home/ci/`、`/Users/runner/`、`/Users/ci/` |

範本豁免只影響 `sensitive-filename` 這條規則：`id_rsa.example` 不會被當成
敏感檔名，但檔案裡的內容仍然照常比對。這是刻意的，避免把範例檔誤判成真憑證。

### `metadata`（身分規則，預設啟用）

| 欄位 | 作用 | 預設 |
| --- | --- | --- |
| `metadata.enabled` | 是否套用 email／姓名規則 | `true` |
| `metadata.emailSeverity` | 一般 author／committer email | `low` |
| `metadata.phoneEmailSeverity` | local part 像電話號碼的 email | **`high`** |
| `metadata.nameSeverity` | author／committer 姓名 | `off` |
| `metadata.scanCommitMessage` | 是否用秘密／PII 規則掃 commit 訊息 | `true` |
| `metadata.allowedEmails` | 允許的 email（完全相同或 glob） | `[]` |
| `metadata.ignoreNoReply` | 略過已知 no-reply 平台的一般 email 發現，不能蓋掉電話風險 | `true` |
| `metadata.knownNoReplyDomains` | 精確比對已知 no-reply domain，可由政策覆寫 | GitHub／GitLab 已知 domain |

**三件要分清楚的事**：

1. **電話型 email 預設就是 `high`，而且預設啟用。** 例如
   `0912345678@example.invalid` 這類 local part 是電話號碼的地址，
   預設就會產生 `commit-metadata-phone-email`（high）並擋住 commit。
   它屬於身分規則，**不需要**打開任何 PII 開關。
2. **一般 email 預設 `low`**，不阻擋。要阻擋請**明確**把
   `metadata.emailSeverity` 調成 `medium` 或 `high`。
3. **台灣 PII 規則（身分證、信用卡號、手機號）是另一組，預設關閉。**
   由 `pii.enabled` 控制，與身分規則無關。

`allowedEmails` 支援完整字串或 glob（`fnmatch` 語意），比對前會轉小寫，
所以大小寫不敏感。它比「把 email 嚴重度關掉」精細：可以只放行自己或組織的網域，
其他人的 email 仍然照常被回報。`ignoreNoReply` 只認已知平台的精確 domain，不以字串含有 noreply 就放行。
電話型數字前後有字母仍屬高風險。GitHub 標準匿名身分的數字帳號 ID 不視為電話；
只有在精確 domain 與合法 `數字ID+login` 形狀下，改檢查 login 段，login 內的電話仍會被回報。

### `pii`（台灣個資格式規則，預設關閉）

| 欄位 | 作用 |
| --- | --- |
| `pii.enabled` | 是否啟用（預設 `false`） |
| `pii.rules` | 自訂規則；省略時使用內建規則 |

內建規則三條：

| ruleId | 比對 | 嚴重度 |
| --- | --- | --- |
| `tw-national-id` | 台灣身分證字號（含 checksum 驗證） | `medium` |
| `tw-mobile-number` | 台灣手機號碼（無 checksum，誤報率較高） | `low` |
| `credit-card-number` | 信用卡號（Visa／Mastercard／Amex／JCB／Diners，含 Luhn 驗證） | `high` |

**內建不提供統一編號規則**，而且政策裡寫 `validate: "tw_uniform_number"`
會被**明確拒絕**並退出 2：統一編號第 7 碼為 7 的特例未實作，硬做會給出錯誤判斷。
需要時請改用自訂規則或略過這一類。

### `masking`、`limits`、`githubSettings`

| 欄位 | 作用 | 預設 |
| --- | --- | --- |
| `masking.previewChars` | 遮罩時最多顯示的開頭字元數（0–8） | `0`（不顯示任何秘密字元） |
| `limits.maxCommits` | 單次掃描的 commit 數上限 | `2000` |
| `limits.maxFiles` | 單次掃描的檔案數上限 | `4000` |
| `limits.maxTotalBytes` | 單次掃描的總位元組上限 | `67108864`（64 MiB） |
| `githubSettings.visibilitySeverity` | `--github` 發現 public repo 的嚴重度 | `medium` |
| `githubSettings.securitySeverity` | `--github` 發現安全功能被停用的嚴重度 | `medium` |

`previewChars` 大於 0 時政策會產生警告，提醒輸出會包含秘密前幾個字元。
**保持 `0` 最安全**，也是範例政策的設定。

## 上限、截斷與被跳過的檔案

以下情況都會讓狀態降為 `incomplete`、退出碼 `2`：

- **檔案超過 `paths.maxFileBytes`**：該檔案**不掃內容**，記為 `errors`。
  這一點常被誤解成只是警告——它是 error，因為那個檔案確實沒被掃過。
- commit 數、檔案數或總位元組超過 `limits`：截斷，`coverage.truncated` 為真。
- 缺少 Gitleaks、外部工具失敗、政策檔有錯。

截斷或跳過後的結果**不能**用來宣稱「掃過整個專案」。要嘛提高上限重跑，
要嘛明確承認這次只掃了一部分。

## 工具與網路行為

- 掃描核心本身不使用網路。
- `--github` 是唯一會連網的模式，只呼叫讀取用的 API，不寫入、不改設定。
- `--staged`、`--range`、`--history`、`--pre-push` 只讀本機 Git 物件。
- Gitleaks 與 GitHub CLI 都不會被自動安裝；缺少時照實回報。
- hooks 只在你明確執行安裝指令時才存在於單一 repo 內。

## 已知不涵蓋（不要宣稱已檢查）

1. 遠端仍存在但本地不可達的物件、已刪除的遠端 ref、reflog。
2. fork、fork 網路，以及曾經公開過的歷史。
3. 依賴檔案鎖定檔以外的第三方套件內部內容、執行期產生的憑證。
4. 未啟用 `pii.enabled` 時的台灣個資類型。
5. 檔名或路徑本身不在 `paths.personalPathPatterns` 命中範圍內的個人資訊。
6. GitHub 專案設定中查不到的項目——「未啟用」與「權限不足」無法區分，一律記為未檢查。
7. 語意層面的隱私問題，例如文件寫法不當但沒有可掃描的樣式。

## 自檢：確認安裝完整

在安裝了這個 skill 的環境裡，可以先做這些唯讀檢查：

```bash
# 1. 檔案是否齊全（SKILL.md、scripts/、assets/、references/）
ls scripts references assets

# 2. 腳本語法
bash -n scripts/repo-privacy-audit.sh
bash -n scripts/rpa-hooks.sh
python3 -c 'import ast,pathlib; ast.parse(pathlib.Path("scripts/audit_core.py").read_text())'

# 3. 政策是合法 JSON
python3 -c 'import json;json.load(open("assets/policy.example.json"))'

# 4. 內建規則與政策預設（JSON）
bash scripts/repo-privacy-audit.sh --print-default-rules

# 5. hooks 狀態（唯讀）
bash scripts/rpa-hooks.sh status --repo .
```

第 4 步可以直接看內建規則、預設路徑樣式與所有政策預設值，不用靠文件轉述。
第 5 步在還沒安裝 hooks 時是正常的，它只會告訴你「未安裝」。
不要為了讓檢查好看而去安裝 hooks。
