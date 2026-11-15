package tui

import (
	"fmt"
	"image/color"
	"math"
	"strconv"
	"strings"

	"isshoni/internal/data"
)

// pulseCol is one figure of one period: its value and the count (or detail) beside it.
type pulseCol struct{ val, n string }

// pulseRow is a labelled figure for today and the baseline. A lead row (label is its wide
// name, short the narrow one) has a delta, a verdict and the per-day values for the
// sparkline; a detail row is quiet and narrow only.
type pulseRow struct {
	label, short string
	today, base  pulseCol
	lead         bool
	delta        string
	verdict      *string
	days         []*float64
}

// pulseSparkMax is the most days a sparkline shows, the newest.
const pulseSparkMax = 30

// pulseCardH is the height of a card: label, today, delta against the baseline, strip, detail.
const pulseCardH = 5

// dayLines is the band at the top of the Pulse page: today against the baseline of days
// before it. From 100 columns, four cards (cost, first pass, approval → merge, conflicts)
// with a verdict colour, a delta and a strip of the days each, and a faint block under them
// for what the cards leave out; narrower, a row for each figure and its detail. Each unknown
// is shown as "-"; nothing when the producer wrote no pulse.
func (m *model) dayLines(p paint, w int) block {
	pl := m.kz.doc.Pulse
	if pl == nil {
		return nil
	}
	th := m.th
	wide := w >= 100
	b := pl.Baseline
	title, since := "TODAY AGAINST YESTERDAY", "yesterday "+shortDate(b.Date)
	if !wide {
		title = "TODAY VS YESTERDAY"
	}
	if b.Days != 1 {
		title, since = fmt.Sprintf("TODAY AGAINST THE %d DAYS BEFORE", b.Days), shortDate(b.Date)+" to "+shortDate(b.To)
		if !wide {
			title, since = fmt.Sprintf("TODAY VS %dD BEFORE", b.Days), shortDate(b.Date)+".."+shortDate(b.To) // fits the baseline column at 60
		}
	}
	rows := m.pulseRows(pl)
	var lines block
	periods := []data.PulseDay{pl.Today, pl.Baseline}
	if wide {
		lines = append(block{fit(p.sp(1)+p.b(title, th.Text), w, p)}, m.pulseCards(p, pl, rows, w)...)
		for _, l := range m.pulseBlock(pl, w) {
			lines = append(lines, p.sp(1)+p.t(l, th.Faint))
		}
	} else {
		lines = m.pulseTable(p, rows, pl, title, since, w)
		var notes []string
		if note := m.sendBackNote(periods, pl.QueueSince); note != "" {
			notes = append(notes, note)
		}
		if t, b := pl.Today.Handover.ApprovalsBackfilled, pl.Baseline.Handover.ApprovalsBackfilled; t+b > 0 {
			notes = append(notes, fmt.Sprintf("approvals backfilled: %d today, %d in the baseline", t, b))
		}
		if both := strings.Join(notes, " · "); len(notes) > 1 && 1+width(both) <= w {
			notes = []string{both}
		}
		for _, note := range notes {
			lines = append(lines, p.sp(1)+p.t(note, th.Faint))
		}
	}
	for i, d := range periods {
		if e := d.Conflicts.Error; e != nil {
			for j, l := range wrapPlain(fmt.Sprintf("conflicted base merges %s: %s", []string{"today", "baseline"}[i], *e), w-5) {
				lead := "  "
				if j == 0 {
					lead = "⚠ "
				}
				lines = append(lines, p.sp(1)+p.t(lead+l, th.Yellow))
			}
		}
	}
	return append(lines, "")
}

// verdictLook is the colour of a delta and of today's mark: green better, orange worse;
// same, few and unknown are muted for the delta and the text colour for the mark, so
// today stands apart from the baseline days.
func (m *model) verdictLook(v *string) (delta, today color.Color) {
	switch {
	case v != nil && *v == "better":
		return m.th.Green, m.th.Green
	case v != nil && *v == "worse":
		return m.th.Orange, m.th.Orange
	}
	return m.th.Muted, m.th.Text
}

// pulseTable is the narrow band: a title line, then a row for each figure with today, the
// delta and the baseline in columns.
func (m *model) pulseTable(p paint, rows []pulseRow, pl *data.Pulse, title, since string, w int) block {
	th := m.th
	lw, dc, gap := 25, 9, " " // label, delta, value-to-count gap
	tc := min(20, (w-1-lw-dc+1)/2)
	bc := w - 1 - lw - dc - tc // the baseline column: the rest of the line
	figure := func(c pulseCol, cw int, bold bool, fg, nfg color.Color) string {
		val := trunc(c.val, cw)
		note := ""
		if c.n != "" {
			note = trunc(gap+c.n, max(0, cw-width(val)))
		}
		if bold {
			val = p.b(val, fg)
		} else {
			val = p.t(val, fg)
		}
		return val + p.t(note, nfg) + p.sp(cw-width(val)-width(note))
	}
	put := func(r pulseRow) string {
		if !r.lead {
			return fit(p.sp(1)+p.t(cell(r.short, lw-1, false), th.Faint)+figure(r.today, tc-1, false, th.Muted, th.Faint)+p.sp(1+dc)+figure(r.base, bc, false, th.Muted, th.Faint), w, p)
		}
		dcol, _ := m.verdictLook(r.verdict)
		delta := trunc(r.delta, dc-1)
		return fit(p.sp(1)+p.t(cell(r.short, lw-1, false), th.Text)+figure(r.today, tc-1, true, th.Text, th.Muted)+p.sp(1)+
			p.b(delta, dcol)+p.sp(dc-width(delta))+figure(r.base, bc, false, th.Text, th.Muted), w, p)
	}
	lines := block{fit(p.sp(1)+p.b(cell(title, lw-1, false), th.Text)+p.t(cell("today "+shortDate(pl.Today.Date), tc, false), th.Faint)+
		p.sp(dc)+p.t(cell(since, bc, false), th.Faint), w, p)}
	for _, r := range rows {
		lines = append(lines, put(r))
	}
	return lines
}

// pulseCards lays the lead rows out as cards across w cells, in the tile row's style, the
// stripe in the verdict's colour.
func (m *model) pulseCards(p paint, pl *data.Pulse, rows []pulseRow, w int) block {
	var leads []pulseRow
	for _, r := range rows {
		if r.lead {
			leads = append(leads, r)
		}
	}
	t := pl.Today
	details := []string{
		m.kzMoney(t.Cost.Tasks) + " · " + m.kzMoney(t.Cost.Dispatcher) + " · " + m.kzMoney(t.Cost.SiteManager),
		sizesText(t.FirstPass),
		"p90 " + p90Text(t.Handover.ApprovalToMerge),
		"-",
	}
	if c := t.Conflicts; c.PerMerged != nil {
		details[3] = fmt.Sprintf("%d in %d PRs", c.Conflicted, c.Prs)
	}
	widths := cardWidths(w, len(leads))
	var cols []block
	for i, r := range leads {
		cols = append(cols, m.pulseCard(p, r, details[i], widths[i]))
	}
	return joinCols(p, pulseCardH, tileGap, widths, cols...)
}

// pulseCard is one card of width w. The strip has two cells a day when they fit.
func (m *model) pulseCard(p paint, r pulseRow, detail string, w int) block {
	th := m.th
	sp := p.on(th.Surface)
	dcol, tcol := m.verdictLook(r.verdict)
	stripe := sp.t("▌", dcol)
	if th.NoColor {
		stripe = "▌"
	}
	inner := w - 2
	label := r.label
	if width(label) > inner {
		label = r.short
	}
	delta := ""
	if r.delta != "" {
		delta = sp.b(r.delta, dcol) + sp.sp(1)
	}
	vs := "vs " + r.base.val + " " + r.base.n
	if width(r.delta)+1+width(vs) > inner { // the baseline's count is what gives
		vs = "vs " + r.base.val
	}
	if width(detail) > inner { // the separators' spaces give next
		detail = strings.ReplaceAll(detail, " · ", "·")
	}
	lines := []string{
		sp.t(label, th.Faint),
		sp.b(r.today.val, th.Text) + sp.t("  "+r.today.n, th.Muted),
		delta + sp.t(vs, th.Muted),
		m.pulseStrip(sp, r.days, inner, tcol),
		sp.t(detail, th.Muted),
	}
	out := make(block, pulseCardH)
	for i, ln := range lines {
		out[i] = stripe + fit(sp.sp(1)+ln, w-1, sp)
	}
	return out
}

// pulseStrip is the card's per-day strip in w cells: two cells a day when they fit, the
// newest days when not. When fewer than 3 of its days have a value it carries a faint caption
// saying so, and gives up days to make room for it; the caption is dropped when it does not fit.
func (m *model) pulseStrip(sp paint, days []*float64, w int, today color.Color) string {
	days = days[max(0, len(days)-pulseSparkMax):]
	last := func(room int) ([]*float64, int) { // the newest days that fit room, and cells a day
		per := 1 + btoi(2*len(days) <= room)
		return days[max(0, len(days)-room/per):], per
	}
	shown, per := last(w)
	caption := ""
	if k := valued(shown); k < 3 {
		caption = fmt.Sprintf("%d of %d days with data", k, len(shown))
		if room := w - 1 - width(caption); room >= 4 {
			shown, per = last(room)
			caption = fmt.Sprintf("%d of %d days with data", valued(shown), len(shown))
		} else {
			caption = ""
		}
	}
	spark := ""
	if len(shown)*per >= 4 {
		spark = sp.spark(shown, per, m.th.Muted, today)
	}
	if spark != "" && caption != "" {
		spark += sp.sp(1)
	}
	return spark + sp.t(caption, m.th.Faint)
}

// valued is how many of the days have a value.
func valued(days []*float64) (k int) {
	for _, d := range days {
		k += btoi(d != nil)
	}
	return k
}

func btoi(b bool) int {
	if b {
		return 1
	}
	return 0
}

// pulseBlock is the faint block under the cards: what the cards do not carry, one item to a
// line and two to a line where they fit, wrapped to w.
func (m *model) pulseBlock(pl *data.Pulse, w int) []string {
	t, b := pl.Today, pl.Baseline
	p90s := func(h data.PulseHandover) string { return p90Text(h.ToMerge) + " · " + p90Text(h.ToApproval) }
	sent := func(h data.PulseHandover) string {
		if h.SentBack == nil || h.SentBackUnchanged == nil {
			return "-"
		}
		return fmt.Sprintf("%d of %d", *h.SentBackUnchanged, *h.SentBack)
	}
	backfilled := ""
	if k := b.Handover.ApprovalsBackfilled; k > 0 {
		backfilled = fmt.Sprintf(" (%d backfilled)", k)
	}
	todayBackfilled := ""
	if k := t.Handover.ApprovalsBackfilled; k > 0 {
		todayBackfilled = fmt.Sprintf(" (%d backfilled)", k)
	}
	baseline := shortDate(b.Date)
	if b.Days != 1 {
		baseline += " to " + shortDate(b.To)
	}
	items := []string{
		fmt.Sprintf("hand-over → merge p90 · → approval p90: today %s (n=%d), baseline %s (n=%d)", p90s(t.Handover), t.Handover.ToMerge.N, p90s(b.Handover), b.Handover.ToMerge.N),
		fmt.Sprintf("baseline %s: parts %s · %s · %s · first pass %s · approval → merge p90 %s", baseline,
			m.kzMoney(b.Cost.Tasks), m.kzMoney(b.Cost.Dispatcher), m.kzMoney(b.Cost.SiteManager), sizesText(b.FirstPass), p90Text(b.Handover.ApprovalToMerge)),
	}
	if c := b.Conflicts; c.PerMerged != nil {
		items = append(items, fmt.Sprintf("baseline conflicts %d in %d PRs", c.Conflicted, c.Prs))
	}
	items = append(items, fmt.Sprintf("send-backs that changed nothing: %s today · %s baseline", sent(t.Handover), sent(b.Handover)))
	if note := m.sendBackNote([]data.PulseDay{t, b}, pl.QueueSince); note != "" {
		items = append(items, note)
	}
	items = append(items, fmt.Sprintf("approvals recorded: %d today%s, %d of %d in the baseline%s", t.Handover.ApprovalToMerge.N, todayBackfilled, b.Handover.ApprovalToMerge.N, b.Handover.ToMerge.N, backfilled))
	var rows []string
	for _, it := range items {
		if last := len(rows) - 1; last >= 0 && 1+width(rows[last]+" · "+it) <= w {
			rows[last] += " · " + it
		} else {
			rows = append(rows, it)
		}
	}
	var out []string
	for _, r := range rows {
		out = append(out, wrapPlain(r, w-1)...)
	}
	return out
}

// shortDate is the month and day of a YYYY-MM-DD date.
func shortDate(d string) string { return d[min(5, len(d)):] }

// yesOf is a first-pass rate as yes/of, "-" when nothing was counted.
func yesOf(r data.PulseRate) string {
	if r.Of == 0 {
		return "-"
	}
	return fmt.Sprintf("%d/%d", r.Yes, r.Of)
}

var pulseSizes = []struct {
	short string
	get   func(data.PulseFirstPass) data.PulseRate
}{
	{"small", func(f data.PulseFirstPass) data.PulseRate { return f.Small }},
	{"medium", func(f data.PulseFirstPass) data.PulseRate { return f.Medium }},
	{"large", func(f data.PulseFirstPass) data.PulseRate { return f.Large }},
	{"unstated", func(f data.PulseFirstPass) data.PulseRate { return f.Unstated }},
}

// sizesText is the first pass by stated size, small to unstated.
func sizesText(f data.PulseFirstPass) string {
	var parts []string
	for _, s := range pulseSizes {
		parts = append(parts, yesOf(s.get(f)))
	}
	return strings.Join(parts, " · ")
}

// p90Text is a duration's p90, "-" when there is none.
func p90Text(q data.PulseP90) string {
	if q.P90S == nil {
		return "-"
	}
	return kzSecs(float64(*q.P90S))
}

// pulseRows are the band's rows in order: each lead figure, then, for the narrow table, its detail.
func (m *model) pulseRows(pl *data.Pulse) []pulseRow {
	t, b, v := pl.Today, pl.Baseline, pl.Verdicts
	n := func(k int) string { return "n=" + strconv.Itoa(k) }
	both := func(short string, f func(data.PulseDay) pulseCol) pulseRow {
		return pulseRow{short: short, today: f(t), base: f(b)}
	}
	lead := func(label, short string, get func(data.PulseDay) *float64, f func(data.PulseDay) pulseCol, verdict *string, pts bool) pulseRow {
		r := both(short, f)
		r.label, r.lead, r.verdict, r.delta = label, true, verdict, pulseDelta(get(t), get(b), verdict, pts)
		for _, d := range pl.Days {
			r.days = append(r.days, get(d))
		}
		return r
	}
	secs := func(s *int) *float64 {
		if s == nil {
			return nil
		}
		return new(float64(*s))
	}
	p90 := func(q data.PulseP90) pulseCol {
		if q.P90S == nil {
			return pulseCol{val: "-"}
		}
		return pulseCol{p90Text(q), n(q.N)}
	}
	p90row := func(get func(data.PulseHandover) data.PulseP90) func(data.PulseDay) pulseCol {
		return func(d data.PulseDay) pulseCol { return p90(get(d.Handover)) }
	}

	rows := []pulseRow{lead("all-in cost / merged PR", "cost / merged PR", func(d data.PulseDay) *float64 { return d.Cost.PerMerged }, func(d data.PulseDay) pulseCol {
		if d.Cost.PerMerged == nil {
			return pulseCol{"-", n(d.Merged)}
		}
		val := m.kzMoney(*d.Cost.PerMerged)
		if d.Cost.UnpricedTokens > 0 {
			val += "*"
		}
		return pulseCol{val, n(d.Merged)}
	}, v.Cost, false)}

	pass := func(d data.PulseDay) data.PulseRate {
		f := d.FirstPass
		var all data.PulseRate
		for _, r := range []data.PulseRate{f.Small, f.Medium, f.Large, f.Unstated} {
			all.Yes, all.Of = all.Yes+r.Yes, all.Of+r.Of
		}
		return all
	}
	rows = append(rows, lead("first pass", "first pass", func(d data.PulseDay) *float64 {
		if r := pass(d); r.Of > 0 {
			return new(float64(r.Yes) / float64(r.Of))
		}
		return nil
	}, func(d data.PulseDay) pulseCol {
		r := pass(d)
		if r.Of == 0 {
			return pulseCol{"-", n(0)}
		}
		return pulseCol{pct(float64(r.Yes) / float64(r.Of) * 100), fmt.Sprintf("%d/%d", r.Yes, r.Of)}
	}, v.FirstPass, true))
	for _, s := range pulseSizes {
		rows = append(rows, both("  "+s.short, func(d data.PulseDay) pulseCol { return pulseCol{val: yesOf(s.get(d.FirstPass))} }))
	}

	rows = append(rows, lead("approval → merge, median", "approval → merge p50", func(d data.PulseDay) *float64 { return secs(d.Handover.ApprovalToMerge.P50S) }, func(d data.PulseDay) pulseCol {
		q := d.Handover.ApprovalToMerge
		if q.P50S == nil {
			return pulseCol{"-", n(q.N)}
		}
		return pulseCol{kzSecs(float64(*q.P50S)), n(q.N)}
	}, v.ApprovalToMerge, false),
		both("  approval → merge p90", p90row(func(h data.PulseHandover) data.PulseP90 { return h.ApprovalToMerge })),
		both("  hand-over → merge p90", p90row(func(h data.PulseHandover) data.PulseP90 { return h.ToMerge })),
		both("  → approval p90", p90row(func(h data.PulseHandover) data.PulseP90 { return h.ToApproval })))
	rows = append(rows, both("  sent back unchanged", func(d data.PulseDay) pulseCol {
		h := d.Handover
		if h.SentBack == nil || h.SentBackUnchanged == nil {
			return pulseCol{val: "-"}
		}
		return pulseCol{val: fmt.Sprintf("%d of %d", *h.SentBackUnchanged, *h.SentBack)}
	}), lead("conflicted base merges / PR", "conflicts / PR", func(d data.PulseDay) *float64 { return d.Conflicts.PerMerged }, func(d data.PulseDay) pulseCol {
		c := d.Conflicts
		if c.PerMerged == nil {
			return pulseCol{"-", n(c.Prs)}
		}
		return pulseCol{fmt.Sprintf("%.2f", *c.PerMerged), n(c.Prs)}
	}, v.Conflicts, false))
	rows = append(rows, both("  conflicted merges", func(d data.PulseDay) pulseCol {
		c := d.Conflicts
		if c.PerMerged == nil {
			return pulseCol{val: "-"}
		}
		return pulseCol{val: fmt.Sprintf("%d in %d PRs", c.Conflicted, c.Prs)}
	}))
	return rows
}

// pulseDelta is how far today is from the baseline: relative percent, or points for a
// rate; "≈" when the verdict is same, nothing when it is unknown.
func pulseDelta(today, base *float64, verdict *string, points bool) string {
	if verdict == nil || today == nil || base == nil {
		return ""
	}
	d := *today - *base
	if *verdict == "same" || d == 0 {
		return "≈"
	}
	arrow := "↑"
	if d < 0 {
		arrow = "↓"
	}
	if points {
		return fmt.Sprintf("%s %.0f pts", arrow, math.Abs(d)*100)
	}
	if *base == 0 {
		return arrow // a change from zero has no relative size
	}
	return fmt.Sprintf("%s %.0f%%", arrow, math.Abs(d / *base * 100))
}

// sendBackNote says why send-backs show "-": the queue log did not cover a period.
func (m *model) sendBackNote(periods []data.PulseDay, since *string) string {
	for _, d := range periods {
		if d.Handover.SentBack != nil {
			continue
		}
		if since == nil {
			return "send-backs: no queue log yet"
		}
		return "send-backs: the queue log starts " + kzTime(*since)
	}
	return ""
}
