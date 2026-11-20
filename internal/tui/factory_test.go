package tui

import (
	"fmt"
	"image/color"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"isshoni/internal/data"
)

// A window too small to lay the Factory view out must not panic on a key or a render.
func TestFactoryViewSurvivesAWindowWithNoRoom(t *testing.T) {
	for _, sz := range [][2]int{{0, 10}, {1, 1}, {0, 0}} {
		for _, repos := range [][]factoryRepo{nil, {alpha()}, fakeFactoryRepos()} {
			for _, asked := range []bool{true, false} {
				m := factoryModel(true, 100, 30, repos...)
				if !asked {
					m.fac.doneAt = time.Time{}
				}
				m.Update(tea.WindowSizeMsg{Width: sz[0], Height: sz[1]})
				send(m, "j", "G", "down", "k")
				m.Render()
			}
		}
	}
}

// ---- fixtures ---------------------------------------------------------------------

func ftask(slug, phase string) data.FactoryTask {
	return data.FactoryTask{Slug: slug, Phase: phase, Base: "origin/main", PRState: "none", Session: "none"}
}

// fstatus is a status whose counts follow its rows.
func fstatus(limit int, rows ...data.FactoryTask) *data.FactoryStatus {
	st := &data.FactoryStatus{Rows: rows, Cap: limit}
	for _, r := range rows {
		switch r.Phase {
		case "motion":
			st.InMotion++
		case "blocked":
			st.InMotion++
			st.Blocked++
		case "review":
			st.InReview++
		case "merged":
			st.Merged++
		case "settled":
			st.Settled++
		}
	}
	return st
}

// manyTasks is n tasks in motion.
func manyTasks(n int) []data.FactoryTask {
	var rows []data.FactoryTask
	for i := 0; i < n; i++ {
		x := ftask(fmt.Sprintf("task-%02d", i), "motion")
		x.Session, x.Elapsed = "working", ptr(int64(60*(i+1)))
		rows = append(rows, x)
	}
	return rows
}

// delta waits on the human in every other way: a deliverable that is a file, one that is a
// link, a blocked lead without a reason, an overtime stop, a long summary in wide characters.
func delta() factoryRepo {
	file, link, bare := ftask("design-review", "review"), ftask("video-cut", "review"), ftask("stuck-on-keys", "blocked")
	slow, wide := ftask("slow-import", "blocked"), ftask("wide-ask", "blocked")
	file.Deliverable, link.Deliverable, link.Waited = "/r/delta/docs/design.md", "https://example.com/cut/v2", ptr(int64(1800))
	bare.Waited = ptr(int64(90))
	slow.Waited, slow.StopKind, slow.StopSummary = ptr(int64(300)), "OVERTIME", "ran 3h of 2h: split it?"
	wide.Waited, wide.StopKind = ptr(int64(7200)), "BLOCKED"
	wide.StopSummary = "データベースの接続情報が見つかりません。どの環境のものを使えばよいですか？ 本番ではない方でお願いします。"
	return factoryRepo{name: "delta-docs", path: "/r/delta", st: fstatus(5, file, link, bare, slow, wide, onDispatcher(), onTask(),
		pausedFor("paused-fresh", 12*time.Minute, "login-flow", "api-keys"), pausedFor("paused-stale", 45*time.Minute, "login-flow"), prioritizedFor("rush-fix", 3*time.Minute))}
}

// onDispatcher and onTask are tasks blocked on someone else, each with a reason; the band leaves them out.
func onDispatcher() data.FactoryTask {
	x := ftask("pick-a-theme", "blocked")
	x.Waited, x.WaitsOn, x.StopKind = ptr(int64(600)), "dispatcher", "BLOCKED"
	x.StopSummary = "the  palette is\nstill   open: which   brief decides it?"
	return x
}

func onTask() data.FactoryTask {
	x := ftask("docs-after-review", "blocked")
	x.Waited, x.WaitsOn, x.StopKind = ptr(int64(900)), "design-review-of-docs", "BLOCKED" // 21 characters
	x.StopSummary = "needs the design review to land first, then the docs can quote its final wording without guessing"
	return x
}

// pausedFor is a task the site manager paused age ago, waiting on the slugs.
func pausedFor(slug string, age time.Duration, waits ...string) data.FactoryTask {
	x := ftask(slug, "blocked")
	x.WaitsOn = strings.Join(waits, ", ")
	x.Paused = &data.FactoryFlag{Since: fakeNow.Add(-age), WaitsOn: waits}
	return x
}

// prioritizedFor is a task in motion the queue prioritized age ago.
func prioritizedFor(slug string, age time.Duration) data.FactoryTask {
	x := ftask(slug, "motion")
	x.Session, x.Elapsed = "working", ptr(int64(600))
	x.Prioritized = &data.FactoryFlag{Since: fakeNow.Add(-age)}
	return x
}

// waitingRepos is the fixture with every kind of waiting task.
func waitingRepos() []factoryRepo { return append(fakeFactoryRepos(), delta()) }

// alpha has all five phases, an overtime and a stale task, a stacked one, a closed pull
// request, and deliverables without a pull request (a link and a file).
func alpha() factoryRepo {
	quiet, login, stacked := ftask("quiet-refactor", "motion"), ftask("login-flow", "motion"), ftask("stacked-on-login", "motion")
	quiet.Session, quiet.Elapsed, quiet.Size, quiet.Limit = "working", ptr(int64(252)), "small", ptr(int64(2700))
	login.Session, login.Elapsed, login.Overtime = "working", ptr(int64(8100)), true
	login.Size, login.Limit = "medium", ptr(int64(5400))
	stacked.Base, stacked.Elapsed, stacked.PRState = "worktree-login-flow", ptr(int64(95)), "unknown"
	stacked.Limit = ptr(int64(7200)) // no size: the setting's limit
	nearing := ftask("slow-export", "motion")
	nearing.Session, nearing.Elapsed, nearing.Nearing = "working", ptr(int64(2460)), true
	nearing.Size, nearing.Limit = "small", ptr(int64(2700))
	limits, docs, old := ftask("api-rate-limits", "review"), ftask("rate-limit-docs", "motion"), ftask("old-experiment", "merged")
	ask, notes := ftask("needs-an-answer", "blocked"), ftask("release-notes", "settled")
	ask.Session, ask.Elapsed, ask.Deliverable = "done", ptr(int64(640)), "https://example.com/design/doc"
	notes.Session, notes.Elapsed, notes.Deliverable = "done", ptr(int64(3000)), "/r/alpha/docs/release-notes.md"
	limits.PRState, limits.PRURL, limits.Elapsed, limits.Waited = "open", "https://github.com/o/alpha/pull/42", ptr(int64(5400)), ptr(int64(10800))
	ask.Waited, ask.StopKind, ask.StopSummary = ptr(int64(2700)), "QUESTION", "Should the export keep the legacy column order?"
	docs.PRState, docs.PRURL, docs.Stale = "closed", "https://github.com/o/alpha/pull/40", true
	old.PRState, old.PRURL, old.Elapsed = "merged", "https://github.com/o/alpha/pull/31", ptr(int64(61))
	plumbing, lost := ftask("old-plumbing", "merged"), heldBy("suppressed", "its replacement redo-plumbing is in review: this one is closed when that lands", 9*time.Hour)
	plumbing.PRState, plumbing.PRURL, plumbing.Elapsed, plumbing.Kept = "merged", "https://github.com/o/alpha/pull/9", ptr(int64(7200)), "uncommitted changes"
	st := fstatus(5, stacked, old, docs, quiet, nearing, limits, login, ask, notes, plumbing, lost)
	st.Reserve = 1
	return factoryRepo{name: "alpha", path: "/r/alpha", st: st}
}

func fakeFactoryRepos() []factoryRepo {
	return []factoryRepo{
		alpha(),
		{name: "beta-é日本語", path: "/r/beta", st: fstatus(2, manyTasks(3)...)},
		{name: "gamma", path: "/r/gamma", err: "not inside a git repository"},
	}
}

// setFloor puts the model on the Factory tab (the floor) holding repos as the last result.
func setFloor(m *model, repos ...factoryRepo) {
	m.view = viewFactory
	m.fac.repos, m.fac.doneAt, m.fac.cap = repos, m.now(), 5
	for _, r := range repos {
		if r.st != nil {
			m.fac.cap, m.fac.reserve = r.st.Cap, r.st.Reserve
			break
		}
	}
	m.fixScroll()
}

// setFactory puts the model on the Inbox, the page of the factory table, holding repos as the last result.
func setFactory(m *model, repos ...factoryRepo) {
	setFloor(m, repos...)
	m.view = viewInbox
	m.fixScroll()
}

func factoryModel(noColor bool, w, h int, repos ...factoryRepo) *model {
	m := fakeModel(noColor, w, h)
	setFactory(m, repos...)
	return m
}

func floorModel(noColor bool, w, h int, repos ...factoryRepo) *model {
	m := fakeModel(noColor, w, h)
	setFloor(m, repos...)
	return m
}

// ---- rendering --------------------------------------------------------------------

func TestFactoryRenderVariants(t *testing.T) {
	unknown := ftask("waiting-on-human", "frozen")
	unknown.Session = "unknown"
	variants := map[string]func(m *model){
		"several": func(m *model) {
			setFactory(m, append(fakeFactoryRepos(), factoryRepo{name: "delta", path: "/r/delta", st: fstatus(5)})...)
		},
		"over cap": func(m *model) {
			setFactory(m, factoryRepo{name: "busy", path: "/r/busy", st: fstatus(5, manyTasks(30)...)})
		},
		"cap 40": func(m *model) {
			setFactory(m, factoryRepo{name: "big", path: "/r/big", st: fstatus(40, manyTasks(12)...)})
		},
		"unknown": func(m *model) {
			setFactory(m, factoryRepo{name: "odd", path: "/r/odd", st: fstatus(5, unknown, ftask("a", "motion"))})
		},
		"blocked meter": func(m *model) {
			setFactory(m, factoryRepo{name: "held", path: "/r/held", st: fstatus(40, append(manyTasks(12), ftask("q1", "blocked"), ftask("q2", "blocked"), ftask("s", "settled"))...)})
		},
		"narrow meter": func(m *model) {
			setFactory(m, factoryRepo{name: "mid-size-nam", path: "/r/mid", st: fstatus(17, manyTasks(4)...)})
		},
		"band": func(m *model) { setFactory(m, waitingRepos()...) },
		"quiet": func(m *model) {
			setFactory(m, factoryRepo{name: "calm", path: "/r/calm", st: fstatus(5, ftask("a", "motion"))})
		},
		"editing": func(m *model) { setFactory(m, fakeFactoryRepos()...); m.fac.editing, m.fac.field = true, "12" },
		"edit err": func(m *model) {
			setFactory(m, fakeFactoryRepos()...)
			m.fac.editing, m.fac.fieldErr = true, "enter a whole number from 1 to 999"
		},
		"cap error": func(m *model) {
			setFactory(m, fakeFactoryRepos()...)
			m.fac.capErr = "~/.claude/factory/settings.toml line 3: the list is not closed"
		},
		"empty":   func(m *model) { setFactory(m) },
		"waiting": func(m *model) { m.view = viewFactory },
		"toast":   func(m *model) { setFactory(m, fakeFactoryRepos()...); m.setToast("worktree cap set to 8", false) },
		"long": func(m *model) {
			setFactory(m, factoryRepo{name: strings.Repeat("長い", 40), path: "/r/x", st: fstatus(5, ftask(strings.Repeat("s", 90), "motion"))})
		},
	}
	for name, setup := range variants {
		for _, sz := range sizes {
			for _, nc := range []bool{false, true} {
				m := fakeModel(nc, sz[0], sz[1])
				setup(m)
				checkFrame(t, fmt.Sprintf("factory %s %dx%d nc=%v", name, sz[0], sz[1], nc), m, nc)
			}
		}
	}
}

func TestFactoryMeter(t *testing.T) {
	cases := []struct {
		limit, n int
		want     string
	}{
		{5, 0, "in motion ○○○○○ 0/5"},
		{5, 3, "in motion ●●●○○ 3/5"},
		{5, 5, "in motion ●●●●● 5/5"},
		{2, 3, "in motion ●●● 3/2"},
	}
	for _, c := range cases {
		m := factoryModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: fstatus(c.limit, manyTasks(c.n)...)})
		if out := plain(m); !strings.Contains(out, c.want) {
			t.Errorf("cap %d, %d in motion: want %q in\n%s", c.limit, c.n, c.want, out)
		}
	}
	withReserve := func(limit, reserve, n int) *data.FactoryStatus {
		st := fstatus(limit, manyTasks(n)...)
		st.Reserve = reserve
		return st
	}
	for _, c := range []struct {
		limit, reserve, n int
		want              string
	}{
		{10, 1, 0, "in motion ○○○○○○○○○◌ 0/9+1"},
		{10, 1, 7, "in motion ●●●●●●●○○◌ 7/9+1"},
		{10, 1, 9, "in motion ●●●●●●●●●◌ 9/9+1"},
		{10, 1, 10, "in motion ●●●●●●●●●● 10/9+1"},
		{10, 1, 12, "in motion ●●●●●●●●●●●● 12/9+1"},
		{5, 2, 4, "in motion ●●●●◌ 4/3+2"},
		{40, 4, 10, "in motion █████░░░░░░░░░░░░░░░ 10/36+4"},
	} {
		m := factoryModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: withReserve(c.limit, c.reserve, c.n)})
		if out := plain(m); !strings.Contains(out, c.want) {
			t.Errorf("cap %d, reserve %d, %d in motion: want %q in\n%s", c.limit, c.reserve, c.n, c.want, out)
		}
	}
	for _, c := range []struct {
		n   int
		col func(th *Theme) color.Color
	}{
		{0, func(th *Theme) color.Color { return th.Muted }},
		{8, func(th *Theme) color.Color { return th.Green }},
		{9, func(th *Theme) color.Color { return th.Yellow }},
		{10, func(th *Theme) color.Color { return th.Yellow }},
		{11, func(th *Theme) color.Color { return th.Red }},
	} {
		m := factoryModel(false, 140, 40, factoryRepo{name: "r", path: "/r", st: withReserve(10, 1, c.n)})
		p, out := m.th.on(m.th.Bg), m.Render()
		if want := p.b(fmt.Sprintf("%d/9+1", c.n), c.col(m.th)); !strings.Contains(out, want) {
			t.Errorf("reserve 1 of 10, %d in motion: the label is not painted as expected", c.n)
		}
		if want := p.t(strings.Repeat("●", c.n), c.col(m.th)); c.n > 0 && !strings.Contains(out, want) {
			t.Errorf("reserve 1 of 10, %d in motion: filled cells are not painted as expected", c.n)
		}
	}
	m := factoryModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: fstatus(40, manyTasks(10)...)})
	if out := plain(m); !strings.Contains(out, "in motion █████░░░░░░░░░░░░░░░ 10/40") {
		t.Errorf("a cap above %d is a %d-cell bar:\n%s", meterCells, meterCells, out)
	}
	blocked := fstatus(5, append(manyTasks(2), ftask("q", "blocked"))...)
	if out := plain(factoryModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: blocked})); !strings.Contains(out, "in motion ▲●●○○ 3/5") {
		t.Errorf("a blocked task holds a slot with its own glyph:\n%s", out)
	}
	for _, c := range []struct {
		limit, n int
		col      func(th *Theme) color.Color
	}{
		{5, 3, func(th *Theme) color.Color { return th.Green }},
		{5, 5, func(th *Theme) color.Color { return th.Yellow }},
		{2, 3, func(th *Theme) color.Color { return th.Red }},
	} {
		m := factoryModel(false, 140, 40, factoryRepo{name: "r", path: "/r", st: fstatus(c.limit, manyTasks(c.n)...)})
		if want := m.th.on(m.th.Bg).t(strings.Repeat("●", c.n), c.col(m.th)); !strings.Contains(m.Render(), want) {
			t.Errorf("cap %d, %d in motion: filled cells are not painted as expected", c.limit, c.n)
		}
	}
	m = factoryModel(false, 140, 40, factoryRepo{name: "r", path: "/r", st: blocked})
	if want := m.th.on(m.th.Bg).t("▲", m.th.Orange); !strings.Contains(m.Render(), want) {
		t.Error("the blocked cell is orange")
	}
	nm := factoryModel(true, 60, 20, factoryRepo{name: "mid-size-nam", path: "/r/mid", st: fstatus(17, manyTasks(4)...)})
	if out := plain(nm); !strings.Contains(out, " mid-size-nam ") || strings.Contains(out, "●●●●○") {
		t.Errorf("at 60 columns a long meter gives way to a bar and the name stays whole:\n%s", out)
	}
	full := fstatus(5, append(manyTasks(4), ftask("q", "blocked"), ftask("v", "review"), ftask("g", "merged"), ftask("s", "settled"))...)
	nm = factoryModel(true, 60, 20, factoryRepo{name: "mid-size-name-long", path: "/r/mid", st: full})
	if out := plain(nm); !strings.Contains(out, " mid-size-nam") || !strings.Contains(out, "in motion ▲●●●● 5/5") {
		t.Errorf("at 60 columns the other counts go before the name or the meter cells:\n%s", out)
	}
}

func TestFactorySettingsLineShowsTheReserve(t *testing.T) {
	line := func(reserve int) string {
		st := fstatus(10, manyTasks(2)...)
		st.Reserve = reserve
		for _, l := range strings.Split(plain(factoryModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: st})), "\n") {
			if strings.Contains(l, "worktree cap") {
				return l
			}
		}
		t.Fatal("no settings line")
		return ""
	}
	if got := line(1); !strings.Contains(got, "worktree cap  10  per repo, 1 in reserve   c edit") {
		t.Errorf("with a reserve: %q", got)
	}
	if got := line(0); !strings.Contains(got, "worktree cap  10  per repo   c edit") || strings.Contains(got, "reserve") {
		t.Errorf("without a reserve: %q", got)
	}
	m := factoryModel(true, 140, 40, alpha())
	m.fac.editing, m.fac.field = true, "12"
	if out := plain(m); strings.Contains(out, "in reserve") {
		t.Errorf("the editing state has no reserve:\n%s", out)
	}
}

func TestFactoryTableColumnsAndPhaseLabels(t *testing.T) {
	out := plain(factoryModel(true, 140, 40, alpha()))
	for _, want := range []string{"SLUG", "PHASE", "ELAPSED", "PR", "BASE", "FLAGS", "in motion", "waiting for review", "merged, not cleaned",
		"#42", "#40 closed", "#31", "↳ login-flow", "[overtime]", "[stale]", "SIZE", "small", "medium", "4m / 45m", "41m / 45m", "2h15m / 1h30m", "1m / 2h", "1h30m",
		"blocked, needs you", "settled, not cleaned", "▲ blocked", "example.com/design/doc", "release-notes.md"} {
		if !strings.Contains(out, want) {
			t.Errorf("140 columns lack %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "/r/alpha/docs") || strings.Contains(out, "https://example") {
		t.Errorf("a deliverable shows a link without its scheme and a file as its base name:\n%s", out)
	}
	if strings.Contains(out, "origin/") {
		t.Errorf("the origin/ prefix of a base is dropped:\n%s", out)
	}
	short := plain(factoryModel(true, 80, 40, alpha()))
	if strings.Contains(short, "waiting for review") || !strings.Contains(short, "◆ review ") || !strings.Contains(short, "⠋ motion") || !strings.Contains(short, "Inbox  7 motion (1 blocked) · 1 review · 2 merged · 1 settled") || !strings.Contains(short, "▲ blocked ") || strings.Contains(short, "blocked, needs you") {
		t.Errorf("below 100 columns the labels are short:\n%s", short)
	}
	narrow := plain(factoryModel(true, 60, 20, alpha()))
	for _, gone := range []string{"BASE", "ELAPSED  PR"} {
		if strings.Contains(narrow, gone) {
			t.Errorf("60 columns should have dropped %q:\n%s", gone, narrow)
		}
	}
	for _, kept := range []string{"SLUG", "PHASE", "FLAGS", "[overtime]"} {
		if !strings.Contains(narrow, kept) {
			t.Errorf("60 columns lack %q:\n%s", kept, narrow)
		}
	}
}

func TestFactoryElapsedAgainstTheLimit(t *testing.T) {
	inside, nearing, over, unsized, review := ftask("a", "motion"), ftask("b", "motion"), ftask("c", "motion"), ftask("d", "motion"), ftask("e", "review")
	inside.Elapsed, inside.Size, inside.Limit = ptr(int64(1500)), "small", ptr(int64(2700))
	nearing.Elapsed, nearing.Size, nearing.Limit, nearing.Nearing = ptr(int64(2460)), "small", ptr(int64(2700)), true
	over.Elapsed, over.Size, over.Limit, over.Overtime = ptr(int64(8100)), "medium", ptr(int64(5400)), true
	unsized.Elapsed, unsized.Limit = ptr(int64(30)), ptr(int64(7200))
	review.Elapsed, review.Limit = ptr(int64(1500)), ptr(int64(2700))
	m := factoryModel(false, 140, 40, factoryRepo{name: "m", st: fstatus(5, inside, nearing, over, unsized, review)})
	p, th, rows := m.th.on(m.th.Bg), m.th, m.fac.repos[0].st.Rows
	cols := m.factoryCols(p, 140, rows)
	for i, want := range []struct {
		text, size string
		col        color.Color
	}{{"25m / 45m", "small", th.Muted}, {"41m / 45m", "small", th.Yellow}, {"2h15m / 1h30m", "medium", th.Red},
		{"0m / 2h", "-", th.Muted}, {"25m00s", "-", th.Muted}} {
		line := m.taskRow(p, cols, rows[i], 140)[0]
		if !strings.Contains(line, p.t(cell(want.text, cols.elapsed, true), want.col)) ||
			!strings.Contains(line, p.t(cell(want.size, cols.size, false), th.Faint)) {
			t.Errorf("row %d: want %q in %v and size %q:\n%s", i, want.text, want.col, want.size, line)
		}
	}
}

func TestFactoryColsDegradeInOrder(t *testing.T) {
	m := factoryModel(true, 140, 40, alpha())
	p, rows := m.th.on(m.th.Bg), m.fac.repos[0].st.Rows
	seen := map[string]bool{}
	for w := 40; w <= 160; w++ {
		c := m.factoryCols(p, w, rows)
		switch {
		case c.size > 0:
			if !c.span || c.base == 0 || c.pr == 0 || c.elapsed == 0 {
				t.Fatalf("%d columns: SIZE shows only while everything else does: %+v", w, c)
			}
			seen["size"] = true
		case c.span:
			seen["span"] = true
		case c.base > 0:
			seen["reduced"] = c.elapsed == elapsedLeast
		case c.pr > 0:
			seen["base"] = true
		case c.elapsed > 0:
			seen["pr"] = true
		default:
			seen["elapsed"] = true
		}
		if c.span && (c.base == 0 || c.pr == 0 || c.elapsed == 0) || c.base > 0 && (c.pr == 0 || c.elapsed == 0) || c.pr > 0 && c.elapsed == 0 {
			t.Fatalf("%d columns: SIZE, the span, BASE, PR and ELAPSED go in that order: %+v", w, c)
		}
	}
	for _, step := range []string{"size", "span", "reduced", "base", "pr", "elapsed"} {
		if !seen[step] {
			t.Errorf("no width between 40 and 160 shows the step %q", step)
		}
	}
}

func TestFactoryColumnsAlign(t *testing.T) {
	var starts []int
	for _, l := range strings.Split(plain(factoryModel(true, 140, 40, fakeFactoryRepos()...)), "\n") {
		if i := strings.Index(l, "PHASE"); i >= 0 {
			starts = append(starts, len([]rune(l[:i])))
		}
	}
	if len(starts) != 2 || starts[0] != starts[1] {
		t.Fatalf("the tables of two repositories share their columns: %v", starts)
	}
}

func TestFactoryRowOrder(t *testing.T) {
	late, early, mid := ftask("zz", "motion"), ftask("aa", "motion"), ftask("mm", "motion")
	late.Overtime = true
	rows := sortedTasks([]data.FactoryTask{ftask("done", "settled"), ftask("merged-one", "merged"), ftask("q", "zeta"), early, ftask("r", "review"), late, mid, ftask("p", "alpha"), ftask("b", "blocked")})
	var got []string
	for _, r := range rows {
		got = append(got, r.Slug)
	}
	if want := "b zz aa mm r merged-one done p q"; strings.Join(got, " ") != want {
		t.Fatalf("order %q, want %q", strings.Join(got, " "), want)
	}
}

// ---- the band ---------------------------------------------------------------------

func bandLines(out string) []string {
	var band []string
	for _, l := range strings.Split(out, "\n") {
		if strings.Contains(l, "worktree cap") {
			break
		}
		band = append(band, l)
	}
	return band
}

func TestFactoryWaitingOrder(t *testing.T) {
	m := factoryModel(true, 140, 40, waitingRepos()...)
	var got []string
	for _, it := range m.waiting() {
		got = append(got, it.task.Slug)
	}
	if want := "api-rate-limits wide-ask needs-an-answer video-cut slow-import stuck-on-keys design-review"; strings.Join(got, " ") != want {
		t.Fatalf("oldest first, an unknown wait last: %q, want %q", strings.Join(got, " "), want)
	}
	tie := func(slug string) data.FactoryTask { x := ftask(slug, "review"); x.Waited = ptr(int64(60)); return x }
	m = factoryModel(true, 140, 40, factoryRepo{name: "b", st: fstatus(5, tie("y"), tie("a"))}, factoryRepo{name: "a", st: fstatus(5, tie("z"))})
	got = nil
	for _, it := range m.waiting() {
		got = append(got, it.repo+"/"+it.task.Slug)
	}
	if want := "a/z b/a b/y"; strings.Join(got, " ") != want {
		t.Fatalf("ties by repository then slug: %q, want %q", strings.Join(got, " "), want)
	}
	if len(factoryModel(true, 140, 40, factoryRepo{name: "m", st: fstatus(5, ftask("a", "motion"), ftask("b", "merged"), ftask("c", "settled"))}).waiting()) != 0 {
		t.Fatal("only review and blocked tasks wait on the human")
	}
}

func TestFactoryBandLeavesOutTasksBlockedOnSomeoneElse(t *testing.T) {
	human, empty := ftask("on-human", "blocked"), ftask("old-script", "blocked")
	human.WaitsOn = "human"
	m := factoryModel(true, 140, 40, factoryRepo{name: "m", st: fstatus(5, human, empty, onDispatcher(), onTask())})
	var got []string
	for _, it := range m.waiting() {
		got = append(got, it.task.Slug)
	}
	slices.Sort(got)
	if want := "old-script on-human"; strings.Join(got, " ") != want {
		t.Fatalf("only blocked-on-human (or unrecorded) tasks are in the band: %q, want %q", strings.Join(got, " "), want)
	}
	if band := strings.Join(m.needsYou(m.th.on(m.th.Bg), 140, m.waiting()), "\n"); !strings.Contains(band, "Needs you  2") {
		t.Fatalf("band count: %s", band)
	}
	if st := m.fac.repos[0].st; st.Blocked != 4 {
		t.Fatalf("the blocked count is unchanged: %+v", st)
	}
}

func TestFactoryTaskRowNamesWhomABlockedTaskWaitsOn(t *testing.T) {
	for _, w := range []int{80, 100, 140} {
		m := factoryModel(true, w, 60, delta())
		page, _ := m.inboxPage(m.th.on(m.th.Bg), w, 60)
		plain := ansi.Strip(strings.Join(page, "\n"))
		lines := strings.Split(plain, "\n")
		for _, l := range lines {
			if width(l) > w {
				t.Errorf("%d columns: line of %d cells: %q", w, width(l), l)
			}
		}
		at := func(slug string) (row, reason string) {
			for i, l := range lines {
				if strings.Contains(l, slug) && strings.Contains(l, "blocked on") {
					return l, lines[i+1]
				}
			}
			t.Fatalf("%d columns: no blocked-on row for %s in\n%s", w, slug, plain)
			return
		}
		row, reason := at("pick-a-theme")
		if !strings.Contains(row, "blocked on dispatcher") || strings.Contains(row, "needs you") {
			t.Errorf("%d columns: %q", w, row)
		}
		if i := width(row[:strings.Index(row, "blocked on")]); !strings.HasPrefix(reason, strings.Repeat(" ", i-2)+"the palette is still open: which") {
			t.Errorf("%d columns: reason not indented under PHASE, whitespace not collapsed: %q", w, reason)
		}
		row, reason = at("docs-after")
		if !strings.Contains(row, "blocked on design") || !strings.Contains(reason, "needs the design review") {
			t.Errorf("%d columns: %q / %q", w, row, reason)
		}
		if w == 140 && !strings.Contains(row, "blocked on design-review-of-docs ") {
			t.Errorf("140 columns: the slug it waits on is cut: %q", row)
		}
		if w == 80 && !strings.HasSuffix(strings.TrimRight(reason, " "), "…") {
			t.Errorf("80 columns: a long reason is truncated: %q", reason)
		}
		for _, l := range lines {
			if strings.Contains(l, "stuck-on-keys") && strings.Contains(l, "blocked on") {
				t.Errorf("a task blocked on the human keeps its row: %q", l)
			}
		}
	}
	rowOf := func(m *model, i int) block {
		p, rows := m.th.on(m.th.Bg), m.fac.repos[0].st.Rows
		return m.taskRow(p, m.factoryCols(p, 140, rows), rows[i], 140)
	}
	m := factoryModel(true, 140, 60, factoryRepo{name: "m", st: fstatus(5, func() data.FactoryTask { x := onDispatcher(); x.StopSummary = ""; return x }())})
	if got := len(rowOf(m, 0)); got != 1 {
		t.Fatalf("no summary, no second line: %d lines", got)
	}
	// a task blocked on the human keeps one line even with a summary, as does one with WaitsOn empty
	human, old := ftask("on-human", "blocked"), ftask("old-script", "blocked")
	human.WaitsOn, human.StopSummary, old.StopSummary = "human", "Which database should it use?", "Which database should it use?"
	m = factoryModel(true, 140, 60, factoryRepo{name: "m", st: fstatus(5, human, old)})
	for i := range 2 {
		if got := len(rowOf(m, i)); got != 1 {
			t.Fatalf("a task blocked on the human has a second line: %d lines", got)
		}
	}
}

func TestFactoryTaskRowShowsWhyATaskInMotionHasMoreTime(t *testing.T) {
	ext := func(mins int, by, reason string) data.FactoryExtension {
		return data.FactoryExtension{Minutes: mins, By: by, Reason: reason}
	}
	motion := func(slug string, es ...data.FactoryExtension) data.FactoryTask {
		x := ftask(slug, "motion")
		x.Extensions = es
		return x
	}
	one := motion("one", ext(30, "site-manager", "the migration  is\nhalf done"))
	two := motion("two", ext(30, "site-manager", "first"), ext(15, "lead", "the rollback path is left"))
	long := motion("long", ext(30, "site-manager", strings.Repeat("a long reason ", 20)))
	bare := motion("bare", ext(10, "site-manager", " "))
	none := motion("none")
	review := ftask("in-review", "review")
	review.Extensions = []data.FactoryExtension{ext(30, "site-manager", "kept")}
	m := factoryModel(true, 140, 60, factoryRepo{name: "m", st: fstatus(5, one, two, long, bare, none, review)})
	p, rows := m.th.on(m.th.Bg), m.fac.repos[0].st.Rows
	cols := m.factoryCols(p, 140, rows)
	detail := func(i int) (string, int) {
		b := m.taskRow(p, cols, rows[i], 140)
		if len(b) == 1 {
			return "", 1
		}
		return strings.TrimRight(ansi.Strip(b[1]), " "), len(b)
	}
	indent := strings.Repeat(" ", 2+cols.slug+2)
	for i, want := range map[int]string{
		0: "limit +30m by site-manager · the migration is half done",
		1: "limit +45m in 2 extensions, last by lead · the rollback path is left",
		3: "limit +10m by site-manager",
	} {
		if got, n := detail(i); got != indent+want || n != 2 {
			t.Errorf("row %d: %q (%d lines), want %q", i, got, n, indent+want)
		}
	}
	got, n := detail(2)
	if n != 2 || width(got) > 139 || !strings.HasSuffix(got, "…") || !strings.HasPrefix(got, indent+"limit +30m by site-manager · a long reason") {
		t.Errorf("a long reason is cut to the width on one line: %q (%d lines, %d cells)", got, n, width(got))
	}
	for _, i := range []int{4, 5} {
		if _, n := detail(i); n != 1 {
			t.Errorf("row %d has no extension line: %d lines", i, n)
		}
	}
}

func TestFactoryBlockedOnSomeoneElseIsLighter(t *testing.T) {
	human := ftask("on-human", "blocked")
	human.WaitsOn = "human"
	m := factoryModel(false, 140, 60, factoryRepo{name: "m", st: fstatus(5, human, onDispatcher(), onTask())})
	p, th := m.th.on(m.th.Bg), m.th
	rows := m.fac.repos[0].st.Rows
	cols := m.factoryCols(p, 140, rows)
	line := func(i int) string { return m.taskRow(p, cols, rows[i], 140)[0] }
	if got := line(0); !strings.Contains(got, p.b("▲ ", th.Orange)) || !strings.Contains(got, p.b("blocked, needs you", th.Orange)) {
		t.Errorf("blocked on the human is bold orange: %q", got)
	}
	for i, label := range map[int]string{1: "blocked on dispatcher", 2: "blocked on design-review-of-docs"} {
		got := line(i)
		if !strings.Contains(got, p.t("▲ ", th.Muted)) || !strings.Contains(got, p.t(label, th.Muted)) {
			t.Errorf("%s is muted: %q", label, got)
		}
		if strings.Contains(got, p.b("▲ ", th.Muted)) || strings.Contains(got, p.b("▲ ", th.Orange)) {
			t.Errorf("%s is bold or orange: %q", label, got)
		}
	}
}

func TestFactoryPausedLabelShowsAgeAndWaitedTasks(t *testing.T) {
	m := factoryModel(false, 140, 60, factoryRepo{name: "m", st: fstatus(5,
		pausedFor("under-a-minute", 20*time.Second, "a"), pausedFor("fresh", 12*time.Minute, "a", "b"),
		pausedFor("nearly", 30*time.Minute-time.Second, "a"), pausedFor("stale", 30*time.Minute, "a"), pausedFor("older", 95*time.Minute, "a"))})
	p, th := m.th.on(m.th.Bg), m.th
	rows := m.fac.repos[0].st.Rows
	cols := m.factoryCols(p, 140, rows)
	for i, c := range []struct {
		label string
		col   color.Color
	}{
		{"paused 0m: waits a", th.Muted}, {"paused 12m: waits a, b", th.Muted}, {"paused 29m: waits a", th.Muted},
		{"paused 30m: waits a", th.Yellow}, {"paused 1h35m: waits a", th.Yellow},
	} {
		got := m.taskRow(p, cols, rows[i], 140)[0]
		if !strings.Contains(got, p.t("▲ ", c.col)) || !strings.Contains(got, p.t(c.label, c.col)) {
			t.Errorf("want %q in %v: %q", c.label, c.col, got)
		}
	}
	noAge := pausedFor("no-age", 0, "a")
	noAge.Paused.Since = time.Time{}
	if _, label, _ := m.phaseLook(noAge, true); label != "paused: waits a" {
		t.Errorf("a pause without a time shows without an age: %q", label)
	}
	// only a blocked task shows as paused
	moving := prioritizedFor("moving", 0)
	moving.Prioritized, moving.Paused = nil, &data.FactoryFlag{Since: fakeNow.Add(-45 * time.Minute), WaitsOn: []string{"a"}}
	if _, label, c := m.phaseLook(moving, false); label != "motion" || c != th.Blue {
		t.Errorf("a paused flag on a task in motion changes its label or colour: %q", label)
	}
	// a task released but still declared paused reads as prioritized, muted, never as a stale pause
	both := pausedFor("released", 45*time.Minute, "a")
	both.Prioritized = &data.FactoryFlag{Since: fakeNow.Add(-2 * time.Minute)}
	if _, label, c := m.phaseLook(both, true); label != "blocked · prioritized 2m" || c != th.Muted {
		t.Errorf("both flags: %q in %v", label, c)
	}
	// a time in the future reads 0m
	ahead, rush := pausedFor("ahead", -time.Hour, "a"), prioritizedFor("rush", -time.Hour)
	if _, label, _ := m.phaseLook(ahead, true); label != "paused 0m: waits a" {
		t.Errorf("a pause from the future: %q", label)
	}
	if _, label, _ := m.phaseLook(rush, true); label != "motion · prioritized 0m" {
		t.Errorf("a prioritization from the future: %q", label)
	}
}

func TestFactoryPrioritizedLabelIsTheShortPhaseNameAndAge(t *testing.T) {
	review := prioritizedFor("in-review", 29*time.Minute)
	review.Phase = "review"
	noAge := prioritizedFor("no-age", 0)
	noAge.Prioritized.Since = time.Time{}
	for _, w := range []int{80, 100, 120} {
		m := factoryModel(false, w, 60, factoryRepo{name: "m", st: fstatus(5, prioritizedFor("rush", 3*time.Minute), review, ftask("plain", "motion"))})
		p, th := m.th.on(m.th.Bg), m.th
		rows := m.fac.repos[0].st.Rows
		cols := m.factoryCols(p, w, rows)
		for i, c := range []struct {
			label string
			col   color.Color
		}{{"motion · prioritized 3m", th.Blue}, {"review · prioritized 29m", th.Yellow}} {
			if got := m.taskRow(p, cols, rows[i], w)[0]; !strings.Contains(got, p.t(c.label, c.col)) {
				t.Errorf("%d columns: want %q in %v: %q", w, c.label, c.col, got)
			}
		}
		if _, label, _ := m.phaseLook(rows[2], w >= 100); label != "motion" && label != "in motion" {
			t.Errorf("%d columns: a task not prioritized keeps its label: %q", w, label)
		}
		if _, label, _ := m.phaseLook(noAge, w >= 100); label != "motion · prioritized" {
			t.Errorf("%d columns: a flag without a time shows without an age: %q", w, label)
		}
	}
}

func TestFactoryPausedAndPrioritizedLabelsKeepTheTable(t *testing.T) {
	long := strings.Repeat("x", 40)
	cols := func(w int, whom string) factoryCols {
		paused, review := pausedFor("docs-after-review", 45*time.Minute, whom), prioritizedFor("rush-fix", 29*time.Minute)
		paused.Size, review.Phase = "medium", "review"
		m := factoryModel(true, w, 40, factoryRepo{name: "m", st: fstatus(5, paused, review, ftask("api-rate-limits", "review"))})
		return m.factoryCols(m.th.on(m.th.Bg), w, m.fac.repos[0].st.Rows)
	}
	for _, w := range []int{80, 100, 120} {
		got, want := cols(w, long), cols(w, "a")
		if got.phase < 2+width("review · prioritized 29m") {
			t.Errorf("%d columns: PHASE %d cuts the prioritized label", w, got.phase)
		}
		got.phase, want.phase = 0, 0 // the label is cut to the cell
		got.size, want.size = 0, 0   // SIZE is the first column to give way to a wider PHASE
		if got != want || got.elapsed == 0 {
			t.Errorf("%d columns: 40 characters of slugs give %+v, one short slug %+v", w, got, want)
		}
	}
}

func TestFactoryColsKeepTheTableWhenAWhomIsLong(t *testing.T) {
	cols := func(w int, whom string) factoryCols {
		x := onTask()
		x.Slug, x.WaitsOn, x.Size = strings.Repeat("s", slugLeast), whom, "medium" // the least slug a clamped PHASE leaves
		m := factoryModel(true, w, 40, factoryRepo{name: "m", st: fstatus(5, x, ftask("quiet-refactor", "motion"), ftask("api-rate-limits", "review"))})
		return m.factoryCols(m.th.on(m.th.Bg), w, m.fac.repos[0].st.Rows)
	}
	long := strings.Repeat("x", 40)
	if c := cols(100, long); c.phase != 53 {
		t.Errorf("100 columns: PHASE is %d cells, it was clamped to make room for SIZE and the wider ELAPSED", c.phase)
	}
	for _, w := range []int{80, 100} {
		got, want := cols(w, long), cols(w, "dispatcher")
		got.phase, want.phase = 0, 0 // the label is cut to the cell
		got.size, want.size = 0, 0   // SIZE is the first column to give way to a wider PHASE
		if got != want || got.elapsed == 0 {
			t.Errorf("%d columns: a 40-character WaitsOn gives %+v, a short one %+v", w, got, want)
		}
	}
	if c := cols(140, "design-review-of-docs"); c.phase != 2+len("blocked on design-review-of-docs") {
		t.Errorf("140 columns: PHASE is %d cells, the 21-character slug is cut", c.phase)
	}
	wide := onTask()
	wide.Slug, wide.WaitsOn, wide.Overtime, wide.Stale = strings.Repeat("s", 40), long, true, true
	m := factoryModel(true, 130, 40, factoryRepo{name: "m", st: fstatus(5, wide)})
	if c := m.factoryCols(m.th.on(m.th.Bg), 130, m.fac.repos[0].st.Rows); c.slug != 40 || c.phase != phaseMax {
		t.Errorf("130 columns: a long whom may not cut a long slug: %+v", c)
	}
}

func TestFactoryColsShowSizeOnlyWhenSomeTaskHasOne(t *testing.T) {
	sized := ftask("sized", "motion")
	sized.Size = "small"
	for _, c := range []struct {
		name  string
		tasks []data.FactoryTask
		size  int
	}{
		{"none", []data.FactoryTask{ftask("a", "motion"), ftask("b", "review")}, 0},
		{"one", []data.FactoryTask{ftask("a", "motion"), sized}, 5},
	} {
		m := factoryModel(true, 140, 40, factoryRepo{name: "m", st: fstatus(5, c.tasks...)})
		got := m.factoryCols(m.th.on(m.th.Bg), 140, c.tasks)
		if got.size != c.size {
			t.Errorf("%s sized: SIZE is %d cells, want %d", c.name, got.size, c.size)
		}
	}
}

func TestFactoryWaitingLeavesOutADraftInReview(t *testing.T) {
	ready, draft, blocked := ftask("ready", "review"), ftask("draft", "review"), ftask("held", "blocked")
	draft.PRDraft, blocked.PRDraft = true, true
	m := factoryModel(true, 140, 40, factoryRepo{name: "m", st: fstatus(5, ready, draft, blocked)})
	var got []string
	for _, it := range m.waiting() {
		got = append(got, it.task.Slug)
	}
	if want := "held ready"; strings.Join(got, " ") != want {
		t.Fatalf("a review task whose pull request is a draft waits on checks, not the human: %q, want %q", strings.Join(got, " "), want)
	}
}

func TestFactoryWaitingWhatAndDo(t *testing.T) {
	rows := map[string]data.FactoryTask{}
	for _, r := range append(delta().st.Rows, alpha().st.Rows...) {
		rows[r.Slug] = r
	}
	for slug, want := range map[string][2]string{
		"api-rate-limits": {"review PR", "#42 https://github.com/o/alpha/pull/42"},
		"design-review":   {"see deliverable", "/r/delta/docs/design.md"},
		"video-cut":       {"see deliverable", "https://example.com/cut/v2"},
		"needs-an-answer": {"answer question", "Should the export keep the legacy column order?"},
		"wide-ask":        {"unblock", rows["wide-ask"].StopSummary},
		"slow-import":     {"decide: overtime", "ran 3h of 2h: split it?"},
		"stuck-on-keys":   {"blocked", ""},
	} {
		if what, do := (waitItem{"r", "", rows[slug]}).todo(); what != want[0] || do != want[1] {
			t.Errorf("%s: %q %q, want %q %q", slug, what, do, want[0], want[1])
		}
	}
	if what, do := (waitItem{"r", "", ftask("x", "review")}).todo(); what != "review" || do != "" {
		t.Errorf("review without anything to show: %q %q", what, do)
	}
	multi := ftask("x", "blocked")
	multi.StopKind, multi.StopSummary = "QUESTION", "two\nlines  of text"
	if _, do := (waitItem{"r", "", multi}).todo(); do != "two lines of text" {
		t.Errorf("a summary is one line: %q", do)
	}
	out := plain(factoryModel(true, 140, 40, waitingRepos()...))
	for _, want := range []string{"▲ Needs you  7", "◆ delta-docs  design-review", "▲ delta-docs  stuck-on-keys    blocked", "no reason recorded: ask its lead", "-  /r/delta/docs/design.md"} {
		if !strings.Contains(out, want) {
			t.Errorf("band lacks %q:\n%s", want, out)
		}
	}
}

func TestFactoryBandWhenNothingWaitsIsOneLineBeforeTheSettings(t *testing.T) {
	lines := strings.Split(plain(factoryModel(true, 100, 30, factoryRepo{name: "calm", st: fstatus(5, ftask("a", "motion"))})), "\n")
	i := slices.IndexFunc(lines, func(l string) bool { return strings.HasPrefix(l, " ✓ nothing waits on you") })
	if i < 0 || !strings.HasPrefix(lines[i+1], " worktree cap") {
		t.Fatalf("one line, then the settings line:\n%s", strings.Join(lines[:7], "\n"))
	}
	if out := plain(factoryModel(true, 100, 30)); strings.Contains(out, "nothing waits") || strings.Contains(out, "Needs you") {
		t.Errorf("no repositories, no band:\n%s", out)
	}
	m := fakeModel(true, 100, 30)
	m.view = viewInbox
	if out := plain(m); strings.Contains(out, "nothing waits") {
		t.Errorf("no band while loading:\n%s", out)
	}
}

func TestFactoryBandDoCellWrapsWhenNarrowAndTruncates(t *testing.T) {
	narrow := bandLines(plain(factoryModel(true, 60, 40, waitingRepos()...)))
	i := slices.IndexFunc(narrow, func(l string) bool { return strings.Contains(l, "stuck-on-keys") })
	if i < 0 || i+1 >= len(narrow) {
		t.Fatalf("no stuck-on-keys row with a line under it:\n%s", strings.Join(narrow, "\n"))
	}
	slug := len([]rune(narrow[i][:strings.Index(narrow[i], "stuck-on-keys")]))
	if !strings.HasPrefix(narrow[i+1], strings.Repeat(" ", slug)+"no reason recorded") {
		t.Errorf("a do cell under 30 cells goes under the slug column:\n%s", strings.Join(narrow, "\n"))
	}
	for _, w := range []int{100, 140} {
		var wide string
		for _, l := range bandLines(plain(factoryModel(true, w, 40, waitingRepos()...))) {
			if strings.Contains(l, "wide-ask") {
				wide = l
			}
		}
		if !strings.Contains(wide, "データベース") || !strings.Contains(wide, "…") {
			t.Errorf("%d columns: a long wide-character summary is cut with an ellipsis:\n%s", w, wide)
		}
	}
}

// At 80 columns seven tabs leave the badge no room beside "N live": the word goes, the count stays.
func TestFactoryTabBadgeKeepsTheLiveCountAt80Columns(t *testing.T) {
	for _, n := range []int{0, 7, 12, 123} {
		for _, v := range []viewID{viewDashboard, viewFactory} {
			m := fakeModel(true, 80, 24)
			m.view = v
			for _, s := range m.sessions {
				m.seen[s.ID] = m.now()
			}
			rows := make([]data.FactoryTask, n)
			for i := range rows {
				rows[i] = ftask(fmt.Sprintf("t%d", i), "review")
			}
			m.fac.repos, m.fac.doneAt = []factoryRepo{{name: "r", st: fstatus(5, rows...)}}, m.now()
			live := m.liveCount()
			head := strings.Split(plain(m), "\n")[0]
			badge, count := fmt.Sprintf("1 ✉ ▲%d", n), fmt.Sprintf("● %d ", live)
			if n == 0 {
				badge = "1 ✉"
			}
			if live == 0 || !strings.Contains(head, badge) || !strings.Contains(head, count) || !strings.Contains(head, "[7d]") {
				t.Errorf("view %d, %d waiting, %d live: %q", v, n, live, head)
			}
		}
	}
}

func TestFactoryTabBadge(t *testing.T) {
	for _, v := range []viewID{viewInbox, viewDashboard, viewAgents, viewFactory, viewKaizen} {
		m := fakeModel(true, 140, 40)
		m.view = v
		m.fac.repos, m.fac.doneAt = waitingRepos(), m.now()
		head := strings.Split(plain(m), "\n")[0]
		want := " 1 Inbox ▲7 "
		if v == viewInbox {
			want = "[1 Inbox ▲7]"
		}
		if !strings.Contains(head, want) {
			t.Errorf("view %d: header %q lacks %q", v, head, want)
		}
		m.fac.repos = []factoryRepo{{name: "calm", st: fstatus(5, ftask("a", "motion"))}}
		if head = strings.Split(plain(m), "\n")[0]; strings.Contains(head, "▲") {
			t.Errorf("view %d: nothing waits, no badge: %q", v, head)
		}
	}
	m := fakeModel(false, 140, 40)
	m.fac.repos = fakeFactoryRepos()
	if want := m.th.on(m.th.Surface).b(" ▲2", m.th.Orange); !strings.Contains(m.Render(), want) {
		t.Error("the badge is bold orange")
	}
	m = fakeModel(true, 80, 24)
	m.fac.repos = fakeFactoryRepos()
	if head := strings.Split(plain(m), "\n")[0]; !strings.Contains(head, "1 ✉ ▲2") {
		t.Errorf("short tab labels keep the badge: %q", head)
	}
}

func TestFactoryCells(t *testing.T) {
	pr := func(phase, state, url string) string {
		x := ftask("x", phase)
		x.PRState, x.PRURL = state, url
		return prText(x)
	}
	u := "https://github.com/o/r/pull/7"
	for got, want := range map[string]string{
		pr("motion", "none", ""): "-", pr("motion", "unknown", ""): "?", pr("motion", "open", u): "#7 open", pr("review", "open", u): "#7",
		pr("review", "closed", u): "#7 closed", pr("merged", "merged", u): "#7", pr("motion", "open", ""): "open",
	} {
		if got != want {
			t.Errorf("pr cell %q, want %q", got, want)
		}
	}
	dl := func(url, deliverable string) string {
		x := ftask("x", "settled")
		x.PRURL, x.Deliverable = url, deliverable
		return prText(x)
	}
	for _, c := range [][3]string{{"", "https://example.com/a/b", "example.com/a/b"}, {"", "/r/docs/notes.md", "notes.md"}, {u, "/r/docs/notes.md", "-"}, {"", "", "-"}} {
		if got := dl(c[0], c[1]); got != c[2] {
			t.Errorf("deliverable cell for %q and %q is %q, want %q", c[0], c[1], got, c[2])
		}
	}
	if b, s := baseText("worktree-login-flow"); b != "↳ login-flow" || !s {
		t.Errorf("stacked base %q %v", b, s)
	}
	if b, s := baseText("origin/main"); b != "main" || s {
		t.Errorf("default base %q %v", b, s)
	}
	m := factoryModel(true, 140, 40, factoryRepo{name: "r", path: "/r", st: fstatus(5, ftask("no-time", "motion"))})
	if !strings.Contains(plain(m), "no-time") || !strings.Contains(plain(m), "-  -") {
		t.Errorf("a task without an elapsed time shows -:\n%s", plain(m))
	}
}

func TestFactoryEmptyAndFirstResult(t *testing.T) {
	m := factoryModel(true, 100, 30)
	out := plain(m)
	for _, want := range []string{"No factory tasks here yet", "/factory:dispatch", "worktree cap", "c edit", "~/.claude/factory/settings.toml"} {
		if !strings.Contains(out, want) {
			t.Errorf("empty state lacks %q:\n%s", want, out)
		}
	}
	m = fakeModel(true, 100, 30)
	m.view = viewInbox
	if out := plain(m); !strings.Contains(out, "asking the factory…") || strings.Contains(out, "No factory tasks") {
		t.Errorf("before the first result:\n%s", out)
	}
}

func TestFactorySubheaderAndSettingsLine(t *testing.T) {
	m := factoryModel(true, 140, 40, fakeFactoryRepos()...)
	out := plain(m)
	for _, want := range []string{"Inbox  10 in motion (1 blocked) · 1 waiting for review · 2 merged, not cleaned · 1 settled  · as of 11:00:00", "worktree cap  5  per repo", "⚠ not inside a git repository", "in motion ●●● 3/2"} {
		if !strings.Contains(out, want) {
			t.Errorf("lacks %q:\n%s", want, out)
		}
	}
	m.fac.busy = true
	if out := plain(m); !strings.Contains(out, "refreshing") || strings.Contains(out, "as of") {
		t.Errorf("a refresh in flight shows refreshing:\n%s", out)
	}
	m.fac.capErr = "~/settings.toml line 3: the list is not closed"
	if out := plain(m); !strings.Contains(out, "worktree cap  ~/settings.toml line 3: the list is not closed") {
		t.Errorf("a broken settings file shows its error instead of a number:\n%s", out)
	}
	m.fac.capErr, m.fac.editing, m.fac.field = "", true, "12"
	if out := plain(m); !strings.Contains(out, "[ 12█ ]") || !strings.Contains(out, "enter save · esc cancel · ↑↓ adjust") || !strings.Contains(out, "save") {
		t.Errorf("editing:\n%s", out)
	}
}

func TestFactoryScroll(t *testing.T) {
	m := factoryModel(true, 80, 24, factoryRepo{name: "busy", path: "/r", st: fstatus(40, manyTasks(30)...)})
	if out := plain(m); !strings.Contains(out, "more lines · ↓ to scroll") {
		t.Fatalf("a cut body says so:\n%s", out)
	}
	send(m, "j")
	if m.inbox.top != 1 {
		t.Fatalf("j scrolls one line, top=%d", m.inbox.top)
	}
	send(m, "k", "k")
	if m.inbox.top != 0 {
		t.Fatalf("top stays at 0, got %d", m.inbox.top)
	}
	send(m, "G")
	out := plain(m)
	if strings.Contains(out, "more lines") || !strings.Contains(out, "task-29") || strings.Contains(out, "task-00") {
		t.Fatalf("G shows the end:\n%s", out)
	}
	send(m, "j")
	if want := m.factoryPositions() - 1; m.inbox.top != want {
		t.Fatalf("scrolling stops at the end: top=%d want %d", m.inbox.top, want)
	}
	send(m, "g")
	if m.inbox.top != 0 {
		t.Fatal("g goes to the top")
	}
	m.w, m.h = 80, 60
	m.inbox.top = 3
	m.fixScroll()
	if m.inbox.top != 0 {
		t.Fatalf("a taller window pulls the scroll back, top=%d", m.inbox.top)
	}
}

func TestFactoryAnimatesOnlyWhileSomethingMoves(t *testing.T) {
	m := factoryModel(true, 100, 30, alpha())
	if !m.animating() {
		t.Fatal("a working task turns its spinner")
	}
	quiet := ftask("quiet", "motion")
	m.fac.repos = []factoryRepo{{name: "q", path: "/q", st: fstatus(5, quiet)}}
	if m.animating() {
		t.Fatal("nothing moves")
	}
	m.fac.busy = true
	if !m.animating() {
		t.Fatal("a refresh in flight turns its spinner")
	}
	m.view = viewDashboard
	m.fac.repos = []factoryRepo{alpha()}
	if m.animating() {
		t.Fatalf("working tasks animate only on the factory view")
	}
}

func TestFactoryViewKeys(t *testing.T) {
	m := fakeModel(true, 120, 40)
	for _, want := range []viewID{viewAgents, viewFactory, viewKaizen, viewOps, viewHanami, viewInbox} {
		send(m, "tab")
		if m.view != want {
			t.Fatalf("tab -> %v, want %v", m.view, want)
		}
	}
	send(m, "shift+tab", "shift+tab", "shift+tab")
	if m.view != viewKaizen {
		t.Fatalf("shift+tab three times from the inbox, past hanami and ops -> %v", m.view)
	}
	send(m, "2", "4")
	if m.view != viewFactory {
		t.Fatal("4 opens the factory view")
	}
	send(m, "esc")
	if m.view != viewInbox {
		t.Fatal("esc goes back to the inbox")
	}
	if !strings.Contains(plain(m), "4 Factory") {
		t.Fatal("the tab is in the header")
	}

	bare := New(Options{Static: true})
	bare.loading = false
	for _, want := range []viewID{viewDashboard, viewAgents, viewFactory, viewKaizen, viewOps, viewHanami} {
		send(bare, "tab")
		if bare.view != want || bare.toast.text != "" {
			t.Fatalf("tab without sessions: got %v, want %v, toast %q", bare.view, want, bare.toast.text)
		}
	}
	send(bare, "shift+tab", "shift+tab", "shift+tab", "shift+tab", "shift+tab", "shift+tab")
	if bare.view != viewInbox {
		t.Fatalf("shift+tab six times: %v", bare.view)
	}
}

func TestHeaderKeepsTheActiveRangeChipAtSixtyColumns(t *testing.T) {
	for _, view := range []viewID{viewInbox, viewDashboard, viewAgents, viewFactory, viewKaizen} {
		m := fakeModel(true, 60, 20)
		m.view = view
		header := strings.Split(plain(m), "\n")[0]
		if want := "[" + ranges[m.rangeIdx].label + "]"; !strings.Contains(header, want) || !strings.Contains(header, "4 Factory") || !strings.Contains(header, "5 Kaizen") {
			t.Errorf("view %v: header lacks %s or the fourth tab: %q", view, want, header)
		}
	}
}

func TestFactoryHelpAndFooter(t *testing.T) {
	m := factoryModel(true, 120, 40, alpha())
	foot := plain(m)
	for _, want := range []string{"↑↓/jk move", "c edit cap", "tab next view", "? help", "q quit"} {
		if !strings.Contains(foot, want) {
			t.Errorf("footer lacks %q", want)
		}
	}
	floor := plain(floorModel(true, 120, 40, alpha()))
	for _, want := range []string{"/ filter", "r range", "tab next view", "? help", "q quit"} {
		if !strings.Contains(floor, want) {
			t.Errorf("the Factory tab's footer lacks %q", want)
		}
	}
	if strings.Contains(floor, "edit cap") {
		t.Errorf("the Factory tab has no cap key:\n%s", floor)
	}
	m.fac.editing = true
	if foot := plain(m); !strings.Contains(foot, "enter save  ·  esc cancel") {
		t.Errorf("editing footer:\n%s", foot)
	}
	m.fac.editing, m.help = false, true
	out := plain(m)
	for _, want := range []string{"1 2 3 4 5 6 7", "inbox sessions agents factory kaizen", "←→/hl", "kaizen page", "c", "edit cap", "every 30 s while shown, asking GitHub; every 2 min off it, not"} {
		if !strings.Contains(out, want) {
			t.Errorf("help lacks %q:\n%s", want, out)
		}
	}
}

// ---- goldens and screenshots ------------------------------------------------------

func TestGoldenInboxTable(t *testing.T) {
	shots := os.Getenv("ISSHONI_SHOT_DIR")
	for _, c := range []struct {
		name string
		w, h int
		set  func(m *model)
		gold bool
	}{
		{"inbox-table-140x40", 140, 40, func(m *model) { setFactory(m, waitingRepos()...) }, true},
		{"inbox-table-100x30", 100, 30, func(m *model) { setFactory(m, waitingRepos()...) }, true},
		{"inbox-table-80x24", 80, 24, func(m *model) { setFactory(m, waitingRepos()...) }, true},
		{"inbox-table-blocked-on-100x30", 100, 30, func(m *model) { setFactory(m, waitingRepos()...); m.inbox.top = 1 << 10; m.fixScroll() }, true},
		{"inbox-table-blocked-on-80x24", 80, 24, func(m *model) { setFactory(m, waitingRepos()...); m.inbox.top = 1 << 10; m.fixScroll() }, true},
		{"inbox-table-quiet-80x24", 80, 24, func(m *model) {
			setFactory(m, factoryRepo{name: "calm", path: "/r/calm", st: fstatus(5, manyTasks(2)...)})
		}, true},
		{"inbox-table-prs-stale-100x30", 100, 30, func(m *model) {
			repos := fakeFactoryRepos()
			repos[0].st.PRs.AsOf = ptr(float64(m.now().Add(-12 * time.Minute).Unix()))
			repos[0].fetchErr = "factory status timed out after 25s"
			repos[1].st.PRs.AsOf = ptr(float64(m.now().Add(-3 * time.Minute).Unix()))
			setFactory(m, repos...)
		}, true},
		{"inbox-table-empty-80x24", 80, 24, func(m *model) { setFactory(m) }, true},
		{"inbox-table-edit-100x30", 100, 30, func(m *model) { setFactory(m, fakeFactoryRepos()...); m.fac.editing, m.fac.field = true, "12" }, false},
		{"inbox-table-busy-140x40", 140, 40, func(m *model) {
			busy := alpha()
			busy.name = "isshoni"
			setFactory(m, busy, factoryRepo{name: "agent", path: "/r/agent", st: fstatus(5, manyTasks(7)...)}, factoryRepo{name: "zahn-os", path: "/r/z", st: fstatus(40, manyTasks(12)...)})
		}, false},
	} {
		t.Run(c.name, func(t *testing.T) { goldenCase(t, shots, c.name, c.w, c.h, c.set, c.gold) })
	}
}

// goldenCase renders the state set builds at w x h: in colour to shots/<name>.ansi when shots is set, and, when
// gold, in plain text against testdata/<name>.golden (rewritten under -update).
func goldenCase(t *testing.T, shots, name string, w, h int, set func(m *model), gold bool) {
	t.Helper()
	if shots != "" {
		m := fakeModel(false, w, h)
		set(m)
		if err := os.WriteFile(filepath.Join(shots, name+".ansi"), []byte(m.Render()+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if !gold {
		return
	}
	m := fakeModel(true, w, h)
	set(m)
	got := m.Render() + "\n"
	path := filepath.Join("testdata", name+".golden")
	if *update {
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run go test ./internal/tui -update)", err)
	}
	if got != string(want) {
		t.Fatalf("%s changed; run go test ./internal/tui -update and review the diff\n--- got ---\n%s", name, got)
	}
}

// ---- update loop with the stub ----------------------------------------------------

// factoryStubModel has two repositories with a worktrees directory and one without.
func factoryStubModel(t *testing.T) (*model, string, *time.Time) {
	t.Helper()
	m, dir := stubModel(t)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("FACTORY_HOME", filepath.Dir(data.FactorySettingsPath()))
	for id, name := range map[string]string{sidA: "one", sidB: "two", "cccccccc-0003": "three"} {
		repo := filepath.Join(t.TempDir(), name)
		if name != "three" {
			if err := os.MkdirAll(filepath.Join(repo, ".claude", "worktrees"), 0o755); err != nil {
				t.Fatal(err)
			}
		}
		m.sessions[id].RepoPath = repo
	}
	now := t0.Add(time.Hour)
	m.now = func() time.Time { return now }
	return m, dir, &now
}

func factoryCalls(t *testing.T, dir string) int { return calls(t, dir, "factory ") }

func TestFactoryEnteringStartsOneRefresh(t *testing.T) {
	m, dir, _ := factoryStubModel(t)
	got := only[factoryMsg](upd(m, press("1")))
	if len(got) != 1 || !m.fac.busy || factoryCalls(t, dir) != 2 {
		t.Fatalf("entering asks each repository with a worktrees directory once: %d results, busy=%v, %d calls", len(got), m.fac.busy, factoryCalls(t, dir))
	}
	if len(got[0].repos) != 2 || got[0].repos[0].name != "one" || got[0].repos[1].name != "two" {
		t.Fatalf("repositories: %+v", got[0].repos)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "calls")); strings.Contains(string(b), "/three") {
		t.Fatalf("a repository without a worktrees directory must not be asked:\n%s", b)
	}
	if more := upd(m, press("2")); len(more) != 0 {
		t.Fatal("leaving starts nothing")
	}
	if more := upd(m, press("1")); len(only[factoryMsg](more)) != 0 || factoryCalls(t, dir) != 2 {
		t.Fatal("one refresh in flight at most")
	}
	upd(m, got[0])
	if m.fac.busy || len(m.fac.repos) != 2 || m.fac.doneAt.IsZero() || m.fac.cap != 5 {
		t.Fatalf("the result is applied: %+v", m.fac)
	}
}

func TestFactoryCadence(t *testing.T) {
	m, dir, now := factoryStubModel(t)
	upd(m, only[factoryMsg](upd(m, press("1")))[0])
	if got := only[factoryMsg](upd(m, tickMsg{})); len(got) != 0 {
		t.Fatal("a tick right after a result starts nothing")
	}
	*now = now.Add(factoryEvery - time.Second)
	if got := only[factoryMsg](upd(m, tickMsg{})); len(got) != 0 {
		t.Fatal("a tick before factoryEvery starts nothing")
	}
	*now = now.Add(2 * time.Second)
	got := only[factoryMsg](upd(m, tickMsg{}))
	if len(got) != 1 || factoryCalls(t, dir) != 4 {
		t.Fatalf("a tick after factoryEvery starts one refresh: %d, %d calls", len(got), factoryCalls(t, dir))
	}
	if more := only[factoryMsg](upd(m, tickMsg{})); len(more) != 0 {
		t.Fatal("a tick while one is in flight starts nothing")
	}
	upd(m, got[0])

	send(m, "2")
	*now = now.Add(time.Hour)
	if got := only[factoryMsg](upd(m, press("1"))); len(got) != 1 {
		t.Fatal("entering after a long time refreshes")
	}
}

func TestFactoryRefreshesOffViewEveryTwoMinutes(t *testing.T) {
	m, dir, now := factoryStubModel(t)
	first := only[factoryMsg](upd(m, tickMsg{}))
	if len(first) != 1 || m.view != viewDashboard {
		t.Fatalf("the first result is fetched off the view: %d", len(first))
	}
	upd(m, first[0])
	calls := factoryCalls(t, dir)
	*now = now.Add(factoryIdleEvery - time.Second)
	if got := only[factoryMsg](upd(m, tickMsg{})); len(got) != 0 {
		t.Fatal("a tick before 2 min starts nothing off the view")
	}
	*now = now.Add(2 * time.Second)
	got := only[factoryMsg](upd(m, tickMsg{}))
	if len(got) != 1 || factoryCalls(t, dir) != calls+2 {
		t.Fatalf("a tick after 2 min starts one refresh: %d, %d calls", len(got), factoryCalls(t, dir)-calls)
	}
	upd(m, got[0])
	if m.fac.doneAt != *now {
		t.Fatal("the off-view result is applied")
	}
	m.loaded = false
	*now = now.Add(time.Hour)
	if got := only[factoryMsg](upd(m, tickMsg{})); len(got) != 0 {
		t.Fatal("no refresh before the sessions have loaded")
	}
	m.loaded, m.sessions = true, nil
	got = only[factoryMsg](upd(m, tickMsg{}))
	if len(got) != 1 {
		t.Fatal("a range without sessions still refreshes")
	}
	upd(m, got[0])
	if len(m.fac.repos) != 0 {
		t.Fatalf("a range without sessions clears the count: %d repositories", len(m.fac.repos))
	}
}

func TestFactoryQuietLineSaysWhenARepositoryDidNotAnswer(t *testing.T) {
	out := plain(factoryModel(true, 100, 30, factoryRepo{name: "calm", st: fstatus(5)}, factoryRepo{name: "gone", err: "boom"}))
	if !strings.Contains(out, " nothing waits on you in the repositories that answered") || strings.Contains(out, "✓") {
		t.Fatalf("the quiet line must not vouch for a repository that failed:\n%s", out)
	}
}

func TestFactorySecondEntryWithinFiveSeconds(t *testing.T) {
	m, _, now := factoryStubModel(t)
	upd(m, only[factoryMsg](upd(m, press("1")))[0])
	send(m, "2")
	*now = now.Add(4 * time.Second)
	if got := only[factoryMsg](upd(m, press("1"))); len(got) != 0 {
		t.Fatal("a result younger than 5 s is kept")
	}
	send(m, "2")
	*now = now.Add(2 * time.Second)
	if got := only[factoryMsg](upd(m, press("1"))); len(got) != 1 {
		t.Fatal("an older one is refreshed")
	}
}

func TestFactoryResultForAChangedRepositoryListIsAppliedWhole(t *testing.T) {
	m, _, _ := factoryStubModel(t)
	upd(m, only[factoryMsg](upd(m, press("1")))[0])
	upd(m, factoryMsg{repos: []factoryRepo{{name: "other", path: "/x/other", st: fstatus(9)}}, cap: 5})
	if len(m.fac.repos) != 1 || m.fac.repos[0].name != "other" || m.fac.cap != 9 {
		t.Fatalf("applied whole: %+v", m.fac)
	}
}

func TestFactoryRangeChangeRefreshesWhenTheSessionsArrive(t *testing.T) {
	m, dir, _ := factoryStubModel(t)
	upd(m, only[factoryMsg](upd(m, press("1")))[0])
	before := factoryCalls(t, dir)
	res := &data.SessionsResult{}
	for _, s := range m.sessions {
		res.Sessions = append(res.Sessions, *s)
	}
	if got := only[factoryMsg](upd(m, sessionsMsg{gen: m.loadGen, res: res})); len(got) != 1 || factoryCalls(t, dir) != before+2 {
		t.Fatalf("a new session list refreshes the shown view: %d", len(got))
	}
}

func TestFactoryErrorsAreOneLinePerRepository(t *testing.T) {
	m, dir, _ := factoryStubModel(t)
	os.WriteFile(filepath.Join(dir, "fail"), nil, 0o644)
	upd(m, only[factoryMsg](upd(m, press("1")))[0])
	for _, r := range m.fac.repos {
		if r.st != nil || r.err != "not inside a git repository" {
			t.Fatalf("%+v", r)
		}
	}
	if out := plain(m); strings.Count(out, "⚠ not inside a git repository") != 2 {
		t.Fatalf("each failed repository shows its error:\n%s", out)
	}
}

// ---- the cap field ----------------------------------------------------------------

func settingsFile(t *testing.T, content string) string {
	t.Helper()
	t.Setenv("HOME", t.TempDir())
	path := data.FactorySettingsPath()
	t.Setenv("FACTORY_HOME", filepath.Dir(path)) // the script and the dashboard read the same file
	if content != "" {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return path
}

// realFactory puts the repository's factory script first on PATH, with its store where the dashboard looks.
func realFactory(t *testing.T) {
	t.Helper()
	if h := os.Getenv("HOME"); h == realHome || h == "" {
		t.Fatalf("HOME=%q is the real home: make the test's temporary home first", h)
	}
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 is missing")
	}
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("ISSHONI_ROOT", root)
	requireTempFactoryHome(t) // after the test has made its temporary HOME and FACTORY_HOME
}

func capModel(t *testing.T, content string) (*model, string) {
	path := settingsFile(t, content)
	m := factoryModel(true, 100, 30, alpha())
	return m, path
}

func TestCapFieldOpensWithTheFileValue(t *testing.T) {
	m, _ := capModel(t, "[limits]\nworktree_cap = 12\n")
	send(m, "c")
	if !m.fac.editing || m.fac.field != "12" {
		t.Fatalf("editing=%v field=%q", m.fac.editing, m.fac.field)
	}
	m, _ = capModel(t, "")
	send(m, "c")
	if m.fac.field != "5" {
		t.Fatalf("no file: the default, got %q", m.fac.field)
	}
	dash := fakeModel(true, 100, 30)
	send(dash, "c")
	if dash.fac.editing {
		t.Fatal("c does nothing outside the factory view")
	}
}

func TestCapFieldTyping(t *testing.T) {
	m, _ := capModel(t, "")
	send(m, "c", "backspace", "backspace")
	if m.fac.field != "" {
		t.Fatalf("backspace erases: %q", m.fac.field)
	}
	send(m, "1", "2", "3", "4", "x")
	if m.fac.field != "123" {
		t.Fatalf("three digits at most, digits only: %q", m.fac.field)
	}
	send(m, "backspace")
	if m.fac.field != "12" {
		t.Fatalf("%q", m.fac.field)
	}
	send(m, "up", "k")
	if m.fac.field != "14" {
		t.Fatalf("up and k add one: %q", m.fac.field)
	}
	send(m, "down", "j", "j")
	if m.fac.field != "11" {
		t.Fatalf("down and j subtract one: %q", m.fac.field)
	}
}

func TestCapFieldBounds(t *testing.T) {
	m, _ := capModel(t, "[limits]\nworktree_cap = 2\n")
	send(m, "c", "down", "down", "down")
	if m.fac.field != "1" {
		t.Fatalf("never below 1: %q", m.fac.field)
	}
	send(m, "backspace", "down")
	if m.fac.field != "1" {
		t.Fatalf("an empty field steps to 1: %q", m.fac.field)
	}
	send(m, "backspace", "9", "9", "9", "up")
	if m.fac.field != "999" {
		t.Fatalf("never above 999: %q", m.fac.field)
	}
}

func TestCapFieldRefusesInvalidAndKeepsOpen(t *testing.T) {
	m, path := capModel(t, "")
	send(m, "c", "backspace", "enter")
	if !m.fac.editing || m.fac.fieldErr == "" {
		t.Fatalf("an empty field stays open with a message: %+v", m.fac)
	}
	if !strings.Contains(plain(m), m.fac.fieldErr) {
		t.Fatal("the message is on screen")
	}
	send(m, "0", "enter")
	if !m.fac.editing || m.fac.fieldErr == "" {
		t.Fatal("0 stays open with a message")
	}
	if _, err := os.Stat(path); err == nil {
		t.Fatal("nothing was written")
	}
	send(m, "7")
	if m.fac.fieldErr != "" {
		t.Fatal("typing clears the message")
	}
}

func TestCapFieldEscLeavesTheFileUntouched(t *testing.T) {
	m, path := capModel(t, "[limits]\nworktree_cap = 3\n")
	send(m, "c", "9", "esc")
	if m.fac.editing {
		t.Fatal("esc closes the field")
	}
	if b, _ := os.ReadFile(path); string(b) != "[limits]\nworktree_cap = 3\n" {
		t.Fatalf("file: %q", b)
	}
}

func TestTheFactoryTabHasNoCapKeyAndNoScroll(t *testing.T) {
	m := floorModel(true, 100, 30, factoryRepo{name: "busy", path: "/r", st: fstatus(40, manyTasks(30)...)})
	before := plain(m)
	send(m, "c", "j", "G", "pgdown")
	if m.fac.editing || m.inbox.top != 0 || m.toast.text != "" || plain(m) != before {
		t.Fatalf("c and the scroll keys do nothing on the floor: editing=%v inbox top=%d toast=%q", m.fac.editing, m.inbox.top, m.toast.text)
	}
	m.view = viewInbox
	send(m, "c")
	if !m.fac.editing {
		t.Fatal("c opens the cap field on the Inbox")
	}
}

func TestCapFieldSwallowsOtherKeys(t *testing.T) {
	m, _ := capModel(t, "")
	send(m, "c")
	for _, k := range []string{"q", "tab", "4", "1", "?", "r", "s"} {
		if cmd := m.handleKey(press(k)); cmd != nil {
			t.Fatalf("%s did something while the field is open", k)
		}
		if m.view != viewInbox || !m.fac.editing || m.help {
			t.Fatalf("%s changed the screen: view=%v editing=%v help=%v", k, m.view, m.fac.editing, m.help)
		}
	}
	if cmd := m.handleKey(press("ctrl+c")); cmd == nil {
		t.Fatal("ctrl+c still quits")
	}
}

func TestCapFieldRefusesAFileItCannotRead(t *testing.T) {
	for _, content := range []string{"[limits]\nworktree_cap = [1\n"} {
		m, path := capModel(t, content)
		realFactory(t)
		send(m, "c")
		if want := data.ShortPath(path) + " line 2: the list holds something other than basic strings"; m.fac.editing || m.fac.capErr != want || !strings.Contains(plain(m), data.ShortPath(path)+" line 2") {
			t.Fatalf("%s: reported by path and line and not opened: editing=%v capErr=%q", content, m.fac.editing, m.fac.capErr)
		}
		m.fac.editing, m.fac.field = true, "4"
		send(m, "enter")
		if !m.fac.editing || !strings.Contains(m.fac.fieldErr, "settings.toml") {
			t.Fatalf("saving reports it too: %+v", m.fac)
		}
		if b, _ := os.ReadFile(path); string(b) != content {
			t.Fatalf("the file was written: %q", b)
		}
	}
}

func TestCapFieldRepairsAnInvalidValue(t *testing.T) {
	for _, bad := range []string{`0`, `"3"`, `true`} {
		m, path := capModel(t, "[models]\nlead = \"x\"\n[limits]\nworktree_cap = "+bad+"\n")
		realFactory(t)
		send(m, "c")
		if !m.fac.editing || m.fac.field != "5" {
			t.Fatalf("worktree_cap %s: the field opens prefilled with 5: editing=%v field=%q", bad, m.fac.editing, m.fac.field)
		}
		send(m, "enter")
		if got, err := data.ReadWorktreeCap(path); err != nil || got != 5 || m.fac.editing {
			t.Fatalf("worktree_cap %s: saving repairs the file: %d, %v", bad, got, err)
		}
	}
}

func TestCapSaveWritesTheFileAndRefreshes(t *testing.T) {
	m, dir, _ := factoryStubModel(t)
	realFactory(t)
	path := data.FactorySettingsPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("# mine\n[limits]\nworktree_cap = 3 # three\n[models]\nlead = \"fable\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	m.view = viewInbox
	send(m, "c", "backspace", "backspace", "8")
	got := only[factoryMsg](upd(m, press("enter")))
	b, _ := os.ReadFile(path)
	if string(b) != "# mine\n[limits]\nworktree_cap = 8 # three\n[models]\nlead = \"fable\"\n" {
		t.Fatalf("file: %q", b)
	}
	if m.fac.editing || m.fac.cap != 8 || len(got) != 1 || factoryCalls(t, dir) != 2 || m.toast.text == "" {
		t.Fatalf("saved: editing=%v cap=%d refreshes=%d calls=%d toast=%q", m.fac.editing, m.fac.cap, len(got), factoryCalls(t, dir), m.toast.text)
	}
	if got[0].cap != 8 || got[0].capErr != nil {
		t.Fatalf("the refresh reads the new cap: %+v", got[0])
	}
}

func TestCapSaveDuringARefreshFollowsIt(t *testing.T) {
	m, dir, _ := factoryStubModel(t)
	realFactory(t)
	first := only[factoryMsg](upd(m, press("1")))
	send(m, "c", "6")
	if got := only[factoryMsg](upd(m, press("enter"))); len(got) != 0 {
		t.Fatal("one refresh in flight at most")
	}
	if !m.fac.again {
		t.Fatal("the save asks for another refresh")
	}
	second := only[factoryMsg](upd(m, first[0]))
	if len(second) != 1 || m.fac.again || factoryCalls(t, dir) != 4 {
		t.Fatalf("a second refresh follows the first: %d, again=%v, calls=%d", len(second), m.fac.again, factoryCalls(t, dir))
	}
}

// ---- snapshot ---------------------------------------------------------------------

func TestSnapshotFactory(t *testing.T) {
	table := []string{"proj", "stacked", "cost-in-money", "in motion ▲▲●●○○◌ 4/6+1", "held 7h5m · ended-undeclared · its replacement", "kept: uncommitted changes", "paused 12m: waits nee", "Needs the login task to land first.", "write-up.md", "needs-an-answer", "worktree cap  7", "[1 ✉ ▲2]", "▲ Needs you  2", "review PR", "1h30m", "answer question", "Which database should it use?"}
	floor := []string{"BUILD 2", "REVIEW 1", "MERGE 1", "SHIPPED 1", "BLOCKED 2", "in motion ▲▲●●○○◌ 4/6+1", "1 ✉ ▲2", "[4 Factory]"}
	for _, c := range []struct {
		view string
		want []string
	}{{"inbox", table}, {"factory", floor}} {
		t.Run(c.view, func(t *testing.T) {
			dir := t.TempDir()
			t.Setenv("STUBDIR", dir)
			t.Setenv("HOME", t.TempDir())
			repo := filepath.Join(t.TempDir(), "proj")
			if err := os.MkdirAll(filepath.Join(repo, ".claude", "worktrees"), 0o755); err != nil {
				t.Fatal(err)
			}
			sessions := fmt.Sprintf(`{"sessions":[{"id":"s1","project":"proj","repo":"proj","repo_path":%q}],"malformed":0}`, repo)
			status, err := os.ReadFile("../data/testdata/factory-status.json")
			if err != nil {
				t.Fatal(err)
			}
			os.WriteFile(filepath.Join(dir, "sessions.json"), []byte(sessions), 0o644)
			os.WriteFile(filepath.Join(dir, "status.json"), status, 0o644)
			py, _ := filepath.Abs("../data/testdata/fakepy.sh")
			root, _ := filepath.Abs("../..")
			r := &data.Runner{Root: root, Python: py, ProjectsDir: dir, Timeout: 5 * time.Second}
			out, err := Snapshot(SnapshotOptions{Options: Options{Runner: r, NoColor: true, Range: "all", Now: func() time.Time { return goldenNow }}, View: c.view, W: 100, H: 30})
			if err != nil {
				t.Fatal(err)
			}
			if n := fetchCount(t, dir); n != 1 {
				t.Fatalf("the snapshot runs the fetch pass once, after the cached one: %d", n)
			}
			for _, want := range c.want {
				if !strings.Contains(out, want) {
					t.Errorf("snapshot lacks %q:\n%s", want, out)
				}
			}
		})
	}
}

// ---- kept and held ------------------------------------------------------------------

// heldBy is a task in motion the site manager decided on age ago.
func heldBy(decision, reason string, age time.Duration) data.FactoryTask {
	x := ftask("lost-its-lead", "motion")
	x.Session, x.Elapsed, x.Limit = "done", ptr(int64(32400)), ptr(int64(2700))
	x.Held = &data.FactoryHeld{Criterion: "ended-undeclared", Decision: decision, Reason: reason}
	if age > 0 {
		x.Held.Since = fakeNow.Add(-age)
	}
	return x
}

func TestFactoryKeptAndHeldRowsAreNeverOvertime(t *testing.T) {
	over, kept, held := ftask("a", "motion"), ftask("b", "motion"), heldBy("suppressed", "", time.Hour)
	over.Elapsed, over.Limit, over.Overtime = ptr(int64(8100)), ptr(int64(5400)), true
	kept.Elapsed, kept.Limit, kept.Overtime, kept.Kept = ptr(int64(8100)), ptr(int64(5400)), true, "uncommitted changes"
	held.Overtime, held.Nearing = true, true
	m := factoryModel(false, 140, 40, factoryRepo{name: "m", st: fstatus(5, over, kept, held)})
	p, th, rows := m.th.on(m.th.Bg), m.th, m.fac.repos[0].st.Rows
	cols := m.factoryCols(p, 140, rows)
	for i, want := range []struct {
		col  color.Color
		pill bool
	}{{th.Red, true}, {th.Muted, false}, {th.Muted, false}} {
		line := m.taskRow(p, cols, rows[i], 140)[0]
		if got := strings.Contains(line, p.t(cell(elapsedText(rows[i], cols.span), cols.elapsed, true), want.col)); !got {
			t.Errorf("row %d: the elapsed cell is not %v:\n%s", i, want.col, line)
		}
		if got := strings.Contains(ansi.Strip(line), "overtime"); got != want.pill {
			t.Errorf("row %d: overtime pill is %v, want %v:\n%s", i, got, want.pill, line)
		}
	}
}

func TestFactoryKeptAndHeldPills(t *testing.T) {
	kept, supp, esc, both := ftask("a", "merged"), heldBy("suppressed", "", 0), heldBy("escalated", "", 0), heldBy("suppressed", "", 0)
	kept.Kept, both.Kept, both.Stale = "uncommitted changes", "x", true
	m := factoryModel(true, 140, 40, factoryRepo{name: "m", st: fstatus(5, kept, supp, esc, both)})
	p := m.th.on(m.th.Bg)
	for i, want := range []string{"[kept]", "[held]", "[escalated]", "[stale] [held] [kept]"} {
		if got := flagPills(p, m.fac.repos[0].st.Rows[i]); got != want {
			t.Errorf("row %d: pills %q, want %q", i, got, want)
		}
	}
}

func TestFactoryKeptAndHeldDetailLine(t *testing.T) {
	keptOnly, both := ftask("a", "merged"), heldBy("suppressed", "its  replacement\nis in review", 9*time.Hour)
	keptOnly.Kept, both.Kept = "uncommitted changes", "uncommitted changes"
	extended := heldBy("suppressed", "r", time.Hour)
	extended.Extensions = []data.FactoryExtension{{Minutes: 30, By: "site-manager", Reason: "more"}}
	for _, c := range []struct {
		name string
		t    data.FactoryTask
		want string
	}{
		{"kept", keptOnly, "kept: uncommitted changes"},
		{"suppressed", heldBy("suppressed", "closed when that lands", 9*time.Hour), "held 9h · ended-undeclared · closed when that lands"},
		{"escalated", heldBy("escalated", "closed when that lands", 25*time.Minute), "escalated 25m · ended-undeclared · closed when that lands"},
		{"both", both, "kept: uncommitted changes · held 9h · ended-undeclared · its replacement is in review"},
		{"zero since", heldBy("suppressed", "why", 0), "held · ended-undeclared · why"},
		{"empty reason", heldBy("suppressed", " ", time.Hour), "held 1h · ended-undeclared"},
		{"replaces the extension line", extended, "held 1h · ended-undeclared · r"},
	} {
		m := factoryModel(true, 140, 40, factoryRepo{name: "m", st: fstatus(5, c.t)})
		p, rows := m.th.on(m.th.Bg), m.fac.repos[0].st.Rows
		row := m.taskRow(p, m.factoryCols(p, 140, rows), rows[0], 140)
		if len(row) != 2 || strings.TrimSpace(row[1]) != c.want {
			t.Errorf("%s: detail line %q, want one line %q", c.name, row[1:], c.want)
		}
	}
}
