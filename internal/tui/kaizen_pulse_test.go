package tui

import (
	"image/color"
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

// bandOf is the band: the page up to the tile row.
func bandOf(out string) string { return out[:strings.Index(out, "LANDED")] }

// cardRow is the text of the card whose label is label, on the row-th line from the label's.
func cardRow(t *testing.T, out, label string, row int) string {
	t.Helper()
	lines := strings.Split(out, "\n")
	for i, l := range lines {
		if idx := strings.Index(l, label); idx >= 0 {
			rs := []rune(lines[i+row])[len([]rune(l[:idx])):]
			seg := string(rs)
			if end := strings.Index(seg, "▌"); end >= 0 {
				seg = seg[:end]
			}
			return strings.TrimSpace(seg)
		}
	}
	t.Fatalf("no card %q:\n%s", label, out)
	return ""
}

func TestPulseBandCardsAndFigures(t *testing.T) {
	out := pulseBand(t, 140, nil)
	for _, want := range []string{
		"TODAY AGAINST THE 7 DAYS BEFORE",
		"$10.25 n=12", "↓ 27% vs $14.00* n=50", "$100 · $20.25 · $2.25",
		"50% 6/12", "↓ 34 pts vs 84% 42/50", "3/4 · 1/3 · 0/1 · 2/4",
		"2m30s n=6", "≈ vs 2m40s n=30", "p90 4m00s",
		"0.36 n=11", "↑ 179% vs 0.13 n=46", "4 in 11 PRs",
		// the faint block: what the cards leave out, the baseline's figures among it
		"hand-over → merge p90 · → approval p90: today 36m44s · 30m00s (n=11), baseline 55m00s · 40m00s (n=46)",
		"baseline 11-07 to 11-13: parts $600 · $90.00 · $10.00 · first pass 19/21 · 12/17 · 3/4 · 8/8 · approval → merge p90 7m00s · conflicts 6 in",
		"46 PRs",
		"send-backs that changed nothing: 1 of 3 today · - baseline · send-backs: the queue log starts 11-14",
		"approvals recorded: 6 today (2 backfilled), 30 of 46 in the baseline (8 backfilled)",
	} {
		if !strings.Contains(squeeze(out), want) {
			t.Errorf("the band lacks %q:\n%s", want, out)
		}
	}
	if strings.Count(out, "▌ all-in cost") != 1 || !strings.Contains(squeeze(out), "approval → merge, median") {
		t.Errorf("four cards:\n%s", out)
	}
	if strings.Index(out, "TODAY AGAINST") > strings.Index(out, "LANDED") {
		t.Errorf("the band is above the tiles:\n%s", out)
	}
	for _, gone := range []string{"yesterday", "(git history)", "approvals backfilled"} {
		if strings.Contains(out, gone) {
			t.Errorf("%q is for a one-day baseline or gone:\n%s", gone, out)
		}
	}
}

func TestPulseTitleFollowsTheBaseline(t *testing.T) {
	one := pulseFrame(t, oneDayDoc(t), 140)
	for _, want := range []string{"TODAY AGAINST YESTERDAY", "baseline 11-13: parts"} {
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
	out := squeeze(bandOf(pulseFrame(t, noMergeDoc(t), 140)))
	for _, want := range []string{
		"- n=0", "vs $14.00* n=50", "$100 · $20.25 · $2.25",
		"vs 84% 42/50", "vs 2m40s n=30", "vs 0.13 n=46",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("the band lacks %q:\n%s", want, out)
		}
	}
	if strings.ContainsAny(out, "↑↓≈") {
		t.Errorf("no delta without a verdict:\n%s", out)
	}
}

func TestPulseUnknownsAreDashesNeverZero(t *testing.T) {
	out := squeeze(pulseBand(t, 140, func(p *data.Pulse) {
		d := &p.Baseline
		d.Merged = 0
		d.Cost.PerMerged, d.Conflicts.PerMerged = nil, nil
		d.FirstPass.Small.Of = 0
		d.Handover.ToMerge.P90S = nil
		p.QueueSince = nil
	}))
	for _, want := range []string{
		"vs - n=0", "vs - n=46",
		"baseline - · 40m00s (n=46)",
		"first pass - · 12/17 · 3/4 · 8/8",
		"1 of 3 today · - baseline",
		"send-backs: no queue log yet",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("the band lacks %q:\n%s", want, out)
		}
	}
	if out := pulseBand(t, 140, func(p *data.Pulse) {
		p.Baseline.Handover.SentBack, p.Baseline.Handover.SentBackUnchanged = ptr(0), ptr(0)
	}); !strings.Contains(out, "0 of 0 baseline") || strings.Contains(out, "send-backs: the queue log") {
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
		return strings.Contains(bandOf(m.Render()), paint(m, fig))
	}
	on := func(m *model) paint { return m.th.on(m.th.Surface) }
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
	// the card's left bar takes the verdict's colour; muted for same, few and none
	stripe := func(col func(*model) color.Color) func(*model, string) string {
		return func(m *model, s string) string { return on(m).t(s, col(m)) }
	}
	if !has(keep, stripe(func(m *model) color.Color { return m.th.Green }), "▌") || !has(keep, stripe(func(m *model) color.Color { return m.th.Orange }), "▌") {
		t.Error("the bar is green for better and orange for worse")
	}
	if has(all("few"), stripe(func(m *model) color.Color { return m.th.Orange }), "▌") || has(all("same"), stripe(func(m *model) color.Color { return m.th.Orange }), "▌") ||
		!has(all("few"), stripe(func(m *model) color.Color { return m.th.Muted }), "▌") {
		t.Error("the bar is muted for few and same")
	}
	// today's strip cell: the verdict's colour for better and worse, the text colour (not muted) for the rest
	cell := func(edit func(*data.Pulse), col func(*model) color.Color) bool {
		for _, g := range "▁▂▃▄▅▆▇█" {
			if has(edit, stripe(col), strings.Repeat(string(g), 2)) {
				return true
			}
		}
		return false
	}
	for _, v := range []string{"few", "same"} {
		if !cell(all(v), func(m *model) color.Color { return m.th.Text }) {
			t.Errorf("with %s today's strip cell is in the text colour", v)
		}
	}
	if !cell(all("better"), func(m *model) color.Color { return m.th.Green }) || !cell(all("worse"), func(m *model) color.Color { return m.th.Orange }) {
		t.Error("today's strip cell takes the verdict's colour")
	}
	// the human's waiting never colours
	if has(keep, orange, "36m44s") || has(keep, green, "36m44s") {
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
	if !strings.Contains(m.Render(), m.th.on(m.th.Surface).b("↑", m.th.Orange)) {
		t.Error("the arrow keeps the verdict's colour")
	}
}

func TestPulseStripHasACellPerDayAndADotForNoValue(t *testing.T) {
	out := pulseBand(t, 140, nil)
	cells := []rune(cardRow(t, out, "approval → merge, median", 3))
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
		t.Error("narrow has no strip")
	}
}

func TestPulseStripCaptionAppearsBelowThreeDaysWithData(t *testing.T) {
	only := func(k int) func(*data.Pulse) {
		return func(p *data.Pulse) {
			for i := range p.Days {
				if i >= k {
					p.Days[i].Cost.PerMerged = nil
				}
			}
		}
	}
	for _, w := range []int{140, 100} {
		two := cardRow(t, pulseBand(t, w, only(2)), "all-in cost", 3)
		if w == 140 && !strings.HasSuffix(two, "2 of 8 days with data") {
			t.Errorf("at %d with data on 2 of 8 days the strip says so: %q", w, two)
		}
		if w == 100 && (!strings.HasPrefix(two, "▇▇██") || strings.Contains(two, "days with data")) {
			t.Errorf("at %d the caption does not fit, the strip stays: %q", w, two)
		}
		if three := cardRow(t, pulseBand(t, w, only(3)), "all-in cost", 3); strings.Contains(three, "days with data") {
			t.Errorf("at %d with data on 3 of 8 days there is no caption: %q", w, three)
		}
	}
	for _, label := range []string{"all-in cost", "first pass", "approval → merge", "conflicts / PR"} {
		if strip := cardRow(t, pulseBand(t, 100, nil), label, 3); !strings.ContainsAny(strip, "▁▂▃▄▅▆▇█") {
			t.Errorf("%s at 100: the strip stays: %q", label, strip)
		}
	}
	for _, label := range []string{"all-in cost", "first pass", "approval → merge", "conflicts / PR"} {
		if strip := cardRow(t, pulseFrame(t, oneDayDoc(t), 100), label, 3); !strings.ContainsAny(strip, "▁▂▃▄▅▆▇█") {
			t.Errorf("one day, %s at 100: the strip stays when the caption does not fit: %q", label, strip)
		}
	}
}

func TestPulseDeltaLineDropsTheBaselineCountWhenTooWide(t *testing.T) {
	wide := func(p *data.Pulse) { p.Baseline.Merged = 50000 }
	if line := cardRow(t, pulseBand(t, 100, wide), "all-in cost", 2); !strings.Contains(line, "↓ 27% vs $14.00*") || strings.Contains(line, "n=") {
		t.Errorf("at 100 the delta line drops the baseline count: %q", line)
	}
	if line := cardRow(t, pulseBand(t, 200, wide), "all-in cost", 2); !strings.Contains(line, "vs $14.00* n=50000") {
		t.Errorf("at 200 it keeps the count: %q", line)
	}
}

func TestPulseCardsHaveNoCutAtWideWidths(t *testing.T) {
	twoDigits := kaizenDoc(t)
	twoDigits.Pulse.Today.FirstPass = data.PulseFirstPass{Small: data.PulseRate{Yes: 10, Of: 12}, Medium: data.PulseRate{Yes: 11, Of: 13}, Large: data.PulseRate{Yes: 1, Of: 2}, Unstated: data.PulseRate{Yes: 2, Of: 2}}
	for _, w := range []int{100, 120, 140, 200} {
		for name, doc := range map[string]*data.Kaizen{"fixture": kaizenDoc(t), "no merge": noMergeDoc(t), "one day": oneDayDoc(t), "two-digit sizes": twoDigits} {
			out := bandOf(pulseFrame(t, doc, w))
			if strings.Contains(out, "…") {
				t.Errorf("%s at %d: something is cut:\n%s", name, w, out)
			}
			for _, l := range strings.Split(out, "\n") {
				if ansi.StringWidth(l) > w {
					t.Errorf("%s at %d: wider than the window: %q", name, w, l)
				}
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
