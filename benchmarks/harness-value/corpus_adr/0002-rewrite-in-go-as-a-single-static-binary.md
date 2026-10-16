# 0002. Rewrite landfirst in Go as a single static binary

## Status

Accepted

## Date

2025-01-08

## Context

v0.1 is a Node implementation (preserved at tag `v0.1.0-node`). It works, and
its performance layer is good: four SHA-keyed caches (`commitSetCache`,
`probeCache`, `measureCache`, `ancestryCache`) and a watcher that separates a
fast pass from a probe pass. But it has to be installed, and installation is
where it keeps failing.

The people who run landfirst are acting as project managers over parallel
agents. Many of them are on Windows, several have no Node toolchain on the
machine where the agents run, and the ones that do have three different Node
versions managed by three different version managers. `npx landfirst` took 1.8 s
to start on a warm cache before doing any git work at all. We also want agents
themselves to be able to call the tool (`-json`), which means it must be present
on CI images and in sandboxes we do not control.

Options considered:

1. **Stay on Node, ship with `pkg` or Node's single-executable support.** The
   resulting binaries were 70 to 90 MB and the single-executable path was still
   experimental on Windows arm64.
2. **Deno `compile`.** Similar size problem (around 80 MB) and a second runtime
   for contributors to learn.
3. **Rust.** Produces small static binaries, but nobody on the project writes
   Rust day to day, and compile times on the Windows laptops we test on were a
   real cost for an iterate-in-the-terminal tool.
4. **Go.** Small static binaries, trivial cross-compilation, a mature TUI
   ecosystem, and the language most contributors already use.

## Decision

Rewrite landfirst in Go and ship it as one static binary per platform.

- Go 1.23, module `github.com/montjeffrey/landfirst`.
- `CGO_ENABLED=0` for every release build. This is a hard constraint, not a
  default: any dependency that needs cgo on any target platform is rejected for
  that reason alone, because it would break cross-compilation from a single
  Linux runner.
- Release builds for linux, darwin and windows on amd64 and arm64 (six
  binaries), produced by a tag-triggered workflow, with a `SHA256SUMS` file.
- The only runtime requirement is `git` on the `PATH`.
- v0.1 remains available at its tag for anyone who needs it; it is not
  maintained.

## Consequences

- Installation becomes "download one file". `go install` works for people who
  have Go.
- The cgo ban will constrain later library choices (a git library, a file
  watcher, a terminal library), and each such choice should state whether the
  candidate builds with `CGO_ENABLED=0` on all six targets.
- The rewrite is a port of the *analysis*. The Node performance layer is not
  part of the first Go release and will need to be brought across deliberately
  later; the risk is that its absence is not noticed until someone runs the tool
  on a large repository.
- The Node version's concurrency control, its caches and its watcher are all
  candidates to port, but each should be re-decided rather than copied, since
  the cost model of a Go process spawning git differs from Node's.
- Binary size is around 6 MB stripped (`-s -w -trimpath`), which is acceptable.
