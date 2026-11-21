package tui

import (
	"cmp"
	"fmt"
	"maps"
	"math"
	"slices"
	"time"

	"charm.land/bubbles/v2/key"
	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

// The factory floor seen from above: one crate per task on a conveyor that snakes down the body in five
// full-width lanes. This file decides where every crate stands and how it travels; floor_art.go draws it.

const (
	floorMoveFor   = time.Second            // a crate travels from its old place to its new one in this long
	floorBeltEvery = 120 * time.Millisecond // one step of the flow marks
	floorEvery     = 10 * time.Second       // the refresh interval while the Factory tab is shown
	floorMinScene  = 16                     // rows the scene keeps before notes under it are cut
	floorGate      = 4                      // columns of a junction block
	floorGap       = 3                      // cells between two crates: the room of a flow mark
)

// floorLanes are the lanes top to bottom. The conveyor snakes through them: BLOCKED flows left into the start of BUILD,
// BUILD right, REVIEW left, MERGE right, SHIPPED left, so lane k flows right when k is odd.
var floorLanes = []string{"blocked", "build", "review", "merged", "settled"}

var floorSigns = map[string]string{"build": "BUILD", "review": "REVIEW", "merged": "MERGE", "settled": "SHIPPED", "blocked": "BLOCKED"}

// floorPlace is the lane a task stands in. A task held on its base is on the siding whatever its phase.
func floorPlace(t data.FactoryTask) string {
	if t.StackHold != nil {
		return "blocked"
	}
	switch t.Phase {
	case "motion":
		return "build"
	case "review", "merged", "settled":
		return t.Phase
	}
	return "blocked"
}

// beltSeg is a run of n steps of (dx, dy) cells from (x, y); the conveyor's centre line is a few of them.
type beltSeg struct{ x, y, dx, dy, n int }

// floorPath is the conveyor's centre line, cell by cell.
type floorPath struct{ segs []beltSeg }

// at is the cell at arc length s; past either end the conveyor goes on straight.
func (p *floorPath) at(s float64) (int, int) {
	k := int(math.Round(s))
	for i, sg := range p.segs {
		if k <= sg.n || i == len(p.segs)-1 {
			k = max(k, 0)
			return sg.x + sg.dx*k, sg.y + sg.dy*k
		}
		k -= sg.n
	}
	return 0, 0
}

// floorTask is a shown task and where it came from.
type floorTask struct {
	repo, path string
	t          data.FactoryTask
}

func (ft floorTask) key() string { return ft.path + "\x00" + ft.t.Slug }

// floorCrate is a task's place: its lane, the arc length of its middle along the conveyor, and the cells of its tile.
type floorCrate struct {
	floorTask
	place string
	at    float64
	rect  rect
}

// floorLane is a lane's rows: the inner ones start at top, and its crates are crate rows tall.
type floorLane struct{ top, inner, crate int }

// cy is the lane's centre row: the middle row of its crates, where its flow marks and name stand.
func (ln floorLane) cy() int { return ln.top + (ln.inner-ln.crate)/2 + ln.crate/2 }

// floorJunction is a solid block that is the gate between two lanes; it takes the colour of the lane it leads into.
type floorJunction struct {
	rect
	place string
}

// floorLayout is the scene's geometry, a function of the window and the counts, and where every shown task stands.
//
// The edge row above lane k is lanes[k].top-1; the last edge closes the floor. Head tiles stand at the left, junction
// blocks of floorGate columns at the ends; crates queue from the head tile, floorGap cells apart.
type floorLayout struct {
	w, h      int
	lanes     [5]floorLane
	hw        int // width of a head tile
	x0, x1    int // crates stand in [x0, x1)
	capW      int // the widest crate
	xl, xr    int // the columns the conveyor turns at, inside the left and right junctions
	path      *floorPath
	start     [5]int // arc length where each lane starts
	heads     [5]rect
	junctions []floorJunction
	count     map[string]int
	more      map[string]int  // tasks of a lane without room for a crate
	work      map[string]bool // a task of the lane has a working session
	waits     map[string]bool // a task of the lane waits on the human
	crates    []floorCrate
	prefix    bool // crates name the repository
}

// active is whether a lane holds work or a task that waits on the human: its junction and head tile are bright.
func (l *floorLayout) active(place string) bool { return l.work[place] || l.waits[place] }

// newFloorLayout shares the rows of a w x h scene among the lanes, their edges and the closing edge: a lane has 1 inner
// row and the rest are handed out one by one, BUILD first. A lane of 3 or more inner rows has 3-row crates.
func newFloorLayout(w, h int) *floorLayout {
	l := &floorLayout{w: w, h: h, count: map[string]int{}, more: map[string]int{}, work: map[string]bool{}, waits: map[string]bool{}}
	l.capW = min(24, max(16, w/5))
	spare := max(0, h-11)
	for i, k := range []int{1, 2, 3, 4, 0} {
		l.lanes[k].inner = 1 + spare/5
		if i < spare%5 {
			l.lanes[k].inner++
		}
	}
	row := 0
	for k := range l.lanes {
		ln := &l.lanes[k]
		ln.top, ln.crate = row+1, 1
		if ln.inner >= 3 {
			ln.crate = 3
		}
		row += ln.inner + 1
	}
	l.xl, l.xr = floorGate/2, w-1-floorGate/2
	length := l.xr - l.xl
	l.path = &floorPath{}
	arc := 0
	for k, ln := range l.lanes {
		dx, x := -1, l.xr
		if k%2 == 1 {
			dx, x = 1, l.xl
		}
		l.start[k] = arc
		l.path.segs = append(l.path.segs, beltSeg{x, ln.cy(), dx, 0, length})
		arc += length
		if k < 4 {
			down := l.lanes[k+1].cy() - ln.cy()
			l.path.segs = append(l.path.segs, beltSeg{x + dx*length, ln.cy(), 0, 1, down})
			arc += down
		}
	}
	rows := func(a, b int) rect { // the inner rows of lane a through those of lane b
		return rect{0, l.lanes[a].top, floorGate, l.lanes[b].top + l.lanes[b].inner - l.lanes[a].top}
	}
	right := func(r rect) rect { r.x = w - floorGate; return r }
	l.junctions = []floorJunction{
		{rect{0, l.lanes[1].top - 1, floorGate, 1}, "blocked"}, // where the siding leaves BUILD
		{right(rows(1, 2)), "review"}, {rows(2, 3), "merged"}, {right(rows(3, 4)), "settled"},
		{rows(4, 4), "settled"}, // the dock
	}
	return l
}

// arc is the arc length of the middle of a crate whose tile starts at x, w wide, in lane k.
func (l *floorLayout) arc(k, x, w int) float64 {
	xc := x + w/2
	if k%2 == 1 {
		return float64(l.start[k] + xc - l.xl)
	}
	return float64(l.start[k] + l.xr - xc)
}

// floorLayout lays the lanes out for a w x h scene and places every shown task in its lane, crate after crate from
// the head tile (the first in order next to it); what has no room is counted in its head tile.
func (m *model) floorLayout(w, h int) *floorLayout {
	byPlace := map[string][]floorTask{}
	repos := map[string]bool{}
	l := newFloorLayout(w, h)
	for _, ft := range m.floorTasks() {
		p := floorPlace(ft.t)
		byPlace[p] = append(byPlace[p], ft)
		repos[ft.path] = true
		l.work[p] = l.work[p] || ft.t.Session == "working"
		l.waits[p] = l.waits[p] || waitsForYou(ft.t)
	}
	l.prefix = m.floorRepo() == "" && len(repos) > 1
	need := 14 // "SHIPPED 99", the edges and a space inside each
	for k, p := range floorLanes {
		l.count[p] = len(byPlace[p])
		name, more := width(l.signName(p)), 0
		if l.count[p] > 0 {
			more = width(fmt.Sprintf(" · %d more", l.count[p]))
		}
		if l.lanes[k].inner >= 2 { // what has no room is a second line
			need = max(need, name+4, more+4)
		} else {
			need = max(need, name+more+4)
		}
	}
	l.hw = min(need, w/4)
	l.x0, l.x1 = floorGate+l.hw+3, w-floorGate-1
	for k, p := range floorLanes {
		l.heads[k] = rect{floorGate, l.lanes[k].top, l.hw, l.lanes[k].inner}
		l.fill(k, p, byPlace[p])
	}
	return l
}

// fill puts a lane's tasks into crates, the first in order next to the head tile. The tasks beyond the room are counted in l.more.
func (l *floorLayout) fill(k int, place string, tasks []floorTask) {
	slices.SortFunc(tasks, floorOrder(place))
	ln := l.lanes[k]
	x := l.x0
	for i, ft := range tasks {
		cw := l.crateWidth(ft)
		if x+cw > l.x1 {
			l.more[place] = len(tasks) - i
			return
		}
		l.crates = append(l.crates, floorCrate{floorTask: ft, place: place, at: l.arc(k, x, cw), rect: rect{x, ln.top + (ln.inner-ln.crate)/2, cw, ln.crate}})
		x += cw + floorGap
	}
}

// floorState is the Factory tab's own state: the repository filter and its dropdown, the
// crates in transit, the flow marks' step.
type floorState struct {
	repo    string // the chosen repository's path; empty is all
	menu    bool
	menuCur int

	sig   string // what the places were computed for; another one forgets them
	seen  map[string]floorSeen
	moves map[string]*floorMove
	gone  []*floorGone

	belt    int
	beltAcc time.Duration
}

type floorSeen struct {
	at float64
	ft floorTask
}

type floorMove struct {
	from float64
	ago  time.Duration
}

type floorGone struct {
	ft       floorTask
	from, to float64
	ago      time.Duration
}

func (f *floorState) moving() bool { return len(f.moves) > 0 || len(f.gone) > 0 }

// lerp is the point t of the way from arc length a to b.
func lerp(a, b, t float64) float64 { return a + (b-a)*min(1, max(0, t)) }

// floorRepo is the chosen repository's path, or empty when none is chosen or it is gone from the data.
func (m *model) floorRepo() string {
	if m.floor.repo != "" && slices.ContainsFunc(m.fac.repos, func(r factoryRepo) bool { return r.path == m.floor.repo }) {
		return m.floor.repo
	}
	return ""
}

func (m *model) floorTasks() []floorTask {
	want := m.floorRepo()
	var out []floorTask
	for _, r := range m.fac.repos {
		if r.st == nil || (want != "" && r.path != want) {
			continue
		}
		for _, t := range r.st.Rows {
			out = append(out, floorTask{r.name, r.path, t})
		}
	}
	return out
}

// floorOrder sorts a place's tasks from the head tile on. BUILD goes by Elapsed/Limit, the
// furthest along first, tasks without both last; the siding puts those that wait on the human first, then
// the held; the rest by slug. An order, not a measurement.
func floorOrder(place string) func(a, b floorTask) int {
	ratio := func(t data.FactoryTask) (float64, bool) {
		if t.Elapsed == nil || t.Limit == nil || *t.Limit <= 0 {
			return 0, false
		}
		return float64(*t.Elapsed) / float64(*t.Limit), true
	}
	rank := func(t data.FactoryTask) int { // on the siding: the human's first, then the held
		switch {
		case waitsForYou(t):
			return 0
		case t.StackHold != nil:
			return 1
		}
		return 2
	}
	return func(a, b floorTask) int {
		if place == "blocked" {
			if c := cmp.Compare(rank(a.t), rank(b.t)); c != 0 {
				return c
			}
		}
		if place == "build" {
			ra, oka := ratio(a.t)
			rb, okb := ratio(b.t)
			switch {
			case oka && okb:
				if c := cmp.Compare(rb, ra); c != 0 {
					return c
				}
			case oka != okb:
				if oka {
					return -1
				}
				return 1
			}
		}
		return cmp.Or(cmp.Compare(a.t.Slug, b.t.Slug), cmp.Compare(a.repo, b.repo), cmp.Compare(a.path, b.path))
	}
}

// floorNote is a line under the scene.
type floorNote struct {
	text string
	err  bool
}

// floorNotes is what the scene cannot say: a repository whose status failed, and the age
// of its pull request data. Errors come first; what does not fit in h rows is counted.
func (m *model) floorNotes(h int) []floorNote {
	want := m.floorRepo()
	var errs, prs []floorNote
	for _, r := range m.fac.repos {
		switch {
		case want != "" && r.path != want:
		case r.err != "":
			errs = append(errs, floorNote{"⚠ " + r.name + ": " + r.err, true})
		case r.st != nil:
			if l := m.prLine(r); l != "" {
				prs = append(prs, floorNote{r.name + ": " + l, false})
			}
		}
	}
	notes := append(errs, prs...)
	if room := max(1, h-floorMinScene); len(notes) > room {
		notes = append(notes[:room-1:room-1], floorNote{fmt.Sprintf("+%d more notes", len(notes)-room+1), false})
	}
	return notes
}

// floorNow is the layout of what the window shows.
func (m *model) floorNow() *floorLayout {
	bh := m.h - 3
	return m.floorLayout(m.w, bh-len(m.floorNotes(bh)))
}

// shown is where a crate is drawn: its place, or on the way to it.
func (f *floorState) shown(key string, at float64) float64 {
	if mv := f.moves[key]; mv != nil {
		return lerp(mv.from, at, float64(mv.ago)/float64(floorMoveFor))
	}
	return at
}

// floorSync compares the places with the ones last seen and sets crates travelling: a new
// task from the intake, a moved one from where it stood, a task that is gone out along the
// conveyor from where it stood. Another window size or filter, and the first sight of the
// floor, only take the places in.
func (m *model) floorSync() {
	f := &m.floor
	f.repo = m.floorRepo()
	if m.view != viewFactory || m.fac.doneAt.IsZero() || m.w < minWidth || m.h < minHeight {
		f.seen, f.moves, f.gone = nil, nil, nil
		return
	}
	l := m.floorNow()
	now := map[string]floorSeen{}
	for _, c := range l.crates {
		now[c.key()] = floorSeen{c.at, c.floorTask}
	}
	sig := fmt.Sprint(l.w, l.h, f.repo)
	if f.seen == nil || sig != f.sig {
		f.sig, f.seen, f.moves, f.gone = sig, now, map[string]*floorMove{}, nil
		return
	}
	intake := float64(l.start[1]) // the mouth at the start of BUILD
	for _, k := range slices.Sorted(maps.Keys(now)) {
		old, ok := f.seen[k]
		switch {
		case !ok:
			f.moves[k] = &floorMove{from: intake}
		case old.at != now[k].at:
			f.moves[k] = &floorMove{from: f.shown(k, old.at)}
		}
	}
	for _, k := range slices.Sorted(maps.Keys(f.seen)) {
		if _, ok := now[k]; !ok {
			old := f.seen[k]
			from := f.shown(k, old.at)
			delete(f.moves, k)
			f.gone = append(f.gone, &floorGone{ft: old.ft, from: from, to: from + 6})
		}
	}
	f.seen = now
}

// floorWorking is true while a shown task has a working session: the flow marks of its lane step.
func (m *model) floorWorking() bool {
	return slices.ContainsFunc(m.floorTasks(), func(ft floorTask) bool { return ft.t.Session == "working" })
}

// floorStep moves the crates in transit on by dt and steps the flow marks while a shown session works.
func (m *model) floorStep(dt time.Duration) {
	m.floorSync()
	f := &m.floor
	for k, mv := range f.moves {
		if mv.ago += dt; mv.ago >= floorMoveFor {
			delete(f.moves, k)
		}
	}
	f.gone = slices.DeleteFunc(f.gone, func(g *floorGone) bool { g.ago += dt; return g.ago >= floorMoveFor })
	if m.view == viewFactory && m.floorWorking() {
		f.beltAcc += dt
		f.belt += int(f.beltAcc / floorBeltEvery)
		f.beltAcc %= floorBeltEvery
	}
}

// ---- the repository filter -------------------------------------------------------------

func (m *model) floorOpenMenu() {
	f := &m.floor
	f.menu, f.menuCur = true, 0
	if i := slices.IndexFunc(m.fac.repos, func(r factoryRepo) bool { return r.path == m.floorRepo() }); i >= 0 {
		f.menuCur = i + 1
	}
}

// floorMenuKey handles every key while the dropdown is open; the caller has taken ctrl+c.
func (m *model) floorMenuKey(msg tea.KeyPressMsg) tea.Cmd {
	f := &m.floor
	f.menuCur = min(f.menuCur, len(m.fac.repos))
	switch {
	case key.Matches(msg, keys.Back):
		f.menu = false
	case key.Matches(msg, keys.Down):
		f.menuCur = min(f.menuCur+1, len(m.fac.repos))
	case key.Matches(msg, keys.Up):
		f.menuCur = max(f.menuCur-1, 0)
	case key.Matches(msg, keys.Open):
		f.repo, f.menu = "", false
		if f.menuCur > 0 {
			f.repo = m.fac.repos[f.menuCur-1].path
		}
		m.floorSync()
	}
	return nil
}

// floorSubheader is the totals line with the chosen repository after it; the name gives way (cut, then left out) to keep the totals whole.
func (m *model) floorSubheader(p paint) string {
	left := m.factorySubheader(p, "Factory")
	want := m.floorRepo()
	i := slices.IndexFunc(m.fac.repos, func(r factoryRepo) bool { return r.path == want })
	if want == "" || i < 0 {
		return left
	}
	room := m.w - 3 - width(left) - 4
	if room < 2 {
		return left
	}
	return left + p.t("  · ", m.th.Faint) + p.b(trunc(m.fac.repos[i].name, room), m.th.Purple)
}
