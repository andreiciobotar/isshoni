<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/isshoni-dark.png">
    <img src="assets/isshoni-light.png" width="220" alt="Isshoni: a disc with blossoms streaming up to the right">
  </picture>
</p>

<h1 align="center">Isshoni · 一緒に</h1>

<p align="center">
  <i>issho ni</i>, Japanese for "together"
</p>

<p align="center">
  Give Claude Code a brief, get back a reviewed pull request,<br>
  and see where every token went on the way.
</p>

<p align="center">
  <a href="https://github.com/andreiciobotar/isshoni/actions/workflows/test.yml"><img src="https://github.com/andreiciobotar/isshoni/actions/workflows/test.yml/badge.svg" alt="tests"></a>
</p>

Isshoni is two tools for people who run [Claude Code](https://claude.com/claude-code) on their repositories. Each works without the other.

| | What it does | You run |
|---|---|---|
| **Monitor** | Shows where the money and tokens went: per repository, task, session, agent and model | `isshoni`, `isshoni-tui` |
| **Factory** | Builds several tasks in parallel, each in its own git worktree, each ending in a draft pull request | `/factory:dispatch`, from the `factory` plugin |

## Kaizen (改善), built in

Most agent setups run on day 100 the way they ran on day 1. Isshoni's factory learns from every task:

- Every agent reports what slowed it down.
- Every lead records the key calls it made without asking, and why.
- Every finished task leaves a retrospective and, in the monitor, its cost.
- Every site manager alert is marked acted-on or noise, so its precision and recall are measured.

The dispatcher reads it all and proposes edits to the factory's own rules; you decide.

On day one, agents reported the same shell friction eleven times and counting. It became a rule that day ([#8](https://github.com/andreiciobotar/isshoni/pull/8)).

## Monitor

`isshoni` reads the transcripts Claude Code already keeps in `~/.claude/projects` and never writes there. It reports cost and tokens by model and agent, the share spent in subagents, and the signs of wasted work: repeated dispatches, review loops, and agents running on a model other than the one pinned for them.

```sh
git clone https://github.com/andreiciobotar/isshoni && cd isshoni
bin/isshoni sessions --since 7d    # sessions by repository and task, with totals
bin/isshoni session <id-prefix>    # one session: models, agents, dispatch timeline
bin/isshoni agents --since 7d      # per agent type: cost, tokens, p50/p90, repeats
```

Cost is the API list price, from [isshoni/prices.toml](isshoni/prices.toml); edit it when prices change. Python 3.11+, no dependencies. Add `--json` for machine-readable output; [docs/isshoni.md](docs/isshoni.md) has the rest.

`isshoni events [--since 24h] [--follow] [--no-gh]` prints what happened as JSON lines, one event per line, sorted by time. It reads the transcripts, `~/.claude/factory` (`--factory-dir`) and `gh pr list`, and never writes. With `--follow` it prints the history since `--since` (default `2m`) plus every pull request still open and every question still unanswered, then a `live` line, then what is appended; it polls `gh` every 60 s, and a slow `gh` pauses the stream for up to its 20 s timeout. `--since` also takes `Nm` minutes. Absent fields are omitted.

| kind | meaning | fields |
|---|---|---|
| `tool` | a tool call: `shell`, `read`, `edit`, `write`, `dispatch`, `message` or `other` | `session`, `agent`, `role`, `tool` |
| `tokens` | one assistant message's usage | `session`, `agent`, `role`, `in`, `out`, `cache_read`, `cache_write` |
| `spawn`, `return` | a subagent starts, hands back or ends its turn; a reviewer's return has `verdict` `changes` or `approve` | `session`, `agent`, `parent`, `role`, `verdict` |
| `pr`, `merge` | a pull request opened (`state` `open`) or closed unmerged (`closed`); one merged | `pr`, `slug`, `state` |
| `ci` | a pull request's checks changed to `running`, `green` or `red` (`--follow` only) | `pr`, `state` |
| `question`, `answer` | a task blocked on you, and the next record that unblocks it | `slug` |
| `watch` | a site manager tick | none |
| `live` | `--follow` only, once: everything before it had happened when the stream started | none |

`isshoni-tui` is the same data as a live terminal dashboard: sessions grouped by repository and task, a pane for the row under the cursor (timeline and dispatches of a session, `enter` for the full screen), and what is running now. Its factory view (`3`) lists what waits on you across repositories, then each repository's factory tasks with their phase and how many hold a slot against the cap, and `c` there changes the cap: the one thing the dashboard writes (`worktree_cap` in `~/.claude/factory/settings.json`). Its kaizen view (`4`) shows from `isshoni kaizen` whether the factory is getting better: cost, speed, review rounds, defects and the feedback outbox.

```sh
make install    # needs Go 1.27+ and Python 3.11+; builds bin/isshoni-tui
make up         # run it; ? shows the keys
```

<!-- screenshot: isshoni-tui dashboard view -->
<!-- screenshot: isshoni-tui session timeline -->

### Hanami (花見), the work as something to watch

The fifth view (`5`) draws the same work as motion, for a second screen. Nothing in it is decoration posing as data: every moving thing is one event from `isshoni events`, and `l` shows the legend.

| you see | it is |
|---|---|
| the disc, its rim swelling | all the work; the rate of events |
| a branch growing out of the disc, its tip glowing | a session, while it works (it withdraws after 3 minutes of silence); a tool call |
| a blossom opening on a branch (`✿` coder, `❀` reviewer, `✾` explorer, `✽` other, in the role's colour) | a subagent dispatched |
| petals flying to the disc; turned back at the disc in red, with a red ring | a subagent returned; a reviewer asked for changes |
| faint wind; bright streaks off an agent | cache reads; output tokens |
| notes on the staff (`●` shell, `○` read, `◆` edit, `■` write, `▲` dispatch, `◇` message, `·` anything else) | tool calls, in the caller's colour, newest at the right |
| a lantern rising to the sky; flickering; green; red; going out | an open pull request; checks running; passed; failed; closed without a merge |
| a firework where a lantern was | a merge |
| a ripple, then a still ringed star above the disc | a task asked you a question; the star stays until it is answered |
| a ripple from the disc across the field | the site manager's tick |

Ambient, and not data: the disc's slow breath, the sway of branches and lanterns, the slowest breeze. With nothing running, that is all there is.

Colour is always the agent's role, as everywhere in the dashboard. Keys: `l` legend, `z` hides the header and footer, `p` replays the last 24 hours (again: back to live). A replay runs at a minute a second and skips stretches where nothing happened, so a day takes as many seconds as it had busy minutes; its clock is in the sub-header, and its rates (the disc, the wind) are per event second, so they read as they would have live. Below 110 columns the tab is a blossom, `5 ✿`.

It runs at 30 frames a second (springs need about that to look like springs; measured on a 140x40 terminal it takes about 6% of one core, idle or busy) and only while it is shown: the view starts `isshoni events --follow` when you open it and stops it when you leave. What had happened before you opened it sets the scene without animation: the last 30 minutes of sessions and agents, every open pull request and every unanswered question. An agent whose transcript was last written more than 30 minutes earlier and that is still running has no blossom until it next writes. Pull requests come from `gh`, polled once a minute, so a lantern or a firework can be up to a minute late; check states are known live only, so a replay's lanterns do not flicker. Without colour (`--no-color`) shapes and motion carry it; a terminal without truecolor gets the nearest colours it has.

```sh
bin/isshoni-tui --replay events.jsonl              # replay a recording (python3 -m isshoni events --since 24h > events.jsonl), looping
bin/isshoni-tui --replay events.jsonl --replay-speed 300  # event seconds per second; the default is 60
make strip REPLAY=internal/tui/testdata/hanami-replay.jsonl SCENE=merge AT=89s FRAMES=12 STEP=200ms ARGS="--replay-speed 3"
```

`make strip` renders consecutive frames (`--snapshot hanami --replay FILE --at D --frames N --frame-step D`, add `--legend` for the legend) to `shots/hanami-<scene>-<size>.png`, a contact strip, and `.gif`, an animation (the GIF needs Pillow; the PNG needs Chrome, as `make screenshot` does). `make snapshot VIEW=hanami` without a replay is the idle scene.

## Factory

You talk to one session, the dispatcher. For each task it starts a lead: a background session in its own worktree that writes a spec, builds through four subagents (explorer on Haiku, coder and lander on Sonnet, reviewer on Opus), has the result reviewed until it is approved, and signs off on the draft pull request its lander opens. A lead asks you only what it cannot decide itself, and nothing merges without you.

In Claude Code, in the repository you want work done in:

```
/plugin marketplace add andreiciobotar/isshoni
/plugin install factory@factory
/rename dispatcher-<repository directory name>
/factory:dispatch
```

Optionally, give the factory its own GitHub identity: `make wizard` (or `factory wizard`) creates a GitHub App for your account after one confirmation in the browser, has you install it on the repositories you choose, and checks that it works. From then on leads and their subagents push, open pull requests and commit as the App's bot instead of with your login (without it, they use yours), and your approval of a pull request is yours alone, since the pull request's author is not you. This is a default, not a boundary: the dispatcher session still acts with your login, and a lead's shell can still reach it, so no agent ever posts `lgtm` or approves. It needs `openssl`; see [Identity](docs/factory.md#identity).

Describe a task, then ask for progress whenever you like: the dispatcher answers from `factory status`, which lists every task with its phase and pull request. To try it for one session without installing, start `claude --plugin-dir <clone>/factory`.

It needs `git`, an authenticated [`gh`](https://cli.github.com) and Python 3.11+. By default leads run unattended on Fable in `auto` permission mode, and five tasks may be in motion per repository. Every session event, lead retrospective and decision a lead took without asking is logged under `~/.claude/factory/` (events for 12 months); `factory feedback` lists the decisions and what agents reported about the factory itself.

A site manager watches the factory for you: every 30 minutes, without running a model, it checks for stuck, blocked or over-limit tasks, red CI and the like, and only when something new turns up starts a short Sonnet run that tells the dispatcher or drops it as noise with a reason. It also merges for you: approve a handed-over pull request (an approving review on its head, or a comment `lgtm`) and, checks green and no conflict, the next check merges it. It starts by itself when a session becomes the dispatcher and stops when the last dispatcher session of the repository is gone. Once a day, when enough tasks have landed, it also sends the dispatcher a digest of where the money went (which token class, task, role or plugin version cost more than before), computed from the monitor's cost anatomy. It needs `claude` and `gh` on your `PATH` and runs on macOS (launchd) and Linux (a systemd user timer, else cron); Windows is not supported. To keep it running while your laptop sleeps, [docs/container.md](docs/container.md) puts the whole factory in a container on an always-on Linux host.

<!-- screenshot: factory status with several tasks -->

## More

- [docs/factory.md](docs/factory.md): the factory's commands, states, settings and log formats, and the site manager's criteria
- [docs/isshoni.md](docs/isshoni.md): the monitor's options, the dashboard's keys and how sessions map to tasks
- `make test` runs everything

## Status

Early and changing; built with itself. Source-available, not open source ([MIT with the Commons Clause](LICENSE)): use it, at work too, change it, [contribute](CONTRIBUTING.md). Don't sell it.
