package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"math"
	"slices"

	"isshoni/internal/data"
)

// The floor's drawing: thin lines in braille dots (the Hanami scene's canvas, one colour a
// cell) on a faint isometric grid, with the text of signs and labels over them. Floor
// spots are fpt (see floor.go); a dot is 1 across and 1 down, so a floor unit along either
// axis is 2 dots across and 1 down.

type floorArt struct {
	m    *model
	l    *floorLayout
	cv   *hCanvas
	cut  int    // dots left off each end of a line: a cube's corners come out rounded
	used []bool // cells that hold a line of the scene (not the grid) or text
	// grey is rest, green is work; every other colour is a signal
	grid, tick, edge, rest, green hrgb
}

func newFloorArt(m *model, l *floorLayout) *floorArt {
	th := m.th
	bg, muted := toRGB(th.Bg), toRGB(th.Muted)
	return &floorArt{
		m: m, l: l, cv: &hCanvas{w: l.w, h: l.h, maxY: 4 * l.h, cells: make([]hCell, l.w*l.h)}, used: make([]bool, l.w*l.h),
		grid: bg.mix(muted, 0.11), tick: bg.mix(muted, 0.55), edge: bg.mix(muted, 0.9), rest: muted, green: toRGB(th.Green),
	}
}

func (a *floorArt) dot(x, y float64, c hrgb) {
	a.cv.plot(x+0.5, y+0.5, c, 1)
	xi, yi := int(math.Floor(x+0.5)), int(math.Floor(y+0.5))
	if a.cv.layer > 0 && xi >= 0 && yi >= 0 && xi < 2*a.cv.w && yi < a.cv.maxY {
		a.used[(yi/4)*a.cv.w+xi/2] = true
	}
}

func (a *floorArt) line(x0, y0, x1, y1 float64, c hrgb) {
	n := int(math.Ceil(math.Max(math.Abs(x1-x0), math.Abs(y1-y0))))
	for i := 0; i <= n; i++ {
		if n > 2*a.cut+1 && (i < a.cut || i > n-a.cut) {
			continue
		}
		t := 0.0
		if n > 0 {
			t = float64(i) / float64(n)
		}
		a.dot(x0+(x1-x0)*t, y0+(y1-y0)*t, c)
	}
}

// seg is a line between two floor spots, each z dots above the floor.
func (a *floorArt) seg(p fpt, zp float64, q fpt, zq float64, c hrgb) {
	x0, y0 := a.l.xy(p, zp)
	x1, y1 := a.l.xy(q, zq)
	a.line(x0, y0, x1, y1, c)
}

// ring is a circle of floor radius r around c, z dots up: an ellipse on screen.
func (a *floorArt) ring(c fpt, r, z float64, col hrgb) {
	n := int(2*math.Pi*r*2.83/0.8) + 8
	var prev fpt
	for k := 0; k <= n; k++ {
		phi := 2 * math.Pi * float64(k) / float64(n)
		q := fpt{c.u + r*math.Cos(phi), c.v + r*math.Sin(phi)}
		if k > 0 {
			a.seg(prev, z, q, z, col)
		}
		prev = q
	}
}

// box is a block fu along the belt by fv across it and h tall, standing z dots up on the floor spot c: its top, its three near edges and the two near edges of its foot.
func (a *floorArt) box(c fpt, z, fu, fv, h float64, col hrgb) {
	at := func(su, sv float64) fpt { return fpt{c.u + su*fu/2, c.v + sv*fv/2} }
	ring := [4]fpt{at(-1, -1), at(1, -1), at(1, 1), at(-1, 1)}
	for i, q := range ring {
		a.seg(q, z+h, ring[(i+1)%4], z+h, col)
	}
	for _, q := range ring[:3] {
		a.seg(q, z, q, z+h, col)
	}
	a.seg(ring[0], z, ring[1], z, col)
	a.seg(ring[1], z, ring[2], z, col)
}

// footprint is the size along and across the belt of the stations that hide the belt under them.
func (l *floorLayout) footprint(place string) (fu, fv float64) {
	switch place {
	case "build":
		return 1.6 * l.e, 2.2 * l.e
	case "settled":
		return 1.7 * l.e, 1.7 * l.e
	}
	return 0, 0
}

// ---- the floor ---------------------------------------------------------------------------

func (a *floorArt) drawGrid() {
	l := a.l
	g := 3 * l.e
	umin, umax, vmin, vmax := math.Inf(1), math.Inf(-1), math.Inf(1), math.Inf(-1)
	for _, c := range [][2]float64{{0, 0}, {float64(2 * l.w), 0}, {0, float64(4 * l.h)}, {float64(2 * l.w), float64(4 * l.h)}} {
		u, v := (c[0]/2+c[1]-l.oy)/2, (c[0]/2-c[1]+l.oy)/2
		umin, umax, vmin, vmax = math.Min(umin, u), math.Max(umax, u), math.Min(vmin, v), math.Max(vmax, v)
	}
	for u := math.Ceil(umin/g) * g; u <= umax; u += g {
		a.seg(fpt{u, vmin}, 0, fpt{u, vmax}, 0, a.grid)
	}
	for v := math.Ceil(vmin/g) * g; v <= vmax; v += g {
		a.seg(fpt{umin, v}, 0, fpt{umax, v}, 0, a.grid)
	}
}

// drawBelt is the belt: two edges, a cross tick every e units, and a centre line in green where a
// working cube's flow runs (the stretch before its station). The ticks and the line step along
// that stretch with the tread.
func (a *floorArt) drawBelt() {
	l, p := a.l, a.l.path
	e, bw := l.e, l.bw()
	shift := e * float64(a.m.floor.belt%4) / 4
	live := func(s float64) bool {
		prev := 0.0
		for _, pl := range floorStations {
			if l.work[pl] && s >= prev && s <= l.stn[pl] {
				return true
			}
			prev = l.stn[pl]
		}
		return false
	}
	covered := func(q fpt) bool {
		for _, pl := range floorStations {
			if fu, fv := l.footprint(pl); fu > 0 {
				c, _ := p.at(l.stn[pl])
				if math.Abs(q.u-c.u) <= fu/2 && math.Abs(q.v-c.v) <= fv/2 {
					return true
				}
			}
		}
		return false
	}
	normal := func(t fpt) fpt { return fpt{-t.v * bw, t.u * bw} }
	side := func(q, t fpt, sgn float64) fpt {
		n := normal(t)
		return fpt{q.u + sgn*n.u, q.v + sgn*n.v}
	}
	for i := 1; i < len(p.pts); i++ {
		if covered(p.pts[i]) || covered(p.pts[i-1]) {
			continue
		}
		for _, sgn := range []float64{-1, 1} {
			a.seg(side(p.pts[i-1], p.tan[i], sgn), 0, side(p.pts[i], p.tan[i], sgn), 0, a.edge)
		}
	}
	for k := 0; ; k++ {
		s := float64(k) * 2.4 * e
		if live(s) {
			s += shift
		}
		if s > p.total() {
			break
		}
		q, t := p.at(s)
		if !covered(q) {
			a.seg(side(q, t, -1), 0, side(q, t, 1), 0, a.tick)
		}
	}
	a.cv.layer = 2
	for i, q := range p.pts {
		if s := p.s[i]; live(s) && !covered(q) && int(math.Floor((s-shift)/(0.7*e)))%2 == 0 {
			x, y := l.xy(q, 0)
			a.dot(x, y, a.green)
		}
	}
	a.cv.layer = 1
}

// drawSpur is the short branch from the belt up to the BLOCKED pad.
func (a *floorArt) drawSpur() {
	l, e := a.l, a.l.e
	c := l.fp(point{l.spur, 0})
	lo, hi := l.spurFar()-float64(floorSide-1)*l.pitch()-l.pitch()/2, l.spurFar()+l.pitch()/2
	for _, sgn := range []float64{-1, 1} {
		a.seg(fpt{c.u + sgn*l.bw(), c.v + l.bw()}, 0, fpt{c.u + sgn*l.bw(), c.v + lo}, 0, a.edge)
	}
	for d := l.bw() + 0.5*e; d < lo; d += 0.7 * e {
		a.seg(fpt{c.u - l.bw(), c.v + d}, 0, fpt{c.u + l.bw(), c.v + d}, 0, a.tick)
	}
	a.box(fpt{c.u, c.v + (lo+hi)/2}, 0, 1.5*e, hi-lo, 0.3*e, a.edge)
	red := a.rest
	if l.count["blocked"] > 0 {
		red = toRGB(a.m.th.Red)
	}
	a.lamp(fpt{c.u - 1.6*e, c.v + (lo+hi)/2}, l.count["blocked"] > 0, red)
}

// lamp is a status lamp on a small post: filled when lit.
func (a *floorArt) lamp(c fpt, lit bool, col hrgb) {
	e := a.l.e
	a.seg(c, 0, c, 1.5*e, a.edge)
	if !lit {
		col = a.edge
	}
	a.ring(c, 0.42*e, 1.5*e, col)
	if lit {
		a.ring(c, 0.28*e, 1.5*e, col)
		a.ring(c, 0.12*e, 1.5*e, col)
	}
}

// drawStation is one station on the belt: a block with a slot (BUILD), a gantry (REVIEW), a press
// (MERGE), a dock pad (SHIPPED); a working one has a green ring with arrowheads that turn with the
// belt, and a green lamp.
func (a *floorArt) drawStation(place string, working bool) {
	l, e := a.l, a.l.e
	c, _ := l.path.at(l.stn[place])
	col := a.edge
	switch place {
	case "build":
		fu, fv := l.footprint(place)
		a.box(c, 0, fu, fv, 1.6*e, col)
		a.box(c, 1.6*e, 0.9*e, 0.28*e, 0, col) // the slot
	case "review":
		for _, sgn := range []float64{-1, 1} {
			post := fpt{c.u, c.v + sgn*(l.bw()+0.35*e)}
			back := fpt{post.u + 0.5*e, post.v}
			a.seg(post, 0, post, 2.6*e, col)
			a.seg(post, 2.6*e, back, 2.6*e, col)
		}
		for _, u := range []float64{0, 0.5 * e} {
			a.seg(fpt{c.u + u, c.v - l.bw() - 0.35*e}, 2.6*e, fpt{c.u + u, c.v + l.bw() + 0.35*e}, 2.6*e, col)
		}
	case "merged":
		for _, sgn := range []float64{-1, 1} {
			p := fpt{c.u, c.v + sgn*1.0*e}
			a.seg(p, 0, p, 1.7*e, col)
		}
		a.box(c, 1.7*e, 1.6*e, 2.2*e, 0.35*e, col)
	case "settled":
		fu, fv := l.footprint(place)
		a.box(c, 0, fu, fv, 0.3*e, col)
	}
	if working {
		a.cv.layer = 2
		ring := a.green.mix(toRGB(a.m.th.Bg), 0.4)
		a.ring(c, 1.7*e, 0, ring)
		ph := float64(a.m.floor.belt) * 0.35
		for k := range 4 {
			a.arrowhead(c, 1.7*e, ph+float64(k)*math.Pi/2, a.rest)
		}
		a.cv.layer = 1
	}
	a.lamp(fpt{c.u, c.v - l.bw() - 1.3*e}, working, a.green)
}

// arrowhead is a chevron on the ring around c at angle phi, pointing the way round.
func (a *floorArt) arrowhead(c fpt, r, phi float64, col hrgb) {
	pt := func(p float64) (float64, float64) {
		return a.l.xy(fpt{c.u + r*math.Cos(p), c.v + r*math.Sin(p)}, 0)
	}
	x, y := pt(phi)
	x2, y2 := pt(phi + 0.05)
	tx, ty := x2-x, y2-y
	n := math.Hypot(tx, ty)
	if n == 0 {
		return
	}
	tx, ty = tx/n, ty/n
	k := a.l.e
	a.line(x, y, x-k*tx-0.6*k*ty, y-k*ty+0.6*k*tx, col)
	a.line(x, y, x-k*tx+0.6*k*ty, y-k*ty-0.6*k*tx, col)
}

// floorDrawn is a cube on screen: base dots, level in its pile, its task and place; cube is nil for a task that is leaving.
type floorDrawn struct {
	x, y  float64
	level int
	t     data.FactoryTask
	place string
	cube  *floorCube
}

// drawCubes draws the cubes, the farthest first, at their place or on the way to it, as wireframes: grey at rest,
// green with a lit top while a session works, yellow when it waits on the human, red when blocked or past its time,
// the muted merged colour once merged.
func (a *floorArt) drawCubes() []floorDrawn {
	l, f, e := a.l, &a.m.floor, a.l.e
	var ds []floorDrawn
	add := func(p point, level int, t data.FactoryTask, c *floorCube) {
		x, y := l.xy(l.fp(p), 0)
		ds = append(ds, floorDrawn{x, y, level, t, floorPlace(t.Phase), c})
	}
	for i := range l.cubes {
		c := &l.cubes[i]
		add(f.shown(c.key(), c.at), c.level, c.t, c)
	}
	for _, g := range f.gone {
		add(lerp(g.from, g.to, float64(g.ago)/float64(floorMoveFor)), 0, g.ft.t, nil)
	}
	slices.SortStableFunc(ds, func(p, q floorDrawn) int { return cmp.Or(cmp.Compare(p.y, q.y), cmp.Compare(p.level, q.level)) })
	a.cv.layer = 3
	for _, d := range ds {
		col, lit := a.m.floorCubeLook(d.t, d.place)
		c := toRGB(col)
		z := float64(d.level) * 2 * e
		// the cube's floor spot is read back from its dots: a box is drawn in floor coordinates
		q := fpt{(d.x/2 + (d.y - l.oy)) / 2, (d.x/2 - (d.y - l.oy)) / 2}
		a.box(q, z, e, e, 2*e, c)
		if lit {
			cx, cy := d.x, d.y-z-2*e
			for dy := -e; dy <= e; dy++ {
				for dx := -2 * e; dx <= 2*e; dx++ {
					if math.Abs(dx)/(2*e)+math.Abs(dy)/e <= 1 && math.Mod(math.Abs(dx+dy), 2) == 0 {
						a.dot(cx+dx, cy+dy, c.mix(toRGB(a.m.th.Bg), 0.35))
					}
				}
			}
		}
	}
	a.cv.layer = 1
	return ds
}

func (m *model) floorCubeLook(t data.FactoryTask, place string) (color.Color, bool) {
	th := m.th
	switch {
	case t.Overtime:
		return th.Red, false
	case t.WaitsOnHuman() || (t.Phase == "review" && !t.PRDraft):
		return th.Yellow, false
	case place == "blocked":
		return th.Red, false
	case t.Phase == "motion" && t.Session == "working":
		return th.Green, true
	case t.Phase == "merged" || t.Phase == "settled":
		return m.mergedColor(), false
	}
	return th.Muted, false
}

// canvas is the dots as braille glyphs, one colour a cell.
func (a *floorArt) canvas() *canvas {
	cv := newCanvas(a.cv.w, a.cv.h)
	for i, cell := range a.cv.cells {
		if cell.mask != 0 {
			cv.put(i%cv.w, i/cv.w, rune(0x2800+int(cell.mask)), color.RGBA{R: uint8(math.Round(cell.c.r)), G: uint8(math.Round(cell.c.g)), B: uint8(math.Round(cell.c.b)), A: 255}, false)
		}
	}
	return cv
}

// ---- text over the dots --------------------------------------------------------------------

// free is whether the n cells from x on row y are all empty (false outside the scene).
func (a *floorArt) free(x, y, n int) bool {
	if y < 0 || y >= a.cv.h || x < 0 || x+n > a.cv.w {
		return false
	}
	for i := range n {
		if a.used[y*a.cv.w+x+i] {
			return false
		}
	}
	return true
}

func (a *floorArt) claim(x, y, n int) {
	for i := range n {
		if y >= 0 && y < a.cv.h && x+i >= 0 && x+i < a.cv.w {
			a.used[y*a.cv.w+x+i] = true
		}
	}
}

// run is how many empty cells there are from x on row y, going right (dir 1) or left (-1), up to max.
func (a *floorArt) run(x, y, dir, max int) int {
	n := 0
	for n < max {
		cx := x + dir*n
		if cx < 0 || cx >= a.cv.w || y < 0 || y >= a.cv.h || a.used[y*a.cv.w+cx] {
			break
		}
		n++
	}
	return n
}

// sign writes a station's name and count above (x, top): where it is free, else the nearest free row above or below.
func (a *floorArt) sign(cv *canvas, s string, x, top float64) {
	st := gstyle{fg: a.m.th.Text, bold: true}
	n := width(s)
	x0, r0 := int(x/2)-n/2, int(top/4)-1
	x0 = max(0, min(x0, a.cv.w-n))
	for _, dy := range []int{0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6} {
		for _, dx := range []int{0, -n / 2, n / 2, -n, n} {
			if a.free(x0+dx, r0+dy, n) {
				cv.text(x0+dx, r0+dy, s, st, cv.w-1)
				a.claim(x0+dx, r0+dy, n)
				return
			}
		}
	}
	cv.text(x0, max(0, r0), s, st, cv.w-1)
}

// signText is a station's sign: its name, its full count, and the tasks that have no slot.
func (l *floorLayout) signText(place string) string {
	s := fmt.Sprintf("%s %d", floorSigns[place], l.count[place])
	if n := l.more[place]; n > 0 {
		s += fmt.Sprintf(" · %d more", n)
	}
	return s
}

// label writes a cube's label where there is room, on the side of the belt away from the next
// leg first (below), to the right of the cube or to its left; it is dropped before it overlaps
// anything.
func (a *floorArt) label(cv *canvas, d floorDrawn) {
	m, e := a.m, int(a.l.e)
	th := m.th
	faint := gstyle{fg: th.Faint}
	c := *d.cube
	ps := m.labelPieces(c, a.l.prefix)
	if c.place == "blocked" {
		if wait := cmp.Or(c.t.WaitsOn, rawPhase(c.t.Phase)); wait != "" {
			ps = append(ps, lpiece{s: " · " + wait, st: faint, opt: true})
		}
	}
	if x := elapsedText(c.t, false); x != "-" {
		ps = append(ps, lpiece{s: "  " + x, st: faint, opt: true})
	}
	if c.pile > 0 {
		ps = append(ps, lpiece{s: fmt.Sprintf("  +%d stacked", c.pile), st: faint, opt: true})
	}
	cx, cy := int(d.x/2), int((d.y-float64(e)-float64(d.level*2*e))/4)
	need := 0
	for _, p := range ps {
		if !p.opt {
			need += width(p.s)
		}
	}
	for _, dy := range []int{1, 2, 0, -1, -2, 3, -3} {
		row := cy + dy
		for _, off := range []int{1, 3, 5} { // beyond the cube, then beyond what stands around it
			x := cx + e + off
			if room := a.run(x, row, 1, a.cv.w); room >= need+2 {
				a.write(cv, x, row, fitPieces(append([]lpiece{{s: "─ ", st: faint}}, ps...), room))
				return
			}
			end := cx - e - off - 1
			if room := a.run(end, row, -1, a.cv.w); room >= need+2 {
				fit := fitPieces(append(slices.Clone(ps), lpiece{s: " ─", st: faint}), room)
				total := 0
				for _, p := range fit {
					total += width(p.s)
				}
				a.write(cv, end-total+1, row, fit)
				return
			}
		}
	}
}

func (a *floorArt) write(cv *canvas, x, y int, ps []lpiece) {
	for _, p := range ps {
		n := width(p.s)
		cv.text(x, y, p.s, p.st, cv.w-1)
		a.claim(x, y, n)
		x += n
	}
}

// drawFloor draws the scene of l into a canvas of cells: the dots, then the signs, the marks on the
// cubes and the labels. hud is the plate of the upper right, hudX its column.
func (m *model) drawFloor(l *floorLayout, hud block, hudX int) *canvas {
	th := m.th
	a := newFloorArt(m, l)
	if !th.NoColor { // the grid is a colour effect; without colour it would be noise
		a.drawGrid()
	}
	a.cv.layer = 1
	a.drawBelt()
	a.drawSpur()
	for _, p := range floorStations {
		a.drawStation(p, l.work[p])
	}
	drawn := a.drawCubes()
	cv := a.canvas()
	for i := range hud {
		a.claim(max(0, hudX-1), i, l.w)
	}
	for _, d := range drawn {
		cell, row := int(d.x/2), int((d.y-l.e-float64(d.level)*2*l.e)/4)
		switch {
		case d.t.Phase == "motion" && d.t.Session == "working":
			cv.put(cell, row, []rune(spin(m.spinFrame()))[0], th.Green, true)
		case d.t.WaitsOnHuman() || (d.t.Phase == "review" && !d.t.PRDraft):
			cv.put(cell, row, '▲', th.Yellow, true)
		}
	}
	for _, p := range floorStations {
		c, _ := l.path.at(l.stn[p])
		x, y := l.xy(c, 0)
		a.sign(cv, l.signText(p), x, y-2.8*l.e-l.e)
	}
	x, y := l.xy(l.fp(point{l.spur, l.spurFar() + l.pitch()/2}), 0)
	a.sign(cv, l.signText("blocked"), x, y-3*l.e-2)
	x, y = l.xy(fpt{}, 0)
	a.sign(cv, "INTAKE", x+2*l.e, y-2*l.e)
	for _, d := range drawn {
		if d.cube != nil && d.level == 0 && m.floor.moves[d.cube.key()] == nil {
			a.label(cv, d)
		}
	}
	return cv
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
