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
| Request | `request` item | the prompt and how it ended ("Allowed", "Denied", "Allowed automatically", "Chose: dev" for a menu row, "Expired before it was answered"...). One still open says "Waiting for your answer". The controls are the approvals dock's (G13) |
| Notice | `compacted`, `error` | a rule with "Context compacted from 120k tokens"; an alert box for an error |
| Live text | `session.live` | after the last items of its own turn; assistant text as prose with a caret, thinking as a muted box, a tool call being typed as "Preparing a tool call" |
| Working | none | a spinner row while a turn is open and the session is `running` but nothing is streaming and no group is open |
| Turn footer | a turn that ended | "1 file +1 −1" from the turn's tool `fileEdits`, counted once per path; for a turn that failed, was stopped or lost its keeper, why. A turn that changed nothing and ended well has none |
| Send | `sends` (`describeSends`) | see below |

**Work groups.** A group is the run of steps between two other rows. It is
closed once it is done and open while it runs, and a person's own toggle wins
and stays: a group does not close under someone reading it, and a running group
the person closed stays closed as it grows. The choice (`useToggles`) is kept
for every entry of the group (its `seqs`) and read from the first that has one,
so it survives the group growing at the end (a running group) or at the front
(an older page of history joined it); the React key is `work-<first seq>`, which
is unique among groups. Closed, its steps are not made at all; opened once, they
stay so the close can animate. A step still "running" in a turn that has ended
never finished: it shows as stopped, not as a spinner. A tool opens for its
input, its progress while it runs, each file edit as a coloured diff
(`diffLines` reads hunk headers, so a changed line such as `++counter;` is not
taken for a file header, and a diff's trailing newline is not a blank row) and
its output. Input, progress, output and diffs are all cut the same way, at 6000
characters or 300 lines, whichever comes first (one very long line is cut too),
with a note of how many characters are not shown. "Worked for" is the span from
the group's first step to its last step's end (`completedAt`).

**Streaming.** The core applies deltas by id and offset; the view draws
`session.live` and does nothing with offsets. A hole shows "Catching up with the
rest of this reply." under the text until the snapshot's `live[]` replaces it.
The reply's live row and its message share an identity, so a reply that
finishes does not enter a second time.

**Sends.** Each unfinished send is a `SendView` from `describeSends`, drawn as a
bubble with the selector's `reason` as its one sentence (a note from the hub or
a tool inside it is shown as it arrived, letters untouched, with a full stop only
after a letter or digit) and a state arrow
(AGENTS.md's mapping: yellow for waiting and sent, a spinner for sending, red
for failed, grey for in doubt and unconfirmed). Discard exists only when
`canDiscard` is true and Retry only when `canRetry` is true, so while the outbox
loads, or a post is out, neither is offered: the view reads the flags and
decides nothing. Retry calls `retrySend` (a new send under a new id; if the store refuses it,
because the session became read-only or is gone, the reason shows under that
send), Discard calls `discardSend`.

**A delivered message shows once (keeper path).** The keeper path echoes the
command id a send was posted under as the `command_id` of the user's
`message.completed` (the core posts `command_id` and `client_id`, equal).
`visibleSends` hides any send whose `commandId` equals the `commandId` of a
user message already in the timeline, and never matches by text. The outbox
entry itself stays until its `command.result` (G11's invariant); the view only
stops drawing it. A send the hub only queued is still drawn, as "Queued: it
will be sent when the current turn ends.": it has no timeline message yet, and
the queued tray that would show it is G13's. A steer ends its queued entry with
a `cancelled` result and its text arrives as a new user message under a new
command id, so the cancelled entry leaves with its result.

**Legacy sends (a session no keeper holds).** The rule above does not hold
there, and the first version of this spec said it did. On that path the hub
answers a send with 202 and no receipt, and the user's message reaches the
journal later from the harness's transcript (`tail.py`), whose records carry no
send id: `protocol.message` has no `client_id` field, and nothing in
`hub.py` (`_send_message`, `_deliver`, `_send_result`) or `sendq.py` attaches
one to a `message.appended` event. So `protocol_compat.to_v2` makes the user's
`message.completed` as `{id: "j<seq>", kind: "user", text}`, with no
`command_id`, for a send from this device, from another device, or typed in the
terminal alike, and no id links a message to a send. The view therefore shows
what is true and guesses nothing:

- The send's bubble ("Sent. Waiting for the session to take it.", no actions)
  and the delivered message are both on screen until the send's `command.result`
  arrives (v2 serves its `client_id` as `command_id`), which removes the entry.
  The result is published when the hub has injected the text, so the two usually
  share the screen for a moment, in either order.
- If that result is lost (a catch-up by snapshot does not carry it, and a legacy
  send cannot be asked about), the entry ends `unconfirmed` after 45 minutes
  (`LEGACY_CONFIRM_MS`) and offers Retry and Discard **beside the message that
  was delivered**. Retry sends it again as a new message, so the session would
  get it twice. The bubble says "never said whether it reached the session"; the
  person has to look.
- A pending send with the same words as a message typed in the terminal stays
  drawn: matching by text would hide a send that was not delivered.

What a hub change would need to carry to close this: the legacy `_deliver` would
have to leave the send's id where the transcript's user record can be matched to
it (for instance a marker in the injected text that the tail strips and turns
into `client_id` on the `message.appended`, or the injector returning the
transcript position it wrote at), `to_v2` would pass it on as `command_id`
(`_v2` already takes a `command_id`), and a satellite's agent would forward it
the same way. `session_v2.json` already allows `command_id` on the event. None
of this is in G12: it changes `hub.py` and the tail, which other work owns.
Limit 10 below states the person's side of it.

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
`@media (prefers-reduced-motion: no-preference)`, and the Tailwind ones are
`motion-safe:`; `motion.test.ts` fails on one outside, in the stylesheet or in a
`.tsx` class, and on a rule not under `.swe-sv`. No ALL CAPS, and the mono font
only on `pre` and `code`.

**Markdown.** `ui/markdown.ts` reads a reply into plain data (paragraphs,
headings, lists with nesting, quotes, fenced code, tables, rules; code, bold,
italic and links in a line) and `Prose` draws it with React elements, so reply
text never becomes HTML. Nesting is capped at 8 levels (quotes, lists, bold in a
link): what is deeper stays as text, so a reply of ten thousand `>` cannot
overflow the stack that reads it. A table is drawn with at most 50
columns and 500 rows, and the whole reply has a budget of 5,000 table cells, nested
tables included, spent in document order. A table is rows times columns cells
(a short row is filled out to the header's width), so a few bytes make a lot of
them: ten kilobytes of `|a|a|...` made 400,000 cells, which froze the tab where
no error boundary can help, and a table at its per-table caps is only about 1.2
kilobytes, so thirty-four of them would still be a million. A table that does not
fit the budget keeps the rows that do, and every later table is left out whole.
What is left out is said ("Table cut: 1950 more columns and 1 more row not
shown.", or for a table left out whole, "Table cut: 50 columns and 500 rows not
shown (this reply has more tables than this view draws).") with the start of the
left-out lines as plain text.

**Nodes per byte.** For every block type, the DOM nodes drawn are bounded by a
small constant times the bytes of its input; that is what keeps a hostile reply
from costing more than its size, and it is checked here so the next reviewer does
not have to:

| Construct | Nodes for N bytes | Why |
|---|---|---|
| Paragraph, heading | at most N / 1.5 | the shortest mark (`` `a` ``, `*a*`) is 3 bytes and makes 2 nodes; formatting is read in the first 6000 characters of a run and nested at most 8 deep |
| Fenced code | 3 | one `pre`, one `code`, one text node, whatever the length |
| List | at most about 2N | an item is a line of 2 bytes or more and makes an `li` and its text; a nested list adds one `ul` for the line that opens it; nesting is capped at 8 |
| Quote | about 1 per quote group | a group of quote lines is one `blockquote`; each level costs at least one byte (`>`) and levels are capped at 8 |
| Rule | 1 per 3 bytes | `---` is the shortest |
| Table | cells at most 5,000 per reply, plus one `tr` per line | the only construct whose output is not linear in its input (rows times columns), so the only one with a budget; `tr`s are one per line of 2 bytes or more and at most 500 per table |
| Cut table text | at most 1000 characters in one node per table | a table left out whole costs one note and one text node |
| Tool input, output, progress, diff | at most 300 lines and 6000 characters, in one `pre` | cut by both |
| Attachment chips | at most 50 | the list comes from the hub |
| Request prompt, error message, system message | 1 text node | plain text |
| Work groups, rows | one per journal item | bounded by the journal (4,000 records, G9), not by a reply |

Formatting is read in the first 6000
characters of one run of text; the rest stays text, and a mark is searched for
only when the text holds what would close it, so many marks that never close
cost no more than a few that do. A link is an anchor only for `http`, `https` and
`mailto` (`rel="noopener noreferrer"`); anything else leaves its words. A code span closes with
a run of backticks as long as its opening one, and a list item's content is
read with its real indent removed, so code under a numbered item keeps its own. An open
code fence runs to the end, so a reply still being written draws sensibly.
`react-markdown` (named in G1) was not added: nothing else needed its
dependency tree.

**One bad item.** Each row sits in its own error boundary: a row that throws
while it is drawn shows "This item could not be shown." (and is tried again when
the item changes, for a work group when any of its steps does) and the rest of the timeline stays. A second boundary around
the whole timeline shows one line in its place and leaves the header and the
composer's place standing. Without them a render error unmounted the whole view,
and since the item is in the journal, did so on every load of that session.

**Where the window sits.** The timeline follows the newest content while the
person is at the end, and stops following once they scroll up ("Jump to
latest" takes them back). When older history is added above, the row they were
reading is kept where it was in the window (the first row with any of it in
view is noted when they ask, and its place is restored once the page arrives in
front of the first item; a reply growing at the bottom meanwhile does not move
it). Opening or closing something is the person's own doing: the height it
changes does not pin the window to the end, which would carry the header they
pressed off the top; following the end resumes once the change has settled and
the window says it is at the end again.

**Cost.** A streamed delta redraws the live row and no other: rows are compared
by what they hold (`rowsEqual`, the journal's items and the live text are the
same objects until they change), `RowView` and `Prose` are memoised, and the
test counts the history rows drawn and parsed after a delta (none). With 5,000
rows a delta cost 255 to 320 ms before and about 40 ms now (what remains is
making the rows and comparing them).

**The composer's place.** `ComposerPlaceholder` is the disabled box G10 drew,
marked `data-placeholder="composer"` and commented as G13's. Its one live part
is the outbox's `note`, which G11 asks a view to show beside the composer when
storage does not survive a reload. It shows the read-only reason as its
placeholder when the row says so, has a dashed border, and says beside the box
"Sending from this view is not available yet." so it is not taken for a
composer that does not work. G13 replaces the file.

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
   (G11 Limit 3), so a very long session costs layout time and memory. Closed
   work groups draw only their headers, and a delta redraws only the live row
   (about 40 ms at 5,000 rows, measured in jsdom), but a first draw of 5,000
   rows takes about 0.6 s and building the rows is still linear in the journal.
   `@legendapp/list` (G1) is the way out when it matters.
2. **Browser tests are jsdom tests, and the screenshots are not a test.** G1
   names Playwright; the toolchain has Vitest and jsdom, and adding a browser
   runner to CI was not this milestone's. The DOM tests run the real store over
   the recorded fixtures and over hand-built sequences, and the scroll tests
   give jsdom a small fake layout (rows of a set height). The screenshots come
   from `session/screens/shoot.mjs`, which does not use Playwright: it starts
   Chrome and drives it over the DevTools protocol with Node's own WebSocket. It
   is a manual step, not a CI job, and checks nothing by itself: the pictures
   are for a person to read. Real scroll physics and the animations are only
   seen there.
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
10. **A legacy send can show beside its delivered message** (see "Legacy sends"):
    both are drawn until the send's result arrives, and if that result is lost
    the send ends unconfirmed with Retry next to the message that was
    delivered; Retry would send it again. It needs the hub to carry the send's
    id onto the transcript's user message, which this milestone does not touch.
11. **Formatting is read in the first 6000 characters of a run of text.** A
    paragraph longer than that shows the rest as plain text, marks and all.
12. **A table is cut at 50 columns and 500 rows, and a reply draws at most 5,000
    table cells in all.** A table past the budget keeps the rows that fit, every
    later one is plain text (the first 1000 characters of what was left out) under
    a "Table cut" line. A real reply with that many cells is a data dump; the
    reader sees the start and the note.
13. **A group's React key is `work-<first seq>`.** When older history joins a
    group at the front its key changes, and the group is drawn again from
    scratch (the person's open or closed choice, and each step's, are kept by
    seq, so nothing they chose is lost).

## Dependencies

None added. The bundle grew by the view: `session-view.js` 96.8 kB (29.7 kB
gzip, from 2.8 kB, now with zustand), `session-view.css` 16.0 kB (3.9 kB gzip,
from 5.1 kB).

## Screens

`pnpm exec vite --config src/session/screens/vite.config.ts` serves the view
over the recorded fixtures at <http://localhost:5199/?scene=NAME&theme=light|dark>
(`window.__scene` has the store, the scripted transport and the environment).
`node src/session/screens/shoot.mjs <dir>` photographs every scene at 1280x800
and at a 390x844 touch phone, in light and dark, over the DevTools protocol
(Node 22 and a Chrome binary, no dependency). Scenes: `default`,
`work-group-expanded`, `tool-expanded`, `stopped-tool`, `streaming`, `empty`, `loading`,
`offline`, `reconnecting`, `session-gone`, `hub-not-answering`,
`outbox-states` (waiting, sending, queued, refused, in doubt, unconfirmed),
`outbox-storage-down`, `outbox-loading`. The outbox scenes set the store's
`outbox` and `sends` through `describeSends`, so the words are the
selector's. The run for this milestone is under the shared notes folder,
`G12-screens/` (56 files).

## Verification

Run on 2026-11-21, macOS 27.3, Node 22.23.1.

| Check | Result |
|---|---|
| `pnpm typecheck`, `pnpm lint:tokens`, `pnpm test` (482 tests), `pnpm build` | pass |
| `tests.test_webapp_session_view`, `tests.test_session_view_fixtures` | pass |
| The built page, served as static files, with a real `fetch`: `/session/index.html?key=claude%3Anope` asks `GET /v1/sessions/claude%3Anope/events?v=2`, gets 404 and shows "Session gone" | pass |
| 56 screenshots, 14 scenes x desktop and phone x light and dark, taken again after the review fixes | the changed ones read (tool-expanded, stopped-tool, loading, the outbox scenes, default); findings fixed along the way (bubble width, the caret on its own line, a connecting banner said three times, a detached arrow, a diff's blank last row, a skeleton invisible in dark) |
