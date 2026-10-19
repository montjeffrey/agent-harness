# 0004. Use Bubble Tea for the terminal UI

## Status

Accepted

## Date

2025-01-22

## Context

The Go rewrite (0002) needs a terminal UI. The Node version's UI was a list
printed with ANSI escapes and redrawn on a timer; it was never interactive
beyond scrolling. The Go version is meant to be a panel the operator keeps open
in a docked terminal while agents work: keyboard-driven, with a selection,
tabs, a confirmation bar for actions, and eventually a custom-drawn commit
graph that no widget library ships.

Requirements, in order of weight:

1. **The view must be testable without a terminal.** Most of the UI's bugs so
   far were "the wrong thing is shown for this state", and those tests need to
   run in CI on three operating systems.
2. **Custom drawing must be first-class.** The graph will be rendered by our
   own code into strings of styled cells; the framework must not fight that.
3. **Builds with `CGO_ENABLED=0` on all six targets** (0002).
4. Works inside the terminals the operators actually use, including Windows
   Terminal and the terminal panel of their editor.

Options considered:

- **gocui** (`jroimartin/gocui`). We prototyped the panel on it first, because
  lazygit is built on it and the owner uses lazygit daily, so its look and key
  conventions were familiar. The prototype got as far as the branch list and a
  detail pane before we stopped. Three problems: gocui's views are text buffers
  with their own cursor, origin and wrapping, so drawing a graph meant fighting
  its edit model rather than just producing lines; the actively maintained
  version is a fork vendored inside lazygit that has diverged from upstream, so
  we would be choosing between an unmaintained upstream and a fork that is not
  published as a library; and its global keybinding table made "what happens
  when this key is pressed in this state" hard to test without driving a real
  terminal. **Rejected.**
- **tview.** Considered on paper only. It is widget-oriented (tables, forms,
  flexbox layouts) and its widgets own their own drawing, which is the opposite
  of requirement 2.
- **tcell directly.** Maximum control, but we would be writing our own event
  loop, focus handling and layout, and testing would still need a simulated
  screen.
- **Bubble Tea** (`charmbracelet/bubbletea`) with **Lip Gloss** for styling.
  An Elm-style architecture: `Update(msg) (Model, Cmd)` is a pure function of
  the previous model and a message, and `View()` returns a string. Both can be
  unit-tested by constructing a model, sending it messages and asserting on the
  string. Custom drawing is just "return the lines you want".

## Decision

Use Bubble Tea for the event loop and Lip Gloss for styling text in the panel.
The UI lives in `internal/tui`. Its model is a value that tests construct
directly; tests call `Update` and `View` and assert on output. The `-plain`
report shares the same package so its wording cannot drift from the panel's.

## Consequences

- View tests are plain Go tests with no terminal; this is the main reason for
  the choice and should be preserved. A change that makes a view untestable
  without a terminal should be treated as a regression.
- Lip Gloss detects terminal colour capability from the environment. That is
  fine for text styling but means anything rendered under test without a tty
  may come out uncoloured; code that must produce identical bytes in test and
  in use should write escapes directly rather than go through Lip Gloss.
- Bubble Tea redraws the whole view on each update. For a panel of this size
  that has not been a performance problem; if it becomes one, the fix is to make
  `View()` cheaper, not to switch frameworks.
- The gocui prototype branch is deleted; nothing from it is carried forward.
