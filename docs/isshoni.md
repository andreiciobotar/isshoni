# isshoni reference

Read-only CLI over `~/.claude/projects` transcripts (Python 3.11+, stdlib only).
```
bin/isshoni sessions [--since 7d]     # sessions grouped by repository and task, with totals
bin/isshoni session <id-prefix>       # totals by model/agent, warnings, dispatch timeline
bin/isshoni agents [--since 7d]       # per agent type: tokens, p50/p90, repeats, WARN
bin/isshoni agents --repo isshoni --task ci-workflow   # tokens by agent and model for one task
bin/isshoni kaizen [--since 30d]      # the factory's improvement measures (see Kaizen measures)
bin/isshoni anatomy [--since 30d]     # per task and role: calls, contexts, cost by token class (see Cost anatomy)
```
`sessions`, `agents` and `anatomy` accept `--repo NAME` and `--task NAME` (exact match).
Global options: `--json` (one JSON document on stdout), `--projects-dir`,
`--agents-dir` (agent definitions used for the model WARN), `--prices` (price file), `--repeat-threshold`
(Jaccard, default 0.5). `--since` takes `Nd`, `Nh`, `Nw` or an ISO date.
Exit codes: 0 ok, 2 usage error (unknown or ambiguous session id, bad option).
Flags in the timeline: `repeat`, `review-loop`, `rechecked`, `WARN`, plus the
status when not `completed` (`running`, `no-run`, `orphan`).

Cost: every command shows money next to tokens, priced per message from
`isshoni/prices.toml` (per million tokens: input, output, cache write 5m and 1h, cache
read; an optional `long_prompt` table for a model that charges more above a prompt
size). Edit the file or pass `--prices PATH` (`isshoni-tui --prices` passes it on); a
missing or invalid file exits 2. A model id matches a table exactly or minus a trailing
`-YYYYMMDD`. A model with no table is never counted as zero: it shows as `unpriced`,
figures that contain its tokens end in `*`, and a footer names it. In `--json` every
usage object gains `cost` and `unpriced` (tokens), each session, task and repository a
`cost` block beside `tokens` (with `unpriced_tokens`, `unpriced_models`), agents
`cost_p50`/`cost_p90`, and each document a `prices` block. Limits: cost is the list
price at the file's `as_of` date, not what a subscription charges; fast mode, US-only
inference, web search and batch are not priced; it is a lower bound, because Claude
Code makes helper calls (web search and fetch summaries) that it does not write to the
transcript.

Grouping: a task is a git worktree a session worked in. A session belongs to one
repository and at most one task: the last worktree it entered (from the
`worktree-state` lines of its main transcript, ignoring `agent-<hex>` subagent
worktrees), else its first `cwd`; the repository is the path before
`/.claude/worktrees/`. A session that switched worktrees is counted whole under the
last one; its `worktrees` field lists every task it entered. In `--json` each session
gains `repo`, `repo_path`, `task`, `worktrees`, and `sessions --json` gains `repos`
(repository and task totals with the same keys as a session). Known limits: a session
started in a subdirectory of a repository, outside any worktree, shows that
subdirectory as its repository; review loops are detected inside one session, so a
task's count is the sum over its sessions.

Plugin agents: a plugin's agent is dispatched as `<plugin>:<name>` (`factory:coder`)
and counts as `<name>` for review loops, re-checks and repeats, and for its colour and
position in the dashboard. Its full name is shown, and it keeps its own row in `agents`
and its own `by_agent` key, apart from a bare `coder`. Its model is not checked by
default, because a transcript does not record where a plugin's definitions were loaded
from; pass `--agents-dir <plugin>/agents` to check it against `<name>.md` there (the option
replaces the default directories, so bare agents are checked against the same files).

For a resumed agent (`SendMessage`), duration covers dispatch to first return while
tokens include the resumed work. `running` means no completion (hand-back or
task-notification) was found in the transcript.

## Kaizen measures
```
isshoni kaizen [--since 7d] [--factory-dir DIR] [--json]
```
Whether the factory is getting better, computed once. It reads the factory's stores
read-only (`--factory-dir`, default `~/.claude/factory`; format in `docs/factory.md`,
"Logs"), the transcripts and the price file; it never imports or runs `factory/bin/factory`.
`--since` keeps the tasks that started at or after the cutoff (and the stops, slowed
texts and feedback of that range) and limits the transcripts loaded. Without `--json` it
prints the headline numbers and one line per task. A missing or empty directory exits 0 with
empty lists (`store_found: false` when the directory is missing). Records are read with a
default for every field and lines that are not JSON objects are skipped.

A task is a `(repo, slug)` that appears in events, retros or the task state log; records without
a `slug` or a `repo` are skipped, and worktrees the monitor sees that the factory never
started are not tasks. **Landed** means the task's pull request is merged. Pull requests
come from one `gh pr list --state all` per repository, run in the repository, matched on
the head branch `worktree-<slug>` (the newest when several). When `gh` cannot be used
(missing, not a GitHub repository, no such path, bad output) the repository's `prs` holds
the reason, its tasks have no `pr`, and nothing there counts as landed.

The document: `generated`, `factory_dir`, `store_found`, `prices`, `roles` (the role
names in display order; every `by_role` and `share` object has exactly these keys), `repos`
(`repo`, `repo_path`, `prs`: `ok` or the reason), `versions` (each plugin version by its
earliest record in any store), `tasks`, `summary`, `stops`, `slowed`, `rewards`, `outbox`,
`site_manager`.
Times are UTC (`2026-11-14T00:01:02Z`), a task's durations whole seconds, medians floats
that may be fractional, money in the price file's currency, and a missing value is `null`.

Per task (listed by `started`, tasks without one first, then slug):

| Field | Definition |
|---|---|
| `repo`, `repo_path`, `slug` | `repo` is the last component of the path |
| `version` | of its `TaskStarted` event, else of its earliest record in any store |
| `started` | `TaskStarted` time, else the start of its earliest transcript session |
| `state` | its last declared state (the task state record with the newest `ts`) |
| `pr` | `{number, url, state, opened, merged}` |
| `landed` | `pr.merged` is set |
| `brief_to_pr_s`, `pr_to_merge_s` | `pr.opened - started` (null when negative); `pr.merged - pr.opened` |
| `cost` | from its transcript sessions (same repository and worktree name): `{total, unpriced_tokens, by_role, rework}`; `by_role` has `lead` (the main session), `coder`, `reviewer`, `explorer` (also `Explore`) and `other` |
| `cost.rework` | cost of coder and reviewer dispatches that started after the first reviewer dispatch returned. A lower bound: the lead's own rework is not separable |
| `review_rounds` | reviewer dispatches that ran (not `no-run`); null without sessions |
| `first_pass` | `review_rounds == 1`; zero reviews is not first-pass |
| `stops`, `stopped_s` | its stops, and the sum of their `waited_s` |
| `defects` | counts by `caught_by` (`tests`, `reviewer`, `lead`, `ci`, `human`, `other`) from its newest retro |

`summary` covers landed tasks only, each figure over the tasks that have the value (`n`):
medians of `cost`, `brief_to_pr_s`, `pr_to_merge_s`, `review_rounds`; `first_pass`
(`yes` of `of`); `rework_share` (rework over cost); `by_role`; `time` (`building_s` is
`brief_to_pr_s` minus `stopped_s`, never below 0, plus `stopped_s` and `merge_wait_s`).
`tasks` counts all, `in_flight` those not landed and not `acknowledged` or `dismissed`, and
`defects` sums every task's newest retro. `trend` is null below `trend_min` (10) landed
tasks; else the median of each measure over the latest half (`latest_n`) against the rest.

`summary.mix` is how the cost mix by role moves: `window` (the integer 5) and `points`,
one per landed task with a cost above zero, in start order, from the fifth such task on
(so `points` is empty below five). A point has the task's `slug` and `version` (a string
or null) and `share`: for each role, the median over the five tasks ending at that task
of the role's share of the task's cost (`by_role` over `total`), a float from 0 to 1.
The medians are taken per role, so a point's shares need not sum to 1. The window does
not restart at a plugin version: a change in the mix shows in full three tasks after it.
A role added to `ROLES` in `isshoni/kaizen.py` gets its own key everywhere; an agent
whose role is not listed there is counted under `other`.

`stops`, oldest first: a lead's `QUESTION`, `BLOCKED` or `OVERTIME` message (`waited_s`
runs to the next prompt from the human or the dispatcher in the same session, null while
unanswered; `why` is the event's `summary`, the first line of the message, or null on
events logged before the hook stored one) and a denied permission
(`DENIED`, `why` is `<tool>: <reason>`). `slowed` lists every `slowed` text of the retros,
newest first. `rewards` fire only on real records over landed tasks in merge order: the
last 3 or more all first-pass; a latest cost, or a latest brief-to-pull-request time, that
is strictly the lowest of at least 5 tasks (a tie is not a record). `outbox` folds
`feedback` as the factory does (the latest status line wins, with its `pr`), counts issues
and decisions by status, and groups open issues by the first keyword topic that matches
(`permissions`, `brief`, `rules`, `tools`, `review`, `time`, else `other`).

`site_manager` is the site manager's record over every repository, read from
the firing log (every month file) and counted as `factory firings` counts it
(`docs/factory.md`, "Site manager"; a record without a `repo` counts nowhere): `escalated` firings and of those `acted`, `ignored` and `no_outcome`;
`suppressed` firings; `missed` problems (`missed_suppressed` had fired and were
suppressed, `missed_never_fired` the rest); `precision` is `acted / (acted + ignored)` and
`recall` is `acted / (acted + missed)`, each 0 to 1, or null with nothing to divide by.
With `--since` it counts the firings that fired, and the misses recorded, at or after the
cutoff. Without a log every count is 0 and both ratios are null. The factory keeps
escalated firings and misses forever, so the two ratios are all-time; `suppressed` covers
what the log still holds (about a year).

Limits: no landed figure without `gh`; a stop logged before the hook stored a summary has no reason;
the site manager's firings are counted, not listed (`factory firings` lists a
repository's); rework counts subagent runs only; costs are the list
prices of `isshoni/prices.toml` (see Cost above).

## Cost anatomy
```
isshoni anatomy [--since 7d] [--repo NAME] [--task NAME] [--factory-dir DIR] [--json]
```
Where a task's money goes, per role, from the same transcripts and prices as `sessions`.
A task is what `sessions` calls one: the sessions sharing `(repo_path, task)`; sessions
without a task are left out. A role is `lead` for a session's own transcript, and for a
subagent run the name of its agent type without the plugin prefix (`factory:lander` is
`lander`). `--factory-dir` (default `~/.claude/factory`) is read only for each task's plugin
version. Without `--json` it prints one row per task and role (`task`, `version`, `role`, `runs`,
`calls`, `ctx first`, `ctx median`, `ctx max`, `cost`, the shares of the role's cost by token
class `in%` `out%` `cw%` `cr%`, own `tools`, `disp`atches) and the prices footer; no tasks prints `no tasks`.

`--json` is `{generated, factory_dir, prices, tasks, versions}`; no tasks gives two empty lists.
Each task has `repo`, `repo_path`, `task`, `version`, `start`, `end`, `cost` (the sum of its
roles' `cost.total`), `unpriced_tokens` and `roles`, a map from role name to a block that
aggregates the role's runs (one run = one transcript):

| field | meaning |
|---|---|
| `runs`, `models`, `calls` | transcripts of the role, the models seen, model calls (each message once; `<synthetic>` excluded: exactly what is priced) |
| `context` | `first` and `peak`: median over runs of a run's first and largest call context; `median`: over all calls; `max`: largest of any call. A call's context is `input + cache write + cache read`. Null when there are no calls |
| `tokens` | exact sums of `input`, `output`, `thinking` (part of `output`, not added to it), `cache_write_5m`, `cache_write_1h`, `cache_read` |
| `cost` | `total` and the five classes, rounded to 6 places. Over a task the roles' `total`s add up to the task's `cost.total` in `sessions --json` (to 1e-6) |
| `tools` | the role's own tool calls by kind, each tool call once, `Agent` excluded. `Bash` is split as `Bash:<command>` by the basename of the first word after an optional `cd <path> &&` |
| `dispatches` | the role's `Agent` calls |
| `fill` | where the context came from, see below |

`version` is the plugin version of the earliest record of the task (same repository and slug) in the
factory's `events`, `retros` and `tasks` logs that carries one; null without one or without a store.
`versions` rolls the tasks up by version (null last): `version`, `tasks`, `cost`, `unpriced_tokens` and
`roles`, the role blocks computed over all runs of that version's tasks, not an average of task blocks.

**Fill.** Per run, `base` is the first call's context. For each later call the context grew by
`delta`: `own_output` is the previous call's output (at most `delta`), the rest is `inbound`,
what arrived in between. A shrinking context counts one in `compactions` and adds nothing else.
`base`, `own_output`, `inbound` and `compactions` are exact. Only `inbound_estimate` is an estimate:
`inbound` split by character share among the sources recorded since the previous call,
`result:<tool>` (`result:?` when the tool is unknown), `message`, `attachment:<type>` and `handback`
(Agent results, `<task-notification>` texts); with no characters recorded it is all `unaccounted`.
The shares are rounded, so they need not sum to `inbound`.

Limits: images count no characters; a compacted run's fill is partial (the characters that arrived before the shrinking call are dropped); a reused slug adds up
into one task; the version is the first one logged, not the one running.

## Dashboard (`isshoni-tui`)

`make up` builds and runs it. It shows what `isshoni --json` reports, re-reading a
session when its transcript changes (checked every second). The only file it writes
under `~/.claude` is the factory's settings, when you save the cap. It finds the
Python package from `ISSHONI_ROOT`, else from the binary's own location. It needs a
terminal of at least 60x16.
```
bin/isshoni-tui [--range 24h|7d|30d|all] [--follow] [--no-color]
                [--projects-dir DIR] [--agents-dir DIR] [--prices PATH]
bin/isshoni-tui --snapshot dashboard|session|agents|factory|kaizen|kaizen-flow|kaizen-board|help [--size 140x40] [--session ID] [--row N] [--fold]
```
`--range` defaults to `7d`. `NO_COLOR` gives the plain rendering too. `--snapshot`
prints one settled frame and exits, non-zero when the load fails; `--row N` puts the
dashboard cursor on table row N (0 is the `all` row) and `--fold` collapses the
repository that row is in; `make screenshot
VIEW=... SIZE=...` turns it into a PNG under `shots/` (needs Chrome).

| Key | |
|---|---|
| `tab` `shift+tab`, `1` `2` `3` `4` `5` | switch view: dashboard, session, agents, factory, kaizen |
| `↑` `↓` `j` `k`, `g` `G`, `pgup` `pgdn` | move, first and last, page |
| `←` `→` `h` `l` | dashboard: collapse the repository the cursor is in (the cursor moves to its row), expand it. A collapsed row (`▸`) keeps its totals, and the tiles and follow mode still count what is inside; a filter leaves it collapsed, counting the matches. Folding turns follow mode off and is forgotten on exit |
| `enter`, `esc` | open the session under the cursor; on `all`, a repository or a task, the most recently active session in it (a message names it); back |
| `r` | range: 24h, 7d, 30d, all (on the error screen: retry) |
| `s` | sort: recent, cost, subagent share; within a repository, tasks and sessions without a task are one list |
| `/` | filter by `repo`, `repo/task`, project or session id |
| `f` | follow the most recently active session |
| `←` `→`, `h` `l` | kaizen view: previous and next page (Pulse, Flow, Board) |
| `c` | factory view: edit the cap (digits or `↑` `↓`, `enter` saves, `esc` cancels) |
| `?`, `q` | help, quit |

- **Dashboard:** totals for the range, the sessions as a tree of repository, task and
  session, the agent and model mix, and every dispatch running now. A group row sums
  its sessions (rates from summed counts, never a mean of rates); a group of one
  session is a single row. The cursor starts on the top row, `all`, which sums every
  session shown, and moves over every row. The agent and model mix show the row under
  it and name it, in their titles or, under 120 columns, in the heading above the two
  strips; money and share are over that row's cost, and a strip ends in `+N` when N
  entries do not fit. The tiles always total every session shown, and a line under them
  says so. In the mix a plugin's agent counts with its role (`factory:coder` as
  `coder`); the Agents view keeps them apart. A cursor row that disappears (filtered
  out, merged away) falls back to `all`. Columns: time (last activity when sorted by
  recent, else start), `COST`, `TOKENS`, `MAIN│SUB` and `SUB%` (shares of cost), `DISP`
  (dispatches), `REP` (repeats), `LOOP` (review loops), `RCHK` (re-checks), `WARN`
  (model warnings), `BAD` (malformed lines, only when there are any). A narrow
  terminal drops `BAD`, `SUB%`, `TOKENS`, then the time.
- **Session:** cost by agent, a timeline with a lane per agent type (dispatches of one
  type that overlap stack on extra rows), the dispatch list and the selected dispatch
  in detail. Idle stretches of the timeline collapse to a marker labelled with their
  length. A dispatch still `running` in a session that is no longer live is labelled
  `unfinished`.
- **Agents:** per agent type: dispatches, models, tokens and cost, p50/p90 cost and
  durations, repeats, review loops, warnings.
- **Factory:** for each repository seen in the range that has task worktrees, what
  `factory status --json` reports: the tasks holding a slot against the cap, and every
  task with its phase, elapsed time, pull request or deliverable, base, and `overtime`
  or `stale`. At the top, a band lists everything that waits on you across repositories,
  oldest first: tasks in review (the pull request or deliverable to look at) and blocked
  tasks (what they asked), each with how long it has waited; when nothing waits it says
  so. The `4 Factory` tab carries `▲N` for the number waiting on every view (in a narrow
  header the live count then shows as `● N`, without the word). It refreshes every 30
  seconds while shown, every 2 minutes otherwise. `c` sets `worktree_cap` (1 to
  999) in `~/.claude/factory/settings.json`, leaving the other keys as they were; a file
  that is not a JSON object is never overwritten. See [factory.md](factory.md).
- **Kaizen:** whether the factory is getting better, across all repositories, from what
  `isshoni kaizen --json` reports (see [Kaizen measures](#kaizen-measures)). It loads when
  the view is shown (unless a result under 30 seconds old is held) and when the range
  changes; there is no periodic refresh. Three pages, switched with `←` `→`, each
  scrolled with the move keys: **Pulse** (the landed count and the median cost, brief to
  pull request, pull request to merge and first-pass rate, each with how the latest half
  of the landed tasks compares with the earlier half from 10 landed tasks; the rewards;
  a column chart per measure, one column per landed task in start order, with the
  plugin versions marked under it; who caught the defects; the site manager's record:
  precision and recall, then what it escalated with the outcomes, suppressed and missed,
  or one line saying nothing is logged yet), **Flow** (where the money
  and the time went; under the money, a line chart of each role's share of a task's cost
  over the landed tasks in start order, each point the median of the five tasks ending
  there (`summary.mix`), one line per role in its colour with its latest value at the
  right, the plugin versions marked under it, and from six landed tasks with a cost,
  below which one line says so; where two lines share a cell the role listed first
  shows; a table of every task, the stops each with its reason in full or
  "reason not recorded", what slowed tasks) and **Board**
  (the factory's feedback outbox: open issues grouped by topic, what was acted on, the
  decisions). The columns of a chart use the colour of what they measure; review rounds
  are green for a first pass and orange otherwise; a chart too narrow for every task
  keeps the newest and says `last N/M`. The Pulse rewards line, when there are rewards,
  shimmers for three seconds each time Pulse is shown or new numbers arrive; the tiles
  count up and the columns grow while new numbers arrive (never in a snapshot).

A session is live when its transcript changed in the last 30 seconds or so. Agents keep
one colour and one order everywhere: main, coder, reviewer, explorer, then the rest.
