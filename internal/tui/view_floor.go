package tui

import (
	"image/color"
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

// floorBody is the Factory tab: the floor, then the notes under it.
func (m *model) floorBody(p paint, w, h int) block {
	th := m.th
	f := &m.floor
	notes := m.floorNotes(h)
	sh := h - len(notes)
	l := m.floorLayout(w, sh)
	hud := m.floorHUD(p, w)
	hudX := 0
	if len(hud) > 0 {
		hudX = w - width(hud[0]) - 1
	}
	cv := m.drawFloor(l, hud, hudX).cv

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
	case len(l.cubes) == 0:
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
	top := max(0, len(lines)-len(box)-1) // low in the scene: the belt runs across the middle
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
		body = m.meter(p, &agg, min(40, w/3))
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
