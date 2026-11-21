# G12: the timeline

Status: built. The first milestone with something to look at: the session view
([G10](G10-session-view-toolchain.md) built its frame, [G11](G11-client-core.md)
its data) now draws the conversation. The composer, the approvals dock and the
queued tray are G13's; the view is still behind `remote.session_view`, which is
off by default.

| File (under `app/web-session/src/`) | What it holds |
|---|---|
| `SessionShell.tsx` | makes the store over the hub (`fetchTransport`, the saved credential), starts it when the view mounts and stops it when it goes; an empty key shows "No session selected" and opens no connection |
| `session/SessionView.tsx` | header, connection banner, timeline, the composer's place; draws any store, whoever started it |
| `session/model.ts` | `buildRows`, `visibleSends`: the rules that turn `session.items` into rows (pure, no React) |
| `session/Timeline.tsx` | the scroll area: rows, follow-the-end, "Jump to latest", "Load earlier messages", entrance animation |
| `session/Bubbles.tsx` | user and assistant messages, `SendBubble` |
| `session/WorkGroup.tsx` | work groups, tool, thinking and checkpoint rows, open and closed state |
| `session/Rows.tsx` | live text, the working row, requests, notices, the turn footer |
| `session/Header.tsx`, `connection.ts` | breadcrumb, the connection dot, what each of the eight states says |
| `session/States.tsx` | loading, empty, unavailable |
| `session/ComposerPlaceholder.tsx` | G13's place (see below) |
| `session/credential.ts` | the saved credential, read as `app.js` reads it |
| `session/session.css` | the animated disclosure and the keyframes, all under `.swe-sv` |
| `ui/` | `Icon`, `StateArrow`, `Collapsible`, `Button`, `Spinner`, `Prose` and `markdown.ts` |
| `session/screens/` | the screens page: the view over recorded frames, and `shoot.mjs` that photographs it (dev only, not in the build) |
| `THIRD_PARTY_NOTICES.md` (in `app/web-session/`) | what is and is not copied from t3code |

`main.tsx` and `boot.ts` are as G10 left them. `SessionShell.tsx` and its test
are the two files outside `session/` and `ui/` that changed: G10 named
`SessionShell` the placeholder G12 replaces.

## What is drawn

`session.items` is one flat list in journal order. The timeline groups it:

| Row | From | Notes |
|---|---|---|
| User message | `message` item, role `user` | a quiet bubble on the right, with attachments as file chips |
| Reply | `message`, role `assistant` | prose, no bubble; Markdown through `ui/markdown.ts` |
| Work group | consecutive `thinking`, `tool` and `checkpoint` items | one line, "Worked for 2m 29s · 3 tool calls, 1 thought", and "1 failed" when a tool failed. While the turn is open and nothing was written after it, "Working" with a spinner and its steps open |
| Request | `request` item | the prompt and how it ended ("Allowed", "Denied", "Allowed automatically", "Expired before it was answered"...). One still open says "Waiting for your answer". The controls are the approvals dock's (G13) |
| Notice | `compacted`, `error` | a rule with "Context compacted from 120k tokens"; an alert box for an error |
| Live text | `session.live` | after the last items of its own turn; assistant text as prose with a caret, thinking as a muted box, a tool call being typed as "Preparing a tool call" |
| Working | none | a spinner row while a turn is open and the session is `running` but nothing is streaming and no group is open |
| Turn footer | a turn that ended | "1 file +1 −1" from the turn's tool `fileEdits`, counted once per path; for a turn that failed, was stopped or lost its keeper, why. A turn that changed nothing and ended well has none |
| Send | `sends` (`describeSends`) | see below |

**Work groups.** A group is the run of steps between two other rows. It is
closed once it is done and open while it runs, and a person's own toggle wins
and stays (`useToggles`, keyed by the group's first `seq`): a group does not
close under someone reading it. Closed, its steps are not made at all; opened
once, they stay so the close can animate. A tool opens for its input, its
progress while it runs, each file edit as a coloured diff (the `+` and `-`
lines counted) and its output, each cut at 6000 characters or 300 lines with a
note of how much is not shown. "Worked for" is the span from the group's first
step to its last step's end (`completedAt`).

**Streaming.** The core applies deltas by id and offset; the view draws
`session.live` and does nothing with offsets. A hole shows "Catching up with the
rest of this reply." under the text until the snapshot's `live[]` replaces it.
The reply's live row and its message share an identity, so a reply that
finishes does not enter a second time.

**Sends.** Each unfinished send is a `SendView` from `describeSends`, drawn as a
bubble with the selector's `reason` as its one sentence and a state arrow
(AGENTS.md's mapping: yellow for waiting and sent, a spinner for sending, red
for failed, grey for in doubt and unconfirmed). Discard exists only when
`canDiscard` is true and Retry only when `canRetry` is true, so while the outbox
loads, or a post is out, neither is offered: the view reads the flags and
decides nothing. Retry calls `retrySend` (a new send under a new id), Discard
calls `discardSend`.

**A delivered message shows once.** The hub echoes the command id a send was
posted under as the `command_id` of the user's `message.completed` (the keeper
path stores it; the legacy path's `client_id` is served under `command_id`, and
the core posts both, equal). `visibleSends` hides any send whose `commandId`
equals the `commandId` of a user message already in the timeline. The outbox
entry itself stays until its `command.result` (G11's invariant); the view only
stops drawing it. A send the hub queued but has not yet started is still drawn,
as "Queued: it will be sent when the current turn ends.": it has no timeline
message yet, and the queued tray that would show it is G13's. A steer ends its
queued entry with a `cancelled` result and its text arrives as a new user
message under a new command id, so the cancelled entry leaves with its result.

**Connection.** The header carries the state arrow and a word ("Live",
"Catching up", "Reconnecting, retrying in 4s", "Offline", "Hub not answering",
"Signed out", "Session gone", "Hub too old"). Under it, once the session has
loaded, a banner says the one sentence for each state that needs it; before the
first load the timeline's own state says it instead (a skeleton while
connecting, "Cannot load this session yet", "Session gone", and so on), so
nothing is said twice. What was loaded stays on screen when the connection
drops.

**Appearance, motion, tokens.** Only tokens: `pnpm lint:tokens` is clean, with
no arbitrary values. The quiver state arrow's shape is inlined with
`currentColor` and a token class, so it follows System, Dark and Light (a
colour a shade off the `state-<colour>.svg` files, which are fixed fills).
Every transition and animation (the disclosure, the chevron, a new row, the
spinner, the pulse, the caret, the banner) sits inside
`@media (prefers-reduced-motion: no-preference)`; `motion.test.ts` fails on one
outside it and on a rule not under `.swe-sv`. No ALL CAPS, and the mono font
only on `pre` and `code`.

**Markdown.** `ui/markdown.ts` reads a reply into plain data (paragraphs,
headings, lists with nesting, quotes, fenced code, tables, rules; code, bold,
italic and links in a line) and `Prose` draws it with React elements, so reply
text never becomes HTML. A link is an anchor only for `http`, `https` and
`mailto` (`rel="noopener noreferrer"`); anything else leaves its words. An open
code fence runs to the end, so a reply still being written draws sensibly.
`react-markdown` (named in G1) was not added: nothing else needed its
dependency tree.

**The composer's place.** `ComposerPlaceholder` is the disabled box G10 drew,
marked `data-placeholder="composer"` and commented as G13's. Its one live part
is the outbox's `note`, which G11 asks a view to show beside the composer when
storage does not survive a reload. It shows the read-only reason as its
placeholder when the row says so. G13 replaces the file.

## Decisions

- **Grouping lives in `session/model.ts`, pure.** The tests replay the recorded
  fixtures through the core's reducer and assert the rows, so a grouping
  change fails there, not in a screenshot.
- **The view selects slices.** `Timeline` reads `items`, `live`, `turns`,
  `stopped`, `openTurn`, `phase`, `sends` one by one, so a ping redraws nothing
  (G11's advice).
- **Credential.** The standalone page reads `localStorage["swe.remote"]` as
  the web app does and sends `X-Swe-Pair: 1`; inside the Mac and iPhone shells
  there is none and the scheme handler adds the bearer.
- **No Restore, no changes panel, no plan or subagent rows.** The turn footer
  has the file count and lines; "Restore to before this turn", the changes
  panel and checkpoints are G19, plans and subagent rows G20, device captions
  ("Allowed on iPhone") G21. The checkpoint rows inside a work group are plain
  lines.
- **Nothing copied from t3code.** See `THIRD_PARTY_NOTICES.md`.

## Limits

1. **No virtualisation.** Every row is in the DOM, and the reducer never trims
   (G11 Limit 3), so a very long session costs layout time. Closed work groups
   draw only their headers, which is most of the weight. `@legendapp/list` (G1)
   is the way out when it matters.
2. **Browser tests are jsdom tests.** G1 names Playwright; the toolchain has
   Vitest and jsdom, and adding a browser runner to CI was not this
   milestone's. The DOM tests run the real store over the recorded fixtures.
   The look was checked in Chrome through `session/screens/shoot.mjs` (a
   manual step, not a CI job). Scroll position and the animations are only
   checked there.
3. **Global class names.** Tailwind's utilities are global (G10's note for
   G14). This stylesheet emits among others `block`, `grid`, `hidden`, `flex`,
   `relative`, `table`, `underline` and `truncate`; the web app already uses
   `class="table"`. G14 must scope or prefix before it embeds the view.
4. **`inert` and `content-visibility`.** A closed panel is `inert`: browsers
   older than Safari 15.5, Chrome 102 and Firefox 112 leave its controls in the
   tab order. Rows do not use `content-visibility`.
5. **Markdown is a reader for what replies use.** No raw HTML (shown as text),
   no images, no footnotes, no task-list boxes; a table is read only with its
   separator row.
6. **A running tool's `text` is the latest `tool.updated` only**, as the core
   keeps it; the view shows it as "Progress".
7. **Request rows say what ended them, not who.** `resolution.by` is a device
   id; naming the device needs the devices list (G21).
8. **The countdown uses `Date.now()`** against the supervisor's `retryAt`,
   which uses the store's clock: they agree in the browser, and not in a test
   with fake timers (the view takes a `now` prop for that).
9. **The footer's lines come from `fileEdits`' diffs.** A tool that reports a
   file edit with no diff counts the file and no lines.

## Dependencies

None added. The bundle grew by the view: `session-view.js` 90.1 kB (27.4 kB
gzip, from 2.8 kB, now with zustand), `session-view.css` 15.9 kB (3.9 kB gzip,
from 5.1 kB).

## Screens

`pnpm exec vite --config src/session/screens/vite.config.ts` serves the view
over the recorded fixtures at <http://localhost:5199/?scene=NAME&theme=light|dark>
(`window.__scene` has the store, the scripted transport and the environment).
`node src/session/screens/shoot.mjs <dir>` photographs every scene at 1280x800
and at a 390x844 touch phone, in light and dark, over the DevTools protocol
(Node 22 and a Chrome binary, no dependency). Scenes: `default`,
`work-group-expanded`, `tool-expanded`, `streaming`, `empty`, `loading`,
`offline`, `reconnecting`, `session-gone`, `hub-not-answering`,
`outbox-states` (waiting, sending, queued, refused, in doubt, unconfirmed),
`outbox-storage-down`, `outbox-loading`. The outbox scenes set the store's
`outbox` and `sends` through `describeSends`, so the words are the
selector's. The run for this milestone is under the shared notes folder,
`G12-screens/` (52 files).

## Verification

Run on 2026-11-21, macOS 27.3, Node 22.23.1.

| Check | Result |
|---|---|
| `pnpm typecheck`, `pnpm lint:tokens`, `pnpm test` (387 tests), `pnpm build` | pass |
| `tests.test_webapp_session_view`, `tests.test_session_view_fixtures` | pass |
| The built page, served as static files, with a real `fetch`: `/session/index.html?key=claude%3Anope` asks `GET /v1/sessions/claude%3Anope/events?v=2`, gets 404 and shows "Session gone" | pass |
| 52 screenshots, each read: 13 scenes x desktop and phone x light and dark | read; findings fixed (bubble width, the caret on its own line, a connecting banner said three times, a detached arrow) |
