# G13: composer, approvals, queued tray

Status: built. Everything under the timeline in the session view
([G12](G12-timeline.md) drew the timeline and left a disabled box): the message
box with Send, Queue, Stop and "Stop and send", attach, the approval and question
dock, and the queued tray. The data layer is [G11](G11-client-core.md) and is
unchanged: the composer imports `src/core/index.ts` and nothing else from the
core. The view is still behind `remote.session_view`, which is off by default.

| File (under `app/web-session/src/composer/`) | What it holds |
|---|---|
| `Composer.tsx` | the footer: dock, tray, notes, the box and its buttons; a bar in place of all of it for a read-only session |
| `submit.ts` | `submitMessage`: the one place a message goes to the store, with its guards |
| `keys.ts` | `enterAction`: what Enter means (pure) |
| `draft.ts`, `useDraft.ts` | the draft kept in storage, and the hook that writes it |
| `RequestDock.tsx`, `answer.ts` | the approval and question dock, and `answer`: a press that lands on the request shown |
| `QueueTray.tsx` | the tray: edit, remove, send now |
| `AnswerButton.tsx`, `Guard.tsx`, `text.ts` | the button for answers, the error boundary, the cut and clip rules for text from an agent |
| `upload.ts` | staging a file on the hub (`POST .../attachments`) |
| `scenes.ts` | the composer's scenes for the screens page (dev only) |
| `harness.tsx`, `*.test.ts(x)` | the tests |

Edits outside `src/composer/`, each the smallest that mounts the composer:

| File | Edit |
|---|---|
| `src/session/SessionView.tsx` | mounts `<Composer>` in a `Guard` where `ComposerPlaceholder` was; `ComposerPlaceholder.tsx` is deleted |
| `src/session/index.ts` | `COMPOSER_PLACEHOLDER` now comes from `../composer` |
| `src/session/Timeline.tsx` | a send the hub holds in `session.queue` is left out of the timeline, since the tray draws it (G11: "draw that instead of the pending bubble") |
| `src/session/model.ts` | a comment only |
| `src/ui/Icon.tsx` | three icons: `attach`, `stop`, `edit` |
| `src/session/screens/main.tsx`, `shoot.mjs` | the composer's scenes (`composer-*`) |
| `SessionShell.test.tsx`, `SessionView.test.tsx` | the tests that described the placeholder now describe the composer |

`src/core/**` and `src/swe/remote/` are untouched. `THIRD_PARTY_NOTICES.md` is
unchanged: **nothing is copied from t3code** (the checkout is still not
available), the dock and the tray were written for this repository.

## The message box

A plain `<textarea>`, no rich-text editor. It grows with its text up to 240 px,
then scrolls. The primary button reads **Send** when the session is idle and
**Queue** while a reply is in progress (`openTurn` is set, or the phase is
`running` or `waiting`); both post `mode: "queue"` through `store.send`, so the
message is kept on the device first (G11's outbox) and the timeline draws its
bubble and its reason. Enter sends, Shift+Enter adds a line. On a touch screen
(`pointer: coarse`) Enter adds a line and Ctrl or Cmd+Enter sends.

While a reply is in progress two more buttons appear:

- **Stop** posts `interrupt` (a gated action: off, with the store's reason as its
  title, while the connection is down or has not caught up). After a successful
  press it reads "Stopping" until the turn ends, or for at most five seconds.
- **Stop and send** is G13's label for steer. It says what it does: "Stops the
  current reply, then sends this message now." That sentence is true on every
  harness (G16 gap 6: a Codex steer does not stop a running command, an opencode
  steer does not cut the reply being written, Claude kills the running call);
  nothing stronger is said. It is disabled, with the reason beside the box, when
  `caps.steer` is not true ("This agent cannot take a message while it is
  replying. Queue it to send when the reply ends."), which is also the case of an
  ACP agent. If the turn ended between the button being drawn and pressed, the
  message goes as a plain send: the hub refuses a steer with no turn open
  (`idle`, G16), and "send now" is what the person asked for.

**Attach** opens the file picker; each file is posted to
`/v1/sessions/<key>/attachments?name=...` as raw bytes with the same credential
as the stream (the store has no action for it, so the box does the request,
`upload.ts`), and the hub's `ref` goes in the message's `attachments`. A file
shows as a chip while it uploads, with the hub's reason if it fails (a failure
never stops a message without the file). A message takes at most ten; the hub
caps a file at 20 MB and takes image, PDF and text types only, and says so with a
422 that the chip shows. Send waits while an upload is open. A queued message's
attachments show as a count in the tray.

## Message safety

One press sends once. Each guard below is tested, and each test was run against
a copy of the code with that guard removed and fails there (see Verification).

| Case | Guard |
|---|---|
| Double click, or two presses in one task | `Composer.submit` takes a ref (set before the first `await`) and returns while it is held; the button's `disabled` waits for the next render and would not stop it |
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
post run in one synchronous step. A request that replaces the one on screen (a
different request within five seconds of the last) keeps its answers off for
350 ms ("A new request just appeared. Read it before you answer."), so a press aimed at
the old one cannot land on the new one. A press is also taken once (a ref in the
dock and a set in `answer`), and after it the answers stay off ("Answer sent.
Waiting for the agent.") until the request ends, so two devices are not both
answered by one device.

**What is sent.** An option whose id is `allow`, `always` or `deny` sends that
word; any other option sends its 1-based place in the request's full option list
(the hub reads it that way, `runtime.respond`), also when the list is cut. A
standing option (`standing: true`, G16 gap 9: "Always allow") takes a second
press ("Yes, always allow", with a sentence saying it turns off later questions).
Only the first allowing option is filled.

**A question in words cannot be answered.** `input.requested` of kind `text` has
no route that carries text (G11 "Schema gaps"). The dock says "This view cannot
send a typed answer yet. Answer it in the terminal, or skip the question." and
offers **Skip this question**, which sends `deny`. Nothing pretends to send words.

**Text from an agent** (the prompt, the tool name, the tool input, the options'
labels, a queued message) is drawn as text in a fixed number of nodes and never
parsed: no Markdown, no links, no HTML. The prompt is cut at 6000 characters or
300 lines, whichever comes first, with "N more characters not shown."; the tool
input is JSON in one `pre` cut the same way, only once opened; the tool name at
120 characters; an option label at 300 (with an ellipsis); at most 20 options with
"N more options are not shown. Answer in the terminal."; a queued message at 2000
characters; at most 20 queued messages. The dock scrolls inside 18 rem (the prompt inside 8), so a
long request cannot push the answers off a phone. A letter that is shown is the
letter that arrived. Nodes: a request is about 20 whatever its size, plus one
button per option (at most 20). Each dock, the tray, each queued row and the whole
composer sit in a `Guard` (an error boundary) whose message says what part failed;
a request that throws costs the dock only, and the next request draws.

## The tray

`session.queue` is the hub's list. Each message can be **edited** in place (Save
posts `queue {op: "edit", text}`), **removed** (`cancel`), or **sent now**
("Stop and send": `steer`, labelled as above; disabled when `caps.steer` is not
true, with a sentence saying why). All are gated actions: off with the store's
reason while the connection is down. One press is one request (a ref per row). A
save the hub refuses keeps the editor and the person's words, with the hub's
reason. A message that left the queue (sent, or removed on another device) while
its edit was open leaves the edited words in a card to move into the message box
or let go; they are never dropped quietly. A send that is in the queue is drawn by
the tray, not also as a bubble in the timeline.

## Left out, and why

| Left out | Why |
|---|---|
| Model and effort pickers, plan toggle | `caps.options` and `caps.plan_mode` are readable, but the store has no action that posts `control {op: "set_option"}`, and `src/core/**` was not this milestone's to change. No v2 event lists an ACP agent's modes or models either (G1, G20). Nothing is drawn rather than a control that does nothing. G20 owns plan mode |
| Queue reorder | the hub has `edit`, `cancel` and `steer` on a queued message and no reorder |
| A typed answer to a question | no route carries the text (above) |
| Slash commands and `@` files | not in this milestone; the placeholder no longer promises them |
| "Allowed on iPhone" captions | G21 |
| t3code's `ComposerPendingApprovalActions` | not copied (no checkout); written from the description in G1 |

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
5. **Steer's one sentence is a minimum.** On Claude "Stop and send" also cancels a
   running tool call; the label does not say so, because it is not true of Codex
   and opencode (G16).
6. **Attachments are not checked for the harness.** The hub stages any file of an
   allowed type; whether the harness can read it is `attachments.capability`'s
   (G8), which the view does not know.
7. **`session.row` with no `row`.** The core stores `undefined` for a `session.row`
   frame whose `data` has no `row`, which makes `readOnlyReason` and the header
   read a field of `undefined`. The composer guards its own read (`?? null`); the
   header (G12) and the core (G11) do not.
8. **No virtual keyboard handling.** On a phone the box sits at the bottom of the
   page and relies on the browser resizing the viewport; the `--keyboard-inset` of
   G18 is not used yet.
9. **jsdom, not a browser.** As in G12, the tests run in jsdom and the screenshots
   are for a person to read. Real IME input and real pointer timing were exercised
   only in the end-to-end run's Chrome (key press, click), not with an input method.

## Screens

`pnpm exec vite --config src/session/screens/vite.config.ts`, then
`node src/session/screens/shoot.mjs <dir> composer-empty ...`. Scenes:
`composer-empty`, `-typing`, `-multiline`, `-sending`, `-busy`, `-queued`,
`-queued-edit`, `-approval`, `-approval-offline`, `-input-text`, `-input-choice`,
`-offline`, `-storage-down`, `-loading`, `-no-steer`, `-read-only`,
`-attachments`, `-hostile`; each at 1280x800 and a 390x844 phone, light and dark.
`scenes.ts` scripts each over the recorded fixtures plus a few hand-built frames,
and clears the stored draft first so one scene does not show another's.

## Dependencies

None added.

## Verification

Run on 2026-11-22, macOS 27.3, Node 22.23.1.

| Check | Result |
|---|---|
| `pnpm typecheck`, `pnpm lint:tokens`, `pnpm test` (33 files; the composer's tests are in `src/composer/*.test.ts(x)`), `pnpm build` | pass |
| The built files under `src/swe/webapp/static/session/` equal a fresh build; `tests.test_webapp_session_view` | pass |
| **Each guard, removed.** 46 one-line edits of the source, each followed by the composer's tests and `SessionView.test.tsx`: every guard in the "Message safety" table (the store's in-flight set, the box's ref, the loading and length refusals, the steer downgrade, read-only while saving, clear only on success, the draft write on unmount and in `clear`, the four IME and key rules), every dock guard (the `seq` check, the latest request with an id, the in-flight set, the settle delay, offline and sent staying off, the standing confirmation, the option, label, prompt and input cuts, the deep-input catch), every tray guard (press ref, orphans, offline, no-steer, the cap), the error boundary, the timeline's queued-bubble rule and the read-only bar | every edit makes at least one test fail. Three edits first survived (a double press in the dock and its `answer()` guard covered each other; "latest request with an id" could not be reached through the reducer; a "busy" message hidden behind "Answer sent"): the dock's own ref was dropped in favour of `answer()`'s set, the id case is built by hand, and the message now wins over "Answer sent", and all three then failed their edits |
| **A real hub.** `swe remote serve` on a throwaway HOME, harness.json pointing Claude's adapter at `tests/fakes/fake_claude.py` in `approval` mode, headless Chrome on the built page (`/session/index.html?key=...`) with the saved credential: the first turn's approval shows in the dock with nothing focused; a real mouse click on Allow answers it; text typed with the keyboard and sent with Enter arrives once; the second turn's approval is answered with a click. The hub's v2 messages hold the typed message once (`["list the build folder","now list it again please"]`) and both approvals as `source user, decision allow` | pass; screenshots `G13-e2e/` in the shared notes folder, script `G13-e2e_view.mjs` (run with `swe` as `PYTHONPATH=src python3 -m swe.cli`, since this was a worktree) |
| Screenshots, 18 composer scenes x desktop 1280 and phone 390 x light and dark (72 files) | read; `G13-screens-final/` in the shared notes folder. Findings fixed on the way: a request with many options pushed the answers off the screen (the dock now scrolls inside 18 rem), a long label overflowed its button, a long tool name overflowed its disclosure, a typed draft from one scene showed in the next (scenes clear it), the offline sentence was said twice (the banner and the box), "Question" was said twice in a question's header |
