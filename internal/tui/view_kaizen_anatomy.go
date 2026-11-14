package tui

import (
	"fmt"
	"image/color"
	"math"
	"slices"
	"strconv"
	"strings"

	"isshoni/internal/data"
)

// ---- Anatomy ----------------------------------------------------------------------

// anatomyMeasure is one measure of a role in a version: how it is read from the version's
// role (nil when it has no value) and shown. A share's change is told in percentage points,
// the others' as a percentage. lower says a smaller value is what the lander change aimed at:
// the others have no good direction and their change is not coloured.
type anatomyMeasure struct {
	name   string
	get    func(*data.AnatomyRole) *float64
	show   func(float64) string
	points bool
	lower  bool
}

// cell is the measure of role r in a version, "-" when there is none, and whether the figure is
// faint: the role ran in fewer than min of the version's landed tasks.
func (ms anatomyMeasure) cell(r *data.AnatomyRole, min int) (string, bool) {
	if r == nil {
		return "-", true
	}
	x := ms.get(r)
	if x == nil {
		return "-", true
	}
	return ms.show(*x), r.N < min
}

func (m *model) anatomyMeasures() []anatomyMeasure {
	class := func(f func(*data.AnatomyClasses) *float64) func(*data.AnatomyRole) *float64 {
		return func(r *data.AnatomyRole) *float64 {
			if r.Classes == nil {
				return nil
			}
			return f(r.Classes)
		}
	}
	share := func(v float64) string { return pct(v * 100) }
	return []anatomyMeasure{
		{"share of task cost", func(r *data.AnatomyRole) *float64 { return &r.Share }, share, true, true},
		{"own tool calls", func(r *data.AnatomyRole) *float64 { return &r.Tools }, Human, false, true},
		{"peak context", func(r *data.AnatomyRole) *float64 { return r.Peak }, Human, false, true},
		{"model calls", func(r *data.AnatomyRole) *float64 { return &r.Calls }, Human, false, true},
		{"dispatches", func(r *data.AnatomyRole) *float64 { return &r.Dispatches }, Human, false, false},
		{"own tool calls / dispatch", func(r *data.AnatomyRole) *float64 { return r.ToolsPerDispatch }, func(v float64) string { return strconv.FormatFloat(v, 'f', 1, 64) }, false, true},
		{"cost per task", func(r *data.AnatomyRole) *float64 { return &r.Cost }, m.kzMoney, false, true},
		{"cost: input", class(func(c *data.AnatomyClasses) *float64 { return &c.Input }), share, true, false},
		{"cost: output", class(func(c *data.AnatomyClasses) *float64 { return &c.Output }), share, true, false},
		{"cost: cache write", class(func(c *data.AnatomyClasses) *float64 { return &c.CacheWrite }), share, true, false},
		{"cost: cache read", class(func(c *data.AnatomyClasses) *float64 { return &c.CacheRead }), share, true, false},
	}
}

// anatomyLatest is the indexes in a.Versions of the latest version with landed tasks and the
// one with landed tasks before it; -1 when there is none.
func anatomyLatest(a data.KaizenAnatomy) (before, latest int) {
	before, latest = -1, -1
	for i, v := range a.Versions {
		if v.Tasks > 0 {
			before, latest = latest, i
		}
	}
	return
}

// roleN is how many landed tasks of the version the role ran in.
func roleN(v data.AnatomyVersion, role string) int {
	if r := v.Roles[role]; r != nil {
		return r.N
	}
	return 0
}

func (m *model) anatomyLines(p paint, w int) block {
	a := m.kz.doc.Summary.Anatomy
	lines := append(m.prWarnings(p, w), m.anatomyVerdict(p, w)...)
	if au := m.anatomyAuthoring(p, w); len(au) > 0 {
		lines = append(lines, "")
		lines = append(lines, au...)
		lines = append(lines, "")
		lines = append(lines, m.authoringChart(p, w, a)...)
	}
	for _, c := range []struct {
		title string
		get   func(*data.AnatomyMedians) *float64
	}{
		{"model calls per task", func(r *data.AnatomyMedians) *float64 { return r.Calls }},
		{"peak context", func(r *data.AnatomyMedians) *float64 { return r.Peak }},
		{"own tool calls per task", func(r *data.AnatomyMedians) *float64 { return r.Tools }},
	} {
		lines = append(lines, "")
		lines = append(lines, m.anatomyChart(p, w, c.title, a, c.get)...)
	}
	lines = append(lines, "")
	return append(lines, m.anatomyRoles(p, w)...)
}

// anatomyChart is one measure over time, a line per role; a role that ran in too few of a
// point's tasks leaves a gap.
func (m *model) anatomyChart(p paint, w int, title string, a data.KaizenAnatomy, get func(*data.AnatomyMedians) *float64) block {
	o := lineOpts{label: Human}
	for _, role := range m.kz.doc.Roles {
		s := lineSeries{name: role, color: m.roleColor(role)}
		for _, pt := range a.Points {
			v := math.NaN()
			if r := pt.Roles[role]; r != nil {
				if x := get(r); x != nil {
					v = *x
				}
			}
			s.vals = append(s.vals, v)
		}
		o.series = append(o.series, s)
	}
	vers := make([]string, len(a.Points))
	for i, pt := range a.Points {
		vers[i] = pt.Version
	}
	return m.pointsChart(p, w, title, fmt.Sprintf("median of %d tasks", a.Window), a.Window, o, vers)
}

// pointsChange tells a share's move from x0 to x1 in percentage points; down is green, up orange.
func (m *model) pointsChange(x1, x0 float64) (string, color.Color) {
	d := int(math.Round((x1 - x0) * 100))
	arrow, col := "≈", m.th.Muted
	switch {
	case d < 0:
		arrow, col = "↓", m.th.Green
	case d > 0:
		arrow, col = "↑", m.th.Orange
	}
	if max(d, -d) == 1 {
		return arrow + " 1 pt", col
	}
	return fmt.Sprintf("%s %d pts", arrow, max(d, -d)), col
}

// anatomyVerdictVersionW caps a version's column: a longer name is cut.
const anatomyVerdictVersionW = 12

// anatomyVerdict is the lead's measures per version and how the latest version with landed
// tasks moved them against the one before; the change column shows only when both have the minimum.
func (m *model) anatomyVerdict(p paint, w int) block {
	th := m.th
	a := m.kz.doc.Summary.Anatomy
	beforeAt, latestAt := anatomyLatest(a)
	if latestAt < 0 {
		return block{m.heading(p, w, "did the latest version move the lead", "landed tasks only"), p.sp(1) + p.t("no landed task with a cost yet", th.Faint)}
	}
	latest := a.Versions[latestAt]
	var before data.AnatomyVersion
	if beforeAt >= 0 {
		before = a.Versions[beforeAt]
	}
	enough := beforeAt >= 0 && roleN(before, "lead") >= a.Min && roleN(latest, "lead") >= a.Min
	measures := m.anatomyMeasures()

	change := func(ms anatomyMeasure) string {
		x0, x1 := ms.get(before.Roles["lead"]), ms.get(latest.Roles["lead"])
		if x0 == nil || x1 == nil {
			return p.t("-", th.Faint)
		}
		arrow, rel, col := m.trendLook(*x1, *x0)
		text := fmt.Sprintf("%s %d%%", arrow, rel)
		if ms.points {
			text, col = m.pointsChange(*x1, *x0)
		}
		if !ms.lower {
			col = th.Muted
		}
		return p.t(text, col)
	}

	labelW := 0
	for _, ms := range measures {
		labelW = max(labelW, width(ms.name))
	}
	colW := func(v data.AnatomyVersion) int {
		cw := max(width(trunc(v.Version, anatomyVerdictVersionW)), width(fmt.Sprintf("n=%d", roleN(v, "lead"))))
		for _, ms := range measures {
			s, _ := ms.cell(v.Roles["lead"], a.Min)
			cw = max(cw, width(s))
		}
		return cw
	}
	valW := 0
	if enough {
		for _, ms := range measures {
			valW = max(valW, width(change(ms)))
		}
	}
	changeHead, changeW := "", 0
	useHead := func(h string) {
		changeHead, changeW = h, 0
		if enough {
			changeHead, changeW = h, max(valW, width(h))
		}
	}
	useHead(before.Version + " → " + latest.Version)
	// the width the table needs for these columns, the label column and the change column
	need := func(cols []data.AnatomyVersion) int {
		used := 1 + labelW
		for _, v := range cols {
			used += 2 + colW(v)
		}
		if enough {
			used += 2 + changeW
		}
		return used
	}
	// as many of the most recent versions as fit, always before and latest
	floor := latestAt
	if beforeAt >= 0 {
		floor = beforeAt
	}
	var cols []data.AnatomyVersion
	for start := 0; start <= floor; start++ {
		if cols = a.Versions[start:]; need(cols) <= w {
			break
		}
	}
	if need(cols) > w {
		cols = a.Versions[floor : latestAt+1]
		if need(cols) > w {
			useHead("change")
		}
	}
	// a label column that does not fit is cut, never the change column
	labelW = max(12, labelW-max(0, need(cols)-w))

	lines := block{m.heading(p, w, "did "+kzVersion(latest.Version)+" move the lead", "medians per landed task")}
	rpad := func(s string, cw int) string { return p.sp(cw-width(s)) + s }
	head, counts := p.sp(1)+p.sp(labelW), p.sp(1)+p.sp(labelW)
	for _, v := range cols {
		head += p.sp(2) + rpad(p.b(trunc(v.Version, anatomyVerdictVersionW), th.Purple), colW(v))
		counts += p.sp(2) + rpad(p.t(fmt.Sprintf("n=%d", roleN(v, "lead")), th.Faint), colW(v))
	}
	if enough {
		head += p.sp(2) + p.t(trunc(changeHead, changeW), th.Faint)
	}
	lines = append(lines, fit(head, w, p), fit(counts, w, p))
	for i, ms := range measures {
		name := th.Muted
		if i < 3 {
			name = th.Text
		}
		line := p.sp(1) + p.t(cell(ms.name, labelW, false), name)
		for _, v := range cols {
			s, faint := ms.cell(v.Roles["lead"], a.Min)
			col := th.Text
			if faint {
				col = th.Faint
			}
			line += p.sp(2) + rpad(p.t(s, col), colW(v))
		}
		if enough {
			line += p.sp(2) + change(ms)
		}
		lines = append(lines, fit(line, w, p))
	}
	lines = append(lines, "")
	switch {
	case beforeAt < 0:
		lines = append(lines, p.sp(1)+p.t("nothing to compare yet: only "+latest.Version+" has landed tasks", th.Faint))
	case !enough:
		var few []string
		for _, v := range []data.AnatomyVersion{before, latest} {
			if n := roleN(v, "lead"); n < a.Min {
				few = append(few, fmt.Sprintf("%s has %d of %d", v.Version, n, a.Min))
			}
		}
		for _, l := range wrapPlain("too few tasks to compare: "+strings.Join(few, ", "), w-2) {
			lines = append(lines, p.sp(1)+p.t(l, th.Faint))
		}
	}
	for _, l := range wrapPlain("first-pass rate and review rounds, what must not move, are on Pulse (← ←)", w-2) {
		lines = append(lines, p.sp(1)+p.t(l, th.Faint))
	}
	return lines
}

// authoringShares is the lead's, the coder's and every other role's part of a version's product
// code and test lines; "-" three times when nobody wrote any.
func authoringShares(au data.Authoring) [3]string {
	if au.Share == nil {
		return [3]string{"-", "-", "-"}
	}
	others := 0.0
	for role, s := range au.Share {
		if role != "lead" && role != "coder" {
			others += s
		}
	}
	return [3]string{pct(au.Share["lead"] * 100), pct(au.Share["coder"] * 100), pct(others * 100)}
}

// anatomyAuthoring is who wrote the product code and tests in every version with landed tasks,
// oldest first; the lead's change (latest against before) shows only when both wrote some in at
// least min tasks, and a version under min is dimmed. Under it, the lead's share in the last
// landed tasks.
func (m *model) anatomyAuthoring(p paint, w int) block {
	th := m.th
	a := m.kz.doc.Summary.Anatomy
	beforeAt, latestAt := anatomyLatest(a)
	if latestAt < 0 {
		return nil
	}
	var rows []data.AnatomyVersion
	for _, v := range a.Versions {
		if v.Tasks > 0 {
			rows = append(rows, v)
		}
	}
	enough := beforeAt >= 0 && a.Versions[beforeAt].Authoring.Tasks >= a.Min && a.Versions[latestAt].Authoring.Tasks >= a.Min
	change, changeCol := "-", th.Faint
	if enough && a.Versions[beforeAt].Authoring.Share != nil && a.Versions[latestAt].Authoring.Share != nil {
		change, changeCol = m.pointsChange(a.Versions[latestAt].Authoring.Share["lead"], a.Versions[beforeAt].Authoring.Share["lead"])
	}

	lines := block{m.heading(p, w, "who wrote the product code and tests", "landed tasks only · share of lines")}
	head := p.sp(1) + p.sp(anatomyVerdictVersionW)
	for _, h := range []string{"lead", "coder", "others"} {
		head += p.sp(2) + p.sp(6-width(h)) + p.t(h, th.Faint)
	}
	head += p.sp(2) + p.t(cell("lead wrote some in", 22, false), th.Faint)
	if enough {
		head += p.sp(2) + p.t("change", th.Faint)
	}
	lines = append(lines, fit(head, w, p))
	for _, v := range rows {
		au := v.Authoring
		col := th.Text
		if au.Tasks < a.Min {
			col = th.Faint
		}
		line := p.sp(1) + p.b(cell(trunc(v.Version, anatomyVerdictVersionW), anatomyVerdictVersionW, false), th.Purple)
		for _, s := range authoringShares(au) {
			line += p.sp(2) + p.t(cell(s, 6, true), col)
		}
		line += p.sp(2) + p.t(cell(fmt.Sprintf("%d of %d tasks", au.LeadTasks, au.Tasks), 22, false), col)
		if enough && v.Version == a.Versions[latestAt].Version {
			line += p.sp(2) + p.t(change, changeCol)
		}
		lines = append(lines, fit(line, w, p))
	}

	var recent []string
	for _, t := range landedTasks(m.kz.doc) {
		s := "-"
		if t.Authoring.LeadShare != nil {
			s = pct(*t.Authoring.LeadShare * 100)
		}
		recent = append(recent, t.Slug+" "+s)
		if len(recent) > 5 {
			recent = recent[1:]
		}
	}
	if len(recent) > 0 {
		lines = append(lines, "")
		for _, l := range wrapPlain("the lead's share in the last landed tasks: "+strings.Join(recent, " · "), w-2) {
			lines = append(lines, p.sp(1)+p.t(l, th.Muted))
		}
	}
	return lines
}

// authoringChart is each role's part of the product code and test lines over time; a point
// with no such line leaves a gap.
func (m *model) authoringChart(p paint, w int, a data.KaizenAnatomy) block {
	o := lineOpts{label: func(v float64) string { return pct(v * 100) }}
	vers := make([]string, len(a.Points))
	for i, pt := range a.Points {
		vers[i] = pt.Version
	}
	for _, role := range m.kz.doc.Roles {
		s := lineSeries{name: role, color: m.roleColor(role)}
		for _, pt := range a.Points {
			v := math.NaN()
			if x, ok := pt.Authoring.Share[role]; ok {
				v = x
			}
			s.vals = append(s.vals, v)
		}
		o.series = append(o.series, s)
	}
	return m.pointsChart(p, w, "share of product code and test lines", fmt.Sprintf("pooled over %d tasks", a.Window), a.Window, o, vers)
}

// anatomyRoles is each role but the lead, latest against before: "before → latest", the
// number of landed tasks the role ran in beside them. Columns drop from the right as the
// width runs out.
func (m *model) anatomyRoles(p paint, w int) block {
	th := m.th
	a := m.kz.doc.Summary.Anatomy
	beforeAt, latestAt := anatomyLatest(a)
	if beforeAt < 0 {
		return block{m.heading(p, w, "the other roles", "latest against before"), p.sp(1) + p.t("nothing to compare yet", th.Faint)}
	}
	before, latest := a.Versions[beforeAt], a.Versions[latestAt]
	all := m.anatomyMeasures()
	byName := func(name string) anatomyMeasure {
		return all[slices.IndexFunc(all, func(ms anatomyMeasure) bool { return ms.name == name })]
	}
	columns := []struct {
		head string
		ms   anatomyMeasure
	}{{"share", byName("share of task cost")}, {"cost", byName("cost per task")}, {"calls", byName("model calls")},
		{"peak", byName("peak context")}, {"tools", byName("own tool calls")}}

	type pair struct {
		a, b           string
		aFaint, bFaint bool
	}
	pw := func(pr pair) int { return width(pr.a) + 3 + width(pr.b) }
	pairOf := func(role string, ms anatomyMeasure) pair {
		var pr pair
		pr.a, pr.aFaint = ms.cell(before.Roles[role], a.Min)
		pr.b, pr.bFaint = ms.cell(latest.Roles[role], a.Min)
		return pr
	}
	var roles []string
	for _, r := range m.kz.doc.Roles {
		if r != "lead" {
			roles = append(roles, r)
		}
	}
	roleW, nW := 0, 1
	for _, r := range roles {
		roleW = max(roleW, width(r))
		nW = max(nW, pw(pair{a: strconv.Itoa(roleN(before, r)), b: strconv.Itoa(roleN(latest, r))}))
	}
	widths := make([]int, len(columns))
	for i, c := range columns {
		widths[i] = width(c.head)
		for _, r := range roles {
			widths[i] = max(widths[i], pw(pairOf(r, c.ms)))
		}
	}
	shown, used := 0, 1+roleW+2+nW
	for shown < len(columns) && (shown == 0 || used+2+widths[shown] <= w) {
		used += 2 + widths[shown]
		shown++
	}

	draw := func(pr pair, cw int) string {
		col := func(faint bool) color.Color {
			if faint {
				return th.Faint
			}
			return th.Text
		}
		return p.sp(cw-pw(pr)) + p.t(pr.a, col(pr.aFaint)) + p.t(" → ", th.Faint) + p.t(pr.b, col(pr.bFaint))
	}
	lines := block{m.heading(p, w, "the other roles", fmt.Sprintf("%s → %s · median per task, - did not run", kzVersion(before.Version), kzVersion(latest.Version)))}
	head := p.sp(1) + p.sp(roleW) + p.sp(2) + p.sp(nW-1) + p.t("n", th.Faint)
	for i, c := range columns[:shown] {
		head += p.sp(2) + p.sp(widths[i]-width(c.head)) + p.t(c.head, th.Faint)
	}
	lines = append(lines, fit(head, w, p))
	for _, r := range roles {
		line := p.sp(1) + p.b(cell(r, roleW, false), m.roleColor(r)) + p.sp(2)
		line += draw(pair{strconv.Itoa(roleN(before, r)), strconv.Itoa(roleN(latest, r)), roleN(before, r) < a.Min, roleN(latest, r) < a.Min}, nW)
		for i, c := range columns[:shown] {
			line += p.sp(2) + draw(pairOf(r, c.ms), widths[i])
		}
		lines = append(lines, fit(line, w, p))
	}
	return lines
}
