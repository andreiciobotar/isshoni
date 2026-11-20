package tui

import (
	"fmt"
	"slices"

	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

// alwaysCol is where the kinds of the Always line start, after its label.
const alwaysCol = noticeIndent + len("Always") + 3

// standingMsg is the end of `factory decision always <kind> off`.
type standingMsg struct {
	kind string
	err  error
}

// standingAnswers are the standing answers of the first repository whose status has them, with that
// repository's root: they are global, so any one status says the same.
func (m *model) standingAnswers() (path string, list []data.FactoryStanding) {
	for _, r := range m.fac.repos {
		if r.st != nil && len(r.st.Standing) > 0 {
			return r.path, r.st.Standing
		}
	}
	return "", nil
}

// switchable are the standing answers that are on: the ones A can switch off.
func (m *model) switchable() []data.FactoryStanding {
	_, list := m.standingAnswers()
	return slices.DeleteFunc(slices.Clone(list), func(s data.FactoryStanding) bool { return !s.On })
}

func (m *model) alwaysLabel(kind string) string {
	_, list := m.standingAnswers()
	if i := slices.IndexFunc(list, func(s data.FactoryStanding) bool { return s.Kind == kind }); i >= 0 {
		return list[i].Label
	}
	return kind
}

func times(n int) string {
	if n == 1 {
		return "1 time"
	}
	return fmt.Sprintf("%d times", n)
}

// alwaysLine is the section line under the sub-header: each kind that is on or has switched itself off, with
// its option and its uses. In pick mode the kinds that are on carry a number, and the hint goes on a line of
// its own when it does not fit beside them.
func (m *model) alwaysLine(p paint, w int) block {
	_, list := m.standingAnswers()
	th, pick := m.th, m.inbox.always
	gap := 5
	if pick {
		gap = 3
	}
	row, n, num := p.sp(noticeIndent)+p.t("Always", th.Faint)+p.sp(3), alwaysCol, 0
	for _, s := range list {
		if !s.On && !s.Lapsed {
			continue
		}
		if n > alwaysCol {
			row, n = row+p.sp(gap), n+gap
		}
		if s.Lapsed {
			text := fmt.Sprintf("%s → off after %s", s.Kind, times(s.Limit))
			row, n = row+p.t(text, th.Orange), n+width(text)
			continue
		}
		if pick {
			num++
			row, n = row+p.b(fmt.Sprint(num), th.Purple)+p.sp(1), n+2
		}
		text := " → " + s.Label + " · " + times(s.Uses)
		if s.Limit > 0 {
			text = fmt.Sprintf(" → %s · %d of %d", s.Label, s.Uses, s.Limit)
		}
		row, n = row+p.t(s.Kind, th.Text)+p.t(text, th.Muted), n+width(s.Kind+text)
	}
	if n == alwaysCol {
		return nil
	}
	lines := block{fit(row, w, p)}
	if pick {
		hint := fmt.Sprintf("· 1–%d switch one off · esc", num)
		if num == 1 {
			hint = "· 1 switch it off · esc"
		}
		if n+gap+width(hint) <= w {
			lines[0] = fit(row+p.sp(gap)+p.t(hint, th.Faint), w, p)
		} else {
			lines = append(lines, fit(p.sp(alwaysCol)+p.t(hint, th.Faint), w, p))
		}
	}
	return append(lines, p.sp(w))
}

// alwaysOff is key A: the Always line takes digits, or the read-only refusal.
func (m *model) alwaysOff() {
	if m.opts.ReadOnly {
		m.setToast("read-only: this key may not change a standing answer", true)
		return
	}
	m.inbox.always = len(m.switchable()) > 0
}

// alwaysKey handles every key while the Always line is in pick mode; the caller has taken ctrl+c.
func (m *model) alwaysKey(msg tea.KeyPressMsg) tea.Cmd {
	if k := msg.String(); k == "esc" || k == "A" {
		m.inbox.always = false
		return nil
	}
	on, t := m.switchable(), msg.Key().Text
	if len(t) != 1 || t[0] < '1' || int(t[0]-'0') > len(on) {
		return nil
	}
	m.inbox.always = false
	path, _ := m.standingAnswers()
	kind, ctx := on[t[0]-'1'].Kind, m.ctx
	return func() tea.Msg { return standingMsg{kind, data.SetStanding(ctx, path, kind, false)} }
}

// standingSet ends a switch-off: a failure shows the script's line, a success refreshes the factory so the line
// loses the kind.
func (m *model) standingSet(msg standingMsg) tea.Cmd {
	if msg.err != nil {
		m.setToast(firstLine(msg.err.Error()), true)
		return nil
	}
	m.setToast(msg.kind+": asks again from now", false)
	if m.runner != nil {
		m.runner.Forget()
	}
	return m.refreshFactory(0)
}

// needsCount is what the Inbox tab's badge counts: the waiting tasks, except those whose only decisions are
// picked: a picked one waits on a slot, not on the human.
func (m *model) needsCount() int {
	type task struct{ path, slug string }
	picked, open := map[task]bool{}, map[task]bool{}
	for _, c := range m.cards() {
		t := task{c.path, c.d.Slug}
		picked[t], open[t] = picked[t] || c.d.Picked != 0, open[t] || c.d.Picked == 0
	}
	n := 0
	for _, it := range m.waiting() {
		if t := (task{it.path, it.task.Slug}); !picked[t] || open[t] {
			n++
		}
	}
	return n
}
