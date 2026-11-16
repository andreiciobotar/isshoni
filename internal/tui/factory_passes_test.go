package tui

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ---- the two passes of a refresh --------------------------------------------------

// passStatus is what the stub prints for one repository with one task, its pull request data dated asOf
// (zero for none) and the task named slug.
func passStatus(slug string, asOf time.Time) string {
	at := "null"
	if !asOf.IsZero() {
		at = fmt.Sprintf("%.1f", float64(asOf.UnixNano())/1e9)
	}
	return fmt.Sprintf(`{"rows":[{"slug":%q,"phase":"motion","base":"main","pr_state":"unknown"}],"cap":5,"in_motion":1,"prs":{"as_of":%s,"error":null}}`, slug, at)
}

func writeStub(t *testing.T, dir, file, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, file), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// fetchCount counts the fetch passes the stub saw.
func fetchCount(t *testing.T, dir string) int {
	t.Helper()
	b, _ := os.ReadFile(filepath.Join(dir, "fetches"))
	return strings.Count(string(b), "factory ")
}

func TestFactoryFirstFrameHasRowsWhileTheFetchPassHangs(t *testing.T) {
	m, dir, now := factoryStubModel(t)
	writeStub(t, dir, "status.json", passStatus("alpha-task", time.Time{}))
	os.WriteFile(filepath.Join(dir, "fetch-hang"), nil, 0o644)
	start := time.Now()
	first := only[factoryMsg](upd(m, press("3")))
	_, fetch := m.Update(first[0])
	if time.Since(start) > 3*time.Second || fetchCount(t, dir) != 0 {
		t.Fatalf("the cached pass answers before any fetch starts: %v, %d fetches", time.Since(start), fetchCount(t, dir))
	}
	if out := plain(m); !strings.Contains(out, "alpha-task") || strings.Contains(out, "GitHub slow") {
		t.Fatalf("the first frame has the rows:\n%s", out)
	}
	got := only[factoryFetchMsg](run(fetch))
	if len(got) != 2 || fetchCount(t, dir) != 2 {
		t.Fatalf("one fetch pass per repository: %d messages, %d fetches", len(got), fetchCount(t, dir))
	}
	for _, g := range got {
		upd(m, g)
	}
	out := plain(m)
	if strings.Count(out, "alpha-task") != 2 || strings.Count(out, "pull requests not fetched yet · GitHub slow: factory status timed out after 700ms") != 2 {
		t.Fatalf("a hung fetch pass keeps the rows and dates them:\n%s", out)
	}

	os.Remove(filepath.Join(dir, "fetch-hang"))
	writeStub(t, dir, "status-fetch.json", passStatus("beta-task", *now))
	*now = now.Add(factoryEvery + time.Second)
	first = only[factoryMsg](upd(m, tickMsg{}))
	_, fetch = m.Update(first[0])
	if out := plain(m); !strings.Contains(out, "GitHub slow") {
		t.Fatalf("a later cached pass does not clear the error by itself:\n%s", out)
	}
	for _, g := range only[factoryFetchMsg](run(fetch)) {
		upd(m, g)
	}
	out = plain(m)
	if strings.Contains(out, "GitHub slow") || strings.Contains(out, "alpha-task") || strings.Count(out, "beta-task") != 2 {
		t.Fatalf("a successful fetch pass replaces the rows and clears the error:\n%s", out)
	}
}

func TestFactoryFailedFetchPassKeepsRowsAndShowsTheDatedLine(t *testing.T) {
	m, dir, now := factoryStubModel(t)
	writeStub(t, dir, "status.json", passStatus("alpha-task", now.Add(-5*time.Minute)))
	os.WriteFile(filepath.Join(dir, "fetch-fail"), nil, 0o644)
	_, fetch := m.Update(only[factoryMsg](upd(m, press("3")))[0])
	for _, g := range only[factoryFetchMsg](run(fetch)) {
		upd(m, g)
	}
	out := plain(m)
	if strings.Count(out, "pull requests as of 5m ago · GitHub slow: not inside a git repository") != 2 || strings.Count(out, "alpha-task") != 2 {
		t.Fatalf("a failed fetch pass keeps the rows and says why, dated:\n%s", out)
	}
}

func TestFactoryOldDataIsDatedWithoutAnError(t *testing.T) {
	m, dir, now := factoryStubModel(t)
	writeStub(t, dir, "status.json", passStatus("alpha-task", now.Add(-3*time.Minute)))
	upd(m, only[factoryMsg](upd(m, press("3")))[0])
	if out := plain(m); strings.Count(out, "pull requests as of 3m ago") != 2 || strings.Contains(out, "GitHub slow") {
		t.Fatalf("data older than 2 minutes is dated:\n%s", out)
	}
	for _, r := range m.fac.repos {
		r.st.PRs.AsOf = ptr(float64(now.Add(-time.Minute).Unix()))
	}
	if out := plain(m); strings.Contains(out, "pull requests as of") {
		t.Fatalf("data under 2 minutes old is not dated:\n%s", out)
	}
}

func TestFactoryNoFetchPassOffTheViewOrWhenFreshOrWithoutRows(t *testing.T) {
	m, dir, now := factoryStubModel(t)
	writeStub(t, dir, "status.json", passStatus("alpha-task", time.Time{}))
	first := only[factoryMsg](upd(m, tickMsg{})) // the dashboard: the badge needs the count
	if len(first) != 1 || m.view == viewFactory {
		t.Fatalf("the first refresh runs off the view: %d", len(first))
	}
	if _, cmd := m.Update(first[0]); cmd != nil || fetchCount(t, dir) != 0 {
		t.Fatalf("off the view only the cached pass runs: %d fetches", fetchCount(t, dir))
	}

	send(m, "3")
	*now = now.Add(factoryEvery + time.Second)
	writeStub(t, dir, "status.json", passStatus("alpha-task", now.Add(-10*time.Second)))
	if _, cmd := m.Update(only[factoryMsg](upd(m, tickMsg{}))[0]); cmd != nil || fetchCount(t, dir) != 0 {
		t.Fatalf("data from under a minute ago starts no fetch pass: %d", fetchCount(t, dir))
	}

	os.Remove(filepath.Join(dir, "status.json")) // the default: no rows
	*now = now.Add(factoryEvery + time.Second)
	if _, cmd := m.Update(only[factoryMsg](upd(m, tickMsg{}))[0]); cmd != nil || fetchCount(t, dir) != 0 {
		t.Fatalf("a repository without rows starts none: %d", fetchCount(t, dir))
	}
}

func TestFactoryOneFetchPassInFlightPerRepository(t *testing.T) {
	m, dir, now := factoryStubModel(t)
	writeStub(t, dir, "status.json", passStatus("alpha-task", time.Time{}))
	first := only[factoryMsg](upd(m, press("3")))[0]
	_, fetch := m.Update(first)
	if fetch == nil {
		t.Fatal("the first result starts the fetch passes")
	}
	m.fac.busy = true
	*now = now.Add(factoryEvery + time.Second)
	if _, again := m.Update(first); again != nil {
		t.Fatal("a second cached pass while the fetch passes run starts no more")
	}
	for _, r := range m.fac.repos {
		if !r.fetching {
			t.Fatalf("the flag survives the cached pass: %+v", r)
		}
	}
	for _, g := range only[factoryFetchMsg](run(fetch)) {
		upd(m, g)
	}
	for _, r := range m.fac.repos {
		if r.fetching {
			t.Fatalf("a result frees the repository: %+v", r)
		}
	}
	if n := fetchCount(t, dir); n != 2 {
		t.Fatalf("two fetch passes in all: %d", n)
	}
}

func TestFactoryFetchesOnlyTheRepositoriesThatAnswered(t *testing.T) {
	m, dir, _ := factoryStubModel(t)
	writeStub(t, dir, "status.json", passStatus("alpha-task", time.Time{}))
	first := only[factoryMsg](upd(m, press("3")))[0]
	first.repos[0].st, first.repos[0].err = nil, "boom"
	_, fetch := m.Update(first)
	if got := only[factoryFetchMsg](run(fetch)); len(got) != 1 || got[0].path != first.repos[1].path {
		t.Fatalf("only the repository whose cached pass succeeded is fetched: %+v", got)
	}
}
