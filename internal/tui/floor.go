package tui

import (
	"cmp"
	"fmt"
	"maps"
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
	floorStepX     = 7                      // cells from one belt slot to the next, across
	floorStepY     = 2                      // and down
	floorSideDX    = -8                     // the siding's slots stand this far left of the belt slot beside them
	floorSideDY    = 5                      // and this far below it
	floorMinScene  = 16                     // rows the scene keeps before notes under it are cut
	floorCubeW     = 6
	floorCubeH     = 3
)

// floorStations are the belt's segments in flow order; every other phase waits on the siding.
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

type point struct{ x, y float64 }

// floorTask is a shown task and where it came from.
type floorTask struct {
	repo, path string
	t          data.FactoryTask
}

func (ft floorTask) key() string { return ft.path + "\x00" + ft.t.Slug }

// floorCube is a task's place: the top-left cell of its glyph.
type floorCube struct {
	floorTask
	place string
	at    point
}

// floorChip stands in the last slot of a place that has more tasks than slots; n counts the tasks it hides.
type floorChip struct {
	place string
	at    point
	n     int
}

type floorLayout struct {
	w, h, x0 int
	slots    int            // belt slots, the intake first
	sideN    int            // slots of the siding
	first    map[string]int // the belt slot of each station's first slot
	count    map[string]int // tasks of each place, shown or not
	cubes    []floorCube
	chips    []floorChip
	prefix   bool // labels name the repository
}

func (l *floorLayout) belt(slot int) point {
	return point{float64(l.x0 + floorStepX*slot), float64(floorStepY * slot)}
}

func (l *floorLayout) side(slot int) point {
	b := l.belt(1 + slot)
	return point{b.x + floorSideDX, b.y + floorSideDY}
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

func lerp(a, b point, t float64) point {
	t = min(1, max(0, t))
	return point{a.x + (b.x-a.x)*t, a.y + (b.y-a.y)*t}
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

// floorAlloc gives each station the slots it needs to show a cube and the chip of the rest
// (one slot for a lone task, two for more) when room allows, else one slot; what room is
// left goes one slot at a time to the station with most tasks still without one.
func floorAlloc(demand [4]int, room int) [4]int {
	var n [4]int
	total := 0
	for i, d := range demand {
		n[i] = min(max(d, 1), 2)
		total += n[i]
	}
	if total > room {
		n = [4]int{1, 1, 1, 1}
	}
	for left := room - n[0] - n[1] - n[2] - n[3]; left > 0; left-- {
		best := -1
		for i := range n {
			if demand[i] > n[i] && (best < 0 || demand[i]-n[i] > demand[best]-n[best]) {
				best = i
			}
		}
		if best < 0 {
			break
		}
		n[best]++
	}
	return n
}

// floorLayout places every shown task in a w x h scene: a cube in a slot, or counted by the
// chip of the last slot of its place.
func (m *model) floorLayout(w, h int) *floorLayout {
	tasks := m.floorTasks()
	byPlace := map[string][]floorTask{}
	repos := map[string]bool{}
	for _, ft := range tasks {
		p := floorPlace(ft.t.Phase)
		byPlace[p] = append(byPlace[p], ft)
		repos[ft.path] = true
	}
	l := &floorLayout{w: w, h: h, first: map[string]int{}, count: map[string]int{}, prefix: m.floorRepo() == "" && len(repos) > 1}
	l.x0 = 14
	if w >= 100 {
		l.x0 = 22
	}
	if w >= 140 {
		l.x0 = 26
	}
	var demand [4]int
	for i, p := range floorStations {
		demand[i] = len(byPlace[p])
	}
	room := max(len(floorStations), min((h-5)/floorStepY, (w-l.x0-16)/floorStepX))
	n := floorAlloc(demand, room)
	l.slots = 1
	for i, p := range floorStations {
		l.first[p] = l.slots
		l.slots += n[i]
		l.fill(p, byPlace[p], n[i], func(slot int) point { return l.belt(l.first[p] + slot) })
	}
	l.sideN = min(max(1, len(byPlace["blocked"])), max(2, n[0]), max(1, (h-8)/floorStepY))
	l.fill("blocked", byPlace["blocked"], l.sideN, l.side)
	return l
}

// fill puts a place's tasks into its n slots, the nearest to the next station in the last
// slot. When there are more tasks than slots the first slot holds a chip.
func (l *floorLayout) fill(place string, tasks []floorTask, n int, at func(slot int) point) {
	l.count[place] = len(tasks)
	slices.SortFunc(tasks, floorOrder(place))
	shown := min(len(tasks), n)
	if len(tasks) > n {
		shown = n - 1
		l.chips = append(l.chips, floorChip{place, at(0), len(tasks) - shown})
	}
	for i, ft := range tasks[:shown] {
		l.cubes = append(l.cubes, floorCube{ft, place, at(n - 1 - i)})
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
			f.moves[k] = &floorMove{from: l.belt(0)}
		case old.at != now[k].at:
			f.moves[k] = &floorMove{from: f.shown(k, old.at)}
		}
	}
	for _, k := range slices.Sorted(maps.Keys(f.seen)) {
		if _, ok := now[k]; !ok {
			old := f.seen[k]
			from := f.shown(k, old.at)
			delete(f.moves, k)
			f.gone = append(f.gone, &floorGone{ft: old.ft, from: from, to: point{from.x + 2*floorStepX, from.y + 2*floorStepY}})
		}
	}
	f.seen = now
}

// floorStep moves the cubes in transit on by dt and turns the belt's tread while a session works.
func (m *model) floorStep(dt time.Duration) {
	m.floorSync()
	f := &m.floor
	for k, mv := range f.moves {
		if mv.ago += dt; mv.ago >= floorMoveFor {
			delete(f.moves, k)
		}
	}
	f.gone = slices.DeleteFunc(f.gone, func(g *floorGone) bool { g.ago += dt; return g.ago >= floorMoveFor })
	if m.view == viewFactory && m.factoryWorking() {
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

// floorSubheader is the totals line with the chosen repository after it; the totals give way to keep the name whole.
func (m *model) floorSubheader(p paint) string {
	left := m.factorySubheader(p, "Factory")
	want := m.floorRepo()
	i := slices.IndexFunc(m.fac.repos, func(r factoryRepo) bool { return r.path == want })
	if want == "" || i < 0 {
		return left
	}
	tag := p.t("  · filter ", m.th.Faint) + p.b(m.fac.repos[i].name, m.th.Purple)
	return trunc(left, max(1, m.w-3-width(tag))) + tag
}
