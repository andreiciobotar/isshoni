package data

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	// cachedTimeout bounds `factory status --cached`, which never leaves the machine.
	cachedTimeout = 10 * time.Second
	// fetchTimeout bounds `factory status --max-age`, which may ask GitHub; the factory
	// itself stops waiting on GitHub after 10 s per call.
	fetchTimeout = 25 * time.Second
	// PRFloor is the age of the cached pull request data that the fetch pass accepts.
	PRFloor = 60 * time.Second
)

// DefaultWorktreeCap is the factory's default (load_settings in factory/bin/factory).
const DefaultWorktreeCap = 5

// FactoryTask is one row of `factory status --json`.
type FactoryTask struct {
	Slug     string `json:"slug"`
	Phase    string `json:"phase"`
	Base     string `json:"base"`
	Elapsed  *int64 `json:"elapsed"` // seconds since the session started
	Overtime bool   `json:"overtime"`
	Nearing  bool   `json:"nearing"` // past the share of its limit the nearing-limit criterion names; false without that entry
	Size     string `json:"size"`    // small, medium or large; empty when null
	Limit    *int64 `json:"limit"`   // seconds; nil from a factory that does not send it
	Stale    bool   `json:"stale"`
	PRState  string `json:"pr_state"`
	PRURL    string `json:"pr_url"`
	PRDraft  bool   `json:"pr_draft"` // its pull request is a draft: still being worked on or checks not green
	Session  string `json:"session"`

	Deliverable string `json:"deliverable"` // a link or an absolute file path; empty when null

	Waited      *int64 `json:"waited"`       // seconds since the last declared record; nil when undeclared
	StopKind    string `json:"stop_kind"`    // QUESTION, BLOCKED or OVERTIME for a blocked task; empty when null
	StopSummary string `json:"stop_summary"` // why it stopped; empty when null
	WaitsOn     string `json:"waits_on"`     // a blocked task: human, dispatcher or a task's slug; empty when null
}

// WaitsOnHuman says whether a blocked task waits on the human; an empty WaitsOn is a
// script from before the field.
func (t FactoryTask) WaitsOnHuman() bool {
	return t.Phase == "blocked" && (t.WaitsOn == "" || t.WaitsOn == "human")
}

// FactoryStatus is what `factory status --json` prints for one repository.
type FactoryStatus struct {
	Rows     []FactoryTask `json:"rows"`
	Cap      int           `json:"cap"`
	Reserve  int           `json:"reserve"`   // slots of the cap held back for critical work; 0 when absent
	InMotion int           `json:"in_motion"` // tasks holding a slot, the blocked included
	Blocked  int           `json:"blocked"`
	InReview int           `json:"in_review"`
	Merged   int           `json:"merged"`
	Settled  int           `json:"settled"`
	PRs      FactoryPRs    `json:"prs"`
}

// FactoryPRs dates the pull request fields of the rows: AsOf is the epoch second of the
// last successful fetch the rows use (nil when there is none), Error why this run could
// not refresh them (nil when it could, and always nil with --cached).
type FactoryPRs struct {
	AsOf  *float64 `json:"as_of"`
	Error *string  `json:"error"`
}

// FactoryStatus runs `factory status --json` in the repository at repoPath: with
// fetch false as `--cached`, which reads the pull requests from the disk cache and
// never asks GitHub; with fetch true as `--max-age`, which asks GitHub when that cache
// is older than PRFloor.
func (r *Runner) FactoryStatus(ctx context.Context, repoPath string, fetch bool) (*FactoryStatus, error) {
	script, err := filepath.Abs(filepath.Join(r.Root, "factory", "bin", "factory"))
	if err != nil {
		return nil, err
	}
	if _, err := os.Stat(script); err != nil {
		return nil, fmt.Errorf("factory command not found: %s", script)
	}
	b, err := r.shared(ctx, "factory\x00"+strconv.FormatBool(fetch)+"\x00"+repoPath, false, func(ctx context.Context) ([]byte, error) {
		return r.factoryOutput(ctx, script, repoPath, fetch)
	})
	if err != nil {
		return nil, err
	}
	var out FactoryStatus
	if err := json.Unmarshal(b, &out); err != nil {
		return nil, fmt.Errorf("decode factory output: %w", err)
	}
	return &out, nil
}

// factoryOutput is one `factory status --json` process and what it printed.
func (r *Runner) factoryOutput(ctx context.Context, script, repoPath string, fetch bool) ([]byte, error) {
	if !r.begin() {
		return nil, cmp.Or(ctx.Err(), context.Canceled)
	}
	defer r.calls.Done()
	timeout, flag := cachedTimeout, []string{"--cached"}
	if fetch {
		timeout, flag = fetchTimeout, []string{"--max-age", strconv.Itoa(int(PRFloor.Seconds()))}
	}
	timeout = cmp.Or(r.Timeout, timeout)
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := groupCmd(ctx, r.python(), append([]string{script, "status", "--json"}, flag...)...)
	cmd.Dir = repoPath
	var so, se bytes.Buffer
	cmd.Stdout, cmd.Stderr = &so, &se
	if err := cmd.Run(); err != nil {
		switch {
		case errors.Is(err, exec.ErrNotFound):
			return nil, ErrPython
		case errors.Is(ctx.Err(), context.DeadlineExceeded):
			return nil, fmt.Errorf("factory status timed out after %s", timeout)
		case ctx.Err() != nil:
			return nil, ctx.Err()
		}
		msg := strings.TrimSpace(se.String())
		if i := strings.LastIndex(msg, "\n"); i >= 0 {
			msg = strings.TrimSpace(msg[i+1:])
		}
		return nil, errors.New(cmp.Or(strings.TrimPrefix(msg, "factory: "), err.Error()))
	}
	return so.Bytes(), nil
}

// FactorySettingsPath is the factory's settings file.
func FactorySettingsPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".claude", "factory", "settings.json")
}

// ErrWorktreeCap marks a settings file that is an object but whose worktree_cap the
// factory would refuse.
var ErrWorktreeCap = errors.New("worktree_cap is invalid")

// ShortPath shows the home directory as ~.
func ShortPath(path string) string {
	if home, err := os.UserHomeDir(); err == nil && strings.HasPrefix(path, home+"/") {
		return "~" + path[len(home):]
	}
	return path
}

// shortErr names the settings file with ~ and drops the path the OS error repeats.
func shortErr(path string, err error) error {
	var pe *fs.PathError
	var le *os.LinkError
	switch {
	case errors.As(err, &pe):
		err = pe.Err
	case errors.As(err, &le):
		err = le.Err
	}
	return fmt.Errorf("%s: %w", ShortPath(path), err)
}

// readSettings returns the file's keys with their values as written; a missing file is
// no keys, and a file that is not JSON or not an object is an error naming the path.
func readSettings(path string) (map[string]json.RawMessage, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return map[string]json.RawMessage{}, nil
	}
	if err != nil {
		return nil, shortErr(path, err)
	}
	if !json.Valid(b) {
		return nil, fmt.Errorf("%s is not valid JSON", ShortPath(path))
	}
	var obj map[string]json.RawMessage
	if json.Unmarshal(b, &obj); obj == nil {
		return nil, fmt.Errorf("%s is not a JSON object", ShortPath(path))
	}
	return obj, nil
}

// ReadWorktreeCap is the cap the factory would use: the file's worktree_cap, else the default.
func ReadWorktreeCap(path string) (int, error) {
	obj, err := readSettings(path)
	if err != nil {
		return 0, err
	}
	raw, ok := obj["worktree_cap"]
	if !ok {
		return DefaultWorktreeCap, nil
	}
	n, err := strconv.Atoi(string(raw))
	if err != nil || n < 1 {
		return 0, fmt.Errorf("%s: %w", ShortPath(path), ErrWorktreeCap)
	}
	return n, nil
}

// dispatcherName is what may be passed to `claude attach`: a leading - would be an option.
var dispatcherName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

// ReadDispatchers is the "dispatchers" list of the settings file, in the order written,
// without the names that are not safe as an argument. A missing key, an empty list, a
// value that is not a list of strings and an unreadable file are errors naming the file.
func ReadDispatchers(path string) ([]string, error) {
	obj, err := readSettings(path)
	if err != nil {
		return nil, err
	}
	no := func(why string) error {
		return fmt.Errorf(`no dispatcher: "dispatchers" %s (%s)`, why, ShortPath(path))
	}
	raw, ok := obj["dispatchers"]
	if !ok {
		return nil, no("is not set")
	}
	var all []string
	if err := json.Unmarshal(raw, &all); err != nil {
		return nil, no("is not a list of strings")
	}
	names := slices.DeleteFunc(all, func(n string) bool { return !dispatcherName.MatchString(n) })
	if len(names) == 0 {
		return nil, no("is empty")
	}
	return names, nil
}

// WriteWorktreeCap sets worktree_cap and leaves every other key's value as it was. The
// file is replaced atomically (a symbolic link is followed, so the link stays), keeps its
// permission bits, and is never written when it is not a JSON object. A link to a missing
// file is refused: renaming over it would replace the link.
func WriteWorktreeCap(path string, n int) error {
	obj, err := readSettings(path)
	if err != nil {
		return err
	}
	if p, err := filepath.EvalSymlinks(path); err == nil {
		path = p
	} else if _, lerr := os.Lstat(path); lerr == nil {
		return fmt.Errorf("%s is a symbolic link to a missing file", ShortPath(path))
	}
	obj["worktree_cap"] = json.RawMessage(strconv.Itoa(n))
	keys := make([]string, 0, len(obj))
	for k := range obj {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var out bytes.Buffer
	out.WriteString("{\n")
	for i, k := range keys {
		name, _ := json.Marshal(k)
		fmt.Fprintf(&out, "  %s: %s", name, obj[k])
		if i < len(keys)-1 {
			out.WriteByte(',')
		}
		out.WriteByte('\n')
	}
	out.WriteString("}\n")
	if err := replaceFile(path, out.Bytes()); err != nil {
		return shortErr(path, err)
	}
	return nil
}

// replaceFile writes data to a temporary file beside path and renames it over path.
func replaceFile(path string, data []byte) error {
	mode := os.FileMode(0o644)
	if st, err := os.Stat(path); err == nil {
		mode = st.Mode().Perm()
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".settings-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}
