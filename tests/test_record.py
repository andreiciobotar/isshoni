import json
import os
import sys
import tempfile
import unittest

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
        self.assertIsNone(self.check("\x1b[31mall fine\x1b[0m here"))

    def test_substring(self):
        self.assertEqual(self.check("cwd /Users/someone/x"), "/Users/someone")
        self.assertEqual(self.check("at /home/ci"), "/home/")

    def test_whole_word(self):
        self.assertEqual(self.check("repo \x1b[1mbeta\x1b[0m: 3"), "beta")
        self.assertEqual(self.check("(fix-it)"), "fix-it")

    def test_word_inside_longer_word_passes(self):
        self.assertIsNone(self.check("betas beta-2 _beta alphabeta fix-it-now"))

    def test_allow(self):
        self.assertIsNone(self.check("a beta b", allow=["beta"]))

    def test_deny_words_exclude_own_names(self):
        sessions = [
            {"project_dir": "own", "repo": "mine", "task": "t1"},
            {"project_dir": "o1", "repo": "mine", "task": "t1"},
            {"project_dir": "o1", "repo": "beta", "task": None},
            {"project_dir": "o2", "repo": "beta", "task": "t2"},
        ]
        self.assertEqual(record.deny_words(sessions, {"own"}), {"beta", "t2"})


class HanamiAtTests(unittest.TestCase):
    def test_no_merge(self):
        self.assertIsNone(record.hanami_at([{"ts": "2026-01-01T00:00:00+00:00", "kind": "tool"}]))

    def test_merge_after_a_gap_is_skipped_to(self):
        evs = [{"ts": "2026-01-01T00:00:00+00:00", "kind": "tool"}, {"ts": "2026-01-01T06:00:00+00:00", "kind": "merge"}]
        # frame 1 applies the first event, frame 2 jumps to 3 replay s before the merge, 60 more frames reach it
        self.assertAlmostEqual(record.hanami_at(evs), 62 * 0.05, delta=0.001)
