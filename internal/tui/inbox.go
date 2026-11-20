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
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"charm.land/lipgloss/v2/table"

	"isshoni/internal/data"
)

// inboxState is the Inbox's cursor over the decisions, its own scroll, and the choice on the
// card under the cursor.
type inboxState struct {
	cursor  int    // index of the card at, in the displayed order
	at      string // the decision the cursor is on: it follows that decision through a refresh
	top     int    // first visible page line
	choice  string // what the cursor card is answered with, as the script takes it: an option number, lead or site-manager; "" when none
	id      string // the decision the choice belongs to
	reject  bool   // the confirm cursor is on Reject
	sending bool   // the choice is being sent
}

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
	cs := m.cards()
	if m.inbox.cursor >= len(cs) {
		return inboxCard{}, false
	}
	return cs[m.inbox.cursor], true
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

// syncInbox keeps the cursor on its decision, or on the nearest card when that one is gone, and drops a
// choice whose decision is gone or parked.
func (m *model) syncInbox() {
	in := &m.inbox
	cs := m.cards()
	if i := slices.IndexFunc(cs, func(c inboxCard) bool { return c.d.ID == in.at }); i >= 0 {
		in.cursor = i
	} else {
		in.cursor = max(0, min(in.cursor, len(cs)-1))
		in.at = ""
		if len(cs) > 0 {
			in.at = cs[in.cursor].d.ID
		}
	}
	if in.choice != "" && !slices.ContainsFunc(cs, func(c inboxCard) bool { return c.d.ID == in.id && c.d.With == "" }) {
		in.choice, in.sending = "", false
	}
}

// stepCard moves the cursor one card; false when there is no card that way, and the caller scrolls.
func (m *model) stepCard(delta int) bool {
	if delta != 1 && delta != -1 {
		return false
	}
	if cs, next := m.cards(), m.inbox.cursor+delta; next >= 0 && next < len(cs) {
		m.inbox.cursor, m.inbox.at = next, cs[next].d.ID
		m.showCard()
		return true
	}
	return false
}

// showCard scrolls the page to the cursor's row of the list, leaving the last line to the "more lines" note.
func (m *model) showCard() {
	h := m.h - 3
	_, cur := m.inboxPage(m.th.on(m.th.Bg), m.w, h)
	top := &m.inbox.top
	if cur+1 > *top+h-1 {
		*top = cur + 2 - h
	}
	*top = max(0, min(*top, max(0, cur-2))) // two lines up keep the repository row in view
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
	m.inbox.id, m.inbox.choice, m.inbox.reject = c.d.ID, choice, false
	m.showCard()
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
		in.choice = ""
	case key.Matches(msg, keys.Mark):
		in.reject = !in.reject
	case key.Matches(msg, keys.Confirm) && in.reject:
		in.choice = ""
	case key.Matches(msg, keys.Confirm):
		return m.accept()
	default:
		if choice := m.choiceOf(msg); choice != "" {
			in.choice, in.reject = choice, false
		}
	}
	return nil
}

// accept sends the choice as a command: the script is never run on the update loop.
func (m *model) accept() tea.Cmd {
	c, _ := m.cursorCard()
	choice, ctx := m.inbox.choice, m.ctx
	m.inbox.sending = true
	return func() tea.Msg {
		return answerMsg{c, choice, data.AnswerDecision(ctx, c.path, c.d.ID, choice)}
	}
}

// answered ends a send: a failure shows the script's line and unpicks, a success refreshes the factory so the
// card goes (or parks) when the status says so.
func (m *model) answered(msg answerMsg) tea.Cmd {
	if msg.card.d.ID == m.inbox.id { // a refresh may have moved the choice to another card
		m.inbox.sending, m.inbox.choice = false, ""
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
// cur is the page line of the cursor's row in the list. A task with a decision is not repeated in the band.
func (m *model) inboxPage(p paint, w, h int) (lines block, cur int) {
	cs := m.cards()
	switch {
	case m.fac.doneAt.IsZero():
	case len(cs) == 0:
		lines = block{fit(p.sp(1)+p.t("Nothing to decide.", m.th.Faint), w, p), p.sp(w)}
	default:
		lines, cur = m.panes(p, cs, w)
		lines = append(lines, p.sp(w))
	}
	items := slices.DeleteFunc(m.waiting(), func(it waitItem) bool {
		return slices.ContainsFunc(cs, func(c inboxCard) bool { return c.path == it.path && c.d.Slug == it.task.Slug })
	})
	var band block
	if len(items) > 0 || len(cs) == 0 {
		band = m.needsYou(p, w, items)
	}
	return append(lines, m.floor(p, w, h-len(lines), band)...), cur
}

// panes is the list of decisions (about 40% of the width, at least 30 columns), a rule, and the cursor
// decision's message, as one table row.
func (m *model) panes(p paint, cs []inboxCard, w int) (block, int) {
	lw := min(max(30, w*2/5), w/2)
	rw := w - lw - 4 // the list's and the message's gap, the rule, one column free on the right
	list, cur := m.list(p, cs, lw)
	msg := m.message(p, cs[m.inbox.cursor], rw)
	cellOf := func(b block, cw int) string {
		for i := range b {
			b[i] = fit(b[i], cw, p)
		}
		return strings.Join(b, "\n")
	}
	t := table.New().
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
	return strings.Split(t.String(), "\n"), cur
}

// list is the table of open decisions: repository rows, then one row per decision with the cursor row marked. cur
// is the line of the cursor row, the header being line 0.
func (m *model) list(p paint, cs []inboxCard, lw int) (block, int) {
	th := m.th
	slugW, ageW := 5, 0 // the marker, a space, the icon, a space; a space follows the slug
	var ages []string
	for _, c := range cs {
		age := ""
		if !c.d.TS.IsZero() {
			age = ago(m.now().Sub(c.d.TS))
		}
		ages = append(ages, age)
		slugW, ageW = max(slugW, 5+width(c.d.Slug)), max(ageW, 1+width(age))
	}
	slugW = min(slugW, lw-ageW)
	askW := lw - slugW - ageW
	showAsk := askW >= 4
	if !showAsk {
		askW = 0
		slugW = lw - ageW
	}
	var rowBg []color.Color
	t := table.New().Border(lipgloss.NormalBorder()).
		BorderTop(false).BorderBottom(false).BorderLeft(false).BorderRight(false).BorderHeader(false).BorderColumn(false).
		Wrap(true)
	add := func(bg color.Color, a, ask, age string) {
		row := []string{a}
		if showAsk {
			row = append(row, ask)
		}
		t.Row(append(row, age)...)
		rowBg = append(rowBg, bg)
	}
	base := p.on(th.Bg)
	head := []string{base.t(cell("    TASK", slugW, false), th.Faint)}
	if showAsk {
		head = append(head, base.t(cell("ASK", askW, false), th.Faint))
	}
	t.Headers(append(head, base.t(cell("AGE", ageW, true), th.Faint))...)
	cur := 0
	for i, c := range cs {
		if i == 0 || c.path != cs[i-1].path {
			name := trunc(c.name, slugW-1)
			add(th.Bg, base.sp(1)+base.b(name, th.Text)+base.sp(slugW-1-width(name)), base.sp(askW), base.sp(ageW))
		}
		bg := th.Bg
		marker, icon, iconFg := base.sp(1), "?", th.Yellow
		text, muted := th.Text, th.Muted
		switch {
		case c.d.With != "":
			icon, iconFg, text, muted = "…", th.Faint, th.Faint, th.Faint
		case c.d.Kind == "blocked":
			icon, iconFg = "▲", th.Orange
		}
		if i == m.inbox.cursor {
			bg, cur = th.Raised, len(rowBg)+1
			marker = base.on(bg).b("▌", th.Purple)
			if th.NoColor {
				marker = ">"
			}
		}
		q := p.on(bg)
		slug := trunc(c.d.Slug, slugW-5)
		add(bg, marker+q.sp(1)+q.b(icon, iconFg)+q.sp(1)+q.b(slug, text)+q.sp(slugW-4-width(slug)),
			q.t(cell(c.d.Ask, askW, false), muted), q.t(cell(" "+ages[i], ageW, true), muted))
	}
	t.StyleFunc(func(row, _ int) lipgloss.Style {
		if row == table.HeaderRow {
			return p.style(th.Faint, false)
		}
		return p.on(rowBg[row]).style(th.Text, false)
	})
	return strings.Split(t.String(), "\n"), cur
}

// message is the cursor decision: who asks and how long ago, the task's own status, the ask, the numbered
// options with the hint of the two delegations, and after a choice its confirm. A decision parked with the site
// manager says since when instead of the options and the hint; one handed back says why under the ask.
func (m *model) message(p paint, c inboxCard, w int) block {
	th, in := m.th, &m.inbox
	picked := in.choice != "" && in.id == c.d.ID
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
		if picked && in.choice == strconv.Itoa(i+1) {
			lines = append(lines, p.pill(fmt.Sprint(i+1), accent)+p.sp(1)+p.b(o, text)+suffix)
		} else {
			lines = append(lines, p.b(fmt.Sprint(i+1), accent)+p.sp(2)+p.t(o, text)+suffix)
		}
	}
	if picked && !in.sending && (in.choice == "lead" || in.choice == "site-manager") {
		lines = append(lines, "", p.b(map[string]string{"lead": "Leave it to the lead?", "site-manager": "Leave it to the site manager?"}[in.choice], text))
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
		lines = append(lines, "", p.t("L leave it to the lead · S to the site manager", th.Faint))
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
