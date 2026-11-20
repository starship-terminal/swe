# G5: keeper process, IPC, discovery, service survival

Status: built (nothing in the hub calls it yet; wiring it in is G8).
The design it implements is G1 section 2, "Keeper", "Surviving the
service manager", "IPC" and "Discovery", and the agent and keeper rows
of the section 7 fault table
([G1-session-runtime-redesign.md](G1-session-runtime-redesign.md)). It
is the first caller of the event log
([G4-keeper-event-log.md](G4-keeper-event-log.md)) and of the adapter
interface ([G3-adapter-protocol.md](G3-adapter-protocol.md)).

| File | What it holds |
|---|---|
| [`src/swe/remote/keeper/process.py`](../../src/swe/remote/keeper/process.py) | `Keeper`: the command loop, the send queue, agent start, restart and park, the client connections |
| [`src/swe/remote/keeper/ipc.py`](../../src/swe/remote/keeper/ipc.py) | the socket folder and its checks, framing, `negotiate`, `KeeperClient` (the hub's end) |
| [`src/swe/remote/keeper/discovery.py`](../../src/swe/remote/keeper/discovery.py) | `keeper.json`, the lock, `discover`, `find`, `connect`, `write_spec`, `spawn` |
| [`src/swe/remote/keeper/__main__.py`](../../src/swe/remote/keeper/__main__.py) | `python -m swe.remote.keeper --session=<key>` |
| [`src/swe/remote/keeper/log.py`](../../src/swe/remote/keeper/log.py) | two additions: `EventLog.snapshot()` and `EventLog.read_back()` |
| [`src/swe/remote/launchd.py`](../../src/swe/remote/launchd.py), [`systemd.py`](../../src/swe/remote/systemd.py) | `AbandonProcessGroup`, `keeps_children`; `keeper_unit`, `keeper_scope_argv` |
| [`src/swe/remote/__init__.py`](../../src/swe/remote/__init__.py) | `cmd_remote` is imported on first use (see Memory) |
| [`tests/test_remote_keeper.py`](../../tests/test_remote_keeper.py) | every rule below |
| [`scripts/keeper_survival_check.py`](../../scripts/keeper_survival_check.py) | the hub service restart check, against the real launchd or systemd |

## One keeper

One process per live session, started by `discovery.spawn`. It owns the
agent's adapter (and through it the agent's stdio), the send queue and
the session's `EventLog`. Its folder:

```
data/remote/keepers/<slug>/     0700; <slug> is protocol.key_slug
  keeper.lock                   flock'd for as long as the keeper lives
  keeper.json                   0600, who it is (below)
  spec.json                     0600, what to start (below)
  keeper.out                    0600, its stdout and stderr
  log/                          the event log (G4)
```

Threads: the command loop is the process's main thread and the only
one that calls the adapter, as G3 requires. A reader and a writer
thread per client, the accept thread and the adapter's own reader hand
it work through one inbox. Log subscribers run under the log's lock, so
they only append to a client's queue.

The keeper imports everything it will ever use when it starts,
built-in adapters included, so a reinstall that replaces the files on
disk does not change a keeper that is already running.

### spec.json

Written by the hub with `discovery.write_spec` before `spawn`:
`{v: 1, session, harness, cwd, resume_id, fork, model, effort,
permission_mode, plan_mode, env, first_seq}`. The keeper resolves the
harness with `adapters.registry.adapter_for` itself. The keeper writes
to this file too: an option change that took effect is recorded in it
(`discovery.update_spec`), so the next keeper of the session starts the
agent with the settings in force, not the ones it was launched with. `env` is stored on
disk, so nothing secret goes in it: a secret for the agent goes in
`spawn(env=)`, which becomes the keeper's process environment and
reaches the agent from there, never through a command line or a file.

### keeper.json

`{v: 1, session, slug, pid, socket, ipc: {version, min}, state,
harness, harness_session_id, started_at, updated_at, boot, swe_version,
token, transcript?}`, replaced atomically. `state` is `starting`,
`running`, `parked`, `agent_down` or `exited`. `transcript` is the
harness transcript's `{path, offset}` at the moment the session was
parked: a larger file later means someone resumed it in a terminal
(G1 section 6; acting on that is the hub's job).

### Exit codes

0 stopped cleanly, 2 bad usage or no usable `spec.json`, 3 no adapter
for the harness (the reason is in `keeper.out`), 4 another keeper holds
the session, 5 the log or the socket could not be opened.

## IPC

A Unix socket per keeper at `/tmp/swe-<uid>/k-<24 hex>.sock`
(`SWE_RUNTIME_DIR` overrides the folder). The name is a hash of the
keeper's folder, so a test home and the real one never share a socket.
A path under HOME can pass the 104 bytes macOS allows a socket address;
this one is about 45.

Who may connect:

- The folder is created 0700 and both ends refuse it (`ipc.Unsafe`)
  unless it is a real folder, not a link, owned by this user, with no
  access for anyone else. The socket is 0600.
- The client checks that the socket file is a socket owned by this
  user. Both ends check the peer's uid (`SO_PEERCRED` on Linux,
  `LOCAL_PEERCRED` on macOS).

Frames are one JSON object per line, like `acp.py`. A line over 16 MiB,
or one that is not a JSON object, ends that connection only.

### The handshake

```
client  {"t":"hello","v":1,"min_v":1,"last_seq":41,"client":"hub"}
keeper  {"t":"welcome","v":1,"last_seq":57,"pending":[...],"live":[...],...}
keeper  {"t":"gap","oldest_seq":50,"reason":"evicted","marker":{...}}   only if history was dropped
keeper  {"t":"event","e":{...}}     replay from disk: seq 42 to 57
keeper  {"t":"live","last_seq":57}  the replay is over
keeper  {"t":"event","e":{...}}     seq 58 on, and deltas, as they happen
```

- `last_seq` in the hello is the last seq the client holds: 0 replays
  everything, `null` skips the replay.
- `welcome` carries the state the client joins at: `last_seq`,
  `first_seq`, `oldest_seq`, `pending` (the open approval and input
  requests, as their events), `live` (the text so far of each message
  in progress), `open_turn`, `state`, `agent` (`up`, `down`, `parked`,
  `none`), `queue`, `caps`, `harness_session_id`, `pid`, and the
  keeper's own version range (`keeper_v`, `keeper_min_v`).
- No gap: the keeper subscribes the client to the log first, replays
  from disk up to the seq it subscribed at, sends `live`, then sends
  what arrived meanwhile. Deltas have no seq: `welcome.live` is the
  text at the moment of subscribing and every later delta follows.
- `gap` means the client asked for history the log no longer has
  (`reason: evicted`, with the `log.truncated` marker) or never had
  (`before_start`: the session was continued from the hub journal).
  The events from `oldest_seq` on still follow. It can also come in
  the middle of a replay, when the cap drops segments the replay has
  not reached yet. The hub should serve
  its own clients a snapshot.

### Versions

`ipc.VERSION` is 1 and `ipc.MIN_VERSION` is 1. Each end offers a range
`[min_v, v]`; the keeper picks the highest version inside both ranges
(`ipc.negotiate`) and says so in `welcome.v`. If the ranges do not
meet it answers `{"t":"refused","reason":"version","v":..,"min_v":..}`
and closes; the client raises `ipc.Refused`.

After a reinstall the hub is newer than the keepers it finds. It offers
`[MIN_VERSION, VERSION]`, so it ends up speaking each old keeper's
version, and should stop and respawn a keeper once its session is idle
(`stop`, then `spawn`) to move it to the new code. Raising
`MIN_VERSION` cuts off keepers older than that: do it only a release
after the version it drops.

Both ends ignore frame types and fields they do not know, and an
unknown op is answered `rejected` (`unknown_op`), so a newer peer can
add any of them without a version bump. A bump is for a change an older
peer would misread.

### Commands

`{"t":"cmd","id":<n>,"op":...,"command_id":...}` is answered by
`{"t":"reply","id":<n>,"status":...,"op":...,"command_id":...}` with
`reason` when rejected. `status` is the receipt.

| Op | Fields | Does |
|---|---|---|
| `send` | `text`, `attachments?`, `device?`, `mode?` (`queue`, `steer`) | starts a turn, or queues behind the one running; `steer` adds to the running turn where `Caps.steer` allows, else queues |
| `interrupt` | `clear_queue?` | stops the turn; with `clear_queue` also empties the queue |
| `respond` | `request_id`, `decision?`, `option_id?`, `text?`, `device?` | answers an approval or input request |
| `set_option` | `name`, `value` | `model`, `effort`, `permission_mode`, `plan_mode`; kept for every later agent start |
| `stop_task` | `task_id` | gated by `Caps.stop_task` |
| `rollback` | `turn_id` | gated by `Caps.rollback` |
| `queue_edit` | `target`, `text` | rewrites a queued message |
| `queue_remove` | `target` | drops one; its send gets `command.result {ok: false, cancelled: true}` |
| `park` | | parks an idle session now |
| `stop` | | closes the agent and ends the keeper |
| `status` | | no `command_id`; state, queue, pending ids, caps, restarts left, log stats, what recovery found |
| `has_command` | `command_id` | no receipt; `{known, status}` |
| `ping` | | answered by the connection's reader, even while the loop is busy |

The rules:

- **Logged before it runs.** Every op but `status`, `has_command` and
  `ping` needs a `command_id`. The keeper writes a `command.receipt`
  (`accepted`, `queued` or `rejected`, fsynced by the log) and replies
  before it calls the adapter, then writes a `command.result` when the
  call returns or fails.
- **Not recorded, not run.** If the log cannot write the receipt, or
  cannot fsync it (a full disk, an I/O error), the command is answered
  `rejected` with `not_recorded` and nothing is applied. A receipt that
  was written but not fsynced may or may not be found by a later
  keeper, so that id stays refused: a second `rejected` receipt is
  written and a retry answers `duplicate` with `first: rejected`. Send
  it again under a new id. `stop` is honoured either way.
- **Never twice.** A `command_id` seen before is answered `duplicate`
  with `first` (the first answer) and nothing is written or applied.
  The last 4096 ids are kept and rebuilt from the log at start, so this
  holds across a keeper restart. `has_command` is how the hub's store
  reconciles after its own crash (G6).
- **One order.** The loop takes commands one at a time, whichever
  client sent them.
- **First answer wins.** A `respond` for a request that is not open
  (never asked, or already answered by another device) is `rejected`
  with `unknown_request`.

### A client that falls behind

A client is never waited for. Each has a queue of at most 8192 frames
(`CLIENT_BUFFER`); past that the keeper sends what was queued, then
`{"t":"bye","reason":"overflow"}`, and closes. A write that blocks for
10 seconds (`SEND_TIMEOUT_S`) closes the connection too. Either way the
client reconnects with its last seq and replays, so nothing is lost
silently. `bye` with `stopped` or `signal` means the keeper is ending.

## The send queue

The queue lives in the keeper. A `send` while a turn is open (or while
older messages still wait) is answered `queued` and appended; every
change is a `queue.updated` event carrying the whole queue. When a turn
ends the oldest message is sent.

- Rebuilt at start from the newest `queue.updated` in the log, read
  backward (`EventLog.read_back`) over at most 64 MiB.
- **The queue is written before the receipt.** A queued send, an edit
  and a removal each write `queue.updated` first and the receipt
  second, and the receipt's fsync carries both to disk. A crash between
  the two leaves a queued message with no receipt, which the next
  keeper rebuilds and counts as taken (a retry answers `duplicate`),
  never a receipt whose message is gone. If the receipt cannot be
  recorded, the change is taken back.
- **Bounded, so it always fits one frame.** One message is at most
  1 MiB as JSON with at most 100 attachments (`too_large`), and the
  queue at most 100 messages and 4 MiB (`queue_full`); a frame may be
  16 MiB.
- **Redacted where it is shown.** `queue.updated`, `welcome.queue` and
  `status` carry each message's text after the same redaction an
  adapter applies to a message it sends. The text as typed stays in
  the keeper's memory and is what the agent receives. A queue rebuilt
  after a keeper restart therefore holds the redacted text.
- A message taken off the queue and not yet started when the keeper
  dies is not sent again (at most once): its command has a receipt and
  no result, which is what the hub's reconcile looks for.
- A turn the agent began by itself (G7, "implicit turns") answers a
  send with `busy`: the message goes back to the head of the queue.
- An agent that dies before it took a message leaves it at the head of
  the queue too.
- `interrupt` stops the turn and the queue then runs on, unless the
  command says `clear_queue`. That default is a guess at what a person
  pressing Stop wants; G8 or the session view can pass the flag.

## The agent's life

| Event | What the keeper does |
|---|---|
| Keeper starts | rebuilds the queue and ids, retires what a dead keeper left open (below), starts the agent, sends the queue |
| Agent will not start | `error` and `session.state agent_down`; the next send tries again (counted) |
| Agent dies mid-turn | the adapter records `approval.resolved {agent_exit}`, `turn.ended {failed}`, `session.state agent_down`; the keeper drops the adapter and keeps the queue |
| Send while `agent_down` | restarts the agent, resuming the harness session, at most 3 times in 10 minutes; past that the send is `rejected` with the wait in minutes |
| Idle for 30 minutes | parks: closes the agent, records `session.state parked`, stays alive |
| Send while parked | starts the agent again, resuming the harness session; not counted as a restart |
| `stop`, SIGTERM, SIGINT | closes the agent (`turn.ended {interrupted}`, `session.state closed`), then the log, then the socket and the lock |

- A signal stops the keeper whichever thread it lands on: the main
  thread sleeps on its inbox, and a Python handler would wait for it to
  wake, so the signal is passed through `signal.set_wakeup_fd` to a
  small thread that posts the stop.
- The agent is restarted only by a send, never by itself. Messages
  queued when it died are sent first, in order.
- Idle means: agent up, `session.state idle`, no open turn, no pending
  request, empty queue. Any command or turn restarts the count. A
  session waiting on an approval is never parked.
- Parking closes the agent, not the session, so the adapter's final
  `session.state closed` is recorded as `parked`.
- **Options.** A `set_option` that took effect is kept for every
  later start of the agent, in memory and in `spec.json`. Plan mode is
  a mode like the permission modes: choosing a permission mode leaves
  plan mode, turning plan mode on keeps the permission mode to return
  to, and an approved plan (`kind: plan`, allowed) ends plan mode. So a
  restart applies the last change, never an older one.
- Every start after the first passes the harness's session id as
  `resume_id`. The id comes from the adapter's `start`, and after a
  keeper restart from `keeper.json` or the log's last `session.state`.

### After a keeper dies

The agent dies with its keeper (its stdin closes). One that ignores
that would run on beside its replacement, both writing one session, so
the next keeper ends it first: a keeper leads its own process group and
its agent is a member, and when the old leader is gone but the group
still has members, they get SIGTERM, then SIGKILL after 2 seconds
(`_reap_orphans`). A pid is not handed out again while a group still
carries it, so this cannot reach a stranger.

The next keeper of
the session opens the same log and, before it starts the agent, writes
`approval.resolved {source: agent_exit}` for every request
`log.recovery.pending` shows open and `turn.ended {status:
keeper_lost}` for `log.recovery.open_turn`. Until a new keeper starts,
nothing is written: a hub that finds a lost keeper (`KeeperInfo.lost`)
should show the turn as cut off and start a new keeper on the next
send. A torn last line is cut by the log on open
(`status.recovery.torn_bytes`); the timeline continues at the next seq.

## Discovery

The lock is the truth: the kernel drops it when the process ends,
however it ends. `keeper.json` is only a description. The lock sits on
its own file, `keeper.lock`, because `keeper.json` is replaced by
rename and a lock would stay on the old inode.

- `discovery.discover(home)` returns a `KeeperInfo` per keeper folder.
  `alive` is true when the lock cannot be taken. For one that is gone,
  `lost` is true unless `keeper.json` says `exited`.
- A hub that starts calls `discover(home, alive_only=True)` and
  `discovery.connect(info, last_seq=...)` for each, with the last seq
  its journal holds for that session.
- A second keeper for a session fails with `KeeperRunning` (exit 4).
  The probe in `discover` holds the lock for an instant, so a starting
  keeper tries the lock for half a second before it gives up.

## Starting a keeper, and outliving the hub's service

`discovery.spawn(session_key, home=, env=)` starts `python -m
swe.remote.keeper --session=<key>` with `start_new_session=True` (its
own session and process group), stdin closed, output to `keeper.out`,
and the same `swe` package first on `PYTHONPATH`. It returns once the
new keeper has written `keeper.json` with the one-time token `spawn`
handed it, which happens after the socket is listening; a keeper that
exits or stays silent for 20 seconds raises `SpawnError` with the tail
of `keeper.out`.

| OS | What would kill a keeper with the hub | What prevents it |
|---|---|---|
| macOS | launchd kills the processes left in the job's process group when the job exits | the keeper has its own process group, and the hub's LaunchAgent sets `AbandonProcessGroup` |
| Linux | systemd stops the hub's unit with `KillMode=control-group`: every process in the unit's cgroup dies, detached or not | `systemd-run --user --scope --unit=swe-keeper-<slug> --collect` puts the keeper in a cgroup of its own |

- **Linux.** `--scope` makes systemd-run exec the keeper itself, so the
  keeper keeps the hub's environment and the agent's keys never ride on
  a command line. Where there is no user manager (`systemd.available`
  is false, or `systemd-run` fails) `spawn` falls back to a plain
  detached child, which is right for a hub started from a shell.
- **macOS.** `launchd.build_plist` now sets `AbandonProcessGroup` for
  the hub and the satellite agent. A LaunchAgent installed before this
  change gets it on the next `swe remote serve --install`;
  `launchd.keeps_children(home)` says whether the installed plist has
  it.

### The service restart check

`python3 scripts/keeper_survival_check.py [--control]` installs a
throwaway service built by `launchd.build_plist` or
`systemd.build_unit`, whose program is a stand-in hub (reconnect to
every live keeper, else spawn one). It restarts the service, requires
the new hub to find the same keeper and talk to it, stops the service,
and requires the keeper to be alive still. It touches neither the real
hub nor `~/.swe`.

Results:

| Where | Result |
|---|---|
| macOS 27.3, launchd, 2026-11-21 | pass: the keeper outlived `launchctl kickstart -k` and `launchctl bootout`, and answered `status` afterwards |
| macOS, `--control` (no `AbandonProcessGroup`) | the keeper survived as well: its own process group is already enough on this macOS. The key is kept as a second guard, as G1 specifies |
| Linux (GitHub `ubuntu-latest`, systemd user manager), CI job `keeper-survival`, 2026-11-21 | pass: the keeper ran in `swe-keeper-<slug>.scope`, outlived `systemctl --user restart` and `stop`, and answered `status` afterwards |
| Linux, `--control` (no scope unit) | the keeper died with the restart: it sat in the hub unit's cgroup. The scope unit is what keeps it alive |

Linux was not run on a developer machine; the CI job is the check.

## Memory

G1 assumed 15 to 20 MB per keeper. Measured on this Mac (arm64, macOS
27.3) for a real `python -m swe.remote.keeper` driving the fake
`claude`, idle with one client connected; the agent process is not
counted:

| Interpreter | RSS | Footprint |
|---|---|---|
| Python 3.13.16 (the one SWE.app bundles) | 33.7 MB | 24 MB |
| Python 3.12.8 | 42.6 MB | 34 MB |
| Python 3.15.2 | 35.2 MB | 26 MB |

The number did not move after 20 turns or when parked. A Python
process that has imported nothing is already 14 to 17 MB RSS here, so
15 to 20 MB was never reachable; a keeper costs about 34 MB RSS on the
shipped interpreter, 17 MB of it above the bare interpreter.

Before this milestone the same measurement was 59.2 MB (3.12): any
import of a `swe.remote` module ran `swe/remote/__init__.py`, which
imported `commands` and with it the whole hub. `cmd_remote` is now
imported on first use, which took 23 MB off every keeper, and a test
(`test_a_keeper_does_not_carry_the_hub`) keeps it off. What is left
above the keeper's own code is the Claude adapter's import of
`swe.reports.transcripts` for redaction, about 7 MB with the session
parsers it pulls in; moving `_redact_secrets` to a small module would
return that.

## Limits

- The queue and the command ids are rebuilt from the last 64 MiB of the
  log. A queue change older than that (one turn that wrote 64 MiB of
  durable events while a message waited) would be missed.
- Only the last 4096 command ids are remembered. An older id sent
  again is applied again.
- `spawn` waits by looking at `keeper.json` every 50 ms.
- A dead keeper's orphaned agent is found through the keeper's process
  group. An agent that moved itself to another group is not found, and
  neither is one whose dead keeper has not been reaped yet (its pid
  still exists as a zombie).
- A failing disk can still lose a command: a receipt written but not
  fsynced is refused under its id, yet a later keeper that finds only
  the first receipt answers a retry `duplicate` of `accepted`. It is
  never applied twice.
- A keeper that is parked never exits by itself. The hub ends it with
  `stop`.

## What G8 must know

- **Start.** `write_spec`, then `spawn`, then `connect(info,
  last_seq=...)`. Give `first_seq` the hub journal's next seq for a
  session continued in SWE. Put provider keys in `spawn(env=)`, never
  in `spec.json`.
- **At hub start.** `discover(home)`: connect to every `alive` keeper
  with the journal's last seq for that session; for every `lost` one
  mark the session's turn as cut off. Then walk the store's
  `unfinished()` commands and ask `has_command` (G6).
- **Events.** Feed `event` frames to the journal with `append_at(seq)`.
  A `gap` frame means the journal cannot be made whole from the keeper:
  serve a snapshot. `message.delta` frames have no seq.
- **A cursor ahead of the keeper.** A `welcome.last_seq` below the
  seq you asked from means this is not the log your journal followed
  (the data folder was wiped). Nothing is replayed; rebuild the
  session's journal from seq 0.
- **Replies are receipts.** Map the reply's `status` onto the store's
  receipt. `duplicate` carries `first`. A `rejected` send while
  `agent_down` carries the reason to show. `not_recorded`, `too_large`
  and `queue_full` are reasons a person should see.
- **Requests are bounded.** `KeeperClient.request` gives up on a write
  that blocks for 10 seconds (a keeper that stopped reading) and closes
  the connection; reconnect with the last seq.
- **Lost connection.** `KeeperClient` hands a `{"t":"closed"}` frame
  when the socket ends. `find(session_key)` then says whether the
  keeper is alive (reconnect with the last seq), `exited`, or lost.
- **Steer.** `mode: "steer"` reaches `adapter.steer` only when
  `Caps.steer` is on; otherwise the message is queued. The keeper has
  no harness-specific rule.
- **Old clients and input requests.** `welcome.pending` holds both
  `approval.requested` and `input.requested` events.
- **Tail.** `TailManager` must skip sessions that have a keeper folder,
  or the timeline doubles (G7).
- **Upgrades.** Compare `KeeperInfo.raw["swe_version"]` and
  `ipc_version` with the hub's and cycle a keeper when it is idle.
- **Plists.** Tell the user to reinstall the service when
  `launchd.keeps_children()` is False.
