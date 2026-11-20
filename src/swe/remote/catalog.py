"""Session registry: local + satellite rows, machine-scoped.

The catalog turns ``swe session`` rows into the phone's ``/v1/sessions``
payload: every ``SessionRow.to_json`` field verbatim, plus ``key`` (the
journal/stream name), ``machine_id``, ``ownership`` and ``model`` (the
model of the transcript's newest reply, ``session_model``). Machines register
in ``data/remote/machines.json`` — the hub writes its own row at serve
time, satellites upsert theirs over the agent channel.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from swe.paths import atomic_write_text, read_json_shared, remote_dir_for
from swe.remote import fork as fork_mod, protocol, sessionmeta
from swe.sessions.models import Session, SessionRow
from swe.sessions.resume import resume_json

# How fresh the sessions list stays. The aggregator's own 60s disk cache
# keeps each refresh cheap even with every parser in the registry.
REFRESH_INTERVAL_S = 10.0

# Follow-ups, errors and interrupts leave the inbox this long after the
# session's last activity; pending approvals never do.
INBOX_MAX_AGE_S = 3 * 86_400
INBOX_STATUSES = frozenset({"followup", "error", "interrupted"})


def load_machine_id(home: Path | str | None = None) -> str:
    """This machine's stable remote id, minted on first use.

    Lives in ``data/remote/machine.json`` — independent of hostname so a
    rename does not orphan journals or registry rows.
    """
    path = remote_dir_for(home) / "machine.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
    except Exception:
        pass
    machine_id = uuid.uuid4().hex[:16]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"id": machine_id}))
    except OSError:
        pass
    return machine_id


def detected_harnesses() -> list[str]:
    """Tool names whose launch binary is on this machine's PATH."""
    from swe.sessions.resume import (
        LIMITED_RESUME,
        RESUME_FLAGS,
        launch_tool_name,
    )

    out = []
    for tool_name in sorted(set(RESUME_FLAGS) | set(LIMITED_RESUME)):
        binary = launch_tool_name(tool_name)
        if binary and shutil.which(binary):
            out.append(tool_name)
    return out


# The Mac app's preferences: Settings > Provider visibility keeps the
# provider order and the ones switched off here. Resolved at each read
# under the current home (None); tests set a path.
APP_PREFS: Path | None = None
# The app's defaults follow its bundle id; Quiver.app's plist is the
# fallback until LegacyDefaults has carried it over.
_APP_PREFS_REL = "Library/Preferences/com.cwenlong.swe.plist"
_OLD_APP_PREFS_REL = "Library/Preferences/com.cwenlong.quiver.plist"


def _provider_visibility() -> tuple[list[str], set[str]]:
    """``(order, disabled)`` from the Mac app's Provider visibility, or
    empty when the app has never run here (a Linux hub)."""
    import plistlib

    prefs: dict = {}
    for rel in (APP_PREFS,) if APP_PREFS else (
            Path.home() / _APP_PREFS_REL, Path.home() / _OLD_APP_PREFS_REL):
        try:
            with open(rel, "rb") as fh:
                prefs = plistlib.load(fh)
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        break
    order = prefs.get("providerOrder")
    disabled = prefs.get("providerDisabled")
    return (
        [str(x) for x in order] if isinstance(order, list) else [],
        {str(x) for x in disabled} if isinstance(disabled, list) else set(),
    )


def _session_parsers() -> dict:
    """``name -> parser`` for every harness swe can list sessions of."""
    from swe.sessions.aggregator import PARSER_REGISTRY

    return {name: parser for name, parser, _keys in PARSER_REGISTRY}


def launchable_harnesses() -> list[dict]:
    """What ``POST /v1/sessions`` can start here, in Provider visibility
    order: every harness ``swe list`` shows (not archived) whose command
    is on PATH, minus the ones switched off in the Mac app's Settings.
    Each item is ``{name, command, display_name, tracked, launch_model,
    launch_effort, launch_yolo}``; ``tracked`` is false for a harness
    swe has no session parser for (qwen-code): it starts, but no session
    ever shows up to open as a chat. ``launch_model`` is true when the
    harness declares how its command line takes a model
    (``capabilities.model`` in harness.json,
    ``launch_model.MODEL_FALLBACK`` underneath); ``launch_effort`` when
    its TUI has a verified ``/effort <v>`` command, typed in once the
    pane is up; ``launch_yolo`` when it declares the argv tokens that
    switch its approval checks off (``capabilities.yolo``,
    ``launch_yolo.YOLO_FALLBACK`` underneath).
    ``supervise.fresh_launch_error`` checks all three."""
    from swe.harness import registry
    from swe.remote import launch_model, launch_yolo, slash_builtins

    try:
        reg = registry.load_registry_readonly()
    except (OSError, ValueError):
        return []
    order, disabled = _provider_visibility()
    rank = {name: i for i, name in enumerate(order)}
    parsers = _session_parsers()
    out = []
    for position, (name, entry) in enumerate(reg.items()):
        if not isinstance(entry, dict) or not registry.is_active(entry):
            continue
        if name in disabled:
            continue
        command = str(entry.get("command") or name)
        if not shutil.which(command):
            continue
        out.append((rank.get(name, len(rank) + position), {
            "name": name,
            "command": command,
            "display_name": str(entry.get("display_name") or name),
            "tracked": name in parsers,
            "launch_model": launch_model.capability(name, reg) is not None,
            "launch_effort": slash_builtins.launch_op_ok(name, "effort"),
            "launch_yolo": launch_yolo.capability(name, reg) is not None,
        }))
    out.sort(key=lambda pair: pair[0])
    return [item for _rank, item in out]


def known_session_ids(
    rows: list[tuple[str, Session, str]], harness: str, cwd: str
) -> list[str]:
    """Ids of ``harness`` sessions already in ``cwd``, for adoption to
    refuse. The catalog can be a minute old, so the harness's own parser
    runs too: a session started since then is known as well."""
    real_cwd = os.path.realpath(cwd)

    def here(session: Session) -> bool:
        return (session.tool_name == harness
                and os.path.realpath(session.path or "") == real_cwd)

    known = {s.session_id for _k, s, _st in rows if here(s)}
    parser = _session_parsers().get(harness)
    if parser is not None:
        try:
            known.update(s.session_id for s in parser() if here(s))
        except Exception:
            pass
    return sorted(known)


def harness_names(launchable: list[dict]) -> dict[str, str]:
    return {item["name"]: item["display_name"] for item in launchable}


def untracked(launchable: list[dict]) -> list[str]:
    return [item["name"] for item in launchable if not item["tracked"]]


def launch_op_names(launchable: list[dict], op: str) -> list[str]:
    """Harnesses whose launch honours ``model``/``effort`` (``launch_<op>``)."""
    return [item["name"] for item in launchable if item.get(f"launch_{op}")]


def reads_launch_options(row: dict | None) -> bool:
    """Whether a satellite's build applies ``model``/``effort`` on the
    launch, fork and attach commands it is sent.

    The release that understands those fields is the one that started
    reporting ``model_at_launch`` on its machine row, so the field's
    presence is the version check: an older agent accepts the command
    and silently drops the pick, which is exactly what this gate exists
    to prevent.
    """
    return isinstance((row or {}).get("model_at_launch"), list)


def reads_launch_yolo(row: dict | None) -> bool:
    """Whether a satellite's build applies ``yolo`` on a launch command
    it is sent — ``reads_launch_options``' twin, with ``yolo_at_launch``
    as the version check: an older agent would accept the command and
    silently run the session with its approval checks on."""
    return isinstance((row or {}).get("yolo_at_launch"), list)


def reads_launch_provider(row: dict | None) -> bool:
    """Whether a satellite's build applies ``provider`` on a launch
    command: the release that does reports ``providers_at_launch``."""
    return isinstance((row or {}).get("providers_at_launch"), dict)


def provider_option_error(row: dict | None, harness: str,
                          provider: str | None) -> str:
    """The refusal note when a satellite's row says its launch of
    ``harness`` can't take ``provider``, else "". The row lists only
    providers whose key is set on that machine, so a missing key is
    refused here too; the satellite re-checks its own keys file when
    the command lands."""
    if not provider:
        return ""
    offered = ((row or {}).get("providers_at_launch") or {}).get(harness)
    if isinstance(offered, list) and provider in offered:
        return ""
    return (f"{harness} on this machine can't take the provider "
            f"{provider!r}: harness.json doesn't declare it, or the "
            f"machine has no key for it")


def provider_fields(launchable: list[dict],
                    home: Path | str | None = None) -> dict:
    """``providers_at_launch`` (harness -> slugs whose key is set in
    this machine's keys file) and ``provider_names`` (slug -> label).
    Slugs and labels only: no key value ever goes on a machine row."""
    from swe.remote import launch_provider

    try:
        keys = launch_provider.read_keys(launch_provider.keys_path(
            Path(home) if home is not None else None))
    except ValueError:
        keys = {}
    from swe.harness import registry

    # One registry read for every harness: capability() and configured()
    # each read harness.json again when they aren't handed it.
    try:
        reg = registry.load_registry_readonly()
    except (OSError, ValueError):
        reg = {}
    offered: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for item in launchable:
        caps = launch_provider.capability(item["name"], reg) or {}
        slugs = launch_provider.configured(item["name"], keys, reg)
        if slugs:
            offered[item["name"]] = slugs
            for slug in slugs:
                names.setdefault(slug, launch_provider.label(slug, caps[slug]))
    return {"providers_at_launch": offered, "provider_names": names}


def launch_option_error(row: dict | None, harness: str,
                        model: str | None, effort: str | None,
                        yolo: bool = False) -> str:
    """The refusal note when a satellite's machine row says its launch of
    ``harness`` can't take ``model``/``effort``/``yolo``, else "".

    The satellite's own ``model_at_launch``/``effort_at_launch``/
    ``yolo_at_launch`` lists answer, not the hub's registry: what a
    launch takes is declared per machine in its harness.json
    (``capabilities.model``, ``capabilities.yolo``). The satellite
    re-checks its own registry when the command lands, so a value-shape
    rule the lists can't carry (opencode's ``provider/model``) still
    fails the launch rather than being dropped.
    """
    row = row or {}
    missing = [
        op for op, value in (("model", model), ("effort", effort),
                             ("yolo", yolo))
        if value and harness not in (row.get(f"{op}_at_launch") or [])
    ]
    if not missing:
        return ""
    what = {"model": "a model", "effort": "an effort level",
            "yolo": "a permissions bypass"}
    ops = " or ".join(what[op] for op in missing)
    return f"{harness} launches on this machine can't take {ops}"


def recent_models(home: Path | str | None = None,
                  harnesses: list[str] | None = None,
                  ) -> dict[str, list[str]]:
    """``harness`` → its recently used models, newest first (at most 5),
    for the launch pickers; see ``recent_models.recent_models``."""
    from swe.remote import recent_models as recent_mod

    return recent_mod.recent_models(home, harnesses)


def last_used_models(home: Path | str | None = None) -> dict[str, str]:
    """``harness`` → the model its most recent ledger event ran: the
    head of each ``recent_models`` list, so the same junk filter and
    OpenCode provider naming apply."""
    return {h: models[0] for h, models in recent_models(home).items()}


def launch_command(harness: str) -> str | None:
    """The command that starts ``harness``, or None when it can't start here."""
    for item in launchable_harnesses():
        if item["name"] == harness:
            return item["command"]
    return None


def tailscale_ip() -> str:
    """This machine's tailnet IPv4, or '' when Tailscale is absent."""
    from swe.remote.tsid import tailscale_exe

    exe = tailscale_exe()
    if not exe:
        return ""
    import subprocess

    try:
        out = subprocess.run(
            [exe, "ip", "-4"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    lines = out.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def local_machine(home: Path | str | None = None) -> dict:
    """This machine's ``/v1/machines`` row.

    ``launchable`` is what the phone's new-session sheet offers, in order
    (``launchable_harnesses``); ``harness_names`` names each one.
    ``model_at_launch``/``effort_at_launch``/``yolo_at_launch`` name the
    harnesses whose launches accept ``model``/``effort``/``yolo``;
    ``recent_models`` lists each
    launchable harness's recently used models, newest first (at most
    5), and ``last_models`` is the head of each list, the picker's
    default.
    """
    harnesses = detected_harnesses()
    launchable = launchable_harnesses()
    return {
        "id": load_machine_id(home),
        "name": platform.node() or "localhost",
        "os": platform.system().lower() or os.name,
        "online": True,
        "last_seen_at": time.time(),
        "harnesses": harnesses,
        **launch_fields(launchable, home),
    }


def launch_fields(launchable: list[dict],
                  home: Path | str | None = None) -> dict:
    """The machine-row fields the launch sheets read, from
    ``launchable_harnesses()``: ``launchable``, ``harness_names``,
    ``untracked_harnesses``, ``model_at_launch``, ``effort_at_launch``,
    ``yolo_at_launch``, ``recent_models`` and ``last_models``. One
    builder for the hub's own row, a satellite's and ``local_machine``."""
    recent = recent_models(home, [item["name"] for item in launchable])
    return {
        "launchable": [item["name"] for item in launchable],
        "harness_names": harness_names(launchable),
        "untracked_harnesses": untracked(launchable),
        "model_at_launch": launch_op_names(launchable, "model"),
        "effort_at_launch": launch_op_names(launchable, "effort"),
        "yolo_at_launch": launch_op_names(launchable, "yolo"),
        "recent_models": recent,
        "last_models": {h: models[0] for h, models in recent.items()},
        **provider_fields(launchable, home),
    }


def _machines_file(home: Path | None) -> Path:
    return remote_dir_for(home) / "machines.json"


def load_machines(home: Path | str | None = None) -> dict[str, dict]:
    """``machine_id → row`` for every machine the hub has heard from."""
    path = _machines_file(Path(home) if home is not None else None)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        machines = data.get("machines") if isinstance(data, dict) else None
        if isinstance(machines, dict):
            return machines
    except Exception:
        pass
    return {}


def _machines_of(data: object) -> dict[str, dict]:
    machines = data.get("machines") if isinstance(data, dict) else None
    return machines if isinstance(machines, dict) else {}


def peek_machines(home: Path | str | None = None) -> dict[str, dict]:
    """``load_machines`` for readers, parsed again only when machines.json
    changes. Shared: never mutate it or save it back."""
    path = _machines_file(Path(home) if home is not None else None)
    try:
        return read_json_shared(path, _machines_of)
    except Exception:
        return {}


_machines_lock = threading.Lock()


def upsert_machine(home: Path | None, row: dict) -> dict[str, dict]:
    """Insert or update one machine row and persist the registry.

    The whole read-modify-write runs under a lock: satellite connect,
    disconnect and register calls hit this concurrently and would
    otherwise lose each other's rows.
    """
    with _machines_lock:
        machines = load_machines(home)
        mid = str(row.get("id") or "")
        if not mid:
            return machines
        existing = machines.get(mid, {})
        machines[mid] = {**existing, **row}
        path = _machines_file(home)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                path, json.dumps({"machines": machines}, indent=1)
            )
        except OSError:
            pass
        return machines


def _satellite_dir(home: Path | None) -> Path:
    return remote_dir_for(home) / "satellite"


def _legacy_safe_mid(machine_id: str) -> str:
    """The file stem before ids that map lossily got a hash suffix."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", machine_id)
    # "..", ".", "" escape the satellite dir — collapse to a slug.
    if not safe or set(safe) <= {".", "_", "-"}:
        safe = protocol.key_slug(machine_id)
    return safe


def _safe_mid(machine_id: str) -> str:
    """``machine_id`` as a filename. An id that is already a safe name
    is used as is; any other gets a hash of the real id appended, since
    "m.1" and "m_1" would otherwise share one file and overwrite each
    other's rows, commands and usage."""
    safe = _legacy_safe_mid(machine_id)
    if safe == machine_id:
        return safe
    return f"{safe}-{hashlib.sha256(machine_id.encode()).hexdigest()[:8]}"


def is_superseded(path: Path, machine_id: str) -> bool:
    """A file left under the pre-suffix name of ``machine_id`` while the
    suffixed one exists: read the new one, never both. Nothing is
    deleted: the old name may be another machine's own file."""
    stem = _safe_mid(machine_id)
    return path.stem != stem and (path.parent / f"{stem}.json").exists()


# A rows or usage file no push has refreshed for this long belongs to a
# machine that is gone (or re-paired under a new id): it is skipped, so
# the hub stops listing it. A machine that wakes later pushes again.
# (Not the "/" menu files: an agent resends those only on a change.)
SATELLITE_FILE_MAX_AGE_S = 30 * 86_400


def satellite_files(folder: Path, now: float | None = None) -> list[Path]:
    """The ``*.json`` under ``folder`` a push refreshed recently. Stale
    ones are skipped, not deleted: a push can replace one at any moment
    and a delete here would race it."""
    now = time.time() if now is None else now
    out = []
    try:
        files = list(folder.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            if now - f.stat().st_mtime > SATELLITE_FILE_MAX_AGE_S:
                continue
        except OSError:
            continue
        out.append(f)
    return out


# supervise.tmux_name: "swe-" and a hex slug of the session key, or the
# "qv-" a satellite from before the rename (or its older panes) uses.
_PANE_NAME = re.compile(r"(?:swe|qv)-[0-9a-f]{1,32}")


def merge_remote_rows(
    home: Path | None, machine_id: str, rows: list[dict]
) -> list[dict]:
    """Persist a satellite's catalog push; rows get ``@mid`` keys.

    The file lets the hub keep showing a sleeping satellite's sessions
    (marked offline) instead of dropping them on restart.
    """
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        local = str(row.get("key") or "")
        row["key"] = protocol.remote_key(machine_id, local)
        row["machine_id"] = machine_id
        row["remote"] = True
        # The pane name reaches the web app's markup and the attach line
        # people paste into a shell, so only a name supervise could have
        # made gets through.
        if "tmux" in row and not _PANE_NAME.fullmatch(str(row["tmux"] or "")):
            del row["tmux"]
        out.append(row)
    path = _satellite_dir(home)
    try:
        path.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path / f"{_safe_mid(machine_id)}.json",
            json.dumps({"rows": out}, indent=1),
        )
    except OSError:
        pass
    return out


def load_remote_rows(home: Path | None) -> list[dict]:
    """All persisted satellite rows (``(key, row)``-ready dicts)."""
    out: list[dict] = []
    for f in satellite_files(_satellite_dir(home)):
        try:
            rows = json.loads(f.read_text()).get("rows") or []
        except (OSError, ValueError):
            continue
        mid = next((str(r.get("machine_id") or "") for r in rows
                    if isinstance(r, dict)), "")
        if mid and is_superseded(f, mid):
            continue
        out.extend(r for r in rows if isinstance(r, dict))
    return out


def _supervised(home: Path | None) -> dict[str, dict]:
    """Supervised entries by key; dead tmux entries prune out on read."""
    from swe.remote import supervise

    return supervise.supervised(home)


def _kept(home: Path | None) -> dict[str, dict]:
    """Kept tmux sessions by key (observed, pane still recorded)."""
    from swe.remote import supervise

    return supervise.kept(home)


def session_to_row(
    session: Session,
    status: str,
    machine_id: str,
    supervised: dict[str, dict],
    kept: dict[str, dict] | None = None,
    model: str = "",
) -> tuple[str, dict]:
    """``Session`` → (key, ``/v1/sessions`` row).

    An attached row carries the supervised ``mode`` and, for tmux mode,
    the ``swe-…`` pane name. An observed row left running by a ``keep``
    detach carries ``kept`` and the pane name, so a client can offer the
    pane back: attach adopts it live instead of relaunching.

    ``model`` is the model the transcript's newest reply ran, left out
    when unknown. ``model_modes`` lists the supervised modes in which the
    ``model`` control op works for this harness, so a client offers a
    model switch only on an attached row whose ``mode`` is in it.
    """
    from swe.remote import session_model

    key = protocol.session_key(
        session.tool_name, session.session_id, session.path
    )
    meta = supervised.get(key)
    row = SessionRow.of(session, status, resume_json(session)).to_json()
    row["key"] = key
    row["machine_id"] = machine_id
    row["ownership"] = (
        protocol.OWNERSHIP_ATTACHED
        if meta is not None else protocol.OWNERSHIP_OBSERVED
    )
    if meta is not None:
        if meta.get("mode"):
            row["mode"] = meta["mode"]
        if meta.get("tmux"):
            row["tmux"] = meta["tmux"]
    elif kept and key in kept:
        row["kept"] = True
        if kept[key].get("mode"):
            row["mode"] = kept[key]["mode"]
        if kept[key].get("tmux"):
            row["tmux"] = kept[key]["tmux"]
    # Whether Attach is even possible: a resumable harness on this
    # machine, or a kept pane attach can adopt without resume flags.
    row["attachable"] = row["resume"] is not None or bool(row.get("kept"))
    # Whether Fork is: the harness's CLI has a fork command line.
    row["can_fork"] = fork_mod.can_fork(session)
    if model:
        row["model"] = model
    row["model_modes"] = session_model.model_modes(session.tool_name)
    return key, row


# A session a keeper holds (remote/runtime.py): how its row says so.
KEEPER_MODE = "keeper"


def _own_row(row: dict, owned: dict) -> dict:
    """``row`` as the list shows a session SWE drives through a keeper:
    attached, with the keeper's own state in place of a status guessed
    from the transcript."""
    row = dict(row)
    caps = owned.get("caps") or {}
    row["key"] = owned["key"]
    row["ownership"] = protocol.OWNERSHIP_ATTACHED
    row["mode"] = KEEPER_MODE
    row.pop("tmux", None)
    row.pop("kept", None)
    row["control"] = "full"
    row["state"] = owned.get("state") or ""
    row["status"] = owned.get("status") or None
    row["adapter"] = owned.get("adapter") or ""
    row["caps"] = caps
    row["attachable"] = True
    row["can_fork"] = bool(caps.get("fork"))
    if owned.get("model") and not row.get("model"):
        row["model"] = owned["model"]
    modes = [m for m in row.get("model_modes") or [] if m != KEEPER_MODE]
    if "model" in (caps.get("options") or []):
        modes.append(KEEPER_MODE)
    row["model_modes"] = modes
    active = owned.get("active_at") or 0
    if isinstance(active, (int, float)) and active > (
            row.get("last_active_at") or 0):
        row["last_active_at"] = active
    return row


def overlay_owned(
    rows: list[tuple[str, Session | None, dict, str]],
    owned: list[dict],
    machine_id: str,
) -> list[tuple[str, Session | None, dict, str]]:
    """``rows`` with every keeper-held session in it once, under the
    hub's key for it.

    A session whose transcript the aggregator found is the same row
    with the keeper's fields on top (matched by harness and the
    harness's own session id, so it is one row even if the two keys
    differ). One with no transcript yet (just launched) gets a row of
    its own, first in the list.
    """
    if not owned:
        return rows
    by_ident = {(o["harness"], o["harness_session_id"]): o for o in owned}
    seen: set[str] = set()
    out: list[tuple[str, Session | None, dict, str]] = []
    for key, session, row, status in rows:
        o = by_ident.get((session.tool_name, session.session_id)) \
            if session is not None else None
        if o is None or o["key"] in seen:
            if o is None:
                out.append((key, session, row, status))
            continue
        seen.add(o["key"])
        out.append((o["key"], session, _own_row(row, o),
                    o.get("status") or ""))
    fresh = []
    for o in owned:
        if o["key"] in seen:
            continue
        cwd = o.get("cwd") or ""
        session = Session(
            timestamp=int((o.get("active_at") or time.time()) * 1000),
            agent=o["harness"], path=cwd,
            title=o.get("title") or os.path.basename(cwd.rstrip("/"))
            or o["harness"],
            session_id=o["harness_session_id"], tool_name=o["harness"])
        _key, row = session_to_row(session, o.get("status") or "",
                                   machine_id, {})
        fresh.append((o["key"], session, _own_row(row, o),
                      o.get("status") or ""))
    return fresh + out


def _index_of(rows: list[tuple]) -> dict[str, int]:
    """key -> position of its first row, as a scan would find it."""
    index: dict[str, int] = {}
    for i, entry in enumerate(rows):
        index.setdefault(entry[0], i)
    return index


class Catalog:
    """The hub-side session list: refresh on a timer, serve instantly."""

    def __init__(
        self,
        home: Path | str | None = None,
        machine_id: str | None = None,
        fetch_sessions: Callable[[], list[Session]] | None = None,
        status_of: Callable[[list[Session]], list[str]] | None = None,
        fetch_github: Callable[[], list[dict] | None] | None = None,
        model_of: Callable[[list[Session]], list[str]] | None = None,
    ) -> None:
        self.home = Path(home) if home is not None else None
        self.machine_id = machine_id or load_machine_id(self.home)
        if fetch_sessions is None:
            from swe.sessions import aggregator

            def fetch_sessions() -> list[Session]:
                # While a launch's marker waits for adoption the 60 s
                # disk cache would hide the new transcript for the rest
                # of its TTL. Invalidating rewrites the cache with the
                # fresh scan too, so the reads after adoption still see
                # the session; the marker is deleted on adoption, so
                # this only costs anything during a launch window.
                from swe.remote import supervise

                if supervise.pending_launch(self.home):
                    aggregator.invalidate_cache()
                return aggregator.get_all_sessions(limit=None, use_cache=True)
        if status_of is None:
            from swe.sessions.status import session_statuses

            status_of = session_statuses
        if model_of is None:
            from swe.remote.session_model import session_models

            model_of = session_models
        self._fetch = fetch_sessions
        self._status_of = status_of
        self._model_of = model_of
        # The GitHub notification feed is hub-only: serve() wires it, so
        # a bare catalog — tests, the satellite agent — never reaches for
        # ``gh``. ``fetch_github`` returns the unread ``github`` inbox
        # rows or None on a failed read; ``on_github_alert`` runs once
        # per thread that newly turned unread.
        self.fetch_github = fetch_github
        self.on_github_alert: Callable[[dict], None] | None = None
        # The sessions a keeper holds, set by the hub
        # (``runtime.SessionRuntime.rows``); ``read_only_reason`` says
        # why SWE let go of one it used to drive.
        self.owned_rows: Callable[[], list[dict]] | None = None
        self.read_only_reason: Callable[[str], str | None] | None = None
        self._github_rows: list[dict] = []
        # gh_thread_id → updated_at of the last-seen unread state; None
        # until the first successful poll, which is a silent baseline —
        # a hub start must not re-push the whole unread backlog.
        self._github_seen: dict[str, str] | None = None
        # The rows and a key -> position index built with them, swapped
        # as one value so a lookup never pairs one list with the other's
        # index. ``row_for``/``session_for`` run per request: a scan of
        # thousands of rows each time was the hub's hottest loop.
        self._table: tuple[list[tuple[str, Session | None, dict, str]],
                           dict[str, int]] = ([], {})
        # One lock for every write to the rows after they are built:
        # refresh's overlay-and-swap, reapply_meta (pins and titles) and
        # note_status (tail probes). Each takes it for its whole
        # read-modify-write, so none is lost to another's older read.
        self._meta_lock = threading.Lock()
        self._lock = self._meta_lock
        # Test seam: runs between refresh's metadata read and row swap.
        self._after_meta_read: Callable[[], None] | None = None
        # Statuses a tailer probed after this catalog's own read, keyed
        # by session: ``(status, probed_at)``. A refresh that started
        # before ``probed_at`` keeps these instead of its own (older)
        # verdict; one that started after drops them. ``_status_as_of``
        # is when the current rows' statuses were read.
        self._fresh: dict[str, tuple[str, float]] = {}
        self._status_as_of = 0.0

    def refresh(self) -> list[dict]:
        """Re-list sessions, reclassify statuses, rebuild rows."""
        started = time.time()
        try:
            sessions = self._fetch()
        except Exception:
            sessions = []
        try:
            statuses = self._status_of(sessions)
        except Exception:
            statuses = [""] * len(sessions)
        try:
            models = self._model_of(sessions)
        except Exception:
            models = []
        if len(models) != len(sessions):
            models = [""] * len(sessions)
        supervised = _supervised(self.home)
        kept = _kept(self.home)
        rows: list[tuple[str, Session | None, dict, str]] = []
        for session, status, model in zip(sessions, statuses, models):
            key, row = session_to_row(
                session, status, self.machine_id, supervised, kept, model
            )
            if self.read_only_reason is not None:
                reason = self.read_only_reason(key)
                if reason:
                    row["control"] = "read_only"
                    row["control_reason"] = reason
            rows.append((key, session, row, status))
        if self.owned_rows is not None:
            try:
                rows = overlay_owned(rows, self.owned_rows(),
                                     self.machine_id)
            except Exception:
                pass  # the list without keeper fields beats no list
        # Satellite sessions travel as row dicts (no Session object — the
        # satellite owns execution); remote entries stay out of tail_rows.
        for row in load_remote_rows(self.home):
            rows.append((
                str(row.get("key") or ""), None, row,
                str(row.get("status") or ""),
            ))
        # Read the pins, overlay them, keep fresher probed statuses and
        # swap the rows under one lock: reapply_meta and note_status take
        # it too, so a pin saved or a status probed meanwhile is applied
        # to the new rows, never lost to this older read.
        with self._meta_lock:
            meta = sessionmeta.load(self.home)
            if self._after_meta_read is not None:
                self._after_meta_read()
            # Fresher-wins: a tailer probe newer than this read stays.
            self._fresh = {
                k: v for k, v in self._fresh.items() if v[1] > started
            }
            for i, (key, session, row, status) in enumerate(rows):
                sessionmeta.apply(row, meta.get(key))
                fresh = self._fresh.get(key)
                if fresh is not None and session is not None:
                    row["status"] = fresh[0] or None
                    rows[i] = (key, session, row, fresh[0])
            self._table = (rows, _index_of(rows))
            self._status_as_of = started
        return [row for _key, _s, row, _st in rows]

    def note_status(self, key: str, status: str, at: float) -> None:
        """A tailer probed ``key`` at ``at``; patch its row now.

        Ignored when the catalog's own read is newer. Otherwise the row
        changes in place and the next refresh keeps this status unless
        that refresh started after ``at``.
        """
        with self._lock:
            if at <= self._status_as_of:
                return
            self._fresh[key] = (status, at)
            rows, index = self._table
            i = index.get(key)
            if i is not None and rows[i][1] is not None:
                k, session, row, _st = rows[i]
                row["status"] = status or None
                rows[i] = (k, session, row, status)

    def patch(self, key: str, fields: dict, status: str) -> None:
        """Set ``fields`` on ``key``'s row now (a keeper reported its
        state): the next refresh builds the same values itself."""
        with self._lock:
            rows, index = self._table
            i = index.get(key)
            if i is not None:
                k, session, row, _st = rows[i]
                row.update(fields)
                rows[i] = (k, session, row, status)

    def tail_snapshot(self) -> tuple[list[tuple[str, Session, str]], float]:
        """``tail_rows()`` plus when their statuses were read, together."""
        with self._lock:
            return self.tail_rows(), self._status_as_of

    def rows(self) -> list[dict]:
        return [row for _key, _s, row, _st in self._rows]

    @property
    def refreshed_at(self) -> float:
        """When the newest refresh started (epoch s); 0 before the first."""
        return self._status_as_of

    def reapply_meta(self, key: str) -> dict | None:
        """Re-overlay one row's pin and title after a write; returns the row."""
        with self._meta_lock:
            row = self.row_for(key)
            if row is not None:
                sessionmeta.apply(row, sessionmeta.get(self.home, key))
            return row

    def tail_rows(self) -> list[tuple[str, Session, str]]:
        """``(key, Session, status)`` for the tail manager, newest first."""
        return [
            (key, session, status)
            for key, session, _row, status in self._rows
            if session is not None
        ]

    @property
    def _rows(self) -> list[tuple[str, Session | None, dict, str]]:
        return self._table[0]

    def row_for(self, key: str) -> dict | None:
        rows, index = self._table
        i = index.get(key)
        return rows[i][2] if i is not None else None

    def session_for(self, key: str) -> Session | None:
        rows, index = self._table
        i = index.get(key)
        return rows[i][1] if i is not None else None

    def poll_github(self) -> None:
        """Fetch the unread GitHub listing, store its rows, and fire
        ``on_github_alert`` once per thread that newly turned unread.

        Kept out of ``refresh()``: the session path — adoption, tail
        sync — never waits on ``gh``; the catalog loop calls this after
        its session work, on the same cadence. The fetch runs unlocked;
        the dedupe and the row swap land under ``_meta_lock`` so two
        overlapping callers can't compare against the same old map and
        repeat an alert. A failed read changes nothing — the last good
        rows and the dedupe map both stay. The first successful poll is
        a silent baseline: a hub start must not re-push the whole
        unread backlog.
        """
        if self.fetch_github is None:
            return
        try:
            rows = self.fetch_github()
        except Exception:
            return
        if not isinstance(rows, list):
            return
        rows = [
            r for r in rows
            if isinstance(r, dict) and str(r.get("key") or "")
        ]
        with self._meta_lock:
            for row in rows:
                row.setdefault("machine_id", self.machine_id)
            prev = self._github_seen
            seen: dict[str, str] = {}
            alerts = []
            for row in rows:
                tid = str(row.get("gh_thread_id") or "")
                if not tid:
                    continue
                stamp = str(row.get("updated_at") or "")
                seen[tid] = stamp
                # Dedupe on gh_thread_id + updated_at: a thread already
                # seen unread at this stamp can't alert again until
                # GitHub moves it.
                if prev is not None and prev.get(tid) != stamp:
                    alerts.append(row)
            self._github_seen = seen
            self._github_rows = rows
        if self.on_github_alert is not None:
            for row in alerts:
                try:
                    self.on_github_alert(row)
                except Exception:
                    pass  # a broken notifier must not stall the loop

    def inbox(
        self,
        pending: dict[str, str] | None = None,
        dismissed: dict[str, float] | None = None,
        include_done: bool = False,
        now: float | None = None,
    ) -> list[dict]:
        """Pending decisions: sessions asking for input or in trouble.

        ``pending`` maps session key -> outstanding approval request id.
        A session waiting on an approval belongs in the inbox even when
        its transcript status is still ``active``, and it stays there
        whatever its age or any dismissal: it blocks an agent until
        answered.

        Follow-ups, errors and interrupts older than ``INBOX_MAX_AGE_S``
        drop out. ``dismissed`` maps key -> the ``last_active_at`` a
        device cleared; the item stays hidden until it moves past that.
        ``include_done`` adds sessions that finished since local
        midnight as ``done`` items.

        Unread GitHub notifications follow as one group after the
        session rows, newest first; they are all unread by construction,
        so the age cutoff never applies to them. Their ``key`` is
        ``gh:<thread id>``, which can't collide with a session key, so
        the same per-device dismissal store hides them — until GitHub
        bumps the thread's ``updated_at``.
        """
        pending = pending or {}
        dismissed = dismissed or {}
        now = time.time() if now is None else now
        cutoff = now - INBOX_MAX_AGE_S
        midnight = _local_midnight(now)
        items = []
        for _key, _s, row, status in self._rows:
            rid = pending.get(row["key"])
            last = _num(row.get("last_active_at"))
            if rid is not None:
                status = "approval"
            elif status in INBOX_STATUSES:
                if last < cutoff:
                    continue
            elif not (include_done and status == "done" and last >= midnight):
                continue
            if rid is None and row["key"] in dismissed and (
                last <= dismissed[row["key"]]
            ):
                continue
            # Satellite rows arrive as pushed: an older agent may leave
            # a field out, which must not fail the whole inbox.
            item = {
                "key": row["key"],
                "machine_id": row.get("machine_id"),
                "harness": row.get("harness"),
                "agent": row.get("agent"),
                "title": row.get("title"),
                "cwd": row.get("cwd"),
                "status": status,
                "last_active_at": row.get("last_active_at"),
            }
            if rid is not None:
                item["request_id"] = rid
            items.append(item)
        github = []
        for row in self._github_rows:
            last = _num(row.get("last_active_at"))
            if row["key"] in dismissed and last <= dismissed[row["key"]]:
                continue
            github.append(dict(row))
        github.sort(key=lambda r: _num(r.get("last_active_at")),
                    reverse=True)
        items.extend(github)
        return items


def _num(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _local_midnight(now: float) -> float:
    """Start of ``now``'s day in the hub's local time."""
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
