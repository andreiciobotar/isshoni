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
	stepFor(s, goldenNow, 100*time.Millisecond)
	if len(s.bells) != 1 || s.bells[0].state != bellReturning {
		t.Fatal("an answer sends its light home before the bell goes")
	}
	stepFor(s, goldenNow, time.Second)
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
	stepFor(s, rest.Add(hanamiKeep), 70*time.Second) // and the beat of the burst of calls has died away
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

// The message that hands back carries its tokens at the return's own time: they must not reopen the blossom.
func TestHanamiReturnStaysClosed(t *testing.T) {
	who := func(e *data.Event) { e.Agent, e.Role, e.Tool, e.Out = "a1", "coder", "message", 900 }
	for _, settled := range []bool{false, true} {
		s := newHanamiScene(NewTheme(true))
		s.Apply(ev("spawn", 0, who), settled)
		s.Apply(ev("tool", 5, who), settled) // the handback call
		s.Apply(ev("return", 5, who), settled)
		s.Apply(ev("tokens", 5, who), settled) // its message's usage, same instant
		if bl := s.branches[0].blossoms[0]; bl.closed.IsZero() {
			t.Fatalf("settled %v: the agent's own last words reopened its blossom", settled)
		}
		s.Apply(ev("tool", 6, who), settled) // resumed a second later
		if bl := s.branches[0].blossoms[0]; !bl.closed.IsZero() {
			t.Fatalf("settled %v: a resumed agent's blossom stays closed", settled)
		}
	}
}

// A note sounds until its agent's next call, whatever that call lands on.
func TestHanamiNotesAreReleasedByTheirAgent(t *testing.T) {
	s := newHanamiScene(NewTheme(true))
	call := func(agent, tool string) {
		s.Apply(ev("tool", 0, func(e *data.Event) { e.Agent, e.Tool = agent, tool }), false)
	}
	held := func() (n int) {
		for _, x := range s.notes {
			if x.held > 0 {
				n++
			}
		}
		return
	}
	call("a", "read")
	stepFor(s, goldenNow, time.Second)
	call("b", "shell")
	if len(s.notes) != 2 || held() != 2 {
		t.Fatalf("two agents, two sounding notes: %d notes, %d held", len(s.notes), held())
	}
	call("a", "shell") // lands in the cell of b's note and merges with it: a's read must stop all the same
	if len(s.notes) != 2 || held() != 1 || s.notes[0].held != 0 || s.notes[1].who != "s1/a" {
		t.Fatalf("a's next call did not release a's note: %d notes, %d held", len(s.notes), held())
	}
	for i := 0; i < 40; i++ { // many notes later, an old held one is still released by its agent
		stepFor(s, goldenNow, 300*time.Millisecond)
		call(fmt.Sprint("x", i), "edit")
	}
	call("a", "write")
	for _, x := range s.notes {
		if x.who == "s1/a" && x.held > 0 && x.glyph != '■' {
			t.Fatal("an older note of a is still held after a called again")
		}
	}
	s.scale = 60 // a replay holds for event seconds
	call("z", "read")
	stepFor(s, goldenNow, time.Second)
	if n := s.notes[len(s.notes)-1]; n.held != 0 {
		t.Fatal("at a minute a second a note is still held after a second (a minute of events)")
	}
}

// The tree holds twelve branches: a newer session takes the place of the one at rest longest, and a flood of
// sessions all at work is bounded too. The canopy and the sky are capped likewise.
func TestHanamiTreeIsBounded(t *testing.T) {
	s := newHanamiScene(NewTheme(true))
	session := func(i int, sec float64) {
		s.Apply(ev("spawn", sec, func(e *data.Event) { e.Session, e.Agent = fmt.Sprint("s", i), "a" }), false)
	}
	for i := 0; i < 40; i++ { // one every five minutes: each finds the earlier ones at rest
		session(i, float64(i)*300)
		stepFor(s, goldenNow.Add(time.Duration(i)*5*time.Minute), 3*time.Second)
		slots := map[int]bool{}
		for _, b := range s.branches {
			if !b.leaving {
				if slots[b.slot] {
					t.Fatalf("after %d sessions two branches share slot %d", i+1, b.slot)
				}
				slots[b.slot] = true
			}
		}
		if len(slots) > hanamiSlots || len(s.branches) > hanamiSlots+1 {
			t.Fatalf("after %d sessions: %d branches", i+1, len(s.branches))
		}
	}
	if b := s.branches[len(s.branches)-1]; b.session != "s39" || b.leaving {
		t.Fatal("the newest session has no branch")
	}
	for i := 100; i < 400; i++ { // all at once
		session(i, 40*300)
	}
	if len(s.branches) > 3*hanamiSlots { // two a slot at work, and those just told to leave
		t.Fatalf("a flood of sessions at work: %d branches", len(s.branches))
	}
	stepFor(s, goldenNow.Add(40*5*time.Minute), 5*time.Second)
	if len(s.branches) > 2*hanamiSlots {
		t.Fatalf("the branches told to leave are still there: %d", len(s.branches))
	}

	one := newHanamiScene(NewTheme(true))
	agent := func(kind string, i int, sec float64) {
		one.Apply(ev(kind, sec, func(e *data.Event) { e.Agent = fmt.Sprint("a", i) }), false)
	}
	for i := 0; i < maxCanopy+6; i++ {
		agent("spawn", i, float64(i))
		if i < 3 {
			agent("return", i, float64(i)+0.5)
		}
	}
	bl := one.branches[0].blossoms
	if len(bl) != maxCanopy || !bl[0].closed.IsZero() || bl[0].agent != "a6" { // three closed went first, then the three oldest open
		t.Fatalf("a full branch drops its closed blossoms first, then the oldest: %d, first %s", len(bl), bl[0].agent)
	}
	for i := 0; i < maxStars+10; i++ {
		one.Apply(ev("pr", 100, func(e *data.Event) { e.PR = i }), true)
		one.Apply(ev("merge", 100, func(e *data.Event) { e.PR = i }), true)
	}
	if len(one.stars) != maxStars || len(one.lanterns) != 0 || len(one.fire) != 0 || one.forte != 0 {
		t.Fatalf("settled merges leave their stars and nothing else: %d stars, %d lanterns", len(one.stars), len(one.lanterns))
	}
	for i, a := range one.stars {
		if a.u < 0 || a.u > 1 || a.v < 0 || a.v > 1 {
			t.Fatalf("star %d is outside the field", i)
		}
		for _, b := range one.stars[:i] {
			if a.u == b.u && a.v == b.v {
				t.Fatalf("two stars in one place")
			}
		}
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

// The history of a long day leaves branches that are already over (their place went to a newer session, or
// they bore nothing and rest). Arriving removes them at once: the first frame is the scene as it stands, and
// nothing withdraws in the first seconds with no event behind it.
func TestHanamiArrivesAsItStands(t *testing.T) {
	s := newHanamiScene(NewTheme(true))
	at := func(i int) float64 { return float64(i) * 240 } // a session every four minutes for two hours
	for i := 0; i < 30; i++ {
		who := func(e *data.Event) { e.Session, e.Agent, e.Tool = fmt.Sprint("s", i), "a", "shell" }
		if i%3 == 0 { // a third dispatch nobody: they only leave a bare branch
			who = func(e *data.Event) { e.Session, e.Tool = fmt.Sprint("s", i), "shell" }
		} else {
			s.Apply(ev("spawn", at(i), who), true)
			s.Apply(ev("return", at(i)+60, who), true)
		}
		s.Apply(ev("tool", at(i)+30, who), true)
	}
	// and one that dispatched nobody but called a moment ago: bare, and at work
	s.Apply(ev("tool", at(29)+50, func(e *data.Event) { e.Session, e.Tool = "bare", "shell" }), true)
	now := goldenNow.Add(time.Duration(at(29)+70) * time.Second)
	if len(s.branches) <= hanamiSlots {
		t.Fatalf("the history should leave more branches than places for this test: %d", len(s.branches))
	}
	s.Arrive(now)
	n := len(s.branches)
	if n == 0 || n > hanamiSlots {
		t.Fatalf("arrived with %d branches", n)
	}
	working := false
	for _, b := range s.branches {
		working = working || b.session == "bare"
		if b.leaving || b.grow.x != 1 || len(b.blossoms) == 0 && b.session != "bare" {
			t.Fatalf("branch %s arrives leaving, ungrown or bare", b.session)
		}
	}
	if !working {
		t.Fatal("arriving removed a session that is at work, because it has no blossom")
	}
	for f := 0; f < 3*hanamiFPS; f++ {
		s.Step(now)
		for _, b := range s.branches {
			if len(s.branches) != n || b.grow.x < 0.99 {
				t.Fatalf("frame %d: a branch withdraws with no event behind it (%d of %d left)", f, len(s.branches), n)
			}
		}
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
	if len(s.briefs) > maxBriefs || len(s.rings) > maxRings {
		t.Fatalf("unbounded: %d briefs, %d rings", len(s.briefs), len(s.rings))
	}
	for _, br := range s.branches {
		if len(br.beads) > maxBeads {
			t.Fatalf("unbounded: %d beads on a branch", len(br.beads))
		}
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

// The sky ends at the waterline: nothing of the scene's own hangs below it, at any size.
func TestHanamiSkyEndsAtTheWaterline(t *testing.T) {
	for _, sz := range [][2]int{{80, 24}, {100, 30}, {140, 40}} {
		s := newHanamiScene(NewTheme(true))
		s.w, s.h = sz[0], sz[1]
		g := s.geo()
		if g.waterRow >= g.staffTop || g.waterY != float64(4*g.waterRow) || g.cy+g.R*0.88 > g.waterY+0.01 {
			t.Fatalf("%v: no water, or the disc does not stand on it: %+v", sz, g)
		}
		for slot := 0; slot < hanamiSlots; slot++ {
			b := &hBranch{slot: slot}
			for u := 0.0; u <= 1; u += 0.02 {
				if _, y := b.point(g, u); y >= g.waterY {
					t.Fatalf("%v: branch %d reaches the water at %.2f", sz, slot, u)
				}
			}
			if _, y := s.lanternTarget(g, &hLantern{slot: slot}); y >= g.waterY {
				t.Fatalf("%v: lantern %d hangs in the water", sz, slot)
			}
			if _, y := bellAt(g, slot); y >= g.waterY {
				t.Fatalf("%v: bell %d is in the water", sz, slot)
			}
		}
		s.star(g, g.W, g.waterY+10, goldenNow)
		if st := s.stars[0]; st.v*g.waterY >= g.waterY {
			t.Fatalf("%v: a star in the water", sz)
		}
		// the row under the waterline holds the waterline's dots only
		for _, c := range []rune(ansi.Strip(s.draw(false)[g.waterRow])) {
			if c != ' ' && c != '⠁' {
				t.Fatalf("%v: something is drawn through the waterline: %q", sz, c)
			}
		}
	}
	short := newHanamiScene(NewTheme(true))
	short.w, short.h = 80, 12 // no staff, no water
	if g := short.geo(); g.waterRow != g.staffTop || g.waterY != g.H {
		t.Fatalf("a short screen has water: %+v", g)
	}
}

func TestHanamiRingsOnTheWater(t *testing.T) {
	s := newHanamiScene(NewTheme(true))
	s.w, s.h = 100, 30
	g := s.geo()
	call := func(agent string, settled bool) {
		s.Apply(ev("tool", 0, func(e *data.Event) { e.Agent, e.Role, e.Tool = agent, "coder", "edit" }), settled)
	}
	s.Apply(ev("spawn", 0, func(e *data.Event) { e.Agent, e.Role = "a1", "coder" }), false)
	stepFor(s, goldenNow, 3*time.Second)
	call("a1", true)
	if len(s.rings) != 0 {
		t.Fatal("a settled call dropped a ring")
	}
	call("a1", false)
	bx, _ := s.origin(g, s.branches[0], s.branches[0].blossoms[0])
	if r := s.rings; len(r) != 1 || r[0].x != bx || r[0].y < g.waterY || r[0].y >= g.H {
		t.Fatalf("a call drops one ring on the water under its blossom: %+v", s.rings)
	}
	y := s.rings[0].y
	stepFor(s, goldenNow, 2*time.Second)
	call("a1", false)
	if len(s.rings) != 1 || s.rings[0].y != y {
		t.Fatal("one agent lands in two places")
	}
	for i := 0; i < 500; i++ {
		call(fmt.Sprint("x", i), false)
	}
	if len(s.rings) != maxRings {
		t.Fatalf("%d rings, want the cap %d", len(s.rings), maxRings)
	}
	stepFor(s, goldenNow, 2*time.Second)
	if len(s.rings) != 0 {
		t.Fatal("rings never fade")
	}
	s.scale, s.ringDebt = 60, 0 // a replay thins them like sparks
	for i := 0; i < 60; i++ {
		call("a1", false)
	}
	if n := len(s.rings); n < 6 || n > 9 {
		t.Fatalf("a replay at x60 dropped %d rings of 60 calls", n)
	}
	for _, line := range s.draw(false) {
		if strings.Contains(line, "\x1b") {
			t.Fatal("no-colour output has an escape sequence")
		}
	}
}

func TestHanamiWaterMirrorsTheSky(t *testing.T) {
	s := newHanamiScene(NewTheme(false))
	s.w, s.h = 100, 30
	g := s.geo()
	cv := &hCanvas{w: s.w, h: s.h, hi: int(g.waterY), sky: g.waterRow, cells: make([]hCell, s.w*s.h)}
	for x := 0; x < s.w; x++ { // whole rows: the shimmer moves a cell sideways
		cv.cells[(g.waterRow-1)*s.w+x].light = hrgb{100, 60, 20}
		cv.cells[(g.waterRow-3)*s.w+x].light = hrgb{100, 60, 20}
	}
	s.reflect(cv, g)
	if cv.cells[g.waterRow*s.w+40].light.r == 0 || cv.cells[(g.waterRow+1)*s.w+40].light.r == 0 {
		t.Fatal("a lit cell above the waterline has no lit mirror cell")
	}
	if deep, near := cv.cells[(g.staffTop-1)*s.w+40].light.r, cv.cells[g.waterRow*s.w+40].light.r; deep >= near {
		t.Fatalf("the water is not dimmer with depth: %.1f against %.1f", deep, near)
	}
}

func TestHanamiHorizonTellsTheHour(t *testing.T) {
	at := func(h int) hHour {
		s := newHanamiScene(NewTheme(false))
		s.now = time.Date(2026, 11, 14, h, 0, 0, 0, time.Local)
		return s.hour()
	}
	seen := map[hrgb]int{}
	for _, h := range []int{3, 7, 12, 19} {
		seen[at(h).c] = h
	}
	if len(seen) != 4 {
		t.Fatalf("the band's colour is not different at 03:00, 07:00, 12:00 and 19:00: %v", seen)
	}
	if at(3).night != 1 || at(12).night != 0 || newHanamiScene(NewTheme(false)).hour().night != 1 {
		t.Fatal("night is not night: 03:00, noon, and a scene with no events")
	}
	s := newHanamiScene(NewTheme(false))
	s.w, s.h = 100, 30
	g := s.geo()
	cv := &hCanvas{w: s.w, h: s.h, hi: int(g.waterY), sky: g.waterRow, cells: make([]hCell, s.w*s.h)}
	s.horizon(cv, g, at(19))
	for r := g.waterRow - horizonRows; r < g.waterRow; r++ {
		for x := 1; x < s.w; x++ {
			if cv.cells[r*s.w+x].light != cv.cells[r*s.w].light {
				t.Fatalf("row %d is not one colour along its length", r)
			}
		}
	}
	if cv.cells[(g.waterRow-1)*s.w].light.r <= cv.cells[(g.waterRow-horizonRows)*s.w].light.r {
		t.Fatal("the band is not strongest at the line")
	}
}

// The scene keeps time: with no tool calls there is no beat; a steady rate sets the tempo, a bar falls on
// every fourth beat, and a replay of the same events keeps the same tempo.
func TestHanamiKeepsTime(t *testing.T) {
	idle := newHanamiScene(NewTheme(true))
	for i := 0; i < 30*hanamiFPS; i++ {
		idle.Step(goldenNow)
		if idle.amp() != 0 || idle.pulse != 0 || idle.bar != 0 {
			t.Fatalf("frame %d: a beat without tool calls", i)
		}
	}

	live := newHanamiScene(NewTheme(true))
	pulses, bars := 0, 0
	was, wasBar := 0.0, 0.0
	for f := 0; f < 90*hanamiFPS; f++ { // a tool call every 3.35 s: 0.3 a second
		if f%67 == 0 {
			live.Apply(ev("tool", float64(f)*0.05, func(e *data.Event) { e.Tool = "edit" }), false)
		}
		live.Step(goldenNow)
		if live.pulse > was {
			pulses++
		}
		if live.bar > wasBar {
			bars++
		}
		was, wasBar = live.pulse, live.bar
	}
	if bpm := live.bpm(); bpm < 70 || bpm > 95 {
		t.Fatalf("0.3 tool calls a second is %.0f bpm", bpm)
	}
	if pulses < 60 || bars != pulses/4 {
		t.Fatalf("%d pulses and %d bars: a bar is every fourth pulse", pulses, bars)
	}

	replay := newHanamiScene(NewTheme(true))
	replay.scale = 60 // the same events at a minute a second
	for f, sent := 0, 0; f < 40*hanamiFPS; f++ {
		for ; float64(sent) < 0.3*60*float64(f+1)*hanamiSecond; sent++ {
			replay.Apply(ev("tool", float64(f), func(e *data.Event) { e.Tool = "edit" }), false)
		}
		replay.Step(goldenNow)
	}
	if d := replay.bpm() - live.bpm(); d < -8 || d > 8 {
		t.Fatalf("a replay at x60 keeps %.0f bpm, live %.0f", replay.bpm(), live.bpm())
	}
	// the beat shows: the disc is larger on a pulse than between them
	live.pulse, live.bar = 1, 0
	a := strings.Join(live.draw(false), "\n")
	live.pulse = 0
	if a == strings.Join(live.draw(false), "\n") {
		t.Fatal("the pulse does not show in the disc")
	}
}

// The flow is visible both ways: a brief runs out and the blossom opens when it arrives; work rises as beads;
// a question's light goes to its bell and an answer's comes home.
func TestHanamiCirculation(t *testing.T) {
	s := newHanamiScene(NewTheme(true))
	s.w, s.h = 100, 30
	agent := func(e *data.Event) { e.Agent, e.Role = "a1", "coder" }
	s.Apply(ev("spawn", 0, agent), false)
	bl := s.branches[0].blossoms[0]
	if len(s.briefs) != 1 || !bl.bud {
		t.Fatal("a spawn sends a brief and leaves a bud")
	}
	for i := 0; i < int(0.8*hanamiFPS); i++ {
		s.Step(goldenNow)
	}
	if bl.open.x > 0.01 || len(s.briefs) != 1 {
		t.Fatalf("the blossom opened before its brief arrived: open %.2f", bl.open.x)
	}
	stepFor(s, goldenNow, 600*time.Millisecond)
	if len(s.briefs) != 0 || bl.bud || bl.open.x <= 0.01 {
		t.Fatalf("the brief arrived and the blossom did not open: %d briefs, open %.2f", len(s.briefs), bl.open.x)
	}
	stepFor(s, goldenNow, 2*time.Second)
	if bl.open.x < 0.9 {
		t.Fatal("the blossom does not open")
	}

	// back before the brief arrives: it opens at once, closes, and no light is left running
	s.Apply(ev("spawn", 5, func(e *data.Event) { e.Agent, e.Role = "a2", "reviewer" }), false)
	s.Apply(ev("return", 5, func(e *data.Event) { e.Agent, e.Role = "a2", "reviewer" }), false)
	if len(s.briefs) != 0 || s.branches[0].blossoms[1].bud || s.branches[0].blossoms[1].closed.IsZero() {
		t.Fatal("a return before the brief arrived leaves a stuck light or a bud")
	}

	// sap: a call sends a bead, six at most in flight, and they arrive
	for i := 0; i < 40; i++ {
		s.Apply(ev("tool", 6, func(e *data.Event) { e.Tool = "edit" }), false)
		s.Apply(ev("tokens", 6, func(e *data.Event) { e.Out = 2000; e.Agent, e.Role = "a1", "coder" }), false)
	}
	if n := len(s.branches[0].beads); n != maxBeads {
		t.Fatalf("%d beads in flight, want %d", n, maxBeads)
	}
	if b := s.branches[0].beads[0]; b.k >= 0.5 && b.bl != nil {
		t.Fatalf("a bead of a bare call is as bright as one of output: %+v", b)
	}
	stepFor(s, goldenNow, 2*time.Second)
	if len(s.branches[0].beads) != 0 {
		t.Fatal("beads never arrive")
	}
	g := s.geo()
	b := s.branches[0]
	if x, y := b.run(g, nil, 1); x != func() float64 { x, _ := b.point(g, b.grow.x); return x }() || y >= g.waterY {
		t.Fatal("sap does not end at the branch's tip")
	}

	// home: the rim takes the colour when the last petal reaches the disc
	stepFor(s, goldenNow, 6*time.Second)
	s.Apply(ev("return", 10, func(e *data.Event) { e.Agent, e.Role = "a1", "coder" }), false)
	if s.home > 0.01 {
		t.Fatal("the rim flashed before any petal came home")
	}
	flashed := false
	for i := 0; i < 5*hanamiFPS && len(s.petals) > 0; i++ {
		s.Step(goldenNow)
		flashed = flashed || s.home > 0.5
	}
	if !flashed {
		t.Fatal("the last petal's arrival did not flash the rim")
	}

	// a question: the bell appears when its light arrives, at once when it is history, and an answer removes it
	q := func(e *data.Event) { e.Slug = "visualiser" }
	s.Apply(ev("question", 20, q), false)
	if b := s.bells[0]; b.state != bellGoing || b.p != 0 {
		t.Fatal("the bell is there before its light")
	}
	stepFor(s, goldenNow, 500*time.Millisecond)
	if s.bells[0].state != bellGoing {
		t.Fatal("the bell is there before its light arrived")
	}
	stepFor(s, goldenNow, 600*time.Millisecond)
	if s.bells[0].state != bellWaiting || len(s.ripples) == 0 {
		t.Fatal("the light arrived and the bell did not ring")
	}
	s.Apply(ev("answer", 30, q), false)
	stepFor(s, goldenNow, 2*time.Second)
	if len(s.bells) != 0 {
		t.Fatal("the answer did not remove the bell")
	}
	s.Apply(ev("question", 40, q), true)
	if len(s.bells) != 1 || s.bells[0].state != bellWaiting {
		t.Fatal("a question from history waits at its bell at once")
	}
	if x, y := bellAt(g, 0); x > 10 || y >= g.waterY {
		t.Fatalf("the bell is not at the left edge of the sky: %.0f, %.0f", x, y)
	}
}

// Small words: a role when a blossom opens, a lantern's number and its merge, a waiting question's slug.
func TestHanamiSmallWords(t *testing.T) {
	s := newHanamiScene(NewTheme(true))
	s.w, s.h = 100, 30
	s.Apply(ev("spawn", 0, func(e *data.Event) { e.Agent, e.Role = "a1", "coder" }), true)
	if len(s.words) != 0 {
		t.Fatal("a settled blossom says its role")
	}
	s.Apply(ev("spawn", 0, func(e *data.Event) { e.Agent, e.Role = "a2", "reviewer" }), false)
	stepFor(s, goldenNow, 500*time.Millisecond)
	if len(s.words) != 0 {
		t.Fatal("the role is said before the brief arrived")
	}
	stepFor(s, goldenNow, 700*time.Millisecond)
	if len(s.words) != 1 || s.words[0].text != "reviewer" {
		t.Fatalf("the role is not said when the blossom opens: %+v", s.words)
	}
	stepFor(s, goldenNow, 1500*time.Millisecond) // in the hold: shown even without colour
	if !strings.Contains(strings.Join(s.draw(false), "\n"), "reviewer") {
		t.Fatal("the role is not on screen")
	}
	stepFor(s, goldenNow, 6*time.Second)
	if len(s.words) != 0 || strings.Contains(strings.Join(s.draw(false), "\n"), "reviewer") {
		t.Fatal("the role is still there 6 s later")
	}

	pr := func(e *data.Event) { e.PR = 31 }
	s.Apply(ev("pr", 0, pr), false)
	if len(s.words) != 1 || s.words[0].text != "#31" {
		t.Fatalf("a lantern's number: %+v", s.words)
	}
	s.Apply(ev("merge", 1, pr), false)
	stepFor(s, goldenNow, 6*time.Second)
	s.Apply(ev("pr", 2, func(e *data.Event) { e.PR = 32 }), true) // from history: no word
	s.Apply(ev("merge", 2, func(e *data.Event) { e.PR = 33 }), true)
	if len(s.words) > 1 {
		t.Fatalf("history spoke: %+v", s.words)
	}
	for i := 0; i < 20; i++ {
		s.say(hWord{text: "x", ttl: 4})
	}
	if len(s.words) != maxWords {
		t.Fatalf("%d words on screen, at most %d", len(s.words), maxWords)
	}

	s.Apply(ev("question", 3, func(e *data.Event) { e.Slug = "quiet-lamps" }), true)
	stepFor(s, goldenNow, 30*time.Second)
	if !strings.Contains(strings.Join(s.draw(false), "\n"), "quiet-lamps") {
		t.Fatal("a waiting question's slug is not beside its bell")
	}
	// a word does not overwrite the disc's mark
	g := s.geo()
	s.words = nil
	s.say(hWord{text: strings.Repeat("w", 60), ttl: 100, x: g.cx + 90, y: g.cy})
	stepFor(s, goldenNow, time.Second)
	if row := ansi.Strip(s.draw(false)[int(g.cy/4)]); !strings.Contains(row, brandKanji) || !strings.Contains(row, "www") {
		t.Fatalf("the word is missing, or it ate the disc's mark: %q", row)
	}
}
