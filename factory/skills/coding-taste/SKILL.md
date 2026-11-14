---
name: coding-taste
description: The factory's standard for code. The smallest complete design, checked facts, proof on the real path, few words.
---

# Coding taste

The coder builds by this, the reviewer holds a change to it, the lead chooses a design by it.
Procedure (who commits, reviews, lands) is in your role file.

## The smallest complete design

Complete comes first: security and validation, the wiring, the migration and the cleanup belong to it. Small is measured over the whole system, not in lines, and never by leaving a feature out.

Choose against a written target: what becomes true, what stays true, the interfaces and trust boundaries touched, the tests that show it, what is left out. Taste never moves the scope.

Stop at the first rung that finishes the job:
1. Delete.
2. Use what already owns the job.
3. Change data or configuration.
4. Write one direct path.
5. Pull a repetition into one small helper.
6. An abstraction comes last, and must pay for itself with the callers that exist.

Every fact has one source, every state one owner, every behavior one path.

Refused: building for a future nobody asked for (frameworks, extension points, flags, modes, compatibility with callers that do not exist); the same thing twice (generic code with one user, a wrapper that only forwards, a second implementation); the misplaced or empty (a special case far from its owner, a file split too early, scaffolding that does nothing); a comment that repeats its code.

## Building

- Before writing, find where the behavior lives and how it is reached at run time. Follow the repository's instructions and conventions, reuse its helpers, work in its toolchain, touch nothing unrelated.
- Check, do not assume: a fact from memory or a sample is a guess. Count them all, run the tool, read the source.
- Connect the change to the entry point that really runs it. Tests, error handling, configuration and docs follow the same rule as code: only what the task needs.
- Prefer a script to a model for anything a script can decide.
- Code that deletes names the condition that makes deleting safe and keeps everything else; it never lists what to spare.
- Every call that leaves the process has a timeout and a visible failure; one slow dependency must not blank the result.
- Never show an unknown as zero, old data as current or an estimate as a fact: say unpriced, stale as of, estimated.
- Across a boundary between languages or coders, the contract is a literal example with a type beside each field.
- When fixes for single cases keep multiplying, stop and find the invariant that makes them irrelevant.

## Proof

- A test fails without the change it tests, waits for the event rather than the clock, and leaves no process behind.
- Finish by deleting layers and files as well as lines, until the next deletion would cost behavior or clarity.
- Prove it where it runs, from the smallest test of the real behavior through an end-to-end run to the repository's checks, and run the lot again after every change, deletions included.

## Words

Write for a reader who pays a dollar a word. Outcome first, then what is blocked, on whom, and what happens next, with exact names, paths, commands and SHAs; nothing said twice, no account of routine steps, every claim backed, every doubt stated.
