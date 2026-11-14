package tui

import (
	"context"
	"fmt"
	"strings"
	"time"

	"charm.land/bubbles/v2/key"
	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

// The Hanami tab: it feeds the scene from the live event stream while the tab is shown
// (the stream's process exists only then), or from recorded events in a replay.

const (
	hanamiFollowSince = "2h"            // history the stream starts with: what exists when the viewer arrives
	hanamiReplaySpeed = 60.0            // a minute a second: an agent of five minutes blooms for five seconds
	hanamiReplayGap   = 3 * time.Second // the longest a replay shows nothing happening
	hanamiReplayRest  = 3 * time.Second // pause at the end of a replay before it starts over
	hanamiReplaySince = "24h"           // what the replay key plays
	hanamiMaxSteps    = 6               // frames stepped per tick at most: a stall does not fast-forward
	hanamiBatch       = 512             // events taken from the stream per message
	hanamiQueue       = 4096            // events buffered between the stream and the screen
	hanamiLead        = time.Second     // a replay starts this long before its first event
	hanamiSnapshotAt  = 4 * time.Second // --snapshot hanami without --at
	hanamiFrameStep   = 400 * time.Millisecond
)

type hanamiState struct {
	scene       *hanamiScene
	legend, zen bool
	err         string
	acc         time.Duration // time not yet stepped

	following bool
	live      bool // the stream's history is over: what arrives now is news, whatever its timestamp
	gen       int  // a message from an older stream is dropped
	hist      int  // likewise for the replay key's history
	cancel    context.CancelFunc

	replay  []data.Event // nil: live
	ri      int
	clock   time.Time // event time of the replay
	speed   float64   // event seconds per second
	ended   time.Duration
	loading bool // the replay key's history is being read
}

type hanamiEventsMsg struct {
	gen int
	evs []data.Event
	ch  <-chan data.Event
}
type hanamiEndMsg struct {
	gen int
	err error
}
type hanamiHistoryMsg struct {
	gen int
	evs []data.Event
	err error
}

// startReplay plays evs from the start on a fresh scene; speed 0 is hanamiReplaySpeed.
func (m *model) startReplay(evs []data.Event, speed float64) {
	hn := &m.hn
	hn.replay, hn.ri, hn.ended, hn.acc = evs, 0, 0, 0
	hn.scene = newHanamiScene(m.th)
	if len(evs) == 0 {
		hn.replay = nil
		return
	}
	hn.clock = evs[0].TS.Add(-hanamiLead)
	if speed <= 0 {
		speed = hanamiReplaySpeed
	}
	hn.speed, hn.scene.scale = speed, speed
}

// hanamiSync starts the event stream when the tab is shown live and stops it otherwise;
// Update calls it after every message, so no way of leaving the tab can leak the process.
func (m *model) hanamiSync() tea.Cmd {
	hn := &m.hn
	want := m.view == viewHanami && hn.replay == nil && !hn.loading && !m.opts.Static && m.runner != nil && m.fatal == nil
	switch {
	case want && !hn.following:
		hn.following, hn.live, hn.err = true, false, ""
		hn.gen++
		hn.scene = newHanamiScene(m.th)
		ctx, cancel := context.WithCancel(m.ctx)
		hn.cancel = cancel
		ch := make(chan data.Event, hanamiQueue)
		r, gen := m.runner, hn.gen
		follow := func() tea.Msg {
			err := r.FollowEvents(ctx, hanamiFollowSince, func(e data.Event) {
				select {
				case ch <- e:
				case <-ctx.Done():
				}
			})
			close(ch)
			return hanamiEndMsg{gen, err}
		}
		return tea.Batch(follow, hanamiWait(gen, ch))
	case !want && hn.following:
		hn.following = false
		hn.gen++
		hn.cancel()
	}
	if m.view != viewHanami && hn.replay == nil {
		hn.scene = nil // nothing is kept, or stepped, while the tab is not shown
	}
	return nil
}

func hanamiWait(gen int, ch <-chan data.Event) tea.Cmd {
	return func() tea.Msg {
		e, ok := <-ch
		if !ok {
			return nil
		}
		evs := []data.Event{e}
		for len(evs) < hanamiBatch {
			select {
			case e, ok := <-ch:
				if !ok {
					return hanamiEventsMsg{gen, evs, nil}
				}
				evs = append(evs, e)
				continue
			default:
			}
			break
		}
		return hanamiEventsMsg{gen, evs, ch}
	}
}

func (m *model) hanamiUpdate(msg tea.Msg) tea.Cmd {
	hn := &m.hn
	switch msg := msg.(type) {
	case hanamiEventsMsg:
		if msg.gen != hn.gen || hn.scene == nil {
			return nil
		}
		hn.scene.w, hn.scene.h = m.hanamiSize()
		for _, e := range msg.evs {
			if e.Kind == "live" {
				hn.live = true
				hn.scene.Arrive(m.now())
				continue
			}
			hn.scene.Apply(e, !hn.live)
		}
		if msg.ch != nil {
			return hanamiWait(msg.gen, msg.ch)
		}
	case hanamiEndMsg:
		switch { // a stream stopped by leaving the tab carries an older generation
		case msg.gen != hn.gen:
		case msg.err != nil:
			hn.err = firstLine(msg.err.Error())
		default:
			hn.err = "it ended"
		}
	case hanamiHistoryMsg:
		if msg.gen != hn.hist {
			return nil
		}
		hn.loading = false
		if msg.err != nil {
			m.setToast("replay failed: "+firstLine(msg.err.Error()), true)
			return nil
		}
		if len(msg.evs) == 0 {
			m.setToast("nothing to replay in the last "+hanamiReplaySince, false)
			return nil
		}
		m.startReplay(msg.evs, 0)
	}
	return nil
}

// hanamiKey handles the tab's own keys; false when the key is not one of them.
func (m *model) hanamiKey(msg tea.KeyPressMsg) (tea.Cmd, bool) {
	hn := &m.hn
	switch {
	case key.Matches(msg, keys.Legend):
		hn.legend = !hn.legend
	case key.Matches(msg, keys.Zen):
		hn.zen = !hn.zen
	case key.Matches(msg, keys.Replay):
		hn.hist++                           // a history still being read is no longer wanted
		if hn.replay != nil || hn.loading { // back to live
			hn.replay, hn.loading = nil, false
			return nil, true
		}
		if m.runner == nil || m.fatal != nil {
			return nil, true
		}
		hn.loading = true
		r, ctx, gen := m.runner, m.ctx, hn.hist
		return func() tea.Msg {
			evs, err := r.Events(ctx, hanamiReplaySince)
			return hanamiHistoryMsg{gen, evs, err}
		}, true
	default:
		return nil, false
	}
	return nil, true
}

// hanamiTick steps the scene for the time that passed, one fixed frame at a time.
func (m *model) hanamiTick(dt time.Duration) {
	hn := &m.hn
	if m.view != viewHanami || hn.scene == nil || m.help {
		hn.acc = 0
		return
	}
	hn.acc += dt
	for n := 0; hn.acc >= hanamiDT; n++ {
		hn.acc -= hanamiDT
		if n < hanamiMaxSteps {
			m.hanamiStep()
		}
	}
}

// hanamiStep is one frame: in a replay it first applies the events the clock has reached.
func (m *model) hanamiStep() {
	hn := &m.hn
	hn.scene.w, hn.scene.h = m.hanamiSize()
	if hn.replay == nil {
		hn.scene.Step(m.now())
		return
	}
	if hn.ri >= len(hn.replay) {
		if hn.ended += hanamiDT; hn.ended >= hanamiReplayRest && len(hn.replay) > 0 && !m.opts.Static {
			m.startReplay(hn.replay, hn.speed)
		}
	} else {
		hn.clock = hn.clock.Add(time.Duration(hn.speed * float64(hanamiDT)))
		// a night without events is not minutes of an empty screen: the clock skips to just before the next one
		if lead := time.Duration(hn.speed * float64(hanamiReplayGap)); hn.replay[hn.ri].TS.Sub(hn.clock) > lead {
			hn.clock = hn.replay[hn.ri].TS.Add(-lead)
		}
		for hn.ri < len(hn.replay) && !hn.replay[hn.ri].TS.After(hn.clock) {
			hn.scene.Apply(hn.replay[hn.ri], false)
			hn.ri++
		}
	}
	hn.scene.Step(hn.clock)
}

func (m *model) hanamiSubheader(p paint) string {
	th, hn := m.th, &m.hn
	left := p.b("Hanami", th.Text) + p.t(" 花見", th.Muted)
	switch {
	case hn.loading:
		left += p.t("  reading the last "+hanamiReplaySince+"…", th.Blue)
	case hn.replay != nil:
		left += p.b("  replay", th.Pink) + p.t(fmt.Sprintf(" ×%.0f · %s UTC", hn.speed, hn.clock.UTC().Format("Jan 2 15:04")), th.Muted)
	case hn.err != "":
		left += p.b("  no event stream: "+hn.err, th.Red)
	default:
		left += p.t("  live · every motion is an event", th.Muted)
	}
	return left
}

// hanamiFrame is the whole screen for the tab: header, scene, footer, or the scene alone.
func (m *model) hanamiFrame(p paint) []string {
	hn := &m.hn
	if hn.scene == nil {
		hn.scene = newHanamiScene(m.th)
	}
	w, h := m.hanamiSize()
	hn.scene.w, hn.scene.h = w, h
	if hn.zen {
		return hn.scene.draw(hn.legend)
	}
	lines := []string{m.header(p), m.subheader(p)}
	lines = append(lines, hn.scene.draw(hn.legend)...)
	return append(lines, m.footer(p))
}

// hanamiSize is the scene's size in cells: the screen without the header, sub-header and footer.
func (m *model) hanamiSize() (w, h int) {
	if m.hn.zen {
		return m.w, m.h
	}
	return m.w, m.h - 3
}

// hanamiSnapshot renders frames of a replay (or of the idle scene) for --snapshot hanami:
// the first after at, then one every step, joined by a form-feed line. No I/O, no clock.
func (m *model) hanamiSnapshot(so SnapshotOptions) string {
	m.view = viewHanami
	m.hn.legend = so.Legend
	m.startReplay(so.Replay, so.ReplaySpeed)
	at, step := so.At, so.FrameStep
	if at <= 0 {
		at = hanamiSnapshotAt
	}
	if step <= 0 {
		step = hanamiFrameStep
	}
	var frames []string
	var t time.Duration
	for i := 0; i < max(1, so.Frames); i++ {
		for until := at + time.Duration(i)*step; t < until; t += hanamiDT {
			m.hanamiStep()
		}
		frames = append(frames, m.finishSnapshot())
	}
	return strings.Join(frames, frameSep)
}
