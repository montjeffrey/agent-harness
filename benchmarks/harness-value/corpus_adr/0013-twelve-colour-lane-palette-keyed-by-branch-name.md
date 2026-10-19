# 0013. Twelve-colour lane palette keyed by branch name

## Status

Accepted

## Date

2025-05-14

## Context

The braille graph (0012) gives every branch its own lane, and lanes are
distinguished by colour. Two questions follow: how a branch gets its colour,
and how many colours there are.

**How.** The first implementation coloured lanes by lane index: lane 0 is blue,
lane 1 green, and so on. That is stable within one frame and wrong across
frames. When a new branch appears or an old one is merged, lanes shift, and
every branch to the right of the change silently changes colour. An operator
tracking "the orange one" loses it on every refresh. In one measured case a
single new commit changed the colour of 7 of 10 visible branches.

**How many.** Colours are 256-colour terminal codes, not the basic 16 ANSI
colours, because the basic set is remapped by every terminal theme and several
popular themes make two or more of them identical. The palette size is a
trade-off: with more colours, two neighbouring lanes are less likely to share
one; but every additional colour has to be distinguishable from all the others,
on dark and light backgrounds, for operators with common colour-vision
deficiencies.

Options considered for size:

- **8 colours.** The first cut. On the reference repository's default window
  (10 lanes), 3 of the 9 adjacent lane pairs ended up sharing a colour, which
  defeats the purpose: two neighbouring tracks in the same colour read as one
  branch. Rejected.
- **16 colours.** Fewer collisions, but there are not 16 colours in the
  256-colour cube that stay mutually distinguishable at the lightness we need.
  Candidates 13 to 16 always included a pair that was indistinguishable on the
  **Solarized** Dark background (which two regular testers use, and whose
  low-contrast base tones flatten neighbouring hues), and a further pair that
  merged under a deuteranopia simulation. Rejected.
- **12 colours.** The largest set we could find in which every pair stayed
  distinguishable on Solarized Dark, the default dark themes of Windows Terminal
  and Zed, and a light background, and under deuteranopia and protanopia
  simulation. On the reference window, 1 of 9 adjacent pairs collided.

## Decision

- A branch's colour is `stableHash(branch name) % Palette`, where `stableHash`
  is 32-bit FNV-1a and `Palette` is **12**. Colour depends only on the branch's
  name, so it survives lane shifts, refreshes and restarts.
- The base branch owns lane 0 and its first-parent spine.
- The palette is the twelve 256-colour codes
  `39, 78, 203, 141, 179, 43, 215, 111, 176, 79, 221, 167`, in that order,
  chosen and ordered so that consecutive indices are not similar hues.

## Consequences

- An operator can follow a branch by colour across refreshes. Tests assert
  that adding an unrelated branch does not change any existing branch's colour.
- Collisions between adjacent lanes are still possible (two names can hash to
  the same index), and at 10 lanes roughly one adjacent collision per screen
  is expected. We accept that; changing palette size later changes every
  branch's colour once, which is a one-off cost, but should not be done lightly.
- Light-background terminals get the same codes. Two of the twelve are weaker
  there but still distinguishable.
- The palette lives in the TUI adapter; the model only exposes the colour
  index, so another adapter can map indices to its own colours.
