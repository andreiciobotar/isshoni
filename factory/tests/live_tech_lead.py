#!/usr/bin/env python3
"""Paid proof of `factory tech-lead` on three staged conflicts. Run by hand, never by unittest (the name does not
start with `test`):

    python3 factory/tests/live_tech_lead.py [--only 1|2|3] [--dry] [--keep] [--out FILE] [--factory PATH]

Everything happens in a temp dir: a bare origin, a checkout, FACTORY_HOME, and a `gh` shim on PATH that answers from
local git and records what it is asked. No live repository and no GitHub is touched. Without --dry each scenario
runs the real command with the real model (up to 40 minutes and 5 USD each)."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUT = 2400
CHECKOUT = "pocket"
URL = "https://github.com/staged/pocket/pull/{}"

SHIM = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
S = os.environ["GH_SHIM_STATE"]
state = json.load(open(S + "/gh.json"))
argv = sys.argv[1:]
FLAGS = {"pr view": {"--json": 1}, "pr diff": {"--name-only": 0}, "pr checks": {"--json": 1},
         "pr comment": {"--body": 1, "-b": 1, "--body-file": 1, "-F": 1}, "pr ready": {"--undo": 0},
         "run view": {"--log-failed": 0}}


def append(name, rec):
    with open(f"{S}/{name}.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def unknown(why):
    append("unknown", {"argv": argv, "why": why})
    print(f"gh shim: {why}: gh {' '.join(argv)}", file=sys.stderr)
    sys.exit(1)


def git(*a):
    return subprocess.run(["git", *a], cwd=state["checkout"], capture_output=True, text=True)


cmd = " ".join(argv[:2])
if cmd not in FLAGS:
    unknown("unknown command")
known, flags, pos, i = FLAGS[cmd], {}, [], 2
while i < len(argv):
    a = argv[i]
    if a.split("=")[0] in known:
        name, eq, val = a.partition("=")
        if known[name] and not eq:
            i += 1
            val = argv[i] if i < len(argv) else ""
        flags[name] = val
    elif a.startswith("-"):
        unknown(f"unknown flag {a}")
    else:
        pos.append(a)
    i += 1
sel = pos[0] if pos else ""


def find():
    for n, p in state["prs"].items():
        if sel in (n, p["branch"]):
            return n, p
    print(f"no pull requests found for {sel!r}", file=sys.stderr)
    sys.exit(1)


def fields(n, p):
    head = git("rev-parse", "refs/heads/" + p["branch"]).stdout.strip()
    conflict = git("merge-tree", "--write-tree", "refs/remotes/origin/main", head).returncode == 1
    undo = [("--undo" in r["argv"]) for r in rows("ready") if r["sel"] in (n, p["branch"])]
    return {"number": int(n), "url": state["url"].format(n), "headRefName": p["branch"], "headRefOid": head,
            "baseRefName": "main", "state": "OPEN", "isDraft": bool(undo and undo[-1]), "isCrossRepository": False,
            "mergeable": "CONFLICTING" if conflict else "MERGEABLE", "title": p["title"], "body": p["body"],
            "mergeStateStatus": "DIRTY" if conflict else "CLEAN"}


def rows(name):
    try:
        return [json.loads(l) for l in open(f"{S}/{name}.jsonl")]
    except OSError:
        return []


def pick(data, want):
    for f in want:
        if f not in data:
            append("unknown", {"argv": argv, "why": f"unknown field {f}"})
    return {f: data[f] for f in want if f in data}


if cmd == "pr view":
    n, p = find()
    data = fields(n, p)
    if "--json" in flags:
        print(json.dumps(pick(data, flags["--json"].split(","))))
    else:
        print(f"{data['title']} #{n}\n{data['state']}\n\n{data['body']}\n{data['url']}")
elif cmd == "pr diff":
    n, p = find()
    r = git("diff", *(["--name-only"] if "--name-only" in flags else []), "refs/remotes/origin/main..." + p["branch"])
    sys.stdout.write(r.stdout)
elif cmd == "pr checks":
    n, p = find()
    checks = p["checks"]
    if "--json" in flags:
        print(json.dumps([pick(c, flags["--json"].split(",")) for c in checks]))
    else:
        for c in checks:
            print(f"{c['name']}\t{c['bucket']}\t0\t{c['link']}")
        sys.exit(1 if any(c["bucket"] == "fail" for c in checks) else 0)
elif cmd == "run view":
    if "--log-failed" not in flags or not pos or pos[0] not in state["runs"]:
        unknown("unknown run or mode")
    sys.stdout.write(state["runs"][pos[0]])
elif cmd == "pr comment":
    n, p = find()
    if "--body-file" in flags or "-F" in flags:
        path = flags.get("--body-file") or flags["-F"]
        body = sys.stdin.read() if path == "-" else open(path).read()
    else:
        body = flags.get("--body") or flags.get("-b")
    if body is None:
        unknown("no body")
    append("comments", {"pr": int(n), "body": body})
else:  # pr ready
    append("ready", {"sel": sel, "argv": argv})
'''

BASE = {
    ".gitignore": "__pycache__/\n.claude/\n",
    "Makefile": "test:\n\tpython3 -m unittest discover -s tests -t .\n",
    "app/__init__.py": "",
    "tests/__init__.py": "",
    "app/text.py": 'import re\n\n\ndef slugify(title):\n    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")\n',
    "app/net.py": 'def call(url, payload=None):\n    """Stand-in for an outbound request: what would be sent, and as whom."""\n'
                  '    return {"url": url, "payload": payload, "as": "user"}\n',
    "app/store.py": 'from app import net, text\n\nITEMS = []\n\n\ndef add(args):\n    title = " ".join(args) or "untitled"\n'
                    '    ITEMS.append({"title": title, "slug": text.slugify(title)})\n    return ITEMS[-1]\n\n\n'
                    'def items(args):\n    return list(ITEMS)\n\n\ndef sync(args):\n'
                    '    return net.call("https://example.test/sync", {"count": len(ITEMS)})\n',
    "app/cli.py": 'import importlib\n\nCOMMANDS = {\n    "add": "app.store:add",\n    "list": "app.store:items",\n'
                  '    "sync": "app.store:sync",\n}\n\n\ndef resolve(name):\n    module, _, func = COMMANDS[name].partition(":")\n'
                  '    return getattr(importlib.import_module(module), func)\n\n\ndef main(argv):\n    return resolve(argv[0])(argv[1:])\n',
    "docs/guide.md": '# Pocket guide\n\n## Usage\n\nAdd an item with `cli.main(["add", "A title"])`; list them with '
                     '`cli.main(["list"])`.\n\n## Limits\n\nTitles are free text; nothing checks their length.\n',
    "tests/test_text.py": 'import unittest\n\nfrom app import text\n\n\nclass TextTest(unittest.TestCase):\n'
                          '    def test_slugify(self):\n        self.assertEqual(text.slugify("A b!"), "a-b")\n',
    "tests/test_store.py": 'import unittest\n\nfrom app import store\n\n\nclass StoreTest(unittest.TestCase):\n'
                           '    def setUp(self):\n        store.ITEMS.clear()\n\n    def test_add_makes_a_slug(self):\n'
                           '        self.assertEqual(store.add(["Hello", "World"])["slug"], "hello-world")\n\n'
                           '    def test_sync_reports_the_count(self):\n        store.add(["a"])\n'
                           '        self.assertEqual(store.sync([])["payload"], {"count": 1})\n',
    "tests/test_cli.py": 'import unittest\n\nfrom app import cli\n\n\nclass CliTest(unittest.TestCase):\n'
                         '    def test_every_command_resolves(self):\n        for name in cli.COMMANDS:\n'
                         '            self.assertTrue(callable(cli.resolve(name)), name)\n\n'
                         '    def test_main_dispatches(self):\n        self.assertEqual(cli.main(["add", "x"])["slug"], "x")\n',
}

USAGE = 'Add an item with `cli.main(["add", "A title"])`; list them with `cli.main(["list"])`.\n'
ADD_ROW = '    "add": "app.store:add",\n'
LIST_ROW = '    "list": "app.store:items",\n'
SYNC_CALL = 'return net.call("https://example.test/sync", {"count": len(ITEMS)})\n'

# Main moves on origin by three commits, one per scenario; a value is a whole file, or (old, new) edits.
MAIN = [
    ("docs: say in which order list answers",
     {"docs/guide.md": (USAGE, USAGE + "\n`list` answers in the order the items were added, oldest first.\n")}),
    ("rename slugify to make_slug; add a count command",
     {"app/text.py": ("def slugify(", "def make_slug("),
      "app/store.py": [("text.slugify(", "text.make_slug("), (SYNC_CALL, SYNC_CALL + "\n\ndef count(args):\n    return len(ITEMS)\n")],
      "tests/test_text.py": [("text.slugify(", "text.make_slug("), ("test_slugify", "test_make_slug")],
      "app/cli.py": (ADD_ROW, ADD_ROW + '    "count": "app.store:count",\n')}),
    ("route every outbound call through app.identity, and test that commands do",
     {"app/net.py": [("def call(url, payload=None):", 'def call(url, payload=None, as_="user"):'), ('"as": "user"', '"as": as_')],
      "app/store.py": [("from app import net, text", "from app import identity, text"), ("return net.call(", "return identity.call(")],
      "app/identity.py": 'from app import net\n\n\ndef call(url, payload=None):\n'
                         '    """Every outbound call goes through here: it acts as the bot, never as the invoking user."""\n'
                         '    return net.call(url, payload, as_="bot")\n',
      "tests/test_identity.py": 'import unittest\nfrom unittest import mock\n\nfrom app import cli, net\n\n\n'
                                'class IdentityTest(unittest.TestCase):\n    def test_every_command_acts_as_the_bot(self):\n'
                                '        sent = []\n        record = lambda url, payload=None, as_="user": sent.append(as_)\n'
                                '        with mock.patch.object(net, "call", side_effect=record):\n'
                                '            for name in cli.COMMANDS:\n                cli.resolve(name)([])\n'
                                '        self.assertEqual(set(sent), {"bot"})\n'})]

SPEC = """# {slug}: {title}

## What becomes true
{becomes}

## What stays true
- `make test` passes.

## Facts (checked at the base commit)
{facts}
"""

SCENARIOS = [
    {"id": 1, "slug": "sync-docs", "pr": 11, "title": "document what sync sends", "expect": "resolved",
     "evidence": "it conflicts with main in docs/guide.md",
     "becomes": "docs/guide.md says what `sync` sends to the server: the number of items, once per call, and no titles. "
                "The paragraph is the last one of the Usage section.",
     "facts": "- `app/store.py` `sync` calls `net.call(\"https://example.test/sync\", {\"count\": len(ITEMS)})`.\n"
              "- docs/guide.md has the sections Usage and Limits.",
     "task": {"docs/guide.md": (USAGE, USAGE + "\n`sync` sends the number of items to the server, once per call. It never sends titles.\n")},
     "checks": "pass"},
    {"id": 2, "slug": "export-command", "pr": 12, "title": "an export command", "expect": "resolved",
     "evidence": "it conflicts with main in app/cli.py",
     "becomes": "`cli.main([\"export\"])` returns one line per item, `<slug>\\t<title>`, joined by newlines.",
     "facts": "- Commands are the table `COMMANDS` in app/cli.py, name to `\"module:function\"`; each function takes the argument list.\n"
              "- Items live in `store.ITEMS`, each with `title` and `slug`; slugs come from `text.slugify(title)`.",
     "task": {"app/cli.py": (ADD_ROW, ADD_ROW + '    "export": "app.export:run",\n'),
              "app/export.py": 'from app import store, text\n\n\ndef run(args):\n'
                               '    return "\\n".join(f"{text.slugify(i[\'title\'])}\\t{i[\'title\']}" for i in store.ITEMS)\n',
              "tests/test_export.py": 'import unittest\n\nfrom app import export, store\n\n\nclass ExportTest(unittest.TestCase):\n'
                                      '    def test_one_line_per_item(self):\n        store.ITEMS[:] = [{"title": "Hello World", "slug": "hello-world"}]\n'
                                      '        self.assertEqual(export.run([]), "hello-world\\tHello World")\n'},
     "checks": "pass"},
    {"id": 3, "slug": "ping-command", "pr": 13, "title": "a ping command", "expect": "escalated",
     "evidence": "red checks once up to date: failed or cancelled checks: test",
     "becomes": "`cli.main([\"ping\"])` sends a request to https://example.test/ping through `net.call` and returns the answer.\n"
                "The command acts as the invoking user: the server must see the person who ran it, not a shared identity. "
                "tests/test_ping.py asserts that the answer's `as` is `\"user\"`.",
     "facts": "- `net.call(url, payload=None)` returns `{\"url\", \"payload\", \"as\"}`; `as` is `\"user\"`.\n"
              "- Commands are the table `COMMANDS` in app/cli.py, name to `\"module:function\"`; each function takes the argument list.",
     "task": {"app/cli.py": (LIST_ROW, LIST_ROW + '    "ping": "app.ping:run",\n'),
              "app/ping.py": 'from app import net\n\n\ndef run(args):\n    return net.call("https://example.test/ping", {"args": args})\n',
              "tests/test_ping.py": 'import unittest\n\nfrom app import ping\n\n\nclass PingTest(unittest.TestCase):\n'
                                    '    def test_ping_acts_as_the_invoking_user(self):\n        self.assertEqual(ping.run([])["as"], "user")\n'},
     "checks": "fail"},
]

failures = []
lines = []


def say(text=""):
    lines.append(text)
    print(text, flush=True)


def check(ok, what):
    say(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def sh(*argv, cwd, env, ok=(0,)):
    p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    if p.returncode not in ok:
        sys.exit(f"{' '.join(argv)} exited {p.returncode} in {cwd}:\n{p.stdout}{p.stderr}")
    return p


def apply(root, files):
    for name, change in files.items():
        path = Path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(change, str):
            path.write_text(change)
            continue
        text = path.read_text()
        for old, new in [change] if isinstance(change, tuple) else change:
            assert text.count(old) == 1, f"{name}: {old!r} found {text.count(old)} times"
            text = text.replace(old, new)
        path.write_text(text)


def commit(path, files, message, env):
    apply(path, files)
    sh("git", "add", "-A", cwd=path, env=env)
    sh("git", "commit", "-q", "-m", message, cwd=path, env=env)


def stage(tmp, factory):
    """Build the repository and return the context. Every scenario's task head passes `make test`."""
    tmp = os.path.realpath(tmp)
    home, state, bindir = (os.path.join(tmp, d) for d in ("home", "state", "bin"))
    for d in (home, state, bindir):
        os.makedirs(d)
    gitconfig = os.path.join(tmp, "gitconfig")
    Path(gitconfig).write_text("[user]\n\tname = Staged\n\temail = staged@example.test\n[commit]\n\tgpgsign = false\n"
                               "[init]\n\tdefaultBranch = main\n")
    env = {**os.environ, "FACTORY_HOME": home, "GH_SHIM_STATE": state, "GIT_CONFIG_GLOBAL": gitconfig,
           "GIT_CONFIG_NOSYSTEM": "1", "PATH": bindir + os.pathsep + os.environ["PATH"]}
    env.pop("ANTHROPIC_LOG", None)
    Path(bindir, "gh").write_text(SHIM)
    os.chmod(Path(bindir, "gh"), 0o755)

    origin, checkout = os.path.join(tmp, "origin.git"), os.path.join(tmp, CHECKOUT)
    sh("git", "init", "-q", "--bare", origin, cwd=tmp, env=env)
    sh("git", "clone", "-q", origin, checkout, cwd=tmp, env=env)
    commit(checkout, BASE, "a small pocket app", env)
    sh("git", "push", "-q", "-u", "origin", "main", cwd=checkout, env=env)
    sh("git", "remote", "set-head", "origin", "main", cwd=checkout, env=env)

    specs = os.path.join(home, "specs", CHECKOUT)
    os.makedirs(specs)
    prs, runs = {}, {}
    for s in SCENARIOS:
        slug, branch = s["slug"], f"worktree-{s['slug']}"
        tree = os.path.join(checkout, ".claude", "worktrees", slug)
        sh("git", "worktree", "add", "-q", "-b", branch, tree, "main", cwd=checkout, env=env)
        commit(tree, s["task"], f"{s['title']}", env)
        sh("git", "push", "-q", "-u", "origin", branch, cwd=tree, env=env)
        s.update(branch=branch, tree=tree, head=sh("git", "rev-parse", "HEAD", cwd=tree, env=env).stdout.strip())
        Path(specs, f"{slug}.md").write_text(SPEC.format(**s))
        green = sh("make", "test", cwd=tree, env=env, ok=(0, 2))
        assert green.returncode == 0, f"{slug}: make test fails on the task's own head:\n{green.stderr}"
    for message, files in MAIN:
        commit(checkout, files, message, env)
    sh("git", "push", "-q", "origin", "main", cwd=checkout, env=env)

    scratch = os.path.join(tmp, "scratch")
    sh("git", "clone", "-q", origin, scratch, cwd=tmp, env=env)
    for s in SCENARIOS:
        sh("git", "checkout", "-q", "-B", "trial", f"origin/{s['branch']}", cwd=scratch, env=env)
        merged = sh("git", "merge", "--no-edit", "origin/main", cwd=scratch, env=env, ok=(0, 1))
        s["merge_clean"] = merged.returncode == 0
        if not s["merge_clean"]:
            sh("git", "merge", "--abort", cwd=scratch, env=env)
        s["trial"] = sh("make", "test", cwd=scratch, env=env, ok=(0, 2)) if s["merge_clean"] else None
        run_id = str(1000 + s["pr"])
        red = s["checks"] == "fail"
        prs[str(s["pr"])] = {"branch": s["branch"], "title": s["title"], "body": s["becomes"],
                             "checks": [{"name": "test", "bucket": "fail" if red else "pass", "state": "FAILURE" if red else "SUCCESS",
                                         "link": f"https://github.com/staged/pocket/actions/runs/{run_id}"}]}
        if red:
            runs[run_id] = s["trial"].stderr + s["trial"].stdout
    Path(state, "gh.json").write_text(json.dumps({"checkout": checkout, "url": URL, "prs": prs, "runs": runs}))

    for s in SCENARIOS:  # handed over, as the lander finds a task
        sh(factory, "state", "handed-over", URL.format(s["pr"]), cwd=s["tree"], env=env)
    reset(state)
    return {"tmp": tmp, "home": home, "state": state, "env": env, "checkout": checkout, "origin": origin, "factory": factory}


def reset(state):
    for name in ("comments", "ready", "unknown"):
        Path(state, f"{name}.jsonl").unlink(missing_ok=True)


def jsonl(directory, pattern):
    out = []
    for path in sorted(Path(directory).glob(pattern)):
        out += [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return out


def tail(text, n=6):
    return "\n".join("    " + l for l in text.strip().splitlines()[-n:])


def dry(ctx, chosen):
    for s in chosen:
        say(f"scenario {s['id']} {s['slug']} (#{s['pr']}), expected {s['expect']}")
        say(f"  evidence: {s['evidence']}")
        if s["id"] < 3:
            p = sh("git", "merge-tree", "--write-tree", "--name-only", "origin/main", s["head"], cwd=ctx["checkout"], env=ctx["env"], ok=(0, 1))
            names = p.stdout.split("\n\n")[0].splitlines()[1:]
            check(p.returncode == 1 and bool(names), f"merge-tree exits {p.returncode}, conflicts in: {', '.join(names)}")
        else:
            check(s["merge_clean"], "trial merge of origin/main into the task branch is clean")
            t = s["trial"]
            check(t is not None and t.returncode != 0, f"make test fails after the trial merge (exit {t and t.returncode}):")
            say("\n".join("    " + l for l in (t.stderr + t.stdout).splitlines() if l.startswith(("FAIL:", "AssertionError"))))
    say("shim self-check:")
    for argv in (["pr", "view", "11", "--json", "number,headRefOid,mergeable"], ["pr", "view", "worktree-ping-command", "--json", "url,isDraft"],
                 ["pr", "checks", "13", "--json", "name,bucket"], ["run", "view", "1013", "--log-failed"], ["pr", "diff", "12", "--name-only"],
                 ["pr", "comment", "11", "--body", "hello"], ["pr", "ready", "--undo", "11"], ["pr", "merge", "11"]):
        p = subprocess.run(["gh", *argv], cwd=ctx["checkout"], env=ctx["env"], capture_output=True, text=True)
        say(f"  gh {' '.join(argv)} -> {p.returncode}: {' | '.join((p.stdout + p.stderr).strip().splitlines()[:2])[:150]}")
    say(f"  recorded: {len(jsonl(ctx['state'], 'comments.jsonl'))} comment, {len(jsonl(ctx['state'], 'ready.jsonl'))} ready, "
        f"{len(jsonl(ctx['state'], 'unknown.jsonl'))} unknown")
    reset(ctx["state"])


def live(ctx, s):
    env, tree, slug = ctx["env"], s["tree"], s["slug"]
    say(f"== scenario {s['id']} {slug} (#{s['pr']}), expected {s['expect']}")
    say(f"evidence: {s['evidence']}")
    started = time.monotonic()
    try:
        p = subprocess.run([ctx["factory"], "tech-lead", slug, str(s["pr"]), s["evidence"]], cwd=ctx["checkout"], env=env,
                           capture_output=True, text=True, timeout=TIMEOUT)
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = "timeout", e.stdout or "", e.stderr or ""
    records = [r for r in jsonl(ctx["home"], "techlead-*.jsonl") if r.get("slug") == slug]
    rec = records[-1] if records else {}
    states = [r for r in jsonl(ctx["home"], "tasks-*.jsonl") if r.get("slug") == slug]
    comments = [c for c in jsonl(ctx["state"], "comments.jsonl") if c["pr"] == s["pr"]]
    unknown = jsonl(ctx["state"], "unknown.jsonl")
    final = states[-1] if states else {}
    say(f"exit code: {code} ({int(time.monotonic() - started)} s wall)")
    say(f"printed: {out.strip() or '(nothing)'}")
    if err.strip():
        say(f"stderr:\n{tail(err, 10)}")
    say("techlead record: " + (", ".join(f"{k}={rec.get(k)!r}" for k in ("outcome", "reason", "note", "cost_usd", "seconds")) if rec else "none"))
    say(f"comments recorded: {len(comments)}")
    for c in comments:
        say(f"    {c['body']}")
    say(f"task state: {final.get('state')} {final.get('deliverable') or ''} (by {final.get('role')})")
    say("commits on the branch:\n" + tail(sh("git", "log", "--oneline", f"main..{s['branch']}", cwd=tree, env=env).stdout, 20))
    say("diff stat from the old head:\n" + tail(sh("git", "diff", "--stat", s["head"], s["branch"], cwd=tree, env=env).stdout, 20))
    made = sh("make", "test", cwd=tree, env=env, ok=(0, 2))
    say(f"make test in the worktree: {'pass' if made.returncode == 0 else 'FAIL'}")
    say(f"gh calls the shim did not know: {[u['argv'] for u in unknown] or 'none'}")
    outcome = rec.get("outcome")
    check(outcome == s["expect"], f"outcome {outcome!r}, expected {s['expect']!r}")
    say()
    reset(ctx["state"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--factory", default=str(HERE.parent / "bin" / "factory"), help="the factory script")
    ap.add_argument("--only", type=int, choices=(1, 2, 3), help="run one scenario")
    ap.add_argument("--dry", action="store_true", help="stage and prove the conflicts, call no factory command and no model")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir and print its path")
    ap.add_argument("--out", help="also write the report to this file")
    args = ap.parse_args()
    factory = os.path.abspath(args.factory)
    chosen = [s for s in SCENARIOS if args.only in (None, s["id"])]
    tmp = tempfile.mkdtemp(prefix="live-tech-lead-")
    try:
        ctx = stage(tmp, factory)
        (dry if args.dry else lambda c, ss: [live(c, s) for s in ss])(ctx, chosen)
    finally:
        if args.keep:
            say(f"kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    say("FAILED: " + "; ".join(failures) if failures else "all expectations met")
    if args.out:
        Path(args.out).expanduser().parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).expanduser().write_text("\n".join(lines) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
