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
	"github.com/creack/pty"
	gossh "golang.org/x/crypto/ssh"

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
	srv, err := wish.NewServer(ssh.HostKeyPEM(hostPEM), wish.WithPublicKeyAuth(s.auth))
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
	var term *terminal
	defer func() {
		if term != nil {
			term.close()
		}
	}()
	program := bubbletea.MiddlewareWithProgramHandler(func(ssh.Session) *tea.Program { // the session without its window channel: not used
		o := tui.Options{Runner: s.o.Runner, Ctx: ctx, Range: s.o.Range, Follow: s.o.Follow, NoColor: s.o.NoColor, ReadOnly: !write}
		req, window, _ := sess.Pty()
		var err error
		if term, err = openTerminal(sess, ptySize(sess)); err != nil {
			s.o.Log.Printf("no terminal for %s: %v", who, err)
			return nil
		}
		if write {
			o.Attach = func(name string) tea.ExecCommand {
				return attachCommand{func() error { return s.attach(sess, req.Term, term.slave.Name(), name) }}
			}
		}
		p := tea.NewProgram(tui.New(o), append(programOptions(sess), tea.WithInput(term.slave), tea.WithOutput(term.slave))...)
		go term.follow(ctx, window, p)
		return p
	})
	activeterm.Middleware()(program(func(ssh.Session) {}))(noWindowChanges{sess})
}

// noWindowChanges keeps wish from reading the session's window changes: the channel has one
// reader, terminal.follow, which resizes the pty as well as telling the dashboard.
type noWindowChanges struct{ ssh.Session }

func (s noWindowChanges) Pty() (ssh.Pty, <-chan ssh.Window, bool) {
	pty, _, ok := s.Session.Pty()
	return pty, nil, ok
}

// terminal is the pseudo-terminal of one session: the dashboard and the attach client run on
// its slave, and a copy carries what they write from its master to the client. The ssh
// library's own pty copy (AllocatePty) is not joined and loses the last bytes of a quit, so
// the session owns the pty and waits for its copy.
type terminal struct {
	master *os.File
	slave  ttyFile
	copied chan struct{} // closed when everything the slave wrote has reached the client
}

// ttyFile is the slave as Bubble Tea gets it. RestoreTerminal starts a goroutine that calls Fd
// and that nothing joins, and os.File.Fd races with Close: so Fd is a plain number, and the
// descriptor can be closed when the session ends. Such a late call asks the kernel for a window
// size and finds nothing or something else's (a read-only question) and its answer goes to a
// program that has ended.
type ttyFile struct {
	*os.File
	fd uintptr
}

func (f ttyFile) Fd() uintptr { return f.fd }

func openTerminal(sess ssh.Session, size ssh.Window) (*terminal, error) {
	master, slave, err := pty.Open()
	if err != nil {
		return nil, err
	}
	if err := pty.Setsize(master, &pty.Winsize{Rows: uint16(size.Height), Cols: uint16(size.Width)}); err != nil {
		master.Close()
		slave.Close()
		return nil, err
	}
	t := &terminal{master, ttyFile{slave, slave.Fd()}, make(chan struct{})}
	go io.Copy(master, sess) // ends with the connection
	go func() {
		defer close(t.copied)
		io.Copy(sess, master)       // ends at the end of the slave's output, or when the connection fails
		io.Copy(io.Discard, master) // a dead connection must not leave the program blocked on a full pty
	}()
	return t, nil
}

// follow resizes the pty and tells the program whenever the client's window changes, until ctx ends.
func (t *terminal) follow(ctx context.Context, window <-chan ssh.Window, p *tea.Program) {
	for {
		select {
		case <-ctx.Done():
			return
		case w, ok := <-window:
			if !ok {
				return
			}
			if w.Width <= 0 || w.Height <= 0 { // a client without a window
				continue
			}
			pty.Setsize(t.master, &pty.Winsize{Rows: uint16(w.Height), Cols: uint16(w.Width)})
			p.Send(tea.WindowSizeMsg{Width: w.Width, Height: w.Height})
		}
	}
}

// close ends the slave and waits, up to 2 seconds, for the client to get the last bytes.
func (t *terminal) close() {
	t.slave.Close()
	select {
	case <-t.copied:
	case <-time.After(2 * time.Second):
	}
	t.master.Close()
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
func (s *server) attach(sess ssh.Session, term, tty, name string) (err error) {
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
	defer func() {
		if err != nil {
			who += ": " + err.Error()
		}
		s.o.Log.Printf("attach end %s", who)
	}()
	// its own open file description of the slave, so what exec does to the flags stays with it
	client, err := os.OpenFile(tty, os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		return err
	}
	defer client.Close()
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

// ptySize is the window the client asked for, 80x24 when it has none (ssh -tt without a local
// terminal reports 0x0).
func ptySize(sess ssh.Session) ssh.Window {
	if pty, _, _ := sess.Pty(); pty.Window.Width > 0 && pty.Window.Height > 0 {
		return pty.Window
	}
	return ssh.Window{Width: 80, Height: 24}
}

// programOptions is what wish makes of the session, with two repairs: a client that has no
// terminal size gets 80x24 (ptySize), and a 0x0 resize is dropped: the dashboard cannot lay out
// in no space.
func programOptions(sess ssh.Session) []tea.ProgramOption {
	opts := bubbletea.MakeOptions(sess)
	size := ptySize(sess)
	opts = append(opts, tea.WithWindowSize(size.Width, size.Height))
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
