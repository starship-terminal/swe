# G11: client core (reducer, supervisor, outbox, store)

Status: built, with no UI. It is the session view's data layer: the part
that turns the hub's v2 stream
([G9-sync-v2.md](G9-sync-v2.md)) into state a view can draw, keeps the
stream open, and keeps what the person typed until the hub has it. The
timeline (G12) and the composer (G13) import the store and nothing else
from it. The design is G1 section 3, "Client"
([G1-session-runtime-redesign.md](G1-session-runtime-redesign.md)); the
toolchain it builds in is G10
([G10-session-view-toolchain.md](G10-session-view-toolchain.md)).

| File (under `app/web-session/`) | What it holds |
|---|---|
| `scripts/gen-types.mjs` | makes `src/core/generated/session-v2.ts` from `src/swe/remote/schema/session_v2.json`; `pnpm gen:types`, `--check` |
| `src/core/generated/session-v2.ts` | the wire types and the schema's `x-swe` constants (`PROTOCOL`). Generated, never edited |
| `src/core/types.ts` | `FrameOf`, `seqOf`, `isFrame`, `codePointLength`, and re-exports of the generated names |
| `src/core/state.ts` | `SessionState`, the timeline item types, `emptySession` |
| `src/core/reducer.ts` | `reduce(state, frame)`: the rules of G9's "What G11 must know" |
| `src/core/transport.ts` | the `Transport` interface, the fetch-stream implementation, the SSE parser |
| `src/core/supervisor.ts` | the connection supervisor: states, backoff, silence, wake |
| `src/core/outbox.ts`, `outbox-storage.ts` | the send outbox, its IndexedDB and memory storages |
| `src/core/gate.ts` | when an action needs a live connection, and the reason it gives when refused |
| `src/core/store.ts` | `createSessionStore`, `useSessionStore`: the zustand store that ties the above together |
| `src/core/index.ts` | what G12 and G13 import |
| `src/core/test-support.ts` | fake timers, transport, environment and the fixture loader (tests only) |
| `scripts/record_session_view.py` | records the fixtures from the hub's own v2 stream |
| `tests/fixtures/session-view/*.json` | the recordings |
| `tests/test_session_view_fixtures.py` | records again and holds the committed files equal to it |

## Types

`pnpm gen:types` reads `session_v2.json` and writes
`src/core/generated/session-v2.ts`: one `export type` per `$defs` entry
(`Frame`, `Snapshot`, `Row`, every `<Type>Event` and `<Type>Data`) and the
`PROTOCOL` constant from `x-swe` (`ping_s`, `silence_s`, `replay_max`,
`snapshot_messages`, the feature names). The generator is about 100 lines
with no dependency. It reads the part of JSON Schema the hub's schema
uses and refuses anything else, so a new construct fails loudly instead
of turning into a wrong type. `generated.test.ts` fails while the
committed file is not what the schema makes now: after a change to
`events.py`, run `python3 -m swe.remote.schema --write`, then
`pnpm gen:types`, and commit both.

`Frame` is the union a stream sends, discriminated by `type`.
`FrameOf<"message.delta">` names one member.

## The reducer

`reduce(state, frame) -> {state, effects}` is pure: it never changes the
state it is given, and shares every array and object a frame did not
touch. `effects` is work only the store can do (`fetch_snapshot`).

What it holds (`SessionState`):

| Field | From |
|---|---|
| `cursor`, `epoch`, `bootId` | the highest journaled `seq`, raised to the `head` of every state frame and ping; the epoch of the snapshot or state frame; the snapshot's `boot_id` |
| `loaded`, `caughtUp` | something to draw has arrived; the state frame that ends a catch-up has arrived on this connection |
| `row` | `session.snapshot`, `session.row` |
| `phase`, `phaseReason`, `harnessSessionId` | `session.state` (journaled, or the catch-up's) |
| `caps` | `session.caps`, the snapshot |
| `openTurn`, `turns[]` | `turn.started`, `turn.ended` |
| `items[]` | the timeline, one item per `seq`, in `seq` order: `message`, `thinking`, `tool` (start, update and completion in one item, with `fileEdits`), `request` (an approval or question with its `resolution`), `compacted`, `error`, `checkpoint` |
| `live[]`, `stopped[]` | the text in progress, and the ids a hole stopped |
| `pending[]` | the requests still open, as their events |
| `plan`, `tasks[]`, `checkpoints[]`, `changes`, `queue[]`, `usage` | their events, the last one winning |
| `commands` | receipts and results by `command_id`, the newest 200 |
| `lastNotice` | the last `notify`, numbered |
| `hasMore`, `nextBefore` | whether older timeline exists, and the `before` for it |

The rules:

| Frame | The reducer |
|---|---|
| `session.snapshot` | replaces everything the session holds. Keeps `commands`, `lastNotice` and `caughtUp`, which are about the client. Its `pending[]` is the list of open requests, and a request the messages show answered is not open |
| `session.state` with `live` | the state frame that ends every catch-up: takes `state`, replaces the live text with `live[]` (and clears `stopped`), drops the request cards it does not list (marks them `dropped`; ids it does not know are left alone), raises the cursor to `head`, marks the connection caught up. An `epoch` it does not hold asks the store for a snapshot |
| `session.state` journaled | takes `state`; ends all live text for idle, agent_down, parked, closed |
| `session.row` | replaces the row |
| `ping` | raises the cursor to `head` |
| `message.delta` | the delta rule below |
| journaled frames | applied once, whatever they change: a repeat of a frame already applied changes nothing anywhere (the timeline, turns, live text, tasks, checkpoints, requests, the state events below), and one that arrives out of order, below the cursor but not seen before, is applied in its place. `seq` gaps are fine. The cursor is the highest `seq` seen even for a frame it skipped. A `seq` of 0 never moves it. The rule is per event, not "at or below the cursor changes nothing", because the brief asks for out-of-order frames to be handled; `reducer.test.ts` applies every journaled frame of every recording a second time and requires the same state |
| state events (`session.state`, `session.caps`, `plan.updated`, `queue.updated`, `usage.updated`, `changes.updated`) | one at or below the `seq` last applied for its type is ignored (a repeat, or an older one arriving late). A snapshot marks all of them as of its `head` |
| `approval.requested`, `input.requested` | the same `seq` is the same request (it can arrive again after a snapshot). A different `seq` under a known id is a new request that reuses it: an agent that restarts counts again from `perm-1` (the fake agent does; see Verification) |
| `approval.resolved` | closes the request it answers: the latest with that id at or before the answer's `seq`, so an old answer that arrives again never closes a newer request that reuses the id. The first answer stands. An answer that comes before its request is kept until the request shows up |
| `tool.started` | the same `seq` is the same call; a start that arrives after its completion joins the completion's item; any other is a new call, even under a reused `call_id` |
| `command.receipt`, `command.result` | recorded by `command_id` |
| `turn.ended` | ends the turn and all live text |
| anything else | ignored, so a newer hub's new event types do not break an older view. Its `seq` still counts |

**The delta rule** (G1 section 3, G9 "Delta offsets"). A delta is applied
to the live text of its `id` like this:

| The delta's `offset` against the text held | The reducer |
|---|---|
| equal to its length | appends |
| behind it, and the delta ends at or before the end | ignores it (text already held, as after a snapshot by `GET`) |
| anything else | drops it, adds the id to `stopped`, returns `fetch_snapshot` with `reason: "hole"`; later deltas for that id are ignored until `live[]` replaces its text |

**Lengths are code points.** The hub counts an offset with Python's
`len`, which counts code points, and `String.length` counts UTF-16 units. A
reply with an emoji in it would read as a hole at its next delta. The
reducer counts code points (`codePointLength`) and keeps the length of
each live text beside it. `reducer.test.ts` has the case.

**Ids can be reused.** An agent that restarts counts from its first id
again (`perm-1`, `toolu_1`). An id is matched with its `seq`: the same
`seq` is the same event, a different one under a known id is a new request or
call, and a page of older history matches its events to their own instance
(the latest one at or before the event's `seq`), not to the newest.

**A snapshot fetched after a hole** replaces only the text of the messages
the hole stopped. The stream may have moved on while it was on its way, so a
message that is not stopped keeps the text the stream gave it, and one the
stream ended meanwhile is not brought back. A whole snapshot (an epoch
change) older than what the stream gave is not taken.

**Message ids and the live text.** A message's live text ends on
`message.completed` or `thinking.completed` with its `id`, on
`tool.started` with its `call_id`, on `turn.ended`, on a journaled state
that is idle, agent_down, parked or closed, and when a state frame's
`live[]` leaves it out. `message.completed` carries the whole text and
replaces whatever was streamed.

### Recordings

The reducer tests replay the hub's own output
(`tests/fixtures/session-view/`, below) and check that the frames add up
to what the hub says: for every recording, the state built from its
frames equals the state built from the snapshot the hub took at the end
(cursor, epoch, phase, caps, plan, changes, usage, queue, pending, live,
timeline items, turns, tasks and checkpoints, `restoredSeq` included).
Tasks and checkpoints match because a snapshot's `messages[]` carry their
events too (G9: tasks, plans and checkpoints are timeline events), and the
reducer reads them from there as it does from a stream; the snapshot's own
`tasks[]` (the running ones) and `checkpoints[]` add only what is older than
those messages. Then each recording has its
own test: the approval pending then answered, a second device taking the
pending request from its snapshot, a queued message and a steer, a
reconnect mid-reply, an overflow recovery, a snapshot on a new epoch.
Hand-made frames cover what the recordings cannot reach (holes, code
points, out-of-order and repeated frames, reused ids).

## The connection supervisor

`Supervisor` keeps one session's stream open. Everything with a side
effect is injected: the `Transport`, `Timers` (`now`, `setTimeout`,
`clearTimeout`), a `random` function and an `Environment` (online,
offline, visible). `test-support.ts` has a fake of each, so a test moves
time by hand and nothing waits.

| State | Means | Leaves it on |
|---|---|---|
| `connecting` | opening the first stream, or still failing for the first time | a frame: `live`; a failure: backs off; 401 or 403, 404 or 410, 426, a v1 frame: the terminal states |
| `live` | a frame has arrived on the open stream | silence, an end of the stream, offline |
| `reconnecting` | the stream dropped or went silent; trying again | a frame: `live`; three failures in a row: `hub_down` |
| `offline` | the device has no network; no timer runs | the `online` event |
| `hub_down` | online, and three attempts in a row failed to get any answer (a stream that opened and gave a frame found the hub up, so it stays `reconnecting` however soon it ended) | a frame |
| `unauthorized` | 401 or 403; waits for `wake()` (a new credential) | `wake()` |
| `gone` | 404 or 410: the session does not exist | `wake()` |
| `outdated` | HTTP 426, or the first frame was not v2 (a hub that does not know `v=2` answers with today's frames) | `wake()` |

- **Connect.** `GET /v1/sessions/<key>/events?v=2`, with
  `&last=<cursor>&epoch=<epoch>` once the client holds both. The first
  time there is no cursor, and the first frame is the snapshot.
- **Silence.** Any frame, a ping included, restarts a 25 s timer
  (`PROTOCOL.silence_s`; the hub pings every 10 s). When it fires the
  stream is closed and replaced. A stream that dies after speaking is a
  drop: `reconnecting`, and the first retry is immediate. A stream that
  never spoke is a failure and backs off.
- **Backoff.** The wait before retry `n` is `min(30 s, 1 s * 2^(n-1))`,
  half of it fixed and half random (`backoffDelay`). The count goes back
  to 0 only when a stream has stayed open for a ping interval (10 s): one
  that speaks once and closes does not reset it, so a hub or proxy that ends
  every stream after its first frames is met by growing, capped waits, never
  by a retry per close. A drop after a stream that did last is retried at once. `status.retryAt` says when the next attempt
  starts, for a countdown.
- **Wake** (`wake()`, the page becoming visible, the `online` event, a
  native shell's call). Whatever waits on a timer, or in a state nothing
  retries out of, tries now. A live stream that has been quiet 25 s or
  more is replaced (a device that slept holds a dead socket whose timer
  never ran). A healthy one is left alone. `online` while live starts the
  connection over: the network may have changed under the stream.
- **Offline** is read from `navigator.onLine` and its events. A failure
  while it says offline is `offline`, not `hub_down`.

`status` is `{state, attempt, since, retryAt, detail}`; `detail` is a few
words for a tooltip ("HTTP 502", "no frame for 25 s", a network error).

JSON requests time out after 30 s (`timeoutMs`) and reject like a lost
network, so one request that hangs across a sleep cannot hold the outbox.

The real transport reads SSE from `fetch` streams, as `chat.js` does
(`EventSource` cannot send an `Authorization` header). `fetchTransport({
baseUrl, headers })` takes the credential from a function, read at each
request. The Mac and iPhone shells inject their own header at the scheme
handler, so they pass none.

## The outbox

Only sends go through it. Approvals, Stop, Restore and the queue edits
are never queued (see "Gated actions").

### What the hub does with a send

The hub has two paths behind `POST /v1/sessions/{key}/message`, and they
promise different things:

| | Keeper path (a session a keeper holds) | Legacy path (any other session: `hub.py` `_send_message`, and a satellite's `queue_command`) |
|---|---|---|
| Reads the id from | `command_id` (or `client_id`, its old name) | `client_id` only |
| 2xx answer | 202 with a `receipt` (`accepted`, `queued`, `held`); a repeat gets the stored receipt again, and the stored `result` once there is one | 202 `{ok, queued, client_id, waiting}`, no receipt |
| De-duplicates a repeat | yes: never applied twice | no: a repeat types the message in again (a repeat while the first is still queued is a 409) |
| Its `command.result` | journaled under the `command_id` | a v1 `command.result` with `client_id` set, which the v2 stream serves under `command_id`: on delivery (`ok`), "session stayed busy for 30 min; message not sent", "hub restarted; message not sent", a failed injection, or `cancelled` from a queue action. The message is typed in only once the session is idle, for up to 30 minutes (`protocol.IDLE_WAIT_MAX_S`) |
| After a catch-up by replay | results journaled while away are replayed | the same |
| After a catch-up by snapshot | not in the snapshot (results are bookkeeping); `GET .../commands/{id}` returns the stored result (G31) | not in the snapshot, and there is no way to ask: the result is lost to this client |
| A result the hub made for the stream (`seq` 0) | not replayed; the lookup returns it | not replayed and not askable |

### The send lifecycle

An entry is `{commandId, session, text, attachments, mode, createdAt,
status, attempts, path?, receipt?, sentAt?, ambiguous?, keeperOnlyAt?,
lastError?, error?, endedAt?, claim?}` (`OutboxEntry`); `keeperOnlyAt`
is G31's (below). Its `commandId` is made once and sent
as both `command_id` and `client_id` on every post. Statuses:

| Status | Terminal | Means |
|---|---|---|
| `waiting` | no | kept on this device; not posted yet (`attempts` 0), or due to be tried again (`lastError`) |
| `sent` | no | the hub answered 2xx. `path` is `keeper` when that answer had a `receipt`, `legacy` when it did not. Waits for its `command.result` |
| `failed` | yes | not delivered, or may or may not have been (then `ambiguous` is set, and a later receipt or result can still move it on); `error` says which |
| `unconfirmed` | yes | the hub took it, but no result came in the time one should have; `error` says so |

**One invariant.** An entry leaves the outbox in exactly two ways: a
`command.result` for its id that says ok (or that a queue action cancelled
it: the person removed it, or steered it and its text went on as a new
message), or the person dismissing it (`discardSend`, or `retrySend`,
which replaces it with a new send). A 2xx answer never removes an entry.
Nothing still open is dropped by age: deadlines end it in a terminal state
with a reason. A terminal entry nobody dismisses is removed 7 days after it
ended (`TERMINAL_KEPT_MS`), so a session that no longer exists does not
bring it back on every reload for ever.

Every transition:

| From | Event | To |
|---|---|---|
| (new) | `send()` | `waiting`, written to storage before anything else happens |
| `waiting` | may not post (offline, row not seen yet, unauthorized, gone, outdated) | stays `waiting`; `sends[]` says why |
| `waiting` | this page claims it to post (one read-write transaction: the claim and the counted attempt are on disk before the request leaves) | posted |
| `waiting` | it holds a claim less than `CLAIM_STALE_MS` (60 s) old: another tab's, or this page's from before its store was stopped and started again | stays `waiting`, left alone ("being sent from another tab"); looked at again when the claim would be stale |
| `waiting` | its claim is older than that (abandoned: a page closed with the post out, so it may have reached the hub). A reloaded page has a new owner id, so it sees the old page's claim as another page's and waits out this limit | the deadline pass itself resolves it, whether or not this page may post: the claim is removed and the entry marked `ambiguous`, atomically, and in the same pass it ends `failed` "may or may not have been sent", on any row: it is never posted again. A keeper-only claim's entry stays `waiting` instead and is posted again keeper-only (G31) |
| `waiting` (this page's post out) | the store is stopped | the answer is still written to storage and the claim released; the outbox that replaces this one takes the entry as the answer left it, at once |
| `waiting` | the claim finds it `sent` or ended by another page, or gone from storage | taken as storage has it |
| `waiting` | post answered 2xx with a `receipt` | `sent`, `path: keeper` |
| `waiting` | post answered 2xx without one | `sent`, `path: legacy` |
| `waiting` | post answered 2xx with the stored `result` | removed if ok or cancelled, else `failed` with its note |
| `waiting` | no answer, 408, any 5xx (every 503 included), on any row | `failed`: "may or may not have been sent: check the session, and send it again if it did not arrive"; never posted again. A keeper-only post's send stays `waiting` and `ambiguous` and is posted again keeper-only on the backoff (G31) |
| `waiting` | 429, 425 (the hub said not now before it stored or queued anything) | `waiting`, tried again with backoff, on any path |
| `waiting` | 409 `duplicate_client_id` (the legacy path still holds a send under this id: an earlier post got there) | `sent`, `path: legacy` |
| `waiting` | 401, 403 | `waiting`; the outbox pauses until `wake()` |
| `waiting` | 404 | `failed`: "this session no longer exists" |
| `waiting` | another 4xx (409, 422 ...) | `failed`, with the hub's message |
| `waiting` | 24 hours since it was made (`OUTBOX_TTL_MS`) | `failed`: "the hub could not be reached for 24 hours" |
| `sent` (keeper) | 24 hours since it was made and no result seen | `unconfirmed`: "The hub took this message, but its result was not seen on this device. Check the session before sending it again." (the hub took it; whether it was delivered, and its result, are unknown: a held send can expire at the hub, and a satellite can refuse even an accepted one) |
| `sent` (legacy) | 45 minutes since the hub took it (`LEGACY_CONFIRM_MS`, longer than the hub's 30 minute busy limit) | `unconfirmed`: "the hub took this message but never said whether it reached the session" |
| `waiting`, `sent` | `command.result` ok or cancelled | removed |
| `waiting`, `sent` | `command.result` not ok | `failed`, with its note |
| `unconfirmed`, `failed` (may or may not) | `command.result` ok | removed: it did arrive |
| any open | the connection reaches `gone` or `outdated`, before or after the stored entries are loaded | `failed`, with that reason |
| `failed`, `unconfirmed` | dismissed, retried, 7 days since it ended, or more than `TERMINAL_KEPT_MAX` (50) ended entries in the session (the oldest go first, never an open one) | removed |

**What each answer means.** Every status and code the message route can
answer with (`hub.py` `_send_message`, `_kept`, `_forward`;
`runtime.py` `send`, `_record`, `_deliver`, `_client`):

| Answer | Code | Raised when | The client |
|---|---|---|---|
| 202 with `receipt` | | the keeper path stored it | `sent`, keeper |
| 202 without | | the legacy path (local or a satellite's) took it | `sent`, legacy |
| 400 | `bad_body` | the body could not be read | `failed`, the hub's message |
| 401, 403 | | no or a revoked credential, before anything is read | `waiting`, outbox paused |
| 404 | `unknown_session`, `not_found` | no such session or route | `failed`, "this session no longer exists" |
| 409 | `command_id_conflict` | the id was used for a different command | `failed`, the hub's message |
| 409 | `send_rejected` | the keeper refused the send | `failed`, the hub's message |
| 409 | `not_attached` | the session is observed, read-only | `failed`, the hub's message |
| 409 | `duplicate_client_id` | the legacy path still holds a send under this id | `sent`, legacy |
| 409 | `not_kept` | a `require_keeper` post (G31) reached a session no keeper holds; nothing was stored or queued | a first post: `waiting`, tried again; a repeat: `failed`, may or may not have been sent |
| 422 | `bad_command`, `bad_attachment` | the command or an attachment is invalid | `failed`, the hub's message |
| 429 | `backlog_full` | too many waiting, before this one was queued | retried, any path |
| 503 | `store_failed` | the hub's store could not record it. Not always "nothing was applied": a satellite's keeper route answers it from `get_command` after `record_command` stored the command, and its `_flush` may have delivered it (`agent.py`) | `failed`, may or may not have been sent |
| 503 | `keeper_failed` | the keeper did not answer, possibly after the request reached it ("not known to have been applied") | `failed`, may or may not have been sent |
| 503 | any other code | not known | `failed`, the same |
| 408, 500, 502, 504, other 5xx, no answer | | a proxy, a crash, a timeout, a lost connection | `failed`, the same |

**The invariant** (in the form G31 gave it; "Keeper-only posts and
command lookups" below has the tables). Every post is made under a claim
the database recorded. A page posts a given command id again only after
an answer that says the hub stored nothing, so the repeat is a first
delivery on either route, or with `require_keeper: true` when every
earlier post of it that may have been applied carried that too:

| Answer | Why nothing was stored | Then |
|---|---|---|
| 429 `backlog_full` (local legacy route, a satellite's forwarded post) | refused before the send was queued (`hub.py` `_send_message` checks `MAX_SEND_BACKLOG` first; `queue_command` returned nothing) | tried again with backoff |
| 425 | not raised by any message route today; read as "not now" | tried again with backoff |
| 401, 403 | the credential is checked before the body is read | paused until `wake()`, then tried again |
| 409 `not_kept` to a keeper-only post | the legacy routes refuse a `require_keeper` body before they queue anything | tried again with backoff; the next claim reads the row again |

One 429 is different: a keeper on a satellite answers 429 `backlog_full`
after `record_command` stored the command as `rejected` (`agent.py`). A
repeat under the same id then gets 202 with receipt `rejected`, not a
delivery; the client ends the send `failed` with the hub's reason on that
receipt. Every other answer may mean the send was applied. A plain
post's send then ends; a keeper-only post's send is posted again,
keeper-only, under the same id.

The hub picks the route for a message when the post arrives
(`runtime.owns(key)`, `hub.py`), and the route that is not the keeper
(`_send_message`) types a repeat in again unless the first is still
queued; a session can leave its keeper after this page last saw
`mode: keeper`, and can come onto one ("Continue in SWE"), so no
client-side check makes a plain repeat safe. With a hub that lists
`require_keeper`, the hub makes that check itself when the post arrives.
Without it, an answer that may mean the send was applied ends it as "may
or may not have been sent", and the person can send it again as a new
message.

**Evidence after a send ended in doubt.** An answer that may mean the send
was applied ends it "may or may not have been sent" (`failed`,
`ambiguous`). On a keeper row a lost answer usually means the keeper took
the message, and the proof follows on the stream, so evidence still moves
it on: its `command.result` ok or cancelled removes it as delivered; a
failed result replaces the doubt with the hub's note (a definite
`failed`, which then stands); its `command.receipt`, live or held in
`session.commands` when the outbox starts (`knownReceipt`), makes it
`sent` on the keeper path. A `rejected` receipt fails a send that is
waiting, in doubt or `sent` with the hub's reason: a keeper can take a
send and then refuse it (its log write failed: `accepted`, then
`rejected` with `not_recorded`).
Evidence that comes while the send's claim is being written or its post
is out is kept. A result means what it says (a result in the 2xx answer
itself still wins). A `rejected` receipt fails the send with the hub's
reason however the post ends. Any other receipt settles it as `sent`
(not posted at all if it came during the claim) when the HTTP answer
leaves the outcome open (no answer, 408, a 5xx, the abort at `stop()`),
says "not now" (425, 429, 401, 403), or is a 2xx; a definite refusal
(any other 4xx, such as 409 `send_rejected`) keeps its own outcome. A
receipt held while the claim finds the send gone from storage (another
page saw it through) is dropped, and nothing is written. An accepted send
whose receipt reaches this page does not end in doubt because its HTTP
answer was lost. This fits
the order as its own stage: in doubt is below `sent` and below a definite
end, so a receipt's `sent` and a result's `failed` both move forward, and
the two copies of a definite `failed` and an `unconfirmed` entry still
compare equal (Limit 10 is unchanged).

What settles a `sent` keeper entry: its `command.result`, live on the
stream or in a catch-up's replay, and `knownResult`, the results this
store took in from those frames (`session.commands`, the last 200
commands, for the store's life) when its outbox starts again. A catch-up
replays at most `REPLAY_MAX` (500) events from the session's journal
(`protocol.py`), which keeps up to 4000 records or 16 MB and trims to 2000
or 8 MB (`journal.py`). A result is missed, and the entry ends at 24 hours
as "The hub took this message, but its result was not seen on this device.
Check the session before sending it again.", when: the catch-up was
a snapshot (a gap over 500 events, a trimmed journal, an epoch change) or
the page loaded fresh (a first connection gets a snapshot, and a snapshot
carries no `command.result`); or the result was one the hub made for the
stream with `seq` 0 while this page was away (not replayed). The hub's
`commands` table keeps a finished command for 7 days (`store.py`
`RETAIN_S`), and with a hub that lists `command_lookup` the outbox reads
it (`GET .../commands/{id}`, G31) when its stored sends load and after
every catch-up, so none of these cases leaves a `sent` keeper send
waiting for its deadline any more.

A `command.result` with an empty id changes
nothing. One with an id the outbox does not hold is kept until the stored
sends are loaded (up to 200), then applied to them with the results the
session already holds (`session.commands`), before anything is posted;
after that, an unknown id changes nothing.

When posting starts: the first post needs no guess about the path, but it
waits until the device is online, the session's row has been seen (so a
read-only session is refused, not posted), and the connection is not
unauthorized, gone or outdated. A send made before the row is known is
accepted and shown as waiting ("Waiting for the hub"); the deadlines above
run on their own timer from the injected clock, so it cannot wait for ever.

Other rules:

- **On disk first.** `send()` resolves, and the entry becomes visible and
  deliverable, only after its first write. A store stopped and started
  again hands its entries, the ones still being written included, to the
  new outbox.
- **Two copies of one send at start.** `init()` loads storage first. Where
  storage and the handed-over list both hold a send, the copy further along
  wins (`furtherAlong`, below), and when that is the handed-over copy it is
  written to storage at once. A handed-over entry storage lacks is written
  once. The outbox remembers every id it removed while loading (discarded,
  retried, settled) and never takes one back from the load or writes it
  again. Until `init()` ends nothing is posted or aged, and results, late
  answers and a gone session are held and applied once loading ends.
- **Nothing to act on while loading.** `start()` publishes the new
  outbox's view at once, with `outbox.loading` true, and while it is true
  every `SendView` has `canDiscard` and `canRetry` false ("Loading the
  sends kept on this device."). `discardSend` called anyway is safe: the
  id stays removed.
- **Order.** Sends go oldest first, one at a time: one that cannot go holds
  back the ones behind it.
- **No Discard while it is claimed.** An entry is `inFlight` (shown
  `sending`, with no Discard or Retry) from the start of its claim until
  its post is answered. A send discarded or ended anyway while its claim is
  being written is never posted.
- **The deadline timer never spins.** It is armed only for the next
  deadline strictly in the future. A deadline at or before now is one the
  pass could not act on (this page's own post still out), and is looked at
  on the next pass. The test timers throw on any zero-delay timer, so every
  test in the suite guards this.
- **One page posts an entry.** Tabs of one browser share the database, and
  a claim (`{owner, at}` on the entry) decides which page posts it. A tab
  learns of another tab's new sends only when it loads; results reach every
  tab on its own stream. A claim the database did not record is not a
  claim: a page whose database stops answering posts nothing, on any row,
  until it answers again (see "When storage fails", which also says why no
  message is posted twice). Where there is no database at all,
  there are no other pages to coordinate with, so a claim made in memory
  is the only one.
- **No IndexedDB.** It can be missing, or throw (a private window, blocked
  site data, a sandboxed frame), when it opens or later. The outbox always
  keeps its entries in memory and treats the database as a write-through
  copy. When it is missing or does not open, the storage is a memory
  storage and says why: `outbox.persistent` is false and `outbox.note` reads
  "IndexedDB is not available (...); sends are kept in memory and will not
  survive a reload". When it fails later, see "When storage fails": the note
  reads "IndexedDB failed (...). Sends saved before are not sent until it
  answers again, and new ones are kept in memory and will not survive a
  reload until then". A view should show the note beside the composer.

### Keeper-only posts and command lookups (G31)

Two hub features, each listed in `GET /v1/status` `features` by a hub
that has it and whose session runtime is on (its store is open: without
it nothing is kept and nothing can be looked up), let the outbox repeat a keeper send safely and read what
became of one:

| Feature | The hub | Where |
|---|---|---|
| `require_keeper` | a message post whose body has `require_keeper: true` goes to the keeper path or is refused with 409 `not_kept`, before anything is stored or queued. It never reaches the legacy route: not the local `_send_message`, not a satellite's `queue_command` forward, and not the satellite's own legacy route (a `kept` message is posted to the satellite's local route with the field set) | `hub.py` `do_POST`, `agent.py` `_run_kept` |
| `command_lookup` | `GET /v1/sessions/{key}/commands/{id}` answers the stored command: `{ok, session, command_id, op, receipt, reason?, result?, received_at, updated_at}`, where `receipt` is the stored status (`held`, `queued`, `accepted`, `rejected`) and `result` the stored result. 404 `unknown_command` when the hub's store does not hold the id for that session. Reads only: no write, no event, no keeper call. The same credential as every session route | `hub.py` `_command_lookup`; schema `CommandLookup` |

An older hub ignores `require_keeper` and answers the GET with 404
`not_found`, so the client relies on neither unless the feature is
listed. **Features are known only while the stream is live**: the store
reads `/v1/status` each time the connection goes live (asking again on
the backoff until it answers, while the stream stays live) and forgets
the answer when it leaves `live` or stops. A hub that restarts as an
older version drops the stream, but the page may not see that before
its next post, so **every keeper-only post is confirmed first**: the
store reads `/v1/status` again right before it, and the post goes only
when that answer still lists `require_keeper` under the `boot_id` the
features were read with. Otherwise the claim is released, the features
are read again, and the send waits for them. `F+` below means the
feature is listed, `F?` that it is not known (not live, or not read
yet), `F-` that `/v1/status` answered without it.

**Keeper-only is decided at the claim.** A post carries
`require_keeper: true` when `require_keeper` is `F+` and the session's
row says `mode: keeper`, and the claim records that on the entry, in
the same transaction as the counted attempt, as `keeperOnlyAt`: the
attempt count of that claim. An entry is keeper-only only while
`keeperOnlyAt` equals `attempts` (`isKeeperOnly`), so a claim made by a
build that does not know the field (it counts an attempt and copies the
rest) leaves its plain post not keeper-only, and the send is never
repeated after it. An entry that is not `ambiguous` has only had posts
whose answers say nothing was stored (below), so each claim decides
afresh; an `ambiguous` entry stays keeper-only only if its latest claim
was, because its earlier post may have been applied and the next must go
the same way. The first post
is keeper-only too, not only the repeats: "Continue in SWE" moves a
terminal session onto a keeper, so a plain first post could reach the
legacy route and a keeper-only repeat then reach the keeper, which
holds no record of the first.

**The invariant, in its new form.** Every post happens under a claim the
database recorded; with the database down nothing is posted, on any
row. A command id is posted again only:

1. after an answer that says the hub stored nothing (429, 425, 401, 403,
   and 409 `not_kept` to a keeper-only post), or
2. with `require_keeper: true`, when every earlier post of it that may
   have been applied carried `require_keeper: true` as well
   (an `ambiguous` entry that `isKeeperOnly`), and `require_keeper` is
   `F+` and confirmed right before the post.

So a legacy route never receives an id the hub may already have
applied, and every post that may be a repeat reaches the keeper path,
whose store answers it with the stored receipt and result instead of
applying it again. A GET can never type a message in, so it needs no
claim.

States below: `W` waiting and not `ambiguous`; `WA` waiting, `ambiguous`
and keeper-only; `WA-` waiting, `ambiguous`, not keeper-only; `S-K`
and `S-L` sent on the keeper or legacy path; `FA` failed "may or may not
have been sent" (`ambiguous`); `F` failed definitely; `U-K`, `U-L`
unconfirmed.

**What a claim posts.**

| State when claimed | `require_keeper` F+ | F? | F- |
|---|---|---|---|
| `W`, the row says keeper | posted with `require_keeper: true` once confirmed; `keeperOnlyAt` set | posted plain; `keeperOnlyAt` cleared | posted plain; `keeperOnlyAt` cleared |
| `W`, any other row | posted plain; `keeperOnlyAt` cleared | the same | the same |
| `WA` | posted again with `require_keeper: true` (a repeat) | not claimed and not posted: it waits for the features, holding back the sends behind it; its 24-hour deadline still ends it `FA` | not posted: `FA` (as merged) |
| `WA-` | not posted: `FA` (as merged) | the same | the same |

A claim that finds the stored copy `ambiguous` when this page's was not
(another page's stale claim, taken over in the claim's transaction)
decides by whether the stored copy was keeper-only: a `WA` copy is posted again
when F+, released and left waiting when F?, ended `FA` when F-.

**What an answer does.** "Plain" is a post without the field (as merged);
"keeper-only, first" one whose entry was `W` at the claim; "keeper-only,
repeat" one whose entry was `WA`.

| Answer | Plain | Keeper-only, first | Keeper-only, repeat |
|---|---|---|---|
| 2xx with `result` | settled by the result | the same | the same |
| 2xx, `receipt: rejected` | `F`, the hub's reason | the same | the same |
| 2xx with another `receipt` (a repeat's is `duplicate: true` with the stored status) | `S-K` | `S-K` | `S-K` |
| 2xx without a receipt | `S-L` | `S-L` (only a hub that ignores the field answers this) | `S-L` (the same) |
| no answer, 408, any 5xx | `FA` | `WA`, tried again on the backoff ("Sending it again under the same id") | `WA`, tried again on the backoff |
| 429, 425 | `W`, tried again on the backoff | `W`, tried again | `WA`, tried again |
| 401, 403 | `W`, paused until `wake()` | the same | `WA`, paused |
| 409 `not_kept` | `F`, the hub's message (no hub sends it to a plain post) | `W`, tried again on the backoff: nothing was stored, and the next claim reads the row again (once the row no longer says keeper, the post is plain) | `FA` |
| 409 `duplicate_client_id` | `S-L` | `S-L` | `S-L` |
| 404 | `F`, "this session no longer exists" | the same | `FA`: the earlier post may have been applied |
| any other 4xx | `F`, the hub's message | the same | `FA` |
| a receipt or result on the stream while it is out | as "Evidence after a send ended in doubt" | the same | the same, except that a receipt settles it as `S-K` even after a definite refusal: a refusal of a repeat says nothing about the earlier post the receipt is for |

A refusal of a repeat ends it `FA`, never `F`: the routes refuse before
they read the store (`_kept` checks attachments, `owns` and the row
first), so a refusal says nothing about the earlier post. Only a result
or a `rejected` receipt ends an `ambiguous` send definitely.

**Deadlines and the claim that was abandoned.**

| State | Event | To |
|---|---|---|
| `WA` | 24 hours since it was made | `FA` |
| `W` with a stale claim (its post may be out or lost) | abandoned (`decideAbandon`, which keeps `keeperOnlyAt` and `attempts`) | `WA` when the claim's post was keeper-only and `require_keeper` is not F-, repeated by the next pass; else `FA` (as merged) |
| `WA` with a stale claim | the same | `WA`, or `FA` when F- |
| `late()` after `stop()` | the answer to a post that was out | as merged for a plain post: storage gets `sent`, a removal, or the entry `waiting` and `ambiguous`. A keeper-only post's 429, 425, 401, 403 or 409 `not_kept` stored nothing, so its entry is left as it was (not made `ambiguous`); a receipt that came while a repeat was out settles it as `sent` whatever the refusal. The next outbox treats a `WA` copy as above |

**What the lookup settles.** With `command_lookup` F+ the outbox asks
`GET .../commands/{id}` for every `S-K`, `U-K` and `FA` entry, oldest
first, one at a time, when its stored sends are loaded, after each
catch-up's state frame, and when it learns the feature. A pass started
while one runs is folded into it. The answer is applied as a stream
frame would be (`noteResult`, `noteReceipt`), so a send whose claim is
being written or whose post is out takes it the same way.

| Answer | `S-K` | `U-K` | `FA` |
|---|---|---|---|
| 200 with a `result`, ok or cancelled | removed | removed | removed |
| 200 with a failed `result` | `F`, its note | `F`, its note | `F`, its note |
| 200, `receipt: rejected`, no result | `F`, its reason | unchanged | `F`, its reason |
| 200, another receipt, no result | unchanged | unchanged | `S-K`: the hub has it, and its own deadline runs |
| 404, any other status, no answer | unchanged | unchanged | unchanged |

A 404 does not make an `FA` send "not sent": its first post may have
been plain and gone the legacy way, which stores nothing. Neither table
applies with F- or F?: the outbox does what G11 merged.

**Two tabs.** Each tab claims before it posts, so two pages posting one
`WA` send post it one after the other, both keeper-only, and the keeper
store answers the second with the first's receipt. A tab that does not
know the features yet leaves a `WA` send alone; a tab of an older build
claims it, sees `ambiguous`, and ends it `FA` without posting (it never
reads `keeperOnlyAt`). A `W` send an older build finds after a
keeper-only post's 429 or `not_kept` is posted plain, which is a first
delivery, since nothing was stored; its claim counts an attempt and
leaves `keeperOnlyAt` behind, so if that plain post's answer is lost, a
page of this build sees the entry as not keeper-only and ends it `FA`
instead of repeating it to a keeper that holds no record of it.

### Two copies of one send

A send can have two copies: the one in storage and the one an earlier
outbox of this page handed over at `stop()`. Either can be further along:
storage, when a post's answer was written after `stop()`; memory, when a
claim write failed and the storage fell back to memory, so the disk kept
the first write while the post went on. `furtherAlong(a, b)` compares them
step by step, and the first step that tells them apart decides:

| Step | Further along | Why it only moves forward |
|---|---|---|
| 1. Stage | `failed` (definite) and `unconfirmed`, over `sent`, over in doubt (`failed` and `ambiguous`: "may or may not have been sent"), over `waiting` | `waiting` becomes `sent` on a 2xx, or in doubt on an answer that may mean it was applied; in doubt becomes `sent` on its receipt, or a definite `failed` on a failed result; `sent` ends `unconfirmed` or `failed`; nothing leaves a definite end except removal (a new id for a retry) |
| 2. `attempts` | more | only a claim changes it, and always by +1 |
| 3. The claim | no claim over a claim | a claim is only ever written together with attempts+1 (`decideClaim`, tested), so at the same count a copy without one is the later, released one: its answer (429, 425, 401, 403: the answers that stored nothing) or `abandon` removed it |
| 4. `ambiguous` | set over not | within `waiting` it is only ever set (an abandoned claim, a stale claim taken over), never cleared; leaving `waiting` decides by stage first |

Both copies are read in today's terms first: a status an earlier build
stored as `pending` is `waiting` (in a load, a claim, an abandon and a
compare-and-write alike), and a status this build does not know ranks
below every stage, so it never wins a comparison.

The two known cases: an answer stored by `late()` after `stop()` (disk
`sent`, memory `waiting` with the old claim) wins on stage, and so does a
sent legacy send whose claim write fell back to memory (memory `sent`,
disk `waiting` at 0 attempts). Copies equal on all four steps keep the
stored copy. What they can still differ in cannot cause a post:
`text`, `attachments`, `mode` and `createdAt` never change; `lastError`,
`error` and `endedAt` are only shown; two terminal copies (`failed` and
`unconfirmed`) can only be retried as a new id or dismissed; and two
`sent` copies at the same count come from the one answer.

### When storage fails

The database can fail after it opened (WebKit's "Connection to Indexed
Database server lost"). `FallbackStorage` then works by three rules:

1. **One more try on a fresh connection.** An operation that fails is
   tried once more on a newly opened connection before anything falls
   back. If that answers, nothing changes for the outbox.
2. **In fallback, the database first.** Every later operation tries the
   database again (one fresh connection each). The first one that answers
   replays what was held, by compare-and-write (`reconcile`, the copy
   further along wins), and the storage is the database again
   (`persistent` true, the note cleared).
3. **A claim the database did not record is not a claim.** In fallback
   every claim answers `unrecorded`, and the outbox posts nothing, on any
   row: it shows "Not sent yet: this device's storage did not answer, and
   sending now could send it twice. Trying again." on every waiting send
   (`outbox.storageDown`), tries again on its backoff, and lets the
   24-hour deadline end it if the database never answers. The entry is
   not written meanwhile (a write would drop a claim other pages rely on).
   A keeper row gets no exception: the session can leave the keeper before
   another page posts the send again, and that post then reaches the route
   that does not de-duplicate.

**Why no message is posted twice on a route that does not de-duplicate.**
Every post is made under a claim the database recorded, on every row,
and a plain post of a given id follows only answers that stored nothing
(above). Two recorded claims on one id cannot both be live (a claim is
taken in one read-write transaction, and a fresh claim makes every other
page see it busy), so two posts that may both be applied need a takeover
of a stale claim, and a takeover marks the send `ambiguous`, which is
posted again only keeper-only, and only when the claim's post was
keeper-only too (G31): the keeper store, which de-duplicates by id, sees
both. A page cannot know
that a write it heard fail did not land, so while a database exists no
send counts as only in memory. With no database at all (it is missing or
did not open, and the storage is a plain memory storage), memory is the
only store and a claim made there is the recorded one.

| Operation | When it fails, after the fresh try |
|---|---|
| A send's first write | held in memory, written when the database answers. The write may have landed anyway |
| Claim | `unrecorded`: not posted on any row, waiting with the reason above, tried again on the backoff; the deadline still ends it |
| Save after an answer (`sent`, a release, an end) | held, replayed when the database answers. Until then the database shows this page's recorded claim: other pages see it busy, then stale, then end it "may or may not have been sent" (Limits); none posts it again |
| `late()` after `stop()` | as a save after an answer |
| Abandon a stale claim | resolved in memory (`ambiguous`, no claim), replayed later; the database keeps the stale claim, which every page resolves the same way |
| Remove (a discard, a delivered result, the terminal cap) | held, replayed when the database answers. If the page reloads first, the next page finds the entry (Limits) |
| Load | answers what memory holds |

Which writes compare and which may be blind:

| Write | How | Why |
|---|---|---|
| A send's first write | blind `put` | a new id: no other copy exists anywhere |
| `init()`'s write-back of a handed-over copy | `reconcile` | another page may have moved the send on since the load read it |
| `save()` (every write after an answer or an end) | `reconcile` | another page may have taken a stale claim over meanwhile |
| `late()` | `reconcile` | the same, and a newer outbox may have written it |
| Replay after a fallback | `reconcile` | what was held may be older than what other pages wrote since |
| Claim, abandon | one read-write transaction each | they decide from the stored copy |
| Remove | blind | it follows a delivered result (every copy is older), or a discard, which is offered only while no post of this page's is out; a post another page has out goes on, and its answer's compare-and-write writes it back as `sent` |

### What a view reads: `sends`

`store.getState().sends` (`describeSends`) is one `SendView` per entry,
oldest first: `{commandId, text, attachments, mode, createdAt, shown,
reason, canRetry, canDiscard}`. `shown` is `waiting`, `sending` (being claimed, or its
post is on its way), `sent`, `failed` or `unconfirmed`, and `reason` is always one
sentence for the person ("Waiting: you are offline. It will be sent when you
are back.", "Queued: it will be sent when the current turn ends.", the
hub's note for a failure). A message bubble reads this, not
`session.commands`, which only holds what the stream said and misses what a
snapshot catch-up leaves out.

## Gated actions

An approval answered from a stale screen can land on a request that has
moved on, so these need a live connection that has caught up and are
refused otherwise, with a reason a view can show:

`respond(requestId, decision)`, `interrupt()`, `restore(checkpointId)`,
`queueAction(op, commandId, text?)`. Each returns
`{ok: false, reason, message}` without touching the network when
`gate()` refuses:

| `reason` | When | `message` (the first words) |
|---|---|---|
| `not_live` | connecting, reconnecting, offline, hub down | "Connecting to the hub", "Reconnecting", "You are offline", "The hub is not answering" |
| `catching_up` | live, but the catch-up's state frame has not arrived | "Catching up with the session" |
| `read_only` | the row says `control` is not `full` | the row's `control_reason` |
| `unauthorized`, `gone`, `outdated` | the terminal states | |

A request that reaches the hub and is refused returns
`{ok: false, reason: "refused", status, message}` with the hub's message
("that request is no longer open"); no answer returns `reason: "network"`.
None of them is retried. `send` is refused only for `read_only`, `gone`
and `outdated`: it is accepted at once, kept, and posted when the session
has been seen and the device is online.

## The store

```ts
const store = createSessionStore({ sessionKey, transport: fetchTransport({ baseUrl, headers }) });
store.getState().start();
const items = useSessionStore(store, (s) => s.session.items);
```

`SessionStoreOptions`: `sessionKey`, `transport`, and injectable `timers`,
`random`, `environment`, `openStorage`, `newId`, `backoff`, `silenceMs`.

`SessionStoreState`:

| Member | |
|---|---|
| `session: SessionState` | what the frames add up to |
| `connection: ConnectionStatus` | the supervisor's status |
| `outbox: {entries, persistent, note, paused}` | sends not yet delivered |
| `started: boolean` | |
| `start()`, `stop()` | open or close the stream and the outbox; `start` twice is one connection; the store can be started again |
| `wake()` | check the connection now (visibility, network, the native shell's `wake()`) |
| `send({text, attachments?, mode?}) -> {ok: true, entry} \| Refusal` | into the outbox; `mode` is `queue` (default) or `steer` |
| `respond(requestId, decision)` | `decision` is `allow`, `deny`, `always`, or the 1-based number of a menu row, as `POST .../approval` takes it |
| `interrupt()` | Stop: `POST .../control {op: "interrupt"}` |
| `restore(checkpointId)` | `POST .../restore {checkpoint_id}`: see Schema gaps |
| `queueAction("edit" \| "cancel" \| "steer", queuedCommandId, text?)` | `POST .../queue` |
| `loadOlder()` | the page before the oldest held (`GET .../messages?v=2&before=`). A page is dropped if, while it was out, the epoch, the boot or the page marker changed (a snapshot replaced what it would extend); the next call pages from the new marker |
| `discardSend(id)`, `retrySend(id)` | dismiss a failed send, or send it again as a new one |
| `refusal()` | why a gated action cannot run now, or null: for disabling a control and saying why |

Every action that reaches the hub carries a fresh `command_id` in its
body. What the store does on its own: a hole in the deltas fetches
`GET .../snapshot` and takes its `live[]` text for the messages the hole stopped (retried
with backoff, five tries, since the next catch-up's state frame restores
it anyway); an epoch the client does not hold fetches the snapshot and
replaces everything; a `command.result` reaches the outbox; going live
wakes the outbox and reads `/v1/status` for the outbox's features, which
are forgotten when the stream leaves live; a catch-up's state frame
starts the outbox's lookups (G31). No message is posted again except as
G31's tables say.

## Fixtures

`scripts/record_session_view.py` starts the hub of
`tests/test_remote_sse_v2.py` (a real `HubState` over a throwaway home,
with a keeper on a thread driving the scripted `FakeAdapter`), opens
`V2Stream` the way a connection does, plays a scenario and writes every
frame the stream sends. Nothing in a fixture is hand-written.
`PYTHONPATH=src:. python3 scripts/record_session_view.py` writes
`tests/fixtures/session-view/<scenario>.json`; `--check` compares.

| Scenario | Shows |
|---|---|
| `default` | a session watched from its first turn: thinking, tools (one with file edits, one failing), plan, a subagent task, a checkpoint made and restored, changes, usage, two replies |
| `streaming` | a reply in four chunks, a ping, and a snapshot taken after the third chunk (what a client that lost the second one fetches) |
| `approval` | an approval and a question, answered; a second device connecting while the approval is open |
| `queued` | a message sent while the agent waits on an approval, queued and then sent |
| `steer` | a queued message sent now (steer), another taken out of the queue |
| `reconnect` | a client cut off mid-reply, coming back with its cursor and epoch: the replay, the row, the state frame's `live[]`, the delta that continues it |
| `overflow` | a stream not read while the agent runs, whose queue overflows: the events it missed, the row, the state frame |
| `epoch_change` | the journal starting over while a client is connected, and a client reconnecting with the old epoch: a snapshot each time |
| `failed_turn` | an error, a compaction, a failed turn |

A fixture is `{scenario, about, connections: [{request: {last, epoch},
frames}], snapshots: [{after: [connection, frame], body}]}`: a client's
connections in order, and the snapshot the hub gave right after the named
frame (the last one is the hub's view at the end, which the frames must
add up to). Everything that varies between runs is replaced with a fixed
name before writing (the session id, the work folder, the boot id, ids the
hub invents, device ids, wall-clock times); times come from the fake clock.
The file puts each frame on its own line, so a hub change reads as a small
diff.

`tests/test_session_view_fixtures.py` records every scenario again and
fails when a committed file differs, when a frame fails
`schema.validate`, when a path of this machine got in, and when recording
twice gives two results. A hub change that alters what a stream sends must
change the fixtures in the same commit.

## Dependencies

| Package | Version | Licence | Why |
|---|---|---|---|
| zustand | 5.0.16 | MIT | the store (runtime) |
| fake-indexeddb | 6.2.7 | Apache-2.0 | IndexedDB in the outbox tests (dev only) |

Nothing else was added: the type generator, the SSE parser and the fake
timers are written here. zustand ships in the bundle only once a view
imports the store (nothing does yet, so the committed bundle's files are
unchanged except `build-info.json`, whose source hash covers the new
sources).

`src/index.css` got one line, `@source not "./core"`. Tailwind scans every
source file for words that look like utilities, and the core's code and
comments have plenty ("visible", "fixed"): the first build shipped
`.visible` and `.fixed` as global class names, which G10's notes warn
against. `core/` holds no markup, so it is out of the scan.

## Verification

Run on 2026-11-21, macOS 27.3, Node 22.23.1.

| Check | Result |
|---|---|
| `pnpm typecheck`, `pnpm lint:tokens`, `pnpm test` (core: 326 tests), `pnpm build` | pass |
| `tests.test_session_view_fixtures` | pass; recording gives the same bytes on every run |
| The store against a real hub (a throwaway home, `swe remote serve`, the fake `claude` agent in approval mode, `fetchTransport` over real HTTP): connect, snapshot and state frame, the approval pending, `respond`, the reply, `send`, the second approval, the outbox emptied by `command.result` | pass. It found that the fake agent reuses `perm-1` and `toolu_fake1` in every turn, which the first version of the reducer treated as repeats: ids are now compared with their `seq` |

## Schema gaps

Things the client needed that the hub or the schema does not say. None was
changed here.

- **Offset unit.** `message.delta.offset` and `live[].text` lengths count
  code points (Python `len`), and the schema says only `integer`. A client
  that counts UTF-16 units is wrong on any reply with an emoji.
- **`row.state` and `row.status` go stale.** A row arrives in the snapshot
  and in `session.row` (ownership, title, pin changes); neither says the
  session started or stopped running. Draw the phase from
  `session.phase`, never from the row.
- **A snapshot's `tasks[]` holds only the running tasks**, and its
  `checkpoints[]` only the checkpoints made, with no restore mark. The
  client reads the rest from the snapshot's `messages[]` (task and checkpoint
  events are timeline events), so a task that finished or a restore older
  than the newest 200 timeline events is not in a state built from a
  snapshot. That is the same window as the timeline.
- **A session no keeper holds answers a send without a receipt, does not
  de-duplicate, and reads `client_id`, not `command_id`.** The client sends
  both, equal, never repeats such a send, and ends one that got no answer as
  "may or may not have been sent". If the hub should store the id on that path
  too, a client could repeat safely and settle on the result.
- **Keeper-only post and command lookup: built in G31.** G11 asked for a
  body field `require_keeper: true` that makes the hub answer 409
  `not_kept` instead of using the legacy route, and for
  `GET /v1/sessions/{key}/commands/{id}`. Both exist now, each listed in
  `/v1/status` `features`, and the client uses them only when listed
  ("Keeper-only posts and command lookups"). One thing differs from the
  plan written here: the first post of a keeper-row send carries the
  field too, not only its repeats, because a session can move onto a
  keeper ("Continue in SWE") between a plain first post that went the
  legacy way and its repeat. The tests are new ones, one per row of
  G31's tables (`outbox-keeper.test.ts`, `store-keeper.test.ts`,
  `tests/test_remote_keeper_only.py`), not the ones G11 deleted.
- **Restore has no route.** `restore(checkpointId)` posts to
  `POST /v1/sessions/{key}/restore {checkpoint_id, command_id}`, which G19
  must provide (G15's notes: a command in `_KEPT_OPS`, not in `HELD_OPS`).
  Until then the hub answers 404 and the store returns a `refused` result.
- **A steer is a command of its own.** `queue {op: "steer"}` ends the queued
  message with `command.result {ok: false, cancelled: true}` and sends its
  text as a new user message under a new command id. A composer that pairs
  a queued bubble with the user message by `command_id` will not find it:
  match on the queue item leaving `queue[]`.
- **Approval answers by text.** `POST .../approval` takes only `decision`
  (`allow`, `deny`, `always` or a menu row number). `input.requested` has
  no route that carries the person's text yet, so a question can be closed
  but not answered with words.
- **No v2 event lists an ACP agent's modes or models**
  ([G1-session-runtime-redesign.md](G1-session-runtime-redesign.md), G20).

## Limits

These are known and left for later. None of them posts a message twice or
hammers the hub. Only one posts something the person did not expect:
Limit 5, on both paths, posts once a message the person discarded. The
others show a wrong end state, leave the view waiting, or hold a resource.
They are not fixed in G11; G13 should know them.

1. **`stop()` during the claim transaction leaves a claim for a send that
   was never posted.** That entry ends "may or may not have been sent"
   after 60 s. A keeper-only claim's entry is posted again keeper-only
   instead (G31), which delivers it: only a plain claim keeps this
   limit.
2. **Two tabs can disagree after a device sleep.** One send can show
   `failed` in one tab and `sent` in the other. No second post goes out.
3. **The reducer never trims.** Every item, turn and index entry stays for
   the life of the view.
4. **An IndexedDB open that never answers leaves the outbox loading.**
   Safari has a known bug where `indexedDB.open` fires neither `success`
   nor `error`. The `DeferredStorage` then never resolves: the view stays
   on "Loading the sends kept on this device.", with no Discard or Retry,
   and a `send()` made then never resolves, so that message is not shown
   at all. Suggested fix (G18): a timeout on the open in
   `openOutboxStorage` (a few seconds) that falls back to a memory storage
   with the usual note, as it does when the open fails.
5. **A removal the database never takes, then a reload.** A discard of a
   `waiting` send the database holds, made while it does not answer, and
   the page reloads (or another tab loads) before it answers again: the
   next page finds the send `waiting` and posts it. That is one post of a
   message the person discarded, not a repeat; nothing stops it on either
   path. A delivered result's removal in the same case: the next page
   finds it `sent`; it ends `unconfirmed` (45 minutes on a legacy row, 24
   hours on a keeper row, unless a G31 lookup finds its result first). A
   wrong display, not a repeat. Possible fix (a later milestone): a tombstone list of removed
   ids in `localStorage`, which the next page applies before its first
   pass.
6. **A save after an answer the database never takes, then a reload.**
   The next page finds the send `waiting` with this page's claim: busy,
   then stale after a minute, and then it ends `failed`, "may or may not
   have been sent", although it was delivered once. A wrong display, never
   a second post. With G31's features a keeper-only send is posted again
   keeper-only instead and the keeper answers its stored receipt, and a
   lookup moves any keeper send it ended on: only a plain post to a hub
   without them keeps this limit.
7. **A `late()` write landing after a discard made after `ready`.** The
   older outbox's answer writes the send back as `sent`. A later page shows
   it `sent`, and it ends `unconfirmed` (45 minutes on a legacy row, 24
   hours on a keeper row, unless a G31 lookup finds its result first). It
   is never posted again.
8. **A send whose first write failed, then a reload before the database
   answers.** If the write did not land, the next page does not know the
   send: it is lost (nothing is posted during an outage, on any row), as
   the fallback note says. If it did land, the next page
   finds it `waiting` and posts it once, under a claim it records.
9. **A claim that landed but reported an error.** This page never posts
   the send (it heard the claim fail). The database holds this page's
   claim: after a minute it is stale, and the send ends `failed`, "may or
   may not have been sent", although it never left. A wrong display; the
   person can retry it. A keeper-only claim's send is posted again
   keeper-only instead (G31), which delivers it once.
10. **`save()` on a full tie keeps the stored copy.** A failed result on
   an `unconfirmed` entry (both terminal, the same attempts, no claim)
   leaves the stored copy, here and after a reload: the entry keeps the
   `unconfirmed` reason instead of the hub's note. A wrong display.
11. **A keeper send's result can be missed, only where the lookup cannot
   run.** G31's lookup settles a `sent` keeper send from the hub's store at
   every start and catch-up. What remains: a hub that does not list
   `command_lookup` (an older one), a page that neither reloads nor
   catches up while the result is published with `seq` 0 and missed (a
   stream that is live receives it), and a lookup that fails every time.
   Then, at 24 hours, the send ends `unconfirmed`, "The hub took this
   message, but its result was not seen on this device." A legacy-path
   send has no stored command to look up; it keeps its 45-minute end.
12. **An answer that may mean the send was applied still ends a plain
   post.** With a hub that lists `require_keeper`, a keeper-row send is
   posted keeper-only and a lost answer, a 5xx or an abandoned claim leads
   to a quiet repeat under the same id. It still ends "may or may not
   have been sent" when: the post was plain (the features were not known
   at its claim, the hub does not list `require_keeper`, or the row did
   not say keeper); the repeat was refused (409 `not_kept`: the session
   left its keeper; a 404; any other 4xx); the hub came back as a version
   that does not list `require_keeper`; or 24 hours passed. A later
   lookup can still move it on from the hub's store. The person can send
   it again.
13. **`late()` reads every answer that is not 2xx as "may have been
   applied".** After `stop()` with a post out, a 429, 401, 409
   `duplicate_client_id` or 422 answer leaves the send `ambiguous`. A plain
   post's send then ends "may or may not have been sent", wrong wording
   for those answers; safe, since nothing is posted again. A keeper-only
   one is posted again keeper-only (G31), and the hub's answer to that
   says what happened.
14. **A `sent` save held during an outage is replayed only when the page
   next touches storage.** Until then the database keeps the older copy,
   and other pages show it in doubt.
15. **A stale `waiting` copy can end a delivered send as not sent.** A tab
   holding a `waiting` copy it cannot post for 24 hours ends it "the hub
   could not be reached for 24 hours" and writes that over another tab's
   `sent`, because a terminal stage beats `sent`. The view then shows a
   delivered message as not sent. Fix: compare with the stored copy
   (`reconcile` first) before a deadline ends a `waiting` entry.
16. **A keeper-only send in doubt waits on the features.** While the
   stream is not live, or `/v1/status` has not answered, it is neither
   posted nor ended, and the sends typed after it wait behind it (sends go
   in order). Its 24-hour deadline still ends it.
17. **A keeper-only send in doubt is posted again for up to 24 hours.**
   While the hub answers each repeat with a 5xx or no answer at all, the
   repeat goes out on the outbox's backoff (at most every 30 s). Each is
   one request the keeper store answers without applying it.
18. **A hub swapped for an older version within the confirm's round
   trip.** The `/v1/status` read before a keeper-only post and the post
   are two requests. A hub process that stops and an older one that
   starts on the same port between the two would take the post on its
   legacy route, ignoring the field. The window is one round trip, and a
   swap takes a process exit and a start.

## What G12 and G13 must know

- **Import `src/core/index.ts` only.** Create one store per open session
  with `createSessionStore`, call `start()` when the view mounts and
  `stop()` when it unmounts. Read it with `useSessionStore(store,
  selector)`: select the smallest slice you can. A ping makes a new
  `session` object but leaves `items` and every other array as they were,
  so `(s) => s.session.items` does not re-render on a ping.
- **Timeline.** `session.items` is in journal order, one entry per `seq`,
  with `turnId`. `session.turns` has each turn's start, end, status and
  duration ("Worked for 2m 29s" is `endedAt - startedAt`, or `durationS`).
  A `tool` item holds its whole life: `status` (`running`, `ok`, `error`,
  `cancelled`), `summary`, `input`, the latest `text`, `output`,
  `fileEdits`. Live text is separate: `session.live` is rendered after the
  items of its turn and is replaced by the item at `message.completed`.
  The `id` pairs them (`MessageItem.id`, `LiveText.id`).
- **No coalescing in the core.** Work groups, prose-first grouping and
  "N tool calls" belong to G12's selectors over `items`.
- **Requests.** `session.pending` is the open ones, in the order asked. A
  `request` item shows the same request with its `resolution` (`source`,
  `decision`, `by`) once it ends, or `source: "dropped"` when the hub's
  state frame stopped listing it. Any device can answer; the first answer
  wins; "Allowed on iPhone" is `resolution.by` (a device id: map it through
  the devices list).
- **Composer.** `send()` is accepted at once and keeps the message
  through a reload. Draw each unfinished send from `sends` (one `shown` and
  one `reason` each, see "What a view reads"), never from
  `session.commands`. When a send's user message (same `commandId`) or its
  `queue[]` entry appears, draw that instead of the pending bubble; a `sent`
  send is still in `sends` until its result, so hide the bubble whose
  message is already in the timeline. `failed` and `unconfirmed` ones get
  the dismiss and send-again actions (`canDiscard`, `canRetry`). Show
  `outbox.note` when `outbox.persistent` is false. Steer is
  `send({text, mode: "steer"})`; **say that it stops the reply in progress**
  with one wording for every harness ([G1](G1-session-runtime-redesign.md),
  "Carried forward from wave 2", and G13's note on [G16](G16-more-adapters.md)).
  Disable the buttons that need a live connection from
  `store.refusal()` and show its `message`.
- **Queued tray.** `session.queue` is the hub's list (`command_id`, `text`,
  `by`, `mode`, `attachments`). Its actions are `queueAction`.
- **Read-only.** `readOnlyReason(session.row)` is the text for the bar that
  replaces the composer, or null.
- **Connection.** `connection.state` has eight values; the
  `ConnectionStatusDot` of G1 maps from them. `retryAt` and `detail` are
  there for a "Reconnecting in 4 s" line.
- **Native.** The Mac and iPhone shells call `store.getState().wake()` on
  foreground and network changes. Inside the shells the scheme handler adds
  the bearer: pass `fetchTransport({baseUrl: ""})` with no `headers`.
- **Tests.** Drive a view with `FakeTransport`, `FakeTimers` and
  `FakeEnvironment` from `src/core/test-support.ts`, and the recordings
  from `tests/fixtures/session-view/` (`loadFixture`).
- **G10's placeholder.** `SessionShell` still reads no session data (its
  breadcrumb shows the harness and the id). G12 replaces it using the
  store.
