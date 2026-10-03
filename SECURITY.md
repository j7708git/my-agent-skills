# 安全性政策

## 回報發現

**先選一個私密通道，再貼任何內容。**

1. **首選**：若這個 repo 的 **Security → Report vulnerability** 可用，用它提交。
   它會開一則只有維護者看得到的私密討論，是首選通道。
2. **備選**：若該入口未啟用或你無法使用，**先與維護者約定一個私密通道**，
   再把細節送進去。不要跳過這一步。

**無論走哪一條路，都不要把秘密原文貼進公開 issue**（包括公開 issue 的
附件、gist、貼文或Discussion）。公開 issue 與公開附件會被搜尋引擎與第三方索引快取，
即使你事後刪除也已經算外洩。

回報請附上：版本（commit SHA 或 skill 版本）、你的作業系統、Python／Bash 版本、
重現步驟，以及相關輸出的**遮罩片段**（保留前後幾個字元即可）。

回應時程屬於盡力而為，不做 SLA 承諾。

## 這個 repo 為什麼需要特別小心

這個 repo 放的是掃描「隱私與秘密」的工具，它的測試 fixtures 會刻意包含
**看起來像真實秘密的合成字串**。這些字串全部是假的，但它們的形狀與真憑證相同，
因此：

- 在公開場合（issue、貼文）複製貼上時容易被誤會，也會讓秘密掃描工具產生雜訊。
- 這些合成字串**不是**可用憑證，不需要、也沒有人應該去「驗證」它們。
- 本 repo 不維護公開的漏洞回報信箱，也不在文件裡放真實 email。

## 發現疑似外洩秘密時的處理順序

1. **先撤銷／輪替憑證**（API key、token、密碼都算）。這一步永遠優先，
   清理 repo 歷史**不能**取代輪替——憑證一旦外洩，輪替是唯一確定有效的補救。
2. 再評估是否需要清理歷史（需要明確授權，且會影響所有協作者）。
3. 最後才評估可見度調整（例如把 fork 的可見度改回去）。

這個 repo 不提供、也不建議任何自動修復指令，也**不會自動修改任何 GitHub 設定**。
以下做法都不要使用：

- `pip install --force-reinstall`、`npm audit fix --force` 等強制覆蓋安裝來「清掉」套件風險。
- 自動改寫檔案內容、批次重寫歷史、`--force` 推送。
- 未經授權把 repo、fork 或 Actions 日誌轉為公開，或變更 branch protection、
  required checks、Dependabot 等設定。

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
請走上面的私密通道描述，不必在公開 issue 開單。