package main

import (
	"bufio"
	"cmp"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync"
	"syscall"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/ssh"
	"charm.land/wish/v2"
	"charm.land/wish/v2/activeterm"
	"charm.land/wish/v2/bubbletea"
	gossh "golang.org/x/crypto/ssh"
	"golang.org/x/sys/unix"

	"isshoni/internal/data"
	"isshoni/internal/tui"
)

// shutdownGrace bounds how long serve waits for session handlers after closing every
// connection.
const shutdownGrace = 5 * time.Second

// writeOption is the authorized_keys option that lets a key change the worktree cap and open
// the dispatcher's conversation (a session that runs commands as this account).
const writeOption = "write"

// serveOptions configures serve. The Runner is shared by every session.
type serveOptions struct {
	Runner         *data.Runner
	AuthorizedKeys string
	HostKey        string
	Range          string
	Follow         bool
	NoColor        bool   // the --no-color flag only: the server's own environment says nothing about a client's terminal
	Claude         string // the claude binary key d attaches with; "claude" on PATH when empty
	Log            *log.Logger
}

// server holds what the session and auth handlers share.
type server struct {
	o serveOptions

	mu       sync.Mutex
	closing  bool
	handlers sync.WaitGroup // sessions in their handler
}

// serve gives every SSH connection accepted on ln its own dashboard over o.Runner, until
// ctx ends. It then closes every connection, waits (bounded) for the sessions and for
// every python process, and returns nil.
func serve(ctx context.Context, ln net.Listener, o serveOptions) error {
	defer ln.Close()
	if _, err := os.Stat(o.AuthorizedKeys); err != nil {
		return fmt.Errorf("authorized keys file %s: %w; create it with one SSH public key per line (the contents of an id_ed25519.pub), and put `write ` in front of a key that may change the worktree cap", o.AuthorizedKeys, errors.Unwrap(err))
	}
	hostPEM, fingerprint, err := loadHostKey(o.HostKey)
	if err != nil {
		return err
	}
	shareCtx, cancelShare := context.WithCancel(context.Background())
	defer cancelShare()
	o.Runner.Share = shareCtx
	s := &server{o: o}
	// a real pty: the attach client needs a terminal of its own, not the emulated one wish defaults to
	srv, err := wish.NewServer(ssh.HostKeyPEM(hostPEM), ssh.AllocatePty(), wish.WithPublicKeyAuth(s.auth))
	if err != nil {
		return err
	}
	srv.Handler = s.session
	srv.HandshakeTimeout = 30 * time.Second // before authentication only
	o.Log.Printf("serving ssh on %s, host key %s", ln.Addr(), fingerprint)

	served := make(chan error, 1)
	go func() { served <- srv.Serve(ln) }()
	select {
	case err := <-served:
		cancelShare()
		o.Runner.Wait()
		return err
	case <-ctx.Done():
	}
	s.mu.Lock()
	s.closing = true // a handshake that finishes from here on gets no session
	s.mu.Unlock()
	srv.Close()
	ln.Close()
	<-served
	handlersDone := make(chan struct{})
	go func() { s.handlers.Wait(); close(handlersDone) }()
	select {
	case <-handlersDone:
	case <-time.After(shutdownGrace):
		o.Log.Printf("sessions still open after %s, stopping anyway", shutdownGrace)
	}
	cancelShare()
	o.Runner.Wait()
	return nil
}

// loadHostKey returns the host key's PEM and SHA256 fingerprint, creating an ed25519 key
// (directory 0700, file 0600) when the file does not exist.
func loadHostKey(path string) ([]byte, string, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		b, err = createHostKey(path)
	}
	if err != nil {
		return nil, "", fmt.Errorf("host key %s: %w", path, err)
	}
	signer, err := gossh.ParsePrivateKey(b)
	if err != nil {
		return nil, "", fmt.Errorf("host key %s: %w", path, err)
	}
	return b, gossh.FingerprintSHA256(signer.PublicKey()), nil
}

func createHostKey(path string) ([]byte, error) {
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	block, err := gossh.MarshalPrivateKey(priv, "")
	if err != nil {
		return nil, err
	}
	b := pem.EncodeToMemory(block)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return nil, err
	}
	if _, err := f.Write(b); err != nil {
		f.Close()
		return nil, err
	}
	return b, f.Close()
}

// authorized reads the keys file afresh and says whether key is listed, and whether a
// line for it carries the write option. Lines that do not parse are skipped; a file that
// cannot be read lists no one.
func authorized(path string, key ssh.PublicKey) (listed, write bool) {
	f, err := os.Open(path)
	if err != nil {
		return false, false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(nil, 1<<20)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, _, opts, _, err := gossh.ParseAuthorizedKey([]byte(line))
		if err != nil || !ssh.KeysEqual(k, key) {
			continue
		}
		listed = true
		write = write || slices.Contains(opts, writeOption)
	}
	return listed, write
}

// auth is the public key handler: the key must be listed. Whether it may write travels
// with the connection's permissions.
func (s *server) auth(ctx ssh.Context, key ssh.PublicKey) bool {
	listed, write := authorized(s.o.AuthorizedKeys, key)
	if !listed {
		s.o.Log.Printf("refused key from %s %s", ctx.RemoteAddr(), gossh.FingerprintSHA256(key))
		return false
	}
	if write {
		p := ctx.Permissions()
		if p.Extensions == nil {
			p.Extensions = map[string]string{}
		}
		p.Extensions[writeOption] = ""
	}
	return true
}

// session runs one dashboard for one connection, and ends with it.
func (s *server) session(sess ssh.Session) {
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		return
	}
	s.handlers.Add(1)
	s.mu.Unlock()
	defer s.handlers.Done()

	_, write := sess.Permissions().Extensions[writeOption]
	mode := "read-only"
	if write {
		mode = "write"
	}
	who := fmt.Sprintf("%s %s %s", sess.RemoteAddr(), gossh.FingerprintSHA256(sess.PublicKey()), mode)
	s.o.Log.Printf("session start %s", who)
	defer s.o.Log.Printf("session end %s", who)

	// the viewer's calls end with its program, not only with its connection
	ctx, cancel := context.WithCancel(sess.Context())
	defer cancel()
	program := bubbletea.MiddlewareWithProgramHandler(func(sess ssh.Session) *tea.Program {
		o := tui.Options{Runner: s.o.Runner, Ctx: ctx, Range: s.o.Range, Follow: s.o.Follow, NoColor: s.o.NoColor, ReadOnly: !write}
		pty, _, _ := sess.Pty() // read once, here: the ssh library rewrites its window on every resize
		// The program gets its own descriptor for the slave: the library closes its own when the
		// connection ends, from another goroutine, while the program still reads and polls it.
		// Nothing closes this one but the garbage collector: Bubble Tea has a goroutine that may
		// still use it when the program returns.
		tty, err := dupFile(pty.Slave)
		if err != nil {
			s.o.Log.Printf("no terminal for %s: %v", who, err)
			return nil
		}
		if write {
			o.Attach = func(name string) tea.ExecCommand {
				return attachCommand{func() error { return s.attach(sess, pty.Term, tty, name) }}
			}
		}
		p := tea.NewProgram(tui.New(o), append(programOptions(sess), tea.WithInput(tty), tea.WithOutput(tty))...)
		go watchWindow(ctx, pty.Window, tty, p)
		return p
	})
	activeterm.Middleware()(program(func(ssh.Session) {}))(noWindowChanges{sess})
}

// noWindowChanges keeps wish from reading the session's window changes. With a real pty the
// ssh library reads them too, to resize the pty, and a channel with two readers hands each
// change to only one: the pty would miss every other resize. watchWindow tells the dashboard
// from the pty's own size instead.
type noWindowChanges struct{ ssh.Session }

func (s noWindowChanges) Pty() (ssh.Pty, <-chan ssh.Window, bool) {
	pty, _, ok := s.Session.Pty()
	return pty, nil, ok
}

// watchWindow sends the program the pty's size whenever it changes, until ctx ends.
func watchWindow(ctx context.Context, last ssh.Window, tty *os.File, p *tea.Program) {
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		var ws *unix.Winsize
		rc, err := tty.SyscallConn()
		if err != nil {
			return
		}
		rc.Control(func(fd uintptr) { ws, _ = unix.IoctlGetWinsize(int(fd), unix.TIOCGWINSZ) })
		if ws == nil || int(ws.Col) == last.Width && int(ws.Row) == last.Height {
			continue
		}
		last = ssh.Window{Width: int(ws.Col), Height: int(ws.Row)}
		p.Send(tea.WindowSizeMsg{Width: last.Width, Height: last.Height})
	}
}

// attachCommand is a tea.ExecCommand that runs on the session's own pty, so it ignores the
// streams tea.Exec hands it.
type attachCommand struct{ run func() error }

func (c attachCommand) Run() error        { return c.run() }
func (attachCommand) SetStdin(io.Reader)  {}
func (attachCommand) SetStdout(io.Writer) {}
func (attachCommand) SetStderr(io.Writer) {}

// attach runs `claude attach name` with the session's pty as its controlling terminal until it
// exits, so the kernel signals it on every resize. It ends the attach client only: the
// dispatcher's session belongs to its own supervisor. The client gets a SIGHUP when the
// connection ends, and a kill 2 seconds later if it stays.
func (s *server) attach(sess ssh.Session, term string, tty *os.File, name string) error {
	cmd := exec.CommandContext(sess.Context(), cmp.Or(s.o.Claude, "claude"), "attach", name)
	cmd.Env = append(os.Environ(), "TERM="+term)
	for _, kv := range sess.Environ() {
		if strings.HasPrefix(kv, "COLORTERM=") {
			cmd.Env = append(cmd.Env, kv)
		}
	}
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGHUP) }
	cmd.WaitDelay = 2 * time.Second
	who := fmt.Sprintf("%s %s %s", sess.RemoteAddr(), gossh.FingerprintSHA256(sess.PublicKey()), name)
	s.o.Log.Printf("attach start %s", who)
	defer s.o.Log.Printf("attach end %s", who)
	client, err := dupFile(tty) // exec reads the descriptor of what it is given: not the program's own
	if err != nil {
		return err
	}
	defer client.Close()
	// the duplicate shares the slave's open file description, which Go leaves non-blocking
	if err := unix.SetNonblock(int(client.Fd()), false); err != nil {
		return err
	}
	cmd.Stdin, cmd.Stdout, cmd.Stderr = client, client, client
	// Its own session with the pty as controlling terminal, so the kernel signals it on a resize.
	// macOS revokes the pty when that session's leader exits, which would end the dashboard's
	// own terminal with it: there the client runs without one and sees no resize.
	if runtime.GOOS != "darwin" {
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true}
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Wait()
}

// dupFile is a second descriptor for f, which the caller closes.
func dupFile(f *os.File) (*os.File, error) {
	rc, err := f.SyscallConn()
	if err != nil {
		return nil, err
	}
	var dup *os.File
	var dupErr error
	if err := rc.Control(func(fd uintptr) {
		n, err := unix.FcntlInt(fd, unix.F_DUPFD_CLOEXEC, 0)
		dup, dupErr = os.NewFile(uintptr(n), f.Name()), err
	}); err != nil {
		return nil, err
	}
	return dup, dupErr
}

// programOptions is what wish makes of the session, with two repairs. A client that has no
// terminal size (ssh -tt without a local terminal reports 0x0) gets 80x24. And a 0x0 resize,
// which wish also sends when the window channel closes at disconnect, is dropped: the
// dashboard cannot lay out in no space.
func programOptions(sess ssh.Session) []tea.ProgramOption {
	opts := bubbletea.MakeOptions(sess)
	if pty, _, _ := sess.Pty(); pty.Window.Width <= 0 || pty.Window.Height <= 0 {
		opts = append(opts, tea.WithWindowSize(80, 24))
	}
	// replaces wish's filter, which turns ctrl+z into a no-op instead of stopping the server's process
	return append(opts, tea.WithFilter(func(_ tea.Model, msg tea.Msg) tea.Msg {
		switch msg := msg.(type) {
		case tea.SuspendMsg:
			return tea.ResumeMsg{}
		case tea.WindowSizeMsg:
			if msg.Width <= 0 || msg.Height <= 0 {
				return nil
			}
		}
		return msg
	}))
}
