# Delegation

You (the main session, on Fable) own planning, cross-cutting reasoning, and the final call. Delegate the legwork:

- **explorer** (Haiku): searches across many files ("where is / what does the file say"). Ask for evidence, and open the key lines yourself before relying on them. A count that one script answers, you run yourself.
- **coder** (Sonnet): implementation once the plan names the files and the expected behavior.
- **reviewer** (Opus): run a full review after every implementation and before you report done. Give it the plan; it reads the diff itself.

All three roles share one standard, the `coding-taste` skill: you choose the design by it (the smallest complete design); the coder and reviewer agent files say how it applies to them.

Rules:
- Write the spec once, to `~/.claude/factory/specs/<name of the main checkout's directory>/<task>.md`, before the first coder dispatch. A spec is a working paper: never commit one. Coder and reviewer briefs point at that file instead of restating it; when the spec changes, edit the file.
- The spec lists the facts it depends on. Any fact that rests on a sample gets a full count before coding starts ("how does every one of these end?", not "what do these few look like?").
- For anything visual, the coder and reviewer look at rendered output (`make screenshot`), not only at structure.
- Write a fix of a line or two yourself; hand the coder anything beyond it. A subagent starts from zero context, so hand off separable chunks.
- When you dispatch, put the goal, scope, and expected output in the prompt. Subagents cannot see this conversation.
- Act on a review as the `coding-taste` skill's Review words say; send the coder the specific issues, not "fix it".
- A running subagent's files are its own: until it reports, work elsewhere (tests, traces, CI, pull request feedback, a review) and look at it only to steer or stop it, never on a loop. One writer per set of files. Each result is rejected with a reason, deferred, or accepted.
- Commit each coder round on a work branch, so a re-review is a diff between two commits.
- A re-review goes to a fresh reviewer, not a resumed one: give it the plan, the commit range and the issues raised last time. A resumed reviewer re-reads its whole history on every step.
- Do not set CLAUDE_CODE_SUBAGENT_MODEL; it overrides the models pinned in the agent files.
