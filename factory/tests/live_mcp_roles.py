#!/usr/bin/env python3
"""Live proof of MCP servers per role. Run by hand, never by unittest; it costs real money (about twenty short haiku sessions).

    python3 factory/tests/live_mcp_roles.py [--trusted-dir DIR] [--keep]

Everything lives in a scratch directory: a scratch FACTORY_HOME (refused under the real ~/.claude), a scratch repository with a
`.mcp.json`, a scratch Claude JSON the factory reads instead of yours, and the stdio server `mcp_role_server.py` started as `small` (3 read
tools), `large` (30) and `broken` (exits at once), each with one write tool that is never granted; a call log only the server writes.
The records are made through the real `factory ops decline` and `factory mcp grant`. The sessions are started from the argv of
`build_launch_argv`, `build_argv` and `tech_lead_argv`, as `-p` runs on haiku with the prompt replaced (the lead's `--bg` and
`--worktree`/`-n` become `-p`; `--json-schema` goes; the output is stream-json).

1. For the lead, the build-run tech lead and the merge-queue tech lead: the MCP tools the session holds (its init record), a
   call of a listed tool, a call of the write tool that is not listed, and a subagent's call of a listed tool, each checked
   against the server's call log and the gate's refusal texts in the stream.
2. Starting context (the first request: input + cache creation + cache read; trivial prompt) of the lead and the build run
   with no record, `small`, `large`, both; the difference per server and per tool; whether the tools arrive as names only.
3. A server that exits at once: whether the session starts, what it holds, what it says.
4. With `--trusted-dir DIR` (a git checkout Claude Code already trusts: a `--bg` session needs a trusted workspace, so a scratch
   repository cannot host one): the real `claude --bg` lead argv without `--worktree`, `-n` and `--plugin-dir` (no worktree is
   made in DIR, and its hooks stay out of your factory store: a background session does not inherit this script's
   FACTORY_HOME) holds and calls the server; then it is stopped and woken with `claude --bg --resume <id> <prompt>`, the
   flag-free argv of `reach_lead`, and called again. The gate of a background session reads the default store, so its
   decisions are proved by the `-p` runs of 1 only.

Every lead start passes the dispatcher name `d`, which no live session has: a scratch lead finds its dispatcher by session name,
which a scratch FACTORY_HOME does not change, so the live name would reach the live dispatcher.

The scratch directory and the sessions' transcripts are deleted at the end unless `--keep`; the size is printed."""
import argparse
import glob
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import real_store

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--trusted-dir", metavar="DIR", help="a trusted git checkout, for the --bg start and the resume")
ap.add_argument("--only", type=int, action="append", choices=(1, 2, 3, 4), help="run these proofs only (repeatable)")
ap.add_argument("--keep", action="store_true", help="keep the scratch directory and the transcripts")
args = ap.parse_args()

TMP = Path(tempfile.mkdtemp(prefix="mcp-live-")).resolve()
os.environ["FACTORY_HOME"] = str(TMP / "home")
real_store.refuse(os.environ["FACTORY_HOME"])
os.environ.pop("CLAUDECODE", None)
os.environ.pop("FACTORY_ROLE", None)
HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin" / "factory"
_loader = importlib.machinery.SourceFileLoader("factory_bin", str(BIN))
factory = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_bin", _loader))
_loader.exec_module(factory)
factory = getattr(factory, "factory_core", factory)

LOGS = TMP / "logs"
CLAUDE_JSON = TMP / "claude.json"
REPO = TMP / "repo"
SERVER = HERE / "mcp_role_server.py"
failures, sessions, transcripts = [], [], set()
SCRATCH_ONLY = "This is a scratch check of tool access, nothing else: send no message to anyone, run no `factory` command, write nothing. "


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def server(name, count):
    return {"command": sys.executable, "args": [str(SERVER), str(LOGS / f"{name}.log"), str(count)]}


SERVERS = {"small": server("small", 3), "large": server("large", 30),
           "broken": {"command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}}


def sh(argv, cwd=None, env=None):
    p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


def calls(name):
    path = LOGS / f"{name}.log"
    return path.read_text().split() if path.exists() else []


def setup(trusted):
    LOGS.mkdir(parents=True)
    REPO.mkdir()
    for argv in (["git", "init", "-q"], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"]):
        sh(argv, cwd=REPO)
    (REPO / ".mcp.json").write_text(json.dumps({"mcpServers": SERVERS}))
    roots = {str(REPO.resolve()): {"mcpServers": SERVERS}}
    if trusted:
        roots[factory.Repo(trusted).root] = {"mcpServers": SERVERS}
    CLAUDE_JSON.write_text(json.dumps({"projects": roots}))
    factory.CLAUDE_JSON = CLAUDE_JSON
    sh(["git", "worktree", "add", "-q", "-b", "task", str(REPO / ".claude" / "worktrees" / "task")], cwd=REPO)


def cli(root, *argv):
    """`factory <argv>` as the human, in the repository at `root`."""
    code, out, err = sh([sys.executable, str(BIN), *argv], cwd=root)
    if code:
        sys.exit(f"factory {' '.join(argv)} failed: {err}")
    return out


def record(root, *tools):
    """The record of the repository at `root`: `tools` for both roles, or none."""
    cli(root, "mcp", "revoke")
    if tools:
        cli(root, "mcp", "grant", "--role", "lead", "--role", "tech-lead", *tools, "--words", "live proof")


def run_print(argv, cwd, role=None, timeout=300):
    """Run a `claude -p` session in its own process group; its stream-json events and its stderr."""
    env = {**os.environ, "FACTORY_SESSION": "1", **({"FACTORY_ROLE": role} if role else {})}
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)  # nothing of the session outlives this script
            except OSError:
                pass
    events = []
    for line in out.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    for e in events:
        if e.get("session_id"):
            transcripts.add(e["session_id"])
    return p.returncode, events, err


def settings():
    s = factory.load_settings()
    s["models"] = {k: "haiku" for k in s["models"]}
    return s


def starts():
    """name -> (argv with the prompt `p`, cwd, FACTORY_ROLE) of the three role starts, from the real builders."""
    s, repo = settings(), factory.Repo(REPO)
    with mock.patch.object(factory, "tech_lead_prompt", return_value="p"), mock.patch.object(factory, "build_prompt", return_value="p"):
        return {
            "lead": (factory.build_launch_argv("lead1", "d", "p", s, "main", "origin", 0, 3600, repo.root), REPO, None),
            "build-run tech lead": (factory.build_argv(repo, "task", "spec", TMP / "h", "", s), REPO, "tech-lead"),
            "merge-queue tech lead": (factory.tech_lead_argv(repo, "task", {}, "e", "spec", s), REPO, "tech-lead")}


def headless(argv, prompt):
    """`argv` as a `-p` run that streams its events and takes `prompt` (the last word)."""
    argv = list(argv[:-1])
    if "--bg" in argv:
        argv[argv.index("--bg")] = "-p"
        del argv[argv.index("-n"):argv.index("-n") + 2]
    for flag in ("--json-schema", "--output-format"):
        if flag in argv:
            del argv[argv.index(flag):argv.index(flag) + 2]
    return [*argv, "--output-format", "stream-json", "--verbose", prompt]


def held(events):
    init = next((e for e in events if e.get("subtype") == "init"), {})
    return [t for t in init.get("tools", []) if t.startswith("mcp__")], init.get("mcp_servers", [])


def first_request(events):
    """The tokens of the first request of the session: input + cache creation + cache read."""
    u = next((e["message"]["usage"] for e in events if e.get("type") == "assistant" and not e.get("parent_tool_use_id")), None)
    return u and u["input_tokens"] + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)


def texts(events):
    """Every tool result of the stream as (from a subagent, text)."""
    out = []
    for e in events:
        for b in (e.get("message") or {}).get("content") or []:
            if e.get("type") == "user" and isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                out.append((bool(e.get("parent_tool_use_id")), c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))))
    return out


def deferred(session_id):
    """Whether the session's transcript lists the `mcp__` tools as names to load (a deferred-tools record), not as schemas in the request."""
    for path in glob.glob(str(Path("~/.claude/projects").expanduser() / "*" / f"{session_id}.jsonl")):
        for line in open(path):
            if '"deferred_tools_delta"' in line and "mcp__" in line:
                return True
    return False


def last_text(session_id):
    """What the session last said, from its transcript."""
    said = ""
    for path in glob.glob(str(Path("~/.claude/projects").expanduser() / "*" / f"{session_id}.jsonl")):
        for line in open(path):
            e = json.loads(line)
            said = next((b["text"] for b in (e.get("message") or {}).get("content") or [] if e.get("type") == "assistant" and isinstance(b, dict) and b.get("type") == "text"), said)
    return said


def proof_1():
    print("1. Each role: held tools, a listed call, an unlisted call, a subagent's call")
    record(REPO, "mcp__small__read_01")
    for name, (argv, cwd, role) in starts().items():
        agents = ["factory:coder", "factory:explorer"] if name == "lead" else ["factory:coder"]
        asks = "; ".join(f"({i + 3}) use the Agent tool with subagent_type {a} to ask it to call mcp__small__read_01 with id 2 itself and to report its exact result or refusal"
                         for i, a in enumerate(agents))
        prompt = ("Make these attempts in order, one call each, and retry none: (1) call mcp__small__read_01 with id 1; (2) call "
                  f"mcp__small__write_record with id 1; {asks}. Then reply with one line per attempt, quoting any refusal text.")
        before = calls("small")
        rc, events, err = run_print(headless(argv, prompt), cwd, role)
        mine = calls("small")[len(before):]
        tools, servers = held(events)
        results = texts(events)
        print(f"- {name}: exit {rc}; servers {servers}; holds {len(tools)} MCP tools {sorted(tools)}")
        print(f"  server log: {mine}")
        check(rc == 0 and tools and "mcp__small__write_record" in tools, f"{name} holds the server's tools, the write tool included")
        check(mine == ["read_01"], f"{name}: the listed tool reached the server once; the write tool and the subagent's call did not")
        check(any(f"mcp__small__write_record is not on this repository's list for the {role or 'lead'}" in t for sub, t in results if not sub),
              f"{name}: the write tool was refused with the gate's reason")
        subagent = [t for sub, t in results if sub and "not to their subagents" in t]
        print(f"  subagent refusals seen in the stream: {len(subagent)}; denials of the attempt to dispatch: "
              f"{[t[:80] for sub, t in results if not sub and 'denied' in t.lower() and 'Agent' in t][:2]}")
        if name != "lead":  # the lead's deny list refuses `factory:coder` itself; its other subagents hold no MCP tool
            check(bool(subagent), f"{name}: a subagent's call of the listed tool was refused with the gate's reason")


def proof_2():
    print("2. Starting context in tokens (first request, trivial prompt, haiku)")
    figures = {}
    for label, tools in (("none", ()), ("small", ("mcp__small__read_01",)), ("large", ("mcp__large__read_01",)),
                         ("both", ("mcp__small__read_01", "mcp__large__read_01"))):
        record(REPO, *tools)
        for name, (argv, cwd, role) in starts().items():
            if name == "merge-queue tech lead":
                continue
            rc, events, _ = run_print(headless(argv, "Reply with the single word ok."), cwd, role)
            figures[name, label] = first_request(events)
            print(f"- {name:20} {label:6} {figures[name, label]}  exit {rc}; MCP tools held {len(held(events)[0])}; names deferred: "
                  f"{next((deferred(e['session_id']) for e in events if e.get('session_id')), None)}")
    for name in ("lead", "build-run tech lead"):
        f = lambda label: figures.get((name, label))
        if None in (f("none"), f("small"), f("large"), f("both")):
            continue
        # a server serves its read tools and one write tool: small 3 + 1, large 30 + 1
        print(f"  {name}: small +{f('small') - f('none')} (4 tools), large +{f('large') - f('none')} (31 tools), both +{f('both') - f('none')}; "
              f"per added tool {(f('large') - f('small')) / 27:.1f}; per server about {(f('both') - f('none')) / 2:.0f}")


def proof_3():
    print("3. A server whose command exits at once")
    record(REPO, "mcp__broken__probe", "mcp__small__read_01")
    argv, cwd, role = starts()["build-run tech lead"]
    rc, events, err = run_print(headless(argv, "List the names of the MCP tools you hold, then reply with the single word ok."), cwd, role)
    tools, servers = held(events)
    result = next((e.get("result") for e in events if e.get("type") == "result"), None)
    print(f"- exit {rc}; servers {servers}; holds {sorted(tools)}; stderr {err.strip()[:200]!r}\n  said: {str(result)[:300]!r}")
    check(rc == 0 and any(t.startswith("mcp__small__") for t in tools), "the session started and holds the working server's tools")


def background(trusted):
    print("4. A background lead, and a lead woken through reach_lead's argv")
    root = factory.Repo(trusted).root
    record(trusted, "mcp__small__read_01")
    s = settings()
    argv = factory.build_launch_argv("lead1", "d", "b", s, "main", "origin", 0, 3600, root)
    for flag in ("--worktree", "-n", "--plugin-dir"):
        del argv[argv.index(flag):argv.index(flag) + 2]
    argv[-1] = SCRATCH_ONLY + "Load mcp__small__read_01 with ToolSearch if it is not loaded, call it with id 1, then say done."
    check(any(a.startswith("--mcp-config=") for a in argv), "the lead's argv carries --mcp-config")

    def wake(argv, want):
        rc, out, err = sh(argv, cwd=trusted)
        found = re.search(r"backgrounded\s*·\s*(\S+)", out + err)
        if not found:
            check(False, f"the session did not start: {(out + err).strip()[:200]}")
            return None
        sid = found[1]
        sessions.append(sid)
        for _ in range(60):
            rows = [a for a in json.loads(sh(["claude", "agents", "--json", "--all"])[1]) if a.get("id") == sid]
            if rows and rows[0].get("state") == "done" and calls("small").count(want) >= 1:
                break
            time.sleep(3)
        return rows[0] if rows else None
    before = calls("small")
    row = wake(argv, "read_01")
    check(bool(row) and calls("small")[len(before):] == ["read_01"], f"the --bg lead started with --mcp-config called the listed tool (log {calls('small')[len(before):]})")
    if row:
        transcripts.add(row["sessionId"])
        deferred_names = deferred(row["sessionId"])
        print(f"  its transcript lists the server's tools as names to load: {deferred_names}")
        sh(["claude", "stop", row["id"]])
        for _ in range(30):  # a session still running would be copied, not woken
            if next(a for a in json.loads(sh(["claude", "agents", "--json", "--all"])[1]) if a.get("id") == row["id"]).get("state") == "stopped":
                break
            time.sleep(2)
        before = calls("small")
        rc, out, err = sh(["claude", "--bg", "--resume", row["sessionId"], SCRATCH_ONLY + "Load mcp__small__read_02 with ToolSearch if it is not loaded, call it with id 2, then say done. If it is unavailable because the server is still connecting, run `sleep 10` with Bash and try again, up to 6 times."], cwd=trusted)
        print(f"  session {row['id']} stopped, then woken: {' | '.join((out + err).strip().splitlines()[:2])[:300]}")
        sessions.extend(re.findall(r"backgrounded\s*·\s*(\S+)", out + err))
        for _ in range(60):
            if "read_02" in calls("small")[len(before):]:
                break
            time.sleep(3)
        called = "read_02" in calls("small")[len(before):]
        check(called, "the woken lead still holds the server and called another of its tools")
        if not called:
            print(f"  it said: {last_text(row['sessionId'])[:300]!r}")


def drop_transcripts():
    for path in glob.glob(str(Path("~/.claude/projects").expanduser() / f"*{re.sub('[^A-Za-z0-9]', '-', TMP.name)}*")):
        shutil.rmtree(path, ignore_errors=True)
    for sid in transcripts:
        for path in glob.glob(str(Path("~/.claude/projects").expanduser() / "*" / f"{sid}*")):
            shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)


def size(path):
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


print(f"claude {sh(['claude', '--version'])[1].strip()}, {time.strftime('%Y-%m-%d')}, scratch {TMP}")
try:
    setup(args.trusted_dir)
    cli(REPO, "ops", "decline", "--words", "live proof: nothing here is production")
    if args.trusted_dir:
        cli(args.trusted_dir, "ops", "decline", "--words", "live proof: nothing here is production")
    for n, proof in enumerate((proof_1, proof_2, proof_3), 1):
        if n in (args.only or (1, 2, 3)):
            proof()
    if 4 in (args.only or (4,)) and args.trusted_dir:
        background(args.trusted_dir)
    elif 4 in (args.only or (4,)):
        print("4. skipped: no --trusted-dir")
finally:
    for sid in sessions:
        sh(["claude", "stop", sid])
    killed = subprocess.run(["pgrep", "-f", str(SERVER)], capture_output=True, text=True).stdout.split()
    for pid in killed:
        os.kill(int(pid), signal.SIGKILL)
    scratch = size(TMP)
    if not args.keep:
        drop_transcripts()
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"scratch {scratch} bytes {'kept' if args.keep else 'deleted'}; servers left behind: {len(killed)}")
print("FAILED: " + "; ".join(failures) if failures else "all checks passed")
sys.exit(1 if failures else 0)
