package docker

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A stub records its pid and its start in a file under $STUBS once its TERM trap is in
// place, and a received TERM in another. It runs until it is told to end.
const stub = `#!/bin/sh
trap 'echo term >> "$STUBS/%[1]s.term"; exit 0' TERM
echo $$ >> "$STUBS/%[1]s.pids"
%[2]s
while :; do sleep 0.2 & wait $!; done
`

// cronExit makes the supercronic stub end by itself, with that status, once the server has started.
const cronExit = `if [ -n "${CRON_EXIT:-}" ]; then
	until [ -s "$STUBS/server.pids" ]; do sleep 0.1; done
	exit "$CRON_EXIT"
fi`

type run struct {
	t      *testing.T
	stubs  string
	cmd    *exec.Cmd
	stderr string // the file the script's stderr goes to
	done   chan error
}

// start runs docker/start under /bin/sh with a temporary HOME, a stub server (when keys) and a
// stub supercronic.
func start(t *testing.T, keys bool, env ...string) *run {
	t.Helper()
	dir := t.TempDir()
	home, stubs, bin := filepath.Join(dir, "home"), filepath.Join(dir, "stubs"), filepath.Join(dir, "bin")
	for _, d := range []string{stubs, bin, filepath.Join(home, "code", "isshoni", "bin"), filepath.Join(home, ".claude", "factory", "dashboard")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	write := func(path, content string, mode os.FileMode) {
		if err := os.WriteFile(path, []byte(content), mode); err != nil {
			t.Fatal(err)
		}
	}
	write(filepath.Join(home, "code", "isshoni", "bin", "isshoni-tui"), strings.NewReplacer("%[1]s", "server", "%[2]s", ":").Replace(stub), 0o755)
	write(filepath.Join(bin, "supercronic"), strings.NewReplacer("%[1]s", "cron", "%[2]s", cronExit).Replace(stub), 0o755)
	if keys {
		write(filepath.Join(home, ".claude", "factory", "dashboard", "authorized_keys"), "ssh-ed25519 AAAA test\n", 0o644)
	}
	r := &run{t: t, stubs: stubs, stderr: filepath.Join(dir, "stderr"), done: make(chan error, 1)}
	errFile, err := os.Create(r.stderr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { errFile.Close() })
	r.cmd = exec.Command("/bin/sh", "start")
	r.cmd.Env = append([]string{"HOME=" + home, "TMPDIR=" + filepath.Join(dir, "tmp"), "STUBS=" + stubs, "PATH=" + bin + ":/usr/bin:/bin"}, env...)
	r.cmd.Stderr = errFile
	r.cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := r.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() { r.done <- r.cmd.Wait() }()
	t.Cleanup(func() {
		// The whole group: the restart loop's subshell outlives the script when it is killed.
		syscall.Kill(-r.cmd.Process.Pid, syscall.SIGKILL)
		for _, f := range []string{"server.pids", "cron.pids"} {
			for _, pid := range r.pids(f) {
				syscall.Kill(pid, syscall.SIGKILL)
			}
		}
	})
	return r
}

func (r *run) read(name string) string {
	b, _ := os.ReadFile(filepath.Join(r.stubs, name))
	return string(b)
}

func (r *run) pids(name string) (pids []int) {
	for _, f := range strings.Fields(r.read(name)) {
		pid, _ := strconv.Atoi(f)
		pids = append(pids, pid)
	}
	return pids
}

func (r *run) waitFor(what string, ok func() bool) {
	r.t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			r.t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func (r *run) started(name string, n int) {
	r.t.Helper()
	r.waitFor(strconv.Itoa(n)+" "+name+" start(s)", func() bool { return len(r.pids(name+".pids")) >= n })
}

// exit returns the script's exit status.
func (r *run) exit() int {
	r.t.Helper()
	select {
	case err := <-r.done:
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			return ee.ExitCode()
		}
		if err != nil {
			r.t.Fatal(err)
		}
		return 0
	case <-time.After(15 * time.Second):
		r.t.Fatal("the script did not end")
		return -1
	}
}

// noStubLeft fails if a process a stub recorded is still there.
func (r *run) noStubLeft() {
	r.t.Helper()
	for _, name := range []string{"server.pids", "cron.pids"} {
		for _, pid := range r.pids(name) {
			if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
				r.t.Errorf("%s: process %d is still there: %v", name, pid, err)
			}
		}
	}
}

func TestServerIsRestartedWhenItDies(t *testing.T) {
	r := start(t, true)
	r.started("server", 1)
	if err := syscall.Kill(r.pids("server.pids")[0], syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	r.started("server", 2)
	b, _ := os.ReadFile(r.stderr)
	if !strings.Contains(string(b), "dashboard server ended (status 137), restarting in 2 s") {
		t.Fatalf("stderr: %q", b)
	}
	r.cmd.Process.Signal(syscall.SIGTERM)
	if status := r.exit(); status != 0 {
		t.Fatalf("exit status %d, want 0", status)
	}
	r.noStubLeft()
}

func TestSIGTERMStopsBothWithoutARestart(t *testing.T) {
	r := start(t, true)
	r.started("server", 1)
	r.started("cron", 1)
	r.cmd.Process.Signal(syscall.SIGTERM)
	if status := r.exit(); status != 0 {
		t.Fatalf("exit status %d, want 0", status)
	}
	for _, name := range []string{"server.term", "cron.term"} {
		if r.read(name) == "" {
			t.Errorf("%s: no SIGTERM received", name)
		}
	}
	if n := len(r.pids("server.pids")); n != 1 {
		t.Fatalf("%d server starts, want 1", n)
	}
	r.noStubLeft()
}

func TestNoKeyFileMeansOnlySupercronic(t *testing.T) {
	r := start(t, false)
	r.started("cron", 1)
	r.cmd.Process.Signal(syscall.SIGTERM)
	if status := r.exit(); status != 0 {
		t.Fatalf("exit status %d, want 0", status)
	}
	if r.read("server.pids") != "" {
		t.Fatal("the server was started without a key file")
	}
	r.noStubLeft()
}

func TestSupercronicEndingStopsTheServerAndSetsTheStatus(t *testing.T) {
	r := start(t, true, "CRON_EXIT=3")
	if status := r.exit(); status != 3 {
		t.Fatalf("exit status %d, want 3", status)
	}
	if r.read("server.term") == "" {
		t.Error("the server got no SIGTERM")
	}
	r.noStubLeft()
}
