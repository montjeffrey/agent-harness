# 0001. Record architecture decisions

## Status

Accepted

## Date

2025-01-06

## Context

landfirst started life as a weekend Node script that answered one question for
one person: which of the branches my agents finished have actually reached the
remote, and in what order should they merge? It has since grown into something
other people run against repositories with dozens of parallel branches and a
dozen linked worktrees, and it is about to be rewritten in Go.

The reasons behind the existing design live in three places, none of which is
durable. Some are in commit messages, which are written at the moment of the
change and rarely explain what was considered and rejected. Some are in chat
logs with the owner, which nobody will search. Some are only in the heads of
whoever built the feature, and the people building features here are often
agents whose context ends with the session.

That last point matters more than usual for this project. A large part of the
work is done by coding agents in separate worktrees, each starting cold. An
agent that does not know *why* a threshold has the value it has will happily
"fix" it, and a reviewer who does not know either will accept the fix. We have
already seen one round of that: the Node version's caches were removed in a
cleanup because nobody remembered what they were for.

Planning briefs (the documents under `docs/` that describe a phase of work) are
not a substitute. A brief describes what to build next and is stale the moment
the phase ships. A decision record describes a choice that stays in force until
it is explicitly replaced.

## Decision

We record architecturally significant decisions as Architecture Decision
Records, in the format described by Michael Nygard, under `docs/adr/`.

- Files are named `NNNN-kebab-case-title.md`, numbered sequentially, never
  renumbered and never reused.
- Each record has a Status, a Date, and Context / Decision / Consequences
  sections. Optional sections (Options considered, Notes) are fine when they
  carry the reasoning.
- A decision is "architecturally significant" if reversing it later would touch
  more than one package, change user-visible output or flags, or if the obvious
  alternative was tried and rejected for a non-obvious reason. Numbers that look
  arbitrary but were measured (thresholds, caps, intervals) qualify by default,
  because they are exactly what gets "tidied" away.
- Once accepted, a record is not edited except to change its Status. When a
  decision is reversed or refined, a new record is written whose Status says
  `Supersedes NNNN`, and the old record's Status becomes `Superseded by NNNN`.
  The old record's text stays as it was, so the history of the reasoning is
  preserved.
- Rejected alternatives are named explicitly, with the reason they lost. "We
  considered other options" is not a record.

Briefs and plans continue to live directly under `docs/`. They may cite ADRs;
ADRs should not depend on briefs, which are allowed to go stale.

## Consequences

- Anyone, human or agent, can find out why something is the way it is by
  searching one directory, and can tell from the Status line whether a record is
  still in force. Reading only the first record found on a topic is a known
  trap: always check whether it has been superseded.
- Writing a record costs perhaps twenty minutes per decision. We accept that for
  significant decisions and do not write records for routine ones.
- Records written after the fact (for decisions taken during the v0.1 Node era
  and carried forward) are dated when written and say so in their Context.
- Code comments may refer to "the plan's number" or "see the ADR"; the record,
  not the comment, is the place where the rejected alternatives are listed.
