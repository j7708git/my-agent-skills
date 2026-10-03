# 安全性政策

## 這個 repo 為什麼是 private

`j7708git/my-agent-skills` 是 private repo。裡面放的是掃描「隱私與秘密」的工具，
它的測試 fixtures 會刻意包含**看起來像真實秘密的合成字串**。這些字串全部是假的，
但公開場合容易造成誤會，也會讓秘密掃描工具產生雜訊，所以維持 private。

## 回報發現

- 對外沒有公開的漏洞回報信箱（不放置真實 email）。回報方式：
  1. 在本 private repo 開 issue 描述問題；或
  2. 直接告知維護者 `j7708git`。
- 回報請附上：版本（commit SHA）、你的作業系統、Python／Bash 版本、重現步驟、
  以及相關輸出的**遮罩片段**。
- 請勿在 issue 中貼上未遮罩的秘密原文，即使你認為那是誤判。

回應時程屬於盡力而為，不做 SLA 承諾。

## 發現疑似外洩秘密時的處理順序

1. **先撤銷／輪替憑證**。這一步永遠優先，清理 repo 歷史不能取代輪替。
2. 再評估是否需要清理歷史（需要明確授權，且會影響所有協作者）。
3. 最後才評估可見度調整（例如把 fork 轉回 private）。

這個 repo 不提供、也不建議任何自動修復指令。以下做法都不要使用：

- `pip install --force-reinstall`、`npm audit fix --force` 等強制覆蓋安裝來「清掉」套件風險。
- 自動改寫檔案內容、批次重寫歷史、`--force` 推送。
- 未經授權把 repo、fork 或 Actions 日誌轉為公開。

## 工具與網路

- 掃描核心不使用網路。需要 Gitleaks 做完整秘密檢查；缺少時狀態為 incomplete、退出碼 2。
- `--github` 模式只做唯讀查詢，不會寫入 GitHub；不會自動修改任何 GitHub 設定。
- hooks 只在明確指定時安裝在單一 repo 內，且不改 `core.hooksPath` 或全域 Git 設定。

## 這個 repo 自己的 CI

- 權限只有 `contents: read`，不需要任何 secret，也不會呼叫會改設定的 API。
- 行動與工具版本固定（action 以 commit SHA 釘選、Gitleaks 固定版本並校驗 SHA-256）。
- branch protection、required checks、Dependabot 等由維護者手動設定，CI 不代勞。

## 支援範圍

只支援 [docs/compatibility.md](docs/compatibility.md) 列出的環境。其他平台的問題
（例如 Windows 原生 shell）請先開 issue 討論，不做修復承諾。
