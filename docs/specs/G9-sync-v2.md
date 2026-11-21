# G9: sync v2 (handshake, catch-up, snapshot, live text, ping)

Status: built. It works with `remote.session_view` on or off, for every
session the hub lists; a client that does not ask for it gets today's
frames, byte for byte. The design it implements is G1 section 3
([G1-session-runtime-redesign.md](G1-session-runtime-redesign.md)). It
serves the journal G8 fills
([G8-hub-on-keepers.md](G8-hub-on-keepers.md)) as the v2 events of G3
([G3-adapter-protocol.md](G3-adapter-protocol.md)). The React client
core (G11) is its first reader.

| File | What it holds |
|---|---|
| [`src/swe/remote/sync.py`](../../src/swe/remote/sync.py) | `SyncHub` (fan-out to v2 streams, the live text, the snapshot, the epoch) and `V2Stream` (one open stream: replay or snapshot, overflow recovery, ping) |
| [`src/swe/remote/hub.py`](../../src/swe/remote/hub.py) | `status_view`, the `v=2` branches of `events` and `messages`, the `snapshot` route, `sse_frame_v2`, the three places an event is handed to `SyncHub` |
| [`src/swe/remote/protocol.py`](../../src/swe/remote/protocol.py) | `PROTOCOLS`, `FEATURES`, `VERSION_HEADER`, `PING_S`, `SILENCE_S`, `REPLAY_MAX`, `SNAPSHOT_MESSAGES`, `wants_v2`, `sync_frame` |
| [`src/swe/remote/protocol_compat.py`](../../src/swe/remote/protocol_compat.py) | `to_v2`: one v1 event as its v2 event |
| [`src/swe/remote/schema/session_v2.json`](../../src/swe/remote/schema/session_v2.json) | the JSON schema of every v2 frame, the snapshot, a messages page and the status body |
| [`src/swe/remote/schema/__init__.py`](../../src/swe/remote/schema/__init__.py) | `build` (the schema, made from `events.py`), `validate`, `python3 -m swe.remote.schema --write` |
| [`src/swe/remote/runtime.py`](../../src/swe/remote/runtime.py) | four small changes, listed under "Interface changes" |
| [`tests/test_remote_sse_v2.py`](../../tests/test_remote_sse_v2.py) | every rule below |

## Asking for v2

```
GET /v1/status
  {"ok": true, "v": 1, "protocols": [1, 2],
   "features": ["sync_v2", "snapshot", "live_resume", "delta_offset",
                "ping", "command_id", "require_keeper", "command_lookup",
                "session_options", "session_runtime"],
   "boot_id": "9f2c41d07ab35e18"}
```

- `v` stays 1: today's clients read it. A v2 client reads `protocols`.
- `features` names what a client may rely on. `session_runtime` is
  listed only when the hub runs sessions through keepers.
- `boot_id` is new for every hub process. A client that sees it change
  knows the hub restarted, and asks again about what it sent with
  `GET .../commands/{id}` (`command_lookup`, G31), never by posting it
  again: the legacy route does not de-duplicate.
- The route is still unauthenticated and still names nothing about the
  machine.

A client asks for v2 per request, with `?v=2` or the header
`X-SWE-Protocol: 2`. Anything else (no value, `1`, an unreadable value)
is a client of today's protocol. The query wins over the header.

| Route | With `v=2` | Without |
|---|---|---|
| `GET /v1/sessions/{key}/events` | the v2 stream below | today's stream, unchanged |
| `GET /v1/sessions/{key}/messages` | a page of the timeline as v2 events | today's page of `message.appended` |
| `GET /v1/sessions/{key}/snapshot` | always v2 (the route is new) | |
| `GET /v1/events` (every session) | today's stream: v2 is per session | today's stream |

## The frames

Every frame is one SSE event: `event:` is its `type`, `data:` its JSON.
The envelope is G3's: `{v: 2, type, session, seq?, ts, turn_id?,
command_id?, data}`.

| `seq` | Means | SSE `id:` line |
|---|---|---|
| above 0 | the event's place in the session's journal. The client's cursor is the highest it has seen | the seq |
| 0 | a frame of an event type that the hub made for the stream. It is not journaled and never moves a cursor | none |
| absent | `message.delta`, and the stream's own frames | none |

So `Last-Event-ID` is always a journaled seq.

The stream's own frames, beside the event types of
[`events.py`](../../src/swe/remote/adapters/events.py):

| Type | `data` | Sent |
|---|---|---|
| `session.snapshot` | the snapshot body (below) | first, when the catch-up cannot be a replay; again whenever the epoch moves |
| `session.state` with `live` | `{state, live[], pending[], head, epoch}` (`seq` 0) | last in every catch-up; again when the hub's view changes with no journaled event (a keeper reconnects or is lost, the session changes hands) |
| `session.row` | `{row}`: the session's row as `GET /v1/sessions` lists it | before the state frame of every catch-up that is a replay (a snapshot has the row in it); again when ownership, control, the title or a pin changes |
| `ping` | `{ts, head}` | every 10 s |

`session.state` is also a journaled event (`{state, reason?,
harness_session_id?}`, `seq` above 0). The catch-up's frame is the same
type with four more fields and `seq` 0; a client tells them apart by
`live` being there.

| Field of the state frame | Meaning | What a client does |
|---|---|---|
| `state` | the session's state as the hub holds it now | takes it |
| `live[]` | each message in progress: `{id, kind, text, turn_id?, done?}`, `text` being all of it so far | replaces every live text it holds with these |
| `pending[]` | the ids of the requests still open | drops any request card whose id is not listed (ids it does not know are left alone: their event is on its way) |
| `head` | the seq this stream is caught up to | sets its cursor to it |
| `epoch` | the session's epoch | keeps it for the next connect |

## Catch-up

```
GET /v1/sessions/{key}/events?v=2&last=<seq>&epoch=<E>
```

`last` is the client's cursor (`Last-Event-ID` wins over the query, as
on today's stream). `V2Stream.catch_up` decides:

| The request | The stream sends |
|---|---|
| `epoch` is the session's epoch, `last` is not past the journal's head, the head is at most `REPLAY_MAX` (500) seqs ahead, and the journal still holds everything after `last` | each journaled event after `last`, in order, then `session.row`, then `session.state` |
| anything else: no `epoch` or an unreadable one, another epoch, a cursor of 0 on a journal that has events, a cursor past the head, a wider gap, a stretch the journal's ring dropped | one `session.snapshot`, then `session.state` |

- A client with no state connects with no `epoch` and is sent the
  snapshot. One that is up to date is sent only `session.row` and
  `session.state`.
- The row is sent on every replay because a change to it while the
  client was away (a new title, a detach) has no event to replay for a
  keeper-held session, and no v2 event for any other.
- A cursor of 0 is never replayed: a ring that was trimmed still reads
  "from the start" with nothing missing, so a replay could not tell
  what it left out, and a client with no cursor has nothing to add to.
- Deltas are never replayed: they were never journaled. The state
  frame's `live[]` is what restores a reply in progress.
- The stream subscribes before it reads the journal, and skips what the
  read already covered, so nothing falls between the two. Events
  journaled while the catch-up was being built are sent before its
  state frame (see "Live text").
- There are no `journal.truncated` or `journal.caught-up` frames in v2.
  The state frame is the "caught up" mark.

## Live text

`message.delta` is never journaled (the decision stands), so a journal
replay alone cannot restore a reply that is still being written. The
hub keeps, per session, the text so far of every message in progress
(`SyncHub._live`):

| Source | What it does |
|---|---|
| a delta from a keeper | appended to that message's text |
| a keeper's `welcome.live` on every connect | replaces what the hub had (it missed deltas while it was restarting or the connection was down). Open streams are sent `session.state` again once the keeper's replay is in, not before: a client that got the text first would clear it at a replayed `turn.ended` |
| a v1 delta (an API channel of a session no keeper holds, or a satellite's) | the same, with the offset counted by the hub |
| `message.completed`, `thinking.completed` | ends the text with that `id` |
| `tool.started` | ends the text with that `call_id` |
| `turn.ended`, or `session.state` idle, agent_down, parked, closed | ends all of it |

An event replayed from before a welcome (its seq is at or below
`welcome.last_seq`) never ends text: that text was written after it.

**Every catch-up ends with the state frame**: a small-gap replay, a
snapshot (which also carries `live`), and an overflow recovery alike.

**The state frame sits at an exact place in the stream.** The live
text and a stream's queue change in one step under `SyncHub`'s lock,
and a state item is put in the queue holding the text as it stands at
that moment. So every event queued before it is sent before it, and
nothing after it is counted in it. A catch-up ends by putting its state
item in the queue and sending what is ahead of it (`V2Stream._settle`):
events journaled while the catch-up was being built go out first, and a
delta queued before the item is dropped, because that text holds it.
After a state frame, the next delta for a message starts exactly where
its `live[]` text ends, and no later event is older than the frame.

## Delta offsets

Every `message.delta` on a v2 stream carries `offset`: where its text
starts in the message. The hub guarantees it equals the length of the
text the hub holds for that message:

- an adapter's own `offset` is passed on when it agrees;
- a delta with no offset (v1, or an adapter that sends none) gets the
  hub's count;
- an offset past the hub's text is put where the hub's text ends, so
  what clients hold and what `live[]` says never part;
- an offset inside the text (an adapter taking text back) is passed on
  as it is and the hub cuts its text there. No adapter does this today.

The client rule, which the tests run from the server side with a
reference client (`Client` in the test file):

| The delta's `offset` against the text held for its message | The client |
|---|---|
| equal to its length | appends |
| behind it, and the delta ends at or before the end (text it already has, which happens right after it took a snapshot by `GET`) | ignores the delta |
| anything else (a hole) | drops the delta, stops appending to that message, asks for `GET /v1/sessions/{key}/snapshot`, replaces its live text with the body's `live[]`, and carries on |

A reply is never left incomplete: `message.completed` carries the whole
text and replaces whatever was streamed.

## No silent loss

Each stream has a queue of 2000 frames. When it fills, nothing is
dropped silently (`V2Stream._recover`):

1. The stream is marked, and stays out of the fan-out's way (the hub
   never blocks on a slow client).
2. At its next step the stream empties its queue, then reads the
   journal again from the last seq it sent. An event journaled after
   the queue was emptied is queued again; one journaled before is in
   the read; the skip-what-was-sent rule removes the overlap.
3. It sends them as a catch-up does: those events, then
   `session.row`, then `session.state` with the live text. If the gap
   is wider than `REPLAY_MAX`, or the epoch moved meanwhile, it sends a
   snapshot instead.

Events reach a stream's queue in seq order: the hub hands each one to
`SyncHub` while it still holds that journal's lock (`HubState.emit` for
a session the hub numbers, `SessionRuntime._on_event` for a keeper's).
A frame the hub made for the stream (`seq` 0) is not in the journal, so
an overflow can lose one; the state and row frames that end the
recovery carry what those frames said, except a failed command's
result (see "What G11 must know").

## Ping

A v2 stream sends `ping {ts, head}` every `PING_S` (10 s), whatever
else it sent. `head` is the stream's own: the seq it has sent up to,
which is the cursor its client should hold. A client treats `SILENCE_S`
(25 s) with no frame of any kind as a dead connection. Today's stream
keeps its `: ka` comment every 15 s and gets no ping.

The cadence runs on `SyncHub.clock`, which tests replace; `wake()`
makes open streams look at the clock again.

## Epoch

A session's epoch is a number that changes when its journal's numbering
starts over or loses a stretch, so a cursor from before is never
replayed against a timeline it does not belong to.

- It is the `epoch` column of the session's row in `remote.sqlite`
  (unused until now). A session the store has never held is epoch 0.
- `SyncHub.new_epoch` moves it. The runtime calls it wherever it used
  to publish `journal.truncated`: when a keeper's log is behind the
  journal (the journal is dropped and taken again from seq 1), and when
  a keeper says events the hub never read are gone from its log.
- Open v2 streams are sent a snapshot on the new epoch at once.

## Snapshot

`GET /v1/sessions/{key}/snapshot` and the `data` of a `session.snapshot`
frame are the same body (`SyncHub.snapshot`), true at `head`:

| Field | What |
|---|---|
| `session`, `epoch`, `head`, `boot_id` | the key, the epoch, the journal seq the body is true at, the hub's boot id |
| `row` | the session's row, with `unread` for the calling device; null if the list no longer has it |
| `state`, `reason?` | the session's state, as in the state frame |
| `caps` | the adapter's capabilities, or null for a session no keeper holds |
| `options?` | the last `session.options` (G32, [G32-session-options.md](G32-session-options.md)); sent by a hub that lists `session_options` |
| `open_turn` | the id of the turn in flight, or null |
| `pending[]` | each open `approval.requested` or `input.requested`, as its event |
| `queue[]` | the messages waiting: `{command_id, text, by?, mode?, attachments?}` |
| `live[]` | the text in progress, as in the state frame |
| `plan` | the last `plan.updated` (`{items, text?}`), or null |
| `tasks[]` | the last `task.updated` of each task still running |
| `checkpoints[]` | each `checkpoint.created` with its `seq`, `ts` and `turn_id`, the newest 200 |
| `changes` | the last `changes.updated`'s `files`, or null |
| `usage` | the last `usage.updated`, or null |
| `messages[]` | the newest `SNAPSHOT_MESSAGES` (200) timeline events at or below `head`, oldest first |
| `has_more`, `next_before` | whether older timeline events exist, and the `before` that pages to them |

- **Timeline events** are every durable event except the bookkeeping a
  view does not draw: `session.state`, `session.caps`, `queue.updated`,
  `command.receipt`, `command.result`, `usage.updated`,
  `changes.updated`, `notify`, `tool.updated`
  (`sync.BOOKKEEPING_TYPES`). Turn boundaries, requests and their
  answers, tasks, plans and checkpoints are in.
- **Older pages**: `GET .../messages?v=2&before=<next_before>&limit=N`
  answers `{session, messages, has_more, next_before}` with the same
  events.
- **Where each field comes from.** For a keeper-held session `state`,
  `caps`, `pending` and `queue` are the runtime's own
  (`runtime.session(key)`), which a keeper's welcome restores after a
  hub restart; a lost keeper reads `agent_down` with nothing pending,
  queued, live or in flight. `plan`, `tasks`, `checkpoints`, `changes`,
  `usage` and `open_turn` are folded from the journal (`sync.Fold`):
  once per session, then carried on from the seq the last fold stopped
  at, for the 32 sessions asked about most recently.
- The runtime's view can be an event ahead of `head` (it applies an
  event before it journals it). The event then follows on the stream,
  so a client applies requests and answers by id and never counts on
  seeing each once.

## Sessions no keeper holds

A terminal session, a satellite's, and the earlier history of a session
continued in SWE have journals of v1 events. A v2 client reads them as
v2 all the same (`protocol_compat.to_v2`), on the stream, in the
snapshot and in a messages page:

| v1 | v2 |
|---|---|
| `message.appended` kind user, assistant, system | `message.completed` with that kind; v1's `approval` kind (a line of transcript) is `system` |
| kind thinking | `thinking.completed` (`duration_s` kept) |
| kind tool_use | `tool.started` (`call_id`, `name`, `summary` from `args_summary`, `input` from `args_full`) |
| kind tool_result | `tool.completed`, `status: ok`, `call_id` from `result_ref` |
| kind compaction, error | `context.compacted`, `error` (`code: error`) |
| `message.delta` | `message.delta` with the hub's `offset` |
| `session.status` | `session.state`: active running, done idle, followup waiting, error agent_down |
| `approval.requested` | the same, `kind: tool`, each menu row `{n, label}` as option `{id: n, label}` |
| `approval.resolved` | the same; no `source` is `user`, a v1-only source (`gone`, `detached`, `dismissed`, `keeper_lost`) is `harness` |
| `command.result` | the same, `command_id` from `command_id` or `client_id`, op `message` as `send` and `approval` as `respond` |
| `notify` | the same, for the kinds v2 has |
| `session.ownership`, `session.meta` | no event: a `session.row` frame (and `session.state` after an ownership change) |
| a tool_use message whose call a legacy channel streamed text for (`acp:<nonce>:<turn>:tool:<call_id>`) | `tool.started`, then `session.state`: the event carries the call's id, not the stream's, so the state frame is what retires that text |
| anything else | nothing |

- v1 messages carry no id. A journaled one is `j<seq>`. The message
  that completes a streamed reply is given the id of the oldest live
  text of its kind instead, so a client retires that text when the
  message lands.
- A journaled event with no v2 frame still counts as sent: the stream's
  `head` covers it.
- Message text is cleaned of what a harness injects, as on today's
  stream (`injected.clean`); a message that is only an injection has no
  frame.
- The result always passes `events.validate`; a v1 event that cannot be
  made valid has no frame.

## What the hub says with no journaled event

For a keeper-held session the keeper numbers the journal, so the hub's
own frames have no seq (G8). On a v2 stream:

| What happened | v2 streams get |
|---|---|
| The keeper is lost (killed, crashed) | `session.state`: `agent_down`, `pending: []`, `live: []` |
| The keeper exited cleanly and the session shows parked | `session.state` |
| A keeper connects or reconnects | the events it replays, then `session.state` with the welcome's live text |
| Ownership or control changes (detach, kill, resumed in a terminal, Continue in SWE) | `session.row`, then `session.state` |
| A title or a pin changes | `session.row` |
| A command the hub failed (`"the hub restarted before this was delivered"`) | `command.result` with `seq` 0 |
| A `notify` the hub raised | `notify` with `seq` 0 |
| The journal was dropped or has a hole | `session.snapshot` on the new epoch, then `session.state` |

## The schema is a public contract

[`session_v2.json`](../../src/swe/remote/schema/session_v2.json) is
JSON Schema 2020-12. G11 generates its TypeScript types from it, so a
change to it is a change to every client:

- **It is generated, never edited.** `schema.build()` makes every
  event's definition from `events.SCHEMAS`, and adds the stream's own
  frames, the snapshot, the messages page and the status body.
  `python3 -m swe.remote.schema --write` writes the file; `--check`
  exits 1 when it is stale.
- **It cannot drift from `events.py`.** A test holds the committed file
  equal to `build()`, checks each type's fields, required fields and
  enums against `events.SCHEMAS` and `events.ENUMS`, and runs one
  sample of every event type (and broken copies of it) through both
  `events.validate` and `schema.validate`, which must agree. A new
  event type fails the test until it has a sample.
- **What the wire adds to `events.py`** is declared in two tables
  (`WIRE_OPTIONAL`, `WIRE_REQUIRED`): the state frame's `live`,
  `pending`, `head`, `epoch`, and `offset` being required on a delta.
- **No nulls for "left out".** `events.validate` takes an optional
  field that is null as left out, so a journal may hold one. The schema
  does not, and the hub leaves such a field out when it serves the
  event (`sync.wire`), a null `turn_id` or `command_id` included. A
  tool's own `input` is passed on as it is.
- **Names.** `$defs` has `<Type>Event` and `<Type>Data` per event type
  (`tool.completed` is `ToolCompletedEvent`), the shared objects
  (`Caps`, `Option`, `FileEdit`, `PlanItem`, `ChangedFile`,
  `QueuedMessage`, `LiveMessage`, `Checkpoint`, `Row`), the frames
  (`SessionSnapshotFrame`, `SessionRowFrame`, `PingFrame`), and the
  entry points `Frame` (anything a stream sends), `Event`, `Snapshot`,
  `MessagesPage`, `Status`.
- **`x-swe`** carries the constants a client needs: the version, the
  header name, `ping_s`, `silence_s`, `replay_max`,
  `snapshot_messages`, the feature names.
- **Adding** an optional field or an event type is compatible. Removing
  or renaming one, or making a field required, is not: it needs a new
  feature name in `protocol.FEATURES`, or a new protocol number.
- `schema.validate(value, name)` checks a value with the standard
  library only. The tests run every v2 frame they read through it.

## Verification

Run on 2026-11-21, macOS 27.3, Python 3.15.2.

| Check | Result |
|---|---|
| `tests.test_remote_sse_v2` (50 tests) | pass, 20 runs in a row |
| Full suite against a throwaway HOME | pass (see the PR) |
| Against an installed `swe` (`pip install -e .` in a fresh venv), a temp HOME, and harness.json pointing `capabilities.session.argv` at `tests/fakes/fake_claude.py` in its `slow` mode: `swe config set remote.session_view true`, `swe remote serve`, pair, `GET /v1/status`, launch. A v2 client connects while the reply is being written, takes the snapshot, and disconnects. The hub is killed with `kill -9` and started again. The client reconnects with its cursor and epoch | pass (see the PR for the transcript) |

## Interface changes to earlier milestones

- `runtime.py` (G8), five changes: a keeper's delta is handed to
  `hub.sync.on_event` before its v1 frame is published; a durable event
  is journaled and handed to `hub.sync.on_event` under
  `Journal.hold(key)`, so v2 streams get events in seq order whichever
  connection's thread brought them; `_take_welcome` hands
  `welcome.live` and `welcome.last_seq` to `hub.sync.on_welcome`; the
  keeper's `live` frame (its replay is in) calls `hub.sync.resync`;
  `_truncated` calls `hub.sync.new_epoch`.
- `HubState.watched` counts open v2 streams, so a client that only
  holds a v2 stream keeps the hub out of low power.
- `protocol_compat.to_v2` and `STATE_FOR_STATUS` (G3's file).
- `GET /v1/status` answers three more fields
  (`tests/test_remote_hub.py` pins the body).
- `HubState.boot_id`, `HubState.sync`, `HubState.status_view`.
- Nothing in `adapters/` changed: the snapshot, the row and the ping
  are frames of the stream, not adapter events.

## Limits

- A session the store has never held is always epoch 0. If its journal
  is swept (`TailManager.sweep_stale`) and written again from seq 1, a
  cursor from before is caught when it is past the new head, and not
  otherwise. Today's stream has the same gap.
- The journal is a ring (4000 events or 16 MiB). A fold started after a
  trim does not see a plan, a checkpoint or a running task whose event
  the ring dropped. A fold that was already running keeps what it saw.
- `/v1/events` (every session at once) is v1 only.
- A stream's frames are encoded once per stream, not shared between
  streams as today's are.
- A v1 `tool_result` has no status, so it reads `ok`.
- `queue[]` of a session no keeper holds lists the hub's waiting sends
  only; a satellite's wait on the satellite, as in `GET .../queue`.
- An `EventSource` that reconnects by itself keeps the `epoch` its URL
  was built with. After the epoch moves, each such reconnect gets a
  snapshot until the client opens the stream again with the new one.
- Satellites do not send v2 events yet (G15); their sessions are read
  through `to_v2`.

## What G11 must know

- **Types.** Generate them from `session_v2.json`; `Frame` is the union
  a stream sends, discriminated by `type`. Read the constants from
  `x-swe` instead of copying them.
- **Connect.** First time: `GET .../events?v=2` with no cursor; the
  first frame is `session.snapshot`, the second `session.state`. After
  that: `?v=2&last=<cursor>&epoch=<epoch>`. Treat a `session.snapshot`
  at any point as "replace everything", and a `session.row` as "replace
  the row".
- **Cursor.** The highest `seq` seen, then `head` of every state frame
  and every ping. Never a `seq` of 0.
- **One reducer.** A journaled `session.state` and the catch-up's are
  one type: take `state`; when `live` is there also replace the live
  text, prune request cards to `pending`, and take `head` and `epoch`.
- **Requests and the queue by id.** The snapshot can be one event ahead
  of `head`, so the same `approval.requested` or `approval.resolved`
  may arrive again. Apply by `request_id` and `command_id`.
- **Live text** is keyed by a delta's `id`. It ends on
  `message.completed` or `thinking.completed` with that `id`, on
  `tool.started` with that `call_id`, on `turn.ended`, and when a state
  frame's `live[]` leaves it out. Apply the offset table above.
- **Timeline items are keyed by `seq`.** `data.id` only pairs a
  message with its live text; for a session no keeper holds it is
  `j<seq>`, or the live text's id.
- **Silence.** No frame for `SILENCE_S` (25 s): the connection is dead.
  Reconnect with the cursor.
- **Restart.** A changed `boot_id` (status, or a snapshot's) means the
  hub restarted. A frame the hub made for the stream (`seq` 0) is not
  replayed after a reconnect or an overflow, so a command still waiting
  for its result is asked about again with `GET .../commands/{id}`
  (G31). A message is posted again after an answer that may mean it
  was applied only with `require_keeper: true`, and only when its
  earlier posts carried it too (after an answer that stored nothing,
  such as a 429, it is posted again as it was): the keeper path answers the stored receipt and result without
  applying it twice, and the legacy route, which would type it in
  again, refuses such a post (409 `not_kept`).
- **Read-only sessions** use the same frames. `row.control` and
  `row.control_reason` say why there is no composer; `caps` is null.
- **Fixtures.** `Client` in `tests/test_remote_sse_v2.py` is a reference
  client in 80 lines: the cursor, the state frame, the offset rule. The
  frames those tests read are what `tests/fixtures/session-view/**`
  should hold.
