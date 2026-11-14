# The factory on an always-on Linux host

For a machine that is always on: the dispatcher, its leads and the site manager live in one container, and `isshoni host add` sets it up from your laptop over the SSH access you already have. The server needs Docker, and an SSH user who can run it without sudo; nothing else is installed or changed on it.

The image ([docker/Dockerfile](../docker/Dockerfile)) holds tools only, each at a pinned version: Claude Code, `gh`, git, Python 3.13, Go, make, openssl, tmux and [supercronic](https://github.com/aptible/supercronic) on Debian 13 slim. It is 886 MB on x86-64 (Claude Code 365 MB, Go 240 MB) and builds in about 35 seconds on a GitHub runner; CI prints both on every run. It has no Chromium, so `make screenshot` does not work there.

## The command
Once, on the laptop, in a clone of this repository: `make wizard` creates the factory's GitHub App ([factory.md](factory.md#identity)). Then:

```sh
bin/isshoni host add nas --state /srv/factory --repo owner/name
```

| Flag | |
|---|---|
| `<target>` | handed to your own `ssh` as it is: an alias from `~/.ssh/config`, `user@host` or `ssh://user@host:port` |
| `--state DIR` | the one directory on the server that holds all state; required the first time, kept in the registry afterwards (a different value later is an error, not a move) |
| `--repo OWNER/NAME` | a repository to clone and start a dispatcher for; repeatable, and accumulates across runs |
| `--key PUBKEY_FILE` | the public key to authorise for the dashboard; default: the first `<identityfile>.pub` that `ssh -G <target>` lists |
| `--name NAME` | the host's name here; default: the target, every character outside `A-Za-z0-9_-` turned into `-` |
| `--dashboard-port PORT` | the server's loopback port for the dashboard; default 2222; like `--state`, fixed once recorded |
| `--alias ALIAS` | write a `Host` block with this name to `~/.ssh/config` (below) |

The laptop keeps its records in `~/.claude/factory/hosts.json` (`$FACTORY_HOME` moves it), mode 0600. Each step first looks at the server and does only what is missing, so **a rerun is also the update**: it ships `HEAD` of your clone again when the server has another revision, and resumes where an earlier run stopped. Each prints `[k/12] <step>: done` or `already`. What the steps do to the server:

1. **Prerequisites.** Checks, and reports everything missing at once: this clone is a git repository (what is shipped is `HEAD`, not uncommitted changes); the App's identity is on the laptop (otherwise `make wizard`); a public key; the server answers `ssh <target> true`, runs `docker` without sudo and has `docker compose`, is x86-64 or arm64, and the dashboard port is free (checked when the server has `ss`; otherwise it says so).
2. **State directory.** `mkdir -p` of `--state`, and the SSH user's user and group id, which the image takes so that files in it are yours.
3. **Image.** Built on the server from `git archive HEAD:docker` sent over SSH, labelled with the git tree id of `docker/`; skipped when the image carries that label. No registry, no clone on the server.
4. **Container.** `docker compose up -d --no-build` with [docker/compose.yaml](../docker/compose.yaml) from `HEAD`, which stays the one definition of mounts, limits and hardening. Recreated when the image changed.
5. **Source and plugin.** `HEAD` is extracted to `~/.isshoni` in the container (the tree is replaced), `make install` builds the dashboard, and the plugin is installed from it (`claude plugin marketplace add`, `claude plugin install factory@factory`; on a new revision the `update` forms).
6. **GitHub identity.** Copies the App's private key and `app.json` into the container's `~/.claude/factory/identity/` (directory 0700, files 0600); skipped when their checksums match.
7. **Claude login.** See below.
8. **Settings.** Merges `"monitor"` and `"dispatchers"` into the container's `~/.claude/factory/settings.json`; every other key is kept.
9. **Repositories.** `git clone https://github.com/owner/name` into `~/code/<name>`; a private repository is instead fetched as the App (`factory env`). The factory's own tooling lives apart, in `~/.isshoni`, so a repository of this name can be one of them.
10. **Dashboard key.** Lists your public key in `~/.claude/factory/dashboard/authorized_keys` with `write` (an existing line without it is upgraded). The container's start script serves the dashboard only if the file and the binary exist when the container starts; when it is not serving, `host add` starts it in place with `docker exec -d` and never restarts the container, which would stop the running sessions.
11. **Dispatchers.** For each repository with no `dispatcher-<name>` in `claude agents --json`: `claude --bg -n dispatcher-<name> --permission-mode auto "/factory:dispatch"` in `~/code/<name>`.
12. **Connect.** Prints how to reach the dashboard.

### The Claude login
Only you can do this, once. With `CLAUDE_CODE_OAUTH_TOKEN` set on the laptop, `host add` uses it. Otherwise it runs `claude setup-token` on the laptop, where you approve the login once in the browser, and asks you to paste the token it prints (hidden). The token is stored in `env` of the container's `~/.claude/settings.json` (0600) and `claude -p` there is run to check it.

- It is a **one-year token for your account that can only make model requests**: no Remote Control, no claude.ai connectors. Nothing else of your login is copied; `~/.claude/.credentials.json` is never read.
- It uses your **subscription**, so the factory's runs count against your plan's usage limits. Anthropic's terms describe subscription login as meant for ordinary individual use: see [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance), which is for you to read and not for this page to interpret.

## What the server holds, and what it can do in your name
- **The App's private key.** It mints tokens for **every repository of the App's installation**, not only the cloned ones. The key is in `<state>/home/.claude/factory/identity/`. Revoke by removing the App installation or the key in the App's GitHub settings.
- **The Claude token.** It spends your plan's usage for a year. Revoke it in your claude.ai settings.
- Both travel only on the standard input of `ssh ... docker exec -i`, never in a command line or an environment, and are written 0600; they are never printed or logged.

## Connect
The dashboard ([isshoni.md](isshoni.md#over-ssh)) stays on the server's loopback and is reached through the SSH access you already have: the last line of `host add` is

```
ssh -J nas -p 2222 -o HostKeyAlias=isshoni-nas 127.0.0.1
```

`HostKeyAlias` keeps the dashboard's host key apart from the server's own. With `--alias factory` (or a name typed at the prompt, which appears only when stdin is a terminal) `host add` appends this block to `~/.ssh/config`, once, and the line becomes `ssh factory`:

```
# isshoni host nas (added by `isshoni host add`)
Host factory
  HostName 127.0.0.1
  Port 2222
  ProxyJump nas
  HostKeyAlias isshoni-nas
# end isshoni host nas
```

A `Host factory` of your own is never touched: that is an error and nothing is written. Your key is a `write` key: it may also change the worktree cap; the dashboard is not a shell.

The dispatcher and the leads are background sessions in the container: `ssh -t nas docker exec -it isshoni-factory claude agents` lists them and attaches.

- The start script supervises the dashboard server it started: when it ends, a line `dashboard server ended (status N), restarting in 2 s` goes to `docker logs isshoni-factory` and it starts again after 2 s. A server that `host add` started in place (step 10) is not supervised until the container next restarts.
- At most 4 connections per source address and 32 in all can be unauthenticated at once ([isshoni.md](isshoni.md#over-ssh)).

## Status, remove, update
- `host list`: the recorded hosts, without network.
- `host status [NAME]`: over SSH, the container, the shipped revision against your `HEAD`, whether the dashboard serves, and which recorded dispatchers are in `claude agents --json`. Exit 1 when one is not running.
- `host remove NAME`: removes the container, the image, the alias block and the record. **The state directory is kept** and its path printed; `--purge-state` deletes it too. A server that does not answer is an error, and nothing local is removed.
- **Update** is `host add <target>` again.

## Known limits
- One factory container per server.
- The SSH user must run `docker` without sudo.
- After a container restart or a server reboot the dispatchers are gone until `host add <target>` is run again; it starts them fresh, without their earlier conversation. Transcripts, worktrees and declared states survive, and `factory status` still lists every task.
- The dispatcher runs in `auto` permission mode, because nobody is there to answer prompts.
- A server that no longer answers cannot be removed from the registry except by editing `hosts.json`.
- Not documented by Anthropic, and so only the first real run shows them: that the token reaches background sessions, that a `--bg` session on a fresh configuration needs no onboarding or trust answer, and that `"/factory:dispatch"` as the prompt of a `--bg` session starts the skill. Steps 7 and 11 verify what they can and say so when it does not hold.
- CI builds and runs the image on x86-64 only; the tests run `host add` against Docker with a stub in place of `claude`.

## State
Everything that must survive is under the state directory, mounted at `/state`. The rest of the container is read-only, apart from 64 MB of `/tmp` in memory.

```
<state>/
  home/                      the user's home, /state/home
    .claude/                 Claude Code: settings.json (with the token), transcripts (projects/), plugins/
      factory/               the factory's stores: logs, settings.json, specs/, watch.log
        identity/            the App's key and app.json
        dashboard/           the dashboard over SSH: authorized_keys, host_key
    .claude.json             Claude Code: account and per-project trust
    .config/gh/              gh's login, only if you made one: `host add` makes none
    .gitconfig
    .crontab                 the site manager's job
    code/<repository>/       the repositories; task worktrees under .claude/worktrees/
    .isshoni/                this repository's source and build, replaced on every update
    go/  .cache/             Go modules and build caches; isshoni's session cache
  tmp/                       temporary files (TMPDIR); safe to empty while nothing runs
```

- **Limits** are in [docker/compose.yaml](../docker/compose.yaml): 2 CPUs (`FACTORY_CPUS`), 4g of memory with no swap on top (`FACTORY_MEMORY`; at the limit the kernel kills a process inside the container, usually the largest; if that is supercronic, the container restarts), 1024 processes, a container log of 10 MB, restart unless stopped. Claude Code does not update itself in the container: change its version in the Dockerfile and rerun `host add`.
- **Recreating or restarting the container stops every session in it.** Rebuild when nothing is in motion; how well a lead cut off mid-turn resumes is not verified.
- **Undo**: `host remove`; the state directory is yours to keep or delete (`chmod -R u+w` first: Go's module cache is read-only, which `--purge-state` does for you).

## The site manager's schedule
A container has no systemd, and a cron daemon needs root. So the container's main process is supercronic, running as the user on `~/.crontab`, and `crontab` in the image is a small script over that file. `factory schedule` finds a `crontab` and uses its cron backend unchanged: the watch ticks every 2 minutes (it notices approvals each time and runs its health part every 30 minutes) and follows the dispatcher's life by the same rules as on any Linux host ([factory.md](factory.md#site-manager)). CI walks that whole life in the container, each tick fired by supercronic.

What differs from cron on a host:
- `crontab` knows only `-l` and `-` (what the factory uses). Edit `~/.crontab` by hand and supercronic does not notice until `pkill -USR2 supercronic` or the factory's next change.
- supercronic also writes a line per run to the container's log (`docker logs isshoni-factory`).
- After the container restarts, a job line left in `~/.crontab` runs once, finds no dispatcher and removes itself.
- There is no desktop: `dispatcher-missing` becomes a `notify: ...` line in `~/.claude/factory/watch.log`.

## What a confused lead could damage
Leads run unattended in `auto` permission mode as the container's only user. One that goes wrong:

- **can** change or delete everything under the state directory: every repository and worktree, the factory's logs, the Claude token and the App's key;
- **can** do on GitHub whatever the App may do, in every repository of its installation: push, merge, delete branches. Install the App on the factory's repositories only and protect their default branches;
- **can** reach the network: the internet, and whatever the host's network lets a container reach, other services on the host and the LAN included. There is no firewall;
- **can** fill the volume the state directory is on, and use CPU, memory and processes up to the limits;
- **cannot** see or change any other host path, talk to Docker, or become root (no capabilities, `no-new-privileges`, no `sudo`);
- **cannot** write to the image or to Docker's storage on the system disk, apart from the capped log.

## GitHub identity
Pull requests come from the App's bot, so you approve them with GitHub's Approve and `factory watch` merges on that ([factory.md](factory.md#identity)). `host add` copies the identity `make wizard` made: leads, their subagents and the script's own `gh` calls then act as the App in repositories of its owner, with a token scoped to the repository they run in ([factory.md](factory.md#how-the-identity-is-used)). There is no `gh` login in the container unless you make one yourself (`docker exec -it`); in repositories of other owners the factory falls back to it.

## Not verified
- Anything that needs a real Claude login: background sessions and their supervisor in the container (`factory new`, `claude attach`), `auto` permission mode, the site manager's Sonnet run.
- A real `gh` login, and the App's tokens minted from inside the container (the image has the `openssl` they need).
- An x86-64 server by hand: the end-to-end test passed locally on an arm64 Docker host, and x86-64 is what CI runs.
- Memory use of a working factory: the 4g default is a guess (Anthropic lists 4 GB as Claude Code's own requirement).
