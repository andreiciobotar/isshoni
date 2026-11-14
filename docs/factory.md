# factory reference

The `factory` command of the plugin (Python 3.11+, standard library; needs `git`, `claude` and, for pull request state, `gh`). Run it inside a repository: the main checkout or any task worktree. Exit codes: 0 ok, 2 usage or precondition error (one line on stderr), 1 a `claude`, `git`, `gh` or scheduler step failed or a log could not be written.

## Install
In a clone of the repository, `make wizard` does every setup step (see Setup and uninstall); with only the plugin, run `factory wizard` after installing it. By hand, in Claude Code: `/plugin marketplace add andreiciobotar/isshoni`, then `/plugin install factory@factory`; `/rename dispatcher-<repository directory name>` and `/factory:dispatch` make the session the dispatcher. To try it for one session without installing, start `claude --plugin-dir <clone>/factory`. `gh` must be authenticated (`gh auth status`). macOS and Linux; Windows is not supported. Leads run unattended: nobody watches a lead's session (`lead_permission_mode`, see Settings).

## Tasks
A task is one slug: lowercase letters, digits and single hyphens, 3 to 40 characters, not starting with `agent-`. From it follow the worktree `.claude/worktrees/<slug>`, the branch `worktree-<slug>`, the lead's session name `<slug>`, and the spec `~/.claude/factory/specs/<repository directory name>/<slug>.md`. Specs are working papers and are never committed; they outlive the worktree.

A session belongs to a task when its `cwd` is in the task's worktree, or when it is the task's exited lead (a background session with no `pid`, named like the slug, with `cwd` at the main checkout). A task whose finished sessions are several is not held; only two or more exited sessions carrying its name at the main checkout are ("cannot tell which is its lead").

## Commands
```
factory new <slug> (--brief <text> | --brief-file <path>) [--dispatcher <session-name>] [--base <slug>]
factory status [--json]
factory clean [--dry-run]
factory state working | blocked | handed-over <deliverable>
factory ack <slug> acknowledged | dismissed
factory feedback [--json] [--status open|accepted|rejected|done] [--kind issue|decision] [--since <ts>]
factory feedback add --role <role> [--slug <slug>] [--model <model>]     # a report or items on stdin
factory feedback decide '<what>. Why: <why>'                             # 200 characters at most
factory feedback set <id> open|accepted|rejected|done [--pr <url>]       # id or a unique prefix
factory retro [<file>]                                                   # a JSON object, on stdin or in the file
factory log events|retros [--since <ts>] [--until <ts>]
factory hook                                                             # run by the plugin's hooks
factory health [--json] [--dispatcher <session-name>]
factory watch [--dispatcher <session-name>]
factory digest [--dispatcher <session-name>] [--dry-run]
factory schedule install|remove|status [--dispatcher <session-name>]
factory firing <id> acted|ignored                                        # id or a unique prefix
factory miss <text> [--slug <slug>] [--firing <id>]
factory firings [--json]
factory merge (<number>... | --all) [--dry-run]
factory wizard [--yes] [--list] [--owner <account>] [--name <app name>]
factory uninstall [--yes] [--list]
factory env                                                              # exports that make git and gh act as the App
factory land (<slug> | --all)
```

- **`new`** starts a lead: a background session in a new worktree, with `factory/roles/lead.md` appended to its system prompt, declared `working`. The calling session must be named `--dispatcher` (default `dispatcher-<main checkout's directory name>`; set it with `/rename`), and no other session may carry that name. It refuses a slug whose worktree, branch, session or pull request (in any state) already exists (when `gh` cannot say, it warns and goes on), and a repository at its cap. `--base` stacks the task on another open task's branch.
- **`status`** prints one row per task worktree: slug, phase, declared state, branch, session, base, session state, age (`overtime` when a session whose turn has not ended is past the time limit), uncommitted changes, commits ahead and behind its base, last commit, pull request, and `stale` when the last commit and the session's start are both older than 7 days. Then the counts per phase against the cap, the leftover `agent-*` and outside worktrees, one line saying who acts on GitHub (`acting on GitHub as <bot login> (the factory's App)`, with `; installed on ALL repositories: narrow it at <installation url>` appended when the installation covers all of them; or `acting on GitHub with your gh login` with the reason: no App identity, the App stored but not installed, this repository not on GitHub under the App's owner, and, instead of that choice, `acting on GitHub: the stored App cannot be read: <reason>; the factory's gh calls fail until it is fixed (factory wizard)` when `app.json` is corrupt or malformed), and one `waiting on the human: <slug> <deliverable>` line per task in review. Pull request state is `unknown` without `gh`. `status --json` also has `identity` (the bot login, or `null` when the factory does not act as the App here) and `repository_selection` (`all`, `selected` or `null`), and its rows also carry `waited` (seconds since the task's last declared record, `null` when undeclared) and, for a blocked task, `stop_kind` and `stop_summary` (the `kind` and `summary` of the last `QUESTION`, `BLOCKED` or `OVERTIME` stop its lead sent after declaring it; `null` when there is none, and `stop_summary` is `null` for stops logged before summaries existed).
- **`clean`** removes finished tasks (session, worktree, branch) and says why it kept the rest. It never forces and keeps a task whenever anything is uncertain. A task goes only when its pull request is merged into the default branch with the local head equal to the merged head, or its state is `acknowledged` or `dismissed` with a recorded deliverable, a pull request that is not open and a head that is pushed; and in both cases nothing is uncommitted, no task is stacked on it, no session in it is unfinished and no other worktree is nested in it. It keeps a task on a detached HEAD, and one whose worktree is locked with no session (it prints the `git worktree unlock` to run). If sessions cannot be listed, or an unfinished session has no `cwd`, it removes nothing (exit 2). A dismissed branch that is not merged makes `git branch -d` refuse: reported, exit 1. Git-ignored files in a removed worktree go with it.
- **`state`** is run by a lead inside its worktree. `handed-over` takes the deliverable: a link (`scheme://...`), or a file that exists and is outside the worktree.
- **`ack`** is run by the dispatcher on the human's word, for a task that was handed over.
- **`wizard`** creates the factory's GitHub App, gets it installed and verifies it; rerun, it verifies or repairs. See Identity.
- **`env`** prints `export` lines that make `git` and `gh` act as the App, or nothing (exit 0) when no installed identity is stored. See Identity.
- **`retro`** takes the task from the working directory and reads the JSON from `<file>` when one is given (exit 2 if it cannot be read), else from stdin.
- **`land`** lands a handed-over task whose base moved or merged, or whose pull request conflicts, without waking its lead (see Landing without the lead).

## Declared state, phases and the cap
| Declared state | By | Meaning |
|---|---|---|
| `working` | `new`; the lead when it resumes | being built |
| `blocked` | the lead, before `QUESTION:`, `BLOCKED:`, `OVERTIME:` | waiting on the human |
| `handed-over` | the lead, before `DONE:` and after any later turn | the deliverable is with the human |
| `acknowledged`, `dismissed` | the dispatcher (`ack`) | the human accepted or dropped it |

A session is unfinished if it is interactive, or a background session whose turn has not ended. In `claude agents --json --all` a turn has ended when `status` is `idle` (a session stays `busy` while a background command it started still runs), or when the entry has no `status` and no `pid` and its `state` is `done` or `stopped` (the process exited, or `claude stop`). The `state` of a live session (`done`, `blocked`, `working`) is its own summary of its last turn and is not read: a lead woken by a message after its hand-over often ends `idle` with `working`. Anything else, such as `waiting`, an unknown status or an exited session with another `state`, is unfinished.

| Phase | When | Holds a slot |
|---|---|---|
| `motion` | a session is unfinished in it, or sessions cannot be listed or placed; or declared `working`; or undeclared with no open or merged pull request | yes |
| `blocked` | declared `blocked`, no session unfinished | yes |
| `review` | no session unfinished; declared `handed-over` and not merged, or undeclared with an open pull request | no |
| `merged` | no session unfinished, pull request merged | no |
| `settled` | no session unfinished, `acknowledged` or `dismissed`, not merged | no |

The cap (`worktree_cap`) counts slots per repository. Only `new` checks it, so a lead that resumes is never stopped. Nothing bounds the number in review.

## Settings
`~/.claude/factory/settings.json`, every key optional; `factory wizard` writes it with these defaults when it is missing and, from a clone, sets `monitor`. A file that is not valid JSON, or an invalid value, is an error naming the file or key.

| Key | Default | |
|---|---|---|
| `worktree_cap` | 5 | tasks holding a slot, per repository; `isshoni-tui` can edit it |
| `lead_model` | `fable` | |
| `lead_permission_mode` | `auto` | `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan` |
| `task_time_limit_hours` | 2 | after it `status` shows `overtime` while the lead's turn has not ended; the lead stops and reports |
| `lgtm_from_author` | `true` | `true` or `false`: whether an `lgtm` by the pull request's author counts; set `false` once the factory has its own account (see Merging) |
| `monitor` | none | the path of the monitor's `isshoni` command; the cost criteria need it (see Criteria) |

## FACTORY_HOME
`FACTORY_HOME` moves everything the factory keeps under `~/.claude/factory`: the settings file, the logs and the outbox, the task state, `watch.log` and the `identity/` directory (App, key, cached tokens). Unset or empty it is `~/.claude/factory`; `~` is expanded; a relative value exits 2 with one line (a hook exits 0 and records nothing), because the hook and the schedule run in other directories. Messages that name the settings file print the real path. It does not move `~/.claude/projects` (Claude Code's transcripts), the scheduler's own files (`~/Library/LaunchAgents`, systemd units, crontab) or the spec directory the lead's rules name. The dashboard's settings editor always opens `~/.claude/factory/settings.json` (`FactorySettingsPath` in `internal/data/factory.go`), and `~/.claude/factory/deliverables/` named in the lead's rules is not moved either. The hook and `factory env` run in the session's environment and read the same variable.

`factory schedule install` with `FACTORY_HOME` set writes it into the job's environment beside `PATH` (launchd `EnvironmentVariables`, a systemd `Environment=` line, a variable before the command in cron) and hashes the home into the job's label, so a job installed under another home is a second job and never replaces the real one; it also leaves a legacy job without a hash alone. With the variable unset the label and the job are unchanged. It is meant for tests and exercises: a scratch home that cannot touch the real outbox, tasks or App.

## Messages
Leads and the dispatcher talk through Claude Code's `SendMessage`, by session name. A lead's first line starts with `QUESTION:`, `BLOCKED:`, `OVERTIME:`, `SPLIT:` or `DONE:` and its slug. The human can always `claude attach <slug>`.

## Subagents and landing
A lead works through four agents of the plugin (`factory/agents/`), each pinned to a model: `explorer` (Haiku) finds and counts, `coder` (Sonnet) implements and commits its round, `reviewer` (Opus) reviews a fixed commit range, `lander` (Sonnet) lands. The lead keeps the judgment: the spec, the design calls, what to make of a review, the quality assessment, the pull request's message and the sign-off.

The lander is given the task's base, the head the lead vouches for (the reviewed head) and two files the lead wrote beside the spec: `<slug>.pr.md` (the pull request body) and `<slug>.retro.json` (the retrospective). It fetches, rebases the branch onto its base when the base moved, resolves a conflict only when it is mechanical (independent neighbouring additions; generated files, which it regenerates), regenerates goldens when told to, runs the full tests, pushes (`--force-with-lease` after a rebase), and opens or updates the draft pull request. The same run serves the first landing and every later "your base moved". Its tools are Bash, Read, Edit, Grep and Glob. It never edits code or tests to get through, never drops a side of a conflict, never merges, and reports one of:

| result | means | then |
|---|---|---|
| `LANDED` | the reviewed commits were pushed unchanged (`git range-diff` identical), the base changed no file the task changes, no golden changed, tests pass | the lander has filed the retrospective and declared `handed-over`; the lead signs off on the report |
| `LANDED, LOOK` | pushed, but a conflict was resolved mechanically, or the base changed files the task changes, or a commit differs, or goldens were regenerated | nothing declared; the lead reads the flagged hunks, has them reviewed when needed, then files and declares itself |
| `STOPPED` (`dirty`, `conflict`, `golden`, `tests`, `denied`, `other`) | a decision is needed; nothing pushed (`denied` can follow a push). After `conflict` the rebase is aborted; after `golden` or `tests` the worktree stays on the rebased commits | the lead decides, then dispatches the lander again |

The lander refuses to start unless the worktree is clean and its head is the reviewed head, and refuses to push over commits on the remote branch that the reviewed head does not contain, unless the lead names that remote head as the one last pushed. When a stacked task's base has merged, the lead says so and the lander rebases onto the default branch and retargets the pull request.

`factory state` and `factory retro` take the task from the working directory, so a lander's declaration is the lead's (`role: lead`).

## Landing without the lead
`factory land <slug>` or `factory land --all` (exactly one) does the mechanics of `factory/agents/lander.md` for a task that was handed over, so the dispatcher does not wake its lead for "your base moved". Run it from the main checkout or any worktree. It prints one report in the lander's format: `## Result` (`LANDED <url>`, `LANDED, LOOK <url>` or `STOPPED <reason>`), `## Facts`, `## Needs the lead` (`none` for `LANDED`); with `--all` each report follows a `# <slug>` line, and `nothing to land` says there was none. Exit 0 when every result is `LANDED`, 1 otherwise, 2 for a task with no worktree or one not declared `handed-over` (no report). `--all` takes only the handed-over tasks whose open pull request GitHub reports `CONFLICTING`, or whose recorded base (`branch.<b>.factoryBase`) has merged while the pull request's base is not yet the default branch: a pull request that is merely behind is left alone, because any push makes an earlier approval stale (an `lgtm` comment older than the push, or a review on the old commit: the merge step then stops with `stale`) and the human would approve again for nothing. A named slug always lands.

It checks, in this order, before changing anything: (1) the task's sessions: they cannot be listed, one has no `cwd` and is unfinished, or one in the worktree is interactive or not finished is `STOPPED busy`, and nothing else is done or declared; (2) `git status --porcelain` not empty is `STOPPED dirty`; (3) after `git fetch`, a branch that is not `worktree-<slug>` or is the default branch, a pull request that is not open, or a local head that is not `<remote>/<branch>` and the pull request's head is `STOPPED other`. The reviewed head is taken to be the head that is at once local, pushed and on the open pull request.

The scripted path uses no model. The target is `<remote>/<recorded base>` while that base counts, else `<remote>/<default branch>` (a recorded base that merged: the pull request is retargeted). A target already in the head's history changes nothing but the pull request's base. Otherwise `git rebase <target>`; then `git range-diff --no-color` of the old against the new commits (identical only when the command succeeds, prints at least one line and every line is a pair marked `=`; anything else, including a diff body line, counts as a difference); `make test` in the worktree (`LAND_TEST_TIMEOUT`, 1800 s; the test command exists only when the first of `GNUmakefile`, `makefile`, `Makefile` has a `test:` rule, so a `test` directory does not count, and included files and rules naming several targets are not read; without one it is `STOPPED tests` ("no `test:` rule in <makefile>" or "no Makefile"), since nothing can say green; no other test command is known); the sessions again (this second check exists only on the scripted path: once a lander session has started, no further look is taken, and a lead woken meanwhile is excluded only by the leased push); `git push --force-with-lease=<branch>:<old head>`; the retarget; a pull request comment (`Rebased onto <target> at <sha> by factory land, without the task's lead: <old> -> <new>`, then range-diff, overlap, conflicts, regenerated files, tests and the result; never `lgtm`, and a `gh pr comment` is neither a review nor a comment whose whole body is `lgtm`, so it cannot count as an approval). It never runs a golden command or edits a file: a stale golden is a failing test.

When the rebase conflicts it is aborted (the worktree is back at the old head) and a short run of the lander starts in the worktree (`LAND_TIMEOUT`, 1800 s): `claude -p --agent factory:lander --plugin-dir <plugin> --model sonnet --permission-mode dontAsk --allowedTools Read Edit Grep Glob "Bash(git *)" "Bash(make *)" "Bash(gh pr view *)" "Bash(gh pr edit *)" "Bash(factory state working)" --output-format json <brief>`. The permission is fixed, not the setting `lead_permission_mode`: with nobody watching, `auto` let its classifier deny the leased push once. `dontAsk` denies everything not listed: every `gh` other than `pr view` and `pr edit` (checked: a session with this argv was refused `gh pr merge 1` and `gh pr comment 1 --body lgtm`, and the fake `gh` recorded neither), and every shell command that is not `git`, `make` or `factory state working` (the Edit tool edits files). `Bash(git *)` is not narrowed: that the lander pushes only the task's branch, and only with a lease, rests on its rules and on the script settling the outcome from the task branch on the remote, not on the allowlist. A denial is `STOPPED denied`. The pattern form `Bash(git *)` matched as written. A golden command that is not `make ...` is therefore a `denied` stop: a limit. The tool list is passed before `--output-format` because `--allowedTools` takes every following word. The brief gives the slug, the base and its remote, the old head as both reviewed and last pushed head, whether the base merged, no title, body or retrospective, no hand-over, the files the branch changes as the goldens it may regenerate, and that file-writing shell commands and other `gh` commands are denied. The script, not the model, settles the outcome from the remote: a branch head that moved is `LANDED, LOOK` (a conflict was resolved) whatever the model said, with the comment; one that did not is `STOPPED <the lander's reason>`, or `other` when the reply cannot be read, the run failed or timed out, or the lander said `LANDED` without pushing. The printed report is the lander's, with the settled result and without its `## Factory feedback` section. What the form was found to do (claude 2.1.325; `--output-format stream-json --verbose` shows it): `--agent factory:lander` with `--plugin-dir` applies the agent's prompt, its model (Sonnet, also without `--model`) and its tool list (the init message names `Bash`, `Read` and `Edit`; no `Write`); hooks of the run carry the payload's `agent_type`, so `tags()` records `role: "lander"` with no change to it; an `Edit` is refused under `~/.claude/` paths as a sensitive file (a scratch repository there cannot be edited), not under a repository's `.claude/worktrees/`; `ANTHROPIC_LOG=debug` makes `claude` log on standard output, so `run` leaves it out of the run's environment.

| Result | When | State after |
|---|---|---|
| `LANDED` | the base did not move, or moved with an empty overlap; the range-diff is identical; tests pass; pushed; the pull request is open against the target | nothing declared: `handed-over` stays |
| `LANDED, LOOK` | pushed, but the base changed a file the task changes, or a commit differs, or the lander resolved a conflict | `working` (`role: lander`) |
| `STOPPED busy` | see (1) above | nothing declared |
| `STOPPED busy` | a lead that turned busy at the second check: the worktree is on the rebased commits, unpushed | `working` |
| `STOPPED dirty`, `other`, `tests`; `conflict`, `golden`, `denied` (from the lander) | as above; the last 40 lines of a failing or timed-out `make test` (the worktree stays on the rebased commits, unpushed); a failed fetch, abort, lease or `gh` step; the hunks, golden diff or denied step the lander reports | `working` |

A branch that holds a merge commit (`git rev-list --merges <old base>..HEAD` prints something) is `STOPPED other` before any rebase, since a rebase would drop the merge's own content. A rebase that fails without a `CONFLICT` line (a hook, say) is aborted and is `STOPPED other` with git's output, with no model run. `working` is declared before the push of a `LANDED, LOOK`, before the lander session starts and at a busy second check, so a kill or crash in between cannot leave the task looking handed over. After a lander run that timed out or exited non-zero the report says so and reads HEAD, whether anything is uncommitted and whether a rebase is in progress from git; a failing fetch after the run is `STOPPED other`. `run` kills only the direct child: a command the lander had in flight can outlive a timeout. `STOPPED other` can follow a push (the retarget or the comment failed; the comment is posted even when the retarget failed, and both errors are reported): the report then says `pushed`. One task takes up to about 30 minutes (the tests or one lander session, never both); `--all` takes the tasks one after another; so the dispatcher starts it in the background.

A `working` task is not mergeable (`handover` stop) and holds a slot until its lead has looked and declared `handed-over` again. The dispatcher messages the lead on `LANDED, LOOK` and on every `STOPPED` other than a first-check `busy`, including the busy that left rebased, unpushed commits, pasting the report; on a first-check `busy` it does nothing and runs `land` again when the lead's turn has ended. A lead that gets such a report declares `working`, acts on it as on a lander's report and declares the hand-over again.

Who acts: with an App installed (see Identity), the script's `gh` calls (the pull request read, the retarget, the comment) get the App's token through `run`, as the merge step's do, and its `git fetch`, `git rebase` and `git push` run with the exports of `factory env` (the App's token for git, its name and email as author and committer; a failed mint stops the land at its first fetch with `STOPPED other`, "no App token", before anything is fetched, rebased or pushed). The lander session of a conflict gets the same exports from the `SessionStart` hook, which `claude -p --agent factory:lander` fires like any session in a task worktree (checked with a stand-in for `factory env`: `git config` in that session saw its exports, with no denial under the allowlist). So the push, the comment and the committer of the rebased commits are the bot (their author stays); with no App they come from your login. A fresh approval is still the human's.

Limits. Busy is told from the session list at two moments: when it starts and after the tests; a lead woken after the second look is not excluded, and only the leased push keeps the remote safe. Listing sessions failing counts as busy. The task's pull request must be open; the old head must equal the pushed one; `--all` makes no mergeability retry (`UNKNOWN` is not conflicting).

Metric: dispatcher messages to a lead after its first hand-over, per landed task. In the events log a dispatcher message is a `PostToolUse` event with `tool_name` `SendMessage`, `role` `other` and `to` the lead's name (the slug); count those of a slug whose `ts` is after the first `handed-over` record of that slug in the task state log, and divide by the number of tasks that landed in the period (`factory log events --since <ts>`).

## Stacked tasks
`new --base <slug>` branches the worktree from the base task's branch and records it in `git config branch.worktree-<slug>.factoryBase`; the pull request targets that branch. The record is never cleared: `status` stops showing a base once it has merged into the default branch, and `clean` stops treating a merged task as stacked. When a base merges, a lead still working on top of it has its lander rebase onto the default branch (the `base-merged` criterion flags it), and a task already handed over is landed by `factory land`; `merge` retargets a stacked pull request itself when its turn comes.

## Logs
All under `~/.claude/factory/`, JSON lines, append-only, mode 0600. Except where noted, a record carries `ts` (UTC, `2026-11-14T00:01:02Z`), `repo` (the main checkout's path), `slug`, `role` (`lead`, `dispatcher`, a subagent's type without the `factory:` prefix such as `coder`, or the same for a session started with `--agent`, as `factory land` starts the lander, `subagent` when it has none, else `other`; `feedback add` stores the `--role` it is given), `model`, `version` (the plugin's), `session`.

| Log | Files | Written by | Kept |
|---|---|---|---|
| events | `events-YYYY-MM.jsonl` | the plugin's hooks; `new`, `clean` | the current month and the 12 before it |
| retros | `retros-YYYY-MM.jsonl` | `factory retro` | forever |
| feedback | `feedback-YYYY-MM.jsonl` | hooks; `feedback add`, `decide`, `set` | `open` and `accepted` forever, `done` and `rejected` at least 12 months |
| task state | `tasks-YYYY-MM.jsonl` | `new`, `state`, `ack` | the current month and the 12 before it, and the newest record of every task that still has a worktree |
| firings | `firings-YYYY-MM.jsonl` | `watch`, `firing`, `miss` | the current month and the 12 before it, and every miss and every firing that is still open or was escalated, with its records |

Unsuffixed `events.jsonl`, `retros.jsonl`, `feedback.jsonl`, `tasks.jsonl` and `firings.jsonl` from older versions are still read, as the oldest. Old files are deleted when a log's first record of a new month is written; the lines still needed (the Kept column) are first carried, unchanged, into the current month's file. What goes after 12 months: a task's records once its worktree is gone, and firings that cleared and were suppressed or never decided. A task's state is its record with the newest `ts`, whichever file holds it.

`factory log` prints records oldest file first, where `since <= ts < until`; a bound is a `ts` or a prefix of one, at least `YYYY-MM`.

**Events** add `event` (the hook's name, or `TaskStarted`, `TaskRemoved`) and:

| `event` | Fields |
|---|---|
| `SessionStart` | `source` |
| `SessionEnd` | `reason` |
| `UserPromptSubmit` | `from`: `human`, `subagent`, `session` or `notification` |
| `Stop` | `background_tasks` (a count) |
| `SubagentStart`, `SubagentStop` | `agent_id` |
| `Notification` | `notification_type`, `message` |
| `PermissionRequest` | `tool_name` |
| `PermissionDenied` | `tool_name`, `reason` |
| `PostToolUse` (`SendMessage`, `SubagentHandback`) | `tool_name`, `agent_id`; for `SendMessage` also `to` and `kind` (`QUESTION`, `BLOCKED`, `OVERTIME`, `SPLIT`, `DONE` or null); for a `QUESTION`, `BLOCKED` or `OVERTIME` also `summary`: the message's first line after the `KIND: <slug>` prefix (the next line when the first is only the prefix), cut to 200 characters and left out when empty. Nothing else of a message is stored |
| `TaskStarted` | `base`, `lead_model` |
| `TaskRemoved` | |

**Feedback** is the outbox: issues agents report about the factory, and key decisions leads took without asking. An entry line adds `id`, `text`, `status: "open"`; a status line is only `id`, `ts`, `status` and optionally `pr`, and the latest one wins. `--since <ts>` lists the entries filed strictly after `<ts>`, compared with the entry's `ts` (the filing time, which a later `set` does not change): `YYYY-MM-DDTHH:MM:SSZ`, as `--json` prints it, or a date `YYYY-MM-DD` for everything from that day on; anything else exits 2. The last `ts` of one round is the next round's `--since`. Entries are filed from the section headed `Factory feedback` at the end of a subagent's report or a lead's `DONE:` message, one per list item (`none` files nothing); the same item filed twice is one entry. An entry whose text starts with `DECISION:` is a decision, any other an issue. The dispatcher reads the outbox, the retrospectives and the monitor's cost per task, and brings the human findings with a proposed edit to the plugin's files (`roles/lead.md`, the agent files, the dispatch skill); the human decides.

**Retros** add exactly these keys:
```json
{"defects": [{"caught_by": "tests|reviewer|lead|ci|human", "what": "..."}],
 "slowed": ["..."], "rules_missing": ["..."], "rules_dead_weight": ["..."], "brief_questions": ["..."]}
```

**Task state** adds `state` and, from the hand-over on, `deliverable`. A task's state is its last record.

## Site manager
A job runs `factory watch` every 30 minutes, output appended to `~/.claude/factory/watch.log`. It follows the dispatcher's life; `factory schedule install|remove|status` are for manual control.

- **Label.** `com.claude.factory.watch.<repository directory name>-<8 hex of the sha256 of the main checkout's path>`: two folders with one name get two jobs. The job runs in the main checkout with the dispatcher's session name (`--dispatcher`, default `dispatcher-<repository directory name>`).
- **Backends**, chosen at run time: launchd on macOS (`~/Library/LaunchAgents/<label>.plist`); elsewhere a systemd user timer when `systemctl --user` works (`<label>.service` and `<label>.timer` under `$XDG_CONFIG_HOME/systemd/user`, `~/.config` by default); else one line in the user's crontab ending in `# <label>`; else `install` exits 2. `install` prints `installed <label> (<backend>)`, `remove` prints `removed <label>` or `nothing installed: <label>`, `status` prints whether the job is present and loaded. On systemd the timer stops when you log out of every session unless `loginctl enable-linger` is on; `install` says so when it is not.
- **PATH.** The job gets a minimal `PATH`, baked at install: the directories of `git`, `gh`, `claude` and the running Python, then the system directories. `install` fails with `<tool> is not on PATH` when one is missing. Rename the dispatcher, or move a tool, and install again.
- **Start.** The plugin's hook (no model, no prompt) installs the job when a session that is named `dispatcher-<repository directory name>` and works in the repository starts, resumes or sends a prompt and there is none loaded. For a session that is not the dispatcher, in a main checkout without the job, that check costs about 0.2 s per prompt (`git worktree list`, the scheduler's status, `claude agents --json --all`). `/rename` fires no hook, so the job appears at the next prompt after it. On startup and resume it replaces the job, so after a plugin update the job moves to the new copy at the dispatcher's next start or resume; the superseded copy stays on disk for 14 days. A dispatcher under another name (`--dispatcher boss`) is not followed: run `factory schedule install --dispatcher boss`.
- **Stop.** When a dispatcher session ends (a `/clear` is not an end) and no other live one of the repository remains, the hook removes the job. A crash fires no hook, so a tick that finds no live dispatcher session removes the job itself. Such a tick merges nothing (`lgtm` merging stops with the watch) and starts no site manager; with live tasks it notifies once that the dispatcher is missing, and it keeps the job one more tick when it could not judge that.
- **Legacy.** On macOS `install` and `remove` also drop the job of the earlier label `com.claude.factory.watch.<repository directory name>` when its working directory is this repository.
- Install from the main checkout or the installed plugin: a job installed from a task worktree breaks when the worktree is removed.
- `remove` by hand lasts until the next prompt of a live default-named dispatcher, which installs the job again.
- `tools/walk.sh` walks this whole life with the real scheduler, stub `claude` and `gh` and a throwaway repository; CI runs it on Linux (systemd, then cron), on macOS and in the container. It installs a real job, so outside CI it wants `WALK=1`. `WALK_CRON=1` hides systemd so that cron is chosen; `WALK_DAEMON=1` leaves each cron tick to the cron daemon (the job's schedule becomes every minute) instead of running the job's line by hand.

`health` evaluates the criteria and prints what fires now; it writes nothing. `watch` does the same without running a model, logs each new firing, and only when there is something new to send starts one headless Sonnet run (no shell, no file tools, 10 minute timeout, about $0.15) under `factory/roles/site-manager.md`. `factory` starts every `claude` child (the lead, the site manager run, the lander run, `claude agents`, `claude rm`) without `ANTHROPIC_LOG` in its environment, since with `ANTHROPIC_LOG=debug` `claude` logs to the standard output that is read as JSON. The run gets the evidence in its prompt and either messages the dispatcher (first line `ESCALATION: <repository directory name>`) or suppresses the firing with a reason. `always` firings cannot be suppressed. Nothing is escalated twice while its condition lasts; a suppression is looked at again after two hours. `dispatcher-missing` becomes a desktop notification instead (`notify-send` on Linux; without a notification service, or when it fails, a `notify: ...` line in the watch log) and holds everything else back until a dispatcher exists. Either way the firing is recorded as escalated by `watch`, so the human hears once.

Criteria are `factory/criteria.json`, one entry per line: `name`, `escalate` (`always` or `judgment`), `threshold`, `what` and optionally `job` (`watch`, the default, or `digest`: see Digest). `health` and `watch` evaluate the `watch` entries only. Delete a line to turn one off; an unknown name or a bad value is an error (exit 2). A criterion whose inputs cannot be read fires nothing and is printed as `not evaluated: <name>: <why>`.

| Criterion | Escalate | Threshold | Fires when |
|---|---|---|---|
| `past-limit` | always | | a lead is working past the time limit |
| `ci-red-default` | always | | a workflow's newest decisive run on the default branch did not succeed |
| `dispatcher-missing` | always | | tasks are live and not exactly one unfinished session carries the dispatcher's name |
| `quiet` | judgment | 20 min | a lead whose turn has not ended wrote nothing and committed nothing for that long |
| `ended-undeclared` | judgment | 10 min | a lead's session ended with no hand-over or block declared |
| `blocked-unanswered` | judgment | 30 min | a task has been `blocked` that long |
| `handover-unacknowledged` | judgment | 24 h | a task was declared `handed-over` that long ago and is still in `review` |
| `review-loops` | judgment | 3 | a task had that many reviewer dispatches since it started |
| `wrong-model` | judgment | | a lead or subagent ran on a model other than the one pinned for its role |
| `ci-red-handover` | judgment | | a handed-over pull request has a failing check |
| `deep-stack` | judgment | 3 | a task's stack, counting the task itself, is deeper than that |
| `merged-not-cleaned` | judgment | 2 h | a task was merged or settled that long ago and not cleaned |
| `lgtm-blocked` | always | | a pull request carries the human's approval (an approving review or an `lgtm`) and was not merged; subject `#<number>/<kind>` (see Merging) |
| `base-merged` | always | | a lead is still working (pull request neither merged nor open against the default branch) on a base that has merged, and its worktree lacks that merge commit |
| `cost-outlier` | judgment | 3 | a working or blocked task has cost more than that many times the median landed task |
| `lead-heavy` | judgment | 85 % | a working or blocked task that has cost at least the median landed task has more than that share of its cost in the lead's own session |

Finished tasks (a merged pull request, or declared `acknowledged` or `dismissed`) are skipped by `quiet`, `ended-undeclared`, `blocked-unanswered`, `review-loops`, `wrong-model`, `deep-stack` (as the task that fires; it still counts in a stack above it), `cost-outlier` and `lead-heavy`. `past-limit` is not: a session running past the limit spends money whatever its pull request says.

The two cost criteria read the monitor, not the plugin: set `monitor` in the settings to the path of `isshoni` and each `health` or `watch` runs `isshoni sessions --json` once (7 seconds over all transcripts on the author's machine; 120 seconds at most). Without the setting, or when the monitor fails, they print `not evaluated`. The median is over this repository's tasks that have a merged pull request and a cost above 0; with fewer than 5 there is no median and they are `not evaluated`. The monitor's price file sets the currency. The thresholds come from this repository's 23 landed tasks (median 6.52, p75 10.48, p90 15.35, max 19.08, so 2.93 times the median): `cost-outlier` at 3 times the median would have fired on none of them. The lead's own share of cost (`main_pct`) had median 78.6, p90 88.3, max 90.5, and 54.7 to 82.2 among the 12 that cost at least the median; 85 is just above what those 12 showed. A task in its first minutes shows 100 % in the lead, which is why `lead-heavy` needs a cost of at least the median.

The dispatcher closes the loop: `firing <id> acted|ignored` on each escalation, `miss` for a problem that was not escalated (`--firing` when it had fired and was suppressed). `firings` lists the log, then precision `acted/(acted+ignored)` and recall `acted/(acted+missed)`.

`firings-YYYY-MM.jsonl` records carry only `ts`, `repo` and one of: a firing (`id`, `criterion`, `escalate`, `slug`, `subject`, `detail`); a decision (`id`, `decision`: `escalated` or `suppressed`, `reason`, `by`: `site-manager` or `watch`); an outcome (`id`, `outcome`); a clearing (`id`, `cleared: true`); a miss (`miss`, `slug`, `firing`); a digest marker (`digest`: the slugs of the tasks the digest covered), which has no `id`.

### Digest
A digest answers where a task's money goes. It is a set of findings computed by a script from `isshoni anatomy --json` (the monitor's per-task cost anatomy), then worded and filtered by the same headless Sonnet run as `watch` (`Job: digest`), which messages the dispatcher (first line `DIGEST: <repository directory name>`, then a `Tasks:` line naming the tasks landed since the last digest, then one line per finding it kept) or sends nothing. A finding is a criterion with `"job": "digest"` in `criteria.json`: it is logged as an ordinary firing, decided by the site manager, closed by the dispatcher with `factory firing <id> acted|ignored` and counted by `firings`. A finding describes one window, not a lasting condition, so the digest marks its firings cleared when it ends, and then writes the marker.

- **Window.** The tasks of this repository that have a merged pull request (`worktree-<slug>`) and a cost above 0, oldest merge first. The current ones merged after the last digest's marker (with no digest yet, the newest 5); the baseline is every one before them. With fewer than 5 in the baseline, or none current, a finding is `not evaluated`.
- **Cadence.** `watch` makes a digest when the last marker is at least 24 hours old (or there is none), the repository has `digest` entries, and at least 5 tasks have landed since the last one. So: at most one a day, none until 5 more have landed. The anatomy only moves when tasks land, a median needs 5 of them (the number the cost criteria use), and the reader's attention is the cost being managed. A tick inside the 24 hours costs nothing; one past them runs `isshoni anatomy --json` once (about 10 seconds on the author's machine) to count what landed, and a digest that is sent costs one Sonnet run (about $0.15). It does nothing when the tick has no dispatcher or fired `dispatcher-missing`. Without `monitor` in the settings, or when the monitor fails, it prints one `digest skipped: <why>` line per due tick and records nothing. A marker is written when at least one finding was evaluated, even if none fired. If the run is unusable, no marker is written, the firings are cleared, the command exits 1 and the next due tick tries again.
- **`factory digest`** makes one now whatever the cadence, and `--dry-run` prints the findings and the `not evaluated` list as `health --json` does, logging nothing and starting no model.

| Finding | Threshold | Fires when |
|---|---|---|
| `mix-shift` | 5 (points) | the share of total cost of one token class (`input`, `output`, `cache_write_5m`, `cache_write_1h`, `cache_read`), current against baseline, moved by at least that many points; the class that moved most |
| `dearest-task` | 1.5 (times) | the dearest current task cost more than that many times the baseline's median task; names the role that explains most of it |
| `role-growing` | 20 (percent) | a role in at least 3 current and 3 baseline tasks has a median context per call, or median number of own tool calls per task, that much above the baseline's |
| `version-shift` | 20 (percent) | at least 3 current tasks ran a plugin version no baseline task ran, and their median task cost, the lead's share of cost or the lead's median context moved by more than that, up or down |

To turn the digest off, delete the four `"job": "digest"` lines of `criteria.json`, or one of them to drop that finding. Without `monitor` in the settings it is skipped. A fifth finding is one line in `criteria.json`, one function and one entry in `CRITERIA`.

## Merging
`factory merge` merges pull requests with no model. It merges only what is mechanical and stops at the first thing that is not. Two things start it:
- **The dispatcher**, on the human's word: `factory merge <number>...`, or `--all` for every open pull request whose task is handed over (the others print `#N skipped: <why>`; with none left, `nothing to merge`).
- **The human approves** the pull request, in either of two ways. Each `watch` tick, before the criteria, merges every open pull request that has one; `health` runs the same checks without acting. This needs the watch running, which it is while a dispatcher session lives.
  - *An approving review.* Per login, the newest review that is not comment-only (`COMMENTED`, `PENDING`) is its verdict, so a later `CHANGES_REQUESTED` or a dismissal withdraws an approval and a later comment-only review does not. An `APPROVED` verdict counts when it is on the pull request's current head (its `commit_id`); one on an older commit is a `stale` blocker. Of several approvals, one on the head is preferred, then the newest.
  - *An `lgtm` comment.* Its whole body, trimmed, in any case, is `lgtm`, and it was created after the head branch last changed (GitHub's activity feed for the branch: the time of the push, not a commit date). Review bodies do not count. An `lgtm` can approve a pull request whose review is on an older commit; the `lgtm` rules then decide.
- **Who may approve**, for both: a login whose GitHub `user.type` is `User` (a bot never), with `admin` or `write` permission on the repository, and not the pull request's author. The exception: an author's `lgtm` counts while `lgtm_from_author` is `true` (the default); an author's review never does.

**Nothing technical tells the human's approval from one an agent posts when they share a login.** What is decided, per identity of the pull request's author:
- *The factory's App* (see Identity): the lead's pull requests are authored by the bot, so your approving review counts (GitHub does not let you approve your own pull request, which is why it could not with one account) and so does your `lgtm` (you are not the author). Nothing the bot posts counts: its comments and reviews have `user.type` `Bot`. A mechanism, and `lgtm_from_author` has no effect on it.
- *A second account of its own* (a regular user with write access): it looks like the human. Set `"lgtm_from_author": false` in the settings once the factory has such an account; its `lgtm` stops counting while another user's still does.
- *Without an identity* (your own `gh` login, the fallback): `lgtm_from_author` stays `true`, and the `lgtm` from the author rests on a rule written into every role file: no agent ever posts one, and none ever approves a pull request.

Per pull request, bottom-up through a stack (a pull request based on another selected one goes after it). Each check that fails is a blocker with a kind:

| Kind | Blocker |
|---|---|
| `fork` | its head is in another repository |
| `handover` | no task worktree has its head branch; or the task is not in phase `review`; or the worktree's head is not the pull request's head |
| `base` | its base is neither the default branch nor the head of a pull request merged into it |
| `ci` | no checks, a failed or cancelled check, or no passed check |
| `pending` | a check is still running; GitHub has not computed mergeability after 5 asks 3 seconds apart; the base's pull request cannot be read |
| `conflict` | it conflicts with its base |
| `stale` | the head changed after the approval (a review on an older commit, or an `lgtm` older than the last change to the head): a fresh one is needed |
| `head-time` | (`lgtm` only) the activity feed does not name the pull request's head, so when it changed cannot be told |
| `merge` | a `gh` step failed |

Then it retargets the pull request onto the default branch when its base is a merged one (`gh pr edit --base`), marks a draft ready (`gh pr ready`) and merges with a merge commit, pinned to the head it checked (`gh pr merge --merge --match-head-commit`). Merge commits keep branches stacked on it valid; squash would not. After any merge it runs `clean`.

- `merge` prints `#N merged <url>`, or `#N stopped: <reason>` and `#M not attempted` for the rest; exit 0 when everything merged, 1 on a stop or a failed step, 2 for a number that is not an open pull request. `--dry-run` changes nothing, checks every pull request as if the earlier ones had merged, and prints `#N would merge` or `#N would stop: <reason>`; mergeability of a pull request that would first be retargeted is not checked.
- In `watch` one pull request's blocker does not stop the others. It prints `#N merged on approval <review url>`, `#N merged on lgtm <comment url>` or `#N blocked: <reason>`. A blocker other than `pending` fires `lgtm-blocked`; `pending` fires nothing and the next tick tries again. Nothing is merged while phases cannot be trusted (a session with no `cwd`, an unknown pull request state) or once a `gh` read fails; `lgtm-blocked` is then `not evaluated`.
- Deleting `lgtm-blocked` from `criteria.json` silences the escalation, not the merging. To turn the `lgtm` trigger off, remove the schedule; while a default-named dispatcher session lives, its next prompt installs it again, so there is no lasting off switch short of ending the dispatcher.
- `clean` keeps the worktree the running plugin lives in: leads started by it load their plugin from there.

## Setup and uninstall
`factory wizard` (or `make wizard`, which takes `ARGS="..."`) walks the steps below in order and can be rerun: every run evaluates every step from the top, skips what is done and prints one line for each (`done:`, `did:`, `skipped:`, `needs you:`). A required step that cannot be completed stops the run, prints what to do and `Then run the wizard again.` and exits 1; an optional step that is declined or cannot run prints one line and the run goes on. It exits 0 when every required step is done and no step failed, else 1. Without a clone (only the installed plugin) the dashboard and cost steps are `skipped`.

| Flag | Meaning |
|---|---|
| `--yes` | accept every offer without asking. Without it an offer prints `question [y/N]` and reads one line from stdin; anything but `y` or `yes` is no, and so is end of input: no answer is never consent |
| `--list` | print each step and its state, change nothing; it runs only the read-only commands `gh auth status`, `claude auth status` and `claude plugin list --json`, and calls no network API |
| `--owner`, `--name` | the GitHub App step: see Identity |

1. **dependencies** (required): `git`, `gh`, `claude` and `openssl` on `PATH`, Python 3.11+, `gh auth status` succeeding and `claude auth status` saying `loggedIn`. It installs nothing; it lists everything missing at once with what to do.
2. **dashboard** (optional, clone only): `bin/isshoni-tui` exists. Offers `make build`; needs Go.
3. **plugin** (required): `factory@factory` is in `claude plugin list`. Otherwise adds the marketplace `andreiciobotar/isshoni` (when no marketplace named `factory` exists) and installs the plugin. A `factory` marketplace from another repository is never replaced: it stops and says so.
4. **settings** (required): `settings.json` exists and is valid. Missing, it is written atomically with the defaults (every key but `monitor`). An existing file that does not parse or validate is never overwritten: it stops with the error and the path.
5. **cost criteria** (optional, clone only): `monitor` names an existing file. Offers to set it to `<clone>/bin/isshoni`, keeping every other key (unknown ones too); a `monitor` that points at a missing file is replaced and the line says what it pointed at.
6. **GitHub App** (optional): see Identity. A failure here is printed, the run goes on to the summary and exits 1.

The summary lists each step's state and says that the site manager starts with the dispatcher session and stops with it (the plugin's hook installs and removes its job), so nothing is installed for it. When every required step is done it prints the two commands to start working, and when the run changed nothing, `Nothing to do: everything is set up.`

`factory uninstall` (or `make uninstall`) removes what the wizard and the factory put on the machine. `--list` says what each step would remove and does nothing; `--yes` confirms every step. Each step says what it found and what will be lost, then asks, then acts; a step with nothing to remove says so, and running it twice is safe. It deletes only on positive evidence that the thing is the factory's; everything else is kept and named in the final list. In order:

1. **schedules**: the watch jobs of this home, for every repository (launchd, systemd or cron). Under another `FACTORY_HOME` a job is this home's when it records that `FACTORY_HOME`; otherwise when it records none. A job of another home, or a file that is not a factory job, stays.
2. **GitHub App**: uninstalls the App's installation (`DELETE /app/installations/<id>` as the App; `404` is "already uninstalled"), then prints and opens the App's settings page: only you can delete the App itself (Advanced, Delete GitHub App). A failed call is reported and the run goes on.
3. **plugin**: `claude plugin uninstall factory@factory` and `claude plugin marketplace remove factory`, only when the plugin is installed and the marketplace `factory` is `andreiciobotar/isshoni`.
4. **home**: the factory's own names in `FACTORY_HOME` (settings, logs, specs, deliverables, `identity/`, `watch.log`), with counts first. Anything else is kept and listed, and the directory is removed only when it is then empty.
5. **dashboard binary** (clone only): `bin/isshoni-tui`.

It exits 0 unless a step failed, and ends with what it left: Claude Code's own transcripts and settings (`~/.claude/projects`), your repositories with their task worktrees and `worktree-*` branches (`factory clean` or `git worktree remove`, before the plugin is gone), the clone, git, `gh`, Go and Claude Code, the App on GitHub until you delete it, and whatever a step declined or kept.

## Identity
`factory wizard [--owner <account>] [--name <app name>]` (or `make wizard`) offers, as its last step, a GitHub App of its own for the factory. Once it is installed, the factory acts on GitHub as that App (see How the identity is used); with no identity stored, or an App stored but not installed, every command acts with your `gh` login, and `factory status` says which. It needs `openssl` on the `PATH` (it signs the App's JWT) and a browser.

First run, with nothing stored:
1. The owner is `--owner`, else your own account (`gh api user`); when that cannot be read it exits 1 naming `--owner`. Pass `--owner` for an organization (`make wizard ARGS="--owner NAME"`). GitHub says whether it is a user or an organization; the App is created under it. The offer is a question first ("Give the factory its own GitHub identity ... Two confirmations in your browser."); no answer is no.
2. The wizard starts a small web server on `127.0.0.1` (a port the OS picks), prints its address and opens it. The page sends GitHub an App manifest named `isshoni-lander-<owner>` cut to 34 characters (an App name is unique across GitHub and 34 characters at most; `--name` sets another, and one longer than 34 exits 2 instead of being cut; you can edit the name on GitHub's confirmation page, the wizard reads the real slug back). **Confirmation one**: create the App. GitHub redirects back to the wizard, which exchanges the code for the App's credentials. It waits 10 minutes, then exits 1 with nothing stored (GitHub creates the App when you confirm, so if you did, delete it on your apps settings page before running again; a failed exchange says the same). Over ssh, forward the printed port (`ssh -L PORT:127.0.0.1:PORT`).
3. The private key and `app.json` are stored at once. **Confirmation two**: install the App on the repositories you choose, on the page the wizard opens; it first says to choose "Only select repositories" and pick the ones the factory works in, because with "All repositories" the bot can write to every repository of the owner. It polls for 10 minutes; on timeout the App stays stored and the next run resumes here.
4. It signs a JWT, mints an installation token, lists the repositories the token reads, looks up the bot user, and prints the App, the bot login, the repositories and `To remove it: factory uninstall`. With "All repositories" it prints the count instead of the names, a `WARNING:` line naming the installation page where to narrow it, and that the tokens the factory hands out reach only one repository either way (see How the identity is used). The token is never printed or stored.

Permissions the App asks for, and why:

| Permission | Level | Why |
| --- | --- | --- |
| Contents | write | push a task branch; merge a pull request (the merge endpoint is under Contents) |
| Pull requests | write | open, edit, mark ready and comment on pull requests |
| Checks, Commit statuses | read | `gh pr checks` |
| Actions | read | `gh run list`: health reads CI on the default branch |
| Metadata | read | implicit |

Left out on purpose: `workflows` (so the bot cannot change CI: a branch touching `.github/workflows/` must be pushed by you, see below) and `issues`. No webhook is active.

Files, in `~/.claude/factory/identity/` (mode 0700):
- `key.pem` (0600): the App's private key. Never printed or logged.
- `app.json` (0600): `app_id`, `slug`, `client_id`, `html_url`, `owner`, `owner_type`, and once verified `installation_id`, `installation_url`, `repository_selection` (`all` or `selected`, refreshed on every run), `bot_login`, `bot_id`, `bot_email` (`<bot_id>+<slug>[bot]@users.noreply.github.com`).

Rerun with an identity stored: it never creates a second App, `--owner` naming another account exits 1 (one identity per machine), and `--name` exits 1 (the wizard does not rename). A `key.pem` without `app.json` exits 1 before any call, naming the `rm -r` to run. It sets looser modes back to 0600/0700 and says so; reports a missing `key.pem` (exit 1); reports when GitHub no longer knows the App or its key was revoked (exit 1, files untouched); opens the install page when the App is not installed; exits 1 when the installation sees no repositories, naming the page where to choose some; otherwise verifies, refreshes `app.json` (slug, URL, bot login, id and email come from GitHub's answer) and prints the report, exit 0. A failure that means the App is broken (key missing, App gone) prints the undo lines.

To rename the App, change its name on its GitHub settings page (there is no API for it), then run `make wizard` (or `factory wizard`): it reads the new slug from GitHub, records it with the bot's login and email, and prints `renamed: <old slug> -> <new slug>`. The App id, its key and the installation stay; `factory env` does not need the wizard, since no token depends on the name. Commits already made under the old name keep it in git and stay attributed to the bot on GitHub (same account id). Running leads use the new name at their next command (see below).

Headless machine: on Linux without `DISPLAY` or `WAYLAND_DISPLAY` the wizard does not open a browser (it would start a text browser in the terminal); it only prints the URL. GitHub redirects the browser to `127.0.0.1`, so opening that URL on another machine works only with the port forwarded (`ssh -L PORT:127.0.0.1:PORT`). Otherwise run the wizard on your laptop, then copy the directory (`scp -rp ~/.claude/factory/identity HOST:.claude/factory/`, or mount it into the container) and run `factory wizard` there: it verifies and prints the report.

### How the identity is used
An identity stored by an earlier wizard has no `repository_selection`: run `factory wizard` again to record it (and after narrowing the installation on GitHub).

Who acts as the App: lead sessions and their subagents (pushes, fetches, `gh`, and commits authored and committed as `<bot_login> <bot_email>`), and the script's own `gh` calls (`status`, `new`, `health`, `watch`, `merge`, `digest`, `land`; `land`'s `git fetch`, `rebase` and `push` also act as the App). Only in a repository whose `origin` is on GitHub and belongs to the App's owner (the accepted forms are `https://[user@]github.com/<owner>/<repo>[.git][/]`, `git@github.com:<owner>/<repo>[.git]` and `ssh://git@github.com/<owner>/<repo>[.git]`, the host in any case; any other form, an ssh port included, means no identity and your login is used): elsewhere, or in a repository of another owner, there is no acting identity (`factory env` prints nothing, `gh` gets no token, `status` says so) and the `gh` login is used. Not the dispatcher's own shell or your terminal; `eval "$(factory env)"` gives any shell in such a repository the same identity.

Mechanism. `factory env` prints these exports (every value shell-quoted):

| Variable | Value |
| --- | --- |
| `GH_TOKEN` | an installation token |
| `GIT_AUTHOR_NAME`, `GIT_COMMITTER_NAME` | the bot login |
| `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_EMAIL` | the bot email |
| `GIT_TERMINAL_PROMPT` | `0` |
| `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_n`, `GIT_CONFIG_VALUE_n` | four config entries: an empty `credential.https://github.com.helper` (drops every helper configured before, such as the keychain or `gh auth git-credential`), then a helper that answers `x-access-token` and `$GH_TOKEN` (read from the environment when git calls it; the token is not in the config), and `url.https://github.com/.insteadOf` for `git@github.com:` and `ssh://git@github.com/` (ssh remotes of github.com are used over https) |

The plugin's `SessionStart` hook (`factory hook`) appends one line, `eval "$(<path of factory> env)"`, to the file named by `CLAUDE_ENV_FILE`, which Claude Code runs as shell code before every Bash command. It does so for a lead session (a session in a task worktree, not a subagent's start), once (the line is not added twice), whether or not an identity is stored: running the wizard takes effect in running leads at their next command, and removing the identity undoes it. The dispatcher and any session outside a task worktree get no line. "A task worktree" is read from the path alone: any session whose project directory is `<repository>/.claude/worktrees/<name>` (a name not starting with `agent-`), in any repository where the plugin is enabled, whether or not `factory new` made it; the exports then apply when that repository's `origin` belongs to the App's owner and the App can reach it. When the owner matches but the installation does not cover the repository, git and gh fail in that session (`GH_TOKEN=unavailable`) rather than fall back to you. So with the App installed on all repositories, worktree sessions in your other repositories also commit and push as the bot, while sessions in a main checkout stay you. The script's own `gh` calls get `GH_TOKEN` in the child's environment; its `git` calls are local except `land`'s fetches, rebases and push, which run with the exports of `factory env` (see Landing without the lead).

Tokens last one hour at GitHub and are scoped to the repository the command runs in: the mint asks for `repositories: [<name of origin>]`, so the token reaches that repository only, whatever the installation covers. A repository the installation does not cover is refused by GitHub (422): the message says `the App is not installed on <owner>/<name>: add it at <installation url>`, and `factory env` fails closed as for any mint failure. The token is cached per repository in `identity/token-<name>.json` (0600) and minted again when under 15 minutes remain, so every Bash command starts with at least 15 minutes, which is more than a foreground command may run (10 minutes). Limits: a background command that outlives its token; a session started before plugin 0.5.0 has no hook line (restart the lead); `GIT_CONFIG_*` variables set by the caller are replaced; only `github.com`.

Secrets. The token is in the environment of the lead's shell commands: an agent can read it. It reaches only the repository the command runs in, with the permissions in the table, and it expires. The identity is a default, not a boundary: the dispatcher session still acts with your login, and a lead's shell can still reach your own `gh` login or keychain by unsetting the variables, so the rule that no agent posts `lgtm` or approves a pull request stays. The key never leaves `identity/`, is read only by `openssl` when a token is minted, and no role is told where it is.

Failure. When an identity applies (installed, and this repository on GitHub under the App's owner) but anything goes wrong finding it or minting the token (a refused mint, GitHub 422 for a repository the installation does not cover, a corrupt or malformed `app.json`), `factory env` prints one line with the reason on stderr, exits 1, and still prints the lock-down: `GH_TOKEN=unavailable`, `GIT_TERMINAL_PROMPT=0` and the four `GIT_CONFIG_*` entries (so `git` cannot reach your credential helper or your ssh key for github.com), plus the bot's author and committer variables when the identity itself could be read. Each command then shows the reason and `git` and `gh` fail with an authentication error; in a lead session they never fall back to your login. A corrupt or malformed `app.json` locks down every repository where `env` runs, not only the App owner's, because the owner cannot be read. When no identity applies, it prints nothing.

`.github/workflows/`: GitHub refuses the App's push of a branch that changes a workflow file. The lander stops (`denied`), the lead reports BLOCKED, and you push the branch from your own terminal (`git -C .claude/worktrees/<slug> push -u origin HEAD`, with `--force-with-lease` after a rebase) and tell the dispatcher; the same again whenever that branch is rebased.

Checked with a real App, read-only: a token minted for one repository reaches that repository only although the installation covered all (another repository of the owner answers 404); `gh pr view --json statusCheckRollup,reviews`, `gh pr list`, `gh pr checks`, `gh run list`, the `collaborators/<login>/permission` and `activity` reads, and GraphQL `viewer` (the bot) all answer; the credential helper authenticates `git push --dry-run`.

Not yet proven, the first real task will show: `gh pr create`, `gh pr edit --base`, `gh pr ready` and `gh pr merge --match-head-commit` as the App; a real push and the bot showing as author on GitHub; GitHub's refusal message for a workflow-file push, and that the lander's later push of an already pushed head succeeds; branch protection that restricts who may push or merge (the App must be allowed there).

Undo: `factory uninstall` uninstalls the App and removes `identity/`; you delete the App itself (`https://github.com/settings/apps/<slug>/advanced`, or `https://github.com/organizations/<owner>/settings/apps/<slug>/advanced`; no API deletes an App).

## In a container
[container.md](container.md) runs the factory on an always-on Linux host: an image with Claude Code, `gh`, git, Python and Go (`docker/`), all state in one host directory, a non-root user, CPU and memory limits. The site manager's job uses the cron backend there, run by supercronic because a container has neither systemd nor a root cron; the rules above hold unchanged. The same page lists the first-run steps, what a lead in `auto` mode could damage, and how the factory's GitHub identity gets into the container.

## Known limits
- Nothing caps what a lead spends; the time limit is checked by the lead between subagent runs.
- A message that a base moved still wakes a lead that is working: it passes it to its lander. A handed-over task is landed by `factory land` without its lead; what the lander may do rests on its rules and its tool list, not on a mechanism: Bash can do anything the session may, and the scripted path trusts the session list to tell a lead that is running. When a stacked task's base was itself rebased, or was squash-merged, the rebase replays the base's old commits: `land` reports a difference or stops on a conflict, and the lead sorts it out.
- A worktree made by hand under a slug used before inherits that slug's last declared state.
- A store nobody writes to is never pruned. A month file and `watch.log` have no size limit.
- The monitor adds up the cost of a reused slug, so a task started under an old slug looks dearer than it is. A model the monitor has no price for understates the cost.
- Merging: without an identity (the author's own account) the author's `lgtm` rests on a rule, not a mechanism (see Merging). A `CHANGES_REQUESTED` review by a different login does not block a merge. The author comparison and the bot test rest on GitHub's `user.type` and login. Checks are not rerun after a retarget, and GitHub does not test the merge result: mergeable means no conflict. A pull request with no task worktree is a `handover` stop; the human merges it. At most 100 open pull requests are seen. An `lgtm` during a running check waits for the next tick. There is no lock: when a tick and the dispatcher merge at once, the second `gh pr merge` fails and is reported.
- The site manager's thresholds are first guesses; `quiet` reads file times, so a lead waiting on a long command looks quiet; a run that dies after sending may escalate the same firing again next tick. A site manager run is read as one JSON document on `claude`'s standard output.
- Two repositories whose directories share a name share a spec folder and a default dispatcher name.
- Identity: the private key itself can mint a token for any repository of the installation, so a narrow installation ("Only select repositories") is still the real boundary, not the scoping of the tokens handed out; during a GitHub outage each lead command waits for the mint attempt (up to 30 s) once the cached token is within 15 minutes of expiry (a cached token that has not expired is used when the mint fails); one per machine; `workflows` is not granted; only `github.com`; a background command can outlive its token; a lead started before plugin 0.5.0 has no hook line; `GIT_CONFIG_*` set by the caller is replaced. See How the identity is used, including what only a real run proves.
