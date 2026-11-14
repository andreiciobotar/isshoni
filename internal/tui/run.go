package tui

import (
	"context"
	"os/signal"
	"strings"
	"syscall"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"isshoni/internal/data"
)

// Run starts the interactive dashboard. Quitting, or a closed terminal (SIGHUP), cancels every
// python call and waits for them to end, so none outlives the TUI.
func Run(o Options) error { return runProgram(o) }

// runProgram is Run with extra program options, which tests use to drive it without a TTY.
func runProgram(o Options, popts ...tea.ProgramOption) error {
	ctx, cancel := context.WithCancel(context.Background())
	o.Ctx = ctx
	defer func() {
		cancel()
		if o.Runner != nil {
			o.Runner.Wait()
		}
	}()
	hup, stop := signal.NotifyContext(ctx, syscall.SIGHUP)
	defer stop()
	_, err := tea.NewProgram(New(o), append(popts, tea.WithContext(hup))...).Run()
	if hup.Err() != nil { // hung up: the program ended on its own terms
		return nil
	}
	return err
}

// SnapshotOptions selects what Snapshot renders.
type SnapshotOptions struct {
	Options
	View    string // dashboard, session (the zoom), agents, factory, kaizen, kaizen-flow, kaizen-anatomy, kaizen-board, hanami, help
	Session string // id prefix for the zoomed session
	Row     int    // dashboard: cursor on this table row, 0 is the all row
	Fold    bool   // dashboard: collapse the repository the cursor is in
	W, H    int

	// frames: dashboard, that many frames of one load, the cursor one row lower in each; hanami, see below.
	// hanami: frames of a replay of Options.Replay (none: the idle scene), the first after At, then one
	// every FrameStep. Frames are separated by a form-feed line.
	At, FrameStep time.Duration
	Frames        int
	Legend        bool
}

// frameSep separates the frames of a snapshot.
const frameSep = "\n\f\n"

// Snapshot loads data once and renders a single settled frame; no TTY needed. A
// session prefix is resolved by the python CLI, so an ambiguous one is an error here.
func Snapshot(so SnapshotOptions) (string, error) {
	o := so.Options
	o.Static = true
	m := New(o)
	m.w, m.h = so.W, so.H
	if so.View == "hanami" { // drawn from the events alone: no transcripts are read
		m.fatal = nil
		return m.hanamiSnapshot(so), nil
	}
	if m.fatal != nil {
		return "", m.fatal
	}
	ctx := o.Ctx
	if ctx == nil {
		ctx = context.Background()
	}
	seen, _ := data.Scan(m.runner.Projects())
	res, err := m.runner.Sessions(ctx, ranges[m.rangeIdx].since)
	if err != nil {
		return "", err
	}
	m.seen = seen
	m.prices = res.Prices
	for i := range res.Sessions {
		s := &res.Sessions[i]
		s.LastChange = seen[s.ID]
		m.sessions[s.ID] = s
	}
	m.loading = false
	m.syncOrder(true)
	if ag, err := m.runner.Agents(ctx, ranges[m.rangeIdx].since); err == nil {
		m.agents = ag
	}
	detail := func(id string) error {
		d, err := m.runner.Session(ctx, id)
		if err != nil {
			return err
		}
		d.LastChange = seen[d.ID]
		m.details[d.ID] = d
		if m.sessions[d.ID] == nil {
			summary := *d
			summary.Dispatches = nil
			m.sessions[d.ID] = &summary
		}
		return nil
	}
	switch so.View {
	case "session":
		m.zoom = true
		if so.Session != "" {
			if err := detail(so.Session); err != nil {
				return "", err
			}
			for id := range m.details {
				m.selID = id
			}
		} else if s := m.mostRecent(); s != nil {
			m.selID = s.ID
			if err := detail(s.ID); err != nil {
				return "", err
			}
		}
	case "agents":
		m.view = viewAgents
	case "factory":
		m.view = viewFactory
	case "kaizen", "kaizen-flow", "kaizen-anatomy", "kaizen-board":
		m.view = viewKaizen
		m.kz.page = map[string]kaizenPage{"kaizen": pagePulse, "kaizen-flow": pageFlow, "kaizen-anatomy": pageAnatomy, "kaizen-board": pageBoard}[so.View]
		doc, err := m.runner.Kaizen(ctx, ranges[m.rangeIdx].since, false)
		if err != nil {
			return "", err
		}
		m.kz.doc, m.kz.doneAt, m.kz.doneRange = doc, m.now(), m.rangeIdx
	case "help":
		m.help = true
	default:
		for _, s := range m.sessions {
			if m.isLive(s) {
				if err := detail(s.ID); err != nil {
					return "", err
				}
			}
		}
	}
	m.applyFactory(m.factoryJob()())
	for _, job := range m.dueFetches() {
		m.applyFetch(job())
	}
	m.normalizeSelection()
	onTree := m.view == viewDashboard && !m.zoom
	n := 1
	if onTree {
		n = max(1, so.Frames)
	}
	frames := make([]string, 0, n)
	for i := 0; i < n; i++ {
		if row := so.Row + i; onTree && row > 0 && len(m.rows()) > 0 {
			rows := m.rows()
			m.selID = rows[min(row, len(rows)-1)].key
			if s, d := m.paneSession(); s != nil && d == nil {
				if err := detail(s.ID); err != nil {
					return "", err
				}
			}
		}
		if so.Fold && onTree {
			m.fold(true)
		}
		frames = append(frames, m.finishSnapshot())
	}
	return strings.Join(frames, frameSep), nil
}

func (m *model) finishSnapshot() string {
	m.settle()
	m.lastUpdate = m.now()
	m.fixScroll()
	out := m.Render()
	if m.th.NoColor {
		out = ansi.Strip(out)
	}
	return out
}

// preload fills a model for tests and renders without any I/O.
func (m *model) preload(sessions []*data.Session, details []*data.Session, agents *data.Agents) {
	m.seen, m.loaded = map[string]time.Time{}, true
	for _, s := range sessions {
		m.sessions[s.ID] = s
		m.seen[s.ID] = s.LastChange
	}
	for _, d := range details {
		m.details[d.ID] = d
	}
	m.agents = agents
	m.loading = false
	m.syncOrder(true)
	m.normalizeSelection()
	m.settle()
	m.fixScroll()
}
