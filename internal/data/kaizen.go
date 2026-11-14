package data

import "context"

// Kaizen is the document `isshoni kaizen --json` prints: the measures of whether the
// factory is getting better. A JSON null decodes to the zero value, or to nil where a
// number or a record may be absent.
type Kaizen struct {
	Generated   string        `json:"generated"`
	FactoryDir  string        `json:"factory_dir"`
	StoreFound  bool          `json:"store_found"`
	Prices      Prices        `json:"prices"`
	Roles       []string      `json:"roles"` // the role order
	Repos       []KaizenRepo  `json:"repos"`
	Versions    []KaizenVer   `json:"versions"`
	Tasks       []KaizenTask  `json:"tasks"`
	Summary     KaizenSummary `json:"summary"`
	Stops       []KaizenStop  `json:"stops"`
	Slowed      []KaizenSlow  `json:"slowed"`
	Rewards     []KaizenAward `json:"rewards"`
	Outbox      KaizenOutbox  `json:"outbox"`
	SiteManager SiteManager   `json:"site_manager"`
	TechLead    TechLead      `json:"tech_lead"`
	Pulse       *Pulse        `json:"pulse"`    // nil from a producer that does not write it
	Friction    *Friction     `json:"friction"` // likewise
}

// Friction is where the factory loses money and time, over the selected range. A nil
// number is unknown, not zero; Queue is nil when there is no queue log.
type Friction struct {
	Dispatcher    FrictionDispatcher `json:"dispatcher"`
	Rework        FrictionRework     `json:"rework"`
	Causes        FrictionCauses     `json:"causes"`
	Refusals      FrictionRefusals   `json:"refusals"`
	Wakeups       FrictionWakeups    `json:"wakeups"`
	Queue         *FrictionQueue     `json:"queue"`
	Held          []FrictionHeld     `json:"held"`
	LateConflicts FrictionLate       `json:"late_conflicts"`
}

// FrictionDispatcher is the dispatcher's main thread: its priced calls and the median context of a call.
type FrictionDispatcher struct {
	Calls          int      `json:"calls"`
	Cost           float64  `json:"cost"`
	PerCall        *float64 `json:"per_call"`
	ContextMedian  *float64 `json:"context_median"`
	UnpricedTokens int64    `json:"unpriced_tokens"`
}

// FrictionRework is the cost of every call after a task's first review; PerTaskMedian and N are over the tasks that had one.
type FrictionRework struct {
	Cost          float64  `json:"cost"`
	TaskCost      float64  `json:"task_cost"`
	Share         *float64 `json:"share"`
	PerTaskMedian *float64 `json:"per_task_median"`
	N             int      `json:"n"`
}

// FrictionCauses counts the defects of the latest retros by cause; Unstated is every other value or none.
type FrictionCauses struct {
	Spec     int `json:"spec"`
	Brief    int `json:"brief"`
	Conflict int `json:"conflict"`
	Scope    int `json:"scope"`
	Code     int `json:"code"`
	Unstated int `json:"unstated"`
}

// FrictionRefusals is the Bash calls a guard or the auto mode classifier refused, by role.
type FrictionRefusals struct {
	AfterCalls int               `json:"after_calls"`
	Roles      []FrictionRefusal `json:"roles"`
}

// FrictionRefusal is one role; Share is nil when it made no Bash call, AfterCost is the spend in the calls after the refusals.
type FrictionRefusal struct {
	Role       string   `json:"role"`
	Bash       int      `json:"bash"`
	Guard      int      `json:"guard"`
	Classifier int      `json:"classifier"`
	Share      *float64 `json:"share"`
	AfterCost  float64  `json:"after_cost"`
}

// FrictionWakeups is the lead prompts from a session after hand-over, and the spend after them.
type FrictionWakeups struct {
	Prompts   int      `json:"prompts"`
	Tasks     int      `json:"tasks"`
	Cost      float64  `json:"cost"`
	PerPrompt *float64 `json:"per_prompt"`
}

// FrictionQueue is the queue steps taken per merged pull request, from Since, the oldest queue record.
type FrictionQueue struct {
	Since     string   `json:"since"`
	Merged    int      `json:"merged"`
	Updated   int      `json:"updated"`
	Rerun     int      `json:"rerun"`
	SentBack  int      `json:"sent_back"`
	Stopped   int      `json:"stopped"`
	PerMerged *float64 `json:"per_merged"`
}

// FrictionHeld is one task's queue send-backs and pauses.
type FrictionHeld struct {
	Repo          string `json:"repo"`
	Slug          string `json:"slug"`
	SentBack      int    `json:"sent_back"`
	Paused        int    `json:"paused"`
	PausedMinutes int    `json:"paused_minutes"`
	Prioritized   int    `json:"prioritized"`
}

// FrictionLate is the conflicts after hand-over from two sources that are never added: the
// queue log (Log is nil without one) and the history of the base merges the Pulse looked up.
type FrictionLate struct {
	Tasks    []FrictionLateTask `json:"tasks"`
	Files    []FrictionLateFile `json:"files"`
	LogTasks int                `json:"log_tasks"`
	GitTasks int                `json:"git_tasks"`
	GitPRs   int                `json:"git_prs"`
}

type FrictionLateTask struct {
	Repo string `json:"repo"`
	Slug string `json:"slug"`
	Log  *int   `json:"log"`
	Git  int    `json:"git"`
}

type FrictionLateFile struct {
	Repo string `json:"repo"`
	File string `json:"file"`
	Log  *int   `json:"log"`
	Git  int    `json:"git"`
}

// Pulse is today against yesterday: the day's figures over all tasks, whatever the range.
// QueueSince is when the queue log starts, nil when there is none.
type Pulse struct {
	QueueSince *string  `json:"queue_since"`
	Today      PulseDay `json:"today"`
	Yesterday  PulseDay `json:"yesterday"`
}

// PulseDay is one calendar day: Merged pull requests, and the figures over them.
type PulseDay struct {
	Date      string         `json:"date"`
	Merged    int            `json:"merged"`
	Cost      PulseCost      `json:"cost"`
	FirstPass PulseFirstPass `json:"first_pass"`
	Handover  PulseHandover  `json:"handover"`
	Conflicts PulseConflicts `json:"conflicts"`
}

// PulseCost is what the factory spent on the day, USD; PerMerged is nil when nothing merged.
type PulseCost struct {
	Tasks          float64  `json:"tasks"`
	Dispatcher     float64  `json:"dispatcher"`
	SiteManager    float64  `json:"site_manager"`
	UnpricedTokens int64    `json:"unpriced_tokens"`
	PerMerged      *float64 `json:"per_merged"`
}

// PulseRate is Yes of Of tasks.
type PulseRate struct {
	Yes int `json:"yes"`
	Of  int `json:"of"`
}

// PulseFirstPass is the first-pass rate by the size a task stated.
type PulseFirstPass struct {
	Small    PulseRate `json:"small"`
	Medium   PulseRate `json:"medium"`
	Large    PulseRate `json:"large"`
	Unstated PulseRate `json:"unstated"`
}

// PulseP90 is a 90th percentile in seconds over N tasks; nil when N is 0.
type PulseP90 struct {
	P90S *int `json:"p90_s"`
	N    int  `json:"n"`
}

// PulseHandover is the time from hand-over to merge and its two halves. SentBack and
// SentBackUnchanged are nil when the queue log did not cover the day.
type PulseHandover struct {
	ToMerge             PulseP90 `json:"to_merge"`
	ToApproval          PulseP90 `json:"to_approval"`
	ApprovalToMerge     PulseP90 `json:"approval_to_merge"`
	ApprovalsBackfilled int      `json:"approvals_backfilled"`
	SentBack            *int     `json:"sent_back"`
	SentBackUnchanged   *int     `json:"sent_back_unchanged"`
}

// PulseConflicts counts the base merges of the day's merged pull requests that conflicted;
// PerMerged is nil when Prs is 0, Error is git's failure, nil when it ran.
type PulseConflicts struct {
	Prs        int      `json:"prs"`
	BaseMerges int      `json:"base_merges"`
	Conflicted int      `json:"conflicted"`
	PerMerged  *float64 `json:"per_merged"`
	Error      *string  `json:"error"`
}

// SiteManager is the site manager's record as `factory firings` counts it, over every
// repository: precision is acted/(acted+ignored), recall acted/(acted+missed), nil when
// there is nothing to divide by.
type SiteManager struct {
	Escalated        int      `json:"escalated"`
	Acted            int      `json:"acted"`
	Ignored          int      `json:"ignored"`
	NoOutcome        int      `json:"no_outcome"`
	Suppressed       int      `json:"suppressed"`
	Missed           int      `json:"missed"`
	MissedSuppressed int      `json:"missed_suppressed"`
	MissedNeverFired int      `json:"missed_never_fired"`
	Precision        *float64 `json:"precision"`
	Recall           *float64 `json:"recall"`
}

// TechLead is what came back from the merge queue and who resolved it. Cost is the sum of
// the priced records and nil when none is priced: unpriced, never zero.
type TechLead struct {
	CameBack     int      `json:"came_back"`
	Resolved     int      `json:"resolved"`
	Escalated    int      `json:"escalated"`
	Stopped      int      `json:"stopped"`
	Unstarted    int      `json:"unstarted"` // went to the lead without a tech lead run
	Cost         *float64 `json:"cost"`
	CostResolved *float64 `json:"cost_resolved"`
	Landed       int      `json:"landed"`       // landed tasks in the window
	LeadWakeups  int      `json:"lead_wakeups"` // over those tasks, after the first hand-over
}

// KaizenRepo says whether the pull requests of one repository could be read, and how old
// they are.
type KaizenRepo struct {
	Repo     string `json:"repo"`
	RepoPath string `json:"repo_path"`
	PRs      string `json:"prs"`       // "ok" when pull request data was applied, else why gh could not be used
	PRsAsOf  string `json:"prs_as_of"` // ISO time of the cached data; empty when there is none
	PRsError string `json:"prs_error"` // why this run could not refresh it; empty when it could
}

type KaizenVer struct {
	Version   string `json:"version"`
	FirstSeen string `json:"first_seen"`
}

// Roles is money by role, keyed by the names in Kaizen.Roles.
type Roles map[string]float64

// Defects counts retro defects by who caught them.
type Defects struct {
	Tests    int `json:"tests"`
	Reviewer int `json:"reviewer"`
	Lead     int `json:"lead"`
	CI       int `json:"ci"`
	Human    int `json:"human"`
	Other    int `json:"other"`
}

type KaizenCost struct {
	Total          float64  `json:"total"`
	UnpricedTokens int64    `json:"unpriced_tokens"`
	ByRole         Roles    `json:"by_role"`
	Rework         float64  `json:"rework"`
	AfterReview    *float64 `json:"after_review"` // every call after the first review; nil without one
}

type KaizenPR struct {
	Number int    `json:"number"`
	URL    string `json:"url"`
	State  string `json:"state"`
	Opened string `json:"opened"`
	Merged string `json:"merged"`
}

type KaizenTask struct {
	Repo      string        `json:"repo"`
	RepoPath  string        `json:"repo_path"`
	Slug      string        `json:"slug"`
	Version   string        `json:"version"`
	Size      *string       `json:"size"` // small, medium or large as the brief stated; nil when it did not
	Started   string        `json:"started"`
	State     string        `json:"state"`
	PR        *KaizenPR     `json:"pr"`
	Landed    bool          `json:"landed"`
	Mode      string        `json:"mode"` // "single" or "team"
	BriefToPR *float64      `json:"brief_to_pr_s"`
	PRToMerge *float64      `json:"pr_to_merge_s"`
	Cost      *KaizenCost   `json:"cost"`
	Rounds    *int          `json:"review_rounds"`
	FirstPass *bool         `json:"first_pass"`
	Stops     int           `json:"stops"`
	StoppedS  float64       `json:"stopped_s"`
	Defects   *Defects      `json:"defects"`
	Authoring TaskAuthoring `json:"authoring"`
}

// TaskAuthoring is the product code and test lines and calls each role made in a task; LeadShare
// is nil when nobody wrote any.
type TaskAuthoring struct {
	Lines     map[string]int `json:"lines"`
	Calls     map[string]int `json:"calls"`
	LeadShare *float64       `json:"lead_share"`
}

// Authoring is the product code and test lines over a version's landed tasks or a point's window:
// Tasks are those in which any role wrote some, LeadTasks those in which the lead did. Share is each
// role's part of the lines (pooled), nil when Tasks is 0.
type Authoring struct {
	Tasks     int                `json:"tasks"`
	LeadTasks int                `json:"lead_tasks"`
	Lines     map[string]int     `json:"lines"`
	Share     map[string]float64 `json:"share"`
}

// Overhead is what the dispatcher and the site manager cost in the period, USD, over the landed tasks.
type Overhead struct {
	Dispatcher     float64  `json:"dispatcher"`
	SiteManager    float64  `json:"site_manager"`
	Sessions       int      `json:"sessions"`
	UnpricedTokens int64    `json:"unpriced_tokens"`
	Landed         int      `json:"landed"`
	PerLanded      *float64 `json:"per_landed"`
}

// Median is a median over the n landed tasks that have the value; nil when n is 0.
type Median struct {
	Median *float64 `json:"median"`
	N      int      `json:"n"`
}

// Pair is a measure's median over the latest landed tasks and over the earlier ones.
type Pair struct {
	Latest  *float64 `json:"latest"`
	Earlier *float64 `json:"earlier"`
}

// KaizenTrend compares the latest half of the landed tasks with the earlier half.
type KaizenTrend struct {
	LatestN   int  `json:"latest_n"`
	EarlierN  int  `json:"earlier_n"`
	Cost      Pair `json:"cost"`
	BriefToPR Pair `json:"brief_to_pr_s"`
	PRToMerge Pair `json:"pr_to_merge_s"`
	Rounds    Pair `json:"review_rounds"`
}

type KaizenTime struct {
	Building  float64 `json:"building_s"`
	Stopped   float64 `json:"stopped_s"`
	MergeWait float64 `json:"merge_wait_s"`
}

// KaizenMixPoint is each role's median share of a task's cost over the Window landed
// tasks ending at Slug.
type KaizenMixPoint struct {
	Slug    string `json:"slug"`
	Version string `json:"version"`
	Share   Roles  `json:"share"`
}

type KaizenMix struct {
	Window int              `json:"window"`
	Points []KaizenMixPoint `json:"points"`
}

// AnatomyMedians is a role's medians per task over the window's tasks in which it ran.
type AnatomyMedians struct {
	Calls *float64 `json:"calls"`
	Peak  *float64 `json:"peak"`
	Tools *float64 `json:"tools"`
}

// AnatomyPoint is the role medians over the Window landed tasks ending at Slug; a role
// that ran in fewer than three of them is nil.
type AnatomyPoint struct {
	Slug      string                     `json:"slug"`
	Version   string                     `json:"version"`
	Roles     map[string]*AnatomyMedians `json:"roles"`
	Authoring Authoring                  `json:"authoring"`
}

// AnatomyClasses is the share of a role's cost in each token class; they sum to 1.
type AnatomyClasses struct {
	Input      float64 `json:"input"`
	Output     float64 `json:"output"`
	CacheWrite float64 `json:"cache_write"`
	CacheRead  float64 `json:"cache_read"`
}

// AnatomyRole is a role's medians per task over the N landed tasks of a version in which it
// ran. Tools are its own tool calls, dispatches not counted.
type AnatomyRole struct {
	N                int             `json:"n"`
	Share            float64         `json:"share"`
	Cost             float64         `json:"cost"`
	Calls            float64         `json:"calls"`
	Peak             *float64        `json:"peak"`
	Tools            float64         `json:"tools"`
	Dispatches       float64         `json:"dispatches"`
	ToolsPerDispatch *float64        `json:"tools_per_dispatch"`
	Classes          *AnatomyClasses `json:"classes"`
}

// AnatomyVersion is one plugin version over its landed tasks with a cost; a role that did
// not run in any of them is nil.
type AnatomyVersion struct {
	Version   string                  `json:"version"`
	Tasks     int                     `json:"tasks"`
	Roles     map[string]*AnatomyRole `json:"roles"`
	Authoring Authoring               `json:"authoring"`
}

// KaizenAnatomy is what drives a task's cost, per role, over time and per plugin version.
// A version is compared only with Min landed tasks.
type KaizenAnatomy struct {
	Window   int              `json:"window"`
	Min      int              `json:"min"`
	Points   []AnatomyPoint   `json:"points"`
	Versions []AnatomyVersion `json:"versions"`
}

type KaizenSummary struct {
	Tasks     int    `json:"tasks"`
	Landed    int    `json:"landed"`
	InFlight  int    `json:"in_flight"`
	Cost      Median `json:"cost"`
	BriefToPR Median `json:"brief_to_pr_s"`
	PRToMerge Median `json:"pr_to_merge_s"`
	Rounds    Median `json:"review_rounds"`
	FirstPass struct {
		Yes int `json:"yes"`
		Of  int `json:"of"`
	} `json:"first_pass"`
	ReworkShare *float64      `json:"rework_share"`
	ByRole      Roles         `json:"by_role"`
	Mix         KaizenMix     `json:"mix"`
	Anatomy     KaizenAnatomy `json:"anatomy"`
	Overhead    Overhead      `json:"overhead"`
	Time        KaizenTime    `json:"time"`
	Defects     Defects       `json:"defects"`
	TrendMin    int           `json:"trend_min"`
	Trend       *KaizenTrend  `json:"trend"`
	ByMode      []ModeRow     `json:"by_mode"`
}

// ModeRow compares the landed tasks of one mode and stated size; a nil number is unknown, not zero.
type ModeRow struct {
	Mode               string   `json:"mode"`
	Size               *string  `json:"size"`
	N                  int      `json:"n"`
	CostMedian         *float64 `json:"cost_median"`
	LeadShareMedian    *float64 `json:"lead_share_median"`
	LeadCallsMedian    *float64 `json:"lead_calls_median"`
	HandoverSMedian    *float64 `json:"handover_s_median"`
	FirstPass          int      `json:"first_pass"`
	ReviewRoundsMedian *float64 `json:"review_rounds_median"`
	Defects            *Defects `json:"defects"`
	LeadWakeups        int      `json:"lead_wakeups"`
	BuildRunsMedian    *float64 `json:"build_runs_median"`
}

// KaizenStop is a moment the line stopped for the human, or a permission was denied.
type KaizenStop struct {
	TS      string   `json:"ts"`
	Repo    string   `json:"repo"`
	Slug    string   `json:"slug"`
	Kind    string   `json:"kind"` // QUESTION, BLOCKED, OVERTIME, DENIED
	WaitedS *float64 `json:"waited_s"`
	Why     string   `json:"why"`
}

type KaizenSlow struct {
	TS   string `json:"ts"`
	Repo string `json:"repo"`
	Slug string `json:"slug"`
	Text string `json:"text"`
}

type KaizenAward struct {
	Kind string `json:"kind"` // first_pass_streak, record_low_cost, record_fast
	Slug string `json:"slug"`
	Text string `json:"text"`
}

// OutboxCounts is entries by status.
type OutboxCounts struct {
	Open     int `json:"open"`
	Accepted int `json:"accepted"`
	Rejected int `json:"rejected"`
	Done     int `json:"done"`
}

type OutboxTopic struct {
	Topic string `json:"topic"`
	Open  int    `json:"open"`
}

type OutboxEntry struct {
	ID     string `json:"id"`
	TS     string `json:"ts"`
	Repo   string `json:"repo"`
	Slug   string `json:"slug"`
	Role   string `json:"role"`
	Kind   string `json:"kind"` // issue, decision
	Status string `json:"status"`
	Topic  string `json:"topic"` // issues only
	Text   string `json:"text"`
	PR     string `json:"pr"`
}

type KaizenOutbox struct {
	Issues    OutboxCounts  `json:"issues"`
	Decisions OutboxCounts  `json:"decisions"`
	Topics    []OutboxTopic `json:"topics"`
	Entries   []OutboxEntry `json:"entries"`
}

// Kaizen runs `isshoni kaizen [--since X] [--cached] --json`; cached never asks GitHub.
func (r *Runner) Kaizen(ctx context.Context, since string, cached bool) (*Kaizen, error) {
	var out Kaizen
	args := append([]string{"kaizen"}, sinceArgs(since)...)
	if cached {
		args = append(args, "--cached")
	}
	if err := r.run(ctx, &out, args...); err != nil {
		return nil, err
	}
	return &out, nil
}
