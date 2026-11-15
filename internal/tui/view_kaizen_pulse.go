package tui

import (
	"fmt"
	"image/color"
	"math"
	"slices"
	"strconv"
	"strings"

	"isshoni/internal/data"
)

// pulseCol is one figure of one period: its value and the count (or detail) beside it.
type pulseCol struct{ val, n string }

// text is the column as shown: the value, then the count after gap.
func (c pulseCol) text(gap string) string {
	if c.n == "" {
		return c.val
	}
	return c.val + gap + c.n
}

// pulseRow is a labelled figure for today and the baseline; label is the wide label and
// short the narrow one, each empty where the row is left out. A lead row has a delta, a
// verdict and the per-day values for the sparkline; a detail row is quiet.
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

// pulseCardH is the height of a card: label, today, delta against the baseline, sparkline, detail.
const pulseCardH = 5

// dayLines is the band at the top of the Pulse page: today against the baseline of days
// before it. Wide, four cards (cost, first pass, approval → merge, conflicts) with a verdict
// colour, a delta and a sparkline each; narrow, a row for each figure and its detail. Each
// unknown is shown as "-"; nothing when the producer wrote no pulse.
func (m *model) dayLines(p paint, w int) block {
	pl := m.kz.doc.Pulse
	if pl == nil {
		return nil
	}
	th := m.th
	wide := w >= 100
	rows := m.pulseRows(pl, wide)
	rows = slices.DeleteFunc(rows, func(r pulseRow) bool { return (wide && r.label == "") || (!wide && r.short == "") })
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
	var lines block
	var notes []string
	if wide {
		var send *pulseRow
		lines, send = m.pulseCards(p, rows, w)
		lines = append(block{fit(p.sp(1)+p.b(title, th.Text), w, p)}, lines...)
		if send != nil {
			notes = append(notes, fmt.Sprintf("%s: %s today · %s baseline", strings.TrimSpace(send.label), send.today.val, send.base.val))
		}
	} else {
		lines = m.pulseTable(p, rows, pl, title, since, w)
	}
	periods := []data.PulseDay{pl.Today, pl.Baseline}
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

// verdictLook is the colour of a delta and of today's mark: green better, orange worse,
// muted (text for the mark) for same, few and unknown.
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
// stripe in the verdict's colour. A lead row's first detail row is the card's last line;
// a second one (the send-backs) is returned for the line under the cards.
func (m *model) pulseCards(p paint, rows []pulseRow, w int) (block, *pulseRow) {
	var leads []pulseRow
	var details []string
	var send *pulseRow
	for i, r := range rows {
		switch {
		case r.lead:
			leads, details = append(leads, r), append(details, "")
		case details[len(details)-1] == "":
			details[len(details)-1] = r.today.val
		default:
			send = &rows[i]
		}
	}
	widths := cardWidths(w, len(leads))
	var cols []block
	for i, r := range leads {
		cols = append(cols, m.pulseCard(p, r, details[i], widths[i]))
	}
	return joinCols(p, pulseCardH, tileGap, widths, cols...), send
}

// pulseCard is one card of width w. The sparkline has two cells a day when they fit.
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
	var spark string
	days := min(pulseSparkMax, len(r.days))
	per := 1
	if 2*days <= inner {
		per = 2
	}
	if n := min(days, inner/per); n*per >= 4 {
		spark = sp.spark(r.days[len(r.days)-n:], per, th.Muted, tcol)
	}
	lines := []string{
		sp.t(label, th.Faint),
		sp.b(r.today.val, th.Text) + sp.t("  "+r.today.n, th.Muted),
		delta + sp.t("vs "+r.base.val+" "+r.base.n, th.Muted),
		spark,
		sp.t(detail, th.Muted),
	}
	out := make(block, pulseCardH)
	for i, ln := range lines {
		out[i] = stripe + fit(sp.sp(1)+trunc(ln, inner), w-1, sp)
	}
	return out
}

// shortDate is the month and day of a YYYY-MM-DD date.
func shortDate(d string) string { return d[min(5, len(d)):] }

// pulseRows are the band's rows in order: each lead figure, then its detail.
func (m *model) pulseRows(pl *data.Pulse, wide bool) []pulseRow {
	t, b, v := pl.Today, pl.Baseline, pl.Verdicts
	n := func(k int) string { return "n=" + strconv.Itoa(k) }
	both := func(label, short string, f func(data.PulseDay) pulseCol) pulseRow {
		return pulseRow{label: label, short: short, today: f(t), base: f(b)}
	}
	lead := func(label, short string, get func(data.PulseDay) *float64, f func(data.PulseDay) pulseCol, verdict *string, pts bool) pulseRow {
		r := both(label, short, f)
		r.lead, r.verdict, r.delta = true, verdict, pulseDelta(get(t), get(b), verdict, pts)
		for _, d := range pl.Days {
			r.days = append(r.days, get(d))
		}
		return r
	}
	secs := func(s *int) *float64 {
		if s == nil {
			return nil
		}
		return ptrTo(float64(*s))
	}
	yesOf := func(r data.PulseRate) string {
		if r.Of == 0 {
			return "-"
		}
		return fmt.Sprintf("%d/%d", r.Yes, r.Of)
	}
	p90 := func(q data.PulseP90) pulseCol {
		if q.P90S == nil {
			return pulseCol{val: "-"}
		}
		return pulseCol{kzSecs(float64(*q.P90S)), n(q.N)}
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
	if wide {
		rows = append(rows, both("  tasks · dispatcher · site manager", "", func(d data.PulseDay) pulseCol {
			c := d.Cost
			return pulseCol{val: m.kzMoney(c.Tasks) + " · " + m.kzMoney(c.Dispatcher) + " · " + m.kzMoney(c.SiteManager)}
		}))
	}

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
			return ptrTo(float64(r.Yes) / float64(r.Of))
		}
		return nil
	}, func(d data.PulseDay) pulseCol {
		r := pass(d)
		if r.Of == 0 {
			return pulseCol{"-", n(0)}
		}
		return pulseCol{pct(float64(r.Yes) / float64(r.Of) * 100), fmt.Sprintf("%d/%d", r.Yes, r.Of)}
	}, v.FirstPass, true))
	sizes := []struct {
		short string
		get   func(data.PulseFirstPass) data.PulseRate
	}{
		{"small", func(f data.PulseFirstPass) data.PulseRate { return f.Small }},
		{"medium", func(f data.PulseFirstPass) data.PulseRate { return f.Medium }},
		{"large", func(f data.PulseFirstPass) data.PulseRate { return f.Large }},
		{"unstated", func(f data.PulseFirstPass) data.PulseRate { return f.Unstated }},
	}
	if wide {
		rows = append(rows, both("  small · medium · large · unstated", "", func(d data.PulseDay) pulseCol {
			var parts []string
			for _, s := range sizes {
				parts = append(parts, yesOf(s.get(d.FirstPass)))
			}
			return pulseCol{val: strings.Join(parts, " · ")}
		}))
	} else {
		for _, s := range sizes {
			rows = append(rows, both("", "  "+s.short, func(d data.PulseDay) pulseCol { return pulseCol{val: yesOf(s.get(d.FirstPass))} }))
		}
	}

	// wide: the p90s are one detail row; narrow keeps a row each
	rows = append(rows, lead("approval → merge, median", "approval → merge p50", func(d data.PulseDay) *float64 { return secs(d.Handover.ApprovalToMerge.P50S) }, func(d data.PulseDay) pulseCol {
		q := d.Handover.ApprovalToMerge
		if q.P50S == nil {
			return pulseCol{"-", n(q.N)}
		}
		return pulseCol{kzSecs(float64(*q.P50S)), n(q.N)}
	}, v.ApprovalToMerge, false))
	if wide {
		rows = append(rows, both("  p90: approval → merge · hand-over → merge · → approval", "", func(d data.PulseDay) pulseCol {
			h := d.Handover
			return pulseCol{val: "p90 " + p90(h.ApprovalToMerge).val + " · " + p90(h.ToMerge).val + " · " + p90(h.ToApproval).val}
		}))
	} else {
		rows = append(rows,
			both("", "  approval → merge p90", p90row(func(h data.PulseHandover) data.PulseP90 { return h.ApprovalToMerge })),
			both("", "  hand-over → merge p90", p90row(func(h data.PulseHandover) data.PulseP90 { return h.ToMerge })),
			both("", "  → approval p90", p90row(func(h data.PulseHandover) data.PulseP90 { return h.ToApproval })))
	}
	rows = append(rows, both("  send-backs that changed nothing", "  sent back unchanged", func(d data.PulseDay) pulseCol {
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
	rows = append(rows, both("  conflicted base merges", "  conflicted merges", func(d data.PulseDay) pulseCol {
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
		return arrow + " 100%"
	}
	return fmt.Sprintf("%s %.0f%%", arrow, math.Abs(d / *base * 100))
}

func ptrTo[T any](v T) *T { return &v }

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
