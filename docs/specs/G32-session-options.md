# G32: a running session's options (model, effort, plan mode)

Status: built (hub, keeper, adapters, client core). No UI: the picker
is its own milestone after G13 (see "What the picker UI needs").

A session view can now see, for a session a keeper runs, the options
its agent lets it change while it runs (`model`, `effort`,
`permission_mode`, `plan_mode`), the value in force for each, and,
where the agent lists them, the values it can take. It changes one
with the keeper's existing `set_option` command. Builds on G3
(`Caps`, `set_option`), G5 and G8 (the keeper op and its hub route),
G9 (the wire and its schema), G11 (the store) and G31 (command
lookups).

| File | What it holds |
|---|---|
| [`src/swe/remote/adapters/options.py`](../../src/swe/remote/adapters/options.py) | `choice`, `string_option`, `flag_option`, `allowed`, `OptionsView` (emits only a change) |
| [`src/swe/remote/adapters/events.py`](../../src/swe/remote/adapters/events.py) | the `session.options` event, `MID_TURN` |
| `claude_wire.py`, `acp.py`, `codex_wire.py`, `opencode_wire.py`, `core.py` | each state machine's `options()`, and the calls that keep it current |
| [`src/swe/remote/keeper/process.py`](../../src/swe/remote/keeper/process.py) | the options in the welcome and `status`, read back from the log, shown while parked, narrowed after a restart |
| [`src/swe/remote/runtime.py`](../../src/swe/remote/runtime.py), [`sync.py`](../../src/swe/remote/sync.py) | `offered` per kept session, the snapshot's `options` |
| [`src/swe/remote/protocol.py`](../../src/swe/remote/protocol.py) | the `session_options` feature |
| [`app/web-session/src/core/options.ts`](../../app/web-session/src/core/options.ts) | `describeOptions`, the selector's types |
| [`app/web-session/src/core/store.ts`](../../app/web-session/src/core/store.ts) | `setOption`, `sessionOptions` |
| [`tests/test_remote_session_options.py`](../../tests/test_remote_session_options.py), [`options.test.ts`](../../app/web-session/src/core/options.test.ts) | one test per row below, the keeper, the hub, the store |

## What each adapter offers

Read from each adapter's code and the recorded fixtures under
`tests/fixtures/protocols/`. "Listed" means the agent itself names the
values it takes; the list is passed on as it came, never a table
written here. An option the caps do not allow is not offered at all.

| Adapter | Option | Listed | Value in force from | Sent | A turn in progress | A refusal |
|---|---|---|---|---|---|---|
| claude_stream 2.1.328 | `model` | yes: `initialize` answer `models[].value` (with `displayName` and `description` where sent; 2.1.328 sends `value` only) | the CLI's `init` message and the `set_model` answer (resolved ids), shown as the listed id that was asked for (`reported` keeps the CLI's) | at once, `set_model` | not recorded: `unverified` | the CLI's control error: `command.result` `ok: false`, note `claude refused set_model: <its error>` |
| | `permission_mode` | no | `current_permission_mode`, `init`, `system status`, the `set_permission_mode` answer | at once | `unverified` | as above |
| | `plan_mode` | (a flag) | `permission_mode` is `plan`; the CLI leaving it after an approved plan is seen (`plan_approve`) | at once, `set_permission_mode plan` and back | `unverified` | as above |
| | `effort` | | not offered: a spawn flag only, no control request (G7) | | | `unsupported` receipt |
| acp (qwen-code 0.28.0) | `model` | yes: `session/new` `models.availableModels` (`modelId`, `name`, `description`) | `currentModelId`, then each `session/set_model` the agent took | at once | `unverified` | an unlisted value: `bad_request` from the adapter before anything is sent; the agent's JSON-RPC error otherwise |
| | `permission_mode` | yes: `modes.availableModes` (`id`, `name`, `description`) | `currentModeId`, `current_mode_update`, each `session/set_mode` taken | at once | `unverified` | as above |
| | `plan_mode` | (a flag) | the mode is `plan` | at once, `session/set_mode` | `unverified` | as above |
| | `effort` | | not offered: no wire form | | | `unsupported` |
| codex_appserver 0.170.0 | `model` | no: no fixture records `model/list` | the `thread/start` answer's `model`, then each value `set_option` kept | with the next `turn/start` | `next_turn`: nothing is sent until then, and the server keeps it for later turns (`options`) | none when set; a model the provider lacks fails the next turn (G16) |
| | `effort` | no | the answer's `reasoningEffort`, then each kept value | next `turn/start` | `next_turn` | as above |
| | `permission_mode` | the v2 API's `approvalPolicy` enum (`untrusted`, `on-request`, `never`): the only values `set_option` takes | the answer's `approvalPolicy`, then each kept value | next `turn/start` | `next_turn` | another value: `bad_request` |
| | `plan_mode` | | not offered (experimental API only) | | | `unsupported` |
| opencode_serve 2.3.2 | `model` | no: no fixture records the server's provider list | the session's `model` ref as `provider/model`, `session.model.selected`, each `POST .../model` taken | at once | `unverified` | a name with no provider: `bad_request`; any other is taken and fails the next turn (G16) |
| | `effort` | no | the model ref's `variant` | at once, with the model | `unverified` | no model yet: `bad_request` |
| | `plan_mode` | (a flag) | the session's agent is `plan`; `session.agent.selected` | at once, `POST .../agent` | `unverified` | the server's HTTP error |
| | `permission_mode` | | not offered: rules, not a mode (G16) | | | `unsupported` |

Every adapter emits `session.options` after `session.caps` and
`session.state` at start, and again whenever a value changes: a change
we made, or one the agent made by itself.

### A change during a turn

`mid_turn` on each option is what a client can say about a change made
while a reply is being written:

- `next_turn` (Codex): proven. The adapter sends the change only with
  the next `turn/start`, so the turn in progress runs on what it
  started with.
- `unverified` (Claude, ACP, opencode): the change is sent to the agent
  at once, and no recording covers one sent mid-turn. Whether the reply
  in progress switches, is cut, or finishes as it was is not known.
  The keeper does not hold or refuse a mid-turn change. Between turns
  every recording shows the next turn running on the new value.

The recordings that would settle the `unverified` rows, not made here
because they run the real CLIs on Kai's account:

1. claude_stream: `set_model` and `set_permission_mode` (plan, then
   back) sent while a reply streams.
2. acp (qwen-code): `session/set_model` and `session/set_mode plan`
   during an open `session/prompt`.
3. opencode_serve: `POST .../model` and `POST .../agent` during a
   prompt.
4. For lists: codex `model/list`, and opencode's provider list
   (`GET /config/providers`). Until then those adapters send the value
   only.

## The wire

### The event

`session.options` is a durable event, journaled and replayed like
`session.caps`, and bookkeeping (not a timeline item):

```json
{"v": 2, "type": "session.options", "session": "claude:…", "seq": 12, "ts": …,
 "data": {"options": {
   "model": {"value": "sonnet", "reported": "claude-sonnet-5-5", "mid_turn": "unverified",
             "choices": [{"id": "default"}, {"id": "opus"}, {"id": "sonnet"}, {"id": "haiku"}]},
   "permission_mode": {"value": "default", "mid_turn": "unverified"},
   "plan_mode": {"value": false, "mid_turn": "unverified"}}}}
```

| Field | |
|---|---|
| `options` | one entry per option the session's caps allow; none absent means not offered |
| `value` | the value in force: a string ("" when the agent has not named one yet), or a flag for `plan_mode` |
| `choices[]` | `{id, label?, description?}`, the agent's own list in its order; absent when it lists none |
| `reported` | the agent's own spelling of `value` when it differs |
| `mid_turn` | `next_turn` or `unverified`, above |

It is sent at start and on a change, never on a timer or a state
frame: the options of a long session cost one event per change.

### The snapshot and the stream

- `GET .../snapshot` (and `session.snapshot`) carries `options`, the
  same object (`{}` when nothing is offered). It is optional in the
  schema: a hub that does not list `session_options` sends none.
- For a kept session it is the runtime's copy (`_Owned.offered`, from
  the keeper's welcome and its events), so a ring that dropped the last
  event does not lose it; for any other session (a satellite keeper's
  copy) it is folded from the journal.
- `/v1/status` lists `session_options` while the session runtime's
  store is open (beside `require_keeper` and `command_lookup`).
- v1 clients get nothing new: `protocol_compat` drops the event.

### Changing one

The route is G8's:

```
POST /v1/sessions/{key}/command
  {"op": "set_option", "name": "model", "value": "sonnet", "command_id": "w-…"}
→ 200 {"ok": true, "receipt": "accepted", "command_id": "w-…"}
→ 409 command_rejected {"error": {"message": "<the keeper's reason>"}}
```

A satellite keeper's session answers 202 with the receipt, or 409
`machine_offline` when the machine sleeps (option changes are not held,
G15). The keeper logs the new `session.options` before the
`command.result`, so a client that sees the result already has the
value.

### The value stays true

| When | What says the value |
|---|---|
| A change works | the adapter's `session.options`, then the result |
| The agent changes it (Claude leaving plan mode after an approved plan, an ACP `current_mode_update`, opencode's `session.agent.selected`, a Claude `init` naming another model) | the adapter's `session.options` |
| A change while the agent is parked or down | the keeper's own `session.options` with the value the next start will use (and plan mode off after a permission mode, as `spec.json` records), then the result `applies when the agent starts`; that start's own event then says what the agent made of it |
| The agent restarts, or a new keeper resumes | the new adapter's event at start; a keeper whose agent does not start reads the last one back from its log for the welcome |
| An agent with narrower caps starts | the keeper drops what it no longer offers |
| A hub restart | the keeper's welcome |
| A satellite session | the copied journal's events, folded |

## The client core

`store.getState()` gains:

| Member | |
|---|---|
| `sessionOptions: SessionOptionsView` | what a picker reads; the same object until something it shows changes |
| `setOption(name, value)` | `set_option` with a fresh command id; `value` is a flag for `plan_mode`, else a string |

`SessionOptionsView` is `{known, turnOpen, model?, effort?,
permission_mode?, plan_mode?}`. `known` is false until the hub has
said; an option is there only when the session offers it. Each
`OptionView`:

| Field | |
|---|---|
| `value`, `reported?` | the hub's, never this page's guess |
| `choices` | the agent's list, or null |
| `midTurn` | as on the wire |
| `pending` | `{value, commandId}` while a change is out; `commandId` null for one waiting behind it |
| `error` | `{value, message}`: the last change that did not take, with the hub's reason as it said it (the 409 message, the result's `note`, a refused receipt's reason, or the network error) |
| `canChange`, `reason`, `message` | whether `setOption` would send now, and why not |

Rules:

- **No optimistic value.** `value` changes only with a
  `session.options` (or a snapshot).
- **One change per option at a time.** A `setOption` while one is out
  returns `{ok: true, commandId: null, receipt: "deferred"}`; the
  latest value asked for goes when the first settles, and any in
  between never does. So two quick changes end on the last one. If the
  latest is the value that just took, nothing more is sent.
- **Settled by the hub.** A `command.result` for the command id (ok
  clears it; not ok is `error` with its note), a refused receipt, a
  non-2xx answer, or a network error. A result the stream missed (a
  hub-made one with `seq` 0, or one a snapshot catch-up skipped) is
  looked up with `GET .../commands/{id}` after the next catch-up (G31).
- **The gates**, in order, each with a sentence for the view
  (`reason`): G11's `gate()` (`not_live`, `catching_up`, `read_only`,
  ...), `unsupported_hub` (no `session_options` in `/v1/status`, or not
  read yet), `not_kept` (the row is not `mode: keeper`), and
  `not_allowed` per option (not in the caps, or not offered). A refused
  `setOption` touches no network.
- A `stop()` forgets changes in flight; the next start shows the hub's
  values.

## What the picker UI needs

Kai's sheet, in the shape of t3code's "Thread settings", for the
session's own agent:

| The sheet shows | From |
|---|---|
| a list of models with a check on the one in force | `model.choices`, check where `id === model.value`; when `choices` is null (Codex, opencode) the value alone, with no list |
| a name and a line under each model | `label ?? id`, `description` |
| a Default badge | the entry whose id is `default` (Claude lists one); the others name no default today (Codex's `model/list` has `isDefault`, once recorded) |
| a search field | filters `choices` by id and label on the page; nothing on the wire |
| effort | `effort` when offered (Codex, opencode), value only |
| plan mode | `plan_mode` as a switch |
| a spinner on the row being changed | `pending.value` (and `pending.commandId` null: "next") |
| an error line under the row | `error.message` |
| disabled rows and why | `canChange`, `message` |
| "applies to the next turn" | `turnOpen && midTurn === "next_turn"`; for `unverified` while a turn runs, say the change is sent now and its effect on the current reply is not known |

Models of other agents are out of scope: switching agent is a new
session.

**A per-agent default.** swe stores none. What exists is
`last_models` and `recent_models` per harness on each machine row
(`catalog.launch_fields`, read from the ledger: the models each harness
ran most recently), which the launch sheets use as their default. A
"default model for Claude" setting would need a new config key (for
example `remote.default_models.<harness>`), read by the launch routes
and shown here as a badge; it is a launch setting, so it changes no
running session.

**Favourites.** swe stores none for models (`swe harness star` stars
harnesses, not models). They would need a small per-user list keyed by
harness, in config (or the hub's store if they should follow the user
across devices), with a route to read and change it; the picker would
then sort them first. `recent_models` could stand in for "recent".

## Limits

- Codex and opencode list no values: the picker shows the value only
  until a recording proves their list requests.
- Mid-turn changes on Claude, ACP and opencode are `unverified`.
- Claude's model is the session's setting as `init` and `set_model`
  report it, not the model of each reply: in plan mode the CLI answered
  with another model than the session's (G7), and that is not shown.
- Claude names a model only in its `init`, which comes with the first
  turn. Before that, a session started with no `--model` shows
  `default`, and one started with `--model x` shows `x`.
- The value shown while parked is what the next start will use, before
  the agent has said what it made of it (an ACP agent that does not
  list the model does not take it, and its start says so).
- A satellite session's options come from the copied journal only: if
  the hub's ring dropped the last `session.options` (4000 events), the
  snapshot has none until the agent next starts.
- The selector's `pending` lives in the page: a reload in the middle of
  a change shows the hub's value and no spinner.
- `permission_mode` values are the harness's own; only ACP and Codex
  list them.

## Interface changes to earlier milestones

- G3: a new event type `session.options`; every adapter emits it at
  start, after `session.caps` and `session.state` (the start triple),
  so fixture goldens gained it (`record_protocol.py golden`, only
  insertions). `FakeAdapter` takes `choices` and `refuse_options`, and
  `agent_sets` drives the agent's own change.
- G5: the welcome and `status` carry `options`.
- G9: the snapshot's optional `options`; `session.options` is a
  bookkeeping type; `/v1/status` lists `session_options`.
- G11: `SessionState.options`, `sessionOptions` and `setOption` in the
  store.
- `tests/fakes/fake_codex.py` answers `thread/start` with the model and
  policy it was asked for, as the real server does.

## Verification

Run on 2026-11-22, macOS 27.3.

| Check | Result |
|---|---|
| `tests.test_remote_session_options` | pass |
| Every `tests/test_remote*.py` against a throwaway HOME | pass |
| Full suite against a throwaway HOME | see the PR |
| `pnpm typecheck`, `pnpm lint:tokens`, `pnpm test`, `pnpm build` in `app/web-session` | pass; the built files under `src/swe/webapp/static/session/` are the build's |
| Each guard removed in turn (28 of them: the caps filter, change-only, the parked event, the read-back, the narrowing, the alias map, each state machine's sync, the runtime's copy, the feature, each client gate, the coalescing, the command-id match, the lookup, the stale-seq check, the stable object) | each fails a test |
