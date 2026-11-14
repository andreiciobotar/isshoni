import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import ROOT

sys.path.insert(0, os.path.join(ROOT, "tools"))
import record  # noqa: E402

MAIN = "/Users/someone/code/proj"
ENC = "-Users-someone-code-proj"


class ScopeTests(unittest.TestCase):
    def test_encode(self):
        self.assertEqual(record.encode("/Users/a.b/code/x"), "-Users-a-b-code-x")

    def test_selection(self):
        names = [ENC, ENC + "--claude-worktrees-task", ENC + "-other", ENC + "x--claude-worktrees-t", "-Users-someone-code-pro"]
        self.assertEqual(record.scoped_dirs(names, MAIN), [ENC, ENC + "--claude-worktrees-task"])


class StoreTests(unittest.TestCase):
    def refusal(self, *records):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in records) + "\n")
            return record.store_refusal(d, MAIN)

    def test_only_this_repository(self):
        self.assertIsNone(self.refusal({"repo": MAIN}, {"x": 1}, {"repo": None}, "not json"))

    def test_other_repository_refused_without_naming_it(self):
        msg = self.refusal({"repo": MAIN}, {"repo": "/Users/someone/code/secret"})
        self.assertIn("other repositories", msg)
        self.assertNotIn("secret", msg)


class FrameTests(unittest.TestCase):
    def check(self, text, words=("beta", "fix-it"), allow=()):
        return record.check_frame(text, set(words), "/Users/someone", allow)

    def test_clean(self):
        self.assertEqual(self.check("\x1b[31mall fine\x1b[0m here ~/.claude/factory"), [])

    def test_substring(self):
        self.assertEqual(self.check("cwd /Users/someone/x"), ["/Users/someone", "/Users/"])
        self.assertEqual(self.check("at /home/ci"), ["/home/"])

    def test_encoded_home(self):
        self.assertEqual(self.check("dir -Users-someone-code-x"), ["-Users-someone"])

    def test_whole_word(self):
        self.assertEqual(self.check("repo \x1b[1mbeta\x1b[0m: 3"), ["beta"])
        self.assertEqual(self.check("(fix-it)"), ["fix-it"])
        self.assertEqual(self.check("\u2502beta\u2502"), ["beta"])

    def test_word_cut_short(self):
        self.assertEqual(self.check("task fix-\u2026"), ["fix-it"])
        self.assertEqual(self.check("task fix\u2026"), ["fix-it"])
        self.assertEqual(self.check("x be\u2026"), [])  # under 3 characters
        self.assertEqual(self.check("x abeta\u2026 xfix\u2026"), [])  # not at a word start

    def test_short_word_is_not_matched_by_a_bare_ellipsis(self):
        self.assertEqual(record.check_frame("cap  \u2026  per x \u2502\u2026\u2502", {"ab", "a"}, ""), [])
        self.assertEqual(record.check_frame("see ab here", {"ab"}, ""), ["ab"])

    def test_word_inside_longer_word_passes(self):
        self.assertEqual(self.check("betas beta-2 _beta alphabeta fix-it-now"), [])

    def test_allow(self):
        self.assertEqual(self.check("a beta b", allow=["beta"]), [])
        self.assertEqual(self.check("a bet\u2026", allow=["beta"]), [])

    def test_all_hits(self):
        self.assertEqual(self.check("beta fix-it"), ["beta", "fix-it"])

    def test_deny_words_exclude_own_names(self):
        sessions = [
            {"project_dir": "own", "repo": "mine", "task": "t1"},
            {"project_dir": "o1", "repo": "mine", "task": "t1"},
            {"project_dir": "o1", "repo": "beta", "task": None},
            {"project_dir": "o2", "repo": "beta", "task": "t2"},
        ]
        self.assertEqual(record.deny_words(sessions, {"own"}), {"beta", "t2"})


class ChildTests(unittest.TestCase):
    def test_a_child_that_hangs_ends_at_the_timeout_and_the_failure_names_the_command(self):
        with self.assertRaises(SystemExit) as cm:
            record.strip.run(["sleep", "30"], 1)
        self.assertRegex(str(cm.exception.code), r"no answer in 1 s: sleep 30$")

    def test_a_child_that_fails_is_named_with_its_exit_code(self):
        with self.assertRaises(SystemExit) as cm:
            record.strip.run(["sh", "-c", "exit 3"])
        self.assertRegex(str(cm.exception.code), r"exit 3: sh -c exit 3$")

    def test_a_child_that_works_returns_its_output(self):
        self.assertEqual(record.strip.run(["echo", "hi"], capture_output=True, text=True).stdout, "hi\n")


class HanamiAtTests(unittest.TestCase):
    def test_no_merge(self):
        self.assertIsNone(record.hanami_at([{"ts": "2026-01-01T00:00:00+00:00", "kind": "tool"}]))

    def test_merge_after_a_gap_is_skipped_to(self):
        evs = [{"ts": "2026-01-01T00:00:00+00:00", "kind": "tool"}, {"ts": "2026-01-01T06:00:00+00:00", "kind": "merge"}]
        # frame 1 applies the first event, frame 2 jumps to 3 replay s before the merge, 60 more frames reach it
        self.assertAlmostEqual(record.hanami_at(evs), 62 * 0.05, delta=0.001)


class WebpTests(unittest.TestCase):
    def test_every_frame_keeps_the_scene_duration(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is missing")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.webp")
            record.save_webp([Image.new("RGB", (8, 8), "black"), Image.new("RGB", (8, 8), "white")], path, 900)
            with open(path, "rb") as f:
                data = f.read()
        # an ANMF chunk holds x, y, width-1, height-1 (3 bytes each) and then the duration (3 bytes)
        durations, pos = [], 12
        while pos < len(data):
            size = int.from_bytes(data[pos + 4:pos + 8], "little")
            if data[pos:pos + 4] == b"ANMF":
                durations.append(int.from_bytes(data[pos + 8 + 12:pos + 8 + 15], "little"))
            pos += 8 + size + size % 2
        self.assertEqual(durations, [900, 900])


class MainTests(unittest.TestCase):
    def test_a_hit_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            sessions = json.dumps({"sessions": [{"project_dir": "o", "repo": "beta", "task": None}]})
            with mock.patch.object(sys, "argv", ["record.py", "--out", out, "--scenes", "factory"]), \
                    mock.patch.object(record, "main_root", return_value=MAIN), \
                    mock.patch.object(record, "store_refusal", return_value=None), \
                    mock.patch.object(record, "snapshot", return_value="a beta frame\n"), \
                    mock.patch.object(record.os, "listdir", return_value=[ENC]), \
                    mock.patch.object(record.os, "symlink"), \
                    mock.patch.object(record.strip.subprocess, "run", return_value=mock.Mock(stdout=sessions)), \
                    mock.patch.object(record.strip, "render") as render, \
                    mock.patch.object(sys, "stderr", io.StringIO()) as err:
                with self.assertRaises(SystemExit) as cm:
                    record.main()
            self.assertIn("nothing was written", str(cm.exception.code))
            self.assertEqual(err.getvalue().count("scene factory frame"), 4)  # every hit is listed
            render.assert_not_called()
            self.assertFalse(os.path.exists(out))
