import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parent.parent / "bin" / "factory"
os.environ.pop("FACTORY_HOME", None)  # the tests make their own homes; an ambient one would move the script's store
_loader = importlib.machinery.SourceFileLoader("factory_bin", str(BIN))
factory = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_bin", _loader))
_loader.exec_module(factory)

# No test may write the real settings file: a write to the real ~/.claude/factory (taken before any test changes HOME) fails.
_REAL_FACTORY_HOME = Path("~/.claude/factory").expanduser().resolve()
_write_text = factory.write_text


def _guarded_write_text(path, content):
    if Path(os.path.realpath(path)).is_relative_to(_REAL_FACTORY_HOME):
        raise AssertionError(f"a test is about to write {path} in the real factory home")
    _write_text(path, content)


factory.write_text = _guarded_write_text

ROOT = "/repo"
WT = f"{ROOT}/.claude/worktrees"
DAY = 86400
BATCH = ("gh", "pr", "list", "--state", "all", "--limit", "100")  # the one call `status` makes per repository


def porcelain(*entries):
    """entries: (path, branch or None, prunable[, lock reason; "" is a lock without one])."""
    out = []
    for path, branch, prunable, *lock in entries:
        out += [f"worktree {path}", "HEAD abc", f"branch refs/heads/{branch}" if branch else "detached"]
        out += [f"locked {reason}".rstrip() for reason in lock]
        if prunable:
            out.append("prunable gitdir file points to non-existent location")
        out.append("")
    return "\n".join(out)


def task_entry(slug):
    return (f"{WT}/{slug}", f"worktree-{slug}", False)


class FakeRun:
    """Canned runner. `answers` maps an argv prefix (tuple) to (rc, out, err), a list of them or a function returning one;
    longest prefix wins."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.inputs = []

    def __call__(self, argv, cwd=None, input=None, timeout=None):
        self.calls.append(list(argv))
        self.inputs.append(input)
        unfiltered = argv[:3] == ["gh", "pr", "list"] and "--head" not in argv and factory.STATUS_PR_FIELDS in argv
        if unfiltered and BATCH not in self.answers:  # a stub of its own wins
            return self.listing()
        return self.resolve(argv)

    def resolve(self, argv):
        best = max((p for p in self.answers if tuple(argv[:len(p)]) == p), key=len, default=None)
        if best is None:
            return 1, "", "unexpected call"
        result = self.answers[best]
        if callable(result):  # computed at call time, to look at the world as the caller left it
            return result()
        if isinstance(result, list):  # successive answers; the last one repeats
            return result.pop(0) if len(result) > 1 else result[0]
        return result

    def listing(self):
        """What one unfiltered `gh pr list` says: the `--head` answers of the stubs, one after the other, each entry
        carrying its branch. The first failing answer is the whole answer."""
        porcelain_out = self.answers.get(("git", "worktree", "list"), (0, "", ""))[1]
        branches = re.findall(r"^branch refs/heads/(.+)$", porcelain_out, re.M)
        branches += [p[4] for p in self.answers if p[:4] == ("gh", "pr", "list", "--head") and p[4] not in branches]
        listed = []
        for branch in branches:
            rc, out, err = self.resolve(["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1",
                                         "--json", factory.STATUS_PR_FIELDS])
            if rc != 0:
                return rc, out, err
            listed += [dict(pr, headRefName=branch) if isinstance(pr, dict) else pr for pr in json.loads(out)]
        return 0, json.dumps(listed), ""

    def ran(self, *prefix):
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


DISPATCHER = {"sessionId": "sid-d", "name": "dispatcher-repo", "cwd": ROOT, "kind": "interactive"}
# The lead of task `x` after its process has exited, as `claude agents --json --all` lists it.
EXITED = {"id": "ex1", "sessionId": "sid-x", "name": "x", "kind": "background", "state": "done", "cwd": ROOT,
          "startedAt": 1}


def task_rec(slug, state, deliverable=None, repo=ROOT):
    return {"repo": repo, "slug": slug, "state": state, **({"deliverable": deliverable} if deliverable else {})}


def written():
    """The records of the tasks log, the unsuffixed file first, then the month files."""
    return [r for _, path in factory.log_files("tasks") for r in factory.read_file(path)]


def invoke(argv, answers, settings=None, env=None, tasks=(), cwd=None, events=None):
    """`tasks` seeds tasks.jsonl (records, or raw lines); the records written are returned as `fake.tasks`.
    `events` seeds the events log (records, each in the file of its ts's month); without it there is none."""
    fake = FakeRun(answers)
    out, err = io.StringIO(), io.StringIO()
    environ = {"HOME": "", "CLAUDE_CODE_SESSION_ID": "sid-d", **(env or {})}
    with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {**environ, "HOME": home}):
        store = Path(home, ".claude", "factory")
        store.mkdir(parents=True)
        if settings is not None:
            (store / "settings.toml").write_text(settings)
        (store / "tasks.jsonl").write_text("".join((t if isinstance(t, str) else json.dumps(t)) + "\n" for t in tasks))
        seeded = written()
        for e in events or ():
            month = e["ts"][:7] if isinstance(e.get("ts"), str) and e["ts"] else "2026-03"
            with open(store / f"events-{month}.jsonl", "a") as f:
                f.write(json.dumps(e) + "\n")
        with mock.patch.object(factory, "run", fake), redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(os, "getcwd", return_value=cwd or ROOT):
            code = factory.main(argv)
        fake.tasks = [r for r in written() if r not in seeded]
    return code, out.getvalue(), err.getvalue(), fake


PLUGINS = [{"id": "zeta@m", "enabled": True}, {"id": "factory@factory", "enabled": True},
           {"id": "alpha@m", "enabled": False}, {"id": "factory@inline", "enabled": True}]
LEAN_SETTINGS = {"disableBundledSkills": True, "enabledPlugins": {"alpha@m": False, "zeta@m": False}}


def repo_answers(entries, sessions=(), *extras):
    answers = {
        ("git", "worktree", "list"): (0, porcelain((ROOT, "main", False), *entries), ""),
        ("git", "symbolic-ref"): (0, "refs/remotes/origin/main\n", ""),
        ("claude", "agents"): (0, json.dumps(list(sessions)), ""),
        ("claude", "plugin", "list"): (0, json.dumps(PLUGINS), ""),
    }
    for extra in extras:
        answers.update(extra)
    return answers


class SlugTests(unittest.TestCase):
    def test_valid(self):
        for slug in ("abc", "tui-now-running", "a1b", "x" * 40, "a-b-c"):
            factory.validate_slug(slug)

    def test_invalid(self):
        for slug in ("ab", "x" * 41, "Abc", "a_b", "a--b", "-ab", "ab-", "a b", "agent-12ab", "ünï"):
            with self.assertRaises(factory.Fail, msg=slug):
                factory.validate_slug(slug)


BUSY = [{"id": "s1", "cwd": f"{WT}/x", "kind": "background", "status": "busy", "state": "working"}]


class PhaseTests(unittest.TestCase):
    def test_every_row_of_the_table(self):
        want = {  # state -> pull request state -> phase, with no session unfinished
            None: dict(none="motion", closed="motion", unknown="motion", open="review", merged="merged"),
            "working": dict(none="motion", closed="motion", unknown="motion", open="motion", merged="motion"),
            "blocked": dict(none="blocked", closed="blocked", unknown="blocked", open="blocked", merged="blocked"),
            "handed-over": dict(none="review", closed="review", unknown="review", open="review", merged="merged"),
            "acknowledged": dict(none="settled", closed="settled", unknown="settled", open="settled", merged="merged"),
            "dismissed": dict(none="settled", closed="settled", unknown="settled", open="settled", merged="merged"),
        }
        for state, by_pr in want.items():
            for pr, expected in by_pr.items():
                with self.subTest(state=state, pr=pr):
                    self.assertEqual(factory.phase(pr, [], state), expected)

    def test_an_unfinished_or_unplaced_session_is_always_motion(self):
        for state in (None, *factory.STATES):
            for pr in ("none", "open", "merged"):
                for mine in (BUSY, [{"kind": "interactive"}], None):
                    with self.subTest(state=state, pr=pr, mine=mine):
                        self.assertEqual(factory.phase(pr, mine, state), "motion")

    def test_a_finished_session_lets_go(self):
        done = [{"kind": "background", "status": "idle", "state": "done"}]
        self.assertEqual(factory.phase("open", done), "review")
        self.assertEqual(factory.phase("none", done, "handed-over"), "review")

    def test_finished_probes_in_the_worktree_do_not_hold_the_task(self):
        lead = {"id": "ld", "kind": "background", "status": "idle", "state": "done", "pid": 7, "cwd": f"{WT}/x"}
        probes = [{"id": "p1", "name": "probe-x", "kind": "background", "state": "done", "cwd": f"{WT}/x"},
                  {"id": "p2", "name": "probe2-x", "kind": "background", "state": "stopped", "cwd": f"{WT}/x"}]
        self.assertIsNone(factory.session_block([lead, *probes]))
        self.assertEqual(factory.phase("open", [lead, *probes], "handed-over"), "review")
        self.assertIsNone(factory.session_block([EXITED, *probes]))

    def test_state_defaults_to_undeclared(self):
        self.assertEqual(factory.phase("open", []), "review")
        self.assertEqual(factory.phase("none", []), "motion")


class StoreTests(unittest.TestCase):
    def read(self, lines=None):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            if lines is not None:
                Path(home, ".claude", "factory").mkdir(parents=True)
                Path(home, ".claude", "factory", "tasks.jsonl").write_text("\n".join(lines) + "\n")
            return {slug: r["state"] for slug, r in factory.read_tasks(ROOT).items()}

    def test_last_record_wins(self):
        lines = [json.dumps(task_rec("a", "working")), json.dumps(task_rec("a", "blocked")),
                 json.dumps(task_rec("b", "working"))]
        self.assertEqual(self.read(lines), {"a": "blocked", "b": "working"})

    def test_other_repos_are_ignored(self):
        lines = [json.dumps(task_rec("a", "working")), json.dumps(task_rec("a", "blocked", repo="/other"))]
        self.assertEqual(self.read(lines), {"a": "working"})

    def test_garbage_lines_are_skipped(self):
        lines = [json.dumps(task_rec("a", "working")), "not json", "[1]", '{"repo": "/repo"}', ""]
        self.assertEqual(self.read(lines), {"a": "working"})

    def test_unknown_last_state_is_undeclared(self):
        lines = [json.dumps(task_rec("a", "working")), json.dumps(task_rec("a", "nonsense")),
                 json.dumps(task_rec("b", None))]
        self.assertEqual(self.read(lines), {})

    def test_missing_file(self):
        self.assertEqual(self.read(), {})


class StateTests(unittest.TestCase):
    CWD = f"{WT}/x/sub"

    def state(self, *args, cwd=CWD, tasks=()):
        return invoke(["state", *args], repo_answers([task_entry("x")]), cwd=cwd, tasks=tasks)

    def test_each_state_is_written_with_the_repo_root_and_slug(self):
        for state in ("working", "blocked"):
            with self.subTest(state):
                code, out, err, fake = self.state(state)
                self.assertEqual((code, out, err), (0, f"x: {state}\n", ""))
                (rec,) = fake.tasks
                self.assertEqual((rec["repo"], rec["slug"], rec["state"], rec["role"], rec["model"]),
                                 (ROOT, "x", state, "lead", None))
                self.assertEqual(rec["session"], "sid-d")
                self.assertNotIn("deliverable", rec)

    def test_working_and_blocked_put_the_pull_request_back_to_draft_even_when_gh_fails(self):
        for state in ("working", "blocked"):
            with self.subTest(state):
                code, out, err, fake = invoke(["state", state], repo_answers(
                    [task_entry("x")], (), {("gh", "pr", "ready"): (1, "", "no pull request")}), cwd=self.CWD)
                self.assertEqual((code, out, err), (0, f"x: {state}\n", ""))
                self.assertIn(["gh", "pr", "ready", "--undo", "worktree-x"], fake.calls)

    def test_handed_over_does_not_touch_the_draft_flag(self):
        *_, fake = self.state("handed-over", "https://github.com/o/r/pull/9")
        self.assertFalse(fake.ran("gh", "pr", "ready"))

    def test_handed_over_stores_a_link_as_given(self):
        code, out, _, fake = self.state("handed-over", "https://github.com/o/r/pull/9")
        self.assertEqual((code, out), (0, "x: handed-over\n"))
        self.assertEqual(fake.tasks[0]["deliverable"], "https://github.com/o/r/pull/9")

    def test_handed_over_resolves_a_file(self):
        with tempfile.NamedTemporaryFile() as f:
            code, _, err, fake = self.state("handed-over", f.name)
            self.assertEqual(code, 0, err)
            self.assertEqual(fake.tasks[0]["deliverable"], os.path.realpath(f.name))

    def test_refusals_write_nothing(self):
        cases = {
            "outside any task worktree": (("working",), ROOT, "inside a task worktree"),
            "the main checkout's subdirectory": (("working",), f"{ROOT}/src", "inside a task worktree"),
            "a sibling of a task": (("working",), f"{WT}/x-other", "inside a task worktree"),
            "handed-over without a deliverable": (("handed-over",), self.CWD, "needs a deliverable"),
            "working with a deliverable": (("working", "https://x/y"), self.CWD, "take none"),
            "blocked with a deliverable": (("blocked", "https://x/y"), self.CWD, "take none"),
            "a missing file": (("handed-over", "/no/such/file"), self.CWD, "does not exist"),
        }
        for name, (args, cwd, pattern) in cases.items():
            with self.subTest(name):
                code, out, err, fake = self.state(*args, cwd=cwd)
                self.assertEqual((code, out, fake.tasks), (2, "", []))
                self.assertRegex(err, pattern)

    def test_from_the_worktree_root_and_a_subdirectory(self):
        for cwd in (f"{WT}/x", self.CWD):
            self.assertEqual(self.state("working", cwd=cwd)[0], 0)


class AckTests(unittest.TestCase):
    def ack(self, slug, state, tasks=()):
        return invoke(["ack", slug, state], repo_answers([task_entry("x")]), tasks=tasks)

    def test_refused_unless_handed_over(self):
        cases = {"unknown slug": ("ghost", [task_rec("ghost", "handed-over")]), "undeclared": ("x", []),
                 "working": ("x", [task_rec("x", "working")]), "blocked": ("x", [task_rec("x", "blocked")]),
                 "another repo's record": ("x", [task_rec("x", "handed-over", repo="/other")])}
        for name, (slug, tasks) in cases.items():
            with self.subTest(name):
                code, out, err, fake = self.ack(slug, "acknowledged", tasks)
                self.assertEqual((code, out, fake.tasks), (2, "", []))
                self.assertEqual(len(err.strip().splitlines()), 1)

    def test_accepted_after_handed_over_and_copies_the_deliverable(self):
        for state in ("acknowledged", "dismissed"):
            with self.subTest(state):
                code, out, err, fake = self.ack("x", state, [task_rec("x", "handed-over", "https://x/y")])
                self.assertEqual((code, out, err), (0, f"x: {state}\n", ""))
                (rec,) = fake.tasks
                self.assertEqual((rec["state"], rec["deliverable"], rec["role"], rec["slug"], rec["repo"]),
                                 (state, "https://x/y", "dispatcher", "x", ROOT))

    def test_a_settled_task_can_be_settled_again_and_working_voids_it(self):
        code, _, _, fake = self.ack("x", "dismissed", [task_rec("x", "acknowledged", "/f")])
        self.assertEqual((code, fake.tasks[0]["deliverable"]), (0, "/f"))
        code, _, err, _ = self.ack("x", "acknowledged", [task_rec("x", "acknowledged", "/f"), task_rec("x", "working")])
        self.assertEqual(code, 2, err)


DEFAULT_SETTINGS = {
    "worktree_cap": 5, "reserve_percent": 10, "task_time_limit_hours": 2, "lead_permission_mode": "auto",
    "lean_start": True, "dispatchers": None, "lgtm_from_author": True, "monitor": None,
    "models": {"dispatcher": "fable", "lead": "fable", "explorer": "haiku", "coder": "sonnet", "reviewer": "opus",
               "lander": "sonnet", "site_manager": "sonnet", "courier": "haiku"}}

# A settings file as a human leaves it: tables and keys in their own order, comments, blank lines, an unknown key.
HAND_EDITED = '''# my notes
[paths]
monitor = "/x"   # where

[limits]  # caps
reserve_percent = 20
worktree_cap = 3  # three
task_time_limit_hours = 1.5

[models]
coder = 'sonnet'   # mine

[sessions]
dispatchers = ["a", "b"]  # names
extra = 1
'''


class InStore(unittest.TestCase):
    """A temporary factory home whose settings.toml is where the script looks."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = self.dir / "settings.toml"
        patcher = mock.patch.object(factory, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        factory.warned.clear()

    def load(self, text=None):
        if text is not None:
            self.path.write_text(text)
        err = io.StringIO()
        with redirect_stderr(err):
            self.settings = factory.load_settings()
        self.err = err.getvalue()
        return self.settings


class SettingsTests(InStore):
    def test_defaults(self):
        self.assertEqual(self.load(), DEFAULT_SETTINGS)
        self.assertEqual(self.load(""), DEFAULT_SETTINGS)
        self.assertEqual(self.load("[models]\n[limits]\n"), DEFAULT_SETTINGS)
        self.assertEqual(factory.SETTINGS_DEFAULTS, DEFAULT_SETTINGS)

    def test_the_shipped_text_loads_to_the_defaults_and_says_each_key_once(self):
        text = factory.settings_text()
        self.assertEqual(self.load(text), DEFAULT_SETTINGS)
        for s in factory.SETTINGS:
            self.assertEqual(len(re.findall(rf"^#? ?{s.key} = ", text, re.M)), 1, s.key)
            self.assertIn(f"# {s.comment}\n", text)
        self.assertIn('\n[models]\n# Advice only: the human starts the dispatcher. Default "fable".\ndispatcher = "fable"\n', text)
        self.assertIn('# monitor = "/path/to/ccmon/bin/isshoni"\n', text)
        self.assertEqual(self.err, "")

    def test_every_key_is_read_from_the_file(self):
        got = self.load('''[models]
dispatcher = "opus"
lead = "claude-opus-4-1[1m]"
explorer = "sonnet"
coder = "haiku"
reviewer = "fable"
lander = "claude-haiku-4-5"
site_manager = "opus"
courier = "sonnet"
[limits]
worktree_cap = 2
reserve_percent = 25
task_time_limit_hours = 0.5
[sessions]
lead_permission_mode = "plan"
lean_start = false
dispatchers = ["dispatcher-a"]
[merging]
lgtm_from_author = false
[paths]
monitor = "/bin/isshoni"
''')
        self.assertEqual(got, {
            "worktree_cap": 2, "reserve_percent": 25, "task_time_limit_hours": 0.5, "lead_permission_mode": "plan",
            "lean_start": False, "dispatchers": ["dispatcher-a"], "lgtm_from_author": False, "monitor": "/bin/isshoni",
            "models": {"dispatcher": "opus", "lead": "claude-opus-4-1[1m]", "explorer": "sonnet", "coder": "haiku",
                       "reviewer": "fable", "lander": "claude-haiku-4-5", "site_manager": "opus", "courier": "sonnet"}})
        for percent in (0, 100):
            self.assertEqual(self.load(f"[limits]\nreserve_percent = {percent}\n")["reserve_percent"], percent)
        self.assertEqual(self.load("[limits]\ntask_time_limit_hours = 3\n")["task_time_limit_hours"], 3)
        self.assertEqual(self.load("[limits]\nworktree_cap = 9\n")["models"]["lead"], "fable")

    def test_the_monitor_is_expanded_from_home(self):
        self.assertEqual(self.load('[paths]\nmonitor = "~/bin/isshoni"\n')["monitor"], os.path.expanduser("~/bin/isshoni"))

    def test_invalid_values_name_file_line_and_key(self):
        for table, key, bad in (
                ("limits", "worktree_cap", "0"), ("limits", "worktree_cap", '"3"'), ("limits", "worktree_cap", "true"),
                ("limits", "worktree_cap", "2.5"), ("limits", "task_time_limit_hours", "0"),
                ("limits", "task_time_limit_hours", "-1"), ("limits", "task_time_limit_hours", '"2"'),
                ("limits", "task_time_limit_hours", "true"), ("limits", "reserve_percent", "true"),
                ("limits", "reserve_percent", "-1"), ("limits", "reserve_percent", "101"), ("limits", "reserve_percent", "2.5"),
                ("limits", "reserve_percent", '"10"'), ("sessions", "lead_permission_mode", '"yolo"'),
                ("sessions", "lean_start", '"yes"'), ("sessions", "lean_start", "1"),
                ("sessions", "dispatchers", '"a"'), ("sessions", "dispatchers", "[1]"), ("sessions", "dispatchers", '[""]'),
                ("merging", "lgtm_from_author", '"no"'), ("merging", "lgtm_from_author", "0"),
                ("paths", "monitor", '""'), ("paths", "monitor", "3")):
            with self.subTest(f"{table}.{key} = {bad}"):
                with self.assertRaises(factory.Fail) as raised:
                    self.load(f"# c\n\n[{table}]\n{key} = {bad}\n")
                self.assertTrue(str(raised.exception).startswith(f"settings.toml line 4: {table}.{key} "), raised.exception)

    def test_the_message_for_the_cap(self):
        with self.assertRaisesRegex(factory.Fail, r"^settings.toml line 3: limits.worktree_cap must be an integer of at least 1$"):
            self.load("\n[limits]\nworktree_cap = 0\n")

    def test_a_bad_model_fails_for_each_role(self):
        for s in factory.SETTINGS:
            if s.table != "models":
                continue
            for bad in ('"sonet"', "3", '""', '"claude-"', '"Claude-opus"', '"claude-opus 4"', '"claude-x[1m"'):
                with self.subTest(f"{s.key} = {bad}"):
                    with self.assertRaises(factory.Fail) as raised:
                        self.load(f"[models]\n{s.key} = {bad}\n")
                    self.assertTrue(str(raised.exception).startswith(f"settings.toml line 2: models.{s.key}: "), raised.exception)
        with self.assertRaises(factory.Fail) as raised:
            self.load('[models]\ncoder = "sonet"\n')
        self.assertEqual(str(raised.exception), 'settings.toml line 2: models.coder: "sonet" is not a model '
                                                '(an alias: fable, opus, sonnet, haiku; or a full id starting claude-)')
        for good in ("fable", "opus", "sonnet", "haiku", "claude-opus-4-1", "claude-sonnet-4-5-20250929", "claude-opus-4-1[1m]",
                     "opus[1m]", "haiku[200k]"):
            self.assertEqual(self.load(f'[models]\ncoder = "{good}"\n')["models"]["coder"], good)

    def test_a_table_where_a_value_is_wrong(self):
        with self.assertRaisesRegex(factory.Fail, "settings.toml: models must be a table"):
            self.load("models = 3\n")

    def test_a_syntax_error_names_the_file(self):
        for text in ("[limits\n", "worktree_cap = \n", "not toml"):
            with self.assertRaisesRegex(factory.Fail, f"^{re.escape(str(self.path))}: "):
                self.load(text)

    def test_an_unknown_table_or_key_is_reported_once_and_ignored(self):
        got = self.load("[models]\ncoder = \"haiku\"\nflavour = 1\n\n[limits]\n[extra]\ny = 1\nz = 2\n")
        self.assertEqual(got["models"]["coder"], "haiku")
        self.assertEqual(sorted(self.err.splitlines()), [
            "factory: settings.toml line 3: unknown key models.flavour, ignored",
            "factory: settings.toml line 6: unknown table extra, ignored"])
        self.load()
        self.assertEqual(self.err, "")  # said already, once per command

    def test_the_agent_files_pin_the_default_models(self):
        for path in (BIN.parent.parent / "agents").glob("*.md"):
            head = path.read_text().split("---")[1]
            self.assertEqual(re.search(r"^model:\s*(\S+)", head, re.M)[1], factory.SETTINGS_DEFAULTS["models"][path.stem], path)


class SetTests(InStore):
    def set(self, key, value, text=HAND_EDITED):
        if text is not None:
            self.path.write_text(text)
        return factory.set_setting(key, value)

    def test_it_changes_one_line_and_keeps_every_other_byte(self):
        for key, value, shown, old, new in (
                ("limits.worktree_cap", "8", "8", "worktree_cap = 3  # three", "worktree_cap = 8  # three"),
                ("limits.task_time_limit_hours", "2.5", "2.5", "task_time_limit_hours = 1.5", "task_time_limit_hours = 2.5"),
                ("models.coder", "haiku", '"haiku"', "coder = 'sonnet'   # mine", 'coder = "haiku"   # mine'),
                ("paths.monitor", "/y/isshoni", '"/y/isshoni"', 'monitor = "/x"   # where', 'monitor = "/y/isshoni"   # where'),
                ("sessions.dispatchers", '["c"]', '["c"]', 'dispatchers = ["a", "b"]  # names', 'dispatchers = ["c"]  # names')):
            with self.subTest(key):
                self.assertEqual(self.set(key, value), f"set {key} = {shown}")
                self.assertEqual(self.path.read_text(), HAND_EDITED.replace(old, new))
                self.load()

    def test_a_string_with_a_hash_and_a_comment_after_it(self):
        self.set("paths.monitor", "/z", '[paths]\nmonitor = "/a#b"  # c # d\n')
        self.assertEqual(self.path.read_text(), '[paths]\nmonitor = "/z"  # c # d\n')

    def test_the_same_value_is_unchanged_and_the_file_is_not_touched(self):
        self.path.write_text(HAND_EDITED)
        before = os.stat(self.path).st_mtime_ns
        self.assertEqual(self.set("limits.worktree_cap", "3", text=None), "unchanged")
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)
        self.assertEqual(self.set("models.coder", "sonnet", text=None), 'set models.coder = "sonnet"')  # 'sonnet' becomes "sonnet"

    def test_a_key_absent_from_its_table_is_added_at_the_end_of_the_table(self):
        self.set("models.lead", "opus")
        self.assertEqual(self.path.read_text(), HAND_EDITED.replace("coder = 'sonnet'   # mine\n", "coder = 'sonnet'   # mine\nlead = \"opus\"\n"))
        self.set("paths.monitor", "/m", "[paths]\n# monitor = \"/path\"\n\n[limits]\nworktree_cap = 2\n")
        self.assertEqual(self.path.read_text(), "[paths]\n# monitor = \"/path\"\nmonitor = \"/m\"\n\n[limits]\nworktree_cap = 2\n")
        self.set("limits.reserve_percent", "0", "[limits]\nworktree_cap = 2")  # no newline at the end
        self.assertEqual(self.path.read_text(), "[limits]\nworktree_cap = 2\nreserve_percent = 0\n")

    def test_a_table_absent_is_appended(self):
        self.set("merging.lgtm_from_author", "false")
        self.assertEqual(self.path.read_text(), HAND_EDITED + "\n[merging]\nlgtm_from_author = false\n")
        self.set("merging.lgtm_from_author", "true", "")
        self.assertEqual(self.path.read_text(), "[merging]\nlgtm_from_author = true\n")

    def test_a_file_absent_gets_the_shipped_text_first(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(self.set("limits.worktree_cap", "9", text=None), "set limits.worktree_cap = 9")
        self.assertEqual(self.path.read_text(), factory.settings_text({("limits", "worktree_cap"): 9}))
        self.assertEqual(self.load()["worktree_cap"], 9)
        shutil.rmtree(self.dir / "sub", ignore_errors=True)
        with mock.patch.object(factory, "SETTINGS_PATH", self.dir / "sub" / "settings.toml"):
            self.assertEqual(factory.set_setting("limits.worktree_cap", "5"), "set limits.worktree_cap = 5")  # the default, new file
            self.assertTrue((self.dir / "sub" / "settings.toml").exists())

    def test_an_invalid_value_leaves_the_file_byte_identical(self):
        for key, value in (("limits.worktree_cap", "0"), ("limits.worktree_cap", "many"), ("models.coder", "sonet"),
                           ("sessions.lean_start", "maybe"), ("sessions.dispatchers", "a,b"), ("paths.monitor", " "),
                           ("sessions.lead_permission_mode", "yolo"), ("nope.key", "1"), ("limits.nope", "1")):
            with self.subTest(f"{key} {value}"):
                self.path.write_text(HAND_EDITED)
                with self.assertRaises(factory.Fail):
                    factory.set_setting(key, value)
                self.assertEqual(self.path.read_text(), HAND_EDITED)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.toml"])

    def test_a_line_the_editor_cannot_handle_is_refused_untouched(self):
        for text in ('[sessions]\ndispatchers = [\n  "a",\n]\n', '[limits]\nworktree_cap = [\n3]\n', "[limits]\nworktree_cap = 3 4\n"):
            self.path.write_text(text)
            with self.assertRaisesRegex(factory.Fail, "edit the file by hand"):
                factory.set_setting("sessions.dispatchers" if "dispatchers" in text else "limits.worktree_cap",
                                    '["b"]' if "dispatchers" in text else "8")
            self.assertEqual(self.path.read_text(), text)

    def test_line_endings_are_kept(self):
        crlf = HAND_EDITED.replace("\n", "\r\n")
        for key, value, old, new in (("limits.worktree_cap", "8", "worktree_cap = 3  # three", "worktree_cap = 8  # three"),
                                     ("models.coder", "haiku", "coder = 'sonnet'   # mine", 'coder = "haiku"   # mine')):
            self.path.write_bytes(crlf.encode())
            factory.set_setting(key, value)
            self.assertEqual(self.path.read_bytes(), crlf.replace(old, new).encode())
        self.set("models.lead", "opus", crlf)  # a key added to a table
        self.assertEqual(self.path.read_bytes(), crlf.replace("coder = 'sonnet'   # mine\r\n", "coder = 'sonnet'   # mine\r\nlead = \"opus\"\r\n").encode())
        self.set("merging.lgtm_from_author", "false", crlf)  # a table added
        self.assertEqual(self.path.read_bytes(), (crlf + "\r\n[merging]\r\nlgtm_from_author = false\r\n").encode())
        self.set("limits.worktree_cap", "8", HAND_EDITED.rstrip("\n"))  # no newline at the end
        self.assertEqual(self.path.read_text(), HAND_EDITED.replace("worktree_cap = 3", "worktree_cap = 8"))
        self.assertEqual(self.load()["worktree_cap"], 8)

    def test_a_table_that_is_not_a_table_is_refused_untouched(self):
        for text in ("[[limits]]\nworktree_cap = 3\n", "limits = 3\n", "limits = {worktree_cap = 3}\n"):
            self.path.write_bytes(text.encode())
            with self.assertRaisesRegex(factory.Fail, "edit the file by hand"):
                factory.set_setting("limits.worktree_cap", "8")
            self.assertEqual(self.path.read_text(), text)

    def test_a_file_that_is_not_toml_is_refused_untouched(self):
        self.path.write_text("[limits\n")
        with self.assertRaisesRegex(factory.Fail, "edit the file by hand"):
            factory.set_setting("limits.worktree_cap", "8")
        self.assertEqual(self.path.read_text(), "[limits\n")

    def test_a_wrong_value_elsewhere_does_not_stop_the_repair_of_that_key(self):
        self.set("limits.worktree_cap", "4", "[limits]\nworktree_cap = 0\nreserve_percent = 200\n")
        self.assertEqual(self.path.read_text(), "[limits]\nworktree_cap = 4\nreserve_percent = 200\n")

    def test_it_follows_a_symlink_keeps_the_link_and_the_mode(self):
        real = self.dir / "real.toml"
        real.write_text(HAND_EDITED)
        real.chmod(0o640)
        self.path.symlink_to(real)
        factory.set_setting("limits.worktree_cap", "8")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(real.read_text(), HAND_EDITED.replace("worktree_cap = 3", "worktree_cap = 8"))
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o640)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["real.toml", "settings.toml"])

    def test_the_command_prints_what_it_did(self):
        for argv, want in ((["settings", "set", "limits.worktree_cap", "8"], "set limits.worktree_cap = 8\n"),
                           (["settings", "set", "limits.worktree_cap", "8"], "unchanged\n")):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(factory.main(argv), 0)
            self.assertEqual(out.getvalue(), want)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(factory.main(["settings", "set", "limits.worktree_cap", "0"]), 2)
        self.assertEqual(err.getvalue(), "factory: limits.worktree_cap must be an integer of at least 1\n")


class MigrationTests(InStore):
    def legacy(self, data):
        (self.dir / "settings.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def old_reader(self):
        """What a version before settings.toml reads: the JSON, key by key."""
        return json.loads((self.dir / "settings.json").read_text())

    def test_this_machines_shape(self):
        self.legacy({"worktree_cap": 7, "monitor": "/m/isshoni"})
        before = (self.dir / "settings.json").read_bytes()
        got = self.load()
        self.assertEqual(got, {**DEFAULT_SETTINGS, "worktree_cap": 7, "monitor": "/m/isshoni"})
        self.assertEqual(self.path.read_text(), factory.settings_text({("limits", "worktree_cap"): 7, ("paths", "monitor"): "/m/isshoni"}))
        self.assertIn('\nmonitor = "/m/isshoni"\n', self.path.read_text())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["settings.json", "settings.toml"])
        self.assertEqual((self.dir / "settings.json").read_bytes(), before)  # untouched, not renamed
        self.assertEqual(self.err, f"factory: wrote {self.path} from {self.dir / 'settings.json'}, which stays for older versions\n")

    def test_the_old_reader_still_gets_the_old_value_after_the_migration_and_after_a_set(self):
        self.legacy({"worktree_cap": 7, "monitor": "/m"})
        self.load()
        self.assertEqual(self.old_reader()["worktree_cap"], 7)
        with redirect_stderr(io.StringIO()):
            factory.set_setting("limits.worktree_cap", "9")
        self.assertEqual((self.old_reader()["worktree_cap"], self.load()["worktree_cap"]), (7, 9))  # not seen by the other version

    def test_all_eight_keys_and_the_dispatchers(self):
        old = {"worktree_cap": 2, "lead_model": "opus", "lead_permission_mode": "plan", "task_time_limit_hours": 0.5,
               "monitor": "/bin/isshoni", "lgtm_from_author": False, "reserve_percent": 25, "lean_start": False,
               "dispatchers": ["dispatcher-x"]}
        self.legacy(old)
        got = self.load()
        self.assertEqual(got, {**DEFAULT_SETTINGS, "worktree_cap": 2, "lead_permission_mode": "plan",
                               "task_time_limit_hours": 0.5, "monitor": "/bin/isshoni", "lgtm_from_author": False,
                               "reserve_percent": 25, "lean_start": False, "dispatchers": ["dispatcher-x"],
                               "models": {**DEFAULT_SETTINGS["models"], "lead": "opus"}})
        self.assertNotIn("not carried", self.err)
        self.assertNotIn("differs", self.err)

    def test_unknown_keys_are_named(self):
        self.legacy({"worktree_cap": 3, "dashboard_theme": "dark", "zeta": 1})
        self.load()
        self.assertTrue(self.err.rstrip().endswith("; not carried over: dashboard_theme, zeta"), self.err)
        self.assertEqual(self.settings["worktree_cap"], 3)

    def test_invalid_json_writes_nothing(self):
        for text in ("{not json", "[1]", "null"):
            self.legacy(text)
            with self.assertRaisesRegex(factory.Fail, f"settings file {re.escape(str(self.dir / 'settings.json'))} "):
                self.load()
            self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["settings.json"])
            self.assertEqual((self.dir / "settings.json").read_text(), text)

    def test_an_invalid_value_in_the_json_writes_nothing(self):
        for text, key in (('{"worktree_cap": 0}', "worktree_cap"), ('{"lead_model": ""}', "lead_model"),
                          ('{"monitor": null}', "monitor"), ('{"lean_start": "yes"}', "lean_start")):
            self.legacy(text)
            with self.assertRaisesRegex(factory.Fail, f"settings.json: {key}"):
                self.load()
            self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["settings.json"])

    def test_a_migration_that_would_not_read_back_the_same_writes_nothing(self):
        self.legacy({"worktree_cap": 7})
        for wrong in ("[limits]\nworktree_cap = 8\n", "[limits\n"):  # text that reads back as other values, or not at all
            with mock.patch.object(factory, "settings_text", lambda values, wrong=wrong: wrong):
                with self.assertRaisesRegex(factory.Fail, r"settings\.json would not read back the same as TOML"):
                    self.load()
            self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["settings.json"])
        self.legacy({"worktree_cap": 7, "monitor": "/a b/\u00e9\"x\\y"})  # what json and TOML both escape is fine
        self.assertEqual(self.load()["monitor"], '/a b/\u00e9"x\\y')

    def test_the_toml_wins_and_a_differing_key_is_reported_once_per_command(self):
        self.legacy({"worktree_cap": 7, "lead_model": "opus", "reserve_percent": 10})
        self.path.write_text("[limits]\nworktree_cap = 2\n")  # lead and reserve: the TOML says the defaults
        self.assertEqual(self.load()["worktree_cap"], 2)
        self.assertEqual(self.err, "factory: settings.json (read only by older versions) differs from settings.toml in "
                                   "worktree_cap, lead_model; delete settings.json once no older version runs\n")
        self.load()
        self.assertEqual(self.err, "")  # said already
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["settings.json", "settings.toml"])

    def test_no_line_when_the_two_agree_or_there_is_no_json(self):
        self.legacy({"worktree_cap": 2})
        self.load("[limits]\nworktree_cap = 2\n")
        self.assertEqual(self.err, "")
        (self.dir / "settings.json").unlink()
        self.load()
        self.assertEqual(self.err, "")

    def test_a_json_that_cannot_be_read_is_reported_not_fatal(self):
        for text in ("{not json", "[1]", '{"worktree_cap": 0}'):
            factory.warned.clear()
            self.legacy(text)
            self.assertEqual(self.load("[limits]\nworktree_cap = 2\n")["worktree_cap"], 2)
            self.assertIn("settings.json (read only by older versions) cannot be used: ", self.err)
            self.assertTrue(self.err.endswith("; delete it once no older version runs\n"), self.err)

    def test_a_hook_run_with_differing_files_is_silent(self):
        self.legacy({"worktree_cap": 7})
        self.path.write_text("[limits]\nworktree_cap = 2\n")
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        env = {**os.environ, "HOME": str(home), "FACTORY_HOME": str(self.dir)}
        p = subprocess.run([sys.executable, str(BIN), "hook"], input="{}", capture_output=True, text=True, env=env)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))

    def test_when_both_files_exist_the_json_is_not_migrated_over_the_toml(self):
        self.legacy({"worktree_cap": 7})
        self.path.write_text("[limits]\nworktree_cap = 2\n")
        self.assertEqual(self.load()["worktree_cap"], 2)
        self.assertEqual(self.path.read_text(), "[limits]\nworktree_cap = 2\n")

    def test_the_second_run_is_a_no_op(self):
        self.legacy({"worktree_cap": 7, "monitor": "/m"})
        first = self.load()
        text, mtime = self.path.read_text(), os.stat(self.path).st_mtime_ns
        factory.warned.clear()
        self.assertEqual(self.load(), first)
        self.assertEqual((self.path.read_text(), os.stat(self.path).st_mtime_ns, self.err), (text, mtime, ""))

    def test_a_racing_process_writes_the_same_file(self):
        self.legacy({"worktree_cap": 7})
        real = factory.migrate_json

        def other_process(path):  # it migrates between this process's read and its own migration
            real(path)
            real(path)

        with mock.patch.object(factory, "migrate_json", other_process):
            self.assertEqual(self.load()["worktree_cap"], 7)
        self.assertEqual(self.path.read_text(), factory.settings_text({("limits", "worktree_cap"): 7}))

    def test_set_reports_the_difference_it_has_just_made(self):
        self.legacy({"worktree_cap": 7})
        self.path.write_text("[limits]\nworktree_cap = 7\n")
        err = io.StringIO()
        with redirect_stderr(err):
            factory.set_setting("limits.worktree_cap", "9")
        self.assertEqual(err.getvalue(), "factory: settings.json (read only by older versions) differs from settings.toml in "
                                         "worktree_cap; delete settings.json once no older version runs\n")
        factory.warned.clear()
        err = io.StringIO()
        with redirect_stderr(err):
            factory.set_setting("limits.worktree_cap", "7")
        self.assertEqual(err.getvalue(), "")  # agreeing again

    def test_set_migrates_first_and_leaves_the_json(self):
        self.legacy({"worktree_cap": 7, "monitor": "/m"})
        before = (self.dir / "settings.json").read_bytes()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(factory.set_setting("paths.monitor", "/n"), 'set paths.monitor = "/n"')
        self.assertEqual(self.load(), {**DEFAULT_SETTINGS, "worktree_cap": 7, "monitor": "/n"})
        self.assertEqual((self.dir / "settings.json").read_bytes(), before)


class AgentDefinitionTests(unittest.TestCase):
    MODELS = {**DEFAULT_SETTINGS["models"], "coder": "haiku", "lander": "claude-haiku-4-5"}

    def files(self, text_by_name):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / "agents").mkdir()
        for name, text in text_by_name.items():
            (d / "agents" / f"{name}.md").write_text(text)
        return d

    def test_the_shipped_agents_become_factory_entries_with_the_configured_models(self):
        got = factory.agent_definitions(self.MODELS)
        self.assertEqual(sorted(got), ["factory:coder", "factory:explorer", "factory:lander", "factory:reviewer"])
        self.assertEqual({k: v["model"] for k, v in got.items()}, {
            "factory:coder": "haiku", "factory:explorer": "haiku", "factory:lander": "claude-haiku-4-5", "factory:reviewer": "opus"})
        agents = BIN.parent.parent / "agents"
        for name, entry in ((k.split(":")[1], v) for k, v in got.items()):
            text = (agents / f"{name}.md").read_text()
            head, body = text.removeprefix("---\n").split("\n---\n", 1)
            self.assertEqual(entry["prompt"], body, name)
            self.assertEqual(entry["description"], re.search(r"^description: (.*)$", head, re.M)[1], name)
            tools = re.search(r"^tools: (.*)$", head, re.M)
            self.assertEqual(entry.get("tools"), tools and [t.strip() for t in tools[1].split(",")], name)
            self.assertEqual(entry.get("skills"), re.findall(r"^  - (.*)$", head, re.M) or None, name)
        self.assertEqual(got["factory:explorer"]["tools"], ["Read", "Grep", "Glob", "Bash"])
        self.assertEqual(got["factory:coder"]["skills"], ["coding-taste"])
        self.assertNotIn("tools", got["factory:coder"])

    def test_an_unknown_frontmatter_key_fails_naming_file_and_key(self):
        d = self.files({"coder": "---\nname: coder\ndescription: d\nmodel: sonnet\nmaxTurns: 3\n---\nbody\n"})
        with mock.patch.object(factory, "PLUGIN_ROOT", d):
            with self.assertRaisesRegex(factory.Fail, r"coder\.md: frontmatter key 'maxTurns'"):
                factory.agent_definitions(self.MODELS)

    def test_an_agent_without_a_models_key_fails(self):
        d = self.files({"janitor": "---\nname: janitor\ndescription: d\nmodel: haiku\n---\nbody\n"})
        with mock.patch.object(factory, "PLUGIN_ROOT", d):
            with self.assertRaisesRegex(factory.Fail, r"janitor\.md: no models\.janitor setting"):
                factory.agent_definitions(self.MODELS)


class ReserveTests(unittest.TestCase):
    def test_reserve_is_a_rounded_up_percent_that_never_takes_the_last_slot(self):
        for cap, want in ((1, 0), (2, 1), (5, 1), (10, 1), (11, 2), (15, 2), (20, 2), (21, 3)):
            self.assertEqual(factory.reserve(cap, 10), want, cap)
        for cap in (1, 2, 7, 40):
            self.assertEqual(factory.reserve(cap, 0), 0)
        self.assertEqual(factory.reserve(5, 100), 4)


class NewTests(unittest.TestCase):
    ARGS = ["new", "demo-task", "--brief", "do it", "--dispatcher", "d1"]
    OK = {("claude", "--bg"): (0, "backgrounded · 96dc4869 · demo-task\n", "")}

    def answers(self, entries, sessions=(), *extras, dispatcher="d1"):
        return repo_answers(entries, [dict(DISPATCHER, name=dispatcher), *sessions], *extras)

    def test_launch_argv(self):
        with mock.patch.object(factory.time, "time", return_value=1794610677):
            code, out, err, fake = invoke(self.ARGS, self.answers([], (), self.OK))
        self.assertEqual(code, 0, err)
        self.assertIn("96dc4869", out)
        self.assertIn("demo-task", out)
        plugin = str(BIN.resolve().parent.parent)
        lead = (BIN.resolve().parent.parent / "roles" / "lead.md").read_text()
        want = ["claude", "--bg", "--worktree", "demo-task", "-n", "demo-task", "--model", "fable",
                "--agents", fake.calls[-1][fake.calls[-1].index("--agents") + 1],
                "--permission-mode", "auto", "--plugin-dir", plugin, "--strict-mcp-config",
                "--settings", json.dumps(LEAN_SETTINGS), f"--tools={factory.LEAD_TOOLS}", "--append-system-prompt", lead,
                "Your task slug is demo-task. The dispatcher's session name is d1. "
                "Your base branch is main (remote origin). You started at 2026-11-13T22:57:57Z. "
                "Your time limit is 2 hours. Your brief follows.\n\ndo it"]
        self.assertEqual(fake.calls[-1], want)

    def test_lean_start_off_launches_the_old_argv_without_listing_plugins(self):
        with mock.patch.object(factory.time, "time", return_value=1794610677):
            _, _, _, fake = invoke(self.ARGS, self.answers([], (), self.OK), '[sessions]\nlean_start = false\n')
        plugin = str(BIN.resolve().parent.parent)
        lead = (BIN.resolve().parent.parent / "roles" / "lead.md").read_text()
        self.assertEqual(fake.calls[-1], [
            "claude", "--bg", "--worktree", "demo-task", "-n", "demo-task", "--model", "fable",
            "--agents", fake.calls[-1][fake.calls[-1].index("--agents") + 1],
            "--permission-mode", "auto", "--plugin-dir", plugin, "--append-system-prompt", lead,
            "Your task slug is demo-task. The dispatcher's session name is d1. "
            "Your base branch is main (remote origin). You started at 2026-11-13T22:57:57Z. "
            "Your time limit is 2 hours. Your brief follows.\n\ndo it"])
        self.assertFalse(fake.ran("claude", "plugin"))

    def test_a_failing_plugin_list_still_launches_without_enabled_plugins(self):
        answers = self.answers([], (), self.OK)
        for listing in ((1, "", "boom"), (0, "not json", ""), (0, '{"a": 1}', ""), (0, "[1]", ""), (124, "", "timed out")):
            answers[("claude", "plugin", "list")] = listing
            with self.subTest(listing=listing):
                code, _, err, fake = invoke(self.ARGS, answers)
                argv = fake.calls[-1]
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"disableBundledSkills": True})
                self.assertIn("plugin list", err)
                self.assertTrue(argv[-1].endswith("do it"))

    def test_the_lean_settings_never_name_the_factory(self):
        _, _, _, fake = invoke(self.ARGS, self.answers([], (), self.OK))
        argv = fake.calls[-1]
        off = json.loads(argv[argv.index("--settings") + 1])["enabledPlugins"]
        self.assertEqual(list(off), ["alpha@m", "zeta@m"])
        self.assertNotIn("factory", "".join(off))

    def test_launch_argv_uses_settings(self):
        settings = '[models]\nlead = "opus"\n[sessions]\nlead_permission_mode = "plan"\n'
        _, _, _, fake = invoke(self.ARGS, self.answers([], (), self.OK), settings)
        argv = fake.calls[-1]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")

    def test_the_lead_gets_its_subagents_through_agents_with_the_configured_models(self):
        settings = '[models]\ncoder = "haiku"\nreviewer = "claude-opus-4-1[1m]"\n'
        _, _, _, fake = invoke(self.ARGS, self.answers([], (), self.OK), settings)
        argv = fake.calls[-1]
        agents = json.loads(argv[argv.index("--agents") + 1])
        self.assertEqual(sorted(agents), ["factory:coder", "factory:explorer", "factory:lander", "factory:reviewer"])
        self.assertEqual({k.split(":")[1]: v["model"] for k, v in agents.items()},
                         {"coder": "haiku", "explorer": "haiku", "lander": "sonnet", "reviewer": "claude-opus-4-1[1m]"})
        coder = (BIN.parent.parent / "agents" / "coder.md").read_text().split("\n---\n", 1)[1]
        self.assertEqual(agents["factory:coder"]["prompt"], coder)
        self.assertEqual(agents["factory:explorer"]["tools"], ["Read", "Grep", "Glob", "Bash"])
        self.assertEqual(argv[argv.index("--model") + 1], "fable")

    def test_lead_model_overrides_the_setting_for_this_task_and_is_recorded(self):
        started = []
        with mock.patch.object(factory, "task_event", lambda kind, repo, slug, **kw: started.append((kind, slug, kw))):
            _, _, _, fake = invoke([*self.ARGS, "--lead-model", "opus"], self.answers([], (), self.OK), '[models]\nlead = "haiku"\n')
        argv = fake.calls[-1]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(started, [("TaskStarted", "demo-task", {
            "base": "main", "lead_model": "opus",
            "models": {"coder": "sonnet", "explorer": "haiku", "lander": "sonnet", "reviewer": "opus"}})])
        started.clear()
        with mock.patch.object(factory, "task_event", lambda kind, repo, slug, **kw: started.append((kind, slug, kw))):
            _, _, _, fake = invoke(self.ARGS, self.answers([], (), self.OK), '[models]\nlead = "haiku"\n')
        self.assertEqual(started[0][2]["lead_model"], "haiku")

    def test_a_bad_lead_model_fails_before_anything_is_created(self):
        for bad in ("sonet", "", "claude-"):
            code, out, err, fake = invoke([*self.ARGS, "--lead-model", bad], self.answers([], (), self.OK))
            self.assertEqual((code, out, fake.calls, err.startswith("factory: --lead-model: ")), (2, "", [], True), bad)

    def test_brief_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
            f.write("from file")
            f.flush()
            args = ["new", "demo-task", "--brief-file", f.name, "--dispatcher", "d1"]
            code, _, _, fake = invoke(args, self.answers([], (), self.OK))
        self.assertEqual(code, 0)
        self.assertTrue(fake.calls[-1][-1].endswith("\n\nfrom file"))

    def test_dispatcher_defaults_to_the_repo_directory_name(self):
        args = ["new", "demo-task", "--brief", "x"]
        code, _, err, fake = invoke(args, self.answers([], (), self.OK, dispatcher="dispatcher-repo"))
        self.assertEqual(code, 0, err)
        self.assertIn("The dispatcher's session name is dispatcher-repo.", fake.calls[-1][-1])

    def test_precondition_errors_exit_2_and_start_nothing(self):
        cases = {
            "bad slug": (["new", "Bad", "--brief", "x", "--dispatcher", "d1"], self.answers([], (), self.OK), "invalid slug"),
            "agent prefix": (["new", "agent-abc", "--brief", "x", "--dispatcher", "d1"], self.answers([], (), self.OK), "agent-"),
            "empty brief": (["new", "demo-task", "--brief", "  ", "--dispatcher", "d1"], self.answers([], (), self.OK), "empty"),
            "worktree exists": (self.ARGS, self.answers([task_entry("demo-task")], (), self.OK), "worktree already exists"),
            "branch exists": (self.ARGS, self.answers([], (), self.OK, {("git", "rev-parse"): (0, "sha\n", "")}), "branch already exists"),
            "session exists": (self.ARGS, self.answers([], [{"name": "demo-task", "cwd": "/elsewhere"}], self.OK), "session named"),
            "session inside the worktree": (self.ARGS, self.answers([], [{"cwd": f"{WT}/demo-task/sub"}], self.OK), "session named"),
            "limit": (self.ARGS, self.answers([task_entry(s) for s in "abcde"], (), self.OK), "at the cap of 5.*a, b, c, d, e"),
            "no repo": (self.ARGS, {("git",): (128, "", "not a repo")}, "not inside a git repository"),
            "agents unlistable": (self.ARGS, self.answers([], (), self.OK, {("claude", "agents"): (1, "", "boom")}), "cannot list sessions"),
            "wrong dispatcher name": (self.ARGS, self.answers([], (), self.OK, dispatcher="other"), "/rename d1"),
            "unnamed dispatcher": (self.ARGS, repo_answers([], [{"sessionId": "sid-d"}], self.OK), "/rename d1"),
            "caller not listed": (self.ARGS, repo_answers([], [], self.OK), "not listed"),
            "second session with the dispatcher name": (
                self.ARGS, self.answers([], [{"sessionId": "sid-2", "name": "d1"}], self.OK), "also named 'd1'"),
            "old merged pull request": (
                self.ARGS, self.answers([], (), self.OK, {("gh",): (0, '[{"state": "MERGED"}]', "")}),
                "merged pull request already exists"),
            "old open pull request": (
                self.ARGS, self.answers([], (), self.OK, {("gh",): (0, '[{"state": "OPEN"}]', "")}),
                "open pull request already exists"),
        }
        for name, (argv, answers, pattern) in cases.items():
            with self.subTest(name):
                code, _, err, fake = invoke(argv, answers)
                self.assertEqual(code, 2, err)
                self.assertRegex(err, pattern)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertFalse(fake.ran("claude", "--bg"))

    def test_unanswerable_pull_request_check_warns_and_proceeds(self):
        for gh in ((127, "", "no gh"), (0, "not json", ""), (0, "[{}]", "")):
            with self.subTest(gh=gh):
                code, _, err, fake = invoke(self.ARGS, self.answers([], (), self.OK, {("gh",): gh}))
                self.assertEqual(code, 0, err)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertRegex(err, "warning: cannot check for an existing pull request on worktree-demo-task")
                self.assertTrue(fake.ran("claude", "--bg"))
        code, _, err, fake = invoke(self.ARGS, self.answers([], (), self.OK, {("gh",): (0, "[]", "")}))
        self.assertEqual((code, err), (0, ""))
        gh = next(c for c in fake.calls if c[0] == "gh")
        self.assertEqual(gh[:6], ["gh", "pr", "list", "--head", "worktree-demo-task", "--state"])
        self.assertEqual(gh[6], "all")

    def test_dispatcher_session_id_unset_is_refused(self):
        for value in ("", "sid-other"):
            with self.subTest(value):
                code, _, err, fake = invoke(self.ARGS, self.answers([], (), self.OK),
                                            env={"CLAUDE_CODE_SESSION_ID": value})
                self.assertEqual(code, 2)
                self.assertRegex(err, "CLAUDE_CODE_SESSION_ID|not listed")
                self.assertFalse(fake.ran("claude", "--bg"))

    def stacked(self, extra, base_entry=None):
        answers = self.answers([base_entry or task_entry("alpha")], (), {
            ("git", "worktree", "add"): (0, "", ""), ("git", "config"): (0, "", ""),
            ("git", "worktree", "remove"): (0, "", ""), ("git", "branch"): (0, "", "")}, extra)
        return invoke(self.ARGS + ["--base", "alpha"], answers)

    def test_base_creates_worktree_records_base_and_launches(self):
        code, _, err, fake = self.stacked(self.OK)
        self.assertEqual(code, 0, err)
        add = ["git", "worktree", "add", "-b", "worktree-demo-task", f"{WT}/demo-task", "worktree-alpha"]
        cfg = ["git", "config", "branch.worktree-demo-task.factoryBase", "worktree-alpha"]
        self.assertLess(fake.calls.index(add), fake.calls.index(cfg))
        self.assertLess(fake.calls.index(cfg), len(fake.calls) - 1)
        launch = fake.calls[-1]
        self.assertEqual(launch[:6], ["claude", "--bg", "--worktree", "demo-task", "-n", "demo-task"])
        self.assertIn("Your base branch is worktree-alpha (remote origin).", launch[-1])
        self.assertFalse(fake.ran("git", "worktree", "remove"))

    def test_base_uses_the_base_tasks_actual_branch(self):
        code, _, err, fake = self.stacked(self.OK, (f"{WT}/alpha", "feature/odd", False))
        self.assertEqual(code, 0, err)
        self.assertIn(["git", "worktree", "add", "-b", "worktree-demo-task", f"{WT}/demo-task", "feature/odd"],
                      fake.calls)
        self.assertIn(["git", "config", "branch.worktree-demo-task.factoryBase", "feature/odd"], fake.calls)
        self.assertIn("Your base branch is feature/odd (remote origin).", fake.calls[-1][-1])

    def test_detached_base_refused(self):
        code, _, err, fake = self.stacked(self.OK, (f"{WT}/alpha", None, False))
        self.assertEqual(code, 2)
        self.assertIn("detached", err)
        self.assertFalse(fake.ran("git", "worktree", "add"))

    def test_unknown_base_refused_before_anything_is_created(self):
        code, _, err, fake = invoke(self.ARGS + ["--base", "ghost"], self.answers([task_entry("alpha")]))
        self.assertEqual(code, 2)
        self.assertIn("ghost", err)
        self.assertFalse(fake.ran("git", "worktree", "add"))
        self.assertFalse(fake.ran("claude", "--bg"))

    def test_launch_exit_nonzero_with_base_removes_worktree_and_branch(self):
        code, _, err, fake = self.stacked({("claude", "--bg"): (1, "", "boom")})
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        self.assertIn(["git", "worktree", "remove", "--force", f"{WT}/demo-task"], fake.calls)
        self.assertIn(["git", "branch", "-D", "worktree-demo-task"], fake.calls)

    def test_launch_exit_nonzero_with_backgrounded_line_removes_nothing(self):
        code, _, err, fake = self.stacked({("claude", "--bg"): (1, "backgrounded · 96dc4869 · demo-task\n", "late")})
        self.assertEqual(code, 1)
        self.assertIn("exited 1 after printing a `backgrounded` line", err)
        self.assertFalse(fake.ran("git", "worktree", "remove"))
        self.assertFalse(fake.ran("git", "branch"))

    def test_launch_exit_nonzero_with_backgrounded_line_on_stderr_removes_nothing(self):
        code, _, err, fake = self.stacked({("claude", "--bg"): (1, "", "backgrounded · 96dc4869 · demo-task\n")})
        self.assertEqual(code, 1)
        self.assertIn("exited 1 after printing a `backgrounded` line", err)
        self.assertFalse(fake.ran("git", "worktree", "remove"))
        self.assertFalse(fake.ran("git", "branch"))

    def test_launch_exit_zero_without_line_removes_nothing(self):
        code, _, err, fake = self.stacked({("claude", "--bg"): (0, "something odd\n", "")})
        self.assertEqual(code, 1)
        self.assertIn("something odd", err)
        self.assertFalse(fake.ran("git", "worktree", "remove"))
        self.assertFalse(fake.ran("git", "branch"))

    def test_config_failure_removes_worktree_and_branch_and_exits_1(self):
        code, _, err, fake = self.stacked({("git", "config"): (1, "", "cannot lock")})
        self.assertEqual(code, 1)
        self.assertIn("cannot lock", err)
        self.assertIn(["git", "worktree", "remove", "--force", f"{WT}/demo-task"], fake.calls)
        self.assertIn(["git", "branch", "-D", "worktree-demo-task"], fake.calls)
        self.assertFalse(fake.ran("claude", "--bg"))

    def test_worktree_add_failure_exits_1(self):
        code, _, err, fake = self.stacked({("git", "worktree", "add"): (128, "", "bad ref")})
        self.assertEqual((code, "bad ref" in err), (1, True))
        self.assertFalse(fake.ran("claude", "--bg"))

    def test_launch_failure_without_base_creates_nothing_to_undo(self):
        code, _, _, fake = invoke(self.ARGS, self.answers([], (), {("claude", "--bg"): (1, "", "boom")}))
        self.assertEqual(code, 1)
        self.assertFalse(fake.ran("git", "worktree", "remove"))

    def test_new_declares_working(self):
        code, _, err, fake = invoke(self.ARGS, self.answers([], (), self.OK))
        self.assertEqual(code, 0, err)
        (rec,) = fake.tasks
        self.assertEqual((rec["repo"], rec["slug"], rec["state"], rec["role"]), (ROOT, "demo-task", "working", "dispatcher"))

    def test_a_failed_launch_declares_nothing(self):
        _, _, _, fake = invoke(self.ARGS, self.answers([], (), {("claude", "--bg"): (1, "", "boom")}))
        self.assertEqual(fake.tasks, [])

    def test_a_reused_slug_starts_clean(self):
        code, _, _, fake = invoke(self.ARGS, self.answers([], (), self.OK), tasks=[task_rec("demo-task", "acknowledged")])
        self.assertEqual((code, fake.tasks[0]["state"]), (0, "working"))

    def test_cap_from_settings(self):
        code, _, err, _ = invoke(self.ARGS, self.answers([task_entry("a")], (), self.OK), '[limits]\nworktree_cap = 1\n')
        self.assertEqual(code, 2)
        self.assertIn("cap of 1", err)

    def test_the_reserve_is_kept_for_critical_tasks(self):
        settings = '[limits]\nworktree_cap = 10\n'
        busy = lambda n: self.answers([task_entry(s) for s in "abcdefghi"[:n]], (), self.OK)
        code, _, err, fake = invoke(self.ARGS, busy(8), settings)
        self.assertEqual(code, 0, err)
        self.assertTrue(fake.ran("claude", "--bg"))
        code, _, err, fake = invoke(self.ARGS, busy(9), settings)
        self.assertEqual(code, 2)
        self.assertRegex(err, "^factory: at the limit of 9 ordinary tasks in motion \\(1 of the cap of 10 held in "
                              "reserve for critical work; start one with --critical\\): a, b, c, d, e, f, g, h, i$")
        self.assertFalse(fake.ran("claude", "--bg"))
        code, _, err, fake = invoke(self.ARGS + ["--critical"], busy(9), settings)
        self.assertEqual(code, 0, err)
        self.assertTrue(fake.ran("claude", "--bg"))
        answers = self.answers([task_entry(s) for s in "abcdefghij"], (), self.OK)
        code, _, err, fake = invoke(self.ARGS, answers, settings)
        self.assertEqual(code, 2)
        self.assertRegex(err, "^factory: at the cap of 10 tasks in motion: a, b, c, d, e, f, g, h, i, j$")
        self.assertNotIn("--critical", err)
        self.assertFalse(fake.ran("claude", "--bg"))
        code, _, err, fake = invoke(self.ARGS + ["--critical"], answers, settings)
        self.assertEqual(code, 2)
        self.assertRegex(err, "at the cap of 10 tasks in motion: a, b, c, d, e, f, g, h, i, j$")
        self.assertFalse(fake.ran("claude", "--bg"))

    def test_without_a_reserve_the_cap_is_the_limit(self):
        settings = '[limits]\nworktree_cap = 10\nreserve_percent = 0\n'
        code, _, err, _ = invoke(self.ARGS, self.answers([task_entry(s) for s in "abcdefghi"], (), self.OK), settings)
        self.assertEqual(code, 0, err)
        code, _, err, _ = invoke(self.ARGS, self.answers([task_entry(s) for s in "abcdefghij"], (), self.OK), settings)
        self.assertEqual(code, 2)
        self.assertRegex(err, "at the cap of 10 tasks in motion")

    def at_cap(self, pr, sessions=(), tasks=()):
        """`new` with a cap of 2 and tasks a and b; b has no pull request, a's pull requests are `pr`."""
        answers = self.answers([task_entry("a"), task_entry("b")], sessions, self.OK, {
            ("gh",): (0, "[]", ""), ("gh", "pr", "list", "--head", "worktree-a"): (0, json.dumps(pr), "")})
        code, _, err, fake = invoke(self.ARGS, answers, '[limits]\nworktree_cap = 2\nreserve_percent = 0\n', tasks=tasks)
        return code, err, fake

    def test_cap_counts_only_tasks_in_motion(self):
        done = {"cwd": f"{WT}/a", "kind": "background", "id": "s1", "status": "idle", "state": "done"}
        frees = {
            "open, no session": ([{"state": "OPEN"}], ()),
            "open, finished session": ([{"state": "OPEN"}], [done]),
            "open, session idle with another state word": ([{"state": "OPEN"}], [dict(done, state="working")]),
            "merged, not cleaned": ([{"state": "MERGED"}], ()),
        }
        for name, (pr, sessions) in frees.items():
            with self.subTest(name):
                code, err, fake = self.at_cap(pr, sessions)
                self.assertEqual(code, 0, err)
                self.assertTrue(fake.ran("claude", "--bg"))
        holds = {
            "no pull request": ([], ()),
            "closed": ([{"state": "CLOSED"}], ()),
            "state unknown": ("not a list", ()),
            "open, session working": ([{"state": "OPEN"}], [dict(done, status="busy", state="working")]),
            "open, session waiting": ([{"state": "OPEN"}], [dict(done, status="waiting")]),
            "open, interactive session": ([{"state": "OPEN"}], [dict(done, kind="interactive")]),
            "merged, session working": ([{"state": "MERGED"}], [dict(done, status="busy", state="working")]),
            "open, an unfinished session without cwd": ([{"state": "OPEN"}], [{"kind": "background", "status": "busy"}]),
        }
        for name, (pr, sessions) in holds.items():
            with self.subTest(name):
                code, err, fake = self.at_cap(pr, sessions)
                self.assertEqual(code, 2, err)
                self.assertRegex(err, "cap of 2 tasks in motion: a, b$")
                self.assertFalse(fake.ran("claude", "--bg"))

    def test_cap_counts_declared_states(self):
        busy = [{"cwd": f"{WT}/a", "kind": "background", "id": "s1", "status": "busy", "state": "working"}]
        link = "https://x/y"
        code, err, fake = self.at_cap([{"state": "OPEN"}], tasks=[task_rec("a", "blocked")])
        self.assertEqual(code, 2)
        self.assertRegex(err, "cap of 2 tasks in motion: a \\(blocked\\), b$")
        self.assertFalse(fake.ran("claude", "--bg"))
        code, err, _ = self.at_cap([{"state": "OPEN"}], tasks=[task_rec("a", "working")])
        self.assertRegex(err, "in motion: a, b$")
        for name, pr in (("no pull request", []), ("closed", [{"state": "CLOSED"}])):
            with self.subTest(name):
                code, err, fake = self.at_cap(pr, tasks=[task_rec("a", "handed-over", link)])
                self.assertEqual(code, 0, err)
                self.assertTrue(fake.ran("claude", "--bg"))
        code, err, _ = self.at_cap([], busy, tasks=[task_rec("a", "handed-over", link)])
        self.assertEqual(code, 2)
        self.assertRegex(err, "in motion: a, b$")
        code, err, _ = self.at_cap([{"state": "OPEN"}], tasks=[task_rec("a", "acknowledged", link)])
        self.assertEqual(code, 0, err)

    def test_cap_lists_only_the_tasks_in_motion(self):
        answers = self.answers([task_entry(s) for s in "abc"], (), self.OK, {
            ("gh",): (0, "[]", ""), ("gh", "pr", "list", "--head", "worktree-a"): (0, '[{"state": "OPEN"}]', "")})
        code, _, err, _ = invoke(self.ARGS, answers, '[limits]\nworktree_cap = 2\nreserve_percent = 0\n')
        self.assertEqual(code, 2)
        self.assertRegex(err, "cap of 2 tasks in motion: b, c$")

    def test_detached_task_counts_as_in_motion(self):
        answers = self.answers([(f"{WT}/a", None, False)], (), self.OK, {("gh",): (0, '[{"state": "OPEN"}]', "")})
        code, _, err, _ = invoke(self.ARGS, answers, '[limits]\nworktree_cap = 1\n')
        self.assertEqual(code, 2)
        self.assertRegex(err, "cap of 1 tasks in motion: a$")

    def test_below_the_cap_asks_gh_only_about_the_new_branch(self):
        code, _, err, fake = invoke(self.ARGS, self.answers([task_entry("a")], (), self.OK, {("gh",): (0, "[]", "")}))
        self.assertEqual(code, 0, err)
        self.assertEqual([c[4] for c in fake.calls if c[0] == "gh"], ["worktree-demo-task"])

    def test_invalid_settings_exit_2(self):
        code, _, err, _ = invoke(self.ARGS, self.answers([]), '[limits]\nworktree_cap = 0\n')
        self.assertEqual((code, "settings.toml line 2: limits.worktree_cap must be" in err), (2, True))
        code, _, err, _ = invoke(self.ARGS, self.answers([]), "not toml")
        self.assertEqual((code, "settings.toml" in err), (2, True))

    def test_claude_failure_passes_stderr(self):
        for result in ((1, "", "model not found\n"), (0, "no marker line\n", "")):
            with self.subTest(result=result):
                code, _, err, _ = invoke(self.ARGS, self.answers([], (), {("claude", "--bg"): result}))
                self.assertEqual(code, 1)
                self.assertRegex(err, "did not start|without a `backgrounded` line")
        _, _, err, _ = invoke(self.ARGS, self.answers([], (), {("claude", "--bg"): (1, "", "model not found\n")}))
        self.assertIn("model not found", err)

    def test_no_remote_defaults_to_main(self):
        answers = self.answers([], (), self.OK)
        answers[("git", "symbolic-ref")] = (128, "", "no such ref")
        self.assertEqual(invoke(self.ARGS, answers)[0], 0)


def git_task_answers(slug, now, *, dirty="", counts="0\t2", commit_age=0, pr=None):
    return {
        ("git", "status"): (0, dirty, ""),
        ("git", "rev-list"): (0, counts + "\n", ""),
        ("git", "log"): (0, f"{int(now - commit_age)} sha-{slug}\n", ""),
        ("gh",): (0, json.dumps(pr if pr is not None else []), "") if pr != "missing" else (127, "", "no gh"),
    }


class StatusTests(unittest.TestCase):
    def status(self, entries, sessions=(), as_json=True, **kw):
        now = time.time()
        answers = repo_answers(entries, sessions, git_task_answers("x", now, **kw))
        code, out, err, fake = invoke(["status", "--json"] if as_json else ["status"], answers)
        self.assertEqual(code, 0, err)
        return (json.loads(out) if as_json else out), fake

    def test_rows_counts_and_exclusions(self):
        entries = [task_entry("alpha"), (f"{WT}/agent-1a2b", "worktree-agent-1a2b", False),
                   ("/elsewhere/wt", "feature", False), (f"{WT}/gone", "worktree-gone", True)]
        session = {"id": "96dc4869", "cwd": f"{WT}/alpha", "kind": "background", "name": "alpha",
                   "status": "idle", "state": "done", "startedAt": int(time.time() * 1000)}
        other = {"cwd": ROOT, "kind": "interactive", "name": "d1", "status": "busy"}
        doc, _ = self.status(entries, [other, session], counts="3\t1", dirty=" M f\n",
                             pr=[{"state": "OPEN", "url": "http://pr/1", "headRefOid": "z"}])
        self.assertEqual((doc["cap"], doc["open"], doc["leftover_agent_worktrees"], doc["outside_worktrees"]),
                         (5, 1, 1, 1))
        (row,) = doc["rows"]
        self.assertEqual((row["slug"], row["branch"], row["session"], row["session_id"]),
                         ("alpha", "worktree-alpha", "done", "96dc4869"))
        self.assertEqual((row["dirty"], row["behind"], row["ahead"]), (True, 3, 1))
        self.assertEqual((row["pr_state"], row["pr_url"]), ("open", "http://pr/1"))
        self.assertFalse(row["stale"])
        self.assertEqual((row["phase"], doc["in_motion"], doc["in_review"], doc["merged"]), ("review", 0, 1, 0))

    def test_phase_and_counts(self):
        entries = [task_entry(s) for s in ("moving", "working", "review", "merged")]
        busy = {"id": "s1", "cwd": f"{WT}/working", "kind": "background", "status": "busy", "state": "working"}
        prs = {("gh", "pr", "list", "--head", f"worktree-{slug}"): (0, json.dumps([{"state": state}]), "")
               for slug, state in (("working", "OPEN"), ("review", "OPEN"), ("merged", "MERGED"))}
        answers = repo_answers(entries, [busy], git_task_answers("x", time.time()), prs)
        code, out, err, _ = invoke(["status", "--json"], answers)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual([r["phase"] for r in doc["rows"]], ["motion", "motion", "review", "merged"])
        self.assertEqual((doc["open"], doc["in_motion"], doc["in_review"], doc["merged"]), (4, 2, 1, 1))
        _, out, _, _ = invoke(["status"], answers)
        lines = out.splitlines()
        self.assertEqual(lines[3].index("review", 6), lines[0].index("PHASE"))
        self.assertTrue(next(l for l in lines if l.startswith("in motion")).startswith(
            "in motion 2/4, reserve 1, cap 5 (0 blocked); waiting for review 1; merged, not cleaned 1; settled, not cleaned 0; "))
        self.assertEqual(lines[-1], "waiting on the human: review -")

    def test_declared_state_deliverable_phases_and_five_counts(self):
        slugs = ("moving", "asking", "handed", "merged", "settled")
        tasks = [task_rec("asking", "blocked"), task_rec("handed", "handed-over", "/out/video.mp4"),
                 task_rec("merged", "handed-over", "https://x/y"), task_rec("settled", "dismissed", "https://x/z")]
        prs = {("gh", "pr", "list", "--head", "worktree-merged"): (0, json.dumps([{"state": "MERGED"}]), "")}
        answers = repo_answers([task_entry(s) for s in slugs], (), git_task_answers("x", time.time()), prs)
        code, out, err, _ = invoke(["status", "--json"], answers, tasks=tasks)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual([(r["phase"], r["state"], r["deliverable"]) for r in doc["rows"]],
                         [("motion", None, None), ("blocked", "blocked", None),
                          ("review", "handed-over", "/out/video.mp4"), ("merged", "handed-over", "https://x/y"),
                          ("settled", "dismissed", "https://x/z")])
        self.assertEqual((doc["in_motion"], doc["blocked"], doc["in_review"], doc["merged"], doc["settled"]),
                         (2, 1, 1, 1, 1))

    def test_table_shows_state_count_line_and_what_waits_on_the_human(self):
        slugs = ("moving", "asking", "handed", "pr-only")
        tasks = [task_rec("asking", "blocked"), task_rec("handed", "handed-over", "/out/video.mp4"),
                 task_rec("pr-only", "handed-over")]
        prs = {("gh", "pr", "list", "--head", "worktree-pr-only"): (0, json.dumps([{"state": "OPEN", "url": "http://pr/7"}]), "")}
        answers = repo_answers([task_entry(s) for s in slugs], (), git_task_answers("x", time.time()), prs)
        _, out, _, _ = invoke(["status"], answers, tasks=tasks)
        lines = out.splitlines()
        self.assertEqual(lines[0].split()[:3], ["SLUG", "PHASE", "STATE"])
        for line, state in zip(lines[1:5], ("-", "blocked", "handed-over", "handed-over")):
            self.assertTrue(line[lines[0].index("STATE"):].startswith(state), line)
        self.assertEqual(lines[5:], [
            "in motion 2/4, reserve 1, cap 5 (1 blocked); waiting for review 2; merged, not cleaned 0; settled, not cleaned 0; "
            "leftover agent worktrees 0; worktrees outside .claude/worktrees 0", "acting on GitHub with your gh login (no App identity: factory wizard)",
            "waiting on the human: handed /out/video.mp4", "waiting on the human: pr-only http://pr/7"])

    def test_a_draft_pull_request_in_review_waits_on_checks_and_is_flagged(self):
        self.assertIn("isDraft", factory.STATUS_PR_FIELDS.split(","))  # the fake gh answers whatever is asked
        for draft, line in ((True, "waiting on checks: handed http://pr/7"), (False, "waiting on the human: handed http://pr/7")):
            with self.subTest(draft=draft):
                pr = [{"state": "OPEN", "url": "http://pr/7", "headRefOid": "sha-x", "isDraft": draft}]
                answers = repo_answers([task_entry("handed")], (), git_task_answers("x", time.time(), pr=pr))
                tasks = [task_rec("handed", "handed-over")]
                _, out, _, _ = invoke(["status"], answers, tasks=tasks)
                self.assertEqual(out.splitlines()[-1], line)
                _, out, _, _ = invoke(["status", "--json"], answers, tasks=tasks)
                self.assertEqual(json.loads(out)["rows"][0]["pr_draft"], draft)

    def test_no_pull_request_is_not_a_draft(self):
        doc, _ = self.status([task_entry("alpha")])
        self.assertIs(doc["rows"][0]["pr_draft"], False)

    def test_a_review_task_without_deliverable_or_pull_request_waits_with_a_dash(self):
        answers = repo_answers([task_entry("handed")], (), git_task_answers("x", time.time()))
        _, out, _, _ = invoke(["status"], answers, tasks=[task_rec("handed", "handed-over")])
        self.assertEqual(out.splitlines()[-1], "waiting on the human: handed -")

    def test_waited_counts_from_the_declared_record_and_is_null_without_one(self):
        def at(ago):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))
        tasks = [{**task_rec("handed", "handed-over"), "ts": at(600)}, {**task_rec("asking", "blocked"), "ts": at(90)},
                 {**task_rec("odd", "handed-over"), "ts": "yesterday"}, {**task_rec("future", "blocked"), "ts": at(-500)}]
        answers = repo_answers([task_entry(s) for s in ("moving", "handed", "asking", "odd", "future")], (),
                               git_task_answers("x", time.time()))
        _, out, _, _ = invoke(["status", "--json"], answers, tasks=tasks)
        waited = {r["slug"]: r["waited"] for r in json.loads(out)["rows"]}
        self.assertIsNone(waited["moving"])
        self.assertIsNone(waited["odd"])
        self.assertAlmostEqual(waited["handed"], 600, delta=5)
        self.assertAlmostEqual(waited["asking"], 90, delta=5)
        self.assertEqual(waited["future"], 0)

    def test_a_blocked_task_carries_the_last_stop_its_lead_sent_after_declaring(self):
        decl = "2026-03-02T10:00:00Z"
        tasks = [{**task_rec(s, "blocked"), "ts": decl} for s in ("asking", "silent", "stale", "other", "bare")]
        tasks.append({**task_rec("handed", "handed-over"), "ts": decl})

        def stop(slug, ts, kind="QUESTION", **kw):
            return {"ts": ts, "event": "SendMessage", "repo": ROOT, "slug": slug, "kind": kind, **kw}
        events = [stop("asking", "2026-03-02T10:00:00Z", summary="old reason"),
                  stop("asking", "2026-03-02T10:05:00Z", "BLOCKED", summary="which database?"),
                  stop("stale", "2026-03-02T09:59:59Z", summary="before the declaration"),
                  stop("stale", "2026-02-27T12:00:00Z", summary="a month file before the declaration"),
                  stop("other", "2026-03-02T10:05:00Z", summary="wrong repo") | {"repo": "/elsewhere"},
                  stop("handed", "2026-03-02T10:05:00Z", summary="not blocked"),
                  stop("bare", "2026-03-02T10:06:00Z", "OVERTIME", summary={"not": "text"}),
                  stop(["asking"], "2026-03-02T10:07:00Z", summary="slug is not a string"),
                  stop("asking", 20260302, summary="ts is not a string"),
                  {k: v for k, v in stop("asking", "", summary="no ts").items() if k != "ts"},
                  stop("silent", "2026-03-02T10:06:00Z", "DONE", summary="not a stop")]
        answers = repo_answers([task_entry(s) for s in ("moving", "asking", "silent", "stale", "other", "bare", "handed")],
                               (), git_task_answers("x", time.time()))
        _, out, _, _ = invoke(["status", "--json"], answers, tasks=tasks, events=events)
        got = {r["slug"]: (r["stop_kind"], r["stop_summary"]) for r in json.loads(out)["rows"]}
        self.assertEqual(got, {"asking": ("BLOCKED", "which database?"), "bare": ("OVERTIME", None),
                               "silent": (None, None), "stale": (None, None), "other": (None, None),
                               "handed": (None, None), "moving": (None, None)})

    def test_no_events_log_gives_null_stop_fields(self):
        answers = repo_answers([task_entry("asking")], (), git_task_answers("x", time.time()))
        _, out, _, _ = invoke(["status", "--json"], answers, tasks=[task_rec("asking", "blocked")])
        (row,) = json.loads(out)["rows"]
        self.assertEqual((row["phase"], row["stop_kind"], row["stop_summary"]), ("blocked", None, None))

    def test_sessions_unlistable_counts_as_in_motion(self):
        doc, _ = self.status([task_entry("alpha")], pr=[{"state": "OPEN"}])
        self.assertEqual(doc["rows"][0]["phase"], "review")
        answers = repo_answers([task_entry("alpha")], (), git_task_answers("x", time.time(), pr=[{"state": "OPEN"}]),
                               {("claude", "agents"): (1, "", "boom")})
        _, out, _, _ = invoke(["status", "--json"], answers)
        self.assertEqual((json.loads(out)["rows"][0]["phase"], json.loads(out)["in_motion"]), ("motion", 1))
        doc, _ = self.status([task_entry("alpha")], [{"kind": "background", "status": "busy"}], pr=[{"state": "OPEN"}])
        self.assertEqual(doc["rows"][0]["phase"], "motion")

    def test_session_matching_is_by_worktree_path_prefix(self):
        inside = {"id": "in1", "cwd": f"{WT}/alpha/sub/dir", "kind": "background", "status": "idle", "state": "done"}
        sibling = {"id": "sib", "cwd": f"{WT}/alpha-two", "kind": "background", "status": "idle", "state": "done"}
        no_cwd = {"id": "nocwd", "kind": "background", "status": "idle", "state": "done"}
        empty_cwd = {"id": "empty", "cwd": "", "kind": "background", "status": "idle", "state": "done"}
        doc, _ = self.status([task_entry("alpha")], [sibling, no_cwd, empty_cwd])
        self.assertEqual((doc["rows"][0]["session"], doc["rows"][0]["session_id"]), ("none", None))
        doc, _ = self.status([task_entry("alpha")], [sibling, no_cwd, inside])
        self.assertEqual(doc["rows"][0]["session_id"], "in1")

    def test_table_shows_the_session_id(self):
        s = {"id": "96dc4869", "cwd": f"{WT}/alpha", "kind": "background", "status": "idle", "state": "done"}
        out, _ = self.status([task_entry("alpha")], [s], as_json=False)
        lines = out.splitlines()
        self.assertEqual(lines[1].index("96dc4869"), lines[0].index("ID"))

    def test_no_session_and_no_pr(self):
        doc, fake = self.status([task_entry("alpha")])
        row = doc["rows"][0]
        self.assertEqual((row["session"], row["pr_state"], row["dirty"]), ("none", "none", False))
        self.assertIn(["git", "rev-list", "--left-right", "--count", "origin/main...HEAD"], fake.calls)

    def test_exited_lead_is_matched_by_name_at_the_main_checkout(self):
        exited = EXITED | {"name": "alpha"}
        pr = [{"state": "OPEN", "url": "u", "headRefOid": "z"}]
        row = self.status([task_entry("alpha")], [exited], pr=pr)[0]["rows"][0]
        self.assertEqual((row["session"], row["session_id"], row["phase"], row["overtime"]),
                         ("done", "ex1", "review", False))
        for name, other in {"another name": {"name": "beta"}, "another repo": {"cwd": "/other"},
                            "process alive": {"pid": 7}, "interactive": {"kind": "interactive"},
                            "no cwd": {"cwd": None}}.items():
            with self.subTest(name):
                row = self.status([task_entry("alpha")], [exited | other], pr=pr)[0]["rows"][0]
                self.assertEqual((row["session"], row["session_id"]), ("none", None))

    def test_worktree_locks_are_parsed(self):
        entries = factory.parse_worktrees(porcelain((ROOT, "main", False), (f"{WT}/a", "worktree-a", False, ""),
                                                    (f"{WT}/b", "worktree-b", False, "claude session b (pid 1)")))
        self.assertEqual([e["locked"] for e in entries], [None, "", "claude session b (pid 1)"])

    def test_missing_gh_is_unknown(self):
        doc, _ = self.status([task_entry("alpha")], pr="missing")
        self.assertEqual(doc["rows"][0]["pr_state"], "unknown")

    def test_sessions_unlistable(self):
        answers = repo_answers([task_entry("alpha")], (), {("claude", "agents"): (127, "", "no claude")},
                               git_task_answers("x", time.time()))
        _, out, _, _ = invoke(["status", "--json"], answers)
        self.assertEqual(json.loads(out)["rows"][0]["session"], "unknown")

    def test_no_remote_compares_against_main(self):
        answers = repo_answers([task_entry("alpha")], (), git_task_answers("x", time.time()))
        answers[("git", "symbolic-ref")] = (128, "", "")
        _, _, _, fake = invoke(["status"], answers)
        self.assertIn(["git", "rev-list", "--left-right", "--count", "main...HEAD"], fake.calls)

    def test_stale_rule(self):
        now_ms = int(time.time() * 1000)
        old_session = {"cwd": f"{WT}/alpha", "kind": "background", "status": "idle", "startedAt": now_ms - 8 * DAY * 1000}
        new_session = dict(old_session, startedAt=now_ms - DAY * 1000)
        cases = [
            ("old commit, no session", 8 * DAY, [], True),
            ("old commit, old session", 8 * DAY, [old_session], True),
            ("old commit, fresh session", 8 * DAY, [new_session], False),
            ("fresh commit, old session", DAY, [old_session], False),
            ("fresh commit, no session", DAY, [], False),
        ]
        for name, commit_age, sessions, stale in cases:
            with self.subTest(name):
                doc, _ = self.status([task_entry("alpha")], sessions, commit_age=commit_age)
                self.assertEqual(doc["rows"][0]["stale"], stale)

    def stacked(self, x_pr, *, x_worktree=True, x_branch=True):
        """The row of y, whose recorded base is worktree-x, and the calls; `x_pr` is x's pull requests."""
        entries = [task_entry("x"), task_entry("y")] if x_worktree else [task_entry("y")]
        answers = repo_answers(entries, (), git_task_answers("y", time.time()),
                               {("git", "config", "--get", "branch.worktree-y.factoryBase"): (0, "worktree-x\n", ""),
                                ("git", "rev-parse", "--verify", "--quiet", "refs/heads/worktree-x"):
                                    (0 if x_branch else 1, "", ""),
                                ("gh", "pr", "list", "--head", "worktree-x"):
                                    (0, json.dumps(x_pr), "") if x_pr != "missing" else (127, "", "no gh")})
        code, out, err, fake = invoke(["status", "--json"], answers)
        self.assertEqual(code, 0, err)
        return json.loads(out)["rows"][-1], fake

    def test_recorded_base_and_ahead_behind_against_it(self):
        merged_elsewhere = [{"state": "MERGED", "url": "u", "headRefOid": "h", "baseRefName": "worktree-w"}]
        cases = [("base task open", [{"state": "OPEN", "url": "u", "headRefOid": "h", "baseRefName": "main"}], True),
                 ("base task has no pull request", [], True),
                 ("base task's pull request unknown", "missing", True),
                 ("base task merged into another branch", merged_elsewhere, True),
                 ("base has no worktree, branch left, not merged", [], False)]
        for name, x_pr, x_worktree in cases:
            with self.subTest(name):
                row, fake = self.stacked(x_pr, x_worktree=x_worktree)
                self.assertEqual((row["base"], row["stacked_on"]), ("worktree-x", "worktree-x"))
                self.assertIn(["git", "rev-list", "--left-right", "--count", "worktree-x...HEAD"], fake.calls)

    def test_a_merged_or_missing_recorded_base_is_not_shown(self):
        merged = [{"state": "MERGED", "url": "u", "headRefOid": "h", "baseRefName": "main"}]
        cases = [("base task merged into the default branch", merged, True, True),
                 ("base has no worktree, branch left, merged", merged, False, True),
                 ("base branch is gone", [], False, False)]
        for name, x_pr, x_worktree, x_branch in cases:
            with self.subTest(name):
                row, fake = self.stacked(x_pr, x_worktree=x_worktree, x_branch=x_branch)
                self.assertEqual((row["base"], row["stacked_on"]), ("origin/main", "worktree-x"))
                self.assertIn(["git", "rev-list", "--left-right", "--count", "origin/main...HEAD"], fake.calls)
                self.assertFalse(fake.ran("git", "rev-list", "--left-right", "--count", "worktree-x...HEAD"))
        _, out, _, _ = invoke(["status"], repo_answers(
            [task_entry("x"), task_entry("y")], (), git_task_answers("y", time.time(), pr=merged),
            {("git", "config", "--get", "branch.worktree-y.factoryBase"): (0, "worktree-x\n", "")}))
        self.assertNotIn("worktree-x ", out.splitlines()[2].split("worktree-y", 1)[1])

    def test_default_base_when_none_recorded(self):
        doc, _ = self.status([task_entry("alpha")])
        self.assertEqual((doc["rows"][0]["base"], doc["rows"][0]["stacked_on"]), ("origin/main", None))

    def test_elapsed_and_overtime_on_both_sides_of_the_limit(self):
        now_ms = int(time.time() * 1000)
        H = 3600 * 1000

        def session(hours, **kw):
            return dict({"cwd": f"{WT}/alpha", "kind": "background", "status": "busy", "state": "working",
                         "startedAt": now_ms - int(hours * H)}, **kw)
        cases = [
            ("working past the limit", session(2.5), True),
            ("working within the limit", session(1.5), False),
            ("waiting past the limit", session(2.5, status="waiting"), True),
            ("turn ended, state working", session(2.5, status="idle"), False),
            ("turn ended, state done", session(2.5, status="idle", state="done"), False),
            ("stopped", {k: v for k, v in session(2.5, state="stopped").items() if k != "status"}, False),
        ]
        for name, s, overtime in cases:
            with self.subTest(name):
                doc, _ = self.status([task_entry("alpha")], [s])
                row = doc["rows"][0]
                self.assertEqual(row["overtime"], overtime)
                self.assertAlmostEqual(row["elapsed"], (now_ms - s["startedAt"]) / 1000, delta=5)
        doc, _ = self.status([task_entry("alpha")])
        self.assertEqual((doc["rows"][0]["elapsed"], doc["rows"][0]["overtime"]), (None, False))

    def test_overtime_follows_the_setting(self):
        s = {"cwd": f"{WT}/alpha", "kind": "background", "status": "busy", "state": "working",
             "startedAt": int(time.time() * 1000) - 90 * 60 * 1000}
        answers = repo_answers([task_entry("alpha")], [s], git_task_answers("x", time.time()))
        _, out, _, _ = invoke(["status", "--json"], answers, '[limits]\ntask_time_limit_hours = 1\n')
        self.assertTrue(json.loads(out)["rows"][0]["overtime"])

    def test_table_output(self):
        entries = [task_entry("alpha"), (f"{WT}/agent-1a2b", "b", False), ("/elsewhere", "c", False)]
        out, _ = self.status(entries, as_json=False)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("SLUG"))
        self.assertTrue(lines[1].startswith("alpha"))
        self.assertEqual(lines[1].index("worktree-alpha"), lines[0].index("BRANCH"))
        self.assertEqual(lines[1].index("origin/main"), lines[0].index("BASE"))
        self.assertEqual(lines[-2:], ["in motion 1/4, reserve 1, cap 5 (0 blocked); waiting for review 0; merged, not cleaned 0; "
                                      "settled, not cleaned 0; leftover agent worktrees 1; "
                                      "worktrees outside .claude/worktrees 1", "acting on GitHub with your gh login (no App identity: factory wizard)"])

    def test_the_reserve_is_in_the_json_and_the_count_line_only_when_it_is_held(self):
        answers = repo_answers([], (), git_task_answers("x", time.time()))
        for settings, reserve, line in (
                (None, 1, "in motion 0/4, reserve 1, cap 5 ("),
                ('[limits]\nworktree_cap = 21\n', 3, "in motion 0/18, reserve 3, cap 21 ("),
                ('[limits]\nreserve_percent = 0\n', 0, "in motion 0/5 (")):
            with self.subTest(settings):
                _, out, _, _ = invoke(["status", "--json"], answers, settings)
                doc = json.loads(out)
                self.assertEqual((doc["cap"], doc["reserve"]), (int(re.search(r"worktree_cap = (\d+)", settings or "").group(1)) if settings and "worktree_cap" in settings else 5, reserve))
                _, out, _, _ = invoke(["status"], answers, settings)
                self.assertTrue(out.startswith(line), out)

    def test_empty_table(self):
        out, _ = self.status([], as_json=False)
        self.assertEqual(out.strip().splitlines(), ["in motion 0/4, reserve 1, cap 5 (0 blocked); waiting for review 0; merged, not cleaned 0; "
                                                    "settled, not cleaned 0; leftover agent worktrees 0; "
                                                    "worktrees outside .claude/worktrees 0", "acting on GitHub with your gh login (no App identity: factory wizard)"])


class MalformedGhTests(unittest.TestCase):
    def test_bad_entries_are_unknown_not_a_traceback(self):
        for name, pr in (("empty object", [{}]), ("null state", [{"state": None}]), ("number state", [{"state": 3}]),
                         ("strings", ["x"]), ("null entry", [None])):
            with self.subTest(name):
                answers = repo_answers([task_entry("x")], (), git_task_answers("x", time.time(), pr=pr))
                code, out, err, _ = invoke(["status", "--json"], answers)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["rows"][0]["pr_state"], "unknown")


class CleanTests(unittest.TestCase):
    HEAD = "sha-x"

    def clean(self, *, dirty="", pr=None, sessions=(), dry=False, extra=None, entries=None, tasks=()):
        if pr is None:
            pr = [{"state": "MERGED", "url": "u", "headRefOid": self.HEAD, "baseRefName": "main"}]
        answers = repo_answers(entries or [task_entry("x")], list(sessions),
                               git_task_answers("x", time.time(), dirty=dirty, pr=pr))
        answers.update({("git", "worktree", "remove"): (0, "", ""), ("git", "branch"): (0, "", ""),
                        ("git", "branch", "-r"): (0, "  origin/worktree-x\n", ""), ("claude", "rm"): (0, "", "")})
        answers.update(extra or {})
        return invoke(["clean"] + (["--dry-run"] if dry else []), answers, tasks=tasks)

    @staticmethod
    def removed_anything(fake):
        return any(c[:3] in (["git", "branch", "-d"], ["git", "worktree", "remove"]) or c[:2] == ["claude", "rm"]
                   for c in fake.calls)

    def pr(self, state="MERGED", base="main", head=HEAD):
        return [{"state": state, "url": "u", "headRefOid": head, "baseRefName": base}]

    def test_keep_reasons(self):
        def bg(**kw):
            return dict({"id": "ab", "cwd": f"{WT}/x", "kind": "background", "status": "idle", "state": "done"}, **kw)
        cases = {
            "uncommitted": (dict(dirty=" M a\n"), "uncommitted changes"),
            "unreadable status": (dict(extra={("git", "status"): (128, "", "bad")}), "cannot read git status"),
            "detached head": (dict(entries=[(f"{WT}/x", None, False)]), "detached HEAD"),
            "fresh task, no pr": (dict(pr=[]), "no merged pull request \\(none\\)"),
            "open pr": (dict(pr=self.pr("OPEN")), "no merged pull request \\(open\\)"),
            "closed pr": (dict(pr=self.pr("CLOSED")), "no merged pull request \\(closed\\)"),
            "gh missing": (dict(pr="missing"), "pull request state is unknown"),
            "merged into another task's branch": (dict(pr=self.pr(base="worktree-y")), "merged into worktree-y, not main"),
            "merged, base missing": (dict(pr=self.pr(base="")), "unknown branch"),
            "newer commits": (dict(pr=self.pr(head="other")), "newer than its merged pull request"),
            "empty head": (dict(pr=self.pr(head=""), extra={("git", "log"): (0, "", "")}), "cannot compare its head"),
            "empty head on both sides": (dict(pr=self.pr(head=""), extra={("git", "log"): (1, "", "")}),
                                         "cannot compare its head"),
            "busy session": (dict(sessions=[bg(status="busy", state="working")]),
                             "not finished \\(status busy, state working\\)"),
            "waiting session": (dict(sessions=[bg(status="waiting")]), "not finished \\(status waiting, state done\\)"),
            "unknown status": (dict(sessions=[bg(status="needs_input")]), "status needs_input, state done"),
            "missing status, process alive": (dict(sessions=[EXITED | {"pid": 7, "cwd": f"{WT}/x"}]),
                                              "status missing, state done"),
            "exited, not done": (dict(sessions=[EXITED | {"state": "failed"}]), "status missing, state failed"),
            "exited, status present": (dict(sessions=[EXITED | {"status": "busy"}]), "status busy, state done"),
            "two exited sessions carry its name": (dict(sessions=[EXITED, EXITED | {"id": "ex2"}]),
                                                   "more than one exited session"),
            "locked, no session": (
                dict(entries=[task_entry("x") + ("claude session x (pid 7 start Sat)",)]),
                f"locked \\(claude session x \\(pid 7 start Sat\\)\\) and no session .*run: git worktree unlock {WT}/x$"),
            "locked without a reason, no session": (
                dict(entries=[task_entry("x") + ("",)]), f"locked and no session .*run: git worktree unlock {WT}/x$"),
            "locked, exited session of another name": (
                dict(entries=[task_entry("x") + ("",)], sessions=[EXITED | {"name": "y"}]), "locked and no session"),
            "exited, no state": (dict(sessions=[{k: v for k, v in EXITED.items() if k != "state"}]),
                                 "status missing, state missing"),
            "pid, no status, stopped": (dict(sessions=[EXITED | {"pid": 7, "cwd": f"{WT}/x", "state": "stopped"}]),
                                        "status missing, state stopped"),
            "one finished, one not": (dict(sessions=[bg(pid=1), bg(id="cd", pid=2, status="busy")]),
                                      "session cd is not finished"),
            "worktree nested inside": (
                dict(entries=[task_entry("x"), (f"{WT}/x/.claude/worktrees/agent-1", "worktree-agent-1", False)]),
                "registered inside it: .*/x/.claude/worktrees/agent-1"),
            "session turns busy before removal": (
                dict(extra={("claude", "agents"): [(0, "[]", ""), (0, json.dumps([bg(status="busy", state="working")]), "")]}),
                "not finished \\(status busy"),
            "exited lead is rechecked before removal": (
                dict(extra={("claude", "agents"): [(0, json.dumps([EXITED]), ""),
                                                   (0, json.dumps([EXITED | {"state": "failed"}]), "")]}),
                "status missing, state failed"),
            "interactive session": (dict(sessions=[{"sessionId": "i1", "cwd": f"{WT}/x", "kind": "interactive"}]),
                                    "interactive session"),
            "interactive session in a subdirectory": (
                dict(sessions=[{"sessionId": "i1", "cwd": f"{WT}/x/src/deep", "kind": "interactive"}]),
                "interactive session"),
        }
        for name, (kw, pattern) in cases.items():
            with self.subTest(name):
                code, out, _, fake = self.clean(**kw)
                self.assertEqual(code, 0)
                self.assertRegex(out, f"^kept x: .*{pattern}")
                self.assertFalse(self.removed_anything(fake))

    def settled(self, state="acknowledged", deliverable="https://x/y", **kw):
        kw.setdefault("pr", [])
        return self.clean(tasks=[task_rec("x", state, deliverable)], **kw)

    def test_settled_task_is_removed_when_every_check_holds(self):
        for state in ("acknowledged", "dismissed"):
            for deliverable in ("https://x/y", "/out/video.mp4"):
                with self.subTest(state=state, deliverable=deliverable):
                    code, out, _, fake = self.settled(state, deliverable)
                    self.assertEqual((code, out.strip()), (0, "removed x"))
                    self.assertIn(["git", "branch", "-r", "--contains", "HEAD"], fake.calls)
        code, out, _, _ = self.settled(pr=self.pr("CLOSED"))
        self.assertEqual(out.strip(), "removed x")

    def test_settled_task_is_kept_when_a_check_fails(self):
        bg = {"id": "ab", "cwd": f"{WT}/x", "kind": "background", "status": "busy", "state": "working"}
        cases = {
            "merely handed over": (dict(state="handed-over"), "no merged pull request \\(none\\)"),
            "working": (dict(state="working"), "no merged pull request \\(none\\)"),
            "pull request open": (dict(pr=self.pr("OPEN")), "settled, but its pull request is open"),
            "pull request unknown": (dict(extra={("gh",): (1, "", "no gh")}), "settled, but its pull request is unknown"),
            "no deliverable": (dict(deliverable=None), "no deliverable is recorded"),
            "deliverable is the worktree": (dict(deliverable=f"{WT}/x"), "deliverable is inside the worktree"),
            "deliverable inside the worktree": (dict(deliverable=f"{WT}/x/out/video.mp4"),
                                                "deliverable is inside the worktree"),
            "head on no remote": (dict(extra={("git", "branch", "-r"): (0, "\n", "")}), "on no remote-tracking branch"),
            "git branch -r fails": (dict(extra={("git", "branch", "-r"): (128, "", "bad")}), "cannot tell whether"),
            "dirty tree": (dict(dirty=" M a\n"), "uncommitted changes"),
            "unfinished session": (dict(sessions=[bg]), "not finished"),
        }
        for name, (kw, pattern) in cases.items():
            with self.subTest(name):
                code, out, _, fake = self.settled(**kw)
                self.assertEqual(code, 0)
                self.assertRegex(out, f"^kept x: .*{pattern}")
                self.assertFalse(self.removed_anything(fake))

    def test_a_sibling_directory_of_the_worktree_is_not_inside_it(self):
        code, out, _, _ = self.settled(deliverable=f"{WT}/x-out/video.mp4")
        self.assertEqual(out.strip(), "removed x")

    def test_rule_1_ignores_the_declared_state(self):
        for tasks in ([], [task_rec("x", "working")], [task_rec("x", "blocked")], [task_rec("x", "handed-over")],
                      [task_rec("x", "acknowledged")]):
            with self.subTest(tasks=tasks):
                code, out, _, fake = self.clean(tasks=tasks, extra={("git", "branch", "-r"): (128, "", "bad")})
                self.assertEqual(out.strip(), "removed x")
                self.assertFalse(fake.ran("git", "branch", "-r"))

    def test_sessions_that_do_not_belong_to_the_task_do_not_protect_it(self):
        elsewhere = [{"sessionId": "i1", "cwd": f"{WT}/x-other", "kind": "interactive"},
                     {"sessionId": "i2", "cwd": ROOT, "kind": "interactive"}]
        code, out, _, _ = self.clean(sessions=elsewhere)
        self.assertEqual(out.strip(), "removed x")

    def test_relist_failure_before_removal_stops_and_exits_2(self):
        entries = [task_entry("x"), task_entry("y")]
        agents = [(0, "[]", ""), (1, "", "boom")]
        code, out, err, fake = self.clean(entries=entries, extra={("claude", "agents"): agents})
        self.assertEqual(code, 2)
        self.assertIn("cannot list sessions again before removing x", err)
        self.assertEqual(out, "")
        self.assertFalse(self.removed_anything(fake))

    def test_sessions_unlistable_removes_nothing(self):
        for name, result in (("missing claude", (127, "", "no claude")), ("bad json", (0, "not json", "")),
                             ("not a list", (0, "{}", "")), ("nonzero exit", (1, "[]", "boom")),
                             ("entry not an object", (0, '["junk"]', ""))):
            with self.subTest(name):
                for dry in (False, True):
                    code, out, err, fake = self.clean(dry=dry, extra={("claude", "agents"): result})
                    self.assertEqual(code, 2)
                    self.assertIn("cannot list sessions", err)
                    self.assertEqual(out, "")
                    self.assertFalse(self.removed_anything(fake))

    def test_unfinished_session_without_cwd_removes_nothing(self):
        for name, session in (("busy background", {"kind": "background", "id": "ab", "status": "busy",
                                                   "state": "working"}),
                              ("interactive", {"kind": "interactive", "sessionId": "s9", "status": "idle"}),
                              ("empty cwd", {"kind": "background", "id": "ab", "status": "busy", "cwd": ""})):
            with self.subTest(name):
                for dry in (False, True):
                    code, out, err, fake = self.clean(dry=dry, sessions=[session])
                    self.assertEqual(code, 2)
                    self.assertIn("has no working directory and is not finished", err)
                    self.assertEqual(out, "")
                    self.assertFalse(self.removed_anything(fake))

    def test_finished_session_without_cwd_does_not_block(self):
        session = {"kind": "background", "id": "ab", "status": "idle", "state": "done"}
        code, out, _, _ = self.clean(sessions=[session])
        self.assertEqual((code, out.strip()), (0, "removed x"))

    def stacked(self, y_pr):
        """`clean` with y's recorded base still x; x is merged into main, y's pull requests are `y_pr`."""
        answers = repo_answers([task_entry("x"), task_entry("y")], (),
                               git_task_answers("x", time.time(), pr=self.pr()),
                               {("git", "config", "--get", "branch.worktree-y.factoryBase"): (0, "worktree-x\n", ""),
                                ("gh", "pr", "list", "--head", "worktree-y"): (0, json.dumps(y_pr), ""),
                                ("git", "worktree", "remove"): (0, "", ""), ("git", "branch"): (0, "", "")})
        _, out, _, fake = invoke(["clean"], answers)
        return out, fake

    def test_kept_when_another_task_is_stacked_on_it(self):
        for name, y_pr in (("open", self.pr("OPEN", "worktree-x")), ("no pull request", []),
                           ("merged into the base task", self.pr(base="worktree-x")),
                           ("open, retargeted to main", self.pr("OPEN"))):
            with self.subTest(name):
                out, fake = self.stacked(y_pr)
                self.assertIn("kept x: y is stacked on it", out)
                self.assertFalse(fake.ran("git", "worktree", "remove", f"{WT}/x"))

    def test_a_merged_stack_is_removed_although_the_recorded_base_was_never_cleared(self):
        out, fake = self.stacked(self.pr())
        self.assertEqual(out.splitlines(), ["removed x", "removed y"])
        self.assertTrue(fake.ran("git", "worktree", "remove", f"{WT}/x"))

    def test_remove_without_session_uses_git(self):
        code, out, _, fake = self.clean()
        self.assertEqual((code, out.strip()), (0, "removed x"))
        self.assertIn(["git", "worktree", "remove", f"{WT}/x"], fake.calls)
        self.assertIn(["git", "branch", "-d", "worktree-x"], fake.calls)
        self.assertNotIn("--force", [a for c in fake.calls for a in c])
        self.assertFalse(fake.ran("claude", "rm"))

    def test_remove_with_session_uses_claude_rm(self):
        idle = {"id": "ab12", "cwd": f"{WT}/x", "kind": "background", "status": "idle", "state": "done"}
        code, out, _, fake = self.clean(sessions=[idle])
        self.assertEqual((code, out.strip()), (0, "removed x"))
        self.assertIn(["claude", "rm", "ab12"], fake.calls)
        self.assertFalse(fake.ran("git", "worktree", "remove"))
        self.assertFalse(fake.ran("git", "branch"))

    def test_the_session_removed_is_the_lead_not_a_probe_listed_before_it(self):
        probe = {"id": "pr1", "name": "probe-x", "kind": "background", "state": "done", "cwd": f"{WT}/x"}
        lead = {"id": "ld1", "name": "x", "kind": "background", "state": "done", "cwd": f"{WT}/x"}
        code, out, _, fake = self.clean(sessions=[probe, lead])
        self.assertEqual((code, out.strip()), (0, "removed x"))
        self.assertIn(["claude", "rm", "ld1"], fake.calls)
        self.assertNotIn(["claude", "rm", "pr1"], fake.calls)

    def test_a_turn_that_has_ended_is_finished_whatever_the_state_word(self):
        live = {"id": "ab12", "cwd": f"{WT}/x", "kind": "background", "status": "idle", "pid": 7}
        cases = {"idle, working": (live | {"state": "working"}, "ab12"),
                 "idle, blocked": (live | {"state": "blocked"}, "ab12"),
                 "idle, no state": (live, "ab12"),
                 "stopped": (EXITED | {"state": "stopped"}, "ex1"),
                 "stopped, cwd still in the worktree": (EXITED | {"state": "stopped", "cwd": f"{WT}/x"}, "ex1")}
        for name, (session, sid) in cases.items():
            with self.subTest(name):
                code, out, _, fake = self.clean(sessions=[session])
                self.assertEqual((code, out.strip()), (0, "removed x"))
                self.assertIn(["claude", "rm", sid], fake.calls)

    def test_exited_lead_is_removed_with_claude_rm(self):
        locked = [task_entry("x") + ("claude session x (pid 7 start Sat)",)]
        for name, kw in {"unlocked": {}, "locked": dict(entries=locked),
                         "exits between listing and removal": dict(extra={("claude", "agents"): [
                             (0, json.dumps([EXITED | {"cwd": f"{WT}/x", "pid": 7, "status": "idle"}]), ""),
                             (0, json.dumps([EXITED]), "")]})}.items():
            with self.subTest(name):
                code, out, _, fake = self.clean(sessions=[EXITED], **kw)
                self.assertEqual((code, out.strip()), (0, "removed x"))
                self.assertIn(["claude", "rm", "ex1"], fake.calls)
                self.assertFalse(fake.ran("git", "worktree", "remove"))

    def test_a_lock_is_reported_only_when_the_task_would_otherwise_go(self):
        code, out, _, _ = self.clean(entries=[task_entry("x") + ("",)], pr=[])
        self.assertEqual(out.strip(), "kept x: no merged pull request (none)")

    def test_dry_run_changes_nothing(self):
        code, out, _, fake = self.clean(dry=True)
        self.assertEqual(out.strip(), "would remove x")
        self.assertFalse(self.removed_anything(fake))

    def test_failed_removal_exits_1_and_says_why(self):
        code, out, err, _ = self.clean(extra={("git", "worktree", "remove"): (1, "", "locked")})
        self.assertEqual(code, 1)
        self.assertIn("locked", err)
        self.assertNotIn("removed", out)

    def test_failed_branch_delete_names_the_branch(self):
        code, out, err, fake = self.clean(extra={("git", "branch"): (1, "", "not fully merged")})
        self.assertEqual(code, 1)
        self.assertRegex(err, "branch worktree-x")
        self.assertIn("not fully merged", err)
        self.assertIn(["git", "worktree", "remove", f"{WT}/x"], fake.calls)


class EndToEndTests(unittest.TestCase):
    FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:2] == ["agents"]:
    print(os.environ["FAKE_AGENTS"])
elif sys.argv[1:3] == ["plugin", "list"]:
    print("[]")
elif sys.argv[1:2] == ["--bg"]:
    json.dump(sys.argv, open(os.environ["FAKE_ARGV"], "w"))
    print("backgrounded · 96dc4869 · " + sys.argv[sys.argv.index("-n") + 1])
else:
    sys.exit(3)
"""
    FAKE_GH = """#!/usr/bin/env python3
import os
print(os.environ["FAKE_PR"])
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name).resolve()
        self.repo, self.home, bindir = tmp / "repo", tmp / "home", tmp / "bin"
        for d in (self.repo, self.home, bindir):
            d.mkdir()
        for name, text in (("claude", self.FAKE_CLAUDE), ("gh", self.FAKE_GH)):
            tool = bindir / name
            tool.write_text(text)
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.argv_file = tmp / "argv.json"
        self.dispatcher = {"sessionId": "sid-d", "name": "dispatcher-repo", "kind": "interactive",
                           "cwd": str(self.repo)}
        self.env = {**os.environ, "HOME": str(self.home), "FACTORY_HOME": str(self.home / ".claude" / "factory"),
                    "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_ARGV": str(self.argv_file), "FAKE_AGENTS": json.dumps([self.dispatcher]),
                    "FAKE_PR": "[]", "CLAUDE_CODE_SESSION_ID": "sid-d", "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "init")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env, check=True, capture_output=True, text=True).stdout

    def factory(self, *args, env=None, cwd=None):
        return subprocess.run([sys.executable, str(BIN), *args], cwd=cwd or self.repo, env={**self.env, **(env or {})},
                              capture_output=True, text=True)

    def test_new_status_clean(self):
        p = self.factory("new", "demo-task", "--brief", "build it")
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        self.assertIn("96dc4869", p.stdout)
        argv = json.loads(self.argv_file.read_text())
        self.assertEqual(argv[1:6], ["--bg", "--worktree", "demo-task", "-n", "demo-task"])
        self.assertIn("build it", argv[-1])
        self.assertIn("dispatcher-repo", argv[-1])

        # the fake does not make worktrees; create the one claude would have made
        self.git("worktree", "add", "-q", "-b", "worktree-demo-task", ".claude/worktrees/demo-task")
        task_dir = self.repo / ".claude/worktrees/demo-task"
        session = {"id": "96dc4869", "cwd": str(task_dir), "kind": "background", "name": "demo-task",
                   "status": "idle", "state": "done", "startedAt": int(time.time() * 1000)}
        agents = json.dumps([self.dispatcher, session])
        p = self.factory("status", "--json", env={"FAKE_AGENTS": agents})
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        (row,) = doc["rows"]
        self.assertEqual((row["slug"], row["session"], row["session_id"], row["ahead"], row["behind"]),
                         ("demo-task", "done", "96dc4869", 0, 0))
        self.assertEqual(row["pr_state"], "none")
        self.assertEqual((doc["open"], doc["leftover_agent_worktrees"], doc["outside_worktrees"]), (1, 0, 0))
        self.assertEqual((row["phase"], doc["in_motion"]), ("motion", 1))

        p = self.factory("new", "demo-task", "--brief", "again", env={"FAKE_AGENTS": agents})
        self.assertEqual(p.returncode, 2)

        # a fresh task with no pull request is kept, even though its tip sits on main
        p = self.factory("clean", env={"FAKE_AGENTS": agents})
        self.assertEqual((p.returncode, p.stdout.split(":")[0]), (0, "kept demo-task"))
        self.assertTrue(task_dir.is_dir())

        # sessions that cannot be listed: nothing removed
        p = self.factory("clean", env={"FAKE_AGENTS": "not json"})
        self.assertEqual(p.returncode, 2)
        self.assertTrue(task_dir.is_dir())

        # merged pull request at the same head: --dry-run changes nothing, a real clean removes it
        head = self.git("-C", str(task_dir), "rev-parse", "HEAD").strip()
        merged = json.dumps([{"state": "MERGED", "url": "u", "headRefOid": head, "baseRefName": "main",
                              "headRefName": "worktree-demo-task"}])
        no_session = {"FAKE_AGENTS": json.dumps([self.dispatcher]), "FAKE_PR": merged}
        p = self.factory("clean", "--dry-run", env=no_session)
        self.assertEqual(p.stdout.strip(), "would remove demo-task")
        self.assertTrue(task_dir.is_dir())
        p = self.factory("clean", env=no_session)
        self.assertEqual((p.returncode, p.stdout.strip(), p.stderr), (0, "removed demo-task", ""))
        self.assertFalse(task_dir.exists())
        self.assertEqual(self.git("branch", "--list", "worktree-demo-task"), "")

    def declared(self):
        files = sorted((self.home / ".claude/factory").glob("tasks-*.jsonl"))
        return [json.loads(line) for f in files for line in f.read_text().splitlines()]

    def test_state_from_inside_a_worktree_shows_in_status(self):
        self.git("worktree", "add", "-q", "-b", "worktree-demo-task", ".claude/worktrees/demo-task")
        task_dir = self.repo / ".claude/worktrees/demo-task"
        (task_dir / "sub").mkdir()
        p = self.factory("state", "working", cwd=task_dir / "sub")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "demo-task: working\n", ""))
        p = self.factory("state", "handed-over", "https://x/y", cwd=task_dir)
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        (rec,) = self.declared()[1:]
        self.assertEqual((rec["repo"], rec["slug"], rec["state"]), (str(self.repo), "demo-task", "handed-over"))
        row = json.loads(self.factory("status", "--json").stdout)["rows"][0]
        self.assertEqual((row["state"], row["deliverable"], row["phase"]), ("handed-over", "https://x/y", "review"))
        p = self.factory("state", "working", cwd=self.repo)
        self.assertEqual(p.returncode, 2)

    def test_a_file_inside_the_worktree_is_refused(self):
        self.git("worktree", "add", "-q", "-b", "worktree-demo-task", ".claude/worktrees/demo-task")
        task_dir = self.repo / ".claude/worktrees/demo-task"
        (task_dir / "out.mp4").write_text("x")
        p = self.factory("state", "handed-over", str(task_dir / "out.mp4"), cwd=task_dir)
        self.assertEqual((p.returncode, p.stdout), (2, ""))
        self.assertIn("inside the worktree", p.stderr)
        outside = self.home / "out.mp4"
        outside.write_text("x")
        (self.home / "rel.mp4").write_text("x")
        rel = os.path.relpath(self.home / "rel.mp4", task_dir)
        for given, stored in ((str(outside), outside), ("~/out.mp4", outside), (rel, self.home / "rel.mp4")):
            p = self.factory("state", "handed-over", given, cwd=task_dir)
            self.assertEqual((p.returncode, p.stderr), (0, ""))
            last = self.declared()[-1]
            self.assertEqual(last["deliverable"], os.path.realpath(stored))

    def test_default_dispatcher_name_comes_from_the_main_checkout_inside_a_worktree(self):
        self.git("worktree", "add", "-q", "-b", "worktree-first", ".claude/worktrees/first")
        p = subprocess.run([sys.executable, str(BIN), "new", "second-task", "--brief", "x"],
                           cwd=self.repo / ".claude/worktrees/first", env=self.env, capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        self.assertIn("The dispatcher's session name is dispatcher-repo.", json.loads(self.argv_file.read_text())[-1])


def listed(**by_branch):
    """The answer of the one `gh pr list`: {slug: state} as entries, newest first."""
    return 0, json.dumps([{"state": state, "url": f"http://pr/{slug}", "headRefOid": "h", "baseRefName": "main",
                           "headRefName": f"worktree-{slug}"} for slug, state in by_branch.items()]), ""


class PullRequestBatchTests(unittest.TestCase):
    """`status` asks GitHub once per repository, through a disk cache that keeps its values when GitHub fails."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        patch = mock.patch.dict(os.environ, {"HOME": home.name, "CLAUDE_CODE_SESSION_ID": "sid-d"})
        patch.start()
        self.addCleanup(patch.stop)
        self.gh = listed(a="OPEN", b="MERGED", c="CLOSED")
        self.extra = {}

    def status(self, *args, slugs="abc"):
        answers = repo_answers([task_entry(s) for s in slugs], (), git_task_answers("x", time.time()),
                               {BATCH: lambda: self.gh, **self.extra})
        fake = FakeRun(answers)
        out = io.StringIO()
        with mock.patch.object(factory, "run", fake), redirect_stdout(out), mock.patch.object(os, "getcwd", return_value=ROOT):
            self.assertEqual(factory.main(["status", *args]), 0)
        return (json.loads(out.getvalue()) if "--json" in args else out.getvalue()), fake

    def asked(self, fake):
        return [c for c in fake.calls if c[:3] == ["gh", "pr", "list"]]

    def age_cache(self, seconds):
        (path,) = (self.home / ".claude/factory/cache").glob("prs-*.json")
        stored = json.loads(path.read_text())
        stored["as_of"] -= seconds
        stored["asked"] -= seconds
        path.write_text(json.dumps(stored))
        return stored["as_of"]

    def test_one_call_for_all_tasks_and_the_rows_read_it(self):
        doc, fake = self.status("--json")
        (call,) = self.asked(fake)
        self.assertEqual(call[:7], ["gh", "pr", "list", "--state", "all", "--limit", "100"])
        self.assertNotIn("--head", call)
        self.assertEqual([(r["pr_state"], r["pr_url"]) for r in doc["rows"]],
                         [("open", "http://pr/a"), ("merged", "http://pr/b"), ("closed", "http://pr/c")])
        self.assertAlmostEqual(doc["prs"]["as_of"], time.time(), delta=60)
        self.assertIsNone(doc["prs"]["error"])

    def test_a_branch_the_list_lacks_is_none_and_the_first_entry_of_a_branch_wins(self):
        self.gh = 0, json.dumps([
            {"state": "OPEN", "url": "new", "headRefOid": "n", "baseRefName": "main", "headRefName": "worktree-a"},
            {"state": "CLOSED", "url": "old", "headRefOid": "o", "baseRefName": "main", "headRefName": "worktree-a"}]), ""
        rows = self.status("--json")[0]["rows"]
        self.assertEqual([(r["pr_state"], r["pr_url"]) for r in rows], [("open", "new"), ("none", ""), ("none", "")])

    def test_no_task_rows_no_call(self):
        doc, fake = self.status("--json", slugs="")
        self.assertEqual((self.asked(fake), doc["prs"]), ([], {"as_of": None, "error": None}))

    def test_cached_makes_no_gh_call(self):
        doc, fake = self.status("--json", "--cached")
        self.assertEqual((self.asked(fake), [c for c in fake.calls if c[0] == "gh"]), ([], []))
        self.assertEqual(([r["pr_state"] for r in doc["rows"]], doc["prs"]), (["unknown"] * 3, {"as_of": None, "error": None}))
        self.status("--json")
        as_of = self.age_cache(300)
        doc, fake = self.status("--json", "--cached", "--max-age", "1")
        self.assertEqual(([c for c in fake.calls if c[0] == "gh"], doc["prs"]), ([], {"as_of": as_of, "error": None}))
        self.assertEqual([r["pr_state"] for r in doc["rows"]], ["open", "merged", "closed"])
        self.assertRegex(self.status("--cached")[0], r"pull requests as of 5m ago\n")

    def test_max_age_skips_a_young_cache_and_refreshes_an_old_one(self):
        self.status("--json")
        self.assertEqual(len(self.asked(self.status("--json", "--max-age", "60")[1])), 0)
        self.assertEqual(len(self.asked(self.status("--json")[1])), 1)  # the default asks every time
        self.age_cache(90)
        self.gh = listed(a="MERGED", b="MERGED", c="CLOSED")
        doc, fake = self.status("--json", "--max-age", "60")
        self.assertEqual((len(self.asked(fake)), doc["rows"][0]["pr_state"]), (1, "merged"))

    def test_a_cache_written_before_the_draft_flag_is_discarded_and_read_again(self):
        self.status("--json")
        (path,) = (self.home / ".claude/factory/cache").glob("prs-*.json")
        stored = json.loads(path.read_text())
        stored["prs"] = {b: v[:4] for b, v in stored["prs"].items()}
        path.write_text(json.dumps(stored))
        doc, fake = self.status("--json", "--max-age", "3600")
        self.assertEqual((len(self.asked(fake)), doc["rows"][0]["pr_state"]), (1, "open"))

    def test_max_age_asks_when_the_cache_lacks_a_branch(self):
        self.status("--json", slugs="ab")
        doc, fake = self.status("--json", "--max-age", "3600")
        self.assertEqual((len(self.asked(fake)), doc["rows"][2]["pr_state"]), (1, "closed"))

    def test_a_failing_or_timing_out_gh_keeps_the_cached_values_and_recovery_clears_the_error(self):
        self.status("--json")
        as_of = self.age_cache(120)
        for failure, error in (((1, "", "HTTP 403: rate limit exceeded\nsecond line\n"), "HTTP 403: rate limit exceeded"),
                               ((124, "", "timed out"), "timed out after 10 s")):
            with self.subTest(error):
                self.gh = failure
                doc, out = self.status("--json")[0], self.status()[0]
                self.assertEqual(doc["prs"], {"as_of": as_of, "error": error})
                self.assertEqual([r["pr_state"] for r in doc["rows"]], ["open", "merged", "closed"])
                self.assertRegex(out, rf"pull requests as of 2m ago \(GitHub: {error}\)\n")
        self.gh = listed(a="MERGED", b="MERGED", c="CLOSED")
        doc, out = self.status("--json")[0], self.status()[0]
        self.assertIsNone(doc["prs"]["error"])
        self.assertGreater(doc["prs"]["as_of"], as_of)
        self.assertNotIn("pull requests as of", out)

    def age_ask(self, seconds):
        (path,) = (self.home / ".claude/factory/cache").glob("prs-*.json")
        stored = json.loads(path.read_text())
        stored["asked"] -= seconds
        path.write_text(json.dumps(stored))

    def test_a_failed_ask_is_not_repeated_within_max_age_and_a_later_success_clears_it(self):
        self.status("--json")
        as_of = self.age_cache(120)
        self.gh = 1, "", "HTTP 502"
        self.assertEqual(self.status("--json", "--max-age", "60")[0]["prs"], {"as_of": as_of, "error": "HTTP 502"})
        self.gh = listed(a="MERGED", b="MERGED", c="CLOSED")
        doc, fake = self.status("--json", "--max-age", "60")
        self.assertEqual((self.asked(fake), doc["prs"], doc["rows"][0]["pr_state"]),
                         ([], {"as_of": as_of, "error": "HTTP 502"}, "open"))
        self.assertIsNone(self.status("--json", "--cached")[0]["prs"]["error"])
        self.age_ask(61)
        doc, fake = self.status("--json", "--max-age", "60")
        self.assertEqual((len(self.asked(fake)), doc["prs"]["error"], doc["rows"][0]["pr_state"]), (1, None, "merged"))

    def test_a_failed_ask_without_a_cache_is_not_repeated_either_and_max_age_0_asks(self):
        self.gh = 1, "", "no route"
        self.status("--json", "--max-age", "60")
        self.gh = listed(a="OPEN", b="MERGED", c="CLOSED")
        doc, fake = self.status("--json", "--max-age", "60")
        self.assertEqual((self.asked(fake), doc["prs"], doc["rows"][0]["pr_state"]),
                         ([], {"as_of": None, "error": "no route"}, "unknown"))
        doc, fake = self.status("--json")
        self.assertEqual((len(self.asked(fake)), doc["prs"]["error"], doc["rows"][0]["pr_state"]), (1, None, "open"))

    def test_a_branch_the_full_list_cannot_settle_fails_the_ask_and_keeps_the_cache(self):
        self.status("--json")
        as_of = self.age_cache(120)
        self.gh = 0, json.dumps([{"state": "OPEN", "url": "u", "headRefOid": "h", "baseRefName": "main",
                                  "headRefName": f"other-{i}"} for i in range(100)]), ""
        self.extra = {("gh", "pr", "list", "--head"): (1, "", "boom")}
        doc = self.status("--json")[0]
        self.assertEqual((doc["prs"]["as_of"], doc["prs"]["error"] is not None), (as_of, True))
        self.assertEqual([r["pr_state"] for r in doc["rows"]], ["open", "merged", "closed"])

    def test_clean_does_not_read_or_write_the_cache_and_keeps_a_task_when_gh_fails(self):
        self.gh = listed(x="MERGED")
        self.status("--json", slugs="x")  # the cache now says merged
        cache = self.home / ".claude/factory/cache"
        before = {p.name: p.read_bytes() for p in cache.iterdir()}
        answers = repo_answers([task_entry("x")], (), git_task_answers("x", time.time()), {BATCH: (1, "", "HTTP 502")})
        answers.update({("git", "worktree", "remove"): (0, "", ""), ("git", "branch"): (0, "", ""), ("claude", "rm"): (0, "", "")})
        fake, out = FakeRun(answers), io.StringIO()
        with mock.patch.object(factory, "run", fake), redirect_stdout(out), mock.patch.object(os, "getcwd", return_value=ROOT):
            factory.main(["clean"])
        self.assertIn("kept x: its pull request state is unknown", out.getvalue())
        self.assertEqual({p.name: p.read_bytes() for p in cache.iterdir()}, before)

    def test_the_whole_ask_gets_a_budget_and_a_fallback_call_gets_what_is_left(self):
        full = [{"state": "OPEN", "url": "u", "headRefOid": "h", "baseRefName": "main", "headRefName": f"other-{i}"}
                for i in range(100)]
        self.gh = 0, json.dumps(full), ""
        timeouts = []

        def run(argv, cwd=None, input=None, timeout=None):
            timeouts.append(timeout)
            return self.gh if "--head" not in argv else (0, json.dumps([{"state": "OPEN"}]), "")
        clock = iter([0.0, 12.0, 25.0, 25.0, 25.0])
        repo = mock.Mock(root="/repo")
        with mock.patch.object(factory, "run", run), mock.patch.object(factory.time, "monotonic", lambda: next(clock)):
            found, error = factory.ask_pull_requests(repo, ["worktree-a", "worktree-b", "worktree-c"])
        self.assertEqual((found, timeouts, error), (None, [10, 8], f"no answer within {factory.PR_BUDGET} s"))

    def test_text_with_no_cache_and_a_failed_ask(self):
        self.gh = 1, "", "no route"
        self.assertRegex(self.status()[0], r"pull requests not fetched yet \(GitHub: no route\)\n")

    def test_a_failing_gh_without_a_cache_leaves_the_rows_unknown(self):
        self.gh = 1, "", "no route to host"
        doc = self.status("--json")[0]
        self.assertEqual(([r["pr_state"] for r in doc["rows"]], doc["prs"]),
                         (["unknown"] * 3, {"as_of": None, "error": "no route to host"}))

    def test_a_full_list_asks_for_a_branch_it_lacks_alone(self):
        full = [{"state": "OPEN", "url": "u", "headRefOid": "h", "baseRefName": "main", "headRefName": f"other-{i}"}
                for i in range(100)]
        self.gh = 0, json.dumps(full), ""
        old = 0, json.dumps([{"state": "MERGED", "url": "old", "headRefOid": "h", "baseRefName": "main"}]), ""
        answers = repo_answers([task_entry("a")], (), git_task_answers("x", time.time()), {
            BATCH: lambda: self.gh, ("gh", "pr", "list", "--head", "worktree-a"): old})
        fake, out = FakeRun(answers), io.StringIO()
        with mock.patch.object(factory, "run", fake), redirect_stdout(out), mock.patch.object(os, "getcwd", return_value=ROOT):
            factory.main(["status", "--json"])
        self.assertEqual(json.loads(out.getvalue())["rows"][0]["pr_state"], "merged")
        self.assertTrue(fake.ran("gh", "pr", "list", "--head", "worktree-a"))


class GhLogTests(unittest.TestCase):
    """`run` logs a `gh` call that is slow or fails, and gives it a default timeout."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        patch = mock.patch.dict(os.environ, {"HOME": home.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.timeouts = []

    def run_gh(self, argv, result, seconds=0.0):
        clock = iter([100.0, 100.0 + seconds])

        def spawn(argv, **kw):
            self.timeouts.append(kw["timeout"])
            if isinstance(result, Exception):
                raise result
            return subprocess.CompletedProcess(argv, *result)
        with mock.patch.object(factory.subprocess, "run", spawn), mock.patch.object(factory.time, "monotonic", lambda: next(clock)), \
                mock.patch.object(factory, "acting_identity", lambda cwd=None: None):
            factory.run(argv, cwd="/some/repo")
        return [r for _, path in factory.log_files("gh") for r in factory.read_file(path)]

    def test_a_fast_success_is_not_logged(self):
        self.assertEqual(self.run_gh(["gh", "pr", "list"], (0, "[]", "")), [])

    def test_a_slow_call_is_logged(self):
        (line,) = self.run_gh(["gh", "pr", "list", "--state", "all"], (0, "[]", ""), seconds=30.1)
        self.assertEqual({k: line[k] for k in ("argv", "cwd", "seconds", "rc", "error")},
                         {"argv": ["gh", "pr", "list", "--state", "all"], "cwd": "/some/repo", "seconds": 30.1, "rc": 0, "error": ""})
        self.assertRegex(line["ts"], r"^\d{4}-\d\d-\d\dT")
        self.assertTrue((self.home / ".claude/factory" / f"gh-{line['ts'][:7]}.jsonl").exists())

    def test_a_failure_is_logged_with_its_first_stderr_line_and_a_timeout_too(self):
        (line,) = self.run_gh(["gh", "pr", "list"], (1, "", "HTTP 403: forbidden\nmore\n"))
        self.assertEqual((line["rc"], line["error"]), (1, "HTTP 403: forbidden"))
        lines = self.run_gh(["gh", "pr", "list"], subprocess.TimeoutExpired("gh", 30), seconds=30.0)
        self.assertEqual([(l["rc"], l["error"]) for l in lines], [(1, "HTTP 403: forbidden"), (124, "timed out")])

    def test_other_commands_are_not_logged(self):
        self.assertEqual(self.run_gh(["git", "status"], (1, "", "fatal")), [])

    def test_a_gh_call_without_a_timeout_gets_30_s_and_a_given_one_stays(self):
        self.run_gh(["gh", "pr", "list"], (0, "", ""))
        with mock.patch.object(factory.subprocess, "run", lambda argv, **kw: self.timeouts.append(kw["timeout"]) or
                               subprocess.CompletedProcess(argv, 0, "", "")), mock.patch.object(factory, "acting_identity", lambda cwd=None: None):
            factory.run(["gh", "x"], timeout=1800)
            factory.run(["git", "status"])
        self.assertEqual(self.timeouts, [30, 1800, None])

    def test_a_log_that_cannot_be_written_is_swallowed(self):
        with mock.patch.object(factory, "log", side_effect=OSError("read-only")):
            self.assertEqual(self.run_gh(["gh", "x"], (1, "", "boom")), [])


if __name__ == "__main__":
    unittest.main()
