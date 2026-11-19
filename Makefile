.PHONY: install up build test modules fmt-check fmt golden test-factory test-python test-go wizard wizard-remote reinstall uninstall uninstall-remote snapshot screenshot strip pictures clean FORCE

BIN := bin/isshoni-tui
VIEW ?= dashboard
SIZE ?= 140x40
CHROME ?= $(shell for c in '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' google-chrome google-chrome-stable chromium chromium-browser; do if [ -x "$$c" ]; then echo "$$c"; break; elif p=$$(command -v "$$c"); then echo "$$p"; break; fi; done)
SHOT := shots/$(VIEW)-$(SIZE)
SCENE ?= replay
AT ?= 12s
FRAMES ?= 8
STEP ?= 400ms
# FIXTURE=<name> renders tests/fixtures/<name> as the golden tests do: every file at 2026-01-01T10:30:00Z, times in UTC, all ranges, the fixture agents and prices.
FIXDIR := $(if $(FIXTURE),shots/fixture-$(FIXTURE))
FIXENV := $(if $(FIXTURE),TZ=UTC )
FIXARGS := $(if $(FIXTURE),--projects-dir $(FIXDIR) --agents-dir tests/fixtures/agents --prices tests/fixtures/prices.toml --range all)

shots/fixture-%: FORCE
	rm -rf shots/fixture-$*
	mkdir -p shots
	cp -R tests/fixtures/$* shots/fixture-$*
	find shots/fixture-$* -type f -exec env TZ=UTC touch -t 202601011030 {} +

# Get a fresh clone ready: check the toolchain, fetch the Go modules, build. Installs nothing system-wide.
install:
	@command -v go >/dev/null || { echo "isshoni: Go is missing. Install Go 1.27 or newer (https://go.dev/dl/ or your package manager), then run make install again." >&2; exit 1; }
	@command -v python3 >/dev/null || { echo "isshoni: python3 is missing. Install Python 3.11 or newer (https://www.python.org/downloads/ or your package manager), then run make install again." >&2; exit 1; }
	@python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "isshoni: Python 3.11 or newer is required. Install a newer one (https://www.python.org/downloads/ or your package manager)." >&2; exit 1; }
	$(MAKE) modules
	$(MAKE) build
	@if [ -n "$(CHROME)" ]; then echo "Chrome found: make screenshot is available."; else echo "Chrome or Chromium not found (optional): make screenshot needs one; install it or set CHROME=/path/to/chrome."; fi
	@echo "Ready: run make up, or $(BIN)."

# Build the dashboard and run it.
up: build
	$(BIN)

build:
	go build -o $(BIN) ./cmd/isshoni-tui

# Each suite runs through tools/noleft.py: it prints "left behind: N" and fails when a test left a process running.
NOLEFT := python3 tools/noleft.py

# The parts are independent, so CI runs them side by side: make -j3 test GOTESTFLAGS=-race
GOTESTFLAGS ?=
test: fmt-check test-factory test-python test-go

# Go modules, fetched once, up to 3 attempts, before any test that needs them. With GOPROXY=off nothing is fetched: one try, a local error.
modules:
	@if [ "$$(go env GOPROXY)" = off ]; then exec go mod download; fi; for i in 1 2 3; do go mod download && exit 0; [ $$i = 3 ] || sleep 2; done; echo "network: go mod download failed 3 times; a network failure, not caused by the code" >&2; exit 1

test-python: modules
	$(NOLEFT) python3 -m unittest discover -s tests

# Tests that run the monitor without --prices must not read the developer's own price file: a config home that never exists.
NOCONFIG := XDG_CONFIG_HOME=/nonexistent

test-go: modules
	go vet ./...
	$(NOCONFIG) $(NOLEFT) go test $(GOTESTFLAGS) ./...

fmt-check:
	@files=$$(gofmt -l $$(git ls-files -co --exclude-standard '*.go')); [ -z "$$files" ] || { echo "isshoni: gofmt would change these files:" >&2; echo "$$files" >&2; echo "Fix with: gofmt -w $$files" >&2; exit 1; }

# Write gofmt's changes to the Go files that fmt-check flags.
fmt:
	@files=$$(gofmt -l $$(git ls-files -co --exclude-standard '*.go')); [ -z "$$files" ] || gofmt -w $$files

# Regenerate the golden files of the dashboard views; review the diff.
golden:
	go test ./internal/tui -update

test-factory:
	$(NOCONFIG) $(NOLEFT) python3 -m unittest discover -s factory/tests

# Set everything up (dependencies, dashboard, plugin, settings, cost alerts, gh stack, GitHub App) and rerun to repair, e.g. make wizard ARGS="--yes --owner my-org"
wizard:
	factory/bin/factory wizard $(ARGS)

# Set up a server (asks for what is missing; same as bin/isshoni host add), e.g. make wizard-remote ARGS="me@server --state /srv/factory --repo owner/name"
wizard-remote:
	bin/isshoni host add $(ARGS)

# Bring every installation of the plugin to the marketplace's current main, at the scopes it has (acts through claude plugin, not on this clone)
reinstall:
	factory/bin/factory reinstall $(ARGS)

# Remove what the wizard and the factory put on this machine, e.g. make uninstall ARGS="--list"
uninstall:
	factory/bin/factory uninstall $(ARGS)

# Remove a server's install (asks which; same as bin/isshoni host remove), e.g. make uninstall-remote ARGS="NAME --purge-state"
uninstall-remote:
	bin/isshoni host remove $(ARGS)

# Render one frame to stdout without a TTY, e.g. make snapshot VIEW=session ARGS="--session 04ca0148 --no-color"
snapshot: build $(FIXDIR)
	$(FIXENV)$(BIN) --snapshot $(VIEW) --size $(SIZE) $(FIXARGS) $(ARGS)

# Render one colored frame to shots/<view>-<size>.png (needs Chrome or Chromium; set CHROME=... to pick one), e.g. make screenshot VIEW=session ARGS="--session 04ca0148"
screenshot: build $(FIXDIR)
	@[ -n "$(CHROME)" ] || { echo "isshoni: Chrome or Chromium is missing. Install one, or set CHROME=/path/to/chrome." >&2; exit 1; }
	@mkdir -p shots
	$(FIXENV)$(BIN) --snapshot $(VIEW) --size $(SIZE) $(FIXARGS) $(ARGS) | python3 tools/ansi2html.py > $(SHOT).html
	"$(CHROME)" --headless --disable-gpu --hide-scrollbars \
		--window-size=$$(( $(firstword $(subst x, ,$(SIZE))) * 9 + 16 )),$$(( $(lastword $(subst x, ,$(SIZE))) * 17 + 16 )) \
		--screenshot="$(CURDIR)/$(SHOT).png" "file://$(CURDIR)/$(SHOT).html" >/dev/null 2>&1
	@echo $(SHOT).png

# Frames of the Hanami scene as a contact strip and a GIF: shots/hanami-<scene>-<size>.png and .gif, e.g. make strip SCENE=spring REPLAY=events.jsonl AT=30s FRAMES=12 STEP=500ms SIZE=100x30
strip: build
	@[ -n "$(REPLAY)" ] || { echo "isshoni: set REPLAY=<events file>." >&2; exit 1; }
	@[ -n "$(CHROME)" ] || { echo "isshoni: Chrome or Chromium is missing. Install one, or set CHROME=/path/to/chrome." >&2; exit 1; }
	$(BIN) --snapshot hanami --replay $(REPLAY) --at $(AT) --frames $(FRAMES) --frame-step $(STEP) --size $(SIZE) $(ARGS) | python3 tools/strip.py --out shots/hanami-$(SCENE)-$(SIZE) --size $(SIZE) --chrome "$(CHROME)"

# Record a looping animated WebP of every tab from this repository's own data into $(OUT), e.g. make pictures SCENES="hanami" OUT=/tmp/tabs SIZE=100x30 (see docs/isshoni.md)
OUT ?= assets/tabs
SCENES ?=
pictures: build
	@[ -n "$(CHROME)" ] || { echo "isshoni: Chrome or Chromium is missing. Install one, or set CHROME=/path/to/chrome." >&2; exit 1; }
	python3 tools/record.py --out "$(OUT)" --scenes "$(SCENES)" --chrome "$(CHROME)"

clean:
	rm -rf $(BIN) shots
