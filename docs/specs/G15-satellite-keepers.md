# G15: satellite keepers and the loopback local API

Status: built, behind `remote.session_view` (off by default), which is
read on each machine for that machine. With the setting off on a
satellite, the satellite behaves as before. The design it implements is
G1 section 3, "Satellites" and "Standing alone", and the satellite row
of section 7
([G1-session-runtime-redesign.md](G1-session-runtime-redesign.md)). It
builds on the hub's keeper code
([G8-hub-on-keepers.md](G8-hub-on-keepers.md)) and the store's held
commands ([G6-sqlite-store.md](G6-sqlite-store.md)).

| File | What it holds |
|---|---|
| [`src/swe/remote/agent.py`](../../src/swe/remote/agent.py) | `RemoteAgent.local` (the satellite's own hub state and session runtime), the upload of keeper journals, `LocalApi` (the loopback API), `SatelliteKeepers` (the hub's side) |
| [`src/swe/remote/journal.py`](../../src/swe/remote/journal.py) | `append_at`, `hold`, `drop` (added by G8, unchanged here) |
| [`src/swe/remote/hub.py`](../../src/swe/remote/hub.py) | three calls into `agent.py`: `mirrored` in `HubState.emit`, the `POST /v1/agent/kept` route, `to_keeper` before a write is forwarded to another machine |
| [`src/swe/remote/commands.py`](../../src/swe/remote/commands.py) | `swe remote serve` starts the hub machine's loopback API |
| [`tests/test_remote_agent.py`](../../tests/test_remote_agent.py) | every rule below (`TestSatelliteForwarding`, `TestSatelliteCommands`, `TestSatelliteRestart`, `TestLocalApi`) |

## Every machine has the same runtime

A satellite used to be a tailer and an injector with an uplink. It now
also holds what a hub holds for its own sessions: `RemoteAgent.local`
is a `HubState` over the satellite's home, with its own journal, its
own `SessionRuntime`, and its own `remote.sqlite`.

- **One catalog and one tailer per machine.** `RemoteAgent.catalog` and
  `tail_manager` are the local state's. The tailer still emits through
  `RemoteAgent._capture`.
- **Everything is journaled twice on a satellite.** `_capture` writes
  each event to the machine's own journal (`data/remote/journal/`, the
  same files a hub writes) and then to the uplink outbox, as before. An
  event the local state emits itself (a route on the loopback API, the
  session runtime) takes the same two steps through `LocalHub.emit`.
- **"Only the hub writes `remote.sqlite`"** now reads: on each machine,
  the one process that runs its sessions writes that machine's store.
  On a satellite that is `swe remote agent`. A machine runs a hub or an
  agent, never both (the service installers already refuse the pair).
- **Live text.** A keeper's `message.delta` reaches the satellite's own
  streams and, converted to the v1 delta, the hub through the existing
  delta uplink. It is never journaled on either machine.

`RemoteAgent(hub, credential, home, runtime={...})` passes `runtime`
to the session runtime as `HubState(home, runtime=...)` does (tests
give a spawner and a registry). `RemoteAgent.close()` stops the agent's
own work and lets go of every keeper; the keepers run on.

## Keepers on a satellite

The agent starts, reconnects to and stops keepers through the same
`SessionRuntime` code as the hub. What differs is who asks.

| The hub sends | The satellite does |
|---|---|
| `launch` (not `yolo`), harness has an adapter | `SessionRuntime.launch` under the hub's own `launch_id`, which is also the launch's `command_id`. `session.launched` goes up when the keeper has the prompt; a launch that fails sends `command.result {op: launch, ok: false, launch_id}`. A replay of the command after a restart answers the launch already made |
| `attach` with no `mode` (or `auto`), harness has an adapter | Continue in SWE through the machine's own attach route, with its checks (mid-turn, transcript moved, another process holds it) |
| `kept` (new, below) | the client's request, given to the machine's own route |
| `message`, `approval`, `control` for a session a keeper holds | the same route. A hub that stores no commands for keeper sessions (older, or its session runtime off) sends these. The message's id is the client's `client_id` when it sent one |
| anything else | as before |

**The machine's own route.** A command for a keeper session is not
re-implemented in `agent.py`. `LocalApi.call` builds the HTTP request a
local client would send and runs it through the hub's request handler
in this process, over a socket pair. No port is involved, so it works
when the loopback API could not bind. `X-SWE-Device` names the device
that sent the command to the hub; the handler reads it only from a
caller that presented the token.

After a restart the agent's first catalog refresh may not have run when
a command arrives. `_local_post` refreshes the catalog once when the
runtime owns a key the list does not have.

## The hub's copy of a keeper session's journal

For a session a satellite keeper holds, the hub's journal for
`key@machine` is a copy of the satellite's journal for `key`: the same
events under the same seqs.

### Upload

`RemoteAgent._push_kept` runs before every outbox push, on the one
uplink thread. It posts to `POST /v1/agent/kept`:

```json
{"machine_id": "m1", "hello": false,
 "sessions": [{"key": "claude:<id>", "head": 41,
               "row": {"harness": "claude", "harness_session_id": "<id>",
                       "cwd": "/work", "adapter": "claude_stream"},
               "events": [{"v": 2, "seq": 40, "...": "..."}],
               "released": false}],
 "results": [{"command_id": "m1", "via": "c0f...", "status": 202,
              "body": {"receipt": "accepted"}}]}
```

and is answered `{"ok": true, "acked": {"claude:<id>": 41}, "released":
[]}`.

- **The hub holds the cursor.** The agent keeps no cursor on disk.
  `acked` is the last seq the hub's copy holds. After an agent restart,
  and each time the command stream comes back up (`_hub_back`), the
  agent forgets what it was told and asks again: its first upload for a
  session carries `head` and `row` and no events. It then sends what
  follows `acked`, at most 50 events and one body's size at a time.
- **Idempotent.** The hub stores each event with `Journal.append_at`
  under the seq it carries. A seq the copy already holds is skipped, so
  an upload sent twice lands once and publishes nothing twice.
- **A gap is never closed by renumbering.** Seqs may skip ahead (the
  satellite's ring dropped events while the hub was away). An event
  older than the copy's last is refused, never given another number.
- **An event the hub could not write** (a full disk) ends that
  session's part of the upload. `acked` stays where it was and the
  agent sends from there again, so the hole is filled under the same
  seqs.
- **An event the hub will not take** (it fails the v2 schema, or is a
  v1 type a satellite may not upload) is skipped, and `acked` moves
  past it so it is not sent for ever.
- **An event too big for a body** is sent with its long strings cut,
  alone in an upload so it has the whole body; it keeps its seq. One
  that cannot be cut to fit is sent as its seq alone, which the hub
  refuses and moves past, so the copy carries on after it.
- **Effects first, the journal second**, as for a local keeper: the
  session's `state`, and the receipt and result of a command that came
  through this hub, are written to the hub's store before the event is
  journaled. Each can be applied twice.
- **Publishing.** Each stored v2 event goes to the session's open
  streams as today's clients read it (`SessionRuntime.v1`) and to the
  hub runtime's `listeners` as the v2 event, so approvals reach the
  inbox and a v2 stream (G9) needs nothing new. A v1 event in a copy
  is the session's history from before its keeper. The hub published
  it when the satellite's tailer first uploaded it, so it is stored
  and not published again: its notification would be raised twice and
  an approval long answered would reopen.
- **Whose commands.** A copied `command.receipt` or `command.result`
  changes a stored command only when that command was stored for this
  machine and this session. A satellite cannot settle a command held
  for another machine, or one of the hub's own.
- **`closed` is the hub's word.** A copied `session.state closed` does
  not close the hub's row: a closed row means the hub stopped copying,
  and only a release says that. A hub restarted between the keeper's
  last event and the release still knows it copies that journal.

The agent is woken to upload by the session runtime's listener, so an
event leaves within the same tick it was journaled.

### The hub numbers nothing in a copy

`HubState.emit` asks `agent.mirrored(state, key)` beside
`runtime.owns(key)`. While the hub copies a session's journal, an event
of the hub's own for that key (`command.queued`, an ownership change, a
title, a `notify` the satellite uploaded) reaches open streams with no
seq and is recorded in the global journal only. The check and the
append are one step under `Journal.hold(key)`, and `take` adds the key
to the copied set under the same lock.

A copied session has a row in the hub's `sessions` table with the
satellite's id as `machine`. `SatelliteKeepers` loads the set from
there when the hub starts.

### When a copy starts over

| What the hub finds | What it does |
|---|---|
| First upload for a key whose journal the hub numbered itself (a terminal session, continued under a keeper) | drops its journal (`Journal.drop`), tells open streams `journal.truncated`, and takes the satellite's journal from its first event. The satellite's journal holds the session's earlier events too, so they come back under the satellite's seqs |
| `head` below the copy's last seq (the satellite's journal started over) | the same |
| `released: true` and the copy has everything up to `head` | stops copying: the row is `closed`, and the hub numbers that journal itself again, from the next seq |

### Letting a session go

When the satellite's runtime no longer holds a session (`detach`,
`kill`, resumed in a terminal), the agent marks it released in
`agent-kept.json` and uploads up to the keeper's last event, then
`released: true`. What the satellite journals after that (its tailer is
back) travels the ordinary way and the hub numbers it.

- **The outbox waits for the release.** `_push_kept` answers False
  while any released session is not yet released on the hub, and the
  uplink loop then sends nothing else. Otherwise the tailer's first
  events would reach a hub that still copies the journal and numbers
  nothing in it, and they would never be journaled there. A release
  the hub has not taken after 60 seconds (`KEPT_RELEASE_WAIT_S`) stops
  holding the outbox; it is still asked for.
- **Taking a release twice changes nothing.** The hub records where the
  copy ended (`sessions.last_seq`) when it releases. A release for a
  copy that ends at or before that seq, for a key the hub no longer
  copies, is answered `released` again and nothing is dropped: the
  answer to the first was lost, or the agent restarted before it heard
  it. A release question carries `released: true` too, for the same
  reason. A copy that ends later is the session continued again, and
  starts a new copy.

## Commands: hub to satellite to keeper

`agent.to_keeper` is asked before the hub forwards a write to another
machine's session. It takes the request when the session's row says
`mode: "keeper"` and the hub's store is open. `message`, `approval`,
`control` (all but `attach`), `queue` and `command` go this way;
uploads and `attach` keep the old path.

1. **An id.** `command_id`, or `client_id` from a client that predates
   it, or one the hub mints. In a `queue` body `client_id` names the
   queued message, as on the hub.
2. **A receipt, before anything is sent.** The command is stored in the
   hub's `commands` table as `held` (reason `machine asleep`), with the
   checked fields of the request as its payload. A repeat of the id
   answers the stored receipt (`duplicate: true`) and sends nothing; the
   same id for another command is 409 `command_id_conflict`.
3. **Delivery.** When the machine's command stream is up, the hub
   flushes that machine's held commands at once, the new one last:
   each is claimed with `transition(id, "queued")` and put on the
   stream as a `kept` command. The request answers 202 with `receipt:
   "queued"`. When the machine is asleep, the command stays `held` and
   the request answers 202 with `receipt: "held"`.
4. **On the satellite** `_run_kept` gives the request to the machine's
   own route under the same `command_id`, on the session's lane, so a
   session's commands and the uploads a message names stay in order.
   The satellite's store and its keeper each refuse an id they have
   seen.
5. **The answer.** The agent posts what the route answered as a
   `results` entry and only then marks the stream command done. An
   agent that stops first runs the command again after its restart;
   the route then answers the duplicate, and that answer goes up. The
   hub stores a refusal (and tells open streams, with no seq, since no
   keeper logged it) and an answer that is final at once (a control, a
   queue edit). A message's result arrives later as the keeper's
   `command.result` in the copied journal. `via` is the stream
   command's own id; it moves the stream's backlog floor as a
   `command.result` from the satellite used to.

Two commands never reach the route (`RemoteAgent._kept_refusal`):

| The command | The satellite answers |
|---|---|
| Read too late. The hub puts a command on the stream when it believes the machine is awake; a machine that fell asleep a moment before reads it when it wakes. A message or an answer older than 24 hours, or anything else older than 10 minutes (`KEPT_FRESH_S`), by the hub's timestamp on the command | 409 `expired`, which the hub stores as a rejection |
| For a session no keeper holds here any more (a replay after a restart, the session let go since). The route would take the old path and type into a pane | the stored receipt when this machine's store has the id; `already detached` for a detach or kill; else 409 `not_kept` |

The `kept` command:

```json
{"op": "kept", "command_id": "c0f...", "session": "claude:<id>",
 "route": "message", "id": "m1", "device": "<device id>",
 "body": {"text": "again", "mode": "queue", "attachments": [],
          "command_id": "m1"}}
```

### Held commands

| Rule | How |
|---|---|
| What is held | `send` and `respond` (a message, and the answer to a request). `control`, `queue` and `command` answer 409 `machine_offline` while the machine is asleep: a Stop delivered hours late would act on a turn it was never meant for |
| How long | 24 hours (`store.HOLD_S`), from the hub's clock |
| When they go | on the first upload after the command stream comes back (`hello: true`), before any command sent after it, and from the hub's timer every 60 seconds |
| Order | arrival order (`list_held`), under a per-machine lock, so two flushes cannot interleave |
| Once | `transition(id, "queued")` is the claim; a second flush finds nothing held |
| Expiry | `expire_held()` runs before every flush and on the timer. A hold past its deadline is `rejected` with reason `expired`, is never put on the stream, and open streams get a `command.result {ok: false}` with no seq. A retry of its id answers the expiry |
| Backlog | a machine more than `MAX_SAT_PENDING` commands behind gets no more: the command is `rejected` and the request answers 429 |

## The loopback API

Every machine binds one on `127.0.0.1`: the hub's own request handler
over that machine's state (`LocalHandler`, a subclass of
`hub._Handler`), so the route bodies are the hub's and nothing is
written twice.

- **Port.** `remote.local_port` in config.json, else 8476. `0` asks for
  any free port. A port that is taken prints one line and leaves the
  API off; the hub or agent runs on.
- **Who may call.** A bearer token, compared in constant time with the
  one in `data/remote/local.json`. Reading that file is the proof the
  caller runs as this account. No paired credential, tailnet identity
  or notification token is accepted, and the token is accepted nowhere
  else.
- **`local.json`.** `{"token", "url", "port", "pid"}`, mode 0600,
  tightened on every write and whenever the token is read. The token is
  made on first use and kept for good; `url`, `port` and `pid` are
  rewritten at each bind. The token is never printed, logged or
  returned by a route.
- **Routes.** `/v1/sessions` and everything under it, `/v1/launches/*`,
  `/v1/events`, `/v1/status`, `/v1/machines`, `/v1/folders`,
  `/v1/inbox`, and the static files. Everything else under `/v1`
  (pairing, devices, SSH keys, `/v1/app`, the satellite routes) answers
  404 `not_found`.
- **Host.** A request whose `Host` is not a loopback name answers 403
  `bad_origin`: a page that rebound its domain to 127.0.0.1 arrives
  under its own name.
- **The caller's device id** is `local`.

On a satellite the API serves that machine's sessions with the hub
unreachable: list, read, send, answer, launch, Continue in SWE. On the
hub machine it serves what the hub serves, satellites included.

## After a satellite restart

`RemoteAgent.__init__` builds the local state last, and opening its
runtime does what a hub restart does (G8, "After a hub restart"):

1. Every keeper whose lock cannot be taken is reconnected with
   `last_seq` set to the satellite journal's last seq, replays what the
   agent missed, and goes live. Open approvals, the queue and the state
   come from `welcome`.
2. The copied set comes from `agent-kept.json` and the runtime's
   sessions. Every `acked` starts unknown.
3. When the command stream is up, the next upload says `hello`, the hub
   answers how far each copy reaches and puts its held commands on the
   stream, and the upload carries on from `acked + 1`.

Stopped mid-turn and started again, the two journals end equal to the
keeper's log: no gap, nothing twice.

## Verification

Run on 2026-11-21, macOS 27.3, Python 3.15.2.

| Check | Result |
|---|---|
| `tests.test_remote_agent` (219 tests, 47 new) | pass |
| Full suite against a throwaway HOME | pass (see the PR) |
| Against an installed `swe` (`pip install -e .` in a fresh venv), two throwaway HOMEs on one Mac, `swe remote serve` in one and `swe remote agent` in the other, harness.json on the satellite pointing `capabilities.session.argv` at `tests/fakes/fake_claude.py` (`FAKE_CLAUDE_MODE=approval`): a launch on the satellite asked for on the hub; an approval and a send from the hub answered by the satellite's keeper; `kill -9` the agent with an approval open, a message sent meanwhile answered `held` (its retry `duplicate`, a Stop 409 `machine_offline`); the agent started again, the approval answered, the held message run once; 55 events, seqs 1..55, the satellite's journal and the hub's copy both equal to the keeper's log; then the hub stopped and the satellite's loopback API listing the session and running one more turn, 401 without the token or with a paired credential, 404 for `/v1/devices`; `local.json` mode 0600 and its token in no other file under either HOME, logs included; detach ends the keeper | pass |

## Interface changes to earlier milestones

- `hub.py` (G9's file this wave): `HubState.emit` asks
  `agent.mirrored`; `do_POST` routes `/v1/agent/kept` to
  `agent.kept_route` and asks `agent.to_keeper` before `_forward`.
  Six lines added, two changed.
- `commands.py`: `swe remote serve` calls `agent.serve_local(state)`.
- `RemoteAgent.catalog` and `tail_manager` are now the local state's;
  `_capture` also journals on the satellite; `_enqueue` is the old
  `_capture` (outbox only).
- New on the wire: `POST /v1/agent/kept`, the `kept` stream command,
  `receipt: "held"` and 409 `machine_offline` on a write to a satellite
  keeper session.
- New files: `data/remote/local.json`, `data/remote/agent-kept.json`.
  New setting: `remote.local_port`.

## Limits

- **The hub's session runtime must be on too.** A hub with
  `remote.session_view` off (or one older than this) answers the upload
  409 or 404. The agent then asks again every 60 seconds; meanwhile the
  hub lists the session but has no timeline for it, and commands take
  the old forwarding path with no hold and no stored receipt.
- **History from before this version.** A satellite journals a session
  from the first event it reads after the upgrade. Continue a session
  it tailed before that, and the hub's copy starts where the
  satellite's journal starts: earlier messages leave the hub's view of
  that session.
- **Only messages and answers are held.** Stop, option changes and
  queue edits need the machine awake.
- **The queue of a satellite keeper session** is not listed by `GET
  .../queue` on the hub (v1). Its `queue.updated` events are in the
  copied journal.
- **Fork** of a satellite keeper session still takes the tmux path.
- **A satellite's launch is not held.** `launch` and `attach` still
  wait in the command stream's journal with no deadline, as before.
- **A hub that dies between claiming a held command and putting it on
  the stream** leaves it `queued` and undelivered. The window is two
  adjacent statements; nothing replays it.
- **Clocks.** The ten-minute limit on a late Stop compares the hub's
  timestamp with the satellite's clock. Two machines whose clocks
  disagree by more than that refuse each other's controls.
- **One process per machine writes the store.** Running `swe remote
  serve` and `swe remote agent` over one `~/.swe` by hand is not
  guarded here; the service installers refuse it.

## What G17 and G19 must know

- **G17 (Mac embed).** The app reads `~/.swe/data/remote/local.json`
  for `url` and `token` and sends `Authorization: Bearer <token>` from
  the scheme handler, as it does with a hub credential. The file is
  there once `swe remote serve` or `swe remote agent` has started; if
  `url` is missing the API never bound (port taken), and `pid` says
  whether the process that wrote it is still up. The API serves the
  session view's files at `/session/` and the same `/v1` session routes
  as the hub, so `SessionWebShell` needs no second code path: point it
  at `url`. On a satellite Mac the list is that Mac's sessions only. A
  request must carry a loopback `Host` (`127.0.0.1:<port>` or
  `localhost:<port>`). `/v1/app/*` is not served; pages that need it
  still go to the hub.
- **G17.** The port is 8476 unless `remote.local_port` says otherwise
  (8478, one above the hub's, is taken on Kai's Mac by another tool).
  Read it from `local.json`, never assume it.
- **G19 (checkpoints).** A checkpoint is made on the machine that runs
  the keeper. On a satellite that is the agent process:
  `RemoteAgent.local` is the `HubState` to hang a checkpoint store on,
  and `local.runtime` the runtime that sees turn boundaries. The
  shadow store therefore lives under the satellite's `~/.swe`, not the
  hub's.
- **G19.** `checkpoint.created` and `checkpoint.restored` are ordinary
  v2 events: once the keeper logs them they are in the hub's copy with
  no further work. A restore is a command, so give it a route under
  `/v1/sessions/{key}/` and add its name to `_KEPT_OPS` in `agent.py`;
  it then travels hub to satellite under its `command_id` like the
  rest. Do not add it to `HELD_OPS`: a restore must not arrive a day
  late.
- **Both.** `SatelliteKeepers._apply` is the one place the hub reacts
  to a copied event. A new event that must change hub state (a list
  row, the store) goes there, and must be safe to apply twice.
