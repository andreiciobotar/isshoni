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
  see where every token went, and watch the factory improve with every task.
</p>

<p align="center">
  <a href="https://github.com/andreiciobotar/isshoni/actions/workflows/test.yml"><img src="https://github.com/andreiciobotar/isshoni/actions/workflows/test.yml/badge.svg" alt="tests"></a>
</p>

<p align="center">
  <img src="assets/tabs/dashboard.webp" alt="The dashboard: the cursor moves over a repository, its tasks and a session; cost, agent mix and dispatches follow the selected row">
</p>

Isshoni is two tools for people who run [Claude Code](https://claude.com/claude-code) on their repositories. Each works without the other.

| | What it does | You run |
|---|---|---|
| **Monitor** | Shows where the money and tokens went: per repository, task, session, agent and model | `isshoni`, `isshoni-tui` |
| **Factory** | Builds several tasks in parallel, each in its own git worktree, each ending in a draft pull request | `/factory:dispatch`, from the `factory` plugin |

## Setup

Needs `git` and Python 3.11+; the factory also needs Claude Code and an authenticated [`gh`](https://cli.github.com), on macOS or Linux.

Monitor:

```sh
git clone https://github.com/andreiciobotar/isshoni && cd isshoni
bin/isshoni sessions --since 7d    # where the money went
make install && make up            # live dashboard; needs Go 1.27+
```

Factory, in Claude Code, in the repository you want work done in:

```
/plugin marketplace add andreiciobotar/isshoni
/plugin install factory@factory
/rename dispatcher-<repository directory name>
/factory:dispatch
```

Describe a task; ask for progress whenever you like. By default leads run unattended on Fable in `auto` permission mode, five tasks at a time per repository ([settings](docs/factory.md#settings)).

Optional:

- [Its own GitHub identity](docs/factory.md#identity): `make wizard ARGS="--owner <owner of your repositories>"` in the clone. Pull requests then come from a bot, so you can approve them.
- [Always on](docs/container.md): the factory in a container on a Linux host.
- [Cost alerts and a daily digest](docs/factory.md#site-manager): set `"monitor": "<clone>/bin/isshoni"` in `~/.claude/factory/settings.json`.

## Factory

```mermaid
flowchart TD
    you([You]) <-- tasks, questions --> disp[Dispatcher]
    disp <-- brief, questions --> lead
    subgraph task [One per task, in its own worktree]
        lead[Lead · Fable] --> subs[explorer · Haiku<br>coder · Sonnet<br>reviewer · Opus<br>lander · Sonnet]
    end
    subs -- opens --> pr[Draft pull request]
    you -- approves --> pr
    sm[Site manager<br>every 30 min] -- alerts --> disp
    sm -- merges --> pr
```

A lead asks you only what it cannot decide itself. Nothing merges without you: approve a pull request with a review or a comment `lgtm`, and the site manager merges it once checks are green.

<p align="center">
  <img src="assets/tabs/factory.webp" alt="The Factory view: the pull request that waits for you, then every task with its phase, elapsed time and pull request, against the cap">
</p>

## Kaizen (改善), built in

Most agent setups run on day 100 the way they ran on day 1. Isshoni's factory learns from every task:

- Every agent reports what slowed it down.
- Every lead records the key calls it made without asking, and why.
- Every finished task leaves a retrospective and, in the monitor, its cost.
- Every site manager alert is marked acted-on or noise, so its precision and recall are measured.

The dispatcher reads it all and proposes edits to the factory's own rules; you decide.

<p align="center">
  <img src="assets/tabs/kaizen.webp" alt="The Kaizen view, page by page: Pulse (cost, time and review rounds per task), Flow (where money and time went), Anatomy (each release compared) and Board (open issues by topic)">
</p>

```mermaid
flowchart TD
    tasks[Every task] -. feedback, decisions,<br>retrospectives, cost .-> disp[Dispatcher]
    disp -. proposes edits .-> you([You])
    you -. accepts .-> rules[Rules for the next task]
```

On day one, agents reported the same shell friction eleven times and counting. It became a rule that day ([#8](https://github.com/andreiciobotar/isshoni/pull/8)).

## Monitor

`isshoni` reads the transcripts Claude Code already keeps in `~/.claude/projects` and never writes there. Beyond cost and tokens it shows the signs of wasted work: repeated dispatches, review loops, agents on a model other than the one pinned for them.

Cost is the API list price, from [isshoni/prices.toml](isshoni/prices.toml). `isshoni-tui` is the same data live, plus the factory's tasks and whether it is getting better. Its fifth view, [Hanami (花見)](docs/isshoni.md#hanami-花見-the-work-as-something-to-watch), draws the work as motion: something to leave on a second screen.

<p align="center">
  <img src="assets/tabs/hanami.webp" alt="Hanami: a day of the factory replayed; blossoms open where agents work and a merge streams away">
</p>

Every view, the Agents one too, and its keys: [docs/isshoni.md](docs/isshoni.md#dashboard-isshoni-tui).

## More

- [docs/factory.md](docs/factory.md): commands, states, settings, logs, the site manager
- [docs/isshoni.md](docs/isshoni.md): the monitor's options and the dashboard's keys
- `make test` runs everything

## Status

Early and changing; built with itself. Source-available, not open source ([MIT with the Commons Clause](LICENSE)): use it, at work too, change it, [contribute](CONTRIBUTING.md). Don't sell it.
