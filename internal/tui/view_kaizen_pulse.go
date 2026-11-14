package tui

import (
	"fmt"
	"image/color"
	"strconv"

	"isshoni/internal/data"
)

// What the review of the factory found worth a warning colour on the band.
const (
	pulseSlowHandover = 15 * 60 // seconds: p90 of hand-over to merge, and of approval to merge
	pulseManyConflict = 0.2     // conflicted base merges per merged pull request
)

// pulseCol is one figure of one day: its value, the count beside it, and whether it is bad.
type pulseCol struct {
	val, note string
	bad       bool
	val2      string // a second figure after " · ", with its own warning
	bad2      bool
}

// pulseRow is a labelled figure for both days; short is the label at narrow widths.
type pulseRow struct {
	label, short string
	days         [2]pulseCol
}

// dayLines is the band at the top of the Pulse page: four figures for today against
// yesterday, each unknown shown as "-", or nothing when the producer wrote no pulse.
func (m *model) dayLines(p paint, w int) block {
	pl := m.kz.doc.Pulse
	if pl == nil {
		return nil
	}
	th := m.th
	wide := w >= 100
	lw := 26
	if wide {
		lw = 44
	}
	cw := min((w-lw)/2, 36) // fixed, so yesterday starts a few cells after the widest today cell
	days := [2]data.PulseDay{pl.Today, pl.Yesterday}
	label := func(r pulseRow) string {
		if wide {
			return r.label
		}
		return r.short
	}
	rows := m.pulseRows(days, wide)
	fg := func(bad bool) color.Color {
		if bad {
			return th.Orange
		}
		return th.Text
	}
	put := func(label string, c [2]pulseCol) string {
		line := p.sp(1) + p.t(cell(label, lw-1, false), th.Muted)
		for _, d := range c {
			main, rest, note := trunc(d.val, cw-1), "", ""
			used := width(main)
			if d.val2 != "" {
				rest = trunc(d.val2, max(0, cw-1-used-3))
				used += 3 + width(rest)
			}
			if d.note != "" {
				note = trunc("  "+d.note, max(0, cw-1-used))
			}
			line += p.b(main, fg(d.bad))
			if d.val2 != "" {
				line += p.t(" · ", th.Muted) + p.b(rest, fg(d.bad2))
			}
			line += p.t(note, th.Muted) + p.sp(cw-used-width(note))
		}
		return fit(line, w, p)
	}

	title := "TODAY AGAINST YESTERDAY"
	if !wide {
		title = "TODAY VS YESTERDAY"
	}
	head := p.sp(1) + p.b(cell(title, lw-1, false), th.Text)
	for i, name := range []string{"today ", "yesterday "} {
		head += p.t(cell(name+days[i].Date[min(5, len(days[i].Date)):], cw, false), th.Faint)
	}
	lines := block{fit(head, w, p)}
	for _, r := range rows {
		lines = append(lines, put(label(r), r.days))
	}
	if note := m.sendBackNote(days, pl.QueueSince); note != "" {
		lines = append(lines, p.sp(1)+p.t(note, th.Faint))
	}
	for _, d := range days {
		if e := d.Conflicts.Error; e != nil {
			for i, l := range wrapPlain(fmt.Sprintf("conflicted base merges %s: %s", d.Date, *e), w-5) {
				lead := "  "
				if i == 0 {
					lead = "⚠ "
				}
				lines = append(lines, p.sp(1)+p.t(lead+l, th.Yellow))
			}
		}
	}
	return append(lines, "")
}

// pulseRows are the table's rows: the four figures, in the order the band shows them.
func (m *model) pulseRows(days [2]data.PulseDay, wide bool) []pulseRow {
	both := func(f func(data.PulseDay) pulseCol) (c [2]pulseCol) {
		for i, d := range days {
			c[i] = f(d)
		}
		return
	}
	n := func(k int) string { return "n=" + strconv.Itoa(k) }
	rate := func(r data.PulseRate) string {
		if r.Of == 0 {
			return "-"
		}
		return pct(float64(r.Yes)/float64(r.Of)*100) + fmt.Sprintf(" %d/%d", r.Yes, r.Of)
	}
	dur := func(q data.PulseP90, slow bool) (string, bool) {
		if q.P90S == nil {
			return "-", false
		}
		return kzSecs(float64(*q.P90S)), slow && *q.P90S > pulseSlowHandover
	}
	p90 := func(get func(data.PulseHandover) data.PulseP90, slow bool, note func(data.PulseHandover) string) func(data.PulseDay) pulseCol {
		return func(d data.PulseDay) pulseCol {
			q := get(d.Handover)
			v, bad := dur(q, slow)
			if q.P90S == nil {
				return pulseCol{val: v}
			}
			return pulseCol{val: v, note: n(q.N) + note(d.Handover), bad: bad}
		}
	}
	none := func(data.PulseHandover) string { return "" }
	backfilled := func(h data.PulseHandover) string {
		if h.ApprovalsBackfilled == 0 {
			return ""
		}
		return fmt.Sprintf(" (%d backfilled)", h.ApprovalsBackfilled)
	}
	toApproval := func(h data.PulseHandover) data.PulseP90 { return h.ToApproval }
	approvalToMerge := func(h data.PulseHandover) data.PulseP90 { return h.ApprovalToMerge }
	cost := func(d data.PulseDay) pulseCol {
		if d.Cost.PerMerged == nil {
			return pulseCol{val: "-"}
		}
		v := m.kzMoney(*d.Cost.PerMerged)
		if d.Cost.UnpricedTokens > 0 {
			v += "*"
		}
		return pulseCol{val: v, note: n(d.Merged)}
	}
	rows := []pulseRow{{"all-in cost / merged PR", "cost / merged PR", both(cost)}}
	if wide {
		rows = append(rows, pulseRow{"  tasks + dispatcher + site manager", "", both(func(d data.PulseDay) pulseCol {
			c := d.Cost
			return pulseCol{val: m.kzMoney(c.Tasks) + " + " + m.kzMoney(c.Dispatcher) + " + " + m.kzMoney(c.SiteManager)}
		})})
	}
	rows = append(rows, firstPassRows(both, rate, wide)...)
	rows = append(rows, pulseRow{"hand-over → merge p90", "hand-over → merge p90", both(p90(func(h data.PulseHandover) data.PulseP90 { return h.ToMerge }, true, none))})
	if wide {
		rows = append(rows, pulseRow{"  to approval · approval to merge", "", both(func(d data.PulseDay) pulseCol {
			h := d.Handover
			if h.ToApproval.P90S == nil {
				return pulseCol{val: "-"}
			}
			a, _ := dur(h.ToApproval, false)
			b, bad := dur(h.ApprovalToMerge, true)
			return pulseCol{val: a, val2: b, bad2: bad, note: n(h.ToApproval.N) + backfilled(h)}
		})})
	} else {
		rows = append(rows,
			pulseRow{"", "  → approval p90", both(p90(toApproval, false, backfilled))},
			pulseRow{"", "  approval → merge p90", both(p90(approvalToMerge, true, none))})
	}
	rows = append(rows,
		pulseRow{"  send-backs that changed nothing", "  sent back unchanged", both(func(d data.PulseDay) pulseCol {
			h := d.Handover
			if h.SentBack == nil || h.SentBackUnchanged == nil {
				return pulseCol{val: "-"}
			}
			return pulseCol{val: fmt.Sprintf("%d of %d", *h.SentBackUnchanged, *h.SentBack), bad: *h.SentBackUnchanged > 0}
		})},
		pulseRow{"conflicted base merges / PR (git history)", "conflicts (git history)", both(func(d data.PulseDay) pulseCol {
			c := d.Conflicts
			if c.PerMerged == nil {
				return pulseCol{val: "-"}
			}
			return pulseCol{val: fmt.Sprintf("%.2f", *c.PerMerged), note: fmt.Sprintf("%d in %d PRs", c.Conflicted, c.Prs), bad: *c.PerMerged > pulseManyConflict}
		})},
	)
	return rows
}

// sendBackNote says why send-backs show "-": the queue log did not cover a day.
func (m *model) sendBackNote(days [2]data.PulseDay, since *string) string {
	for _, d := range days {
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

// firstPassRows are the first-pass rate by stated size: two sizes a row when wide, one at narrow widths.
func firstPassRows(both func(func(data.PulseDay) pulseCol) [2]pulseCol, rate func(data.PulseRate) string, wide bool) []pulseRow {
	type size struct {
		label, short string
		get          func(data.PulseFirstPass) data.PulseRate
	}
	sizes := []size{
		{"small", "small", func(f data.PulseFirstPass) data.PulseRate { return f.Small }},
		{"medium", "medium", func(f data.PulseFirstPass) data.PulseRate { return f.Medium }},
		{"large", "large", func(f data.PulseFirstPass) data.PulseRate { return f.Large }},
		{"no size stated", "unstated", func(f data.PulseFirstPass) data.PulseRate { return f.Unstated }},
	}
	var rows []pulseRow
	if wide {
		for i, lead := range []string{"first pass  ", "            "} {
			a, b := sizes[2*i], sizes[2*i+1]
			rows = append(rows, pulseRow{label: lead + a.label + " · " + b.label, days: both(func(d data.PulseDay) pulseCol {
				return pulseCol{val: rate(a.get(d.FirstPass)), val2: rate(b.get(d.FirstPass))}
			})})
		}
		return rows
	}
	for i, sz := range sizes {
		lead := "         "
		if i == 0 {
			lead = "1st pass "
		}
		rows = append(rows, pulseRow{short: lead + sz.short, days: both(func(d data.PulseDay) pulseCol {
			return pulseCol{val: rate(sz.get(d.FirstPass))}
		})})
	}
	return rows
}
