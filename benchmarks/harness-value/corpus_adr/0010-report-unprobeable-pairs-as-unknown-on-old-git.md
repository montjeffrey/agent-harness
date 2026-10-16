# 0010. Report unprobeable pairs as unknown on git older than 2.38

## Status

Accepted

## Date

2025-04-02

## Context

0009 settled on `git merge-tree --write-tree` for conflict probing. That form
arrived in git 2.38 (October 2022). A meaningful share of the machines landfirst
runs on have older git: Ubuntu 22.04 LTS ships 2.34, several corporate Windows
images pin an older Git for Windows, and some CI base images lag by a year or
more.

On those versions, `merge-tree --write-tree` does not exist. git exits with
status 129 (usage error) and prints its usage text. The first implementation
treated "no conflict lines in the output" as "no conflict", which meant that on
old git every pair was reported as merging cleanly. That is the worst possible
failure: a confident, wrong, reassuring answer, and the merge plan built on it
recommends an order that will conflict.

Options considered:

1. **Require git 2.38 and refuse to start on older versions.** Clear and honest,
   but it locks out exactly the users on LTS distributions and managed Windows
   images who cannot simply upgrade, for the sake of one feature. Everything
   else landfirst does (landing analysis, worktree state, the graph) works fine
   on 2.34. Rejected.
2. **Fall back to the pre-2.38 `merge-tree <base> <a> <b>` form and parse its
   output.** The old form exists on all versions we care about, but its output
   is a diff-like format that does not reliably mark every conflict type (it
   misses some rename and mode conflicts), and parsing it is fragile.
3. **Fall back to the old form where it gives an answer, and report the pair as
   unknown where it cannot.** Never report a pair as clean unless git actually
   said so.

## Decision

At startup, `git --version` is parsed and a conflict mode is recorded on the
model:

- **full**: git 2.38 or later. Probes use `merge-tree --write-tree` (0009).
- **legacy**: older git. Probes use the pre-2.38 three-argument form; any pair
  whose result cannot be interpreted with confidence is reported as **unknown**.
- **none**: the version string could not be parsed. Every pair is unknown.

Any probe that exits with a usage error (129) is unknown, regardless of mode.
"Clean" is only ever reported from a successful probe whose output positively
says there is no conflict. The conflict mode and git version are shown in
`-json` output and on the panel's status line, so a user on old git can see why
some pairs are unknown.

CI gains a job pinned to ubuntu-22.04 with its stock git 2.34, so the legacy
path is exercised on every change and cannot silently rot.

## Consequences

- Users on old git get everything except complete conflict information, and are
  told that is what they are getting.
- An unknown probe contributes nothing to a pair's conflict information; it is
  never folded into "clean". Code that consumes probe results must keep
  unknown distinct from an empty conflict list.
- The version check is a string parse of `git version 2.x.y...`; vendor
  suffixes (`.windows.1`, `(Apple Git-...)`) are ignored. A git that reports
  something unparseable gets mode none rather than a guess.
- If we ever raise the minimum git version for other reasons, the legacy path
  can be removed, but only together with the pinned CI job.
