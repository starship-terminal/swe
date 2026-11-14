# Sessions: the chat

The Sessions page (sidebar id `sessions`, drawn by `static/sessions.js`
from `GET /v1/app/sessions`, see docs/web-app.md) opens a session's chat
when you click its row. The chat is `static/pages/chat.js` (+ `chat.css`),
a port of the remote PWA's chat it replaced: remote
control from the web app, with the same routes, ownership rules and event
handling as the phone. It has no page module and no `/v1/app` route of
its own; it talks to the hub's session routes (docs/remote-control.md,
docs/json-contracts.md "Remote control").

The chat lives at `#/sessions?chat=<key>`, so the sidebar keeps Sessions
selected, the browser's Back button returns to the list (scrolled where it
was), and a chat can be linked. `chat.js` is not a registered page: it
publishes one global, `SessionChat`, which `renderSessions` calls when the
hash names a chat. Should the script fail to load, a row opens the details
sheet as before.

Beside the chat, a rail (the Mac app's `SessionRail`) lists every session
the page does, grouped by day the same way, so switching sessions doesn't
go back through the list. Its rows are the page's
`sessionsPage.data.sessions` with none of the page's filters applied; a
chat opened from a link, the list never loaded, fetches
`/v1/app/sessions` once and leaves the answer in the page's state for
when the chat is left. A rail row navigates to `#/sessions?chat=<key>`
exactly like a list row, the open session is marked, and it stays listed
even when the data predates it or the filters would leave it out. The
header's button refetches (`/v1/app/sessions?rescan=1`, the page's own
Refresh). Under 960 px the rail folds to a strip of session chips above
the chat, like the Repositories rail's fold.

## What it shows

| Part | What |
|---|---|
| Top bar | Back, harness logo, title, harness · folder (· machine for a satellite), the status badge, a `swe-…` tmux chip when the row names a pane (click copies `tmux attach -t swe-…`), a Live / Reconnecting dot, Detach (attached sessions only), More actions (the list's Pin, Rename and Fork menu, see From the list), and Details: the sheet the row used to open (harness, machine, folder, last active, id, models, the supervised mode and pane, tokens and spend, icon copy buttons, Run in terminal on macOS hubs). |
| Rail | Every session the page lists, grouped by day like the list: harness logo, title and a status dot (the badge colours; nothing for a finished one). A row swaps the chat to that session; the open one is marked and stays listed even when the list's data or filters would leave it out. The header button relists; under 960 px the rail is a strip of chips above the chat. |
| Transcript | The last 500 messages from `GET /v1/sessions/{key}/messages?limit=500`, then live events. User text right in a bubble, assistant text left, both with the app's markdown (fenced code, inline code, bold; everything else escaped). Thinking folds under "Thought for 12s" (`Fmt.thought` over the hub's `duration_s`), or plain "Thought" when the hub sent no duration. Tool calls are cards ("Edit src/x.py") that open to their arguments and output; Edit, MultiEdit, Write, NotebookEdit and apply_patch cards show a diff instead. A card whose call writes an `.html`, `.htm` or `.svg` file inside the session's folder has a Preview button (see Page preview below). A reply's web links (http and https, not in code) get up to three cards under it: host, path and Open, which opens a new tab with `rel="noopener noreferrer"`. Compaction is a "Context compacted" rule; system notes are centred captions; errors are red. A reply still being written (`message.delta`) is drawn from its last settled block on: blocks that end at a blank line keep their HTML and their nodes, and each repaint parses and replaces only the text after them (`mdStream`), always matching a full `md()` render. The log keeps its newest 200 entries (a tool run counts once); older ones leave the DOM for a "Show earlier" button at its top that puts them back 200 at a time. The fold runs only while the log is scrolled to its end, and stops at a send still waiting or an unanswered approval, so every entry keeps its place. `SessionChat.revealSeq(seq)` expands down to a folded message before scrolling to it. The log is no live region; a hidden status node reads a finished reply, an error or an approval prompt aloud once, cut to its first 300 characters. |
| Approval card | "Approval needed", the prompt, and the menu rows the pane shows as stacked option buttons (the event carries `options` with each row's digit and label, so "6 Edit command" or "Yes, switch to bypass mode" are answerable too). Older events and non-tmux approvals carry no menu; they show Allow, Always, Deny. Once answered the card folds to a "Approval · {what was picked}" row that re-opens to the full prompt and menu, the picked row at full shade and the rest greyed. |
| Working dots | While the session is active, a send waits for its reply, or live messages are still being revealed. |
| Queued tray | The iPhone's tray, above the composer: a Queue-mode send made while the agent works waits here instead of in the transcript, "N messages queued" with Remove all (a second click within 3 s confirms). Each row has Edit (its text goes into the box, Send becomes Save, Escape or Cancel puts the draft back), Remove, and Send now (a steer into the running turn, reading "Sending now" until it lands). A refused action leaves a short note on the row ("Already sent", "Attach to send now"). A row leaves for the transcript on its echo or its `command.result`; a removed one is just gone. The hub holds the sends, so the tray is read again from `GET .../queue` when the chat opens and when the tab comes back, which also shows sends queued, edited or removed on another device. A satellite's sessions never use it: their sends wait on the satellite. |
| Composer | Queue or Steer, the message box (Enter sends, Shift+Enter is a new line), Stop and Send. Off, reading "Read-only until this session is attached", unless the session is attached. Pinned to the bottom of the panel: an open chat is a fixed-height layout where the transcript is the only scroller (chat.js toggles `body.chat-open`), like the apps'. Typing `/` as the box's first character opens the command menu above it: `GET /v1/sessions/{key}/commands`, fetched when the chat opens (and kept per session key, so a reopened chat shows it at once) and filtered as the name grows, each row with its argument hint, description and source tag (Built-in, User, Project, Skill, Plugin). Arrows highlight, Enter or Tab inserts `/name ` (or the row's own `insert`, like a Codex skill's `$name`), Escape dismisses until the token changes. The hub only lists commands; the harness expands the text when the message arrives. An `@` at the start of the box or after a space opens the file menu instead: paths under the session's folder from `GET /v1/sessions/{key}/files?q=` (hub-machine sessions only), with a path that has spaces written quoted, `@"My Documents/notes.txt"`. The paperclip, a drop on the composer or a pasted screenshot adds files: each uploads at once through `POST /v1/sessions/{key}/attachments` and shows as a chip with its name and size, after the hub's caps are checked in the page (20 MB; images, PDFs and text files), and the send lists their refs in `attachments`. |
| Observed banner | "Observed session: read-only until attached." with Attach when the hub says the session is `attachable` and it isn't mid-turn; otherwise why it can't be attached (mid-turn, or a harness that can't be resumed). A `kept` row reads "Still running in tmux…" and offers Reattach even mid-turn — the pane is adopted, nothing restarts. |
| Detaching banner | While a `when_idle` detach is pending: "Detaches when this turn ends." with Stay attached, which posts `op: attach` to cancel it. |
| Page preview | `GET /v1/sessions/{key}/preview?path=<relative path>` (`encodeURIComponent`, so `+` goes as `%2B`), fetched fresh on open and on Reload. The page is drawn in an `<iframe>` whose `sandbox` attribute is empty (no scripts, no same origin, no forms, popups or top navigation) and whose `srcdoc` starts with `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; base-uri 'none'; form-action 'none'">` and `<base target="_blank">`, placed straight after a leading doctype or else first, so it precedes anything that loads. That frame sits inside a wrapper frame of our own with the same empty sandbox and policy: a frame may navigate itself (a `target="_self"` link), and the browser checks that against the parent's `frame-src`, which the wrapper's `default-src 'none'` closes. The page's frame also inherits the wrapper's policy. An SVG is wrapped in a page as a `data:` image. Source shows the text; a refused file shows the hub's reason in plain words (the phone's wording). Which calls count and how an absolute path becomes relative to the session's `cwd` is a port of the iPhone's `PagePreview.swift`. |
| Detach sheet | From the bar's Detach button, an attached session gets the phone's three-way choice as stacked options: Detach when it finishes (`when_idle`), Keep it running in tmux (`keep`, only when the mode has a pane), Detach now (`now`). |

## How it talks to the hub

1. `GET /v1/sessions/{key}/messages?limit=500`, the backfill.
2. Then, together: `GET /v1/sessions?key=<key>` for the catalog row (`ownership`,
   `attachable` and `status` as the hub has them now, since the list's
   copy can be minutes old, and the machine's name), and `GET /v1/inbox`
   for an approval of this session the hub still waits on (its
   `request_id`). The row is read after the backfill so an attach or
   status change journaled before its last message is already in it;
   one after it comes with the stream. The composer stays off until the
   row is in, and a tab coming back to view doesn't reconnect before
   the stream has started.
3. `GET /v1/sessions/{key}/events?last=<seq of the last message shown>`,
   read with a fetch stream (EventSource can't send the bearer). Events
   are revealed one at a time with a short gap; a replay of
   more than 24 lands at once. A dropped stream reconnects after 2 s from
   the last seq shown, and again when the tab becomes visible. A hidden
   tab keeps the stream and every event but draws nothing: live reply
   text and held events are drawn when the tab is shown, through the same
   reveal, and the cursor moves only as each is shown. Leaving the
   chat by any route aborts the stream, so it never keeps one of the
   browser's few connections to the hub. When the waiting approval was
   asked before that message (codex's app-server journals the ask before
   its tool call), the stream starts at 0 instead, takes only that ask
   from the part already shown, and puts its card back where it was
   asked.

Every request sends app.js's `authHeaders()`: the paired bearer when the
browser has one, and `X-Swe-Pair: 1`, so the hub owner's own tailnet
devices work without pairing.

Events handled: `message.appended` (a user message also takes its send
out of the queued tray), `session.status` (badge, dots, Attach offer),
`session.ownership` (composer and banner), `session.meta` (the header's
title and the menu's Pin or Unpin), `approval.requested`,
`approval.resolved`, `command.result` (settles a send by `client_id` or a
queued approval; an unmatched failure becomes a system line).

Writes, all with the device bearer:

| Control | Request | Answer |
|---|---|---|
| Send | `POST .../message {text, mode: queue\|steer, client_id}` | `202 {client_id, waiting}`: `waiting` moves it into the queued tray (a Queue-mode send while the agent works starts there), else the bubble reads "Sent", then settles on its `command.result` (dropped on success, "Sent (queued: …)" when degraded, red with the note on failure) or when the transcript echoes the message. |
| Queued tray | `POST .../queue {client_id, op: steer\|cancel\|edit, text?}` for Send now, Remove and Save. | `200` acts (Remove and Save at once; Send now waits for the send's result or echo). `409 not_waiting` marks the row as already gone, `404 unknown_send` drops it, `409 not_attached` reads "Attach to send now"; an older hub's `422` on `edit` removes the message and queues the new text at the end. |
| Menu option or Allow, Always, Deny | `POST .../approval {request_id, decision}` where `decision` is the row's digit when the card lists the pane's menu, else `allow`/`always`/`deny`. The injector checks a digit against the menu actually on screen before sending it. | `200` resolves the card; `202` (a satellite's session) reads "Queued: waiting for the satellite" until its `command.result`; a refusal re-enables the buttons with the hub's message. |
| Stop | `POST .../control {op: interrupt}` | Off while the request runs; a refusal adds an error line. |
| Attach | `POST .../control {op: attach}` after a confirm in the banner: "Attach stops <harness> in the terminal running this session, then resumes the session under SWE so you can send to it from here." — for a `kept` row the confirm reads "Reattach to the running tmux session?" since the pane is adopted live. | `200` opens the composer at once (the hub's `session.ownership` follows); `202` (a satellite) adds "Attach queued: waiting for the satellite"; `409 not_idle` and `422 not_attachable` show as an error line with the hub's reason. |
| Detach | `POST .../control {op: detach, mode: now\|when_idle\|keep}` from the sheet. | `200`/`202`: `when_idle` marks the banner pending until the hub fires (its `session.ownership`, or a `detach` `command.result`, clears it); `now`/`keep` flip the row to observed at once, `keep` leaving `tmux`/`kept` set. A refusal adds an error line. |
| Run in terminal | `POST /v1/app/sessions/open_terminal {key}` from the details sheet, shown only when the hub is macOS and the row is local with a pane. | `200` opens Terminal.app attached to the `swe-…` session; `409 no_tmux`, `422 unsupported`, `502 open_failed` land on the button. |

Any write answered `409 not_attached` means the session went back to
observed: the composer locks and the banner returns.

`/v1/app/sessions` rows also carry `ownership` (`attached` or `observed`)
and `attachable`, so the list marks attached sessions with an Attached
badge and a kept one with Kept.

## From the list

The list carries the iPhone app's session actions itself, so a session
can be attached or detached without opening its chat:

- A row offers one action, the phone's leading-swipe rule: **Detach** on
  an attached row, **Attach** on an `attachable` one that isn't mid-turn
  (a `kept` row attaches even mid-turn — the pane is adopted). A write in
  flight reads "Attaching…"/"Detaching…".
- Detach on an idle row just posts `detach` with `when_idle`. On an
  active or approval-waiting row it opens a sheet first — the chat's
  same three-way choice: Detach when it finishes, Keep it running in
  tmux (only when the row has a pane), Detach now. Detaching now stops
  the turn.
- The details sheet has the same button, next to Run in terminal.
- A finished write refetches the list rather than patching the row, so
  badges and actions always match the hub; a refusal lands as a note
  above the filters.

A row shows a blue dot before its title when the hub flags it `unread`
for this browser (#1439). Opening the chat clears it at once through a
local mark and posts `POST /v1/sessions/{key}/read {}`; while the chat
stays open and the tab shows, live messages post again at most every
3 s (`READ_EVERY_MS`), and closing it posts once more, covering what it
showed. A hidden tab posts nothing, since the hub's cursor is the time
of the post. The next list read drops marks the hub has caught up with
(`unreadReconcile`).
A close keeps the newest reply it showed marked read, so a failed or late closing post can't relight it. Marks hold the hub's times and message stamps, never the browser's clock. `SessionUnread`
in sessions.js holds the rules; chat.js calls it.

The iPhone's swipe-left actions sit behind each row's More actions
button (the `⋯`), and a right-click on a row opens the same menu, as a
long press does on the phone. The chat's header has the same button.

- **Pin** / **Unpin**: `POST /v1/sessions/{key}/pin {pinned}`. The row
  moves at once; a refusal puts it back with a note above the filters
  ("Couldn't pin it. …"). Pinned rows lead the list in a Pinned group,
  in the order they were pinned (`pinned_at`), out of their day groups,
  and stay listed whatever the time filter says (a pinned short session
  too). Like the phone's `PinLedger`, each pin choice gets a version: a
  reply for an older choice is dropped, and a list fetched before the
  latest choice was answered keeps the choice.
- **Rename**: the phone's `RenameSheet`. `POST .../title {title}` with
  the hub's rules applied as you type: one line, at most 120 code
  points, spaces collapsed on save. Save is off until the name is
  non-empty and new; a renamed session also offers "Use the original
  name" (posts `""`) with the harness's own title under it. The hub
  keeps the name, so every paired device shows it.
- **Fork**, only when the row's `can_fork` is true: the phone's
  `ForkSheet`. A first message is required; `POST .../fork {prompt}`
  then polls `GET /v1/launches/<id>` the same way New session does, and
  opens the fork's chat once the hub names its key. The hub's refusals
  (`fork_unsupported`, `fork_pending`, `bad_cwd`, …) show in the sheet.

**New session** in the header is the iPhone's `NewSessionView`: machine
chips (from `GET /v1/machines`, the ones with anything in `launchable`,
online first), harness chips (the machine's `launchable`, its
`harness_names` for display), a folder, and a first prompt. The folder
box takes an absolute path, offers the machine's recent session folders
filtered as you type, and on the hub machine can browse subfolders via
`GET /v1/folders` (a satellite's disk can't be listed from here, so its
box stays typed-or-recent and the browser shows the hub's reason). The
harness defaults to the one last used in that folder on that machine,
else the first offered. When the machine row's `model_at_launch` names
the picked harness, a Model menu follows the harness chips, filled from
the row's `recent_models` (or `last_models` on an old hub); it is the
same control as the Tasks page's, see
[The Model menu](tasks.md#the-model-menu).

Start posts `POST /v1/sessions` once (the button locks) and then polls
`GET /v1/launches/<id>` once a second like the phone's `LaunchFlow`:
`started` opens the chat on the named key; `failed` shows the hub's
note; `untracked` (the harness runs under tmux but swe can't read its
sessions) and the 90-attempt bound land on an "unconfirmed" note saying
it'll appear in the list when it does; a satellite's `queued` launch
says it starts when the machine is online. Closing the sheet cancels the
wait, not the launch — the hub keeps it and the session appears in the
list whenever it lands.

## Mirrored sessions

A row with `mirrored: true` is a session whose harness deleted its
transcript; swe lists the read-only copy it kept (`swe session mirror`,
#1918). `webapp/sessions.py` passes the flag through and forces the
fields that only make sense for a live session: `resume` null,
`attachable` and `can_fork` false, and `trivial` false (the ledger has no
usage for a copy, and "no usage after a clean scan" would otherwise hide
it as an accidental session). The one wording everywhere is "Its
transcript was deleted by the harness. swe kept a read-only copy."

| Place | A mirrored session |
|---|---|
| List row | A quiet "Mirrored" tag in the status badges' slot, with the wording as its hover hint. No status badge, Attached or Kept mark, and no Attach or Detach button. Pin and Rename stay in the More actions menu. |
| More actions menu (row, right-click, chat header) | Pin and Rename as usual; Fork is listed but off (`aria-disabled`) with the wording as its hint. |
| Details sheet | The tag, the wording as a line under the title, no Attach or Detach, no Resume command. |
| Chat | A one-line notice at the top (the banner, in neutral colours, no button) in place of "Observed session: read-only…". The composer stays off, there is no Live dot, and the page asks for no stream, approval or queue. The transcript shows when the hub can read the copy; if it cannot yet (a 404 or error) or the copy is empty, the log says "This session's chat is not available yet." with no error styling. |
| Filters and counts | The page has no status filter, so no "Mirrored" option was added. A mirrored row stays in the list, in the day groups and in the summary strip's session count (its tokens and spend read as dashes, as for any row without ledger usage). |

At phone width the tag shrinks to 11 px with tighter padding, and the
title (the one part of the row that ellipsises) takes the difference.

## Differences from the remote PWA it replaced

- Attach asks first. The PWA attached on one tap; here it stops the
  harness in its terminal, so the banner names that before it happens.
- Opening a chat streams from the last message the backfill showed, not
  from a seq saved in `localStorage`, so an approval asked or a result
  reported after the last message is replayed into view, and a waiting
  approval asked before it is restored too. Within an open chat,
  reconnects resume from the last seq shown, as the PWA did.
- Codex's `apply_patch` (raw patch text) renders as a diff, and code
  blocks keep `**` and backticks as written.
- New messages scroll the page only when it is already at the bottom, so
  reading back through a long transcript isn't interrupted.
- Stop shows a refusal instead of ignoring it, and an answered approval
  says what was decided.
- A reconnect that finds the hub's journal trimmed past where the view
  stopped (`journal.truncated`) adds a notice that some events were
  missed and are gone from the journal, where the PWA carried on as if
  nothing were missing.
- Not ported: "load earlier". Attachments, the `@` file menu and the
  waiting-sends tray are ported, with the Queue/Steer picker kept.
- The list actions are the iPhone app's, which the PWA doesn't have:
  rows carry no swipe actions there (Attach/Detach live in its chat
  only), and it has no New session sheet. The page mirrors the iPhone
  rules instead: the same leading-swipe attach/detach rules, the same
  three-way detach choice, the same `NewSessionView` pickers and
  `LaunchFlow` polling, and its pin, rename and fork actions behind a
  More actions menu instead of swipes.
