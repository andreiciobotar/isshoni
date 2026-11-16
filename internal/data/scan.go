package data

import (
	"maps"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Scan stats the transcripts under root (<root>/<project>/<sid>.jsonl and
// <root>/<project>/<sid>/subagents/*.jsonl) and returns the newest mtime per session id.
// A session exists while its main transcript does. A project directory that cannot be
// read is skipped; only an unreadable root is an error, and then the result is nil.
func Scan(root string) (map[string]time.Time, error) {
	out := map[string]time.Time{}
	projects, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	for _, p := range projects {
		if p.IsDir() {
			maps.Copy(out, listProject(filepath.Join(root, p.Name())))
		}
	}
	return out, nil
}

// listProject is the newest mtime per session id in one project directory; nil when it
// cannot be read.
func listProject(pd string) map[string]time.Time {
	ents, err := os.ReadDir(pd)
	if err != nil {
		return nil
	}
	out := map[string]time.Time{}
	for _, e := range ents {
		if id, ok := strings.CutSuffix(e.Name(), ".jsonl"); ok && !e.IsDir() {
			if fi, err := e.Info(); err == nil {
				out[id] = fi.ModTime()
			}
		}
	}
	for _, e := range ents {
		if t, ok := out[e.Name()]; ok && e.IsDir() {
			out[e.Name()] = newest(filepath.Join(pd, e.Name(), "subagents"), t)
		}
	}
	return out
}

const (
	hotFor     = 10 * time.Minute // a session touched this recently is statted every scan
	sweepEvery = 10 * time.Second // a full scan at least this often catches what the rest misses
)

// Scanner is Scan for a poll: the same result, with less work between sweeps. Every call
// reads the root and stats each project directory; a project that is new or whose directory
// mtime moved (an entry was created or removed) is listed in full, and in the others only
// the sessions touched within hotFor are statted again. A cold session's append is therefore
// seen at the next sweep, at most sweepEvery later. The zero value is ready; it is not safe
// for concurrent use.
type Scanner struct {
	root    string
	swept   time.Time
	project map[string]*scanned
}

type scanned struct {
	mtime time.Time // of the project directory when it was listed
	times map[string]time.Time
}

// Scan returns a fresh map. now is the clock for hotFor and sweepEvery; a different root
// starts over.
func (s *Scanner) Scan(root string, now time.Time) (map[string]time.Time, error) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	if root != s.root {
		*s = Scanner{root: root}
	}
	sweep := s.swept.IsZero() || now.Sub(s.swept) >= sweepEvery
	if sweep {
		s.swept = now
	}
	next := map[string]*scanned{}
	out := map[string]time.Time{}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		pd := filepath.Join(root, e.Name())
		fi, err := os.Stat(pd)
		if err != nil {
			continue
		}
		p := s.project[e.Name()]
		if sweep || p == nil || !p.mtime.Equal(fi.ModTime()) {
			p = &scanned{mtime: fi.ModTime(), times: listProject(pd)}
		} else {
			p.restat(pd, now)
		}
		next[e.Name()] = p
		maps.Copy(out, p.times)
	}
	s.project = next
	return out, nil
}

// restat stats again the hot sessions of an unchanged project directory and drops those
// whose main transcript is gone.
func (p *scanned) restat(pd string, now time.Time) {
	for id, t := range p.times {
		if now.Sub(t) >= hotFor {
			continue
		}
		fi, err := os.Stat(filepath.Join(pd, id+".jsonl"))
		if err != nil {
			delete(p.times, id)
			continue
		}
		p.times[id] = newest(filepath.Join(pd, id, "subagents"), fi.ModTime())
	}
}

// newest is the latest mtime among the .jsonl files in dir, or t.
func newest(dir string, t time.Time) time.Time {
	ents, _ := os.ReadDir(dir)
	for _, e := range ents {
		if strings.HasSuffix(e.Name(), ".jsonl") {
			if fi, err := e.Info(); err == nil && fi.ModTime().After(t) {
				t = fi.ModTime()
			}
		}
	}
	return t
}
