import json
import os
import unittest
from unittest import mock

from test_factory import DISPATCHER, ROOT, WT, factory
from test_health import Home, SchedHarness, register


def running(sid, name, **kw):
    return {"sessionId": sid, "name": name, "cwd": "/elsewhere", "kind": "interactive", "pid": 7, "status": "busy", **kw}


OLD = DISPATCHER | {"name": "dispatcher-repo"}


class ResolverTests(Home):
    def find(self, sessions, fallback=None):
        return factory.find_dispatcher(sessions, ROOT, fallback)

    def test_the_registered_session_wins_in_any_directory_whatever_its_name(self):
        register("sid-r")
        mine = running("sid-r", "anything")
        self.assertEqual(self.find([OLD, mine]), (mine, None))

    def test_a_rename_changes_nothing_but_the_name_returned(self):
        register("sid-r")
        self.assertEqual(self.find([running("sid-r", "first")])[0]["name"], "first")
        self.assertEqual(self.find([running("sid-r", "second")])[0]["name"], "second")

    def test_an_idle_background_session_with_a_process_is_present(self):
        register("sid-r")
        idle = running("sid-r", "bg", kind="background", status="idle")
        self.assertEqual(self.find([idle]), (idle, None))

    def test_a_registered_session_that_is_gone_falls_back_to_the_old_style_name(self):
        register("sid-r")
        exited = {"sessionId": "sid-r", "name": "anything", "kind": "background", "state": "done", "cwd": ROOT}
        self.assertEqual(self.find([exited])[1], f"no dispatcher session for {ROOT}: run /factory:dispatch in a session there")
        self.assertEqual(self.find([exited, OLD]), (OLD, None))

    def test_old_style_needs_exactly_one_session_named_for_the_repository_inside_it(self):
        self.assertEqual(self.find([OLD]), (OLD, None))
        self.assertEqual(self.find([OLD, OLD | {"sessionId": "sid-2"}]), (None, "2 sessions named dispatcher-repo"))
        self.assertEqual(self.find([OLD | {"cwd": "/elsewhere"}])[0], None)
        self.assertEqual(self.find([OLD | {"name": "boss"}], "boss")[0]["name"], "boss")

    def test_a_name_that_another_running_session_carries_is_refused(self):
        register("sid-r")
        twin = running("sid-2", "same")
        self.assertEqual(self.find([running("sid-r", "same"), twin]), (None, "2 sessions are named same: rename one"))

    def test_a_session_without_a_name_is_refused(self):
        register("sid-r")
        self.assertEqual(self.find([running("sid-r", None)]), (None, "the dispatcher session sid-r has no name yet"))

    def test_a_registration_of_another_repository_or_a_broken_file_counts_for_nothing(self):
        factory.repo_state(ROOT, "dispatcher", "json").write_text(json.dumps({"root": "/other", "session": "sid-r"}))
        self.assertIsNone(self.find([running("sid-r", "anything")])[0])
        factory.repo_state(ROOT, "dispatcher", "json").write_text("not json")
        self.assertIsNone(self.find([running("sid-r", "anything")])[0])


class CommandTests(SchedHarness):
    def run_in(self, cwd, *argv, **kw):
        with mock.patch.object(os, "getcwd", return_value=cwd):
            return self.cli(*argv, **kw)

    def registered(self):
        return json.loads(factory.repo_state(ROOT, "dispatcher", "json").read_text())

    def test_register_writes_the_file_installs_the_job_and_says_who(self):
        me = running("sid-d", "my session", cwd=ROOT)
        code, out, err = self.run_in(ROOT, "dispatcher", "register", sessions=[me])
        self.assertEqual((code, out, err), (0, f"dispatcher of {ROOT}: this session (my session)\n", ""))
        self.assertEqual({k: v for k, v in self.registered().items() if k != "since"}, {"root": ROOT, "session": "sid-d"})
        self.assertTrue(self.plist().exists())

    def test_register_again_in_the_same_session_changes_nothing(self):
        me = running("sid-d", "my session", cwd=ROOT)
        register("sid-d")
        before = self.registered()
        code, out, _ = self.run_in(ROOT, "dispatcher", "register", sessions=[me])
        self.assertEqual((code, out), (0, f"dispatcher of {ROOT}: this session (my session)\n"))
        self.assertEqual((self.registered(), before["since"]), (before, "t"))

    def test_register_takes_over_from_a_running_dispatcher_and_names_it(self):
        register("sid-old")
        me, old = running("sid-d", "mine", cwd=ROOT), running("sid-old", "the old one", cwd=ROOT)
        code, out, _ = self.run_in(ROOT, "dispatcher", "register", sessions=[me, old])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[1], "took over from the old one (sid-old): send it one message that it is "
                                             "no longer the dispatcher of repo")
        self.assertEqual(self.registered()["session"], "sid-d")

    def test_register_says_nothing_of_a_previous_session_that_is_gone(self):
        register("sid-old")
        code, out, _ = self.run_in(ROOT, "dispatcher", "register", sessions=[running("sid-d", "mine", cwd=ROOT)])
        self.assertEqual((code, len(out.splitlines())), (0, 1))

    def test_register_refuses_a_task_worktree_and_a_missing_session_id(self):
        me = running("sid-d", "mine", cwd=ROOT)
        code, _, err = self.run_in(f"{WT}/x", "dispatcher", "register", sessions=[me])
        self.assertEqual(code, 2)
        self.assertIn(f"run it from the main checkout, {ROOT}", err)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            code, _, err = self.run_in(ROOT, "dispatcher", "register", sessions=[me])
        self.assertEqual(code, 2)
        self.assertIn("CLAUDE_CODE_SESSION_ID is not set", err)
        self.assertFalse(factory.repo_state(ROOT, "dispatcher", "json").exists())

    def test_register_reports_a_name_it_cannot_address_and_exits_1(self):
        me, twin = running("sid-d", "same", cwd=ROOT), running("sid-2", "same")
        code, out, err = self.run_in(ROOT, "dispatcher", "register", sessions=[me, twin])
        self.assertEqual(code, 1)
        self.assertIn("2 sessions are named same: rename one", err)
        self.assertEqual(self.registered()["session"], "sid-d")

    def test_dispatcher_prints_the_current_name_from_the_main_checkout_and_a_worktree(self):
        register("sid-r")
        for cwd in (ROOT, f"{WT}/x"):
            code, out, _ = self.run_in(cwd, "dispatcher", sessions=[running("sid-r", "now called")])
            self.assertEqual((code, out), (0, "now called\n"))

    def test_dispatcher_json_says_whether_it_was_registered(self):
        register("sid-r")
        _, out, _ = self.run_in(ROOT, "dispatcher", "--json", sessions=[running("sid-r", "n")])
        self.assertEqual(json.loads(out), {"root": ROOT, "session": "sid-r", "name": "n", "registered": True})
        _, out, _ = self.run_in(ROOT, "dispatcher", "--json", sessions=[OLD])
        self.assertEqual(json.loads(out), {"root": ROOT, "session": "sid-d", "name": "dispatcher-repo",
                                           "registered": False})

    def test_dispatcher_with_none_exits_1_with_the_reason(self):
        code, out, err = self.run_in(ROOT, "dispatcher", sessions=[])
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"no dispatcher session for {ROOT}", err)


if __name__ == "__main__":
    unittest.main()
