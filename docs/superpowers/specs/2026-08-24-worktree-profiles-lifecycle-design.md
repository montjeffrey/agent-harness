# Worktree profiles and lifecycle

Date: 2026-08-24  
Status: approved for implementation

## Problem

`agent-harness-work` already creates task-scoped branches, prevents active
path-claim overlap, and creates an external Git worktree when the primary
checkout is dirty or another task is active. Its state, plans, and external
worktrees live under `~/.agent-harness/work/<repository-id>/`, so none appear
in a repository diff. This is the correct default for a private work device.

The command intentionally stops at setup and handoff: it does not resume work
from another device, record verification, rebase, merge, or remove a
worktree. That leaves two gaps:

- A task begun privately has no gated path to become safely transferable when
  its branch needs work from another device.
- A completed, verified task has no guided, evidence-based sequence from
  worktree through rebase and cleanup. Operators have to reconstruct the
  branch's base, verification state, and retirement conditions by hand.

The project must remain offline-first and secure. Private state must never
become tracked merely to make a workflow convenient. In particular, a local
exclusion belongs in `.git/info/exclude`, never in a project's root
`.gitignore`.

## Scope

This design adds a profile-aware lifecycle around the existing local worktree
primitive.

It does:

- make `private` the built-in, machine-configurable default;
- provide an explicit `portable` profile for a committed, branch-local
  handoff record that another device can use after the branch is fetched;
- guide a task through start, resume, diff, verification, rebase, integration
  preparation, and local retirement;
- automate only safe local transitions and stop for publish, rebase, merge,
  and deletion confirmations.

It does not:

- make a network call, push, open a pull request, or depend on a hosted
  service;
- edit a root `.gitignore` for private state;
- automatically merge, force-push, delete a branch, or delete a worktree;
- make a private task shared without a clean, explicit promotion;
- treat a portable handoff as a place for secrets, machine paths, agent
  transcripts, session identifiers, or uncommitted changes.

## Approach

Three approaches were considered.

1. **Layered JSON settings and a branch-local handoff manifest. Chosen.**
   It is inspectable, works offline, follows ordinary Git workflows, and
   separates private state from the small amount of collaboration state a
   second device actually needs.
2. **Git-native metadata only.** Branch names, Git config, and notes avoid
   new files, but are difficult to discover and are inconsistently shared by
   ordinary clone/fetch workflows.
3. **A remote coordination backend.** This is useful for a later team-facing
   adapter, but introduces an account, authentication, and availability
   dependency that weakens the secure offline baseline.

The chosen approach is intentionally compatible with a later remote adapter:
the profile and lifecycle state machine are local capabilities; a provider
would only be another transport for already-sanitized handoff data.

## Settings and profiles

Settings merge in this order, where the later source wins:

```text
built-in defaults
→ ~/.agent-harness/settings.json
→ .agent-harness/work-settings.json
→ explicit command-line profile/option
```

`~/.agent-harness/settings.json` is machine-local and is the only settings
file created by default. It holds private defaults, such as the default
profile, local worktree root, and locally trusted verification profiles.
On the current work device, the default profile is expected to remain
`private`.

`.agent-harness/work-settings.json` is optional and may be committed only
when a repository wants portable-safe policy defaults. It contains no
machine-specific location, identity, credential, or command that would be
executed automatically after a clone. Private work never creates it.

Profiles are:

| Profile | State and permissions |
|---|---|
| `private` | Uses the existing local registry and external worktree locations. It creates no committed settings or handoff data. If a repository-local private artifact is unavoidable, the harness adds its exact path only to `.git/info/exclude`, preserving every user rule and leaving the root `.gitignore` unchanged. |
| `portable` | Permits a sanitized, tracked handoff manifest on the task branch. It still makes no network call; the operator chooses whether and when to push the branch. |

The harness must validate settings as JSON objects with a schema version,
reject unknown security-sensitive keys, and show the effective profile and
its source in `work status`.

## State model

The existing local `Task` record gains a profile, verification receipts, and
a local recovery ref. Its private fields remain only in:

```text
~/.agent-harness/work/<repository-id>/tasks.json
```

A portable task also has a tracked manifest at:

```text
.agent-harness/handoffs/<work-id>.json
```

The portable manifest is strict and contains only:

- schema version and task ID;
- concise objective and claimed repository-relative paths;
- branch name and immutable starting base SHA;
- lifecycle status;
- completed verification receipts (command label, exit code, tested commit,
  and timestamp);
- next recommended action.

It must reject unknown fields as well as prohibited values such as absolute
paths, a session ID, local plan path, secrets, transcript content, or dirty
worktree state. Its branch and base SHA must agree with Git before `resume`,
`rebase`, or `prepare-integrate` may rely on it.

Portable manifests are deliberately branch-local. They permit a collaborator
to fetch that branch and resume its work without introducing a central ledger
that several branches must merge concurrently. They are removed by an
explicit integration-preparation commit before the implementation branch is
merged, so the main checkout contains no active handoff file.

## Lifecycle and gates

The state machine is:

```text
start → implement → verify → handoff/resume (when portable) → rebase
      → prepare-integrate → merge by normal Git workflow → retire
```

| Command | Allowed behavior | Gate |
|---|---|---|
| `work start` | Claim paths; create a branch and an isolated worktree when necessary. | Refuse overlapping active claims. Defaults to `private`. |
| `work resume` | Reopen local state, or reconstruct a portable task from its checked-out branch manifest. | Verify task ID, branch, and base; never silently switch the primary checkout. |
| `work diff` | Show the full tracked diff against the immutable recorded base, including uncommitted changes. | Read-only. |
| `work verify` | Run an explicitly selected, locally trusted verification profile and record the result. | Commands are passed without shell interpolation; repository configuration is never auto-executed. |
| `work promote <id> --portable` | Turn a private task into a portable task. | Require a clean worktree with implementation changes committed; preview sanitized JSON and require explicit confirmation before the metadata-only commit. |
| `work handoff` | Update the portable manifest after a safe transition. | Available only to portable tasks; commits only the manifest after explicit confirmation. |
| `work rebase --onto <ref>` | Rebase a verified, clean task branch onto a named local ref, such as `origin/main`. | Make a local recovery ref first; do not fetch; pause normally for Git conflicts. |
| `work prepare-integrate` | Remove the portable manifest once verification and rebase gates pass. | Creates an explicit cleanup commit; private tasks do not use this command. |
| `work privatize <id>` | Stop future portable updates. | Remove the manifest in an explicit commit and retain only local registry state; it cannot erase a manifest already committed or pushed. |
| `work retire` | Remove an external worktree and archive local task state. | Require clean state, a successful verification receipt, and proof that the branch is merged before `--delete-branch`; branch removal is always explicit. |

`work status <id>` computes the current state and prints only valid next
actions. `work advance` may perform safe local state transitions, such as
creating a worktree, recording a verification result, creating a recovery
ref, or updating a manifest already approved for the current profile. It
must stop at every publish, history-rewrite, merge, and delete boundary.

Examples of its output are:

```text
private-active
→ commit or discard local changes before promotion

portable-verified
→ work rebase --onto origin/main

rebased-ready
→ prepare-integrate, then merge through your normal Git workflow

integrated
→ work retire --delete-branch
```

## Error handling and recovery

Every state-changing command must validate first and write state atomically.
Failures must leave the existing task record usable and report the exact
recovery command.

- A dirty worktree refuses promotion and rebase; it is never auto-stashed.
- An invalid or mismatched portable manifest refuses resumption rather than
  guessing a mapping.
- A failed rebase leaves Git in its normal conflict state and preserves a
  harness recovery ref. The operator resolves and runs Git's normal
  `rebase --continue`, or aborts; the harness only records success after the
  rebase completes.
- `retire` refuses to remove a worktree with uncommitted changes or a branch
  that has not been proven merged into the explicitly named target.
- `privatize` is prospective only. Once handoff data is committed or pushed,
  Git history cannot be made private by a configuration toggle.

## Verification strategy

Tests must cover:

- settings precedence, absent optional configuration, malformed JSON, and
  effective-profile reporting;
- private lifecycle behavior proving that no project settings, handoff, or
  root `.gitignore` edit is created;
- exact, idempotent `.git/info/exclude` updates that preserve user entries;
- portable handoff creation, strict field validation, and resume from a
  separate clone after the branch is fetched;
- promotion refusal for dirty worktrees and sanitization of the manifest;
- verification receipt recording and refusal of untrusted command sources;
- rebase success, rebase conflict/recovery ref behavior, and normal Git
  continuation/abort paths;
- integration preparation and retire refusal until verification, cleanliness,
  and merged-proof conditions pass;
- Windows path handling and linked-worktree common-Git-directory discovery.

No test relies on a network service. A two-clone fixture is sufficient to
exercise portable handoff and resume behavior.
