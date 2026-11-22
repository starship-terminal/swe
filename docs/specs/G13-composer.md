# G13: composer, approvals, queued tray

Status: built. Everything over the bottom of the session view
([G12](G12-timeline.md) drew the timeline and left a disabled box): a floating
glass bar with icon-only controls (attach, the message box, Send or Queue, Stop),
the menu behind the primary button (the steer item, "Stop and send" or "Send now"), the approval and question
dock, and the queued tray. The data layer is [G11](G11-client-core.md) and is
unchanged: the composer imports `src/core/index.ts` and nothing else from the
core. The view is still behind `remote.session_view`, which is off by default.

| File (under `app/web-session/src/composer/`) | What it holds |
|---|---|
| `Composer.tsx` | the floating bar and what stacks above it (notes, tray, dock); a bar with the reason in place of all of it for a read-only session |
| `Menu.tsx`, `composer.css` | the glass menu (attach, send options, a queued message's), and the rules a utility class cannot say: the glass, the keyboard inset, the timeline's end padding |
| `submit.ts` | `submitMessage`: the one place a message goes to the store, with its guards |
| `keys.ts` | `enterAction`: what Enter means (pure) |
| `draft.ts`, `useDraft.ts` | the draft kept in storage (with its age limit and sign-out clearing), and the hook that writes it |
| `press.ts` | what counts as a press of an answer that cannot be taken back (its own pointer or key start, no repeat) |
| `Menu.tsx` | the glass menu: safe first focus, no repeat, no key held from opening |
| `RequestDock.tsx`, `answer.ts` | the approval and question dock, and `answer`: a press that lands on the request shown |
| `QueueTray.tsx` | the tray: a strip that opens upward into a list; edit, remove, and send now in each item's menu |
| `steer.ts` | what steer is called and says, from `caps.steer_effect` |
| `ErrorNote.tsx` | a message that must be read: its own solid backing, full ink, a red border and arrow |
| `AnswerButton.tsx`, `Guard.tsx`, `text.ts` | the button for answers, the error boundary, the cut and clip rules for text from an agent |
| `upload.ts` | staging a file on the hub (`POST .../attachments`) |
| `scenes.ts` | the composer's scenes for the screens page (dev only) |
| `harness.tsx`, `*.test.ts(x)` | the tests |

Edits outside `src/composer/`, each the smallest that mounts the composer:

| File | Edit |
|---|---|
| `src/session/SessionView.tsx` | mounts `<Composer>` in a `Guard` where `ComposerPlaceholder` was, inside a host that also holds the timeline, so the bar floats over it; `ComposerPlaceholder.tsx` is deleted |
| `src/session/index.ts` | `COMPOSER_PLACEHOLDER` now comes from `../composer` |
| `src/session/Timeline.tsx` | a send the hub holds in `session.queue` is left out of the timeline, since the tray draws it (G11: "draw that instead of the pending bubble") |
| `src/session/model.ts` | a comment only |
| `src/ui/Icon.tsx` | eight icons: `attach`, `stop`, `edit`, `plus`, `queue`, `more`, `folder`, `photo` |
| `src/swe/webapp/static/tokens.css`, `src/index.css` | the glass tokens (below), and their names for Tailwind (`rounded-bar`, `rounded-glass`, `border-glass-border`) |
| `src/session/Timeline.tsx` (again) | the timeline's content leaves `--sv-composer-h` of padding at its end, and "Jump to latest" sits that far above the bottom (two class names: `sv-pad-end`, `sv-jump`); its resize observer watches the border box, since that padding is the composer's height and a content box does not change with it, so a taller bar keeps a reader at the end |
| `src/ui/Prose.tsx`, `src/ui/index.ts` | `InlineText`: one run of text with code spans drawn as code and every character kept (nothing else is read) |
| `src/session/Rows.tsx` | the timeline's request row draws its prompt through `InlineText`, cut with the timeline's `clip` (6000 characters, 300 lines), so a prompt shows the same in the row and in the dock |
| `src/session/WorkGroup.tsx` | a step stopped by the end of its turn has its own mark (the square), not the cancelled step's cross |
| `src/swe/webapp/static/app.js` | `pairForget` also removes the saved drafts (`swe.sv.draft.v1:*`), 13 lines |
| `src/session/screens/main.tsx`, `shoot.mjs` | the composer's scenes (`composer-*`) |
| `SessionShell.test.tsx`, `SessionView.test.tsx` | the tests that described the placeholder now describe the composer |

`src/core/**` and `src/swe/remote/` are untouched. `THIRD_PARTY_NOTICES.md` is
unchanged: **nothing is copied from t3code** (the checkout is still not
available), the dock and the tray were written for this repository.

## The bar

**Where it sits.** One floating bar pinned to the bottom of the view, over the
timeline, not a card in the page: a pill (a soft rectangle when the box grows)
of translucent glass with a hairline border, inset from the edges, above the
home indicator, and above the keyboard when `--keyboard-inset` says there is one
(`max(var(--safe-bottom), var(--keyboard-inset, 0px))` plus 0.5 rem; the iPhone
embed sets the variable, G1 section 4 and G18). The timeline scrolls under it:
the composer reports the height of everything it draws, and of the keyboard or home indicator
under it, as `--sv-composer-h` on the view's host (measured again when the stack changes size and when the
host's padding changes, which is how the keyboard arrives), the timeline's content ends with that much padding (so the last
line is never hidden, and a dock that appears moves the content, which keeps
following the end if the person was there), and "Jump to latest" sits above it.
The same bar on desktop, centred at the timeline's width (`max-w-3xl`).
Everything the composer draws is held to half of the space above the keyboard or the home
indicator (`max-h-1/2` on the stack; the bottom padding is the host's, so the keyboard is not counted in
the half); the notes, tray and dock scroll inside what is left, and the bar never leaves the screen.

**Measurements.** t3code's source is not on this machine (the same as for G12),
so nothing was read from it. The numbers below are estimated from Kai's
reference screenshot of its iPhone app (924 px wide, which is about 2.35 px to
the point at a 393 pt screen) and are marked as estimates: the bar is about 47 pt
high and about 367 pt wide, inset about 13 pt from each side, with a plus at
the left, the box, a model chip with a chevron, a microphone and a filled round
send button at the right (about 34 pt). Here: a 24 px radius and 12 px inset
(estimated from that, set by tokens), buttons 36 px (a thumb's width, not
measured from t3code). No microphone: SWE has no voice input.

**The glass** is three tokens with a light and a dark value, added to
`src/swe/webapp/static/tokens.css` (the shared sheet, which the Mac app's
`Theme.swift` follows by name; these are not in `Theme.swift` yet), and one
rule, `.sv-glass`, in `composer.css`:

| Token | Light | Dark | Is |
|---|---|---|---|
| `--glass` | `rgba(254,254,254,.86)` | `rgba(37,37,37,.84)` | the translucent surface |
| `--glass-solid` | `#fefefe` | `#252525` | the surface without `backdrop-filter`, and under `prefers-reduced-transparency` |
| `--glass-border` | `rgba(6,6,6,.12)` | `rgba(255,255,255,.14)` | the hairline |
| `--glass-blur` | `20px` | | the blur radius (`blur(20px) saturate(1.5)`) |
| `--bar-radius` | `24px` | | a pill at one line |
| `--glass-radius` | `18px` | | the docks, the tray, the menus and the notes |

`.sv-glass` is `background: var(--glass-solid)`; inside `@supports
(backdrop-filter: blur(1px))` it is `var(--glass)` with the blur; inside
`@media (prefers-reduced-transparency: reduce)` it is solid and unblurred again.
No arbitrary value is in a class name and `lint:tokens` is clean.

**Contrast** (WCAG, computed from the token values by `G13-contrast.mjs` in the shared notes folder: the glass composited over a
backdrop, then the text over that. Not a measurement of pixels; the screenshots over a code block and over a red diff line were also read by eye):

| Over | Text | Placeholder and icons | Error message |
|---|---|---|---|
| light: plain timeline | 15.1 | 4.4 | 15.2 |
| light: a code block | 14.9 | 4.4 | 15.2 |
| light: dense text behind (35% ink) | 13.8 | 4.3 | 15.2 |
| light: a red diff line | 14.1 | 4.3 | 15.2 |
| light: worst case (70% ink) | 12.7 | 4.2 | 15.2 |
| dark: plain timeline | 12.9 | 5.3 | 12.6 |
| dark: a code block | 12.7 | 5.2 | 12.6 |
| dark: dense text behind (35% ink) | 11.0 | 4.8 | 12.6 |
| dark: a red diff line | 12.2 | 5.1 | 12.6 |
| dark: worst case (70% ink) | 9.2 | 4.3 | 12.6 |
| solid fallback, light and dark | 15.2, 12.6 | 4.4, 5.2 | 15.2, 12.6 |

How the error column got there. An error message first used `--danger-text` on the glass, which measured 4.8 at best
and 2.9 over the dark worst case (that token is 4.2 on a plain dark card by itself), and raising the glass alpha bought
under one point for a surface that stops looking like glass. So no danger- or warning-coloured text goes on the glass
now (`glass.test.ts` reads the sources and fails on one). A message the person must read (a refused send, a refused
answer, a failed save, a tray item's error) is an `ErrorNote`: its own opaque backing (the `--card` token), the text in
full ink, and the red in a border and an arrow. Ink on the card is 15.2 in light and 12.6 in dark whatever is behind
the bar, so the column holds at 4.5 and above over every backdrop tried. The same went for the dock's "Approval
needed" line (warning text measured 3.9 over the light worst case; it is ink now, with the yellow arrow), and for
Deny and Skip (ink with a red border). The red trash icon in a queued message is secondary ink, red only on hover.
Text is 9.2 or more everywhere. The placeholder and icon colour is `--text-muted`, 4.4 on the plain light card by itself,
so the glass costs it 0.2 at the most except over a worst case, where it is 4.2 to 4.3; a placeholder is not a message
the person must read, and it is left as the token has it.

**Controls**, all icon-only, each with an accessible name and a tooltip (`title`),
none with a visible word:

- left: **Attach** (plus). It opens a small glass menu over the bar with what the
  browser offers: "Choose files", and "Photo library" on a touch screen (a picker
  for `image/*`). Each file is posted to
  `/v1/sessions/<key>/attachments?name=...` as raw bytes with the same credential
  as the stream (the store has no action for it, so the box does the request,
  `upload.ts`), and the hub's `ref` goes in the message's `attachments`. A file
  shows as a chip above the box while it uploads, with the hub's reason if it
  fails (a failure never stops a message without the file). A message takes at
  most ten, counted as each file is accepted; the hub caps a file at 20 MB and takes
  image, PDF and text types only, and says so with a 422 that the chip shows.
  Send waits while an upload is open.
- middle: a plain growing `<textarea>` (to 200 px, then it scrolls), "Ask anything"
  when idle and "Queue a message" while a reply runs. Enter sends, Shift+Enter adds
  a line. On a touch screen (`pointer: coarse`) Enter adds a line and Ctrl or
  Cmd+Enter sends.
- a slot for the model chip: `ComposerProps.chip`, a `ReactNode` drawn between
  the box and the buttons, `null` now. Nothing is drawn, not a disabled chip.
- right, idle: one filled round **Send** (arrow up).
- right, a reply running (`openTurn` is set, or the phase is `running` or
  `waiting`): **Stop** (a square on a danger-coloured circle; posts `interrupt`,
  a gated action: off, with the store's reason as its title, while the connection is
  down or has not caught up; "Stopping" in its title after a successful press, until
  the turn ends or five seconds) and the filled primary button, now **Queue**
  (a different picture: a list with a plus).

Both send through `store.send` with `mode: "queue"`, so the message is kept on the
device first (G11's outbox) and the timeline draws its bubble and its reason.

**The menu behind the primary button.** While a reply runs, holding the primary
button (450 ms), a right click, or the ArrowUp key (or Shift+F10) opens a glass
menu above the whole bar (not above the button: on a phone a draft of several lines has its first line level with it) with two items: **Queue** ("Sent when the current reply ends.") and
the steer item, which is named and worded from `caps.steer_effect` ("Stop and send"
only when the session says its steer stops the reply, "Send next" for the two that wait, "Send now" when the field is absent; see
"What steer does, per agent"; for `next_turn` it is not offered at all, and with it the menu: a steer that lands when the reply ends is Queue again). The
menu opens on Queue, never on the steer item. That is a rule of `Menu` itself: an item is `safe` (may take the first
focus) only if choosing it sends nothing, stops nothing, steers nothing and removes nothing in a way that cannot be undone
(Queue and the file choices are; the steer item and the tray's steer menu are not), and a menu with no safe
item opens with the focus on the menu itself, so a key still down from opening it chooses nothing. A repeat of any key
chooses nothing, and neither does a key's click while the Enter or Space that was down when the menu opened is still down
(the page tracks which keys are down; a pointer's click is not held back). The arrow keys move, Escape or Tab or a press elsewhere closes it
and the focus goes back to the button. The steer item is disabled, with the
reason in the menu and beside the bar, when `caps.steer` is not true ("This agent
cannot be steered."), which is also the case of an ACP agent. With no steer and a message typed, the line beside the bar
says what is true: "This agent cannot send a message ahead of the queue. A message sent now waits and goes when the reply ends." If the turn ended
between the menu opening and the item being chosen, the message goes as a plain
send: the hub refuses a steer with no turn open (`idle`, G16), and "send now" is
what the person asked for. The press that opened the menu never also sends: the
click that follows the release of a hold is swallowed (a hold let go off the button, a cancelled pointer and a button that
lost the focus forget the flag, so the next keyboard Enter or Space on it is a press; `sv-press` also stops the
iOS callout and text selection), and a short press, with no menu, sends once.
There is no menu while idle.

## Message safety

One press sends once. Each guard below is tested, and each test was run against
a copy of the code with that guard removed and fails there (see Verification).

| Case | Guard |
|---|---|
| Double click, or two presses in one task | `Composer.submit` takes a ref (set before the first `await`) and returns while it is held; the button's `disabled` waits for the next render and would not stop it |
| A hold that opens the menu, then its release | the click after the release is swallowed (`swallowClick`, set only while a pointer is down and forgotten 80 ms after its release); a right click and the key open the menu without a press at all |
| A hold let go somewhere else, a cancelled pointer, a lost focus | `pointerleave`, `pointercancel` and `blur` on the button clear `swallowClick` and `pointerDown`: nothing is left to swallow the next Enter or Space. The blur that the menu itself causes (it takes the focus off the button when a hold opens it) does not: the release that follows over the button is still the hold's |
| A key that opened a menu, held or pressed twice | `Menu` ignores key repeats, ignores the click a key makes while the key that was down at opening is still down, and never starts the focus on an item that is not `safe`; see "The menu behind the primary button" |
| A second press by any other route (a held key, a form `requestSubmit`, a re-render's replay, a second composer on the same store) | `submitMessage` keeps a `WeakSet` of stores with a send in flight and answers `busy` ("Still sending your last message.") without touching the store |
| Enter held down | `enterAction` returns `swallow` for `repeat`: the first Enter sent, the rest neither send nor type blank lines into the next draft |
| IME composition | Enter does nothing while `isComposing`, while `keyCode` is 229 (Safari fires `compositionend` before the key press that ended the composition), or while `compositionstart` has no `compositionend` yet |
| Send while the outbox is `loading` | the button is disabled with "Loading the sends kept on this device.", and `submitMessage` refuses with `loading` for any other route: sending then would draw beside entries that may be about to change, and in a browser whose IndexedDB never answers (G11 Limit 4) `send()` would never settle |
| A message over 200,000 characters | disabled with the reason; `submitMessage` refuses too. The text is never cut |
| Refused or thrown | the draft is cleared only when `store.send` answers `ok`. A refusal (read-only, gone, outdated) or an exception keeps the text in the box and in storage, and says why in an alert |
| While the message is being saved | the box is read-only (not disabled, so focus stays) with the draft in it, and the Send button is off |

**The draft.** It is kept in `localStorage`, one key per session
(`swe.sv.draft.v1:<session key>`), with the staged file refs. Why there: it is
read synchronously when the box first draws, so it does not flash empty and fill
later (IndexedDB answers after the first draw); it is small; and it is text the
person has *not* sent, which is a different thing from the outbox's IndexedDB
store of messages they have (G11). It is written 250 ms after typing stops, at
once on `pagehide`, when the page is hidden and when the box unmounts, so a
reload loses at most the last keystrokes. It is removed only after the outbox has
the message (`useDraft.clear`, which writes storage itself rather than trust the
timer: a copy older than the box must not come back). A browser that refuses
storage, or a draft over 200,000 characters, keeps the draft in memory for the
life of the page and the box says "This draft cannot be kept on this device, so it
will not survive a reload." A reconnect never touches it: the box is not
unmounted by the connection.

*What is stored, and for how long.* Per session: the text, the staged file refs (name, size, ref: not the bytes) and
the time it was last edited, nothing else. It is removed when its send is accepted (above), when the box empties, and
when the credential is forgotten: the web app's `pairForget` (`app.js`) removes every `swe.sv.draft.v1:` key with the
saved pairing, and a `storage` event for the pairing's key being removed, or `localStorage.clear()`, in another tab
does the same (`dropDraftsOnSignOut`); a box that is showing a draft at that moment forgets its text and its pending save, so
nothing is written back (`onDraftsCleared`). A draft last edited more than 14 days ago is dropped when it is read, also the copy a page holds in memory (it is
timed too), and by a sweep when the box opens (`sweepDrafts`). A draft with no time on it is treated as expired.

**Offline and storage down.** The box stays usable offline: a message is accepted,
kept, and its bubble says "Waiting: you are offline. It will be sent when you are
back." (`describeSends`, unchanged). The banner under the header already says it
for every state after the session has loaded, so the composer repeats it only
before that (the `connecting` state). Gated actions (an answer, Stop, the tray)
are off with `store.refusal()`'s message. When the outbox's storage does not
survive a reload the box shows the outbox's own note ("IndexedDB failed (...).
Sends saved before are not sent until it answers again, and new ones are kept in
memory and will not survive a reload until then"), and a stuck send shows "Not
sent yet: this device's storage did not answer, and sending now could send it
twice. Trying again." in the timeline, as in G12.

**Read-only.** A session whose row says `control` is not `full` shows a bar with
`readOnlyReason(row)` in place of the box, the dock and the tray. "Continue in
SWE" is G21's.

## The dock

The oldest open request in `session.pending` is docked above the box; others wait
behind it ("2 more waiting"). An approval shows its prompt, its answers from
`options` (or Allow and Deny when it has none), and the tool call it asks about
(found by `call_id`) under a "Details" disclosure. An `input.requested` of kind
`choice` shows its options. Nothing in the dock takes focus: a key the person is
typing never lands on an answer, and the destructive answer (Deny, Skip) is never
the one focus would start on.

**An approval answers exactly the request shown.** A request is named to the hub
by its id alone, and an agent that restarts counts its ids again (G11: the fake
agent reuses `perm-1`). Each press carries the request's `seq`; `answer()` looks
the id up again at press time, takes the latest open request with that id, and
refuses with "That request changed while you were looking. Nothing was sent: read
the new one." (or "no longer open") unless its `seq` is the one shown. Check and
post run in one synchronous step. Every request that
newly becomes the shown one, the first or one that replaces another, keeps its answers off for `SETTLE_MS` (500 ms, one
named constant, the composer's default `settleMs`), shown as a state: the buttons are dimmed and the line under them
says "A new request just appeared. Read it before you answer." A dock appears over the page and pushes it up, so a press
aimed at the tray strip or a line of the timeline can arrive under it; in real Chrome one 60 ms later posted a deny
(G13 review, scenario D). A request already open when the page loads is treated the same. A press also has to be its own
(`press.ts`): a click from a pointer counts only if that pointer went down on that button after it could be pressed; a
click with no pointer (a key's, or a screen reader's or voice control's bare activation) counts unless it comes from a
key that went down on the button while it could not be pressed and is held, or was let go, after the wait (a Space's click
follows its key's release in the same turn); a key repeat is never a press. The in-flight set in `answer` takes one answer at a time per request (the dock keeps no
ref of its own: a second press that gets that far is refused as "busy" and shows nothing), and after the first the
answers stay off ("Answer sent. Waiting for the agent.") until the request ends, so two devices are not both
answered by one device.

**What is sent.** An option whose id is `allow`, `always` or `deny` sends that
word; any other option sends its 1-based place in the request's full option list
(the hub reads it that way, `runtime.respond`), also when the list is cut. A
standing option (`standing: true`, G16 gap 9) is worded "Always allow" when the
hub's own label is just "Always" (any other label is the hub's, cut and not
changed), and takes a second press. The first press changes the button to "Press
again to always allow", which stays off for `SETTLE_MS` ("One moment, then press again." says so), so the second click of a
double click, which lands on the new button, grants nothing. The second press must also be a press of its own (above), so a press held
through the wait or a Space let go late confirms nothing, while a bare click from assistive technology, after the wait, does. The note beside it says what the second press does: "Always allow Bash?
Pressing again turns off later questions of the same kind for this session.
Cancel to go back." (the tool is named from the call the request asks about, else
"this kind of call"). Only the first allowing option is filled.

**Layout: what the agent writes scrolls, the answers do not.** The dock is a column: the request's text (kind, prompt,
details, the agent's own choices, "N more options") is one scroll area (`min-h-0 flex-1 overflow-y-auto`), and under it the
decisions (Allow, Always allow, Deny, Skip), the standing note and the status are pinned (`shrink-0`) and visible
together at every width, whatever the text holds. The decisions are the options whose id is `allow`, `always` or `deny`; their
buttons say the hub's short word, or ours ("Allow", "Always allow", "Deny") when the label is over 40 characters.
Every other option is the agent's own choice: a full-width dashed button, numbered ("1."), its label cut to two lines
with the whole label (to 300 characters) in its tooltip and accessible name ("Option 2: ..."), and, when any is
over 80 characters, listed in full under "Options in full". A choice labelled "Allow" or "Deny" therefore never looks
like an answer: it is in the scroll area, dashed, numbered. The stack above the bar shrinks the dock first.

**A question in words cannot be answered.** `input.requested` of kind `text` has
no route that carries text (G11 "Schema gaps"). The dock says "This view cannot
send a typed answer yet. Answer it in the terminal, or skip the question." and
offers **Skip this question**, which sends `deny`. Nothing pretends to send words.

**Text from an agent** (the prompt, the tool name, the tool input, the options'
labels, a queued message) is drawn as text in a bounded number of nodes, never as
HTML. The prompt goes through `InlineText`: a run of backticks and what it encloses is drawn as code, with the
backticks kept, and nothing else is read: no bold, no italic (`rm *.log *.txt` is not italics: an approval must show
what will run), no link, no heading, no table. Every character that arrived is drawn. Only the first 2000
characters of the prompt are looked at for code spans, which caps its nodes at about 700; the rest is one text node. The other texts are plain. The prompt is cut at 6000 characters or
300 lines, whichever comes first, with "N more characters not shown."; the tool
input is JSON in one `pre` cut the same way, only once opened; the tool name at
120 characters; an option label at 300 (with an ellipsis); at most 20 options with
"N more options are not shown. Answer in the terminal."; a queued message at 2000
characters; at most 20 queued messages. The dock sits in the stack that is held to half of the view, and the dock gives (its text scrolls) before its answers do, so a
long request cannot push the bar or the answers off a phone. A letter that is shown is the
letter that arrived. Nodes: a request is about 20 whatever its size, plus one
button per option (at most 20). Each dock, the tray, each queued row and the whole
composer sit in a `Guard` (an error boundary) whose message says what part failed;
a request that throws costs the dock only, and the next request draws.

## The tray

`session.queue` is the hub's list, drawn as a compact glass strip just above the
bar ("Queued: 2 messages", with a chevron that points up while closed) that
opens upward into the list. The list is not made until the strip is first opened
(a long queue costs nothing closed), has its own height (`max-h-44`, `max-h-72` while a message is being edited, which needs the room) and its own
scroll, and stays open under the person's hands while one of its messages is being
edited. Each item has icon buttons with names and tooltips: **Edit**, **Remove**
(`cancel`), and **More**, whose menu holds the steer item (`steer`, worded as
above; disabled, with its reason, when `caps.steer` is not true). Edit opens the
message in place with **Save the edit** and **Cancel the edit** (icons). All are
gated actions: off, with the store's reason, while the connection is down. One
press is one request (a ref per row). **Remove** is two presses, the same as "Always allow" and under the same
rule: the first shows "Press again to remove" (and "Keep it"), off for `SETTLE_MS`, the second counts only as a
press of its own (`press.ts`); a message is typed text and is not taken out by a double click. A save the hub refuses keeps the editor
and the person's words, with the hub's reason. An edit remembers the text it started from. If another device changes the message meanwhile, the editor shows the new text
under "This message was changed on another device while you were editing it", **Save** is off, and the person chooses
**Use the new text** or **Keep my words** (which then saves over it, knowingly). A message that left the queue (sent
or taken out) while its edit was open leaves the edited words in a card ("This message was sent or taken out of the queue
before your edit was saved. Your words are here.") to move into the message box or let go; they are never dropped quietly. The
tray's error boundary, and each row's, try again when the queue (or that row's text) changes. Closing a message's menu returns the
focus to its More button, as the bar's menus do. A send
that is in the queue is drawn by the tray, not also as a bubble in the timeline.

## What steer does, per agent

Claude's steer cuts the reply and kills a running tool call, an open approval is withdrawn. Codex's cuts the reply but
lets a running command finish, and an open approval holds the steer back. opencode's cuts nothing: the steer lands
when the current step ends, whether that is the reply or a command. ACP has no steer (G16 gap 6; G7). `Caps.steer` is a boolean, true for
the first three alike, and the view must not branch on a harness name (AGENTS.md: capabilities first), so the hub says
which it is: `session.caps.steer_effect`, one of `"stops_reply"`, `"after_tool"`, `"next_turn"` (added by G32; read
here as an optional field, `steer.ts`, and not in the generated types, which the hub's schema owns). The control reads
it:

| `steer_effect` | Label | Sentence |
|---|---|---|
| `"stops_reply"` (Claude, Codex) | Stop and send | Stops the reply being written and sends this message now. A command that is already running may keep going. |
| `"after_tool"` (opencode) | Send next | Sends this message when the current step ends. Nothing is stopped. |
| `"next_turn"` (no adapter sends it today) | Send next | Sends this message when the reply ends. Not offered: see below. |
| absent, or a value this view does not know | Send now | Sends this message ahead of the queue. Some agents stop the current reply to do it. |

A steer that lands when the reply ends (`next_turn`) is what Queue does, so it is not offered: the send menu does not open
(the main button queues, as before) and a queued message has no "More" button. `steerWording` keeps its words for the day an
adapter sends the value and the item means something else.

While a request (an approval or a question) is waiting, the two sentences that cannot rule it out (`stops_reply` and
absent) add: "A request waiting for your answer may be withdrawn, or may hold this message back until you answer
it." That is true of Claude (withdrawn) and of Codex (held back). The store does not stop a steer for a waiting
request, it posts it and the hub decides; `after_tool` and `next_turn` say nothing about it because nothing known
says what they do there.

The label and the sentence are the steer item's in the send menu (with its picture: a square for a stopping steer, the
queue picture otherwise), its tooltip, and the item in a queued message's menu. Only a session that says `stops_reply`
is ever told that the reply is stopped. The sends are the same whatever the words (`mode: "steer"`, `queue {op:
"steer"}`). `steer.test.tsx` has the four cases for the send menu and for the tray, an unknown value, and the reader;
the screenshots `composer-menu-send` (absent), `composer-menu-steer-stops`, `-steer-after-tool`, `-steer-next-turn` and
`composer-queued-menu` show each.

## Left out, and why

What each would need, so the next milestone can pick it up. For the model chip: the store action `setOption(name, value)` below; the current model, from a new `value` on each option of `session.caps` (or a `session.options` event) so the chip can show it; and the list of models, which the hub has only for ACP agents (kept in `AcpState.models`) and as `provider/model` strings for opencode, and not at all for Claude or Codex (their lists are the harness's own, G7 and G16): a field `models: [{id, label, default?}]` on the same event, filled by each adapter.

| Left out | Why | What it needs |
|---|---|---|
| Model and effort pickers (the chip) | the slot is `ComposerProps.chip`; `caps.options` lists the option names a session can set (`model`, `permission_mode`, ...), but the store has no action that sets one, and `src/core/**` was not this milestone's to change | a store action `setOption(name, value)` that posts `POST /v1/sessions/{key}/control {op: "set_option", name, value, command_id}` (the hub's `_KEPT_OPS` already has `set_option` with `name` and `value`); the values to offer: Claude has no list (G7), ACP agents list their own modes and models (`AcpState.modes`, `.models`) and no v2 event carries them yet, opencode models are `provider/model`, Codex `permission_mode` is `untrusted`, `on-request` or `never` (G16): a new field on `session.caps` or a `session.options` event |
| Plan toggle | the same missing action; G20 owns plan mode | `setOption("plan_mode", true|false)` through the same route, shown only when `caps.plan_mode` |
| Queue reorder | the hub has `edit`, `cancel` and `steer` on a queued message and nothing that moves one | a hub op `queue {op: "move", client_id, before}` in `hub.py`'s queue handler and the keeper's queue, then `queueAction("move", id, ...)` in the store |
| A typed answer to a question | no route carries the text | `POST .../approval {request_id, text}` reading `text` into the adapter's `Answer.text` (the field exists in `adapters/base.py` and `claude_wire.answer` uses it), and `respond(requestId, decision, text?)` in the store |
| Slash commands and `@` files | not in this milestone; the placeholder no longer promises them | G20/G21's |
| "Allowed on iPhone" captions | | G21 |
| t3code's `ComposerPendingApprovalActions` | not copied (no checkout); written from the description in G1 | |

## Limits

1. **The box is locked while a message is being saved.** If `store.send` never
   settles (G11 Limit 4, Safari's IndexedDB open that never answers) the box
   stays read-only with the draft in it. `loading` is checked first, which is
   the same wait, so the box is refused with a reason before it gets that far;
   a hang inside the first write itself is not timed out here.
2. **A reload between the outbox taking a message and the draft being cleared**
   (a window of a few microtasks) brings the draft back beside the sent
   message. The draft is cleared after, never before, so the other order cannot
   lose a message.
3. **Two tabs.** A draft is read once, when the box mounts. A tab that sent a
   message clears the stored draft, and another tab that still holds the same
   text in its box writes it back on its next keystroke.
4. **The answer names the id, not the instance.** The `seq` check closes the
   window between what was drawn and the press; it cannot close the round trip
   after the post leaves, where an agent that reuses an id between the post and
   its delivery gets the answer. The hub would need to take the `seq` too.
5. **Steer wording depends on a field the hub adds.** `steer_effect` is read when present (G32); a hub that
   does not send it gets "Send now" with the sentence that is true of every agent, which undersells a steer that does stop the reply.
6. **Attachments are not checked for the harness.** The hub stages any file of an
   allowed type; whether the harness can read it is `attachments.capability`'s
   (G8), which the view does not know.
7. **`session.row` with no `row`.** The core stores `undefined` for a `session.row`
   frame whose `data` has no `row`, which makes `readOnlyReason` and the header
   read a field of `undefined`. The composer guards its own read (`?? null`); the
   header (G12) and the core (G11) do not.
8. **Nothing sets `--keyboard-inset` yet.** The bar rides it when a shell sets it
   (the iPhone embed, G18, which also has to decide the hardware-keyboard case); in a
   plain browser it relies on the browser resizing the viewport, which `bottom: 0`
   of an absolutely placed host follows. The screens simulate the variable
   (`composer-keyboard`).
9. **jsdom, not a browser.** As in G12, the tests run in jsdom and the screenshots
   are for a person to read. Real IME input and real pointer timing were exercised
   only in the end-to-end run's Chrome (key press, click), not with an input method.
10. **Glass costs a blur.** `backdrop-filter` over a scrolling timeline repaints the bar's backdrop as the page moves; on a very old phone this is the first thing to turn off (`prefers-reduced-transparency` does). The bar and its docks are each one blurred surface, not one per item.
11. **Hold on a touch screen is a timer.** 450 ms, not a platform gesture: a finger that slides off the button cancels it (`pointerleave`), and the menu is chosen with a second tap, not by sliding to an item. iOS Safari was not tried with a real finger; the tests drive pointer events.

## Screens

`pnpm exec vite --config src/session/screens/vite.config.ts`, then
`node src/session/screens/shoot.mjs <dir> composer-empty ...`. Scenes:
`composer-empty`, `-typing`, `-multiline`, `-sending`, `-busy`, `-queued`,
`-queued-edit`, `-queued-many`, `-approval`, `-approval-confirm`, `-approval-offline`, `-input-text`, `-input-choice`,
`-offline`, `-storage-down`, `-loading`, `-no-steer`, `-read-only`,
`-attachments`, `-hostile`, `-menu-send` (steer_effect absent), `-menu-steer-stops`, `-menu-steer-after-tool`, `-menu-steer-next-turn` (no menu opens: nothing to show but the bar), `-menu-steer-approval` (the steer menu with a request open), `-menu-send-multiline` (the menu above the whole bar, over a draft of five lines), `-queued-conflict`, `-queued-remove`, `-hostile-labels`, `-queued-menu`, `-error-note`, `-approval-error`, `-menu-attach`, `-over-code` and `-over-diff` (the bar over a long scrolled timeline, under a code block and under a red diff line), `-keyboard` and `-keyboard-approval` (the phone with `--keyboard-inset` at 336 px, without and with a dock); each at 1280x800 and a 390x844 phone, light and dark.
`scenes.ts` scripts each over the recorded fixtures plus a few hand-built frames,
and clears the stored draft first so one scene does not show another's.

## Dependencies

None added.

## Verification

Run on 2026-11-22, macOS 27.3, Node 22.23.1.

| Check | Result |
|---|---|
| `pnpm typecheck`, `pnpm lint:tokens`, `pnpm test` (37 files, 741 tests; the composer's are in `src/composer/*.test.ts(x)`), `pnpm build` | pass |
| The built files under `src/swe/webapp/static/session/` equal a fresh build; `tests.test_webapp_session_view` | pass |
| **Each guard, removed.** 146 one-line edits of the source, each followed by the composer's tests and `SessionView.test.tsx`: every guard in the "Message safety" table (the store's in-flight set, the box's ref, the loading and length refusals, the steer downgrade, read-only while saving, clear only on success, the draft write on unmount and in `clear`, the four IME and key rules), every dock guard (the `seq` check, the latest request with an id, the in-flight set, the settle delay, offline and sent staying off, the standing confirmation, the option, label, prompt and input cuts, the deep-input catch), every tray guard (press ref, orphans, offline, no-steer, the cap), the error boundary, the timeline's queued-bubble rule and the read-only bar | every edit makes at least one test fail. The first 46 covered the guards above; 11 more covered the inline reader (code spans only, every character kept, the read cap), the standing wording and its second press, the tool named in it, and the layout contract (the stack and tray caps, icon-only buttons); 19 more covered the bar (the hold and its swallowed click, the menu's keys, focus and disabled items, the touch-only photo item, the file slots, draft ownership and the newer-in-memory copy, a replacement after a request left, the closed tray, the timeline's end padding, the floating host, the solid fallback); 10 more covered the steer wording (the reader, each of the four cases, the tray's read) and the rule that no danger-coloured text goes on the glass. Four edits first survived (the last rounds added 5 more edits, for the keyboard-opened menu, the code-span reader and the host's resize watch, all killed) (a double press in the dock and its `answer()` guard covered each other; "latest request with an id" could not be reached through the reducer; a "busy" message hidden behind "Answer sent"; the idle right click, which the menu's own close-on-idle hid): the dock's own ref was dropped in favour of `answer()`'s set, the id case is built by hand, and the message now wins over "Answer sent", the idle test reads whether the browser's own menu was left alone, and all four then failed their edits. The strong-tier review round added 41 more edits: the dock's settle for every request, the confirm's wait and its press rules (`press.ts`), the menu's key rules (safe first focus, repeats, the key held from opening), the three pointer flags, the tray's two-press Remove, edit conflict and boundary reset, the draft's age, sweep and sign-out, the timeline row's clip, the stopped mark, the untrue steer sentence and the label's text; a later round added 11 for the hold's blur, the border-box observer, the sign-out reset, the memory copy's age, the menu's focus return and the assistive and held-key clicks. Three survived first: a `confirmReady` check that the disabled button already covers (removed), a pointer going down on a button that is off, and a cancelled pointer (the 80 ms timer also cleared it); the last two now have tests that fail without them. |
| **A real hub.** `swe remote serve` on a throwaway HOME, harness.json pointing Claude's adapter at `tests/fakes/fake_claude.py` in `approval` mode, headless Chrome on the built page (`/session/index.html?key=...`) with the saved credential: the first turn's approval shows in the dock with nothing focused; a real mouse click on Allow answers it; text typed with the keyboard and sent with Enter arrives once; the second turn's approval is answered with a click. The hub's v2 messages hold the typed message once (`["list the build folder","now list it again please"]`) and both approvals as `source user, decision allow` | pass; screenshots `G13-e2e/` in the shared notes folder, script `G13-e2e_view.mjs` (run with `swe` as `PYTHONPATH=src python3 -m swe.cli`, since this was a worktree) Re-run on the review round's build: scenario D (a real mouse press on Deny right after the dock appeared answers nothing, the hub sees no approval) and scenario E (while an approval is open, a message queued with Enter, then a held and repeating Enter on the tray's More button: the menu opens with the focus on the menu, the hub's queue still holds the message), then three approvals answered and three messages arriving once each. |
| Screenshots, 37 composer scenes x desktop 1280 and phone 390 x light and dark (148 files; the review round's new scenes are `-menu-steer-approval`, `-menu-send-multiline`, `-queued-conflict`, `-queued-remove`, `-hostile-labels`) | read; `G13-screens-glass/` in the shared notes folder. Findings fixed on the way: a request with many options pushed the answers off the screen (the stack above the bar now scrolls inside half the view), a long label overflowed its button, a long tool name overflowed its disclosure, a typed draft from one scene showed in the next (scenes clear it), the offline sentence was said twice (the banner and the box), "Question" was said twice in a question's header |
