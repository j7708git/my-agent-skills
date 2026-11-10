# 專案現況（status）

> 使用方式：把整份檔案複製到 private notes repo 的 `projects/<project-id>/status.md`。
> 所有 `<...>` 都要換成實際值；`status.md` 只寫「現在」，歷史寫 `log/`。
> 不要在這裡貼整份驗收證據或舊進度。

## 骨架

```markdown
# <專案名稱>

schema: 1
state: <planning／active／paused／blocked／done>
needs: <none／user／external>
now: <現在做到哪，白話一行>
next: <下一個具體動作>
blocker: <卡點，或 none>
updated: <YYYY-MM-DDTHH:MM+08:00>
code_dir: <相對工作區根的路徑，或 no-code>
machine: <unknown 或 A／B／C>
branch: <branch 名稱；不知道就省略>

## 需要你決定

- [ ] <需要使用者決定的事>
```

## 欄位填寫規則

| 欄位 | 填寫規則 |
| --- | --- |
| `schema` | 固定整數 `1`；不相符的檔案不要硬讀 |
| `state` | 只能選 `planning`／`active`／`paused`／`blocked`／`done` |
| `needs` | `none`／`user`／`external`；需要拍板就寫 `user` |
| `now` | 一行白話，說明「現在做到哪」；不要只寫代號或版本號 |
| `next` | 下一個具體、可執行的動作；不要寫「持續觀察」 |
| `blocker` | 有卡點就寫清楚；沒有寫 `none`，不要留空 |
| `updated` | ISO 8601 且含時區，例如 `2026-11-10T09:00+08:00` |
| `code_dir` | 相對**工作區根**的路徑；研究型專案寫 `no-code` |
| `machine` | 未指派寫 `unknown`；只有確定時才寫 `A`／`B`／`C` |
| `branch` | 只是提示，不是權威；不知道就省略，不要寫 `unknown` |
| `local_claim_*` | 三個要一起出現；本機提示，不是跨機鎖；超過 24 小時視為過期 |
| `## 需要你決定` | 選填；沒有待決事項就整節省略 |

## 注意

- 分支、HEAD、是否已 push 以 **Git 即時查詢**為準，`branch` 只是提示。
- `status.md` 與舊 `progress.md` 衝突時，**停下來對帳**，不要自行挑一份。
- 不要寫入憑證、真名、私人 email、帳號 ID 或個人絕對路徑。
