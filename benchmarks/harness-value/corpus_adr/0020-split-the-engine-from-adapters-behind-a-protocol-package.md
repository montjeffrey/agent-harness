# 0020. Split the engine from adapters behind a protocol package

## Status

Accepted

## Date

2025-08-06

## Context

By v0.5 the TUI imported `internal/model` directly and reached into its types
everywhere: verdict enums, worktree risk lists, the graph's commit slice. So did
the `-plain` report and the `-json` output, each in slightly different ways. Three
problems followed:

1. Any change to the model's internals rippled into the TUI, and several
   "refactors" of the model were blocked on TUI churn.
2. There was no single definition of what a client is allowed to see, so the
   JSON output was simply whatever the model's struct tags happened to produce,
   including internal fields.
3. The next phase needs live updates and actions that a client requests and the
   engine performs. A client needs a way to say "act on the state I was looking
   at", which requires that state to have an identity. The model has none.

We also expect a third adapter: a `serve` mode so that agents and editors can
talk to a running engine. It should not have to import model internals.

Options considered:

1. **Keep adapters on the model; add a JSON view type for `-json` only.**
   Rejected: fixes problem 2 and nothing else.
2. **A protocol defined in protobuf**, with generated Go types, served over gRPC
   later. Rejected for now: a code generation step, a second toolchain, and a
   binary size cost, for a single in-process client today. JSON struct tags on
   plain Go types give us the same shapes with none of that, and a later
   network protocol can serialise those same types.
3. **A `protocol` package of plain data types** that is the only thing adapters
   import, and an `engine` package that owns the model and exposes operations in
   terms of protocol types.

## Decision

- `internal/protocol` holds the wire format: `Snapshot`, `Unit`, `Worktree`,
  `ForkPoint`, `Contention`, `PlanItem`, `Graph`, decision trees and action
  requests. It performs no I/O and imports nothing from the repository.
- `internal/engine` owns building the model and converting it to protocol
  shapes. Its operations are `Snapshot`, `Graph`, `Decide`, `Execute`,
  `Worktrees` and `Watch`.
- `internal/tui` (the Bubble Tea panel and `-plain`) and the `-json` output import
  `protocol` and the engine's interface only. An import-boundary test (using
  `go list -json`) fails if `internal/tui` directly imports `internal/model` or
  `internal/git`.
- Units and worktrees get stable string IDs (for example `branch:<name>`) so a
  client can refer to them across snapshots.

## Consequences

- The model can be restructured freely as long as the conversion to protocol
  shapes is kept up to date; adapters do not change.
- The protocol package is the contract for `-json`. Fields added there are
  public; fields left in the model are not.
- There is some duplication: a few small helpers exist in both the engine and
  the model rather than opening a seam into the model for a single value. That
  is preferred to leaking model internals.
- Every snapshot needs an identity so a client can say which state it is acting
  on. How that identity is computed is a separate decision.
- A future `serve` adapter can be written against the same interface without
  touching the engine.
