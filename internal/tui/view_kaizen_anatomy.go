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
// role and shown. A share's change is told in percentage points, the others' as a percentage.
type anatomyMeasure struct {
	name   string
	get    func(*data.AnatomyRole) (float64, bool)
	show   func(float64) string
	points bool
}

func (m *model) anatomyMeasures() []anatomyMeasure {
	num := func(f func(*data.AnatomyRole) float64) func(*data.AnatomyRole) (float64, bool) {
		return func(r *data.AnatomyRole) (float64, bool) { return f(r), true }
	}
	opt := func(f func(*data.AnatomyRole) *float64) func(*data.AnatomyRole) (float64, bool) {
		return func(r *data.AnatomyRole) (float64, bool) {
			if x := f(r); x != nil {
				return *x, true
			}
			return 0, false
		}
	}
	class := func(f func(*data.AnatomyClasses) float64) func(*data.AnatomyRole) (float64, bool) {
		return func(r *data.AnatomyRole) (float64, bool) {
			if r.Classes == nil {
				return 0, false
			}
			return f(r.Classes), true
		}
	}
	share := func(v float64) string { return pct(v * 100) }
	return []anatomyMeasure{
		{"share of task cost", num(func(r *data.AnatomyRole) float64 { return r.Share }), share, true},
		{"own tool calls", num(func(r *data.AnatomyRole) float64 { return r.Tools }), Human, false},
		{"peak context", opt(func(r *data.AnatomyRole) *float64 { return r.Peak }), Human, false},
		{"model calls", num(func(r *data.AnatomyRole) float64 { return r.Calls }), Human, false},
		{"dispatches", num(func(r *data.AnatomyRole) float64 { return r.Dispatches }), Human, false},
		{"own tool calls / dispatch", opt(func(r *data.AnatomyRole) *float64 { return r.ToolsPerDispatch }), func(v float64) string { return strconv.FormatFloat(v, 'f', 1, 64) }, false},
		{"cost per task", num(func(r *data.AnatomyRole) float64 { return r.Cost }), m.kzMoney, false},
		{"cost: input", class(func(c *data.AnatomyClasses) float64 { return c.Input }), share, true},
		{"cost: output", class(func(c *data.AnatomyClasses) float64 { return c.Output }), share, true},
		{"cost: cache write", class(func(c *data.AnatomyClasses) float64 { return c.CacheWrite }), share, true},
		{"cost: cache read", class(func(c *data.AnatomyClasses) float64 { return c.CacheRead }), share, true},
	}
}

// anatomySplit is the versions with landed tasks, and the newest version when it has none.
func anatomySplit(a data.KaizenAnatomy) (with []data.AnatomyVersion, empty *data.AnatomyVersion) {
	for _, v := range a.Versions {
		if v.Tasks > 0 {
			with = append(with, v)
		}
	}
	if n := len(a.Versions); n > 0 && a.Versions[n-1].Tasks == 0 {
		empty = &a.Versions[n-1]
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
	lines := m.anatomyVerdict(p, w)
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

// anatomyVerdict is the lead's measures per version and how the latest version with landed
// tasks moved them against the one before; the change column shows only when both have the minimum.
func (m *model) anatomyVerdict(p paint, w int) block {
	th := m.th
	a := m.kz.doc.Summary.Anatomy
	with, empty := anatomySplit(a)
	if len(with) == 0 {
		return block{m.heading(p, w, "did the latest version move the lead", "landed tasks only"), p.sp(1) + p.t("no landed task with a cost yet", th.Faint)}
	}
	latest := with[len(with)-1]
	compare := len(with) >= 2
	var before data.AnatomyVersion
	if compare {
		before = with[len(with)-2]
	}
	enough := compare && roleN(before, "lead") >= a.Min && roleN(latest, "lead") >= a.Min
	measures := m.anatomyMeasures()

	text := func(v data.AnatomyVersion, ms anatomyMeasure) (string, bool) {
		r := v.Roles["lead"]
		if r == nil {
			return "-", true
		}
		x, ok := ms.get(r)
		if !ok {
			return "-", true
		}
		return ms.show(x), r.N < a.Min
	}
	change := func(ms anatomyMeasure) string {
		r0, r1 := before.Roles["lead"], latest.Roles["lead"]
		x0, ok0 := ms.get(r0)
		x1, ok1 := ms.get(r1)
		if !ok0 || !ok1 {
			return p.t("-", th.Faint)
		}
		arrow, rel, col := m.trendLook(x1, x0)
		if ms.points {
			return p.t(fmt.Sprintf("%s %d pts", arrow, int(math.Round(math.Abs(x1-x0)*100))), col)
		}
		return p.t(fmt.Sprintf("%s %d%%", arrow, rel), col)
	}

	labelW := 0
	for _, ms := range measures {
		labelW = max(labelW, width(ms.name))
	}
	headOf := func(v data.AnatomyVersion) string {
		return fmt.Sprintf("%s n=%d", v.Version, roleN(v, "lead"))
	}
	colW := func(v data.AnatomyVersion) int {
		cw := width(headOf(v))
		for _, ms := range measures {
			s, _ := text(v, ms)
			cw = max(cw, width(s))
		}
		return cw
	}
	changeW := 0
	if enough {
		changeW = width("change")
		for _, ms := range measures {
			changeW = max(changeW, width(change(ms)))
		}
	}
	fits := func(cols []data.AnatomyVersion) bool {
		used := 1 + labelW
		for _, v := range cols {
			used += 2 + colW(v)
		}
		if enough {
			used += 2 + changeW
		}
		return used <= w
	}
	cands := slices.Clone(with)
	if empty != nil {
		cands = append(cands, *empty)
	}
	startMax := len(with) - min(2, len(with))
	var cols []data.AnatomyVersion
	for start := 0; ; start++ {
		cols = cands[start:]
		if start == startMax || fits(cols) {
			break
		}
	}
	if !fits(cols) && empty != nil {
		cols = with[startMax:]
	}

	lines := block{m.heading(p, w, "did "+kzVersion(latest.Version)+" move the lead", "medians per landed task")}
	rpad := func(s string, cw int) string { return p.sp(cw-width(s)) + s }
	head := p.sp(1) + p.sp(labelW)
	for _, v := range cols {
		h := p.b(v.Version, th.Purple) + p.t(fmt.Sprintf(" n=%d", roleN(v, "lead")), th.Faint)
		head += p.sp(2) + p.sp(colW(v)-width(headOf(v))) + h
	}
	if enough {
		head += p.sp(2) + p.t("change", th.Faint)
	}
	lines = append(lines, fit(head, w, p))
	for i, ms := range measures {
		name := th.Muted
		if i < 3 {
			name = th.Text
		}
		line := p.sp(1) + p.t(cell(ms.name, labelW, false), name)
		for _, v := range cols {
			s, faint := text(v, ms)
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
	case !compare:
		lines = append(lines, p.sp(1)+p.t("nothing to compare yet: only "+latest.Version+" has landed tasks", th.Faint))
	case !enough:
		var few []string
		for _, v := range []data.AnatomyVersion{before, latest} {
			if n := roleN(v, "lead"); n < a.Min {
				few = append(few, fmt.Sprintf("%s has %d of %d", v.Version, n, a.Min))
			}
		}
		lines = append(lines, p.sp(1)+p.t("too few tasks to compare: "+strings.Join(few, ", "), th.Faint))
	}
	for _, l := range wrapPlain("first-pass rate and review rounds, what must not move, are on Pulse (← ←)", w-2) {
		lines = append(lines, p.sp(1)+p.t(l, th.Faint))
	}
	return lines
}

// anatomyRoles is each role but the lead, latest against before: "before → latest", the
// number of landed tasks the role ran in beside them. Columns drop from the right as the
// width runs out.
func (m *model) anatomyRoles(p paint, w int) block {
	th := m.th
	a := m.kz.doc.Summary.Anatomy
	with, _ := anatomySplit(a)
	if len(with) < 2 {
		return block{m.heading(p, w, "the other roles", "latest against before"), p.sp(1) + p.t("nothing to compare yet", th.Faint)}
	}
	before, latest := with[len(with)-2], with[len(with)-1]
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
		side := func(v data.AnatomyVersion) (string, bool) {
			r := v.Roles[role]
			if r == nil {
				return "-", true
			}
			x, ok := ms.get(r)
			if !ok {
				return "-", true
			}
			return ms.show(x), r.N < a.Min
		}
		var pr pair
		pr.a, pr.aFaint = side(before)
		pr.b, pr.bFaint = side(latest)
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
