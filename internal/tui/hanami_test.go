package tui

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"isshoni/internal/data"
)

func hanamiFixture(t *testing.T) []data.Event {
	t.Helper()
	evs, err := data.ReadEvents("testdata/hanami-replay.jsonl")
	if err != nil || len(evs) == 0 {
		t.Fatalf("fixture: %d events, %v", len(evs), err)
	}
	return evs
}

func hanamiShot(t *testing.T, evs []data.Event, at time.Duration, w, h int, noColor, legend bool) string {
	t.Helper()
	out, err := Snapshot(SnapshotOptions{
		Options: Options{NoColor: noColor, Replay: evs, ReplaySpeed: 3, Now: func() time.Time { return goldenNow }},
		View:    "hanami", W: w, H: h, At: at, Legend: legend,
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// The fixture's moments at speed 3: the question is asked at 39 s, the tick ripples at 69 s, the merge is at 89 s.
func TestHanamiGoldenFrames(t *testing.T) {
	evs := hanamiFixture(t)
	for _, c := range []struct {
		name   string
		evs    []data.Event
		at     time.Duration
		w, h   int
		legend bool
	}{
		{"idle", nil, 4 * time.Second, 80, 24, false},
		{"busy", evs, 60 * time.Second, 80, 24, false},
		{"busy", evs, 60 * time.Second, 140, 40, false},
		{"question", evs, 39500 * time.Millisecond, 100, 30, false},
		{"tick", evs, 69500 * time.Millisecond, 100, 30, false},
		{"merge", evs, 89500 * time.Millisecond, 100, 30, false},
		{"legend", evs, 60 * time.Second, 80, 24, true},
	} {
		name := fmt.Sprintf("hanami-%s-%dx%d", c.name, c.w, c.h)
		t.Run(name, func(t *testing.T) {
			got := hanamiShot(t, c.evs, c.at, c.w, c.h, true, c.legend)
			path := filepath.Join("testdata", name+".golden")
			if *update {
				if err := os.WriteFile(path, []byte(got+"\n"), 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			want, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("%v (run go test ./internal/tui -update)", err)
			}
			if got+"\n" != string(want) {
				t.Fatalf("%s changed; run go test ./internal/tui -update and review the diff\n--- got ---\n%s", name, got)
			}
		})
	}
}

func TestHanamiFramesAreExactAndRepeatable(t *testing.T) {
	evs := hanamiFixture(t)
	for _, sz := range [][2]int{{60, 16}, {80, 24}, {101, 31}, {200, 60}} {
		for _, noColor := range []bool{true, false} {
			got := hanamiShot(t, evs, 89500*time.Millisecond, sz[0], sz[1], noColor, false)
			if again := hanamiShot(t, evs, 89500*time.Millisecond, sz[0], sz[1], noColor, false); again != got {
				t.Fatalf("%v: two runs of the same replay differ", sz)
			}
			lines := strings.Split(got, "\n")
			if len(lines) != sz[1] {
				t.Fatalf("%v: %d lines", sz, len(lines))
			}
			for i, l := range lines {
				if n := ansi.StringWidth(l); n != sz[0] {
					t.Fatalf("%v colour %v: line %d is %d cells wide\n%s", sz, !noColor, i, n, ansi.Strip(l))
				}
			}
		}
	}
}

func TestHanamiSnapshotFrames(t *testing.T) {
	out, err := Snapshot(SnapshotOptions{
		Options: Options{NoColor: true, Replay: hanamiFixture(t), ReplaySpeed: 3, Now: func() time.Time { return goldenNow }},
		View:    "hanami", W: 80, H: 24, At: 60 * time.Second, Frames: 3, FrameStep: 500 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	frames := strings.Split(out, "\n\f\n")
	if len(frames) != 3 || frames[0] == frames[1] || frames[1] == frames[2] {
		t.Fatalf("want 3 different frames, got %d", len(frames))
	}
	if frames[0] != hanamiShot(t, hanamiFixture(t), 60*time.Second, 80, 24, true, false) {
		t.Fatal("the first frame is not the frame at --at")
	}
}

func ev(kind string, sec float64, f func(*data.Event)) data.Event {
	e := data.Event{Kind: kind, TS: goldenNow.Add(time.Duration(sec * float64(time.Second))), Session: "s1"}
	if f != nil {
		f(&e)
	}
	return e
}

func stepFor(s *hanamiScene, now time.Time, d time.Duration) {
	for t := time.Duration(0); t < d; t += hanamiDT {
		s.Step(now)
	}
}

// Every moving thing comes from an event, and goes when the event says so.
func TestHanamiSceneFollowsEvents(t *testing.T) {
	s := newHanamiScene(NewTheme(true))
	stepFor(s, goldenNow, 2*time.Second)
	if s.busy() || len(s.branches)+len(s.lanterns)+len(s.bells) != 0 {
		t.Fatal("an idle scene holds something that is not ambient")
	}

	agent := func(id, role, verdict string) func(*data.Event) {
		return func(e *data.Event) { e.Agent, e.Role, e.Verdict = id, role, verdict }
	}
	s.Apply(ev("spawn", 0, agent("a1", "coder", "")), false)
	s.Apply(ev("spawn", 0, agent("r1", "reviewer", "")), false)
	s.Apply(ev("spawn", 0, agent("a1", "coder", "")), false) // the same agent again: no second blossom
	if len(s.branches) != 1 || len(s.branches[0].blossoms) != 2 {
		t.Fatalf("want one branch with two blossoms, got %d branches", len(s.branches))
	}
	stepFor(s, goldenNow, 3*time.Second)
	if g := s.branches[0].grow.x; g < 0.9 {
		t.Fatalf("the branch did not grow: %.2f", g)
	}

	// a blossom is as large as its agent's work
	small := s.branches[0].blossoms[0].size.x
	for i := 0; i < 30; i++ {
		s.Apply(ev("tool", 3, func(e *data.Event) { e.Agent, e.Role, e.Tool = "a1", "coder", "edit" }), false)
	}
	stepFor(s, goldenNow, 3*time.Second)
	if a1, r1 := s.branches[0].blossoms[0], s.branches[0].blossoms[1]; a1.size.x < 2*small || r1.size.x > 1.2*small {
		t.Fatalf("thirty calls did not grow the caller's blossom, or grew the other: %.1f and %.1f from %.1f", a1.size.x, r1.size.x, small)
	}
	// its note is held until its agent's next call, 12 s at most
	if n := s.notes; len(n) == 0 || n[len(n)-1].held == 0 || n[len(n)-1].x1 <= n[len(n)-1].x0 {
		t.Fatal("the last call's note is not sounding")
	}
	stepFor(s, goldenNow, noteHold*time.Second)
	if n := s.notes; len(n) != 0 && n[len(n)-1].held != 0 {
		t.Fatal("a note is held longer than its limit")
	}

	s.Apply(ev("return", 3, agent("a1", "coder", "")), false)
	s.Apply(ev("return", 3, agent("zz", "coder", "")), false) // nobody's blossom: nothing
	s.Apply(ev("return", 3, agent("a1", "coder", "")), false) // it returned already: no second handful
	if bl := s.branches[0].blossoms; len(bl) != 2 || bl[0].closed.IsZero() || !bl[1].closed.IsZero() || len(s.petals) != 5 {
		t.Fatalf("a return closes its blossom, which stays, and lets go five petals: %d blossoms, %d petals", len(bl), len(s.petals))
	}
	for i := 0; i < 3; i++ { // drawing is not part of the physics: it must not touch the generator or the state
		s.draw(false)
	}
	twin := newHanamiScene(NewTheme(true))
	stepFor(twin, goldenNow, 2*time.Second)
	twin.Apply(ev("spawn", 0, agent("a1", "coder", "")), false)
	twin.Apply(ev("spawn", 0, agent("r1", "reviewer", "")), false)
	stepFor(twin, goldenNow, 3*time.Second)
	for i := 0; i < 30; i++ {
		twin.Apply(ev("tool", 3, func(e *data.Event) { e.Agent, e.Role, e.Tool = "a1", "coder", "edit" }), false)
	}
	stepFor(twin, goldenNow, 3*time.Second+noteHold*time.Second)
	twin.Apply(ev("return", 3, agent("a1", "coder", "")), false)
	if twin.rnd != s.rnd || strings.Join(twin.draw(false), "\n") != strings.Join(s.draw(false), "\n") {
		t.Fatal("drawing a frame changed what the next frame is")
	}
	stepFor(s, goldenNow, 4*time.Second)
	if len(s.petals) != 0 {
		t.Fatalf("%d petals never reached the disc", len(s.petals))
	}

	s.Apply(ev("return", 8, agent("r1", "reviewer", "changes")), false)
	turned, ring := false, false
	for i := 0; i < 8*hanamiFPS && len(s.petals) > 0; i++ {
		s.Step(goldenNow)
		for _, p := range s.petals {
			turned = turned || p.back
		}
		ring = ring || len(s.ripples) == 1
	}
	if !turned || !ring || len(s.petals) != 0 {
		t.Fatalf("changes needed: petals turned back %v with a ring %v, %d left", turned, ring, len(s.petals))
	}
	stepFor(s, goldenNow, 2*time.Second)

	pr := func(n int, state string) func(*data.Event) {
		return func(e *data.Event) { e.PR, e.State = n, state }
	}
	s.Apply(ev("pr", 9, pr(7, "open")), false)
	s.Apply(ev("ci", 9, pr(7, "running")), false)
	s.Apply(ev("pr", 9, pr(8, "open")), false)
	stepFor(s, goldenNow, 6*time.Second)
	s.Apply(ev("pr", 15, pr(8, "closed")), false)
	if len(s.lanterns) != 2 || len(s.fire) != 0 {
		t.Fatal("a pull request closed without a merge goes out: no burst, and not at once")
	}
	stepFor(s, goldenNow, 2*time.Second)
	if len(s.lanterns) != 1 || s.lanterns[0].ci != "running" {
		t.Fatalf("want the one open pull request's lantern, checks running: %+v", s.lanterns)
	}
	s.Apply(ev("merge", 16, func(e *data.Event) { e.PR, e.Repo = 7, "another" }), false) // the same number elsewhere: its own lantern
	if len(s.lanterns) != 2 || len(s.fire) != 0 {
		t.Fatal("a merge in another repository burst this repository's lantern")
	}
	s.lanterns = s.lanterns[:1]
	s.Apply(ev("merge", 16, pr(7, "")), false)
	if len(s.stars) != 1 || s.forte != 1 {
		t.Fatalf("a merge lights the stage and leaves a star: %d stars, forte %.1f", len(s.stars), s.forte)
	}
	if len(s.lanterns) != 0 || len(s.fire) == 0 {
		t.Fatalf("a merge bursts its lantern: %d lanterns, %d sparks", len(s.lanterns), len(s.fire))
	}
	s.Apply(ev("merge", 16, pr(9, "")), false) // its opening was never seen: a lantern rises, then bursts
	before := len(s.fire)
	stepFor(s, goldenNow, 5*time.Second)
	if len(s.lanterns) != 0 || before == 0 {
		t.Fatalf("the unseen pull request's lantern did not burst: %d lanterns", len(s.lanterns))
	}
	stepFor(s, goldenNow, 5*time.Second)
	if len(s.fire) != 0 {
		t.Fatal("the fireworks never end")
	}

	slug := func(e *data.Event) { e.Slug = "quiet-lamps" }
	s.Apply(ev("question", 30, slug), false)
	stepFor(s, goldenNow, 5*time.Second)
	if len(s.bells) != 1 || len(s.ripples) != 0 {
		t.Fatalf("a question holds one still point after its ripple: %d bells, %d ripples", len(s.bells), len(s.ripples))
	}
	s.Apply(ev("answer", 40, slug), false)
	s.Apply(ev("watch", 40, nil), false)
	if len(s.bells) != 0 || len(s.ripples) != 1 {
		t.Fatalf("answered, and one ripple for the tick: %d bells, %d ripples", len(s.bells), len(s.ripples))
	}

	s.Apply(ev("tool", 41, func(e *data.Event) { e.Tool, e.Role = "edit", "main" }), false)
	s.Apply(ev("tokens", 41, func(e *data.Event) { e.Out, e.CacheRead, e.Role = 900, 200000, "main" }), false)
	if len(s.notes) != 1 || s.notes[0].glyph != '◆' || len(s.sparks) == 0 {
		t.Fatalf("a tool use is a note, output tokens are sparks: %d notes, %d sparks", len(s.notes), len(s.sparks))
	}
	s.Step(goldenNow)
	if s.cacheRate == 0 {
		t.Fatal("cache reads do not reach the wind")
	}

	// the agent that returned acts again (it was resumed): its blossom reopens
	s.Apply(ev("tool", 42, func(e *data.Event) { e.Agent, e.Role, e.Tool = "a1", "coder", "shell" }), false)
	if !s.branches[0].blossoms[0].closed.IsZero() {
		t.Fatal("a resumed agent's blossom stays closed")
	}
	// the session falls silent: after three minutes whoever had not returned is closed, and the tree keeps
	// what it bore for two hours; then the branch withdraws and everything is ambient again
	rest := goldenNow.Add(42*time.Second + hanamiQuiet + time.Second)
	stepFor(s, rest, 20*time.Second)
	if b := s.branches; len(b) != 1 || len(b[0].blossoms) != 2 || b[0].blossoms[0].closed.IsZero() || b[0].grow.x < 0.9 {
		t.Fatalf("a session at rest keeps its branch with its blossoms closed: %d branches", len(b))
	}
	stepFor(s, rest.Add(hanamiKeep), 20*time.Second)
	if len(s.branches) != 0 || s.busy() {
		t.Fatalf("after the keep time the branch is still there: %d branches", len(s.branches))
	}
	if len(s.stars) == 0 {
		t.Fatal("the merge's star went with the branch: it stays three times as long")
	}
	stepFor(s, rest.Add(4*hanamiKeep), time.Second)
	if len(s.stars) != 0 {
		t.Fatal("stars never fade")
	}
}

// What happened before the viewer arrived is there, without any motion.
func TestHanamiSettledEventsDoNotMove(t *testing.T) {
	s := newHanamiScene(NewTheme(true))
	for _, e := range hanamiFixture(t) {
		s.Apply(e, true)
	}
	if s.busy() || s.acc != 0 || len(s.lanterns) != 1 || !s.lanterns[0].placed || s.forte != 0 {
		t.Fatalf("settled history moved something, or lost the open pull request: %d lanterns", len(s.lanterns))
	}
	// the tree arrives with what it bore: ten agents came and went, each left a closed blossom of its size, and
	// the merge left its star
	closed, grown := 0, 0
	for _, b := range s.branches {
		for _, bl := range b.blossoms {
			if !bl.closed.IsZero() {
				closed++
			}
			if bl.size.x > blossomSize(s.geo(), 0) {
				grown++
			}
		}
	}
	if closed != 10 || grown < 8 || len(s.stars) != 1 {
		t.Fatalf("settled history left %d closed blossoms (%d grown) and %d stars", closed, grown, len(s.stars))
	}
	// a session that was working is there at full length, and stays while its last event is recent
	last := hanamiFixture(t)[len(hanamiFixture(t))-1].TS
	if len(s.branches) == 0 || s.branches[0].grow.x != 1 {
		t.Fatal("settled work left no grown branch")
	}
	stepFor(s, last, time.Second)
	if n := len(s.branches); n == 0 {
		t.Fatal("a branch whose session worked a moment ago withdrew")
	}
}

// A busy hour at replay speed must stay bounded: sprites are capped, not one per event.
func TestHanamiSpritesAreCapped(t *testing.T) {
	s := newHanamiScene(NewTheme(true))
	for i := 0; i < 20000; i++ {
		s.Apply(ev("tool", 0, func(e *data.Event) { e.Tool = hanamiTools[i%6].kind }), false)
		s.Apply(ev("tokens", 0, func(e *data.Event) { e.Out = 5000 }), false)
		s.Apply(ev("merge", 0, func(e *data.Event) { e.PR = i }), false)
		s.Apply(ev("watch", 0, nil), false)
		s.Apply(ev("spawn", 0, func(e *data.Event) { e.Agent = "x" }), false)
		s.Apply(ev("return", 0, func(e *data.Event) { e.Agent = "x" }), false)
		if i%50 == 0 {
			s.Step(goldenNow)
		}
	}
	if len(s.petals) > maxPetals || len(s.ripples) > maxRipples {
		t.Fatalf("unbounded: %d petals, %d ripples", len(s.petals), len(s.ripples))
	}
	if len(s.notes) > maxNotes || len(s.sparks) > maxSparks || len(s.fire) > maxFire || len(s.lanterns) > maxLanterns {
		t.Fatalf("unbounded: %d notes, %d sparks, %d fire, %d lanterns", len(s.notes), len(s.sparks), len(s.fire), len(s.lanterns))
	}
}

// The stream's process exists only while the tab is shown live; nothing is stepped otherwise.
func TestHanamiCostsNothingWhenHidden(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("STUBDIR", dir)
	py, _ := filepath.Abs("../data/testdata/fakeevents.sh")
	now := goldenNow
	r := &data.Runner{Root: "../..", Python: py, ProjectsDir: dir, Timeout: time.Second}
	m := New(Options{NoColor: true, Runner: r, Now: func() time.Time { return now }})
	m.fatal, m.loading = nil, false
	if m.hanamiSync() != nil || m.hn.following || m.hn.scene != nil {
		t.Fatal("the stream runs outside the tab")
	}
	m.hanamiTick(time.Second)
	if m.hn.scene != nil {
		t.Fatal("a scene exists outside the tab")
	}

	m.Update(press("5"))
	if m.view != viewHanami || !m.hn.following || m.hn.scene == nil {
		t.Fatal("key 5 does not open the tab on a live stream")
	}
	gen := m.hn.gen
	pr := func(e *data.Event) { e.PR = 7 }
	m.Update(hanamiEventsMsg{gen: gen, evs: []data.Event{
		ev("spawn", -60, func(e *data.Event) { e.Agent = "old" }), // the stream's history: there, settled
		ev("tool", -60, func(e *data.Event) { e.Tool = "shell" }), // history: no note
		ev("pr", -900, pr), // open since a quarter of an hour: hanging in the sky
		ev("live", 0, nil),
		ev("tool", 0, func(e *data.Event) { e.Tool = "shell" }),
	}})
	s := m.hn.scene
	if len(s.branches) != 1 || s.branches[0].blossoms[0].open.x != 1 || len(s.notes) != 1 || len(s.lanterns) != 1 || !s.lanterns[0].placed {
		t.Fatalf("history must set state only, news must move: %d notes, %d lanterns", len(s.notes), len(s.lanterns))
	}
	// gh is polled once a minute and a tick's line lands when the tick ends: news arrives with an old stamp, and still shows
	m.Update(hanamiEventsMsg{gen: gen, evs: []data.Event{ev("merge", -50, pr), ev("watch", -10, nil), ev("pr", -40, func(e *data.Event) { e.PR = 8 })}})
	if len(s.fire) == 0 || len(s.ripples) != 1 || len(s.lanterns) != 1 || s.lanterns[0].placed {
		t.Fatalf("late news did not show: %d sparks of the merge, %d ripples, %d lanterns", len(s.fire), len(s.ripples), len(s.lanterns))
	}
	m.tickFrom = now
	now = now.Add(100 * time.Millisecond)
	m.hanamiTick(100 * time.Millisecond)
	if f := s.frame; f != 2 {
		t.Fatalf("100 ms is two frames at 20 a second, stepped %d", f)
	}
	m.hanamiTick(time.Hour) // a stalled terminal does not fast-forward
	if f := s.frame; f != 2+hanamiMaxSteps {
		t.Fatalf("a stall stepped %d frames", f-2)
	}

	for _, k := range []string{"l", "z"} {
		m.Update(press(k))
	}
	if !m.hn.legend || !m.hn.zen || len(m.frame()) != m.h {
		t.Fatal("l and z do not toggle the legend and the full screen")
	}
	if got := strings.Join(m.frame(), "\n"); strings.Contains(got, "Kaizen") || !strings.Contains(got, "legend") {
		t.Fatalf("full screen keeps the header, or the legend is missing\n%s", got)
	}

	m.Update(press("esc"))
	if m.view != viewDashboard || m.hn.following || m.hn.scene != nil || m.hn.gen == gen {
		t.Fatal("leaving the tab keeps the stream or the scene")
	}
	m.Update(hanamiEventsMsg{gen: gen, evs: []data.Event{ev("tool", 0, nil)}}) // a late message of the old stream
	if m.hn.scene != nil {
		t.Fatal("a late message revived the scene")
	}
}

// p from the live tab reads the last day, replays it, and p again is live; a history that comes back after the
// viewer changed their mind is dropped.
func TestHanamiReplayKey(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("STUBDIR", dir)
	b, _ := os.ReadFile("testdata/hanami-replay.jsonl")
	os.WriteFile(filepath.Join(dir, "events.jsonl"), b, 0o644)
	py, _ := filepath.Abs("../data/testdata/fakeevents.sh")
	r := &data.Runner{Root: "../..", Python: py, ProjectsDir: dir, Timeout: 5 * time.Second}
	m := New(Options{NoColor: true, Runner: r, Now: func() time.Time { return goldenNow }})
	m.fatal, m.loading = nil, false
	defer func() { m.hn.cancel(); r.Wait() }()
	m.Update(press("5"))
	history := func() tea.Msg {
		t.Helper()
		cmd := m.handleKey(press("p"))
		m.hanamiSync() // what Update does after every message: the live stream stops while the history loads
		if cmd == nil || !m.hn.loading || m.hn.following {
			t.Fatal("p does not start reading the history, or the stream still runs")
		}
		return cmd()
	}
	late := history()
	m.Update(press("p")) // changed their mind before it arrived
	m.Update(late)
	if m.hn.replay != nil || m.hn.loading || !m.hn.following {
		t.Fatal("a history nobody waits for started a replay, or the tab did not go back to live")
	}
	m.Update(history())
	if m.hn.replay == nil || m.hn.loading || m.hn.following || m.hn.speed != hanamiReplaySpeed || !strings.Contains(plain(m), "replay ×60") {
		t.Fatalf("p did not start the replay: loading %v, following %v", m.hn.loading, m.hn.following)
	}
	m.Update(press("p"))
	if m.hn.replay != nil || !m.hn.following {
		t.Fatal("p again does not return to the live stream")
	}
}

// A replay never shows more than three seconds of nothing: the clock skips the night.
func TestHanamiReplaySkipsIdleGaps(t *testing.T) {
	evs := []data.Event{ev("tool", 0, nil), ev("tool", 8*3600, nil), ev("tool", 8*3600+60, nil)}
	m := New(Options{NoColor: true, Replay: evs, ReplaySpeed: 60, Now: func() time.Time { return goldenNow }})
	steps := 0
	for ; m.hn.ri < len(evs) && steps < 60*hanamiFPS; steps++ {
		m.hanamiStep()
	}
	if got := time.Duration(steps) * hanamiDT; got > 6*time.Second || got < 3*time.Second {
		t.Fatalf("eight idle hours and one minute took %v of replay", got)
	}
	if !strings.Contains(plain(m), "Jan 1 08:01 UTC") {
		t.Fatalf("the subheader does not show the replay's clock\n%s", strings.Split(plain(m), "\n")[1])
	}
}

func TestHanamiReplayLoopsAndReturnsToLive(t *testing.T) {
	evs := hanamiFixture(t)[:40]
	m := New(Options{NoColor: true, Replay: evs, ReplaySpeed: 60, Now: func() time.Time { return goldenNow }})
	m.fatal, m.loading = nil, false
	if m.view != viewHanami || m.hn.replay == nil || m.hanamiSync() != nil {
		t.Fatal("--replay does not open the tab in a replay without a stream")
	}
	for i := 0; i < 20*hanamiFPS && m.hn.ri < len(evs); i++ {
		m.hanamiStep()
	}
	if m.hn.ri != len(evs) {
		t.Fatalf("the replay stopped at %d of %d", m.hn.ri, len(evs))
	}
	for i := 0; i <= int(hanamiReplayRest/hanamiDT); i++ {
		m.hanamiStep()
	}
	if m.hn.ri >= len(evs) {
		t.Fatal("the replay does not start over")
	}
	m.Update(press("p"))
	if m.hn.replay != nil {
		t.Fatal("p does not leave the replay")
	}
}
