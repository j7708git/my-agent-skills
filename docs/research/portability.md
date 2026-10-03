# 首版可移植性依據

查閱日期：2026-10-03。

## Agent Skills 格式

來源：https://agentskills.io/specification

- 一個 skill 是含 `SKILL.md` 的目錄，YAML frontmatter 至少有 name、description。
- name 採 lowercase kebab-case，1–64 字元且符合目錄名稱；description 非空且不超過 1024 字元。
- scripts、references、assets 以 skill 根目錄為相對基準，支援檔案按需讀取。
- 不依賴平台專屬工具／allowed-tools／context 等擴充，保持通用。

## OpenCode V2

來源：https://opencode.ai/v2/docs/skills 、 https://opencode.ai/v2/docs/agents

- 原生 global skill：`~/.config/opencode/skills`；原生 project：`.opencode/skills`。
- 相容 `.agents/skills`、`.claude/skills`；也能透過 config 的 skills sources 指向本 repo 的 skills 目錄。
- Agent 是平台專屬角色、權限與模型設定，與 skill 分開；首版先不發行 agent adapters。
- OpenChamber 使用底層 OpenCode；不需獨立 SKILL.md 格式。

## 現成安裝工具

來源：https://github.com/vercel-labs/skills

- `npx skills add owner/repo --list` 列出技能；`--skill NAME -g -a opencode|claude-code|codex` 安裝。
- 標準來源目錄包含 `skills/`；私有 repo 使用既有 Git／gh／SSH 授權。
- 可選 copy 或 symlink；不需首版自製跨平台安裝器。
- Node.js 只用於此安裝方法，不是掃描核心必須依賴。提供手動複製的替代文件。
- 遙測可用 `DISABLE_TELEMETRY=1` 關閉；實測不得寫入真實使用者的 agent 設定。
- Skill 安装不會自動啟用本 repo 的 Git hooks，hooks 必須對各 repo 明確安裝。

## 不可誇大的相容性

格式和安裝路徑驗證不是 agent runtime 實測。Bash/Python 核心優先支援 Linux 與 macOS；Windows 僅在 WSL 或相容環境測試後才能聲稱支援。缺少 Gitleaks 不是完整掃描。
