# 0029. Flag risky worktree locations: volatile, cloud, nested

## Status

Accepted

## Date

2025-11-12

## Context

A recurring way agent work gets lost has nothing to do with branches or remotes:
the worktree it lives in disappears, or gets corrupted, because of where it is.
We collected incidents from early users over the v0.5 and v0.6 cycles. They fell
into three groups.

- **Temp directories.** Agent tools and scripts often create worktrees under the
  OS temp directory (`/tmp`, `%TEMP%`). Those are cleared on reboot on some
  systems and by disk-cleanup tools on others. Two users lost uncommitted agent
  work this way, and one lost committed-but-unpushed work when the worktree's
  branch was later pruned.
- **Cloud-synced folders.** Repositories and worktrees inside OneDrive, Dropbox,
  Google Drive or iCloud folders. The sync client races git: it uploads
  half-written lock files, restores files git just deleted, and occasionally
  creates conflict copies of refs. The owner's own repository lives in OneDrive,
  which is how this was first noticed (a 600 kB report file rewritten inside a
  synced `.git` caused a sync storm).
- **Nested worktrees.** A linked worktree inside the main worktree. Not dangerous
  by itself, but tools that walk the main tree see it as part of the project, and
  a careless clean (`git clean -fdx` in the main worktree) can delete it.

None of these show up in branch state. A worktree can be perfectly up to date
with its remote and still be one reboot away from losing its uncommitted changes.

Options considered:

1. **Refuse to analyse risky worktrees.** Rejected: the operator still needs to
   see what is in them, especially if they are at risk.
2. **Move them.** Rejected: moving a worktree an agent is using breaks the agent,
   and landfirst does not rewrite or relocate other people's work.
3. **Report the risk alongside the worktree**, so the operator can act.

## Decision

Each worktree carries a list of location risks, computed from its path with no
git calls:

- `volatile`: the normalised, lower-cased path contains a temp directory
  segment (`/tmp/`, `/temp/`, `/appdata/local/temp/`).
- `cloud`: the lower-cased path contains `onedrive`, `dropbox`, `google drive`
  or `icloud`.
- `nested`: a linked worktree whose path is inside the main worktree.

Risks are shown next to the worktree in the topology view and included in
`-json`. They are independent of the branch verdict; a branch can be "unpushed"
and its worktree "volatile", and both are shown.

The same check is exported and runs before landfirst creates a worktree (0028),
so there is one definition of a safe path rather than two that drift. At
creation time `cloud` and `nested` are refused; `volatile` is informational.

## Consequences

- The operator sees location risk directly next to the work it threatens.
- Detection of cloud folders is by path substring, which is crude. It catches the
  default folder names of the four common clients. A sync client configured to a
  custom folder name is not detected; we accept that rather than probing for
  running sync processes.
- `volatile` is not refused at creation time: test fixtures live under temp
  directories, and an operator deliberately creating a scratch worktree there is
  making an informed choice. The flag remains visible afterwards.
- Additional risk kinds (network drives, removable media) can be added without
  changing the shape: a risk is a short code plus a human-readable note.
