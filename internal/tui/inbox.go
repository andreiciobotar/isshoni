package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"slices"
	"strings"
	"time"

	"charm.land/bubbles/v2/key"
	"charm.land/bubbles/v2/table"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	lgtable "charm.land/lipgloss/v2/table"

	"isshoni/internal/data"
)

// inboxState is the Inbox's list of messages (its table holds the one cursor, a row per message in the
// displayed order), its own scroll, and the pick on the decision under the cursor.
type inboxState struct {
	tbl     table.Model
	at      string // the key of the row the cursor is on: it follows that decision or pull request through a refresh
	top     int    // first visible page line
	pick    int    // the option picked on the cursor card, 1-based; 0 when none
	id      string // the decision the pick belongs to
	mark    int    // the confirm button marked: 0 Accept, then Always when offered, last Reject
	sending bool   // the pick is being sent
	always  bool   // the Always line is in pick mode: a digit switches a standing answer off

	history bool // the history shows every entry the status carries, not the day's first few
}

func newInboxState() inboxState { return inboxState{tbl: table.New()} }

// inboxCard is a message of the list: an open decision, or (app set) a pull request waiting for the human's
// approval, with the repository it belongs to.
type inboxCard struct {
	name string
	path string
	d    data.FactoryDecision
	app  *data.FactoryApproval
}

// key is what holds the cursor on its row through a refresh: the decision's id, or the repository and pull
// request number.
func (c inboxCard) key() string {
	if c.app != nil {
		return fmt.Sprintf("%s#%d", c.path, c.app.Number)
	}
	return c.d.ID
}

// slug is the task the message is about.
func (c inboxCard) slug() string {
	if c.app != nil {
		return c.app.Slug
	}
	return c.d.Slug
}

// answerMsg is the end of `factory decision answer` for a card.
type answerMsg struct {
	card   inboxCard
	n      int
	always bool
	err    error
}

// cards lists the messages in the order they are shown: repositories by their oldest decision, each
// repository's decisions oldest first (one without a time last) and then its approvals oldest first; the
// repositories with approvals only come last.
func (m *model) cards() []inboxCard {
	var cs []inboxCard
	for _, r := range m.fac.repos {
		if r.st == nil {
			continue
		}
		for _, d := range r.st.Decisions {
			cs = append(cs, inboxCard{name: r.name, path: r.path, d: d})
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
	for _, r := range m.fac.repos {
		if r.st == nil || len(r.st.Approvals.Items) == 0 {
			continue
		}
		at := len(cs) // after the repository's decisions, or at the end for one without
		for i, c := range cs {
			if c.path == r.path {
				at = i + 1
			}
		}
		var as []inboxCard
		for i := range r.st.Approvals.Items {
			as = append(as, inboxCard{name: r.name, path: r.path, app: &r.st.Approvals.Items[i]})
		}
		cs = slices.Insert(cs, at, as...)
	}
	return cs
}

// cursorCard is the decision under the cursor; false when the cursor is on an approval or there is no row.
func (m *model) cursorCard() (inboxCard, bool) {
	cs, i := m.cards(), m.inbox.tbl.Cursor()
	if i < 0 || i >= len(cs) || cs[i].app != nil {
		return inboxCard{}, false
	}
	return cs[i], true
}

// syncInbox keeps the cursor on its row, or on the nearest row when that one is gone, and drops a
// pick whose decision is gone. It sizes the table to the messages; the page fills the rows in.
func (m *model) syncInbox() {
	in := &m.inbox
	cs := m.cards()
	i := slices.IndexFunc(cs, func(c inboxCard) bool { return c.key() == in.at })
	if i < 0 {
		i = max(0, min(in.tbl.Cursor(), len(cs)-1))
	}
	in.tbl.SetRows(make([]table.Row, len(cs)))
	in.tbl.SetCursor(0) // MoveDown from the top sets the scroll offset; SetCursor alone leaves it
	in.tbl.MoveDown(i)
	in.at = ""
	if len(cs) > 0 {
		in.at = cs[i].key()
	}
	if in.pick != 0 && !slices.ContainsFunc(cs, func(c inboxCard) bool { return c.app == nil && c.d.ID == in.id }) {
		in.pick, in.sending = 0, false
	}
	if len(m.switchable()) == 0 {
		in.always = false
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
	m.inbox.at = m.cards()[next].key()
	m.inbox.top = 0 // the list and the message are at the top of the page
	return true
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
	m.inbox.id, m.inbox.pick, m.inbox.mark = c.d.ID, n, 0
	m.inbox.top = 0
	return nil
}

// alwaysOffered is whether the pick on the card under the cursor is the option that may stand as an answer.
func (m *model) alwaysOffered() bool {
	c, _ := m.cursorCard()
	return c.d.Always != 0 && c.d.Always == m.inbox.pick
}

// buttons is how many the confirm line has: Accept and Reject, with Always between when it is offered.
func (m *model) buttons() int {
	if m.alwaysOffered() {
		return 3
	}
	return 2
}

// confirmKey handles every key while a pick waits for Accept, Always or Reject; the caller has taken ctrl+c.
func (m *model) confirmKey(msg tea.KeyPressMsg) tea.Cmd {
	in := &m.inbox
	if in.sending {
		return nil
	}
	buttons := m.buttons()
	switch {
	case key.Matches(msg, keys.Accept):
		return m.accept(false)
	case key.Matches(msg, keys.Always) && buttons == 3:
		return m.accept(true)
	case key.Matches(msg, keys.Reject):
		in.pick = 0
	case key.Matches(msg, keys.Mark):
		step := 1
		if k := msg.String(); k == "left" || k == "h" {
			step = buttons - 1
		}
		in.mark = (in.mark + step) % buttons
	case key.Matches(msg, keys.Confirm) && in.mark == buttons-1:
		in.pick = 0
	case key.Matches(msg, keys.Confirm):
		return m.accept(in.mark == 1 && buttons == 3)
	default:
		if n := m.pickable(msg); n > 0 {
			in.pick, in.mark = n, 0
		}
	}
	return nil
}

// accept sends the pick as a command, as the standing answer of its kind when always: the script is never run
// on the update loop.
func (m *model) accept(always bool) tea.Cmd {
	c, _ := m.cursorCard()
	n, ctx := m.inbox.pick, m.ctx
	m.inbox.sending = true
	return func() tea.Msg {
		return answerMsg{c, n, always, data.AnswerDecision(ctx, c.path, c.d.ID, n, always)}
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
	toast := fmt.Sprintf("answered %s: %d", msg.card.d.Slug, msg.n)
	if msg.always {
		toast += " · always " + m.alwaysLabel(msg.card.d.Kind) + " from now"
	}
	m.setToast(toast, false)
	if m.runner != nil {
		m.runner.Forget() // the shared runner would answer with the status from before
	}
	return m.refreshFactory(0)
}

// inboxPage is the Inbox: the list of messages beside the cursor's message, the history, then the factory's
// lines. A task with a decision is not repeated in the band, nor is the pull request of an approve row.
func (m *model) inboxPage(p paint, w, h int) (lines block) {
	cs := m.cards()
	if !m.fac.doneAt.IsZero() {
		if len(cs) == 0 {
			lines = append(block{fit(p.sp(1)+p.t("Nothing to decide.", m.th.Faint), w, p)}, m.alwaysLine(p, w)...)
		} else {
			lines = append(m.panes(p, cs, w), m.alwaysLine(p, w)...)
		}
		lines = append(lines, m.history(p, w)...)
	}
	items := slices.DeleteFunc(m.waiting(), func(it waitItem) bool {
		return slices.ContainsFunc(cs, func(c inboxCard) bool {
			what, _ := it.todo()
			return c.d.Picked == 0 && c.path == it.path && it.task.Slug == c.slug() && (c.app == nil || what == "review PR")
		})
	})
	var band block
	if len(items) > 0 || len(cs) == 0 {
		band = m.needsYou(p, w, items)
	}
	return append(lines, m.floor(p, w, h-len(lines), band)...)
}

// panes is the list of messages (about 40% of the width, at least 30 columns), a rule, and the cursor
// message, as one table row.
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

// list is the bubbles table of messages: REPO (named on the first row of its group), the kind's icon,
// TASK, ASK and AGE. It shows eight rows and scrolls inside itself. The cursor row is plain text, so the
// selected style paints all of it; the other rows carry their own colours.
func (m *model) list(p paint, cs []inboxCard, lw int) block {
	const iconW = 4 // the marker, a space, the icon, a space
	th, t := m.th, &m.inbox.tbl
	var ages []string
	repoW, slugW, ageW := 0, 0, 0
	for _, c := range cs {
		ts := c.d.TS
		if c.app != nil {
			ts = c.app.Since
		}
		age := ""
		if !ts.IsZero() {
			age = ago(m.now().Sub(ts))
		}
		ages = append(ages, age)
		repoW, slugW, ageW = max(repoW, 1+width(c.name)), max(slugW, 1+width(c.slug())), max(ageW, 1+width(age))
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
		ask, askFg := c.d.Ask, th.Muted
		switch {
		case c.app != nil:
			icon, iconFg, ask, askFg = "◆", th.Orange, fmt.Sprintf("%s #%d", c.app.Why, c.app.Number), th.Orange
			if c.app.Why == "stale" {
				askFg = th.Yellow
			}
			if askW-1 < width(ask) { // put keeps the last column as the gap; the number goes before the slug is cut
				ask = c.app.Why
			}
		case c.d.Kind == "blocked":
			icon, iconFg = "▲", th.Orange
		case c.d.Kind == "split": // not a blocker
			icon, iconFg = "⑂", th.Faint
		}
		lead := p.sp(2) + p.b(icon, iconFg) + p.sp(1)
		if on {
			mark := "▌"
			if th.NoColor {
				mark = ">"
			}
			lead = mark + " " + icon + " "
		}
		rows[i] = table.Row{put(repo, repoW, false, th.Text, true), lead, put(c.slug(), slugW, false, th.Text, true),
			put(ask, askW, false, askFg, false), put(ages[i], ageW, true, th.Muted, false)}
	}
	t.SetRows(rows)
	return strings.Split(t.View(), "\n")
}

// message is the cursor row: for an approval its own message; for a decision who asks and how long ago, the task's own status, the ask, the numbered
// options, and after a pick the Accept and Reject of it.
func (m *model) message(p paint, c inboxCard, w int) block {
	if c.app != nil {
		return m.approveMessage(p, c, w)
	}
	th, in := m.th, &m.inbox
	picked := in.pick != 0 && in.id == c.d.ID
	dot := p.t(" · ", th.Faint)
	kind, label := th.Yellow, c.d.Kind
	switch c.d.Kind {
	case "blocked":
		kind = th.Orange
	case "split": // not a blocker
		kind, label = th.Faint, "⑂ split"
	}
	title := p.b(c.d.Slug, th.Text) + dot + p.t(label, kind)
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
	if c.d.Note != "" {
		for _, l := range wrapPlain(c.d.Note, max(8, w)) {
			lines = append(lines, p.t(l, th.Faint))
		}
	}
	lines = append(lines, "")
	const recommended = "  (recommended)"
	for i, o := range c.d.Options {
		suffix := ""
		if i+1 == c.d.Recommend {
			suffix = p.t(recommended, th.Muted)
		}
		o = trunc(o, max(1, w-4-width(suffix)))
		if (picked && in.pick == i+1) || (!picked && c.d.Picked == i+1) {
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
		row := button("Accept", in.mark == 0, th.Green) + p.sp(2)
		if m.alwaysOffered() {
			row += button("Always", in.mark == 1, th.Blue) + p.sp(2)
		}
		lines = append(lines, "", row+button("Reject", in.mark == m.buttons()-1, th.Red))
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
