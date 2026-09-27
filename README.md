# Blockburst

A block-dropping puzzle game in one self-contained HTML file. No build step, no
dependencies — open `index.html` in a browser and play.

## How it works

- **8×8 board.** You're dealt three pieces at a time in the tray below it.
- **Drag a piece onto the board.** Pieces can't be rotated. A ghost preview shows
  where it lands, and any row or column you're about to complete flashes white.
- **Full rows and columns clear**, both at once if you set it up that way.
- **The tray refills only when all three pieces are gone**, so the third piece is
  the one that traps you.
- **Game over** when none of your remaining pieces fit anywhere on the board.

## Scoring

| Event | Points |
| --- | --- |
| Placing a piece | 1 per cell |
| Clearing lines | `cells cleared × 10 × lines cleared × combo` |

Clearing 2+ lines with a single placement multiplies the whole clear. Clearing on
consecutive placements builds a combo streak worth an extra ×0.5 per step; one
placement without a clear resets it.

## Implementation notes

- 37 fixed piece orientations, weighted so small and common shapes come up more
  often than the 3×3 square or the 5-long bar.
- Every fresh tray is validated against the current board — at least one of the
  three pieces is always placeable, so you never lose to a dead deal.
- Pointer events throughout, so mouse and touch share one code path. On touch the
  piece is lifted above the finger so you can see the drop target.
- Best score persists in `localStorage`, wrapped in try/catch so blocked storage
  degrades to a session-only score rather than breaking the game.

## Also in this repo

[`clipper/`](clipper/README.md) — an OpusClip-style tool that cuts the best 30-second
vertical, captioned clip from any video.
