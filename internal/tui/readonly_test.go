package tui

import (
	"os"
	"testing"
)

func TestReadOnlyRefusesTheCapField(t *testing.T) {
	path := settingsFile(t, "[limits]\nworktree_cap = 12\n")
	m := factoryModel(true, 100, 30, alpha())
	m.opts.ReadOnly = true
	send(m, "c")
	if m.fac.editing {
		t.Fatal("a read-only dashboard opened the cap field")
	}
	if m.toast.text != "read-only: this key may not change the cap" {
		t.Fatalf("toast %q", m.toast.text)
	}
	before, _ := os.ReadFile(path)
	send(m, "c", "6", "enter")
	if after, _ := os.ReadFile(path); string(after) != string(before) {
		t.Fatalf("the settings file changed: %s", after)
	}
}
