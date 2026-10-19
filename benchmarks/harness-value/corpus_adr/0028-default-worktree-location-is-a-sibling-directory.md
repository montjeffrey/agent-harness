# 0028. Default worktree location is a sibling `<repo>.worktrees` directory

## Status

Accepted

## Date

2025-10-29

## Context

`w` in the topology view gives the selected branch its own worktree, confirmed
first like any other action (0008). The operator should not have to type a path
for the common case, so the engine needs a default location. Where worktrees
live turned out to matter more than expected, because several common locations
cause problems that only show up later.

Options considered:

1. **Inside the repository**, for example `<repo>/.worktrees/<branch>`. Easy to
   find, and the convention some agent tools use. Rejected: a worktree nested
   inside the main worktree is visible to every tool that walks the main tree.
   `go test ./...` and `go vet ./...` descend into it and test a second copy of
   the code; ripgrep and editors index it twice; a careless `git add -A` in the
   main worktree can try to add it (it shows as an untracked directory unless
   ignored). landfirst already flags nested worktrees as a risk (0029); the
   default should not create one.
2. **A tool-specific directory inside the repository** such as
   `.claude/worktrees/<name>`. Same nesting problems as option 1, plus it ties a
   generic git tool to one agent product's convention. Rejected.
3. **The OS temp directory.** Never nested, and cleanup is free. Rejected: the OS
   or a cleanup utility can delete it, taking uncommitted work with it, which is
   exactly the kind of loss landfirst exists to prevent. landfirst flags temp
   locations as volatile.
4. **A user-level directory** such as `~/worktrees/<repo>/<branch>`. Not nested
   and not volatile, but it separates worktrees from their repository; on
   machines where repositories live on different drives it can put a worktree
   on a different volume from its repository, and moving a repository leaves its
   worktrees behind.
5. **A sibling of the repository**: `<parent>/<repo>.worktrees/<branch>`. Not
   nested, not volatile, on the same volume, next to the repository in a
   directory listing, and named so its owner is obvious.

## Decision

When a worktree-add request does not specify a path, the engine uses
`filepath.Join(filepath.Dir(<repo root>), <repo name> + ".worktrees", <branch>)`.
Branch names containing `/` produce nested directories under that sibling
(`feat/x` becomes `<repo>.worktrees/feat/x`), matching how git stores the refs.

Before creating it, the engine evaluates the same location risks it reports for
existing worktrees. A path that would be nested or inside a cloud-synced folder
is refused with an unsafe-path error that lists the reasons, rather than created
and then flagged. A path that merely looks like a flag (a leading `-`) is also
refused, because git's option parser would consume it as one.

## Consequences

- Worktrees created through landfirst with the default path are never nested,
  and are easy to find next to their repository.
- If the repository itself lives in a cloud-synced folder, the default sibling
  does too, and creation is refused. We do not silently pick a location outside
  the user's chosen area; the operator supplies a path.
- A temp-directory path is reported as volatile on existing worktrees but is not
  a reason to refuse creation, partly because every test fixture lives under a
  temp directory.
- `W` removes a worktree created this way or any other. It refuses on a dirty
  worktree or one with an operation in progress and prints the `--force` command
  instead.
- The path is documented on the protocol request type, so clients can predict
  it without calling the engine.
