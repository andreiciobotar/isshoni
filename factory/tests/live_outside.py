#!/usr/bin/env python3
"""Live proof that a session that is not the factory's leaves no side effect. Run by hand, never by unittest. It COSTS MONEY: two Haiku
sessions with one subagent each, each capped by --max-budget-usd.

    python3 factory/tests/live_outside.py [--keep] [--budget USD]

In a temp directory with a scratch repository and a scratch FACTORY_HOME (refused under the real ~/.claude): a headless session with
this checkout's plugin (--plugin-dir), without FACTORY_SESSION and FACTORY_ROLE, started in the repository, is asked to start
`factory:reviewer` on a trivial diff and to end. Asserted: the reviewer ran, and the scratch store holds no events, feedback, tasks,
firings, queue or outbox file. Then the control: the task declared in the scratch store and the same session started in that task's
worktree. Asserted: `SubagentStart` and `SubagentStop` records with role `reviewer` and the task's slug are there.

Not proved here: the filing of feedback. A `-p` session's subagent is not offered `SubagentHandback` (seen: the tool is in no transcript
of these runs), and that call, or a `SendMessage` of kind DONE, is what files a `Factory feedback` section; `test_telemetry.py` covers it.

The scratch directory and the sessions' transcripts (the project directories named after it) are deleted at the end unless --keep;
the size is printed."""
import argparse
import glob
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

import real_store

PLUGIN = Path(__file__).resolve().parent.parent
PROJECTS = Path("~/.claude/projects").expanduser()
PROMPT = ("Use the Agent tool once with subagent_type factory:reviewer and the prompt `Review the one-line change in git diff. "
          "Say APPROVE or CHANGES_NEEDED in one line.` Then reply with the word done and end. Use no other tool.")
FILES = ("events", "feedback", "tasks", "firings", "queue", "outbox")


def git(*args, cwd, env):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60).stdout


def session(cwd, env, name, budget):
    """One headless session; (its stream-json events, its stderr, the exit code). Nothing of it outlives this call."""
    argv = ["claude", "-p", "--model", "haiku", "--plugin-dir", str(PLUGIN), "--permission-mode", "bypassPermissions", "--name", name,
            "--max-budget-usd", budget, "--output-format", "stream-json", "--verbose", PROMPT]
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=600)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    events = []
    for line in out.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events, err, proc.returncode


def reviewer_ran(events):
    """Whether the session called the Agent tool for factory:reviewer and got a result that is not an error."""
    blocks = [b for e in events for b in (e.get("message") or {}).get("content") or [] if isinstance(b, dict)]
    ids = {b["id"] for b in blocks if b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task")
           and b["input"].get("subagent_type") == "factory:reviewer"}
    return any(b.get("type") == "tool_result" and b.get("tool_use_id") in ids and not b.get("is_error") for b in blocks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the scratch directory and the transcripts")
    ap.add_argument("--budget", default="0.50", help="--max-budget-usd of each session")
    args = ap.parse_args()
    if not shutil.which("claude"):
        sys.exit("needs `claude` on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="live-outside-")).resolve()
    home = tmp / "home"
    real_store.refuse(home)
    env = {k: v for k, v in os.environ.items() if k not in ("FACTORY_SESSION", "FACTORY_ROLE", "CLAUDECODE", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE")}
    env.update(FACTORY_HOME=str(home), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    os.environ["FACTORY_HOME"] = str(home)
    loader = importlib.machinery.SourceFileLoader("factory_bin", str(PLUGIN / "bin" / "factory"))
    factory = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_bin", loader))
    loader.exec_module(factory)
    factory = getattr(factory, "factory_core", factory)
    repo, wt = tmp / "repo", tmp / "repo" / ".claude" / "worktrees" / "proof-task"
    spend, failures = 0.0, []

    def check(ok, what):
        print(f"  {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            failures.append(what)

    def store_files():
        return sorted(p.name for p in home.rglob("*.jsonl")) if home.exists() else []

    def run(cwd, name):
        nonlocal spend
        events, err, rc = session(cwd, env, name, args.budget)
        cost = sum(e.get("total_cost_usd") or 0 for e in events if e.get("type") == "result")
        spend += cost
        print(f"- {name}: exit {rc}, cost ${cost:.4f}, stderr {err.strip()[:200]!r}")
        return events

    try:
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env, timeout=60)
        (repo / "a.txt").write_text("one\n")
        (repo / ".gitignore").write_text(".claude/worktrees/\n")
        git("add", ".", cwd=repo, env=env)
        git("commit", "-q", "-m", "init", cwd=repo, env=env)
        git("worktree", "add", "-q", "-b", "worktree-proof-task", str(wt), cwd=repo, env=env)
        (repo / "a.txt").write_text("two\n")
        (wt / "a.txt").write_text("two\n")

        print("outside: a session in the scratch repository that no task declares")
        events = run(repo, f"proof-outside-{tmp.name[-6:]}")
        check(reviewer_ran(events), "the reviewer ran")
        check(store_files() == [], f"the scratch store holds no jsonl file (has {store_files()})")

        print("control: the task declared, the session in its worktree")
        factory.log("tasks", {**factory.record(str(repo), "proof-task", "dispatcher", None, None), "state": "working"})
        events = run(wt, f"proof-task-{tmp.name[-6:]}")
        check(reviewer_ran(events), "the reviewer ran")
        for event in ("SubagentStart", "SubagentStop"):
            found = [r for r in factory.read_log("events") if r.get("event") == event and r.get("role") == "reviewer" and r.get("slug") == "proof-task"]
            check(len(found) == 1, f"one {event} record with role reviewer and slug proof-task ({len(found)})")

        print(f"total cost ${spend:.4f}")
        print("PASS" if not failures else "FAIL")
        return 1 if failures else 0
    finally:
        left = glob.glob(str(PROJECTS / f"*{tmp.name}*"))
        size = sum(f.stat().st_size for root in [tmp, *map(Path, left)] for f in ([root] if root.is_file() else root.rglob("*")) if f.is_file())
        if args.keep:
            print(f"kept {tmp} and {len(left)} transcript paths, {size} bytes")
        else:
            for p in left:
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.unlink(p)
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"deleted the scratch directory and {len(left)} transcript paths, {size} bytes")


if __name__ == "__main__":
    sys.exit(main())
