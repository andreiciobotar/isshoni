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


class WebpTests(unittest.TestCase):
    def test_every_frame_keeps_the_scene_duration(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is missing")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.webp")
            record.save_webp([Image.new("RGB", (8, 8), "black"), Image.new("RGB", (8, 8), "white")], path, 900)
            data = open(path, "rb").read()
        # an ANMF chunk holds x, y, width-1, height-1 (3 bytes each) and then the duration (3 bytes)
        durations, pos = [], 12
        while pos < len(data):
            size = int.from_bytes(data[pos + 4:pos + 8], "little")
            if data[pos:pos + 4] == b"ANMF":
                durations.append(int.from_bytes(data[pos + 8 + 12:pos + 8 + 15], "little"))
            pos += 8 + size + size % 2
        self.assertEqual(durations, [900, 900])
