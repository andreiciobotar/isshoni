package tui

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

// ---- fixtures ---------------------------------------------------------------------

const (
	idQuestion = "3f9a1c2b4d5e"
	idBlocked  = "7c0b9e1d2a46"
)

// inboxRepos is the factory fixture with two open decisions: a question of alpha (three options, the
// first recommended) and a blocked one of delta-docs. Both tasks wait on the human.
func inboxRepos() []factoryRepo {
	repos := waitingRepos()
	repos[0].st.Decisions = []data.FactoryDecision{{
		ID: idQuestion, Slug: "needs-an-answer", Kind: "question", Recommend: 1, TS: fakeNow.Add(-45 * time.Minute),
		Ask:     "Should the export keep the legacy column order, or switch to the new one now? Switching breaks two downstream readers.",
		Options: []string{"Keep the legacy order", "Switch now and update both readers", "Keep legacy now, switch next release"},
	}}
	last := len(repos) - 1
	repos[last].st.Decisions = []data.FactoryDecision{{
		ID: idBlocked, Slug: "stuck-on-keys", Kind: "blocked", TS: fakeNow.Add(-90 * time.Second),
		Ask:     "git push is refused: the deploy key is read-only. Run `git push origin worktree-stuck-on-keys` by hand.",
		Options: []string{"Done, carry on", "Stop the task"},
	}}
	return repos
}

func setInbox(m *model, repos ...factoryRepo) {
	setFactory(m, repos...)
	m.view = viewInbox
	m.fixScroll()
}

func inboxModel(w, h int) *model {
	m := fakeModel(true, w, h)
	setInbox(m, inboxRepos()...)
	return m
}

// answerScript puts a factory command where data.LocateRoot finds it: it appends the directory it ran in
// and its arguments to calls, and fails with the line a refused answer prints when failFile exists.
func answerScript(t *testing.T) (calls, failFile string) {
	t.Helper()
	root, dir := t.TempDir(), t.TempDir()
	calls, failFile = filepath.Join(dir, "calls"), filepath.Join(dir, "fail")
	for path, body := range map[string]string{
		filepath.Join(root, "isshoni", "__init__.py"): "",
		filepath.Join(root, "factory", "bin", "factory"): "#!/bin/sh\necho \"$(pwd -P) $*\" >> " + calls +
			"\n[ -e " + failFile + " ] && { echo 'factory: decision " + idQuestion + " is already answered' >&2; exit 1; }\nexit 0\n",
	} {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("ISSHONI_ROOT", root)
	return calls, failFile
}

// answerModel is the inbox over repositories that exist on disk, so the script can run in them.
func answerModel(t *testing.T) (m *model, alpha, delta string) {
	t.Helper()
	repos := inboxRepos()
	for i, p := range map[int]*string{0: &alpha, len(repos) - 1: &delta} {
		dir, err := filepath.EvalSymlinks(t.TempDir())
		if err != nil {
			t.Fatal(err)
		}
		repos[i].path, *p = dir, dir
	}
	m = fakeModel(true, 100, 30)
	setInbox(m, repos...)
	return m, alpha, delta
}

func readCalls(path string) string {
	b, _ := os.ReadFile(path)
	return string(b)
}

// ---- goldens ----------------------------------------------------------------------

func TestGoldenInbox(t *testing.T) {
	shots := os.Getenv("ISSHONI_SHOT_DIR")
	for _, c := range []struct {
		name string
		w, h int
		set  func(m *model)
	}{
		{"inbox-140x40", 140, 40, func(m *model) { setInbox(m, inboxRepos()...) }},
		{"inbox-100x30", 100, 30, func(m *model) { setInbox(m, inboxRepos()...) }},
		{"inbox-80x24", 80, 24, func(m *model) { setInbox(m, inboxRepos()...) }},
		{"inbox-confirm-100x30", 100, 30, func(m *model) { setInbox(m, inboxRepos()...); send(m, "2") }},
		{"inbox-empty-80x24", 80, 24, func(m *model) { setInbox(m, fakeFactoryRepos()...) }},
	} {
		t.Run(c.name, func(t *testing.T) { goldenCase(t, shots, c.name, c.w, c.h, c.set, true) })
	}
	if shots != "" { // the new tab bar over the sessions view
		t.Run("dashboard-140x40", func(t *testing.T) { goldenCase(t, shots, "dashboard-140x40", 140, 40, func(*model) {}, false) })
	}
}

// ---- the page ---------------------------------------------------------------------

func TestInboxShowsTheCardsOverTheFactoryLines(t *testing.T) {
	out := plain(inboxModel(140, 40))
	for _, want := range []string{
		"> ? needs-an-answer", "  ▲ stuck-on-keys", "REPO", "TASK", "AGE", "│",
		"needs-an-answer · question · asked 45m ago", "Should the export keep the legacy column order",
		"1  Keep the legacy order  (recommended)", "2  Switch now and update both readers",
		"worktree cap", "delta-docs", "SLUG",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("inbox lacks %q:\n%s", want, out)
		}
	}
	if strings.Index(out, "TASK") > strings.Index(out, "worktree cap") {
		t.Error("the panes come first")
	}
}

func TestInboxMessagePaneFollowsTheCursor(t *testing.T) {
	calls, _ := answerScript(t)
	m, _, delta := answerModel(t)
	send(m, "j")
	out := plain(m)
	for _, want := range []string{"stuck-on-keys · blocked · asked 1m ago", "git push is refused", "1  Done, carry on", "2  Stop the task"} {
		if !strings.Contains(out, want) {
			t.Errorf("after j the message lacks %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "Should the export keep") {
		t.Errorf("after j the first decision's ask is gone:\n%s", out)
	}
	send(m, "1")
	msgs := upd(m, press("y"))
	if got := only[answerMsg](msgs); len(got) != 1 || got[0].err != nil {
		t.Fatalf("%v", msgs)
	}
	if want := fmt.Sprintf("%s decision answer %s 1\n", delta, idBlocked); readCalls(calls) != want {
		t.Errorf("script ran as %q, want %q", readCalls(calls), want)
	}
}

func TestInboxBandDoesNotRepeatATaskWithACard(t *testing.T) {
	m := inboxModel(140, 40)
	if out := plain(m); !strings.Contains(out, "▲ Needs you  5") || strings.Contains(out, "answer question") {
		t.Errorf("seven wait, two have cards:\n%s", out)
	}
	if got := len(m.waiting()); got != 7 {
		t.Fatalf("the badge counts every waiting task: %d", got)
	}
	m.view = viewFactory
	if out := plain(m); !strings.Contains(out, "▲ Needs you  7") || !strings.Contains(out, "answer question") {
		t.Errorf("the Factory view stays as it was:\n%s", out)
	}
	only := inboxRepos()[0]
	only.st = fstatus(5, only.st.Rows[:0]...)
	task := ftask("needs-an-answer", "blocked")
	task.WaitsOn = "human"
	only.st.Rows, only.st.Blocked, only.st.InMotion = []data.FactoryTask{task}, 1, 1
	only.st.Decisions = inboxRepos()[0].st.Decisions
	setInbox(m, only)
	if out := plain(m); strings.Contains(out, "Needs you") || strings.Contains(out, "nothing waits on you") {
		t.Errorf("every waiting task has its card: no band, no 'nothing waits':\n%s", out)
	}
}

func TestInboxWithoutADecision(t *testing.T) {
	m := fakeModel(true, 100, 30)
	setInbox(m, fakeFactoryRepos()...)
	if out := plain(m); !strings.Contains(out, "Nothing to decide.") || !strings.Contains(out, "worktree cap") {
		t.Errorf("no decision:\n%s", out)
	}
	m.fac.doneAt = time.Time{}
	if out := plain(m); strings.Contains(out, "Nothing to decide.") {
		t.Errorf("before the factory answered nothing is known:\n%s", out)
	}
}

func TestInboxTabBadgeAndSubheader(t *testing.T) {
	out := plain(inboxModel(140, 40))
	lines := strings.Split(out, "\n")
	if !strings.Contains(lines[0], "[1 Inbox ▲7]") || strings.Contains(lines[0], "Factory ▲") {
		t.Errorf("the badge is on the Inbox tab: %q", lines[0])
	}
	if !strings.HasPrefix(strings.TrimSpace(lines[1]), "Inbox") {
		t.Errorf("subheader %q", lines[1])
	}
}

// Everything the Factory view does when shown holds for the Inbox.
func TestInboxIsAFactoryPage(t *testing.T) {
	m, dir, _ := factoryStubModel(t)
	if got := only[factoryMsg](upd(m, press("1"))); len(got) != 1 || factoryCalls(t, dir) != 2 {
		t.Fatalf("entering the Inbox asks the factory like entering the Factory view: %d results, %d calls", len(got), factoryCalls(t, dir))
	}
	m, _, _ = answerModel(t)
	send(m, "c")
	if !m.fac.editing {
		t.Fatal("c edits the cap on the Inbox")
	}
	m = inboxModel(80, 24)
	send(m, "j", "j", "j", "j", "j", "j")
	if m.inbox.tbl.Cursor() != 1 || m.inbox.top == 0 {
		t.Fatalf("j walks the cards, then scrolls the page: cursor %d, top %d", m.inbox.tbl.Cursor(), m.inbox.top)
	}
	send(m, "k", "k", "k", "k", "k", "k", "k", "k", "k", "k", "k", "k")
	if m.inbox.tbl.Cursor() != 0 || m.inbox.top != 0 {
		t.Fatalf("k comes back: cursor %d, top %d", m.inbox.tbl.Cursor(), m.inbox.top)
	}
	send(m, "G")
	if m.inbox.top == 0 || m.fac.top != 0 {
		t.Fatalf("G scrolls the Inbox, and only the Inbox: top %d, factory top %d", m.inbox.top, m.fac.top)
	}
}

// ---- pick and confirm -------------------------------------------------------------

func TestInboxDigitOpensTheConfirmOnAccept(t *testing.T) {
	m := inboxModel(100, 30)
	send(m, "2")
	if m.view != viewInbox || m.inbox.pick != 2 || m.inbox.reject || m.inbox.id != idQuestion {
		t.Fatalf("view %v, inbox %+v", m.view, m.inbox)
	}
	if out := plain(m); !strings.Contains(out, "[Accept]") || !strings.Contains(out, " Reject ") || !strings.Contains(out, "[2] Switch now") {
		t.Errorf("the pick and Accept show:\n%s", out)
	}
	send(m, "n", "j", "1")
	if m.inbox.pick != 1 || m.inbox.id != idBlocked {
		t.Fatalf("the second card picks for itself: %+v", m.inbox)
	}
	send(m, "n", "4") // the card has two options: 4 is the Factory tab
	if m.view != viewFactory {
		t.Fatalf("a digit beyond the options switches the tab: %v", m.view)
	}
}

func TestInboxAnswerRunsTheScriptInTheRepository(t *testing.T) {
	for _, c := range []struct {
		keys []string
		repo func(a, d string) string
		id   string
		n    int
	}{
		{[]string{"2", "enter"}, func(a, _ string) string { return a }, idQuestion, 2},
		{[]string{"3", "y"}, func(a, _ string) string { return a }, idQuestion, 3},
		{[]string{"j", "1", "y"}, func(_, d string) string { return d }, idBlocked, 1},
		{[]string{"2", "right", "left", "enter"}, func(a, _ string) string { return a }, idQuestion, 2},
	} {
		calls, _ := answerScript(t)
		m, alpha, delta := answerModel(t)
		send(m, c.keys[:len(c.keys)-1]...)
		msgs := upd(m, press(c.keys[len(c.keys)-1]))
		if !m.inbox.sending || !strings.Contains(plain(m), "sending…") {
			t.Fatalf("%v: the card shows sending… until the result", c.keys)
		}
		got := only[answerMsg](msgs)
		if len(got) != 1 || got[0].err != nil {
			t.Fatalf("%v: %v", c.keys, msgs)
		}
		if want := fmt.Sprintf("%s decision answer %s %d\n", c.repo(alpha, delta), c.id, c.n); readCalls(calls) != want {
			t.Fatalf("%v: script ran as %q, want %q", c.keys, readCalls(calls), want)
		}
		m.Update(got[0])
		if m.inbox.sending || m.inbox.pick != 0 || m.toast.err || !strings.HasPrefix(m.toast.text, "answered ") || !strings.HasSuffix(m.toast.text, fmt.Sprintf(": %d", c.n)) {
			t.Fatalf("%v: after the answer: inbox %+v, toast %+v", c.keys, m.inbox, m.toast)
		}
	}
}

func TestInboxRejectAnswersNothing(t *testing.T) {
	for _, keys := range [][]string{{"n"}, {"esc"}, {"right", "enter"}, {"tab", "enter"}, {"l", "enter"}} {
		calls, _ := answerScript(t)
		m, _, _ := answerModel(t)
		send(m, "2")
		for _, k := range keys {
			if cmd := upd(m, press(k)); len(cmd) != 0 {
				t.Fatalf("%v: %v ran something", keys, k)
			}
		}
		if m.inbox.pick != 0 || m.inbox.sending || readCalls(calls) != "" {
			t.Fatalf("%v: pick %d, calls %q", keys, m.inbox.pick, readCalls(calls))
		}
	}
}

func TestInboxConfirmIgnoresEveryOtherKey(t *testing.T) {
	m, _, _ := answerModel(t)
	send(m, "2")
	state := func() string {
		return fmt.Sprint(m.inbox.pick, m.inbox.id, m.inbox.reject, m.inbox.sending, m.inbox.tbl.Cursor(), m.inbox.top)
	}
	before := state()
	for _, k := range []string{"x", "j", "k", "q", "?", "c", "d", "r", "4", "7", "pgdown", "G", "/"} { // 4: the card has three options
		if cmd := upd(m, press(k)); len(cmd) != 0 {
			t.Fatalf("%q ran something", k)
		}
		if state() != before || m.view != viewInbox || m.help || m.fac.editing {
			t.Fatalf("%q changed the confirm: %s view %v", k, state(), m.view)
		}
	}
	send(m, "3")
	if m.inbox.pick != 3 || m.inbox.reject {
		t.Fatalf("a digit valid for the card changes the pick: %+v", m.inbox)
	}
	send(m, "tab")
	send(m, "1")
	if m.inbox.pick != 1 || m.inbox.reject {
		t.Fatalf("a new pick marks Accept again: %+v", m.inbox)
	}
	_, cmd := m.Update(press("ctrl+c"))
	if cmd == nil {
		t.Fatal("ctrl+c still quits")
	}
	if _, ok := cmd().(tea.QuitMsg); !ok {
		t.Fatal("ctrl+c is a quit")
	}
}

func TestInboxReadOnlyRefusesToPick(t *testing.T) {
	calls, _ := answerScript(t)
	m, _, _ := answerModel(t)
	m.opts.ReadOnly = true
	send(m, "2")
	if m.inbox.pick != 0 || m.toast.text != "read-only: this key may not answer a decision" || !m.toast.err {
		t.Fatalf("pick %d, toast %+v", m.inbox.pick, m.toast)
	}
	send(m, "enter", "y")
	if readCalls(calls) != "" {
		t.Fatal("a read-only dashboard answered")
	}
}

func TestInboxFailedAnswerShowsTheScriptsLineAndUnpicks(t *testing.T) {
	calls, failFile := answerScript(t)
	if err := os.WriteFile(failFile, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	m, _, _ := answerModel(t)
	send(m, "1")
	got := only[answerMsg](upd(m, press("y")))
	if len(got) != 1 || got[0].err == nil {
		t.Fatalf("the script failed: %v", got)
	}
	m.Update(got[0])
	want := "decision " + idQuestion + " is already answered"
	if m.toast.text != want || !m.toast.err || m.inbox.pick != 0 || m.inbox.sending {
		t.Fatalf("toast %+v, inbox %+v", m.toast, m.inbox)
	}
	if !strings.Contains(plain(m), "1  Keep the legacy order") || strings.Contains(plain(m), "[Accept]") {
		t.Error("the card is back to unpicked")
	}
	if readCalls(calls) == "" {
		t.Fatal("the script did not run")
	}
}

func TestInboxPickIsDroppedWhenItsCardIsGone(t *testing.T) {
	m := inboxModel(100, 30)
	send(m, "2")
	repos := inboxRepos()
	repos[0].st.Decisions = nil
	m.applyFactory(factoryMsg{repos: repos})
	if m.inbox.pick != 0 {
		t.Fatalf("the status says the card is gone: %+v", m.inbox)
	}
}

func TestInboxFooterAndHelpNameThePickKeys(t *testing.T) {
	m := inboxModel(120, 30)
	if foot := plain(m); !strings.Contains(foot, "1–3 pick an option") {
		t.Errorf("footer:\n%s", foot)
	}
	send(m, "2")
	foot := strings.Split(plain(m), "\n")[29]
	for _, want := range []string{"enter do the marked one", "←→/hl mark accept · reject", "y n accept · reject"} {
		if !strings.Contains(foot, want) {
			t.Errorf("confirm footer lacks %q: %q", want, foot)
		}
	}
	m = inboxModel(120, 40)
	m.help = true
	if out := plain(m); !strings.Contains(out, "1-4") || !strings.Contains(out, "pick an option") || !strings.Contains(out, "do the marked one") {
		t.Errorf("help:\n%s", out)
	}
}

// ---- snapshots --------------------------------------------------------------------

func TestSnapshotInboxAndDashboard(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("STUBDIR", dir)
	t.Setenv("HOME", t.TempDir())
	repo := filepath.Join(t.TempDir(), "proj")
	if err := os.MkdirAll(filepath.Join(repo, ".claude", "worktrees"), 0o755); err != nil {
		t.Fatal(err)
	}
	writeStub(t, dir, "sessions.json", fmt.Sprintf(`{"sessions":[{"id":"s1","project":"proj","repo":"proj","repo_path":%q}],"malformed":0}`, repo))
	writeStub(t, dir, "status.json", `{"rows":[],"cap":5,"decisions":[{"id":"3f9a1c2b4d5e","slug":"demo-task","kind":"question","ask":"Ship the importer without retry, or add it now?","options":["Ship without","Add retry now"],"recommend":2,"ts":"2026-11-14T19:00:00Z"}]}`)
	py, _ := filepath.Abs("../data/testdata/fakepy.sh")
	root, _ := filepath.Abs("../..")
	r := &data.Runner{Root: root, Python: py, ProjectsDir: dir, Timeout: 5 * time.Second}
	snap := func(view string) string {
		out, err := Snapshot(SnapshotOptions{Options: Options{Runner: r, NoColor: true, Range: "all", Now: func() time.Time { return goldenNow }}, View: view, W: 140, H: 30})
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	if out := snap("inbox"); !strings.Contains(out, "[1 Inbox]") || !strings.Contains(out, "> ? demo-task") || !strings.Contains(out, "demo-task · question · asked") || !strings.Contains(out, "2  Add retry now  (recommended)") {
		t.Errorf("inbox snapshot:\n%s", out)
	}
	if out := snap("dashboard"); !strings.Contains(out, "[2 Sessions]") || strings.Contains(out, "demo-task") {
		t.Errorf("--snapshot dashboard is the sessions view:\n%s", out)
	}
}

func TestInboxCardsAreOldestFirstAndAnUnknownAgeLast(t *testing.T) {
	d := func(id string, ago time.Duration) data.FactoryDecision {
		x := data.FactoryDecision{ID: id, Slug: id, Kind: "question", Ask: "a", Options: []string{"x", "y"}}
		if ago > 0 {
			x.TS = fakeNow.Add(-ago)
		}
		return x
	}
	one, two := fakeFactoryRepos()[0], fakeFactoryRepos()[1]
	one.st.Decisions = []data.FactoryDecision{d("noage", 0), d("new", time.Minute)}
	two.st.Decisions = []data.FactoryDecision{d("old", time.Hour)}
	m := fakeModel(true, 100, 30)
	setInbox(m, one, two)
	var got []string
	for _, c := range m.cards() {
		got = append(got, c.d.ID)
	}
	if strings.Join(got, " ") != "old new noage" {
		t.Fatalf("order %v", got)
	}
}

// reposWithAnExtra is inboxRepos with a second, newer decision in alpha: by age it falls between the two
// others, so only the grouping puts it before delta-docs' card.
func reposWithAnExtra() []factoryRepo {
	repos := inboxRepos()
	repos[0].st.Decisions = append(repos[0].st.Decisions, data.FactoryDecision{
		ID: "aa11", Slug: "second-of-alpha", Kind: "question", TS: fakeNow.Add(-10 * time.Second),
		Ask: "Which?", Options: []string{"This", "That"},
	})
	return repos
}

func TestInboxGroupsCardsByRepository(t *testing.T) {
	m := fakeModel(true, 140, 40)
	setInbox(m, reposWithAnExtra()...)
	var order []string
	for _, c := range m.cards() {
		order = append(order, c.d.Slug)
	}
	if got := strings.Join(order, " "); got != "needs-an-answer second-of-alpha stuck-on-keys" {
		t.Fatalf("cards %s", got)
	}
	var seen []string                                 // the headings and cards in the order shown
	for _, l := range strings.Split(plain(m), "\n") { // the list is what is left of the rule; the lines below have none
		if !strings.Contains(l, "│") {
			continue
		}
		f := slices.DeleteFunc(strings.Fields(strings.SplitN(l, "│", 2)[0]), func(s string) bool { return s == ">" })
		if len(f) > 0 && (f[0] == "alpha" || f[0] == "delta-docs") { // the repository, on its first row only
			seen, f = append(seen, f[0]), f[1:]
		}
		if len(f) > 1 && (f[0] == "?" || f[0] == "▲") {
			seen = append(seen, f[1])
		}
	}
	if got := strings.Join(seen, " "); got != "alpha needs-an-answer second-of-alpha delta-docs stuck-on-keys" {
		t.Errorf("the repository on the first row of its group: %s", got)
	}
	send(m, "j", "j")
	if c, _ := m.cursorCard(); c.d.Slug != "stuck-on-keys" {
		t.Errorf("j walks the displayed order: %s", c.d.Slug)
	}
}

func TestInboxFooterNamesTheCardsOptionRange(t *testing.T) {
	m := inboxModel(140, 40)
	if out := plain(m); !strings.Contains(out, "1–3 pick an option") {
		t.Errorf("three options:\n%s", out)
	}
	send(m, "j")
	if out := plain(m); !strings.Contains(out, "1–2 pick an option") {
		t.Errorf("two options:\n%s", out)
	}
}

func TestEscFromAnotherTopLevelViewGoesToTheInbox(t *testing.T) {
	m := inboxModel(100, 30)
	for _, v := range []viewID{viewAgents, viewFactory, viewKaizen} {
		m.switchView(v)
		send(m, "esc")
		if m.view != viewInbox {
			t.Errorf("esc from view %v -> %v", v, m.view)
		}
	}
	m.switchView(viewDashboard)
	m.zoom = true
	send(m, "esc")
	if m.view != viewDashboard || m.zoom {
		t.Errorf("esc leaves a zoomed session for Sessions: view %v, zoom %v", m.view, m.zoom)
	}
}

// withOlderInAlpha puts a decision older than every other at the head of alpha, so every card of alpha
// moves one place down.
func withOlderInAlpha(repos []factoryRepo) []factoryRepo {
	repos[0].st.Decisions = append([]data.FactoryDecision{{
		ID: "oo00", Slug: "older-of-alpha", Kind: "question", TS: fakeNow.Add(-3 * time.Hour),
		Ask: "Which?", Options: []string{"This", "That"},
	}}, repos[0].st.Decisions...)
	return repos
}

func TestInboxCursorFollowsItsDecisionThroughARefresh(t *testing.T) {
	m := fakeModel(true, 100, 30)
	setInbox(m, reposWithAnExtra()...)
	send(m, "j") // second-of-alpha, the second card
	setInbox(m, withOlderInAlpha(reposWithAnExtra())...)
	if c, _ := m.cursorCard(); c.d.ID != "aa11" || m.inbox.tbl.Cursor() != 2 {
		t.Errorf("the cursor stays on its decision at its new place: %s at %d", c.d.ID, m.inbox.tbl.Cursor())
	}
	repos := reposWithAnExtra()
	repos[0].st.Decisions = nil // its decision goes: the nearest card
	setInbox(m, repos...)
	if c, _ := m.cursorCard(); c.d.ID != idBlocked {
		t.Errorf("the nearest card: %s", c.d.ID)
	}
}

func TestInboxPendingPickFollowsItsDecisionAndClosesWhenItIsGone(t *testing.T) {
	m := inboxModel(100, 30)
	send(m, "2")
	setInbox(m, withOlderInAlpha(inboxRepos())...)
	if c, _ := m.cursorCard(); c.d.ID != idQuestion || m.inbox.tbl.Cursor() != 1 || m.inbox.pick != 2 {
		t.Fatalf("the pick stays on its decision at its new place: %s, %+v", c.d.ID, m.inbox)
	}
	repos := withOlderInAlpha(inboxRepos())
	repos[0].st.Decisions = repos[0].st.Decisions[:1] // another decision takes the place the picked one had
	setInbox(m, repos...)
	if m.inbox.pick != 0 || m.inbox.sending {
		t.Fatalf("the pick closes: %+v", m.inbox)
	}
	if c, _ := m.cursorCard(); c.d.ID != idBlocked {
		t.Errorf("cursor %s", c.d.ID)
	}
}

func TestInboxLateAnswerKeepsThePickOfAnotherCard(t *testing.T) {
	m := inboxModel(100, 30)
	send(m, "j", "1") // a pick on the blocked card
	late := answerMsg{card: inboxCard{d: data.FactoryDecision{ID: idQuestion, Slug: "needs-an-answer"}}, n: 2}
	m.Update(late)
	if m.inbox.pick != 1 || m.inbox.id != idBlocked {
		t.Errorf("the answer of another card leaves the pick: %+v", m.inbox)
	}
}

func TestInboxElevenDecisionsScrollTheListNotThePage(t *testing.T) {
	repos := inboxRepos()
	repos[0].st.Decisions = nil
	repos[len(repos)-1].st.Decisions = nil
	for i := 1; i <= 11; i++ {
		repos[0].st.Decisions = append(repos[0].st.Decisions, data.FactoryDecision{
			ID: fmt.Sprintf("id%02d", i), Slug: fmt.Sprintf("task-%02d", i), Kind: "question", TS: fakeNow.Add(time.Duration(i-12) * time.Minute),
			Ask: fmt.Sprintf("Ask number %d?", i), Options: []string{fmt.Sprintf("First of %d", i), "Second"},
		})
	}
	m := fakeModel(true, 140, 40)
	setInbox(m, repos...)
	listed := func() (n int) {
		for _, l := range strings.Split(plain(m), "\n") {
			if left, _, ok := strings.Cut(l, "│"); ok && strings.Contains(left, "task-") {
				n++
			}
		}
		return n
	}
	if out := plain(m); listed() != 8 || !strings.Contains(out, "Ask number 1?") || !strings.Contains(out, "1  First of 1") {
		t.Fatalf("the list shows eight rows beside the first message:\n%s", out)
	}
	send(m, "f", "d", "u", "b") // the table's own paging keys are not bound here
	if m.inbox.tbl.Cursor() != 0 {
		t.Fatalf("the table's paging keys moved the cursor to %d", m.inbox.tbl.Cursor())
	}
	for range 10 {
		send(m, "j")
	}
	out := plain(m)
	if m.inbox.tbl.Cursor() != 10 || m.inbox.top != 0 || listed() != 8 {
		t.Fatalf("cursor %d, page top %d, %d rows listed:\n%s", m.inbox.tbl.Cursor(), m.inbox.top, listed(), out)
	}
	for _, want := range []string{"> ? task-11", "task-11 · question", "Ask number 11?", "1  First of 11", "worktree cap"} {
		if !strings.Contains(out, want) {
			t.Errorf("at the last row the page lacks %q:\n%s", want, out)
		}
	}
	send(m, "j")
	if m.inbox.top == 0 {
		t.Error("past the last row j scrolls the page")
	}
}

func TestInboxListShowsTheCursorRowAfterARefreshMovesItFar(t *testing.T) {
	build := func(newest string) []factoryRepo {
		repos := inboxRepos()
		repos[0].st.Decisions = nil
		repos[len(repos)-1].st.Decisions = nil
		for i := 1; i <= 11; i++ {
			id := fmt.Sprintf("id%02d", i)
			age := time.Duration(12-i) * time.Minute
			if id == newest {
				age = 0 // the decision that becomes the newest, the last row
			}
			repos[0].st.Decisions = append(repos[0].st.Decisions, data.FactoryDecision{
				ID: id, Slug: "task-" + id[2:], Kind: "question", TS: fakeNow.Add(-age),
				Ask: "Ask of " + id + "?", Options: []string{"First", "Second"},
			})
		}
		return repos
	}
	for _, tc := range []struct {
		name         string
		from, to     int
		before, then []factoryRepo
	}{
		{"row 0 to row 10", 0, 10, build(""), build("id01")},
		{"row 10 to row 0", 10, 0, build("id01"), build("")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := fakeModel(true, 140, 40)
			setInbox(m, tc.before...)
			for range tc.from {
				send(m, "j")
			}
			c, _ := m.cursorCard()
			setInbox(m, tc.then...)
			if got, _ := m.cursorCard(); got.d.ID != c.d.ID || m.inbox.tbl.Cursor() != tc.to {
				t.Fatalf("the cursor is on %s at %d, want %s at %d", got.d.ID, m.inbox.tbl.Cursor(), c.d.ID, tc.to)
			}
			out := plain(m)
			if !strings.Contains(out, "> ? task-"+c.d.ID[2:]) || !strings.Contains(out, "Ask of "+c.d.ID+"?") {
				t.Errorf("the list lacks the selected row of %s:\n%s", c.d.ID, out)
			}
		})
	}
}
