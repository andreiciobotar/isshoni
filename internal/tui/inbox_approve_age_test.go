package tui

import (
	"strings"
	"testing"
)

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
