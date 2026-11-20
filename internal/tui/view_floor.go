package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"math"
	"slices"
	"strings"

	"github.com/charmbracelet/x/ansi"

	"isshoni/internal/data"
)

// ---- a canvas of cells -------------------------------------------------------------------

type gstyle struct {
	fg, bg color.Color
	bold   bool
}

func (a gstyle) same(b gstyle) bool {
	return a.bold == b.bold && key24(a.fg) == key24(b.fg) && key24(a.bg) == key24(b.bg)
}

type glyph struct {
	r    rune // 0 is blank
	st   gstyle
	wide bool // the right half of the wide rune before it
}

type canvas struct {
	w, h int
	g    []glyph
}

func newCanvas(w, h int) *canvas { return &canvas{w, h, make([]glyph, max(0, w*h))} }

func (c *canvas) put(x, y int, r rune, fg color.Color, bold bool) {
	c.putOn(x, y, r, gstyle{fg: fg, bold: bold})
}

// putOn draws r at x, y; a cell outside the canvas is ignored.
func (c *canvas) putOn(x, y int, r rune, st gstyle) {
	if x < 0 || y < 0 || x >= c.w || y >= c.h {
		return
	}
	c.g[y*c.w+x] = glyph{r: r, st: st}
}

// text draws s from x, y, stopping before column maxX+1, and returns the column after it.
func (c *canvas) text(x, y int, s string, st gstyle, maxX int) int {
	for _, r := range s {
		rw := width(string(r))
		if rw == 0 {
			continue
		}
		if x+rw-1 > maxX {
			break
		}
		c.putOn(x, y, r, st)
		if rw == 2 && x+1 < c.w && y >= 0 && y < c.h && x >= 0 {
			c.g[y*c.w+x+1] = glyph{wide: true, st: st}
		}
		x += rw
	}
	return x
}

func (c *canvas) lines(p paint) block {
	out := make(block, c.h)
	for y := range out {
		var sb strings.Builder
		for x := 0; x < c.w; {
			st := c.g[y*c.w+x].st
			var run []rune
			for x < c.w && c.g[y*c.w+x].st.same(st) {
				switch g := c.g[y*c.w+x]; {
				case g.wide:
				case g.r == 0:
					run = append(run, ' ')
				default:
					run = append(run, g.r)
				}
				x++
			}
			pp := p
			if st.bg != nil {
				pp = p.on(st.bg)
			}
			fg := st.fg
			if fg == nil {
				fg = p.th.Text
			}
			if st.bold {
				sb.WriteString(pp.b(string(run), fg))
			} else {
				sb.WriteString(pp.t(string(run), fg))
			}
		}
		out[y] = sb.String()
	}
	return out
}

// overlay puts the styled string over at column x of a styled line w cells wide.
func overlay(line string, x int, over string, w int) string {
	return ansi.Cut(line, 0, x) + over + ansi.Cut(line, x+width(over), w)
}

// ---- the scene ---------------------------------------------------------------------------

var beltTread = []rune("══╪═")

// floorBody is the Factory tab: the floor, then the notes under it.
func (m *model) floorBody(p paint, w, h int) block {
	th := m.th
	f := &m.floor
	notes := m.floorNotes(h)
	sh := h - len(notes)
	l := m.floorLayout(w, sh)
	moving := m.factoryWorking()
	cv := newCanvas(w, sh)

	hud := m.floorHUD(p, w)
	hudX := 0
	if len(hud) > 0 {
		hudX = w - width(hud[0]) - 1
	}

	m.drawBelt(cv, l, moving)
	m.drawStations(cv, l)
	m.drawLabels(cv, l, hudX, len(hud))
	m.drawCubes(cv, l)

	lines := cv.lines(p)
	for i, hl := range hud {
		if i < len(lines) {
			lines[i] = overlay(lines[i], hudX, hl, w)
		}
	}
	switch {
	case f.menu:
		m.overlayMenu(p, lines, w)
	case m.fac.doneAt.IsZero():
		overlayCentered(p, lines, w, p.t(spin(m.spinFrame())+" ", th.Purple)+p.t("reading the factory…", th.Muted))
	case len(l.cubes) == 0 && len(l.chips) == 0:
		why := "No repository here has a task worktree."
		if repo := m.floorRepo(); repo != "" {
			why = "No task in this repository."
		}
		overlayCentered(p, lines, w, p.b("No task on the floor", th.Text), p.t(why, th.Muted), p.t("A dispatcher starts leads with /factory:dispatch.", th.Faint))
	}

	for _, n := range notes {
		fg := th.Faint
		if n.err {
			fg = th.Red
		}
		lines = append(lines, fit(p.sp(1)+p.t(trunc(n.text, w-2), fg), w, p))
	}
	return lines
}

// overlayCentered draws a small plate in the middle of lines.
func overlayCentered(p paint, lines block, w int, content ...string) {
	bw := 0
	for _, c := range content {
		bw = max(bw, width(c))
	}
	bw = min(w-2, bw+4)
	box := p.boxed("", content, bw, len(content)+2, p.th.Border)
	top := max(0, (len(lines)-len(box))/2)
	for i, bl := range box {
		if top+i < len(lines) {
			lines[top+i] = overlay(lines[top+i], max(0, (w-bw)/2), bl, w)
		}
	}
}

// floorHUD is the sign plate of the upper right: the factory's name and the capacity meter.
func (m *model) floorHUD(p paint, w int) block {
	th := m.th
	want := m.floorRepo()
	title, agg := "all repositories", data.FactoryStatus{}
	known := false
	for _, r := range m.fac.repos {
		if r.st == nil || (want != "" && r.path != want) {
			continue
		}
		if want != "" {
			title = r.name
		}
		st := r.st
		agg.InMotion, agg.Blocked, agg.Cap, agg.Reserve = agg.InMotion+st.InMotion, agg.Blocked+st.Blocked, agg.Cap+st.Cap, agg.Reserve+st.Reserve
		known = true
	}
	if want != "" && !known {
		for _, r := range m.fac.repos {
			if r.path == want {
				title = r.name
			}
		}
	}
	body := p.t("no status yet", th.Faint)
	if known {
		body = m.meter(p, &agg, 40)
	}
	bw := min(w-2, max(width(body), width(title)+2)+4)
	if bw < 12 {
		return nil
	}
	label := " " + trunc(title, bw-6) + " "
	return block{
		fit(p.t("╭─", th.Border)+p.b(label, th.Pink)+p.t(strings.Repeat("─", max(0, bw-3-width(label)))+"╮", th.Border), bw, p),
		p.t("│", th.Border) + fit(p.sp(1)+body, bw-2, p) + p.t("│", th.Border),
		p.t("╰"+strings.Repeat("─", bw-2)+"╯", th.Border),
	}
}

func (m *model) overlayMenu(p paint, lines block, w int) {
	th := m.th
	f := &m.floor
	names := []string{"all"}
	for _, r := range m.fac.repos {
		names = append(names, r.name)
	}
	cur := min(f.menuCur, len(names)-1)
	room := max(1, len(lines)-4)
	top := max(0, min(cur-room+1, len(names)-room))
	var rows []string
	chosen := 0
	if i := slices.IndexFunc(m.fac.repos, func(r factoryRepo) bool { return r.path == m.floorRepo() }); i >= 0 {
		chosen = i + 1
	}
	bw := 0
	for i := top; i < min(len(names), top+room); i++ {
		mark, fg, tick := "  ", th.Text, "  "
		if i == cur {
			mark, fg = "› ", th.Purple
		}
		if i == chosen {
			tick = "✓ "
		}
		rows = append(rows, p.on(th.Surface).t(mark, fg)+p.on(th.Surface).t(tick, th.Green)+p.on(th.Surface).t(trunc(names[i], max(4, w-12)), fg))
		bw = max(bw, width(rows[len(rows)-1]))
	}
	hint := p.on(th.Surface).t("↑↓ move · enter choose · esc close", th.Faint)
	rows = append(rows, hint)
	bw = min(w-2, max(bw, width(hint))+4)
	for i, bl := range p.boxed("repository", rows, bw, len(rows)+2, th.Pink) {
		if i < len(lines) {
			lines[i] = overlay(lines[i], 1, bl, w)
		}
	}
}

// drawBelt draws the tread from the intake to the end of the last slot, a row under the
// cubes' feet, one cell down for every floorStepX across. It moves with the belt's phase
// only while moving.
func (m *model) drawBelt(cv *canvas, l *floorLayout, moving bool) {
	th := m.th
	last := l.x0 + floorStepX*(l.slots-1) + 5
	tread := th.Faint
	if moving {
		tread = th.Teal
	}
	for x := l.x0; x <= last; x++ {
		y := 3 + (x-l.x0)*floorStepY/floorStepX
		cv.put(x, y, beltTread[((x-m.floor.belt)%len(beltTread)+len(beltTread))%len(beltTread)], tread, false)
		cv.put(x, y+1, '▔', th.Faint, false)
	}
	// the intake chute
	for i, row := range []string{"╲▁▁▁▁╱", " ╲  ╱ ", "  ╲╱  "} {
		for j, r := range []rune(row) {
			if r != ' ' {
				cv.put(l.x0+j, i, r, th.Muted, false)
			}
		}
	}
}

// drawStations draws a post and a sign plate at the start of every station, and the siding's lamp and bay.
func (m *model) drawStations(cv *canvas, l *floorLayout) {
	th := m.th
	plate := gstyle{fg: th.Text, bg: th.Raised, bold: true}
	for _, p := range floorStations {
		at := l.belt(l.first[p])
		x, y := int(at.x), int(at.y)
		cv.put(x-1, y-1, '╔', th.Muted, false)
		cv.put(x-1, y, '║', th.Muted, false)
		cv.put(x-1, y+1, '║', th.Muted, false)
		cv.put(x-1, y+2, '╨', th.Muted, false)
		cv.text(x, y-1, fmt.Sprintf(" %s %d ", floorSigns[p], l.count[p]), plate, cv.w)
	}

	side := l.side(0)
	sign := fmt.Sprintf("BLOCKED %d", l.count["blocked"])
	sx := max(0, int(side.x)+floorCubeW-width(sign)-2)
	cv.put(sx, int(side.y)-1, '●', th.Red, true)
	cv.text(sx+2, int(side.y)-1, sign, gstyle{fg: th.Text, bold: true}, cv.w)
	for j := 0; j < l.sideN; j++ {
		at := l.side(j)
		for dx := 0; dx < floorCubeW; dx++ {
			cv.put(int(at.x)+dx, int(at.y)+floorCubeH, '╌', th.Faint, false)
		}
	}
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

// drawLabels writes each static cube's label: on the belt to the right of the cube, on the
// siding to its left. hudX is the left of the sign plate, hudH its height.
func (m *model) drawLabels(cv *canvas, l *floorLayout, hudX, hudH int) {
	th := m.th
	faint := gstyle{fg: th.Faint}
	intakeMax := cv.w - 1
	if hudX > 0 {
		intakeMax = hudX - 2
	}
	cv.text(l.x0+floorStepX, 0, "INTAKE", gstyle{fg: th.Muted}, intakeMax)
	for _, c := range l.cubes {
		if m.floor.moves[c.key()] != nil {
			continue
		}
		x, y := int(c.at.x), int(c.at.y)
		ps := m.labelPieces(c, l.prefix)
		if c.place == "blocked" {
			if wait := cmp.Or(c.t.WaitsOn, rawPhase(c.t.Phase)); wait != "" {
				ps = append(ps, lpiece{s: " · " + wait, st: faint})
			}
			ps = fitPieces(ps, x-2)
			total := 0
			for _, p := range ps {
				total += width(p.s)
			}
			for sx := x - 1 - total; len(ps) > 0; ps = ps[1:] {
				sx = cv.text(sx, y+floorCubeH-1, ps[0].s, ps[0].st, cv.w)
			}
			continue
		}
		maxX := cv.w - 1
		if y < hudH && hudX > 0 {
			maxX = hudX - 2
		}
		avail := maxX - (x + floorCubeW + 1) + 1
		ps = append([]lpiece{{s: "─ ", st: faint}}, ps...)
		if e := elapsedText(c.t, false); e != "-" {
			ps = append(ps, lpiece{s: "  " + e, st: faint, opt: true})
		}
		sx := x + floorCubeW + 1
		for _, p := range fitPieces(ps, avail) {
			sx = cv.text(sx, y, p.s, p.st, maxX)
		}
	}
	for _, ch := range l.chips {
		x, y := int(ch.at.x), int(ch.at.y)
		text := fmt.Sprintf("%d more", ch.n)
		if ch.place == "blocked" {
			cv.text(x-2-width(text), y+floorCubeH-1, text, faint, cv.w)
		} else {
			cv.text(x+floorCubeW+1, y, "─ "+text, faint, cv.w)
		}
	}
}

// rawPhase is the phase text of a task whose phase the floor does not know; empty for a known one.
func rawPhase(phase string) string {
	if slices.Contains(knownPhases, phase) {
		return ""
	}
	return phase
}

// drawCubes draws the chips and the cubes, the farthest first so that a nearer one covers it.
func (m *model) drawCubes(cv *canvas, l *floorLayout) {
	th := m.th
	f := &m.floor
	type draw struct {
		x, y int
		t    *data.FactoryTask
		chip int
	}
	var ds []draw
	at := func(p point) (int, int) { return int(math.Round(p.x)), int(math.Round(p.y)) }
	for _, c := range l.cubes {
		x, y := at(f.shown(c.key(), c.at))
		ds = append(ds, draw{x: x, y: y, t: &c.t})
	}
	for _, g := range f.gone {
		x, y := at(lerp(g.from, g.to, float64(g.ago)/float64(floorMoveFor)))
		ds = append(ds, draw{x: x, y: y, t: &g.ft.t})
	}
	for _, ch := range l.chips {
		x, y := at(ch.at)
		ds = append(ds, draw{x: x, y: y, chip: ch.n})
	}
	slices.SortStableFunc(ds, func(a, b draw) int { return a.y - b.y })
	for _, d := range ds {
		if d.t == nil {
			drawChip(cv, d.x, d.y, d.chip, th)
			continue
		}
		m.drawCube(cv, d.x, d.y, *d.t)
	}
}

func drawChip(cv *canvas, x, y, n int, th *Theme) {
	num := trunc(fmt.Sprintf("+%d", n), 4)
	pad := 4 - len(num)
	row := "┆" + strings.Repeat(" ", pad/2) + num + strings.Repeat(" ", pad-pad/2) + "┆"
	for i, s := range []string{" ╭┄┄╮ ", row, " ╰┄┄╯ "} {
		for j, r := range []rune(s) {
			if r != ' ' {
				cv.put(x+j, y+i, r, th.Muted, i == 1)
			}
		}
	}
}

// drawCube draws a task as a cube in three tones: top lightest, left face mid, right face darkest.
// Heavy side edges mark overtime; a spinner on the left face a working session; ▲ a task that waits on the human.
func (m *model) drawCube(cv *canvas, x, y int, t data.FactoryTask) {
	th := m.th
	_, _, col := m.phaseLook(t, false)
	top, mid, dark := mix(col, th.Text, 0.35), col, mix(col, th.Bg, 0.45)
	edge, el, er := dark, '▐', '▌'
	if t.Overtime {
		edge, el, er = th.Red, '█', '█'
	}
	cv.put(x+1, y, '▗', top, false)
	cv.put(x+2, y, '▄', top, false)
	cv.put(x+3, y, '▄', top, false)
	cv.put(x+4, y, '▖', top, false)
	cv.put(x, y+1, el, edge, false)
	cv.put(x+1, y+1, '▓', mid, false)
	cv.put(x+2, y+1, '▓', mid, false)
	cv.put(x+3, y+1, '▒', dark, false)
	cv.put(x+4, y+1, '▒', dark, false)
	cv.put(x+5, y+1, er, edge, false)
	cv.put(x+1, y+2, '▝', mid, false)
	cv.put(x+2, y+2, '▀', mid, false)
	cv.put(x+3, y+2, '▀', dark, false)
	cv.put(x+4, y+2, '▘', dark, false)
	switch {
	case t.Phase == "motion" && t.Session == "working":
		cv.put(x+2, y+1, []rune(spin(m.spinFrame()))[0], th.Text, true)
	case t.WaitsOnHuman() || (t.Phase == "review" && !t.PRDraft):
		cv.put(x+2, y+1, '▲', th.Yellow, true)
	}
}
