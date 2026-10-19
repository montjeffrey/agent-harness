# 0014. Place lanes three cells apart

## Status

Accepted

## Date

2025-05-21

## Context

On the braille canvas (0012), resolution is per dot but colour is per cell: a
terminal cell has exactly one foreground colour, and every dot drawn in that
cell takes it. If two lanes in different palette colours (0013) ever draw into the same
cell, one of them loses its colour in that cell. At best this looks like a
glitch; at worst it makes one branch's track appear, for a few rows, to belong
to another branch, which is the one thing a graph in a tool about where branches
land must not do.

The first canvas layout spaced lanes two dots apart, because it looked compact.
With radius-2 nodes, adjacent lanes overlapped in almost every row that had a
node, and colours visibly swapped between neighbouring branches.

Options considered:

1. **Pack lanes tightly and resolve shared cells by priority** (for example, the
   lane with a node in that row wins). Rejected: it hides the problem rather
   than avoiding it, and the "loser" track still visibly changes colour.
2. **One cell per lane** (2 dots apart). Rejected: a radius-2 node is 5 dots
   wide and bleeds into both neighbours.
3. **Two cells per lane** (4 dots apart). Each lane's centre column is in a
   different cell from its neighbour's, but node edges still touch the
   neighbouring cell. Tested; collisions dropped but did not disappear.
4. **Three cells per lane** (6 dots apart). The minimum spacing in which a
   radius-2 node, plus the curve leaving it, stays entirely within cells that
   no other lane uses.

## Decision

Lanes are placed three cells (six braille dots) apart, as the constant
`laneCells = 3` in the graph renderer. Lane assignment comes from branch
identity, not from row position, and the base owns lane 0.

Curves that move a branch between lanes necessarily cross other lanes' cells
for a row or two; that is accepted, because a crossing is brief and reads as a
crossing, whereas two parallel tracks sharing cells for many rows read as one
branch.

## Consequences

- Each lane owns its own column of cells, so branch colours are truthful along
  every straight run of track.
- The graph is wider. At ten lanes the drawing is 30 cells wide before any text,
  which is a meaningful share of a docked panel. That is why the default window
  limits the number of branches given their own lane and why `-all` enables
  horizontal panning (`h`/`l`, `H` to snap back) rather than squeezing lanes.
- Density modes change vertical spacing (rows per commit), never horizontal lane
  spacing. A future "compact" option that reduces lane spacing would reintroduce
  the colour problem and should be rejected unless the colour model changes.
- Text to the right of the graph lines up by row, using the commit-to-row map
  the canvas returns, not by assuming one commit per line.
