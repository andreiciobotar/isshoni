"""MCP servers for the lead and the tech lead: the record, the argv of the sessions that hold them, the gate (docs/factory.md, "MCP servers for the lead and the tech lead")."""
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import test_attribution
from test_factory import DEFAULT_SETTINGS, ROOT, factory, put_access

GET, SEARCH, DELETE = "mcp__tracker__get_issue", "mcp__docs__search", "mcp__tracker__delete_issue"
TRACKER, DOCS = {"type": "http", "url": "https://tracker.example/mcp"}, {"command": "docs-server", "args": ["--token=${DOCS_TOKEN}"]}
CONFIG = {"mcpServers": {"tracker": TRACKER, "docs": DOCS, "other": {"type": "http", "url": "https://other.example"}}}


def put_mcp(root, roles, **fields):
    """Record the MCP tools of the roles of the repository at `root` in the store, as `factory mcp grant` does."""
    path = factory.repo_state(root, "mcp-access", "json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root": root, "roles": roles, "at": "2026-11-18T15:00:00Z", "by": "human", "words": "", **fields}))
    return path


class InScratchStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        claude_json = self.dir / "claude.json"
        claude_json.write_text(json.dumps(CONFIG))
        for patcher in (mock.patch.object(factory, "STORE", self.dir), mock.patch.object(factory, "CLAUDE_JSON", claude_json),
                        mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("CLAUDECODE", "FACTORY_ROLE"):
            os.environ.pop(name, None)
        factory.warned.clear()


class McpAccessTests(InScratchStore):
    """`factory mcp access|grant|revoke` for the repository at ROOT."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(factory, "Repo", lambda cwd=None: mock.Mock(root=ROOT))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.file = factory.repo_state(ROOT, "mcp-access", "json")
        put_access(ROOT, tools=["mcp__logs__query"])

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = factory.main(["mcp", *argv])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def record(self):
        return json.loads(self.file.read_text())

    def test_a_repository_with_no_record_says_so(self):
        self.assertEqual(self.cli("access"), (0, "mcp servers: none recorded\n", ""))

    def test_the_human_grants_a_role_a_list_and_it_is_recorded_privately(self):
        code, out, err = self.cli("grant", "--role", "lead", GET, SEARCH, "--words", "the tracker and the docs")
        rec = self.record()
        self.assertEqual((code, err, stat.S_IMODE(self.file.stat().st_mode)), (0, "", 0o600))
        self.assertEqual({k: v for k, v in rec.items() if k != "at"},
                         {"root": ROOT, "roles": {"lead": [GET, SEARCH]}, "by": "human", "words": "the tracker and the docs"})
        self.assertEqual(out, f"mcp servers, lead: {GET} {SEARCH}\nrecorded {rec['at']} by human, the tracker and the docs\n")
        self.assertEqual(self.cli("access")[1], out)

    def test_both_roles_in_one_grant_and_a_second_grant_keeps_the_other_role(self):
        self.cli("grant", "--role", "lead", "--role", "tech-lead", GET)
        self.assertEqual(self.record()["roles"], {"lead": [GET], "tech-lead": [GET]})
        self.cli("grant", "--role", "tech-lead", SEARCH)
        self.assertEqual(self.record()["roles"], {"lead": [GET], "tech-lead": [SEARCH]})

    def test_a_session_cannot_grant_and_nothing_is_written(self):
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(self.cli("grant", "--role", "lead", GET),
                         (2, "", "factory: only the human gives a role MCP servers: run this in your own terminal\n"))
        self.assertFalse(self.file.exists())

    def test_a_wildcard_or_a_bad_tool_is_refused_with_the_reason(self):
        for bad in ("mcp__tracker__*", "Bash", "mcp__x", "mcp__a__q*"):
            code, _, err = self.cli("grant", "--role", "lead", GET, bad)
            self.assertEqual((code, err.startswith("factory: mcp: the tools must be exact MCP tool names")), (2, True), bad)
            self.assertIn("a tool a server adds later is not covered", err)
        self.assertFalse(self.file.exists())

    def test_an_unknown_role_or_none_is_refused(self):
        for argv in (("grant", "--role", "coder", GET), ("grant", GET)):
            self.assertEqual(self.cli(*argv)[0], 2, argv)
        self.assertFalse(self.file.exists())

    def test_production_access_not_asked_blocks_the_grant(self):
        factory.repo_state(ROOT, "ops-access", "json").unlink()
        code, _, err = self.cli("grant", "--role", "lead", GET)
        self.assertEqual((code, err), (2, "factory: mcp: answer production access first: factory ops grant ... or factory ops decline --words ...\n"))
        self.assertFalse(self.file.exists())

    def test_a_declined_production_goes_on_with_one_line(self):
        put_access(ROOT, state="declined", words="no")
        code, out, _ = self.cli("grant", "--role", "lead", GET)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("production access is declined here: by this line you say none of these servers reads production\n"), out)
        self.assertTrue(self.file.exists())

    def test_a_tool_of_a_production_server_needs_the_production_list_to_cover_it(self):
        put_access(ROOT, tools=["mcp__tracker__list_issues"])
        code, _, err = self.cli("grant", "--role", "lead", GET)
        self.assertEqual(code, 2)
        self.assertIn(f"{GET} is a tool of a production server", err)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.cli("grant", "--role", "lead", "mcp__tracker__list_issues", SEARCH)[0], 0)
        put_access(ROOT, tools=["mcp__tracker__*"])
        self.assertEqual(self.cli("grant", "--role", "lead", GET)[0], 0)

    def test_a_server_that_is_not_configured_is_warned_about_as_a_mcp_grant(self):
        code, _, err = self.cli("grant", "--role", "lead", "mcp__gone__x")
        self.assertEqual((code, err), (0, "factory: factory mcp grant names server gone, which is not configured here (claude mcp add)\n"))

    def test_revoke_deletes_the_record_and_the_config_files_and_a_session_may_run_it(self):
        self.cli("grant", "--role", "lead", "--role", "tech-lead", GET)
        configs = [Path(a.removeprefix("--mcp-config=")) for role in ("lead", "tech-lead")
                   for a in factory.role_mcp_flag(ROOT, role)]
        self.assertTrue(all(c.exists() for c in configs) and len(configs) == 2)
        os.environ["CLAUDECODE"] = "1"
        code, out, _ = self.cli("revoke", "--words", "done")
        self.assertEqual((code, out), (0, "mcp servers: none recorded (revoked, done)\n"))
        self.assertFalse(self.file.exists() or any(c.exists() for c in configs))
        self.assertEqual(self.cli("revoke")[0], 0)

    def test_a_record_that_cannot_be_read_fails_naming_the_file(self):
        good = {"root": ROOT, "roles": {"lead": [GET]}, "at": "t", "by": "human", "words": ""}
        for what, text in (("not json", "{"), ("a list", "[]"), ("another root", json.dumps({**good, "root": "/other"})),
                           ("no roles", json.dumps({**good, "roles": {}})), ("an unknown role", json.dumps({**good, "roles": {"coder": [GET]}})),
                           ("an empty list", json.dumps({**good, "roles": {"lead": []}})),
                           ("a wildcard", json.dumps({**good, "roles": {"lead": ["mcp__tracker__*"]}})),
                           ("bad by", json.dumps({**good, "by": "robot"})), ("no words", json.dumps({k: v for k, v in good.items() if k != "words"}))):
            with self.subTest(what):
                self.file.write_text(text)
                code, _, err = self.cli("access")
                self.assertEqual(code, 2)
                self.assertIn(str(self.file), err)


class McpArgvTests(InScratchStore):
    """The argv of each session start, with a record for the repository the builders are given."""

    def setUp(self):
        super().setUp()
        self.attribution = test_attribution.Attribution("builders")
        self.record = {"root": "x", "roles": {"lead": [GET], "tech-lead": [GET, SEARCH]}, "at": "t", "by": "human", "words": ""}

    def argv(self, name, lean=True, record="default"):
        record = self.record if record == "default" else record
        with mock.patch.object(factory, "mcp_access", return_value=record):
            return self.attribution.argv(self.attribution.builders()[name], lean)

    def config(self, argv):
        flags = [a for a in argv if a.startswith("--mcp-config=")]
        self.assertEqual(len(flags), 1, argv)
        path = Path(flags[0].removeprefix("--mcp-config="))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        return json.loads(path.read_text())

    def test_the_three_role_starts_carry_the_path_of_a_file_with_their_roles_servers_only(self):
        for lean in (True, False):
            self.assertEqual(self.config(self.argv("lead", lean)), {"mcpServers": {"tracker": TRACKER}}, lean)
            for name in ("tech lead", "build"):
                self.assertEqual(self.config(self.argv(name, lean)), {"mcpServers": {"tracker": TRACKER, "docs": DOCS}}, (name, lean))

    def test_only_the_path_reaches_the_command_line(self):
        for name in ("lead", "tech lead", "build"):
            argv = self.argv(name)
            self.assertNotIn(TRACKER["url"], json.dumps(argv))
            self.assertNotIn("DOCS_TOKEN", json.dumps(argv))

    def test_a_role_the_record_does_not_name_gets_no_flag(self):
        for name, roles in (("lead", {"tech-lead": [GET]}), ("tech lead", {"lead": [GET]}), ("build", {"lead": [GET]})):
            argv = self.argv(name, record={**self.record, "roles": roles})
            self.assertEqual([a for a in argv if a.startswith("--mcp-config")], [], name)

    def test_no_record_gives_no_flag_and_no_file(self):
        for name in ("lead", "tech lead", "build"):
            self.assertEqual([a for a in self.argv(name, record=None) if a.startswith("--mcp-config")], [], name)
        self.assertEqual(list(self.dir.glob("mcp-config-*")), [])

    def test_the_other_session_starts_carry_nothing_of_it(self):
        for name in ("site manager", "ops watch", "courier", "lander", "kaizen review"):
            for lean in (True, False):
                argv = self.argv(name, lean)
                self.assertEqual([a for a in argv if a.startswith("--mcp-config") or GET in a or SEARCH in a], [], (name, lean))

    def test_the_agent_definitions_are_the_same_with_and_without_a_record(self):
        put_access(ROOT, tools=["mcp__tracker__list_issues"])
        without = factory.agent_definitions(DEFAULT_SETTINGS, ROOT)
        put_mcp(ROOT, {"lead": [GET], "tech-lead": [GET, SEARCH]})
        self.assertEqual(factory.agent_definitions(DEFAULT_SETTINGS, ROOT), without)

    def test_another_repository_record_gives_nothing_here(self):
        put_mcp("/elsewhere", {"lead": [GET], "tech-lead": [GET]})
        for name in ("lead", "tech lead", "build"):
            argv = self.attribution.argv(self.attribution.builders()[name], True)
            self.assertEqual([a for a in argv if a.startswith("--mcp-config")], [], name)


class McpGateTests(InScratchStore):
    """`factory ops-hook` for the calls of the lead and the tech lead, in real repositories."""

    def setUp(self):
        super().setUp()
        self.a, self.b = self.repository("a"), self.repository("b")
        self.worktree = str(Path(self.a) / ".claude" / "worktrees" / "task")
        self.git(self.a, "worktree", "add", "-q", self.worktree, "-b", "task")
        self.record = put_mcp(self.a, {"lead": [GET], "tech-lead": [GET, SEARCH]})

    def git(self, cwd, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)

    def repository(self, name):
        path = self.dir / name
        path.mkdir()
        self.git(path, "init", "-q")
        self.git(path, "commit", "-q", "--allow-empty", "-m", "init")
        return os.path.realpath(path)

    def hook(self, payload):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = factory.main(["ops-hook"])
            finally:
                signal.alarm(0)
        return code, out.getvalue(), err.getvalue()

    def call(self, tool, cwd=None, role=None, **extra):
        """The decision of the gate on a call of `tool` from a session started in `cwd` (a task worktree: the lead) with FACTORY_ROLE `role`."""
        env = {"FACTORY_ROLE": role} if role else {}
        with mock.patch.dict(os.environ, env):
            code, out, err = self.hook({"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": "s1", "cwd": cwd or self.worktree, **extra})
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)["hookSpecificOutput"] if out else None

    def verdict(self, *args, **kw):
        return (said := self.call(*args, **kw)) and (said["permissionDecision"], said["permissionDecisionReason"])

    def test_the_lead_and_the_tech_lead_are_allowed_a_listed_tool_and_denied_an_unlisted_one_of_the_same_server(self):
        self.assertEqual(self.verdict(GET), ("allow", "on this repository's list for the lead"))
        self.assertEqual(self.verdict(GET, role="tech-lead"), ("allow", "on this repository's list for the tech-lead"))
        for role in (None, "tech-lead"):
            self.assertEqual(self.verdict(DELETE, role=role), ("deny", f"{DELETE} is not on this repository's list for the {role or 'lead'} (factory mcp access)"))
        self.assertEqual(self.verdict(SEARCH, role="tech-lead")[0], "allow")
        self.assertEqual(self.verdict(SEARCH)[0], "deny")

    def test_a_role_with_no_list_is_denied(self):
        put_mcp(self.a, {"lead": [GET]})
        self.assertEqual(self.verdict(GET, role="tech-lead")[0], "deny")

    def test_a_subagent_is_denied_whoever_started_it(self):
        reason = "MCP servers are given to the lead and the tech lead, not to their subagents (factory mcp access)"
        for role in (None, "tech-lead"):
            for agent in ("factory:coder", "factory:reviewer", "general-purpose"):
                self.assertEqual(self.verdict(GET, role=role, agent_id="a1", agent_type=agent), ("deny", reason), (role, agent))

    def test_a_session_with_no_role_gets_no_output(self):
        self.assertIsNone(self.call(GET, cwd=self.a))
        self.assertIsNone(self.call(GET, cwd=self.a, agent_id="a1", agent_type="factory:coder"))

    def test_a_repository_with_no_record_gets_no_output(self):
        other = str(Path(self.b) / ".claude" / "worktrees" / "task")
        self.git(self.b, "worktree", "add", "-q", other, "-b", "task")
        for role in (None, "tech-lead"):
            self.assertIsNone(self.call(GET, cwd=other, role=role))
            self.assertIsNone(self.call(GET, cwd=other, role=role, agent_id="a1"))

    def test_production_granted_later_forbids_a_tool_outside_its_list(self):
        put_access(self.a, tools=["mcp__tracker__list_issues"])
        self.assertEqual(self.verdict(GET)[0], "deny")
        self.assertEqual(self.verdict(SEARCH, role="tech-lead")[0], "allow")
        put_access(self.a, tools=["mcp__tracker__*"])
        self.assertEqual(self.verdict(GET)[0], "allow")
        put_access(self.a, state="declined", words="no")
        self.assertEqual(self.verdict(GET)[0], "allow")

    def test_the_result_of_a_call_is_not_recorded(self):
        payload = {"hook_event_name": "PostToolUse", "tool_name": GET, "session_id": "s1", "cwd": self.worktree, "tool_use_id": "toolu_1",
                   "tool_input": {}, "tool_response": []}
        self.assertEqual(self.hook(payload), (0, "", ""))
        self.assertFalse((self.dir / "deliverables").exists())

    def test_a_record_that_cannot_be_read_blocks_the_call(self):
        for text in ("{", "[]", json.dumps({"root": self.a, "roles": {"lead": ["mcp__tracker__*"]}, "at": "t", "by": "human", "words": ""})):
            self.record.write_text(text)
            code, out, err = self.hook({"hook_event_name": "PreToolUse", "tool_name": GET, "session_id": "s1", "cwd": self.worktree})
            self.assertEqual((code, out), (2, ""))
            self.assertIn(self.record.name, err)


if __name__ == "__main__":
    unittest.main()
