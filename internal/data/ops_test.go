package data

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// opsDoc is the contract example: one watching watch with every field set, one ended with a verdict and
// no last look or next look.
const opsDoc = `{"watches": [
 {"name": "pr60-logs", "repo": "/home/dev/app", "goal": "watch the logs", "state": "watching", "verdict": null,
  "started": "2026-11-15T09:00:00Z", "until": "2026-11-15T15:00:00Z", "every_seconds": 600, "ticks": 7, "missed": 3,
  "last_look": {"ts": "2026-11-15T09:54:00Z", "outcome": "could-not-look", "saw": "0 errors", "late_seconds": 120},
  "next_look": "2026-11-15T10:10:00Z", "spent_usd": 0.42, "unpriced_ticks": 2, "budget_usd": 5.0,
  "notes": "/home/dev/store/ops/pr60-logs/notes.md"},
 {"name": "old", "repo": "/home/dev/lib", "goal": "g", "state": "ended", "verdict": "met",
  "started": "2026-11-14T09:00:00Z", "until": "2026-11-14T15:00:00Z", "every_seconds": 3600, "ticks": 0, "missed": 0,
  "last_look": null, "next_look": null, "spent_usd": 0, "unpriced_ticks": 0, "budget_usd": 1.5, "notes": "/n"}]}`

func writeStub(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestOpsWatchesDecodesTheContract(t *testing.T) {
	r, dir := factoryStub(t)
	writeStub(t, dir, "ops.json", opsDoc)
	ws, err := r.OpsWatches(context.Background())
	if err != nil || len(ws) != 2 {
		t.Fatalf("%d watches, err %v", len(ws), err)
	}
	a, b := ws[0], ws[1]
	if a.Name != "pr60-logs" || a.Repo != "/home/dev/app" || a.Goal != "watch the logs" || a.State != "watching" || a.Verdict != nil ||
		a.Started != "2026-11-15T09:00:00Z" || a.Until != "2026-11-15T15:00:00Z" || a.EverySeconds != 600 || a.Ticks != 7 || a.Missed != 3 ||
		a.NextLook == nil || *a.NextLook != "2026-11-15T10:10:00Z" || a.SpentUSD != 0.42 || a.UnpricedTicks != 2 || a.BudgetUSD != 5 ||
		a.Notes != "/home/dev/store/ops/pr60-logs/notes.md" {
		t.Fatalf("the watching watch: %+v", a)
	}
	if l := a.LastLook; l == nil || *l != (OpsLook{TS: "2026-11-15T09:54:00Z", Outcome: "could-not-look", Saw: "0 errors", LateSeconds: 120}) {
		t.Fatalf("last look: %+v", l)
	}
	if b.Verdict == nil || *b.Verdict != "met" || b.LastLook != nil || b.NextLook != nil || b.State != "ended" {
		t.Fatalf("the ended watch: %+v", b)
	}
	if log, _ := os.ReadFile(filepath.Join(dir, "calls")); !strings.HasPrefix(string(log), "ops ") || !strings.Contains(string(log), "/") {
		t.Fatalf("one ops call: %q", log)
	}
}

func TestOpsWatchesErrors(t *testing.T) {
	noOps, err := os.ReadFile("testdata/ops-no-command.err") // what this checkout's factory printed for `ops list --json`
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		name, file, content, want string
	}{
		{"no ops command", "ops.err", string(noOps), "this factory has no ops command yet"},
		{"failure", "fail", "", "boom"},
		{"bad json", "ops.json", "{", "decode factory ops output: "},
		{"timeout", "hang", "", "factory ops list timed out after 700ms"},
	} {
		t.Run(c.name, func(t *testing.T) {
			r, dir := factoryStub(t)
			writeStub(t, dir, c.file, c.content)
			_, err := r.OpsWatches(context.Background())
			if err == nil || !strings.HasPrefix(err.Error(), c.want) {
				t.Fatalf("got %v, want %q", err, c.want)
			}
		})
	}
	t.Run("missing script", func(t *testing.T) {
		r, _ := factoryStub(t)
		r.Root = t.TempDir()
		if _, err := r.OpsWatches(context.Background()); err == nil || err.Error() != "factory command not found: "+filepath.Join(r.Root, "factory", "bin", "factory") {
			t.Fatalf("got %v", err)
		}
	})
}
