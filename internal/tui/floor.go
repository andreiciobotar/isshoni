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

// The factory floor: one cube per task on a belt. This file decides where every cube
// stands and how it travels; view_floor.go draws it.

const (
	floorMoveFor   = time.Second            // a cube travels from its old place to its new one in this long
	floorBeltEvery = 120 * time.Millisecond // one step of the belt's tread
	floorEvery     = 10 * time.Second       // the refresh interval while the Factory tab is shown
	floorMinScene  = 16                     // rows the scene keeps before notes under it are cut
)

// floorStations are the belt's stations in flow order; every other phase waits on the BLOCKED pad.
var floorStations = []string{"build", "review", "merged", "settled"}

var floorSigns = map[string]string{"build": "BUILD", "review": "REVIEW", "merged": "MERGE", "settled": "SHIPPED", "blocked": "BLOCKED"}

func floorPlace(phase string) string {
	switch phase {
	case "motion":
		return "build"
	case "review", "merged", "settled":
		return phase
	}
	return "blocked"
}

// point is a place on the belt: x the arc length along its centre line (in cells), y the distance along the BLOCKED spur (0 on the belt).
type point struct{ x, y float64 }

// beltSeg is a run of n steps of (dx, dy) cells from (x, y): the belt's centre line is a few of them.
type beltSeg struct{ x, y, dx, dy, n int }

// floorPath is the belt's centre line: four legs, each a diagonal run of hc cells, and the flat run after each.
type floorPath struct {
	segs  []beltSeg
	total int
}

// at is the cell at arc length s; past either end the belt goes on straight.
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

// floorCube is a task's place.
type floorCube struct {
	floorTask
	place string
	at    point
}

// floorLayout is the scene's geometry, a function of the window alone, and where every shown task stands.
//
// The belt is a sawtooth of four legs, one per station: leg 0 runs down-right from the top left, leg 1 up-right, leg 2
// down-right, leg 3 up-right. Each leg is hc cells long; a flat run of flat[i] cells follows leg i, and the station
// stands on it. The band is 6 columns wide, so it reaches 3 rows beyond the legs' corners.
type floorLayout struct {
	w, h    int
	hc      int    // cells of a leg: the band spans hc+6 rows
	xa, yt  int    // where leg 0 starts: column, and the row of the top corners
	yb      int    // the row of the bottom corners
	flat    [4]int // flat run after each leg
	flatX   [4]int // column where each flat run starts
	path    *floorPath
	start   [4]int // arc length where each leg starts
	padTop  int    // the BLOCKED pad's first row
	padCap  int    // cubes the pad holds, side by side
	spur    int    // arc length on leg 0 where the spur leaves
	spurM   int    // cells of the spur that go diagonally down-left
	landing int    // column where the diagonal part ends
	count   map[string]int
	more    map[string]int
	work    map[string]bool
	waits   map[string]bool // a task of the place waits on the human
	cubes   []floorCube
	prefix  bool // labels name the repository
}

// H is the rows the belt spans.
func (l *floorLayout) H() int { return l.hc + 6 }

// padCol is the column of the leftmost cell of the pad's slot j (0 is nearest the belt).
func (l *floorLayout) padCol(j int) int { return 1 + 5*(l.padCap-1-j) }

// pos is the cell of the middle of a cube's middle row.
func (l *floorLayout) pos(p point) (int, int) {
	if p.y <= 0 {
		return l.path.at(p.x)
	}
	jx, jy := l.path.at(float64(l.spur))
	y := int(math.Round(p.y))
	if y <= l.spurM {
		return jx - y, jy + y
	}
	return l.landing - (y - l.spurM), l.padTop + 1
}

// newFloorLayout fits the belt to a w x h scene: legs as long as the height allows, the width left over shared as flat runs.
func newFloorLayout(w, h int) *floorLayout {
	l := &floorLayout{w: w, h: h, xa: 4, count: map[string]int{}, more: map[string]int{}, work: map[string]bool{}, waits: map[string]bool{}}
	l.hc = max(2, min(h-10, (w-l.xa-4-24)/4))
	l.yt = 5 + max(0, (h-4-l.H())/2)
	l.yb = l.yt + l.hc
	slack := w - 4 - l.xa - 4*l.hc // the band ends 4 columns before the right edge, as the left one starts 1 after it
	for i := range l.flat {
		l.flat[i] = slack / 4
		if i >= 4-slack%4 {
			l.flat[i]++
		}
	}
	l.path = &floorPath{}
	x, y, arc := l.xa, l.yt, 0
	for i := range 4 {
		dy := 1 - 2*(i%2)
		l.start[i] = arc
		l.path.segs = append(l.path.segs, beltSeg{x, y, 1, dy, l.hc})
		x, y, arc = x+l.hc, y+dy*l.hc, arc+l.hc
		l.flatX[i] = x
		l.path.segs = append(l.path.segs, beltSeg{x, y, 1, 0, l.flat[i]})
		x, arc = x+l.flat[i], arc+l.flat[i]
	}
	l.path.total = arc

	// the BLOCKED pad: below-left of leg 0, as many cubes side by side as the room there holds
	for p := 4; p >= 1; p-- {
		if top := h - 5 - p; top > l.yt && 5*p <= l.xa+top-l.yt-2 {
			l.padCap, l.padTop = p, top
			break
		}
	}
	if l.padCap > 0 {
		cx0, row := 5*l.padCap-2, l.padTop+1
		l.spur = min(max(1, (cx0+row-l.yt-l.xa+1)/2), l.hc-1)
		l.spurM = max(1, row-l.yt-l.spur)
		l.landing = l.xa + l.spur - l.spurM
	}
	return l
}

// floorSlots are the places of a station's queue, the nearest to the station first: the cells of its leg from 2 (3 when the
// leg starts at a turn) to 2 before its end, 3 rows apart, or 4 when all n cubes fit like that.
func (l *floorLayout) slots(i, n int) []point {
	lo, hi := 3, l.hc-2
	if i == 0 {
		lo = 2
	}
	pitch := 3
	if (hi-lo)/4+1 >= n {
		pitch = 4
	}
	var out []point
	for k := hi; k >= lo || len(out) == 0; k -= pitch {
		out = append(out, point{x: float64(l.start[i] + max(k, 1))})
	}
	return out
}

// floorLayout lays the belt out for a w x h scene and places every shown task: a cube in a slot of the queue
// before its station (the first in order nearest the station), or counted in its station's sign when its place
// has no slot left.
func (m *model) floorLayout(w, h int) *floorLayout {
	byPlace := map[string][]floorTask{}
	repos := map[string]bool{}
	l := newFloorLayout(w, h)
	for _, ft := range m.floorTasks() {
		p := floorPlace(ft.t.Phase)
		byPlace[p] = append(byPlace[p], ft)
		repos[ft.path] = true
		l.work[p] = l.work[p] || ft.t.Session == "working"
		l.waits[p] = l.waits[p] || ft.t.WaitsOnHuman() || (ft.t.Phase == "review" && !ft.t.PRDraft)
	}
	l.prefix = m.floorRepo() == "" && len(repos) > 1
	for i, p := range floorStations {
		l.fill(p, byPlace[p], l.slots(i, len(byPlace[p])))
	}
	var side []point
	for j := range l.padCap {
		cx := l.padCol(j) + 2
		side = append(side, point{float64(l.spur), float64(l.spurM + l.landing - cx)})
	}
	l.fill("blocked", byPlace["blocked"], side)
	return l
}

// fill puts a place's tasks into its slots, the first in order in the first slot. Tasks beyond the slots are counted in l.more.
func (l *floorLayout) fill(place string, tasks []floorTask, slots []point) {
	l.count[place] = len(tasks)
	slices.SortFunc(tasks, floorOrder(place))
	shown := min(len(tasks), len(slots))
	l.more[place] = len(tasks) - shown
	for i, ft := range tasks[:shown] {
		l.cubes = append(l.cubes, floorCube{floorTask: ft, place: place, at: slots[i]})
	}
}

// floorState is the Factory tab's own state: the repository filter and its dropdown, the
// cubes in transit, the belt's tread.
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
	at point
	ft floorTask
}

type floorMove struct {
	from point
	ago  time.Duration
}

type floorGone struct {
	ft       floorTask
	from, to point
	ago      time.Duration
}

func (f *floorState) moving() bool { return len(f.moves) > 0 || len(f.gone) > 0 }

// lerp is the point t of the way from a to b: along the belt, and up or down the spur, never across between them.
func lerp(a, b point, t float64) point {
	d := math.Abs(b.x - a.x)
	s := min(1, max(0, t)) * (a.y + d + b.y)
	switch {
	case s <= a.y && a.y > 0:
		return point{a.x, a.y - s}
	case s <= a.y+d:
		return point{a.x + math.Copysign(s-a.y, b.x-a.x), 0}
	}
	return point{b.x, s - a.y - d}
}

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

// floorOrder sorts a place's tasks from the one nearest the next station back. BUILD goes
// by Elapsed/Limit, the furthest along first, tasks without both last; the rest by slug.
// An order, not a measurement.
func floorOrder(place string) func(a, b floorTask) int {
	ratio := func(t data.FactoryTask) (float64, bool) {
		if t.Elapsed == nil || t.Limit == nil || *t.Limit <= 0 {
			return 0, false
		}
		return float64(*t.Elapsed) / float64(*t.Limit), true
	}
	return func(a, b floorTask) int {
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

// shown is where a cube is drawn: its place, or on the way to it.
func (f *floorState) shown(key string, at point) point {
	if mv := f.moves[key]; mv != nil {
		return lerp(mv.from, at, float64(mv.ago)/float64(floorMoveFor))
	}
	return at
}

// floorSync compares the places with the ones last seen and sets cubes travelling: a new
// task from the intake, a moved one from where it stood, a task that is gone out along the
// belt from where it stood. Another window size or filter, and the first sight of the
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
	for _, c := range l.cubes {
		now[c.key()] = floorSeen{c.at, c.floorTask}
	}
	sig := fmt.Sprint(l.w, l.h, f.repo)
	if f.seen == nil || sig != f.sig {
		f.sig, f.seen, f.moves, f.gone = sig, now, map[string]*floorMove{}, nil
		return
	}
	for _, k := range slices.Sorted(maps.Keys(now)) {
		old, ok := f.seen[k]
		switch {
		case !ok:
			f.moves[k] = &floorMove{from: point{}}
		case old.at != now[k].at:
			f.moves[k] = &floorMove{from: f.shown(k, old.at)}
		}
	}
	for _, k := range slices.Sorted(maps.Keys(f.seen)) {
		if _, ok := now[k]; !ok {
			old := f.seen[k]
			from := f.shown(k, old.at)
			delete(f.moves, k)
			f.gone = append(f.gone, &floorGone{ft: old.ft, from: from, to: point{from.x + 6, from.y}})
		}
	}
	f.seen = now
}

// floorWorking is true while a shown task is in motion with a working session: the belt turns.
func (m *model) floorWorking() bool {
	return slices.ContainsFunc(m.floorTasks(), func(ft floorTask) bool { return ft.t.Phase == "motion" && ft.t.Session == "working" })
}

// floorStep moves the cubes in transit on by dt and turns the belt's tread while a shown session works.
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
