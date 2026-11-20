package tui

import (
	"fmt"
	"slices"
	"strings"
	"time"

	"isshoni/internal/data"
)

const (
	approveStale   = 10 * time.Minute // older approvals are labelled with their age
	historyRecent  = 24 * time.Hour   // the history shows entries younger than this by default
	historyDefault = 8                // and at most this many
)

// approveItem and historyItem carry the short name of the repository they come from.
type approveItem struct {
	repo string
	data.FactoryApproval
}

type historyItem struct {
	repo string
	data.FactoryHistory
}

// notices are the Approve and History sections under the cards: the pull requests waiting for the
// human's approval (given on GitHub), oldest first, and what was decided, newest first.
func (m *model) notices(p paint, w int) block {
	var apps []approveItem
	var hist []historyItem
	var asOf time.Time
	for _, r := range m.fac.repos {
		if r.st == nil {
			continue
		}
		for _, it := range r.st.Approvals.Items {
			apps = append(apps, approveItem{r.name, it})
			if asOf.IsZero() || r.st.Approvals.AsOf.Before(asOf) {
				asOf = r.st.Approvals.AsOf
			}
		}
		for _, h := range r.st.History {
			hist = append(hist, historyItem{r.name, h})
		}
	}
	slices.SortStableFunc(apps, func(a, b approveItem) int { return a.Since.Compare(b.Since) })
	slices.SortStableFunc(hist, func(a, b historyItem) int { return b.TS.Compare(a.TS) })
	return append(m.approveSection(p, w, apps, asOf), m.historySection(p, w, hist)...)
}

// manyRepos says whether the items come from more than one repository: then a column names it.
func manyRepos[T any](items []T, repo func(T) string) bool {
	return slices.ContainsFunc(items, func(it T) bool { return repo(it) != repo(items[0]) })
}

func (m *model) approveSection(p paint, w int, items []approveItem, asOf time.Time) block {
	if len(items) == 0 {
		return nil
	}
	th := m.th
	label := "Approve"
	if age := m.now().Sub(asOf); age > approveStale {
		label += " · as of " + ago(age)
	}
	lines := block{fit(p.sp(1)+p.t(label, th.Faint), w, p)}
	many := manyRepos(items, func(it approveItem) string { return it.repo })
	repoW, headW, urlW := 0, 0, 0
	ages := make([]string, len(items))
	for i, it := range items {
		ages[i] = ago(m.now().Sub(it.Since))
		repoW = max(repoW, min(nameLeast, width(it.repo)))
		headW = max(headW, width(it.Slug+" · "+it.Why+" · "+ages[i]))
		urlW = max(urlW, width(it.URL))
	}
	lead := 1
	if many {
		lead += repoW + 2
	}
	inline := lead+headW+3+urlW <= w // the URL is never cut: when it does not fit it moves under the entry
	for i, it := range items {
		why := th.Orange
		if it.Why == "stale" {
			why = th.Yellow
		}
		dot := p.t(" · ", th.Faint)
		row := p.sp(1)
		if many {
			row += p.t(cell(it.repo, repoW, false), th.Muted) + p.sp(2)
		}
		row += p.b(it.Slug, th.Text) + dot + p.t(it.Why, why) + dot + p.t(ages[i], th.Muted)
		under := lead + 2
		if inline {
			pad := headW - width(it.Slug+" · "+it.Why+" · "+ages[i]) + 3
			row += p.sp(pad) + p.t(it.URL, th.Blue)
			under = lead + headW + 3
		}
		lines = append(lines, fit(row, w, p))
		if !inline {
			lines = append(lines, fit(p.sp(under)+p.t(it.URL, th.Blue), w, p))
		}
		if it.Text != "" {
			for _, l := range wrapPlain(it.Text, max(8, w-under-1)) {
				lines = append(lines, fit(p.sp(under)+p.t(l, th.Faint), w, p))
			}
		}
	}
	return append(lines, p.sp(w))
}

func (m *model) historySection(p paint, w int, items []historyItem) block {
	if len(items) == 0 {
		return nil
	}
	th := m.th
	shown := items
	if !m.inbox.history {
		n := 0
		for n < len(items) && n < historyDefault && m.now().Sub(items[n].TS) < historyRecent {
			n++
		}
		shown = items[:n]
	}
	many := manyRepos(items, func(it historyItem) string { return it.repo })
	repoW, whenW, slugW, byW := 0, 0, 0, 0
	whens, bys := make([]string, len(shown)), make([]string, len(shown))
	for i, it := range shown {
		whens[i], bys[i] = m.when(it.TS), strings.ReplaceAll(it.By, "-", " ")
		repoW, whenW = max(repoW, min(nameLeast, width(it.repo))), max(whenW, width(whens[i]))
		slugW, byW = max(slugW, width(it.Slug)), max(byW, width(bys[i]))
	}
	textCol := 1 + whenW + 2 + slugW + 2 + byW + 2
	if many {
		textCol += repoW + 2
	}
	lines := block{fit(p.sp(1)+p.t("History", th.Faint), w, p)}
	for i, it := range shown {
		row := p.sp(1)
		if many {
			row += p.t(cell(it.repo, repoW, false), th.Muted) + p.sp(2)
		}
		row += p.t(cell(whens[i], whenW, false), th.Muted) + p.sp(2) + p.b(cell(it.Slug, slugW, false), th.Text) + p.sp(2) +
			p.t(cell(bys[i], byW, false), th.Blue) + p.sp(2)
		text := strings.Join(strings.Fields(it.Text), " ")
		lines = append(lines, fit(row+p.t(trunc(text, max(1, w-textCol-1)), th.Text), w, p))
	}
	if older := len(items) - len(shown); older > 0 {
		lines = append(lines, fit(p.sp(1)+p.t(fmt.Sprintf("… %d older · H shows them", older), th.Faint), w, p))
	}
	return append(lines, p.sp(w))
}

// when is the local time of an entry, with its date when it is not from today; "-" when the script gave none.
func (m *model) when(t time.Time) string {
	switch {
	case t.IsZero():
		return "-"
	case t.Local().YearDay() == m.now().Local().YearDay() && t.Local().Year() == m.now().Local().Year():
		return t.Local().Format("15:04")
	}
	return t.Local().Format("Jan 2 15:04")
}
