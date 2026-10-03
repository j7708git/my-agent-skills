#!/usr/bin/env bash
# my-agent-skills 統一檢查腳本。
#
# 依序執行：格式驗證 → 單元測試 → Bash 語法 → Python 語法 → Gitleaks（歷史＋staged）。
# 不下載、不安裝任何東西，不修改任何全域設定。
#
# 退出碼（聚合）：
#   0  全部通過
#   1  至少一步失敗
#   2  至少一步未完成（例如缺少 Gitleaks、找不到測試），無法宣告通過
#
# 用法：
#   bash scripts/run-checks.sh [--repo PATH] [--gitleaks PATH] [--skip-gitleaks] [--skip-tests]

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GITLEAKS_BIN="${GITLEAKS:-}"
SKIP_GITLEAKS=0
SKIP_TESTS=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)
      # 缺參數時必須直接結束：shift 2 失敗會造成無限迴圈。
      if [ "$#" -lt 2 ]; then
        echo "錯誤：--repo 需要一個路徑參數。" >&2
        exit 2
      fi
      REPO_ROOT="$2"
      shift 2
      ;;
    --gitleaks)
      if [ "$#" -lt 2 ]; then
        echo "錯誤：--gitleaks 需要一個執行檔路徑。" >&2
        exit 2
      fi
      GITLEAKS_BIN="$2"
      shift 2
      ;;
    --skip-gitleaks)
      SKIP_GITLEAKS=1
      shift
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    -h|--help)
      sed -n '2,20p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "未知的參數：$1" >&2
      exit 1
      ;;
  esac
done

STEP_NAMES=()
STEP_CODES=()
STEP_NOTES=()

record() {
  STEP_NAMES+=("$1")
  STEP_CODES+=("$2")
  STEP_NOTES+=("$3")
}

banner() {
  echo ""
  echo "=== $1 ==="
}

cd "$REPO_ROOT" || {
  echo "無法進入 repo：$REPO_ROOT" >&2
  exit 2
}

echo "repo：$REPO_ROOT"

# 1. 格式驗證 ------------------------------------------------------------------
banner "1/6 格式驗證（scripts/validate_skills.py）"
if command -v python3 >/dev/null 2>&1; then
  # 驗證器永遠來自本 checkout，--repo 只決定被檢查的目標。
  python3 "$SCRIPT_DIR/validate_skills.py" --repo "$REPO_ROOT" --check-docs --strict
  record "格式驗證" "$?" ""
else
  echo "找不到 python3，無法執行格式驗證。"
  record "格式驗證" 2 "缺少 python3"
fi

# 2. 單元測試 ------------------------------------------------------------------
banner "2/6 單元測試（unittest）"
if [ "$SKIP_TESTS" -eq 1 ]; then
  echo "已依 --skip-tests 略過；此結果不代表測試通過。"
  record "單元測試" 2 "依參數略過"
elif ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 python3，無法執行單元測試。"
  record "單元測試" 2 "缺少 python3"
else
  TEST_FILES=()
  while IFS= read -r file; do
    TEST_FILES+=("$file")
  done < <(find tests -type f -name 'test_*.py' 2>/dev/null | LC_ALL=C sort)
  if [ "${#TEST_FILES[@]}" -eq 0 ]; then
    echo "找不到測試檔（tests/**/test_*.py）；測試未被執行。"
    record "單元測試" 2 "找不到測試檔"
  else
    echo "測試檔：${TEST_FILES[*]}"
    # 逐檔執行，並把該檔所在目錄加入 PYTHONPATH：
    #   - unittest discover 會跳過非套件目錄（例如含連字號的目錄），所以不用 discover。
    #   - 測試常在同目錄放 helpers.py，逐一執行可讓這些匯入可用。
    unittest_code=0
    for file in "${TEST_FILES[@]}"; do
      dir="$(dirname "$file")"
      echo "--- $file ---"
      if [ -n "${PYTHONPATH:-}" ]; then
        PYTHONPATH="$dir:$PYTHONPATH" python3 -m unittest "$file" || unittest_code=1
      else
        PYTHONPATH="$dir" python3 -m unittest "$file" || unittest_code=1
      fi
    done
    record "單元測試" "$unittest_code" ""
  fi
fi

# 3. Bash 語法 -----------------------------------------------------------------
banner "3/6 Bash 語法檢查（bash -n）"
SH_FILES=()
while IFS= read -r file; do
  SH_FILES+=("$file")
done < <(find . -type f -name '*.sh' -not -path './.git/*' -not -path '*/.venv/*' 2>/dev/null | LC_ALL=C sort)
if [ "${#SH_FILES[@]}" -eq 0 ]; then
  echo "找不到 .sh 檔案。"
  record "Bash 語法" 0 ""
else
  echo "檢查 ${#SH_FILES[@]} 個腳本：${SH_FILES[*]}"
  bash_syntax_code=0
  for file in "${SH_FILES[@]}"; do
    if ! bash -n "$file"; then
      bash_syntax_code=1
    fi
  done
  record "Bash 語法" "$bash_syntax_code" ""
fi

# 4. Python 語法 ---------------------------------------------------------------
banner "4/6 Python 語法檢查（compileall）"
if command -v python3 >/dev/null 2>&1; then
  PY_FILES=()
  while IFS= read -r file; do
    PY_FILES+=("$file")
  done < <(find . -type f -name '*.py' -not -path './.git/*' -not -path '*/.venv/*' -not -path '*/__pycache__/*' 2>/dev/null | LC_ALL=C sort)
  if [ "${#PY_FILES[@]}" -eq 0 ]; then
    echo "找不到 .py 檔案。"
    record "Python 語法" 0 ""
  else
    echo "檢查 ${#PY_FILES[@]} 個檔案。"
    python3 -m py_compile "${PY_FILES[@]}"
    record "Python 語法" "$?" ""
  fi
else
  echo "找不到 python3。"
  record "Python 語法" 2 "缺少 python3"
fi

# 5. Gitleaks -----------------------------------------------------------------
# 兩個掃描各自独立記錄：
#   5a 歷史：git log --all 的所有本機 refs 可達 commit（不含未 commit 的工作樹／index）
#   5b staged：index 中即將進入 commit 的內容
# 未 staged 的工作樹變更不在本腳本範圍內；發布前請另外確認。
# 退出碼映射：0=通過、99=有發現→1、其它非 0→2（未完成）。
# 報告檔不存在或不是合法 JSON 一律視為未完成（fail closed），不當成通過。
banner "5/6 Gitleaks 敏感資訊掃描（本 repo）"

summarize_report() {
  # 只輸出規則、檔案與行號；不輸出 Secret 欄位，也不直接轉發 gitleaks 的原始輸出。
  python3 - "$1" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    print(f"報告無法解析（視為未完成）：{exc}")
    sys.exit(2)
if not isinstance(data, list):
    print("報告格式不是預期的 JSON 陣列（視為未完成）")
    sys.exit(2)
if not data:
    print("報告內容：0 筆發現")
    sys.exit(0)
print(f"報告內容：{len(data)} 筆發現（Secret 欄位已遮罩，不在此列出）")
for item in data[:20]:
    print(f"  - {item.get('RuleID')} {item.get('File')}:{item.get('StartLine')} commit={str(item.get('Commit'))[:8]}")
if len(data) > 20:
    print(f"  …另有 {len(data) - 20} 筆，詳見報告")
sys.exit(0)
PY
}

run_gitleaks() {
  # $1 = 步驟名稱，$2 = 報告路徑，其餘為 gitleaks 額外參數
  step_name="$1"
  report_path="$2"
  shift 2
  log_path="$REPORT_DIR/$(echo "$step_name" | tr ' /' '__').log"
  # --redact 確保報告與輸出都被遮罩；原始輸出只寫入暫存 log，不直接顯示。
  "$GITLEAKS_BIN" git . \
    --redact \
    --no-banner \
    --exit-code 99 \
    --report-format json \
    --report-path "$report_path" \
    "$@" >"$log_path" 2>&1
  scan_code=$?

  if [ ! -s "$report_path" ]; then
    echo "$step_name：沒有產生報告檔（原始輸出：$log_path）" >&2
    record "$step_name" 2 "缺少報告檔"
    return
  fi
  if ! summarize_report "$report_path"; then
    record "$step_name" 2 "報告不合法"
    return
  fi

  case "$scan_code" in
    0) record "$step_name" 0 "" ;;
    99)
      echo "$step_name：發現疑似秘密。請依 SECURITY.md 手動處理（先撤銷／輪替），不要使用自動修復。" >&2
      record "$step_name" 1 "有發現"
      ;;
    *)
      echo "$step_name：gitleaks 以非預期碼結束（$scan_code），原始輸出：$log_path" >&2
      record "$step_name" 2 "掃描未完成（gitleaks=$scan_code）"
      ;;
  esac
}

if [ "$SKIP_GITLEAKS" -eq 1 ]; then
  echo "已依 --skip-gitleaks 略過；秘密掃描未執行，不能視為通過。"
  record "Gitleaks 歷史" 2 "依參數略過"
  record "Gitleaks staged" 2 "依參數略過"
else
  if [ -z "$GITLEAKS_BIN" ]; then
    if command -v gitleaks >/dev/null 2>&1; then
      GITLEAKS_BIN="$(command -v gitleaks)"
    fi
  fi
  if [ -z "$GITLEAKS_BIN" ] || [ ! -x "$GITLEAKS_BIN" ]; then
    cat >&2 <<'MSG'
找不到可執行的 Gitleaks。

完整秘密掃描需要 Gitleaks；缺少時掃描狀態是 incomplete、退出碼 2，
不能宣稱「沒有秘密」。本腳本不會自動下載或安裝任何工具。
請手動安裝後再跑，或明確使用 --skip-gitleaks（輸出會標註掃描未執行）。
固定版本與校驗和見 docs/development.md。
MSG
    record "Gitleaks 歷史" 2 "缺少 Gitleaks"
    record "Gitleaks staged" 2 "缺少 Gitleaks"
  elif ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "這個目錄不是 Git repo，無法掃描。" >&2
    record "Gitleaks 歷史" 2 "非 Git repo"
    record "Gitleaks staged" 2 "非 Git repo"
  else
    echo "使用 Gitleaks：$GITLEAKS_BIN（$("$GITLEAKS_BIN" version 2>/dev/null | tr -d '\n')）"
    REPORT_DIR="$(mktemp -d)"

    echo "--- 5a 歷史（所有本機 refs 可達的 commit）---"
    if ! git rev-parse --verify --quiet HEAD >/dev/null 2>&1; then
      echo "這個 repo 還沒有任何 commit，沒有歷史可掃描（退出碼 2：掃描不完整）。" >&2
      record "Gitleaks 歷史" 2 "尚無 commit"
    else
      run_gitleaks "Gitleaks 歷史" "$REPORT_DIR/gitleaks-history.json" --log-opts="--all"
    fi

    echo "--- 5b staged（index 中即將進入 commit 的內容）---"
    if git diff --cached --quiet; then
      echo "目前沒有 staged 變更可掃描；歷史掃描不涵蓋未 commit 的內容。" >&2
      record "Gitleaks staged" 0 "無 staged 變更"
    else
      run_gitleaks "Gitleaks staged" "$REPORT_DIR/gitleaks-staged.json" --staged
    fi
  fi
fi

# 彙總 ------------------------------------------------------------------------
banner "檢查結果彙總"
overall=0
index=0
while [ "$index" -lt "${#STEP_NAMES[@]}" ]; do
  name="${STEP_NAMES[$index]}"
  code="${STEP_CODES[$index]}"
  note="${STEP_NOTES[$index]}"
  case "$code" in
    0) status="通過" ;;
    2) status="未完成" ;;
    *) status="失敗" ;;
  esac
  if [ -n "$note" ]; then
    status="$status（$note）"
  fi
  printf '  %-16s %s\n' "$name" "$status"
  if [ "$code" -eq 2 ]; then
    overall=2
  elif [ "$code" -ne 0 ] && [ "$overall" -ne 2 ]; then
    overall=1
  fi
  index=$((index + 1))
done

echo ""
if [ "$overall" -eq 0 ]; then
  echo "全部檢查通過。"
elif [ "$overall" -eq 2 ]; then
  echo "有步驟未完成：以上結果不足以宣告驗收通過（退出碼 2）。"
else
  echo "有步驟失敗（退出碼 1）。"
fi
exit "$overall"
