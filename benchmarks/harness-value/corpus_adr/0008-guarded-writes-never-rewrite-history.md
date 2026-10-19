# 0008. Guarded writes: never rewrite history, never force

## Status

Accepted

## Date

2025-03-05

## Context

v0.1 was read-only and said so on its first screen. That was a deliberate
promise: a tool pointed at a repository where several agents are mid-commit must
not be the thing that loses their work.

The cost of being read-only showed up quickly in the Go rewrite. The panel tells
the operator that a branch was never pushed, or that a worktree is safe to
delete, and the operator then has to leave the panel, find the right worktree,
and type the command themselves, often getting the branch name subtly wrong.
Owners asked for the obvious next step to be one key away.

The risk is equally obvious. An agent may be in the middle of a rebase in the
worktree whose branch we are about to touch. A force-push from the wrong
worktree can erase another agent's pushed work. A `reset --hard` cannot be
undone by anyone who does not know about the reflog.

Options considered:

1. **Stay read-only.** Safe, but the friction is the main complaint about the
   tool, and operators are pasting commands from it into a shell anyway.
2. **Execute anything, behind a confirmation.** A confirmation prompt does not
   make a history rewrite safe; it only makes it deliberate. The person
   confirming cannot see what an agent in another worktree is doing.
3. **Execute only additive operations, behind a confirmation; print the rest.**

## Decision

landfirst may perform **guarded writes**: a fixed set of operations that only
*add* to the repository or the remote, each executed only after an explicit
confirmation.

- The allowed set is: `push` (never with `--force` or `--force-with-lease`),
  `worktree add`, `branch -d` (lower-case: refuses to delete unmerged work),
  `switch -c`, and a non-rewriting `merge`.
- Anything that rewrites local history (rebase, reset, amend, `branch -D`,
  force-push) is **printed** for the operator to run themselves, never executed.
- The staged action holds the exact argv that will run, and that argv is what
  is displayed. There is no separate "description" that could drift from the
  command.
- While a confirmation is pending, the confirmation bar swallows every other
  key, so a stray keystroke cannot fire the action or change what is selected
  underneath it.
- A test enumerates every action the engine can produce and asserts that each
  argv's subcommand is in the allowed set and that none carries `--force`.

The README stops claiming the tool is read-only and says plainly what it will
and will not do.

## Consequences

- The most common follow-ups (push a never-pushed branch, delete a fully landed
  one, give a branch a worktree) become one key plus a confirmation.
- Anything that could lose work still requires the operator to type it, which
  keeps a human deliberately in the loop for exactly those cases.
- New action types have to be added to the allowed set explicitly, and the test
  makes adding a rewriting one fail loudly. That friction is intended.
- Worktree removal is handled separately and refuses on a dirty worktree or one
  with an operation in progress, printing the `--force` command instead.
