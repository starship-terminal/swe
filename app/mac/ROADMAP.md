# swe app roadmap

A native macOS app on top of `swe`. You see usage, limits and sessions at a
glance, and manage accounts, skills, AGENTS.md and MCP servers without
remembering CLI commands.

## Decisions so far

| Topic | Decision |
|---|---|
| Location | `app/mac/` in this repo, SwiftPM package |
| Platform | macOS 26 only, native SwiftUI. Not a fork of TokenTracker (Node and React in a web view): its look and best features are ported instead. |
| Look | The TokenTracker style package in design-systems (`systems/tokentracker`); values come from its `tokens.json`. Borderless sidebar on the window wash, rounded content panel, hairline cards, sentence case everywhere (no all caps), green only for money and "on" states, and a visible focus ring. |
| Data that refreshes often | Native Swift: usage, cost, limits, account profiles |
| Data that writes harness config | `swe … --json`, so the tested Python code stays the only writer |
| Sidebar | Usage: Overview, Sessions, Calendar, Pricing. GitHub: Repositories, Tasks, Inbox. Manage: Accounts, Harnesses, Compute. Primitives: Plugins, Skills, AGENTS.md, MCP servers, Hooks, Models. System: Menu bar, Widgets, Service status, Settings |
| Accounts page | Limits merged with Switchboard-style profiles: each account card has its limit bars, when they were fetched, and Open app / Terminal buttons |
| Kept from TokenTracker | Sessions and Limits (the most important), menu bar and widgets, Skills (built on swe hub), Service Status (extended to more services), Settings, Tokens overview. Claude IP Check at low priority. |
| Out of scope | Leaderboard, Achievements, Pet. Cloud sync narrowed by M9: syncing app state stays out, remote access to sessions is in. |
| Other apps' data | None shared. SWE replaces Switchboard, TokenTracker, AIUsage and OpenUsage, which get deleted, so it never reads or writes their files, settings or caches, not even to import. |
| Profile store | Switchboard's model (one config home and one desktop data folder per profile, launched with the env var and `--user-data-dir`), kept under `~/.swe/accounts` |

## Borrowed code

| Source | License | What |
|---|---|---|
| OpenUsage | MIT | Menu bar `NSStatusItem` + key-capable `NSPanel`, provider fetchers, pricing snapshot, JSONL scanner. Not its name or icon (TRADEMARK.md). |
| AIUsage | Apache 2.0 | Per-file cursor ledger for fast incremental scans |
| TokenTracker | MIT | Design tokens, page layouts, and the harness logos it ships (most from LobeHub icons, MIT). The logos stay their owners' trademarks, used only to name each harness. |
| Switchboard | MIT | Profile directories, `open -n --env … --args --user-data-dir=…` launch, terminal launchers |

## Milestones

### M0. `swe` speaks JSON
- [x] `python -m quiver.mcp_server` starts the server ([#124](https://github.com/starship-terminal/swe/pull/124))
- [x] `--json` on `swe list --usage` (rate limits per harness, with `fetched_at`)
- [x] `--json` on `swe session` (id, harness, cwd, title, timestamp, status)
- [x] `--json` on `swe find skills` and `swe init --check` (link state per harness)
- [x] `--json` on `swe mcp list|status` and `swe doctor` (findings with fix commands)
- [x] One shared envelope: `{"schema": 1, "data": …}`, documented in ARCHITECTURE.md

### M1. App shell
- [x] `app/mac/` SwiftPM package, macOS 26, app target plus a testable core library
- [x] Main window: `NavigationSplitView` sidebar, glass background, empty pages
- [x] Menu bar item with a panel (OpenUsage pattern)
- [x] `SweClient`: runs `swe` with the login-shell PATH, decodes the M0 envelope
- [x] Doctor page as the first real consumer of `swe --json`
- [x] Settings: choose which `swe` to run, and a clear message when it predates `--json`
- [x] CI job on a macOS 26 runner: build and `swift test`

### M2. Accounts
- [x] Profile store and directory layout under `~/.swe/accounts`
- [x] Open a desktop app or terminal per profile (Claude, Codex)
- [x] Limit fetchers: Claude and Codex natively, per account; Devin, Cursor and Copilot through `swe list --usage --json`, which already polls them with its safeguards
- [x] Carry over the safeguards in `rate_limits.py`: honor provider cooldowns
  and `Retry-After`, keep the original `fetched_at` when reusing a reading,
  and expire stale readings instead of showing them as current
- [x] Account cards with bars, plan badge, reset time and "fetched N min ago"
- [x] Menu bar panel shows the same bars
- [x] Ask `swe` only for the harnesses SWE has no account for (`--exclude`, #131), so Claude and Codex are not polled twice

### M3. Overview
- [x] Incremental scanner for Claude and Codex JSONL, sqlite for OpenCode and Devin
  (`SWECore/Ledger`, persisted to `~/.swe/data/ledger/ledger.sqlite`)
- [x] Pricing from a bundled LiteLLM snapshot, refreshed daily before a scan
  (`PricingStore`, kept with every past price in `~/.swe/data/catalog/catalog.sqlite`)
- [x] Overview page: total tokens and cost, harness split, daily table, heatmap
- [x] Cost donut in the menu bar panel (Today, Yesterday, 30 days)

### Restyle
- [x] Shell, cards, controls and Overview after the TokenTracker style package ([#133](https://github.com/starship-terminal/swe/pull/133))

### M4. Sessions
Modelled on TokenTracker's Sessions page.
- [x] List from `swe session --json`, joined with M3 token and cost data by session id
- [x] Search plus tool, time, project and model filters
- [x] Summary strip: matching sessions, tokens, cost
- [x] Rows grouped by date, with models, duration and project on a second line
- [x] Resume action: opens the session in your terminal, in its folder and account
- [x] Session detail sheet
- [x] Chat from the local hub, as in the web app: live transcript ([#708](https://github.com/starship-terminal/swe/pull/708)); composer with Queue/Steer and Stop ([#710](https://github.com/starship-terminal/swe/pull/710)); Attach and approvals
- [x] Resolve a GitHub issue with an agent ([#816](https://github.com/starship-terminal/swe/issues/816)): issue rows open a detail sheet with comments and a composer (`swe gh issue view|comment`); Resolve starts a session through the hub (`POST /v1/issues/resolve`) in a `swe/issue-<n>` worktree, shows the hub's steps, then opens its chat in Sessions
- [x] Mirrored sessions ([#1918](https://github.com/starship-terminal/swe/issues/1918)): a "Mirrored" tag, no Resume, Attach or Fork, a read-only chat from swe's copy, and Restore transcript (`swe session restore --json`); the scan reads the copies for search (the ported copy rule) and for usage the ledger never recorded

### M5. Manage
- [x] Skills, built on swe hub: per-harness link chips, link and unlink through `swe skills` ([#144](https://github.com/starship-terminal/swe/pull/144))
- [x] AGENTS.md and CLAUDE.md, global and per project: edit in place, link status per harness ([#142](https://github.com/starship-terminal/swe/pull/142))
- [x] MCP servers: list by harness, diff, sync (`swe mcp sync --json`) ([#142](https://github.com/starship-terminal/swe/pull/142))
- [x] Plugins: drift findings with one-click fixes (`swe find plugins --fix=<id>`) ([#142](https://github.com/starship-terminal/swe/pull/142))
- [x] Plugins Explore: every plugin in every marketplace, install, uninstall, enable and disable per harness (`swe plugins`); plugin skills listed there, not on Skills
- [x] Harnesses: discover installed ones, star and archive them ([#144](https://github.com/starship-terminal/swe/pull/144)); install and uninstall steps open from Settings › Providers › Harnesses
- [x] Launch a session in the right folder with one click
- [x] API keys: the provider keys in `~/.api_keys`, masked until revealed, edited in place; in Settings › Providers › Inference beside the inference plans

### M6. Menu bar, widgets and shipping
- [x] Limits in the menu bar: the status item shows the tightest limit; the panel lists every account's windows and the other harnesses
- [x] WidgetKit widgets after TokenTracker's designs (`screenshots/widgets-dark-full.png`): usage summary, activity heatmap, top models, usage limits
- [x] Notifications when a limit resets or crosses 90%, once per crossing
- [x] Launch at login (`SMAppService.mainApp`, a switch in Settings)
- [x] Signing, notarization, Sparkle updates (the release items below)

### M7. Service status
- [x] Status of the services the harnesses depend on (Anthropic, OpenAI, GitHub, Cursor and more), from their public status pages
  (`SWECore/Status`: Statuspage feeds plus Google Cloud's for Gemini, cached at `~/.swe/cache/service-status.json`)
- [x] Incidents surfaced in the menu bar when a service is degraded

### M8. Download and first run (DMG)

Goal: someone downloads a DMG onto a Mac with no `~/.swe`, no `swe`, no
Python, no keys and no MCP servers. From there they reach a working setup,
and add or edit everything from the app, without a terminal. The audit
behind this list ran on 2026-10-19 and covered packaging, the `~/.swe`
layout, keys, the Manage pages, and the usage and system pages.

Phases in order. Phase 4 can run alongside 1 to 3.

**Phase 0. Quick fixes**
- [x] `swe` version probe tried `claude version` first, which starts a Claude session ([#370](https://github.com/starship-terminal/swe/pull/370))
- [x] Seeded `~/.swe/.gitignore` lists `secrets/` ([#369](https://github.com/starship-terminal/swe/pull/369))
- [x] A missing `swe` shows raw `env: swe: No such file or directory`. `ProcessRunner` runs `/usr/bin/env`, so exit 127 lands as `commandFailed`, never `.notInstalled` (`CommandRunner.swift`, `SweClient.swift`). Map 127 to `.notInstalled`, and replace its "Install swe with pip" text, since the PyPI name belongs to someone else. ([#408](https://github.com/starship-terminal/swe/pull/408))
- [x] `swe providers add` tells users `~/.api_keys/<name>` is a directory of files, but the app reads `~/.api_keys` as one file and fails on a directory. Make them agree. ([#408](https://github.com/starship-terminal/swe/pull/408))

**Phase 1. Bundle `swe` inside the app**
- [x] python-build-standalone CPython 3.13 plus `src/swe` (then `src/quiver`) at `Contents/Resources/swe`, used when Settings names no `swe` ([#371](https://github.com/starship-terminal/swe/pull/371))
- [x] Check Droid limits with the bundled Python. `rate_limits.py` loads `libcrypto` through ctypes and falls back to an unversioned dylib. ([#413](https://github.com/starship-terminal/swe/pull/413))

**Phase 2. First-run setup**
- [x] `swe setup --apply` seeds `~/.swe` and links AGENTS.md, skills and hooks. `--adopt=<file>` and `--starred=a,b` added ([#373](https://github.com/starship-terminal/swe/pull/373)).
- [x] Four-step setup window: agents, shared AGENTS.md, skills and MCP, look, notifications. Settings › General can open it again ([#375](https://github.com/starship-terminal/swe/pull/375)).
- [x] The links step clones your hub repo (`swe setup --hub=<url> --json`) while `~/.swe` has no hub yet ([#696](https://github.com/starship-terminal/swe/issues/696)).

**Phase 3. Add and edit from the app.** Every Manage page edits or syncs what exists, but almost none can create.
- [x] API keys: a provider picker built from `providers/defaults.py` and `APIKeyCheck.swift`, with a "Get a key" link instead of the free-text Provider and Variable fields. Test a key before saving. ([#397](https://github.com/starship-terminal/swe/pull/397))
- [x] API keys: offer to add `source ~/.api_keys` to `~/.zshrc` (back it up, ask first). Nothing does this today, so saved keys reach no new terminal, and the page subtitle claims they do. ([#400](https://github.com/starship-terminal/swe/pull/400))
- [x] MCP servers: an Add server sheet (stdio command, args and env, or http url and headers) and Remove. `swe mcp` has neither, and `swe mcp edit` needs `$EDITOR`. Needs `swe mcp add|remove --json`. ([#399](https://github.com/starship-terminal/swe/pull/399))
- [x] MCP secrets: `${NAME}` values live in `~/.swe/secrets/.api_keys`, which nothing writes. Add `swe mcp secret set NAME` (0600) and a paste field per unresolved name. `swe mcp sync` writes the literal `${NAME}` into harness configs when a value is missing; refuse instead. ([#398](https://github.com/starship-terminal/swe/pull/398))
- [x] Skills: New skill (writes `~/.swe/skills/<name>/SKILL.md`) and Link all. Needs `swe skills new --json`. ([#401](https://github.com/starship-terminal/swe/pull/401))
- [x] Plugins: Add marketplace (GitHub owner/repo or path) through `swe plugins marketplace add --json`. Open Explore first when the hub is empty. ([#402](https://github.com/starship-terminal/swe/pull/402))
- [x] Harnesses: a Detect and register button (`swe setup --apply --json`), and a form for a custom harness. ([#403](https://github.com/starship-terminal/swe/pull/403); harness discovery instead of `swe setup --apply`)
- [x] Accounts: Sign in opens a terminal that says "command not found" when the CLI isn't installed. Show Install there instead. ([#404](https://github.com/starship-terminal/swe/pull/404))
- [x] Empty states: `EmptyCard` is a label only, and several pages tell users to run a command. Give each a button. ([#407](https://github.com/starship-terminal/swe/pull/407))
- [x] Doctor: check for a missing `~/.swe`, a missing AGENTS.md, unlinked harnesses and an empty registry. Each finding gets a Run button. ([#405](https://github.com/starship-terminal/swe/pull/405))
- [x] Services: Sessions and Models say when a Devin or Artificial Analysis key is needed, and link to Settings › Services and the token page. ([#406](https://github.com/starship-terminal/swe/pull/406))

**Phase 4. Release pipeline**
- [x] Prod builds hide the channel picker and Rebuild ([#409](https://github.com/starship-terminal/swe/pull/409)). Releases build with `SWE_CHANNEL=prod` once the release job below exists.
- [x] Sign with Developer ID, hardened runtime and `--timestamp`, then notarize and staple ([#414](https://github.com/starship-terminal/swe/pull/414)). Prod signs Developer ID with runtime+timestamp; `SWE_NOTARY_PROFILE=quiver-notary` zips+submits+staples — a local prod build was notarized `Accepted` and passes `spctl` as Notarized Developer ID.
- [x] A macOS 26 release job that builds, signs, notarizes, makes the DMG and attaches it to the GitHub release ([#415](https://github.com/starship-terminal/swe/pull/415), [#417](https://github.com/starship-terminal/swe/pull/417)). Runs unsigned until the `MACOS_CERTIFICATE`, `NOTARY_*` and `SPARKLE_PRIVATE_KEY` secrets land. `script/make-dmg.sh` owns the post-build path (notarize the .app, stage, sign, notarize and staple the DMG) and runs locally too; prod builds refuse non-arm64 hosts, a dirty checkout and a missing version tag.
- [x] Version stamps come from `git describe`, which reads `0.0.0` on a shallow CI checkout. Fetch tags or pass the version in. ([#411](https://github.com/starship-terminal/swe/pull/411))
- [x] Universal build or Apple Silicon only: Apple Silicon only, decided. macOS 26 is the floor; release notes and the download page should say so. `build-app.sh` refuses a `SWE_CHANNEL=prod` build on other hosts, and release.yml compiles the release config and smoke-tests the bundled swe and the app binary on every tag (PRs run `swift test` only).
- [x] In-app updates (Sparkle and an appcast). Sparkle 2.10.0 bundled + signed ([#416](https://github.com/starship-terminal/swe/pull/416)); prod Info.plist carries SUFeedURL + SUPublicEDKey; the release job signs `appcast.xml` with `SPARKLE_PRIVATE_KEY` ([#417](https://github.com/starship-terminal/swe/pull/417)).
- [x] `NSAppleEventsUsageDescription` (Sign in, Resume and Run in Terminal use `osascript`). ([#412](https://github.com/starship-terminal/swe/pull/412))
- [x] Keychain writes pass the secret on `security`'s argv (`UsageProduction.swift`). Use stdin or the Security framework. ([#410](https://github.com/starship-terminal/swe/pull/410))
- [x] The release check calls the `starship-terminal/swe` releases API, so the repo has to be public. (the repo is public)

**Phase 5. Polish**
- [x] First ledger scan: show files done out of total, and partial results. A heavy user waits minutes behind a spinner.
- [ ] Explain the Desktop and Documents folder prompt before it appears (first Sessions or Calendar load).
- [ ] Don't start the Cursor, Devin cloud and Codex daily pollers for users without those harnesses.
- [ ] Fan helper: offer it only on Macs with fans, remove it when SWE is removed, and reinstall a stale helper binary after an update.
- [ ] Menu bar incidents: show only the providers this Mac uses.

### M9. Remote control

Watch and steer harness sessions from a phone, for every harness `swe`
reads — not just Claude. Design: [docs/remote-control.md](../../docs/remote-control.md).
Hub-and-spoke: the always-on machine runs `swe remote serve`, other
machines run a `swe remote agent` satellite that dials out to it, and the
phone talks only to the hub over Tailscale. Transcript tailing gives the
read path for every harness; a tiered inject layer gives the write path
(`opencode serve`, `codex app-server`, headless `claude -p --resume`,
tmux fallback). A session is `attached` (supervised, writable) or
`observed` (someone's terminal owns it, read-only) so two writers never
share one transcript.

- [x] R1: hub, byte-offset tailers, session catalog, SSE journal, web read-only chat
- [x] R2: claude headless-resume inject + tmux supervisor + Attach flow
- [x] R3: satellite daemon, machine registry, offline command queue
- [x] R4: native iOS app, Inbox of pending decisions, push via ntfy
- [x] R5: opencode serve + codex app-server adapters, diff pane, Queue/Steer

### Later
- [ ] Claude IP check (low priority)

## Open questions

- App name and icon.
