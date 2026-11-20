package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"slices"

	"charm.land/bubbles/v2/key"
	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

// inboxState is the Inbox's cursor over the decision cards, its own scroll, and the pick on the
// card under the cursor.
type inboxState struct {
	cursor  int    // index of the card at, in the displayed order
	at      string // the decision the cursor is on: it follows that decision through a refresh
	top     int    // first visible page line
	pick    int    // the option picked on the cursor card, 1-based; 0 when none
	id      string // the decision the pick belongs to
	mark    int    // the confirm button marked: 0 Accept, then Always when offered, last Reject
	sending bool   // the pick is being sent
	always  bool   // the Always line is in pick mode: a digit switches a standing answer off

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
	card   inboxCard
	n      int
	always bool
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

// inboxRepos are the repositories with a group: those with cards in the order of the cards, then those with
// only approvals or history.
func (m *model) inboxRepos(cs []inboxCard) []factoryRepo {
	var out []factoryRepo
	for _, c := range cs {
		if len(out) == 0 || out[len(out)-1].path != c.path {
			out = append(out, m.fac.repos[slices.IndexFunc(m.fac.repos, func(r factoryRepo) bool { return r.path == c.path })])
		}
	}
	for _, r := range m.fac.repos {
		if r.st != nil && (len(r.st.Approvals.Items) > 0 || len(r.st.History) > 0) && !slices.ContainsFunc(out, func(o factoryRepo) bool { return o.path == r.path }) {
			out = append(out, r)
		}
	}
	return out
}

func (m *model) cursorCard() (inboxCard, bool) {
	cs := m.cards()
	if m.inbox.cursor >= len(cs) {
		return inboxCard{}, false
	}
	return cs[m.inbox.cursor], true
}

// syncInbox keeps the cursor on its decision, or on the nearest card when that one is gone, and drops a
// pick whose decision is gone.
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
	if in.pick != 0 && !slices.ContainsFunc(cs, func(c inboxCard) bool { return c.d.ID == in.id }) {
		in.pick, in.sending = 0, false
	}
	if len(m.switchable()) == 0 {
		in.always = false
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

// showCard scrolls the page to the cursor card, leaving the last row to the "more lines" note.
func (m *model) showCard() {
	h := m.h - 3
	_, spans := m.inboxPage(m.th.on(m.th.Bg), m.w, h)
	if m.inbox.cursor >= len(spans) {
		return
	}
	s, top := spans[m.inbox.cursor], &m.inbox.top
	if s[1] > *top+h-1 {
		*top = s[1] - h + 1
	}
	*top = max(0, min(*top, s[0]))
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
	m.showCard()
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

// inboxPage is the Inbox: a group per repository with its decision cards, Approve and History, then the factory's lines. spans holds the lines each card takes.
// A task with a card is not repeated in the band.
func (m *model) inboxPage(p paint, w, h int) (lines block, spans [][2]int) {
	cs := m.cards()
	if !m.fac.doneAt.IsZero() {
		lines = m.alwaysLine(p, w)
		if len(cs) == 0 {
			lines = append(lines, fit(p.sp(1)+p.t("Nothing to decide.", m.th.Faint), w, p), p.sp(w))
		}
		i := 0
		for _, r := range m.inboxRepos(cs) {
			lines = append(lines, fit(p.sp(1)+p.b(r.name, m.th.Text), w, p))
			for ; i < len(cs) && cs[i].path == r.path; i++ {
				start := len(lines)
				lines = append(lines, m.card(p, cs[i], i == m.inbox.cursor, w)...)
				spans = append(spans, [2]int{start, len(lines)})
				lines = append(lines, p.sp(w))
			}
			lines = append(lines, m.notices(p, w, r)...)
		}
	}
	items := slices.DeleteFunc(m.waiting(), func(it waitItem) bool {
		return slices.ContainsFunc(cs, func(c inboxCard) bool { return c.d.Picked == 0 && c.path == it.path && c.d.Slug == it.task.Slug })
	})
	var band block
	if len(items) > 0 || len(cs) == 0 {
		band = m.needsYou(p, w, items)
	}
	return append(lines, m.floor(p, w, h-len(lines), band)...), spans
}

// card is one decision: who asks and how long ago, the ask, the numbered options, and under the
// card under the cursor the Accept and Reject of its pick.
func (m *model) card(p paint, c inboxCard, cur bool, w int) block {
	th, in := m.th, &m.inbox
	picked := cur && in.pick != 0 && in.id == c.d.ID
	mark, dot := p.sp(1), p.t(" · ", th.Faint)
	if cur {
		mark = p.b("▸", th.Purple)
	}
	kind, label := th.Yellow, c.d.Kind
	switch c.d.Kind {
	case "blocked":
		kind = th.Orange
	case "split": // not a blocker
		kind, label = th.Faint, "⑂ split"
	}
	head := p.sp(1) + mark + p.sp(1) + p.b(c.d.Slug, th.Text) + dot + p.t(label, kind)
	if !c.d.TS.IsZero() {
		head += dot + p.t(ago(m.now().Sub(c.d.TS)), th.Muted)
	}
	lines := block{fit(head, w, p)}
	for _, l := range wrapPlain(c.d.Ask, max(8, w-5)) {
		lines = append(lines, fit(p.sp(3)+p.t(l, th.Text), w, p))
	}
	if c.d.Note != "" {
		for _, l := range wrapPlain(c.d.Note, max(8, w-5)) {
			lines = append(lines, fit(p.sp(3)+p.t(l, th.Faint), w, p))
		}
	}
	const recommended = "  (recommended)"
	for i, o := range c.d.Options {
		suffix := ""
		if i+1 == c.d.Recommend {
			suffix = p.t(recommended, th.Muted)
		}
		o = trunc(o, max(1, w-6-width(suffix)))
		if picked && in.pick == i+1 {
			lines = append(lines, fit(p.sp(2)+p.pill(fmt.Sprint(i+1), th.Purple)+p.sp(1)+p.b(o, th.Text)+suffix, w, p))
		} else {
			lines = append(lines, fit(p.sp(3)+p.b(fmt.Sprint(i+1), th.Purple)+p.sp(2)+p.t(o, th.Text)+suffix, w, p))
		}
	}
	switch {
	case picked && in.sending:
		lines = append(lines, fit(p.sp(3)+p.t(spin(m.spinFrame())+" sending…", th.Blue), w, p))
	case picked:
		button := func(label string, on bool, col color.Color) string {
			if on {
				return p.pill(label, col)
			}
			return p.t(" "+label+" ", th.Muted)
		}
		row := p.sp(3) + button("Accept", in.mark == 0, th.Green) + p.sp(2)
		if m.alwaysOffered() {
			row += button("Always", in.mark == 1, th.Blue) + p.sp(2)
		}
		lines = append(lines, fit(row+button("Reject", in.mark == m.buttons()-1, th.Red), w, p))
	}
	return lines
}
