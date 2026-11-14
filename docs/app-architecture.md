# macOS app architecture

The app under `app/mac/` is a SwiftPM package. Its main targets are
`SWECore` (models, decoders, stores, samplers), `SWEApp` (the window,
menu bar and pages) and `SWEWidgetsUI` (views shared by the app preview
and the widget extension); `SWEWidgets` is the widget extension itself
and `SWEFanHelper` the root fan-control helper, which links only
`SWECore`. Views live in `SWEApp`; logic that deserves a test lives in
`SWECore`. This document is the frontend map; [ARCHITECTURE.md](../ARCHITECTURE.md)
covers the whole repo and [json-contracts.md](json-contracts.md) the `swe`
decoder shapes.

## Folder layout

```
Sources/SWEApp/
  App/         entry point and app-wide state
    SWEApp.swift     @main, the Window and menu bar scenes
    main.swift          NSApplication bootstrap
    AppModel.swift      the one app-wide model every page reads
    LimitsMonitor.swift limit notifications and the menubar figure refresh
    ServiceStatusNotifier.swift  provider incident banners (rule in SWECore ServiceStatusAlerts)
  Navigation/
    MainWindow.swift    sidebar (collapsible to a rail) plus the page
  MenuBar/
    StatusItemController.swift  the status item, its panel and rows
  Pages/       one file per page (a page may hold private subviews)
  Components/  reusable views and controls
  Theme/       design tokens: colors, type, metrics, Card and Page
Sources/SWECore/
  one folder per domain: Swe, Primitives, Mcp, Sessions, Hub, Ledger,
  Accounts, Limits, Status, APIKeys, Agents, Calendar, Catalog,
  Compute, GitHub, Models, Onboarding, Search, Services, Syntax,
  Tasks, Usage, Vector, Providers, MenuBar, Markdown, Lists,
  Snapshots, Support, App, Updates; Resources holds the bundled fonts
  and logos
```

## AppModel

`AppModel` (`App/AppModel.swift`, `@Observable`, `AppModel.shared`) is the
single environment object. It owns the `SweClient`, the page models that must
outlive their page (`accounts`, `others`, `skills`, `mcp`, `plugins`,
`pluginCatalog`, `sessions`, `limits`, `serviceStatus`, `apiKeys`,
`overview`, `providers`, `apps`, `popup`, `widgetSelection`, `menuBar`), and
the UserDefaults-backed settings (`swePath`, `terminal`,
`appearance`, Dock presence, `lastSection`, `codeEditor`, `navReselects`,
`pluginsTab`). `start()` kicks off the watchers: model restores, the
skills/catalog cross-feed, sessions, limit refresh loops, widget snapshot
writes and the activation-time provider recheck.

Page models are `@Observable` classes in `SWECore` (`OverviewModel`,
`HarnessesModel`, `SkillsModel`, `McpModel`, `McpServerRepoModel`,
`PluginsModel`, `PluginCatalogModel`, `SessionsModel`, `ServiceStatusModel`,
`DoctorModel`, `APIKeysModel`, `AccountsModel`, `OtherLimitsModel`,
`ComputeModel`, `AgentsModel`, `ProviderVisibilityModel`, `UpdatesModel`).
Pages get them
from `AppModel` or build them in a `.task` and keep them in `@State`
(`DoctorModel`).

## Navigation

`MainWindow` lays the sidebar and the selected page out in an `HStack`:
the layout reserves `sidebarCollapsedWidth` or `sidebarWidth` and the
`Sidebar` view overlays it at its own width. `AppModel.selection` and
`sidebarCollapsed` persist across launches; `SWECore`'s
`SidebarExpansion` carries the pointer state so a collapsed rail floats
open under hover and a toggle under the pointer stays shut until the
pointer leaves once (`hoverSuppressed`). The View menu's Toggle Sidebar
(⌃⌘S) and the sidebar's own first row flip the same flag. While pinned
open, a drag strip on the sidebar's trailing edge resizes it live
(`AppModel.sidebarWidth`, clamped by `SWECore`'s `SidebarWidth` to
160...360, standard 196; double-click resets, the value persists).
Reselecting the current sidebar item (or the Plugins > Explore tab) bumps
`navReselects`, which detail views watch to go back through their own
unsaved-changes ask. The menu bar and the desktop widgets are separate
pages: `MenuBarPage.swift` (status item and popup tabs) and
`WidgetsPage.swift` (one underline tab per `WidgetCatalog` kind, its
previews and settings, the chosen kind stored in `desktopWidgetTab`).

The sidebar's first row is the machine switcher (`MachineSwitcherButton`,
#681): "This Mac" shows the native pages, and another machine's hub
takes over the whole window with `HubWebPage`, a `WKWebView` on that
hub's web app at `/` — the native sidebar included, since the web app
brings its own. Its title-bar strip holds the machine menu
(`StripSwitcher`, an `NSButton` like the sidebar toggle: a SwiftUI
button in that row never fires), the way back to This Mac. `SWECore`'s
`MachineSwitcher` keeps the list and the pick in
the app's preferences (`remoteHubs`, `remoteHubAgent`,
`remoteHubSelected`): the hub `swe remote agent --status --json` names
(this Mac's `agent.json`, refreshed when the window opens) plus hubs added
by address in Settings › Integrations › Remote › Other hubs. Opening a
native page (sidebar click, ⌘-number, a notification) brings This Mac
back. `HubNavigation.decide` pins the web view to the hub's origin (plus
`about:blank`/`about:srcdoc` and the hub's own `blob:` URLs): a clicked
link elsewhere, http(s) or mailto, goes to the default app, and anything
else, `file:` included, never loads. Each hub gets its own persistent
`WKWebsiteDataStore` (`HubOrigin.dataStoreID`), so the web app's pairing
credential survives a restart. `HubWebViews` keeps the views of the last
two hubs shown (`RecentKeys`); an older hub's view and its WebContent
process go, and it loads again when picked. The hub signs the web view in exactly as
it does a browser on this Mac: the owner's tailnet identity (#653), else
the web app's own pairing card. The bundle sets
`NSAllowsArbitraryLoadsInWebContent` so a plain-http `--tailnet` hub
loads.

## Theme

`Theme` (`Theme/Theme.swift`) is an enum of tokens, never a view: surfaces
(`wash`, `panel`, `card`, `field`, `code`, `subtle`, `hover`, `selected`),
text roles (`textPrimary` through `textMuted`, `dangerText`...), borders and
accents (`accent` emerald for money and "on", `interactive` blue for links,
focus rings and selected controls, `link` at 85% of it), type (`display`,
`figure`, `sectionTitle`, `cardTitle`, `rowTitle`,
`bodySmall`, `caption`, `control`, `tableHeader`...), and metrics (card and
control radii, paddings, field/row/icon heights, sheet sizes, animation
durations, the heatmap palette). The colour values come from the
design-systems repo's `systems/devin-desktop` package: one ink at fixed
alphas, three surfaces, blue kept for interaction. Feature-specific numbers stay near their
view as `private static let`. `Card` (surface with optional title/accessory)
and `Page` (title, subtitle, actions, centred 1152pt column) are the two
scaffolds; `PageHeader` is the non-scrolling variant used by sheets.

## Components

| View | What it is |
|---|---|
| `Card`, `Page`, `PageHeader` | the card surface and the page scaffold (Theme) |
| `Tabs` | the one tab switcher; `.underline`, `.segmented`, `.period` styles; `onSelect` fires on reselect |
| `ExpandRow` | the "Show N more" / "Show less" fold row; `.cardBottom` at a card's edge, `.inset` inside one |
| `SelectMenu` | the themed dropdown; SWE-drawn list (`SelectOptionList`), `icons` draws a logo per option, `symbols` an SF Symbol |
| `SearchField` | the filter text field |
| `EllipsisMenu` | a row's "more" menu: quiet ellipsis, hover shape |
| `FormField` | a labelled sheet field with an optional hint/problem |
| `Badge` | a small capsule label (plan, state, count) |
| `HarnessIcon`, `HarnessMark`, `HarnessMarkView`, `ChartSwatch` | the logo lookup: icon, icon+wordmark, grid cell, legend swatch |
| `EmptyCard`, `LoadingCard`, `ErrorCard` | status and empty states, always inside a card |
| `SkeletonBlock` | a placeholder bar with a light band sweeping it, for loading states laid out like their content |
| `ExpandRow` + `RowFold` (SWECore) | the fold pair: `RowFold.visible`/`hiddenCount` cut the rows, `ExpandRow` draws the toggle |
| `ProviderVisibilityCard` | the Settings provider list (order and show/hide) |
| `ReorderableRows` | drag-to-reorder rows with grip dots |
| `RailRow`, `RailIconButton` | a right-hand rail's tap row (selected/hover treatment) and its header icon button |
| `HoverTip` | the themed tooltip modifier |
| `MarkdownTextView` | the rich markdown editor (NSViewRepresentable) |
| Button styles | `primaryButton`, `secondaryButton`, `iconButton`, `plainButton`, `AccentSwitchStyle`, `SegmentActionStyle` |
| `focusRing`, `sweField`, `sheetScaffold` | modifiers: keyboard ring, themed field, sheet frame |
| `FlowLayout` | left-to-right wrapping layout |

## Conventions

- Every list that can grow long folds with `RowFold` + `ExpandRow`; each
  view picks its limit as `private static let foldLimit`.
- Every harness or provider shown by name gets a `HarnessIcon` or
  `HarnessMark` beside it.
- Status and empty states render inside `EmptyCard` / `LoadingCard` /
  `ErrorCard`, never as loose text.
- User-facing strings use Theme text roles and contain no em dashes.
- Page actions sit in `Page`'s actions with ⌘R on refresh.
- A view showing something a terminal changes under `~/.swe` follows it
  while on screen: a `.task` looping over `FolderChanges.stream`, filtered
  to the paths that decide what it shows (`ChaptersModel.affectsCheck`,
  `HubSyncStatus.affects`). Never let `data/`, `cache/` or `.git/index`
  through: the app's own reads write them, and the view reloads in a loop.
- Decoding `swe` output happens only in `SWECore` decoders, matched to
  fixtures under `app/mac/Tests/SWECoreTests/Fixtures` (see
  [json-contracts.md](json-contracts.md)).
- Persistent stores, caches and UserDefaults keys are inventoried in
  `docs/data-model.md`.

## Gotchas

- **Sidebar clipping is not hit testing** (`SidebarListFrame` in `app/mac/Sources/SWEApp/Navigation/MainWindow.swift`): the rail keeps its scroll content at expanded width to preserve icon positions. `.clipped()` alone leaves that overflow intercepting page clicks ([#1892](https://github.com/starship-terminal/swe/issues/1892): the AGENTS.md Edit tab); the modifier adds `.contentShape(.rect)` at the visible list frame. Keep the title-bar toggle outside that frame because it extends beyond the rail, and never give a background `NSView` (`TitleBarReader`) a hit area. The visible width comes from `SidebarExpansion.visibleWidth`, which `SidebarExpansionTests` pins.

Non-obvious decisions and traps in `app/mac/`, recorded so they are not re-learned. Read the entry for a file before changing it. The Python side's entries are in [backend-architecture.md](backend-architecture.md#gotchas).

- **FSEvents paths are real paths** (`app/mac/Sources/SWECore/Support/FolderChanges.swift`): events name `/private/var/...` and a linked folder's target, but `URL.resolvingSymlinksInPath()` strips `/private`, so a prefix built from it matches nothing and the watch stays silent with no error. The root goes through `realpath(3)` instead.

- **Accounts history**: usage history on the Accounts page is one switch for the whole page ("Show history", off by default, `accountsShowHistory`), and every card gets the same `AccountHistorySection`, so cards stay alike. SWE's own figures come from `AccountHistory` (one ledger `dailyTokens(for:)` read for every shown card, rebuilt after scans only while the page shows history; keys from `AccountsModel.historyKey`: a profile card sums its own home, a Cursor profile its app data folder, and a harness's own card every other home). Codex alone adds OpenAI's account-wide total (`CodexAccountUsage`); never give one vendor a section the others lack.
- **Grok Bot in Calendar**: its Bots pass one task between them, so every `grok-bot` session groups into one project, "Grok Bot" (`ProjectResolver.group`), and Bots active within the calendar gap merge into one block. `GrokBotRoster` only titles each session row with its Bot's name; never split blocks by Bot again. Deleting a Bot removes its name from Grok Bot, so `GrokBotRoster` remembers every id and name it reads in `~/.swe/data/grok-bot-names.json` (only the `shared` instance writes it; tests pass a temp `memory:`). An id in neither, while the roster parsed, titles "Deleted Bot (<short id>)".
- **Model brands** (`app/mac/Sources/SWECore/Models/ModelBrands.swift`): every model lab and API provider's display name, chart colour, logo, keywords (Artificial Analysis creator slugs, key-file headings) and model-name patterns live here and nowhere else. `ModelVendor` (Models page), `ModelProvider` (Pricing) and `APIKeyProviderLogo` (API keys) are thin reads of it, so a brand or palette change reaches every page at once. Never add a brand colour, logo id or name match in a view or another table. Every view that names a model draws `BrandLogo` (`app/mac/Sources/SWEApp/Components/BrandLogo.swift`) before the name; harness logos, where shown, say which tool ran it and come after. Anthropic's colour is deliberately Claude Code's harness colour, so the two always match. Harness names live in one table, `HarnessMark.names` (`app/mac/Sources/SWECore/Swe/HarnessIcons.swift`), read through `HarnessMark(id).name` or `UsageFormat.harness`; an account or provider reads `HarnessMark.accountName` (a Claude account is "Claude"). Names always start with a capital letter (`HarnessNamesTests`). Harness colours (Claude Code, Codex, …) are a separate palette with one source too: `HarnessColor.fixed` (`app/mac/Sources/SWECore/Swe/HarnessColor.swift`), read through `HarnessColor.of(id)` or `Theme.harness(id)` by the Overview marble pile, menu bar donut, Calendar, Compute and widgets. A harness outside the table takes its colour logo's hue. swe's `primary_color` is decoded but not used for colour, since it gave Calendar and Compute a second colour for the same harness; change the table instead.
- **Hook run counts** (`app/mac/Sources/SWECore/Primitives/HookStats.swift`, `HooksPage.swift`): the Hooks page reads `swe hooks stats --since=<window> --sum-harnesses --json` (the timeline's numbers: one row per script, every harness and every name its runs were logged under folded in by swe before it caps the reasons at five, so the app adds nothing up) and `swe hooks log --source=<path>` (the side panel's latest runs, one ask per script) through `HookStatsModel` (`AppModel.hookStats`), per window, off the main actor, on page load, a picker change and the Refresh button, never on a timer (the log changes with every hook run, so a poll would spawn a child all day). A failed run leaves the last numbers up, or dashes when there were none: no banner, since an swe from before `hooks stats` must not break the page. Which runs are a script's is swe's call (`src/swe/hooks/identity.py`, shared with the web page and the CLI): `HookStatsTable` joins a stats row's `source_paths` to `Hook.source` and nothing else, so every inventory row that shares a source (a shared script's per-harness rows) reads the same one row. A source in the report's `uncertain` (a run that may be its own and swe could not tell from another script's) shows dashes, and so does every script when the stats carry no `source_paths` (an swe from before the join): never a count that may be short or another hook's. Never match by name, stem, list name or a scan of a directory hook's folder. A rate that rounds to 0% with blocks reads `<1%`.
- **Devin cloud sessions** (`app/mac/Sources/SWECore/Sessions/DevinCloud.swift`): read from Devin's REST API v3 with the Settings › Services token, into `~/.swe/data/devin-cloud/sessions.json` (`SWE_DEVIN_DATA_DIR` overrides). Each sync merges: a session the API stops listing (deleted or archived on Devin's side) stays in the store, and only a change of token user or org starts it over. Only the token's own user's sessions are imported (`user_ids=<GET /v3/self user_id>`): an org also lists teammates' sessions and automation runs, which on one real org were 50 of 953 and 903 of 953. They use their own harness id, `devin-cloud`, and stay out of the usage ledger: the API has no tokens and `acus_consumed` is 0 on plans that don't meter per session, and zero-token ledger events would count as responses and trip "Hide accidental sessions". `updated_at` includes the sleep after a session goes idle (one read as 5 days), so durations and calendar blocks come from message times, read again only when a session's `updated_at` moves. Message text is never stored.
- **Claude desktop side sessions** (`app/mac/Sources/SWECore/Sessions/ClaudeDesktopSessions.swift`, `SessionFolders.swift`): the desktop app keeps one record per session at `~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json` (100KB to 1MB each, mostly app state). A session started from another carries `spawnedFrom.sessionId` (the parent's `local_` id; `userFork: true` for a user fork), and `cliSessionId` / `priorCliSessionIds` are the transcript ids the ledger and swe use. SWE keeps only those fields, cached per file by mtime in `cache/claude-desktop-sessions.json`. A side session also logs a few events from `side-session-notes/local_<parent id>` while writing its report, so a session's or calendar block's folder is the one most of its events used, and a notes folder only when nothing else is known.
- **Model catalog database** (`app/mac/Sources/SWECore/Catalog/CatalogDatabase.swift`): LiteLLM prices, Artificial Analysis models and feeds, the join between them, and a row per fetch live in `~/.swe/data/catalog/catalog.sqlite` (`SWE_CATALOG_DB` overrides; a build with only `SWE_AA_DATA_DIR` set keeps it in that directory). It is data, not cache: `prices` holds every price a key ever had, a new row only when a price field changes (not when LiteLLM edits a context window), so the ledger charges each event the rate its entry had at that time. Names still resolve through today's list; only the rates come from history, and a time before a key's first row takes its first price so usage from before the first download is priced. The ledger splits its groups only at price changes of entries its own models resolve to, inside the query range, so a list with hundreds of changes still adds a handful of `ts >=` terms at most. `model_aliases` is derived: `rebuildAliases` rewrites it from the matcher and `config/model-links.json`, which stays the source of truth for manual links. The old `cache/pricing/litellm.json` and `data/artificial-analysis/` are imported once (dated by the file's mtime and `meta.json`) and left in place.
- **Keychain reads that must not prompt** (`app/mac/Sources/SWECore/Usage/UsageProduction.swift:LegacyKeychainPrompts`): SWE's items are in the legacy login keychain, because the Data Protection keychain needs a `keychain-access-groups` entitlement the builds lack (`SecItemAdd` answers -34018). On a legacy item, `kSecUseAuthenticationUIFail` and an `LAContext` with `interactionNotAllowed` are both ignored: a read of an item SWE is not trusted on still puts up the password dialog and blocks. Only the process-wide `SecKeychainSetUserInteractionAllowed(false)` makes it fail at once (-25293). Never probe this by hand on a desktop someone is using: the dialog takes keyboard focus and can swallow whatever they are typing, and it lingers a few seconds after the probe is killed.
- **The app ignores `$HOME`** (`app/mac/`): `FileManager.default.homeDirectoryForCurrentUser` reads the account record, so `HOME=$(mktemp -d)` (or `open --env HOME=...`) does not move a running SWE.app off the real `~/.swe`; it moves only the `swe` processes it spawns. Point data at scratch with the per-store env overrides (`SWE_SERVICES_KEYCHAIN`, `SWE_AA_DATA_DIR`, `SWE_DEVIN_DATA_DIR`), or check new code through a test instead of a full app run.
- **LaunchServices and scratch builds** (`app/mac/`): launching an app bundle from a scratch folder (a /tmp build, a copy in the Trash) registers it with LaunchServices under the same bundle id. Stale registrations make WidgetKit fail with `bundleStubNotSupported` ("Bundle version did not match"), and desktop widgets stay as greyed placeholders. After testing a scratch build, run `/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -u <app>` on it, and after replacing the installed app, unregister the old copy too.
- **Every font goes through `Theme`** (`Theme.font(size:weight:design:)` and the type roles in `app/mac/Sources/SWEApp/Theme/Theme.swift`): they are Geist (Geist Mono for `.monospaced`), bundled in SWECore and registered on first use by `AppFonts`, and they scale by View › Zoom (`TextZoom` in SWECore, `TextZoomModel` in the app). A `.system(size:)` or text style (`.caption`) written in a view keeps its size at every zoom, and `TextZoomFontTests` fails on one. An `NSFont` that draws or measures text beside a SwiftUI font comes from `Theme.nsFont(size:weight:monospaced:monospacedDigits:)` (or `AppFonts.nsFont` at an explicit zoom), never `NSFont.systemFont`, so its widths match, and a font cached in a `static let` never sees a zoom change, so make it a computed `static var`.
- **No custom hover pop-ups** (`app/mac/Sources/SWEApp/Components/HoverTip.swift`): the app used to open a popover after a 150 ms rest and pin it on click. The popover covered the control it explained and the modifier's tap ate the click meant for it — a row's checkbox spent its first press pinning the tip (the Order list's per-harness checkbox was fully dead). They were removed wholesale. `.hoverTip(text, title:)` survives only to hand the text to VoiceOver — a label for a mark with none, a hint otherwise — and its `pinsOnClick`/`enabled` parameters do nothing; a SwiftUI `.help` is still fine for a plain hint since the native tooltip never touches clicks. `HoverTipClickTests` asserts the click reaches the button and no window opens. Don't bring the pop-ups back.
- **The window shell never measures a page** (`PanelFrame` in `app/mac/Sources/SWEApp/Navigation/MainWindow.swift`): the root `HStack` ranks its children by measuring them at zero, infinity and the real width, and the window's size constraints measure the root again. A page in a `.frame(maxWidth: .infinity)` still answers those, and a scrolling page answers by laying out all of its content, so every frame of any animation that moved geometry laid out the whole page 3 or 4 times (on Overview, about 90% of the main thread). `PanelFrame` returns the offered size without asking the page. To profile an animation, `sample <pid> 6` a release build and compare main-thread samples in `mach_msg2_trap` (idle) with those under `NSHostingView.layout()`; check `NSWindow.occlusionState` while sampling, since a locked screen, a full-screen Space or a stray screenshot overlay stops drawing and skews the numbers.
- **No filesystem calls on the main actor for Plugins and Skills** (`LatestBuild` in `app/mac/Sources/SWECore/Support/LatestBuild.swift`): `PluginExploreIndex` and `SkillPlacement` call `realpath(2)` and `fileExists` per plugin and skill folder, and a folder a file provider or a stale mount holds can block those indefinitely; on a fresh Mac that beachballed the app on Plugins > Explore ([#580](https://github.com/starship-terminal/swe/issues/580)). `PluginCatalogModel` and `SkillsModel` build them through `LatestBuild`, which runs the work on a GCD queue (not the concurrency pool, which a blocked call would starve) and publishes only the newest start. The marketplaces a user may rename are worked out in the index too, never in a view body. The plugin and skill detail pages do the same through `offMain` (same file): `PluginFolders` / `SkillFolders` in a `.task(id:)` that drops a cancelled result, and `PartsCard` gets `PrimitiveTree.pathsInside` with its tree walk, so no `PrimitiveOwnership` call runs in a view body. Tests that set `skills` or `plugins` directly await `indexed()` / `placed()` before checking.
- **Slow ledger reads stay off the `Ledger` actor** (`readGroups` in `app/mac/Sources/SWECore/Ledger/Ledger.swift`): the Overview's figure queries and the Accounts history take seconds on a large ledger. On the actor each one ran between two scanned files, so a launch scan that skips its unchanged files in about a second sat at "1 of 3,700 files" for 10 seconds and the figures took 26. They now run on pooled read-only connections (a WAL reader never waits on the scan's writer), side by side. A new query that can take more than a few milliseconds belongs there too, and a test that drops the ledger's `TempDir` early breaks them, since each opens the file by path. `OverviewModel` also runs full scans one at a time.
- **Timer scans rebuild only what changed** (`backgroundScan` in `app/mac/Sources/SWECore/Ledger/OverviewModel.swift`): the search index's 60 s reconcile and `keepFresh`'s 300 s sweep used to run the full `load(scan:)`, which re-read every figure, warmed all seven periods and bumped `completedScans` on every tick. With agents writing transcripts all day that was about 10 s at 200-400% CPU each minute ([#1542](https://github.com/starship-terminal/swe/issues/1542)). A timer scan that read nothing now queries nothing and leaves `completedScans` alone; one that read usage rebuilds the spend, the last 30 days (merged into the heatmap's rows) and the selected period only while `pageVisible`, and the page's `refreshForPage()` brings back the rest. `load` stays the path for launch and the refresh buttons. Only a scan that wrote usage events counts as new usage (`ScanReport.eventsWritten`, counted by `LedgerStore.upsert` and `delete(key:)`): most appended transcript lines carry no usage, and counting every file read woke every consumer on nearly every tick ([#1568](https://github.com/starship-terminal/swe/issues/1568)). A new ledger writer must count its writes there too. A new view that shows ledger figures must reload on `completedScans` (and `ledgerWrites`), never on a timer of its own, and a new timer must call `backgroundScan`, not `load`.
- **Background `swe` runs hold no activity** (`CommandActivity` in `app/mac/Sources/SWECore/Support/CommandRunner.swift`): `ProcessRunner` holds `.userInitiated` (no App Nap, no idle system sleep) for each run, which is right for a button and wrong for a poll that spawns a child every few seconds ([#1540](https://github.com/starship-terminal/swe/issues/1540)). A timer, watcher or page poll runs inside `CommandActivity.background { ... }`, wrapped where its `Task` starts (`AppModel.start()`, a page's `.task`); the flag is task-local, so tasks the loop starts inherit it. A new poller gets the same wrap.
- **The compute trend sampler runs on its consumers** (`trendPlan` in `app/mac/Sources/SWECore/Compute/ComputeModel.swift`): it used to sweep every process, the GPU and the SMC at the chosen interval for the app's whole life ([#1539](https://github.com/starship-terminal/swe/issues/1539)). Full rate runs only while a view registered with `.computeTrendsLive(model)` is on screen (`.onScreenChange` in `Components/OnScreen.swift`: in the hierarchy and its window visible, which `onAppear` alone does not tell for a minimised window or the ordered-out menu bar panel). With none on screen it reads only what auto clean, the fan threshold (`watchesTemperature`) and a placed Compute widget (`widgetPlaced`) need, every `backgroundInterval`, and parks when none is on. A new view that draws `compute.history` must add `.computeTrendsLive`, or it shows "Collecting" with the windows closed.
- **Devin search reads continue a shared mark** (`app/mac/Sources/SWECore/Search/SearchDevin.swift`, `indexDevinSessions` in `Ledger/Ledger.swift`): a grown Devin session reads only the chain past the `rowid` watermark in its `devin_chain:<path>` meta row, which swe writes too, so both sides must parse, write and check it the same way ([#1613](https://github.com/starship-terminal/swe/issues/1613)). The rules are in the "Two writers, one search index" entry in [backend-architecture.md](backend-architecture.md#gotchas), and `tests/fixtures/search/devin-chain.json` is the case both suites replay. Never walk a session's chain by loading its whole `message_nodes` parent map: on a 52,000-node session that took 1 to 2.3 s for a 260-node chain.
- **A managed shell rc resolves into a read-only store** (`app/mac/Sources/SWECore/APIKeys/ShellStartup.swift`): `ShellStartupWriter` writes through a symlinked rc to the real file by design, but a home-manager `~/.zshrc` resolves into `/nix/store`, which is immutable, so `writable()` preflights the resolved path and its folder before the backup, and the page offers a copyable line instead of the Add button when it fails (`Sourcing.notWritable`). `sources()` follows the rc's sourced files, `for`-loop variables and `*?[]`/`**` globs on disk (bounded: `maxSourcedDepth`/`maxSourcedReads`/`maxSourcedBytes`/`maxGlobMatches`), since a drop-in folder like `~/.config/zshrc.d` is where home-manager users put editable lines; never treat a glob or `$var` argument as a literal path again.

### Plugin inventory rendering

The management table uses `PluginExploreIndex.localEntries`: only SWE source folders and installed/cache folders that exist. Folder checks, ownership, per-row actions and the deduplicated local table are computed in the background index build. The table uses `LazyVStack` with stable plugin ids, avoiding eager creation of every expanded row and its menus. Do not move filesystem checks or catalog-wide action scans into a row body. Marketplace-only discovery remains in `swe plugins catalog`; unmanaged local rows offer Import to SWE for owning/editing source files. Existing installs can still be enabled, disabled and uninstalled before import; installation from a source requires SWE ownership and a registered SWE marketplace. Row secondary clicks use `ActionContextMenu` and `SecondaryClickRouter` (one shared event monitor with weak, visible-row targets; ordinary primary clicks pass through), reusing `SelectOptionList` styles and keyboard navigation through `ActionOptionList`. Removal is destructive and confirmed through the existing plugin action runner.

Cursor plugin actions honor each state's `allowed_actions` mask. `control_detail` explains configured/pinned or team restrictions in cell help. Cursor confirmations show the actual `swe plugins ... --harness=cursor` API-backed command; result banners explain refocusing Cursor or starting a new CLI session.

Downloaded Cursor caches remain local inventory and import sources even when authoritative account state says `installed: false`. Keep those two facts separate: cache presence does not authorize a harness action. Cursor cell restrictions use `.help` for mouse hints and an accessibility hint; `.hoverTip` alone is not visible.
