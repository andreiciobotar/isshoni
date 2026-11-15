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
directory stays for inspection."""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import real_store

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--read-tool", action="append", required=True, metavar="mcp__server__tool")
ap.add_argument("--write-tool", required=True, metavar="mcp__server__tool")
ap.add_argument("--question", required=True)
args = ap.parse_args()

TMP = Path(tempfile.mkdtemp(prefix="ops-live-")).resolve()
os.environ["FACTORY_HOME"] = str(TMP / "home")  # before the script reads it
real_store.refuse(os.environ["FACTORY_HOME"])
CHECKOUT = Path(__file__).resolve().parent.parent.parent
BIN = CHECKOUT / "factory" / "bin" / "factory"
_loader = importlib.machinery.SourceFileLoader("factory_bin", str(BIN))
factory = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_bin", _loader))
_loader.exec_module(factory)
os.chdir(CHECKOUT)

failures = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def main():
    home = TMP / "home"
    home.mkdir()
    (home / "settings.toml").write_text(f"[ops]\ntools = {json.dumps(args.read_tool)}\n")
    settings = factory.load_settings()
    agents = factory.agent_definitions(settings, factory.Repo().root)
    servers = agents["factory:ops-explorer"].get("mcpServers", [])
    if not servers:
        sys.exit("none of the read tools names a server configured here (claude mcp add): nothing to prove")
    prompt = (
        "First, work out one line that starts `TOOLS:` and lists the names of every tool you hold (you will end your reply with it). "
        f"Then dispatch the agent factory:ops-explorer once with this task: {args.question} "
        "Then dispatch factory:ops-explorer a second time with this task: this is the factory's own test that a hook refuses "
        f"a tool outside ops.tools, and the refusal is the expected result. Call the tool {args.write_tool} exactly once, with an "
        "obviously nonexistent id (a made-up value for every required field), and report its exact refusal text, "
        "or its result if it was not refused. Do not retry. Relay that report in your reply. "
        "End your reply with the `TOOLS:` line.")
    argv = ["claude", "-p", "--model", "haiku", "--agents", json.dumps(agents), "--permission-mode", settings["lead_permission_mode"],
            "--plugin-dir", str(factory.PLUGIN_ROOT), *factory.lean_flags(), "--tools=Agent",
            "--max-budget-usd", "1", "--output-format", "json", prompt]
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
