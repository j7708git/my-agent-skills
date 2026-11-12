---
name: fizgig-moss-lora
description: 整理 MOSS-Image 人物 LoRA 訓練集、檢查與修正 caption、調整 Fizgig 訓練設定，並比較 checkpoint 的人物相似度與服裝、姿勢、表情、場景控制。使用本機版本與設定格式核對功能，提供資料盤點、保留原檔的分組複製與可驗證的訓練配置；適用於 Fizgig 的 MOSS 人物任務，不把其他模型的配方直接套用。
license: MIT
---

> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# fizgig-moss-lora

讓同一人物能被穩定生成，同時保留換衣、姿勢、表情與場景的控制。先完成使用者當次要求的資料或設定工作；真正跑圖後才判定訓練效果。

輔助腳本需 Python 3.11 以上，只用標準庫；視覺檢查需 agent 的看圖工具，實際訓練另需 Fizgig 環境與支援的 GPU。

## 選擇本次路徑

| 使用者要求 | 要讀的內容 | 完成條件 |
| --- | --- | --- |
| 整理素材、去重、分 train／holdout、打標 | [資料與 caption](references/dataset.md) | 有選圖清單、拒收理由、準備好的副本與視覺檢查範圍 |
| 設定 Fizgig、改參數、建試訓配置 | [版本與設定](references/fizgig.md) | 已核對本機 schema，有可讀回的設定與改動摘要 |
| 開訓、比較存檔點、排查不像或綁定 | [驗證與排查](references/validation.md)；涉及配置再讀版本與設定 | 有實跑證據或精確的未測原因，結果能重現 |
| 問某項建議的依據、版本與文件衝突 | [來源與證據等級](references/sources.md) | 區分模型官方、工具實作、作者觀察與待驗建議 |

只讀本次需要的 reference。版本更新時讀當前來源，不把這份 skill 的 v9.5.1 參考配方當永久預設。

## 先確定工作上下文

1. 讀目標專案的 AGENTS 與資料集規範，確認素材來源、輸出目的地、Fizgig checkout／安裝位置及當次授權。
2. 優先讀現有 config、圖片與機器資訊；只有找不到且會改變本次動作的資訊才問使用者。GPU 未知不妨礙盤點資料、修 caption 或產生明確標為待驗的計畫。
3. 記錄訓練 Base、推論模型、TE、VAE、Fizgig revision；資料記錄來源組、caption 版本與選圖理由。產物遵循目標專案的紀錄位置，不依賴原研究的私人檔案。

## 執行原則

- 使用者已要求整理資料或調設定時，直接完成範圍內的副本、caption、配置與驗證。dry-run 是 agent 自檢，不是每個步驟都要再取得人類確認。
- 原始照片保持原樣。先檢查將寫入的目的地；同名輸出用新版本目錄。修改既有 Fizgig 設定時先備份、做最小 diff，再從實際入口讀回。
- 刪除原圖、覆蓋使用者檔案、外傳素材、租機或啟動長時間訓練，依對話已給的授權執行；授權不足才問缺少的那一項。使用本 skill 本身不新增這些授權。
- 先用本機 CLI、API 或已查明的設定檔；只有可見介面才能完成的動作才用可用的瀏覽器／桌面工具。不能假設所有 client 都有同名工具。
- 模型權重、人物圖片、caption、manifest 與本機路徑留在使用者指定的本機資料區。公開 skill 不承載私人素材或執行紀錄。

## 輔助工具

以下路徑相對於安裝後的 skill 根目錄，請用目前環境實際可用的 Python 3.11 以上解譯器：

```text
python <skill>/scripts/dataset.py audit --source <source> --output <report>
python <skill>/scripts/dataset.py prepare --source <source> --manifest <selection> --destination <new-output>
python <skill>/scripts/fizgig_config.py inspect --fizgig-root <checkout>
```

- [dataset.py](scripts/dataset.py)：盤點與 hash 去重；依人工／agent 看圖後的 manifest 複製分組。prepare 預設只預檢，加 `--apply` 才建立新輸出。
- [fizgig_config.py](scripts/fizgig_config.py)：讀本機 source 的 CLI schema，建立並驗證獨立 run bundle。`build` 預設預檢，`--apply` 寫新 bundle；不直接開訓。完整用法見版本與設定。
- 腳本只做可機械驗證的工作。相似身分、模糊、美顏、姿勢或 caption 是否真實，須實際看圖；看不到就記「未視覺檢查」，不依檔名編造內容。

## 結果回報

交付具體檔案／設定位置、採用與排除數量、改動前後關鍵值、實際跑過的檢查，以及尚未驗證的部分。資料整理完成、配置能解析、訓練能啟動、人物效果合格是四個不同結果，分別回報。
