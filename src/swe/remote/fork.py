"""Fork: a new harness run that continues from an existing session.

``POST /v1/sessions/{key}/fork {prompt?}`` starts the harness under tmux
with its own fork command line (or, for a harness SWE drives through a
session keeper and whose adapter can fork, a new keeper that forks by
the harness's own id: ``request``), so the new run gets the source's history
and a new session id while the source stays as it was. It is tracked like
a ``POST /v1/sessions`` launch: a pending marker lets the catalog adopt
the new transcript, and ``GET /v1/launches/{id}`` names its key.

Only harnesses whose CLI has a fork command line are in ``FORK_SUPPORT``;
every other harness answers 409 ``fork_unsupported``. Each entry was read
off the installed binary's ``--help`` on the date in ``checked``. Not in
the table, checked the same day: opencode 2.1.3 (``--fork`` exists only
on the one-shot ``opencode run``, not the TUI), devin 3000.11.92,
cursor-agent 2026.10.22 and cline 3.0.73 (no fork option at all).
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from swe.remote import protocol, supervise
from swe.sessions.models import Session
from swe.sessions.resume import launch_tool_name


@dataclass(frozen=True)
class ForkSpec:
    """One harness's fork command line.

    ``build(session_id, new_id)`` returns the args after the binary, or
    [] when this session has no id the CLI takes. ``presets_id``: the
    command line names the new session's id up front, so adoption can
    wait for exactly that id.
    """

    build: Callable[[str, str], list[str]]
    checked: str
    version: str
    presets_id: bool = False


def _codex(sid: str, _new: str) -> list[str]:
    from swe.sessions.parsers import codex_thread_id

    thread = codex_thread_id(sid)
    return ["fork", thread] if thread else []


FORK_SUPPORT: dict[str, ForkSpec] = {
    # "--session-id can only be used with --continue or --resume if
    # --fork-session is also specified" (the binary's own error text).
    "claude": ForkSpec(
        lambda sid, new: (["--resume", sid, "--fork-session",
                           "--session-id", new] if sid and new else []),
        checked="2026-10-24", version="2.1.308", presets_id=True,
    ),
    # `codex fork [SESSION_ID] [PROMPT]`: the thread uuid, as for resume.
    "codex": ForkSpec(_codex, checked="2026-10-24", version="0.163.0"),
    # "--fork: fork the session when continuing (use with --continue or
    # --session)".
    "kilo": ForkSpec(
        lambda sid, _new: ["--session", sid, "--fork"] if sid else [],
        checked="2026-10-24", version="7.8.15",
    ),
    # "--fork <path|id>: Fork specific session file or partial UUID into
    # a new session".
    "pi": ForkSpec(
        lambda sid, _new: ["--fork", sid] if sid else [],
        checked="2026-10-24", version="1.3.0",
    ),
    # "--fork <sessionId>: Fork and resume a session".
    "droid": ForkSpec(
        lambda sid, _new: ["--fork", sid] if sid else [],
        checked="2026-10-24", version="0.219.0",
    ),
}


def fork_argv(session: Session, new_id: str = "") -> list[str]:
    """The full command line that forks ``session``, or [] when none does."""
    spec = FORK_SUPPORT.get(session.tool_name)
    if spec is None or getattr(session, "mirrored", False):
        return []
    args = spec.build(session.session_id or "", new_id or str(uuid.uuid4()))
    return [launch_tool_name(session.tool_name), *args] if args else []


def can_fork(session: Session) -> bool:
    """Whether a Fork action can work for this session's row."""
    return bool(fork_argv(session, "id"))


def parse_fork_body(body: dict) -> dict:
    """The optional first message plus launch tuning; ValueError on bad."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    prompt = body.get("prompt")
    if prompt is None:
        prompt = ""
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    if len(prompt) > protocol.MAX_LAUNCH_PROMPT:
        raise ValueError(
            f"prompt is over {protocol.MAX_LAUNCH_PROMPT} characters")
    return {"prompt": prompt if prompt.strip() else "",
            "model": protocol._launch_value(body, "model"),
            "effort": protocol._launch_value(body, "effort")}


def start(
    home,
    session: Session,
    rows: list[tuple[str, Session, str]],
    prompt: str,
    launch_id: str,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[bool, str]:
    """Fork ``session`` on this machine (the hub, or a satellite's agent).

    ``rows`` is the catalog at request time: every same-cwd session of
    the harness that exists now is refused by adoption, so only the
    fork's own transcript can be claimed.
    """
    from swe.remote import catalog as catalog_mod

    spec = FORK_SUPPORT.get(session.tool_name)
    new_id = str(uuid.uuid4()) if spec and spec.presets_id else ""
    argv = fork_argv(session, new_id)
    if not argv:
        return False, f"{session.tool_name} sessions can't be forked"
    known = catalog_mod.known_session_ids(
        rows, session.tool_name, session.path or "")
    ok, note, _name = supervise.launch_fork(
        home, session, argv, prompt,
        known_ids=known, launch_id=launch_id, expect_id=new_id,
        model=model, effort=effort,
    )
    return ok, note


def _unsupported(harness: str) -> tuple[int, dict]:
    names = ", ".join(sorted(FORK_SUPPORT))
    return 409, protocol.error(
        "fork_unsupported",
        f"{harness or 'this harness'} can't fork a session from the "
        f"command line (forkable: {names})",
    )


def request(state, key: str, row: dict, body: dict | None) -> tuple[int, dict]:
    """``POST /v1/sessions/{key}/fork``: answers ``(status, payload)``.

    The source can be attached or only watched: a fork never writes to
    it. A satellite's session goes through its command journal like a
    launch; the satellite checks its own folder and reports back.
    """
    if body is None:
        return 400, protocol.error("bad_body", "unreadable JSON body")
    try:
        cmd = parse_fork_body(body)
    except ValueError as exc:
        return 422, protocol.error("bad_command", str(exc))
    prompt, model, effort = cmd["prompt"], cmd["model"], cmd["effort"]
    harness = str(row.get("harness") or "")
    cwd = str(row.get("cwd") or "")
    mid = row.get("machine_id")
    if mid and mid != state.machine_id:
        # A satellite computes can_fork for its own rows; one too old to
        # send it has no fork op either.
        if not row.get("can_fork"):
            return _unsupported(harness)
        # And one too old to know the launch-option fields would accept
        # the fork command and silently drop the pick.
        from swe.remote import catalog as catalog_mod

        machine_row = catalog_mod.load_machines(state.home).get(mid)
        if (model or effort) and not \
                catalog_mod.reads_launch_options(machine_row):
            return 409, protocol.error(
                "unsupported_launch_option",
                f"{mid} runs an agent too old to take launch options; "
                "upgrade it, or fork without them")
        # The hub's table answers for the satellite's too.
        blocked = supervise.launch_tuning_error(harness, model, effort)
        if blocked:
            return 409, protocol.error("unsupported_launch_option", blocked)
        launch_id = state.new_launch(
            mid, harness, cwd, model=model or "", effort=effort or "")
        queued = state.queue_command(mid, "fork", key, {
            "prompt": prompt, "model": model or "", "effort": effort or "",
            "launch_id": launch_id,
        })
        if queued is None:
            state.settle_launch(launch_id, state="failed",
                                note="machine is too far behind")
            return 429, protocol.error("backlog_full",
                                       "machine is too far behind")
        return 202, {"ok": True, "queued": True, "launch_id": launch_id,
                     "state": "pending", "note": "queued for satellite"}

    session = state.catalog.session_for(key)
    if session is None:
        return 404, protocol.error("unknown_session", "no such session")
    cwd = session.path or ""
    runtime = getattr(state, "runtime", None)
    if runtime is not None and runtime.can_drive(session.tool_name) \
            and runtime.resolve(session.tool_name).caps.fork:
        # Through a keeper: the adapter forks by the harness's own id
        # and names the new session up front, so nothing is adopted.
        if not session.session_id or getattr(session, "mirrored", False):
            return _unsupported(session.tool_name)
        if not cwd or not os.path.isdir(cwd):
            return 422, protocol.error(
                "bad_cwd", "the session's folder is gone from this "
                           f"machine: {cwd or '(none recorded)'}")
        from swe.remote.runtime import Refused

        launch_id = state.new_launch(
            state.machine_id, session.tool_name, cwd,
            model=model or "", effort=effort or "")
        try:
            runtime.launch(launch_id, session.tool_name, cwd, prompt,
                           model=model or "", effort=effort or "",
                           resume_id=session.session_id, fork=True)
        except Refused as exc:
            state.settle_launch(launch_id, state="failed", note=exc.message)
            return exc.status, protocol.error(exc.code, exc.message)
        return 202, {"ok": True, "launch_id": launch_id, "state": "pending",
                     "note": "forking"}
    argv = fork_argv(session)
    if not argv:
        return _unsupported(session.tool_name)
    if not cwd or not os.path.isdir(cwd):
        return 422, protocol.error(
            "bad_cwd", f"the session's folder is gone from this machine: "
                       f"{cwd or '(none recorded)'}")
    if shutil.which(argv[0]) is None:
        return 422, protocol.error(
            "not_installed", f"{argv[0]} is not installed on this machine")
    if not supervise.tmux_bin():
        return 422, protocol.error("launch_failed", "tmux is not installed")
    if not FORK_SUPPORT[session.tool_name].presets_id and \
            supervise.fork_blocker(state.home, session.tool_name, cwd):
        # supervise.launch_fork re-checks under the registry lock; this
        # is the answer the phone can show straight away.
        return 409, protocol.error(
            "fork_pending", supervise.fork_busy_note(session.tool_name))
    blocked = supervise.launch_tuning_error(
        session.tool_name, model, effort)
    if blocked:
        return 409, protocol.error("unsupported_launch_option", blocked)
    launch_id = state.new_launch(
        state.machine_id, session.tool_name, cwd,
        model=model or "", effort=effort or "")
    rows = state.catalog.tail_rows()

    def run() -> None:
        try:
            ok, note = start(state.home, session, rows, prompt, launch_id,
                             model=model, effort=effort)
        except Exception as exc:  # never leave the launch pending for a day
            ok, note = False, f"fork failed: {exc}"
        if not ok:
            state.settle_launch(launch_id, state="failed", note=note)

    threading.Thread(
        target=run, name=f"remote-fork-{launch_id}", daemon=True
    ).start()
    return 202, {"ok": True, "launch_id": launch_id, "state": "pending",
                 "note": "forking under tmux"}
