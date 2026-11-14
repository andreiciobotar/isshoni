import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests.support import ROOT
from isshoni import events, kaizen

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "events")
CUTOFF = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
SESSION = "11111111-aaaa-4aaa-8aaa-111111111111"
MAIN = os.path.join("projects", "-fixture-events", SESSION + ".jsonl")
SUBAGENTS = os.path.join("projects", "-fixture-events", SESSION, "subagents")


def at(s):
    return datetime.fromisoformat("2026-11-14T" + s).replace(tzinfo=timezone.utc)


def pr(number, branch, state, created, merged=None, closed=None, rollup=None):
    return {"number": number, "headRefName": branch, "state": state, "createdAt": "2026-11-14T" + created + "Z",
            "mergedAt": merged and "2026-11-14T" + merged + "Z", "closedAt": closed and "2026-11-14T" + closed + "Z",
            "statusCheckRollup": rollup or []}


PRS = {"/r/alpha": [
    pr(11, "worktree-one", "MERGED", "10:10:00", merged="10:20:00"),
    pr(12, "worktree-two", "OPEN", "10:30:00", rollup=[{"status": "IN_PROGRESS", "conclusion": ""}]),
    pr(13, "feature-x", "CLOSED", "10:15:00", closed="10:16:00"),
    {**pr(7, "worktree-old", "OPEN", "10:00:00"), "createdAt": "2026-11-01T09:00:00Z"},
]}


class FakeGh:
    """gh as a function: PRS by repository, GhError for /r/broken."""

    def __init__(self, prs=None):
        self.prs, self.calls = PRS if prs is None else prs, []

    def __call__(self, repo):
        self.calls.append(repo)
        if repo == "/r/broken":
            raise kaizen.GhError("gh exited 1")
        return self.prs.get(repo, [])


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch("sys.stderr", io.StringIO())  # the fake gh's failure line
        patch.start()
        self.addCleanup(patch.stop)
        self.root = os.path.join(self.tmp, "events")
        shutil.copytree(FIXTURE, self.root)
        self.projects, self.factory = os.path.join(self.root, "projects"), os.path.join(self.root, "factory")
        for d, _, files in os.walk(self.root):
            for f in files:
                os.utime(os.path.join(d, f), (CUTOFF.timestamp() + 7200,) * 2)

    def feed(self, gh=None, follow=False, cutoff=CUTOFF):
        return events.Feed(self.projects, self.factory, cutoff, gh, follow, now=lambda: at("11:00:00"))

    def history(self, **kw):
        return self.feed(**kw).history()

    def of(self, kind, evs=None):
        return [e for e in (evs if evs is not None else self.history()) if e["kind"] == kind]


class TestHistory(Env):
    def test_sorted_and_whole(self):
        evs = self.history(gh=FakeGh())
        self.assertEqual([e["ts"] for e in evs], sorted(e["ts"] for e in evs))
        kinds = [e["kind"] for e in evs]
        self.assertEqual({k: kinds.count(k) for k in set(kinds)}, {
            "tool": 9, "tokens": 12, "spawn": 5, "return": 7, "question": 2, "answer": 1, "watch": 2,
            "pr": 4, "merge": 1})

    def test_tool_kinds_and_dedupe(self):
        tools = self.of("tool")  # tu1 appears on two lines and tu5 in two files: once each
        self.assertEqual([(e["ts"].strftime("%H:%M:%S"), e["tool"], e["role"]) for e in tools], [
            ("10:00:01", "shell", "main"), ("10:00:02", "read", "main"), ("10:00:02", "dispatch", "main"),
            ("10:00:03", "other", "main"), ("10:00:04", "message", "main"), ("10:00:06", "edit", "coder"),
            ("10:00:07", "write", "coder"), ("10:03:00", "message", "coder"), ("10:03:10", "message", "reviewer")])
        self.assertEqual(events.TOOL_KIND["bash"], "shell")
        self.assertEqual({events.TOOL_KIND[n] for n in ("Task", "Skill")}, {"dispatch"})
        self.assertEqual({events.TOOL_KIND[n] for n in ("SubagentHandback", "AskUserQuestion")}, {"message"})
        self.assertEqual({events.TOOL_KIND[n] for n in ("Grep", "WebSearch", "ToolSearch")}, {"read"})

    def test_tokens_once_per_message_and_not_synthetic(self):
        toks = self.of("tokens")
        self.assertEqual(len(toks), 12)  # msg_m1 has three lines, msg_c1 two files, msg_m3 is synthetic
        first = toks[0]
        self.assertEqual((first["in"], first["out"], first["cache_read"], first["cache_write"]), (3, 100, 50000, 2000))
        self.assertEqual((first["session"], first["role"]), (SESSION, "main"))
        self.assertNotIn("agent", first)

    def test_spawn_roles_and_parent(self):
        spawns = self.of("spawn")
        self.assertEqual([(e["agent"], e["role"], e.get("parent")) for e in spawns],
                         [("aaa111", "coder", None), ("bbb222", "reviewer", "aaa111"), ("ccc333", "reviewer", None),
                          ("eee555", "coder", None), ("fff666", "reviewer", "eee555")])
        self.assertEqual({e["session"] for e in spawns}, {SESSION})
        self.assertEqual([e["ts"].strftime("%H:%M:%S") for e in spawns], ["10:00:05", "10:00:30", "10:02:00", "10:02:40", "10:03:05"])

    def test_returns_and_verdicts(self):
        rets = self.of("return")
        self.assertEqual([(e["ts"].strftime("%H:%M:%S"), e["agent"], e.get("verdict")) for e in rets], [
            ("10:00:20", "aaa111", None),  # a coder's text mentioning CHANGES_NEEDED has no verdict
            ("10:00:50", "bbb222", "changes"),  # the verdict is in an earlier line of the same message
            ("10:01:30", "bbb222", None),  # a resumed reviewer returns again; no verdict in its text
            ("10:02:10", "ccc333", "approve"),
            ("10:03:00", "eee555", None),  # a handback: a coder has no verdict, a main transcript's handback is no return
            ("10:03:10", "fff666", "changes"),  # the verdict is the handback's message
            ("10:03:11", "fff666", None)])  # it then ends its turn: a second return, once per block and once per message

    def test_question_and_answer(self):
        self.assertEqual([(e["ts"].strftime("%H:%M:%S"), e["slug"]) for e in self.of("question")],
                         [("10:02:00", "one"), ("10:03:30", "two")])  # the repeated blocked record is no new question
        self.assertEqual([(e["ts"].strftime("%H:%M:%S"), e["slug"]) for e in self.of("answer")], [("10:03:00", "one")])

    def test_a_question_older_than_the_cutoff_and_still_open_shows_to_a_follower_only(self):
        late = at("10:04:00")
        self.assertEqual([e for e in self.history(cutoff=late) if e["kind"] == "question"], [])
        got = [(e["slug"], e["ts"]) for e in self.history(cutoff=late, follow=True) if e["kind"] == "question"]
        self.assertEqual(got, [("two", at("10:03:30"))])  # not "one": that was answered

    def test_an_agent_dispatched_before_the_cutoff_and_not_returned_shows_to_a_follower_only(self):
        sub = os.path.join(self.root, SUBAGENTS)
        with open(os.path.join(sub, "agent-abc999.meta.json"), "w") as f:
            f.write('{"agentType":"coder"}')
        with open(os.path.join(sub, "agent-abc999.jsonl"), "w") as f:  # dispatched at 10:01, still working
            f.write('{"type":"user","timestamp":"2026-11-14T10:01:00.000Z","message":{}}\n')
        for name in ("agent-abc999.jsonl", "agent-abc999.meta.json"):  # written, like the fixture's files, after the cutoff
            os.utime(os.path.join(sub, name), (CUTOFF.timestamp() + 7200,) * 2)
        late = at("10:30:00")
        early = lambda **kw: [e["agent"] for e in self.history(cutoff=late, **kw) if e["kind"] == "spawn" and e["ts"] < late]  # noqa: E731
        returned = {e["agent"] for e in self.history() if e["kind"] == "return"}
        self.assertTrue(returned and "abc999" not in returned)
        self.assertEqual(early(), [])
        self.assertEqual(early(follow=True), ["abc999"])  # not the ones that returned

    def test_a_follower_sees_the_open_pull_request_of_a_repository_quiet_since_before_the_cutoff(self):
        gh = FakeGh({"/r/old": [{**pr(5, "worktree-zero", "OPEN", "09:00:00"), "createdAt": "2026-11-13T09:00:00Z"}]})
        self.assertEqual([e for e in self.history(gh=gh) if e["kind"] == "pr"], [])  # /r/old's only record is from 09:59
        got = [(e["repo"], e["pr"], e["slug"], e["state"]) for e in self.history(gh=gh, follow=True) if e["kind"] == "pr"]
        self.assertEqual(got, [("old", 5, "zero", "open")])

    def test_watch_ticks_only(self):
        self.assertEqual([e["ts"].strftime("%H:%M:%S") for e in self.of("watch")], ["10:05:00", "10:35:00"])

    def test_cutoff_filters_events_and_old_files(self):
        late = self.history(cutoff=at("10:00:40"))
        self.assertEqual(min(e["ts"] for e in late), at("10:00:40"))
        old = os.path.join(self.root, SUBAGENTS, "agent-bbb222.jsonl")
        os.utime(old, (CUTOFF.timestamp() - 86400,) * 2)
        self.assertNotIn("bbb222", {e.get("agent") for e in self.history()})

    def test_line_format(self):
        s = events.line(self.of("tokens")[0])
        self.assertEqual(s, '{"ts":"2026-11-14T10:00:01.120Z","kind":"tokens","session":"%s","role":"main",'
                            '"in":3,"out":100,"cache_read":50000,"cache_write":2000}' % SESSION)

    def test_missing_directories_give_no_events(self):
        self.assertEqual(events.Feed(os.path.join(self.tmp, "no"), os.path.join(self.tmp, "none"), CUTOFF).history(), [])


class TestGh(Env):
    def test_pr_merge_closed(self):
        gh = FakeGh()
        evs = self.history(gh=gh)
        got = [(e["ts"].strftime("%H:%M:%S"), e["kind"], e["pr"], e.get("slug"), e.get("state"))
               for e in evs if e["kind"] in ("pr", "merge")]
        self.assertEqual(got, [
            ("10:10:00", "pr", 11, "one", "open"), ("10:15:00", "pr", 13, "feature-x", "open"),
            ("10:16:00", "pr", 13, "feature-x", "closed"), ("10:20:00", "merge", 11, None, None),
            ("10:30:00", "pr", 12, "two", "open")])

    def test_only_repositories_named_in_the_window(self):
        gh = FakeGh()
        self.history(gh=gh)
        self.assertEqual(gh.calls, ["/r/alpha", "/r/broken"])  # /r/old's record is before the cutoff

    def test_gh_failure_is_one_stderr_line_and_the_rest_carries_on(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            feed = self.feed(gh=FakeGh())
            feed.history()
            feed.poll_gh()
        self.assertEqual(err.getvalue().splitlines(), ["isshoni: gh: /r/broken: gh exited 1"])

    def test_no_gh_no_pull_requests(self):
        self.assertEqual(self.of("pr"), [])

    def test_history_has_no_ci_and_no_old_open_pull_request(self):
        evs = self.history(gh=FakeGh())
        self.assertEqual(self.of("ci", evs), [])
        self.assertNotIn(7, {e.get("pr") for e in evs})

    def test_ci_state(self):
        run, ok, bad = {"status": "IN_PROGRESS"}, {"status": "COMPLETED", "conclusion": "SUCCESS"}, {"status": "COMPLETED", "conclusion": "FAILURE"}
        self.assertEqual([events.ci_state(r) for r in ([], None, [run, ok], [ok, ok], [ok, bad, run], [{"state": "PENDING"}], [{"state": "SUCCESS"}])],
                         [None, None, "running", "green", "red", "running", "green"])


class TestGhFetch(unittest.TestCase):
    def run_gh(self, **kw):
        with mock.patch.object(events.subprocess, "run", **kw) as run:
            return events.gh_prs(ROOT), run

    def test_lists_pull_requests_with_the_check_rollup(self):
        prs, run = self.run_gh(return_value=subprocess.CompletedProcess([], 0, '[{"number": 1}]', ""))
        self.assertEqual(prs, [{"number": 1}])
        argv, kw = run.call_args
        self.assertEqual(argv[0], ["gh", "pr", "list", "--state", "all", "--limit", "1000", "--json", events.GH_FIELDS])
        self.assertEqual((kw["cwd"], kw["timeout"]), (ROOT, 20))

    def test_a_missing_repository_path_is_not_asked(self):
        with mock.patch.object(events.subprocess, "run") as run, self.assertRaises(kaizen.GhError) as cm:
            events.gh_prs(os.path.join(ROOT, "no-such-dir"))
        self.assertEqual((str(cm.exception), run.called), ("no such repository path", False))

    def test_failures_become_one_line_reasons(self):
        cases = (({"side_effect": FileNotFoundError(2, "No such file or directory: 'gh'")}, "gh unavailable: [Errno 2]"),
                 ({"side_effect": subprocess.TimeoutExpired("gh", 20)}, "gh unavailable: Command 'gh' timed out after 20 seconds"),
                 ({"return_value": subprocess.CompletedProcess([], 1, "", "none of the git remotes\nsecond line")},
                  "none of the git remotes"),
                 ({"return_value": subprocess.CompletedProcess([], 4, "", "")}, "gh exited 4"),
                 ({"return_value": subprocess.CompletedProcess([], 0, "not json", "")}, "gh printed no JSON"),
                 ({"return_value": subprocess.CompletedProcess([], 0, "{}", "")}, "gh printed an unexpected document"))
        for kw, reason in cases:
            with self.assertRaises(kaizen.GhError) as cm:
                self.run_gh(**kw)
            self.assertTrue(str(cm.exception).startswith(reason), (str(cm.exception), reason))
            self.assertNotIn("\n", str(cm.exception))


class TestFollow(Env):
    def start(self, gh=None, gh_every=60.0):
        self.got, self.lock, self.stop = [], threading.Lock(), threading.Event()

        def emit(batch):
            with self.lock:
                self.got.extend(batch)

        feed = self.feed(gh=gh, follow=True)
        self.thread = threading.Thread(target=events.follow, args=(feed, emit, self.stop, 0.01, 0.02, gh_every))
        self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        self.stop.set()
        self.thread.join(5)

    def seen(self, kind, **match):
        with self.lock:
            return [e for e in self.got if e["kind"] == kind and all(e.get(k) == v for k, v in match.items())]

    def wait(self, kind, **match):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if self.seen(kind, **match):
                return self.seen(kind, **match)
            time.sleep(0.01)
        self.fail("no %s %s event arrived" % (kind, match))

    def append(self, rel, text):
        with open(os.path.join(self.root, rel), "a") as f:
            f.write(text)

    def test_history_then_appended_lines_with_a_partial_one(self):
        self.start()
        self.wait("return", agent="ccc333")  # the history came first
        new = '{"type":"assistant","timestamp":"2026-11-14T10:09:00.000Z","message":{"id":"msg_n1","model":"m","content":[{"type":"tool_use","id":"tu_n1","name":"Grep","input":{}}]}}'
        self.append(MAIN, new[:60])
        time.sleep(0.2)
        self.assertEqual(self.seen("tool", tool="read", ts=at("10:09:00")), [])  # no newline yet: not a line
        self.append(MAIN, new[60:] + "\n")
        self.assertEqual(len(self.wait("tool", tool="read", ts=at("10:09:00"))), 1)

    def test_live_marker_separates_history_from_news(self):
        self.start(gh=FakeGh())
        self.wait("live")
        with self.lock:
            kinds = [e["kind"] for e in self.got]
        self.assertEqual(kinds.count("live"), 1)
        self.assertGreater(kinds.index("live"), max(i for i, k in enumerate(kinds) if k in ("pr", "merge", "spawn")))  # after the first gh look too
        self.append(os.path.join("factory", "watch.log"), "2026-11-14T10:36:00Z watch alpha\n")
        self.wait("watch", ts=at("10:36:00"))
        with self.lock:
            self.assertEqual(self.got[-1]["kind"], "watch")  # news comes after the marker

    def test_truncated_file_is_read_again_from_its_start(self):
        self.start()
        self.wait("live")
        with open(os.path.join(self.root, MAIN), "w") as f:
            f.write('{"type":"assistant","timestamp":"2026-11-14T10:50:00.000Z","message":{"id":"msg_t","model":"m","content":[{"type":"tool_use","id":"tu_t","name":"Edit","input":{}}]}}\n')
        self.wait("tool", tool="edit", ts=at("10:50:00"))

    def test_new_files_and_log_lines(self):
        self.start()
        self.wait("watch", ts=at("10:35:00"))
        sub = os.path.join(self.root, SUBAGENTS)
        with open(os.path.join(sub, "agent-ddd444.meta.json"), "w") as f:
            f.write('{"agentType":"explorer"}')
        with open(os.path.join(sub, "agent-ddd444.jsonl"), "w") as f:
            f.write('{"type":"user","timestamp":"2026-11-14T10:10:00.000Z","message":{}}\n')
        self.append(os.path.join("factory", "watch.log"), "2026-11-14T10:36:00Z watch alpha\n")
        self.append(os.path.join("factory", "tasks-2026-11.jsonl"), '{"ts":"2026-11-14T10:11:00Z","repo":"/r/alpha","slug":"two","state":"working"}\n')
        self.assertEqual(self.wait("spawn", agent="ddd444")[0]["role"], "explorer")
        self.wait("watch", ts=at("10:36:00"))
        self.wait("answer", slug="two")
        self.assertEqual(len(self.seen("watch")), 3)  # the history's two, once each

    def test_gh_first_poll_and_changes(self):
        gh = FakeGh({"/r/alpha": [pr(12, "worktree-two", "OPEN", "10:30:00", rollup=[{"status": "IN_PROGRESS"}])]})
        self.start(gh=gh, gh_every=0.05)
        self.assertEqual(self.wait("pr", pr=12)[0]["ts"], at("10:30:00"))
        self.assertEqual(self.wait("ci", pr=12)[0]["state"], "running")
        self.assertEqual(self.wait("ci", pr=12)[0]["ts"], at("11:00:00"))  # stamped now
        gh.prs["/r/alpha"] = [pr(12, "worktree-two", "MERGED", "10:30:00", merged="10:40:00")]
        self.wait("merge", pr=12)
        time.sleep(0.2)  # later polls say nothing new
        self.assertEqual((len(self.seen("pr")), len(self.seen("merge")), len(self.seen("ci"))), (1, 1, 1))

    def test_old_open_pull_request_shows_in_follow_only(self):
        self.start(gh=FakeGh())
        self.wait("pr", pr=7)

    def test_ci_green_after_running(self):
        gh = FakeGh({"/r/alpha": [pr(12, "worktree-two", "OPEN", "10:30:00", rollup=[{"status": "IN_PROGRESS"}])]})
        self.start(gh=gh, gh_every=0.05)
        self.wait("ci", state="running")
        gh.prs["/r/alpha"] = [pr(12, "worktree-two", "OPEN", "10:30:00", rollup=[{"status": "COMPLETED", "conclusion": "SUCCESS"}])]
        self.wait("ci", state="green")


class TestCommand(Env):
    def run_cli(self, *args, stdout=subprocess.PIPE):
        p = subprocess.Popen([sys.executable, "-m", "isshoni", "events", "--projects-dir", self.projects,
                              "--factory-dir", self.factory, "--no-gh", "--since", "2026-11-14T10:00:00", *args],
                             cwd=ROOT, stdout=stdout, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.reap, p)
        return p

    @staticmethod
    def reap(p):
        p.kill()  # a no-op once it has ended
        p.wait()
        for f in (p.stdout, p.stderr):
            if f:
                f.close()

    def test_history_prints_json_lines_and_exits(self):
        p = self.run_cli()
        out, err = p.communicate(timeout=30)
        rows = [json.loads(s) for s in out.splitlines()]
        self.assertEqual((p.returncode, err), (0, ""))
        self.assertEqual(len(rows), 38)
        self.assertEqual([r["ts"] for r in rows], sorted(r["ts"] for r in rows))

    def test_follow_flushes_each_line_and_ends_cleanly_on_sigterm(self):
        p = self.run_cli("--follow")
        watchdog = threading.Timer(30, p.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        rows = []
        while not rows or rows[-1]["ts"] < "2026-11-14T10:35:00":
            rows.append(json.loads(p.stdout.readline()))
        self.append(MAIN, '{"type":"assistant","timestamp":"2026-11-14T10:40:00.000Z","message":{"id":"msg_f","model":"m","content":[{"type":"tool_use","id":"tu_f","name":"Write","input":{}}]}}\n')
        while rows[-1]["kind"] != "tool" or rows[-1]["ts"] != "2026-11-14T10:40:00.000Z":
            rows.append(json.loads(p.stdout.readline()))
        p.terminate()
        _, err = p.communicate(timeout=10)
        self.assertEqual((p.returncode, err), (0, ""))
        self.assertEqual(rows[-1]["tool"], "write")

    def append(self, rel, text):
        with open(os.path.join(self.root, rel), "a") as f:
            f.write(text)

    def test_closed_stdout_is_not_an_error(self):
        r, w = os.pipe()
        os.close(r)
        p = self.run_cli(stdout=w)
        os.close(w)
        _, err = p.communicate(timeout=30)
        self.assertEqual((p.returncode, err), (0, ""))

class FollowParentTests(unittest.TestCase):
    CHILD = """
import threading
from isshoni import events
class Feed:
    def now(self): return "2026-11-14T10:00:00Z"
    def history(self): return []
    def poll(self, discover): return []
    def poll_gh(self): return []
events.follow(Feed(), lambda batch: print('started', flush=True), threading.Event(), tick=0.1)
"""
    # A parent that starts the follow and prints its pid, then waits to be killed.
    PARENT = "import subprocess, sys, time\n" \
             "p = subprocess.Popen([sys.executable, '-c', sys.argv[1]])\n" \
             "print(p.pid, flush=True)\n" \
             "time.sleep(60)\n"

    @staticmethod
    def kill_group(parent):
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.wait()
        parent.stdout.close()

    def test_follow_ends_within_three_ticks_of_its_parent_being_killed(self):
        parent = subprocess.Popen([sys.executable, "-c", self.PARENT, self.CHILD], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                                  start_new_session=True)
        self.addCleanup(self.kill_group, parent)
        lines = {parent.stdout.readline().strip(), parent.stdout.readline().strip()}  # the pid, and "started": the parent is recorded
        child = int(next(l for l in lines if l.isdigit()))
        self.assertIn("started", lines)
        os.kill(child, 0)
        parent.kill()
        parent.wait()
        end = time.monotonic() + 3 * 0.1 + 2  # three ticks, plus the scheduler's slack
        while time.monotonic() < end:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail("the follow process %d outlived its parent" % child)


if __name__ == "__main__":
    unittest.main()
