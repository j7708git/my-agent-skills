#!/usr/bin/env bash
# repo-privacy-audit CLI 入口。
#
# 這支腳本只負責：找到 Python 3、把參數原樣交給掃描核心。
# 所有邏輯都在 audit_core.py（只用 Python 3 標準函式庫）。
#
# 退出碼：0=完整且無 high；1=有 high；2=未完整掃描或操作錯誤。
set -uo pipefail

# 解析自身路徑（含 symlink），讓 skill 被 copy 或 symlink 到別處仍可運作。
rpa_self_dir() {
  local src=${BASH_SOURCE[0]} dir
  while [ -L "$src" ]; do
    dir=$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd) || return 1
    src=$(readlink "$src")
    case $src in
      /*) ;;
      *) src=$dir/$src ;;
    esac
  done
  cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd
}

SELF_DIR=$(rpa_self_dir) || {
  echo "[repo-privacy-audit] 無法解析腳本自身位置" >&2
  exit 2
}
CORE="$SELF_DIR/audit_core.py"

if [ ! -f "$CORE" ]; then
  echo "[repo-privacy-audit] 找不到掃描核心：$CORE（skill 安裝不完整）" >&2
  exit 2
fi

# Python 直譯器：環境變數優先，其次 python3，最後 python。
PYTHON=${REPO_PRIVACY_AUDIT_PYTHON:-}
if [ -z "$PYTHON" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON=$(command -v "$candidate")
      break
    fi
  done
fi

if [ -z "$PYTHON" ]; then
  # 沒有 Python 就是無法完成掃描；老實回報，不假通過。
  cat >&2 <<'EOF'
[repo-privacy-audit] 找不到 Python 3 執行檔。
  安裝 Python 3.8 以上，或設定 REPO_PRIVACY_AUDIT_PYTHON 指向可執行檔後重試。
  掃描未完成，退出碼 2。
EOF
  exit 2
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
  echo "[repo-privacy-audit] 需要 Python 3.8 以上；掃描未完成。" >&2
  exit 2
fi

exec "$PYTHON" "$CORE" "$@"
