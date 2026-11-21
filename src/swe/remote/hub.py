"""The hub: REST + SSE over ``http.server``, device auth, event fan-out.

Stdlib-only by design — SSE is a held HTTP GET, so no WebSocket
dependency enters the tree. ``HubState`` is the reusable core: the
catalog refreshes sessions, the tail manager turns transcripts into
events, and one ``emit`` funnel writes every event to the journal and
the subscriber bus. The satellite agent builds the same core minus the
listener.

Auth: every ``/v1/*`` route except ``/v1/status`` and ``/v1/pair`` wants
``Authorization: Bearer <device credential>`` from ``devices.json``.
The approval POST alone also accepts a scoped, single-use ntfy action
token (``notify.ApprovalTokens``) so a notification never carries a
full-power credential.
"""

from __future__ import annotations

import email.message
import ipaddress
import json
import os
import queue
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.utils import formatdate
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from swe.github import cli as ghcli
from swe.github import shared as github_shared
from swe.paths import atomic_write_text, read_json_dict, remote_dir_for
from swe.remote import (
    attachments,
    catalog as catalog_mod,
    devices as devices_mod,
    injected as injected_mod,
    dismissals,
    files as files_mod,
    inject,
    notify,
    pairing,
    readstate,
    peer as peer_mod,
    preview as preview_mod,
    protocol,
    search as search_mod,
    sessionmeta,
    sendq,
    slash,
    sshkeys,
    supervise,
    tail,
    tasks as tasks_mod,
    tsid,
    usage as usage_mod,
)
from swe.remote import agent as agent_mod
from swe.remote import fork as fork_mod
from swe.remote import issues as issues_mod
from swe.remote import pulls as pulls_mod
from swe.remote import runtime as runtime_mod
from swe.remote import store as store_mod
from swe.remote import stream as stream_mod
from swe.remote import sync as sync_mod
from swe.remote.detach import PendingDetaches
from swe.remote.journal import Journal
from swe.webapp import (
    api as webapp_api,
    harnesses as webapp_harnesses,
    pages as webapp_pages,
    static as webapp_static,
)

DEFAULT_PORT = 8477
_MAX_BODY = 4 << 20  # 4 MB JSON cap; attachments go through their own route
_SSE_HEARTBEAT_S = 15.0
# A request that stalls mid-read (declared Content-Length, no bytes)
# can't hold a handler thread forever; SSE writes get the same cap,
# which is harmless — a healthy client's socket never blocks for 30 s.
_REQ_TIMEOUT_S = 30.0
# A response sent before the handler read the request body (a 401, 404,
# 409, 413 ...) first discards that body, or the next request on the
# keep-alive connection would be parsed out of its bytes. Up to this
# much, within this long, is read and dropped so the connection stays
# open; anything bigger, slower or unframed closes the connection.
# ``_scoped_approval_device``'s answer when it already sent the 403 for
# a live ntfy token asked to spend a choice it does not carry.
_NTFY_REFUSED = "\0ntfy-refused"
_DRAIN_MAX = 1 << 20
_DRAIN_S = 2.0
_MAX_WORKERS = 64
# How often an open SSE stream re-checks that its device wasn't revoked.
_REVOCATION_CHECK_S = 5.0
# The web app's generated bundles, served beside its static files.
_BUNDLES = frozenset({"pages.js", "pages.css"})
# Sent on every response that doesn't set its own. The policy leaves
# script sources alone: the page bundle evaluates each page script.
_SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "frame-ancestors 'self'; object-src 'none'; base-uri 'none'"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)

# Peers that count as "this machine". Behind ``tailscale serve`` every
# request arrives from one of these, with the real client in headers.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

# Behind serve the owner lookup needs this node's tailnet IP, which a hub
# started at login can ask for before Tailscale is up; it retries on use
# this often until it settles.
_OWNER_RETRY_S = 30.0

# The owner's own devices skip pairing, so every page load asks who the
# caller is. ``tailscale whois`` is a subprocess; its answer per IP is
# kept this long (a miss for less, so a device that just joined the
# tailnet isn't locked out for long).
_WHOIS_TTL_S = 60.0
_WHOIS_MISS_TTL_S = 10.0

# Journal bucket for non-session events (machine.online, hub-level notify).
GLOBAL_KEY = "_global"

# ``queue``-mode sends poll a fresh session status on this cadence and
# give up after the cap — a turn outliving it fails the send honestly.
QUEUE_POLL_S = protocol.IDLE_POLL_S
QUEUE_WAIT_MAX_S = protocol.IDLE_WAIT_MAX_S
# Per-key in-flight sends (waiting plus running) get an honest 429
# rather than an unbounded backlog nobody reads.
MAX_SEND_BACKLOG = 20

# Refuse new commands for a satellite whose unacked queue is deeper
# than this — the journal trims at ~4k records and silently dropped
# commands are worse than an honest 429.
MAX_SAT_PENDING = 2000

# /v1/folders lists at most this many subfolders of one folder.
FOLDER_LIST_CAP = 500

# The unauthenticated pair routes (/v1/pair, /v1/pair/request, and the
# owner-identity path of /v1/pair/requests*) allow this many attempts
# per source IP per window.
_PAIR_ATTEMPT_MAX = 5
_PAIR_ATTEMPT_S = 600
# A background loop that keeps failing logs its traceback at most this
# often per loop step, so a stuck step shows in the unit log without
# flooding it every 250 ms.
_LOOP_ERROR_LOG_S = 300.0
# The catalog loop drops expired pair requests (and any uncollected
# credential on them) at most this often.
_PAIR_PRUNE_S = 60.0

# Low power (#1520): with no SSE stream open (the ntfy notifier holds
# one while it can push) and no client request for CLIENT_IDLE_S, the
# tailers poll at tail.POLL_*_LOW_S, the catalog refreshes every
# CATALOG_IDLE_S and the tail loop ticks every TAIL_TICK_LOW_S. A
# client request ends it at once (``HubState.note_client``).
CLIENT_IDLE_S = 120.0
CATALOG_IDLE_S = 60.0
TAIL_TICK_S = 0.25
TAIL_TICK_LOW_S = 1.0
# A list request that finds the catalog older than this (the first one
# after low power) waits up to CATALOG_CATCHUP_S for a fresh refresh,
# so a client that polls without a stream never reads minute-old rows.
CATALOG_STALE_S = 2 * catalog_mod.REFRESH_INTERVAL_S
CATALOG_CATCHUP_S = 5.0
# The tail loop sweeps stale journals, cursors and attachments this often.
SWEEP_S = 300.0

# A launch the phone started stays answerable at /v1/launches/<id> this
# long; past it the tmux pane has long since been adopted or abandoned.
LAUNCH_TTL_S = 86400

# An unanswered approval expires from the inbox after this long: a dead
# satellite or a killed pane can never emit ``approval.resolved``, so
# without an expiry the item outlives the prompt itself.
APPROVAL_TTL_S = 86400

# Event types a satellite may journal via /v1/agent/events:
# protocol.EVENT_TYPES plus the two agent→hub bookkeeping types the
# phone never sends. Anything else is dropped, not journaled.
# ``compaction`` left EVENT_TYPES (nothing ever emitted it; compactions
# are ``message.appended`` with that kind) but an older satellite's
# build still lists it, so the hub keeps taking it rather than start
# refusing a type it used to accept.
_SAT_EVENT_TYPES = protocol.EVENT_TYPES | {
    "session.launched", "command.result", "compaction",
}

# Per-type ``data.kind`` vocabularies for the events that carry one.
# The journal feeds the clients verbatim, so an unknown kind would render
# unvetted markup there; these events must name a known kind.
_SAT_KIND_SETS = {
    "message.appended": protocol.MESSAGE_KINDS,
    "message.delta": protocol.MESSAGE_KINDS,
    "notify": protocol.NOTIFY_KINDS,
}


def _sat_delta(mid: str, local_key: str, data) -> dict | None:
    """A satellite's ``message.delta``, rebuilt from its known fields
    under the hub-scoped key, or None when it is malformed. The
    satellite coalesced it already; the hub only relays it."""
    if not local_key or not isinstance(data, dict):
        return None
    msg_id, text, seq = data.get("id"), data.get("text"), data.get("seq")
    if not isinstance(msg_id, str) or not msg_id or \
            not isinstance(text, str) or \
            not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return None
    return protocol.delta_event(
        protocol.remote_key(mid, local_key), mid, msg_id[:200],
        str(data.get("kind")), text, seq, done=data.get("done") is True)


_SAT_PREFIX = "_sat_"
_NO_FRAME = object()


class SharedEvent(dict):
    """One published event, the same object in every subscriber's queue.

    It carries its SSE frame, cleaned and encoded by the first
    subscriber that writes it, so N open streams pay for one
    ``clean_event`` + ``json.dumps`` + encode instead of N. Two threads
    racing on the first write both compute the same bytes; the last
    assignment wins, harmlessly. Subscribers only read the dict.
    """

    __slots__ = ("frame",)

    def __init__(self, event: dict) -> None:
        super().__init__(event)
        self.frame = _NO_FRAME


def sse_frame(ev: dict) -> bytes | None:
    """``ev`` as the bytes of one SSE frame, or None when it is not served.

    A transient event (``message.delta``) has no seq and no ``id:``
    line, so an EventSource's Last-Event-ID stays on the last journaled
    event.
    """
    if isinstance(ev, SharedEvent):
        frame = ev.frame
        if frame is _NO_FRAME:
            frame = ev.frame = _encode_frame(ev)
        return frame
    return _encode_frame(ev)


def sse_frame_v2(frame: dict) -> bytes:
    """One v2 frame as SSE bytes. Only a journaled event has an ``id:``
    line (its seq): a delta, a ping, a snapshot and anything the hub
    made for the stream (``seq`` 0) leave Last-Event-ID where it is."""
    seq = frame.get("seq")
    return (
        (f"id: {seq}\n" if isinstance(seq, int) and seq > 0 else "")
        + f"event: {frame.get('type', 'message')}\n"
        + f"data: {json.dumps(frame, ensure_ascii=False, default=str)}\n\n"
    ).encode("utf-8")


def _encode_frame(ev: dict) -> bytes | None:
    ev = injected_mod.clean_event(ev)
    if ev is None:
        return None
    return (
        (f"id: {ev['seq']}\n" if "seq" in ev else "")
        + f"event: {ev.get('type', 'message')}\n"
        + f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
    ).encode("utf-8")


def _ownership_event(key: str, machine_id: str, ownership, tmux, mode,
                     kept) -> dict:
    """The ``session.ownership`` event every ownership change emits."""
    return protocol.event("session.ownership", key, machine_id, {
        "ownership": ownership, "tmux": tmux, "mode": mode,
        "kept": bool(kept),
    })


class EventBus:
    """Fan-out to SSE subscriber queues, keyed by session or global."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, "queue.Queue"]] = []
        self._lock = threading.Lock()

    def subscribe(self, key: str) -> "queue.Queue":
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append((key, q))
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            self._subs = [(k, s) for k, s in self._subs if s is not q]

    def watched(self) -> bool:
        """Whether any stream is open besides satellite command queues
        (those carry the hub's commands out, not its sessions to anyone)."""
        with self._lock:
            return any(
                not key.startswith(_SAT_PREFIX) for key, _q in self._subs
            )

    def publish(self, key: str, event: dict) -> None:
        """Hand ``event`` to ``key``'s subscribers. The global stream is
        its own key (``GLOBAL_KEY``), fed by ``HubState.emit``."""
        with self._lock:
            subs = list(self._subs)
        if not isinstance(event, SharedEvent):
            event = SharedEvent(event)
        for want, q in subs:
            if want == key:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass  # a wedged client misses events, never blocks the hub


class HubState:
    """Everything the HTTP handlers close over; threads start here."""

    def __init__(self, home: Path | str | None = None, *,
                 runtime: dict | None = None) -> None:
        self.home = Path(home) if home is not None else None
        self.machine = catalog_mod.local_machine(self.home)
        self.machine_id = self.machine["id"]
        self.journal = Journal(self.home)
        self.bus = EventBus()
        # Live token deltas from this hub's own API channels, coalesced
        # per message and published without a journal entry.
        self.deltas = stream_mod.DeltaStream(self.publish_transient)
        self.catalog = catalog_mod.Catalog(self.home, self.machine_id)
        self.tail_manager = tail.TailManager(
            emit=self.emit,
            machine_id=self.machine_id,
            home=self.home,
            journal_has=self._journal_has,
            # Hot or growing sessions get their status re-probed between
            # catalog refreshes; the catalog keeps the fresher of the two.
            status_of=lambda sessions: self.status_of(sessions),
            on_status=self._on_probed_status,
        )
        # The web app's page data (/v1/app/*): its ledger opens on the
        # first request, so a hub nobody browses never scans.
        self.webapp = webapp_api.WebApp(self.home)
        self.started_at = time.time()
        self._stop = threading.Event()
        # Set to cut the catalog loop's wait short — a launch finishing
        # its tmux prime means a transcript lands any second.
        self._catalog_wake = threading.Event()
        self._threads: list[threading.Thread] = []
        # Satellite presence: command-stream subscribers are the online
        # signal — a sleeping MacBook just drops offline until it dials in.
        self._sat_subs: dict[str, int] = {}
        # Each satellite stream connects and disconnects on its own
        # request thread. The lock also spans the online/offline write
        # and event, so a disconnect racing a connect can't persist the
        # flags out of order.
        self._sat_subs_lock = threading.Lock()
        # (machine, session key) -> command id of its last queued
        # ``tail``, so a chat reopened (or an SSE reconnecting) while
        # the satellite is offline queues one tail, not one per GET.
        self._sat_tail_cmds: dict[tuple[str, str], str] = {}
        # Spans queue_tail's check, enqueue and record, so two opens at
        # once can't both queue. Taken before ``_cmd_lock``, never under.
        self._sat_tail_lock = threading.Lock()
        # Guards every piece of satellite ack
        # bookkeeping below — command streams, result uploads and
        # queue_command all run on different request threads.
        self._cmd_lock = threading.Lock()
        # The contiguous floor of what each satellite has *completed*:
        # ``?last=`` sets it on connect, ``command.result`` moves it
        # over consecutive seqs through ``_sat_done`` — a control that
        # finishes while an earlier message still runs must not raise
        # it, or the journal could trim commands a restart still owes.
        self._sat_acked: dict[str, int] = {}
        # machine_id -> {command_id: journal seq} for queued commands
        # awaiting their result — how a ``command.result`` finds the
        # queue position it acks. Entries at or under the floor are
        # pruned; a hub restart starts the map over (the agent's own
        # floor survives in its cursor file).
        self._sat_cmd_seqs: dict[str, dict[str, int]] = {}
        # machine_id -> completed seqs above the contiguous floor —
        # the same out-of-order set the agent's cursor keeps, so the
        # floor only climbs a run with no gaps in it.
        self._sat_done: dict[str, set[int]] = {}
        # Message sends run here — a headless turn or a queue-until-idle
        # wait must not hold the phone's POST open.
        self.sendq = sendq.SendQueue()
        # queue-mode sends not yet handed to an injector, by session
        # and client_id: GET/POST /v1/sessions/{key}/queue read it.
        self.waiting = sendq.Waiting()
        # ``detach`` with ``mode: when_idle``, waiting out a turn.
        self.detaches = PendingDetaches(self._stop)
        # Fresh per-session status for queued sends; tests stub it.
        from swe.sessions.status import session_statuses
        self.status_of = session_statuses
        # Tailscale identity lookup for pair requests; tests stub it.
        self.whois = tsid.whois
        self._whois_cache: dict[str, tuple[float, dict | None]] = {}
        self._whois_lock = threading.Lock()
        # One lookup in flight per IP: a page firing several requests at
        # once waits on the first instead of forking its own whois.
        self._whois_ip_locks: dict[str, threading.Lock] = {}
        # The hub owner's tailnet login — set by serve() from whois of
        # the bind address; "" means owner-identity approval is off.
        self.owner_login = ""
        # This machine's own tailnet node, set with owner_login. A
        # request that whois names as this node came from a process on
        # this machine (serve's local listener, or a direct dial of the
        # tailnet IP), whichever user runs it, so it never counts as
        # the owner's device.
        self.own_node_id = ""
        # Set by configure() when that lookup could not run yet; owner()
        # calls it again, at most every _OWNER_RETRY_S.
        self.owner_retry = None
        self._owner_tried_at = 0.0
        # Unauthenticated pair-route rate limits: ip → timestamps, one
        # bucket per route so token tries, request floods and owner-
        # identity approvals don't share a budget. One lock guards all.
        self._pair_req_times: dict[str, list[float]] = {}
        self._pair_ex_times: dict[str, list[float]] = {}
        self._pair_dec_times: dict[str, list[float]] = {}
        self._pair_req_lock = threading.Lock()
        self._loop_error_at: dict[str, float] = {}
        self._pair_pruned_at = 0.0
        # Low power (#1520): when a client last made a request
        # (monotonic), and a condition the catalog loop notifies after
        # each refresh, which a list request may wait on
        # (``fresh_catalog``). ``_catalog_running`` says a loop is there
        # to wait for.
        self._client_seen_at = float("-inf")
        self._catalog_done = threading.Condition()
        self._catalog_running = False
        # When the newest GitHub poll that finished had started (epoch s);
        # an inbox read after low power waits for it too.
        self._github_at = 0.0
        # Host values this hub answers to — the owner-identity approval
        # path requires one, and the unauthenticated routes answer only
        # these (filled by serve() from the bind host + whois); empty
        # disables both checks.
        self.allowed_hosts: set[str] = set()
        # Set for a wildcard bind, which answers on every interface:
        # any IP-literal Host on ``allowed_port`` passes too. A rebound
        # page always arrives under its own domain name, never an IP,
        # so this keeps LAN addresses working without a name check gap.
        self.allowed_ip_port: int | None = None
        # The hub's reachable base — set by serve() before start() so
        # ntfy action buttons can point back at this host.
        self.base_url = ""
        # Set when the hub runs behind ``tailscale serve`` (``--public-url``
        # on a loopback bind): serve overwrites X-Forwarded-For/-Proto
        # with the real tailnet client, so loopback requests carrying
        # them are read as that client. Off, the headers are ignored.
        self.trust_proxy = False
        # Whether the loopback peer ``(server, peer)`` is the Tailscale
        # daemon itself (peer.is_proxy). With trust_proxy on, only its
        # X-Forwarded-For names the client: any other local process
        # could write one. Tests stub it.
        self.proxy_peer = peer_mod.is_proxy
        # Whether that check can see socket owners on this host at all
        # (peer.owner_lookup_works). serve() asks once, only to warn;
        # proxy_peer is the same either way. Tests stub it.
        self.proxy_lookup_works = peer_mod.owner_lookup_works
        # session key -> request_id of its newest approval.requested
        # that has not yet been answered — local panes and satellites
        # both flow through emit(), so the inbox sees them uniformly.
        # Derived from ``_approval_at`` by ``_sync_pending_approval``.
        self.pending_approvals: dict[str, str] = {}
        # One-shot, minutes-lived bearer tokens the ntfy action buttons
        # send instead of a device credential (notify.ApprovalTokens).
        self.approval_tokens = notify.ApprovalTokens()
        # session key -> {request_id: requested_at} for every open
        # approval. A Codex app-server holds overlapping approvals, so
        # resolving one must leave the others pending. Entries expire on
        # APPROVAL_TTL_S, since a dead satellite or killed pane can
        # never emit the approval.resolved that would clear them.
        # ``_approval_lock`` covers both dicts — the expiry sweep and
        # emit's updates run on different request threads.
        self._approval_at: dict[str, dict[str, float]] = {}
        self._approval_lock = threading.Lock()
        # launch_id -> {machine, harness, cwd, state, key, note,
        # created_at} for every POST /v1/sessions. ``state`` goes pending
        # -> started (a ``session.launched`` event names the key) or
        # failed (the launch, or the satellite's command, said no).
        self.launches: dict[str, dict] = {}
        self._launch_lock = threading.Lock()
        # One id per hub process, in ``GET /v1/status`` and every
        # snapshot: a client that sees it change knows the hub
        # restarted (it then asks again about what it sent).
        self.boot_id = secrets.token_hex(8)
        # What ``v=2`` streams are fed, and the live text and snapshot
        # they catch up from (``sync.py``). Made before the runtime: a
        # keeper found at start hands it the text in progress.
        self.sync = sync_mod.SyncHub(self)
        # The sessions SWE runs through keepers (``runtime.py``), behind
        # ``remote.session_view``. ``runtime`` passes its options
        # through (tests: a spawner, a registry). Opened last: it reads
        # the journal and may publish at once.
        self.runtime = runtime_mod.SessionRuntime(self, **(runtime or {}))
        self.catalog.owned_rows = self.runtime.rows
        self.catalog.read_only_reason = self.runtime.read_only_reason
        self.tail_manager.skip = self.runtime.owns
        self.runtime.open()

    def status_view(self) -> dict:
        """``GET /v1/status``: a reachability check that also says what
        this hub speaks. ``v`` stays 1 for today's clients; a v2 client
        reads ``protocols`` and ``features``, and ``boot_id`` to notice
        a restart. Still nothing that names the machine."""
        features = list(protocol.FEATURES)
        if self.runtime.active:
            features.append(protocol.FEATURE_RUNTIME)
        else:
            # No store: no session is kept and none can be looked up.
            # A satellite keeper's row would still say ``keeper`` while
            # every keeper-only post to it was refused (G31).
            features = [f for f in features
                        if f not in protocol.STORE_FEATURES]
        return {
            "ok": True,
            "v": protocol.PROTOCOL_VERSION,
            "protocols": list(protocol.PROTOCOLS),
            "features": features,
            "boot_id": self.boot_id,
        }

    def new_launch(
        self, machine: str, harness: str, cwd: str,
        model: str = "", effort: str = "", yolo: bool = False,
        provider: str = "",
    ) -> str:
        now = time.time()
        launch_id = "l" + secrets.token_hex(6)
        with self._launch_lock:
            for lid in [k for k, v in self.launches.items()
                        if now - v["created_at"] > LAUNCH_TTL_S]:
                del self.launches[lid]
            self.launches[launch_id] = {
                "launch_id": launch_id, "machine": machine,
                "harness": harness, "cwd": cwd, "state": "pending",
                "model": model, "effort": effort, "yolo": bool(yolo),
                "provider": provider, "key": "", "note": "", "created_at": now,
            }
        return launch_id

    def drop_launch(self, launch_id: str) -> None:
        """Forget a launch that turned out to be another one's retry."""
        with self._launch_lock:
            self.launches.pop(launch_id, None)

    def settle_launch(self, launch_id: str, **fields) -> bool:
        """Update a pending launch; False when it is gone or settled.
        Without ``state`` it only notes progress (``note``, ``cwd``)."""
        with self._launch_lock:
            entry = self.launches.get(launch_id)
            # A launch settles once: a late failure note for one that
            # already started must not flip it back.
            if entry is not None and entry["state"] == "pending":
                entry.update(fields)
                return True
            return False

    def _settle_untracked(self, launch_id: str) -> None:
        """A satellite started a harness swe can't read sessions of: no
        session will ever name this launch, so say so now."""
        with self._launch_lock:
            entry = dict(self.launches.get(launch_id) or {})
        row = catalog_mod.load_machines(self.home).get(entry.get("machine"))
        if entry and entry["harness"] in (row or {}).get(
                "untracked_harnesses", []):
            self.settle_launch(launch_id, state="untracked", note=(
                f"{entry['harness']} is running under tmux, but swe can't "
                f"read its sessions, so there's no chat to open"
            ))

    def launch_view(self, launch_id: str) -> dict | None:
        """``GET /v1/launches/<id>``: started only once the catalog has
        the row, so the phone can open the chat straight away."""
        with self._launch_lock:
            entry = self.launches.get(launch_id)
            view = dict(entry) if entry else None
        if view is None:
            # A keeper launch taken by a hub that has restarted since.
            view = self.runtime.launch_view(launch_id)
        if view and view["state"] == "started" \
                and self.catalog.row_for(view["key"]) is None:
            view["state"] = "pending"
        return view

    def _whois_hit(self, ip: str) -> tuple[bool, dict | None]:
        with self._whois_lock:
            hit = self._whois_cache.get(ip)
            if hit is not None and hit[0] > time.time():
                return True, hit[1]
        return False, None

    def whois_cached(self, ip: str) -> dict | None:
        """``whois(ip)``, remembered for _WHOIS_TTL_S (a miss for
        _WHOIS_MISS_TTL_S). Concurrent callers for one IP share a single
        lookup; different IPs look up in parallel."""
        found, peer = self._whois_hit(ip)
        if found:
            return peer
        with self._whois_lock:
            if len(self._whois_ip_locks) > 256:
                self._whois_ip_locks.clear()
            ip_lock = self._whois_ip_locks.setdefault(ip, threading.Lock())
        with ip_lock:
            # Whoever held the lock may have just filled the cache.
            found, peer = self._whois_hit(ip)
            if found:
                return peer
            peer = self.whois(ip)
            ttl = _WHOIS_TTL_S if peer is not None else _WHOIS_MISS_TTL_S
            with self._whois_lock:
                if len(self._whois_cache) > 256:
                    self._whois_cache.clear()
                self._whois_cache[ip] = (time.time() + ttl, peer)
        return peer

    def owner(self) -> str:
        """The hub owner's tailnet login, retrying a lookup that failed
        at start-up (behind serve, before Tailscale was up)."""
        if not self.owner_login and self.owner_retry is not None:
            now = time.time()
            if now - self._owner_tried_at >= _OWNER_RETRY_S:
                self._owner_tried_at = now
                if self.owner_retry():
                    self.owner_retry = None
        return self.owner_login

    def _on_probed_status(self, key: str, status: str, at: float) -> None:
        """A tail probe read ``key``'s status: patch the catalog row, and
        let a pending ``when_idle`` detach act once the turn is over."""
        self.catalog.note_status(key, status, at)
        if status != "active":
            self.detaches.nudge(key)

    def _journal_has(self, key: str) -> bool:
        return self.journal.last_seq(key) > 0

    def _note_event(self, key: str, event: dict):
        """The bookkeeping every v1 event gets, whoever numbered it:
        open approvals, launches, satellite acks. Returns ``(launch id,
        session key)`` when it settled a launch as started."""
        etype = event.get("type")
        adopted = None
        if etype == "approval.requested":
            rid = str((event.get("data") or {}).get("request_id") or "")
            if rid:
                with self._approval_lock:
                    self._approval_at.setdefault(key, {})[rid] = time.time()
                    self._sync_pending_approval(key)
        elif etype == "approval.resolved":
            # Retire only the resolved request — a different still-
            # pending request on the session (a Codex app-server holds
            # approvals independently) stays in the inbox and keeps its
            # buttons live. A resolve naming no request clears them all.
            rid = str((event.get("data") or {}).get("request_id") or "")
            with self._approval_lock:
                if rid:
                    (self._approval_at.get(key) or {}).pop(rid, None)
                else:
                    self._approval_at.pop(key, None)
                self._sync_pending_approval(key)
            self.approval_tokens.drop(key, rid)
        elif etype in {"session.launched", "command.result"}:
            data = event.get("data") or {}
            if etype == "command.result":
                # A result is the satellite's in-band ack: between
                # reconnects this is what moves the backlog floor.
                self._sat_result_acked(
                    str(event.get("machine") or ""),
                    str(data.get("command_id") or ""))
            lid = str(data.get("launch_id") or "")
            if lid and etype == "session.launched":
                started = str(event.get("session") or key)
                if self.settle_launch(lid, state="started", key=started):
                    adopted = (lid, started)
            elif lid and data.get("op") in {"launch", "fork"} \
                    and not data.get("ok"):
                self.settle_launch(
                    lid, state="failed", note=str(data.get("note") or "")
                )
            elif lid and data.get("op") == "launch":
                note = str(data.get("note") or "")
                if supervise.ACP_FALLBACK_NOTE in note:
                    # The launch poll says why ACP's live controls
                    # aren't there.
                    self.settle_launch(lid, note=note)
                self._settle_untracked(lid)
        return adopted

    # The one funnel: every event is journaled, then fanned out.
    def emit(self, key: str, event: dict) -> dict:
        adopted = self._note_event(key, event)
        written = None
        if key == GLOBAL_KEY:
            written = self.journal.append(key, event)
        else:
            # Check and append as one step: a keeper taking the session
            # over reads the journal's last seq under this same lock.
            with self.journal.hold(key):
                if not (self.runtime.owns(key) or agent_mod.mirrored(self, key)):
                    written = self.journal.append(key, event)
                    # Under the journal's lock, so v2 streams are
                    # queued events in seq order (it never blocks).
                    self.sync.on_v1(key, written)
        if written is None:
            # A keeper numbers this session's journal: an event of the
            # hub's own (ownership, a notify, a title) would take a seq
            # the keeper's next event needs. Open streams get it with
            # no seq; the global journal still records it.
            written = self._publish_unnumbered(key, event)
        else:
            self.bus.publish(key, written)
        if key != GLOBAL_KEY and "seq" in written:
            # The global journal mirrors everything so /v1/events replays
            # a whole-machine timeline. Global subscribers get the
            # *global*-numbered copy so their ``?last=`` cursor lines up
            # with replay.
            mirrored = self.journal.append(GLOBAL_KEY, written)
            self.bus.publish(GLOBAL_KEY, mirrored)
        if adopted:
            # An issue resolve or a PR open names its session after the
            # issue or pull request, and links it to the task its launch
            # was for.
            issues_mod.title_adopted(self, *adopted)
            tasks_mod.origin_adopted(self, *adopted)
        return written

    def _publish_unnumbered(self, key: str, event: dict) -> dict:
        """Fan a session event out with no journal seq of its own: to
        the session's open streams as it is, and into the global
        journal (which numbers its copy)."""
        ev = dict(event)
        ev.pop("seq", None)
        self.bus.publish(key, ev)
        self.sync.on_v1(key, ev)
        mirrored = self.journal.append(GLOBAL_KEY, ev)
        self.bus.publish(GLOBAL_KEY, mirrored)
        return ev

    def publish_kept(self, key: str, event: dict, *, seq: bool = True
                     ) -> None:
        """A v1 event for a keeper-held session, already in the
        session's journal under the keeper's seq (``seq`` False: an
        event the keeper's log does not have, sent with no seq). Runs
        the same bookkeeping as ``emit`` and mirrors it to the global
        stream."""
        self._note_event(key, event)
        if not seq:
            self._publish_unnumbered(key, event)
            return
        self.bus.publish(key, event)
        mirrored = self.journal.append(GLOBAL_KEY, event)
        self.bus.publish(GLOBAL_KEY, mirrored)

    def restore_approvals(self, key: str, open_: dict[str, float]) -> None:
        """Set ``key``'s open approvals to ``open_`` (request id ->
        asked at): what a keeper says is still open, after a restart
        that forgot the events which announced them."""
        with self._approval_lock:
            if open_:
                self._approval_at[key] = dict(open_)
            else:
                self._approval_at.pop(key, None)
            self._sync_pending_approval(key)

    def session_released(self, key: str) -> None:
        """A keeper let ``key`` go: its row is a terminal session's
        again, and clients hear the ownership change."""
        row = self.catalog.row_for(key)
        if row is not None:
            for field in ("mode", "state", "caps", "adapter", "control"):
                row.pop(field, None)
            row["ownership"] = protocol.OWNERSHIP_OBSERVED
            reason = self.runtime.read_only_reason(key)
            if reason:
                row["control"] = "read_only"
                row["control_reason"] = reason
        self.emit(key, _ownership_event(
            key, self.machine_id, protocol.OWNERSHIP_OBSERVED, None, None,
            False))
        self._catalog_wake.set()

    def publish_transient(self, key: str, event: dict) -> None:
        """Fan ``event`` out to ``key``'s live subscribers only.

        For ``message.delta``: never journaled (the final
        ``message.appended`` is the record), so it carries no ``seq``
        and a reconnect never replays it. The global stream doesn't get
        it either; only an open chat has a use for it.
        """
        ev = dict(event)
        ev.pop("seq", None)
        self.bus.publish(key, ev)
        if not self.runtime.owns(key):
            # A keeper-held session's deltas reach v2 streams as the v2
            # events they are (``runtime._on_event``).
            self.sync.on_v1_delta(key, ev)

    def expire_pending_approvals(self) -> None:
        """Sweep ``pending_approvals`` entries past ``APPROVAL_TTL_S``.

        ``approval.resolved`` clears a live one; a dead satellite or a
        pane killed mid-prompt never sends it, so without this the
        inbox shows the prompt forever. Runs lazily where the inbox is
        read — it is the only consumer. The whole sweep sits under
        ``_approval_lock`` so a fresh request landing mid-pass is
        either expired by this check or survives it, never deleted by
        a stale timestamp.
        """
        with self._approval_lock:
            cutoff = time.time() - APPROVAL_TTL_S
            for key, open_ in list(self._approval_at.items()):
                for rid, at in list(open_.items()):
                    if at <= cutoff:
                        del open_[rid]
                self._sync_pending_approval(key)

    def _sync_pending_approval(self, key: str) -> None:
        """Point ``pending_approvals[key]`` at the session's newest open
        request, or drop both entries when none is left. Caller holds
        ``_approval_lock``."""
        open_ = self._approval_at.get(key)
        if open_:
            self.pending_approvals[key] = max(open_, key=open_.__getitem__)
        else:
            self._approval_at.pop(key, None)
            self.pending_approvals.pop(key, None)

    # -- GitHub notifications ---------------------------------------------
    # The catalog polls ``fetch_github`` on its refresh cadence and feeds
    # ``/v1/inbox``; serve() wires it so a bare HubState — tests, the
    # satellite agent — never reaches for ``gh``.

    def _fetch_github_rows(self) -> list[dict] | None:
        """Unread GitHub notifications as inbox rows through the web
        app's shared ``gh`` cache; ``None`` when the read failed."""
        from swe.webapp.pages import inbox as gh_inbox
        return gh_inbox.unread_rows(self.home)

    def _github_alert(self, row: dict) -> None:
        """A notification thread just turned unread. Only the reasons a
        phone should ring for become ``notify`` events; comments on
        subscribed threads stay inbox-only. The event lands on the
        global journal like every session's mirrored notify."""
        if str(row.get("reason") or "") not in ghcli.PUSH_REASONS:
            return
        self.emit(GLOBAL_KEY, protocol.event(
            "notify", "", self.machine_id,
            {
                "kind": "github",
                "reason": str(row.get("reason") or ""),
                "repo": str(row.get("repo") or ""),
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "thread": str(row.get("gh_thread_id") or ""),
            },
        ))

    # -- Service status pushes -------------------------------------------
    # With ``service_status`` on in notify.json and ntfy ready, the hub
    # reads the status pages every five minutes through the web app's
    # shared poller and turns a provider starting or ending an incident
    # into a ``notify`` event of kind ``service`` (the Notifier posts it).

    def _service_status_loop(self) -> None:
        from swe.status import alerts as status_alerts
        from swe.status.poller import poller_for
        poller = poller_for(self.home)
        tracker = status_alerts.Tracker(self.home)
        while not self._stop.is_set():
            try:
                self._service_status_tick(poller, tracker)
            except Exception:
                self._log_loop_error("service status check")
            if self._stop.wait(status_alerts.INTERVAL):
                return

    def _service_status_tick(self, poller, tracker) -> None:
        """One check: every tracked page (built-ins plus the web app's
        additions), the shared rule, one event per alert."""
        from swe.status import tracked
        from swe.status.catalog import SERVICES
        statuses = poller.statuses(list(SERVICES) + tracked.load(self.home))
        for alert in tracker.check(statuses):
            self.emit(GLOBAL_KEY, protocol.event(
                "notify", "", self.machine_id,
                {
                    "kind": "service",
                    "change": alert.kind,
                    "service": alert.service_id,
                    "name": alert.service_name,
                    "health": alert.health.key,
                    "incident": alert.incident,
                    "title": alert.title,
                    "message": alert.body,
                    "url": alert.page_url,
                },
            ))

    def start(self) -> None:
        self._register_machine()
        # A keep or adopt cut short by a crash leaves a key in both
        # the supervised and kept registries; settle it before serving.
        supervise.reconcile_kept(self.home)
        notify_cfg = notify.load_config(self.home)
        # Copies of the legacy ntfy action credential already sent to a
        # topic still authenticate as a paired device, so retire it at
        # boot whether or not notifications still post — a disabled or
        # incomplete config must not strand the row live.
        notify.retire_action_credential(self.home, notify_cfg)
        targets: list = [
            (self._catalog_loop, "remote-catalog"),
            (self._tail_loop, "remote-tail"),
            (self._search_index_loop, "remote-search-index"),
        ]
        # Only with somewhere to push: a check with no ntfy would record
        # incidents as announced and owe their recoveries a push.
        if notify_cfg.get("service_status") is True and notify.ready(notify_cfg):
            targets.append((self._service_status_loop, "remote-service-status"))
        if notify.ready(notify_cfg):
            notifier = notify.Notifier(
                self, self.base_url, subscribe_key=GLOBAL_KEY
            )
            # Subscribe before the producers start — an approval prompt
            # already visible at boot would otherwise be emitted into a
            # bus with no listener and never reach the phone.
            notifier.subscribe()
            targets.append((notifier.run, "remote-notify"))
        for target, name in targets:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._catalog_wake.set()
        self.tail_manager.interrupt()
        # Fail every accepted-but-not-yet-started send honestly; the
        # in-flight one sees _stop and reports the same note.
        self.waiting.wake_all()
        self.sendq.close()
        self.detaches.close()
        # Open v2 streams end at their next step.
        self.sync.close()
        for t in self._threads:
            t.join(timeout=2)
        # The keepers keep running: the next hub reconnects to them.
        self.runtime.stop()
        self.webapp.close()

    def _register_machine(self) -> None:
        catalog_mod.upsert_machine(
            self.home, {**self.machine, "online": True}
        )
        # Presence is this process's live streams — a restart opens with
        # zero satellite connections, so every persisted ``online`` flag
        # for another machine is stale until that machine dials back in.
        for mid in catalog_mod.load_machines(self.home):
            if mid != self.machine_id:
                catalog_mod.upsert_machine(
                    self.home, {"id": mid, "online": False,
                                "last_seen_at": time.time()}
                )

    # -- satellites ---------------------------------------------------------

    @staticmethod
    def sat_journal_key(machine_id: str) -> str:
        return f"{_SAT_PREFIX}{machine_id}"

    def _own_journal_keys(self) -> list[str]:
        """Journals the sweep must leave: the global stream, each paired
        satellite's command queue (its agent's cursor counts on that
        queue's seqs), and every satellite session still listed. Those
        have no tailer or cursor here, and the agent that tails them
        doesn't know to backfill a journal the hub dropped."""
        keys = [GLOBAL_KEY] + [
            self.sat_journal_key(mid)
            for mid in catalog_mod.load_machines(self.home)
        ]
        keys += [
            str(row.get("key") or "") for row in self.catalog.rows()
            if row.get("remote")
        ]
        # A keeper-held session has no tailer either, and its journal
        # is where today's clients read its timeline.
        keys += [row["key"] for row in self.runtime.rows()]
        return keys

    def machine_online(self, mid: str) -> bool:
        return self._sat_subs.get(mid, 0) > 0 or mid == self.machine_id

    def sat_connected(self, mid: str) -> None:
        with self._sat_subs_lock:
            self._sat_connected(mid)

    def _sat_connected(self, mid: str) -> None:
        first = self._sat_subs.get(mid, 0) == 0
        self._sat_subs[mid] = self._sat_subs.get(mid, 0) + 1
        if first:
            catalog_mod.upsert_machine(
                self.home, {"id": mid, "online": True,
                            "last_seen_at": time.time()}
            )
            self.emit(
                GLOBAL_KEY,
                protocol.event(
                    "machine.online", "", mid, {"machine_id": mid}
                ),
            )

    def sat_disconnected(self, mid: str) -> None:
        with self._sat_subs_lock:
            self._sat_disconnected(mid)

    def _sat_disconnected(self, mid: str) -> None:
        left = max(0, self._sat_subs.get(mid, 0) - 1)
        self._sat_subs[mid] = left
        if left:
            return
        catalog_mod.upsert_machine(
            self.home, {"id": mid, "online": False,
                        "last_seen_at": time.time()}
        )
        self.emit(
            GLOBAL_KEY,
            protocol.event(
                "machine.offline", "", mid, {"machine_id": mid}
            ),
        )

    def _prune_sat_cmd_seqs(self, mid: str) -> None:
        """Drop result-map entries at or under the ack floor — a result
        naming one could never move the floor again.

        Caller holds ``_cmd_lock``."""
        seqs = self._sat_cmd_seqs.get(mid)
        if not seqs:
            return
        floor = self._sat_acked.get(mid, 0)
        for cid in [c for c, s in seqs.items() if s <= floor]:
            del seqs[cid]

    def _sat_result_acked(self, mid: str, command_id: str) -> None:
        """Move a satellite's backlog floor on a ``command.result``.

        A result acks only its own seq: the floor mirrors the agent's
        contiguous done floor, so it climbs ``_sat_done`` one
        consecutive seq at a time — a control finishing while an
        earlier message still runs on the agent's send queue leaves it
        put, and
        the pending count keeps covering the commands a restart still
        owes the satellite. ``?last=`` resets it on connect; an id the
        map doesn't know (a hub-local result, or a command queued
        before a hub restart) changes nothing.
        """
        if not command_id:
            return
        with self._cmd_lock:
            seqs = self._sat_cmd_seqs.get(mid)
            seq = seqs.pop(command_id, None) if seqs else None
            if seq is None:
                return
            floor = self._sat_acked.get(mid, 0)
            if seq > floor:
                done = self._sat_done.setdefault(mid, set())
                done.add(seq)
                while floor + 1 in done:
                    floor += 1
                    done.discard(floor)
                self._sat_acked[mid] = floor
            # A map entry at or under the floor can never move it.
            for cid in [c for c, s in seqs.items() if s <= floor]:
                del seqs[cid]

    def queue_tail(self, mid: str, session_key: str, status: str) -> None:
        """Ask a satellite to tail ``session_key``, unless a tail for it
        is already queued and not yet acked."""
        with self._sat_tail_lock:
            with self._cmd_lock:
                cid = self._sat_tail_cmds.get((mid, session_key))
                if cid and cid in self._sat_cmd_seqs.get(mid, {}):
                    return
            cmd = self.queue_command(
                mid, "tail", session_key, {"status": status})
            if cmd is None:
                return
            if len(self._sat_tail_cmds) > 4096:
                self._sat_tail_cmds.clear()
            self._sat_tail_cmds[(mid, session_key)] = str(
                (cmd.get("data") or {}).get("command_id") or "")

    def queue_command(
        self, mid: str, op: str, session_key: str, data: dict
    ) -> dict | None:
        """Append a command to the satellite's journal; returns the envelope.

        The satellite replays ``_sat_<mid>`` on reconnect, so this IS the
        offline queue — nothing else to build.

        Returns ``None`` when the machine's unacked backlog exceeds
        ``MAX_SAT_PENDING``: the journal trims and silently dropped
        commands are worse than an honest "machine too far behind".
        """
        with self._cmd_lock:
            pending = self.journal.last_seq(
                self.sat_journal_key(mid)
            ) - self._sat_acked.get(mid, 0)
        if pending >= MAX_SAT_PENDING:
            if session_key:
                self.emit(
                    session_key,
                    protocol.event(
                        "session.error", session_key, self.machine_id,
                        {"message": f"{mid} is {pending} commands behind; "
                                    f"not queueing more"},
                    ),
                )
            return None
        # Unique for ever: a counter starts over with the hub, and a
        # second ``c1`` would read as a retry of the first wherever
        # command ids are remembered (the store, a satellite's cursor).
        command_id = runtime_mod.new_command_id("c")
        local_key, _mid = protocol.split_remote_key(session_key)
        cmd = protocol.command(op, local_key, mid, command_id, data)
        written = self.journal.append(self.sat_journal_key(mid), cmd)
        seq = written.get("seq")
        if isinstance(seq, int):
            with self._cmd_lock:
                self._sat_cmd_seqs.setdefault(mid, {})[command_id] = seq
        self.bus.publish(self.sat_journal_key(mid), written)
        if session_key:
            self.emit(
                session_key,
                protocol.event(
                    "command.queued", session_key, self.machine_id,
                    {"command_id": command_id, "op": op},
                ),
            )
        return written

    def _log_loop_error(self, step: str) -> None:
        """From an ``except`` in a background loop: print the traceback
        to stderr (the launchd/systemd log), at most once per
        _LOOP_ERROR_LOG_S for each step. The loop carries on."""
        now = time.monotonic()
        last = self._loop_error_at.get(step)
        if last is not None and now - last < _LOOP_ERROR_LOG_S:
            return
        self._loop_error_at[step] = now
        print(f"swe remote hub: {step} failed (kept running):",
              file=sys.stderr)
        traceback.print_exc()

    def _prune_pair_requests(self) -> None:
        now = time.time()
        if now - self._pair_pruned_at < _PAIR_PRUNE_S:
            return
        self._pair_pruned_at = now
        try:
            pairing.prune_requests(self.home)
        except Exception:
            self._log_loop_error("pair request prune")

    # -- low power (#1520) --------------------------------------------------

    def watched(self) -> bool:
        """Whether anyone could see what the hub reads: an open stream
        (a client's, or the ntfy notifier's) or a client request within
        the last CLIENT_IDLE_S. Satellite traffic doesn't count: it
        watches the satellite's sessions, not this machine's."""
        if self.bus.watched() or self.sync.watching():
            return True
        return time.monotonic() - self._client_seen_at < CLIENT_IDLE_S

    def note_client(self) -> None:
        """A client request arrived: leave low power now, so the tailers
        poll and the catalog refreshes at once instead of in a minute."""
        idle = not self.watched()
        self._client_seen_at = time.monotonic()
        if idle:
            self.tail_manager.set_low_power(False)
            self._catalog_wake.set()

    def fresh_catalog(self, timeout: float = CATALOG_CATCHUP_S,
                      github: bool = False) -> None:
        """Wait up to ``timeout`` for a catalog refresh that started
        within CATALOG_STALE_S, when the current one is older.

        ``github`` (the inbox) also waits for the GitHub poll that runs
        after the refresh, within the same timeout.

        Only the first list request after low power waits: the live
        cadence keeps the catalog newer than that. A hub whose catalog
        loop isn't running (tests, a stopped hub) never waits.
        """
        if not self._catalog_running:
            return
        floor = time.time() - CATALOG_STALE_S

        def fresh() -> bool:
            if not self._catalog_running:
                return True
            return self.catalog.refreshed_at >= floor and (
                not github or self._github_at >= floor)

        with self._catalog_done:
            if fresh():
                return
            self._catalog_wake.set()
            self._catalog_done.wait_for(fresh, timeout)

    def _catalog_refreshed(self) -> None:
        with self._catalog_done:
            self._catalog_done.notify_all()

    def _catalog_loop(self) -> None:
        self._catalog_running = True
        try:
            self._catalog_cycle()
        finally:
            self._catalog_running = False
            self._catalog_refreshed()

    def _catalog_cycle(self) -> None:
        while not self._stop.is_set():
            try:
                prev_own = {
                    row["key"]: row.get("ownership")
                    for row in self.catalog.rows()
                }
                self.catalog.refresh()
                self._catalog_refreshed()
                self._adopt_launches()
                self._refresh_launchable()
                # Ownership can change outside HTTP — `swe remote
                # attach`, pending adoption, dead-tmux pruning — and the
                # phone needs the event either way.
                for row in self.catalog.rows():
                    new_own = row.get("ownership")
                    if prev_own.get(row["key"]) not in (None, new_own):
                        if new_own == protocol.OWNERSHIP_ATTACHED:
                            # Attached outside HTTP (`swe remote attach`,
                            # a kept tmux session taken back): same hot
                            # window as an attach through the hub.
                            self.tail_manager.wake(row["key"])
                        self.emit(row["key"], _ownership_event(
                            row["key"], self.machine_id, new_own,
                            row.get("tmux"), row.get("mode"),
                            row.get("kept")))
                rows, as_of = self.catalog.tail_snapshot()
                self.tail_manager.sync(rows, as_of=as_of)
                # A launched ACP session's first-turn approvals waited
                # for its tailer; it has one now.
                inject.AcpInjector.release_held(
                    self.tail_manager.tailers, self.emit,
                    self.machine_id, self.deltas)
                supervise.reap_kept(self.home, self.catalog.session_for)
                self.runtime.tick()
                self._prune_pair_requests()
                # GitHub last: a slow ``gh`` must never hold up adoption
                # or tail sync, which don't need it.
                github_started = time.time()
                self.catalog.poll_github()
                self._github_at = github_started
                self._catalog_refreshed()
            except Exception:
                self._log_loop_error("catalog refresh")
            self._catalog_wake.wait(
                catalog_mod.REFRESH_INTERVAL_S if self.watched()
                else CATALOG_IDLE_S)
            self._catalog_wake.clear()

    def _search_index_loop(self) -> None:
        """Keep this machine's transcript search index current while
        ``swe session index --on`` says so, and its transcript mirror
        while ``swe session mirror --off`` has not said otherwise. Both
        settings are read every cycle, so changing one needs no
        restart."""
        from swe.sessions import search_writer
        from swe.sessions import transcript_mirror

        while not self._stop.is_set():
            if search_writer.is_enabled(self.home):
                try:
                    # Mirrors the transcripts first, from the same walk.
                    search_writer.run_pass(self.home, should_stop=self._stop.is_set)
                except search_writer.IndexBusy:
                    pass  # a `swe session index` run has it; next cycle
                except Exception:
                    # A newer schema or a locked file: run_pass saved it
                    # as last_error for `swe session index --status` and
                    # the Settings row, and the next cycle tries again.
                    pass
            elif transcript_mirror.is_enabled(self.home):
                # Search indexing is off, and the mirror is on by default:
                # it must not depend on the index to keep what the
                # harnesses are about to prune.
                try:
                    transcript_mirror.run_pass(
                        self.home, should_stop=self._stop.is_set, wait=False)
                except Exception:
                    pass  # busy, or a disk problem; the next cycle tries again
            self._stop.wait(search_writer.INDEX_INTERVAL_S)

    def _refresh_launchable(self) -> None:
        """Keep this machine's ``launchable`` current: Provider visibility
        and the registry can change while the hub runs."""
        launchable = catalog_mod.launchable_harnesses()
        fields = catalog_mod.launch_fields(launchable, self.home)
        if all(self.machine.get(k) == v for k, v in fields.items()):
            return
        self.machine.update(fields)
        catalog_mod.upsert_machine(self.home, {"id": self.machine_id, **fields})

    def _adopt_launches(self) -> None:
        adopted = supervise.adopt_pending(
            self.home, self.catalog.tail_rows()
        )
        if adopted:
            # Newly adopted keys need a second refresh pass so the
            # rows carry ownership: attached.
            self.catalog.refresh()
        for key in adopted:
            # A launch or fork starts with its first turn: its tailer
            # (created on the next sync) starts hot and status-probed.
            self.tail_manager.wake(key)
            lid = (supervise.meta_for(self.home, key) or {}).get("launch_id")
            if lid:
                # Global stream only: anything in the session's own
                # journal before its tailer starts reads as history
                # already sent, and the tailer would skip the first turn.
                self.emit(GLOBAL_KEY, protocol.event(
                    "session.launched", key, self.machine_id,
                    {"launch_id": lid},
                ))

    def _tail_loop(self) -> None:
        swept_at = time.monotonic()
        while not self._stop.is_set():
            low = not self.watched()
            self.tail_manager.set_low_power(low)
            try:
                self.tail_manager.tick()
            except Exception:
                self._log_loop_error("tail tick")
            if time.monotonic() - swept_at >= SWEEP_S:
                swept_at = time.monotonic()
                try:
                    attachments.sweep(self.home)
                except Exception:
                    self._log_loop_error("attachment sweep")
                try:
                    self.tail_manager.sweep_stale(
                        self.journal, keep=self._own_journal_keys())
                except Exception:
                    self._log_loop_error("journal sweep")
            # A write to a session (tail_manager.wake) or a client
            # arriving (note_client) cuts this short. A hot session
            # (written to outside HTTP, say an attach from the CLI) keeps
            # the fast tick even in low power.
            slow = low and not self.tail_manager.any_hot()
            self.tail_manager.wait(TAIL_TICK_LOW_S if slow else TAIL_TICK_S)


_UNSET = object()


def _no_constant(name: str):
    """``json.loads``'s hook for ``NaN``/``Infinity``: a request body
    holding one is unreadable. Stored and served back, a non-finite
    number breaks every client's ``JSON.parse`` and ``JSONDecoder``."""
    raise ValueError(f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    """``json.loads``'s float hook: ``1e400`` parses to ``inf``, which
    would serve back as ``Infinity``, so it is refused like ``NaN``."""
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{text} is out of range")
    return value


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "swe-remote"
    protocol_version = "HTTP/1.1"
    state: HubState  # stamped on the server, set in make_server

    def setup(self) -> None:
        super().setup()
        # Without a deadline a tailnet peer can park threads by never
        # finishing a request. Reads only happen while parsing headers
        # and body — SSE never reads again after dispatch.
        self.connection.settimeout(_REQ_TIMEOUT_S)

    # -- plumbing ---------------------------------------------------------

    @property
    def _state(self) -> HubState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # quiet: hub runs as a daemon
        return

    def _send_json(self, status: int, payload) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, protocol.error(code, message))

    def _refuse_unless_kept(self, body: dict) -> bool:
        """Answer 409 ``not_kept`` for a message whose body says
        ``require_keeper: true`` that reached a route other than the
        keeper's (G31): the client may be repeating a message the
        keeper may already have, and only the keeper path
        de-duplicates. Called by the legacy routes (``_send_message``
        and the satellite forward) before they queue anything, so
        nothing is stored or queued. True when it refused."""
        if body.get("require_keeper") is not True:
            return False
        self._send_error(409, "not_kept", (
            "this session is not run by a session keeper; the message "
            "was not sent"))
        return True

    def _command_lookup(self, key: str, command_id: str) -> None:
        """``GET /v1/sessions/{key}/commands/{id}``: one stored command,
        its receipt (the stored status) and result, from the commands
        table (kept 7 days after it finished). Reads only: nothing is
        written, published or delivered. 404 ``unknown_command`` when
        the store does not hold the id for this session."""
        store = self._state.runtime.store
        cmd = None
        if store is not None and store_mod._COMMAND_ID_RE.fullmatch(
                command_id):
            try:
                cmd = store.get_command(command_id)
            except store_mod.StoreError as exc:
                self._send_error(503, "store_failed", str(exc))
                return
        if cmd is None or cmd.session != key:
            self._send_error(404, "unknown_command",
                             "this session has no command with that id")
            return
        body = {"ok": True, "session": key, "command_id": cmd.command_id,
                "op": cmd.op, "receipt": cmd.status,
                "received_at": cmd.received_at,
                "updated_at": cmd.updated_at}
        if cmd.reason:
            body["reason"] = cmd.reason
        if cmd.result is not None:
            body["result"] = cmd.result
        self._send_json(200, body)

    def _refuse_unless_attached(self, row: dict) -> bool:
        """Answer 409 not_attached for a session the hub only observes.

        Returns True when it refused, so the caller returns at once.
        """
        if row.get("ownership") == protocol.OWNERSHIP_ATTACHED:
            return False
        self._send_error(
            409, "not_attached",
            "session is observed: read-only until attached",
        )
        return True

    # -- request body bookkeeping -------------------------------------
    #
    # ``_unread`` is how many body bytes of the current request are still
    # on the socket: the declared Content-Length until something reads
    # it, then 0. None means the body can't be framed (Transfer-Encoding,
    # which the hub doesn't decode, or a malformed Content-Length), so
    # the only safe end for the connection is a close. Every response
    # goes through ``send_response``, which settles what is left before
    # the status line, so no early-exit path can leave bytes behind.

    _unread: int | None = 0
    _must_close = False
    _body_cache: object = _UNSET
    _device_row: dict | None = None
    # Whether this connection's loopback peer is the Tailscale daemon;
    # None until a forwarded header first needs the answer.
    _peer_is_proxy: bool | None = None

    def handle_one_request(self) -> None:
        self._unread = 0
        self._must_close = False
        self._body_cache = _UNSET
        self._device_row = None
        self._sent_headers = None
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            # The client went away; nothing to answer and nothing to log.
            self.close_connection = True
        except Exception:
            # A route bug. Answer 500 if nothing went out yet, so the
            # client sees an error instead of a dropped connection.
            traceback.print_exc()
            self.close_connection = True
            if self._sent_headers is None:
                try:
                    self._send_error(500, "internal", "the hub hit an error")
                except OSError:
                    pass

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        self._unread = self._declared_length()
        return True

    def _declared_length(self) -> int | None:
        if self.headers.get("Transfer-Encoding") is not None:
            return None
        values = {v.strip() for v in
                  self.headers.get_all("Content-Length") or []}
        if not values:
            return 0  # a request with neither header has no body
        if len(values) > 1:
            return None
        raw = values.pop()
        # int() would also take "+5", "1_000" or non-ASCII digits.
        if not (raw.isascii() and raw.isdigit()):
            return None
        try:
            return int(raw)
        except ValueError:  # past sys.get_int_max_str_digits()
            return None

    def _take_body(self, limit: int) -> bytes | None:
        """The request body when it is at most ``limit`` bytes, else None
        (left unread for ``send_response`` to settle)."""
        left = self._unread
        if left is None or left > limit:
            return None
        self._unread = 0
        return self.rfile.read(left) if left else b""

    def _settle_body(self) -> None:
        left = self._unread
        self._unread = 0
        if left == 0:
            return
        if left is None or left > _DRAIN_MAX or not self._drain(left):
            self._must_close = True

    def _drain(self, left: int) -> bool:
        """Read and drop ``left`` body bytes within ``_DRAIN_S``. False
        when the peer is too slow or goes away first."""
        deadline = time.monotonic() + _DRAIN_S
        try:
            while left > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.connection.settimeout(remaining)
                # read1: at most one recv per call, so the deadline holds
                # against a peer trickling a byte at a time.
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    return False
                left -= len(chunk)
            return True
        except OSError:  # TimeoutError included
            return False
        finally:
            try:
                self.connection.settimeout(_REQ_TIMEOUT_S)
            except OSError:
                pass

    def version_string(self) -> str:
        # The default appends the Python version, which tells any peer
        # what to try before it has authenticated.
        return self.server_version

    def send_response(self, code, message=None) -> None:
        # 100 Continue goes out through send_response_only, so a client
        # waiting for it is never drained here.
        self._settle_body()
        self._sent_headers: set[str] = set()
        super().send_response(code, message)
        if self._must_close:
            self.send_header("Connection", "close")

    def send_header(self, keyword, value) -> None:
        sent = getattr(self, "_sent_headers", None)
        if sent is not None:
            sent.add(keyword.lower())
        super().send_header(keyword, value)

    def end_headers(self) -> None:
        # A handler's own "Connection: keep-alive" must not reopen a
        # connection whose request body was left on the socket.
        if self._must_close:
            self.close_connection = True
        # The web app keeps its bearer in this origin's storage and the
        # owner's own devices get in on their tailnet identity alone, so
        # no other site may frame a hub page, sniff a body into a script
        # or learn the hub's URL from a referrer. A route that sets its
        # own policy (page previews) keeps it.
        sent = getattr(self, "_sent_headers", None) or set()
        for name, value in _SECURITY_HEADERS:
            if name.lower() not in sent:
                self.send_header(name, value)
        super().end_headers()

    def _read_body(self) -> dict | None:
        # A second call returns the first answer: the bytes are gone.
        if self._body_cache is not _UNSET:
            return self._body_cache  # type: ignore[return-value]
        try:
            raw = self._take_body(_MAX_BODY)
            if raw is None:
                return None
            if not raw:
                data: object = {}
            else:
                data = json.loads(raw.decode("utf-8", errors="replace"),
                                  parse_constant=_no_constant,
                                  parse_float=_finite_float)
        except (ValueError, UnicodeError):
            data = None
        result = data if isinstance(data, dict) else None
        self._body_cache = result
        return result

    def _device(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        cred = auth[7:].strip() if auth.startswith("Bearer ") else ""
        hit = pairing.device_for_credential(self._state.home, cred)
        if hit is None:
            return None
        device_id, row = hit
        # The agent routes read role/machine_id off this row.
        self._device_row = row
        # Throttled last_seen update: a write per request would churn
        # devices.json; five-minute freshness is plenty for the list.
        seen = row.get("last_seen_at") or 0
        if time.time() - seen > 300:
            try:
                # Same write backfills kind/client/os on rows that
                # predate their capture — never overwrites pair-time
                # fields or a user rename.
                pairing.touch_device(
                    self._state.home, device_id,
                    meta=pairing.client_meta(
                        self.headers.get("User-Agent") or ""
                    ),
                )
            except Exception:
                pass
        return device_id

    @staticmethod
    def _device_revoked(home: Path | None, device_id: str) -> bool:
        """Whether the caller's access is gone: its paired row, or for
        the owner's ``tailnet:<node>`` identity, that node's sign-in.
        Open SSE streams ask this every few seconds."""
        if device_id.startswith(devices_mod.TAILNET_PREFIX):
            return pairing.signin_revoked(
                home, device_id[len(devices_mod.TAILNET_PREFIX):])
        row = pairing.peek_devices(home).get(device_id) or {}
        return bool(row.get("revoked_at"))

    def _approver_node(self, peer: dict | None) -> str | None:
        """The peer's node id when it shares the hub owner's login —
        else None. Tagged nodes never qualify; nor does this machine's
        own node (any local user's process reaches the hub as that node
        through serve's local listener, so it proves nothing), nor a node
        whose sign-in the owner revoked (a lost phone still on their
        login)."""
        owner = self._state.owner()
        if (
            peer is None or peer.get("tagged")
            or not owner
            or peer.get("login") != owner
        ):
            return None
        node = str(peer.get("node_id") or "")
        if not node or node == self._state.own_node_id:
            return None
        if pairing.signin_revoked(self._state.home, node):
            return None
        return node

    def _owner_device(self) -> str | None:
        """A device id for the hub owner's own tailnet device, which needs
        no pairing: tailnet login is the identity the Mac app's owner
        already has. Held to the approval path's gate (custom header,
        known Host, matching Origin), so a page on another site can't
        borrow the owner's network identity. Tagged nodes and other
        logins still pair."""
        if not self._identity_ok() or self._bad_origin():
            return None
        peer = self._state.whois_cached(self._client_ip())
        node = self._approver_node(peer)
        if not node:
            return None
        # Listed in Settings so the owner can revoke it if it goes
        # missing; throttled, and a failed write never refuses the caller.
        try:
            pairing.note_signin(self._state.home, node,
                                str((peer or {}).get("node_name") or ""))
        except Exception:
            pass
        return f"{devices_mod.TAILNET_PREFIX}{node}"

    def _client_device(self) -> str | None:
        """The paired device behind the bearer, unless it is a satellite.

        A satellite credential is a machine's, for /v1/agent/* only.
        Any pair token can mint one, so without this it would carry
        every phone power: launch agents, approve pair requests, read
        API keys through /v1/app."""
        device_id = self._device()
        if device_id is not None and (
                (self._device_row or {}).get("role") == "satellite"):
            return None
        return device_id

    def _caller(self, path: str) -> str | None:
        """The paired device, else the owner's own device. Satellite
        routes stay bearer-only: a satellite is a paired machine.

        A client's authenticated request ends low power
        (``HubState.note_client``); a satellite's does not."""
        if path.startswith("/v1/agent/"):
            return self._device()
        device_id = self._client_device() or self._owner_device()
        if device_id is not None:
            self._state.note_client()
        return device_id

    def _scoped_approval_device(self, path: str) -> str | None:
        """A minted ntfy action token as caller identity.

        Action buttons carry a scoped token, not a device credential:
        bound to ``(session key, request_id)``, single-use, minutes
        lived, and accepted only here. The token is burned on any
        presentation — a mismatched ``request_id`` is a failed spend,
        not a free probe — so a topic reader holding it can answer
        exactly the one prompt it was minted for and nothing else.

        The token spends only the decisions its notification showed as
        buttons, and never a standing grant — always allow, don't ask
        again, session-wide or bypass rows (#1464): those turn off later
        approvals, so they need a paired device. A live token presented
        with any other decision burns, gets a 403 and a log line, and
        this returns ``_NTFY_REFUSED`` so the caller sends nothing more.
        """
        prefix = "/v1/sessions/"
        suffix = "/approval"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        key = unquote(path[len(prefix):-len(suffix)])
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not token:
            return None
        body = self._read_body() or {}
        rid = str(body.get("request_id") or "")
        decision = body.get("decision")
        spent = self._state.approval_tokens.consume(
            token, key, rid, decision)
        if spent == notify.SPEND_INVALID:
            return None
        if spent != notify.SPEND_OK:
            print(
                f"swe remote hub: refused an ntfy action token on {key!r} "
                f"request {rid!r}: decision {str(decision)[:16]!r} was "
                "not offered or needs a paired device",
                file=sys.stderr, flush=True,
            )
            self._send_error(
                403, "decision_not_allowed",
                "this notification cannot send that choice; answer it "
                "from a paired device",
            )
            return _NTFY_REFUSED
        # The approval route never reads the device id back; non-None
        # is the whole grant.
        return "ntfy-action"
    def _agent_row(self) -> dict | None:
        """The caller's device row when it is a satellite, else None.

        Rows minted before roles exist have no ``role`` and read as
        phones here — an old credential can authenticate but never
        speaks for a machine.
        """
        row = self._device_row
        if not row or row.get("role") != "satellite":
            return None
        return row

    def _require_satellite(self) -> bool:
        """The agent routes' first gate: a satellite credential. Sends
        the 403 and returns False for anything else."""
        if self._agent_row() is None:
            self._send_error(
                403, "forbidden", "agent routes need a satellite credential"
            )
            return False
        return True

    def _require_machine(
        self, device_id: str, mid: str | None, *, bind: bool = False
    ) -> bool:
        """``_agent_machine_ok`` as a gate: sends the 403 and returns
        False when ``mid`` is missing or not this credential's machine."""
        if mid is None or not self._agent_machine_ok(
                device_id, mid, bind=bind):
            self._send_error(
                403, "forbidden", "credential is bound to another machine"
            )
            return False
        return True

    def _agent_machine_ok(
        self, device_id: str, mid: str, *, bind: bool = False
    ) -> bool:
        """True when ``mid`` is the machine this satellite speaks for.

        ``bind`` is set by ``/v1/agent/register``: the first register
        pins the credential's ``machine_id``, and every agent call —
        register included — must present that same id afterwards.
        """
        row = self._device_row or {}
        bound = row.get("machine_id")
        if bound:
            return bound == mid
        if not bind:
            return False
        bound = pairing.bind_machine(self._state.home, device_id, mid)
        if bound is not None:
            # The stashed row predates the write; re-read so later
            # checks in this request see the pin.
            self._device_row = (
                pairing.load_devices(self._state.home).get(device_id)
                or self._device_row
            )
        return bound == mid

    def _proxied(self) -> bool:
        """True when the request may have come through ``tailscale
        serve``: the hub runs behind it and the TCP peer is this
        machine. Enough for X-Forwarded-Proto, which only feeds the
        Origin check a local process can satisfy anyway; the client's
        identity needs ``_from_proxy``."""
        return (self._state.trust_proxy
                and self.client_address[0] in _LOOPBACK)

    def _from_proxy(self) -> bool:
        """True when the loopback peer is the Tailscale daemon, so its
        X-Forwarded-For can name the client. Asked once per connection:
        serve keeps its connections open across requests."""
        if not self._proxied():
            return False
        cached = self._peer_is_proxy
        if cached is None:
            try:
                server = self.connection.getsockname()[:2]
                cached = bool(self._state.proxy_peer(
                    (server[0], server[1]),
                    (self.client_address[0], self.client_address[1])))
            except Exception:  # noqa: BLE001 - doubt means not the proxy
                cached = False
            self._peer_is_proxy = cached
        return cached

    def _client_ip(self) -> str:
        """The requester's address: the tailnet client serve forwarded
        for, else the TCP peer. Serve replaces any X-Forwarded-For the
        client sent, so the last hop is the one it added. Only serve's
        own connection is believed: another local process sending the
        header is read as the loopback peer it is."""
        forwarded = self.headers.get("X-Forwarded-For") or ""
        hop = forwarded.split(",")[-1].strip()
        if hop and self._from_proxy():
            return hop
        return self.client_address[0]

    def _scheme(self) -> str:
        """``https`` when serve terminated TLS in front of us."""
        if self._proxied():
            proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
            if proto in {"http", "https"}:
                return proto
        return "http"

    def _bad_origin(self) -> bool:
        """Origin must name this hub when present — a cross-site page
        can't satisfy either form."""
        origin = self.headers.get("Origin")
        if origin is not None and (
            origin != f"{self._scheme()}://{self.headers.get('Host')}"
        ):
            return True
        return False

    def _identity_ok(self) -> bool:
        """The unauthenticated approval path's CSRF gate: a cross-site
        form can't send a custom header, and the Host must be a name
        this hub actually answers (DNS-rebinding check)."""
        if not self._pair_header():
            return False
        return self._local_host_ok()

    def _pair_header(self) -> bool:
        """The web app's own-page mark, ``X-Swe-Pair: 1``. ``X-Quiver-Pair``
        is its name from before the rename, still read so a browser tab
        holding an older page keeps working until it reloads."""
        return any((self.headers.get(name) or "") == "1"
                   for name in ("X-Swe-Pair", "X-Quiver-Pair"))

    def _pair_meta(self, body: dict | None, node_name: str = "") -> dict:
        """kind/model/client/os for the pairing device: the body's own
        ``device`` map wins over whatever the User-Agent lets us infer,
        and the tailnet node name goes in when it resolves."""
        meta = pairing.client_meta(self.headers.get("User-Agent") or "")
        sent = (body or {}).get("device")
        meta.update(pairing.clean_device_meta(sent))
        if node_name:
            meta["node_name"] = node_name
        return meta

    def _peer_node_name(self) -> str:
        """The caller's tailnet node name, best-effort: a miss costs the
        row its node_name, nothing more."""
        try:
            peer = self._state.whois_cached(self._client_ip())
        except Exception:
            return ""
        return str((peer or {}).get("node_name") or "")

    def _local_host_gate(self) -> bool:
        """``_local_host_ok`` as a gate: sends the 403 and returns False
        for a Host this hub doesn't answer to."""
        if self._local_host_ok():
            return True
        self._send_error(
            403, "bad_origin", "request blocked by origin policy"
        )
        return False

    def _local_host_ok(self) -> bool:
        """The DNS-rebinding gate on the unauthenticated routes: once
        ``allowed_hosts`` names the addresses this hub answers to, a
        request under any other Host came through a name that isn't
        this hub's — a page that rebound its domain to this address.
        Browsers can't forge Host, so the check is quiet for clients
        that dialed the hub's own address."""
        return _host_allowed(self._state, self.headers.get("Host") or "")

    def _attempt_ok(self, bucket: dict[str, list[float]]) -> bool:
        """The per-source attempt cap on the unauthenticated pair
        routes: ``_PAIR_ATTEMPT_MAX`` per ``_PAIR_ATTEMPT_S``. Locked:
        read-check-append must be atomic across handler threads."""
        ip = self._client_ip()
        now = time.time()
        with self._state._pair_req_lock:
            times = [t for t in bucket.get(ip, [])
                     if now - t < _PAIR_ATTEMPT_S]
            if len(times) >= _PAIR_ATTEMPT_MAX:
                return False
            times.append(now)
            bucket[ip] = times
        return True

    def _approver_ok(self) -> bool:
        """Auth for the pair-approval routes: a paired bearer, or the
        owner's tailnet identity. Sends the refusal and returns False.

        Every check that needs no subprocess runs first, so a caller
        that can't pass them never forks ``tailscale whois``; the
        identity path is rate-limited like the other unauthenticated
        pair routes, and its lookup goes through the whois cache."""
        if self._client_device() is not None:
            if self._bad_origin():
                self._send_error(
                    403, "bad_origin", "request blocked by origin policy"
                )
                return False
            return True
        if not self._pair_header():
            # Without the header the identity path can't pass at all.
            self._send_error(401, "unauthorized", "pair this device first")
            return False
        if self._bad_origin() or not self._identity_ok():
            self._send_error(
                403, "bad_origin", "request blocked by origin policy"
            )
            return False
        if not self._attempt_ok(self._state._pair_dec_times):
            self._send_error(429, "rate_limited", "too many pair attempts")
            return False
        peer = self._state.whois_cached(self._client_ip())
        if self._approver_node(peer) is None:
            self._send_error(401, "unauthorized", "pair this device first")
            return False
        return True

    def _pair_request(self) -> None:
        if not self._local_host_gate():
            return
        # Rate limit BEFORE the whois fork — a flood must not spawn a
        # subprocess per request.
        if not self._attempt_ok(self._state._pair_req_times):
            self._send_error(429, "rate_limited",
                             "too many pair requests")
            return
        ip = self._client_ip()
        peer = self._state.whois(ip)
        if peer is None:
            self._send_error(
                403, "not_tailnet",
                "can't identify this device on the tailnet; "
                "use a pair token",
            )
            return
        body = self._read_body() or {}
        try:
            result = pairing.request_pair(
                self._state.home, {**peer, "ip": ip},
                str(body.get("name") or ""),
                meta=self._pair_meta(body),
            )
        except pairing.PairBusy:
            self._send_error(429, "busy",
                             "too many pending pair requests")
            return
        if self._owner_auto_approve(peer, result["request_id"]):
            result["auto_approved"] = True
        self._send_json(201, result)

    def _owner_auto_approve(self, peer: dict, request_id: str) -> bool:
        """Approve a fresh request without a code when the owner turned
        that on and the peer is the owner's own untagged device.

        On top of ``pairing.owner_auto_approve_refusal``, the request
        must be one a cross-site page can't send: a JSON body (which a
        form can't post without a preflight the hub never answers) and
        no foreign ``Origin``. The native app and the web app both send
        JSON. Any doubt leaves the request pending for a code."""
        try:
            enabled = pairing.auto_approve_owner(self._state.home)
            owner = self._state.owner()
        except Exception:
            return False
        if pairing.owner_auto_approve_refusal(
            peer, owner, enabled, self._state.own_node_id
        ):
            return False
        ct = self.headers.get("Content-Type") or ""
        if not ct.startswith("application/json") or self._bad_origin():
            return False
        try:
            res = pairing.auto_approve_request(
                self._state.home, request_id, peer)
        except Exception:
            return False
        if res is None:
            return False
        _log_pair_approval(res, "auto-approved (owner device)")
        return True

    def _pair_decide(self, approve: bool) -> None:
        ct = self.headers.get("Content-Type") or ""
        if not ct.startswith("application/json"):
            self._send_error(
                403, "bad_origin", "request blocked by origin policy"
            )
            return
        if not self._approver_ok():
            return
        # The peer's node decides self-approval no matter how the
        # caller proved itself — bearer or owner identity alike. A
        # cached miss is re-checked: with no node the self-approval
        # check can't run, and the caller is already authenticated.
        ip = self._client_ip()
        peer = self._state.whois_cached(ip) or self._state.whois(ip)
        node_id = (
            str(peer.get("node_id") or "") or None
        ) if peer else None
        body = self._read_body() or {}
        code = str(body.get("code") or "")
        rid = body.get("request_id")
        rid = str(rid) if rid else None
        if approve:
            try:
                result = pairing.approve_request(
                    self._state.home, code, rid, approver_node_id=node_id
                )
            except pairing.SelfApproval:
                self._send_error(
                    403, "self_approval",
                    "approve from a different device",
                )
                return
            if result is None:
                self._send_error(404, "no_match",
                                 "no pending request matches that code")
                return
            _log_pair_approval(result, "approved with code")
            self._send_json(200, {
                "ok": True, "device_id": result["device_id"],
                "name": result["name"],
            })
            return
        if pairing.deny_request(self._state.home, code, rid):
            self._send_json(200, {"ok": True})
        else:
            self._send_error(404, "no_match",
                             "no pending request matches that code")

    # -- SSE --------------------------------------------------------------

    def _sse_stream(
        self,
        key: str | None,
        after_seq: int,
        device_id: str | None = None,
        subscribe_key: str | None = None,
    ) -> None:
        journal_key = key if key is not None else GLOBAL_KEY
        # Subscribe BEFORE replay: an event appended between read_since
        # and subscribe would be lost from both. Queued events are then
        # deduped by seq against what replay already sent.
        q = self._state.bus.subscribe(
            subscribe_key if subscribe_key is not None else journal_key
        )
        try:
            replay, truncated = self._state.journal.read_since(
                journal_key, after_seq
            )
            last = self._state.journal.last_seq(journal_key)
            if after_seq > last:
                # A cursor from before the journal was wiped: nothing
                # replays, and every new event (seq <= the old cursor)
                # would be dropped as already delivered. Restart the
                # client at the journal's head and tell it to refetch.
                replay, truncated, after_seq = [], True, last
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # An SSE response is the last thing this connection ever
            # does. Must come after end_headers — send_header parses the
            # Connection field and would flip this back off.
            self.close_connection = True
            replayed = after_seq
            try:
                if truncated:
                    self._sse_write(
                        {"type": "journal.truncated", "seq": after_seq})
                for ev in replay:
                    # A keeper-held session's journal holds v2 events:
                    # today's clients get each as its v1 frame, or
                    # nothing where v1 has none (the cursor still moves).
                    frame = self._state.runtime.v1(journal_key, ev) \
                        if key is not None else ev
                    if frame is not None:
                        self._sse_write(frame)
                    if isinstance(ev.get("seq"), int):
                        replayed = max(replayed, ev["seq"])
                self._sse_write({
                    "type": "journal.caught-up",
                    "seq": replayed,
                })
            except (BrokenPipeError, ConnectionResetError,
                    TimeoutError, OSError):
                return
            last_revocation_check = 0.0
            while True:
                try:
                    ev = q.get(timeout=_SSE_HEARTBEAT_S)
                except queue.Empty:
                    ev = None
                now = time.time()
                if (
                    device_id
                    and now - last_revocation_check > _REVOCATION_CHECK_S
                ):
                    last_revocation_check = now
                    if self._device_revoked(self._state.home, device_id):
                        return
                if ev is None:
                    try:
                        self.wfile.write(b": ka\n\n")
                        self.wfile.flush()
                    except (
                        BrokenPipeError, ConnectionResetError,
                        TimeoutError, OSError,
                    ):
                        return
                    continue
                if (
                    isinstance(ev.get("seq"), int)
                    and ev["seq"] <= replayed
                ):
                    continue  # already delivered by the replay pass
                try:
                    self._sse_write(ev)
                    if isinstance(ev.get("seq"), int):
                        replayed = max(replayed, ev["seq"])
                except (
                    BrokenPipeError, ConnectionResetError,
                    TimeoutError, OSError,
                ):
                    return
        finally:
            self._state.bus.unsubscribe(q)

    def _agent_stream(
        self, mid: str, after_seq: int, device_id: str | None = None
    ) -> None:
        """Command SSE for one satellite; presence = this stream's lifetime."""
        state = self._state
        # The seq the satellite acknowledges: new queues past
        # MAX_SAT_PENDING over this floor are refused. A restarted
        # agent reports its contiguous done floor, which can sit below
        # the seqs already acked here — follow it down; the replay
        # dedups what it finished. Map and done-set entries at or under
        # it are for commands that will never report a result again.
        with state._cmd_lock:
            state._sat_acked[mid] = after_seq
            state._prune_sat_cmd_seqs(mid)
            done = state._sat_done.get(mid)
            if done:
                done.difference_update(
                    s for s in done if s <= after_seq)
        state.sat_connected(mid)
        try:
            self._sse_stream(
                HubState.sat_journal_key(mid), after_seq,
                device_id=device_id,
                subscribe_key=HubState.sat_journal_key(mid),
            )
        finally:
            state.sat_disconnected(mid)

    def _pull_attachment(self, key: str, ref: str) -> None:
        target = attachments.resolve(self._state.home, key, ref)
        if target is None:
            self._send_error(404, "not_found", "no such attachment")
            return
        try:
            body = Path(target).read_bytes()
        except OSError:
            self._send_error(404, "not_found", "attachment unreadable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse_write(self, ev: dict) -> None:
        frame = sse_frame(ev)
        if frame is None:
            return
        self.wfile.write(frame)
        self.wfile.flush()

    def _sse_stream_v2(self, key: str, after_seq: int, args,
                       device_id: str | None = None) -> None:
        """``GET /v1/sessions/{key}/events`` for a v2 client
        (``sync.V2Stream`` decides what is sent): the events after
        ``last`` or one ``session.snapshot``, then ``session.state``
        with the live text, then every event and delta as it happens
        and a ``ping`` every ``PING_S``."""
        epoch = _opt_int_arg(args.get("epoch"))
        stream = self._state.sync.open(
            key, last=after_seq, epoch=epoch, device_id=device_id)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # As in ``_sse_stream``: after end_headers, and the last
            # thing this connection does.
            self.close_connection = True
            last_revocation_check = 0.0
            try:
                frames = stream.catch_up()
                while frames is not None:
                    for frame in frames:
                        self.wfile.write(sse_frame_v2(frame))
                    if frames:
                        self.wfile.flush()
                    now = time.time()
                    if (
                        device_id
                        and now - last_revocation_check > _REVOCATION_CHECK_S
                    ):
                        last_revocation_check = now
                        if self._device_revoked(self._state.home, device_id):
                            return
                    frames = stream.next()
            except (BrokenPipeError, ConnectionResetError,
                    TimeoutError, OSError):
                return
        finally:
            stream.close()

    def _messages_v2(self, key: str, args) -> None:
        """``GET .../messages?v=2``: a page of the timeline as v2
        events, paged like the v1 route."""
        limit = max(1, min(_int_arg(args.get("limit"), 500), 2000))
        self._send_json(200, self._state.sync.messages_page(
            key, before=_int_arg(args.get("before"), 0), limit=limit))

    def _sse_after(self, args) -> int:
        """The SSE resume cursor.

        The ``Last-Event-ID`` header wins — it is what an EventSource
        client sends automatically on reconnect, matching the ``id:``
        lines ``_sse_write`` emits. The ``?last=`` query arg is the
        fallback for clients that drive the cursor by hand.
        """
        header = self.headers.get("Last-Event-ID")
        if header is not None:
            try:
                return int(header.strip())
            except ValueError:
                pass  # unreadable header: fall through to the query arg
        return _int_arg(args.get("last"), 0)

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        args = parse_qs(parsed.query)

        if path == "/v1/status":
            if not self._local_host_gate():
                return
            # A reachability check only: machine id, version and uptime
            # tell the whole tailnet more than a caller needs.
            self._send_json(200, self._state.status_view())
            return

        # The requester's poll is unauthenticated — the request id +
        # poll secret it carries are the capability.
        if path.startswith("/v1/pair/request/"):
            if not self._local_host_gate():
                return
            rid = unquote(path[len("/v1/pair/request/"):])
            # The secret rides a header: a query string lands in proxy
            # and access logs. ``?secret=`` still works for the iPhone
            # builds already out, which send it there.
            secret = (self.headers.get("X-Pair-Secret")
                      or str((args.get("secret") or [""])[0]))
            result = pairing.poll_request(self._state.home, rid, secret)
            if result is None:
                self._send_error(
                    404, "unknown_request", "unknown or expired request"
                )
                return
            self._send_json(200, result)
            return

        # Approving devices get in two ways: a paired bearer, or the
        # tailnet login that owns this hub node.
        if path == "/v1/pair/requests":
            if not self._approver_ok():
                return
            self._send_json(
                200, {"requests": pairing.list_requests(self._state.home)}
            )
            return

        # Everything else under /v1 wants a paired device credential, or
        # the owner's own tailnet device.
        device_id = None
        if path.startswith("/v1/"):
            device_id = self._caller(path)
            if device_id is None:
                self._send_error(401, "unauthorized", "pair this device first")
                return

        if path == "/v1/sessions" and args.get("key"):
            self._session_row(device_id, str(args["key"][0]))
            return

        if path == "/v1/sessions":
            self._state.fresh_catalog()
            self._send_json(200, {
                "sessions": readstate.annotate(
                    self._state.home, device_id, self._state.catalog.rows()),
                "machines": list(
                    catalog_mod.peek_machines(self._state.home).values()
                ),
                "server_time": time.time(),
            })
            return

        if path.startswith("/v1/launches/"):
            view = self._state.launch_view(
                unquote(path[len("/v1/launches/"):]))
            if view is None:
                self._send_error(404, "unknown_launch", "no such launch")
                return
            self._send_json(200, view)
            return

        if path == "/v1/folders":
            self._folders(args)
            return

        if path == "/v1/machines":
            self._send_json(200, {
                "machines": list(
                    catalog_mod.peek_machines(self._state.home).values()
                )
            })
            return

        if path == "/v1/devices":
            self._send_json(
                200, devices_mod.view(self._state.home, device_id))
            return

        if path == "/v1/devices/me":
            self._device_me(device_id)
            return

        if path == "/v1/ssh":
            self._send_json(200, sshkeys.view(self._state.home, device_id))
            return

        if path == "/v1/search":
            query = (args.get("q") or [""])[0]
            self._send_json(200, search_mod.search(
                self._state.catalog.rows(),
                query,
                home=self._state.home,
                limit=_int_arg(args.get("limit"), search_mod.DEFAULT_LIMIT),
            ))
            return

        if path == "/v1/usage":
            refresh = github_shared.refresh_asked(args)
            try:
                body = usage_mod.snapshot(refresh=refresh)
            except (OSError, ValueError) as exc:
                self._send_error(503, "usage_unavailable", str(exc))
                return
            body["machine"] = self._state.machine
            body["machines"] = self._usage_machines(body)
            self._send_json(200, body)
            return

        if path == "/v1/app/overview":
            try:
                body = self._state.webapp.overview(
                    period=str((args.get("period") or [""])[0]),
                    tz_name=str((args.get("tz") or [""])[0]) or None,
                    rescan=str((args.get("rescan") or [""])[0]) in {"1", "true"},
                    machine=str((args.get("machine") or [""])[0]) or None,
                    here={"id": self._state.machine_id,
                          "name": self._state.machine.get("name") or self._state.machine_id},
                    others=self._overview_machines(),
                )
            except (OSError, sqlite3.Error) as exc:
                self._send_error(503, "ledger_unavailable", str(exc))
                return
            body["machine"] = self._state.machine.get("name")
            self._send_json(200, body)
            return

        if path == "/v1/app/harnesses":
            try:
                harnesses = webapp_harnesses.rows()
            except (OSError, ValueError) as exc:
                self._send_error(503, "registry_unavailable", str(exc))
                return
            self._send_json(200, {
                "harnesses": harnesses,
                "window_days": usage_mod.WINDOW_DAYS,
                "machine": self._state.machine.get("name"),
            })
            return

        if path == "/v1/app/sessions":
            self._state.fresh_catalog()
            try:
                self._state.expire_pending_approvals()
                body = self._state.webapp.sessions(
                    self._state.catalog.rows(),
                    hub_machine_id=self._state.machine_id,
                    machines=catalog_mod.load_machines(self._state.home),
                    rescan=str((args.get("rescan") or [""])[0]) in {"1", "true"},
                    pending=self._state.pending_approvals,
                )
            except (OSError, sqlite3.Error) as exc:
                self._send_error(503, "ledger_unavailable", str(exc))
                return
            body["machine"] = self._state.machine.get("name")
            body["sessions"] = readstate.annotate(
                self._state.home, device_id, body.get("sessions") or [])
            self._send_json(200, body)
            return

        if path.startswith("/v1/app/"):
            self._page_get(path[len("/v1/app/"):], args, device_id)
            return

        if path == "/v1/inbox":
            done =str((args.get("done") or [""])[0])
            self._state.fresh_catalog(github=True)
            self._state.expire_pending_approvals()
            self._send_json(200, {
                "items": self._state.catalog.inbox(
                    pending=self._state.pending_approvals,
                    dismissed=dismissals.for_device(
                        self._state.home, device_id
                    ),
                    include_done=done in {"1", "true"},
                )
            })
            return

        if path == "/v1/pulls":
            # pulls.py: every pinned repo's open PRs, behind the gh cache.
            self._send_json(*pulls_mod.list_view(self._state, args))
            return

        if path.startswith("/v1/pulls/"):
            parts = path[len("/v1/pulls/"):].split("/")
            if len(parts) != 3:
                self._send_error(404, "not_found", "unknown route")
                return
            # pulls.py: one PR in full (body, checks, reviews).
            self._send_json(*pulls_mod.detail(
                self._state, unquote(parts[0]), unquote(parts[1]),
                parts[2], args))
            return

        if path == "/v1/issues":
            # issues.py: every pinned repo's open issues, behind the cache.
            self._send_json(*issues_mod.list_view(self._state, args))
            return

        if path.startswith("/v1/issues/"):
            parts = path[len("/v1/issues/"):].split("/")
            if len(parts) != 3:
                self._send_error(404, "not_found", "unknown route")
                return
            # issues.py: one issue in full (body, comments, the prompt).
            self._send_json(*issues_mod.detail(
                self._state, unquote(parts[0]), unquote(parts[1]),
                parts[2], args))
            return

        if path == "/v1/tasks":
            # tasks.py: the work-item store over the sources' lists —
            # which session or launch is on which issue or PR.
            self._send_json(*tasks_mod.list_view(self._state, args))
            return

        if path == "/v1/events":
            after = self._sse_after(args)
            self._sse_stream(None, after, device_id=device_id)
            return

        if path == "/v1/agent/commands":
            if not self._require_satellite():
                return
            mid = str((args.get("machine") or [""])[0])
            if not mid:
                self._send_error(
                    422, "bad_command", "machine is required"
                )
                return
            if not self._require_machine(device_id, mid):
                return
            after = self._sse_after(args)
            self._agent_stream(mid, after, device_id=device_id)
            return

        if path.startswith("/v1/agent/attachments/"):
            if not self._require_satellite():
                return
            stem = path[len("/v1/agent/attachments/"):]
            key_part, _sep, ref = stem.rpartition("/")
            key = unquote(key_part)
            # The staged key is hub-scoped ``local@mid``; the pull must
            # stay inside the machine this credential speaks for.
            _local, key_mid = protocol.split_remote_key(key)
            if not self._require_machine(device_id, key_mid):
                return
            self._pull_attachment(key, unquote(ref))
            return

        if path.startswith("/v1/sessions/"):
            stem = path[len("/v1/sessions/"):]
            key_part, sep, action = stem.rpartition("/")
            # ``{key}/commands/{id}``: one stored command (G31).
            owner_part, sep2, middle = key_part.rpartition("/")
            if sep and sep2 and middle == "commands":
                self._command_lookup(unquote(owner_part), unquote(action))
                return
            key = unquote(key_part if sep else stem)
            if sep and action == "commands":
                self._slash_commands(key)
                return
            if sep and action in {"events", "messages", "snapshot"}:
                if self._state.catalog.row_for(key) is None:
                    self._send_error(
                        404, "unknown_session", "no such session"
                    )
                    return
                # Sessions past the background tail cap get a tailer on
                # demand so an opened chat isn't an empty journal.
                session = self._state.catalog.session_for(key)
                row = self._state.catalog.row_for(key) or {}
                if session is not None:
                    self._state.tail_manager.ensure(
                        key, session, str(row.get("status") or "")
                    )
                elif row.get("remote") and row.get("machine_id"):
                    # A satellite session has no local Session to tail —
                    # ask its agent to start one instead.
                    self._state.queue_tail(
                        str(row["machine_id"]), key,
                        str(row.get("status") or ""),
                    )
            v2 = protocol.wants_v2(args, self.headers)
            if sep and action == "events":
                after = self._sse_after(args)
                if v2:
                    self._sse_stream_v2(key, after, args,
                                        device_id=device_id)
                else:
                    self._sse_stream(key, after, device_id=device_id)
                return
            if sep and action == "messages":
                if v2:
                    self._messages_v2(key, args)
                else:
                    self._messages(key, args)
                return
            if sep and action == "snapshot":
                # Always v2: the route is new with it.
                self._send_json(
                    200, self._state.sync.snapshot(key, device_id))
                return
            if sep and action == "files":
                self._files(key, args)
                return
            if sep and action == "preview":
                self._preview(key, args)
                return
            if sep and action == "queue":
                if self._state.catalog.row_for(key) is None:
                    self._send_error(
                        404, "unknown_session", "no such session"
                    )
                    return
                # A satellite session's sends never wait on the hub, so
                # its list is honestly empty.
                self._send_json(200, {
                    "sends": self._state.runtime.queue_view(key)
                    if self._state.runtime.owns(key)
                    else self._state.waiting.list(key),
                })
                return
            self._send_error(404, "not_found", "unknown route")
            return

        self._serve_static(path, parsed.query)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/v1/pair":
            if not self._local_host_gate():
                return
            # Same per-source cap as /v1/pair/request: the 192-bit
            # token isn't brute-forceable, but a flood shouldn't get
            # unlimited tries (or a whois fork each).
            if not self._attempt_ok(self._state._pair_ex_times):
                self._send_error(429, "rate_limited",
                                 "too many pair attempts")
                return
            body = self._read_body()
            if body is None:
                self._send_error(400, "bad_body", "unreadable JSON body")
                return
            result = pairing.exchange(
                self._state.home,
                str(body.get("token") or ""),
                str(body.get("name") or ""),
                meta=self._pair_meta(
                    body, node_name=self._peer_node_name()
                ),
                role=str(body.get("role") or ""),
            )
            if result is None:
                self._send_error(
                    403, "pair_failed", "unknown or expired pair token"
                )
                return
            self._send_json(200, result)
            return

        if path == "/v1/pair/request":
            self._pair_request()
            return

        if path in {"/v1/pair/requests/approve", "/v1/pair/requests/deny"}:
            self._pair_decide(path.endswith("/approve"))
            return

        device_id = self._caller(path)
        if device_id is None:
            device_id = self._scoped_approval_device(path)
            if device_id == _NTFY_REFUSED:
                return  # the 403 already went out
        if device_id is None:
            self._send_error(401, "unauthorized", "pair this device first")
            return

        if path.startswith("/v1/app/"):
            self._page_post(path[len("/v1/app/"):], device_id)
            return

        if path == "/v1/devices/rename":
            self._device_rename(device_id)
            return

        if path == "/v1/devices/revoke":
            self._device_revoke(device_id)
            return

        if path == "/v1/ssh/key":
            self._ssh_key(device_id)
            return

        if path == "/v1/inbox/dismiss":
            body = self._read_body()
            if body is None:
                self._send_error(400, "bad_body", "unreadable JSON body")
                return
            key = body.get("key")
            last = body.get("last_active_at")
            if not dismissals.valid(key, last):
                self._send_error(
                    422, "bad_dismiss",
                    "key and a numeric last_active_at are required",
                )
                return
            try:
                dismissals.dismiss(self._state.home, device_id, key, last)
            except OSError:
                self._send_error(500, "store_failed",
                                 "could not save the dismissal")
                return
            self._send_json(200, {"ok": True})
            return

        if path == "/v1/agent/register":
            if not self._require_satellite():
                return
            body = self._read_body()
            machine = (body or {}).get("machine")
            if not isinstance(machine, dict):
                machine = {}
            mid = str(machine.get("id") or "")
            if not mid:
                self._send_error(
                    422, "bad_command", "machine.id is required"
                )
                return
            if mid == self._state.machine_id:
                # A satellite claiming the hub's own id would shadow the
                # local machine row and its queues.
                self._send_error(
                    403, "forbidden", "cannot register the hub machine"
                )
                return
            if not self._require_machine(device_id, mid, bind=True):
                return
            catalog_mod.upsert_machine(
                self._state.home,
                {**machine, "last_seen_at": time.time()},
            )
            # ``message_delta`` tells the satellite this hub publishes
            # its deltas live instead of journaling them; an older hub
            # would journal every chunk, so the satellite sends none.
            self._send_json(200, {"ok": True, "machine_id": mid,
                                  "message_delta": True})
            return

        if path == "/v1/agent/catalog":
            if not self._require_satellite():
                return
            body = self._read_body()
            if body is None:
                self._send_error(400, "bad_body", "unreadable JSON body")
                return
            mid = str(body.get("machine_id") or "")
            rows = body.get("rows") or []
            if not mid or not isinstance(rows, list):
                self._send_error(
                    422, "bad_command",
                    "machine_id and rows are required",
                )
                return
            if not self._require_machine(device_id, mid):
                return
            # Snapshot ownership before the merge so attach/detach done
            # on the satellite surfaces as an event the clients react to.
            before = {
                protocol.remote_key(mid, str(r.get("key") or "")):
                    r.get("ownership")
                for r in rows if isinstance(r, dict)
            }
            old = {
                k: (self._state.catalog.row_for(k) or {}).get("ownership")
                for k in before
            }
            catalog_mod.merge_remote_rows(self._state.home, mid, rows)
            stored = "slash" in body and slash.save_satellite(
                self._state.home, mid, body["slash"])
            self._state.catalog.refresh()
            for hub_key, ownership in before.items():
                if ownership and ownership != old.get(hub_key):
                    hub_row = self._state.catalog.row_for(hub_key) or {}
                    self._state.emit(hub_key, _ownership_event(
                        hub_key, self._state.machine_id, ownership,
                        hub_row.get("tmux"), hub_row.get("mode"),
                        hub_row.get("kept")))
            self._send_json(200, {
                "ok": True, "rows": len(rows), "slash": bool(stored),
            })
            return

        if path == "/v1/agent/usage":
            if not self._require_satellite():
                return
            body = self._read_body()
            if body is None:
                self._send_error(400, "bad_body", "unreadable JSON body")
                return
            mid = str(body.get("machine_id") or "")
            if not mid:
                self._send_error(
                    422, "bad_command", "machine_id is required"
                )
                return
            if not self._require_machine(device_id, mid):
                return
            try:
                usage_mod.save_satellite(self._state.home, mid, body)
            except ValueError as exc:
                self._send_error(422, "bad_command", str(exc))
                return
            except OSError as exc:
                self._send_error(503, "usage_unavailable", str(exc))
                return
            self._send_json(200, {"ok": True})
            return

        if path == "/v1/agent/events":
            if not self._require_satellite():
                return
            body = self._read_body()
            if body is None:
                self._send_error(400, "bad_body", "unreadable JSON body")
                return
            events = body.get("events") or []
            mid = str(body.get("machine_id") or "")
            if not mid:
                self._send_error(
                    422, "bad_command", "machine_id is required"
                )
                return
            if not isinstance(events, list):
                self._send_error(
                    422, "bad_command", "events must be a list"
                )
                return
            if not self._require_machine(device_id, mid):
                return
            accepted = self._accept_agent_events(mid, events)
            self._send_json(200, {"ok": True, "accepted": accepted})
            return

        if path == "/v1/agent/kept":  # agent.py: satellite keeper sessions
            agent_mod.kept_route(self, device_id)
            return

        if path == "/v1/sessions":
            self._launch()
            return

        if path == "/v1/issues/resolve":  # issues.py: worktree + launch
            self._send_json(*issues_mod.request(
                self._state, self._read_body()))
            return

        if path == "/v1/issues/comment":  # issues.py: gh issue comment
            self._send_json(*issues_mod.comment(
                self._state, self._read_body()))
            return

        if path.startswith("/v1/pulls/"):
            parts = path[len("/v1/pulls/"):].split("/")
            if len(parts) != 4 or parts[3] != "open":
                self._send_error(404, "not_found", "unknown route")
                return
            # pulls.py: detached PR worktree + launch, the resolve shape.
            self._send_json(*pulls_mod.request(
                self._state, unquote(parts[0]), unquote(parts[1]),
                parts[2], self._read_body()))
            return

        if path.startswith("/v1/sessions/"):
            # Resolve "<key>/<action>" by splitting once on the last slash.
            stem = path[len("/v1/sessions/"):]
            key_part, _, action = stem.rpartition("/")
            key = unquote(key_part)
            row = self._state.catalog.row_for(key)
            if row is None:
                self._send_error(404, "unknown_session", "no such session")
                return
            if action == "fork":  # hub or satellite: fork.py routes it
                self._send_json(*fork_mod.request(
                    self._state, key, row, self._read_body()))
                return
            if action in sessionmeta.ACTIONS:
                self._session_meta(key, action)
                return
            if action == "read":  # hub-side, satellite rows included
                self._mark_read(device_id, key, row)
                return
            if action == "task":  # tasks.py: link a work item, hub-side
                self._send_json(*tasks_mod.link_request(
                    self._state, key, self._read_body()))
                return
            remote_mid = row.get("machine_id")
            if remote_mid and remote_mid != self._state.machine_id:
                if not agent_mod.to_keeper(self, key, action, row, device_id):
                    self._forward(key, remote_mid, action, row)
                return
            if action == "attachments":
                self._stage_attachment(key)
                return
            if action in {"message", "approval", "control", "queue"} \
                    and self._state.runtime.owns(key):
                self._kept(key, row, action, device_id)
                return
            if action == "command":
                self._kept_command(key, device_id)
                return
            if action == "control":
                self._control(key, row)
                return
            if action == "queue":
                self._queue_action(key, row)
                return
            if action in {"message", "approval"}:
                if self._refuse_unless_attached(row):
                    return
                if action == "message":
                    self._send_message(key, row)
                else:
                    self._send_approval(key)
                return
            self._send_error(404, "not_found", "unknown route")
            return

        self._send_error(404, "not_found", "unknown route")

    def do_DELETE(self) -> None:
        """Only ``/v1/sessions/{key}/task`` deletes anything: unlink a
        work item (``tasks.py``). Everything else answers 404."""
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/v1/"):
            device_id = self._caller(path)
            if device_id is None:
                self._send_error(401, "unauthorized", "pair this device first")
                return
        if path.startswith("/v1/sessions/"):
            stem = path[len("/v1/sessions/"):]
            key_part, _, action = stem.rpartition("/")
            if action == "task":
                # No catalog check: the task store keeps linked keys past
                # the session's life, so unlinking one must still work
                # after the catalog has forgotten it.
                key = unquote(key_part)
                self._send_json(*tasks_mod.unlink_request(
                    self._state, key, self._read_body()))
                return
        self._send_error(404, "not_found", "unknown route")

    def _folders(self, args) -> None:
        """``GET /v1/folders?path=``: the subfolders of one folder on the
        hub machine, for the phone's folder browser. Names only, no files;
        dot-folders stay hidden. A satellite's disk is not readable from
        here, so another ``machine`` is refused."""
        machine = str((args.get("machine") or [""])[0])
        if machine and machine != self._state.machine_id:
            self._send_error(422, "browse_unavailable",
                             "only the hub machine's folders can be browsed")
            return
        raw = str((args.get("path") or [""])[0]) or os.path.expanduser("~")
        if not raw.startswith("/") or "\x00" in raw:
            self._send_error(422, "bad_cwd", "path must be absolute")
            return
        folder = os.path.realpath(raw)
        if not os.path.isdir(folder):
            self._send_error(422, "bad_cwd",
                             f"no such folder on this machine: {raw}")
            return
        try:
            with os.scandir(folder) as it:
                names = sorted(
                    (e.name for e in it
                     if not e.name.startswith(".") and _is_dir(e)),
                    key=str.casefold,
                )
        except OSError:
            self._send_error(403, "not_readable",
                             f"can't read {folder} on this machine")
            return
        parent = os.path.dirname(folder)
        self._send_json(200, {
            "path": folder,
            "parent": parent if parent != folder else None,
            "folders": [
                {"name": n, "path": os.path.join(folder, n)}
                for n in names[:FOLDER_LIST_CAP]
            ],
            "truncated": len(names) > FOLDER_LIST_CAP,
        })

    def _files(self, key: str, args) -> None:
        """``GET /v1/sessions/{id}/files?q=&limit=``: paths under the
        session's working folder for the prompt box's ``@`` menu
        (``files.py``)."""
        row = self._state.catalog.row_for(key)
        if row is None:
            self._send_error(404, "unknown_session", "no such session")
            return
        try:
            body = files_mod.for_session(
                row,
                hub_machine_id=self._state.machine_id,
                machines=catalog_mod.load_machines(self._state.home),
                query=str((args.get("q") or [""])[0]),
                limit=_int_arg(args.get("limit"), files_mod.DEFAULT_LIMIT),
            )
        except files_mod.FilesError as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        self._send_json(200, body)

    def _preview(self, key: str, args) -> None:
        """``GET /v1/sessions/{id}/preview?path=``: one ``.html``,
        ``.htm`` or ``.svg`` file from the session's working folder, as
        it is on disk now, for the phone's Preview sheet
        (``preview.py``)."""
        row = self._state.catalog.row_for(key)
        if row is None:
            self._send_error(404, "unknown_session", "no such session")
            return
        try:
            got = preview_mod.for_session(
                row,
                hub_machine_id=self._state.machine_id,
                machines=catalog_mod.load_machines(self._state.home),
                path=str((args.get("path") or [""])[0]),
            )
        except files_mod.FilesError as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        self.send_response(200)
        self.send_header("Content-Type", got.content_type)
        self.send_header("Content-Length", str(len(got.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Last-Modified", formatdate(got.mtime, usegmt=True)
        )
        self.send_header("Content-Security-Policy", preview_mod.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(got.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _launch(self) -> None:
        """``POST /v1/sessions``: start a harness on the hub or a satellite.

        Answers 202 with a ``launch_id``; ``GET /v1/launches/<id>`` turns
        it into the session key once the new transcript is adopted.
        """
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        try:
            cmd = protocol.parse_launch_command(body)
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        state = self._state
        machine = cmd["machine"] or state.machine_id
        harness, cwd = cmd["harness"], cmd["cwd"]
        if machine != state.machine_id:
            row = catalog_mod.load_machines(state.home).get(machine)
            if row is None:
                # No satellite by that id would ever drain the queue:
                # the launch would sit pending in a fresh journal.
                self._send_error(404, "unknown_machine",
                                 f"no machine {machine} on this hub")
                return
            # A satellite older than the launchable field reports none;
            # it checks its own registry when the command arrives.
            offered = row.get("launchable")
            if isinstance(offered, list) and harness not in offered:
                self._send_error(422, "not_launchable",
                                 f"{harness} can't be launched on {machine}")
                return
            # A satellite too old to know the launch-option fields
            # would accept the command and silently drop the pick.
            if (cmd["model"] or cmd["effort"]) and not \
                    catalog_mod.reads_launch_options(row):
                self._send_error(
                    409, "unsupported_launch_option",
                    f"{machine} runs an agent too old to take launch "
                    "options; upgrade it, or launch without them")
                return
            # Same gate for yolo: a silently dropped bypass is a
            # session the user thinks runs unattended but doesn't.
            if cmd["yolo"] and not catalog_mod.reads_launch_yolo(row):
                self._send_error(
                    409, "unsupported_launch_option",
                    f"{machine} runs an agent too old to take yolo; "
                    "upgrade it, or launch without it")
                return
            # The satellite's own advertised lists answer: its registry
            # (capabilities.model) decides what its launch can take, so a
            # launch command never carries a field it can't apply.
            blocked = catalog_mod.launch_option_error(
                row, harness, cmd["model"], cmd["effort"], cmd["yolo"])
            if blocked:
                self._send_error(409, "unsupported_launch_option", blocked)
                return
            # Only the slug goes to the satellite, which reads the key
            # from its own keys file; its row lists the slugs it has.
            if cmd["provider"] and not \
                    catalog_mod.reads_launch_provider(row):
                self._send_error(
                    409, "unsupported_launch_option",
                    f"{machine} runs an agent too old to take a provider; "
                    "upgrade it, or launch without one")
                return
            blocked = catalog_mod.provider_option_error(
                row, harness, cmd["provider"])
            if blocked:
                self._send_error(422, "unsupported_provider", blocked)
                return
            launch_id = state.new_launch(
                machine, harness, cwd,
                model=cmd["model"] or "", effort=cmd["effort"] or "",
                yolo=cmd["yolo"], provider=cmd["provider"] or "")
            queued = state.queue_command(machine, "launch", "", {
                "harness": harness, "cwd": cwd, "prompt": cmd["prompt"],
                "model": cmd["model"] or "", "effort": cmd["effort"] or "",
                "yolo": cmd["yolo"],
                "provider": cmd["provider"] or "",
                "launch_id": launch_id,
            })
            if queued is None:
                state.settle_launch(launch_id, state="failed",
                                    note="machine is too far behind")
                self._send_error(429, "backlog_full",
                                 "machine is too far behind")
                return
            self._send_json(202, {
                "ok": True, "queued": True, "launch_id": launch_id,
                "state": "pending", "note": "queued for satellite",
            })
            return
        if not os.path.isdir(cwd):
            self._send_error(422, "bad_cwd",
                             f"no such folder on this machine: {cwd}")
            return
        rows = state.catalog.tail_rows()
        # Failures a phone can act on answer now; launch_new itself
        # runs after the reply, since priming the pane can take 30 s and
        # the phone needs the launch id to follow it.
        binary = catalog_mod.launch_command(harness)
        if binary is None:
            self._send_error(
                422, "not_launchable",
                f"{harness} can't start here: archived, switched off in "
                f"Provider visibility, or not installed",
            )
            return
        blocked = supervise.fresh_launch_error(
            harness, cmd["model"], cmd["effort"], yolo=cmd["yolo"])
        if blocked:
            self._send_error(409, "unsupported_launch_option", blocked)
            return
        if cmd["provider"]:
            from swe.remote import launch_provider

            try:
                blocked = launch_provider.provider_error(
                    harness, cmd["provider"],
                    keys=launch_provider.read_keys(
                        launch_provider.keys_path(state.home)))
            except ValueError as exc:
                blocked = f"can't read the keys file: {exc}"
            if blocked:
                self._send_error(422, "unsupported_provider", blocked)
                return
        rt = state.runtime
        if rt.can_drive(harness) and not cmd["yolo"]:
            # Through a keeper: the session's id is chosen here, so
            # there is no pending marker and nothing to adopt. A yolo
            # launch still takes the path below (the keeper has no
            # autonomy setting yet).
            self._launch_kept(body, cmd, machine)
            return
        tracked = harness not in catalog_mod.untracked(
            catalog_mod.launchable_harnesses())
        # An ACP launch needs no tmux; its fallback reports a missing
        # tmux itself.
        over_acp = supervise.acp_launch_eligible(
            harness, cmd["prompt"], model=cmd["model"],
            effort=cmd["effort"], yolo=cmd["yolo"],
            provider=cmd["provider"], tracked=tracked)
        if not over_acp and not supervise.tmux_bin():
            self._send_error(422, "launch_failed", "tmux is not installed")
            return
        # One new session per folder: adoption tells launches apart only
        # by harness, folder and time, so a second one waiting here
        # would leave both unadopted. launch_new re-checks under the
        # registry lock; this is the answer the phone can show now.
        busy = tracked and supervise.fork_blocker(state.home, harness, cwd)
        if busy:
            self._send_error(409, "launch_pending",
                             supervise.launch_busy_note(harness, busy))
            return
        launch_id = state.new_launch(
            machine, harness, cwd,
            model=cmd["model"] or "", effort=cmd["effort"] or "",
            yolo=cmd["yolo"], provider=cmd["provider"] or "")

        def run() -> None:
            # Every same-cwd session that exists before launch: adoption
            # later refuses these ids, so a pre-existing session can never
            # be claimed as the one we just started.
            known = catalog_mod.known_session_ids(rows, harness, cwd)
            ok, note, _name = supervise.launch_new(
                state.home, harness, cwd, cmd["prompt"],
                known_ids=known, launch_id=launch_id, binary=binary,
                model=cmd["model"], effort=cmd["effort"], yolo=cmd["yolo"],
                provider=cmd["provider"], exclusive=tracked,
            )
            if not ok:
                state.settle_launch(launch_id, state="failed", note=note)
            elif not tracked:
                state.settle_launch(launch_id, state="untracked", note=(
                    f"{harness} is running under tmux, but swe can't read "
                    f"its sessions, so there's no chat to open"
                ))
            else:
                if supervise.ACP_FALLBACK_NOTE in note:
                    # Kept on the launch record: the poll says why the
                    # session runs under tmux, not ACP.
                    state.settle_launch(launch_id, note=note)
                # The transcript lands any second: the catalog loop must
                # not sleep out its 10 s tick before looking for it.
                state._catalog_wake.set()

        threading.Thread(
            target=run, name=f"remote-launch-{launch_id}", daemon=True
        ).start()
        self._send_json(202, {
            "ok": True, "launch_id": launch_id, "state": "pending",
            "note": "starting over ACP" if over_acp else "starting under tmux",
        })

    def _launch_kept(self, body: dict, cmd: dict, machine: str) -> None:
        """``POST /v1/sessions`` for a harness SWE drives through a
        keeper. A retry under the same ``command_id`` (or ``client_id``)
        answers the launch it already made."""
        state, rt = self._state, self._state.runtime
        try:
            command_id = store_mod.command_id_of(body)
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        fields = {"model": cmd["model"] or "", "effort": cmd["effort"] or "",
                  "provider": cmd["provider"] or ""}
        made = rt.launch_for(command_id)
        if made is None:
            launch_id = state.new_launch(machine, cmd["harness"], cmd["cwd"],
                                         **fields)
            try:
                rt.launch(launch_id, cmd["harness"], cmd["cwd"],
                          cmd["prompt"], command_id=command_id, **fields)
            except runtime_mod.DuplicateLaunch as dup:
                # Another request under this id got there first.
                state.drop_launch(launch_id)
                made = dup.launch_id
            except runtime_mod.Refused as exc:
                state.settle_launch(launch_id, state="failed",
                                    note=exc.message)
                self._send_error(exc.status, exc.code, exc.message)
                return
            else:
                self._send_json(202, {
                    "ok": True, "launch_id": launch_id, "state": "pending",
                    "note": "starting",
                })
                return
        if not rt.launch_matches(made, cmd["harness"], cmd["cwd"],
                                 cmd["prompt"], **fields):
            self._send_error(409, "command_id_conflict", (
                "command_id was already used for a different launch"))
            return
        view = state.launch_view(made) or {}
        self._send_json(202, {
            "ok": True, "launch_id": made, "duplicate": True,
            "state": view.get("state") or "pending",
            "note": view.get("note") or "",
        })

    def _kept(self, key: str, row: dict, action: str,
              device_id: str | None) -> None:
        """A write to a session a keeper holds: ``message``,
        ``approval``, ``control`` or ``queue``. Each is stored under its
        ``command_id`` (``client_id`` from a client that predates it)
        before it is delivered, so a retry answers the stored receipt
        and is never applied twice."""
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        state, rt = self._state, self._state.runtime
        try:
            # A queue action's ``client_id`` names the queued message,
            # not the action: only ``command_id`` is the action's own.
            command_id = store_mod.command_id_of(
                {"command_id": body.get("command_id")}
                if action == "queue" else body)
            if action == "message":
                cmd = protocol.parse_message_command(body)
                paths = []
                for ref in cmd["attachments"]:
                    path = attachments.resolve(state.home, key, ref)
                    if path is None:
                        self._send_error(422, "bad_attachment",
                                         f"unknown attachment {ref}")
                        return
                    paths.append(path)
                self._send_json(*rt.send(
                    key, cmd["text"], mode=cmd["mode"], paths=paths,
                    device=device_id, command_id=command_id))
            elif action == "approval":
                cmd = protocol.parse_approval_command(body)
                self._send_json(*rt.respond(
                    key, cmd["request_id"], cmd["decision"],
                    device=device_id, command_id=command_id))
            elif action == "queue":
                act = protocol.parse_queue_action(body)
                self._send_json(*rt.queue_action(
                    key, act["client_id"], act["op"], act["text"],
                    device=device_id, command_id=command_id))
            else:
                self._kept_control(key, body, device_id, command_id)
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
        except runtime_mod.Refused as exc:
            self._send_error(exc.status, exc.code, exc.message)

    # ``POST .../command`` ops and the fields each takes.
    _KEPT_OPS = {
        "set_option": ("name", "value"),
        "stop_task": ("task_id",),
        "interrupt": ("clear_queue",),
        "park": (),
    }

    def _kept_command(self, key: str, device_id: str | None) -> None:
        """``POST /v1/sessions/{key}/command {op, command_id?, ...}``:
        the keeper ops today's routes have no name for. ``set_option``
        takes ``name`` (``model``, ``effort``, ``permission_mode``,
        ``plan_mode``) and ``value``; ``stop_task`` a ``task_id``;
        ``interrupt`` an optional ``clear_queue``; ``park`` nothing.
        Answers the receipt. Only for a session a keeper holds."""
        rt = self._state.runtime
        if not rt.owns(key):
            self._send_error(409, "not_kept", (
                "this session is not run by a session keeper"))
            return
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        op = body.get("op")
        if not isinstance(op, str) or op not in self._KEPT_OPS:
            self._send_error(422, "bad_command", (
                f"op must be one of {sorted(self._KEPT_OPS)}"))
            return
        fields = {name: body[name] for name in self._KEPT_OPS[op]
                  if body.get(name) is not None}
        try:
            command_id = store_mod.command_id_of(body)
            status, reason = rt.command(key, op, device=device_id,
                                        command_id=command_id, **fields)
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        except runtime_mod.Refused as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        if status == "rejected":
            self._send_error(409, "command_rejected",
                             runtime_mod._reason(reason))
            return
        self._send_json(200, {"ok": True, "receipt": status,
                              "command_id": command_id or ""})

    def _kept_control(self, key: str, body: dict, device_id: str | None,
                      command_id: str | None) -> None:
        state, rt = self._state, self._state.runtime
        cmd = protocol.parse_control_command(body)
        op, mode = cmd["op"], cmd.get("mode") or "now"
        if op == "attach":
            undone = state.detaches.cancel(key, "cancelled by attach")
            self._send_json(200, {"ok": True, "note": (
                "already attached; the pending detach is cancelled"
                if undone == "cancelled" else "already attached")})
            return
        if op in {"kill", "detach"}:
            if op == "detach" and mode == "keep":
                raise runtime_mod.Refused(409, "control_failed", (
                    "keep leaves a tmux pane running; this session has "
                    "none. Detach it instead: it can be continued later"))
            if op == "detach" and mode == "when_idle":
                self._detach_when_idle(key)
                return
            state.detaches.cancel(key, f"replaced by {op} {mode}")
            rt.close(key)
            attachments.drop_session(state.home, key)
            state.session_released(key)
            self._send_json(200, {"ok": True, "note": "detached"})
            return
        self._send_json(*rt.control(
            key, op, str(cmd.get("value") or ""), device=device_id,
            command_id=command_id))

    def _injector(self, key: str):
        meta = supervise.meta_for(self._state.home, key)
        # emit + machine let tier-A channels raise approval events from
        # their server streams, same path the tmux watcher uses. Every
        # write through it wakes the session's tailer.
        return self._state.tail_manager.waking(key, inject.injector_for(
            self._state.home, key, meta,
            emit=self._state.emit, machine=self._state.machine_id,
            deltas=self._state.deltas,
        ))

    def _forward(
        self, key: str, mid: str, action: str, row: dict
    ) -> None:
        """Route a write to the satellite owning ``key``.

        Same ownership rules as local writes: observed sessions only
        accept ``attach``. Everything lands in the machine's command
        journal, so the phone gets 202 whether the satellite is live or
        asleep — results arrive later as ``command.result`` events.
        """
        if action == "queue":
            # A satellite parks a queue-mode send on its own machine
            # until the session is idle; the hub holds nothing to list,
            # steer or remove.
            self._send_error(
                409, "queue_unsupported",
                "sends to another machine's session wait on that "
                "machine; they can't be steered or removed from here",
            )
            return
        if action != "attach" and action != "control":
            if self._refuse_unless_attached(row):
                return
        if action == "attachments":
            upload = self._read_upload()
            if upload is None:
                return
            name, data = upload
            payload, err = attachments.stage(
                self._state.home, key, name, data
            )
            if payload is None:
                self._send_error(
                    422, "bad_attachment", err or "stage failed"
                )
                return
            cmd = self._state.queue_command(mid, "attachment", key, {
                "ref": payload["ref"],
                "hub_key": key,
                "machine": mid,
            })
            if cmd is None:
                self._send_error(429, "backlog_full",
                                 "machine is too far behind")
                return
            self._send_json(202, {**payload, "queued": True})
            return
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        if action == "message":
            if self._refuse_unless_kept(body):
                return
            try:
                cmd = protocol.parse_message_command(body)
            except ValueError as exc:
                self._send_error(422, "bad_command", str(exc))
                return
            queued = self._state.queue_command(mid, "message", key, {
                "text": cmd["text"], "mode": cmd["mode"],
                "attachments": cmd["attachments"],
                "client_id": cmd["client_id"],
                "status": row.get("status") or "",
            })
            if queued is None:
                self._send_error(429, "backlog_full",
                                 "machine is too far behind")
                return
            # Same shape as the local 202. ``waiting`` stays false: the
            # satellite waits for idle itself, and a client that saw
            # ``waiting`` would park the send in its tray, whose Edit /
            # Send now / Remove act on the hub's queue, which holds
            # nothing for another machine's session.
            self._send_json(202, {
                "ok": True, "queued": True,
                "client_id": cmd["client_id"], "waiting": False,
            })
            return
        elif action == "approval":
            try:
                cmd = protocol.parse_approval_command(body)
            except ValueError as exc:
                self._send_error(422, "bad_command", str(exc))
                return
            queued = self._state.queue_command(mid, "approval", key, {
                "request_id": cmd["request_id"],
                "decision": cmd["decision"],
            })
            if queued is None:
                self._send_error(429, "backlog_full",
                                 "machine is too far behind")
                return
        elif action == "control":
            try:
                cmd = protocol.parse_control_command(body)
            except ValueError as exc:
                self._send_error(422, "bad_command", str(exc))
                return
            op = cmd["op"]
            if op == "attach":
                try:
                    a_model = protocol._launch_value(body, "model")
                    a_effort = protocol._launch_value(body, "effort")
                except ValueError as exc:
                    self._send_error(422, "bad_command", str(exc))
                    return
                # Same gate as a satellite launch: an agent that does
                # not report launch-option support would drop the pick.
                if (a_model or a_effort) and not \
                        catalog_mod.reads_launch_options(
                            catalog_mod.load_machines(
                                self._state.home).get(mid)):
                    self._send_error(
                        409, "unsupported_launch_option",
                        f"{mid} runs an agent too old to take launch "
                        "options; upgrade it, or attach without them")
                    return
                queued = self._state.queue_command(mid, "attach", key, {
                    "mode": body.get("mode") or "auto",
                    "status": row.get("status") or "",
                    "model": a_model or "", "effort": a_effort or "",
                })
            else:
                if self._refuse_unless_attached(row):
                    return
                fields = {"ctl": op, "value": cmd.get("value") or ""}
                if "mode" in cmd:
                    fields["mode"] = cmd["mode"]
                # The agent echoes it on the command.result, so the phone
                # can tell its own model switch's failure from another
                # control's.
                cid = body.get("client_id")
                if isinstance(cid, str) and cid:
                    fields["client_id"] = cid[:64]
                queued = self._state.queue_command(
                    mid, "control", key, fields)
            if queued is None:
                self._send_error(429, "backlog_full",
                                 "machine is too far behind")
                return
        else:
            self._send_error(404, "not_found", "unknown route")
            return
        self._send_json(202, {"ok": True, "queued": True})

    def _accept_agent_events(
        self, mid: str, events: list
    ) -> int:
        """Journal upstream events under the hub-scoped ``key@mid``.

        Anything outside ``_SAT_EVENT_TYPES`` is dropped, and an event
        whose ``data.kind`` is not in that type's vocabulary is dropped:
        the journal feeds the clients verbatim, so an unknown kind would
        render unvetted markup there.
        """
        accepted = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if not isinstance(etype, str) or etype not in _SAT_EVENT_TYPES:
                continue
            data = ev.get("data")
            if data is not None and not isinstance(data, dict):
                # emit() and the clients read data as an object; one
                # bad event would 500 the batch, and the agent resends a
                # refused batch whole, journaling its good events again.
                continue
            kinds = _SAT_KIND_SETS.get(etype)
            if kinds is not None:
                kind = data.get("kind") if isinstance(data, dict) else None
                if not isinstance(kind, str) or kind not in kinds:
                    continue
            local_key = str(ev.get("session") or "")
            if etype == "message.delta":
                delta = _sat_delta(mid, local_key, ev.get("data"))
                if delta is not None:
                    self._state.publish_transient(delta["session"], delta)
                    accepted += 1
                continue
            if local_key:
                hub_key = protocol.remote_key(mid, local_key)
                ev = dict(ev, session=hub_key, machine=mid)
                self._state.emit(hub_key, ev)
            else:
                self._state.emit(GLOBAL_KEY, dict(ev, machine=mid))
            accepted += 1
        return accepted

    def _send_message(self, key: str, row: dict) -> None:
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        if self._refuse_unless_kept(body):
            return
        try:
            cmd = protocol.parse_message_command(body)
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        # Resolve attachment refs to staged paths before injection.
        refs = list(cmd["attachments"])
        paths = []
        for ref in refs:
            path = attachments.resolve(self._state.home, key, ref)
            if path is None:
                self._send_error(
                    422, "bad_attachment", f"unknown attachment {ref}"
                )
                return
            paths.append(path)
        cmd["attachments"] = paths
        session = self._state.catalog.session_for(key)
        # The row's status can lag a just-finished turn — reclassify now
        # so ``waiting`` and the first poll don't trust a stale read.
        status = self._fresh_status(
            session, str(row.get("status") or ""), key)
        client_id = cmd["client_id"]
        state = self._state
        waiting = cmd["mode"] == "queue" and status == "active"
        qkey = self._send_key(key, cmd["mode"])
        if state.sendq.pending(qkey) >= MAX_SEND_BACKLOG:
            self._send_error(
                429, "backlog_full",
                "too many messages waiting for this session",
            )
            return
        alive = None
        if cmd["mode"] == "queue":
            send = sendq.QueuedSend(
                client_id, cmd["text"], refs,
                {"cmd": cmd, "session": session, "status": status},
            )
            added = state.waiting.add(key, send)
            if added:
                job, cancel = self._queue_job(key, send)
                # A steered or removed send frees its backlog slot now,
                # not when its parked job finally reaches the front.
                alive = send.is_waiting
        else:
            added = not state.waiting.has(key, client_id)
            if added:
                job, cancel = self._steer_job(key, cmd, session, status)
        if not added:
            # Two sends under one client_id would make the tray's
            # actions and the command.result ambiguous.
            self._send_error(
                409, "duplicate_client_id",
                "a send with this client_id is still queued",
            )
            return
        state.sendq.submit(qkey, job, cancel, alive)
        self._send_json(202, {
            "ok": True, "queued": True, "client_id": client_id,
            "waiting": waiting,
        })

    @staticmethod
    def _send_key(key: str, mode: str) -> str:
        # steer jobs get their own key: ordered among themselves but
        # never stuck behind a queue-mode job waiting out a turn.
        return key if mode == "queue" else f"{key}\x00steer"

    def _queue_job(self, key: str, send: "sendq.QueuedSend"):
        """A queue-mode send's (job, cancel): wait for idle, then inject.

        Every exit that reports goes through ``waiting.claim`` first; a
        send the phone already steered or removed exits silently, since
        that action reported it.
        """
        state = self._state
        cmd = send.payload["cmd"]
        session = send.payload["session"]
        status = send.payload["status"]
        client_id = send.client_id

        def fail(note: str) -> None:
            if state.waiting.claim(key, send, "failed"):
                self._send_result(key, client_id, False, note, False)

        def job() -> None:
            if send.state != "waiting":
                return
            # Re-read at run time: the submit-time status predates any
            # wait behind earlier jobs on this key — a send that started
            # a turn since then must still be waited out. The Session
            # object itself can go stale mid-wait (the status classifier
            # times out against session.timestamp), so re-fetch it too.
            current = state.catalog.session_for(key) or session
            live = self._fresh_status(current, status, key)
            deadline = time.monotonic() + QUEUE_WAIT_MAX_S
            while live == "active" and time.monotonic() < deadline:
                send.wake.wait(QUEUE_POLL_S)
                if send.state != "waiting":
                    return
                if state._stop.is_set():
                    fail("hub restarted; message not sent")
                    return
                current = state.catalog.session_for(key) or session
                live = self._fresh_status(current, status, key)
            if live == "active":
                fail("session stayed busy for 30 min; message not sent")
                return
            if not state.waiting.claim(key, send, "sending"):
                return
            self._deliver(key, client_id, current, cmd, live)

        def cancel() -> None:
            fail("hub restarted; message not sent")

        return job, cancel

    def _steer_job(self, key: str, cmd: dict, session, status: str):
        """A steer-mode send's (job, cancel): inject as soon as it runs."""
        state = self._state

        def job() -> None:
            current = state.catalog.session_for(key) or session
            live = self._fresh_status(current, status, key)
            self._deliver(key, cmd["client_id"], current, cmd, live)

        def cancel() -> None:
            self._send_result(
                key, cmd["client_id"], False,
                "hub restarted; message not sent", False,
            )

        return job, cancel

    def _deliver(
        self, key: str, client_id: str, session, cmd: dict, live: str
    ) -> None:
        try:
            result = self._injector(key).send(session, cmd, live)
            ok, note, degraded = result.ok, result.note, result.degraded
        except Exception as exc:
            ok, note, degraded = False, str(exc), False
        self._send_result(key, client_id, ok, note, degraded)

    # Why a client_id is no longer waiting, for the 409 on a late action.
    _GONE_NOTES = {
        "sending": "that message already went out",
        "steered": "that message was already sent now",
        "removed": "that message was already removed",
        "failed": "that message already failed",
    }

    def _queue_action(self, key: str, row: dict) -> None:
        """``POST /v1/sessions/{key}/queue {client_id, op, text?}``.

        ``steer`` takes one waiting queue-mode send off the idle wait and
        submits it as a steer (after the steers already queued);
        ``cancel`` drops it and reports ``command.result`` with
        ``cancelled: true``; ``edit`` replaces its text in place. All
        three act under the ``Waiting`` lock, so they race the send's own
        job safely: the job injects only after claiming it there.
        """
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        try:
            act = protocol.parse_queue_action(body)
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        client_id, op = act["client_id"], act["op"]
        state = self._state
        steer_key = self._send_key(key, "steer")
        if op == "edit":
            text = act["text"]

            def change(send) -> None:
                # The job's command is the same dict, read only after
                # its claim, so it injects the new text.
                send.text = text
                send.payload["cmd"]["text"] = text

            send, gone = state.waiting.update(key, client_id, change)
            if send is None:
                self._queue_gone(gone)
                return
            self._send_json(200, {
                "ok": True, "client_id": client_id, "op": op,
                "text": text,
            })
            return
        if op == "steer":
            # Sending now is a write: the same rules as a steer message.
            if self._refuse_unless_attached(row):
                return
            if state.sendq.pending(steer_key) >= MAX_SEND_BACKLOG:
                self._send_error(
                    429, "backlog_full",
                    "too many messages waiting for this session",
                )
                return
        send, gone = state.waiting.take(
            key, client_id, "steered" if op == "steer" else "removed"
        )
        if send is None:
            self._queue_gone(gone)
            return
        if op == "cancel":
            self._send_result(
                key, client_id, False, "removed before sending", False,
                cancelled=True,
            )
        else:
            payload = send.payload
            job, cancel = self._steer_job(
                key, dict(payload["cmd"], mode="steer"),
                payload["session"], payload["status"],
            )
            state.sendq.submit(steer_key, job, cancel)
        self._send_json(200, {"ok": True, "client_id": client_id, "op": op})

    def _queue_gone(self, gone: str) -> None:
        """Answer an action on a send that is not waiting."""
        if gone == "unknown":
            self._send_error(
                404, "unknown_send", "no queued message with that client_id",
            )
        else:
            self._send_error(409, "not_waiting", self._GONE_NOTES.get(
                gone, "that message is no longer queued"))

    def _fresh_status(self, session, fallback: str, key: str = "") -> str:
        """Reclassify one session now; the catalog row can be stale.

        The catalog's session can carry an activity time minutes old
        for a turn that just started, which the probe reads as
        "interrupted"; the tailer knows the transcript moved since.
        """
        try:
            if key:
                session = self._state.tail_manager.fresh_session(
                    key, session)
            got = self._state.status_of([session])
        except Exception:
            return fallback
        return str(got[0]) if got else fallback

    def _send_result(
        self,
        key: str,
        client_id: str,
        ok: bool,
        note: str,
        degraded: bool,
        cancelled: bool = False,
    ) -> None:
        """Report a queued send the way a satellite would.

        ``cancelled`` is set (and true) only on a send the phone removed
        from the queue, so a client can tell it from a failure.
        """
        fields = {
            "command_id": "", "op": "message", "ok": ok,
            "note": note, "degraded": degraded,
            "client_id": client_id,
        }
        if cancelled:
            fields["cancelled"] = True
        self._state.emit(
            key,
            protocol.event(
                "command.result", key, self._state.machine_id, fields,
            ),
        )

    def _send_approval(self, key: str) -> None:
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        try:
            cmd = protocol.parse_approval_command(body)
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        session = self._state.catalog.session_for(key)
        result = self._injector(key).approval(
            session, cmd["request_id"], cmd["decision"]
        )
        if not result.ok:
            code = (
                "approval_not_supported"
                if result.note.startswith("approval_not_supported")
                else "approval_failed"
            )
            self._send_error(409, code, result.note)
            return
        self._state.emit(
            key,
            protocol.event(
                "approval.resolved", key, self._state.machine_id,
                {
                    "request_id": cmd["request_id"],
                    "decision": cmd["decision"],
                },
            ),
        )
        self._send_json(200, {"ok": True, "note": result.note})

    def _control(self, key: str, row: dict) -> None:
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        try:
            cmd = protocol.parse_control_command(body)
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        op = cmd["op"]
        mode = cmd.get("mode") or "now"
        if op == "attach":
            self._attach(key, row, body)
            return
        if op == "kill" or (op == "detach" and mode != "when_idle"):
            # A later detach or kill replaces a pending when_idle one.
            self._state.detaches.cancel(key, f"replaced by {op} {mode}")
        if self._refuse_unless_attached(row):
            return
        if op == "detach" and mode == "when_idle":
            self._detach_when_idle(key)
            return
        session = self._state.catalog.session_for(key)
        if op == "detach" and mode == "keep":
            result = inject.InjectResult(*supervise.keep(
                self._state.home, key))
        else:
            result = self._injector(key).control(
                session, op, str(cmd.get("value") or "")
            )
        if not result.ok:
            self._send_error(409, "control_failed", result.note)
            return
        if op in {"kill", "detach"}:
            self._detached(key, drop_files=mode != "keep")
        self._send_json(200, {"ok": True, "note": result.note})

    def _detached(self, key: str, drop_files: bool = True) -> None:
        # A kept harness still runs and may read its staged files.
        if drop_files:
            attachments.drop_session(self._state.home, key)
        row = self._state.catalog.row_for(key)
        if row is not None:
            row["ownership"] = protocol.OWNERSHIP_OBSERVED
        self._emit_ownership(key)

    def _detach_when_idle(self, key: str) -> None:
        """``detach`` with ``mode: when_idle``: 202 now, detach later.

        The wait, its 30 min cap and its cancel live in ``detach.py``;
        the outcome lands as a ``command.result`` (``op: detach``).
        """
        state = self._state

        def status() -> str:
            owned = state.runtime.session(key)
            if owned is not None:
                # The keeper's own state, not a read of the transcript.
                return owned.status
            row = state.catalog.row_for(key) or {}
            session = state.catalog.session_for(key)
            fallback = str(row.get("status") or "")
            return self._fresh_status(session, fallback, key) \
                if session is not None else fallback

        def fire() -> tuple[bool, str]:
            if state.runtime.owns(key):
                try:
                    state.runtime.close(key)
                except runtime_mod.Refused as exc:
                    return False, exc.message
                attachments.drop_session(state.home, key)
                state.session_released(key)
                return True, "detached"
            if supervise.meta_for(state.home, key) is None:
                return False, "session was already detached"
            result = self._injector(key).control(
                state.catalog.session_for(key), "detach", "")
            if result.ok:
                self._detached(key)
            return result.ok, result.note

        def report(ok: bool, note: str, cancelled: bool) -> None:
            fields = {"command_id": "", "op": "detach",
                      "mode": "when_idle", "ok": ok, "note": note}
            if cancelled:
                fields["cancelled"] = True
            state.emit(key, protocol.event(
                "command.result", key, state.machine_id, fields))

        fresh = state.detaches.schedule(
            key, status=status, fire=fire, report=report)
        self._send_json(202, {
            "ok": True, "queued": True, "mode": "when_idle",
            "note": "detaches when the current turn ends" if fresh
            else "a detach is already waiting for this turn to end",
        })

    def _attach(self, key: str, row: dict, body: dict) -> None:
        # Attaching again cancels a when_idle detach; one already
        # running finishes first, and this attach then starts over.
        undone = self._state.detaches.cancel(key, "cancelled by attach")
        if undone == "fired":
            # The detach ran while we waited, and a catalog refresh may
            # have replaced the row: judge from the current one and the
            # registry, never the request's copy.
            row = self._state.catalog.row_for(key) or row
            if supervise.meta_for(self._state.home, key) is None:
                row["ownership"] = protocol.OWNERSHIP_OBSERVED
        if row.get("ownership") == protocol.OWNERSHIP_ATTACHED:
            self._send_json(200, {"ok": True, "note": (
                "already attached; the pending detach is cancelled"
                if undone == "cancelled" else "already attached")})
            return
        if not row.get("attachable"):
            self._send_error(
                422, "not_attachable",
                "this harness cannot be resumed under supervision",
            )
            return
        session = self._state.catalog.session_for(key)
        if session is None:
            self._send_error(404, "unknown_session", "no such session")
            return
        mode = str(body.get("mode") or "auto")
        if mode not in supervise.ATTACH_MODES:
            self._send_error(
                422, "bad_mode",
                f"mode must be one of {sorted(supervise.ATTACH_MODES)}",
            )
            return
        if mode == "auto" and not row.get("kept") \
                and self._state.runtime.can_drive(session.tool_name):
            # Continue in SWE: resume it under a keeper. An explicit
            # mode still asks for that channel by name, and a kept tmux
            # pane is taken back as it is.
            self._continue_in_swe(key, row, session, body)
            return
        if mode == "headless" and session.tool_name != "claude":
            self._send_error(
                422, "bad_mode", "headless attach only exists for claude"
            )
            return
        if mode == "serve" and session.tool_name != "opencode":
            self._send_error(
                422, "bad_mode", "serve attach only exists for opencode"
            )
            return
        if mode == "appserver" and session.tool_name != "codex":
            self._send_error(
                422, "bad_mode", "appserver attach only exists for codex"
            )
            return
        if mode == "acp":
            from swe.remote import acp

            if acp.command_for(session.tool_name) is None:
                self._send_error(
                    422, "bad_mode",
                    f"{session.tool_name} has no Agent Client Protocol "
                    "mode installed",
                )
                return
        if str(row.get("status") or "") == "active" and \
                key not in supervise.kept(self._state.home):
            # Busy beats environment: on a box with no tmux the truest
            # answer for a mid-turn session is still not_idle. A kept
            # tmux session is adopted as it is, so mid-turn is fine.
            self._send_error(
                409, "not_idle",
                "session is mid-turn; try again when it is idle",
            )
            return
        try:
            model = protocol._launch_value(body, "model")
            effort = protocol._launch_value(body, "effort")
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        resolved = supervise.resolve_mode(session, mode, model=model,
                                          effort=effort)
        if resolved == "tmux" and supervise.tmux_bin() is None:
            # Environment failure, not a state conflict: 409/not_idle
            # would tell the client to retry, which never helps.
            self._send_error(
                422, "not_attachable", "tmux is not available"
            )
            return
        blocked = supervise.channel_tuning_error(
            resolved, session.tool_name, model, effort)
        if blocked:
            self._send_error(409, "unsupported_launch_option", blocked)
            return
        ok, note = supervise.attach(
            self._state.home, key, session,
            last_append_at=self._state.tail_manager.last_append_at(key),
            status=str(row.get("status") or ""),
            mode=mode, model=model, effort=effort,
        )
        if not ok:
            self._send_error(409, "not_idle", note)
            return
        self._state.tail_manager.wake(key)
        row["ownership"] = protocol.OWNERSHIP_ATTACHED
        self._emit_ownership(key)
        self._send_json(200, {"ok": True, "note": note})

    def _continue_in_swe(self, key: str, row: dict, session,
                         body: dict) -> None:
        """Attach a terminal-started session by resuming it under a
        keeper. Refused while anything else may still write to it: a
        turn in flight, a transcript that moved a moment ago, or a
        harness process that still has the session open."""
        state = self._state
        try:
            model = protocol._launch_value(body, "model")
            effort = protocol._launch_value(body, "effort")
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        if not session.session_id or getattr(session, "mirrored", False):
            self._send_error(422, "not_attachable",
                             "this session has no id to resume it by")
            return
        moving = "transcript is still moving; try again when it is idle"
        if str(row.get("status") or "") == "active":
            self._send_error(409, "not_idle",
                             "session is mid-turn; try again when it is idle")
            return
        last = state.tail_manager.last_append_at(key)
        if last is not None and time.time() - last < supervise.ATTACH_IDLE_S:
            self._send_error(409, "not_idle", moving)
            return
        transcript = tail.resolve_jsonl_path(session)
        try:
            if transcript is not None and time.time() - \
                    transcript.stat().st_mtime < supervise.ATTACH_IDLE_S:
                self._send_error(409, "not_idle", moving)
                return
        except OSError:
            pass
        writers = supervise._local_writer_pids(session)
        if writers:
            pids = ", ".join(str(pid) for pid in writers)
            self._send_error(409, "session_in_use", (
                f"{session.tool_name} still has this session open in a "
                f"terminal (pid {pids}); quit it there, then continue "
                "here"))
            return
        from swe.sessions.resume import account_env

        try:
            note = state.runtime.continue_session(
                key, session, model=model or "", effort=effort or "",
                env=account_env(session))
        except runtime_mod.Refused as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        state.catalog.patch(key, {
            "ownership": protocol.OWNERSHIP_ATTACHED,
            "mode": runtime_mod.MODE, "control": "full",
        }, str(row.get("status") or ""))
        self._emit_ownership(key)
        state._catalog_wake.set()
        self._send_json(200, {"ok": True, "note": note})

    def _mark_read(self, device_id: str, key: str, row: dict) -> None:
        """``POST /v1/sessions/{key}/read {at?}``: this device's
        last-read cursor (``readstate.py``). ``at`` defaults to now, and
        any ``at`` is held to the later of now and the row's own
        ``last_active_at``: a satellite clock a little ahead can't leave
        a just-opened chat unread, and a client can't park a cursor in
        the far future."""
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        at = body.get("at")
        if not readstate.valid_at(at):
            self._send_error(422, "bad_read", "at must be a number")
            return
        ceiling = max(time.time(), readstate.num(row.get("last_active_at")))
        at = ceiling if at is None else min(float(at), ceiling)
        try:
            stored = readstate.mark_read(
                self._state.home, device_id, key, at,
                (str(r.get("key") or "") for r in self._state.catalog.rows()),
            )
        except OSError:
            self._send_error(500, "store_failed", "could not save the read")
            return
        self._send_json(200, {"ok": True, "at": stored})

    def _session_meta(self, key: str, action: str) -> None:
        """``POST /v1/sessions/{key}/pin|title``: kept on the hub for
        every session, satellite ones too (``sessionmeta.py``)."""
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        try:
            sessionmeta.update(self._state.home, key, action, body)
        except ValueError as exc:
            self._send_error(422, "bad_command", str(exc))
            return
        except sessionmeta.TooManyEntries as exc:
            self._send_error(409, "too_many_pins", str(exc))
            return
        except OSError:
            self._send_error(500, "store_failed", "could not save the change")
            return
        row = self._state.catalog.reapply_meta(key) or {}
        data = sessionmeta.fields(row)
        self._state.emit(
            key,
            protocol.event("session.meta", key, self._state.machine_id, data),
        )
        self._send_json(200, {"ok": True, **data})

    def _emit_ownership(self, key: str) -> None:
        row = self._state.catalog.row_for(key) or {}
        # This emit follows the registry write, but the catalog row can
        # still hold the old supervised fields until the next refresh, so
        # they come from the registries themselves. A remote row has no
        # local registry: its fields ride the satellite's rows.
        meta = supervise.meta_for(self._state.home, key)
        krow = meta is None and supervise.kept(self._state.home).get(key)
        if self._state.runtime.owns(key):
            tmux, mode, kept = None, runtime_mod.MODE, False
        elif meta is not None:
            tmux, mode, kept = meta.get("tmux"), meta.get("mode"), False
        elif krow:
            tmux, mode, kept = krow.get("tmux"), krow.get("mode"), True
        elif row.get("remote"):
            tmux = row.get("tmux")
            mode = row.get("mode")
            kept = bool(row.get("kept"))
        else:
            tmux = mode = None
            kept = False
        self._state.emit(key, _ownership_event(
            key, self._state.machine_id, row.get("ownership"),
            tmux, mode, kept))

    def _read_upload(self):
        """Returns ``(name, data)`` or sends the error and returns None."""
        if not self._unread:  # None: unframed, which the hub doesn't decode
            self._send_error(400, "bad_body", "empty upload")
            return None
        data = self._take_body(attachments.MAX_BYTES)
        if data is None:
            self._send_error(413, "too_large", "attachment exceeds 20 MB")
            return None
        ctype = self.headers.get("Content-Type") or ""
        if ctype.startswith("multipart/form-data"):
            name, data = _parse_multipart(ctype, data)
            if data is None:
                self._send_error(
                    400, "bad_body", "unreadable multipart upload"
                )
                return None
            return name, data
        from urllib.parse import parse_qs, urlparse

        args = parse_qs(urlparse(self.path).query)
        name = (args.get("name") or [self.headers.get("X-Filename") or ""])[0]
        return name, data

    def _stage_attachment(self, key: str) -> None:
        upload = self._read_upload()
        if upload is None:
            return
        name, data = upload
        payload, err = attachments.stage(self._state.home, key, name, data)
        if payload is None:
            self._send_error(422, "bad_attachment", err or "stage failed")
            return
        self._send_json(200, payload)

    def _session_row(self, device_id: str | None, key: str) -> None:
        """``GET /v1/sessions?key=``: one catalog row, in the shape the
        whole list has, with only its own machine (#1556). The web chat
        reads a row on open and after each turn; serialising every row
        for that was most of the cost."""
        # The same freshness wait as the whole list after low power.
        self._state.fresh_catalog()
        row = self._state.catalog.row_for(key)
        if row is None:
            self._send_error(404, "unknown_session", "no such session")
            return
        machine_id = row.get("machine_id")
        machines = catalog_mod.peek_machines(self._state.home)
        self._send_json(200, {
            "sessions": readstate.annotate(self._state.home, device_id, [row]),
            "machines": [m for mid, m in machines.items() if mid == machine_id],
            "server_time": time.time(),
        })

    def _slash_commands(self, key: str) -> None:
        """``GET /v1/sessions/{key}/commands``: the "/" menu (``slash.py``)."""
        row = self._state.catalog.row_for(key)
        if row is None:
            self._send_error(404, "unknown_session", "no such session")
            return
        if row.get("remote"):
            body = slash.for_remote_row(self._state.home, key, row)
        else:
            body = slash.for_local_session(
                self._state.home, key, row,
                self._state.catalog.session_for(key),
            )
        self._send_json(200, body)

    def _messages(self, key: str, args) -> None:
        before = _int_arg(args.get("before"), 0)
        limit = _int_arg(args.get("limit"), 500)
        limit = max(1, min(limit, 2000))
        # One past the page tells whether an older page exists, without
        # reading the rest of the journal.
        v1 = self._state.runtime.v1
        messages = self._state.journal.read_tail(
            key, limit + 1, before_seq=before,
            keep=lambda ev: _served_message(v1(key, ev)),
        )
        has_more = len(messages) > limit
        tail_events = messages[-limit:]
        self._send_json(200, {
            "session": key,
            "messages": tail_events,
            "next_before": tail_events[0]["seq"]
            if has_more and tail_events else None,
            "has_more": has_more,
        })

    def _overview_machines(self) -> list[dict]:
        """Each satellite's daily token rows for the Overview (#525):
        ``{"id", "name", "reported_at", "days"}``, only for satellites
        whose agent sends them."""
        machines = catalog_mod.load_machines(self._state.home)
        out = []
        for record in usage_mod.load_satellites(self._state.home):
            mid = record["machine_id"]
            daily = record.get("daily_usage")
            if mid == self._state.machine_id or not isinstance(daily, dict):
                continue
            out.append({
                "id": mid,
                "name": (machines.get(mid) or {}).get("name") or mid,
                # When these daily rows were reported, which is older than
                # the limits when the last reports came without them.
                "reported_at": daily.get("reported_at", record.get("reported_at")),
                "days": daily.get("days") or [],
            })
        return out

    def _usage_machines(self, own: dict) -> list[dict]:
        """``/v1/usage``'s per-machine list: the hub, then each satellite.

        Every entry carries ``reported_at`` (the reporting machine's
        clock when it took the snapshot) and ``received_at`` (the hub's
        clock when it got it); for the hub both are its ``server_time``.
        """
        now = own.get("server_time") or time.time()
        machines = catalog_mod.load_machines(self._state.home)
        out = [{
            "machine": self._state.machine,
            "harnesses": own.get("harnesses") or [],
            "window_days": own.get("window_days"),
            "reported_at": now,
            "received_at": now,
            "hub": True,
        }]
        for record in usage_mod.load_satellites(self._state.home):
            mid = record["machine_id"]
            if mid == self._state.machine_id:
                continue
            out.append({
                "machine": machines.get(mid) or {"id": mid, "name": mid},
                "harnesses": record["harnesses"],
                "window_days": record.get("window_days"),
                "reported_at": record.get("reported_at"),
                "received_at": record.get("received_at"),
                "hub": False,
            })
        return out

    # -- static ------------------------------------------------------------

    def _serve_static(self, path: str, query: str = "") -> None:
        """The web app (``swe/webapp/static``) at the root: ``/`` is its
        page and each asset a flat name beside it (``/app.js``,
        ``/harness-claude.svg``). Every non-API path lands here, and only
        a flat name of a known type is served, so no request can climb
        out of the folder or shadow a ``/v1/`` route. The pages fetch
        ``/v1/app/*`` with the device credential the app's ``#pair=``
        handler pairs, which this origin shares. ``/session/`` is the
        React session view's own folder (``_serve_session``).
        """
        # The app lived under ``/app/`` until #944, and pair links minted
        # then (``/app/#pair=<token>``) are still out there: send them,
        # and a page cached with ``/app/…`` asset paths, to the same name
        # at the root. The browser keeps the fragment across a redirect;
        # the query string is copied onto ``Location`` here.
        suffix = f"?{query}" if query else ""
        if path == "/session":
            self._redirect("/session/" + suffix)
            return
        if path.startswith("/session/"):
            self._serve_session(path[len("/session/"):])
            return
        if path == "/app" or path.startswith("/app/"):
            rest = path[len("/app/"):]
            # Only a name this handler would serve: anything else (say
            # ``/app//evil.example``) would make ``Location`` another host.
            if rest and not self._is_webapp_name(rest):
                self._send_error(404, "not_found", "unknown route")
                return
            self._redirect("/" + rest + suffix)
            return
        if path == "/index.html":
            self._redirect("/" + suffix)
            return
        name = path[1:] or "index.html"
        if name in _BUNDLES:
            body, etag = webapp_pages.bundled(name[len("pages"):])
            if webapp_pages.etag_matches(
                    self.headers.get("If-None-Match"), etag):
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            self._send_body(
                body,
                "text/javascript; charset=utf-8" if name.endswith(".js")
                else "text/css; charset=utf-8",
                etag=etag,
            )
            return
        if not webapp_static.is_asset_name(name):
            self._send_error(404, "not_found", "unknown route")
            return
        self._send_asset(webapp_static.STATIC_DIR / name)

    def _serve_session(self, rel: str) -> None:
        """The session view (app/web-session's committed build in
        ``static/session/``) at ``/session/``, public like ``/``: its page
        and scripts hold nothing private, and what it reads comes from
        ``/v1`` with the device credential. ``rel`` is the raw path after
        the prefix, never unquoted, and only a name the build lists in
        ``build-info.json`` is served (``webapp_static.session_file``). The
        page and the stable entry names revalidate by ETag; the hashed
        chunks under ``assets/`` are cached for a year."""
        target = webapp_static.session_file(rel)
        if target is None:
            if rel in ("", "index.html") and not webapp_static.session_built():
                body = webapp_static.SESSION_MISSING
                self.send_response(503)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'self'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self._send_error(404, "not_found", "unknown route")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._send_error(404, "not_found", "missing asset")
            return
        immutable = webapp_static.session_immutable(rel)
        etag = None if immutable else webapp_static.etag_for(body)
        if etag and webapp_pages.etag_matches(self.headers.get("If-None-Match"), etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy", webapp_static.SESSION_CSP)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", webapp_static.content_type(target))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", webapp_static.SESSION_IMMUTABLE if immutable else "no-cache")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Security-Policy", webapp_static.SESSION_CSP)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _is_webapp_name(name: str) -> bool:
        return name in _BUNDLES or webapp_static.is_asset_name(name)

    def _redirect(self, location: str) -> None:
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- paired device self-service --------------------------------------

    def _device_me(self, device_id: str) -> None:
        """``GET /v1/devices/me``: the caller's own pairing record, so a
        client can show (and rename) itself. The owner's unpaired tailnet
        device has no devices.json row — a stub with its node name."""
        if device_id.startswith(devices_mod.TAILNET_PREFIX):
            self._send_json(200, {
                "id": device_id,
                "name": "This device",
                "node_name": self._peer_node_name(),
                "owner": True,
            })
            return
        row = pairing.load_devices(self._state.home).get(device_id)
        if row is None:
            self._send_error(404, "unknown_device", "no such device")
            return
        self._send_json(200, pairing.device_view(device_id, row))

    def _device_rename(self, device_id: str) -> None:
        """``POST /v1/devices/rename`` ``{name}``: the caller sets its own
        ``display_name``; an empty name clears it back to the paired one.
        Owner tailnet devices have no row to rename — same answer as the
        SSH key route."""
        if device_id.startswith(devices_mod.TAILNET_PREFIX):
            self._send_error(
                409, "needs_pairing",
                "pair this device to rename it",
            )
            return
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        name = pairing.clean_name(body.get("name"))
        if not pairing.update_device(
            self._state.home, device_id, display_name=name or None
        ):
            self._send_error(404, "unknown_device", "no such device")
            return
        row = pairing.load_devices(self._state.home).get(device_id) or {}
        self._send_json(200, {
            "ok": True,
            "device_id": device_id,
            "name": str(row.get("display_name") or row.get("name") or ""),
        })

    def _device_revoke(self, device_id: str) -> None:
        """``POST /v1/devices/revoke`` ``{device_id, confirm_this_device?}``:
        sign one paired device out — the web Settings action's refusal
        set, shared in ``devices.py``."""
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        try:
            self._send_json(
                200, devices_mod.revoke(self._state.home, device_id, body))
        except devices_mod.Refused as exc:
            self._send_error(exc.status, exc.code, exc.message)

    def _ssh_key(self, device_id: str) -> None:
        """``POST /v1/ssh/key`` ``{public_key}``: store this device's SSH
        key ("" clears it) and bring ``authorized_keys`` in line."""
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        raw = body.get("public_key")
        key = None
        if raw != "":
            key = sshkeys.normalize_public_key(raw)
            if key is None:
                self._send_error(
                    422, "bad_key",
                    "public_key must be one ed25519 or ecdsa OpenSSH key",
                )
                return
        try:
            sshkeys.set_device_key(self._state.home, device_id, key)
        except KeyError:
            # The owner's own tailnet devices have no devices.json row
            # to keep a key on.
            self._send_error(
                409, "needs_pairing",
                "pair this device to give it an SSH key",
            )
            return
        except OSError as exc:
            self._send_error(500, "store_failed",
                             f"could not update authorized_keys: {exc}")
            return
        self._send_json(200, sshkeys.view(self._state.home, device_id))

    # -- web app page plugins (swe/webapp/pages) ---------------------------

    def _page_context(self, device_id: str | None):
        return webapp_pages.PageContext(
            home=self._state.home, state=self._state, device_id=device_id
        )

    def _page_call(self, fn, *call_args) -> None:
        try:
            body = fn(*call_args)
        except webapp_pages.PageError as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._send_error(503, "page_unavailable", str(exc))
            return
        except Exception as exc:  # a page bug: answer, don't drop the socket
            traceback.print_exc()
            self._send_error(500, "page_failed", type(exc).__name__)
            return
        self._send_json(200, body)

    def _page_get(self, name: str, args, device_id: str | None) -> None:
        page = webapp_pages.load(name)
        if page is None or not hasattr(page, "get"):
            self._send_error(404, "not_found", "unknown page")
            return
        flat = {k: str(v[0]) for k, v in args.items() if v}
        self._page_call(page.get, self._page_context(device_id), flat)

    def _page_post(self, rest: str, device_id: str | None) -> None:
        name, _sep, action = rest.partition("/")
        page = webapp_pages.load(name)
        if page is None or not hasattr(page, "post") or not action:
            self._send_error(404, "not_found", "unknown page action")
            return
        if self._bad_origin():
            self._send_error(403, "bad_origin", "request blocked by origin policy")
            return
        body = self._read_body()
        if body is None:
            self._send_error(400, "bad_body", "unreadable JSON body")
            return
        self._page_call(page.post, self._page_context(device_id), action, body)

    def _send_body(self, body: bytes, ctype: str,
                   etag: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_asset(self, target: Path) -> None:
        try:
            body = target.read_bytes()
        except OSError:
            self._send_error(404, "not_found", "missing asset")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".webmanifest": "application/manifest+json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _parse_multipart(ctype: str, body: bytes) -> tuple[str, bytes | None]:
    """Minimal single-file multipart reader: returns (filename, bytes)."""
    try:
        # Parse the Content-Type header properly: `boundary=` may be
        # followed by more params (`; charset=utf-8`) or quoted — taking
        # everything after it would corrupt the delimiter marker.
        msg = email.message.Message()
        msg["content-type"] = ctype
        boundary = msg.get_param("boundary", header="content-type")
        # RFC 2231 continuation params come back as tuples — not a
        # boundary we can build a marker from.
        if not isinstance(boundary, str) or not boundary:
            return "", None
        marker = ("--" + boundary).encode()
        for part in body.split(marker):
            if b"Content-Disposition" not in part:
                continue
            head, _, blob = part.partition(b"\r\n\r\n")
            if not blob:
                continue
            # The CRLF before the closing boundary is separator, not
            # content — strip exactly that pair, never content bytes.
            if blob.endswith(b"\r\n"):
                blob = blob[:-2]
            name = ""
            for line in head.decode("utf-8", "replace").splitlines():
                if "content-disposition" not in line.lower():
                    continue
                for field in line.split(";"):
                    field = field.strip()
                    if field.startswith("filename="):
                        name = field.split("=", 1)[1].strip().strip('"')
            return name, blob
    except Exception:
        pass
    return "", None


def _served_message(ev: dict) -> dict | None:
    """A ``message.appended`` journal event as ``/messages`` serves it
    (cleaned), or None for any other event or one cleaning drops."""
    if ev is None or ev.get("type") != "message.appended":
        return None
    return injected_mod.clean_event(ev)


def _int_arg(values, default: int) -> int:
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return default


def _opt_int_arg(values) -> int | None:
    """An integer query argument, or None when it is absent or not a
    number (a v2 stream with no readable ``epoch`` gets a snapshot)."""
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


class _BoundedHTTPServer(ThreadingHTTPServer):
    """Thread-per-connection, but capped — slowloris peers get refused
    at the socket instead of spawning unbounded handler threads."""

    daemon_threads = True

    # A browser opening the web app fires its script fetches at once,
    # and behind ``tailscale serve`` each stream becomes a separate
    # backend connection in the same instant. The default backlog of 5
    # lets the kernel reset the overflow before accept() — the proxy
    # answers 502, and a lost app.js leaves a blank page.
    request_queue_size = min(socket.SOMAXCONN, 128)

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(_MAX_WORKERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            # All workers busy: refuse the connection outright.
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(
    state: HubState, host: str, port: int
) -> ThreadingHTTPServer:
    server = _BoundedHTTPServer((host, port), _Handler)
    server.state = state  # type: ignore[attr-defined]
    return server


# Bind values that listen on every interface rather than one address.
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})


def _host_allowed(state: HubState, host: str) -> bool:
    """Whether a request's Host names this hub: anything while
    ``allowed_hosts`` is empty (a state ``configure`` never filled), a
    listed name, or, on a wildcard bind, an IP literal on its port."""
    allowed = state.allowed_hosts
    if not allowed or host in allowed:
        return True
    port = state.allowed_ip_port
    if port is None:
        return False
    name, sep, tail = host.rpartition(":")
    if not sep or tail != str(port):
        return False
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _advertise_url(host: str, port: int) -> str:
    """The base URL handed to phones/ntfy — never a wildcard bind."""
    advertised = host
    if host in WILDCARD_HOSTS:
        advertised = (
            catalog_mod.tailscale_ip()
            or socket.gethostname()
            or "127.0.0.1"
        )
    return f"http://{advertised}:{port}"


def check_public_url(url: str) -> str:
    """``url`` reduced to scheme and host[:port], no trailing slash.

    Raises ``ValueError`` unless it is an absolute http(s) URL.
    """
    parts = urlsplit((url or "").strip())
    try:
        # Reading .port validates it: non-numeric or out of range raises.
        bad = (parts.scheme not in {"http", "https"} or not parts.hostname
               or parts.port == 0)
    except ValueError:
        bad = True
    if bad:
        raise ValueError(
            "--public-url must be an http(s) URL, e.g. "
            "https://box.tailnet.ts.net:8477"
        )
    return f"{parts.scheme}://{parts.netloc}"


def _public_hosts(public_url: str) -> set[str]:
    """Host header values that name ``public_url``. A browser drops the
    scheme's default port, so 443/80 are allowed with and without it."""
    parts = urlsplit(public_url)
    default = 443 if parts.scheme == "https" else 80
    port = parts.port or default
    hosts = {f"{parts.hostname}:{port}"}
    if port == default:
        hosts.add(parts.hostname or "")
    return hosts


def _loopback_hosts(port: int) -> set[str]:
    """Host values a client on this machine uses for a loopback bind.
    A browser drops the http default port, so 80 is allowed bare."""
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if port == 80:
        hosts |= {"127.0.0.1", "localhost", "[::1]"}
    return hosts


def _hostport(name: str, port: int) -> str:
    """``name:port`` spelled the way a Host header carries it — a v6
    literal needs brackets to be told apart from the port."""
    return f"[{name}]:{port}" if ":" in name else f"{name}:{port}"


def _hub_file(home: Path | None) -> Path:
    return remote_dir_for(home) / "hub.json"


def save_hub_config(home: Path | None, host: str, port: int,
                    public_url: str) -> None:
    """Record where the running hub listens and how phones reach it, so
    ``swe remote pair`` and ``status`` find it without flags."""
    path = _hub_file(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({
            "host": host, "port": port, "public_url": public_url,
        }, indent=1))
    except OSError:
        pass


def load_hub_config(home: Path | None = None) -> dict:
    """``{"host", "port", "public_url"}`` from the last ``serve``, or {}."""
    return read_json_dict(_hub_file(home))


def _log_pair_approval(res: dict, how: str) -> None:
    """One line per pair approval on the hub's stderr (``serve.log``
    under the LaunchAgent), so a code approval and an owner
    auto-approval leave the same trail."""
    def field(key: str, missing: str) -> str:
        # The name comes from the requester: escape anything unprintable
        # (newlines, terminal escapes) so it can't forge or hide lines.
        text = str(res.get(key) or missing)
        return "".join(
            ch if ch.isprintable() else f"\\x{ord(ch):02x}"
            for ch in text
        )

    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{stamp} pair: {how} {field('name', 'device')} "
            f"(node {field('node_name', '?')}, "
            f"login {field('login', '?')}) "
            f"-> device {field('device_id', '?')}",
            file=sys.stderr, flush=True,
        )
    except Exception:
        pass


def configure(state: HubState, host: str, port: int,
              public_url: str = "") -> None:
    """Set the advertised URL, proxy trust and owner identity on
    ``state`` for a hub listening on ``host:port``.

    ``public_url`` is the address phones use when something fronts the
    hub; ``tailscale serve`` on a loopback bind is the supported case
    (``https://box.tailnet.ts.net:8477``), and only then are its
    forwarded-client headers trusted.
    """
    # The listen address and the advertised URL are different things:
    # ntfy action buttons need a dialable address — the public URL,
    # else the tailnet IP, then the machine hostname. (The CLI refuses
    # a wildcard bind without --unsafe-bind-any; _advertise_url still
    # turns one into something reachable.)
    state.base_url = public_url or _advertise_url(host, port)
    state.allowed_ip_port = None
    # Owner identity for pair-request approval: the tailnet login that
    # owns this node. A plain loopback bind and tagged nodes get none;
    # behind serve the node is looked up by its own tailnet IP.
    if public_url:
        state.allowed_hosts = _public_hosts(public_url)
        if host in {"127.0.0.1", "::1", "localhost"}:
            # The loopback bind still answers local clients directly
            # (swe remote status, a browser on this machine), so its
            # own Host names stay allowed next to the public URL's.
            state.allowed_hosts |= _loopback_hosts(port)
            state.trust_proxy = True

            def lookup() -> bool:
                """True once settled: an owner found, or a tagged node
                that never has one. False means ask again later."""
                ip = catalog_mod.tailscale_ip()
                peer = state.whois(ip) if ip else None
                if peer is None:
                    return False
                state.own_node_id = str(peer.get("node_id") or "")
                if not peer.get("tagged"):
                    state.owner_login = str(peer.get("login") or "")
                return True

            if not lookup():
                state.owner_retry = lookup
            return
    elif host in {"127.0.0.1", "::1", "localhost"}:
        # A loopback bind is where a DNS-rebinding page lands, so the
        # unauthenticated routes answer only the loopback names a
        # local client would use.
        state.allowed_hosts = _loopback_hosts(port)
        return
    if host not in {"127.0.0.1", "::1", "localhost"}:
        # A wildcard bind ("--unsafe-bind-any") has no address of its
        # own to look up or allow; it answers every name this node has.
        wildcard = host in WILDCARD_HOSTS
        peer = None if wildcard else state.whois(host)
        own_ip = catalog_mod.tailscale_ip()
        if peer is None and own_ip and own_ip != host:
            # whois wants an IP; a bind by MagicDNS name is looked up
            # through the node's own address.
            peer = state.whois(own_ip)
        if not public_url:
            # Every spelling this node answers to — the bind value,
            # the tailnet IP and the MagicDNS names — so a pairing
            # link reaches the hub whichever one it names (`swe
            # remote pair` defaults the phone address to the tailnet
            # IP). The names belong to the node even when it is
            # tagged; a tag only means there is no owner login.
            if wildcard:
                # Loopback, and the hostname _advertise_url falls back
                # to off the tailnet, next to the tailnet names below;
                # a LAN address dialed directly passes as an IP literal.
                state.allowed_ip_port = port
                state.allowed_hosts = _loopback_hosts(port)
                if socket.gethostname():
                    state.allowed_hosts.add(
                        _hostport(socket.gethostname(), port))
            else:
                state.allowed_hosts = {_hostport(host, port)}
            if own_ip:
                state.allowed_hosts.add(_hostport(own_ip, port))
            if peer:
                for name in (peer.get("node_name"),
                             peer.get("node_short")):
                    if name:
                        state.allowed_hosts.add(_hostport(name, port))
        if peer:
            state.own_node_id = str(peer.get("node_id") or "")
        if peer and not peer.get("tagged"):
            state.owner_login = str(peer.get("login") or "")


def proxy_lookup_warning(state: HubState) -> str:
    """The start-up warning for a hub that trusts ``tailscale serve``'s
    forwarded client but whose socket-owner lookup is blind on this host
    (#1944), else "". Probes only when the proxy check matters
    (``trust_proxy``, set by ``configure``) on a platform that has one,
    and changes nothing the check accepts: a blind hub keeps failing
    closed, and this only says so."""
    if not state.trust_proxy:
        return ""
    if sys.platform == "darwin":
        # An ad-hoc-signed parent blinds its children too, so the shell
        # that starts the hub counts (Nix's bash is ad-hoc signed).
        fix = ("run the hub with a Developer-ID-signed Python, such as "
               "python.org's, started by launchd or a signed shell")
    elif sys.platform.startswith("linux"):
        fix = "run the hub where it can read /proc/net/tcp"
    else:
        return ""
    try:
        works = bool(state.proxy_lookup_works())
    except Exception:  # noqa: BLE001 - a probe that fails is blind
        works = False
    if works:
        return ""
    return (
        "this hub cannot see which process owns a loopback connection, "
        "so requests through tailscale serve lose their tailnet identity "
        "(the owner's devices need a paired credential and pair requests "
        f"need a pair token); {fix}"
    )


def serve(state: HubState, host: str, port: int,
          public_url: str = "") -> int:
    """Blocking entry point for ``swe remote serve``."""
    configure(state, host, port, public_url)
    # GitHub notifications ride the catalog's refresh cadence into
    # /v1/inbox and the ntfy fan-out. Wired here, not in HubState, so a
    # state built for tests or the satellite agent never reaches for
    # ``gh``.
    state.catalog.fetch_github = state._fetch_github_rows
    state.catalog.on_github_alert = state._github_alert
    # Bind first: a second serve on a taken port must fail before
    # start() registers this machine, reconciles kept sessions, retires
    # ntfy credentials and starts loops that would fight the running hub.
    server = make_server(state, host, port)
    # Said once, after the bind, so a second serve that fails to bind
    # stays quiet: the hub still serves, without tailnet identity.
    warning = proxy_lookup_warning(state)
    if warning:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  warning: {warning}",
              file=sys.stderr, flush=True)
    state.start()
    # Auto clean quits orphans without a browser ever opening Compute:
    # its switch is hub state, so the background sampler starts at boot
    # while it is on rather than wait for a page request.
    from swe.webapp import compute as webapp_compute
    try:
        # A settings read that fails means off: it can never keep the
        # hub from serving.
        auto_clean = webapp_compute.supported() \
            and webapp_compute.clean_settings(state.home)["enabled"]
    except (OSError, ValueError, TypeError, AttributeError):
        auto_clean = False
    if auto_clean:
        webapp_compute.model().home = state.home or Path.home()
        webapp_compute.start_ticker()
    # Only a hub that owns the port may say where it is: a second copy
    # that fails to bind must not overwrite the running one's address.
    save_hub_config(state.home, host, port, public_url)
    # SIGTERM (launchd/systemd stop) must take the same shutdown path as
    # ^C — without it the process dies mid-write and queued sends drop
    # silently. shutdown() can't run on the serve_forever thread.
    def _on_sigterm(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    prev_sigterm = None
    if threading.current_thread() is threading.main_thread():
        prev_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
        server.server_close()
        state.stop()
    return 0
