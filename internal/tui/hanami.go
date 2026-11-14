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
	hanamiFPS    = 30 // smooth for springs, and a full-screen frame still costs little; see the README
	hanamiDT     = time.Second / hanamiFPS
	hanamiQuiet  = 3 * time.Minute // a session without events this long withdraws its branch
	hanamiSlots  = 7
	maxSparks    = 400
	maxFire      = 600
	maxNotes     = 600
	maxPetals    = 300
	maxLanterns  = 60
	maxRipples   = 40
	noteDrift    = 4.0 // cells per second, right to left
	staffRows    = 6
	hanamiSecond = 1.0 / hanamiFPS
)

// One spring per kind of motion, all stepped at the scene's frame rate.
var (
	springSwell   = harmonica.NewSpring(harmonica.FPS(hanamiFPS), 3.0, 0.8)
	springBloom   = harmonica.NewSpring(harmonica.FPS(hanamiFPS), 8.0, 0.32)
	springGrow    = harmonica.NewSpring(harmonica.FPS(hanamiFPS), 2.4, 1.0)
	springLantern = harmonica.NewSpring(harmonica.FPS(hanamiFPS), 1.6, 0.42)
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

// roleGlyph is the open blossom of a role, so the role reads without colour too.
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
	n           int // order on its branch: where it sits
	open        sv
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

type hNote struct {
	col   float64 // cell column
	row   int
	glyph rune
	c     hrgb
	age   float64
	n     int
}

type hLantern struct {
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

	branches []*hBranch
	petals   []hPetal
	sparks   []hSpark
	wind     []hWind
	windN    sv
	notes    []hNote
	beat     [staffRows]float64
	lanterns []*hLantern
	fire     []hFire
	bells    []*hBell
	ripples  []hRipple
}

func newHanamiScene(th *Theme) *hanamiScene {
	s := &hanamiScene{th: th, w: 80, h: 21, scale: 1, rnd: 0x9E3779B97F4A7C15, flashCol: toRGB(th.Text)}
	for i := 0; i < 700; i++ {
		s.wind = append(s.wind, hWind{u: s.rnd.f(), v: s.rnd.f(), speed: s.rnd.span(0.6, 1.5), phase: s.rnd.span(0, 6.28)})
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
	g.R = math.Max(7, math.Min(g.H*0.27, g.W*0.12))
	g.cx = g.W * 0.22
	g.cy = g.H - g.R - 5
	return g
}

var slotAngles = [hanamiSlots]float64{34, 58, 14, 72, 46, 24, 65}

// point is where a branch is at u in 0..1 of its grown length.
func (b *hBranch) point(g hGeo, t, u float64) (x, y float64) {
	a := slotAngles[b.slot]*math.Pi/180 + b.jitter
	dx, dy := math.Cos(a), -math.Sin(a)
	sx, sy := g.cx+(g.R+2)*dx, g.cy+(g.R+2)*dy
	reach := math.Min((g.W-8-sx)/math.Max(dx, 0.05), (sy-7)/math.Max(-dy, 0.05))
	L := math.Max(6, math.Min(reach, g.W*0.72)*(0.66+0.32*frac(b.slot, 0.3)))
	bend := 0.11
	if b.slot%2 == 1 {
		bend = -0.09
	}
	side := math.Sin(math.Pi*u)*L*bend + math.Sin(t*0.6+float64(b.slot))*u*1.1 // the second term is ambient sway
	return sx + dx*u*L - dy*side, sy + dy*u*L + dx*side
}

// blossomAt is where a blossom sits: on a short twig off its branch.
func (b *hBranch) blossomAt(g hGeo, t float64, bl *hBlossom) (x, y, bx, by float64) {
	u := (0.28 + 0.72*frac(bl.n, 0.21)) * b.grow.x
	bx, by = b.point(g, t, u)
	x2, y2 := b.point(g, t, math.Min(1, u+0.02))
	nx, ny := -(y2 - by), x2-bx
	if l := math.Hypot(nx, ny); l > 0 {
		nx, ny = nx/l, ny/l
	}
	twig := (4 + 4*frac(bl.n, 0.7)) * b.grow.x
	if bl.n%2 == 1 {
		twig = -twig
	}
	return bx + nx*twig, by + ny*twig, bx, by
}

func (s *hanamiScene) lanternTarget(g hGeo, l *hLantern) (x, y float64) {
	x = g.W * (0.42 + 0.53*frac(l.slot, 0.1))
	y = 7 + float64(l.slot%3)*g.H*0.07
	return x + math.Sin(s.t*0.7+float64(l.slot))*1.6, y + math.Sin(s.t*0.5+float64(l.slot)*2)*0.8 // ambient sway
}

// ---- events ---------------------------------------------------------------------------

func (s *hanamiScene) branch(session string, at time.Time) *hBranch {
	for _, b := range s.branches {
		if b.session == session {
			b.last = at
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
	b := &hBranch{session: session, slot: slot, last: at, jitter: float64(used[slot]) * 0.09}
	s.branches = append(s.branches, b)
	return b
}

// origin is where an agent's events come from: its blossom, or its session's branch tip.
func (s *hanamiScene) origin(g hGeo, b *hBranch, agent string) (x, y float64) {
	for _, bl := range b.blossoms {
		if bl.agent == agent {
			x, y, _, _ = b.blossomAt(g, s.t, bl)
			return
		}
	}
	return b.point(g, s.t, b.grow.x)
}

// lantern is a pull request's lantern, lit if it was not. One that was already open when the viewer arrived
// (settled) hangs in the sky at once; a new one rises from the disc.
func (s *hanamiScene) lantern(g hGeo, pr int, settled bool) *hLantern {
	for _, l := range s.lanterns {
		if l.pr == pr && l.gone == 0 {
			return l
		}
	}
	slot := 0
	for slices.ContainsFunc(s.lanterns, func(l *hLantern) bool { return l.slot == slot }) {
		slot++
	}
	l := &hLantern{pr: pr, slot: slot}
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
// viewer arrived: it sets what exists (blossoms, lanterns, bells) without any motion.
func (s *hanamiScene) Apply(e data.Event, settled bool) {
	g := s.geo()
	if e.TS.After(s.now) {
		s.now = e.TS
	}
	col := toRGB(s.th.AgentColor(e.Role))
	switch e.Kind {
	case "tool":
		b := s.branch(e.Session, e.TS)
		if settled {
			b.grow.x = 1
			return
		}
		b.heat = 1
		s.acc++
		s.note(g, e.Tool, col)
	case "tokens":
		b := s.branch(e.Session, e.TS)
		if settled {
			b.grow.x = 1
			return
		}
		s.acc++
		s.cacheAcc += float64(e.CacheRead)
		if e.Out > 0 {
			x, y := s.origin(g, b, e.Agent)
			s.sparkDebt = math.Min(8, s.sparkDebt+float64(min(8, 2+e.Out/400))/math.Sqrt(s.scale))
			for ; s.sparkDebt >= 1 && len(s.sparks) < maxSparks; s.sparkDebt-- {
				life := s.rnd.span(1.2, 2.6)
				s.sparks = append(s.sparks, hSpark{x: x, y: y, vx: s.rnd.span(26, 58), vy: s.rnd.span(-16, 3), life: life, max_: life, col: col})
			}
		}
	case "spawn":
		b := s.branch(e.Session, e.TS)
		if slices.ContainsFunc(b.blossoms, func(bl *hBlossom) bool { return bl.agent == e.Agent }) {
			return
		}
		bl := &hBlossom{agent: e.Agent, role: e.Role, n: b.spawned}
		b.spawned++
		if settled {
			bl.open.x, b.grow.x = 1, 1
		} else {
			s.acc++
		}
		b.blossoms = append(b.blossoms, bl)
	case "return":
		b := s.branch(e.Session, e.TS)
		i := slices.IndexFunc(b.blossoms, func(bl *hBlossom) bool { return bl.agent == e.Agent })
		if i < 0 {
			return
		}
		x, y, _, _ := b.blossomAt(g, s.t, b.blossoms[i])
		b.blossoms = slices.Delete(b.blossoms, i, i+1)
		if settled {
			return
		}
		s.acc++
		for k := 0; k < 5 && len(s.petals) < maxPetals; k++ {
			a := s.rnd.span(0, 2*math.Pi)
			s.petals = append(s.petals, hPetal{
				x: sv{x, 26 * math.Cos(a)}, y: sv{y, 26 * math.Sin(a)}, ox: x, oy: y, col: col,
				spring: k % 3, delay: k * 2, bounce: e.Verdict == "changes", lead: k == 0, max_: 2.2,
			})
		}
	case "pr":
		switch {
		case e.State != "closed":
			s.lantern(g, e.PR, settled)
		case settled:
			s.lanterns = slices.DeleteFunc(s.lanterns, func(l *hLantern) bool { return l.pr == e.PR })
		default: // closed without a merge: it goes out
			for _, l := range s.lanterns {
				if l.pr == e.PR && l.gone == 0 {
					l.gone = hanamiSecond
				}
			}
		}
	case "ci":
		s.lantern(g, e.PR, settled).ci = e.State
	case "merge":
		i := slices.IndexFunc(s.lanterns, func(l *hLantern) bool { return l.pr == e.PR && l.gone == 0 })
		switch {
		case settled && i >= 0:
			s.lanterns = slices.Delete(s.lanterns, i, i+1)
		case settled:
		case i >= 0 && s.lanterns[i].placed:
			l := s.lanterns[i]
			s.lanterns = slices.Delete(s.lanterns, i, i+1)
			s.hanabi(g, l.x.x, l.y.x)
		default:
			s.lantern(g, e.PR, false).burst = true // rises first, then bursts
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
			s.ripple(hRipple{x: x, y: y, r: 5, speed: 24, maxR: 30, alpha: 1, col: toRGB(s.th.Text)})
		}
	case "answer":
		s.bells = slices.DeleteFunc(s.bells, func(b *hBell) bool { return b.slug == e.Slug })
	case "watch":
		if !settled {
			s.ripple(hRipple{x: g.cx, y: g.cy, r: g.R, speed: 75, maxR: math.Hypot(g.W, g.H), alpha: 0.55, col: toRGB(s.th.Purple), fromDisc: true})
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
	return g.cx - 0.75*g.R + 14*float64(slot), math.Max(6, g.cy-g.R-12)
}

func (s *hanamiScene) ripple(r hRipple) {
	if len(s.ripples) < maxRipples {
		s.ripples = append(s.ripples, r)
	}
}

func (s *hanamiScene) note(g hGeo, tool string, col hrgb) {
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
	head := float64(s.w - 4)
	for i := len(s.notes) - 1; i >= 0 && i >= len(s.notes)-12; i-- {
		if n := &s.notes[i]; n.row == row && head-n.col < 1 {
			n.n++
			n.age = 0
			if glyph != '·' {
				n.glyph, n.c = glyph, col
			}
			return
		}
	}
	if len(s.notes) < maxNotes {
		s.notes = append(s.notes, hNote{col: head, row: row, glyph: glyph, c: col, n: 1})
	}
}

func (s *hanamiScene) hanabi(g hGeo, x, y float64) {
	size := math.Max(0.8, g.R/18)
	dt := harmonica.FPS(hanamiFPS)
	for ring := 0; ring < 2; ring++ {
		n := 84 - 36*ring
		for i := 0; i < n && len(s.fire) < maxFire; i++ {
			a := 2*math.Pi*float64(i)/float64(n) + s.rnd.span(-0.08, 0.08)
			v := s.rnd.span(52, 60) * size * (1 - 0.45*float64(ring))
			col := toRGB(s.th.gradientAt(s.rnd.f()))
			if i%5 == 0 {
				col = toRGB(s.th.Yellow)
			}
			life := s.rnd.span(1.5, 2.7)
			f := hFire{
				p:    harmonica.NewProjectile(dt, harmonica.Point{X: x, Y: y}, harmonica.Vector{X: v * math.Cos(a), Y: v * math.Sin(a)}, harmonica.Vector{Y: 11 * size}),
				life: life, max_: life, col: col, delay: ring * 7, star: i%6 == 0,
			}
			for k := range f.trail {
				f.trail[k] = [2]float64{x, y}
			}
			s.fire = append(s.fire, f)
		}
	}
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

	for i := 0; i < len(s.branches); i++ {
		b := s.branches[i]
		target := 1.0
		if s.now.Sub(b.last) > hanamiQuiet {
			target = 0
		}
		b.grow.to(springGrow, target)
		b.heat *= 1 - dt/0.7
		for _, bl := range b.blossoms {
			bl.open.to(springBloom, 1)
		}
		if target == 0 && b.grow.x < 0.03 {
			s.branches = slices.Delete(s.branches, i, i+1)
			i--
		}
	}

	c := s.cacheLevel()
	for i := range s.wind {
		p := &s.wind[i]
		p.u += (0.03 + 0.13*c) * p.speed * dt
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
		if math.Hypot(p.x.x-g.cx, p.y.x-g.cy) < g.R*0.85 {
			if p.bounce { // turned back: a swirl sideways, then out the way it came
				p.back, p.col = true, toRGB(s.th.Red)
				if p.lead {
					s.ripple(hRipple{x: p.x.x, y: p.y.x, r: 2, speed: 16, maxR: 12, alpha: 1, col: p.col})
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

	notes := s.notes[:0]
	for _, n := range s.notes {
		n.col -= noteDrift * math.Min(5, math.Cbrt(s.scale)) * dt // a fast replay turns the page faster
		n.age += dt
		if n.col >= 3 {
			notes = append(notes, n)
		}
	}
	s.notes = notes
	for i := range s.beat {
		s.beat[i] *= 1 - dt/0.25
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
	most := math.Min(float64(len(s.wind)), g.W*g.H/170)
	return most * (0.14 + 0.86*s.cacheLevel())
}

// busy says whether anything beyond the ambient breeze is in motion (the tests' definition of idle).
func (s *hanamiScene) busy() bool {
	return len(s.petals)+len(s.sparks)+len(s.notes)+len(s.fire)+len(s.ripples) > 0
}

// ---- drawing --------------------------------------------------------------------------

type hCell struct {
	mask  uint8
	a     float64 // the strongest dot of the front-most layer: its colour is the cell's
	layer int
	ch    rune
	c     hrgb // of the glyph, or of the dots
	bold  bool
	skip  bool // the right half of a double-width glyph
}

// hCanvas is the frame being drawn. A braille cell has one colour, so the dots of the
// front-most layer in a cell decide it, and among those the strongest: wind behind a blossom does not
// grey the blossom, and two colours of a firework do not average to mud.
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
	cell.layer, cell.c, cell.a = cv.layer, c, a
}

func (cv *hCanvas) glyph(cx, cy int, ch rune, c hrgb, bold bool) {
	if cx < 0 || cy < 0 || cx >= cv.w || cy >= cv.h {
		return
	}
	cv.cells[cy*cv.w+cx] = hCell{ch: ch, c: c, bold: bold}
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
			cv.cells[cy*cv.w+cx+1] = hCell{skip: true}
		}
		cx += w
	}
	return cx
}

func (cv *hCanvas) lines(th *Theme) []string {
	bg := toRGB(th.Bg)
	out := make([]string, cv.h)
	var sb strings.Builder
	sgr := func(c hrgb, bold bool) {
		sb.WriteString("\x1b[")
		if bold {
			sb.WriteString("1;")
		} else {
			sb.WriteString("22;")
		}
		sb.WriteString("38;2;")
		sb.WriteString(strconv.Itoa(int(c.r + 0.5)))
		sb.WriteByte(';')
		sb.WriteString(strconv.Itoa(int(c.g + 0.5)))
		sb.WriteByte(';')
		sb.WriteString(strconv.Itoa(int(c.b + 0.5)))
		sb.WriteByte('m')
	}
	for y := 0; y < cv.h; y++ {
		sb.Reset()
		if !th.NoColor {
			sb.WriteString("\x1b[48;2;" + strconv.Itoa(int(bg.r)) + ";" + strconv.Itoa(int(bg.g)) + ";" + strconv.Itoa(int(bg.b)) + "m")
		}
		var last [4]int
		for x := 0; x < cv.w; x++ {
			cell := cv.cells[y*cv.w+x]
			ch, c, bold := cell.ch, cell.c, cell.bold
			switch {
			case cell.skip:
				continue
			case ch != 0:
			case cell.mask != 0:
				ch = rune(0x2800 + int(cell.mask))
				c = bg.mix(c, cell.a)
			default:
				sb.WriteByte(' ')
				continue
			}
			if !th.NoColor {
				b := 0
				if bold {
					b = 1
				}
				if k := [4]int{int(c.r + 0.5), int(c.g + 0.5), int(c.b + 0.5), b + 1}; k != last {
					sgr(c, bold)
					last = k
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
	level := math.Max(0, math.Min(1.3, s.swell.x))
	inDisc := func(x, y float64) bool { return math.Hypot(x-g.cx, y-g.cy) < g.R+1.5 }

	// wind: cache reads, behind everything
	c := s.cacheLevel()
	windN := int(s.windN.x + 0.5)
	if th.NoColor {
		windN /= 4 // without colour nothing can be faint: thin the wind so it stays behind
	}
	for i := 0; i < windN && i < len(s.wind); i++ {
		p := s.wind[i]
		a := 0.22 + 0.33*c
		n := 2 + int(5*c*p.speed) // a streak: longer the harder it blows
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
				cv.plot(x, y, muted, a*(1-float64(k)/float64(n)))
			}
		}
	}

	cv.layer = 1
	// ripples: the site manager's tick from the disc, a question's bell from its point
	for _, r := range s.ripples {
		fade := r.alpha * (1 - r.r/r.maxR)
		for k, n := 0, int(math.Max(12, 2*math.Pi*r.r/1.6)); k < n; k++ {
			a := 2 * math.Pi * float64(k) / float64(n)
			x, y := r.x+r.r*math.Cos(a), r.y+r.r*math.Sin(a)
			if y < g.H && !(r.fromDisc && inDisc(x, y)) {
				cv.plot(x, y, r.col, fade)
			}
		}
	}

	cv.layer = 2
	// branches, then their blossoms on top
	wood := toRGB(mix(th.Border, th.Yellow, 0.42))
	yellow := toRGB(th.Yellow)
	for _, b := range s.branches {
		steps := int(110 * b.grow.x)
		for k := 0; k <= steps; k++ {
			u := b.grow.x * float64(k) / float64(max(1, steps))
			x, y := b.point(g, s.t, u)
			col := wood.mix(yellow, b.heat*u)
			cv.plot(x, y, col, 0.9)
			if u < 0.55 {
				cv.plot(x, y+1, col, 0.9)
			}
		}
		for _, bl := range b.blossoms {
			x, y, bx, by := b.blossomAt(g, s.t, bl)
			for k := 0; k <= 8; k++ {
				f := float64(k) / 8
				cv.plot(bx+(x-bx)*f, by+(y-by)*f, wood, 0.8)
			}
			col, open := toRGB(th.AgentColor(bl.role)), math.Max(0, bl.open.x)
			cv.layer = 3
			for k := 0; k < 5; k++ { // five petals, as wide as the blossom is open
				a := 2*math.Pi*float64(k)/5 - math.Pi/2
				for _, r := range [3]float64{2.6, 3.7, 4.8} {
					cv.plot(x+r*open*math.Cos(a), y+r*open*math.Sin(a), col, 0.9)
				}
				for _, da := range [2]float64{-0.33, 0.33} {
					cv.plot(x+3.9*open*math.Cos(a+da), y+3.9*open*math.Sin(a+da), col, 0.9)
				}
			}
			cv.layer = 2
		}
	}

	cv.layer = 3
	// sparks: output tokens, streaming off whoever wrote them
	for _, p := range s.sparks {
		a := p.life / p.max_
		for k := 0; k < 4; k++ {
			f := float64(k) * 0.025
			cv.plot(p.x-p.vx*f, p.y-p.vy*f, p.col, a*(1-float64(k)/4))
		}
	}

	// petals: what a returning agent hands back
	for _, p := range s.petals {
		a := 1.0
		if p.back {
			a = 1 - p.life/p.max_
		}
		for _, d := range [4][2]float64{{0, 0}, {1, 0}, {0, 1}, {1, 1}} {
			cv.plot(p.x.x+d[0], p.y.x+d[1], p.col, a)
		}
		for k := 1; k <= 3; k++ {
			f := 0.025 * float64(k)
			cv.plot(p.x.x-p.x.v*f, p.y.x-p.y.v*f, p.col, a*(0.8-0.2*float64(k)))
		}
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
		if pos.Y >= g.H {
			continue
		}
		col := f.col.mix(text, 2*a-1.2) // white-hot at first
		cv.plot(pos.X, pos.Y, col, a)
		for k, tr := range f.trail {
			if tr[1] < g.H {
				cv.plot(tr[0], tr[1], f.col, a*(0.8-0.15*float64(k)))
			}
		}
		if f.star && a > 0.6 && a < 0.9 {
			cv.glyph(int(pos.X/2), int(pos.Y/4), '*', col, true)
		}
	}

	cv.layer = 4
	// the disc: its rim, and the name inside
	rim := g.R + 2.5*level + 0.5*math.Sin(s.t*0.9) // the sine is the ambient breath
	fl := math.Max(0, math.Min(1, s.flash.x))
	for k, n := 0, int(2*math.Pi*rim*1.3); k < n; k++ {
		a := 2 * math.Pi * float64(k) / float64(n)
		dx, dy := math.Cos(a), math.Sin(a)
		col := toRGB(th.gradientAt((dx-dy)*0.3536+0.5)).mix(s.flashCol, fl)
		cv.plot(g.cx+rim*dx, g.cy+rim*dy, col, 0.5+0.5*math.Min(1, level+fl))
		if level > 0.25 {
			cv.plot(g.cx+(rim+1.2)*dx, g.cy+(rim+1.2)*dy, col, 0.45*level)
		}
	}
	if g.R >= 12 {
		cv.text(int(g.cx/2)-3, int(g.cy/4), brandKanji, faint.mix(text, 0.25+0.6*math.Min(1, level+fl)), false)
	}

	// glyphs on top: branch tips, blossoms, lanterns, bells
	for _, b := range s.branches {
		if b.grow.x > 0.15 {
			x, y := b.point(g, s.t, b.grow.x)
			cv.glyph(int(x/2), int(y/4), '•', wood.mix(yellow, 0.45+0.55*b.heat), b.heat > 0.4)
		}
		for _, bl := range b.blossoms {
			x, y, _, _ := b.blossomAt(g, s.t, bl)
			ch := roleGlyph(bl.role)
			switch {
			case bl.open.x < 0.3:
				ch = '·'
			case bl.open.x < 0.7:
				ch = '∘'
			}
			cv.glyph(int(x/2), int(y/4), ch, toRGB(th.AgentColor(bl.role)), bl.open.x > 1.03)
		}
	}
	cv.layer = 5
	orange := toRGB(th.Orange)
	for _, l := range s.lanterns {
		col, halo := orange, 0.45
		switch l.ci {
		case "green":
			col, halo = toRGB(th.Green), 0.8
		case "red":
			col, halo = toRGB(th.Red), 0
		case "running":
			halo = 0.9
		}
		if l.gone > 0 {
			cv.glyph(int(l.x.x/2), int(l.y.x/4), '○', orange.mix(bg, l.gone), false)
			continue
		}
		for k := 0; k < 10; k++ {
			a := 2 * math.Pi * float64(k) / 10
			if l.ci == "running" && flicker(s.frame/3, l.pr, k) {
				continue // the flicker of checks still running
			}
			cv.plot(l.x.x+5.6*math.Cos(a), l.y.x+5.2*math.Sin(a), col, halo)
		}
		for dy := -3.0; dy <= 3; dy++ {
			for dx := -2.0; dx <= 2; dx++ {
				if dx*dx/6.5+dy*dy/12 <= 1 {
					cv.plot(l.x.x+dx, l.y.x+dy, col, 0.75)
				}
			}
		}
		cv.glyph(int(l.x.x/2), int(l.y.x/4), '◉', col, true)
	}
	for _, b := range s.bells { // still: a question does not move until it is answered
		x, y := bellAt(g, b.slot)
		for k := 0; k < 12; k++ {
			a := 2 * math.Pi * float64(k) / 12
			cv.plot(x+4.6*math.Cos(a), y+4.6*math.Sin(a), text, 0.95)
		}
		cv.glyph(int(x/2), int(y/4), '✦', text, true)
	}

	// the staff: one line per tool kind, notes drifting left from the playhead
	if g.staffTop < h {
		line := bg.mix(toRGB(th.Border), 0.55)
		for i, t := range hanamiTools {
			y := g.staffTop + i
			for x := 3; x < w-3; x++ {
				cv.glyph(x, y, '─', line, false)
			}
			cv.glyph(1, y, t.glyph, faint.mix(text, s.beat[i]), false)
			cv.glyph(w-3, y, '╴', line.mix(text, s.beat[i]), false)
		}
		for _, n := range s.notes {
			k := math.Pow(math.Max(0, (n.col-3)/float64(max(1, w-7))), 0.6)
			col := bg.mix(n.c, 0.2+0.8*k)
			if n.age < 0.18 {
				col = n.c.mix(text, 0.7)
			}
			cv.glyph(int(n.col), g.staffTop+n.row, n.glyph, col, n.n > 1 || n.age < 0.18)
		}
	}

	if legend {
		s.drawLegend(cv)
	}
	return cv.lines(th)
}

// hanamiLegend says what every moving thing is; the scene's mapping in one place.
var hanamiLegend = [][2]string{
	{"◯", "the disc: all the work. Its rim swells with the rate of events"},
	{"─•", "a branch: one session at work. Its tip glows at each tool call"},
	{"✿❀✾✽", "a blossom: an agent (coder, reviewer, explorer, other)"},
	{"⠶", "petals: an agent returns to the disc. Turned back: changes needed"},
	{"⠐⠂", "wind: cache reads (faint). Bright streaks: output tokens"},
	{"●○◆■▲◇", "notes: shell, read, edit, write, dispatch, message"},
	{"◉", "a lantern: an open pull request. Flicker, green, red: its checks"},
	{"⠿*", "hanabi: a merge"},
	{"✦", "a bell: a question waiting for the human"},
	{"◌", "a ripple from the disc: the site manager's tick"},
	{"", "colour: the agent's role, on blossoms, notes, streaks and petals"},
	{"", "ambient, not data: the breath, the sway, the slowest breeze"},
}

func (s *hanamiScene) drawLegend(cv *hCanvas) {
	th := s.th
	text, muted, border := toRGB(th.Text), toRGB(th.Muted), toRGB(th.Pink)
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
			cv.glyph(x, y, ch, border, false)
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
