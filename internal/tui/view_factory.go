package tui

import (
	"cmp"
	"fmt"
	"image/color"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"isshoni/internal/data"
)

const (
	meterCells  = 20 // beyond this many slots the meter is a bar
	baseMax     = 24
	prMax       = 24
	phaseMax    = 25 // (glyph, space and the 23 cells of "review · prioritized 3m") PHASE keeps more than this only when every column fits at it
	slugLeast   = 16
	pausedStale = 30 * time.Minute // a pause this old is stale: the script's blocked-unanswered bound
	nameLeast   = 12               // cells the repository name keeps before the meter shrinks
	bandDoLeast = 30               // cells the band's do cell needs to stay on its row
)

// factoryBody is the factory view: what waits on the human, the settings line, then one
// section per repository, scrolled when it is taller than the window.
func (m *model) factoryBody(p paint, w, h int) block {
	return scrolled(p, m.factoryLines(p, w, h), m.fac.top, w, h)
}

// scrolled shows h of the lines from top, the last of them saying how many more there are.
func scrolled(p paint, lines block, top, w, h int) block {
	top = max(0, min(top, len(lines)-h))
	out := slices.Clone(lines[top:min(len(lines), top+h)])
	if more := len(lines) - top - h; more > 0 {
		out[h-1] = fit(p.sp(1)+p.t(fmt.Sprintf("+%d more lines · ↓ to scroll", more+1), p.th.Faint), w, p)
	}
	return out
}

// factoryPositions is how many places the body can be scrolled to.
// Below the size the frame needs there is one: the small-window screen shows instead.
func (m *model) factoryPositions() int {
	if m.w < minWidth || m.h < minHeight {
		return 1
	}
	h := m.h - 3
	return max(1, len(m.factoryLines(m.th.on(m.th.Bg), m.w, h))-h+1)
}

func (m *model) factoryLines(p paint, w, h int) block {
	th := m.th
	f := &m.fac
	lines := block{m.settingsLine(p, w), p.sp(w)}
	switch {
	case f.doneAt.IsZero():
		msg := p.t(spin(m.spinFrame())+" ", th.Purple) + p.t("asking the factory…", th.Muted)
		return append(lines, p.center(block{msg}, w, h-len(lines))...)
	case len(f.repos) == 0:
		msg := block{p.b("No factory tasks here yet", th.Text), ""}
		for _, l := range wrapPlain("Tasks appear when a dispatcher starts leads with /factory:dispatch in a repository seen in this range.", min(w-8, 60)) {
			msg = append(msg, p.t(l, th.Muted))
		}
		return append(lines, p.center(msg, w, h-len(lines))...)
	}
	lines = append(m.needsYou(p, w), lines...)
	var all []data.FactoryTask
	for _, r := range f.repos {
		if r.st != nil {
			all = append(all, r.st.Rows...)
		}
	}
	cols := m.factoryCols(p, w, all)
	for i, r := range f.repos {
		if i > 0 {
			lines = append(lines, p.sp(w))
		}
		lines = append(lines, m.repoHeader(p, r, w))
		switch {
		case r.err != "":
			lines = append(lines, p.sp(2)+p.t(trunc("⚠ "+r.err, w-3), th.Red))
		case len(r.st.Rows) == 0:
			lines = append(lines, p.sp(2)+p.t("no tasks", th.Faint))
		default:
			if l := m.prLine(r); l != "" {
				lines = append(lines, p.sp(2)+p.t(trunc(l, w-3), th.Faint))
			}
			lines = append(lines, cols.header(p))
			for _, t := range sortedTasks(r.st.Rows) {
				lines = append(lines, m.taskRow(p, cols, t, w)...)
			}
		}
	}
	return lines
}

// prLine dates a repository's pull request data when it is older than prStale or the
// fetch pass failed; it is empty otherwise.
func (m *model) prLine(r factoryRepo) string {
	asOf := r.st.PRs.AsOf
	if asOf == nil && r.fetchErr == "" {
		return ""
	}
	line := "pull requests not fetched yet"
	if asOf != nil {
		age := m.now().Sub(time.Unix(0, int64(*asOf*1e9)))
		if age <= prStale && r.fetchErr == "" {
			return ""
		}
		line = "pull requests as of " + ago(age) + " ago"
	}
	if r.fetchErr != "" {
		line += " · GitHub slow: " + r.fetchErr
	}
	return line
}

// ---- what waits on the human ------------------------------------------------------

// waitItem is a task that waits on the human: in review (its pull request ready, not a draft), or blocked on the human.
type waitItem struct {
	repo string
	task data.FactoryTask
}

// waiting lists the waiting tasks of every repository, the longest waiting first (an
// unknown wait last), then by repository and slug.
func (m *model) waiting() []waitItem {
	var items []waitItem
	for _, r := range m.fac.repos {
		if r.st == nil {
			continue
		}
		for _, t := range r.st.Rows {
			if (t.Phase == "review" && !t.PRDraft) || t.WaitsOnHuman() {
				items = append(items, waitItem{r.name, t})
			}
		}
	}
	waited := func(it waitItem) int64 {
		if it.task.Waited == nil {
			return -1
		}
		return *it.task.Waited
	}
	slices.SortFunc(items, func(a, b waitItem) int {
		return cmp.Or(cmp.Compare(waited(b), waited(a)), cmp.Compare(a.repo, b.repo), cmp.Compare(a.task.Slug, b.task.Slug))
	})
	return items
}

// todo says what the human is asked to do with a waiting task, and what to look at;
// do is empty when there is nothing to show.
func (it waitItem) todo() (what, do string) {
	t := it.task
	if t.Phase == "review" {
		switch {
		case t.PRURL != "":
			return "review PR", "#" + t.PRURL[strings.LastIndex(t.PRURL, "/")+1:] + " " + t.PRURL
		case strings.Contains(t.Deliverable, "://"):
			return "see deliverable", t.Deliverable
		case t.Deliverable != "":
			return "see deliverable", data.ShortPath(t.Deliverable)
		}
		return "review", ""
	}
	what = map[string]string{"QUESTION": "answer question", "BLOCKED": "unblock", "OVERTIME": "decide: overtime"}[t.StopKind]
	return cmp.Or(what, "blocked"), strings.Join(strings.Fields(t.StopSummary), " ")
}

// needsYou is the band at the top of the view: a title and a row per waiting task, then a
// blank line; one faint line when nothing waits. A row's do cell takes the rest of the
// line, or its own line when that leaves it under bandDoLeast cells.
func (m *model) needsYou(p paint, w int) block {
	th := m.th
	items := m.waiting()
	if len(items) == 0 {
		quiet := "✓ nothing waits on you"
		if slices.ContainsFunc(m.fac.repos, func(r factoryRepo) bool { return r.st == nil }) {
			quiet = "nothing waits on you in the repositories that answered"
		}
		return block{fit(p.sp(1)+p.t(quiet, th.Faint), w, p)}
	}
	lines := block{fit(p.sp(1)+p.b(fmt.Sprintf("▲ Needs you  %d", len(items)), th.Orange), w, p)}
	repoW, slugW, whatW, waitW := 0, 0, 0, 1
	whats, dos, waits := make([]string, len(items)), make([]string, len(items)), make([]string, len(items))
	for i, it := range items {
		whats[i], dos[i] = it.todo()
		waits[i] = "-"
		if it.task.Waited != nil {
			waits[i] = FmtDuration(time.Duration(*it.task.Waited) * time.Second)
		}
		repoW, slugW = max(repoW, min(nameLeast, width(it.repo))), max(slugW, width(it.task.Slug))
		whatW, waitW = max(whatW, width(whats[i])), max(waitW, width(waits[i]))
	}
	slugCol := 1 + 2 + repoW + 2 // where the slug starts
	slugW = max(1, min(slugW, w-slugCol-2-whatW-2-waitW))
	doCol := slugCol + slugW + 2 + whatW + 2 + waitW + 2
	for i, it := range items {
		glyph, _, col := m.phaseLook(it.task, false)
		do, doFg := dos[i], th.Text
		switch {
		case do != "":
		case it.task.Phase == "blocked":
			do, doFg = "no reason recorded: ask its lead", th.Faint
		default:
			do, doFg = "-", th.Faint
		}
		row := p.sp(1) + p.t(glyph, col) + p.sp(1) + p.t(cell(it.repo, repoW, false), th.Muted) + p.sp(2) +
			p.t(cell(it.task.Slug, slugW, false), th.Text) + p.sp(2) + p.t(cell(whats[i], whatW, false), col) + p.sp(2) +
			p.t(cell(waits[i], waitW, true), th.Muted)
		if w-doCol-1 >= bandDoLeast {
			lines = append(lines, fit(row+p.sp(2)+p.t(trunc(do, w-doCol-1), doFg), w, p))
			continue
		}
		lines = append(lines, fit(row, w, p), fit(p.sp(slugCol)+p.t(trunc(do, max(1, w-slugCol-1)), doFg), w, p))
	}
	return append(lines, p.sp(w))
}

// ---- subheader and settings line ------------------------------------------------

func (m *model) factorySubheader(p paint) string {
	th := m.th
	f := &m.fac
	left := p.b("Factory", th.Text)
	var motion, blocked, review, merged, settled int
	reported := false
	for _, r := range f.repos {
		if r.st != nil {
			motion, blocked, review, merged, settled = motion+r.st.InMotion, blocked+r.st.Blocked, review+r.st.InReview, merged+r.st.Merged, settled+r.st.Settled
			reported = true
		}
	}
	totals := func(long bool) string {
		if !reported {
			return ""
		}
		dot := p.t(" · ", th.Faint)
		out := p.sp(2) + p.t(fmt.Sprintf("%d %s", motion, phaseLabel("motion", long)), th.Blue)
		if blocked > 0 {
			out += p.b(fmt.Sprintf(" (%d blocked)", blocked), th.Orange)
		}
		out += dot + p.t(fmt.Sprintf("%d %s", review, phaseLabel("review", long)), th.Yellow) + dot +
			p.t(fmt.Sprintf("%d %s", merged, phaseLabel("merged", long)), m.mergedColor())
		if settled > 0 {
			out += dot + p.t(fmt.Sprintf("%d settled", settled), th.Muted)
		}
		return out
	}
	lead := p.sp(2)
	if reported {
		lead = p.t("  · ", th.Faint)
	}
	tail := func(asOf string) string {
		switch {
		case f.busy && asOf == "":
			return lead + p.t(spin(m.spinFrame()), th.Blue)
		case f.busy:
			return lead + p.t(spin(m.spinFrame())+" refreshing", th.Blue)
		case !f.doneAt.IsZero():
			return lead + p.t(asOf+f.doneAt.Local().Format("15:04:05"), th.Faint)
		}
		return ""
	}
	if long := left + totals(true) + tail("as of "); m.w >= 100 && width(long) <= m.w-2 {
		return long
	}
	if short := left + totals(false) + tail("as of "); width(short) <= m.w-2 {
		return short
	}
	return left + totals(false) + tail("")
}

func (m *model) settingsLine(p paint, w int) string {
	th := m.th
	f := &m.fac
	key := func(k, desc string) string { return p.b(k, th.Purple) + p.t(" "+desc, th.Faint) }
	left := p.sp(1) + p.t("worktree cap  ", th.Muted)
	switch {
	case f.editing:
		left += p.t("[ ", th.Purple) + p.b(f.field, th.Text) + p.t("█", th.Purple) + p.t(" ]", th.Purple)
		if f.fieldErr != "" {
			left += p.sp(2) + p.t(f.fieldErr, th.Red)
		} else {
			left += p.t("  per repo", th.Muted) + p.sp(3) + key("enter", "save") + p.t(" · ", th.Faint) + key("esc", "cancel") + p.t(" · ", th.Faint) + key("↑↓", "adjust")
		}
	case f.capErr != "":
		left += p.t(f.capErr, th.Red) + p.sp(3) + key("c", "edit")
	case f.cap == 0:
		left += p.t("…", th.Faint) + p.t("  per repo", th.Muted)
	default:
		left += p.b(strconv.Itoa(f.cap), th.Text) + p.t("  per repo", th.Muted)
		if f.reserve > 0 {
			left += p.t(fmt.Sprintf(", %d in reserve", f.reserve), th.Muted)
		}
		left += p.sp(3) + key("c", "edit")
	}
	if right := p.t(data.ShortPath(data.FactorySettingsPath()), th.Faint); width(left)+width(right)+3 <= w {
		left += p.sp(w-width(left)-width(right)-1) + right
	}
	return fit(left, w, p)
}

// ---- repository header and capacity meter -----------------------------------------

func (m *model) repoHeader(p paint, r factoryRepo, w int) string {
	th := m.th
	right := ""
	if st := r.st; st != nil {
		count := func(label string, n int, c color.Color) string {
			if n == 0 {
				c = th.Faint
			}
			return p.t(label+" ", th.Faint) + p.b(strconv.Itoa(n), c)
		}
		counts := p.sp(3) + count("review", st.InReview, th.Yellow) + p.sp(3) + count("merged", st.Merged, m.mergedColor())
		if st.Settled > 0 {
			counts += p.sp(3) + count("settled", st.Settled, th.Muted)
		}
		budget := w - 3 - (1 + min(nameLeast, width(r.name)))
		right = m.meter(p, st, budget-width(counts)) + counts
		if width(right) > budget {
			right = m.meter(p, st, budget) // the name comes before the other counts
		}
	}
	left := trunc(p.sp(1)+p.b(r.name, th.Text), max(1, w-width(right)-3))
	return fit(left+p.sp(max(1, w-width(left)-width(right)-1))+right, w, p)
}

// meter shows tasks in motion against the cap: one cell per slot, a bar when there are
// too many to count or no room for them within budget cells. With a reserve the slots
// are the ordinary limit L plus the reserve R, the label reads n/L+R, and free reserve
// cells (◌) follow the free ordinary ones; filled cells past L run into the reserve.
// Green below L (below the cap without a reserve), yellow from L up to the cap, red above
// it; the cells of blocked tasks keep the blocked phase's look.
func (m *model) meter(p paint, st *data.FactoryStatus, budget int) string {
	th := m.th
	n, limit := st.InMotion, st.Cap-st.Reserve
	col := th.Green
	switch {
	case n > st.Cap:
		col = th.Red
	case n >= limit:
		col = th.Yellow
	case n == 0:
		col = th.Muted
	}
	label := fmt.Sprintf("%d/%d", n, limit)
	if st.Reserve > 0 {
		label += fmt.Sprintf("+%d", st.Reserve)
	}
	room := min(meterCells, budget-len("in motion ")-1-len(label))
	var cells string
	if max(n, st.Cap) > room {
		cells = p.bar(max(4, room), float64(n)/float64(max(1, st.Cap)), col, false)
	} else {
		blocked := min(st.Blocked, n)
		cells = p.t(strings.Repeat("▲", blocked), th.Orange) + p.t(strings.Repeat("●", n-blocked), col) +
			p.t(strings.Repeat("○", max(0, limit-n)), th.Faint) + p.t(strings.Repeat("◌", max(0, st.Cap-max(n, limit))), th.Faint)
	}
	return p.t("in motion ", th.Faint) + cells + p.sp(1) + p.b(label, col)
}

func (m *model) mergedColor() color.Color { return mix(m.th.Green, m.th.Muted, 0.45) }

// ---- tasks ------------------------------------------------------------------------

var knownPhases = []string{"blocked", "motion", "review", "merged", "settled"}

// sortedTasks orders by phase (the known ones first, the rest by name), overtime first, then slug.
func sortedTasks(rows []data.FactoryTask) []data.FactoryTask {
	rows = slices.Clone(rows)
	rank := func(phase string) int {
		if i := slices.Index(knownPhases, phase); i >= 0 {
			return i
		}
		return len(knownPhases)
	}
	slices.SortFunc(rows, func(a, b data.FactoryTask) int {
		if c := cmp.Or(cmp.Compare(rank(a.Phase), rank(b.Phase)), cmp.Compare(a.Phase, b.Phase)); c != 0 {
			return c
		}
		if a.Overtime != b.Overtime {
			if a.Overtime {
				return -1
			}
			return 1
		}
		return cmp.Compare(a.Slug, b.Slug)
	})
	return rows
}

// phaseLabel names a phase, in full from 100 columns up.
func phaseLabel(phase string, long bool) string {
	if long {
		switch phase {
		case "motion":
			return "in motion"
		case "review":
			return "waiting for review"
		case "merged":
			return "merged, not cleaned"
		case "blocked":
			return "blocked, needs you"
		case "settled":
			return "settled, not cleaned"
		}
	}
	return phase
}

// phaseLook is the glyph, label and colour of a task's phase; the glyph of a task in
// motion turns while its session works.
func (m *model) phaseLook(t data.FactoryTask, long bool) (glyph, label string, c color.Color) {
	th := m.th
	label = phaseLabel(t.Phase, long)
	if t.Phase == "blocked" && !t.WaitsOnHuman() {
		label = "blocked on " + t.WaitsOn
	}
	if f := t.Prioritized; f != nil {
		label = phaseLabel(t.Phase, false) + " · prioritized"
		if !f.Since.IsZero() {
			label += " " + limitSpan(int64(max(0, m.now().Sub(f.Since)).Seconds()))
		}
	}
	stalePause := false
	if f := t.Paused; f != nil && t.Phase == "blocked" {
		label = "paused"
		if !f.Since.IsZero() {
			age := max(0, m.now().Sub(f.Since))
			label += " " + limitSpan(int64(age.Seconds()))
			stalePause = age >= pausedStale
		}
		if len(f.WaitsOn) > 0 {
			label += ": waits " + strings.Join(f.WaitsOn, ", ")
		}
	}
	switch t.Phase {
	case "motion":
		glyph = "●"
		if t.Session == "working" {
			glyph = spin(m.spinFrame())
		}
		return glyph, label, th.Blue
	case "review":
		return "◆", label, th.Yellow
	case "merged":
		return "✓", label, m.mergedColor()
	case "blocked":
		if stalePause {
			return "▲", label, th.Yellow
		}
		if !t.WaitsOnHuman() {
			return "▲", label, th.Muted // not waiting on you
		}
		return "▲", label, th.Orange
	case "settled":
		return "·", label, th.Faint
	}
	return "·", label, th.Muted
}

func prText(t data.FactoryTask) string {
	if t.PRURL == "" && t.Deliverable != "" {
		if _, link, ok := strings.Cut(t.Deliverable, "://"); ok {
			return link
		}
		return filepath.Base(t.Deliverable)
	}
	switch t.PRState {
	case "none":
		return "-"
	case "unknown":
		return "?"
	}
	num := ""
	if t.PRURL != "" {
		num = "#" + t.PRURL[strings.LastIndex(t.PRURL, "/")+1:]
	}
	state := t.PRState
	if (state == "open" && t.Phase == "review") || (state == "merged" && t.Phase == "merged") {
		state = ""
	}
	return cmp.Or(strings.TrimSpace(num+" "+state), "-")
}

// baseText shows a stacked task (based on another task's branch) as the task it sits on.
func baseText(base string) (text string, stacked bool) {
	if slug, ok := strings.CutPrefix(base, "worktree-"); ok {
		return "↳ " + slug, true
	}
	return strings.TrimPrefix(base, "origin/"), false
}

func flagPills(p paint, t data.FactoryTask) string {
	var ps []string
	if t.Overtime {
		ps = append(ps, p.pill("overtime", p.th.Red))
	}
	if t.Stale {
		ps = append(ps, p.pill("stale", p.th.Yellow))
	}
	return strings.Join(ps, p.sp(1))
}

// elapsedLeast is the ELAPSED width of the bare elapsed time (FmtDuration).
const elapsedLeast = 7

// factoryCols are the column widths shared by every table; 0 is a dropped column. span says
// ELAPSED shows the elapsed time against the limit rather than the elapsed time alone.
type factoryCols struct {
	slug, phase, elapsed, size, pr, base, flags int
	span                                        bool
}

// elapsedText is the ELAPSED cell: elapsed against the limit (41m / 45m) for a task in
// motion that has one and when span, else the elapsed time alone.
func elapsedText(t data.FactoryTask, span bool) string {
	switch {
	case t.Elapsed == nil:
		return "-"
	case span && t.Phase == "motion" && t.Limit != nil:
		return limitSpan(*t.Elapsed) + " / " + limitSpan(*t.Limit)
	}
	return FmtDuration(time.Duration(*t.Elapsed) * time.Second)
}

// limitSpan renders seconds to the minute: 41m, 1h30m, 2h.
func limitSpan(secs int64) string {
	h, m := secs/3600, secs/60%60
	s := ""
	if h > 0 {
		s = fmt.Sprintf("%dh", h)
	}
	if m > 0 || h == 0 {
		s += fmt.Sprintf("%dm", m)
	}
	return s
}

// factoryCols sizes the columns to the widest cell of any task; SIZE shows only when some
// task has a size. When the slug would get
// less than slugLeast cells it drops SIZE, then shows ELAPSED as the elapsed time alone,
// then drops BASE, PR and ELAPSED in that order.
func (m *model) factoryCols(p paint, w int, tasks []data.FactoryTask) factoryCols {
	c := factoryCols{slug: 4, phase: 5, elapsed: elapsedLeast, pr: 2, base: 4, flags: 5, span: true}
	for _, t := range tasks {
		_, label, _ := m.phaseLook(t, m.w >= 100)
		base, _ := baseText(t.Base)
		c.slug = max(c.slug, width(t.Slug))
		c.phase = max(c.phase, 2+width(label))
		c.elapsed = max(c.elapsed, width(elapsedText(t, true)))
		if t.Size != "" {
			c.size = max(c.size, 4, width(t.Size))
		}
		c.pr = max(c.pr, min(width(prText(t)), prMax))
		c.base = max(c.base, min(width(base), baseMax))
		c.flags = max(c.flags, width(flagPills(p, t)))
	}
	used := func(cc factoryCols) int {
		n := 2 + cc.phase + cc.flags + 4 // indent, and the gaps before PHASE and FLAGS
		for _, x := range []int{cc.elapsed, cc.size, cc.pr, cc.base} {
			if x > 0 {
				n += x + 2
			}
		}
		return n
	}
	if today := (factoryCols{phase: c.phase, flags: c.flags, elapsed: elapsedLeast, pr: c.pr, base: c.base}); w-used(today) < c.slug {
		c.phase = min(c.phase, phaseMax)
	}
	for step := 0; ; step++ {
		cc := c
		if step >= 1 {
			cc.size = 0
		}
		if step >= 2 {
			cc.span, cc.elapsed = false, elapsedLeast
		}
		if step >= 3 {
			cc.base = 0
		}
		if step >= 4 {
			cc.pr = 0
		}
		if step >= 5 {
			cc.elapsed = 0
		}
		if free := w - used(cc); free >= min(c.slug, slugLeast) || step == 5 {
			cc.slug = max(1, min(c.slug, free))
			return cc
		}
	}
}

// join lays out one table line from its painted cells, each already padded to its column.
func (c factoryCols) join(p paint, slug, phase, elapsed, size, pr, base, flags string) string {
	parts := []string{slug, phase}
	for _, x := range []struct {
		w    int
		cell string
	}{{c.elapsed, elapsed}, {c.size, size}, {c.pr, pr}, {c.base, base}} {
		if x.w > 0 {
			parts = append(parts, x.cell)
		}
	}
	return p.sp(2) + strings.Join(append(parts, flags), p.sp(2))
}

func (c factoryCols) header(p paint) string {
	f := func(s string, w int, right bool) string { return p.t(cell(s, w, right), p.th.Faint) }
	return c.join(p, f("SLUG", c.slug, false), f("PHASE", c.phase, false), f("ELAPSED", c.elapsed, true), f("SIZE", c.size, false), f("PR", c.pr, false), f("BASE", c.base, false), f("FLAGS", c.flags, false))
}

// taskRow is a task's table line, and under a task blocked on someone else the reason it
// gave, faint, from the PHASE column.
func (m *model) taskRow(p paint, c factoryCols, t data.FactoryTask, w int) block {
	th := m.th
	glyph, label, col := m.phaseLook(t, m.w >= 100)
	pr := prText(t)
	elapsedCol := th.Muted
	switch {
	case t.Overtime:
		elapsedCol = th.Red
	case t.Nearing:
		elapsedCol = th.Yellow
	}
	prCol := th.Text
	if pr == "-" || pr == "?" {
		prCol = th.Faint
	}
	base, stacked := baseText(t.Base)
	baseCol := th.Faint
	if stacked {
		baseCol = th.Purple
	}
	phase := p.t
	if t.WaitsOnHuman() {
		phase = p.b
	}
	row := block{c.join(p,
		p.t(cell(t.Slug, c.slug, false), th.Text),
		fit(phase(glyph+" ", col)+phase(label, col), c.phase, p),
		p.t(cell(elapsedText(t, c.span), c.elapsed, true), elapsedCol),
		p.t(cell(cmp.Or(t.Size, "-"), c.size, false), th.Faint),
		p.t(cell(pr, c.pr, false), prCol),
		p.t(cell(base, c.base, false), baseCol),
		fit(flagPills(p, t), c.flags, p))}
	if reason := strings.Join(strings.Fields(t.StopSummary), " "); t.Phase == "blocked" && !t.WaitsOnHuman() && reason != "" {
		indent := 2 + c.slug + 2
		row = append(row, fit(p.sp(indent)+p.t(trunc(reason, max(1, w-indent-1)), th.Faint), w, p))
	}
	return row
}
