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
	"strconv"
	"strings"
	"syscall"
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
	Stale    bool   `json:"stale"`
	PRState  string `json:"pr_state"`
	PRURL    string `json:"pr_url"`
	PRDraft  bool   `json:"pr_draft"` // its pull request is a draft: still being worked on or checks not green
	Session  string `json:"session"`

	Deliverable string `json:"deliverable"` // a link or an absolute file path; empty when null

	Waited      *int64 `json:"waited"`       // seconds since the last declared record; nil when undeclared
	StopKind    string `json:"stop_kind"`    // QUESTION, BLOCKED or OVERTIME for a blocked task; empty when null
	StopSummary string `json:"stop_summary"` // why it stopped; empty when null
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
	cmd := exec.CommandContext(ctx, r.python(), append([]string{script, "status", "--json"}, flag...)...)
	cmd.Dir = repoPath
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	cmd.WaitDelay = time.Second
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
		return nil, stderrLine(se.String(), err)
	}
	return so.Bytes(), nil
}

// FactorySettingsPath is the factory's settings file.
func FactorySettingsPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".claude", "factory", "settings.toml")
}

// ErrWorktreeCap marks a settings file whose worktree_cap the factory would refuse.
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

// legacySettings reads the keys of a settings.json with their values as written; a missing file is no keys.
func legacySettings(path string) (map[string]json.RawMessage, error) {
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

// wrongType is the value of a key that is neither an integer nor a list of strings.
type wrongType struct{}

var tableLine = regexp.MustCompile(`^\[([^\[\]]+)\]\s*(#.*)?$`)

// readSetting is the one reader of the settings file the dashboard has; the factory is the only writer. It finds
// `key` inside `[table]` and returns its value: an int, a []string (an array of basic strings on one line or
// over several, comments and a trailing comma allowed), or wrongType for any other single-line value. found is
// false for no such key. It is not a TOML parser: an array it cannot read is an error naming the file and the line.
// A missing file reads the same key of a legacy settings.json beside it, if there is one.
func readSetting(path, table, key string) (val any, found bool, err error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return readLegacy(filepath.Join(filepath.Dir(path), "settings.json"), key)
	}
	if err != nil {
		return nil, false, shortErr(path, err)
	}
	lines, current := strings.Split(string(b), "\n"), ""
	for i, line := range lines {
		line = strings.TrimSpace(line)
		if m := tableLine.FindStringSubmatch(line); m != nil {
			current = strings.TrimSpace(m[1])
			continue
		}
		name, rest, ok := strings.Cut(line, "=")
		if !ok || current != table || strings.TrimSpace(name) != key {
			continue
		}
		rest = strings.TrimSpace(rest)
		if !strings.HasPrefix(rest, "[") {
			rest, _, _ = strings.Cut(rest, "#")
			if n, err := strconv.Atoi(strings.TrimSpace(rest)); err == nil {
				return n, true, nil
			}
			return wrongType{}, true, nil
		}
		list, at, err := readStrings(lines, i, rest[1:])
		if err != nil {
			return nil, false, fmt.Errorf("%s line %d: %w", ShortPath(path), at+1, err)
		}
		return list, true, nil
	}
	return nil, false, nil
}

// readStrings reads an array of basic strings that began on lines[i], whose text after the `[` is s; on an error
// the second result is the index of the line it is on (for an unclosed list, where it began).
func readStrings(lines []string, i int, s string) ([]string, int, error) {
	out, wantValue, start := []string{}, true, i
	for {
		s = strings.TrimLeft(s, " \t\r")
		switch {
		case s == "" || s[0] == '#':
			if i++; i >= len(lines) {
				return nil, start, errors.New("the list is not closed")
			}
			s = lines[i]
		case s[0] == ']':
			if rest := strings.TrimSpace(s[1:]); rest != "" && rest[0] != '#' {
				return nil, i, errors.New("text after the list")
			}
			return out, i, nil
		case s[0] == ',' && !wantValue:
			wantValue, s = true, s[1:]
		case s[0] == '"' && wantValue:
			quoted, err := strconv.QuotedPrefix(s)
			if err != nil {
				return nil, i, errors.New("a string is not closed")
			}
			v, _ := strconv.Unquote(quoted)
			out, wantValue, s = append(out, v), false, s[len(quoted):]
		default:
			return nil, i, errors.New("the list holds something other than basic strings")
		}
	}
}

// readLegacy is the value of `key` in a settings.json, read as readSetting reads TOML.
func readLegacy(path, key string) (any, bool, error) {
	obj, err := legacySettings(path)
	raw, ok := obj[key]
	if err != nil || !ok {
		return nil, false, err
	}
	var list []string
	if n, err := strconv.Atoi(string(raw)); err == nil {
		return n, true, nil
	} else if string(raw) == "null" {
		return nil, true, nil
	} else if json.Unmarshal(raw, &list) == nil {
		return list, true, nil
	}
	return wrongType{}, true, nil
}

// ReadWorktreeCap is the cap the factory would use: limits.worktree_cap of the file, else the default.
func ReadWorktreeCap(path string) (int, error) {
	v, found, err := readSetting(path, "limits", "worktree_cap")
	if err != nil {
		return 0, err
	}
	if !found {
		return DefaultWorktreeCap, nil
	}
	n, ok := v.(int)
	if !ok || n < 1 {
		return 0, fmt.Errorf("%s: %w", ShortPath(path), ErrWorktreeCap)
	}
	return n, nil
}

// dispatcherName is what may be passed to `claude attach`: a leading - would be an option.
var dispatcherName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

// ReadDispatchers is sessions.dispatchers of the settings file, in the order written,
// without the names that are not safe as an argument. A missing key, an empty list, a
// value that is not a list of strings and an unreadable file are errors naming the file.
func ReadDispatchers(path string) ([]string, error) {
	v, found, err := readSetting(path, "sessions", "dispatchers")
	if err != nil {
		return nil, err
	}
	no := func(why string) error {
		return fmt.Errorf(`no dispatcher: "dispatchers" %s (%s)`, why, ShortPath(path))
	}
	if !found {
		return nil, no("is not set")
	}
	all, ok := v.([]string)
	if !ok && v != nil {
		return nil, no("is not a list of strings")
	}
	names := slices.DeleteFunc(all, func(n string) bool { return !dispatcherName.MatchString(n) })
	if len(names) == 0 {
		return nil, no("is empty")
	}
	return names, nil
}

// WriteWorktreeCap runs `factory settings set limits.worktree_cap n`: the factory is the only writer of its
// settings file. Its one-line complaint is the error.
func WriteWorktreeCap(n int) error {
	var se bytes.Buffer
	cmd := exec.Command("factory", "settings", "set", "limits.worktree_cap", strconv.Itoa(n))
	cmd.Stderr = &se
	if err := cmd.Run(); err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			return errors.New("the factory command is not on PATH")
		}
		return stderrLine(se.String(), err)
	}
	return nil
}

// stderrLine is the last line of what a factory command wrote to stderr without its "factory: " prefix, else err.
func stderrLine(stderr string, err error) error {
	msg := strings.TrimSpace(stderr)
	if i := strings.LastIndex(msg, "\n"); i >= 0 {
		msg = strings.TrimSpace(msg[i+1:])
	}
	return errors.New(cmp.Or(strings.TrimPrefix(msg, "factory: "), err.Error()))
}
