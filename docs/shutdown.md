# What ends each process, and what is left

An audit of everything isshoni starts or opens, in three parts: the factory script ([docs/factory.md](factory.md#shutdown-and-leftovers)), the dashboard ([docs/isshoni.md](isshoni.md#shutdown)), and this page: the monitor, the session cache, the host commands, the recording tools, the container's start script and the test suites. Each row says how it was learned: measured (on macOS, with a script that starts only its own processes and counts survivors with `ps`) or read (from the code; not run).

## Monitor (`bin/isshoni`)

Starts: `gh pr list` (`events`, 20 s timeout) and `gh` pages (`kaizen`, 10 s a page). `events --follow` is the only command that lives on: it ends when its stop flag is set, or at a write to a closed pipe, or (since this audit) when its parent is gone.

| Event | What happens | How learned |
|---|---|---|
| Normal exit, error | The command returns; its `gh` call has ended (own timeout). | read |
| SIGINT | `events --follow` exits 0. Other commands: Python's `KeyboardInterrupt`; a running `gh` child is killed by `subprocess.run`. | measured (follow), read |
| SIGTERM | `events --follow` exits 0 through its handler. Other commands die at once; a running `gh` child is not signalled and ends at its own timeout. | measured (follow), read |
| SIGHUP | The process dies (default action); `events --follow` has a child only while its `gh pr list` runs (every 60 s unless `--no-gh`, bounded by its 20 s timeout). A running `gh` child is not signalled. | measured (follow), read |
| Parent killed with SIGKILL | `events --follow` checks `os.getppid()` every tick (0.5 s) and ends. Before: it lived until the next event was written (still running 5 s later). After: gone 0.11 s later. | measured |
| Reader closes the pipe | Ends at its next write; the first write (the `live` marker) comes at once. | measured |
| Hung network | `gh` ends at 20 s (`events`) or 10 s a page (`kaizen`). | read |
| Left on disk | The session cache (below) and nothing else. | read |

## Session cache

`~/.cache/isshoni/sessions/<digest>/` (or `$ISSHONI_CACHE_DIR`; empty turns it off): one pickle per transcript, written to a temporary file and renamed. Opens no process.

| Event | What happens | How learned |
|---|---|---|
| Any end of the process | An interrupted write leaves a temporary file in the directory; it is removed by the next run's `prune` (every file that is not an entry of the current transcripts). | read |
| Left on disk | One directory per projects dir, pruned per projects dir only: a projects dir that is never used again keeps its directory. On this machine: 1 directory, 52160 kB (`du -sk`). | measured |

## Host commands (`isshoni host add|remove|status`)

Starts: one `ssh` per remote step (every one goes through `host.ssh`), `git` on this machine, `ssh -G` to find the public key.

| Event | What happens | How learned |
|---|---|---|
| Normal exit, error | Each child has been waited for. | read |
| SIGINT | `subprocess.run` kills its direct child (`ssh`) and re-raises. | read |
| SIGTERM, SIGHUP | The process dies; the running `ssh` is not signalled and ends when the connection or the remote command ends. | read |
| Hung network | Every `ssh` call carries `ConnectTimeout=10`, `ServerAliveInterval=15`, `ServerAliveCountMax=3`: a connection that goes quiet ends after about 45 s (three missed 15 s probes; `ConnectTimeout` applies only while connecting). No overall limit: a remote clone may be long. | tested with a fake `ssh` that records its options |
| Local `git` and `ssh -G` hang | Each ends at 30 s and the command stops with a sentence naming it. | tested |
| Left on disk | `~/.ssh/config` block and `hosts.json` by design (`host remove` takes them out). Nothing temporary. | read |

## Recording and screenshot tools (`tools/record.py`, `tools/strip.py`, `make screenshot`, `tools/walk.sh`)

Start: headless Chrome (`tools/strip.py`, 120 s timeout), `bin/isshoni-tui` and `bin/isshoni` (120 s), `git` (30 s). A timeout or a failing child stops the tool with a sentence that names the command. `make screenshot` runs Chrome from the Makefile.

| Event | What happens | How learned |
|---|---|---|
| Normal exit, error | Chrome has exited; the `isshoni-record-*` temporary directory is removed by `finally`. | read |
| SIGINT | `finally` runs; the temporary directory goes. `subprocess.run` kills its direct child. | read |
| SIGTERM, SIGHUP | The process dies without `finally`: the temporary directory stays and a running child ends by itself (Chrome's `--screenshot` exits after the page loads). | read |
| Hung child | Killed at its timeout. | tested |
| `make screenshot` run | 0 processes left (`tools/noleft.py`). 0 new entries in `$TMPDIR`; it already held 466 `*Chrome*` entries from earlier runs, whose origin was not determined. | measured |
| Left on disk | `shots/` and `assets/tabs/` by design. | read |

`tools/walk.sh` installs a real per-user scheduled job and refuses to run outside CI. Its `cleanup` removes the job and its temporary directory; it now runs on EXIT, HUP, INT and TERM (`exit 129`, `130`, `143`). Under `sh` (dash) only the EXIT trap used to run, so an interrupt left the job installed. **Not run here**: the trap lines are checked with `sh -n` only.

## Container start script (`docker/start`)

Starts `supercronic` and, when a key and the dashboard exist, a loop that runs `isshoni-tui --serve` and restarts it 2 s after it ends. Both are children of the script. A third, `revive &`, runs in the background when the same container restarts: `stop()` ends it too when the dashboard runs (without one the script is replaced by `supercronic` and nothing stops `revive`), and inside it `claude agents --json` and `claude --bg` each get 30 s.

| Event | What happens | How learned |
|---|---|---|
| SIGTERM, SIGINT | The script stops the loop, cron and `revive`, waits for each (`end`), exits 0. `end` sends TERM every 0.1 s until the child is gone, because a TERM that reaches a child the shell has just forked is lost in dash. | `docker/start_test.go` (`noStubLeft`: no recorded process survives) |
| The server dies | Restarted after 2 s; the loop is not started again once a stop was asked for. | `docker/start_test.go` |
| Left on disk | `TMPDIR=/state/tmp` is never emptied. Not measured: no container on this machine. | read |

## Test suites

`make test` and `make test-factory` run each suite (`unittest` for `tests/`, `unittest` for `factory/tests/`, `go test ./...`) through `tools/noleft.py`.

| Measure | Result |
|---|---|
| Processes left, at the base commit | `tests/`: 0, `factory/tests/`: 0, `go test`: 0 |
| Processes left now | 0, 0, 0 |
| Entries left in a fresh `$TMPDIR` after all three suites | 0 |

The count was already 0, so no test needed a fix for a leftover process. Two things changed in the tests: the `Popen` in `tests/test_events.py` is killed and reaped in cleanup, and every `subprocess.run` that runs `bin/isshoni`, the factory script, a shell, `git`, `docker` or `ssh-keygen` has a timeout.

## What belongs to Claude Code

These are left by Claude Code, not by a script of this repository. The factory only reports them.

- **The lock on a worktree.** A background session locks its worktree (`git worktree list --porcelain`: `locked claude session <name> (pid N ...)`). The lock stays when the process has exited. `factory status` then prints `if nothing uses it, run: git worktree unlock <path>`; `factory clean` keeps a locked worktree.
- **The session's state directory.** `~/.claude/jobs/<id>` (`state.json`, `timeline.jsonl`, `tmp/`) is removed by `claude rm`. While a process the session started still writes to `tmp/`, the removal fails with `ENOTEMPTY`. `factory clean` calls `claude rm <session>` and prints a failure; it does not retry. `factory health` counts finished job directories older than 24 h. On this machine: 17 directories, 555052 kB (`du -sk ~/.claude/jobs`, live sessions included).
- **Subagent worktrees.** A subagent started with worktree isolation gets `.claude/worktrees/agent-<id>`; Claude Code removes it only when it is unchanged. The factory does not list these as tasks, rejects task slugs that start with `agent-`, and `factory health` counts those older than 24 h. Removing one is by hand: `git worktree remove <path>`, then `git branch -d <branch>`.
- **A busy session.** A session with a live background command counts as busy: `factory clean` and the lander's merge refuse it. The site manager's row for the task (`children`) names the process.

## How it is kept true

- **The wrapper.** `tools/noleft.py <command...>` runs a command, then lists every process that descended from it and is still alive, kills those (they are the command's own), prints `left behind: N`, and exits 1 when N is above 0. A process is found by walking the process table every 0.1 s (`ps -axo pid=,ppid=,lstart=,command=`), not by an environment token: macOS hides the environment of system binaries (`/bin/sleep`, `/bin/sh`) from `ps -E`, so a token would miss the most likely leftover. Limit: a process whose whole parent chain ends within 0.1 s of its start is missed. A second limit: where the test runner is PID 1 of a container without an init, zombies count as left behind; the project's container sets `init: true`. Linux uses the same code path (`ps` there takes the same options); it was verified on macOS only.
- **The watch criterion and the health lines** report what a script cannot prevent: a session that handed over but is still busy, old `agent-*` worktrees and old finished job directories ([docs/factory.md](factory.md#shutdown-and-leftovers)).
- **The rules.** `factory/agents/*.md` and `factory/roles/lead.md` say how to wait for a background command (through its completion notice, not a polling loop: `until ! pgrep -f name` matches its own command line and never ends; `factory/roles/lead.md` and `factory/agents/lander.md` do this, and the coder, the reviewer and the explorer now run commands in the foreground), that every background command is stopped before the report, that a test that starts a process kills its process group, and that the report names the `left behind` count.
