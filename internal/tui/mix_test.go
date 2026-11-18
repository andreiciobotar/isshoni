package tui

import (
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/charmbracelet/x/ansi"

	"isshoni/internal/data"
)

type mixRendering struct {
	text    string
	amounts bool // an amount stands beside each share
}

// mixUsage is a row of `tokens` input+output+cache-write tokens with a large cache read that must not
// count; an unpriced row has no cost and all its tokens unpriced.
func mixUsage(tokens int64, cost float64, unpriced bool) data.Usage {
	u := data.Usage{Input: tokens / 2, Output: tokens / 4, CacheWrite: tokens - tokens/2 - tokens/4, CacheRead: 1000 * tokens}
	u.Total = u.Input + u.Output + u.CacheWrite + u.CacheRead
	if unpriced {
		u.Unpriced = u.Total
		return u
	}
	u.Cost = cost
	return u
}

// mixSet is three rows, named as agents (main, coder, explorer) and as models (a, b, c).
func mixSet(a, b, c data.Usage) (byAgent, byModel map[string]data.Usage) {
	return map[string]data.Usage{"main": a, "coder": b, "explorer": c},
		map[string]data.Usage{"claude-a": a, "claude-b": b, "claude-c": c}
}

// mixRenderings draws the six mixes through the real view code, as plain text.
func mixRenderings(byAgent, byModel map[string]data.Usage) map[string]mixRendering {
	m := fakeModel(true, 140, 40)
	p := m.th.on(m.th.Bg)
	tot := totals{byAgent: byAgent, byModel: byModel}
	lines := func(b block) string { return ansi.Strip(strings.Join(b, "\n")) }

	m.agents.ByModel = byModel
	m.view = viewAgents
	var agentsView string
	for _, l := range strings.Split(plain(m), "\n") {
		if strings.Contains(l, "models") {
			agentsView = l
			break
		}
	}
	agentRows, agentTotal := agentUsage(roleUsage(byAgent))
	modelRows, modelTotal := sortedUsage(byModel)
	return map[string]mixRendering{
		"agent box":    {lines(m.agentMixBox(p, "", tot, 60, 8)), true},
		"model box":    {lines(m.modelMixBox(p, "", tot, 60, 8)), true},
		"agents strip": {ansi.Strip(m.scopeStrip(p, "agents", agentRows, agentTotal, m.th.AgentColor, func(k string) string { return k }, 140)), true},
		"models strip": {ansi.Strip(m.scopeStrip(p, "models", modelRows, modelTotal, m.th.ModelColor, shortModel, 140)), true},
		"agents view":  {agentsView, false},
		"session bar":  {lines(m.agentBar(p, &data.Session{ByAgent: byAgent}, 140)), true},
	}
}

var pctRe = regexp.MustCompile(`(\d+)%`)

func sharesOf(t *testing.T, name, text string) []int {
	t.Helper()
	var out []int
	for _, m := range pctRe.FindAllStringSubmatch(text, -1) {
		n, _ := strconv.Atoi(m[1])
		out = append(out, n)
	}
	if len(out) != 3 {
		t.Fatalf("%s: want 3 shares, got %v in\n%s", name, out, text)
	}
	return out
}

// The tokens are 100, 300, 600: cost order (a, b, c) is the reverse of usage order.
func TestMixIsByTokensWhenAllUnpriced(t *testing.T) {
	for name, r := range mixRenderings(mixSet(mixUsage(100, 0, true), mixUsage(300, 0, true), mixUsage(600, 0, true))) {
		s := sharesOf(t, name, r.text)
		if sum := s[0] + s[1] + s[2]; sum != 100 {
			t.Errorf("%s: shares %v sum to %d\n%s", name, s, sum, r.text)
		}
		if strings.Contains(r.text, "$0") || strings.Contains(r.text, "unpriced") {
			t.Errorf("%s: shows $0 or the word unpriced\n%s", name, r.text)
		}
		for _, want := range []string{"100", "300", "600"} {
			if r.amounts && !strings.Contains(r.text, want) {
				t.Errorf("%s: no token count %s\n%s", name, want, r.text)
			}
		}
	}
}

func TestMixKeepsDollarsWhenAllPriced(t *testing.T) {
	for name, r := range mixRenderings(mixSet(mixUsage(100, 5, false), mixUsage(300, 1.5, false), mixUsage(600, 0.25, false))) {
		s := sharesOf(t, name, r.text)
		if sum := s[0] + s[1] + s[2]; sum != 100 {
			t.Errorf("%s: shares %v sum to %d\n%s", name, s, sum, r.text)
		}
		if strings.Contains(r.text, "*") {
			t.Errorf("%s: a mark on fully priced rows\n%s", name, r.text)
		}
		for _, cost := range []float64{5, 1.5, 0.25} {
			if want := Money(cost, "USD"); r.amounts && !strings.Contains(r.text, want) {
				t.Errorf("%s: no %s\n%s", name, want, r.text)
			}
		}
	}
}

func TestMixShowsTokensForTheUnpricedRowOfAMixedSet(t *testing.T) {
	for name, r := range mixRenderings(mixSet(mixUsage(100, 5, false), mixUsage(300, 1.5, false), mixUsage(600, 0, true))) {
		s := sharesOf(t, name, r.text)
		if sum := s[0] + s[1] + s[2]; sum != 100 {
			t.Errorf("%s: shares %v sum to %d\n%s", name, s, sum, r.text)
		}
		if strings.Contains(r.text, "$0") {
			t.Errorf("%s: $0 for the unpriced row\n%s", name, r.text)
		}
		if r.amounts && (!strings.Contains(r.text, "600") || !strings.Contains(r.text, Money(5, "USD"))) {
			t.Errorf("%s: want 600 tokens beside the unpriced row and dollars beside the others\n%s", name, r.text)
		}
	}
}

func TestModelMixIsOrderedByTokensNotCost(t *testing.T) {
	rs := mixRenderings(mixSet(mixUsage(100, 5, false), mixUsage(300, 1.5, false), mixUsage(600, 0.25, false)))
	for _, name := range []string{"model box", "models strip", "agents view"} {
		text := rs[name].text
		c, b, a := strings.Index(text, "c "), strings.Index(text, "b "), strings.Index(text, "a ")
		if !(c >= 0 && c < b && b < a) {
			t.Errorf("%s: want c, b, a by tokens\n%s", name, text)
		}
	}
}
