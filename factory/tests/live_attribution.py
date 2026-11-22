#!/usr/bin/env python3
"""Paid proof that the factory's sessions leave no harness attribution. Run by hand, never by unittest (the name does not
start with `test`). It runs a real build (Opus, with its subagents) and COSTS MONEY: a few USD, at most the build's 15 USD
budget.

    python3 factory/tests/live_attribution.py [--control] [--keep] [--factory PATH]

Everything happens in a temp dir, as in live_build.py (a bare origin, a checkout, a scratch FACTORY_HOME the script refuses
to leave, the `gh` shim of live_build.py): a task worktree with a one-line spec, and `factory build <slug> --run` runs
synchronously. The session commits and calls `gh pr create` on the shim. The script asserts that no commit message on the
task branch and no pull request body or argument the shim saw matches `co-authored-by` or `generated with` (case
insensitive), and that the commits and the pull request exist (an empty search proves nothing). The `claude` on PATH is a
shim that records each `--settings` value and starts the real one. With --control it first removes the `attribution` key
from that value, so the harness default is on: the lines are then expected to appear, which shows the assertion can fail;
the run then exits 0 when they do, and 1 when they do not."""
import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import live_build as B
import live_land as L
import real_store

SLUG = "add-greeting"
LINES = re.compile(r"co-authored-by|generated with", re.I)
CLAUDE_SHIM = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
if argv[:1] == ["agents"]:
    print("[]")
    sys.exit(0)
if "--settings" in argv:
    at = argv.index("--settings") + 1
    settings = json.loads(argv[at])
    open(os.environ["LIVE_STATE"] + "/settings.jsonl", "a").write(json.dumps(settings) + "\\n")
    if os.environ.get("LIVE_CONTROL"):
        settings.pop("attribution", None)
        argv[at] = json.dumps(settings)
os.execv(os.environ["LIVE_REAL_CLAUDE"], [os.environ["LIVE_REAL_CLAUDE"], *argv])
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--factory", default=str(L.HERE.parent / "bin" / "factory"), help="the factory script")
    ap.add_argument("--control", action="store_true", help="remove the attribution key before the real claude starts; expect the lines")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir and print its path")
    args = ap.parse_args()
    if not shutil.which("claude"):
        sys.exit("needs `claude` on PATH")
    tmp = tempfile.mkdtemp(prefix="live-attribution-")
    try:
        L.SHIM, L.CLAUDE_SHIM = B.SHIM, CLAUDE_SHIM
        lab = L.Lab(tmp, os.path.abspath(args.factory))
        real_store.refuse(str(lab.home))
        if args.control:
            lab.env["LIVE_CONTROL"] = "1"
        L.say(f"build run of {SLUG}, {'CONTROL (attribution key removed)' if args.control else 'attribution off'} (scratch store {lab.home})")
        tree = lab.tree(SLUG)
        lab.git("worktree", "add", "-q", "-b", f"worktree-{SLUG}", tree, "main")
        spec = lab.home / "specs" / Path(lab.checkout).name / f"{SLUG}.md"
        spec.parent.mkdir(parents=True)
        spec.write_text(B.SPEC)
        p = lab.run_factory("build", SLUG, "--run", cwd=tree, timeout=5400 + 60)
        L.say("    " + (p.stdout + p.stderr).strip().replace("\n", "\n    "))
        records = [r for r in B.monthly(lab.home, "techlead") if r.get("slug") == SLUG]
        L.say(f"build records: {[(r.get('outcome'), r.get('cost_usd')) for r in records]}")

        messages = lab.git("log", f"main..worktree-{SLUG}", "--format=%B", cwd=tree)
        creates = L.jsonl(lab.state, "pr_create")
        bodies = [" ".join(c["argv"]) + "\n" + (c.get("body") or "") for c in creates]
        found = [m.group(0) for m in LINES.finditer("\n".join([messages, *bodies]))]
        sessions = L.jsonl(lab.state, "settings")
        L.say("commit messages on the branch:\n    " + (messages.replace("\n", "\n    ") or "(none)"))
        L.say(f"pull request calls the shim saw: {len(creates)}")
        for b in bodies:
            L.say("    " + b.replace("\n", "\n    "))
        L.say(f"`--settings` values the claude shim saw: {len(sessions)}; with the attribution key: {sum('attribution' in s for s in sessions)}")
        L.check(bool(messages.strip()), "the branch has at least one commit")
        L.check(len(creates) >= 1, "the shim saw a `gh pr create`")
        if args.control:
            L.say(f"matches for co-authored-by / generated with: {found or 'none'}")
            L.say("control: the lines " + ("APPEAR" if found else "DO NOT appear"))
            return 0 if found else 1
        L.check(bool(sessions) and all(s.get("attribution") == {"commit": "", "pr": "", "sessionUrl": False} for s in sessions),
                "every claude the factory started got attribution off")
        L.check(not found, f"no co-authored-by or generated with line in a commit or pull request body: {found or 'none'}")
    finally:
        if args.keep:
            L.say(f"kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    L.check(real_store.leaked(os.path.basename(tmp)) == 0, f"no line of the real store's logs names {os.path.basename(tmp)}")
    L.say("FAILED: " + "; ".join(L.failures) if L.failures else "all checks passed")
    return 1 if L.failures else 0


if __name__ == "__main__":
    sys.exit(main())
