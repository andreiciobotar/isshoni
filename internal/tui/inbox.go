package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"slices"
	"strconv"
	"strings"
	"time"

	"charm.land/bubbles/v2/key"
	"charm.land/bubbles/v2/table"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	lgtable "charm.land/lipgloss/v2/table"

	"isshoni/internal/data"
)

// inboxState is the Inbox's list of decisions (its table holds the one cursor, a row per decision in the
// displayed order), its own scroll, and the pick on the decision under the cursor.
type inboxState struct {
	tbl     table.Model
	at      string // the decision the cursor is on: it follows that decision through a refresh
	top     int    // first visible page line
	pick    string // what the cursor card is answered with, as the script takes it: an option number, lead or site-manager; "" when none
	id      string // the decision the pick belongs to
	reject  bool   // the confirm cursor is on Reject
	sending bool   // the pick is being sent
}

func newInboxState() inboxState { return inboxState{tbl: table.New()} }

// inboxCard is an open decision with the repository it belongs to.
type inboxCard struct {
	name string
	path string
	d    data.FactoryDecision
}

// answerMsg is the end of `factory decision answer` for a card.
type answerMsg struct {
	card   inboxCard
	choice string
	err    error
}

// cards lists the open decisions in the order they are shown: repositories by their oldest decision, each
// repository's decisions oldest first (one without a time last).
func (m *model) cards() []inboxCard {
	var cs []inboxCard
	for _, r := range m.fac.repos {
		if r.st == nil {
			continue
		}
		for _, d := range r.st.Decisions {
			cs = append(cs, inboxCard{r.name, r.path, d})
		}
	}
	unknown := func(c inboxCard) int {
		if c.d.TS.IsZero() {
			return 1
		}
		return 0
	}
	slices.SortStableFunc(cs, func(a, b inboxCard) int {
		return cmp.Or(cmp.Compare(unknown(a), unknown(b)), a.d.TS.Compare(b.d.TS), cmp.Compare(a.path, b.path), cmp.Compare(a.d.ID, b.d.ID))
	})
	var order []string // the repositories by their oldest decision
	for _, c := range cs {
		if !slices.Contains(order, c.path) {
			order = append(order, c.path)
		}
	}
	slices.SortStableFunc(cs, func(a, b inboxCard) int {
		return cmp.Compare(slices.Index(order, a.path), slices.Index(order, b.path))
	})
	return cs
}

func (m *model) cursorCard() (inboxCard, bool) {
	cs, i := m.cards(), m.inbox.tbl.Cursor()
	if i < 0 || i >= len(cs) {
		return inboxCard{}, false
	}
	return cs[i], true
}

// waitingOnYou is the waiting tasks minus those whose every decision is parked with the site manager.
func (m *model) waitingOnYou() int {
	cs := m.cards()
	n := 0
	for _, it := range m.waiting() {
		mine := func(c inboxCard) bool { return c.path == it.path && c.d.Slug == it.task.Slug }
		if !slices.ContainsFunc(cs, mine) || slices.ContainsFunc(cs, func(c inboxCard) bool { return mine(c) && c.d.With == "" }) {
			n++
		}
	}
	return n
}

// syncInbox keeps the cursor on its decision, or on the nearest row when that one is gone, and drops a
// pick whose decision is gone or parked. It sizes the table to the decisions; the page fills the rows in.
func (m *model) syncInbox() {
	in := &m.inbox
	cs := m.cards()
	i := slices.IndexFunc(cs, func(c inboxCard) bool { return c.d.ID == in.at })
	if i < 0 {
		i = max(0, min(in.tbl.Cursor(), len(cs)-1))
	}
	in.tbl.SetRows(make([]table.Row, len(cs)))
	in.tbl.SetCursor(0) // MoveDown from the top sets the scroll offset; SetCursor alone leaves it
	in.tbl.MoveDown(i)
	in.at = ""
	if len(cs) > 0 {
		in.at = cs[i].d.ID
	}
	if in.pick != "" && !slices.ContainsFunc(cs, func(c inboxCard) bool { return c.d.ID == in.id && c.d.With == "" }) {
		in.pick, in.sending = "", false
	}
}

// stepCard moves the cursor one row; false when there is no row that way, and the caller scrolls the page.
func (m *model) stepCard(delta int) bool {
	t := &m.inbox.tbl
	next := t.Cursor() + delta
	if (delta != 1 && delta != -1) || next < 0 || next >= len(t.Rows()) {
		return false
	}
	if delta > 0 {
		t.MoveDown(1)
	} else {
		t.MoveUp(1)
	}
	m.inbox.at = m.cards()[next].d.ID
	m.inbox.top = 0 // the list and the message are at the top of the page
	return true
}

// choiceOf is what a key answers the card under the cursor with: the option a digit names, lead for L, site-manager
// for S; "" when it names none, and always on a parked card.
func (m *model) choiceOf(msg tea.KeyPressMsg) string {
	t := msg.Key().Text
	c, ok := m.cursorCard()
	switch {
	case !ok || c.d.With != "":
	case t == "L":
		return "lead"
	case t == "S":
		return "site-manager"
	case len(t) == 1 && t[0] >= '1' && t[0] <= '9' && int(t[0]-'0') <= len(c.d.Options):
		return t
	}
	return ""
}

func (m *model) pickChoice(choice string) tea.Cmd {
	if m.opts.ReadOnly {
		m.setToast("read-only: this key may not answer a decision", true)
		return nil
	}
	c, _ := m.cursorCard()
	m.inbox.id, m.inbox.pick, m.inbox.reject = c.d.ID, choice, false
	m.inbox.top = 0
	return nil
}

// confirmKey handles every key while a choice waits for Accept or Reject; the caller has taken ctrl+c.
func (m *model) confirmKey(msg tea.KeyPressMsg) tea.Cmd {
	in := &m.inbox
	if in.sending {
		return nil
	}
	switch {
	case key.Matches(msg, keys.Accept):
		return m.accept()
	case key.Matches(msg, keys.Reject):
		in.pick = ""
	case key.Matches(msg, keys.Mark):
		in.reject = !in.reject
	case key.Matches(msg, keys.Confirm) && in.reject:
		in.pick = ""
	case key.Matches(msg, keys.Confirm):
		return m.accept()
	default:
		if choice := m.choiceOf(msg); choice != "" {
			in.pick, in.reject = choice, false
		}
	}
	return nil
}

// accept sends the choice as a command: the script is never run on the update loop.
func (m *model) accept() tea.Cmd {
	c, _ := m.cursorCard()
	choice, ctx := m.inbox.pick, m.ctx
	m.inbox.sending = true
	return func() tea.Msg {
		return answerMsg{c, choice, data.AnswerDecision(ctx, c.path, c.d.ID, choice)}
	}
}

// answered ends a send: a failure shows the script's line and unpicks, a success refreshes the factory so the
// card goes (or parks) when the status says so.
func (m *model) answered(msg answerMsg) tea.Cmd {
	if msg.card.d.ID == m.inbox.id { // a refresh may have moved the choice to another card
		m.inbox.sending, m.inbox.pick = false, ""
	}
	if msg.err != nil {
		m.setToast(firstLine(msg.err.Error()), true)
		return nil
	}
	m.setToast(fmt.Sprintf("answered %s: %s", msg.card.d.Slug, msg.choice), false)
	if m.runner != nil {
		m.runner.Forget() // the shared runner would answer with the status from before
	}
	return m.refreshFactory(0)
}

// inboxPage is the Inbox: the list of open decisions beside the cursor's message, then the factory's lines.
// A task with a decision is not repeated in the band.
func (m *model) inboxPage(p paint, w, h int) (lines block) {
	cs := m.cards()
	switch {
	case m.fac.doneAt.IsZero():
	case len(cs) == 0:
		lines = block{fit(p.sp(1)+p.t("Nothing to decide.", m.th.Faint), w, p), p.sp(w)}
	default:
		lines = append(m.panes(p, cs, w), p.sp(w))
	}
	items := slices.DeleteFunc(m.waiting(), func(it waitItem) bool {
		return slices.ContainsFunc(cs, func(c inboxCard) bool { return c.path == it.path && c.d.Slug == it.task.Slug })
	})
	var band block
	if len(items) > 0 || len(cs) == 0 {
		band = m.needsYou(p, w, items)
	}
	return append(lines, m.floor(p, w, h-len(lines), band)...)
}

// panes is the list of decisions (about 40% of the width, at least 30 columns), a rule, and the cursor
// decision's message, as one table row.
func (m *model) panes(p paint, cs []inboxCard, w int) block {
	lw := min(max(30, w*2/5), w/2)
	rw := w - lw - 4 // the list's and the message's gap, the rule, one column free on the right
	list := m.list(p, cs, lw)
	msg := m.message(p, cs[m.inbox.tbl.Cursor()], rw)
	cellOf := func(b block, cw int) string {
		for i := range b {
			b[i] = fit(b[i], cw, p)
		}
		return strings.Join(b, "\n")
	}
	t := lgtable.New().
		Border(lipgloss.NormalBorder()).BorderStyle(p.style(m.th.Border, false)).
		BorderTop(false).BorderBottom(false).BorderLeft(false).BorderRight(false).BorderHeader(false).
		Wrap(true).
		StyleFunc(func(_, c int) lipgloss.Style {
			s := p.style(m.th.Text, false)
			if c == 0 {
				return s.PaddingRight(1)
			}
			return s.PaddingLeft(1)
		}).
		Row(cellOf(list, lw), cellOf(msg, rw))
	return strings.Split(t.String(), "\n")
}

// list is the bubbles table of open decisions: REPO (named on the first row of its group), the kind's icon,
// TASK, ASK and AGE. It shows eight rows and scrolls inside itself. The cursor row is plain text, so the
// selected style paints all of it; the other rows carry their own colours.
func (m *model) list(p paint, cs []inboxCard, lw int) block {
	const iconW = 4 // the marker, a space, the icon, a space
	th, t := m.th, &m.inbox.tbl
	var ages []string
	repoW, slugW, ageW := 0, 0, 0
	for _, c := range cs {
		age := ""
		if !c.d.TS.IsZero() {
			age = ago(m.now().Sub(c.d.TS))
		}
		ages = append(ages, age)
		repoW, slugW, ageW = max(repoW, 1+width(c.name)), max(slugW, 1+width(c.d.Slug)), max(ageW, 1+width(age))
	}
	repoW = min(repoW, max(6, lw/4))
	rest := lw - repoW - iconW - ageW
	slugW = min(slugW, rest)
	askW := rest - slugW
	if askW < 4 { // the ask goes before the slug is cut
		slugW, askW = rest, 0
	}
	t.SetColumns([]table.Column{{Title: "REPO", Width: repoW}, {Width: iconW}, {Title: "TASK", Width: slugW}, {Title: "ASK", Width: askW}, {Title: cell("AGE", ageW, true), Width: ageW}})
	t.SetStyles(table.Styles{Header: p.style(th.Faint, false), Cell: lipgloss.NewStyle(), Selected: p.on(th.Raised).style(th.Text, true)})
	t.SetWidth(lw)
	t.SetHeight(min(len(cs), 8) + 1)
	rows := make([]table.Row, len(cs))
	for i, c := range cs {
		on := i == t.Cursor()
		put := func(s string, w int, right bool, fg color.Color, bold bool) string {
			s = cell(trunc(s, w-1), w, right) // the cell's last column is the gap
			switch {
			case on:
				return s
			case bold:
				return p.b(s, fg)
			}
			return p.t(s, fg)
		}
		repo := ""
		if i == 0 || c.path != cs[i-1].path {
			repo = c.name
		}
		icon, iconFg := "?", th.Yellow
		text, muted := th.Text, th.Muted
		switch {
		case c.d.With != "":
			icon, iconFg, text, muted = "…", th.Faint, th.Faint, th.Faint
		case c.d.Kind == "blocked":
			icon, iconFg = "▲", th.Orange
		}
		lead := p.sp(2) + p.b(icon, iconFg) + p.sp(1)
		if on {
			mark := "▌"
			if th.NoColor {
				mark = ">"
			}
			lead = mark + " " + icon + " "
		}
		rows[i] = table.Row{put(repo, repoW, false, text, true), lead, put(c.d.Slug, slugW, false, text, true),
			put(c.d.Ask, askW, false, muted, false), put(ages[i], ageW, true, muted, false)}
	}
	t.SetRows(rows)
	return strings.Split(t.View(), "\n")
}

// message is the cursor decision: who asks and how long ago, the task's own status, the ask, the numbered
// options with the hint of the two delegations, and after a choice its confirm. A decision parked with the site
// manager says since when instead of the options and the hint; one handed back says why under the ask.
func (m *model) message(p paint, c inboxCard, w int) block {
	th, in := m.th, &m.inbox
	picked := in.pick != "" && in.id == c.d.ID
	parked := c.d.With != ""
	text, muted, accent := th.Text, th.Muted, th.Purple
	if parked {
		text, muted, accent = th.Faint, th.Faint, th.Faint
	}
	dot := p.t(" · ", th.Faint)
	kind := th.Yellow
	switch {
	case parked:
		kind = th.Faint
	case c.d.Kind == "blocked":
		kind = th.Orange
	}
	title := p.b(c.d.Slug, text) + dot + p.t(c.d.Kind, kind)
	if !c.d.TS.IsZero() {
		title += dot + p.t("asked "+ago(m.now().Sub(c.d.TS))+" ago", muted)
	}
	lines := block{title}
	if ctx := m.taskContext(c); ctx != "" {
		lines = append(lines, p.t(ctx, th.Faint))
	}
	lines = append(lines, "")
	for _, l := range wrapPlain(c.d.Ask, max(8, w)) {
		lines = append(lines, p.t(l, text))
	}
	if c.d.Returned != "" {
		lines = append(lines, "")
		for _, l := range wrapPlain("The site manager handed it back: "+c.d.Returned, max(8, w)) {
			lines = append(lines, p.t(l, th.Orange))
		}
	}
	lines = append(lines, "")
	if parked {
		since := ""
		if !c.d.WithSince.IsZero() {
			since = " since " + c.d.WithSince.Local().Format("15:04")
		}
		return append(lines, p.t("With the site manager"+since, th.Faint))
	}
	const recommended = "  (recommended)"
	for i, o := range c.d.Options {
		suffix := ""
		if i+1 == c.d.Recommend {
			suffix = p.t(recommended, muted)
		}
		o = trunc(o, max(1, w-4-width(suffix)))
		if picked && in.pick == strconv.Itoa(i+1) {
			lines = append(lines, p.pill(fmt.Sprint(i+1), accent)+p.sp(1)+p.b(o, text)+suffix)
		} else {
			lines = append(lines, p.b(fmt.Sprint(i+1), accent)+p.sp(2)+p.t(o, text)+suffix)
		}
	}
	if picked && !in.sending && (in.pick == "lead" || in.pick == "site-manager") {
		lines = append(lines, "", p.b(map[string]string{"lead": "Leave it to the lead?", "site-manager": "Leave it to the site manager?"}[in.pick], text))
	}
	switch {
	case picked && in.sending:
		lines = append(lines, "", p.t(spin(m.spinFrame())+" sending…", th.Blue))
	case picked:
		button := func(label string, on bool, col color.Color) string {
			if on {
				return p.pill(label, col)
			}
			return p.t(" "+label+" ", th.Muted)
		}
		lines = append(lines, "", button("Accept", !in.reject, th.Green)+p.sp(2)+button("Reject", in.reject, th.Red))
	default:
		lines = append(lines, "")
		for _, l := range wrapPlain("L leave it to the lead · S to the site manager", max(8, w)) {
			lines = append(lines, p.t(l, th.Faint))
		}
	}
	return lines
}

// taskContext is the decision's repository and whatever the task's own status row has: how long it has been
// blocked or running, its pull request and its base.
func (m *model) taskContext(c inboxCard) string {
	parts := []string{c.name}
	for _, r := range m.fac.repos {
		if r.path != c.path || r.st == nil {
			continue
		}
		for _, t := range r.st.Rows {
			if t.Slug != c.d.Slug {
				continue
			}
			switch {
			case t.StopKind != "" && t.Waited != nil:
				parts = append(parts, "blocked "+FmtDuration(time.Duration(*t.Waited)*time.Second))
			case t.Elapsed != nil:
				parts = append(parts, "running "+FmtDuration(time.Duration(*t.Elapsed)*time.Second))
			}
			if pr := prText(t); pr != "-" {
				parts = append(parts, "PR "+pr)
			}
			if t.Base != "" {
				b, _ := baseText(t.Base)
				parts = append(parts, "base "+b)
			}
		}
	}
	return strings.Join(parts, " · ")
}
