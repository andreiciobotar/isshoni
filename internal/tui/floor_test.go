package tui

import (
	"fmt"
	"image/color"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"isshoni/internal/data"
)

func floorCrates(m *model) map[string]floorCrate {
	out := map[string]floorCrate{}
	for _, c := range m.floorNow().crates {
		out[c.t.Slug] = c
	}
	return out
}

func rowsOf(repos []factoryRepo) (n int) {
	for _, r := range repos {
		if r.st != nil {
			n += len(r.st.Rows)
		}
	}
	return n
}

// laneOf is the lane a crate stands in: the index of the lane whose inner rows hold its tile.
func laneOf(l *floorLayout, c floorCrate) int {
	for k, ln := range l.lanes {
		if c.rect.y >= ln.top && c.rect.y < ln.top+ln.inner {
			return k
		}
	}
	return -1
}

func TestFloorPlacesEveryPhase(t *testing.T) {
	m := floorModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: fstatus(5,
		ftask("m", "motion"), ftask("b", "blocked"), ftask("v", "review"), ftask("g", "merged"), ftask("s", "settled"), ftask("u", "frozen"))})
	l := m.floorNow()
	got := floorCrates(m)
	for slug, place := range map[string]string{"m": "build", "b": "blocked", "v": "review", "g": "merged", "s": "settled", "u": "blocked"} {
		if got[slug].place != place || floorLanes[laneOf(l, got[slug])] != place {
			t.Errorf("%s is in %q (lane %d), want %q", slug, got[slug].place, laneOf(l, got[slug]), place)
		}
	}
	for _, want := range []string{"BUILD 1", "REVIEW 1", "MERGE 1", "SHIPPED 1", "BLOCKED 2", "frozen"} { // an unknown phase shows its raw text
		if out := plain(m); !strings.Contains(out, want) {
			t.Errorf("the floor lacks %q:\n%s", want, out)
		}
	}
}

func TestFloorBuildOrder(t *testing.T) {
	mk := func(slug string, elapsed, limit int64) data.FactoryTask {
		x := ftask(slug, "motion")
		if elapsed > 0 {
			x.Elapsed = ptr(elapsed)
		}
		if limit > 0 {
			x.Limit = ptr(limit)
		}
		return x
	}
	m := floorModel(true, 300, 60, factoryRepo{name: "r", path: "/r", st: fstatus(5,
		mk("c-none", 0, 0), mk("a-far", 90, 100), mk("d-nolimit", 50, 0), mk("b-near", 10, 100))})
	got := floorCrates(m)
	if len(got) != 4 {
		t.Fatalf("the BUILD lane holds all four at 300x60: %v", got)
	}
	order := []string{"a-far", "b-near", "c-none", "d-nolimit"} // the nearest to the head tile first; the unknown last, by slug
	for i := 1; i < len(order); i++ {
		if got[order[i-1]].rect.x >= got[order[i]].rect.x {
			t.Errorf("%s stands after %s: %v", order[i-1], order[i], got)
		}
	}
}

func TestFloorEveryTaskIsACrateOrCounted(t *testing.T) {
	crowded := factoryRepo{name: "busy", path: "/r/busy", st: fstatus(40, manyTasks(12)...)}
	for name, repos := range map[string][]factoryRepo{
		"waiting": waitingRepos(), "fake": fakeFactoryRepos(), "crowded": {crowded}, "none": nil, "one": {alpha()},
	} {
		for _, sz := range [][2]int{{80, 24}, {100, 30}, {140, 40}, {60, 16}} {
			m := floorModel(true, sz[0], sz[1], repos...)
			l := m.floorNow()
			n := len(l.crates)
			for _, more := range l.more {
				n += more
			}
			if want := rowsOf(repos); n != want {
				t.Errorf("%s at %dx%d: %d crates and counted, want %d rows", name, sz[0], sz[1], n, want)
			}
			plain(m)
		}
	}
	if l := floorModel(true, 80, 24, waitingRepos()...).floorNow(); len(l.crates) < 6 {
		t.Errorf("80x24 holds at least 6 crates, got %d", len(l.crates))
	}
	// a wider window shows more crates, or longer text in them
	shown := func(w, h int) int { return len(floorModel(true, w, h, crowded).floorNow().crates) }
	if a, b, c := shown(80, 24), shown(100, 30), shown(140, 40); a < 3 || b < 3 || c <= b {
		t.Errorf("a crowded BUILD lane holds at least 3 crates at 80x24 and 100x30, more at 140x40: %d, %d, %d", a, b, c)
	}
	l := floorModel(true, 80, 24, crowded).floorNow()
	if out := plain(floorModel(true, 80, 24, crowded)); !strings.Contains(out, "BUILD 12") || l.more["build"] == 0 || !strings.Contains(out, fmt.Sprintf("· %d more", l.more["build"])) {
		t.Errorf("a crowded lane keeps its full count and says how many have no room:\n%s", out)
	}
}

// floorGeometryCases are the scenes the geometry is checked on: one task and twenty, at the three sizes.
func floorGeometryCases(f func(name string, w, h int, m *model)) {
	for _, sz := range [][2]int{{80, 24}, {100, 30}, {140, 40}} {
		for name, repos := range map[string][]factoryRepo{
			"one": {{name: "r", path: "/r", st: fstatus(5, ftask("a", "motion"))}},
			"20":  {{name: "r", path: "/r", st: fstatus(40, manyTasks(20)...)}},
			"all": waitingRepos(),
		} {
			f(name, sz[0], sz[1], floorModel(true, sz[0], sz[1], repos...))
		}
	}
}

// Every lane runs from the left edge to the right one, and the lanes use every row of the scene.
func TestFloorLanesFillTheScene(t *testing.T) {
	floorGeometryCases(func(name string, w, h int, m *model) {
		l := m.floorNow()
		a := m.drawFloor(l)
		if l.h != m.h-3-len(m.floorNotes(m.h-3)) {
			t.Errorf("%s at %dx%d: the scene is %d rows", name, w, h, l.h)
		}
		inner := 0
		for _, ln := range l.lanes {
			inner += ln.inner
		}
		if inner+6 != l.h {
			t.Errorf("%s at %dx%d: five lanes of %d inner rows and six edges are not %d rows", name, w, h, inner, l.h)
		}
		reach := func(y int) (int, int) { // the columns row y is drawn in, but for the side walls
			lo, hi := l.w, -1
			for x := 1; x < l.w-1; x++ {
				if a.cv.g[y*l.w+x].r != 0 {
					lo, hi = min(lo, x), max(hi, x)
				}
			}
			return lo, hi
		}
		for y := range l.h {
			if lo, hi := reach(y); hi < 0 {
				t.Errorf("%s at %dx%d: row %d is empty (%d to %d)", name, w, h, y, lo, hi)
			}
		}
		for k, ln := range l.lanes {
			// the edge above the lane runs the full width, and the lane's centre row carries its marks, crates, junction or head tile
			if lo, hi := reach(ln.top - 1); lo != 1 || hi != l.w-2 {
				t.Errorf("%s at %dx%d: the edge above lane %d is drawn from column %d to %d of %d", name, w, h, k, lo, hi, l.w)
			}
			if lo, hi := reach(ln.cy()); lo > floorGate || (k > 0 && hi < l.w-1-floorGate) { // the siding stands short of the right end
				t.Errorf("%s at %dx%d: lane %d's centre row is drawn from column %d to %d of %d", name, w, h, k, lo, hi, l.w)
			}
			if ln.inner < 1 || (ln.inner >= 3) != (ln.crate == 3) {
				t.Errorf("%s at %dx%d: lane %d has %d inner rows and crates %d rows tall", name, w, h, k, ln.inner, ln.crate)
			}
		}
	})
}

// No two crates share a cell, and none shares one with a head tile or a junction.
func TestFloorCratesKeepApart(t *testing.T) {
	floorGeometryCases(func(name string, w, h int, m *model) {
		l := m.floorNow()
		var fixed []rect
		fixed = append(fixed, l.heads[:]...)
		for _, j := range l.junctions {
			fixed = append(fixed, j.rect)
		}
		for i, c := range l.crates {
			if c.rect.x < l.x0 || c.rect.x+c.rect.w > l.x1 || laneOf(l, c) < 0 {
				t.Errorf("%s at %dx%d: %s stands at %v, outside its lane's room", name, w, h, c.t.Slug, c.rect)
			}
			for _, o := range l.crates[i+1:] {
				if c.rect.overlaps(o.rect) {
					t.Errorf("%s at %dx%d: %s and %s share cells", name, w, h, c.t.Slug, o.t.Slug)
				}
			}
			for _, r := range fixed {
				if c.rect.overlaps(r) {
					t.Errorf("%s at %dx%d: %s overlaps a head tile or junction at %v", name, w, h, c.t.Slug, r)
				}
			}
		}
	})
}

// A crate that changes lane travels the conveyor: along its lane to the end, down through the junction, into its place.
func TestFloorTransitPassesTheJunction(t *testing.T) {
	m := floorModel(true, 100, 30, factoryRepo{name: "r", path: "/r", st: fstatus(5, ftask("a", "motion"), ftask("b", "review"), ftask("c", "merged"))})
	l := m.floorNow()
	cells := func(from, to float64) (out [][2]int) {
		for s := from; s <= to; s++ {
			x, y := l.path.at(s)
			out = append(out, [2]int{x, y})
		}
		return out
	}
	// the whole conveyor is one unbroken line
	all := cells(0, float64(l.start[4]+l.xr-l.xl))
	for i := 1; i < len(all); i++ {
		if d := abs(all[i][0]-all[i-1][0]) + abs(all[i][1]-all[i-1][1]); d != 1 {
			t.Fatalf("the conveyor jumps %d cells at %v", d, all[i])
		}
	}
	got := floorCrates(m)
	for _, tc := range []struct {
		from, to string
		junction int // the index of the junction between their lanes
	}{{"a", "b", 1}, {"b", "c", 2}} {
		var in, out int
		for _, c := range cells(got[tc.from].at, got[tc.to].at) {
			if l.junctions[tc.junction].has(c[0], c[1]) {
				in++
			} else if c[1] != l.lanes[laneOf(l, got[tc.from])].cy() && c[1] != l.lanes[laneOf(l, got[tc.to])].cy() {
				out++
			}
		}
		if in == 0 || out != 0 {
			t.Errorf("%s to %s: %d cells in the junction, %d off it and off both lanes' centre rows", tc.from, tc.to, in, out)
		}
	}
	// and the siding is reached through its junction too: from BUILD's start to the BLOCKED lane
	for _, c := range cells(float64(l.start[0]+l.xr-l.xl), float64(l.start[1])) {
		if c[0] != l.xl {
			t.Errorf("the siding leaves BUILD at its left junction, not at column %d", c[0])
		}
	}
}

func abs(n int) int { return max(n, -n) }

func (r rect) has(x, y int) bool { return x >= r.x && x < r.x+r.w && y >= r.y && y < r.y+r.h }

func (r rect) overlaps(o rect) bool {
	return r.x < o.x+o.w && o.x < r.x+r.w && r.y < o.y+o.h && o.y < r.y+r.h
}

func TestFloorLabelsNameTheRepositoryOnlyWhenSeveralHaveTasks(t *testing.T) {
	if out := plain(floorModel(true, 140, 40, waitingRepos()...)); !strings.Contains(out, "alpha/") {
		t.Errorf("several repositories: the crate has the repository:\n%s", out)
	}
	if out := plain(floorModel(true, 140, 40, alpha())); strings.Contains(out, "alpha/") {
		t.Errorf("one repository: no prefix:\n%s", out)
	}
}

func TestFloorCrateMarks(t *testing.T) {
	working, human, over := ftask("work", "motion"), ftask("ask", "blocked"), ftask("late", "motion")
	working.Session, over.Overtime = "working", true
	m := floorModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: fstatus(5, working, human, over)})
	out := plain(m)
	for _, want := range []string{"▐⠋ work", "▐▲ ask", "▐! late", "BLOCKED 1"} {
		if !strings.Contains(out, want) {
			t.Errorf("the crates lack %q:\n%s", want, out)
		}
	}
	for _, c := range []struct {
		t    data.FactoryTask
		want rune
		col  color.Color
	}{
		{working, '⠋', m.th.Green}, {human, '▲', m.th.Yellow}, {over, '!', m.th.Red}, {ftask("q", "motion"), ' ', m.th.Text},
	} {
		if r, col := m.crateMark(c.t); r != c.want || key24(col) != key24(c.col) {
			t.Errorf("%s: mark %q in %v, want %q in %v", c.t.Slug, r, col, c.want, c.col)
		}
	}
	if key24(m.floorLaneColor("merged")) != key24(m.mergedColor()) || key24(m.floorLaneColor("build")) != key24(m.th.Blue) {
		t.Error("a junction takes the colour phaseLook gives its phase")
	}
}

// A crate's text is its slug, what a blocked task waits on and the elapsed time; they go last first, and only the slug is cut with an ellipsis.
func TestFloorCrateTextDropsElapsedBeforeCuttingTheSlug(t *testing.T) {
	mk := func(slug, phase string) data.FactoryTask {
		x := ftask(slug, phase)
		x.Elapsed = ptr(int64(252))
		return x
	}
	elapsed := elapsedText(mk("x", "motion"), false)
	blocked := mk("stuck", "blocked")
	blocked.WaitsOn = "ci"
	m := floorModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: fstatus(5,
		mk("short", "motion"), mk("fits-the-crate-12345", "review"), mk("a-slug-far-too-long-for-any-crate", "merged"), blocked)})
	l := m.floorNow()
	text := func(slug string) string {
		var s string
		for _, p := range l.crateParts(floorCrates(m)[slug].floorTask) {
			s += p.s
		}
		return s
	}
	if got := text("short"); got != "short  "+elapsed {
		t.Errorf("a short slug keeps its elapsed text: %q", got)
	}
	if got := text("fits-the-crate-12345"); got != "fits-the-crate-12345" {
		t.Errorf("elapsed goes before the slug is cut: %q", got)
	}
	if got := text("a-slug-far-too-long-for-any-crate"); !strings.HasSuffix(got, "…") || strings.Contains(got, elapsed) {
		t.Errorf("a slug too long is cut with an ellipsis and has no elapsed: %q", got)
	}
	if got := text("stuck"); got != "stuck · ci  "+elapsed {
		t.Errorf("a blocked crate shows what it waits on: %q", got)
	}
	if out := plain(m); !strings.Contains(out, "short  "+elapsed) || !strings.Contains(out, "stuck · ci") {
		t.Errorf("the text is drawn in the crates:\n%s", out)
	}
}

// A junction and a head tile are bright (█) when their lane holds work or a task that waits on the human, dimmed (▓) when it is idle.
func TestFloorJunctionsAreBrightOnlyOnALiveLane(t *testing.T) {
	busy := ftask("a", "motion")
	busy.Session = "working"
	m := floorModel(true, 100, 30, factoryRepo{name: "r", path: "/r", st: fstatus(5, busy, ftask("v", "review"), ftask("g", "merged"))})
	l := m.floorNow()
	a := m.drawFloor(l)
	glyph := func(r rect) rune { return a.cv.g[r.y*l.w+r.x].r }
	for _, tc := range []struct {
		place string
		want  rune
	}{{"build", '█'}, {"review", '█'}, {"merged", '▓'}, {"settled", '▓'}, {"blocked", '▓'}} { // BUILD works, REVIEW waits on the human (its pull request is no draft)
		k := slices.Index(floorLanes, tc.place)
		if got := glyph(l.heads[k]); got != tc.want {
			t.Errorf("the %s head tile is %q, want %q", tc.place, got, tc.want)
		}
	}
	for _, j := range l.junctions {
		if want := map[string]rune{"review": '█', "merged": '▓', "settled": '▓', "blocked": '▓'}[j.place]; glyph(j.rect) != want {
			t.Errorf("the %s junction is %q, want %q", j.place, glyph(j.rect), want)
		}
	}
}

// transit: a crate moves for a second from its old place to its new one.
func TestFloorCratesTravelBetweenPlaces(t *testing.T) {
	a, b, gone := ftask("a", "motion"), ftask("b", "review"), ftask("gone", "settled")
	m := floorModel(true, 240, 60, factoryRepo{name: "r", path: "/r", st: fstatus(5, a, b, gone)})
	m.floorSync()
	before := floorCrates(m)

	a.Phase = "review"
	fresh := ftask("fresh", "motion")
	m.applyFactory(factoryMsg{repos: []factoryRepo{{name: "r", path: "/r", st: fstatus(5, a, b, fresh)}}})
	m.step(floorMoveFor / 2)
	after := floorCrates(m)
	intake := float64(m.floorNow().start[1])

	key := func(slug string) string { return "/r\x00" + slug }
	between := func(p, from, to float64) bool { return p != from && p != to && (p-from)*(p-to) < 0 }
	if now := m.floor.shown(key("a"), after["a"].at); !between(now, before["a"].at, after["a"].at) {
		t.Errorf("a was at %v, goes to %v, is drawn at %v", before["a"].at, after["a"].at, now)
	}
	if now := m.floor.shown(key("fresh"), after["fresh"].at); !between(now, intake, after["fresh"].at) {
		t.Errorf("a new task comes in at the intake: drawn at %v", now)
	}
	if len(m.floor.gone) != 1 || m.floor.gone[0].ft.t.Slug != "gone" || m.floor.gone[0].from != before["gone"].at {
		t.Errorf("a task that is gone leaves from where it stood: %+v", m.floor.gone)
	}
	if !m.animating() {
		t.Error("the view ticks while a crate is in transit")
	}
	if m.floor.shown(key("b"), after["b"].at) != after["b"].at && before["b"].at == after["b"].at {
		t.Error("a crate that did not change place stands still")
	}
	m.step(floorMoveFor)
	if m.floor.moving() || m.floor.shown(key("a"), after["a"].at) != after["a"].at || m.animating() {
		t.Errorf("after a second the crates stand in their places and the clock rests: %+v", m.floor)
	}
}

func TestFloorFirstResultOnlyTakesTheCratesIn(t *testing.T) {
	m := fakeModel(true, 100, 30)
	m.view = viewFactory
	m.floorSync()
	m.applyFactory(factoryMsg{repos: []factoryRepo{alpha()}})
	m.floorSync()
	if m.floor.moving() {
		t.Error("the first result puts the crates in place without a flight")
	}
}

// laneRow is a lane's centre row as drawn, the text a flow mark shows in.
func laneRow(m *model, place string) string {
	lines := strings.Split(plain(m), "\n")
	return lines[2+m.floorNow().lanes[slices.Index(floorLanes, place)].cy()]
}

// The flow marks step only on a lane with a working session; every other lane stands still.
func TestFloorFlowMarksStepOnlyOnAWorkingLane(t *testing.T) {
	busy := ftask("a", "motion")
	busy.Session = "working"
	m := floorModel(true, 100, 30, factoryRepo{name: "r", path: "/r", st: fstatus(5, busy, ftask("v", "review"))})
	build, review, merge := laneRow(m, "build"), laneRow(m, "review"), laneRow(m, "merged")
	for _, row := range []string{build, review, merge} {
		if !strings.ContainsAny(row, "›‹") {
			t.Fatalf("a lane carries flow marks: %q", row)
		}
	}
	if !strings.Contains(build, "›") || !strings.Contains(review, "‹") {
		t.Errorf("marks point along the flow, BUILD right and REVIEW left: %q %q", build, review)
	}
	m.step(floorBeltEvery)
	if laneRow(m, "build") == build {
		t.Error("the marks of a lane with a working session step each tick")
	}
	if laneRow(m, "review") != review || laneRow(m, "merged") != merge {
		t.Error("the marks of the other lanes stand still")
	}
}

func TestFloorBeltTurnsOnlyWhileASessionWorks(t *testing.T) {
	idle := floorModel(true, 100, 30, factoryRepo{name: "r", path: "/r", st: fstatus(5, ftask("a", "motion"))})
	before := plain(idle)
	idle.step(time.Second)
	if idle.floor.belt != 0 || plain(idle) != before {
		t.Error("the marks stand still when no session is working")
	}
	busy := ftask("a", "motion")
	busy.Session = "working"
	m := floorModel(true, 100, 30, factoryRepo{name: "r", path: "/r", st: fstatus(5, busy)})
	before = plain(m)
	m.step(floorBeltEvery)
	if m.floor.belt != 1 || plain(m) == before {
		t.Errorf("the marks move one step per tick of work: belt %d", m.floor.belt)
	}
	m.step(floorBeltEvery / 3)
	m.step(floorBeltEvery / 3)
	if m.floor.belt != 1 {
		t.Errorf("a fraction of a step is kept, not rounded up: %d", m.floor.belt)
	}
}

func TestFloorBeltIgnoresTasksTheFilterHides(t *testing.T) {
	busy := ftask("a", "motion")
	busy.Session = "working"
	m := floorModel(true, 100, 30,
		factoryRepo{name: "idle", path: "/r/idle", st: fstatus(5, ftask("b", "motion"))},
		factoryRepo{name: "work", path: "/r/work", st: fstatus(5, busy)})
	m.step(floorBeltEvery)
	if m.floor.belt != 1 {
		t.Fatalf("a shown working task steps the marks: %d", m.floor.belt)
	}
	m.floor.repo = "/r/idle"
	m.step(time.Second)
	if m.floor.belt != 1 || m.animating() {
		t.Errorf("the marks stand still when the working task is hidden: belt %d, animating %v", m.floor.belt, m.animating())
	}
}

func TestFloorHeadTilesShowAtEverySize(t *testing.T) {
	piled := factoryRepo{name: "r", path: "/r", st: fstatus(5, ftask("m", "merged"), ftask("a", "settled"), ftask("b", "settled"), ftask("c", "settled"))}
	for _, sz := range [][2]int{{80, 24}, {100, 30}, {140, 40}} {
		for name, repos := range map[string][]factoryRepo{"waiting": waitingRepos(), "piled": {piled}} {
			out := plain(floorModel(true, sz[0], sz[1], repos...))
			for _, want := range []string{"BUILD ", "REVIEW ", "MERGE ", "SHIPPED ", "BLOCKED "} {
				if !strings.Contains(out, want) {
					t.Errorf("%s at %dx%d: no head tile %q:\n%s", name, sz[0], sz[1], want, out)
				}
			}
		}
	}
	out := plain(floorModel(true, 100, 30, piled))
	for _, want := range []string{"MERGE 1", "SHIPPED 3"} {
		if !strings.Contains(out, want) {
			t.Errorf("settled tasks queue in the SHIPPED lane, lacks %q:\n%s", want, out)
		}
	}
}

func TestFactoryRefreshesEveryTenSecondsOnTheTab(t *testing.T) {
	m, dir, now := factoryStubModel(t)
	upd(m, only[factoryMsg](upd(m, press("4")))[0])
	if m.view != viewFactory {
		t.Fatalf("on the view %v", m.view)
	}
	*now = now.Add(floorEvery - time.Second)
	if got := only[factoryMsg](upd(m, tickMsg{})); len(got) != 0 {
		t.Fatal("a tick before 10 s starts nothing")
	}
	calls := factoryCalls(t, dir)
	*now = now.Add(2 * time.Second)
	if got := only[factoryMsg](upd(m, tickMsg{})); len(got) != 1 || factoryCalls(t, dir) <= calls {
		t.Fatalf("a tick after 10 s starts one refresh: %d results", len(got))
	}
}

func TestFloorFilter(t *testing.T) {
	m := floorModel(true, 100, 30, fakeFactoryRepos()...)
	send(m, "/")
	out := plain(m)
	for _, want := range []string{"repository", "all", "alpha", "beta-é日本語", "gamma", "enter choose"} {
		if !strings.Contains(out, want) {
			t.Errorf("the dropdown lacks %q:\n%s", want, out)
		}
	}
	if !m.floor.menu || m.floor.menuCur != 0 || !strings.Contains(out, "› ✓ all") {
		t.Errorf("it opens on the current choice, all:\n%s", out)
	}
	for _, k := range []string{"q", "tab", "4", "r", "?", "c"} {
		if cmd := m.handleKey(press(k)); cmd != nil || m.view != viewFactory || m.help || !m.floor.menu {
			t.Fatalf("%s reached the view while the dropdown is open", k)
		}
	}
	send(m, "j", "j", "k", "j", "enter")
	if m.floor.menu || m.floor.repo != "/r/beta" {
		t.Fatalf("enter chooses beta: menu %v repo %q", m.floor.menu, m.floor.repo)
	}
	out = plain(m)
	if !strings.Contains(out, "· beta-é") || strings.Contains(out, "alpha") || !strings.Contains(out, "task-01") {
		t.Errorf("the scene and the subheader follow the filter:\n%s", out)
	}
	if l := m.floorNow(); len(l.crates)+l.more["build"]+l.more["review"]+l.more["merged"]+l.more["settled"]+l.more["blocked"] != 3 {
		t.Errorf("only beta's 3 tasks are on the floor: %d crates", len(m.floorNow().crates))
	}
	send(m, "/", "k", "esc")
	if m.floor.menu || m.floor.repo != "/r/beta" {
		t.Error("esc closes without changing the choice")
	}
	send(m, "/")
	if m.floor.menuCur != 2 {
		t.Errorf("it opens on beta: %d", m.floor.menuCur)
	}
	send(m, "esc")
	m.applyFactory(factoryMsg{repos: fakeFactoryRepos()})
	m.step(time.Millisecond)
	if m.floor.repo != "/r/beta" {
		t.Error("the choice survives a refresh")
	}
	m.applyFactory(factoryMsg{repos: []factoryRepo{alpha()}})
	m.step(time.Millisecond)
	if m.floor.repo != "" || len(m.floorNow().crates) == 0 {
		t.Errorf("a vanished repository returns the filter to all: %q", m.floor.repo)
	}
	send(m, "/", "j", "k", "k", "enter")
	if m.floor.repo != "" {
		t.Error("all chooses every repository")
	}
}

func TestFloorFilterOnlyOnTheFactoryTab(t *testing.T) {
	m := fakeModel(true, 100, 30)
	m.view = viewAgents
	send(m, "/")
	if m.floor.menu || m.filterIn {
		t.Error("/ does nothing on the Agents tab")
	}
}

func TestFloorHonesty(t *testing.T) {
	loading := fakeModel(true, 100, 30)
	loading.view = viewFactory
	if out := plain(loading); !strings.Contains(out, "reading the factory…") || strings.Contains(out, "No task on the floor") || !strings.Contains(out, "BUILD 0") {
		t.Errorf("before the first result the floor is there with a plate:\n%s", out)
	}
	if out := plain(floorModel(true, 100, 30)); !strings.Contains(out, "No task on the floor") || !strings.Contains(out, "BUILD 0") || strings.Contains(out, "reading the factory") {
		t.Errorf("no task: the idle floor and a plate:\n%s", out)
	}
	m := floorModel(true, 100, 30, fakeFactoryRepos()...)
	m.fac.repos[0].st.PRs.AsOf = ptr(float64(m.now().Add(-12 * time.Minute).Unix()))
	out := plain(m)
	if !strings.Contains(out, "⚠ gamma: not inside a git repository") || !strings.Contains(out, "alpha: pull requests as of 12m") {
		t.Errorf("a failed repository and stale pull requests are said under the scene:\n%s", out)
	}
	if m := floorModel(true, 100, 30, fakeFactoryRepos()[1:]...); len(m.floorNow().crates) == 0 || !strings.Contains(plain(m), "task-02") || !strings.Contains(plain(m), "⚠ gamma") {
		t.Errorf("the other repositories still draw:\n%s", plain(m))
	}
	send(m, "/", "j", "j", "j", "enter")
	if out := plain(m); !strings.Contains(out, "No task on the floor") || !strings.Contains(out, "No task in this repository") || !strings.Contains(out, "⚠ gamma") {
		t.Errorf("a filter on a repository that failed: no task, and why:\n%s", out)
	}
}

func TestFloorSurvivesAnyWindow(t *testing.T) {
	for _, sz := range [][2]int{{60, 16}, {61, 17}, {80, 24}, {200, 60}} {
		for _, repos := range [][]factoryRepo{nil, waitingRepos(), {{name: "busy", path: "/b", st: fstatus(99, manyTasks(60)...)}}} {
			m := floorModel(true, sz[0], sz[1], repos...)
			send(m, "/", "j", "enter", "/", "esc")
			for _, line := range strings.Split(plain(m), "\n") {
				if w := len([]rune(line)); w > sz[0]+8 { // wide runes count twice in the width, once in runes
					t.Fatalf("%dx%d: a line of %d runes", sz[0], sz[1], w)
				}
			}
		}
	}
}

// ---- goldens ------------------------------------------------------------------------

func TestGoldenFloor(t *testing.T) {
	shots := os.Getenv("ISSHONI_SHOT_DIR")
	stale := func(m *model) {
		repos := fakeFactoryRepos()
		repos[0].st.PRs.AsOf = ptr(float64(m.now().Add(-12 * time.Minute).Unix()))
		repos[0].fetchErr = "factory status timed out after 25s"
		repos[1].st.PRs.AsOf = ptr(float64(m.now().Add(-3 * time.Minute).Unix()))
		setFloor(m, repos...)
	}
	for _, c := range []struct {
		name string
		w, h int
		set  func(m *model)
	}{
		{"floor-140x40", 140, 40, func(m *model) { setFloor(m, waitingRepos()...) }},
		{"floor-100x30", 100, 30, func(m *model) { setFloor(m, waitingRepos()...) }},
		{"floor-80x24", 80, 24, func(m *model) { setFloor(m, waitingRepos()...) }},
		{"floor-empty-80x24", 80, 24, func(m *model) { setFloor(m) }},
		{"floor-single-100x30", 100, 30, func(m *model) {
			one := ftask("quiet-refactor", "motion")
			one.Session, one.Elapsed, one.Limit = "working", ptr(int64(252)), ptr(int64(2700))
			setFloor(m, factoryRepo{name: "alpha", path: "/r/alpha", st: fstatus(5, one)})
		}},
		{"floor-crowded-80x24", 80, 24, func(m *model) {
			setFloor(m, factoryRepo{name: "busy", path: "/r/busy", st: fstatus(40, manyTasks(12)...)})
		}},
		{"floor-filter-open-100x30", 100, 30, func(m *model) { setFloor(m, waitingRepos()...); send(m, "/", "j") }},
		{"floor-one-repo-100x30", 100, 30, func(m *model) { setFloor(m, waitingRepos()...); send(m, "/", "j", "enter") }},
		{"floor-errors-100x30", 100, 30, stale},
		{"floor-settled-100x30", 100, 30, func(m *model) {
			setFloor(m, factoryRepo{name: "r", path: "/r", st: fstatus(5, ftask("merged-one", "merged"), ftask("a-shipped", "settled"), ftask("b-shipped", "settled"), ftask("c-shipped", "settled"))})
		}},
	} {
		t.Run(c.name, func(t *testing.T) {
			if shots != "" {
				m := fakeModel(false, c.w, c.h)
				c.set(m)
				if err := os.WriteFile(filepath.Join(shots, c.name+".ansi"), []byte(m.Render()+"\n"), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			m := fakeModel(true, c.w, c.h)
			c.set(m)
			got := m.Render() + "\n"
			if strings.ContainsAny(plain(m), "╱╲") {
				t.Errorf("the floor has a diagonal stroke:\n%s", got)
			}
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
