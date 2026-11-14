package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestMain(m *testing.M) {
	// Nothing a test starts may write the real ~/.claude/factory: its home is a temp directory unless a test
	// points it at another temp directory (never at "", which means the real home).
	home, err := os.MkdirTemp("", "factory-home-")
	if err != nil {
		panic(err)
	}
	os.Setenv("FACTORY_HOME", home)
	code := m.Run()
	os.RemoveAll(home)
	os.Exit(code)
}

// realHome is the home directory the tests started with, read before any test changes HOME.
var realHome, _ = os.UserHomeDir()

// requireTempFactoryHome fails the test unless the factory script, started now, would use a settings file in a
// temporary directory: FACTORY_HOME set, and neither the real ~/.claude/factory nor inside it.
func requireTempFactoryHome(t *testing.T) {
	t.Helper()
	real := filepath.Join(realHome, ".claude", "factory")
	fh := filepath.Clean(os.Getenv("FACTORY_HOME"))
	if os.Getenv("FACTORY_HOME") == "" || realHome == "" || fh == real || strings.HasPrefix(fh, real+string(filepath.Separator)) {
		t.Fatalf("FACTORY_HOME=%q would reach the real %s", os.Getenv("FACTORY_HOME"), real)
	}
}

func TestAbsoluteResolvesAgainstWorkingDirectory(t *testing.T) {
	wd := t.TempDir()
	if err := os.Mkdir(filepath.Join(wd, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(wd)
	got := absolute("sub")
	if !filepath.IsAbs(got) {
		t.Fatalf("relative dir not made absolute: %q", got)
	}
	want, _ := filepath.EvalSymlinks(filepath.Join(wd, "sub"))
	if real, _ := filepath.EvalSymlinks(got); real != want {
		t.Fatalf("absolute(sub) = %q, want %q", real, want)
	}
	if got := absolute(""); got != "" {
		t.Fatalf("empty stays empty, got %q", got)
	}
	if got := absolute("/x/y"); got != "/x/y" {
		t.Fatalf("an absolute dir stays, got %q", got)
	}
}
