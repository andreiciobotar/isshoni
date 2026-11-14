package tui

import (
	"errors"
	"io"
	"os/exec"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
)

type nopExec struct{}

func (nopExec) Run() error          { return nil }
func (nopExec) SetStdin(io.Reader)  {}
func (nopExec) SetStdout(io.Writer) {}
func (nopExec) SetStderr(io.Writer) {}

// attachModel is a dashboard whose Attach records the names it was asked for.
func attachModel(t *testing.T, settings string) (*model, *[]string) {
	t.Helper()
	settingsFile(t, settings)
	var asked []string
	m := fakeModel(true, 100, 30)
	m.opts.Attach = func(name string) tea.ExecCommand {
		asked = append(asked, name)
		return nopExec{}
	}
	return m, &asked
}

func TestDOpensTheOnlyDispatcherAtOnce(t *testing.T) {
	m, asked := attachModel(t, `{"dispatchers": ["dispatcher-isshoni"]}`)
	for _, v := range []viewID{viewDashboard, viewAgents, viewFactory, viewKaizen, viewHanami} {
		*asked = nil
		m.view = v
		if _, cmd := m.Update(press("d")); cmd == nil || len(*asked) != 1 || (*asked)[0] != "dispatcher-isshoni" {
			t.Errorf("view %d: asked %v", v, *asked)
		}
	}
}

func TestDPicksAmongSeveral(t *testing.T) {
	m, asked := attachModel(t, `{"dispatchers": ["dispatcher-a", "-bad", "dispatcher-b", "dispatcher-c"]}`)
	send(m, "d")
	out := plain(m)
	if len(*asked) != 0 || !strings.Contains(out, "1  dispatcher-a") || !strings.Contains(out, "2  dispatcher-b") || strings.Contains(out, "-bad") {
		t.Fatalf("the picker lists the safe names and starts nothing:\n%s", out)
	}
	send(m, "esc")
	if m.pick != nil || len(*asked) != 0 {
		t.Fatal("esc cancels the picker")
	}
	send(m, "d", "2")
	if len(*asked) != 1 || (*asked)[0] != "dispatcher-b" || m.pick != nil {
		t.Fatalf("2 picks the second: %v", *asked)
	}
	*asked = nil
	send(m, "d", "j", "j", "enter")
	if len(*asked) != 1 || (*asked)[0] != "dispatcher-c" {
		t.Fatalf("the arrows and enter pick: %v", *asked)
	}
	*asked = nil
	send(m, "d", "9", "4")
	if len(*asked) != 0 || m.pick == nil {
		t.Fatal("a number past the list does nothing")
	}
}

func TestDRefusesWithoutADispatcherOrWithoutWrite(t *testing.T) {
	m, asked := attachModel(t, `{"worktree_cap": 5}`)
	send(m, "d")
	if !m.toast.err || !strings.Contains(m.toast.text, `no dispatcher: "dispatchers" in `) || len(*asked) != 0 {
		t.Fatalf("toast %q", m.toast.text)
	}
	m, asked = attachModel(t, `{"dispatchers": ["dispatcher-a"]}`)
	m.opts.ReadOnly = true
	send(m, "d")
	if m.toast.text != "read-only: this key may not open the dispatcher" || len(*asked) != 0 {
		t.Fatalf("toast %q, asked %v", m.toast.text, *asked)
	}
}

func TestDDoesNothingWhereAttachIsUnavailable(t *testing.T) {
	settingsFile(t, `{"dispatchers": ["dispatcher-a"]}`)
	m := fakeModel(true, 100, 30)
	send(m, "d")
	if m.toast.text != "" || m.pick != nil {
		t.Fatalf("d acted without Attach: %q", m.toast.text)
	}
	m.help = true
	if out := plain(m); strings.Contains(out, "dispatcher") {
		t.Fatalf("the help lists d without Attach:\n%s", out)
	}
	m.opts.Attach = func(string) tea.ExecCommand { return nopExec{} }
	if out := plain(m); !strings.Contains(out, "d ") || !strings.Contains(out, "dispatcher") {
		t.Fatalf("the help lacks d with Attach:\n%s", out)
	}
}

func TestDIsNotTakenInsideAnOpenInput(t *testing.T) {
	m, asked := attachModel(t, `{"dispatchers": ["dispatcher-a"]}`)
	send(m, "/", "d")
	if m.filter != "d" || len(*asked) != 0 {
		t.Fatalf("filter %q, asked %v", m.filter, *asked)
	}
}

func TestTheEndOfAnAttachToastsAFailure(t *testing.T) {
	m, _ := attachModel(t, `{}`)
	m.Update(attachMsg{name: "dispatcher-a"})
	if m.toast.text != "" {
		t.Fatalf("a clean return toasts %q", m.toast.text)
	}
	m.Update(attachMsg{name: "dispatcher-a", err: errors.New("no pty")})
	if m.toast.text != "dispatcher dispatcher-a: no pty" || !m.toast.err {
		t.Fatalf("toast %q", m.toast.text)
	}
	m.Update(attachMsg{name: "dispatcher-a", err: exec.Command("sh", "-c", "exit 3").Run()})
	if m.toast.text != "dispatcher dispatcher-a: exit status 3; is it running? (claude agents)" {
		t.Fatalf("toast %q", m.toast.text)
	}
}
