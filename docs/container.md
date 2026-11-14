# The factory in a container

For a machine that is always on: the dispatcher, its leads and the site manager live in one container, and you reach them from a laptop or a phone. The host needs Docker and a user in the `docker` group; nothing else is installed or changed on it.

The image ([docker/Dockerfile](../docker/Dockerfile)) holds tools only, each at a pinned version: Claude Code, `gh`, git, Python 3.13, Go, make, tmux and [supercronic](https://github.com/aptible/supercronic) on Debian 13 slim. It is IMAGE_SIZE (Claude Code 365 MB, Go 240 MB) and builds in BUILD_TIME on a GitHub runner. It has no Chromium, so `make screenshot` does not work there.

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

Also fixed in [docker/compose.yaml](../docker/compose.yaml): 1024 processes at most, a container log of 10 MB at most, restart unless stopped.

## First run
Only you can do these. In order:

1. Open a shell that outlives your connection: `docker exec -it isshoni-factory tmux new -A -s main` (leave with `ctrl-b d`; everything keeps running).
2. Log Claude Code in: run `claude`. With no browser it shows a login URL (`c` copies it); open it on any device and paste the code it gives back. `claude auth login` does the same from the shell. Use your subscription login, not `claude setup-token`: that token only makes model requests, so Remote Control would not work.
3. Give `gh` a token and let git use it: `gh auth login` (HTTPS, paste a token), then `gh auth setup-git`, then `git config --global user.name` and `user.email`. See GitHub identity below for which token.
4. Clone what the factory works on: `git clone https://github.com/<owner>/<repository> ~/code/<repository>`.
5. Install the plugin: `claude plugin marketplace add andreiciobotar/isshoni`, then `claude plugin install factory@factory`.
6. Optional, for the cost criteria and the daily digest: clone this repository to `~/code/isshoni`, run `make install` there, and set `"monitor": "/state/home/code/isshoni/bin/isshoni"` in `~/.claude/factory/settings.json`.
7. Start the dispatcher in the tmux shell: `cd ~/code/<repository> && claude`, then `/rename dispatcher-<repository>` and `/factory:dispatch`. Detach.

## Talking to the dispatcher
- **Recommended: SSH to the host, then `docker exec -it isshoni-factory tmux attach`.** It needs nothing beyond the SSH access you already have, and works from any SSH client, a phone's included. A lead is `claude attach <slug>` from that shell.
- **Claude Code's Remote Control** (`/remote-control` in the dispatcher session, then claude.ai or the mobile app): outbound HTTPS only, no port to open. It needs the subscription login of step 2. A good second door for a phone; documented by Anthropic, not tried here.
- Not built: an SSH server inside the container. Port 2222 is reserved in the compose file for the dashboard served over SSH, which is not built.

## State
Everything that must survive is under `FACTORY_STATE`, mounted at `/state`. The rest of the container is read-only, apart from 64 MB of `/tmp` in memory.

```
<FACTORY_STATE>/
  home/                      the user's home, /state/home
    .claude/                 Claude Code: login (.credentials.json), settings, transcripts (projects/), plugins/
      factory/               the factory's stores: logs, settings.json, specs/, watch.log
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
With your own token the factory's pull requests are yours, and GitHub lets nobody approve their own: an `lgtm` comment stands in, and nothing technical tells yours from an agent's ([factory.md](factory.md#merging)). Give the factory its own identity and you approve its pull requests with GitHub's Approve, which `factory watch` already merges on.

**Recommended: a GitHub App** that you create and install on the repositories the factory works on.
- Repository permissions: Contents, Pull requests: read and write; Checks, Commit statuses, Actions, Metadata: read; Workflows: read and write only if tasks may edit `.github/workflows`.
- It acts with installation tokens that expire after one hour, made from the App's private key. The key goes in the state directory at `~/.config/factory/github-app.pem` (mode 600). `gh` reads a token from `GH_TOKEN`, and git gets it from `gh` (`gh auth setup-git`), so nothing else in the container changes.
- Commits: `user.name` `<app-slug>[bot]`, `user.email` `<bot user id>+<app-slug>[bot]@users.noreply.github.com` (the id is in `gh api 'users/<app-slug>[bot]'`).
- Per GitHub's documentation, a pull request opened with an installation token triggers Actions workflows (unlike a workflow's own `GITHUB_TOKEN`), and only a pull request's author is barred from approving it, so you can approve the App's.
- The App is a bot to GitHub, so an `lgtm` or a review it posts never counts as approval: no setting needed.
- **Not built**: making and refreshing those tokens (task `act-as-github-app`). Until it lands, use the fallback.

**Fallback: a second GitHub account**, added to the repositories with write access, with a fine-grained token for those repositories only (Contents and Pull requests: read and write; Actions, Commit statuses, Metadata: read; Workflows as above) given to `gh auth login`, and its name and email in `git config --global`. It looks like a person to GitHub, so also set `"lgtm_from_author": false` in `~/.claude/factory/settings.json`: its own `lgtm` then stops counting.

## Not verified
- Anything that needs a real Claude login: logging in from the container, background sessions and their supervisor there (`factory new`, `claude attach`), `auto` permission mode, the site manager's Sonnet run, Remote Control.
- A real `gh` login, and every GitHub App statement above beyond what GitHub's documentation says.
- CI builds and runs the image on x86-64 only; an arm64 build passed the same steps (`make install`, `make test`, the walk with `WALK_DAEMON=1`) by hand, once, on a development machine.
- Memory use of a working factory: the 4g default is a guess (Anthropic lists 4 GB as Claude Code's own requirement).
