# G6: SQLite store and command receipts

Status: built (a pure addition; nothing calls it yet). The hub on
keepers (G8) is its first caller; satellite keepers (G15) may open one
per machine. Design context: section 2 ("Store", "Command path",
"Launch") and section 7 of
[G1-session-runtime-redesign.md](G1-session-runtime-redesign.md).

| File | What it holds |
|---|---|
| [`src/swe/remote/store.py`](../../src/swe/remote/store.py) | `Store`, the row types (`Session`, `Keeper`, `Launch`, `Command`, `Receipt`), `command_id_of`, `MIGRATIONS`, the errors |
| [`tests/test_remote_store.py`](../../tests/test_remote_store.py) | open and refusal, migrations, each table, receipts and races, holds, results, pruning, backup, concurrent writers |

## The file

`data/remote/remote.sqlite` under the swe data folder
(`store.store_path(home)`, from `paths.remote_dir_for`).

- **WAL** with `busy_timeout` 5000 ms and `synchronous = FULL`. FULL
  means a receipt that returned is on disk: a power cut cannot forget a
  command, so its retry cannot be applied a second time. Commands
  arrive at human speed, so the extra fsync per commit costs nothing
  that matters. If SQLite will not turn WAL on, the open fails.
- **Local disk only.** WAL needs every process on one host (shared
  memory in `-shm`). Never put the swe data folder on a network
  filesystem or a volume shared between machines. No code checks this.
- **Mode 0600** for the file, and so for `-wal` and `-shm` (SQLite gives
  them the database's mode). The folder is created 0700 if missing. A
  looser mode on an existing file is tightened at open. This matches
  `devices.json` and the journals beside it.
- **One writer.** Only the hub writes. Any other process may read it
  through its own SQLite connection (WAL lets readers run beside the
  writer and never block it); keep read transactions short so
  checkpoints can run. Never read a copy of the file: a commit can sit
  in `-wal` long after it returned.
- **Backups** only through `Store.backup(dest)`, which uses the SQLite
  backup API on a live connection, so it includes commits still in the
  `-wal`. It never overwrites `dest`, writes a temporary file beside it,
  switches it to a single self-contained file (rollback journal),
  checks it with `quick_check`, fsyncs and renames it into place, mode
  0600. A test proves a `-wal`-only commit is in the backup and that a
  plain file copy would have lost it.
- **No secrets.** Payloads, options and results are checked before they
  reach the disk. Options and results take only allowlisted keys; any
  payload key that names a credential (`token`, `bearer`, `secret`,
  `password`, `cookie`, `authorization`, `api_key`, ...) at any depth is
  refused with ValueError. Device ids are stored, never device
  credentials.

## Connection model

One `Store` is one connection (`check_same_thread=False`) behind an
`RLock`; the hub's threads share it. Every write runs in
`BEGIN IMMEDIATE`, which takes SQLite's write lock before the first
read, so a check-then-insert is one step even against another `Store`
on the same file (another connection or process). `busy_timeout` makes
the second writer wait rather than fail with `database is locked`, and
`IMMEDIATE` avoids the WAL case where a read transaction that later
writes fails at once with `SQLITE_BUSY` instead of waiting. Reads run
in autocommit, one statement each, under the same lock. A failed write
rolls back and releases the lock; a failed `COMMIT` is rolled back too.
Tests run eight threads over two `Store`s writing at once.

## Schema (version 1)

No foreign keys: a command for an unknown session still needs its
receipt stored, and a launch exists before its session id does for
adapters that learn it at `start`. No `CHECK` constraints on enums
either: the code validates every enum, so adding a value later needs
no table rebuild.

### `sessions`: every session SWE owns

| Column | Type | Meaning |
|---|---|---|
| `key` | TEXT PK | the hub-scoped key, `protocol.session_key(harness, id)`, with `@machine` for a satellite's |
| `harness` | TEXT | harness name (`claude`) |
| `harness_session_id` | TEXT | the harness's own session id, known up front |
| `machine` | TEXT | machine id the session runs on |
| `cwd` | TEXT | working folder |
| `state` | TEXT | `session.state` value: starting, idle, running, waiting, agent_down, parked, closed |
| `control` | TEXT | `full` or `read_only` |
| `control_reason` | TEXT | why it is read-only, for the session view's bar; set exactly when `control` is `read_only` |
| `adapter` | TEXT | adapter id driving it (`claude_stream`), null when none |
| `options` | TEXT (JSON) | launch and resume picks: `model`, `effort`, `permission_mode`, `plan_mode`, `yolo`, `provider`; strings or bools only |
| `epoch` | INTEGER | the session's event epoch (G9 catch-up); set by the caller |
| `last_seq` | INTEGER | the highest seq the hub has seen; `note_seq` only moves it forward |
| `created_at`, `updated_at` | REAL | epoch seconds |

`UNIQUE(machine, harness, harness_session_id)`: one conversation has one
key. `put_session` upserts (keeping `created_at`, `epoch`, `last_seq`)
and raises `StoreConflict` when the conversation is stored under
another key. Sessions are never pruned.

### `keepers`: the keeper holding each live session

| Column | Type | Meaning |
|---|---|---|
| `slug` | TEXT PK | the keeper's slug (its folder under `data/remote/keepers/`); G4 and G8 decide how it is derived |
| `session_key` | TEXT | the session it holds (indexed) |
| `pid` | INTEGER | process id |
| `socket_path` | TEXT | absolute path of its Unix socket |
| `ipc_version` | INTEGER | the IPC version it speaks |
| `state` | TEXT | starting, live, exited, lost |
| `note` | TEXT | last exit or loss reason |
| `started_at` | REAL | when this keeper started |
| `last_seen` | REAL | last time the hub heard from it (`touch_keeper`) |

`put_keeper` replaces the row for a slug: a new keeper for the same
session. The flock in `keeper.json` stays the truth about whether a
keeper is alive (G1 "Discovery"); this table is the hub's record.

### `launches`: every `POST /v1/sessions`

| Column | Type | Meaning |
|---|---|---|
| `launch_id` | TEXT PK | the id the 202 answers |
| `command_id` | TEXT UNIQUE | the command that asked for it, so a retried launch gets the same `launch_id` back (`launch_for_command`) |
| `session_key` | TEXT | the session it started; null until known |
| `machine`, `harness`, `cwd` | TEXT | where and what |
| `options` | TEXT (JSON) | same allowlist as `sessions.options` |
| `state` | TEXT | pending, then once: started, failed or untracked |
| `note` | TEXT | progress or failure text for `GET /v1/launches/<id>` |
| `created_at`, `settled_at` | REAL | `settled_at` is set when it leaves pending |

`settle_launch` moves a pending launch once and returns False after
that, so a late failure cannot undo a start (the rule `hub.py` keeps
today). Without a state it records progress only. There is no pending
marker and no adoption: the adapter names the session id.

### `commands`: every write a client sent, with its receipt

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | arrival order; held commands deliver in this order. Never reused |
| `command_id` | TEXT UNIQUE | the idempotency key, 1 to 64 of `[A-Za-z0-9_-]` (the old `client_id` rule) |
| `session_key` | TEXT | target session; `''` for none (a launch) |
| `machine` | TEXT | target machine; `''` when it does not matter |
| `op` | TEXT | what it does (`send`, `approval`, `interrupt`, `launch`, ...): a short lower-case name, chosen by G8 |
| `payload` | TEXT (JSON) | message text and option answers, canonical JSON (sorted keys), at most 1 MiB |
| `device` | TEXT | the sending device's id, null for local |
| `status` | TEXT | accepted, queued, rejected or held, as it stands now |
| `reason` | TEXT | why it is held or rejected |
| `result` | TEXT (JSON) | the `command.result` data (`op`, `ok`, `note?`, `degraded?`, `cancelled?`, `request_id?`, `launch_id?`); null until it finished |
| `received_at`, `updated_at` | REAL | first arrival, last change |
| `expires_at` | REAL | the hold deadline; set only while held |
| `finished_at` | REAL | when the result was stored |

Partial indexes: held commands by `(machine, id)`, open commands by
`id`, finished commands by `finished_at`.

## Receipt rules

`record_command(command_id, session=, op=, status=, payload=, machine=,
device=, reason=, hold_s=)` is one `BEGIN IMMEDIATE` transaction:

1. **First call** stores the command with the status the caller
   decided (accepted, queued, rejected, held) and answers it. A held
   command gets `expires_at = now + hold_s` (24 hours by default). A
   rejected one gets the result `{op, ok: false, note: reason}` at once,
   so it counts as finished.
2. **Same id, same command** answers `duplicate`, with `original` (the
   stored status as it stands now: a held command delivered since
   answers `accepted`), the stored `reason`, and the stored `result` if
   it finished. Nothing changes. Two connections racing on one id store
   it once: one caller gets the first answer, the other `duplicate`.
3. **Same id, different command** (another session, machine, op or
   payload) answers `rejected` with `conflict=True` and a reason, and
   stores nothing; the first command is untouched. Echoing the first
   command's receipt would tell the second sender its command ran when
   it never did. `device` is not compared: it records who sent it, not
   what it is, and a retry from a re-paired phone is still the same
   command.
4. A rejected command stays rejected: a retry with the same id answers
   `duplicate` / `rejected`. A new attempt needs a new id.

`command_id_of(body)` reads the id from a request: `command_id`, or
`client_id` from a client that predates it. Both present and different
is a ValueError.

Moves after the first call:

| From | To | Call |
|---|---|---|
| held | accepted, queued, rejected | `transition(id, to, reason=, result=)` |
| queued | accepted, rejected | `transition` |
| held | rejected (`expired`) | `expire_held()` at or after the deadline |
| accepted, queued | (finished) | `set_result(id, result)`, once |

Every move is conditional on the current status, so two threads cannot
both deliver one held command: the loser gets False. A move to
rejected stores a result (the caller's, or `{ok: false, note: reason}`).
`set_result` refuses a second result, so a replayed `command.result`
changes nothing.

### Held commands

`list_held(machine)` lists held commands still in time, in arrival
order; one past its deadline is never listed, even before
`expire_held` sweeps it. `expire_held()` rejects every held command at
or past its deadline with reason `expired` and returns them, so the hub
can send each one's `command.result`. Time comes from the clock passed
to `Store` (tests use `FakeClock`).

## Migrations

- `PRAGMA user_version` is the schema version; `PRAGMA application_id`
  is `0x53574552` ("SWER"), so another program's SQLite file at the
  path is refused rather than written.
- `MIGRATIONS` is an ordered tuple of `(version, step)`. Step N takes
  the file from N-1 to N. Steps are append-only: a shipped step is
  never edited; a schema change is a new step. `SCHEMA_VERSION` is its
  length.
- All pending steps and the `user_version` write run in one
  `BEGIN IMMEDIATE` transaction (DDL is transactional in SQLite; never
  `executescript`, which commits first). A failed step leaves the file
  at its old version with its old tables. The version is read again
  under the write lock, so two opens at once migrate once.
- **Downgrade rule.** A file with a newer `user_version` than this
  build knows raises `StoreNewer` and is left byte-identical: the
  version is read from the header bytes first (no SQLite open, so not
  even an empty `-wal` or `-shm` appears), then through a read-only
  connection (which cannot checkpoint a WAL into the file when it
  closes) to catch a bump still in another process's `-wal`. A file
  that is not this store, or not a database, raises `StoreError` and
  is left as it is too. The store never deletes or rebuilds its file:
  it is user data, not a cache.

## Retention

`prune(older_than_s=7 days)` deletes commands that finished (have a
result) more than `older_than_s` ago, settled launches, and keepers
that exited or were lost and not heard from since. It never deletes a
held command, a command with no result, a pending launch or a session.
A retry is recognised only while its row exists, so the retention may
not be shorter than the 24 hour hold (ValueError).

## What G8 must know

- **Apply only non-duplicates, after the commit.** Decide the status,
  call `record_command`, and deliver only when `receipt.duplicate` and
  `receipt.conflict` are both False. Never hold the store's lock (or a
  transaction) across keeper IPC.
- **At-most-once, then reconcile.** If the hub dies between the
  receipt and delivery, the command is stored `accepted` with no
  result and its retry answers duplicate. On start, walk
  `unfinished()` and ask each keeper whether it has the `command_id`
  in its log; deliver the ones it lacks.
- **Hub-made ids must be unique for ever.** `queue_command` numbers
  satellite commands `c1, c2, ...` from a counter that restarts with
  the hub; stored, a restarted hub's `c1` would answer duplicate. Use
  `secrets.token_hex` or a uuid.
- **`client_id` is optional on old clients.** A send with no id has no
  idempotency; mint one for the row, and the old behaviour (no dedupe)
  is kept.
- **Held delivery.** On reconnect: `expire_held()`, then for each
  `list_held(machine)` claim it with `transition(id, "accepted")` and
  deliver only if that returned True. Run `expire_held()` on a timer
  (once a minute is plenty) and send the `command.result` it returns.
- **Launch retries.** `launch_for_command(command_id)` before
  `add_launch`, so a retried `POST /v1/sessions` answers the same
  `launch_id`.
- **One hub at a time.** Two swe versions writing one store at once is
  not supported; the version check runs at open.
- **What stays elsewhere.** `devices.json`, `session_meta.json` (pins,
  titles) and the other JSON files are unchanged; nothing is migrated
  from them. The keeper log, not this store, is the session timeline.
