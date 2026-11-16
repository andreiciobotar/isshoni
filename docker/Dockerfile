# The factory's image: Claude Code, gh, git, Python, Go, openssl, tmux and a cron that needs no root.
# It holds tools only. Everything that changes lives in the volume mounted at /state:
# see docs/container.md. Upgrade a tool by changing its version here (for the two images their digest,
# for supercronic its checksums too) and rebuilding.
FROM golang:1.27.4-trixie@sha256:b54b2639de8c1bfe538ec5ffc45f29b1932235ac5f6d63c059853646677112d9 AS go

FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
ARG TARGETARCH
ARG CLAUDE_VERSION=2.1.325
ARG GH_VERSION=2.104.0
ARG SUPERCRONIC_VERSION=0.2.51
ARG SUPERCRONIC_SHA1_amd64=1692066e350a88ff4d7d6474fe3c7a6c41ec5e89
ARG SUPERCRONIC_SHA1_arm64=9d4c2000493215a1898502b2fc2dada3e8ea8c7a
# The owner of the state directory on the host, so that files written there are the user's own.
ARG UID=1000
ARG GID=1000

RUN apt-get -o Acquire::Retries=3 update \
 && apt-get -o Acquire::Retries=3 install -y --no-install-recommends ca-certificates curl git less lsof make openssl procps python3 tmux \
 && rm -rf /var/lib/apt/lists/*

COPY --from=go /usr/local/go /usr/local/go

# Each download is checked against the checksum its publisher lists for that version.
RUN set -eu; \
 get() { curl -fsSL --retry 3 --retry-all-errors "$@" || { echo "network: download failed after 3 retries: $*; a network failure, not caused by the code" >&2; return 1; }; }; \
 cd /tmp \
 && claude_arch=$([ "$TARGETARCH" = amd64 ] && echo x64 || echo "$TARGETARCH") \
 && claude=https://downloads.claude.ai/claude-code-releases/$CLAUDE_VERSION \
 && get -o /usr/local/bin/claude "$claude/linux-$claude_arch/claude" \
 && get "$claude/manifest.json" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["platforms"][sys.argv[1]]["checksum"], sys.argv[2], sep="  ")' \
      "linux-$claude_arch" /usr/local/bin/claude | sha256sum -c - \
 && gh=https://github.com/cli/cli/releases/download/v$GH_VERSION \
 && get -O "$gh/gh_${GH_VERSION}_linux_$TARGETARCH.tar.gz" \
 && get "$gh/gh_${GH_VERSION}_checksums.txt" | grep " gh_${GH_VERSION}_linux_$TARGETARCH.tar.gz\$" | sha256sum -c - \
 && tar -xzf "gh_${GH_VERSION}_linux_$TARGETARCH.tar.gz" -C /usr/local/bin --strip-components=2 "gh_${GH_VERSION}_linux_$TARGETARCH/bin/gh" \
 && get -o /usr/local/bin/supercronic \
    "https://github.com/aptible/supercronic/releases/download/v$SUPERCRONIC_VERSION/supercronic-linux-$TARGETARCH" \
 && eval "sha1=\$SUPERCRONIC_SHA1_$TARGETARCH" \
 && echo "$sha1  /usr/local/bin/supercronic" | sha1sum -c - \
 && chmod +x /usr/local/bin/claude /usr/local/bin/supercronic \
 && rm -f /tmp/*

# In /usr/bin, where a crontab is on any Linux: the watch job runs with a minimal PATH that has it.
COPY --chmod=755 crontab /usr/bin/crontab
COPY --chmod=755 start /usr/local/bin/isshoni-start

# -o: the host's ids may be ones Debian already uses (a group like `users`).
RUN groupadd -o -g "$GID" factory && useradd -o -u "$UID" -g "$GID" -d /state/home -s /bin/bash factory
USER factory
WORKDIR /state
# Temporary files go to the state volume, outside the home: the root filesystem is read-only and /tmp
# is a small tmpfs.
# Claude Code stays at the version above until the image is rebuilt.
ENV PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    TMPDIR=/state/tmp \
    DISABLE_AUTOUPDATER=1

# The container's one long-lived process is the cron that runs the site manager's job, started by a
# script that first starts the dashboard over SSH if it is wanted; sessions are started with `docker exec`.
CMD ["isshoni-start"]
