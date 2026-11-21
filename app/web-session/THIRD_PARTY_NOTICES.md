# Third-party notices

What in `app/web-session/` was written by someone else, and under which licence.
The packages the built bundle carries (React, React DOM, Scheduler, zustand)
have their licence texts in `src/swe/webapp/static/session/third-party-licenses.md`,
which the build writes.

## t3code (nothing copied so far)

The session redesign ([G1](../../docs/specs/G1-session-runtime-redesign.md),
section 4) names t3code as the reference for the look of the view and lists
what may be copied from it under its MIT licence: `components/ui/*`,
`ComposerPendingApprovalActions`, `ChangedFilesTree`, `DiffStatLabel` and
`ConnectionStatusDot`. As of G12 **no file here is copied from t3code**: the
timeline, the work groups, the Markdown reader and the small components in
`src/ui/` were written for this repository from the descriptions in G1, with
t3code's screens as a picture of the goal and none of its source open beside
them. The checkout of t3code was not available when G12 was built.

When a later milestone does copy a file, it adds a section here that names the
file, where it came from (repository, path, commit), and t3code's copyright line
and MIT licence text taken from t3code's own `LICENSE`, and puts a one-line
comment with the same source at the top of the copied file.

## Tailwind CSS preflight

`src/preflight.css` follows Tailwind CSS's preflight (MIT, Copyright (c)
Tailwind Labs, Inc.), with every rule scoped under `.swe-sv`. Its header says
so. The utilities and theme come from the `tailwindcss` package at build time.
