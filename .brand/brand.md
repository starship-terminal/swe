# swe brand kit

Mark 4-4. Kit folder: `.brand/`. Marks are letter-free; only the lockups and banners carry the name.

## Palette

| Role | HEX | RGB |
|---|---|---|
| Background | #1C1B1F | 28 27 31 |
| Cream | #F2E0CC | 242 224 204 |
| Orange | #D44E14 | 212 78 20 |
| Mono cream / charcoal / black / white | #F2E0CC / #1C1B1F / #000000 / #FFFFFF | 242 224 204 / 28 27 31 / 0 0 0 / 255 255 255 |

The vector master has 3 colour layer(s). Within the 2 to 3 colour guideline (the charcoal layer is the outline/hole of the artwork and matches the background).

swe has two mark versions: the full app-window mark (title-bar dots, tilted window, flame) and a simplified small-size version in which the three title-bar dots merge into one pill and the centre square is enlarged. The favicons (`png/favicon-16.png`, `favicon-32.png`, `favicon-48.png`, `favicon.ico`) use the simplified version. Use it below about 48 px: `png/simplified-16/32/48/64.png`, `svg/mark-simplified-color-on-bg.svg`, `svg/mark-simplified-color.svg`.

## Typeface

Space Mono (Google Fonts, SIL OFL 1.1): Bold for the name in lockups and banners, Regular for text and captions. A quirky, retro-futurist monospace: it stays code-native next to the terminal window mark while its geometric curves give the warp-engine a space-age edge. Names are outlined in every SVG/PDF; install the TTFs in `fonts/` for new text. Licence: `fonts/OFL.txt`.

## Rules

- Clear space: x = 0.25 x mark height (tight bounding box) on all sides.
- Minimum size: 32 px tall (digital) / 10 mm (print). Below that use `png/favicon-16.png`.
- Do: use the supplied files; keep clear space; cream marks go on dark or mid backgrounds, use the light version (or mono charcoal) on light.
- Don't: stretch or skew, recolour, add shadows/glow/gradients, or place on busy backgrounds.

## Files

### `png/`
Raster set (avatars, logos, mark-transparent, favicons + .ico, apple-touch/PWA icons, maskable, social preview mark-only)

`apple-touch-icon-180.png`, `avatar-1024.png`, `avatar-500.png`, `favicon-16.png`, `favicon-32.png`, `favicon-48.png`, `favicon.ico`, `icon-192.png`, `icon-512.png`, `logo-rounded-160.png`, `logo-rounded.png`, `logo-square-2048.png`, `logo-square.png`, `mark-transparent-2048.png`, `mark-transparent.png`, `maskable-512.png`, `simplified-16.png`, `simplified-32.png`, `simplified-48.png`, `simplified-64.png`, `social-preview-1280x640.png`

### `svg/`
Vector masters: full colour on background, full colour transparent, one-colour versions. Palette-exact, one filled path per colour layer

`mark-color-on-bg.svg`, `mark-color.svg`, `mark-mono-black.svg`, `mark-mono-charcoal.svg`, `mark-mono-cream.svg`, `mark-mono-white.svg`, `mark-simplified-color-on-bg.svg`, `mark-simplified-color.svg`

### `pdf/`
Vector PDFs generated from the SVGs

`mark-color-on-bg.pdf`, `mark-color.pdf`, `mark-mono-black.pdf`, `mark-mono-charcoal.pdf`, `mark-mono-cream.pdf`, `mark-mono-white.pdf`

### `mono/`
One-colour marks (cream #F2E0CC, charcoal #1C1B1F, black, white) as SVG and transparent PNG, with knockouts so the silhouette reads

`mono-black.png`, `mono-black.svg`, `mono-charcoal.png`, `mono-charcoal.svg`, `mono-cream.png`, `mono-cream.svg`, `mono-white.png`, `mono-white.svg`

### `lockups/`
Mark + name (set in the mark typeface, outlined). Horizontal and stacked x dark / light / transparent (for dark use) / transparent-onlight, as SVG, PDF and PNG

`swe-horizontal-dark.pdf`, `swe-horizontal-dark.png`, `swe-horizontal-dark.svg`, `swe-horizontal-light.pdf`, `swe-horizontal-light.png`, `swe-horizontal-light.svg`, `swe-horizontal-transparent-onlight.pdf`, `swe-horizontal-transparent-onlight.png`, `swe-horizontal-transparent-onlight.svg`, `swe-horizontal-transparent.pdf`, `swe-horizontal-transparent.png`, `swe-horizontal-transparent.svg`, `swe-stacked-dark.pdf`, `swe-stacked-dark.png`, `swe-stacked-dark.svg`, `swe-stacked-light.pdf`, `swe-stacked-light.png`, `swe-stacked-light.svg`, `swe-stacked-transparent-onlight.pdf`, `swe-stacked-transparent-onlight.png`, `swe-stacked-transparent-onlight.svg`, `swe-stacked-transparent.pdf`, `swe-stacked-transparent.png`, `swe-stacked-transparent.svg`

### `banners/`
README banner 1280x320 and social preview with name 1280x640, both on dark

`banner-readme-1280x320.png`, `banner-readme-1280x320.svg`, `social-preview-name-1280x640.png`, `social-preview-name-1280x640.svg`

### `fonts/`
Static TTFs for the mark's typeface (name and body weights) and the SIL OFL licence

`OFL.txt`, `SpaceMono-Bold.ttf`, `SpaceMono-Regular.ttf`

`brand-sheet.pdf`: one-page brand sheet (versions, palette, typeface, clear space, minimum size, dos and don'ts). `brand.md`: this file.

## Vector fidelity

SVG (rendered at 1024) vs the flat-palette raster: 0.0% of pixels differ by more than 40/255 away from colour edges (0.944% including the 1 px edge antialiasing); mean abs diff 1.08/255. Node counts: colour 875, mono 722.
