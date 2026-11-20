"""``python -m swe.remote.keeper --session=<key>``: one session's keeper.

Started by ``discovery.spawn`` (the hub), never by hand. It reads the
session's ``spec.json``, resolves the harness's adapter, and serves
until it is told to stop. Everything it will ever need is imported
here, at start: a reinstall of SWE that replaces the files on disk
must not change the code of a keeper that is already running.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading

from swe.remote.adapters import registry
from swe.remote.adapters.base import StartSpec
from swe.remote.keeper import discovery, ipc
from swe.remote.keeper.log import LogError
from swe.remote.keeper.process import PARK_AFTER_S, Keeper

# Every built-in adapter is imported now, with the rest: part of
# "everything at start".
registry.registered()

USAGE = "usage: python -m swe.remote.keeper --session=<key>"


def _value(argv: list[str], flag: str) -> str | None:
    for tok in argv:
        if tok.startswith(flag + "="):
            return tok[len(flag) + 1:]
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    session = _value(argv, "--session")
    if not session:
        print(USAGE, file=sys.stderr)
        return discovery.EXIT_USAGE
    park_after = PARK_AFTER_S
    raw = _value(argv, "--park-after")
    if raw is not None:
        try:
            park_after = float(raw)
        except ValueError:
            print(USAGE, file=sys.stderr)
            return discovery.EXIT_USAGE
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    spec = discovery.read_spec(session)
    if spec is None:
        print(f"keeper: no spec.json for {session}", file=sys.stderr)
        return discovery.EXIT_USAGE
    resolved = registry.adapter_for(spec["harness"])
    if not resolved.ok:
        print(f"keeper: {resolved.reason}", file=sys.stderr)
        return discovery.EXIT_NO_ADAPTER
    try:
        start = StartSpec(
            session_key=session, cwd=spec["cwd"], argv=resolved.argv,
            resume_id=spec["resume_id"], fork=spec["fork"],
            model=spec["model"], effort=spec["effort"],
            permission_mode=spec["permission_mode"],
            plan_mode=spec["plan_mode"], env=spec["env"],
            harness=spec["harness"], session_id=spec["session_id"])
    except ValueError as exc:
        print(f"keeper: bad spec.json: {exc}", file=sys.stderr)
        return discovery.EXIT_USAGE
    keeper = Keeper(session, start, lambda: registry.create(resolved),
                    first_seq=spec["first_seq"], park_after=park_after,
                    token=os.environ.get(discovery.TOKEN_ENV, ""))
    try:
        keeper.open()
    except discovery.KeeperRunning as exc:
        print(f"keeper: {exc}", file=sys.stderr)
        return discovery.EXIT_RUNNING
    except (ipc.IpcError, LogError, OSError) as exc:
        print(f"keeper: could not start: {exc}", file=sys.stderr)
        return discovery.EXIT_FAILED
    watch = _stop_on_signal(keeper)
    keeper.run()
    del watch
    return discovery.EXIT_OK


def _stop_on_signal(keeper: Keeper) -> tuple:
    """Stop the keeper on SIGTERM or SIGINT, whichever thread the
    signal lands on.

    A Python signal handler runs only when the main thread next runs
    bytecode, and the main thread here sleeps on its inbox for up to
    half an hour. The kernel may hand the signal to any other thread,
    which does not wake that sleep. So the signal's number is written
    to a socket (``set_wakeup_fd``, done in C by whichever thread got
    it) and a small thread turns it into a stop."""
    reader, writer = socket.socketpair()
    writer.setblocking(False)
    signal.set_wakeup_fd(writer.fileno(), warn_on_full_buffer=False)
    stops = (signal.SIGTERM, signal.SIGINT)
    for signum in stops:
        signal.signal(signum, lambda *_: None)
    # Detached from every terminal: a hangup is not for us.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def watch() -> None:
        while True:
            try:
                data = reader.recv(16)
            except OSError:
                return
            if not data:
                return
            if any(byte in stops for byte in data):
                keeper.request_stop("signal")

    threading.Thread(target=watch, daemon=True,
                     name="swe-keeper-signals").start()
    return reader, writer


if __name__ == "__main__":
    sys.exit(main())
