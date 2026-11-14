# SWE state arrows

Artwork for the status indicators every client draws. `AGENTS.md` and the
ported-logic table in `docs/ported-logic.md` hold the state mapping.

| Colour | Intended state |
| --- | --- |
| Red | Critical failure |
| Yellow | Partial failure or attention |
| Green | Fully operational |
| Grey | Interrupted or unknown |

Pair every arrow with state text or an accessible description. The shared
shape does not distinguish states without colour.

`normal/` contains SVGs and PNGs at 16, 32, 64, 128, 256 and 512 pixels.
`pixel/` contains 8, 16 and 32 pixel grids at integer scales. Combined rows
retain the original three-colour logo motif; grey is a standalone state.
`arrows-preview.png` shows the original three-colour export. `mockup.html`
is an illustrative proposal, not a capture of the current app.

The four normal SVGs are copied into Mac resources, the iOS asset catalog and
web static files. Each client draws them through its own indicator: the Mac's
`StateArrow` view, iOS's `StatusDot`, and the web's `state-<color>.svg` images
on the service-status page and session badges.

`arrows.py` is the original export script, kept as a source reference. It is
not a build step: it uses its original `/workspace` output path and a Linux
font path, requires CairoSVG, Pillow and Shapely, and exports the original
red, yellow and green set. It does not regenerate the added grey variants.
The project adds no runtime dependencies for these assets.
