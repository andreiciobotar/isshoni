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

	Paused      *FactoryFlag `json:"paused"`      // the site manager paused this blocked task; nil when not, or from a script before the field
	Prioritized *FactoryFlag `json:"prioritized"` // the queue prioritizes this task: in motion, in review, or a still-blocked task just released; nil likewise

	Extensions []FactoryExtension `json:"extensions"` // limit extensions granted, oldest first; Limit already includes them; nil from a script before the field

	Kept string       `json:"kept"` // why cleanup keeps a task whose work is over; empty when null
	Held *FactoryHeld `json:"held"` // the site manager's standing call on it; nil when none, or from a script before the field

	InLine string `json:"in_line"` // why its pull request waits its turn, "waiting behind #12: conflicts with main in a.txt"; empty when null, or from a script before the field
}

// FactoryExtension is one grant of more time on a task's limit.
type FactoryExtension struct {
	Minutes int    `json:"minutes"`
	By      string `json:"by"`
	Reason  string `json:"reason"`
	Decided string `json:"decided"` // timestamp as the script wrote it; not shown
}

// FactoryFlag is a paused or prioritized mark. Since is the zero time when the script's
// timestamp does not parse: the flag then shows without an age. WaitsOn is the paused
// task's waited slugs, nil for a prioritized one.
type FactoryFlag struct {
	Since   time.Time
	WaitsOn []string
}

// UnmarshalJSON reads {"since": "2026-11-14T19:00:00Z", "waits_on": ["a", "b"]}; a bad
// since leaves Since zero rather than failing the status.
func (f *FactoryFlag) UnmarshalJSON(b []byte) error {
	var raw struct {
		Since   string   `json:"since"`
		WaitsOn []string `json:"waits_on"`
	}
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	since, _ := time.Parse(time.RFC3339, raw.Since)
	*f = FactoryFlag{Since: since, WaitsOn: raw.WaitsOn}
	return nil
}

// FactoryHeld is the site manager's newest uncleared call that a task stays as it is.
// Since is the zero time when the script's timestamp does not parse.
type FactoryHeld struct {
	Criterion string
	Decision  string // suppressed or escalated
	Since     time.Time
	Reason    string
}

// UnmarshalJSON reads {"criterion": "...", "decision": "...", "since": "2026-11-14T19:00:00Z",
// "reason": "..."}; a bad since leaves Since zero rather than failing the status.
func (h *FactoryHeld) UnmarshalJSON(b []byte) error {
	var raw struct{ Criterion, Decision, Since, Reason string }
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	since, _ := time.Parse(time.RFC3339, raw.Since)
	*h = FactoryHeld{Criterion: raw.Criterion, Decision: raw.Decision, Since: since, Reason: raw.Reason}
	return nil
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

	Decisions []FactoryDecision `json:"decisions"` // open decisions of the repository, oldest first; nil from a script before the field
}

// FactoryDecision is an open decision a lead posted for the human: the ask and its numbered options.
// Recommend is the 1-based option the lead would take, 0 when it named none. TS is the zero time when
// the script's timestamp does not parse: the decision then shows without an age. With is "site-manager" while the
// human has left it there (WithSince is when, zero when it does not parse); Returned is the reason the site
// manager handed it back.
type FactoryDecision struct {
	ID        string
	Slug      string
	Kind      string // question or blocked
	Ask       string
	Options   []string
	Recommend int
	TS        time.Time
	With      string
	WithSince time.Time
	Returned  string
}

// UnmarshalJSON reads {"id": "3f9a1c2b4d5e", "slug": "demo-task", "kind": "question", "ask": "...",
// "options": ["a", "b"], "recommend": 1, "ts": "2026-11-14T19:00:00Z", "with": "site-manager",
// "with_since": "2026-11-14T19:30:00Z", "returned": "why"}; recommend, with, with_since and returned may be
// absent and a bad ts or with_since leaves the time zero rather than failing the status.
func (d *FactoryDecision) UnmarshalJSON(b []byte) error {
	var raw struct {
		ID, Slug, Kind, Ask string
		Options             []string
		Recommend           int
		TS, With            string
		WithSince           string `json:"with_since"`
		Returned            string
	}
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	ts, _ := time.Parse(time.RFC3339, raw.TS)
	since, _ := time.Parse(time.RFC3339, raw.WithSince)
	*d = FactoryDecision{ID: raw.ID, Slug: raw.Slug, Kind: raw.Kind, Ask: raw.Ask, Options: raw.Options, Recommend: raw.Recommend, TS: ts,
		With: raw.With, WithSince: since, Returned: raw.Returned}
	return nil
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
	script, err := r.factoryScript()
	if err != nil {
		return nil, err
	}
	b, err := r.shared(ctx, "factory\x00"+strconv.FormatBool(fetch)+"\x00"+repoPath, false, func(ctx context.Context) ([]byte, error) {
		timeout, flag := cachedTimeout, []string{"--cached"}
		if fetch {
			timeout, flag = fetchTimeout, []string{"--max-age", strconv.Itoa(int(PRFloor.Seconds()))}
		}
		return r.factoryOutput(ctx, "factory status", repoPath, cmp.Or(r.Timeout, timeout), append([]string{script, "status", "--json"}, flag...)...)
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

// factoryScript is the factory command of the checkout the runner reads.
func (r *Runner) factoryScript() (string, error) {
	script, err := filepath.Abs(filepath.Join(r.Root, "factory", "bin", "factory"))
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(script); err != nil {
		return "", fmt.Errorf("factory command not found: %s", script)
	}
	return script, nil
}

// factoryOutput is one factory process, `python argv...` run in dir, and what it printed; what names
// it in the timeout message. argv[1] is the subcommand: a factory that does not know it (argparse
// exits 2 with `invalid choice`) is "this factory has no <subcommand> command yet".
func (r *Runner) factoryOutput(ctx context.Context, what, dir string, timeout time.Duration, argv ...string) ([]byte, error) {
	if !r.begin() {
		return nil, cmp.Or(ctx.Err(), context.Canceled)
	}
	defer r.calls.Done()
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := groupCmd(ctx, r.python(), argv...)
	cmd.Dir = dir
	var so, se bytes.Buffer
	cmd.Stdout, cmd.Stderr = &so, &se
	if err := cmd.Run(); err != nil {
		var exit *exec.ExitError
		switch {
		case errors.Is(err, exec.ErrNotFound):
			return nil, ErrPython
		case errors.Is(ctx.Err(), context.DeadlineExceeded):
			return nil, fmt.Errorf("%s timed out after %s", what, timeout)
		case ctx.Err() != nil:
			return nil, ctx.Err()
		case errors.As(err, &exit) && exit.ExitCode() == 2 && strings.Contains(se.String(), "invalid choice: '"+argv[1]+"'"):
			return nil, fmt.Errorf("this factory has no %s command yet", argv[1])
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

var (
	tableLine = regexp.MustCompile(`^\[\s*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\s*\](?:\s*#.*)?$`)
	bareKey   = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
)

// scanLine is the number of `[` minus the number of `]` in a line outside its strings and comment (a basic string
// has backslash escapes, a literal string none), and whether a `"""` or `”'` opens outside a string: a
// multi-line string, which this reader does not follow.
func scanLine(s string) (delta int, triple bool) {
	var quote byte
	for i := 0; i < len(s); i++ {
		switch c := s[i]; {
		case quote == '"' && c == '\\':
			i++
		case quote != 0:
			if c == quote {
				quote = 0
			}
		case c == '"' || c == '\'':
			if strings.HasPrefix(s[i:], strings.Repeat(string(c), 3)) {
				return delta, true
			}
			quote = c
		case c == '#':
			return delta, false
		case c == '[':
			delta++
		case c == ']':
			delta--
		}
	}
	return delta, false
}

// readSetting is the one reader of the settings file the dashboard has; the factory is the only writer. It finds
// `key` inside `[table]` and returns its value: an int, a []string (an array of basic strings on one line or
// over several, comments and a trailing comma allowed), or wrongType for any other single-line value. found is
// false for no such key. It is not a TOML parser: a line it cannot classify (a header that is not [bare.name], a key
// that is not a bare name, a value starting `{`, a `"""` or `”'` anywhere, a list it cannot read) is an error naming the file and the line.
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
	unreadable := func(i int, what string) error {
		return fmt.Errorf("%s line %d: %s", ShortPath(path), i+1, what)
	}
	for i := 0; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" || line[0] == '#' {
			continue
		}
		if line[0] == '[' {
			m := tableLine.FindStringSubmatch(line)
			if m == nil {
				return nil, false, unreadable(i, "a table header other than [name] or [name.name]")
			}
			current = m[1]
			continue
		}
		name, rest, ok := strings.Cut(line, "=")
		name, rest = strings.TrimSpace(name), strings.TrimSpace(rest)
		switch {
		case !ok || !bareKey.MatchString(name):
			return nil, false, unreadable(i, "a line that is not a table header, a comment or a plain name = value")
		case strings.HasPrefix(rest, "{"):
			return nil, false, unreadable(i, "an inline table")
		}
		if _, triple := scanLine(line); triple {
			return nil, false, unreadable(i, "a multi-line string")
		}
		if current != table || name != key {
			// another key: a list over several lines is skipped whole
			start := i
			depth, _ := scanLine(rest)
			for depth > 0 {
				if i++; i >= len(lines) {
					return nil, false, unreadable(start, "the list is not closed")
				}
				d, triple := scanLine(lines[i])
				if triple {
					return nil, false, unreadable(i, "a multi-line string")
				}
				depth += d
			}
			continue
		}
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

// WriteWorktreeCap runs `<root>/factory/bin/factory settings set limits.worktree_cap n`, root being the checkout
// the dashboard runs from (LocateRoot): the factory is the only writer of its settings file, and `factory` is on
// the PATH only inside Claude Code sessions. Its one-line complaint is the error.
func WriteWorktreeCap(n int) error {
	root, err := LocateRoot()
	if err != nil {
		return err
	}
	script := filepath.Join(root, "factory", "bin", "factory")
	if _, err := os.Stat(script); err != nil {
		return fmt.Errorf("factory command not found: %s", script)
	}
	var se bytes.Buffer
	cmd := exec.Command(script, "settings", "set", "limits.worktree_cap", strconv.Itoa(n))
	cmd.Stderr = &se
	if err := cmd.Run(); err != nil {
		return stderrLine(se.String(), err)
	}
	return nil
}

// answerTimeout bounds `factory decision answer`, which writes two log lines and never leaves the machine.
const answerTimeout = 10 * time.Second

// AnswerDecision runs `<root>/factory/bin/factory decision answer id choice` in the repository at repoPath (root as
// for WriteWorktreeCap); choice is what the script takes: an option number, "lead" or "site-manager". The factory
// queues the human's answer. Its one-line complaint is the error.
func AnswerDecision(ctx context.Context, repoPath, id, choice string) error {
	root, err := LocateRoot()
	if err != nil {
		return err
	}
	script := filepath.Join(root, "factory", "bin", "factory")
	if _, err := os.Stat(script); err != nil {
		return fmt.Errorf("factory command not found: %s", script)
	}
	ctx, cancel := context.WithTimeout(ctx, answerTimeout)
	defer cancel()
	var se bytes.Buffer
	cmd := groupCmd(ctx, script, "decision", "answer", id, choice)
	cmd.Dir, cmd.Stderr = repoPath, &se
	if err := cmd.Run(); err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return fmt.Errorf("answering timed out after %s", answerTimeout)
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
