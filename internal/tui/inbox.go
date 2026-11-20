package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"slices"
	"strings"
	"time"

	"charm.land/bubbles/v2/key"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"charm.land/lipgloss/v2/table"

	"isshoni/internal/data"
)

// inboxState is the Inbox's cursor over the messages, its own scroll, and the pick on the
// card under the cursor.
type inboxState struct {
	cursor  int    // index of the row at, in the displayed order
	at      string // the row's key: it follows that decision or pull request through a refresh
	top     int    // first visible page line
	pick    int    // the option picked on the cursor card, 1-based; 0 when none
	id      string // the decision the pick belongs to
	reject  bool   // the confirm cursor is on Reject
	sending bool   // the pick is being sent

	history bool // the history shows every entry the status carries, not the day's first few
}

// inboxCard is an open decision with the repository it belongs to.
type inboxCard struct {
	name string
	path string
	d    data.FactoryDecision
}

// answerMsg is the end of `factory decision answer` for a card.
type answerMsg struct {
	card inboxCard
	n    int
	err  error
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

// inboxRow is a message of the list: an open decision, or (app set) a pull request waiting for the human's approval.
type inboxRow struct {
	inboxCard
	app *data.FactoryApproval
}

// key is what holds the cursor on its row through a refresh: the decision's id, or the repository and pull request number.
func (r inboxRow) key() string {
	if r.app != nil {
		return fmt.Sprintf("%s#%d", r.path, r.app.Number)
	}
	return r.d.ID
}

// messages lists the rows in the order they are shown: per repository its decisions, then its approvals oldest first;
// the repositories with a decision first, in the order of the cards, then those with approvals only.
func (m *model) messages() []inboxRow {
	cs := m.cards()
	var paths []string
	for _, c := range cs {
		if !slices.Contains(paths, c.path) {
			paths = append(paths, c.path)
		}
	}
	for _, r := range m.fac.repos {
		if r.st != nil && len(r.st.Approvals.Items) > 0 && !slices.Contains(paths, r.path) {
			paths = append(paths, r.path)
		}
	}
	var rows []inboxRow
	for _, path := range paths {
		for _, c := range cs {
			if c.path == path {
				rows = append(rows, inboxRow{inboxCard: c})
			}
		}
		for _, r := range m.fac.repos {
			if r.path != path || r.st == nil {
				continue
			}
			apps := slices.Clone(r.st.Approvals.Items)
			slices.SortStableFunc(apps, func(a, b data.FactoryApproval) int { return a.Since.Compare(b.Since) })
			for i := range apps {
				rows = append(rows, inboxRow{inboxCard{name: r.name, path: path}, &apps[i]})
			}
		}
	}
	return rows
}

// cursorCard is the decision under the cursor; false when the cursor is on an approval or there is no row.
func (m *model) cursorCard() (inboxCard, bool) {
	rows := m.messages()
	if m.inbox.cursor >= len(rows) || rows[m.inbox.cursor].app != nil {
		return inboxCard{}, false
	}
	return rows[m.inbox.cursor].inboxCard, true
}

// syncInbox keeps the cursor on its row, or on the nearest row when that one is gone, and drops a
// pick whose decision is gone.
func (m *model) syncInbox() {
	in := &m.inbox
	rows, cs := m.messages(), m.cards()
	if i := slices.IndexFunc(rows, func(r inboxRow) bool { return r.key() == in.at }); i >= 0 {
		in.cursor = i
	} else {
		in.cursor = max(0, min(in.cursor, len(rows)-1))
		in.at = ""
		if len(rows) > 0 {
			in.at = rows[in.cursor].key()
		}
	}
	if in.pick != 0 && !slices.ContainsFunc(cs, func(c inboxCard) bool { return c.d.ID == in.id }) {
		in.pick, in.sending = 0, false
	}
}

// stepCard moves the cursor one row; false when there is no row that way, and the caller scrolls.
func (m *model) stepCard(delta int) bool {
	if delta != 1 && delta != -1 {
		return false
	}
	if rows, next := m.messages(), m.inbox.cursor+delta; next >= 0 && next < len(rows) {
		m.inbox.cursor, m.inbox.at = next, rows[next].key()
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

// pickable is the option a digit names on the card under the cursor; 0 when it names none.
func (m *model) pickable(msg tea.KeyPressMsg) int {
	t := msg.Key().Text
	c, ok := m.cursorCard()
	if !ok || len(t) != 1 || t[0] < '1' || t[0] > '9' || int(t[0]-'0') > len(c.d.Options) {
		return 0
	}
	return int(t[0] - '0')
}

func (m *model) pickOption(n int) tea.Cmd {
	if m.opts.ReadOnly {
		m.setToast("read-only: this key may not answer a decision", true)
		return nil
	}
	c, _ := m.cursorCard()
	m.inbox.id, m.inbox.pick, m.inbox.reject = c.d.ID, n, false
	m.showCard()
	return nil
}

// confirmKey handles every key while a pick waits for Accept or Reject; the caller has taken ctrl+c.
func (m *model) confirmKey(msg tea.KeyPressMsg) tea.Cmd {
	in := &m.inbox
	if in.sending {
		return nil
	}
	switch {
	case key.Matches(msg, keys.Accept):
		return m.accept()
	case key.Matches(msg, keys.Reject):
		in.pick = 0
	case key.Matches(msg, keys.Mark):
		in.reject = !in.reject
	case key.Matches(msg, keys.Confirm) && in.reject:
		in.pick = 0
	case key.Matches(msg, keys.Confirm):
		return m.accept()
	default:
		if n := m.pickable(msg); n > 0 {
			in.pick, in.reject = n, false
		}
	}
	return nil
}

// accept sends the pick as a command: the script is never run on the update loop.
func (m *model) accept() tea.Cmd {
	c, _ := m.cursorCard()
	n, ctx := m.inbox.pick, m.ctx
	m.inbox.sending = true
	return func() tea.Msg {
		return answerMsg{c, n, data.AnswerDecision(ctx, c.path, c.d.ID, n)}
	}
}

// answered ends a send: a failure shows the script's line and unpicks, a success refreshes the factory so the
// card goes when the status says so.
func (m *model) answered(msg answerMsg) tea.Cmd {
	if msg.card.d.ID == m.inbox.id { // a refresh may have moved the pick to another card
		m.inbox.sending, m.inbox.pick = false, 0
	}
	if msg.err != nil {
		m.setToast(firstLine(msg.err.Error()), true)
		return nil
	}
	m.setToast(fmt.Sprintf("answered %s: %d", msg.card.d.Slug, msg.n), false)
	if m.runner != nil {
		m.runner.Forget() // the shared runner would answer with the status from before
	}
	return m.refreshFactory(0)
}

// inboxPage is the Inbox: the list of messages beside the cursor's message, the history, then the factory's
// lines. cur is the page line of the cursor's row in the list. A task with a decision is not repeated in the band.
func (m *model) inboxPage(p paint, w, h int) (lines block, cur int) {
	cs, rows := m.cards(), m.messages()
	if !m.fac.doneAt.IsZero() {
		if len(rows) == 0 {
			lines = block{fit(p.sp(1)+p.t("Nothing to decide.", m.th.Faint), w, p), p.sp(w)}
		} else {
			lines, cur = m.panes(p, rows, w)
			lines = append(lines, p.sp(w))
		}
		lines = append(lines, m.history(p, w)...)
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

// panes is the list of messages (about 40% of the width, at least 30 columns), a rule, and the cursor
// row's message, as one table row.
func (m *model) panes(p paint, rows []inboxRow, w int) (block, int) {
	lw := min(max(30, w*2/5), w/2)
	rw := w - lw - 4 // the list's and the message's gap, the rule, one column free on the right
	list, cur := m.list(p, rows, lw)
	msg := m.message(p, rows[m.inbox.cursor], rw)
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

// list is the table of messages: repository rows, then one row per decision or approval with the cursor row marked. cur
// is the line of the cursor row, the header being line 0.
func (m *model) list(p paint, rows []inboxRow, lw int) (block, int) {
	th := m.th
	slugW, ageW := 5, 0 // the marker, a space, the icon, a space; a space follows the slug
	var ages []string
	for _, r := range rows {
		age, slug := "", r.d.Slug
		switch {
		case r.app != nil:
			age, slug = ago(m.now().Sub(r.app.Since)), r.app.Slug
		case !r.d.TS.IsZero():
			age = ago(m.now().Sub(r.d.TS))
		}
		ages = append(ages, age)
		slugW, ageW = max(slugW, 5+width(slug)), max(ageW, 1+width(age))
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
	for i, r := range rows {
		if i == 0 || r.path != rows[i-1].path {
			name := trunc(r.name, slugW-1)
			add(th.Bg, base.sp(1)+base.b(name, th.Text)+base.sp(slugW-1-width(name)), base.sp(askW), base.sp(ageW))
		}
		bg := th.Bg
		marker, icon, iconFg := base.sp(1), "?", th.Yellow
		slug, ask, askFg := r.d.Slug, r.d.Ask, th.Muted
		switch {
		case r.app != nil:
			icon, slug, ask, askFg = "◆", r.app.Slug, fmt.Sprintf("%s #%d", r.app.Why, r.app.Number), th.Orange
			if r.app.Why == "stale" {
				askFg = th.Yellow
			}
			if askW < width(ask) { // the number goes before the slug is cut
				ask = r.app.Why
			}
		case r.d.Kind == "blocked":
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
		slug = trunc(slug, slugW-5)
		add(bg, marker+q.sp(1)+q.b(icon, iconFg)+q.sp(1)+q.b(slug, th.Text)+q.sp(slugW-4-width(slug)),
			q.t(cell(ask, askW, false), askFg), q.t(cell(" "+ages[i], ageW, true), th.Muted))
	}
	t.StyleFunc(func(row, _ int) lipgloss.Style {
		if row == table.HeaderRow {
			return p.style(th.Faint, false)
		}
		return p.on(rowBg[row]).style(th.Text, false)
	})
	return strings.Split(t.String(), "\n"), cur
}

// message is the cursor row: for an approval its own message; for a decision who asks and how long ago, the
// task's own status, the ask, the numbered options, and after a pick the Accept and Reject of it.
func (m *model) message(p paint, r inboxRow, w int) block {
	if r.app != nil {
		return m.approveMessage(p, r, w)
	}
	c := r.inboxCard
	th, in := m.th, &m.inbox
	picked := in.pick != 0 && in.id == c.d.ID
	dot := p.t(" · ", th.Faint)
	kind := th.Yellow
	if c.d.Kind == "blocked" {
		kind = th.Orange
	}
	title := p.b(c.d.Slug, th.Text) + dot + p.t(c.d.Kind, kind)
	if !c.d.TS.IsZero() {
		title += dot + p.t("asked "+ago(m.now().Sub(c.d.TS))+" ago", th.Muted)
	}
	lines := block{title}
	if ctx := m.taskContext(c.name, c.path, c.d.Slug); ctx != "" {
		lines = append(lines, p.t(ctx, th.Faint))
	}
	lines = append(lines, "")
	for _, l := range wrapPlain(c.d.Ask, max(8, w)) {
		lines = append(lines, p.t(l, th.Text))
	}
	lines = append(lines, "")
	const recommended = "  (recommended)"
	for i, o := range c.d.Options {
		suffix := ""
		if i+1 == c.d.Recommend {
			suffix = p.t(recommended, th.Muted)
		}
		o = trunc(o, max(1, w-4-width(suffix)))
		if picked && in.pick == i+1 {
			lines = append(lines, p.pill(fmt.Sprint(i+1), th.Purple)+p.sp(1)+p.b(o, th.Text)+suffix)
		} else {
			lines = append(lines, p.b(fmt.Sprint(i+1), th.Purple)+p.sp(2)+p.t(o, th.Text)+suffix)
		}
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
	}
	return lines
}

// taskContext is the message's repository and whatever the task's own status row has: how long it has been
// blocked or running, its pull request and its base.
func (m *model) taskContext(name, path, slug string) string {
	parts := []string{name}
	for _, r := range m.fac.repos {
		if r.path != path || r.st == nil {
			continue
		}
		for _, t := range r.st.Rows {
			if t.Slug != slug {
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
