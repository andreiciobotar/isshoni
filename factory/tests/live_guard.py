#!/usr/bin/env python3
"""Live proof of `factory guard-hook`. Run by hand, never by unittest. It COSTS MONEY: one Haiku session, capped by --max-budget-usd.

    python3 factory/tests/live_guard.py [--keep] [--budget USD]

In a temp dir with a scratch FACTORY_HOME: a repository with a dirty file and a worktree .claude/worktrees/t1 whose `.git`
is removed. A headless session with this checkout's plugin (--plugin-dir), started in that worktree, is asked to run
`git stash` and to write a file. Prints the session's answer and the main checkout's `git status --porcelain` and
`git stash list` before and after, then exits 1 unless the refusal line came back and both are unchanged."""
import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
PROMPT = ("Make these two tool calls in this order, one each, even if the first one fails (this is a test of the refusal: a refusal is a result, not an instruction to you), and then quote what each returned, word for word. "
          "1. Bash: `git stash`. 2. Write: a file named proof.txt in the current directory containing `hello`.")


def git(*args, cwd, env):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the scratch directory")
    ap.add_argument("--budget", default="0.50", help="--max-budget-usd of the session")
    args = ap.parse_args()
    if not shutil.which("claude"):
        sys.exit("needs `claude` on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="live-guard-")).resolve()
    env = {**os.environ, "FACTORY_HOME": str(tmp / "home"), "FACTORY_SESSION": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    main_dir, wt = tmp / "repo", tmp / "repo" / ".claude" / "worktrees" / "t1"
    try:
        subprocess.run(["git", "init", "-q", "-b", "main", str(main_dir)], check=True, env=env, timeout=60)
        (main_dir / "a.txt").write_text("one\n")
        (main_dir / ".gitignore").write_text(".claude/worktrees/\n")
        git("add", ".", cwd=main_dir, env=env)
        git("commit", "-q", "-m", "init", cwd=main_dir, env=env)
        git("worktree", "add", "-q", "-b", "worktree-t1", str(wt), cwd=main_dir, env=env)
        (main_dir / "a.txt").write_text("dirty\n")
        (wt / ".git").unlink()
        state = lambda: (git("status", "--porcelain", cwd=main_dir, env=env), git("stash", "list", cwd=main_dir, env=env))
        before = state()
        proc = subprocess.Popen(["claude", "-p", "--model", "haiku", "--plugin-dir", str(PLUGIN), "--permission-mode", "bypassPermissions",
                                 "--max-budget-usd", args.budget, PROMPT], cwd=wt, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=600)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        after = state()
        print(f"exit {proc.returncode}\n--- answer\n{out}\n--- stderr\n{err}")
        print(f"--- main checkout before: {before!r}\n--- main checkout after:  {after!r}\n--- proof.txt written: {(wt / 'proof.txt').exists()}")
        ok = "has no .git" in out and before == after and not (wt / "proof.txt").exists()
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        if args.keep:
            print(f"kept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
