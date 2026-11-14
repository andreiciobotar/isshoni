package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"io"
	"log"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	charmlog "charm.land/log/v2"
	"github.com/charmbracelet/x/ansi"
	gossh "golang.org/x/crypto/ssh"

	"isshoni/internal/data"
)

// syncBuf is a buffer a goroutine writes while the test reads.
type syncBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuf) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuf) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func (s *syncBuf) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Len()
}

// squash is what a screen shows, whatever way the renderer drew it: no escape
// sequences, no whitespace.
func squash(s string) string {
	return strings.Join(strings.Fields(ansi.Strip(s)), "")
}

func waitFor(t *testing.T, what string, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func newSigner(t *testing.T) gossh.Signer {
	t.Helper()
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	s, err := gossh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// keyLine is the signer's authorized_keys line, with options in front of it.
func keyLine(s gossh.Signer, options string) string {
	line := strings.TrimSpace(string(gossh.MarshalAuthorizedKey(s.PublicKey()))) + " laptop\n"
	if options != "" {
		line = options + " " + line
	}
	return line
}

// testEnv is a stub runner, a HOME, and the files serve reads.
type testEnv struct {
	t      *testing.T
	stub   string // STUBDIR
	home   string
	runner *data.Runner
	keys   string
	host   string
	logs   *syncBuf
	starts int

	maxUnauthenticated, maxUnauthenticatedPerAddress int // serveOptions' limits; zero is the default
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	e := &testEnv{t: t, stub: t.TempDir(), home: t.TempDir(), logs: &syncBuf{}}
	t.Setenv("STUBDIR", e.stub)
	t.Setenv("HOME", e.home)
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	py, err := filepath.Abs("../../internal/data/testdata/fakepy.sh")
	if err != nil {
		t.Fatal(err)
	}
	e.runner = &data.Runner{Root: root, Python: py, ProjectsDir: e.stub, Timeout: 30 * time.Second}
	if err := e.runner.Check(); err != nil {
		t.Skipf("stub runner unusable: %v", err)
	}
	e.writeStub("sessions.json", `{"sessions":[{"id":"s1","project":"zebrafish","repo":"zebrafish","repo_path":"/r/zebrafish"}],"malformed":0}`)
	e.keys = filepath.Join(t.TempDir(), "authorized_keys")
	e.host = filepath.Join(t.TempDir(), "state", "host_key")
	return e
}

func (e *testEnv) writeStub(name, content string) {
	e.t.Helper()
	if err := os.WriteFile(filepath.Join(e.stub, name), []byte(content), 0o644); err != nil {
		e.t.Fatal(err)
	}
}

func (e *testEnv) allow(lines ...string) {
	e.t.Helper()
	if err := os.WriteFile(e.keys, []byte(strings.Join(lines, "")), 0o644); err != nil {
		e.t.Fatal(err)
	}
}

// stubCalls counts the python calls logged with the prefix.
func (e *testEnv) stubCalls(prefix string) int {
	b, _ := os.ReadFile(filepath.Join(e.stub, "calls"))
	n := 0
	for _, l := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(l, prefix) {
			n++
		}
	}
	return n
}

// start runs serve on a free loopback port and returns its address; stop cancels it and
// returns serve's result.
func (e *testEnv) start() (addr string, stop func() error) {
	e.t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		e.t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- serve(ctx, ln, serveOptions{Runner: e.runner, AuthorizedKeys: e.keys, HostKey: e.host, Range: "7d", Log: log.New(e.logs, "", 0),
			MaxUnauthenticated: e.maxUnauthenticated, MaxUnauthenticatedPerAddress: e.maxUnauthenticatedPerAddress})
	}()
	var once sync.Once
	var result error
	stop = func() error {
		once.Do(func() {
			cancel()
			select {
			case result = <-done:
			case <-time.After(15 * time.Second):
				result = errors.New("serve did not return")
			}
		})
		return result
	}
	e.t.Cleanup(func() { stop() })
	e.starts++
	waitFor(e.t, "the server's start line", func() bool { return strings.Count(e.logs.String(), "serving ssh on") >= e.starts })
	return ln.Addr().String(), stop
}

func dial(addr string, s gossh.Signer, hostKey func(gossh.PublicKey)) (*gossh.Client, error) {
	return gossh.Dial("tcp", addr, &gossh.ClientConfig{
		User: "viewer",
		Auth: []gossh.AuthMethod{gossh.PublicKeys(s)},
		HostKeyCallback: func(_ string, _ net.Addr, k gossh.PublicKey) error {
			if hostKey != nil {
				hostKey(k)
			}
			return nil
		},
		Timeout: 10 * time.Second,
	})
}

// viewer is one client session on a terminal.
type viewer struct {
	t      *testing.T
	client *gossh.Client
	sess   *gossh.Session
	in     io.WriteCloser
	out    *syncBuf
	exited chan error // the session's end
}

type viewOpts struct {
	term   string
	w, h   int
	env    map[string]string
	noPty  bool
	noSize bool // a terminal that reports 0x0, as ssh -tt does without a local terminal
	signer gossh.Signer
}

func (e *testEnv) view(addr string, o viewOpts) *viewer {
	e.t.Helper()
	if o.term == "" {
		o.term = "xterm-256color"
	}
	if o.w == 0 {
		o.w, o.h = 100, 30
	}
	c, err := dial(addr, o.signer, nil)
	if err != nil {
		e.t.Fatal(err)
	}
	e.t.Cleanup(func() { c.Close() })
	sess, err := c.NewSession()
	if err != nil {
		e.t.Fatal(err)
	}
	for k, v := range o.env {
		if err := sess.Setenv(k, v); err != nil {
			e.t.Fatalf("server refused env %s: %v", k, err)
		}
	}
	if !o.noPty {
		if o.noSize {
			o.w, o.h = 0, 0
		}
		if err := sess.RequestPty(o.term, o.h, o.w, gossh.TerminalModes{}); err != nil {
			e.t.Fatal(err)
		}
	}
	v := &viewer{t: e.t, client: c, sess: sess, out: &syncBuf{}, exited: make(chan error, 1)}
	stdout, err := sess.StdoutPipe()
	if err != nil {
		e.t.Fatal(err)
	}
	if v.in, err = sess.StdinPipe(); err != nil {
		e.t.Fatal(err)
	}
	go io.Copy(v.out, stdout)
	if err := sess.Shell(); err != nil {
		e.t.Fatal(err)
	}
	go func() { v.exited <- sess.Wait() }()
	return v
}

func (v *viewer) send(s string) {
	v.t.Helper()
	if _, err := io.WriteString(v.in, s); err != nil {
		v.t.Fatal(err)
	}
}

func (v *viewer) waitFor(text string) {
	v.t.Helper()
	want := squash(text)
	waitFor(v.t, "the screen to show "+text, func() bool { return strings.Contains(squash(v.out.String()), want) })
}

func (v *viewer) waitExit() error {
	v.t.Helper()
	select {
	case err := <-v.exited:
		return err
	case <-time.After(10 * time.Second):
		v.t.Fatal("the session did not end")
		return nil
	}
}

func TestServeAcceptedKeyGetsADashboard(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	addr, _ := e.start()

	v := e.view(addr, viewOpts{signer: s})
	v.waitFor("zebrafish")
	waitFor(t, "the session log line", func() bool {
		return regexp.MustCompile(`session start 127\.0\.0\.1:\d+ SHA256:\S+ read-only`).MatchString(e.logs.String())
	})
	v.send("q")
	if err := v.waitExit(); err != nil {
		t.Fatalf("q ends the session cleanly: %v", err)
	}
	waitFor(t, "the session end line", func() bool { return strings.Contains(e.logs.String(), "session end") })

	// no terminal: refused with a plain message, no dashboard
	bare := e.view(addr, viewOpts{signer: s, noPty: true})
	var exit *gossh.ExitError
	if err := bare.waitExit(); !errors.As(err, &exit) || exit.ExitStatus() != 1 {
		t.Fatalf("a session without a terminal must exit 1: %v", err)
	}
	if !strings.Contains(bare.out.String(), "Requires an active PTY") || strings.Contains(squash(bare.out.String()), "zebrafish") {
		t.Fatalf("output %q", bare.out.String())
	}
}

func TestServeRefusesUnlistedKeys(t *testing.T) {
	e := newTestEnv(t)
	good, bad := newSigner(t), newSigner(t)
	e.allow(keyLine(good, ""))
	addr, _ := e.start()

	var banners []string
	_, err := gossh.Dial("tcp", addr, &gossh.ClientConfig{
		User: "viewer",
		Auth: []gossh.AuthMethod{
			gossh.Password("hunter2"),
			gossh.KeyboardInteractive(func(string, string, []string, []bool) ([]string, error) { return nil, nil }),
			gossh.PublicKeys(bad),
		},
		HostKeyCallback: gossh.InsecureIgnoreHostKey(),
		BannerCallback:  func(m string) error { banners = append(banners, m); return nil },
		Timeout:         10 * time.Second,
	})
	if err == nil {
		t.Fatal("an unlisted key got in")
	}
	// the client skips a method the server does not offer: only publickey was on offer
	if !strings.Contains(err.Error(), "attempted methods [none publickey]") {
		t.Fatalf("methods offered: %v", err)
	}
	if len(banners) != 0 {
		t.Fatalf("a banner was sent: %q", banners)
	}
	waitFor(t, "the refusal log line", func() bool {
		return regexp.MustCompile(`refused key from 127\.0\.0\.1:\d+ SHA256:\S+`).MatchString(e.logs.String())
	})

	// the file is read on every attempt: a key added later gets in without a restart
	if _, err := dial(addr, bad, nil); err == nil {
		t.Fatal("still unlisted")
	}
	e.allow(keyLine(good, ""), keyLine(bad, ""))
	c, err := dial(addr, bad, nil)
	if err != nil {
		t.Fatalf("a key added to the file is refused: %v", err)
	}
	c.Close()
	// an unreadable file refuses everyone
	if err := os.Remove(e.keys); err != nil {
		t.Fatal(err)
	}
	if _, err := dial(addr, good, nil); err == nil {
		t.Fatal("a missing keys file must refuse everyone")
	}
}

func TestServeNeedsAnAuthorizedKeysFile(t *testing.T) {
	e := newTestEnv(t)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	err = serve(context.Background(), ln, serveOptions{Runner: e.runner, AuthorizedKeys: e.keys, HostKey: e.host, Log: log.New(io.Discard, "", 0)})
	if err == nil || !strings.Contains(err.Error(), e.keys) || !strings.Contains(err.Error(), "public key") {
		t.Fatalf("want a message naming the file and what to put in it: %v", err)
	}
}

func TestAuthorizedOptions(t *testing.T) {
	a, b, c := newSigner(t), newSigner(t), newSigner(t)
	path := filepath.Join(t.TempDir(), "keys")
	content := "# a comment\n\n" +
		keyLine(a, "write") +
		keyLine(b, `no-pty,command="x"`) +
		"not a key at all\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		s             gossh.Signer
		listed, write bool
	}{{a, true, true}, {b, true, false}, {c, false, false}} {
		listed, write := authorized(path, tc.s.PublicKey())
		if listed != tc.listed || write != tc.write {
			t.Errorf("listed=%v write=%v, want %v %v", listed, write, tc.listed, tc.write)
		}
	}
}

func TestServeTwoViewersShareOneCall(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	addr, _ := e.start()

	a := e.view(addr, viewOpts{signer: s})
	b := e.view(addr, viewOpts{signer: s})
	a.waitFor("zebrafish")
	b.waitFor("zebrafish")
	if n := e.stubCalls("sessions"); n != 1 {
		t.Fatalf("%d sessions processes for two viewers, want 1", n)
	}
}

// rule is the longest run of horizontal border the screen text draws: it grows with the
// width the panes were laid out for.
func rule(s string) int {
	longest, run := 0, 0
	for _, r := range ansi.Strip(s) {
		if r == '─' {
			run++
			longest = max(longest, run)
		} else {
			run = 0
		}
	}
	return longest
}

func TestServeEachViewerRendersAtItsOwnSize(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	addr, _ := e.start()

	a := e.view(addr, viewOpts{signer: s, w: 80, h: 24})
	b := e.view(addr, viewOpts{signer: s, w: 140, h: 40})
	c := e.view(addr, viewOpts{signer: s, w: 120, h: 30})
	for _, v := range []*viewer{a, b, c} {
		v.waitFor("zebrafish")
	}
	ra, rb, rc := rule(a.out.String()), rule(b.out.String()), rule(c.out.String())
	if ra >= rc || rc >= rb {
		t.Fatalf("panes laid out for 80, 120 and 140 columns drew rules of %d, %d and %d", ra, rc, rb)
	}
	before := a.out.Len()
	if err := a.sess.WindowChange(30, 120); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "A to redraw at the new width", func() bool { return rule(a.out.String()[before:]) == rc })
}

func TestServeATerminalWithoutASizeGetsTheDefault(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	addr, _ := e.start()

	v := e.view(addr, viewOpts{signer: s, noSize: true})
	v.waitFor("zebrafish")
	v.send("3")
	v.waitFor("worktree cap")
	v.send("q")
	if err := v.waitExit(); err != nil {
		t.Fatal(err)
	}
}

// A client that drops while on the Factory view must not take anything else down: wish
// sends a 0x0 resize as the window channel closes, which the Factory view cannot lay out.
func TestServeAbruptDisconnectLeavesTheOthersRunning(t *testing.T) {
	var wishLog syncBuf // wish logs a program that ended in an error, a recovered panic included
	charmlog.SetOutput(&wishLog)
	t.Cleanup(func() { charmlog.SetOutput(os.Stderr) })
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	addr, stop := e.start()

	a := e.view(addr, viewOpts{signer: s})
	b := e.view(addr, viewOpts{signer: s})
	a.waitFor("zebrafish")
	b.waitFor("zebrafish")
	a.send("3c")
	a.waitFor("worktree cap")
	a.sess.Close()
	a.client.Close()
	waitFor(t, "A's session to end", func() bool { return strings.Contains(e.logs.String(), "session end") })
	b.send("s")
	b.waitFor("sort: cost")
	c := e.view(addr, viewOpts{signer: s})
	c.waitFor("zebrafish")
	// the server's own shutdown closes the window channel of the viewers still on the Factory view
	b.send("3")
	b.waitFor("worktree cap")
	c.send("3")
	c.waitFor("worktree cap")
	if err := stop(); err != nil {
		t.Fatal(err)
	}
	if out := wishLog.String(); out != "" {
		t.Fatalf("a program ended in an error: %s", out)
	}
}

func TestServeColourFollowsTheClientTerminal(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	addr, _ := e.start()

	var sgr = regexp.MustCompile(`\x1b\[[0-9;]*m`)
	for _, tc := range []struct {
		name string
		o    viewOpts
		want func(out string) bool
	}{
		{"256 colours", viewOpts{term: "xterm-256color"}, func(out string) bool {
			return !strings.Contains(out, "38;2;") && !strings.Contains(out, "48;2;") && strings.Contains(out, "38;5;")
		}},
		{"truecolor", viewOpts{term: "xterm-256color", env: map[string]string{"COLORTERM": "truecolor"}}, func(out string) bool { return strings.Contains(out, "38;2;") || strings.Contains(out, "48;2;") }},
		{"dumb", viewOpts{term: "dumb"}, func(out string) bool { return !strings.Contains(out, "38;") && !strings.Contains(out, "48;") }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tc.o.signer = s
			v := e.view(addr, tc.o)
			v.waitFor("zebrafish")
			out := v.out.String()
			if !tc.want(out) {
				t.Fatalf("SGR sequences: %q", sgr.FindAllString(out, 20))
			}
		})
	}
}

func settingsPath(e *testEnv) string {
	return filepath.Join(e.home, ".claude", "factory", "settings.json")
}

func TestServeOnlyAWriteKeyChangesTheCap(t *testing.T) {
	e := newTestEnv(t)
	ro, rw := newSigner(t), newSigner(t)
	e.allow(keyLine(ro, ""), keyLine(rw, "write"))
	addr, _ := e.start()

	r := e.view(addr, viewOpts{signer: ro})
	r.waitFor("zebrafish")
	r.send("3c")
	r.waitFor("read-only: this key may not change the cap")
	r.send("6\r")
	if _, err := os.Stat(settingsPath(e)); err == nil {
		t.Fatal("a read-only key wrote the settings file")
	}
	waitFor(t, "the read-only mode in the log", func() bool { return strings.Contains(e.logs.String(), "read-only") })

	w := e.view(addr, viewOpts{signer: rw})
	w.waitFor("zebrafish")
	w.send("3c\x7f6\r")
	waitFor(t, "the cap in the settings file", func() bool {
		b, _ := os.ReadFile(settingsPath(e))
		return strings.Contains(string(b), `"worktree_cap": 6`)
	})
	if !strings.Contains(e.logs.String(), "write") {
		t.Fatalf("the log does not say the session may write: %s", e.logs.String())
	}
}

func TestServeQuitEndsOnlyThatViewer(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	addr, _ := e.start()

	a := e.view(addr, viewOpts{signer: s})
	b := e.view(addr, viewOpts{signer: s})
	a.waitFor("zebrafish")
	b.waitFor("zebrafish")
	a.send("q")
	if err := a.waitExit(); err != nil {
		t.Fatal(err)
	}
	b.send("s")
	b.waitFor("sort: cost")
	select {
	case err := <-b.exited:
		t.Fatalf("the other viewer ended too: %v", err)
	default:
	}
}

func TestServeShutdownClosesViewersAndLeavesNoProcess(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	e.writeStub("hang", "")
	addr, stop := e.start()

	a := e.view(addr, viewOpts{signer: s})
	b := e.view(addr, viewOpts{signer: s})
	waitFor(t, "a slow call in flight", func() bool { return e.stubCalls("sessions") >= 1 && e.stubCalls("agents") >= 1 })
	start := time.Now()
	if err := stop(); err != nil {
		t.Fatalf("serve: %v", err)
	}
	if time.Since(start) > shutdownGrace {
		t.Fatalf("shutdown took %s", time.Since(start))
	}
	for _, v := range []*viewer{a, b} {
		v.waitExit() // the connection closed: Wait returned, whatever the status
	}
	pids, err := os.ReadFile(filepath.Join(e.stub, "pids"))
	if err != nil || len(pids) == 0 {
		t.Fatalf("the stub logged no pids: %v", err)
	}
	for _, f := range strings.Fields(string(pids)) {
		pid, _ := strconv.Atoi(f)
		if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
			t.Errorf("process %d outlived the server: %v", pid, err)
		}
	}
}

func TestServeHostKeyIsCreatedOnceAndReused(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	var seen []string
	for range 2 {
		addr, stop := e.start()
		c, err := dial(addr, s, func(k gossh.PublicKey) { seen = append(seen, gossh.FingerprintSHA256(k)) })
		if err != nil {
			t.Fatal(err)
		}
		c.Close()
		if err := stop(); err != nil {
			t.Fatal(err)
		}
	}
	if len(seen) != 2 || seen[0] != seen[1] {
		t.Fatalf("host key changed between starts: %v", seen)
	}
	st, err := os.Stat(e.host)
	if err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("host key: %v, %v", st, err)
	}
	if dir, err := os.Stat(filepath.Dir(e.host)); err != nil || dir.Mode().Perm() != 0o700 {
		t.Fatalf("host key directory: %v, %v", dir, err)
	}
}

func TestLimiter(t *testing.T) {
	l := limiter{total: 3, perAddress: 2, byAddr: map[string]int{}}
	if !l.acquire("a") || !l.acquire("a") {
		t.Fatal("two from one address fit")
	}
	if l.acquire("a") {
		t.Fatal("a third from one address must not")
	}
	if !l.acquire("b") {
		t.Fatal("another address has its own allowance")
	}
	if l.acquire("c") {
		t.Fatal("the total is full")
	}
	l.release("a")
	if !l.acquire("c") {
		t.Fatal("a released slot is free again")
	}
	l.release("a")
	l.release("b")
	l.release("c")
	if l.n != 0 || len(l.byAddr) != 0 {
		t.Fatalf("after every release: n=%d, map %v", l.n, l.byAddr)
	}
}

// rawConn connects from the local address ("" for any) without speaking SSH. It says whether
// the server admitted the connection: an admitted one is sent the SSH banner at once, a
// refused one is closed before any byte.
func rawConn(t *testing.T, local, addr string) (c net.Conn, admitted bool) {
	t.Helper()
	d := net.Dialer{Timeout: 10 * time.Second}
	if local != "" {
		d.LocalAddr = &net.TCPAddr{IP: net.ParseIP(local)}
	}
	c, err := d.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	c.SetReadDeadline(time.Now().Add(10 * time.Second))
	buf := make([]byte, len("SSH-2.0-"))
	if _, err := io.ReadFull(c, buf); err != nil {
		if ne, ok := err.(net.Error); ok && ne.Timeout() {
			t.Fatalf("neither a banner nor a close: %v", err)
		}
		return c, false
	}
	if string(buf) != "SSH-2.0-" {
		t.Fatalf("first bytes %q", buf)
	}
	return c, true
}

// dialFrom is dial from a given local address.
func dialFrom(local, addr string, s gossh.Signer) (*gossh.Client, error) {
	d := net.Dialer{Timeout: 10 * time.Second, LocalAddr: &net.TCPAddr{IP: net.ParseIP(local)}}
	conn, err := d.Dial("tcp", addr)
	if err != nil {
		return nil, err
	}
	cc, chans, reqs, err := gossh.NewClientConn(conn, addr, &gossh.ClientConfig{
		User:            "viewer",
		Auth:            []gossh.AuthMethod{gossh.PublicKeys(s)},
		HostKeyCallback: gossh.InsecureIgnoreHostKey(),
		Timeout:         10 * time.Second,
	})
	if err != nil {
		conn.Close()
		return nil, err
	}
	return gossh.NewClient(cc, chans, reqs), nil
}

func TestServeBoundsUnauthenticatedConnectionsPerAddress(t *testing.T) {
	e := newTestEnv(t)
	e.maxUnauthenticated, e.maxUnauthenticatedPerAddress = 50, 3
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	addr, _ := e.start()

	admitted := 0
	for range 12 {
		if _, ok := rawConn(t, "", addr); ok {
			admitted++
		}
	}
	if admitted != 3 {
		t.Fatalf("%d of 12 connections from one address admitted, want 3", admitted)
	}
	if n := strings.Count(e.logs.String(), "that had not authenticated"); n != 1 {
		t.Fatalf("%d refusal lines for the flood, want 1: %s", n, e.logs.String())
	}
	if c, err := dial(addr, s, nil); err == nil {
		c.Close()
		t.Fatal("a listed key got in from an address whose allowance is used up")
	}
	c, err := dialFrom("127.0.0.2", addr, s)
	if err != nil {
		if strings.Contains(err.Error(), "can't assign requested address") {
			t.Skipf("no second loopback address here (%v); the rest of the test ran", err)
		}
		t.Fatalf("a second address must have its own allowance: %v", err)
	}
	c.Close()
}

func TestServeBoundsUnauthenticatedConnectionsInTotal(t *testing.T) {
	e := newTestEnv(t)
	e.maxUnauthenticated, e.maxUnauthenticatedPerAddress = 2, 10
	e.allow(keyLine(newSigner(t), ""))
	addr, _ := e.start()

	a, ok := rawConn(t, "", addr)
	if !ok {
		t.Fatal("the first connection was refused")
	}
	if _, ok := rawConn(t, "", addr); !ok {
		t.Fatal("the second connection was refused")
	}
	if _, ok := rawConn(t, "", addr); ok {
		t.Fatal("the third connection was admitted")
	}
	a.Close()
	waitFor(t, "a connection admitted after one closed", func() bool {
		_, ok := rawConn(t, "", addr)
		return ok
	})
}

func TestServeAuthenticatedConnectionsAreNotBounded(t *testing.T) {
	e := newTestEnv(t)
	e.maxUnauthenticated, e.maxUnauthenticatedPerAddress = 1, 10
	a, b := newSigner(t), newSigner(t)
	e.allow(keyLine(a, ""), keyLine(b, ""))
	addr, _ := e.start()

	first, err := dial(addr, a, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	second, err := dial(addr, b, nil)
	if err != nil {
		t.Fatalf("an authenticated connection holds a slot: %v", err)
	}
	second.Close()
}

func TestServeAFailedAuthenticationFreesItsSlot(t *testing.T) {
	e := newTestEnv(t)
	e.maxUnauthenticated, e.maxUnauthenticatedPerAddress = 1, 10
	good, bad := newSigner(t), newSigner(t)
	e.allow(keyLine(good, ""))
	addr, _ := e.start()

	if _, err := dial(addr, bad, nil); err == nil {
		t.Fatal("an unlisted key got in")
	}
	waitFor(t, "the listed key to get in", func() bool {
		c, err := dial(addr, good, nil)
		if err != nil {
			return false
		}
		c.Close()
		return true
	})
}
