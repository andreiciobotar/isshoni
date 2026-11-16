#!/usr/bin/env python3
"""Live proof of the ops watch. Run by hand, never by unittest; it costs real money (a few headless `claude -p` runs).

    python3 factory/tests/live_ops_watch.py --model sonnet

`--model` is `models.ops_watch` of a scratch FACTORY_HOME (try sonnet, then haiku). The scratch directory holds a scratch
repository whose `.mcp.json` names `ops_proof_server.py`, a stdio MCP server with one read tool that answers
{"served": 10*n, "errors": 0}, n counting its calls. The watch is started through the CLI (`main(["ops", "start", ...])`, with
CLAUDECODE unset) with `--every 5s`, then the real `factory watch` runs in the scratch repository until the watch ends.
The scheduler is a fake (no job is installed on this machine) and desktop notifications are off.

Checks, all on files the script owns: at least 3 look records with evidence; an evidence file with a non-empty response per look;
at least 3 calls in the server's own log; `watch.json` ended with verdict met; the notes hold the checks and at least 3
entries; the real store names nothing of the scratch directory. It prints each look's outcome, cost, seconds, turns and
evidence count. Exit 1 on any failure. Refuses a scratch home under the real ~/.claude; the scratch directory stays."""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import real_store

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--model", required=True, help="models.ops_watch for the run")
ap.add_argument("--budget", default="1.5", help="the watch's budget in dollars")
ap.add_argument("--wait", type=int, default=900, help="seconds to let the watch run before giving up")
args = ap.parse_args()

TMP = Path(tempfile.mkdtemp(prefix="ops-watch-live-")).resolve()
os.environ["FACTORY_HOME"] = str(TMP / "home")  # before the script reads it
os.environ.pop("CLAUDECODE", None)  # the human's terminal: `ops start` refuses a session
os.environ.pop("FACTORY_ROLE", None)
real_store.refuse(os.environ["FACTORY_HOME"])
CHECKOUT = Path(__file__).resolve().parent.parent.parent
SERVER = Path(__file__).resolve().parent / "ops_proof_server.py"
_loader = importlib.machinery.SourceFileLoader("factory_bin", str(CHECKOUT / "factory" / "bin" / "factory"))
factory = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_bin", _loader))
_loader.exec_module(factory)
factory = getattr(factory, "factory_core", factory)

failures, claude_argvs = [], []
GOAL = ("Zero failed requests. Read the proof server's read_served tool exactly once per look. Done once served reaches 30: "
        "verdict met if no errors were seen, not-met if there were.")


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


class FakeScheduler:
    """No job is installed on this machine."""
    name = "fake"
    installed = []

    def status(self, label):
        return bool(self.installed), bool(self.installed)

    def install(self, label, argv, cwd, env, log):
        self.installed.append(label)

    def remove(self, label, root):
        return False


def noted_run(argv, *a, **kw):
    if argv[:2] == ["claude", "-p"]:
        claude_argvs.append(list(argv))
    return real_run(argv, *a, **kw)


def setup():
    repo, home, state = TMP / "repo", TMP / "home", TMP / "state"
    for d in (repo, home, state):
        d.mkdir()
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "scratch"]):
        subprocess.run(cmd, cwd=repo, check=True)
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"proof": {"command": sys.executable, "args": [str(SERVER), str(state)]}}}))
    (repo / "CLAUDE.md").write_text("To read production, call the `read_served` tool of the `proof` MCP server (no arguments). "
                                    "It returns the requests served so far and how many failed.\n")
    (home / "settings.toml").write_text(f'[models]\nops_watch = "{args.model}"\n[ops]\ntools = ["mcp__proof__read_served"]\n')
    os.chdir(repo)
    return repo, home, state


def main():
    global real_run
    repo, home, state = setup()
    real_run = factory.run
    factory.run, factory.scheduler, factory.notify_argv = noted_run, FakeScheduler, lambda *_: None
    factory.scheduler = lambda: FakeScheduler()
    factory.review_tick = lambda *_: None  # the proof copy of the watch is not to start a review
    print(f"scratch: {TMP}\nmodel: {args.model}")
    code = factory.main(["ops", "start", "proof", GOAL, "--every", "5s", "--until", "30m", "--budget", args.budget])
    print(f"ops start: exit {code}")
    stop, ticks = time.time() + args.wait, 0
    while time.time() < stop:
        ticks += 1
        factory.main(["watch"])
        state_now = json.loads((home / "ops" / "proof" / "watch.json").read_text())["state"]
        if state_now not in factory.OPS_ACTIVE:
            break
        time.sleep(6)
    w = json.loads((home / "ops" / "proof" / "watch.json").read_text())
    records = [r for r in factory.read_log("ops") if r.get("watch") == "proof" and "tick" in r]
    print(f"\n`factory watch` ran {ticks} time(s) in the scratch repository; the watch is {w['state']} ({w['verdict']}), "
          f"spent ${w['spent_usd']:.4f} of ${w['budget_usd']:.2f}, {w['unpriced_ticks']} unpriced, {w['missed']} missed")
    print("look  outcome         cost_usd  seconds  turns  evidence  late")
    for r in records:
        cost = "unpriced" if r["cost_usd"] is None else f"{r['cost_usd']:.4f}"
        print(f"{r['tick']:<5} {r['outcome']:<15} {cost:<9} {r['seconds']:<8} {r['turns']!s:<6} {r['evidence']:<9} {r['late_seconds']}")
        print(f"      saw: {r['saw']}")
    if claude_argvs:
        print(f"SETTINGS_ISOLATION {list(factory.SETTINGS_ISOLATION)} in the argv: {'--setting-sources' in claude_argvs[0]}")
    print("checks:")
    looked = [r for r in records if r["evidence"] > 0]
    check(len(looked) >= 3, f"at least 3 look records with evidence ({len(looked)})")
    filled = all(any(json.loads(f.read_text()).get("response") for f in (home / "deliverables" / f"session-{r['session']}" / "evidence").glob("*.json"))
                 for r in looked)
    check(bool(looked) and filled, "each such look has an evidence file with a non-empty response")
    calls = (state / "calls.log").read_text().splitlines() if (state / "calls.log").exists() else []
    check(len(calls) >= 3, f"the server's own log holds at least 3 calls ({len(calls)})")
    check((w["state"], w["verdict"]) == ("ended", "met"), f"watch.json is ended/met ({w['state']}/{w['verdict']})")
    notes = (home / "ops" / "proof" / "notes.md").read_text()
    entries = [line for line in notes.splitlines() if factory.OPS_ENTRY_RE.match(line)]
    check("## Checks" in notes and len(entries) >= 3, f"the notes hold the checks and at least 3 entries ({len(entries)})")
    leaked = real_store.leaked(TMP.name)
    check(leaked == 0, f"the real store's logs name this scratch directory in {leaked} line(s)")
    print("PASS" if not failures else f"FAIL: {len(failures)} check(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
