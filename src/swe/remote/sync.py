"""Sync v2: what a ``v=2`` session stream is fed, and how it catches up.

A v2 client reads one session as the v2 events of
:mod:`swe.remote.adapters.events`, whoever wrote them: a keeper-held
session's journal holds them as they are, and a journal of v1 events (a
terminal session, the earlier history of one continued in SWE) is
converted on the way out (:func:`protocol_compat.to_v2`).

:class:`SyncHub` is the fan-out and the state a catch-up needs:

- **Live text.** ``message.delta`` is never journaled, so the hub keeps
  the text so far of every message in progress, per session. Every
  catch-up ends with a ``session.state`` frame that carries it as
  ``live[]``, and every delta carries the ``offset`` its text starts at.
- **Subscribers.** One bounded queue per open v2 stream. Events are
  queued in seq order (the caller holds the journal's lock for that
  key), so a stream that skips what it already sent loses nothing.
- **Snapshot.** What a session's journal adds up to (plan, tasks,
  checkpoints, changes, usage, the open turn), folded once and kept up
  from the journal, plus what the hub holds live (state, open requests,
  queue, text in progress) and the last timeline events.

:class:`V2Stream` is one open stream: it decides between a replay and a
snapshot, drops what the catch-up already covered, sends a ``ping``
every ``PING_S``, and reads the journal again when its queue overflowed.
It only builds frames; the HTTP handler writes them.

Design: docs/specs/G9-sync-v2.md. The frames' shapes are
``schema/session_v2.json``.
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
from typing import Any, Callable

from swe.remote import injected as injected_mod
from swe.remote import protocol, protocol_compat, readstate
from swe.remote.adapters import events as ev

logger = logging.getLogger("swe.remote.sync")

# Frames one stream may have waiting before it is cut over to a
# re-read of the journal.
QUEUE_MAX = 2000
# Sessions whose folded journal is kept between snapshots.
FOLD_CACHE = 32
# Checkpoints a snapshot lists (the newest).
CHECKPOINTS_MAX = 200
# Live text is dropped oldest first past these: a channel that died
# mid-reply never completes its message, and nothing else would ever
# free the text. A dropped message's later deltas start over at 0; its
# completion still carries the whole text.
LIVE_SESSIONS_MAX = 512
LIVE_PER_SESSION_MAX = 32

# Durable events that are bookkeeping, not something a timeline draws:
# the state they carry is in a snapshot's own fields.
BOOKKEEPING_TYPES = frozenset({
    "session.state", "session.caps", "queue.updated", "command.receipt",
    "command.result", "usage.updated", "changes.updated", "notify",
    "tool.updated",
})
TIMELINE_TYPES = ev.DURABLE_TYPES - BOOKKEEPING_TYPES

# States in which nothing is being written (``keeper/log.py``'s rule).
_AT_REST = frozenset({"idle", "agent_down", "parked", "closed"})

# v1 events that change a session's row, not its timeline.
_ROW_TYPES = frozenset({"session.ownership", "session.meta"})


def _without_nulls(value: Any, spec: Any) -> Any:
    """``value`` with every optional field that is null left out, by
    ``spec`` (an ``events.py`` field spec). The same object when there
    is nothing to leave out. A field of any shape (a tool's ``input``)
    is not looked into: a null in there is the tool's own."""
    if isinstance(spec, ev._List) and isinstance(value, list):
        items = [_without_nulls(item, spec.item) for item in value]
        if all(new is old for new, old in zip(items, value)):
            return value
        return items
    if not isinstance(spec, ev._Obj) or not isinstance(value, dict):
        return value
    out = None
    for name, item in value.items():
        if item is None and name in spec.optional:
            new, drop = None, True
        else:
            field = spec.required.get(name) or spec.optional.get(name)
            new, drop = _without_nulls(item, field), False
        if (drop or new is not item) and out is None:
            out = {k: v for k, v in value.items()}
        if drop:
            del out[name]
        elif new is not item:
            out[name] = new
    return value if out is None else out


def wire(event: dict) -> dict:
    """A v2 event as the schema has it. ``events.validate`` takes an
    optional field that is null as left out, so a journal may hold one;
    on the wire it is left out (and so are a null ``turn_id`` or
    ``command_id``), which keeps a client's types to "there or not"."""
    spec = ev.SCHEMAS.get(event.get("type"))
    data = event.get("data")
    fixed = _without_nulls(data, spec) if spec is not None else data
    nulls = [name for name in ("turn_id", "command_id")
             if name in event and event[name] is None]
    if fixed is data and not nulls:
        return event
    out = {k: v for k, v in event.items() if k not in nulls}
    out["data"] = fixed
    return out


def served(event: Any, *, msg_id: str | None = None) -> dict | None:
    """One journal event as the v2 frame a client gets, or None when it
    has none. A v1 event is converted; message text is cleaned of what
    a harness injects, as the v1 stream does (``injected.clean``)."""
    if not isinstance(event, dict):
        return None
    if event.get("v") != ev.VERSION:
        cleaned = injected_mod.clean_event(event)
        if cleaned is None:
            return None
        return protocol_compat.to_v2(cleaned, msg_id=msg_id)
    event = wire(event)
    data = event.get("data")
    if event.get("type") == "message.completed" and isinstance(data, dict):
        shown = injected_mod.clean({"kind": data.get("kind"),
                                    "text": data.get("text") or ""})
        if shown is None:
            return None
        if shown.get("text") != data.get("text") \
                or shown.get("kind") != data.get("kind"):
            return dict(event, data=dict(
                data, kind=shown.get("kind"), text=shown.get("text") or ""))
    return event


def timeline(event: Any) -> dict | None:
    """``event`` as a snapshot's ``messages`` list holds it, or None for
    one a timeline does not draw."""
    frame = served(event)
    if frame is None or frame.get("type") not in TIMELINE_TYPES:
        return None
    return frame


class Fold:
    """What a session's journaled events add up to, besides the
    timeline: the fields of a snapshot that only the journal knows."""

    def __init__(self, epoch: int = 0) -> None:
        self.epoch = epoch
        # The last journal seq folded in.
        self.head = 0
        self.state = ""
        self.reason: str | None = None
        self.caps: dict | None = None
        self.open_turn: str | None = None
        self.pending: dict[str, dict] = {}
        self.plan: dict | None = None
        self.tasks: dict[str, dict] = {}
        self.checkpoints: list[dict] = []
        self.changes: list | None = None
        self.usage: dict | None = None

    def add(self, event: dict) -> None:
        """Fold one v2 event in."""
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) \
            else {}
        if etype == "session.state":
            self.state = str(data.get("state") or "")
            self.reason = data.get("reason") or None
            if self.state in _AT_REST:
                self.open_turn = None
        elif etype == "session.caps":
            if isinstance(data.get("caps"), dict):
                self.caps = dict(data["caps"])
        elif etype == "turn.started":
            self.open_turn = event.get("turn_id") or None
        elif etype == "turn.ended":
            if self.open_turn == event.get("turn_id"):
                self.open_turn = None
        elif etype in ("approval.requested", "input.requested"):
            rid = data.get("request_id")
            if isinstance(rid, str) and rid:
                self.pending[rid] = event
        elif etype == "approval.resolved":
            self.pending.pop(str(data.get("request_id") or ""), None)
        elif etype == "plan.updated":
            self.plan = dict(data)
        elif etype == "task.updated":
            tid = data.get("task_id")
            if isinstance(tid, str) and tid:
                if data.get("status") == "running":
                    self.tasks[tid] = dict(data)
                else:
                    self.tasks.pop(tid, None)
        elif etype == "checkpoint.created":
            entry = dict(data, seq=event.get("seq") or 0,
                         ts=event.get("ts") or 0)
            if event.get("turn_id"):
                entry["turn_id"] = event["turn_id"]
            self.checkpoints.append(entry)
            del self.checkpoints[:-CHECKPOINTS_MAX]
        elif etype == "changes.updated":
            if isinstance(data.get("files"), list):
                self.changes = list(data["files"])
        elif etype == "usage.updated":
            self.usage = dict(data)

    def view(self) -> dict:
        """A copy a snapshot can hold while the fold moves on."""
        return {
            "head": self.head, "epoch": self.epoch, "state": self.state,
            "reason": self.reason,
            "caps": dict(self.caps) if self.caps is not None else None,
            "open_turn": self.open_turn, "pending": dict(self.pending),
            "plan": dict(self.plan) if self.plan is not None else None,
            "tasks": [dict(t) for t in self.tasks.values()],
            "checkpoints": [dict(c) for c in self.checkpoints],
            "changes": list(self.changes)
            if self.changes is not None else None,
            "usage": dict(self.usage) if self.usage is not None else None,
        }


class _Live:
    """The messages one session is still writing: id -> ``{id, kind,
    text, turn_id?, done?}``. ``floor`` is the seq the entries were
    true at (a keeper's welcome): an older event being replayed must
    not clear text written after it."""

    __slots__ = ("entries", "floor")

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.floor = 0


class SyncHub:
    """See the module docstring. ``hub`` is the ``HubState``. ``clock``
    times the pings and ``now`` stamps frames; tests set both, and the
    limits below, on the instance."""

    def __init__(self, hub, *, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], float] = time.time) -> None:
        self.hub = hub
        self.clock = clock
        self.now = now
        self.queue_max = QUEUE_MAX
        self.replay_max = protocol.REPLAY_MAX
        self.snapshot_messages = protocol.SNAPSHOT_MESSAGES
        self.ping_s = protocol.PING_S
        self.closed = False
        # Guards the subscriber lists and the live text. Never held
        # across a journal read or a socket write. The live text
        # changes and a stream's queue is added to in one step under
        # it, so a state item put in a queue holds exactly the text as
        # it stood at that place in the stream.
        self._lock = threading.RLock()
        self._subs: dict[str, list["V2Stream"]] = {}
        self._live: dict[str, _Live] = {}
        self._folds: "collections.OrderedDict[str, Fold]" = \
            collections.OrderedDict()
        self._fold_lock = threading.Lock()

    # -- streams ------------------------------------------------------

    def open(self, key: str, *, last: int = 0, epoch: int | None = None,
             device_id: str | None = None) -> "V2Stream":
        """A stream for ``key``, subscribed from this moment. The
        caller writes ``catch_up()``, then ``next()`` until it is None,
        and closes it."""
        return V2Stream(self, key, last=last, epoch=epoch,
                        device_id=device_id)

    def _add(self, stream: "V2Stream") -> None:
        with self._lock:
            self._subs.setdefault(stream.key, []).append(stream)

    def _remove(self, stream: "V2Stream") -> None:
        with self._lock:
            subs = [s for s in self._subs.get(stream.key, ())
                    if s is not stream]
            if subs:
                self._subs[stream.key] = subs
            else:
                self._subs.pop(stream.key, None)

    def watching(self, key: str | None = None) -> int:
        """How many v2 streams are open on ``key`` (on any session when
        None)."""
        with self._lock:
            if key is None:
                return sum(len(subs) for subs in self._subs.values())
            return len(self._subs.get(key, ()))

    def wake(self) -> None:
        """Make every open stream look at its clock again."""
        with self._lock:
            for subs in self._subs.values():
                for stream in subs:
                    stream.put(("wake",))

    def close(self) -> None:
        """The hub is stopping: every stream ends at its next step."""
        self.closed = True
        self.wake()

    def _put(self, key: str, item: tuple) -> None:
        # Caller holds the lock.
        for stream in self._subs.get(key, ()):
            stream.put(item)

    def _state_item(self, key: str) -> tuple:
        """A queue item for a state frame, holding the live text as it
        stands now. Caller holds the lock. Put in a queue at once, it
        sits exactly where that text is true: every event before it
        was counted, none after it."""
        live = self._live.get(key)
        return ("state", [dict(m) for m in live.entries.values()]
                if live else [])

    def _put_state(self, key: str) -> None:
        # Caller holds the lock.
        if key in self._subs:
            self._put(key, self._state_item(key))

    def resync(self, key: str) -> None:
        """Send ``key``'s open streams the state again: the hub's view
        changed with no event to say so."""
        with self._lock:
            self._put_state(key)

    # -- what the hub feeds in ----------------------------------------

    def on_event(self, key: str, event: dict) -> None:
        """One v2 event from a keeper: a delta as it arrives, or a
        durable event the hub journaled just now (the caller holds that
        journal's lock, so the queues fill in seq order)."""
        with self._lock:
            if event.get("type") == "message.delta":
                frame = self._delta(key, event)
                if frame is not None:
                    self._put(key, ("delta", frame))
                return
            self._settle_live(key, event)
            if key in self._subs:
                frame = served(event)
                self._put(key, ("event", frame) if frame is not None
                          else ("skip", event.get("seq")))

    def on_welcome(self, key: str, live: Any, last_seq: Any) -> None:
        """A keeper's welcome: the text it holds for the messages in
        progress, true at ``last_seq``. The hub may have missed deltas
        (it restarted, or the connection dropped), so this replaces what
        it had. Open streams are sent the state once the keeper's
        replay is in (:meth:`resync`), not here: the events it replays
        are older than this text, and a client that got the text first
        would clear it at a replayed ``turn.ended``."""
        with self._lock:
            kept = self._live[key] = _Live()
            if isinstance(last_seq, int) and not isinstance(last_seq, bool):
                kept.floor = max(0, last_seq)
            for msg in live if isinstance(live, list) else []:
                if not isinstance(msg, dict) \
                        or not isinstance(msg.get("id"), str) \
                        or not msg["id"] \
                        or not isinstance(msg.get("text"), str):
                    continue
                entry = {"id": msg["id"],
                         "kind": msg.get("kind") if msg.get("kind")
                         in ev.DELTA_KINDS else "assistant",
                         "text": msg["text"]}
                if isinstance(msg.get("turn_id"), str) and msg["turn_id"]:
                    entry["turn_id"] = msg["turn_id"]
                if msg.get("done") is True:
                    entry["done"] = True
                kept.entries[entry["id"]] = entry

    def on_v1(self, key: str, event: dict) -> None:
        """One v1 event for a session's streams: journaled by the hub
        (it has a seq; the caller holds that journal's lock), or made
        by the hub for the open streams of a keeper-held session (no
        seq)."""
        if key.startswith("_"):
            return  # the global and satellite command journals
        with self._lock:
            live = self._live.get(key)
            subs = key in self._subs
            if not subs and live is None:
                return
            etype = event.get("type")
            data = event.get("data") if isinstance(event.get("data"), dict) \
                else {}
            seq = event.get("seq")
            numbered = isinstance(seq, int) and seq > 0
            msg_id = None
            retired = False
            if live is not None and numbered:
                if etype == "message.appended":
                    kind = data.get("kind")
                    if kind == "tool_use":
                        retired = self._retire_tool(live, data.get("tool"))
                    else:
                        msg_id = self._pair(live, kind)
                elif etype == "session.status" \
                        and data.get("status") != "active":
                    live.entries.clear()
            hub_made = not numbered and etype in ("session.status",
                                                  "approval.resolved")
            if hub_made and live is not None:
                # The hub speaking for a keeper that is gone: what it
                # was writing is gone with it, watched or not.
                owned = self.hub.runtime.session(key)
                if owned is not None and owned.lost:
                    live.entries.clear()
            if not subs:
                return
            if hub_made:
                # A stream takes the state whole: what is open, what is
                # live.
                self._put_state(key)
                return
            row = etype in _ROW_TYPES
            frame = None if row else served(event, msg_id=msg_id)
            if frame is not None:
                self._put(key, ("event", frame))
            elif numbered:
                # Journaled, with no v2 frame: the stream still counts
                # it as sent, so its ``head`` covers it.
                self._put(key, ("skip", seq))
            if row:
                # A change to the row, not to the timeline.
                self._put(key, ("row",))
                if etype == "session.ownership":
                    self._put_state(key)
            elif retired or (frame is None
                             and etype == "approval.resolved"):
                # Live text ended under an id the event does not carry,
                # or a resolve that names no request retired them all:
                # the state says what is left.
                self._put_state(key)

    def on_v1_delta(self, key: str, event: dict) -> None:
        """A v1 ``message.delta`` (an API channel of a session no
        keeper holds, or a satellite's). v1 deltas carry no offset; the
        hub counts it from the text it has kept."""
        if key.startswith("_"):
            return
        v2 = protocol_compat.to_v2(event)
        if v2 is None:
            return
        self.on_event(key, v2)

    # -- live text ----------------------------------------------------

    def _delta(self, key: str, event: dict) -> dict | None:
        """Add a delta to the text kept for its message; returns the
        frame to send, whose ``offset`` is where its text starts in
        what every client holds. Caller holds the lock."""
        data = event.get("data") if isinstance(event.get("data"), dict) \
            else {}
        mid, text = data.get("id"), data.get("text")
        if not isinstance(mid, str) or not mid or not isinstance(text, str):
            return None
        live = self._live.get(key)
        if live is None:
            live = self._live[key] = _Live()
            while len(self._live) > LIVE_SESSIONS_MAX:
                del self._live[next(iter(self._live))]
        cur = live.entries.get(mid)
        if cur is None:
            while len(live.entries) >= LIVE_PER_SESSION_MAX:
                del live.entries[next(iter(live.entries))]
            cur = live.entries[mid] = {
                "id": mid, "kind": data.get("kind") if data.get("kind")
                in ev.DELTA_KINDS else "assistant", "text": ""}
            if isinstance(event.get("turn_id"), str) and event["turn_id"]:
                cur["turn_id"] = event["turn_id"]
        held = cur["text"]
        offset = data.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool) \
                or offset < 0 or offset > len(held):
            # No offset (v1, or an adapter that sends none), or one
            # past the text the hub has: the text every client can have
            # is the hub's, so that is where this delta starts.
            offset = len(held)
        cur["text"] = held[:offset] + text
        if data.get("done") is True:
            cur["done"] = True
        if offset == data.get("offset"):
            return event
        return dict(event, data=dict(data, offset=offset))

    def _settle_live(self, key: str, event: dict) -> None:
        """A durable v2 event ends the live text it completes
        (``keeper/log.py``'s rule). Caller holds the lock."""
        live = self._live.get(key)
        if live is None:
            return
        seq = event.get("seq")
        if isinstance(seq, int) and 0 < seq <= live.floor:
            return
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) \
            else {}
        if etype in ("message.completed", "thinking.completed"):
            live.entries.pop(data.get("id"), None)
        elif etype == "tool.started":
            live.entries.pop(data.get("call_id"), None)
        elif etype == "turn.ended" or (
                etype == "session.state" and data.get("state") in _AT_REST):
            live.entries.clear()

    @staticmethod
    def _pair(live: _Live, kind: Any) -> str | None:
        """A v1 message carries no id. The one that completes a
        streamed reply takes the id of the oldest live text of its
        kind, so a client retires that text when the message lands."""
        if kind not in ("assistant", "thinking"):
            return None
        for mid, entry in live.entries.items():
            if entry.get("kind") == kind:
                del live.entries[mid]
                return mid
        return None

    @staticmethod
    def _retire_tool(live: _Live, tool: Any) -> bool:
        """A v1 tool message ends the live text streamed for that call.
        A legacy channel streams it under its own id
        (``acp:<nonce>:<turn>:tool:<call_id>``), which the v2
        ``tool.started`` does not carry, so the caller sends the state.
        True when something was retired."""
        call_id = str(tool.get("call_id") or "") \
            if isinstance(tool, dict) else ""
        tools = [mid for mid, entry in live.entries.items()
                 if entry.get("kind") == "tool_use"]
        match = [mid for mid in tools if call_id and (
            mid == call_id or mid.endswith(f":tool:{call_id}"))]
        for mid in (match or tools)[:1]:
            del live.entries[mid]
            return True
        return False

    def live(self, key: str) -> list[dict]:
        """The messages ``key`` is still writing, each with its text so
        far."""
        with self._lock:
            live = self._live.get(key)
            return [dict(m) for m in live.entries.values()] if live else []

    # -- epoch --------------------------------------------------------

    def epoch(self, key: str) -> int:
        """The number of ``key``'s timeline. It changes when the
        journal's numbering starts over or loses a stretch, so a cursor
        from before is not replayed against it. 0 for a session the
        store has never held."""
        store = self.hub.runtime.store
        if store is None:
            return 0
        try:
            row = store.get_session(key)
        except Exception:  # a closing store reads as epoch 0
            return 0
        return row.epoch if row is not None else 0

    def new_epoch(self, key: str) -> int:
        """``key``'s journal was dropped, or has a hole no replay
        fills: move its epoch on, forget what was folded, and send
        every open stream a snapshot."""
        store = self.hub.runtime.store
        epoch = 0
        if store is not None:
            try:
                row = store.get_session(key)
                if row is not None:
                    epoch = row.epoch + 1
                    store.update_session(key, epoch=epoch)
            except Exception:
                logger.exception("moving the epoch of %s", key)
        with self._fold_lock:
            self._folds.pop(key, None)
        with self._lock:
            self._put(key, ("snapshot",))
        return epoch

    # -- frames -------------------------------------------------------

    def _volatile(self, key: str) -> tuple[str, list[str]]:
        """``(state, open request ids)`` as the hub holds them now."""
        hub = self.hub
        owned = hub.runtime.session(key)
        if owned is not None:
            if owned.lost:
                return "agent_down", []
            return owned.state, list(owned.pending)
        row = hub.catalog.row_for(key) or {}
        state = protocol_compat.STATE_FOR_STATUS.get(
            str(row.get("status") or ""), "idle")
        with hub._approval_lock:
            open_ = list(hub._approval_at.get(key) or {})
        return state, open_

    def state_frame(self, key: str, head: int, live: list) -> dict:
        """The frame every catch-up ends with: the session's state, the
        requests still open (by id), ``head`` (the seq the stream is
        caught up to) and ``live[]`` (a state item's text). A client
        replaces its live text with ``live[]`` and drops any request
        card whose id is not listed."""
        state, pending = self._volatile(key)
        return {
            "v": ev.VERSION, "type": "session.state", "session": key,
            "seq": 0, "ts": self.now(),
            "data": {"state": state, "live": live, "pending": pending,
                     "head": head, "epoch": self.epoch(key)},
        }

    def _row(self, key: str, device_id: str | None) -> dict | None:
        row = self.hub.catalog.row_for(key)
        if row is None:
            return None
        return readstate.annotate(self.hub.home, device_id, [row])[0]

    def row_frame(self, key: str, device_id: str | None) -> dict | None:
        row = self._row(key, device_id)
        if row is None:
            return None
        return protocol.sync_frame("session.row", key, {"row": row},
                                   ts=self.now())

    def ping_frame(self, key: str, head: int) -> dict:
        """``head`` is the stream's own: the seq it has sent up to,
        which is the cursor its client should hold."""
        now = self.now()
        return protocol.sync_frame("ping", key, {"ts": now, "head": head},
                                   ts=now)

    # -- snapshot -----------------------------------------------------

    def _folded(self, key: str) -> dict:
        """``key``'s journal folded up to its head, kept between calls
        and carried on from where the last one stopped."""
        journal = self.hub.journal
        with self._fold_lock:
            epoch = self.epoch(key)
            fold = self._folds.pop(key, None)
            if fold is None or fold.epoch != epoch \
                    or fold.head > journal.last_seq(key):
                fold = Fold(epoch)
            events, _truncated = journal.read_since(key, fold.head)
            for event in events:
                v2 = event if event.get("v") == ev.VERSION \
                    else protocol_compat.to_v2(event)
                if v2 is not None:
                    try:
                        fold.add(v2)
                    except Exception:  # one odd record never ends a fold
                        logger.exception("folding %s", key)
                seq = event.get("seq")
                if isinstance(seq, int) and seq > fold.head:
                    fold.head = seq
            self._folds[key] = fold
            while len(self._folds) > FOLD_CACHE:
                self._folds.popitem(last=False)
            return fold.view()

    def messages_page(self, key: str, *, before: int = 0,
                      limit: int | None = None) -> dict:
        """The newest ``limit`` timeline events with seq below
        ``before`` (any when 0), oldest first, as v2 events: what
        ``GET .../messages?v=2`` serves and a snapshot's ``messages``
        is."""
        limit = self.snapshot_messages if limit is None else limit
        # One past the page says whether an older page exists.
        events = self.hub.journal.read_tail(
            key, limit + 1, before_seq=before, keep=timeline)
        has_more = len(events) > limit
        page = events[-limit:]
        return {
            "session": key, "messages": page, "has_more": has_more,
            "next_before": page[0]["seq"] if has_more and page else None,
        }

    def snapshot(self, key: str, device_id: str | None = None) -> dict:
        """The body of a ``session.snapshot`` frame and of
        ``GET .../snapshot``: everything a client needs to draw the
        session from nothing, true at ``head``."""
        hub = self.hub
        fold = self._folded(key)
        head = fold["head"]
        state, open_ids = self._volatile(key)
        owned = hub.runtime.session(key)
        reason = None
        if owned is not None:
            lost = owned.lost
            pending = [] if lost else list(owned.pending.values())
            queued = [] if lost else [dict(q) for q in owned.queue]
            caps = dict(owned.caps) if owned.caps else fold["caps"]
            open_turn = None if lost else fold["open_turn"]
            if not lost and fold["state"] == state:
                reason = fold["reason"]
        else:
            pending = [fold["pending"][rid] for rid in open_ids
                       if rid in fold["pending"]]
            queued = [_queued(send) for send in hub.waiting.list(key)]
            caps = fold["caps"]
            open_turn = None
        page = self.messages_page(key, before=head + 1)
        body = {
            "session": key, "epoch": fold["epoch"], "head": head,
            "boot_id": hub.boot_id, "row": self._row(key, device_id),
            "state": state, "caps": caps, "open_turn": open_turn,
            "pending": [p for p in (served(e) for e in pending)
                        if p is not None],
            "queue": queued, "live": self.live(key),
            "plan": fold["plan"], "tasks": fold["tasks"],
            "checkpoints": fold["checkpoints"], "changes": fold["changes"],
            "usage": fold["usage"], "messages": page["messages"],
            "has_more": page["has_more"],
            "next_before": page["next_before"],
        }
        if reason:
            body["reason"] = reason
        return body


def _queued(send: dict) -> dict:
    """A hub-side waiting send (``sendq.Waiting``'s view) as a v2 queue
    item."""
    item = {"command_id": str(send.get("client_id") or ""),
            "text": str(send.get("text") or ""), "mode": "queue"}
    paths = [str(p) for p in send.get("attachments") or []]
    if paths:
        item["attachments"] = paths
    return item


class V2Stream:
    """One open v2 stream on one session. Subscribed from the moment it
    is made; the handler writes what :meth:`catch_up` and then each
    :meth:`next` return."""

    def __init__(self, sync: SyncHub, key: str, *, last: int = 0,
                 epoch: int | None = None,
                 device_id: str | None = None) -> None:
        self.sync = sync
        self.key = key
        self.last = max(0, last)
        self.asked_epoch = epoch
        self.device_id = device_id
        # The highest journal seq this stream has sent or skipped as
        # covered, and the epoch its client is on.
        self.sent = 0
        self.epoch = 0
        self.overflow = False
        self.closed = False
        self._q: queue.Queue = queue.Queue(maxsize=sync.queue_max)
        self._ping_at = sync.clock() + sync.ping_s
        sync._add(self)

    def put(self, item: tuple) -> None:
        """Called by the hub under its lock; never blocks. A full queue
        is not a lost event: the stream reads the journal again."""
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self.overflow = True

    def close(self) -> None:
        self.closed = True
        self.sync._remove(self)

    @property
    def waiting(self) -> bool:
        """Whether a call to :meth:`next` has something to work on
        besides the clock."""
        return self.overflow or not self._q.empty()

    # -- catching up --------------------------------------------------

    def catch_up(self) -> list[dict]:
        """The frames a new connection gets first: the events after
        ``last`` and the row when the client is on this epoch and not
        far behind, else one ``session.snapshot``; then
        ``session.state``."""
        epoch = self.sync.epoch(self.key)
        self.epoch = epoch
        return self._resume(self.last, self.asked_epoch == epoch)

    def _resume(self, after: int, same_epoch: bool) -> list[dict]:
        sync = self.sync
        journal = sync.hub.journal
        head = journal.last_seq(self.key)
        # Never a replay from 0 of a journal that has events: a ring
        # that was trimmed still reads "from the start" with nothing
        # missing, and a client with no cursor has nothing to add to.
        if same_epoch and (after > 0 or head == 0) and after <= head \
                and head - after <= sync.replay_max:
            events, truncated = journal.read_since(self.key, after)
            if not truncated:
                frames = []
                sent = after
                for event in events:
                    frame = served(event)
                    if frame is not None:
                        frames.append(frame)
                    seq = event.get("seq")
                    if isinstance(seq, int) and seq > sent:
                        sent = seq
                self.sent = sent
                # The row as it is now: a change to it while the client
                # was away (a title, a detach) has no event to replay.
                frames.extend(self._row())
                return frames + self._settle()
        return self._snapshot()

    def _settle(self) -> list[dict]:
        """The end of a catch-up: what was queued while it was built,
        then the state frame. The state item goes in the queue now, so
        its live text is true at exactly that place: every event queued
        before it is sent first (or was in the read), and a delta
        queued before it is dropped, because that text holds it."""
        sync = self.sync
        with sync._lock:
            mine = sync._state_item(self.key)
            self.put(mine)
        out: list[dict] = []
        while True:
            if self.overflow:
                # Not even the state item fitted.
                return out + self._recover()
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                # A snapshot taken on the way already ended with one.
                return out
            if item is mine:
                out.append(sync.state_frame(self.key, self.sent, item[1]))
                return out
            if item[0] in ("delta", "state"):
                continue  # older than the state this ends with
            if item[0] == "snapshot":
                return out + self._snapshot()
            out.extend(self._take(item))

    def _snapshot(self) -> list[dict]:
        sync = self.sync
        body = sync.snapshot(self.key, self.device_id)
        self.sent = body["head"]
        self.epoch = body["epoch"]
        return [protocol.sync_frame("session.snapshot", self.key, body,
                                    ts=sync.now())] + self._settle()

    def _row(self) -> list[dict]:
        frame = self.sync.row_frame(self.key, self.device_id)
        return [frame] if frame is not None else []

    def _recover(self) -> list[dict]:
        """The queue overflowed. Everything in it is dropped; what was
        journaled since the last frame sent is read again, as a
        catch-up does, and the row and state frames that end it bring
        what the dropped frames said. An event journaled after the
        queue is emptied is queued again, and one journaled before is
        in the read, so none is lost."""
        sync = self.sync
        with sync._lock:
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self.overflow = False
        return self._resume(self.sent, sync.epoch(self.key) == self.epoch)

    # -- the live loop ------------------------------------------------

    def next(self, block: bool = True) -> list[dict] | None:
        """The frames to write now: [] when there is nothing yet, None
        when the stream is over (the hub is stopping). Blocks until a
        frame is queued or the next ping is due, unless ``block`` is
        false."""
        sync = self.sync
        if sync.closed or self.closed:
            return None
        if self.overflow:
            return self._recover()
        due = self._ping_at - sync.clock()
        if due <= 0:
            self._ping_at = sync.clock() + sync.ping_s
            return [sync.ping_frame(self.key, self.sent)]
        try:
            item = self._q.get(timeout=due) if block \
                else self._q.get_nowait()
        except queue.Empty:
            return []
        if sync.closed or self.closed:
            return None
        return self._take(item)

    def _take(self, item: tuple) -> list[dict]:
        kind = item[0]
        if kind == "event":
            frame = item[1]
            seq = frame.get("seq")
            if isinstance(seq, int) and seq > 0:
                if seq <= self.sent:
                    return []  # the catch-up already covered it
                self.sent = seq
            return [frame]
        if kind == "skip":
            seq = item[1]
            if isinstance(seq, int) and seq > self.sent:
                self.sent = seq
            return []
        if kind == "delta":
            return [item[1]]
        if kind == "state":
            return [self.sync.state_frame(self.key, self.sent, item[1])]
        if kind == "row":
            return self._row()
        if kind == "snapshot":
            return self._snapshot()
        return []  # a wake
