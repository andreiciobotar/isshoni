package tui

import (
	"fmt"
	"image/color"
	"path/filepath"
	"strings"
	"time"

	"github.com/charmbracelet/x/ansi"

	"isshoni/internal/data"
)

// opsBody is the Ops tab: one block per watch, scrolled when taller than the window.
func (m *model) opsBody(p paint, w, h int) block {
	return scrolled(p, m.opsLines(p, w, h), m.ops.top, w, h)
}

func (m *model) opsLines(p paint, w, h int) block {
	th, o := m.th, &m.ops
	switch {
	case o.doneAt.IsZero():
		msg := p.t(spin(m.spinFrame())+" ", th.Purple) + p.t("asking the factory…", th.Muted)
		return p.center(block{msg}, w, h)
	case len(o.watches) == 0 && o.err != "":
		return p.center(block{p.t("⚠ "+trunc(o.err, w-6), th.Red)}, w, h)
	case len(o.watches) == 0:
		msg := block{p.b("No ops watch yet", th.Text), ""}
		for _, l := range wrapPlain(`Start one from your own terminal: factory ops start <name> "<goal>" --every 10m --until 6h --budget 5`, min(w-8, 60)) {
			msg = append(msg, p.t(l, th.Muted))
		}
		return p.center(msg, w, h)
	}
	var lines block
	if o.err != "" {
		lines = block{p.sp(1) + p.t("⚠ "+trunc(o.err, w-4), th.Red), ""}
	}
	for i, wt := range o.watches {
		if i > 0 {
			lines = append(lines, "")
		}
		lines = append(lines, m.opsBlock(p, wt, w)...)
	}
	return lines
}

// opsSubheader is the tab's title, the count of watches that need a look from the human and the age of the data.
func (m *model) opsSubheader(p paint) string {
	th, o := m.th, &m.ops
	out := p.b("Ops", th.Text)
	if n := len(o.watches); !o.doneAt.IsZero() && (o.err == "" || n > 0) {
		out += p.t("  "+opsCount(n, "watch", "watches"), th.Muted)
		var alarmed, broke int
		for _, wt := range o.watches {
			switch wt.State {
			case "alarmed":
				alarmed++
			case "out-of-budget":
				broke++
			}
		}
		if alarmed > 0 {
			out += p.t(" · ", th.Faint) + p.b(fmt.Sprintf("%d alarmed", alarmed), th.Red)
		}
		if broke > 0 {
			out += p.t(" · ", th.Faint) + p.b(fmt.Sprintf("%d out of budget", broke), th.Yellow)
		}
	}
	switch {
	case o.busy:
		out += p.t(" · "+spin(m.spinFrame())+" reading", th.Blue)
	case o.err != "" && len(o.watches) > 0:
		out += p.t(" · stale", th.Yellow)
	case o.err == "" && !o.doneAt.IsZero():
		out += p.t(" · updated "+ago(m.now().Sub(o.doneAt))+" ago", th.Faint)
	}
	return out
}

func opsCount(n int, one, many string) string {
	if n == 1 {
		return "1 " + one
	}
	return fmt.Sprintf("%d %s", n, many)
}

// opsSpan is a span to the minute, to the second under a minute.
func opsSpan(d time.Duration) string {
	if d < time.Minute {
		return FmtDuration(d)
	}
	return limitSpan(int64(d.Seconds()))
}

// opsIn says when an ISO time is: "<label> in 4m" ahead of now, "<label> overdue 3m" behind it, "<label> <raw>" when it does not parse.
func opsIn(label, raw string, now time.Time) string {
	t, err := time.Parse(time.RFC3339, raw)
	switch {
	case err != nil:
		return label + " " + raw
	case t.After(now):
		return label + " in " + opsSpan(t.Sub(now))
	}
	return label + " overdue " + opsSpan(now.Sub(t))
}

// opsLook is the state word of a watch and its colour; a state this dashboard does not know is shown as it came.
func (m *model) opsLook(wt data.OpsWatch) (string, color.Color) {
	th := m.th
	switch wt.State {
	case "watching":
		return wt.State, th.Green
	case "alarmed":
		return wt.State, th.Red
	case "stopped":
		return wt.State, th.Faint
	case "out-of-budget":
		return "out of budget", th.Yellow
	case "ended":
		if wt.Verdict != nil {
			return "ended: " + strings.ReplaceAll(*wt.Verdict, "-", " "), th.Muted
		}
		return wt.State, th.Muted
	}
	return wt.State, th.Muted
}

// opsBlock is one watch: its state and schedule, goal, last look and spend.
func (m *model) opsBlock(p paint, wt data.OpsWatch, w int) block {
	th, now := m.th, m.now()
	label := func(s string) string { return p.sp(3) + p.t(cell(s, 7, false), th.Muted) }
	// side puts right at the end of the line; left gives way to it.
	side := func(left, right string) string {
		left = trunc(left, max(1, w-width(right)-3))
		return left + p.sp(max(2, w-1-width(left)-width(right))) + right
	}
	dot := p.t(" · ", th.Faint)
	looks := func(n int) string { return opsCount(n, "look", "looks") }

	word, col := m.opsLook(wt)
	head := p.sp(1) + p.t("●", col) + p.sp(1) + p.b(wt.Name, th.Text) + p.sp(2) + p.t(word, col) + p.t(" · "+filepath.Base(wt.Repo), th.Muted)
	every := "every " + opsSpan(time.Duration(wt.EverySeconds)*time.Second)
	if wt.NextLook != nil {
		every = opsIn("next look", *wt.NextLook, now) + " · " + every + " · " + opsIn("ends", wt.Until, now)
	}
	lines := block{side(head, p.t(every, th.Muted))}

	for i, l := range wrapCapped(wt.Goal, w-11, 2) {
		lead := p.sp(10)
		if i == 0 {
			lead = label("goal")
		}
		lines = append(lines, lead+p.t(l, th.Text))
	}

	counts := p.t(looks(wt.Ticks), th.Muted)
	if wt.Missed > 0 {
		counts += dot + p.t(looks(wt.Missed)+" missed", th.Yellow)
	}
	last := label("last") + p.t("no look yet", th.Faint)
	if l := wt.LastLook; l != nil {
		when, outcome, outCol := l.TS, l.Outcome, th.Text
		if t, err := time.Parse(time.RFC3339, l.TS); err == nil {
			when = ago(now.Sub(t)) + " ago"
		}
		switch l.Outcome {
		case "alarm":
			outCol = th.Red
		case "could-not-look":
			outcome, outCol = "could not look", th.Yellow
		case "fine":
			outCol = th.Green
		}
		last = label("last") + p.t(when, th.Text) + dot + p.t(outcome, outCol)
		late, lateW := "", 0
		if l.LateSeconds > 0 {
			late = opsSpan(time.Duration(l.LateSeconds)*time.Second) + " late"
			lateW = 3 + width(late)
		}
		if room := w - 1 - width(counts) - 2 - width(last) - 3 - lateW; l.Saw != "" && room > 0 {
			last += dot + p.t(trunc(l.Saw, room), th.Text)
		}
		if late != "" {
			last += dot + p.t(late, th.Yellow)
		}
	}
	lines = append(lines, side(last, counts))

	spent := Money(wt.SpentUSD, m.prices.Currency)
	if wt.SpentUSD == 0 && wt.UnpricedTicks > 0 {
		spent = fmt.Sprintf("unpriced (%s)", looks(wt.UnpricedTicks))
	}
	spend := label("spend") + p.t(spent+" of "+Money(wt.BudgetUSD, m.prices.Currency), th.Text)
	if wt.SpentUSD != 0 && wt.UnpricedTicks > 0 {
		spend += dot + p.t(looks(wt.UnpricedTicks)+" unpriced", th.Yellow)
	}
	notes := ""
	if room := w - 1 - width(spend) - 2 - len("notes "); wt.Notes != "" && room > 0 {
		path := wt.Notes
		if width(path) > room {
			path = ansi.TruncateLeft(path, width(path)-room+1, "…")
		}
		notes = p.t("notes ", th.Muted) + p.t(path, th.Text)
	}
	return append(lines, side(spend, notes))
}
