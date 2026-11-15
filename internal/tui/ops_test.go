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
	m.applyOps(opsMsg{watches: ws})
	if err != "" {
		m.ops.err, m.ops.doneAt = err, m.now()
	}
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

func TestOpsLoadsAtStartEveryTwoMinutesOffTheTabAndEveryThirtySecondsOnIt(t *testing.T) {
	m, dir, now := opsStub(t)
	first := only[opsMsg](upd(m, sessionsMsg{gen: m.loadGen, res: &data.SessionsResult{}}))
	if len(first) != 1 || calls(t, dir, "ops ") != 1 {
		t.Fatalf("the first data load starts one ops load: %d msgs, %d calls", len(first), calls(t, dir, "ops "))
	}
	if more := only[opsMsg](upd(m, tickMsg{})); len(more) != 0 {
		t.Fatal("never two loads at once")
	}
	if more := only[opsMsg](upd(m, press("5"))); len(more) != 0 {
		t.Fatal("entering the tab while a load runs starts none")
	}
	upd(m, first[0])
	if m.ops.busy || m.ops.err != "" || len(m.ops.watches) != len(opsWatches(t)) {
		t.Fatalf("the answer is applied: %+v", m.ops.busy)
	}
	send(m, "1")
	*now = now.Add(119 * time.Second)
	if got := only[opsMsg](upd(m, tickMsg{})); len(got) != 0 {
		t.Fatal("off the tab an answer under 2 minutes old is kept")
	}
	*now = now.Add(2 * time.Second)
	got := only[opsMsg](upd(m, tickMsg{}))
	if len(got) != 1 {
		t.Fatal("off the tab an older one is loaded again, in the background")
	}
	upd(m, got[0])
	send(m, "5")
	m.ops.busy = false
	*now = now.Add(29 * time.Second)
	if got := only[opsMsg](upd(m, tickMsg{})); len(got) != 0 {
		t.Fatal("on the tab an answer under 30 s old is kept")
	}
	*now = now.Add(2 * time.Second)
	if got := only[opsMsg](upd(m, tickMsg{})); len(got) != 1 {
		t.Fatal("on the tab an older one is loaded again")
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
	if sub := strings.Split(out, "\n")[1]; !strings.Contains(sub, "11 watches") || !strings.Contains(sub, "stale, from 31s ago") {
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

func opsByName(t testing.TB, name string) data.OpsWatch {
	t.Helper()
	for _, w := range opsWatches(t) {
		if w.Name == name {
			return w
		}
	}
	t.Fatalf("no watch %s in the fixture", name)
	return data.OpsWatch{}
}

func TestOpsOrderAlarmedFirstThenWatchingThenTheRestEachInTheCommandsOrder(t *testing.T) {
	m := fakeModel(true, 100, 30)
	setOps(m, opsWatches(t), "")
	var got []string
	for _, w := range m.ops.watches {
		got = append(got, w.Name)
	}
	want := "disk-pressure pr60-logs nightly-backup fresh-watch cost-probe release-train-monitor cert-renewal flaky-deploy migration-check queue-depth api-latency"
	if strings.Join(got, " ") != want {
		t.Fatalf("order:\n%v\nwant %s", got, want)
	}
}

func TestOpsAlarmBadgeOnTheTabOnlyOffTheTabAndOnlyWhileFresh(t *testing.T) {
	for _, w := range []int{60, 80, 100, 140} {
		m := fakeModel(true, w, 30)
		setOps(m, opsWatches(t), "")
		m.view = viewDashboard
		head := strings.Split(plain(m), "\n")[0]
		if !strings.Contains(head, "▲1") || !strings.Contains(head, "[7d]") || !strings.Contains(head, "4 Kaizen") || width(head) != w {
			t.Errorf("%d wide, with the badge: %q", w, head)
		}
		m.view = viewOps
		if head := strings.Split(plain(m), "\n")[0]; strings.Contains(head, "▲") {
			t.Errorf("%d wide: the shown tab has no badge: %q", w, head)
		}
		m.view = viewDashboard
		m.ops.err = "boom"
		if head := strings.Split(plain(m), "\n")[0]; strings.Contains(head, "▲") {
			t.Errorf("%d wide: no badge from watches a failed reload left stale: %q", w, head)
		}
		m.ops.err = ""
		m.ops.watches = m.ops.watches[1:] // the alarmed one is first
		if head := strings.Split(plain(m), "\n")[0]; strings.Contains(head, "▲") {
			t.Errorf("%d wide: no alarmed watch, no badge: %q", w, head)
		}
	}
	m := fakeModel(true, 80, 30)
	setOps(m, opsWatches(t), "")
	m.view = viewDashboard
	if head := strings.Split(plain(m), "\n")[0]; !strings.Contains(head, "5 ▲1") && !strings.Contains(head, "5 Ops ▲1") {
		t.Errorf("the badge sits on the Ops tab: %q", head)
	}
}

func TestOpsFirstLineNeverLosesTheStateWord(t *testing.T) {
	long := opsByName(t, "disk-pressure")
	long.Name = "a-name-of-thirty-characters-xx" // 30 cells
	for _, w := range []int{60, 80} {
		for _, wt := range []data.OpsWatch{opsByName(t, "disk-pressure"), long, opsByName(t, "nightly-backup")} {
			m := fakeModel(true, w, 30)
			setOps(m, []data.OpsWatch{wt}, "")
			line := strings.Split(plain(m), "\n")[2]
			word, _ := m.opsLook(wt)
			if !strings.Contains(line, word) || !strings.HasPrefix(line, " ● "+wt.Name[:min(len(wt.Name), 12)]) {
				t.Errorf("%d wide, %s: %q", w, wt.Name, line)
			}
		}
	}
	m := fakeModel(true, 80, 30)
	setOps(m, []data.OpsWatch{long}, "")
	if line := strings.Split(plain(m), "\n")[2]; !strings.Contains(line, "alarmed · infra") || !strings.Contains(line, "next look in") {
		t.Errorf("a long name at 80 keeps the repository and the next look: %q", line)
	}
	m = fakeModel(true, 60, 30)
	setOps(m, []data.OpsWatch{opsByName(t, "release-train-monitor")}, "")
	if line := strings.Split(plain(m), "\n")[2]; !strings.Contains(line, "watching") || strings.Contains(line, "ends in") {
		t.Errorf("at 60 the schedule gives way before the state: %q", line)
	}
}

func TestOpsLastLineKeepsTheSawOverTheLooksRun(t *testing.T) {
	wt := opsByName(t, "nightly-backup")
	m := fakeModel(true, 100, 30)
	setOps(m, []data.OpsWatch{wt}, "")
	line := strings.Split(plain(m), "\n")[4]
	if strings.Contains(line, "10 looks") || !strings.Contains(line, "3 looks missed") || !strings.Contains(line, "2m late") || !strings.Contains(line, "2 of 3 volumes verified, the third") {
		t.Errorf("at 100 the looks run go first, the saw stays: %q", line)
	}
	m = fakeModel(true, 140, 30)
	setOps(m, []data.OpsWatch{wt}, "")
	if line := strings.Split(plain(m), "\n")[4]; !strings.Contains(line, "10 looks · 3 looks missed") || !strings.Contains(line, "still copying") {
		t.Errorf("room for everything shows everything: %q", line)
	}
}

func TestOpsPastItsEnd(t *testing.T) {
	wt := opsByName(t, "pr60-logs")
	wt.Until = "2026-11-13T10:50:00Z"
	m := fakeModel(true, 120, 30)
	setOps(m, []data.OpsWatch{wt}, "")
	if line := strings.Split(plain(m), "\n")[2]; !strings.Contains(line, "past its end by 10m") || strings.Contains(line, "overdue") {
		t.Errorf("%q", line)
	}
}

func TestOpsUnknownStateAndEndedWithoutAVerdict(t *testing.T) {
	odd, ended := opsByName(t, "pr60-logs"), opsByName(t, "cert-renewal")
	odd.State, ended.Verdict = "paused-by-wind", nil
	m := fakeModel(true, 120, 30)
	setOps(m, []data.OpsWatch{odd, ended}, "")
	out := plain(m)
	if !strings.Contains(out, "pr60-logs  paused-by-wind · app") || !strings.Contains(out, "cert-renewal  ended · app") || strings.Contains(out, "ended:") {
		t.Errorf("an unknown state as it came, ended alone without a verdict:\n%s", out)
	}
}

func TestOpsAmountsAreDollarsWhateverThePriceFileSays(t *testing.T) {
	m := fakeModel(true, 100, 30)
	m.prices.Currency = "EUR"
	setOps(m, opsWatches(t)[:1], "")
	if out := plain(m); !strings.Contains(out, "$0.42 of $5.00") || strings.Contains(out, "EUR") {
		t.Errorf("amounts are USD by contract:\n%s", out)
	}
}
