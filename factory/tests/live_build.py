#!/usr/bin/env python3
"""Proof that a build run's lander declares no state. Run by hand, never by unittest (the name does not start with
`test`). It starts a real tech lead (Opus, with coder, reviewer and lander subagents) and COSTS MONEY: a few USD, at most
the build's 15 USD budget.

    python3 factory/tests/live_build.py [--keep] [--factory PATH]

Everything happens in a temp dir: a bare origin, a checkout, a scratch FACTORY_HOME (the script refuses to run against the
real store) and the `gh` shim of live_land.py extended with `pr create`. A task worktree gets a one-line spec, and
`factory build <slug> --run` runs synchronously. It asserts only on what the script owns: the build is `built` with its
hand-over file; the origin holds the task branch at the worktree's HEAD and the shim saw one `pr create --draft`; every
state record of the task has role `tech-lead` (the lander declared nothing as the lead); the scratch store holds no
`PermissionDenied` hook record (the hooks of the nested sessions write to FACTORY_HOME, which they inherit)."""
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

import live_land as L

SLUG = "add-greeting"
SPEC = ("# add-greeting\n\nSmall task. Append the line `hello` to `a.txt` and commit it. Test command: `none` (there is "
        "nothing to run). No other change. The base is `main`, the remote `origin`.\n")
SHIM = L.SHIM.replace('elif cmd in ("pr edit", "pr comment", "pr ready"):', 'elif cmd in ("pr edit", "pr comment", "pr ready", "pr create"):') \
             .replace("    if not p:\n        sys.exit(1)", "    if not p:\n        sys.exit(print('no pull requests found for branch', file=sys.stderr) or 1)") \
             .replace('note(cmd.replace(" ", "_"), {"body": open(path).read()} if path else None)',
                      'note(cmd.replace(" ", "_"), {"body": open(path).read()} if path else None)\n'
                      '    if cmd == "pr create":\n        print(f"https://example.test/pull/{subprocess.run([\'git\', \'branch\', \'--show-current\'], capture_output=True, text=True).stdout.strip()}")')


def monthly(home, name):
    """The records of the store's `<name>-YYYY-MM.jsonl` files."""
    return [r for f in sorted(Path(home).glob(f"{name}-*.jsonl")) for r in L.jsonl(f.parent, f.stem)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--factory", default=str(L.HERE.parent / "bin" / "factory"), help="the factory script")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir and print its path")
    args = ap.parse_args()
    if not shutil.which("claude"):
        sys.exit("needs `claude` on PATH")
    real = Path(os.environ.get("FACTORY_HOME") or "~/.claude/factory").expanduser().resolve()
    tmp = tempfile.mkdtemp(prefix="live-build-")
    try:
        L.SHIM = SHIM
        lab = L.Lab(tmp, os.path.abspath(args.factory))
        if Path(lab.home).resolve() == real:
            sys.exit("FACTORY_HOME is the real store")
        L.say(f"build run of {SLUG} (scratch store {lab.home})")
        tree = lab.tree(SLUG)
        lab.git("worktree", "add", "-q", "-b", f"worktree-{SLUG}", tree, "main")
        spec = lab.home / "specs" / Path(lab.checkout).name / f"{SLUG}.md"
        spec.parent.mkdir(parents=True)
        spec.write_text(SPEC)
        L.check(lab.run_factory("commands", "test", "none", cwd=tree).returncode == 0, "factory commands test none exits 0")
        p = lab.run_factory("build", SLUG, "--run", cwd=tree, timeout=5400 + 60)
        L.say("    " + (p.stdout + p.stderr).strip().replace("\n", "\n    "))
        records = [r for r in monthly(lab.home, "techlead") if r.get("slug") == SLUG]
        L.check(p.returncode == 0 and [r.get("outcome") for r in records] == ["built"], f"the build record says built: {[r.get('outcome') for r in records]}")
        L.check(spec.with_suffix(".handover.md").exists(), "the hand-over file exists")
        head = lab.git("rev-parse", "HEAD", cwd=tree)
        L.check(lab.git("rev-parse", f"worktree-{SLUG}", cwd=lab.origin) == head, "the origin holds the task branch at the worktree's HEAD")
        creates = [c["argv"] for c in L.jsonl(lab.state, "pr_create")]
        L.check(len(creates) == 1 and "--draft" in creates[0], f"the gh shim saw one `pr create --draft`: {creates}")
        roles = [r.get("role") for r in monthly(lab.home, "tasks") if r.get("slug") == SLUG]
        L.check(roles and set(roles) == {"tech-lead"}, f"every state record of the task has role tech-lead: {roles}")
        denied = [r for r in monthly(lab.home, "events") if r.get("event") == "PermissionDenied"]
        L.check(not denied, f"no PermissionDenied hook record in the scratch store: {denied}")
        L.check(any(r.get("event") == "SessionStart" for r in monthly(lab.home, "events")),
                "the nested sessions' hooks reached the scratch store (a SessionStart record is there)")
    finally:
        if args.keep:
            L.say(f"kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    L.say("FAILED: " + "; ".join(L.failures) if L.failures else "all checks passed")
    return 1 if L.failures else 0


if __name__ == "__main__":
    sys.exit(main())
