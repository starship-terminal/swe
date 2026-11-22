"""What a session's options are now: the ``session.options`` payload.

Each adapter's state machine knows the values its agent reported (a
model, a mode, an effort) and, where the agent lists them, the values it
offers. :class:`OptionsView` turns that into the ``session.options``
event of ``events.py`` and emits it only when something changed, so a
client is told at start and on every change and never on a timer
(docs/specs/G32-session-options.md).

Only the names the session's ``Caps`` allow are carried: an option the
channel cannot change is not offered, whatever the agent reports. The
choices are always the agent's own list, never a table written here.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable

from swe.remote.adapters import events as ev
from swe.remote.adapters.base import Caps

NEXT_TURN = "next_turn"
REPLY_FINISHES = "reply_finishes"
UNVERIFIED = "unverified"


def choice(cid: str, label: Any = "", description: Any = "") -> dict:
    """One listed value: the agent's id, and its own name and
    description where it gave them (and they say more than the id)."""
    out: dict[str, Any] = {"id": cid}
    if isinstance(label, str) and label and label != cid:
        out["label"] = label
    if isinstance(description, str) and description:
        out["description"] = description
    return out


def string_option(value: Any, mid_turn: str, *,
                  choices: Iterable[dict] | None = None,
                  reported: Any = "") -> dict:
    """A string option: the value in force ("" when the agent has not
    said), the agent's list when it gave one, and the agent's own
    spelling of the value when it differs from the listed id."""
    value = value if isinstance(value, str) else ""
    out: dict[str, Any] = {"value": value, "mid_turn": mid_turn}
    listed = [dict(c) for c in choices or () if isinstance(c, dict)
              and isinstance(c.get("id"), str) and c["id"]]
    if choices is not None and listed:
        out["choices"] = listed
    if isinstance(reported, str) and reported and reported != value:
        out["reported"] = reported
    return out


def flag_option(value: Any, mid_turn: str) -> dict:
    return {"value": bool(value), "mid_turn": mid_turn}


def allowed(options: dict, caps: Caps) -> dict:
    """``options`` with only the names ``caps`` lets ``set_option``
    change."""
    return {name: opt for name, opt in options.items()
            if name in ev.OPTION_NAMES and caps.can_set(name)}


class OptionsView:
    """Remembers the last ``session.options`` emitted, so a state
    machine can ask after any change and emit only a real one."""

    def __init__(self) -> None:
        self.last: dict | None = None

    def changed(self, session: str, options: dict, caps: Caps,
                ts: float) -> dict | None:
        """The event to emit for ``options`` under ``caps``, or None
        when it says what the last one said."""
        shown = allowed(options, caps)
        # A session that never offered anything says nothing: no
        # event is the same as an empty one to a client.
        if shown == self.last or (self.last is None and not shown):
            return None
        self.last = copy.deepcopy(shown)
        return ev.session_options(session, shown, ts=ts)
