# Cross-device path claims: assessment

Date: 2026-11-23
Status: assessment only. No design is chosen. Three small experiments decide it;
they are listed at the end.

## Question

Path claims stop two agent sessions on one machine from editing the same files
(ADR 0002). Should a session on one device also see, and respect, claims held by
a session on another device?

This is kept apart from memory sync on purpose. Memory tolerates a delay of
minutes. A claim that arrives minutes late has already failed.

## What exists today

- `work_start` records a task under `~/.agent-harness/work/<repository-id>/`:
  session id, exact repository-relative paths, base SHA, branch, worktree.
- Starting a task refuses paths an active task already claims.
- In Claude Code the `claim_guard` hook refuses an edit to a path another
  session claims. For other clients the claim is cooperative.
- State is local to one machine, and a short file lock serializes transitions.
- **A claim has no expiry.** It is held until `work_finish`. A session that dies
  holds its paths until someone finishes the task by hand.
- A second, separate claim exists on memory tasks (`memory_claims`): one owner
  per agent program, judged stale by age. It becomes cross-device for free once
  memory syncs, with the same delay as memory.

## What a cross-device claim can and cannot do

Two devices have two checkouts. They cannot corrupt each other's files. A
collision between them shows up later, as a merge conflict or as the same work
done twice.

So a cross-device claim is a warning, not a lock:

- It can tell an agent, before it starts, that another device is working on
  those paths, and refuse `work_start` unless overridden.
- It cannot be enforced the way a local claim is. The other device may be
  offline, asleep, or behind on sync.
- It must fail open. If the claim service cannot be reached, work proceeds with
  a visible note. A laptop with no network must still be able to start a task.

Whatever transport is chosen, three things have to exist first, and they improve
single-machine claims too:

1. **Expiry and heartbeat.** A claim carries a time-to-live and is renewed while
   its session is active. An expired claim is free to take.
2. **Device identity** on every claim, the same device id the memory store uses.
3. **A stable repository identity** across devices: the root commit SHA, as in
   the memory design. Today's id hashes a local path.

## Options

### L1. Claims as files in the synced store folder

One small file per device per repository, listing that device's active claims.

- For: no new infrastructure; same transport as memory; works offline.
- Claimed paths go wherever the operator's sync tool sends the folder. With a
  cloud drive that is the provider's servers, the same as memory.
- Against: delay of seconds to minutes, so two devices can both start inside the
  window. Good enough to notice overlap after the fact, not to prevent it.

### L2. Claims as refs on the project's git remote

Each active task publishes a ref such as
`refs/agent-harness/claims/<device>/<task>` that points at a small object
listing its paths. `work_start` lists those refs before it starts, and publishes
its own after.

- For: no server to run; every device that can push already has the transport;
  delay is one network round trip; the remote is the natural meeting point for
  devices that will merge there anyway.
- Against:
  - Needs network and push access. A read-only clone cannot claim.
  - Check-then-publish is a race. Two devices can both pass the check. The fix
    is to re-list after publishing and have the later claim back off, which
    narrows the window and does not close it.
  - On a public repository the claim is public. It would carry paths and ids
    only, never task summaries.
  - Custom ref namespaces must be accepted by the hosting service and must not
    trigger CI. Not yet verified.
  - A repository with no remote gets nothing.

### L3. A small claim service on a private network

One device runs a service; every device's MCP server calls it.

- For: the only option with a real atomic claim, immediate release, and push
  notification when a claim changes. One place to see every device.
- Against: needs a device that is always on and reachable; a second thing to
  install, secure and keep running; a much larger ask for anyone else adopting
  the harness. When the service is down every device falls back to local claims,
  so the fallback has to be built regardless.

### L4. A hosted coordination service

A managed key-value store or realtime database.

- For: atomic, immediate, nothing to host.
- Against: an account, a network dependency, and file paths and task ids leaving
  the operator's machines. Conflicts with the harness's "nothing leaves the
  machine" promise.

## Comparison

| | L1 synced files | L2 git refs | L3 private service | L4 hosted |
|---|---|---|---|---|
| Delay | seconds to minutes | about a second | immediate | immediate |
| Prevents a simultaneous start | no | mostly | yes | yes |
| Works offline | yes, stale | no, falls back | no, falls back | no, falls back |
| Extra infrastructure | none | none | always-on device | external account |
| Data leaves the operator's machines | depends on the sync tool: no for device-to-device sync, yes for a cloud drive | to the git remote | no | yes |
| Others can adopt it | trivially | if they have a remote | only with a host | only with an account |

## Preliminary recommendation

Not a decision. The order that risks least:

1. **Build the three prerequisites** (expiry and heartbeat, device identity,
   stable repository identity). They are needed by every option and fix the
   stuck-claim problem on a single machine.
2. **Ship L1 as visibility only.** Other devices' claims appear in `work_status`
   and in the `work_start` reply as a warning. Nothing is refused. Each device
   appends a start event and a finish event for every claim, with paths and
   times, and keeps them after the claim ends. Overlap is then computed from
   the intervals once the logs have synced. A list of currently active claims
   would miss two tasks that both started and finished before either device
   synced, and would make overlap look rarer than it is.
3. **Decide enforcement from that record.** If cross-device overlap is rare,
   stop here. If it is common, add L2 as the enforcing transport where a remote
   exists. Consider L3 only if L2's race is observed in practice, or if the
   operator wants a live view of every device for its own sake.

L4 is not recommended.

## Experiments that decide it

1. **How often does it happen?** Two weeks of L1 visibility on real work, and a
   count of claim intervals that overlapped between devices, taken from the
   retained start and finish events. This is the number the whole
   decision turns on, and nobody has it.
2. **Does the git host accept claim refs?** Push, list and delete a ref under a
   custom namespace on the hosting service in use; confirm no CI run starts and
   nothing appears in the branch list. Time the list call.
3. **How wide is the race?** Two devices running `work_start` on the same path
   in a loop against L2 with back-off; count double grants.

## Open questions for the maintainer

- How many devices run agents at the same time on the same repository, and is
  one of them always on?
- Should a cross-device overlap refuse `work_start`, or warn and let the agent
  decide?
- For public repositories, is publishing claimed paths acceptable?
