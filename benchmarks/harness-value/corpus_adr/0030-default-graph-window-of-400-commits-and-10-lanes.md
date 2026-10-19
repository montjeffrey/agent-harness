# 0030. Default graph window of 400 commits and 10 lanes

## Status

Accepted

## Date

2025-11-26

## Context

This record documents a decision taken during v0.3 and adjusted in v0.4, written
up now because the numbers keep being questioned in review.

The graph tab (0012) draws commits on a braille canvas with one lane per branch
(0014). Drawing the entire history of a large repository is slow and, in a docked
panel, mostly off-screen: the reference repository has 1370 commits and 26
branches, and the full topology took over 15 s to build with probes off before
the v0.5 work. Most of the time the operator wants the recent shape of the
active branches, not the whole history.

So the default graph is a window, and the questions are how big, and what the
window must always contain so that it is not misleading.

Options considered for size:

- **200 commits.** Fast, but on the reference repository it cut off the fork
  points of four active branches, which then appeared to float with no parent.
- **400 commits.** Contained every active branch's fork point on all three
  reference repositories, and built quickly enough for a first screen.
- **1000 commits.** Contained more history than any docked panel can show, and
  roughly doubled graph build time for no visible benefit.

Options considered for lanes:

- **Every branch gets a lane.** At three cells per lane (0014), 26 lanes is 78
  cells of graph before any text, wider than most docks.
- **10 lanes.** 30 cells, leaving room for the commit text, and covering the
  branches most recently active. Other branches' commits are still drawn, on
  shared lanes.

## Decision

- `-limit` (default **400**) caps the commits drawn; `-branches` (default **10**)
  caps the branches given their own lane. `-1` for either means no cap.
- `-all` sets both to unlimited and draws the **entire topology**: every branch,
  every commit, including history reachable only through a merge.
- Each branch contributes at most 250 of its own commits to the window, so one
  very long branch cannot crowd out the rest.
- The window always contains a continuous first-parent trunk of the base, plus
  each displayed branch's merge-base with it, so every branch visibly joins the
  trunk.
- When a cap is in effect, the panel says so, so a capped window is never
  mistaken for the whole history.

The entire topology is built from a different construction than the window (a
full `--all` log) because the windowed construction cannot produce it at any cap:
`--first-parent` skips commits that arrived through a merge, and `base..branch`
is empty for a branch that is already merged.

## Consequences

- The first graph screen is fast and complete for the branches that matter now.
- `-all` is the escape hatch, and is wider than any dock, so `h`/`l` pan
  horizontally and `H` snaps back.
- The same defaults appear in the engine (for clients that pass zero) and in the
  command's flags; changing one without the other is a bug.
- Density (`-density`, `d` to cycle) changes rows per commit, not these caps.
- With more lanes than palette colours, colours repeat by hash; raising
  `-branches` does not change the palette.
