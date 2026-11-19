# G10: Session view toolchain

Milestone G10 of the session redesign (decisions D8, D9 and D11). It sets
up the React toolchain for the new session view, moves the design tokens
into a shared `tokens.css`, serves the view at `/session/` on the hub, and
adds a CI job. The view itself is a placeholder that proves the pipeline:
a breadcrumb header, an empty timeline and a disabled composer. The real
timeline is G12 and the composer G13; G14 shows the view inside the web
app.

## Layout

| Path | What |
|---|---|
| `app/web-session/package.json`, `pnpm-lock.yaml` | pnpm project, exact versions, `packageManager: pnpm@10.10.0` |
| `app/web-session/vite.config.ts` | the build: inputs, output names, `build-info.json`, the dev server |
| `app/web-session/index.html` | the standalone page (`/session/`) |
| `app/web-session/public/appearance.js` | classic script in the page's head that applies the stored appearance |
| `app/web-session/src/main.tsx` | the `session-view` entry: `SWESessionView.mount(el, {key})` |
| `app/web-session/src/boot.ts` | the page's entry: mounts on `#root` with `?key=` |
| `app/web-session/src/SessionShell.tsx` | the placeholder shell |
| `app/web-session/src/session-key.ts` | `?key=` and the breadcrumb text |
| `app/web-session/src/index.css`, `preflight.css`, `page.css` | Tailwind theme mapping, scoped base styles, page-only styles |
| `app/web-session/scripts/lint-tokens.mjs` | `pnpm lint:tokens` |
| `app/web-session/scripts/source-hash.mjs` | the source hash rule |
| `src/swe/webapp/static/session/` | the committed build output |
| `src/swe/webapp/static/tokens.css` | the shared tokens, loaded before `styles.css` |
| `src/swe/webapp/static.py` | `session_file` (the name check), `SESSION_CSP`, cache rules |
| `src/swe/remote/hub.py` | `_serve_session`, called by `_serve_static` for `/session` and `/session/…` |
| `tests/test_webapp_session_view.py` | route, headers, name check, fallback, freshness, tokens |

The build output:

| File | Cache | What |
|---|---|---|
| `index.html` | `no-cache` + ETag | the page |
| `session-view.js`, `session-view.css` | `no-cache` + ETag | the embeddable entry, stable names for G14 |
| `appearance.js` | `no-cache` + ETag | copied from `public/` |
| `assets/*-<hash>.js`, `assets/*-<hash>.css` | `public, max-age=31536000, immutable` | the React chunk, the page's boot chunk and stylesheet |
| `build-info.json` | `no-cache` + ETag | `source_hash`, per-file `sources`, and `outputs` (the only names the hub serves) |
| `third-party-licenses.md` | `no-cache` + ETag | the licence texts of the bundled packages (React, React DOM, Scheduler) |

## Commands

Run inside `app/web-session`:

| Command | What |
|---|---|
| `pnpm install --frozen-lockfile` | install the pinned dependencies, project-local |
| `pnpm dev` | Vite dev server at `/session/`; serves `/tokens.css` from the static folder and proxies `/v1` to `SWE_HUB_URL` (default `http://127.0.0.1:8477`) |
| `pnpm typecheck` | `tsc --noEmit`, strict |
| `pnpm lint:tokens` | fails on an arbitrary Tailwind value (`bg-[#171717]`), a colour literal, ALL CAPS, or the mono font outside code |
| `pnpm test` | Vitest under jsdom |
| `pnpm build` | writes `src/swe/webapp/static/session/` |

Rebuild and commit the bundle with every source change, and again after a
rebase. CI's Session view job runs the five steps above and then fails on
any difference in `src/swe/webapp/static/session`. On the Python side,
`tests/test_webapp_session_view.py` recomputes the source hash.

## Source hash

`build-info.json`'s `source_hash` is computed by
`scripts/source-hash.mjs`, and the Python test recomputes it the same way:

1. Walk `app/web-session`. Skip any name starting with `.`, `node_modules`,
   and files ending in `.md` or `.tsbuildinfo`. Symlinks are skipped.
2. For each file, take its `/`-separated path relative to
   `app/web-session` and the sha256 of its raw bytes.
3. Sort by path, join `<path>\0<sha256>\n` lines, and take the sha256 of
   that text.

The build is reproducible: two builds from the same source give
byte-identical output. Hashed names come from file contents, and nothing
time-dependent is written.

## Tokens and Tailwind

`tokens.css` holds the light `:root` block, the dark media block
(`:root:not([data-theme="light"])`) and the forced dark block
(`:root[data-theme="dark"]`) exactly as they were in `styles.css`.
`index.html` links it before `styles.css`. The session page links
`/tokens.css` itself; when G14 embeds the view, the web app has already
loaded it.

`src/index.css` imports Tailwind's theme and utilities, replaces its
preflight with `preflight.css` (every rule under `.swe-sv`), resets
`--color-*`, `--font-*` and `--radius-*`, and maps SWE's tokens with
`@theme inline`, so a utility reads the token directly (`bg-card` is
`background-color: var(--card)`) and follows every palette:

| Tailwind name | SWE token |
|---|---|
| `wash`, `panel`, `card`, `subtle`, `hover`, `selected` | `--wash`, `--panel`, `--card`, `--subtle`, `--hover`, `--selected` |
| `primary`, `secondary`, `muted` (text) | `--text-primary`, `--text-secondary`, `--text-muted` |
| `border`, `border-strong`, `divider` | `--border`, `--border-strong`, `--row-divider` |
| `accent`, `accent-fill`, `interactive`, `link` | `--accent`, `--accent-fill`, `--interactive`, `--link` |
| `success`, `warning`, `danger`, `warning-text`, `danger-text` | the same names |
| `rounded-card`, `-control`, `-field`, `-block`, `-dialog` | `--card-radius`, `--control-radius`, `--field-radius`, `--block-radius`, `--dialog-radius` |
| `font-sans` | `--font` |
| `font-mono` | `ui-monospace, SFMono-Regular, Menlo, monospace` (code only) |
| `*-safe-top`, `-right`, `-bottom`, `-left` (spacing) | `--safe-top`, `--safe-right`, `--safe-bottom`, `--safe-left` |

Tailwind's spacing, type scale and container sizes stay as they are.
Test files are excluded from Tailwind's class scan, so the lint test's own
bad examples never reach the CSS.

Appearance works as in the web app: Settings stores `light` or `dark`
under `localStorage["swe.appearance"]` (System removes it), and
`data-theme` on `<html>` forces the palette. `appearance.js` applies it
before the first paint and follows a change made in another tab.

## Serving

`/session` redirects (301) to `/session/`, keeping the query. Under
`/session/`, `webapp/static.py::session_file` admits a path only if it is
`name.ext` or `assets/name.ext`, has no leading dot or `..`, is listed in
`build-info.json`'s `outputs`, is a regular file and not a symlink (nor in
a symlinked folder), and resolves inside the bundle. The path is never
unquoted, so `%2e%2e` and `%2F` never match. Anything else is a 404.

Auth is the same as `/`: the page and its files are public, and the data
comes from `/v1` with the device credential.

Every `/session/` response carries this Content-Security-Policy:

```
default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
font-src 'self'; connect-src 'self'; frame-src 'self'; base-uri 'none';
form-action 'none'; frame-ancestors 'self'
```

`frame-ancestors 'self'` is the one addition to the policy in the plan. A
route that sets its own policy replaces the hub's default one, and the
default carries `frame-ancestors 'self'`; without it the session view
would lose that framing rule (the `X-Frame-Options: SAMEORIGIN` header
still applies). The built page needs nothing looser: no inline script or
style, no eval, and every file it loads is on the same origin.

When the bundle is missing (no `build-info.json`, or `index.html` not
there), `/session/` answers 503 with a short plain page saying the view
is not built, and other names answer 404.

## Packaging

The wheel ships `src/swe` whole (`packages = ["src/swe", "src/quiver"]`),
so the bundle is in it; the sdist excludes only `app/`, so it carries the
bundle but not the source. `app/mac/script/bundle-swe.sh` copies
`src/swe` with `cp -R`, so SWE.app carries it too. No `.gitignore`
pattern matches the bundle (a test checks it). Nothing needed changing.
The Nix source filter leaves out `app/web-session`, so the freshness test
skips there.

`.gitattributes` marks `src/swe/webapp/static/session/**` as
`linguist-generated -diff`.

## Dependencies and licences

Direct dependencies, pinned exactly:

| Package | Version | Licence | Use |
|---|---|---|---|
| react | 19.3.1 | MIT | runtime |
| react-dom | 19.3.1 | MIT | runtime |
| vite | 8.3.6 | MIT | build, dev server |
| tailwindcss, @tailwindcss/vite | 4.3.4 | MIT | styles |
| typescript | 6.0.3 | Apache-2.0 | typecheck |
| vitest | 5.1.0 | MIT | tests |
| jsdom | 31.0.0 | MIT | test DOM |
| @types/react, @types/react-dom | 19.3.0 | MIT | types |
| @types/node | 22.20.5 | MIT | types |

JSX compiles through Vite's built-in Oxc transform with
`jsx: "react-jsx"`, so there is no `@vitejs/plugin-react`; Fast Refresh in
`pnpm dev` is not needed yet.

All installed packages, transitive included (`pnpm licenses list`):

| Licence | Packages |
|---|---|
| MIT | 71 packages, including react, react-dom, scheduler, vite, rolldown, tailwindcss, vitest, jsdom, postcss |
| Apache-2.0 | typescript, detect-libc, expect-type, xml-name-validator |
| BSD-2-Clause | entities, webidl-conversions |
| BSD-3-Clause | source-map-js, tough-cookie |
| ISC | picocolors, saxes |
| BlueOak-1.0.0 | graceful-fs, lru-cache (permissive, OSI approved) |
| MPL-2.0 | lightningcss (a dependency of vite and @tailwindcss/node) |

Only react, react-dom and scheduler (all MIT) end up in the bundle. The
two outside the MIT, Apache, BSD and ISC set are build tools that never
ship: BlueOak-1.0.0 is permissive, and lightningcss is MPL-2.0, a
file-level licence that covers its own source, not the CSS it produces.
Vite 8 depends on it directly, so it cannot be dropped without dropping
Vite.

## Bundle size

Measured on the committed build (gzip level 9):

| File | Raw (bytes) | Gzip (bytes) |
|---|---|---|
| `assets/react-*.js` | 218,729 | 67,354 |
| `session-view.js` | 2,815 | 1,240 |
| `session-view.css` | 5,071 | 1,688 |
| `appearance.js` | 849 | 479 |
| `index.html` | 773 | 363 |
| `assets/index-*.js` | 166 | 147 |
| `assets/index-*.css` | 66 | 84 |
| Page total | 228,469 | 71,355 |

`build-info.json` and `third-party-licenses.md` are not loaded by the page.

## Notes for later milestones

- G11 replaces the breadcrumb's placeholder: today the harness stands where
  the project will go and the session id where the title will, since this
  view reads no session data.
- G14: Tailwind's utilities are global class names in a cascade layer. In
  the web app, its unlayered `styles.css` rules win over them, and an
  element of the plain-JS app with a class such as `flex` or `hidden` would
  pick up the utility. Check for clashes before embedding, or switch the
  view to a Tailwind prefix.
- The plan says a missing bundle falls back to `chat.js`. That fallback
  belongs to G14, where the web app chooses which view to show; G10's route
  answers the plain "not built" page.
