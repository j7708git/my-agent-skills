---
name: vastai-comfyui-provision
description: 把剛租到的 vast.ai ComfyUI instance 弄到能用 API 出圖的七階段流程：取得 SSH、盤點 GPU 與磁碟與 ComfyUI 版本、依模型清單從 Hugging Face 下載並驗 SHA256、處理 Caddy 入口的 401、用一張真圖驗收、把實測數字回填。主機規格與模型清單都由現場盤點與 manifest 決定，不寫死任何型號或檔名。當使用者說「租了一台 vast.ai 的 ComfyUI」「幫我把模型搬上去」「本機 API 打這台回 401」時使用。
license: MIT
compatibility: 純文字流程指引，本身無執行時相依；實際操作需要目標 instance 的 root SSH（或 vastai CLI）、可用的 Hugging Face token，以及專案既有的一份 API 格式工作流。缺少任一項時停在原步回報，不以其他方式冒充已完成。
---

> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# vastai-comfyui-provision

這是一份流程指引，把「租到一台 vast.ai ComfyUI」到「能用 API 出圖」之間的事
固定成七個階段（Phase 0 到 Phase 6）。每一步都留下可核對的輸出，最後回報的是數字，不是形容詞。

- 下載模型會佔磁碟、重啟 Caddy 會短暫中斷入口。這兩件事各自需要當輪明確授權；
  載入本 skill 不等於授權。
- 它不設計工作流。Phase 5 只驗「專案現有的工作流能不能在這台上跑」，
  跑不動就回報，不順手改圖。
- 它不代管 token，也不宣稱免驗證的入口是安全的。風險要寫進回報。

## 什麼時候用

- 剛租到一台 vast.ai 的 ComfyUI，上面還沒有專案要的模型。
- 本機 API 打這台回 401 或連不上，需要判斷是哪一層在擋。
- 換了一台不同 GPU 的機器，要重跑一次「盤點 → 下載 → 驗收」。

## 前置條件

1. **目標 instance**：instance id，SSH 公鑰已掛上（或 `vastai` CLI 可用）。
2. **Hugging Face token**：需要讀 private repo 時必備；instance 內的 `HF_TOKEN`
   常已預設，先確認再談補。
3. **模型清單**：一份 manifest，schema 見 `references/model-manifest.md`。
4. **一份能跑的工作流**：專案現有的 API 格式 JSON，Phase 5 拿它驗收。

## Phase 0：取得存取

```bash
vastai attach ssh <instance_id> ~/.ssh/id_ed25519.pub   # 掛完約等 10 秒才生效
vastai show instance <instance_id>                      # 找 public ip 與 ssh port
ssh -p <ssh_port> root@<public_ip> 'nvidia-smi -L'
```

`ssh<N>.vast.ai:<port>` 這種 proxy 位址常常連不上，先試直連 IP。
把實際可用的連法記下來，後面每一步都用它。

## Phase 1：盤點

一則指令把事實塊一次抓齊。這些數字決定後面的每一步，不要用型號猜。

```bash
ssh -p <ssh_port> root@<public_ip> '
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
df -h /workspace | tail -1
grep -A3 "ComfyUI" /etc/portal.yaml        # 內部 port 與服務名
curl -s http://127.0.0.1:<internal_port>/system_stats | head -c 200
ls /workspace/ComfyUI/models
[ -x /venv/main/bin/python ] && /venv/main/bin/python -c "import huggingface_hub;print(huggingface_hub.__version__)"
command -v hf huggingface-cli
[ -n "$HF_TOKEN" ] && echo "HF_TOKEN set" || echo "HF_TOKEN missing"'
```

判讀規則：

- **ComfyUI 的 port 從 `/etc/portal.yaml` 讀**，不要假設固定數字；不同 image 版本會變。
- 這個 image 通常沒有 `hf` CLI，但有 `/venv/main/bin/python` 與 `huggingface_hub`。
  下載一律走後者。
- 磁碟看 `/workspace`，模型全部放這裡。

## Phase 2：決定規模

1. **磁碟**：manifest 的 `size_gb` 加總，與 `df` 的可用量比。不夠就停下回報，
   不要下載到一半。
2. **落點**：`/workspace/ComfyUI/models/<type>/<子目錄>`，子目錄沿用專案既有結構。
3. **起測規模**：專案有實測紀錄就用它；沒有就從最小設定起（較小解析度、少步數），
   先確認能出圖再往上加。**不要依 VRAM 推算解析度上限**，上限只有實測才算數。

## Phase 3：下載與驗證

先落 staging、驗 SHA256、再 `mv` 到落點。理由：`snapshot_download` 的落點由
repo 內的目錄結構決定，換一個 repo 就會多一層或差一層，不能靠運氣。

逐列處理 manifest：

```bash
/venv/main/bin/python - "<repo_id>" "<revision>" "<path_in_repo>" /tmp/models-staging <<'PY'
import sys
from huggingface_hub import hf_hub_download
repo, rev, path, stage = sys.argv[1:5]
print(hf_hub_download(repo_id=repo, filename=path, revision=rev, local_dir=stage))
PY

sha256sum /tmp/models-staging/<path_in_repo>          # 與 manifest 的 sha256 比對
mkdir -p /workspace/ComfyUI/models/<type>/<子目錄>
mv /tmp/models-staging/<path_in_repo> /workspace/ComfyUI/models/<type>/<子目錄>/
```

staging 會多一個 `/tmp/models-staging/.cache/huggingface/`，清 staging 時一起刪，不要只刪檔案。

三種驗證都要做，缺一不算下載完成：

1. **SHA256** 與 manifest 逐位元相符。
2. **專案若有 config/models.tsv**，兩邊的檔案指紋也要一致；不一致就回報，
   不要自行挑一份。
3. **ComfyUI 認得它**：

   ```bash
   curl -s http://127.0.0.1:<internal_port>/object_info | grep -c '<檔名>'
   ```

   出現次數大於 0 才算真的進到清單；檔案存在不等於節點看得到。

## Phase 4：入口

先判別 401 是誰發的，不要直接假設是 ComfyUI：

```bash
curl -sI http://<public_ip>:<VAST_TCP_PORT_8188>/system_stats | head -5
```

- `Server: Caddy` 加上 `WWW-Authenticate: Basic realm="restricted"` → 是 vast.ai 的
  入口代理。完整配方與驗收表見 `references/caddy-auth.md`。
- 預設建議用 token，不動機器；使用者明確要求免驗證才走 `AUTH_EXCLUDE`，
  並在回報寫出暴露風險。
- 手改 `/etc/Caddyfile` 沒用，重啟時會被重新生成。
- 查 Caddyfile 只看 `basic_auth` 的計數，不要整份讀；原因見 `references/caddy-auth.md`。

## Phase 5：端到端驗收

用專案現成的 API 工作流送一張，不要只看 `/system_stats` 回 200。

```bash
curl -s -X POST http://<public_ip>:<port>/prompt -H 'Content-Type: application/json' -d @payload.json
curl -s http://<public_ip>:<port>/history/<prompt_id>
curl -s -o out.png "http://<public_ip>:<port>/view?filename=<檔名>&subfolder=<子目錄>&type=output"
```

通過條件：檔案大小大於 0、能正常解碼、內容與提示相符。同時記下：

- 這次的解析度、步數、CFG、種子
- 出圖秒數
- 出圖期間的 VRAM 峰值（`nvidia-smi --query-gpu=memory.used --format=csv`）

## Phase 6：回填與收尾

1. **實測數字寫回專案**：config/models.tsv 補上新檔、README 或驗收紀錄補上
   秒數與 VRAM。只留在對話裡的數字等於沒有。
2. **記帳**：instance id、`$/hr`、這台實測的設定、未測項目。
3. **提醒停機**：instance 不關就在燒錢。

## 不要做

| 禁止 | 為什麼 | 怎麼驗 |
| --- | --- | --- |
| 改 `/etc/Caddyfile` | 啟動時由 `caddy_config_manager.py` 重生，改了會消失 | 重啟 caddy 後看設定還在不在 |
| 整份 `cat /etc/Caddyfile` 或 `grep token` | 裡面有明文 token，貼進對話等於交出入口 | 只用 `grep -c basic_auth` 之類的計數查法 |
| `stop` caddy／instance_portal／tunnel_manager | 官方指南列為管理面；restart 可以，stop 會斷入口 | `supervisorctl status` |
| 把 token、IP、instance id 寫進對話或 commit | 進版控等於外流 | 提交前跑隱私掃描 |
| 把模型放到 `models` 以外 | ComfyUI 看不到，且下次換機會漏 | Phase 3 第 3 項驗證 |
| 改專案工作流讓驗收通過 | 驗的是「這台能不能跑」，不是「圖能不能改到能跑」 | 工作流檔案 diff 應為空 |

## 失敗模式

| 症狀 | 真正的原因 | 處理 |
| --- | --- | --- |
| 401 且 `Server: Caddy` | 入口代理在擋，不是 ComfyUI | 走 `references/caddy-auth.md` |
| proxy 位址 SSH 連不上 | vast.ai 的 proxy 常不通 | 改用直連 `public_ip:ssh_port` |
| 掛完公鑰立刻 SSH 失敗 | 公鑰生效有延遲 | 等約 10 秒再試 |
| `hf: command not found` | image 沒裝 CLI | 改用 `/venv/main/bin/python` + `huggingface_hub` |
| 下載完模型節點看不到 | 落點多一層或差一層 | 比對 `models` 實際結構後搬正 |
| 打 8080 連線被重設 | Jupyter 服務本來就是 `EXITED` | 看 `supervisorctl status`，別當成驗證問題 |
| 改了 `/etc/environment` 重啟後失效 | 那檔案標著 template controlled | 改 `/workspace/.env` |
| 換 instance 後設定全沒了 | 設定跟著磁碟，不跨機器 | 照本流程重跑 |

## 回報格式

```text
事實塊：<GPU> / <VRAM> / <driver> / <磁碟可用> / ComfyUI <版本> / HF <有無 token>
下載：<n> 檔 / <總 GB> / SHA256 全對 or 哪一檔不符
入口：<埠> → <狀態碼>；其他服務 <狀態碼>（證明沒有一併放行）
端到端：<解析度> <步數> <CFG> → <秒數> / <bytes> / VRAM 峰值 <GB>
未測：<逐項列出>
花費：<instance id> / <$/hr> / 已提醒停機
```
