package tui

import (
	"strings"
	"testing"

	"isshoni/internal/data"
)

// frictionPage is the fixture's Friction page without colour, tall enough for all of it, after edit changed its figures.
func frictionPage(t *testing.T, w int, edit func(*data.Friction)) string {
	t.Helper()
	doc := kaizenDoc(t)
	if edit != nil {
		edit(doc.Friction)
	}
	m := fakeModel(true, w, 100)
	setKaizen(m, doc, pageFriction)
	return plain(m)
}

// squeeze joins the words of every line with single spaces.
func squeeze(out string) string {
	var ls []string
	for _, l := range strings.Split(out, "\n") {
		ls = append(ls, strings.Join(strings.Fields(l), " "))
	}
	return strings.Join(ls, "\n")
}

func TestFrictionRowsAndFigures(t *testing.T) {
	out := squeeze(frictionPage(t, 140, nil))
	for _, want := range []string{
		"DISPATCHER $0.18* / call n=400 calls median context 450.0k",
		"COST AFTER THE FIRST REVIEW 40% of task cost $48.00 of $120 median $2.50 / task n=10 (subagents only: 9%)",
		"DEFECT CAUSES spec 40% 2 brief 0 conflict 20% 1 scope 0 code 40% 2 n=5 stated · 20 unstated",
		"REFUSED BASH CALLS role refused guard classifier next 3 calls",
		"lead 2.4% 12/500 10 2 $1.50",
		"coder 6.7% 20/300 12 8 $2.25",
		"explorer - 1/0 1 0 $0.20",
		"LEAD PROMPTS AFTER HAND-OVER 30 in 12 tasks $14.20 after them $0.47 / prompt",
		"QUEUE ATTEMPTS / MERGED PR 1.00 n=3 updates 2 · re-runs 0 · send-backs 1 · stops 0 (queue log since 11-15 09:00)",
		"QUEUE SEND-BACKS AND PAUSES task send-backs pauses paused",
		"fixture-alpha/fx-held-00 10 1 14m",
		"fixture-alpha/fx-held-01 9 0 -",
		"LATE CONFLICTS task / file queue log git history (backfill)",
		"by task fixture-alpha/fx-late-00 2 12",
		"by file fixture-beta/src/mod0/main.go 1 9",
		"9 tasks in the queue log (since 11-15 09:00) · 9 of 14 PRs in git history (backfill)",
		"OUTBOX 6 triaged of 20 issues · 14 open",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("the page lacks %q:\n%s", want, out)
		}
	}
}

func TestFrictionNarrowTablesDropWholeColumnsAndTheSinceDateGoesFirst(t *testing.T) {
	for _, w := range []int{71, 60} {
		var table []string
		for _, l := range strings.Split(frictionPage(t, w, nil), "\n") {
			if strings.Contains(l, "REFUSED BASH") || len(table) > 0 {
				if strings.TrimSpace(l) == "" {
					break
				}
				table = append(table, l)
			}
		}
		if len(table) < 2 || strings.Contains(strings.Join(table, "\n"), "…") {
			t.Errorf("at %d columns the refusals table drops columns, it does not cut a cell:\n%s", w, strings.Join(table, "\n"))
		}
	}
	if out := squeeze(frictionPage(t, 71, nil)); !strings.Contains(out, "9 tasks in the queue log · 9 of 14") {
		t.Errorf("narrow, the footer drops the date:\n%s", out)
	}
}

func TestFrictionListsCutAtEightRows(t *testing.T) {
	out := squeeze(frictionPage(t, 140, nil))
	for _, c := range []struct{ shown, hidden, more string }{
		{"fx-held-07", "fx-held-08", "+2 more"},
		{"fx-late-07", "fx-late-08", "+1 more"},
	} {
		if !strings.Contains(out, c.shown) || strings.Contains(out, c.hidden) || !strings.Contains(out, c.more) {
			t.Errorf("%s is the last row shown, then %q:\n%s", c.shown, c.more, out)
		}
	}
	out = squeeze(frictionPage(t, 140, func(f *data.Friction) {
		f.Held, f.LateConflicts.Files = f.Held[:8], f.LateConflicts.Files[:2]
	}))
	if !strings.Contains(out, "fx-held-07") || strings.Contains(out, "+2 more") {
		t.Errorf("eight rows are shown whole:\n%s", out)
	}
}

func TestFrictionUnknownsAreDashesAndEmptyListsSaySo(t *testing.T) {
	out := squeeze(frictionPage(t, 140, func(f *data.Friction) {
		*f = data.Friction{}
	}))
	for _, want := range []string{
		"DISPATCHER - / call n=0 calls median context -",
		"COST AFTER THE FIRST REVIEW - of task cost $0 of $0 median - / task n=0",
		"n=0 stated · 0 unstated",
		"REFUSED BASH CALLS none recorded",
		"LEAD PROMPTS AFTER HAND-OVER none recorded",
		"QUEUE ATTEMPTS / MERGED PR no queue log yet",
		"QUEUE SEND-BACKS AND PAUSES none recorded",
		"by task none recorded",
		"by file none recorded",
		"no queue log yet · 0 of 0 PRs in git history",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("lacks %q:\n%s", want, out)
		}
	}
	out = squeeze(frictionPage(t, 140, func(f *data.Friction) {
		f.LateConflicts.Tasks[0].Log = nil
		f.Queue.PerMerged = nil
	}))
	if !strings.Contains(out, "fixture-alpha/fx-late-00 - 12") || !strings.Contains(out, "QUEUE ATTEMPTS / MERGED PR - n=3") {
		t.Errorf("a null is a dash, never 0:\n%s", out)
	}
}

func TestFrictionWithoutTheKeySaysNothingYet(t *testing.T) {
	doc := kaizenDoc(t)
	doc.Friction = nil
	m := fakeModel(true, 100, 30)
	setKaizen(m, doc, pageFriction)
	if out := plain(m); !strings.Contains(out, "Nothing measured yet") || strings.Contains(out, "DISPATCHER") {
		t.Fatalf("no key, one line:\n%s", out)
	}
}

func TestFrictionBadValuesAreWarningColoured(t *testing.T) {
	orange := func(edit func(*data.Friction), fig string) bool {
		doc := kaizenDoc(t)
		edit(doc.Friction)
		m := fakeModel(false, 140, 100)
		setKaizen(m, doc, pageFriction)
		return strings.Contains(m.Render(), m.th.on(m.th.Bg).t(fig, m.th.Orange))
	}
	refused := func(share float64, guard int) func(*data.Friction) {
		return func(f *data.Friction) { f.Refusals.Roles[1].Share, f.Refusals.Roles[1].Guard = ptr(share), guard }
	}
	causes := func(spec, brief, conflict int) func(*data.Friction) {
		return func(f *data.Friction) { f.Causes = data.FrictionCauses{Spec: spec, Brief: brief, Conflict: conflict} }
	}
	for _, c := range []struct {
		name     string
		atLimit  func(*data.Friction)
		okFig    string // shown at the threshold, not coloured
		over     func(*data.Friction)
		overFig  string // shown just over it, coloured
		unitCell int    // the figure sits in a table cell of this width, 0 when it is a row piece
	}{
		{"dispatcher over $0.15 a call", func(f *data.Friction) { f.Dispatcher.PerCall = ptr(0.15) }, "$0.15* / call", func(f *data.Friction) { f.Dispatcher.PerCall = ptr(0.16) }, "$0.16* / call", 0},
		{"median context over 400k", func(f *data.Friction) { f.Dispatcher.ContextMedian = ptr(400000.0) }, "median context 400.0k", func(f *data.Friction) { f.Dispatcher.ContextMedian = ptr(401000.0) }, "median context 401.0k", 0},
		{"rework over a third", func(f *data.Friction) { f.Rework.Share = ptr(1.0 / 3) }, "33% of task cost", func(f *data.Friction) { f.Rework.Share = ptr(0.34) }, "34% of task cost", 0},
		{"a cause over a third of the stated", causes(1, 1, 1), "spec 33% 1", causes(2, 1, 1), "spec 50% 2", 0},
		{"refused over 5% of a role's Bash", refused(0.05, 14), "5.0% 22/300", refused(0.051, 14), "5.1% 22/300", 16},
	} {
		pad := func(s string) string {
			if c.unitCell > 0 {
				return cell(s, c.unitCell, false)
			}
			return s
		}
		if orange(c.atLimit, pad(c.okFig)) {
			t.Errorf("%s: at the threshold nothing is coloured", c.name)
		}
		if !orange(c.over, pad(c.overFig)) {
			t.Errorf("%s: %q is not in the warning colour", c.name, c.overFig)
		}
	}
}

func TestFrictionHasNoRepoPrefixWithOneRepository(t *testing.T) {
	doc := kaizenDoc(t)
	doc.Repos = doc.Repos[:1]
	m := fakeModel(true, 140, 100)
	setKaizen(m, doc, pageFriction)
	if out := plain(m); strings.Contains(out, "fixture-alpha/") || !strings.Contains(out, "fx-held-00") {
		t.Fatalf("a single repository needs no prefix:\n%s", out)
	}
}
