package data

import (
	"cmp"
	"context"
	"encoding/json"
	"fmt"
	"time"
)

// opsTimeout bounds `factory ops list`, which only reads the store on this machine.
const opsTimeout = 10 * time.Second

// OpsWatch is one entry of `factory ops list --json`. Timestamps are the ISO strings the factory
// printed: the view parses them, and shows one that does not parse as it came.
type OpsWatch struct {
	Name          string   `json:"name"`
	Repo          string   `json:"repo"` // absolute path
	Goal          string   `json:"goal"`
	State         string   `json:"state"`   // watching, alarmed, ended, stopped or out-of-budget
	Verdict       *string  `json:"verdict"` // met, not-met or inconclusive once ended; nil before
	Started       string   `json:"started"`
	Until         string   `json:"until"`
	EverySeconds  int64    `json:"every_seconds"`
	Ticks         int      `json:"ticks"`  // looks run
	Missed        int      `json:"missed"` // looks missed in total
	LastLook      *OpsLook `json:"last_look"`
	NextLook      *string  `json:"next_look"` // nil when the watch is not active
	SpentUSD      float64  `json:"spent_usd"` // priced looks only
	UnpricedTicks int      `json:"unpriced_ticks"`
	BudgetUSD     float64  `json:"budget_usd"`
	Notes         string   `json:"notes"` // absolute path
}

// OpsLook is the last look of a watch.
type OpsLook struct {
	TS          string `json:"ts"`
	Outcome     string `json:"outcome"` // fine, alarm, done or could-not-look
	Saw         string `json:"saw"`
	LateSeconds int64  `json:"late_seconds"`
}

// OpsWatches runs `factory ops list --json` in the repository root and returns the watches in the
// order it printed them.
func (r *Runner) OpsWatches(ctx context.Context) ([]OpsWatch, error) {
	script, err := r.factoryScript()
	if err != nil {
		return nil, err
	}
	b, err := r.factoryOutput(ctx, "factory ops list", r.Root, cmp.Or(r.Timeout, opsTimeout), script, "ops", "list", "--json")
	if err != nil {
		return nil, err
	}
	var out struct {
		Watches []OpsWatch `json:"watches"`
	}
	if err := json.Unmarshal(b, &out); err != nil {
		return nil, fmt.Errorf("decode factory ops output: %w", err)
	}
	return out.Watches, nil
}
