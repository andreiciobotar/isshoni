package tui

import (
	"image/color"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"isshoni/internal/data"
)

func floorCubes(m *model) map[string]floorCube {
	out := map[string]floorCube{}
	for _, c := range m.floorNow().cubes {
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

func TestFloorPlacesEveryPhase(t *testing.T) {
	m := floorModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: fstatus(5,
		ftask("m", "motion"), ftask("b", "blocked"), ftask("v", "review"), ftask("g", "merged"), ftask("s", "settled"), ftask("u", "frozen"))})
	got := floorCubes(m)
	for slug, place := range map[string]string{"m": "build", "b": "blocked", "v": "review", "g": "merged", "s": "settled", "u": "blocked"} {
		if got[slug].place != place {
			t.Errorf("%s is in %q, want %q", slug, got[slug].place, place)
		}
	}
	if !(got["m"].at.x < got["v"].at.x && got["v"].at.x < got["g"].at.x && got["g"].at.x < got["s"].at.x) {
		t.Errorf("the stations follow the flow: %v", got)
	}
	if got["b"].at.y <= 0 || got["m"].at.y != 0 {
		t.Errorf("the BLOCKED pad is up its spur, the others on the belt: %v %v", got["b"].at, got["m"].at)
	}
	if out := plain(m); !strings.Contains(out, "frozen") {
		t.Errorf("an unknown phase is shown as its raw text:\n%s", out)
	}
	for _, want := range []string{"BUILD 1", "REVIEW 1", "MERGE 1", "SHIPPED 1", "BLOCKED 2"} {
		if out := plain(m); !strings.Contains(out, want) {
			t.Errorf("a station sign lacks %q", want)
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
	got := floorCubes(m)
	if len(got) != 4 {
		t.Fatalf("the BUILD queue holds all four at 300x60: %v", got)
	}
	order := []string{"a-far", "b-near", "c-none", "d-nolimit"} // the nearest to REVIEW first; the unknown last, by slug
	for i := 1; i < len(order); i++ {
		if got[order[i-1]].at.x <= got[order[i]].at.x {
			t.Errorf("%s stands upstream of %s: %v", order[i-1], order[i], got)
		}
	}
}

func TestFloorEveryTaskIsACubeOrCounted(t *testing.T) {
	crowded := factoryRepo{name: "busy", path: "/r/busy", st: fstatus(40, manyTasks(12)...)}
	for name, repos := range map[string][]factoryRepo{
		"waiting": waitingRepos(), "fake": fakeFactoryRepos(), "crowded": {crowded}, "none": nil, "one": {alpha()},
	} {
		for _, sz := range [][2]int{{80, 24}, {100, 30}, {140, 40}, {60, 16}} {
			m := floorModel(true, sz[0], sz[1], repos...)
			l := m.floorNow()
			n := len(l.cubes)
			for _, more := range l.more {
				n += more
			}
			if want := rowsOf(repos); n != want {
				t.Errorf("%s at %dx%d: %d cubes and counted, want %d rows", name, sz[0], sz[1], n, want)
			}
			plain(m)
		}
	}
	l := floorModel(true, 80, 24, waitingRepos()...).floorNow()
	belt, siding := 0, 0
	for _, c := range l.cubes {
		if c.place == "blocked" {
			siding++
		} else {
			belt++
		}
	}
	if belt < 6 || siding < 2 {
		t.Errorf("80x24 holds at least 6 cubes on the belt and %d on the BLOCKED pad, got %d and %d", 2, belt, siding)
	}
	// the queues grow with the window
	shown := func(w, h int) int { return len(floorModel(true, w, h, crowded).floorNow().cubes) }
	if a, b, c := shown(80, 24), shown(100, 30), shown(140, 40); a < 3 || b <= a || c <= b {
		t.Errorf("a crowded BUILD queue holds at least 3 cubes at 80x24, then more at 100x30 and 140x40: %d, %d, %d", a, b, c)
	}
	if out := plain(floorModel(true, 80, 24, crowded)); !strings.Contains(out, "BUILD 12 · ") || !strings.Contains(out, " more") {
		t.Errorf("a crowded station keeps its full count and says how many have no slot:\n%s", out)
	}
}

// The belt crosses the window whatever its size, one task or twenty.
func TestFloorBeltSpansTheWindow(t *testing.T) {
	for _, sz := range [][2]int{{80, 24}, {100, 30}, {140, 40}} {
		for name, repos := range map[string][]factoryRepo{
			"one": {{name: "r", path: "/r", st: fstatus(5, ftask("a", "motion"))}},
			"20":  {{name: "r", path: "/r", st: fstatus(40, manyTasks(20)...)}},
		} {
			m := floorModel(true, sz[0], sz[1], repos...)
			l := m.floorNow()
			a := newFloorArt(m, l)
			a.drawBelt()
			minX, maxX, minY, maxY := l.w, -1, l.h, -1
			for i, k := range a.kind {
				if k == kEdge {
					minX, maxX, minY, maxY = min(minX, i%l.w), max(maxX, i%l.w), min(minY, i/l.w), max(maxY, i/l.w)
				}
			}
			if minX > 3 || l.w-1-maxX > 3 {
				t.Errorf("%s at %dx%d: the belt runs from column %d to %d of %d", name, sz[0], sz[1], minX, maxX, l.w)
			}
			if rows := maxY - minY + 1; rows < l.h-4-2 {
				t.Errorf("%s at %dx%d: the belt spans %d rows of %d", name, sz[0], sz[1], rows, l.h)
			}
		}
	}
}

// No two cubes share a cell, none shares one with the belt's edges, a sign or a machine.
func TestFloorCubesKeepApart(t *testing.T) {
	for _, sz := range [][2]int{{80, 24}, {100, 30}, {140, 40}} {
		for name, repos := range map[string][]factoryRepo{
			"one": {{name: "r", path: "/r", st: fstatus(5, ftask("a", "motion"))}},
			"20":  {{name: "r", path: "/r", st: fstatus(40, manyTasks(20)...)}},
			"all": waitingRepos(),
		} {
			m := floorModel(true, sz[0], sz[1], repos...)
			l := m.floorNow()
			a := m.drawFloor(l, nil, 0)
			belt := newFloorArt(m, l)
			belt.drawBelt()
			taken := map[[2]int]string{}
			for _, c := range l.cubes {
				x, y := l.pos(c.at)
				for _, cell := range cubeCells(x, y) {
					if other, ok := taken[cell]; ok {
						t.Errorf("%s at %dx%d: %s and %s share cell %v", name, sz[0], sz[1], c.t.Slug, other, cell)
					}
					taken[cell] = c.t.Slug
					if belt.kind[cell[1]*l.w+cell[0]] == kEdge {
						t.Errorf("%s at %dx%d: %s stands on the belt's edge at %v", name, sz[0], sz[1], c.t.Slug, cell)
					}
					for _, r := range append(slices.Clone(a.machines), a.signs...) {
						if r.has(cell[0], cell[1]) {
							t.Errorf("%s at %dx%d: %s stands on a sign or machine at %v", name, sz[0], sz[1], c.t.Slug, cell)
						}
					}
				}
			}
			if len(a.signs) < 5 || len(a.machines) != 5 {
				t.Errorf("%s at %dx%d: %d signs, %d machines", name, sz[0], sz[1], len(a.signs), len(a.machines))
			}
		}
	}
}

// A cube that travels the belt passes every cell of it, corners included, one step at a time, and ends at the right edge.
func TestFloorPathHasNoGaps(t *testing.T) {
	for _, sz := range [][2]int{{80, 24}, {100, 30}, {140, 40}} {
		l := newFloorLayout(sz[0], sz[1]-3)
		px, py := l.path.at(0)
		for s := 1; s <= l.path.total; s++ {
			x, y := l.path.at(float64(s))
			if x-px < 0 || x-px > 1 || y-py > 1 || py-y > 1 || (x == px && y == py) {
				t.Fatalf("%dx%d: from (%d,%d) to (%d,%d) at arc %d", sz[0], sz[1], px, py, x, y, s)
			}
			px, py = x, y
		}
		if px != sz[0]-4 {
			t.Errorf("%dx%d: the belt ends at column %d", sz[0], sz[1], px)
		}
	}
}

func TestFloorLabelsNameTheRepositoryOnlyWhenSeveralHaveTasks(t *testing.T) {
	if out := plain(floorModel(true, 140, 40, waitingRepos()...)); !strings.Contains(out, "alpha/") {
		t.Errorf("several repositories: the label has the repository:\n%s", out)
	}
	if out := plain(floorModel(true, 140, 40, alpha())); strings.Contains(out, "alpha/") {
		t.Errorf("one repository: no prefix:\n%s", out)
	}
}

func TestFloorCubeMarks(t *testing.T) {
	working, human, over := ftask("work", "motion"), ftask("ask", "blocked"), ftask("late", "motion")
	working.Session, over.Overtime = "working", true
	m := floorModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: fstatus(5, working, human, over)})
	out := plain(m)
	if !strings.Contains(out, "⠋") || !strings.Contains(out, "▲") {
		t.Errorf("a working cube turns its spinner, a cube that waits on the human has a ▲:\n%s", out)
	}
	for _, c := range []struct {
		t     data.FactoryTask
		place string
		want  color.Color
	}{
		{working, "build", m.th.Green},
		{human, "blocked", m.th.Yellow},
		{over, "build", m.th.Red},
		{ftask("p", "frozen"), "blocked", m.th.Red},
		{ftask("q", "motion"), "build", m.th.Muted},
		{ftask("s", "settled"), "settled", m.mergedColor()},
	} {
		if got := m.floorCubeLook(c.t, c.place); key24(got) != key24(c.want) {
			t.Errorf("%s in %s: colour %v, want %v", c.t.Slug, c.place, got, c.want)
		}
	}
}

// transit: a cube moves for a second from its old place to its new one.
func TestFloorCubesTravelBetweenPlaces(t *testing.T) {
	a, b, gone := ftask("a", "motion"), ftask("b", "review"), ftask("gone", "settled")
	m := floorModel(true, 240, 60, factoryRepo{name: "r", path: "/r", st: fstatus(5, a, b, gone)})
	m.floorSync()
	before := floorCubes(m)

	a.Phase = "review"
	fresh := ftask("fresh", "motion")
	m.applyFactory(factoryMsg{repos: []factoryRepo{{name: "r", path: "/r", st: fstatus(5, a, b, fresh)}}})
	m.step(floorMoveFor / 2)
	after := floorCubes(m)

	key := func(slug string) string { return "/r\x00" + slug }
	between := func(p, from, to point) bool {
		return p != from && p != to && (p.x-from.x)*(p.x-to.x) <= 0 && (p.y-from.y)*(p.y-to.y) <= 0
	}
	if now := m.floor.shown(key("a"), after["a"].at); !between(now, before["a"].at, after["a"].at) {
		t.Errorf("a was at %v, goes to %v, is drawn at %v", before["a"].at, after["a"].at, now)
	}
	if now := m.floor.shown(key("fresh"), after["fresh"].at); !between(now, point{}, after["fresh"].at) {
		t.Errorf("a new task comes in at the intake: drawn at %v", now)
	}
	if len(m.floor.gone) != 1 || m.floor.gone[0].ft.t.Slug != "gone" || m.floor.gone[0].from != before["gone"].at {
		t.Errorf("a task that is gone leaves from where it stood: %+v", m.floor.gone)
	}
	if !m.animating() {
		t.Error("the view ticks while a cube is in transit")
	}
	if m.floor.shown(key("b"), after["b"].at) != after["b"].at && before["b"].at == after["b"].at {
		t.Error("a cube that did not change place stands still")
	}
	m.step(floorMoveFor)
	if m.floor.moving() || m.floor.shown(key("a"), after["a"].at) != after["a"].at || m.animating() {
		t.Errorf("after a second the cubes stand in their places and the clock rests: %+v", m.floor)
	}
}

func TestFloorFirstResultOnlyTakesTheCubesIn(t *testing.T) {
	m := fakeModel(true, 100, 30)
	m.view = viewFactory
	m.floorSync()
	m.applyFactory(factoryMsg{repos: []factoryRepo{alpha()}})
	m.floorSync()
	if m.floor.moving() {
		t.Error("the first result puts the cubes in place without a flight")
	}
}

func TestFloorBeltTurnsOnlyWhileASessionWorks(t *testing.T) {
	idle := floorModel(true, 100, 30, factoryRepo{name: "r", path: "/r", st: fstatus(5, ftask("a", "motion"))})
	before := plain(idle)
	idle.step(time.Second)
	if idle.floor.belt != 0 || plain(idle) != before {
		t.Error("the belt stands still when no session is working")
	}
	busy := ftask("a", "motion")
	busy.Session = "working"
	m := floorModel(true, 100, 30, factoryRepo{name: "r", path: "/r", st: fstatus(5, busy)})
	before = plain(m)
	m.step(floorBeltEvery)
	if m.floor.belt != 1 || plain(m) == before {
		t.Errorf("the tread moves one step per tick of work: belt %d", m.floor.belt)
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
		t.Fatalf("a shown working task turns the belt: %d", m.floor.belt)
	}
	m.floor.repo = "/r/idle"
	m.step(time.Second)
	if m.floor.belt != 1 || m.animating() {
		t.Errorf("the belt stands still when the working task is hidden: belt %d, animating %v", m.floor.belt, m.animating())
	}
}

func TestFloorSignsAndPileShowAtEverySize(t *testing.T) {
	piled := factoryRepo{name: "r", path: "/r", st: fstatus(5, ftask("m", "merged"), ftask("a", "settled"), ftask("b", "settled"), ftask("c", "settled"))}
	for _, sz := range [][2]int{{80, 24}, {100, 30}, {140, 40}} {
		for name, repos := range map[string][]factoryRepo{"waiting": waitingRepos(), "piled": {piled}} {
			out := plain(floorModel(true, sz[0], sz[1], repos...))
			for _, want := range []string{"BUILD ", "REVIEW ", "MERGE ", "SHIPPED ", "BLOCKED ", "INTAKE"} {
				if !strings.Contains(out, want) {
					t.Errorf("%s at %dx%d: no sign %q:\n%s", name, sz[0], sz[1], want, out)
				}
			}
		}
	}
	out := plain(floorModel(true, 100, 30, piled))
	for _, want := range []string{"MERGE 1", "SHIPPED 3"} {
		if !strings.Contains(out, want) {
			t.Errorf("settled tasks queue before the dock and keep the signs readable, lacks %q:\n%s", want, out)
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
	if l := m.floorNow(); len(l.cubes)+l.more["build"]+l.more["review"]+l.more["merged"]+l.more["settled"]+l.more["blocked"] != 3 {
		t.Errorf("only beta's 3 tasks are on the floor: %d cubes", len(m.floorNow().cubes))
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
	if m.floor.repo != "" || len(m.floorNow().cubes) == 0 {
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
	if m := floorModel(true, 100, 30, fakeFactoryRepos()[1:]...); len(m.floorNow().cubes) == 0 || !strings.Contains(plain(m), "task-02") || !strings.Contains(plain(m), "⚠ gamma") {
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

// A cube between the belt and the BLOCKED spur goes along the belt, then along the spur, never across.
func TestFloorMoveBetweenBeltAndSpurTurnsAtTheJunction(t *testing.T) {
	belt, spur := point{x: 10}, point{x: 4, y: 6}
	for _, tc := range []struct {
		a, b point
		t    float64
		want point
	}{
		{belt, spur, 0.5, point{x: 4, y: 0}}, // 6 along the belt of 12 in all
		{belt, spur, 0.75, point{x: 4, y: 3}},
		{spur, belt, 0.25, point{x: 4, y: 3}},
		{spur, belt, 0.75, point{x: 7, y: 0}},
	} {
		if got := lerp(tc.a, tc.b, tc.t); got != tc.want {
			t.Errorf("lerp(%v, %v, %v) = %v, want %v", tc.a, tc.b, tc.t, got, tc.want)
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
