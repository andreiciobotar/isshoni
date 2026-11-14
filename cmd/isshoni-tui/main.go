// Command isshoni-tui is a live terminal dashboard over the isshoni CLI.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"

	"isshoni/internal/data"
	"isshoni/internal/tui"
)

func main() {
	var (
		projects = flag.String("projects-dir", "", "transcripts root (default ~/.claude/projects)")
		agents   = flag.String("agents-dir", "", "agent definitions dir, passed to isshoni")
		prices   = flag.String("prices", "", "price file, passed to isshoni (default isshoni/prices.toml)")
		rng      = flag.String("range", "7d", "initial range: 24h, 7d, 30d, all")
		snapshot = flag.String("snapshot", "", "render one frame to stdout and exit: dashboard, session, agents, factory, kaizen, kaizen-flow, kaizen-anatomy, kaizen-board, hanami, help")
		size     = flag.String("size", "140x40", "snapshot size WxH")
		session  = flag.String("session", "", "session id prefix for --snapshot session")
		row      = flag.Int("row", 0, "dashboard snapshot: cursor on table row N, 0 is the all row")
		fold     = flag.Bool("fold", false, "dashboard snapshot: collapse the repository the cursor is in")
		noColor  = flag.Bool("no-color", false, "plain rendering (also honors NO_COLOR)")
		follow   = flag.Bool("follow", false, "start in follow mode")
		replay   = flag.String("replay", "", "open the Hanami tab replaying this events file (JSON lines, as python3 -m isshoni events prints)")
		speed    = flag.Float64("replay-speed", 0, "replay: event seconds per second (default 60)")
		at       = flag.Duration("at", 0, "hanami snapshot: time of the first frame (default 4s)")
		frames   = flag.Int("frames", 1, "hanami snapshot: number of frames, separated by a form-feed line")
		step     = flag.Duration("frame-step", 0, "hanami snapshot: time between frames (default 400ms)")
		legend   = flag.Bool("legend", false, "hanami snapshot: with the legend open")
		serveAt  = flag.String("serve", "", "serve the dashboard over SSH on ADDR (e.g. 127.0.0.1:2222) instead of on this terminal; public keys only")
		keysFile = flag.String("authorized-keys", dashboardFile("authorized_keys"), "with --serve: the keys allowed in, one per line; `write ssh-ed25519 ...` may also change the worktree cap and open the dispatcher")
		hostKey  = flag.String("host-key", dashboardFile("host_key"), "with --serve: the server's private key, created when missing")
	)
	flag.Parse()

	root, rootErr := data.LocateRoot()
	// a missing root is not fatal here: the TUI reports it on screen
	r := &data.Runner{Root: root, ProjectsDir: absolute(*projects), AgentsDir: absolute(*agents), Prices: absolute(*prices), Err: rootErr}
	nc := *noColor || os.Getenv("NO_COLOR") != ""
	o := tui.Options{Runner: r, Range: *rng, NoColor: nc, Follow: *follow, ReplaySpeed: *speed}
	if *replay != "" {
		evs, err := data.ReadEvents(*replay)
		if err == nil && len(evs) == 0 {
			err = fmt.Errorf("%s holds no events", *replay)
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, "isshoni-tui:", err)
			os.Exit(1)
		}
		o.Replay = evs
	}

	if *serveAt != "" {
		if *snapshot != "" {
			fmt.Fprintln(os.Stderr, "isshoni-tui: --serve and --snapshot cannot be combined")
			os.Exit(2)
		}
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
		defer stop()
		ln, err := net.Listen("tcp", *serveAt)
		if err == nil {
			err = serve(ctx, ln, serveOptions{Runner: r, AuthorizedKeys: *keysFile, HostKey: *hostKey, Range: *rng, Follow: *follow, NoColor: *noColor, Log: log.New(os.Stderr, "isshoni-tui: ", log.LstdFlags)})
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, "isshoni-tui:", err)
			os.Exit(1)
		}
		return
	}
	if *snapshot != "" {
		w, h, err := parseSize(*size)
		if err != nil {
			fmt.Fprintln(os.Stderr, "isshoni-tui:", err)
			os.Exit(2)
		}
		switch *snapshot {
		case "dashboard", "session", "agents", "factory", "kaizen", "kaizen-flow", "kaizen-anatomy", "kaizen-board", "hanami", "help":
		default:
			fmt.Fprintln(os.Stderr, "isshoni-tui: unknown snapshot view", *snapshot)
			os.Exit(2)
		}
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
		defer stop()
		o.Ctx = ctx
		out, err := tui.Snapshot(tui.SnapshotOptions{Options: o, View: *snapshot, Session: *session, Row: *row, Fold: *fold, W: w, H: h, At: *at, FrameStep: *step, Frames: *frames, Legend: *legend})
		if err != nil {
			fmt.Fprintln(os.Stderr, "isshoni-tui:", err)
			os.Exit(1)
		}
		if ctx.Err() != nil { // a signal cut the render short: no partial frame
			fmt.Fprintln(os.Stderr, "isshoni-tui:", ctx.Err())
			os.Exit(1)
		}
		fmt.Println(out)
		return
	}
	if err := tui.Run(o); err != nil {
		fmt.Fprintln(os.Stderr, "isshoni-tui:", err)
		os.Exit(1)
	}
}

// dashboardFile is a file in the dashboard's state directory.
func dashboardFile(name string) string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".claude", "factory", "dashboard", name)
}

// absolute resolves a directory flag against the user's working directory, because
// the python side runs with the repo root as its own.
func absolute(dir string) string {
	if dir == "" {
		return ""
	}
	if abs, err := filepath.Abs(dir); err == nil {
		return abs
	}
	return dir
}

func parseSize(s string) (int, int, error) {
	a, b, ok := strings.Cut(strings.ToLower(s), "x")
	w, e1 := strconv.Atoi(a)
	h, e2 := strconv.Atoi(b)
	if !ok || e1 != nil || e2 != nil || w < 1 || h < 1 {
		return 0, 0, fmt.Errorf("invalid --size %q (want WxH)", s)
	}
	return w, h, nil
}
