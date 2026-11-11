> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# 模型清單 manifest

這份文件定義 `SKILL.md` Phase 3 逐列處理的 manifest schema，以及下載、驗證與
回填的語意。它只描述格式，不下載任何檔案，也不判斷模型是否適合你的專案。

## 為什麼要獨立一份

專案既有的 `config/models.tsv` 是給人看的：來源欄是自由文字，可能寫「官網」、
「朋友給的」或一個網址。程式拆不出 repo 與 revision，也無法逐檔驗證。
manifest 是同一批模型的**機器可讀版本**，兩者並存、用途不同：

- `config/models.tsv`：人看的清單與來歷。
- manifest：下載程式吃的欄位，每一列都能獨立重跑。

## Schema

七個欄位，以 TSV 儲存（第一列為表頭）：

| 欄位 | 必填 | 格式 | 說明 |
| --- | --- | --- | --- |
| `repo_id` | 是 | `owner/name` | Hugging Face repo，例如 `org/model-repo` |
| `revision` | 是 | commit hash 或 tag | **不要用 `main`**；固定 revision 才能重現 |
| `path_in_repo` | 是 | repo 內相對路徑 | 含子目錄與檔名，例如 `split_files/diffusion_models/x.safetensors` |
| `dest` | 是 | 相對 ComfyUI 根目錄 | 例如 `models/diffusion_models`；不含檔名 |
| `sha256` | 是 | 64 位十六進位 | 下載後逐位元比對，不符就停下回報 |
| `size_gb` | 是 | 十進位數字 | 用來在 Phase 2 加總比對磁碟可用量 |
| `note` | 否 | 自由文字 | 用途、來源頁或授權註記；不得寫入 token |

`dest` 的基準是 ComfyUI 根目錄（這個 image 是 `/workspace/ComfyUI`），
不是 instance 的 `/workspace`。寫成 `models/checkpoints` 代表
`/workspace/ComfyUI/models/checkpoints`。

## 範例

```tsv
repo_id	revision	path_in_repo	dest	sha256	size_gb	note
<owner>/<model-repo>	<commit-sha>	split_files/diffusion_models/<檔名>.safetensors	models/diffusion_models	<64 位 sha256>	6.46	主模型
<owner>/<vae-repo>	<commit-sha>	<檔名>.safetensors	models/vae	<64 位 sha256>	0.33	VAE
```

範例刻意全部用 `<…>` placeholder。實際使用時填真實值，但 manifest 本身
**不要**提交到公開 repo：裡面的 repo 名稱與 revision 可能透露你用了哪些私有模型。

## 下載語意

每一列都走同一條路徑，不因 repo 的目錄結構而變：

1. **staging**：用 `hf_hub_download` 的 `local_dir` 指到暫存目錄，
   例如 `/tmp/models-staging`。`snapshot_download` 的落點由 repo 內的目錄結構決定，
   換一個 repo 就會多一層或差一層，不要靠運氣。
2. **SHA256**：對 staging 的檔案算 `sha256sum`，與 manifest 的 `sha256` 逐位元比對。
   不符就停下回報，不要先搬到落點再說。
3. **`mv` 到 `dest`**：`mkdir -p` 建好 `<ComfyUI 根目錄>/<dest>`，再把檔案搬進去。
4. **ComfyUI 認得它**：查 `/object_info`，檔名出現次數大於 0 才算完成。
   檔案存在不等於節點看得到。

## 與 `config/models.tsv` 的對應

抓完之後把這三項回填到專案的 `config/models.tsv`：

| manifest 欄位 | 回填到 `config/models.tsv` 的什麼 |
| --- | --- |
| `path_in_repo` 的檔名 | 檔名欄 |
| `sha256` | 指紋欄（兩邊必須一致） |
| `size_gb` | 大小欄（換算成該表慣用的單位） |

兩邊指紋不一致時回報，不要自行挑一份當正確答案。
manifest 留在本機或專案的忽略路徑；要進版控前先確認它不含私有 repo 名稱與 token。
