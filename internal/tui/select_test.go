package tui

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"isshoni/internal/data"
)

// pluginModel is groupedModel where some subagent cost belongs to plugin agents (factory:coder, factory:reviewer).
func pluginModel(w, h int) *model {
	m := groupedModel(w, h)
	split := func(s *data.Session, role string) {
		u := s.ByAgent[role]
		half := u
		half.Cost = u.Cost / 2
		s.ByAgent[role] = half
		s.ByAgent["factory:"+role] = half
	}
	split(m.sessions["a2000000"], "coder")
	split(m.sessions["a3000000"], "explorer")
	m.sessions["a3000000"].ByAgent["factory:reviewer"] = data.Usage{Cost: 0}
	return m
}

func rowByKey(m *model, key string) treeRow {
	for _, r := range m.rows() {
		if r.key == key {
			return r
		}
	}
	return treeRow{}
}

// rowNamed finds a group row (no session) by kind and name.
func rowNamed(t *testing.T, m *model, kind rowKind, name string) treeRow {
	t.Helper()
	for _, r := range m.rows() {
		if r.kind == kind && r.name == name {
			return r
		}
	}
	t.Fatalf("no row %q", name)
	return treeRow{}
}

func selectRow(m *model, key string) {
	m.selID = key
	m.fixScroll()
}

// mixRow is one mix entry summed without sumTotals: the tokens the share is over (without cache reads),
// the cost and the tokens with no price.
type mixRow struct {
	tokens   float64
	cost     float64
	unpriced int64
}

// amount is what stands beside the share: dollars, or the token count for a row with no price at all.
func (r mixRow) amount() string {
	if r.cost == 0 && r.unpriced > 0 {
		return Human(r.tokens)
	}
	return Money(r.cost, "USD")
}

// sums totals a group's agent roles, models and cost without sumTotals.
func sums(group []*data.Session) (byRole, byModel map[string]mixRow, cost float64) {
	byRole, byModel = map[string]mixRow{}, map[string]mixRow{}
	add := func(m map[string]mixRow, k string, u data.Usage) {
		r := m[k]
		r.tokens += float64(u.Input + u.Output + u.CacheWrite)
		r.cost += u.Cost
		r.unpriced += u.Unpriced
		m[k] = r
	}
	for _, s := range group {
		cost += s.Cost.Total
		for k, u := range s.ByAgent {
			add(byRole, agentRole(k), u)
		}
		for k, u := range s.ByModel {
			add(byModel, k, u)
		}
	}
	return
}

// shareOf is a row's share of its mix, whose total is the sum of the tokens of its rows.
func shareOf(m map[string]mixRow, k string) string {
	var total float64
	for _, r := range m {
		total += r.tokens
	}
	return pct(100 * m[k].tokens / total)
}

// paneLine is the pane header: the line under the first box (the table).
func paneLine(out string) string {
	lines := strings.Split(out, "\n")
	for i, ln := range lines {
		if strings.HasPrefix(ln, "╰") {
			return lines[i+1]
		}
	}
	return ""
}

func lineWith(t *testing.T, out string, parts ...string) string {
	t.Helper()
	for _, ln := range strings.Split(out, "\n") {
		ok := true
		for _, p := range parts {
			ok = ok && strings.Contains(ln, p)
		}
		if ok {
			return ln
		}
	}
	t.Fatalf("no line with %q in\n%s", parts, out)
	return ""
}

// TestMixIsTheRowUnderTheCursor: whatever row the cursor is on, the panel titles name it and every
// figure in the panels is the sum over that row's sessions, the same sum as the row's COST cell.
func TestMixIsTheRowUnderTheCursor(t *testing.T) {
	for name, mk := range map[string]func(w, h int) *model{"grouped": groupedModel, "plugin": pluginModel} {
		for _, sz := range [][2]int{{140, 40}, {100, 30}} {
			m := mk(sz[0], sz[1])
			for _, r := range m.rows() {
				selectRow(m, r.key)
				out := plain(m)
				byRole, byModel, cost := sums(r.group)
				var agentSum, modelSum float64
				for _, v := range byRole {
					agentSum += v.cost
				}
				for _, v := range byModel {
					modelSum += v.cost
				}
				if d := agentSum - cost; d > 1e-9 || d < -1e-9 {
					t.Fatalf("%s %s: agent costs %v != row cost %v", name, r.label(), agentSum, cost)
				}
				if d := modelSum - cost; d > 1e-9 || d < -1e-9 {
					t.Fatalf("%s %s: model costs %v != row cost %v", name, r.label(), modelSum, cost)
				}
				if got := lineWith(t, out, ">"); !strings.Contains(got, Money(cost, "USD")) {
					t.Fatalf("%s %s: the row's COST cell is not %s: %s", name, r.label(), Money(cost, "USD"), got)
				}
				where := fmt.Sprintf("%s %dx%d %s", name, sz[0], sz[1], r.label())
				if sz[0] >= 120 {
					for _, title := range []string{"Agent tokens · ", "Model tokens · "} {
						if want := trunc(title+r.label(), 32); !strings.Contains(out, want) { // the box cuts a title at 32
							t.Fatalf("%s: no title %q", where, want)
						}
					}
				}
				if head := paneLine(out); !strings.Contains(head, r.label()) || !strings.Contains(head, Money(cost, "USD")) {
					t.Fatalf("%s: the pane header does not name the row and its cost: %q", where, head)
				}
				if sz[0] >= 120 {
					for role, v := range byRole {
						want := v.amount() + " " + shareOf(byRole, role)
						if ln := lineWith(t, out, "● "+role); !strings.Contains(strings.Join(strings.Fields(ln), " "), want) {
							t.Fatalf("%s: agent %s: want %q in %q", where, role, want, ln)
						}
					}
					for k, v := range byModel {
						want := v.amount() + " " + shareOf(byModel, k)
						ln := lineWith(t, out, shortModel(k))
						if got := strings.Join(strings.Fields(ln), " "); !strings.Contains(got, want) {
							t.Fatalf("%s: model %s: want %q in %q", where, k, want, ln)
						}
					}
				} else {
					entries := map[string]string{}
					for role, v := range byRole {
						entries[role] = v.amount() + " " + shareOf(byRole, role)
					}
					checkStrip(t, where+" agents", lineWith(t, out, "agents "), entries)
					entries = map[string]string{}
					for k, v := range byModel {
						entries[shortModel(k)] = v.amount() + " " + shareOf(byModel, k)
					}
					checkStrip(t, where+" models", lineWith(t, out, "models "), entries)
				}
			}
		}
	}
}

// checkStrip: the strip holds `name money share` for entries, and says how many it left out with +N.
func checkStrip(t *testing.T, where, ln string, entries map[string]string) {
	t.Helper()
	present := 0
	for name, want := range entries {
		if strings.Contains(ln, name+" "+want) {
			present++
		}
	}
	if present == 0 {
		t.Fatalf("%s: the first entry is missing: %q", where, ln)
	}
	if out := len(entries) - present; out > 0 && !strings.Contains(ln, fmt.Sprintf(" +%d", out)) {
		t.Fatalf("%s: %d entries left out without a matching +N: %q", where, out, ln)
	} else if out == 0 && strings.Contains(ln, " +") {
		t.Fatalf("%s: a +N marker with nothing left out: %q", where, ln)
	}
}

func TestPluginAgentsMergeIntoTheirRole(t *testing.T) {
	m := pluginModel(140, 40)
	m.agents = &data.Agents{Agents: []data.AgentRow{{Agent: "coder"}, {Agent: "factory:coder"}}}
	selectRow(m, "a2000000")
	s := m.sessions["a2000000"]
	want := Money(s.ByAgent["coder"].Cost+s.ByAgent["factory:coder"].Cost, "USD")
	out := plain(m)
	if n := strings.Count(out, "● coder"); n != 1 {
		t.Fatalf("%d coder lines", n)
	}
	if ln := lineWith(t, out, "● coder"); !strings.Contains(ln, want) {
		t.Fatalf("coder money is not the sum %s: %s", want, ln)
	}
	if strings.Contains(out, "factory:coder") {
		t.Fatal("a plugin agent keeps its own line")
	}
	send(m, "2")
	out = plain(m)
	if !strings.Contains(out, "factory:coder") || strings.Count(out, "coder") < 2 {
		t.Fatalf("the Agents view lists both:\n%s", out)
	}
}

func TestCursorSurvivesRefresh(t *testing.T) {
	for _, pick := range []struct {
		kind rowKind
		name string
	}{{kindTask, "ci"}, {kindRepo, "alpha"}, {kindSession, "a1000000"}} {
		m := groupedModel(120, 40)
		row := rowNamed(t, m, pick.kind, pick.name)
		selectRow(m, row.key)
		check := func(step string) {
			t.Helper()
			cur, _ := m.cursorRow(m.rows())
			if r := m.rows()[cur]; m.selID != row.key || r.key != row.key || r.name != pick.name || len(r.group) != len(row.group) {
				t.Fatalf("%s %s: selID %q, cursor on %s", pick.name, step, m.selID, r.label())
			}
		}
		send(m, "s")
		check("re-sort")

		changed := *m.sessions["a2000000"]
		changed.Cost.Total *= 3
		changed.LastChange = changed.LastChange.Add(time.Minute)
		m.mergeSession(&changed)
		m.normalizeSelection()
		check("mergeSession")

		m.sortBy = sortRecent
		fresh := groupSession("z1000000", "zeta", "/r/zeta", "", 500, 100, 0)
		fresh.LastChange = fresh.End.Add(time.Minute)
		m.mergeSession(fresh)
		m.normalizeSelection()
		if m.rows()[1].name != "zeta" {
			t.Fatalf("the new session should sort first: %s", rowNames(m))
		}
		check("new session above")

		var all []data.Session
		for _, s := range m.sessions {
			all = append(all, *s)
		}
		m.mergeLoad(sessionsMsg{res: &data.SessionsResult{Sessions: all}})
		m.normalizeSelection()
		check("mergeLoad")
	}
}

// A row that disappears hands the cursor to the row now at the same table index, not to the all row.
func TestSelectionFallsBackToTheSameIndex(t *testing.T) {
	m := groupedModel(120, 40)
	selectRow(m, rowNamed(t, m, kindTask, "ci").key)
	at := mustCursor(m)
	send(m, "/", "b", "e", "t", "a")
	want := m.rows()[min(at, len(m.rows())-1)]
	if r := m.rows()[mustCursor(m)]; r.kind == kindAll || r.key != want.key || m.selID != r.key {
		t.Fatalf("the filter hides ci (row %d): cursor on %s, want %s", at, r.label(), want.label())
	}
	send(m, "backspace", "backspace", "backspace", "backspace")
	if r := m.rows()[mustCursor(m)]; r.key != want.key {
		t.Fatalf("clearing the filter keeps the cursor on %s, got %s", want.label(), r.label())
	}

	// a group of two loses a session and merges into one row: the cursor takes the row at its index
	m = groupedModel(120, 40)
	selectRow(m, "a3000000")
	at = mustCursor(m)
	cur := map[string]time.Time{}
	for id, at := range m.seen {
		if id != "a3000000" {
			cur[id] = at
		}
	}
	m.applyScan(cur)
	if r := m.rows()[mustCursor(m)]; m.sessions["a3000000"] != nil || r.kind == kindAll || mustCursor(m) != min(at, len(m.rows())-1) {
		t.Fatalf("a3000000 is gone: cursor %d on %s, want index %d, not the all row", mustCursor(m), r.label(), min(at, len(m.rows())-1))
	}
}

// enter opens a session row full screen and esc returns with the cursor where it was; a group row does not open.
func TestEnterZoomsOnSessionRowsOnly(t *testing.T) {
	m := groupedModel(120, 40)
	for _, r := range m.rows() {
		selectRow(m, r.key)
		m.toast = toast{}
		send(m, "enter")
		if r.s != nil {
			if !m.zoomed() || m.selID != r.key || m.toast.text != "" {
				t.Errorf("enter on %s: zoom %v, selID %q, toast %q", r.label(), m.zoom, m.selID, m.toast.text)
			}
			send(m, "esc")
		} else if m.zoomed() || m.selID != r.key || m.toast.text != "enter opens a session row" {
			t.Errorf("enter on %s: zoom %v, selID %q, toast %q", r.label(), m.zoom, m.selID, m.toast.text)
		}
		if m.zoomed() || m.view != viewDashboard || m.selID != r.key {
			t.Errorf("%s, then esc: zoom %v, view %v, selID %q", r.label(), m.zoom, m.view, m.selID)
		}
	}
	selectRow(m, "a1000000")
	send(m, "left", "enter") // the folded repository is a group row
	if m.zoomed() || m.selID != groupKey("repo", "/r/alpha", "alpha") || m.toast.text != "enter opens a session row" {
		t.Errorf("enter on a folded repository: zoom %v, selID %q, toast %q", m.zoom, m.selID, m.toast.text)
	}

	m = modelOf(120, 40)
	send(m, "enter")
	if m.zoomed() || m.toast.text != "enter opens a session row" {
		t.Errorf("enter without sessions: zoom %v, toast %q", m.zoom, m.toast.text)
	}
}

// left folds the repository the cursor is in, from any row of it, and puts the cursor on its row; right
// unfolds it. Totals, the header and the sort ignore the fold; the mix panels show the repository.
func TestFoldRepository(t *testing.T) {
	for _, from := range []string{"a3000000", "a1000000", "a4000000", groupKey("task", "/r/alpha", "alpha", "ci"), groupKey("repo", "/r/alpha", "alpha")} {
		for _, ks := range [][2]string{{"left", "right"}, {"h", "l"}} {
			m := groupedModel(120, 40)
			selectRow(m, from)
			before, open := plain(m), rowNames(m)
			send(m, ks[0])
			if got := rowNames(m); got != "all ×5,beta b1000000,alpha ×4" {
				t.Fatalf("%s from %q: rows %s", ks[0], from, got)
			}
			cur := m.rows()[mustCursor(m)]
			out := plain(m)
			if cur.name != "alpha" || !cur.folded || !strings.Contains(out, "alpha ×4 ▸") || !strings.Contains(out, "tokens · repo alpha ×4") {
				t.Fatalf("%s from %q: cursor on %s\n%s", ks[0], from, cur.label(), out)
			}
			for _, same := range []string{"Sessions  5 in 2 repos, 2 tasks", "totals of all 5 sessions shown"} {
				if !strings.Contains(before, same) || !strings.Contains(out, same) {
					t.Fatalf("a fold keeps %q\n%s", same, out)
				}
			}
			if len(m.visible()) != 5 || m.totals().tokens != sumTotals(m.rows()[0].group).tokens {
				t.Fatalf("a fold hides rows, not sessions: %d visible", len(m.visible()))
			}
			send(m, ks[0]) // already folded: nothing
			if got := m.rows()[mustCursor(m)]; got.name != "alpha" || len(m.rows()) != 3 {
				t.Fatalf("%s on a folded repository changes nothing, cursor on %s", ks[0], got.label())
			}
			send(m, ks[1])
			if got := m.rows()[mustCursor(m)]; rowNames(m) != open || got.name != "alpha" || got.folded {
				t.Fatalf("%s unfolds and keeps the cursor: %s, cursor on %s", ks[1], rowNames(m), got.label())
			}
			send(m, ks[1]) // already open: nothing
			if rowNames(m) != open || m.rows()[mustCursor(m)].name != "alpha" {
				t.Fatalf("%s on an open repository changes nothing", ks[1])
			}
		}
	}

	// nothing to fold: the all row, a repository of one session
	m := groupedModel(120, 40)
	for _, key := range []string{groupKey("all"), "b1000000"} {
		selectRow(m, key)
		rows := rowNames(m)
		send(m, "left", "right")
		if rowNames(m) != rows || m.selID != key || len(m.folded) != 0 {
			t.Fatalf("left and right on %q: rows %s, selID %q", key, rowNames(m), m.selID)
		}
	}
	// only in the dashboard
	send(m, "2", "left")
	if len(m.folded) != 0 {
		t.Fatal("left folds outside the dashboard")
	}
	selectRow(m, "b1000000")
	send(m, "1", "enter", "left")
	if len(m.folded) != 0 || !m.zoomed() {
		t.Fatal("left folds inside the zoom")
	}
}

// A fold is kept through a re-sort, a refresh, a new session in the folded repository and a filter; the
// cursor stays on the folded row.
func TestFoldSurvivesUpdatesAndFilter(t *testing.T) {
	m := groupedModel(120, 40)
	selectRow(m, "a3000000")
	send(m, "left")
	key := groupKey("repo", "/r/alpha", "alpha")
	check := func(step string, n int) {
		t.Helper()
		r := m.rows()[mustCursor(m)]
		if m.selID != key || r.key != key || !r.folded || len(r.group) != n || strings.Contains(rowNames(m), "ci") {
			t.Fatalf("%s: selID %q, cursor on %s, rows %s", step, m.selID, r.label(), rowNames(m))
		}
	}
	send(m, "s")
	check("re-sort", 4)
	changed := *m.sessions["a2000000"]
	changed.Cost.Total *= 3
	changed.LastChange = changed.LastChange.Add(time.Hour)
	m.mergeSession(&changed)
	m.normalizeSelection()
	check("mergeSession", 4)
	fresh := groupSession("a5000000", "alpha", "/r/alpha", "new", 500, 100, 0)
	m.mergeSession(fresh)
	m.normalizeSelection()
	check("new session inside", 5)
	var all []data.Session
	for _, s := range m.sessions {
		all = append(all, *s)
	}
	m.mergeLoad(sessionsMsg{res: &data.SessionsResult{Sessions: all}})
	m.normalizeSelection()
	check("mergeLoad", 5)

	// the filter finds what is inside: the folded row stays, counting the matches, and opens the match
	send(m, "/", "c", "i", "enter")
	if got := rowNames(m); got != "all ×2,alpha ×2" {
		t.Fatalf("filter ci with alpha folded: %s", got)
	}
	check("filter", 2)
	send(m, "right")
	if got := rowNames(m); got != "all ×2,alpha ×2,  ci ×2,    a3000000,    a2000000" { // the order is held: a2000000's tripled cost does not move it
		t.Fatalf("right shows the matches: %s", got)
	}
}

// Follow mode targets the most recent session, folded or not, and the zoom ends when it lands on a folded row.
func TestFollowOntoAFoldedRepository(t *testing.T) {
	m := groupedModel(120, 40)
	selectRow(m, "a1000000")
	send(m, "left", "f")
	if !m.follow || m.selID != "b1000000" {
		t.Fatalf("follow targets the most recent session: follow %v, selID %q", m.follow, m.selID)
	}
	m.seen["a2000000"] = m.activity(m.sessions["b1000000"]).Add(time.Hour)
	m.followTarget()
	if r := m.rows()[mustCursor(m)]; m.selID != "a2000000" || r.name != "alpha" || !r.folded {
		t.Fatalf("following a folded session: selID %q, cursor on %s", m.selID, r.label())
	}
	if out := plain(m); !strings.Contains(out, "tokens · repo alpha ×4") || !strings.Contains(paneLine(out), "repo alpha ×4") {
		t.Fatalf("the mix and the pane show the folded row\n%s", out)
	}
	// unfolding and folding move the cursor to the repository row, so each turns follow mode off
	for _, k := range []string{"right", "left"} {
		m.follow = true
		m.followTarget()
		send(m, k)
		if r := m.rows()[mustCursor(m)]; m.follow || m.selID != r.key || r.name != "alpha" {
			t.Fatalf("%s while following: follow %v, selID %q, cursor on %s", k, m.follow, m.selID, r.label())
		}
	}

	m = groupedModel(120, 40)
	selectRow(m, "b1000000")
	send(m, "enter")
	m.folded[groupKey("repo", "/r/alpha", "alpha")] = true
	m.follow = true
	m.seen["a3000000"] = m.activity(m.sessions["b1000000"]).Add(time.Hour)
	m.followTarget()
	if m.zoomed() || m.selID != "a3000000" {
		t.Fatalf("follow onto a folded repository: zoom %v, selID %q", m.zoom, m.selID)
	}
}

func TestFollowFromAGroupRow(t *testing.T) {
	m := groupedModel(120, 40)
	selectRow(m, rowNamed(t, m, kindTask, "ci").key)
	m.follow = true
	m.followTarget()
	if m.selID != "b1000000" {
		t.Fatalf("follow moves to the most recent session, got %q", m.selID)
	}
	send(m, "k")
	if m.follow {
		t.Fatal("a cursor move turns follow off")
	}
}

func TestLayoutStaysPutAsTheCursorWalks(t *testing.T) {
	for _, sz := range [][2]int{{140, 40}, {120, 24}, {100, 30}, {80, 24}} {
		m := pluginModel(sz[0], sz[1])
		var lines, tableEnd int
		for i, r := range m.rows() {
			selectRow(m, r.key)
			out := strings.Split(plain(m), "\n")
			end := -1
			for j, ln := range out {
				if strings.HasPrefix(ln, "╰") && end < 0 {
					end = j
				}
			}
			if i == 0 {
				lines, tableEnd = len(out), end
			}
			if len(out) != lines || end != tableEnd {
				t.Fatalf("%dx%d on %s: %d lines, table ends at %d; want %d and %d", sz[0], sz[1], r.label(), len(out), end, lines, tableEnd)
			}
			if head := out[end+1]; !strings.Contains(head, r.label()) {
				t.Fatalf("%dx%d on %s: the pane header is not on line %d: %q", sz[0], sz[1], r.label(), end+1, head)
			}
		}
	}
}

func TestTilesAreRangeWideAndCaptioned(t *testing.T) {
	m := groupedModel(140, 40)
	tiles := func() string {
		out := strings.Split(plain(m), "\n")
		for i, ln := range out {
			if strings.Contains(ln, "COST") && strings.Contains(ln, "SUBAGENT SHARE") {
				return strings.Join(out[i:i+3], "\n")
			}
		}
		t.Fatal("no tiles")
		return ""
	}
	want := tiles()
	for _, r := range m.rows() {
		selectRow(m, r.key)
		if got := tiles(); got != want {
			t.Fatalf("tiles change on %s:\n%s\nwant\n%s", r.label(), got, want)
		}
		if !strings.Contains(plain(m), " ↑ totals of all 5 sessions shown · the mix and the pane show the selected row") {
			t.Fatalf("no caption on %s", r.label())
		}
	}
	m = groupedModel(60, 30)
	for _, ln := range strings.Split(plain(m), "\n") {
		if strings.Contains(ln, "↑ totals of all 5") && width(ln) <= 60 {
			return
		}
	}
	t.Fatal("no caption that fits 60 columns")
}

// Under 30 rows one line stands for the tiles and the caption.
func TestShortScreensTotalOnOneLine(t *testing.T) {
	m := groupedModel(100, 29)
	out := plain(m)
	if strings.Contains(out, "SUBAGENT SHARE") || strings.Contains(out, "totals of all") {
		t.Fatalf("tiles under 30 rows:\n%s", out)
	}
	ln := lineWith(t, out, " all 5 sessions", "sub ", "disp", "↻", "↺", "⚠")
	if !strings.HasPrefix(ln, " all 5 sessions  "+Money(m.totals().cost, "USD")) {
		t.Fatalf("the totals line: %q", ln)
	}
}

func TestNoCaptionWithoutSessions(t *testing.T) {
	out := plain(modelOf(140, 40))
	if strings.Contains(out, "totals of all") {
		t.Fatal("an empty table has no caption")
	}
}
