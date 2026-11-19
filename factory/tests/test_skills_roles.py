"""Skills per role: the record, who records it, and what each role start does with it (docs/factory.md, "Skills per role")."""
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_factory import DISPATCHER, NewTests, factory, invoke, repo_answers
from test_mcp_roles import InScratchStore

ALPHA, BETA = "alpha-rule", "beta-rule"
LAUNCHES = ("lead", "tech-lead (build)", "tech-lead (merge)")
SETTINGS = {**factory.SETTINGS_DEFAULTS, "lean_start": False}


def skill(directory, name, head=""):
    (directory / name).mkdir(parents=True)
    (directory / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n{head}---\nbody\n")


class InScratchRepo(InScratchStore):
    """A scratch repository with the skills ALPHA (its own) and BETA (the user's), and a scratch user directory."""

    def setUp(self):
        super().setUp()
        self.root = str(Path(tempfile.mkdtemp()).resolve())
        self.user = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(shutil.rmtree, self.user)
        skill(Path(self.root) / ".claude" / "skills", ALPHA)
        skill(self.user, BETA)
        patcher = mock.patch.object(factory, "USER_SKILLS", self.user)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.file = factory.repo_state(self.root, "skills", "json")

    def put(self, roles, root=None, **fields):
        root = root or self.root
        path = factory.repo_state(root, "skills", "json")
        path.write_text(json.dumps({"root": root, "roles": roles, "at": "2026-11-18T15:00:00Z", "by": "human", "words": "", **fields}))


class SkillsCommandTests(InScratchRepo):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(factory, "Repo", lambda cwd=None: mock.Mock(root=self.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = factory.main(["skills", *argv])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def record(self):
        return json.loads(self.file.read_text())

    def as_session(self, dispatcher):
        sessions = [{"sessionId": "me", "id": "me", "name": "d", "pid": 1}]
        found = (sessions[0], None) if dispatcher else (None, "no dispatcher")
        env = mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "me"})
        for patcher in (env, mock.patch.object(factory, "list_sessions", return_value=sessions),
                        mock.patch.object(factory, "find_dispatcher", return_value=found)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_no_record_says_so(self):
        self.assertEqual(self.cli("access"), (0, "skills: none recorded\n", ""))

    def test_a_terminal_grants_from_the_repository_and_the_user_skills_and_the_record_is_private(self):
        code, out, err = self.cli("grant", "--role", "reviewer", ALPHA, BETA, "--words", "the rules")
        rec = self.record()
        self.assertEqual((code, err, stat.S_IMODE(self.file.stat().st_mode)), (0, "", 0o600))
        self.assertEqual({k: v for k, v in rec.items() if k != "at"},
                         {"root": self.root, "roles": {"reviewer": [ALPHA, BETA]}, "by": "human", "words": "the rules"})
        self.assertEqual(out, f"skills, reviewer: {ALPHA} {BETA}\nrecorded {rec['at']} by human, the rules\n")
        self.assertEqual(self.cli("access")[1], out)

    def test_the_dispatcher_session_grants_and_it_is_recorded_as_a_session(self):
        self.as_session(dispatcher=True)
        self.assertEqual(self.cli("grant", "--role", "coder", ALPHA)[0], 0)
        self.assertEqual(self.record()["by"], "session")

    def test_another_session_is_refused_and_nothing_is_written(self):
        self.as_session(dispatcher=False)
        code, _, err = self.cli("grant", "--role", "coder", ALPHA)
        self.assertEqual(code, 2)
        self.assertIn("only the dispatcher records skills, on the human's word", err)
        self.assertFalse(self.file.exists())

    def test_grant_keeps_the_other_roles_list_and_replaces_the_named_one(self):
        self.cli("grant", "--role", "lead", "--role", "coder", ALPHA)
        self.cli("grant", "--role", "coder", BETA)
        self.assertEqual(self.record()["roles"], {"lead": [ALPHA], "coder": [BETA]})

    def test_a_name_that_cannot_be_recorded_is_refused_with_the_reason_and_nothing_is_written(self):
        skill(Path(self.root) / ".claude" / "skills", "hidden", "disable-model-invocation: true\n")
        for name, reason in (("nothing-here", "nothing-here is not in .claude/skills of " + self.root), ("plug:skill", "plugin skill"),
                             ("../x", "not a skill name"), (f"{self.root}/.claude/skills/{ALPHA}", "not a skill name"),
                             ("Alpha", "not a skill name"), ("hidden", "disable-model-invocation: true")):
            with self.subTest(name):
                code, _, err = self.cli("grant", "--role", "coder", name)
                self.assertEqual(code, 2)
                self.assertIn(reason, err)
                self.assertFalse(self.file.exists())

    def test_an_unknown_name_names_both_places_it_looked(self):
        _, _, err = self.cli("grant", "--role", "coder", "nothing-here")
        self.assertIn(f"{self.root}", err)
        self.assertIn(str(self.user), err)

    def test_an_unknown_role_is_refused(self):
        self.assertEqual(self.cli("grant", "--role", "lander", ALPHA)[0], 2)

    def test_a_skill_deleted_afterwards_is_shown_as_not_found(self):
        self.cli("grant", "--role", "coder", ALPHA, BETA)
        (self.user / BETA / "SKILL.md").unlink()
        self.assertIn(f"skills, coder: {ALPHA} {BETA} (not found)\n", self.cli("access")[1])

    def test_revoke_deletes_the_record_and_the_dispatcher_may_run_it(self):
        self.cli("grant", "--role", "coder", ALPHA)
        self.as_session(dispatcher=True)
        self.assertEqual(self.cli("revoke", "--words", "no more"), (0, "skills: none recorded (revoked, no more)\n", ""))
        self.assertFalse(self.file.exists())

    def test_another_session_cannot_revoke(self):
        self.cli("grant", "--role", "coder", ALPHA)
        self.as_session(dispatcher=False)
        self.assertEqual(self.cli("revoke")[0], 2)
        self.assertTrue(self.file.exists())

    def test_a_record_that_cannot_be_read_fails_naming_the_file(self):
        good = {"root": self.root, "roles": {"coder": [ALPHA]}, "at": "t", "by": "human", "words": ""}
        for what, text in (("not json", "{"), ("no roles", json.dumps({**good, "roles": {}})),
                           ("a role of no skills", json.dumps({**good, "roles": {"lead": []}})),
                           ("an unknown role", json.dumps({**good, "roles": {"lander": [ALPHA]}})),
                           ("a path as a name", json.dumps({**good, "roles": {"coder": ["../x"]}})),
                           ("another root", json.dumps({**good, "root": "/elsewhere"})), ("bad by", json.dumps({**good, "by": "robot"}))):
            with self.subTest(what):
                self.file.write_text(text)
                code, _, err = self.cli("access")
                self.assertEqual(code, 2)
                self.assertIn(str(self.file), err)
                self.assertIn("fix or delete it", err)


class SkillsStartTests(InScratchRepo):
    """What each start does with a record; `without` is what it gives with none."""

    def setUp(self):
        super().setUp()
        self.repo = SimpleNamespace(root=self.root, default="main", remote="origin", worktree_path=lambda name: f"{self.root}/.claude/worktrees/{name}")
        for name in ("tech_lead_prompt", "build_prompt", "commands_line"):
            patcher = mock.patch.object(factory, name, return_value="p")
            patcher.start()
            self.addCleanup(patcher.stop)

    def starts(self, root=None):
        """The three argvs and the agent definitions, as started now."""
        repo = SimpleNamespace(**{**vars(self.repo), "root": root or self.root})
        return {"lead": factory.build_launch_argv("s", "d", "brief", SETTINGS, "main", "origin", 0, 3600, repo.root),
                "tech-lead (build)": factory.build_argv(repo, "s", "spec", Path("h"), "", SETTINGS),
                "tech-lead (merge)": factory.tech_lead_argv(repo, "s", {}, "e", "spec", SETTINGS),
                "agents": factory.agent_definitions(SETTINGS, repo.root, repo.worktree_path("s"))}

    @staticmethod
    def prompt(argv):
        return argv[argv.index("--append-system-prompt") + 1]

    def sentence(self, role, *names):
        return factory.skills_said(role, names)

    def test_a_record_for_the_reviewer_adds_its_skills_after_the_default_and_a_sentence_to_nothing_else(self):
        without = self.starts()
        self.put({"reviewer": [ALPHA, BETA]})
        got = self.starts()
        reviewer = got["agents"]["factory:reviewer"]
        self.assertEqual(reviewer["skills"], [*without["agents"]["factory:reviewer"]["skills"], ALPHA, BETA])
        self.assertEqual(without["agents"]["factory:reviewer"]["skills"][0], "coding-taste")
        self.assertEqual(reviewer["prompt"], f"{without['agents']['factory:reviewer']['prompt']}\n\n{self.sentence('reviewer', ALPHA, BETA)} "
                                             "They are loaded above: work by them, as by your defaults.")
        self.assertEqual({**got["agents"], "factory:reviewer": None}, {**without["agents"], "factory:reviewer": None})
        self.assertEqual([self.prompt(got[k]) for k in LAUNCHES], [self.prompt(without[k]) for k in LAUNCHES])

    def test_a_record_for_the_coder_gives_the_reviewer_nothing(self):
        without = self.starts()
        self.put({"coder": [ALPHA]})
        got = self.starts()
        self.assertEqual(got["agents"]["factory:coder"]["skills"], [*without["agents"]["factory:coder"]["skills"], ALPHA])
        self.assertIn(self.sentence("coder", ALPHA), got["agents"]["factory:coder"]["prompt"])
        self.assertEqual({**got["agents"], "factory:coder": None}, {**without["agents"], "factory:coder": None})
        self.assertEqual([self.prompt(got[k]) for k in LAUNCHES], [self.prompt(without[k]) for k in LAUNCHES])

    def test_a_record_for_the_lead_changes_only_the_leads_system_prompt(self):
        without = self.starts()
        self.put({"lead": [ALPHA]})
        got = self.starts()
        self.assertEqual(self.prompt(got["lead"]), f"{self.prompt(without['lead'])}\n\n{self.sentence('lead', ALPHA)} "
                                                  "Load each with the Skill tool before your first decision and work by it.")
        self.assertEqual({**got, "lead": without["lead"]}, without)
        self.assertEqual([a for a in got["lead"] if a != self.prompt(got["lead"])], [a for a in without["lead"] if a != self.prompt(without["lead"])])

    def test_a_record_for_the_tech_lead_changes_both_its_starts_and_nothing_else(self):
        without = self.starts()
        self.put({"tech-lead": [BETA]})
        got = self.starts()
        for name in ("tech-lead (build)", "tech-lead (merge)"):
            self.assertEqual(self.prompt(got[name]), f"{self.prompt(without[name])}\n\n{self.sentence('tech-lead', BETA)} "
                                                    "Load each with the Skill tool before your first decision and work by it.")
            self.assertEqual([a for a in got[name] if a != self.prompt(got[name])], [a for a in without[name] if a != self.prompt(without[name])])
        self.assertEqual((got["lead"], got["agents"]), (without["lead"], without["agents"]))

    def test_a_default_named_again_is_not_duplicated(self):
        skill(Path(self.root) / ".claude" / "skills", "coding-taste")
        self.put({"coder": ["coding-taste", ALPHA]})
        self.assertEqual(self.starts()["agents"]["factory:coder"]["skills"].count("coding-taste"), 1)

    def test_another_repositorys_record_gives_nothing_here(self):
        without = self.starts()
        self.put({"lead": [ALPHA], "tech-lead": [ALPHA], "coder": [ALPHA], "reviewer": [ALPHA]}, root="/elsewhere")
        self.assertEqual(self.starts(), without)

    def test_a_skill_of_the_worktree_is_found_there(self):
        skill(Path(self.root) / ".claude" / "worktrees" / "s" / ".claude" / "skills", "only-here")
        self.put({"coder": ["only-here"]})
        self.assertEqual(factory.role_skills(self.root, "coder", self.repo.worktree_path("s")), ["only-here"])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(factory.role_skills(self.root, "coder"), [])

    def test_a_worktree_with_its_own_skills_does_not_fall_back_to_the_main_checkouts(self):
        skill(Path(self.root) / ".claude" / "worktrees" / "s" / ".claude" / "skills", "other")
        self.put({"coder": [ALPHA]})
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(factory.role_skills(self.root, "coder", self.repo.worktree_path("s")), [])
        self.assertIn(f"names {ALPHA} for coder, which is not in .claude/skills of {self.repo.worktree_path('s')}", err.getvalue())

    def test_a_skill_deleted_afterwards_is_left_out_and_warned_about_once_per_role(self):
        self.put({"lead": [ALPHA, BETA], "coder": [BETA], "tech-lead": [BETA], "reviewer": [BETA]})
        (self.user / BETA / "SKILL.md").unlink()
        err = io.StringIO()
        with redirect_stderr(err):
            self.starts()
            self.starts()
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), len(set(lines)))
        for role in ("lead", "coder", "tech-lead", "reviewer"):
            self.assertTrue(any(f"names {BETA} for {role}, which is not in .claude/skills of" in line for line in lines), role)
        self.assertTrue(all(line.startswith("factory: skills record names ") and line.endswith("starting without it (factory skills access)") for line in lines))
        got = self.starts()
        self.assertIn(f"`{ALPHA}`", self.prompt(got["lead"]))
        self.assertNotIn(BETA, self.prompt(got["lead"]))
        self.assertEqual(got["agents"]["factory:coder"]["skills"], factory.agent_definitions(SETTINGS, "/elsewhere")["factory:coder"]["skills"])

    def test_a_new_task_resolves_all_four_roles_once(self):
        (self.user / BETA / "SKILL.md").unlink()
        record = {"roles": {role: [BETA] for role in factory.SKILL_ROLES}}
        answers = repo_answers([], [dict(DISPATCHER, name="d1")], NewTests.OK)
        with mock.patch.object(factory, "skills_record", return_value=record):
            code, _, err, _ = invoke(["new", "demo-task", "--brief", "do it", "--dispatcher", "d1"], answers)
        self.assertEqual(code, 0, err)
        roles = [line.split(" for ")[1].split(",")[0] for line in err.splitlines() if "skills record names" in line]
        self.assertEqual(sorted(roles), sorted(factory.SKILL_ROLES))

    def test_the_ops_watch_reads_no_skills_record(self):
        self.file.write_text("{")
        notes = Path(self.root) / "notes.md"
        notes.write_text("")
        watch = {"name": "w", "goal": "g", "until": "u", "notes": str(notes), "checks": None}
        err = io.StringIO()
        with redirect_stderr(err):
            argv = factory.ops_argv(SimpleNamespace(root=self.root), watch, False, 1)
            self.assertEqual(list(json.loads(argv[argv.index("--agents") + 1])), [factory.OPS_AGENT])
            with self.assertRaises(factory.Fail):
                factory.agent_definitions(SETTINGS, self.root)
        self.assertNotIn("skills record", err.getvalue())


class DispatcherCheckTests(unittest.TestCase):
    """The check `factory new` and `factory skills` share."""

    def check(self, env=None, sessions=None, found=None):
        repo = SimpleNamespace(root="/r")
        with mock.patch.dict(os.environ, env or {}, clear=True), mock.patch.object(factory, "find_dispatcher", return_value=found or (None, "no one")):
            return factory.require_dispatcher(repo, sessions or [])

    def test_the_registered_dispatcher_passes_and_returns_itself(self):
        me = {"sessionId": "me", "name": "d"}
        self.assertIs(self.check({"CLAUDE_CODE_SESSION_ID": "me"}, [me], (me, None)), me)

    def test_the_skills_commands_name_themselves(self):
        with mock.patch.dict(os.environ, clear=True), self.assertRaises(factory.Fail) as cm:
            factory.require_dispatcher(SimpleNamespace(root="/r"), [], command="factory skills")
        self.assertEqual(str(cm.exception), "CLAUDE_CODE_SESSION_ID is not set; run factory skills from the dispatcher session")

    def test_each_way_to_fail_keeps_its_message(self):
        me, other = {"sessionId": "me", "name": "d"}, {"sessionId": "o", "name": "x"}
        for env, sessions, found, message in (({}, [], None, "CLAUDE_CODE_SESSION_ID is not set; run factory new from the dispatcher session"),
                                              ({"CLAUDE_CODE_SESSION_ID": "me"}, [], None, "the calling session me is not listed by `claude agents --json --all`"),
                                              ({"CLAUDE_CODE_SESSION_ID": "me"}, [me], (other, None), "this session is not the dispatcher of /r (it is x); run /factory:dispatch in it"),
                                              ({"CLAUDE_CODE_SESSION_ID": "me"}, [me], (None, "no one"), "this session is not the dispatcher of /r (no one); run /factory:dispatch in it")):
            with self.subTest(message), self.assertRaises(factory.Fail) as cm:
                self.check(env, sessions, found)
            self.assertEqual(str(cm.exception), message)


if __name__ == "__main__":
    unittest.main()
