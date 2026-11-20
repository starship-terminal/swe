"""Finding keepers, and starting one.

Each keeper owns a folder, ``data/remote/keepers/<slug>/``::

    keeper.lock   flock'd for as long as the keeper lives
    keeper.json   who it is: pid, socket, IPC versions, state      (0600)
    spec.json     what to start: harness, cwd, resume id, options  (0600)
    keeper.out    its stdout and stderr
    log/          the event log (keeper/log.py)

The lock is the truth about whether a keeper is alive: the kernel drops
it when the process ends, however it ends. ``keeper.json`` is only a
description, replaced atomically, which is why the lock sits on its own
file (a rename would leave a lock on the old inode).

:func:`discover` is how a hub that just started finds the keepers a
previous hub left running: every folder whose lock it cannot take.
:func:`spawn` starts one detached from the hub, so the service manager
that restarts the hub does not take the agent with it.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import swe
from swe.paths import remote_dir_for
from swe.remote import systemd
from swe.remote.keeper import ipc
from swe.remote.keeper.log import log_dir_for
from swe.remote.protocol import key_slug

KEEPER_FILE = "keeper.json"
LOCK_FILE = "keeper.lock"
SPEC_FILE = "spec.json"
OUT_FILE = "keeper.out"
INFO_VERSION = 1
SPEC_VERSION = 1

# How long spawn waits for a new keeper to answer on its socket.
SPAWN_WAIT_S = 20.0
SPAWN_POLL_S = 0.05

# A keeper's exit codes (``python -m swe.remote.keeper``).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_ADAPTER = 3
EXIT_RUNNING = 4
EXIT_FAILED = 5
_KEEPER_EXITS = frozenset({EXIT_OK, EXIT_USAGE, EXIT_NO_ADAPTER, EXIT_FAILED})

# spawn hands the new keeper a one-time token in this variable.
TOKEN_ENV = "SWE_KEEPER_TOKEN"


class KeeperRunning(Exception):
    """Another keeper already holds this session's lock."""


class SpawnError(Exception):
    """A keeper did not come up. ``output`` is the tail of what it
    printed."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


def keepers_root(home: Path | str | None = None) -> Path:
    """``data/remote/keepers``."""
    return remote_dir_for(Path(home) if home is not None else None) / "keepers"


def keeper_dir_for(session_key: str,
                   home: Path | str | None = None) -> Path:
    """``data/remote/keepers/<slug>`` for ``session_key``."""
    return log_dir_for(session_key, home).parent


# ---------------------------------------------------------------------
# The lock and keeper.json


def take_lock(keeper_dir: Path) -> int | None:
    """An exclusive lock on ``keeper_dir``'s lock file as an open fd, or
    None when another process holds it. Closing the fd drops it."""
    fd = os.open(keeper_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def write_json(path: Path, data: dict) -> None:
    """Replace ``path`` with ``data`` atomically, mode 0600."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict | None:
    """``path`` as a JSON object, or None when it is missing or not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class KeeperInfo:
    """One keeper folder as :func:`discover` found it.

    ``alive`` is the lock's answer. For a keeper that is gone, ``state``
    is what ``keeper.json`` last said: ``exited`` after a clean stop,
    anything else means it was killed or crashed (``lost``).
    """

    slug: str
    session_key: str
    dir: Path
    alive: bool
    pid: int = 0
    socket: str = ""
    ipc_version: int = 0
    ipc_min: int = 0
    state: str = ""
    harness: str = ""
    harness_session_id: str = ""
    started_at: float = 0.0
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def lost(self) -> bool:
        """Gone without a clean stop: its open turn was cut off."""
        return not self.alive and self.state != "exited"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else 0


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else 0.0


def inspect(keeper_dir: Path) -> KeeperInfo | None:
    """What ``keeper_dir`` holds, or None when no keeper ever ran there
    (no ``keeper.json``)."""
    raw = read_json(keeper_dir / KEEPER_FILE)
    if raw is None:
        return None
    fd = take_lock(keeper_dir)
    alive = fd is None
    if fd is not None:
        os.close(fd)
    ipc_info = raw.get("ipc") if isinstance(raw.get("ipc"), dict) else {}
    return KeeperInfo(
        slug=keeper_dir.name,
        session_key=_text(raw.get("session")),
        dir=keeper_dir,
        alive=alive,
        pid=_int(raw.get("pid")),
        socket=_text(raw.get("socket")),
        ipc_version=_int(ipc_info.get("version")),
        ipc_min=_int(ipc_info.get("min")),
        state=_text(raw.get("state")),
        harness=_text(raw.get("harness")),
        harness_session_id=_text(raw.get("harness_session_id")),
        started_at=_num(raw.get("started_at")),
        raw=raw,
    )


def discover(home: Path | str | None = None, *,
             alive_only: bool = False) -> list[KeeperInfo]:
    """Every keeper folder under ``home``, oldest start first. A hub
    reconnects to the ones that are ``alive`` and records a lost turn
    for the ones that are ``lost``."""
    root = keepers_root(home)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    found = []
    for name in names:
        folder = root / name
        if not folder.is_dir():
            continue
        info = inspect(folder)
        if info is not None and (info.alive or not alive_only):
            found.append(info)
    found.sort(key=lambda i: (i.started_at, i.slug))
    return found


def find(session_key: str,
         home: Path | str | None = None) -> KeeperInfo | None:
    """The keeper folder of ``session_key``, if a keeper ever ran."""
    return inspect(keeper_dir_for(session_key, home))


def connect(info: KeeperInfo, *, last_seq: int | None = None,
            **kwargs: Any) -> ipc.KeeperClient:
    """A connected client for a live keeper. The hub speaks whatever
    version the keeper does, down to ``ipc.MIN_VERSION``."""
    client = ipc.KeeperClient(info.socket, last_seq=last_seq, **kwargs)
    client.connect()
    return client


# ---------------------------------------------------------------------
# spec.json: what a keeper starts


_SPEC_TEXT = ("harness", "cwd", "resume_id", "model", "effort",
              "permission_mode", "session_id")


def write_spec(session_key: str, *, harness: str, cwd: str,
               resume_id: str = "", fork: bool = False, model: str = "",
               effort: str = "", permission_mode: str = "",
               plan_mode: bool = False, env: dict | None = None,
               first_seq: int = 1, session_id: str = "",
               home: Path | str | None = None) -> Path:
    """Write the keeper's ``spec.json`` and return its path.

    ``env`` is extra environment for the agent and is stored in the
    file: nothing secret goes in it. A secret reaches the agent through
    the keeper's own environment (:func:`spawn`'s ``env``).
    """
    if not harness or not cwd.startswith("/"):
        raise ValueError("a spec needs a harness and an absolute cwd")
    if first_seq < 1:
        raise ValueError("first_seq must be at least 1")
    folder = keeper_dir_for(session_key, home)
    log_dir_for(session_key, home).mkdir(parents=True, exist_ok=True,
                                         mode=0o700)
    os.chmod(folder, 0o700)
    spec = {
        "v": SPEC_VERSION, "session": session_key, "harness": harness,
        "cwd": cwd, "resume_id": resume_id, "fork": bool(fork),
        "model": model, "effort": effort,
        "permission_mode": permission_mode, "plan_mode": bool(plan_mode),
        "env": {str(k): str(v) for k, v in (env or {}).items()},
        "first_seq": int(first_seq), "session_id": session_id,
    }
    write_json(folder / SPEC_FILE, spec)
    return folder / SPEC_FILE


def update_spec(session_key: str, home: Path | str | None = None,
                **changes: Any) -> bool:
    """Change fields of a stored spec (the keeper records an option
    change here, so the next keeper starts the agent the same way).
    False when there is no spec for this session."""
    path = keeper_dir_for(session_key, home) / SPEC_FILE
    raw = read_json(path)
    if raw is None or raw.get("session") != session_key:
        return False
    raw.update(changes)
    write_json(path, raw)
    return True


def read_spec(session_key: str,
              home: Path | str | None = None) -> dict | None:
    """The stored spec, with every field present and typed, or None
    when there is none for this session."""
    raw = read_json(keeper_dir_for(session_key, home) / SPEC_FILE)
    if raw is None or raw.get("session") != session_key:
        return None
    spec: dict[str, Any] = {name: _text(raw.get(name)) for name in _SPEC_TEXT}
    spec["session"] = session_key
    spec["fork"] = raw.get("fork") is True
    spec["plan_mode"] = raw.get("plan_mode") is True
    env = raw.get("env")
    spec["env"] = {k: v for k, v in env.items()
                   if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(env, dict) else {}
    spec["first_seq"] = max(1, _int(raw.get("first_seq")))
    if not spec["harness"] or not spec["cwd"].startswith("/"):
        return None
    return spec


# ---------------------------------------------------------------------
# Starting a keeper


def keeper_argv(session_key: str, python: str | None = None) -> list[str]:
    """The command line of one keeper."""
    return [python or sys.executable, "-m", "swe.remote.keeper",
            f"--session={session_key}"]


def _child_env(env: dict | None, runtime: Path | str | None) -> dict:
    """The keeper's environment: the caller's, ``env`` on top, and the
    ``swe`` package this process runs first on the import path, so the
    keeper runs the same code as the hub that started it."""
    out = dict(os.environ)
    out.update(env or {})
    root = str(Path(swe.__file__).resolve().parent.parent)
    parts = [p for p in out.get("PYTHONPATH", "").split(os.pathsep) if p]
    if root not in parts:
        out["PYTHONPATH"] = os.pathsep.join([root, *parts])
    if runtime is not None:
        out[ipc.RUNTIME_ENV] = str(runtime)
    return out


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def spawn(
    session_key: str,
    *,
    home: Path | str | None = None,
    runtime: Path | str | None = None,
    env: dict | None = None,
    python: str | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    scope: Callable[[str], list[str] | None] | None = None,
    wait_s: float = SPAWN_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> KeeperInfo:
    """Start the keeper for ``session_key`` (its ``spec.json`` must be
    written) and return it once it answers on its socket.

    The keeper is its own session and process group
    (``start_new_session``). On Linux under a systemd user manager it
    also runs in its own scope unit (``systemd.keeper_scope_argv``), so
    stopping the hub's service leaves it alone; ``scope`` overrides that
    choice (it returns the argv prefix, or None for a plain child).
    Secrets for the agent go in ``env``: they reach the keeper as
    process environment, never on a command line or on disk.

    Raises :class:`KeeperRunning` when a keeper already holds the
    session, and :class:`SpawnError` when the new one exits or does not
    answer within ``wait_s`` seconds.
    """
    folder = keeper_dir_for(session_key, home)
    if read_spec(session_key, home) is None:
        raise SpawnError(f"no spec.json for {session_key}; write_spec first")
    existing = inspect(folder)
    if existing is not None and existing.alive:
        raise KeeperRunning(f"a keeper already holds {session_key}")
    slug = key_slug(session_key)
    child_env = _child_env(env, runtime)
    if home is not None:
        child_env["HOME"] = str(home)
    # The keeper copies this into keeper.json, so the wait below cannot
    # mistake the file a previous keeper left for the new one's.
    token = secrets.token_hex(8)
    child_env[TOKEN_ENV] = token
    argv = keeper_argv(session_key, python)
    prefix = (scope or systemd.keeper_scope_argv)(slug)
    attempts = [prefix + argv, argv] if prefix else [argv]
    last = ""
    for attempt in attempts:
        with open(folder / OUT_FILE, "ab") as out:
            os.chmod(folder / OUT_FILE, 0o600)
            proc = popen(attempt, stdin=subprocess.DEVNULL, stdout=out,
                         stderr=out, cwd="/", env=child_env,
                         start_new_session=True, close_fds=True)
        deadline = clock() + wait_s
        while True:
            info = inspect(folder)
            if info is not None and info.alive \
                    and info.raw.get("token") == token:
                # Reap the child whenever it ends, so a keeper that
                # outlives this call never lingers as a zombie.
                threading.Thread(target=proc.wait, daemon=True,
                                 name=f"swe-keeper-reap-{slug}").start()
                return info
            code = proc.poll()
            if code is not None:
                last = (f"the keeper for {session_key} exited with "
                        f"status {code}")
                if code == EXIT_RUNNING:
                    raise KeeperRunning(last)
                if code in _KEEPER_EXITS:
                    # The keeper itself ran and gave up; the plain
                    # child would only fail the same way.
                    raise SpawnError(last, _tail(folder / OUT_FILE))
                break
            if clock() >= deadline:
                proc.kill()
                proc.wait()
                raise SpawnError(
                    f"the keeper for {session_key} did not come up in "
                    f"{wait_s:g} s", _tail(folder / OUT_FILE))
            sleep(SPAWN_POLL_S)
        # The scope wrapper failed (no user manager on this login): the
        # plain child is the fallback.
    raise SpawnError(last, _tail(folder / OUT_FILE))
