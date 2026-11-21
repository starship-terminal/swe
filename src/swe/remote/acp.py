"""Agent Client Protocol: the client side of one ACP agent over stdio.

ACP (agentclientprotocol.com) is newline-delimited JSON-RPC 2.0 between
a client (us) and an agent subprocess: ``initialize``, then
``session/load`` (when the agent advertises ``loadSession``) or
``session/new``, then one ``session/prompt`` request per turn whose
response arrives when the turn ends. While a turn runs the agent sends
``session/update`` notifications (message, thought and tool-call
chunks) and may ask ``session/request_permission``, a request the
client answers with one of the offered options. ``session/cancel`` is
the client's interrupt.

This module owns that wire and nothing about supervision:

- ``command_for`` says whether a harness can run as an ACP agent here,
  from ``capabilities.acp`` in harness.json over ``ACP_FALLBACK``, with
  each candidate checked against the installed CLI (cached).
- ``Connection`` is one agent process plus its reader thread. It turns
  updates into ``message.delta`` text (``DeltaStream``) and permission
  requests into the hub's ``approval.requested``/``approval.resolved``
  events, and answers every agent request it does not implement with a
  JSON-RPC error so the agent never waits on us.

``inject.AcpInjector`` and ``supervise.attach`` are the callers of the
pre-keeper channel. The session adapter (``adapters/acp.py``, G16)
drives the same ``Connection`` under a keeper: it subclasses it to read
every update itself, and emits v2 events instead of binding the hub's.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time

from swe.reports import transcripts as _tr

PROTOCOL_VERSION = 1

# How long each handshake step and a plain call get. session/load
# replays the whole conversation as notifications before it answers, so
# it gets longer.
INIT_TIMEOUT_S = 20.0
LOAD_TIMEOUT_S = 60.0
CALL_TIMEOUT_S = 30.0

# How long a closed agent gets to exit on stdin EOF before SIGTERM.
_CLOSE_GRACE_S = 2.0

# A ``--help`` probe that does not finish in this long counts as "no".
_PROBE_TIMEOUT_S = 5.0

# harness -> candidate ACP commands, first one the installed CLI lists
# wins. The first token is the binary; when it is the harness's own CLI,
# the rest must show up as a whole word in its ``--help`` (a subcommand
# or a flag), so a CLI version without ACP is never started as one. A
# separate adapter binary (claude's) only has to be on PATH: running an
# ACP server with ``--help`` could just sit waiting on stdin.
# codex and opencode are left out on purpose: their native tier-A
# channels (app-server, serve) expose more than their ACP modes.
ACP_FALLBACK: dict[str, list[list[str]]] = {
    "devin": [["devin", "acp"]],
    "cursor": [["cursor-agent", "acp"]],
    "copilot": [["copilot", "--acp"]],
    "gemini": [["gemini", "--acp"], ["gemini", "--experimental-acp"]],
    "qwen-code": [["qwen", "--acp"], ["qwen", "--experimental-acp"]],
    "kimi": [["kimi", "acp"], ["kimi", "--acp"]],
    # An adapter, not claude itself: only an explicit --mode=acp uses
    # it, because claude's session adapter is another (``auto_eligible``).
    "claude": [["claude-agent-acp"], ["claude-code-acp"]],
}

# The session adapter id that makes ACP a harness's own channel.
ACP_ADAPTER = "acp"

# The harness's own CLI per fallback key, to tell a subcommand probe
# from an adapter binary.
_OWN_CLI = {
    "devin": "devin", "cursor": "cursor-agent", "copilot": "copilot",
    "gemini": "gemini", "qwen-code": "qwen", "kimi": "kimi",
    "claude": "claude",
}

# (resolved binary path, mtime_ns, token) -> whether --help lists token.
_PROBES: dict[tuple, bool] = {}
_PROBES_LOCK = threading.Lock()


def _declared(name: str, reg: dict | None) -> list[list[str]] | None:
    """``capabilities.acp`` from harness.json: ``{"argv": [...]}`` or
    ``{"supported": false}``. None when nothing usable is declared."""
    if reg is None:
        from swe.harness import registry

        try:
            reg = registry.load_registry_readonly()
        except (OSError, ValueError):
            reg = {}
    entry = reg.get(name) if isinstance(reg, dict) else None
    caps = (entry if isinstance(entry, dict) else {}).get("capabilities")
    caps = caps.get("acp") if isinstance(caps, dict) else None
    if not isinstance(caps, dict):
        return None
    if "supported" in caps and not caps["supported"]:
        return []
    argv = caps.get("argv")
    if isinstance(argv, list) and argv and all(
            isinstance(tok, str) and tok.strip() for tok in argv):
        return [[str(tok) for tok in argv]]
    return None


def _help_lists(path: str, token: str) -> bool:
    """Whether ``<path> --help`` mentions ``token`` as a whole word."""
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return False
    cache_key = (path, stamp, token)
    with _PROBES_LOCK:
        if cache_key in _PROBES:
            return _PROBES[cache_key]
    try:
        out = subprocess.run(
            [path, "--help"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
        text = (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError, ValueError):
        text = ""
    found = re.search(
        rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None
    with _PROBES_LOCK:
        _PROBES[cache_key] = found
    return found


def command_for(name: str, reg: dict | None = None) -> list[str] | None:
    """The argv that starts ``name`` as an ACP agent on this machine, or
    None. A harness.json declaration is trusted once its binary is on
    PATH; a fallback candidate must also pass the ``--help`` probe."""
    declared = _declared(name, reg)
    if declared == []:
        return None
    if declared:
        return list(declared[0]) if shutil.which(declared[0][0]) else None
    for argv in ACP_FALLBACK.get(name, ()):
        path = shutil.which(argv[0])
        if path is None:
            continue
        if argv[0] != _OWN_CLI.get(name) or len(argv) == 1:
            return list(argv)
        if _help_lists(path, argv[1]):
            return list(argv)
    return None


def auto_eligible(name: str, reg: dict | None = None) -> bool:
    """Whether ``attach(mode="auto")`` should pick ACP for ``name``:
    ACP is the harness's session adapter (``capabilities.session`` in
    harness.json over the fallback table) and its ACP command runs
    here. A harness driven another way (claude: its stream adapter)
    takes ACP only when asked for by name."""
    from swe.remote.adapters import registry as adapters

    resolved = adapters.adapter_for(name, reg, which=None)
    return getattr(resolved, "adapter", "") == ACP_ADAPTER \
        and command_for(name, reg) is not None


# ---------------------------------------------------------------------
# One agent process


class AcpError(Exception):
    pass


class NoLoadSession(AcpError):
    """The agent does not advertise ``loadSession`` and the caller
    refused a new session (an attach: the chat tails the attached
    session's transcript, which a new ACP session never writes)."""


# ``Connection.stop_waiting``: the turn is still running.
PENDING = object()


# PermissionOptionKind per hub decision, in preference order.
_DECISION_KINDS = {
    "allow": ("allow_once", "allow_always"),
    "always": ("allow_always",),
    "deny": ("reject_once", "reject_always"),
}

# The update kinds that stream, and the RemoteMessage kind each becomes
# (``message.delta``'s ``kind``; the transcript's final record of the
# same kind replaces the bubble).
_CHUNK_KINDS = {
    "agent_message_chunk": "assistant",
    "agent_thought_chunk": "thinking",
}

# Events kept for a connection nobody listens to yet.
_HELD_MAX = 64
_HELD_HARD_MAX = 1024


def _informational(data: dict) -> bool:
    """An event only recording what Autonomy answered by itself."""
    return bool(data.get("auto")) or data.get("source") == "autonomy"

# JSON-RPC "method not found".
_NO_METHOD = -32601


class Connection:
    """One ACP agent child: a writer lock, a reader thread, the session
    it serves, and the turn and permission requests in flight."""

    def __init__(self, argv: list[str], cwd: str, env: dict | None = None,
                 *, spawn=None, auto_allow: bool = False) -> None:
        self.argv = list(argv)
        # Autonomy (a launch's ``yolo``): each permission request is
        # answered with the agent's allow-once option, never an
        # "always" one; a request without one gets the usual card.
        self.auto_allow = auto_allow
        self.cwd = cwd
        popen = spawn or subprocess.Popen
        self.proc = popen(
            self.argv, cwd=cwd or None,
            env={**os.environ, **env} if env else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.wlock = threading.Lock()
        self.lock = threading.Lock()
        self.next_id = 1
        self.replies: dict = {}
        self.dead = False
        self.session_id = ""
        self.resumed = False
        self.agent_caps: dict = {}
        self.agent_name = ""
        self.agent_version = ""
        # What session/new or session/load answered (its modes, models).
        self.session_info: dict = {}
        # True while session/load replays history: those updates are
        # the past, not live text.
        self.replaying = False
        # The in-flight session/prompt's rpc id, and an event set when no
        # turn is running.
        self.turn_rpc = None
        self.idle = threading.Event()
        self.idle.set()
        self.last_stop = ""
        self.last_error = ""
        # Set once send() stopped waiting on the running turn: an error
        # answer after that has nobody to report it but an event.
        self._report_late = False
        # request_id -> (rpc id, options) for open permission requests.
        self.approvals: dict[str, tuple[object, list]] = {}
        # Live sinks, bound by the injector (the hub's emit and
        # DeltaStream); None until something listens.
        self.key = ""
        self.machine = ""
        self.emit = None
        self.deltas = None
        self._held: list[tuple[str, dict]] = []
        # toolCallId -> {title, rawInput} from session/update.
        self._tool_info: dict[str, dict] = {}
        self._flush_lock = threading.Lock()
        # The stream id currently growing per kind, and tool calls whose
        # title streams until they finish.
        self._streams: dict[str, str] = {}
        self._stream_mids: dict[str, str] = {}
        self._stream_no = 0
        self._tools: set[str] = set()
        self._turn_no = 0
        # The turn session/cancel was sent for (-1: none yet).
        self._cancelled_turn = -1
        # Stream ids carry this, so a restarted agent's ids never repeat
        # ones the clients already closed.
        self._nonce = secrets.token_hex(4)
        threading.Thread(target=self._read_loop, daemon=True,
                         name=f"acp-{os.path.basename(self.argv[0])}").start()

    # -- sinks -----------------------------------------------------------

    def bind(self, key: str, machine: str, emit, deltas) -> None:
        """Point the live sinks at ``key``. Events raised while no emit
        was bound (a launch's first turn, before its session has a
        tailer) go out now, in order."""
        with self.lock:
            self.key = key
            if machine:
                self.machine = machine
            if deltas is not None:
                self.deltas = deltas
        if emit is None:
            return
        # Flush what was held, oldest first, and only then let the
        # reader send directly: an event raised during the flush (the
        # request's own approval.resolved) queues behind the rest. One
        # flush at a time, so two binds can't interleave their halves.
        with self._flush_lock:
            while True:
                with self.lock:
                    held, self._held = self._held, []
                    if not held:
                        self.emit = emit
                        return
                for etype, data in held:
                    self._send_event(emit, etype, data)

    def _emit(self, etype: str, data: dict) -> None:
        with self.lock:
            emit = self.emit
            if emit is None:
                # Nobody listens yet. A launched session's journal must
                # stay empty until its tailer starts (a journal with no
                # cursor makes the tailer skip the first turn), so the
                # event waits for bind().
                # Past the soft cap only the record of auto-answered
                # requests is shed: a request someone must answer is
                # always kept (open requests bound it), up to a hard cap.
                full = len(self._held) >= _HELD_MAX
                if full and (_informational(data)
                             or len(self._held) >= _HELD_HARD_MAX):
                    return
                self._held.append((etype, data))
                return
        self._send_event(emit, etype, data)

    def _send_event(self, emit, etype: str, data: dict) -> None:
        if not self.key:
            return
        from swe.remote import protocol

        try:
            emit(self.key, protocol.event(etype, self.key, self.machine,
                                          data))
        except Exception:
            pass

    # -- wire ------------------------------------------------------------

    def _write(self, payload: dict) -> bool:
        line = json.dumps(payload).encode() + b"\n"
        with self.wlock:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError, AssertionError):
                return False

    def request(self, method: str, params: dict,
                turn: bool = False) -> tuple[object, queue.Queue]:
        """Send a request; ``(rpc id, queue the response lands on)``.
        ``turn`` marks it as the running turn before it is written, so
        an answer that comes back at once still ends the turn. Raises
        AcpError when the agent can't be written to."""
        q: queue.Queue = queue.Queue()
        with self.lock:
            rpc_id = self.next_id
            self.next_id += 1
            self.replies[rpc_id] = q
            if turn:
                self.turn_rpc = rpc_id
        if self.dead or not self._write({
                "jsonrpc": "2.0", "id": rpc_id,
                "method": method, "params": params}):
            with self.lock:
                self.replies.pop(rpc_id, None)
                if turn and self.turn_rpc == rpc_id:
                    self.turn_rpc = None
            raise AcpError(f"{self.label} is not reachable")
        return rpc_id, q

    def call(self, method: str, params: dict,
             timeout: float = CALL_TIMEOUT_S) -> dict:
        """One request/response; the ``result`` dict. Raises AcpError on
        a timeout, an error response or a dead agent."""
        rpc_id, q = self.request(method, params)
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self.lock:
                self.replies.pop(rpc_id, None)
            raise AcpError(f"{method} timed out") from None
        if msg is None:
            raise AcpError(f"{self.label} exited during {method}")
        if "error" in msg:
            raise AcpError(f"{method} failed: {_error_text(msg['error'])}")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict) -> bool:
        return self._write({"jsonrpc": "2.0", "method": method,
                            "params": params})

    def _respond(self, rpc_id, result=None, error=None) -> bool:
        msg: dict = {"jsonrpc": "2.0", "id": rpc_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        return self._write(msg)

    @property
    def label(self) -> str:
        return self.agent_name or os.path.basename(self.argv[0])

    # -- handshake -------------------------------------------------------

    def handshake(self, session_ref: str, *, allow_new: bool = True,
                  budget: float | None = None) -> str:
        """initialize, then load ``session_ref`` or (``allow_new``) start
        a new session. ``budget`` caps the whole handshake in seconds.
        Returns "" after a load, else a note saying the session is new;
        raises AcpError when the agent can't serve."""
        from swe import __version__

        deadline = None if budget is None else time.monotonic() + budget

        def within(limit: float) -> float:
            if deadline is None:
                return limit
            left = deadline - time.monotonic()
            if left <= 0:
                raise AcpError("the agent took too long to start")
            return min(limit, left)

        hello = self.call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            # We run no editor: the agent uses its own file and terminal
            # tools rather than asking us for them.
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "swe", "version": __version__},
        }, timeout=within(INIT_TIMEOUT_S))
        caps = hello.get("agentCapabilities")
        self.agent_caps = caps if isinstance(caps, dict) else {}
        info = hello.get("agentInfo")
        if isinstance(info, dict) and info.get("name"):
            self.agent_name = str(info["name"])[:64]
        if isinstance(info, dict) and isinstance(info.get("version"), str):
            self.agent_version = info["version"][:32]
        cwd = os.path.realpath(self.cwd or ".")
        if self.agent_caps.get("loadSession") and session_ref:
            self.replaying = True
            try:
                self.session_info = self.call("session/load", {
                    "sessionId": session_ref, "cwd": cwd,
                    "mcpServers": [],
                }, timeout=within(LOAD_TIMEOUT_S))
            finally:
                self.replaying = False
                self._streams.clear()
                self._stream_mids.clear()
            self.session_id = session_ref
            self.resumed = True
            return ""
        if not allow_new:
            raise NoLoadSession(
                f"{self.label} can't load sessions over ACP, and a new "
                "ACP session would not write to this chat")
        made = self.call("session/new", {"cwd": cwd, "mcpServers": []},
                         timeout=within(INIT_TIMEOUT_S))
        sid = made.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise AcpError("session/new returned no sessionId")
        self.session_info = made
        self.session_id = sid
        self.resumed = False
        return (f"{self.label} can't load sessions over ACP, so this is "
                f"a new session ({sid}): the earlier conversation is not "
                "in its context, and its transcript is that session's, "
                "not this chat's")

    # -- turns -----------------------------------------------------------

    def prompt(self, text: str,
               blocks: list | None = None) -> queue.Queue:
        """Start a turn; the queue gets the session/prompt response (or
        None if the agent dies). ``blocks`` are more content blocks
        after the text (attached files as ``resource_link``). Raises
        AcpError when one is running."""
        with self.lock:
            if self.turn_rpc is not None or not self.idle.is_set():
                raise AcpError("a turn is already running")
            self._turn_no += 1
            self.idle.clear()
        try:
            _rpc_id, q = self.request("session/prompt", {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}, *(blocks or ())],
            }, turn=True)
        except AcpError:
            self.idle.set()
            raise
        return q

    def stop_waiting(self, q: queue.Queue):
        """send() is done waiting on the turn behind ``q``: the answer if
        it arrived meanwhile (None when the agent died), else
        ``PENDING``, and a later error answer is reported as a
        ``notify`` error event instead."""
        with self.lock:
            if self.turn_rpc is not None:
                self._report_late = True
                return PENDING
        try:
            return q.get_nowait()
        except queue.Empty:
            return PENDING

    def cancel(self) -> bool:
        """session/cancel, and every open permission request answered
        ``cancelled`` (the spec's rule for a cancelled turn)."""
        if self.turn_rpc is None and not self.approvals:
            return False
        # A permission request still in flight from this turn is answered
        # ``cancelled`` too, never auto-allowed after the Stop.
        with self.lock:
            self._cancelled_turn = self._turn_no
        ok = self.notify("session/cancel", {"sessionId": self.session_id})
        self._retire_approvals("cancelled", answer=True)
        return ok

    def answer(self, request_id: str, decision: str) -> tuple[bool, str]:
        """Answer one open permission request with the hub's decision:
        a menu row number, or allow|always|deny by option kind."""
        with self.lock:
            entry = self.approvals.get(request_id)
        if entry is None:
            return False, "unknown or already-answered approval request"
        rpc_id, options = entry
        chosen = None
        if decision.isdigit():
            n = int(decision)
            if 1 <= n <= len(options):
                chosen = options[n - 1]
            else:
                return False, "that menu option is not offered"
        else:
            for kind in _DECISION_KINDS.get(decision, ()):
                chosen = next((o for o in options
                               if o.get("kind") == kind), None)
                if chosen is not None:
                    break
            if chosen is None:
                return False, (f"this request offers no {decision} option; "
                               "answer with a menu row")
        with self.lock:
            if self.approvals.pop(request_id, None) is None:
                return False, "unknown or already-answered approval request"
        if not self._respond(rpc_id, {"outcome": {
                "outcome": "selected", "optionId": chosen["optionId"]}}):
            with self.lock:
                self.approvals[request_id] = entry
            return False, f"{self.label} is not reachable"
        return True, f"selected {chosen.get('name') or chosen['optionId']}"

    def close(self) -> None:
        """End the agent: EOF on its stdin first, so it can finish what it
        read and exit on its own; terminate, then kill, if it lingers."""
        self.dead = True
        with self.lock:
            # Closed on purpose: not an exit worth a notification.
            self._report_late = False
        with self.wlock:
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=_CLOSE_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass

    def alive(self) -> bool:
        return not self.dead and self.proc.poll() is None

    # -- reader ----------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            assert self.proc.stdout is not None
            for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self._dispatch(msg)
                except Exception:
                    # One odd message is skipped; ending the reader would
                    # strand every later reply.
                    continue
        except Exception:
            pass
        try:
            if self.proc.stdout is not None:
                self.proc.stdout.close()
        except Exception:
            pass
        self._closed()

    def _closed(self) -> None:
        """The agent exited or its pipe broke: wake every waiter, close
        what was streaming, retire what can no longer be answered."""
        self.dead = True
        with self.lock:
            waiting = list(self.replies.values())
            self.replies.clear()
            late, self._report_late = self._report_late, False
            self.turn_rpc = None
        if late:
            self._emit("notify", {
                "kind": "error", "status": "error",
                "message": f"{self.label} exited mid-turn"})
        for q in waiting:
            q.put(None)
        self._finish_turn()
        self._retire_approvals("channel_closed", answer=False)

    def _dispatch(self, msg: dict) -> None:
        method = msg.get("method")
        if isinstance(method, str) and method:
            params = msg.get("params")
            params = params if isinstance(params, dict) else {}
            if "id" in msg and msg["id"] is not None:
                self._agent_request(msg["id"], method, params)
            elif method == "session/update":
                self._update(params)
            return
        rpc_id = msg.get("id")
        if rpc_id is None:
            return
        try:
            with self.lock:
                q = self.replies.pop(rpc_id, None)
                ended = rpc_id == self.turn_rpc
                late = False
                if ended:
                    self.turn_rpc = None
                    late, self._report_late = self._report_late, False
        except TypeError:  # an unhashable id
            return
        if ended:
            result = msg.get("result")
            if isinstance(result, dict):
                self.last_stop = str(result.get("stopReason") or "")
            self.last_error = (_error_text(msg["error"])
                               if "error" in msg else "")
            if late and self.last_error:
                self._emit("notify", {
                    "kind": "error", "status": "error",
                    "message": f"{self.label}: {self.last_error}"[:280]})
            self._finish_turn()
            self._retire_approvals("turn_ended", answer=True)
        if q is not None:
            q.put(msg)

    def _agent_request(self, rpc_id, method: str, params: dict) -> None:
        if method == "session/request_permission":
            self._permission(rpc_id, params)
            return
        # fs/*, terminal/* and anything newer: we advertised none of it.
        self._respond(rpc_id, error={
            "code": _NO_METHOD, "message": f"{method} is not supported"})

    def _permission(self, rpc_id, params: dict) -> None:
        raw = params.get("options")
        options = [o for o in raw if isinstance(o, dict)
                   and isinstance(o.get("optionId"), str)] \
            if isinstance(raw, list) else []
        if not options or (params.get("sessionId") not in
                           (None, self.session_id)):
            self._respond(rpc_id, {"outcome": {"outcome": "cancelled"}})
            return
        with self.lock:
            stopped = self._cancelled_turn == self._turn_no
        if stopped:
            # The turn was cancelled: nothing more is granted in it.
            self._respond(rpc_id, {"outcome": {"outcome": "cancelled"}})
            return
        request_id = f"acp-{secrets.token_hex(6)}"
        once = next((o for o in options if o.get("kind") == "allow_once"),
                    None) if self.auto_allow else None
        if once is None:
            with self.lock:
                self.approvals[request_id] = (rpc_id, options)
        prompt = _permission_prompt(params.get("toolCall"), self._tool_info)
        request = {
            "request_id": request_id,
            "prompt": _tr._redact_secrets(prompt),
            "options": [_menu_row(i, o) for i, o in enumerate(options, 1)],
        }
        if once is None:
            self._emit("approval.requested", request)
            return
        # Answered here, and the pair journaled so the apps show what
        # was allowed: the request (``auto``: no push, no inbox wait)
        # and its resolve, naming the row that was picked.
        if not self._respond(rpc_id, {"outcome": {
                "outcome": "selected", "optionId": once["optionId"]}}):
            return
        self._emit("approval.requested", {**request, "auto": True})
        self._emit("approval.resolved", {
            "request_id": request_id, "decision": "allow",
            "source": "autonomy"})

    def _remember_tool(self, update: dict) -> None:
        """Keep each tool call's title and input by id: a permission
        request may name only the id (devin sends ``toolCallId`` and
        vendor ``_meta``), and the card should still say what it runs."""
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return
        info = self._tool_info.setdefault(call_id, {})
        for field in ("title", "rawInput"):
            value = update.get(field)
            if value:
                info[field] = value
        while len(self._tool_info) > _TOOL_INFO_MAX:
            self._tool_info.pop(next(iter(self._tool_info)))

    def _retire_approvals(self, source: str, *, answer: bool) -> None:
        with self.lock:
            open_ = list(self.approvals.items())
            self.approvals.clear()
        for request_id, (rpc_id, _options) in open_:
            if answer:
                self._respond(rpc_id, {"outcome": {"outcome": "cancelled"}})
            self._emit("approval.resolved",
                       {"request_id": request_id, "source": source})

    # -- live text -------------------------------------------------------

    def _update(self, params: dict) -> None:
        if self.replaying:
            return
        if params.get("sessionId") not in (None, self.session_id):
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        what = update.get("sessionUpdate")
        if what in ("tool_call", "tool_call_update"):
            self._remember_tool(update)
        deltas = self.deltas
        if deltas is None or not self.key:
            return
        if what in _CHUNK_KINDS:
            content = update.get("content")
            if not isinstance(content, dict) or content.get("type") != "text":
                return
            text = content.get("text")
            if not isinstance(text, str) or not text:
                return
            kind = _CHUNK_KINDS[what]
            mid = update.get("messageId")
            mid = mid if isinstance(mid, str) and mid else ""
            current = self._streams.get(kind)
            # A chunk of another kind (or a tool call) in between starts
            # the next message: the transcript writes them apart too.
            for other, sid in list(self._streams.items()):
                if other != kind:
                    deltas.finish(self.key, sid)
                    self._streams.pop(other, None)
                    self._stream_mids.pop(other, None)
            if current is None or (mid and mid != self._stream_mids.get(kind)):
                if current is not None:
                    deltas.finish(self.key, current)
                # Never the agent's messageId alone: an agent may reuse
                # one across turns or after a tool call, and an id that
                # was finished never streams again on the clients.
                self._stream_no += 1
                current = (f"acp:{self._nonce}:{self._turn_no}:"
                           f"{self._stream_no}:{kind}")
                self._streams[kind] = current
                self._stream_mids[kind] = mid
            deltas.push(self.key, self.machine, current, kind, text)
        elif what in ("tool_call", "tool_call_update"):
            call_id = update.get("toolCallId")
            if not isinstance(call_id, str) or not call_id:
                return
            stream = (f"acp:{self._nonce}:{self._turn_no}:"
                      f"tool:{call_id}")
            if what == "tool_call":
                for sid in self._streams.values():
                    deltas.finish(self.key, sid)
                self._streams.clear()
                self._stream_mids.clear()
                title = update.get("title")
                if isinstance(title, str) and title:
                    self._tools.add(stream)
                    deltas.push(self.key, self.machine, stream, "tool_use",
                                title)
            if update.get("status") in ("completed", "failed") \
                    and stream in self._tools:
                self._tools.discard(stream)
                deltas.finish(self.key, stream)

    def _finish_turn(self) -> None:
        self._streams.clear()
        self._stream_mids.clear()
        self._tools.clear()
        self.idle.set()
        deltas = self.deltas
        if deltas is not None and self.key:
            deltas.finish_all(self.key)


# Tool calls remembered for permission prompts; the oldest go first.
_TOOL_INFO_MAX = 256


def _permission_prompt(tool, known: dict) -> str:
    """What a permission card says: the tool call's title, else what an
    earlier ``tool_call`` update said about the same id, plus the
    command or path it acts on. A ``_meta`` field whose name ends in
    ``command`` (devin's ``cognition.ai/editableCommand``) stands in for
    an input the agent sent no other way."""
    tool = tool if isinstance(tool, dict) else {}
    seen = known.get(tool.get("toolCallId")) or {} \
        if isinstance(tool.get("toolCallId"), str) else {}
    title = tool.get("title") or seen.get("title")
    raw_input = tool.get("rawInput") or seen.get("rawInput")
    detail = None
    if isinstance(raw_input, dict):
        detail = raw_input.get("command") or raw_input.get("path") \
            or raw_input.get("file_path")
        if isinstance(detail, list):
            detail = " ".join(str(p) for p in detail)
    meta = tool.get("_meta")
    if not detail and isinstance(meta, dict):
        detail = next((v for k, v in meta.items()
                       if isinstance(k, str) and isinstance(v, str)
                       and k.lower().endswith("command") and v), None)
    prompt = str(title or ("Run a command" if detail
                           else "permission requested"))
    if isinstance(detail, str) and detail and detail not in prompt:
        prompt += f"\n\n`{detail[:500]}`"
    return prompt


# Option kinds whose answer outlives this one request.
_STANDING_KINDS = frozenset({"allow_always", "reject_always"})


def _menu_row(n: int, option: dict) -> dict:
    """One approval menu row for an ACP permission option. ``standing``
    marks a grant (or refusal) that outlives the request, by the
    option's kind rather than its label, so a notification never puts
    it behind a button whatever language the agent names it in."""
    row = {"n": str(n), "label": _tr._redact_secrets(
        str(option.get("name") or option["optionId"]))[:140]}
    kind = option.get("kind")
    if isinstance(kind, str) and kind:
        row["kind"] = kind[:32]
        if kind in _STANDING_KINDS:
            row["standing"] = True
    return row


def _error_text(err) -> str:
    if isinstance(err, dict):
        text = str(err.get("message") or err.get("code") or "error")
    else:
        text = str(err or "error")
    return _tr._redact_secrets(text)[:300]
