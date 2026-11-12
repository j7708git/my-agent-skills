> 本檔若與程式碼、測試或實際指令不符，以程式碼為準；安全邊界、授權與不可逆操作則以本檔為準。發現不符時：照程式碼做，並在回報中指出「本檔第 {line} 行已過期」。

# Caddy 入口的 401：判別、帶 token 與放行

這份文件說明 vast.ai ComfyUI image 的對外入口為什麼回 401、三種不必改機器的
帶 token 方式，以及真的要放行單一 port 時 env 該放哪裡、怎麼驗收。
數字與行為都以**目標 instance 的實際 image** 為準；換 image 版本就可能不同。

## 症狀與判別

從外部打 ComfyUI 的對外 port：

```bash
curl -sI http://<public_ip>:<host_port>/system_stats | head -5
```

若回應是：

```text
HTTP/1.1 401 Unauthorized
Server: Caddy
Www-Authenticate: Basic realm="restricted"
```

那是 **vast.ai 的 Caddy 入口代理在擋，不是 ComfyUI**。ComfyUI 只綁
`127.0.0.1`，實際的內部 port 由 `/etc/portal.yaml` 決定，不要假設固定值。
`/etc/Caddyfile` 由 `caddy_config_manager.py` 依 `/etc/portal.yaml` 生成。

## 不要整份讀 Caddyfile

`/etc/Caddyfile` 由 `caddy_config_manager.py` 依 env 生成，`basic_auth` 的 hash 與
cookie／Bearer 的 token 字面值都在裡面。整份貼進對話或 log，等於把這台 instance
的入口交出去。

只用不會輸出 token 的查法：

```bash
grep -c basic_auth /etc/Caddyfile
sed -n '/^:8188 {/,/^}/p' /etc/Caddyfile | grep -c basic_auth
grep -n '^:[0-9]* {' /etc/Caddyfile
```

- **不要** `cat /etc/Caddyfile`、不要 `grep token`，也不要把整個 block 貼進對話或 log。
- 上面三條只輸出計數與 block 開頭的行號，足以判斷某個 port 的 block 有沒有 `basic_auth`。

## 三種帶 token 的方式（不必改機器）

Caddy 的服務 block 同時接受這幾種憑證：

| 方式 | 形態 | 備註 |
| --- | --- | --- |
| 標頭 | `Authorization: Bearer <token>` | 適合程式呼叫 |
| 查詢參數 | `?token=<token>` | 適合貼在瀏覽器網址列 |
| cookie | `C.<instance_id>_auth_token=<token>` | 入口頁登入後由瀏覽器帶上 |

token 在 instance 內是 `$OPEN_BUTTON_TOKEN`（與 `$JUPYTER_TOKEN` 同值），
換一台 instance 就換一組。打服務的**內部 port** 或走 SSH 通道則完全繞過這層。

## `external_port` 與 host port 是兩個不同的數字

`AUTH_EXCLUDE` 比對的是 `/etc/portal.yaml` 裡該服務的 `external_port`，
**不是**對外映射的 host port。兩者常被混用，填錯不會報錯，只會靜默不生效。

| 名稱 | 哪裡來 | 例子 | 用途 |
| --- | --- | --- | --- |
| `external_port` | `/etc/portal.yaml` | 8188（固定，依服務而定） | `AUTH_EXCLUDE` 要填這個 |
| host port | `$VAST_TCP_PORT_<external_port>` | 30347（每台不同） | 對外呼叫 API 用這個 |

依據是 `caddy_config_manager.py` 的 `is_port_auth_excluded(external_port)`；
`generate_caddyfile` 傳進去的就是 portal.yaml 的 `external_port`。
**填成 host port 不會報錯，只是靜默沒生效，驗收時會看到仍然 401。**

## 放行單一 port

`generate_caddyfile()` 的分支是：

```python
if enable_auth and not is_port_auth_excluded(external_port):
    caddyfile += generate_auth_config(...)
else:
    caddyfile += generate_noauth_config(...)
```

所以 `AUTH_EXCLUDE=<external_port>` 只讓該服務的 block 變成無驗證；
其他服務（Instance Portal、API Wrapper、Syncthing）的 basic_auth 與 token 都保留。
`ENABLE_AUTH=false` 則是**全部**關掉，兩者的影響範圍不同，不要混用。

## env 要放哪裡

`/opt/supervisor-scripts/utils/environment.sh` 的內容是：

```bash
set -a
. /etc/environment 2>/dev/null
[[ -f ${WORKSPACE}/.env ]] && . ${WORKSPACE}/.env 2>/dev/null
set +a
```

- `/etc/environment` 開頭標著「Template controlled environment」，
  **重啟會照啟動模板重生**，改這裡會消失。
- `${WORKSPACE}/.env`（即 `/workspace/.env`）在它之後 source，會覆蓋，
  且跟著容器磁碟留著 → **env 放這裡**。注意它會被**所有** supervisor 服務 source。

## 為什麼一定要 `supervisorctl restart caddy`

`caddy_config_manager.py` 只在 caddy 啟動時跑一次，而 Caddy 是 `admin off`，
沒有 reload 路徑。所以：

- 必須 `supervisorctl restart caddy` 才會重生 `/etc/Caddyfile`。
- 手改 `/etc/Caddyfile` 沒用，下次啟動就被覆寫。

## 操作

```bash
printf 'AUTH_EXCLUDE=<external_port>\n' > /workspace/.env && chmod 600 /workspace/.env
supervisorctl restart caddy     # Portal 斷線數秒；SSH 不受影響
```

重啟 Caddy 會短暫中斷入口，屬於當輪要明確授權的動作；載入 skill 不等於授權。

## 驗收

| 位置 | 改前 | 改後 |
| --- | --- | --- |
| ComfyUI `/system_stats`（放行的 `external_port`） | 401 | 200 |
| Instance Portal | 401 | 401 |
| API Wrapper | 401 | 401 |
| Syncthing | 401 | 401 |

- 其他服務仍回 401，才證明只放行了指定的 port，沒有全部關掉驗證。
- 再 `supervisorctl restart caddy` 一次，ComfyUI 仍回 200，
  才證明是 env 生效，而不是手改檔案的暫時結果。
- 放行後還要跑一次端到端（`POST /prompt` → `GET /history` → `GET /view`），
  只看 `/system_stats` 回 200 不算完成。

## 風險

- 放行後的 ComfyUI 在公網**完全沒有驗證**。`vastai/comfy` 的啟動參數帶
  `--enable-cors-header`，任何網頁的 JS 都能打；ComfyUI 的 custom node 又能
  讀寫容器檔案，等於半個遠端執行面。**只在租用期間開，用完停 instance 或改回去。**
- `supervisorctl restart caddy` 是唯一有風險的動作：起不來就 Web 入口全斷。
  SSH 是獨立服務，可用 `supervisorctl status` 與 `/var/log/portal/caddy.log` 檢查。
- Jupyter 在這個 image 常是 `EXITED`，打它的 port 會連線被重設，
  那是它沒在跑，不是驗證問題。

## 官方指南的邊界

官方指南明講不要 `stop` `caddy`／`instance_portal`／`tunnel_manager`。
`restart` 可以，但要知道那是管理面：入口中斷時 SSH 仍可用來復原。
