# Flaky tests

A test that fails without a change to what it tests is a defect in the test or the script, never a reason to rerun.

## Rules

- The fake clock of the factory tests is patched on the script's module, `factory` (`factory_core`), never on `time`. `factory.time` is the standard library's `time` module: patching its `sleep` or `time` also reaches `subprocess`, whose wait loop then spins and moves the fake clock by whatever the machine's load makes it.
- Go modules are fetched once, before the tests, by the Makefile (`make modules`); the fetch is retried, and when it still fails the message says it is a network failure and not the code. `tests/test_host_e2e.py` runs its `make install` with `GOPROXY=off`, so a module that was not downloaded is a plain local error.
- `tests/test_host_e2e.py` reaches the network only in the image build: the base image pull (no retry from this repository), and `apt-get` and the binary downloads (retried). Its `git clone` runs the path of `host add` against a local bare copy of this checkout, set with git's `insteadOf` in the container.
- A test waits for the event it needs, not for a time.

## Reproducing the clock flake

`PYTHONPATH=factory/tests python3 factory/tests/late_exit.py test_collision` makes every child process exit late. It fails when the fake clock reaches the standard library.

## CI history

Every run of `test.yml` created from 2026-11-14 to 2026-11-16 (365 runs, 7 with a second attempt; the 1000 limit was not reached). A job is flaky when (a) an earlier attempt failed and a later attempt passed on the same commit, or (b) it failed on a commit whose pull request touches neither the failing test nor the code it exercises. The 32 cancelled runs and 2 unfinished ones had no failed job.

| Job | Test or step | Seen | First seen | Cause | Rule | How it is dealt with |
|---|---|---|---|---|---|---|
| test | `test_a_told_that_names_no_task_in_the_firing_is_not_a_decision` | 3 (43870829205, 43875627358, 43879544833) | 43870829205, 2026-11-16 | The fake clock patched on `time` reached `time.sleep` inside `subprocess`; a late child exit moved it by 40 to 60 minutes and a second pass recorded a decision | (a) | Fixed: the clock is patched on `factory`; `late_exit.py` reproduces it |
| test | `test_the_whole_life_of_a_host` | 1 (43879544833) | 2026-11-16 | `go` could not read a module from the module proxy during `make install` (`stream error`) | (a) | Fixed: modules fetched once, retried, the test runs with `GOPROXY=off` |
| container | `make install` in the container | 1 (43877474735) | 2026-11-16 | The same module proxy read error | (b): the pull request changed no Go file, module file or Makefile | Fetch retried, failure says network |
| container | image build, binary download | 1 (43862670528) | 2026-11-16 | `curl: (28)` connecting to the download host timed out | (a) | Download retried, failure says network |
| container | `TestServerIsRestartedWhenItDies` | 3 (43629359171, 43636253798, 43639149848) | 43629359171, 2026-11-14 | The container's crontab sends `pkill -USR2 -x supercronic`, which also hit the test's stub; the stub died and the start script ended | (a) for 43639149848, (b) for 43636253798; 43629359171 has the same symptom, its pull request changed the test | Fixed in 5947afb9: the stubs ignore USR2 |
| test | `TestAttachFollowsEveryWindowChangeAndTheDashboardComesBackAtTheLastSize` | 1 (43615514332) | 2026-11-14 | The test sampled the rule widths instead of waiting for them | (a) | Fixed in 83722c81: it waits |
| test | `TestAttachEndsWithItsConnection` | 1 (43757582587) | 2026-11-15 | Not found: it waits 10 s for the hangup file under `-race` on a loaded runner | (b) | Open: read the test; 30 local `go test -race -cpu 1` runs passed; if it recurs, record the stub's state on timeout as `start_test.go` does |
| container | `TestServeAcceptedKeyGetsADashboard` | 1 (43605603110) | 2026-11-14 | Probably the test reads the session's output right after the exit status, before the output copy ends; not reproduced | (a) | Open: read the test; 30 local `go test -race -cpu 1` runs passed; no fix |
| test, container | `test_starts_the_missing_ones_and_not_the_running_ones`, stderr `Terminated` | 4 (43616483220 and 43616842170, each in both jobs) | 43616483220, 2026-11-14 | The shell prints a notice when it ends a background job; whether it does depends on timing | (b) | Fixed in 734b0e79: the test allows the notice |
| test | the same test, the start script timed out | 1 (43624577123) | 2026-11-14 | A TERM sent to a just-forked child is lost in `dash` and the script waits for ever | (b) | Fixed in 2bcb2e39 and a61871b1: TERM is sent until the child is gone |
| test (local) | `test_a_detached_process_is_named_killed_and_fails_the_run` | 2 local runs of tests/ in 47 (seeds 240081320, 134032205) | 2026-11-16 | Bug in `tools/noleft.py`: `scan()` kept a process's command from the first `ps` table that showed it, so a child seen between fork and exec was reported under its parent's command line | local | Fixed in fa393347: the report takes the command from the final `ps` table; the test's child is a shell that execs the sleep after 1 s, which fails without the fix every time |
| test (local) | `test_a_first_signal_after_the_command_ended_still_reports_and_kills` | 1 local `make test` in 20, on 63a8671e | 2026-11-16 | Bug in `tools/noleft.py`: the signal handler raised `Stopped`, which only the wait for the command caught; a signal after it returned ended the wrapper with a traceback, exit 1 and empty stdout, with no report and no kill; not reproduced naturally (480 test runs, 400 wrapper runs), its mechanism shown by injecting the signal there | local | Fixed in fa393347: the handler only records the signal, so none can end the wrapper early; `test_a_signal_before_the_command_started_still_reports` covers the earliest window |

Failures that are not flaky: `TestGoldenKaizen` (43607048022, 43607538314: the pull request changed the golden's code, the failure repeated), `sessions_in()` argument error (43582818192) and the watch walks (43586287661, a pull request's own new code), `test_tool_kinds_and_dedupe` (43595384980, the pull request's own new test, fixed in the same pull request), `test_the_whole_life_of_a_host` with the dispatcher not found (43617216732: the pull request changed that lookup), `test_the_script_on_sighup_ends_its_group_children` (43624577123: the pull request changed that test; the same signal-before-child cause as the row above).

The collision test first failed in run 43870829205 (2026-11-16 12:54 UTC), 49 runs after it first ran in CI (2026-11-15 16:45 UTC). It failed in 3 of 60 test-job attempts. Before the merge of da14056, 2 of 52; after it, 1 of 8. Around 9ffe165 and 4b0bc37 the runs after are 6 and 4 attempts with 1 failure: not enough runs to tell whether any of those merges changed its rate.
