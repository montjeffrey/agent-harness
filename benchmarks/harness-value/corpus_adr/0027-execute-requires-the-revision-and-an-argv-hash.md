# 0027. Execute requires the snapshot revision and an argv hash

## Status

Accepted

## Date

2025-10-15

## Context

With decision trees (0026), actions are computed by the engine and executed on
request by a client. Today the only client is the panel, in the same process.
Soon there will be others (a `serve` mode for agents and editors), and a client
may be a program that did not show anything to a human at all.

0008 requires that what runs is exactly what was shown. In a single process with
a confirmation bar that is easy to guarantee. Across a protocol boundary it is
not: a client could request "execute action 3" without having displayed action
3, or after the repository moved and action 3 now means something different.

Two separate guarantees are needed:

1. **The state has not moved.** The action was computed against the current
   state of the repository.
2. **The caller saw the exact command.** The argv the engine will run is the
   argv the caller displayed, not a description of it.

Options considered for (1):

- **Re-derive the action at execute time and run it if it still exists.**
  Rejected: an action with the same ID can mean something different after the
  repository moves (the branch now points elsewhere).
- **Require the snapshot revision (0021) in the request** and refuse if it does
  not match the current one. Chosen.

Options considered for (2):

- **A random nonce** issued with each decision tree and returned with the execute
  request. Rejected: it proves only that the caller received the tree, not that it
  showed the command; and it makes the engine stateful (it has to remember which
  nonces it issued, for which client, until when), which does not survive a
  restart or multiple clients.
- **Type-to-confirm**, where the caller must echo the branch name, as some web UIs
  do for destructive operations. Rejected: far too slow for a one-key panel flow,
  and none of the allowed actions is destructive enough to justify it.
- **A hash of the argv itself.** The caller computes SHA-256 over the action's
  argv elements joined by a NUL byte and sends it. The engine recomputes it from
  the action it would run and refuses on mismatch. Stateless, and a caller can
  only produce the right value if it has the exact argv in hand.

## Decision

`ExecuteRequest` carries the unit, the action ID, the snapshot revision the
action was computed against, and a confirmation: the hex SHA-256 of the action's
argv joined with `\x00`. NUL is used as the separator because it cannot appear in
an argument, so two different argv lists cannot join to the same string.

`Execute` refuses with a specific error if the revision is stale, if the action
does not exist in the current tree, or if the confirmation does not match. On
success it runs the argv, builds a new full snapshot, and returns the
post-action revision.

## Consequences

- An action can only run against the state it was computed for, and only if the
  caller held its exact argv.
- The engine stays stateless between requests; any client, including one started
  after the tree was computed, can execute with the right revision and hash.
- The confirmation proves possession of the argv, not that a human looked at it.
  The panel's confirmation bar remains the human gate; the hash keeps clients
  honest about what they are confirming.
- Tests compute the confirmation with the documented recipe, so changing the
  recipe is a protocol change and breaks them deliberately.
