#!/usr/bin/env python3
"""Live proof of skills per role. Run by hand, never by unittest; it costs real money (four short haiku runs with two subagents each).

    python3 factory/tests/live_skills_roles.py [--trusted-dir DIR] [--keep]

Everything lives in a scratch directory: a scratch FACTORY_HOME (refused under the real ~/.claude) and a scratch repository whose
tracked `.claude/skills` hold two skills, each with a marker word its body tells the reader to state: `proof-review-rule`
(recorded for the reviewer) and `proof-build-rule` (recorded for the tech lead), through the real `factory skills grant`.
One `-p` session on haiku is started with the argv `build_argv` returns (the prompt replaced, `--json-schema` gone, the output
stream-json) and asked to dispatch `factory:reviewer` and `factory:coder`. Then `factory skills revoke`, and the same again.

Asserted: with the record the reviewer's answer holds the review marker and the coder's does not; the session called `Skill` for
`proof-build-rule`; after the revoke neither marker appears anywhere (the stream and every transcript of the runs).
Measured: the bytes of each SKILL.md, the reviewer's first request with and without the record, the tokens the session's Skill
call returned (estimated from the next request), the session's first request with and without the sentence.

`--trusted-dir DIR` puts the scratch directory in DIR (a directory Claude Code already trusts) instead of the system temp directory.

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
ap.add_argument("--trusted-dir", metavar="DIR", help="a directory to create the scratch directory in (default: the system temp directory)")
ap.add_argument("--keep", action="store_true", help="keep the scratch directory and the transcripts")
args = ap.parse_args()

TMP = Path(tempfile.mkdtemp(prefix="skills-live-", dir=args.trusted_dir)).resolve()
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

REPO = TMP / "repo"
PROJECTS = Path("~/.claude/projects").expanduser()
MARKERS = {"proof-review-rule": "ZEPHYRQUILL", "proof-build-rule": "MARIGOLDVEX"}
failures, transcripts, spend = [], set(), [0.0]
RUNS = 3
BRIEF = "State every marker word in your instructions, change nothing."
PROMPT = ("Send no message to anyone, run no `factory` command, write nothing yourself. After your own first steps by your instructions, use the Agent tool once with "
          f"subagent_type factory:reviewer and once with subagent_type factory:coder, the prompt of each being exactly: {BRIEF} "
          "Then reply with the answer of each, labelled reviewer and coder.")


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def sh(argv, cwd=None):
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


def cli(*argv):
    """`factory <argv>` as the human, in the scratch repository."""
    code, out, err = sh([sys.executable, str(BIN), *argv], cwd=REPO)
    if code:
        sys.exit(f"factory {' '.join(argv)} failed: {err}")
    return out


def setup():
    REPO.mkdir()
    for name, marker in MARKERS.items():
        path = REPO / ".claude" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: A scratch rule for a proof run; load it only when a role is told to.\n---\n"
                        f"When this skill is loaded, state the word {marker} in your reply, exactly once, and say nothing else about it.\n")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"],
                 ["git", "worktree", "add", "-q", "-b", "task", str(REPO / ".claude" / "worktrees" / "task")]):
        sh(argv, cwd=REPO)
    cli("ops", "decline", "--words", "live proof: nothing here is production")


def build_start():
    """The argv `build_argv` returns for the scratch task, as a `-p` run that streams its events and takes PROMPT."""
    s, repo = factory.load_settings(), factory.Repo(REPO)
    s["models"] = {k: "haiku" for k in s["models"]}
    with mock.patch.object(factory, "build_prompt", return_value="p"):
        argv = factory.build_argv(repo, "task", "spec", TMP / "h", "", s)
    argv = argv[:-1]
    del argv[argv.index("--json-schema"):argv.index("--json-schema") + 2]
    del argv[argv.index("--output-format"):argv.index("--output-format") + 2]
    return [*argv, "--output-format", "stream-json", "--verbose", PROMPT]


def run_print(argv, timeout=420):
    """Run a `claude -p` session in its own process group; its stream-json events and its stderr."""
    env = {**os.environ, "FACTORY_ROLE": "tech-lead"}
    with subprocess.Popen(argv, cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True) as p:
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
        if e.get("type") == "result":
            spend[0] += e.get("total_cost_usd") or 0
    return p.returncode, events, err


def blocks(e):
    return [b for b in (e.get("message") or {}).get("content") or [] if isinstance(b, dict)]


def requests(events, parent=None):
    """The model requests of the session (`parent` None) or of the subagent that tool call started, once each: (message id, input tokens, output tokens, tool_use ids)."""
    seen = {}
    for e in events:
        if e.get("type") == "assistant" and e.get("parent_tool_use_id") == parent and e["message"]["id"] not in seen:
            u = e["message"]["usage"]
            seen[e["message"]["id"]] = (e["message"]["id"], u["input_tokens"] + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
                                        u["output_tokens"], [b["id"] for b in blocks(e) if b.get("type") == "tool_use"])
        elif e.get("type") == "assistant" and e.get("parent_tool_use_id") == parent:  # later blocks of a message: its tool calls
            seen[e["message"]["id"]][3].extend(b["id"] for b in blocks(e) if b.get("type") == "tool_use" and b["id"] not in seen[e["message"]["id"]][3])
    return list(seen.values())


def answers(events):
    """subagent_type -> (the Agent call's id, what the subagent said: its text and the message it handed back, then the Agent call's result)."""
    calls = {b["id"]: b["input"].get("subagent_type") for e in events if e.get("type") == "assistant" and not e.get("parent_tool_use_id")
             for b in blocks(e) if b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task")}
    out = {}
    for e in events:
        for b in blocks(e):
            if e.get("type") == "user" and b.get("type") == "tool_result" and b.get("tool_use_id") in calls and not e.get("parent_tool_use_id"):
                c = b.get("content")
                said = [c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))]
                said[:0] = [b2.get("text") or json.dumps(b2.get("input")) for e2 in events if e2.get("type") == "assistant" and e2.get("parent_tool_use_id") == b["tool_use_id"]
                            for b2 in blocks(e2) if b2.get("type") in ("text", "tool_use")]
                out[calls[b["tool_use_id"]]] = (b["tool_use_id"], "\n".join(said))
    return out


def transcript_files():
    return [p for sid in transcripts for p in glob.glob(str(PROJECTS / "*" / f"{sid}*"), recursive=False)]


def run(label):
    rc, events, err = run_print(build_start())
    init = next((e for e in events if e.get("subtype") == "init"), {})
    got = answers(events)
    print(f"- {label}: exit {rc}; session skills visible {len(init.get('skills', []))}, the two scratch ones among them: "
          f"{sorted(s for s in init.get('skills', []) if s in MARKERS)}; subagents answered: {sorted(got)}; stderr {err.strip()[:200]!r}")
    return events, got


def reviewer_first(events, got):
    reqs = requests(events, got["factory:reviewer"][0]) if "factory:reviewer" in got else []
    return reqs[0][1] if reqs else None


def skill_call_cost(events):
    """The tokens the session's Skill call returned, estimated: the next request's input less the call's request's input and output."""
    reqs = requests(events)
    skill_ids = {b["id"] for e in events if e.get("type") == "assistant" and not e.get("parent_tool_use_id") for b in blocks(e)
                 if b.get("type") == "tool_use" and b.get("name") == "Skill" and "proof-build-rule" in json.dumps(b.get("input"))}
    for i, (_, tokens, out, ids) in enumerate(reqs[:-1]):
        if skill_ids & set(ids):
            return reqs[i + 1][1] - tokens - out, len(ids)
    return None, 0


print(f"claude {sh(['claude', '--version'])[1].strip()}, {time.strftime('%Y-%m-%d')}, scratch directory under {'the given directory' if args.trusted_dir else 'the system temp directory'}")
try:
    setup()
    sizes = {n: (REPO / ".claude" / "skills" / n / "SKILL.md").stat().st_size for n in MARKERS}
    print(f"scratch SKILL.md sizes in bytes: {sizes}")

    print("1. With the record")
    cli("skills", "grant", "--role", "reviewer", "proof-review-rule", "--words", "live proof")
    cli("skills", "grant", "--role", "tech-lead", "proof-build-rule", "--words", "live proof")
    print(cli("skills", "access").rstrip())
    called_skill = lambda events: any(b.get("name") == "Skill" for e in events if e.get("type") == "assistant" and not e.get("parent_tool_use_id") for b in blocks(e))
    attempts, chosen = [], None
    for attempt in range(1, RUNS + 1):  # all runs are made, so the rate is not biased by stopping at the first call
        events, got = run(f"with the record, run {attempt}")
        attempts.append(called_skill(events))
        print(f"  run {attempt}: the session {'called' if attempts[-1] else 'did not call'} Skill")
        if chosen is None or (attempts[-1] and not any(attempts[:-1])):
            chosen = events, got
    print(f"  called Skill in {sum(attempts)} of {len(attempts)} runs (the first run that did is the one measured below, else the first)")
    events1, got1 = chosen
    rev, cod = got1.get("factory:reviewer", (None, "")), got1.get("factory:coder", (None, ""))
    check(MARKERS["proof-review-rule"] in rev[1], "the reviewer's answer holds the review marker")
    check(bool(cod[0]) and MARKERS["proof-review-rule"] not in cod[1], "the coder answered and its answer lacks the review marker")
    skill_calls = [b["input"] for e in events1 if e.get("type") == "assistant" and not e.get("parent_tool_use_id") for b in blocks(e)
                   if b.get("type") == "tool_use" and b.get("name") == "Skill"]
    print(f"  the session's Skill calls: {skill_calls}")
    check(any("proof-build-rule" in json.dumps(i) for i in skill_calls), "the session made a Skill call naming proof-build-rule")
    print(f"  the reviewer said: {rev[1][:200]!r}\n  the coder said: {cod[1][:200]!r}")

    print("2. After `factory skills revoke`")
    print(cli("skills", "revoke", "--words", "live proof").rstrip())
    events2, got2 = run("without the record")
    text2 = json.dumps(events2)
    check(all(m not in text2 for m in MARKERS.values()), "neither marker appears in the second run's stream")
    check(len(got2) == 2, "the second run's subagents both answered")

    print("3. Measured")
    r1, r0 = reviewer_first(events1, got1), reviewer_first(events2, got2)
    s1, s0 = (requests(e)[0][1] if requests(e) else None for e in (events1, events2))
    skill_tokens, parallel = skill_call_cost(events1)
    print(f"- SKILL.md bytes: {sizes}")
    print(f"- reviewer first request (input + cache creation + cache read): with {r1}, without {r0}; difference {None if None in (r1, r0) else r1 - r0} tokens per reviewer start")
    print(f"- Skill call of the session: about {skill_tokens} tokens returned (estimated from the next request; {parallel} tool call(s) in that request)")
    print(f"- session first request: with the sentence {s1}, without {s0}; difference {None if None in (s1, s0) else s1 - s0} tokens")
    print(f"- spend of all runs: ${spend[0]:.4f}")
    # transcripts hold the subagents' files too: the revoke must have left no marker in any of the second run's
    second = {e["session_id"] for e in events2 if e.get("session_id")}
    files = [f for sid in second for p in glob.glob(str(PROJECTS / "*" / f"{sid}*")) for f in ([Path(p)] if os.path.isfile(p) else Path(p).rglob("*")) if f.is_file()]
    check(all(m not in f.read_text(errors="replace") for f in files for m in MARKERS.values()), f"neither marker appears in any of the {len(files)} transcript files of the second run")
finally:
    scratch = sum(f.stat().st_size for f in TMP.rglob("*") if f.is_file())
    left = subprocess.run(["pgrep", "-f", str(TMP)], capture_output=True, text=True).stdout.split()
    for pid in left:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
    if not args.keep:
        for path in glob.glob(str(PROJECTS / f"*{re.sub('[^A-Za-z0-9]', '-', TMP.name)}*")):
            shutil.rmtree(path, ignore_errors=True)
        for path in transcript_files():
            shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"scratch {scratch} bytes {'kept' if args.keep else 'deleted'}; processes left behind: {len(left)}")
print("FAILED: " + "; ".join(failures) if failures else "all checks passed")
sys.exit(1 if failures else 0)
