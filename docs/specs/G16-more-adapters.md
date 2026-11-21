# G16: Codex, opencode and ACP adapters on keepers

Status: the Codex adapter is built (PR 1 of 3). The opencode and ACP
adapters follow in their own PRs and add their sections here. The
milestone's entry in
[milestones.yaml](../roadmap/milestones.yaml) is marked done by the
third.

| Adapter id | Harness | Status | Fixtures |
|---|---|---|---|
| `codex_appserver` | Codex | built | 12 recordings of `codex` 0.170.0 |
| `opencode_serve` | opencode | not built yet | |
| `acp` | Gemini, Qwen, Kimi and the other ACP harnesses | not built yet | |

Read [G3-adapter-protocol.md](G3-adapter-protocol.md) for the contract
and [G7-claude-stream-adapter.md](G7-claude-stream-adapter.md) for the
worked example these follow.

## Shared by the three adapters

| File | What it holds |
|---|---|
| [`src/swe/remote/adapters/core.py`](../../src/swe/remote/adapters/core.py) | `SessionCore` (the open turn, the open requests, redacted live text, the two exit sequences of the contract), `LineWriter` (the one thread that writes a child's stdin), `Ack`, `Fed` |
| [`scripts/record_protocol.py`](../../scripts/record_protocol.py) | the recorder's entry point; `--adapter=` picks the module that holds an adapter's scenarios |

`SessionCore` is what `claude_wire.StreamState` does for Claude, with
the Claude-only parts left out. A wire module subclasses it and adds
only what its harness's messages mean. Redaction is
`claude_wire.redact` and `claude_wire.stable_cut`, imported as they
are: one rule for what a secret looks like, whichever harness printed
it. Every string that reaches an event goes through it, live text
included (a streamed message sends only the prefix whose redaction can
no longer change, as in G7).

Turn ids are `t-<run>-<n>` with 8 random hex digits per start of the
adapter, and approval request ids carry the same run prefix, so one
session's log never repeats an id across starts.

## Codex (`codex_appserver`)

| File | What it holds |
|---|---|
| [`src/swe/remote/adapters/codex_wire.py`](../../src/swe/remote/adapters/codex_wire.py) | the only code that knows the wire: builders for our lines, `decode`, `ThreadState` (server lines to v2 events), `replay` |
| [`src/swe/remote/adapters/codex_appserver.py`](../../src/swe/remote/adapters/codex_appserver.py) | `CodexAppServerAdapter`: the process, the reader threads, the locks and the bounded waits |
| [`scripts/record_codex.py`](../../scripts/record_codex.py) | the recorder's Codex scenarios |
| [`tests/fixtures/protocols/codex_appserver/0.170.0/`](../../tests/fixtures/protocols/codex_appserver/0.170.0/) | one `<scenario>.jsonl` per scenario and its `<scenario>.events.json` |
| [`tests/fakes/fake_codex.py`](../../tests/fakes/fake_codex.py) | a stdlib fake app-server speaking the recorded protocol |
| [`tests/test_remote_adapters.py`](../../tests/test_remote_adapters.py) | from the "G16" banner: fixture replay, wire unit tests, the adapter over the fake |

### The proven spawn line

```
codex app-server
```

with nothing appended: the session is chosen on the wire. The protocol
is JSON-RPC 2.0, one JSON object per line on stdio. The handshake:

1. `initialize {clientInfo: {name: "swe", title: "SWE", version}}`. Its
   answer's `userAgent` (`swe/0.170.0 (Mac OS ...)`) carries the CLI
   version, which `start` checks against `min_version`.
2. the `initialized` notification.
3. one of `thread/start {cwd, model?, approvalPolicy?}`,
   `thread/resume {threadId, excludeTurns: true, ...}` or
   `thread/fork {threadId, excludeTurns: true, ...}`. The answer's
   `thread` names the thread id, the rollout file (`path`) and
   `cliVersion`.

The server exits 0 when its stdin closes (every recording).

The default floor is `0.170.0`, the only version recorded;
`capabilities.session.min_version` in harness.json overrides it.

### Session ids

Codex assigns the thread id; a caller cannot choose it, so
`StartSpec.session_id` is not used. `start` returns the id SWE's
session list knows the thread by: the rollout file's stem
(`rollout-2026-11-21T11-46-48-<thread uuid>`, see
`sessions.parsers.codex_thread_id`). `resume_id` takes that stem or a
bare thread uuid; the wire gets the thread uuid, the first uuid in it.

`thread/revert` writes the shortened history to a new rollout file
(`rollout-<time>-<thread uuid>_<rollout uuid>.jsonl`; the old file
stays), and SWE's session list follows the newest rollout of a thread.
So after `rollback` the adapter emits `session.state` idle with the new
`harness_session_id`, and `transcript_position` follows the new file.

### Mapping, wire to v2

What we write:

| SessionAdapter call | Wire line |
|---|---|
| `start` | spawn, `initialize`, `initialized`, then `thread/start`, `thread/resume` or `thread/fork` |
| `send_turn` | `turn/start {threadId, clientUserMessageId, input, model?, effort?, approvalPolicy?}`. `input` is one text item, then a `localImage` item per image attachment; any other attached file is named in the text as `[Attached file: /abs/path]` |
| `steer` | `turn/steer {threadId, expectedTurnId, clientUserMessageId, input}` (see "Codex steer") |
| `interrupt` | `turn/interrupt {threadId, turnId}`; answer `{}`, then `turn/completed` with `status: interrupted`. "no active turn to interrupt" is taken as done |
| `respond` allow / always / deny | the JSON-RPC answer to the server's request: `{decision: "accept" / "acceptForSession" / "decline"}` |
| `set_option(model / effort / permission_mode)` | nothing at once: the next `turn/start` carries `model`, `effort`, `approvalPolicy`, and the server keeps them for the turns after |
| `rollback(turn_id)` | `thread/revert {threadId, beforeTurnId}` |
| `close` | stdin EOF through the writer thread, then terminate, then kill |
| (a server request nothing serves) | a JSON-RPC error answer, so the server never waits on us |

What the server prints:

| Server line | v2 events |
|---|---|
| the answer to `turn/start` | `turn.started`, `session.state` running, the user's `message.completed` (nothing if it is an error) |
| the answer to `turn/steer` | none yet; the user's `message.completed` when the message lands (below) |
| `turn/started` | `turn.started` when no turn is open (a turn the server started, or one whose answer its notifications overtook) |
| `item/started` `userMessage` | a steer landing: open streams are completed with the text so far, then the user's `message.completed` (`<turn id>:user2`) |
| `item/started` `commandExecution`, `fileChange`, `mcpToolCall`, `dynamicToolCall`, `webSearch`, `imageView`, `collabAgentToolCall` | `tool.started` (`name` `shell`, `apply_patch`, `<server>.<tool>`, ...; `call_id` the item id) |
| `item/completed` for those | `tool.completed` (`completed` ok, `failed` error, `declined` cancelled; a command's `aggregatedOutput` cut at 8000 chars; a file change's `file_edits`) |
| `item/agentMessage/delta` | `message.delta` (`id` the item id) |
| `item/reasoning/summaryTextDelta`, `item/reasoning/textDelta` | `message.delta` kind thinking |
| `item/completed` `agentMessage` | `message.completed` (an item with no text and no deltas is dropped: the real CLI sends empty commentary items) |
| `item/completed` `reasoning` | `thinking.completed` (dropped when empty, which is the usual case: the summary is hidden) |
| `item/completed` `plan`, `turn/plan/updated` | `plan.updated` |
| `item/commandExecution/requestApproval` (a request) | `approval.requested` kind command, prompt the command line; options allow, deny, and always only when `availableDecisions` lists `acceptForSession`; then `session.state` waiting |
| `item/fileChange/requestApproval` (a request) | `approval.requested` kind tool, prompt naming the files; options allow and deny |
| `item/permissions/requestApproval` (a request) | `approval.requested` kind tool; answered `{permissions, scope: turn / session}` |
| `item/tool/requestUserInput` with one question (a request) | `input.requested` (choice when it has options: ids `o1`, `o2`..., labels redacted; the label as the model wrote it is kept for the answer only); answered `{answers: {<question id>: {answers: [text]}}}` |
| `serverRequest/resolved` | `approval.resolved` `source: harness` when the request is still open |
| `thread/tokenUsage/updated` | `usage.updated` (the `last` breakdown; `context_window` from `modelContextWindow`) |
| `thread/compacted`, `item/completed` `contextCompaction` | `context.compacted` |
| `error` with `willRetry: false` | `error` (`code` from `codexErrorInfo`) |
| `turn/completed` | open streams completed, open requests retired (`turn_ended`), `turn.ended` (`completed`, `interrupted`, `failed`), `session.state` idle |
| a notification naming another thread (a sub-agent's) | nothing |
| anything else | nothing; counted in `ThreadState.unknown` |

### Proven by fixture, and unverified

Every scenario ran against the real `codex` 0.170.0 on 2026-11-21, in a
throwaway work folder with a throwaway `CODEX_HOME` and a throwaway
`HOME`, model `gpt-5.6-luna` at low effort. The throwaway `config.toml`
names one model provider that reads `OPENAI_API_KEY` from the
environment, so the owner's ChatGPT login was neither read nor copied.

| Capability | Status | Fixture |
|---|---|---|
| Handshake, streaming deltas, usage, turn end | proven | `hello` |
| Command approval: allow runs it, deny (`decline`) does not | proven | `approval` |
| File-change approval and `file_edits` | proven | `approval` |
| "Always" (`acceptForSession`) on a file change | recorded: it does not stop the next file change from asking. Not offered | `always` |
| "Always" on a command | the server did not list `acceptForSession` for any recorded command; offered only when it does. That it then holds is unverified | |
| `turn/interrupt` mid-reply | proven; the cut reply is completed with the text so far | `interrupt` |
| `turn/steer` mid-reply | proven | `steer` |
| `turn/steer` while a command runs | recorded: the command is not stopped | `steer_tool` |
| `turn/steer` while an approval is open | recorded: the steer waits for the answer | `steer_approval` |
| `thread/resume` by thread id (same rollout stem) | proven | `resume` |
| `thread/fork` (new thread id, new stem) | proven | `resume` |
| `thread/revert` (conversation only; a new rollout file) | proven | `rollback` |
| `approvalPolicy` on `turn/start`, kept for later turns | proven: 1 approval under `untrusted`, 0 under `never`, 0 on the turn after with no override | `options` |
| `model` on `turn/start`, kept for later turns | proven: a model the provider lacks fails the turn, and the next turn with no override fails the same way | `error` |
| `effort` on `turn/start` | proven read: a value the provider does not know fails the turn | `error` |
| A failed turn (`error` notification, `turn/completed` `failed`) | proven | `error` |
| `localImage` attachments; other files named in the text | proven | `attach` |
| `item/permissions/requestApproval`, `item/tool/requestUserInput` | fake only; shapes from `codex app-server generate-json-schema` | |
| Reasoning text, plan updates, MCP and web-search items, compaction | fake only; the recorded model sent empty reasoning items | |
| Plan mode (`collaborationMode`) | experimental API only (`capabilities.experimentalApi`); off | |
| Stopping one sub-agent | no request for it; off | |

Caps the adapter can ever offer (`codex_wire.PROVEN`): `steer`,
`rollback`, `fork`, options `model`, `effort`, `permission_mode`.
`plan_mode` and `stop_task` stay off whatever harness.json says. The
caps in force are harness.json's met with `PROVEN`; a server that
answers `turn/steer` with "method not found" narrows steer off with a
second `session.caps`.

`permission_mode` on Codex is `approvalPolicy`: `untrusted`,
`on-request` or `never`. `set_option` refuses any other value
(`bad_request`); a `StartSpec.permission_mode` that is none of them is
left out, so a default meant for another harness never breaks a start.
The sandbox is never set by the adapter: it stays what the user's Codex
config says.

### Codex steer

`steer(turn)` writes `turn/steer` with the server's id of the open
turn. From the three recordings:

1. **Mid-reply** (`steer`): the server accepts at once (`{turnId}`),
   abandons the message it was writing (no `item/completed` ever comes
   for it), announces our message as a `userMessage` item, and answers
   it in the same turn. One `turn/completed`.
2. **While a command runs** (`steer_tool`): the same, but the command
   is not stopped. `sleep 25 && touch late.txt` went on after the turn
   had ended (the turn ended 4.7 s after the command began; the file
   appeared, and the command's `item/completed` came 20 s after
   `turn/completed`). This differs from Claude, where a steer kills the
   running call.
3. **While an approval is open** (`steer_approval`): the server accepts
   the steer and then does nothing for as long as the approval is
   unanswered (20 s in the recording). The request is not withdrawn.
   Once it is answered, our message lands and is answered.

What a client sees, all in the one turn the steer joined:

| Order | Event | When |
|---|---|---|
| 1 | the cut reply's last `message.delta` and its `message.completed` with the text so far | when the steer lands, if a reply was streaming |
| 2 | `message.completed` user, id `<turn id>:user2` (`:user3` for a second), with `by`, `attachments` and the envelope `command_id` | when the steer lands |
| 3 | `message.delta`s and `message.completed` assistant | the answer to the steer |
| 4 | `usage.updated`, `turn.ended` `completed`, `session.state` idle | at `turn/completed` |
| 5 | `tool.completed` for a command still running, with no `turn_id` | whenever it finishes, after the turn |

A steer "lands" when the server's `userMessage` item for it arrives,
matched by the `clientUserMessageId` we gave (`swe-<rpc id>`, echoed as
the item's `clientId`); a server that echoes none gets the oldest steer
waiting. A steer the server accepted but never announced is shown just
before `turn.ended`. An open approval stays open across a steer and can
still be answered.

Refusals: no turn open, or the server's "no active turn to steer", is
`AdapterError(code="idle")` and emits nothing (the keeper sends a
normal turn instead). Any other error answer is `refused`.

### Verification

Run on 2026-11-21, macOS 27.3, Python 3.15.2 and 3.12 (the suite).

| Check | Result |
|---|---|
| The Codex tests in `tests.test_remote_adapters` (135 tests) | pass, 16 runs side by side, with no `codex` needed |
| Full suite against a throwaway HOME | pass (see the PR) |
| G1 Verification step 2, against an installed `swe` (`pip install -e .` in a fresh venv), a temp HOME, and harness.json pointing `capabilities.session.argv` at `tests/fakes/fake_codex.py`: `swe config set remote.session_view true`, `swe remote serve`, launch (a retried launch answered as a duplicate), a turn sent with a client on the stream (3 `message.delta` frames that add up to the completed text), a turn that asks a command approval, `kill -9` the hub with the approval open, restart it, the approval still in `/v1/inbox`, answer it (a second answer gets 409), the turn finishes with its tool card and result, journal seqs 1..38 equal the keeper's log, detach ends the keeper and the fake agent | pass, 23 checks |
| One manual run with the real `codex` 0.170.0 through `CodexAppServerAdapter` (model `gpt-5.6-luna`, a throwaway folder, `CODEX_HOME` and `HOME`): a command approval answered from another device (the file appears only after Allow), a steer mid-reply (11 deltas, the cut reply completed, then "steered"), an interrupt, `permission_mode` and `effort` set for the next turn, a rollback (the model no longer knows the rolled-back word), `steer` when idle (`idle`), close (exit 0), a resume in a new process (it recalls the first command), a fork (a new id) | pass |

### Contract gaps

Places the wire does not fit G3 as written; the adapter does the
closest correct thing and nothing in G3 was bent.

1. **The server assigns the session id**, so `StartSpec.session_id` is
   ignored and the hub's key for a launched session (`codex:<uuid the
   hub picked>`) is not the session's own id. See "What G20 and G22
   must know".
2. **`rollback` changes the session id** (a new rollout file). G3 says
   nothing about an id that moves; the adapter says the new one with a
   `session.state`.
3. **`rollback` only knows turns of this start.** `thread/revert` needs
   the server's turn id, which the adapter learns as each turn starts.
   A turn from before a keeper restart raises `unknown_turn`.
4. **A steer's `message.completed` is not emitted by `steer` itself**
   but when the message lands, on the reader thread: milliseconds later
   mid-reply, after the answer when an approval is open. G3 says
   `steer` "emits that user `message.completed`".
5. **A tool call can outlive its turn.** A command a steer left running
   finishes after `turn.ended`, between turns or during a later one;
   its `tool.completed` then carries no `turn_id` (never the id of a
   turn that started since). G3's ordering puts `turn.ended` last in a
   turn.
6. **`steer` means something different per harness.** On Codex it does
   not stop a running command and waits behind an open approval; on
   Claude it kills the call and withdraws the approval. One label
   ("stops the current reply") is true of both; anything stronger is
   not (G13).
7. **Options apply at the next turn, and cannot be checked when set.**
   A model the provider does not have fails that turn, and the server
   keeps it for the turns after until another is set.
8. **`permission_mode` values are the harness's own** (`untrusted`,
   `on-request`, `never` here; `default`, `acceptEdits`... on Claude).
   A caller cannot pick one without knowing the harness; `Caps` says
   only that the option exists.
9. **"Always" is the server's to offer.** It is offered only where
   `availableDecisions` lists `acceptForSession`. The server's other
   standing choice (`acceptWithExecpolicyAmendment`) writes a rule into
   the user's Codex config and is never offered.
10. **Approval and message ids are the server's**, except approval
    request ids, which are `<run>-<rpc id>`: the server numbers its
    requests from 0 in every process.
11. **`usage.updated` can come outside a turn**: a resumed thread
    reports its last usage before any turn starts.

### The pre-keeper channel

`inject.CodexAppServerInjector` (a prompt into a terminal-started
session, used until G22 and G24 remove the tmux and headless paths)
still owns its process, but builds its lines and reads the server's
through `codex_wire` (`request`, `initialize`, `plain_turn_params`,
`approval_reply`, `live_text`, `is_approval_request`...). Its behaviour
and its tests are unchanged.

### Recording again for a new CLI version

```bash
python3 scripts/record_protocol.py list
OPENAI_API_KEY=... python3 scripts/record_protocol.py record \
    --adapter=codex_appserver --scenario=hello --scenario=steer \
    --raw-dir=/some/scratch/dir          # outside the repo
python3 scripts/record_protocol.py golden   # rebuild goldens after a wire change
```

`record` reads the version from `codex --version` and writes under
`tests/fixtures/protocols/codex_appserver/<version>/`. A notification
the adapter does not read is cut to its method name before it is
written (account, remote-control, MCP and skill status carry the
machine's name and the account's plan), and a thread object to the
fields the adapter reads. Before committing, do the four checks in G7's
"Recording again". All twelve scenarios together cost a few US cents
and run in parallel in under a minute.

## What G20 and G22 must know

- **harness.json must turn the proven features on for codex.** The
  fallback row resolves the adapter with every feature off: launch,
  turns, approvals and interrupt work, and steer, rollback, fork and the
  options are refused. The entry to write:

  ```json
  "capabilities": {"session": {
      "adapter": "codex_appserver", "argv": ["codex", "app-server"],
      "min_version": "0.170.0",
      "steer": true, "plan_mode": false, "rollback": true,
      "fork": true, "stop_task": false,
      "options": ["model", "effort", "permission_mode"]
  }}
  ```

  No `fallback.SESSION_SEED` row was added: the test that pins the seed
  table lives in `tests/test_remote_hub_keeper.py`, in the hub's lane
  this wave.
- **Session keys.** The hub names a launched session before its keeper
  exists (`codex:<uuid>`), and Codex then assigns its own thread id.
  The session row still merges, because `start` returns the rollout
  stem the session list uses. But once such a session is let go
  (detach, or resumed in a terminal), the tailer lists it under
  `codex:<rollout stem>`, and the journal the keeper filled sits under
  the hub's key. Decide how these sessions are keyed (re-key at launch
  once the id is known, or an alias from the hub's key) before
  `remote.session_view` is on by default. The same holds for every
  harness that assigns its own id (opencode, ACP).
- **The session id moves after a rollback** (`session.state` with a new
  `harness_session_id`). The keeper keeps the id `start` returned; it
  still resumes correctly (same thread uuid), but anything that stores
  the id should take the newest.
- **Steer wording** (G13): see contract gap 6.
- **`steer` raises `idle` with no turn open**: send a normal turn.
- **`permission_mode` values** for a picker: `untrusted`, `on-request`,
  `never`.
- **Codex still writes its own rollout** under `$CODEX_HOME/sessions/`;
  `TailManager` must skip SWE-owned sessions or the timeline doubles
  (as for Claude).
- **A write that times out** (`write_timeout`, 10 s) raises `timeout`
  and kills the child; the keeper sees `agent_down` and restarts it.
- **G22**: when the tmux and headless paths go, delete
  `inject._AppServer`, `CodexAppServerInjector` and the "pre-keeper
  channel" section of `codex_wire.py` with them.

## Owed docs

Not edited while wave 5 PRs are open; each PR body lists what it owes.

- `CHANGELOG.md`: the Codex session adapter (behind
  `remote.session_view`).
- `FEATURES.md`: Codex sessions started and continued in SWE (beta,
  behind the setting), with `tests/test_remote_adapters.py`.
- `ARCHITECTURE.md` and `docs/backend-architecture.md`: `adapters/core.py`
  and the Codex modules in the package map.
- `docs/testing.md`: `tests/fakes/fake_codex.py` beside
  `fake_claude.py`, and `record_codex.py`.
- `docs/specs/G3-adapter-protocol.md`: "true of every id until G7 and
  G16" and the registry table.
