# 0026. Decision trees for acting on a verdict

## Status

Accepted

## Date

2025-10-08

## Context

0008 allowed guarded writes, and v0.4 bound a few of them to keys: push the
selected branch, delete it if safe. That worked for verdicts with one obvious
next step and failed for the rest. A **diverged** branch, where both the local
branch and its remote moved, has several reasonable next steps (merge the remote
into local, rebase local onto remote, inspect first, push to a new branch), and
which is right depends on facts the operator knows and the tool does not. The
v0.4 behaviour was to offer nothing for diverged branches, which is precisely
where operators most wanted help.

The interaction we wanted: press one key on a branch and be led to the right
action, with the tool doing the deciding where it can and asking where it
cannot.

Options considered:

1. **A flat menu of every action that applies to the verdict.** Rejected: for
   diverged branches it presents rewriting and non-rewriting options side by
   side with no guidance, and invites picking the first one.
2. **Always choose for the operator** and show a single suggested action.
   Rejected for verdicts where the choice depends on intent. The tool cannot know
   whether the remote's extra commits are a colleague's work to keep or a stale
   force-push to discard.
3. **A decision tree per unit.** Each node is either a question (numbered
   options, one marked as the engine's suggestion), an action (an exact command,
   confirmed before running, from the allowed set in 0008), or advice (a command
   printed for the operator to run, for anything that rewrites history or where
   guessing is unsafe). Most verdicts have a single sensible next step, so their
   tree is a single leaf.

## Decision

The engine computes a `DecisionTree` for a unit on request (`Decide`). The tree
is stamped with the snapshot revision it was computed from (0021).

- Leaf **actions** carry the exact argv to execute and an ID; executing one goes
  through `Execute`, which checks the revision and a confirmation (recorded
  separately).
- Leaf **advice** carries a command to display, never to run.
- **Questions** carry numbered options (`[1]`, `[2]`, ...) leading to subtrees,
  and the engine's own pick is marked `(suggested)`. A question can lead to
  another question before reaching a leaf.
- The protocol reserves a field on the unit for the engine's single
  recommendation, so that scripted clients can eventually see it without
  walking the tree.

In the Bubble Tea panel, `p` on a selected branch in the topology view opens its tree in
the bottom pane; `esc` steps back one level, and at the top level leaves the tree.

## Consequences

- Diverged branches get guided help instead of nothing, and the history-rewriting
  options stay printed rather than executed.
- The tree is computed against a specific revision, so if the repository moves
  while the operator is reading it, executing a leaf is refused as stale rather
  than running against a state the operator did not see.
- Computing a tree requires a full snapshot, since the right action can depend on
  contention; the engine builds one if it has none.
- New verdicts must come with a tree. A unit with no tree is a gap, not a
  default.
