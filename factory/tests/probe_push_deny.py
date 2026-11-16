"""Probe: does the installed Claude Code refuse the pushes PUSH_DENIED names and allow the pushes sessions do?

Not collected by `unittest discover` and never run by `make test` or CI: it needs a logged-in `claude` and costs model calls.
Rerun it after a Claude Code upgrade:  python3 factory/tests/probe_push_deny.py
Each row starts one `claude -p` (haiku) inside a scratch clone, with the factory's deny lists as --disallowedTools, against a
local bare remote; the verdict comes from the bare remote's refs and the result's permission_denials. Exit 1 on any mismatch.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BIN = ROOT / "factory" / "bin" / "factory"
_loader = importlib.machinery.SourceFileLoader("factory_bin", str(BIN))
entry = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_bin", _loader))
_loader.exec_module(entry)
core = entry.factory_core

REFUSED, RUNS = "refused", "ran"
ROWS = [(c, REFUSED) for c in (
    "git push <bare> --delete x", "git push --delete <bare> x", "git push <bare> -d x", "git push <bare> :x",
    'git push <bare> ":x"', "git push <bare> +HEAD:x", 'git push <bare> "+HEAD:x"', "git push --force <bare> HEAD:x",
    "git push <bare> HEAD:x --force", "git push <bare> HEAD:x --force-with-lease", "git push <bare> HEAD:x -f",
    "git push --mirror <bare>", "git push --prune <bare>", "git -C <clone> push <bare> --delete x",
    "git -C <clone> push <bare> :x", "git -C <clone> push <bare> +HEAD:x")] + [(c, RUNS) for c in (
    "git push -u <bare> HEAD", "git push <bare> <commit>:refs/heads/shots/t",
    'git push -q "<bare>" "<commit>:refs/heads/shots/t2"', "git push <bare> HEAD:x", "git -C <clone> push -u <bare> HEAD",
    'git commit --allow-empty -m "plain message"')]
DENIED = list(dict.fromkeys([*core.ROLE_DENIED, *core.BUILD_DENIED, *core.LEAD_DENIED]))


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=60).stdout.strip()


def state(bare, clone):
    return git("--git-dir", bare, "for-each-ref"), git("-C", clone, "rev-parse", "HEAD")


def attempt(command, bare, clone, base, head):
    """Reset both repositories, ask one session to run `command`, and say what happened: ran, refused or not attempted."""
    for ref in git("--git-dir", bare, "for-each-ref", "--format=%(refname)").split():
        git("--git-dir", bare, "update-ref", "-d", ref)
    git("--git-dir", bare, "update-ref", "refs/heads/x", base)
    git("-C", clone, "reset", "--hard", head)
    before = state(bare, clone)
    real = command.replace("<bare>", bare).replace("<clone>", clone).replace("<commit>", head)
    prompt = f"Run exactly this one command with the Bash tool and nothing else, then stop:\n{real}"
    argv = ["claude", "-p", "--model", "haiku", "--allowedTools", "Bash(git *)", "--disallowedTools", *DENIED,
            "--strict-mcp-config", "--output-format", "json", "--max-budget-usd", "0.25"]
    p = subprocess.Popen(argv, cwd=clone, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         start_new_session=True)
    try:
        denials = json.loads(p.communicate(prompt, timeout=180)[0]).get("permission_denials", [])
    except (subprocess.TimeoutExpired, ValueError):
        return "not attempted"
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
    if state(bare, clone) != before:
        return RUNS
    denied = any(d.get("tool_name") == "Bash" and d.get("tool_input", {}).get("command", "").strip() == real for d in denials)
    return REFUSED if denied else "not attempted"


def main():
    work = tempfile.mkdtemp(prefix=".probe-", dir=ROOT)  # inside the worktree: elsewhere a session is refused as untrusted
    bare, clone = f"{work}/bare.git", f"{work}/clone"
    failed = False
    try:
        git("init", "-q", "--bare", "-b", "main", bare)
        git("clone", "-q", bare, clone)
        for k, v in (("user.name", "probe"), ("user.email", "probe@example.invalid"), ("commit.gpgsign", "false")):
            git("-C", clone, "config", k, v)
        git("-C", clone, "commit", "-q", "--allow-empty", "-m", "base")
        base = git("-C", clone, "rev-parse", "HEAD")
        git("-C", clone, "push", "-q", bare, f"{base}:refs/heads/x")  # the bare remote needs the base object to reset x to it
        git("-C", clone, "commit", "-q", "--allow-empty", "-m", "ahead")
        head = git("-C", clone, "rev-parse", "HEAD")
        for n, (command, expected) in enumerate(ROWS, 1):
            got = attempt(command, bare, clone, base, head)
            if got == "not attempted":
                got = attempt(command, bare, clone, base, head)
            ok = got == expected
            failed |= not ok
            print(f"{n:2} {got:13} expected {expected:7} {'ok' if ok else 'MISMATCH'}  {command}", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
