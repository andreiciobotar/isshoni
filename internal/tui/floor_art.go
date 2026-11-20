package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"slices"

	"isshoni/internal/data"
)

// The floor's drawing: a top view in horizontal and vertical strokes and filled cells, nothing at an angle.
// Lane edges in Border; colour is spent at the junctions, the head tiles and the crates. Layout (floor.go) decides
// where things stand; this file paints them.

type rect struct{ x, y, w, h int }

// lpiece is a run of a crate's text; opt says the crate may do without it.
type lpiece struct {
	s          string
	faint, opt bool
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

// rawPhase is the phase text of a task whose phase the floor does not know; empty for a known one.
func rawPhase(phase string) string {
	if slices.Contains(knownPhases, phase) {
		return ""
	}
	return phase
}

// crateParts is the text of a crate after its mark: the slug (with the repository before it under the prefix rule, when the
// slug keeps 6 cells), what a blocked task waits on, then the elapsed text; all cut to the widest crate, the last first.
func (l *floorLayout) crateParts(ft floorTask) []lpiece {
	room := l.capW - 4
	var ps []lpiece
	if l.prefix && width(ft.repo)+1+6 <= room {
		ps = append(ps, lpiece{s: ft.repo + "/", faint: true})
	}
	ps = append(ps, lpiece{s: ft.t.Slug})
	if floorPlace(ft.t.Phase) == "blocked" {
		if wait := cmp.Or(ft.t.WaitsOn, rawPhase(ft.t.Phase)); wait != "" {
			ps = append(ps, lpiece{s: " · " + wait, faint: true, opt: true})
		}
	}
	if x := elapsedText(ft.t, false); x != "-" {
		ps = append(ps, lpiece{s: "  " + x, faint: true, opt: true})
	}
	return fitPieces(ps, room)
}

// crateWidth is the cells of a crate: its two edges, its mark and a space, and its text.
func (l *floorLayout) crateWidth(ft floorTask) int {
	w := 4
	for _, p := range l.crateParts(ft) {
		w += width(p.s)
	}
	return w
}

// crateMark is the first cell of a crate: ▲ when its task waits on the human, ! when it is past its time, the spinner
// when a session works, else a space.
func (m *model) crateMark(t data.FactoryTask) (rune, color.Color) {
	th := m.th
	switch {
	case waitsForYou(t):
		return '▲', th.Yellow
	case t.Overtime:
		return '!', th.Red
	case t.Session == "working":
		return []rune(spin(m.spinFrame()))[0], th.Green
	}
	return ' ', th.Text
}

// floorLaneColor is the colour of a lane's junction and head tile: what phaseLook gives its phase.
func (m *model) floorLaneColor(place string) color.Color {
	phase := map[string]string{"blocked": "blocked", "build": "motion", "review": "review", "merged": "merged", "settled": "settled"}[place]
	_, _, c := m.phaseLook(data.FactoryTask{Phase: phase}, false)
	return c
}

type floorArt struct {
	m  *model
	l  *floorLayout
	cv *canvas
}

// tile is how a lane's tile looks: its fill, the colour of its text and its glyph. Bright when the lane holds work or a task that
// waits on the human; else the same hue mixed toward the background, and ▓ where █ would be (the goldens have no colour).
func (a *floorArt) tile(place string) (bg, fg color.Color, g rune) {
	th := a.m.th
	c := a.m.floorLaneColor(place)
	if a.l.active(place) {
		return c, th.Bg, '█'
	}
	return mix(c, th.Bg, 0.7), th.Text, '▓'
}

func (a *floorArt) put(x, y int, r rune, fg color.Color) {
	a.cv.putOn(x, y, r, gstyle{fg: fg})
}

// fill paints the cells of r with g, in the colour bg on bg: a solid block.
func (a *floorArt) fill(r rect, g rune, bg color.Color) {
	for y := r.y; y < r.y+r.h; y++ {
		for x := r.x; x < r.x+r.w; x++ {
			a.cv.putOn(x, y, g, gstyle{fg: bg, bg: bg})
		}
	}
}

// drawEdges is the lane edges and the walls at the ends. The edges of the siding are dashed, to read as off the line.
func (a *floorArt) drawEdges() {
	l, th := a.l, a.m.th
	rows := []int{0}
	for k := 1; k < 5; k++ {
		rows = append(rows, l.lanes[k].top-1)
	}
	rows = append(rows, l.lanes[4].top+l.lanes[4].inner)
	for i, y := range rows {
		first, last, bar := '├', '┤', '─'
		switch i {
		case 0:
			first, last = '┌', '┐'
		case len(rows) - 1:
			first, last = '└', '┘'
		}
		if i <= 1 {
			bar = '╌'
		}
		for x := range l.w {
			g := bar
			switch x {
			case 0:
				g = first
			case l.w - 1:
				g = last
			}
			a.put(x, y, g, th.Border)
		}
	}
	for k, ln := range l.lanes {
		for y := ln.top; y < ln.top+ln.inner; y++ {
			if k != 1 { // BUILD's left end is open: the intake
				a.put(0, y, '│', th.Border)
			}
			a.put(l.w-1, y, '│', th.Border)
		}
	}
	a.put(0, l.lanes[1].cy(), '›', a.m.floorLaneColor("build"))
}

// drawHeads is the head tile of each lane, with its name and count; what has no room for a crate is counted under the
// name (after it when the lane has one inner row).
func (a *floorArt) drawHeads() {
	l := a.l
	for k, place := range floorLanes {
		r, ln := l.heads[k], l.lanes[k]
		bg, fg, g := a.tile(place)
		a.fill(rect{r.x, r.y, 1, r.h}, g, bg)
		a.fill(rect{r.x + r.w - 1, r.y, 1, r.h}, g, bg)
		for y := r.y; y < r.y+r.h; y++ {
			for x := r.x + 1; x < r.x+r.w-1; x++ {
				a.cv.putOn(x, y, ' ', gstyle{fg: fg, bg: bg})
			}
		}
		st := gstyle{fg: fg, bg: bg, bold: true}
		name, more, room := l.signName(place), "", r.w-4
		if n := l.more[place]; n > 0 {
			more = fmt.Sprintf("· %d more", n)
		}
		if ln.inner >= 2 {
			a.cv.text(r.x+2, ln.cy(), trunc(name, room), st, r.x+r.w-2)
			a.cv.text(r.x+2, ln.cy()+1, trunc(more, room), st, r.x+r.w-2)
		} else {
			if more != "" {
				name += " " + more
			}
			a.cv.text(r.x+2, ln.cy(), trunc(name, room), st, r.x+r.w-2)
		}
	}
}

// drawJunctions is the solid blocks at the ends of the lanes, in the colour of the lane they lead into.
func (a *floorArt) drawJunctions() {
	for _, j := range a.l.junctions {
		bg, _, g := a.tile(j.place)
		a.fill(j.rect, g, bg)
	}
}

// drawMarks is the flow marks along each lane's centre row, ›  or ‹ every 4 cells: in the lane's colour and stepping with the
// belt on a lane with a working session, faint and standing still on the others. BLOCKED has none. A mark next to a crate is left out.
func (a *floorArt) drawMarks() {
	l, th := a.l, a.m.th
	for k := 1; k < 5; k++ {
		place, ln := floorLanes[k], l.lanes[k]
		col, phase := th.Faint, 0
		if l.work[place] {
			col, phase = a.m.floorLaneColor(place), a.m.floor.belt
		}
		g, step := '‹', phase
		if k%2 == 1 {
			g, step = '›', -phase
		}
		x0 := l.heads[k].x + l.heads[k].w
		for x := x0; x < l.w-floorGate; x++ {
			beside := slices.ContainsFunc(l.crates, func(c floorCrate) bool {
				return c.rect.y <= ln.cy() && ln.cy() < c.rect.y+c.rect.h && x >= c.rect.x-1 && x <= c.rect.x+c.rect.w
			})
			if ((x-x0+step)%4+4)%4 == 0 && !beside {
				a.put(x, ln.cy(), g, col)
			}
		}
	}
}

// drawCrate paints a crate of the task t at r: ▐ and ▌ at its sides, a tile tinted with its phase colour between them, and on the
// middle row its mark, a space and the text. A crate of one row has only the middle one.
func (a *floorArt) drawCrate(r rect, t data.FactoryTask, ps []lpiece) {
	th := a.m.th
	_, _, c := a.m.phaseLook(t, false)
	tint := mix(c, th.Bg, 0.75)
	for y := r.y; y < r.y+r.h; y++ {
		a.cv.putOn(r.x, y, '▐', gstyle{fg: tint})
		a.cv.putOn(r.x+r.w-1, y, '▌', gstyle{fg: tint})
		for x := r.x + 1; x < r.x+r.w-1; x++ {
			a.cv.putOn(x, y, ' ', gstyle{fg: th.Text, bg: tint})
		}
	}
	mid := r.y + r.h/2
	mark, mc := a.m.crateMark(t)
	a.cv.putOn(r.x+1, mid, mark, gstyle{fg: mc, bg: tint, bold: true})
	x := r.x + 3
	for _, p := range ps {
		fg := th.Text
		if p.faint {
			fg = th.Faint
		}
		x = a.cv.text(x, mid, p.s, gstyle{fg: fg, bg: tint}, r.x+r.w-2)
	}
}

// drawCrates paints the crates at rest, then those on the way: a crate in transit is one row, its text short, at the cell of
// the conveyor it has reached. They go over whatever they pass.
func (a *floorArt) drawCrates() {
	l, f := a.l, &a.m.floor
	var moving []floorTask
	var at []float64
	for _, c := range l.crates {
		if f.moves[c.key()] != nil {
			moving, at = append(moving, c.floorTask), append(at, f.shown(c.key(), c.at))
			continue
		}
		a.drawCrate(c.rect, c.t, l.crateParts(c.floorTask))
	}
	for _, g := range f.gone {
		moving, at = append(moving, g.ft), append(at, lerp(g.from, g.to, float64(g.ago)/float64(floorMoveFor)))
	}
	for i, ft := range moving {
		x, y := l.path.at(at[i])
		ps := []lpiece{{s: trunc(ft.t.Slug, 8)}}
		cw := 4 + width(ps[0].s)
		a.drawCrate(rect{min(max(0, x-cw/2), l.w-cw), y, cw, 1}, ft.t, ps)
	}
}

// signName is a lane's name and full count.
func (l *floorLayout) signName(place string) string {
	return fmt.Sprintf("%s %d", floorSigns[place], l.count[place])
}

// drawFloor draws the scene of l: the edges, the head tiles, the flow marks, the junctions, then the crates.
func (m *model) drawFloor(l *floorLayout) *floorArt {
	a := &floorArt{m: m, l: l, cv: newCanvas(l.w, l.h)}
	a.drawEdges()
	a.drawHeads()
	a.drawMarks()
	a.drawJunctions()
	a.drawCrates()
	return a
}
