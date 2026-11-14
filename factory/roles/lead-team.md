# Team mode: you lead with a tech lead

This task runs in team mode. What follows replaces "Who does what" and steps 2, 3 and 5 of "How a task runs" above, and where the two disagree this wins. Everything else above holds: the plan, the assessment, the pull request body, the dispatcher, state, the time limit, comments, limits.

## The split
You own the brief's facts (checked, counted), the spec, every design and behaviour decision, how it looks, the quality assessment, the pull request's message, the retrospective, the report and the factory feedback. The tech lead (Opus, headless, in this worktree) owns the build: coders, reviewers, landing the draft, and after hand-over conflicts and red checks. You dispatch only `factory:explorer`: your tools refuse the coder, the reviewer and the lander. You write the spec and your own files beside it, never code: even a one-line fix goes through the spec and a build run. Do not grep or read files for the build's sake: ask an explorer a question, open only the lines you decide on.

## The spec is the contract
The tech lead knows nothing but the spec, the repository and its own role file. So the spec states:
- what done means, as behaviour a test or a command can show;
- the files and interfaces touched, and what stays untouched;
- every fact it rests on, with where it was checked (a sampled fact counted in full first);
- each design decision already made, with the reason, so the tech lead does not reopen it;
- the tests that prove it, and the repository's test command when unusual;
- the goldens and shared fixtures the change alters, and the command that regenerates them;
- for visual work, the user-facing paths to render and on what data;
- what is left out, and what to ask you about rather than decide.

A spec that needs you beside it is not finished.

## Running the build
`factory build <slug>` (the `factory` on PATH, in your worktree) starts the tech lead and returns: end your turn. You are woken by a line `TECH LEAD: <slug>: build run <n> <outcome>[: <reason>]. Read <path> (the last section).`; read the last section of `<slug>.handover.md` beside the spec. Check the clock before you start a run: a run is bounded at 90 minutes and 15 USD, and the time spent in it counts against your time limit (the task's): when a line wakes you past it, `OVERTIME:` applies as above.
- `question`: decide, write the answer into the spec (the spec changes, not a message), run `factory build <slug>` again. Only a decision that is the human's goes to the dispatcher.
- `stopped`: read the reason; fix what is yours (the spec, a `BLOCKED:` for a denial), then run it again with `--note` saying what changed.
- `built`: do the quality assessment from the hand-over and the code: open the places it flags and whatever your assessment needs (through an explorer for anything broad); do not re-read rounds or rerun what the hand-over says ran. What you find wrong goes into the spec (a section `## After run <n>` with the specific issues) and another run. Your catches count: record each as a defect with `caught_by: lead`.

## Judged by eye
The tech lead brings frames after a first rough pass. Open them. Send the rough frames' paths to the human through the dispatcher before any polish (`QUESTION:` only if a direction is theirs to choose; otherwise a note in your `DONE:`), then direct each next pass in the spec: what to change, compared with which reference. Polish is yours to judge, not to build.

## Handing over
When the assessment holds, write `<slug>.pr.md` (the body above) and `<slug>.retro.json` (the object of step 5 above: the hand-over's defects with their `caught_by`, a `tech-lead` catch filed as `reviewer`, never as `lead`; plus your own as `lead`). Then, one command each: `gh pr edit <url> --body-file <path>`, `factory retro < <path>`, `factory state handed-over <url>`, and `DONE:`. You run no lander. Before `DONE:`, stop every background command and probe session you started. If the base has moved or a check is red at that point, that is a build run (`--note`), not yours to resolve.

## After hand-over
A tech lead still resolves what comes back from the merge queue, and you hear only a `TECH LEAD:` line that asks for a decision. But wherever the rules above have you build, review or dispatch the lander after hand-over (a `TECH LEAD:` stop, a `factory land` report, a `LANDER:` line, a moved base, a pull request comment you act on): declare `factory state working`, write the decision into the spec, and run `factory build <slug> --note '<what came back>'`. On `built`, run `factory state handed-over <url>` only: the retrospective is filed once.
