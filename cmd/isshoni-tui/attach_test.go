package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	gossh "golang.org/x/crypto/ssh"
)

// withDispatchers points the server at the stub claude and writes the settings file's list
// (nothing written for nil).
func (e *testEnv) withDispatchers(names []string) {
	e.t.Helper()
	claude, err := filepath.Abs("testdata/claude")
	if err != nil {
		e.t.Fatal(err)
	}
	e.claude = claude
	if names == nil {
		return
	}
	list, _ := json.Marshal(names) // a JSON list of strings is a TOML array
	body := []byte("[sessions]\ndispatchers = " + string(list) + "\n")
	if err := os.MkdirAll(filepath.Dir(settingsPath(e)), 0o755); err != nil {
		e.t.Fatal(err)
	}
	if err := os.WriteFile(settingsPath(e), body, 0o644); err != nil {
		e.t.Fatal(err)
	}
}

// waitAfter waits for text on the screen drawn after the first mark bytes of output.
func (v *viewer) waitAfter(mark int, text string) {
	v.t.Helper()
	want := squash(text)
	waitFor(v.t, "the screen to show "+text, func() bool { return strings.Contains(squash(v.out.String()[mark:]), want) })
}

// attachedPids are the processes the stub has run as, in order.
func (e *testEnv) attachedPids() []int {
	b, _ := os.ReadFile(filepath.Join(e.stub, "attach-pids"))
	var pids []int
	for _, f := range strings.Fields(string(b)) {
		pid, _ := strconv.Atoi(f)
		pids = append(pids, pid)
	}
	return pids
}

func (e *testEnv) attachedNames() []string {
	b, _ := os.ReadFile(filepath.Join(e.stub, "attached"))
	return strings.Fields(string(b))
}

func gone(pid int) bool { return errors.Is(syscall.Kill(pid, 0), syscall.ESRCH) }

func TestAttachGivesTheTerminalToTheDispatcherAndComesBack(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, "write"))
	e.withDispatchers([]string{"dispatcher-a"})
	addr, _ := e.start()

	v := e.view(addr, viewOpts{signer: s, term: "xterm-256color", w: 120, h: 36, env: map[string]string{"COLORTERM": "truecolor", "SECRET": "client-only"}})
	v.waitFor("zebrafish")
	v.send("d")
	v.waitFor("STUB ATTACHED dispatcher-a tty=yes opost=yes size=36 120 term=xterm-256color colorterm=truecolor")
	waitFor(t, "the attach start line", func() bool {
		return strings.Contains(e.logs.String(), "attach start") && strings.Contains(e.logs.String(), "dispatcher-a")
	})

	// what is typed while attached reaches the stub, not the dashboard (q would quit it)
	v.send("q\r")
	v.waitFor("STUB GOT q")
	select {
	case err := <-v.exited:
		t.Fatalf("q ended the session while attached: %v", err)
	default:
	}

	mark := v.out.Len()
	v.send("detach\r")
	v.waitAfter(mark, "zebrafish")
	waitFor(t, "the attach end line", func() bool { return strings.Contains(e.logs.String(), "attach end") })
	mark = v.out.Len()
	v.send("?") // a key typed after the return reaches the dashboard
	v.waitAfter(mark, "dashboard · agents · factory")
	v.send("?q") // closes the help, then quits
	if err := v.waitExit(); err != nil {
		t.Fatalf("q ends the session cleanly after coming back: %v", err)
	}
	if pids := e.attachedPids(); len(pids) != 1 {
		t.Fatalf("attach clients: %v", pids)
	} else {
		waitFor(t, "the attach client to be gone", func() bool { return gone(pids[0]) })
	}
}

func TestAttachFollowsEveryWindowChangeAndTheDashboardComesBackAtTheLastSize(t *testing.T) {
	if runtime.GOOS == "darwin" {
		t.Skip("macOS revokes the pty when a session leader exits, so the client runs without a controlling terminal and gets no SIGWINCH")
	}
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, "write"))
	e.withDispatchers([]string{"dispatcher-a"})
	addr, _ := e.start()

	wide := e.view(addr, viewOpts{signer: s, w: 160, h: 45})
	wide.waitFor("zebrafish")
	wantRule := rule(wide.out.String())

	v := e.view(addr, viewOpts{signer: s, w: 100, h: 30})
	v.waitFor("zebrafish")
	narrowRule := rule(v.out.String())
	if narrowRule >= wantRule {
		t.Fatalf("rules %d and %d do not tell the widths apart", narrowRule, wantRule)
	}
	v.send("d")
	v.waitFor("STUB ATTACHED dispatcher-a tty=yes opost=yes size=30 100")
	if err := v.sess.WindowChange(40, 130); err != nil {
		t.Fatal(err)
	}
	v.waitFor("STUB WINCH 40 130")
	if err := v.sess.WindowChange(45, 160); err != nil { // the dashboard is still in exec: the pty follows anyway
		t.Fatal(err)
	}
	v.waitFor("STUB WINCH 45 160")
	mark := v.out.Len()
	v.send("detach\r")
	waitFor(t, "the dashboard at the last width", func() bool { return rule(v.out.String()[mark:]) == wantRule })
}

func TestAttachEndsWithItsConnection(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, "write"))
	e.withDispatchers([]string{"dispatcher-a"})
	addr, stop := e.start()

	a := e.view(addr, viewOpts{signer: s})
	a.waitFor("zebrafish")
	a.send("d")
	a.waitFor("STUB ATTACHED")
	a.client.Close()
	waitFor(t, "the attach client to get its hangup", func() bool {
		_, err := os.Stat(filepath.Join(e.stub, "hup"))
		return err == nil
	})
	pids := e.attachedPids()
	if len(pids) != 1 {
		t.Fatalf("attach clients: %v", pids)
	}
	waitFor(t, "the attach client to be gone", func() bool { return gone(pids[0]) })

	// the server serves on, and its own shutdown ends an attach too
	b := e.view(addr, viewOpts{signer: s})
	b.waitFor("zebrafish")
	b.send("d")
	waitFor(t, "a second attach client", func() bool { return len(e.attachedPids()) == 2 })
	b.waitFor("STUB ATTACHED")
	start := time.Now()
	if err := stop(); err != nil {
		t.Fatalf("serve: %v", err)
	}
	if time.Since(start) > shutdownGrace {
		t.Fatalf("shutdown took %s", time.Since(start))
	}
	waitFor(t, "the second attach client to be gone", func() bool { return gone(e.attachedPids()[1]) })
}

func TestAttachIsRefusedForAReadOnlyKey(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, ""))
	e.withDispatchers([]string{"dispatcher-a"})
	addr, _ := e.start()

	v := e.view(addr, viewOpts{signer: s})
	v.waitFor("zebrafish")
	v.send("d")
	v.waitFor("read-only key: no dispatcher")
	if names := e.attachedNames(); len(names) != 0 {
		t.Fatalf("the stub ran for a read-only key: %v", names)
	}
}

func TestAttachNeedsADispatcherInTheSettings(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, "write"))
	e.withDispatchers(nil)
	addr, _ := e.start()

	v := e.view(addr, viewOpts{signer: s, w: 160, h: 36})
	v.waitFor("zebrafish")
	v.send("d")
	v.waitFor(`no dispatcher: "dispatchers" is`)
	if names := e.attachedNames(); len(names) != 0 {
		t.Fatalf("the stub ran without a dispatcher: %v", names)
	}
}

func TestAttachOffersSeveralDispatchersAndNeverAnOption(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, "write"))
	e.withDispatchers([]string{"dispatcher-a", "--version", "dispatcher-b"})
	addr, _ := e.start()

	v := e.view(addr, viewOpts{signer: s})
	v.waitFor("zebrafish")
	mark := v.out.Len()
	v.send("d")
	v.waitAfter(mark, "2 dispatcher-b")
	if out := squash(v.out.String()[mark:]); !strings.Contains(out, "1dispatcher-a") || strings.Contains(out, "version") {
		t.Fatalf("the picker: %q", out)
	}
	v.send("2")
	v.waitFor("STUB ATTACHED dispatcher-b")
	if names := e.attachedNames(); len(names) != 1 || names[0] != "dispatcher-b" {
		t.Fatalf("the stub ran for %v", names)
	}
}

func TestAttachFailureComesBackWithAToast(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, "write"))
	e.withDispatchers([]string{"dispatcher-a"})
	addr, _ := e.start()

	v := e.view(addr, viewOpts{signer: s, w: 160, h: 36})
	v.waitFor("zebrafish")
	v.send("d")
	v.waitFor("STUB ATTACHED dispatcher-a")
	mark := v.out.Len()
	v.send("fail\r")
	v.waitAfter(mark, "dispatcher-a: not running?")
	v.send("q")
	var exit *gossh.ExitError
	if err := v.waitExit(); errors.As(err, &exit) {
		t.Fatalf("the dashboard is back, so q ends it cleanly: %v", err)
	}
}

func TestAttachWithoutClaudeOnThePathComesBackWithAToast(t *testing.T) {
	e := newTestEnv(t)
	s := newSigner(t)
	e.allow(keyLine(s, "write"))
	e.withDispatchers([]string{"dispatcher-a"})
	e.claude = "claude-is-missing"
	addr, _ := e.start()

	v := e.view(addr, viewOpts{signer: s, w: 160, h: 36})
	v.waitFor("zebrafish")
	mark := v.out.Len()
	v.send("d")
	v.waitAfter(mark, "dispatcher-a: exec:")
	v.send("q")
	var exit *gossh.ExitError
	if err := v.waitExit(); errors.As(err, &exit) {
		t.Fatalf("the dashboard is back, so q ends it cleanly: %v", err)
	}
}
