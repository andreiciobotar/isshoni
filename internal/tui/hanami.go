package tui

import (
	"image/color"
	"math"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/harmonica"
	"github.com/charmbracelet/x/ansi"

	"isshoni/internal/data"
)

// The Hanami scene (花見, flower viewing): the factory's work drawn as motion. It is pure
// state: Apply feeds it events, Step advances it by one fixed frame, draw renders it. It
// never reads a clock and its only randomness is its own seeded generator, so a given
// sequence of events and steps always gives the same frames.

const (
	hanamiFPS    = 20 // springs still read as springs, and a frame of glow costs the terminal layer real work: measured in the README
	hanamiDT     = time.Second / hanamiFPS
	hanamiQuiet  = 3 * time.Minute // a session without events this long is at rest: its blossoms close
	hanamiKeep   = 2 * time.Hour   // how long what was done stays on the tree: closed blossoms, a resting branch
	hanamiSlots  = 12
	maxSparks    = 400
	maxFire      = 900
	maxNotes     = 600
	maxPetals    = 300
	maxLanterns  = 60
	maxRipples   = 40
	maxCanopy    = 14 // blossoms on one branch; the longest closed go first
	maxStars     = 40
	noteDrift    = 4.0  // cells per second, right to left
	noteHold     = 12.0 // seconds a note is held at most while its agent has not called again
	staffRows    = 6
	hanamiSecond = 1.0 / hanamiFPS
)

// One spring per kind of motion, all stepped at the scene's frame rate.
var (
	springSwell   = harmonica.NewSpring(harmonica.FPS(hanamiFPS), 3.0, 0.8)
	springBloom   = harmonica.NewSpring(harmonica.FPS(hanamiFPS), 8.0, 0.32)
	springSize    = harmonica.NewSpring(harmonica.FPS(hanamiFPS), 4.0, 0.5)
	springGrow    = harmonica.NewSpring(harmonica.FPS(hanamiFPS), 2.4, 1.0)
	springLantern = harmonica.NewSpring(harmonica.FPS(hanamiFPS), 2.6, 0.5)
	springPetal   = [3]harmonica.Spring{
		harmonica.NewSpring(harmonica.FPS(hanamiFPS), 2.6, 0.62),
		harmonica.NewSpring(harmonica.FPS(hanamiFPS), 3.2, 0.7),
		harmonica.NewSpring(harmonica.FPS(hanamiFPS), 2.2, 0.55),
	}
)

// sv is a value on a spring.
type sv struct{ x, v float64 }

func (s *sv) to(sp harmonica.Spring, target float64) { s.x, s.v = sp.Update(s.x, s.v, target) }

type hrgb struct{ r, g, b float64 }

func toRGB(c color.Color) hrgb {
	r, g, b := rgbOf(c)
	return hrgb{r, g, b}
}

func (a hrgb) mix(b hrgb, t float64) hrgb {
	t = math.Max(0, math.Min(1, t))
	return hrgb{a.r + (b.r-a.r)*t, a.g + (b.g-a.g)*t, a.b + (b.b-a.b)*t}
}

// lit is the colour with light added, as a lamp adds to a wall.
func (a hrgb) lit(l hrgb) hrgb {
	return hrgb{math.Min(255, a.r+l.r), math.Min(255, a.g+l.g), math.Min(255, a.b+l.b)}
}

func (a hrgb) times(k float64) hrgb { return hrgb{a.r * k, a.g * k, a.b * k} }

// stepped rounds each channel to a multiple of n.
func (a hrgb) stepped(n float64) hrgb {
	return hrgb{math.Min(255, math.Round(a.r/n)*n), math.Min(255, math.Round(a.g/n)*n), math.Min(255, math.Round(a.b/n)*n)}
}

// hrand is a xorshift generator: the scene's only source of variation.
type hrand uint64

func (r *hrand) f() float64 {
	x := uint64(*r)
	x ^= x << 13
	x ^= x >> 7
	x ^= x << 17
	*r = hrand(x)
	return float64(x>>11) / (1 << 53)
}

func (r *hrand) span(a, b float64) float64 { return a + (b-a)*r.f() }

// frac spreads an index over 0..1 by the golden ratio, so neighbours land apart.
func frac(i int, off float64) float64 {
	_, f := math.Modf(float64(i)*0.618034 + off)
	return f
}

// Tool kinds, top line of the staff first: the rarest highest, the shell as the bass.
var hanamiTools = []struct {
	kind  string
	glyph rune
}{{"message", '◇'}, {"dispatch", '▲'}, {"write", '■'}, {"edit", '◆'}, {"read", '○'}, {"shell", '●'}}

// roleGlyph is the heart of a role's blossom, so the role reads without colour too.
func roleGlyph(role string) rune {
	switch role {
	case "coder":
		return '✿'
	case "reviewer":
		return '❀'
	case "explorer", "Explore":
		return '✾'
	}
	return '✽'
}

type hBlossom struct {
	agent, role string
	n           int       // order on its branch: where it sits
	open        sv        // 0 a bud, 1 open
	work        float64   // what its agent has done: tool calls, and output tokens by the 800
	size        sv        // radius in dots, following work
	heat        float64   // 1 at its agent's event, decaying
	closed      time.Time // when its agent returned (or its session came to rest); zero while it works
}

type hBranch struct {
	session  string
	slot     int
	jitter   float64 // radians, so two sessions sharing a slot do not coincide
	grow     sv
	heat     float64 // 1 at an event, decaying: the tip glows
	last     time.Time
	blossoms []*hBlossom
	spawned  int
}

type hPetal struct {
	x, y       sv
	ox, oy     float64 // where it let go
	col        hrgb
	spring     int
	delay      int  // frames before it moves
	bounce     bool // changes needed: turned back at the disc
	lead       bool // the first of its five: where it turns back, a red ring marks the refusal
	back       bool // on its way back out
	life, max_ float64
}

type hSpark struct {
	x, y, vx, vy, life, max_ float64
	col                      hrgb
}

type hWind struct{ u, v, speed, phase float64 }

// hNote is one tool call on the staff: struck at x0, sounding until its agent's next call.
type hNote struct {
	x0, x1 float64 // cell columns of its start and its end; the end stays at the playhead while it is held
	row    int
	glyph  rune
	c      hrgb
	who    string  // the agent holding it
	held   float64 // seconds held so far; 0 once released
	age    float64
	n      int
}

type hLantern struct {
	key    string // repository and number
	pr     int
	slot   int
	x, y   sv
	ci     string
	placed bool    // false until its position is first set from the geometry
	burst  bool    // bursts when it reaches the sky (a merge seen without its opening)
	gone   float64 // seconds since it was closed without a merge: it fades for one, then is removed
}

type hFire struct {
	p            *harmonica.Projectile
	trail        [5][2]float64 // where it was, newest first
	life, max_   float64
	col          hrgb
	delay, twink int
	star         bool // a few carry a star while they are bright
}

// hStar is what a merge leaves in the sky where its lantern burst.
type hStar struct {
	u, v float64 // position as a share of the field, so a resize keeps it
	at   time.Time
}

type hBell struct {
	slug string
	slot int
}

type hRipple struct {
	x, y, r, speed, maxR, alpha float64
	col                         hrgb
	fromDisc                    bool
}

type hanamiScene struct {
	th    *Theme
	w, h  int // cells
	t     float64
	frame int
	rnd   hrand
	now   time.Time // event time: the newest event applied, or the replay clock
	scale float64   // event seconds per second: 1 live, the speed in a replay. Rates are per event second.

	acc, rate           float64 // events this frame; events per second, smoothed
	sparkDebt           float64 // sparks owed: a fast replay thins them, so fractions carry over
	cacheAcc, cacheRate float64 // cache-read tokens likewise
	swell, flash        sv
	flashCol            hrgb
	forte               float64 // 1 at a merge, decaying: the whole stage is lit

	branches []*hBranch
	petals   []hPetal
	sparks   []hSpark
	wind     []hWind
	windN    sv
	notes    []hNote
	beat     [staffRows]float64 // 1 at a note on the voice, decaying
	voice    [staffRows]sv      // how loud each voice is: its calls per second, smoothed
	voiceAcc [staffRows]float64
	voiceCol [staffRows]hrgb
	lanterns []*hLantern
	fire     []hFire
	stars    []hStar
	bells    []*hBell
	ripples  []hRipple
}

func newHanamiScene(th *Theme) *hanamiScene {
	s := &hanamiScene{th: th, w: 80, h: 21, scale: 1, rnd: 0x9E3779B97F4A7C15, flashCol: toRGB(th.Text)}
	for i := 0; i < 900; i++ {
		s.wind = append(s.wind, hWind{u: s.rnd.f(), v: s.rnd.f(), speed: s.rnd.span(0.6, 1.5), phase: s.rnd.span(0, 6.28)})
	}
	for i := range s.voiceCol {
		s.voiceCol[i] = toRGB(th.Muted)
	}
	return s
}

// ---- geometry: recomputed from the size every frame, so a resize leaves nothing behind

type hGeo struct {
	W, H      float64 // the field in dots (2 per cell across, 4 down)
	staffTop  int     // first cell row of the staff; h when there is none
	cx, cy, R float64
}

func (s *hanamiScene) geo() hGeo {
	staff := staffRows
	if s.h < 14 {
		staff = 0
	}
	rows := s.h - staff
	g := hGeo{W: float64(2 * s.w), H: float64(4 * rows), staffTop: rows}
	g.R = math.Max(7, math.Min(g.H*0.26, g.W*0.105))
	g.cx = g.W * 0.15
	g.cy = g.H - g.R - 3
	return g
}

// The branches fan up and to the right of the disc, as the blossoms of the mark do.
var slotAngles = [hanamiSlots]float64{44, 24, 63, 34, 54, 14, 73, 29, 59, 19, 68, 49}

// point is where a branch is at u in 0..1 of its grown length.
func (b *hBranch) point(g hGeo, t, u float64) (x, y float64) {
	a := slotAngles[b.slot]*math.Pi/180 + b.jitter
	dx, dy := math.Cos(a), -math.Sin(a)
	sx, sy := g.cx+(g.R+1)*dx, g.cy+(g.R+1)*dy
	reach := math.Min((g.W-6-sx)/math.Max(dx, 0.05), (sy-5)/math.Max(-dy, 0.05))
	L := math.Max(6, reach*(0.78+0.22*frac(b.slot, 0.3)))
	bend := 0.05
	if b.slot%2 == 1 {
		bend = -0.04
	}
	side := math.Sin(math.Pi*u) * L * bend // still: a tree that swayed would repaint its whole canopy every frame
	return sx + dx*u*L - dy*side, sy + dy*u*L + dx*side
}

// blossomAt is where a blossom sits: on a short twig off its branch.
func (b *hBranch) blossomAt(g hGeo, t float64, bl *hBlossom) (x, y, bx, by float64) {
	u := (0.2 + 0.8*frac(bl.n, 0.21)) * b.grow.x
	bx, by = b.point(g, t, u)
	x2, y2 := b.point(g, t, math.Min(1, u+0.02))
	nx, ny := -(y2 - by), x2-bx
	if l := math.Hypot(nx, ny); l > 0 {
		nx, ny = nx/l, ny/l
	}
	twig := (2.5 + 0.7*bl.size.x + 3*frac(bl.n, 0.7)) * b.grow.x
	if bl.n%2 == 1 {
		twig = -twig
	}
	return bx + nx*twig, by + ny*twig, bx, by
}

// blossomSize is the radius, in dots, a blossom grows to for the work its agent has done.
func blossomSize(g hGeo, work float64) float64 {
	return math.Min(3.4+1.8*math.Sqrt(work), math.Max(7, g.R*0.48))
}

func (s *hanamiScene) lanternTarget(g hGeo, l *hLantern) (x, y float64) {
	x = g.W * (0.36 + 0.6*frac(l.slot, 0.1))
	y = 8 + float64(l.slot%3)*g.H*0.08
	return x + math.Sin(s.t*0.7+float64(l.slot))*1.6, y + math.Sin(s.t*0.5+float64(l.slot)*2)*0.8 // ambient sway
}

// ---- events ---------------------------------------------------------------------------

func (s *hanamiScene) branch(session string, at time.Time) *hBranch {
	for _, b := range s.branches {
		if b.session == session {
			if at.After(b.last) {
				b.last = at
			}
			return b
		}
	}
	used := [hanamiSlots]int{}
	for _, b := range s.branches {
		used[b.slot]++
	}
	slot := 0
	for i := range used {
		if used[i] < used[slot] {
			slot = i
		}
	}
	b := &hBranch{session: session, slot: slot, last: at, jitter: float64(used[slot]) * 0.07}
	s.branches = append(s.branches, b)
	return b
}

// touch is the agent's blossom on the branch, reopened if it had closed, with work added; nil for the
// session's own work, which shows at the branch tip.
func (s *hanamiScene) touch(g hGeo, b *hBranch, agent string, work float64) *hBlossom {
	for _, bl := range b.blossoms {
		if bl.agent == agent {
			bl.closed = time.Time{}
			bl.work += work
			return bl
		}
	}
	return nil
}

// origin is where an agent's events come from: its blossom, or its session's branch tip.
func (s *hanamiScene) origin(g hGeo, b *hBranch, bl *hBlossom) (x, y float64) {
	if bl != nil {
		x, y, _, _ = b.blossomAt(g, s.t, bl)
		return
	}
	return b.point(g, s.t, b.grow.x)
}

func prKey(e data.Event) string { return e.Repo + "#" + strconv.Itoa(e.PR) }

// lit is the index of the event's pull request's lantern if one hangs and is not going out, else -1.
func (s *hanamiScene) lit(e data.Event) int {
	key := prKey(e)
	return slices.IndexFunc(s.lanterns, func(l *hLantern) bool { return l.key == key && l.gone == 0 })
}

// lantern is a pull request's lantern, lit if it was not. One that was already open when the viewer arrived
// (settled) hangs in the sky at once; a new one rises from the disc.
func (s *hanamiScene) lantern(g hGeo, e data.Event, settled bool) *hLantern {
	if i := s.lit(e); i >= 0 {
		return s.lanterns[i]
	}
	slot := 0
	for slices.ContainsFunc(s.lanterns, func(l *hLantern) bool { return l.slot == slot }) {
		slot++
	}
	l := &hLantern{key: prKey(e), pr: e.PR, slot: slot}
	if len(s.lanterns) >= maxLanterns {
		return l // the sky is full: this one is not hung, and what the caller sets on it is dropped
	}
	if settled {
		l.x.x, l.y.x = s.lanternTarget(g, l)
		l.placed = true
	}
	s.lanterns = append(s.lanterns, l)
	return l
}

// Apply feeds one event to the scene. A settled event is one that happened before the
// viewer arrived: it sets what exists (the tree and what it has borne, lanterns, bells)
// without any motion.
func (s *hanamiScene) Apply(e data.Event, settled bool) {
	g := s.geo()
	if e.TS.After(s.now) {
		s.now = e.TS
	}
	col := toRGB(s.th.AgentColor(e.Role))
	switch e.Kind {
	case "tool", "tokens":
		b := s.branch(e.Session, e.TS)
		work := 1.0
		if e.Kind == "tokens" {
			work = float64(e.Out) / 800
		}
		bl := s.touch(g, b, e.Agent, work)
		if settled {
			b.grow.x = 1
			if bl != nil {
				bl.size.x = blossomSize(g, bl.work)
			}
			return
		}
		s.acc++
		b.heat = 1
		if bl != nil {
			bl.heat = 1
		}
		if e.Kind == "tool" {
			s.note(g, e.Tool, col, e.Session+"/"+e.Agent)
			return
		}
		s.cacheAcc += float64(e.CacheRead)
		if e.Out > 0 {
			x, y := s.origin(g, b, bl)
			s.sparkDebt = math.Min(8, s.sparkDebt+float64(min(8, 2+e.Out/400))/math.Sqrt(s.scale))
			for ; s.sparkDebt >= 1 && len(s.sparks) < maxSparks; s.sparkDebt-- {
				life := s.rnd.span(1.4, 3.0)
				s.sparks = append(s.sparks, hSpark{x: x, y: y, vx: s.rnd.span(30, 70), vy: s.rnd.span(-22, 2), life: life, max_: life, col: col})
			}
		}
	case "spawn":
		b := s.branch(e.Session, e.TS)
		if s.touch(g, b, e.Agent, 0) != nil {
			return
		}
		bl := &hBlossom{agent: e.Agent, role: e.Role, n: b.spawned}
		b.spawned++
		if settled {
			bl.open.x, bl.size.x, b.grow.x = 1, blossomSize(g, 0), 1
		} else {
			s.acc++
			bl.heat = 1
		}
		b.blossoms = append(b.blossoms, bl)
		if len(b.blossoms) > maxCanopy { // the longest closed makes room
			old := -1
			for i, x := range b.blossoms {
				if !x.closed.IsZero() && (old < 0 || x.closed.Before(b.blossoms[old].closed)) {
					old = i
				}
			}
			b.blossoms = slices.Delete(b.blossoms, max(old, 0), max(old, 0)+1)
		}
	case "return":
		b := s.branch(e.Session, e.TS)
		i := slices.IndexFunc(b.blossoms, func(bl *hBlossom) bool { return bl.agent == e.Agent && bl.closed.IsZero() })
		if i < 0 {
			return
		}
		bl := b.blossoms[i]
		bl.closed = e.TS // it stays on the branch, closed: what the branch has borne
		if settled {
			return
		}
		s.acc++
		x, y, _, _ := b.blossomAt(g, s.t, bl)
		for k := 0; k < 5 && len(s.petals) < maxPetals; k++ {
			a := s.rnd.span(0, 2*math.Pi)
			s.petals = append(s.petals, hPetal{
				x: sv{x, 30 * math.Cos(a)}, y: sv{y, 30 * math.Sin(a)}, ox: x, oy: y, col: col,
				spring: k % 3, delay: k * 2, bounce: e.Verdict == "changes", lead: k == 0, max_: 2.2,
			})
		}
	case "pr":
		switch {
		case e.State != "closed":
			s.lantern(g, e, settled)
		case s.lit(e) < 0:
		case settled:
			s.lanterns = slices.Delete(s.lanterns, s.lit(e), s.lit(e)+1)
		default: // closed without a merge: it goes out
			s.lanterns[s.lit(e)].gone = hanamiSecond
		}
	case "ci":
		s.lantern(g, e, settled).ci = e.State
	case "merge":
		i := s.lit(e)
		switch {
		case settled && i >= 0:
			l := s.lanterns[i]
			s.lanterns = slices.Delete(s.lanterns, i, i+1)
			s.star(g, l.x.x, l.y.x, e.TS)
		case settled:
		case i >= 0 && s.lanterns[i].placed:
			l := s.lanterns[i]
			s.lanterns = slices.Delete(s.lanterns, i, i+1)
			s.hanabi(g, l.x.x, l.y.x)
		default:
			s.lantern(g, e, false).burst = true // rises first, then bursts
		}
	case "question":
		if slices.ContainsFunc(s.bells, func(b *hBell) bool { return b.slug == e.Slug }) {
			return
		}
		slot := 0
		for slices.ContainsFunc(s.bells, func(b *hBell) bool { return b.slot == slot }) {
			slot++
		}
		s.bells = append(s.bells, &hBell{slug: e.Slug, slot: slot})
		if !settled {
			x, y := bellAt(g, slot)
			s.ripple(hRipple{x: x, y: y, r: 5, speed: 24, maxR: 34, alpha: 1, col: toRGB(s.th.Text)})
		}
	case "answer":
		s.bells = slices.DeleteFunc(s.bells, func(b *hBell) bool { return b.slug == e.Slug })
	case "watch":
		if !settled {
			s.ripple(hRipple{x: g.cx, y: g.cy, r: g.R, speed: 75, maxR: math.Hypot(g.W, g.H), alpha: 0.7, col: toRGB(s.th.Purple), fromDisc: true})
		}
	}
}

// flicker is a fixed coin per (moment, lantern, dot): drawing never touches the generator.
func flicker(moment, pr, k int) bool {
	x := uint64(moment)*0x9E3779B97F4A7C15 ^ uint64(pr*31+k)*0xC2B2AE3D27D4EB4F
	x ^= x >> 29
	return (x*0xBF58476D1CE4E5B9)>>63 == 1
}

// bellAt is where a waiting question's point sits, in dots: above the disc's left shoulder, where no
// branch grows.
func bellAt(g hGeo, slot int) (x, y float64) {
	return math.Max(7, g.cx-0.8*g.R) + 14*float64(slot), math.Max(7, g.cy-g.R-13)
}

func (s *hanamiScene) ripple(r hRipple) {
	if len(s.ripples) < maxRipples {
		s.ripples = append(s.ripples, r)
	}
}

// star leaves a merge's mark where its lantern was.
func (s *hanamiScene) star(g hGeo, x, y float64, at time.Time) {
	if len(s.stars) >= maxStars {
		s.stars = s.stars[1:]
	}
	st := hStar{u: x / g.W, v: y / g.H, at: at}
	for i := 0; i < len(s.stars); i++ { // lanterns reuse their places: a later star sits beside an earlier one
		if o := s.stars[i]; math.Abs(o.u-st.u)*g.W < 4 && math.Abs(o.v-st.v)*g.H < 3 {
			st.u += 6 / g.W
			i = -1
		}
	}
	s.stars = append(s.stars, st)
}

// note strikes a tool call on its voice. It sounds until who calls again, so who's held note is released.
func (s *hanamiScene) note(g hGeo, tool string, col hrgb, who string) {
	if g.staffTop >= s.h {
		return
	}
	row, glyph := staffRows-1, '·' // any other tool: a dot on the bass line
	for i, t := range hanamiTools {
		if t.kind == tool {
			row, glyph = i, t.glyph
		}
	}
	s.beat[row] = 1
	s.voiceAcc[row]++
	s.voiceCol[row] = col
	head := float64(s.w - 5)
	for i := len(s.notes) - 1; i >= 0 && i >= len(s.notes)-24; i-- {
		n := &s.notes[i]
		if n.who == who {
			n.held = 0
		}
		if n.row == row && head-n.x0 < 1 { // struck within the same cell: one louder note
			n.n++
			n.age, n.who, n.held = 0, who, hanamiSecond // and it is this caller's note now, sounding
			if glyph != '·' {
				n.glyph, n.c = glyph, col
			}
			return
		}
	}
	if len(s.notes) < maxNotes {
		s.notes = append(s.notes, hNote{x0: head, x1: head, row: row, glyph: glyph, c: col, who: who, held: hanamiSecond, n: 1})
	}
}

// hanabi is a merge: the firework where the lantern was, the whole stage lit, and a star left behind.
func (s *hanamiScene) hanabi(g hGeo, x, y float64) {
	size := math.Max(0.9, math.Min(g.W, 2*g.H)/170)
	dt := harmonica.FPS(hanamiFPS)
	for ring := 0; ring < 3; ring++ {
		n := 96 - 30*ring
		for i := 0; i < n && len(s.fire) < maxFire; i++ {
			a := 2*math.Pi*float64(i)/float64(n) + s.rnd.span(-0.06, 0.06)
			v := s.rnd.span(58, 66) * size * (1 - 0.3*float64(ring))
			col := toRGB(s.th.gradientAt(s.rnd.f()))
			if i%5 == 0 {
				col = toRGB(s.th.Yellow)
			}
			life := s.rnd.span(1.7, 3.0)
			f := hFire{
				p:    harmonica.NewProjectile(dt, harmonica.Point{X: x, Y: y}, harmonica.Vector{X: v * math.Cos(a), Y: v * math.Sin(a)}, harmonica.Vector{Y: 11 * size}),
				life: life, max_: life, col: col, delay: ring * 6, star: i%6 == 0,
			}
			for k := range f.trail {
				f.trail[k] = [2]float64{x, y}
			}
			s.fire = append(s.fire, f)
		}
	}
	s.star(g, x, y, s.now)
	s.forte = 1
	s.flash.v += 6
	s.flashCol = toRGB(s.th.Pink)
}

// ---- one frame ------------------------------------------------------------------------

// Step advances the scene by one frame. now is event time: the wall clock when live,
// the replay clock in a replay.
func (s *hanamiScene) Step(now time.Time) {
	const dt = hanamiSecond
	g := s.geo()
	s.t += dt
	s.frame++
	if now.After(s.now) {
		s.now = now
	}
	s.rate += (s.acc/dt/s.scale - s.rate) * dt / 0.9
	s.cacheRate += (s.cacheAcc/dt/s.scale - s.cacheRate) * dt / 1.5
	s.acc, s.cacheAcc = 0, 0
	s.swell.to(springSwell, 1-math.Exp(-s.rate/2.5))
	s.flash.to(springSwell, 0)
	s.windN.to(springSwell, s.windTarget(g))
	s.forte *= 1 - dt/1.3

	for i := 0; i < len(s.branches); i++ {
		b := s.branches[i]
		quiet := s.now.Sub(b.last)
		target := 1.0
		if quiet > hanamiKeep {
			target = 0
		}
		b.grow.to(springGrow, target)
		b.heat *= 1 - dt/0.7
		kept := b.blossoms[:0]
		for _, bl := range b.blossoms {
			if bl.closed.IsZero() && quiet > hanamiQuiet {
				bl.closed = b.last // the session is at rest: whoever had not returned is no longer working
			}
			bl.open.to(springBloom, 1)
			bl.size.to(springSize, blossomSize(g, bl.work))
			bl.heat *= 1 - dt/0.6
			if bl.closed.IsZero() || s.now.Sub(bl.closed) < hanamiKeep {
				kept = append(kept, bl)
			}
		}
		b.blossoms = kept
		if target == 0 && b.grow.x < 0.03 {
			s.branches = slices.Delete(s.branches, i, i+1)
			i--
		}
	}
	for len(s.stars) > 0 && s.now.Sub(s.stars[0].at) > 3*hanamiKeep {
		s.stars = s.stars[1:]
	}

	c := s.cacheLevel()
	gust := 1 + 2.5*s.forte
	for i := range s.wind {
		p := &s.wind[i]
		far := 1.0
		if i%3 == 0 {
			far = 0.45 // a far layer, slower
		}
		p.u += (0.03 + 0.13*c) * p.speed * far * gust * dt
		if p.u > 1 {
			p.u, p.v = 0, s.rnd.f()
		}
	}

	kept := s.petals[:0]
	for _, p := range s.petals {
		if p.delay > 0 {
			p.delay--
			kept = append(kept, p)
			continue
		}
		sp := springPetal[p.spring]
		if p.back {
			p.x.to(sp, p.ox)
			p.y.to(sp, p.oy)
			if p.life += dt; p.life < p.max_ {
				kept = append(kept, p)
			}
			continue
		}
		p.x.to(sp, g.cx)
		p.y.to(sp, g.cy)
		if math.Hypot(p.x.x-g.cx, p.y.x-g.cy) < g.R*0.9 {
			if p.bounce { // turned back: a swirl sideways, then out the way it came
				p.back, p.col = true, toRGB(s.th.Red)
				if p.lead {
					s.ripple(hRipple{x: p.x.x, y: p.y.x, r: 2, speed: 18, maxR: 16, alpha: 1, col: p.col})
				}
				p.x.v, p.y.v = -p.y.v*1.6, p.x.v*1.6
				kept = append(kept, p)
			} else {
				s.flash.v += 1.2
				s.flashCol = p.col
			}
			continue
		}
		kept = append(kept, p)
	}
	s.petals = kept

	sparks := s.sparks[:0]
	for _, p := range s.sparks {
		p.x += p.vx * dt
		p.y += p.vy * dt
		p.vy *= 1 - dt*0.8
		if p.life -= dt; p.life > 0 && p.x < g.W {
			sparks = append(sparks, p)
		}
	}
	s.sparks = sparks

	drift := noteDrift * math.Min(5, math.Cbrt(s.scale)) * dt // a fast replay turns the page faster
	notes := s.notes[:0]
	for _, n := range s.notes {
		n.x0 -= drift
		n.age += dt
		if n.held > 0 {
			if n.held += dt; n.held > noteHold {
				n.held = 0
			}
		} else {
			n.x1 -= drift
		}
		if n.x1 >= 3 {
			notes = append(notes, n)
		}
	}
	s.notes = notes
	for i := range s.beat {
		s.beat[i] *= 1 - dt/0.25
		s.voice[i].to(springSwell, 1-math.Exp(-s.voiceAcc[i]/dt/s.scale/0.5))
		s.voiceAcc[i] = 0
	}

	for i := 0; i < len(s.lanterns); i++ {
		l := s.lanterns[i]
		if !l.placed {
			l.x.x, l.y.x, l.placed = g.cx, g.cy-g.R, true
		}
		if l.gone > 0 {
			if l.gone += dt; l.gone >= 1 {
				s.lanterns = slices.Delete(s.lanterns, i, i+1)
				i--
			}
			continue
		}
		tx, ty := s.lanternTarget(g, l)
		l.x.to(springLantern, tx)
		l.y.to(springLantern, ty)
		if l.burst && math.Abs(l.y.x-ty) < 3 {
			s.lanterns = slices.Delete(s.lanterns, i, i+1)
			i--
			s.hanabi(g, l.x.x, l.y.x)
		}
	}

	fire := s.fire[:0]
	for _, f := range s.fire {
		if f.delay > 0 {
			f.delay--
			fire = append(fire, f)
			continue
		}
		pos := f.p.Position()
		copy(f.trail[1:], f.trail[:])
		f.trail[0] = [2]float64{pos.X, pos.Y}
		f.p.Update()
		v := f.p.Velocity() // air drag, so the burst hangs before it falls
		*f.p = *harmonica.NewProjectile(harmonica.FPS(hanamiFPS), f.p.Position(), harmonica.Vector{X: v.X * 0.955, Y: v.Y * 0.955}, f.p.Acceleration())
		f.twink++
		if f.life -= dt; f.life > 0 {
			fire = append(fire, f)
		}
	}
	s.fire = fire

	ripples := s.ripples[:0]
	for _, r := range s.ripples {
		if r.r += r.speed * dt; r.r < r.maxR {
			ripples = append(ripples, r)
		}
	}
	s.ripples = ripples
}

func (s *hanamiScene) cacheLevel() float64 { return 1 - math.Exp(-s.cacheRate/60000) }

// windTarget is how many wind dots fly: a few always (ambient), more with cache reads.
func (s *hanamiScene) windTarget(g hGeo) float64 {
	most := math.Min(float64(len(s.wind)), g.W*g.H/120)
	return most * (0.3 + 0.7*s.cacheLevel())
}

// busy says whether anything beyond the ambient breeze is in motion (the tests' definition of idle).
func (s *hanamiScene) busy() bool {
	return len(s.petals)+len(s.sparks)+len(s.notes)+len(s.fire)+len(s.ripples) > 0
}

// ---- drawing --------------------------------------------------------------------------

type hCell struct {
	mask   uint8
	a      float64 // the strongest dot of the front-most layer: its colour is the cell's
	layer  int
	ch     rune
	c      hrgb // of the glyph, or of the dots
	bold   bool
	skip   bool // the right half of a double-width glyph
	light  hrgb // glow falling on the cell: added to its background
	fill   hrgb // a solid body behind the cell (the disc, the legend), in place of background and glow
	filled bool
}

// hCanvas is the frame being drawn: braille dots and glyphs in front, light behind. A braille cell has one
// colour, so the dots of the front-most layer in a cell decide it, and among those the strongest: wind
// behind a blossom does not grey the blossom, and two colours of a firework do not average to mud.
type hCanvas struct {
	w, h  int
	maxY  int // dots at or below this row are not drawn: the staff is not part of the field
	layer int
	cells []hCell
}

var brailleBit = [4][2]uint8{{0x01, 0x08}, {0x02, 0x10}, {0x04, 0x20}, {0x40, 0x80}}

func (cv *hCanvas) plot(x, y float64, c hrgb, a float64) {
	xi, yi := int(math.Floor(x)), int(math.Floor(y))
	if xi < 0 || yi < 0 || xi >= 2*cv.w || yi >= cv.maxY || a <= 0 {
		return
	}
	cell := &cv.cells[(yi/4)*cv.w+xi/2]
	cell.mask |= brailleBit[yi%4][xi%2]
	if cv.layer < cell.layer || (cv.layer == cell.layer && a <= cell.a) {
		return
	}
	cell.layer, cell.c, cell.a = cv.layer, c, math.Min(1, a)
}

// glow lights the cells around a point in the field (dots), strongest at the point, nothing at radius r.
func (cv *hCanvas) glow(x, y, r float64, c hrgb, k float64) {
	if r <= 0 || k <= 0 {
		return
	}
	for cy := max(0, int((y-r)/4)); cy <= min(cv.maxY/4-1, int((y+r)/4)); cy++ {
		for cx := max(0, int((x-r)/2)); cx <= min(cv.w-1, int((x+r)/2)); cx++ {
			d := math.Hypot(float64(2*cx+1)-x, float64(4*cy+2)-y) / r
			if d < 1 {
				f := k * (1 - d) * (1 - d)
				l := &cv.cells[cy*cv.w+cx].light
				l.r, l.g, l.b = l.r+c.r*f, l.g+c.g*f, l.b+c.b*f
			}
		}
	}
}

func (cv *hCanvas) glyph(cx, cy int, ch rune, c hrgb, bold bool) {
	if cx < 0 || cy < 0 || cx >= cv.w || cy >= cv.h {
		return
	}
	cell := &cv.cells[cy*cv.w+cx]
	cell.ch, cell.c, cell.bold, cell.skip = ch, c, bold, false
}

// text writes s from (cx, cy), clipped to the canvas; double-width runes take two cells.
func (cv *hCanvas) text(cx, cy int, s string, c hrgb, bold bool) int {
	for _, r := range s {
		w := ansi.StringWidth(string(r))
		if cx < 0 || cy < 0 || cy >= cv.h || cx+w > cv.w {
			break
		}
		cv.glyph(cx, cy, r, c, bold)
		if w == 2 {
			cv.cells[cy*cv.w+cx+1].skip = true
		}
		cx += w
	}
	return cx
}

// flower draws a five-petalled blossom of radius r dots: filled while its agent works, an outline once it
// has closed.
func (cv *hCanvas) flower(x, y, r, rot float64, c hrgb, a float64, outline bool) {
	along, across := math.Max(1, 0.46*r), math.Max(0.8, 0.25*r)
	for k := 0; k < 5; k++ {
		ang := rot + 2*math.Pi*float64(k)/5 - math.Pi/2
		ca, sa := math.Cos(ang), math.Sin(ang)
		px, py := x+0.6*r*ca, y+0.6*r*sa
		for dy := -along - 1; dy <= along+1; dy++ {
			for dx := -along - 1; dx <= along+1; dx++ {
				l, t := (dx*ca+dy*sa)/along, (-dx*sa+dy*ca)/across
				e := l*l + t*t
				if e > 1 || (outline && r >= 4.5 && e < 0.5) {
					continue
				}
				cv.plot(px+dx, py+dy, c, a*(1-0.3*e))
			}
		}
	}
}

func (cv *hCanvas) lines(th *Theme) []string {
	base := toRGB(th.Bg)
	out := make([]string, cv.h)
	var sb strings.Builder
	num := func(v float64) { sb.WriteString(strconv.Itoa(int(v + 0.5))) }
	rgb := func(lead string, c hrgb) {
		sb.WriteString(lead)
		num(c.r)
		sb.WriteByte(';')
		num(c.g)
		sb.WriteByte(';')
		num(c.b)
		sb.WriteByte('m')
	}
	key := func(c hrgb, extra int) [4]int { return [4]int{int(c.r + 0.5), int(c.g + 0.5), int(c.b + 0.5), extra} }
	for y := 0; y < cv.h; y++ {
		sb.Reset()
		var lastFg, lastBg [4]int
		for x := 0; x < cv.w; x++ {
			cell := cv.cells[y*cv.w+x]
			if cell.skip {
				continue
			}
			bg := base.lit(cell.light)
			if cell.filled {
				bg = cell.fill
			}
			// colours in steps: a glow that breathes, or dots that brighten as the stage gets louder, then change a
			// cell every few frames, not every frame, and the terminal has a fraction of the cells to repaint
			bg = bg.stepped(4)
			ch, c, bold := cell.ch, cell.c, cell.bold
			switch {
			case ch != 0:
			case cell.mask != 0:
				ch = rune(0x2800 + int(cell.mask))
				c = bg.mix(c, cell.a).stepped(8)
			default:
				ch = ' '
			}
			if !th.NoColor {
				if k := key(bg, 1); k != lastBg {
					rgb("\x1b[48;2;", bg)
					lastBg = k
				}
				b := 1
				if bold {
					b = 2
				}
				if k := key(c, b); ch != ' ' && k != lastFg {
					if bold {
						rgb("\x1b[1;38;2;", c)
					} else {
						rgb("\x1b[22;38;2;", c)
					}
					lastFg = k
				}
			}
			sb.WriteRune(ch)
		}
		if !th.NoColor {
			sb.WriteString("\x1b[0m")
		}
		out[y] = sb.String()
	}
	return out
}

// draw renders the scene into exactly h lines of w cells.
func (s *hanamiScene) draw(legend bool) []string {
	w, h := s.w, s.h
	th := s.th
	g := s.geo()
	cv := &hCanvas{w: w, h: h, maxY: int(g.H), cells: make([]hCell, w*h)}
	bg, faint, muted, text := toRGB(th.Bg), toRGB(th.Faint), toRGB(th.Muted), toRGB(th.Text)
	level := math.Max(0, math.Min(1.3, s.swell.x)) // how busy: the dynamics of the whole stage
	fl := math.Max(0, math.Min(1, s.flash.x))
	forte := math.Max(0, math.Min(1, s.forte))
	loud := 0.75 + 0.25*math.Min(1, level) + 0.3*forte // everything is a little brighter when it is busy
	inDisc := func(x, y float64) bool { return math.Hypot(x-g.cx, y-g.cy) < g.R+1.5 }
	pink := toRGB(th.Pink)

	// a merge lights the whole stage for a moment
	if forte > 0.02 {
		for i := range cv.cells[:g.staffTop*w] {
			cv.cells[i].light = pink.mix(text, 0.5).times(0.16 * forte)
		}
	}

	// wind: cache reads, behind everything; a far layer and a near one
	c := s.cacheLevel()
	windN := int(s.windN.x + 0.5)
	if th.NoColor {
		windN /= 4 // without colour nothing can be faint: thin the wind so it stays behind
	}
	for i := 0; i < windN && i < len(s.wind); i++ {
		p := s.wind[i]
		a, col := (0.34+0.36*c)*loud, muted
		n := 2 + int((5*c+6*forte)*p.speed) // a streak: longer the harder it blows
		if i%3 == 0 {
			a, col, n = a*0.6, faint, 1+n/3
		} else {
			cv.layer = 1
		}
		if th.NoColor {
			n = 1
		}
		for k := 0; k < n; k++ {
			u := p.u - float64(k)/g.W
			x, y := u*g.W, (p.v+0.035*math.Sin(5*u+p.phase+s.t*0.15))*g.H
			if d := (y - g.cy) / (2.2 * g.R); d > -1 && d < 1 { // the stream parts around the disc
				side := 1.0
				if d < 0 {
					side = -1
				}
				y += side * (1 - math.Abs(d)) * 1.3 * g.R * math.Exp(-math.Pow((x-g.cx)/(1.5*g.R), 2))
			}
			if !inDisc(x, y) {
				cv.plot(x, y, col, a*(1-float64(k)/float64(n)))
			}
		}
		cv.layer = 0
	}

	cv.layer = 2
	// ripples: the site manager's tick from the disc, a question's bell, a refusal's red ring
	for _, r := range s.ripples {
		fade := r.alpha * (1 - r.r/r.maxR)
		for k, n := 0, int(math.Max(12, 2*math.Pi*r.r/1.3)); k < n; k++ {
			a := 2 * math.Pi * float64(k) / float64(n)
			x, y := r.x+r.r*math.Cos(a), r.y+r.r*math.Sin(a)
			if !(r.fromDisc && inDisc(x, y)) {
				cv.plot(x, y, r.col, fade)
			}
		}
	}

	// stars: where merges were
	gold := toRGB(th.Yellow)
	for _, st := range s.stars {
		age := math.Min(1, s.now.Sub(st.at).Seconds()/(3*hanamiKeep.Seconds()))
		x, y := st.u*g.W, st.v*g.H
		cv.glow(x, y, 8, gold, 0.07*(1-age))
		cv.glyph(int(x/2), int(y/4), '✶', bg.mix(gold, 0.9-0.6*age), false)
	}

	// the tree: branches, the canopy of closed blossoms, then the open ones
	cv.layer = 3
	wood := toRGB(mix(th.Border, th.Text, 0.3))
	yellow := toRGB(th.Yellow)
	for _, b := range s.branches {
		rest := 1.0
		if s.now.Sub(b.last) > hanamiQuiet {
			rest = 0.55 // at rest: dimmer
		}
		steps := int(160 * b.grow.x)
		for k := 0; k <= steps; k++ {
			u := b.grow.x * float64(k) / float64(max(1, steps))
			x, y := b.point(g, s.t, u)
			col := wood.mix(yellow, 0.25+0.75*b.heat*u)
			cv.plot(x, y, col, 0.9*rest*loud)
			if u < 0.6 {
				cv.plot(x, y+1, col, 0.9*rest*loud)
			}
			if u < 0.25 {
				cv.plot(x+1, y+1, col, 0.9*rest*loud)
			}
		}
	}
	for pass := 0; pass < 2; pass++ { // closed ones behind, open ones in front
		cv.layer = 4 + pass
		for _, b := range s.branches {
			for _, bl := range b.blossoms {
				if bl.closed.IsZero() != (pass == 1) {
					continue
				}
				x, y, bx, by := b.blossomAt(g, s.t, bl)
				col := toRGB(th.AgentColor(bl.role))
				r := math.Max(1.5, bl.size.x) * math.Max(0.15, bl.open.x)
				cv.layer = 3
				for k := 0; k <= 8; k++ {
					f := float64(k) / 8
					cv.plot(bx+(x-bx)*f, by+(y-by)*f, wood, 0.8)
				}
				cv.layer = 4 + pass
				rot := 1.2 * frac(bl.n, 0.5)
				if pass == 0 {
					fade := s.now.Sub(bl.closed).Seconds() / hanamiKeep.Seconds()
					cv.flower(x, y, r, rot, col, 0.5-0.34*fade, true)
					cv.glow(x, y, 1.6*r, col, 0.035*(1-fade))
					continue
				}
				cv.flower(x, y, r, rot, col.mix(text, 0.35*bl.heat), loud, false)
				cv.glow(x, y, 2.4*r+4, col, 0.2+0.3*bl.heat)
			}
		}
	}

	cv.layer = 6
	// sparks: output tokens, streaming off whoever wrote them
	for _, p := range s.sparks {
		a := p.life / p.max_
		for k := 0; k < 5; k++ {
			f := float64(k) * 0.022
			cv.plot(p.x-p.vx*f, p.y-p.vy*f, p.col.mix(text, 0.4*a), a*(1-float64(k)/5))
		}
	}

	// petals: what a returning agent hands back
	for _, p := range s.petals {
		a := 1.0
		if p.back {
			a = 1 - p.life/p.max_
		}
		for _, d := range [5][2]float64{{0, 0}, {1, 0}, {0, 1}, {1, 1}, {2, 1}} {
			cv.plot(p.x.x+d[0], p.y.x+d[1], p.col, a)
		}
		for k := 1; k <= 4; k++ {
			f := 0.022 * float64(k)
			cv.plot(p.x.x-p.x.v*f, p.y.x-p.y.v*f, p.col, a*(0.85-0.18*float64(k)))
		}
		cv.glow(p.x.x, p.y.x, 7, p.col, 0.12*a)
	}

	// hanabi: a merge
	for _, f := range s.fire {
		if f.delay > 0 {
			continue
		}
		a := 0.25 + 0.75*f.life/f.max_
		if a < 0.5 && (f.twink/2)%2 == 0 {
			continue // the embers twinkle out
		}
		pos := f.p.Position()
		col := f.col.mix(text, 2*a-1.2) // white-hot at first
		cv.plot(pos.X, pos.Y, col, a)
		for k, tr := range f.trail {
			cv.plot(tr[0], tr[1], f.col, a*(0.8-0.15*float64(k)))
		}
		if a > 0.7 {
			cv.glow(pos.X, pos.Y, 6, f.col, 0.1*a)
		}
		if f.star && a > 0.6 && a < 0.9 && pos.Y < g.H {
			cv.glyph(int(pos.X/2), int(pos.Y/4), '*', col, true)
		}
	}

	// the disc: a solid body with a breathing halo, and the name inside
	cv.layer = 7
	rim := g.R + 1.5*math.Min(1, level) + 0.5*math.Sin(s.t*0.9) // the sine is the ambient breath
	grad := func(dx, dy float64) hrgb { return toRGB(th.gradientAt((dx-dy)*0.3536 + 0.5)) }
	halo := grad(0.3, -0.3).mix(s.flashCol, fl)
	cv.glow(g.cx, g.cy, rim*(2.5+0.6*math.Min(1, level)+0.6*fl)+2*math.Sin(s.t*0.9), halo, 0.27+0.22*math.Min(1, level)+0.3*fl+0.2*forte)
	for cy := max(0, int((g.cy-rim)/4)-1); cy <= min(g.staffTop-1, int((g.cy+rim)/4)+1); cy++ {
		for cx := max(0, int((g.cx-rim)/2)-1); cx <= min(w-1, int((g.cx+rim)/2)+1); cx++ {
			cell := &cv.cells[cy*w+cx]
			inside := 0
			for d := 0; d < 8; d++ {
				x, y := float64(2*cx+d%2)+0.5, float64(4*cy+d/2)+0.5
				if math.Hypot(x-g.cx, y-g.cy) <= rim {
					inside++
				}
			}
			if inside == 0 {
				continue
			}
			nx, ny := (float64(2*cx+1)-g.cx)/rim, (float64(4*cy+2)-g.cy)/rim
			body := bg.mix(grad(nx, ny), 0.5+0.2*math.Min(1, level)+0.12*(-nx-ny)).mix(s.flashCol, 0.55*fl).mix(text, 0.3*forte)
			if !th.NoColor { // the edge is shaded by how much of the cell the disc covers
				*cell = hCell{fill: bg.lit(cell.light).mix(body, float64(inside)/8), filled: true}
				continue
			}
			*cell = hCell{}
			for d := 0; d < 8; d++ {
				x, y := float64(2*cx+d%2)+0.5, float64(4*cy+d/2)+0.5
				if math.Hypot(x-g.cx, y-g.cy) <= rim {
					cv.plot(x, y, text, 1)
				}
			}
		}
	}
	if g.R >= 10 {
		cv.text(int(g.cx/2)-3, int(g.cy/4), brandKanji, text.mix(bg, 0.25-0.25*math.Min(1, level+fl)), true)
	}

	// glyphs on top: branch tips, blossom hearts, lanterns, bells
	for _, b := range s.branches {
		if b.grow.x > 0.15 {
			x, y := b.point(g, s.t, b.grow.x)
			cv.glow(x, y, 8, yellow, 0.25*b.heat)
			cv.glyph(int(x/2), int(y/4), '•', wood.mix(yellow, 0.45+0.55*b.heat), b.heat > 0.4)
		}
		for _, bl := range b.blossoms {
			x, y, _, _ := b.blossomAt(g, s.t, bl)
			col, ch := toRGB(th.AgentColor(bl.role)), roleGlyph(bl.role)
			switch {
			case !bl.closed.IsZero():
				col = bg.mix(col, 0.6-0.35*s.now.Sub(bl.closed).Seconds()/hanamiKeep.Seconds())
			case bl.open.x < 0.3:
				ch = '·'
			case bl.open.x < 0.7:
				ch = '∘'
			default:
				col = col.mix(text, 0.55)
			}
			cv.glyph(int(x/2), int(y/4), ch, col, bl.closed.IsZero())
		}
	}
	cv.layer = 8
	orange := toRGB(th.Orange)
	for _, l := range s.lanterns {
		if l.gone > 0 {
			cv.glyph(int(l.x.x/2), int(l.y.x/4), '○', orange.mix(bg, l.gone), false)
			continue
		}
		col, halo := orange, 0.5
		switch l.ci {
		case "green":
			col, halo = toRGB(th.Green), 0.8
		case "red":
			col, halo = toRGB(th.Red), 0.15
		case "running":
			halo = 0.9
		}
		lit := halo
		if l.ci == "running" && flicker(s.frame/3, l.pr, 99) {
			lit *= 0.45 // the flicker of checks still running
		}
		cv.glow(l.x.x, l.y.x, 17, col, 0.42*lit)
		for k := 0; k < 12; k++ {
			a := 2 * math.Pi * float64(k) / 12
			if l.ci == "running" && flicker(s.frame/3, l.pr, k) {
				continue
			}
			cv.plot(l.x.x+7.2*math.Cos(a), l.y.x+6.6*math.Sin(a), col, halo)
		}
		for dy := -4.0; dy <= 4; dy++ {
			for dx := -3.0; dx <= 3; dx++ {
				if dx*dx/11+dy*dy/19 <= 1 {
					cv.plot(l.x.x+dx, l.y.x+dy, col, 0.85)
				}
			}
		}
		cv.glyph(int(l.x.x/2), int(l.y.x/4), '◉', col.mix(text, 0.5), true)
	}
	for _, b := range s.bells { // still: a question does not move until it is answered
		x, y := bellAt(g, b.slot)
		cv.glow(x, y, 15, text, 0.3)
		for k := 0; k < 14; k++ {
			a := 2 * math.Pi * float64(k) / 14
			cv.plot(x+5.2*math.Cos(a), y+5.2*math.Sin(a), text, 1)
		}
		cv.glyph(int(x/2), int(y/4), '✦', text, true)
	}

	// the staff: a voice per tool kind. A note is struck at its call and sounds until its agent's next one.
	if g.staffTop < h {
		sheet := bg.mix(toRGB(th.Surface), 0.55)
		line := sheet.mix(toRGB(th.Border), 0.7)
		head := w - 5
		for i, t := range hanamiTools {
			y := g.staffTop + i
			v := math.Max(0, math.Min(1, s.voice[i].x))
			for x := 0; x < w; x++ {
				cell := &cv.cells[y*w+x]
				*cell = hCell{fill: sheet, filled: true}
				if d := float64(head - x); d >= 0 && d < 26 { // the voice glows at the playhead as loud as it is
					cell.fill = sheet.mix(s.voiceCol[i], (0.1+0.3*v)*(1-d/26)*(0.4+0.6*math.Max(v, s.beat[i])))
				}
				if x >= 3 && x < head {
					cell.ch, cell.c = '─', line
				}
			}
			cv.glyph(1, y, t.glyph, faint.mix(text, 0.3+0.7*s.beat[i]), false)
			cv.glyph(head, y, '┃', line.mix(text, 0.25+0.75*s.beat[i]), false)
			meter := []rune(" ▁▂▃▄▅▆▇█")[int(v*8+0.5)]
			cv.glyph(head+1, y, meter, s.voiceCol[i], false)
			cv.glyph(head+2, y, meter, s.voiceCol[i].mix(sheet, 0.45), false)
		}
		for _, n := range s.notes {
			y := g.staffTop + n.row
			k := math.Pow(math.Max(0, (n.x0-3)/float64(max(1, w-8))), 0.6)
			col := sheet.mix(n.c, 0.3+0.7*k)
			for x := max(3, int(n.x0)+1); x <= int(n.x1) && x < head; x++ { // the sustain
				cv.glyph(x, y, '━', col.mix(sheet, 0.25), false)
			}
			if n.x0 >= 3 {
				if n.age < 0.18 {
					col = n.c.mix(text, 0.7)
				}
				cv.glyph(int(n.x0), y, n.glyph, col, n.n > 1 || n.age < 0.18)
			}
		}
	}

	if legend {
		s.drawLegend(cv)
	}
	return cv.lines(th)
}

// hanamiLegend says what every moving thing is; the scene's mapping in one place.
var hanamiLegend = [][2]string{
	{"●", "the disc: all the work. It swells and brightens with the rate of events"},
	{"─•", "a branch: one session. Its tip glows at each call; at rest it dims"},
	{"✿❀✾✽", "a blossom: an agent (coder, reviewer, explorer, other), as large as its work"},
	{"⠶", "petals: it returns to the disc and its blossom closes to an outline for 2 h"},
	{"⠶", "petals turned back in red, with a red ring: a reviewer asked for changes"},
	{"⠐⠂", "wind: cache reads. Bright streaks off an agent: output tokens"},
	{"●○◆■▲◇", "notes: shell, read, edit, write, dispatch, message. Each sounds until its"},
	{"━▃", "agent's next call (12 s at most); the bars at the right: calls per second"},
	{"◉", "a lantern: an open pull request. Flicker, green, red: its checks"},
	{"*✶", "hanabi: a merge lights the stage, and leaves a star for 6 h"},
	{"✦", "a ringed star above the disc: a question waiting for the human"},
	{"◌", "a ripple from the disc: the site manager's tick"},
	{"", "colour: the agent's role, on blossoms, notes, streaks and petals"},
	{"", "ambient, not data: the breath, the sway, the slowest breeze"},
}

func (s *hanamiScene) drawLegend(cv *hCanvas) {
	th := s.th
	text, muted, border, panel := toRGB(th.Text), toRGB(th.Muted), toRGB(th.Pink), toRGB(th.Surface)
	inner := 0
	for _, l := range hanamiLegend {
		inner = max(inner, 8+ansi.StringWidth(l[1]))
	}
	inner = min(inner, cv.w-4)
	bw, bh := inner+4, len(hanamiLegend)+2
	x0, y0 := (cv.w-bw)/2, max(0, (cv.h-bh)/2)
	for y := y0; y < y0+bh && y < cv.h; y++ {
		for x := x0; x < x0+bw; x++ {
			ch := ' '
			switch {
			case y == y0 && x == x0:
				ch = '╭'
			case y == y0 && x == x0+bw-1:
				ch = '╮'
			case y == y0+bh-1 && x == x0:
				ch = '╰'
			case y == y0+bh-1 && x == x0+bw-1:
				ch = '╯'
			case y == y0 || y == y0+bh-1:
				ch = '─'
			case x == x0 || x == x0+bw-1:
				ch = '│'
			}
			cv.cells[y*cv.w+x] = hCell{ch: ch, c: border, fill: panel, filled: true}
		}
	}
	cv.text(x0+2, y0, " legend ", border, true)
	for i, l := range hanamiLegend {
		y := y0 + 1 + i
		if y >= cv.h-1 {
			break
		}
		cv.text(x0+2, y, l[0], text, true)
		cv.text(x0+10, y, trunc(l[1], inner-8), muted, false)
	}
}
