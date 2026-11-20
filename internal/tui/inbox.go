package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"slices"
	"strconv"

	"charm.land/bubbles/v2/key"
	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

// inboxState is the Inbox's cursor over the decision cards, its own scroll, and the choice on the
// card under the cursor.
type inboxState struct {
	cursor  int
	top     int    // first visible page line
	choice  string // what the cursor card is answered with, as the script takes it: an option number, lead or site-manager; "" when none
	id      string // the decision the choice belongs to
	reject  bool   // the confirm cursor is on Reject
	sending bool   // the choice is being sent
}

// inboxCard is an open decision with the repository it belongs to.
type inboxCard struct {
	path string
	d    data.FactoryDecision
}

// answerMsg is the end of `factory decision answer` for a card.
type answerMsg struct {
	card   inboxCard
	choice string
	err    error
}

// cards lists the open decisions of every repository, oldest first (one without a time last).
func (m *model) cards() []inboxCard {
	var cs []inboxCard
	for _, r := range m.fac.repos {
		if r.st == nil {
			continue
		}
		for _, d := range r.st.Decisions {
			cs = append(cs, inboxCard{r.path, d})
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

// syncInbox keeps the cursor on a card and drops a choice whose card is gone.
func (m *model) syncInbox() {
	in := &m.inbox
	cs := m.cards()
	in.cursor = max(0, min(in.cursor, len(cs)-1))
	if in.choice != "" && (len(cs) == 0 || cs[in.cursor].d.ID != in.id) {
		in.choice, in.sending = "", false
	}
}

// stepCard moves the cursor one card; false when there is no card that way, and the caller scrolls.
func (m *model) stepCard(delta int) bool {
	if delta != 1 && delta != -1 {
		return false
	}
	if next := m.inbox.cursor + delta; next >= 0 && next < len(m.cards()) {
		m.inbox.cursor = next
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
	m.inbox.sending, m.inbox.choice = false, ""
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
	return append(lines, m.floor(p, w, h-len(lines), band)...), spans
}

// card is one decision: who asks and how long ago, the ask, the numbered options, then a hint of the
// two delegations, and under the card under the cursor the confirm of its choice. A card parked with the
// site manager is dim, says since when instead of the hint, and takes no choice.
func (m *model) card(p paint, c inboxCard, cur bool, w int) block {
	th, in := m.th, &m.inbox
	picked := cur && in.choice != "" && in.id == c.d.ID
	parked := c.d.With != ""
	text, muted, accent := th.Text, th.Muted, th.Purple
	if parked {
		text, muted, accent = th.Faint, th.Faint, th.Faint
	}
	mark, dot := p.sp(1), p.t(" · ", th.Faint)
	if cur {
		mark = p.b("▸", th.Purple)
	}
	kind := th.Yellow
	switch {
	case parked:
		kind = th.Faint
	case c.d.Kind == "blocked":
		kind = th.Orange
	}
	head := p.sp(1) + mark + p.sp(1) + p.b(c.d.Slug, text) + dot + p.t(c.d.Kind, kind)
	if !c.d.TS.IsZero() {
		head += dot + p.t(ago(m.now().Sub(c.d.TS)), muted)
	}
	lines := block{fit(head, w, p)}
	for _, l := range wrapPlain(c.d.Ask, max(8, w-5)) {
		lines = append(lines, fit(p.sp(3)+p.t(l, text), w, p))
	}
	if c.d.Returned != "" {
		for _, l := range wrapPlain("The site manager handed it back: "+c.d.Returned, max(8, w-5)) {
			lines = append(lines, fit(p.sp(3)+p.t(l, th.Orange), w, p))
		}
	}
	const recommended = "  (recommended)"
	for i, o := range c.d.Options {
		suffix := ""
		if i+1 == c.d.Recommend {
			suffix = p.t(recommended, muted)
		}
		o = trunc(o, max(1, w-6-width(suffix)))
		if picked && in.choice == strconv.Itoa(i+1) {
			lines = append(lines, fit(p.sp(2)+p.pill(fmt.Sprint(i+1), accent)+p.sp(1)+p.b(o, text)+suffix, w, p))
		} else {
			lines = append(lines, fit(p.sp(3)+p.b(fmt.Sprint(i+1), accent)+p.sp(2)+p.t(o, text)+suffix, w, p))
		}
	}
	if picked && !in.sending && (in.choice == "lead" || in.choice == "site-manager") {
		lines = append(lines, fit(p.sp(3)+p.b(map[string]string{"lead": "Leave it to the lead?", "site-manager": "Leave it to the site manager?"}[in.choice], text), w, p))
	}
	switch {
	case parked:
		since := ""
		if !c.d.WithSince.IsZero() {
			since = " since " + c.d.WithSince.Local().Format("15:04")
		}
		lines = append(lines, fit(p.sp(3)+p.t("With the site manager"+since, th.Faint), w, p))
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
	default:
		lines = append(lines, fit(p.sp(3)+p.t("L leave it to the lead · S to the site manager", th.Faint), w, p))
	}
	return lines
}
