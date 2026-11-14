package data

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// sharedStub is a stub-backed runner in serve mode; the shared runs live until the test ends.
func sharedStub(t *testing.T) (*Runner, string) {
	t.Helper()
	r := stubRunner(t)
	ctx, cancel := context.WithCancel(context.Background())
	r.Share = ctx
	t.Cleanup(func() { cancel(); r.Wait() })
	return r, os.Getenv("STUBDIR")
}

// calls counts the stub calls logged with the given prefix.
func calls(t *testing.T, dir, prefix string) int {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, "calls"))
	if errors.Is(err, os.ErrNotExist) {
		return 0
	}
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, l := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(l, prefix) {
			n++
		}
	}
	return n
}

func TestShareConcurrentIdenticalCallsRunOneProcess(t *testing.T) {
	r, dir := sharedStub(t)
	repo := t.TempDir()
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			if _, err := r.Agents(context.Background(), "7d"); err != nil {
				t.Error(err)
			}
			if _, err := r.FactoryStatus(context.Background(), repo, false); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if n := calls(t, dir, "agents"); n != 1 {
		t.Fatalf("%d agents processes, want 1", n)
	}
	if n := calls(t, dir, "factory"); n != 1 {
		t.Fatalf("%d factory processes, want 1", n)
	}
	if _, err := r.Agents(context.Background(), "30d"); err != nil {
		t.Fatal(err)
	}
	if n := calls(t, dir, "agents"); n != 2 {
		t.Fatalf("a different range is a different call: %d processes, want 2", n)
	}
}

func TestShareEachCallerDecodesItsOwnCopy(t *testing.T) {
	r, _ := sharedStub(t)
	a, err := r.Session(context.Background(), "x")
	if err != nil {
		t.Fatal(err)
	}
	b, err := r.Session(context.Background(), "x")
	if err != nil {
		t.Fatal(err)
	}
	if a == b {
		t.Fatal("two callers got one struct")
	}
}

func TestShareTranscriptTouchRerunsSessionsNotAgents(t *testing.T) {
	r, dir := sharedStub(t)
	ctx := context.Background()
	transcript := filepath.Join(dir, "proj", "s1.jsonl")
	if err := os.MkdirAll(filepath.Dir(transcript), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(transcript, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// the transcript is older than any run below
	old := time.Now().Add(-time.Minute)
	os.Chtimes(transcript, old, old)
	for range 2 {
		if _, err := r.Sessions(ctx, ""); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Agents(ctx, ""); err != nil {
			t.Fatal(err)
		}
	}
	if s, a := calls(t, dir, "sessions"), calls(t, dir, "agents"); s != 1 || a != 1 {
		t.Fatalf("sessions=%d agents=%d, want 1 each", s, a)
	}
	now := time.Now()
	os.Chtimes(transcript, now, now)
	if _, err := r.Sessions(ctx, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Agents(ctx, ""); err != nil {
		t.Fatal(err)
	}
	if s, a := calls(t, dir, "sessions"), calls(t, dir, "agents"); s != 2 || a != 1 {
		t.Fatalf("after a touch: sessions=%d agents=%d, want 2 and 1", s, a)
	}
}

func TestShareForgetForcesARerun(t *testing.T) {
	r, dir := sharedStub(t)
	ctx := context.Background()
	repo := t.TempDir()
	for range 2 {
		if _, err := r.FactoryStatus(ctx, repo, false); err != nil {
			t.Fatal(err)
		}
	}
	if n := calls(t, dir, "factory"); n != 1 {
		t.Fatalf("%d runs before Forget, want 1", n)
	}
	r.Forget()
	if _, err := r.FactoryStatus(ctx, repo, false); err != nil {
		t.Fatal(err)
	}
	if n := calls(t, dir, "factory"); n != 2 {
		t.Fatalf("%d runs after Forget, want 2", n)
	}
}

func TestForgetWithoutShareIsHarmless(t *testing.T) {
	r := stubRunner(t)
	r.Forget()
	if _, err := r.Agents(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Agents(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if n := calls(t, os.Getenv("STUBDIR"), "agents"); n != 2 {
		t.Fatalf("without Share every call runs: %d", n)
	}
}

func TestShareFailedRunIsNotReused(t *testing.T) {
	r, dir := sharedStub(t)
	ctx := context.Background()
	fail := filepath.Join(dir, "fail")
	os.WriteFile(fail, nil, 0o644)
	if _, err := r.Agents(ctx, ""); err == nil {
		t.Fatal("want the failure")
	}
	os.Remove(fail)
	if _, err := r.Agents(ctx, ""); err != nil {
		t.Fatalf("a failed run was kept: %v", err)
	}
	if n := calls(t, dir, "agents"); n != 2 {
		t.Fatalf("%d runs, want 2", n)
	}
}

// waitCalls polls until the stub has logged n calls with the prefix.
func waitCalls(t *testing.T, dir, prefix string, n int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for calls(t, dir, prefix) < n {
		if time.Now().After(deadline) {
			t.Fatalf("stub never logged %d %q calls", n, prefix)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestShareCancelledCallerReturnsWhileAnotherStillGetsTheRun(t *testing.T) {
	r, dir := sharedStub(t)
	os.WriteFile(filepath.Join(dir, "hang"), nil, 0o644)
	ctxA, cancelA := context.WithCancel(context.Background())
	defer cancelA()
	errA, errB := make(chan error, 1), make(chan error, 1)
	go func() { _, err := r.Agents(ctxA, ""); errA <- err }()
	waitCalls(t, dir, "agents", 1)
	go func() { _, err := r.Agents(context.Background(), ""); errB <- err }()
	cancelA()
	select {
	case err := <-errA:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("A got %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the cancelled caller did not return")
	}
	select {
	case err := <-errB:
		if err == nil || !strings.Contains(err.Error(), "timed out") {
			t.Fatalf("B got %v, want the run's own timeout", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("B did not get the run's result")
	}
	if n := calls(t, dir, "agents"); n != 1 {
		t.Fatalf("%d processes, want 1", n)
	}
}

func TestShareEntriesExpire(t *testing.T) {
	old := shareFor
	shareFor = 50 * time.Millisecond
	t.Cleanup(func() { shareFor = old })
	r, dir := sharedStub(t)
	ctx := context.Background()
	if _, err := r.Agents(ctx, "7d"); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for calls(t, dir, "agents") < 2 {
		if time.Now().After(deadline) {
			t.Fatal("the entry never expired")
		}
		time.Sleep(10 * time.Millisecond)
		if _, err := r.Agents(ctx, "7d"); err != nil {
			t.Fatal(err)
		}
	}
	// an old finished entry of another key is dropped by a later call
	if _, err := r.Agents(ctx, "30d"); err != nil {
		t.Fatal(err)
	}
	for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(10 * time.Millisecond) {
		if _, err := r.Agents(ctx, "24h"); err != nil {
			t.Fatal(err)
		}
		r.mu.Lock()
		_, kept := r.entries["agents\x00--since\x0030d"]
		r.mu.Unlock()
		if !kept {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("an expired entry is kept")
		}
	}
}

func TestShareCancelEndsRunsAndWaitReturns(t *testing.T) {
	r := stubRunner(t)
	r.Timeout = time.Minute
	share, cancel := context.WithCancel(context.Background())
	r.Share = share
	os.WriteFile(filepath.Join(os.Getenv("STUBDIR"), "hang"), nil, 0o644)
	done := make(chan error, 1)
	go func() { _, err := r.Agents(context.Background(), ""); done <- err }()
	waitCalls(t, os.Getenv("STUBDIR"), "agents", 1)
	cancel()
	waited := make(chan struct{})
	go func() { r.Wait(); close(waited) }()
	select {
	case <-waited:
	case <-time.After(5 * time.Second):
		t.Fatal("Wait did not return after the shared context was cancelled")
	}
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("got %v", err)
	}
	if _, err := r.Agents(context.Background(), "7d"); err == nil {
		t.Fatal("a call after Wait must fail")
	}
	if n := calls(t, os.Getenv("STUBDIR"), "agents"); n != 1 {
		t.Fatalf("a process started after Wait: %d", n)
	}
}
