# G8: the hub on keepers (Claude launch and Continue in SWE)

Status: built, behind `remote.session_view` (off by default). With the
setting off and no keeper left running, the hub behaves as before. The
design it implements is G1 sections 2, 6 and 7
([G1-session-runtime-redesign.md](G1-session-runtime-redesign.md)). It
is the first caller of the keeper
([G5-keeper-process.md](G5-keeper-process.md)), the store
([G6-sqlite-store.md](G6-sqlite-store.md)) and the Claude adapter
([G7-claude-stream-adapter.md](G7-claude-stream-adapter.md)).

| File | What it holds |
|---|---|
| [`src/swe/remote/runtime.py`](../../src/swe/remote/runtime.py) | `SessionRuntime`: launch, Continue in SWE, the command path, the event pump, restart recovery, the harness.json seed |
| [`src/swe/remote/hub.py`](../../src/swe/remote/hub.py) | the routes that branch to the runtime, `emit` for keeper-held sessions, v1 frames on the way out |
| [`src/swe/remote/catalog.py`](../../src/swe/remote/catalog.py) | `overlay_owned`: one row per keeper-held session |
| [`src/swe/remote/tail.py`](../../src/swe/remote/tail.py) | `TailManager.skip`, `drop`, `hand_back` |
| [`src/swe/remote/fork.py`](../../src/swe/remote/fork.py) | a fork through a keeper |
| [`src/swe/remote/journal.py`](../../src/swe/remote/journal.py) | `append_at`, `hold`, `drop` |
| [`src/swe/remote/adapters/fallback.py`](../../src/swe/remote/adapters/fallback.py) | `SESSION_SEED` |
| [`src/swe/remote/launchd.py`](../../src/swe/remote/launchd.py), [`commands.py`](../../src/swe/remote/commands.py) | `upgrade_plist`, `reload_detached`, `upgrade_service` |
| [`tests/test_remote_hub_keeper.py`](../../tests/test_remote_hub_keeper.py) | every rule below |

## Turning it on

```bash
swe config set remote.session_view true    # then restart the hub
```

Two gates, both needed for a session to go through a keeper:

1. `remote.session_view` is `true` in config.json (read once, when the
   hub starts).
2. `adapters.registry.adapter_for(harness)` resolves an adapter for the
   harness (harness.json's `capabilities.session`, else the fallback
   row).

With the setting off the runtime opens nothing: no `remote.sqlite`, no
thread, no socket. One exception: a hub that starts with the setting
off and finds a keeper still alive serves that session until it is
closed (its agent is running, and nothing else would ever stop it), but
starts nothing new.

A launch with `yolo: true` still takes the old path: the keeper has no
autonomy setting yet. An attach that names a channel (`mode: tmux`,
`headless`, `acp`) still gets that channel, and a kept tmux pane is
taken back as it is.

### harness.json seed

The fallback row keeps every optional feature off, so harness.json has
to turn on what the fixtures prove. When the hub starts with the
setting on, `SessionRuntime.seed_capabilities` writes
`fallback.SESSION_SEED` into `capabilities.session` of each harness the
table names (today: `claude`), if that harness is registered and its
entry declares no `capabilities.session`. An entry that declares one is
never changed, so an edit by hand stands. For Claude the seed is the
proven spawn line, `min_version` 2.0.0, and `steer`, `plan_mode`,
`fork`, `stop_task`, `options: [model, permission_mode]`; `rollback`
and the `effort` option stay off (no wire form). A test holds the seed
equal to `claude_wire.PROVEN`.

## What a keeper-held session is

A session with a row in the store's `sessions` table whose `control` is
`full` and whose `state` is not `closed`. `SessionRuntime.owns(key)`
answers it. Its catalog row says `ownership: attached`, `mode:
"keeper"`, `control: "full"`, `state` (the keeper's `session.state`),
`status` (the v1 status for that state), `adapter` and `caps`.

- **The key.** `protocol.session_key(harness, id)`, as for every
  session. For a launch the hub chooses the id (a UUID) and passes it
  to the adapter as `StartSpec.session_id` (new in this milestone;
  `spec.json` carries it), so the key is known before the keeper
  exists and matches the row the transcript later produces.
- **One row.** `catalog.overlay_owned` matches a keeper-held session to
  the aggregator's row by harness and the harness's own session id and
  lays the keeper's fields on top. A session with no transcript on disk
  yet (just launched) gets a row built from the store.
- **Not tailed.** `TailManager.sync` and `ensure` skip every key the
  runtime owns (`TailManager.skip`): the CLI still writes its own
  transcript, and tailing it would journal every message twice.

## The timeline

For a keeper-held session the keeper's log is the timeline, and the
hub's journal for that key is a copy of it:

- Each durable event from the keeper is appended with
  `Journal.append_at` under the keeper's own seq, as the v2 event it
  is. A seq the journal already holds is refused, so a replay after a
  reconnect lands each event once.
- An event the journal cannot write (a full disk) ends that
  connection: the hub takes nothing more from it, hangs up, and after
  `RESYNC_WAIT_S` (2 s) connects again from the journal's last seq.
  Taking the next event instead would leave a hole no replay fills,
  because a replay starts after the journal's last seq.
- The hub never numbers an event in that journal itself. `HubState.emit`
  for an owned key sends the event to the session's open streams with
  no seq and records it in the global journal only (ownership changes,
  `notify`, `session.meta`). The check and the append are one step
  under `Journal.hold(key)`.
- `message.delta` is never journaled, as before.

### Today's clients

Events reach a client that predates v2 through
`SessionRuntime.v1(key, event)`:

- `protocol_compat.to_v1` with the session harness's
  `attachments.at_refs` (Claude: `@/abs/path`).
- `input.requested` of kind `choice` with options becomes an
  `approval.requested` card whose rows are the options (v1 has no
  frame for a question; without this the session would look idle
  while it waits). A free-text question has no v1 frame; the status
  still turns `followup`.
- A `command.result` keeps its `command_id` as `client_id`, and its op
  is renamed to what v1 clients match on: `send` to `message`,
  `respond` to `approval`.

Where it runs: the live path publishes the converted frame on the bus;
`GET .../events` converts each journaled event during replay;
`GET .../messages` converts before it filters. A v2 event with no v1
frame is skipped but still moves the replay cursor. The global journal
and stream carry v1 frames only, so the inbox, the notifier and
`/v1/events` are unchanged. A journal that holds v1 history followed by
v2 events (a continued session) serves both.

`notify` events (`needs_you`, `finished`, `error`) are raised by the
runtime from the keeper's state changes, by the rule the tailer uses.

## Launch

`POST /v1/sessions` for a harness the runtime can drive:

1. The hub picks the session id and key and stores, in this order:
   the prompt as a `send` command under the id `<launch_id>-prompt`
   (`queued`), the launch (`pending`), the session (`starting`). It
   then writes `spec.json` and answers 202 with the `launch_id`. The
   prompt is stored before the launch is accepted, so a launch never
   exists without the message it owes.
2. A thread starts the keeper (`discovery.spawn`, provider keys in its
   environment, never in `spec.json`) and connects. Connecting
   delivers every stored command the keeper has not seen, the prompt
   among them (see "After a hub restart"); the thread then asks for
   the prompt's receipt under the same id.
3. The launch settles `started` (store and memory) and
   `session.launched` goes on the global stream, after a catalog
   refresh so the row exists.

There is no pending marker and no adoption on this path
(`adopt_pending` and `_pending_match` are not used).
`GET /v1/launches/<id>` falls back to the store, so it still answers
after a hub restart. A keeper that will not start settles the launch
`failed` with the last line it printed.

**One launch per id.** `Store.add_launch` claims the request's
`command_id` (or `client_id`), and the store allows one launch per id.
A retry, or a second request racing the first, is answered with the
launch the id already made (`duplicate: true`) and starts nothing: the
request that loses the claim drops its in-memory launch and fails its
own stored prompt. The same id with a different harness, folder,
prompt, model, effort or provider is refused (409
`command_id_conflict`).

**A launch cut off by a hub restart.** A launch still `pending` in the
store when a hub starts is settled there and then:

| What the new hub finds | Launch | Prompt |
|---|---|---|
| The keeper is up | `started` | delivered on connect if the keeper never saw it |
| No keeper, but the session ran (the keeper took the prompt, or the journal has events) | `started` | as the store has it |
| No keeper and nothing ever ran | `failed` ("the hub restarted while this was starting") | `rejected` with the same note; the session is closed |

`POST /v1/sessions/{key}/fork` starts a keeper with `fork: true` and
the source's id as `resume_id` when the adapter's caps allow fork;
otherwise the tmux fork answers as before.

## Commands

Every write to a keeper-held session (`message`, `approval`, `control`,
`queue`) goes through one path:

1. **An id.** `store.command_id_of(body)`: `command_id`, or `client_id`
   from a client that predates it. A write with neither gets one from
   `runtime.new_command_id` (`h` + 24 hex), which gives it a receipt
   but no deduplication, as before. The hub's own ids are minted the
   same way; the satellite command counter (`c1`, `c2`, ...) that
   restarted with the hub is gone (`c` + 24 hex).
2. **A receipt.** `Store.record_command(..., status="queued")` before
   anything is delivered. A repeat of the id answers the stored
   receipt (`duplicate: true`, `receipt`, and `result` once it
   finished) and delivers nothing. The same id with a different
   command is refused (409 `command_id_conflict`).
3. **Delivery.** `KeeperClient.request(op, command_id=...)` under the
   session's command lock. The keeper's answer moves the stored
   receipt: `accepted`, stays `queued`, or `rejected` with the reason.
   A connection that fails mid-request is retried once under the same
   id (the keeper answers `duplicate` if it already has it). A keeper
   that is gone is started first (see "Keeper lost").
4. **The result** arrives as the keeper's `command.result` event and
   is stored with `set_result`.

| Route | Keeper op | Answer |
|---|---|---|
| `message` | `send` (`mode` `queue` or `steer`) | 202 `{receipt, waiting, command_id, client_id}`; 409 `send_rejected` with the reason |
| `approval` | `respond` | 200; 409 `approval_failed` when someone answered first |
| `control` `interrupt` | `interrupt` | 200 |
| `control` `model`, `effort` | `set_option` | 200; 409 when the adapter cannot set it |
| `control` `detach`, `kill` | `stop`, then the session is let go | 200; 503 `keeper_failed` when the keeper did not stop (the session stays attached) |
| `control` `detach` `when_idle` | waits for the keeper's state to leave `running` | 202 |
| `control` `compact`, `clear`, `detach` `keep` | none | 409 `control_failed` |
| `queue` `edit`, `cancel` | `queue_edit`, `queue_remove` | 200 `{command_id}`; 409 `not_waiting` |
| `queue` `steer` | `queue_remove`, then `send` as a steer under an id made from the action's | 200 `{command_id}` |
| `GET queue` | (the last `queue.updated`) | the waiting sends, `client_id` being the id each was sent under |
| `command` (new) | `set_option`, `stop_task`, `interrupt`, `park` | 200 `{receipt, command_id}`; 409 `command_rejected`; 409 `not_kept` for a session no keeper holds |

`POST /v1/sessions/{key}/command {op, command_id?, ...}` is the one new
route: the keeper ops today's routes have no name for. `set_option`
takes `name` (`model`, `effort`, `permission_mode`, `plan_mode`) and
`value`; `stop_task` a `task_id`; `interrupt` an optional
`clear_queue`; `park` nothing. It is how plan mode is turned on until
the session view has its own control.

**Queue actions.** In a `queue` body `client_id` names the queued
message, so it is never read as the action's id. The action's own id
is `command_id` (optional): a retry under it answers what the first
try did, and the same id for another action is refused. Send now
sends the message as it stands: the last text edited through this
hub, else the text as it was sent, else the text the queue shows (the
queue shows text redacted, so that is used only when it differs from
the redacted original, which means an earlier hub edited it). The
text is stored in the `queue_remove` command (`resend`), and the steer
goes under an id made from the action's (`s` + 24 hex of its SHA-1),
so a retry after a crash between the two sends the same words once.

An approval `decision` of `allow`, `deny` or `always` goes as that
decision. A menu row number names the request's n-th option
(`option_id`, and `decision` too when the option's id is one of the
three).

**First answer wins.** Two devices answering one approval: the keeper
takes the first; the second is `rejected` with `unknown_request`, which
the hub answers 409, and a retry of the second gets that stored
refusal back.

## After a hub restart

`SessionRuntime.open` runs before the hub serves:

1. Every stored session that is not closed is loaded.
2. `discovery.discover`: for each keeper whose lock cannot be taken,
   connect with `last_seq` set to the journal's last seq for that
   session. The keeper replays what the hub missed, then goes live.
   The journal is exact because the store effects of an event are
   applied before the event is journaled, and every one of them can be
   applied twice: a hub that dies between the two is sent the event
   again.
3. `welcome` restores what events alone cannot after a restart: the
   state, caps, queue, and the open approval and input requests (the
   inbox and the notification tokens are filled from
   `welcome.pending`). It is applied before any event of its
   connection: the client's reader starts inside `connect`, so each
   connection's frames wait behind a gate that opens once the welcome
   is in. An event applied first would be overwritten by the older
   state the welcome carries.
4. A keeper found dead without a clean stop marks its session cut off
   (below). A live keeper the store does not know is recorded from its
   own `spec.json`.
5. **Commands with no result** (`Store.unfinished(session=)`) are
   checked against the keeper once its replay is in, here and after
   every later connect:

| The keeper says | The hub does |
|---|---|
| never saw it, op is `send` or `respond`, under 24 hours old | delivers it now |
| never saw it, any other op, or older | fails it: "the hub restarted before this was delivered" (a Stop or an option change that arrives late would act on a turn it was not meant for) |
| knows it, and it is in the queue | leaves it |
| knows it, no result, not queued | fails it: "lost when the session's keeper stopped; send it again" (a message a dead keeper took and never started is not sent again) |

A command failed this way gets a `command.result` on the session's
streams with no seq: the keeper's log has no event for it.

**A cursor ahead of the keeper.** A `welcome.last_seq` below the
journal's means this is not the log the journal followed. The journal
for that key is dropped (`Journal.drop`), open streams get
`journal.truncated`, and the hub reconnects from seq 0.

## Faults

| Failure | What the hub does |
|---|---|
| Agent crashes | Nothing special: the keeper's events (`approval.resolved {agent_exit}`, `turn.ended {failed}`, `session.state agent_down`) arrive and old clients see the card retired and status `error`. The next send restarts the agent in the keeper |
| Keeper lost (killed, crashed) | The connection ends; `discovery.find` says lost. Nothing is written to the timeline. Open streams get `approval.resolved {source: keeper_lost}` per open request and `session.status error`, with no seq. The next send starts a new keeper, which records `approval.resolved {agent_exit}` and `turn.ended {keeper_lost}` and resumes the harness session by its own id |
| Keeper exited cleanly while still ours (a logout, an upgrade cycle) | The session shows `parked`; the next send starts a keeper |
| Keeper connection dropped, keeper alive (`bye overflow`, a write timeout) | Reconnect with the journal's last seq |
| Keeper's lock held but its socket does not answer | It is on its way up or out (a killed process drops its sockets a moment before its lock): asked again up to 6 times, 250 ms apart, before the hub decides. Still unreachable: the session is marked cut off |
| Keeper will not start | The command is `rejected` with why; 503 `keeper_failed` |
| Keeper runs another swe version or an older IPC | `tick` stops it once the session is idle (no turn, no request, empty queue); the next send starts one on the current code |

Before every keeper start the hub sets `first_seq` in `spec.json` to
the journal's next seq, so a log that is reopened after the hub wrote
to the journal (a session let go and continued again) never reuses a
seq.

## Continue in SWE

`POST .../control {op: attach}` on an observed session whose harness
the runtime can drive (no `mode`, or `auto`):

1. Refused while anything else may write the session: status `active`
   (409 `not_idle`); a transcript that moved in the last 5 seconds
   (409 `not_idle`); a harness process that still has the session open,
   found by `supervise._local_writer_pids` (409 `session_in_use`,
   naming the pid). Unlike the tmux attach, that process is never
   stopped for the user.
2. Under `Journal.hold(key)` the session becomes owned and the
   journal's last seq is read; the tailer is dropped. From that point
   the tailer can no longer journal for the key, so `first_seq` is
   exactly the next seq.
3. The store row is written, `spec.json` gets `resume_id` (the
   harness's own id) and `first_seq`, and the keeper is started. The
   existing journal stays as the earlier history; the keeper's events
   follow it.
4. A keeper that will not start undoes it: the session is observed and
   tailed again.

### Letting a session go

`detach`, `kill`, a failed launch and the terminal-resume case below
all end in `SessionRuntime.close`: the keeper is stopped (`stop`), its
last events are taken in, the store row is `closed`, and the key goes
back to the tailer through `TailManager.hand_back`.

The session is let go only once no keeper holds its lock (looked for
up to 20 times, 250 ms apart). A keeper that cannot be reached or does
not stop keeps its session attached and the request answers 503
`keeper_failed`: letting go would leave an agent running that nobody
drives, beside whoever resumes the session next. The tailer's cursor
is written before the key stops being kept, so no tailer can start in
between from the cursor of before the keeper. Without a mark the
cursor file is removed, so the next tailer (which finds a journal)
starts at the transcript's end and journals nothing the keeper already
logged. The ownership event that follows is journaled by the hub under
the next seq.

### Resumed in a terminal while parked

A parked keeper records the transcript's `{path, offset}` in
`keeper.json`. Each catalog cycle `SessionRuntime.tick` compares the
file's size with that offset. Larger means someone resumed the session
elsewhere: after the keeper confirms it is still parked (asked on the
keeper's loop, under the command lock, so no send of ours can be in
between), the hub closes the session with `control: read_only` and the
reason, and hands the tailer that `{path, offset}`, so what the
terminal wrote is journaled from there. The row then says `ownership:
observed`, `control: read_only`, `control_reason`. Continuing it again
clears the note.

## Service files pick up G5's settings

`swe remote serve` calls `commands.upgrade_service` before it starts:

- **macOS.** A LaunchAgent installed before G5 lacks
  `AbandonProcessGroup`. `launchd.upgrade_plist` adds the key to the
  installed plist and keeps every other key. launchd reads a plist only
  when the job is loaded, so when this process is that job
  (`XPC_SERVICE_NAME` is the label) `launchd.reload_detached` runs
  `launchctl bootout` and `bootstrap` from a shell in its own session
  (the unload ends the hub; launchd ends a job's process group, and
  that shell is not in it). The hub comes back from the new plist and
  finds nothing to change. Run from a shell, the file is fixed and
  nothing is reloaded.
- **Linux.** Nothing in the unit file changed for keepers: a keeper is
  put in a scope unit of its own when it is started
  (`systemd.keeper_scope_argv`), so the new code is enough.

## Verification

Run on 2026-11-21, macOS 27.3, Python 3.15.2 and 3.12 (the suite).

| Check | Result |
|---|---|
| `tests.test_remote_hub_keeper` (77 tests) | pass, 20 runs in a row, and with no `claude` on `PATH` |
| Full suite against a throwaway HOME | pass (see the PR) |
| G1 Verification step 2, against an installed `swe` (`pip install -e .` in a fresh venv), a temp HOME, and harness.json pointing `capabilities.session.argv` at `tests/fakes/fake_claude.py`: `swe config set remote.session_view true`, `swe remote serve`, launch, send (queued), a retried launch and a retried send answered as duplicates, streamed text, `kill -9` the hub mid-turn with the approval open, restart it, the approval still in `/v1/inbox` and the message still in the queue, answer it (a second answer gets 409), the queued turn runs, journal seqs 1..39 equal the keeper's log, `GET /session/` 200, detach ends the keeper | pass |
| One manual run with the real `claude` 2.1.328 (model `haiku`, a throwaway folder, a throwaway `CLAUDE_CONFIG_DIR`, the recorder's isolation flags), through the hub's HTTP routes: a Bash approval mid-turn (the file appears only after Allow); plan mode turned on with the `command` route, a `kind: plan` approval, then a Write approval and the file; a subagent (`task.updated` running, running, completed); 70 events, no gap in seq | pass, 14 US cents reported |

## Interface changes to earlier milestones

- `StartSpec.session_id` (G3), `spec.json` `session_id` (G5), honoured
  by `ClaudeStreamAdapter` (G7, `--session-id`) and the fake adapter.
  An adapter that cannot choose its id ignores it; the catalog still
  shows one row, matched by the id the keeper reports.
- `Journal.append_at`, `hold`, `drop` (journal.py is G15's file; these
  are the additions G5's spec asked G8 for).
- `HubState(home, runtime={...})` passes options to the runtime (tests
  pass a spawner and a registry).
- `POST /v1/sessions/{key}/command` (above).

## Limits

- A keeper-held session's `compact` and `clear` controls answer 409.
- `yolo` launches and explicit attach modes stay on the old paths.
- Send now on a queued message is a remove and a new send. A hub that
  dies between the two has removed the message (its result says
  `cancelled`); a retry of the action under its `command_id` then
  sends it. An action sent with no `command_id` cannot be retried.
- An edit to a queued message is remembered by the hub that made it.
  After a hub restart Send now reads the text the queue shows, which
  is redacted: an edited message that held a credential is sent with
  `[REDACTED]` in its place.
- A free-text `input.requested` has no card on old clients.
- `effort` at launch is passed to the adapter as a spawn flag; the
  tmux-channel check (`supervise.fresh_launch_error`) still runs first,
  so a pick the tmux channel refuses is refused here too.
- The store, keeper folders and keeper-held journals are never pruned
  by the hub (`Store.prune` exists and nothing calls it yet).
- Queue rows have no `queued_at` (the keeper does not record one).
- Satellites do not run keepers yet (G15); a satellite session is
  untouched.
- `hand_back` with a mark relies on the tailer resolving the same
  transcript path the adapter derived; when the two differ the tailer
  starts at the end instead.

## What G9 must know

- **Where v2 events are.** A keeper-held session's journal holds v2
  events at the keeper's seq, with no gaps of the hub's making; serve
  a `v=2` stream from it without `runtime.v1`. A continued session's
  journal starts with v1 events: a v2 client needs those converted the
  other way or served as history.
- **Live v2.** The bus carries v1 frames. `SessionRuntime.listeners`
  is called with `(key, event)` for every v2 event after it is
  journaled, in order, on the keeper connection's thread: subscribe a
  v2 fan-out there.
- **Events with no seq.** Hub-made frames for a keeper-held session
  (`session.ownership`, `session.meta`, `notify`, the lost-keeper
  `approval.resolved` and `session.status`, a failed command's
  `command.result`) reach open streams only. A v2 snapshot should
  carry their effect (`runtime.session(key)` holds state, caps, queue
  and pending).
- **Snapshot inputs.** `welcome` already gives `pending`, `live`,
  `open_turn`, `queue`, `caps`; the hub keeps all but `live` and
  `open_turn`.
- **`epoch`.** The store column is unused. Bump it when a journal is
  dropped (`_connect`'s cursor-ahead branch) so clients refetch.
- **`/v1/status`.** `protocols`, `features` and `boot_id` are not added
  yet.
- **Commands.** `POST .../command` (and `runtime.command(key, op,
  ...)` behind it) delivers `set_option`, `stop_task`, `interrupt` and
  `park` with a receipt. `rollback` has no route yet.

## What G15 and G16 must know

- **G15.** `queue_command` ids are now unique for ever, so they can go
  in the store as they are. The hub runs keepers for its own machine
  only (`list_sessions(machine=)`).
- **G16.** A new adapter gets its launch and Continue paths for free
  once `adapter_for` resolves it. Add a `SESSION_SEED` row only for
  what its fixtures prove. If the harness assigns its own session id,
  the key the hub chose will differ from the harness's id: the row
  still merges, but the journal key and a later observed row's key
  will not match, so decide how such a session is keyed before
  shipping it.
