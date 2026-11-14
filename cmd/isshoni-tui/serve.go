package main

import (
	"bufio"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"errors"
	"fmt"
	"io/fs"
	"log"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/ssh"
	"charm.land/wish/v2"
	"charm.land/wish/v2/activeterm"
	"charm.land/wish/v2/bubbletea"
	gossh "golang.org/x/crypto/ssh"

	"isshoni/internal/data"
	"isshoni/internal/tui"
)

// shutdownGrace bounds how long serve waits for session handlers after closing every
// connection.
const shutdownGrace = 5 * time.Second

// writeOption is the authorized_keys option that lets a key change the worktree cap.
const writeOption = "write"

// serveOptions configures serve. The Runner is shared by every session.
type serveOptions struct {
	Runner         *data.Runner
	AuthorizedKeys string
	HostKey        string
	Range          string
	Follow         bool
	NoColor        bool // the --no-color flag only: the server's own environment says nothing about a client's terminal
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
	program := bubbletea.MiddlewareWithProgramHandler(func(sess ssh.Session) *tea.Program {
		m := tui.New(tui.Options{Runner: s.o.Runner, Ctx: ctx, Range: s.o.Range, Follow: s.o.Follow, NoColor: s.o.NoColor, ReadOnly: !write})
		return tea.NewProgram(m, programOptions(sess)...)
	})
	activeterm.Middleware()(program(func(ssh.Session) {}))(sess)
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
