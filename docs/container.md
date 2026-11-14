# The factory in a container

For a machine that is always on: the dispatcher, its leads and the site manager live in one container, and you reach them from a laptop or a phone. The host needs Docker and a user in the `docker` group; nothing else is installed or changed on it.

The image ([docker/Dockerfile](../docker/Dockerfile)) holds tools only, each at a pinned version: Claude Code, `gh`, git, Python 3.13, Go, make, openssl, tmux and [supercronic](https://github.com/aptible/supercronic) on Debian 13 slim. It is 886 MB on x86-64 (Claude Code 365 MB, Go 240 MB) and builds in about 35 seconds on a GitHub runner; CI prints both on every run. It has no Chromium, so `make screenshot` does not work there.

## Start it
On the host:

```sh
git clone https://github.com/andreiciobotar/isshoni && cd isshoni/docker
mkdir -p /srv/factory                 # yours: a directory Docker has to create would belong to root
printf 'FACTORY_STATE=/srv/factory\nFACTORY_UID=%s\nFACTORY_GID=%s\n' "$(id -u)" "$(id -g)" > .env
docker compose up -d --build
```

| `.env` key | Default | |
|---|---|---|
| `FACTORY_STATE` | required | the one host directory the container sees; all state lives in it |
| `FACTORY_UID`, `FACTORY_GID` | 1000 | the owner of that directory; baked into the image, so rebuild after changing them |
| `FACTORY_CPUS` | 2 | CPU limit |
| `FACTORY_MEMORY` | 4g | memory limit, with no swap on top; at the limit the kernel kills a process inside the container, usually the largest; if that is supercronic, the container restarts |

Also fixed in [docker/compose.yaml](../docker/compose.yaml): 1024 processes at most, a container log of 10 MB at most, restart unless stopped. The dashboard's two settings are in its own section below.

## First run
Only you can do these. In order:

1. Open a shell that outlives your connection: `docker exec -it isshoni-factory tmux new -A -s main` (leave with `ctrl-b d`; everything keeps running).
2. Log Claude Code in: run `claude`. With no browser it shows a login URL (`c` copies it); open it on any device and paste the code it gives back. `claude auth login` does the same from the shell. Use your subscription login, not `claude setup-token`: that token only makes model requests, so Remote Control would not work.
3. Give `gh` a token and let git use it: `gh auth login` (HTTPS, paste a token), then `gh auth setup-git`, then `git config --global user.name` and `user.email`. See GitHub identity below for which token.
4. Clone what the factory works on: `git clone https://github.com/<owner>/<repository> ~/code/<repository>`.
5. Clone this repository to `~/code/isshoni` and run `make wizard` there: it checks the logins of steps 2 and 3, installs the plugin, writes the settings with `"monitor"` (the cost criteria and the daily digest) and offers the GitHub App.
6. Start the dispatcher in the tmux shell: `cd ~/code/<repository> && claude`, then `/rename dispatcher-<repository>` and `/factory:dispatch`. Detach.

## Talking to the dispatcher
- **Recommended: SSH to the host, then `docker exec -it isshoni-factory tmux attach`.** It needs nothing beyond the SSH access you already have, and works from any SSH client, a phone's included. A lead is `claude attach <slug>` from that shell.
- **Claude Code's Remote Control** (`/remote-control` in the dispatcher session, then claude.ai or the mobile app): outbound HTTPS only, no port to open. It needs the subscription login of step 2. A good second door for a phone; documented by Anthropic, not tried here.
- **The dashboard over SSH**, read-only by default: see the next section. It is the dashboard only, not a shell.

## The dashboard over SSH
The container can serve `isshoni-tui` to any SSH client, for a phone or a laptop that has no `docker exec` ([isshoni.md](isshoni.md#over-ssh) has what it does). It needs the dashboard built (step 6) and a key:

1. Put a public key in `<FACTORY_STATE>/home/.claude/factory/dashboard/authorized_keys`, one per line (`mkdir -p` the directory first). A line that starts with `write ` (`write ssh-ed25519 AAAA... laptop`) may also change the worktree cap; every other key is read-only.
2. Restart the container: `docker compose restart`. (A container built before the dashboard existed has neither the start script nor the published port: run `docker compose up -d --build` once instead.) The start script runs `isshoni-tui --serve 0.0.0.0:2222` when the file is not empty and `~/code/isshoni/bin/isshoni-tui` exists.
3. Connect: `ssh -p 2222 HOST`, with `HOST` the machine the container runs on. The first time, `ssh` shows the server's fingerprint; compare it with the `serving ssh on ..., host key SHA256:...` line in `docker logs isshoni-factory`. The host key is created on that first start and kept in `<FACTORY_STATE>/home/.claude/factory/dashboard/host_key`.

| `.env` key | Default | |
|---|---|---|
| `DASHBOARD_BIND` | `127.0.0.1` | the host address the port is published on |
| `DASHBOARD_PORT` | 2222 | the host port |

- **The key file is the only gate**: the container has no firewall and no other login. By default the port is open on the host's loopback only, so you reach it with an SSH tunnel to the host (`ssh -L 2222:127.0.0.1:2222 HOST`, then `ssh -p 2222 127.0.0.1`) or by putting the address of a LAN or VPN interface in `DASHBOARD_BIND`. `0.0.0.0` there publishes it to everyone who can reach the host. Keys can be added and removed while it runs.
- **The port is published whether or not the dashboard is on**; with no key file nothing listens behind it. If host port 2222 is taken, set `DASHBOARD_PORT`: the container's own port stays 2222.
- **Off**: empty or remove the key file and restart the container.
- The server is not supervised: if it crashes it stays down until the container restarts. On `docker compose stop` it is killed with the container, not signalled, and holds no state to lose.
- It runs where the factory runs, as the container's user, so the Factory view's `factory status` sees the same tmux and processes; a `write` key changes `~/.claude/factory/settings.json` of that user.

## State
Everything that must survive is under `FACTORY_STATE`, mounted at `/state`. The rest of the container is read-only, apart from 64 MB of `/tmp` in memory.

```
<FACTORY_STATE>/
  home/                      the user's home, /state/home
    .claude/                 Claude Code: login (.credentials.json), settings, transcripts (projects/), plugins/
      factory/               the factory's stores: logs, settings.json, specs/, watch.log
        dashboard/           the dashboard over SSH: authorized_keys, host_key
    .claude.json             Claude Code: account and per-project trust
    .config/gh/              gh's token
    .gitconfig
    .crontab                 the site manager's job
    code/<repository>/       the repositories; task worktrees under .claude/worktrees/
    go/  .cache/             Go modules and build caches
  tmp/                       temporary files (TMPDIR); safe to empty while nothing runs
```

- **Upgrade or rebuild**: change a version in the Dockerfile (for supercronic its two checksums as well, for the base images their digest), then `docker compose up -d --build`. Nothing under `FACTORY_STATE` is touched. Claude Code does not update itself in the container.
- **Recreating or restarting the container stops every session in it**: the dispatcher and the leads are processes. Their transcripts, worktrees and declared states survive, and `factory status` still lists every task; start the dispatcher again (step 7, with `claude --continue`). How well a lead cut off mid-turn resumes is not verified, so rebuild when nothing is in motion.
- **Undo**: `docker compose down` removes the container; the state directory is yours to keep or delete (`chmod -R u+w` first: Go's module cache is read-only).

## The site manager's schedule
A container has no systemd, and a cron daemon needs root. So the container's main process is supercronic, running as the user on `~/.crontab`, and `crontab` in the image is a small script over that file. `factory schedule` finds a `crontab` and uses its cron backend unchanged: the watch ticks every 30 minutes and follows the dispatcher's life by the same rules as on any Linux host ([factory.md](factory.md#site-manager)). CI walks that whole life in the container, each tick fired by supercronic.

What differs from cron on a host:
- `crontab` knows only `-l` and `-` (what the factory uses). Edit `~/.crontab` by hand and supercronic does not notice until `pkill -USR2 supercronic` or the factory's next change.
- supercronic also writes a line per run to the container's log (`docker logs isshoni-factory`).
- After the container restarts, a job line left in `~/.crontab` runs once, finds no dispatcher and removes itself.
- There is no desktop: `dispatcher-missing` becomes a `notify: ...` line in `~/.claude/factory/watch.log`.

## What a confused lead could damage
Leads run unattended in `auto` permission mode as the container's only user. One that goes wrong:

- **can** change or delete everything under `FACTORY_STATE`: every repository and worktree, the factory's logs, the Claude login and the `gh` token;
- **can** do on GitHub whatever that token may do, in every repository it reaches: push, merge, delete branches. Scope the token to the factory's repositories and protect their default branches;
- **can** reach the network: the internet, and whatever the host's network lets a container reach, other services on the host and the LAN included. There is no firewall;
- **can** fill the volume `FACTORY_STATE` is on, and use CPU, memory and processes up to the limits;
- **cannot** see or change any other host path, talk to Docker, or become root (no capabilities, `no-new-privileges`, no `sudo`);
- **cannot** write to the image or to Docker's storage on the system disk, apart from the capped log.

## GitHub identity
With your own token the factory's pull requests are yours, and GitHub lets nobody approve their own: an `lgtm` comment stands in, and nothing technical tells yours from an agent's ([factory.md](factory.md#merging)). With an identity of its own you approve its pull requests with GitHub's Approve, which `factory watch` merges on.

- **A GitHub App** is the way: `factory wizard` creates, installs and verifies one ([factory.md](factory.md#identity): its permissions, files and undo). It keeps the App's key in `~/.claude/factory/identity/`, which is in the state directory, and so the per-repository token cache (`token-<name>.json`) the factory writes there. A read-only mount of that directory works: nothing can be cached, so each command then mints a token. The wizard cannot create the App from inside the container: GitHub sends your browser back to the wizard on `127.0.0.1`, and the container publishes no port. Run `factory wizard` on your laptop, copy `~/.claude/factory/identity` to `<FACTORY_STATE>/home/.claude/factory/identity`, and run `factory wizard` in the container: it verifies and prints the report.
- **Used by the factory**: with the identity in place, leads, their subagents and the script's own `gh` calls act as the App in repositories of its owner, in the container too, with a token scoped to the repository they run in ([factory.md](factory.md#how-the-identity-is-used)). Elsewhere, or with no identity, they use the `gh` login of step 3.
- **Fallback: a second GitHub account** with write access to the repositories and a fine-grained token for those only (Contents and Pull requests: read and write; Actions, Commit statuses, Metadata: read; Workflows: read and write if tasks may edit `.github/workflows`), given to `gh auth login`, its name and email in `git config --global`. GitHub takes it for a person, so set `"lgtm_from_author": false` in `~/.claude/factory/settings.json`: its own `lgtm` then stops counting.

## Not verified
- Anything that needs a real Claude login: logging in from the container, background sessions and their supervisor there (`factory new`, `claude attach`), `auto` permission mode, the site manager's Sonnet run, Remote Control.
- A real `gh` login, the token permissions of the fallback account, and `factory wizard` verifying a copied identity inside the container (the image has the `openssl` it needs).
- CI builds and runs the image on x86-64 only; an arm64 build passed the same steps (`make install`, `make test`, the walk with `WALK_DAEMON=1`) by hand, once, on a development machine.
- Memory use of a working factory: the 4g default is a guess (Anthropic lists 4 GB as Claude Code's own requirement).
