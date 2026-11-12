> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 驗證與排查

## 開始與完成條件

| 階段 | 驗證內容 | 能宣稱的結果 |
| --- | --- | --- |
| 資料準備 | 圖／caption 成對、來源分組、hash、輸出隔離、已看圖的品質範圍 | 資料副本已準備；未看圖項目另列 |
| 配置 | 當前版本、參數與 enum、dataset 路徑、模型檔、快取隔離、讀回設定 | 設定通過靜態核對 |
| 1–2 epoch smoke | log 無非有限 loss、LoRA 有非零更新、可載入並產生可觀測差異、train 與 preview 峰值 | 實機流程可跑，尚非人物品質驗收 |
| 小試訓 | 固定資料與解析度，比較 epoch 0／10／20 | 找到調整方向；不用單一 loss 當身份分數 |
| 正式訓練 | checkpoint 矩陣與實際部署模型效果 | 身分與可控性分別記錄，依結果挑版本 |

開訓前承接對話已給的授權；只有尚未授權的 GPU 工作、租機／費用或外傳才需要補問。幫使用者調設定不自動代表立即開始長時間訓練。

## 固定比較矩陣

以下為試驗設計，依目標調整並保存實際值：六個 prompt × 三個 seed（17、123、2026）；比較無 LoRA、epoch 10／20／35／50／70 中實際存在的版本。固定推論模型、TE、VAE、解析度、sampler、scheduler、CFG、SLG、negative prompt、strength。

以真正部署的 SFT 完整採樣選版。參考 Fizgig 配方是 res_multistep／simple、30 steps、CFG 4、shift 5、SLG layer 22／start 0／end 0.5；先查目前實作仍一致。Base＋Turbo＋SFT-look 快速預覽只用於監看，不與完整 SFT 圖直接混算訓練進步。

| Prompt 類型 | 要檢查的能力 |
| --- | --- |
| 正面中性表情、素牆特寫 | 五官與臉型 |
| 半身、未見過的衣色與坐姿 | 換裝與上半身姿勢 |
| 全身、白襯衫牛仔褲、海灘行走 | 比例、全身與新場景 |
| 側臉、大笑、頭髮綁起 | 弱勢角度、表情與髮型 |
| 正式外套、嚴肅表情、繁忙車站 | 複雜背景與語意控制 |
| 綠色運動服、舉手、攝影棚 | 肢體動作與服裝 |

另測相同 prompt 移除 trigger：人物是否不應出現時也自動生成。先 strength 1 選候選，再測 0.6／0.8／1。相同 seed 跨軟體或不同版本不保證逐像素相同。

每張分別記身分、服裝、姿勢、表情、場景、畫質，可用 0／1／2 分並保留失敗張數。以獨立 holdout 作參考，不只取最好的一張。ArcFace 等指標只看同一參考組與裁臉流程下的趨勢，0.72 不等於 72% 成功率；插畫與照片的分數不可直接混比。

## 問題定位

| 現象 | 先查 | 再改 |
| --- | --- | --- |
| 所有 checkpoint 都不像 | LoRA 是否載入、trigger、實際讀圖數、cache、Base／SFT／TE、非有限 loss／零更新 | 補缺少的臉部與角度資訊，再獨立調 epochs／LR／rank |
| 早期像，晚期難換衣或變形 | 同推論條件比較早晚；資料是否固定造型 | 早期 checkpoint／較低 strength；下一 run 減曝光或 LR |
| 衣服綁定 | 衣服頻率、caption、衣服與背景共現 | 補交叉素材；再單獨測 person-not-clothes mask |
| 姿勢僵化 | 是否全站姿／正面；crop 是否都變大頭；底模能否完成該 prompt | 補真實姿態與全身資料、修錯誤 caption |
| 背景反覆出現 | 單場次佔比、背景 caption、主體大小 | 平衡背景；必要時測 mask 降低背景 loss |
| 無 trigger 也出現人物 | 對照底模與較早 checkpoint | 降曝光／strength；另測 DOP，不與其他變因同改 |
| 只有 ComfyUI 不像 | 推論模型、TE 量化、VAE、loader、4-step／其他 LoRA、CFG／SLG | 對齊完整流程，再逐一加回加速與特徵 bridge |
| OOM／突然慢 | 出錯在 cache、train 或 preview；空閒 VRAM、RAM、shared GPU memory、compile 暖機 | Auto 精度、checkpointing、batch、swap 或預覽大小各自調整 |
| mask 開了反而差 | 每張 mask 是否對主體、全黑／錯位／未偵測回退 | 修 mask；和無 mask 基準比較，不假設功能已生效 |

int8 SFT 的 LoRA merge 可能受再量化影響；Fizgig 建議旁路 model-only loader。先確認當前 ComfyUI 有相應節點，再對照測試。既有 4-step、GPTQ TE 或 llama.cpp 文字特徵 bridge 是第二階段相容性測試。

## 一次一個可解釋的差異

每個 run 記錄資料／caption 版本、Fizgig revision、模型檔識別、rank／alpha、LR／optimizer、epochs、曝光、updates、解析度與額外功能。先修資料錯誤，再選單一參數做 A/B；至少固定驗證矩陣，候選可加第二個訓練 seed 複驗。

改 batch／accumulation／repeats 時，同 epoch、同曝光、同 updates 是不同對照。改解析度需新 cache；改 TE 或 caption 需重建文字快取。延長非 constant scheduler 的總 epochs 可能改曲線；續訓須保存完整訓練 state，不能只將 LoRA 當完整 resume。

結果回報附實際輸出位置與 log／矩陣；沒有執行的部分直接寫未測。不要把格式驗證、腳本合成測試或工具作者的分數當成本次人物訓練成果。
