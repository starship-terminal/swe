# Remote control

Status: shipped. R1–R5 all landed — `swe remote serve|setup|agent|pair|
status|devices|attach|notify|ssh`, the hub-served web app, the iOS app and
the macOS app's Settings › Integrations › Remote checklist all exist. This doc stays as
the design record: what follows describes the build as it is, and the
milestones table at the end is history, not a plan.

A redesign of session execution is in progress: SWE starts every controllable
session itself, a keeper process holds the agent across hub restarts, and one
web session view replaces the separate chats. Read the
[spec](specs/G1-session-runtime-redesign.md), the
[decision record](decisions/0001-session-runtime-redesign.md) and the
[roadmap](roadmap/milestones.yaml). The first part is in the hub behind a
setting that is off by default: see
[Sessions on keepers](#sessions-on-keepers--runtimepy). With it off, the
rest of this doc is accurate as it stands.

Watch and steer CLI coding sessions (Claude Code, Codex, opencode and the
rest of the registry) from a phone. Read the chat, send prompts, see new
messages stream in, answer approvals, get notified when an agent needs a
decision. The Claude Code Remote Control and ChatGPT Remote experience,
generalised across every harness `swe` already understands.

## Topology

```
              ┌─────────────┐
              │ phone       │
              │ (web app /  │
              │  iOS app)   │
              └──────┬──────┘
                     │ HTTPS + SSE over Tailscale
                     ▼
   ┌────────────────────────────────┐
   │ home server (Linux, always on) │
   │ `swe remote serve` — hub       │
   │ catalog · journal · tailers ·  │
   │ injectors · notify fan-out     │
   └──┬──────────────────┬──────────┘
      │ local tail/inject           │ outbound persistent conn (Tailscale)
      ▼                             ▼
 sessions on this box      ┌──────────────────┐
                           │ MacBook          │
                           │ `swe remote agent│
                           │ — satellite      │
                           └──────────────────┘
```

The phone only ever talks to the hub. Satellites dial *out* to the hub —
no inbound ports anywhere, the same shape Claude Remote Control uses
("registers with the API and polls for work" / streaming connection). A
sleeping satellite is normal: the hub marks its sessions offline and holds
queued commands until it reconnects.

Tailscale is the transport because the relay is our own server: nothing
third-party stores transcripts (Claude RC stores them on Anthropic
servers; we don't need to). v1 is tailnet-only: the hub binds its
Tailscale IP, never `0.0.0.0` unless `swe remote serve --unsafe-bind-any` is passed. A TLS fallback with a cert pinned at
pairing is deferred until a non-tailnet device actually needs in.

### Setting it up — `setup.py`

`swe remote setup` checks every step from nothing to a paired phone, in
order, and says for each whether it is done, something SWE does
itself, or something a person must do, with the page to open. The Mac
app's Settings › Integrations › Remote shows the same list from `--json`. On a fresh
Linux box, running the installer's `--hub` variant
(`... | sh -s -- --hub`, see the README's install section) installs swe
and then runs this list with `--apply`.

| Step | Checked by | When it isn't done |
|---|---|---|
| Tailscale installed | the CLI on PATH or inside Tailscale.app | link to the download page |
| Signed in | `BackendState` in `tailscale status --json` | the `AuthURL` Tailscale reports, else open the app (Mac) or `sudo tailscale up` (Linux) |
| MagicDNS on | `CurrentTailnet.MagicDNSEnabled` and the node's `DNSName` | link to the admin console's DNS page; only an admin can turn it on |
| HTTPS certificates on | the node's name in `CertDomains` | the same DNS page, Enable HTTPS |
| Certificate | `tailscale cert` into a temp folder, removed at once (tailscaled keeps its own copy) | the reason, in plain words: a DNS lookup timeout says to add a reachable DNS server or turn on Override DNS servers |
| Hub running in the background | the LaunchAgent (Mac) or systemd user unit (Linux) runs `serve --host=127.0.0.1 --public-url=https://<name>:<port>` and answers on loopback | `--apply` installs it; a Linux user whose services stop at logout is told to run `sudo loginctl enable-linger $USER`; without a systemd user manager, the `serve --public-url` command to run under the init system |
| Reachable over https | `tailscale serve status --json` proxies `:<port>` to the hub, and `https://<name>:<port>/v1/status` answers | `--apply` runs `tailscale serve --bg --https=<port> http://127.0.0.1:<port>`; on Linux a refused serve gives `sudo tailscale set --operator=$USER` |
| A phone paired | `devices.json` | Pair a phone |

A step waits (`blocked`) until the ones before it are done. `--apply`
loops look, act, look again, and stops at the first step that needs a
person or failed, reporting what it found rather than what it hoped.
The certificate is fetched before the hub moves: a hub moved behind
`tailscale serve` without one is unreachable from every device (the TLS
handshake hangs), while the hub it replaced, on the tailnet IP, worked.
A fetch that worked is cached for a day and one that failed for a
minute (`data/remote/setup-cert.json`), so polling the list stays
quick; `--recheck` fetches again. Installing the LaunchAgent retries
`launchctl bootstrap` when it fails with "Input/output error", which it
does while launchd is still tearing the old job down.

### Behind `tailscale serve`

`swe remote serve --public-url=https://box.tailnet.ts.net:8477` runs the
hub on `127.0.0.1` with `tailscale serve` in front: serve terminates TLS
with the node's Let's Encrypt cert and dials the hub on loopback. Every
request then arrives from `127.0.0.1`, so with `--public-url` on a
loopback bind the hub reads the client from the headers serve sets:

| Header | Used for |
|---|---|
| `X-Forwarded-For` | the tailnet client: `tailscale whois` for pairing and approval, the pair-request rate limit |
| `X-Forwarded-Proto` | the scheme the `Origin` check expects (`https://<Host>`) |
| `Host` | kept as the public `host:port`; the DNS-rebinding check on the approval path and the unauthenticated routes allows it plus the loopback names the same bind answers locally |

Serve replaces any `X-Forwarded-For` / `Tailscale-User-*` the client sent
(checked on a live node, 2026-10-23), and the hub reads the headers only
from loopback peers while `--public-url` is set; on a tailnet-IP bind
they are ignored. A loopback peer alone isn't serve: any local process,
another user's included, can dial the same port and write its own
`X-Forwarded-For`. So the hub believes that header only when the OS says
the far end of the connection is the Tailscale daemon (`peer.py`):

| Platform | The proxy is |
|---|---|
| Linux | the socket whose owner in `/proc/net/tcp` is root |
| macOS | the pid `netstat -anv` names for the socket (or, where netstat's table is blind, the daemon pid `nettop` lists it under; see below), when it runs as root and its executable (from the kernel, `proc_pidpath`) is the daemon's own: the Standalone variant's system extension under `/Library/SystemExtensions/<UUID>/io.tailscale.ipn.macsys.network-extension.systemextension/`, open-source `tailscaled`, or the App Store variant's `Contents/PlugIns/IPNExtension.appex/Contents/MacOS/IPNExtension` inside a root-owned `/Applications/Tailscale.app` |

No other executable in `Tailscale.app` counts on macOS, whoever runs
it: the app itself (`Contents/MacOS/Tailscale`), its updater, widgets
and the staged copy of the system extension are not the daemon, and
neither is any other root process. The App Store variant's
extension has not been checked on a live install; it is held to the same
root rule, so if it runs as the signed-in user it is not recognised and
its requests read as loopback.

On both platforms the check reads only the table row for exactly this
connection (local end the peer, far end the hub), and only while that
row is `ESTABLISHED`. The owner either table shows for a socket that is
closing or closed is not reliable, so such a row never counts. Two rows
for one connection, a row cut short, or a pid column that is not a
plain positive number all count as "can't tell". The answer is kept for
the connection, and a request whose peer fails the check, or that the
check can't place, is read as the loopback client it is: no tailnet
identity, bearer credentials unaffected.

The macOS `netstat` table can be blind ([#1944](https://github.com/starship-terminal/swe/issues/1944), [#1950](https://github.com/starship-terminal/swe/issues/1950)).
On macOS 27 a process that is ad-hoc or Apple-Development signed, or
has a live ancestor that is, sees an empty TCP table: `netstat -anv`
lists no sockets. Homebrew's Python, Nix's shells and the Python bundled
in a locally installed SWE.app (`app/mac/script/install-app.sh` signs it
for development) are all signed that way. When netstat's table is
empty, and only then, the hub asks `nettop` (NetworkStatistics), which
such a process can still read:

| Step | What the hub requires |
|---|---|
| Find the daemon | processes whose executable, from the kernel, is the daemon's own (the same paths as the table above) and that run as root; process names never pick one |
| Ask about each | `nettop -p <pid>` lists exactly one row for exactly this connection (local end the peer, far end the hub), and it is `Established` |
| Read the listing | by its fixed shape: known leading columns, that pid's own row first, socket rows after, every row the header's width; anything else is "can't tell" |
| Check the pid kept still | its executable, uid and start time are the same after the listing as before it |

More than one daemon pid holding the connection, `nettop` missing, slow
(over 2 s) or printing anything unexpected, all count as "can't tell".
Whichever tool names the pid, the same final check decides: root and
the daemon's own executable. A table netstat can see is never
second-guessed: a "no" from it stands.

If neither can see, the check still fails closed, so nothing is let in
that should not be: the owner's devices need a paired credential and
`POST /v1/pair/request` answers `403 not_tailnet` (pair with a token
instead). At start, `swe remote serve` behind serve asks the same
lookup (netstat, then nettop when netstat is blind) about a loopback
connection of its own (`peer.owner_lookup_works`) and, when no source
sees it, prints one line to stderr (`serve.log` under the LaunchAgent):

```
2026-11-15 10:42:07  warning: this hub cannot see which process owns a loopback connection, so requests through tailscale serve lose their tailnet identity (the owner's devices need a paired credential and pair requests need a pair token); run the hub with a Developer-ID-signed Python, such as python.org's, started by launchd or a signed shell
```

The probe only feeds that line; the check accepts exactly what it
accepts without it.

What the check cannot tell apart, on either platform or with either
tool: a connection the daemon itself opens on a local caller's behalf
really is the daemon's. A daemon feature that dials a local port for
another local process, rather than for a tailnet client, would reach
the hub as the proxy. The CLI's relay (`tailscale nc`) is not one: the
CLI process holds that connection, so it is refused.

`X-Forwarded-Proto` keeps the loopback rule, since it only feeds the
`Origin` check a local process can pass anyway. Owner identity
comes from `whois` of the node's own tailnet IP, which also records
that node: a request `whois` names as this machine's own node never
counts as the owner's device, because serve's local listener forwards
any local process (and a direct dial of the tailnet IP arrives) as that
node, whichever user runs it. A browser on the hub machine pairs like
any other device. The public URL also
becomes the ntfy action-button base, and `serve` saves
`{host, port, public_url}` to `data/remote/hub.json` so `swe remote
pair` prints `https://` links (the `quiver://` deep link gains
`scheme=https`) and `swe remote status` probes the loopback bind.
Satellites dial the `https://` URL; when Python's default store has no
CA certificates (python.org builds on macOS), the agent retries once
against the OS bundle and keeps it, never skipping verification.
`swe remote serve --install --public-url=URL` writes a LaunchAgent
(Mac) or systemd user unit (Linux) that runs the hub on `127.0.0.1`
with that URL instead of `--tailnet`.

## Precedents

| System | Transport | Write path | What we take |
|---|---|---|---|
| Claude `remote-control` / `/rc` | outbound HTTPS to Anthropic relay, transcript stored server-side | same-process channel | ownership semantics, QR pairing, reconnect journal, Queue semantics, compaction/diff/model controls |
| ChatGPT Remote (Codex) | desktop app as host, account relay | app-server internally | Queue vs Steer follow-ups; "phone as control plane" framing; approvals + diffs + notify |
| `codex app-server` | JSON-RPC 2.0 over stdio | `thread/resume` + `turn/start`, server-initiated approval requests, `command/exec` pty | our tier-A Codex inject channel |
| `opencode serve` | HTTP + one SSE `/event` | `POST /session/{id}/prompt_async`, `permission.asked`/`replied` over HTTP | our tier-A opencode channel; API-driven-seat model |
| Agent Client Protocol | JSON-RPC 2.0 over stdio | `session/load`/`new`, `session/prompt`, `session/update` chunks, `session/request_permission`, `session/cancel` | our ACP channel (`remote/acp.py`) for devin, cursor-agent and copilot builds with an ACP mode (gemini, qwen and kimi are detected but have no resume flags to attach with); claude via `claude-agent-acp` on `--mode=acp` (#1637) |
| Happy Coder | encrypted blob relay, QR key exchange, offline queue | pty wrapper | pairing + offline-first queue shape (no E2E needed — relay is ours) |
| VibeTunnel | pty → WebSocket, server-side VT emulation | pty write | pty is an *input* fallback, never the display format |

## Ownership

Two writers on one transcript is the failure mode the open Codex issue
reports (CLI session invisible to mobile; write-lock conflict). Rule:

- `attached` — the session runs under our supervisor (tmux, `opencode
  serve`, `codex app-server`, headless turns). Read **and** write.
- `observed` — a human terminal owns it, or the harness has no inject
  path. Read-only plus notifications.

Composer renders only on `attached` sessions. `observed` sessions offer
**Attach**: relaunch under the supervisor via the resume flags
`sessions/resume.py` already knows (`claude --resume`, `codex resume`,
`--session`, `--continue`, …). Attach is offered only while the session
looks idle (no transcript append in ~5s, status not running), and the
hub re-checks at execution time: killing a terminal mid-keystroke is
the failure mode. `LIMITED_RESUME` harnesses stay observed.

## New domain: `src/swe/remote/`

Stdlib-only — SSE is a held HTTP GET, no WebSocket dependency needed
(POST upstream + SSE downstream covers the protocol). If a later phase
wants WS it goes behind a new extra; `server` is taken by the mcp extra.

```
remote/
  protocol.py    # wire schema: events, messages, commands (versioned)
  pairing.py     # QR payload, one-time token → device credential
  catalog.py     # session registry: local + satellite rows, machine-scoped
  devices.py     # the paired-device list + revoke, shared by /v1/devices and web Settings
  dismissals.py  # per-device inbox dismissals (inbox_dismissals.json, 0600)
  readstate.py   # per-device last-read cursors, the rows' unread flag (session_reads.json, 0600)
  sessionmeta.py # pins and display names, hub-wide (session_meta.json, 0600)
  tasks.py       # work-item store: sessions/launches linked to tasks (tasks.json, 0600)
  tail.py        # byte-offset transcript readers → events (read path)
  inject.py      # per-harness send/approval/interrupt registry (write path)
  supervise.py   # tmux lifecycle for attached sessions
  detach.py      # detach when_idle: wait out the turn, cancellable
  fork.py        # fork a session: per-harness fork command lines, the route
  journal.py     # per-session seq-numbered event log (replay on reconnect)
  hub.py         # http.server ThreadingHTTPServer: REST + SSE, device auth
  runtime.py     # the hub's side of session keepers (behind remote.session_view)
  keeper/        # one process per kept session: agent, queue, event log, IPC
  adapters/      # one structured channel per harness (claude_stream, codex_appserver, opencode_serve, acp), v2 events
  store.py       # remote.sqlite: sessions, keepers, launches, command receipts
  protocol_compat.py  # v2 events as today's v1 frames
  peer.py        # is a loopback peer the Tailscale daemon? gates X-Forwarded-For
  files.py       # @-mention paths under a session's folder (/v1/sessions/{id}/files)
  preview.py     # one .html/.svg file from a session's folder (/v1/sessions/{id}/preview)
  agent.py       # satellite daemon: dial hub, run local tail/inject and keepers; the loopback API; the hub's side of satellite keepers
  notify.py      # needs-you/finished/error/GitHub-unread/service-status fan-out via ntfy
  launchd.py     # serve/agent --install: the hub or satellite as a per-user LaunchAgent
  systemd.py     # serve/agent --install on Linux: the hub or satellite as a systemd user unit
  setup.py       # swe remote setup: every step to a paired phone, --apply
  commands.py    # swe remote serve|agent|pair|attach|status — argv + print
  attachments.py # phone uploads staged on the session's machine
  sendq.py       # per-key FIFO send queue: message writes off the HTTP handler
  stream.py      # message.delta: live text coalesced per message, never journaled
  injected.py    # strip harness-injected context from chat messages
  slash.py       # the "/" menu: commands a session's harness will run
  slash_builtins.py  # curated built-in slash commands per harness
  issues.py      # /v1/issues: pinned repos' issues, resolve, comment
  pulls.py       # /v1/pulls: pinned repos' PRs, open in a session
  linear.py      # the Linear task source
  search.py      # /v1/search over the session-search index
  usage.py       # /v1/usage: usage and limits
  session_model.py   # the model each session's last reply ran
  recent_models.py   # the models each harness ran lately, for the pickers
  launch_model.py    # how a fresh launch starts a harness on a picked model
  sshkeys.py     # phone SSH keys in authorized_keys, host keys for /v1/ssh
  tsid.py        # Tailscale node identity: whois for a peer IP
```

Layering: `remote` imports `sessions`, `reports`, `github`, `ledger`
(session models), `mcp.secrets` (the Linear key), `webapp` (the hub
serves it), `harness`, `json_output`, `paths`, `console` and `flags`.
The web app's pages import
`remote` modules back (catalog, devices, pairing, setup, supervise), most
of them function-local, and two other packages borrow from it:
`mcp/gateway_commands.py` uses `remote.launchd` and `ledger/pricing.py`
uses `remote.agent._urlopen`. State under
`~/.swe/data/remote/` (`devices.json` 0600, `inbox_dismissals.json`
0600, `session_reads.json` 0600, `session_meta.json` 0600, `tasks.json` 0600, `cursors/`, `journal/`) —
gitignored like the rest of `data/`. The hub's per-request readers of
`devices.json`, the sign-ins file, `session_reads.json` and
`machines.json` go through `paths.read_json_shared`, which parses a
file again only when its inode, mtime or size changes; its value is
shared, so a read-change-write still reads the file itself (`load_*`,
never `peek_*`). New commands run the usual
checklist: `completion.py`, `help_text.py`, `docs/json-contracts.md`.

## Read path — `tail.py`

Copy the `search_index.py` `files`-row pattern: byte-offset cursor per
transcript, stat → read appended bytes → decode new records → emit
events. ~1s poll while `sessions/status.py` says active, back off idle.

Cadence per session, fastest rule first:

| State | Poll every | Constant |
|---|---|---|
| Hot: the hub or satellite wrote to it in the last 15 s | 0.25 s (snapshot stores 1 s) | `POLL_HOT_S`, `HOT_WINDOW_S` |
| Active per `sessions/status.py` | 1 s | `POLL_ACTIVE_S` |
| Anything else | 8 s | `POLL_IDLE_S` |

Low power stretches the active and idle rows to 5 s and 60 s
(`POLL_ACTIVE_LOW_S`, `POLL_IDLE_LOW_S`), the catalog refresh from 10 s
to 60 s (`CATALOG_IDLE_S`) and the tail loop's tick from 0.25 s to 1 s.
The hub is in low power while nobody could see what it reads: no SSE
stream is open (the ntfy notifier holds one whenever it can push;
satellite command streams don't count) and no client made an
authenticated request in the last two minutes (`CLIENT_IDLE_S`). The
transcripts stay on disk, so a slower poll only delays the journal's
copy. The next client request (`HubState.note_client`) ends it at once:
every tailer is owed a poll, the catalog loop wakes, and a list request
(`/v1/sessions`, `/v1/inbox`, `/v1/app/sessions`) that finds the catalog
over 20 s old waits up to 5 s for the fresh refresh, so a client that
polls without a stream never reads a minute-old list
([#1520](https://github.com/starship-terminal/swe/issues/1520)).

A write is any message that actually goes out (queue mode once the wait
is over, steer at once), an approval answer, a control op, or an attach.
Every injector the hub or a satellite builds comes wrapped by
`TailManager.waking`, which calls `TailManager.wake(key)` before and
after the write; the attach routes call it directly. `wake` is safe from
any thread: it marks the key owed a poll, opens or extends its window,
and sets an event the tail loop parks on, so the loop polls at once
instead of sleeping out its 0.25 s tick or the session's 8 s idle
interval. Each new write re-arms the window. Without it a send to an
idle session waited up to 8 s for its echo, and longer for the reply,
until the next catalog refresh (every 10 s) flipped the row to active.

The hot window costs little because only woken sessions get it. A JSONL
poll that finds the file the same size is one `stat`, and an opencode
poll is a few queries on per-session indexes. Snapshot stores (Gemini,
Cline, ...) re-read the whole window whenever their change token
moves, which is the cost behind [#515](https://github.com/starship-terminal/swe/issues/515), so
`SnapshotSource.hot_poll_s` caps them at the active cadence: they get the
immediate wake but never poll faster than an active session already
does.

Devin reads only what changed (`reports/transcripts.py::DevinChain`,
[#1522](https://github.com/starship-terminal/swe/issues/1522)). Its source keeps
the window's decoded chain nodes; a moved token fetches the rows written
since the last read (a rewrite comes back under a new `row_id`), walks
from the head until it meets a kept node, and cuts the kept chain there,
so a switched `main_chain_id` splices at the fork. A walk that meets
nothing kept, a rewritten node with a new parent, or 60 s since the last
whole read (`DEVIN_FULL_READ_S`) reads the window whole, which also
bounds anything the step read cannot see, such as a row edited in place.
A session unchanged for five minutes (`DEVIN_CHAIN_KEEP_S`) lets its
kept nodes go.

Status between catalog refreshes: a session that is hot, or whose
source moved since its last probe, has its status re-probed by the tail
thread at most once a second (`STATUS_PROBE_S`). "Moved" means the poll
advanced the cursor, message or not, so a turn that ends on a record
with nothing to show (Codex `task_complete`, Claude `turn_duration`)
still gets its probe. Every session owed a probe in one tick goes
through a single `sessions/status.py` call, so the live-pid registry,
the bg-job table and the status tail cache are read once per tick, not
once per session. Each session is probed with the session's last-activity time raised to the
transcript's mtime (the catalog's copy can be minutes old, and a
mid-turn transcript older than `ACTIVE_WINDOW_S` reads as
interrupted). A probe that finds the session no longer active also nudges a pending
`when_idle` detach (`PendingDetaches.nudge`) to check at once instead of
at its next 2 s poll. Besides writes through the hub, a launch, a fork,
and an attach the catalog loop notices (the CLI, a kept tmux session
taken back) wake the session too. A change emits `session.status` and
`notify` at once, so
a send flips the phone to active within about a second and the end of
the turn flips it back as fast. Sessions neither hot nor growing are
never probed.

Two readers now set a session's status, so each read carries the time
it was taken and the newer read wins:

| Who | Time of the read | Where the rule lives |
|---|---|---|
| Catalog refresh | when the refresh started (`Catalog.tail_snapshot`) | `TailManager.sync(rows, as_of=...)` skips keys probed later |
| Tail probe | just before the probe reads, not the tick's start | `Catalog.note_status` patches the row; a refresh that started earlier keeps it |

So a refresh that read "done" just before the send landed cannot flip
the phone back to done, and the catalog's own rows (the session list,
the inbox, a satellite's catalog push) agree with the events. A tie
goes to the catalog. `TailManager` writes `statuses` and emits status
events only under `_status_lock`, since the catalog thread (`sync`) and
the tail thread (probes) both write them. The probe reads outside that
lock and applies a result only if the session's tailer is still the
same object, and `sync` drops tailers under the lock, so a session the
catalog removed mid-read never comes back as a status event. Satellites probe the same way
and their catalog push carries the result.

Before a message leaves the hub, `injected.py` removes what a harness
wrote into the transcript for itself rather than for you: Claude Code's
`<system-reminder>` and `<task-notification>` envelopes (a finished task
becomes one system line), Cursor's `<timestamp>`/`<user_query>` wrapper,
Codex's developer turns, `AGENTS.md` and environment injections and its
`<oai-mem-citation>` footer. It runs at decode and again when journal
events are served, so history journaled before it existed is cleaned too.

One upgrade over `reports/transcripts.py`: that layer *strips* thinking
and collapses tool calls — right for reports, wrong for a chat UI that
auto-collapses them. `tail.py` keeps the block detail the raw records
already carry (Claude jsonl thinking/redacted_thinking blocks and
compact boundaries, Codex reasoning items and `compacted` payloads,
opencode part types). Message model:

```python
RemoteMessage:
  seq, session_id, machine_id, ts
  kind: user|assistant|thinking|tool_use|tool_result
      | compaction|system|approval|error
  text                          # markdown for user/assistant
  tool:  {name, call_id, args_summary, args_full}
  result_ref                    # tool_result → tool_use call_id
  duration_s                    # thinking only, when known
```

`duration_s` ("Thought for 12s") is measured, not guessed. Claude Code
and Codex write each content block as its own timestamped record the
moment the block finishes, so a thinking record's timestamp is when the
thinking ended and the record before it (the prompt, a tool result,
Claude's `api-request`) is when it began. `JsonlTailSource` keeps that
previous timestamp (`since`, saved in the cursor so a hub restart
between the two still times it) and `stamp_thinking` sets the gap on a
record that decodes to thinking alone. Records that belong to the
thinking are not a start: Claude Code's empty thinking record written
just before the full one, and Codex's `agent_reasoning` and
`item_completed` Reasoning events. The gap to the *next* record would
time the reply after the thinking, not the thinking. Because the start
is always read before the thinking record, the value goes out on the
first `message.appended` and nothing is patched later. A record mixing
thinking with text (one record per reply) and the first record after a
backfill start carry no duration. opencode parts and 2.x content blocks
carry their own `time` span, which is used as is. Every other harness
writes a whole reply as one record, so it sends none.

`_redact_secrets` runs before anything leaves the process — prompt text
is user-originated, transcript text is not.

Granularity is **record append**, not token delta: transcripts only
materialize completed records. True deltas come free on tier-A channels
(opencode SSE, codex notifications, `claude -p --output-format
stream-json`) where the harness emits them; `kind` survives both paths.

`needs_you` detection is tiered the same way: tier A gets it free from
structured events (approval requests, `permission.asked`); for tail-only
sessions the tailer infers "waiting for input" from transcript state,
reusing the `reports/followups.py` classification where it applies
rather than a second heuristic.

## Write path — `inject.py`

Registry keyed by `tool_name`, same shape as `PARSER_REGISTRY` /
`READER_REGISTRY`:

| Tier | Harness | Channel | Gets |
|---|---|---|---|
| A | opencode | `opencode serve` REST + SSE (`prompt_async`, permission replies) | structured msgs, approvals, interrupt, multi-client |
| A | codex | `codex app-server` JSON-RPC (`thread/resume`+`turn/start`, approval reqs) | structured items, approvals, deltas |
| A | claude | headless `claude -p --resume <id> --output-format stream-json` per message | one appended turn per call, deltas; permissions preset via `--permission-mode`/`--allowedTools` (no mid-turn prompt in print mode — flagged limitation) |
| A | any harness whose CLI has an Agent Client Protocol mode (devin `acp`, cursor-agent, copilot; gemini, qwen and kimi are detected but not attachable without resume flags; claude through the `claude-agent-acp` adapter, opt-in) | `acp.py` JSON-RPC over stdio (`initialize`, `session/load` or `session/new`, `session/prompt`, `session/cancel`) | streamed text, thought and tool deltas, approvals with the agent's own options, interrupt |
| B | resumable TUIs (gemini, cursor, crush, cline, droid, pi…) | `supervise.py` tmux: send = `send-keys` (C0 control characters other than tab and newline dropped first; multi-line text as one bracketed `paste-buffer -p`), prompt/approval = `capture-pane` parse, display still transcript tail | universal write; approvals are buttons that send menu keys — best effort |
| C | neither resumable nor headless | tail only | read + notify |

### Agent Client Protocol — `acp.py`

`swe remote attach --mode=acp` (or `auto`, see below) runs the
harness's ACP agent (agentclientprotocol.com) as a child of the hub,
one per attached session, and speaks newline-delimited JSON-RPC 2.0
over its stdio. `acp.command_for` decides whether a harness has an ACP
mode here: `capabilities.acp` in harness.json (`{"argv": [...]}`, or
`{"supported": false}` to turn it off) wins over the `ACP_FALLBACK`
table, and a fallback candidate counts only when the CLI's own
`--help` lists its subcommand or flag as a whole word (cached per
binary path and mtime). An adapter binary (`claude-agent-acp`, then
`claude-code-acp`) only has to be on PATH, since an ACP server run with
`--help` may wait on stdin. codex and opencode have no entry: their
`app-server` and `serve` channels expose more than their ACP modes.

`auto` picks ACP after the native tier-A channels and before tmux,
except for claude (its tmux pane already answers menus, so the adapter
is used only on an explicit `--mode=acp`) and except when the attach
carries a `model` or `effort` pick, which ACP has no verified control
for. Attach starts the agent and does the handshake before writing the
registry row: `initialize` (protocol version 1, no `fs` or `terminal`
client capabilities, so the agent uses its own tools), then
`session/load` with the id the harness's resume flags carry when the
agent advertises `loadSession`. Attach still needs those resume flags,
so a harness without them (gemini, qwen, kimi today) is not attachable
on any channel. The handshake gets `ATTACH_ACP_S` (5 s), which leaves
a tmux fallback room inside the apps' 15 s attach timeout. Under
`auto`, an agent that fails any step (not signed in, a load error, too
slow, a crash) or can't load sessions at all falls back to the tmux
launch, and the note carries the reason. An explicit `--mode=acp`
reports a failure instead, except on an agent without `loadSession`:
that one falls back to tmux under either mode, with the reason in the
note. Attach never runs `session/new`, since the chat keeps tailing
the attached session's transcript, which a new ACP session never
writes.

`POST /v1/sessions` (and a satellite's `launch` command) for a harness
`auto` would attach over ACP starts it over ACP too, when the launch
carries no `model`, `effort` or `provider` pick and swe can read
the harness's sessions (`supervise.acp_launch_eligible`). No resume
mapping is needed: `_launch_acp` reserves the folder with a pending
marker (`mode: acp`), runs `initialize` and `session/new` within
`LAUNCH_ACP_S` (20 s; the launch runs after the 202, so the apps'
timeout doesn't bind it), writes the new session's id into the marker
as `expect_id`, and sends the first prompt. Adoption then binds exactly
that session, as `mode: acp`, once its transcript shows; an ACP marker
without its `expect_id` yet adopts nothing, so a slow handshake can't
claim someone else's session in the folder. The agent is
held under the key that id gives (`tool:<id>`); events it raises before
the session has a tailer (a first-turn approval) wait in the connection
and go out once the hub's tail sync has one (`release_held`), since a
journal written before the tailer starts would make it skip the first
turn. An `initialize` or `session/new` that fails or times out, or an
agent that exits on the first prompt, falls back to the tmux launch,
with the reason in the launch's note (kept on the launch record, so
`GET /v1/launches/{id}` shows it), and the fallback never adopts the
session the dead agent created. An agent that refuses the first prompt
fails the launch. With `yolo: true` (Autonomy) the connection
answers each `session/request_permission` itself with the first option
of kind `allow_once`, never an `allow_always` one, and journals the pair
so the apps show what was allowed: `approval.requested` with `auto:
true` (no ntfy push) and `approval.resolved` with `decision: "allow"`,
`source: "autonomy"`. A request offering no `allow_once` option gets the
usual approval card. After Stop, any request still arriving in that
turn is answered `cancelled`, never allowed. While a launch's events are
held for its tailer, the record of auto-answers is what gets shed past
64 events; a request that needs an answer is always kept. `yolo` rides
onto the row at adoption, so a
restarted agent keeps answering this way. The launch wire is unchanged: the 202's note
reads "starting over ACP", and an ACP launch needs no tmux.

The attach's live agent stays registered with the injector, so the
first send in the hub process reuses it; an agent that has exited is
restarted on the next send and loads the same session. Each send is one
`session/prompt`; its answer ends the turn. ACP takes one prompt per
session at a time, so a send that arrives mid-turn waits for the turn
to end, then goes (`steer` reports `degraded: true`). A queued send
waits too: a harness with no status probe reads `""` mid-turn, so the
hub's queue may let it through early. An answer within a second (a
refused prompt) is reported as the send's result; an error answer
after that arrives as a `notify` event of kind `error`, as does the
agent exiting mid-turn. Interrupt
is `session/cancel`. `session/request_permission` becomes
`approval.requested` with `toolCall.title` (plus its command or path)
as the prompt (the title and input an earlier `tool_call` update gave
the same id when the request carries only the id, as devin's does, and
a `_meta` field ending in `command` as the last resort) and the agent's options as the menu rows (`n` from 1,
`label` the option name, `kind` the option's kind, and `standing: true`
for `allow_always`/`reject_always`, which an ntfy button never offers
whatever the label says); `allow`, `always` and `deny` pick the first
option of kind `allow_once`/`allow_always`, `allow_always`, and
`reject_once`/`reject_always`. A cancel, the turn ending or the agent
dying retires open requests with `approval.resolved` (`source`
`cancelled`, `turn_ended` or `channel_closed`), answering the agent
`cancelled` where it still listens. Every other agent request (`fs/*`,
`terminal/*`) gets JSON-RPC error -32601; unparseable lines are
skipped. Detach closes the agent's stdin, gives it two seconds to exit,
then terminates it. Model and effort controls answer
`unsupported_control`.

### Live text — `stream.py`

The tier-A channels read a reply as it is written and push each text
chunk into the machine's `DeltaStream`, which sends a `message.delta`
at most every 100 ms per message plus a last one with `done: true`
(payload in docs/json-contracts.md). Codex streams
`item/agentMessage/delta` (finished by `item/completed` or the turn
ending); opencode streams `message.part.updated` with a `delta`, or a
bare `message.part.delta`, for `text` parts (finished by the part's
`time.end` or `session.idle`); headless Claude runs with
`--include-partial-messages` and streams each `text_delta` content
block, skipping a subagent's lines. The flag adds `stream_event` lines
to stdout only: the session's JSONL transcript and `--resume` behave as
before, and a claude that refuses the flag gets the turn rerun without
it. Reasoning is not streamed, since its text often never reaches the
transcript to replace the bubble. ACP streams `agent_message_chunk`
text as `assistant`, `agent_thought_chunk` as `thinking` and each
`tool_call`'s title as `tool_use` (finished when the call completes or
fails); a chunk of another kind or a tool call ends the message before
it, and `session/load`'s replayed history never streams. Clients keep
the `thinking` and `tool_use` bubbles for matching but draw only
`assistant`.

Text is redacted over the whole message so far, not per chunk; the
unfinished word, a line that looks like a credential, and an open
private-key block wait for more text, and a message whose sent text a
later redaction would rewrite stops streaming. Each message keeps its
cleaned text up to a line break that no redaction or escape can reach
across (no open OSC escape or key block, and no secret's name in the
last few words before it) and re-cleans only the text after it, so a
long reply costs linear work; the `done` flush cleans the whole text.
Past 8 KB of text with no such break, each flush waits proportionally
longer, which keeps that case linear too. A message with no new text
for 15 minutes stops streaming and frees its text; its empty entry,
which keeps a late chunk from streaming without what came before it,
goes after a day. The hub publishes deltas
to the session's SSE subscribers only (not `/v1/events`) and never
journals them; the transcript's `message.appended` stays the record. A
satellite coalesces its own deltas and posts them best-effort on a
separate uplink to `/v1/agent/events`, never through its outbox, and
only when the hub's register reply says `message_delta: true` (an older
hub would journal them). tmux sessions stay record-by-record.

## Protocol — `protocol.py`

One schema for phone↔hub and satellite↔hub (satellites forward the same
events upstream and receive command envelopes downstream):

```
GET  /v1/status                      reachability check {ok, v, protocols, features, boot_id}; no credential needed
GET  /v1/events                      SSE stream of every session's events; Last-Event-ID or ?last=seq resumes
GET  /v1/sessions                    catalog: SessionRow shape + machine + ownership
GET  /v1/sessions/{id}/events        SSE stream; ?last=seq replays journal. ?v=2&last=seq&epoch=E is the v2 stream (Sync v2 below)
GET  /v1/sessions/{id}/messages      journal backfill {session, messages, next_before, has_more}; ?before=seq&limit=N (default 500, at most 2000); ?v=2 pages the timeline as v2 events
GET  /v1/sessions/{id}/snapshot      v2: everything needed to draw the session from nothing (Sync v2 below)
GET  /v1/sessions/{id}/commands      the "/" menu: commands this session's harness will run
POST /v1/sessions/{id}/message       {text, mode: queue|steer, attachments[]}
GET  /v1/sessions/{id}/queue         queue-mode sends still waiting for idle
POST /v1/sessions/{id}/queue         {client_id, op: steer|cancel|edit, text?, command_id?} on one waiting send
POST /v1/sessions/{id}/attachments   multipart upload → staged file, returns ref
POST /v1/sessions/{id}/approval      {request_id, decision}
POST /v1/sessions/{id}/pin           {pinned: bool} → pin or unpin on the hub
POST /v1/sessions/{id}/title         {title: string} → display name, "" clears it
POST /v1/sessions/{id}/control       interrupt|compact|clear|model|effort|attach|kill|detach {mode: now|when_idle|keep}; attach also takes {mode: auto|tmux|headless|serve|appserver|acp, model?, effort?}; {value?} for model|effort; {client_id?}, echoed on a satellite's command.result
POST /v1/sessions                    new session {machine, harness, cwd, prompt, model?, effort?, yolo?} → launch_id
POST /v1/sessions/{id}/fork          {prompt?, model?, effort?} new session from this one's history → launch_id
GET  /v1/issues                      every pinned repo's issues, each row plus `mine` (`?state=open|closed|all`, open default)
GET  /v1/issues/<o>/<r>/<n>          one issue in full: row + body, comments, branch, worktree, suggested_prompt
POST /v1/issues/resolve              {repo, number, harness, prompt?, context?, model?, effort?} worktree + agent for a GitHub issue → launch_id
POST /v1/issues/comment              {repo, number, body} post a comment on a pinned repo's issue as the hub's gh login
GET  /v1/pulls                       every pinned repo's open PRs, each row plus `mine` and `review_requested`
GET  /v1/pulls/<o>/<r>/<n>           one PR in full: row + body, per-check runs, reviews, worktree, suggested_prompt
POST /v1/pulls/<o>/<r>/<n>/open      {harness, prompt?, model?, effort?} detached PR-head worktree + agent → launch_id
GET  /v1/tasks                       the task store over the sources' open items: state, sessions, agent_running
POST /v1/sessions/{id}/task          {task_ref} link a work item to this session
DELETE /v1/sessions/{id}/task        {task_ref}? unlink one task, or all without a ref
GET  /v1/folders?path=                subfolders of one folder on the hub machine (new-session browser)
GET  /v1/sessions/{id}/files?q=     paths under the session's folder for the prompt box's @ menu
GET  /v1/sessions/{id}/preview?path= one .html/.htm/.svg file from the session's folder, for Preview
GET  /v1/launches/{launch_id}        pending|started|failed|untracked; started carries the session key
GET  /v1/machines · /v1/usage · /v1/search?q=
GET  /v1/inbox                       pending decisions; ?done=1 adds finished-today
GET  /v1/app/overview                the web app's Overview (docs/web-app.md)
GET  /v1/app/sessions                the web app's Sessions: catalog rows + usage
GET  /v1/app/harnesses               the web app's Harnesses: swe list --scope=all rows
GET  /v1/app/<page> · POST /v1/app/<page>/<action>   every other web app page (webapp/pages/<page>.py), see docs/web-app/<page>.md; POST also passes the Origin check
GET  /                               the web app's page; its assets are flat names beside it (/app.js); /app/… 301s to the same name here, query and fragment intact
POST /v1/inbox/dismiss               {key, last_active_at} → cleared for this device
POST /v1/sessions/{id}/read          {at?} → this device's last-read cursor; rows carry unread
POST /v1/pair                        QR payload → device credential; optional {device:{kind,model,client,os}} metadata
POST /v1/pair/request                a device asks to be paired; identified by tailscale whois, no credential needed → {request_id, poll_secret, code, ...}
GET  /v1/pair/request/{id}           the requester's poll (secret in X-Pair-Secret): pending|denied|approved (approved answers once with the credential)
GET  /v1/pair/requests               pending pair requests; a paired bearer or the hub owner's tailnet login
POST /v1/pair/requests/approve|deny  {code, request_id?}; the owner's yes or no
GET  /v1/devices                     every paired device (device_view rows + paired_via + this_device) and the pending pair requests, for the pair-requests-and-devices screen
POST /v1/devices/revoke              {device_id, confirm_this_device?} sign one device out → {revoked, this_device, devices}
GET  /v1/devices/me                  this device's row: kind, model, client, os, node_name, display_name, timestamps
POST /v1/devices/rename              {name} → this device's display_name; "" clears it
GET  /v1/ssh                         SSH console setup: user, port, listening, host_keys, this device's key
POST /v1/ssh/key                     {public_key} ("" clears) → stored on the device, authorized_keys synced

# satellite credentials only, each bound to one machine id
POST /v1/agent/register              {machine} → binds the credential to machine.id (never the hub's own)
GET  /v1/agent/commands?machine=     SSE stream of commands queued for that machine
POST /v1/agent/events                {machine_id, events[]} the satellite's transcript events
POST /v1/agent/catalog               {machine_id, rows[], slash?} the satellite's session rows
POST /v1/agent/usage                 {machine_id, harnesses, window_days, ...} its rows for /v1/usage
GET  /v1/agent/attachments/{key}/{ref}  pull a file the hub staged for one of the machine's sessions
```

Events: `message.appended`, `message.delta` (tier A only),
`session.status`, `session.meta` (title, harness_title, pinned,
pinned_at after a pin or rename), `approval.requested|resolved`,
`machine.online|offline`, `notify` (needs_you|finished|error|limit;
`github` for a notification thread that just turned unread, and
`service` for a model provider or agent platform starting or ending an
incident, both hub-local, not satellite kinds). A compaction has no event of its own: it is a
`message.appended` with `kind: "compaction"`. The hub still accepts a
bare `compaction` event from an older satellite.

`limit` comes from the transcript, not the status probe. The tailer
marks Claude Code's 429 record (`isApiErrorMessage` with `error:
"rate_limit"`, "You've hit your session limit · resets 9:30am") and a
Codex `task_complete` whose `error.codex_error_info` is
`usage_limit_exceeded`; the record still shows in the chat as an
`error` message. Its notify adds `message` (the harness's line, at most
280 characters) and, for Claude Code, `resets_at` (epoch seconds) and
`window` (`five_hour`, `seven_day`, ...) from `quotaLimits`. It rings
once per limit: a record read more than 10 minutes after it was written
(backfill) is quiet, and the same limit again in that session (a retry
into it) stays quiet until its reset time, or for an hour when the
harness gave none. The ntfy push puts the harness's line in the body.
Other harnesses don't raise `limit` yet.

`queue|steer` is the OpenAI Remote distinction: queue waits for idle;
steer injects mid-turn where the channel allows (codex turn input, tmux
send-keys), else degrades to queue with a UI hint. `SessionRow.to_json`
is reused verbatim so phone and macOS app share semantics —
`docs/json-contracts.md` gains a section rather than a parallel dialect.

Pins and names are kept on the hub in `session_meta.json`, keyed by
session key, so the phone, the web app and satellite sessions agree.
Each session row carries `pinned`, `pinned_at` (epoch seconds or
`null`; sort the Pinned section by it), `title` (the name set with
`POST /v1/sessions/{id}/title`, else the harness's own) and
`harness_title` (always the harness's own). Both routes answer those
fields and emit `session.meta` so open clients update.

The task store (`tasks.py`, `tasks.json` on the hub) is the "what am I
working on" half of remote: a normalized work item, a `task_ref` —
`{source: github|linear|notion, kind: issue|pull|card|page,
repo?|project?, ref, title, url}` — mapped to the sessions and launches
the hub ran for it. Links land three ways: a resolve or a PR open
stamps `origin` on its launch record — the launch links at once, so
the task reads `agent_running` while it prepares, and adoption adds
the session; `POST /v1/sessions/{id}/task` links by hand (a GitHub
task the store doesn't hold yet must be in a pinned repo, `422
not_pinned` otherwise, since every stored task is read with `gh`; a
task already stored keeps its links and refreshes after its repo is
unpinned); `DELETE` unlinks one task (or every task, without a
`task_ref`). `GET
/v1/tasks` merges the store over what the sources list — GitHub, the
pinned repos' open issues and PRs behind the same 60-second `gh` cache
`/v1/pulls` uses, and Linear (`remote/linear.py`), the issues assigned
to the key's owner plus open issues in configured teams, behind the
same minute cache — so a row answers "what's happening on X": the
derived `state` (`open` → `in_progress` → `pr_open` → `merged|closed`),
`sessions`, `agent_running` (a linked session is active, or its launch
is still pending), `last_active_at`, and `pr` — for an issue the PR its
`swe/issue-<n>` branch (or a pre-rename `quiver/issue-<n>`) produced,
so a merged PR reads `merged` even
after it leaves the open list. A stored task no source can read this
round (the source is blocked or down, or the task is past the 24 most
recently active and off the open list) keeps the state its source last
reported, from `cache/task_states.json`, and says so with
`state_source: cached`; that cache is written only when a fresh read
changes a task's state, and `tasks.json` itself stays links only.
`/v1/pulls` rows carry `agent_running` from the same store, and `GET
/v1/launches/{id}` shows a launch's `origin` while pending and after it
settles.

The Linear source is opt-in through a `linear` section in
`~/.swe/config/config.json`:

```json
"linear": {"key_ref": "${LINEAR_API_KEY}", "teams": ["QUI"]}
```

`key_ref` names a `${NAME}` in the MCP secrets store
(`~/.swe/secrets/mcp/.api_keys` — `printf %s "$KEY" | swe mcp secret
set LINEAR_API_KEY` writes it); a literal key is refused because
`config.json` stays credential-free. It defaults to
`${LINEAR_API_KEY}`, and `teams` to none, so a bare `"linear": {}`
reads the personal API key owner's assigned issues (`swe config set
linear.teams '["QUI"]'` widens to those teams' open issues). Each
issue becomes `{source: "linear", kind: "issue", project: <team key>,
ref: "QUI-123", title, url}`, and its workflow state's type maps to the
task state: `completed`/`canceled`/`duplicate` → `closed`, `started` →
`in_progress`, everything else → `open`. The answer's `linear` block
carries the source's env: `disabled` (no section), `ok`, `auth-required`
(the ref won't resolve or the key was refused — `error` says which and
stored Linear tasks keep their last-known state, the way `env_blocked`
does for GitHub), `bad-config` (a malformed setting, like a non-list
`teams`) or `upstream-error`. Reads only: launching never moves
a Linear issue's workflow state — states are per-team there, so
`issueUpdate` write-back is a later step.

The Notion source (`remote/notion.py`) is opt-in the same way, through
a `notion` section. Notion databases have whatever columns the
workspace gave them, so the section names each database and which of
its properties play which role:

```json
"notion": {
  "token_ref": "${NOTION_TOKEN}",
  "user": "you@example.com",
  "databases": [
    {"id": "https://www.notion.so/acme/Roadmap-0123456789abcdef0123456789abcdef",
     "name": "Roadmap",
     "properties": {"status": "Status", "owner": "Owner",
                    "title": "Name", "url": "Link"}}
  ]
}
```

- `token_ref` names an internal integration token (`ntn_…`) in the
  secrets store, as Linear's `key_ref` does; it defaults to
  `${NOTION_TOKEN}` (`printf %s "$TOKEN" | swe mcp secret set
  NOTION_TOKEN`). Create the integration at notion.so/profile/integrations
  with "Read content", then share each database with it (the
  database's ••• menu → Connections).
- `user` is whose rows list: a Notion user id, or the email you sign in
  with. An email is looked up in `/v1/users`, which needs the
  integration's "Read user information including email addresses"
  capability.
- `databases[].id` takes a database id, dashed or not, or the URL
  copied from the browser. `name` is the task's `project` (the
  database's own title when left out; set it to keep task ids stable
  if the database gets renamed).
- `properties.status` (a status, select or checkbox property) and
  `properties.owner` (a person, created-by or last-edited-by property)
  are required. `title` defaults to the database's title property and
  `url` (a url or text property) to the page's own Notion link. A
  `properties` object beside `databases` sets defaults each database
  can override.

Each database is read with `POST /v1/databases/<id>/query`, filtered to
rows whose owner holds the user, newest edits first, 100 rows a page,
at most 5 pages and 50 open rows per database; a database that had more
rows than those caps read sets `truncated: true` on the `notion` block.
Rows whose status reads done are left off the open list. Each row becomes `{source: "notion",
kind: "page", project: <name>, ref: <page id>, title, url}`; its
`status` is the status or select option's own name (a ticked checkbox
reads as the property's name), and `state` is a best guess: a status
property's group (To-do → `open`, In progress → `in_progress`,
Complete → `closed`), a select option's words ("Done", "Shipped",
"Won't do" → `closed`; "In progress", "Review" → `in_progress`;
anything else → `open`), a ticked checkbox → `closed`. An archived page
reads `closed`. A stored page refreshes through `GET /v1/pages/<id>`
alone; nothing reads a page's body.

The answer's `notion` block reads like `linear`'s: `disabled`, `ok`
(`login` is the user's name), `auth-required` (the token won't resolve
or was refused), `bad-config` or `upstream-error`. `bad-config` names
the fault: a missing `user` or `databases`, an email Notion doesn't
know, a property the config needs but doesn't name, or one the
database doesn't have (`Notion database "Roadmap" has no property
"State" for status (it has: …)`). A database that fails says so in
`error` while the other databases' rows still list. Every call sends
`Notion-Version: 2022-06-28` (`notion.NOTION_VERSION`), and a 429 waits
out its `Retry-After` (at most 5 seconds, twice) before the source
reports `upstream-error`. Reads only: nothing writes the status
property back yet.

The phone shows queue-mode sends that are still waiting in a tray above
the prompt box. `GET /v1/sessions/{id}/queue` lists them, oldest first:
`{sends: [{client_id, text, attachments, queued_at}]}` (attachment refs
as sent, `queued_at` in epoch seconds). Only sends that carried a
`client_id` are listed, and a send leaves the list the moment its job
starts handing it to the injector. The list lives in the hub's memory
(`sendq.Waiting`), so it survives the app closing but not a hub restart,
which fails every waiting send with "hub restarted; message not sent".
While a send waits, a second send under the same `client_id` is `409
duplicate_client_id`.

`POST /v1/sessions/{id}/queue {client_id, op}` acts on one waiting send.
`steer` stops its wait and submits it as a steer-mode send, behind the
steers already queued for that session; it then degrades and reports
exactly like a steer message, and needs the session attached (`409
not_attached`). `cancel` drops it and emits its `command.result` with
`ok: false`, `note: "removed before sending"` and `cancelled: true`.
Both answer `200 {ok, client_id, op}`. Either frees the send's slot in the
session's 20-send backlog at once. A send leaves the waiting state
exactly once, under a lock, and whoever moves it reports it: the job
(to inject, or to fail it on timeout or restart) or this route. So a
cancel racing the job's start either wins, and nothing is sent, or
loses with `409 not_waiting` ("that message already went out"); there is
never both a send and a removal. An unknown `client_id` is `404
unknown_send`; one that already left (sent, steered, removed, failed) is
`409 not_waiting` while the hub remembers it (the last 256). A
satellite session's queue-mode send waits on the satellite, not the hub:
`swe remote agent` runs `message` commands through the same per-session
`SendQueue`, polls the session's status every 2 s and delivers once the
turn ends, failing it after 30 min of busy with "session stayed busy for
30 min; message not sent". Steers go on their own queue and never wait
behind it. A send the agent is stopped before delivering is not marked
done, so the hub replays it when the agent comes back. The hub holds
nothing for those sends, so their list is empty and every op answers
`409 queue_unsupported`. The `202` for such a send keeps `waiting:
false`, so a client never parks it in a tray whose actions would act on
the hub's queue.

`edit` takes a `text` (checked like a message's: a string with more
than spaces in it, else `422 bad_command`) and replaces the waiting
send's text in place, keeping its place in the queue; it answers `200
{ok, client_id, op, text}`. The send's job reads its command only after
claiming it under the same lock, so an edit racing the injection either
lands first, and the new text goes out, or answers `409 not_waiting`
and the old text goes out. A `200` edit is never followed by the old
text. Attachments stay as they were.

`POST /v1/sessions/{id}/control {op: "detach", mode}` hands an attached
session back to `observed`. The mode says what happens to the harness:

| `mode` | Answer | What happens |
|---|---|---|
| `now` (the default, also when `mode` is missing) | `200` | The tmux session is closed, and the harness with it, even mid-turn. The transcript stays, so a later attach resumes it. Tier-A channels stop their headless run, `opencode serve` or `codex app-server`. |
| `when_idle` | `202 {queued: true, mode, note}` | The hub polls the session's status every 2 s and detaches (as `now`) once it is no longer `active`. The outcome arrives as a `command.result` with `op: "detach"`, `mode: "when_idle"`, then the usual `session.ownership`. A second `when_idle` while one waits answers `202` with "already waiting". |
| `keep` | `200` | The hub stops supervising but leaves the tmux session running, so `tmux attach -t swe-…` on the Mac still works. Only tmux-mode sessions can be kept (`409 control_failed` otherwise). Staged attachments stay, since the harness may still read them. |

A `when_idle` detach is cancelled by a later attach (which answers
`200` "already attached; the pending detach is cancelled"), by a `now`
or `keep` detach, or by `kill`; each emits its `command.result` with
`ok: false` and `cancelled: true`. The wait is capped at 30 minutes,
like a queue-mode send: past it the result is `ok: false`, "session
stayed busy for 30 min; it is still attached". It lives in the hub's
memory (`detach.PendingDetaches`), so a hub restart reports it as not
done and the session stays attached. The waiter and a cancel move the
pending detach out of its waiting state under one lock, so exactly one
of them acts; a cancel that arrives while the detach is already
running waits for it to finish.

A kept session is recorded in `data/remote/kept.json` with its tmux
name, the pane's pid and `kept_at`. Attaching it again adopts that tmux
session instead of launching a second run, when the tmux session still
exists and its pane still runs the same process (same pane pid, same
harness). Adoption starts nothing, so it is allowed mid-turn — but a
`model`/`effort` pick only holds when it is the one the run was
launched with, since `/model` can't safely be typed into a live,
possibly-watched pane; a different value is refused (`409`). A kept
tmux session gone since is forgotten and attach launches as usual; one
whose pane now runs a different process is refused, since a launch
would reuse the name and bind to that pane. The hub's catalog loop (and
each satellite's) closes a kept tmux session once no tmux client is
attached to it and neither the keep nor its transcript has moved for
`supervise.KEPT_IDLE_REAP_S` (12 hours). It forgets a record only once
that tmux session is confirmed gone: a failed kill is retried on the
next loop, and a record whose pane now runs another process stays (and
is never killed), so attach keeps refusing it. `launch_tmux` never binds
to a live tmux session it did not just start. Both moves between the
registries (keep, adopt) write the destination before removing the
source and fail the request on a write error; a key left in both by a
crash is settled at start-up, and before each keep, adopt and reap, in
favour of the supervised row.

Satellites take the same modes: the hub forwards `mode` in the control
command, and the satellite runs the wait, the keep, the adoption and
the reaping against its own tmux and statuses. The forwarded command
answers `202` as always; a `when_idle` detach later reports its own
`command.result`, and the ownership change reaches the hub with the
next catalog push.

`POST /v1/sessions` starts a harness under tmux (`supervise.launch_new`;
over ACP for an ACP-speaking harness, see the ACP section) on the hub, or queues a `launch` command for the satellite named in
`machine` (empty means the hub). The harnesses a machine can start come
from `catalog.launchable_harnesses`, never a hardcoded list: every
harness `swe list` shows there (not archived) whose registry `command`
is on PATH, minus the ones switched off in the Mac app's Settings ›
Provider visibility, in that setting's order (read from the app's
preferences, `providerOrder` and `providerDisabled`; a machine without
the app keeps registry order). The launch runs the registry command, so
`cursor` starts `cursor-agent`. Providers with no CLI, like Antigravity,
can't start. A harness swe has no session parser for (qwen-code) still
starts, but nothing can ever name its session: the machine row lists it
in `untracked_harnesses`, and its launch settles `untracked` with a note
instead of staying pending. Adoption knows a new session by its JSONL transcript's birth
time; harnesses that keep sessions in a database (opencode, kilo, devin,
cline) are matched by the session's own activity time instead, with every
session that existed at launch excluded (the catalog's rows plus a fresh
run of that harness's parser, since the catalog can be a minute old). `cwd` must be absolute and,
on the hub, an existing folder
(a satellite checks its own disk and fails the command otherwise);
`prompt` is required, since a harness writes no transcript until its
first message, and is typed into the pane once the TUI is up. Each
machine row in `/v1/machines` carries `launchable` (in order),
`harness_names`, `model_at_launch` and `effort_at_launch` (the
harnesses whose launches accept `model`/`effort`, per the launch-tuning
rules below), `recent_models` (each launchable harness's recently used
models, newest first, at most 5; `{}` on a machine with no history) and
`last_models` (the head of each `recent_models` list, the picker's
default). Both come from the web ledger (`remote/recent_models.py`),
cached behind the ledger file's signature and read incrementally past
the last folded `rowid`, so a refresh never rescans history. Names a
launch could never take back are dropped: anything outside the `model`
token shape (Claude's `<synthetic>`, Cursor's "(Auto …)" labels),
`unknown`, Devin's internal `compactor`/`summarizer` and Cursor's
`default`. An OpenCode name the ledger kept without its provider is
named `provider/model` from `opencode.db`'s `session_v2.model` JSON
(`{id, providerID}`), the form its launch needs, and dropped when no
provider is recorded anywhere.
The hub refuses a launch outside `launchable`
(`422 not_launchable`). Errors: `422 bad_command`
(body), `422 bad_cwd` (no such folder on the hub), `422 launch_failed`
(tmux missing), `409 unsupported_launch_option` (the harness's launch
can't honour `model`/`effort`), `429 backlog_full` (satellite too far
behind).

Optional `model` and `effort` ride every launch route — `POST
/v1/sessions`, `POST /v1/issues/resolve`, `POST
/v1/pulls/<o>/<r>/<n>/open`, `POST /v1/sessions/{id}/fork` — and the
`attach` control op (`{op: "attach", mode, model?, effort?}`, CLI
`--model`/`--effort`). Each is one short token —
`[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}`, the shape a `/model <v>`
command line takes — and a bad shape is `422 bad_command` (the same
parse error a bad `prompt` gets); absent or `""` means "no pick".

Application is per channel, and a pick a channel can't honour is
refused outright — `409 unsupported_launch_option` — never silently
dropped (the same honesty `unsupported_control` gives a live control
op):

| Channel | `model` | `effort` |
|---|---|---|
| fresh launch (`POST /v1/sessions`, issue resolve, PR open) | On the harness's command line, as its `capabilities.model` in harness.json declares (table below; `launch_model.py`) | Typed as `/effort <v>` once the TUI is up, before the first prompt (claude only) |
| tmux pane of a fork or a `mode: tmux` attach | Typed as `/model <v>` once the TUI is up, before the first prompt — only where the harness has a verified `/model <arg>` builtin (claude and codex today; `slash_builtins.launch_op_ok`) | Same rule — claude's `/effort <v>`; nothing else has a verified one |
| headless (`mode: headless`, claude only) | `--model <v>` on every `claude -p --resume` invocation, read off the session's supervised meta | Refused — print mode has no effort flag |
| `serve` (opencode attach) | `prompt_async`'s `model` object — the value must be `provider/model` (split into `{providerID, modelID}`), anything else is refused | `prompt_async`'s `variant` |
| `appserver` (codex attach) | `turn/start`'s `model` override (holds for the turn and the ones after) | `turn/start`'s `effort` override — a name codex doesn't know fails the turn loudly, not silently |

A fresh launch reads `capabilities.model` from the machine's own
harness.json, over `launch_model.MODEL_FALLBACK` for a harness that
declares none (`supported: false` switches it off). Its fields are
`flag` (the argv flag that takes the model), `subcommand` (tokens put
right after the binary, only when a model is picked), `settings_flag`
plus `settings_key` (the model goes into a per-launch JSON file under
`data/remote/launch-settings/`, nested at that dotted key, passed as
`<settings_flag> <file>`) and `provider` (the value must be
`provider/model`). The fallback, each flag tried on 2026-10-27:

| Harness | Command line with a pick |
|---|---|
| claude | `claude --model <v>` |
| codex | `codex -m <v>` |
| cursor | `cursor-agent --model <v>` |
| pi | `pi --model <v>` |
| cline | `cline -m <v>` |
| devin | `devin --model <v>` |
| droid (0.219: no interactive `-m`) | `droid --settings <file>`, the file `{"sessionDefaultSettings": {"model": "<v>"}}` |
| opencode (v2: the full TUI has no model flag) | `opencode mini -m <provider/model>`; plain `opencode` with no pick, and a bare name is refused |

Any other harness gets `409 unsupported_launch_option` for a `model`.
The value is already one token with no leading `-` (the shape check
above), so it can't smuggle a flag of its own into the argv.

`POST /v1/sessions` also takes `yolo: true` — the sheets' Autonomy
checkbox, on by default — which appends the harness's approval-bypass
argv so the session never waits on a permission prompt. The tokens are
declared per machine as `capabilities.yolo` in harness.json (`argv`,
with `supported: false` switching it off), over
`launch_yolo.YOLO_FALLBACK` for a harness that declares none. The
fallback, each flag read off the harness's own `--help` on 2026-10-31:

| Harness | Command line with `yolo` |
|---|---|
| claude | `claude --dangerously-skip-permissions` |
| codex | `codex --dangerously-bypass-approvals-and-sandbox` |
| cursor | `cursor-agent --yolo` |
| devin | `devin --permission-mode dangerous` |
| opencode | `opencode --auto` |
| droid | `droid --auto high` |
| cline | `cline --yolo` |
| kilo | `kilo --auto` |
| qwen-code | `qwen --yolo` |

Any other harness gets `409 unsupported_launch_option` for `yolo`, and
a machine row names the harnesses that take it in `yolo_at_launch` —
the same gate `model_at_launch`/`effort_at_launch` give a pick, so a
satellite too old to apply it is refused before anything is queued,
never launched with checks silently on.

`POST /v1/sessions` also takes `provider: "<slug>"` (#526): start the
harness on a provider whose key is in the launching machine's own keys
file (`~/.swe/secrets/providers/.api_keys`). How each harness takes a
provider is declared as `capabilities.provider` in harness.json over
`launch_provider.PROVIDER_FALLBACK`: OpenCode and Pi read the provider's
own key variable (`GROQ_API_KEY`), Claude Code reads `ANTHROPIC_API_KEY`
or, for an Anthropic-compatible endpoint (DeepSeek, Kimi, MiniMax,
OpenRouter), `ANTHROPIC_BASE_URL` plus `ANTHROPIC_AUTH_TOKEN` with
`ANTHROPIC_API_KEY` blanked. Claude Code also takes every coding plan in
[coding-plans.md](coding-plans.md) as a provider, one slug per plan
region, each reading only the plan's own key variable.

How the key reaches the harness without leaking:

- The pane runs `python3 -I .../remote/provider_exec.py --keys=<file>
  --spec=<names and base URL> -- <harness argv>`. That process reads the
  keys file itself, sets only the provider's variables, drops every
  other variable the keys file names, and execs the harness under the
  same pid. No key is on any argv, in the tmux command, the pending
  marker, the supervised entry, the launch record or the journal; they
  carry the slug.
- With no provider picked nothing changes: no key is injected.
- The hub never sends a key to a satellite. It checks the slug against
  the satellite's `providers_at_launch` row (slugs whose key is set
  there) and queues only the slug; the satellite reads its own keys
  file. A satellite whose row lacks the field gets `409
  unsupported_launch_option`.
- A missing key, or a slug the harness doesn't declare, is `422
  unsupported_provider`, worded with variable names only.

From a terminal, `swe use --provider=<slug> <harness> [args]` does the
same with this machine's keys file.

On the two API channels the pick rides the session's supervised meta
into every send — the same plumbing headless uses for `--model` — so it
keeps applying until the session is detached. The pick also rides
hub→satellite launch and fork command envelopes and attach commands
the same way (`{model, effort}` in the command data). The hub only
forwards a nonempty pick when the satellite's machine row advertises
the build that reads it — `model_at_launch` present as a list, the
field that release started reporting; an older agent would accept the
command and silently drop the fields, so it gets `409
unsupported_launch_option` instead. A launch's pick must also be one
that satellite advertises (the harness listed in its row's
`model_at_launch`/`effort_at_launch`, which its own registry decides),
else `409 unsupported_launch_option` before anything is queued (forks
still check the hub's `/model` table). Once the command
lands the satellite re-checks its own registry and fails it with the
same note, so the pick is applied or refused, never dropped. It also rides the
launch's pending marker and supervised-entry meta (as bookkeeping —
a session's *current* model is a transcript/ledger question), and the
launch record (`GET /v1/launches/{id}` carries `model`/`effort`).

Each session row says which model the session runs now: `model` is the
model of the newest reply in its transcript, left out when unknown
(`remote/session_model.py`). A switch with no reply since does not show
until the next reply lands. Claude Code, Codex and Pi are read from the
transcript's tail, backward, parsing only lines that can name a model,
and cached per file under its stat signature in
`cache/session_model_cache.json`, so an unchanged transcript costs one
`stat` per refresh. OpenCode, Kilo and Devin ask their database for the
session's newest assistant row, cached in memory until the session's
activity time moves. The names follow the ledger's rules (OpenCode's
`provider/model`), and the junk `recent_models` drops (`<synthetic>`,
`unknown`, Devin's `compactor`) is skipped for the reply before it. The
web ledger is not the source because it is scanned only when a web page
asks, so it can trail the transcript by days on a hub nobody browses.
`model_modes` lists the supervised modes in which the `model` control op
switches that harness: `tmux` where its TUI has a verified
`/model <name>` builtin (claude and codex), plus `headless` for claude,
`appserver` for codex and `serve` for opencode.
On an API channel the op sends nothing: it records the value on the
session's supervised row (`inject.switch_model`), the same field an
attach pick lands in, so it applies from the next turn (headless claude:
`--model` on the next `claude -p --resume`; codex: the next
`turn/start`'s `model` override; opencode: the next `prompt_async`'s
`{providerID, modelID}`, split at the first slash, so a bare name is
refused). The value is held to the launch shape
and the channel's attach rule (`channel_tuning_error`), and a detached
session refuses it. A client offers a model switch only on an attached row whose
`mode` is in the list.

A pick can only change what a fresh run starts with, so two attach
shapes refuse a changed value rather than dropping it: attaching a
session that is already attached or kept running (`409 not_idle` —
detach first, then attach again), each comparing the request against
the pick the run was launched with. The same value, or none, attaches
as before.

The answer is `202 {launch_id, state: "pending", note}` (plus
`queued: true` for a satellite). On the hub it comes before the pane is
primed, which can take 30 s; a launch that fails after that reads
`failed` at the route below — including a pane that came up but never
took its launch controls or prompt, since no turn will ever start on
it (the pane is left up for a manual attach). The harness picks its own session id,
so the key is not known yet: the launch id rides the pending marker into
the supervised entry when `adopt_pending` claims the new transcript, and
the adopting machine emits `session.launched {launch_id}` naming the
new session (the hub puts it on the global stream: an event in the
session's own journal before its tailer starts would make the tailer
skip the transcript as history already sent). `GET /v1/launches/{launch_id}` then answers
`{state: "started", key}`, only once the catalog has that row, so the
phone can open the chat straight away; a satellite's failed `launch`
command (`command.result` echoing `launch_id`) turns it `failed` with the
note. The hub keeps launches in memory for a day.

Adoption knows a new session only by its harness, folder and creation
time, so two launches of one harness waiting in one folder would each
see both transcripts and neither would be adopted. One new session per
folder starts at a time: while a launch there is still waiting (its
marker is under a minute old or its pane is alive), the next launch of
that harness answers `409 launch_pending`, naming the waiting pane. The
marker is written under the registry lock before the pane starts, as a
fork's is, so two requests at once can't both pass; a satellite runs the
same check and fails the launch with the same note. A harness swe can't
read is never adopted, so its launches skip the check.

`POST /v1/sessions/{id}/fork {prompt?, model?, effort?}` (`fork.py`)
starts a new run of
the session's harness that continues from its history, in the session's
folder under tmux, and leaves the original as it was. The source can be
attached or only watched; the fork never writes to it, and attaching the
source later never stops a fork (`_local_writer_pids` skips a process
carrying a fork flag, unless it names the attached session as its own
output with `--session-id`). The answer is the same `202 {launch_id, state:
"pending", note}` as a new-session launch, followed through `GET
/v1/launches/{launch_id}`. `prompt` is optional and typed in once the TUI
is up; with none, the pane is still booted past the trust prompt, and the
launch stays pending until the harness writes the fork's transcript.
Session rows carry `can_fork`, so the app offers Fork only where it
works. Errors: `404 unknown_session`, `409 fork_unsupported` (the harness
has no fork command line, or its session has no id to pass), `422
bad_cwd` (the session's folder is gone from the hub), `409 fork_pending`
(see below), `422 not_installed`,
`422 launch_failed` (tmux missing), `422 bad_command`,
`409 unsupported_launch_option` (`model`/`effort` the harness's launch
can't honour), `429 backlog_full`.

Each harness's fork command line, read off the installed binary's
`--help` on 2026-10-24:

| Harness | Version checked | Command line |
|---|---|---|
| Claude Code | 2.1.308 | `claude --resume <id> --fork-session --session-id <new uuid>` |
| Codex | 0.163.0 | `codex fork <thread uuid>` |
| Kilo | 7.8.15 | `kilo --session <id> --fork` |
| Pi | 1.3.0 | `pi --fork <id>` |
| Droid | 0.219.0 | `droid --fork <id>` |

Everything else answers `409 fork_unsupported`. opencode 2.1.3 has
`--fork` only on the one-shot `opencode run`, not the TUI; devin,
cursor-agent and cline have no fork option.

Adoption must claim the fork, never its source. Claude Code takes the
new session's id on the command line, so the pending marker carries it
as `expect_id` and adoption waits for exactly that id. The other
harnesses pick their own id: the marker excludes the source's id along
with every same-folder session that existed at the fork, and `fork_of`
also keeps out a codex resume of the source (a new rollout file under
the same thread uuid). That leaves only the folder and creation time, so
two such forks waiting in one folder could take each other's
transcripts: a fork of these harnesses is refused with `409
fork_pending` while another launch of that harness is still waiting for
adoption in the folder (its marker is under a minute old or its pane is
alive). The marker is written under the registry lock before the pane
starts, so two requests at once can't both pass. An id a Claude fork
named is never claimed by any other pending launch. A satellite's session gets a `fork` command in
its journal, like `launch`; the satellite runs it on its own thread, so
booting the pane never delays the commands behind it. It checks its own
folder, its
failed `command.result` (echoing `launch_id`) turns the launch `failed`,
and its `session.launched` names the new key. The hub refuses a
satellite row without `can_fork`, which covers satellites too old to
send the field or run the op.

`GET /v1/issues` (`issues.py`) lists the open issues of every repo
pinned on the hub: `{repos: [{name, status, issues, error?}], machine,
machine_id, github, env_blocked?, fetched_at}`, the pulls shape. Each
issue is the `swe gh issue --json` row — `number`, `title`, `state`,
`author`, `labels`, `assignees`, `comments` (the count), `updated_at`,
`url` — plus `mine`, true when the row's author is the hub's `gh` login,
so the phone filters All/Mine without a second round trip. A row may
also carry `agent_running` when the hub knows a resolve is already on
the issue; clients show a chip only on `true` and treat its absence as
none. `?state=closed` or `?state=all` answers the `gh issue list
--state` of the same name — the Closed chip reads it, and it caches
apart from the open slot (`tasks.py` shares that one); any other word is
a `422 bad_state`. One repo's failure stays in its `status`/`error`
while the others answer; a missing or signed-out `gh` answers
`env_blocked` before any fetch. The reads share the web app's 60-second
`gh` cache (`github/shared.py`), and a `refresh=1` bypasses it.

`GET /v1/issues/<owner>/<repo>/<n>` answers one issue in full: the row
plus `body`, `comments`, `branch` (`swe/issue-<n>`), `worktree`
(where a resolve puts the checkout) and `suggested_prompt` (the resolve
brief `github/issues.build_prompt` writes — what the resolve sheet
prefills). Anyone can write on a public issue, so the brief quotes the
issue's title, body and comments line by line (`> `) behind a note that
they are untrusted data the agent must not take instructions from, and
it keeps only comments whose `association` (gh's `authorAssociation`)
is `OWNER`, `MEMBER` or `COLLABORATOR`; the rest become one line, "N
comments from others, not included". Every client prefills this brief
rather than building its own. `400 bad_repo` for a malformed owner/repo, `422 bad_issue`
for a non-number, `404 not_pinned` for a repo the hub doesn't pin; a
`gh` failure answers `200 {status, issue: null, error}` — the row's
`status` carries `gh`'s word (`not-found`, `upstream-error`), so the
client renders it rather than a blank page. The phone's
`quiver://issue/<owner>/<repo>/<n>` deep link (a row's Copy link writes
it) opens this detail on the Issues stack.

`POST /v1/issues/resolve {repo, number, harness, prompt?, context?,
model?, effort?}`
(`issues.py`) starts an agent on a GitHub issue, on the hub machine
only. It answers what a client can fix at once with a 422: `bad_command`
for a bad body, `not_pinned` for a repo not pinned on the hub (`swe gh
repo add`), `not_launchable`, `launch_failed` (no tmux), and
`gh_unavailable` when `gh` is missing or signed out, with the message
saying which; `409 unsupported_launch_option` for `model`/`effort` the
harness's launch can't honour. A second resolve of an issue whose last one is still
starting (preparing, or its pane not yet adopted) gets `409
resolve_pending`: adoption has only the folder and start time to go on,
so two launches waiting in one checkout could claim each other's
sessions. Otherwise it answers `202 {launch_id, state: "pending",
note: "preparing worktree"}` and does the rest on a thread, like a
new-session launch. It reads the issue, then runs `swe gh issue
worktree`'s `manage.issue_worktree`: the repo's bare mirror (cloned on
first use), a fetch of its default branch, and branch
`swe/issue-<n>` checked out under
`~/.swe/worktrees/<owner>__<repo>-issue<n>`. A second resolve of the
same issue resumes in that checkout. A resolve started before the
rename cut `quiver/issue-<n>`: its checkout resumes on that branch, a
branch whose folder was deleted is checked out again under its old
name, and the prompt names whichever branch the checkout is on. The
harness then starts there under
tmux with the client's `prompt`, or the one `swe gh issue view` shows as
`suggested_prompt` when none was sent. A non-empty `context` is added as
a "Context for this run" block either way, unless the client's prompt
already holds that text.

While the launch is pending, `GET /v1/launches/{launch_id}` reads as
progress: `note` steps through "Cloning mirror…" (first run only),
"Creating worktree swe/issue-<n>…" and "Starting <harness>…", and
`cwd` becomes the worktree. A failure at any step settles it `failed`
with a readable note. The entry also carries `title`, `<owner>/<repo>#<n>
<issue title>`: when `session.launched` adopts the new session, the hub
stores that as the session's display name (as `POST
/v1/sessions/{id}/title` would) and emits `session.meta` on the global
stream only, since an event in the session's own journal before its
tailer starts would make the tailer skip the first turn.

`POST /v1/issues/comment {repo, number, body}` (`issues.py`) posts
`body` as a comment on issue `number`, with the hub machine's own `gh`
login: the web and iPhone issue detail's composer, and their Resolve
panel's "Also post as a comment" switch, which posts the context before
the resolve. It goes through the function behind `swe gh issue comment
--body-file=-` in process, so the text reaches `gh issue comment
--body-file -` on stdin and never sits in argv, where every process on
the machine can read it. The caller is gated like every other write
here: a paired phone or the owner's own tailnet device, never a
satellite credential. The route refuses before running `gh`: `400
bad_body` for unreadable JSON, `422 bad_repo` for a missing or malformed
`owner/name`, `422 bad_issue` for a number that isn't 1 to nine digits
(a JSON number or a digit string; `true` is not one), `422 bad_comment`
when `body` is not a string or is blank, `422 comment_too_long` past
GitHub's own 65,536 characters, `404 not_pinned` for a repo the hub
doesn't pin (so a client can't post anywhere else the hub's login
reaches), and from a fresh `gh auth status` `409 gh_unavailable` (no
`gh`), `gh_auth_required` (signed out) or `gh_upstream_error` (the check
itself failed or timed out, so nothing says the account is signed out). The body is trimmed first, as the Mac's composer
trims it. `gh` refusing answers `404 unknown_issue` (no such issue) or
`502 comment_failed` with `gh`'s words. Success is `200 {ok, github,
repo, number, comment: {author, body, created_at, url}, machine,
machine_id}`, and the hub drops the issue's cached detail and the
repo's issue lists, board and task rows, so the next read lists the
comment and its new count, even after a failure, since a timed-out
`gh` may still have posted.

`GET /v1/pulls` (`pulls.py`) lists the open pull requests of every repo
pinned on the hub (`swe gh repo list`): `{repos: [{name, status, pulls,
error?}], machine, machine_id, github, env_blocked?, fetched_at}`. Each
pull is the `swe gh pr --json` row — `number`, `title`, `state`,
`draft`, `author`, `branch`, `base`, `labels`, `mergeable`,
`review_decision`, `checks`, `updated_at`, `url` — plus `mine` and
`review_requested`, which `gh pr status` supplies, so the phone filters
All / Mine / Review requested without a second round trip. When `gh pr
status` fails the rows still list, with `review_requested` null rather
than a guess. One repo's failure stays in its `status`/`error` while
the others answer; a missing or signed-out `gh` answers `env_blocked`
before any fetch, the same words `swe gh` stops on. The reads share the
web app's 60-second `gh` cache (`github/shared.py`), so the
phone, the Repositories page and a `refresh=1` agree.

`GET /v1/pulls/<owner>/<repo>/<n>` answers one PR in full: the list row
plus `body`, per-check `check_runs`, `reviews`, `review_requests`,
merge state and additions/deletions, and two fields a client shows
before opening: `worktree` (where an open puts the checkout) and
`suggested_prompt` (the review brief `github/pulls.build_prompt`
writes), plus `association`, the author's `author_association` from
one REST read (`gh pr view` has no such field). Anyone can open a PR
on a public repo, and a fork's workflow names its own checks, so the
brief follows the resolve prompt's rule: it quotes the title and body
line by line (`> `) behind a note that they are untrusted data only
when `association` is `OWNER`, `MEMBER` or `COLLABORATOR`, and
otherwise says in one line that they were left out; failing check
names are always quoted. `400 bad_repo` for a malformed owner/repo, `422 bad_pr` for a
non-number, `404 not_pinned` for a repo the hub doesn't pin; a `gh`
failure answers `200 {status, pr: null, error}` — the row's `status`
carries `gh`'s word (`not-found`, `upstream-error`), so the client
renders it rather than a blank page.

`POST /v1/pulls/<o>/<r>/<n>/open {harness, prompt?, model?, effort?}`
is a resolve for
pull requests, run by the same asynchronous-launch shape: the hub
answers what a client can fix at once — `400 bad_body`, `422 bad_repo`,
`bad_pr`, `bad_command` (a `harness` that fails the name pattern or a
`prompt` past the launch cap), `not_pinned`, `not_launchable`,
`launch_failed` (no tmux), `gh_unavailable` (missing or signed out) —
and `409 open_pending` while another open of the same PR is still
starting in its worktree, the same reason `resolve_pending` exists;
`409 unsupported_launch_option` covers `model`/`effort` a launch can't
honour.
Otherwise `202 {launch_id, state: "pending", note: "preparing
worktree"}` and the rest runs on a thread: read the PR (for the title
and the default prompt), `manage.pr_worktree` — the repo's bare mirror
cloned on first use, `refs/pull/<n>/head` fetched into a detached
checkout under `~/.swe/worktrees/<owner>__<repo>-pr<n>`, where a
second open of the same PR resumes — then `supervise.launch_new` there
with the client's `prompt`, or `build_prompt`'s review brief when none
was sent. The launch's `note` steps through "Cloning mirror…" (first
run only), "Checking out PR #<n>…" and "Starting <harness>…"; `cwd`
becomes the worktree and `title` `<owner>/<repo>#<n> <pr title>`, which
adoption stores as the session's display name exactly as a resolve's
does. Two opens of PRs in one repo share the mirror's `FETCH_HEAD`, so
the worktree calls serialize on a per-repo lock (the same one the web
app's worktree action holds). A harness swe can't read still launches
and settles `untracked`, as a resolve's does.

`GET /v1/folders?path=` lists one folder's subfolders on the hub machine
for the phone's folder browser: `{path, parent, folders: [{name, path}],
truncated}`, names only, dot-folders hidden, at most 500. No `path` means
the hub user's home; a relative or missing folder is `422 bad_cwd`, an
unreadable one `403 not_readable`, and another `machine` is `422
browse_unavailable`, since a satellite's disk isn't readable from the
hub.

`GET /v1/sessions/{id}/commands` (`slash.py`) lists the slash commands
a client's "/" menu offers for one session. The hub only lists them:
the client inserts `/name ` into the message and the harness expands it when
the message arrives. The answer is `{key, harness, channel, attached,
supported, custom_available, commands: [{name, description, source,
argument_hint?, argument_required?, insert?}], note?}`. `source` is
`builtin`, `user` (a user command file), `project` (a project command
file), `skill` or `plugin`; `insert` is the text to put in the box when
it isn't `/name ` (Codex skills are mentioned as `$name`). An unknown
session is `404 unknown_session`; a harness with no table answers an
empty list with `supported: false`.

- **Built-ins** come from curated tables in `slash_builtins.py` (Claude
  Code, Codex, opencode), with the date they were last checked against
  each harness's docs.
- **Custom commands and skills** come from the user's and the project's
  folders (the folder and its parents up to the repo root): for Claude
  Code `~/.claude/commands`, `.claude/commands` (a subfolder becomes a
  `dir:name` segment), the skills root the registry names (else
  `~/.claude/skills`), `.claude/skills`, and enabled plugins' commands
  and skills as `plugin:name`; for Codex its skills (registry root or
  `~/.codex/skills`, `~/.agents/skills`, `.agents/skills`); for opencode
  `~/.config/opencode/command(s)` and `.opencode/command(s)`.
  Descriptions come from frontmatter, else the body's first line;
  `user-invocable: false` hides a skill. Only regular files are read,
  and only their first 16 KB; in a project's folders a link out of the
  folder is skipped too, since the repo decides what is there. On a name clash each harness's
  own rule applies: Claude Code takes a skill over a command file and
  the personal one over the project one, opencode takes the project's.
  Codex file prompts (`/prompts:`) are not listed: codex no longer reads
  them.
- **Channel filter.** `channel` is the attached session's mode, or for
  an observed one the mode an `auto` attach would pick. Over `tmux` the
  text reaches the real TUI, so built-ins work, except ones whose bare
  form opens a picker or dialog the phone can't drive: those are left
  out, or listed with `argument_required` when an argument skips the
  picker (`/model sonnet`). `headless` (`claude -p`) keeps custom
  commands and skills and only the built-ins Claude Code documents for
  print mode. `serve` and `appserver` drop every built-in; opencode's
  server takes no custom command from a prompt (`custom_available:
  false`), codex's app-server still expands `$skill` mentions.
- **Caching.** Each harness's user-level scan and each folder's scan are
  reused for 30 s.
- **Satellites.** The agent adds a `slash` object to its
  `/v1/agent/catalog` push, only when it changed: per harness, the user
  layers once, the project layers of each folder that has any, and its
  `auto` channel, plus the mode of each attached session. The hub keeps
  it beside the satellite's rows and answers from it. The object is
  capped (500 entries a layer, 1 MiB, the biggest folders dropped first);
  if the push would still pass the hub's 4 MiB limit, the agent sends
  the rows without it. A satellite whose
  agent never sent one gets built-ins only, `custom_available: false`
  and a note saying custom commands aren't available for that machine
  yet.

`GET /v1/sessions/{id}/files?q=&limit=` (`files.py`) feeds the prompt
box's `@` menu: `{cwd, query, source, truncated, entries: [{path, kind,
mtime}]}`, where `path` is relative to the session's working folder,
`kind` is `file` or `dir`, and `source` says how the folder was listed.
The app inserts `@path` into the message and the harness reads the file
itself; the hub only lists names. In a git work tree the listing is `git
ls-files --cached --others --exclude-standard` (so `.gitignore` holds,
with the repo's fsmonitor hook off and no index lock taken), and an
empty answer stays empty; outside a work tree a breadth-first walk skips
dot-folders and dependency or build folders (`node_modules`, `venv`,
`__pycache__`, `build`, `dist`, `target`, ...) and never descends a
symlink: it opens each folder one part at a time with `O_NOFOLLOW` and
reads its children through that open folder, so a folder swapped for a
link mid-walk is skipped. A listing keeps at most 20,000 files, opens
at most 5,000 folders, and stops after 3 s (`truncated: true` when any cap
hit); it is cached per folder for 10 s, so each keystroke only re-ranks,
and keystrokes that arrive while it is being built wait for that build.
Every path must resolve inside the working folder: a symlink, or a
folder reached through one, that points outside is left out. Ranking is
case-insensitive, in tiers: the name equals the query (with or without
its extension), starts with it, holds it; the whole path starts with it,
holds it; the name, then the path, holds its letters in order. Within a
tier the tighter match, then the shorter path, then the more recently
modified one leads. An empty `q` gives the most recently modified files.
A query that is absolute, starts with `~`, or has a `..` part matches
nothing. `limit` defaults to 50, at most 200. Errors: `404
unknown_session`, `422 bad_cwd` (the session has no absolute working
folder), `422 cwd_missing` (it no longer exists or isn't a folder), `403
not_readable`, and `422 files_unavailable` for a satellite's session,
whose disk the hub can't read and whose command journal has no way to
answer a request yet.

`GET /v1/sessions/{id}/preview?path=` (`preview.py`) returns one page
file from the session's working folder as it is on disk now, for the
Preview button on a tool card that wrote it. `path` is relative to the
working folder (a client holding a tool call's absolute path strips the
session's `cwd` first). The route shares the `@` search's confinement:
an absolute path, a `..` part (either slash) or a NUL is refused before
anything touches disk; the path is resolved with `realpath` and must
stay inside the resolved working folder; then the file is opened one
folder at a time from that root with `O_NOFOLLOW`, so a part swapped
for a symlink after the check can't lead outside. A symlink that stays
inside the folder is followed. Both the requested name and the resolved
name must end in `.html`, `.htm` or `.svg` (any case), only a regular
file is read, and the cap is 2 MB. The answer is the raw bytes with
`Content-Type` `text/html; charset=utf-8` or `image/svg+xml` (from the
resolved name, so a link keeps its target's type), `Cache-Control: no-store`, `Last-Modified`, `X-Content-Type-Options:
nosniff`, and `Content-Security-Policy: default-src 'none'; style-src
'unsafe-inline'; img-src data:; font-src data:; media-src data:;
base-uri 'none'; form-action 'none'; sandbox`, so even a browser that
opens the response directly runs no script, loads nothing over the
network and gets an opaque origin. Errors use the JSON error shape:
`404 unknown_session`, `422 bad_path` (empty, absolute, `..`, NUL, or
over 4096 characters), `415 unsupported_type`, `403 outside_folder`,
`404 not_found`, `422 not_a_file` (a folder, FIFO or device), `413
too_large`, `403 not_readable`, the `@` search's `bad_cwd` and
`cwd_missing`, and `422 preview_unavailable` for a satellite's session.

The iPhone app (`PagePreview.swift`, `PagePreviewSheet.swift`) shows
Preview on a tool card whose write or edit names a page file inside the
session's `cwd`: it strips `cwd` from an absolute path (`/private/tmp`
and `/tmp` count as one), and a path outside it, with a `..` part or
starting with `~` gets no button. It fetches when the sheet opens and
draws in a `WKWebView` with JavaScript off, a non-persistent data store,
a content rule list that blocks every load but `about:` and `data:`,
`loadHTMLString` with no base URL, and a navigation delegate that
allows only that first load. An SVG goes in as a `data:` image, which
WebKit draws without running its scripts.

`GET /v1/search?q=&limit=` (`search.py`) puts each matching session in
the first tier it qualifies for: its title holds the query (whole, or
every word of it); then its details (title, harness, folder, id
together) do; then its transcript holds the exact phrase, all the words,
or any word. The transcript tiers come from the FTS5 index at
`~/.swe/data/search/search.sqlite` when it exists on the hub machine.
The SWE app writes it on a Mac; anywhere else, `swe session index --on`
makes the hub index agent transcripts every two minutes
(`sessions/search_writer.py`; [data-model.md](data-model.md) lists the harnesses).
Details match as case-insensitive text (the Mac app's Sessions match;
never a regex, which could backtrack without bound). Within a tier,
relevance (BM25 as a share of the tier's best, or 1 for the whole query
and 0.7 for the words apart) is multiplied by a recency weight that
starts at 1, halves its decaying part every 14 days, and never drops
below 0.3. Every result carries the snippet of its best transcript match
when it has one. Results are catalog rows, so each opens through the
usual session routes; satellite sessions match on details only, since
the index is per machine.

`GET /v1/usage[?refresh=1]` (`usage.py`) returns the hub machine's
`swe list --usage --json` rows as `harnesses`, plus `window_days` (100),
`machine` and `server_time`: every active harness with its 100-day
session count, and a `rate_limit` reading for each starred one. Readings
come through the rate-limit disk cache with swe's default 300 s TTL,
pinned so a `SWE_RATE_LIMITS_TTL` override in the shell that started the
hub can't freeze them (the Mac app strips it for the same reason), and
concurrent requests share one fetch. `refresh=1` refetches only readings older than 60 s and
failed ones, so a refresh button can't hammer providers that rate-limit.
Each reading carries `fetched_at`; one older than the cache window is
the 24 h outage fallback (newer fetches failed), and the clients mark it
stale. An unreadable registry answers 503 `usage_unavailable`.

Satellites run the same snapshot every 300 s and post it to
`POST /v1/agent/usage` (`{machine_id, harnesses, window_days,
server_time}`); a failed push retries after 60 s. The hub keeps each
machine's last report in `data/remote/satellite-usage/<machine>.json`,
so a sleeping MacBook still shows its last figures after a hub restart.
`GET /v1/usage` lists every machine under `machines`, hub first:
`{machine, harnesses, window_days, reported_at, received_at, hub}`.
`reported_at` is the reporting machine's clock when it took the
snapshot and `received_at` the hub's when the report arrived, so a
client can age a satellite's `fetched_at` stamps without trusting the
two clocks to agree. The top-level `harnesses` stay the hub's own rows.
The iOS app's Usage tab reads this route, with a machine switcher once
more than one machine reports.

The same push carries `daily_usage` (#525): `{time_zone, days: [{date,
harness, input, output, cache_read, cache_write, reasoning, tokens,
cost_usd, unpriced_tokens}]}`, this machine's tokens and cost per local
day and harness for the last 400 days. The satellite scans its own web
ledger (`data/web-ledger/`, new lines only after the first pass) and
prices from the price list on disk, with no network; a ledger it can't
read leaves the field out and the limits still go. The hub stores it
with the report, never returns it from `/v1/usage`, and adds it into
`GET /v1/app/overview`, which takes `machine=<id>` to show one machine
and lists every machine's period totals under `machines`. A satellite's
rows carry no model, so its view has no top models.

`journal.py` is the reconnect story: seq-numbered per-session ring on
disk; client resumes with `?last=seq`. "Open the app, catch up on 30 min
of agent work" replays the journal instead of refetching a transcript —
this is what Claude RC's server-side transcript buys them.

Attachments stage through the hub: `POST .../attachments` lands the
bytes under `remote/inbox/<session>/` on the machine running the session
(a satellite writes them via its own connection when the session is
remote), and the prompt references the path: `@`-ref syntax where the
harness supports it, an inlined path otherwise. Caps: 20 MB per file,
image/pdf/text only, swept after 7 days or at session end, whichever
comes first.

Which form a harness gets is its harness.json
`capabilities.attachments.at_refs`, over `attachments.ATTACH_FALLBACK`
for a harness that declares nothing (`supported: false` turns `@` refs
off). The fallback says yes for Claude Code (its TUI and `claude -p`
expand `@/abs/path` on submit), Gemini CLI and Qwen Code (their `@`
command processor reads the file). Every other harness gets the path in
plain text, `[Attached file: /abs/path] <message>`, and reads the file
with its own tools: Codex's and OpenCode's `@` is a TUI picker that never
runs on typed or API-sent text. Either form stays on one line, since a
newline typed into a tmux pane would submit early. A channel tied to one
harness (headless Claude, OpenCode serve, Codex app-server) uses that
harness's answer; a tmux pane uses the session's harness.

`GET /v1/devices` answers `{devices, requests, signins}` for the phone's
pair-requests-and-devices screen: every `devices.json` row as
`pairing.device_view` serves it (everything but the credential, plus
`id` and `revoked`), each with `paired_via` and `this_device` marking
the caller; `requests` is the same pending-request list
`GET /v1/pair/requests` serves, codes and secrets never in it; and
`signins` lists the owner's tailnet devices the hub let in without
pairing (`tailnet_signins.json`, 0600, written at most every five
minutes per node): `{id: "tailnet:<node>", node_name, first_seen_at,
last_seen_at, revoked, revoked_at, this_device}`, newest first. `POST
/v1/devices/revoke {device_id}` signs one paired device out
(`swe remote devices --revoke`'s hub-side twin, shared with the web
Settings page's `revoke` action in `devices.py`) and answers `{revoked,
this_device, devices, signins}` with the lists refreshed. A
`tailnet:<node>` id revokes that sign-in: the node gets no owner
identity (no unpaired access, no owner approval of pair requests) until
a pair request from it is approved again, and devices paired from it are
signed out with it. Revoking a device paired through a tailnet request
revokes its node the same way, every other device paired from it
included, so a lost phone on the owner's login can't walk back in
through Safari or a second pairing; open event streams from the node
close within the usual revocation check. A `tailnet_signins.json` that
can't be read refuses every node rather than forgetting revocations
(owner devices then pair like anyone else), and is never rewritten
until it reads again. `this_device` in the answer, and the
`confirm_this_device` guard, cover the caller losing its own access
through its node, not only its exact id. The owner's other nodes are
untouched. Refusals: `400 bad_device`, `404 unknown_device`, `409
already_revoked`, `409 confirm_this_device` for the caller's own row or
sign-in without `confirm_this_device: true` (a stray tap can't lock the
phone out), and `500 ssh_key_left` when the device is out but its
`authorized_keys` line stayed.

## Sessions on keepers — `runtime.py`

Behind `remote.session_view` (`swe config set remote.session_view true`,
then restart the hub; off by default). Full rules:
[G8-hub-on-keepers.md](specs/G8-hub-on-keepers.md).

With the setting on, a session of a harness that has a session adapter
(`capabilities.session` in harness.json, over the built-in fallback table;
today Claude Code, Codex, opencode and the ACP harnesses, each through the
adapter listed below) is run by a keeper: one detached process per session
(`python -m swe.remote.keeper`) that holds the agent, the send queue
and the session's event log, and keeps running when the hub restarts.
Everything else on this page still applies to every other session, and
to all of them when the setting is off.

| Adapter id | Harness | How SWE talks to it | Recorded against |
|---|---|---|---|
| `claude_stream` | Claude Code | `claude` with stream-json on stdio (`claude_wire.py`) | `claude` 2.1.328 |
| `codex_appserver` | Codex | `codex app-server`, JSON-RPC on stdio (`codex_wire.py`) | `codex` 0.170.0 |
| `opencode_serve` | opencode | `opencode serve` on a loopback port with a password the adapter makes (`opencode_wire.py`) | `opencode` 2.3.2 |
| `acp` | Gemini, Qwen, Kimi, Devin, Cursor, Copilot | the agent's ACP mode, JSON-RPC on stdio (`remote/acp.py`) | `qwen` 0.28.0; Gemini and Kimi from hand-written fixtures |

When the hub starts with the setting on, it writes Claude's
`capabilities.session` entry into harness.json once. For the other
harnesses, the fallback row turns launch, turns, approvals and interrupt
on, and refuses steer, fork, rollback and the options until harness.json
declares them. Each adapter's spec ([G7](specs/G7-claude-stream-adapter.md),
[G16](specs/G16-more-adapters.md)) has the entry to write and what its
recordings prove. opencode 2.x needs its adapter: the older channel in
`inject.py` speaks the 1.x routes, which 2.3.2 answers with 405, so an
opencode 2.x session started in a terminal cannot be written to.

| What | With a keeper |
|---|---|
| `POST /v1/sessions` | the hub picks the session id, stores the prompt, starts a keeper and sends the prompt. No pending marker, nothing to adopt. One launch per `command_id`: a retry, or a second request at the same moment, answers the launch already made; the same id for a different launch answers 409 `command_id_conflict`. `yolo: true` still launches the old way |
| Attach (`control {op: attach}`, no `mode`) | Continue in SWE: the session is resumed by its own id under a keeper. Refused (409) while it is mid-turn, its transcript moved in the last 5 seconds, or a harness process still has it open (`session_in_use`). An explicit `mode` still asks for that channel |
| The row | `ownership: attached`, `mode: "keeper"`, `control: "full"`, `state` (the keeper's), `status`, `adapter`, `caps` |
| `message`, `approval`, `control`, `queue` | stored under `command_id` (or `client_id`) in `remote.sqlite` before delivery. A retry answers the stored receipt (`duplicate: true`) and is not applied again. `message` answers 202 with `receipt` (`accepted` or `queued`) and `waiting` |
| Two devices answer one approval | the first wins; the second gets 409 `approval_failed` |
| The queue | lives in the keeper. `GET .../queue` lists it; `edit`, `cancel` and `steer` act on it. `client_id` names the queued message; an optional `command_id` is the action's own id, and a retry under it is not applied again. Send now sends the text as last edited |
| The timeline | the keeper's log. The hub journals each event under the keeper's seq and does not tail the CLI's transcript for that session. Today's clients get the same v1 frames as before (`protocol_compat.to_v1`); a question with choices arrives as an approval card |
| Hub restart | the hub reconnects to every keeper still running, replays from the journal's last seq, and the open approvals and the queue are as they were. A message the old hub accepted and never delivered is delivered |
| Agent crash | status `error`; the next message restarts the agent in the same keeper |
| Keeper killed | open approval cards are retired and status turns `error`; the next message starts a new keeper that resumes the harness session |
| `detach`, `kill` | stop the keeper; the session is a terminal session again (`ownership: observed`) and can be continued later. A keeper that does not stop keeps the session attached and answers 503 `keeper_failed`. `detach` with `mode: keep` answers 409 (there is no pane to keep) |
| `compact`, `clear` | 409 `control_failed`: not available on a keeper yet |
| `POST /v1/sessions/{key}/command {op, command_id?, ...}` | new, keeper-held sessions only (409 `not_kept` otherwise): `set_option` with `name` (`model`, `effort`, `permission_mode`, `plan_mode`) and `value`, `stop_task` with `task_id`, `interrupt` with an optional `clear_queue`, `park`. Answers `{ok, receipt, command_id}` |
| Fork | a new keeper that forks by the harness's id, when the adapter can fork |
| Parked, then resumed in a terminal | the hub lets go: `ownership: observed`, `control: "read_only"`, `control_reason` |

A keeper parks its agent after 30 idle minutes and resumes it on the
next message. `GET /v1/launches/<id>` answers from `remote.sqlite` when
the hub that took the launch has restarted.

### On a satellite

Full rules: [G15-satellite-keepers.md](specs/G15-satellite-keepers.md).
The setting is read on each machine for that machine: turn it on on the
satellite (and restart its agent) for its sessions to run under
keepers, and on the hub for the hub to copy their timelines and hold
commands for them.

| What | With a keeper on a satellite |
|---|---|
| Launch, attach | the satellite's agent starts the keeper, the way the hub does for its own machine. The hub sends the same `launch` and `attach` commands as before |
| The timeline | the satellite journals the keeper's events, and uploads them with `POST /v1/agent/kept`. The hub's journal for `key@machine` is a copy: same events, same seqs, stored with `Journal.append_at`, so an upload sent twice lands once. The hub numbers nothing in that journal while it copies it |
| `message`, `approval`, `control`, `queue`, `command` | stored in the hub's `remote.sqlite` under `command_id`, sent to the satellite as a `kept` command, and given there to the same route the hub runs for its own sessions. Answers 202 with `receipt` (`queued` or `held`) and `command_id` |
| The machine is asleep | a message or an approval answer is `held` for 24 hours and delivered, in order and once, when the machine's command stream comes back. A hold that runs out is rejected (`expired`) and never delivered. `control`, `queue` and `command` answer 409 `machine_offline` |
| Hub restart | a message or an answer the hub had claimed and not yet put on the machine's command stream is put there once at the machine's next flush (its reconnect, or within 60 seconds of it), in arrival order with what is still held. One that arrived 24 hours ago or more is rejected instead |
| Agent restart | the keepers run on. The new agent reconnects to them, asks the hub how far each copy reaches, and uploads from there |
| The hub's session runtime is off | the upload is refused (409 `session_view_off`) and retried every minute; the hub lists the session without a timeline, and commands take the old forwarding path |

## The loopback API

Every machine binds one more listener, on `127.0.0.1` only:
`swe remote serve` on the hub machine and `swe remote agent` on a
satellite. It serves the same `/v1` session routes as the hub, through
the same handler, over that machine's own sessions, so a client on the
machine (the Mac app) can list, read and drive them with no network
and with the hub down.

| | |
|---|---|
| Address | `http://127.0.0.1:8476`, or the port in `remote.local_port` (`swe config set remote.local_port <n>`; `0` picks a free one). The address bound is written to `local.json` |
| Who may call | `Authorization: Bearer <token>`, the token in `~/.swe/data/remote/local.json` (mode 0600, made on first use). Being able to read that file is the proof. A paired device's credential is not accepted here, and the token is accepted nowhere else |
| Routes | `/v1/sessions` and everything under it, `/v1/launches/*`, `/v1/events`, `/v1/status`, `/v1/machines`, `/v1/folders`, `/v1/inbox`, and the static files. Anything else under `/v1` answers 404 |
| Host | a request whose `Host` is not `127.0.0.1`, `localhost` or `[::1]` (with or without the port) answers 403 `bad_origin` |
| Port taken | one line on stderr and no local API; the hub or agent runs on |

The token is never printed, logged or returned by a route.

## Sync v2 — `sync.py`

A second protocol for one session's stream, asked for per request with
`?v=2` or the header `X-SWE-Protocol: 2`. A client that does not ask
gets everything above unchanged. It works for every session, with
`remote.session_view` on or off. Full rules, and what a client must do
with each frame: [G9-sync-v2.md](specs/G9-sync-v2.md). The shapes are
[`schema/session_v2.json`](../src/swe/remote/schema/session_v2.json),
generated from `adapters/events.py`; the session view's TypeScript
types are made from that file.

| What | In v2 |
|---|---|
| `GET /v1/status` | adds `protocols` (`[1, 2]`), `features` and `boot_id` (new for every hub process) |
| The events | the v2 events of `adapters/events.py`, as a keeper logged them. A journal of v1 events (a terminal session, a satellite's, the earlier history of one continued in SWE) is converted on the way out (`protocol_compat.to_v2`) |
| `seq` | above 0: the event's place in the journal, and the only frames with an SSE `id:` line. 0: a frame the hub made for the stream. A delta and the stream's own frames have none |
| Catch-up | `?v=2&last=<seq>&epoch=<E>`. On the session's epoch and at most 500 seqs behind: the events after `last`, then `session.row`. Otherwise one `session.snapshot`. Either way it ends with a `session.state` frame |
| `session.state` at the end of a catch-up | `{state, live[], pending[], head, epoch}`: the text so far of every message in progress, the ids of the requests still open, the seq the stream is caught up to |
| Live text | `message.delta` is never journaled, so the hub keeps the text in progress itself (from the deltas, and from a keeper's welcome after a hub restart) and sends it in `live[]` |
| Delta offsets | every delta says the `offset` its text starts at. A client appends only at the length it holds; on a hole it asks for the snapshot and carries on from `live[]` |
| A slow client | its queue (2000 frames) fills: the stream reads the journal again from the last seq it sent, then sends `session.row` and `session.state`. Nothing journaled is lost |
| `ping` | `{ts, head}` every 10 s; 25 s of silence means the connection is dead |
| Epoch | a number per session that moves when its journal starts over or loses a stretch; a cursor from another epoch gets a snapshot |
| `GET /v1/sessions/{key}/snapshot` | the same body the stream's `session.snapshot` carries: row, state, caps, open requests, queue, live text, plan, running tasks, checkpoints, changes, usage, the last 200 timeline events |
| `GET /v1/sessions/{key}/messages?v=2` | older pages of the timeline, as v2 events |
| A row change (ownership, title, pin) | a `session.row` frame |
| `GET /v1/events` | v1 only |

## Keeping the hub up — `launchd.py`, `systemd.py`

A hub started from a shell dies with the login session, and nothing
restarts it. `swe remote serve --install` writes
`~/Library/LaunchAgents/com.cwenlong.swe.remote-hub.plist` with
`RunAtLoad` and `KeepAlive`, so the hub starts at login and launchd
restarts it 15 s after it exits. The agent runs SWE.app's bundled
`swe` when the app is installed, and otherwise the `swe` that installed
it. macOS privacy protection blocks a launchd agent from reading
`~/Desktop`, `~/Documents` and `~/Downloads`, so a checkout's
`.venv/bin/swe` there would fail at every start: `--install` refuses it
and says to install the app. `install-app.sh` restarts the agent on the
new bundle. The agent gets the installing shell's `PATH` and logs to
`~/.swe/data/remote/serve.log` with timestamped start and stop lines.
It runs `serve --tailnet`: with no Tailscale address yet (early in a
login) the hub exits instead of binding localhost, and launchd retries.
`--install` again restarts it; `--uninstall` stops and removes it.

The LaunchAgent sets `AbandonProcessGroup`, so launchd leaves session
keepers running when the hub's job ends. An agent installed before that
key existed gets it without a reinstall: `swe remote serve` adds the key
to the installed plist when it starts, and when it is running as that
agent it reloads the job from the new file (`launchctl bootout` and
`bootstrap`, from a shell outside the job, because the unload ends the
hub). The hub is back a moment later. On Linux the unit needs nothing:
each keeper is started in a systemd scope of its own.

On Linux, `--install` writes the systemd user unit
`~/.config/systemd/user/swe-remote-hub.service` (under
`$XDG_CONFIG_HOME` when set) with `Restart=always`, `RestartSec=15` and
`WantedBy=default.target`, then runs `daemon-reload`, `enable` and
`restart`. A hub installed before the rename ran as
`quiver-remote-hub.service`: once the new unit has started, `--install`
stops, disables and removes that one (its file stays if it will not
stop), `--uninstall` removes both, and `--status` reports the old unit
while it is all a machine has. It runs the `swe` that installed it, with the installing
shell's `PATH`, and appends to the same `serve.log`. A user service
stops at logout unless the user lingers, so `--install` runs `loginctl
enable-linger` when lingering is off (polkit usually lets a user do it
for themself) and, when that's refused, says to run `sudo loginctl
enable-linger $USER`. A machine with no systemd user manager (some
containers, WSL without systemd) is told to run `swe remote serve`
under its own init system.

### Keeping a satellite up

`swe remote agent --install` does the same for a satellite, through the
same code (`commands.Service`: one record per service with its launchd
label, systemd unit and log). It writes its own
`~/Library/LaunchAgents/com.cwenlong.swe.remote-agent.plist` on a
Mac and `~/.config/systemd/user/swe-remote-agent.service` on Linux
(clearing a pre-rename `quiver-remote-agent.service` the same way),
so installing, restarting or removing it never touches the hub's
service. It runs `swe remote agent` (the app's bundled `swe` on a Mac,
under the same protected-folder rule), with the installing shell's
`PATH`, logging to `~/.swe/data/remote/agent.log`. The credential
stays in `agent.json`: the service reads it at start, so no secret
lands in the plist or unit. `--install --hub=URL --token=T` pairs first;
`--install --hub=URL` alone puts `--hub` on the service's command line.
Lingering is handled as for the hub. `--uninstall` stops and removes it,
and `--status [--json]` shows whether this machine is paired, whether
the service is installed and running, and whether the hub is installed.

`--install` refuses on a machine whose own hub service is installed
(the hub's plist or unit exists), and the hub's install (`serve
--install`, `setup --apply`) refuses the other way round: the check sits
in `install_service`, which every install path goes through. Both
services run as the same user, so they share `~/.swe/data/remote/`:
each would read and move the other's tail cursors in `cursors/`, and a
satellite would send its own hub's sessions back to it. The refusal
names the other service's file and says which `--uninstall` to run.

Only one `swe remote agent` runs per `~/.swe`: it holds an `flock`
on `data/remote/agent.lock` (its pid inside) for its lifetime, and a
second one exits at start, so the hub's commands never run twice. Under
launchd or systemd that exit is retried, so the service takes over once
a shell-started agent stops. `--install` refuses while an agent started
outside the service holds the lock; the service's own agent is the one
a reinstall restarts.
`install-app.sh` restarts either agent when it runs the app's bundle.

## Satellite — `agent.py`

Same `tail.py`/`inject.py`, no listener: persistent outbound connection
to the hub over Tailscale (long-lived HTTPS stream or long-poll command
fetch + POST events; stdlib covers both). Registers machine metadata —
hostname, specs, harnesses detected by the existing `discover`
machinery. Hub stamps every session `machine_id` + reachability; queued
commands wait in the hub journal until reconnect.

Since G15 the agent also holds this machine's own hub state
(`RemoteAgent.local`): a journal of every session here, the session
runtime that runs this machine's keepers, and the loopback API (see
"On a satellite" and "The loopback API" above). Each event is written
to that journal and to the uplink outbox. `agent-kept.json` lists the
keeper sessions whose journal the hub copies.

`swe remote agent --install` keeps it running across reboots and
logouts (see "Keeping a satellite up" above). The macOS app side is a
Settings › Integrations › Remote switch, "Keep this Mac's agent running", that calls
`swe remote agent --install|--uninstall --json` (`RemoteAgentModel`). It
shows only on a satellite of another hub: `agent --status --json` says
the Mac is paired (`configured`) and its own hub is not installed
(`hub_installed` false), or the agent is already installed so it can be
turned off. All session data
still flows through bundled `swe` — the app
never writes transcripts itself, same as the
writes-go-through-tested-Python rule.

## Phone client

[app/ios/ROADMAP.md](../app/ios/ROADMAP.md) tracks the client redesign:
every page, component and feature, and the hub work each needs.

The Mac app reaches another machine's hub through the web app too: its
sidebar's machine switcher (#681) loads a picked hub's `/` in a web view
that takes over the window, native sidebar included, pinned to that
hub's origin. The title-bar strip names the machine and keeps the
machine menu for the way back. The hub treats it as a
browser on the Mac: the owner's own tailnet node gets in with no pairing
(the web app sends `X-Swe-Pair: 1` from the hub's own origin; the hub also reads `X-Quiver-Pair`, its name before the rename, for a tab holding an older page), and any
other node sees the web app's pairing card, whose credential stays in
that hub's persistent web data store. The switcher lists the hub this
Mac's `agent.json` names and hubs added by address in Settings ›
Integrations › Remote › Other hubs.

Two clients. The web app (`src/swe/webapp/`, served by the hub under
`/`; older `/app/` links redirect there, query and fragment intact) works in any browser over
Tailscale: its `#pair=` fragment trades a pair token for the credential
it keeps in localStorage, and pending pair requests are decided inline
in Settings › Integrations › Remote. On iPhone the client is native
SwiftUI (`app/ios/`):
it reads the same REST routes and SSE streams, keeps one credential per
paired hub in the Keychain (it can be paired with several hubs and
switches between them from the hub menu), and leaves room for widgets, Live Activities, Face ID and
APNs. It replaced an earlier WKWebView wrapper around the SPA once the
webview's scrolling, keyboard handling and blank offline launch showed.
Push comes from ntfy for now. A device paired in a browser and the
app pair separately; each holds its own credential. Share only protocol
DTOs with SWECore: decoding stays server-side and most of SWECore
is AppKit-bound.

Screens:

- **Sessions** — machine → project groups; status dot
  (active/needs-you/error/offline); unread badges; new-session sheet
  (machine → dir → harness → prompt).
- **Chat** — `kind` drives rendering: `thinking` auto-collapses to a
  "Thought for 12s" card; `tool_use`/`tool_result` pair into collapsible
  cards (`Edit · src/foo.py` → args + output), runs fold to "used 6
  tools"; `compaction` renders an inline divider; `approval` renders an
  action card (allow/deny/always). In both clients a send goes out at
  once when the agent is idle; mid-turn it waits in a Queued tray above
  the prompt box (Edit, Remove, Send now, Remove all), reloaded from
  `GET /v1/sessions/{id}/queue` on open, and the send button turns into
  a red stop while the box is empty and the agent works. The composer
  also carries `/` shortcuts, `@` file-complete proxied through the
  hub, and photo/file attach landing on the machine as `@`-refs, or
  as plain paths for harnesses whose prompt doesn't expand `@`.
- **Inbox** — the control-plane screen: every pending decision across
  both machines (approvals, `needs_followup` hits, errors, finished).
  The phone's job is unblocking agents, not being a terminal. The hub
  keeps it bounded: follow-ups, errors and interrupts drop out three
  days after the session's last activity (`catalog.INBOX_MAX_AGE_S`),
  while pending approvals stay until answered. `POST /v1/inbox/dismiss`
  clears a row for the calling device only; the hub stores
  `key → last_active_at` per device id in `inbox_dismissals.json` and
  hides the row until its `last_active_at` moves past that, so new
  activity brings it back. Dismissals prune 30 days after they were
  made and never hide an approval. `?done=1` adds sessions that
  finished since the hub's local midnight as `done` items.

- **Unread dots** — every session row the hub serves (`GET /v1/sessions`,
  `GET /v1/app/sessions`) carries `unread` for the calling device: true
  when the row's `last_active_at` is newer than that device's last-read
  cursor. A client posts `POST /v1/sessions/{id}/read` when a chat opens
  and again as messages arrive while it stays open. `last_active_at` is
  when the transcript last moved, which in practice is the agent's
  reply, since a user turn is followed by one. The hub keeps
  `device_id → key → at` in `session_reads.json` beside `devices.json`
  (0600), so a phone and a laptop keep separate state; the owner's
  tailnet browser reads as its `tailnet:<node>` id, the Mac app as its
  paired device. A session the device never opened is judged against a
  baseline, the later of when the device paired (`created_at`, or the
  sign-in's `first_seen_at`) and when the store began (`since`, set on
  the first listing), so pairing a phone or upgrading the hub leaves
  old rows read. The cursor defaults to the later of now and the row's
  own `last_active_at`, so a satellite clock running ahead can't leave a
  just-opened chat unread, and it never moves back. Each write prunes
  cursors for sessions the catalog no longer lists (skipped while it
  lists nothing) and for revoked or unpaired devices, and keeps the
  newest 5,000 per device.

  GitHub's "needs you" joins the same feed: `serve` wires the hub's
  own `gh` login into the catalog's refresh loop through the web app's
  shared notifications cache (`webapp/pages/inbox.unread_rows`), so the
  poll costs nothing beyond the cache's cadence and a hub without a
  working `gh` silently has no GitHub group. Unread notifications land
  as `kind: "github"` items (`repo`, `subject_type`, `reason`, `url`,
  `gh_thread_id`, `updated_at`) keyed `gh:<thread id>` after the
  session rows, newest first — being unread, they never age out.
  Dismiss is the same route and the same bring-back rule, with the
  thread's `updated_at` as `last_active_at`; marking read goes through
  `POST /v1/app/inbox/read` with the thread id, which PATCHes
  `notifications/threads/<id>` like `swe gh inbox read` does. A thread
  that newly turns unread emits a hub-local `notify` event
  (`kind: "github"`, deduped on `gh_thread_id` + `updated_at` so a
  repoll can't re-push) and ntfy posts only for
  `review_requested`/`assignee`/`mention`/`ci_fail` — comments on
  subscribed threads stay inbox-only. No phone replies to threads.
- **Machine** — per-device strip; the MacBook satellite can ride
  SWECore's Compute samplers (CPU/GPU/temps) along this view.

Service status pushes are off unless `notify.json` has
`"service_status": true` (`swe remote notify --service-status=on`; read
at `serve` start) and ntfy is configured and on. The hub then reads every tracked status page (the
built-ins plus pages tracked in the web app) every five minutes through
the web app's shared poller (`status/poller.py`, so the floors and cache
are the page's own) and applies `status/alerts.py`, the port of the
Mac's `ServiceStatusAlerts`: only the model providers and agent
platforms groups, from a minor incident up, never maintenance. A
provider entering that set emits a hub-local `notify` event
(`kind: "service"`, `change: "started"`, `service`, `name`, `health`,
`incident`, `title`, `message`, `url`) once per `service@incident`; it
leaving emits one `change: "resolved"`. A page's first reading only
records what is already open, and the state lives in
`~/.swe/cache/web-service-status-notified.json` (read, updated and
saved under a lock file, with the hub's memory winning over a file it
failed to replace), so a restart repeats nothing. ntfy gets the title and body, with the status page as
the tap target.

Beyond remote control, nearly free on this architecture: push for
needs-you/finished/error/limit via the self-hosted ntfy (iOS background
delivery needs `upstream-base-url: https://ntfy.sh` set, and ntfy `http`
action buttons answer straight from the lock screen: a POST to
`/v1/sessions/{id}/approval` over the tailnet, no app code. When the
event lists the pane's menu rows the buttons carry those labels — up
to ntfy's three-action cap, with a longer menu's last slot linking to
the session chat where every option answers — and prompts with no
option list fall back to Allow/Deny; rows that grant past the one
prompt, such as always allow, don't ask again or bypass, never get a
button); a
usage/limits dashboard (`swe list` data); transcript FTS across machines
(the search index normalizes already); diff viewer + commit/push via
`swe gh`; `$ swe report` cards; voice dictation; offline prompt queue.

## Security

- Tailnet membership is the transport boundary: the hub binds its
  Tailscale IP only (or loopback behind `tailscale serve`), so no
  LAN/WAN port opens unless `swe remote serve --unsafe-bind-any` is passed
  for a `--host` that is not one of those. Tailscale's OAuth
  login is how a device joins the tailnet; this feature does not
  re-implement it.
- Owner devices: a request without a bearer is still let in when its
  tailnet node belongs to the hub owner's login (not tagged), isn't the
  hub machine's own node, it sends
  `X-Swe-Pair: 1` from an allowed Host, and its `Origin` is the hub's
  own; the web app at `/` relies on this. Behind `tailscale serve` the
  node comes from `X-Forwarded-For` only on a connection the OS places
  in the Tailscale daemon. The `whois` answer is
  cached per IP (60 s, a miss 10 s). Satellite routes stay bearer-only.
- Pairing: the phone posts `POST /v1/pair/request`; the hub resolves
  its Tailscale node with `tailscale whois <peer ip>` and hands back a
  6-digit code + a poll secret. A second device — or the hub's own
  shell — approves it (`swe remote devices --approve=<code>`, the Mac
  app's Settings › Integrations › Remote › Pending requests when the
  hub runs on that Mac, the web app's Settings › Integrations › Remote
  on another browser, or any already-paired device). `--request-id=<id>`
  holds a CLI code to one request, as the HTTP route's `request_id`
  does; the Mac app always sends it. Approving mints a stored credential in
  `devices.json`
  (0600), revocable per device — Trusted Devices minus biometrics. A
  device can't approve its own request; five wrong codes deny it. The
  QR/token path (`swe remote pair`, `http://hub/#pair=token`; `--app`
  swaps in the `quiver://` deep link) stays as the fallback for devices
  `whois` can't identify. Either way, only a tailnet device can reach
  the pairing routes at all.
- Owner auto-approve (off by default; `swe remote devices
  --auto-approve=on|off`, or the switch in the Mac app's Pending
  requests): when on, a `POST /v1/pair/request` is approved at once,
  with no code, only if every one of these holds. The setting reads
  literally `true` in `data/remote/pairing.json`; the hub knows its
  owner's login (from `whois` of its own node at startup, so never on a
  plain loopback bind or a tagged node); the requesting node's `whois`
  says `tagged: false` and its login is exactly that owner login; it has
  a node id other than the hub machine's own (any local user's process
  reaches the hub as that node); the owner hasn't revoked that node (its tailnet sign-in,
  or the newest device paired from it; a later code approval lifts the
  second); the request has a JSON body and no foreign `Origin` (a
  cross-site form can't send either). Anything unknown leaves the
  request pending for a code. Both approval paths mint the same device
  row, marked `approved_via: code` or `owner_auto`, and the hub writes
  one `pair: ...` line per approval to its stderr (`serve.log`).
- Device identity: both pair paths record `kind` (iphone/ipad/android/
  mac/windows/linux/browser), `model`, `client` and `os` — explicit
  `device` fields the client posts, else whatever the User-Agent lets
  `pairing.client_meta` infer — plus the tailnet `node_name` where the
  request came from (the only "location" CGNAT offers). A paired client
  reads its own row on `/v1/devices/me` and renames itself through
  `/v1/devices/rename` (`display_name` beats `name` everywhere the
  device is listed). Requests from devices paired before these fields
  existed backfill them from the User-Agent on the same throttled write
  as `last_seen_at`.
- SSH console: the phone's terminal dials the machine's own sshd over
  the tailnet, never the hub, so it works while the hub is down. Its
  key is a Secure Enclave P-256 key; the hub only stores the public
  half on the device's row and, once `swe remote ssh --on` is set,
  writes it into `~/.ssh/authorized_keys` as
  `no-agent-forwarding,no-X11-forwarding <key> swe-remote:<device id>`
  (`sshkeys.py`). Those lines are rebuilt from `devices.json` on every
  change, so a revoke removes the device's login with its credential.
  A line a pre-rename hub tagged `quiver-remote:` is still managed: the
  next rewrite swaps it for the `swe-remote:` one.
  The phone pins the host keys `GET /v1/ssh` returned over the paired
  channel instead of trusting sshd's first answer. Tailscale SSH is not
  used: its server side needs the open-source `tailscaled` on macOS,
  not Tailscale.app.
- Notifications leave the tailnet: whatever an ntfy body carries is
  readable by the ntfy host (and ntfy.sh's relay for iOS delivery), so
  no full-power credential and no raw pane text goes in one. Approval
  action buttons POST with a token minted per notification — bound to
  (session, request_id) and to the decisions its buttons send, never a
  standing grant (always allow, don't ask again, session-wide, bypass
  or yolo rows need a paired device; anything else is a 403),
  single-use, ten minutes, the resolved
  request's token dropped while overlapping requests keep theirs —
  which only the approval route accepts (`notify.ApprovalTokens`);
  prompts run the transcript secret-redactor and a length cap first.
  The legacy `ntfy-actions` device row those buttons used to carry is
  revoked at every hub start, whether or not notifications still post.
- Threat model for the README: the hub executes on your machines by
  design; the credential gates the app layer on top of the tailnet —
  per-device revocation is what "revoke my lost phone" means without
  touching the tailnet. That covers a phone on the owner's own tailnet
  login too: revoking its sign-in (or a device paired from it) refuses
  the node until it pairs again.

## Milestones

| # | Scope | Proves |
|---|---|---|
| R1 | hub + tail + catalog + SSE + PWA read-only chat, Linux only | streaming, rendering on real transcripts |
| R2 | inject A-tier claude headless + tmux supervisor + attach | phone can prompt |
| R3 | satellite + machine registry + offline queue | MacBook joins |
| R4 | iOS app (a WKWebView shell, since replaced by SwiftUI) + Inbox + ntfy push + approval cards | notifications |
| R5 | opencode serve adapter, codex app-server adapter, diff pane, Queue/Steer polish | tier-A depth |

## Risks

- **Two writers on a transcript** — exactly the Codex write-lock
  complaint; the ownership rule is the fix, never inject into `observed`.
- **Approval fidelity** — only tier A is structured; tmux approvals are
  capture-pane heuristics.
- **Claude headless ≠ Claude RC** — no mid-turn approval prompts in
  `-p` mode, permissions fixed per invocation; true RC parity would need
  Anthropic's relay, which we deliberately don't use.
- **Not all harnesses persist thinking** — the card simply never
  appears there.
- **Push rides ntfy.sh's APNs relay** — a self-hosted ntfy still needs
  `upstream-base-url: https://ntfy.sh` for iOS background delivery, so
  notification metadata transits ntfy.sh unless bodies are encrypted.
- **Reverses the "cloud sync: out of scope" roadmap line** — scoped to
  session access, not app-state sync; already recorded as M9 in
  `app/mac/ROADMAP.md`.
