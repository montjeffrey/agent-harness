# 0012. Draw the commit graph on a braille sub-cell canvas

## Status

Accepted

## Date

2025-04-30

## Context

v0.3 adds a commit graph tab. The obvious way to draw one in a terminal is the
way `git log --graph` and most TUIs do it: one character cell per lane position,
with box-drawing glyphs (`│ ├ ╰ ─`) for edges. We built that first.

It stops being legible somewhere around three branches. A terminal cell is the
smallest unit most TUIs can draw, so every lane change becomes a corner glyph
that the reader has to decode, diagonals become staircases, and two edges
crossing in one cell become an ambiguous `┼`. On the reference repository the
box-drawn graph at ten lanes was something people scrolled past rather than
read.

Options considered:

1. **Box-drawing characters.** Built, measured against real data, rejected for
   the reasons above.
2. **Half-block characters** (`▀ ▄ █`). Doubles vertical resolution, so a cell
   holds two pixels stacked. That helps vertical runs but does nothing for
   horizontal resolution, which is where lane changes happen, and colouring
   relies on foreground/background tricks that collide with lane colours.
3. **Terminal image protocols** (Sixel, the Kitty graphics protocol). True pixels.
   Rejected because the terminal the owner docks landfirst in (Zed's built-in
   terminal, an Alacritty fork) supports neither, and the upstream request is
   open with no timeline. A graph that only works outside the dock is not the
   graph we need.
4. **Braille patterns** (U+2800 to U+28FF). Each cell addresses a 2x4 grid of
   dots, independently. That is eight times the resolution of a cell, works in
   every terminal with a Unicode font, and is plain text.

## Decision

The graph is drawn on a sub-cell canvas (`internal/canvas`) backed by braille
dots. Commits are nodes, edges are drawn as quadratic Bezier curves sampled at
dot resolution, and the canvas renders to lines of braille characters with
colour escapes. Filled nodes are ordinary commits and hollow nodes are merges,
so the distinction survives without colour.

Braille dots in common terminal fonts are close to square, so the canvas uses
equal x and y weighting. An earlier version weighted y as though dots were
half-height, which stretched every node into an ellipse.

Colour is written as escape sequences directly by the canvas renderer, not
through Lip Gloss, so the same canvas produces identical bytes under test and in
use (see 0004's note on Lip Gloss detecting capability from the environment).

## Consequences

- Edges read as curves that visibly depart and rejoin a parent, and the graph
  stays legible at ten lanes and beyond.
- A cell still holds only one foreground colour. Two lanes that share a cell
  will lose one of their colours, so lane spacing becomes a correctness
  question rather than an aesthetic one; that is recorded separately.
- `-density compact|comfortable|spacious` trades curve smoothness against how
  much history fits on screen, and `d` cycles it live. A docked panel is short,
  so this is a real trade-off rather than a preference.
- The canvas returns plain strings, which fits Bubble Tea's `View()` model
  (0004) without any custom widget. Colour indices are mapped to terminal
  colours by a lane palette, chosen separately.
- Fonts without braille glyphs render boxes. We document the requirement rather
  than ship a fallback renderer.
- If image protocols become available in the docked terminal, the canvas
  abstraction is the place to add a second backend.
