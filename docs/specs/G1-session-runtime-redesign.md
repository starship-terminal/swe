# Session runtime redesign

Status: approved design, no code changed by this document. Milestone G1 of [docs/roadmap/milestones.yaml](../roadmap/milestones.yaml). The reasoning behind each decision is in [docs/decisions/0001-session-runtime-redesign.md](../decisions/0001-session-runtime-redesign.md). The current system is described in [docs/remote-control.md](../remote-control.md).

## Context

SWE should be one control plane for AI coding agents across devices. Today the hub reads each harness's transcript file and sends input through a separate channel, joined by guesses (status from file activity, launches matched by folder and time, tmux approvals read off the screen). That is the source of the unreliability.

The target: SWE starts every controllable session itself, one adapter per harness carries input and output over a structured protocol, a keeper process holds the agent across hub restarts, and one React session view (t3code style) is shown in the browser, the Mac app and the iPhone app.

## Decisions (binding)

| # | Decision |
|---|---|
| D1 | SWE-started sessions only. Terminal-started sessions are read-only history with "Continue in SWE". |
| D2 | One web session view, served by the hub, embedded in the Swift Mac and iOS apps. No Electron now. |
| D3 | A running agent survives a hub restart, through a keeper process separate from the hub. |
| D4 | tmux is removed completely. Harnesses with no structured protocol are read-only until they ship one. |
| D5 | SWE speaks each harness's protocol directly in stdlib Python. No vendor SDK packages, no Node sidecar. |
| D6 | Every machine stands alone. The hub aggregates and is the one address a phone knows. |
| D7 | First version includes the changes panel, plan cards and plan mode, checkpoints with restore, and subagent rows. |
| D8 | The session view follows t3code's look: prose-first replies, "Worked for 2m 29s" work groups, approvals docked in the composer. |
| D9 | The session view is React with a build step, session view only. The rest of the web app stays plain JS. |
| D10 | iPhone: prove the web view first (scroll, keyboard, offline) and keep the SwiftUI chat as fallback. |
| D11 | The built bundle is committed under `src/swe/webapp/static/session/`, with a CI freshness gate. |

The alternatives rejected for each decision are in the decision record.

## What is verified, assumed, recommended

**Verified in code or docs**

- SWE already has structured channels: `CodexAppServerInjector`, `OpencodeServeInjector`, `AcpInjector`, `ClaudeHeadlessInjector` in [inject.py](../../src/swe/remote/inject.py). tmux is the fallback, and the default for Claude Code (`supervise.resolve_mode`).
- No device locks out another today. Ownership is `attached` or `observed` per session ([protocol.py](../../src/swe/remote/protocol.py)).
- `EventBus.publish` in [hub.py](../../src/swe/remote/hub.py) drops events for a stuck client. A hub restart fails queued sends ("hub restarted; message not sent").
- The iOS app replaced an earlier WKWebView wrapper because of scrolling, keyboard handling and a blank offline launch ([docs/remote-control.md](../remote-control.md), Phone client).
- All installs copy `src/swe` as source (pipx `git+`, `install.sh`, `bundle-swe.sh`).
- t3code: no agent runs in a terminal; 9 of 10 are CLI child processes over stdio; agents die on server restart; pending approvals are lost on restart; its UI is its own code on general kits, with no shared harness UI library.
- Claude Code (installed 2.1.327; Agent SDK source read): long-lived stream-json process, `control_request`/`control_response` with `can_use_tool`, `interrupt`, `set_model`, `set_permission_mode` (accepts `plan`), `stop_task`, `rewind_files`; `system` messages for `task_started/progress/notification` and `session_state_changed`; closing stdin cancels pending prompts; a second message mid-turn is queued by the CLI.
- Omnigent: one runner process per session under a host daemon that outlives its server; its stream has no cursor, and its own resilience doc ties stuck status and missing approval cards to that; it builds its web UI at wheel build.

**Assumptions to prove inside milestones**

- Claude's control wire format has no stated stability promise. The exact spawn line is settled by a recorded fixture in G7, starting from the SDK's own line.
- Claude steer (message priority "now") is proven by the G7 fixtures: it stops the reply in progress (a running tool call is killed, an open approval is withdrawn) and answers the new message in the same turn; it is offered only when the CLI lists `interrupt_send_now_v1`. The wire form of ExitPlanMode is proven too: a `can_use_tool` request for `ExitPlanMode` whose input carries the plan, mapped to a plan approval ([G7-claude-stream-adapter.md](G7-claude-stream-adapter.md)).
- A WebKit custom scheme handler can stream fetch bodies and use IndexedDB (G2 proves it; cookie auth is the fallback).
- A keeper costs about 15 to 20 MB. Measure in G5.
- Current clients tolerate gaps in seq numbers (needed by the compat layer).

**Defaults chosen (open to change at approval)**

- Mid-turn sends queue unless the user picks steer. First approval answer wins. Any device can edit any queued message. Each message records the sending device.
- Checkpoints go in a shadow store under `~/.swe/data/checkpoints/`, never as refs in the user's repos. Restore is files only in v1; conversation rollback only where the adapter declares it.
- The hub holds messages for a sleeping machine for 24 hours. A keeper parks an idle session after 30 minutes.
- Autonomy keeps today's default. Presence ("also open on iPhone") is a later milestone.
- For one release, the hub may still run `tmux kill-session` to convert an old pane.

## 1. Harness adapter architecture

- New package `src/swe/remote/adapters/`. `base.py` holds a `typing.Protocol`, `SessionAdapter`: `capabilities`, `start(spec, sink)` (new or resume, returns the harness session id), `send_turn`, `steer`, `interrupt`, `respond(request_id, answer)`, `set_option(name, value)` (model, effort, plan_mode, permission_mode), `stop_task`, `rollback`, `transcript_position`, `close`.
- Capabilities live in harness.json under `capabilities.session` (`adapter`, `argv`, `min_version`, `steer`, `plan_mode`, `rollback`, `fork`, `stop_task`), with `adapters/fallback.py` as the read-only fallback, as the repo's capabilities-first rule requires. `adapters/registry.py::adapter_for` is the only reader.
- No harness-name checks outside adapters. This removes the `tool_name == "claude"` branch and `_AUTO_MODE` in `supervise.resolve_mode`, the mode pairs in `inject.injector_for`, and `acp.EXPLICIT_ONLY`.
- Moves: `_AppServer` and `CodexAppServerInjector` to `adapters/codex_appserver.py`; `_ServeProc` and `OpencodeServeInjector` to `adapters/opencode_serve.py`; `adapters/acp.py` wraps [acp.py](../../src/swe/remote/acp.py)`::Connection`, which stays the wire module.
- New `adapters/claude_stream.py` plus `adapters/claude_wire.py` (the only file that knows the wire format). One long-lived process per session. Approvals map to `can_use_tool`, plan mode to `set_permission_mode`, subagent rows to `system` task messages, turn end to `session_state_changed: idle`. The session's init `capabilities` array drives feature detection.
- Canonical events (envelope `v: 2`, with `turn_id` and `command_id`): `session.state`, `session.caps`, `turn.started`, `turn.ended`, `message.delta` (transient), `message.completed`, `thinking.completed`, `tool.started|updated|completed` (with `file_edits`), `approval.requested|resolved` (kind tool, command, question, plan), `input.requested`, `plan.updated`, `task.updated` (subagent or background), `checkpoint.created|restored`, `changes.updated`, `queue.updated`, `command.receipt|result`, `usage.updated`, `context.compacted`, `error`, `notify`.
- `remote/protocol_compat.py` serves v2 events to current clients under today's names (`message.appended`, `tool_use`, `tool_result`, `session.status`), so the old chats keep working during migration.

## 2. Session management

- **Keeper:** one process per live session, `python -m swe.remote.keeper --session=<key>`, detached (`start_new_session=True`). It owns the agent's stdio, the adapter, the send queue and the event log. One per session, so a crash or upgrade touches one session only.
- **Surviving the service manager:** Linux uses `systemd-run --user --unit=swe-keeper-<slug> --collect`; macOS sets `AbandonProcessGroup` in the hub's LaunchAgent (`launchd.py`). Both are checked by a test that restarts the hub service.
- **IPC:** Unix socket under `/tmp/swe-<uid>/` (0700, owner checked; HOME paths can exceed the macOS 104-byte limit), newline-delimited JSON like `acp.py`. Handshake returns `last_seq`, pending approvals and the partial message, then replays from disk, then goes live.
- **Event log:** `data/remote/keepers/<slug>/log/events-<firstseq>.jsonl`. fsync on durable events (accepted command, approvals, turn boundaries, completed messages, checkpoints), 250 ms group commit otherwise, deltas never on disk, 8 MiB segments, 256 MiB cap with a `log.truncated` marker.
- **Discovery:** each keeper writes `keeper.json` and holds an flock. After a restart the hub reconnects to every keeper whose lock it cannot take.
- **Store:** `data/remote/remote.sqlite` (WAL, `busy_timeout`, backups through the backup API) with `sessions`, `commands`, `keepers`, `launches`. Only the hub writes it. `devices.json` and the other JSON files stay.
- **Command path:** every write carries `command_id` (`client_id` stays as an alias). Receipts are `accepted`, `duplicate`, `queued`, `rejected`, `held`. The keeper's single loop orders commands. The queue lives in the keeper and is rebuilt from its log.
- **Timeline source:** for SWE-owned sessions the keeper log is the timeline and `TailManager.sync` skips them. [tail.py](../../src/swe/remote/tail.py) stays for observed sessions and for backfill.
- **Launch:** every adapter knows its session id up front, so pending-marker adoption (`adopt_pending`, `_pending_match`) is deleted.

## 3. Multi-device communication

- Keep REST + SSE, hub and satellites, pairing, ntfy. `GET /v1/status` adds `protocols`, `features`, `boot_id`. A v2 client asks with `v=2` (header also accepted); everyone else gets today's frames.
- **Catch-up:** stream takes `last=seq&epoch=E`. Replay when the epoch matches and the gap is small, else one `session.snapshot` frame (row, state, pending approvals and inputs, queue, live text so far, plan, tasks, checkpoints, last 200 messages). `GET /v1/sessions/{id}/snapshot` serves the same body.
- **Live text is recovered separately from the journal.** `message.delta` is never journaled, so a journal replay alone cannot restore a reply that is still being written. Every catch-up therefore ends with a `session.state` frame that carries `live[]`: for each message in progress, its id and the full text so far. This holds for a small-gap replay, a snapshot, and an overflow recovery alike.
- **Delta offsets:** each delta carries the `offset` at which its text starts. A client that receives a delta whose `offset` does not equal the length of the text it holds drops that delta, stops appending to that message, and asks for `GET /v1/sessions/{id}/snapshot`, then resumes from the `live[]` entry it returns. A reply is never left incomplete until `message.completed`.
- **No silent loss:** when a subscriber's queue overflows, the hub re-reads journaled events from that subscriber's last sent seq and then sends the `session.state` frame described above. v2 sends `ping {ts, head}` every 10 s; the client treats 25 s of silence as dead.
- **Client:** a connection supervisor (states connecting, live, reconnecting, offline, hub down, unauthorized, gone, outdated; jittered backoff; wake on visibility, network and native `wake()`), and an IndexedDB outbox for sends only. Approvals, Stop and Restore need a live connection.
- **Satellites:** run their own keepers. `agent.py` forwards events with the keeper's seq; [journal.py](../../src/swe/remote/journal.py) gains `append_at(seq)` so a resend is idempotent. The hub answers `held` for a sleeping machine.
- **Standing alone:** every machine binds a loopback API with the same `/v1` session routes, authenticated by `data/remote/local.json` (0600). The Mac app uses it with no network.

## 4. Native session interface

- Source in `app/web-session/` (pnpm, Vite, React 19, TypeScript, Tailwind mapped onto SWE's tokens with `@theme inline`, Base UI components, `react-markdown`, `@legendapp/list`, `@pierre/diffs`, zustand). Tokens move from `styles.css` to `static/tokens.css`, shared by both pages and kept in step with `Theme.swift`.
- Served at `/session/` by `hub.py::_serve_static`; [static.py](../../src/swe/webapp/static.py) learns the `session/` names. A missing bundle falls back to `chat.js`.
- Copy from t3code with its MIT notice (`app/web-session/THIRD_PARTY_NOTICES.md`): `components/ui/*`, `ComposerPendingApprovalActions`, `ChangedFilesTree`, `DiffStatLabel`, `ConnectionStatusDot`. Rewrite, using t3code as reference: the timeline and work-group logic, and the composer (a plain growing textarea, no TipTap).
- Layout: breadcrumb header, timeline of turns, work groups, turn footer ("3 files +40 −12", "Restore to before this turn"), composer with attach, model and effort, plan toggle, Queue/Steer, Send/Stop, queued tray, approval and user-input docks. Changes is a right panel on desktop and a sheet on a phone.
- Embedding: a shared Swift package `app/shared/SessionWebShell` with one WebKit scheme handler (`swe-hub://`) that forwards to the hub and adds the bearer, so the token never reaches page scripts. Mac keeps its native sidebar and list (extends the pattern in [HubWebPage.swift](../../app/mac/Sources/SWEApp/Pages/HubWebPage.swift)). iPhone keeps the native navigation bar; only the timeline scrolls; the keyboard height is passed in as `--keyboard-inset`; the handler serves a cached copy offline.
- Read-only sessions use the same view with a bar in place of the composer: "Started in a terminal" with Continue in SWE, or the reason a harness cannot be driven.

## 5. Migration away from tmux

1. Structured adapters land one harness at a time behind `remote.session_view` and per-harness adapter capability. Claude Code first.
2. Release N: `auto` never picks tmux and launches never use it. Existing `mode: tmux` rows become observed with the note "running in tmux, read-only; Continue in SWE when idle". Panes are left running.
3. Release N+1: delete the tmux code in `supervise.py`, `TmuxInjector` and `ClaudeHeadlessInjector` in `inject.py`, the pane approval poller in `tail.py`, the tmux and headless attach modes in `protocol.py`, the tmux branches in `hub.py`, `fork.py`, `issues.py`, `slash.py`, `catalog.py`, the CLI `--mode` choices, and their tests.

Lose phone control on day one: Pi, Droid, Cline, Kilo, Freebuff, and fresh launches of Crush, Continue, Grok and Forge. Gain it through ACP `session/new`: Gemini, Qwen, Kimi. Session rows carry `control: read_only` and a reason for the UI.

## 6. Compatibility with existing sessions

- All history stays readable through `tail.py` and the parsers.
- "Continue in SWE" resumes by the harness's own id (`sessions/resume.py`), keeps the existing hub journal as earlier history, and starts the keeper at the next seq. It refuses while another process is writing the session.
- If the transcript grows while a keeper is parked (someone resumed it in a terminal), the session flips to observed.
- Old native app builds and `chat.js` keep working through the compat layer until the default flips.

## 7. Fault tolerance

| Failure | Behaviour |
|---|---|
| Hub restarts or crashes | Keepers keep running. Hub reconnects, clients replay or snapshot. Queue and pending approvals intact. |
| Client drops or sleeps | Supervisor reconnects; replay by seq or snapshot; outbox flushes sends once. |
| Retried send or approval | Same `command_id` returns the stored receipt; never applied twice. |
| Agent crashes | Keeper records `turn.ended {failed}`, retires open approvals, state `agent_down`; next send restarts it (3 tries in 10 minutes). |
| Keeper crashes | Agent exits with it. Hub records `turn.ended {keeper_lost}`; next send starts a new keeper and resumes by harness id. |
| Satellite offline | Hub answers `held`; commands deliver in order on reconnect; events dedupe by seq. |
| SWE reinstall with live sessions | Keepers import everything at start; the hub speaks the previous IPC version and cycles keepers when idle. |
| Harness CLI update changes the protocol | `min_version` and the init capabilities gate features; the session falls back to read-only with a reason; fixtures catch it in CI. |

## 8. Testing

- `adapters/fake.py` (scripted adapter) for keeper and hub logic; fake agents under `tests/fakes/` started like `FAKE_AGENT` in `tests/test_remote_acp.py`.
- Recorded fixtures per CLI version under `tests/fixtures/protocols/<adapter>/<version>/`, made by `scripts/record_protocol.py` (redacts HOME and tokens), each with golden canonical events.
- Keeper tests with an injected clock: kill the keeper, crash the agent mid-turn, restart the hub and answer a pending approval, torn log line, rotation, idle park, older IPC version. Every test stops its keepers in cleanup.
- Multi-client tests on `HubBase`: concurrent sends, duplicate ids, two approval answers, overflow replay.
- React: Vitest unit tests for reducer, supervisor and outbox; browser tests with Playwright; a JSON schema (`src/swe/remote/schema/session_v2.json`) checked from Python and used to generate the TypeScript types; a Python test that the committed bundle matches its source hash.
- Screenshots at 1280x800 and 390x844, light and dark: default, streaming, approval pending, queued tray, changes open, restore confirm, offline, read-only.
- Swift: shell package tests (request rewriting, origin pinning, bridge), Mac UI tests, iOS keyboard and deep-link tests.
- Everything keeps the throwaway-HOME rule, the 84% floor, and the end-to-end check against the installed `swe`.

## Milestones

The milestones, with their dependencies, owned paths and checks, are in [docs/roadmap/milestones.yaml](../roadmap/milestones.yaml). `hub.py` is one lane: milestones that own it run one at a time. Ready at start: G1, G2, G3, G10. Then G4, G6, G7 together.

## Critical files

`src/swe/remote/{inject,hub,supervise,journal,agent,protocol,tail,sendq,stream,acp}.py`, [static.py](../../src/swe/webapp/static.py), `src/swe/webapp/static/pages/chat.js`, [HubWebPage.swift](../../app/mac/Sources/SWEApp/Pages/HubWebPage.swift), `app/ios/Sources/SWERemote/Hub/ChatModel.swift`.

Reused as they are: `journal.py` (seq replay), `sendq.Waiting` claim logic, `acp.Connection`, `stream.DeltaStream`, `tail.decode_record`, `pairing.py`, `devices.py`, `readstate.py`, `notify.py`, `HubBase` in `tests/test_remote_hub.py`.

## Verification (end to end)

1. `HOME="$(mktemp -d)" PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` and the coverage run from [AGENTS.md](../../AGENTS.md).
2. `pip install -e .`, then with a temp HOME whose harness.json points `capabilities.session.argv` at the fake Claude agent: `swe remote serve`, launch a session, open `/session/`, send, see streaming, kill the hub mid-turn, restart it, answer the pending approval, confirm no gap.
3. Two browsers on one session: send from both mid-turn, answer one approval from both, check the queue and the "Allowed on" caption.
4. One manual run with the real `claude`: approval mid-turn, plan mode, a subagent, restore a turn.
5. `pnpm test && pnpm build` in `app/web-session`, then `git diff --exit-code` on the committed bundle.
6. Mac: `./script/build-app.sh debug`, open a session with networking off. iPhone simulator: scroll a long session, open the keyboard, launch offline.
7. Screenshots listed in section 8, at both widths, light and dark.

## Top risks

1. Claude's wire format drifts with no notice. Fixtures, `min_version`, and read-only fallback limit the damage.
2. The iPhone web view repeats the old faults. G2 decides before any iPhone work is spent.
3. The service manager kills keepers with the hub. G5 tests it on macOS and Linux.
4. Committed bundles conflict between parallel PRs. Rule: rebuild after rebase; CI gate.
5. Checkpoint time on very large folders delays turn start. Budget and skip with a visible note.
6. Five harnesses lose phone control when tmux goes.

## Carried forward from wave 1 (G1, G2, G3, G10)

Notes the first four milestones left for later ones.

- **G8 and G9:** `protocol_compat.to_v1` takes an `at_refs` argument that picks how attached files appear in message text for old clients (`@/abs/path` or `[Attached file: ...]`). The caller passes `attachments.at_refs(harness)` for the session.
- **G8:** old clients get no frame for `input.requested`, so a session waiting on a question would look idle to them. Map a single question with options onto the existing approval card for those clients.
- **G14:** Tailwind utility names such as `flex` and `hidden` are global class names. Check they cannot restyle the plain-JS app once the session view is mounted inside it.
- **G17 and G18:** the findings in [G2-iphone-webview-proof.md](G2-iphone-webview-proof.md), and the open questions on G18 in [milestones.yaml](../roadmap/milestones.yaml). The iPhone move stays blocked until the page-load stall seen under heavy machine load is reproduced or ruled out.
- **Current clients tolerate gaps in `seq`.** All three keep only the highest seq seen. Two frames sharing one seq is the hazard, so the compat layer sends at most one old-style event per v2 event ([G3-adapter-protocol.md](G3-adapter-protocol.md)).

## Carried forward from wave 2

- **G13:** the Steer control must say that it stops the current reply, with one wording for every harness in the first version. On Claude a steer also kills a tool call that is running: the call ends cancelled, and what it would have written never appears (the `steer_tool` recording in [G7-claude-stream-adapter.md](G7-claude-stream-adapter.md)).
