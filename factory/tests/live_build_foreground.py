#!/usr/bin/env python3
"""Paid proof that a build run waits in the foreground. Run by hand, never by unittest (the name does not start with
`test`):

    python3 factory/tests/live_build_foreground.py start <n> <dir> [--factory PATH]
    python3 factory/tests/live_build_foreground.py wait <dir> [seconds]

`start` sets up <n> runs under <dir>, each in its own scratch repository and FACTORY_HOME (a bare origin, a checkout, a
`gh` shim answering from local git), starts each `factory build` detached with the real model, and returns at once.
`wait` blocks until every run has ended or the seconds (default 540) pass: exit 3 while some still run. When all ended
it prints one line per run and the total cost, and exits 0 only if every run is `built` with no reason, started at
least two subagents (a coder and a reviewer) and has no `Stop` event of its session with background tasks."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import real_store

HERE = Path(__file__).resolve().parent
SLUG = "add-shout"
CHECKOUT = "repo"
STILL_RUNNING = 3

SHIM = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
S, ORIGIN = os.environ["LIVE_STATE"], os.environ["LIVE_ORIGIN"]
argv = sys.argv[1:]
cmd = " ".join(argv[:2])
rec = {"argv": argv}


def note(name, extra=None):
    open(f"{S}/{name}.jsonl", "a").write(json.dumps({**rec, **(extra or {})}) + "\n")


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


def pull(branch):
    head = subprocess.run(["git", "--git-dir", ORIGIN, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                          capture_output=True, text=True).stdout.strip()
    return {"state": "OPEN", "url": f"https://example.test/pull/{branch}", "headRefOid": head, "baseRefName": "main",
            "headRefName": branch, "isDraft": True, "mergeable": "MERGEABLE", "statusCheckRollup": [],
            "number": 1} if head else None


def branch_of(sel):
    if sel and not sel.startswith("-"):
        return sel.rsplit("/", 1)[-1]
    return subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True).stdout.strip()


if cmd == "pr list":
    p = pull(flag("--head"))
    print(json.dumps([p] if p else []))
elif cmd == "pr view":
    p = pull(branch_of(argv[2] if len(argv) > 2 else None))
    if not p:
        print("no pull requests found", file=sys.stderr)
        sys.exit(1)
    want = (flag("--json") or "").split(",")
    print(json.dumps({k: p[k] for k in want if k in p}))
elif cmd == "pr create":
    note("pr_create")
    print("https://example.test/pull/" + branch_of(None))
elif cmd in ("pr edit", "pr comment", "pr ready"):
    path = flag("--body-file")
    note(cmd.replace(" ", "_"), {"body": open(path).read()} if path else None)
else:
    note("other")
    print(f"gh shim: no such command: gh {' '.join(argv)}", file=sys.stderr)
    sys.exit(1)
'''

CLAUDE_SHIM = '''#!/usr/bin/env python3
import os, sys
if sys.argv[1:2] == ["agents"]:
    print("[]")
else:
    os.execv(os.environ["LIVE_REAL_CLAUDE"], [os.environ["LIVE_REAL_CLAUDE"], *sys.argv[1:]])
'''

NO_NOTIFICATION = "#!/bin/sh\nexit 0\n"

FILES = {".gitignore": ".claude/\n",
         "words.py": 'def title(text):\n    return text.title()\n',
         "test_words.py": 'import unittest\n\nimport words\n\n\nclass WordsTest(unittest.TestCase):\n'
                          '    def test_title(self):\n        self.assertEqual(words.title("a b"), "A B")\n\n\n'
                          'if __name__ == "__main__":\n    unittest.main()\n'}

SPEC = f"""# {SLUG}

Size: small. In `words.py` add `shout(text)`, which returns `text.upper() + "!"`. Add one test of it to `test_words.py`.
Nothing else changes. A coder builds it and a reviewer reviews it before the pull request. Test command:
`python3 -m unittest`.
"""


def sh(*argv, cwd, env):
    p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    if p.returncode:
        sys.exit(f"{' '.join(argv)} exited {p.returncode} in {cwd}:\n{p.stdout}{p.stderr}")


def set_up(run, factory):
    """The scratch repository, home and shims of one run; the task's worktree and spec. Returns (env, worktree)."""
    run.mkdir()
    home, state, bindir = (run / d for d in ("home", "state", "bin"))
    for d in (home, state, bindir):
        d.mkdir()
    real_store.refuse(home)
    gitconfig = run / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Staged\n\temail = staged@example.test\n[commit]\n\tgpgsign = false\n"
                         "[init]\n\tdefaultBranch = main\n")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
                                                             "CLAUDE_ENV_FILE", "FACTORY_ROLE", "FACTORY_SLUG")}
    origin, checkout = run / "origin.git", run / CHECKOUT
    env |= {"FACTORY_HOME": str(home), "LIVE_STATE": str(state), "LIVE_ORIGIN": str(origin), "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1", "LIVE_REAL_CLAUDE": shutil.which("claude") or "", "PATH": f"{bindir}{os.pathsep}{env['PATH']}"}
    for name, text in (("gh", SHIM), ("claude", CLAUDE_SHIM), ("osascript", NO_NOTIFICATION), ("notify-send", NO_NOTIFICATION)):
        (bindir / name).write_text(text)
        (bindir / name).chmod(0o755)
    sh("git", "init", "-q", "--bare", origin, cwd=run, env=env)
    sh("git", "clone", "-q", origin, checkout, cwd=run, env=env)
    for name, text in FILES.items():
        (checkout / name).write_text(text)
    sh("git", "add", "-A", cwd=checkout, env=env)
    sh("git", "commit", "-q", "-m", "a words module", cwd=checkout, env=env)
    sh("git", "push", "-q", "-u", "origin", "main", cwd=checkout, env=env)
    sh("git", "remote", "set-head", "origin", "main", cwd=checkout, env=env)
    worktree = checkout / ".claude" / "worktrees" / SLUG
    sh("git", "worktree", "add", "-q", "-b", f"worktree-{SLUG}", worktree, "main", cwd=checkout, env=env)
    (home / "specs" / CHECKOUT).mkdir(parents=True)
    (home / "specs" / CHECKOUT / f"{SLUG}.md").write_text(SPEC)
    return env, worktree


def start(args):
    if not shutil.which("claude"):
        sys.exit("needs `claude` on PATH")
    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for i in range(1, args.n + 1):
        run = root / f"run-{i}"
        env, worktree = set_up(run, os.path.abspath(args.factory))
        with open(run / "out.txt", "ab") as out:
            proc = subprocess.Popen([os.path.abspath(args.factory), "build", SLUG, "--run"], cwd=worktree, env=env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        (run / "pid").write_text(str(proc.pid))
    print(root)
    return 0


def alive(run):
    try:
        os.kill(int((run / "pid").read_text()), 0)
    except ProcessLookupError:
        return False
    return True


def rows(home, name):
    return [json.loads(l) for p in sorted(home.glob(f"{name}-*.jsonl")) for l in p.read_text().splitlines() if l.strip()]


def judge(run):
    """(the run's printed line, whether it passes, its cost)."""
    home = run / "home"
    records = [r for r in rows(home, "techlead") if r.get("kind") == "build"]
    if not records:
        return f"{run.name}: no build record (see {run / 'out.txt'})", False, 0
    rec = records[-1]
    events = [e for e in rows(home, "events") if e.get("session") == rec["session"]]
    starts = sum(e.get("event") == "SubagentStart" for e in events)
    tasks = [e.get("background_tasks") for e in events if e.get("event") == "Stop"]
    ok = rec["outcome"] == "built" and rec["reason"] is None
    line = f"{run.name}: outcome={rec['outcome']} reason={rec['reason']} cost_usd={rec['cost_usd']} seconds={rec['seconds']}"
    if events:
        line += f" subagent_starts={starts} background_tasks={tasks}"
        ok = ok and starts >= 2 and not any(t for t in tasks if t)
    else:
        line += " events not recorded"
    return line, ok, rec["cost_usd"] or 0


def wait(args):
    root = Path(args.dir)
    runs = sorted(root.glob("run-*"))
    deadline = time.monotonic() + args.seconds
    while any(alive(r) for r in runs) and time.monotonic() < deadline:
        time.sleep(5)
    if any(alive(r) for r in runs):
        print(f"{sum(alive(r) for r in runs)} of {len(runs)} runs still going")
        return STILL_RUNNING
    results = [judge(r) for r in runs]
    for line, _, _ in results:
        print(line)
    print(f"total cost_usd={sum(c for _, _, c in results):.2f}")
    return 0 if runs and all(ok for _, ok, _ in results) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start")
    s.add_argument("n", type=int)
    s.add_argument("dir")
    s.add_argument("--factory", default=str(HERE.parent / "bin" / "factory"), help="the factory script")
    w = sub.add_parser("wait")
    w.add_argument("dir")
    w.add_argument("seconds", nargs="?", type=int, default=540)
    args = ap.parse_args()
    return (start if args.command == "start" else wait)(args)


if __name__ == "__main__":
    sys.exit(main())
