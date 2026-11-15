package data

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const kaizenFixture = "../tui/testdata/kaizen-fixture.json"

// stub is a python that records its arguments and prints the fixture.
func kaizenStubRunner(t *testing.T) (*Runner, string) {
	t.Helper()
	dir := t.TempDir()
	script := filepath.Join(dir, "py.sh")
	body := "#!/bin/sh\necho \"$@\" > " + filepath.Join(dir, "args") + "\nexec cat " + kaizenFixture + "\n"
	abs, _ := filepath.Abs(kaizenFixture)
	body = strings.ReplaceAll(body, kaizenFixture, abs)
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return &Runner{Root: "../..", Python: script}, filepath.Join(dir, "args")
}

func TestKaizenDecodesTheFixtureAndPassesSince(t *testing.T) {
	r, args := kaizenStubRunner(t)
	doc, err := r.Kaizen(context.Background(), "7d", false)
	if err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(args); strings.TrimSpace(string(b)) != "-m isshoni kaizen --since 7d --json" {
		t.Fatalf("args: %q", b)
	}
	s := doc.Summary
	if !doc.StoreFound || len(doc.Tasks) != 24 || s.Landed != 20 || s.InFlight != 3 || s.Trend == nil || s.Trend.LatestN != 10 || s.FirstPass.Of != 19 {
		t.Fatalf("summary: %+v", s)
	}
	if s.Cost.Median == nil || *s.Cost.Median != 6.2 || s.ReworkShare == nil {
		t.Fatalf("medians: %+v", s.Cost)
	}
	pts := s.Mix.Points
	if !slices.Equal(doc.Roles, []string{"lead", "tech-lead", "coder", "reviewer", "explorer", "lander", "other"}) || s.Mix.Window != 5 || len(pts) != 15 || pts[14].Share["other"] != 0.138889 || pts[14].Version != "0.3.0" {
		t.Fatalf("roles and mix: %v %+v", doc.Roles, s.Mix)
	}
	tl := doc.TechLead
	if tl.CameBack != 9 || tl.Resolved != 6 || tl.Escalated != 2 || tl.Stopped != 0 || tl.Unstarted != 1 || tl.Cost == nil || *tl.Cost != 9.8 || tl.CostResolved == nil || *tl.CostResolved != 7.1 || tl.Landed != 20 || tl.LeadWakeups != 3 || s.ByRole["tech-lead"] != 3.2 {
		t.Fatalf("tech lead: %+v %v", tl, s.ByRole)
	}
	last := doc.Tasks[20]
	if last.Slug != "fx-quiet-refactor" || !last.Landed || last.PR == nil || last.PR.Number != 120 || last.Cost == nil || last.Cost.Total != 2.31 || last.Cost.ByRole["lander"] != 0.2 || last.Rounds == nil || *last.Rounds != 1 {
		t.Fatalf("task: %+v", last)
	}
	if doc.Tasks[3].Cost != nil || doc.Tasks[3].Rounds != nil || doc.Tasks[5].PR != nil || doc.Tasks[5].State != "dismissed" {
		t.Fatal("nulls decode to nil")
	}
	if len(doc.Stops) != 6 || doc.Stops[5].WaitedS != nil || doc.Stops[3].Why == "" || len(doc.Rewards) != 2 || len(doc.Outbox.Entries) != 27 || doc.Outbox.Topics[0].Topic != "brief" {
		t.Fatalf("stops, rewards, outbox: %d %d %d", len(doc.Stops), len(doc.Rewards), len(doc.Outbox.Entries))
	}
	if _, err := r.Kaizen(context.Background(), "", false); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(args); strings.TrimSpace(string(b)) != "-m isshoni kaizen --json" {
		t.Fatalf("no range, no --since: %q", b)
	}
}

func TestKaizenDecodesThePulse(t *testing.T) {
	r, _ := kaizenStubRunner(t)
	doc, err := r.Kaizen(context.Background(), "", false)
	if err != nil {
		t.Fatal(err)
	}
	p := doc.Pulse
	if p == nil || p.QueueSince == nil || *p.QueueSince != "2026-11-14T09:00:00Z" {
		t.Fatalf("pulse: %+v", p)
	}
	d, b := p.Today, p.Baseline
	if d.Date != "2026-11-14" || d.To != d.Date || d.Days != 1 || d.Merged != 12 || d.Cost.Tasks != 100.5 || d.Cost.PerMerged == nil || *d.Cost.PerMerged != 10.25 || d.FirstPass.Small != (PulseRate{3, 4}) || d.FirstPass.Unstated.Of != 4 {
		t.Fatalf("today: %+v", d)
	}
	h := d.Handover
	if h.ToMerge.P50S == nil || *h.ToMerge.P50S != 1200 || h.ToMerge.P90S == nil || *h.ToMerge.P90S != 2204 || h.ToMerge.N != 11 || h.ApprovalsBackfilled != 2 || h.SentBack == nil || *h.SentBack != 3 || h.SentBackUnchanged == nil || *h.SentBackUnchanged != 1 {
		t.Fatalf("handover: %+v", h)
	}
	if c := d.Conflicts; c.Prs != 11 || c.BaseMerges != 9 || c.Conflicted != 4 || c.PerMerged == nil || c.Error != nil {
		t.Fatalf("conflicts: %+v", c)
	}
	if b.Date != "2026-11-07" || b.To != "2026-11-13" || b.Days != 7 || b.Merged != 50 || b.Cost.UnpricedTokens != 1200 || b.Handover.SentBack != nil {
		t.Fatalf("baseline: %+v", b)
	}
	if len(p.Days) != 8 || p.Days[0].Date != b.Date || p.Days[7].Date != d.Date || p.Days[0].Handover.ApprovalToMerge.P50S != nil || p.Days[0].Handover.ApprovalToMerge.N != 0 {
		t.Fatalf("days, oldest first, today last, nulls decode to nil: %d", len(p.Days))
	}
	v := p.Verdicts
	if v.Cost == nil || *v.Cost != "better" || v.FirstPass == nil || *v.FirstPass != "worse" || v.ApprovalToMerge == nil || *v.ApprovalToMerge != "same" || v.Conflicts == nil || *v.Conflicts != "worse" {
		t.Fatalf("verdicts: %+v", v)
	}
	if doc.Tasks[0].Size == nil || *doc.Tasks[0].Size != "small" || doc.Tasks[2].Size != nil {
		t.Fatal("a task's stated size, nil when none")
	}
}

func TestKaizenCachedFlagAndPRDates(t *testing.T) {
	r, args := kaizenStubRunner(t)
	if _, err := r.Kaizen(context.Background(), "7d", true); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(args); strings.TrimSpace(string(b)) != "-m isshoni kaizen --since 7d --cached --json" {
		t.Fatalf("args: %q", b)
	}
	var doc Kaizen
	if err := json.Unmarshal([]byte(`{"repos":[{"repo":"a","repo_path":"/a","prs":"ok","prs_as_of":"2026-11-14T13:40:00Z","prs_error":"HTTP 403"},{"repo":"b","prs":"not fetched yet","prs_as_of":null,"prs_error":null}]}`), &doc); err != nil {
		t.Fatal(err)
	}
	if a, b := doc.Repos[0], doc.Repos[1]; a.PRsAsOf != "2026-11-14T13:40:00Z" || a.PRsError != "HTTP 403" || b.PRsAsOf != "" || b.PRsError != "" {
		t.Fatalf("repos: %+v", doc.Repos)
	}
}
