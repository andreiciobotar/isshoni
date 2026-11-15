package tui

import (
	"time"

	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

// opsEvery is the reload interval while the Ops tab is shown, from the end of one load to the start of the next.
const opsEvery = 30 * time.Second

type opsState struct {
	watches []data.OpsWatch
	err     string // the last load failed: the first line of why; watches is the result before it
	busy    bool
	doneAt  time.Time // when the last load finished, failed or not
	top     int       // first visible body line
}

type opsMsg struct {
	watches []data.OpsWatch
	err     error
}

// loadOps starts a load when the tab is shown, none is in flight and the last one finished minAge ago or more.
func (m *model) loadOps(minAge time.Duration) tea.Cmd {
	o := &m.ops
	if m.view != viewOps || m.runner == nil || m.fatal != nil || o.busy {
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

// applyOps takes a load's result. A failure keeps the watches before it and says why.
func (m *model) applyOps(msg opsMsg) {
	o := &m.ops
	o.busy, o.doneAt = false, m.now()
	if msg.err != nil {
		o.err = firstLine(msg.err.Error())
		return
	}
	o.watches, o.err = msg.watches, ""
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
