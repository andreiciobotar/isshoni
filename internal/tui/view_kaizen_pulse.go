package tui

import (
	"fmt"
	"strconv"
	"strings"

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
	lw := 24
	if wide {
		lw = 38
	}
	cw := min((w-lw)/2, 32) // fixed, so yesterday starts a few cells after the widest today cell
	days := [2]data.PulseDay{pl.Today, pl.Yesterday}
	label := func(r pulseRow) string {
		if wide {
			return r.label
		}
		return r.short
	}
	rows := m.pulseRows(days, wide)
	mw := 0 // the widest figure that has a count beside it, so the counts line up
	for _, r := range rows {
		for _, c := range r.days {
			if c.note != "" {
				mw = max(mw, width(c.val))
			}
		}
	}
	put := func(label string, c [2]pulseCol) string {
		line := p.sp(1) + p.t(cell(label, lw-1, false), th.Muted)
		for _, d := range c {
			main, note := trunc(d.val, cw-1), ""
			if d.note != "" {
				note = trunc(strings.Repeat(" ", mw-width(main))+"  "+d.note, max(0, cw-1-width(main)))
			}
			fg := th.Text
			if d.bad {
				fg = th.Orange
			}
			line += p.b(main, fg) + p.t(note, th.Muted) + p.sp(cw-width(main)-width(note))
		}
		return fit(line, w, p)
	}

	title := "TODAY AGAINST YESTERDAY"
	if !wide {
		title = "TODAY VS YESTERDAY"
	}
	head := p.sp(1) + p.sp(lw-1)
	for i, name := range []string{"today ", "yesterday "} {
		head += p.t(cell(name+days[i].Date[min(5, len(days[i].Date)):], cw, false), th.Faint)
	}
	lines := block{m.heading(p, w, title, ""), fit(head, w, p)}
	for _, r := range rows {
		lines = append(lines, put(label(r), r.days))
	}
	lines = append(lines, p.sp(1)+p.t("  (from git history)", th.Faint))
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
	size := func(label string, rate func(data.PulseFirstPass) data.PulseRate) pulseRow {
		short := label
		if label == "no size stated" {
			short = "unstated"
		}
		return pulseRow{label, short, both(func(d data.PulseDay) pulseCol {
			r := rate(d.FirstPass)
			if r.Of == 0 {
				return pulseCol{val: "-"}
			}
			return pulseCol{val: pct(float64(r.Yes) / float64(r.Of) * 100), note: fmt.Sprintf("%d/%d", r.Yes, r.Of)}
		})}
	}
	p90 := func(get func(data.PulseHandover) data.PulseP90, slowAbove int, note func(data.PulseHandover) string) func(data.PulseDay) pulseCol {
		return func(d data.PulseDay) pulseCol {
			q := get(d.Handover)
			if q.P90S == nil {
				return pulseCol{val: "-"}
			}
			return pulseCol{val: kzSecs(float64(*q.P90S)), note: n(q.N) + note(d.Handover), bad: *q.P90S > slowAbove}
		}
	}
	none := func(data.PulseHandover) string { return "" }
	backfilled := func(h data.PulseHandover) string {
		if h.ApprovalsBackfilled == 0 {
			return ""
		}
		return fmt.Sprintf(" (%d backfilled)", h.ApprovalsBackfilled)
	}
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
	first := "first pass "
	if !wide {
		first = "1st pass "
	}
	for i, r := range []pulseRow{
		size("small", func(f data.PulseFirstPass) data.PulseRate { return f.Small }),
		size("medium", func(f data.PulseFirstPass) data.PulseRate { return f.Medium }),
		size("large", func(f data.PulseFirstPass) data.PulseRate { return f.Large }),
		size("no size stated", func(f data.PulseFirstPass) data.PulseRate { return f.Unstated }),
	} {
		lead := strings.Repeat(" ", len(first))
		if i == 0 {
			lead = first
		}
		r.label, r.short = lead+r.label, lead+r.short
		rows = append(rows, r)
	}
	rows = append(rows,
		pulseRow{"hand-over → merge p90", "hand-over → merge p90", both(p90(func(h data.PulseHandover) data.PulseP90 { return h.ToMerge }, pulseSlowHandover, none))},
		pulseRow{"  hand-over → approval p90", "  → approval p90", both(p90(func(h data.PulseHandover) data.PulseP90 { return h.ToApproval }, 1<<30, backfilled))},
		pulseRow{"  approval → merge p90", "  approval → merge p90", both(p90(func(h data.PulseHandover) data.PulseP90 { return h.ApprovalToMerge }, pulseSlowHandover, none))},
		pulseRow{"  send-backs that changed nothing", "  sent back unchanged", both(func(d data.PulseDay) pulseCol {
			h := d.Handover
			if h.SentBack == nil || h.SentBackUnchanged == nil {
				return pulseCol{val: "-"}
			}
			return pulseCol{val: fmt.Sprintf("%d of %d", *h.SentBackUnchanged, *h.SentBack), bad: *h.SentBackUnchanged > 0}
		})},
		pulseRow{"conflicted base merges / merged PR", "conflicts / merged PR", both(func(d data.PulseDay) pulseCol {
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
