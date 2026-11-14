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

Needs `git`, Python 3.11+, [Go 1.27+](https://go.dev/dl) for the dashboard, and, for the factory, Claude Code and an authenticated [`gh`](https://cli.github.com), on macOS or Linux.

Monitor:

```sh
git clone https://github.com/andreiciobotar/isshoni && cd isshoni
bin/isshoni sessions --since 7d    # where the money went
make install && make up            # live dashboard; needs Go 1.27+
```

Factory: `make wizard` in the clone checks the tools, builds the dashboard, installs the plugin, writes the settings, offers cost alerts with the daily digest and offers the factory its own GitHub App ([what each step does](docs/factory.md#setup-and-uninstall)). Then, in Claude Code, in the repository you want work done in:

```
/rename dispatcher-<repository directory name>
/factory:dispatch
```

Describe a task; ask for progress whenever you like. By default leads run unattended on Fable in `auto` permission mode, five tasks at a time per repository, one of them held for emergencies ([settings](docs/factory.md#settings)).

Optional:

- [Always on](docs/container.md): `bin/isshoni host add <ssh target> --state <dir> --repo <owner/name>` runs the factory in a container on a Linux host that has Docker.

`make uninstall` removes what the wizard put on the machine.

## Factory

### Option M

```mermaid
flowchart LR
    you([You]) -- brief --> disp[Dispatcher · Fable]
    disp -- one lead per task --> lead
    subgraph task [one task · its own worktree]
        lead[Lead · Fable] --> ex[explorer · Haiku]
        lead --> co[coder · Sonnet]
        lead --> rv[reviewer · Opus]
    end
    ex & co & rv --> lander[Lander · Sonnet]
    lander -- tests, pushes --> pr[Pull request<br>draft → ready]
    pr -- review or lgtm --> you2([You])
    you2 -- lander merges --> main[main]
    watcher[watcher · every 2 min] -. comments, draft ↔ ready, starts lander .-> pr
    sm[site manager · Sonnet · every 30 min] -. escalates .-> disp
    classDef yellow stroke:#FFD34E,stroke-width:2px
    classDef teal stroke:#3FD8C4,stroke-width:2px
    classDef blue stroke:#5E9BFF,stroke-width:2px
    classDef pink stroke:#FF6FAE,stroke-width:2px
    classDef lime stroke:#ACD56E,stroke-width:2px
    classDef muted stroke:#8E8BA8,stroke-width:2px,stroke-dasharray:4
    class disp,lead yellow
    class ex teal
    class co blue
    class rv pink
    class lander,main lime
    class watcher,sm muted
```

```mermaid
flowchart LR
    tasks[every task: feedback, decisions,<br>retrospective, cost] -.-> disp[Dispatcher]
    disp -. proposes rule edits .-> you([You])
    you -. decide .-> rules[rules for the next task]
    rules -.-> next[next task]
    classDef yellow stroke:#FFD34E,stroke-width:2px
    class disp yellow
```

### Option S

<p align="center">
  <picture>
    <source media="(max-width: 600px)" srcset="assets/diagrams/factory-narrow.svg">
    <img src="assets/diagrams/factory-wide.svg" width="100%" alt="You give the dispatcher a brief. It starts one lead per task, in its own worktree; the lead dispatches an explorer, a coder and a reviewer until the reviewer approves. The lander tests and pushes a pull request that stays a draft until checks are green. You approve with a review or a comment lgtm, and the lander merges it to main. Every task leaves feedback, decisions, a retrospective and its cost; the dispatcher proposes rule edits, you decide, and the rules apply to the next task.">
  </picture>
</p>

A lead asks you only what it cannot decide itself. Nothing merges without you: approve a pull request with a review or a comment `lgtm`, and the lander merges it within a few minutes once checks are green. Your other comments on a pull request reach its lead on the next watch tick.

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

On day one, agents reported the same shell friction eleven times and counting. It became a rule that day ([#8](https://github.com/andreiciobotar/isshoni/pull/8)).

## Monitor

`isshoni` reads the transcripts Claude Code already keeps in `~/.claude/projects` and never writes there. Beyond cost and tokens it shows the signs of wasted work: repeated dispatches, review loops, agents on a model other than the one pinned for them.

Parsing a transcript is slow, so parsed sessions are cached, one file per transcript, in `$XDG_CACHE_HOME/isshoni/sessions` (else `~/.cache/isshoni/sessions`). Set `ISSHONI_CACHE_DIR` to another directory, or to an empty value to turn the cache off. An entry is dropped when its transcript or its `subagents/` files change, or when the parser or the price file does. Each run deletes the entries of transcripts that no longer exist, so the cache stays a small fraction of the transcripts' size; only a projects directory you stop using leaves its folder behind. Deleting the whole cache is always safe and costs one slow run. Entries are Python pickles, trusted like the code itself: the cache directory and its files are private to you (0700 and 0600), and anything that can write there can run code as you. The cache lives outside `~/.claude/projects`, which is still only read.

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
