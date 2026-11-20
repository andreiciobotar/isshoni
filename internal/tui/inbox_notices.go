package tui

import (
	"fmt"
	"slices"
	"strings"
	"time"

	"charm.land/lipgloss/v2"
	"charm.land/lipgloss/v2/table"

	"isshoni/internal/data"
)

const (
	approveStale   = 10 * time.Minute // older approvals are labelled with their age
	historyRecent  = 24 * time.Hour   // the history shows entries younger than this by default
	historyDefault = 8                // and at most this many
	historyWhat    = 40               // cells WHAT keeps before TIME drops its date
)

// approveMessage is the right pane on a pull request waiting for the human's approval, which is given on GitHub.
func (m *model) approveMessage(p paint, r inboxRow, w int) block {
	th, a := m.th, r.app
	dot := p.t(" · ", th.Faint)
	when, text := "handed over "+ago(m.now().Sub(a.Since))+" ago", a.Text
	if a.Why == "stale" {
		when = "approval stale " + ago(m.now().Sub(a.Since)) + " ago"
	} else {
		text = "New: handed over, checks green."
	}
	title := p.b(a.Slug, th.Text) + dot + p.t("approve", th.Yellow) + dot + p.t(when, th.Muted)
	for _, repo := range m.fac.repos {
		if repo.path != r.path || repo.st == nil {
			continue
		}
		switch asOf := repo.st.Approvals.AsOf; {
		case asOf.IsZero():
			title += dot + p.t("as of unknown", th.Faint)
		case m.now().Sub(asOf) > approveStale:
			title += dot + p.t("as of "+ago(m.now().Sub(asOf)), th.Faint)
		}
	}
	lines := block{title}
	if ctx := m.taskContext(r.name, r.path, a.Slug); ctx != "" {
		lines = append(lines, p.t(ctx, th.Faint))
	}
	lines = append(lines, "")
	for _, l := range wrapPlain(text, w) {
		lines = append(lines, p.t(l, th.Text))
	}
	lines = append(lines, "")
	for _, l := range wrapPlain(a.URL, w) { // the URL is never cut: a pane narrower than it wraps it
		lines = append(lines, p.t(l, th.Blue))
	}
	lines = append(lines, "")
	for _, l := range wrapPlain("Approve on GitHub: an approving review, or a comment reading lgtm.", w) {
		lines = append(lines, p.t(l, th.Faint))
	}
	return lines
}

// historyEntry is a history line with the repository it comes from.
type historyEntry struct {
	repo string
	data.FactoryHistory
}

// history is the table of what was decided and by whom, across repositories, newest first: the entries of the
// last day up to historyDefault, or all of them after H. The last line counts what is left out.
func (m *model) history(p paint, w int) block {
	var all []historyEntry
	repos := 0
	for _, r := range m.fac.repos {
		if r.st != nil && len(r.st.History) > 0 {
			repos++
		}
		if r.st == nil {
			continue
		}
		for _, h := range r.st.History {
			all = append(all, historyEntry{r.name, h})
		}
	}
	if len(all) == 0 {
		return nil
	}
	slices.SortStableFunc(all, func(a, b historyEntry) int { return b.TS.Compare(a.TS) })
	shown := all
	if !m.inbox.history {
		n := 0
		for n < len(all) && n < historyDefault && m.now().Sub(all[n].TS) < historyRecent {
			n++
		}
		shown = all[:n]
	}
	th := m.th
	lines := block{fit(p.sp(1)+p.t("History", th.Faint), w, p)}
	if len(shown) > 0 {
		lines = append(lines, m.historyTable(p, w, shown, repos > 1)...)
	}
	if older := len(all) - len(shown); older > 0 {
		lines = append(lines, fit(p.sp(1)+p.t(fmt.Sprintf("… %d older · H shows them", older), th.Faint), w, p))
	}
	return append(lines, p.sp(w))
}

// historyTable is the library table of the entries with TIME, REPO (when withRepo), TASK, BY and WHAT; no row is marked.
func (m *model) historyTable(p paint, w int, shown []historyEntry, withRepo bool) block {
	th := m.th
	timeW, repoW, taskW, byW := 4, 0, 4, 2
	for _, e := range shown {
		timeW, repoW = max(timeW, width(m.when(e.TS, true))), max(repoW, min(nameLeast, width(e.repo)))
		taskW, byW = max(taskW, min(20, width(e.Slug))), max(byW, min(14, width(strings.ReplaceAll(e.By, "-", " "))))
	}
	fixed := func() int {
		n := 1 + timeW + 2 + taskW + 2 + byW + 2
		if withRepo {
			n += max(repoW, 4) + 2
		}
		return n
	}
	dated := w-fixed()-1 >= historyWhat
	if !dated { // a narrow window keeps the room for WHAT: the time alone
		timeW = 5
	}
	whatW := max(1, w-fixed()-1)
	var head []string
	head = append(head, p.t(cell(" TIME", 1+timeW, false), th.Faint))
	if withRepo {
		head = append(head, p.t(cell("REPO", max(repoW, 4), false), th.Faint))
	}
	head = append(head, p.t(cell("TASK", taskW, false), th.Faint), p.t(cell("BY", byW, false), th.Faint), p.t("WHAT", th.Faint))
	t := table.New().Border(lipgloss.NormalBorder()).
		BorderTop(false).BorderBottom(false).BorderLeft(false).BorderRight(false).BorderHeader(false).BorderColumn(false).
		Wrap(true).Headers(head...)
	for _, e := range shown {
		row := []string{p.t(cell(" "+m.when(e.TS, dated), 1+timeW, false), th.Muted)}
		if withRepo {
			row = append(row, p.t(cell(e.repo, max(repoW, 4), false), th.Muted))
		}
		t.Row(append(row, p.t(cell(e.Slug, taskW, false), th.Text),
			p.t(cell(strings.ReplaceAll(e.By, "-", " "), byW, false), th.Blue),
			p.t(trunc(strings.Join(strings.Fields(e.Text), " "), whatW), th.Text))...)
	}
	last := len(head) - 1
	t.StyleFunc(func(_, c int) lipgloss.Style {
		s := p.style(th.Text, false)
		if c < last {
			return s.PaddingRight(2)
		}
		return s
	})
	return strings.Split(t.String(), "\n")
}

// when is the local time of an entry, with its date when it is not from today and dated is set; "-" when the script gave none.
func (m *model) when(t time.Time, dated bool) string {
	switch {
	case t.IsZero():
		return "-"
	case !dated || t.Local().YearDay() == m.now().Local().YearDay() && t.Local().Year() == m.now().Local().Year():
		return t.Local().Format("15:04")
	}
	return t.Local().Format("Jan 2 15:04")
}
