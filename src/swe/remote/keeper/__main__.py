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
import sys

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
            harness=spec["harness"])
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
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: keeper.request_stop("signal"))
    # Detached from every terminal: a hangup is not for us.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    keeper.run()
    return discovery.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
