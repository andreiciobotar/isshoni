package tui

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"charm.land/bubbles/v2/key"
	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

const (
	factoryEvery = 10 * time.Second // from the end of one refresh to the start of the next, while the factory view is shown
	factoryFresh = 5 * time.Second  // entering the view within this of a result starts no refresh
	// factoryIdleEvery is the refresh interval while another view is shown: the tab badge needs the count.
	factoryIdleEvery = 2 * time.Minute
	prStale          = 2 * time.Minute // pull request data older than this is dated under the repository heading
	capDigits        = 3
	capMax           = 999
)

// factoryRepo is one repository with a worktrees directory and what `factory status`
// said about it.
type factoryRepo struct {
	name, path string
	st         *data.FactoryStatus
	err        string // the cached pass failed

	fetching bool   // a fetch pass for this repository is in flight
	fetchErr string // the last fetch pass failed, or reported why GitHub could not be asked; a successful one clears it
}

type factoryState struct {
	repos  []factoryRepo
	busy   bool // a refresh is in flight
	again  bool // asked for another while one was in flight
	doneAt time.Time
	top    int // first visible body line

	cap     int    // worktree cap shown on the settings line; 0 until known
	reserve int    // slots of the cap held in reserve, from status; 0 when unknown
	capErr  string // the settings file cannot be used

	editing  bool
	field    string
	fieldErr string
}

// factoryMsg is the cached pass over every repository.
type factoryMsg struct {
	repos  []factoryRepo
	cap    int
	capErr error
}

// factoryFetchMsg is the fetch pass over one repository.
type factoryFetchMsg struct {
	path string
	st   *data.FactoryStatus
	err  error
}

// factoryPaths lists the repositories of the sessions in this range, by name then path.
func (m *model) factoryPaths() []string {
	var paths []string
	for _, s := range m.sessions {
		if s.RepoPath != "" && !slices.Contains(paths, s.RepoPath) {
			paths = append(paths, s.RepoPath)
		}
	}
	slices.SortFunc(paths, func(a, b string) int {
		if c := strings.Compare(filepath.Base(a), filepath.Base(b)); c != 0 {
			return c
		}
		return strings.Compare(a, b)
	})
	return paths
}

// factoryJob is the cached pass of a refresh: it keeps the repositories that have a
// worktrees directory, reads each of them from the factory's disk cache concurrently, and
// reads the settings file. It never waits on GitHub.
func (m *model) factoryJob() func() factoryMsg {
	r, ctx, paths := m.runner, m.ctx, m.factoryPaths()
	return func() factoryMsg {
		var repos []factoryRepo
		for _, p := range paths {
			if st, err := os.Stat(filepath.Join(p, ".claude", "worktrees")); err == nil && st.IsDir() {
				repos = append(repos, factoryRepo{name: filepath.Base(p), path: p})
			}
		}
		var wg sync.WaitGroup
		for i := range repos {
			wg.Go(func() {
				st, err := r.FactoryStatus(ctx, repos[i].path, false)
				if err != nil {
					repos[i].err = firstLine(err.Error())
					return
				}
				repos[i].st = st
			})
		}
		wg.Wait()
		n, err := data.ReadWorktreeCap(data.FactorySettingsPath())
		return factoryMsg{repos: repos, cap: n, capErr: err}
	}
}

// refreshFactory starts a refresh when none is in flight and the last result is older
// than minAge. Off the factory view it waits factoryIdleEvery whatever minAge is, and
// only once the sessions have loaded. A refresh asked for while one runs follows it.
func (m *model) refreshFactory(minAge time.Duration) tea.Cmd {
	f := &m.fac
	if m.runner == nil || m.fatal != nil {
		return nil
	}
	if f.busy {
		f.again = f.again || minAge == 0
		return nil
	}
	if m.view != viewFactory {
		if !m.loaded {
			return nil
		}
		minAge = factoryIdleEvery
	}
	if !f.doneAt.IsZero() && m.now().Sub(f.doneAt) < minAge {
		return nil
	}
	f.busy = true
	job := m.factoryJob()
	return func() tea.Msg { return job() }
}

// dueFetches marks and returns the fetch passes to start: one per repository with rows
// whose pull request data is missing or older than data.PRFloor and that has none in
// flight. Only the factory view asks GitHub.
func (m *model) dueFetches() []func() factoryFetchMsg {
	if m.view != viewFactory || m.runner == nil {
		return nil
	}
	r, ctx, now := m.runner, m.ctx, float64(m.now().UnixNano())/1e9
	var jobs []func() factoryFetchMsg
	for i := range m.fac.repos {
		repo := &m.fac.repos[i]
		if repo.st == nil || len(repo.st.Rows) == 0 || repo.fetching {
			continue
		}
		if asOf := repo.st.PRs.AsOf; asOf != nil && now-*asOf <= data.PRFloor.Seconds() {
			continue
		}
		repo.fetching = true
		path := repo.path
		jobs = append(jobs, func() factoryFetchMsg {
			st, err := r.FactoryStatus(ctx, path, true)
			return factoryFetchMsg{path: path, st: st, err: err}
		})
	}
	return jobs
}

// applyFetch replaces one repository's rows with the fetch pass's. A failure keeps the
// rows and records why; a success clears the error unless the factory reports one itself.
func (m *model) applyFetch(msg factoryFetchMsg) {
	i := slices.IndexFunc(m.fac.repos, func(r factoryRepo) bool { return r.path == msg.path })
	if i < 0 {
		return
	}
	r := &m.fac.repos[i]
	r.fetching = false
	switch {
	case msg.err != nil:
		r.fetchErr = firstLine(msg.err.Error())
	case msg.st.PRs.Error != nil:
		r.st, r.fetchErr = msg.st, firstLine(*msg.st.PRs.Error)
	default:
		r.st, r.fetchErr = msg.st, ""
	}
	m.fixScroll()
}

// applyFactory takes the cached pass's result whole, except that a repository keeps the
// state of its fetch pass.
func (m *model) applyFactory(msg factoryMsg) tea.Cmd {
	f := &m.fac
	old := f.repos
	f.busy, f.doneAt, f.repos = false, m.now(), msg.repos
	for i := range f.repos {
		if j := slices.IndexFunc(old, func(o factoryRepo) bool { return o.path == f.repos[i].path }); j >= 0 {
			f.repos[i].fetching, f.repos[i].fetchErr = old[j].fetching, old[j].fetchErr
		}
	}
	f.cap, f.reserve, f.capErr = msg.cap, 0, ""
	for _, r := range msg.repos {
		if r.st != nil {
			f.cap, f.reserve = r.st.Cap, r.st.Reserve
			break
		}
	}
	if msg.capErr != nil {
		f.cap, f.capErr = 0, msg.capErr.Error()
	}
	m.fixScroll()
	if f.again {
		f.again = false
		return m.refreshFactory(0)
	}
	return nil
}

// startFetches starts the fetch passes that are due.
func (m *model) startFetches() tea.Cmd {
	var cmds []tea.Cmd
	for _, job := range m.dueFetches() {
		cmds = append(cmds, func() tea.Msg { return job() })
	}
	return tea.Batch(cmds...)
}

// factoryWorking is true while a task's session is working: its spinner turns.
func (m *model) factoryWorking() bool {
	for _, r := range m.fac.repos {
		if r.st == nil {
			continue
		}
		for _, t := range r.st.Rows {
			if t.Phase == "motion" && t.Session == "working" {
				return true
			}
		}
	}
	return false
}

// ---- the cap field -----------------------------------------------------------------

func (m *model) openCap() {
	n, err := data.ReadWorktreeCap(data.FactorySettingsPath())
	switch {
	case errors.Is(err, data.ErrWorktreeCap):
		n = data.DefaultWorktreeCap
	case err != nil:
		m.fac.capErr = err.Error()
		return
	}
	m.fac.editing, m.fac.field, m.fac.fieldErr = true, strconv.Itoa(n), ""
}

// capKey handles every key while the field is open; the caller has taken ctrl+c.
func (m *model) capKey(msg tea.KeyPressMsg) tea.Cmd {
	f := &m.fac
	f.fieldErr = ""
	switch {
	case key.Matches(msg, keys.CapCancel):
		f.editing = false
	case key.Matches(msg, keys.CapSave):
		return m.saveCap()
	case key.Matches(msg, keys.Erase):
		if n := len(f.field); n > 0 {
			f.field = f.field[:n-1]
		}
	case key.Matches(msg, keys.Up):
		m.adjustCap(1)
	case key.Matches(msg, keys.Down):
		m.adjustCap(-1)
	default:
		if t := msg.Key().Text; len(t) == 1 && t[0] >= '0' && t[0] <= '9' && len(f.field) < capDigits {
			f.field += t
		}
	}
	return nil
}

func (m *model) adjustCap(delta int) {
	n, _ := strconv.Atoi(m.fac.field)
	m.fac.field = strconv.Itoa(min(capMax, max(1, n+delta)))
}

func (m *model) saveCap() tea.Cmd {
	f := &m.fac
	n, _ := strconv.Atoi(f.field)
	if n < 1 {
		f.fieldErr = fmt.Sprintf("enter a whole number from 1 to %d", capMax)
		return nil
	}
	if err := data.WriteWorktreeCap(n); err != nil {
		f.fieldErr = err.Error()
		return nil
	}
	if m.runner != nil {
		m.runner.Forget() // other viewers of a shared runner must see the new cap
	}
	f.editing, f.cap, f.capErr = false, n, ""
	m.setToast(fmt.Sprintf("worktree cap set to %d", n), false)
	return m.refreshFactory(0)
}
