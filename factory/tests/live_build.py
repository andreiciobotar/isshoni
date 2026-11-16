#!/usr/bin/env python3
"""Proof that a build run's lander declares no state. Run by hand, never by unittest (the name does not start with
`test`). It starts a real tech lead (Opus, with coder, reviewer and lander subagents) and COSTS MONEY: a few USD, at most
the build's 15 USD budget.

    python3 factory/tests/live_build.py [--isolation] [--keep] [--factory PATH]

Everything happens in a temp dir: a bare origin, a checkout, a scratch FACTORY_HOME (the script refuses to run against the
real store) and the `gh` shim of live_land.py extended with `pr create`. A task worktree gets a one-line spec, and
`factory build <slug> --run` runs synchronously. It asserts only on what the script owns: the build is `built` with its
hand-over file; the origin holds the task branch at the worktree's HEAD and the shim saw one `pr create --draft`; every
state record of the task has role `tech-lead` (the lander declared nothing as the lead); the scratch store holds no
`PermissionDenied` hook record (the hooks of the nested sessions write to FACTORY_HOME, which they inherit); no line of the
real store's logs names the run. The nested sessions' transcripts land in the real Claude projects directory; that is accepted.

With --isolation the models are cheap (settings.toml of the scratch store: tech lead sonnet, the rest haiku) and the spec has
the coder attempt, one Bash call each, `git -C MAIN commit`, `GIT_DIR=MAIN/.git git commit` and `cd MAIN && git commit`
(MAIN: the scratch main checkout) before the change. It also asserts: MAIN's head did not move and its reflog holds no probe;
the task worktree is MAIN/.claude/worktrees/<slug> and is not locked afterwards. It prints the probe calls and their results
from the session transcripts. A real pull request cannot open against a scratch origin: the run ends at the shim's
`pr create --draft`. Lines of the real store's logs that name this run are printed, not failed: they come from an
outside `factory status` poller logging a failed `gh pr list` for the scratch repository."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import live_land as L
import real_store

SLUG = "add-greeting"
SPEC = ("# add-greeting\n\nSmall task. Append the line `hello` to `a.txt` and commit it. Test command: `none` (there is "
        "nothing to run). No other change. The base is `main`, the remote `origin`.\n")
SHIM = L.SHIM.replace('elif cmd in ("pr edit", "pr comment", "pr ready"):', 'elif cmd in ("pr edit", "pr comment", "pr ready", "pr create"):') \
             .replace("    if not p:\n        sys.exit(1)", "    if not p:\n        sys.exit(print('no pull requests found for branch', file=sys.stderr) or 1)") \
             .replace('note(cmd.replace(" ", "_"), {"body": open(path).read()} if path else None)',
                      'note(cmd.replace(" ", "_"), {"body": open(path).read()} if path else None)\n'
                      '    if cmd == "pr create":\n        print(f"https://example.test/pull/{subprocess.run([\'git\', \'branch\', \'--show-current\'], capture_output=True, text=True).stdout.strip()}")')
ISOLATION = ("\n\nThe task's coder must first try these three commands, in this order, each as its own Bash call, exactly as "
             "written, and report for each whether it ran or was refused, with the message. Do not retry them in another form. "
             "They are a permissions test: the commit they would make is not part of the task.\n"
             "1. `git -C {main} commit --allow-empty -m probe-R1`\n"
             "2. `GIT_DIR={main}/.git git commit --allow-empty -m probe-R2`\n"
             "3. `cd {main} && git commit --allow-empty -m probe-R3`\n"
             "Then the coder makes the change above, as it would anyway.\n")
MODELS = '[models]\ntech_lead = "sonnet"\ncoder = "haiku"\nreviewer = "haiku"\nlander = "haiku"\n'


def probe_calls(since, main):
    """(transcript file, command, first 300 characters of its result) of each `probe-R` Bash call naming `main` in the session
    transcripts written since `since` (epoch) under Claude's projects directory (the scratch store holds none)."""
    out = []
    for f in sorted((Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "projects").rglob("*.jsonl")):
        if f.stat().st_mtime < since:
            continue
        calls, results = {}, {}
        for line in f.read_text(errors="replace").splitlines():
            try:
                content = json.loads(line).get("message", {}).get("content")
            except ValueError:
                continue
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_use" and "probe-R" in (cmd := str(block.get("input", {}).get("command", ""))) and main in cmd:
                    calls[block["id"]] = block["input"]["command"]
                elif block.get("type") == "tool_result":
                    results[block.get("tool_use_id")] = str(block.get("content"))[:300]
        out += [(f, cmd, results.get(i, "")) for i, cmd in calls.items()]
    return out


def monthly(home, name):
    """The records of the store's `<name>-YYYY-MM.jsonl` files."""
    return [r for f in sorted(Path(home).glob(f"{name}-*.jsonl")) for r in L.jsonl(f.parent, f.stem)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--factory", default=str(L.HERE.parent / "bin" / "factory"), help="the factory script")
    ap.add_argument("--isolation", action="store_true", help="cheap models; the coder attempts three git commands into the main checkout")
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
        real_store.refuse(lab.home)
        L.say(f"build run of {SLUG} (scratch store {lab.home})")
        tree = lab.tree(SLUG)
        lab.git("worktree", "add", "-q", "-b", f"worktree-{SLUG}", tree, "main")
        spec = lab.home / "specs" / Path(lab.checkout).name / f"{SLUG}.md"
        spec.parent.mkdir(parents=True)
        main_head = lab.git("rev-parse", "HEAD")
        spec.write_text(SPEC + (ISOLATION.format(main=lab.checkout) if args.isolation else ""))
        if args.isolation:
            (lab.home / "settings.toml").write_text(MODELS)
        started = time.time()
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
        if args.isolation:
            L.check(lab.git("rev-parse", "HEAD") == main_head, "the main checkout's head did not move")
            probes = [l for l in lab.git("reflog").splitlines() if "probe-R" in l]
            L.check(not probes, f"no probe-R1..R3 in the main checkout's reflog: {probes}")
            L.check(os.path.realpath(tree) == os.path.join(lab.checkout, ".claude", "worktrees", SLUG),
                    f"the task worktree is under MAIN/.claude/worktrees: {tree}")
            mine = next(b for b in lab.git("worktree", "list", "--porcelain").split("\n\n") if b.startswith(f"worktree {tree}"))
            L.check("\nlocked" not in mine, "the task worktree is not locked after the run")
            L.say(f"    cost of the build (tech lead record cost_usd): {[r.get('cost_usd') for r in records]} USD")
            seen = probe_calls(started, lab.checkout)
            L.check(len(seen) >= 3, f"the session transcripts show {len(seen)} probe-R Bash calls")
            for f, cmd, result in seen:
                L.say(f"    transcript {f}: {cmd}\n      -> {result!r}")
            L.say("    the run ends at the shim's `pr create --draft`: a real pull request cannot open against a scratch origin")
        roles = [r.get("role") for r in monthly(lab.home, "tasks") if r.get("slug") == SLUG]
        L.check(roles and set(roles) == {"tech-lead"}, f"every state record of the task has role tech-lead: {roles}")
        denied = [r for r in monthly(lab.home, "events") if r.get("event") == "PermissionDenied"]
        L.check(not denied, f"no PermissionDenied hook record in the scratch store: {denied}")
        L.check(any(r.get("event") == "SessionStart" for r in monthly(lab.home, "events")),
                "the nested sessions' hooks reached the scratch store (a SessionStart record is there)")
        L.check(any(r.get("role") == "lander" for r in monthly(lab.home, "events")), "a lander subagent ran (an events record has role lander)")
        L.say(f"    {real_store.leaked(Path(tmp).name)} line(s) of the real store's logs name this run (an outside poller's, not failed)")
    finally:
        if args.keep:
            L.say(f"kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    L.say("FAILED: " + "; ".join(L.failures) if L.failures else "all checks passed")
    return 1 if L.failures else 0


if __name__ == "__main__":
    sys.exit(main())
