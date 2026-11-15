package tui

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"isshoni/internal/data"
)

// opsWatches reads the made-up watches the tab is tested on. Unknown keys are an error, so the Go types
// and the fixture cannot drift apart.
func opsWatches(t testing.TB) []data.OpsWatch {
	t.Helper()
	b, err := os.ReadFile("testdata/ops-fixture.json")
	if err != nil {
		t.Fatal(err)
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	var doc struct{ Watches []data.OpsWatch }
	if err := dec.Decode(&doc); err != nil {
		t.Fatal(err)
	}
	return doc.Watches
}

// setOps puts the model on the Ops tab holding ws as the last answer, or err when the last load failed.
func setOps(m *model, ws []data.OpsWatch, err string) {
	m.view = viewOps
	m.ops.watches, m.ops.err, m.ops.doneAt = ws, err, m.now()
	m.fixScroll()
}

// opsStub is a model on whose runner `factory ops list` prints the fixture; the clock is the returned one.
func opsStub(t *testing.T) (*model, string, *time.Time) {
	t.Helper()
	m, dir := stubModel(t)
	b, err := os.ReadFile("testdata/ops-fixture.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "ops.json"), b, 0o644); err != nil {
		t.Fatal(err)
	}
	now := fakeNow
	m.now = func() time.Time { return now }
	return m, dir, &now
}

func TestGoldenOps(t *testing.T) {
	shots := os.Getenv("ISSHONI_SHOT_DIR")
	for _, c := range []struct {
		name string
		w, h int
		ws   func(t testing.TB) []data.OpsWatch
		err  string
	}{
		{"ops-140x40", 140, 40, opsWatches, ""},
		{"ops-100x30", 100, 30, opsWatches, ""},
		{"ops-80x24", 80, 24, opsWatches, ""},
		{"ops-empty-80x24", 80, 24, func(testing.TB) []data.OpsWatch { return nil }, ""},
		{"ops-error-80x24", 80, 24, func(testing.TB) []data.OpsWatch { return nil }, "this factory has no ops command yet"},
	} {
		t.Run(c.name, func(t *testing.T) {
			if shots != "" {
				m := fakeModel(false, c.w, c.h)
				setOps(m, c.ws(t), c.err)
				if err := os.WriteFile(filepath.Join(shots, c.name+".ansi"), []byte(m.Render()+"\n"), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			m := fakeModel(true, c.w, c.h)
			setOps(m, c.ws(t), c.err)
			got := m.Render() + "\n"
			path := filepath.Join("testdata", c.name+".golden")
			if *update {
				if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			want, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("%v (run go test ./internal/tui -update)", err)
			}
			if got != string(want) {
				t.Fatalf("%s changed; run go test ./internal/tui -update and review the diff\n--- got ---\n%s", c.name, got)
			}
		})
	}
}

func TestOpsViewKeys(t *testing.T) {
	m := fakeModel(true, 120, 40)
	send(m, "5")
	if m.view != viewOps || !strings.Contains(plain(m), "[5 Ops]") {
		t.Fatalf("5 opens the Ops tab: view %v", m.view)
	}
	send(m, "tab")
	if m.view != viewHanami {
		t.Fatalf("tab goes from Ops to Hanami: %v", m.view)
	}
	send(m, "shift+tab")
	if m.view != viewOps {
		t.Fatalf("shift+tab goes back to Ops: %v", m.view)
	}
	send(m, "esc")
	if m.view != viewDashboard {
		t.Fatal("esc goes back to the dashboard")
	}
	send(m, "6")
	if m.view != viewHanami {
		t.Fatalf("6 opens Hanami: %v", m.view)
	}
}

func TestOpsLoadsOnEntryAndEveryThirtySecondsWhileShown(t *testing.T) {
	m, dir, now := opsStub(t)
	if got := only[opsMsg](upd(m, tickMsg{})); len(got) != 0 || calls(t, dir, "ops ") != 0 {
		t.Fatal("no load while the tab is not shown")
	}
	got := only[opsMsg](upd(m, press("5")))
	if len(got) != 1 || !m.ops.busy || calls(t, dir, "ops ") != 1 {
		t.Fatalf("entering starts one load: %d msgs, %d calls", len(got), calls(t, dir, "ops "))
	}
	if !strings.Contains(plain(m), "asking the factory…") {
		t.Fatalf("the first load shows a spinner line:\n%s", plain(m))
	}
	if more := only[opsMsg](upd(m, tickMsg{})); len(more) != 0 {
		t.Fatal("never two loads at once")
	}
	upd(m, got[0])
	if m.ops.busy || len(m.ops.watches) != len(opsWatches(t)) || m.ops.err != "" {
		t.Fatalf("the answer is applied: %+v", m.ops)
	}
	*now = now.Add(29 * time.Second)
	if got := only[opsMsg](upd(m, tickMsg{})); len(got) != 0 {
		t.Fatal("an answer under 30 s old is kept")
	}
	*now = now.Add(2 * time.Second)
	if got := only[opsMsg](upd(m, tickMsg{})); len(got) != 1 {
		t.Fatal("an older one is loaded again")
	}
	send(m, "1")
	*now = now.Add(time.Minute)
	if got := only[opsMsg](upd(m, tickMsg{})); len(got) != 0 || calls(t, dir, "ops ") != 2 {
		t.Fatalf("no load once the tab is left: %d msgs, %d calls", len(got), calls(t, dir, "ops "))
	}
}

func TestOpsEnteringTheTabLoadsAgainAtOnce(t *testing.T) {
	m, dir, _ := opsStub(t)
	upd(m, only[opsMsg](upd(m, press("5")))[0])
	send(m, "1")
	if got := only[opsMsg](upd(m, press("5"))); len(got) != 1 || calls(t, dir, "ops ") != 2 {
		t.Fatalf("entering again loads: %d msgs, %d calls", len(got), calls(t, dir, "ops "))
	}
}

func TestOpsFailedReloadKeepsTheWatchesAndShowsOneLine(t *testing.T) {
	m, dir, now := opsStub(t)
	upd(m, only[opsMsg](upd(m, press("5")))[0])
	*now = now.Add(31 * time.Second)
	os.WriteFile(filepath.Join(dir, "fail"), nil, 0o644)
	got := only[opsMsg](upd(m, tickMsg{}))
	if len(got) != 1 {
		t.Fatal("no reload")
	}
	upd(m, got[0])
	out := plain(m)
	if m.ops.err != "boom" || !strings.Contains(out, "⚠ boom") || !strings.Contains(out, "pr60-logs") || strings.Count(out, "⚠") != 1 {
		t.Fatalf("one line with the first line of the error above the old watches:\n%s", out)
	}
	if sub := strings.Split(out, "\n")[1]; !strings.Contains(sub, "11 watches") || !strings.Contains(sub, "stale") {
		t.Fatalf("the sub-header counts the old watches and says they are stale: %q", sub)
	}
	os.Remove(filepath.Join(dir, "fail"))
	*now = now.Add(31 * time.Second)
	upd(m, only[opsMsg](upd(m, tickMsg{}))[0])
	if m.ops.err != "" || strings.Contains(plain(m), "⚠") {
		t.Fatalf("a good load clears the line: %q", m.ops.err)
	}
}

func TestOpsFactoryWithoutTheCommand(t *testing.T) {
	m, dir, _ := opsStub(t)
	b, err := os.ReadFile("../data/testdata/ops-no-command.err")
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(dir, "ops.err"), b, 0o644)
	upd(m, only[opsMsg](upd(m, press("5")))[0])
	if out := plain(m); !strings.Contains(out, "⚠ this factory has no ops command yet") || strings.Contains(out, "0 watches") {
		t.Fatalf("the page says so and does not count zero watches:\n%s", out)
	}
}

func TestOpsBlockFacts(t *testing.T) {
	m := fakeModel(true, 120, 80)
	setOps(m, opsWatches(t), "")
	out := plain(m)
	for _, want := range []string{
		"11 watches · 1 alarmed · 1 out of budget",
		"● pr60-logs  watching · app", "next look in 4m · every 10m · ends in 4h",
		"last   6m ago · fine · 0 errors, 412 requests", "6 looks",
		"9m ago · fine · 2 of 3 volumes verified, the third still copying · 2m late", "10 looks · 3 looks missed",
		"spend  $0.42 of $5.00", "spend  $1.37 of $4.00 · 2 looks unpriced", "spend  unpriced (3 looks) of $5.00",
		"alarmed · infra", "ended: met", "ended: inconclusive", "ended: not met", "stopped · lib", "out of budget · app",
		"no look yet", "could not look", "next look overdue 2m", "next look in 4m · every 5m · ends in 1h",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("the page lacks %q:\n%s", want, out)
		}
	}
	for _, bad := range []string{"$0.00", "unpriced of", "could-not-look", "not-met", "out-of-budget"} {
		if strings.Contains(out, bad) {
			t.Errorf("the page shows %q", bad)
		}
	}
	// an ended watch has no next look: only its interval
	if strings.Contains(strings.Split(out[strings.Index(out, "cert-renewal"):], "\n")[0], "next look") {
		t.Error("an ended watch shows a next look")
	}
	m = fakeModel(true, 80, 80)
	setOps(m, opsWatches(t), "")
	out = plain(m)
	if n := strings.Count(out, "notes.md"); n != 11 {
		t.Errorf("80 wide: the file name of %d of 11 notes paths shows", n)
	}
	for _, name := range []string{"pr60-logs", "nightly-backup", "disk-pressure", "release-train-monitor"} {
		if !strings.Contains(out, name) {
			t.Errorf("80 wide: %s is cut", name)
		}
	}
}

func TestOpsUnparseableTimesAreShownAsTheyCame(t *testing.T) {
	ws := opsWatches(t)[:1]
	soon, someday := "soon", "someday"
	ws[0].NextLook, ws[0].Until = &soon, someday
	ws[0].LastLook.TS = "yesterday"
	m := fakeModel(true, 100, 30)
	setOps(m, ws, "")
	out := plain(m)
	for _, want := range []string{"next look soon", "ends someday", "yesterday · fine"} {
		if !strings.Contains(out, want) {
			t.Errorf("lacks %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "yesterday ago") || strings.Contains(out, "overdue") {
		t.Errorf("an unparseable time is never an age:\n%s", out)
	}
}

func TestOpsScroll(t *testing.T) {
	m := fakeModel(true, 80, 24)
	setOps(m, opsWatches(t), "")
	send(m, "j", "j", "j")
	if m.ops.top != 3 {
		t.Fatalf("j scrolls: top %d", m.ops.top)
	}
	send(m, "G")
	if want := m.opsPositions() - 1; m.ops.top != want || strings.Contains(plain(m), "more lines") {
		t.Fatalf("G goes to the end: top %d, want %d", m.ops.top, want)
	}
	send(m, "g")
	if m.ops.top != 0 || !strings.Contains(plain(m), "more lines") {
		t.Fatalf("g goes to the top: top %d", m.ops.top)
	}
}
