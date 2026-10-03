"""hooks 安裝／移除／實際觸發的合成測試。

原則
----
- 所有 Git repo 都在 /tmp/opencode 底下的暫存目錄建立，不碰任何真實專案。
- hooks 的「實際觸發」測試是真的跑 `git commit` 與 `git push`，並斷言
  退出碼 1／2 確實擋下來，而不是只看 hook 檔案存不存在。
- hook plumbing 測試用一個安全的 mock core（只印出收到的參數並回傳指定退出碼），
  這樣能在不依賴 audit_core.py 的情況下驗證 hook 傳參與守門行為。
  真實 core 的端到端整合由 PM 用暫存安裝的 Gitleaks 另跑。
- fixture 只含合成資料，不含真實秘密、email 或路徑。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from helpers import (
    SCRATCH_ROOT,
    GitRepoFixture,
    HOOKS_SH,
    SKILL_DIR,
    run_hooks,
    write_script,
)

HOOK_MARKER = "# repo-privacy-audit-hook:v1"
HOOK_NAMES = ("pre-commit", "pre-push")

# 安全的 mock core：只印出收到的參數，然後回傳 MOCK_CORE_EXIT。
# 它不掃描任何東西，也不會修改 repo。
MOCK_CORE = """#!/usr/bin/env bash
set -uo pipefail
echo "mock-core-args: $*"
if [ ! -t 0 ]; then
  stdin_data=$(cat)
  if [ -n "$stdin_data" ]; then
    echo "mock-core-stdin: $(printf '%s' "$stdin_data" | tr '\\n' '|')"
  fi
fi
exit "${MOCK_CORE_EXIT:-0}"
"""


def _text(proc) -> str:
    """subprocess.CompletedProcess 或 helpers.CliResult 都能取文字輸出。"""
    def conv(value):
        return value if isinstance(value, str) else value.decode("utf-8", "replace")
    return conv(proc.stdout) + conv(proc.stderr)


def ensure_scratch() -> str:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return str(SCRATCH_ROOT)


def git_env(repo: GitRepoFixture) -> Dict[str, str]:
    return {
        "HOME": str(repo.root / "home"),
        "XDG_CONFIG_HOME": str(repo.root / "home" / ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Synthetic Author",
        "GIT_AUTHOR_EMAIL": "synthetic-user@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Committer",
        "GIT_COMMITTER_EMAIL": "synthetic-committer@example.invalid",
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
    }


def run_git(
    repo: GitRepoFixture,
    args: Sequence[str],
    *,
    check: bool = False,
    stdin: str = "",
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """在 fixture repo 執行 git。``env`` 可帶入 hook 需要的環境變數。"""
    merged = dict(os.environ)
    merged.update(git_env(repo))
    if env:
        merged.update(env)
    proc = subprocess.run(
        ["git", "-C", str(repo.root), *args],
        input=stdin.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=merged,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失敗：{proc.stderr.decode('utf-8', 'replace')}")
    return proc


class HookTestCase(unittest.TestCase):
    """基底：建立暫存 mock core、repo、工具與安裝環境。"""

    def setUp(self) -> None:
        self.tool_dir = Path(tempfile.mkdtemp(prefix="hook-tools-", dir=ensure_scratch()))
        self.addCleanup(shutil.rmtree, self.tool_dir, True)
        self.mock_core = write_script(self.tool_dir / "mock-core.sh", MOCK_CORE)
        self.mock_exit = "0"
        self.repos: List[GitRepoFixture] = []

    def make_repo(self, name: str) -> GitRepoFixture:
        repo = GitRepoFixture(name)
        self.repos.append(repo)
        self.addCleanup(repo.cleanup)
        repo.init()
        (repo.root / "home").mkdir(parents=True, exist_ok=True)
        repo.write("README.md", "synthetic fixture\n", stage=True)
        run_git(repo, ["commit", "-q", "-m", "seed"], check=True)
        return repo

    def install(self, repo: GitRepoFixture, *extra: str, expect_rc: Optional[int] = 0) -> subprocess.CompletedProcess:
        proc = run_hooks(
            ["install", "--repo", str(repo.root), *extra],
            env={"REPO_PRIVACY_AUDIT_BIN": str(self.mock_core), **git_env(repo)},
        )
        if expect_rc is not None:
            self.assertEqual(
                proc.returncode,
                expect_rc,
                f"install 退出碼不符\nstdout={proc.stdout}\n"
                f"stderr={proc.stderr}",
            )
        return proc

    def hooks_dir(self, repo: GitRepoFixture) -> Path:
        """以 Git 自己回報的路徑為準（優先 --path-format=absolute，舊版退回手動拼接）。"""
        proc = run_git(repo, ["rev-parse", "--path-format=absolute", "--git-path", "hooks"])
        raw = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not raw:
            raw = run_git(repo, ["rev-parse", "--git-path", "hooks"]).stdout.decode("utf-8", "replace").strip()
            candidate = Path(raw)
            return candidate if candidate.is_absolute() else repo.git_dir / raw
        return Path(raw)

    def hook_path(self, repo: GitRepoFixture, name: str) -> Path:
        return self.hooks_dir(repo) / name

    def state_file(self, repo: GitRepoFixture) -> Path:
        return Path(
            run_git(repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
            .stdout.decode("utf-8", "replace")
            .strip()
        ) / "repo-privacy-audit" / "hooks.json"

    def read_state(self, repo: GitRepoFixture) -> Dict:
        return json.loads(self.state_file(repo).read_text(encoding="utf-8"))

    def commit_with_env(
        self,
        repo: GitRepoFixture,
        message: str,
        *,
        core_exit: str,
        body: str = "x\n",
        use_bin_override: bool = True,
    ) -> subprocess.CompletedProcess:
        """用指定的 mock core 退出碼實際執行 `git commit`。

        ``use_bin_override=False`` 表示不在環境變數裡指定 CLI，
        讓 hook 只能讀設定檔記錄的路徑——這才是「CLI 真的被移走」的演練。
        """
        repo.write("payload.txt", body)
        run_git(repo, ["add", "-A"], check=True)
        env = git_env(repo)
        if use_bin_override:
            env["REPO_PRIVACY_AUDIT_BIN"] = str(self.mock_core)
        env["MOCK_CORE_EXIT"] = core_exit
        proc = subprocess.run(
            ["git", "-C", str(repo.root), "commit", "-m", message],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **env},
            check=False,
        )
        return proc

    def repo_with_spaces(self, base_name: str = "spacey-repo") -> GitRepoFixture:
        """把 repo 移到含空白的路徑下。repo 根含空白是常見情況，不是特例。"""
        repo = self.make_repo(base_name)
        holder = Path(tempfile.mkdtemp(prefix="spacey-", dir=ensure_scratch()))
        self.addCleanup(shutil.rmtree, holder, True)
        spaced = holder / "a dir with spaces"
        spaced.parent.mkdir(parents=True, exist_ok=True)
        repo.root.rename(spaced)
        # fixture 的 root 改指到新位置，cleanup 與後續 git 呼叫都會跟著走。
        repo.root = spaced
        self.assertIn(" ", str(repo.root), "前置條件：repo 路徑必須含空白")
        return repo

    def linked_worktree(self, repo: GitRepoFixture, branch: str = "") -> Path:
        """建立一個真正的 linked worktree，並只清理自己建立的那個目錄。"""
        path = Path(tempfile.mkdtemp(prefix=f"{repo.name}-wt-", dir=ensure_scratch()))
        self.addCleanup(shutil.rmtree, path, True)
        args = ["worktree", "add", "-q", str(path)]
        if branch:
            args.append(branch)
        run_git(repo, args, check=True)
        return path


class TestInstall(HookTestCase):
    def test_install_creates_both_hooks_and_valid_state(self) -> None:
        repo = self.make_repo("install-ok")
        self.install(repo)

        for name in HOOK_NAMES:
            path = self.hook_path(repo, name)
            self.assertTrue(path.is_file(), f"{name} 未建立")
            self.assertIn(HOOK_MARKER, path.read_text(encoding="utf-8"))
            self.assertTrue(os.access(path, os.X_OK), f"{name} 不可執行")

        state = self.read_state(repo)
        self.assertEqual(state["cli"], str(self.mock_core))
        self.assertEqual(state["repo"], str(repo.root))
        self.assertEqual(state["policy"], None)
        self.assertEqual(sorted(state["hooks"]), sorted(HOOK_NAMES))
        self.assertEqual(state["hooksDir"], str(self.hooks_dir(repo)))

    def test_hooks_dir_is_not_double_prefixed(self) -> None:
        """回歸測試：hooks 路徑不能變成 .git/.git/hooks。"""
        repo = self.make_repo("hooks-dir")
        self.install(repo)
        hooks = self.hooks_dir(repo)
        self.assertEqual(hooks.name, "hooks")
        self.assertEqual(hooks.parent, Path(run_git(repo, ["rev-parse", "--absolute-git-dir"]).stdout.decode().strip()))
        self.assertNotIn(".git/.git", str(hooks))
        for name in HOOK_NAMES:
            self.assertTrue((hooks / name).is_file())

    def test_hooks_dir_absolute_path_format_supported(self) -> None:
        """安裝後的 hooks 目錄必須和 Git 自己算的一致。"""
        repo = self.make_repo("hooks-dir-consistency")
        expected = run_git(repo, ["rev-parse", "--path-format=absolute", "--git-path", "hooks"]).stdout.decode().strip()
        self.install(repo)
        self.assertEqual(str(self.hooks_dir(repo)), expected)

    def test_hooks_do_not_embed_paths(self) -> None:
        """hook 內容不含路徑；設定改由旁邊 JSON 提供。"""
        repo = self.make_repo("no-embed")
        self.install(repo)
        for name in HOOK_NAMES:
            body = self.hook_path(repo, name).read_text(encoding="utf-8")
            self.assertNotIn(str(self.mock_core), body, f"{name} 不應內嵌 CLI 路徑")
            self.assertNotIn(str(repo.root), body, f"{name} 不應內嵌 repo 路徑")
            self.assertNotIn("@@RPA_CONFIG@@", body)
            self.assertNotIn("eval ", body)
            self.assertNotIn("\nsource ", body)

    def test_refuses_existing_hook(self) -> None:
        repo = self.make_repo("existing-hook")
        target = self.hook_path(repo, "pre-commit")
        target.write_text("#!/usr/bin/env bash\necho '原本的檢查'\n", encoding="utf-8")
        target.chmod(0o755)
        original = target.read_text(encoding="utf-8")

        self.install(repo, expect_rc=2)

        # 既有 hook 未被覆蓋、也沒有被改名或停用。
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertFalse(self.hook_path(repo, "pre-push").exists(), "不該留下只裝一半的狀態")
        self.assertFalse(self.state_file(repo).exists())

    def test_backup_existing_option_rejected(self) -> None:
        repo = self.make_repo("backup-option")
        proc = self.install(repo, "--backup-existing", expect_rc=2)
        message = proc.stderr
        self.assertIn("--backup-existing", message)

    def test_refuses_dangling_symlink_at_hook_path(self) -> None:
        """dangling symlink 也算「已存在」，不可被視為空位而覆寫。"""
        repo = self.make_repo("dangling-symlink")
        target = self.hook_path(repo, "pre-push")
        target.symlink_to(repo.root / "does-not-exist")
        self.assertFalse(target.exists(), "前置條件：這是 dangling symlink")
        self.assertTrue(target.is_symlink())

        self.install(repo, expect_rc=2)
        self.assertTrue(target.is_symlink(), "dangling symlink 不該被取代")
        self.assertEqual(os.readlink(target), str(repo.root / "does-not-exist"))

    def test_preflight_checks_all_hooks_before_writing(self) -> None:
        """第二個 hook 被占用時，第一個也不該被寫入。"""
        repo = self.make_repo("preflight")
        target = self.hook_path(repo, "pre-push")
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)

        self.install(repo, expect_rc=2)
        self.assertFalse(self.hook_path(repo, "pre-commit").exists(), "preflight 失敗時不該寫入任何 hook")
        self.assertFalse(self.state_file(repo).exists())

    def test_refuses_local_hooks_path(self) -> None:
        repo = self.make_repo("hooks-path-local")
        run_git(repo, ["config", "core.hooksPath", "custom-hooks"])
        proc = self.install(repo, expect_rc=2)
        message = proc.stderr
        self.assertIn("core.hooksPath", message)
        self.assertFalse(self.hook_path(repo, "pre-commit").exists())
        # 設定未被我們改動
        self.assertEqual(
            run_git(repo, ["config", "--get", "core.hooksPath"]).stdout.decode().strip(), "custom-hooks"
        )

    def test_refuses_global_hooks_path_without_touching_it(self) -> None:
        """全域 hooksPath 也要拒絕，且絕不改 ~/.gitconfig。"""
        repo = self.make_repo("hooks-path-global")
        gitconfig = repo.root / "home" / ".gitconfig"
        gitconfig.parent.mkdir(parents=True, exist_ok=True)
        gitconfig.write_text("[core]\n\thooksPath = /tmp/opencode/somewhere-else\n", encoding="utf-8")
        before = gitconfig.read_text(encoding="utf-8")

        proc = self.install(repo, expect_rc=2)
        self.assertIn("core.hooksPath", proc.stderr)
        self.assertEqual(gitconfig.read_text(encoding="utf-8"), before, "全域 gitconfig 不得被修改")
        self.assertFalse(self.hook_path(repo, "pre-commit").exists())

    def test_refuses_linked_worktree(self) -> None:
        """worktree 共用 hooks 目錄，無法隔離，必須拒絕。"""
        repo = self.make_repo("worktree-refuse")
        linked = self.linked_worktree(repo)
        proc = run_hooks(
            ["install", "--repo", str(linked)],
            env={"REPO_PRIVACY_AUDIT_BIN": str(self.mock_core), **git_env(repo)},
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("worktree", proc.stderr)
        # 主 worktree 的 hooks 目錄不該被動到
        self.assertFalse(self.hook_path(repo, "pre-commit").exists())

    def test_refuses_main_repo_that_has_linked_worktree(self) -> None:
        """主 worktree 底下還有其他 worktree 時也要拒絕：那會改到別人。"""
        repo = self.make_repo("worktree-count")
        self.linked_worktree(repo)
        proc = self.install(repo, expect_rc=2)
        self.assertIn("worktree", proc.stderr)
        for name in HOOK_NAMES:
            self.assertFalse(self.hook_path(repo, name).exists())
        self.assertFalse(self.state_file(repo).exists())

    def test_uninstall_refused_when_main_repo_has_linked_worktree(self) -> None:
        repo = self.make_repo("worktree-count-uninstall")
        self.install(repo)
        self.linked_worktree(repo)
        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 2, _text(proc))
        self.assertIn("worktree", proc.stderr)
        for name in HOOK_NAMES:
            self.assertTrue(self.hook_path(repo, name).exists(), f"{name} 不該被移除")

    def test_rollback_with_spaces_in_path_removes_only_its_own_files(self) -> None:
        """repo 路徑含空白且中途失敗時，回滾不得刪錯檔案、也不得留半套。

        這是空白分隔字串回滾的真實失效情境：路徑被切成
        ``.../a``、``dir``、``with``、``spaces/.git/hooks/pre-commit``，
        結果是該刪的沒刪、不該刪的被刪。改成陣列後逐一比對完整路徑。
        """
        if os.geteuid() == 0:
            self.skipTest("root 不受目錄權限限制，無法用權限模擬失敗")
        repo = self.repo_with_spaces("rollback-spaces")
        # 在空白路徑旁邊放一個「其他切片」，任何路徑切片誤刪都會動到它。
        bystander = repo.root.parent / "dir"
        bystander.write_text("不該被回滾刪掉\n", encoding="utf-8")
        other_hook_dir = repo.root.parent / "pre-commit"
        other_hook_dir.mkdir(exist_ok=True)
        keep = other_hook_dir / "unrelated.txt"
        keep.write_text("使用者自己的檔案\n", encoding="utf-8")

        # 狀態目錄唯讀 → 安裝會寫完 hook 後在寫設定檔時失敗，必須回滾。
        state_dir = repo.git_dir / "repo-privacy-audit"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o500)
        self.addCleanup(state_dir.chmod, 0o700)

        proc = self.install(repo, expect_rc=2)
        self.assertIn("無法", proc.stderr, f"應在寫入階段失敗：{proc.stderr}")

        # 精確斷言：本次建立的 hook 一個都不該留
        for name in HOOK_NAMES:
            self.assertFalse(
                self.hook_path(repo, name).exists(),
                f"{name} 應被回滾移除（空白路徑不得讓回滾失效）",
            )
        self.assertFalse(self.state_file(repo).exists())
        # 空白切片的鄰居檔案完全沒被碰到
        self.assertTrue(bystander.exists(), "不得刪到空白路徑的切片")
        self.assertTrue(keep.exists(), "不得刪到空白路徑切到的其他目錄")
        self.assertEqual(keep.read_text(encoding="utf-8"), "使用者自己的檔案\n")
        self.assertFalse(
            any(p.name.startswith(".rpa-staging.") for p in self.hooks_dir(repo).iterdir()),
            "不得留下暫存目錄",
        )

        # 排除障礙後必須還能安裝，且 pre-commit 完整可用
        state_dir.chmod(0o700)
        self.install(repo)
        for name in HOOK_NAMES:
            self.assertTrue(self.hook_path(repo, name).is_file(), f"{name} 應重新安裝成功")
        commit = self.commit_with_env(repo, "after rollback", core_exit="0")
        self.assertEqual(commit.returncode, 0, _text(commit))
        self.assertIn("mock-core-args", _text(commit))

    def test_refuses_hooks_dir_that_is_symlink(self) -> None:
        """hooks 目錄若是符號連結，寫入會落到 repo 之外，必須拒絕。"""
        repo = self.make_repo("hooks-symlink")
        hooks = self.hooks_dir(repo)
        self.assertTrue(hooks.is_dir(), "前置條件：Git 預設會有 hooks 目錄")

        # 把 hooks 目錄搬到 repo 之外，再讓原路徑變成指向它的符號連結。
        outside = Path(tempfile.mkdtemp(prefix="outside-hooks-", dir=ensure_scratch()))
        self.addCleanup(shutil.rmtree, outside, True)
        target_dir = outside / "hooks"
        hooks.rename(target_dir)
        outside_marker = target_dir / "outside-marker.txt"
        outside_marker.write_text("不該被這個工具碰到\n", encoding="utf-8")
        hooks.symlink_to(target_dir)

        proc = self.install(repo, expect_rc=2)
        # 兩種守門任一觸發都算拒絕：符號連結本身，或解析後落在 repo 之外。
        self.assertTrue(
            "符號連結" in proc.stderr or "不在這個 repo" in proc.stderr,
            f"應明確拒絕在 repo 之外寫入，實際訊息：{proc.stderr}",
        )
        self.assertTrue(outside_marker.exists(), "不得寫進符號連結指向的目錄")
        self.assertFalse((target_dir / "pre-commit").exists(), "不得在 repo 外安裝 hook")
        self.assertFalse(self.state_file(repo).exists())

    def test_refuses_bare_repo(self) -> None:
        bare = GitRepoFixture("bare")
        self.addCleanup(bare.cleanup)
        run_git(bare, ["init", "-q", "--bare", "-b", "main", "."], check=True)
        proc = run_hooks(["install", "--repo", str(bare.root)], env={"REPO_PRIVACY_AUDIT_BIN": str(self.mock_core)})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bare", proc.stderr.lower())

    def test_missing_bin_fails(self) -> None:
        repo = self.make_repo("missing-bin")
        proc = run_hooks(
            ["install", "--repo", str(repo.root)],
            env={"REPO_PRIVACY_AUDIT_BIN": str(self.tool_dir / "nope.sh"), **git_env(repo)},
        )
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.hook_path(repo, "pre-commit").exists())

    def test_refuses_to_overwrite_modified_owned_hook(self) -> None:
        """檔案裡有本工具的標記 ≠ 可以覆蓋：雜湊不符就必須拒絕。

        原本的行為只看 marker，重裝會直接蓋掉使用者手動加的內容。
        """
        repo = self.make_repo("modified-owned")
        self.install(repo)
        target = self.hook_path(repo, "pre-commit")
        target.write_text(target.read_text(encoding="utf-8") + "\n# 使用者自己加的檢查\n", encoding="utf-8")
        modified = target.read_text(encoding="utf-8")

        proc = self.install(repo, expect_rc=2)
        self.assertIn("已被修改過", proc.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), modified, "使用者改過的內容不得被覆蓋")

    def test_refuses_when_own_hook_exists_but_state_is_gone(self) -> None:
        """只有 marker、沒有設定檔可比對雜湊時，拒絕覆蓋。"""
        repo = self.make_repo("owned-no-state")
        self.install(repo)
        target = self.hook_path(repo, "pre-push")
        original = target.read_text(encoding="utf-8")
        self.state_file(repo).unlink()

        proc = self.install(repo, expect_rc=2)
        self.assertIn("設定檔", proc.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_refuses_when_state_file_is_corrupt(self) -> None:
        """設定檔無法解析時，不得猜測既有 hook 的狀態。"""
        repo = self.make_repo("corrupt-state")
        self.install(repo)
        target = self.hook_path(repo, "pre-commit")
        original = target.read_text(encoding="utf-8")
        self.state_file(repo).write_text("{ 這不是合法 JSON", encoding="utf-8")

        proc = self.install(repo, expect_rc=2)
        self.assertIn("無法解析", proc.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_install_failure_leaves_no_partial_hooks(self) -> None:
        """安裝寫到一半失敗時必須回滾：不留半套 hook，也不動使用者自己的檔案。

        觸發方式：狀態目錄先存在但唯讀。安裝會順利寫完兩個 hook，最後在搬設定檔
        時失敗——正好落在需要回滾的位置，而且只有「本次建立的檔案」可以被刪。
        """
        if os.geteuid() == 0:
            self.skipTest("root 不受目錄權限限制，無法用權限模擬失敗")
        repo = self.make_repo("install-rollback")
        state_dir = repo.git_dir / "repo-privacy-audit"
        state_dir.mkdir(parents=True, exist_ok=True)
        bystander = state_dir / "unrelated.txt"
        bystander.write_text("使用者自己的檔案\n", encoding="utf-8")
        state_dir.chmod(0o500)
        self.addCleanup(state_dir.chmod, 0o700)

        proc = self.install(repo, expect_rc=2)
        self.assertIn("無法", proc.stderr, f"應在寫入階段失敗：{proc.stderr}")
        for name in HOOK_NAMES:
            self.assertFalse(self.hook_path(repo, name).exists(), f"{name} 應被回滾移除")
        self.assertFalse(self.state_file(repo).exists())
        self.assertTrue(bystander.exists(), "回滾不得碰到使用者的檔案")
        self.assertFalse(
            any(p.name.startswith(".rpa-staging.") for p in self.hooks_dir(repo).iterdir()),
            "回滾不得留下暫存目錄",
        )

    def test_failed_install_does_not_block_a_later_install(self) -> None:
        """失敗回滾之後必須還能重新安裝，不留阻擋下一次執行的殘骸。"""
        if os.geteuid() == 0:
            self.skipTest("root 不受目錄權限限制，無法用權限模擬失敗")
        repo = self.make_repo("install-retry")
        state_dir = repo.git_dir / "repo-privacy-audit"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o500)
        self.addCleanup(state_dir.chmod, 0o700)

        self.install(repo, expect_rc=2)
        self.assertFalse(self.state_file(repo).exists())

        state_dir.chmod(0o700)  # 障礙排除
        self.install(repo)  # 這次必須成功
        self.assertEqual(sorted(self.read_state(repo)["hooks"]), sorted(HOOK_NAMES))
        for name in HOOK_NAMES:
            self.assertTrue(self.hook_path(repo, name).is_file())
        self.assertFalse(
            any(p.name.startswith(".rpa-staging.") for p in self.hooks_dir(repo).iterdir()),
            "不得留下暫存目錄",
        )
        # 移除也要正常，代表沒有殘留狀態讓驗證機制誤判
        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 0, _text(proc))

    def test_install_succeeds_with_read_only_state_dir(self) -> None:
        """安裝成功時設定檔權限必須是 0600（內容含 CLI 與政策路徑）。"""
        repo = self.make_repo("state-mode")
        self.install(repo)
        mode = self.state_file(repo).stat().st_mode & 0o777
        self.assertEqual(oct(mode), oct(0o600), f"設定檔權限應為 0600，實際 {oct(mode)}")

    def test_install_fails_when_state_chmod_fails(self) -> None:
        """設定檔權限設定失敗不得當成成功，必須回滾並回報。"""
        repo = self.make_repo("chmod-fails")
        bin_dir = self.tool_dir / "bin2"
        bin_dir.mkdir(parents=True, exist_ok=True)
        real_chmod = shutil.which("chmod")
        self.assertIsNotNone(real_chmod, "找不到 chmod")
        # 只讓 0600（設定檔權限）失敗，0755（hook 權限）照常轉呼叫真 chmod。
        # 這樣失敗點就精確落在「設定檔已就位、權限卻設不成」那一刻。
        write_script(
            bin_dir / "chmod",
            "#!/usr/bin/env bash\n"
            'echo "chmod $*" >> "%s"\n'
            'if [ "${1:-}" = "0600" ]; then exit 1; fi\n'
            'exec "%s" "$@"\n' % (self.tool_dir / "chmod-calls.txt", real_chmod),
        )

        proc = run_hooks(
            ["install", "--repo", str(repo.root)],
            env={
                "REPO_PRIVACY_AUDIT_BIN": str(self.mock_core),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                **git_env(repo),
            },
        )
        self.assertEqual(proc.returncode, 2, _text(proc))
        self.assertIn("權限", proc.stderr)
        calls = self.tool_dir / "chmod-calls.txt"
        self.assertTrue(calls.is_file(), "chmod 應該被呼叫過")
        self.assertIn("0600", calls.read_text(encoding="utf-8"), "必須以 0600 呼叫")
        # 回滾：設定檔不得以過寬權限留在原地
        self.assertFalse(self.state_file(repo).exists(), "權限設定失敗時不得留下設定檔")
        for name in HOOK_NAMES:
            self.assertFalse(self.hook_path(repo, name).exists(), f"{name} 應被回滾移除")

    def test_refuses_when_state_file_is_symlink(self) -> None:
        """設定檔是符號連結時必須拒絕安裝：mv 會寫進連結指向的位置。"""
        repo = self.make_repo("state-symlink-install")
        state_dir = repo.git_dir / "repo-privacy-audit"
        state_dir.mkdir(parents=True, exist_ok=True)
        outside = Path(tempfile.mkdtemp(prefix="outside-state-", dir=ensure_scratch()))
        self.addCleanup(shutil.rmtree, outside, True)
        target = outside / "hijacked.json"
        target.write_text("{}\n", encoding="utf-8")
        link = state_dir / "hooks.json"
        link.symlink_to(target)

        proc = self.install(repo, expect_rc=2)
        self.assertIn("符號連結", proc.stderr)
        for name in HOOK_NAMES:
            self.assertFalse(self.hook_path(repo, name).exists(), "拒絕時不該留下 hook")
        self.assertEqual(target.read_text(encoding="utf-8"), "{}\n", "不得寫進連結指向的檔案")

    def test_uninstall_refuses_when_state_file_is_symlink(self) -> None:
        """設定檔是符號連結時也拒絕移除：沿著它讀雜湊會刪到別處的判斷依據。"""
        repo = self.make_repo("state-symlink-uninstall")
        self.install(repo)
        target = self.hook_path(repo, "pre-commit")
        original = target.read_text(encoding="utf-8")
        state = self.state_file(repo)
        elsewhere = Path(tempfile.mkdtemp(prefix="outside-state-", dir=ensure_scratch()))
        self.addCleanup(shutil.rmtree, elsewhere, True)
        payload = elsewhere / "foreign.json"
        # 內容是有效 state，但屬於別的地方：雜湊故意不吻合。
        payload.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "tool": "repo-privacy-audit",
                    "repo": "/elsewhere",
                    "hooksDir": str(self.hooks_dir(repo)),
                    "cli": "/elsewhere/cli.sh",
                    "policy": None,
                    "hooks": list(HOOK_NAMES),
                    "preCommitSha256": "0" * 64,
                    "prePushSha256": "0" * 64,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state.unlink()
        state.symlink_to(payload)

        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 2, _text(proc))
        self.assertIn("符號連結", proc.stderr)
        self.assertTrue(target.exists(), "設定是符號連結時必須保留 hook")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_uninstall_refuses_when_state_dir_is_symlink(self) -> None:
        """狀態目錄是符號連結時拒絕移除，避免讀到別的 repo 的狀態。"""
        repo = self.make_repo("state-dir-symlink-uninstall")
        self.install(repo)
        target = self.hook_path(repo, "pre-push")
        original = target.read_text(encoding="utf-8")
        state_dir = repo.git_dir / "repo-privacy-audit"

        elsewhere = Path(tempfile.mkdtemp(prefix="outside-state-", dir=ensure_scratch()))
        self.addCleanup(shutil.rmtree, elsewhere, True)
        foreign = elsewhere / "hooks.json"
        foreign.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "tool": "repo-privacy-audit",
                    "repo": "/elsewhere",
                    "hooksDir": str(self.hooks_dir(repo)),
                    "cli": "/elsewhere/cli.sh",
                    "policy": None,
                    "hooks": list(HOOK_NAMES),
                    "preCommitSha256": "0" * 64,
                    "prePushSha256": "0" * 64,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state_dir.rename(elsewhere / "saved")
        state_dir.symlink_to(elsewhere)

        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 2, _text(proc))
        self.assertIn("符號連結", proc.stderr)
        self.assertTrue(target.exists(), "狀態目錄是符號連結時必須保留 hook")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_install_refuses_when_state_dir_is_symlink(self) -> None:
        repo = self.make_repo("state-dir-symlink-install")
        elsewhere = Path(tempfile.mkdtemp(prefix="outside-state-", dir=ensure_scratch()))
        self.addCleanup(shutil.rmtree, elsewhere, True)
        (repo.git_dir / "repo-privacy-audit").symlink_to(elsewhere)

        proc = self.install(repo, expect_rc=2)
        self.assertIn("符號連結", proc.stderr)
        for name in HOOK_NAMES:
            self.assertFalse(self.hook_path(repo, name).exists())
        self.assertEqual(list(elsewhere.iterdir()), [], "不得寫進連結指向的目錄")

    def _git_wrapper_that_breaks(self, subcommand: str) -> Path:
        """做一個 git 的代理腳本，讓指定子指令回傳非 0，其餘照常轉呼叫真 git。

        子指令比對會略過選項，所以 `git -C DIR worktree list` 也算數到。
        """
        real = shutil.which("git")
        self.assertIsNotNone(real, "找不到 git")
        # 必須真的叫 git，並且放在 PATH 最前面，才會被優先取用。
        wrapper_dir = self.tool_dir / "bin"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        wrapper = wrapper_dir / "git"
        write_script(
            wrapper,
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do\n'
            '  if [ "$a" = "%s" ]; then\n'
            '    echo "synthetic failure: git %s is unavailable" >&2\n'
            "    exit 128\n"
            "  fi\n"
            "done\n"
            'exec "%s" "$@"\n' % (subcommand, subcommand, real),
        )
        return wrapper

    def test_refuses_when_worktree_list_fails(self) -> None:
        """讀不到 worktree 清單就必須拒絕，不能因為讀不到就假設只有一個。"""
        repo = self.make_repo("wt-list-fails")
        wrapper = self._git_wrapper_that_breaks("worktree")

        proc = run_hooks(
            ["install", "--repo", str(repo.root)],
            env={
                "REPO_PRIVACY_AUDIT_BIN": str(self.mock_core),
                "PATH": f"{wrapper.parent}{os.pathsep}{os.environ.get('PATH', '')}",
                **git_env(repo),
            },
        )
        self.assertEqual(proc.returncode, 2, _text(proc))
        self.assertIn("worktree", proc.stderr)
        self.assertIn("拒絕", proc.stderr)
        for name in HOOK_NAMES:
            self.assertFalse(self.hook_path(repo, name).exists(), "拒絕時不該留下 hook")
        self.assertFalse(self.state_file(repo).exists())
        self.assertTrue(wrapper.is_file())

    def test_uninstall_refuses_when_worktree_list_fails(self) -> None:
        repo = self.make_repo("wt-list-fails-uninstall")
        self.install(repo)
        wrapper = self._git_wrapper_that_breaks("worktree")

        proc = run_hooks(
            ["uninstall", "--repo", str(repo.root)],
            env={
                "PATH": f"{wrapper.parent}{os.pathsep}{os.environ.get('PATH', '')}",
                **git_env(repo),
            },
        )
        self.assertEqual(proc.returncode, 2, _text(proc))
        self.assertIn("worktree", proc.stderr)
        for name in HOOK_NAMES:
            self.assertTrue(self.hook_path(repo, name).exists(), f"{name} 不該被移除")
        self.assertTrue(wrapper.is_file())

    def test_refuses_when_state_file_path_is_a_directory(self) -> None:
        """設定檔路徑被目錄占住時必須拒絕，而不是把檔案搬進去。"""
        repo = self.make_repo("state-is-dir")
        state_dir = repo.git_dir / "repo-privacy-audit"
        (state_dir / "hooks.json").mkdir(parents=True)

        proc = self.install(repo, expect_rc=2)
        self.assertIn("非一般檔案", proc.stderr)
        for name in HOOK_NAMES:
            self.assertFalse(self.hook_path(repo, name).exists())
        self.assertEqual(list((state_dir / "hooks.json").iterdir()), [], "不得把檔案搬進那個目錄")

    def test_reinstall_is_idempotent_and_updates_settings(self) -> None:
        """未改動的重裝應該成功，且設定可以被更新。"""
        repo = self.make_repo("reinstall-settings")
        self.install(repo)
        first = self.hook_path(repo, "pre-commit").read_text(encoding="utf-8")

        other_core = write_script(self.tool_dir / "other-core.sh", MOCK_CORE)
        proc = run_hooks(
            ["install", "--repo", str(repo.root)],
            env={"REPO_PRIVACY_AUDIT_BIN": str(other_core), **git_env(repo)},
        )
        self.assertEqual(proc.returncode, 0, _text(proc))
        self.assertEqual(
            self.hook_path(repo, "pre-commit").read_text(encoding="utf-8"),
            first,
            "同一份範本重裝後內容不該改變",
        )
        self.assertEqual(self.read_state(repo)["cli"], str(other_core))

    def test_cli_path_with_spaces_works(self) -> None:
        """CLI 路徑含空白時必須能用（JSON 設計本來就安全，不該多餘拒絕）。"""
        repo = self.make_repo("cli-spaces")
        spaced = self.tool_dir / "with spaces" / "mock core.sh"
        write_script(spaced, MOCK_CORE)

        proc = run_hooks(
            ["install", "--repo", str(repo.root)],
            env={"REPO_PRIVACY_AUDIT_BIN": str(spaced), **git_env(repo)},
        )
        self.assertEqual(proc.returncode, 0, _text(proc))
        self.assertEqual(self.read_state(repo)["cli"], str(spaced))

        # 端到端：hook 真的能執行那個含空白的路徑
        commit = self.commit_with_env(repo, "spaced cli", core_exit="0")
        self.assertEqual(commit.returncode, 0, _text(commit))
        self.assertIn("mock-core-args", _text(commit))

    def test_policy_path_with_spaces_is_safe(self) -> None:
        repo = self.make_repo("policy-spaces")
        policy_dir = self.tool_dir / "policy with spaces"
        policy_dir.mkdir(parents=True, exist_ok=True)
        policy = policy_dir / "policy file.json"
        policy.write_text('{"schemaVersion":"1.0"}', encoding="utf-8")

        proc = run_hooks(
            ["install", "--repo", str(repo.root)],
            env={
                "REPO_PRIVACY_AUDIT_BIN": str(self.mock_core),
                "REPO_PRIVACY_AUDIT_POLICY": str(policy),
                **git_env(repo),
            },
        )
        self.assertEqual(proc.returncode, 0, _text(proc))
        self.assertEqual(self.read_state(repo)["policy"], str(policy))

    def test_policy_path_with_shell_metacharacters_is_safe(self) -> None:
        """政策路徑含 shell meta 字元時，仍要寫成合法 JSON 且不被執行。"""
        repo = self.make_repo("policy-metachars")
        tricky_dir = self.tool_dir / "wei$(touch-pwned)x'$(id)'`id`"
        tricky_dir.mkdir(parents=True, exist_ok=True)
        policy = tricky_dir / "policy.json"
        policy.write_text('{"schemaVersion":"1.0"}', encoding="utf-8")

        self.install(repo)
        proc = run_hooks(
            ["install", "--repo", str(repo.root)],
            env={"REPO_PRIVACY_AUDIT_BIN": str(self.mock_core), "REPO_PRIVACY_AUDIT_POLICY": str(policy), **git_env(repo)},
        )
        self.assertEqual(proc.returncode, 0, _text(proc))
        state = self.read_state(repo)
        self.assertEqual(state["policy"], str(policy))
        self.assertFalse((self.tool_dir / "pwned").exists(), "政策路徑不得被當成 shell 片段執行")

    def test_does_not_touch_other_repo(self) -> None:
        """對 repo A 安裝不得影響 repo B。"""
        repo_a = self.make_repo("multi-a")
        repo_b = self.make_repo("multi-b")
        self.install(repo_a)
        self.assertTrue(self.hook_path(repo_a, "pre-commit").exists())
        for name in HOOK_NAMES:
            self.assertFalse(self.hook_path(repo_b, name).exists(), f"不該影響其他 repo：{name}")
        self.assertFalse(self.state_file(repo_b).exists())


class TestStatus(HookTestCase):
    def test_status_reports_installed_state(self) -> None:
        repo = self.make_repo("status-ok")
        self.install(repo)
        proc = run_hooks(["status", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout
        self.assertIn("pre-commit：已由 repo-privacy-audit 安裝", out)
        self.assertIn("core.hooksPath  ：未設定", out)

    def test_status_reports_foreign_hook(self) -> None:
        repo = self.make_repo("status-foreign")
        target = self.hook_path(repo, "pre-commit")
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        proc = run_hooks(["status", "--repo", str(repo.root)], env=git_env(repo))
        out = proc.stdout
        self.assertIn("不是 repo-privacy-audit 建立", out)

    def test_status_works_without_install(self) -> None:
        repo = self.make_repo("status-none")
        proc = run_hooks(["status", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("未安裝", proc.stdout)


class TestUninstall(HookTestCase):
    def test_uninstall_removes_owned_hooks(self) -> None:
        repo = self.make_repo("uninstall-ok")
        self.install(repo)
        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 0, _text(proc))
        for name in HOOK_NAMES:
            self.assertFalse(self.hook_path(repo, name).exists(), f"{name} 未被移除")
        # 沒有 --purge 時設定檔保留
        self.assertTrue(self.state_file(repo).exists())

    def test_uninstall_purge_removes_only_our_files(self) -> None:
        repo = self.make_repo("uninstall-purge")
        self.install(repo)
        state_dir = self.state_file(repo).parent
        bystander = state_dir / "unrelated.txt"
        bystander.write_text("使用者自己的檔案\n", encoding="utf-8")

        proc = run_hooks(["uninstall", "--repo", str(repo.root), "--purge"], env=git_env(repo))
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(self.state_file(repo).exists())
        self.assertTrue(bystander.exists(), "不得刪除本工具狀態目錄裡的其他檔案")
        self.assertTrue(state_dir.is_dir())

    def test_uninstall_leaves_foreign_hook(self) -> None:
        repo = self.make_repo("uninstall-foreign")
        target = self.hook_path(repo, "pre-commit")
        target.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        target.chmod(0o755)
        original = target.read_text(encoding="utf-8")

        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertIn("不是本工具建立", proc.stdout)

    def test_uninstall_keeps_modified_owned_hook(self) -> None:
        repo = self.make_repo("uninstall-modified")
        self.install(repo)
        target = self.hook_path(repo, "pre-commit")
        target.write_text(target.read_text(encoding="utf-8") + "\n# 我自己加的行\n", encoding="utf-8")

        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(target.exists(), "被修改過的 owned hook 不應自動刪除")
        self.assertIn("已被修改過", proc.stdout)
        # 未被修改的那個仍會移除
        self.assertFalse(self.hook_path(repo, "pre-push").exists())

    def test_uninstall_refuses_when_hooks_path_changed(self) -> None:
        """hooksPath 被改掉後不得依新路徑去刪別處的檔案。"""
        repo = self.make_repo("uninstall-hookspath")
        self.install(repo)
        # 模擬使用者後來設定了 hooksPath
        run_git(repo, ["config", "core.hooksPath", "some-other-hooks"])
        decoy_dir = repo.root / "some-other-hooks"
        decoy_dir.mkdir(parents=True, exist_ok=True)
        decoy = decoy_dir / "pre-commit"
        decoy.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        decoy.chmod(0o755)
        original = decoy.read_text(encoding="utf-8")

        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("core.hooksPath", proc.stderr)
        self.assertEqual(decoy.read_text(encoding="utf-8"), original, "不得刪除 hooksPath 指向的檔案")

    def test_uninstall_refuses_linked_worktree(self) -> None:
        repo = self.make_repo("uninstall-worktree")
        self.install(repo)
        linked = self.linked_worktree(repo)
        proc = run_hooks(["uninstall", "--repo", str(linked)], env=git_env(repo))
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(self.hook_path(repo, "pre-commit").exists(), "worktree 操作不得影響主 worktree 的 hook")

    def test_uninstall_without_install_is_harmless(self) -> None:
        repo = self.make_repo("uninstall-none")
        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("不存在，略過", proc.stdout)

    def test_uninstall_refuses_when_state_file_is_gone(self) -> None:
        """設定檔不見時不得只憑 marker 就刪檔。"""
        repo = self.make_repo("uninstall-no-state")
        self.install(repo)
        target = self.hook_path(repo, "pre-commit")
        original = target.read_text(encoding="utf-8")
        self.state_file(repo).unlink()

        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 2, _text(proc))
        self.assertIn("設定檔不存在", proc.stderr)
        self.assertTrue(target.exists(), "沒有雜湊可比對時必須保留")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_uninstall_refuses_when_state_file_is_corrupt(self) -> None:
        repo = self.make_repo("uninstall-corrupt")
        self.install(repo)
        target = self.hook_path(repo, "pre-commit")
        original = target.read_text(encoding="utf-8")
        self.state_file(repo).write_text("[[[ 不是 JSON", encoding="utf-8")

        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 2, _text(proc))
        self.assertIn("無法解析", proc.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_uninstall_keeps_hook_when_digest_cannot_be_read(self) -> None:
        """雜湊欄位缺失也要保留，而不是因為比對不到就放行刪除。"""
        repo = self.make_repo("uninstall-no-digest")
        self.install(repo)
        state = self.read_state(repo)
        state.pop("preCommitSha256", None)
        self.state_file(repo).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        target = self.hook_path(repo, "pre-commit")
        original = target.read_text(encoding="utf-8")

        proc = run_hooks(["uninstall", "--repo", str(repo.root)], env=git_env(repo))
        self.assertEqual(proc.returncode, 0, _text(proc))
        self.assertTrue(target.exists(), "無法驗證時必須保留")
        self.assertIn("未自動刪除", proc.stdout)
        self.assertEqual(target.read_text(encoding="utf-8"), original)


class TestArgumentHandling(HookTestCase):
    """參數處理：缺參數必須立即非零，不能無限迴圈或假成功。"""

    def _bounded(self, args: Sequence[str], env: Optional[Dict] = None, timeout: int = 20) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["bash", str(HOOKS_SH), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, **(env or {})},
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"指令逾時（可能無限迴圈）：{' '.join(args)}")

    def test_repo_flag_without_value(self) -> None:
        for command in ("install", "uninstall", "status"):
            proc = self._bounded([command, "--repo"])
            self.assertNotEqual(proc.returncode, 0, f"{command} --repo 缺值應非零")
            self.assertIn("--repo", _text(proc))

    def test_unknown_option(self) -> None:
        for command in ("install", "uninstall", "status"):
            proc = self._bounded([command, "--not-a-real-option"])
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("未知選項", _text(proc))

    def test_unknown_command_and_no_command(self) -> None:
        self.assertNotEqual(self._bounded(["frobnicate"]).returncode, 0)
        self.assertNotEqual(self._bounded([]).returncode, 0)

    def test_help_exits_zero(self) -> None:
        for args in (["--help"], ["help"], ["install", "--help"]):
            self.assertEqual(self._bounded(args).returncode, 0)


class TestHookExecution(HookTestCase):
    """真的觸發 hook：git commit 與 git push 都必須被擋。"""

    def test_commit_blocked_by_exit_1(self) -> None:
        repo = self.make_repo("commit-exit1")
        self.install(repo)
        proc = self.commit_with_env(repo, "should be blocked", core_exit="1")
        self.assertEqual(proc.returncode, 1, "hook 退出碼 1 必須擋下 commit")
        self.assertIn("mock-core-args", _text(proc))
        # 沒有產生 commit
        self.assertEqual(repo.git("rev-list", "--count", "HEAD").strip(), "1")

    def test_commit_blocked_by_exit_2(self) -> None:
        repo = self.make_repo("commit-exit2")
        self.install(repo)
        proc = self.commit_with_env(repo, "should be blocked", core_exit="2")
        self.assertEqual(proc.returncode, 1, "hook 退出碼 2（未完整掃描）也必須擋下 commit")
        self.assertEqual(repo.git("rev-list", "--count", "HEAD").strip(), "1")

    def test_commit_allowed_on_exit_0(self) -> None:
        repo = self.make_repo("commit-exit0")
        self.install(repo)
        proc = self.commit_with_env(repo, "clean change", core_exit="0")
        self.assertEqual(proc.returncode, 0, _text(proc))
        self.assertEqual(repo.git("rev-list", "--count", "HEAD").strip(), "2")

    def test_commit_hook_passes_staged_and_repo(self) -> None:
        repo = self.make_repo("commit-args")
        self.install(repo)
        proc = self.commit_with_env(repo, "clean change", core_exit="0")
        output = _text(proc)
        self.assertIn("--staged", output)
        self.assertIn("--repo", output)
        self.assertIn(str(repo.root), output)

    def test_hook_runs_via_symlink(self) -> None:
        """hook 被 symlink 呼叫時仍要能解析自身位置。"""
        repo = self.make_repo("commit-symlink")
        self.install(repo)
        real = self.hook_path(repo, "pre-commit")
        link = self.tool_dir / "linked-pre-commit"
        link.symlink_to(real)

        repo.write("payload.txt", "y\n")
        run_git(repo, ["add", "-A"], check=True)
        env = {**os.environ, **git_env(repo)}
        env["REPO_PRIVACY_AUDIT_BIN"] = str(self.mock_core)
        env["MOCK_CORE_EXIT"] = "0"
        proc = subprocess.run(
            ["bash", str(link)],
            cwd=str(repo.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, _text(proc))
        self.assertIn("mock-core-args", _text(proc))

    def test_fails_closed_when_cli_deleted(self) -> None:
        """設定檔記錄的 CLI 被移走後，hook 必須以非零擋下，而不是放行。

        這裡刻意**不**用 REPO_PRIVACY_AUDIT_BIN 覆寫：真實情境是使用者把 skill
        目錄改名或刪掉，hook 只能讀設定檔裡那個已經不存在的路徑。
        """
        repo = self.make_repo("cli-moved")
        self.install(repo)
        self.assertEqual(self.read_state(repo)["cli"], str(self.mock_core))
        shutil.copy(self.mock_core, self.tool_dir / "moved-core.sh")
        self.mock_core.unlink()
        # 環境變數不帶 REPO_PRIVACY_AUDIT_BIN，只留著搬走的副本，
        # 確保 hook 沒辦法「僥倖找到」一個還能用的 CLI。
        self.mock_core = self.tool_dir / "moved-core.sh"

        proc = self.commit_with_env(repo, "after cli moved", core_exit="0", use_bin_override=False)
        self.assertNotEqual(proc.returncode, 0, "CLI 不存在時不得放行 commit")
        output = _text(proc)
        self.assertNotIn("mock-core-args", output, "CLI 不存在時不該執行掃描")
        self.assertIn("不存在", output, "應明確說明記錄的 CLI 不存在")
        self.assertEqual(repo.git("rev-list", "--count", "HEAD").strip(), "1", "不該產生 commit")

    def test_fails_closed_when_state_removed(self) -> None:
        repo = self.make_repo("state-removed")
        self.install(repo)
        self.state_file(repo).unlink()

        proc = self.commit_with_env(repo, "after state removed", core_exit="0")
        self.assertNotEqual(proc.returncode, 0, "設定檔不見時不得放行 commit")
        self.assertNotIn("mock-core-args", _text(proc))

    def _setup_remote(self, repo: GitRepoFixture) -> Path:
        """建立一個 bare remote。目錄由暫存根目錄收走，測完只清掉自己那個。"""
        remote = Path(tempfile.mkdtemp(prefix=f"{repo.name}-remote-", dir=ensure_scratch()))
        self.addCleanup(shutil.rmtree, remote, True)
        remote.rmdir()
        run_git(repo, ["init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        run_git(repo, ["remote", "add", "origin", str(remote)], check=True)
        return remote

    def _mock_env(self, repo: GitRepoFixture, core_exit: str = "0") -> Dict[str, str]:
        """讓已安裝的 hook 以 mock core 執行，並回傳指定退出碼。"""
        env = git_env(repo)
        env["REPO_PRIVACY_AUDIT_BIN"] = str(self.mock_core)
        env["MOCK_CORE_EXIT"] = core_exit
        return env

    def _push(
        self,
        repo: GitRepoFixture,
        *,
        core_exit: str,
        refspec: Sequence[str] = ("HEAD:refs/heads/main",),
    ) -> subprocess.CompletedProcess:
        """真正執行 `git push`。refspec 以序列傳入，每個元素各自是一個 argv。"""
        env = {**os.environ, **self._mock_env(repo, core_exit)}
        return subprocess.run(
            ["git", "-C", str(repo.root), "push", "origin", *refspec],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )

    def _push_setup(self, repo: GitRepoFixture, *refspec: str) -> None:
        """讓 hook 放行的預備推送（mock core 回 0）。"""
        proc = self._push(repo, core_exit="0", refspec=refspec)
        self.assertEqual(proc.returncode, 0, f"準備推送失敗：{_text(proc)}")

    def test_push_blocked_by_exit_1(self) -> None:
        repo = self.make_repo("push-exit1")
        self.install(repo)
        self._setup_remote(repo)
        run_git(repo, ["add", "-A"])
        run_git(repo, ["commit", "-q", "-m", "add payload"], env=self._mock_env(repo, "0"))

        proc = self._push(repo, core_exit="1")
        self.assertNotEqual(proc.returncode, 0, "hook 退出碼 1 必須擋下 push")
        output = _text(proc)
        self.assertIn("mock-core-args", output)

    def test_push_blocked_by_exit_2(self) -> None:
        repo = self.make_repo("push-exit2")
        self.install(repo)
        self._setup_remote(repo)
        run_git(repo, ["add", "-A"])
        run_git(repo, ["commit", "-q", "-m", "add payload"], env=self._mock_env(repo, "0"))

        proc = self._push(repo, core_exit="2")
        self.assertNotEqual(proc.returncode, 0, "hook 退出碼 2（未完整掃描）也必須擋下 push")

    def test_push_allowed_on_exit_0_and_receives_refs_on_stdin(self) -> None:
        """首次 push：hook 必須從 stdin 收到 refs 清單。"""
        repo = self.make_repo("push-first")
        self.install(repo)
        remote = self._setup_remote(repo)
        run_git(repo, ["add", "-A"])
        run_git(repo, ["commit", "-q", "-m", "add payload"], env=self._mock_env(repo, "0"))

        proc = self._push(repo, core_exit="0")
        self.assertEqual(proc.returncode, 0, _text(proc))
        output = _text(proc)
        self.assertIn("--pre-push", output)
        self.assertIn("refs/heads/main", output, "stdin 的 refs 清單應被傳到掃描器")
        # 遠端真的有拿到 ref
        refs = subprocess.run(
            ["git", "-C", str(remote), "for-each-ref", "--format=%(refname)"],
            stdout=subprocess.PIPE,
            check=False,
        ).stdout.decode()
        self.assertIn("refs/heads/main", refs)

    def test_push_multiple_refs(self) -> None:
        """一次 push 多個 ref（含非預設名稱）都要經過 hook，且全部被擋。

        三個 refspec 必須是三個獨立 argv；合成一個字串會讓 Git 直接拒絕，
        那就等於沒有測到 hook。
        """
        repo = self.make_repo("push-multi")
        self.install(repo)
        remote = self._setup_remote(repo)
        repo.git("checkout", "-q", "-b", "feature/x")
        repo.write("feature.txt", "f\n", stage=True)
        repo.git("commit", "-q", "-m", "feature")
        run_git(repo, ["checkout", "-q", "main"])
        run_git(repo, ["branch", "-q", "other"])

        proc = self._push(repo, core_exit="1", refspec=("main", "feature/x", "other"))
        self.assertNotEqual(proc.returncode, 0, "hook 退出碼 1 必須擋下這次 push")
        output = _text(proc)
        self.assertIn("mock-core-args", output, "hook 必須真的被執行")
        for ref in ("refs/heads/main", "refs/heads/feature/x", "refs/heads/other"):
            self.assertIn(ref, output, f"stdin 的 refs 清單應包含 {ref}")
        # 遠端不該拿到任何一個被擋下的 ref
        refs = subprocess.run(
            ["git", "-C", str(remote), "for-each-ref", "--format=%(refname)"],
            stdout=subprocess.PIPE,
            check=False,
        ).stdout.decode("utf-8", "replace")
        for ref in ("refs/heads/main", "refs/heads/feature/x", "refs/heads/other"):
            self.assertNotIn(ref, refs, f"被擋下的 push 不該讓遠端出現 {ref}")

    def test_push_multiple_refs_allowed_on_exit_0(self) -> None:
        """放行時多個 ref 都必須真的推到遠端。"""
        repo = self.make_repo("push-multi-ok")
        self.install(repo)
        remote = self._setup_remote(repo)
        repo.git("checkout", "-q", "-b", "feature/x")
        repo.write("feature.txt", "f\n", stage=True)
        repo.git("commit", "-q", "-m", "feature")
        run_git(repo, ["checkout", "-q", "main"])
        run_git(repo, ["branch", "-q", "other"])

        proc = self._push(repo, core_exit="0", refspec=("main", "feature/x", "other"))
        self.assertEqual(proc.returncode, 0, _text(proc))
        refs = subprocess.run(
            ["git", "-C", str(remote), "for-each-ref", "--format=%(refname)"],
            stdout=subprocess.PIPE,
            check=False,
        ).stdout.decode("utf-8", "replace")
        for ref in ("refs/heads/main", "refs/heads/feature/x", "refs/heads/other"):
            self.assertIn(ref, refs, f"遠端缺少 {ref}")

    def test_push_deletion_runs_hook(self) -> None:
        """刪除分支的 push 也會觸發 hook（refs 清單會被讀取）。"""
        repo = self.make_repo("push-delete")
        self.install(repo)
        remote = self._setup_remote(repo)
        self._push_setup(repo, "HEAD:refs/heads/main")
        repo.git("branch", "-q", "doomed")
        self._push_setup(repo, "doomed:refs/heads/doomed")
        run_git(repo, ["branch", "-q", "-D", "doomed"])

        proc = self._push(repo, core_exit="0", refspec=(":refs/heads/doomed",))
        output = _text(proc)
        self.assertIn("--pre-push", output, "刪除推送也會觸發 hook")

    def test_push_fails_closed_when_state_removed(self) -> None:
        repo = self.make_repo("push-state-removed")
        self.install(repo)
        self._setup_remote(repo)
        run_git(repo, ["add", "-A"])
        run_git(repo, ["commit", "-q", "-m", "add payload"], env=self._mock_env(repo, "0"))
        self.state_file(repo).unlink()

        proc = self._push(repo, core_exit="0")
        self.assertNotEqual(proc.returncode, 0, "設定檔不見時不得放行 push")

    def test_reinstall_after_own_hook_is_fine(self) -> None:
        """已由本工具安裝的 hook 可以重新安裝（更新設定）。"""
        repo = self.make_repo("reinstall")
        self.install(repo)
        first = self.hook_path(repo, "pre-commit").read_text(encoding="utf-8")
        self.install(repo)
        second = self.hook_path(repo, "pre-commit").read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(self.read_state(repo)["cli"], str(self.mock_core))


if __name__ == "__main__":
    unittest.main()
