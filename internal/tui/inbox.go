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
	reject  bool   // the confirm cursor is on Reject
	sending bool   // the pick is being sent
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

// inboxPage is the Inbox: the decision cards, then the factory's lines. spans holds the lines each card takes.
// A task with a card is not repeated in the band.
func (m *model) inboxPage(p paint, w, h int) (lines block, spans [][2]int) {
	cs := m.cards()
	switch {
	case m.fac.doneAt.IsZero():
	case len(cs) == 0:
		lines = block{fit(p.sp(1)+p.t("Nothing to decide.", m.th.Faint), w, p), p.sp(w)}
	default:
		for i, c := range cs {
			start := len(lines)
			if i == 0 || c.path != cs[i-1].path {
				lines = append(lines, fit(p.sp(1)+p.b(c.name, m.th.Text), w, p))
			}
			lines = append(lines, m.card(p, c, i == m.inbox.cursor, w)...)
			spans = append(spans, [2]int{start, len(lines)})
			lines = append(lines, p.sp(w))
		}
	}
	items := slices.DeleteFunc(m.waiting(), func(it waitItem) bool {
		return slices.ContainsFunc(cs, func(c inboxCard) bool { return c.path == it.path && c.d.Slug == it.task.Slug })
	})
	var band block
	if len(items) > 0 || len(cs) == 0 {
		band = m.needsYou(p, w, items)
	}
	return append(lines, m.table(p, w, h-len(lines), band)...), spans
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
	kind := th.Yellow
	if c.d.Kind == "blocked" {
		kind = th.Orange
	}
	head := p.sp(1) + mark + p.sp(1) + p.b(c.d.Slug, th.Text) + dot + p.t(c.d.Kind, kind)
	if !c.d.TS.IsZero() {
		head += dot + p.t(ago(m.now().Sub(c.d.TS)), th.Muted)
	}
	lines := block{fit(head, w, p)}
	for _, l := range wrapPlain(c.d.Ask, max(8, w-5)) {
		lines = append(lines, fit(p.sp(3)+p.t(l, th.Text), w, p))
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
		lines = append(lines, fit(p.sp(3)+button("Accept", !in.reject, th.Green)+p.sp(2)+button("Reject", in.reject, th.Red), w, p))
	}
	return lines
}
