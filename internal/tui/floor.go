package tui

import (
	"cmp"
	"fmt"
	"maps"
	"math"
	"slices"
	"sort"
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
	floorPile      = 3                      // settled cubes stacked on the dock pad
	floorSide      = 2                      // cubes the BLOCKED pad holds
	floorMinScene  = 16                     // rows the scene keeps before notes under it are cut
	floorStep      = 0.5                    // floor units between two samples of the belt's centre line
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

// point is a place on the belt: x the arc length along it, y the distance up the BLOCKED spur (0 on the belt).
type point struct{ x, y float64 }

// fpt is a spot on the floor: u runs down-right on screen, v up-right; a unit is 2 dots across and 1 down.
type fpt struct{ u, v float64 }

// floorPath is the belt's centre line, sampled, with its direction and arc length at each sample.
type floorPath struct {
	pts    []fpt
	tan    []fpt
	s      []float64
	legEnd []float64 // arc length where each leg's straight part ends
}

// newFloorPath walks legs (corner to corner) along the two floor axes in turn, starting on +u,
// rounding every corner with a quarter circle of radius r. The odd legs run along v: a negative
// length runs down-left on screen, a positive one up-right.
func newFloorPath(lens []float64, r float64) *floorPath {
	p := &floorPath{}
	pos := fpt{}
	add := func(q, t fpt) {
		if n := len(p.pts); n == 0 {
			p.s = append(p.s, 0)
		} else {
			p.s = append(p.s, p.s[n-1]+math.Hypot(q.u-p.pts[n-1].u, q.v-p.pts[n-1].v))
		}
		p.pts, p.tan = append(p.pts, q), append(p.tan, t)
	}
	dir := func(i int) fpt {
		if i%2 == 0 {
			return fpt{1, 0}
		}
		return fpt{0, math.Copysign(1, lens[i])}
	}
	for i, l := range lens {
		l = math.Abs(l)
		d := dir(i)
		last := i == len(lens)-1
		n := l
		if i > 0 {
			n -= r
		}
		if !last {
			n -= r
		}
		n = math.Max(0, n)
		start := pos
		if i == 0 {
			add(pos, d)
		}
		for k := 1; k <= int(math.Ceil(n/floorStep)); k++ {
			t := math.Min(n, float64(k)*floorStep)
			pos = fpt{start.u + d.u*t, start.v + d.v*t}
			add(pos, d)
		}
		p.legEnd = append(p.legEnd, p.s[len(p.s)-1])
		if last {
			break
		}
		nd := dir(i + 1)
		c := fpt{pos.u + r*nd.u, pos.v + r*nd.v}
		steps := int(math.Ceil(math.Pi / 2 * r / floorStep))
		for k := 1; k <= steps; k++ {
			phi := float64(k) / float64(steps) * math.Pi / 2
			q := fpt{c.u + r*(-nd.u*math.Cos(phi)+d.u*math.Sin(phi)), c.v + r*(-nd.v*math.Cos(phi)+d.v*math.Sin(phi))}
			add(q, fpt{d.u*math.Cos(phi) + nd.u*math.Sin(phi), d.v*math.Cos(phi) + nd.v*math.Sin(phi)})
			pos = q
		}
	}
	return p
}

func (p *floorPath) total() float64 { return p.s[len(p.s)-1] }

// at is the spot and direction at arc length s; past either end the belt goes on straight.
func (p *floorPath) at(s float64) (fpt, fpt) {
	n := len(p.pts) - 1
	switch {
	case s <= 0:
		return fpt{p.pts[0].u + p.tan[0].u*s, p.pts[0].v + p.tan[0].v*s}, p.tan[0]
	case s >= p.s[n]:
		d := s - p.s[n]
		return fpt{p.pts[n].u + p.tan[n].u*d, p.pts[n].v + p.tan[n].v*d}, p.tan[n]
	}
	i := sort.SearchFloat64s(p.s, s)
	if i == 0 {
		return p.pts[0], p.tan[0]
	}
	t := (s - p.s[i-1]) / math.Max(1e-9, p.s[i]-p.s[i-1])
	a, b := p.pts[i-1], p.pts[i]
	return fpt{a.u + (b.u-a.u)*t, a.v + (b.v-a.v)*t}, p.tan[i]
}

// newFloorSerpentine is the belt for a w x h scene and the length of its tail past the dock: seven legs, long
// ones down-right (+u) joined by short ones along v, a station at the end of the 1st, 3rd, 5th and 7th. It
// enters at the left edge and leaves at the right one. The joins run down-left, so that every leg descends,
// as long as the height allows; otherwise some run up-right, which gives back height.
func newFloorSerpentine(w, h int, e float64) (*floorPath, float64) {
	r, tail := 0.4*e, e
	x := float64(2*w-4) / 2          // across, in floor units (2 dots each): the long legs plus the joins' signed sum
	avail := float64(4*h) - 4*e - 10 // the cubes, the signs and the BLOCKED spur stand above and beside the belt
	minB := 2*r + 0.2
	// the height spanned is x minus twice the joins' signed sum
	b, signs := minB, []float64{1, 1, 1}
	for _, p := range [][]float64{{-1, -1, -1}, {-1, 1, -1}, {1, -1, 1}} {
		k := p[0] + p[1] + p[2]
		if k < 0 {
			if sb := (avail - x) / (-2 * k); sb >= minB {
				b, signs = min(sb, x/4), p
				break
			}
		} else if x-2*k*minB <= avail {
			signs = p
			break
		}
	}
	v := (signs[0] + signs[1] + signs[2]) * b
	a := math.Max(0, (x-tail-v)/6.6)
	return newFloorPath([]float64{3.5 * a, signs[0] * b, 1.2 * a, signs[1] * b, a, signs[2] * b, 0.9*a + tail}, r), tail
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
	level int // cubes below it in its pile
	pile  int // on the lowest one: the cubes above it
}

type floorLayout struct {
	w, h   int
	e      float64            // the floor side of a cube: 3 or 4 units, by window width
	path   *floorPath         // the belt's centre line
	oy     float64            // dots of the floor's origin, down; across it is 0, the belt enters at the left edge
	stn    map[string]float64 // arc length of each station's centre
	spur   float64            // arc length where the BLOCKED spur leaves the belt
	count  map[string]int     // tasks of each place, shown or not
	more   map[string]int     // tasks of each place that have no slot
	work   map[string]bool    // a task of the place has a working session
	cubes  []floorCube
	prefix bool // labels name the repository
}

func (l *floorLayout) bw() float64      { return 0.8 * l.e }  // half the belt's width
func (l *floorLayout) pitch() float64   { return 1.35 * l.e } // from one cube of a queue to the next
func (l *floorLayout) hs() float64      { return 0.9 * l.e }  // half a station's length along the belt
func (l *floorLayout) spurFar() float64 { return 1.6*l.e + float64(floorSide-1)*l.pitch() }

// fp is the floor spot of a place on the belt or the spur.
func (l *floorLayout) fp(p point) fpt {
	q, _ := l.path.at(p.x)
	q.v += p.y
	return q
}

// xy is the dot of a floor spot z dots above the floor.
func (l *floorLayout) xy(q fpt, z float64) (float64, float64) {
	return 2 * (q.u + q.v), l.oy + q.u - q.v - z
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

// floorLayout lays the belt out for a w x h scene and places every shown task: a cube in a slot
// of the queue before its station (the first in order nearest the station), settled ones
// stacked on the dock pad, or counted in its station's sign when its place has no slot left.
func (m *model) floorLayout(w, h int) *floorLayout {
	byPlace := map[string][]floorTask{}
	repos := map[string]bool{}
	l := &floorLayout{w: w, h: h, stn: map[string]float64{}, count: map[string]int{}, more: map[string]int{}, work: map[string]bool{}}
	for _, ft := range m.floorTasks() {
		p := floorPlace(ft.t.Phase)
		byPlace[p] = append(byPlace[p], ft)
		repos[ft.path] = true
		l.work[p] = l.work[p] || ft.t.Session == "working"
	}
	l.prefix = m.floorRepo() == "" && len(repos) > 1
	l.e = 3
	switch {
	case w >= 200:
		l.e = 5
	case w >= 130:
		l.e = 4
	}
	e := l.e
	var tail float64
	l.path, tail = newFloorSerpentine(w, h, e)
	for i, place := range floorStations {
		l.stn[place] = l.path.legEnd[2*i] - l.hs()
	}
	l.stn["settled"] = l.path.total() - tail
	l.spur = 1.2 * e

	// Vertically, the belt and the spur's far cube are centred in the scene.
	minY, maxY := math.Inf(1), math.Inf(-1)
	for _, q := range l.path.pts {
		minY, maxY = math.Min(minY, q.u-q.v), math.Max(maxY, q.u-q.v)
	}
	sp, _ := l.path.at(l.spur)
	minY = math.Min(minY, sp.u-sp.v-l.spurFar()-3*e-2)
	maxY += 2 * e
	l.oy = math.Max(0, (float64(4*h)-(maxY-minY))/2-minY)

	for _, p := range floorStations {
		slots := []point{{l.stn[p], 0}}
		if p != "settled" {
			slots = l.slots(p)
		}
		l.fill(p, byPlace[p], slots)
	}
	var side []point
	for k := range floorSide {
		side = append(side, point{l.spur, l.spurFar() - float64(k)*l.pitch()})
	}
	l.fill("blocked", byPlace["blocked"], side)
	return l
}

// slots are the places of a queue: as many cubes as fit between the previous station and this one.
func (l *floorLayout) slots(place string) []point {
	lo := l.spur + 0.8*l.e
	for _, p := range floorStations {
		if p == place {
			break
		}
		lo = l.stn[p] + l.hs() + 0.5*l.e
	}
	var out []point
	for s := l.stn[place] - l.hs() - 0.5*l.e; s >= lo-1e-9 || len(out) == 0; s -= l.pitch() {
		out = append(out, point{s, 0})
	}
	return out
}

// fill puts a place's tasks into its slots, the first in order in the first slot; settled ones
// stack floorPile high in their one slot. Tasks beyond the slots are counted in l.more.
func (l *floorLayout) fill(place string, tasks []floorTask, slots []point) {
	l.count[place] = len(tasks)
	slices.SortFunc(tasks, floorOrder(place))
	shown := min(len(tasks), len(slots))
	if place == "settled" {
		shown = min(len(tasks), floorPile)
	}
	l.more[place] = len(tasks) - shown
	for i, ft := range tasks[:shown] {
		c := floorCube{floorTask: ft, place: place}
		if place == "settled" {
			c.at, c.level = slots[0], i
			if i == 0 {
				c.pile = shown - 1
			}
		} else {
			c.at = slots[i]
		}
		l.cubes = append(l.cubes, c)
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
			f.gone = append(f.gone, &floorGone{ft: old.ft, from: from, to: point{from.x + 3*l.e, from.y}})
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
