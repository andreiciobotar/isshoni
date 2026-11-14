package tui

import (
	"strings"
	"testing"

	"github.com/charmbracelet/x/ansi"

	"isshoni/internal/data"
)

// pulseBand is the fixture's Pulse page without colour, trailing spaces cut, after edit changed its pulse.
func pulseBand(t *testing.T, w int, edit func(*data.Pulse)) string {
	t.Helper()
	doc := kaizenDoc(t)
	if edit != nil {
		edit(doc.Pulse)
	}
	return pulseFrame(t, doc, w)
}

func pulseFrame(t *testing.T, doc *data.Kaizen, w int) string {
	m := fakeModel(true, w, 100)
	setKaizen(m, doc, pagePulse)
	var out []string
	for _, l := range strings.Split(plain(m), "\n") {
		out = append(out, strings.TrimRight(l, " "))
	}
	return strings.Join(out, "\n")
}

func TestPulseBandRowsAndFigures(t *testing.T) {
	out := pulseBand(t, 140, nil)
	squeezed := ""
	for _, l := range strings.Split(out, "\n") {
		squeezed += strings.Join(strings.Fields(l), " ") + "\n"
	}
	for _, want := range []string{
		"TODAY AGAINST YESTERDAY", "today 11-14", "yesterday 11-13",
		"all-in cost / merged PR", "$10.25 n=12", "$14.10* n=5",
		"tasks + dispatcher + site manager", "$100 + $20.25 + $2.25",
		"first pass small", "75% 3/4", "100% 2/2", "medium", "33% 1/3", "large", "0% 0/1", "no size stated", "50% 2/4",
		"hand-over → merge p90", "36m44s n=11", "10m00s n=5",
		"hand-over → approval p90", "30m00s n=6 (2 backfilled)", "approval → merge p90", "4m00s n=6",
		"send-backs that changed nothing", "1 of 3",
		"conflicted base merges / merged PR", "0.36 4 in 11 PRs", "(from git history)",
	} {
		if !strings.Contains(squeezed, want) {
			t.Errorf("the band lacks %q:\n%s", want, out)
		}
	}
	if strings.Index(out, "TODAY AGAINST YESTERDAY") > strings.Index(out, "LANDED") {
		t.Errorf("the band is above the tiles:\n%s", out)
	}
}

func TestPulseUnknownsAreDashesNeverZero(t *testing.T) {
	out := pulseBand(t, 140, func(p *data.Pulse) {
		d := &p.Today
		d.Merged = 0
		d.Cost.PerMerged, d.Conflicts.PerMerged = nil, nil
		d.FirstPass.Small.Of = 0
		d.Handover.ToMerge.P90S = nil
		d.Handover.SentBack, d.Handover.SentBackUnchanged = nil, nil
		p.QueueSince = nil
	})
	// today is a dash; yesterday's figure follows it
	for row, tail := range map[string]string{
		"all-in cost / merged PR":            "- $14.10* n=5",
		"first pass small":                   "- 100% 2/2",
		"hand-over → merge p90":              "- 10m00s n=5",
		"send-backs that changed nothing":    "- -",
		"conflicted base merges / merged PR": "- -",
	} {
		if got := strings.Join(strings.Fields(lineWith(t, out, row)), " "); !strings.HasSuffix(got, tail) {
			t.Errorf("%s: want it to end in %q: %q", row, tail, got)
		}
	}
	if !strings.Contains(out, "send-backs: no queue log yet") {
		t.Errorf("no queue log is said once under the table:\n%s", out)
	}
	if !strings.Contains(pulseBand(t, 140, nil), "send-backs: the queue log starts 11-14") {
		t.Error("yesterday's send-backs say where the queue log starts")
	}
	if out := pulseBand(t, 140, func(p *data.Pulse) {
		p.Yesterday.Handover.SentBack, p.Yesterday.Handover.SentBackUnchanged = ptr(0), ptr(0)
	}); !strings.Contains(out, "0 of 0") || strings.Contains(out, "send-backs: the queue log") {
		t.Errorf("a recorded zero is shown as zero, with no note:\n%s", out)
	}
}

func TestPulseWithoutThePulseKeyRendersThePageAsBefore(t *testing.T) {
	doc := kaizenDoc(t)
	doc.Pulse = nil
	out := pulseFrame(t, doc, 140)
	if strings.Contains(out, "TODAY AGAINST") || strings.Contains(out, "from git history") {
		t.Fatalf("no pulse, no band:\n%s", out)
	}
	if lines := strings.Split(out, "\n"); !strings.Contains(lines[2], "LANDED") {
		t.Fatalf("the tiles start the page:\n%s", out)
	}
}

func TestPulseGitErrorShowsAsAWarningLine(t *testing.T) {
	if out := pulseBand(t, 100, nil); strings.Contains(out, "⚠") {
		t.Fatalf("no error, no warning:\n%s", out)
	}
	out := pulseBand(t, 100, func(p *data.Pulse) { p.Yesterday.Conflicts.Error = ptr("git log timed out after 10 s") })
	if !strings.Contains(out, "⚠ conflicted base merges 2026-11-13: git log timed out after 10 s") {
		t.Fatalf("the git error:\n%s", out)
	}
}

func TestPulseBadValuesAreWarningColoured(t *testing.T) {
	bad := func(edit func(*data.Pulse), fig string) bool {
		doc := kaizenDoc(t)
		edit(doc.Pulse)
		m := fakeModel(false, 140, 100)
		setKaizen(m, doc, pagePulse)
		return strings.Contains(m.Render(), m.th.on(m.th.Bg).b(fig, m.th.Orange))
	}
	good := func(p *data.Pulse) {
		d := &p.Today
		d.Handover.ToMerge.P90S, d.Handover.ApprovalToMerge.P90S = ptr(900), ptr(900)
		d.Handover.SentBackUnchanged = ptr(0)
		d.Conflicts.PerMerged = ptr(0.2)
	}
	for _, c := range []struct {
		name, fig string
		set       func(*data.Pulse)
	}{
		{"hand-over to merge over 15 min", "15m01s", func(p *data.Pulse) { d := &p.Today; d.Handover.ToMerge.P90S = ptr(901) }},
		{"approval to merge over 15 min", "15m01s", func(p *data.Pulse) { d := &p.Today; d.Handover.ApprovalToMerge.P90S = ptr(901) }},
		{"a send-back that changed nothing", "1 of 3", func(p *data.Pulse) { d := &p.Today; d.Handover.SentBackUnchanged = ptr(1) }},
		{"conflicts over 0.2", "0.21", func(p *data.Pulse) { d := &p.Today; d.Conflicts.PerMerged = ptr(0.21) }},
	} {
		if bad(good, c.fig) {
			t.Errorf("%s: at the threshold nothing is coloured", c.name)
		}
		if !bad(func(p *data.Pulse) { good(p); c.set(p) }, c.fig) {
			t.Errorf("%s: not in the warning colour", c.name)
		}
	}
}

func TestPulseFitsNarrowWithoutWrapping(t *testing.T) {
	out := pulseBand(t, 60, nil)
	for _, l := range strings.Split(out, "\n") {
		if ansi.StringWidth(l) > 60 {
			t.Errorf("wider than the window: %q", l)
		}
	}
	if strings.Contains(out, "tasks + dispatcher") || !strings.Contains(out, "unstated") || !strings.Contains(out, "(from git history)") {
		t.Errorf("narrow drops the breakdown and shortens labels, keeps the mark:\n%s", out)
	}
}
