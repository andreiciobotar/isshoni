package tui

import (
	"strings"
	"testing"
)

// At 107 columns the ASK column is 8 wide, one short of "stale #9" once the gap column is kept: the number goes, not the word's end.
func TestInboxApproveAskDropsTheNumberWholeAtTheGap(t *testing.T) {
	for _, l := range strings.Split(plain(noticeModel(107, 40)), "\n") {
		if !strings.Contains(l, "◆ demo-import") {
			continue
		}
		list := strings.SplitN(l, "│", 2)[0]
		if !strings.Contains(list, " stale ") || strings.Contains(list, "…") || strings.Contains(list, "#9") {
			t.Fatalf("the stale row's ASK should read plain stale: %q", list)
		}
		return
	}
	t.Fatal("no stale approve row")
}

func TestInboxApproveRowsShowTheirSlugAndAge(t *testing.T) {
	only := noticeModel(100, 40)
	for _, r := range only.fac.repos {
		if r.st != nil {
			r.st.Decisions = nil
		}
	}
	for name, m := range map[string]*model{"beside decisions": noticeModel(100, 40), "approvals only": only} {
		out := plain(m)
		for slug, age := range map[string]string{"demo-import": "2h", "demo-export": "25m"} {
			found := false
			for _, l := range strings.Split(out, "\n") {
				found = found || strings.Contains(l, "◆ "+slug) && strings.Contains(strings.SplitN(l, "│", 2)[0], " "+age)
			}
			if !found {
				t.Errorf("%s: the %s row lacks its slug or age %s:\n%s", name, slug, age, out)
			}
		}
	}
}
