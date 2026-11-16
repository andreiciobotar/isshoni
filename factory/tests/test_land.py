import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from test_factory import NO_ATTRIBUTION, factory

BIN = Path(__file__).resolve().parent.parent / "bin" / "factory"

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
d, a = os.environ["FAKE_STATE"], sys.argv[1:]
read = lambda name: json.load(open(f"{d}/{name}.json"))
if a[:2] == ["pr", "list"]:
    print(json.dumps(read("prs").get(a[a.index("--head") + 1], [])))
elif a[:2] == ["pr", "view"]:
    print(json.dumps({"mergeable": read("mergeable").get(a[2], "MERGEABLE")}))
elif a[:2] in (["pr", "edit"], ["pr", "comment"]):
    if a[1] == "edit" and os.environ.get("FAKE_GH_FAIL_EDIT"):
        print("edit refused", file=sys.stderr)
        sys.exit(1)
    rec = {"argv": a}
    if "--body-file" in a:
        rec["body"] = open(a[a.index("--body-file") + 1]).read()
    open(f"{d}/gh.jsonl", "a").write(json.dumps(rec) + "\\n")
elif a[:2] == ["pr", "ready"]:
    open(f"{d}/ready.jsonl", "a").write(json.dumps(a) + "\\n")
elif a[:1] == ["api"]:
    print("[]")
else:
    sys.exit(1)
"""

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, subprocess, sys
d, a = os.environ["FAKE_STATE"], sys.argv[1:]
REPLY = "## Result\\n`%s\\n\\n## Facts\\n- conflicts: a.txt - merged by hand\\n- tests: `make test` pass\\n\\n" \\
        "## Needs the lead\\n- a.txt:1 both sides\\n\\n## Factory feedback\\n- the brief was fine\\n"
if a[:1] == ["agents"]:
    print(open(f"{d}/agents.json").read())
elif a[:2] == ["plugin", "list"]:
    print('[{"id": "alpha@m", "enabled": true}, {"id": "factory@factory", "enabled": true}]')
elif a[:1] == ["-p"]:
    open(f"{d}/claude.jsonl", "a").write(json.dumps({"argv": a, "cwd": os.path.realpath(os.getcwd())}) + "\\n")
    mode = os.environ["FAKE_LANDER"]
    if mode == "fail":
        sys.exit(1)
    if mode == "sleep":
        import time
        time.sleep(30)
    if mode in ("push", "lie"):
        subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "resolved"], check=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], check=True)
    if mode == "garbage-merging":
        subprocess.run(["git", "merge", "--no-commit", "origin/main"])
    says = {"push": "LANDED, LOOK https://x/pull/t1`", "lie": "LANDED https://x/pull/t1`",
            "lie-nopush": "LANDED https://x/pull/t1`", "stop": "STOPPED conflict` (hunks below)"}
    if mode in ("garbage", "garbage-merging"):
        print("not json")
    else:
        print(json.dumps({"result": REPLY % says[mode]}))
else:
    sys.exit(3)
"""

BUMP = """#!/bin/sh
set -e
cd "$1"
git fetch -q origin
git checkout -q -B worktree-t1 origin/worktree-t1
git commit -q --allow-empty -m "someone else"
git push -q origin worktree-t1
"""


FACTORY = factory


def lines(n=30):
    return [f"line {i}" for i in range(1, n + 1)]


class LandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name).resolve()
        self.remote, self.repo, self.other = tmp / "remote.git", tmp / "repo", tmp / "other"
        self.home, self.state, bindir = tmp / "home", tmp / "state", tmp / "bin"
        for d in (self.home, self.state, bindir):
            d.mkdir()
        for name, text in (("claude", FAKE_CLAUDE), ("gh", FAKE_GH), ("bump", BUMP)):
            tool = bindir / name
            tool.write_text(text)
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.bump = bindir / "bump"
        self.env = {**os.environ, "HOME": str(self.home), "FACTORY_HOME": str(self.home / ".claude" / "factory"),
                    "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_STATE": str(self.state), "FAKE_LANDER": "stop", "TEST_CMD": "true",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t"}
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.prs, self.mergeable = {}, {}
        self.sessions([])
        self.write_state()
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=tmp)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=tmp)
        (self.repo / "Makefile").write_text("test:\n\t@$(TEST_CMD)\n")
        (self.repo / "docs").mkdir()
        for name in ("a.txt", "b.txt", "README.md", "docs/a.md"):
            (self.repo / name).write_text("\n".join(lines()) + "\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "main")
        self.git("remote", "set-head", "origin", "main")
        self.git("clone", "-q", str(self.remote), str(self.other), cwd=tmp)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def sessions(self, sessions):
        (self.state / "agents.json").write_text(sessions if isinstance(sessions, str) else json.dumps(sessions))

    def write_state(self):
        (self.state / "prs.json").write_text(json.dumps(self.prs))
        (self.state / "mergeable.json").write_text(json.dumps(self.mergeable))

    def edit(self, root, line, text, name="a.txt"):
        path = root / name
        content = path.read_text().splitlines()
        content[line - 1] = text
        path.write_text("\n".join(content) + "\n")
        self.git("commit", "-q", "-am", f"{name}:{line}", cwd=root)

    def advance_main(self, line, text, name="a.txt"):
        self.git("pull", "-q", "--ff-only", cwd=self.other)
        self.edit(self.other, line, text, name)
        self.git("push", "-q", "origin", "main", cwd=self.other)

    def task(self, slug, line=20, text="task line", base="main", state="handed-over", start="main", name="a.txt", number=None):
        wt = self.repo / ".claude" / "worktrees" / slug
        self.git("worktree", "add", "-q", "-b", f"worktree-{slug}", str(wt), start)
        self.edit(wt, line, text, name)
        self.git("push", "-q", "-u", "origin", f"worktree-{slug}", cwd=wt)
        url = f"https://x/pull/{number or slug}"
        self.prs[f"worktree-{slug}"] = [{"state": "OPEN", "url": url, "baseRefName": base,
                                         "headRefOid": self.git("rev-parse", "HEAD", cwd=wt)}]
        self.write_state()
        if state == "handed-over":
            self.factory("state", "handed-over", url, cwd=wt)
        return wt

    def factory(self, *args, cwd=None, **env):
        return subprocess.run([sys.executable, str(BIN), *args], cwd=cwd or self.repo, env={**self.env, **env},
                              capture_output=True, text=True)

    def land(self, *args, **env):
        return self.factory("land", *args, **env)

    def declared(self):
        files = sorted((self.home / ".claude/factory").glob("tasks-*.jsonl"))
        return [json.loads(line) for f in files for line in f.read_text().splitlines()]

    def last_state(self, slug="t1"):
        return [r["state"] for r in self.declared() if r["slug"] == slug][-1]

    def remote_head(self, branch="worktree-t1"):
        return self.git("rev-parse", branch, cwd=self.remote)

    def gh_calls(self):
        path = self.state / "gh.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def claude_calls(self):
        path = self.state / "claude.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def result(self, p):
        return p.stdout.splitlines()[1] if p.stdout.startswith("## Result") else p.stdout

    def busy_session(self, wt, **extra):
        return {"id": "s1", "sessionId": "s1", "kind": "background", "cwd": str(wt), "status": "busy",
                "state": "working", **extra}

    def test_clean_merge_without_overlap_lands_with_no_model(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        old = self.git("rev-parse", "HEAD", cwd=wt)
        p = self.land("t1")
        self.assertEqual((p.returncode, p.stderr, self.result(p)), (0, "", "`LANDED https://x/pull/t1`"))
        self.assertIn("## Needs the lead\nnone", p.stdout)
        self.assertNotEqual(self.remote_head(), old)
        self.assertEqual(self.remote_head(), self.git("rev-parse", "HEAD", cwd=wt))
        self.assertEqual(self.git("rev-parse", "origin/main", cwd=wt), self.git("merge-base", "HEAD", "origin/main", cwd=wt))
        self.assertEqual(self.git("rev-parse", f"{self.remote_head()}^1", cwd=wt), old)
        self.assertEqual(len(self.git("rev-list", "--parents", "-1", self.remote_head(), cwd=wt).split()), 3)
        self.assertEqual(self.last_state(), "handed-over")
        (comment,) = self.gh_calls()
        self.assertEqual(comment["argv"][:3], ["pr", "comment", "https://x/pull/t1"])
        self.assertIn(f"`{old}` -> `{self.remote_head()}`", comment["body"])
        self.assertIn("The reviewed commits are unchanged.", comment["body"])
        self.assertNotIn("Range-diff", comment["body"])
        self.assertIn("Result: LANDED.", comment["body"])
        self.assertNotIn("lgtm", comment["body"].lower())
        self.assertEqual(self.claude_calls(), [])

    def test_overlap_lands_with_a_look_and_holds_the_slot(self):
        wt = self.task("t1", line=20)
        self.advance_main(2, "main line")
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (1, "`LANDED, LOOK https://x/pull/t1`"))
        self.assertEqual(self.remote_head(), self.git("rev-parse", "HEAD", cwd=wt))
        self.assertIn("overlap: a.txt", p.stdout)
        self.assertIn("the base changed files the task also changes: a.txt", p.stdout)
        self.assertEqual(self.last_state(), "working")
        self.assertIn("Result: LANDED, LOOK (the lead has been asked to look).", self.gh_calls()[0]["body"])

    def test_overlap_prints_none_safe_and_look(self):
        wt = self.task("t1", line=20, name="README.md")
        self.advance_main(1, "main line", "b.txt")
        self.git("fetch", "-q", "origin", cwd=wt)
        base = self.git("merge-base", "HEAD", "origin/main", cwd=wt)
        head = self.git("rev-parse", "HEAD", cwd=wt)
        p = self.factory("overlap", base, head, "origin/main", cwd=wt)
        self.assertEqual((p.returncode, p.stdout), (0, "overlap: none\nverdict: safe\n"))
        self.advance_main(2, "main line", "README.md")
        self.git("fetch", "-q", "origin", cwd=wt)
        p = self.factory("overlap", base, head, "origin/main", cwd=wt)
        self.assertEqual((p.returncode, p.stdout),
                         (0, "overlap: README.md (prose, 17 lines apart, not looked at)\nverdict: safe\n"))
        self.advance_main(3, "main line")
        self.edit(wt, 4, "task line")
        self.git("fetch", "-q", "origin", cwd=wt)
        head = self.git("rev-parse", "HEAD", cwd=wt)
        p = self.factory("overlap", base, head, "origin/main", cwd=wt)
        self.assertEqual(p.stdout.splitlines()[:3], [
            "overlap: README.md (prose, 17 lines apart, not looked at); a.txt (code)", "verdict: look", "a.txt: code"])
        self.assertIn("the base changed:\ndiff --git a/a.txt b/a.txt", p.stdout)
        self.assertIn("the task changed:\ndiff --git a/a.txt b/a.txt", p.stdout)
        self.assertNotIn("README.md:", p.stdout)

    def test_overlap_with_a_bad_ref_fails_and_is_never_safe(self):
        wt = self.task("t1")
        p = self.factory("overlap", "nosuchref", self.git("rev-parse", "HEAD", cwd=wt), "origin/main", cwd=wt)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nosuchref", p.stderr)
        self.assertNotIn("verdict", p.stdout)

    def overlap(self, wt, base=None):
        """`factory overlap` of the task's head against origin/main, run in the task worktree."""
        self.git("fetch", "-q", "origin", cwd=wt)
        base = base or self.git("merge-base", "HEAD", "origin/main", cwd=wt)
        return self.factory("overlap", base, self.git("rev-parse", "HEAD", cwd=wt), "origin/main", cwd=wt)

    def test_overlap_far_apart_in_prose_is_safe(self):
        wt = self.task("t1", line=20, name="README.md")
        self.advance_main(2, "main line", "README.md")
        p = self.overlap(wt)
        self.assertEqual(p.stdout, "overlap: README.md (prose, 17 lines apart, not looked at)\nverdict: safe\n")

    def test_overlap_far_apart_in_code_is_a_look(self):
        wt = self.task("t1", line=20)
        self.advance_main(2, "main line")
        p = self.overlap(wt)
        self.assertEqual(p.stdout.splitlines()[:3], ["overlap: a.txt (code)", "verdict: look", "a.txt: code"])
        self.assertIn("+main line", p.stdout)
        self.assertIn("+task line", p.stdout)

    def test_overlap_near_in_prose_is_a_look(self):
        wt = self.task("t1", line=4, name="docs/a.md")
        self.advance_main(2, "main line", "docs/a.md")
        p = self.overlap(wt)
        self.assertEqual(p.stdout.splitlines()[:3],
                         ["overlap: docs/a.md (prose, 1 lines apart)", "verdict: look", "docs/a.md: prose, 1 lines apart"])

    def test_overlap_of_the_same_line_in_prose_is_a_look_0_lines_apart(self):
        wt = self.task("t1", line=5, name="README.md")
        self.advance_main(5, "main line", "README.md")
        p = self.overlap(wt)
        self.assertEqual(p.stdout.splitlines()[:2], ["overlap: README.md (prose, 0 lines apart)", "verdict: look"])

    def test_overlap_of_a_prose_file_renamed_on_one_side_is_a_look(self):
        wt = self.task("t1", line=20, name="docs/a.md")
        self.git("pull", "-q", "--ff-only", cwd=self.other)
        self.git("mv", "docs/a.md", "docs/b.md", cwd=self.other)
        self.git("commit", "-q", "-m", "rename", cwd=self.other)
        self.git("push", "-q", "origin", "main", cwd=self.other)
        p = self.overlap(wt)
        self.assertEqual(p.stdout.splitlines()[:3], ["overlap: docs/a.md (added, deleted or renamed on one side)",
                                                     "verdict: look", "docs/a.md: added, deleted or renamed on one side"])

    def test_a_prose_path_with_non_ascii_letters_is_judged_as_prose(self):
        (self.repo / "docs/café.md").write_text("\n".join(lines()) + "\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "add café")
        self.git("push", "-q", "origin", "main")
        wt = self.task("t1", line=20, name="docs/café.md")
        self.advance_main(2, "main line", "docs/café.md")
        p = self.overlap(wt)
        self.assertEqual(p.stdout, "overlap: docs/café.md (prose, 17 lines apart, not looked at)\nverdict: safe\n")

    def test_a_long_diff_is_cut_with_the_count_of_the_rest(self):
        wt = self.task("t1", line=20)
        self.advance_main(2, "main line")
        self.git("fetch", "-q", "origin", cwd=wt)
        base = self.git("merge-base", "HEAD", "origin/main", cwd=wt)
        (wt / "a.txt").write_text("\n".join(f"new {i}" for i in range(100)) + "\n")
        self.git("commit", "-q", "-am", "rewrite", cwd=wt)
        p = self.factory("overlap", base, self.git("rev-parse", "HEAD", cwd=wt), "origin/main", cwd=wt)
        self.assertRegex(p.stdout, r"\.\.\. \(\d+ more lines\)\n$")

    def test_a_base_that_has_not_moved_changes_nothing(self):
        wt = self.task("t1")
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (0, "`LANDED https://x/pull/t1`"))
        self.assertIn("has not moved", p.stdout)
        self.assertEqual((self.gh_calls(), self.claude_calls(), self.last_state()), ([], [], "handed-over"))
        self.assertEqual(self.remote_head(), self.git("rev-parse", "HEAD", cwd=wt))

    def conflict(self):
        wt = self.task("t1", line=1, text="task line")
        self.advance_main(1, "main line")
        return wt, self.git("rev-parse", "HEAD", cwd=wt)

    def test_lean_start_off_gives_the_lander_its_old_argv(self):
        (self.home / ".claude" / "factory").mkdir(parents=True)
        (self.home / ".claude" / "factory" / "settings.toml").write_text('[sessions]\nlean_start = false\n')
        self.conflict()
        self.land("t1")
        (call,) = self.claude_calls()
        self.assertFalse(any(a.startswith("--strict") for a in call["argv"]))
        self.assertEqual(json.loads(call["argv"][call["argv"].index("--settings") + 1]), NO_ATTRIBUTION)

    def test_the_lander_runs_on_the_configured_model(self):
        (self.home / ".claude" / "factory").mkdir(parents=True)
        (self.home / ".claude" / "factory" / "settings.toml").write_text('[models]\nlander = "claude-haiku-4-5"\n')
        self.conflict()
        self.land("t1")
        (call,) = self.claude_calls()
        self.assertEqual(call["argv"][call["argv"].index("--model") + 1], "claude-haiku-4-5")

    def test_a_broken_settings_file_stops_the_land_before_anything_is_declared(self):
        (self.home / ".claude" / "factory").mkdir(parents=True)
        (self.home / ".claude" / "factory" / "settings.toml").write_text('[sessions]\nlean_start = "no"\n')
        self.conflict()
        p = self.land("t1")
        self.assertEqual(p.returncode, 2)
        self.assertIn("lean_start", p.stderr)
        self.assertEqual((self.last_state(), self.claude_calls()), ("handed-over", []))

    def test_a_conflict_goes_to_the_lander_in_the_worktree(self):
        wt, old = self.conflict()
        p = self.land("t1", FAKE_LANDER="push")
        self.assertEqual((p.returncode, self.result(p)), (1, "`LANDED, LOOK https://x/pull/t1`"))
        (call,) = self.claude_calls()
        argv = call["argv"]
        self.assertEqual(call["cwd"], str(wt))
        self.assertEqual(argv[:5], ["-p", "--agent", "factory:lander", "--plugin-dir", str(BIN.parent.parent)])
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        i = argv.index("--strict-mcp-config")
        self.assertEqual(argv[i:i + 3], ["--strict-mcp-config", "--settings", json.dumps(
            {**NO_ATTRIBUTION, "disableBundledSkills": True, "enabledPlugins": {"alpha@m": False}})])
        tools = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        self.assertEqual(tools, ["Read", "Edit", "Grep", "Glob", "Bash(git *)", "Bash(make *)", "Bash(gh pr view *)",
                                 "Bash(gh pr edit *)", "Bash(factory overlap *)"])
        self.assertFalse([t for t in FACTORY.LAND_TOOLS if t.startswith("Bash(factory state")])
        agent = (BIN.parent.parent / "agents" / "lander.md").read_text()
        for banned in ("`factory state working`", "factory state handed-over", "factory retro <"):
            self.assertNotIn(banned, agent)
        self.assertEqual(argv[argv.index("--disallowedTools") + 1:-3], list(FACTORY.ROLE_DENIED))
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        brief = argv[-1]
        for text in ("Your task slug is t1.", "The base is main (remote origin).", f"The reviewed head is {old}.",
                     "There is no pull request title or body file.", "a.txt"):
            self.assertIn(text, brief)
        self.assertNotIn("head last pushed", brief)
        self.assertNotIn("has merged", brief)
        self.assertNotIn("authorised", brief)
        self.assertNotEqual(self.remote_head(), old)
        self.assertIn("- conflicts: a.txt - merged by hand", p.stdout)
        self.assertIn(f"the conflict resolution: `git show --remerge-diff {self.remote_head()}`", p.stdout)
        self.assertNotIn("Factory feedback", p.stdout)
        self.assertNotIn("the brief was fine", p.stdout)
        self.assertEqual(self.last_state(), "working")
        (comment,) = self.gh_calls()
        self.assertIn("conflicts: a.txt - merged by hand", comment["body"])

    def queued(self):
        """The `queue` records of the run, without their `ts`, in the temporary store."""
        files = sorted((self.home / ".claude/factory").glob("queue-*.jsonl"))
        return [{k: v for k, v in json.loads(line).items() if k != "ts"} for f in files for line in f.read_text().splitlines()]

    def number_the_pull_request(self, number=12):
        self.prs["worktree-t1"][0]["url"] = f"https://x/pull/{number}"
        self.write_state()

    def test_the_queue_records_a_clean_base_merge_and_nothing_when_the_base_has_not_moved(self):
        wt = self.task("t1")
        self.number_the_pull_request()
        self.land("t1")
        self.assertEqual(self.queued(), [])
        self.advance_main(1, "main line", "b.txt")
        old = self.git("rev-parse", "HEAD", cwd=wt)
        self.land("t1")
        self.assertEqual(self.queued(), [{"repo": str(self.repo), "slug": "t1", "pr": 12, "head": old,
                                          "step": "land-updated", "base": "origin/main"}])

    def test_the_queue_records_a_conflicting_base_merge_with_its_files(self):
        wt, old = self.conflict()
        self.number_the_pull_request()
        self.land("t1")
        self.assertEqual(self.queued(), [{"repo": str(self.repo), "slug": "t1", "pr": 12, "head": old,
                                          "step": "land-conflict", "base": "origin/main", "files": ["a.txt"]}])

    def test_a_lander_that_stops_pushes_nothing(self):
        wt, old = self.conflict()
        p = self.land("t1", FAKE_LANDER="stop")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED conflict`"))
        self.assertEqual((self.remote_head(), self.git("rev-parse", "HEAD", cwd=wt), self.git("status", "--porcelain", cwd=wt)),
                         (old, old, ""))
        self.assertIn("a.txt:1 both sides", p.stdout)
        self.assertEqual((self.gh_calls(), self.last_state()), ([], "working"))

    def test_the_script_settles_a_lander_that_says_landed_from_the_remote(self):
        _, old = self.conflict()
        p = self.land("t1", FAKE_LANDER="lie")
        self.assertEqual(self.result(p), "`LANDED, LOOK https://x/pull/t1`")
        self.assertNotEqual(self.remote_head(), old)

    def test_a_lander_that_says_landed_without_a_push_is_a_stop(self):
        self.conflict()
        p = self.land("t1", FAKE_LANDER="lie-nopush")
        self.assertEqual(self.result(p), "`STOPPED other`")

    def test_garbage_from_the_lander_is_a_stop(self):
        _, old = self.conflict()
        p = self.land("t1", FAKE_LANDER="garbage")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED other`"))
        self.assertEqual(self.remote_head(), old)
        self.assertIn("the lander's reply could not be read", p.stdout)

    def test_an_unreadable_reply_with_a_merge_left_in_progress_says_so(self):
        wt, old = self.conflict()
        p = self.land("t1", FAKE_LANDER="garbage-merging")
        self.assertEqual(self.result(p), "`STOPPED other`")
        self.assertIn("the lander's reply could not be read", p.stdout)
        self.assertIn(f"the worktree is at `{old}`, uncommitted changes: some, merge in progress: yes", p.stdout)
        self.assertEqual(self.remote_head(), old)

    def test_failing_tests_leave_the_merge_commit_unpushed(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        old = self.git("rev-parse", "HEAD", cwd=wt)
        p = self.land("t1", TEST_CMD="echo boom; exit 1")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED tests`"))
        self.assertIn("boom", p.stdout)
        new = self.git("rev-parse", "HEAD", cwd=wt)
        self.assertNotEqual(new, old)
        self.assertIn(f"on the merge commit at {new}, unpushed", p.stdout)
        self.assertEqual(self.git("rev-parse", f"{new}^1", cwd=wt), old)
        self.assertEqual((self.remote_head(), self.gh_calls(), self.last_state()), (old, [], "working"))

    def test_a_busy_lead_stops_before_anything_changes(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        old = self.git("rev-parse", "HEAD", cwd=wt)
        for sessions in ([self.busy_session(wt)], [self.busy_session(wt, kind="interactive")], "not json"):
            self.sessions(sessions)
            p = self.land("t1")
            self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED busy`"), sessions)
            self.assertEqual((self.git("rev-parse", "HEAD", cwd=wt), self.remote_head()), (old, old))
            self.assertEqual(self.last_state(), "handed-over")
        self.assertEqual(self.claude_calls(), [])

    def test_a_lead_busy_at_the_second_check_is_not_pushed_over(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        old = self.git("rev-parse", "HEAD", cwd=wt)
        (self.state / "busy.json").write_text(json.dumps([self.busy_session(wt)]))
        p = self.land("t1", TEST_CMD=f"cp {self.state}/busy.json {self.state}/agents.json")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED busy`"))
        self.assertIn("merge commit", p.stdout)
        self.assertEqual((self.remote_head(), self.gh_calls(), self.last_state()), (old, [], "working"))

    def test_a_dirty_worktree_stops(self):
        wt = self.task("t1")
        (wt / "scratch.txt").write_text("x")
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED dirty`"))
        self.assertEqual(self.last_state(), "working")

    def test_a_head_that_is_not_the_pushed_one_stops(self):
        wt = self.task("t1")
        self.git("commit", "-q", "--allow-empty", "-m", "local only", cwd=wt)
        p = self.land("t1")
        self.assertEqual(self.result(p), "`STOPPED other`")
        self.assertIn("is not origin/worktree-t1", p.stdout)

    def test_a_task_that_is_not_handed_over_is_a_usage_error(self):
        self.task("t1", state="working")
        p = self.land("t1")
        self.assertEqual((p.returncode, p.stdout), (2, ""))
        self.assertIn("not handed over", p.stderr)
        p = self.land("nope")
        self.assertEqual((p.returncode, p.stdout), (2, ""))
        self.assertEqual(self.factory("land").returncode, 2)

    def test_a_locked_worktree_lands_as_normal(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        self.git("worktree", "lock", str(wt))
        p = self.land("t1")
        self.assertEqual(self.result(p), "`LANDED https://x/pull/t1`")

    def test_a_merged_base_retargets_onto_the_default_branch(self):
        base = self.task("base", line=3, text="base line", state="working")
        wt = self.task("t1", line=25, text="task line", base="worktree-base", start="worktree-base")
        self.git("pull", "-q", "--ff-only", cwd=self.other)
        self.git("fetch", "-q", "origin", "worktree-base", cwd=self.other)
        self.git("merge", "-q", "--no-ff", "-m", "merge base", "origin/worktree-base", cwd=self.other)
        self.git("push", "-q", "origin", "main", cwd=self.other)
        self.git("config", "branch.worktree-t1.factoryBase", "worktree-base")
        self.prs["worktree-base"] = [{"state": "MERGED", "url": "https://x/pull/base", "baseRefName": "main",
                                      "headRefOid": self.git("rev-parse", "HEAD", cwd=base)}]
        self.write_state()
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (0, "`LANDED https://x/pull/t1`"))
        self.assertEqual(self.git("merge-base", "--is-ancestor", "origin/main", "HEAD", cwd=wt), "")
        edit, comment = self.gh_calls()
        self.assertEqual(edit["argv"], ["pr", "edit", "https://x/pull/t1", "--base", "main"])
        self.assertIn("Merged `origin/main`", comment["body"])

    def test_all_takes_only_conflicting_or_base_merged_tasks(self):
        self.task("t1", line=1)
        self.task("t2", line=10)
        self.task("t3", line=20, state="working")
        self.task("t4", line=25, base="worktree-gone")
        self.task("t5", line=26)
        self.advance_main(30, "main line")
        self.mergeable = {"https://x/pull/t1": "CONFLICTING", "https://x/pull/t3": "CONFLICTING"}
        self.write_state()
        self.git("branch", "worktree-gone", "main")  # a recorded base that merged: t4's
        self.git("config", "branch.worktree-t4.factoryBase", "worktree-gone")
        self.git("config", "branch.worktree-t5.factoryBase", "worktree-gone")  # its pull request is already on main
        self.prs["worktree-gone"] = [{"state": "MERGED", "url": "https://x/pull/gone", "baseRefName": "main",
                                      "headRefOid": "z"}]
        self.write_state()
        p = self.land("--all")
        self.assertEqual((p.returncode, p.stderr), (1, ""))
        self.assertEqual([l for l in p.stdout.splitlines() if l.startswith("# ")], ["# t1", "# t4"])

    def test_all_with_nothing_to_land(self):
        self.task("t1")
        p = self.land("--all")
        self.assertEqual((p.returncode, p.stdout), (0, "nothing to land\n"))

    def test_a_remote_that_moves_before_the_push_is_refused_without_force(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        p = self.land("t1", TEST_CMD=f"{self.bump} {self.other}")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED other`"))
        self.assertIn("git push failed", p.stdout)
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.last_state(), "working")
        self.assertNotEqual(self.remote_head(), self.git("rev-parse", "HEAD", cwd=wt))
        self.assertEqual(self.remote_head(), self.git("rev-parse", "origin/worktree-t1", cwd=self.other))

    def test_a_push_refused_for_workflows_is_denied_with_the_human_command(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        old = self.remote_head()
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'refusing to allow a GitHub App to create or update workflow "
                        "`.github/workflows/test.yml` without `workflows` permission' >&2\nexit 1\n")
        hook.chmod(0o755)
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED denied`"))
        self.assertIn(f"From your own terminal: git -C {wt} push -u origin HEAD", p.stdout)
        self.assertIn("without `workflows` permission", p.stdout)
        self.assertEqual((self.remote_head(), self.gh_calls(), self.last_state()), (old, [], "working"))

    def test_no_test_target_is_a_stop_even_when_a_test_directory_exists(self):
        (self.repo / "Makefile").write_text("all:\n\t@true\n")
        (self.repo / "test").mkdir()
        (self.repo / "test" / "x.txt").write_text("x")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "no target")
        self.git("push", "-q", "origin", "main")
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        old = self.git("rev-parse", "HEAD", cwd=wt)
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED tests`"))
        self.assertIn(FACTORY.TEST_NOT_STATED, p.stdout)
        self.assertEqual((self.remote_head(), self.last_state()), (old, "working"))

    def test_working_is_declared_before_a_look_is_pushed(self):
        self.task("t1", line=20)
        self.advance_main(2, "main line")
        seen = self.state / "state-at-push"
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\npython3 -c \"import glob, json, os; rs = [json.loads(l) for f in sorted(glob.glob(os.environ['HOME'] "
                        "+ '/.claude/factory/tasks-*.jsonl')) for l in open(f)]; "
                        f"print([r['state'] for r in rs if r['slug'] == 't1'][-1])\" > {seen}\n")
        hook.chmod(0o755)
        p = self.land("t1")
        self.assertEqual(self.result(p), "`LANDED, LOOK https://x/pull/t1`")
        self.assertEqual(seen.read_text().strip(), "working")
        self.assertEqual([r["state"] for r in self.declared() if r["slug"] == "t1"], ["handed-over", "working"])

    def test_declaring_working_puts_the_pull_request_back_to_draft(self):
        self.task("t1", line=20)
        self.advance_main(2, "main line")
        self.land("t1")
        ready = self.state / "ready.jsonl"
        self.assertEqual([json.loads(l) for l in ready.read_text().splitlines()],
                         [["pr", "ready", "--undo", "worktree-t1"]])

    def test_declaring_working_puts_the_recorded_pull_request_back_to_draft_by_its_number(self):
        self.task("t1", line=20, number=5)
        self.advance_main(2, "main line")
        self.land("t1")
        ready = self.state / "ready.jsonl"
        self.assertEqual([json.loads(l) for l in ready.read_text().splitlines()], [["pr", "ready", "--undo", "5"]])

    def test_a_pull_request_other_than_the_recorded_one_on_the_branch_is_a_stop_that_names_both(self):
        wt = self.task("t1", number=5)
        self.prs["worktree-t1"][0]["url"] = "https://x/pull/6"
        self.write_state()
        old = self.git("rev-parse", "HEAD", cwd=wt)
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED other`"))
        self.assertIn("https://x/pull/6 is not the pull request recorded for t1 (#5)", p.stdout)
        self.assertIn("`factory state handed-over https://x/pull/6` adopts it", p.stdout)
        self.assertEqual((self.remote_head(), self.git("rev-parse", "HEAD", cwd=wt)), (old, old))

    def test_declaring_working_on_a_branchless_landing_makes_no_gh_call(self):
        with mock.patch.object(factory, "declare") as declare, mock.patch.object(factory, "run") as run:
            factory.Landing(mock.Mock(), {"path": "/x/t1", "branch": None}, False, "auto").mark_working()
        declare.assert_called_once()
        run.assert_not_called()

    def test_a_lander_that_exits_non_zero_is_a_stop_with_what_git_shows(self):
        wt, old = self.conflict()
        p = self.land("t1", FAKE_LANDER="fail")
        self.assertEqual(self.result(p), "`STOPPED other`")
        self.assertIn("the lander run exited 1", p.stdout)
        self.assertIn(f"`{old}`, uncommitted changes: none, merge in progress: no", p.stdout)
        self.assertEqual(self.last_state(), "working")

    def test_a_lander_that_times_out_is_a_stop(self):
        wt, old = self.conflict()
        out, cwd = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo)
        with mock.patch.dict(os.environ, {**self.env, "FAKE_LANDER": "sleep"}), mock.patch.object(factory, "LAND_TIMEOUT", 1), \
                redirect_stdout(out):
            code = factory.main(["land", "t1"])
        self.assertEqual(code, 1)
        self.assertIn("`STOPPED other`", out.getvalue())
        self.assertIn("the lander run timed out", out.getvalue())

    def test_a_merge_that_fails_without_a_conflict_is_a_stop_with_no_model(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        hook = self.repo / ".git" / "hooks" / "pre-merge-commit"
        hook.write_text("#!/bin/sh\necho refused by hook >&2\nexit 1\n")
        hook.chmod(0o755)
        p = self.land("t1")
        self.assertEqual(self.result(p), "`STOPPED other`")
        self.assertIn("refused by hook", p.stdout)
        self.assertEqual(self.claude_calls(), [])

    def test_a_merged_base_is_named_in_the_lander_brief(self):
        self.task("base", line=3, text="base line", state="working")
        self.task("t1", line=1, text="task line", base="worktree-base", start="worktree-base")
        self.git("pull", "-q", "--ff-only", cwd=self.other)
        self.git("fetch", "-q", "origin", "worktree-base", cwd=self.other)
        self.git("merge", "-q", "--no-ff", "-m", "merge base", "origin/worktree-base", cwd=self.other)
        self.git("push", "-q", "origin", "main", cwd=self.other)
        self.advance_main(1, "main line")
        self.git("config", "branch.worktree-t1.factoryBase", "worktree-base")
        self.prs["worktree-base"] = [{"state": "MERGED", "url": "u", "baseRefName": "main", "headRefOid": "z"}]
        self.write_state()
        self.land("t1", FAKE_LANDER="stop")
        (call,) = self.claude_calls()
        self.assertIn("The base is worktree-base (remote origin). The base has merged.", call["argv"][-1])

    def test_a_base_that_still_counts_is_the_target(self):
        base = self.task("base", line=3, text="base line", state="working")
        wt = self.task("t1", line=25, text="task line", base="worktree-base", start="worktree-base")
        self.git("config", "branch.worktree-t1.factoryBase", "worktree-base")
        self.edit(base, 5, "base again")
        self.git("push", "-q", "origin", "worktree-base", cwd=base)
        p = self.land("t1")
        self.assertEqual(self.result(p), "`LANDED, LOOK https://x/pull/t1`")
        self.assertIn("base: `origin/worktree-base`", p.stdout)
        self.assertEqual(self.git("rev-parse", "origin/worktree-base", cwd=wt), self.git("merge-base", "HEAD", "origin/worktree-base", cwd=wt))
        self.assertNotIn("--base", [a for c in self.gh_calls() for a in c["argv"]])
        self.assertIn("`origin/worktree-base`", self.gh_calls()[0]["body"])

    def test_a_merged_base_already_in_the_head_only_retargets(self):
        base = self.task("base", line=3, text="base line", state="working")
        wt = self.task("t1", line=25, text="task line", base="worktree-base", start="worktree-base")
        self.git("pull", "-q", "--ff-only", cwd=self.other)
        self.git("fetch", "-q", "origin", "worktree-base", cwd=self.other)
        self.git("merge", "-q", "--ff-only", "origin/worktree-base", cwd=self.other)
        self.git("push", "-q", "origin", "main", cwd=self.other)
        self.git("config", "branch.worktree-t1.factoryBase", "worktree-base")
        self.prs["worktree-base"] = [{"state": "MERGED", "url": "u", "baseRefName": "main", "headRefOid": "z"}]
        self.write_state()
        old = self.git("rev-parse", "HEAD", cwd=wt)
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (0, "`LANDED https://x/pull/t1`"))
        self.assertIn("nothing pushed", p.stdout)
        (edit,) = self.gh_calls()
        self.assertEqual(edit["argv"], ["pr", "edit", "https://x/pull/t1", "--base", "main"])
        self.assertEqual((self.remote_head(), self.last_state()), (old, "handed-over"))

    def test_a_branch_that_holds_a_merge_commit_lands_again(self):
        wt = self.task("t1", line=20)
        self.advance_main(1, "main line", "b.txt")
        self.git("fetch", "-q", "origin", cwd=wt)
        self.git("merge", "-q", "--no-ff", "--no-commit", "origin/main", cwd=wt)
        (wt / "extra.txt").write_text("in the merge\n")
        self.git("add", ".", cwd=wt)
        self.git("commit", "-q", "-m", "merge main", cwd=wt)
        self.git("push", "-q", "origin", "worktree-t1", cwd=wt)
        self.prs["worktree-t1"][0]["headRefOid"] = self.git("rev-parse", "HEAD", cwd=wt)
        self.write_state()
        self.advance_main(2, "again", "b.txt")
        old = self.git("rev-parse", "HEAD", cwd=wt)
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (0, "`LANDED https://x/pull/t1`"))
        new = self.remote_head()
        self.assertEqual((new, self.git("rev-parse", f"{new}^1", cwd=wt), self.claude_calls()), (self.git("rev-parse", "HEAD", cwd=wt), old, []))
        self.assertEqual((wt / "extra.txt").read_text(), "in the merge\n")

    def test_a_failed_retarget_after_the_push_is_reported_with_one_comment(self):
        wt = self.task("t1", line=25, base="worktree-gone")
        self.advance_main(1, "main line", "b.txt")
        self.git("branch", "worktree-gone", "main")
        self.git("config", "branch.worktree-t1.factoryBase", "worktree-gone")
        self.prs["worktree-gone"] = [{"state": "MERGED", "url": "u", "baseRefName": "main", "headRefOid": "z"}]
        self.write_state()
        p = self.land("t1", FAKE_GH_FAIL_EDIT="1")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED other`"))
        self.assertIn("pushed", p.stdout)
        self.assertIn("edit refused", p.stdout)
        (comment,) = self.gh_calls()
        self.assertEqual(comment["argv"][:2], ["pr", "comment"])
        self.assertIn("Retarget onto main failed", comment["body"])
        self.assertIn("Result: STOPPED other", comment["body"])
        self.assertNotIn("Result: LANDED", comment["body"])
        self.assertEqual(self.last_state(), "working")

    def commands(self, wt, *args):
        return self.factory("commands", *args, cwd=wt)

    def test_a_stated_command_runs_in_a_shell_and_is_named_in_facts_and_comment(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        self.assertEqual(self.commands(wt, "test", "true && echo ok > ran.txt && rm ran.txt").returncode, 0)
        p = self.land("t1", TEST_CMD="exit 1")  # the Makefile rule is not run
        self.assertEqual((p.returncode, self.result(p)), (0, "`LANDED https://x/pull/t1`"))
        self.assertIn("tests: `true && echo ok > ran.txt && rm ran.txt` pass", p.stdout)
        self.assertIn("Tests: `true && echo ok > ran.txt && rm ran.txt` pass.", self.gh_calls()[0]["body"])

    def test_a_failing_stated_command_stops_tests(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        old = self.remote_head()
        self.commands(wt, "test", "echo boom && false")
        p = self.land("t1")
        self.assertEqual((p.returncode, self.result(p)), (1, "`STOPPED tests`"))
        self.assertIn("echo boom && false failed", p.stdout)
        self.assertIn("boom", p.stdout)
        self.assertEqual((self.remote_head(), self.gh_calls()), (old, []))

    def test_no_tests_land_loudly(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        self.commands(wt, "test", "none")
        p = self.land("t1", TEST_CMD="exit 1")
        self.assertEqual((p.returncode, self.result(p)), (0, "`LANDED https://x/pull/t1`"))
        self.assertIn("tests: NO TESTS WERE RUN: none found in this repository", p.stdout)
        self.assertIn("Tests: NO TESTS WERE RUN: none found in this repository.", self.gh_calls()[0]["body"])

    def test_the_makefile_rule_is_the_last_resort_and_its_text_is_unchanged(self):
        self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        p = self.land("t1")
        self.assertIn("tests: `make test` pass", p.stdout)
        self.assertIn("Tests: `make test` pass.", self.gh_calls()[0]["body"])

    def test_resolution_prefers_stated_then_last_handed_over_then_the_makefile(self):
        wt = self.task("t1", state="working")
        self.assertEqual(self.commands(wt).stdout, "test: make test (Makefile rule)\ngolden: none\n")
        self.git("config", "factory.lastTest", "./old.sh")
        self.assertEqual(self.commands(wt).stdout, "test: ./old.sh (last handed over in this repository)\ngolden: none\n")
        self.commands(wt, "test", "./new.sh")
        self.commands(wt, "golden", "./gen.sh")
        self.assertEqual(self.commands(wt).stdout, "test: ./new.sh (stated for this task)\ngolden: ./gen.sh\n")
        self.assertEqual((self.commands(wt, "test").stdout, self.commands(wt, "golden").stdout), ("./new.sh\n", "./gen.sh\n"))
        self.commands(wt, "golden", "none")
        self.assertEqual(self.commands(wt, "golden").stdout, "none\n")
        self.assertEqual(self.commands(self.repo).stdout, "test: ./old.sh (last handed over in this repository)\ngolden: none\n")

    def drop_makefile(self):
        self.git("rm", "-q", "Makefile")
        self.git("commit", "-q", "-m", "no makefile")
        self.git("push", "-q", "origin", "main")

    def test_none_is_a_stated_value_and_not_stated_exits_1_with_the_message(self):
        self.drop_makefile()
        wt = self.task("t1", state="working")
        p = self.commands(wt, "test")
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn(FACTORY.TEST_NOT_STATED, p.stderr)
        self.assertEqual(self.commands(wt).stdout, "test: not stated\ngolden: none\n")
        self.commands(wt, "test", "none")
        self.assertEqual((self.commands(wt, "test").returncode, self.commands(wt, "test").stdout), (0, "none\n"))

    def test_an_empty_command_and_a_setting_outside_a_task_are_refused(self):
        wt = self.task("t1", state="working")
        for value in ("", "  "):
            p = self.commands(wt, "test", value)
            self.assertEqual((p.returncode, p.stdout), (1, ""))
            self.assertIn("empty", p.stderr)
        p = self.commands(self.repo, "test", "./x.sh")
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn("inside a task worktree", p.stderr)

    def test_a_detached_task_worktree_gets_its_own_refusal(self):
        wt = self.task("t1", state="working")
        self.git("checkout", "-q", "--detach", cwd=wt)
        p = self.commands(wt, "test", "./x.sh")
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn("detached HEAD; check out worktree-t1", p.stderr)

    def test_handing_over_records_the_stated_command_for_the_next_task_but_not_none(self):
        wt = self.task("t1", state="working")
        self.commands(wt, "test", "none")
        self.factory("state", "handed-over", "https://x/pull/t1", cwd=wt)
        self.assertEqual(subprocess.run(["git", "config", "--get", "factory.lastTest"], cwd=self.repo, capture_output=True,
                                        text=True).stdout, "")
        self.commands(wt, "test", "./check.sh")
        self.factory("state", "handed-over", "https://x/pull/t1", cwd=wt)
        self.assertEqual(self.git("config", "--get", "factory.lastTest"), "./check.sh")

    def test_the_lander_gets_the_mode_the_deny_rules_and_the_commands(self):
        (self.home / ".claude" / "factory").mkdir(parents=True)
        (self.home / ".claude" / "factory" / "settings.toml").write_text('[sessions]\nlead_permission_mode = "acceptEdits"\n')
        wt, _ = self.conflict()
        self.commands(wt, "test", "./check.sh")
        self.commands(wt, "golden", "./gen.sh")
        self.land("t1")
        (call,) = self.claude_calls()
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("The test command is ./check.sh. The golden command is ./gen.sh.", argv[-1])

    def test_a_conflict_without_a_stated_command_tells_the_lander_to_find_it(self):
        self.drop_makefile()
        self.conflict()
        self.land("t1")
        (call,) = self.claude_calls()
        self.assertIn("The test command is not stated: find how this repository runs its tests and run that.", call["argv"][-1])

    def land_as(self, exports, error=None):
        """`factory land t1` in this process, with the App's exports as `factory env` would give them."""
        out, cwd = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(factory, "identity_exports", lambda: (exports, error)), \
                redirect_stdout(out):
            factory.main(["land", "t1"])
        return out.getvalue()

    def test_fetch_merge_and_push_act_as_the_app(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        seen = self.state / "probe"
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text(f"#!/bin/sh\necho \"$LAND_PROBE\" > {seen}\n")
        hook.chmod(0o755)
        out = self.land_as({"LAND_PROBE": "app", "GIT_COMMITTER_NAME": "bot", "GIT_COMMITTER_EMAIL": "bot@x"})
        self.assertIn("`LANDED https://x/pull/t1`", out)
        self.assertEqual(seen.read_text().strip(), "app")
        self.assertEqual(self.git("log", "-1", "--format=%cn", cwd=wt), "bot")

    def test_a_failed_mint_stops_at_the_first_fetch_with_git_untouched(self):
        wt = self.task("t1")
        self.advance_main(1, "main line", "b.txt")
        old = self.git("rev-parse", "HEAD", cwd=wt)
        touched = self.state / "git-touched"
        hook = self.repo / ".git" / "hooks" / "reference-transaction"
        hook.write_text(f"#!/bin/sh\necho ran >> {touched}\n")
        hook.chmod(0o755)
        out = self.land_as({"HARMLESS": "1"}, error="no App token: the mint was refused")
        self.assertIn("`STOPPED other`", out)
        self.assertIn("git fetch origin failed: no App token: the mint was refused", out)
        self.assertEqual((self.git("rev-parse", "HEAD", cwd=wt), self.remote_head()), (old, old))
        self.assertFalse(touched.exists())
        self.assertEqual(self.gh_calls(), [])


class OverlapRuleTests(unittest.TestCase):
    def verdict(self, base_hunk, task_hunk, path="README.md"):
        diffs = iter([f"@@ {base_hunk} @@", f"@@ {task_hunk} @@"])
        return FACTORY.Overlap.judge(lambda *a: next(diffs), "b", {"base": {path: "M"}, "task": {path: "M"}}, path, "t", "h")

    def test_the_gap_between_hunks_counts_the_unchanged_lines_between_them(self):
        self.assertEqual(FACTORY.hunk_ranges("@@ -5,0 +6 @@\n@@ -9 +9 @@\n@@ -20,3 +20 @@"), [(6, 5), (9, 9), (20, 22)])
        self.assertEqual(FACTORY.hunk_gap((2, 2), (12, 12)), 9)
        self.assertEqual(FACTORY.hunk_gap((12, 12), (2, 2)), 9)
        self.assertEqual(FACTORY.hunk_gap((6, 5), (7, 7)), 1)  # an insertion after line 5, then line 7
        self.assertEqual(FACTORY.hunk_gap((6, 5), (6, 6)), 0)

    def test_nine_lines_apart_is_a_look_and_ten_is_safe(self):
        self.assertEqual(self.verdict("-2 +2", "-12 +12"), ("prose, 9 lines apart", False))
        self.assertEqual(self.verdict("-2 +2", "-13 +13"), ("prose, 10 lines apart", True))
        self.assertEqual(self.verdict("-5,0 +6", "-16 +16"), ("prose, 10 lines apart", True))  # lines 6..15 between
        self.assertEqual(self.verdict("-5,0 +6", "-15 +15"), ("prose, 9 lines apart", False))

    def test_no_hunks_is_a_look(self):
        diffs = iter(["", "@@ -1 +1 @@"])
        self.assertEqual(FACTORY.Overlap.judge(lambda *a: next(diffs), "b", {"base": {"README.md": "M"}, "task": {"README.md": "M"}},
                                               "README.md", "t", "h"), ("no line changes to compare", False))

    def test_prose_is_decided_by_path_alone(self):
        for path in ("README.md", "sub/README.rst", "docs/a.md", "x/doc/y/z.TXT", "docs/deep/b.md"):
            self.assertTrue(FACTORY.is_prose(path), path)
        for path in ("a.md", "docs/a.py", "factory/agents/lander.md", "factory/roles/lead.md", "x/SKILL.md", "CLAUDE.md",
                     "docs.md", "readme.md"):
            self.assertFalse(FACTORY.is_prose(path), path)

    def test_a_failing_git_is_a_fail_with_its_error(self):
        out = FACTORY.checked_out(lambda *a: (128, "", "fatal: bad revision\n"))
        with self.assertRaisesRegex(FACTORY.Fail, "fatal: bad revision"):
            FACTORY.Overlap(out, "nosuchref", "h", "t")


if __name__ == "__main__":
    unittest.main()
