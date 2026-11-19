import contextlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from test_factory import factory, real_short_wake

REASON = "the machine is in a dark wake (asleep, awake for a moment)"
FULL = "Current System Capabilities are: CPU Graphics Audio Network\n"


class ShortWake(unittest.TestCase):
    def probe(self, platform="darwin", answer=(0, FULL, "")):
        with mock.patch.object(factory.sys, "platform", platform), mock.patch.object(factory, "run", return_value=answer) as run:
            return real_short_wake(), run

    def test_not_darwin_runs_nothing(self):
        got, run = self.probe("linux")
        self.assertIsNone(got)
        run.assert_not_called()

    def test_a_full_wake_is_none(self):
        self.assertIsNone(self.probe()[0])

    def test_no_graphics_is_a_dark_wake(self):
        got, run = self.probe(answer=(0, "System State:\nCurrent System Capabilities are: CPU Network\n", ""))
        self.assertEqual(got, REASON)
        self.assertEqual(run.call_args.args[0], ["pmset", "-g", "systemstate"])

    def test_an_unreadable_answer_is_none(self):
        for answer in ((1, "Current System Capabilities are: CPU\n", ""), (0, "", ""), (124, "", "timed out"), (127, "", "no pmset")):
            with self.subTest(answer=answer):
                self.assertIsNone(self.probe(answer=answer)[0])


class Tick(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self, wake):
        steps = {n: mock.Mock() for n in ("ops_tick", "review_tick", "watch_tick", "drain")}
        with mock.patch.object(factory, "Repo", return_value=SimpleNamespace(root="/repo")), \
                mock.patch.object(factory, "short_wake", return_value=wake), \
                mock.patch.multiple(factory, **steps), \
                mock.patch.object(factory, "repo_lock", side_effect=lambda *a: contextlib.nullcontext(True)), \
                mock.patch.object(factory, "read_word", return_value={"all": False, "prs": False}), \
                mock.patch.object(factory, "handed_over", return_value=False), mock.patch.object(factory, "paused_rows", return_value={}), \
                redirect_stdout(io.StringIO()) as out:
            code = factory.cmd_watch(SimpleNamespace(dispatcher="d"))
        return code, out.getvalue(), steps

    def test_a_dark_wake_runs_nothing_and_records_why(self):
        code, out, steps = self.tick(REASON)
        self.assertEqual((code, out), (0, f"watch: tick not run: {REASON}\n"))
        for step in steps.values():
            step.assert_not_called()
        [r] = factory.read_log("ticks")
        self.assertEqual((r["repo"], r["outcome"], r["reason"]), ("/repo", "not run", REASON))

    def test_a_full_wake_runs_the_tick(self):
        code, _, steps = self.tick(None)
        self.assertEqual(code, 0)
        for name in ("ops_tick", "review_tick", "watch_tick"):
            steps[name].assert_called_once()
        self.assertEqual(list(factory.read_log("ticks")), [])
