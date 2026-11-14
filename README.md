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
  A software factory for Claude Code that gets better with every task.<br>
  Give it a brief, get back a reviewed pull request, and see where every token went on a live terminal dashboard.
</p>

<p align="center">
  <a href="https://github.com/andreiciobotar/isshoni/actions/workflows/test.yml"><img src="https://github.com/andreiciobotar/isshoni/actions/workflows/test.yml/badge.svg" alt="tests"></a>
</p>

<p align="center">
  <img src="assets/tabs/dashboard.webp" alt="The live terminal dashboard: the cursor moves over a repository, its tasks and a session; cost, agent mix and dispatches follow the selected row">
</p>

Isshoni is two tools for people who run [Claude Code](https://claude.com/claude-code) on their repositories. Each works without the other.

| | What it does | You run |
|---|---|---|
| **Monitor** | Shows where the money and tokens went: per repository, task, session, agent and model | `isshoni`, `isshoni-tui` |
| **Factory** | Orchestrates Claude Code agents: several tasks in parallel, each in its own git worktree, each ending in a reviewed pull request | `/factory:dispatch`, from the `factory` plugin |

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
/factory:dispatch
```

Describe a task; ask for progress whenever you like. By default leads run unattended on Fable in `auto` permission mode, five tasks at a time per repository, one of them held for emergencies, each against a time limit set by its size (45 min, 90 min or 2 h) ([settings](docs/factory.md#settings)).

Optional:

- [Always on](docs/container.md): `make wizard-remote` asks for what it needs and runs the factory in a container on a Linux host that has Docker. One SSH address shows the dashboard, and `d` there opens the dispatcher.

`make uninstall` removes what the wizard put on this machine, `make uninstall-remote` a server's install.

## The software factory

<p align="center">
  <picture>
    <source media="(max-width: 600px)" srcset="assets/diagrams/factory-narrow.svg">
    <img src="assets/diagrams/factory-wide.svg" width="100%" alt="You give the dispatcher a brief; it starts one lead per task in its own worktree. The lead has an explorer, a coder and a reviewer work until the reviewer approves. The lander tests and pushes a pull request, a draft until it is handed over, green and free of conflicts. You approve; a script merges approved pull requests to main in order. What conflicts or goes red there goes to a tech lead, which resolves it and hands over again, and asks the lead only if behaviour would change. A site manager watches and tells the dispatcher or a lead. Every task leaves feedback and a retrospective; the dispatcher proposes rule edits, you decide. Models are defaults.">
  </picture>
</p>

A lead asks you only what it cannot decide itself; comment on its pull request and it answers or acts. The pull request is a draft until it is handed over, green and free of conflicts. Nothing merges without you: approve with a review (or a comment `lgtm`), and a script merges what you approved, in order.

What the merge queue cannot merge cleanly goes to a tech lead (Opus): it resolves the conflict, notes it on the pull request and hands over again, and wakes the lead only when the resolution would change the behaviour that was asked for.

<p align="center">
  <img src="assets/tabs/factory.webp" alt="The Factory view: the pull request that waits for you, then every task with its phase, elapsed time and pull request, against the cap and its reserve">
</p>

## Kaizen (改善), built in

Most multi-agent setups run on day 100 the way they ran on day 1. Isshoni's factory is self-improving. On every task:

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

`isshoni` reads the transcripts Claude Code already keeps in `~/.claude/projects` and never writes there. Beyond cost and tokens it shows the signs of wasted work: repeated dispatches, review loops, agents on a model other than the one pinned for them. Parsed sessions are cached outside that directory ([cache](docs/isshoni.md#cache)).

Cost is the API list price, from [isshoni/prices.toml](isshoni/prices.toml). `isshoni-tui` is the same data live, plus the factory's tasks and whether it is getting better. Its fifth view, [Hanami (花見)](docs/isshoni.md#hanami-花見-the-work-as-something-to-watch), draws the work as motion: something to leave on a second screen.

<p align="center">
  <img src="assets/tabs/hanami.webp" alt="Hanami: a day of the factory replayed; blossoms open where agents work, and a merge bursts as a firework that lights the stage">
</p>

Every view, the Agents one too, and its keys: [docs/isshoni.md](docs/isshoni.md#dashboard-isshoni-tui).

## More

- [docs/factory.md](docs/factory.md): commands, states, settings, logs, the site manager
- [docs/isshoni.md](docs/isshoni.md): the monitor's options, the dashboard's keys and serving it over SSH
- [docs/shutdown.md](docs/shutdown.md): what ends each process and what each leaves behind
- `make test` runs everything

## Status

Early and changing; built with itself. Source-available, not open source ([MIT with the Commons Clause](LICENSE)): use it, at work too, change it, [contribute](CONTRIBUTING.md). Don't sell it.
