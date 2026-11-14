package data

import (
	"bufio"
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"slices"
	"strings"
	"syscall"
	"time"
)

// Event is one line of `isshoni events`: something that happened in the factory.
type Event struct {
	TS         time.Time `json:"ts"`
	Kind       string    `json:"kind"`
	Session    string    `json:"session"`
	Agent      string    `json:"agent"`
	Parent     string    `json:"parent"`
	Role       string    `json:"role"`
	Tool       string    `json:"tool"`
	Slug       string    `json:"slug"`
	State      string    `json:"state"`
	Verdict    string    `json:"verdict"`
	PR         int       `json:"pr"`
	In         int64     `json:"in"`
	Out        int64     `json:"out"`
	CacheRead  int64     `json:"cache_read"`
	CacheWrite int64     `json:"cache_write"`
}

// ReadEvents reads an events file (JSON lines); a bad line is skipped and the result is sorted by TS (stable).
func ReadEvents(path string) ([]Event, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return collectEvents(f)
}

// collectEvents reads every event of in, sorted by TS (stable).
func collectEvents(in io.Reader) ([]Event, error) {
	var evs []Event
	err := eachEvent(in, func(e Event) { evs = append(evs, e) })
	slices.SortStableFunc(evs, func(a, b Event) int { return a.TS.Compare(b.TS) })
	return evs, err
}

// eachEvent calls fn for every line of in that is an event; it returns the read error, if any.
func eachEvent(in io.Reader, fn func(Event)) error {
	br := bufio.NewReader(in)
	for {
		line, err := br.ReadBytes('\n')
		var e Event
		if json.Unmarshal(line, &e) == nil && !e.TS.IsZero() {
			fn(e)
		}
		if err != nil {
			if err == io.EOF {
				return nil
			}
			return err
		}
	}
}

// eventsCmd is `python3 -m isshoni events <args>` in its own process group, killed as a whole on cancel.
func (r *Runner) eventsCmd(ctx context.Context, args ...string) *exec.Cmd {
	full := append([]string{"-m", "isshoni", "events"}, args...)
	if r.ProjectsDir != "" {
		full = append(full, "--projects-dir", r.ProjectsDir)
	}
	cmd := exec.CommandContext(ctx, r.python(), full...)
	cmd.Dir = r.Root
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	cmd.WaitDelay = time.Second
	return cmd
}

// eventsErr is the error of a finished events process: the last line of its stderr.
func eventsErr(err error, stderr string) error {
	if errors.Is(err, exec.ErrNotFound) {
		return ErrPython
	}
	if strings.Contains(stderr, "No module named isshoni") {
		return ErrPackage
	}
	msg := strings.TrimSpace(stderr)
	if i := strings.LastIndex(msg, "\n"); i >= 0 {
		msg = strings.TrimSpace(msg[i+1:])
	}
	return errors.New(cmp.Or(msg, err.Error()))
}

// Events runs `isshoni events --since <since>` and returns the history.
func (r *Runner) Events(ctx context.Context, since string) ([]Event, error) {
	if !r.begin() {
		return nil, cmp.Or(ctx.Err(), context.Canceled)
	}
	defer r.calls.Done()
	timeout := cmp.Or(r.Timeout, callTimeout)
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := r.eventsCmd(ctx, sinceArgs(since)...)
	var so, se bytes.Buffer
	cmd.Stdout, cmd.Stderr = &so, &se
	if err := cmd.Run(); err != nil {
		switch {
		case errors.Is(ctx.Err(), context.DeadlineExceeded):
			return nil, errors.New("isshoni events timed out after " + timeout.String())
		case ctx.Err() != nil:
			return nil, ctx.Err()
		}
		return nil, eventsErr(err, se.String())
	}
	return collectEvents(&so)
}

// FollowEvents runs `isshoni events --follow [--since <since>]` and calls emit for each event until ctx is
// cancelled or the process ends. It returns nil on cancellation or a clean end, else the process's error.
func (r *Runner) FollowEvents(ctx context.Context, since string, emit func(Event)) error {
	if !r.begin() {
		return cmp.Or(ctx.Err(), context.Canceled)
	}
	defer r.calls.Done()
	cmd := r.eventsCmd(ctx, append([]string{"--follow"}, sinceArgs(since)...)...)
	var se bytes.Buffer
	cmd.Stderr = &se
	out, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	if err := cmd.Start(); err != nil {
		return eventsErr(err, "")
	}
	eachEvent(out, emit)
	err = cmd.Wait()
	if err != nil && ctx.Err() == nil {
		return eventsErr(err, se.String())
	}
	return nil
}
