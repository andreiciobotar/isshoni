package tui

import (
	"fmt"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

// splitModel is answerModel with noticeRepos' split card on alpha (the cursor's second card) and the standing
// answers.
func splitModel(t *testing.T) (m *model, alpha string) {
	t.Helper()
	m, alpha, _ = answerModel(t)
	st := m.fac.repos[0].st
	st.Decisions = append(st.Decisions, data.FactoryDecision{
		ID: idSplit, Slug: "demo-task", Kind: "split", Always: 1, TS: fakeNow.Add(-20 * time.Minute),
		Ask: "The importer's retry is separable.", Options: []string{"Start demo-retry", "Do not start it"},
	})
	st.Standing = []data.FactoryStanding{{Kind: "split", Label: "start it", On: true, Uses: 4}}
	m.fixScroll()
	return m, alpha
}

// confirmLine is the line of the page with the Accept button.
func confirmLine(m *model) string {
	for _, l := range strings.Split(plain(m), "\n") {
		if strings.Contains(l, "Accept") {
			_, r, _ := strings.Cut(l, "│")
			return strings.TrimSpace(r)
		}
	}
	return ""
}

func TestInboxConfirmOffersAlwaysOnlyForTheOptionThatMayStand(t *testing.T) {
	m, _ := splitModel(t)
	send(m, "2")
	if got := confirmLine(m); got != "[Accept]   Reject" {
		t.Fatalf("a question has no Always: %q", got)
	}
	send(m, "n", "j", "2")
	if got := confirmLine(m); got != "[Accept]   Reject" {
		t.Fatalf("a split's option 2 has no Always: %q", got)
	}
	send(m, "1")
	if confirmLine(m) != "[Accept]   Always    Reject" {
		t.Fatalf("option 1 of a split offers Always: %q", confirmLine(m))
	}
	send(m, "l")
	if m.inbox.mark != 1 || confirmLine(m) != "Accept   [Always]   Reject" {
		t.Fatalf("l marks Always: %q", confirmLine(m))
	}
	send(m, "tab", "tab")
	if m.inbox.mark != 0 {
		t.Fatalf("tab wraps past Reject to Accept: %d", m.inbox.mark)
	}
	send(m, "h")
	if m.inbox.mark != 2 {
		t.Fatalf("h from Accept goes to Reject: %d", m.inbox.mark)
	}
	send(m, "enter")
	if m.inbox.pick != "" {
		t.Fatal("enter on Reject unpicks")
	}
}

func TestInboxAlwaysAnswersWithTheFlag(t *testing.T) {
	for _, keys := range [][]string{{"j", "1", "a"}, {"j", "1", "l", "enter"}} {
		calls, _ := answerScript(t)
		m, alpha := splitModel(t)
		send(m, keys[:len(keys)-1]...)
		got := only[answerMsg](upd(m, press(keys[len(keys)-1])))
		if len(got) != 1 || got[0].err != nil || !got[0].always {
			t.Fatalf("%v: %+v", keys, got)
		}
		if want := fmt.Sprintf("%s decision answer %s 1 --always\n", alpha, idSplit); readCalls(calls) != want {
			t.Fatalf("%v: script ran as %q, want %q", keys, readCalls(calls), want)
		}
		m.Update(got[0])
		if m.toast.err || m.toast.text != "answered demo-task: 1 · always start it from now" {
			t.Fatalf("%v: toast %+v", keys, m.toast)
		}
	}
	calls, _ := answerScript(t)
	m, _ := splitModel(t)
	send(m, "j", "1", "y")
	if got := only[answerMsg](upd(m, press("y"))); len(got) != 0 {
		t.Fatal("a second key while sending does nothing")
	}
	if readCalls(calls) != "" {
		t.Fatal("y sends only when the command runs")
	}
}

func TestInboxAIgnoredOnAQuestionConfirm(t *testing.T) {
	calls, _ := answerScript(t)
	m, _ := splitModel(t)
	send(m, "2")
	if got := upd(m, press("a")); len(got) != 0 || m.inbox.sending || readCalls(calls) != "" {
		t.Fatalf("a answers nothing on a pick that may not stand: %v", got)
	}
}

func TestInboxAlwaysLineShowsKindsThatAreOn(t *testing.T) {
	m, _ := splitModel(t)
	if out := plain(m); !strings.Contains(out, "Always   split → start it · 4 times") {
		t.Fatalf("the line under the sub-header:\n%s", out)
	}
	list := m.fac.repos[0].st.Standing
	list[0].Uses = 1
	if out := plain(m); !strings.Contains(out, "Always   split → start it · 1 time") {
		t.Fatalf("one use reads 1 time:\n%s", out)
	}
	list[0].On = false // switched off by hand: not listed
	if out := plain(m); strings.Contains(out, "Always ") {
		t.Fatalf("no kind on, no line:\n%s", out)
	}
}

func TestInboxAlwaysPickSwitchesOneOff(t *testing.T) {
	calls, _ := answerScript(t)
	m, alpha := splitModel(t)
	send(m, "A")
	if !m.inbox.always || !strings.Contains(plain(m), "1 split → start it · 4 times") {
		t.Fatalf("A puts the line in pick mode:\n%s", plain(m))
	}
	send(m, "x", "2", "j", "q", "tab")
	if !m.inbox.always || m.inbox.tbl.Cursor() != 0 || m.view != viewInbox {
		t.Fatalf("other keys and a digit past the kinds are ignored: %+v", m.inbox)
	}
	send(m, "esc")
	if m.inbox.always {
		t.Fatal("esc leaves")
	}
	send(m, "A", "A")
	if m.inbox.always {
		t.Fatal("A leaves")
	}
	send(m, "A")
	_, cmd := m.Update(press("ctrl+c"))
	if cmd == nil {
		t.Fatal("ctrl+c quits in pick mode")
	}
	if _, ok := cmd().(tea.QuitMsg); !ok {
		t.Fatal("ctrl+c is a quit")
	}
	got := only[standingMsg](upd(m, press("1")))
	if len(got) != 1 || got[0].err != nil || got[0].kind != "split" || m.inbox.always {
		t.Fatalf("a digit runs the script and leaves the mode: %+v, %+v", got, m.inbox)
	}
	if want := fmt.Sprintf("%s decision always split off\n", alpha); readCalls(calls) != want {
		t.Fatalf("script ran as %q, want %q", readCalls(calls), want)
	}
	m.Update(got[0])
	if m.toast.err || m.toast.text != "split: asks again from now" {
		t.Fatalf("toast %+v", m.toast)
	}
}

func TestInboxAlwaysPickFailureShowsTheScriptsLine(t *testing.T) {
	m, _ := splitModel(t)
	m.Update(standingMsg{"split", fmt.Errorf("split is not a kind\nsecond")})
	if !m.toast.err || m.toast.text != "split is not a kind" {
		t.Fatalf("toast %+v", m.toast)
	}
}

func TestInboxReadOnlyRefusesToSwitchAnAlwaysOff(t *testing.T) {
	calls, _ := answerScript(t)
	m, _ := splitModel(t)
	m.opts.ReadOnly = true
	if got := upd(m, press("A")); len(got) != 0 || m.inbox.always || !m.toast.err || !strings.Contains(m.toast.text, "read-only") {
		t.Fatalf("A in a read-only dashboard: %v, %+v, %+v", got, m.inbox, m.toast)
	}
	send(m, "j", "1")
	if m.inbox.pick != "" || readCalls(calls) != "" {
		t.Fatalf("a read-only dashboard opens no confirm, so Always cannot run: %+v", m.inbox)
	}
}

func TestInboxFootersNameAlwaysKeys(t *testing.T) {
	m, _ := splitModel(t)
	m.w = 120
	send(m, "j") // the split card
	lines := strings.Split(plain(m), "\n")
	if last := lines[len(lines)-1]; strings.Contains(last, "A always off") || !strings.Contains(last, "H all history") || !strings.Contains(last, "tab next view") {
		t.Fatalf("short of room, A is the hint that goes: %q", last)
	}
	m.w = 140 // the footer has room for A beside the base's keys
	if out := plain(m); !strings.Contains(strings.Split(out, "\n")[len(strings.Split(out, "\n"))-1], "A always off") {
		t.Fatalf("the footer names A while the line shows:\n%s", out)
	}
	send(m, "1")
	if out := plain(m); !strings.Contains(out, "a always") {
		t.Fatalf("the confirm footer names a:\n%s", out)
	}
	send(m, "n", "2")
	if out := plain(m); strings.Contains(out, "a always") {
		t.Fatalf("not offered, not named:\n%s", out)
	}
	send(m, "n", "A")
	if out := plain(m); !strings.Contains(out, "1 switch it off") {
		t.Fatalf("the pick mode footer:\n%s", out)
	}
	m.w, m.h = 100, 60
	send(m, "esc", "?")
	if out := plain(m); !strings.Contains(out, "always off") || !strings.Contains(out, "│ a    ") {
		t.Fatalf("the help names a and A:\n%s", out)
	}
}

func TestInboxPickedCardsAreNotCountedInTheBadge(t *testing.T) {
	m := inboxModel(100, 30)
	want := " Inbox ▲6 "
	repos := inboxRepos()
	repos[0].st.Decisions = append(repos[0].st.Decisions, data.FactoryDecision{ID: idSplit, Slug: "needs-an-answer", Kind: "split", Always: 1, Picked: 1,
		Options: []string{"a", "b"}, TS: fakeNow})
	setInbox(m, repos...)
	if !strings.Contains(plain(m), "▲6") {
		t.Fatalf("a task with a picked and an open card still counts:\n%s", plain(m))
	}
	repos[0].st.Decisions = repos[0].st.Decisions[1:] // only the picked card remains on that task
	setInbox(m, repos...)
	if out := plain(m); !strings.Contains(out, "▲6") {
		t.Fatalf("a blocked task whose only card is picked still counts (%s):\n%s", want, out)
	}
	if out := plain(m); !strings.Contains(out, "answer question") {
		t.Fatalf("a blocked task whose only card is picked is still in Needs you:\n%s", out)
	}
}

func TestInboxServerPickedOptionIsMarkedWithoutAConfirm(t *testing.T) {
	m, _ := splitModel(t)
	m.fac.repos[0].st.Decisions[1].Picked = 1
	send(m, "j")
	out := plain(m)
	if !strings.Contains(out, "[1] Start demo-retry") || strings.Contains(out, "Accept") {
		t.Fatalf("a picked decision marks its option and offers no confirm:\n%s", out)
	}
}

func TestInboxCardDrawsTheNoteUnderTheAskInFaint(t *testing.T) {
	m, _ := splitModel(t)
	m.fac.repos[0].st.Decisions[1].Note = "starts by itself when it can: at the cap of 6 tasks in motion: a, b (always: start it)"
	send(m, "j")
	lines := strings.Split(plain(m), "\n")
	for i, l := range lines {
		if strings.Contains(l, "The importer's retry is separable.") {
			if !strings.Contains(lines[i+1], "starts by itself when it can") || !strings.Contains(strings.Join(lines[i+2:i+5], "\n"), "1  Start demo-retry") {
				t.Fatalf("the note sits between the ask and the options:\n%s", strings.Join(lines, "\n"))
			}
			return
		}
	}
	t.Fatalf("no split card:\n%s", strings.Join(lines, "\n"))
}

func TestInboxSplitAndOverrunCardsTakeNoLeaveKey(t *testing.T) {
	const idOverrun = "0ver0ver0ver"
	m, _ := splitModel(t)
	st := m.fac.repos[0].st
	st.Decisions = append(st.Decisions, data.FactoryDecision{
		ID: idOverrun, Slug: "slow-task", Kind: "overrun", Recommend: 1, TS: fakeNow.Add(-5 * time.Minute),
		Ask: "Over its limit.", Options: []string{"Continue", "Stop it"},
	})
	m.fixScroll()
	for _, id := range []string{idSplit, idOverrun} {
		for i := 0; ; i++ {
			if c, _ := m.cursorCard(); c.d.ID == id {
				break
			} else if i == 10 {
				t.Fatalf("no card %s", id)
			}
			send(m, "j")
		}
		for _, k := range []string{"L", "S"} {
			if cmd := upd(m, press(k)); len(cmd) != 0 || m.inbox.pick != "" {
				t.Fatalf("%s on card %s picked %q", k, id, m.inbox.pick)
			}
		}
		if out := plain(m); strings.Contains(out, "L S") || strings.Contains(out, "leave it to") {
			t.Errorf("card %s shows a leave hint:\n%s", id, out)
		}
	}
}
