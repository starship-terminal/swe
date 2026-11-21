"""Write path: the phone's commands into a supervised session.

Registry keyed by harness, same shape as the parser/reader registries.
The channels:

- ``ClaudeHeadlessInjector`` (tier A): one ``claude -p --resume <id>
  --output-format stream-json --include-partial-messages`` invocation
  per prompt. The transcript file gets the appended turn, so the tailer
  picks up the same ``message.appended`` events as a TUI run; the
  partial-message lines on stdout feed ``message.delta`` only.
  Permissions are fixed per invocation (print mode has no mid-turn
  prompts), so approvals answer ``approval_not_supported``.
- ``TmuxInjector`` (tier B): ``send-keys`` for prompts, menu digits for
  approvals, Escape for interrupt — best-effort against whatever TUI is
  running under the supervisor.
- ``OpencodeServeInjector`` (tier A): REST against a managed ``opencode
  serve`` bound to the session's project — ``prompt_async`` for
  messages, permission replies for approvals, ``abort`` for interrupt.
  A background ``GET /event`` SSE subscription translates permission
  asks into ``approval.requested`` events.
- ``CodexAppServerInjector`` (tier A): a persistent ``codex app-server``
  JSON-RPC process per session — ``thread/resume`` once, ``turn/start``
  per message, ``turn/interrupt`` for interrupt; server-initiated
  approval requests become ``approval.requested`` and decisions are
  written back as JSON-RPC responses.
- ``AcpInjector``: any harness with an Agent Client Protocol mode
  (``devin acp``, ``cursor-agent acp``, ``claude-agent-acp``, ...), one
  agent process per session over ``acp.Connection`` — ``session/load``
  once, ``session/prompt`` per message, ``session/cancel`` for
  interrupt; ``session/request_permission`` becomes
  ``approval.requested`` with the agent's options as menu rows.

``queue|steer``: queue refuses while a turn looks active; steer writes
immediately where the channel accepts mid-turn input (tmux buffers
keystrokes, app-server queues server-side). Headless invocations are
atomic — steer degrades to queue and one run is in flight per session
at a time; a degraded send reports ``degraded=True`` so the client can
say so.

Live text: the API channels push each text chunk they read into a
``stream.DeltaStream`` (``deltas``), which coalesces them into
``message.delta`` events that are never journaled. ACP also streams
thought chunks (``thinking``) and tool-call titles (``tool_use``),
which clients keep for matching but do not draw. The transcript's
``message.appended`` stays the record; tmux panes never stream.
"""

from __future__ import annotations

import collections
import json
import os
import select
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

from swe.remote import attachments, supervise
from swe.remote.adapters import codex_wire, opencode_wire
from swe.remote.launch_model import provider_split
from swe.reports import transcripts as _tr
from swe.sessions.models import Session
from swe.sessions.resume import account_env

# A headless claude run gets this long before we kill it; a queued send
# waits on the in-flight run with half that patience.
HEADLESS_TIMEOUT_S = 300
_QUEUE_WAIT_S = 150

# Fallback when the pane's option rows don't parse: claude's order —
# 1 allow once, 2 always allow, 3 deny. Numbered menus are answered by
# label via ``supervise.menu_key_for`` instead, because positions vary
# (devin puts "No" at 8). Other TUIs ignore stray digits harmlessly.
_APPROVAL_KEYS = {"allow": "1", "always": "2", "deny": "3"}

_OPS_TEXT = {
    "compact": "/compact",
    "clear": "/clear",
    "model": "/model {value}",
    "effort": "/effort {value}",
}


def control_text(op: str, value: str) -> str:
    """The pane text a control op types (``/model sonnet``); "" for an
    op the TUI has no line for."""
    template = _OPS_TEXT.get(op)
    if template is None:
        return ""
    return template.format(value=value).rstrip()


def switch_model(home: Path | None, session_key: str, mode: str,
                 tool: str, value: str) -> InjectResult:
    """The ``model`` control op on an API channel: record the pick on
    the session's supervised row, which the channel's next turn reads
    (the same field an attach-time pick lands in). Nothing is sent now,
    so the switch applies from the next turn, mid-turn or not. The
    value is held to the launch rule and to the channel's own shape
    rule (``supervise.channel_tuning_error``)."""
    from swe.remote import protocol

    try:
        value = protocol._launch_value({"value": value}, "value") or ""
    except ValueError as exc:
        return InjectResult(False, str(exc))
    if not value:
        return InjectResult(False, "model needs a value")
    blocked = supervise.channel_tuning_error(mode, tool, value, None)
    if blocked:
        return InjectResult(False, blocked)
    if not supervise.set_meta(home, session_key, "model", value):
        return InjectResult(
            False, "session detached or its row could not be saved; "
                   "model unchanged")
    return InjectResult(True, f"model set to {value} from the next turn")


def _prompt(session: Session, cmd: dict, harness: str = "") -> str:
    """The message text with its attachments referenced the way the
    harness takes them (``@path`` or a bracketed path). A channel tied
    to one harness names it; a tmux pane runs whatever the session's
    harness is."""
    return attachments.prompt_text(
        str(cmd.get("text") or ""),
        [str(p) for p in cmd.get("attachments") or []],
        harness or getattr(session, "tool_name", "") or "",
    )


class InjectResult(NamedTuple):
    ok: bool
    note: str = ""
    # steer asked for mid-turn delivery but the channel queued it —
    # clients surface this as "sent (queued)" instead of pretending.
    degraded: bool = False


class TmuxInjector:
    """Tier B: everything is a keystroke into the supervised pane."""

    # request_ids already answered — a retried approval must not answer
    # whichever menu happens to be on screen now. Class-level because an
    # injector is built per request; bounded so a long-lived hub does
    # not grow it forever.
    _answered: dict[str, "collections.deque[str]"] = {}

    def __init__(self, home: Path | None, session_key: str) -> None:
        self.home = home
        self.key = session_key
        # Adopted sessions live under a launch-time tmux name, not the
        # derived one — channel_name resolves either.
        self.name = supervise.channel_name(home, session_key)

    def send(self, session: Session, cmd: dict, status: str) -> InjectResult:
        if cmd.get("mode") == "queue" and status == "active":
            return InjectResult(
                False, "session is mid-turn; steer to write anyway"
            )
        text = _prompt(session, cmd)
        if not text.strip():
            return InjectResult(False, "empty message")
        # A detach could land between the route's ownership check and
        # this write; re-check under the pane lock's read side.
        if supervise.meta_for(self.home, self.key) is None:
            return InjectResult(
                False, "session detached; refusing the write"
            )
        if supervise.send_text(self.name, text):
            return InjectResult(True, "sent to tmux pane")
        return InjectResult(False, "tmux session is not reachable")

    def approval(self, session: Session, request_id: str, decision: str) -> InjectResult:
        # A semantic name maps to a menu row; a bare digit is the client
        # answering the exact numbered option the event listed.
        if decision not in _APPROVAL_KEYS and not (
            decision.isdigit() and len(decision) <= 2
        ):
            return InjectResult(False, f"unknown decision {decision}")
        # Checks, pane capture, the key walk and the answered mark are
        # one critical section per pane — a concurrent reply must see
        # either the request still open or the menu already consumed.
        # It is send_text's own pane lock, so a message steered in
        # meanwhile can't land between the walk's arrow keys either.
        with supervise._pane_lock(self.name):
            return self._approval(session, request_id, decision)

    def _approval(self, session: Session, request_id: str, decision: str) -> InjectResult:
        # The TUI menu does not display request ids, so we cannot bind a
        # reply to *the* on-screen prompt. What we can bind: reject a
        # request_id we already answered, and require a visible
        # permission prompt before sending a menu key — a stale retry
        # then can't consume a different pending menu under its old id.
        answered = self._answered.setdefault(
            self.key, collections.deque(maxlen=512)
        )
        if request_id in answered:
            return InjectResult(False, "approval already answered")
        # When the tailer issued a request_id for the prompt currently
        # on screen, only that id may answer it — a stale or superseded
        # id cannot consume the new menu.
        # The id and its fingerprint are read once, together: the
        # watcher may clear or replace them while the pane is captured.
        snap = supervise.pending_entry(self.key)
        outstanding, fp = snap if snap else (None, None)
        if (outstanding and request_id != outstanding) \
                or supervise.approval_retired(self.key, request_id):
            return InjectResult(False, "stale approval request id")
        pane = supervise.capture_pane(self.name)
        if supervise.pending_entry(self.key) != snap:
            # Answered at the terminal, or a new prompt named, while
            # capturing: the screen may already be the next prompt.
            return InjectResult(
                False, "approval prompt changed since the request"
            )
        if not supervise.pane_shows_approval(pane):
            return InjectResult(
                False, "no approval prompt is on screen"
            )
        # The id was minted for the prompt that was on screen then —
        # re-fingerprint now so a menu swapped in between polls can't
        # be answered by the old id.
        if fp and fp != supervise.approval_fingerprint(pane):
            return InjectResult(
                False, "approval prompt changed since the request"
            )
        if decision.isdigit():
            # Only a row the menu actually shows may be sent — a digit
            # that isn't listed can't reach into an unrelated prompt.
            rows, kind, cursor = supervise.option_menu(pane)
            offered = {n for n, _ in rows}
            if decision not in offered:
                return InjectResult(
                    False, "that menu option is not on screen"
                )
            if kind == "choices":
                # An unnumbered arrow-select list takes no digit key —
                # walk the cursor onto the row and confirm.
                if self._send_choice_keys(
                        rows, int(decision) - 1, cursor):
                    answered.append(request_id)
                    return InjectResult(
                        True, f"selected menu option {decision}"
                    )
                return InjectResult(
                    False, "tmux session is not reachable"
                )
            key = decision
        else:
            rows, kind, _cursor = supervise.option_menu(pane)
            if kind == "choices" and rows:
                # An arrow-select list has no allow/always mapping —
                # only "deny" carries over, as the menu's own cancel.
                if decision != "deny":
                    return InjectResult(
                        False,
                        "this prompt takes a menu option, not a decision",
                    )
                if supervise.send_key(self.name, "Escape"):
                    answered.append(request_id)
                    return InjectResult(True, "escape sent")
                return InjectResult(
                    False, "tmux session is not reachable"
                )
            key = supervise.menu_key_for(pane, decision) \
                or _APPROVAL_KEYS[decision]
        if supervise.send_key(self.name, key):
            answered.append(request_id)
            return InjectResult(True, f"sent menu key {key}")
        return InjectResult(False, "tmux session is not reachable")

    def _send_choice_keys(
        self, rows: list, target: int, cursor: int | None
    ) -> bool:
        """Arrow to the 0-based ``target`` row and confirm.

        The cursor-marked row anchors the walk; without one, saturating
        Up lands on row 1 from wherever the list sits.
        """
        if cursor is not None:
            delta = target - cursor
            keys = ["Down" if delta > 0 else "Up"] * abs(delta)
        else:
            keys = ["Up"] * len(rows) + ["Down"] * target
        for key in keys + ["Enter"]:
            if not supervise.send_key(self.name, key):
                return False
        return True

    def control(self, session: Session, op: str, value: str) -> InjectResult:
        if op == "interrupt":
            ok = supervise.send_key(self.name, "Escape")
            return InjectResult(ok, "escape sent" if ok else "pane unreachable")
        if op in {"kill", "detach"}:
            return InjectResult(
                supervise.detach(self.home, self.key),
                "supervisor stopped; session is observed again",
            )
        if op in {"model", "effort"}:
            from swe.remote import slash_builtins

            # Only a TUI verified to run /<op> <value> gets it typed: in
            # any other, the line would reach the model as a prompt. The
            # same rule model_modes advertises and launch_tuning_error
            # applies at launch and attach.
            if not slash_builtins.launch_op_ok(session.tool_name, op):
                return InjectResult(
                    False, f"unsupported_control: "
                           f"{session.tool_name or 'this harness'}'s TUI "
                           f"has no /{op} that takes a value")
        text = control_text(op, value)
        if not text:
            return InjectResult(False, f"unsupported control {op}")
        ok = supervise.send_text(self.name, text)
        return InjectResult(ok, f"sent {text!r}" if ok else "pane unreachable")


class ClaudeHeadlessInjector:
    """Tier A: one print-mode invocation per prompt, serialized."""

    _locks: dict[str, threading.Lock] = {}
    _procs: dict[str, subprocess.Popen] = {}
    # Cleared once a claude refuses --include-partial-messages (a build
    # from before the flag): runs go on without live text.
    _partial = True

    def __init__(self, home: Path | None, session_key: str,
                 deltas=None, machine: str = "") -> None:
        self.home = home
        self.key = session_key
        self.deltas = deltas
        self.machine = machine
        # Set when the last run's claude refused --include-partial-messages
        # at argv parsing, before any JSON record (so before any work).
        self._flag_refused = False
        self._locks.setdefault(session_key, threading.Lock())

    @staticmethod
    def argv(session: Session, text: str, model: str = "",
             partial: bool = True) -> list[str]:
        args = [
            "claude", "-p", "--resume", session.session_id,
            "--output-format", "stream-json",
            "--verbose",
        ]
        # Token-level ``stream_event`` lines on stdout, for live text.
        # They change nothing on disk: the session's JSONL transcript
        # and --resume behave as without the flag.
        if partial:
            args.append("--include-partial-messages")
        # A launch-time pick recorded on the session's meta rides every
        # print-mode invocation as ``--model`` — the headless channel's
        # own mechanism for the launch ``model`` field.
        if model:
            args += ["--model", model]
        # "--" ends the flags: a message that starts with "-" (a pasted
        # list, "--help") is the prompt, not an option.
        return [*args, "--", text]

    def _run(self, session: Session, argv: list[str]) -> tuple[bool, str]:
        env = account_env(session)
        try:
            proc = subprocess.Popen(
                argv, cwd=session.path or None,
                env={**os.environ, **env} if env else None,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            return False, f"claude failed to launch: {exc}"
        self._procs[self.key] = proc
        # A detach that landed between Popen() and registration missed
        # this child — kill it ourselves rather than run detached.
        if supervise.meta_for(self.home, self.key) is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            self._procs.pop(self.key, None)
            return False, "session detached; refusing the write"
        # The stdout loop can block mid-read forever on a stalled run, so
        # the deadline lives in a watchdog: kill() closes the pipe and
        # ends the iteration.
        timed_out = {"hit": False}

        def _expire() -> None:
            timed_out["hit"] = True
            try:
                proc.kill()
            except OSError:
                pass

        watchdog = threading.Timer(HEADLESS_TIMEOUT_S, _expire)
        watchdog.daemon = True
        watchdog.start()
        self._flag_refused = False
        saw_json = False
        live = _ClaudeLive(self.deltas, self.key, self.machine)
        try:
            result_text = ""
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    if not saw_json and _refuses_partial_flag(line):
                        self._flag_refused = True
                    continue
                saw_json = True
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") == "stream_event":
                    live.feed(rec)
                    continue
                if rec.get("type") == "result":
                    result_text = str(rec.get("result") or "")
                    if rec.get("is_error"):
                        return False, result_text or "claude reported an error"
            if timed_out["hit"]:
                return False, "headless run timed out"
            rc = proc.wait(timeout=10)
            if rc != 0:
                return False, result_text or f"claude exited {rc}"
            return True, result_text or "turn complete"
        except subprocess.TimeoutExpired:
            proc.kill()
            return False, "headless run timed out"
        finally:
            watchdog.cancel()
            live.close()
            # Reap whatever the run left: an errored or killed child
            # otherwise stays a zombie with an open pipe.
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            self._procs.pop(self.key, None)

    def send(self, session: Session, cmd: dict, status: str) -> InjectResult:
        text = _prompt(session, cmd, "claude")
        if not text.strip():
            return InjectResult(False, "empty message")
        # steer degrades to queue: a print-mode run is atomic.
        degraded = cmd.get("mode") == "steer"
        lock = self._locks[self.key]
        if not lock.acquire(timeout=_QUEUE_WAIT_S):
            return InjectResult(False, "a headless turn is still in flight")
        try:
            # A detach could land between the route's ownership check and
            # this write; under the session lock the registry is the
            # last word.
            meta = supervise.meta_for(self.home, self.key)
            if meta is None:
                return InjectResult(
                    False, "session detached; refusing the write"
                )
            model = str(meta.get("model") or "")
            partial = ClaudeHeadlessInjector._partial
            ok, note = self._run(session, self.argv(
                session, text, model, partial=partial))
            if not ok and partial and self._flag_refused:
                # This claude predates the flag and stopped at argv
                # parsing, before it printed a record or reached the
                # model: run the turn again without live text.
                ClaudeHeadlessInjector._partial = False
                ok, note = self._run(session, self.argv(
                    session, text, model, partial=False))
            if ok and degraded:
                note = (note or "turn complete") + \
                    " (steer queued: headless runs are atomic)"
            return InjectResult(ok, note, degraded=ok and degraded)
        finally:
            lock.release()

    def approval(self, session: Session, request_id: str, decision: str) -> InjectResult:
        return InjectResult(
            False, "approval_not_supported: headless permissions are "
                   "fixed per invocation"
        )

    def control(self, session: Session, op: str, value: str) -> InjectResult:
        if op == "interrupt":
            proc = self._procs.pop(self.key, None)
            if proc is None:
                return InjectResult(False, "no headless run in flight")
            proc.kill()
            try:
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            return InjectResult(True, "killed the in-flight run")
        if op in {"kill", "detach"}:
            proc = self._procs.pop(self.key, None)
            if proc is not None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            supervise.unmark(self.home, self.key)
            return InjectResult(True, "detached; session is observed again")
        if op == "model":
            # The next print-mode run reads it off the row as --model
            # (send(), under the session lock); a run in flight keeps
            # the model it started with.
            return switch_model(self.home, self.key, "headless", "claude",
                                value)
        return InjectResult(
            False, f"unsupported_control: {op} needs a live TUI"
        )


def _refuses_partial_flag(line: str) -> bool:
    """claude's argv error for a flag it doesn't know (``error: unknown
    option '--include-partial-messages'``)."""
    return "unknown option" in line.lower() and \
        "--include-partial-messages" in line


class _ClaudeLive:
    """One headless run's ``stream_event`` lines into ``message.delta``.

    Each text content block streams as its own message, id
    ``<API message id>:<block index>``. A subagent's lines
    (``parent_tool_use_id`` set) are skipped: they never reach the
    session's own transcript, so their bubble would never be replaced.
    """

    def __init__(self, deltas, key: str, machine: str) -> None:
        self.deltas = deltas
        self.key = key
        self.machine = machine
        self.msg = ""
        self.text_blocks: set = set()

    def _id(self, index) -> str:
        return f"{self.msg}:{index}"

    def feed(self, rec: dict) -> None:
        if self.deltas is None or rec.get("parent_tool_use_id"):
            return
        ev = rec.get("event")
        if not isinstance(ev, dict):
            return
        etype = ev.get("type")
        index = ev.get("index")
        if etype == "message_start":
            message = ev.get("message")
            mid = message.get("id") if isinstance(message, dict) else None
            self.msg = str(mid or f"claude-{time.time_ns()}")
            self.text_blocks = set()
        elif etype == "content_block_start":
            block = ev.get("content_block")
            if isinstance(block, dict) and block.get("type") == "text" \
                    and self.msg:
                self.text_blocks.add(index)
        elif etype == "content_block_delta":
            delta = ev.get("delta")
            if index in self.text_blocks and isinstance(delta, dict) \
                    and delta.get("type") == "text_delta":
                self.deltas.push(self.key, self.machine, self._id(index),
                                 "assistant", str(delta.get("text") or ""))
        elif etype == "content_block_stop":
            if index in self.text_blocks:
                self.text_blocks.discard(index)
                self.deltas.finish(self.key, self._id(index))

    def close(self) -> None:
        """The run ended (or died): nothing more will stream."""
        if self.deltas is not None:
            self.deltas.finish_all(self.key)


# ---------------------------------------------------------------------
# Tier A: opencode serve
#
# `opencode serve` exposes the project over HTTP + one SSE event stream.
# The hub owns a serve proc per project directory (refcounted across
# attached sessions) and translates `permission.*` events into the same
# `approval.requested`/`approval.resolved` protocol events the tmux
# watcher emits, so phone and ntfy paths need no second channel.

# How long a spawned serve gets to answer /session before we give up.
_SERVE_READY_S = 15.0
_SERVE_REQ_TIMEOUT = 20.0

# opencode decisions differ from ours: allow once / always / reject.
# The routes, bodies and event names of this channel (opencode 1.x's
# serve) live in ``adapters/opencode_wire.py``, with the 2.x API the
# session adapter speaks.
_OPENCODE_DECISIONS = opencode_wire.LEGACY_DECISIONS


def _free_port() -> int:
    """An ephemeral loopback port for a spawned serve."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _seg(value: str) -> str:
    """``value`` as one URL path segment. The approval's request id is
    the client's string: unquoted, ``../abort`` or ``x?y`` would aim the
    POST at another opencode serve route."""
    return opencode_wire.seg(value)


def _serve_call(
    base: str, method: str, path: str, payload: dict | None,
    timeout: float = _SERVE_REQ_TIMEOUT,
) -> tuple[int, bytes]:
    """One HTTP call against a serve proc; ``(status, body)``."""
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        except Exception:
            return exc.code, b""
    except Exception:
        return 0, b""


class _ServeProc:
    """One ``opencode serve`` child plus its SSE reader state."""

    def __init__(self, proc: subprocess.Popen, base: str) -> None:
        self.proc = proc
        self.base = base
        self.refs = 0
        self.lock = threading.Lock()
        # opencode session id -> (session_key, emit, machine)
        self.subs: dict[str, tuple[str, object, str]] = {}
        # permission id -> session id (for reply routing)
        self.permissions: dict[str, str] = {}
        # opencode session id -> the subscriber's DeltaStream
        self.deltas: dict[str, object] = {}
        # part id -> part type ("text", "reasoning", "tool", ...), so a
        # bare ``message.part.delta`` knows what it is growing
        self.part_types: dict[str, str] = {}
        self.reader_started = False
        self.dead = False


def _serve_event_loop(serve: _ServeProc) -> None:
    """Read ``GET /event``; permission asks become approval events."""
    try:
        req = urllib.request.Request(
            serve.base + "/event", headers={"Accept": "text/event-stream"}
        )
        resp = urllib.request.urlopen(req, timeout=None)
    except Exception:
        return
    data_lines: list[bytes] = []
    try:
        with resp:
            for raw in resp:
                line = raw.rstrip(b"\n")
                if line == b"":
                    if data_lines:
                        blob = b"\n".join(data_lines)
                        data_lines = []
                        _serve_event(serve, blob)
                    continue
                if line.startswith(b"data:"):
                    data_lines.append(line[5:].lstrip())
    except Exception:
        pass
    # The serve died or its stream dropped: no session.idle will close
    # what was mid-stream, so close it here.
    for sid, deltas in list(serve.deltas.items()):
        sub = serve.subs.get(sid)
        if sub is not None:
            deltas.finish_all(sub[0])


# Bound on remembered part types; a long-lived serve forgets the oldest.
_PART_TYPES_MAX = 2000


def _serve_live(serve: _ServeProc, etype: str, props: dict) -> None:
    """Text parts growing into ``message.delta``.

    opencode streams a part two ways depending on its version:
    ``message.part.updated`` with a ``delta`` beside the part, or a
    bare ``message.part.delta`` (``{sessionID, messageID, partID,
    field, delta}``) after the part was announced. Only ``text`` parts
    stream (an assistant reply; a prompt arrives whole, without a
    delta). The part's id is the stream id; a part with ``time.end``,
    or the session going idle, finishes it.
    """
    if etype == "message.part.updated":
        part = props.get("part")
        if not isinstance(part, dict):
            return
        part_id = str(part.get("id") or "")
        sid = str(part.get("sessionID") or props.get("sessionID") or "")
        ptype = str(part.get("type") or "")
        if not part_id:
            return
        if len(serve.part_types) >= _PART_TYPES_MAX:
            serve.part_types.pop(next(iter(serve.part_types)))
        serve.part_types[part_id] = ptype
        if ptype != "text":
            return
        target = _serve_target(serve, sid)
        if target is None:
            return
        deltas, key, machine = target
        delta = props.get("delta")
        if isinstance(delta, str) and delta:
            deltas.push(key, machine, part_id, "assistant", delta)
        timing = part.get("time")
        if isinstance(timing, dict) and timing.get("end"):
            deltas.finish(key, part_id)
            serve.part_types.pop(part_id, None)
    elif etype == "message.part.delta":
        part_id = str(props.get("partID") or "")
        if serve.part_types.get(part_id) != "text" or \
                props.get("field", "text") != "text":
            return
        target = _serve_target(serve, str(props.get("sessionID") or ""))
        delta = props.get("delta")
        if target is not None and isinstance(delta, str) and delta:
            deltas, key, machine = target
            deltas.push(key, machine, part_id, "assistant", delta)
    elif etype == "session.idle":
        target = _serve_target(serve, str(props.get("sessionID") or ""))
        if target is not None:
            deltas, key, _machine = target
            deltas.finish_all(key)


def _serve_target(serve: _ServeProc, sid: str):
    """``(deltas, key, machine)`` for an opencode session someone
    streams, else None."""
    deltas = serve.deltas.get(sid)
    sub = serve.subs.get(sid)
    if deltas is None or sub is None:
        return None
    return deltas, sub[0], sub[2]


def _serve_event(serve: _ServeProc, blob: bytes) -> None:
    """One SSE payload: route permission asks/replies to subscribers,
    and text deltas to their ``DeltaStream``."""
    from swe.remote import protocol

    parsed = opencode_wire.legacy_event(blob)
    if parsed is None:
        return
    etype, props = parsed
    if opencode_wire.legacy_is_live(etype):
        _serve_live(serve, etype, props)
        return
    permission = opencode_wire.legacy_permission(
        etype, props, serve.permissions)
    if permission is None:
        return
    what, perm_id, session_id, prompt = permission
    sub = serve.subs.get(session_id)
    if sub is None:
        return
    key, emit, machine = sub
    if emit is None:
        return
    if what == "resolved":
        serve.permissions.pop(perm_id, None)
        emit(key, protocol.event(
            "approval.resolved", key, machine,
            {"request_id": perm_id},
        ))
        return
    if not perm_id:
        return
    serve.permissions[perm_id] = session_id
    emit(key, protocol.event(
        "approval.requested", key, machine,
        {"request_id": perm_id, "prompt": _tr._redact_secrets(prompt)},
    ))


class OpencodeServeInjector:
    """Tier A over a managed ``opencode serve`` for the session's cwd."""

    _serves: "dict[str, _ServeProc]" = {}
    _serves_lock = threading.Lock()

    def __init__(
        self,
        home: Path | None,
        session_key: str,
        emit=None,
        machine: str = "",
        deltas=None,
    ) -> None:
        self.home = home
        self.key = session_key
        self.emit = emit
        self.machine = machine
        self.deltas = deltas

    # -- serve proc lifecycle -------------------------------------------

    @staticmethod
    def _spawn(cwd: str, port: int) -> subprocess.Popen:
        return subprocess.Popen(
            [*opencode_wire.DEFAULT_ARGV,
             *opencode_wire.LEGACY_SERVE_ARGS, str(port)],
            cwd=cwd or None,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _serve_for(self, session: Session) -> _ServeProc | None:
        """The shared serve proc for this session's project, spawning
        it (and waiting for readiness) on first use."""
        cwd = os.path.realpath(session.path or ".")
        sid = session.session_id
        with self._serves_lock:
            serve = self._serves.get(cwd)
            if serve is not None and serve.proc.poll() is not None:
                serve.dead = True
                self._serves.pop(cwd, None)
                serve = None
            if serve is None:
                port = _free_port()
                try:
                    proc = self._spawn(cwd, port)
                except OSError:
                    return None
                serve = _ServeProc(proc, f"http://127.0.0.1:{port}")
                self._serves[cwd] = serve
                if not serve.reader_started:
                    serve.reader_started = True
                    threading.Thread(
                        target=_serve_event_loop, args=(serve,),
                        daemon=True, name=f"opencode-events-{sid}",
                    ).start()
        # Wait for readiness outside the lock — slow starts shouldn't
        # serialize unrelated sessions.
        deadline = time.monotonic() + _SERVE_READY_S
        while time.monotonic() < deadline:
            if serve.proc.poll() is not None:
                with self._serves_lock:
                    serve.dead = True
                    if self._serves.get(cwd) is serve:
                        self._serves.pop(cwd, None)
                return None
            status, _ = _serve_call(serve.base, "GET", "/session", None,
                                    timeout=2.0)
            if status != 0:
                with self._serves_lock:
                    # Another waiter may have given up on this proc and
                    # killed it while our probe was in flight.
                    if serve.dead or self._serves.get(cwd) is not serve:
                        return None
                    if sid not in serve.subs:
                        serve.refs += 1
                    serve.subs[sid] = (self.key, self.emit, self.machine)
                    if self.deltas is not None:
                        serve.deltas[sid] = self.deltas
                return serve
            time.sleep(0.25)
        # Never answered. With no other session holding it, nothing
        # would ever release it: kill it rather than leak the process.
        with self._serves_lock:
            if serve.refs:
                return None
            serve.dead = True
            if self._serves.get(cwd) is serve:
                self._serves.pop(cwd, None)
        try:
            serve.proc.kill()
            serve.proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    def _release(self, session: Session) -> None:
        """Drop this session's hold; kill the serve when unused."""
        cwd = os.path.realpath(session.path or ".")
        with self._serves_lock:
            serve = self._serves.get(cwd)
            if serve is None:
                return
            # Only a hold on THIS proc counts: after a respawn the
            # sessions subscribed to the dead one hold nothing here, and
            # dropping a ref they never took would kill the new serve
            # under the sessions using it.
            deltas = serve.deltas.pop(session.session_id, None)
            sub = serve.subs.pop(session.session_id, None)
            if deltas is not None and sub is not None:
                # A reply cut off by the detach: close its bubbles now,
                # no session.idle will come for it.
                deltas.finish_all(sub[0])
            if sub is None:
                return
            serve.refs = max(0, serve.refs - 1)
            if serve.refs:
                return
            self._serves.pop(cwd, None)
            serve.dead = True
        try:
            serve.proc.kill()
            serve.proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    # -- injector interface ----------------------------------------------

    def send(self, session: Session, cmd: dict, status: str) -> InjectResult:
        text = _prompt(session, cmd, "opencode")
        if not text.strip():
            return InjectResult(False, "empty message")
        meta = supervise.meta_for(self.home, self.key)
        if meta is None:
            return InjectResult(False, "session detached; refusing the write")
        serve = self._serve_for(session)
        if serve is None:
            return InjectResult(False, "opencode serve failed to start")
        # prompt_async queues server-side — steer and queue are the same
        # call; opencode decides whether mid-turn input lands or waits.
        # The attach-time pick rides the channel's own fields: model as
        # {providerID, modelID} (provider/model is the only form the
        # server parses — channel_tuning_error enforces it; a stale or
        # hand-edited meta without the slash is left to the server's
        # default), effort as the model's variant.
        split = provider_split(str(meta.get("model") or ""))
        request = opencode_wire.legacy_prompt(
            session.session_id, text,
            model={"providerID": split[0], "modelID": split[1]}
            if split is not None else None,
            variant=str(meta.get("effort") or ""))
        status_code, body = _serve_call(
            serve.base, request.method, request.path, request.body)
        if status_code not in (200, 204):
            return InjectResult(
                False, f"opencode refused the prompt ({status_code})"
            )
        queued = cmd.get("mode") == "steer" and status == "active"
        note = "queued with opencode" if queued else "sent to opencode"
        return InjectResult(True, note, degraded=queued)

    def approval(
        self, session: Session, request_id: str, decision: str
    ) -> InjectResult:
        response = _OPENCODE_DECISIONS.get(decision)
        if response is None:
            return InjectResult(False, f"unknown decision {decision}")
        serve = self._serve_for(session)
        if serve is None:
            return InjectResult(False, "opencode serve failed to start")
        session_id = serve.permissions.get(request_id) or session.session_id
        request = opencode_wire.legacy_permission_reply(
            session_id, request_id, response)
        status_code, _ = _serve_call(
            serve.base, request.method, request.path, request.body)
        if status_code in (404, 405):
            # Older opencode: /permission/{id}/reply {reply}.
            request = opencode_wire.legacy_permission_reply_old(
                request_id, response)
            status_code, _ = _serve_call(
                serve.base, request.method, request.path, request.body)
        if status_code not in (200, 204):
            return InjectResult(
                False, f"opencode refused the reply ({status_code})"
            )
        serve.permissions.pop(request_id, None)
        return InjectResult(True, f"replied {response}")

    def control(
        self, session: Session, op: str, value: str
    ) -> InjectResult:
        if op == "interrupt":
            serve = self._serve_for(session)
            if serve is None:
                return InjectResult(False, "opencode serve failed to start")
            request = opencode_wire.legacy_abort(session.session_id)
            status_code, _ = _serve_call(
                serve.base, request.method, request.path, request.body)
            if status_code not in (200, 204):
                return InjectResult(
                    False, f"opencode refused abort ({status_code})"
                )
            return InjectResult(True, "abort sent")
        if op in {"kill", "detach"}:
            self._release(session)
            supervise.unmark(self.home, self.key)
            return InjectResult(True, "detached; session is observed again")
        if op == "model":
            # The next prompt_async carries it as its {providerID,
            # modelID}; a bare name is refused (channel_tuning_error),
            # since the server would drop it for its own default.
            return switch_model(self.home, self.key, "serve", "opencode",
                                value)
        return InjectResult(
            False, f"unsupported_control: {op} needs a live TUI"
        )


# ---------------------------------------------------------------------
# Tier A: codex app-server
#
# `codex app-server` speaks newline-delimited JSON-RPC 2.0 on stdio. One
# proc per attached session: initialize + initialized, thread/resume
# once, then turn/start per prompt. Server-initiated approval requests
# arrive as JSON-RPC *requests* on our read side; a decision is the
# matching response.

_APPSERVER_CALL_S = 60.0
# The wire format lives in ``adapters/codex_wire.py``; this channel only
# holds the process and its bookkeeping.
_CODEX_DECISIONS = codex_wire.DECISIONS


class _AppServer:
    """One ``codex app-server`` child: writer lock + reader fan-out."""

    def __init__(self, proc: subprocess.Popen, thread_id: str) -> None:
        self.proc = proc
        self.thread_id = thread_id
        self.turn_id = ""
        self.wlock = threading.Lock()
        self.next_id = 1
        # rpc id -> Queue for the matching response
        self.replies: dict = {}
        # request_id (str rpc id) -> raw id, for approval() replies
        self.approvals: dict[str, object] = {}
        self.dead = False
        # Set once initialize+thread/resume finished (or failed) — a
        # second caller must not turn/start mid-handshake.
        self.ready = threading.Event()
        self.handshake_ok = False


def _rpc_line(proc: subprocess.Popen, payload: dict) -> bool:
    try:
        proc.stdin.write(codex_wire.encode(payload))
        proc.stdin.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def _appserver_live(method: str, params: dict, key: str, machine: str,
                    deltas) -> None:
    """Agent-message text into ``message.delta``: what each
    notification means is ``codex_wire.live_text``'s to say."""
    if deltas is None:
        return
    live = codex_wire.live_text(method, params)
    if live is None:
        return
    what, item_id, text = live
    if what == "delta":
        deltas.push(key, machine, item_id, "assistant", text)
    elif what == "done":
        deltas.finish(key, item_id)
    else:
        deltas.finish_all(key)


def _appserver_read_loop(server: _AppServer, key: str, emit, machine: str,
                         deltas=None) -> None:
    """Demux stdout: responses go to waiting calls; server-initiated
    approval requests become protocol events; message text streams
    into ``deltas``."""
    proc = server.proc
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            msg = codex_wire.decode(raw)
            if msg is None:
                continue
            try:
                _appserver_message(server, msg, key, emit, machine, deltas)
            except Exception:
                # One odd message (list params, an unhashable id) is
                # skipped; ending the reader would strand every later
                # reply until the session detaches.
                continue
    except Exception:
        pass
    server.dead = True
    if deltas is not None:
        deltas.finish_all(key)


def _appserver_message(server: _AppServer, msg: dict, key: str, emit,
                       machine: str, deltas) -> None:
    """Route one app-server message (see ``_appserver_read_loop``)."""
    from swe.remote import protocol

    method = str(msg.get("method") or "")
    if method:
        # Server -> client request or notification.
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        else:
            _appserver_live(method, params, key, machine, deltas)
        if codex_wire.is_approval_request(method):
            req_id = msg.get("id")
            if req_id is not None:
                request_id = str(req_id)
                server.approvals[request_id] = req_id
                prompt = codex_wire.approval_prompt(method, params)
                if emit is not None:
                    emit(key, protocol.event(
                        "approval.requested", key, machine,
                        {"request_id": request_id,
                         "prompt": _tr._redact_secrets(prompt)},
                    ))
        else:
            turn_id = codex_wire.started_turn_id(method, params)
            if turn_id is not None:
                server.turn_id = turn_id
        return
    if "id" in msg:
        q = server.replies.pop(msg["id"], None)
        if q is not None:
            q.put(msg)


class CodexAppServerInjector:
    """Tier A over a persistent ``codex app-server`` per session."""

    _servers: "dict[str, _AppServer]" = {}
    _servers_lock = threading.Lock()

    def __init__(
        self,
        home: Path | None,
        session_key: str,
        emit=None,
        machine: str = "",
        deltas=None,
    ) -> None:
        self.home = home
        self.key = session_key
        self.emit = emit
        self.machine = machine
        self.deltas = deltas

    # -- proc + rpc plumbing --------------------------------------------

    @staticmethod
    def _spawn(cwd: str, env: dict | None = None) -> subprocess.Popen:
        return subprocess.Popen(
            ["codex", "app-server"],
            cwd=cwd or None,
            env={**os.environ, **env} if env else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _server_for(self, session: Session) -> _AppServer | None:
        spawned = False
        with self._servers_lock:
            server = self._servers.get(self.key)
            if server is not None and server.proc.poll() is None \
                    and not server.dead:
                spawned = False
            else:
                if server is not None:
                    self._servers.pop(self.key, None)
                    if server.proc.poll() is None:
                        # Its reader is gone: no reply would ever be
                        # read and its stdout would back up unread.
                        try:
                            server.proc.kill()
                        except OSError:
                            pass
                thread_id = self._thread_id(session)
                if not thread_id:
                    return None
                env = account_env(session)
                try:
                    proc = (
                        self._spawn(session.path or ".", env=env)
                        if env else self._spawn(session.path or ".")
                    )
                except OSError:
                    return None
                server = _AppServer(proc, thread_id)
                self._servers[self.key] = server
                spawned = True
                threading.Thread(
                    target=_appserver_read_loop,
                    args=(server, self.key, self.emit, self.machine,
                          self.deltas),
                    daemon=True, name=f"codex-rpc-{self.key}",
                ).start()
        if not spawned:
            # Another caller (or an earlier call) owns the handshake —
            # wait for it rather than interleave writes mid-handshake.
            server.ready.wait(timeout=_SERVE_READY_S)
            return server if server.handshake_ok else None
        # Handshake outside the registry lock.
        from swe import __version__

        try:
            hello = self._call(
                server, "initialize",
                codex_wire.initialize(0, __version__)["params"])
            if hello is None or "error" in hello:
                return None
            _rpc_line(proc, codex_wire.initialized())
            resumed = self._call(server, "thread/resume",
                                 {"threadId": server.thread_id})
            if resumed is None or "error" in resumed:
                return None
            server.handshake_ok = True
            return server
        finally:
            server.ready.set()
            if not server.handshake_ok:
                self._kill(server)

    @staticmethod
    def _thread_id(session: Session) -> str:
        from swe.sessions.parsers import codex_thread_id

        return str(codex_thread_id(session.session_id) or "")

    def _call(
        self, server: _AppServer, method: str, params: dict | None,
        timeout: float | None = None,
    ):
        """One request/response; returns the reply dict or None."""
        import queue as queue_mod

        if timeout is None:
            timeout = _APPSERVER_CALL_S

        q: queue_mod.Queue = queue_mod.Queue()
        with server.wlock:
            rpc_id = server.next_id
            server.next_id += 1
            server.replies[rpc_id] = q
            if not _rpc_line(server.proc,
                             codex_wire.request(rpc_id, method, params)):
                server.replies.pop(rpc_id, None)
                return None
        try:
            return q.get(timeout=timeout)
        except queue_mod.Empty:
            server.replies.pop(rpc_id, None)
            return None

    def _kill(self, server: _AppServer) -> None:
        with self._servers_lock:
            if self._servers.get(self.key) is server:
                self._servers.pop(self.key, None)
        server.dead = True
        try:
            server.proc.kill()
            server.proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    # -- injector interface ----------------------------------------------

    def send(self, session: Session, cmd: dict, status: str) -> InjectResult:
        text = _prompt(session, cmd, "codex")
        if not text.strip():
            return InjectResult(False, "empty message")
        # queue waits for idle; steer writes mid-turn (the server queues
        # or interleaves it — either way the write is attempted).
        if cmd.get("mode") == "queue" and status == "active":
            return InjectResult(
                False, "session is mid-turn; steer to write anyway"
            )
        meta = supervise.meta_for(self.home, self.key)
        if meta is None:
            return InjectResult(False, "session detached; refusing the write")
        server = self._server_for(session)
        if server is None:
            return InjectResult(False, "codex app-server failed to start")
        # turn/start's own overrides carry the attach-time pick (each
        # holds for the turn and the ones after, matching headless's
        # per-call --model).
        params = codex_wire.plain_turn_params(
            server.thread_id, text, model=str(meta.get("model") or ""),
            effort=str(meta.get("effort") or ""))
        resp = self._call(server, "turn/start", params)
        if resp is None:
            return InjectResult(False, "codex app-server timed out")
        if "error" in resp:
            msg = str((resp.get("error") or {}).get("message") or "error")
            return InjectResult(False, f"codex refused: {msg}")
        turn = ((resp.get("result") or {}).get("turn") or {})
        if isinstance(turn, dict) and turn.get("id"):
            server.turn_id = str(turn["id"])
        return InjectResult(True, "turn started")

    def approval(
        self, session: Session, request_id: str, decision: str
    ) -> InjectResult:
        mapped = _CODEX_DECISIONS.get(decision)
        if mapped is None:
            return InjectResult(False, f"unknown decision {decision}")
        server = self._servers.get(self.key)
        if server is None or server.proc.poll() is not None:
            return InjectResult(False, "codex app-server is not running")
        rpc_id = server.approvals.pop(request_id, None)
        if rpc_id is None:
            return InjectResult(
                False, "unknown or already-answered approval request"
            )
        ok = _rpc_line(server.proc,
                       codex_wire.approval_reply(rpc_id, mapped))
        if not ok:
            server.approvals[request_id] = rpc_id  # don't lose it
            return InjectResult(False, "codex app-server is unreachable")
        return InjectResult(True, f"replied {mapped}")

    def control(
        self, session: Session, op: str, value: str
    ) -> InjectResult:
        if op == "interrupt":
            server = self._servers.get(self.key)
            if server is None or server.proc.poll() is not None:
                return InjectResult(False, "codex app-server is not running")
            resp = self._call(
                server, "turn/interrupt", codex_wire.interrupt_params(
                    server.thread_id, server.turn_id))
            if resp is None or "error" in resp:
                return InjectResult(False, "interrupt failed")
            return InjectResult(True, "interrupted")
        if op in {"kill", "detach"}:
            server = self._servers.pop(self.key, None)
            if server is not None:
                server.dead = True
                try:
                    server.proc.kill()
                    server.proc.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            supervise.unmark(self.home, self.key)
            return InjectResult(True, "detached; session is observed again")
        if op == "model":
            # The next turn/start carries it as its model override,
            # which codex keeps for the turns after too; a turn already
            # running keeps the model it started on.
            return switch_model(self.home, self.key, "appserver", "codex",
                                value)
        return InjectResult(
            False, f"unsupported_control: {op} needs a live TUI"
        )


# ---------------------------------------------------------------------
# Agent Client Protocol (any harness that speaks it)
#
# One ACP agent process per attached session (``acp.Connection``):
# session/load (or session/new) once, session/prompt per message,
# session/cancel for interrupt. Permission requests become the same
# approval events the other channels raise, their options the menu rows.

# How long send() waits for session/prompt's answer before reporting the
# turn as started: an agent that refuses the prompt answers at once.
_ACP_EARLY_S = 1.0


def acp_session_ref(session: Session) -> str:
    """The id an ACP agent knows ``session`` by: the value its resume
    flags carry (``devin --resume <id>``), else the session id."""
    from swe.sessions.resume import resume_flags

    tokens = [t for t in resume_flags(session)
              if t and not t.startswith("-") and t != "resume"]
    return tokens[-1] if tokens else session.session_id


class AcpInjector:
    """Over an ACP agent process per session (``remote/acp.py``)."""

    _conns: "dict[str, object]" = {}
    _conns_lock = threading.Lock()
    # One spawn+handshake per key at a time.
    _opening: dict[str, threading.Lock] = {}

    def __init__(
        self,
        home: Path | None,
        session_key: str,
        emit=None,
        machine: str = "",
        deltas=None,
    ) -> None:
        self.home = home
        self.key = session_key
        self.emit = emit
        self.machine = machine
        self.deltas = deltas

    # -- connection ------------------------------------------------------

    @classmethod
    def _gate(cls, session_key: str) -> threading.Lock:
        with cls._conns_lock:
            return cls._opening.setdefault(session_key, threading.Lock())

    @classmethod
    def open(cls, home: Path | None, session_key: str, session: Session,
             meta: dict, *, emit=None, machine: str = "",
             deltas=None, allow_new: bool = True,
             budget: float | None = None, attaching: bool = False,
             raise_no_load: bool = False):
        """The live connection for ``session_key``, starting the agent
        and loading (or, with ``allow_new``, creating) its session when
        none runs. ``budget`` caps the handshake. Returns ``(conn,
        note)``; conn is None when the agent can't serve, and note says
        why. A new session's id is recorded on the row. Outside an
        attach (``attaching``) the row must still exist once the
        handshake ends, or the agent is closed: a detach that landed
        meanwhile wins. With ``raise_no_load``, an agent that can't load
        the session (and ``allow_new`` is off) raises
        ``acp.NoLoadSession`` instead, so an attach can tell that case
        from a failure."""
        from swe.remote import acp

        with cls._gate(session_key):
            with cls._conns_lock:
                conn = cls._conns.get(session_key)
            if conn is not None and conn.alive():
                conn.bind(session_key, machine, emit, deltas)
                return conn, ""
            restarted = conn is not None
            if conn is not None:
                cls._drop(session_key, conn)
            tool = str(meta.get("tool_name") or session.tool_name or "")
            argv = acp.command_for(tool)
            if argv is None:
                return None, f"{tool} has no ACP mode installed"
            try:
                conn = acp.Connection(
                    argv, session.path or str(meta.get("cwd") or ""),
                    account_env(session) or None,
                    auto_allow=bool(meta.get("yolo")))
            except OSError as exc:
                return None, f"{argv[0]} failed to launch: {exc}"
            conn.bind(session_key, machine, emit, deltas)
            ref = str(meta.get("acp_session_id") or "") \
                or acp_session_ref(session)
            try:
                note = conn.handshake(ref, allow_new=allow_new,
                                      budget=budget)
            except acp.NoLoadSession as exc:
                conn.close()
                if raise_no_load:
                    raise
                return None, str(exc)
            except acp.AcpError as exc:
                conn.close()
                return None, f"ACP handshake failed: {exc}"
            if not attaching and \
                    supervise.meta_for(home, session_key) is None:
                conn.close()
                return None, "session detached; refusing the write"
            with cls._conns_lock:
                cls._conns[session_key] = conn
            if conn.session_id != ref:
                # Recorded so a restart loads this session, not the one
                # the agent could not load.
                supervise.set_meta(home, session_key, "acp_session_id",
                                   conn.session_id)
            if restarted:
                note = f"{conn.label} had exited and was restarted" + (
                    f"; {note}" if note else ", same session")
            return conn, note

    @classmethod
    def _drop(cls, session_key: str, conn) -> None:
        with cls._conns_lock:
            if cls._conns.get(session_key) is conn:
                cls._conns.pop(session_key, None)
        conn.close()

    @classmethod
    def close(cls, session_key: str) -> None:
        """Stop the key's agent. Waits out an open() in flight (the same
        per-key gate), so a handshake finishing after a detach cannot
        register an agent nobody owns."""
        with cls._gate(session_key):
            with cls._conns_lock:
                conn = cls._conns.pop(session_key, None)
            if conn is not None:
                conn.close()

    @classmethod
    def register(cls, session_key: str, conn) -> None:
        """Hold ``conn`` as ``session_key``'s agent (a launch, which
        started it before the session had a row)."""
        with cls._gate(session_key):
            with cls._conns_lock:
                old = cls._conns.get(session_key)
                cls._conns[session_key] = conn
            if old is not None and old is not conn:
                old.close()

    @classmethod
    def release_held(cls, ready, emit, machine: str = "",
                     deltas=None) -> None:
        """Bind the sinks of every agent nobody listens to yet whose key
        is in ``ready`` (keys that have a tailer now), so a launch's
        held approval requests reach the chat."""
        with cls._conns_lock:
            waiting = [(k, c) for k, c in cls._conns.items()
                       if c.emit is None and k in ready]
        for key, conn in waiting:
            conn.bind(key, machine, emit, deltas)

    def _conn(self, session: Session, meta: dict):
        return self.open(self.home, self.key, session, meta,
                         emit=self.emit, machine=self.machine,
                         deltas=self.deltas)

    def _live(self):
        conn = self._conns.get(self.key)
        if conn is None or not conn.alive():
            return None
        conn.bind(self.key, self.machine, self.emit, self.deltas)
        return conn

    # -- injector interface ----------------------------------------------

    def send(self, session: Session, cmd: dict, status: str) -> InjectResult:
        from swe.remote import acp

        text = _prompt(session, cmd)
        if not text.strip():
            return InjectResult(False, "empty message")
        meta = supervise.meta_for(self.home, self.key)
        if meta is None:
            return InjectResult(False, "session detached; refusing the write")
        conn, note = self._conn(session, meta)
        if conn is None:
            return InjectResult(False, note)
        degraded = False
        if not conn.idle.is_set():
            # One prompt per session at a time. The hub's queue may not
            # see the turn (a harness with no status probe reads ""), so
            # queue waits for the turn here too; steer waits and says it
            # was queued.
            if not conn.idle.wait(timeout=_QUEUE_WAIT_S):
                return InjectResult(False, "the running turn did not end")
            degraded = cmd.get("mode") == "steer"
        # A detach may have landed while the agent started or the turn
        # ran: the row is the last word before the prompt goes out.
        if supervise.meta_for(self.home, self.key) is None:
            return InjectResult(False, "session detached; refusing the write")
        try:
            q = conn.prompt(text)
        except acp.AcpError as exc:
            return InjectResult(False, str(exc))
        import queue as queue_mod

        try:
            reply = q.get(timeout=_ACP_EARLY_S)
        except queue_mod.Empty:
            # Past this, a late error answer arrives as a notify event.
            reply = conn.stop_waiting(q)
            if reply is acp.PENDING:
                reply = False
        if reply is None:
            return InjectResult(False, f"{conn.label} exited mid-turn")
        if reply and "error" in reply:
            return InjectResult(False, f"{conn.label} refused the prompt: "
                                       f"{conn.last_error or 'error'}")
        done = "turn complete" if reply else "turn started"
        if note:
            done = f"{done}; {note}"
        if degraded:
            done += " (steer queued: ACP takes one prompt at a time)"
        return InjectResult(True, done, degraded=degraded)

    def approval(
        self, session: Session, request_id: str, decision: str
    ) -> InjectResult:
        if decision not in ("allow", "always", "deny") and not (
                decision.isdigit() and len(decision) <= 2):
            return InjectResult(False, f"unknown decision {decision}")
        conn = self._live()
        if conn is None:
            return InjectResult(False, "the ACP agent is not running")
        ok, note = conn.answer(request_id, decision)
        return InjectResult(ok, note)

    def control(
        self, session: Session, op: str, value: str
    ) -> InjectResult:
        if op == "interrupt":
            conn = self._live()
            if conn is None:
                return InjectResult(False, "the ACP agent is not running")
            if not conn.cancel():
                return InjectResult(False, "no turn is running")
            return InjectResult(True, "cancel sent")
        if op in {"kill", "detach"}:
            self.close(self.key)
            supervise.unmark(self.home, self.key)
            return InjectResult(True, "detached; session is observed again")
        return InjectResult(
            False, f"unsupported_control: {op} needs a live TUI"
        )


def injector_for(
    home: Path | str | None,
    session_key: str,
    meta: dict | None,
    emit=None,
    machine: str = "",
    deltas=None,
) -> (TmuxInjector | ClaudeHeadlessInjector | OpencodeServeInjector
      | CodexAppServerInjector | AcpInjector):
    """Pick the channel from the supervised registry row. ``deltas`` (a
    ``stream.DeltaStream``) takes the API channels' live text; tmux
    has none."""
    meta = meta or {}
    home_path = Path(home) if home is not None else None
    mode = meta.get("mode")
    tool = meta.get("tool_name")
    if mode == "headless" and tool == "claude":
        return ClaudeHeadlessInjector(
            home_path, session_key, deltas=deltas, machine=machine
        )
    if mode == "serve" and tool == "opencode":
        return OpencodeServeInjector(
            home_path, session_key, emit=emit, machine=machine,
            deltas=deltas,
        )
    if mode == "appserver" and tool == "codex":
        return CodexAppServerInjector(
            home_path, session_key, emit=emit, machine=machine,
            deltas=deltas,
        )
    if mode == "acp":
        return AcpInjector(
            home_path, session_key, emit=emit, machine=machine,
            deltas=deltas,
        )
    return TmuxInjector(home_path, session_key)
