# G4: keeper event log

Status: built (a pure addition; nothing calls it yet). The keeper
process and its IPC (G5) are its first caller; the hub on keepers (G8)
reads it through G5, or from disk with `read_log` once a keeper is gone.
The design it implements is G1 section 2, "Event log"
([G1-session-runtime-redesign.md](G1-session-runtime-redesign.md)).

| File | What it holds |
|---|---|
| [`src/swe/remote/keeper/log.py`](../../src/swe/remote/keeper/log.py) | `EventLog` (the adapter's `EventSink`), `read_log`, `Replay`, `Recovery`, `Subscription`, `SYNC_TYPES`, `log_dir_for` |
| [`src/swe/remote/keeper/__init__.py`](../../src/swe/remote/keeper/__init__.py) | the package G5 fills in |
| [`tests/test_remote_keeper_log.py`](../../tests/test_remote_keeper_log.py) | every rule below, on a fake clock with a recording fsync |

## The interface

One `EventLog` per session, opened by the keeper. The adapter emits
into it; the keeper's IPC clients subscribe to it and replay from it.

| Call | Does |
|---|---|
| `EventLog(session_key, home=, first_seq=1, ...)` | opens or creates the log, takes its lock, recovers it (`log.recovery`) |
| `emit(event)` | validates, stamps `seq`, writes, fsyncs if critical, delivers; returns the stamped event or None. Deltas: delivered, never written |
| `subscribe(callback)` | registers a live subscriber; returns `Subscription(last_seq, live, pending, open_turn)` taken under the same lock |
| `replay(after_seq, until_seq=, limit=)` | `Replay(events, oldest_seq, truncated, reason, more, skipped, marker)` |
| `tick()` | group commit: fsyncs if the oldest unsynced write is `commit_interval` old |
| `flush()` | fsyncs now |
| `close()` | flushes, records a clean shutdown, releases the lock; idempotent |
| `last_seq`, `first_seq`, `oldest_seq`, `marker`, `stats()`, `segments()` | state |
| `read_log(session_key, after_seq, home=)` | a lock-free, read-only replay from disk |

Parameters, with the spec values as defaults: `segment_bytes` (8 MiB),
`cap_bytes` (256 MiB), `commit_interval` (0.25 s), `seq_lease` (1024).
Injected for tests: `clock` (default `time.monotonic`), `fsync`
(default `full_fsync`), `boot_id`, `background` (the flusher thread;
off means the caller drives `tick`), `on_error(exc, event)`.

The rules `emit` keeps, from the G3 `EventSink` contract:

- **Thread-safe.** Any thread may emit. One lock orders every event, so
  seqs, the disk and every subscriber see one order.
- **Never raises into the adapter.** A write or fsync failure goes to
  `on_error` (default: a log warning). An invalid event is dropped. Only
  a subscriber that emits from its own callback gets a `RuntimeError`.
- **Never calls back into the adapter.** It calls subscribers only.
- **Stamps `seq`.** The adapter's `seq: 0` is replaced; the adapter's
  dict is not changed (the log stamps a copy).

## On disk

```
data/remote/keepers/<slug>/log/        0700; <slug> is protocol.key_slug
  events-000000000001.jsonl            0600, one v2 envelope per line
  events-000000004711.jsonl            named by the first seq it may hold
  meta.json                            0600
  lock                                 flock'd by the one writer
```

- A segment line is the stamped v2 envelope as compact JSON, UTF-8, and
  `\n`. A text with a lone surrogate is written as escaped ASCII. Seqs
  rise strictly through a segment and from one segment to the next.
- A segment holds the seqs from its name up to the next segment's name.
  Its first record is usually exactly its name; after a seq gap it can
  be higher, never lower. Names are zero-padded to 12 digits so a plain
  listing sorts by seq.
- `meta.json` is replaced atomically (temp file, fsync, rename, folder
  fsync): `{v: 1, first_seq, reserved, boot, truncated}`. `first_seq` is
  where the log began, `reserved` the seq lease, `boot` the boot id that
  wrote the lease, `truncated` the `log.truncated` marker or null.

## Durability

| What | When it reaches stable storage |
|---|---|
| any durable event | written to the OS (`os.write`, no user-space buffer) before it is delivered |
| `SYNC_TYPES`: `command.receipt`, `approval.requested`, `approval.resolved`, `input.requested`, `turn.started`, `turn.ended`, `message.completed`, `checkpoint.created`, `checkpoint.restored` | fsynced before any subscriber sees it |
| everything else durable | fsynced within `commit_interval` of its write (group commit) |
| `message.delta` | never |
| a full segment | fsynced when it is sealed, before the next one is created |
| a new or removed segment, `meta.json` | the folder is fsynced |

- Writing before delivery means a keeper crash (the process dies, the
  machine does not) loses nothing a subscriber saw: the page cache
  outlives the process, and a replay agrees with the live stream.
- `full_fsync` uses `F_FULLFSYNC` where it exists (macOS, where a plain
  fsync leaves data in the drive cache) and falls back to `fsync`.
- Group commit has two triggers: an emit that finds the oldest unsynced
  write `commit_interval` old syncs inline, and the flusher thread
  (`background=True`) sleeps on a condition until that deadline. It
  sleeps without waking while nothing is unsynced. Tests turn it off and
  call `tick()` after moving a `FakeClock`.
- fsync runs under the log's lock, so a critical event briefly blocks
  other emitters. That is what makes "a subscriber saw it" imply "it is
  on disk".

## seq is never reused

A power loss can drop group-committed events that were already
delivered, perhaps to a hub on another machine. Handing their seqs out
again would make the hub's idempotent `append_at(seq)` drop new events
as duplicates. So seqs are leased:

1. Before any event with a seq past `reserved` is written, `reserved`
   moves to `seq - 1 + seq_lease` in `meta.json` (durably).
2. `close()` shrinks `reserved` back to the last seq: a clean shutdown.
3. On open, the next seq is `last_seq_on_disk + 1`, unless `reserved` is
   past that and `meta.json`'s boot id differs from this boot's (or
   either is unknown). Then it is `reserved + 1`, and
   `recovery.seq_gap` says how many seqs were skipped.

A keeper killed on the same boot lost nothing it wrote, so it continues
at `last_seq + 1` with no gap. Clients already tolerate gaps
([G3-adapter-protocol.md](G3-adapter-protocol.md), "Gaps in seq").
The boot id is `/proc/sys/kernel/random/boot_id` on Linux and
`kern.bootsessionuuid` on macOS.

## Recovery on open

1. Take the flock on `lock`; `LogLocked` if another writer holds it.
2. Read `meta.json` (a missing, corrupt or other-version file counts as
   absent: `first_seq` then comes from the oldest segment, and the lease
   is lost, so no gap can be applied).
3. Unlink segments named below the marker's `before_seq`: an eviction
   that a crash interrupted after writing the marker.
4. Scan the newest segment forward. Everything after the last valid,
   newline-terminated record is the torn tail: it is cut off with
   `ftruncate` and fsynced (`recovery.torn_bytes`). A segment left
   empty is unlinked and the one before it is scanned the same way.
5. Pick the next seq (above), apply the cap, write `meta.json` with a
   fresh lease, fsync the newest segment once (the last run's writes may
   still be only in the page cache), and open it for append.
6. Read backward from the tail to the newest turn boundary
   (`turn.ended`, `turn.started`, or a `session.state` of idle,
   agent_down, parked or closed) to find `recovery.open_turn` and
   `recovery.pending`.

A record is valid when it parses as a JSON object with a string `type`,
an int `seq` of at least 1, at least the segment's name, and above the
record before it. A complete JSON line without its `\n` is torn too:
`\n` is written in the same `os.write` as the line.

**Corruption in the middle.** An invalid line with a valid record after
it is not a torn write: one writer, appends only, and a torn tail is
always cut before the next append. It is external damage, so it is left
in place as evidence and skipped by every reader (`recovery.skipped_lines`
counts it in the newest segment, `Replay.skipped` in what a replay
read). Numbering goes on from the last valid record.

**A failed write.** The segment is cut back to its size before the
write and the seq is not used (the event was never delivered, so the
seq is free). If the cut fails too, the next append first writes a lone
`\n`, so the partial bytes become one corrupt line instead of being
glued to the next record.

## The cap and the `log.truncated` marker

When a segment fills, the log rotates. It then drops the oldest sealed
segments until the sealed ones plus one full active segment fit under
`cap_bytes`, so the total stays under the cap (one event larger than a
segment gets a segment of its own and may exceed it). Before any file
is unlinked, the marker goes to `meta.json`:

```json
{"type": "log.truncated", "before_seq": 4711, "ts": 1760000000.0,
 "segments": 3, "bytes": 25165824}
```

`before_seq` is the first seq still kept; `segments` and `bytes` add up
over the log's life. The marker is a record the log owns, not a v2
event: it never enters the event stream, `events.validate` does not know
it, and `protocol_compat.to_v1` never sees it. A reader meets it as
`Replay.marker`.

## Replay

`replay(after_seq)` returns the events with `after_seq < seq <=
until_seq`, oldest first. `until_seq` defaults to the last seq when the
call began, so a replay is a fixed prefix even while emits continue.
The file handles are opened under the lock and read outside it, so a
long replay never stalls the adapter, and a segment the cap drops
mid-read stays readable through its open handle.

| Field | Meaning |
|---|---|
| `oldest_seq` | the first seq the log can still return: `max(first_seq, marker.before_seq)` |
| `truncated` | the caller asked for history before `oldest_seq` (`after_seq + 1 < oldest_seq`); the events from `oldest_seq` on are still returned |
| `reason` | `evicted` (the cap dropped it; see `marker`) or `before_start` (it predates the log: a session continued from the hub journal) |
| `more` | `limit` cut the read; page on with `after_seq` = the last event's seq |
| `skipped` | corrupt lines passed over |

Seqs inside a crash gap are not truncation: nothing was ever kept
there, and a replay from inside the gap simply starts at the next seq.

`read_log` is the same replay without the writer: no lock, no repair.
It skips a torn tail rather than cutting it, ignores segments below the
marker, and treats a segment that vanishes between its listing and its
open (dropped by a live writer's cap) as evicted.

## Validation

Every event goes through `events.validate` first. One it rejects is
dropped: not written, not delivered, counted in `stats()["rejected"]`,
and logged. The first rejected event of each type is also recorded as a
durable `error` event (`code: invalid_event`, the problems in
`message`), so the timeline shows something went missing without one
adapter bug flooding it. Rejecting rather than logging as is keeps
every record on disk a valid v2 envelope for every reader. An adapter's
own tests should keep using `ListSink`, which raises on the first bad
event.

## First seq

`first_seq` is where a new log starts: the hub journal's next seq for a
session "Continued in SWE" (G1 section 6), so the keeper's events follow
the journal's without overlap. On an existing log it can only move the
next seq forward, never back; the log's own `first_seq` stays as
recorded. A client asking such a log for anything before `first_seq`
gets `truncated` with `reason: before_start` and reads that history from
the hub journal.

## One writer

The log takes an exclusive, non-blocking `flock` on `log/lock` when it
opens, and a second `EventLog` on the same folder, in this process or
another, raises `LogLocked`. G5's keeper flock (on `keeper.json`)
already guarantees one keeper per session; the log's own lock also
covers a stale keeper, a test or a tool that opens the log directly.
Closing the log (or the process dying) drops the lock. Readers never
lock: `read_log` and `replay` read only complete lines.

## What G5 must know

- **Handshake without a gap.** Register the client first:
  `sub = log.subscribe(queue_put)` buffers every event after
  `sub.last_seq`. Send `last_seq`, `sub.pending` and `sub.live` (the
  partial messages: `id`, `kind`, `text`, `turn_id?`, `done?`), then
  `log.replay(client_seq, until_seq=sub.last_seq)` page by page, then
  drain the queue. If `replay` says `truncated`, send the client to a
  snapshot instead.
- **Subscriber callbacks run under the log's lock** on the emitting
  thread (usually the adapter's reader). Enqueue and return: no I/O, no
  blocking, no `emit` or `subscribe` (those raise). Overflow handling
  is the subscriber's: a callback that raises is dropped. Events are
  shared dicts; treat them as read-only.
- **Live text and pending requests** come from the log's own tracking of
  the stream (`message.delta` text by id, cleared by its completion, a
  `tool.started` with that call id, `turn.ended` or a session at rest).
  Pending requests and the open turn are rebuilt on open
  (`log.recovery`); live text is not (deltas are never on disk).
- **After a keeper crash** the agent died with it, so
  `log.recovery.open_turn` and `log.recovery.pending` are stale. The new
  keeper should emit `approval.resolved` (`source: agent_exit`) for each
  pending request and `turn.ended` (`keeper_lost`) for the open turn
  before it starts the agent.
- **Close order.** Close the adapter, then the log: an emit after
  `close()` is dropped (`stats()["dropped"]`).
- **Errors.** Pass `on_error` to hear about a full disk or an fsync
  failure; the default only logs. The log keeps going after one.
- **Cost.** One flusher thread per log, asleep while nothing is
  unsynced. One fsync per critical event and per group commit, plus two
  per lease renewal (every 1024 events) and per rotation.
- **Not here:** the keeper process, the socket, `keeper.json`,
  discovery and the IPC framing of `Replay` and the marker are G5's.
