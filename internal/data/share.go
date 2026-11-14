package data

import (
	"context"
	"time"
)

// shareFor is how long a finished run answers identical calls, and how old a run may be
// and still be waited for.
var shareFor = 10 * time.Second

// entry is one run for one key. out and err are set before done closes.
type entry struct {
	started time.Time
	done    chan struct{}
	out     []byte
	err     error
}

// finished reports whether the run has ended.
func (e *entry) finished() bool {
	select {
	case <-e.done:
		return true
	default:
		return false
	}
}

// Forget makes every run started before now stale: the next call asks again. It does
// nothing without Share.
func (r *Runner) Forget() {
	r.mu.Lock()
	r.forgot = time.Now()
	r.mu.Unlock()
}

// shared returns the output of fn for key. Without Share it is fn under the caller's
// context. With it, callers share one run per key: a run is used while it started after
// the newest of now-shareFor, the last Forget and, when scan is set, the newest
// transcript mtime, and only one run per key is in flight at a time.
func (r *Runner) shared(ctx context.Context, key string, scan bool, fn func(context.Context) ([]byte, error)) ([]byte, error) {
	if r.Share == nil {
		return fn(ctx)
	}
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		now := time.Now()
		need := now.Add(-shareFor)
		if scan {
			seen, err := Scan(r.Projects())
			if err != nil {
				need = now
			}
			for _, t := range seen {
				need = laterOf(need, t)
			}
		}
		r.mu.Lock()
		need = laterOf(need, r.forgot)
		for k, e := range r.entries {
			if e.finished() && now.Sub(e.started) > shareFor {
				delete(r.entries, k)
			}
		}
		e := r.entries[key]
		if e != nil && e.started.After(need) {
			r.mu.Unlock()
			return wait(ctx, e)
		}
		if e != nil && !e.finished() {
			r.mu.Unlock()
			select {
			case <-e.done:
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		e = &entry{started: now, done: make(chan struct{})}
		if r.entries == nil {
			r.entries = map[string]*entry{}
		}
		r.entries[key] = e
		r.mu.Unlock()
		go func() {
			e.out, e.err = fn(r.Share)
			if e.err != nil {
				r.mu.Lock()
				if r.entries[key] == e {
					delete(r.entries, key)
				}
				r.mu.Unlock()
			}
			close(e.done)
		}()
		return wait(ctx, e)
	}
}

// wait returns the result of a run, or the caller's context error if that ends first.
func wait(ctx context.Context, e *entry) ([]byte, error) {
	select {
	case <-e.done:
		return e.out, e.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func laterOf(a, b time.Time) time.Time {
	if b.After(a) {
		return b
	}
	return a
}
