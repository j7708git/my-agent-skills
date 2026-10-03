#!/usr/bin/env bash
# repo-privacy-audit 的 Git hook 安裝／移除／檢查工具。
#
# 安全邊界（刻意的設計，不是可選項）：
# - 只影響 --repo 指定的那一個 repo。絕不碰其他專案。
# - 絕不修改 ~/.gitconfig 或任何全域 Git 設定；發現 core.hooksPath 有設定就拒絕。
# - 絕不覆蓋既有 hook，也不在別人的 hook 後面「默默串接」。
#   遇到任何既有 hook 或 dangling symlink 一律拒絕，沒有自動接管的選項。
# - 「有本工具標記」不等於「可以覆蓋」：own hook 必須與設定檔記錄的雜湊一致，
#   否則視為你改過的檔案，安裝與移除都拒絕動它。
# - 只在**確認設定檔可解析**的情況下移除 hook。設定檔缺失或損壞時一律保留，
#   不會只憑檔案裡的一行標記就刪除你可能已經手動改過的內容。
# - hooks 目錄或狀態路徑（狀態目錄、設定檔）若是符號連結就拒絕，
#   寫入與讀取都不會跟著連結跑到 repo 之外或別的 repo。
# - 需要 Git 2.31 以上：hooks 目錄一律取自 --path-format=absolute，
#   不做「舊版 fallback」手動拼接（那會算出 .git/.git/hooks 這種錯誤位置）。
# - 不支援 git worktree：worktree 共用同一個 hooks 目錄，無法只影響單一 worktree。
#   不論在主 worktree 或 linked worktree 操作，只要這個 repo 還有其他 worktree 就拒絕。
# - 讀不到 worktree 清單、或計數不合法時**拒絕**而不是當作只有一個 worktree：
#   工具讀不到狀態就放行，等於把安全邊界變成可選的。
# - 寫入前先完成**全部**檢查；內容先在 hooks 目錄內的暫存目錄生成，再以 rename 寫入。
# - 回滾只刪除「本次自己建立」且原本不存在的精確檔案路徑，不碰任何既有檔案，
#   也不使用 rm -rf 作用在未知路徑上。
#
# 退出碼：0=成功；1=用法／前置條件錯誤；2=安全護欄拒絕。
set -uo pipefail

TOOL=repo-privacy-audit
HOOK_MARKER='# repo-privacy-audit-hook:v1'
HOOK_NAMES='pre-commit pre-push'
STATE_DIR_NAME='repo-privacy-audit'
STATE_FILE_NAME='hooks.json'

usage() {
  cat <<EOF
用法：
  bash rpa-hooks.sh install   --repo PATH
  bash rpa-hooks.sh uninstall --repo PATH [--purge]
  bash rpa-hooks.sh status    --repo PATH

選項：
  --repo PATH   目標 repo（預設目前目錄）。只會影響這個 repo。
                路徑可以包含空白；設定以 JSON 儲存，不經過 shell 插入。
  --purge       移除時順便刪掉本工具自己的設定檔（只刪本工具的精確檔案）。
  -h, --help    顯示本說明。

環境變數：
  REPO_PRIVACY_AUDIT_BIN      指定要記錄在設定檔的 CLI 路徑。
  REPO_PRIVACY_AUDIT_POLICY   指定 JSON 政策檔（會記錄在設定檔）。
  REPO_PRIVACY_AUDIT_FORMAT   hook 輸出格式（預設 text；CI 建議 json）。
  REPO_PRIVACY_AUDIT_ARGS     額外傳給掃描器的參數（以空白分隔）。
  REPO_PRIVACY_AUDIT_CONFIG   hook 執行時改讀這個設定檔（測試與除錯用）。

限制：需要 Git 2.31 以上（用 --path-format=absolute 取得 hooks 目錄，
      不手動拼接相對路徑）。core.hooksPath 有設定時拒絕；
      這個 repo 存在其他 worktree 時拒絕；hooks 目錄或狀態路徑是
      符號連結時拒絕；bare repo 不支援。讀不到狀態就拒絕，不猜。
EOF
}

die() {
  echo "[$TOOL] $1" >&2
  exit "${2:-2}"
}

need_python() {
  local candidate
  if [ -n "${REPO_PRIVACY_AUDIT_PYTHON:-}" ]; then
    printf '%s\n' "$REPO_PRIVACY_AUDIT_PYTHON"
    return 0
  fi
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON=$(need_python) || die "找不到 Python 3；無法安全產生 hook 設定檔。" 1

# ---------------------------------------------------------------- 路徑解析 ---

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

# 把可能是相對路徑的檔案路徑轉成絕對路徑（不要求檔案存在）。
abspath() {
  local path=$1 dir base
  case $path in
    /*) printf '%s\n' "$path" ;;
    *)
      dir=$(cd -P "$(dirname "$path")" >/dev/null 2>&1 && pwd) || return 1
      base=$(basename "$path")
      printf '%s/%s\n' "$dir" "$base"
      ;;
  esac
}

SELF_DIR=$(rpa_self_dir) || die "無法解析腳本自身位置。" 1

# CLI 路徑：必須存在、可讀，且第一行是 bash shebang 或副檔名為 .sh。
resolve_cli() {
  local candidate resolved first
  if [ -n "${REPO_PRIVACY_AUDIT_BIN:-}" ]; then
    candidate=$REPO_PRIVACY_AUDIT_BIN
  else
    candidate=$SELF_DIR/repo-privacy-audit.sh
  fi
  resolved=$(abspath "$candidate") || die "無法解析 CLI 路徑：$candidate" 1
  if [ ! -f "$resolved" ]; then
    die "找不到掃描 CLI：$resolved（先安裝完整 skill，或設定 REPO_PRIVACY_AUDIT_BIN）" 1
  fi
  if [ ! -r "$resolved" ]; then
    die "掃描 CLI 不可讀：$resolved" 1
  fi
  first=$(head -n 1 "$resolved" 2>/dev/null || true)
  case $first in
    '#!'*bash* | '#!'*/bash) ;;
    '#!'*sh) ;;
    *)
      case $resolved in
        *.sh) ;;
        *) die "掃描 CLI 既不是 shell 腳本也沒有 shebang：$resolved（不能安全地以 bash 執行）" 1 ;;
      esac
      ;;
  esac
  printf '%s\n' "$resolved"
}

CLI_ABS=$(resolve_cli) || exit $?

POLICY_ABS=''
if [ -n "${REPO_PRIVACY_AUDIT_POLICY:-}" ]; then
  if [ ! -f "$REPO_PRIVACY_AUDIT_POLICY" ]; then
    die "政策檔不存在：$REPO_PRIVACY_AUDIT_POLICY" 1
  fi
  POLICY_ABS=$(abspath "$REPO_PRIVACY_AUDIT_POLICY") || die "無法解析政策檔路徑" 1
fi

# hook 範本目錄
ASSET_DIR=''
for candidate in "$SELF_DIR/../assets" "$SELF_DIR/assets"; do
  resolved=$(cd -P "$candidate" >/dev/null 2>&1 && pwd) || continue
  if [ -f "$resolved/hook-pre-commit" ] && [ -f "$resolved/hook-pre-push" ]; then
    ASSET_DIR=$resolved
    break
  fi
done
[ -n "$ASSET_DIR" ] || die "找不到 hook 範本（assets/hook-pre-commit、assets/hook-pre-push）" 1

# ------------------------------------------------------------ repo 解析 -----

repo_top=''
git_dir=''
git_common_dir=''
hooks_root=''
is_linked_worktree=0

resolve_repo() {
  local path=${1:-.}
  if ! git -C "$path" rev-parse --git-dir >/dev/null 2>&1; then
    die "不是 Git repo 或無法讀取：$path" 1
  fi
  if [ "$(git -C "$path" rev-parse --is-bare-repository 2>/dev/null)" = "true" ]; then
    die "bare repo 不支援安裝 hook：$path" 1
  fi
  repo_top=$(git -C "$path" rev-parse --show-toplevel) || die "無法取得 repo 根目錄：$path" 1
  git_dir=$(git -C "$repo_top" rev-parse --absolute-git-dir) || die "無法取得 git 目錄" 1
  git_common_dir=$(git -C "$repo_top" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || git_common_dir=''
  if [ -z "$git_common_dir" ]; then
    git_common_dir=$git_dir
  fi
  # linked worktree：--git-dir 與 --git-common-dir 不同。
  case $git_dir in
    "$git_common_dir") is_linked_worktree=0 ;;
    *) is_linked_worktree=1 ;;
  esac
}

# hooks 目錄以 Git 自己的 --path-format=absolute 輸出為準。
# 首版要求 Git 2.31+，不手動拼接相對路徑：舊的 fallback 會在 .git 已經是
# 相對路徑時拼出 .git/.git/hooks，屬於「看似相容、實際寫錯位置」。
# 拿不到絕對路徑就拒絕，不猜。
hooks_dir() {
  local raw
  raw=$(git -C "$repo_top" rev-parse --path-format=absolute --git-path hooks 2>/dev/null) || raw=''
  if [ -z "$raw" ]; then
    die "拒絕：無法用 --path-format=absolute 解析 hooks 目錄。
  需要 Git 2.31 以上；本工具不會手動拼接相對路徑，
  因為那會在 .git 是相對路徑時算出錯誤的位置（例如 .git/.git/hooks）。
  請升級 Git，或自行手動安裝 hook。" 2
  fi
  case $raw in
    /*) printf '%s\n' "$raw" ;;
    *)
      die "拒絕：Git 回報的 hooks 路徑不是絕對路徑：$raw
  本工具只接受絕對路徑，避免拼出錯誤位置。" 2
      ;;
  esac
}

refuse_if_hooks_path() {
  local action=$1
  local out origin value
  out=$(git -C "$repo_top" config --show-origin --get-all core.hooksPath 2>/dev/null)
  if [ -n "$out" ]; then
    origin=$(printf '%s\n' "$out" | head -n 1)
    value=$(git -C "$repo_top" config --get-all core.hooksPath 2>/dev/null | head -n 1)
    cat >&2 <<EOF
[$TOOL] 拒絕$action：這個 repo 的 Git 設定了 core.hooksPath。
  設定來源：$origin
  目前值　：$value
  本工具不會修改它，也不會修改 ~/.gitconfig 或任何全域 Git 設定。
  拒絕的原因是：在這種設定下 hook 實際執行位置與本工具假設的不同，
  安裝或移除可能動到這個目錄以外的檔案。
  請先自行決定要沿用該 hooksPath，或由你手動調整設定後再重新執行。
EOF
    exit 2
  fi
}

refuse_if_worktree() {
  local action=$1
  if [ "$is_linked_worktree" -eq 1 ]; then
    cat >&2 <<EOF
[$TOOL] 拒絕$action：這是一個 git linked worktree。
  worktree 共用同一個 hooks 目錄（$git_common_dir/hooks），
  在此安裝或移除會影響同一個 repo 的其他 worktree，無法只作用於目前這一個。
  首版選擇明確拒絕，而不是給一個做不到的隔離保證。
  請改在主 worktree（$git_common_dir 的上層）操作，或自行手動安裝。
EOF
    exit 2
  fi
}

# 即使在主 worktree，只要這個 repo 還有其他 worktree 就拒絕：
# hooks 目錄是共用的，安裝或移除會同時改到那些 worktree 的行為。
# 解析不出 worktree 清單就拒絕（fail closed）。曾經在這裡 fallback 成「視為 1 個」
# 而放行，那等於因為工具讀不到狀態就假裝沒事——與安全邊界相反。
refuse_if_other_worktrees() {
  local action=$1 count raw
  raw=$(git -C "$repo_top" worktree list --porcelain 2>/dev/null) || raw=''
  if [ -z "$raw" ]; then
    die "拒絕$action：無法讀取這個 repo 的 worktree 清單。
  讀不到就無法確認 hooks 目錄是否只影響單一 worktree，
  本工具選擇拒絕，不會因為讀不到狀態就假設一切正常。
  請確認 git 可正常使用這個 repo 後再試一次。" 2
  fi
  count=$(printf '%s\n' "$raw" | grep -c '^worktree ' || true)
  case $count in
    '' | *[!0-9]*)
      die "拒絕$action：無法解析 worktree 清單（讀到的計數是「$count」）。
  無法確認 hooks 目錄的作用範圍，因此拒絕操作。" 2
      ;;
  esac
  if [ "$count" -gt 1 ]; then
    cat >&2 <<EOF
[$TOOL] 拒絕$action：這個 repo 目前有 $count 個 worktree。
  hooks 目錄（$git_common_dir/hooks）是所有 worktree 共用的，
  在其中安裝或移除會同時改到其他 worktree 的行為，
  而那正是「只影響你指定的那個 repo」做不到的隔離保證。
  首版選擇明確拒絕，而不是給一個做不到的承諾。
  請自行手動安裝，或在只有單一 worktree 時再執行本工具。
EOF
    exit 2
  fi
}

# hooks 目錄本身不能是符號連結（否則寫入會落到 repo 之外），
# 且解析後必須仍在 git common dir 底下。
guard_hooks_dir() {
  local action=$1 hooks=$2 common_real hooks_real
  if [ -L "$hooks" ]; then
    die "拒絕$action：hooks 目錄是符號連結，寫入會落到 repo 之外：
  $hooks → $(readlink "$hooks")
  本工具不會跟著符號連結寫入，也不會修改連結目標。" 2
  fi
  [ -d "$hooks" ] || return 0
  common_real=$(cd -P "$git_common_dir" >/dev/null 2>&1 && pwd) || die "無法解析 git common dir：$git_common_dir" 1
  hooks_real=$(cd -P "$hooks" >/dev/null 2>&1 && pwd) || die "無法解析 hooks 目錄：$hooks" 1
  case $hooks_real in
    "$common_real"/*) ;;
    *) die "拒絕$action：hooks 目錄解析後不在這個 repo 的 git 目錄底下：
  $hooks → $hooks_real
  本工具只會寫入 $common_real 底下的檔案。" 2 ;;
  esac
}

state_dir() {
  printf '%s/%s\n' "$git_common_dir" "$STATE_DIR_NAME"
}

state_file() {
  printf '%s/%s/%s\n' "$git_common_dir" "$STATE_DIR_NAME" "$STATE_FILE_NAME"
}

# 目標是否已被占用。dangling symlink（-e 為 false 但 -L 為 true）也算已存在。
path_taken() {
  [ -e "$1" ] || [ -L "$1" ]
}

is_ours() {
  [ -f "$1" ] && [ ! -L "$1" ] && grep -q -F "$HOOK_MARKER" "$1" 2>/dev/null
}

# 內容雜湊：用來判斷 owned hook 是否被使用者改過（改過就不自動刪）。
hook_digest() {
  "$PYTHON" -c '
import hashlib, sys
with open(sys.argv[1], "rb") as handle:
    sys.stdout.write(hashlib.sha256(handle.read()).hexdigest())
' "$1" 2>/dev/null
}

# 讀設定檔裡記錄的某個 key（用 JSON 解析器，不 eval）。
state_value() {
  "$PYTHON" -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    raise SystemExit(1)
if not isinstance(data, dict):
    raise SystemExit(1)
value = data.get(sys.argv[2])
if value is None:
    raise SystemExit(1)
if not isinstance(value, str):
    raise SystemExit(1)
sys.stdout.write(value)
' "$1" "$2"
}

# 設定檔是否可解析、且確實是本工具寫的那一份。
# 沒有通過就代表無法證明 hook 內容未被修改，此時不允許覆蓋或刪除任何檔案。
state_read_ok() {
  "$PYTHON" -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    raise SystemExit(1)
if not isinstance(data, dict) or data.get("tool") != "repo-privacy-audit":
    raise SystemExit(1)
for key in ("repo", "hooksDir", "cli", "hooks"):
    if key not in data:
        raise SystemExit(1)
' "$1" 2>/dev/null
}

# 設定檔裡記錄的某個 hook 內容雜湊。
state_digest_for() {
  local sf=$1 name=$2 key
  case $name in
    pre-commit) key=preCommitSha256 ;;
    pre-push) key=prePushSha256 ;;
    *) return 1 ;;
  esac
  state_value "$sf" "$key" 2>/dev/null || true
}

# ---------------------------------------------------------------- install ---

cmd_install() {
  # chmod 可被 PATH 上的其他執行檔蓋掉（測試需要模擬失敗），
  # 所以明確用 command 呼叫，避免遞迴到代理腳本。
  set_chmod_mode() {
    command chmod "$@"
  }

  local repo_arg='' name target asset sf sd expected actual digest_commit digest_push
  local hooks='' state_ok=0 stage='' install_made_hooks_dir=0
  # 用陣列而不是空白分隔的字串：repo 路徑含空白時，
  # `for f in $install_created` 會把一個路徑切成好幾段，刪錯檔案。
  local -a install_created=()
  local -a install_pre_existing=()

  # 只刪除「本次自己建立、且安裝前確認不存在」的精確路徑。
  # 不 rm -rf 任何未知目錄，也不刪任何安裝前就存在的檔案。
  install_abort() {
    local f
    for f in "${install_created[@]}"; do
      if [ -f "$f" ] || [ -L "$f" ]; then
        rm -f "$f" 2>/dev/null || true
      fi
    done
    if [ -n "$stage" ] && [ -d "$stage" ]; then
      rm -f "$stage/pre-commit" "$stage/pre-push" "$stage/hooks.json" 2>/dev/null || true
      rmdir "$stage" 2>/dev/null || true
    fi
    if [ "$install_made_hooks_dir" -eq 1 ] && [ -d "$hooks" ]; then
      rmdir "$hooks" 2>/dev/null || true
    fi
    install_created=()
    stage=''
    install_made_hooks_dir=0
  }
  install_die() {
    install_abort
    die "$1" "${2:-2}"
  }

  while [ $# -gt 0 ]; do
    case $1 in
      --repo)
        [ $# -ge 2 ] || die "--repo 需要參數值" 1
        repo_arg=$2
        shift 2
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      --backup-existing)
        die "不支援 --backup-existing：先版刻意不提供自動接管既有 hook 的選項。
  既有 hook 必須由你手動合併本工具的檢查；本工具不會覆蓋或停用別人的檢查。" 2
        ;;
      *)
        die "未知選項：$1（見 --help）" 1
        ;;
    esac
  done

  resolve_repo "${repo_arg:-.}"
  refuse_if_hooks_path '安裝'
  refuse_if_worktree '安裝'
  refuse_if_other_worktrees '安裝'

  hooks=$(hooks_dir)
  guard_hooks_dir '安裝' "$hooks"

  sd=$(state_dir)
  sf=$(state_file)
  if [ -L "$sd" ]; then
    die "拒絕安裝：狀態目錄是符號連結：$sd → $(readlink "$sd")
  設定會被寫到別處。本工具不會沿著符號連結寫入，請先人工處理這個連結。" 2
  fi
  if [ -e "$sd" ] && [ ! -d "$sd" ]; then
    die "狀態路徑已被非目錄項目占用，拒絕覆寫：$sd" 2
  fi
  # 設定檔是符號連結時，mv 會寫進**連結指向**的位置，而不是這個 repo 的
  # 狀態目錄。會不會寫到 repo 之外取決於別人怎麼設的連結，所以直接拒絕。
  if [ -L "$sf" ]; then
    die "拒絕安裝：設定檔路徑是符號連結：$sf → $(readlink "$sf")
  寫入會跟著連結跑到別處。本工具不會沿著符號連結寫入，請先人工處理這個連結。" 2
  fi
  # 設定檔位置若是目錄或其他非一般檔案，mv 會把它搬進去而不是覆寫，
  # 造成「看起來成功、實際多了一層」；直接拒絕。
  if path_taken "$sf" && [ ! -f "$sf" ]; then
    die "拒絕安裝：設定檔路徑已被非一般檔案占用：$sf
  本工具不會把設定檔搬進那個項目裡，也不會覆蓋它。
  請先人工處理這個路徑。" 2
  fi
  local state_was_present=0
  if [ -f "$sf" ]; then
    state_was_present=1
    if state_read_ok "$sf"; then
      state_ok=1
    else
      die "拒絕安裝：設定檔存在但無法解析（或不是本工具寫的）：$sf
  在無法確認既有 hook 是否被修改過的情況下，本工具不覆蓋任何檔案。
  請先人工確認 $sd 內的內容，再手動移除這個檔案。" 2
    fi
  fi

  # --- 第一階段：完整檢查。全部通過才寫任何檔案 ---
  # 重點：檔案裡有本工具標記並不足以被覆蓋，必須與設定檔記錄的雜湊一致。
  for name in $HOOK_NAMES; do
    target=$hooks/$name
    if ! path_taken "$target"; then
      continue
    fi
    if ! is_ours "$target"; then
      cat >&2 <<EOF
[$TOOL] 拒絕安裝：$target 已經存在，且不是本工具建立的 hook。
  本工具不覆蓋既有 hook，也不在別人的 hook 後面默默串接——
  那會讓原本的檢查靜默失效。
  請手動把本工具的掃描合併進該 hook 後再重新執行。
EOF
      exit 2
    fi
    if [ "$state_ok" -ne 1 ]; then
      die "拒絕安裝：$target 是本工具的 hook，但設定檔不存在（$sf）。
  沒有設定檔就無法確認它的內容有沒有被你改過；
  本工具不會因為檔案裡有一行標記就把它當成自己的原版覆蓋掉。
  請先人工確認該檔案內容，再手動刪除它或刪除設定檔後重新執行。" 2
    fi
    expected=$(state_digest_for "$sf" "$name")
    actual=$(hook_digest "$target")
    if [ -z "$expected" ] || [ -z "$actual" ] || [ "$expected" != "$actual" ]; then
      die "拒絕安裝：$target 的內容已被修改過（與設定檔記錄的 sha256 不符）。
  這代表它可能已經是你自己改過的檢查。本工具不會覆蓋它。
  請先自行確認要保留哪一份，再手動處理這個檔案。" 2
    fi
    echo "[$TOOL] 檢查：$target 已由本工具安裝且內容未變，將重新寫入並更新設定。"
    install_pre_existing+=("$target")
  done

  # --- 第二階段：先在 hooks 目錄內生成完整內容，再以 rename 原子寫入 ---
  if [ ! -d "$hooks" ]; then
    mkdir -p "$hooks" || die "無法建立 hooks 目錄：$hooks" 2
    install_made_hooks_dir=1
  fi
  stage=$(mktemp -d "$hooks/.rpa-staging.XXXXXX" 2>/dev/null) ||
    install_die "無法在 hooks 目錄內建立暫存目錄：$hooks" 2

  for name in $HOOK_NAMES; do
    asset=$ASSET_DIR/hook-$name
    # 直接複製範本：hook 內不含路徑，沒有任何需要插入的設定。
    cp "$asset" "$stage/$name" || install_die "無法複製 hook 範本：$asset" 2
    chmod 0755 "$stage/$name" || install_die "無法設定 hook 權限：$stage/$name" 2
  done

  # 設定檔先用 Python json.dump 產生合法 JSON，路徑完全不經過 shell 插入。
  # 雜湊取自暫存檔，寫入後與實際安裝到 hooks 裡的內容一致。
  digest_commit=$(hook_digest "$stage/pre-commit") || digest_commit=''
  digest_push=$(hook_digest "$stage/pre-push") || digest_push=''
  if ! "$PYTHON" -c '
import json, sys
payload = {
    "schemaVersion": "1.0",
    "tool": sys.argv[1],
    "repo": sys.argv[2],
    "hooksDir": sys.argv[3],
    "cli": sys.argv[4],
    "policy": sys.argv[5] or None,
    "hooks": ["pre-commit", "pre-push"],
    "preCommitSha256": sys.argv[6] or None,
    "prePushSha256": sys.argv[7] or None,
}
with open(sys.argv[8], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
' "$TOOL" "$repo_top" "$hooks" "$CLI_ABS" "$POLICY_ABS" "$digest_commit" "$digest_push" "$stage/hooks.json"; then
    install_die "無法產生設定檔內容：$stage/hooks.json" 2
  fi

  if [ ! -d "$sd" ]; then
    mkdir -p "$sd" || install_die "無法建立狀態目錄：$sd" 2
  fi

  # 先放 hook，最後才寫設定檔。中途失敗時回滾只刪掉剛建立的 hook，
  # 舊的設定檔完全沒被動過——重裝時它記錄的雜湊與同一份範本相符，仍然有效。
  for name in $HOOK_NAMES; do
    target=$hooks/$name
    if ! mv "$stage/$name" "$target"; then
      install_die "無法安裝 hook：$target" 2
    fi
    # 只有安裝前不存在的位置才記進回滾清單。重裝既有（已驗證未改動）的
    # hook 不列入：失敗時保留原檔，而不是把它刪掉。
    # 用陣列比對，含空白的路徑也不會被切開。
    local already=0 existing
    for existing in "${install_pre_existing[@]}"; do
      if [ "$existing" = "$target" ]; then
        already=1
        break
      fi
    done
    if [ "$already" -eq 0 ]; then
      install_created+=("$target")
    fi
  done

  # 最後才寫設定檔。mv 在同一個檔案系統內是原子的 rename：
  # 要嘛整個設定檔到位（成功），要嘛什麼都沒發生（失敗），不會有半個檔案。
  # 這裡不再把它列入回滾清單——它不是「本次建立的新檔案」，
  # 而是把預先產生的內容搬到最終位置。
  if ! mv "$stage/hooks.json" "$sf"; then
    install_die "無法寫入設定檔：$sf" 2
  fi
  # 首次安裝時這個設定檔是本次建立的，權限設定失敗要一併回滾。
  # 重裝時它本來就存在（mv 是原子的，內容已是新的），不列入清單。
  if [ "$state_was_present" -eq 0 ]; then
    install_created+=("$sf")
  fi
  # 權限設定失敗不當成成功：設定檔裡有 CLI 路徑與政策路徑，
  # 權限過寬是實際的資訊暴露，必須回滾並回報。
  if ! set_chmod_mode 0600 "$sf"; then
    install_die "無法設定設定檔權限（0600）：$sf" 2
  fi

  rm -f "$stage/pre-commit" "$stage/pre-push" 2>/dev/null || true
  rmdir "$stage" 2>/dev/null || true
  stage=''
  install_created=()
  install_made_hooks_dir=0

  cat <<EOF
[$TOOL] 已安裝（僅此 repo）：
  repo     ：$repo_top
  hooks    ：$hooks
  hook 清單：${HOOK_NAMES// /, }
  CLI      ：$CLI_ABS
  政策     ：${POLICY_ABS:-（內建預設）}
  設定檔   ：$(state_file)
  全域 Git 設定：未變更
移除方式：bash $SELF_DIR/rpa-hooks.sh uninstall --repo "$repo_top"
EOF
}

# -------------------------------------------------------------- uninstall ---

cmd_uninstall() {
  local repo_arg='' purge=0 name target

  while [ $# -gt 0 ]; do
    case $1 in
      --repo)
        [ $# -ge 2 ] || die "--repo 需要參數值" 1
        repo_arg=$2
        shift 2
        ;;
      --purge)
        purge=1
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        die "未知選項：$1（見 --help）" 1
        ;;
    esac
  done

  resolve_repo "${repo_arg:-.}"
  # hooksPath 若已改變，這裡算出來的目錄就不再是安裝時那個，
  # 對它動手等於刪別處的檔案。直接拒絕。
  refuse_if_hooks_path '移除'
  refuse_if_worktree '移除'
  refuse_if_other_worktrees '移除'

  local hooks sd sf recorded_dir state_ok=0 expected actual
  hooks=$(hooks_dir)
  sd=$(state_dir)
  sf=$(state_file)
  guard_hooks_dir '移除' "$hooks"
  # 狀態路徑若是符號連結，它可能指向**別的** repo。照著它讀雜湊、
  # 再拿雜湊去刪這個 repo 的 hook，就是用別處的判斷依據刪這裡的檔案。
  # 因此設定檔本身是連結時也要拒絕（即使內容看起來是合法 state）。
  if [ -L "$sd" ]; then
    die "拒絕移除：狀態目錄是符號連結：$sd → $(readlink "$sd")
  這可能指向別的 repo，本工具不會沿著它讀取設定再回來刪這個 repo 的 hook。
  請先人工確認該連結，再手動處理。" 2
  fi
  if [ -L "$sf" ]; then
    die "拒絕移除：設定檔是符號連結：$sf → $(readlink "$sf")
  本工具不會把別處的雜湊當成這個 repo 的驗證依據。
  請先人工確認該連結與內容，再手動處理。" 2
  fi

  # 設定檔不存在或不完整，就無法證明 hook 內容未被改動。
  # 這時只「保留並回報」，不因為檔案裡有一行標記就把它刪掉。
  local has_ours=0
  for name in $HOOK_NAMES; do
    if path_taken "$hooks/$name" && is_ours "$hooks/$name"; then
      has_ours=1
      break
    fi
  done

  if [ -f "$sf" ]; then
    if state_read_ok "$sf"; then
      state_ok=1
    else
      die "拒絕移除：設定檔存在但無法解析（或不是本工具寫的）：$sf
  只憑 hook 檔案裡的一行標記就刪除，會誤刪你已經手動改過的內容。
  請先人工確認 $sd 內的內容與 $hooks 下的 hook，再手動刪除。" 2
    fi
  elif [ "$has_ours" -eq 1 ]; then
    die "拒絕移除：$hooks 下有本工具的 hook，但設定檔不存在（$sf）。
  沒有設定檔就沒有雜湊可比對，無法分辨它是原版還是你改過的版本。
  請先人工確認那些 hook 的內容，再自行手動刪除。" 2
  fi

  # 檢查設定檔記錄的 hooks 目錄是否就是現在算出來的。
  if [ "$state_ok" -eq 1 ]; then
    recorded_dir=$(state_value "$sf" hooksDir 2>/dev/null || true)
    if [ -n "$recorded_dir" ] && [ "$recorded_dir" != "$hooks" ]; then
      die "拒絕移除：設定檔記錄的 hooks 目錄（$recorded_dir）
  與目前計算出的 hooks 目錄（$hooks）不同。
  在這種情況下對目前路徑動手會刪到別處的檔案。
  若 hook 是手動搬到新位置的，請先手動確認並刪除。" 2
    fi
  fi

  local removed=0 kept=0
  for name in $HOOK_NAMES; do
    target=$hooks/$name
    if ! path_taken "$target"; then
      echo "[$TOOL] $name：不存在，略過。"
      continue
    fi
    if ! is_ours "$target"; then
      echo "[$TOOL] $name：不是本工具建立的 hook，未變更。"
      kept=$((kept + 1))
      continue
    fi
    # 內容被改過就不自動刪：使用者可能已手動加工。
    # 設定檔缺雜湊（同上已拒絕）或雜湊對不上，兩種情況都要保留。
    if [ "$state_ok" -eq 1 ]; then
      expected=$(state_digest_for "$sf" "$name")
      actual=$(hook_digest "$target")
      if [ -z "$expected" ] || [ -z "$actual" ] || [ "$expected" != "$actual" ]; then
        echo "[$TOOL] $name：內容已被修改過（或無法驗證），未自動刪除。請自行確認後手動處理：$target"
        kept=$((kept + 1))
        continue
      fi
    fi
    rm -f "$target" || die "無法移除 hook：$target" 2
    echo "[$TOOL] 已移除：$target"
    removed=$((removed + 1))
  done

  if [ "$purge" -eq 1 ] && [ -f "$sf" ]; then
    # 只刪本工具自己的精確檔案，再嘗試 rmdir 空目錄。不使用 rm -rf。
    rm -f "$sf" || die "無法移除設定檔：$sf" 2
    echo "[$TOOL] 已移除設定檔：$sf"
    rmdir "$sd" 2>/dev/null || true
  fi

  echo "[$TOOL] 移除完成：刪除 $removed 個本工具的 hook，保留 $kept 個未動。全域 Git 設定未變更。"
}

# ----------------------------------------------------------------- status ---

cmd_status() {
  local repo_arg='' name target hp

  while [ $# -gt 0 ]; do
    case $1 in
      --repo)
        [ $# -ge 2 ] || die "--repo 需要參數值" 1
        repo_arg=$2
        shift 2
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        die "未知選項：$1（見 --help）" 1
        ;;
    esac
  done

  resolve_repo "${repo_arg:-.}"
  local hooks
  hooks=$(hooks_dir)
  echo "repo            ：$repo_top"
  echo "git dir         ：$git_dir"
  echo "common dir      ：$git_common_dir"
  echo "hooks 目錄      ：$hooks"
  if [ "$is_linked_worktree" -eq 1 ]; then
    echo "worktree        ：是（install／uninstall 會拒絕）"
  else
    echo "worktree        ：否"
  fi
  local wt_count wt_raw
  wt_raw=$(git -C "$repo_top" worktree list --porcelain 2>/dev/null) || wt_raw=''
  if [ -z "$wt_raw" ]; then
    echo "worktree 數量   ：讀不到（install／uninstall 會拒絕）"
  else
    wt_count=$(printf '%s\n' "$wt_raw" | grep -c '^worktree ' || true)
    case $wt_count in
      '' | *[!0-9]*) echo "worktree 數量   ：無法解析（install／uninstall 會拒絕）" ;;
      *)
        echo "worktree 數量   ：$wt_count"
        if [ "$wt_count" -gt 1 ]; then
          echo "                 （install／uninstall 會拒絕：hooks 目錄是共用的）"
        fi
        ;;
    esac
  fi
  hp=$(git -C "$repo_top" config --show-origin --get core.hooksPath 2>/dev/null)
  if [ -n "$hp" ]; then
    echo "core.hooksPath  ：有設定（$hp）— 本工具不會修改它，也不會操作 hook"
  else
    echo "core.hooksPath  ：未設定"
  fi
  for name in $HOOK_NAMES; do
    target=$hooks/$name
    if ! path_taken "$target"; then
      echo "hook $name：未安裝"
    elif is_ours "$target"; then
      echo "hook $name：已由 $TOOL 安裝"
    else
      echo "hook $name：存在，但不是 $TOOL 建立（不會更動）"
    fi
  done
  local sf
  sf=$(state_file)
  if [ -f "$sf" ]; then
    echo "設定檔         ：$sf"
    local recorded
    recorded=$(state_value "$sf" cli 2>/dev/null || true)
    if [ -n "$recorded" ]; then
      if [ -f "$recorded" ]; then
        echo "記錄的 CLI     ：$recorded（存在）"
      else
        echo "記錄的 CLI     ：$recorded（不存在——hook 會 fail closed）"
      fi
    fi
  fi
}

# ------------------------------------------------------------------ main ----

command=${1:-}
if [ $# -gt 0 ]; then
  shift
fi
case $command in
  install)
    cmd_install "$@"
    ;;
  uninstall)
    cmd_uninstall "$@"
    ;;
  status)
    cmd_status "$@"
    ;;
  -h | --help | help)
    usage
    exit 0
    ;;
  '')
    usage >&2
    exit 1
    ;;
  *)
    usage >&2
    die "未知指令：$command" 1
    ;;
esac
