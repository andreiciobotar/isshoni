#!/usr/bin/env python3
"""Staged proof that a line of the lead outbox ends `delivered` only when the lead's own session took it. Run by hand, never by
unittest (the name does not start with `test`). Real `claude` sessions, a few cents each on the lead model `haiku`:

    python3 factory/tests/live_receipt.py DIR setup
    python3 factory/tests/live_receipt.py DIR dispatcher
    python3 factory/tests/live_receipt.py DIR lead SLUG [--bare] [--model M] PROMPT
    python3 factory/tests/live_receipt.py DIR forward SLUG TEXT      # the real `forward`; prints the result and the seconds
    python3 factory/tests/live_receipt.py DIR tick                   # the real `drain_outbox`
    python3 factory/tests/live_receipt.py DIR rows                   # the outbox records, one JSON line each
    python3 factory/tests/live_receipt.py DIR stop [SLUG]            # stop the sessions this script started (all, or SLUG's)

DIR is a new scratch directory: a git checkout `pocket` (its git directory `pocket.git` kept apart, so that Claude Code finds no
git root to ask trust for and the directory counts as one under a trusted parent) with plain task directories under
`.claude/worktrees`, a scratch FACTORY_HOME `home`, `sessions.json` (every session started).
A lead is started in its task worktree with `--plugin-dir` set to the plugin this script sits in, the scratch FACTORY_HOME in its
`env` setting and the installed factory plugin switched off, so the receipt is written by this plugin's hook into the scratch
store. `--bare` starts it without any plugin: no hook, so it can never write a receipt.
The script refuses to run when FACTORY_HOME names the real home. Only the sessions it started are stopped."""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from live_undelivered import HERE, load, say, sh

DISPATCHER = "scratch-dispatcher"
PLUGIN = HERE.parent
HOUSE = {"attribution": {"commit": "", "pr": ""}}


def scratch(args):
    tmp = os.path.realpath(args.dir)
    os.makedirs(tmp, exist_ok=True)
    factory = load(str(PLUGIN / "bin" / "factory"), tmp, True)
    for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE", "FACTORY_SESSION", "CLAUDE_CODE_SESSION_ID"):
        os.environ.pop(var, None)
    checkout = os.path.join(tmp, "pocket")
    if os.path.exists(os.path.join(tmp, "pocket.git")):
        os.environ.update(GIT_DIR=os.path.join(tmp, "pocket.git"), GIT_WORK_TREE=checkout)
    return factory, tmp, checkout


def open_repo(factory, checkout):
    """The factory's view of the scratch repository, rooted at the checkout as the hooks of the leads see it (`git worktree list`
    names the git directory for a checkout without a `.git` of its own)."""
    os.chdir(checkout)
    repo = factory.Repo()
    repo.root = checkout
    return repo


def task_dir(factory, repo, slug):
    """The task `slug` declared, in a directory under `.claude/worktrees` holding a dangling `.git` link: the factory's `run`
    refuses a task directory without a `.git` entry, and a link that points nowhere is no git root for Claude Code's trust check."""
    path = repo.worktree_path(slug)
    os.makedirs(path, exist_ok=True)
    if not os.path.lexists(os.path.join(path, ".git")):
        os.symlink("nowhere", os.path.join(path, ".git"))
    if slug not in factory.read_tasks(repo.root):
        factory.declare(repo, slug, "lead", "working")


def sessions_file(tmp):
    return Path(tmp, "sessions.json")


def started(tmp):
    path = sessions_file(tmp)
    return json.loads(path.read_text()) if path.exists() else []


def setup(factory, tmp, checkout, args):
    origin = os.path.join(tmp, "origin.git")
    sh("git", "init", "-q", "--bare", origin, cwd=tmp)
    sh("git", "clone", "-q", "--separate-git-dir", os.path.join(tmp, "pocket.git"), origin, checkout, cwd=tmp)
    Path(checkout, "f.txt").write_text("x\n")
    Path(checkout, ".gitignore").write_text(".claude/\n")
    sh("git", "add", "-A", cwd=checkout)
    sh("git", "commit", "-q", "-m", "base", cwd=checkout)
    sh("git", "push", "-q", "-u", "origin", "main", cwd=checkout)
    os.remove(os.path.join(checkout, ".git"))  # Claude Code asks to trust each git root: the checkout stays a plain directory under a trusted one
    os.environ.update(GIT_DIR=os.path.join(tmp, "pocket.git"), GIT_WORK_TREE=checkout)
    say(f"scratch ready: {tmp}")


def start(factory, tmp, cwd, name, model, prompt, bare):
    settings = {**HOUSE, "env": {"FACTORY_HOME": os.environ["FACTORY_HOME"]}, "enabledPlugins": {"factory@factory": False}}
    argv = ["claude", "--bg", "-n", name, "--model", model, "--settings", json.dumps(settings), "--permission-mode", "dontAsk",
            "--allowedTools=Bash",*([] if bare else ["--plugin-dir", str(PLUGIN)]), prompt]
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    say(f"claude --bg exited {p.returncode}: {(p.stdout + p.stderr).strip()}")
    if p.returncode:
        sys.exit(1)
    short = factory.BACKGROUNDED_RE.search(p.stdout + p.stderr).group(1)
    time.sleep(3)
    rows = [s for s in factory.list_sessions() or [] if s.get("id") == short or s.get("sessionId", "").startswith(short)]
    full = rows[0].get("sessionId") if rows else None
    entry = {"name": name, "id": short, "sessionId": full, "pid": rows[0].get("pid") if rows else None, "bare": bare, "started": factory.now()}
    sessions_file(tmp).write_text(json.dumps([*started(tmp), entry]))
    say(f"started: {json.dumps(entry)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("setup", "dispatcher", "tick", "rows", "stop"):
        p = sub.add_parser(name)
        if name == "stop":
            p.add_argument("slug", nargs="?")
    p = sub.add_parser("lead")
    p.add_argument("slug")
    p.add_argument("--bare", action="store_true")
    p.add_argument("--model", default="haiku")
    p.add_argument("prompt")
    p = sub.add_parser("forward")
    p.add_argument("slug")
    p.add_argument("text")
    args = ap.parse_args()
    factory, tmp, checkout = scratch(args)
    if args.cmd == "setup":
        return setup(factory, tmp, checkout, args)
    repo = open_repo(factory, checkout)
    if args.cmd in ("lead", "forward"):
        task_dir(factory, repo, args.slug)
    if args.cmd == "dispatcher":
        start(factory, tmp, checkout, DISPATCHER, "haiku", "You are a test session. Reply with the single word ready and stop. Every message that arrives later: reply with the single word ack.", False)
        sid = started(tmp)[-1]["sessionId"]
        factory.repo_state(repo.root, "dispatcher", "json").write_text(json.dumps({"root": repo.root, "session": sid}))
    elif args.cmd == "lead":
        start(factory, tmp, os.path.join(checkout, ".claude", "worktrees", args.slug), args.slug, args.model, args.prompt, args.bare)
    elif args.cmd == "forward":
        t0 = time.time()
        got = factory.forward(repo, DISPATCHER, args.slug, "TECH LEAD", args.text)
        say(f"forward returned {got!r} after {time.time() - t0:.1f} s")
    elif args.cmd == "tick":
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            factory.drain_outbox(repo, DISPATCHER)
        say(out.getvalue().rstrip() or "(tick printed nothing)")
    elif args.cmd == "rows":
        for r in factory.read_log("outbox"):
            say(json.dumps(r))
    elif args.cmd == "stop":
        mine = [s for s in started(tmp) if args.slug in (None, s["name"])]
        for s in mine:
            p = subprocess.run(["claude", "stop", s["id"]], capture_output=True, text=True, timeout=60)
            say(f"claude stop {s['id']} ({s['name']}): exit {p.returncode} {(p.stdout + p.stderr).strip()}")


if __name__ == "__main__":
    sys.exit(main())
