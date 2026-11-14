package data

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

const eventLines = `{"ts":"2026-11-14T10:00:02.500Z","kind":"tool","session":"s1","agent":"a1","role":"coder","tool":"edit"}
{"ts":"2026-11-14T10:00:01.120Z","kind":"tokens","session":"s1","role":"main","in":3,"out":812,"cache_read":104000,"cache_write":2000}
not json
{"kind":"watch"}
{"ts":"2026-11-14T10:00:02.500Z","kind":"return","session":"s1","agent":"a1","role":"reviewer","verdict":"changes"}
{"ts":"2026-11-14T10:00:03Z","kind":"pr","pr":31,"slug":"needs-you-band","state":"open"}
{"ts":"2026-11-14T10:00:04Z","kind":"spawn","session":"s1","agent":"a2","parent":"a1","role":"coder"}
`

func TestReadEvents(t *testing.T) {
	path := filepath.Join(t.TempDir(), "e.jsonl")
	os.WriteFile(path, []byte(eventLines+`{"ts":"2026-11-14T10:00:05Z","kind":"merge","pr":31}`), 0o644) // the last line has no newline
	evs, err := ReadEvents(path)
	if err != nil {
		t.Fatal(err)
	}
	var kinds []string
	for _, e := range evs {
		kinds = append(kinds, e.Kind)
	}
	if got := strings.Join(kinds, " "); got != "tokens tool return pr spawn merge" { // a bad line and one without ts are skipped; ties keep file order
		t.Fatalf("kinds = %s", got)
	}
	tok := evs[0]
	if tok.TS != time.Date(2026, 11, 14, 10, 0, 1, 120e6, time.UTC) || tok.Session != "s1" || tok.Role != "main" ||
		tok.In != 3 || tok.Out != 812 || tok.CacheRead != 104000 || tok.CacheWrite != 2000 || tok.Agent != "" {
		t.Fatalf("tokens event = %+v", tok)
	}
	if ret := evs[2]; ret.Verdict != "changes" || ret.Agent != "a1" {
		t.Fatalf("return event = %+v", ret)
	}
	if pr := evs[3]; pr.PR != 31 || pr.Slug != "needs-you-band" || pr.State != "open" {
		t.Fatalf("pr event = %+v", pr)
	}
	if sp := evs[4]; sp.Parent != "a1" {
		t.Fatalf("spawn event = %+v", sp)
	}
	if _, err := ReadEvents(filepath.Join(t.TempDir(), "none")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("missing file: %v", err)
	}
}

func eventsRunner(t *testing.T) (*Runner, string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("STUBDIR", dir)
	py, _ := filepath.Abs("testdata/fakeevents.sh")
	if err := os.WriteFile(filepath.Join(dir, "events.jsonl"), []byte(eventLines), 0o644); err != nil {
		t.Fatal(err)
	}
	return &Runner{Root: "../..", Python: py, ProjectsDir: dir, Timeout: 500 * time.Millisecond}, dir
}

func eventCalls(t *testing.T, dir string) string {
	t.Helper()
	b, _ := os.ReadFile(filepath.Join(dir, "calls"))
	return strings.TrimSpace(string(b))
}

func TestEventsHistory(t *testing.T) {
	r, dir := eventsRunner(t)
	evs, err := r.Events(context.Background(), "24h")
	if err != nil {
		t.Fatal(err)
	}
	if len(evs) != 5 || evs[0].Kind != "tokens" || evs[4].Kind != "spawn" {
		t.Fatalf("events = %+v", evs)
	}
	if want := "-m isshoni events --since 24h --projects-dir " + dir; eventCalls(t, dir) != want {
		t.Fatalf("call = %q, want %q", eventCalls(t, dir), want)
	}
}

func TestEventsFailureAndTimeout(t *testing.T) {
	r, dir := eventsRunner(t)
	os.WriteFile(filepath.Join(dir, "fail"), nil, 0o644)
	if _, err := r.Events(context.Background(), ""); err == nil || err.Error() != "boom" {
		t.Fatalf("got %v", err)
	}
	os.Remove(filepath.Join(dir, "fail"))
	os.WriteFile(filepath.Join(dir, "hang"), nil, 0o644)
	start := time.Now()
	if _, err := r.Events(context.Background(), ""); err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("got %v", err)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatal("the hung call was not killed")
	}
	r.Python = "no-such-python"
	if _, err := r.Events(context.Background(), ""); !errors.Is(err, ErrPython) {
		t.Fatalf("got %v", err)
	}
}

// follow starts FollowEvents and returns the events it delivers, its result, and cancel.
func follow(t *testing.T, r *Runner, since string) (<-chan Event, <-chan error, context.CancelFunc) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	got, done := make(chan Event, 100), make(chan error, 1)
	go func() { done <- r.FollowEvents(ctx, since, func(e Event) { got <- e }) }()
	return got, done, cancel
}

func TestFollowEventsDeliversThenCancelKillsTheGroup(t *testing.T) {
	r, dir := eventsRunner(t)
	r.Timeout = time.Millisecond // follow is not bound by the per-call timeout
	got, done, cancel := follow(t, r, "2m")
	var kinds []string
	for i := 0; i < 5; i++ {
		select {
		case e := <-got:
			kinds = append(kinds, e.Kind)
		case <-time.After(5 * time.Second):
			t.Fatalf("only %v arrived", kinds)
		}
	}
	if strings.Join(kinds, " ") != "tool tokens return pr spawn" { // as the process printed them
		t.Fatalf("kinds = %v", kinds)
	}
	if want := "-m isshoni events --follow --since 2m --projects-dir " + dir; eventCalls(t, dir) != want {
		t.Fatalf("call = %q, want %q", eventCalls(t, dir), want)
	}
	var child int
	for i := 0; i < 100 && child == 0; i++ {
		b, _ := os.ReadFile(filepath.Join(dir, "child"))
		child, _ = strconv.Atoi(strings.TrimSpace(string(b)))
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("cancel returned %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("cancel did not end the follower")
	}
	for i := 0; i < 100 && syscall.Kill(child, 0) == nil; i++ {
		time.Sleep(20 * time.Millisecond)
	}
	if child == 0 || syscall.Kill(child, 0) == nil {
		t.Fatal("the process group survived the cancel")
	}
}

func TestFollowEventsProcessEnds(t *testing.T) {
	r, dir := eventsRunner(t)
	os.WriteFile(filepath.Join(dir, "end"), nil, 0o644)
	got, done, _ := follow(t, r, "")
	if err := <-done; err != nil {
		t.Fatalf("a clean end returned %v", err)
	}
	if len(got) != 5 {
		t.Fatalf("%d events", len(got))
	}
	os.Remove(filepath.Join(dir, "end"))
	os.WriteFile(filepath.Join(dir, "fail"), nil, 0o644)
	_, done, _ = follow(t, r, "")
	if err := <-done; err == nil || err.Error() != "boom" {
		t.Fatalf("a failing process returned %v", err)
	}
}

func TestFollowEventsAfterWaitStartsNothing(t *testing.T) {
	r, dir := eventsRunner(t)
	r.Wait()
	if err := r.FollowEvents(context.Background(), "", func(Event) {}); err == nil {
		t.Fatal("a follower after Wait must fail")
	}
	if eventCalls(t, dir) != "" {
		t.Fatal("no process may start after Wait")
	}
}

func TestWaitCoversFollower(t *testing.T) {
	r, _ := eventsRunner(t)
	got, done, cancel := follow(t, r, "")
	<-got
	cancel()
	waited := make(chan struct{})
	go func() { r.Wait(); close(waited) }()
	select {
	case <-waited:
	case <-time.After(5 * time.Second):
		t.Fatal("Wait did not return after cancel")
	}
	<-done
}
