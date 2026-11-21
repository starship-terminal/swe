# SWE for macOS

A native app on top of `swe`: usage, limits and sessions at a glance, and
accounts, skills, AGENTS.md and MCP servers without remembering commands.
See [ROADMAP.md](ROADMAP.md) for what is built and what is next.

## Build and run

Needs macOS 26 and Xcode 26 (Swift 6.2).

```bash
cd app/mac
swift test                      # SWECore unit tests
./script/test.sh                # the same, stopped with thread stacks if it hangs (CI uses this)
./script/build-app.sh debug     # writes build/SWE.app, widgets included
open build/SWE.app
```

The app runs `swe … --json`. `build-app.sh` puts a copy of `swe` inside
the bundle (`Contents/Resources/swe`, with a standalone Python 3.13 from
python-build-standalone; see `script/bundle-swe.sh`), so a Mac with no
Python or swe installed still works. The first build downloads that
Python (about 25 MB, pinned by checksum) into `~/.swe/cache/build/python`,
and builds `libcrypto.3.dylib` for the Droid limits decryptor there from
OpenSSL's pinned source (also checked by checksum; a few minutes, once per
architecture). `SWE_LIBCRYPTO` points the build at another
`libcrypto.3.dylib` instead; its `LICENSE.txt` must sit in that OpenSSL's
prefix or be named by `SWE_LIBCRYPTO_LICENSE`.
Settings › Files can point the app at another `swe` instead, such as an
editable install you're working on. `swift run` builds have no bundle and
use the `swe` on your login shell's PATH.

## Install

```bash
app/mac/script/install-app.sh       # release build of origin/main into ~/Applications
```

It fetches, builds `origin/main` in its own worktree under
`~/.swe/cache/install` (kept between runs, so rebuilds are incremental),
quits the installed SWE, swaps the new bundle in and relaunches it. It
never builds the checkout's current branch, so sessions working on other
branches can't leave a half-finished app installed. A failed build changes
nothing. `SWE_INSTALL_REF` builds another ref and `SWE_INSTALL_DIR`
installs somewhere else. It holds a `caffeinate -i` power assertion while
it runs, so the Mac doesn't idle to sleep mid-build. That covers sleep
only: to keep a build going after an SSH disconnect, start it under
`nohup` or in `tmux`.

## Set up a new Mac

```bash
app/mac/script/setup-machine.sh --dry-run   # print the plan, change nothing
app/mac/script/setup-machine.sh             # run it
```

Run it from the checkout you want to work in. Each step can run again
safely and says what it did; `--only=xcode,checkout` runs a subset.

1. **xcode**: checks that `xcode-select` points into an Xcode.app with
   SwiftUI's macro plugin. Command Line Tools alone fail the build minutes
   in, so it stops early and prints the `sudo xcode-select -s` line to run.
2. **checkout**: fetches and puts the checkout on `main` at `origin/main`.
   It never deletes a branch or discards work. A local `main` that has
   commits `origin/main` lacks is renamed to `main-stale-<date>`, a current
   branch with no history in common with `origin/main` gets the same kind
   of name, and a branch that shares history keeps its name. With
   uncommitted or untracked files, or a merge or rebase in progress, it
   stops and changes nothing.
3. **install**: runs `install-app.sh`, unless the installed app is already
   `origin/main`.
4. **hub**: runs `swe remote serve --install` (with the app's bundled
   `swe`), unless the hub's LaunchAgent is already loaded.
5. **app**: reports which SWE is running. It quits this checkout's own
   `app/mac/build` dev copy, leaves other builds alone, and opens the
   installed app.

## Package a DMG

```bash
SWE_CHANNEL=prod app/mac/script/build-app.sh release   # clean checkout, arm64
app/mac/script/make-dmg.sh                                # writes build/SWE-<version>.dmg
```

Prod builds need a Developer ID certificate (or `SWE_SIGN_IDENTITY=-` to
force ad-hoc) and refuse a dirty or tagless checkout. The DMG step notarizes
and staples the app and the image when `SWE_NOTARY_PROFILE` or the
`NOTARY_*` App Store Connect key are set; a Developer-ID-signed app without
either stops instead of writing a DMG Gatekeeper would reject. A
credential-free DMG for local testing needs an ad-hoc bundle:
`SWE_SIGN_IDENTITY=-` on the prod build, or a plain debug build.
The release job reads them from the repo's Actions secrets
(`MACOS_CERTIFICATE`, `MACOS_CERTIFICATE_PASSWORD`, `KEYCHAIN_PASSWORD`,
`NOTARY_KEY`, `NOTARY_KEY_ID`, `NOTARY_ISSUER`, `SPARKLE_PRIVATE_KEY`;
see `.github/workflows/release.yml` and [RELEASE_CHECKLIST.md](../../RELEASE_CHECKLIST.md)).

## Layout

| Path | What |
|---|---|
| `Sources/SWECore` | Everything testable: the `swe` client, models, view models |
| `Sources/SWECore/Ledger` | Usage ledger: harness log scanners, the deduplicated event store, pricing |
| `Sources/SWECore/Status` | Service status: the status-page catalog, Statuspage and Google Cloud parsers, the polling cache |
| `Sources/SWECore/Resources` | Trimmed LiteLLM price snapshot and SWE's pricing supplement |
| `Sources/SWECore/Limits` | Limit readings from every source, the 90% and reset alerts, the widget snapshot |
| `Sources/SWEApp` | The SwiftUI shell: window, sidebar, pages, menu bar panel |
| `Sources/SWEWidgets` | The WidgetKit extension: usage limits, usage summary, activity heatmap, top models |
| `Sources/SWEWidgetsUI` | The widgets' views and colours, shared by the extension and the app's widget previews |
| `Sources/SWEFanHelper` | The root launchd helper that holds a manual fan speed while SWE runs |
| `Tests/SWECoreTests` | Swift Testing suites; `Fixtures/` holds real `swe --json` output |
| `Tests/SWEAppUITests` | Tests of app-target code: views hosted in offscreen windows, page rules |
| `Tests/SWEWidgetsUITests` | The widget views' layout rules |
| `script/install-app.sh` | Builds `origin/main` and installs it as `~/Applications/SWE.app` |
| `script/setup-machine.sh` | Sets up a new dev Mac: Xcode check, checkout onto `origin/main`, install, hub, running app |
| `script/bundle-swe.sh` | Puts `swe` and its own Python into the bundle; `build-app.sh` runs it and signs the Python binaries |
| `script/build-app.sh` | Bundles `build/SWE.app` with the widget extension and signs both (Developer ID on `SWE_CHANNEL=prod`, Apple Development otherwise, else ad-hoc) |
| `script/make-dmg.sh` | Packages a built SWE.app into a DMG with a laid-out install window (app, arrow, /Applications link), signing, notarizing and stapling when `SWE_NOTARY_PROFILE` or the `NOTARY_*` key are set |
| `script/dmg-settings.py` | dmgbuild settings for that window: size, icon positions, background |
| `script/make-dmg-background.swift` | Draws the window's background at 1x or 2x; `make-dmg.sh` joins both into one HiDPI TIFF |
| `script/SWEWidgets.entitlements` | The extension's sandbox: read access to `~/.swe/cache/widgets/` only |
| `script/trim-litellm-pricing.py` | Regenerates `Resources/pricing.json` from LiteLLM's price list |
| `THIRD_PARTY_NOTICES.md` | Licences for the LiteLLM snapshot, ported code, logos, Sparkle and the bundled Python |
| `Licenses/` | Licence texts for the bundled Python's libraries; `script/bundle-licenses.sh` (run by `build-app.sh`) copies them, with Sparkle's, into `Contents/Resources/Licenses/` and fails if a listed text is missing |

Behaviour lives in SWECore behind seams the tests replace: `CommandRunning`
stands in for the `swe` process, `PricingTransport` for the network. The app
target stays thin.

## Usage ledger

`Ledger` (an actor) reads harness logs into a deduplicated SQLite store in the
directory you pass in (production: `~/.swe/data/ledger/`, chosen by
`LedgerLocation`, which moves a ledger left in the old `cache/ledger/` there at
launch). `keepBackedUp` keeps one copy a day in `backups/`, the newest 7. Sources:

| Harness | Source | One event per | Dedup key |
|---|---|---|---|
| Claude | `<home>/projects/**/*.jsonl`, every home in `LedgerRoots.claudeHomes` | API response | `message.id` + `requestId`; sidechain copies also collide on `message.id` |
| Codex | `<home>/sessions/**` and `archived_sessions/` | response (`token_usage_record`) or turn (`token_count`) | `response_id`, else session + timestamp |
| OpenCode | `opencode.db`, read-only | assistant message | message id |
| Devin | `sessions.db`, read-only | request | `metadata.request_id` |
| Pi | `<home>/agent/sessions/<project>/*.jsonl` | assistant message | file name + message id |
| Droid | `<home>/sessions/<project>/<uuid>.settings.json` | session (totals, re-read when rewritten) | session id |
| Cline | `<home>/data/sessions/<id>/<id>.json` | session (totals, re-read when rewritten) | session id |
| Cursor, Grok Bot | `swe harness usage-events cursor --json` (Cursor's usage feed) | usage event | account + time + model + conversation |

`scan(roots:)` reads only what changed: files resume from a per-file cursor
(inode, size, mtime, offset, head and tail hashes) and are re-read from the
start when replaced or truncated. Rows keep token counts, not dollars, and
outlive the logs they came from. `totals`, `daily`, `bySession`, `byAccount`
and `byHarness` price at query time with the current `PricingCatalog`; a model
the catalog does not know is counted in `unpricedTokens`, never as $0.

## Menu bar, notifications and widgets

The status item shows the tightest limit: a ring filled to the most used
current window across every account and every harness swe reports, plus
its percentage, red from 90%. Stale readings and windows whose reset time
has passed don't count. The panel lists every window of every account,
then the other harnesses, then the spend donut.

`LimitAlerter` notifies once when a window reaches 90%, and once when a
window that did so resets (a reading taken after its reset time, or a
reset time that moved on by more than ten minutes). A window that falls
below 80% without a reset rearms quietly. What it has notified is kept in
`~/.swe/cache/app-limit-alerts.json`, so a relaunch repeats nothing.

### How the widgets are built

SwiftPM has no app extension product, but a WidgetKit extension is only an
executable whose `@main` is a `WidgetBundle`, inside an `.appex` with an
`NSExtension` Info.plist. So `SWEWidgets` is a plain executable target
sharing SWECore, and `build-app.sh` wraps it in
`Contents/PlugIns/SWEWidgets.appex`. No Xcode project or XcodeGen spec.
An extension's `Bundle.main` is the .appex, so the script also copies
SWECore's resource bundle into the appex's own `Contents/Resources`,
where `CoreResources` finds the harness logos the widgets draw. A packaged
app or appex never falls back to SwiftPM's `Bundle.module`, which would
stop the process on any Mac but the one that built it.

Data goes one way, through a file. The app writes
`~/.swe/cache/widgets/snapshot.json` (usage figures, top models and
limit readings; never a credential, email or session) whenever what the
widgets show changes, then asks WidgetKit to reload. macOS only loads
sandboxed extensions, so the extension's entitlements grant read access to
that one folder through a home-relative temporary exception. That keeps
everything under `~/.swe`, and it means the app needs no App Group, so
an ad-hoc signed build works without a paid team: pluginkit registers the
extension and macOS launches it in its own sandbox container. A Mac App Store build
would need an App Group instead, since the store rejects temporary
exceptions.

## Known rough edges

- Privacy grants (Desktop folder access, Accessibility) stick across
  rebuilds only when the app is signed with a stable identity.
  `build-app.sh` uses your first "Apple Development" certificate, or
  `SWE_SIGN_IDENTITY`; `SWE_CHANNEL=prod` instead requires a
  "Developer ID Application" certificate and adds the hardened runtime
  and a secure timestamp. A dev build without a certificate falls back
  to ad-hoc signing, and every rebuild looks like a new app that asks
  again.
- `SWE_BUILD_DIR` overrides where the bundle lands (default
  `app/mac/build`); the app ends up at `$SWE_BUILD_DIR/SWE.app`.
  Settings > General > Updates uses this so a rebuild never overwrites the running
  copy.
- `SWE_CHANNEL=dev|prod` (default `dev`) stamps `SWEChannel` in the
  app's Info.plist. Dev builds also stamp `SWECommit`,
  `SWESourcePath` and `SWEDirty` from the checkout, which is what
  Settings > General > Updates compares against HEAD.
