package data

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// factoryStub is a runner whose interpreter is the stub script; STUBDIR holds its switches.
func factoryStub(t *testing.T) (*Runner, string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("STUBDIR", dir)
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	py, err := filepath.Abs("testdata/fakepy.sh")
	if err != nil {
		t.Fatal(err)
	}
	return &Runner{Root: root, Python: py, Timeout: 700 * time.Millisecond}, dir
}

func TestFactoryStatusDecodesNulls(t *testing.T) {
	r, dir := factoryStub(t)
	b, err := os.ReadFile("testdata/factory-status.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "status.json"), b, 0o644); err != nil {
		t.Fatal(err)
	}
	repo := t.TempDir()
	st, err := r.FactoryStatus(context.Background(), repo, false)
	if err != nil {
		t.Fatal(err)
	}
	if st.Cap != 7 || st.Reserve != 1 || st.InMotion != 2 || st.Blocked != 1 || st.InReview != 1 || st.Settled != 1 || len(st.Rows) != 4 {
		t.Fatalf("status %+v", st)
	}
	if a := st.Rows[0]; a.Elapsed != nil || a.Waited != nil || a.StopKind != "" || a.StopSummary != "" || a.Slug != "cost-in-money" || a.PRState != "unknown" {
		t.Fatalf("row with nulls: %+v", a)
	}
	if b := st.Rows[1]; b.Elapsed == nil || *b.Elapsed != 252 || !b.Overtime || !b.Stale || b.PRURL == "" || b.PRDraft || !st.Rows[0].PRDraft {
		t.Fatalf("row: %+v", b)
	}
	if c := st.Rows[2]; c.Waited == nil || *c.Waited != 600 || c.StopKind != "QUESTION" || c.StopSummary != "Which database should it use?" {
		t.Fatalf("the blocked row's wait and stop: %+v", c)
	}
	if c, d := st.Rows[2], st.Rows[3]; c.Phase != "blocked" || c.Deliverable != "" || d.Phase != "settled" || d.Deliverable != "/r/docs/specs/write-up.md" {
		t.Fatalf("blocked and settled rows: %+v %+v", c, d)
	}
	log, _ := os.ReadFile(filepath.Join(dir, "calls"))
	if !strings.HasPrefix(string(log), "factory ") || !strings.HasSuffix(strings.TrimSpace(string(log)), repo[strings.Index(repo, "TestFactory"):]) {
		t.Fatalf("the command runs in the repository %s: %q", repo, log)
	}
}

func TestFactoryStatusErrors(t *testing.T) {
	r, dir := factoryStub(t)
	os.WriteFile(filepath.Join(dir, "fail"), nil, 0o644)
	if _, err := r.FactoryStatus(context.Background(), t.TempDir(), false); err == nil || err.Error() != "not inside a git repository" {
		t.Fatalf("exit 2 gives the stderr line without its prefix: %v", err)
	}
	os.Remove(filepath.Join(dir, "fail"))

	os.WriteFile(filepath.Join(dir, "status.json"), []byte("nope"), 0o644)
	if _, err := r.FactoryStatus(context.Background(), t.TempDir(), false); err == nil || !strings.Contains(err.Error(), "decode factory output") {
		t.Fatalf("decode error: %v", err)
	}
	os.Remove(filepath.Join(dir, "status.json"))

	missing := &Runner{Root: t.TempDir(), Python: r.Python}
	if _, err := missing.FactoryStatus(context.Background(), t.TempDir(), false); err == nil || !strings.Contains(err.Error(), "factory command not found: "+filepath.Join(missing.Root, "factory", "bin", "factory")) {
		t.Fatalf("missing script names its path: %v", err)
	}
}

func TestFactoryStatusTimeoutKillsTheChildren(t *testing.T) {
	r, dir := factoryStub(t)
	os.WriteFile(filepath.Join(dir, "hang"), nil, 0o644)
	if _, err := r.FactoryStatus(context.Background(), t.TempDir(), false); err == nil {
		t.Fatal("a hung call must time out")
	}
	b, err := os.ReadFile(filepath.Join(dir, "child"))
	if err != nil {
		t.Fatalf("the stub did not start a child: %v", err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; syscall.Kill(pid, 0) == nil; i++ {
		if i == 100 {
			syscall.Kill(pid, syscall.SIGKILL)
			t.Fatalf("child %d survived the timeout", pid)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestFactoryStatusTimesOutAndFreesTheRunner(t *testing.T) {
	r, dir := factoryStub(t)
	os.WriteFile(filepath.Join(dir, "hang"), nil, 0o644)
	start := time.Now()
	_, err := r.FactoryStatus(context.Background(), t.TempDir(), false)
	if err == nil || err.Error() != "factory status timed out after 700ms" {
		t.Fatalf("got %v", err)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatal("a hung call must be killed at its timeout")
	}
	done := make(chan struct{})
	go func() { r.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Wait did not return after the call timed out")
	}
}

func TestFactoryStatusNoProcessAfterWait(t *testing.T) {
	r, dir := factoryStub(t)
	r.Wait()
	if _, err := r.FactoryStatus(context.Background(), t.TempDir(), false); err == nil {
		t.Fatal("a call after Wait must fail")
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "calls")); len(b) != 0 {
		t.Fatalf("a process started after Wait: %q", b)
	}
}

func TestFactoryStatusRealScriptInEmptyRepo(t *testing.T) {
	for _, tool := range []string{"git", "python3"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("%s missing", tool)
		}
	}
	root, _ := filepath.Abs("../..")
	repo := t.TempDir()
	t.Setenv("HOME", t.TempDir())
	t.Setenv("GIT_CONFIG_GLOBAL", os.DevNull)
	for _, args := range [][]string{{"init", "-q"}, {"-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"}} {
		cmd := exec.Command("git", args...)
		cmd.Dir = repo
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v %s", args, err, out)
		}
	}
	st, err := (&Runner{Root: root}).FactoryStatus(context.Background(), repo, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(st.Rows) != 0 || st.Cap != 5 {
		t.Fatalf("empty repository: %+v", st)
	}
}

func settingsNamed(t *testing.T, name, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "factory", name)
	if content != "" {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	return filepath.Join(filepath.Dir(path), "settings.toml")
}

func settings(t *testing.T, content string) string { return settingsNamed(t, "settings.toml", content) }

// handEdited is a settings file as a human leaves it: tables and keys in their own order, comments, blank lines, an unknown key.
const handEdited = `# my notes
[paths]
monitor = "/x" # mine

[limits]   # the caps
reserve_percent = 20
worktree_cap = 3   # three is enough
task_time_limit_hours = 1.5

[sessions]
dispatchers = [
  "dispatcher-a", # first
  "dispatcher-b",
]
extra = 1
`

func TestReadWorktreeCap(t *testing.T) {
	ok := map[string]int{"": 5, "[limits]\n": 5, "[models]\nlead = \"x\"\n": 5, "[limits]\nworktree_cap = 12\n": 12,
		"worktree_cap = 9\n[limits]\nreserve_percent = 1\n": 5, "[sessions]\nworktree_cap = 9\n": 5, handEdited: 3,
		"[limits]\nworktree_cap=7#seven\n": 7, "[ limits ]\n  worktree_cap  =  4  \n": 4}
	for content, want := range ok {
		if got, err := ReadWorktreeCap(settings(t, content)); err != nil || got != want {
			t.Errorf("%q: %d, %v; want %d", content, got, err, want)
		}
	}
	for _, value := range []string{`0`, `-1`, `1.5`, `true`, `"3"`, `abc`, ``} {
		path := settings(t, "[limits]\nworktree_cap = "+value+"\n")
		if _, err := ReadWorktreeCap(path); !errors.Is(err, ErrWorktreeCap) || !strings.Contains(err.Error(), path) {
			t.Errorf("%q: want an invalid-value error naming the file, got %v", value, err)
		}
	}
	path := settings(t, "[limits]\nworktree_cap = [1\n")
	if _, err := ReadWorktreeCap(path); err == nil || !strings.HasPrefix(err.Error(), path+" line 2: ") || errors.Is(err, ErrWorktreeCap) {
		t.Errorf("a list it cannot read is an error naming the file and the line: %v", err)
	}
	home := t.TempDir()
	t.Setenv("HOME", home)
	os.WriteFile(filepath.Join(home, "s.toml"), []byte("[limits]\nworktree_cap = 0\n"), 0o644)
	if _, err := ReadWorktreeCap(filepath.Join(home, "s.toml")); err == nil || err.Error() != "~/s.toml: worktree_cap is invalid" {
		t.Errorf("the home directory is shown as ~: %v", err)
	}
}

func TestReadSettingsFromALegacyJSONWhenThereIsNoTOML(t *testing.T) {
	for content, want := range map[string]int{`{"worktree_cap": 12}`: 12, `{}`: 5, `{"lead_model":"x"}`: 5} {
		if got, err := ReadWorktreeCap(settingsNamed(t, "settings.json", content)); err != nil || got != want {
			t.Errorf("%q: %d, %v; want %d", content, got, err, want)
		}
	}
	for _, content := range []string{`{"worktree_cap":0}`, `{"worktree_cap":1.5}`, `{"worktree_cap":true}`, `{"worktree_cap":"3"}`, `{"worktree_cap":null}`} {
		path := settingsNamed(t, "settings.json", content)
		if _, err := ReadWorktreeCap(path); !errors.Is(err, ErrWorktreeCap) {
			t.Errorf("%q: want an invalid-value error, got %v", content, err)
		}
	}
	for content, kind := range map[string]string{`[1]`: "not a JSON object", `not json`: "not valid JSON"} {
		path := settingsNamed(t, "settings.json", content)
		if _, err := ReadWorktreeCap(path); err == nil || !strings.Contains(err.Error(), "settings.json is "+kind) {
			t.Errorf("%q: want %q naming the file, got %v", content, kind, err)
		}
	}
	path := settingsNamed(t, "settings.json", `{"worktree_cap": 12}`)
	os.WriteFile(path, []byte("[limits]\nworktree_cap = 2\n"), 0o644)
	if got, _ := ReadWorktreeCap(path); got != 2 {
		t.Errorf("the TOML wins when both exist: %d", got)
	}
	if got, err := ReadDispatchers(settingsNamed(t, "settings.json", `{"dispatchers": ["dispatcher-a"]}`)); err != nil || !slices.Equal(got, []string{"dispatcher-a"}) {
		t.Errorf("legacy dispatchers: %v, %v", got, err)
	}
}

func TestReadDispatchers(t *testing.T) {
	got, err := ReadDispatchers(settings(t, "[sessions]\ndispatchers = [\"dispatcher-b\", \"-x\", \"a.b_c\", \"\", \"has space\", \"dispatcher-a\"]\n"))
	if err != nil || !slices.Equal(got, []string{"dispatcher-b", "a.b_c", "dispatcher-a"}) {
		t.Fatalf("order kept, unsafe names dropped: %v, %v", got, err)
	}
	got, err = ReadDispatchers(settings(t, handEdited))
	if err != nil || !slices.Equal(got, []string{"dispatcher-a", "dispatcher-b"}) {
		t.Fatalf("a list over several lines, with comments and a trailing comma: %v, %v", got, err)
	}
	for content, why := range map[string]string{
		``:                                     "is not set",
		"[sessions]\n":                         "is not set",
		"[sessions]\ndispatchers = []\n":       "is empty",
		"[sessions]\ndispatchers = [\n]\n":     "is empty",
		"[sessions]\ndispatchers = [\"-x\"]\n": "is empty",
		"[sessions]\ndispatchers = \"a\"\n":    "is not a list of strings",
		"[sessions]\ndispatchers = 3\n":        "is not a list of strings",
		"[merging]\ndispatchers = [\"a\"]\n":   "is not set",
	} {
		path := settings(t, content)
		_, err := ReadDispatchers(path)
		if err == nil || !strings.Contains(err.Error(), `no dispatcher: "dispatchers" `+why+" ("+path+")") {
			t.Errorf("%q: want %q naming the file, got %v", content, why, err)
		}
	}
	for content, line := range map[string]int{
		"[sessions]\ndispatchers = [\"a\", 1]\n":    2,
		"[sessions]\ndispatchers = [\"a\" \"b\"]\n": 2,
		"[sessions]\n\ndispatchers = [\"a\"\n":      3,
		"[sessions]\ndispatchers = [\n\"a\",\n,]\n": 4,
		"[sessions]\ndispatchers = [\"a\"] x\n":     2,
		"[sessions]\ndispatchers = [\"a\n":          2,
	} {
		path := settings(t, content)
		if _, err := ReadDispatchers(path); err == nil || !strings.HasPrefix(err.Error(), path+" line "+strconv.Itoa(line)+": ") {
			t.Errorf("%q: want an error naming the file and line %d, got %v", content, line, err)
		}
	}
}

// stubFactory puts a `factory` that records its arguments (and fails with `fail` on stderr when that is set) first on PATH.
func stubFactory(t *testing.T, fail string) string {
	t.Helper()
	dir := t.TempDir()
	script := "#!/bin/sh\necho \"$@\" > \"" + dir + "/args\"\n"
	if fail != "" {
		script += "echo 'noise' >&2\necho '" + fail + "' >&2\nexit 2\n"
	}
	if err := os.WriteFile(filepath.Join(dir, "factory"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return dir
}

func TestWriteWorktreeCapRunsTheFactoryCommand(t *testing.T) {
	dir := stubFactory(t, "")
	if err := WriteWorktreeCap(8); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "args")); string(b) != "settings set limits.worktree_cap 8\n" {
		t.Fatalf("arguments: %q", b)
	}
	stubFactory(t, "factory: limits.worktree_cap must be an integer of at least 1")
	if err := WriteWorktreeCap(0); err == nil || err.Error() != "limits.worktree_cap must be an integer of at least 1" {
		t.Fatalf("the command's last stderr line is the error: %v", err)
	}
	t.Setenv("PATH", t.TempDir())
	if err := WriteWorktreeCap(3); err == nil || !strings.Contains(err.Error(), "not on PATH") {
		t.Fatalf("no factory on PATH: %v", err)
	}
}

func TestWriteWorktreeCapWithTheRealFactoryChangesOneLine(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 is missing")
	}
	script, err := filepath.Abs("../../factory/bin/factory")
	if err != nil {
		t.Fatal(err)
	}
	bin := t.TempDir()
	if err := os.Symlink(script, filepath.Join(bin, "factory")); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	home := t.TempDir()
	t.Setenv("FACTORY_HOME", home)
	path := filepath.Join(home, "settings.toml")
	if err := os.WriteFile(path, []byte(handEdited), 0o640); err != nil {
		t.Fatal(err)
	}
	if err := WriteWorktreeCap(8); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(path)
	if want := strings.Replace(handEdited, "worktree_cap = 3   # three", "worktree_cap = 8   # three", 1); string(b) != want {
		t.Fatalf("got %q\nwant %q", b, want)
	}
	if got, err := ReadWorktreeCap(path); err != nil || got != 8 {
		t.Fatalf("the reader sees the new value: %d, %v", got, err)
	}
	if err := WriteWorktreeCap(0); err == nil || !strings.Contains(err.Error(), "limits.worktree_cap must be an integer of at least 1") {
		t.Fatalf("the factory's refusal is the error: %v", err)
	}
	if b2, _ := os.ReadFile(path); string(b2) != string(b) {
		t.Fatalf("a refused value leaves the file as it was: %q", b2)
	}
}

func TestSettingsErrorsNameTheFileShort(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	path := FactorySettingsPath()
	if err := os.MkdirAll(path, 0o755); err != nil { // a directory where the file should be
		t.Fatal(err)
	}
	if _, err := ReadWorktreeCap(path); err == nil || !strings.HasPrefix(err.Error(), "~/.claude/factory/settings.toml: ") || strings.Contains(err.Error(), home) {
		t.Errorf("want an error starting with the ~ path and no absolute path, got %v", err)
	}
}

func TestFactoryStatusPassesTellApartByCachedAndDecodePRs(t *testing.T) {
	r, dir := factoryStub(t)
	repo := t.TempDir()
	cached := `{"rows":[],"cap":5,"prs":{"as_of":null,"error":null}}`
	fetched := `{"rows":[],"cap":5,"prs":{"as_of":1794668400.5,"error":"timed out after 10 s"}}`
	os.WriteFile(filepath.Join(dir, "status.json"), []byte(cached), 0o644)
	os.WriteFile(filepath.Join(dir, "status-fetch.json"), []byte(fetched), 0o644)
	st, err := r.FactoryStatus(context.Background(), repo, false)
	if err != nil || st.PRs.AsOf != nil || st.PRs.Error != nil {
		t.Fatalf("cached pass: %+v, %v", st, err)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "fetches")); len(b) != 0 {
		t.Fatalf("the cached pass is not a fetch pass: %q", b)
	}
	st, err = r.FactoryStatus(context.Background(), repo, true)
	if err != nil || st.PRs.AsOf == nil || *st.PRs.AsOf != 1794668400.5 || st.PRs.Error == nil || *st.PRs.Error != "timed out after 10 s" {
		t.Fatalf("fetch pass: %+v, %v", st, err)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "fetches")); !strings.HasPrefix(string(b), "factory ") {
		t.Fatalf("the fetch pass is logged: %q", b)
	}
}

func TestFactoryStatusFetchPassFailsAndHangsAlone(t *testing.T) {
	r, dir := factoryStub(t)
	os.WriteFile(filepath.Join(dir, "fetch-hang"), nil, 0o644)
	if _, err := r.FactoryStatus(context.Background(), t.TempDir(), false); err != nil {
		t.Fatalf("a hung fetch pass does not touch the cached one: %v", err)
	}
	if _, err := r.FactoryStatus(context.Background(), t.TempDir(), true); err == nil || err.Error() != "factory status timed out after 700ms" {
		t.Fatalf("the fetch pass times out: %v", err)
	}
	os.Remove(filepath.Join(dir, "fetch-hang"))
	os.WriteFile(filepath.Join(dir, "fetch-fail"), nil, 0o644)
	if _, err := r.FactoryStatus(context.Background(), t.TempDir(), false); err != nil {
		t.Fatalf("a failing fetch pass does not touch the cached one: %v", err)
	}
	if _, err := r.FactoryStatus(context.Background(), t.TempDir(), true); err == nil {
		t.Fatal("the fetch pass fails")
	}
}
