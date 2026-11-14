.PHONY: install up build test fmt-check test-factory wizard snapshot screenshot strip pictures clean FORCE

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
	go mod download
	$(MAKE) build
	@if [ -n "$(CHROME)" ]; then echo "Chrome found: make screenshot is available."; else echo "Chrome or Chromium not found (optional): make screenshot needs one; install it or set CHROME=/path/to/chrome."; fi
	@echo "Ready: run make up, or $(BIN)."

# Build the dashboard and run it.
up: build
	$(BIN)

build:
	go build -o $(BIN) ./cmd/isshoni-tui

test: fmt-check test-factory
	python3 -m unittest discover -s tests
	go vet ./...
	go test ./...

fmt-check:
	@files=$$(gofmt -l $$(git ls-files -co --exclude-standard '*.go')); [ -z "$$files" ] || { echo "isshoni: gofmt would change these files:" >&2; echo "$$files" >&2; echo "Fix with: gofmt -w $$files" >&2; exit 1; }

test-factory:
	python3 -m unittest discover -s factory/tests

# Create and install the factory's own GitHub App, or verify the stored one (optional), e.g. make wizard ARGS="--owner my-org"
wizard:
	factory/bin/factory wizard $(ARGS)

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
