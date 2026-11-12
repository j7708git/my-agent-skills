> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 資料整理與 caption

目標是可追溯的乾淨輸入，不是用張數補足缺少的資訊。以下配比為建議試驗，非 MOSS 已證明的最佳值；來源分類見 [sources.md](sources.md)。

## 從盤點到輸出

1. 確認使用者指定的 source；遵循該專案原有結構。若專案規定 origin／gen／train／rejected，就保留此結構；其他專案不強套這套命名。
2. 跑 `dataset.py audit`，讀完整報告：圖檔、同名 caption、hash 重複、輔助檔與未檢查項目。它只找位元完全相同的重複，不代表已找完連拍或視覺近重複。
3. 用看圖工具檢視候選，按拍攝場次／原始來源組分群。特寫、角度、服裝、背景、表情與明顯問題由實看記錄；不能從檔名猜。
4. 由 agent 建立 selection manifest，不把手寫清單的工作交回使用者。選圖與 caption 修正的理由要能回溯；待確認的圖片先不放 train。先跑 prepare 預檢，處理錯誤後在已授權的整理範圍內加 `--apply`。
5. 檢查輸出圖與 caption 數、holdout 隔離、來源 hash 與未選檔案。Fizgig 只指向輸出中的 train，不指向含 holdout／rejected 的父目錄。

若沒有看圖能力，完成盤點與結構預檢，將選圖品質與 caption 真實性列為未檢查；可沿用使用者已審核的明確清單，不能冒充已重新目視驗收。

## 選圖準則

| 判斷 | 操作 |
| --- | --- |
| 同一人物但不同外貌時期 | 依使用者目標選版本；年齡跨度、改形美顏、不同畫風臉型先分群 |
| 連拍、裁切、放大衍生 | 記同一來源組，選有新視覺資訊者；整組只能落在 train 或 holdout 其中一側 |
| 臉部清楚、景深背景模糊 | 可採用；以臉部資訊判斷，不因背景模糊淘汰 |
| 嚴重主體模糊、壓縮、瘦臉／改眼形濾鏡 | 列 rejected 並寫理由；保留原檔 |
| 遮擋或多人背景 | 主體須明確；少量可作變化，caption 寫出可見遮擋與主體位置 |
| 浮水印、字幕或拼圖 | 優先找乾淨原片；裁除須在副本上且不傷主體，不能靠省略 caption 消除像素 |
| 合成補圖 | 記生成來源，人工核對身分；先建立原始素材基準，再少量 A/B；holdout 使用獨立可信素材 |
| 一套衣服與一個背景總是共現 | 補交叉素材；只有 caption 寫 red coat 無法提供缺少的對照 |

可管理的試訓起點是 30–60 張。例：40 張 train 中 16 特寫、16 半身、8 全身；正面／左右四分之三角／側面／俯仰都有覆蓋，至少 5 套服裝和 5 類背景交錯；另留 8 張不同場次參考。8–15 張也可試，但缺失視角仍未知。這些是設計建議，不是最低張數或成功保證。

## 裁切與增強

- 保留原始長寬比，檢查 Fizgig bucket 裁後內容。v9.5.1 MOSS 尺寸按 16 像素對齊；約 1 MP 是面積目標。
- 同一全身照另加臉 crop 時，記同源、調整 caption，避免此來源因多 crop 佔過大權重。先完成裁切，再打標與快取。
- 首輪可開 `bucket_no_upscale`，不用強放大替代真細節。鏡像可能反轉痣與左右不對稱；強色彩變化可能破壞髮色／膚色。
- v9.5.1 已查 schema 沒有可供本流程使用的 caption shuffle、keep_tokens、flip_aug 或 random_crop 參數。離線產生副本與 trainer 即時增強分開記錄，依現行版本確認後才使用新功能。
- Fizgig 不保證忽略底線開頭圖檔。聯絡表、預覽、mask 與其他輔助圖不可被自動當訓練照片。

## Caption 規則與例子

先用英文自然語言，短段落描述可見內容。MOSS 接受 tags 與 JSON，但沒有充分人物消融證據證明它們優於自然語言。4096 token 上限不是建議長度。

選拼寫穩定且無強烈既有語意的 trigger，例如 `zyvra`，配適合圖片的類別詞。觸發詞是普通文字條件，不必是 tokenizer 單一 token；Fizgig 不訓練 TE。

描述需要控制的服裝、髮型、表情、姿勢、視角、背景與畫風。清楚可見的固定外貌可一致描述；「固定特徵全部省略」不是 MOSS 定律。省略不可見細節、故事與空泛讚美。不要為解耦而故意把圖寫錯。

| 情境 | 可改寫的範例 | 應修正的錯法 |
| --- | --- | --- |
| 正面特寫、中性表情 | `A close-up photo of zyvra, a woman, facing the camera with a neutral expression. Her dark hair is tucked behind one ear. Soft daylight falls on her face against a gray wall.` | 用 full body 描述特寫，或只堆 perfect face／masterpiece |
| 半身、藍襯衫、坐桌邊 | `A waist-up photo of zyvra, a woman, sitting at a wooden table in a blue shirt. Her hands rest on the tabletop and she looks to the side.` | 用 usual clothes 代替實際衣服 |
| 全身、步行、運動服 | `A full-body photo of zyvra, a woman, walking on a sidewalk in a black athletic jacket, gray trousers and white sneakers. Her arms swing naturally.` | 明明步行卻寫 standing，或衣色寫錯 |
| 側臉笑容、馬尾 | `A profile photo of zyvra, a woman, laughing with her teeth visible. Her hair is tied in a ponytail, with blurred trees behind her.` | 每張統一寫 looking at viewer／neutral |
| 紅毛衣、複雜咖啡館 | `A waist-up photo of zyvra, a woman, in a red sweater, holding a white cup. Shelves, hanging lights and blurred customers fill the cafe behind her.` | 把背景每個人都稱為 trigger，或編造看不清的背景文字 |

自動打標後逐張核對主體、左右、景別、衣色、表情、姿勢、遮擋；可見固定特徵按真值描述。改 caption 後重建 text cache；改圖或解析度後重新處理 latent 與 text cache。換 TE／tokenizer 也要新快取，不能假設舊 cache 已校驗 TE hash。

`caption_dropout` 丟掉整份 caption，包含 trigger；起始可用 0，後續單獨對照 0.05。它不等於 tag dropout，也不是治服裝綁定的第一步。

## dataset.py 介面

從 skill 根目錄執行；下列大寫內容代表實際路徑，不是需要建立的固定檔名。

```text
python scripts/dataset.py audit --source SOURCE --output REPORT.json
python scripts/dataset.py prepare --source SOURCE --manifest SELECT.json --destination NEW_ROOT
python scripts/dataset.py prepare --source SOURCE --manifest SELECT.json --destination NEW_ROOT --apply
```

省略 audit 的 `--output` 時 JSON 寫 stdout。selection 的 schema：

helper 處理 jpg／jpeg／png／webp／bmp；其他格式在 audit 標成 `non_image_file`，prepare 不接受。這是 helper 的範圍，不代表 Fizgig 不支援其他格式。PNG／JPEG 的 header 尺寸也不能證明圖片可完整解碼，仍須實際開圖。

```json
{
  "version": 1,
  "items": [
    {"image": "session-a/portrait.jpg", "split": "train", "group": "session-a", "caption": "An accurate, reviewed description with the chosen trigger."},
    {"image": "session-b/profile.jpg", "split": "holdout", "group": "session-b"},
    {"image": "session-c/blurred.jpg", "split": "rejected", "group": "session-c", "reason": "Subject is too blurred to assess facial detail."}
  ]
}
```

`image` 是 source 內的相對路徑；`group` 表示拍攝／來源組，不依 split 任意改名來通過檢查。可加 `sha256` 鎖定輸入內容。省略 `caption` 時讀同名 `.txt`；train 必須有非空 caption，其他 split 可沒有。

prepare 的 destination 父目錄須先存在；完整預檢後才建立新目的地，副本分在 train／holdout／rejected，manifest 放其父層。它保留 basename，拒絕跨來源同名或大小寫折疊後 stem 碰撞、source／destination 交疊、路徑逃逸、symlink／junction 與既有目的地；相同 hash 或來源組跨 train／holdout 也會被拒絕。輸出的 manifest 是結果紀錄，不可直接回餵為 selection；下一版要依 source 的相對路徑重建 selection。

以腳本 JSON 的實際 counts、findings 與退出碼判斷結果。成功複製不代表 caption 與人物品質已驗證；視覺判斷範圍另外記錄。必要遮罩由 Fizgig Image Prep 或已驗證的本機工具產生、逐張檢查；本 helper 不自動生成 mask。
