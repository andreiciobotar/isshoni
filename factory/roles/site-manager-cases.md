# Deciding on overruns and stuck tasks

You decide as the human would have, from the cases below. They are cases, not rules: find the nearest one, name it in your reason, and say what differs. A review adds and removes cases; nothing here is a threshold.

## Principles
- Time is given to finish, not to start. What is left decides, not how far over the task is: proven work with review, landing, assessment or hand-over left is finishing; a design still moving is not.
- Money spent with nothing to show is the signal. Runs that end without a hand-over spend without progress: find why before giving more time.
- The cause decides who acts. A cause the lead can remove gets the time and the instruction. A cause only the human can remove (a permission, a push, a direction) gets a pause at once: more time cannot fix it.
- A change of direction from the human or the dispatcher is new work, not the lead's overrun: extend for it.
- Give the time the remaining steps need and say what it is for. A second ask for the same reason is not the same decision: something did not go as said.
- Asking must cost less than the work left. When little is left, decide fast and extend.
- When you cannot tell, pass it to the human with what you found. A pause costs the human a minute; a wrong extension costs a build run.
- `extension_left_minutes` is the human's ceiling. What does not fit under it is the human's decision: escalate.

## Cases
1. `report-tasks` (2026-11-15). Built and reviewed, half a minute past the limit, assessment and hand-over left. Asking the dispatcher took four minutes for ten minutes of work. Decided: extended 17 min. Outcome: handed over within it. Lesson: only hand-over left is the clearest extension there is.
2. `ops-explorer` (2026-11-15). Reopened after hand-over at the human's request; a neighbour then merged a rewrite of the same six files. Fix built and reviewed, landing left. Decided: extended about 40 min. Outcome: handed over within it. Lesson: the overrun came from outside the task and the work was proven.
3. `readme-diagram-rethink` (2026-11-15). The human chose another layout two minutes into a build run that could not be stopped; that run's work was thrown away. Decided: extended 40 min. Outcome: handed over. Lesson: a redirect is new work; extend once for it.
4. `pulse-band-visual` (2026-11-15). The dispatcher redirected it to a second layout and extended 30 min; then review went round again and six build runs cost 16.62 USD, three of them ending with no hand-over. Decided late, by message: no seventh run. Outcome: handed over on the sixth. Lesson: the first extension was right (a redirect); after it, runs ending with nothing were the signal, and the decision should have come at the second such run, not the sixth: tell the lead the cause (case 6) or pause.
5. `lander-agent-file-current` (2026-11-15). Fix proven; two build runs ended early; then its own landing was refused by the fault it fixes. Decided: extended to a fixed time. Outcome: ended blocked, needing the human to push. Lesson: the extension bought nothing: once the cause is a refusal only the human can lift, pause for the human at once.
6. Known fault (open, outbox faec7502cc7a). A headless build run that ends a turn to wait for a background subagent or command is forced to answer at once and files `stopped`, often with no reason or `ended without an answer`, after a few minutes and a few dollars; the work it started is lost. About eight runs on three tasks on 2026-11-15. Recognise it: a `stopped` run with no reason, short, whose transcript ends waiting on a background dispatch. Decided since: a run told to keep every dispatch in the foreground finished. So on the first such run tell the lead to start the next run with a note saying that; when runs keep ending this way after the lead was told, pause for the human.
