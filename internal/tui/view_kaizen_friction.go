package tui

import (
	"fmt"
	"image/color"
	"strconv"
)

// What the review of the factory found worth a warning colour on the Friction page.
const (
	frictionDearCall     = 0.15   // dispatcher cost per call, USD
	frictionLongContext  = 400000 // dispatcher median context, tokens
	frictionManyRework   = 1.0 / 3
	frictionTopCause     = 1.0 / 3 // of the stated defects
	frictionManyRefusals = 0.05    // refused share of a role's Bash calls
	frictionRows         = 8       // rows of a list before "+N more"
)

// fseg is a piece of a row: its text and the colour it is shown in.
type fseg struct {
	s   string
	col color.Color
}

// frictionLines is the page: one section a row or a short table each, "-" for what is unknown.
func (m *model) frictionLines(p paint, w int) block {
	th := m.th
	f := m.kz.doc.Friction
	if f == nil {
		return block{"", p.sp(1) + p.t("Nothing measured yet: this producer writes no friction figures.", th.Muted)}
	}
	wide := w >= 100
	lw := 21
	if wide {
		lw = 32
	}
	label := func(long, short string) string {
		if wide {
			return long
		}
		return short
	}
	// row is a label and its pieces, two cells apart.
	row := func(label string, segs ...fseg) string {
		line := p.sp(1) + p.b(cell(label, lw-1, false), th.Text)
		for i, s := range segs {
			if i > 0 {
				if width(line)+2+width(s.s) > w { // the trailing detail goes, it does not get cut in half
					break
				}
				line += p.sp(2)
			}
			line += p.t(s.s, s.col)
		}
		return fit(line, w, p)
	}
	// table lays out columns under a header; the label shows on the header line only.
	table := func(label string, cols []int, head []string, rows [][]string, bad func(r, c int) bool, more int) block {
		put := func(label string, cells []string, col func(c int) color.Color) string {
			line := p.sp(1) + p.b(cell(label, lw-1, false), th.Text)
			for c, s := range cells {
				line += p.t(cell(s, cols[c], false), col(c)) + p.sp(2)
			}
			return fit(line, w, p)
		}
		out := block{put(label, head, func(int) color.Color { return th.Faint })}
		for r, cells := range rows {
			out = append(out, put("", cells, func(c int) color.Color {
				if bad != nil && bad(r, c) {
					return th.Orange
				}
				return th.Text
			}))
		}
		if more > 0 {
			out = append(out, p.sp(lw)+p.t(fmt.Sprintf("+%d more", more), th.Faint))
		}
		return out
	}
	hue := func(bad bool) color.Color {
		if bad {
			return th.Orange
		}
		return th.Text
	}
	dim := func(s string) fseg { return fseg{s, th.Muted} }
	none := dim("none recorded")
	money := func(x float64, unpriced bool) string {
		if unpriced {
			return m.kzMoney(x) + "*"
		}
		return m.kzMoney(x)
	}
	opt := func(x *float64, f func(float64) string) string {
		if x == nil {
			return "-"
		}
		return f(*x)
	}
	prefix := func(repo string) string {
		if len(m.kz.doc.Repos) > 1 {
			return repo + "/"
		}
		return ""
	}
	cut := func(n int) (shown, more int) { return min(n, frictionRows), max(0, n-frictionRows) }

	var lines block
	lines = append(lines, "")

	d := f.Dispatcher
	perCall := opt(d.PerCall, func(x float64) string { return money(x, d.UnpricedTokens > 0) }) + " / call"
	lines = append(lines, row("DISPATCHER",
		fseg{perCall, hue(d.PerCall != nil && *d.PerCall > frictionDearCall)},
		dim(fmt.Sprintf("n=%d calls", d.Calls)),
		fseg{"median context " + opt(d.ContextMedian, kzTokens), hue(d.ContextMedian != nil && *d.ContextMedian > frictionLongContext)}))

	r := f.Rework
	subagents := "-"
	if s := m.kz.doc.Summary.ReworkShare; s != nil {
		subagents = pct(*s * 100)
	}
	lines = append(lines, row(label("COST AFTER THE FIRST REVIEW", "AFTER 1ST REVIEW"),
		fseg{opt(r.Share, func(x float64) string { return pct(x * 100) }) + " of task cost", hue(r.Share != nil && *r.Share > frictionManyRework)},
		dim(m.kzMoney(r.Cost)+" of "+m.kzMoney(r.TaskCost)),
		dim("median "+opt(r.PerTaskMedian, m.kzMoney)+fmt.Sprintf(" / task  n=%d", r.N)),
		fseg{"(subagents only: " + subagents + ")", th.Faint}))

	c := f.Causes
	stated := c.Spec + c.Brief + c.Conflict + c.Scope + c.Code
	cause := func(name string, n int) fseg {
		if stated == 0 || n == 0 {
			return fseg{name + " " + strconv.Itoa(n), th.Text}
		}
		share := float64(n) / float64(stated)
		return fseg{fmt.Sprintf("%s %s %d", name, pct(share*100), n), hue(share > frictionTopCause)}
	}
	causes := []fseg{cause("spec", c.Spec), cause("brief", c.Brief), cause("conflict", c.Conflict), cause("scope", c.Scope), cause("code", c.Code)}
	tail := dim(fmt.Sprintf("n=%d stated · %d unstated", stated, c.Unstated))
	if wide {
		lines = append(lines, row("DEFECT CAUSES", causes[0], causes[1], causes[2], causes[3], causes[4], tail))
	} else {
		lines = append(lines, row("DEFECT CAUSES", causes[0], causes[1], causes[2], causes[3], causes[4]), row("", tail))
	}

	rf := f.Refusals
	if len(rf.Roles) == 0 {
		lines = append(lines, row(label("REFUSED BASH CALLS", "REFUSED BASH"), none))
	} else {
		var rows [][]string
		for _, x := range rf.Roles {
			share := opt(x.Share, func(s float64) string { return fmt.Sprintf("%.1f%%", s*100) })
			rows = append(rows, []string{x.Role, share + fmt.Sprintf(" %d/%d", x.Guard+x.Classifier, x.Bash), strconv.Itoa(x.Guard), strconv.Itoa(x.Classifier), m.kzMoney(x.AfterCost)})
		}
		cols, next := []int{12, 13, 5, 10, 6}, fmt.Sprintf("next %d", rf.AfterCalls)
		if wide {
			cols, next = []int{12, 16, 5, 10, 14}, next+" calls"
		}
		lines = append(lines, table(label("REFUSED BASH CALLS", "REFUSED BASH"), cols,
			[]string{"role", "refused", "guard", "classifier", next}, rows,
			func(r, c int) bool { s := rf.Roles[r].Share; return c == 1 && s != nil && *s > frictionManyRefusals }, 0)...)
	}

	if len(rf.Roles) > 0 {
		lines = append(lines, "")
	}

	wk := f.Wakeups
	if wk.Prompts == 0 {
		lines = append(lines, row(label("LEAD PROMPTS AFTER HAND-OVER", "LEAD PROMPTS"), none))
	} else {
		lines = append(lines, row(label("LEAD PROMPTS AFTER HAND-OVER", "LEAD PROMPTS"),
			fseg{fmt.Sprintf("%d in %d tasks", wk.Prompts, wk.Tasks), th.Text},
			dim(m.kzMoney(wk.Cost)+" after them"),
			dim(opt(wk.PerPrompt, m.kzMoney)+" / prompt")))
	}

	if q := f.Queue; q == nil {
		lines = append(lines, row(label("QUEUE ATTEMPTS / MERGED PR", "QUEUE / MERGED PR"), dim("no queue log yet")))
	} else {
		lines = append(lines, row(label("QUEUE ATTEMPTS / MERGED PR", "QUEUE / MERGED PR"),
			fseg{opt(q.PerMerged, func(x float64) string { return fmt.Sprintf("%.2f", x) }) + fmt.Sprintf("  n=%d", q.Merged), th.Text},
			dim(fmt.Sprintf("updates %d · re-runs %d · send-backs %d · stops %d", q.Updated, q.Rerun, q.SentBack, q.Stopped)),
			fseg{"(queue log since " + kzTime(q.Since) + ")", th.Faint}))
	}
	lines = append(lines, "")

	if len(f.Held) == 0 {
		lines = append(lines, row(label("QUEUE SEND-BACKS AND PAUSES", "SEND-BACKS, PAUSES"), none))
	} else {
		shown, more := cut(len(f.Held))
		var rows [][]string
		for _, h := range f.Held[:shown] {
			paused := "-"
			if h.Prioritized > 0 {
				paused = fmt.Sprintf("%dm", h.PausedMinutes)
			}
			rows = append(rows, []string{prefix(h.Repo) + h.Slug, strconv.Itoa(h.SentBack), strconv.Itoa(h.Paused), paused})
		}
		lines = append(lines, table(label("QUEUE SEND-BACKS AND PAUSES", "SEND-BACKS, PAUSES"), []int{max(8, min(34, w-lw-32)), 10, 7, 7},
			[]string{"task", "send-backs", "pauses", "paused"}, rows, nil, more)...)
	}
	lines = append(lines, "")

	lc := f.LateConflicts
	logCell := func(n *int) string {
		if n == nil {
			return "-"
		}
		return strconv.Itoa(*n)
	}
	var rows [][]string
	shown, more := cut(len(lc.Tasks))
	for i, t := range lc.Tasks[:shown] {
		by := ""
		if i == 0 {
			by = "by task"
		}
		rows = append(rows, []string{by, prefix(t.Repo) + t.Slug, logCell(t.Log), strconv.Itoa(t.Git)})
	}
	if len(lc.Tasks) == 0 {
		rows = append(rows, []string{"by task", "none recorded", "", ""})
	}
	if more > 0 {
		rows = append(rows, []string{"", fmt.Sprintf("+%d more", more), "", ""})
	}
	shown, more = cut(len(lc.Files))
	for i, x := range lc.Files[:shown] {
		by := ""
		if i == 0 {
			by = "by file"
		}
		rows = append(rows, []string{by, prefix(x.Repo) + x.File, logCell(x.Log), strconv.Itoa(x.Git)})
	}
	if len(lc.Files) == 0 {
		rows = append(rows, []string{"by file", "none recorded", "", ""})
	}
	if more > 0 {
		rows = append(rows, []string{"", fmt.Sprintf("+%d more", more), "", ""})
	}
	nw := 8
	for _, r := range rows {
		nw = max(nw, width(r[1]))
	}
	nw = min(nw, max(8, w-lw-47))
	logs := "no queue log yet"
	if lc.LogTasks != nil {
		logs = plural(*lc.LogTasks, "task") + " in the log"
	}
	lines = append(lines, table("LATE CONFLICTS", []int{8, nw, 9, 22},
		[]string{"", "task / file", "queue log", "git history (backfill)"}, rows, nil, 0)...)
	lines = append(lines, p.sp(lw)+p.t(fmt.Sprintf("%s · %d of %d PRs in git history (backfill)", logs, lc.GitTasks, lc.GitPRs), th.Faint))
	lines = append(lines, "")

	is := m.kz.doc.Outbox.Issues
	triaged := is.Accepted + is.Rejected + is.Done
	lines = append(lines, row("OUTBOX", fseg{fmt.Sprintf("%d triaged of %d issues · %d open", triaged, triaged+is.Open, is.Open), th.Text}))
	return append(lines, "")
}

// kzTokens is a token count as 310k.
func kzTokens(x float64) string {
	if x >= 1000 {
		return fmt.Sprintf("%.0fk", x/1000)
	}
	return strconv.Itoa(int(x))
}
