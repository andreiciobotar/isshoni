#!/usr/bin/env python3
"""Staged proof of the churning rule, end to end. Run by hand, never by unittest (the name does not start with `test`):

    python3 factory/tests/live_pause_churning.py [--keep] [--factory PATH]

It spends no money. Everything happens in a temp dir: a bare origin, a checkout with five task worktrees (real git), and a
scratch FACTORY_HOME and HOME. `gh` and `claude` are never started: the script answers both in process (a small GitHub
that merges with real git, a courier that records the lines it is asked to send, a site manager that decides `paused`,
a tech lead that merges the default branch in). The script refuses to run when FACTORY_HOME names the real home.

The story: A merges and sends H (the holder) back once; B merges and H conflicts again while W, still open, changes the
same file; the site manager pauses H for W; W merges; the script releases H and serves it first, so L, handed over after
the release, waits; H lands before L."""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = Path("~/.claude/factory").expanduser()
DISPATCHER = "dispatcher-pocket"
URL = "https://example.test/pocket/pull/{}"
NUMBERS = {"alpha": 1, "bravo": 2, "holder": 3, "waiter": 4, "late": 5}
BASE = "l1\nl2\nl3\nl4\nl5\n"
failures = []


def say(text=""):
    print(text, flush=True)


def check(ok, what):
    say(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def sh(*argv, cwd, ok=(0,)):
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if p.returncode not in ok:
        sys.exit(f"{' '.join(argv)} exited {p.returncode} in {cwd}:\n{p.stdout}{p.stderr}")
    return p


class Hub:
    """GitHub and claude in memory; git is real."""

    def __init__(self, factory, origin, checkout):
        self.f, self.origin, self.checkout = factory, origin, checkout
        self.real_run = factory.run
        self.open_prs, self.merged, self.draft, self.comments, self.lines, self.unknown = set(), [], {}, [], [], []
        self.models = []  # what each `claude -p` model run was: "site-manager" or "tech-lead"
        self.spawned = []

    def slug(self, n):
        return next(s for s, i in NUMBERS.items() if i == n)

    def head(self, slug):
        return sh("git", "rev-parse", f"refs/heads/worktree-{slug}", cwd=self.origin).stdout.strip()

    def entry(self, slug):
        n = NUMBERS[slug]
        merged = n in self.merged
        return {"number": n, "url": URL.format(n), "state": "MERGED" if merged else "OPEN", "isDraft": self.draft.get(n, False),
                "headRefOid": self.head(slug), "headRefName": f"worktree-{slug}", "baseRefName": "main",
                "isCrossRepository": False, "author": {"login": "bot", "is_bot": True}, "latestReviews": [], "reviews": [],
                "comments": []}

    def __call__(self, argv, cwd=None, timeout=None, input=None, env=None, group=False):
        if argv[0] == "gh":
            return self.gh(argv[1:])
        if argv[0] == "claude":
            return self.claude(argv[1:], cwd)
        return self.real_run(argv, cwd=cwd, timeout=timeout, input=input, env=env, group=group)

    def gh(self, a):
        if a[:2] == ["pr", "list"]:
            states = a[a.index("--state") + 1] if "--state" in a else "open"
            branch = a[a.index("--head") + 1] if "--head" in a else None
            found = [self.entry(self.slug(n)) for n in sorted(self.open_prs, reverse=True)]
            found = [e for e in found if (states == "all" or e["state"] == "OPEN") and branch in (None, e["headRefName"])]
            return 0, json.dumps(found), ""
        n = next((int(x) for x in a[2:] if x.isdigit()), None)
        if a[:2] == ["pr", "checks"]:
            return 0, json.dumps([{"name": "test", "bucket": "pass"}]), ""
        if a[:2] == ["pr", "view"]:
            e = self.entry(self.slug(n))
            conflict = sh("git", "merge-tree", "--write-tree", "origin/main", e["headRefOid"], cwd=self.checkout, ok=(0, 1)).returncode
            return 0, json.dumps({**e, "mergeable": "CONFLICTING" if conflict else "MERGEABLE"}), ""
        if a[:2] == ["pr", "ready"]:
            target = [x for x in a[2:] if not x.startswith("-")][0]
            number = int(target) if target.isdigit() else NUMBERS[target.removeprefix("worktree-")]
            self.draft[number] = "--undo" in a
            return 0, "", ""
        if a[:2] == ["pr", "comment"]:
            self.comments.append((n, a[a.index("--body") + 1]))
            return 0, "", ""
        if a[:2] == ["pr", "merge"]:
            sh("git", "fetch", "-q", "origin", cwd=self.checkout)
            sh("git", "merge", "-q", "--no-ff", "--no-edit", f"origin/worktree-{self.slug(n)}", cwd=self.checkout)
            sh("git", "push", "-q", "origin", "main", cwd=self.checkout)
            self.merged.append(n)
            return 0, "", ""
        self.unknown.append(a)
        return 1, "", f"gh shim: unknown call {a}"

    def claude(self, a, cwd):
        if a[:2] == ["agents", "--json"]:
            return 0, json.dumps([{"sessionId": "sid-d", "name": DISPATCHER, "cwd": self.checkout, "kind": "interactive"}]), ""
        if a[:2] == ["plugin", "list"]:
            return 0, "[]", ""
        if a[:1] != ["-p"]:
            self.unknown.append(["claude", *a])
            return 1, "", "claude shim: unknown call"
        prompt = a[-1]
        if prompt.startswith("Send one message"):
            self.lines.append(re.search(r"nothing else: (.*)\. Then reply", prompt, re.S)[1])
            return 0, json.dumps({"result": "sent"}), ""
        if prompt.startswith("Job: watch"):
            self.models.append("site-manager")
            decisions = []
            for firing in json.loads(prompt.partition("\n\n")[2]):
                waits = re.findall(r"(\S+) \((?:working|handed-over)\) on", firing["detail"])
                decisions.append({"id": firing["id"], "decision": "paused", "reason": f"{', '.join(waits)} will land within minutes",
                                  "looked": [], "waits_on": waits})
            return 0, json.dumps({"total_cost_usd": 0, "structured_output": {"sent": True, "decisions": decisions}}), ""
        self.models.append("tech-lead")
        sh("git", "merge", "-q", "-X", "ours", "--no-edit", "origin/main", cwd=cwd)
        answer = {"outcome": "resolved", "note": "Merged origin/main in once, kept this task's line."}
        return 0, json.dumps({"session_id": "staged", "total_cost_usd": 0, "structured_output": answer}), ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--factory", default=str(HERE.parent / "bin" / "factory"), help="the factory script")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir and print its path")
    args = ap.parse_args()
    preset = os.environ.get("FACTORY_HOME")
    if preset and Path(preset).expanduser().resolve() == REAL_HOME.resolve():
        sys.exit(f"refusing to run: FACTORY_HOME is the real home {REAL_HOME}; this proof makes its own scratch home")
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="live-pause-churning-"))
    os.environ.update(FACTORY_HOME=os.path.join(tmp, "home"), HOME=os.path.join(tmp, "user"), CLAUDE_CODE_SESSION_ID="sid-d",
                      GIT_CONFIG_GLOBAL=os.path.join(tmp, "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    os.environ.pop("FACTORY_ROLE", None)
    Path(tmp, "gitconfig").write_text("[user]\n\tname = Staged\n\temail = staged@example.test\n[commit]\n\tgpgsign = false\n"
                                      "[init]\n\tdefaultBranch = main\n")
    loader = importlib.machinery.SourceFileLoader("factory_bin", args.factory)
    factory = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_bin", loader))
    loader.exec_module(factory)
    if not factory.other_home() or factory.STORE.expanduser().resolve() == REAL_HOME.resolve():
        sys.exit("refusing to run: the factory's home is the real one")
    try:
        return run(factory, tmp)
    finally:
        if args.keep:
            say(f"kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def run(factory, tmp):
    origin, checkout = os.path.join(tmp, "origin.git"), os.path.join(tmp, "pocket")
    os.makedirs(os.environ["FACTORY_HOME"])
    sh("git", "init", "-q", "--bare", origin, cwd=tmp)
    sh("git", "clone", "-q", origin, checkout, cwd=tmp, ok=(0, 128))
    Path(checkout, "f.txt").write_text(BASE)
    Path(checkout, ".gitignore").write_text(".claude/\n")
    sh("git", "add", "-A", cwd=checkout)
    sh("git", "commit", "-q", "-m", "base", cwd=checkout)
    sh("git", "push", "-q", "-u", "origin", "main", cwd=checkout)
    sh("git", "remote", "set-head", "origin", "main", cwd=checkout)
    os.chdir(checkout)
    hub = Hub(factory, origin, checkout)
    specs = Path(os.environ["FACTORY_HOME"], "specs", "pocket")
    specs.mkdir(parents=True)
    patches = {"run": hub, "spawn": lambda argv, cwd, out: hub.spawned.append(argv), "POLL": 0.01, "run_clean": lambda: 0}
    saved = {k: getattr(factory, k) for k in patches}
    for k, v in patches.items():
        setattr(factory, k, v)
    try:
        story(factory, hub, checkout, specs)
    finally:
        for k, v in saved.items():
            setattr(factory, k, v)
    say("FAILED: " + "; ".join(failures) if failures else "all expectations met")
    return 1 if failures else 0


def story(factory, hub, checkout, specs):
    def repo():
        return factory.Repo()

    def task(slug, line, edit, state="working"):
        """A worktree off main with one commit pushed to its branch; its pull request exists and is a draft."""
        tree = os.path.join(checkout, ".claude", "worktrees", slug)
        sh("git", "worktree", "add", "-q", "-b", f"worktree-{slug}", tree, "main", cwd=checkout)
        text = Path(tree, "f.txt").read_text().split("\n")
        text[line] = edit
        Path(tree, "f.txt").write_text("\n".join(text))
        sh("git", "commit", "-qam", f"{slug}: {edit}", cwd=tree)
        sh("git", "push", "-q", "-u", "origin", f"worktree-{slug}", cwd=tree)
        hub.open_prs.add(NUMBERS[slug])
        hub.draft[NUMBERS[slug]] = True
        (specs / f"{slug}.md").write_text(f"# {slug}\n")
        factory.declare(repo(), slug, "lead", state, deliverable=URL.format(NUMBERS[slug]) if state == "handed-over" else None)

    def hand_over(slug):
        factory.declare(repo(), slug, "lead", "handed-over", deliverable=URL.format(NUMBERS[slug]))
        hub.draft[NUMBERS[slug]] = False

    def call(*argv, quiet=False):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = factory.main(list(argv))
        for line in [] if quiet else (out.getvalue() + err.getvalue()).splitlines():
            say(f"    | {line}")
        return code, out.getvalue()

    def tech_lead():
        argv = hub.spawned.pop(0)
        say(f"    tech lead start: factory {' '.join(argv[2:5])} ...")
        return call(*argv[2:])

    def rows(slug, **match):
        return [r for r in factory.read_log("tasks") if r["slug"] == slug and all(r.get(k) == v for k, v in match.items())]

    def events(kind):
        return [e for e in factory.read_log("events") if e["event"] == kind]

    def state(slug):
        return factory.read_tasks(repo().root)[slug]

    word = {"all": time.time() + 3600, "prs": {}}
    factory.write_word(repo(), word)
    task("alpha", 3, "A4")
    task("holder", 3, "H4")
    task("waiter", 0, "W1")
    for slug in ("alpha", "holder"):
        hand_over(slug)
    say("1. alpha and holder are handed over; the window is open. alpha merges, holder conflicts: its first send-back, to a tech lead.")
    call("lander", "--dispatcher", DISPATCHER)
    check(hub.merged == [1], f"A merged first (merged: {hub.merged})")
    check(len(factory.queue_send_backs(repo(), "holder")) == 1, "H was sent back once by the queue")
    check(len(hub.spawned) == 1, "a tech lead run was started for it")
    tech_lead()
    check(state("holder")["state"] == "handed-over" and hub.models == ["tech-lead"], "the tech lead resolved it and H is handed over again")

    say("2. B (off the new main) is handed over after H and takes the same line; it merges first (the queue goes by number).")
    task("bravo", 3, "B4")
    hand_over("bravo")
    call("lander", "--dispatcher", DISPATCHER)
    check(hub.merged == [1, 2], f"B merged (merged: {hub.merged})")
    check(len(factory.queue_send_backs(repo(), "holder")) == 1 and 3 in map(int, factory.read_lander(repo())["waiting"]),
          "H conflicts again but is held in waiting for the site manager's decision, not sent back")

    say("3. The tick: `churning` fires (H, W open on f.txt), the site manager decides paused for W, the queue then sends H back.")
    code, out = call("watch", "--dispatcher", DISPATCHER)
    check(hub.models == ["tech-lead", "site-manager"], f"one site manager run (models: {hub.models})")
    [decision] = [r for r in factory.read_log("firings") if r.get("by") == "site-manager"]
    check((decision["decision"], decision["waits_on"]) == ("paused", ["waiter"]), f"decision logged: paused, waits_on {decision.get('waits_on')}")
    check(len(factory.queue_send_backs(repo(), "holder")) == 2 and len(hub.spawned) == 1, "H was sent back (second time) and a tech lead start followed")
    before = len(hub.models)
    tech_lead()
    check(len(hub.models) == before, "the tech lead start ran no model")
    h = state("holder")
    check((h["state"], h["role"], h["waits_on"], h.get("paused")) == ("blocked", "tech-lead", ["waiter"], True), f"H is blocked and paused: {h['state']} waits_on {h.get('waits_on')} paused {h.get('paused')}")
    check(hub.draft[3] is True, "H's pull request is a draft")
    [paused] = events("TaskPaused")
    check((paused["slug"], paused["waits_on"], paused["files"], paused["sent_back"]) == ("holder", ["waiter"], ["f.txt"], 2),
          f"TaskPaused event: waits_on {paused['waits_on']} files {paused['files']} sent_back {paused['sent_back']}")
    _, status = call("status", "--json", quiet=True)
    row = next(r for r in json.loads(status)["rows"] if r["slug"] == "holder")
    check(row["paused"]["waits_on"] == ["waiter"] and row["prioritized"] is None, f"status --json: paused {row['paused']}, prioritized {row['prioritized']}")

    say("4. W is handed over and merges while H waits; the next drain releases H and starts its tech lead run.")
    hand_over("waiter")
    call("lander", "--dispatcher", DISPATCHER)
    check(hub.merged == [1, 2, 4], f"W merged (merged: {hub.merged})")
    check(not hub.spawned, "H is not released while W is still open")
    task("late", 3, "L4")
    call("lander", "--dispatcher", DISPATCHER)
    check("holder" in factory.read_lander(repo())["prioritized"] and len(events("TaskPrioritized")) == 1, "H is released: prioritized, one TaskPrioritized event")
    check(len(hub.spawned) == 1 and hub.spawned[0][5].startswith("the tasks it waited for (waiter) have landed"), "a tech lead run was started with the release evidence")

    say("5. L (the same line) is handed over after the release: the hold keeps it waiting; H resolves, hands over and lands first.")
    hand_over("late")
    call("lander", "--dispatcher", DISPATCHER)
    check(hub.merged == [1, 2, 4], f"nothing merged while H is not handed over (merged: {hub.merged})")
    tech_lead()
    check(state("holder")["state"] == "handed-over" and hub.models.count("tech-lead") == 2, "the tech lead resolved H after the release")
    call("lander", "--dispatcher", DISPATCHER)
    check(hub.merged[:4] == [1, 2, 4, 3], f"H merged before L (merged: {hub.merged})")
    check(len(factory.queue_send_backs(repo(), "holder")) == 2, "H was not sent back a third time")
    [ended] = events("TaskPrioritizedEnd")
    check(ended["reason"] == "merged" and factory.read_lander(repo())["prioritized"] == {}, f"priority ended: {ended['reason']}")
    check(len(factory.queue_send_backs(repo(), "late")) == 1 and 5 not in hub.merged, "L, behind H on the same line, was sent back for its own conflict (the hold ended with H's merge)")
    check(not hub.unknown, f"gh and claude calls the shim did not know: {hub.unknown or 'none'}")

    say("6. Counted.")
    call("pauses")
    call("firings")


if __name__ == "__main__":
    sys.exit(main())
