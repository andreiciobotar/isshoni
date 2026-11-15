package tui

import (
	"cmp"
	"slices"
	"time"

	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

// The reload intervals, from the end of one load to the start of the next: while the Ops tab is shown, and while
// another is (the alarm badge on the tab needs the count).
const (
	opsEvery     = 30 * time.Second
	opsIdleEvery = 2 * time.Minute
)

type opsState struct {
	watches []data.OpsWatch
	err     string // the last load failed: the first line of why; watches is the result before it
	busy    bool
	doneAt  time.Time // when the last load finished, failed or not: the cadence
	okAt    time.Time // when the last good load finished: the age of watches
	top     int       // first visible body line
}

type opsMsg struct {
	watches []data.OpsWatch
	err     error
}

// loadOps starts a load when none is in flight and the last one finished minAge ago or more. Off the Ops tab it
// waits for the first data load, as the Factory view does.
func (m *model) loadOps(minAge time.Duration) tea.Cmd {
	o := &m.ops
	if m.runner == nil || m.fatal != nil || o.busy || (m.view != viewOps && !m.loaded) {
		return nil
	}
	if !o.doneAt.IsZero() && m.now().Sub(o.doneAt) < minAge {
		return nil
	}
	o.busy = true
	r, ctx := m.runner, m.ctx
	return func() tea.Msg {
		ws, err := r.OpsWatches(ctx)
		return opsMsg{ws, err}
	}
}

// opsTickEvery is the reload interval of the tab now.
func (m *model) opsTickEvery() time.Duration {
	if m.view == viewOps {
		return opsEvery
	}
	return opsIdleEvery
}

// opsRank orders the groups: alarmed first, then the other active ones, then the rest.
func opsRank(w data.OpsWatch) int {
	switch w.State {
	case "alarmed":
		return 0
	case "watching":
		return 1
	}
	return 2
}

// applyOps takes a load's result. A failure keeps the watches before it and says why. Watches are shown alarmed
// first, then watching, then the rest, each group in the command's order.
func (m *model) applyOps(msg opsMsg) {
	o := &m.ops
	o.busy, o.doneAt = false, m.now()
	if msg.err != nil {
		o.err = firstLine(msg.err.Error())
		return
	}
	o.watches, o.err, o.okAt = slices.Clone(msg.watches), "", m.now()
	slices.SortStableFunc(o.watches, func(a, b data.OpsWatch) int { return cmp.Compare(opsRank(a), opsRank(b)) })
	m.fixScroll()
}

// opsPositions is how many places the body can be scrolled to.
func (m *model) opsPositions() int {
	if m.w < minWidth || m.h < minHeight {
		return 1
	}
	h := m.h - 3
	return max(1, len(m.opsLines(m.th.on(m.th.Bg), m.w, h))-h+1)
}
