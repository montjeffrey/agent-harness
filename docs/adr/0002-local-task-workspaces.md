# ADR 0002: Local task workspaces and path claims

Date: 2026-08-24

## Context

Multiple agent chats can edit the same repository concurrently. Branches and
Git worktrees isolate checkout state but do not prevent two tasks from editing
the same file, so conflicts surface only at rebase or integration time.
Private plans must not modify a project's `.gitignore` or contaminate branch
diffs.

## Decision

- `agent-harness-work` keeps plans and task state under
  `~/.agent-harness/work/<repository-id>/`, never inside the repository.
- A task records a unique session id, exact repository-relative claimed paths,
  immutable base SHA, branch, worktree, and plan path.
- Starting a task refuses overlap with active path claims, including two
  sessions using the same agent program.
- Every task receives a branch. A clean, otherwise idle primary checkout is
  switched to that branch; an active peer session or dirty checkout creates an
  external worktree instead.
- `work diff` compares against the recorded base SHA. `work finish` releases
  only the claim; it never rebase, merges, or removes Git artifacts.

## Consequences

- Private plans do not appear in project status, branch diffs, commits, or
  release artifacts.
- Path claims remain cooperative: every implementation client must call
  `work_start` before editing.
- Integration stays serialized and explicit. This prevents automatic setup
  from hiding a merge conflict or moving another agent's active branch.
