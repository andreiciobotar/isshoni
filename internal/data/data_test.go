package data

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func fixtureRunner(t *testing.T) *Runner {
	t.Helper()
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	r := &Runner{
		Root:        root,
		ProjectsDir: filepath.Join(root, "tests", "fixtures", "projects"),
		AgentsDir:   filepath.Join(root, "tests", "fixtures", "agents"),
	}
	if err := r.Check(); err != nil {
		t.Skipf("python unavailable: %v", err)
	}
	return r
}

func TestSessionsFixture(t *testing.T) {
	r := fixtureRunner(t)
	res, err := r.Sessions(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Sessions) != 1 || res.Malformed != 2 {
		t.Fatalf("sessions=%d malformed=%d", len(res.Sessions), res.Malformed)
	}
	s := res.Sessions[0]
	if s.DispatchCount != 18 || s.Repeats != 1 || s.ReviewLoops != 3 || s.WarnCount != 1 || s.Malformed != 2 {
		t.Fatalf("unexpected signals: %+v", s)
	}
	if s.ByAgent["main"].Total != 300 {
		t.Fatalf("main total = %d", s.ByAgent["main"].Total)
	}
	if s.Tokens.Total != s.Tokens.Main+s.Tokens.Subagents {
		t.Fatal("token split does not add up")
	}
	if s.Start == nil || s.End == nil {
		t.Fatal("missing span")
	}
}

func TestSessionDetailFixture(t *testing.T) {
	r := fixtureRunner(t)
	s, err := r.Session(context.Background(), "fixture")
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Dispatches) != 18 {
		t.Fatalf("dispatches=%d", len(s.Dispatches))
	}
	var found bool
	for _, d := range s.Dispatches {
		if d.Description == "Find config loader again" {
			found = true
			if d.Status != "running" || len(d.Flags) != 1 || d.Flags[0] != "repeat" || d.RepeatOf == nil || *d.RepeatOf != "toolu_E1" {
				t.Fatalf("bad dispatch %+v", d)
			}
			if _, ok := d.Duration(time.Now()); !ok {
				t.Fatal("running dispatch should estimate a duration when start is known")
			}
		}
	}
	if !found {
		t.Fatal("dispatch not found")
	}
	if len(s.ReviewLoopEvent) != 3 || s.ReviewLoopEvent[2].Via != "resume" {
		t.Fatalf("review loop events: %+v", s.ReviewLoopEvent)
	}
}

func TestAgentsFixture(t *testing.T) {
	r := fixtureRunner(t)
	a, err := r.Agents(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if a.Agents[0].Agent != "main" {
		t.Fatalf("first agent %q", a.Agents[0].Agent)
	}
	for _, row := range a.Agents {
		if row.Agent == "explorer" && (row.Dispatches != 10 || row.Usage.Output != 39 || row.WarnCount != 1) {
			t.Fatalf("explorer row %+v", row)
		}
		if row.Agent == "coder" && row.ReviewLoops != 3 {
			t.Fatalf("coder row %+v", row)
		}
	}
	if _, ok := a.ByModel["claude-haiku-5-5"]; !ok {
		t.Fatal("missing haiku model")
	}
}

func TestDecodeCost(t *testing.T) {
	var res SessionsResult
	doc := `{"sessions":[{"id":"s","by_model":{"m":{"total":9,"cost":1.5,"unpriced":0},"x":{"total":4,"cost":0,"unpriced":4}},
		"tokens":{"total":13},"cost":{"total":1.5,"main":1,"subagents":0.5,"main_pct":66.7,"subagent_pct":33.3,"unpriced_tokens":4,"unpriced_models":["x"]}}],
		"prices":{"currency":"USD","as_of":"2026-11-14","source":"https://example.test","path":"/p/prices.toml"}}`
	if err := json.Unmarshal([]byte(doc), &res); err != nil {
		t.Fatal(err)
	}
	s := res.Sessions[0]
	if s.Cost.Total != 1.5 || s.Cost.SubagentPct != 33.3 || s.Cost.UnpricedTokens != 4 || len(s.Cost.UnpricedModels) != 1 || s.Cost.UnpricedModels[0] != "x" {
		t.Fatalf("cost block %+v", s.Cost)
	}
	if u := s.ByModel["x"]; u.Cost != 0 || u.Unpriced != 4 {
		t.Fatalf("unpriced usage %+v", u)
	}
	if res.Prices.Currency != "USD" || res.Prices.AsOf != "2026-11-14" || res.Prices.Path != "/p/prices.toml" {
		t.Fatalf("prices %+v", res.Prices)
	}
	var sum Usage
	sum.Add(s.ByModel["m"])
	sum.Add(s.ByModel["x"])
	if sum.Cost != 1.5 || sum.Unpriced != 4 {
		t.Fatalf("Add drops cost: %+v", sum)
	}
	var ag Agents
	if err := json.Unmarshal([]byte(`{"agents":[{"agent":"coder","cost_p50":0.25,"cost_p90":1.5}],"prices":{"currency":"EUR"}}`), &ag); err != nil {
		t.Fatal(err)
	}
	if *ag.Agents[0].CostP50 != 0.25 || *ag.Agents[0].CostP90 != 1.5 || ag.Prices.Currency != "EUR" {
		t.Fatalf("agents %+v", ag)
	}
}

func TestSessionError(t *testing.T) {
	r := fixtureRunner(t)
	if _, err := r.Session(context.Background(), "nope-nope"); err == nil {
		t.Fatal("expected error for unknown session")
	}
}

func TestMissingPython(t *testing.T) {
	r := &Runner{Root: "../..", Python: "definitely-not-python-xyz"}
	if err := r.Check(); err != ErrPython {
		t.Fatalf("got %v", err)
	}
	if _, err := r.Sessions(context.Background(), ""); err != ErrPython {
		t.Fatalf("got %v", err)
	}
}

func TestMissingPackage(t *testing.T) {
	r := &Runner{Root: t.TempDir()}
	if err := r.Check(); err == nil {
		t.Fatal("expected package error")
	}
}

func writeTranscript(t *testing.T, path string, mtime time.Time) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatal(err)
	}
}

func TestScan(t *testing.T) {
	root := t.TempDir()
	t1 := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	t2 := t1.Add(time.Hour)
	writeTranscript(t, filepath.Join(root, "proj", "s1.jsonl"), t1)
	writeTranscript(t, filepath.Join(root, "proj", "s2.jsonl"), t1)
	writeTranscript(t, filepath.Join(root, "proj", "s2", "subagents", "agent-1.jsonl"), t2) // bumps s2
	writeTranscript(t, filepath.Join(root, "proj", "s2", "subagents", "agent-1.meta.json"), t2.Add(time.Hour))
	writeTranscript(t, filepath.Join(root, "proj", "orphan", "subagents", "agent-1.jsonl"), t2) // no main transcript
	writeTranscript(t, filepath.Join(root, "proj", "s3", "memory", "x.jsonl"), t2)              // not a subagent file
	got, err := Scan(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || !got["s1"].Equal(t1) || !got["s2"].Equal(t2) {
		t.Fatalf("scan = %v", got)
	}
	if err := os.Remove(filepath.Join(root, "proj", "s1.jsonl")); err != nil {
		t.Fatal(err)
	}
	if got, _ = Scan(root); len(got) != 1 {
		t.Fatalf("a deleted transcript must disappear, got %v", got)
	}
	if _, err := Scan(filepath.Join(root, "missing")); err == nil {
		t.Fatal("an unreadable root is an error, not an empty corpus")
	}
}

func TestScanSkipsUnreadableProject(t *testing.T) {
	root := t.TempDir()
	t1 := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	writeTranscript(t, filepath.Join(root, "good", "s1.jsonl"), t1)
	writeTranscript(t, filepath.Join(root, "locked", "s2.jsonl"), t1)
	locked := filepath.Join(root, "locked")
	if err := os.Chmod(locked, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chmod(locked, 0o755) })
	if _, err := os.ReadDir(locked); err == nil {
		t.Skip("directory permissions are not enforced (running as root?)")
	}
	got, err := Scan(root)
	if err != nil || len(got) != 1 || !got["s1"].Equal(t1) {
		t.Fatalf("an unreadable project is skipped: %v, %v", got, err)
	}
}

func stubRunner(t *testing.T) *Runner {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("STUBDIR", dir)
	py, _ := filepath.Abs("testdata/fakepy.sh")
	return &Runner{Root: "../..", Python: py, ProjectsDir: dir, Timeout: 500 * time.Millisecond}
}

func TestCallTimeout(t *testing.T) {
	r := stubRunner(t)
	os.WriteFile(filepath.Join(os.Getenv("STUBDIR"), "hang"), nil, 0o644)
	start := time.Now()
	_, err := r.Session(context.Background(), "x")
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("got %v", err)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatal("the hung call was not killed")
	}
}

func TestCancelKillsCallAndWaitReturns(t *testing.T) {
	r := stubRunner(t)
	r.Timeout = time.Minute
	os.WriteFile(filepath.Join(os.Getenv("STUBDIR"), "hang"), nil, 0o644)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { _, err := r.Agents(ctx, ""); done <- err }()
	time.Sleep(200 * time.Millisecond)
	cancel()
	waited := make(chan struct{})
	go func() { r.Wait(); close(waited) }()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("got %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("cancel did not end the call")
	}
	select {
	case <-waited:
	case <-time.After(5 * time.Second):
		t.Fatal("Wait did not return after cancel")
	}
}

func TestCallAfterWaitDoesNotStart(t *testing.T) {
	r := stubRunner(t)
	r.Wait()
	if _, err := r.Session(context.Background(), "x"); err == nil {
		t.Fatal("a call after Wait must fail")
	}
	if _, err := os.Stat(filepath.Join(os.Getenv("STUBDIR"), "calls")); err == nil {
		t.Fatal("no process may start after Wait")
	}
}

func TestSessionsGroupingFields(t *testing.T) {
	r := fixtureRunner(t)
	r.ProjectsDir = filepath.Join(r.Root, "tests", "fixtures", "grouping")
	res, err := r.Sessions(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]*Session{}
	for i := range res.Sessions {
		got[res.Sessions[i].ID[:8]] = &res.Sessions[i]
	}
	for id, want := range map[string][3]string{
		"a1000001": {"alpha", "/r/alpha", ""},
		"a1000002": {"alpha", "/r/alpha", "ci"},
		"c1000001": {"-no-cwd", "", ""}, // a null repo_path decodes to the empty string
	} {
		s := got[id]
		if s == nil {
			t.Fatalf("missing session %s", id)
		}
		if s.Repo != want[0] || s.RepoPath != want[1] || s.Task != want[2] {
			t.Errorf("%s: repo %q path %q task %q, want %q", id, s.Repo, s.RepoPath, s.Task, want)
		}
	}
	if l := got["a1000002"].Label(); l != "alpha/ci" {
		t.Errorf("label %q", l)
	}
	if l := got["a1000001"].Label(); l != "alpha" {
		t.Errorf("label %q", l)
	}
}

// stubChild waits up to 5 s for the stub to record its child's pid in $STUBDIR/child and returns it; a survivor is killed at cleanup.
func stubChild(t *testing.T, dir string) int {
	t.Helper()
	var pid int
	for i := 0; i < 500 && pid == 0; i++ {
		b, _ := os.ReadFile(filepath.Join(dir, "child"))
		pid, _ = strconv.Atoi(strings.TrimSpace(string(b)))
		if pid == 0 {
			time.Sleep(10 * time.Millisecond)
		}
	}
	if pid == 0 {
		t.Fatal("the stub did not start a child")
	}
	t.Cleanup(func() { syscall.Kill(pid, syscall.SIGKILL) })
	return pid
}

// requireChildGone waits for the child `pid` to end.
func requireChildGone(t *testing.T, pid int) {
	t.Helper()
	for i := 0; syscall.Kill(pid, 0) == nil; i++ {
		if i == 100 {
			t.Fatalf("child %d survived", pid)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestMonitorTimeoutKillsTheChildren(t *testing.T) {
	r := stubRunner(t)
	r.Timeout = 3 * time.Second
	dir := os.Getenv("STUBDIR")
	os.WriteFile(filepath.Join(dir, "hang"), nil, 0o644)
	if _, err := r.Agents(context.Background(), ""); err == nil {
		t.Fatal("a hung call must time out")
	}
	requireChildGone(t, stubChild(t, dir))
}

func TestMonitorCancelKillsTheChildren(t *testing.T) {
	r := stubRunner(t)
	r.Timeout = time.Minute
	dir := os.Getenv("STUBDIR")
	os.WriteFile(filepath.Join(dir, "hang"), nil, 0o644)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { _, err := r.Agents(ctx, ""); done <- err }()
	child := stubChild(t, dir)
	cancel()
	<-done
	requireChildGone(t, child)
}

// scannerRoot is a root with a hot session (hot, in proj), a cold one (cold, in proj) and a
// cold one in a second project, with now the scanner's clock.
func scannerRoot(t *testing.T, now time.Time) (root string, s *Scanner) {
	t.Helper()
	root = t.TempDir()
	writeTranscript(t, filepath.Join(root, "proj", "hot.jsonl"), now.Add(-time.Minute))
	writeTranscript(t, filepath.Join(root, "proj", "cold.jsonl"), now.Add(-time.Hour))
	writeTranscript(t, filepath.Join(root, "other", "old.jsonl"), now.Add(-time.Hour))
	s = &Scanner{}
	got, err := s.Scan(root, now)
	if err != nil || len(got) != 3 {
		t.Fatalf("first scan = %v, %v", got, err)
	}
	return root, s
}

func sameAsScan(t *testing.T, root string, got map[string]time.Time) {
	t.Helper()
	want, _ := Scan(root)
	if len(got) != len(want) {
		t.Fatalf("scanner = %v, Scan = %v", got, want)
	}
	for id, w := range want {
		if !got[id].Equal(w) {
			t.Fatalf("scanner = %v, Scan = %v", got, want)
		}
	}
}

func TestScannerHotAppendIsSeenAtOnce(t *testing.T) {
	now := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	root, s := scannerRoot(t, now)
	later := now.Add(time.Second)
	if err := os.Chtimes(filepath.Join(root, "proj", "hot.jsonl"), later, later); err != nil {
		t.Fatal(err)
	}
	got, _ := s.Scan(root, now.Add(time.Second))
	if !got["hot"].Equal(later) {
		t.Fatalf("hot = %v, want %v", got["hot"], later)
	}
	sameAsScan(t, root, got)
}

func TestScannerHotSubagentIsSeenAtOnce(t *testing.T) {
	now := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	root, s := scannerRoot(t, now)
	sub := filepath.Join(root, "proj", "hot", "subagents", "x.jsonl")
	t1 := now.Add(time.Second)
	writeTranscript(t, sub, t1) // a first subagent file: the session directory is new
	got, _ := s.Scan(root, now.Add(time.Second))
	if !got["hot"].Equal(t1) {
		t.Fatalf("new subagent file: hot = %v, want %v", got["hot"], t1)
	}
	sameAsScan(t, root, got)
	t2 := now.Add(2 * time.Second) // an append to it moves no directory mtime
	if err := os.Chtimes(sub, t2, t2); err != nil {
		t.Fatal(err)
	}
	got, _ = s.Scan(root, now.Add(2*time.Second))
	if !got["hot"].Equal(t2) {
		t.Fatalf("advanced subagent file: hot = %v, want %v", got["hot"], t2)
	}
	sameAsScan(t, root, got)
}

func TestScannerColdAppendWaitsForTheSweep(t *testing.T) {
	now := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	root, s := scannerRoot(t, now)
	cold := filepath.Join(root, "proj", "cold.jsonl")
	t1 := now.Add(time.Second)
	if err := os.Chtimes(cold, t1, t1); err != nil {
		t.Fatal(err)
	}
	before, _ := s.Scan(root, now.Add(sweepEvery-time.Second))
	if !before["cold"].Equal(now.Add(-time.Hour)) {
		t.Fatalf("a cold append before the sweep must not be statted: cold = %v", before["cold"])
	}
	at, _ := s.Scan(root, now.Add(sweepEvery))
	if !at["cold"].Equal(t1) {
		t.Fatalf("the sweep sees it: cold = %v, want %v", at["cold"], t1)
	}
	sameAsScan(t, root, at)
	t2 := now.Add(sweepEvery + time.Second) // now hot: seen by the next scan, not the next sweep
	if err := os.Chtimes(cold, t2, t2); err != nil {
		t.Fatal(err)
	}
	next, _ := s.Scan(root, now.Add(sweepEvery+time.Second))
	if !next["cold"].Equal(t2) {
		t.Fatalf("a resumed session is hot: cold = %v, want %v", next["cold"], t2)
	}
	sameAsScan(t, root, next)
}

func TestScannerSeesNewSessionsAndProjects(t *testing.T) {
	now := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	root, s := scannerRoot(t, now)
	writeTranscript(t, filepath.Join(root, "proj", "new.jsonl"), now.Add(-2*time.Hour))
	got, _ := s.Scan(root, now.Add(time.Second))
	if len(got) != 4 || got["new"].IsZero() {
		t.Fatalf("new session in a known project: %v", got)
	}
	sameAsScan(t, root, got)
	writeTranscript(t, filepath.Join(root, "fresh", "f.jsonl"), now.Add(-2*time.Hour))
	got, _ = s.Scan(root, now.Add(2*time.Second))
	if len(got) != 5 || got["f"].IsZero() {
		t.Fatalf("new project: %v", got)
	}
	sameAsScan(t, root, got)
}

func TestScannerDropsDeletedSessionsAndProjects(t *testing.T) {
	now := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	root, s := scannerRoot(t, now)
	if err := os.Remove(filepath.Join(root, "proj", "cold.jsonl")); err != nil {
		t.Fatal(err)
	}
	got, _ := s.Scan(root, now.Add(time.Second))
	if _, ok := got["cold"]; ok || len(got) != 2 {
		t.Fatalf("a deleted cold session is gone: %v", got)
	}
	sameAsScan(t, root, got)
	if err := os.RemoveAll(filepath.Join(root, "other")); err != nil {
		t.Fatal(err)
	}
	got, _ = s.Scan(root, now.Add(2*time.Second))
	if _, ok := got["old"]; ok || len(got) != 1 {
		t.Fatalf("a deleted project is gone: %v", got)
	}
	sameAsScan(t, root, got)
}

func TestScannerDropsADeletedHotSessionWithoutMovingItsDirectory(t *testing.T) {
	now := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	root, s := scannerRoot(t, now)
	dir := filepath.Join(root, "proj")
	fi, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "hot.jsonl")); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(dir, fi.ModTime(), fi.ModTime()); err != nil {
		t.Fatal(err)
	}
	got, _ := s.Scan(root, now.Add(time.Second))
	if _, ok := got["hot"]; ok || len(got) != 2 {
		t.Fatalf("a hot session whose transcript is gone is dropped: %v", got)
	}
}

func TestScannerRootUnreadableThenReadable(t *testing.T) {
	now := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	root, s := scannerRoot(t, now)
	if err := os.Chmod(root, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chmod(root, 0o755) })
	if _, err := os.ReadDir(root); err == nil {
		t.Skip("directory permissions are not enforced (running as root?)")
	}
	got, err := s.Scan(root, now.Add(time.Second))
	if err == nil || got != nil {
		t.Fatalf("an unreadable root is an error with a nil result: %v, %v", got, err)
	}
	os.Chmod(root, 0o755)
	got, err = s.Scan(root, now.Add(2*time.Second))
	if err != nil || len(got) != 3 {
		t.Fatalf("readable again: %v, %v", got, err)
	}
	sameAsScan(t, root, got)
}

func TestScannerReturnsAFreshMap(t *testing.T) {
	now := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	root, s := scannerRoot(t, now)
	a, _ := s.Scan(root, now.Add(time.Second))
	a["hot"] = time.Time{}
	delete(a, "cold")
	b, _ := s.Scan(root, now.Add(2*time.Second))
	if b["hot"].IsZero() || len(b) != 3 {
		t.Fatalf("the caller's changes leaked into the scanner: %v", b)
	}
}
