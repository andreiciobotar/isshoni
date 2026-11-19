package tui

import (
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"isshoni/internal/data"
)

func TestStartsOnTheDashboard(t *testing.T) {
	for _, o := range []Options{{Static: true}, {Static: true, ReadOnly: true}} {
		o.NoColor = true
		m := New(o)
		m.w, m.h = 140, 40                // wide enough for the full tab names
		m.preload(nil, nil, fakeAgents()) // the first frame after the load; before it only the loading screen shows
		if m.view != viewDashboard {
			t.Fatalf("%+v starts on view %v", o, m.view)
		}
		if out := plain(m); !strings.Contains(out, "[1 Dashboard]") {
			t.Fatalf("%+v: the first frame does not have the Dashboard tab active\n%s", o, out)
		}
	}
}

// runSession is a finished session of secs seconds costing cost dollars, started min minutes after t0.
func runSession(id, repo, task string, min, secs int, cost float64) *data.Session {
	s := groupSession(id, repo, "/r/"+repo, task, min, 100, 0)
	en := s.Start.Add(time.Duration(secs) * time.Second)
	s.End, s.LastChange = &en, en
	return priced(s, cost, 0)
}

func TestShortRunPredicate(t *testing.T) {
	m := modelOf(120, 40)
	live := runSession("l1000000", "r", "", 0, 10, 0.01)
	end := m.now().Add(-5 * time.Second) // ended 5 s ago: live is under 30 s
	live.End = &end
	noEnd := runSession("n1000000", "r", "", 0, 10, 0.01)
	noEnd.End = nil
	for _, c := range []struct {
		name string
		s    *data.Session
		want bool
	}{
		{"59 s", runSession("a", "r", "", 0, 59, 0.01), true},
		{"60 s", runSession("a", "r", "", 0, 60, 0.01), false},
		{"$0.24", runSession("a", "r", "", 0, 10, 0.24), true},
		{"$0.25", runSession("a", "r", "", 0, 10, 0.25), false},
		{"no end", noEnd, false},
		{"live", live, false},
	} {
		if got := m.shortRun(c.s); got != c.want {
			t.Errorf("%s: shortRun %v, want %v", c.name, got, c.want)
		}
	}
}

var (
	ciShort    = groupKey("short", "/r/alpha", "alpha", "ci")
	alphaShort = groupKey("short", "/r/alpha", "alpha", "")
	betaShort  = groupKey("short", "/r/beta", "beta", "")
	alphaRepo  = groupKey("repo", "/r/alpha", "alpha")
)

// shortRunsModel: repo alpha holds a bare session and two bare short runs, task ci a session and two short runs,
// task zz a session and one short run (which stays a row of its own); repo beta holds three bare short runs.
func shortRunsModel(w, h int) *model {
	return modelOf(w, h,
		runSession("a1000000", "alpha", "", 0, 600, 1),
		runSession("a2000000", "alpha", "", 1, 5, 0.01),
		runSession("a7000000", "alpha", "", 2, 5, 0.04),
		runSession("a3000000", "alpha", "ci", 3, 600, 2),
		runSession("a4000000", "alpha", "ci", 4, 5, 0.02),
		runSession("a5000000", "alpha", "ci", 5, 20, 0.03),
		runSession("a6000000", "alpha", "zz", 6, 600, 1),
		runSession("a8000000", "alpha", "zz", 7, 5, 0.05),
		runSession("b1000001", "beta", "", 8, 5, 0.01),
		runSession("b1000002", "beta", "", 9, 5, 0.02),
		runSession("b2000001", "beta", "", 10, 5, 0.03))
}

func TestShortRunsRowPlace(t *testing.T) {
	m := shortRunsModel(120, 40)
	for _, mode := range []sortMode{sortRecent, sortCost, sortShare} {
		m.sortBy = mode
		rows := m.tree()
		at := map[string]int{}
		for i, r := range rows {
			if r.kind == kindShort {
				at[r.key] = i
			}
		}
		if len(at) != 3 {
			t.Fatalf("sort %v: short rows %v", mode, at)
		}
		if r, i := rows[at[ciShort]], at[ciShort]; r.depth != 2 || r.s != nil || r.name != "2 short runs" || r.of != "ci" || len(r.group) != 2 || rows[i-1].key != "a3000000" || rows[i+1].depth != 3 {
			t.Fatalf("sort %v: task row %+v", mode, r)
		}
		// the repository's row ends alpha: after its sessions and tasks, then its own two sessions
		if r, i := rows[at[alphaShort]], at[alphaShort]; r.depth != 1 || r.of != "alpha" || len(r.group) != 2 || rows[i+1].depth != 2 || rows[i+2].depth != 2 || rows[i+3].key != groupKey("repo", "/r/beta", "beta") && i+3 != len(rows) {
			t.Fatalf("sort %v: repository row at %d of %d", mode, i, len(rows))
		}
		if r := rows[at[betaShort]]; r.depth != 1 || r.name != "3 short runs" {
			t.Fatalf("sort %v: beta row %+v", mode, r)
		}
		for _, s := range []string{"a8000000", "a2000000"} { // one short run in zz; a2000000 is in the row
			if k := slicesIndexKey(rows, s); k < 0 {
				t.Fatalf("sort %v: session %s is not in the tree", mode, s)
			}
		}
		if r := rows[slicesIndexKey(rows, "a8000000")]; r.depth != 2 || r.kind != kindSession {
			t.Fatalf("sort %v: a single short run is an ordinary row: %+v", mode, r)
		}
	}
}

func slicesIndexKey(rows []treeRow, key string) int {
	for i, r := range rows {
		if r.key == key {
			return i
		}
	}
	return -1
}

func TestShortRunsOpenAndClose(t *testing.T) {
	for _, ks := range [][2]string{{"left", "right"}, {"h", "l"}} {
		m := shortRunsModel(120, 40)
		closed := rowNames(m)
		if strings.Contains(closed, "a4000000") || strings.Count(closed, "short runs") != 3 {
			t.Fatalf("closed by default: %s", closed)
		}
		selectRow(m, ciShort)
		send(m, ks[1])
		if r := m.rows()[mustCursor(m)]; r.folded || r.key != ciShort || m.selID != ciShort || !m.opened[ciShort] {
			t.Fatalf("%s opens: %+v, selID %q", ks[1], r, m.selID)
		}
		var got []string
		for _, r := range m.rows() {
			if r.depth == 3 {
				got = append(got, rowNameOf(r))
			}
		}
		if strings.Join(got, ",") != "a5000000,a4000000" { // the order sessionUnits gives: latest end first
			t.Fatalf("the open row lists %v", got)
		}
		send(m, ks[0]) // left on the open row closes it
		if rowNames(m) != closed || m.selID != ciShort {
			t.Fatalf("%s on the open row: %s, selID %q", ks[0], rowNames(m), m.selID)
		}
		send(m, ks[1], "j") // on its first session
		if r := m.rows()[mustCursor(m)]; r.s == nil || r.depth != 3 {
			t.Fatalf("cursor on %+v", r)
		}
		send(m, ks[0])
		if rowNames(m) != closed || m.selID != ciShort || len(m.opened) != 0 {
			t.Fatalf("%s from a child: %s, selID %q", ks[0], rowNames(m), m.selID)
		}
		send(m, ks[0]) // left on the closed row folds the repository
		if r := m.rows()[mustCursor(m)]; r.kind != kindRepo || !r.folded || m.selID != alphaRepo {
			t.Fatalf("%s on a closed row: cursor on %s", ks[0], r.label())
		}
		send(m, ks[1]) // right on the folded repository unfolds it
		if r := m.rows()[mustCursor(m)]; r.kind != kindRepo || r.folded || rowNames(m) != closed {
			t.Fatalf("%s on a folded repository: cursor on %s", ks[1], r.label())
		}
	}

	// Rows that merely follow a short-runs row are not it. Order: alpha, ci, ci's short-runs row, the bare
	// session a1, alpha's short-runs row, then beta.
	newM := func() *model {
		m := modelOf(120, 40,
			runSession("a1000000", "alpha", "", 10, 600, 1),
			runSession("a2000000", "alpha", "", 11, 5, 0.01),
			runSession("a7000000", "alpha", "", 12, 5, 0.04),
			runSession("a3000000", "alpha", "ci", 13, 600, 2),
			runSession("a4000000", "alpha", "ci", 14, 5, 0.02),
			runSession("a5000000", "alpha", "ci", 15, 20, 0.03),
			runSession("b1000001", "beta", "", 0, 600, 1),
			runSession("b1000002", "beta", "", 1, 600, 1))
		var keys []string
		for _, r := range m.rows() {
			keys = append(keys, r.key)
		}
		want := []string{groupKey("all"), alphaRepo, groupKey("task", "/r/alpha", "alpha", "ci"), "a3000000", ciShort, "a1000000", alphaShort, groupKey("repo", "/r/beta", "beta")}
		if strings.Join(keys[:len(want)], ",") != strings.Join(want, ",") {
			t.Fatalf("rows %q, want them to start with %q", keys, want)
		}
		return m
	}
	betaRepo := groupKey("repo", "/r/beta", "beta")

	m := newM()
	m.folded[betaRepo] = true
	selectRow(m, betaRepo)
	send(m, "right")
	if m.folded[betaRepo] || len(m.opened) != 0 || m.selID != betaRepo {
		t.Fatalf("right on a folded repository after a short-runs row: folded %v, opened %v, selID %q", m.folded, m.opened, m.selID)
	}

	m = newM()
	selectRow(m, "a1000000")
	send(m, "right")
	if len(m.opened) != 0 || m.selID != "a1000000" {
		t.Fatalf("right on a bare session after a task's short-runs row: opened %v, selID %q", m.opened, m.selID)
	}

	m.opened[ciShort] = true
	send(m, "left")
	if !m.opened[ciShort] || !m.folded[alphaRepo] || m.selID != alphaRepo {
		t.Fatalf("left on that session: opened %v, folded %v, selID %q", m.opened, m.folded, m.selID)
	}

	m = newM()
	m.opened[alphaShort] = true
	selectRow(m, betaRepo)
	send(m, "left")
	if !m.opened[alphaShort] || !m.folded[betaRepo] {
		t.Fatalf("left on a repository after an open short-runs row: opened %v, folded %v", m.opened, m.folded)
	}
}

func TestZoomedSessionIsNotAShortRun(t *testing.T) {
	m := shortRunsModel(120, 40)
	m.opened[ciShort] = true
	selectRow(m, "a4000000")
	send(m, "enter")
	if !m.zoomed() || m.selID != "a4000000" {
		t.Fatalf("no zoom on a4000000: selID %q", m.selID)
	}
	if rows := m.rows(); slicesIndexKey(rows, ciShort) >= 0 || slicesIndexKey(rows, "a4000000") < 0 {
		t.Fatalf("the zoomed session is folded into a row: %s", rowNames(m))
	}
}

func TestShortRunsCursorVisitsEveryRow(t *testing.T) {
	m := shortRunsModel(120, 40)
	for _, open := range []bool{false, true} {
		m.opened = map[string]bool{ciShort: open}
		send(m, "g")
		var seen, want []string
		for _, r := range m.rows() {
			want = append(want, r.key)
		}
		for range m.rows() {
			seen = append(seen, m.rows()[mustCursor(m)].key)
			send(m, "j")
		}
		if strings.Join(seen, ",") != strings.Join(want, ",") {
			t.Fatalf("open=%v: j visited %q, want %q", open, seen, want)
		}
	}
}

func TestShortRunsHoldAHiddenSelection(t *testing.T) {
	m := shortRunsModel(120, 40)
	selectRow(m, "a4000000") // a short run inside the closed row
	if r := m.rows()[mustCursor(m)]; r.key != ciShort {
		t.Fatalf("a hidden session resolves to %s", r.label())
	}
	m.normalizeSelection()
	if m.selID != "a4000000" {
		t.Fatalf("normalize moved the selection to %q", m.selID)
	}

	// a refresh that turns the selected session into a short run lands on the row
	m = shortRunsModel(120, 40)
	s := *m.sessions["a3000000"]
	end := s.Start.Add(10 * time.Second)
	s.End, s.LastChange = &end, s.LastChange.Add(time.Second)
	s.Cost.Total = 0.01
	selectRow(m, "a3000000")
	m.mergeSession(&s)
	m.normalizeSelection()
	if r := m.rows()[mustCursor(m)]; r.key != ciShort || len(r.group) != 3 {
		t.Fatalf("after the refresh the cursor is on %s", r.label())
	}
}

func TestShortRunsTotalsAndPane(t *testing.T) {
	m := shortRunsModel(140, 40)
	var all float64
	for _, s := range m.sessions {
		all += s.Cost.Total
	}
	if got := sumTotals(m.rows()[0].group).cost; math.Abs(got-all) > 1e-9 || len(m.visible()) != 11 {
		t.Fatalf("all row costs %v over %d sessions, want %v over 11", got, len(m.visible()), all)
	}
	if got := sumTotals(rowNamed(t, m, kindTask, "ci").group).cost; got != 2.05 {
		t.Fatalf("task ci costs %v with its short runs closed, want 2.05", got)
	}
	selectRow(m, ciShort)
	out := plain(m)
	if row := lineWith(t, out, "2 short runs ▸"); !strings.Contains(row, "$0.05") {
		t.Fatalf("COST of the row is the total of its sessions: %s", row)
	}
	if !strings.Contains(out, "tokens · 2 short runs · ci") || !strings.Contains(paneLine(out), "2 short runs · ci") {
		t.Fatalf("mix and pane header name the row\n%s", out)
	}
	if !strings.Contains(out, "totals of all 11 sessions shown") {
		t.Fatal("the caption counts every session")
	}
	send(m, "enter")
	if m.zoomed() {
		t.Fatal("enter zooms a short-runs row")
	}
}

func TestShortRunsFilterAndRefresh(t *testing.T) {
	m := shortRunsModel(120, 40)
	m.filter = "b"
	if got := rowNames(m); !strings.Contains(got, "3 short runs") {
		t.Fatalf("filter b: %s", got)
	}
	m.filter = "b1"
	if got := rowNames(m); !strings.Contains(got, "2 short runs") || strings.Contains(got, "3 short runs") {
		t.Fatalf("the row counts the short runs that match: %s", got)
	}
	m.filter = "b2" // one match: an ordinary row
	if got := rowNames(m); strings.Contains(got, "short runs") {
		t.Fatalf("one short run is not folded: %s", got)
	}

	m = shortRunsModel(120, 40)
	selectRow(m, ciShort)
	send(m, "right", "/", "a", "l", "enter") // the filter keeps the row open
	if !m.opened[ciShort] || !strings.Contains(rowNames(m), "a4000000") {
		t.Fatalf("the filter closes the row: %s", rowNames(m))
	}
	var all []data.Session
	for _, s := range m.sessions {
		all = append(all, *s)
	}
	m.mergeLoad(sessionsMsg{res: &data.SessionsResult{Sessions: all}})
	m.normalizeSelection()
	if r := m.rows()[mustCursor(m)]; !m.opened[ciShort] || r.key != ciShort || r.folded || !strings.Contains(rowNames(m), "a4000000") {
		t.Fatalf("a refresh keeps the row open: %s", rowNames(m))
	}
}

// The dashboard with a closed short-runs row under the cursor and with it open; the ANSI of both goes to
// ISSHONI_SHOT_DIR when set.
func TestShortRunsGoldens(t *testing.T) {
	for _, c := range []struct {
		name string
		open bool
	}{{"short-runs-closed", false}, {"short-runs-open", true}} {
		m := shortRunsModel(140, 40)
		m.opened[ciShort] = c.open
		selectRow(m, ciShort)
		got := m.Render() + "\n"
		if shots := os.Getenv("ISSHONI_SHOT_DIR"); shots != "" {
			m.th = NewTheme(false)
			if err := os.WriteFile(filepath.Join(shots, c.name+".ansi"), []byte(m.Render()+"\n"), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		path := filepath.Join("testdata", c.name+"-140x40.golden")
		if *update {
			if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		want, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("%v (run go test ./internal/tui -update)", err)
		}
		if got != string(want) {
			t.Fatalf("%s changed; run go test ./internal/tui -update and review the diff\n--- got ---\n%s", c.name, got)
		}
	}
}
