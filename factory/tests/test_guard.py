import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from test_factory import BIN, factory


def line(name, path, found):
    return (f"factory: {name}: the worktree {path} has no .git, so git there answers from {found}: nothing ran. "
            "Change nothing and hand back with this line.")


class GuardCase(unittest.TestCase):
    """Real git: a main checkout with a dirty file and a worktree `t1`, whose `.git` a test removes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name).resolve()
        self.home = tmp / "home"  # never created: the hook must not touch it
        self.env = {**os.environ, "HOME": str(tmp), "FACTORY_HOME": str(self.home), "FACTORY_SESSION": "1", "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        self.env.pop("CLAUDE_PROJECT_DIR", None)
        self.root, self.wt = tmp / "repo", tmp / "repo" / ".claude" / "worktrees" / "t1"
        self.git(tmp, "init", "-q", "-b", "main", str(self.root))
        (self.root / "a.txt").write_text("one\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-q", "-m", "init")
        self.git(self.root, "worktree", "add", "-q", "-b", "worktree-t1", str(self.wt), "main")
        (self.root / "a.txt").write_text("dirty\n")
        self.want = line("t1", self.wt, self.root)
        self.main_before = self.main_state()

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True)

    def main_state(self):
        return [self.git(self.root, *a).stdout for a in (("status", "--porcelain"), ("stash", "list"))]

    def break_worktree(self):
        (self.wt / ".git").unlink()

    def hook(self, payload, env=None):
        p = subprocess.run([str(BIN), "guard-hook"], input=json.dumps(payload), env={**self.env, **(env or {})},
                           capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr

    def hook_in_process(self, payload):
        """(exit code, stdout), with every way to start a process refused."""
        with mock.patch.object(factory, "run", side_effect=AssertionError), mock.patch("subprocess.Popen", side_effect=AssertionError), \
                mock.patch("sys.stdin", io.StringIO(payload)), mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(io.StringIO()) as out:
            return factory.cmd_guard_hook(None), out.getvalue()

    def assert_denied(self, out):
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                                   "permissionDecisionReason": self.want}})


class HookTests(GuardCase):
    def test_a_broken_worktree_is_denied_with_the_line_and_the_main_checkout_is_untouched(self):
        self.break_worktree()
        (self.wt / "sub").mkdir()
        for cwd in (self.wt, self.wt / "sub"):
            code, out, err = self.hook({"cwd": str(cwd), "tool_name": "Bash"})
            self.assertEqual((code, err), (0, ""))
            self.assert_denied(out)
        self.assertEqual(self.main_state(), self.main_before)
        self.assertFalse(self.home.exists())

    def test_a_session_that_is_not_the_factorys_is_not_denied_and_reads_no_store(self):
        self.break_worktree()
        del self.env["FACTORY_SESSION"]
        self.assertEqual(self.hook({"cwd": str(self.wt), "tool_name": "Bash"}), (0, "", ""))
        self.assertEqual(self.hook_in_process(json.dumps({"cwd": str(self.wt)})), (0, ""))
        self.assertFalse(self.home.exists())

    def test_the_session_of_a_declared_task_is_denied_without_FACTORY_SESSION(self):
        self.break_worktree()
        del self.env["FACTORY_SESSION"]
        self.home.mkdir()
        (self.home / "tasks.jsonl").write_text(json.dumps(
            {"ts": "2026-11-19T10:00:00Z", "repo": str(self.root), "slug": "t1", "state": "working"}) + "\n")
        code, out, err = self.hook({"cwd": str(self.wt), "tool_name": "Bash"})
        self.assertEqual((code, err), (0, ""))
        self.assert_denied(out)

    def test_the_process_directory_may_be_gone(self):
        self.break_worktree()
        gone = self.root.parent / "gone"
        gone.mkdir()
        p = subprocess.run(["sh", "-c", 'cd "$1" && rmdir "$1" && exec "$2" guard-hook', "sh", str(gone), str(BIN)],
                           input=json.dumps({"cwd": str(self.wt)}), env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        self.assert_denied(p.stdout)

    def test_a_healthy_project_directory_does_not_hide_a_broken_payload_directory(self):
        self.break_worktree()
        code, out, _ = self.hook({"cwd": str(self.wt)}, env={"CLAUDE_PROJECT_DIR": str(self.root)})
        self.assertEqual(code, 0)
        self.assert_denied(out)

    def test_an_unreadable_payload_is_an_empty_one(self):
        self.break_worktree()
        self.env["CLAUDE_PROJECT_DIR"] = str(self.wt)
        code, out = self.hook_in_process("not json")
        self.assertEqual(code, 0)
        self.assert_denied(out)

    def test_a_healthy_worktree_and_a_directory_in_none_are_silent_and_start_nothing(self):
        for cwd in (self.wt, self.root, self.root.parent):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.hook({"cwd": str(cwd)}), (0, "", ""))
                self.assertEqual(self.hook_in_process(json.dumps({"cwd": str(cwd)})), (0, ""))
        self.assertFalse(self.home.exists())

    def test_the_denial_starts_no_process(self):
        self.break_worktree()
        code, out = self.hook_in_process(json.dumps({"cwd": str(self.wt)}))
        self.assertEqual(code, 0)
        self.assert_denied(out)


class HelperTests(GuardCase):
    def test_git_in_a_worktree_with_no_pointer_file_answers_from_the_main_checkout(self):
        self.break_worktree()
        top = self.git(self.wt, "rev-parse", "--show-toplevel")
        self.assertEqual(os.path.realpath(top.stdout.strip()), str(self.root))

    def test_a_broken_pointer_file_makes_git_fail_and_is_not_a_gone_worktree(self):
        (self.wt / ".git").write_text("garbage\n")
        p = self.git(self.wt, "status")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("invalid gitfile format", p.stderr)
        self.assertIsNone(factory.worktree_gone(self.wt))

    def test_every_worktree_on_the_path_is_checked(self):
        inner = self.wt / ".claude" / "worktrees" / "agent-1"
        inner.mkdir(parents=True)
        self.assertEqual(factory.worktree_gone(inner / "x"), line("agent-1", inner, self.wt))
        self.break_worktree()
        self.assertEqual(factory.worktree_gone(inner / "x"), line("t1", self.wt, self.root))
        self.assertIsNone(factory.worktree_gone(self.root / "sub"))

    def test_with_no_repository_above_there_is_none_to_name(self):
        path = self.root.parent / "plain" / ".claude" / "worktrees" / "w"
        path.mkdir(parents=True)
        with mock.patch("os.path.lexists", lambda p: p.startswith(str(self.root)) and os.path.exists(p)):
            self.assertEqual(factory.worktree_gone(path), line("w", path, "no repository"))


def refusal(what, instead):
    return f"factory: refused: {what} prints a credential into this session's transcript. Instead: {instead}."


TOKEN_LINE = refusal("`gh auth token`", "gh auth status")
SHOW_LINE = refusal("`gh auth status` with `--show-token`", "gh auth status")
ENV_LINE = refusal("`factory env`", "factory status (its `acting on GitHub as` line)")
GIT_LINE = refusal("`git credential fill`", "git config --get-all credential.helper (names the helper)")
SECURITY_LINE = refusal("a `security` keychain read", "security find-generic-password without -w or -g (names the item)")
IDENTITY_LINE = refusal("a file under the store's `identity/`", "factory status")
WHOLE_LINE = refusal("the whole environment", '[ -n "$NAME" ] && echo set')
SECRET_LINE = refusal("a secret variable", '[ -n "$NAME" ] && echo set')

REFUSED = [
    ("gh auth token", TOKEN_LINE), ("gh auth token --hostname x", TOKEN_LINE), ("cd x && gh auth token", TOKEN_LINE),
    ("echo $(gh auth token)", TOKEN_LINE), ("GH_HOST=x gh auth token", TOKEN_LINE),
    ("gh auth status --show-token", SHOW_LINE), ("gh auth status -t", SHOW_LINE), ("gh auth status -h x -t", SHOW_LINE),
    ("gh auth status -at", SHOW_LINE),
    ("factory env", ENV_LINE), ("/opt/x/bin/factory env", ENV_LINE), ('eval "$(factory env)"', ENV_LINE),
    ("git credential fill", GIT_LINE), ("git -C /x credential fill", GIT_LINE), ("git -c a=b --no-pager credential fill", GIT_LINE),
    ("gh auth status --show-token=true", SHOW_LINE), ("git credential-osxkeychain get", GIT_LINE), ("git credential osxkeychain get", GIT_LINE),
    ("security find-generic-password -s x -w", SECURITY_LINE), ("security find-internet-password -g -s x", SECURITY_LINE),
    ("security dump-keychain", SECURITY_LINE),
    ("cat $FACTORY_HOME/identity/key.pem", IDENTITY_LINE), ("cat ${FACTORY_HOME}/identity/key.pem", IDENTITY_LINE),
    ("cat ~/.claude/factory/identity/key.pem", IDENTITY_LINE),
    ("printenv", WHOLE_LINE), ("env", WHOLE_LINE), ("env | sort", WHOLE_LINE), ("env > f", WHOLE_LINE), ("ls; env", WHOLE_LINE),
    ("export -p", WHOLE_LINE),
    ("printenv GH_TOKEN", SECRET_LINE), ("echo $GH_TOKEN", SECRET_LINE), ('echo "${GITHUB_TOKEN}"', SECRET_LINE),
    ("printf %s $ANTHROPIC_API_KEY", SECRET_LINE), ("echo $GH_ENTERPRISE_TOKEN | cat", SECRET_LINE),
    ("echo $ANTHROPIC_AUTH_TOKEN", SECRET_LINE), ("echo $CLAUDE_CODE_OAUTH_TOKEN", SECRET_LINE), ("echo $DB_PASSWORD", SECRET_LINE),
    ("echo $X_SECRET", SECRET_LINE), ("echo $DB_PASSWD", SECRET_LINE),
]
PASSES = [
    "gh pr list", "gh auth status", "gh auth status --hostname github.com", "gh api user --jq .login", "git status",
    "git push -u origin HEAD", 'git commit -m "refuse gh auth token and factory env"', 'grep -rn "gh auth token" docs',
    "env FOO=1 make test", "printenv HOME", 'echo "$HOME"', "echo ${#GH_TOKEN}", '[ -n "$GH_TOKEN" ] && echo set',
    'curl -H "Authorization: Bearer $GH_TOKEN" https://example.invalid', "factory status", "security find-generic-password -s x",
]


class CredentialTests(GuardCase):
    def bash(self, command, **extra):
        return self.hook({"cwd": str(self.root), "tool_name": "Bash", "tool_input": {"command": command}, **extra})

    def denied(self, result, want):
        code, out, err = result
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                                   "permissionDecisionReason": want}})

    def test_every_printer_is_denied_with_its_line(self):
        for command, want in REFUSED:
            with self.subTest(command=command):
                self.denied(self.bash(command), want)

    def test_a_subagent_is_denied_too(self):
        for command, want in REFUSED[::7]:
            with self.subTest(command=command):
                self.denied(self.bash(command, agent_id="a1", agent_type="coder"), want)

    def test_every_other_call_passes_silently(self):
        for command in PASSES:
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), (0, "", ""))
        self.assertEqual(self.hook({"cwd": str(self.root), "tool_name": "Bash"}), (0, "", ""))
        self.assertEqual(self.hook({"cwd": str(self.root), "tool_name": "Bash", "tool_input": {"command": 5}}), (0, "", ""))

    def test_a_session_that_is_not_the_factorys_is_not_denied_and_reads_no_store(self):
        del self.env["FACTORY_SESSION"]
        self.assertEqual(self.bash("gh auth token"), (0, "", ""))
        self.assertFalse(self.home.exists())

    def test_the_session_of_a_declared_task_is_denied_without_FACTORY_SESSION(self):
        del self.env["FACTORY_SESSION"]
        self.home.mkdir()
        (self.home / "tasks.jsonl").write_text(json.dumps(
            {"ts": "2026-11-19T10:00:00Z", "repo": str(self.root), "slug": "t1", "state": "working"}) + "\n")
        self.denied(self.hook({"cwd": str(self.wt), "tool_name": "Bash", "tool_input": {"command": "gh auth token"}}), TOKEN_LINE)

    def test_an_ordinary_call_starts_no_process_and_reads_no_store(self):
        payload = json.dumps({"cwd": str(self.root), "tool_name": "Bash", "tool_input": {"command": "gh pr list"}})
        del self.env["FACTORY_SESSION"]
        with mock.patch.object(factory, "read_tasks", side_effect=AssertionError), \
                mock.patch.object(factory, "read_registration", side_effect=AssertionError):
            self.assertEqual(self.hook_in_process(payload), (0, ""))

    def test_blank_lines_are_decided_quickly(self):
        start = time.monotonic()
        self.assertEqual(self.bash("ls" + "\n" * 20000 + "ls"), (0, "", ""))
        self.assertLess(time.monotonic() - start, 1)

    def test_a_read_outside_identity_from_a_broken_worktree_passes(self):
        self.break_worktree()
        self.assertEqual(self.hook({"cwd": str(self.wt), "tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}), (0, "", ""))

    def test_a_read_of_the_identity_directory_is_denied_and_any_other_read_passes_directory_is_denied_and_any_other_read_passes(self):
        read = lambda path: self.hook({"cwd": str(self.root), "tool_name": "Read", "tool_input": {"file_path": str(path)}})
        self.denied(read(self.home / "identity" / "key.pem"), IDENTITY_LINE)
        self.denied(read(self.home / "x" / ".." / "identity" / "key.pem"), IDENTITY_LINE)
        self.assertEqual(read(self.home / "settings.toml"), (0, "", ""))
        self.assertEqual(read(self.home / "identityx" / "a"), (0, "", ""))
        self.assertEqual(self.hook({"cwd": str(self.root), "tool_name": "Read"}), (0, "", ""))

    def test_a_broken_worktree_gives_the_worktree_line(self):
        self.break_worktree()
        code, out, err = self.hook({"cwd": str(self.wt), "tool_name": "Bash", "tool_input": {"command": "gh auth token"}})
        self.assertEqual((code, err), (0, ""))
        self.assert_denied(out)


class RunTests(GuardCase):
    def setUp(self):
        super().setUp()
        self.break_worktree()

    def test_run_and_run_in_worktree_refuse_without_starting_a_process(self):
        repo = factory.Repo(str(self.root))
        (self.wt / "sub").mkdir()
        with mock.patch.object(factory.subprocess, "Popen", side_effect=AssertionError):
            self.assertEqual(factory.run(["git", "stash"], cwd=self.wt), (1, "", self.want))
            self.assertEqual(factory.run(["git", "stash"], cwd=str(self.wt / "sub")), (1, "", self.want))
            self.assertEqual(repo.git("stash", path=str(self.wt)), (1, "", self.want))
            self.assertEqual(factory.run_in_worktree(repo, str(self.wt), ["claude", "-p"]), (1, "", self.want))

    def test_a_stash_from_the_broken_worktree_leaves_the_main_checkout_as_it_was(self):
        with mock.patch.dict(os.environ, self.env):
            factory.run(["git", "stash"], cwd=self.wt)
        self.assertEqual(self.main_state(), self.main_before)
        self.assertNotEqual(self.main_before[0], "")

    def test_no_cwd_and_other_directories_run(self):
        self.assertEqual(factory.run(["true"])[0], 0)
        self.assertEqual(factory.run(["true"], cwd=self.root)[0], 0)


if __name__ == "__main__":
    unittest.main()
