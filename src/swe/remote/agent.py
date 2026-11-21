"""Satellite agent — ``swe remote agent``.

Same tail/inject machinery as the hub, no listener: a persistent
outbound connection to the hub over Tailscale. The agent registers its
machine metadata, pushes catalog snapshots and tailed transcript events
upstream, and long-polls a per-machine command journal downstream — the
command SSE stream doubles as the presence signal, so a sleeping MacBook
simply drops offline on the hub until it wakes and replays the queue.

Since G15 every machine also stands alone (docs/specs/
G15-satellite-keepers.md). Three more things live here:

- **The satellite's own hub state** (``RemoteAgent.local``): the same
  ``HubState`` the hub serves from, over this machine's sessions. It
  runs this machine's session keepers through ``SessionRuntime`` and
  keeps a journal of every session here, so the machine can be driven
  with the hub away.
- **The loopback API** (``LocalApi``): the hub's own ``/v1`` session
  routes on 127.0.0.1, for a caller that holds the token in
  ``data/remote/local.json``. The hub machine binds one too.
- **The hub's side of satellite keepers** (``SatelliteKeepers``): the
  hub's journal for a session a satellite keeper holds is a copy of
  the satellite's, event for event under the keeper's seq, and a
  command for a sleeping machine is held in the store for 24 hours.
"""

import collections
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no flock, and no agent service to guard
    fcntl = None

from swe.paths import atomic_write_text, remote_dir_for
from swe.remote import attachments, catalog as catalog_mod, slash
from swe.remote import inject, journal, protocol, sendq, supervise, tail
from swe.remote import fork as fork_mod, usage as usage_mod
from swe.remote import stream as stream_mod
from swe.remote.detach import PendingDetaches

logger = logging.getLogger("swe.remote.agent")

AGENT_FILE = "agent.json"          # satellite's stored hub credential
PUSH_BATCH = 50                    # most events per /v1/agent/events POST
RECONNECT_S = 5.0
CATALOG_PUSH_S = 10.0
# The hub refuses JSON bodies over 4 MiB (hub._MAX_BODY); a catalog push
# stays under it with room to spare.
CATALOG_BODY_MAX = (4 << 20) - (64 << 10)
# An events POST is held to the same size: a batch is cut by encoded
# bytes as well as count, and an event too big to go alone is clipped.
EVENTS_BODY_MAX = CATALOG_BODY_MAX
# Commands that run on the session's queue, in order, off the stream.
_SESSION_QUEUED_OPS = frozenset({"message", "attachment"})
OUTBOX_KEY = "_outbox"             # journal key for unsent uplink events
# Same as the hub's: a ``queue``-mode send polls a fresh status on this
# cadence and fails honestly once a turn outlives the cap.
QUEUE_POLL_S = protocol.IDLE_POLL_S
QUEUE_WAIT_MAX_S = protocol.IDLE_WAIT_MAX_S
# How long a queue-mode send that reached idle waits for an attachment
# pull it names to finish downloading.
PULL_WAIT_MAX_S = 300
# The in-memory uplink buffer keeps no more than the outbox journal's
# ring does across a restart; while the hub is down the oldest go first.
OUTBOX_MAX = journal.RING_MAX


# Live deltas waiting for the delta uplink; past this the oldest go.
DELTA_QUEUE_MAX = 500
LOCK_FILE = "agent.lock"           # held by the one running agent
# The keeper sessions whose journal the hub copies, and which of them
# this machine has let go since (the hub is told once their last
# events are up).
KEPT_FILE = "agent-kept.json"
# How long the agent leaves a hub alone that does not take keeper
# journals (an older hub, or one with its session runtime off).
KEPT_RETRY_S = 60.0
# How old a ``kept`` command may be when this machine reads it: the
# hub's 24 hour hold for a message or an answer, ten minutes for
# anything else (see ``RemoteAgent._kept_refusal``). Measured against
# the hub's clock, so the short one leaves room for two clocks that
# disagree by minutes.
KEPT_HOLD_S = 24 * 3600.0
KEPT_FRESH_S = 600.0
# How long the outbox waits for the hub to release a session this
# machine let go. A hub that still has not taken the release by then
# (it keeps failing on that session) no longer holds every other
# session's events back.
KEPT_RELEASE_WAIT_S = 60.0


def take_lock(home: Path | None = None):
    """``(handle, holder)``: while ``handle`` stays open this process is
    the only ``swe remote agent`` on this ~/.swe. When another agent
    holds the lock, ``handle`` is None and ``holder`` is its pid (0 when
    unreadable). Without ``fcntl`` (Windows) there is no lock to take.
    """
    path = remote_dir_for(home) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    if fcntl is None:
        return fh, None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        text = fh.read().strip()
        fh.close()
        return None, int(text) if text.isdigit() else 0
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh, None


def lock_holder(home: Path | None = None) -> int | None:
    """The pid of the agent running on this ~/.swe (0 when it can't
    be read), or None when none runs."""
    fh, holder = take_lock(home)
    if fh is not None:
        fh.close()
    return holder


def agent_file_for(home: Path | None) -> Path:
    return remote_dir_for(home) / AGENT_FILE


def load_agent(home: Path | None = None) -> dict | None:
    try:
        return json.loads(agent_file_for(home).read_text())
    except (OSError, ValueError):
        return None


def save_agent(home: Path | None, conf: dict) -> Path:
    path = agent_file_for(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(conf, indent=1), private=True)
    # private=True floors a new file at 0600 but keeps an existing mode,
    # so a restored or hand-written file must be tightened on every save.
    os.chmod(path, 0o600)
    return path


# Set once an https hub's certificate fails against Python's default
# store: python.org builds on macOS ship no CA certificates, so a hub
# behind ``tailscale serve`` (a real Let's Encrypt cert) is unverifiable
# until the OS bundle is loaded. Verification is never turned off.
_fallback_ctx: "ssl.SSLContext | None" = None


def _urlopen(req: urllib.request.Request, timeout: float | None):
    """``urlopen`` that retries a certificate failure once against the
    OS CA bundle, then keeps using that bundle for every later call."""
    global _fallback_ctx
    if _fallback_ctx is not None:
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=_fallback_ctx)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as exc:
        if not isinstance(getattr(exc, "reason", None),
                          ssl.SSLCertVerificationError):
            raise
        from swe.harness.rate_limits import _verified_context

        ctx = _verified_context()
        if ctx is None:
            raise
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        _fallback_ctx = ctx
        return resp


def _encoded_len(event: dict) -> int:
    return len(json.dumps(event, ensure_ascii=False).encode("utf-8"))


def _clip_strings(value, cap: int):
    if isinstance(value, str):
        return value if len(value) <= cap else value[:cap] + "… (truncated)"
    if isinstance(value, dict):
        return {k: _clip_strings(v, cap) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_strings(v, cap) for v in value]
    return value


def _clip_event(event: dict, limit: int) -> dict | None:
    """``event`` with its long strings cut until it encodes within
    ``limit`` bytes, or None when even 1 KiB strings don't fit it."""
    cap = limit // 2
    while cap >= 1024:
        clipped = _clip_strings(event, cap)
        if _encoded_len(clipped) <= limit:
            return clipped
        cap //= 4
    return None


def _headers(credential: str, ctype: str | None = "application/json") -> dict:
    h = {"Authorization": f"Bearer {credential}"}
    if ctype:
        h["Content-Type"] = ctype
    return h


def _post(hub: str, path: str, credential: str, payload: dict,
          timeout: float = 10.0) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        hub.rstrip("/") + path, data=body,
        headers=_headers(credential), method="POST",
    )
    try:
        with _urlopen(req, timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        with exc:  # it holds the response: closed here, not by the GC
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except Exception:
                return exc.code, {}
    except Exception:
        return 0, {}


def _get_bytes(hub: str, path: str, credential: str,
               timeout: float = 30.0) -> bytes | None:
    req = urllib.request.Request(
        hub.rstrip("/") + path, headers=_headers(credential, None),
    )
    try:
        with _urlopen(req, timeout) as resp:
            return resp.read()
    except Exception:
        return None


# -- the loopback API ------------------------------------------------------
#
# Every machine binds one (G1 section 3, "Standing alone"): the hub's
# own /v1 session routes on 127.0.0.1, over that machine's HubState, for
# a caller that proves it can read this account's files by presenting
# the token in data/remote/local.json. The Mac app drives the sessions
# of the machine it runs on through it, with no network.

LOCAL_FILE = "local.json"          # the loopback API's token and address
LOCAL_PORT = 8476                  # one below the hub's DEFAULT_PORT
LOCAL_HOST = "127.0.0.1"
# What a loopback caller is recorded as (commands, read state).
LOCAL_DEVICE = "local"
# The header ``RemoteAgent`` names the sending device in when it passes
# a hub command on to this machine's own routes. Read only from a
# caller that already presented the token.
DEVICE_HEADER = "X-SWE-Device"
_DEVICE_RE = re.compile(r"[A-Za-z0-9_.:@-]{1,128}")
_TOKEN_BYTES = 32
# The /v1 routes the loopback API answers besides /v1/sessions and
# everything under it. Pairing, devices, SSH keys, the web app's pages
# (/v1/app, which read API keys) and the satellite routes are a hub's
# business with other machines and are not served here.
_LOCAL_GETS = frozenset({"/v1/status", "/v1/events", "/v1/machines",
                         "/v1/folders", "/v1/inbox"})


def local_file_for(home: Path | None = None) -> Path:
    return remote_dir_for(home) / LOCAL_FILE


def load_local(home: Path | None = None) -> dict | None:
    """``local.json`` as it is on disk, or None when there is none."""
    try:
        data = json.loads(local_file_for(home).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_local(home: Path | None, conf: dict) -> Path:
    path = local_file_for(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(conf, indent=1), private=True)
    # As for agent.json: an existing file keeps its mode through the
    # atomic write, so it is tightened on every save.
    os.chmod(path, 0o600)
    return path


def local_token(home: Path | None = None) -> str:
    """The loopback API's bearer token, made on first use and kept for
    good in ``local.json`` (mode 0600). Never printed, logged or
    returned by a route: a caller reads it from the file, which is the
    proof that it runs as this account."""
    conf = load_local(home) or {}
    token = conf.get("token")
    if isinstance(token, str) and len(token) >= _TOKEN_BYTES:
        path = local_file_for(home)
        try:
            if path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass
        return token
    token = secrets.token_hex(_TOKEN_BYTES)
    _save_local(home, {**conf, "token": token})
    return token


def local_port_for(home: Path | None = None) -> int:
    """The port this machine's loopback API binds: ``remote.local_port``
    in config.json, else ``LOCAL_PORT``. 0 asks for any free port (the
    one bound is recorded in local.json)."""
    from swe.configuration import load_config
    from swe.paths import config_file_for

    try:
        section = load_config(config_file_for(home)).get("remote")
    except Exception:  # an unreadable config is the setting left alone
        section = None
    value = section.get("local_port") if isinstance(section, dict) else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) \
            and 0 <= value <= 65535:
        return value
    return LOCAL_PORT


def _local_route_ok(method: str, path: str) -> bool:
    """Whether the loopback API serves ``method path``."""
    if not path.startswith("/v1/"):
        # The session view's own files; they are public on a hub too.
        return method == "GET"
    if path == "/v1/sessions" or path.startswith("/v1/sessions/"):
        return True
    if method == "GET":
        return path in _LOCAL_GETS or path.startswith("/v1/launches/")
    return False


_local_classes: dict = {}
_local_classes_lock = threading.Lock()


def _local_handler():
    """The hub's request handler, held to the loopback API's rules. Made
    on first use: ``hub`` imports this module, so it cannot be imported
    while this one loads."""
    with _local_classes_lock:
        cls = _local_classes.get("handler")
        if cls is not None:
            return cls
        from swe.remote import hub as hub_mod

        class LocalHandler(hub_mod._Handler):
            """``hub._Handler`` with one caller: whoever holds the
            token in local.json. Every route body is the hub's."""

            def _gate(self, method: str) -> bool:
                # A page that rebound its domain to 127.0.0.1 arrives
                # under its own name, never a loopback one.
                host = (self.headers.get("Host") or "").strip().lower()
                if host not in self.server.local_hosts:
                    self._send_error(403, "bad_origin",
                                     "request blocked by origin policy")
                    return False
                if not _local_route_ok(method, urlparse(self.path).path):
                    self._send_error(404, "not_found", (
                        "not served on the local API"))
                    return False
                return True

            def do_GET(self) -> None:
                if self._gate("GET"):
                    super().do_GET()

            def do_POST(self) -> None:
                if self._gate("POST"):
                    super().do_POST()

            def do_DELETE(self) -> None:
                if self._gate("DELETE"):
                    super().do_DELETE()

            def _local_host_ok(self) -> bool:
                return True  # ``_gate`` checked the Host already

            def _caller(self, path: str) -> str | None:
                auth = self.headers.get("Authorization") or ""
                given = auth[7:].strip() if auth.startswith("Bearer ") else ""
                want = self.server.local_token
                if not given or not want or not hmac.compare_digest(
                        given.encode("utf-8"), want.encode("utf-8")):
                    return None
                self._state.note_client()
                device = self.headers.get(DEVICE_HEADER) or ""
                return device if _DEVICE_RE.fullmatch(device) \
                    else LOCAL_DEVICE

            def _scoped_approval_device(self, path: str) -> str | None:
                return None  # no notification tokens are minted here

        _local_classes["handler"] = LocalHandler
        return LocalHandler


class LocalApi:
    """One machine's loopback API over ``state`` (a ``HubState``).

    ``start`` binds 127.0.0.1 and records the address in local.json;
    ``call`` runs one request through the same handler with no socket
    bound at all, which is how the satellite agent passes a hub command
    on to this machine's own routes.
    """

    def __init__(self, state, home: Path | str | None = None) -> None:
        self.state = state
        self.home = Path(home) if home is not None else None
        self._token: str | None = None
        self.local_hosts = {LOCAL_HOST, "localhost", "[::1]"}
        self.server = None
        self.thread: threading.Thread | None = None
        self.port = 0

    @property
    def local_token(self) -> str:
        """The token, read (or made) the first time it is needed."""
        if self._token is None:
            self._token = local_token(self.home)
        return self._token

    def start(self, port: int = LOCAL_PORT) -> int:
        """Bind and serve; returns the port. Raises OSError when the
        port is taken (another swe on this machine already serves it)."""
        from swe.remote import hub as hub_mod

        token = self.local_token
        server = hub_mod._BoundedHTTPServer((LOCAL_HOST, port),
                                            _local_handler())
        self.port = server.server_address[1]
        server.state = self.state  # type: ignore[attr-defined]
        server.local_token = token  # type: ignore[attr-defined]
        server.local_hosts = self.local_hosts | {  # type: ignore
            f"{name}:{self.port}" for name in self.local_hosts}
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.25},
            name="swe-local-api", daemon=True)
        self.thread.start()
        try:
            _save_local(self.home, {
                **(load_local(self.home) or {}), "token": token,
                "url": f"http://{LOCAL_HOST}:{self.port}",
                "port": self.port, "pid": os.getpid()})
        except OSError:
            logger.warning("could not record the local API's address")
        return self.port

    def close(self) -> None:
        server, self.server = self.server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def call(self, method: str, path: str, body: dict | None = None, *,
             device: str | None = None, timeout: float = 120.0
             ) -> tuple[int, dict]:
        """One request to this machine's own routes, in this process:
        ``(status, JSON body)``, or ``(0, {})`` when it could not be
        made. The request crosses a socket pair, not a port, so it
        works whether or not ``start`` could bind."""
        raw = b"" if body is None else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        head = [f"{method} {path} HTTP/1.1", f"Host: {LOCAL_HOST}",
                f"Authorization: Bearer {self.local_token}",
                "Connection: close", f"Content-Length: {len(raw)}"]
        if raw:
            head.append("Content-Type: application/json")
        if device and _DEVICE_RE.fullmatch(device):
            head.append(f"{DEVICE_HEADER}: {device}")
        ours, theirs = socket.socketpair()
        handler = _local_handler()

        def answer() -> None:
            try:
                handler(theirs, (LOCAL_HOST, 0), self)
            except Exception:
                logger.exception("local call %s %s", method, path)
            finally:
                try:
                    theirs.close()
                except OSError:
                    pass

        thread = threading.Thread(target=answer, name="swe-local-call",
                                  daemon=True)
        thread.start()
        try:
            ours.settimeout(timeout)
            ours.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1")
                         + raw)
            resp = http.client.HTTPResponse(ours, method=method)
            resp.begin()
            status, data = resp.status, resp.read()
        except (OSError, http.client.HTTPException):
            return 0, {}
        finally:
            try:
                ours.close()
            except OSError:
                pass
            thread.join(timeout)
        try:
            payload = json.loads(data.decode("utf-8")) if data else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return status, payload if isinstance(payload, dict) else {}


def serve_local(state, home: Path | str | None = None,
                port: int | None = None) -> LocalApi | None:
    """Start ``state``'s loopback API (on the configured port unless
    ``port`` is given), or None, with a note on stderr, when the port
    could not be bound. The hub keeps serving either way."""
    home = Path(home) if home is not None else None
    if port is None:
        port = local_port_for(home)
    api = LocalApi(state, home)
    try:
        api.start(port)
    except OSError as exc:
        print(f"swe remote: the local API is off (127.0.0.1:{port}: {exc})",
              file=sys.stderr, flush=True)
        return None
    return api


def _local_hub():
    """``HubState`` for a satellite: the same state the hub serves from,
    over this machine's sessions, with everything it emits also sent up
    to the hub this machine is paired with. Made on first use, like
    ``_local_handler``."""
    with _local_classes_lock:
        cls = _local_classes.get("hub")
        if cls is not None:
            return cls
        from swe.remote import hub as hub_mod

        class LocalHub(hub_mod.HubState):
            # No satellite reports to a satellite: ``SatelliteKeepers``
            # holds nothing here and starts no timer.
            hosts_satellites = False

            def __init__(self, agent: "RemoteAgent",
                         home: Path | None, runtime: dict | None) -> None:
                self.agent = agent
                super().__init__(home, runtime=runtime)

            def record(self, key: str, event: dict) -> dict:
                """Journal and publish ``event`` on this machine only."""
                return super().emit(key, event)

            def emit(self, key: str, event: dict) -> dict:
                written = self.record(key, event)
                self.agent._enqueue(event)
                return written

            def publish_transient(self, key: str, event: dict) -> None:
                super().publish_transient(key, event)
                self.agent._queue_delta(key, event)

            def settle_launch(self, launch_id: str, **fields) -> bool:
                if fields.get("state") == "failed":
                    self.agent._launch_failed(
                        launch_id, str(fields.get("note") or ""))
                return super().settle_launch(launch_id, **fields)

        _local_classes["hub"] = LocalHub
        return LocalHub


def pair_agent(hub: str, token: str, name: str,
               home: Path | None = None) -> dict | None:
    """Exchange a one-time pair token for this satellite's credential.

    The claimed ``role`` lands on the device row; the hub binds it to
    this machine at first ``/v1/agent/register`` and refuses the agent
    routes to anything else.
    """
    status, payload = _post(
        hub, "/v1/pair", "",  # no auth needed for /v1/pair
        {"token": token, "name": name or "satellite",
         "role": "satellite"},
    )
    if status != 200 or not payload.get("credential"):
        return None
    conf = {
        "hub": hub.rstrip("/"),
        "credential": payload["credential"],
        "device_id": payload.get("device_id"),
    }
    save_agent(home, conf)
    return conf


class RemoteAgent:
    """The satellite daemon: tails local sessions, executes hub commands."""

    def __init__(
        self,
        hub: str,
        credential: str,
        home: Path | str | None = None,
        *,
        runtime: dict | None = None,
    ) -> None:
        """``runtime`` goes to this machine's session runtime, as
        ``HubState(home, runtime=...)`` passes it (tests give a spawner
        and a registry)."""
        self.home = Path(home) if home is not None else None
        self.hub = hub.rstrip("/")
        self.cred = credential
        self.machine = catalog_mod.local_machine(self.home)
        self.machine_id = self.machine["id"]
        # Last "/" menu payload the hub accepted; see _push_catalog.
        self._slash_digest: str | None = None
        self._slash_skipped: str | None = None
        # Row count last trimmed to fit a push, so the note prints once.
        self._rows_trimmed: int | None = None
        # Set after a printed 403 register refusal, so the loop retries
        # without re-printing; cleared on the next accepted register.
        self._register_denied: str | None = None
        self.outbox: "list[dict]" = []
        self._outbox_lock = threading.Lock()
        # Live ``message.delta`` events: coalesced here, sent best-effort
        # on their own uplink and never written to the outbox journal.
        # Only a hub whose register reply says ``message_delta`` gets
        # them; an older hub would journal every chunk.
        self.deltas = stream_mod.DeltaStream(self._queue_delta)
        self._delta_out: "collections.deque[dict]" = collections.deque(
            maxlen=DELTA_QUEUE_MAX)
        self._delta_wake = threading.Event()
        self._hub_deltas = False
        self._stop = threading.Event()
        # Commands dispatch on the stream thread except ``message``,
        # which can run minutes (headless claude) or wait out a turn
        # (``queue`` mode), and attachment pulls: those go through
        # ``_sendq``, one worker per session like the hub's, so a queued
        # interrupt/detach isn't stuck behind a run and one session's
        # turn doesn't hold every other session's messages.
        self._sendq = sendq.SendQueue()
        # Attachment pulls not finished yet, by (session, ref): a
        # queue-mode send waits on its own lane, so it can't count on
        # the session lane's order to find its file staged.
        self._pulls: dict[tuple[str, str], threading.Event] = {}
        self._pulls_lock = threading.Lock()
        # ``detach`` with ``mode: when_idle``, waiting out a turn here.
        self.detaches = PendingDetaches(self._stop)
        # Fresh per-session status for those waits; tests stub it.
        from swe.sessions.status import session_statuses
        self.status_of = session_statuses
        # Two cursors: ``_last_cmd_seq`` dedups frames seen on the wire;
        # ``_done_cmd_seq`` is the persisted *completed* floor the
        # ``?last=`` replay starts from — a crash mid-dispatch replays
        # the command instead of skipping it. ``_done_set`` is the
        # persisted out-of-order completions above that floor, so a
        # replayed command that already finished is skipped, not run
        # twice.
        self._last_cmd_seq, self._done_set = self._load_cmd_cursor()
        self._done_cmd_seq = self._last_cmd_seq
        # The stream thread, the work loop and fork threads all finish
        # commands; the cursor advance must not interleave.
        self._done_lock = threading.Lock()
        # Bumped when the hub's command journal starts over under our
        # cursor (``_reset_cmd_cursor``); see ``_mark_done``.
        self._cmd_epoch = 0
        # The ``?last=`` the current command stream sent.
        self._stream_cursor = self._done_cmd_seq
        # Requeue uplink events the hub never acknowledged — the
        # transcript cursor is saved on read, so an in-memory buffer
        # would lose them across a restart.
        self._outbox_ack = self._load_outbox_ack()
        self._outbox = journal.Journal(self.home)
        events, _truncated = self._outbox.read_since(
            OUTBOX_KEY, self._outbox_ack)
        self.outbox = list(events)[-OUTBOX_MAX:]
        # Set to send the next upload now instead of on the half-second
        # tick: a keeper logged an event, or the hub just came back.
        self._kick = threading.Event()
        # Set to push the catalog now: a launch just got its session.
        self._catalog_kick = threading.Event()
        # Keeper sessions whose journal the hub copies: key ->
        # {"acked": the last seq the hub said its copy holds (None
        # until it has said), "released": this machine let it go}.
        self._kept_lock = threading.Lock()
        self._kept: dict[str, dict] = {
            key: {"acked": None, "released": bool(released),
                  "since": time.monotonic()}
            for key, released in self._load_kept().items()}
        # Set when the command stream comes up: the next upload opens
        # with "this machine is back", and the hub answers by
        # delivering what it held for it.
        self._kept_hello = False
        # The port ``run`` binds the loopback API on; None binds none
        # (``serve`` sets it; a test that calls ``run`` binds nothing).
        self.local_port: int | None = None
        self._kept_off_until = 0.0
        # Launches the hub asked for that went to a keeper: a failure
        # of one is reported upstream.
        self._hub_launches: set[str] = set()
        # This machine's own hub state (G15): the journal of every
        # session here, the session runtime that runs the keepers, and
        # what the loopback API serves. Built last: opening the runtime
        # reconnects to live keepers, whose events come up through the
        # fields above at once. Its catalog and tailer are the agent's:
        # one of each per machine.
        options = dict(runtime or {})
        options["listeners"] = [*(options.get("listeners") or []),
                                self._on_kept_event]
        self.local = _local_hub()(self, self.home, options)
        self.local.status_of = lambda sessions: self.status_of(sessions)
        self.catalog = self.local.catalog
        # Same status re-probe as the hub: the satellite's tailer emits
        # session.status itself, and its catalog push carries the
        # fresher of the two.
        self.tail_manager = self.local.tail_manager
        self.tail_manager.emit = self._capture
        self.tail_manager._on_status = self._on_probed_status
        # One journal object for this machine's files: a sweep through
        # either name then forgets the same cached seqs.
        self._outbox = self.local.journal
        self.api = LocalApi(self.local, self.home)

    @property
    def runtime(self):
        """This machine's session runtime (``SessionRuntime``)."""
        return self.local.runtime

    def _on_probed_status(self, key: str, status: str, at: float) -> None:
        """Same as the hub's: patch the catalog row, and let a pending
        ``when_idle`` detach act once the turn is over (one the hub
        asked for, or one asked for on this machine's own API)."""
        self.catalog.note_status(key, status, at)
        if status != "active":
            self.detaches.nudge(key)
            self.local.detaches.nudge(key)

    def _cmd_cursor_file(self) -> Path:
        return agent_file_for(self.home).parent / "agent-cursor.json"

    def _load_cmd_cursor(self) -> tuple[int, set[int]]:
        """(Last *completed* seq, out-of-order done seqs); zeroed for a
        new or re-paired hub."""
        path = self._cmd_cursor_file()
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data.get("hub") == self.hub:
                done = {
                    s for s in (data.get("done") or [])
                    if isinstance(s, int) and not isinstance(s, bool)
                }
                return int(data.get("seq") or 0), done
            return 0, set()  # paired to a different hub — replay its queue
        except (OSError, ValueError):
            pass
        # Pre-migration cursor was a bare int for whatever hub was
        # paired then; honor it once rather than replay the backlog.
        legacy = path.with_name("agent-cursor")
        try:
            return int(legacy.read_text().strip()), set()
        except (OSError, ValueError):
            return 0, set()

    def _save_cmd_cursor(self) -> None:
        path = self._cmd_cursor_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: a crash mid-write left a torn file that loaded as
            # no cursor, so the whole backlog replayed and re-ran.
            atomic_write_text(path, json.dumps({
                "hub": self.hub,
                "seq": self._done_cmd_seq,
                # Done seqs above the contiguous floor — without them a
                # restart replays and re-runs a command that already
                # finished. Every entry sits above the floor by
                # construction (_mark_done drops them as it climbs),
                # and the hub's backlog cap bounds how many can pile
                # up while one seq below them still runs — evicting
                # any would re-run that command, so the whole set
                # persists.
                "done": sorted(self._done_set),
            }))
        except OSError:
            pass

    def _outbox_ack_file(self) -> Path:
        return agent_file_for(self.home).parent / "agent-outbox-ack"

    def _load_outbox_ack(self) -> int:
        try:
            return int(self._outbox_ack_file().read_text().strip())
        except (OSError, ValueError):
            return 0

    def _save_outbox_ack(self) -> None:
        try:
            # Atomic: a torn file reads as ack 0 and resends the outbox.
            atomic_write_text(self._outbox_ack_file(), str(self._outbox_ack))
        except OSError:
            pass

    def _mark_done(self, seq: int | None, epoch: int | None = None) -> None:
        """Advance the completed cursor to the highest contiguous seq.

        ``epoch`` is ``_cmd_epoch`` when the command arrived: a command
        from before a journal reset finishing late must not mark the
        new journal's seq of the same number done.
        """
        if not isinstance(seq, int):
            return
        with self._done_lock:
            if epoch is not None and epoch != self._cmd_epoch:
                return
            self._done_set.add(seq)
            while self._done_cmd_seq + 1 in self._done_set:
                self._done_cmd_seq += 1
                self._done_set.discard(self._done_cmd_seq)
            self._save_cmd_cursor()

    # -- upstream ----------------------------------------------------------

    def _capture(self, key: str, event: dict) -> dict:
        """TailManager's emit: into this machine's own journal and
        streams (the loopback API reads them), then onto the uplink."""
        if key:
            try:
                self.local.record(key, event)
            except Exception:
                logger.exception("journaling %s for %s",
                                 event.get("type"), key)
        return self._enqueue(event)

    def _enqueue(self, event: dict) -> dict:
        """Buffer ``event`` for the uplink thread.

        Events are journaled before they're buffered — the transcript
        cursor is committed on read, so anything unacked must survive a
        restart or transcript output is lost while the hub is down.
        """
        stamped = self._outbox.append(OUTBOX_KEY, event)
        with self._outbox_lock:
            # Buffer the stamped copy: the seq is what the ack file
            # records once the hub accepts the batch.
            self.outbox.append(stamped or event)
            self._trim_outbox()
        if event.get("type") == "session.launched":
            # The hub shows a launch as started once it has the row.
            self._catalog_kick.set()
        return stamped or event

    def _trim_outbox(self) -> None:
        """Drop the oldest buffered events past ``OUTBOX_MAX``; the
        caller holds ``_outbox_lock``. The next accepted batch acks past
        them, so they are gone for good, as the ring would drop them."""
        over = len(self.outbox) - OUTBOX_MAX
        if over > 0:
            del self.outbox[:over]

    def _take_batch(self) -> tuple[list[dict], list[dict]]:
        """Pop the next POST off the outbox: (events taken, events to
        send). At most ``PUSH_BATCH`` events and ``EVENTS_BODY_MAX``
        encoded bytes go at once. An event over that alone goes
        clipped, or not at all when clipping can't fit it, so one huge
        tool result can't get a 400 on every retry and wedge the
        uplink."""
        taken: list[dict] = []
        batch: list[dict] = []
        size = 0
        with self._outbox_lock:
            for orig in self.outbox:
                if len(batch) >= PUSH_BATCH:
                    break
                ev, n = orig, _encoded_len(orig) + 1
                if n > EVENTS_BODY_MAX:
                    ev = _clip_event(orig, EVENTS_BODY_MAX - 1)
                    if ev is None:
                        taken.append(orig)  # dropped, acked with the rest
                        continue
                    n = _encoded_len(ev) + 1
                if batch and size + n > EVENTS_BODY_MAX:
                    break
                taken.append(orig)
                batch.append(ev)
                size += n
            del self.outbox[:len(taken)]
        return taken, batch

    def _push_events(self) -> bool:
        taken, batch = self._take_batch()
        if not taken:
            return True
        if batch:
            status, _payload = _post(
                self.hub, "/v1/agent/events", self.cred,
                {"machine_id": self.machine_id, "events": batch},
            )
            if status != 200:
                with self._outbox_lock:
                    self.outbox[:0] = taken
                    self._trim_outbox()
                return False
        seqs = [ev.get("seq") for ev in taken]
        seq = max((s for s in seqs if isinstance(s, int)), default=None)
        if seq is not None and seq > self._outbox_ack:
            self._outbox_ack = seq
            self._save_outbox_ack()
        return True

    # -- keeper sessions: the hub's copy of their journals ---------------

    def _kept_file(self) -> Path:
        return agent_file_for(self.home).parent / KEPT_FILE

    def _load_kept(self) -> dict[str, bool]:
        try:
            data = json.loads(self._kept_file().read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("hub") != self.hub:
            return {}  # paired to another hub: it copies nothing yet
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            return {}
        return {k: bool(v) for k, v in sessions.items() if isinstance(k, str)}

    def _save_kept(self) -> None:
        with self._kept_lock:
            sessions = {k: bool(v["released"]) for k, v in self._kept.items()}
        try:
            atomic_write_text(self._kept_file(), json.dumps(
                {"hub": self.hub, "sessions": sessions}))
        except OSError:
            pass

    def _on_kept_event(self, key: str, event: dict) -> None:
        """The session runtime journaled a keeper event: send it now."""
        self._kick.set()

    def _sync_kept(self) -> dict[str, dict]:
        """Bring the copied set in line with what the runtime holds:
        a session it took is copied from here on, one it let go is
        marked released. Returns the runtime's rows by key."""
        owned = {row["key"]: row for row in self.local.runtime.rows()}
        changed = False
        with self._kept_lock:
            for key in owned:
                entry = self._kept.get(key)
                if entry is None or entry["released"]:
                    self._kept[key] = {"acked": None, "released": False,
                                       "since": time.monotonic()}
                    changed = True
            for key, entry in self._kept.items():
                if key not in owned and not entry["released"]:
                    entry["released"] = True
                    entry["since"] = time.monotonic()
                    changed = True
        if changed:
            self._save_kept()
        return owned

    def _kept_entry(self, key: str, state: dict, owned: dict,
                    budget: int, hello: bool) -> tuple[dict | None, bool]:
        """What the next upload says about one session: ``(entry,
        more)``. None when there is nothing to say; ``more`` when its
        journal holds events past what fits this upload."""
        journal_ = self.local.journal
        head = journal_.last_seq(key)
        if state["released"]:
            # What this machine journaled after it let the session go
            # (its tailer's events) goes up the ordinary way, and the
            # hub numbers it: the copy ends at the keeper's last event.
            last = journal_.read_tail(
                key, 1, keep=lambda e: e if e.get("v") == 2 else None)
            head = last[0]["seq"] if last else 0
        entry: dict = {"key": key, "head": head}
        acked = state["acked"]
        if acked is None:
            # Not known yet how far the hub's copy reaches: ask, and
            # say what the session is in case this is its first event.
            row = owned.get(key) or {}
            entry["row"] = {
                "harness": row.get("harness") or key.split(":", 1)[0],
                "harness_session_id": row.get("harness_session_id") or "",
                "cwd": row.get("cwd") or "",
                "adapter": row.get("adapter") or "",
            }
            if state["released"]:
                # Said with the question too: a hub that already took
                # this release must not start the copy over.
                entry["released"] = True
            else:
                # The requests still open here. A hub that restarted
                # knows open requests only from events it published
                # before, and the upload resumes after those.
                session = self.local.runtime.session(key)
                entry["pending"] = {
                    rid: float(event.get("ts") or time.time())
                    for rid, event in list(
                        (session.pending if session else {}).items())}
            return entry, False
        batch: list[dict] = []
        if acked < head:
            events, _truncated = journal_.read_since(
                key, acked, limit=PUSH_BATCH)
            size = 0
            for event in events:
                if event["seq"] > head:
                    break
                n = _encoded_len(event) + 2
                if size + n > budget:
                    if batch or budget < EVENTS_BODY_MAX:
                        # It goes in the next upload, where it may
                        # have a whole body to itself.
                        break
                    # One event too big for any body: cut its long
                    # strings. It keeps its seq, so nothing after it
                    # is renumbered. One that cannot be cut to fit is
                    # sent as its seq alone: the hub refuses that and
                    # moves on, where otherwise the copy would stop
                    # here for good.
                    event = _clip_event(event, budget - 2) or {
                        "v": event.get("v"), "seq": event["seq"],
                        "type": "journal.skipped"}
                    n = _encoded_len(event) + 2
                batch.append(event)
                size += n
            entry["events"] = batch
        sent = batch[-1]["seq"] if batch else acked
        more = sent < head
        if state["released"] and not more:
            entry["released"] = True
        elif not batch and acked <= head and not hello:
            return None, more
        return entry, more

    def _push_kept(self) -> bool:
        """One upload of what the hub's copies lack. False when the
        rest of the uplink must wait: the hub could not be reached, or
        a session this machine let go is not released on the hub yet.
        Until it is, the hub numbers nothing in that session's journal,
        so the tailer's events for it must not arrive first."""
        owned = self._sync_kept()
        with self._kept_lock:
            states = {k: dict(v) for k, v in self._kept.items()}
            hello, self._kept_hello = self._kept_hello, False
        if not states and not hello:
            return True
        if time.monotonic() < self._kept_off_until:
            self._again_hello(hello)
            return True
        sessions: list[dict] = []
        budget = EVENTS_BODY_MAX
        again = False
        for key, state in states.items():
            entry, more = self._kept_entry(key, state, owned, budget, hello)
            again = again or more or state["acked"] is None
            if entry is None:
                continue
            sessions.append(entry)
            budget -= _encoded_len(entry)
            if budget <= 0:
                again = True
                break
        if not sessions and not hello:
            return self._kept_settled()
        status, reply = _post(
            self.hub, "/v1/agent/kept", self.cred,
            {"machine_id": self.machine_id, "hello": hello,
             "sessions": sessions})
        if status != 200:
            self._again_hello(hello)
            if status in (0, 401) or status >= 500:
                return False
            # An older hub (404) or one whose session runtime is off
            # (409): it copies no keeper journals, so there is nothing
            # to wait for. Ask again later.
            self._kept_off_until = time.monotonic() + KEPT_RETRY_S
            return True
        acked = reply.get("acked") if isinstance(reply, dict) else None
        acked = acked if isinstance(acked, dict) else {}
        gone = reply.get("released") if isinstance(reply, dict) else None
        gone = set(gone) if isinstance(gone, list) else set()
        changed = moved = False
        with self._kept_lock:
            for key, seq in acked.items():
                entry = self._kept.get(key)
                if entry is None or not isinstance(seq, int) \
                        or isinstance(seq, bool):
                    continue
                if key in gone and entry["released"]:
                    del self._kept[key]
                    changed = moved = True
                else:
                    moved = moved or entry["acked"] != seq
                    entry["acked"] = seq
        if changed:
            self._save_kept()
        if again and moved:
            # More to send, and the hub is taking it: go again now. A
            # hub that took nothing is asked again on the next tick.
            self._kick.set()
        return self._kept_settled()

    def _again_hello(self, hello: bool) -> None:
        """An upload that said "this machine is back" did not go: the
        next one says it."""
        if hello:
            with self._kept_lock:
                self._kept_hello = True

    def _kept_settled(self) -> bool:
        """Whether no session this machine let go still waits to be
        released on the hub (one that has waited past
        ``KEPT_RELEASE_WAIT_S`` no longer counts)."""
        fresh = time.monotonic() - KEPT_RELEASE_WAIT_S
        with self._kept_lock:
            return not any(e["released"] and e["since"] > fresh
                           for e in self._kept.values())

    def _report_kept(self, command_id: str, via: str, status: int,
                     body: dict) -> bool:
        """Tell the hub what this machine's route answered for one of
        its commands; tried until it is told or the agent stops.
        False when the agent stopped first: the command stays undone
        here, so the hub's replay runs it again (the store answers it
        as the duplicate it is) and the answer goes up then."""
        payload = {"machine_id": self.machine_id, "results": [{
            "command_id": command_id, "via": via, "status": status,
            "body": body}]}
        while not self._stop.is_set():
            got, _reply = _post(self.hub, "/v1/agent/kept", self.cred,
                                payload)
            if got == 200 or (got not in (0, 401) and got < 500):
                return True
            if self._stop.wait(RECONNECT_S):
                break
        return False

    def _launch_failed(self, launch_id: str, note: str) -> None:
        """A keeper launch the hub asked for did not start: say so, as
        a failed tmux launch does."""
        if launch_id not in self._hub_launches:
            return
        self._hub_launches.discard(launch_id)
        self._buffer_result(protocol.event(
            "command.result", "", self.machine_id, {
                "command_id": "", "op": "launch", "ok": False,
                "note": note, "degraded": False, "launch_id": launch_id}))

    def _queue_delta(self, key: str, event: dict) -> None:
        """DeltaStream's sink: hand a delta to the delta uplink."""
        if not self._hub_deltas:
            return
        self._delta_out.append(event)
        self._delta_wake.set()

    def _push_deltas(self) -> None:
        """POST what is queued, once; a failed POST drops it (the
        final ``message.appended`` still goes through the outbox)."""
        batch: list[dict] = []
        while self._delta_out and len(batch) < PUSH_BATCH:
            batch.append(self._delta_out.popleft())
        if batch:
            _post(self.hub, "/v1/agent/events", self.cred,
                  {"machine_id": self.machine_id, "events": batch})

    def _delta_loop(self) -> None:
        while not self._stop.is_set():
            self._delta_wake.wait(1.0)
            self._delta_wake.clear()
            try:
                while self._delta_out and not self._stop.is_set():
                    self._push_deltas()
            except Exception:
                pass

    def _push_catalog(self) -> bool:
        # Launch adoption runs here too: `launch` marks a pending file
        # and the new session must claim its tmux pane before the push
        # reports it, or it reaches the hub still `observed` — writable
        # nowhere.
        rows = self.catalog.refresh()
        adopted = supervise.adopt_pending(self.home, self.catalog.tail_rows())
        if adopted:
            rows = self.catalog.refresh()
        for key in adopted:
            # A launch or fork starts with its first turn: its tailer
            # (created on the next sync) starts hot and status-probed.
            self.tail_manager.wake(key)
            # Names the key a phone's launch became; the hub maps it to
            # the hub-scoped key and answers GET /v1/launches with it.
            lid = (supervise.meta_for(self.home, key) or {}).get("launch_id")
            if lid:
                self._capture(key, protocol.event(
                    "session.launched", key, self.machine_id,
                    {"launch_id": lid},
                ))
        body = {"machine_id": self.machine_id,
                "rows": self._fit_rows(rows)}
        # The "/" menu's custom commands live on this machine's disk; send
        # them only when they changed, so the 10 s push stays small.
        try:
            commands = slash.satellite_payload(rows, self.home)
        except Exception:
            commands = None
        digest = slash.payload_digest(commands) if commands else None
        if digest and digest != self._slash_digest:
            body["slash"] = commands
            # Never let the menu cost the rows: a body the hub would
            # refuse goes without it.
            if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) \
                    > CATALOG_BODY_MAX:
                del body["slash"]
                if digest != self._slash_skipped:
                    self._slash_skipped = digest
                    print("swe remote agent: slash command list too large "
                          "for the catalog push; sending rows without it",
                          file=sys.stderr)
        status, reply = _post(self.hub, "/v1/agent/catalog", self.cred, body)
        # An older hub ignores the field and doesn't echo it: keep sending
        # until one says it stored the list.
        if status == 200 and "slash" in body and \
                isinstance(reply, dict) and reply.get("slash"):
            self._slash_digest = digest
        if self._refresh_launchable():
            self._register()
            self._note_machine()
        return status == 200

    def _fit_rows(self, rows: list[dict]) -> list[dict]:
        """The newest rows whose push fits under ``CATALOG_BODY_MAX``.

        A machine with thousands of sessions can list more than the hub
        takes in one body; refused whole, the push failed every 10 s
        and the hub never heard of a new session. Rows come newest
        first, so the oldest are the ones left out.
        """
        base = len(json.dumps({"machine_id": self.machine_id, "rows": []},
                              ensure_ascii=False).encode("utf-8"))
        total = base
        for n, row in enumerate(rows):
            # _post's json.dumps puts ", " between rows.
            total += len(json.dumps(row, ensure_ascii=False)
                         .encode("utf-8")) + (2 if n else 0)
            if total > CATALOG_BODY_MAX:
                if self._rows_trimmed != len(rows):
                    self._rows_trimmed = len(rows)
                    print(f"swe remote agent: {len(rows)} sessions are too "
                          f"many for one catalog push; sending the newest "
                          f"{n}", file=sys.stderr)
                return rows[:n]
        self._rows_trimmed = None
        return rows

    def _push_usage(self) -> bool:
        body = usage_mod.snapshot()
        # Daily token totals for the hub's Overview machine filter; a
        # ledger that can't be read sends the limits without them.
        daily = usage_mod.daily_usage(self.home)
        if daily is not None:
            body["daily_usage"] = daily
        status, _ = _post(
            self.hub, "/v1/agent/usage", self.cred,
            {"machine_id": self.machine_id, **body},
        )
        return status == 200

    def _note_machine(self) -> None:
        """Save this machine's own row on this machine, as a hub does
        for itself when it starts: the loopback API's ``/v1/machines``
        and ``/v1/sessions`` list it, and the new-session picker reads
        what it can launch from there."""
        try:
            catalog_mod.upsert_machine(self.home, {
                **self.machine, "online": True,
                "last_seen_at": time.time()})
        except Exception:
            logger.exception("saving this machine's row")

    def _refresh_launchable(self) -> bool:
        """Re-read what this machine can launch; True when it changed, so
        a Provider visibility change reaches the hub without a restart."""
        launchable = catalog_mod.launchable_harnesses()
        fields = catalog_mod.launch_fields(launchable, self.home)
        if all(self.machine.get(k) == v for k, v in fields.items()):
            return False
        self.machine.update(fields)
        return True

    def _register(self) -> bool:
        status, reply = _post(
            self.hub, "/v1/agent/register", self.cred,
            {"machine": self.machine},
        )
        if status == 403 and self._register_denied is None:
            # A permanent refusal — pre-roles satellites pair as phones
            # and the binding gate rejects them forever, so explain
            # once instead of letting the retry loop look flaky.
            # The hub answers {"error": {"code", "message"}}.
            err = reply.get("error") if isinstance(reply, dict) else None
            detail = str(err.get("message") or "") \
                if isinstance(err, dict) else ""
            self._register_denied = detail or "forbidden"
            print(
                "swe remote agent: the hub refused this credential "
                f"(403 {self._register_denied}); re-pair this machine: "
                "run `swe remote pair` on the hub, then "
                "`swe remote agent --hub=<url> --token=<token>` here",
                file=sys.stderr,
            )
        if status == 200:
            self._register_denied = None
            self._hub_deltas = isinstance(reply, dict) and \
                reply.get("message_delta") is True
        return status == 200

    # -- downlink ----------------------------------------------------------

    def _command_stream(self) -> None:
        """GET /v1/agent/commands as an SSE stream; dispatch each command."""
        # The cursor this connection asks from: a journal.truncated
        # reply is judged against it, not against a floor that commands
        # finishing meanwhile moved on.
        self._stream_cursor = self._done_cmd_seq
        url = (
            f"{self.hub}/v1/agent/commands"
            f"?machine={self.machine_id}&last={self._stream_cursor}"
        )
        req = urllib.request.Request(url, headers=_headers(self.cred, None))
        try:
            resp = _urlopen(req, 90)
        except Exception:
            time.sleep(RECONNECT_S)
            return
        self._hub_back()
        data_lines: list[bytes] = []
        try:
            with resp:
                for raw in resp:
                    if self._stop.is_set():
                        break
                    line = raw.rstrip(b"\n")
                    if line == b"":
                        if data_lines:
                            frame = b"\n".join(data_lines)
                            data_lines = []
                            if self._handle_frame(frame) == "reset":
                                # Reconnect from the new cursor, so the
                                # hub's backlog floor follows it down.
                                break
                        continue
                    if line.startswith(b"data:"):
                        data_lines.append(line[5:].lstrip())
        except Exception:
            pass
        if not self._stop.is_set():
            time.sleep(1)

    def _hub_back(self) -> None:
        """The command stream is up: the hub may have restarted, or
        this machine was asleep. The next upload says so (the hub then
        delivers what it held) and asks afresh how far each copied
        journal reaches, rather than trust what an earlier hub said."""
        with self._kept_lock:
            for entry in self._kept.values():
                entry["acked"] = None
            self._kept_hello = True
        self._kick.set()

    def _handle_frame(self, blob: bytes) -> str | None:
        """Act on one SSE frame; "reset" when it moved the command
        cursor back (the stream should reconnect from there)."""
        try:
            ev = json.loads(blob.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(ev, dict):
            return None
        if ev.get("type") == "journal.truncated":
            return self._reset_cmd_cursor(ev.get("seq"))
        if ev.get("type") != "command":
            return None
        epoch = self._cmd_epoch
        seq = ev.get("seq")
        if isinstance(seq, int):
            if seq in self._done_set:
                # Finished before a restart, above the contiguous
                # floor the replay started from: seen it, don't run
                # it again. The wire cursor still moves.
                self._last_cmd_seq = max(self._last_cmd_seq, seq)
                return None
            if seq <= self._last_cmd_seq:
                return None
            self._last_cmd_seq = seq
        op = (ev.get("data") or {}).get("op")
        if op == "kept":
            # A command for a keeper session: on the session's lane, so
            # a session's commands (and the uploads a message names)
            # stay in order and the stream is never held by one.
            data = ev.get("data") or {}
            sent_at = ev.get("ts")
            self._sendq.submit(
                str(data.get("session") or ""),
                lambda: self._run_kept(seq, data, epoch, sent_at),
                lambda: None)
            return None
        if op in _SESSION_QUEUED_OPS:
            # Headless sends block for the whole turn and an attachment
            # pull is a download: queue them so a later interrupt/detach
            # isn't stuck behind them. A session's pulls and steers stay
            # in order; a queue-mode send waits for idle on its own lane
            # and then for the pulls it names (``_submit_queued``).
            # attach stays on the stream: queued behind a turn it could
            # land after a later detach and undo it. One still queued at
            # shutdown is left undone, so the replay runs it.
            self._submit_queued(seq, ev, epoch)
            return None
        if op in ("fork", "launch"):
            # Booting a new session's pane can take 30 s: run it beside
            # the stream so an interrupt or detach behind it goes at
            # once. The cursor still waits for it, so a crash replays it.
            threading.Thread(
                target=self._dispatch_then_done, args=(seq, ev, epoch),
                name=f"agent-{op}-{seq}", daemon=True,
            ).start()
            return None
        self._dispatch(ev)
        self._mark_done(seq, epoch)
        return None

    def _dispatch_then_done(self, seq, ev: dict,
                            epoch: int | None = None) -> None:
        try:
            self._dispatch(ev)
        finally:
            self._mark_done(seq, epoch)

    def _submit_queued(self, seq, ev: dict, epoch: int) -> None:
        """Put a ``message`` or ``attachment`` command on its lane.

        Attachment pulls and steer sends share the session's lane, in
        order. A ``queue``-mode send gets a lane of its own, as on the
        hub, and waits there until the session is idle, so it never
        holds a steer or the pull a steer needs. It first waits for the
        pulls it names, then for idle. A command the agent stops before
        running is
        never marked done, so the hub replays it after the restart.
        """
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        key = str(ev.get("session") or "")
        op = str(data.get("op") or "")
        if op == "attachment":
            pull = (key, str(data.get("ref") or ""))
            done = threading.Event()
            with self._pulls_lock:
                self._pulls[pull] = done

            def pull_job() -> None:
                try:
                    self._dispatch_then_done(seq, ev, epoch)
                finally:
                    with self._pulls_lock:
                        if self._pulls.get(pull) is done:
                            del self._pulls[pull]
                    done.set()

            self._sendq.submit(key, pull_job, lambda: None)
            return
        if str(data.get("mode") or "queue") != "queue":
            self._sendq.submit(
                key, lambda: self._dispatch_then_done(seq, ev, epoch),
                lambda: None)
            return

        def job() -> None:
            # Pulls first: they run on the session lane, which never
            # waits on a turn, so they end soon. The idle check then
            # sits right before the send; a steer that started a turn
            # during a download is waited out, not typed into.
            if not self._await_pulls(key, data):
                return
            waited = self._wait_idle(key, data)
            if waited == "stopped":
                return
            if waited == "busy":
                self._report_message(
                    key, data, False,
                    f"session stayed busy for "
                    f"{round(QUEUE_WAIT_MAX_S / 60)} min; message not sent")
                self._mark_done(seq, epoch)
                return
            self._dispatch_then_done(seq, ev, epoch)

        self._sendq.submit(f"{key}\x00queue", job, lambda: None)

    def _local_post(self, key: str, route: str, body: dict,
                    device: str | None = None) -> tuple[int, dict]:
        """``POST /v1/sessions/{key}/{route}`` on this machine's own
        routes: the code the hub runs for a session it holds itself."""
        if self.catalog.row_for(key) is None \
                and self.local.runtime.owns(key):
            # A command can beat the first catalog refresh after a
            # restart; the route looks the session up in the list.
            self.catalog.refresh()
        return self.api.call(
            "POST", f"/v1/sessions/{quote(key, safe='')}/{route}", body,
            device=device)

    def _run_kept(self, seq, data: dict, epoch: int,
                  sent_at: float | None = None) -> None:
        """Run one ``kept`` command: hand the client's request to this
        machine's route under the id the hub stored it by, then tell
        the hub what the route answered. Done only once the hub knows;
        until then a restart runs it again, and the store here answers
        the second run as a duplicate."""
        key = str(data.get("session") or "")
        route = str(data.get("route") or "")
        body = data.get("body") if isinstance(data.get("body"), dict) else {}
        command_id = str(data.get("id") or body.get("command_id") or "")
        refused = self._kept_refusal(key, route, body, command_id, sent_at)
        if refused is not None:
            status, reply = refused
        else:
            status, reply = self._local_post(
                key, route, body, str(data.get("device") or "") or None)
        if self._report_kept(command_id, str(data.get("command_id") or ""),
                             status, reply):
            self._mark_done(seq, epoch)

    def _kept_refusal(self, key: str, route: str, body: dict,
                      command_id: str, sent_at: float | None
                      ) -> tuple[int, dict] | None:
        """The answer to a ``kept`` command that must not reach the
        route, or None when it may.

        **Already run.** A command this machine's store knows is a
        replay, and its stored answer stands however late the replay
        is read: saying "expired" for something that ran would have the
        hub reject a command that did its work.

        **Too old.** The hub puts a command on the stream when it
        believes this machine is awake, and a machine that fell asleep
        a moment before reads it whenever it wakes. A message or an
        answer older than the 24 hour hold has expired; anything else
        (a Stop, an option change, a queue edit) older than
        ``KEPT_FRESH_S`` would act on a turn it was never meant for.

        **No keeper here any more.** The session was let go since (a
        replay after a restart, a detach that already ran). The route
        would take the old path and type into a pane, so the stored
        answer is given instead, a detach or kill is done already, and
        anything else is refused.
        """
        runtime = self.local.runtime
        owned = runtime.owns(key)
        stored = None
        if runtime.store is not None and command_id:
            try:
                stored = runtime.store.get_command(command_id)
            except Exception:
                stored = None
        if stored is not None:
            # It ran here already (this is a replay): what it did is
            # the answer, however late the replay is read.
            if owned:
                return None  # the route answers the duplicate itself
            reply = {"ok": True, "duplicate": True,
                     "command_id": command_id, "receipt": stored.status}
            if stored.reason:
                reply["reason"] = stored.reason
            return 200, reply
        if not owned and route == "control" \
                and body.get("op") in ("detach", "kill"):
            return 200, {"ok": True, "note": "already detached"}
        if isinstance(sent_at, (int, float)) and not isinstance(sent_at, bool):
            limit = KEPT_HOLD_S if route in ("message", "approval") \
                else KEPT_FRESH_S
            if time.time() - sent_at > limit:
                return 409, protocol.error("expired", (
                    "this reached the machine too late to run"))
        if owned:
            return None
        return 409, protocol.error("not_kept", (
            "this session is no longer run by a session keeper"))

    @staticmethod
    def _local_result(status: int, reply: dict) -> tuple[bool, str, bool]:
        """A local route's answer as a command result."""
        if status == 0:
            return False, "this machine's session runtime did not answer", \
                False
        if status >= 400:
            err = reply.get("error") if isinstance(reply.get("error"),
                                                   dict) else {}
            return False, str(err.get("message")
                              or f"refused ({status})"), False
        if reply.get("duplicate") is True \
                and reply.get("receipt") == "rejected":
            return False, str(reply.get("reason") or "rejected"), False
        return True, str(reply.get("note") or reply.get("receipt")
                         or "ok"), False

    def _execute_kept(self, key: str, op: str,
                      data: dict) -> tuple[bool, str, bool]:
        """A v1 command (from a hub that does not store commands for
        keeper sessions) for a session a keeper holds here."""
        if op == "message":
            body = {"text": data.get("text") or "",
                    "mode": data.get("mode") or "queue",
                    "attachments": list(data.get("attachments") or []),
                    # The client's id when it sent one: its retry is
                    # then the same command here.
                    "command_id": str(data.get("client_id")
                                      or data.get("command_id") or "")}
            route = "message"
        elif op == "approval":
            body = {"request_id": str(data.get("request_id") or ""),
                    "decision": str(data.get("decision") or ""),
                    "command_id": str(data.get("command_id") or "")}
            route = "approval"
        else:
            body = {"op": str(data.get("ctl") or ""),
                    "command_id": str(data.get("command_id") or "")}
            if data.get("value"):
                body["value"] = str(data["value"])
            if data.get("mode"):
                body["mode"] = str(data["mode"])
            route = "control"
        return self._local_result(*self._local_post(key, route, body))

    def _launch_kept(self, data: dict, harness: str,
                     cwd: str) -> tuple[bool, str, bool]:
        """Start a new session under a keeper, as the hub does for its
        own machine: the id is chosen here, nothing is adopted, and the
        hub hears ``session.launched`` under its own launch id."""
        from swe.remote import runtime as runtime_mod

        if not os.path.isdir(cwd):
            return False, f"no such folder on this machine: {cwd}", False
        launch_id = str(data.get("launch_id") or "") \
            or "l" + secrets.token_hex(6)
        self._hub_launches.add(launch_id)
        try:
            self.local.runtime.launch(
                launch_id, harness, cwd, str(data.get("prompt") or ""),
                model=str(data.get("model") or ""),
                effort=str(data.get("effort") or ""),
                provider=str(data.get("provider") or ""),
                command_id=launch_id)
        except runtime_mod.DuplicateLaunch:
            pass  # this command ran before a restart: already started
        except runtime_mod.Refused as exc:
            self._hub_launches.discard(launch_id)
            return False, exc.message, False
        return True, "starting", False

    def _live_status(self, key: str, data: dict) -> str:
        """The session's status now: a fresh probe, else the tailer's.

        The status in the queued command is stale: the session may have
        gone idle or errored while we were offline.
        """
        fallback = (self.tail_manager.statuses.get(key)
                    or str(data.get("status") or "done"))
        session = self.catalog.session_for(key)
        if session is None:
            return fallback
        try:
            # The catalog's activity time can be minutes old for a turn
            # that just started; the probe would call it "interrupted".
            session = self.tail_manager.fresh_session(key, session)
            got = self.status_of([session])
        except Exception:
            return fallback
        return str(got[0]) if got and got[0] else fallback

    def _wait_idle(self, key: str, data: dict) -> str:
        """Wait for ``key``'s turn to end: ``idle``, ``busy`` or ``stopped``."""
        deadline = time.monotonic() + QUEUE_WAIT_MAX_S
        while self._live_status(key, data) == "active":
            if self.catalog.session_for(key) is None:
                # Gone from this machine: stop waiting, and let the
                # dispatch report the unknown session.
                return "idle"
            if time.monotonic() >= deadline:
                return "busy"
            if self._stop.wait(QUEUE_POLL_S):
                return "stopped"
        return "stopped" if self._stop.is_set() else "idle"

    def _await_pulls(self, key: str, data: dict) -> bool:
        """Wait for the attachment pulls ``data`` names; False if the
        agent stopped meanwhile. A pull that never finishes leaves the
        send to report its attachment unknown."""
        deadline = time.monotonic() + PULL_WAIT_MAX_S
        for ref in data.get("attachments") or []:
            with self._pulls_lock:
                done = self._pulls.get((key, str(ref)))
            while done is not None and not done.is_set():
                if self._stop.is_set():
                    return False
                if time.monotonic() >= deadline:
                    break
                done.wait(min(QUEUE_POLL_S, 0.5))
        return not self._stop.is_set()

    def _report_message(
        self, key: str, data: dict, ok: bool, note: str
    ) -> None:
        fields = {"command_id": str(data.get("command_id") or ""),
                  "op": "message", "ok": ok, "note": note,
                  "degraded": False}
        if data.get("client_id"):
            fields["client_id"] = str(data["client_id"])
        self._buffer_result(protocol.event(
            "command.result", key, self.machine_id, fields))

    def _reset_cmd_cursor(self, seq) -> str | None:
        """The hub answered ``journal.truncated``: start over if it says
        our cursor is past its command journal (wiped, or a hub that
        lost its data). Its seqs restart below us, and every new
        command would be dropped as already seen.

        The cursor goes to 0, not to the head the hub names: every
        command in the new journal was queued after ours ended, so the
        reconnect replays them all rather than skipping those queued
        while we were away. Behind the ring the hub echoes the cursor
        this connection sent, and nothing changes: compared with that,
        not the live floor, which commands finishing since have moved."""
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            return None
        with self._done_lock:
            if seq >= self._stream_cursor:
                return None
            self._cmd_epoch += 1
            self._done_cmd_seq = 0
            self._last_cmd_seq = 0
            # Done seqs above the old floor name the old journal's
            # commands, not the new one's.
            self._done_set.clear()
            self._save_cmd_cursor()
        return "reset"

    def _dispatch(self, cmd: dict) -> None:
        """Execute one command envelope; report back as command.result."""
        data = cmd.get("data") if isinstance(cmd.get("data"), dict) else {}
        op = str(data.get("op") or "")
        key = str(cmd.get("session") or "")
        command_id = str(data.get("command_id") or "")
        ok, note, degraded = self._execute(key, op, data)
        fields = {"command_id": command_id, "op": op, "ok": ok,
                  "note": note, "degraded": degraded}
        # Echo request_id back so a queued approval card on the phone
        # can tell which prompt this result belongs to.
        if data.get("request_id"):
            fields["request_id"] = str(data["request_id"])
        if data.get("client_id"):
            fields["client_id"] = str(data["client_id"])
        if data.get("launch_id"):
            fields["launch_id"] = str(data["launch_id"])
        result = protocol.event(
            "command.result", key, self.machine_id, fields,
        )
        self._buffer_result(result)

    def _buffer_result(self, event: dict) -> None:
        """Journal + buffer a command.result the same way _capture does."""
        self._capture(str(event.get("session") or ""), event)

    def _execute(
        self, key: str, op: str, data: dict
    ) -> tuple[bool, str, bool]:
        session = self.catalog.session_for(key)
        try:
            if op == "launch":
                harness = str(data.get("harness") or "")
                cwd = str(data.get("cwd") or "")
                # Same pre-launch snapshot the hub takes: any session
                # that exists must not be adopted.
                known = catalog_mod.known_session_ids(
                    self.catalog.tail_rows(), harness, cwd)
                binary = catalog_mod.launch_command(harness)
                if binary is None:
                    return False, (
                        f"{harness} can't start here: archived, switched "
                        f"off in Provider visibility, or not installed"
                    ), False
                if not data.get("yolo") \
                        and self.local.runtime.can_drive(harness):
                    return self._launch_kept(data, harness, cwd)
                ok, note, _ = supervise.launch_new(
                    self.home, harness, cwd,
                    str(data.get("prompt") or ""), known_ids=known,
                    launch_id=str(data.get("launch_id") or ""),
                    binary=binary,
                    model=str(data.get("model") or "") or None,
                    effort=str(data.get("effort") or "") or None,
                    yolo=bool(data.get("yolo")),
                    provider=str(data.get("provider") or "") or None,
                    # As on the hub: a harness swe can't read is never
                    # adopted, so its waiting launch guards no folder.
                    exclusive=harness not in catalog_mod.untracked(
                        catalog_mod.launchable_harnesses()),
                )
                return ok, note, False
            if op == "attachment":
                ref = str(data.get("ref") or "")
                # The hub staged the upload under the hub-scoped key —
                # that's what the pull URL carries, not our local key.
                hub_key = str(data.get("hub_key") or key)
                blob = _get_bytes(
                    self.hub,
                    # Both quoted whole: a key can hold "/" or "#",
                    # and the hub unquotes each part.
                    f"/v1/agent/attachments/{quote(hub_key, safe='')}/"
                    f"{quote(ref, safe='')}", self.cred,
                )
                if blob is None:
                    return False, "attachment fetch failed", False
                # Keep the hub's ref name — the message op that follows
                # resolves attachments by it.
                payload, err = attachments.place(
                    self.home, key, ref, blob
                )
                return payload is not None, (err or "staged"), False
            if op in ("message", "approval", "control") \
                    and self.local.runtime.owns(key):
                return self._execute_kept(key, op, data)
            if session is None:
                return False, "unknown session on this machine", False
            if op == "fork":
                # Tracked like launch: the result and the adopted
                # session both carry the hub's launch id.
                ok, note = fork_mod.start(
                    self.home, session, self.catalog.tail_rows(),
                    str(data.get("prompt") or ""),
                    str(data.get("launch_id") or ""),
                    model=str(data.get("model") or "") or None,
                    effort=str(data.get("effort") or "") or None,
                )
                return ok, note, False
            if op == "attach" and str(data.get("mode") or "auto") == "auto" \
                    and (self.local.runtime.owns(key)
                         or self.local.runtime.can_drive(session.tool_name)):
                # Continue in SWE: this machine's own attach route
                # checks that nothing else holds the session and starts
                # its keeper.
                body = {"op": "attach"}
                for name in ("model", "effort"):
                    if data.get(name):
                        body[name] = str(data[name])
                return self._local_result(
                    *self._local_post(key, "control", body))
            if op == "attach":
                # Same as the hub: attaching again cancels a when_idle
                # detach (one already running finishes first).
                undone = self.detaches.cancel(key, "cancelled by attach")
                ok, note = supervise.attach(
                    self.home, key, session,
                    last_append_at=self.tail_manager.last_append_at(key),
                    status=str(data.get("status") or ""),
                    mode=str(data.get("mode") or "auto"),
                    model=str(data.get("model") or "") or None,
                    effort=str(data.get("effort") or "") or None,
                )
                if ok:
                    self.tail_manager.wake(key)
                if ok and undone == "cancelled":
                    note += "; the pending detach is cancelled"
                return ok, note, False
            if op == "tail":
                # The hub is asking us to stream an older session a
                # phone just opened (past the background tail cap).
                self.tail_manager.ensure(
                    key, session, str(data.get("status") or ""))
                return True, "tailing", False
            # Same wake-on-write as the hub: the satellite tails its
            # own sessions, so a command it runs here starts the hot
            # window here.
            injector = self.tail_manager.waking(key, inject.injector_for(
                self.home, key, supervise.meta_for(self.home, key),
                emit=self._capture, machine=self.machine_id,
                deltas=self.deltas,
            ))
            if op == "message":
                # Resolve attachment refs to the paths the attachment
                # op staged locally, same as the hub's send path.
                paths = []
                for ref in data.get("attachments") or []:
                    path = attachments.resolve(self.home, key, str(ref))
                    if path is None:
                        return False, f"unknown attachment {ref}", False
                    paths.append(path)
                live = self._live_status(key, data)
                result = injector.send(
                    session,
                    {"text": data.get("text") or "",
                     "mode": data.get("mode") or "queue",
                     "attachments": paths},
                    live,
                )
                return result.ok, result.note, result.degraded
            if op == "approval":
                result = injector.approval(
                    session,
                    str(data.get("request_id") or ""),
                    str(data.get("decision") or ""),
                )
                return result.ok, result.note, result.degraded
            if op == "control":
                ctl = str(data.get("ctl") or "")
                mode = str(data.get("mode") or "now")
                if ctl == "kill" or (ctl == "detach" and mode != "when_idle"):
                    self.detaches.cancel(key, f"replaced by {ctl} {mode}")
                if ctl == "detach" and mode == "when_idle":
                    return self._detach_when_idle(key, session)
                if ctl == "detach" and mode == "keep":
                    ok, note = supervise.keep(self.home, key)
                    return ok, note, False
                result = injector.control(
                    session, ctl, str(data.get("value") or ""),
                )
                if result.ok and ctl in {"kill", "detach"}:
                    # As on the hub: the session is no longer ours, so
                    # its staged files go (a kept one returned above).
                    attachments.drop_session(self.home, key)
                return result.ok, result.note, result.degraded
            return False, f"unknown op {op}", False
        except Exception as exc:
            return False, str(exc), False

    def _detach_when_idle(
        self, key: str, session
    ) -> tuple[bool, str, bool]:
        """Queue a detach for when this machine's session goes idle.

        The command's own result says it is queued; the detach itself
        reports a second ``command.result`` (``op: detach``), and the
        catalog push carries the ownership change to the hub.
        """
        if supervise.meta_for(self.home, key) is None:
            return False, "session is not attached on this machine", False

        def status() -> str:
            current = self.catalog.session_for(key) or session
            got = self.status_of([current])
            return str(got[0]) if got else ""

        def fire() -> tuple[bool, str]:
            if supervise.meta_for(self.home, key) is None:
                return False, "session was already detached"
            current = self.catalog.session_for(key) or session
            result = inject.injector_for(
                self.home, key, supervise.meta_for(self.home, key),
                emit=self._capture, machine=self.machine_id,
            ).control(current, "detach", "")
            if result.ok:
                attachments.drop_session(self.home, key)
            return result.ok, result.note

        def report(ok: bool, note: str, cancelled: bool) -> None:
            fields = {"command_id": "", "op": "detach",
                      "mode": "when_idle", "ok": ok, "note": note}
            if cancelled:
                fields["cancelled"] = True
            self._buffer_result(protocol.event(
                "command.result", key, self.machine_id, fields))

        if not self.detaches.schedule(
                key, status=status, fire=fire, report=report):
            return True, "a detach is already waiting for this turn to end", False
        return True, "detaches when the current turn ends", False

    # -- loops -------------------------------------------------------------

    def _uplink_loop(self) -> None:
        while not self._stop.is_set():
            try:
                # Keeper journals first: a session this machine let go
                # must be released on the hub before its tailer's
                # events arrive, or the hub would not number them.
                if self._push_kept():
                    self._push_events()
            except Exception:
                pass
            self._kick.wait(0.5)
            self._kick.clear()

    def _catalog_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._push_catalog()
                rows, as_of = self.catalog.tail_snapshot()
                self.tail_manager.sync(rows, as_of=as_of)
                inject.AcpInjector.release_held(
                    self.tail_manager.tailers, self._capture,
                    self.machine_id, self.deltas)
                supervise.reap_kept(self.home, self.catalog.session_for)
                # A parked session resumed in a terminal, a keeper on
                # an older swe: as the hub's catalog loop does.
                self.local.runtime.tick()
            except Exception:
                pass
            self._catalog_kick.wait(CATALOG_PUSH_S)
            self._catalog_kick.clear()

    def _usage_loop(self) -> None:
        # A failed push (hub down, registry unreadable) retries on the
        # reconnect cadence rather than waiting out the whole interval.
        while not self._stop.is_set():
            try:
                ok = self._push_usage()
            except Exception:
                ok = False
            self._stop.wait(
                usage_mod.SATELLITE_PUSH_S if ok else RECONNECT_S * 12
            )

    def _tail_loop(self) -> None:
        sweeps = 0
        while not self._stop.is_set():
            try:
                self.tail_manager.tick()
            except Exception:
                pass
            sweeps += 1
            if sweeps >= 1200:  # ~5 min at a 250 ms tick, as on the hub
                sweeps = 0
                self._sweep()
            self.tail_manager.wait(0.25)

    def _sweep(self) -> None:
        """Delete files this satellite stopped needing: cursors of
        sessions idle past ``tail.STALE_FILE_AGE_S`` (the uplink outbox
        is its own and stays), and staged attachments past
        ``attachments.MAX_AGE_S``, as the hub's loop does."""
        try:
            from swe.remote import hub as hub_mod

            # This machine's own stream, and the journals of the
            # sessions its keepers hold (no tailer keeps those alive).
            with self._kept_lock:
                kept = list(self._kept)
            self.tail_manager.sweep_stale(self._outbox, keep=(
                OUTBOX_KEY, hub_mod.GLOBAL_KEY, *kept,
                *(row["key"] for row in self.local.runtime.rows())))
        except Exception:
            pass
        try:
            attachments.sweep(self.home)
        except Exception:
            pass

    def _downlink_loop(self) -> None:
        while not self._stop.is_set():
            # Re-register on every reconnect: the first attempt can run
            # while the hub is down, and the stream itself only tells
            # the hub {id, online} — name/os/harnesses come from here.
            try:
                self._register()
            except Exception:
                pass
            try:
                self._command_stream()
            except Exception:
                self._stop.wait(RECONNECT_S)

    def run(self) -> int:
        supervise.reconcile_kept(self.home)
        self._note_machine()
        if self.local_port is not None:
            try:
                self.api.start(self.local_port)
            except OSError as exc:
                # The agent still runs keepers and takes the hub's
                # commands; only a local client finds no door.
                print(f"swe remote agent: the local API is off "
                      f"(127.0.0.1:{self.local_port}: {exc})",
                      file=sys.stderr, flush=True)
        self._register()
        self._push_catalog()
        threads = [
            threading.Thread(target=self._uplink_loop, daemon=True,
                             name="agent-uplink"),
            threading.Thread(target=self._delta_loop, daemon=True,
                             name="agent-deltas"),
            threading.Thread(target=self._catalog_loop, daemon=True,
                             name="agent-catalog"),
            threading.Thread(target=self._usage_loop, daemon=True,
                             name="agent-usage"),
            threading.Thread(target=self._tail_loop, daemon=True,
                             name="agent-tail"),
            threading.Thread(target=self._downlink_loop, daemon=True,
                             name="agent-downlink"),
        ]
        for t in threads:
            t.start()
        try:
            while True:
                time.sleep(0.5)
                if self._stop.is_set():
                    break
        except KeyboardInterrupt:
            pass
        self.close()
        for t in threads:
            t.join(timeout=2)
        return 0

    def close(self) -> None:
        """Stop this agent's own work. The keepers run on: the next
        agent on this machine reconnects to them."""
        self._stop.set()
        self._kick.set()
        self._catalog_kick.set()
        self._sendq.close()
        self.detaches.close()
        self.api.close()
        # Interrupts the tailers and lets go of every keeper.
        self.local.stop()


def serve(hub: str, credential: str,
          home: Path | str | None = None) -> int:
    """Blocking entry point for ``swe remote agent``."""
    agent = RemoteAgent(hub, credential, home)
    agent.local_port = local_port_for(agent.home)
    return agent.run()


# -- the hub's side of satellite keepers -------------------------------------
#
# Everything below runs in the hub process. ``hub.py`` calls three
# functions at the bottom of this section (``mirrored``, ``kept_route``,
# ``to_keeper``); the rest is reached through them.

# Client routes a satellite keeper session takes through the hub, and
# the op each is stored under.
_KEPT_OPS = {"message": "send", "approval": "respond", "control": "control",
             "queue": "queue", "command": "command"}
_KEPT_ROUTES = {op: route for route, op in _KEPT_OPS.items()}
# What the hub holds for a sleeping machine: a message, and the answer
# to a request that will still be open when the machine wakes (the
# keeper refuses it otherwise). A Stop or an option change delivered
# hours late would act on a turn it was never meant for, so those need
# the machine awake. The same split as ``runtime.REDELIVERED_OPS``.
HELD_OPS = frozenset({"send", "respond"})
HELD_REASON = "machine asleep"
# How often the hub looks for holds that ran out.
EXPIRE_EVERY_S = 60.0
# v2 command ops under the names today's clients match results on.
_V1_RESULT_OPS = {"send": "message", "respond": "approval"}
_COMMAND_FIELDS = ("op", "name", "value", "task_id", "clear_queue")


class SatelliteKeepers:
    """What the hub does for sessions a satellite's keeper holds.

    **Events.** The hub's journal for such a session is a copy of the
    satellite's own journal for it: each event is stored with
    ``Journal.append_at`` under the seq it already has, so an event
    sent twice lands once and a seq is never changed. The hub numbers
    nothing in that journal while it copies it (``mirrored``).

    **Commands.** Each write is stored in ``remote.sqlite`` under its
    ``command_id`` before it goes anywhere. For a machine that is
    asleep the receipt is ``held``; when the machine comes back the
    held commands are claimed one at a time, in arrival order, and put
    on its command stream. A hold past its deadline is rejected
    (``expired``) and never delivered.

    The claim is a write to the store and the hand-over a write to the
    stream's journal, and a hub can stop between the two. So a claimed
    command with no result is checked against the stream the first
    time its machine is flushed after the hub starts (and after a
    flush that ended early), and one the stream does not hold is put
    there then, in its place among the held ones (``_claimed_not_sent``).

    All of it needs the hub's store, so it is off unless the hub's own
    session runtime is on (``remote.session_view``).
    """

    def __init__(self, state) -> None:
        self.state = state
        self._lock = threading.RLock()
        self._machine_locks: dict[str, threading.Lock] = {}
        # Hub keys (``key@machine``) whose journal copies a satellite's.
        self._mirrors: set[str] = set()
        # Machines with a claimed command that has not been checked
        # against their stream since this hub started, or since a
        # flush of theirs ended early. ``_walked`` is False until the start-up
        # read of the store worked: until then every machine counts.
        self._unchecked: set[str] = set()
        self._walked = False
        self._load()
        self._walk()
        self._thread: threading.Thread | None = None
        if self.store is not None \
                and getattr(state, "hosts_satellites", True):
            self._thread = threading.Thread(
                target=self._expire_loop, name="remote-held-expiry",
                daemon=True)
            self._thread.start()

    @property
    def store(self):
        runtime = getattr(self.state, "runtime", None)
        return getattr(runtime, "store", None)

    def _load(self) -> None:
        store = self.store
        if store is None:
            return
        try:
            rows = store.list_sessions()
        except Exception:
            logger.exception("reading the satellites' sessions")
            return
        for row in rows:
            if row.machine != self.state.machine_id and row.state != "closed":
                self._mirrors.add(row.key)

    def _walk(self) -> None:
        """Once after the hub starts, walk ``Store.unfinished()`` and
        note each satellite with a claimed command (``queued``) that
        has no result. The hub before this one may have stopped between
        a claim and its hand-over; ``_flush`` finds out, when the
        machine is next flushed. The hub's own sessions are checked
        against their keepers instead (``SessionRuntime._reconcile``).
        """
        store = self.store
        if store is None or self._walked:
            return
        try:
            open_ = store.unfinished()
        except Exception:
            logger.exception("reading the commands with no result")
            return
        with self._lock:
            self._unchecked.update(
                cmd.machine for cmd in open_
                if cmd.status == "queued" and cmd.machine
                and cmd.machine != self.state.machine_id)
            self._walked = True

    def mirrored(self, key: str) -> bool:
        return key in self._mirrors

    def _machine_lock(self, mid: str) -> threading.Lock:
        with self._lock:
            return self._machine_locks.setdefault(mid, threading.Lock())

    # -- events ---------------------------------------------------------

    def take(self, mid: str, entry: dict) -> tuple[int | None, bool]:
        """Take one session's part of an upload into the hub's copy of
        its journal: ``(last seq the copy holds, released)``, or
        ``(None, False)`` when it could not be taken at all."""
        from swe.remote.store import StoreError

        local_key = entry.get("key")
        if not isinstance(local_key, str) or not local_key:
            return None, False
        store = self.store
        if store is None:
            return None, False
        state = self.state
        key = protocol.remote_key(mid, local_key)
        journal = state.journal
        head = entry.get("head")
        events = entry.get("events")
        events = events if isinstance(events, list) else []
        taken: list[dict] = []
        dropped = False
        let_go = entry.get("released") is True
        # The last seq dealt with: stored, or looked at and refused (a
        # malformed event is never asked for again).
        seen = 0
        with journal.hold(key):
            last = journal.last_seq(key)
            if key not in self._mirrors and let_go \
                    and self._released_before(store, key, head):
                # A release this hub already took (its answer was lost
                # on the way back). Nothing to do, and above all
                # nothing to drop: the hub has numbered that journal
                # since.
                return last, True
            if key not in self._mirrors:
                # The first event of a copy, or the first after the
                # satellite let the session go and took it back. What
                # the journal holds was numbered here, by another
                # count: the copy starts over from the satellite's.
                try:
                    self._begin(store, key, mid, local_key, entry.get("row"))
                except (StoreError, ValueError):
                    logger.exception("recording %s", key)
                    return None, False
                if last > 0:
                    journal.drop(key)
                    dropped = True
                self._mirrors.add(key)
            elif isinstance(head, int) and not isinstance(head, bool) \
                    and head < last and not let_go:
                # The satellite's journal started over (its keeper's
                # log was replaced): so does the copy.
                journal.drop(key)
                dropped = True
            for raw in events:
                event = self._clean(mid, key, raw)
                if event is None:
                    seq = raw.get("seq") if isinstance(raw, dict) else None
                    if isinstance(seq, int) and not isinstance(seq, bool):
                        seen = max(seen, seq)
                    continue  # malformed: skipped for good
                if event["seq"] > journal.last_seq(key):
                    # Effects first, the journal second, as for a
                    # local keeper: every effect can be applied twice,
                    # and a hub that dies in between is sent the event
                    # again.
                    self._apply(store, key, mid, event)
                    if not journal.append_at(key, event):
                        break  # not written: the satellite sends it again
                    taken.append(event)
                    # Under the journal's lock, as for a local keeper:
                    # v2 streams (G9) are queued events in seq order.
                    self._to_v2(key, event)
                seen = max(seen, event["seq"])
            acked = max(journal.last_seq(key), seen)
            released = let_go and (
                not isinstance(head, int) or acked >= head)
            if released:
                # From here the hub numbers this journal itself again:
                # the satellite's tailer has the session back.
                self._mirrors.discard(key)
        self._restore_pending(key, entry.get("pending"))
        if dropped:
            state.bus.publish(key, {"type": "journal.truncated", "seq": 0})
            sync = getattr(state, "sync", None)
            if sync is not None:
                # The numbering started over: a v2 client's cursor
                # from before is answered with a snapshot.
                sync.new_epoch(key)
        for event in taken:
            self._publish(key, event)
        if released:
            try:
                # ``last_seq`` is where this copy ended: a release for
                # the same keeper events, sent again, is known by it.
                store.update_session(key, state="closed")
                store.note_seq(key, acked)
            except (StoreError, ValueError):
                logger.exception("closing %s", key)
        return acked, released

    @staticmethod
    def _released_before(store, key: str, head) -> bool:
        """Whether the hub already took the release of the copy that
        ends at ``head``: its row is closed, and that copy reached
        there. A later copy of the same session (it was continued
        again) ends at a higher seq, and is taken as new."""
        try:
            row = store.get_session(key)
        except Exception:
            return False
        return row is not None and row.state == "closed" \
            and isinstance(head, int) and not isinstance(head, bool) \
            and head <= row.last_seq

    def _restore_pending(self, key: str, pending) -> None:
        """Set the session's open requests to what the satellite says
        are open, when it says (the question that opens an upload
        after either side restarted). As for a local keeper's welcome:
        the inbox and a v2 stream's state have them again, and nothing
        is announced a second time. Events that follow in the upload
        add and retire requests as usual."""
        if not isinstance(pending, dict):
            return
        open_ = {}
        for rid, asked in list(pending.items())[:64]:
            if isinstance(rid, str) and 0 < len(rid) <= 200 \
                    and isinstance(asked, (int, float)) \
                    and not isinstance(asked, bool):
                open_[rid] = float(asked)
        try:
            self.state.restore_approvals(key, open_)
        except Exception:
            logger.exception("restoring the open requests of %s", key)

    def _to_v2(self, key: str, event: dict) -> None:
        """Hand one event just journaled to the session's open v2
        streams (``sync.py``): a keeper's event as it is, and a v1
        event of the session's earlier history the way the hub's own
        journaled v1 events reach them."""
        from swe.remote.adapters import events as ev

        sync = getattr(self.state, "sync", None)
        if sync is None:
            return
        try:
            if event.get("v") == ev.VERSION:
                sync.on_event(key, event)
            else:
                sync.on_v1(key, event)
        except Exception:
            logger.exception("v2 streams of %s", key)

    def _begin(self, store, key: str, mid: str, local_key: str,
               row) -> None:
        row = row if isinstance(row, dict) else {}
        harness = str(row.get("harness") or local_key.split(":", 1)[0])
        harness_id = str(row.get("harness_session_id")
                         or local_key.partition(":")[2] or local_key)
        store.put_session(
            key, harness=harness, harness_session_id=harness_id,
            machine=mid, cwd=str(row.get("cwd") or ""), state="starting",
            adapter=str(row.get("adapter") or "") or None)

    def _clean(self, mid: str, key: str, event) -> dict | None:
        """``event`` as the hub stores it (under the hub's key), or
        None for one it will not take. The journal feeds clients as it
        is, so a v2 event must pass the schema and a v1 event (the
        session's history from before its keeper) the same checks as
        any event a satellite uploads."""
        from swe.remote import hub as hub_mod
        from swe.remote.adapters import events as ev

        if not isinstance(event, dict):
            return None
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            return None
        etype = event.get("type")
        data = event.get("data")
        if event.get("v") == ev.VERSION:
            event = dict(event, session=key)
            if etype in ev.TRANSIENT_TYPES or ev.validate(event):
                return None
            return event
        if not isinstance(etype, str) or etype == "message.delta" \
                or etype not in hub_mod._SAT_EVENT_TYPES:
            return None
        if data is not None and not isinstance(data, dict):
            return None
        kinds = hub_mod._SAT_KIND_SETS.get(etype)
        if kinds is not None:
            kind = data.get("kind") if isinstance(data, dict) else None
            if not isinstance(kind, str) or kind not in kinds:
                return None
        return dict(event, session=key, machine=mid)

    def _apply(self, store, key: str, mid: str, event: dict) -> None:
        """The store's part of one keeper event: the session's state,
        and the receipt and result of a command that came through this
        hub for this session. A command sent on the satellite itself is
        not in this store, and a satellite's events never touch a
        command stored for another machine or session."""
        from swe.remote import protocol_compat
        from swe.remote.adapters import events as ev
        from swe.remote.store import StoreError

        if event.get("v") != ev.VERSION:
            return
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        cid = event.get("command_id")
        try:
            if etype in ("command.receipt", "command.result"):
                cmd = store.get_command(cid) if isinstance(cid, str) \
                    else None
                if cmd is None or cmd.machine != mid or cmd.session != key:
                    return
            if etype == "session.state":
                state = data.get("state")
                if isinstance(state, str) and state in ev.SESSION_STATES:
                    if state != "closed":
                        # ``closed`` in the store means the hub stopped
                        # copying; only a release says that.
                        store.update_session(key, state=state)
                    status = protocol_compat.STATUS_FOR_STATE.get(state)
                    # The list hears it now; the satellite's next
                    # catalog push says the same.
                    fields = {"state": state}
                    if status:
                        fields["status"] = status
                    self.state.catalog.patch(key, fields, status or "done")
            elif etype == "command.receipt" and isinstance(cid, str):
                status = data.get("status")
                if status == "accepted":
                    store.transition(cid, "accepted")
                elif status == "rejected":
                    reason = data.get("reason")
                    store.transition(cid, "rejected", reason=(
                        reason if isinstance(reason, str) and reason
                        else None))
            elif etype == "command.result" and isinstance(cid, str):
                result = {k: data[k] for k in (
                    "ok", "note", "degraded", "cancelled", "request_id",
                    "launch_id") if k in data}
                if data.get("ok") is True:
                    store.transition(cid, "accepted")
                store.set_result(cid, result)
        except (StoreError, ValueError):
            logger.exception("store effect of %s on %s", etype, key)

    def _publish(self, key: str, event: dict) -> None:
        """Send one event just stored to the session's open streams,
        as today's clients read it, and to the v2 listeners. A v1
        event in a copy is the session's history from before its
        keeper: the hub published it when the satellite's tailer first
        uploaded it, and saying it again would raise its notification
        and reopen its approval."""
        from swe.remote.adapters import events as ev

        state = self.state
        if event.get("v") != ev.VERSION:
            return
        try:
            frame = state.runtime.v1(key, event)
            if frame is not None:
                state.publish_kept(key, frame)
            if event.get("v") == ev.VERSION:
                for listener in state.runtime.listeners:
                    listener(key, event)
        except Exception:
            logger.exception("publishing %s for %s", event.get("type"), key)

    # -- commands -------------------------------------------------------

    def forward(self, handler, key: str, mid: str, action: str, row: dict,
                device_id: str | None) -> bool:
        """``POST /v1/sessions/{key}/{action}`` for a session a
        satellite keeper holds. True when it answered the request;
        False leaves the request to the hub's own forwarding (the
        session is not kept, or the action is one that path handles:
        uploads, attach)."""
        from swe.remote import runtime as runtime_mod, store as store_mod

        store = self.store
        if store is None or action not in _KEPT_OPS \
                or row.get("mode") != runtime_mod.MODE:
            return False
        body = handler._read_body()
        if body is None:
            handler._send_error(400, "bad_body", "unreadable JSON body")
            return True
        op = _KEPT_OPS[action]
        try:
            payload = self._payload(action, body)
            if payload is None:
                return False  # attach: the hub's own path
            # A queue action's ``client_id`` names the queued message,
            # not the action: only ``command_id`` is the action's own.
            cid = store_mod.command_id_of(
                {"command_id": body.get("command_id")}
                if action == "queue" else body)
        except ValueError as exc:
            handler._send_error(422, "bad_command", str(exc))
            return True
        client_id = cid or ""
        cid = cid or runtime_mod.new_command_id()
        device = device_id if isinstance(device_id, str) \
            and _DEVICE_RE.fullmatch(device_id) else None
        state = self.state
        with self._machine_lock(mid):
            online = state.machine_online(mid)
            if not online and op not in HELD_OPS:
                handler._send_error(409, "machine_offline", (
                    f"{mid} is asleep or offline; this needs it awake"))
                return True
            try:
                receipt = store.record_command(
                    cid, session=key, op=op, status="held", payload=payload,
                    machine=mid, device=device, reason=HELD_REASON)
            except ValueError as exc:
                handler._send_error(422, "bad_command", str(exc))
                return True
            except store_mod.StoreError as exc:
                handler._send_error(503, "store_failed", str(exc))
                return True
            if receipt.conflict:
                handler._send_error(409, "command_id_conflict",
                                    receipt.reason or "")
                return True
            if not receipt.duplicate and online:
                # Whatever was held before it goes first.
                self._flush(mid)
        try:
            stored = store.get_command(cid)
        except store_mod.StoreError as exc:
            handler._send_error(503, "store_failed", str(exc))
            return True
        status = stored.status if stored is not None else "held"
        out = {"ok": True, "queued": True, "waiting": False,
               "command_id": cid, "client_id": client_id,
               "receipt": status}
        if receipt.duplicate:
            out["duplicate"] = True
            if stored is not None and stored.result is not None:
                out["result"] = stored.result
            if stored is not None and stored.reason \
                    and status == "rejected":
                out["reason"] = stored.reason
        elif status == "rejected":
            handler._send_error(429, "backlog_full", (
                stored.reason if stored is not None and stored.reason
                else "machine is too far behind"))
            return True
        handler._send_json(202, out)
        return True

    @staticmethod
    def _payload(action: str, body: dict) -> dict | None:
        """The fields of ``body`` the route takes, checked: what is
        stored, and what the satellite's own route is given. None for a
        control this path does not carry (attach)."""
        if action == "message":
            cmd = protocol.parse_message_command(body)
            return {"text": cmd["text"], "mode": cmd["mode"],
                    "attachments": list(cmd["attachments"])}
        if action == "approval":
            cmd = protocol.parse_approval_command(body)
            return {"request_id": cmd["request_id"],
                    "decision": cmd["decision"]}
        if action == "control":
            cmd = protocol.parse_control_command(body)
            if cmd["op"] == "attach":
                return None
            out = {"op": cmd["op"]}
            if cmd.get("value"):
                out["value"] = cmd["value"]
            if "mode" in cmd:
                out["mode"] = cmd["mode"]
            return out
        if action == "queue":
            act = protocol.parse_queue_action(body)
            out = {"client_id": act["client_id"], "op": act["op"]}
            if act.get("text") is not None:
                out["text"] = act["text"]
            return out
        return {name: body[name] for name in _COMMAND_FIELDS
                if body.get(name) is not None}

    def _flush(self, mid: str) -> None:
        """Deliver ``mid``'s held commands, oldest first, each claimed
        once, and with them any command claimed before and never put
        on the stream. Caller holds the machine's lock, so two flushes
        cannot interleave and the order on the wire is the order of
        arrival. A store or stream that fails ends the flush: nothing
        behind the command it failed on overtakes it, and the next
        flush carries on from there."""
        from swe.remote.store import StoreError

        store = self.store
        if store is None:
            return
        self._expire()
        try:
            owed = self._claimed_not_sent(store, mid)
            held = store.list_held(mid)
        except StoreError:
            logger.exception("listing %s's held commands", mid)
            return
        for cmd in sorted((*owed, *held), key=lambda c: c.id):
            try:
                # A held message's deadline is the store's to keep
                # (the claim below refuses one that ran out).
                if (cmd.status != "held" or cmd.op not in HELD_OPS) \
                        and self._too_late(store, cmd):
                    continue
                # The claim: of two deliveries of one command, one
                # wins. A hold that ran out meanwhile cannot be claimed.
                if cmd.status == "held" \
                        and not store.transition(cmd.command_id, "queued"):
                    continue
            except StoreError:
                logger.exception("claiming %s", cmd.command_id)
                self._owe(mid)
                return
            except ValueError:
                logger.exception("claiming %s", cmd.command_id)
                continue
            if not self._hand_over(mid, cmd):
                self._owe(mid)
                return
        with self._lock:
            self._unchecked.discard(mid)

    def _owe(self, mid: str) -> None:
        """A flush of ``mid`` ended early: its claimed commands are
        checked against its stream again on the next one."""
        with self._lock:
            self._unchecked.add(mid)

    def _claimed_not_sent(self, store, mid: str) -> list:
        """``mid``'s commands that were claimed and never handed over:
        ``queued``, no result, and not among the ``kept`` commands on
        its stream. Empty, and nothing read, once the machine has been
        checked since the hub started and no flush ended early since.

        ``accepted`` means the machine answered, so those are never
        here. The stream's journal is a ring, and what it dropped it
        cannot vouch for: a command counts as never sent only when the
        ring still reaches back to its claim (nothing was ever dropped,
        or the oldest entry left is older than the claim). One claimed
        before that is left as it is; the machine has most likely had
        it. Caller holds the machine's lock."""
        with self._lock:
            if self._walked and mid not in self._unchecked:
                return []
        if mid == self.state.machine_id:
            return []
        claimed = [cmd for cmd in store.unfinished(machine=mid)
                   if cmd.status == "queued"]
        if not claimed:
            return []
        events, _cut = self.state.journal.read_since(
            self.state.sat_journal_key(mid), 0)
        sent = set()
        for event in events:
            data = event.get("data")
            if isinstance(data, dict) and data.get("op") == "kept":
                sent.add(data.get("id"))
        whole = not events or events[0].get("seq") == 1
        since = events[0].get("ts") if events else None
        owed = []
        for cmd in claimed:
            if cmd.command_id in sent:
                continue
            if whole or (isinstance(since, (int, float))
                         and not isinstance(since, bool)
                         and since <= cmd.updated_at):
                owed.append(cmd)
        return owed

    def _too_late(self, store, cmd) -> bool:
        """Whether a command about to go on the stream is past the age
        a satellite would run it at, counted from when it arrived: the
        24 hour hold for a message or an answer, ten minutes for
        anything else (a Stop that late would act on a turn it was
        never meant for). True once it is rejected and announced, and
        for one that finished meanwhile. The satellite's own check
        (``_kept_refusal``) cannot do this for a command that waited
        on the hub: it reads the time the command went on the stream.
        Not asked for a held message (the store keeps its deadline,
        the same 24 hours). A control is stored ``held`` only for the
        moment before its claim, and one a hub left that way is
        caught here."""
        from swe.remote import store as store_mod

        limit = store_mod.HOLD_S if cmd.op in HELD_OPS else KEPT_FRESH_S
        if store.now() - cmd.received_at < limit:
            return False
        note = ("expired: the hub stopped before this was sent, and it "
                "is too old to send now")
        if store.transition(cmd.command_id, "rejected", reason=note):
            self._announce(cmd, note)
        return True

    def _hand_over(self, mid: str, cmd) -> bool:
        """Put one claimed command on ``mid``'s stream. False when it
        is not there afterwards and is still owed. The journal reports
        no failed write (``Journal.append`` returns the event either
        way), so the stream is read back."""
        local_key, _mid = protocol.split_remote_key(cmd.session)
        state = self.state
        try:
            sent = state.queue_command(mid, "kept", "", {
                "session": local_key,
                "route": _KEPT_ROUTES.get(cmd.op, cmd.op),
                "id": cmd.command_id,
                "body": {**cmd.payload, "command_id": cmd.command_id},
                "device": cmd.device or "",
            })
        except Exception:
            logger.exception("putting %s on %s's stream",
                             cmd.command_id, mid)
            return False
        if sent is None:
            self._reject(cmd, f"{mid} is too far behind; not sent")
            return True
        seq = sent.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            written, _cut = state.journal.read_since(
                state.sat_journal_key(mid), seq - 1)
            if any(event.get("seq") == seq for event in written):
                return True
        logger.error("%s was not written to %s's stream",
                     cmd.command_id, mid)
        return False

    def _reject(self, cmd, note: str) -> None:
        from swe.remote.store import StoreError

        store = self.store
        try:
            if store is not None and store.transition(
                    cmd.command_id, "rejected", reason=note):
                self._announce(cmd, note)
        except (StoreError, ValueError):
            logger.exception("rejecting %s", cmd.command_id)

    def _announce(self, cmd, note: str) -> None:
        """Tell the session's open streams a command will never run.
        No keeper logged it, so it carries no seq."""
        state = self.state
        state.publish_kept(cmd.session, protocol.event(
            "command.result", cmd.session, state.machine_id, {
                "command_id": cmd.command_id, "client_id": cmd.command_id,
                "op": _V1_RESULT_OPS.get(cmd.op, cmd.op), "ok": False,
                "note": note, "degraded": False}), seq=False)

    def _expire(self) -> None:
        from swe.remote.store import StoreError

        store = self.store
        if store is None:
            return
        try:
            expired = store.expire_held()
        except StoreError:
            logger.exception("expiring held commands")
            return
        for cmd in expired:
            self._announce(cmd, str((cmd.result or {}).get("note")
                                    or "expired"))

    def tick(self) -> None:
        """One pass of the timer: reject the holds that ran out, and
        deliver what is still held, or was claimed and never sent, for
        a machine that is back."""
        from swe.remote.store import StoreError

        store = self.store
        if store is None:
            return
        self._expire()
        self._walk()
        try:
            machines = {cmd.machine for cmd in store.list_held()}
        except StoreError:
            return
        with self._lock:
            # And the machines owed a command that was claimed and may
            # never have reached their stream.
            machines |= self._unchecked
        for mid in sorted(machines):
            if mid and self.state.machine_online(mid):
                with self._machine_lock(mid):
                    self._flush(mid)

    def _expire_loop(self) -> None:
        stop = self.state._stop
        while not stop.wait(EXPIRE_EVERY_S):
            try:
                self.tick()
            except Exception:
                logger.exception("held-command timer")

    def settle(self, mid: str, result) -> None:
        """What the satellite's own route answered for one delivered
        command. A refusal there never reaches a keeper, so no event
        will say so: it is stored and announced here. An answer that
        is final at once (a control, a queue edit) is stored too; a
        message's result comes later, as the keeper's event."""
        from swe.remote.store import StoreError

        store = self.store
        if store is None or not isinstance(result, dict):
            return
        via = result.get("via")
        if isinstance(via, str) and via:
            # The command stream's own ack, as a ``command.result``
            # from the satellite would give it.
            self.state._sat_result_acked(mid, via)
        cid = result.get("command_id")
        status = result.get("status")
        body = result.get("body") if isinstance(result.get("body"), dict) \
            else {}
        if not isinstance(cid, str) or not isinstance(status, int):
            return
        try:
            cmd = store.get_command(cid)
            if cmd is None or cmd.machine != mid:
                return
            refused = status >= 400 or status == 0 or (
                body.get("duplicate") is True
                and body.get("receipt") == "rejected")
            if refused:
                err = body.get("error") if isinstance(body.get("error"),
                                                      dict) else {}
                note = str(err.get("message") or body.get("reason")
                           or "the machine refused it")[:500]
                if store.transition(cid, "rejected", reason=note):
                    self._announce(cmd, note)
                return
            receipt = body.get("receipt")
            if receipt in (None, "accepted"):
                store.transition(cid, "accepted")
            if receipt is None:
                store.set_result(cid, {"ok": True, "note": str(
                    body.get("note") or "")[:500]})
        except (StoreError, ValueError):
            logger.exception("settling %s", cid)

    # -- the route ------------------------------------------------------

    def route(self, handler, device_id: str | None) -> None:
        """``POST /v1/agent/kept``: a satellite uploads what its keeper
        sessions logged and what its own routes answered, and learns
        how far the hub's copy of each journal reaches."""
        if not handler._require_satellite():
            return
        body = handler._read_body()
        if body is None:
            handler._send_error(400, "bad_body", "unreadable JSON body")
            return
        mid = str(body.get("machine_id") or "")
        if not mid:
            handler._send_error(422, "bad_command", "machine_id is required")
            return
        if not handler._require_machine(device_id, mid):
            return
        if self.store is None:
            handler._send_error(409, "session_view_off", (
                "this hub's session runtime is off (remote.session_view)"))
            return
        sessions = body.get("sessions")
        results = body.get("results")
        if (sessions is not None and not isinstance(sessions, list)) or (
                results is not None and not isinstance(results, list)):
            handler._send_error(422, "bad_command",
                                "sessions and results must be lists")
            return
        acked: dict[str, int] = {}
        released: list[str] = []
        for entry in sessions or []:
            if not isinstance(entry, dict):
                continue
            seq, let_go = self.take(mid, entry)
            if seq is not None:
                acked[str(entry.get("key"))] = seq
                if let_go:
                    released.append(str(entry.get("key")))
        for result in results or []:
            self.settle(mid, result)
        if body.get("hello") is True:
            # The machine is back. Its command stream replays the
            # journal from its own cursor, so what was held can go on
            # it now whether or not that stream is up yet.
            with self._machine_lock(mid):
                self._flush(mid)
        handler._send_json(200, {"ok": True, "acked": acked,
                                 "released": released})


_keepers_guard = threading.Lock()


def satellite_keepers(state) -> SatelliteKeepers:
    """``state``'s ``SatelliteKeepers``, made the first time it is
    asked for."""
    side = state.__dict__.get("_satellite_keepers")
    if side is None:
        with _keepers_guard:
            side = state.__dict__.get("_satellite_keepers")
            if side is None:
                side = SatelliteKeepers(state)
                state.__dict__["_satellite_keepers"] = side
    return side


def mirrored(state, key: str) -> bool:
    """Whether ``key``'s journal on this hub is a copy of a satellite
    keeper session's: the hub numbers nothing in it (``HubState.emit``
    asks)."""
    return satellite_keepers(state).mirrored(key)


def kept_route(handler, device_id: str | None) -> None:
    """``POST /v1/agent/kept`` (``hub._Handler.do_POST`` calls it)."""
    satellite_keepers(handler._state).route(handler, device_id)


def to_keeper(handler, key: str, action: str, row: dict,
              device_id: str | None) -> bool:
    """A write to another machine's session (``hub._Handler.do_POST``
    asks before it forwards one): True when a satellite keeper holds
    the session and the request was answered here."""
    if row.get("mode") != "keeper":
        return False
    return satellite_keepers(handler._state).forward(
        handler, key, str(row.get("machine_id") or ""), action, row,
        device_id)
