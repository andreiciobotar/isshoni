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

// noMergeDoc is the fixture on a day when nothing merged yet: today's lead figures and
// verdicts are unknown, the day's spend is not.
func noMergeDoc(t testing.TB) *data.Kaizen {
	d := kaizenDoc(t)
	today := &d.Pulse.Today
	*today = data.PulseDay{Date: today.Date, To: today.To, Days: 1, Cost: today.Cost}
	today.Cost.PerMerged = nil
	d.Pulse.Days[len(d.Pulse.Days)-1] = *today
	d.Pulse.Verdicts = data.PulseVerdicts{}
	return d
}

// oneDayDoc is the fixture with yesterday as the whole baseline, the range a day.
func oneDayDoc(t testing.TB) *data.Kaizen {
	d := kaizenDoc(t)
	p := d.Pulse
	y := p.Days[len(p.Days)-2]
	p.Baseline, p.Days = y, p.Days[len(p.Days)-2:]
	p.Verdicts = data.PulseVerdicts{Cost: ptr("same"), FirstPass: ptr("worse"), ApprovalToMerge: ptr("few"), Conflicts: ptr("worse")}
	return d
}

func TestPulseBandRowsAndFigures(t *testing.T) {
	out := pulseBand(t, 140, nil)
	for _, want := range []string{
		"TODAY AGAINST THE 7 DAYS BEFORE", "today 11-14", "11-07 to 11-13", "each day, today last",
		"all-in cost / merged PR $10.25 n=12 ↓ 27% $14.00* n=50",
		"tasks · dispatcher · site manager $100 · $20.25 · $2.25 $600 · $90.00 · $10.00",
		"first pass 50% 6/12 ↓ 34 pts 84% 42/50",
		"small · medium · large · unstated 3/4 · 1/3 · 0/1 · 2/4 19/21 · 12/17 · 3/4 · 8/8",
		"approval → merge, median 2m30s n=6 p90 4m00s ≈ 2m40s n=30 p90 7m00s",
		"hand-over → merge · → approval, p90 36m44s · 30m00s n=11 55m00s · 40m00s n=46",
		"send-backs that changed nothing 1 of 3 -",
		"conflicted base merges / PR 0.36 4 in 11 PRs ↑ 179% 0.13 6 in 46 PRs",
	} {
		if !strings.Contains(squeeze(out), want) {
			t.Errorf("the band lacks %q:\n%s", want, out)
		}
	}
	lineWith(t, out, "send-backs: the queue log starts 11-14", " · approvals backfilled: 2 today, 8 in the baseline") // one note line
	if strings.Index(out, "TODAY AGAINST") > strings.Index(out, "LANDED") {
		t.Errorf("the band is above the tiles:\n%s", out)
	}
	for _, gone := range []string{"yesterday", "(git history)", "approval → merge p90", "conflicted base merges 4"} {
		if strings.Contains(out, gone) {
			t.Errorf("%q is for a one-day baseline or gone:\n%s", gone, out)
		}
	}
}

func TestPulseTitleAndHeadsFollowTheBaseline(t *testing.T) {
	one := pulseFrame(t, oneDayDoc(t), 140)
	for _, want := range []string{"TODAY AGAINST YESTERDAY", "yesterday 11-13"} {
		if !strings.Contains(one, want) {
			t.Errorf("one-day baseline lacks %q:\n%s", want, one)
		}
	}
	narrow := pulseFrame(t, oneDayDoc(t), 60)
	if !strings.Contains(narrow, "TODAY VS YESTERDAY") {
		t.Errorf("narrow one-day title:\n%s", narrow)
	}
	if n := pulseBand(t, 60, nil); !strings.Contains(n, "TODAY VS 7D BEFORE") {
		t.Errorf("narrow title names the days:\n%s", n)
	}
}

func TestPulseTodayWithNothingMergedShowsDashesAndTheSpend(t *testing.T) {
	out := squeeze(pulseFrame(t, noMergeDoc(t), 140))
	for _, want := range []string{
		"all-in cost / merged PR - n=0 $14.00* n=50",
		"tasks · dispatcher · site manager $100 · $20.25 · $2.25",
		"first pass - n=0 84% 42/50",
		"approval → merge, median - n=0 2m40s n=30",
		"conflicted base merges / PR - n=0 0.13 6 in 46 PRs",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("the band lacks %q:\n%s", want, out)
		}
	}
	if strings.ContainsAny(out[:strings.Index(out, "LANDED")], "↑↓≈") {
		t.Errorf("no delta without a verdict:\n%s", out)
	}
}

func TestPulseUnknownsAreDashesNeverZero(t *testing.T) {
	out := pulseBand(t, 140, func(p *data.Pulse) {
		d := &p.Baseline
		d.Merged = 0
		d.Cost.PerMerged, d.Conflicts.PerMerged = nil, nil
		d.FirstPass.Small.Of = 0
		d.Handover.ToMerge.P90S = nil
		p.QueueSince = nil
	})
	for row, tail := range map[string]string{
		"all-in cost / merged PR":         "- n=0",
		"small · medium":                  "3/4 · 1/3 · 0/1 · 2/4 - · 12/17 · 3/4 · 8/8",
		"hand-over → merge ·":             "36m44s · 30m00s n=11 - · 40m00s",
		"send-backs that changed nothing": "1 of 3 -",
		"conflicted base merges / PR":     "4 in 11 PRs - n=46",
	} {
		if got := strings.Join(strings.Fields(lineWith(t, out, row)), " "); !strings.Contains(got, tail) {
			t.Errorf("%s: want %q in %q", row, tail, got)
		}
	}
	if !strings.Contains(out, "send-backs: no queue log yet") {
		t.Errorf("no queue log is said once under the table:\n%s", out)
	}
	if out := pulseBand(t, 140, func(p *data.Pulse) {
		p.Baseline.Handover.SentBack, p.Baseline.Handover.SentBackUnchanged = ptr(0), ptr(0)
	}); !strings.Contains(out, "0 of 0") || strings.Contains(out, "send-backs: the queue log") {
		t.Errorf("a recorded zero is shown as zero, with no note:\n%s", out)
	}
}

func TestPulseWithoutThePulseKeyRendersThePageAsBefore(t *testing.T) {
	doc := kaizenDoc(t)
	doc.Pulse = nil
	out := pulseFrame(t, doc, 140)
	if strings.Contains(out, "TODAY AGAINST") || strings.Contains(out, "approval → merge") {
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
	out := pulseBand(t, 100, func(p *data.Pulse) { p.Baseline.Conflicts.Error = ptr("git log timed out after 10 s") })
	if !strings.Contains(out, "⚠ conflicted base merges baseline: git log timed out after 10 s") {
		t.Fatalf("the git error:\n%s", out)
	}
}

func TestPulseColourFollowsTheVerdict(t *testing.T) {
	has := func(edit func(*data.Pulse), paint func(*model, string) string, fig string) bool {
		doc := kaizenDoc(t)
		edit(doc.Pulse)
		m := fakeModel(false, 140, 100)
		setKaizen(m, doc, pagePulse)
		return strings.Contains(m.Render(), paint(m, fig))
	}
	on := func(m *model) paint { return m.th.on(m.th.Bg) }
	orange := func(m *model, s string) string { return on(m).b(s, m.th.Orange) }
	green := func(m *model, s string) string { return on(m).b(s, m.th.Green) }
	muted := func(m *model, s string) string { return on(m).b(s, m.th.Muted) }
	all := func(v string) func(*data.Pulse) {
		return func(p *data.Pulse) {
			p.Verdicts = data.PulseVerdicts{Cost: &v, FirstPass: &v, ApprovalToMerge: &v, Conflicts: &v}
		}
	}
	keep := func(*data.Pulse) {}
	// the fixture: cost fell (better), first pass fell (worse), conflicts rose (worse)
	if !has(keep, green, "↓ 27%") || !has(keep, orange, "↓ 34 pts") || !has(keep, orange, "↑ 179%") {
		t.Error("better is green and worse orange, whichever way the figure moved")
	}
	for _, fig := range []string{"↓ 27%", "↓ 34 pts", "↑ 179%"} {
		if has(all("few"), orange, fig) || has(all("few"), green, fig) || !has(all("few"), muted, fig) {
			t.Errorf("few keeps %q and mutes it", fig)
		}
	}
	if !has(all("same"), muted, "≈") {
		t.Error("same is a muted ≈")
	}
	text := func(m *model, s string) string { return on(m).t(s, m.th.Text) }
	for _, v := range []string{"few", "same"} {
		for _, g := range "▁▂▃▄▅▆▇█" {
			if has(all(v), text, strings.Repeat(string(g), 2)) {
				t.Errorf("with %s today's sparkline cell is muted, not text: %q", v, g)
			}
		}
	}
	// the human's waiting never colours: the hand-over rows have no colour
	if has(func(*data.Pulse) {}, orange, "36m44s") || has(func(*data.Pulse) {}, green, "36m44s") {
		t.Error("hand-over → merge is not coloured")
	}
}

func TestPulseDeltaFromAZeroBaselineIsTheArrowAlone(t *testing.T) {
	zero := func(p *data.Pulse) {
		p.Baseline.Cost.PerMerged = new(0.0)
		p.Verdicts.Cost = new("worse")
	}
	band := pulseBand(t, 140, zero)
	if strings.Contains(band, "↑ 100%") || !strings.Contains(band, "↑ ") {
		t.Errorf("a change from zero has an arrow and no percent:\n%s", band)
	}
	m := fakeModel(false, 140, 100)
	doc := kaizenDoc(t)
	zero(doc.Pulse)
	setKaizen(m, doc, pagePulse)
	if !strings.Contains(m.Render(), m.th.on(m.th.Bg).b("↑", m.th.Orange)) {
		t.Error("the arrow keeps the verdict's colour")
	}
}

func TestPulseSparklineHasACellPerDayAndADotForNoValue(t *testing.T) {
	out := pulseBand(t, 140, nil)
	row := lineWith(t, out, "approval → merge, median")
	cells := []rune(strings.TrimSpace(row[strings.LastIndex(row, "p90 7m00s")+len("p90 7m00s"):]))
	if len(cells) != 16 || cells[0] != '·' || cells[1] != '·' || cells[14] == '·' || cells[15] != cells[14] {
		t.Errorf("eight days of two cells, the first without a value, today last: %q", string(cells))
	}
	for _, r := range cells[2:] {
		if !strings.ContainsRune("▁▂▃▄▅▆▇█", r) {
			t.Errorf("a day with a value is an eighth block: %q", string(cells))
		}
	}
	if !strings.Contains(string(cells), "█") {
		t.Errorf("the largest day fills the cell: %q", string(cells))
	}
	if strings.ContainsAny(strings.Split(pulseBand(t, 60, nil), "\n")[2], "▁▂▃▄▅▆▇█·") {
		t.Error("narrow has no sparkline")
	}
}

func TestPulseSparklineShowsAt100OneGapAfterTheBaseline(t *testing.T) {
	row := lineWith(t, pulseBand(t, 100, nil), "approval → merge, median")
	tail := row[strings.LastIndex(row, "p90 7m00s")+len("p90 7m00s"):]
	if !strings.HasPrefix(tail, " ") || strings.HasPrefix(tail, "  ") || len([]rune(strings.TrimSpace(tail))) < 4 {
		t.Errorf("at 100 the sparkline starts one gap after the baseline and has four cells or more: %q", tail)
	}
}

func TestPulseBandIsAtMostElevenLinesAtWideAndNothingExceedsTheWidth(t *testing.T) {
	lines := strings.Split(pulseBand(t, 140, nil), "\n")
	first, tiles := 0, 0
	for i, l := range lines {
		if strings.Contains(l, "TODAY AGAINST") {
			first = i
		}
		if strings.Contains(l, "LANDED") {
			tiles = i
		}
	}
	if n := tiles - first; n > 11 {
		t.Errorf("the band at 140 is %d lines, want at most 11", n)
	}
	for _, w := range []int{60, 100, 140} {
		for _, l := range strings.Split(pulseBand(t, w, nil), "\n") {
			if ansi.StringWidth(l) > w {
				t.Errorf("at %d: line wider than the window: %q", w, l)
			}
		}
	}
}

func TestPulseNoLineIsWiderThanThePage(t *testing.T) {
	for _, w := range []int{60, 80, 99, 100, 120, 140, 200} {
		for name, doc := range map[string]*data.Kaizen{"fixture": kaizenDoc(t), "no merge": noMergeDoc(t), "one day": oneDayDoc(t)} {
			doc.Pulse.Today.Conflicts.Error = ptr(strings.Repeat("git said no ", 20))
			out := pulseFrame(t, doc, w)
			for _, l := range strings.Split(out, "\n") {
				if ansi.StringWidth(l) > w {
					t.Errorf("%s at %d: wider than the window: %q", name, w, l)
				}
			}
		}
	}
	out := pulseBand(t, 60, nil)
	if strings.Contains(out, "tasks · dispatcher") || !strings.Contains(out, "unstated") || !strings.Contains(out, "conflicts / PR") {
		t.Errorf("narrow drops the breakdown and shortens labels:\n%s", out)
	}
}
