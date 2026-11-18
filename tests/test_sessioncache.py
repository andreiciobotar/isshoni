import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from tests.support import AGENTS, PRICES, PRICES_PATH, PROJECTS, user_prices
from tests.test_cli import run
from isshoni import metrics, prices as price_file, sessioncache, transcript


def summary(s):
    metrics.analyze(s, AGENTS, 0.5)
    return json.dumps(metrics.session_summary(s, detail=True), sort_keys=True)


def write(path, text, mode="a"):
    with open(path, mode) as f:
        f.write(text)


def listing(root):
    return sorted((d, f) for d, _, fs in os.walk(root) for f in fs)


class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.projects = os.path.join(tmp, "projects")
        shutil.copytree(PROJECTS, self.projects)
        self.root = os.path.join(tmp, "cache")
        env = mock.patch.dict(os.environ, {"ISSHONI_CACHE_DIR": self.root})
        env.start()
        self.addCleanup(env.stop)
        self.path = os.path.join(self.projects, "-fixture-proj", "fixture-0001.jsonl")
        self.sub_dir = os.path.join(self.projects, "-fixture-proj", "fixture-0001", "subagents")
        self.cache = sessioncache.SessionCache(self.projects, PRICES)

    def entries(self):
        return [f for _, f in listing(self.root)]

    def test_miss_then_hit_equals_uncached(self):
        first = summary(self.cache.load(self.path))
        with mock.patch.object(transcript, "load_session", side_effect=AssertionError("parsed")):
            second = summary(self.cache.load(self.path))
        self.assertEqual(first, second)
        self.assertEqual(first, summary(transcript.load_session(self.path, PRICES)))
        self.assertEqual(len(self.entries()), 1)

    def test_entry_is_private(self):
        self.cache.load(self.path)
        (d, f), = listing(self.root)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_appended_main_transcript_is_parsed_again(self):
        before = self.cache.load(self.path).malformed
        with open(self.path, "a") as f:
            f.write("not json\n")
        self.assertEqual(self.cache.load(self.path).malformed, before + 1)

    def test_appended_and_new_subagent_files_are_parsed_again(self):
        sub = next(os.path.join(self.sub_dir, n) for n in sorted(os.listdir(self.sub_dir)) if n.endswith(".jsonl"))
        malformed = self.cache.load(self.path).malformed
        write(sub, "not json\n")
        self.assertEqual(self.cache.load(self.path).malformed, malformed + 1)
        with mock.patch.object(transcript, "load_session", wraps=transcript.load_session) as m:
            self.cache.load(self.path)
            self.assertEqual(m.call_count, 0)
            write(os.path.join(self.sub_dir, "agent-zzz.jsonl"), "not json\n", "w")
            self.assertEqual(self.cache.load(self.path).malformed, malformed + 2)
            self.assertEqual(m.call_count, 1)

    def test_dangling_symlink_in_subagents_does_not_cut_the_signature_short(self):
        os.symlink(os.path.join(self.sub_dir, "nowhere"), os.path.join(self.sub_dir, "agent-aaa.jsonl"))
        self.cache.load(self.path)
        malformed = self.cache.load(self.path).malformed
        write(os.path.join(self.sub_dir, "agent-zzz.jsonl"), "not json\n", "w")
        self.assertEqual(self.cache.load(self.path).malformed, malformed + 1)

    def test_signature_is_taken_before_the_parse(self):
        real = transcript.load_session

        def appending(path, prices):
            s = real(path, prices)
            write(path, "not json\n")
            return s

        with mock.patch.object(transcript, "load_session", appending):
            before = self.cache.load(self.path).malformed
        self.assertEqual(self.cache.load(self.path).malformed, before + 1)

    def test_relative_and_absolute_projects_dir_share_entries_and_print_their_own_path(self):
        base = ["--agents-dir", AGENTS, "--prices", PRICES_PATH]
        cwd = os.path.dirname(self.projects)
        for argv in (["sessions", "--json"], ["session", "fixture", "--json"]):
            outs = []
            for pd in ("projects", self.projects):
                full = argv + ["--projects-dir", pd] + base
                old = os.getcwd()
                os.chdir(cwd)
                try:
                    with mock.patch.dict(os.environ, {"ISSHONI_CACHE_DIR": ""}):
                        off = run(*full)
                    outs.append((off, run(*full)))
                finally:
                    os.chdir(old)
            for off, on in outs:
                self.assertEqual(off, on)
        self.assertEqual(len(self.entries()), 1)

    def test_changed_price_file_is_a_miss(self):
        self.cache.load(self.path)
        prices_copy = os.path.join(os.path.dirname(self.root), "prices.toml")
        shutil.copy(PRICES_PATH, prices_copy)
        cache = sessioncache.SessionCache(self.projects, price_file.load(prices_copy))
        cache.load(self.path)
        with open(prices_copy, "a") as f:
            f.write("\n# changed\n")
        cache = sessioncache.SessionCache(self.projects, price_file.load(prices_copy))
        with mock.patch.object(transcript, "load_session", wraps=transcript.load_session) as m:
            cache.load(self.path)
        self.assertEqual(m.call_count, 1)

    def test_changed_user_price_file_is_a_miss(self):
        with user_prices("[models.a]\nlike = 'claude-opus-5-5'\n") as path:
            sessioncache.SessionCache(self.projects, price_file.load()).load(self.path)
            with mock.patch.object(transcript, "load_session", wraps=transcript.load_session) as m:
                sessioncache.SessionCache(self.projects, price_file.load()).load(self.path)
                self.assertEqual(m.call_count, 0)  # unchanged: a hit
                write(path, "[models.b]\nlike = 'claude-opus-5-5'\n")
                sessioncache.SessionCache(self.projects, price_file.load()).load(self.path)
                self.assertEqual(m.call_count, 1)
                os.unlink(path)
                sessioncache.SessionCache(self.projects, price_file.load()).load(self.path)
                self.assertEqual(m.call_count, 2)  # no user file any more: another key

    def test_corrupt_entry_is_a_miss_then_repaired(self):
        self.cache.load(self.path)
        (d, f), = listing(self.root)
        with open(os.path.join(d, f), "wb") as fh:
            fh.write(b"garbage")
        expected = summary(transcript.load_session(self.path, PRICES))
        self.assertEqual(summary(self.cache.load(self.path)), expected)
        with mock.patch.object(transcript, "load_session", side_effect=AssertionError("parsed")):
            self.assertEqual(summary(self.cache.load(self.path)), expected)

    def test_unwritable_cache_root_still_loads(self):
        with open(self.root, "w") as f:  # a file where the directory should be
            f.write("x")
        s = self.cache.load(self.path)
        self.assertEqual(summary(s), summary(transcript.load_session(self.path, PRICES)))

    def test_prune_keeps_live_entries_only(self):
        other = os.path.join(self.projects, "-fixture-proj", "fixture-0002.jsonl")
        shutil.copy(self.path, other)
        self.cache.load(self.path)
        self.cache.load(other)
        with open(os.path.join(self.cache.dir, "stray.tmp"), "w") as f:
            f.write("x")
        os.unlink(other)
        self.cache.prune([self.path])
        self.assertEqual(self.entries(), [os.path.basename(self.cache._entry(self.path))])

    def test_empty_cache_dir_turns_the_cache_off(self):
        elsewhere = os.path.join(os.path.dirname(self.root), "home")
        with mock.patch.dict(os.environ, {"ISSHONI_CACHE_DIR": "", "XDG_CACHE_HOME": elsewhere, "HOME": elsewhere}):
            cache = sessioncache.SessionCache(self.projects, PRICES)
            cache.load(self.path)
            cache.prune([self.path])
        self.assertFalse(os.path.exists(elsewhere))
        self.assertFalse(os.path.exists(self.root))

    def test_cli_output_is_the_same_with_and_without_cache(self):
        argv = ["sessions", "--json", "--projects-dir", self.projects, "--agents-dir", AGENTS, "--prices", PRICES_PATH]
        with mock.patch.dict(os.environ, {"ISSHONI_CACHE_DIR": ""}):
            off = run(*argv)
        before = listing(self.projects)
        cold, warm = run(*argv), run(*argv)
        self.assertEqual(off[0], 0)
        self.assertEqual(off, cold)
        self.assertEqual(off, warm)
        self.assertTrue(self.entries())
        self.assertEqual(listing(self.projects), before)


if __name__ == "__main__":
    unittest.main()
