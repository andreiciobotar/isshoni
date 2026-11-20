package tui

import (
	"fmt"
	"slices"

	"charm.land/bubbles/v2/key"
)

// keyMap is the one definition of every key: the handler matches against it and the
// footer and the help overlay read their text from it. A binding without help text
// is a second key for the one documented before it.
type keyMap struct {
	Interrupt, Quit, Help                    key.Binding
	Next, Prev, Inbox, Dash, Agents, Factory key.Binding
	Kaizen, Ops                              key.Binding
	Pick, Confirm, Mark, Accept, Reject      key.Binding
	History                                  key.Binding
	PageLeft, PageRight                      key.Binding
	Down, Up, Top, Bottom, PageDown, PageUp  key.Binding
	Fold, Unfold, Open, Back                 key.Binding
	Range, Follow, Sort, Filter              key.Binding
	FilterKeep, FilterClear, Erase, Wipe     key.Binding
	CapEdit, CapSave, CapCancel              key.Binding
	Hanami, Legend, Zen, Replay              key.Binding
	Dispatcher                               key.Binding
}

var keys = keyMap{
	Interrupt: key.NewBinding(key.WithKeys("ctrl+c")),
	Quit:      key.NewBinding(key.WithKeys("q"), key.WithHelp("q", "quit")),
	Help:      key.NewBinding(key.WithKeys("?"), key.WithHelp("?", "help")),

	Next:    key.NewBinding(key.WithKeys("tab"), key.WithHelp("tab", "next view")),
	Prev:    key.NewBinding(key.WithKeys("shift+tab"), key.WithHelp("shift+tab", "previous view")),
	Inbox:   key.NewBinding(key.WithKeys("1"), key.WithHelp("1 2 3 4 5 6 7", "inbox sessions agents factory kaizen ops hanami")),
	Dash:    key.NewBinding(key.WithKeys("2")),
	Agents:  key.NewBinding(key.WithKeys("3")),
	Factory: key.NewBinding(key.WithKeys("4")),
	Kaizen:  key.NewBinding(key.WithKeys("5")),
	Ops:     key.NewBinding(key.WithKeys("6")),
	Hanami:  key.NewBinding(key.WithKeys("7")),

	// the Inbox: a digit picks an option of the card under the cursor, then the pick is confirmed
	Pick:    key.NewBinding(key.WithKeys("1", "2", "3", "4"), key.WithHelp("1-4", "pick an option")),
	Confirm: key.NewBinding(key.WithKeys("enter"), key.WithHelp("enter", "do the marked one")),
	Mark:    key.NewBinding(key.WithKeys("left", "right", "h", "l", "tab"), key.WithHelp("←→/hl", "mark accept · reject")),
	Accept:  key.NewBinding(key.WithKeys("y"), key.WithHelp("y n", "accept · reject")),
	History: key.NewBinding(key.WithKeys("H"), key.WithHelp("H", "all history")),
	Reject:  key.NewBinding(key.WithKeys("n", "esc")),

	PageLeft:  key.NewBinding(key.WithKeys("h", "left"), key.WithHelp("←→/hl", "kaizen page")),
	PageRight: key.NewBinding(key.WithKeys("l", "right")),

	Down:     key.NewBinding(key.WithKeys("j", "down"), key.WithHelp("↑↓/jk", "move")),
	Up:       key.NewBinding(key.WithKeys("k", "up")),
	Top:      key.NewBinding(key.WithKeys("g", "home"), key.WithHelp("g G", "first · last")),
	Bottom:   key.NewBinding(key.WithKeys("G", "end")),
	PageDown: key.NewBinding(key.WithKeys("pgdown", "ctrl+d"), key.WithHelp("pgdn pgup", "page")),
	PageUp:   key.NewBinding(key.WithKeys("pgup", "ctrl+u")),

	Fold:   key.NewBinding(key.WithKeys("left", "h"), key.WithHelp("←→/hl", "fold repo/runs")),
	Unfold: key.NewBinding(key.WithKeys("right", "l")),

	Open: key.NewBinding(key.WithKeys("enter"), key.WithHelp("enter", "open")),
	Back: key.NewBinding(key.WithKeys("esc", "backspace"), key.WithHelp("esc", "back")),

	Range:  key.NewBinding(key.WithKeys("r"), key.WithHelp("r", "range")),
	Follow: key.NewBinding(key.WithKeys("f"), key.WithHelp("f", "follow")),
	Sort:   key.NewBinding(key.WithKeys("s"), key.WithHelp("s", "sort")),
	Filter: key.NewBinding(key.WithKeys("/"), key.WithHelp("/", "filter")),

	FilterKeep:  key.NewBinding(key.WithKeys("enter"), key.WithHelp("enter", "keep")),
	FilterClear: key.NewBinding(key.WithKeys("esc"), key.WithHelp("esc", "clear")),
	Erase:       key.NewBinding(key.WithKeys("backspace")),
	Wipe:        key.NewBinding(key.WithKeys("ctrl+u")),

	Dispatcher: key.NewBinding(key.WithKeys("d"), key.WithHelp("d", "dispatcher")),

	CapEdit:   key.NewBinding(key.WithKeys("c"), key.WithHelp("c", "edit cap")),
	CapSave:   key.NewBinding(key.WithKeys("enter"), key.WithHelp("enter", "save")),
	CapCancel: key.NewBinding(key.WithKeys("esc"), key.WithHelp("esc", "cancel")),

	// the Hanami tab's own keys: its footer shows them; the help overlay has no row left for them at 24 rows
	Legend: key.NewBinding(key.WithKeys("l"), key.WithHelp("l", "legend")),
	Zen:    key.NewBinding(key.WithKeys("z"), key.WithHelp("z", "full screen")),
	Replay: key.NewBinding(key.WithKeys("p"), key.WithHelp("p", "replay 24h · live")),
}

// footerKeys is what the footer advertises, most useful first; the footer drops from
// the middle when the terminal is narrow.
func (m *model) footerKeys() []key.Binding {
	switch {
	case m.filterIn:
		return []key.Binding{keys.FilterKeep, keys.FilterClear}
	case m.fac.editing:
		return []key.Binding{keys.CapSave, keys.CapCancel}
	case m.view == viewInbox && m.inbox.pick != 0:
		return []key.Binding{keys.Confirm, keys.Mark, keys.Accept}
	case m.view == viewInbox:
		bs := []key.Binding{keys.Down, keys.CapEdit, keys.History, keys.Next, keys.Help, keys.Quit}
		if rows := m.messages(); m.inbox.cursor >= len(rows) || rows[m.inbox.cursor].app == nil { // an approval has nothing to pick
			bs = slices.Insert(bs, 1, m.pickHint())
		}
		return bs
	case m.view == viewFactory:
		return []key.Binding{keys.Down, keys.CapEdit, keys.Range, keys.Next, keys.Help, keys.Quit}
	case m.view == viewHanami:
		return []key.Binding{keys.Legend, keys.Zen, keys.Replay, keys.Next, keys.Help, keys.Quit}
	case m.view == viewKaizen:
		return []key.Binding{keys.PageLeft, keys.Down, keys.Range, keys.Next, keys.Help, keys.Quit}
	case m.view == viewOps:
		return []key.Binding{keys.Down, keys.Next, keys.Help, keys.Quit}
	case m.zoomed():
		return []key.Binding{keys.Down, keys.Open, keys.Back, keys.Follow, keys.Next, keys.Help, keys.Quit}
	case m.view == viewAgents:
		return []key.Binding{keys.Down, keys.Range, keys.Next, keys.Back, keys.Help, keys.Quit}
	}
	return []key.Binding{keys.Down, keys.Open, keys.Filter, keys.Fold, keys.Sort, keys.Range, keys.Follow, keys.Next, keys.Help, keys.Quit}
}

// helpGroups is the help overlay: every binding that has help text, the dispatcher's only
// where attaching is available.
func (m *model) helpGroups() [][]key.Binding {
	more := []key.Binding{keys.Follow, keys.CapEdit}
	if m.opts.Attach != nil {
		more = append(more, keys.Dispatcher)
	}
	groups := [][]key.Binding{
		{keys.Down, keys.Top, keys.PageDown, keys.Fold, keys.Open, keys.Back},
		{keys.Next, keys.Prev, keys.Inbox, keys.PageLeft},
		append([]key.Binding{keys.Range, keys.Sort, keys.Filter}, more...),
		{keys.Help, keys.Quit},
	}
	if m.view == viewInbox {
		groups = slices.Insert(groups, 2, []key.Binding{keys.Pick, keys.Confirm, keys.Mark, keys.Accept, keys.History})
	}
	return groups
}

// pickHint is the Pick binding with the option range of the card under the cursor.
func (m *model) pickHint() key.Binding {
	c, ok := m.cursorCard()
	if !ok || len(c.d.Options) == 0 {
		return keys.Pick
	}
	return key.NewBinding(key.WithKeys(keys.Pick.Keys()...), key.WithHelp(fmt.Sprintf("1–%d", min(len(c.d.Options), 9)), "pick an option"))
}
