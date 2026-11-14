package data

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestMain(m *testing.M) {
	os.Setenv("ISSHONI_CACHE_DIR", "") // the tests never touch the real session cache
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
// temporary directory: FACTORY_HOME set, and not under the real home's .claude.
func requireTempFactoryHome(t *testing.T) {
	t.Helper()
	fh := os.Getenv("FACTORY_HOME")
	if fh == "" || realHome == "" || fh == realHome || strings.HasPrefix(fh, filepath.Join(realHome, ".claude")) {
		t.Fatalf("FACTORY_HOME=%q would reach the real %s/.claude/factory", fh, realHome)
	}
}
