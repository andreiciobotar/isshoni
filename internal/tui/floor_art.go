package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"slices"
	"strings"

	"isshoni/internal/data"
)

// The floor's drawing: thin line art in box-drawing characters, one stroke a character, over a faint
// lattice of dots. The floor axes are the cell diagonals: ╲ goes one column right per row down, ╱ one
// column right per row up. Layout (floor.go) decides where things stand; this file draws them.

type rect struct{ x, y, w, h int }

func (r rect) has(x, y int) bool { return x >= r.x && x < r.x+r.w && y >= r.y && y < r.y+r.h }

// cell kinds: what stands in a cell of the scene.
const (
	kEdge    = 'e' // the belt's two edges and its turns
	kMachine = 'm' // a station or the intake chute
	kSign    = 's'
	kCube    = 'c'
	kLabel   = 'l'
	kPad     = 'p' // the BLOCKED pad and its spur
	kPlate   = 'h'
)

type floorArt struct {
	m    *model
	l    *floorLayout
	cv   *canvas
	kind []byte // what stands in each cell; 0 is empty
	band []bool // the inside of the belt: cubes ride there, text stays out
	// rectangles of what the tests keep cubes away from
	machines, signs           []rect
	lattice, edge, tick, mach color.Color
}

func newFloorArt(m *model, l *floorLayout) *floorArt {
	th := m.th
	return &floorArt{
		m: m, l: l, cv: newCanvas(l.w, l.h), kind: make([]byte, l.w*l.h), band: make([]bool, l.w*l.h),
		lattice: mix(th.Bg, th.Border, 0.6), edge: mix(th.Border, th.Muted, 0.45), tick: th.Faint, mach: th.Muted,
	}
}

func (a *floorArt) in(x, y int) bool { return x >= 0 && y >= 0 && x < a.cv.w && y < a.cv.h }

func (a *floorArt) put(x, y int, r rune, col color.Color, k byte) {
	if !a.in(x, y) {
		return
	}
	a.cv.put(x, y, r, col, false)
	if k != 0 {
		a.kind[y*a.cv.w+x] = k
	}
}

// free is whether a cell is inside the scene and holds nothing, not even the belt's inside.
func (a *floorArt) free(x, y int) bool {
	return a.in(x, y) && a.kind[y*a.cv.w+x] == 0 && !a.band[y*a.cv.w+x]
}

// run is how many free cells there are from x on row y going right, up to n.
func (a *floorArt) run(x, y, n int) int {
	c := 0
	for c < n && a.free(x+c, y) {
		c++
	}
	return c
}

// text writes s from x, y and marks its cells.
func (a *floorArt) text(x, y int, s string, st gstyle, k byte) {
	end := a.cv.text(x, y, s, st, a.cv.w-1)
	for cx := x; cx < end; cx++ {
		if a.in(cx, y) {
			a.kind[y*a.cv.w+cx] = k
		}
	}
}

// drawBelt is the band: its inside, its two edges (6 columns apart), the turns as ╰─╯ and ╭─╮, and a tick
// in the centre every other row, green and stepping with the tread on a leg whose station has a working cube.
func (a *floorArt) drawBelt() {
	l, th := a.l, a.m.th
	for _, sg := range l.path.segs {
		for k := 0; k <= sg.n; k++ {
			cx, cy := sg.x+sg.dx*k, sg.y+sg.dy*k
			for d := -3; d <= 3; d++ {
				x, y := cx+d, cy
				if sg.dy == 0 {
					x, y = cx, cy+d
				}
				if a.in(x, y) {
					a.band[y*a.cv.w+x] = true
				}
			}
		}
	}
	cup := func(y, x0, x1 int, left, right rune) {
		a.put(x0, y, left, a.edge, kEdge)
		for x := x0 + 1; x < x1; x++ {
			a.put(x, y, '─', a.edge, kEdge)
		}
		a.put(x1, y, right, a.edge, kEdge)
	}
	for i := range 4 {
		start := l.path.segs[2*i]
		g := '╲'
		if i%2 == 1 {
			g = '╱'
		}
		// a leg from a turn has its inner edge start 4 cells in and its outer one 2 before it; both stop at the next turn's cups
		lo, ro := 4, -2
		if i == 0 {
			lo, ro = 0, 0
		}
		for k := lo; k <= l.hc+2; k++ {
			a.put(start.x+k-3, start.y+start.dy*k, g, a.edge, kEdge)
		}
		for k := ro; k <= l.hc-4; k++ {
			a.put(start.x+k+3, start.y+start.dy*k, g, a.edge, kEdge)
		}
		fx, fy := l.flatX[i], start.y+start.dy*l.hc
		switch {
		case i == 3:
			cup(fy-3, fx, fx+l.flat[i], '╭', '─')
			cup(fy+3, fx, fx+l.flat[i], '╭', '─')
		case i%2 == 0:
			cup(fy+3, fx, fx+l.flat[i], '╰', '╯')
			cup(fy-3, fx, fx+l.flat[i], '╰', '╯')
		default:
			cup(fy-3, fx, fx+l.flat[i], '╭', '╮')
			cup(fy+3, fx, fx+l.flat[i], '╭', '╮')
		}
		col, phase := a.tick, 0
		if l.work[floorStations[i]] {
			col, phase = th.Green, a.m.floor.belt
		}
		for k := 1; k < l.hc; k++ {
			if (k+phase)%2 == 0 {
				a.put(start.x+k, start.y+start.dy*k, '·', col, 0)
			}
		}
	}
}

// drawLattice is the faint diamond lattice of dots under everything: every 4 rows and 8 columns, offset on alternate rows.
// Without colour it would only be noise.
func (a *floorArt) drawLattice() {
	if a.m.th.NoColor {
		return
	}
	for y := 0; y < a.cv.h; y += 4 {
		for x := y / 4 % 2 * 4; x < a.cv.w; x += 8 {
			if a.free(x, y) {
				a.put(x, y, '·', a.lattice, 0)
			}
		}
	}
}

// machineRows is a station's drawing, w columns wide (at least 3), at most 5 rows.
func machineRows(place string, w int) []string {
	bar := func(l, r string) string { return l + strings.Repeat("─", w-2) + r }
	sp := func(n int) string { return strings.Repeat(" ", max(0, n)) }
	box := []string{bar("╭", "╮"), "│" + sp(w-2) + "│", "│" + sp(w-2) + "│", "│" + sp(w-2) + "│", bar("╰", "╯")}
	if w < 7 {
		return box
	}
	switch place {
	case "build": // a block with a slot
		return []string{bar("╭", "╮"), "│ ╭" + strings.Repeat("─", w-6) + "╮ │", "│ │" + sp(w-6) + "│ │", "│ ╰" + strings.Repeat("─", w-6) + "╯ │", bar("╰", "╯")}
	case "review": // a gantry the belt passes under
		leg := "  │" + sp(w-6) + "│  "
		return []string{bar("╭", "╮"), "╰─╮" + sp(w-6) + "╭─╯", leg, leg, "╭─╯" + sp(w-6) + "╰─╮"}
	case "merged": // a press: a plate above, two rods, a bed
		rod := " │" + sp(w-4) + "│ "
		return []string{bar("╰", "╯"), rod, rod, rod, bar("╭", "╮")}
	}
	// a dock pad
	return []string{" ╱" + strings.Repeat("─", w-4) + "╲ ", "╱" + sp(w-2) + "╲", "╲" + sp(w-2) + "╱", " ╲" + strings.Repeat("─", w-4) + "╱ "}
}

// drawStation stands station i on its flat run.
func (a *floorArt) drawStation(i int) {
	l := a.l
	place := floorStations[i]
	w := min(9, l.flat[i]+1)
	rows := machineRows(place, w)
	left, mid := l.flatX[i]+(l.flat[i]+1-w)/2, l.path.segs[2*i+1].y
	top := mid - 2
	for r, s := range rows {
		for c, ch := range []rune(s) {
			a.put(left+c, top+r, ch, a.mach, kMachine)
		}
	}
	a.machines = append(a.machines, rect{left, top, w, len(rows)})
}

// lamp is a station's lamp colour: green while it has a working cube, Yellow when one waits on the human, else faint.
func (a *floorArt) lamp(place string) color.Color {
	switch {
	case a.l.work[place]:
		return a.m.th.Green
	case a.l.waits[place]:
		return a.m.th.Yellow
	}
	return a.m.th.Faint
}

// drawIntake is the chute at the top-left start of the belt.
func (a *floorArt) drawIntake() {
	l := a.l
	rows := []string{"╭─────╮", "╰╮   ╭╯", " │   │ "}
	for r, s := range rows {
		for c, ch := range []rune(s) {
			a.put(l.xa-3+c, l.yt-3+r, ch, a.mach, kMachine)
		}
	}
	a.machines = append(a.machines, rect{l.xa - 3, l.yt - 3, 7, 3})
	a.sign("INTAKE", l.xa+5, l.yt-3, 1)
}

// drawPad is the BLOCKED pad below-left of leg 0: a bracketed row with a red lamp, and the spur up to the belt.
func (a *floorArt) drawPad() {
	l, th := a.l, a.m.th
	if l.padCap == 0 {
		return
	}
	n, row := 5*l.padCap, l.padTop+3
	a.put(0, row, '╰', a.edge, kPad)
	for x := 1; x < n; x++ {
		a.put(x, row, '─', a.edge, kPad)
	}
	a.put(n, row, '╯', a.edge, kPad)
	lamp := th.Faint
	if l.count["blocked"] > 0 {
		lamp = th.Red
	}
	a.put(n+2, row, '●', lamp, kPad)
	jx, jy := l.path.at(float64(l.spur))
	for i := 1; i <= l.spurM-2; i++ {
		if a.free(jx-i, jy+i) {
			a.put(jx-i, jy+i, '╱', a.edge, kPad)
		}
	}
	s, name := l.signText("blocked"), l.signName("blocked")
	if width(s) > max(n+3, width(name)) {
		s = name
	}
	a.sign(s, 1, row+1, 1)
}

// sign writes s with its first cell at x on row y, cut to the free room; the text is Text and bold.
func (a *floorArt) sign(s string, x, y, minRoom int) {
	if room := a.run(x, y, width(s)); room >= minRoom {
		s = trunc(s, room)
		a.text(x, y, s, gstyle{fg: a.m.th.Text, bold: true}, kSign)
		a.signs = append(a.signs, rect{x, y, width(s), 1})
	}
}

// signBeside puts a station's sign, then its lamp, near its machine, centred or shifted a little: below it first when the belt turns up there
// (a bottom turn), above it first otherwise; the other side when that has no room.
func (a *floorArt) signBeside(s string, r rect, below bool, lamp color.Color) {
	n := width(s) + 2
	up, down := make([]int, 0, 9), make([]int, 0, 9)
	for d := 1; d <= 9; d++ {
		up, down = append(up, -d), append(down, r.h-1+d)
	}
	order := [][]int{up, down}
	if below {
		order = [][]int{down, up}
	}
	for _, rows := range order {
		for _, d := range rows {
			y := r.y + d
			for _, dx := range []int{0, -3, 3, -6, 6, -n / 2, n / 2, -n} {
				if x := r.x + r.w/2 - n/2 + dx; a.run(x, y, n) == n && a.free(x-1, y) && a.free(x+n, y) {
					a.text(x, y, s, gstyle{fg: a.m.th.Text, bold: true}, kSign)
					a.put(x+n-1, y, '●', lamp, kSign)
					a.signs = append(a.signs, rect{x, y, n, 1})
					return
				}
			}
		}
	}
}

// signName is a sign's name and full count.
func (l *floorLayout) signName(place string) string {
	return fmt.Sprintf("%s %d", floorSigns[place], l.count[place])
}

// signText is a station's sign: its name, its full count, and the tasks that have no slot.
func (l *floorLayout) signText(place string) string {
	s := l.signName(place)
	if n := l.more[place]; n > 0 {
		s += fmt.Sprintf(" · %d more", n)
	}
	return s
}

// floorDrawn is a cube on screen: the cell of its middle, its task and place; cube is nil for a task that is leaving.
type floorDrawn struct {
	x, y  int
	t     data.FactoryTask
	place string
	cube  *floorCube
}

// cubeCells are the eight cells a cube draws in, around its middle (x, y).
func cubeCells(x, y int) [8][2]int {
	return [8][2]int{{x - 1, y - 1}, {x, y - 1}, {x - 2, y}, {x - 1, y}, {x, y}, {x + 1, y}, {x - 1, y + 1}, {x, y + 1}}
}

// drawCubes draws every cube at its place or on the way to it: grey at rest, green while a session works, yellow when it
// waits on the human, red when blocked or past its time, the muted merged colour once merged. A working cube shows the
// spinner, a waiting one ▲, where the middle row has ╲╱.
func (a *floorArt) drawCubes() []floorDrawn {
	l, th, f := a.l, a.m.th, &a.m.floor
	var ds []floorDrawn
	add := func(p point, t data.FactoryTask, c *floorCube) {
		x, y := l.pos(p)
		ds = append(ds, floorDrawn{x, y, t, floorPlace(t.Phase), c})
	}
	for i := range l.cubes {
		c := &l.cubes[i]
		add(f.shown(c.key(), c.at), c.t, c)
	}
	for _, g := range f.gone {
		add(lerp(g.from, g.to, float64(g.ago)/float64(floorMoveFor)), g.ft.t, nil)
	}
	for _, d := range ds {
		col := a.m.floorCubeLook(d.t, d.place)
		for i, rc := range []rune("╱╲│╲╱│╲╱") {
			c := cubeCells(d.x, d.y)[i]
			a.put(c[0], c[1], rc, col, kCube)
		}
		switch {
		case d.t.Phase == "motion" && d.t.Session == "working":
			a.put(d.x-1, d.y, []rune(spin(a.m.spinFrame()))[0], th.Green, kCube)
			a.put(d.x, d.y, ' ', th.Green, kCube)
		case waitsForYou(d.t):
			a.put(d.x-1, d.y, '▲', th.Yellow, kCube)
			a.put(d.x, d.y, ' ', th.Yellow, kCube)
		}
	}
	return ds
}

func (m *model) floorCubeLook(t data.FactoryTask, place string) color.Color {
	th := m.th
	switch {
	case t.Overtime:
		return th.Red
	case waitsForYou(t):
		return th.Yellow
	case place == "blocked":
		return th.Red
	case t.Phase == "motion" && t.Session == "working":
		return th.Green
	case t.Phase == "merged" || t.Phase == "settled":
		return m.mergedColor()
	}
	return th.Muted
}

// labelFrom writes pieces from x, y if at least 8 cells (of at most limit) are free there, cut with … to what fits.
func (a *floorArt) labelFrom(x, y, limit int, ps []lpiece) bool {
	room := a.run(x, y, limit)
	if room < 8 {
		return false
	}
	for _, p := range fitPieces(ps, room) {
		a.text(x, y, p.s, p.st, kLabel)
		x += width(p.s)
	}
	return true
}

// drawLabels writes one label beside each resting cube: to its right, a faint leader then the slug and the elapsed
// text; the BLOCKED cubes' under the pad, one a row, no wider than the pad. A cube with no room to its right (its station
// is there) has its label on the left of the band instead; one with room on neither side has none.
// The BLOCKED ones go first (blocked true) so that no sign takes their place.
func (a *floorArt) drawLabels(ds []floorDrawn, blocked bool) {
	l, m := a.l, a.m
	faint := gstyle{fg: m.th.Faint}
	for _, d := range ds {
		if d.cube == nil || m.floor.moves[d.cube.key()] != nil || (d.place == "blocked") != blocked {
			continue
		}
		c := *d.cube
		ps := m.labelPieces(c, l.prefix)
		if c.place == "blocked" {
			if wait := cmp.Or(c.t.WaitsOn, rawPhase(c.t.Phase)); wait != "" {
				ps = append(ps, lpiece{s: " · " + wait, st: faint, opt: true})
			}
		}
		if x := elapsedText(c.t, false); x != "-" {
			ps = append(ps, lpiece{s: "  " + x, st: faint, opt: true})
		}
		if c.place == "blocked" {
			j := slices.IndexFunc(l.cubes, func(o floorCube) bool { return o.key() == c.key() }) - slices.IndexFunc(l.cubes, func(o floorCube) bool { return o.place == "blocked" })
			a.labelFrom(1, l.padTop+5+j, 5*l.padCap+3, ps)
		} else if !a.labelFrom(d.x+4, d.y, a.cv.w, append([]lpiece{{s: "─ ", st: faint}}, ps...)) {
			a.labelLeft(d.x-4, d.y, append(ps, lpiece{s: " ─", st: faint}))
		}
	}
}

// labelLeft is the label of a cube with no room to its right (the machine of its station is there): it ends at x,
// on the left of the band, with its leader on its right.
func (a *floorArt) labelLeft(x, y int, ps []lpiece) {
	room := 0
	for room < x+1 && a.free(x-room, y) {
		room++
	}
	if room <= x { // stopped by something drawn: keep one clear column before it
		room--
	}
	if room < 8 {
		return
	}
	fit := fitPieces(ps[:len(ps)-1], room-2)
	total := 2
	for _, p := range fit {
		total += width(p.s)
	}
	x -= total - 1
	for _, p := range append(fit, ps[len(ps)-1]) {
		a.text(x, y, p.s, p.st, kLabel)
		x += width(p.s)
	}
}

// drawFloor draws the scene of l: the belt, the stations, the pad, the cubes, then the signs and labels, each
// only where nothing stands. hud is the plate of the upper right, hudX its column.
func (m *model) drawFloor(l *floorLayout, hud block, hudX int) *floorArt {
	a := newFloorArt(m, l)
	a.drawBelt()
	a.drawLattice()
	a.drawIntake()
	for i := range floorStations {
		a.drawStation(i)
	}
	a.drawPad()
	for i := range hud {
		for x := max(0, hudX-1); x < l.w; x++ {
			if a.in(x, i) {
				a.kind[i*l.w+x] = kPlate
			}
		}
	}
	ds := a.drawCubes()
	a.drawLabels(ds, true)
	for i, p := range floorStations {
		a.signBeside(l.signText(p), a.machines[i+1], i%2 == 0, a.lamp(p))
	}
	a.drawLabels(ds, false)
	return a
}

// rawPhase is the phase text of a task whose phase the floor does not know; empty for a known one.
func rawPhase(phase string) string {
	if slices.Contains(knownPhases, phase) {
		return ""
	}
	return phase
}

// lpiece is a run of a label's text; opt says the label may do without it.
type lpiece struct {
	s   string
	st  gstyle
	opt bool
}

// fitPieces cuts pieces to w cells: an optional one stays only whole, the first that does
// not fit is cut with an ellipsis.
func fitPieces(ps []lpiece, w int) []lpiece {
	var out []lpiece
	for _, p := range ps {
		pw := width(p.s)
		switch {
		case pw <= w:
			out = append(out, p)
			w -= pw
		case p.opt || w <= 0:
		default:
			p.s = trunc(p.s, w)
			return append(out, p)
		}
	}
	return out
}

func (m *model) labelPieces(c floorCube, prefix bool) []lpiece {
	th := m.th
	var ps []lpiece
	if prefix {
		ps = append(ps, lpiece{s: c.repo + "/", st: gstyle{fg: th.Faint}})
	}
	return append(ps, lpiece{s: c.t.Slug, st: gstyle{fg: th.Text}})
}
