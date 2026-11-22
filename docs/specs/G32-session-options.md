# G32: a running session's options (model, effort, plan mode)

Status: built (hub, keeper, adapters, client core). No UI: the picker
is its own milestone after G13 (see "What the picker UI needs").

A session view can now see, for a session a keeper runs, the options
its agent lets it change while it runs (`model`, `effort`,
`permission_mode`, `plan_mode`), the value of each, and, where the
agent lists them, the values it can take. It changes the model, the
effort and plan mode with the keeper's existing `set_option` command;
the permission mode is shown, never changed from a page (Limits). Builds on G3
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
The permission mode is a value only, never listed and never changed
from a page (Limits). A cell marked *(synthetic)* is proven by a fake
or a hand-written input only, not by a recording; a cell with a fixture
name is recorded; `unverified` means nothing proves it.

| Adapter | Option | Listed | Value from | Sent | A turn in progress | A refusal |
|---|---|---|---|---|---|---|
| claude_stream (2.1.328, mid-turn 2.1.330) | `model` | yes: `initialize` answer `models[]` (`hello`, every recording). The recordings keep `value` only (the recorder cuts each model to it), so whether these versions send `displayName` is not known; the reader of `displayName` and `description` is proven *(synthetic)* | the CLI's `init` message and the `set_model` answer (resolved ids), shown as the listed id that was asked for, `reported` keeping the CLI's (`set_model`); a model the CLI names by itself (a fallback) shows as it names it *(synthetic)* | at once, `set_model` | `reply_finishes`: the reply in progress ran to its end on the old model, the next turn on the new one (`set_model_mid`) | the CLI's control error: `command.result` `ok: false`, note `claude refused set_model: <its error>` *(synthetic)* |
| | `permission_mode` | no; a value only | `current_permission_mode`, `init`, `system status`, the `set_permission_mode` answer (`plan_mode`) | not from a page | `reply_finishes` (`plan_mode_mid`) | as above *(synthetic)* |
| | `plan_mode` | (a flag) | `permission_mode` is `plan`; the CLI leaving it after an approved plan (`plan_approve`) | at once, `set_permission_mode plan`; off goes back to the mode from before plan, or `default` | `reply_finishes`: answered at once, the reply ran on to all 150 lines (`plan_mode_mid`) | as above *(synthetic)* |
| | `effort` | | not offered: a spawn flag only, no control request (G7) | | | `unsupported` receipt |
| acp (qwen-code 0.28.0) | `model` | yes: `session/new` `models.availableModels` (`modelId`, `name`, `description`) (`model`) | `currentModelId`, then each `session/set_model` the agent took (`model`) | at once | `unverified`: taken mid-prompt and the reply went on (`model_mid`), but the recording's agent lists one model, so no switch could show | an unlisted value: `bad_request` from the adapter before anything is sent *(synthetic)*; the agent's own JSON-RPC error for one sent anyway (`model`) |
| | `permission_mode` | no; a value only (the agent's list holds `yolo` and `auto`) | `currentModeId`, each `session/set_mode` taken (`plan_mode`); `current_mode_update` *(synthetic)* | not from a page | `reply_finishes`: taken mid-prompt, the reply ran on (939 chunks after) and ended `end_turn` (`plan_mode_mid`) | an unknown mode: the agent's error (`plan_mode`) |
| | `plan_mode` | (a flag) | the mode is `plan` (`plan_mode`) | at once, `session/set_mode plan`; off goes back to the mode from before plan, or `default` when the agent lists it; with neither, off is refused (`refused`) and the mode left alone, never the first mode listed (*(synthetic)*: a fake whose list starts with `yolo`) | `reply_finishes` (`plan_mode_mid`) | as above |
| | `effort` | | not offered: no wire form | | | `unsupported` |
| codex_appserver (0.170.0; list 0.171.0) | `model` | yes: `model/list`, asked after the start (`model`, `displayName`; one page) (`model_list`) | the `thread/start` answer's `model`, then each value `set_option` kept: the model the next turn uses (`options`) | with the next `turn/start` | `next_turn`: nothing is sent until then, and the server keeps it for later turns (`options`) | none when set; a model the provider lacks fails the next turn (G16 `error`) |
| | `effort` | no (`model/list` names efforts per model; only ids and names were kept) | the answer's `reasoningEffort`, then each kept value (`options`) | next `turn/start` | `next_turn` | as above |
| | `permission_mode` | no; a value only | the answer's `approvalPolicy`, then each kept value (`options`) | not from a page | `next_turn` | another value: `bad_request` against the API's enum *(synthetic)* |
| | `plan_mode` | | not offered (experimental API only) | | | `unsupported` |
| opencode_serve 2.3.2 | `model` | yes: `GET /api/model` (`providerID`, `id`, `name`), as `provider/model` refs, asked after the start; `/api/config/providers` is 404 on 2.x (`providers`) | the session's `model` ref as `provider/model`, each `POST .../model` taken (`options`); `session.model.selected` *(synthetic)*. A `session.step.started` names the model (and agent) only while none is known: a step can still run on the value from before a change *(synthetic)*. A model the server takes but cannot run shows as the value until the next turn fails (`error`) | at once | `reply_finishes`: the step in progress ran on the old model, the next turn on the new (`model_mid`) | a name with no provider: `bad_request` *(synthetic)*; any other is taken and fails the next turn (`error`) |
| | `effort` | no (the list's variants were not kept) | the model ref's `variant` (`options`) | at once, with the model | `reply_finishes`: the same request as a model change | no model yet: `bad_request` *(synthetic)* |
| | `plan_mode` | (a flag) | the session's agent is `plan`, as each `POST .../agent` and `session.agent.selected` after it say (`plan_mode`, `plan_mode_mid`); the server switching agent by itself *(synthetic)* | at once, `POST .../agent plan`; off goes back to the agent from before plan, or `build` | `reply_finishes`: the step in progress ran to its end as `build`, then the server ran one more step, as `plan`, in the same execution (`plan_mode_mid`) | the server's HTTP error *(synthetic)* |
| | `permission_mode` | | not offered: rules, not a mode (G16) | | | `unsupported` |

Every adapter emits `session.options` after `session.caps` and
`session.state` at start, and again whenever a value changes: a change
we made, or one the agent made by itself.

### A change during a turn

`mid_turn` on each option is what a client can say about a change made
while a reply is being written. Each value comes from a recording:

- `next_turn` (Codex): the adapter sends the change only with the next
  `turn/start`, so the whole turn in progress runs on what it started
  with.
- `reply_finishes` (Claude, ACP's modes, opencode): the change is sent
  at once and taken; the reply being written is neither cut nor
  switched and runs to its end. The change takes hold after it: the
  next turn in every recording, and on opencode a plan switch also ran
  one more step, as `plan`, inside the same turn.
- `unverified` (ACP's model): nothing recorded shows a switch.

The keeper does not hold or refuse a mid-turn change.

The G32 recordings, each one short counting prompt in a throwaway
folder with a throwaway config home and no tools used:

| Recording | CLI | What it shows |
|---|---|---|
| `claude_stream/2.1.330/set_model_mid` | Claude Code 2.1.330, haiku | `set_model sonnet` after the first delta: answered at once, the reply ran to all 150 lines on haiku (`end_turn`), the next turn on sonnet |
| `claude_stream/2.1.330/plan_mode_mid` | the same | `set_permission_mode plan` mid-reply: answered at once, the reply ran to all 150 lines |
| `acp/qwen-code-0.28.0/model_mid`, `plan_mode_mid` | qwen 0.28.0, gpt-4o-mini | both taken mid-prompt; 904 and 939 chunks came after the change; `end_turn` |
| `opencode_serve/2.3.2/model_mid`, `plan_mode_mid` | opencode 2.3.2, gpt-5.4-nano | 204 at once; the step finished; the next turn ran on gpt-5.4-mini; the plan switch ran one more step as `plan` in the same execution |
| `codex_appserver/0.171.0/model_list` | codex 0.171.0 | 11 models, ids and display names |
| `opencode_serve/2.3.2/providers` | opencode 2.3.2 | `/api/config/providers` 404, `/api/provider` (2 providers), `/api/model` (88 models); ids and names only |

The recorders cut each list down to ids and names before writing, so
no base URL, key, cost or limit is committed.

## Steer (`caps.steer_effect`)

`session.caps` also says what a steer does to the turn in progress,
so the composer can label its steer control truthfully. It is
optional, set by the adapter from what its steer recordings prove
(`PROVEN.steer_effect`), never from harness.json, and dropped when
steer is off.

| Value | Means | Adapters |
|---|---|---|
| `stops_reply` | the reply being written is cut and the message answered at once, in the same turn | Claude (a running tool call is killed too and an open approval withdrawn: G7 `steer`, `steer_tool`, `steer_approval`); Codex (a running command goes on and an open approval holds the steer back: G16 `steer`, `steer_tool`, `steer_approval`) |
| `after_tool` | nothing is cut; the message lands when the current step, the reply or a running command, ends | opencode (G16 `steer`, `steer_tool`) |
| `next_turn` | the message waits for the next turn | none today |
| absent | no steer, or nothing proves what it does | ACP; Claude before an init lists `interrupt_send_now_v1` |

## The wire

### The event

`session.options` is a durable event, journaled and replayed like
`session.caps`, and bookkeeping (not a timeline item):

```json
{"v": 2, "type": "session.options", "session": "claude:…", "seq": 12, "ts": …,
 "data": {"options": {
   "model": {"value": "sonnet", "reported": "claude-sonnet-5-5", "mid_turn": "reply_finishes",
             "choices": [{"id": "default"}, {"id": "opus"}, {"id": "sonnet"}, {"id": "haiku"}]},
   "permission_mode": {"value": "default", "mid_turn": "reply_finishes"},
   "plan_mode": {"value": false, "mid_turn": "reply_finishes"}}}}
```

| Field | |
|---|---|
| `options` | one entry per option the session's caps allow; none absent means not offered |
| `value` | the value the agent has taken: a string ("" when the agent has not named one yet), or a flag for `plan_mode`. Where `mid_turn` is `next_turn` (Codex) it is the value the next turn uses: it changes as soon as `set_option` keeps it, while the turn in progress runs on the old one. It can be a value the agent took but has not run yet (an opencode model the provider lacks shows until the next turn fails) |
| `choices[]` | `{id, label?, description?}`, the agent's own list in its order; absent when it lists none |
| `reported` | the agent's own spelling of `value` when it differs |
| `mid_turn` | `next_turn`, `reply_finishes` or `unverified`, above |

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
| The agent reports a change this adapter sent, late | nothing: opencode keeps the model refs and agents it posted and the server has not reported back yet. A `session.model.selected` or `session.agent.selected` equal to one of them is that post's echo and is used up (the newest post is shown already); any other report is the server's own change (a plan approved, a fallback model) and is shown at once. Posting A, B, A ends on A whatever order the echoes come in; the list is cleared when an execution ends *(synthetic)*. An echo is lost only with an event the reader drops (a line over the size bound, or one the state cannot read): the event stream never reconnects, and when it ends so does the session. On a dropped event the list is cleared and the session is read back once (`GET .../session/<id>`, which carries `model` and `agent`); the read is shown unless a newer post overtook it *(synthetic)*. ACP needs no such rule: the agent's updates and answers share one ordered stdio stream, and the keeper sends the next `session/set_mode` only after the last one's answer, so an update the agent writes while it handles a set lands before the next set is sent. ACP has `current_mode_update` for the agent's own changes, and qwen sent none after a set; each one is shown as the agent's word. Codex has no such report: its values come from `set_option` and the thread's answers only |
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
| `changedThisTurn` | the value changed while the turn in progress runs |
| `fromNextMessage` | it changed this turn and waits for the next message (`next_turn`): a view says "from your next message". With `reply_finishes` it does not wait for a message (opencode's plan switch ran a later step of the same turn): a view says the current reply finishes first |
| `pending` | `{value, commandId, waiting}` while a change is out; `commandId` null for one behind it, and `waiting` the reason it has not gone yet once the one before it settled (reconnecting, catching up) |
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
  (`reason`): `display_only` for `permission_mode` (shown, never sent
  from a page), G11's `gate()` (`not_live`, `catching_up`, `read_only`,
  ...), `unsupported_hub` (no `session_options` in `/v1/status`, or not
  read yet), `not_kept` (the row is not `mode: keeper`), and
  `not_allowed` per option (not in the caps, or not offered). A refused
  `setOption` touches no network. A change deferred behind another
  goes through the same gates when its turn comes: it waits through
  `not_live`, `catching_up` and features not read yet (`pending.waiting`
  says why) and ends as `error` on any other.
- A `stop()` forgets changes in flight; the next start shows the hub's
  values.

## What the picker UI needs

Kai's sheet, in the shape of t3code's "Thread settings", for the
session's own agent:

| The sheet shows | From |
|---|---|
| a list of models with a check on the one in force | `model.choices`, check where `id === model.value`; when `choices` is null (a server that did not answer its list request) the value alone |
| a name and a line under each model | `label ?? id`, `description` |
| a Default badge | the entry whose id is `default` (Claude lists one); the others name no default (Codex's `model/list` has `isDefault`, but only ids and names were kept from the recording) |
| a search field | filters `choices` by id and label on the page; nothing on the wire |
| effort | `effort` when offered (Codex, opencode), value only |
| plan mode | `plan_mode` as a switch |
| a spinner on the row being changed | `pending.value` (and `pending.commandId` null: "next"; `pending.waiting` the reason it waits) |
| the permission mode | `permission_mode.value`, read-only (`reason: display_only`) |
| an error line under the row | `error.message` |
| disabled rows and why | `canChange`, `message` |
| what a change does mid-turn | when `turnOpen`: `next_turn` "applies to the next turn"; `reply_finishes` "the current reply finishes first"; `unverified` "sent now; its effect on the current reply is not known"; after it, `fromNextMessage` "from your next message", or for `reply_finishes` with `changedThisTurn` "after the current reply" |
| the steer control's label | `caps.steer_effect` (see "Steer") |

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

- **The permission mode from any signed-in device.** The page never
  names a permission mode, and no session offers its modes as choices.
  Plan mode off returns to the mode from before plan, or to the
  fallback the table gives per adapter (Claude `default`, ACP `default`
  when listed, opencode the `build` agent). The hub route
  is unchanged from G8: any device with a valid token can `POST
  .../command {op: "set_option", name: "permission_mode", value}` and
  switch a running session with no check and no confirmation, and the
  keeper keeps the value for every later start of the agent
  (`spec.json`). Per adapter: Claude takes any mode the CLI accepts
  (`check_option` wants one short token), its most permissive one
  included; ACP takes any mode the agent lists (qwen lists `yolo` and
  `auto`); Codex takes `untrusted`, `on-request` or `never` (no
  approvals at all); opencode has no permission mode. Whether to
  restrict this on the hub or ask for a confirmation is G14's question.
- Each list is asked once per agent start, after the start (Codex's
  first page only), and arrives as one more `session.options`. A
  refused or late list leaves the value alone, and the keeper keeps the
  list the option had before (a restart, or a list that never comes,
  does not blank the picker). A model the server adds later shows after
  the next start.
- opencode's list is every model of every provider the server knows
  (88 in the recording, about 5 KB per `session.options`).
- Efforts are listed nowhere: the lists' per-model efforts and variants
  were not kept (only ids and names may be committed from a provider's
  answer), so `effort` shows its value only.
- ACP's model change mid-turn stays `unverified` until an agent that
  lists two models is recorded.
- `reply_finishes` is proven for a counting reply with no tool calls; a
  turn that goes on to further steps may take the change at its next
  step (opencode's plan switch did).
- Claude's model is the session's setting as `init` and `set_model`
  report it, not the model of each reply: in plan mode the CLI answered
  with another model than the session's (G7), and that is not shown.
- Claude names a model only in its `init`, which comes with the first
  turn. Before that, a session started with no `--model` shows
  `default`, and one started with `--model x` shows `x`.
- The value shown while parked is what the next start will use, before
  the agent has said what it made of it (an ACP agent that does not
  list the model does not take it, and its start says so).
- `changedThisTurn` and `fromNextMessage` are known from changes seen since the last snapshot:
  a snapshot taken mid-turn does not say when a value changed.
- A satellite session's options come from the copied journal only: if
  the hub's ring dropped the last `session.options` (4000 events), the
  snapshot has none until the agent next starts.
- The selector's `pending` lives in the page: a reload in the middle of
  a change shows the hub's value and no spinner.
- opencode: a model the server switches to by itself with no
  `session.model.selected` report, seen only in a step's start, is not
  shown (a step's start names the model only while none is known).
- The page bundle still exports `fetchTransport` and `sessionPath`,
  the one remaining way for page code to post a command itself. The
  view changes options only through `setOption`, and a test
  (`core/options.test.ts`) fails if a file under `src/composer/`,
  `src/session/` or `src/ui/` names the command route.

## Interface changes to earlier milestones

- G3: `Caps.steer_effect` (and `base.STEER_EFFECTS`), set in each
  adapter's `PROVEN`; `session.caps` carries it when known.
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
- Codex asks `model/list`, and opencode `GET /api/model`, on a thread
  of their own after the start events; the answer is one more
  `session.options`. The keeper carries an option's last list into an
  event that has none (`Keeper._carry_choices`).
- The redactor blanks a user agent's OS release and architecture and a
  Codex rollout name's time (`blank_machine`).
- A `session.row` frame with no row keeps the row held (G11's
  reducer).
- `scripts/record_protocol.py`, `record_acp.py`, `record_codex.py`,
  `record_opencode.py`: the G32 scenarios above.
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
