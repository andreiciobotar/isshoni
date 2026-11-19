#!/usr/bin/env python3
"""Proof of what `factory lander` does with a stop it already reported (#220) and with the stops it words (#229, #220). Run by
hand, never by unittest (the name does not start with `test`). It costs nothing and touches no live store:

    python3 factory/tests/live_lander_stops.py [--before PATH] [--keep] [--seconds N] [--factory PATH]

Per scenario, a scratch directory holds a bare origin, a checkout with one handed-over task, FACTORY_HOME (never the live
store) and a `gh` shim that replays what GitHub answered in the incidents: `gh pr checks` exits 1 with `no checks reported`,
the rules endpoints answer 0 or one required check, a reviews read exits 1 printing `[]`. The shim logs every call; a `claude`
shim lists a dispatcher named `scratch-dispatcher` and records each line a courier is asked to send. The real `factory lander`
runs against it.

1. `reported`: a pull request with no checks whose `ci` stop is already in lander.json `stopped` at its head; `factory lander`
   runs bounded at --seconds. At 34f857f3 it never ends (`gh pr checks` every 20 s); now it makes one pass and ends.
   --before PATH runs the same scenario with the factory script of another tree (a worktree of 34f857f3) and shows its count.
2. `no-ci`: no checks reported, no merge rule requires a status check, past its 180 s of patience (seeded): the stop line, one
   comment on the pull request, one line to the dispatcher, nothing on the second run.
3. `no approval read`: the reviews read exits 1 printing `[]` (a `pending` stop already past its 1800 s): the stop line says
   the read failed; it never says `not approved`.

Nothing is deleted: the scratch root is kept and printed."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import live_land as L

DISPATCHER = "scratch-dispatcher"

GH_SHIM = r'''#!/usr/bin/env python3
import json, os, sys, time
S = os.environ["LIVE_STATE"]
argv = sys.argv[1:]
scenario = json.load(open(f"{S}/scenario.json"))
open(f"{S}/gh.jsonl", "a").write(json.dumps({"ts": time.time(), "argv": argv}) + "\n")
branch_of = {b: n for b, n in scenario["prs"].items()}


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


def pull(branch):
    import subprocess
    head = subprocess.run(["git", "--git-dir", os.environ["LIVE_ORIGIN"], "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                          capture_output=True, text=True).stdout.strip()
    if not head or branch not in branch_of:
        return None
    n = branch_of[branch]
    return {"state": "OPEN", "url": f"https://example.test/pull/{n}", "headRefOid": head, "headRefName": branch,
            "baseRefName": "main", "isDraft": False, "isCrossRepository": False, "number": n,
            "author": {"login": "andrei", "is_bot": False}, "mergeable": "MERGEABLE", "statusCheckRollup": [],
            "latestReviews": [{"state": "APPROVED", "submittedAt": "2026-11-19T10:00:00Z"}], "reviews": [], "comments": []}


def number(i):
    return int(argv[i])


cmd = " ".join(argv[:2])
if cmd == "pr list":
    if "--head" in argv:
        p = pull(flag("--head"))
        print(json.dumps([p] if p else []))
    else:
        print(json.dumps([p for p in map(pull, branch_of) if p]))
elif cmd == "pr checks":
    if scenario["checks"] == "none":
        print(f"no checks reported on the 'worktree-x' branch", file=sys.stderr)
        sys.exit(1)
    print(json.dumps([{"name": "test", "bucket": "pass"}]))
elif cmd == "pr comment":
    body = open(flag("--body-file")).read() if "--body-file" in argv else flag("--body")
    open(f"{S}/comments.jsonl", "a").write(json.dumps({"pr": number(2), "body": body}) + "\n")
elif cmd == "pr view" and (argv[2].startswith("http") or "mergeable" not in (flag("--json") or "")):
    p = next((p for p in map(pull, branch_of) if str(p["number"]) == argv[2] or p["url"] == argv[2]), None)
    if not p:
        sys.exit(1)
    print(json.dumps({k: p[k] for k in (flag("--json") or "").split(",") if k in p}))
elif cmd == "pr view":
    print(json.dumps({"mergeable": "MERGEABLE"}))
elif argv[:1] == ["api"] and argv[-1].endswith("rules/branches/main"):
    print("[]")
elif argv[:1] == ["api"] and argv[-1].endswith("branches/main"):
    print(json.dumps({"protection": {"enabled": True, "required_status_checks": {"contexts": ["test"] * scenario["required"], "checks": []}}}))
elif argv[:1] == ["api"] and argv[-1].endswith("/reviews"):
    if scenario["reviews_fail"]:
        print("[]")
        print("unexpected end of JSON input", file=sys.stderr)
        sys.exit(1)
    n = argv[-1].split("/")[-2]
    head = next(p["headRefOid"] for p in map(pull, branch_of) if str(p["number"]) == n)
    print(json.dumps([[{"state": "APPROVED", "submitted_at": "2026-11-19T10:00:00Z", "commit_id": head,
                        "html_url": "https://example.test/r", "user": {"login": "dev", "type": "User"}}]]))
elif argv[:1] == ["api"] and argv[-1].endswith("/permission"):
    print(json.dumps({"permission": "write"}))
elif argv[:1] == ["api"] and "--slurp" in argv:
    print("[[]]")
elif argv[:1] == ["api"] and "stacks" in argv[-1]:
    print("[]")
else:
    open(f"{S}/other.jsonl", "a").write(json.dumps(argv) + "\n")
    print(f"gh shim: no such command: gh {' '.join(argv)}", file=sys.stderr)
    sys.exit(1)
'''

CLAUDE_SHIM = f'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
if argv[:1] == ["agents"]:
    print(json.dumps([{{"sessionId": "sid-d", "name": "{DISPATCHER}", "cwd": os.environ["LIVE_CHECKOUT"], "kind": "interactive", "pid": 4242}}]))
elif "-p" in argv:
    open(os.environ["LIVE_STATE"] + "/courier.jsonl", "a").write(json.dumps(argv[-1]) + "\\n")
    print(json.dumps({{"result": "sent"}}))
else:
    sys.exit(1)
'''

failures = []


def say(text=""):
    print(text, flush=True)


def check(ok, what):
    say(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def lab_for(root, name, factory, *, checks, required, reviews_fail=False):
    """A scratch lab with one handed-over task whose pull request is #1; the shim's answers as the scenario says."""
    tmp = Path(root, name)
    tmp.mkdir(parents=True)
    L.SHIM, L.CLAUDE_SHIM = GH_SHIM, CLAUDE_SHIM
    lab = L.Lab(str(tmp), factory)
    lab.env["LIVE_CHECKOUT"] = lab.checkout
    Path(lab.state, "scenario.json").write_text(json.dumps({"prs": {"worktree-t-one": 1}, "checks": checks, "required": required,
                                                            "reviews_fail": reviews_fail}))
    return lab


def calls(lab, *prefix):
    path = Path(lab.state, "gh.jsonl")
    seen = [json.loads(l)["argv"] for l in path.read_text().splitlines()] if path.exists() else []
    return [a for a in seen if a[:len(prefix)] == list(prefix)]


def seed(lab, kind, key, record):
    """Write lander.json (`repo_state(root, "lander", "json")`) as a real wait or a real report leaves it."""
    name = f"lander-{os.path.basename(lab.checkout)}-{hashlib.sha256(lab.checkout.encode()).hexdigest()[:8]}.json"
    path = lab.home / name
    state = json.loads(path.read_text()) if path.exists() else {}
    state.setdefault(kind, {})[key] = record
    path.write_text(json.dumps(state))


def task(lab):
    tree = lab.task("t-one", "a.txt", "line 20", "task line")
    head = lab.git("rev-parse", "HEAD", cwd=tree)
    return head


def lander(lab, seconds=None):
    """(exit code or 'timed out', stdout) of `factory lander`, killed after `seconds`."""
    try:
        p = lab.run_factory("lander", "--dispatcher", DISPATCHER, timeout=seconds or 120)
    except subprocess.TimeoutExpired as e:
        return "timed out", (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    return p.returncode, p.stdout


def lines(lab, name):
    path = Path(lab.state, f"{name}.jsonl")
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def reported(root, factory, tag, seconds):
    """#220: the `ci` stop of a pull request with no checks is already reported at its head; one drain, bounded."""
    lab = lab_for(root, f"reported-{tag}", factory, checks="none", required=1)
    head = task(lab)
    seed(lab, "stopped", "1", {"head": head, "kind": "ci", "ts": "2026-11-19T10:00:00Z",
                               "text": "no checks reported on its head, and main requires a status check: nothing has started them on this head"})
    started = time.time()
    code, out = lander(lab, seconds)
    took = time.time() - started
    n = len(calls(lab, "pr", "checks", "1"))
    say(f"  [{tag}] factory lander: {code} after {took:.0f} s; `gh pr checks 1` called {n} times (shim log); output: {out.strip()!r}")
    return code, n, lab


def no_ci(root, factory):
    lab = lab_for(root, "no-ci", factory, checks="none", required=0)
    head = task(lab)
    seed(lab, "waiting", "1", {"head": head, "kind": "no-ci", "since": time.time() - 181})  # its 180 s of patience are over
    code, out = lander(lab)
    stops = [l for l in out.splitlines() if " stopped " in l]
    say(f"  first run: exit {code}")
    for l in stops:
        say(f"    {l}")
    comments = lines(lab, "comments")
    courier = lines(lab, "courier")
    check(len(stops) == 1 and stops[0].startswith("#1 stopped no-ci: no check is reported on it and main requires no status check, "
                                                  "so the factory does not merge it: merge it by hand, or add a required check to main"),
          "one `no-ci` stop line with the exact reason")
    check(len(comments) == 1 and comments[0]["body"].startswith("Merge queue: no check is reported on it"), f"one pull request comment: {comments}")
    check(len(courier) == 1 and "#1 stopped no-ci:" in courier[0], "one line to the dispatcher")
    rules = calls(lab, "api", "repos/{owner}/{repo}/branches/main"), calls(lab, "api", "repos/{owner}/{repo}/rules/branches/main")
    say(f"  rules reads in the first run: branches/main {len(rules[0])}, rules/branches/main {len(rules[1])}; `gh pr checks 1`: {len(calls(lab, 'pr', 'checks', '1'))}")
    code, out = lander(lab, 60)
    say(f"  second run: exit {code}, output {out.strip()!r}; `gh pr checks 1` total {len(calls(lab, 'pr', 'checks', '1'))}")
    check(code in (0, 1) and " stopped " not in out and len(lines(lab, "comments")) == 1 and len(lines(lab, "courier")) == 1,
          "the second run ends by itself and tells nobody again")
    return stops


def no_approval_read(root, factory, tag):
    lab = lab_for(root, f"approval-{tag}", factory, checks="green", required=1, reviews_fail=True)
    head = task(lab)
    seed(lab, "waiting", "1", {"head": head, "kind": "pending", "since": time.time() - 1801})
    code, out = lander(lab, 90)
    stops = [l for l in out.splitlines() if " stopped " in l]
    say(f"  [{tag}] exit {code}")
    for l in stops:
        say(f"    {l}")
    return stops


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--factory", default=str(L.HERE.parent / "bin" / "factory"), help="the factory script")
    ap.add_argument("--before", help="the factory script of a tree at 34f857f3: runs the first scenario with it too")
    ap.add_argument("--seconds", type=int, default=70, help="the bound of a drain that must not end by itself (default 70)")
    ap.add_argument("--keep", action="store_true", help="accepted for symmetry: the scratch root is always kept")
    args = ap.parse_args()
    root = tempfile.mkdtemp(prefix="live-lander-stops-")
    say(f"scratch root {root}")
    factory = os.path.abspath(args.factory)
    say("1. a no-checks stop already reported at its head: one drain, bounded at %d s" % args.seconds)
    if args.before:
        code, n, _ = reported(root, os.path.abspath(args.before), "before", args.seconds)
        check(code == "timed out" and n >= 3, f"before: the loop does not end by itself and asks {n} times")
    code, n, _ = reported(root, factory, "after", args.seconds)
    check(code in (0, 1) and n == 1, f"after: one pass ends it, with {n} read of the checks")
    say("2. no checks reported, the default branch requires none")
    no_ci(root, factory)
    say("3. the reviews read exits 1 printing `[]`, a `pending` stop past its 1800 s")
    if args.before:
        before = no_approval_read(root, os.path.abspath(args.before), "before")
        say(f"  before says: {before}")
    after = no_approval_read(root, factory, "after")
    check(len(after) == 1 and "#1 stopped pending: could not read its approval on GitHub: " in after[0]
          and "unexpected end of JSON input" in after[0] and "not approved" not in after[0], "after: the stop says the read failed")
    size = subprocess.run(["du", "-sh", root], capture_output=True, text=True).stdout.split()[0]
    say(f"scratch root {root} ({size}), kept")
    say("FAILED: " + "; ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
