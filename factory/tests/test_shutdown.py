"""What ends the script's children, one tick at a time, and the leftovers the script reports. Every process these tests
start is in a process group of its own, and the cleanup kills that group."""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import BIN, ROOT, WT, FakeRun, factory, invoke, porcelain, repo_answers
from test_factory import DISPATCHER
from test_health import HOUR, NOW, SID, Harness, check, choice, decided, fired, iso, lead, pic, register, reply

FAKE_CLAUDE = """#!/bin/sh
case "$1" in
  agents) echo "${AGENTS:-[]}"; exit 0 ;;
esac
sleep 60 &
echo $! > "$GRANDCHILD"
wait
"""
SLEEPING_GH = """#!/bin/sh
echo $$ > "$GHPID"
exec sleep 60
"""
DETACHING_CLAUDE = """#!/bin/sh
case "$1" in
  agents) echo '[]'; exit 0 ;;
esac
sleep 60 >/dev/null 2>&1 &
echo $! > "$GRANDCHILD"
"""
FAKE_GH = """#!/bin/sh
echo '[{"number": 1, "url": "u", "isDraft": false, "headRefName": "worktree-t", "headRefOid": "abc", "baseRefName": "main", "author": {"login": "a"}, "isCrossRepository": true, "latestReviews": [], "comments": []}]'
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone(pid, seconds=5.0):
    """Whether `pid` ends within `seconds` (it is reaped by init, so there is no event to wait on)."""
    end = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < end:
        time.sleep(0.02)
    return not alive(pid)


def start_in_group(testcase, argv, **kw):
    """A child of the test in its own process group, killed and reaped when the test ends."""
    p = subprocess.Popen(argv, start_new_session=True, **kw)

    def cleanup():
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        for pipe in (p.stdin, p.stdout, p.stderr):
            if pipe:
                pipe.close()
    testcase.addCleanup(cleanup)
    return p


class Fakes(unittest.TestCase):
    """A directory with a fake `claude` that starts a grandchild (`sleep 60`, its pid in `grandchild`) and waits."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in (("claude", FAKE_CLAUDE), ("gh", FAKE_GH)):
            tool = self.bin / name
            tool.write_text(text)
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.grandchild = self.tmp / "grandchild"
        self.addCleanup(self.kill_grandchild)

    def kill_grandchild(self):
        try:
            os.kill(int(self.grandchild.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def started(self):
        """The pid of the grandchild, once the fake `claude` has written it."""
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if self.grandchild.exists() and self.grandchild.read_text().strip():
                return int(self.grandchild.read_text())
            time.sleep(0.02)
        self.fail("the fake claude did not start")


class RunLifetimeTests(Fakes):
    def test_a_group_run_that_times_out_leaves_no_grandchild(self):
        env = {**os.environ, "GRANDCHILD": str(self.grandchild)}
        self.assertEqual(factory.run([str(self.bin / "claude"), "-p"], env=env, timeout=1, group=True),
                         (124, "", "timed out"))
        self.assertTrue(gone(self.started()))
        self.assertEqual(factory.GROUPS, set())

    def test_kill_group_never_signals_the_own_group_or_a_system_group(self):
        with mock.patch.object(factory.os, "killpg") as killpg:
            for pgid in (0, 1, -5, os.getpgrp()):
                factory.kill_group(pgid)
            killpg.assert_not_called()
            factory.kill_group(2**31 - 2)
            killpg.assert_called_once_with(2**31 - 2, signal.SIGKILL)

    def test_a_call_without_a_timeout_that_hangs_gets_the_default(self):
        with mock.patch.object(factory, "RUN_TIMEOUT", 0.3):
            self.assertEqual(factory.run(["sleep", "60"]), (124, "", "timed out"))

    def run_script(self, env):
        """The script `lander 1` in a throwaway repository, in a process group of its own: the pull request is a fork,
        so the loop stops on it and tells the dispatcher (the fake `claude` lists one) with a `claude -p` run."""
        repo, home = self.tmp / "repo", self.tmp / "home"
        repo.mkdir()
        home.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
        agents = json.dumps([{"sessionId": "sid-d", "name": "dispatcher-repo", "cwd": str(repo), "kind": "interactive"}])
        env = {**os.environ, "HOME": str(home), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
               "GRANDCHILD": str(self.grandchild), "CLAUDE_CODE_SESSION_ID": "sid-d", "AGENTS": agents, **env}
        env.pop("FACTORY_HOME", None)
        return start_in_group(self, [sys.executable, str(BIN), "lander", "1"], cwd=repo, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_a_group_run_that_ends_normally_leaves_no_grandchild(self):
        claude = self.bin / "claude"
        claude.write_text(DETACHING_CLAUDE)
        env = {**os.environ, "GRANDCHILD": str(self.grandchild)}
        self.assertEqual(factory.run([str(claude), "-p"], env=env, group=True), (0, "", ""))
        self.assertTrue(gone(self.started()))

    def test_the_script_on_sigterm_ends_an_ordinary_child(self):
        (self.bin / "gh").write_text(SLEEPING_GH)
        ghpid = self.tmp / "ghpid"
        p = self.run_script({"GHPID": str(ghpid)})
        end = time.monotonic() + 10
        while not (ghpid.exists() and ghpid.read_text().strip()) and time.monotonic() < end:
            time.sleep(0.02)
        pid = int(ghpid.read_text())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        os.kill(p.pid, signal.SIGTERM)
        self.assertEqual(p.wait(timeout=10), 143)
        self.assertTrue(gone(pid))

    def script_gets(self, signum):
        """The script `lander 1` with the fake `claude` and `gh`, signalled during the courier's claude run."""
        p = self.run_script({})
        pid = self.started()
        os.kill(p.pid, signum)
        self.assertEqual(p.wait(timeout=10), 128 + signum)
        self.assertTrue(gone(pid))

    def test_the_script_on_sigterm_ends_its_group_children(self):
        self.script_gets(signal.SIGTERM)

    def test_the_script_on_sighup_ends_its_group_children(self):
        self.script_gets(signal.SIGHUP)


class TickLockTests(Harness):
    def hold(self):
        """A process that holds the watch lock until it is killed."""
        path = factory.repo_state(ROOT, "watch", "lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        code = "import fcntl, sys; f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); sys.stdin.read()"
        p = start_in_group(self, [sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline(), "held\n")
        return p

    def test_a_tick_that_finds_the_lock_held_does_nothing_and_a_dead_holder_frees_it(self):
        holder = self.hold()
        code, out, _ = self.cli("watch")
        self.assertEqual((code, out), (0, "watch: a tick is already running\n"))
        self.assertEqual([c for c in self.fake.calls if c[0] != "git"], [])
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait()
        code, out, _ = self.cli("watch")
        self.assertEqual(code, 0)
        self.assertIn(" watch repo\n", out)


class BusyAfterHandoverTests(unittest.TestCase):
    PS = """  PID  PPID     ELAPSED COMMAND
    1     0 3-02:00:00 /sbin/launchd
  100     1       20:00 claude --bg
  101   100       19:00 zsh -c sleep 600
  102   101       18:30 sleep 600
  103   100       05:00 node server.js
  104   103       00:10 {long}
  105   100       00:05 sh
  106   100       00:01 ls
  200     1       30:00 unrelated child
"""

    def picture(self, minutes=7, sessions=None, state="handed-over", **kw):
        repo = SimpleNamespace(root=ROOT, default="main", tasks=[{"path": f"{WT}/x"}])
        return pic(repo=repo, declared={"x": {"state": state, "ts": iso(NOW - minutes * 60)}},
                   sessions=sessions or [lead("x", pid=100)], **kw)

    def ps(self):
        return FakeRun({("ps",): (0, self.PS.format(long="c" * 150), "")})

    def test_it_names_the_five_oldest_descendants_of_the_leads_pid_and_counts_the_rest(self):
        fake = self.ps()
        with mock.patch.object(factory, "run", fake):
            ((slug, subject, detail),) = check("busy-after-handover", self.picture(), 6)
        self.assertEqual((slug, subject), ("x", "x"))
        self.assertEqual(detail, "handed over 7m ago, lead session busy/working: "
                                 "101 up 19:00: zsh -c sleep 600; 102 up 18:30: sleep 600; 103 up 05:00: node server.js; "
                                 f"104 up 00:10: {'c' * 120}; 105 up 00:05: sh; +1 more")
        self.assertEqual(fake.calls, [["ps", "-axo", "pid=,ppid=,etime=,command="]])

    def test_no_child_is_said_with_what_cannot_be_seen(self):
        with mock.patch.object(factory, "run", FakeRun({("ps",): (0, "100 1 10:00 lead\n", "")})):
            ((_, _, detail),) = check("busy-after-handover", self.picture(), 6)
        self.assertTrue(detail.endswith("busy/working: no child process (a subagent waiting without a process "
                                        "cannot be seen from outside)"), detail)

    def test_it_waits_for_the_threshold_and_for_a_busy_session(self):
        done = lead("x", pid=100, status="idle")
        for label, p in (("too early", self.picture(minutes=5)), ("finished", self.picture(sessions=[done])),
                         ("working", self.picture(state="working"))):
            with self.subTest(label):
                fake = self.ps()
                with mock.patch.object(factory, "run", fake):
                    self.assertEqual(check("busy-after-handover", p, 6), [])
                self.assertEqual(fake.calls, [])  # no ps call when no task qualifies

    def test_two_tasks_cost_one_ps_call(self):
        repo = SimpleNamespace(root=ROOT, default="main", tasks=[{"path": f"{WT}/{s}"} for s in "xy"])
        p = pic(repo=repo, declared={s: {"state": "handed-over", "ts": iso(NOW - HOUR)} for s in "xy"},
                sessions=[lead("x", pid=100), lead("y", sid="22222222-bbbb", pid=105)])
        fake = self.ps()
        with mock.patch.object(factory, "run", fake):
            found = check("busy-after-handover", p, 6)
        self.assertEqual([f[0] for f in found], ["x", "y"])
        self.assertEqual(len(fake.calls), 1)

    def test_sessions_that_cannot_be_listed_are_not_evaluated_only_when_a_task_qualifies(self):
        self.assertEqual(check("busy-after-handover", self.picture(minutes=5, sessions=[lead("x")]), 6), [])
        with self.assertRaises(factory.NotEvaluated):
            check("busy-after-handover", {**self.picture(), "sessions": None}, 6)

    def test_it_is_shipped_in_the_criteria_file(self):
        (c,) = [c for c in factory.load_criteria() if c["name"] == "busy-after-handover"]
        self.assertEqual((c["escalate"], c["threshold"], c.get("job", "watch")), ("judgment", 6, "watch"))
        self.assertEqual(factory.EVERY_TICK, {"busy-after-handover", "churning"})


class BusyEveryTickTests(Harness):
    """The criterion on the ticks between two health passes, and what it costs there."""

    CRITERIA = ["busy-after-handover", "past-limit"]
    PS = "  100     1       20:00 claude --bg\n  101   100       19:00 sleep 600\n"

    def setUp(self):
        super().setUp()
        factory.write_lander(SimpleNamespace(root=ROOT), {"stopped": {}, "waiting": {}, "health": NOW - 10})  # no health pass
        self.busy = [DISPATCHER | {"name": "dispatcher-repo"}, lead("x", pid=100)]

    def hand_over(self, minutes):
        self.seed("tasks", [{"repo": ROOT, "slug": "x", "state": "handed-over", "ts": iso(NOW - minutes * 60)}])

    def tick(self, at=NOW, sessions=None, answers=None):
        out = self.cli("watch", entries=["x"], sessions=sessions or self.busy, at=at,
                       answers={("ps",): (0, self.PS, ""), **(answers or {})})
        return out[1]

    def count(self, *prefix):
        return sum(c[:len(prefix)] == list(prefix) for c in self.fake.calls)

    def test_a_task_not_yet_past_the_threshold_costs_nothing_extra(self):
        self.hand_over(5)
        self.assertIn("nothing new", self.tick())
        # the listings are the draft sync's and the lander loop's, which run with any handed-over task
        self.assertEqual((self.count("claude", "agents"), self.count("ps"), self.count("claude", "-p")), (2, 0, 0))

    def test_no_handed_over_task_runs_no_session_listing_and_no_ps(self):
        self.assertIn("nothing new", self.tick())
        self.assertEqual([c for c in self.fake.calls if c[0] in ("claude", "ps", "gh")], [])

    def test_a_new_finding_reaches_the_site_manager_within_a_tick_and_costs_one_listing_and_one_ps(self):
        self.hand_over(7)
        self.tick()
        [record] = self.records()
        self.assertEqual((record["criterion"], record["subject"]), ("busy-after-handover", "x"))
        self.assertIn("101 up 19:00: sleep 600", record["detail"])
        # three listings: the draft sync's, the criterion's and the lander loop's
        self.assertEqual((self.count("claude", "agents"), self.count("ps"), len(self.claude_runs())), (3, 1, 1))

    def test_an_undecided_finding_is_sent_again_on_the_health_pass_only_and_a_decided_one_is_not(self):
        self.hand_over(7)
        self.tick()
        fid = self.records()[0]["id"]
        self.tick(at=NOW + 120)
        self.assertEqual(self.claude_runs(), [])  # between health passes only a new finding is sent
        self.tick(at=NOW + 1800, answers={("claude", "-p"): reply([choice(fid)])})  # the health pass
        self.assertEqual(len(self.claude_runs()), 1)  # undecided: sent again
        self.tick(at=NOW + 1920)
        self.assertEqual(self.claude_runs(), [])
        self.assertEqual(len([r for r in self.records() if r.get("criterion") == "busy-after-handover"]), 1)

    def test_the_site_manager_is_given_the_name_the_dispatcher_has_now_between_health_passes(self):
        register("sid-d")
        self.hand_over(7)
        self.tick(sessions=[DISPATCHER | {"name": "renamed", "pid": 7}, lead("x", pid=100)])
        [argv] = self.claude_runs()
        self.assertIn("The dispatcher's session name is renamed.", argv[-1])

    def test_a_failing_site_manager_run_is_not_repeated_on_every_tick(self):
        self.hand_over(7)
        runs = 0
        for later in (0, 120, 240, 360):
            self.tick(at=NOW + later, answers={("claude", "-p"): (1, "", "boom")})
            runs += len(self.claude_runs())
        self.assertEqual(runs, 1)

    def test_a_finding_whose_session_finished_is_cleared_on_the_next_tick(self):
        self.hand_over(7)
        self.tick()
        fid = self.records()[0]["id"]
        self.tick(at=NOW + 120, sessions=[self.busy[0], lead("x", pid=100, status="idle")])
        self.assertTrue(any(r.get("id") == fid and r.get("cleared") for r in self.records()))

    def test_no_dispatcher_session_means_no_site_manager_run_on_any_tick(self):
        self.hand_over(7)
        for later in (0, 120):
            self.tick(at=NOW + later, sessions=[self.busy[1]])
            self.assertEqual(self.claude_runs(), [])
        self.assertEqual(len([r for r in self.records() if r.get("criterion")]), 1)  # logged once, never sent

    def test_a_failed_session_listing_neither_clears_nor_fires(self):
        failing = {("claude", "agents"): (1, "", "boom")}
        self.hand_over(7)
        self.tick(answers=failing)
        self.assertEqual((self.records(), self.claude_runs()), ([], []))
        self.tick(at=NOW + 120)
        fid = self.records()[0]["id"]
        self.tick(at=NOW + 240, answers=failing)
        self.assertEqual([r for r in self.records() if r.get("id") == fid and r.get("cleared")], [])
        self.assertEqual(len([r for r in self.records() if r.get("criterion")]), 1)

    def test_a_finding_without_a_row_still_carries_the_transcript_tail(self):
        entries = [{"timestamp": "T0", "message": {"role": "user", "content": "start"}}]
        self.transcript("{sid}.jsonl", NOW - 60).write_text("".join(json.dumps(e) + "\n" for e in entries))
        self.hand_over(7)
        self.tick()
        [sent] = json.loads(self.claude_runs()[0][-1].partition("\n\n")[2])
        self.assertEqual(sent["task"], {"worktree": f"{WT}/x", "lead": "x", "pid": 100})
        self.assertEqual(sent["transcript_tail"], ["T0 user: start"])

    def test_a_lead_told_longer_than_the_grace_ago_is_fired_again_on_a_tick_between_health_passes(self):
        self.hand_over(7)
        told = NOW + 120 - (factory.LEAD_GRACE_MINUTES + 1) * 60
        self.seed("firings", [fired("aaa", "busy-after-handover", "judgment"),
                              decided("aaa", "escalated", ts=told, reason="stop sleep") | {"to": "lead"}])
        self.tick(at=NOW + 120)
        [argv] = self.claude_runs()
        [sent] = json.loads(argv[-1].partition("\n\n")[2])
        self.assertNotEqual(sent["id"], "aaa")
        self.assertTrue(sent["lead_told"])

    def test_the_other_criteria_are_left_alone_between_health_passes(self):
        self.seed("firings", [fired("a", criterion="past-limit")])  # open, and not evaluated on this tick
        self.hand_over(5)
        self.tick()
        self.assertFalse(any(r.get("cleared") for r in self.records()))


class StaleLinesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for name, path in (("JOBS_DIR", self.tmp / "jobs"), ("STORE", self.tmp / "store")):
            patch = mock.patch.object(factory, name, path)
            patch.start()
            self.addCleanup(patch.stop)
            path.mkdir()

    def old(self, path, hours=25):
        path.mkdir(exist_ok=True)
        t = time.time() - hours * 3600
        os.utime(path, (t, t))
        return path

    def job(self, name, state, hours=25, size=0):
        d = self.tmp / "jobs" / name
        d.mkdir()
        (d / "state.json").write_text(json.dumps({"state": state}))
        (d / "tmp").mkdir()
        (d / "tmp" / "blob").write_bytes(b"x" * size)
        return self.old(d, hours)

    def test_nothing_is_nothing(self):
        self.assertEqual(factory.stale_lines(SimpleNamespace(agents=[])), [])

    def test_old_subagent_worktrees_are_counted_and_named_at_most_five(self):
        paths = [{"path": str(self.old(self.tmp / f"agent-{i}"))} for i in range(7)]
        young = {"path": str(self.old(self.tmp / "agent-new", hours=1))}
        (line,) = factory.stale_lines(SimpleNamespace(agents=[*paths, young]))
        self.assertEqual(line, "7 subagent worktrees (agent-*) older than 24 h: agent-0, agent-1, agent-2, agent-3, agent-4")

    def test_finished_job_directories_older_than_a_day_are_counted_with_their_size(self):
        self.job("a", "done", size=1_500_000)
        self.job("b", "stopped", size=500_000)
        self.job("c", "working")
        self.job("d", "done", hours=1)
        (self.tmp / "jobs" / "e").mkdir()
        (line,) = factory.stale_lines(SimpleNamespace(agents=[]))
        self.assertTrue(line.startswith("2 finished job directories older than 24 h under ~/.claude/jobs (2.0 MB)"), line)

    def clean(self, lsof):
        code, out, err, fake = invoke(["clean"], {**repo_answers([]), ("lsof",): lsof})
        self.assertEqual((code, err), (0, ""))
        return out.splitlines(), fake

    def test_clean_reports_finished_jobs_and_never_removes_them(self):
        a, b, c = (self.job(n, "done") for n in "abc")
        self.job("d", "done", hours=1)
        self.job("e", "working")
        held = os.path.realpath(a / "tmp" / "blob")
        lsof = f"p4242\ncvim\nfcwd\nn/elsewhere\nftxt\nn{held}\np7\ncother\nn/tmp\n"
        lines, fake = self.clean((0, lsof, ""))
        self.assertEqual(lines, ["kept job a: belongs to Claude Code; pid 4242 (vim) has files open in it",
                                 "kept job b: belongs to Claude Code; no process has files open in it, try: claude rm b",
                                 "kept job c: belongs to Claude Code; no process has files open in it, try: claude rm c"])
        self.assertEqual([c for c in fake.calls if c[0] == "lsof"], [["lsof", "-w", "-F", "pcn"]])
        self.assertFalse(fake.ran("claude", "rm"))
        self.assertTrue(all(d.is_dir() for d in (a, b, c)))

    def test_a_directory_whose_name_only_starts_like_the_job_is_not_inside_it(self):
        a = self.job("a", "done")
        lines, _ = self.clean((0, f"p7\ncother\nn{os.path.realpath(a)}-other/x\n", ""))
        self.assertEqual(lines, ["kept job a: belongs to Claude Code; no process has files open in it, try: claude rm a"])

    def test_clean_says_when_it_cannot_list_open_files(self):
        self.job("a", "done")
        for lsof in ((1, "", "boom"), (127, "", "no lsof"), (124, "", "timed out"), (0, "", "")):
            with self.subTest(lsof=lsof):
                (line,), _ = self.clean(lsof)
                self.assertRegex(line, "^kept job a: belongs to Claude Code; cannot list open files: lsof printed nothing")

    def test_clean_asks_lsof_only_when_something_needs_it(self):
        self.job("d", "done", hours=1)
        lines, fake = self.clean((1, "", "boom"))
        self.assertEqual((lines, fake.ran("lsof")), ([], False))

    def test_the_repo_lists_its_agent_worktrees(self):
        listed = porcelain((ROOT, "main", False), (f"{WT}/agent-ab", "worktree-agent-ab", False),
                           (f"{WT}/task", "worktree-task", False))
        with mock.patch.object(factory, "run", FakeRun({("git", "worktree"): (0, listed, "")})):
            repo = factory.Repo()
        self.assertEqual(([(a["path"], a["branch"], a["locked"]) for a in repo.agents], repo.leftover),
                         ([(f"{WT}/agent-ab", "worktree-agent-ab", None)], 1))

    def test_the_repo_keeps_the_branch_and_the_lock_of_an_agent_worktree(self):
        listed = porcelain((ROOT, "main", False), (f"{WT}/agent-ab", "worktree-agent-ab", False, "claude agent (pid 7)"),
                           (f"{WT}/agent-cd", None, False))
        with mock.patch.object(factory, "run", FakeRun({("git", "worktree"): (0, listed, "")})):
            repo = factory.Repo()
        self.assertEqual([(a["branch"], a["locked"]) for a in repo.agents],
                         [("worktree-agent-ab", "claude agent (pid 7)"), (None, None)])

    def test_strangers_in_the_store_are_counted_and_named_at_most_five(self):
        store = self.tmp / "store"
        for name in ("tmp", *(f"tmp-fix{i}.py" for i in range(1, 7)), "tasks.jsonl", "cache"):
            (store / name).mkdir() if name in ("tmp", "cache") else (store / name).write_text("x")
        (line,) = factory.stale_lines(SimpleNamespace(agents=[]))
        self.assertEqual(line, f"7 entries in {store} the factory does not write: "
                               "tmp, tmp-fix1.py, tmp-fix2.py, tmp-fix3.py, tmp-fix4.py")

    def test_every_name_the_factory_writes_in_the_store_is_not_a_stranger(self):
        store = self.tmp / "store"
        for name in ("events", "tasks", "feedback", "firings", "retros", "comments", "gh", "techlead"):
            factory.log(name, {"ts": factory.now()})
        root = "/some/repo"
        for kind, ext in (("dispatcher", "json"), ("comments", "json"), ("lander", "json"), ("watch", "lock"), ("tech-lead-a-task", "lock"),
                          ("lander", "lock"), ("word", "json"), ("word", "json.tmp"), ("dispatcher", "json.tmp"), ("comments", "json.tmp"), ("lander", "json.tmp")):
            factory.repo_state(root, kind, ext).write_text("x")
        for name in ("tasks.jsonl", "events.jsonl", "settings.json", "watch.log", "tech-lead.log", "hosts.json", "hosts.json.tmp",
                     ".settings-ab12.tmp"):
            (store / name).write_text("x")
        for name in ("cache", "comments", "identity", "specs", "deliverables", "dashboard"):
            (store / name).mkdir()
        self.assertEqual(factory.strangers(), [])
        (store / "tmp-fix1.py").write_text("x")
        (store / "gh.jsonl").write_text("x")
        self.assertEqual([p.name for p in factory.strangers()], ["gh.jsonl", "tmp-fix1.py"])


class HealthLinesTests(Harness):
    def test_health_prints_the_lines_only_when_there_is_something(self):
        self.assertEqual(self.cli("health")[1], "nothing fired\n")
        job = self.home / ".claude" / "jobs" / "a"
        job.mkdir(parents=True)
        (job / "state.json").write_text('{"state": "done"}')
        os.utime(job, (NOW - 3 * 86400,) * 2)
        self.assertEqual(self.cli("health")[1],
                         "nothing fired\n1 finished job directories older than 24 h under ~/.claude/jobs (0.0 MB)\n")


if __name__ == "__main__":
    unittest.main()
