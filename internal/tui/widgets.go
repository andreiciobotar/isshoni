package tui

import (
	"cmp"
	"image/color"
	"math"
	"slices"
	"strings"

	"charm.land/bubbles/v2/spinner"
)

var (
	partials   = []rune{' ', '▏', '▎', '▍', '▌', '▋', '▊', '▉'}
	plainFills = []string{"█", "▓", "▒", "░", "#", "%", "+", "="}
	spinFrames = spinner.MiniDot.Frames
)

// trackBg is the empty part of a bar: a step lighter than whatever it sits on.
func (p paint) trackBg() color.Color {
	if key24(p.bg) == key24(p.th.Raised) {
		return p.th.Border
	}
	return p.th.Raised
}

// bar draws frac (0..1) of width w in fg; the rest is the track. Sub-cell precision
// via eighth blocks. gradient fills with the signature blue->purple->pink.
func (p paint) bar(w int, frac float64, fg color.Color, gradient bool) string {
	if w <= 0 {
		return ""
	}
	frac = math.Max(0, math.Min(1, frac))
	if p.th.NoColor {
		n := int(math.Round(frac * float64(w)))
		return strings.Repeat("█", n) + strings.Repeat("░", w-n)
	}
	track := p.on(p.trackBg())
	cells := frac * float64(w)
	full := int(cells)
	part := int((cells - float64(full)) * 8)
	colorAt := func(i int) color.Color {
		if !gradient {
			return fg
		}
		t := 0.0
		if w > 1 {
			t = float64(i) / float64(w-1)
		}
		return p.th.gradientAt(t)
	}
	var sb strings.Builder
	for i := 0; i < full && i < w; {
		j := i + 1 // one render per run of one color
		for j < full && j < w && key24(colorAt(j)) == key24(colorAt(i)) {
			j++
		}
		sb.WriteString(track.t(strings.Repeat("█", j-i), colorAt(i)))
		i = j
	}
	used := full
	if part > 0 && full < w {
		sb.WriteString(track.t(string(partials[part]), colorAt(full)))
		used++
	}
	sb.WriteString(track.sp(w - used))
	return sb.String()
}

// swatch is a legend entry's mark for segment i of a stack: the segment's own glyph
// when there are no colors to tell them apart.
func (p paint) swatch(i int, fg color.Color) string {
	if p.th.NoColor {
		return plainFills[i%len(plainFills)]
	}
	return p.t("■", fg)
}

type seg struct {
	val   float64
	color color.Color
}

// stack draws segments proportionally across w cells (largest remainder, so the cell
// counts always sum to w when any value is positive).
func (p paint) stack(w int, segs []seg) string {
	if w <= 0 {
		return ""
	}
	total := 0.0
	for _, s := range segs {
		total += math.Max(0, s.val)
	}
	track := p.on(p.trackBg())
	if total <= 0 {
		if p.th.NoColor {
			return strings.Repeat("░", w)
		}
		return track.sp(w)
	}
	counts := make([]int, len(segs))
	type rem struct {
		i int
		r float64
	}
	var rems []rem
	used := 0
	for i, s := range segs {
		exact := math.Max(0, s.val) / total * float64(w)
		counts[i] = int(exact)
		used += counts[i]
		rems = append(rems, rem{i, exact - float64(counts[i])})
	}
	for used < w {
		best := 0
		for j := range rems {
			if rems[j].r > rems[best].r {
				best = j
			}
		}
		counts[rems[best].i]++
		rems[best].r = -1
		used++
	}
	var sb strings.Builder
	for i, s := range segs {
		if counts[i] == 0 {
			continue
		}
		if p.th.NoColor {
			sb.WriteString(strings.Repeat(plainFills[i%len(plainFills)], counts[i]))
		} else {
			sb.WriteString(p.t(strings.Repeat("█", counts[i]), s.color))
		}
	}
	return sb.String()
}

// pill is a small colored tag; plain rendering uses [brackets].
func (p paint) pill(text string, fg color.Color) string {
	if p.th.NoColor {
		return "[" + text + "]"
	}
	bg := mix(p.bg, fg, 0.2)
	q := p.on(bg)
	return q.b(" "+text+" ", fg)
}

func pillWidth(text string) int { return width(text) + 2 }

// dot is a status dot; phase pulses it between bright and dim.
func (p paint) dot(fg color.Color, bright bool) string {
	if p.th.NoColor {
		if bright {
			return "●"
		}
		return "○"
	}
	if bright {
		return p.b("●", fg)
	}
	return p.t("●", mix(p.bg, fg, 0.45))
}

// spin returns the spinner glyph for a frame counter.
func spin(frame int) string { return spinFrames[frame%len(spinFrames)] }

// block is a rectangle of already-painted lines.
type block []string

// joinCols places blocks side by side with a painted gap; every output line is
// exactly the sum of widths plus gaps, h lines tall.
func joinCols(p paint, h, gap int, widths []int, cols ...block) block {
	out := make(block, h)
	for y := 0; y < h; y++ {
		var sb strings.Builder
		for i, c := range cols {
			if i > 0 {
				sb.WriteString(p.sp(gap))
			}
			if y < len(c) {
				sb.WriteString(fit(c[y], widths[i], p))
			} else {
				sb.WriteString(p.sp(widths[i]))
			}
		}
		out[y] = sb.String()
	}
	return out
}

// boxed wraps lines in a rounded border with an optional title; inner lines render on
// the surface color. Result is exactly w x h.
func (p paint) boxed(title string, lines []string, w, h int, titleFg color.Color) block {
	if w < 4 || h < 2 {
		return make(block, 0)
	}
	in := p.on(p.th.Surface)
	bd := p.th.Border
	out := make(block, 0, h)
	// top
	label := ""
	if title != "" {
		label = " " + trunc(title, w-6) + " "
	}
	rest := w - 3 - width(label)
	if rest < 0 {
		rest = 0
	}
	top := p.t("╭─", bd) + p.b(label, titleFg) + p.t(strings.Repeat("─", rest)+"╮", bd)
	out = append(out, fit(top, w, p))
	for i := 0; i < h-2; i++ {
		ln := ""
		if i < len(lines) {
			ln = lines[i]
		}
		out = append(out, p.t("│", bd)+fit(in.sp(1)+ln, w-2, in)+p.t("│", bd))
	}
	out = append(out, p.t("╰"+strings.Repeat("─", w-2)+"╯", bd))
	return out
}

// center places a block in the middle of a w x h canvas.
func (p paint) center(b block, w, h int) block {
	out := make(block, h)
	bw := 0
	for _, l := range b {
		bw = max(bw, width(l))
	}
	top := max(0, (h-len(b))/2)
	left := max(0, (w-bw)/2)
	for y := 0; y < h; y++ {
		i := y - top
		if i >= 0 && i < len(b) {
			out[y] = fit(p.sp(left)+b[i], w, p)
		} else {
			out[y] = p.sp(w)
		}
	}
	return out
}

// ---- column chart -----------------------------------------------------------------

const chartRows = 5 // rows tall; each holds eight levels

var chartBlocks = []rune("▁▂▃▄▅▆▇█")

// chartOpts describes a run chart: one column per value, oldest first.
type chartOpts struct {
	vals      []float64
	median    float64
	hasMedian bool
	label     func(float64) string    // y labels
	pick      func(i int) color.Color // the colour of column i, before the older ones are dimmed
	star      bool                    // mark the newest column with ★
	grow      float64                 // 0..1, the share of its height each column shows
}

// chartOut is what the caller needs to draw an axis under the columns.
type chartOut struct {
	lines  block // chartRows lines of exactly the width asked for
	first  int   // index of the first value drawn: the newest ones that fit are kept
	gutter int   // cells left of the first column
	stride int   // cells from one column to the next
	step   int   // line chart: dots from one point to the next
	plot   int   // line chart: cells of the plot, between the gutter and the end labels
}

// chart draws o in w cells on p's background. The y labels give the largest drawn value
// and the median; the median is a dotted rule across the empty cells of its row; the
// newest column is bright and the older ones dimmed.
func (p paint) chart(o chartOpts, w int) chartOut {
	th := p.th
	n := len(o.vals)
	// the gutter holds the widest label any value, the median or the shown maximum can have
	gutter := 1
	for _, v := range o.vals {
		gutter = max(gutter, width(o.label(v))+1)
	}
	if o.hasMedian {
		gutter = max(gutter, width(o.label(o.median))+1)
	}
	pw := max(1, w-gutter)
	// columns are two cells wide with a gap when all fit that way, else one wide with a gap
	colW, gap := 1, 1
	if n*3-1 <= pw {
		colW = 2
	}
	out := chartOut{gutter: gutter, stride: colW + gap}
	shown := min(n, (pw+gap)/out.stride)
	out.first = n - shown
	vals := o.vals[out.first:]
	hi := 0.0
	for _, v := range vals {
		hi = math.Max(hi, v)
	}
	level := func(v float64) int {
		if hi <= 0 || o.grow <= 0 {
			return 0
		}
		return max(1, int(math.Round(v/hi*float64(chartRows*8)*o.grow)))
	}
	medRow := -1
	if o.hasMedian && hi > 0 {
		ml := min(chartRows*8, max(1, int(math.Round(o.median/hi*float64(chartRows*8)))))
		medRow = chartRows - 1 - (ml-1)/8
	}
	type glyph struct {
		r rune
		k uint32 // key24 of c: colours compare by value
		c color.Color
	}
	mk := func(r rune, c color.Color) glyph { return glyph{r, key24(c), c} }
	grid := make([][]glyph, chartRows)
	for r := range grid {
		grid[r] = make([]glyph, pw)
		for x := range grid[r] {
			grid[r][x] = glyph{' ', 0, nil}
		}
		if r == medRow {
			for x := range grid[r] {
				grid[r][x] = mk('·', th.Faint)
			}
		}
	}
	for j, v := range vals {
		x := j * out.stride
		base := o.pick(out.first + j)
		col := mix(base, p.bg, 0.3)
		if j == shown-1 {
			col = base
		}
		l := level(v)
		for r := 0; r < chartRows; r++ {
			for c := 0; c < colW; c++ {
				if fill := l - (chartRows-1-r)*8; fill >= 8 {
					grid[r][x+c] = mk('█', col)
				} else if fill > 0 {
					grid[r][x+c] = mk(chartBlocks[fill-1], col)
				}
			}
		}
		if o.star && j == shown-1 && l > 0 {
			grid[max(0, chartRows-1-(l-1)/8-1)][x] = mk('★', th.Yellow)
		}
	}
	for r := 0; r < chartRows; r++ {
		lab := ""
		switch {
		case r == 0:
			lab = o.label(hi)
		case r == medRow:
			lab = o.label(o.median)
		}
		var sb strings.Builder
		sb.WriteString(p.t(cell(lab, gutter-1, true), th.Muted) + p.sp(1))
		for x := 0; x < pw; {
			y := x + 1
			for y < pw && grid[r][y] == grid[r][x] {
				y++
			}
			g := grid[r][x]
			if g.c == nil {
				sb.WriteString(p.sp(y - x))
			} else {
				sb.WriteString(p.t(strings.Repeat(string(g.r), y-x), g.c))
			}
			x = y
		}
		out.lines = append(out.lines, fit(sb.String(), w, p))
	}
	return out
}

// ---- line chart -------------------------------------------------------------------

const lineRows = 8 // rows tall; each holds four dots

var dotBits = [2][4]rune{{0x01, 0x02, 0x04, 0x40}, {0x08, 0x10, 0x20, 0x80}}

// lineSeries is one line: values oldest first, as many as every other series has. A NaN
// value is a gap: nothing is drawn for it and no line crosses it.
type lineSeries struct {
	name  string
	color color.Color
	vals  []float64
}

type lineOpts struct {
	series []lineSeries
	label  func(float64) string // y and end labels
}

// dotsBetween calls set for every dot on the straight line from (x0, y0) to (x1, y1).
func dotsBetween(x0, y0, x1, y1 int, set func(x, y int)) {
	dx, dy := max(x1-x0, x0-x1), -max(y1-y0, y0-y1)
	sx, sy := 1, 1
	if x1 < x0 {
		sx = -1
	}
	if y1 < y0 {
		sy = -1
	}
	for e := dx + dy; ; {
		set(x0, y0)
		if x0 == x1 && y0 == y1 {
			return
		}
		e2 := 2 * e
		if e2 >= dy {
			e, x0 = e+dy, x0+sx
		}
		if e2 <= dx {
			e, y0 = e+dx, y0+sy
		}
	}
}

// lineChart draws the series as braille lines in w cells on p's background: a y gutter,
// the plot, and each line's name and latest value at its end. The scale runs from 0 to the
// largest drawn value rounded up to 10%. Where two lines share a cell the earlier series
// owns its colour. The points are spread over the plot, as many dots apart as fit; when
// even one dot each does not fit, only the newest that do are drawn.
func (p paint) lineChart(o lineOpts, w int) chartOut {
	th := p.th
	n := len(o.series[0].vals)
	scale := func(from int) float64 {
		hi := 0.0
		for _, s := range o.series {
			for _, v := range s.vals[from:] {
				if !math.IsNaN(v) {
					hi = math.Max(hi, v)
				}
			}
		}
		return math.Max(0.1, math.Ceil(hi*10-1e-9)/10)
	}
	positive := func(vals []float64) bool { return slices.ContainsFunc(vals, func(v float64) bool { return v > 0 }) }
	ends := make([]string, len(o.series))
	endW := 0
	for i, s := range o.series {
		if positive(s.vals) && !math.IsNaN(s.vals[n-1]) {
			ends[i] = s.name + " " + o.label(s.vals[n-1])
			endW = max(endW, width(ends[i])+1)
		}
	}
	gutter := width(o.label(scale(0))) + 1
	pw := max(8, w-gutter-endW)
	step := max(1, (2*pw-1)/(n-1))
	out := chartOut{gutter: gutter, step: step, plot: pw}
	if (n-1)*step+1 > 2*pw {
		out.first = n - 2*pw
	}
	top := scale(out.first)
	type cellDots struct {
		bits rune
		col  color.Color
		k    uint32
	}
	grid := make([][]cellDots, lineRows)
	for r := range grid {
		grid[r] = make([]cellDots, pw)
	}
	endRow := make([]int, len(o.series))
	for si := len(o.series) - 1; si >= 0; si-- {
		s := o.series[si]
		vals := s.vals[out.first:]
		if !positive(vals) {
			ends[si] = ""
			continue
		}
		set := func(x, y int) {
			c := &grid[y/4][x/2]
			c.bits |= dotBits[x%2][y%4]
			c.col, c.k = s.color, key24(s.color)
		}
		var px, py int
		joined := false
		for j, v := range vals {
			if math.IsNaN(v) {
				joined = false
				continue
			}
			x, y := j*step, lineRows*4-1-int(math.Round(v/top*(lineRows*4-1)))
			if !joined {
				px, py = x, y
			}
			dotsBetween(px, py, x, y, set)
			px, py, joined = x, y, true
		}
		endRow[si] = py / 4
	}
	// end labels read top to bottom in the lines' order: the highest latest value first, each on
	// its line's row or the next free one below; a block that runs off the bottom moves up
	var labelled []int
	for si := range o.series {
		if ends[si] != "" {
			labelled = append(labelled, si)
		}
	}
	slices.SortStableFunc(labelled, func(a, b int) int { return cmp.Compare(o.series[b].vals[n-1], o.series[a].vals[n-1]) })
	labelled = labelled[:min(len(labelled), lineRows)]
	rows := make([]int, len(labelled))
	for i, si := range labelled {
		rows[i] = endRow[si]
		if i > 0 {
			rows[i] = max(rows[i], rows[i-1]+1)
		}
	}
	for i, limit := len(rows)-1, lineRows-1; i >= 0 && rows[i] > limit; i, limit = i-1, limit-1 {
		rows[i] = limit
	}
	labelAt := make([]string, lineRows)
	for i, si := range labelled {
		s := o.series[si]
		labelAt[rows[i]] = p.sp(1) + p.t(s.name, s.color) + p.sp(1) + p.b(o.label(s.vals[n-1]), th.Text)
	}
	for r := 0; r < lineRows; r++ {
		lab := ""
		switch r {
		case 0:
			lab = o.label(top)
		case lineRows - 1:
			lab = o.label(0)
		}
		var sb strings.Builder
		sb.WriteString(p.t(cell(lab, gutter-1, true), th.Muted) + p.sp(1))
		for x := 0; x < pw; {
			y := x + 1
			for y < pw && grid[r][y].bits == grid[r][x].bits && grid[r][y].k == grid[r][x].k {
				y++
			}
			if d := grid[r][x]; d.bits == 0 {
				sb.WriteString(p.sp(y - x))
			} else {
				sb.WriteString(p.t(strings.Repeat(string(0x2800+d.bits), y-x), d.col))
			}
			x = y
		}
		sb.WriteString(labelAt[r])
		out.lines = append(out.lines, fit(sb.String(), w, p))
	}
	return out
}
