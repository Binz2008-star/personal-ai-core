# personal-ai-core brand assets

Two marks and one wordmark. Nothing else is part of the brand.

| File | Use |
|---|---|
| `logo-mark.svg` | **Primary mark.** White fox on a navy circle with a blue tail swoosh. Web UI, avatars, social. |
| `logo-mark-mono.svg` | **Secondary mark.** Outlined fox with circuit nodes, one colour (`currentColor`). Docs, diagrams, print. |
| `logo-horizontal.svg` | Primary mark + `personal-ai-core` wordmark, for light backgrounds. |
| `logo-horizontal-dark.svg` | Same lockup for dark backgrounds (white text, cyan `-ai-`, blue ring on the mark). |
| `favicon.svg` | Simplified primary mark for 16–64 px: no inner-ear or eye detail, solid blue swoosh. |
| `favicon-32.png` | 32 × 32 raster of `favicon.svg`, transparent corners. |
| `app-icon-512.png` | 512 × 512 raster of `logo-mark.svg`, transparent corners. |

## Palette

| Name | Hex | Role |
|---|---|---|
| Navy | `#1B2A5B` | Mark circle, wordmark on light |
| Blue | `#2F6BEA` | Tail swoosh, `-ai-` on light |
| Cyan | `#2EC4D6` | Swoosh gradient end, `-ai-` on dark |
| White | `#FFFFFF` | Fox, wordmark on dark |

## Wordmark

Lowercase, always: `personal-ai-core`. The letters are converted to outlines (Liberation Sans Regular,
SIL OFL), so the SVGs render identically without the font installed. The hyphens and `ai` take the
accent colour.

## Usage

- **Clear space:** at least one quarter of the mark's diameter on every side.
- **Minimum size:** mark 16 px (use `favicon.svg` below 48 px); horizontal lockup 24 px tall.
- **Light backgrounds:** `logo-horizontal.svg`, `logo-mark.svg`.
- **Dark backgrounds:** `logo-horizontal-dark.svg`. The bare `logo-mark.svg` navy circle loses its edge
  on very dark backgrounds, so prefer the dark lockup, which adds the blue ring.
- **Mono mark:** `currentColor` only follows the surrounding text colour when the SVG is **inlined** in
  HTML. Loaded through `<img>` it renders black. Inline it, or set the colour with CSS on the `<svg>`.
- Do not recolour, stretch, rotate, add effects, or change the wordmark case.

## Provenance

Redrawn by hand as vector paths from a concept sheet (selected: row 1 / col 4 for the primary mark,
row 2 / col 2 for the secondary). The raster sheet is not stored in the repository and nothing here is
traced from it. The PNGs are exports of the SVGs above; regenerate them from the SVGs rather than
editing them.
