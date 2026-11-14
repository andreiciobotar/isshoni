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

from test_factory import BIN, ROOT, WT, FakeRun, factory, porcelain
from test_health import HOUR, NOW, SID, Harness, check, iso, lead, pic, row

FAKE_CLAUDE = """#!/bin/sh
case "$1" in
  agents) echo '[]'; exit 0 ;;
esac
sleep 60 &
echo $! > "$GRANDCHILD"
wait
"""
FAKE_GH = """#!/bin/sh
echo '[{"number": 1, "url": "u", "isDraft": false, "headRefName": "worktree-t", "headRefOid": "abc", "baseRefName": "main", "author": {"login": "a"}, "isCrossRepository": false, "latestReviews": [], "comments": []}]'
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

    def test_a_call_without_a_timeout_that_hangs_gets_the_default(self):
        with mock.patch.object(factory, "RUN_TIMEOUT", 0.3):
            self.assertEqual(factory.run(["sleep", "60"]), (124, "", "timed out"))

    def script_gets(self, signum):
        """The script `lander 1` with the fake `claude` and `gh`, signalled during the claude run."""
        repo, home = self.tmp / "repo", self.tmp / "home"
        repo.mkdir()
        home.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
        env = {**os.environ, "HOME": str(home), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
               "GRANDCHILD": str(self.grandchild), "CLAUDE_CODE_SESSION_ID": "sid-d"}
        env.pop("FACTORY_HOME", None)
        p = start_in_group(self, [sys.executable, str(BIN), "lander", "1"], cwd=repo, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        path = factory.repo_state(SimpleNamespace(root=ROOT), "watch", "lock")
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
  200     1       30:00 unrelated child
"""

    def picture(self, minutes=11, sessions=None, **kw):
        r = row("x", phase="review", state="handed-over", deliverable="u")
        return pic(rows=[r], declared={"x": {"ts": iso(NOW - minutes * 60)}},
                   sessions=sessions or [lead("x", pid=100)], **kw)

    def ps(self):
        return FakeRun({("ps",): (0, self.PS.format(long="c" * 100), "")})

    def test_it_names_the_three_oldest_descendants_of_the_leads_pid(self):
        fake = self.ps()
        with mock.patch.object(factory, "run", fake):
            ((slug, subject, detail),) = check("busy-after-handover", self.picture(), 10)
        self.assertEqual((slug, subject), ("x", "x"))
        self.assertEqual(detail, "handed over 11m ago, lead session still busy: "
                                 "101 up 19:00: zsh -c sleep 600; 102 up 18:30: sleep 600; 103 up 05:00: node server.js")
        self.assertEqual(fake.calls, [["ps", "-axo", "pid=,ppid=,etime=,command="]])

    def test_a_long_command_is_cut_to_80_characters(self):
        fake = FakeRun({("ps",): (0, "100 1 10:00 lead\n104 100 00:10 " + "c" * 100 + "\n", "")})
        with mock.patch.object(factory, "run", fake):
            ((_, _, detail),) = check("busy-after-handover", self.picture(), 10)
        self.assertTrue(detail.endswith("104 up 00:10: " + "c" * 80), detail)

    def test_no_child_is_said(self):
        with mock.patch.object(factory, "run", FakeRun({("ps",): (0, "100 1 10:00 lead\n", "")})):
            ((_, _, detail),) = check("busy-after-handover", self.picture(), 10)
        self.assertTrue(detail.endswith("still busy: no child process"))

    def test_it_waits_for_the_threshold_and_for_a_busy_session(self):
        done = lead("x", pid=100, status="idle")
        for label, p in (("too early", self.picture(minutes=9)), ("finished", self.picture(sessions=[done])),
                         ("working", pic(rows=[row("x", phase="motion")], sessions=[lead("x", pid=100)]))):
            with self.subTest(label):
                fake = self.ps()
                with mock.patch.object(factory, "run", fake):
                    self.assertEqual(check("busy-after-handover", p, 10), [])
                self.assertEqual(fake.calls, [])  # no ps call when no task qualifies

    def test_two_tasks_cost_one_ps_call(self):
        a = row("x", phase="review", state="handed-over", deliverable="u")
        b = row("y", phase="review", state="handed-over", deliverable="u")
        p = pic(rows=[a, b], declared={s: {"ts": iso(NOW - HOUR)} for s in "xy"},
                sessions=[lead("x", pid=100), lead("y", sid="22222222-bbbb", pid=105)])
        fake = self.ps()
        with mock.patch.object(factory, "run", fake):
            found = check("busy-after-handover", p, 10)
        self.assertEqual([f[0] for f in found], ["x", "y"])
        self.assertEqual(len(fake.calls), 1)

    def test_it_is_shipped_in_the_criteria_file(self):
        (c,) = [c for c in factory.load_criteria() if c["name"] == "busy-after-handover"]
        self.assertEqual((c["escalate"], c["threshold"], c.get("job", "watch")), ("judgment", 10, "watch"))


class StaleLinesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patch = mock.patch.object(factory, "JOBS_DIR", self.tmp / "jobs")
        patch.start()
        self.addCleanup(patch.stop)
        (self.tmp / "jobs").mkdir()

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
        paths = [str(self.old(self.tmp / f"agent-{i}")) for i in range(7)]
        young = str(self.old(self.tmp / "agent-new", hours=1))
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

    def test_the_repo_lists_its_agent_worktrees(self):
        listed = porcelain((ROOT, "main", False), (f"{WT}/agent-ab", "worktree-agent-ab", False),
                           (f"{WT}/task", "worktree-task", False))
        with mock.patch.object(factory, "run", FakeRun({("git", "worktree"): (0, listed, "")})):
            repo = factory.Repo()
        self.assertEqual((repo.agents, repo.leftover), ([f"{WT}/agent-ab"], 1))


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
