# The site manager's reference

You decide as the human would have, from the entries below. They are reference, not rules; the figures are what happened, not triggers. Find the nearest entry and case, name it in your reason, and say what differs. A review edits this file from the scores (`factory firings`) and from misses; nothing here is a threshold.

How to use a look: you are given facts and what changed since the last look. Most looks need no call. Make one when you looked into something, when something an entry names is plainly there, or when an earlier call of yours on the task (its `calls`) needs a different answer. Never repeat a call whose answer has not changed.

## nearing-limit
**What:** A working lead has used most of its task's time limit and has not handed over (`elapsed`, `limit` in its row).
**Why it matters:** A lead that will not make it ends in an `overrun` ask or `past-limit`; a call for one that will make it costs the dispatcher a read.
**What it has looked like:** The script used to fire this at 75 % of the limit. It fired 2 times in two days, both suppressed.
**A good call:** The transcript and commits show whether it will hand over by the limit. Escalate to the dispatcher (a lead cannot fix this alone) only if it does not look like it will (no review started, still building, scope visibly grown), one line saying why; a build run under way or the pull request being written: suppress.
**Score (to 2026-11-15):** fired 2; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 2; missed 0.

## past-limit
**What:** A lead is still working past its task's time limit (set by the brief's expected size, never above the setting): `elapsed`, `limit`, `extended_minutes` in its row.
**Why it matters:** A session past its limit spends money whatever its pull request says; a call for a lead that is finishing costs a read.
**What it has looked like:** The script fired this with no threshold, and passed it on unjudged. It fired 5 times in two days: 4 acted on, 1 ignored. Since `factory overrun` a lead asks for time itself, so a call is for a lead over its limit that has not asked.
**A good call:** Look at the transcript tail and `runs`. A lead over its limit that has not asked: escalate to the dispatcher with what it is doing and how far over it is. One that has asked is the `overrun` firing's.
**Score (to 2026-11-15):** fired 5; escalated 5 (acted 4, ignored 1, no outcome 0); suppressed 0; missed 0.

## quiet
**What:** A working lead has written no transcript line and made no commit for a long time (`last_commit`, `last_activity` in its row).
**Why it matters:** A stuck lead burns its time limit; a call for one waiting on a long test costs a read.
**What it has looked like:** The script used to fire this at 20 minutes. It never fired.
**A good call:** A last step that is a long test, build or subagent started within the hour, commits, or transcript progress: large, not stuck, suppress. The same failing step repeated, or no movement far past 20 minutes: escalate.
**Score (to 2026-11-15):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

## blocked-unanswered
**What:** A lead declared itself blocked a while ago and is still blocked (`state`, `declared`, `waits_on` in its row).
**Why it matters:** The human may not have seen the question and the task stands still; a repeat costs a read.
**What it has looked like:** The script used to fire this at 30 minutes. It fired once, suppressed.
**A good call:** Blocked on a question: escalate once; the human may not have seen it. A task paused by the script (`paused`, `waits_on`) for more than 30 minutes: escalate to the dispatcher with the waited tasks and their states; you never lift a pause.
**Score (to 2026-11-15):** fired 1; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 1; missed 0.

## handover-unacknowledged
**What:** A lead handed its work over a long time ago and nobody has acknowledged it (`state`, `declared`).
**Why it matters:** Finished work waits for the human; a call for work handed over minutes ago costs a read.
**What it has looked like:** The script used to fire this at 24 hours. It never fired.
**A good call:** Already visible to the human and needs nothing yet (work handed over barely past 24 hours): suppress.
**Score (to 2026-11-15):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

## busy-after-handover
**What:** A lead declared a hand-over and its session is still not finished (`session`, `children` in its row: up to five child processes with their ages), or a merge was refused as busy.
**Why it matters:** A leftover process keeps the worktree and the session; a call for a test just started costs a read.
**What it has looked like:** The script used to fire this at 6 minutes after the hand-over, on every tick. It fired 2 times in two days: one went to the lead (no outcome), one was suppressed.
**A good call:** `pgrep -P <task.pid>` then `ps` shows more than `children`. A child running a test or build the transcript shows was just started is working, suppress. A background command that outlived its purpose is a leftover: name pid, age, command. A subagent waiting without a process is not in the process list: read the `transcripts` of the subagents before calling a quiet session a leftover. When sessions cannot be listed the rows hold only the worktree: `factory status` has the rest. A tech lead run is neither (see `ended-undeclared`).
**Score (to 2026-11-15):** fired 2; escalated 1 (acted 0, ignored 0, no outcome 1); suppressed 1; missed 0.

## review-loops
**What:** A task has had many reviewer dispatches since it started (`reviews` in its row).
**Why it matters:** A review that never converges burns money; a call on an ordinary run of rounds costs a read.
**What it has looked like:** The script used to fire this at 3 reviewer dispatches. It fired 30 times in two days, 2 acted on of the 2 with an outcome, 28 suppressed: three rounds is ordinary here. `needs-you-band` (2026-11-14): 3 rounds in 27 minutes, a commit 2 minutes ago, applying a review fix and merging main; suppressed.
**A good call:** Look at `reviews`, the commits between rounds and the transcript tail. Rounds that each fix what the last found are convergence: suppress. The same finding coming round again, or rounds without commits: escalate.
**Score (to 2026-11-15):** fired 30; escalated 2 (acted 2, ignored 0, no outcome 0); suppressed 28; missed 0.

## deep-stack
**What:** A task's stack, counting the task itself, is deep (`stack`, `base` in its row).
**Why it matters:** Stacks land bottom-up; a stalled base holds everything above it.
**What it has looked like:** The script used to fire this above 3 tasks deep. It never fired.
**A good call:** Look at `stack` and the state of each task below. A chain whose lower tasks are moving: suppress. A stalled base holding the ones above: escalate, naming it.
**Score (to 2026-11-15):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

## merged-not-cleaned
**What:** A task's pull request was merged, or the task settled, a while ago and it has not been cleaned (`merged` in its row).
**Why it matters:** A leftover worktree costs a slot; the human or `factory clean` clears it.
**What it has looked like:** The script used to fire this at 2 hours. It fired 5 times in two days, all suppressed.
**A good call:** Already visible to the human and needs nothing yet (a worktree merged a short while ago): suppress.
**Score (to 2026-11-15):** fired 5; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 5; missed 0.

## cost-outlier
**What:** A working or blocked task has cost many times the median landed task of the repository (`cost_usd`, `landed_median_usd`; a price the monitor did not give is `unpriced`, never zero).
**Why it matters:** Money spent with nothing to show is the signal; a big task that is progressing is not.
**What it has looked like:** The script used to fire this at 3 times the median. It fired 3 times in two days: 1 acted on, 2 suppressed.
**A good call:** Look at `runs` (cost and outcomes) and the transcript tail. Spend without a hand-over in sight: escalate with the figures. Spend that matches proven work: suppress.
**Score (to 2026-11-15):** fired 3; escalated 1 (acted 1, ignored 0, no outcome 0); suppressed 2; missed 0.

## lead-heavy
**What:** A working or blocked task that has cost at least the median landed task has most of its cost in the lead's own session instead of its subagents (`lead_share_pct`).
**Why it matters:** A lead doing its subagents' work is the dearest way to do it.
**What it has looked like:** The script used to fire this at 85 % of the cost, and only for a task that had cost at least the median: a task in its first minutes shows 100 % in the lead. It never fired.
**A good call:** Look at what the lead does itself in its transcript. Work its subagents should have done: escalate with what. Otherwise suppress.
**Score (to 2026-11-15):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

## wrong-model
**What:** A subagent or lead ran on a model other than the one configured for its role (`models_off`: role and model seen).
**Why it matters:** A dearer model than configured costs money on every call; a cheaper one may do worse work.
**What it has looked like:** It fired 4 times in two days: 0 acted on of the 3 escalated, 1 suppressed. All were deliberate.
**A good call:** The transcript start, brief or session name shows a deliberate probe, or the model the task's brief asks for: suppress.
**Score (to 2026-11-15):** fired 4; escalated 3 (acted 0, ignored 3, no outcome 0); suppressed 1; missed 0.

## overrun
**What:** A lead over its limit, or not converging, asks for time with `factory overrun`. The firing's `detail` is its ask; `runs`, `extended_minutes` and `extension_left_minutes` are the evidence.
**Why it matters:** An ask held back stops a lead that is nearly done; time given for nothing costs a build run.
**What it has looked like:** The script fires it on every tick, with no threshold. It fired 2 times in two days, both extended. `pulse-band-visual` is case 4 below.
**A good call:** Decide by the principles and cases below: `extended` with `minutes`, `paused` with `waits_on` `["human"]`, or `escalated` to the dispatcher. The script tells the lead and the dispatcher about `extended` and `paused`; you message nobody for them.
- Time is given to finish, not to start. What is left decides, not how far over the task is: proven work with review, landing, assessment or hand-over left is finishing; a design still moving is not.
- Money spent with nothing to show is the signal. Runs that end without a hand-over spend without progress: find why before giving more time.
- The cause decides who acts. A cause the lead can remove gets the time and the instruction. A cause only the human can remove (a permission, a push, a direction) gets a pause at once: more time cannot fix it.
- A change of direction from the human or the dispatcher is new work, not the lead's overrun: extend for it.
- Give the time the remaining steps need and say what it is for. A second ask for the same reason is not the same decision: something did not go as said.
- Asking must cost less than the work left. When little is left, decide fast and extend.
- When you cannot tell, pass it to the human with what you found. A pause costs the human a minute; a wrong extension costs a build run.
- `extension_left_minutes` is the human's ceiling. What does not fit under it is the human's decision: escalate.

**Score (to 2026-11-15):** fired 2; extended 2 (kept 1, overran 1, no outcome 0); escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

### Cases
1. `report-tasks` (2026-11-15). Built and reviewed, half a minute past the limit, assessment and hand-over left. Asking the dispatcher took four minutes for ten minutes of work. Decided: extended 17 min. Outcome: handed over within it. Lesson: only hand-over left is the clearest extension there is.
2. `ops-explorer` (2026-11-15). Reopened after hand-over at the human's request; a neighbour then merged a rewrite of the same six files. Fix built and reviewed, landing left. Decided: extended about 40 min. Outcome: handed over within it. Lesson: the overrun came from outside the task and the work was proven.
3. `readme-diagram-rethink` (2026-11-15). The human chose another layout two minutes into a build run that could not be stopped; that run's work was thrown away. Decided: extended 40 min. Outcome: handed over. Lesson: a redirect is new work; extend once for it.
4. `pulse-band-visual` (2026-11-15). The dispatcher redirected it to a second layout and extended 30 min; then review went round again and six build runs cost 16.62 USD, three of them ending with no hand-over. Decided late, by message: no seventh run. Outcome: handed over on the sixth. Lesson: the first extension was right (a redirect); after it, runs ending with nothing were the signal, and the decision should have come at the second such run, not the sixth: tell the lead the cause (case 6) or pause.
5. `lander-agent-file-current` (2026-11-15). Fix proven; two build runs ended early; then its own landing was refused by the fault it fixes. Decided: extended to a fixed time. Outcome: ended blocked, needing the human to push. Lesson: the extension bought nothing: once the cause is a refusal only the human can lift, pause for the human at once.
6. Known fault (open, outbox faec7502cc7a). A headless build run that ends a turn to wait for a background subagent or command is forced to answer at once and files `stopped`, often with no reason or `ended without an answer`, after a few minutes and a few dollars; the work it started is lost. About eight runs on three tasks on 2026-11-15. Recognise it: a `stopped` run with no reason, short, whose transcript ends waiting on a background dispatch. Decided since: a run told to keep every dispatch in the foreground finished. So on the first such run tell the lead to start the next run with a note saying that; when runs keep ending this way after the lead was told, pause for the human.

## run-stopped
**What:** A build run ended `stopped` on a task still `working`. The firing names its reason and how many of the task's runs ended `stopped`, with their cost; the row's `runs` list them.
**Why it matters:** A stopped run spends money and loses the work it started; a call on a one-off costs the lead a turn.
**What it has looked like:** The script fires it on every tick, with no threshold. It fired once in two days, escalated, acted on. `pulse-band-visual` (2026-11-15): six build runs, 16.62 USD, five ending `stopped` (overrun case 4).
**A good call:** Decide by the overrun cases (case 6 is the usual cause): `suppressed`, `escalated` to the lead (one instruction) or the dispatcher, or `paused` for the human. The script tells the lead and the dispatcher about `paused`; you message nobody for it.
**Score (to 2026-11-15):** fired 1; escalated 1 (acted 1, ignored 0, no outcome 0); suppressed 0; missed 0.

## collision
**What:** Two tasks in flight (or a task and the default branch or an open pull request) have committed work that conflicts line by line. The firing's `detail` has both sides, the conflicting files with each side's changed line ranges, what each has changed so far and the specs.
**Why it matters:** A late conflict costs the human a second approval, not just the resolution; a finding costs a paragraph in a build run, and noise teaches tech leads to skip them.
**What it has looked like:** The script fires it on every tick, with no threshold. It never fired; the four cases below are what it is for.
**A good call:** Read the two specs, `git diff`/`git log` on the two branches and the two transcripts as far as you need. Decide by the principles and cases below: `told` with `tell` (the slugs of the tasks whose tech lead should hear it) and a `reason` written for that tech lead, or `suppressed`. You message nobody: the script hands a told finding to the tech lead's next build run.
- Speak only when a tech lead would build differently because of it: order its work, merge the base first, leave lines alone, land after the other. Sharing a file is not a reason; the same function or the same paragraph is.
- Say what the other task changed and where (file, function or lines), which of the two is further along, and what you would do. One finding, not a list.
- Tell the side that can move cheaply: the one with less built, or the one not yet handed over. Telling both is right only when each must do something.
- A conflict with the default branch on a working task matters when the task will build more on top of the stale lines; when its next landing will merge the base anyway and the conflict is mechanical (both added separate lines), suppress.
- Two specs collide when they change the same behaviour or rewrite the same block, not when they name the same file. When two specs build the same thing, that is the human's: escalate to the dispatcher.
- A finding costs a paragraph in a build run, not a lead's turn; still, each is scored (`acted`, `noise`, `collided`): noise teaches tech leads to skip them.

**Score (to 2026-11-15):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

### Cases
1. `site-manager-decides` and `readme-diagram-ops` (2026-11-15). The first changed one label in two diagram files (`assets/diagrams/factory-narrow.svg`, `factory-wide.svg`); the second re-laid out both. #109 merged first; #110 conflicted in both files 13 seconds later, a tech lead took main's files and redid the label in 4 minutes, and the human's approval went stale: about 40 minutes until a fresh one. A good call, once both had committed: `told` to `site-manager-decides` only: the other task redraws these files, yours changes a label; leave the files alone until it lands, or make the label change last. Lesson: a small edit yields to a rewrite, and the price of a late conflict is the human's second approval, not the resolution.
2. `lander-agent-file-current` and `visual-work-draft-pr` (2026-11-15). Adjacent lines of `factory/roles/tech-lead-build.md`, steps 3 and 4 of "How a run goes": one rewrote a sentence, the other deleted the next. #105 merged, #106 conflicted 14 seconds later; resolved mechanically in 4 minutes, then a fresh approval was needed. A good call: `told` to the one further from landing, with the two line numbers: merge the base as soon as the other lands, before the human approves. When both are already handed over: suppress, the queue has it. Lesson: a mechanical conflict is still worth a line when an approval is at stake.
3. `ops-explorer` and `team-is-the-only-mode` (2026-11-15). Started two minutes apart; the second rewrote six files the first was adding to (`docs/factory.md`, `factory/bin/factory`, `factory/roles/lead.md`, the dispatch skill, two test files) and merged (#96) while the first was handed over; the queue recorded no conflict, but the first was reopened and rebuilt on the rewrite: about 20 minutes and one coder round. A good call, at `new-spec`, having read both specs: `told` to `ops-explorer`: the other task rewrites the files yours adds to and is ahead; build to its branch's shape or land after it. Lesson: git sees no conflict when one side rewrites what the other extends; only the specs show it.
4. `ops-watch` and `report-tasks` (2026-11-15). Each shared four to six of the hot files with neighbours that merged mid-task (#96, #97, #99); the queue recorded no conflict for either. A good call: nothing; no `collision` fires, and at `new-spec`, specs that touch different behaviour in the same files: suppress. Lesson: the hot files are shared by everything (31 merged pull requests changed `docs/factory.md` in two days); the same file is never the finding.

## new-spec
**What:** A working task's spec has appeared while another open task has an older spec, or has no spec but has commits (so only the newer spec's task fires). The firing's `detail` names both specs and what the other task has changed so far. Fired once per task.
**Why it matters:** Git sees no conflict when one side rewrites what the other extends; only the specs show it. Before any conflict exists is the cheapest time to say so.
**What it has looked like:** The script fires it on every tick, with no threshold. It never fired; collision cases 3 and 4 are the ones it is for.
**A good call:** Read both specs, then decide as for `collision`, by its principles and cases.
**Score (to 2026-11-15):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

## churning
**What:** A handed-over task the queue already sent back conflicts again, while other open tasks change the same files. The firing's `detail` and evidence name them with their files.
**Why it matters:** A task sent back again and again burns tech lead runs on a conflict that resolves itself once the others land.
**What it has looked like:** The script fires it on every tick, with no threshold. It never fired. `lander-agent-file-current` (2026-11-15) was sent back by the queue for a conflict at 13:12:00Z and stopped at 13:18:32Z as stale.
**A good call:** Look at the others' transcripts and pull requests only. Wait for the others that will land within minutes (handed over, or in review or landing): decide `paused` with `waits_on`, the slugs of those others, exactly as the detail names them. None will land soon: `suppressed` (resolving now is right; the send-back goes ahead). You cannot tell which should wait: `escalated`. On `paused` send the dispatcher one informing line, no answer expected: first line `PAUSED: <the repo directory's name>`, then `<id> churning <slug>: paused, waits for <slugs> on <files>; released by the script when they merge`. Message nobody else. The script pauses the task and releases it; you never lift it.
**Score (to 2026-11-15):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

## base-merged
**What:** A working or blocked task is stacked on a branch whose pull request has been merged into the default branch.
**Why it matters:** A lead that builds on a merged base builds on stale lines; a call to a lead that was already told wakes it for nothing.
**What it has looked like:** The script fires this with no threshold, on the full pass. It fired 3 times in two days: 2 acted on, 1 ignored.
**A good call:** The transcript shows the lead was already told, or is merging the base in: suppress. Otherwise tell the lead that its base has merged and that it starts a build run for it (whose lander merges the default branch in); with no live lead, the dispatcher.
**Score (to 2026-11-15):** fired 3; escalated 3 (acted 2, ignored 1, no outcome 0); suppressed 0; missed 0.

## ci-red-default
**What:** The newest decisive CI run of a workflow on the default branch did not succeed.
**Why it matters:** A red default branch is a fact the dispatcher or the human must hear whatever the cause.
**What it has looked like:** The script fires this with no threshold, on the full pass (up to 29 minutes after it goes red), and used to pass it on unjudged. It fired 2 times in two days: 1 acted on, 1 without outcome.
**A good call:** Escalate to the dispatcher with the workflow and the failing run.
**Score (to 2026-11-15):** fired 2; escalated 2 (acted 1, ignored 0, no outcome 1); suppressed 0; missed 0.

## ci-red-handover
**What:** A task handed over a pull request that has a failing check.
**Why it matters:** A red pull request cannot merge; the lander sends it back to its tech lead itself, so a call may only repeat that.
**What it has looked like:** The script fires this with no threshold, on the full pass. It never fired.
**A good call:** The lander sent it back (a tech lead is working the task): suppress. A held pull request (kind `not-ours`) is not in it: see `red-waiting`. A red check standing on a handed-over pull request nobody has sent back: escalate with the check's name.
**Score (to 2026-11-15):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

## red-waiting
**What:** The queue holds a pull request (kind `not-ours`: its red check is a test outside what the branch changes, which also failed on independent branches) and it has waited past the grace with no open pull request naming it in an `Unblocks:` line (`waiting` in the watch body).
**Why it matters:** A held pull request is approved and ready but cannot merge until the fix lands; with no fix naming it, nobody is working on its release.
**What it has looked like:** It never fired.
**A good call:** Look at `waiting` (`tests`, `named_by`) and the open pull requests for a fix. A fix exists and the pull request names it: suppress. No fix: escalate to the dispatcher with the tests and the wait, so a fix task is opened with the line. A fix merged without the line: `factory unblock <pr> --by <#fix>`.
**Score (to 2026-11-16):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.

### Cases
1. `closes-dead-tasks-finish` (2026-11-16). #138, approved, went red on check `test` at `a08df2a0`: two `CollisionTests` in `factory/tests/test_collision.py`, a file it does not change; the same test had failed in attempt 1 on two other branches that morning (runs 43870829205, 43875627358). It was put back to draft and `working`; a tech lead run of 26 minutes found nothing of its own; the task was paused on the human; the fix was another task's (#148). Decided (in hindsight): hold it ready and approved, tell the human once, unblock when the fix merges. Lesson: a failure that also happens without the branch is the fix task's, and the waiting pull request needs a name on the fix, not a lead.

## ended-undeclared
**What:** A lead's session ended without it declaring a hand-over or a block: a task declared `working` with no live lead session and no build run holding its lock. Seen on the tick it happens.
**Why it matters:** The task stands still and nobody knows; the lead may be a tech lead run, or may have been handing over a minute ago.
**What it has looked like:** The script used to fire this after 10 minutes of nothing, on the full pass. It fired 3 times in two days: 2 acted on, 1 ignored. `kaizen-review-role` (2026-11-15) fired at 15:22:43Z, "active 47m ago", seen 47 minutes after its lead went; escalated, acted on. `kaizen-review-role`'s lead session went at 13:33:49Z while its build run held the lock until 14:54:10Z; the firing waits for the run to end, and the row shows `session: none` before that.
**A good call:** Escalate, unless the tail shows the hand-over was under way minutes ago. A headless session in a task's worktree while the task is `working` by role `lander` or `tech-lead` is a tech lead run (bounded at 30 minutes; a build run, started by the lead's `factory build`, at 90 minutes, and its lead waits on it, idle: neither is a stray nor ended): never messaged.
**Score (to 2026-11-15):** fired 3; escalated 3 (acted 2, ignored 1, no outcome 0); suppressed 0; missed 0.

## unlisted
**What:** Something worth a call that no other entry names.
**Why it matters:** The file is a best guess; what it lacks is found by looking, and a repeated one belongs in an entry.
**What it has looked like:** It never fired.
**A good call:** Name `unlisted` as the entry, the task, and in `detail` the facts you saw, one line. A review turns repeated ones into entries.
**Score (to 2026-11-15):** fired 0; escalated 0 (acted 0, ignored 0, no outcome 0); suppressed 0; missed 0.
