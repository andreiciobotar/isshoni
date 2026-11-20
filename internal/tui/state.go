package tui

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"charm.land/bubbles/v2/key"
	tea "charm.land/bubbletea/v2"

	"isshoni/internal/data"
)

type viewID int

const (
	viewInbox viewID = iota // the start view
	viewDashboard
	viewAgents
	viewFactory
	viewKaizen
	viewOps
	viewHanami
)

type sortMode int

const (
	sortRecent sortMode = iota
	sortCost
	sortShare
)

func (s sortMode) String() string {
	return [...]string{"recent", "cost", "subagent share"}[s]
}

// sortLabel is the sort as the subheader names it.
func (m *model) sortLabel() string {
	if m.holding() {
		return m.sortBy.String() + ", held"
	}
	return m.sortBy.String()
}

type rangeOpt struct{ label, since string }

var ranges = []rangeOpt{{"24h", "24h"}, {"7d", "7d"}, {"30d", "30d"}, {"all", ""}}

const (
	liveWindow  = 30 * time.Second
	agentsEvery = 20 * time.Second
	flashFor    = 1500 * time.Millisecond
	toastFor    = 4 * time.Second
	tickFast    = 80 * time.Millisecond
	tickSlow    = 400 * time.Millisecond
	tickIdle    = time.Second // redraw interval when nothing moves
	pollEvery   = time.Second // transcript mtime poll interval
	minWidth    = 60
	minHeight   = 16

	detailDebounce = 150 * time.Millisecond // how long the cursor rests on a row before its dispatches are fetched
)

// Options configures a model.
type Options struct {
	Runner  *data.Runner
	Ctx     context.Context // cancelled on quit; every python call runs under it
	Range   string          // initial range label, default 7d
	NoColor bool
	Follow  bool
	Static  bool // render settled (snapshots, tests): no animation phase, no clock
	// ReadOnly refuses the one write the dashboard makes, the worktree cap.
	ReadOnly bool
	// Attach builds the command that gives this terminal to the dispatcher's conversation, run
	// by key d until it returns; nil where nothing can be attached (the local dashboard, a
	// snapshot, a read-only connection), and then d is not in the help.
	Attach func(name string) tea.ExecCommand
	Now    func() time.Time

	Replay      []data.Event // start in the Hanami tab replaying these, not the live stream
	ReplaySpeed float64      // event seconds per second; 0 is the default, a minute a second
}

type toast struct {
	text  string
	err   bool
	until time.Time
}

type model struct {
	opts   Options
	th     *Theme
	runner *data.Runner
	scan   *data.Scanner // the poll's; a pointer so copies of the model share it
	now    func() time.Time
	w, h   int

	ctx        context.Context // lives until quit
	loadCtx    context.Context // lives until the next load replaces it
	loadCancel context.CancelFunc

	view     viewID
	help     bool
	rangeIdx int
	sortBy   sortMode
	follow   bool
	filter   string
	filterIn bool // typing in the filter box

	sessions map[string]*data.Session // list entries: summaries, no dispatches
	details  map[string]*data.Session // per-session results with dispatches
	agents   *data.Agents
	prices   data.Prices

	selID      string          // key of the row under the dashboard cursor: a session id, or a group row's key
	folded     map[string]bool // keys of the repository rows collapsed to one row
	opened     map[string]bool // keys of the short-runs rows that are open
	lastCur    int             // table index of the cursor row when it was last found: where the cursor lands if its row vanishes
	top        int             // first visible table row
	zoom       bool            // dashboard only: the session row under the cursor shown full screen
	dcursor    int             // dispatch cursor in the zoom
	dtop       int
	detailOnly bool // zoom, narrow terminals: enter swaps the list for the detail pane
	selGen     int  // a selMsg carrying an older generation was superseded
	// held is the cost, subagent cost and activity the sort reads for each session, so the tree
	// does not reshuffle under a cursor that is on a row.
	held    map[string]rankKey
	acursor int
	atop    int
	fac     factoryState
	inbox   inboxState
	kz      kaizenState
	ops     opsState
	hn      hanamiState

	loading     bool
	loadGen     int
	fatal       error // python or package missing: nothing can load
	refreshErr  string
	lastUpdate  time.Time
	agentsBusy  bool
	agentsDirty bool
	agentsAt    time.Time // when the agents aggregate last finished, ok or not

	// seen is the newest transcript mtime per session id as of the last scan; nil until
	// the first load has finished, which is also when polling starts.
	seen     map[string]time.Time
	loaded   bool   // the first load has finished, whatever its outcome
	scanErr  string // the last scan failed (the projects root cannot be read): data is stale
	scanning bool
	polledAt time.Time
	// retry: per-session refreshes that failed, with when; tried again every agentsEvery.
	retry map[string]time.Time
	// dirty: changed while its refresh was in flight. inflight: a detail fetch is running.
	dirty    map[string]bool
	inflight map[string]bool

	counter map[string]*Counter
	rowTok  map[string]*Counter
	flash   map[string]time.Time
	toast   toast

	pick    []string // the dispatchers on offer while the picker is open
	pickCur int

	tickOn   bool
	tickGen  int           // a tick carrying an older generation was superseded
	tickD    time.Duration // delay of the pending tick
	tickFrom time.Time     // when the pending tick was scheduled
}

// New builds the model.
func New(o Options) *model {
	if o.Range == "" {
		o.Range = "7d"
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	if o.Ctx == nil {
		o.Ctx = context.Background()
	}
	idx := 1
	for i, r := range ranges {
		if r.label == o.Range {
			idx = i
		}
	}
	m := &model{
		opts: o, th: NewTheme(o.NoColor), runner: o.Runner, scan: &data.Scanner{}, now: o.Now, ctx: o.Ctx,
		w: 100, h: 30, rangeIdx: idx, follow: o.Follow, loading: true,
		sessions: map[string]*data.Session{}, details: map[string]*data.Session{},
		dirty: map[string]bool{}, inflight: map[string]bool{}, retry: map[string]time.Time{}, counter: map[string]*Counter{},
		rowTok: map[string]*Counter{}, flash: map[string]time.Time{}, folded: map[string]bool{}, opened: map[string]bool{}, held: map[string]rankKey{},
	}
	m.loadCtx, m.loadCancel = context.WithCancel(m.ctx)
	for _, k := range append([]string{"tokens", "dispatches", "repeats", "loops", "warns", "share", "recheck"}, kaizenCounters...) {
		m.counter[k] = &Counter{}
	}
	if o.Runner != nil {
		m.fatal = o.Runner.Check()
	}
	if o.Replay != nil {
		m.view = viewHanami
		m.startReplay(o.Replay, o.ReplaySpeed)
	}
	return m
}

// ---- messages ----------------------------------------------------------------

type sessionsMsg struct {
	gen     int
	res     *data.SessionsResult
	seen    map[string]time.Time // mtimes taken before the read
	err     error
	scanErr error // the scan behind seen failed
}
type agentsMsg struct {
	gen int
	res *data.Agents
	err error
}
type detailMsg struct {
	id  string
	at  time.Time // mtime known when the read started
	res *data.Session
	err error
}
type scanMsg struct {
	seen map[string]time.Time
	err  error
}
type tickMsg struct{ gen int }

// selMsg fires when the cursor has rested on a row for detailDebounce.
type selMsg struct{ gen int }

// ---- commands ----------------------------------------------------------------

// reload starts a full load for the current range, cancelling the one it replaces.
func (m *model) reload() tea.Cmd {
	if m.runner == nil || m.fatal != nil {
		return nil
	}
	m.loadCancel()
	m.loadCtx, m.loadCancel = context.WithCancel(m.ctx)
	m.loadGen++
	m.loading = true
	if len(m.sessions) == 0 {
		m.refreshErr = ""
	}
	m.agentsBusy = false
	return tea.Batch(m.loadSessionsCmd(), m.loadAgentsCmd())
}

func (m *model) loadSessionsCmd() tea.Cmd {
	r, ctx, since, gen := m.runner, m.loadCtx, ranges[m.rangeIdx].since, m.loadGen
	return func() tea.Msg {
		// stat first: anything written after this shows up as a change in the poll
		seen, scanErr := data.Scan(r.Projects())
		res, err := r.Sessions(ctx, since)
		if err == nil {
			for i := range res.Sessions {
				res.Sessions[i].LastChange = seen[res.Sessions[i].ID]
			}
		}
		return sessionsMsg{gen: gen, res: res, seen: seen, err: err, scanErr: scanErr}
	}
}

func (m *model) loadAgentsCmd() tea.Cmd {
	m.agentsBusy = true
	r, ctx, since, gen := m.runner, m.loadCtx, ranges[m.rangeIdx].since, m.loadGen
	return func() tea.Msg {
		res, err := r.Agents(ctx, since)
		return agentsMsg{gen: gen, res: res, err: err}
	}
}

// fetchSession is the only way a detail fetch starts. A fetch already running for the
// session is not doubled: the session is marked dirty and fetched once more when the
// running one returns.
func (m *model) fetchSession(id string) tea.Cmd {
	if m.runner == nil || m.fatal != nil {
		return nil
	}
	if m.inflight[id] {
		m.dirty[id] = true
		return nil
	}
	m.inflight[id] = true
	delete(m.dirty, id)
	delete(m.retry, id)
	r, ctx, at := m.runner, m.ctx, m.seen[id]
	return func() tea.Msg {
		res, err := r.Session(ctx, id)
		return detailMsg{id: id, at: at, res: res, err: err}
	}
}

func (m *model) scanCmd() tea.Cmd {
	m.scanning = true
	root, sc, now := m.runner.Projects(), m.scan, m.now()
	return func() tea.Msg {
		seen, err := sc.Scan(root, now)
		return scanMsg{seen: seen, err: err}
	}
}

// ensureTick keeps one clock running at the pace the screen needs. A pending tick that
// is slower than needed (an animation just started during idle) is superseded.
func (m *model) ensureTick() tea.Cmd {
	if m.opts.Static {
		return nil
	}
	d := tickIdle
	switch {
	case m.view == viewHanami && !m.help:
		d = hanamiDT
	case m.animating():
		d = tickFast
	case m.pulsing():
		d = tickSlow
	}
	if m.tickOn && m.tickD <= d {
		return nil
	}
	m.tickOn, m.tickD, m.tickFrom = true, d, m.now()
	m.tickGen++
	gen := m.tickGen
	return tea.Tick(d, func(time.Time) tea.Msg { return tickMsg{gen} })
}

// Init starts the first load and the clock.
func (m *model) Init() tea.Cmd {
	return tea.Batch(m.reload(), m.ensureTick())
}

// ---- derived state -------------------------------------------------------------

// activity is when the session last moved: its newest transcript write or its end.
func (m *model) activity(s *data.Session) time.Time {
	t := m.seen[s.ID]
	if s.End != nil && s.End.After(t) {
		t = *s.End
	}
	return t
}

func (m *model) isLive(s *data.Session) bool {
	return m.now().Sub(m.activity(s)) < liveWindow
}

// A short run is a finished session under a minute and under a quarter dollar. Across a real set of
// sessions both limits sit in a gap: under $0.25 the durations jump from seconds to minutes, under a minute
// the costs jump from cents to tens of cents. Probe and housekeeping runs fall below, real work above.
// The session under zoom is never one: it stays a row of its own while it is shown full screen.
const (
	shortRunSpan = time.Minute
	shortRunCost = 0.25 // dollars
)

func (m *model) shortRun(s *data.Session) bool {
	return s.Start != nil && s.End != nil && s.End.Sub(*s.Start) < shortRunSpan && s.Cost.Total < shortRunCost && !m.isLive(s) && !(m.zoom && m.selID == s.ID)
}

// splitShort takes the short runs out of a container's sessions when there are two or more of them.
func (m *model) splitShort(ss []*data.Session) (rest, short []*data.Session) {
	for _, s := range ss {
		if m.shortRun(s) {
			short = append(short, s)
		} else {
			rest = append(rest, s)
		}
	}
	if len(short) < 2 {
		return ss, nil
	}
	return rest, short
}

func (m *model) liveCount() int {
	n := 0
	for _, s := range m.sessions {
		if m.isLive(s) {
			n++
		}
	}
	return n
}

type rowKind int

const (
	kindRepo rowKind = iota
	kindTask
	kindSession
	kindAll
	kindShort // the short runs of one task or repository, closed to one row
)

// treeRow is one display row of the sessions table: all sessions, a repository, a task, a session or short runs.
// A group of exactly one session is merged into that session's row: s is set, the kind stays the group's.
type treeRow struct {
	kind   rowKind
	key    string          // what selID holds while the cursor is here: the session id when s is set, else a group key that is never an id
	s      *data.Session   // set on session rows and merged rows only: the rows that enter opens full screen
	name   string          // repository, task or 8-character session id; "N short runs" on a short-runs row
	of     string          // short-runs row: the task or repository they ran in
	suffix string          // faint, after the name: ×N sessions of a group, the id of a merged row
	depth  int             // indent in steps of two cells
	group  []*data.Session // what the row totals; a session row holds just itself
	folded bool            // a repository or short-runs row collapsed to this row: what is under it is not shown
}

// holds says the cursor is on this row while selID is key: the row's own key, or a session folded into it.
func (r treeRow) holds(key string) bool {
	return r.key == key || r.folded && slices.ContainsFunc(r.group, func(s *data.Session) bool { return s.ID == key })
}

// groupKey is the key of a group row; the NUL bytes keep it from ever equalling a session id.
func groupKey(parts ...string) string { return "\x00" + strings.Join(parts, "\x00") }

// rankKey is what a sort mode compares: a session or the sum of a group.
type rankKey struct {
	cost, sub float64
	act       time.Time
}

type unit struct {
	group     []*data.Session
	name, tie string
	key       rankKey
}

func (m *model) unit(group []*data.Session, name, tie string) unit {
	u := unit{group: group, name: name, tie: tie}
	for _, s := range group {
		k, ok := m.held[s.ID]
		if !ok {
			k = m.liveRank(s)
		}
		u.key.cost += k.cost
		u.key.sub += k.sub
		if k.act.After(u.key.act) {
			u.key.act = k.act
		}
	}
	return u
}

func (m *model) liveRank(s *data.Session) rankKey {
	return rankKey{s.Cost.Total, s.Cost.Subagents, m.activity(s)}
}

// holding is true while the cursor is on a row other than all: the order of the tree is held.
func (m *model) holding() bool { return m.selID != "" && m.selID != groupKey("all") }

// syncOrder brings the held sort values up to date: every session's when force is set or the cursor
// is on the all row (or nowhere), else only those of sessions not held yet. Sessions that are gone are dropped.
func (m *model) syncOrder(force bool) {
	for id, s := range m.sessions {
		if _, ok := m.held[id]; force || !m.holding() || !ok {
			m.held[id] = m.liveRank(s)
		}
	}
	for id := range m.held {
		if m.sessions[id] == nil {
			delete(m.held, id)
		}
	}
}

// sortUnits orders units by the sort mode, then latest activity, then name.
func (m *model) sortUnits(us []unit) {
	share := func(k rankKey) float64 {
		if k.cost == 0 {
			return 0
		}
		return k.sub / k.cost
	}
	sort.Slice(us, func(i, j int) bool {
		a, b := us[i], us[j]
		switch m.sortBy {
		case sortCost:
			if a.key.cost != b.key.cost {
				return a.key.cost > b.key.cost
			}
		case sortShare:
			if sa, sb := share(a.key), share(b.key); sa != sb {
				return sa > sb
			}
		}
		if !a.key.act.Equal(b.key.act) {
			return a.key.act.After(b.key.act)
		}
		if a.name != b.name {
			return a.name < b.name
		}
		return a.tie < b.tie
	})
}

// sessionUnits makes one unit per session, named by its id.
func (m *model) sessionUnits(ss []*data.Session) []unit {
	us := make([]unit, len(ss))
	for i, s := range ss {
		us[i] = m.unit([]*data.Session{s}, s.ID, "")
	}
	m.sortUnits(us)
	return us
}

// rows is the sessions table as shown: the tree without what is under a folded repository or a closed
// short-runs row.
func (m *model) rows() []treeRow {
	var out []treeRow
	hide := -1 // depth of the folded row whose rows are skipped; -1 when none
	for _, r := range m.tree() {
		if hide >= 0 && r.depth > hide {
			continue
		}
		switch r.kind {
		case kindRepo:
			r.folded = m.folded[r.key]
		case kindShort:
			r.folded = !m.opened[r.key]
		}
		hide = -1
		if r.folded {
			hide = r.depth
		}
		out = append(out, r)
	}
	return out
}

// shortRow is a container's short runs: one row at depth, then the sessions one step deeper.
func (m *model) shortRow(short []*data.Session, path, repo, task string, depth int) []treeRow {
	of := cmp.Or(task, repo)
	key := groupKey("short", path, repo, task)
	out := []treeRow{{kind: kindShort, key: key, name: fmt.Sprintf("%d short runs", len(short)), of: of, depth: depth, group: short}}
	for _, u := range m.sessionUnits(short) {
		out = append(out, treeRow{kind: kindSession, key: u.group[0].ID, s: u.group[0], name: short8(u.name), depth: depth + 1, group: u.group})
	}
	return out
}

// tree is the filtered, sorted sessions table, the one owner of display order:
// repositories, each with its tasks and its sessions without a task in one sorted list.
func (m *model) tree() []treeRow {
	f := strings.ToLower(strings.TrimSpace(m.filter))
	type repo struct {
		name, path string
		bare       []*data.Session
		tasks      map[string][]*data.Session
		all        []*data.Session
	}
	byRepo := map[[2]string]*repo{}
	for _, s := range m.sessions {
		if f != "" && !strings.Contains(strings.ToLower(s.Label()+" "+s.Project+" "+s.ID), f) {
			continue
		}
		k := [2]string{s.RepoPath, s.Repo}
		r := byRepo[k]
		if r == nil {
			r = &repo{name: s.Repo, path: s.RepoPath, tasks: map[string][]*data.Session{}}
			byRepo[k] = r
		}
		r.all = append(r.all, s)
		if s.Task == "" {
			r.bare = append(r.bare, s)
		} else {
			r.tasks[s.Task] = append(r.tasks[s.Task], s)
		}
	}
	var repos []unit
	for _, r := range byRepo {
		repos = append(repos, m.unit(r.all, r.name, r.path))
	}
	m.sortUnits(repos)
	var out []treeRow
	var shown []*data.Session
	for _, ru := range repos {
		r := byRepo[[2]string{ru.tie, ru.name}]
		shown = append(shown, r.all...)
		repoKey := groupKey("repo", r.path, r.name)
		if len(r.all) == 1 && len(r.bare) == 1 {
			out = append(out, treeRow{kind: kindRepo, key: r.all[0].ID, s: r.all[0], name: r.name, suffix: short8(r.all[0].ID), group: ru.group})
			continue
		}
		out = append(out, treeRow{kind: kindRepo, key: repoKey, name: r.name, suffix: fmt.Sprintf("×%d", len(ru.group)), group: ru.group})
		var units []unit
		bare, bareShort := m.splitShort(r.bare)
		for _, s := range bare {
			units = append(units, m.unit([]*data.Session{s}, s.ID, "session"))
		}
		for name, ss := range r.tasks {
			units = append(units, m.unit(ss, name, "task"))
		}
		m.sortUnits(units)
		for _, tu := range units {
			if tu.tie == "session" {
				out = append(out, treeRow{kind: kindSession, key: tu.group[0].ID, s: tu.group[0], name: short8(tu.name), depth: 1, group: tu.group})
				continue
			}
			if len(tu.group) == 1 {
				out = append(out, treeRow{kind: kindTask, key: tu.group[0].ID, s: tu.group[0], name: tu.name, suffix: short8(tu.group[0].ID), depth: 1, group: tu.group})
				continue
			}
			out = append(out, treeRow{kind: kindTask, key: groupKey("task", r.path, r.name, tu.name), name: tu.name, suffix: fmt.Sprintf("×%d", len(tu.group)), depth: 1, group: tu.group})
			rest, short := m.splitShort(tu.group)
			for _, u := range m.sessionUnits(rest) {
				out = append(out, treeRow{kind: kindSession, key: u.group[0].ID, s: u.group[0], name: short8(u.name), depth: 2, group: u.group})
			}
			if short != nil {
				out = append(out, m.shortRow(short, r.path, r.name, tu.name, 2)...)
			}
		}
		if bareShort != nil {
			out = append(out, m.shortRow(bareShort, r.path, r.name, "", 1)...)
		}
	}
	if len(shown) == 0 {
		return nil
	}
	all := treeRow{kind: kindAll, key: groupKey("all"), name: "all", suffix: fmt.Sprintf("×%d", len(shown)), group: shown}
	return append([]treeRow{all}, out...)
}

// visible is the filtered session list in table order, folded or not: the rows of the tree that carry a
// session. An index into it is not a cursor position.
func (m *model) visible() []*data.Session {
	var out []*data.Session
	for _, r := range m.tree() {
		if r.s != nil {
			out = append(out, r.s)
		}
	}
	return out
}

// shownGroups counts the repositories and tasks in the visible table.
func shownGroups(rows []treeRow) (repos, tasks int) {
	for _, r := range rows {
		switch r.kind {
		case kindRepo:
			repos++
		case kindTask:
			tasks++
		}
	}
	return
}

// cursorRow is the display row under the dashboard cursor (the first row when selID is not shown; the
// repository row when selID is a session folded into it), and the
// first row of the group rows directly above it (which stay in view with it).
func (m *model) cursorRow(rows []treeRow) (cur, first int) {
	cur = slices.IndexFunc(rows, func(r treeRow) bool { return r.holds(m.selID) })
	cur = max(cur, 0)
	first = cur
	for first > 0 && rows[first-1].s == nil {
		first--
	}
	return
}

// scope is the row under the cursor: what the pane, the mix and the zoom show.
func (m *model) scope() (treeRow, bool) {
	rows := m.rows()
	if len(rows) == 0 {
		return treeRow{}, false
	}
	cur, _ := m.cursorRow(rows)
	return rows[cur], true
}

// zoomed is the dashboard showing one session full screen.
func (m *model) zoomed() bool { return m.view == viewDashboard && m.zoom }

// paneSession is the session under the cursor and its dispatches (nil until fetched); nil when the row is a group.
func (m *model) paneSession() (s, d *data.Session) {
	if row, _ := m.scope(); row.s != nil {
		return row.s, m.details[row.s.ID]
	}
	return nil, nil
}

// mostRecent is the visible session with the latest activity.
func (m *model) mostRecent() *data.Session {
	var best *data.Session
	for _, s := range m.visible() {
		if best == nil || m.activity(s).After(m.activity(best)) {
			best = s
		}
	}
	return best
}

// normalizeSelection keeps selID the key of a shown row: when its row is gone the cursor takes the row now at
// the same table index and the zoom ends. The zoom never shows a group row.
func (m *model) normalizeSelection() {
	rows := m.rows()
	switch {
	case len(rows) == 0:
		m.selID = ""
	case !slices.ContainsFunc(rows, func(r treeRow) bool { return r.holds(m.selID) }):
		m.selID = rows[min(m.lastCur, len(rows)-1)].key
		m.zoom = false
	}
	if row, _ := m.scope(); row.s == nil {
		m.zoom = false
	}
}

type totals struct {
	tokens                            int64
	cost, mainCost, subCost           float64
	unpriced                          int64
	unpricedModels                    []string
	dispatches, repeats, loops, warns int
	rechecked, candidates, malformed  int
	byAgent                           map[string]data.Usage
	byModel                           map[string]data.Usage
}

func (m *model) totals() totals { return sumTotals(m.visible()) }

func sumTotals(ss []*data.Session) totals {
	t := totals{byAgent: map[string]data.Usage{}, byModel: map[string]data.Usage{}}
	for _, s := range ss {
		t.tokens += s.Tokens.Total
		t.cost += s.Cost.Total
		t.mainCost += s.Cost.Main
		t.subCost += s.Cost.Subagents
		t.unpriced += s.Cost.UnpricedTokens
		for _, id := range s.Cost.UnpricedModels {
			if !slices.Contains(t.unpricedModels, id) {
				t.unpricedModels = append(t.unpricedModels, id)
			}
		}
		t.dispatches += s.DispatchCount
		t.repeats += s.Repeats
		t.loops += s.ReviewLoops
		t.warns += s.WarnCount
		t.malformed += s.Malformed
		t.rechecked += s.Recheck.Rechecked
		t.candidates += s.Recheck.ExplorersWithCitations
		for k, u := range s.ByAgent {
			v := t.byAgent[k]
			v.Add(u)
			t.byAgent[k] = v
		}
		for k, u := range s.ByModel {
			v := t.byModel[k]
			v.Add(u)
			t.byModel[k] = v
		}
	}
	slices.Sort(t.unpricedModels)
	return t
}

// retarget points every counter at the current values; jump skips the animation.
func (m *model) retarget(jump bool) {
	t := m.totals()
	set := func(k string, v float64) {
		if jump {
			m.counter[k].Jump(v)
		} else {
			m.counter[k].Set(v)
		}
	}
	set("tokens", float64(t.tokens))
	set("dispatches", float64(t.dispatches))
	set("repeats", float64(t.repeats))
	set("loops", float64(t.loops))
	set("warns", float64(t.warns))
	share := 0.0
	if t.cost > 0 {
		share = 1000 * t.subCost / t.cost
	}
	set("share", share)
	rc := -1.0
	if t.candidates > 0 {
		rc = 1000 * float64(t.rechecked) / float64(t.candidates)
	}
	set("recheck", rc)
	for id, s := range m.sessions {
		c := m.rowTok[id]
		if c == nil {
			c = &Counter{}
			m.rowTok[id] = c
			if jump {
				c.Jump(float64(s.Tokens.Total))
			}
		}
		if jump {
			c.Jump(float64(s.Tokens.Total))
		} else {
			c.Set(float64(s.Tokens.Total))
		}
	}
}

// animating is true while anything is mid-motion (fast tick).
func (m *model) animating() bool {
	if m.loading && len(m.sessions) == 0 {
		return true
	}
	for _, c := range m.counter {
		if !c.Settled() {
			return true
		}
	}
	for _, c := range m.rowTok {
		if !c.Settled() {
			return true
		}
	}
	now := m.now()
	for _, t := range m.flash {
		if t.After(now) {
			return true
		}
	}
	if m.toast.text != "" && m.toast.until.After(now) {
		return true
	}
	switch m.view {
	case viewDashboard:
		return len(m.nowRunning()) > 0
	case viewFactory, viewInbox:
		return m.fac.busy || m.factoryWorking()
	case viewKaizen:
		return m.kaizenMoving()
	case viewOps:
		return m.ops.busy
	}
	return false
}

// running is a running dispatch of a live session.
type running struct {
	s *data.Session
	d data.Dispatch
}

// nowRunning lists every running dispatch of every live session, oldest first.
func (m *model) nowRunning() []running {
	var out []running
	for id, s := range m.sessions {
		det := m.details[id]
		if det == nil || !m.isLive(s) {
			continue
		}
		for _, x := range det.Dispatches {
			if x.Status == "running" && x.Start != nil {
				out = append(out, running{s, x})
			}
		}
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i].d, out[j].d
		if !a.Start.Equal(*b.Start) {
			return a.Start.Before(*b.Start)
		}
		return a.ToolUseID < b.ToolUseID
	})
	return out
}

// pulsing is true while a live dot is on screen.
func (m *model) pulsing() bool { return m.liveCount() > 0 }

// pulse is the bright half of the live-dot cycle.
func (m *model) pulse() bool {
	if m.opts.Static {
		return true
	}
	return (m.now().UnixMilli()/700)%2 == 0
}

// spinFrame is the current spinner frame.
func (m *model) spinFrame() int {
	if m.opts.Static {
		return 0
	}
	return int(m.now().UnixMilli() / 80)
}

// ---- merge / refresh -------------------------------------------------------------

func (m *model) setToast(text string, isErr bool) {
	m.toast = toast{text: text, err: isErr, until: m.now().Add(toastFor)}
}

func (m *model) inRange(s *data.Session) bool {
	since := ranges[m.rangeIdx].since
	if since == "" {
		return true
	}
	var d time.Duration
	switch since {
	case "24h":
		d = 24 * time.Hour
	case "7d":
		d = 7 * 24 * time.Hour
	case "30d":
		d = 30 * 24 * time.Hour
	}
	return m.now().Sub(m.activity(s)) <= d
}

// mergeSession folds a refreshed session into the list, the detail cache and the
// aggregates, raising toasts for events in sessions we have detail for. ns.LastChange
// says how new its data is; the list entry is never replaced by an older one.
func (m *model) mergeSession(ns *data.Session) {
	old := m.sessions[ns.ID]
	if old == nil && !m.inRange(ns) {
		return
	}
	row, _ := m.scope()
	if prev := m.details[ns.ID]; prev != nil && (m.follow || m.view == viewDashboard && row.s != nil && row.s.ID == ns.ID) {
		m.diffToasts(prev, ns)
	}
	m.details[ns.ID] = ns
	if old != nil && old.LastChange.After(ns.LastChange) {
		return
	}
	summary := *ns
	summary.Dispatches = nil
	m.sessions[ns.ID] = &summary
	m.flash[ns.ID] = m.now().Add(flashFor)
	m.agentsDirty = true
	m.lastUpdate = m.now()
	m.retarget(false)
}

// mergeSeen folds the mtimes a load took into the poll baseline; afterwards seen is
// never nil.
func (m *model) mergeSeen(seen map[string]time.Time) {
	if m.seen == nil {
		m.seen = map[string]time.Time{}
	}
	for id, t := range seen {
		if t.After(m.seen[id]) {
			m.seen[id] = t
		}
	}
}

// mergeLoad folds a full load into the model. The load read its data after taking its
// mtimes, so a session whose data is newer than that (a per-session refresh landed
// meanwhile) is kept as it is.
func (m *model) mergeLoad(msg sessionsMsg) {
	next := map[string]*data.Session{}
	for i := range msg.res.Sessions {
		s := &msg.res.Sessions[i]
		if cur := m.sessions[s.ID]; cur != nil && cur.LastChange.After(s.LastChange) {
			s = cur
		}
		next[s.ID] = s
	}
	for id, cur := range m.sessions {
		_, loaded := next[id]
		if !loaded && m.exists(id) && msg.seen != nil && cur.LastChange.After(msg.seen[id]) {
			next[id] = cur // appeared after the load looked
		}
	}
	m.sessions = next
	m.prices = msg.res.Prices
	m.pruneCaches()
}

// exists reports whether the session's transcript is known to be on disk.
func (m *model) exists(id string) bool {
	if m.seen == nil {
		return m.sessions[id] != nil
	}
	_, ok := m.seen[id]
	return ok
}

// pruneCaches forgets everything kept for sessions that left the list.
func (m *model) pruneCaches() {
	for id := range m.details {
		if m.sessions[id] == nil {
			delete(m.details, id)
		}
	}
	for id := range m.rowTok {
		if m.sessions[id] == nil {
			delete(m.rowTok, id)
		}
	}
	for id := range m.flash {
		if m.sessions[id] == nil {
			delete(m.flash, id)
		}
	}
}

// applyScan compares a poll with what was seen: changed and new sessions refresh,
// sessions whose transcript is gone are dropped.
func (m *model) applyScan(cur map[string]time.Time) []tea.Cmd {
	var cmds []tea.Cmd
	for id, t := range cur {
		if t.After(m.seen[id]) {
			m.seen[id] = t
			m.agentsDirty = true
			cmds = append(cmds, m.fetchSession(id))
		}
	}
	for id := range m.seen {
		if _, ok := cur[id]; !ok {
			delete(m.seen, id)
			delete(m.dirty, id)
			delete(m.retry, id)
			delete(m.sessions, id)
			m.agentsDirty = true
		}
	}
	m.pruneCaches()
	m.normalizeSelection()
	m.retarget(false)
	return cmds
}

// fetchLive makes sure every live session has its dispatches, for the Now running panel.
func (m *model) fetchLive() []tea.Cmd {
	var cmds []tea.Cmd
	for id, s := range m.sessions {
		if m.details[id] == nil && !m.inflight[id] && m.isLive(s) {
			cmds = append(cmds, m.fetchSession(id))
		}
	}
	return cmds
}

func (m *model) diffToasts(prev, cur *data.Session) {
	known := map[string]data.Dispatch{}
	for _, d := range prev.Dispatches {
		known[d.ToolUseID] = d
	}
	for _, d := range cur.Dispatches {
		was, seen := known[d.ToolUseID]
		switch {
		case !seen:
			m.setToast(fmt.Sprintf("▸ %s dispatched: %s", d.AgentType, firstLine(d.Description)), false)
		case was.Status == "running" && d.Status == "completed":
			dur := ""
			if x, ok := d.Duration(m.now()); ok {
				dur = " in " + FmtDuration(x)
			}
			m.setToast(fmt.Sprintf("✓ %s finished%s", d.AgentType, dur), false)
		case was.Status != "failed" && d.Status == "failed":
			m.setToast(fmt.Sprintf("✗ %s failed: %s", d.AgentType, firstLine(d.Description)), true)
		}
	}
	if cur.ReviewLoops > prev.ReviewLoops {
		m.setToast(fmt.Sprintf("↺ reviewer returned CHANGES_NEEDED (loop #%d)", cur.ReviewLoops), true)
	}
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	return s
}

func (m *model) followTarget() tea.Cmd {
	if !m.follow {
		return nil
	}
	s := m.mostRecent()
	if s == nil {
		return nil
	}
	if s.ID != m.selID {
		m.selID = s.ID
		m.dcursor, m.dtop = 0, 0
	}
	if row, _ := m.scope(); row.s == nil {
		m.zoom = false // following onto a folded repository
	}
	return m.ensureDetail(s.ID)
}

// ---- update ------------------------------------------------------------------------

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmds []tea.Cmd
	shown := ""
	if s, _ := m.paneSession(); s != nil {
		shown = s.ID
	}
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.w, m.h = msg.Width, msg.Height

	case sessionsMsg:
		if msg.gen != m.loadGen {
			break
		}
		m.loading, m.loaded = false, true
		m.mergeSeen(msg.seen)
		m.setScanErr(msg.scanErr)
		if msg.err != nil {
			m.noteFailure(msg.err)
			break
		}
		m.refreshErr = ""
		m.mergeLoad(msg)
		m.syncOrder(true)
		m.lastUpdate = m.now()
		m.normalizeSelection()
		m.retarget(false)
		cmds = append(cmds, m.followTarget())
		if s, _ := m.paneSession(); s != nil {
			cmds = append(cmds, m.ensureDetail(s.ID))
		}
		cmds = append(cmds, m.fetchLive()...)
		cmds = append(cmds, m.refreshFactory(0), m.loadOps(opsIdleEvery))

	case agentsMsg:
		if msg.gen != m.loadGen {
			break
		}
		m.agentsBusy = false
		m.agentsAt = m.now()
		if msg.err != nil {
			m.agentsDirty = true // the next try waits for the next cadence
			m.noteFailure(msg.err)
			break
		}
		m.agents = msg.res
		m.agentsDirty = false

	case factoryMsg:
		cmds = append(cmds, m.applyFactory(msg), m.startFetches())

	case factoryFetchMsg:
		m.applyFetch(msg)

	case answerMsg:
		cmds = append(cmds, m.answered(msg))

	case standingMsg:
		cmds = append(cmds, m.standingSet(msg))

	case kaizenMsg:
		cmds = append(cmds, m.applyKaizen(msg))

	case opsMsg:
		m.applyOps(msg)

	case hanamiEventsMsg, hanamiEndMsg, hanamiHistoryMsg:
		cmds = append(cmds, m.hanamiUpdate(msg))

	case detailMsg:
		delete(m.inflight, msg.id)
		exists := m.exists(msg.id)
		switch {
		case msg.err != nil:
			if m.sessions[msg.id] != nil {
				m.noteFailure(msg.err)
				m.retry[msg.id] = m.now()
			}
		case exists:
			delete(m.retry, msg.id)
			m.refreshErr = ""
			msg.res.LastChange = msg.at
			m.mergeSession(msg.res)
			m.normalizeSelection()
			cmds = append(cmds, m.followTarget())
		}
		if m.dirty[msg.id] && exists {
			cmds = append(cmds, m.fetchSession(msg.id))
		}

	case scanMsg:
		m.scanning = false
		m.setScanErr(msg.err)
		if msg.err == nil {
			cmds = append(cmds, m.applyScan(msg.seen)...)
		}

	case selMsg:
		if row, _ := m.scope(); msg.gen == m.selGen && row.s != nil {
			cmds = append(cmds, m.ensureDetail(row.s.ID))
		}

	case tickMsg:
		if msg.gen != m.tickGen {
			break // a faster tick replaced this one
		}
		m.tickOn = false
		now := m.now()
		m.step(now.Sub(m.tickFrom))
		m.hanamiTick(now.Sub(m.tickFrom))
		if m.loaded && !m.scanning && now.Sub(m.polledAt) >= pollEvery {
			m.polledAt = now
			cmds = append(cmds, m.scanCmd())
		}
		for id, at := range m.retry {
			if now.Sub(at) >= agentsEvery && !m.inflight[id] {
				cmds = append(cmds, m.fetchSession(id))
			}
		}
		if m.agentsDirty && !m.agentsBusy && now.Sub(m.agentsAt) >= agentsEvery {
			cmds = append(cmds, m.loadAgentsCmd())
		}
		cmds = append(cmds, m.refreshFactory(factoryEvery), m.loadOps(m.opsTickEvery()))

	case attachMsg:
		if msg.err != nil {
			text := msg.name + ": " + firstLine(msg.err.Error())
			if errors.As(msg.err, new(*exec.ExitError)) {
				text = msg.name + ": not running?"
			}
			m.setToast(text, true)
		}

	case tea.KeyPressMsg:
		cmds = append(cmds, m.handleKey(msg))
	}
	// the pane never waits for a key to load: a walking cursor fetches once it rests, a row the cursor was
	// handed (its own row vanished) fetches at once
	if s, _ := m.paneSession(); s != nil && s.ID != shown {
		if _, key := msg.(tea.KeyPressMsg); key {
			cmds = append(cmds, m.debounceDetail())
		} else {
			cmds = append(cmds, m.ensureDetail(s.ID))
		}
	}
	m.syncOrder(false)
	m.fixScroll()
	cmds = append(cmds, m.hanamiSync(), m.ensureTick())
	return m, tea.Batch(cmds...)
}

// setScanErr records whether the last scan worked; a failing one marks the screen
// stale, with one toast when it starts failing.
func (m *model) setScanErr(err error) {
	if err == nil {
		m.scanErr = ""
		return
	}
	if m.scanErr == "" && len(m.sessions) > 0 {
		m.setToast("scan failed: "+firstLine(err.Error()), true)
	}
	m.scanErr = err.Error()
}

func (m *model) noteFailure(err error) {
	m.refreshErr = err.Error()
	if len(m.sessions) > 0 {
		m.setToast("refresh failed: "+firstLine(err.Error()), true)
	}
}

// step advances the tweens by dt and prunes finished flashes and toasts.
func (m *model) step(dt time.Duration) {
	for _, c := range m.counter {
		c.Step(dt)
	}
	for _, c := range m.rowTok {
		c.Step(dt)
	}
	now := m.now()
	for id, t := range m.flash {
		if !t.After(now) {
			delete(m.flash, id)
		}
	}
	if m.toast.text != "" && !m.toast.until.After(now) {
		m.toast = toast{}
	}
}

// settle jumps every animation to its end state (snapshots).
func (m *model) settle() {
	m.retarget(true)
	m.kaizenRetarget(true)
	m.flash = map[string]time.Time{}
}

// ---- keys ----------------------------------------------------------------------------

func (m *model) handleKey(msg tea.KeyPressMsg) tea.Cmd {
	switch {
	case key.Matches(msg, keys.Interrupt):
		return tea.Quit
	case m.filterIn:
		return m.filterKey(msg)
	case m.fac.editing:
		return m.capKey(msg)
	case m.pick != nil:
		return m.pickKey(msg)
	case m.view == viewInbox && m.inbox.pick != 0 && !m.help:
		return m.confirmKey(msg)
	case m.view == viewInbox && m.inbox.always && !m.help:
		return m.alwaysKey(msg)
	case m.help:
		if key.Matches(msg, keys.Help, keys.Back, keys.Open, keys.Quit) {
			m.help = false
		}
		return nil
	}
	if m.view == viewHanami {
		if cmd, ok := m.hanamiKey(msg); ok {
			return cmd
		}
	}
	switch {
	case key.Matches(msg, keys.Quit):
		return tea.Quit
	case key.Matches(msg, keys.Help):
		m.help = true
	case key.Matches(msg, keys.Dispatcher):
		return m.dispatcher()
	case key.Matches(msg, keys.Next):
		return m.switchView(m.cycle(1))
	case key.Matches(msg, keys.Prev):
		return m.switchView(m.cycle(-1))
	case m.view == viewInbox && m.pickable(msg) > 0:
		return m.pickOption(m.pickable(msg))
	case m.view == viewInbox && key.Matches(msg, keys.History):
		m.inbox.history = !m.inbox.history
	case m.view == viewInbox && key.Matches(msg, keys.AlwaysOff):
		m.alwaysOff()
	case key.Matches(msg, keys.Inbox):
		return m.switchView(viewInbox)
	case key.Matches(msg, keys.Dash):
		return m.switchView(viewDashboard)
	case key.Matches(msg, keys.Agents):
		return m.switchView(viewAgents)
	case key.Matches(msg, keys.Factory):
		return m.switchView(viewFactory)
	case key.Matches(msg, keys.Kaizen):
		return m.switchView(viewKaizen)
	case key.Matches(msg, keys.Ops):
		return m.switchView(viewOps)
	case key.Matches(msg, keys.Hanami):
		return m.switchView(viewHanami)
	case m.view == viewDashboard && !m.zoom && key.Matches(msg, keys.Fold): // the same keys turn the page in the Kaizen view
		m.fold(true)
	case m.view == viewDashboard && !m.zoom && key.Matches(msg, keys.Unfold):
		m.fold(false)
	case key.Matches(msg, keys.PageLeft):
		if m.view == viewKaizen {
			m.kaizenPage(-1)
		}
	case key.Matches(msg, keys.PageRight):
		if m.view == viewKaizen {
			m.kaizenPage(1)
		}
	case key.Matches(msg, keys.Down):
		m.move(1)
	case key.Matches(msg, keys.Up):
		m.move(-1)
	case key.Matches(msg, keys.Top):
		m.moveTo(0)
	case key.Matches(msg, keys.Bottom):
		m.moveTo(1 << 30)
	case key.Matches(msg, keys.PageDown):
		m.move(max(1, m.pageSize()/2))
	case key.Matches(msg, keys.PageUp):
		m.move(-max(1, m.pageSize()/2))
	case key.Matches(msg, keys.Open):
		return m.enter()
	case key.Matches(msg, keys.Back):
		return m.back()
	case key.Matches(msg, keys.Range):
		if len(m.sessions) == 0 && m.refreshErr != "" {
			return m.reload() // the error screen: try the same range again
		}
		m.rangeIdx = (m.rangeIdx + 1) % len(ranges)
		m.setToast("range: "+ranges[m.rangeIdx].label, false)
		return tea.Batch(m.reload(), m.loadKaizen())
	case key.Matches(msg, keys.Follow):
		m.follow = !m.follow
		if m.follow {
			m.setToast("follow mode on: tracking the most recent session", false)
			return m.followTarget()
		}
		m.setToast("follow mode off", false)
	case key.Matches(msg, keys.Sort):
		m.sortBy = (m.sortBy + 1) % 3
		m.syncOrder(true)
		m.setToast("sort: "+m.sortBy.String(), false)
	case key.Matches(msg, keys.Filter):
		if m.view == viewDashboard && !m.zoom {
			m.filterIn = true
		}
	case key.Matches(msg, keys.CapEdit):
		if m.onFloor() && m.opts.ReadOnly {
			m.setToast("read-only: this key may not change the cap", true)
		} else if m.onFloor() {
			m.openCap()
		}
	}
	m.fixScroll()
	return nil
}

// attachMsg is the end of an attach: err is nil when the conversation was left cleanly.
type attachMsg struct {
	name string
	err  error
}

// dispatcher is key d: attach at once to the one dispatcher named in the factory settings, or
// open the picker over several.
func (m *model) dispatcher() tea.Cmd {
	switch {
	case m.opts.ReadOnly:
		m.setToast("read-only key: no dispatcher", true)
		return nil
	case m.opts.Attach == nil:
		return nil
	}
	names, err := data.ReadDispatchers(data.FactorySettingsPath())
	if err != nil {
		m.setToast(err.Error(), true)
		return nil
	}
	if len(names) == 1 {
		return m.attach(names[0])
	}
	m.pick, m.pickCur = names, 0
	return nil
}

func (m *model) attach(name string) tea.Cmd {
	m.pick = nil
	return tea.Exec(m.opts.Attach(name), func(err error) tea.Msg { return attachMsg{name, err} })
}

// pickKey handles every key while the picker is open: 1-9 pick the first nine, the arrows reach all.
func (m *model) pickKey(msg tea.KeyPressMsg) tea.Cmd {
	switch {
	case key.Matches(msg, keys.Back):
		m.pick = nil
	case key.Matches(msg, keys.Down):
		m.pickCur = min(m.pickCur+1, len(m.pick)-1)
	case key.Matches(msg, keys.Up):
		m.pickCur = max(m.pickCur-1, 0)
	case key.Matches(msg, keys.Open):
		return m.attach(m.pick[m.pickCur])
	default:
		if n, err := strconv.Atoi(msg.Key().Text); err == nil && n >= 1 && n <= min(9, len(m.pick)) {
			return m.attach(m.pick[n-1])
		}
	}
	return nil
}

func (m *model) filterKey(msg tea.KeyPressMsg) tea.Cmd {
	switch {
	case key.Matches(msg, keys.FilterClear):
		m.filter, m.filterIn = "", false
	case key.Matches(msg, keys.FilterKeep):
		m.filterIn = false
	case key.Matches(msg, keys.Erase):
		if r := []rune(m.filter); len(r) > 0 {
			m.filter = string(r[:len(r)-1])
		}
	case key.Matches(msg, keys.Wipe):
		m.filter = ""
	default:
		if t := msg.Key().Text; t != "" {
			m.filter += t
		}
	}
	m.normalizeSelection()
	m.retarget(false)
	m.fixScroll()
	return nil
}

func (m *model) switchView(v viewID) tea.Cmd {
	m.view, m.zoom, m.detailOnly = v, false, false
	m.fixScroll()
	m.inbox.pick, m.inbox.always = 0, false
	switch v {
	case viewFactory, viewInbox:
		return m.refreshFactory(factoryFresh)
	case viewKaizen:
		m.kz.shownAt = m.now()
		return m.loadKaizen()
	case viewOps:
		return m.loadOps(0)
	}
	return nil
}

// fold collapses the repository the cursor is in to its one row (on), or expands the collapsed repository
// under the cursor; either way the cursor ends on the repository row, which is a cursor move (follow mode
// goes off). It does nothing on the all row, on a repository of one session, and when the repository is
// already as asked. A short-runs row comes first: on it or on one of its sessions, left closes it, and
// right opens it when closed; both put the cursor on it.
func (m *model) fold(on bool) {
	rows := m.rows()
	if len(rows) == 0 {
		return
	}
	i, _ := m.cursorRow(rows)
	if k := shortRowAt(rows, i); k >= 0 && rows[k].folded != on {
		if on {
			delete(m.opened, rows[k].key)
		} else {
			m.opened[rows[k].key] = true
		}
		m.moveTo(k)
		return
	}
	for rows[i].depth > 0 {
		i--
	}
	r := rows[i]
	switch {
	case r.kind != kindRepo || r.s != nil || r.folded == on:
	case on:
		m.folded[r.key] = true
		m.moveTo(i)
	default:
		delete(m.folded, r.key)
		m.moveTo(i)
	}
}

// shortRowAt is the index of the short-runs row that rows[i] is, or is listed under; -1 when none.
func shortRowAt(rows []treeRow, i int) int {
	if rows[i].kind == kindShort {
		return i
	}
	j := i
	for j > 0 && rows[j].depth >= rows[i].depth {
		j--
	}
	if rows[j].kind == kindShort {
		return j
	}
	return -1
}

// cycle is the next view in a direction.
func (m *model) cycle(step int) viewID {
	const n = int(viewHanami) + 1 // the last view constant
	return viewID((int(m.view) + step + n) % n)
}

// ensureDetail fetches a session's dispatches unless they are here or coming.
func (m *model) ensureDetail(id string) tea.Cmd {
	if id == "" || m.details[id] != nil || m.inflight[id] {
		return nil
	}
	return m.fetchSession(id)
}

// debounceDetail waits for the cursor to rest on a session row before fetching its dispatches, so a cursor
// walking down the table starts one fetch, not one per row.
func (m *model) debounceDetail() tea.Cmd {
	s, d := m.paneSession()
	if s == nil || d != nil || m.inflight[s.ID] || m.opts.Static {
		return nil
	}
	m.selGen++
	gen := m.selGen
	return tea.Tick(detailDebounce, func(time.Time) tea.Msg { return selMsg{gen} })
}

// enter zooms into the session under the cursor; in the zoom it swaps the list for the detail pane.
func (m *model) enter() tea.Cmd {
	if m.view != viewDashboard {
		return nil
	}
	if m.zoom {
		m.detailOnly = !m.detailOnly
		return nil
	}
	row, _ := m.scope()
	if row.s == nil {
		m.setToast("enter opens a session row", false)
		return nil
	}
	m.zoom = true
	m.dcursor, m.dtop = 0, 0
	return m.ensureDetail(row.s.ID)
}

func (m *model) back() tea.Cmd {
	switch {
	case m.zoomed() && m.detailOnly:
		m.detailOnly = false
	case m.zoomed():
		m.zoom = false
	case m.view != viewDashboard && m.view != viewInbox:
		return m.switchView(viewInbox)
	case m.view == viewDashboard && m.filter != "":
		m.filter = ""
		m.normalizeSelection()
		m.retarget(false)
	}
	return nil
}

func (m *model) listLen() int {
	switch m.view {
	case viewDashboard:
		if m.zoom {
			if _, d := m.paneSession(); d != nil {
				return len(d.Dispatches)
			}
			return 0
		}
		return len(m.rows())
	case viewAgents:
		if m.agents != nil {
			return len(m.agents.Agents)
		}
	case viewFactory, viewInbox:
		return m.factoryPositions()
	case viewKaizen:
		return m.kaizenPositions()
	case viewOps:
		return m.opsPositions()
	}
	return 0
}

func (m *model) pageSize() int { return max(1, m.h-10) }

func (m *model) move(delta int) {
	if m.view == viewInbox && m.stepCard(delta) {
		return
	}
	n := m.listLen()
	if n == 0 {
		return
	}
	var cur int
	switch m.view {
	case viewDashboard:
		if m.zoom {
			cur = m.dcursor
		} else {
			cur, _ = m.cursorRow(m.rows())
		}
	case viewAgents:
		cur = m.acursor
	case viewFactory, viewInbox:
		cur = *m.floorTop()
	case viewKaizen:
		cur = m.kz.top[m.kz.page]
	case viewOps:
		cur = m.ops.top
	}
	m.moveTo(cur + delta)
}

func (m *model) moveTo(i int) {
	n := m.listLen()
	if n == 0 {
		return
	}
	i = max(0, min(n-1, i))
	switch m.view {
	case viewDashboard:
		if m.zoom {
			m.dcursor = i
			break
		}
		m.selID = m.rows()[i].key
		m.dcursor, m.dtop = 0, 0
		if m.follow {
			m.follow = false
			m.setToast("follow mode off", false)
		}
	case viewAgents:
		m.acursor = i
	case viewFactory, viewInbox:
		*m.floorTop() = i
	case viewKaizen:
		m.kz.top[m.kz.page] = i
	case viewOps:
		m.ops.top = i
	}
	m.fixScroll()
}

// fixScroll keeps each cursor inside its window.
func (m *model) fixScroll() {
	keep := func(cursor, top, rows int) int {
		if rows < 1 {
			return 0
		}
		if cursor < top {
			top = cursor
		}
		if cursor >= top+rows {
			top = cursor - rows + 1
		}
		return max(0, top)
	}
	m.syncInbox()
	rows := m.rows()
	cur, first := m.cursorRow(rows)
	if slices.ContainsFunc(rows, func(r treeRow) bool { return r.holds(m.selID) }) {
		m.lastCur = cur
	}
	n := m.dashRows()
	if first < m.top {
		m.top = first
	}
	m.top = keep(cur, m.top, n)
	if m.zoomed() {
		m.dtop = keep(m.dcursor, m.dtop, m.sessRows())
	}
	m.atop = keep(m.acursor, m.atop, m.agentRows())
	m.top = min(m.top, max(0, len(rows)-n))
	switch m.view {
	case viewFactory, viewInbox:
		t := m.floorTop()
		*t = min(*t, m.factoryPositions()-1)
	case viewKaizen:
		m.kz.top[m.kz.page] = min(m.kz.top[m.kz.page], m.kaizenPositions()-1)
	case viewOps:
		m.ops.top = min(m.ops.top, m.opsPositions()-1)
	}
}
