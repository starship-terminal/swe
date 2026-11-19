# Web app

The Mac app's pages as a web app, served by the remote hub, so they open in
any browser on the tailnet. Decided 2026-10-23: one UI, web first. Pages are
built once, in HTML, and the logic they need lives in Python on the hub. The
native app in `app/mac/` is left exactly as it is while this is tried, so going
back means deleting this, not repairing anything.

## Why web first

The Mac app's busiest pages (Overview, Accounts, Calendar, Compute, Models,
Pricing, Service status) run on Swift code with no Python equivalent: the
usage ledger, pricing, the account fetchers. A browser on another machine
cannot reach that code, so a web version needs it in Python anyway. Built
alongside the native pages, every change would have to be made twice, and the
web version would always trail. With one UI the change lands once and every
client sees it.

## Shape

| Piece | Where | What |
|---|---|---|
| Pages | `src/swe/webapp/static/` | Plain HTML, CSS and JS: no build step, no framework, no CDN. The one exception is the session view under `static/session/` (below), which has a build step whose output is committed. Same Devin-derived look as the Mac app (`Theme.swift`); the CSS variables in `tokens.css` carry the same names, and every page loads `tokens.css` before its own styles. |
| Install | `static/manifest.webmanifest`, `index.html`, `styles.css` | The manifest makes it installable (`display: standalone`, `id`, `start_url`; its colours are the light default's `--wash`, with `color_scheme_dark` for dark). `index.html` has one `theme-color` meta per scheme, which `syncThemeColor` in `app.js` overrides when Settings forces Light or Dark. `viewport-fit=cover` draws under the notch, so layout pads by `--safe-top/right/bottom/left` (`env(safe-area-inset-*)`). `tests/test_webapp_pwa.py` keeps the colours in step with `tokens.css`. |
| Page data | `src/swe/webapp/api.py`, `src/swe/webapp/pages/` | Builds each page's JSON from the domain services, in process, the way `/v1/usage` does. |
| Ledger | `src/swe/ledger/` | The Python port of `SWECore/Ledger`: scans harness logs into its own SQLite store and prices tokens. |
| Routes | `src/swe/remote/hub.py` | `/` serves the app and its assets as flat names beside it (`_serve_static`, names checked by `webapp/static.py`); `/v1/app/*` serves their data. Old `/app/…` paths redirect to the same name at the root. `/session/` serves the session view's committed build (`_serve_session`). |

The hub already runs on the always-on machine, binds 127.0.0.1 and is
published by Tailscale Serve, so the web app needs no new port, process or
service: it is at `https://<host>:<hub port>/`.

## Auth

The hub owner's own devices need no pairing: open the hub's URL and the
app loads. The hub identifies the caller with `tailscale whois` (behind
`tailscale serve`, on the client serve forwards for) and lets in a node
whose login is the hub owner's, as long as the request carries
`X-Swe-Pair: 1`, names a Host the hub answers to, and has no foreign
`Origin`. That is the gate the app's tailnet-identity calls use: a page
on another site can't send the header, so it can't borrow the owner's
network identity. The hub machine's own node doesn't count (any local
process reaches the hub as that node), so a browser on the hub machine
pairs like anyone else. Tagged nodes and other people's logins still pair
from the gate itself: "Pair with Tailscale" posts
`POST /v1/pair/request` and polls for the owner's yes, the token field
below trades a pair token through the app's own `#pair=` fragment, and
either mints a device credential (`Authorization: Bearer`, kept in
`localStorage` under the hub's origin, as `swe.remote`; every key the
app keeps there starts `swe.`, and `app.js` moves a key a browser saved
under its pre-rename `quiver.` name on the first load that finds it,
keeping the `swe.` value if both exist; `quiver.remote` is copied but
kept, so a tab still on an older page stays signed in, until the next
credential is saved or forgotten). The app lived under `/app/`
until #944: that path now 301-redirects to the same name at `/`, query
and fragment intact, so a `/app/#pair=` link or `/app/#approve` bookmark
minted then still lands. The browser keeps the fragment across the
redirect (it never reaches the hub); the hub copies the query onto
`Location`. Only a name the root serves is redirected, so no `/app/…`
path can point `Location` at another host. Satellite routes (`/v1/agent/*`) always want a bearer.
Behind `tailscale serve`, run the hub with `--public-url=https://…` so it
reads the tailnet client from Serve's headers: "Pair with Tailscale" then
works, and the Origin check accepts the `https://` origin Serve presents.
A pair token (`swe remote pair`) works either way.

Writes are `POST /v1/app/<page>/<action>` with the same bearer and the
same Origin check every `/v1` write uses. The first is the Harnesses page's
star, archive and restore (docs/web-app/harness.md).

Doctor (`#/doctor`, docs/web-app/doctor.md; Fix all posts to
`/v1/app/doctor/fix-all`) is a page with no sidebar
entry, as in the app, where it is a Settings section.

## Data on the hub machine

The web app shows the machine the hub runs on. Its ledger lives in
`~/.swe/data/web-ledger/`, apart from the Mac app's
`~/.swe/data/ledger/`, so the two never write the same file even when
both run on one Mac. Other machines' usage reaches the hub the way limits
already do, through satellites; that is a later step.

## Pages, in order

1. Overview (tokens, cost, harness split, daily activity, top models). Shipped.
2. Sessions (the hub's catalog, with tokens and cost per session from the
   ledger). Shipped. Differences from the app: Resume becomes "Copy resume
   command" (the browser may be on another machine), the Claude desktop
   "Started from" label is left out, the model
   filter counts tokens of the sessions in the chosen window rather than the
   app's daily rows, and usage joins only for sessions on the hub machine
   (a satellite's rows list without tokens). Sessions only in an SWE
   account profile's home, and Devin cloud sessions, are not listed yet.
   The search box filters each row's details in the browser (the list redraws 80 ms after typing pauses) and, after a
   250 ms pause, asks `/v1/search` for what was said: a session whose
   transcript matches stays in the list with the matching snippet under
   it. That needs a search index on the hub (the Mac app's, or
   `swe session index --on`, or the switch in Settings › Experimental);
   without one the page says it is matching titles and details only and
   links to that switch. The index covers the hub machine, so a
   satellite's sessions match on their details.
   A row opens the session's chat, the remote control client
   (docs/web-app/sessions.md); its Details button is the old sheet.
3. Harnesses and Accounts limits. Shipped, read only. Harnesses lists
   `/v1/app/harnesses` (every registered harness, archived included, as
   `swe list --scope=all`) with the app's In use / Archived scopes, search,
   starred-first order and the fold for unstarred rows. Accounts is one
   card per starred harness from `/v1/usage`, per machine (hub and each
   satellite), with the app's limit bars, plan badge, on-demand and extra
   usage rows and status lines. Harnesses can star, unstar, archive (with
   a reason) and restore, edit a row, Detect and Add harness
   (docs/web-app/harness.md). SWE's own Claude and Codex account
   profiles, history, and Open app / Terminal are Mac-only.
4. Skills, AGENTS.md, MCP servers, Plugins, Doctor: read-only first, from the
   `swe … --json` builders. Shipped.
5. Writes for the Manage pages. Shipped; [FEATURES.md](../FEATURES.md)
   names what each page still lacks next to the Mac app.
6. Calendar, Compute, Models, Pricing, Service status. Shipped, with the
   same per-page notes in FEATURES.md.

The GitHub pages shipped alongside that list: Repositories and Inbox
(docs/web-app/repositories.md) and Tasks (docs/web-app/tasks.md) — the
work-items surface over the hub's task store.

Mac-only parts stay native whatever happens here: the menu bar, widgets,
notifications, and opening a terminal.

## Adding a page

Every page after the first four is a plugin, so pages are built in
parallel without touching a shared file. A page with the sidebar id
`skills` is:

| File | What |
|---|---|
| `src/swe/webapp/pages/skills.py` | `get(ctx, args) -> dict` answers `GET /v1/app/skills` (`args`: each query parameter's first value). An optional `post(ctx, action, body) -> dict` answers `POST /v1/app/skills/<action>`. Raise `PageError(status, code, message)` to refuse. `ctx.state` is the hub's `HubState`, `ctx.home` its home. |
| `src/swe/webapp/static/pages/skills.js` | `registerPage("skills", render)`. The bundle runs each script in its own function scope, so private names never collide with another page's. `render(id)` paints `#panel` with the shared helpers in `app.js` (`api`, `esc`, `icon`, `pageHeader`, `renderGate`) and `manage.js` (`pageError`, `bindRefresh`); take `const gen = routeGen` before awaiting and drop the reply when it changed. |
| `src/swe/webapp/static/pages/skills.css` | Optional styles, using the tokens in `tokens.css`. Buttons, spinners and switches are shared there (`.btn` with `.primary`, `.danger`, `.lg`, `.hairline`, `.on-panel`, `.medium`, `.secondary`; `.spinner`; `.switch`), so a page rule only says what is its own, never a copy of them. |
| `tests/test_webapp_skills.py` | The page's data, and the route with auth. |
| `docs/web-app/skills.md` | What the page shows, its payload, and what differs from the Mac app. |

The hub dispatches `/v1/app/<page>` to the module (device bearer as for
every `/v1` route; POSTs also pass the Origin check) and serves every
page script as one `/pages.js`, every stylesheet as `/pages.css`.
The page goes live in the sidebar as soon as its script registers. The
sidebar's order and icons are fixed in `app.js` and already list every
Mac app page; Menu bar and Widgets wear Settings' "Mac app only" pill and
are not links (`MAC_ONLY`), and a `#/menubar` or `#/widgets` link opens
Overview.

A page that needs usage reads the shared ledger with `with ctx.state.webapp.ledger() as (store, catalog, scan_ok):`, never a store of its own.

Read data the way the Mac app does: from the same `swe … --json`
builders in process, not by shelling out, and never from the Mac app's
own stores.

## Session view (the one build step)

The session view (decision D9 of the session redesign) is React, and it is
the only part of the web app with a build step. Its source is
`app/web-session/` (pnpm, Vite, React, TypeScript, Tailwind mapped onto
`tokens.css`); its build output is committed to
`src/swe/webapp/static/session/`, so pipx installs, `install.sh` and the
`swe` inside SWE.app serve it with no Node at run time. The hub serves it
at `/session/` (`/session` redirects there), public like `/`; the page
reads `/v1` with the same device credential. Everything else in
`static/` stays plain JS with no build.

| Rule | What |
|---|---|
| Rebuild | `cd app/web-session && pnpm install --frozen-lockfile && pnpm build` writes `static/session/`; commit the source change and the rebuilt bundle together. |
| After a rebase | Rebuild and commit again. The bundle never merges by hand: `.gitattributes` marks it generated and folds its diff. |
| Freshness | `build-info.json` holds a `source_hash` over the source and lockfile. `tests/test_webapp_session_view.py` recomputes it in Python, and CI's Session view job rebuilds and fails on any `git diff` in the bundle. |
| Serving | Only names the build lists in `build-info.json` are served (`webapp/static.py::session_file`). `index.html` and the stable names (`session-view.js`, `session-view.css`, `appearance.js`) revalidate by ETag; hashed chunks under `assets/` are immutable for a year. Every response carries the strict policy in `webapp/static.py::SESSION_CSP`. Without a bundle, `/session/` answers a plain page saying so. |

`docs/specs/G10-session-view-toolchain.md` has the layout, commands, token
mapping and dependency list.

## Going back

Nothing in `app/mac/` changes. To drop the web app, delete `src/swe/webapp/`,
`app/web-session/`, `docs/web-app/`,
`src/swe/ledger/`, the root static handler (`_serve_static`) and the
`/v1/app/` branches in `hub.py`, and
`~/.swe/data/web-ledger/`.
