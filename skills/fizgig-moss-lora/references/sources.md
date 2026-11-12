> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 來源與版本範圍

查閱日期：2026-11-12。Fizgig 參考版本為 v9.5.1／`f1d1bb21a61654d9fc1263ce233f5a04dcaa1af9`；OpenMOSS 官方 repo 參考 `6be4453024c5acae9dcd0a191e26ee524e743310`。這些是來源版本定位，不是使用者必須升降版的指令。

## 依證據做決定

- **模型官方**：架構、凍結 TE、Base／SFT 定位與預訓練 caption 方法。預訓練超參數不直接等於人物 LoRA 配方。
- **工具文件／實作**：參數存在、當版預設、cache 與 steps 行為；源碼與當版 release 優先於尚未更新的範例。
- **作者／社群實測**：只支持原本資料與版本的觀察。工具作者的相似度、VRAM 或速度數字不是本機保證。
- **本 skill 的試驗建議**：40＋8 配置、配比、20 epoch 試訓、mask／boost 關閉基準、固定 seed 矩陣；需在使用者素材驗證。

## 模型官方

| 來源 | 用於核對 |
| --- | --- |
| [OpenMOSS USERGUIDE](https://github.com/OpenMOSS/MOSS-Image/blob/6be4453024c5acae9dcd0a191e26ee524e743310/USERGUIDE.md) | Base 推論、caption、TE 凍結、自然語言／tags／layout |
| [MOSS-Image 技術報告 v1](https://arxiv.org/html/2610.23374v1) | 5B DiT、42 blocks、Qwen3.5-9B、FLUX.2、16 像素 stride、短中長描述 |
| [Base](https://huggingface.co/OpenMOSS-Team/MOSS-Image-Base)／[SFT](https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT) model cards | 模型來源與變體；實際下載另記 revision／hash |

## Fizgig 維護者來源

| 來源 | 用於核對 |
| --- | --- |
| [v9.5.1 release](https://github.com/shootthesound/Fizgig/releases/tag/v9.5.1) | Element boost 與版本差異 |
| [MOSS 指南](https://github.com/shootthesound/Fizgig/blob/v9.5.1/docs/MOSS_IMAGE.md) | 模型檔、訓練路線、作者觀察、快速／完整預覽 |
| [MOSS family](https://github.com/shootthesound/Fizgig/blob/v9.5.1/src/fizgig/families/moss_image.py) | Preset、Muon、Slide、TE 支援、精度與 targets |
| [Trainer](https://github.com/shootthesound/Fizgig/blob/v9.5.1/src/fizgig/families/train.py) | CLI、warmup、dropout、accumulation、resume |
| [Cache](https://github.com/shootthesound/Fizgig/blob/v9.5.1/src/fizgig/families/cache.py) | 兩階段快取、caption matching 與 TE hash 限制 |
| [Dataset schema](https://github.com/shootthesound/Fizgig/blob/v9.5.1/src/fizgig/dataset/config.py)／[loader](https://github.com/shootthesound/Fizgig/blob/v9.5.1/src/fizgig/dataset/image_dataset.py) | 支援欄位、圖片列舉、分桶、裁切 |
| [GUI user data](https://github.com/shootthesound/Fizgig/blob/v9.5.1/src/fizgig/user_data.py)／[launch](https://github.com/shootthesound/Fizgig/blob/v9.5.1/src/fizgig/families/launch.py)／[form](https://github.com/shootthesound/Fizgig/blob/v9.5.1/src/fizgig/families/form.py) | 原生設定位置、GUI 狀態與 CLI 映射；先讀目前安裝對應檔 |
| [requirements](https://github.com/shootthesound/Fizgig/blob/v9.5.1/requirements.txt)／[INSTALL](https://github.com/shootthesound/Fizgig/blob/v9.5.1/docs/INSTALL.md) | 安裝版本；舊 CUDA 文字有漂移，核對實際 requirements |
| [caption 指令](https://github.com/shootthesound/Fizgig/blob/v9.5.1/src/fizgig/krea2/caption_text.py)／[mask 指南](https://github.com/shootthesound/Fizgig/blob/v9.5.1/docs/MASKED_TRAINING.md) | 可見特徵描述、遮罩語意；Krea 起源經驗不冒稱 MOSS 消融結果 |

未找到公開受控證據能決定所有人物的最佳張數、固定外貌是否省略、rank／LR 或跨服裝泛化配置。社群 LoKR 成功回報也不能直接當一般 LoRA 的同條件測試。遇到文件漂移先用本機 revision 查來源，保留未確認項，不補造功能或數字。

## Agent 載入位置

[Codex 官方 skill 說明](https://learn.chatgpt.com/docs/build-skills)與 [OpenCode 官方 skill 說明](https://opencode.ai/docs/skills/)皆列出使用者層級的 `~/.agents/skills/`；可共用一份完整 skill 目錄。查閱日期同上。這是文件確認的載入位置，不代表已在兩個 client 實測本 skill 的觸發與操作。
