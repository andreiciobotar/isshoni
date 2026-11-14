---
name: coding-taste
description: The factory's standard for code, always loaded. The smallest complete design, checked facts, proof on the real path, few words.
---

# Coding taste

The coder builds by this, the reviewer holds a change to it, the lead chooses a design by it.
It is taste, not procedure: who commits, reviews and lands is in your role file.

## The smallest complete design

Complete comes first: wiring, validation, security, migration and cleanup included. Small is measured over the whole system, not in lines, and never by leaving a feature out.

Done is fixed before choosing: outcome, interfaces, invariants, security boundaries, tests, non-goals. Scope neither grows nor shrinks.

Take the first rung that is complete and go no further:
1. Delete.
2. Use what already owns the job.
3. Change data or configuration.
4. Write one direct path.
5. Pull a repetition into one small helper.
6. Add an abstraction, for today's callers only, when it takes away more than it brings.

Every behavior has one path, every state one owner, every fact one source.

Refused: anything built for a future nobody asked for (compatibility layers, modes, flags, extension points, frameworks); the same thing twice (a second implementation, a wrapper that only forwards, generic code with a single user); scaffolding that does nothing; a file split too early; a special case far from its owner; a comment that repeats the code beside it.

## Building

- Read first: the repository's instructions, the owning code, the path it really runs on, existing helpers and conventions, the narrowest real test. Leave unrelated changes alone; use the project's environment.
- Check, do not assume: a fact from memory or a sample is a guess. Count them all, run the tool, read the source.
- Wire the change to the real public path, adding only what it requires: code, errors, security, tests, configuration, documentation.
- Prefer a script to a model for anything a script can decide.
- Code that deletes names the condition that makes deleting safe and keeps everything else; it never lists what to spare.
- Every call that leaves the process has a timeout and a visible failure; one slow dependency must not blank the result.
- Never show an unknown as zero or an estimate as a fact: say unpriced, stale as of, estimated.
- Across a boundary between languages or coders, the contract is a literal example with a type beside each field.
- When fixes for single cases keep multiplying, stop and find the invariant that makes the cases irrelevant.

## Proof

- Run the narrow test, the integration and end-to-end runs on the real path, and the repository's checks. Fix and rerun.
- A test fails without the change it tests, waits for the event rather than the clock, and leaves no process behind.
- Then cut duplication, nesting, indirection, flags, wrappers, files and comments until the next cut would cost required behavior or clarity.

## Words

Every token costs a dollar. Result first. Exact names, paths, commands and SHAs; what is blocked, who owns it, what happens next. No filler, repetition, vague claims or account of routine steps. Say plainly what you are unsure of.
