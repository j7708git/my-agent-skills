---
description: 獨立審查者（重要節點審查，如公開到 GitHub 前）：已合併至 reviewer 的 deep 模式，本檔保留為相容入口
mode: primary
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: edit
    resource: "*"
    effect: deny
---

已合併至 `reviewer` 的 deep 模式。
本檔只是相容 stub，不再維護獨立 checklist；被叫用時照 `reviewer.md` 的〈deep 模式〉執行（日常紀律、禁止事項與輸出格式也以 `reviewer` 為準）。若無法載入 `reviewer`，停止並回報，不要自行重建 checklist。
