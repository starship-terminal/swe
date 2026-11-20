# Plugins

The Mac app's Plugins page (`PluginsPage.swift`, `PluginExplorer.swift`)
at `/#/plugins`: both tabs, with Health's Fix and Fix all, and the
plugin sheet's Install, Enable, Disable and Uninstall per harness. Module `swe/webapp/pages/plugins.py`,
script and styles `static/pages/plugins.js` and `plugins.css`.

## What it shows

Same title and subtitle as the app, and its two tabs.

- **Health**: `swe find plugins --json`. One card per harness with drift,
  in report order; each finding has its ref, kind badge (Not installed,
  Out of date, ...), detail, the differing files (stale only), and the
  fix swe would run with a copy button. A finding swe can fix has a
  **Fix** button; one that is an edit to make by hand says so instead.
  A card folds after 8 findings. Empty states as in the app: nothing in
  `~/.swe/plugins`, or every harness runs what it holds.
- **Fix** opens an inline confirm under the finding, the app's "Run this
  fix?": "swe will check the finding still applies, then run:" and the
  exact command(s). **Run** posts it; Escape or Cancel closes it.
- **Fix all** (in the header while any finding is fixable) opens a confirm
  card above the findings, "Run N fixes?", listing every finding it will
  fix with its command. **Run all** sends exactly those ids. While a fix
  runs, every Fix, Fix all and Refresh is off and the running row shows a
  spinner. Afterwards the cards repaint from the hub's answer, under a
  banner as in the app: "Ran <command>" or "Ran N fixes", or the error
  ("Ran 1 fix, then stopped. <error>" when Fix all got partway).
- **Explore**: `swe plugins catalog --json` placed the way
  `PluginExploreIndex` places it: Local (the hub), then each harness in
  swe's order, then Not registered. Each marketplace is a card that opens
  in place (closed by default; a search opens every match) with its
  source tag (`local`, `github`, `git`, `cache`), a lock for GitHub ones,
  and its installed count. Each plugin row has its name, version, a drift
  badge that opens Health, its ref when a display name differs, its
  description, up to 8 skill badges, and a chip per harness with the
  app's status dot (Enabled, Disabled, Installed, Available, Cached).
  The harness select and search ("Search plugins and their skills")
  filter rows as the app does, and the count reads "N of M plugins".
  What swe could not read is listed above the sources. A plugin's name
  opens a sheet with its marketplace, per-harness state, components,
  category, author, homepage, tags, folders and skills, then "In each
  harness": a row per harness that has it or could, with its state and
  the app's menu items as buttons (Install where its marketplace is
  listed; Enable or Disable and Uninstall once installed; Cursor personal
  installs offer supported controls, while team policy and configured/pinned
  restrictions show a read-only or removal-only explanation). A button opens a confirm under its
  row with the app's question and the command or config edit it makes
  ("Disable tool@mk in Codex on box?", `set [plugins."tool@mk"] enabled =
  false in ~/.codex/config.toml`); Uninstall's button is red. While it
  runs the row reads "Disabling…" and every other button waits; then the
  sheet repaints from the answer with the app's summary ("Disabled
  tool@mk in Codex") or the error.

Like the app, Explore is the first tab and the one the page opens on.

## Payload

`GET /v1/app/plugins?view=health|explore[&refresh=1]` (default `health`).
Both carry `view`, `built_at` (epoch seconds the view was read) and
`machine`.

- `health`: `{sources, drift: [...]}` as `swe find plugins --json`, each
  finding with `kind_title` and `severe` added.
- `explore`: `harnesses`, `actions`, `errors` and `total` from the
  catalog; `plugins`, each catalog plugin once keyed by ref, slimmed to
  what the page shows (`title` is the display name, `harnesses` maps each
  harness to `{status, actions, version, scopes, path, copies}` with
  `actions` the verbs `PluginCatalog.actions(for:in:)` offers there and
  `copies` a count, `skills` are `{name, description}`, `drift` is the worst gap as
  `{state, title, harnesses}` or null); and `sources`, each
  `{id, kind, harness, title, note, marketplaces}`, each marketplace
  `{id, name, title, tag, plugins}` and each row `{ref, harnesses}`, the
  harnesses whose chips it shows. Filtering and installed counts run in
  the browser.

Each view is kept in the page module for 60 s (the app's snapshot age),
one read per view at a time; Refresh passes `refresh=1`.

### Fixes

`POST /v1/app/plugins/fix` with `{"id": "<fix id>"}`, and
`POST /v1/app/plugins/fix_all` with `{"ids": ["<fix id>", ...]}` (the ids
the confirm listed). Behind the device bearer and the Origin check, like
every page write.

The hub reads the findings again and refuses unless every id is one
Health lists right now (404 `unknown_finding`) and one swe can run (422
`not_runnable`); a malformed body is 422, and a second fix while one runs
is 409 `busy` (one fix at a time across the hub). It then calls
`swe.find.plugin_drift.apply_fix`, the function behind
`swe find plugins --fix=<id>`, in process: the commands are the ones that
module builds from the finding, run without a shell, with `HOME` set to
the hub's home. Fix all follows `PluginsModel.fixAll`: report order, the
next fix picked from a fresh read so one an earlier fix cleared is
skipped, only the listed ids (a finding a fix newly exposes waits for its
own confirm), stopping at the first failure. Both cached views are
dropped, and the answer is

```json
{"action": "fix_all", "ran": 1,
 "results": [{"id": "...", "status": "fixed", "fixed": {...FixedDrift row...}, "error": null},
             {"id": "...", "status": "failed", "fixed": null, "error": "codex plugin remove tool@mk failed: ..."},
             {"id": "...", "status": "not-run", "fixed": null, "error": null}],
 "health": {"sources": 1, "drift": [...], "view": "health", "built_at": 0, "machine": "box"}}
```

with `status` one of `fixed`, `failed`, `gone` (cleared before its turn)
or `not-run` (after a failure). `fixed` is the finding as `FixedDrift`
(`swe find plugins --fix=<id> --json`) carries it. A command that fails
or is missing from the hub's `PATH` is a `failed` result, not an HTTP
error, so the page still gets the fresh findings.

### Install, enable, disable, uninstall

`POST /v1/app/plugins/plugin` with `{"verb", "ref", "harness"}` runs
`swe.plugins.actions.plugin_action`, the code behind `swe plugins
<verb> <ref> --harness=<h> --json`, in process: the harness's own CLI
(`claude plugin install|uninstall|enable|disable`, `codex plugin
add|remove`) with `HOME` pinned to the hub's, or for Codex enable and
disable swe's proven one-line `config.toml` edit. Only a verb Explore
offers that plugin in that harness right now (409 `not_offered`; 404
`unknown_plugin` for a ref the catalog doesn't have; 422 `bad_action` for
a malformed body). It shares the fixes' one-at-a-time lock (409 `busy`).
Both cached views are dropped and the answer is `{"action": "plugin",
"result", "error", "explore"}`: `result` is the `ActionResult` `--json`
shape (`changed`, `detail` when nothing needed doing, `ran`, `edited`,
`state`) or null when the command failed, and `error` says why.

## Differences from the Mac app

Explore's other writes are left out; they stay in `swe` and the Mac app:

- The actions live in the plugin sheet, not in a menu on each row's
  chip; the chips show the state only.
- **Add marketplace** (`swe plugins marketplace add`).
- **Rename** of a marketplace or plugin display name. The names the app
  saved in `~/.swe/config/plugin-display-names.json` are read (never
  written) so rows are named the same.
- The plugin detail page's rail (manifest fields, file list) and inline
  file editor, and Show in Finder. The web detail is a read-only sheet.

Also different:

- A plugin's skills are the catalog's (read from each harness's loaded
  copy). The app also folds in installed copies `swe skills` finds inside
  the plugin and drops one shown on the Skills page; that placement waits
  for the Skills page.
- Fix all is one request, so its button reads "Fixing N findings…" for
  the whole run rather than the app's "Fixing 2 of 5…". The fixes run on
  the hub; closing the tab does not stop them.
- The confirms are inline (a panel under the finding, a card above the
  findings) where the app uses confirmation dialogs.
- A fix does not refresh the Skills page's data; Explore reads again the
  next time it opens.
- The app's last-result and error banners for Explore's actions do not
  exist, since Explore has no actions here.

## Local inventory and SWE ownership

Explore filters out marketplace-only previews and installs without files. Installed plugins can be enabled, disabled and uninstalled in their existing harness before import. Import to SWE is required for source ownership/editing and new source installs, and remains a separate detail action. This uses the existing `plugin` POST with `{verb: "import", ref, harness: "swe"}` and returns the same action result and refreshed view. The row gains `managed`; an unmanaged row has a synthetic `swe` action cell. The full catalog is still available from the CLI. Import does not register, compile or install the SWE source into a harness.

Cursor account state and per-plugin action restrictions match the Mac app. The existing plugin POST routes Cursor actions to the authenticated API; its control details contain no credentials. Team entries stay read-only; configured/pinned personal plugins offer removal only.

Devin resolved installs offer native removal only; OpenCode global file plugins
offer enable/disable/removal. Both clients obey each state's `allowed_actions`
mask. OpenCode controls move discovery entries into/out of SWE storage and apply
to new sessions; package and project configuration is unchanged. Devin removal
uses its native personal-cloud behavior and never forces governance removal.
