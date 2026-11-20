# G7: Claude stream adapter, protocol recorder, recorded fixtures

Status: built (pure additions; nothing calls it yet). G8 wires it into
the keeper and the hub. The fixtures are real recordings of Claude Code
2.1.328 (12 scenarios), not hand-written.

| File | What it holds |
|---|---|
| [`src/swe/remote/adapters/claude_wire.py`](../../src/swe/remote/adapters/claude_wire.py) | the only code that knows the wire: the spawn line, encoders, `decode`, `Handshake`, `StreamState` (CLI lines to v2 events), `replay` |
| [`src/swe/remote/adapters/claude_stream.py`](../../src/swe/remote/adapters/claude_stream.py) | `ClaudeStreamAdapter` (`adapter_id = "claude_stream"`): the process, the reader threads, the locks and the bounded waits |
| [`scripts/record_protocol.py`](../../scripts/record_protocol.py) | runs the real CLI through scripted scenarios, redacts, writes a fixture and its golden events |
| [`tests/fixtures/protocols/claude_stream/2.1.328/`](../../tests/fixtures/protocols/claude_stream/2.1.328/) | one `<scenario>.jsonl` per scenario and its `<scenario>.events.json` |
| [`tests/fakes/fake_claude.py`](../../tests/fakes/fake_claude.py) | a stdlib fake CLI speaking the recorded protocol, for the contract suite |
| [`tests/test_remote_claude_adapter.py`](../../tests/test_remote_claude_adapter.py) | fixture replay, wire unit tests, the adapter over the fake |

The adapter registers itself (`registry.BUILTIN_MODULES`). Every
feature stays off in `SESSION_FALLBACK`; harness.json turns on the ones
listed as proven below.

## The proven spawn line

```
claude --output-format stream-json --verbose --input-format stream-json \
       --permission-prompt-tool stdio --include-partial-messages \
       --session-id=<uuid>            # new session
       --resume=<id>                  # resume (no --session-id)
       --resume=<id> --fork-session --session-id=<new uuid>   # fork
       [--model=<m>] [--effort=<e>] [--permission-mode=<mode>]
```

with `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1` in the environment and
`CLAUDECODE` removed from it. The base argv comes from
`capabilities.session.argv` (or the fallback); the adapter appends any
of the five protocol flags it lacks, then the per-session arguments, all
in `--flag=value` form. No `-p` is needed: stream-json input puts the
CLI in print mode.

- `--permission-prompt-tool stdio` routes every permission prompt to us
  as a `can_use_tool` control request (the Agent SDK passes it when the
  host has a `can_use_tool` callback).
- `--include-partial-messages` adds the `stream_event` lines live text
  comes from.
- `--session-id` lets `start` return the id before the first turn: the
  init message, the only other place the id appears, arrives with the
  first turn. A fork's init message carries the new id we passed.
- `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1` makes the CLI print
  `session_state_changed` (`running`, `requires_action`, `idle`). `idle`
  is the only reliable turn end: a steer, or a background subagent that
  wakes the session, puts two `result` lines in one running stretch.
- The fallback row's argv lacks the last two protocol flags. It was left
  as it is: the adapter supplies them, so the default is incomplete but
  not wrong.

The first stdin line is `{"type": "control_request", "request_id": R,
"request": {"subtype": "initialize", "hooks": null}}`. Its answer
carries `claude_code_version`, `current_permission_mode`, `models`,
`capabilities` and `session_state`; `start` checks `min_version`
against the version there (default floor `2.0.0`, the Agent SDK's own
`MINIMUM_CLAUDE_CODE_VERSION`).

## Mapping, wire to v2

What we write:

| SessionAdapter call | Wire line |
|---|---|
| `start` | spawn, then `control_request` `initialize` |
| `send_turn` | `{"type": "user", "message": {"role": "user", "content": "@/a.png text"}, "parent_tool_use_id": null}` (attachments as `@path` refs) |
| `respond` allow / always / deny | `control_response` success `{"behavior": "allow", "updatedInput": <input>}`, plus `updatedPermissions` for always (the CLI's `permission_suggestions` with every `destination` set to `session` and `setMode` ones dropped); `{"behavior": "deny", "message": <text>}` |
| `interrupt` | `control_request` `interrupt`; answer `{"still_queued": []}` |
| `set_option("model", m)` | `control_request` `set_model {model}` |
| `set_option("permission_mode", m)` | `control_request` `set_permission_mode {mode}`; answer `{"mode": m}` |
| `set_option("plan_mode", true/false)` | `set_permission_mode` `plan`, then back to the mode before it (or `default`) |
| `stop_task(id)` | `control_request` `stop_task {task_id}`; answer `{}` |
| `close` | stdin EOF through the writer thread (the CLI drops pending prompts and exits 0), then terminate, then kill; a writer stuck on a full pipe skips the EOF and goes straight to terminate |
| (any CLI control request but `can_use_tool`) | `control_response` error, so the CLI never waits on us |

What the CLI prints:

| CLI line | v2 events |
|---|---|
| `control_response` | none; wakes the call waiting on that request id |
| `control_request` `can_use_tool` | `approval.requested` (`kind` plan for ExitPlanMode, command for Bash, tool otherwise; `request_id` is the CLI's; options allow, always (only with suggestions), deny), then `session.state` waiting |
| `control_cancel_request` | `approval.resolved` `source: harness` |
| `system` `session_state_changed` `running` | `turn.started` + `session.state` running when no turn is open (a turn the CLI started itself) |
| `system` `session_state_changed` `requires_action` | `session.state` waiting |
| `system` `session_state_changed` `idle` | open requests retired (`turn_ended`), `turn.ended` (interrupted if we interrupted, failed if the result was an error, else completed), `session.state` idle |
| `system` `init` (once per turn) | `session.caps` when its `capabilities` array narrows the caps |
| `system` `task_started` | `task.updated` running (`subagent` for `local_agent`, else `background`; `title` from `description`, `parent_turn_id` the open turn) |
| `system` `task_progress` | `task.updated` when the description changes |
| `system` `task_updated` (`patch.status`) / `task_notification` (`status`) | `task.updated` completed / failed / stopped (`killed` is stopped); duplicates dropped |
| `system` `compact_boundary` | `context.compacted` (`tokens_before` from `compact_metadata.pre_tokens`) |
| `system` `status` | none (records `permissionMode`) |
| `stream_event` `content_block_delta` text / thinking | `message.delta` (`id` `<api message id>:<block index>`, `seq` from 1, `offset`), redacted before it goes out (see "Live text and redaction") |
| `assistant` text block | `message.completed` assistant, same id as its deltas |
| `assistant` thinking block | `thinking.completed` (`duration_s` from `thinking_duration_ms`) |
| `assistant` tool_use block | `tool.started` (`summary` like `Bash · ls`, `input` redacted key-aware, `task_id` when a subagent made it) |
| `user` tool_result block | `tool.completed` (ok, error, or cancelled when `tool_use_result.interrupted`; `output` cut at 8000 chars; `file_edits` from a Write or Edit result's `structuredPatch`) |
| `result` | `usage.updated` (tokens, `cost_usd`, `context_window`); `error` when `is_error` and we did not interrupt; ends the turn itself only for a CLI that sends no state frames |
| a subagent's lines (`parent_tool_use_id` set) | tool rows only, under the subagent's `task_id`; its text is not forwarded |
| anything else (`rate_limit_event`, `command_lifecycle`, `background_tasks_changed`, unknown types, non-JSON) | nothing; counted in `StreamState.unknown` |

The adapter emits `turn.started`, `session.state` running and the user's
`message.completed` itself in `send_turn`, once the line is written, and
`approval.resolved` (`source: user`) in `respond`.

Turn ids are `t-<run>-<n>`: `<run>` is 8 random hex digits per start of
the adapter, so one session's log, which spans many starts (a resume
after a keeper restart, Continue in SWE), never repeats a turn id or a
user message id (`<turn id>:user`). `replay` passes a fixed run id
(`00000001` for a fixture's first process) so goldens stay stable.

### Live text and redaction

Every event is redacted before it is emitted, live text included. A
secret can arrive split across deltas, so each streamed block keeps the
raw text it has received and sends only a prefix whose redaction cannot
change (`claude_wire.stable_cut`): text up to the last whitespace; the
whole line (and the one before it, since a name and its value can sit
on two lines) when it names a credential (`authorization`, `api_key`,
`token`, `secret`, `passw`, `private_key`); everything from an open
`-----BEGIN` until its `-----END ...-----` has arrived. The rest goes out
as the block's last delta (`done: true`) just before its
`message.completed` or `thinking.completed`. So offsets always equal the
length already sent, `seq` runs 1, 2, 3, no text is taken back, and the
deltas add up to the redacted completed text. Prose streams word by
word; a line that mentions a credential name streams when it ends.

Tool input and other structured values go through `claude_wire.redact`,
which is key-aware: a value under a key that names a credential
(`api_key`, `accessToken`, `client_secret`, `password`, `Authorization`,
`cookie`, at any depth) is masked whatever it holds; every other string
goes through the transcript redactor.

### Writing to the CLI

One writer thread owns the CLI's stdin. A caller queues its line and
waits at most `write_timeout` (10 s) for it to reach the pipe; the
reader thread's own replies are queued without waiting, so a CLI that
stops reading can never stall the reader. A write that misses its
deadline raises `AdapterError(code="timeout")` and kills the child (its
exit sequence follows: `agent_down`), since the pipe may hold half a
line. `close` cannot be held up by a stuck writer.

## Proven by fixture, and unverified

Every scenario ran against the real `claude` 2.1.328 on the owner's
account, model `haiku`, in a throwaway work folder, with a throwaway
`CLAUDE_CONFIG_DIR`, `--safe-mode`, `--setting-sources=project` and an
empty `--strict-mcp-config`.

| Capability | Status | Fixture |
|---|---|---|
| Handshake, streaming deltas, turn end on `idle` | proven | `hello` |
| `can_use_tool` allow and deny | proven | `approval` |
| `interrupt` mid-reply | proven | `interrupt` |
| `set_permission_mode` plan and back (`plan_mode`, `permission_mode`) | proven | `plan_mode` |
| ExitPlanMode: `can_use_tool` with `tool_name: "ExitPlanMode"`, input `{plan, planFilePath}`; deny keeps plan mode, allow leaves it (`system status permissionMode: default`) and the turn goes on | proven, `kind: plan` | `plan_mode`, `plan_approve` |
| `set_model` (`model`) | proven | `set_model` |
| Subagent task rows (`task_started`, `task_progress`, `task_updated`, `task_notification`) | proven | `subagent` |
| `stop_task` | proven | `stop_task` |
| Resume by id, fork with `--fork-session --session-id` | proven | `resume` |
| `rewind_files` restores files (with `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=true` and a user message `uuid`) | proven on the wire; no SessionAdapter method uses it | `rewind` |
| Steer (`priority: "now"`) | recorded, left off | `steer` |
| "Always allow": the CLI's suggestions sent back with every `destination` set to `session` (`setMode` ones dropped). The CLI took them, did not ask again for the same call in that session (its result says `reason_type: "rule"`), and wrote no settings file in the work folder or the throwaway `CLAUDE_CONFIG_DIR` (both checked before and after) | proven, offered when the CLI sends suggestions | `always` |
| `--effort` at start; effort mid-session | flag not recorded; no control request exists | |
| AskUserQuestion answers | not recorded; it arrives as a plain tool approval | |
| An interrupt while a `can_use_tool` is open | fake only | |
| Conversation rollback | no wire form | |

Caps the adapter can ever offer (`claude_wire.PROVEN`): `plan_mode`,
`fork`, `stop_task`, options `model` and `permission_mode`. `steer`,
`rollback` and `effort` stay off whatever harness.json says. The caps in
force are harness.json's met with `PROVEN`, then with the handshake
(`models` present for `model`, `current_permission_mode` present for
modes), then with each init message's `capabilities` array under
`REQUIRED_CAPABILITIES` (empty today: no string in 2.1.328's array names
a proven feature).

### What the steer fixture shows

A user message with `"priority": "now"` sent while the reply streams:

1. the CLI aborts the reply in flight: its `assistant` message arrives
   with `aborted: true` and the partial text (`1\n2`), and a `result`
   with `subtype: success`, `terminal_reason: aborted_streaming`;
2. without going idle, it prints a new `init` and answers the new
   message (`steered`) with its own `result`;
3. then one `session_state_changed: idle`.

So "now" interrupts the reply and runs the message at once inside the
same running stretch; it does not slip the message into a reply that
keeps going. `replay` maps it as one turn with two user messages. The
init array has `interrupt_send_now_v1`, which plausibly names this
behaviour (guessing). The owner decides whether this is the steer the
session view wants; turning it on means `PROVEN.steer`,
`REQUIRED_CAPABILITIES["steer"] = "interrupt_send_now_v1"`, and
`steer()` writing `user_message(..., priority="now")` plus the user's
`message.completed`.

## Contract gaps

Places the wire does not fit G3 as written; the adapter does the
closest correct thing and nothing in G3 was bent.

1. **Turns the CLI starts itself.** A background subagent's result wakes
   the session for a turn of its own (`session_state_changed: running`
   with no user message). The adapter opens a turn with no `by`, `text`
   or `command_id`; `send_turn` during it raises `busy`.
2. **Subagents run in the background by default.** The CLI launched the
   `general-purpose` subagent with `is_backgrounded: true` without being
   asked; the turn's first `result` came while it ran, and the session
   stayed `running` until a second, self-started reply. One v2 turn
   covers both.
3. **No conversation rollback.** `rewind_files` restores files only.
   G3's `rollback` is conversation-only and the keeper owns file
   checkpoints, so Claude's native file checkpoints have no method.
4. **Effort is start-only.** `--effort` is a spawn flag; no control
   request changes it mid-session, so `options` never includes
   `effort`. `StartSpec.model`, `effort` and `permission_mode` are
   spawn flags and are honoured whatever `Caps.options` says.
5. **Approval options are synthesized.** `can_use_tool` carries no
   option list. The adapter offers allow, deny and, when the CLI sent
   `permission_suggestions`, always. The CLI's suggestions name their
   own `destination` (`localSettings` for a Bash rule, which would write
   the project's `.claude/settings.local.json`) and can include a
   `setMode` to `acceptEdits`. "Always" sends them back scoped to the
   session and without the mode change, so a tap on a phone never
   writes a settings file or widens the session's mode (mode changes
   are `set_option`'s). A text-only answer is sent as a deny with that
   text.
6. **The init message is per turn.** Feature narrowing from its
   `capabilities` array happens at the first turn, not at `start`; the
   adapter emits a second `session.caps` then.
7. **`steer` semantics** (above): Claude's "now" aborts the reply.
8. **AskUserQuestion** comes through `can_use_tool`; answering it needs
   `updatedInput` with the answers. It maps to a tool approval, not
   `input.requested`, until a fixture shows the shape.
9. **`transcript_position`** is derived (`<CLAUDE_CONFIG_DIR or
   $HOME/.claude>/projects/<cwd with non-alphanumerics as ->/<id>.jsonl`,
   the rule seen on disk, read from the environment the child was
   started with), not reported by the CLI.
10. **Live text lags at credential names.** To keep secrets out of
   `message.delta`, a line that mentions `token`, `secret`, `api_key`
   and the like streams only when it ends (see "Live text and
   redaction").

## Recording again for a new CLI version

```bash
python3 scripts/record_protocol.py list
python3 scripts/record_protocol.py record --scenario=hello \
    --scenario=approval --raw-dir=/some/scratch/dir   # outside the repo
python3 scripts/record_protocol.py golden   # rebuild goldens after a wire change
python3 scripts/record_protocol.py scrub    # re-apply the field blanking
```

`record` reads the version from `claude --version` and writes under
`tests/fixtures/protocols/claude_stream/<version>/`. It refuses to write
a fixture that still holds the home path, the user name, the account's
email or ids, an API key, a temp-folder name, or a name from the owner's
plugin, skill or command lists. Before committing:

1. read every new fixture;
2. `gitleaks detect --no-git --source tests/fixtures/protocols`;
3. `grep -rn -i "<user name>\|@gmail\|/Users/\|/var/folders/" tests/fixtures/protocols`
   must find nothing (`git grep` misses untracked files);
4. `python3 -m unittest tests.test_remote_claude_adapter`. A golden
   mismatch on an old version's fixtures means the mapping changed; on
   the new version's it means the wire did. Fix `claude_wire.py`, run
   `golden`, and read the diff of the `.events.json` files.

Each recorded result reports at most about three US cents, and a
scenario takes under a minute. Scenarios can run in parallel (each has
its own folders).

## What G8 must know

- harness.json must turn the proven features on for claude
  (`plan_mode`, `fork`, `stop_task`, `options: ["model",
  "permission_mode"]`); with the fallback alone the adapter reports
  every feature off.
- The adapter's events start with `session.caps`, `session.state idle`;
  the first turn's init may send a second `session.caps`.
- The CLI still writes its own transcript under `~/.claude/projects/`,
  as for a terminal session; `TailManager` must skip SWE-owned sessions
  (G1, section 2) or the timeline doubles.
- Approval `request_id`s are the CLI's UUIDs; respond with them as is.
- Turn ids are `t-<8 hex>-<n>`, unique across starts of one session;
  never parse them. The constructor's `run_id` fixes the prefix (tests).
- A stdin write that times out (`write_timeout`, 10 s) raises `timeout`
  and kills the child; the keeper sees `agent_down` and restarts it.
- A plan approval's `prompt` is the whole plan in Markdown; allowing it
  takes the CLI out of plan mode by itself.
- Implicit turns (gap 1) and background subagents (gap 2) mean a turn
  can be open that no `send_turn` started.
- A user message with a `uuid` gets `command_lifecycle` frames
  (`queued`, `started`, `completed`) back (`msg_lifecycle_v1`); the
  adapter sends no uuid yet. These could back `command.receipt`.
- `system background_tasks_changed` lists the running tasks; it could
  rebuild the task list after a keeper restart.
- `close` sends stdin EOF; the CLI exited 0 on it in every recording.
