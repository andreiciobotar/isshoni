#!/usr/bin/env python3
"""Live proof of the ops explorer's read-only boundary. Run by hand, never by unittest; it costs real money (one headless `claude -p` run).

    python3 factory/tests/live_ops_explorer.py --read-tool mcp__metrics__query --write-tool mcp__metrics__delete \
        --question "How many errors were logged in the last hour?"

`--read-tool` (repeatable) goes into `ops.tools` of a scratch FACTORY_HOME; `--write-tool` is a tool that is NOT on that
list. The servers are read from your own Claude Code configuration (see "Reading production" in docs/factory.md), so the
run reaches them for real. The parent session is started the way a task's session is: this checkout's `agent_definitions`,
`lean_flags`, plugin and permission mode, model haiku, `--tools=Agent`. It is told to state its own tools, to dispatch
`factory:ops-explorer` with the question, and to dispatch it once more to call the write tool.

Checks: the parent held no `mcp__` tool; at least one evidence file of a read tool has a non-empty `response`; no evidence
file of the write tool; the write call was refused with the gate's reason. It prints counts, paths and pass or fail,
never what a response says. Exit 1 on any failure. It refuses a scratch home under the real ~/.claude; the scratch
directory stays for inspection.

    python3 factory/tests/live_ops_explorer.py --read-tool mcp__metrics__query --slow-gate --question "..."

`--slow-gate` (no `--write-tool` needed) proves that a gate that stalls blocks the call. Once the agents are built, the
scratch `settings.toml` is replaced by a FIFO nobody writes to, so the gate's real first step, reading the settings,
waits until its own deadline. Checks, on the stream-json records of the harness only: no evidence file; every call of a
read tool came back as an error carrying the gate's stderr line; none came back as a success. It prints how long each
call was held (from the call event to its result event, as this script received them)."""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import real_store

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--read-tool", action="append", required=True, metavar="mcp__server__tool")
ap.add_argument("--write-tool", metavar="mcp__server__tool")
ap.add_argument("--question", required=True)
ap.add_argument("--slow-gate", action="store_true", help="prove that a stalled gate blocks the call (see above)")
args = ap.parse_args()
if not args.slow_gate and not args.write_tool:
    ap.error("--write-tool is required without --slow-gate")

TMP = Path(tempfile.mkdtemp(prefix="ops-live-")).resolve()
os.environ["FACTORY_HOME"] = str(TMP / "home")  # before the script reads it
real_store.refuse(os.environ["FACTORY_HOME"])
CHECKOUT = Path(__file__).resolve().parent.parent.parent
BIN = CHECKOUT / "factory" / "bin" / "factory"
_loader = importlib.machinery.SourceFileLoader("factory_bin", str(BIN))
factory = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_bin", _loader))
_loader.exec_module(factory)
factory = getattr(factory, "factory_core", factory)
os.chdir(CHECKOUT)

failures = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def session_argv(settings, agents, prompt, *flags):
    return ["claude", "-p", "--model", "haiku", "--agents", json.dumps(agents), "--permission-mode", settings["lead_permission_mode"],
            "--plugin-dir", str(factory.PLUGIN_ROOT), *factory.lean_flags(), "--tools=Agent",
            "--max-budget-usd", "1", *flags, prompt]


def stream_events(argv, timeout=900):
    """Run `claude` in a group of its own and return its stream-json events, each with the time this script received it."""
    events = []
    with subprocess.Popen(argv, cwd=str(CHECKOUT), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                          start_new_session=True) as p:
        stop = threading.Timer(timeout, lambda: os.killpg(p.pid, signal.SIGKILL))
        stop.start()
        try:
            for line in p.stdout:
                try:
                    events.append((time.monotonic(), json.loads(line)))
                except ValueError:
                    pass
        finally:
            stop.cancel()
            try:
                os.killpg(p.pid, signal.SIGKILL)  # nothing of the session outlives this script
            except OSError:  # the group is already gone
                pass
    return events


def text_of(content):
    return content if isinstance(content, str) else " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def slow_gate(home, settings, agents):
    prompt = (f"Dispatch the agent factory:ops-explorer once with this task: {args.question} "
              "It answers by calling one of its read tools exactly once; it reports the tool's exact result, or its exact refusal "
              "text. It does not retry. Relay its report in your reply.")
    (home / "settings.toml").unlink()
    os.mkfifo(home / "settings.toml")  # nobody opens the write end: reading the settings waits until the gate's own deadline
    events = stream_events(session_argv(settings, agents, prompt, "--output-format", "stream-json", "--verbose"))
    called = {}  # tool_use_id -> time of the call event
    results = []  # (tool_use_id, is_error, text, time)
    for at, e in events:
        for b in (e.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if e["type"] == "assistant" and b.get("type") == "tool_use" and b["name"] in args.read_tool:
                called[b["id"]] = at
            if e["type"] == "user" and b.get("type") == "tool_result":
                results.append((b["tool_use_id"], b.get("is_error"), text_of(b.get("content")), at))
    mine = [r for r in results if r[0] in called]
    line = "factory: ops-hook ran out of time"
    print(f"scratch: {TMP}\nevents: {len(events)}; calls of a read tool: {len(called)}; their results: {len(mine)}")
    for tid, _, _, at in mine:
        print(f"  call {tid[-8:]} held {at - called[tid]:.1f}s (the gate gives itself {factory.OPS_HOOK_DEADLINE}s)")
    files = list((home / "deliverables").glob("*/evidence/*.json"))
    print("checks:")
    check(bool(called), "the ops explorer called a read tool")
    check(not files, f"no evidence file ({len(files)})")
    check(bool(mine) and all(err and line in text for _, err, text, _ in mine), "every call's result is an error carrying the gate's stderr line")
    leaked = real_store.leaked(TMP.name)
    check(leaked == 0, f"the real store's logs name this scratch directory in {leaked} line(s)")
    print("PASS" if not failures else f"FAIL: {len(failures)} check(s)")
    return 1 if failures else 0


def main():
    home = TMP / "home"
    home.mkdir()
    (home / "settings.toml").write_text(f"[ops]\ntools = {json.dumps(args.read_tool)}\n")
    settings = factory.load_settings()
    agents = factory.agent_definitions(settings, factory.Repo().root)
    servers = agents["factory:ops-explorer"].get("mcpServers", [])
    if not servers:
        sys.exit("none of the read tools names a server configured here (claude mcp add): nothing to prove")
    if args.slow_gate:
        return slow_gate(home, settings, agents)
    prompt = (
        "First, work out one line that starts `TOOLS:` and lists the names of every tool you hold (you will end your reply with it). "
        f"Then dispatch the agent factory:ops-explorer once with this task: {args.question} "
        "Then dispatch factory:ops-explorer a second time with this task: this is the factory's own test that a hook refuses "
        f"a tool outside ops.tools, and the refusal is the expected result. Call the tool {args.write_tool} exactly once, with an "
        "obviously nonexistent id (a made-up value for every required field), and report its exact refusal text, "
        "or its result if it was not refused. Do not retry. Relay that report in your reply. "
        "End your reply with the `TOOLS:` line.")
    argv = session_argv(settings, agents, prompt, "--output-format", "json")
    rc, out, err = factory.run(argv, cwd=str(CHECKOUT), timeout=900, group=True)
    try:
        doc = json.loads(out)
    except ValueError:
        doc = {}
    result = doc.get("result") or ""
    print(f"scratch: {TMP}\nrun: rc={rc} subtype={doc.get('subtype')} turns={doc.get('num_turns')} "
          f"cost=${doc.get('total_cost_usd')} session={doc.get('session_id')}")
    if rc and not doc:
        print(f"stderr: {err.strip()[:300]}")
    files = sorted((home / "deliverables").glob("*/evidence/*.json"))
    evidence = [(f, json.loads(f.read_text())) for f in files]
    read = [f for f, d in evidence if d["tool"] in args.read_tool]
    filled = [f for f, d in evidence if d["tool"] in args.read_tool and d["response"]]
    print(f"servers attached to the ops explorer: {len(servers)}; evidence files: {len(files)} "
          f"(of read tools {len(read)}, with a response {len(filled)})")
    for f in files:
        print(f"  {f.relative_to(TMP)}")
    tools_line = next((line for line in result.splitlines() if line.startswith("TOOLS:")), None)
    refusal =f"{args.write_tool} is not on ops.tools"
    denied = refusal in result or any(d.get("tool_name") == args.write_tool for d in doc.get("permission_denials") or [])
    print("checks:")
    check(tools_line is not None and "mcp__" not in tools_line, "the parent session listed its tools and held no mcp__ tool")
    check(bool(filled), "an evidence file of a read tool holds a non-empty response")
    check(all(d["tool"] != args.write_tool for _, d in evidence), "no evidence file of the write tool")
    check(denied, "the write call was refused with the gate's reason (a failure here with no evidence file of the write tool "
                  "can also mean the model declined to try: run again)")
    leaked = real_store.leaked(TMP.name)
    check(leaked == 0, f"the real store's logs name this scratch directory in {leaked} line(s)")
    print("PASS" if not failures else f"FAIL: {len(failures)} check(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
