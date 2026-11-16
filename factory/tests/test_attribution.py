"""Every session the factory starts is started with the harness's commit and pull request attribution off."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import NO_ATTRIBUTION, factory
from test_health import pic


class Recorded(Exception):
    """Raised by the fake `run` once a session's argv is in hand: nothing after it matters here."""


class Attribution(unittest.TestCase):
    def argv(self, build, lean):
        """The argv of the session `build` starts: the first `claude -p` or `claude --bg` the code runs."""
        seen = []

        def fake_run(argv, **_):
            if argv[:3] == ["claude", "plugin", "list"]:
                return 0, "[]", ""
            seen.append(argv)
            raise Recorded
        settings = {**factory.SETTINGS_DEFAULTS, "lean_start": lean}
        with mock.patch.object(factory, "run", fake_run), mock.patch.object(factory, "load_settings", return_value=settings), \
                mock.patch.object(factory, "tech_lead_prompt", return_value="p"), mock.patch.object(factory, "build_prompt", return_value="p"), \
                mock.patch.object(factory, "commands_line", return_value="c"), tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            try:
                result = build(settings, tmp)
            except Recorded:
                result = seen[0]
            return result

    def builders(self):
        repo = lambda tmp: SimpleNamespace(root=tmp, default="main", remote="origin", worktree_path=lambda name: f"{tmp}/.claude/worktrees/{name}")

        def ops(settings, tmp):
            (tmp / "notes").write_text("")
            return factory.ops_argv(repo(tmp), {"name": "w", "goal": "g", "until": "u", "notes": str(tmp / "notes")}, False, 1)

        def lander(settings, tmp):
            land = factory.Landing.__new__(factory.Landing)
            land.__dict__.update(repo=repo(tmp), path=str(tmp), branch="b", remote="origin", slug="s", lean=settings["lean_start"],
                                 mode="auto", declared=True, old_head="h", base="main", merged=False, target="main")
            land.out = lambda *a: "h" if a[0] == "rev-parse" else "x" if a[0] == "diff" else ""
            land.git = land.log_queue = land.mark_working = lambda *a, **k: None
            return land.conflict([], "CONFLICT (content): x")

        return {
            "lead": lambda s, tmp: factory.build_launch_argv("s", "d", "brief", s, "main", "origin", 0, 3600, tmp),
            "site manager": lambda s, tmp: factory.site_manager_argv(pic(dispatcher="d", lean_start=s["lean_start"]), []),
            "ops watch": ops,
            "courier": lambda s, tmp: factory.courier(repo(tmp), "n", "l") or None,
            "lander": lander,
            "tech lead": lambda s, tmp: factory.tech_lead_argv(repo(tmp), "s", {}, "e", "spec", s),
            "build": lambda s, tmp: factory.build_argv(repo(tmp), "s", "spec", tmp / "h", "", s),
            "kaizen review": lambda s, tmp: factory.review_argv(s, "f", "p"),
        }

    def test_every_session_start_has_one_settings_flag_with_attribution_off(self):
        self.assertEqual(factory.ATTRIBUTION_OFF, NO_ATTRIBUTION["attribution"])
        for name, build in self.builders().items():
            for lean in (True, False):
                with self.subTest(session=name, lean_start=lean):
                    argv = self.argv(build, lean)
                    self.assertEqual(argv.count("--settings"), 1)
                    self.assertEqual(json.loads(argv[argv.index("--settings") + 1])["attribution"], factory.ATTRIBUTION_OFF)

    def test_a_new_session_start_must_be_added_to_this_test(self):
        starts = re.findall(r'"claude", "(?:-p|--bg)"', Path(factory.__file__).read_text())
        self.assertEqual(len(starts), len(self.builders()), "a session start was added: give it the settings, then list it here")


if __name__ == "__main__":
    unittest.main()
