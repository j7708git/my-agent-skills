# A2r 人工自檢白名單範例

> 用途：人工檢查跨檔重複句時，可接受共用的樣板句範例；本工具**不做自動比對**。
> 維護責任：只放「多份文件本來就該逐字相同」的句子；新增時寫在下面清單。

- Claude Code 使用 .claude/skills（全域為 ~/.、專案內為 ./）
- scripts/run-checks.sh 只維護這個集合 repo，不是 skill 的執行入口
- status：passed / findings / incomplete / error
