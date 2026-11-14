# SWE Remote roadmap

The phone client for remote control: watch every agent session on your
machines, unblock the ones waiting on you, and steer the ones you
attached. The hub, protocol and milestones R1 to R5 are in
[docs/remote-control.md](../../docs/remote-control.md). This file covers the
client: every page, component and feature of the redesign, and the hub
work each one needs.

## Decisions so far

| Topic | Decision |
|---|---|
| Look | The TokenTracker style package (`systems/tokentracker` in design-systems), dark first, the same as the Mac app. Near-black page, `#171717` hairline cards, white primary buttons, green only for "active" and "on", amber for "needs you", red for errors and destructive actions, sentence case everywhere, a visible focus ring. Values come from its `tokens.json`. |
| Icon | `assets/logo-square.png`, the same art as the Mac app's icon |
| Client | Native SwiftUI on iPhone (`app/ios/`), decided 2026-10-23. The hub's web app at `/` is the client for browsers and other devices; it took the standalone PWA's `#pair=` and `#approve` fragments and the PWA is gone. See "SwiftUI or PWA" below. |
| Signing | A paid Apple Developer team, so profiles last a year and APNs, TestFlight, widgets and Live Activities are all available |
| Push | ntfy stays the push path until the APNs work under "Features" lands |
| Shared code | Protocol DTOs only. Decoding stays on the hub; most of SWECore is AppKit-bound. |

## SwiftUI or PWA

| | SwiftUI app | PWA in a WKWebView |
|---|---|---|
| Feel | Real navigation stack, swipe back, collapsing large titles, swipe actions, context menus, haptics, pull to refresh | Imitations of each, which never feel quite right in a webview |
| Composer and keyboard | Native text field that stays above the keyboard, system dictation, a photo picker | Webview keyboard handling jumps and covers the composer |
| Long transcripts | `LazyVStack` renders only what is on screen | The DOM holds all 500 backfilled messages |
| Offline | SwiftData cache of sessions and recent messages, so the app opens instantly with the last known state | Service workers in WKWebView are flaky; the app is blank until the hub answers |
| Push | APNs with notification actions handled in the app (approve from the lock screen, open the right chat) | ntfy only; a tap opens the app, not the session |
| Widgets, Live Activities, Siri, Shortcuts | Share the app's Swift networking and models | Each extension needs that Swift code anyway, since extensions cannot run the webview |
| Face ID, Dynamic Type, VoiceOver | Built in | Partial; Dynamic Type does not reach the page |
| Rendering code and diffs | More work: SwiftUI's markdown has no code blocks, so they need a custom view | Already works (`md()`, `renderDiff()` in `app.js`) |
| Shipping a change | Rebuild and reinstall (or TestFlight) | The hub serves the new page on next load |
| Version drift | The app can lag the hub, so the protocol needs a version field and the app must handle a newer hub | None; the hub serves the client that matches it |
| Other devices | iPhone and iPad only | Android, any browser, and the `#approve` page on a Mac |

Decision: the iPhone client is SwiftUI. The PWA's "every other device"
role later moved to the web app (now at the hub's `/`; `/app/` redirects there), which took its `#pair=` and
`#approve` fragments and deleted the standalone client. The checkboxes
below track the SwiftUI app.

## Pages

Each page lists what it shows, then the hub work it needs. "Hub:" items
are server changes in `src/swe/remote/`.

### P1. Welcome
- [x] App icon, name and a one-line description, shown before any hub is paired
- [x] Primary "Pair with Tailscale" button, secondary "Scan pair QR" button
- [x] Hint line: on the hub, run `swe remote pair --app`
- [x] In-app QR scanner (VisionKit `DataScannerViewController`), so pairing no longer goes through the Camera app

### P2. Pair this device
- [ ] Device name field, prefilled with the phone's name
- [ ] Hub card: the hub's name and a status dot once it is found
- [x] "Pair with Tailscale" primary button
- [x] "Use a pair token instead" link that opens a token field
- [x] Errors in plain words: hub unreachable, not on the tailnet, rate limited
- [ ] Hub: a discovery route, or a tailnet lookup, so the app finds the hub without typing an address

### P3. Approve this device
- [x] The 6-digit code, large and monospaced
- [x] The approve command with the code filled in, tap to copy
- [x] Waiting card with a spinner and an expiry countdown
- [x] Expired, denied and "too many wrong codes" states, each with "Try again"
- [x] Cancel
- [x] Drive the countdown from `expires_in_s`, which `POST /v1/pair/request` already returns

### P4. Sessions
- [x] Large title, and a hub chip with a connection dot (green connected, grey reconnecting, red offline)
- [x] Filter chips with counts: All, Active, Needs you
- [x] Groups: machine, then project folder; finished sessions fold into an "Earlier" group
- [x] Session row: status dot, title, harness, status and age; an eye icon for observed and a link icon for attached
- [x] Unread dot after the title when the session moved since this phone last read it; opening the chat clears it and tells the hub (#1439)
- [x] Pull to refresh
- [x] Swipe to attach or open (Open sits beside attach/detach on a half swipe and is the only right-swipe action on a row that cannot attach)
- [x] Empty state: "No sessions on this machine yet"
- [x] Mirrored sessions (#1918): a "Mirrored" tag replaces the status and attached marks; swipes and the long-press menu offer Open, Pin and Rename only; the chat shows a read-only notice, the saved chat, a dimmed prompt box and a calm "not available yet" state; mirrored wins over attached
- [x] More than one machine: the hub chip's menu switches between paired hubs, and the new-session sheet picks the machine
- [x] New-session sheet: machine, folder, harness, first prompt
- [x] Hub: a route to start a session (machine, cwd, harness, prompt): `POST /v1/sessions` answers a `launch_id`, and `GET /v1/launches/<id>` names the session once it is adopted

### P5. Chat, observed
- [x] Header: title, harness and folder, and a menu (copy session id, interrupt)
- [ ] "Open on Mac" in that menu
- [x] Read-only transcript
- [x] Opens on the newest 100 messages; the next older page loads once you stop scrolling within 15 entries of the oldest one loaded, and the entry at the top of the screen stays put (a button on iOS 17)
- [x] The last 8 chats opened stay in memory, so going back into one shows it at once and replays only what changed
- [x] Attach card above the keyboard area: "Watching only", plus Attach when the session can be attached
- [x] Mid-turn and "this harness cannot be attached" wording on the card, as `updateOwnership()` has today

### P6. Chat, attached
- [x] Message bubbles: yours on the right, the agent's as plain text
- [x] Agent Markdown: headings, lists, quotes, rules, code blocks, and tables that scroll sideways inside their own box when wider than the screen
- [x] Task checkboxes, image and rendered-view chips, local file links shown as file names, MCP tool names as "server · tool"
- [x] Thinking card, collapsed to "Thought"
- [x] Thinking duration ("Thought for 12s") from the hub's `duration_s` (#1440)
- [x] Tool card: `Edit · app.js` with an added and removed line count, tap to expand
- [x] Diff view inside tool cards for edit and write tools
- [x] Runs of tool calls fold into "Used 6 tools"
- [x] Approval card: the prompt, then the pane's numbered menu rows when the hub read them (the row number is the decision), else Allow, Always and Deny; queued, resolved and failed states
- [x] Compaction divider
- [x] Composer: Stop and a round send button. No Queue/Steer segment: a send while the agent works steers in, or waits in the queued tray when the session holds sends, and a tray row's Send now steers it
- [x] Send states on your bubble: sending, queued (with the hub's reason), failed with retry
- [x] `/` command shortcuts and `@` file completion
- [x] Photo and file attach, landing as `@` refs on the machine (paperclip in the composer: Photos or Files, chips with upload progress; photos go up as JPEG, long edge 2048 px)
- [x] Hub: `@` file completion (`GET /v1/sessions/{key}/files`, hub-machine sessions only) and the `/` list (`GET /v1/sessions/{key}/commands`)

### P7. Inbox
- [x] Groups: Waiting on you (approvals and follow-ups), Errors, unread GitHub notifications, Finished today
- [x] Approval rows carry a Review button
- [ ] Approval rows show the prompt itself
- [x] Clear a row (swipe or long press) and "Clear" in the header, through `POST /v1/inbox/dismiss`
- [x] Swipe to dismiss a row
- [x] Tab badge counts only Waiting on you, not every row
- [x] Empty state: "Nothing needs you"
- [x] Hub: an age cutoff on `catalog.inbox()`: follow-ups, errors and interrupts older than 3 days drop out (it used to return every one ever seen, which is how the badge reached 278). Approvals never age out.
- [x] Hub: a dismiss route, stored per device, so a cleared item stays cleared (`POST /v1/inbox/dismiss`; new activity brings it back)
- [x] Hub: include finished sessions from today, marked as finished (`GET /v1/inbox?done=1`)
- [x] App: use the hub's cutoff, dismiss route and `?done=1` in place of `Staleness` and the on-phone `InboxDismissals`

### P8. Pair requests and devices
- [x] Pending requests: device, Tailscale node, login, age, a code field, Approve and Deny
- [x] Paired devices list: name, "This device", last seen, Remove
- [x] Reached from the hub chip menu
- [ ] Opened straight from a "new pair request" push
- [x] Hub: `GET /v1/devices` and `POST /v1/devices/revoke` (the phone-facing twin of `swe remote devices` / `--revoke`)
- [x] Show `last_seen_at`, which the hub already updates on each authenticated request and `GET /v1/devices` returns

### P9. Machine
- [ ] Per-machine strip: name, online state, satellite version
- [ ] CPU, GPU and temperatures from SWECore's Compute samplers
- [ ] Hub: carry those samples from the satellite

### P10. Settings
- [ ] Hub address and connection test
- [x] Unpair this device
- [ ] Notification preferences: which events push
- [ ] Face ID lock on or off
- [ ] App and hub versions

### P11. Pull requests
- [x] The GitHub tab's Pull requests side (one tab with an Issues/Pull requests switch, so the tab bar stays at five), one section per pinned repo, filter chips with counts: All, Mine, Review requested
- [x] PR row: number, title, author, branch, check rollup, review decision, mergeable state, labels, age, draft badge
- [x] Repo section keeps its `status`/`error` when its fetch fails while the others still list
- [x] Pull to refresh (`?refresh=1` bypasses the hub's `gh` cache); `env_blocked` shows the `gh` fix on an empty list
- [x] Detail page: the row plus rendered body (the chat's Markdown), per-check runs, latest reviews, review requests, changed files and +/- counts, merge state
- [x] Merge with a strategy (Squash, Rebase, merge commit) or arm auto-merge, behind a confirm that names the PR, base and strategy; a check row from an Actions run re-runs its failed or all jobs (the web app's `POST /v1/app/repositories/merge` and `…/rerun`)
- [x] "Open in session" sheet: harness picker from `/v1/sessions`' launchable set, prompt field prefilled with `suggested_prompt`
- [x] Hub: `GET /v1/pulls` (list + viewer flags), `GET /v1/pulls/<o>/<r>/<n>` (detail), `POST /v1/pulls/<o>/<r>/<n>/open {harness, prompt?}` → `launch_id`, followed on `GET /v1/launches/<id>`; the adopted session pushes its chat onto the pulls stack
- [ ] Mine/Review-requested push notifications once APNs lands

### P12. Issues
- [x] The GitHub tab's Issues side, one section per pinned repo, filter chips with counts: All, Mine
- [x] Issue row: state icon, number, title, author, comments count, labels (≤3), assignees (≤2), age; an "agent running" chip when the hub sends `agent_running` (absent field draws none)
- [x] Repo section keeps its `status`/`error` when its fetch fails while the others still list
- [x] Pull to refresh (`?refresh=1` bypasses the hub's `gh` cache); `env_blocked` shows the `gh` fix on an empty list
- [x] Detail page: the row plus rendered body (the chat's Markdown) and its comments — author, relative age, rendered markdown
- [x] Swipe Resolve and a context menu (Resolve with agent / Copy link / Open on GitHub / Share) on the row — Resolve opens the same sheet, never a blind launch; Copy link writes the `swe://issue/<o>/<r>/<n>` deep link
- [x] "Resolve with agent" sheet: "Starts in" card naming `repo · swe/issue-<n>`, the harness icon grid, prompt field prefilled with `suggested_prompt` — a row's Resolve has no detail yet, so the sheet fetches it for the prefill and a blank field still lets the hub build the brief
- [x] Hub: `GET /v1/issues` (list + `mine`, `?state=` picks the `gh` list state for the Closed chip), `GET /v1/issues/<o>/<r>/<n>` (detail), `POST /v1/issues/resolve {repo, number, harness, prompt?}` → `launch_id`, followed on `GET /v1/launches/<id>`; the adopted session pushes its chat onto the issues stack
- [x] Comments on the detail page
- [x] Comment box under the thread, posting through the hub's `gh` login (`POST /v1/issues/comment {repo, number, body}`); a refusal keeps the draft, a post reads the issue again
- [x] Resolve sheet: "Your context (optional)" (sent as `context`) and "Also post as a comment on #<n>", on by default as on the Mac; a refused comment is a warning and the launch goes ahead, and a retried Start doesn't post the same context twice
- [x] Closed-issue filter chip (fetches `?state=closed` on first pick)
- [x] Issues tab: list → detail (comments) → resolve sheet on the hub-served web app, the same loop browsers and Android get
- [ ] "Assigned to you" push notifications once APNs lands

### P13. Tasks

The work-items surface from #875: every source's open issues and pull
requests, joined to the agent sessions the hub links to them, on one
tab. `GET /v1/tasks` hands back the store rows: `state` derived by the
source (open → in progress → PR open → merged, or closed), the linked
session keys and pending launches, `agent_running`, `last_active_at`,
and per-source env blocks so a missing token explains an empty page.

- [x] Tasks tab between Inbox and GitHub: filter chips (All / Running /
  PR open / Done) with counts, repo-or-project grouped hairline cards,
  rows with a kind glyph + `o/r#5` ref + title + state label, a `●
  Agent` mark on `agent_running`, pull to refresh, and an amber
  blocked-source banner (failed repos and auth-required envs) that
  marks the rows below it stale
- [x] Task detail pushed on the tasks stack: ref + title header, a
  state stepper (pull tasks skip the PR rung, closed swaps the last
  rung), a Sessions card joining the task's keys to `GET /v1/sessions`
  rows that push their chats, the linked PR card pushing
  `PullDetailView`/`IssueDetailView`, "New agent session" and "Open on
  GitHub"
- [x] Row actions (`SessionAction.newSession`, amber like Resolve):
  Copy link writes the `swe://issue` deep link for GitHub issues,
  Open on GitHub, New session
- [x] "Link a session" sheet on the detail posts `POST
  /v1/sessions/<key>/task`; a non-GitHub task's launch sheet links the
  session it starts (a GitHub issue/pull launch's `origin` links on
  adoption)
- [x] Client: `tasks()`, `linkTask`, `unlinkTask` in `HubClient`;
  `TasksModel` filters, groups and marks stale rows
- [x] The Mac and web's Repositories rail and five-row groups (#941,
  #942): a scope menu over the chips (owners, then Linear and other
  sources, quiet scopes folded), chip counts within the pick, and
  "Show N more" past five rows a group
- [ ] Merge the same work filed in two sources into one task (the
  hub's keying job, when a second source is in real use)

## Components

- [x] Status dot: active, needs you, error, idle; the hub chip's dot for connected, connecting and offline
- [x] Session row, and grouped hairline card lists
- [x] Filter chip with a count
- [x] Hub chip with a connection dot and a menu
- [x] Tab bar with icons and a badge
- [x] Primary, secondary and destructive buttons at TokenTracker sizes
- [x] Text field and code field
- [x] Attach card
- [x] Composer: Stop and send (no Queue/Steer segment, see P6)
- [x] Message bubble with send states
- [x] Tool card and diff view
- [x] Approval card
- [x] Thinking card and compaction divider
- [x] Pair code display and waiting card
- [x] Device row
- [x] Empty, loading and error states for every list
- [x] Toast for failures that have no bubble to sit on (the Sessions list's row actions)

## Features

- [x] Live updates over SSE, resuming from the last seq after a reconnect or relaunch
- [ ] Offline cache: last sessions list and recent messages per session
- [ ] APNs push for needs you, approval, finished, error and limit events, with Approve and Deny actions on the lock screen
- [ ] Hub: an APNs sender alongside ntfy, and device token registration at pairing
- [ ] Live Activity for a running session: title, status, and an approval button when one is pending
- [x] Home screen widget: sessions waiting on you, sessions working, today's spend; rows open the chat (#1442). Lock screen widgets not yet
- [ ] Face ID gate on launch
- [x] Share sheet target: send text, a link, images or files to a session as a prompt (#1441)
- [ ] Siri and Shortcuts: "Approve the pending request", "What needs me"
- [x] Deep links: `swe://pair`, `swe://issue/<o>/<r>/<n>` and `swe://session/<key>?hub=<hub>` (links written as `quiver://` before the rename still open)
- [ ] Dictation in the composer
- [x] Search sessions from Sessions: transcripts through the hub's FTS index when present, titles and folders as text always (`GET /v1/search`)
- [ ] Search satellite transcripts (the index covers the hub machine only)
- [x] Usage and limits view from `swe list --usage --json`. Hub: `GET /v1/usage` serves those rows for the hub machine, and the web app shows them. The app's Usage tab adds tokens and cost from `GET /v1/app/overview` over the overview's seven periods, with quick figures, top models, the activity heatmap and the daily table
- [x] Hub: satellites report their own usage, so a MacBook's quota shows too (`POST /v1/agent/usage`, `machines` in `GET /v1/usage`)
- [ ] Diff viewer with commit and push through `swe gh`
- [ ] iPad layout: sessions list and chat side by side
- [ ] Protocol version check, with a "hub is newer, update the app" message
- [x] Browser client: the web app (at `/`, first shipped at `/app/`) replaced the standalone PWA outright, taking its `#pair=` and `#approve` fragments, so no restyle was needed
- [x] SSH console: a terminal on the hub's machine for when the hub is down, dialling sshd over the tailnet with a Secure Enclave key (SwiftNIO SSH + SwiftTerm), in the hub menu on Sessions
- [x] Hub: `GET /v1/ssh` (host keys to pin, login user) and `POST /v1/ssh/key`; `swe remote ssh --on` writes paired phones' keys into `authorized_keys`, and a revoke removes them
- [ ] Hub: satellites install the same keys and report their host keys, so the console reaches every machine
