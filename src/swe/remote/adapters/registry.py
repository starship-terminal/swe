"""Which adapter drives a harness's sessions, and the adapter classes.

:func:`adapter_for` is the only reader of ``capabilities.session``
(harness.json) and of ``fallback.SESSION_FALLBACK``. It answers with a
:class:`Resolved` description or a :class:`NoAdapter` whose ``reason``
is written for a person: it becomes the session view's read-only bar.

Adapter classes register themselves by adapter id with :func:`register`
when their module is imported, so this module never names a harness and
no module outside an adapter does either.
"""

from __future__ import annotations

import importlib
import re
import shutil
import threading
from dataclasses import dataclass, field
from typing import Callable, Mapping

from swe.remote.adapters.base import Caps, OPTION_NAMES, SessionAdapter
from swe.remote.adapters.fallback import SESSION_FALLBACK

# An adapter id: what ``capabilities.session.adapter`` names.
ADAPTER_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Adapter modules shipped with SWE, imported once on first lookup so
# each registers itself. G7 adds ``claude_stream``, G16 the others.
BUILTIN_MODULES: tuple[str, ...] = ()

_BOOL_KEYS = ("steer", "plan_mode", "rollback", "fork", "stop_task")

AdapterFactory = Callable[..., SessionAdapter]

_ADAPTERS: dict[str, AdapterFactory] = {}
_LOCK = threading.Lock()
_BUILTINS_LOADED = False


@dataclass(frozen=True)
class Resolved:
    """How to drive one harness: the adapter id, the command that starts
    the harness's protocol endpoint (None: the adapter's default), the
    minimum CLI version, the caps, and where the answer came from
    (``registry`` or ``fallback``)."""

    harness: str
    adapter: str
    argv: tuple | None
    caps: Caps
    min_version: str = ""
    source: str = "fallback"
    ok: bool = field(default=True, init=False)


@dataclass(frozen=True)
class NoAdapter:
    """Why a harness cannot be driven here. ``adapter`` names the id
    that was asked for when there was one; ``source`` is ``registry``,
    ``fallback`` or ``unknown``."""

    harness: str
    reason: str
    adapter: str = ""
    source: str = "unknown"
    ok: bool = field(default=False, init=False)


def register(adapter_id: str, factory: AdapterFactory, *,
             replace: bool = False) -> None:
    """File ``factory`` (an adapter class, or a callable returning one
    adapter) under ``adapter_id``. A second registration of the same id
    is an error unless ``replace``."""
    if not ADAPTER_ID_RE.fullmatch(adapter_id or ""):
        raise ValueError(f"bad adapter id {adapter_id!r}")
    if not callable(factory):
        raise TypeError("factory must be callable")
    with _LOCK:
        if adapter_id in _ADAPTERS and not replace \
                and _ADAPTERS[adapter_id] is not factory:
            raise ValueError(f"adapter {adapter_id!r} is already registered")
        _ADAPTERS[adapter_id] = factory


def unregister(adapter_id: str) -> None:
    """Forget ``adapter_id``; a no-op when it was never registered."""
    with _LOCK:
        _ADAPTERS.pop(adapter_id, None)


def registered() -> frozenset:
    """Every adapter id with a class behind it."""
    _load_builtins()
    with _LOCK:
        return frozenset(_ADAPTERS)


def factory_for(adapter_id: str) -> AdapterFactory | None:
    _load_builtins()
    with _LOCK:
        return _ADAPTERS.get(adapter_id)


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    for name in BUILTIN_MODULES:
        importlib.import_module(f"swe.remote.adapters.{name}")


def _declared(harness: str, registry: Mapping | None) -> dict | None:
    """``capabilities.session`` for ``harness``, or None."""
    if registry is None:
        from swe.harness import registry as harness_registry

        try:
            registry = harness_registry.load_registry_readonly()
        except (OSError, ValueError):
            registry = {}
    entry = registry.get(harness) if isinstance(registry, Mapping) else None
    caps = entry.get("capabilities") if isinstance(entry, Mapping) else None
    session = caps.get("session") if isinstance(caps, Mapping) else None
    return dict(session) if isinstance(session, Mapping) else None


def _argv(value) -> tuple | None:
    """A usable argv, or None for anything else."""
    if isinstance(value, (list, tuple)) and value and all(
            isinstance(tok, str) and tok.strip() for tok in value):
        return tuple(value)
    return None


def _usable(declared: dict) -> dict:
    """The fields of a registry entry that are well formed; a field of
    the wrong type counts as not declared, so the fallback still
    answers for it."""
    out: dict = {}
    adapter = declared.get("adapter")
    if isinstance(adapter, str) and ADAPTER_ID_RE.fullmatch(adapter):
        out["adapter"] = adapter
    if "argv" in declared and _argv(declared["argv"]) is not None:
        out["argv"] = _argv(declared["argv"])
    min_version = declared.get("min_version")
    if isinstance(min_version, str) and min_version.strip():
        out["min_version"] = min_version.strip()
    for key in _BOOL_KEYS:
        if isinstance(declared.get(key), bool):
            out[key] = declared[key]
    options = declared.get("options")
    if isinstance(options, list) and all(isinstance(o, str) for o in options):
        out["options"] = frozenset(options) & (OPTION_NAMES - {"plan_mode"})
    return out


def adapter_for(
    harness: str,
    registry: Mapping | None = None,
    *,
    which: Callable[[str], str | None] | None = shutil.which,
) -> Resolved | NoAdapter:
    """How SWE drives ``harness``'s sessions on this machine.

    ``registry`` is harness.json's contents (None reads it). A
    ``capabilities.session`` entry wins field by field over
    ``SESSION_FALLBACK``; ``supported: false`` there means no adapter,
    whatever the table says. The adapter id must have a registered
    class, and a resolved ``argv``'s binary must be on PATH (``which``;
    None skips that check).
    """
    declared = _declared(harness, registry)
    if declared is not None and declared.get("supported") is False:
        reason = declared.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = (f"{harness} has no structured session protocol "
                      "SWE can drive; its sessions are read-only")
        return NoAdapter(harness, reason.strip(), source="registry")
    usable = _usable(declared) if declared else {}
    base = SESSION_FALLBACK.get(harness)
    if not usable.get("adapter") and base is None:
        return NoAdapter(
            harness,
            f"SWE does not know how to drive {harness} sessions yet; "
            "they are read-only",
        )
    merged: dict = dict(base or {})
    merged.update(usable)
    source = "registry" if usable else "fallback"
    adapter = merged["adapter"]
    if factory_for(adapter) is None:
        return NoAdapter(
            harness,
            f"this SWE build has no {adapter} session adapter yet; "
            f"{harness} sessions are read-only",
            adapter=adapter, source=source)
    argv = _argv(merged.get("argv"))
    if argv is not None and which is not None and which(argv[0]) is None:
        return NoAdapter(
            harness, f"{argv[0]} is not installed on this machine",
            adapter=adapter, source=source)
    caps = Caps(**{k: bool(merged.get(k, False)) for k in _BOOL_KEYS},
                options=merged.get("options", frozenset()))
    return Resolved(harness, adapter, argv, caps,
                    min_version=str(merged.get("min_version") or ""),
                    source=source)


def create(resolved: Resolved, **kwargs) -> SessionAdapter:
    """A new adapter for ``resolved``: its factory called with
    ``resolved`` and ``kwargs``."""
    factory = factory_for(resolved.adapter)
    if factory is None:
        raise LookupError(f"no adapter registered as {resolved.adapter!r}")
    return factory(resolved, **kwargs)
