> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# Fizgig 版本與設定操作

已核對的參考版本為 shootthesound/Fizgig v9.5.1（2026-11-12 查閱）；來源見 [sources.md](sources.md)。先檢查目前安裝，允許新版有不同 schema；不要為套用範例自行降版或重新安裝依賴。

## 找到實際操作入口

1. 從使用者提供的安裝路徑、啟動命令、執行中程序或專案設定定位 Fizgig。記錄 Git revision／release 與工作目錄；多份安裝時辨識使用者實際啟動哪份。
2. 跑本 skill 的 `fizgig_config.py inspect --fizgig-root CHECKOUT`。它只以 AST 讀 train／cache parser，不 import 或啟動 Fizgig。
3. 讀當前 MOSS family、train／cache parser、dataset schema。參數存在只證明可接受名稱，不證明某組合語意或模型相容。
4. 有 GPU 資訊才估硬體配置；缺少時仍可整理資料或準備明確假設的配置，將啟動與效能留為未測。

查原生 GUI 設定時，從當前 checkout 的 user_data 模組找實際儲存目錄與檔名；v9.5.1 有 user_data、presets 與 `.last_train_settings.json`。接著查 families 的 form／launch 與對應 web route，確認 GUI 欄位如何映射到 argv。CLI 的 `--learning_rate` 不代表 JSON 也叫相同名稱；GUI 可能使用大寫狀態鍵。

## 修改使用者現有設定

- 先讀現值與未知欄位，備份原檔，列出這次相關欄位的前後差異。保持使用者未要求修改的模型、資料路徑與其他 family 設定。
- 優先使用已查明的原生 API／匯入／儲存入口；寫設定檔前確認執行中的 UI 是否持有舊狀態而會覆蓋檔案。必要時透過使用者目前 UI 操作並存檔，不同時改記憶體與磁碟兩個來源。
- 編輯後以同一入口讀回。檢查頁面欄位、保存結果與實際將產生的 CLI；只有檔案可解析仍不足以證明 UI 採用。
- 找不到 schema 時，先搜尋當前 source 或官方文件；只產生獨立 run bundle 作可審查替代，不把猜測的 keys 寫入現有設定。未知新版仍能由 agent 查證後調整，不必被參考版產生器的限制擋住整個任務。

## MOSS 特有的不變項與版本注意

| 項目 | 已確認的參考行為與操作 |
| --- | --- |
| 訓練底模 | Base 訓練、SFT 生成是 Fizgig 推薦路線；SFT 精調不是蒸餾 |
| LoRA targets | Base 的 42 blocks，attention q/k/v/o 加 FFN up-gate/down，包含 block 22；不訓練 TE 或 text adapter |
| Block 22 | 訓練包含它；SLG 負向分支跳過它是推論操作 |
| TE | 首輪用支援的 Qwen3.5-9B bf16；v9.5.1 不支援 w4a8 GPTQ 層型別，其他文字特徵 bridge 要獨立驗證 |
| VAE | 支援 FLUX.2 small VAE；輸出外觀與完整 decoder 可能不同，記錄實際檔案 |
| 快速預覽 | v9.5.1 預設 Base＋Turbo＋SFT-look；挑成品要另做完整 SFT 比較 |
| 快取 | 圖、crop、解析度變更需重新處理快取；caption 變更重建 text；TE／tokenizer 變更用新 cache，原 matching 不完整校驗 TE hash |
| 依賴 | 參考版 requirements 與舊 INSTALL 文字有漂移；用當前固定版本安裝器／requirements，不把 OpenMOSS demo 依賴裝進 Fizgig 環境 |

Fizgig 的 MOSS 支援標為 experimental。作者記載特定 12 GB 卡配置可跑，不代表任意 rank／batch／預覽都能跑；先測快取、1–2 epochs 與一張預覽，才確定實機資源。

## 參考配方與調整順序

已核對的 Standard：rank／alpha 32／32、1 MP、Muon、LR 1e-4、EMA 0.98、70 epochs 上限、每 epoch 存檔、Slide `11:7:40:8:3`、Element boost 開啟。這是工具預設與作者觀察，不是所有人物的最優解。

helper 的兩種 profile 都以 **20 epochs 試訓**為預設：`standard` 保留作者 boost，`baseline` 關 boost 以便做單變因比較。baseline 不宣稱優於 Standard；正式 70 epochs 需明確指定，並以 checkpoint 選擇結果。

| 改什麼 | 起點／順序 | 何時調整 |
| --- | --- | --- |
| 資料／caption／模型與快取 | 第一優先 | 臉一直不像或改圖未生效，先排管線和資訊缺口 |
| Checkpoint／推論 strength | 先選候選，再測 0.6／0.8／1 | 晚期僵化、換衣或指令下降 |
| Epochs／LR | 20 試訓、1e-4，按矩陣趨勢調 | 持續改善才延長；迅速變形或學場景則降低曝光／LR |
| Rank／alpha | 32／32 | 資料充分仍學不進細節才另測容量；改 rank 時記錄 alpha 比例 |
| Batch／accumulation／repeats | 1／1／1 | 改後重算 updates 與曝光，不套舊 steps |
| 解析度 | 1 MP；Fast 0.25 MP 是另一個實驗 | 記錄臉佔像素與原片品質，換獨立 cache |
| 顯存 | Auto 精度、checkpointing on、Auto swap | OOM 先分清 cache／train／preview；更低精度與更低解析度各自比較 |
| Mask／boost／DOP／dropout | 一次測一項 | 背景綁定可測 mask；無 trigger 外溢可測 DOP；dropout 首輪 0 |

`constant` 與 `constant_with_warmup` 在此版都先執行非零 warmup；基準為 0。Slide 前 7 epochs 約 11%，第 8 到 40%，之後 epoch 11／14／17 等 refresh 回較低比例並半 LR。不能把 constant 稱為全程實際 LR 不變。

Element boost 的參考旗標是 `--face_weight 9 --face_area head --face_cycle 3:1 --face_until 12:600`。Head 會包含頭髮；髮型常變可另測 face。Mask 是 loss 加權，仍輸入完整圖片；檢查未偵測／錯位／全黑 mask 的回退，不假設完全解耦。

## 步數

普通單 GPU 圖片 LoRA、同一 dataset 設定下：

```text
micro-batches/epoch = sum(ceil(bucket_images * repeats / batch_size))
updates/epoch = ceil(micro-batches/epoch / accumulation)
total_updates = epochs * updates/epoch
```

40 張、batch 1、accumulation 1、repeats 1：20 epochs 是 800 updates，70 是 2800。batch 大於 1 時要有實際 bucket 分布才算精確；兩桶 21／19 張、batch 2 是 21 個 micro-batches，不是 20。UI global_step 計 micro-batches。此版使用 max_train_epochs，沒有 shared trainer 的 max_train_steps 旗標。

## fizgig_config.py

從 skill 根目錄呼叫，PYTHON 是 **Fizgig venv 的實際 Python 執行檔**，不是隨意沿用 agent 的 Python；各模型與資料位置須存在。

```text
python scripts/fizgig_config.py inspect --fizgig-root CHECKOUT
python scripts/fizgig_config.py build --fizgig-root CHECKOUT --python PYTHON --dataset TRAIN_DIR --dit BASE_FILE --text-encoder TE_FILE --vae VAE_FILE --preview-checkpoint SFT_FILE --trigger zyvra --destination NEW_BUNDLE --profile baseline
python scripts/fizgig_config.py build --fizgig-root CHECKOUT --python PYTHON --dataset TRAIN_DIR --dit BASE_FILE --text-encoder TE_FILE --vae VAE_FILE --preview-checkpoint SFT_FILE --trigger zyvra --destination NEW_BUNDLE --profile baseline --apply
python scripts/fizgig_config.py validate --fizgig-root CHECKOUT --bundle NEW_BUNDLE
```

可用 overrides：`--class-word person|woman|man`、`--epochs`、`--rank`、`--alpha`、`--lr`、`--batch-size`、`--accumulation`、`--repeats`、`--resolution W H`；以當前 `--help` 為準。先讀 dry-run 輸出再 apply，產生 dataset TOML、sample prompts 與 run JSON，不覆蓋既有 bundle，也不開訓。

自動產生器的已驗證範圍是固定 v9.5.1 revision 且相關 source 未修改。未知版本仍可 inspect，回 `needs-review`／退出碼 2；build 則回 `error`／退出碼 2。此時查當前 source 後由 agent 建立／修改原生設定，或先維持資料整理。不要更改 Git 標籤、偽造 revision 或忽略結果來繞過適用範圍。

run JSON 含 `schema_version: 1`、`cwd`、依序排列的 `commands`；每項有 `stage` 與 `argv`。validate 回 `static-valid`／退出碼 0 且已有開訓授權、模型與資料驗證完成時，再使用 subprocess 的 argv 陣列與該 cwd 執行 latents → text → train，任一步失敗即停並讀 log。不要拼成 shell 字串或使用 shell=True；設定中的 trigger 和路徑均是資料。

validate 通過代表已核對設定與可見檔案，不代表 safetensors 內容正確、GPU 足夠或人物效果達標。執行與結果判斷續看 [validation.md](validation.md)。
