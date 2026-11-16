---
name: coding-taste
description: The factory's standard for code. The smallest complete design, checked facts, proof on the real path, few words.
---

# Coding taste

The coder builds by this, the reviewer holds a change to it, the lead chooses a design by it.
It is taste, not procedure: who commits, reviews and lands is in your role file.

## The smallest complete design

Complete comes first: the behavior asked for, with its security and validation, its wiring into everything it connects to (callers, configuration, build), its migration and its cleanup. Small is measured over the whole system, not in lines, and never by leaving a feature out.

Choose against a written target: what becomes true, what stays true, the interfaces and trust boundaries touched, the tests that show it, what is left out. The scope never moves.

Stop at the first rung that finishes the job:
1. Delete.
2. Use the owner or helper that already does the job.
3. Change data or configuration.
4. Write one direct path.
5. Pull a repetition into one small helper.
6. An abstraction comes last, and must pay for itself with the callers that exist.

Every fact has one source, every state one owner, every behavior one path.

Refused: building for a future nobody asked for (frameworks, extension points, flags, modes, compatibility with callers that do not exist); the same thing twice (generic code with one user, a wrapper that only forwards, a second implementation); the misplaced or empty (a special case far from its owner, a file split too early, scaffolding that does nothing); a comment that repeats its code.

## Building

- Before writing, find where the behavior lives, how it is reached at run time, and the narrowest test that exercises it. Follow the repository's instructions and conventions, reuse its helpers, work in its toolchain, make no unrelated edit, and leave unrelated changes already in the worktree as you found them.
- Check, do not assume: a fact from memory or a sample is a guess. Count them all, run the tool, read the source.
- The change must be reachable from the real public entry point, not only from a test. Bring exactly what the task needs: its tests and docs, its configuration, its error handling and security, and the code itself.
- Prefer a script to a model for anything a script can decide.
- Code that deletes names the condition that makes deleting safe and keeps everything else; it never lists what to spare.
- Every call that leaves the process has a timeout and a visible failure; one slow dependency must not blank the result.
- Never show an unknown as zero, old data as current or an estimate as a fact: say unpriced, stale as of, estimated.
- Across a boundary between languages or coders, the contract is a literal example with a type beside each field.
- When fixes for single cases keep multiplying, stop and find the invariant that makes them irrelevant.

## Then cut

Once it works, read the change again as if someone else wrote it, and delete: a comment, a file, a wrapper, a flag, a layer of indirection, a level of nesting, code that repeats other code. Keep going until the next deletion would lose behavior the task needs or make the code harder to follow.

## Proof

- A test fails without the change it tests, waits for the event rather than the clock, and leaves no process behind.
- Prove it where it runs: the narrow test of the real behavior, then integration, then an end-to-end run, then the repository's own checks. A failure is fixed and the whole set run again; so is the set after the last cut.
- It is not finished until whatever is unverified is named, the checks have passed on the final diff, that diff holds only the change, and the behavior works on the real path.

## Words

The reader bills you a dollar a word. So nothing is padding, nothing is said twice, no routine step is narrated and no claim is vague: write the exact command, path, SHA or name instead of describing it, one short concrete claim to a sentence. The outcome comes first, then the next action, then each blocker with its owner. A doubt is stated as a doubt.

## Review words

A review is written and read in these words, and no others.
- Verdict: `CHANGES_NEEDED` when at least one issue is a blocker, else `APPROVE`.
- `blocker`: the change must not land as it is. It is fixed, and a fresh reviewer, given the commit range and the earlier issues, reviews the fix.
- `minor`: the change can land once it is fixed. Whoever briefed the reviewer checks the fix (the tests, that round's diff); there is no new review.
- A word from outside is read by its meaning, the stricter way when in doubt: a level that must be fixed before landing (`major`, `critical`) is a blocker and any lesser one (`nit`, `suggestion`) is minor; a verdict that approves (`APPROVED`) is `APPROVE` and any other is `CHANGES_NEEDED`.

A brief decides what a review covers (the range, the spec, where to look hardest) and is followed in all of that. It does not change these words: asked for others, answer in these. Nothing was guessed, so there is no cost to report.
