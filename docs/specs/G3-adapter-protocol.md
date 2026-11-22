# G3: harness adapter protocol, v2 events, compat layer

Status: built (pure additions; nothing calls it yet). The keeper (G4, G5)
and the hub on keepers (G8) are its first callers; the Claude stream
adapter (G7) and the Codex, opencode and ACP adapters (G16) are its
first implementations.

| File | What it holds |
|---|---|
| [`src/swe/remote/adapters/base.py`](../../src/swe/remote/adapters/base.py) | `SessionAdapter`, `EventSink`, `Caps`, `StartSpec`, `TurnInput`, `Answer`, `TranscriptPosition`, the errors |
| [`src/swe/remote/adapters/events.py`](../../src/swe/remote/adapters/events.py) | the v2 event model: builders, `validate`, enums, `DURABLE_TYPES` |
| [`src/swe/remote/adapters/fallback.py`](../../src/swe/remote/adapters/fallback.py) | `SESSION_FALLBACK`, the read-only floor under `capabilities.session` |
| [`src/swe/remote/adapters/registry.py`](../../src/swe/remote/adapters/registry.py) | `adapter_for` (the only reader), `register`, `create` |
| [`src/swe/remote/adapters/fake.py`](../../src/swe/remote/adapters/fake.py) | `FakeAdapter`, `FakeClock`, `ListSink`, script steps |
| [`src/swe/remote/protocol_compat.py`](../../src/swe/remote/protocol_compat.py) | `to_v1`: one v2 event as today's v1 frames |

## The interface

One `SessionAdapter` drives one session. The keeper calls it; the
adapter emits v2 events into an `EventSink` (the keeper's log).

| Method | Does | Gated by |
|---|---|---|
| `capabilities` | the `Caps` in force now | |
| `start(spec, sink)` | new, resume (`resume_id`) or fork (`fork=True`); returns the harness's session id; emits `session.caps` then `session.state` | `fork` |
| `send_turn(turn)` | starts a turn, returns its `turn_id`; `AdapterError(code="busy")` mid-turn | |
| `steer(turn)` | adds a message, with its attachments, to the turn in flight | `steer` |
| `interrupt()` | stops the turn; a no-op when idle | |
| `respond(request_id, answer)` | answers an approval or input request; `UnknownRequest` if not open | |
| `set_option(name, value)` | `model`, `effort`, `permission_mode` (in `Caps.options`) or `plan_mode` | `options`, `plan_mode` |
| `stop_task(task_id)` | stops one subagent or background task | `stop_task` |
| `rollback(turn_id)` | rewinds the conversation to before a turn (files are checkpoints, not this) | `rollback` |
| `transcript_position()` | the harness transcript's path and size, or None | |
| `close(grace_s)` | ends the session; idempotent; a later `start` raises `AdapterError(code="closed")` | |

The rules every adapter keeps:

- **Threading.** All methods are called from one thread (the keeper's
  command loop), never concurrently. Events go out from the adapter's
  own reader thread; `sink.emit` is thread-safe and never calls back in.
- **Blocking.** `start` may block on the handshake (bounded, about 60 s
  at most). Every other call returns once the harness has accepted the
  request, not when the work ends. Every wait is bounded; a timeout
  raises `AdapterError(code="timeout")`.
- **Ordering.** One session's events are emitted in one order:
  `turn.started` first and `turn.ended` last in a turn; a message's
  deltas before its `message.completed`; a request before its resolve;
  `tool.started` before that call's update and completion.
- **Agent exit.** On an exit the adapter did not cause, it emits once,
  in order: `approval.resolved` (`source: agent_exit`) per open request,
  `turn.ended` (`failed`) for an open turn, `session.state`
  (`agent_down`). After `close`: the same with `source: closed`,
  `interrupted`, and `session.state` `closed`. Nothing after either;
  later calls raise `AdapterError(code="agent_down")`.
- **Capabilities, not names.** Optional methods check `Caps` and raise
  `Unsupported(feature)`. No adapter or caller branches on a harness
  name. Caps can narrow after `start` (the harness handshake says less
  than harness.json); the adapter then emits `session.caps`.
- **Redaction.** Tool input and output, prompts and message text are
  redacted before they are emitted.
- **seq.** Adapters emit `seq: 0`; the sink stamps the real one.

Errors: `AdapterError(message, code=)` is the base; `Unsupported` carries
`feature`; `UnknownRequest` carries `request_id`.

## Event reference (v2)

Envelope: `{v: 2, type, session, seq, ts, turn_id?, command_id?, data}`.
`message.delta` is the one transient type: never logged, never replayed,
and it has no `seq`. `DURABLE_TYPES` is every other type. Optional
fields marked `?`; enum values are in `events.ENUMS`.

| Type | `data` fields |
|---|---|
| `session.state` | `state` (starting, idle, running, waiting, agent_down, parked, closed), `reason?`, `harness_session_id?` |
| `session.caps` | `caps` {steer, plan_mode, rollback, fork, stop_task, options[]} |
| `session.options` | `options` {model?, effort?, permission_mode?, plan_mode?}: each `{value, mid_turn, choices?, reported?}`, for the names the caps allow; at start and on each change (G32) |
| `turn.started` | `by?` (device), `text?`; envelope needs `turn_id` |
| `turn.ended` | `status` (completed, interrupted, failed, keeper_lost), `reason?`, `duration_s?`; envelope needs `turn_id` |
| `message.delta` | `id`, `kind` (assistant, thinking, tool_use), `text`, `seq` (this message's delta count, a positive integer from 1), `offset?`, `done?` |
| `message.completed` | `id`, `kind` (user, assistant, system), `text`, `by?`, `attachments?` |
| `thinking.completed` | `id`, `text`, `duration_s?` |
| `tool.started` | `call_id`, `name`, `summary?` (one-line card), `input?`, `task_id?` |
| `tool.updated` | `call_id`, `text?`, `summary?` |
| `tool.completed` | `call_id`, `status` (ok, error, cancelled), `name?`, `output?`, `file_edits?` [{path, kind (add, modify, delete), diff?, old_path?}] |
| `approval.requested` | `request_id`, `kind` (tool, command, question, plan), `prompt`, `options?` [{id, label, kind?, standing?}], `call_id?`, `auto?` |
| `approval.resolved` | `request_id`, `source` (user, autonomy, harness, turn_ended, agent_exit, closed, superseded, expired), `decision?` (allow, deny, always), `option_id?`, `text?`, `by?` |
| `input.requested` | `request_id`, `prompt`, `kind` (text, choice), `options?`; closed by `approval.resolved` |
| `plan.updated` | `items` [{text, status (pending, in_progress, completed)}], `text?` |
| `task.updated` | `task_id`, `kind` (subagent, background), `status` (running, completed, failed, stopped), `parent_turn_id?`, `title?`, `can_stop?` |
| `checkpoint.created` | `checkpoint_id`, `files?`, `note?` |
| `checkpoint.restored` | `checkpoint_id`, `files?`, `by?` |
| `changes.updated` | `files` [{path, kind, added?, removed?}] |
| `queue.updated` | `items` [{command_id, text, by?, mode? (queue, steer), attachments?}] |
| `command.receipt` | `status` (accepted, duplicate, queued, rejected, held), `reason?`, `op?`; envelope needs `command_id` |
| `command.result` | `op`, `ok`, `note?`, `degraded?`, `cancelled?`, `request_id?`, `launch_id?`; envelope needs `command_id` |
| `usage.updated` | `input_tokens?`, `output_tokens?`, `cache_read_tokens?`, `cache_write_tokens?`, `context_tokens?`, `context_window?`, `cost_usd?` |
| `context.compacted` | `summary?`, `tokens_before?` |
| `error` | `code`, `message`, `fatal?` |
| `notify` | `kind` (v1's `NOTIFY_KINDS`), `message`, `status?` |

Field names follow v1 where the meaning is the same (`request_id`,
`prompt`, `options`, `decision`, `source`; a delta's `id`, `kind`,
`text`, `seq`, `done`; `call_id`; `duration_s`; `op`, `ok`, `note`).
`validate` reports unknown fields, so a typo fails the adapter's tests,
and anything `json.dumps(event, allow_nan=False)` cannot encode (sets,
bytes, tuple keys, NaN, infinity, at any depth): the log and the wire
are strict JSON. The envelope `seq` may be 0 before the sink stamps it.

## Capability keys

`capabilities.session` in harness.json is the source of truth.
`SESSION_FALLBACK` answers only for a harness the registry has never
described; a registry entry wins field by field.

| Key | Type | Meaning |
|---|---|---|
| `adapter` | adapter id | which registered adapter drives it |
| `argv` | list of strings | the command that starts the protocol endpoint; the adapter appends per-session arguments only. Absent: the adapter's default (ACP probes `acp.ACP_FALLBACK`) |
| `min_version` | string | the oldest CLI version the adapter accepts; checked at `start` |
| `steer`, `plan_mode`, `rollback`, `fork`, `stop_task` | bool | optional features; off unless a fixture proves them |
| `options` | list | `set_option` names besides `plan_mode`: `model`, `effort`, `permission_mode` |
| `supported` | `false` | no adapter, whatever the fallback says; `reason` (string) is shown as the read-only reason |

A field of the wrong type counts as not declared. The fallback seeds
`claude` (`claude_stream`), `codex` (`codex_appserver`), `opencode`
(`opencode_serve`) and the ACP harnesses of `acp.ACP_FALLBACK` except
claude (`acp`), every feature off.

`adapter_for(harness, registry, which=)` returns `Resolved(harness,
adapter, argv, caps, min_version, source)` or `NoAdapter(harness,
reason, adapter, source)`. It is a `NoAdapter` when the registry says
`supported: false`, nothing describes the harness, no class is
registered for the adapter id (none today: G7 and G16 registered every
built-in id), or
`argv`'s binary is not on PATH. `reason` is written for the session
view's read-only bar.

## Compatibility with v1 clients

`protocol_compat.to_v1(event, machine=, at_refs=)` maps one v2 event to at most
one v1 event, keeping its `seq` (deltas keep none):

| v2 | v1 |
|---|---|
| `message.completed` | `message.appended` kind user, assistant or system; `attachments` folded into the text exactly as `attachments.prompt_text` sends them (`@/abs/path` when the caller passes `at_refs=True`, else `[Attached file: /abs/path]`) |
| `thinking.completed` | `message.appended` kind thinking, with `duration_s` |
| `tool.started` / `tool.completed` | `message.appended` `tool_use` (tool card) / `tool_result` (`result_ref`) |
| `context.compacted` | `message.appended` kind compaction |
| `error` | `message.appended` kind error |
| `message.delta` | `message.delta` |
| `session.state` | `session.status`: running active, idle done, waiting followup, agent_down error, parked and closed done; starting dropped |
| `approval.requested` / `resolved` | the v1 events; options become menu rows `{n, label, kind?, standing?}` numbered by position |
| `command.result` | `command.result`, with `client_id` set to the command id |
| `notify` | `notify` |
| the rest | dropped |

### Gaps in seq

Dropping events leaves gaps in the seqs a v1 client sees. Every client
keeps only the highest seq as its cursor and never checks that seqs are
consecutive, so a gap is harmless: `remember` in `chat.js`,
`EventStream.resume` and `ChatModel.apply` on iOS, `HubClient` and
`SessionChatModel.apply` on the Mac. The one consecutive-number rule
("a gap stops drawing a bubble", `ReplyStreams.swift` and `chat.js`)
applies to a delta's `data.seq` within one message, which compat passes
through unchanged. Two v1 frames sharing a seq would break things (all
three clients drop a `message.appended` whose seq they have seen), which
is why compat never emits more than one frame per event.

## Adding an adapter

1. Write `src/swe/remote/adapters/<id>.py` with a class that has
   `adapter_id = "<id>"`, takes `(resolved, **kwargs)` and implements
   every `SessionAdapter` method under the contract above. Only this
   file (and a `<id>_wire.py` beside it) knows the harness's wire format.
2. Call `registry.register("<id>", TheClass)` at the end of the module
   and add `"<id>"` to `registry.BUILTIN_MODULES`.
3. Give the harness a `SESSION_FALLBACK` row in `fallback.py` with
   every feature off, or describe it only in harness.json.
4. Test it with the shared contract: mix `AdapterContract` from
   `tests/test_remote_adapter_base.py` into a `TestCase` and implement
   `make_adapter(caps=, reply=)` over a fake agent. `ListSink.wait_for`
   waits for an event without polling. Add recorded fixtures under
   `tests/fixtures/protocols/<id>/<version>/` (G7).
5. Turn a feature on in harness.json (and later in the fallback) only
   once a fixture proves it.
