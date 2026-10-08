# ADR 0004: Project memory is machine-local and never enters git

Date: 2026-09-11

## Context

ADR 0001 and the memory design spec assumed that project-scope memory
(`<project>/.agent-harness/`) would be committed with the repository so it
travels with the code. In practice the directory is a stream of agent-written
proposals, session-local working context, and an operator's private review
state. Committing it puts that churn into every collaborator's `git status`,
diffs, pull requests, and history, and turns a private working aid into
something reviewed alongside code. The owner's decision: memory stays on disk
beside the project, but must never appear in anyone's git work -- not even as
a `.gitignore` change, which would itself be a diff that others see.

Before this decision, nothing in the code established any exclusion. Three
enrolled projects on this machine ignored the directory by hand, two through
`.gitignore` and one through `.git/info/exclude`, and this repository tracked
its own memory. ADR 0002 already holds that the harness never modifies a
project's `.gitignore` for work state.

## Decision

- `<project>/.agent-harness/` is project-owned and machine-local. It lives
  beside the project it describes and is never tracked by git.
- `memory_store.init_project` -- the single path through which
  `agent-harness-enroll`, `agent-harness-memory init`, and the SessionStart
  hook's auto-enrolment create project storage -- lists `.agent-harness/` in
  the repository's private `.git/info/exclude`. The write is idempotent, is
  skipped silently outside a git repository, follows a `.git` *file* (linked
  worktree, submodule) to the common git directory the way
  `agent-harness-work` does, and never fails enrolment: an unwritable exclude
  file is reported as a note.
- The harness never touches `.gitignore` for memory. `.git/info/exclude` is
  chosen precisely because it is not versioned: the exclusion protects
  every collaborator's view without becoming a diff in anyone's branch.
- A `.agent-harness/` that git already tracks is reported, not fixed. Enroll
  prints `git rm -r --cached .agent-harness` and stops; untracking is a
  visible history change the operator makes deliberately.
- `~/.agent-harness/` is the one machine-wide store: the project registry,
  machine-scope memory, indexes, and task workspaces. Cross-project
  references are resolved through the registry, never through git.
- Machine scope remains the only cross-machine story, and it is still not
  synchronized. A second machine starts blank; that is unchanged.

## Consequences

- Memory never appears in `git status`, diffs, commits, or pull requests, and
  a project's memory on one machine is invisible to the same project cloned
  elsewhere. Moving memory between machines is an explicit operator action.
- Re-running `agent-harness-enroll` repairs a project enrolled before this
  rule existed by adding the missing exclusion; the two hand-written
  `.gitignore` lines on this machine are now redundant and may be removed at
  the owner's convenience.
- ADR 0001's "memory is the durable source of truth" stands; its unstated
  assumption that the source of truth is versioned with the code does not.
  The design spec's "committed with the repository by default" paragraph is
  superseded by this record.
- The SessionStart hook's auto-enrolment gains one short git subprocess
  (`rev-parse --git-common-dir`) on first contact with a repository. It runs
  with `stdin` detached so it cannot block on the hook's own pipe.
