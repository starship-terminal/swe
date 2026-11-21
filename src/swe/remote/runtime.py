"""The hub's side of the session keepers.

A session SWE owns runs under a keeper (``swe.remote.keeper``): one
process that holds the agent, the send queue and the event log, and
outlives the hub. ``SessionRuntime`` is everything the hub needs to
drive those sessions:

- **Launch and Continue in SWE** write the keeper's ``spec.json``,
  start it, connect, and record the session in ``remote.sqlite``.
- **Commands** (send, approval, control, queue edits) get a globally
  unique ``command_id``, are stored with their receipt before they are
  delivered, and are never applied twice: the store answers a retry
  with the stored receipt, and the keeper refuses an id it has seen.
- **Events** from a keeper are journaled under the keeper's own seq
  (``Journal.append_at``) as the v2 events they are. Today's clients
  read them through ``protocol_compat.to_v1`` (:meth:`v1`).
- **After a hub restart** it reconnects to every keeper still alive,
  replays from the last seq the journal holds, and checks every
  unfinished command against its keeper.

All of it sits behind ``remote.session_view`` in config.json and the
harness's ``capabilities.session`` adapter: with the setting off (and
no keeper left running) the hub behaves exactly as before.

Design: docs/specs/G8-hub-on-keepers.md.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from swe.paths import config_file_for
# The redaction a keeper applies to the queue text it shows.
from swe.reports.transcripts import _redact_secrets
from swe.remote import attachments, protocol, protocol_compat
from swe.remote.adapters import events as ev
from swe.remote.adapters import registry as adapter_registry
from swe.remote.keeper import discovery, ipc
from swe.remote.store import Store, StoreConflict, StoreError

logger = logging.getLogger("swe.remote.runtime")

# What a session row says its channel is.
MODE = "keeper"

# How long a connect waits for the keeper's replay to finish before the
# hub carries on (the events still arrive, in order, afterwards).
REPLAY_WAIT_S = 30.0
# How long ``close`` waits for a stopped keeper to hang up.
STOP_WAIT_S = 20.0
# A keeper whose lock is held but whose socket does not answer is on
# its way up or out (a killed process drops its sockets a moment before
# its lock). It is asked again this many times, this far apart, before
# the hub decides.
CONNECT_TRIES = 6
CONNECT_RETRY_S = 0.25
# How many times ``close`` looks for a stopped keeper's lock to be free
# before it says the keeper is still running.
STOP_TRIES = 20
# How long the hub waits before it asks a keeper again for events its
# journal could not take (a full disk does not clear in a millisecond).
RESYNC_WAIT_S = 2.0
# An undelivered command older than this is not delivered after a hub
# restart: the 24 hours the hub holds a message for a sleeping machine.
REDELIVER_MAX_AGE_S = 24 * 3600.0
# Ops a restarted hub delivers when the keeper never saw them. A Stop
# or an option change that arrives minutes late would act on a turn it
# was never meant for, so those are failed instead.
REDELIVERED_OPS = frozenset({"send", "respond"})

# v2 command ops under the names today's clients match results on.
_V1_OPS = {"send": "message", "respond": "approval"}

# A keeper's reasons a person should read as a sentence.
_REASONS = {
    "too_large": "that message is too large to send",
    "queue_full": "too many messages are already waiting for this session",
    "not_recorded": "the session could not record the message (disk full "
                    "or failing); it was not sent",
    "bad_request": "the session refused that request as malformed",
    "unknown_request": "that request was already answered, or is no "
                       "longer open",
    "unsupported": "this session's harness does not support that",
    "agent_down": "the agent is not running; send a message to restart it",
    "parked": "the session is parked; send a message to resume it",
    "not_queued": "that message is no longer queued",
    "busy": "the session is busy",
    "unknown_op": "the session's keeper is too old for that; it restarts "
                  "when the session is idle",
}


def enabled(home: Path | None = None) -> bool:
    """Whether ``remote.session_view`` is on in config.json."""
    from swe.configuration import load_config

    try:
        section = load_config(config_file_for(home)).get("remote")
    except Exception:  # an unreadable config is the setting left off
        return False
    return isinstance(section, dict) and section.get("session_view") is True


def new_command_id(prefix: str = "h") -> str:
    """A command id the hub mints: unique for ever, unlike a counter
    that restarts with the hub."""
    return f"{prefix}{secrets.token_hex(12)}"


class Refused(Exception):
    """A request the runtime answers with an HTTP error."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class DuplicateLaunch(Exception):
    """The command id of a launch already made one: ``launch_id``."""

    def __init__(self, launch_id: str) -> None:
        super().__init__(launch_id)
        self.launch_id = launch_id


class _Owned:
    """One session the hub drives through a keeper."""

    def __init__(self, key: str, harness: str, harness_id: str, cwd: str,
                 adapter: str, *, state: str = "starting",
                 options: dict | None = None, at_refs: bool = False,
                 created_at: float = 0.0) -> None:
        self.key = key
        self.harness = harness
        self.harness_id = harness_id
        self.cwd = cwd
        self.adapter = adapter
        self.options = dict(options or {})
        self.at_refs = at_refs
        self.state = state
        self.caps: dict = {}
        self.queue: list[dict] = []
        # request_id -> the approval.requested or input.requested event
        # still open.
        self.pending: dict[str, dict] = {}
        self.title = ""
        self.created_at = created_at
        self.active_at = created_at
        # Queued messages edited through this hub: command id -> the
        # text as typed (the keeper's queue shows text redacted).
        self.edits: dict[str, str] = {}
        self.client: ipc.KeeperClient | None = None
        self.gen = 0
        # The connection (its gen) whose events stopped being taken in
        # because the journal could not write one.
        self.broken = 0
        self.lost = False
        self.closing = False
        # One command to the keeper at a time, and one connect.
        self.cmd_lock = threading.RLock()
        # Set once the keeper's replay has been taken in.
        self.live = threading.Event()
        # Set when the connection to a keeper being stopped has ended.
        self.gone = threading.Event()
        # The transcript's {path, offset} when the keeper parked it.
        self.parked_at: dict | None = None
        self.noted_seq = 0

    @property
    def status(self) -> str:
        """The v1 ``session.status`` for the state."""
        if self.lost:
            return "error"
        return protocol_compat.STATUS_FOR_STATE.get(self.state, "done")

    @property
    def connected(self) -> bool:
        client = self.client
        return client is not None and not client.closed


class SessionRuntime:
    """See the module docstring. ``hub`` is the ``HubState``; ``spawn``
    starts a keeper (``discovery.spawn`` by default; tests pass one that
    runs a keeper on a thread)."""

    def __init__(self, hub, *, spawn: Callable[..., Any] | None = None,
                 on: bool | None = None, registry: Mapping | None = None,
                 runtime_dir: Path | str | None = None,
                 listeners: list | None = None) -> None:
        self.hub = hub
        self.home: Path | None = hub.home
        self.enabled = enabled(self.home) if on is None else on
        self.spawn = spawn or discovery.spawn
        # harness.json's contents for adapter lookups; None reads it.
        self.registry = registry
        # The IPC socket folder; None is the default under /tmp.
        self.runtime_dir = runtime_dir
        # Called with ``(key, event)`` for every v2 event taken into a
        # journal, on the keeper connection's thread, in order: where a
        # v2 stream will subscribe (G9), and what tests wait on.
        self.listeners: list[Callable[[str, dict], None]] = \
            list(listeners or [])
        self.store: Store | None = None
        self._sessions: dict[str, _Owned] = {}
        self._lock = threading.RLock()
        self._stopping = False
        self._stopped = threading.Event()
        self._at_refs: dict[str, bool] = {}
        # Keys closed read-only (resumed in a terminal): key -> reason.
        self._read_only: dict[str, str] = {}
        self._titles: dict[str, str] = {}

    # -- opening ------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.store is not None

    def open(self) -> None:
        """Open the store and reconnect to the keepers still alive.
        Called once, before the hub serves. With the setting off this
        does nothing unless a keeper is still running: its session is
        served until it is closed, but nothing new starts."""
        infos = discovery.discover(self.home)
        if not self.enabled and not any(i.alive for i in infos):
            return
        try:
            self.store = Store.open(self.home)
        except (StoreError, OSError) as exc:
            logger.error("session runtime off: %s", exc)
            print(f"swe remote hub: session runtime off: {exc}",
                  file=sys.stderr)
            self.enabled = False
            return
        store = self.store
        for row in store.list_sessions(machine=self.hub.machine_id):
            if row.control == "read_only":
                if row.control_reason:
                    self._read_only[row.key] = row.control_reason
                continue
            if row.state == "closed":
                continue
            self._adopt(_Owned(
                row.key, row.harness, row.harness_session_id, row.cwd,
                row.adapter or "", state=row.state, options=row.options,
                at_refs=self.at_refs(row.harness),
                created_at=row.created_at))
        by_key = {i.session_key: i for i in infos if i.session_key}
        for key, info in by_key.items():
            owned = self._sessions.get(key)
            if owned is None and info.alive:
                owned = self._adopt_stray(info)
            if owned is None:
                continue
            if info.alive:
                try:
                    self._connect(owned, info)
                except (ipc.IpcError, OSError) as exc:
                    logger.warning("keeper %s: %s", key, exc)
            elif info.lost:
                self._mark_lost(owned, announce=False)
            else:
                self._note_keeper(owned, info, "exited")
        for owned in list(self._sessions.values()):
            if not owned.connected and not owned.lost \
                    and owned.state not in ("parked", "closed"):
                # No keeper holds it: nothing is running.
                owned.state = "parked"
        self._settle_stale_launches()
        if self.enabled:
            self.seed_capabilities()

    def _adopt(self, owned: _Owned) -> None:
        with self._lock:
            self._sessions[owned.key] = owned

    def _adopt_stray(self, info: discovery.KeeperInfo) -> _Owned | None:
        """A live keeper the store does not know (the store was lost):
        record its session from the keeper's own files."""
        spec = discovery.read_spec(info.session_key, self.home)
        if spec is None or self.store is None:
            return None
        harness_id = info.harness_session_id or spec["session_id"] \
            or spec["resume_id"]
        if not harness_id:
            return None
        try:
            self.store.put_session(
                info.session_key, harness=spec["harness"],
                harness_session_id=harness_id, machine=self.hub.machine_id,
                cwd=spec["cwd"], state="starting")
        except (StoreConflict, ValueError):
            return None
        owned = _Owned(info.session_key, spec["harness"], harness_id,
                       spec["cwd"], "", at_refs=self.at_refs(spec["harness"]),
                       created_at=time.time())
        self._adopt(owned)
        return owned

    def _settle_stale_launches(self) -> None:
        """A launch still pending in the store was cut off by a hub
        restart. It started if its keeper is up (connecting to it just
        delivered the prompt, when the old hub had not: ``_reconcile``)
        or if the session ran at all. Otherwise nothing ever ran and
        nothing will: the launch failed, its prompt with it, and the
        session is closed, so nobody is shown an empty session that is
        waiting for a message it will never get."""
        store = self.store
        assert store is not None
        note = "the hub restarted while this was starting"
        for launch in store.list_launches(states=["pending"]):
            key = launch.session or ""
            owned = self._sessions.get(key)
            prompt = store.get_command(_prompt_id(launch.launch_id))
            taken = prompt is not None and (prompt.status != "queued"
                                            or prompt.result is not None)
            try:
                if owned is not None and (
                        owned.connected or taken
                        or self.hub.journal.last_seq(key) > 0):
                    store.settle_launch(launch.launch_id, "started")
                    continue
                store.settle_launch(launch.launch_id, "failed", note=note)
                if prompt is not None and not taken:
                    store.transition(prompt.command_id, "rejected",
                                     reason=note)
            except (StoreError, ValueError):
                logger.exception("settling launch %s", launch.launch_id)
                continue
            if owned is not None:
                self._disown(owned, state="closed")

    def stop(self) -> None:
        """Let go of every keeper (they keep running) and close the
        store."""
        self._stopping = True
        self._stopped.set()
        with self._lock:
            sessions = list(self._sessions.values())
        for owned in sessions:
            self._note_seq(owned)
            client, owned.client = owned.client, None
            if client is not None:
                client.close()
        if self.store is not None:
            self.store.close()

    def stop_keepers(self, timeout: float = STOP_WAIT_S) -> None:
        """Stop every connected keeper (tests, and nothing else)."""
        with self._lock:
            sessions = list(self._sessions.values())
        for owned in sessions:
            client = owned.client
            if client is None or client.closed:
                continue
            owned.closing = True
            try:
                client.request("stop", command_id=new_command_id("x"),
                               timeout=timeout)
            except ipc.IpcError:
                continue
            owned.gone.wait(timeout)

    # -- lookups ------------------------------------------------------

    def owns(self, key: str) -> bool:
        return key in self._sessions

    def session(self, key: str) -> _Owned | None:
        return self._sessions.get(key)

    def resolve(self, harness: str):
        return adapter_registry.adapter_for(harness, self.registry)

    def can_drive(self, harness: str) -> bool:
        """Whether a new or continued ``harness`` session goes through a
        keeper: the setting is on and the harness has an adapter."""
        return self.enabled and self.active and bool(
            self.resolve(harness).ok)

    def at_refs(self, harness: str) -> bool:
        if harness not in self._at_refs:
            reg = dict(self.registry) if self.registry is not None else None
            self._at_refs[harness] = attachments.at_refs(harness, reg)
        return self._at_refs[harness]

    def read_only_reason(self, key: str) -> str | None:
        return self._read_only.get(key)

    def rows(self) -> list[dict]:
        """What the catalog needs to list each owned session."""
        with self._lock:
            sessions = list(self._sessions.values())
        return [{
            "key": o.key, "harness": o.harness,
            "harness_session_id": o.harness_id, "cwd": o.cwd,
            "state": "agent_down" if o.lost else o.state,
            "status": o.status, "adapter": o.adapter, "caps": dict(o.caps),
            "title": o.title or self._titles.get(o.key, ""),
            "active_at": o.active_at or o.created_at,
            "model": str(o.options.get("model") or ""),
        } for o in sessions]

    # -- harness.json seed --------------------------------------------

    def seed_capabilities(self) -> list[str]:
        """Write ``capabilities.session`` into harness.json for each
        harness the seed table names, whose entry is registered and
        declares none yet. Never changes an entry that has one."""
        if self.registry is not None:
            return []
        from swe.remote.adapters.fallback import SESSION_SEED
        from swe.harness import registry as harness_registry

        try:
            reg = harness_registry.load_registry_if_present()
        except (OSError, ValueError):
            return []
        added = []
        for harness, seed in SESSION_SEED.items():
            entry = reg.get(harness)
            if not isinstance(entry, dict):
                continue
            caps = entry.get("capabilities")
            if caps is not None and not isinstance(caps, dict):
                continue
            if isinstance(caps, dict) and "session" in caps:
                continue
            entry["capabilities"] = {**(caps or {}), "session": {
                name: list(value) if isinstance(value, (list, tuple))
                else value for name, value in seed.items()}}
            added.append(harness)
        if added:
            try:
                harness_registry.save_registry(reg)
            except (OSError, ValueError) as exc:
                logger.warning("could not seed harness.json: %s", exc)
                return []
        return added

    # -- connecting ---------------------------------------------------

    def _connect(self, owned: _Owned, info: discovery.KeeperInfo) -> None:
        """Connect to ``owned``'s live keeper, take in its replay and
        check the commands still open against it."""
        with owned.cmd_lock:
            last = self.hub.journal.last_seq(owned.key)
            client, gate = self._dial(owned, info, last)
            try:
                welcome = client.welcome
                head = welcome.get("last_seq")
                if isinstance(head, int) and head < last:
                    # Not the log this journal followed (the keeper's
                    # data was wiped): start the journal over from its
                    # log.
                    # (Its frames are dropped, not applied: the gen
                    # moves on before its gate opens.)
                    client.close()
                    owned.gen += 1
                    gate.set()
                    self.hub.journal.drop(owned.key)
                    self._truncated(owned.key, 0)
                    client, gate = self._dial(owned, info, 0)
                    welcome = client.welcome
                owned.client = client
                owned.lost = False
                # The welcome is the state at the moment the keeper
                # subscribed this connection; every event it sends is
                # at or after that. So the welcome goes in first, and
                # only then are the connection's events let through.
                self._take_welcome(owned, welcome)
            finally:
                gate.set()
            self._note_keeper(owned, info, "live")
            owned.live.wait(REPLAY_WAIT_S)
            self._reconcile(owned)

    def _dial(self, owned: _Owned, info: discovery.KeeperInfo,
              last_seq: int) -> tuple[ipc.KeeperClient, threading.Event]:
        """A connected client and the gate its frames wait behind: the
        client's reader starts inside ``connect``, and nothing it reads
        may be applied before the welcome is."""
        owned.gen += 1
        gen = owned.gen
        owned.live.clear()
        owned.gone.clear()
        gate = threading.Event()

        def on_frame(frame: dict) -> None:
            gate.wait(REPLAY_WAIT_S)
            self._on_frame(owned, gen, frame)

        client = ipc.KeeperClient(info.socket, last_seq=last_seq,
                                  on_frame=on_frame)
        try:
            client.connect()
        except BaseException:
            gate.set()
            raise
        return client, gate

    def _take_welcome(self, owned: _Owned, welcome: dict) -> None:
        state = welcome.get("state")
        if isinstance(state, str) and state in ev.SESSION_STATES:
            owned.state = state
        if isinstance(welcome.get("caps"), dict):
            owned.caps = welcome["caps"]
        if isinstance(welcome.get("queue"), list):
            owned.queue = [q for q in welcome["queue"] if isinstance(q, dict)]
        harness_id = welcome.get("harness_session_id")
        if isinstance(harness_id, str) and harness_id:
            owned.harness_id = harness_id
        pending = {}
        for event in welcome.get("pending") or []:
            data = event.get("data") if isinstance(event, dict) else None
            rid = data.get("request_id") if isinstance(data, dict) else None
            if isinstance(rid, str) and rid:
                pending[rid] = event
        owned.pending = pending
        # The text of the messages in progress: the hub missed their
        # deltas while it was away, and v2 streams resume from it.
        self.hub.sync.on_welcome(owned.key, welcome.get("live"),
                                 welcome.get("last_seq"))
        # The inbox and the notification tokens know open requests only
        # from events; after a hub restart those were sent long ago.
        self.hub.restore_approvals(owned.key, {
            rid: float(e.get("ts") or time.time())
            for rid, e in pending.items()})

    def _note_keeper(self, owned: _Owned, info: discovery.KeeperInfo,
                     state: str) -> None:
        store = self.store
        if store is None or not info.pid or not info.socket.startswith("/"):
            return
        try:
            if state == "live" or store.get_keeper(info.slug) is None:
                store.put_keeper(
                    info.slug, session=owned.key, pid=info.pid,
                    socket_path=info.socket,
                    ipc_version=max(1, info.ipc_version), state=state)
            else:
                store.touch_keeper(info.slug, state=state)
        except (StoreError, ValueError, OSError):
            logger.exception("recording keeper %s", info.slug)

    def _ensure_keeper(self, owned: _Owned) -> ipc.KeeperClient:
        """The connected client for ``owned``, starting a keeper when
        none is alive. Caller holds ``owned.cmd_lock``."""
        if owned.connected:
            assert owned.client is not None
            return owned.client
        for attempt in range(CONNECT_TRIES):
            info = discovery.find(owned.key, self.home)
            if info is None or not info.alive:
                info = self._start_keeper(owned)
            try:
                self._connect(owned, info)
            except (ipc.IpcError, OSError) as exc:
                # Its lock is held and its socket is not there: a
                # keeper on its way out. Look again shortly.
                logger.info("keeper %s: %s", owned.key, exc)
                if self._stopped.wait(CONNECT_RETRY_S):
                    break
                continue
            if owned.connected:
                assert owned.client is not None
                return owned.client
        raise Refused(503, "keeper_failed",
                      "the session's keeper is not answering")

    def _secret_env(self, owned: _Owned) -> dict:
        """Provider keys for the agent: environment only, never a file."""
        provider = str(owned.options.get("provider") or "")
        if not provider:
            return {}
        from swe.remote import launch_provider

        keys = launch_provider.read_keys(launch_provider.keys_path(self.home))
        reg = dict(self.registry) if self.registry is not None else None
        return launch_provider.provider_env(owned.harness, provider, keys, reg)

    def _start_keeper(self, owned: _Owned) -> discovery.KeeperInfo:
        """Start a keeper for a session whose spec is on disk. Its log
        starts (or carries on) past everything the journal holds."""
        discovery.update_spec(
            owned.key, self.home,
            first_seq=self.hub.journal.last_seq(owned.key) + 1)
        try:
            env = self._secret_env(owned)
        except ValueError as exc:
            raise Refused(409, "unsupported_provider", str(exc)) from exc
        kwargs: dict[str, Any] = {"home": self.home, "env": env or None}
        if self.runtime_dir is not None:
            kwargs["runtime"] = self.runtime_dir
        try:
            return self.spawn(owned.key, **kwargs)
        except discovery.KeeperRunning:
            info = discovery.find(owned.key, self.home)
            if info is not None and info.alive:
                return info
            raise Refused(503, "keeper_failed",
                          "another keeper holds this session")
        except discovery.SpawnError as exc:
            tail = (exc.output or "").strip().splitlines()
            raise Refused(503, "keeper_failed", (
                f"could not start the session: {exc}"
                + (f" ({tail[-1]})" if tail else ""))) from exc

    # -- frames from a keeper -----------------------------------------

    def _on_frame(self, owned: _Owned, gen: int, frame: dict) -> None:
        """One frame from ``owned``'s keeper, on its reader thread, in
        order. Frames of a connection that was replaced are dropped."""
        if gen != owned.gen:
            return
        kind = frame.get("t")
        if kind != "closed" and owned.broken == gen:
            return  # nothing more from a connection that lost an event
        if kind == "event":
            event = frame.get("e")
            if isinstance(event, dict):
                try:
                    taken = self._on_event(owned, event)
                except Exception:
                    logger.exception("keeper event for %s", owned.key)
                    taken = True
                if not taken:
                    # The journal could not write this event. Taking
                    # the next one would leave a hole no replay fills
                    # (a replay starts after the journal's last seq), so
                    # this connection ends here and the next one asks
                    # for everything from the last event that was kept.
                    logger.warning("journal write failed for %s; asking "
                                   "its keeper again", owned.key)
                    owned.broken = gen
                    client = owned.client
                    if client is not None:
                        client.close()
        elif kind == "live":
            owned.live.set()
            # The replay is in: v2 streams take the state now, with the
            # text the welcome restored (sending it before the replay
            # would let a replayed turn.ended clear it on the client).
            self.hub.sync.resync(owned.key)
        elif kind == "gap":
            oldest = frame.get("oldest_seq")
            self._truncated(owned.key,
                            oldest - 1 if isinstance(oldest, int) else 0)
        elif kind == "closed":
            owned.live.set()
            owned.gone.set()
            client = owned.client
            if client is not None and client.closed:
                # Its reader has ended; the socket is still ours to shut.
                owned.client = None
                client.close()
            if self._stopping or owned.closing:
                return
            threading.Thread(target=self._recover, args=(owned, gen),
                             name="swe-keeper-recover", daemon=True).start()

    def _truncated(self, key: str, seq: int) -> None:
        """Tell open streams the journal cannot be made whole: they
        refetch the messages. The session's epoch moves on, so a v2
        client's cursor from before is answered with a snapshot, never
        replayed across the hole."""
        self.hub.sync.new_epoch(key)
        self.hub.bus.publish(key, {"type": "journal.truncated", "seq": seq})

    def _recover(self, owned: _Owned, gen: int) -> None:
        """The connection to a keeper ended and nobody asked for that:
        reconnect when it is alive, else record what happened to it."""
        if owned.broken == gen and self._stopped.wait(RESYNC_WAIT_S):
            return
        with owned.cmd_lock:
            if gen != owned.gen or self._stopping or owned.closing \
                    or not self.owns(owned.key):
                return
            info = None
            for _attempt in range(CONNECT_TRIES):
                info = discovery.find(owned.key, self.home)
                if info is None or not info.alive:
                    break
                try:
                    self._connect(owned, info)
                    return
                except (ipc.IpcError, OSError) as exc:
                    # Lock held, socket gone: it is on its way out.
                    # Its lock says which way in a moment.
                    logger.info("keeper %s: %s", owned.key, exc)
                    if self._stopped.wait(CONNECT_RETRY_S):
                        return
            if info is not None and not info.alive and not info.lost:
                self._note_keeper(owned, info, "exited")
                if owned.state not in ("parked", "closed"):
                    self._set_state(owned, "parked", announce=True)
                return
            # Lost, or alive and not answering: either way the turn is
            # cut off as far as anyone can drive it.
            if info is not None and not info.alive:
                self._note_keeper(owned, info, "lost")
            self._mark_lost(owned, announce=True)

    def _mark_lost(self, owned: _Owned, *, announce: bool) -> None:
        """The keeper is gone without a clean stop: the turn was cut
        off. Nothing is written to the timeline here (the next keeper
        records it); open streams are told so no card waits for ever."""
        old = owned.status
        owned.lost = True
        owned.client = None
        pending, owned.pending = owned.pending, {}
        owned.queue = []
        self._store_state(owned, "agent_down")
        if not announce:
            self._patch_row(owned)
            return
        for rid in pending:
            self.hub.publish_kept(owned.key, protocol.event(
                "approval.resolved", owned.key, self.hub.machine_id,
                {"request_id": rid, "source": "keeper_lost"}), seq=False)
        self._announce_status(owned, old)

    # -- events -------------------------------------------------------

    def _on_event(self, owned: _Owned, event: dict) -> bool:
        """Take one keeper event in. False only when the journal could
        not write it (the caller then stops this connection)."""
        etype = event.get("type")
        if etype == "message.delta":
            # v2 streams get the delta as it is; today's clients its
            # v1 frame.
            self.hub.sync.on_event(owned.key, event)
            for v1 in protocol_compat.to_v1(
                    event, machine=self.hub.machine_id,
                    at_refs=owned.at_refs):
                self.hub.publish_transient(owned.key, v1)
            return True
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return True
        key = owned.key
        if seq <= self.hub.journal.last_seq(key):
            return True  # already taken in before a reconnect
        # Effects first, the journal second: a hub that dies in between
        # is sent this event again, and every effect here can be applied
        # twice. The journal's last seq therefore means "all done up to
        # here".
        old = owned.status
        self._apply(owned, event)
        with self.hub.journal.hold(key):
            if not self.hub.journal.append_at(key, event):
                return False
            # Under the journal's lock: v2 streams are queued events in
            # seq order, whichever connection's thread brought them.
            self.hub.sync.on_event(key, event)
        owned.active_at = float(event.get("ts") or time.time())
        v1 = self.v1(key, event)
        if v1 is not None:
            self.hub.publish_kept(key, v1)
        if etype == "session.state":
            self._patch_row(owned)
            self._notify(owned, old, owned.status)
        for listener in self.listeners:
            listener(key, event)
        return True

    def _apply(self, owned: _Owned, event: dict) -> None:
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        store = self.store
        cid = event.get("command_id")
        if etype == "session.state":
            state = data.get("state")
            harness_id = data.get("harness_session_id")
            if isinstance(harness_id, str) and harness_id:
                owned.harness_id = harness_id
            if isinstance(state, str) and state in ev.SESSION_STATES:
                owned.lost = False
                owned.parked_at = None
                if state != owned.state:
                    owned.state = state
                    self._store_state(owned, state)
        elif etype == "session.caps":
            if isinstance(data.get("caps"), dict):
                owned.caps = data["caps"]
        elif etype == "queue.updated":
            items = data.get("items")
            owned.queue = [q for q in items if isinstance(q, dict)] \
                if isinstance(items, list) else []
            waiting = {q.get("command_id") for q in owned.queue}
            for gone in [c for c in owned.edits if c not in waiting]:
                del owned.edits[gone]
        elif etype in ("approval.requested", "input.requested"):
            rid = data.get("request_id")
            if isinstance(rid, str) and rid:
                owned.pending[rid] = event
        elif etype == "approval.resolved":
            owned.pending.pop(str(data.get("request_id") or ""), None)
        elif etype == "message.completed":
            if data.get("kind") == "user" and not owned.title:
                owned.title = _title_of(str(data.get("text") or ""))
        elif etype == "command.receipt" and store is not None \
                and isinstance(cid, str):
            status = data.get("status")
            try:
                if status == "accepted":
                    store.transition(cid, "accepted")
                elif status == "rejected":
                    store.transition(cid, "rejected",
                                     reason=_text(data.get("reason")))
            except (StoreError, ValueError):
                logger.exception("receipt of %s", cid)
        elif etype == "command.result" and store is not None \
                and isinstance(cid, str):
            result = {k: data[k] for k in ("ok", "note", "degraded",
                                           "cancelled", "request_id",
                                           "launch_id") if k in data}
            try:
                if data.get("ok") is True:
                    # A message that waited in the queue has no second
                    # receipt: its result is what says it went.
                    store.transition(cid, "accepted")
                store.set_result(cid, result)
            except (StoreError, ValueError):
                logger.exception("result of %s", cid)
        elif etype == "turn.ended":
            self._note_seq(owned, event.get("seq"))

    def _note_seq(self, owned: _Owned, seq: Any = None) -> None:
        """Record how far the hub has read, at a turn boundary or on the
        way out: every store write is an fsync, so never per event."""
        if seq is None:
            seq = self.hub.journal.last_seq(owned.key)
        if self.store is None or not isinstance(seq, int) \
                or seq <= owned.noted_seq:
            return
        try:
            self.store.note_seq(owned.key, seq)
            owned.noted_seq = seq
        except (StoreError, ValueError):
            logger.exception("note_seq for %s", owned.key)

    def _store_state(self, owned: _Owned, state: str) -> None:
        if self.store is None:
            return
        try:
            self.store.update_session(owned.key, state=state)
        except (StoreError, ValueError):
            logger.exception("state of %s", owned.key)

    def _set_state(self, owned: _Owned, state: str, *,
                   announce: bool) -> None:
        old = owned.status
        owned.state = state
        self._store_state(owned, state)
        if announce:
            self._announce_status(owned, old)
        else:
            self._patch_row(owned)

    def _announce_status(self, owned: _Owned, old: str) -> None:
        """A status the keeper did not log (it is gone): streams and the
        list still need it."""
        self._patch_row(owned)
        self.hub.publish_kept(owned.key, protocol.event(
            "session.status", owned.key, self.hub.machine_id,
            {"status": owned.status}), seq=False)
        self._notify(owned, old, owned.status)

    def _patch_row(self, owned: _Owned) -> None:
        self.hub.catalog.patch(owned.key, {
            "status": owned.status or None,
            "state": "agent_down" if owned.lost else owned.state,
            "caps": dict(owned.caps),
        }, owned.status)
        if owned.status != "active":
            self.hub.detaches.nudge(owned.key)

    def _notify(self, owned: _Owned, old: str, new: str) -> None:
        """The pushes the tailer raises for a session it follows:
        needs_you on a fresh followup, finished on active to done, error
        on a turn to error."""
        if new == old:
            return
        if new == "followup":
            kind = "needs_you"
        elif new == "done" and old == "active":
            kind = "finished"
        elif new == "error":
            kind = "error"
        else:
            return
        self.hub.emit(owned.key, protocol.event(
            "notify", owned.key, self.hub.machine_id,
            {"kind": kind, "status": new}))

    # -- v2 to v1 -----------------------------------------------------

    def v1(self, key: str, event: dict) -> dict | None:
        """``event`` as today's clients take it: a v2 event converted
        (None when v1 has no frame for it), anything else unchanged."""
        if not isinstance(event, dict) or event.get("v") != ev.VERSION:
            return event
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        machine = self.hub.machine_id
        if event.get("type") == "input.requested":
            # v1 has no frame for a question. One with choices fits the
            # approval card old clients already draw; without this the
            # session would look idle to them while it waits.
            options = data.get("options") or []
            if data.get("kind") != "choice" or not options:
                return None
            out = protocol.event("approval.requested", key, machine, {
                "request_id": data.get("request_id"),
                "prompt": data.get("prompt") or "",
                "options": protocol_compat._option_rows(options),
            }, ts=event.get("ts"))
            out["seq"] = event.get("seq", 0)
            return out
        try:
            frames = protocol_compat.to_v1(
                event, machine=machine,
                at_refs=self.at_refs(key.split(":", 1)[0]))
        except (ValueError, KeyError, TypeError):
            return None
        if not frames:
            return None
        out = frames[0]
        if out.get("type") == "command.result":
            body = out["data"]
            body["op"] = _V1_OPS.get(body.get("op"), body.get("op"))
        return out

    # -- commands -----------------------------------------------------

    def _record(self, owned: _Owned, cid: str, op: str, payload: dict,
                device: str | None):
        assert self.store is not None
        if device is not None and not _DEVICE_OK(device):
            device = None
        try:
            return self.store.record_command(
                cid, session=owned.key, op=op, status="queued",
                payload=payload, machine=self.hub.machine_id, device=device)
        except ValueError as exc:
            raise Refused(422, "bad_command", str(exc)) from exc
        except StoreError as exc:
            raise Refused(503, "store_failed", str(exc)) from exc

    def _settle(self, cid: str, reply: dict) -> tuple[str, str]:
        """Bring the stored receipt in line with the keeper's answer;
        returns ``(status, reason)`` with a duplicate read as its first
        answer."""
        status = reply.get("status")
        if status == "duplicate":
            status = reply.get("first") or "accepted"
        reason = _text(reply.get("reason")) or ""
        store = self.store
        if store is not None:
            try:
                if status == "accepted":
                    store.transition(cid, "accepted")
                elif status == "rejected":
                    store.transition(cid, "rejected", reason=reason or None)
            except (StoreError, ValueError):
                logger.exception("receipt of %s", cid)
        return str(status or ""), reason

    def _deliver(self, owned: _Owned, op: str, cid: str,
                 **fields: Any) -> tuple[str, str]:
        """Send one stored command to the keeper; ``(status, reason)``.
        A connection that fails mid-request is retried once under the
        same id: the keeper answers ``duplicate`` if it already has
        it."""
        with owned.cmd_lock:
            try:
                reply = None
                for attempt in (1, 2):
                    client = self._ensure_keeper(owned)
                    try:
                        reply = client.request(op, command_id=cid, **fields)
                        break
                    except ipc.IpcError:
                        client.close()
                        owned.client = None
                        if attempt == 2:
                            raise
            except Refused as exc:
                self._fail(cid, exc.message)
                raise
            except (ipc.IpcError, OSError) as exc:
                # Not known to have been applied: the receipt stays
                # open, and the next connect settles it.
                raise Refused(503, "keeper_failed", (
                    f"the session's keeper did not answer: {exc}")) from exc
            assert reply is not None
            return self._settle(cid, reply)

    def _fail(self, cid: str, note: str) -> None:
        if self.store is None:
            return
        try:
            self.store.transition(cid, "rejected", reason=note)
        except (StoreError, ValueError):
            logger.exception("rejecting %s", cid)

    def _owned(self, key: str) -> _Owned:
        owned = self._sessions.get(key)
        if owned is None or self.store is None:
            raise Refused(404, "unknown_session", "no such session")
        return owned

    def _duplicate(self, receipt) -> dict:
        body = {"ok": True, "duplicate": True, "command_id":
                receipt.command_id, "receipt": receipt.original}
        if receipt.result is not None:
            body["result"] = receipt.result
        if receipt.reason:
            body["reason"] = receipt.reason
        return body

    def send(self, key: str, text: str, *, mode: str = "queue",
             paths: list[str] | None = None, device: str | None = None,
             command_id: str | None = None) -> tuple[int, dict]:
        """``POST .../message`` for an owned session."""
        owned = self._owned(key)
        cid = command_id or new_command_id()
        payload = {"text": text, "mode": mode,
                   "attachments": list(paths or [])}
        receipt = self._record(owned, cid, "send", payload, device)
        if receipt.conflict:
            raise Refused(409, "command_id_conflict", receipt.reason or "")
        base = {"ok": True, "queued": True, "client_id": command_id or "",
                "command_id": cid}
        if receipt.duplicate:
            return 202, {**base, **self._duplicate(receipt),
                         "waiting": receipt.original == "queued"
                         and receipt.result is None}
        status, reason = self._deliver(
            owned, "send", cid, text=text, mode=mode,
            attachments=list(paths or []) or None, device=device or None)
        if status == "rejected":
            raise Refused(409, "send_rejected", _reason(reason))
        return 202, {**base, "receipt": status,
                     "waiting": status == "queued"}

    def respond(self, key: str, request_id: str, decision: str, *,
                device: str | None = None,
                command_id: str | None = None) -> tuple[int, dict]:
        """``POST .../approval`` for an owned session. ``decision`` is
        allow, deny, always, or the number of a menu row."""
        owned = self._owned(key)
        cid = command_id or new_command_id()
        payload = {"request_id": request_id, "decision": decision}
        receipt = self._record(owned, cid, "respond", payload, device)
        if receipt.conflict:
            raise Refused(409, "command_id_conflict", receipt.reason or "")
        if receipt.duplicate:
            return 200, self._duplicate(receipt)
        fields: dict[str, Any] = {"request_id": request_id,
                                  "device": device or None}
        if decision in ev.DECISIONS:
            fields["decision"] = decision
        else:
            asked = owned.pending.get(request_id)
            options = ((asked or {}).get("data") or {}).get("options") or []
            index = int(decision) - 1 if decision.isdigit() else -1
            if not 0 <= index < len(options):
                self._fail(cid, "no such option")
                raise Refused(409, "approval_failed", (
                    "that request is no longer open" if asked is None
                    else f"this request has no option {decision}"))
            option_id = str(options[index].get("id") or "")
            fields["option_id"] = option_id
            if option_id in ev.DECISIONS:
                fields["decision"] = option_id
        if not owned.connected:
            self._fail(cid, "the session is not running")
            raise Refused(409, "approval_failed",
                          "the session is not running")
        status, reason = self._deliver(owned, "respond", cid, **fields)
        if status == "rejected":
            raise Refused(409, "approval_failed", _reason(reason))
        return 200, {"ok": True, "note": "", "command_id": cid,
                     "receipt": status}

    def command(self, key: str, op: str, *, device: str | None = None,
                command_id: str | None = None, **fields: Any
                ) -> tuple[str, str]:
        """Any other keeper op (interrupt, set_option, stop_task,
        queue_edit, queue_remove) for an owned session, stored and
        delivered once; ``(status, reason)``."""
        owned = self._owned(key)
        cid = command_id or new_command_id()
        payload = {k: v for k, v in fields.items() if v is not None}
        receipt = self._record(owned, cid, op, payload, device)
        if receipt.conflict:
            raise Refused(409, "command_id_conflict", receipt.reason or "")
        if receipt.duplicate:
            return str(receipt.original or "accepted"), receipt.reason or ""
        if not owned.connected:
            self._fail(cid, "the session is not running")
            raise Refused(409, "control_failed",
                          "the session is not running")
        return self._deliver(owned, op, cid, **payload)

    def control(self, key: str, op: str, value: str, *,
                device: str | None = None,
                command_id: str | None = None) -> tuple[int, dict]:
        """``POST .../control`` ops that reach the agent: interrupt,
        model, effort."""
        if op == "interrupt":
            status, reason = self.command(key, "interrupt", device=device,
                                          command_id=command_id)
        elif op in ("model", "effort"):
            if not value:
                raise Refused(422, "bad_command", f"{op} needs a value")
            status, reason = self.command(
                key, "set_option", device=device, command_id=command_id,
                name=op, value=value)
        else:
            raise Refused(409, "control_failed", (
                f"/{op} is not available for a session SWE runs through "
                "its session runtime yet"))
        if status == "rejected":
            raise Refused(409, "control_failed", _reason(reason))
        return 200, {"ok": True, "note": ""}

    def queue_view(self, key: str) -> list[dict]:
        """The waiting sends, in the shape ``GET .../queue`` lists."""
        owned = self._sessions.get(key)
        if owned is None:
            return []
        return [{"client_id": str(q.get("command_id") or ""),
                 "text": str(q.get("text") or ""),
                 "attachments": list(q.get("attachments") or []),
                 "queued_at": 0.0} for q in owned.queue]

    def queue_action(self, key: str, target: str, op: str, text: str | None,
                     *, device: str | None = None,
                     command_id: str | None = None) -> tuple[int, dict]:
        """``POST .../queue``: edit, cancel, or send now one queued
        message. ``target`` is the id that message was sent under;
        ``command_id`` is this action's own id, so a retry of the
        action answers what the first try did."""
        owned = self._owned(key)
        cid = command_id or new_command_id()
        body = {"ok": True, "client_id": target, "op": op,
                "command_id": cid}
        if op == "edit":
            # One edit at a time, so the text remembered here is the
            # text of the edit the keeper applied last.
            with owned.cmd_lock:
                fresh = self.store is None \
                    or self.store.get_command(cid) is None
                status, reason = self.command(
                    key, "queue_edit", device=device, command_id=cid,
                    target=target, text=text)
                if status == "rejected":
                    raise _queue_gone(reason)
                if fresh and isinstance(text, str) and any(
                        q.get("command_id") == target for q in owned.queue):
                    # Only an edit that was applied now: a retry of an
                    # older edit answers its receipt and changes
                    # nothing, here or in the keeper. Kept only while
                    # the message waits (``_apply`` drops it after).
                    owned.edits[target] = text
            return 200, {**body, "text": text}
        if op != "steer":
            status, reason = self.command(key, "queue_remove", device=device,
                                          command_id=cid, target=target)
            if status == "rejected":
                raise _queue_gone(reason)
            return 200, body
        # Send now. The keeper has no "send this one now": the message
        # leaves the queue and goes again as a steer under an id made
        # from this action's. The text to send again is stored with the
        # removal, so a retry of the action sends the same words.
        prior = self.store.get_command(cid) if self.store else None
        if prior is not None and prior.op == "queue_remove" \
                and prior.payload.get("target") == target:
            again = str(prior.payload.get("resend") or "")
            paths = list(prior.payload.get("attachments") or [])
        else:
            again, paths = self._queued_text(owned, target)
        fields: dict[str, Any] = {"target": target, "resend": again}
        if paths:
            fields["attachments"] = paths
        status, reason = self.command(key, "queue_remove", device=device,
                                      command_id=cid, **fields)
        if status == "rejected":
            raise _queue_gone(reason)
        self.send(key, again, mode="steer", paths=paths, device=device,
                  command_id="s" + hashlib.sha1(cid.encode()).hexdigest()[:24])
        return 200, body

    def _queued_text(self, owned: _Owned, target: str
                     ) -> tuple[str, list[str]]:
        """The text and files of the queued message ``target`` as they
        stand now: an edit made through this hub, else what was sent,
        else what the queue shows. The queue shows text redacted, so it
        is used only when it is not just the redacted original (the
        message was edited before this hub started)."""
        queued = next((q for q in owned.queue
                       if q.get("command_id") == target), None)
        if queued is None:
            raise _queue_gone("not_queued")
        stored = self.store.get_command(target) if self.store else None
        payload = stored.payload if stored is not None \
            and stored.op == "send" else {}
        paths = list(payload.get("attachments")
                     or queued.get("attachments") or [])
        shown = str(queued.get("text") or "")
        if target in owned.edits:
            return owned.edits[target], paths
        original = str(payload.get("text") or "")
        if original and _redact_secrets(original) == shown:
            return original, paths
        return shown, paths

    # -- after a restart: commands with no result ---------------------

    def _reconcile(self, owned: _Owned) -> None:
        """Check every command stored without a result against the
        keeper, now that its replay is in. Caller holds ``cmd_lock``.

        The keeper answers on its one loop, so by the time it says it
        knows an id, that command's result (if it has one) has already
        come down this connection. A command it knows, that has no
        result and is not in its queue, was lost with an earlier keeper:
        it is failed, never sent again. One it never saw was taken by a
        hub that died before delivering it: a message or an answer is
        delivered now, anything else is failed.
        """
        store, client = self.store, owned.client
        if store is None or client is None:
            return
        open_ = store.unfinished(session=owned.key)
        if not open_:
            return
        now = time.time()
        try:
            for cmd in open_:
                known = client.request("has_command",
                                       command_id=cmd.command_id)
                fresh = store.get_command(cmd.command_id)
                if fresh is None or fresh.result is not None:
                    continue
                if known.get("known"):
                    status = client.request("status")
                    queued = {q.get("command_id")
                              for q in status.get("queue") or []
                              if isinstance(q, dict)}
                    if cmd.command_id in queued:
                        continue
                    if known.get("status") == "rejected":
                        store.transition(cmd.command_id, "rejected",
                                         reason="rejected")
                        continue
                    self._lost_command(owned, cmd, (
                        "lost when the session's keeper stopped; "
                        "send it again"))
                elif cmd.op in REDELIVERED_OPS \
                        and now - cmd.received_at < REDELIVER_MAX_AGE_S:
                    fields = dict(cmd.payload)
                    if cmd.op == "respond":
                        decision = str(fields.pop("decision", ""))
                        if decision in ev.DECISIONS:
                            fields["decision"] = decision
                        else:
                            self._lost_command(owned, cmd, (
                                "the hub restarted before this answer "
                                "was delivered"))
                            continue
                    if not fields.get("attachments"):
                        fields.pop("attachments", None)
                    reply = client.request(cmd.op,
                                           command_id=cmd.command_id,
                                           device=cmd.device, **fields)
                    self._settle(cmd.command_id, reply)
                else:
                    self._lost_command(owned, cmd, (
                        "the hub restarted before this was delivered"))
        except (ipc.IpcError, StoreError, ValueError):
            logger.exception("checking open commands of %s", owned.key)

    def _lost_command(self, owned: _Owned, cmd, note: str) -> None:
        """Finish a command that will never run, and tell open streams
        (the keeper's log has no result for it)."""
        assert self.store is not None
        if not self.store.set_result(cmd.command_id,
                                     {"ok": False, "note": note}):
            return
        self.hub.publish_kept(owned.key, protocol.event(
            "command.result", owned.key, self.hub.machine_id, {
                "command_id": cmd.command_id,
                "op": _V1_OPS.get(cmd.op, cmd.op), "ok": False,
                "note": note, "degraded": False,
                "client_id": cmd.command_id}), seq=False)

    # -- launch -------------------------------------------------------

    def launch(self, launch_id: str, harness: str, cwd: str, prompt: str, *,
               model: str = "", effort: str = "", provider: str = "",
               command_id: str | None = None,
               resume_id: str = "", fork: bool = False) -> str:
        """Record a new session and its launch, then start its keeper
        on a thread; returns the session key. ``resume_id`` with
        ``fork`` starts it as a fork of that harness session."""
        store = self.store
        if store is None:
            raise Refused(409, "not_launchable",
                          "the session runtime is not running")
        resolved = self.resolve(harness)
        if not resolved.ok:
            raise Refused(422, "not_launchable", resolved.reason)
        if fork and not resolved.caps.fork:
            raise Refused(409, "fork_unsupported", (
                f"{harness} sessions can't be forked through the session "
                "runtime"))
        session_id = str(uuid.uuid4())
        key = protocol.session_key(harness, session_id)
        options = {k: v for k, v in (("model", model), ("effort", effort),
                                     ("provider", provider)) if v}
        owned = _Owned(key, harness, session_id, cwd, resolved.adapter,
                       options=options, at_refs=self.at_refs(harness),
                       created_at=time.time())
        owned.title = _title_of(prompt)
        prompt_id = _prompt_id(launch_id)
        try:
            # The prompt first, so a launch never exists without the
            # message it owes: a hub that dies after this either
            # delivers it when it reconnects or fails the launch.
            if prompt.strip():
                self._record(owned, prompt_id, "send", _prompt_payload(
                    prompt), None)
            try:
                # Then the launch, which claims the command id: of two
                # requests under one id, one gets here and the other
                # is told which launch that was.
                store.add_launch(launch_id, machine=self.hub.machine_id,
                                 harness=harness, cwd=cwd, options=options,
                                 command_id=command_id, session=key)
            except StoreConflict:
                made = self.launch_for(command_id)
                if made is None:
                    raise
                if prompt.strip():
                    self._fail(prompt_id, "another request made this launch")
                raise DuplicateLaunch(made) from None
            store.put_session(
                key, harness=harness, harness_session_id=session_id,
                machine=self.hub.machine_id, cwd=cwd, state="starting",
                adapter=resolved.adapter, options=options)
        except (StoreError, ValueError) as exc:
            # Nothing was started. Whatever was stored says so.
            try:
                store.settle_launch(launch_id, "failed", note=str(exc))
                if prompt.strip() and store.get_command(prompt_id):
                    self._fail(prompt_id, "the launch could not be stored")
            except (StoreError, ValueError):
                logger.exception("undoing launch %s", launch_id)
            raise Refused(503, "store_failed", str(exc)) from exc
        self._titles[key] = owned.title
        try:
            discovery.write_spec(
                key, harness=harness, cwd=cwd, resume_id=resume_id,
                fork=fork, model=model, effort=effort,
                session_id=session_id, home=self.home)
        except (OSError, ValueError) as exc:
            store.update_session(key, state="closed")
            store.settle_launch(launch_id, "failed", note=str(exc))
            raise Refused(422, "launch_failed", str(exc)) from exc
        self._adopt(owned)
        threading.Thread(
            target=self._run_launch, args=(owned, launch_id, prompt),
            name=f"remote-launch-{launch_id}", daemon=True).start()
        return key

    def _run_launch(self, owned: _Owned, launch_id: str,
                    prompt: str) -> None:
        hub = self.hub
        try:
            with owned.cmd_lock:
                self._ensure_keeper(owned)
            if prompt.strip():
                # Connecting already delivered it when the keeper had
                # not seen it; asking again answers how that went.
                status, reason = self._deliver(
                    owned, "send", _prompt_id(launch_id), text=prompt,
                    mode="queue")
                if status == "rejected":
                    raise Refused(409, "send_rejected", _reason(reason))
        except Refused as exc:
            self._launch_failed(owned, launch_id, exc.message)
            return
        except Exception as exc:  # never leave a launch pending for a day
            logger.exception("launch %s", launch_id)
            self._launch_failed(owned, launch_id, f"launch failed: {exc}")
            return
        # The row first: a launch reads as started only once the list
        # has its session.
        hub.catalog.refresh()
        if self.store is not None:
            self.store.settle_launch(launch_id, "started")
        hub.emit(hub_global_key(), protocol.event(
            "session.launched", owned.key, hub.machine_id,
            {"launch_id": launch_id}))

    def _launch_failed(self, owned: _Owned, launch_id: str,
                       note: str) -> None:
        self.hub.settle_launch(launch_id, state="failed", note=note)
        if self.store is not None:
            try:
                self.store.settle_launch(launch_id, "failed", note=note)
            except (StoreError, ValueError):
                logger.exception("settling launch %s", launch_id)
        try:
            self.close(owned.key, quiet=True)
        except Refused:
            pass

    def launch_view(self, launch_id: str) -> dict | None:
        """A launch as ``GET /v1/launches/<id>`` shows it, from the
        store: the hub that took it may have restarted since."""
        if self.store is None:
            return None
        launch = self.store.get_launch(launch_id)
        if launch is None:
            return None
        options = launch.options
        return {
            "launch_id": launch.launch_id, "machine": launch.machine,
            "harness": launch.harness, "cwd": launch.cwd,
            "state": launch.state, "model": str(options.get("model") or ""),
            "effort": str(options.get("effort") or ""),
            "yolo": bool(options.get("yolo")),
            "provider": str(options.get("provider") or ""),
            "key": launch.session or "", "note": launch.note or "",
            "created_at": launch.created_at,
        }

    def launch_for(self, command_id: str | None) -> str | None:
        """The launch a retried ``POST /v1/sessions`` already made."""
        if self.store is None or not command_id:
            return None
        launch = self.store.launch_for_command(command_id)
        return launch.launch_id if launch is not None else None

    def launch_matches(self, launch_id: str, harness: str, cwd: str,
                       prompt: str, *, model: str = "", effort: str = "",
                       provider: str = "") -> bool:
        """Whether ``launch_id`` is the launch these fields ask for: a
        command id reused for another launch is a conflict, not a
        retry."""
        if self.store is None:
            return False
        launch = self.store.get_launch(launch_id)
        if launch is None:
            return False
        options = {k: v for k, v in (("model", model), ("effort", effort),
                                     ("provider", provider)) if v}
        sent = self.store.get_command(_prompt_id(launch_id))
        text = str(sent.payload.get("text") or "") if sent is not None \
            else ""
        return (launch.harness, launch.cwd, launch.options, text) == (
            harness, cwd, options, prompt if prompt.strip() else "")

    # -- Continue in SWE ----------------------------------------------

    def continue_session(self, key: str, session, *, model: str = "",
                         effort: str = "", env: dict | None = None) -> str:
        """Take a terminal-started session over: resume it by its own
        id under a keeper whose log starts at the journal's next seq.
        The caller has checked that it is idle and that nothing else
        holds it. Returns a note; raises :class:`Refused`."""
        store = self.store
        assert store is not None
        harness = session.tool_name
        resolved = self.resolve(harness)
        if not resolved.ok:
            raise Refused(422, "not_attachable", resolved.reason)
        if self.owns(key):
            return "already continued in SWE"
        options = {k: v for k, v in (("model", model), ("effort", effort))
                   if v}
        owned = _Owned(key, harness, session.session_id, session.path or "",
                       resolved.adapter, options=options,
                       at_refs=self.at_refs(harness), created_at=time.time())
        owned.title = session.title or ""
        journal = self.hub.journal
        # From here the tailer's events for this key are no longer
        # journaled, so the seq read below is the last one it wrote.
        with journal.hold(key):
            self._adopt(owned)
            first_seq = journal.last_seq(key) + 1
        self.hub.tail_manager.drop(key)
        try:
            store.put_session(
                key, harness=harness,
                harness_session_id=session.session_id,
                machine=self.hub.machine_id, cwd=session.path or "",
                state="starting", adapter=resolved.adapter, options=options)
            discovery.write_spec(
                key, harness=harness, cwd=session.path or "/",
                resume_id=session.session_id, model=model, effort=effort,
                env=env, first_seq=first_seq, home=self.home)
            with owned.cmd_lock:
                self._ensure_keeper(owned)
        except Exception as exc:
            info = discovery.find(key, self.home)
            if info is not None and info.alive:
                # A keeper came up and then would not answer: stop it
                # before the session goes back to its terminal. If it
                # will not stop, ``close`` says so and SWE keeps it.
                self.close(key, quiet=True)
            else:
                self._disown(owned, state="closed")
            if isinstance(exc, Refused):
                raise
            if isinstance(exc, (StoreError, ValueError, OSError)):
                raise Refused(409, "not_attachable", str(exc)) from exc
            raise
        self._read_only.pop(key, None)
        return "continued in SWE"

    # -- letting a session go -----------------------------------------

    def close(self, key: str, *, quiet: bool = False,
              read_only: str | None = None,
              tail_from: dict | None = None) -> None:
        """Stop ``key``'s keeper and give the session back to the
        tailer: it is a terminal session again. ``read_only`` records
        why SWE no longer drives it; ``tail_from`` is the transcript
        ``{path, offset}`` the tailer picks up at (default: its end)."""
        owned = self._owned(key)
        with owned.cmd_lock:
            owned.closing = True
            if not owned.connected:
                # A keeper still alive that this hub is not connected
                # to must be stopped too, or it would run on unowned.
                info = discovery.find(key, self.home)
                if info is not None and info.alive:
                    try:
                        self._connect(owned, info)
                    except (ipc.IpcError, OSError):
                        logger.warning("could not reach the keeper of %s "
                                       "to stop it", key)
            client = owned.client
            if client is not None and not client.closed:
                cid = new_command_id("x")
                try:
                    self._record(owned, cid, "stop", {}, None)
                    client.request("stop", command_id=cid,
                                   timeout=STOP_WAIT_S)
                    owned.gone.wait(STOP_WAIT_S)
                except (ipc.IpcError, Refused):
                    if not quiet:
                        logger.warning("stopping the keeper of %s", key)
                self._settle(cid, {"status": "accepted"})
                if self.store is not None:
                    try:
                        self.store.set_result(cid, {"ok": True})
                    except (StoreError, ValueError):
                        pass
                client.close()
            owned.client = None
            if not self._keeper_gone(key):
                # Still running: the session stays ours. Letting it go
                # now would leave an agent working that nobody drives,
                # beside whoever resumes the session next.
                owned.closing = False
                threading.Thread(target=self._recover,
                                 args=(owned, owned.gen),
                                 name="swe-keeper-recover",
                                 daemon=True).start()
                raise Refused(503, "keeper_failed", (
                    "the session's keeper did not stop; the session is "
                    "still attached. Try again"))
            self._note_seq(owned)
            self._disown(owned, state="closed", read_only=read_only,
                         tail_from=tail_from)

    def _keeper_gone(self, key: str) -> bool:
        """Whether no keeper holds ``key``'s lock, looking a few times:
        a keeper hangs up a moment before it lets its lock go."""
        for _attempt in range(STOP_TRIES):
            info = discovery.find(key, self.home)
            if info is None or not info.alive:
                return True
            if self._stopped.wait(CONNECT_RETRY_S):
                break
        return False

    def _disown(self, owned: _Owned, *, state: str,
                read_only: str | None = None,
                tail_from: dict | None = None) -> None:
        key = owned.key
        # The cursor first, while the key is still ours and so cannot
        # be tailed: a tailer started in between would pick up the
        # cursor from before the keeper and journal its turns again.
        self.hub.tail_manager.hand_back(key, tail_from)
        with self._lock:
            self._sessions.pop(key, None)
        self.hub.restore_approvals(key, {})
        if self.store is not None:
            try:
                if read_only:
                    self.store.update_session(
                        key, state=state, control="read_only",
                        control_reason=read_only)
                    self._read_only[key] = read_only
                else:
                    self.store.update_session(key, state=state)
            except (StoreError, ValueError):
                logger.exception("closing %s", key)

    # -- a parked session someone resumed in a terminal ---------------

    def tick(self) -> None:
        """Run from the catalog loop. A parked session whose transcript
        grew was resumed somewhere else, so SWE lets go of it. A keeper
        started by another swe version is stopped once its session is
        idle, so the next send starts one on this version's code."""
        with self._lock:
            connected = [o for o in self._sessions.values()
                         if not o.lost and o.connected]
        for owned in connected:
            try:
                if owned.state == "parked":
                    self._check_parked(owned)
                if self.owns(owned.key) and owned.connected \
                        and owned.state in ("idle", "parked"):
                    self._cycle_if_old(owned)
            except Exception:
                logger.exception("checking %s", owned.key)

    def _cycle_if_old(self, owned: _Owned) -> None:
        """Stop a keeper that runs another swe version (or an older
        IPC), when nothing is in flight. A keeper keeps the code it
        started with for as long as it lives; the session stays ours,
        and the next send starts a keeper that resumes it."""
        import swe

        info = discovery.find(owned.key, self.home)
        if info is None or not info.alive:
            return
        theirs = info.raw.get("swe_version")
        if (not isinstance(theirs, str) or not theirs
                or theirs == getattr(swe, "__version__", "")) \
                and info.ipc_version >= ipc.VERSION:
            return
        with owned.cmd_lock:
            client = owned.client
            if client is None or client.closed:
                return
            try:
                status = client.request("status")
            except ipc.IpcError:
                return
            if status.get("open_turn") is not None or status.get("pending") \
                    or status.get("queue") \
                    or status.get("state") not in ("idle", "parked"):
                return
            owned.closing = True
            try:
                client.request("stop", command_id=new_command_id("x"),
                               timeout=STOP_WAIT_S)
                owned.gone.wait(STOP_WAIT_S)
            except ipc.IpcError:
                pass
            finally:
                client.close()
                owned.client = None
                owned.closing = False
            self._note_keeper(owned, info, "exited")
            self._set_state(owned, "parked", announce=False)

    def _check_parked(self, owned: _Owned) -> None:
        info = discovery.find(owned.key, self.home)
        mark = info.raw.get("transcript") if info is not None else None
        if not isinstance(mark, dict) or not isinstance(mark.get("path"), str) \
                or not isinstance(mark.get("offset"), int):
            return
        try:
            size = os.path.getsize(mark["path"])
        except OSError:
            return
        if size <= mark["offset"]:
            return
        with owned.cmd_lock:
            # Asked on the keeper's own loop, with no command of ours
            # in between: still parked means our agent wrote none of it.
            client = owned.client
            if client is None or client.closed:
                return
            try:
                status = client.request("status")
            except ipc.IpcError:
                return
            if status.get("agent") != "parked":
                return
            self.close(owned.key, read_only=(
                "resumed in a terminal while it was parked; it is "
                "read-only here until that run ends"), tail_from=mark)
        self.hub.session_released(owned.key)


# ---------------------------------------------------------------------


def hub_global_key() -> str:
    from swe.remote import hub

    return hub.GLOBAL_KEY


def _prompt_id(launch_id: str) -> str:
    """The command id a launch's first message is stored under."""
    return f"{launch_id}-prompt"


def _prompt_payload(prompt: str) -> dict:
    return {"text": prompt, "mode": "queue", "attachments": []}


def _DEVICE_OK(device: str) -> bool:
    from swe.remote.store import _DEVICE_RE

    return bool(_DEVICE_RE.fullmatch(device))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _reason(reason: str) -> str:
    return _REASONS.get(reason, reason or "the session refused it")


def _queue_gone(reason: str) -> Refused:
    if reason == "not_queued":
        return Refused(409, "not_waiting", "that message is no longer queued")
    return Refused(409, "queue_failed", _reason(reason))


def _title_of(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line[:80]
