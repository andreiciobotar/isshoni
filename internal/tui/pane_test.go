package tui

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

// liveRunningModel is groupedModel with a1000000 live, its dispatches loaded and one of them running.
func liveRunningModel(w, h int) *model {
	m := groupedModel(w, h)
	a := m.sessions["a1000000"]
	m.details[a.ID] = fakeDetail(a)
	m.seen[a.ID] = m.now().Add(-5 * time.Second)
	m.syncOrder(true)
	m.fixScroll()
	return m
}

// The pane under the table is about the row under the cursor, whatever its kind; Now running is always there.
func TestPaneFollowsTheCursorRow(t *testing.T) {
	for _, sz := range [][2]int{{140, 40}, {100, 30}, {80, 24}} {
		m := liveRunningModel(sz[0], sz[1])
		for _, r := range m.rows() {
			selectRow(m, r.key)
			out := plain(m)
			where := fmt.Sprintf("%dx%d on %s", sz[0], sz[1], r.label())
			if head := paneLine(out); !strings.Contains(head, r.label()) {
				t.Errorf("%s: the pane header is %q", where, head)
			}
			if !strings.Contains(out, "Now running") || !strings.Contains(out, "my-custom-agent") {
				t.Errorf("%s: Now running lacks the running dispatch of the live session a1000000:\n%s", where, out)
			}
			switch d := m.details[r.key]; {
			case r.s == nil:
				if strings.Contains(out, "Dispatches") {
					t.Errorf("%s: a group row has no dispatch list", where)
				}
			case d == nil:
				if !strings.Contains(out, "loading dispatches…") {
					t.Errorf("%s: no loading line", where)
				}
			default:
				if !strings.Contains(out, fmt.Sprintf("Dispatches %d", len(d.Dispatches))) {
					t.Errorf("%s: no dispatch list", where)
				}
				if has := strings.Contains(out, "Timeline"); has != (sz[0] == 140) {
					t.Errorf("%s: timeline shown %v", where, has)
				}
			}
		}
	}
}

// In the zoom the dispatch cursor moves and the tree cursor stays; on the tree it is the other way round.
func TestZoomKeysMoveDispatchesTreeKeysMoveRows(t *testing.T) {
	m := liveRunningModel(140, 40)
	selectRow(m, "a1000000")
	send(m, "enter", "j", "j")
	if !m.zoomed() || m.selID != "a1000000" || m.dcursor != 2 {
		t.Fatalf("j in the zoom: zoom %v, selID %q, dcursor %d", m.zoom, m.selID, m.dcursor)
	}
	send(m, "k", "G")
	if m.selID != "a1000000" || m.dcursor != len(m.details["a1000000"].Dispatches)-1 {
		t.Fatalf("k, G in the zoom: selID %q, dcursor %d", m.selID, m.dcursor)
	}
	send(m, "enter")
	if !m.detailOnly {
		t.Fatal("enter in the zoom swaps the list for the detail")
	}
	send(m, "esc")
	if m.detailOnly || !m.zoomed() {
		t.Fatalf("esc first leaves the detail: detailOnly %v, zoom %v", m.detailOnly, m.zoom)
	}
	send(m, "esc")
	if m.zoomed() {
		t.Fatal("esc then leaves the zoom")
	}
	send(m, "j")
	if m.selID == "a1000000" || m.dcursor != 0 || m.dtop != 0 {
		t.Fatalf("j on the tree: selID %q, dcursor %d", m.selID, m.dcursor)
	}
	for _, k := range []string{"1", "tab", "shift+tab"} {
		selectRow(m, "a1000000")
		send(m, "enter", k)
		if m.zoomed() {
			t.Errorf("%s leaves the zoom", k)
		}
		send(m, "1")
	}
}

func rowKeyOf(t *testing.T, m *model, kind rowKind, name string) string {
	t.Helper()
	return rowNamed(t, m, kind, name).key
}

// The cursor moves only when a key is pressed. While it is on a row, a refresh leaves the cursor on its row and
// the rows where they are; on the all row the tree stays live.
func TestCursorAndOrderHoldAcrossRefreshes(t *testing.T) {
	later := t0.Add(10 * time.Hour)
	bump := map[string]func(m *model){
		"another session becomes the most recent": func(m *model) {
			seen := map[string]time.Time{}
			for id, at := range m.seen {
				seen[id] = at
			}
			seen["a2000000"] = later
			m.Update(scanMsg{seen: seen})
		},
		"a session is added": func(m *model) {
			fresh := groupSession("z1000000", "zeta", "/r/zeta", "", 500, 100, 0)
			m.seen[fresh.ID] = fresh.End.Add(time.Minute)
			m.Update(detailMsg{id: fresh.ID, at: m.seen[fresh.ID], res: fresh})
		},
		"costs change": func(m *model) {
			changed := *m.sessions["a2000000"]
			changed.Cost.Total *= 10
			changed.Cost.Subagents *= 0.1
			m.Update(detailMsg{id: changed.ID, at: changed.LastChange.Add(time.Minute), res: &changed})
		},
	}
	for name, refresh := range bump {
		for _, sortBy := range []sortMode{sortRecent, sortCost, sortShare} {
			for _, pick := range []struct {
				kind rowKind
				name string
			}{{kindSession, "a1000000"}, {kindRepo, "alpha"}, {kindTask, "ci"}} {
				m := groupedModel(140, 40)
				m.sortBy = sortBy
				m.syncOrder(true)
				selectRow(m, rowKeyOf(t, m, pick.kind, pick.name))
				key, at, order := m.selID, mustCursor(m), rowNames(m)
				refresh(m)
				where := fmt.Sprintf("%s, sorted by %s, cursor on %s", name, sortBy, pick.name)
				if m.selID != key {
					t.Fatalf("%s: selID %q, want %q", where, m.selID, key)
				}
				if name != "a session is added" && (mustCursor(m) != at || rowNames(m) != order) {
					t.Fatalf("%s: cursor %d (was %d), rows %s (were %s)", where, mustCursor(m), at, rowNames(m), order)
				}
				if !strings.Contains(plain(m), "sorted by "+sortBy.String()+", held") {
					t.Fatalf("%s: the subheader does not say the order is held", where)
				}
			}
		}
	}

	// on the all row the same refresh re-sorts
	for _, name := range []string{"another session becomes the most recent", "costs change"} {
		m := groupedModel(140, 40)
		m.sortBy = sortCost
		m.syncOrder(true)
		order := rowNames(m)
		bump[name](m)
		if rowNames(m) == order || m.selID != groupKey("all") {
			t.Fatalf("%s on the all row: rows %s, were %s", name, rowNames(m), order)
		}
		if out := plain(m); !strings.Contains(out, "sorted by cost") || strings.Contains(out, "held") {
			t.Fatalf("the all row holds nothing:\n%s", out)
		}
	}

	// s, and the all row, bring the order up to date
	m := groupedModel(140, 40)
	selectRow(m, "a1000000")
	before := rowNames(m)
	bump["costs change"](m)
	send(m, "s")
	got := rowNames(m)
	m.held = map[string]rankKey{} // what the live values say
	if got != rowNames(m) || got == before {
		t.Fatalf("s re-sorts: %s, live %s, before %s", got, rowNames(m), before)
	}
	m.syncOrder(true)
	send(m, "g")
	if m.selID != groupKey("all") || strings.Contains(plain(m), "held") {
		t.Fatal("back on the all row nothing is held")
	}
}

func TestFollowIsOffUntilAskedAndAKeyEndsIt(t *testing.T) {
	m := groupedModel(140, 40)
	if m.follow || strings.Contains(plain(m), "following") {
		t.Fatal("follow starts off")
	}
	selectRow(m, "a1000000")
	send(m, "f")
	if !m.follow || m.selID != "b1000000" || !strings.Contains(plain(m), "following") || !strings.Contains(m.toast.text, "follow mode on") {
		t.Fatalf("f: follow %v, selID %q, toast %q", m.follow, m.selID, m.toast.text)
	}
	a4 := *m.sessions["a4000000"]
	m.seen[a4.ID] = t0.Add(10 * time.Hour)
	m.Update(detailMsg{id: a4.ID, at: m.seen[a4.ID], res: &a4})
	if m.selID != "a4000000" {
		t.Fatalf("a refresh moves the cursor while following, selID %q", m.selID)
	}
	send(m, "j")
	if m.follow || strings.Contains(plain(m), "following") {
		t.Fatal("a cursor key turns follow off")
	}
	before := m.selID
	m.seen[a4.ID] = t0.Add(20 * time.Hour)
	m.Update(detailMsg{id: a4.ID, at: m.seen[a4.ID], res: &a4})
	if m.selID != before {
		t.Fatal("the cursor moved without a key")
	}
}

// A cursor walking over uncached rows starts one fetch, for the row it rests on; the pane shows the summary at
// once and the dispatches when they land.
func TestDetailFetchWaitsForTheCursorToRest(t *testing.T) {
	m, dir := stubModel(t)
	delete(m.details, sidA)
	m.opts.Static = false
	m.tickOn, m.tickD = true, 0 // no clock in the way: the commands under test are the debounce ticks
	rows := m.rows()
	var ticks []selMsg
	for i := 0; i < 3; i++ {
		_, cmd := m.Update(press("j"))
		ticks = append(ticks, only[selMsg](run(cmd))...)
	}
	if len(ticks) != 3 {
		t.Fatalf("%d debounce ticks for three uncached rows", len(ticks))
	}
	rest := rows[3]
	if m.selID != rest.key {
		t.Fatalf("the cursor rests on %q, want %q", m.selID, rest.key)
	}
	if out := plain(m); !strings.Contains(paneLine(out), rest.label()) || !strings.Contains(out, "loading dispatches…") {
		t.Fatalf("the pane shows the header and the loading line at once:\n%s", out)
	}
	var fetched []detailMsg
	for _, tk := range ticks {
		fetched = append(fetched, only[detailMsg](upd(m, tk))...)
	}
	if len(fetched) != 1 || fetched[0].id != rest.key || calls(t, dir, "session ") != 1 || calls(t, dir, "session "+rest.key) != 1 {
		t.Fatalf("one fetch for %q expected: %d detail messages, %d session calls", rest.key, len(fetched), calls(t, dir, "session "))
	}
	upd(m, fetched[0])
	if out := plain(m); strings.Contains(out, "loading dispatches…") || !strings.Contains(out, "Dispatches 0") {
		t.Fatalf("the pane shows the dispatches once they land:\n%s", out)
	}

	// a row whose dispatches are cached waits for nothing
	m.details[sidB] = fakeDetail(m.sessions[sidB])
	_, cmd := m.Update(press("k"))
	if got := only[selMsg](run(cmd)); len(got) != 0 || m.selID != sidB {
		t.Fatalf("a cached row started a debounce: %v, cursor on %q", got, m.selID)
	}

	// a live refresh of the session under the cursor updates the pane
	cur := m.sessions[m.selID]
	d := fakeDetail(cur)
	d.Dispatches[0].Description = "Brand new dispatch"
	m.Update(detailMsg{id: cur.ID, at: m.seen[cur.ID], res: d})
	if !strings.Contains(plain(m), "Brand new dispatch") {
		t.Fatalf("the pane did not take the refresh:\n%s", plain(m))
	}
}
