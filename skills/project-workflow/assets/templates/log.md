# 每月日誌（log）

> 使用方式：把整份檔案複製到 private notes repo 的 `projects/<project-id>/log/YYYY-MM.md`。
> 每則 ≤10 行；只寫歷史，不重複 `status.md` 的現況。

## 骨架

```markdown
# 日誌：<專案名稱>（<YYYY-MM>）

## <YYYY-MM-DD>

- 做了什麼：<一到兩句白話>
- 版本／commit：<版本或真實 commit SHA；沒有就寫 N/A>
- 驗收證據：<acceptance 檔名或實際指令與結果>
- 同步：<已 push／僅本機／不適用>
- 下一步：<具體動作或 none>
```

## 欄位填寫規則

| 欄位 | 填寫規則 |
| --- | --- |
| 檔名 | `YYYY-MM.md`，一個月一檔；跨月就開新檔 |
| 日期標題 | `## YYYY-MM-DD`；同一天多則就合併成一則 |
| 做了什麼 | 白話描述，不要只寫任務代號 |
| 版本／commit | 只寫**真實已存在**的版本或 commit；未 push 要另外註明 |
| 驗收證據 | 指向 `acceptance/` 的檔名，或寫實際指令與 exit code |
| 同步 | 兩個 repo 分開判斷；只寫「已 push」要有遠端依據 |
| 下一步 | 寫成可執行的一步；沒有就寫 `none` |

## 注意

- 不要用日誌取代 `status.md`：現況只看 `status.md`，日誌是歷史。
- 不要把整份測試輸出貼進來；留摘要與證據位置。
- 舊 `progress.md` 不再追加；新日誌一律寫在 `log/`。
