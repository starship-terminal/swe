"""The JSON schema of sync v2: ``session_v2.json`` beside this file.

It describes every frame a ``v=2`` session stream sends (the v2 events
of :mod:`swe.remote.adapters.events`, plus ``session.snapshot``,
``session.row`` and ``ping``), the snapshot body, a ``messages`` page
and what ``GET /v1/status`` answers. It is a public contract: the
session view's TypeScript types are generated from the file
(docs/specs/G9-sync-v2.md).

The file is generated, never edited by hand::

    python3 -m swe.remote.schema --write     # after changing events.py
    python3 -m swe.remote.schema --check     # exit 1 when it is stale

:func:`build` makes the schema from ``events.SCHEMAS``, so an event
field added there shows up here, and ``tests/test_remote_sse_v2.py``
fails until the committed file is written again. :func:`validate`
checks a value against it with the standard library only (the subset of
JSON Schema 2020-12 the file uses).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swe.remote import protocol
from swe.remote.adapters import events as ev

PATH = Path(__file__).with_name("session_v2.json")

SCHEMA_ID = "https://github.com/starship-terminal/swe/blob/main/" \
            "src/swe/remote/schema/session_v2.json"

# The objects events.py shares between types, by the name they get
# under ``$defs`` (and in the generated TypeScript).
_SHARED = {
    "FileEdit": ev._FILE_EDIT,
    "Option": ev._OPTION,
    "PlanItem": ev._PLAN_ITEM,
    "ChangedFile": ev._CHANGED_FILE,
    "QueuedMessage": ev._QUEUED,
    "Caps": ev._CAPS,
    "OptionChoice": ev._CHOICE,
    "StringOption": ev._STRING_OPTION,
    "FlagOption": ev._FLAG_OPTION,
    "SessionOptions": ev._OPTIONS,
}
_SHARED_NAME = {id(spec): name for name, spec in _SHARED.items()}

# What a v2 stream adds to an event's ``data`` beyond events.py. The
# catch-up's ``session.state`` carries the live text, the open request
# ids, the seq the stream is caught up to and the epoch; and a delta on
# the wire always says where its text starts.
WIRE_OPTIONAL = {
    "session.state": ("live", "pending", "head", "epoch"),
}
WIRE_REQUIRED = {
    "message.delta": ("offset",),
}


def type_name(etype: str) -> str:
    """``tool.completed`` -> ``ToolCompleted``."""
    return "".join(part.capitalize()
                   for part in etype.replace("-", ".").split("."))


def _ref(name: str) -> dict:
    return {"$ref": f"#/$defs/{name}"}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _spec(spec: Any, *, inline: bool = False) -> dict:
    """One events.py field spec as JSON Schema."""
    if isinstance(spec, ev._Str):
        return {"type": "string"} if spec.empty \
            else {"type": "string", "minLength": 1}
    if isinstance(spec, ev._Int):
        return {"type": "integer", "minimum": spec.least}
    if isinstance(spec, ev._Num):
        return {"type": "number", "minimum": 0}
    if isinstance(spec, ev._Bool):
        return {"type": "boolean"}
    if isinstance(spec, ev._Enum):
        return {"type": "string", "enum": sorted(spec.values)}
    if isinstance(spec, ev._Any):
        return {}
    if isinstance(spec, ev._List):
        return {"type": "array", "items": _spec(spec.item)}
    if isinstance(spec, ev._Obj):
        if not inline and id(spec) in _SHARED_NAME:
            return _ref(_SHARED_NAME[id(spec)])
        props = {name: _spec(field) for name, field in spec.required.items()}
        props.update({name: _spec(field)
                      for name, field in spec.optional.items()})
        return {"type": "object", "properties": props,
                "required": list(spec.required),
                "additionalProperties": bool(spec.open)}
    raise TypeError(f"no JSON Schema for {type(spec).__name__}")


def _envelope(etype: str, data: dict, *, seq: bool, extra: tuple = ()
              ) -> dict:
    props: dict[str, Any] = {
        "v": {"const": protocol.SYNC_VERSION},
        "type": {"const": etype},
        "session": {"type": "string", "minLength": 1},
    }
    required = ["v", "type", "session"]
    if seq:
        props["seq"] = {"type": "integer", "minimum": 0}
        required.append("seq")
    props["ts"] = {"type": "number", "minimum": 0}
    required.append("ts")
    for name in ("turn_id", "command_id"):
        if seq or etype in ev.EVENT_TYPES:
            props[name] = {"type": "string", "minLength": 1}
    required.extend(extra)
    props["data"] = data
    required.append("data")
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def _live_message() -> dict:
    return {
        "description": "A message still being written: its id and the "
                       "full text so far.",
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "kind": {"type": "string", "enum": sorted(ev.DELTA_KINDS)},
            "text": {"type": "string"},
            "turn_id": {"type": "string", "minLength": 1},
            "done": {"type": "boolean"},
        },
        "required": ["id", "kind", "text"],
        "additionalProperties": False,
    }


def _wire_fields() -> dict[str, dict]:
    return {
        "live": {"type": "array", "items": _ref("LiveMessage")},
        "pending": {"type": "array",
                    "items": {"type": "string", "minLength": 1}},
        "head": {"type": "integer", "minimum": 0},
        "epoch": {"type": "integer", "minimum": 0},
    }


def _checkpoint() -> dict:
    data = _spec(ev.SCHEMAS["checkpoint.created"])
    props = dict(data["properties"])
    props["seq"] = {"type": "integer", "minimum": 0}
    props["ts"] = {"type": "number", "minimum": 0}
    props["turn_id"] = {"type": "string", "minLength": 1}
    return {"description": "One checkpoint.created, with the seq, time "
                           "and turn of its event.",
            "type": "object", "properties": props,
            "required": list(data["required"]) + ["seq", "ts"],
            "additionalProperties": False}


def _snapshot() -> dict:
    integer = {"type": "integer", "minimum": 0}
    props = {
        "session": {"type": "string", "minLength": 1},
        "epoch": integer,
        "head": integer,
        "boot_id": {"type": "string", "minLength": 1},
        "row": _nullable(_ref("Row")),
        "state": {"type": "string", "enum": sorted(ev.SESSION_STATES)},
        "reason": {"type": "string"},
        "caps": _nullable(_ref("Caps")),
        "options": _ref("SessionOptions"),
        "open_turn": _nullable({"type": "string", "minLength": 1}),
        "pending": {"type": "array", "items": {"oneOf": [
            _ref("ApprovalRequestedEvent"),
            _ref("InputRequestedEvent")]}},
        "queue": {"type": "array", "items": _ref("QueuedMessage")},
        "live": {"type": "array", "items": _ref("LiveMessage")},
        "plan": _nullable(_ref("PlanUpdatedData")),
        "tasks": {"type": "array", "items": _ref("TaskUpdatedData")},
        "checkpoints": {"type": "array", "items": _ref("Checkpoint")},
        "changes": _nullable({"type": "array",
                              "items": _ref("ChangedFile")}),
        "usage": _nullable(_ref("UsageUpdatedData")),
        "messages": {"type": "array", "items": _ref("Event")},
        "has_more": {"type": "boolean"},
        "next_before": _nullable(integer),
    }
    return {
        "description": "Everything a client needs to draw one session "
                       "from nothing, true at `head`. The body of "
                       "GET /v1/sessions/{id}/snapshot and the data of "
                       "a session.snapshot frame.",
        "type": "object", "properties": props,
        # ``options`` is G32's: a hub that does not list the
        # session_options feature sends none.
        "required": [name for name in props
                     if name not in ("reason", "options")],
        "additionalProperties": False,
    }


def build() -> dict:
    """The schema, made from ``events.SCHEMAS`` and the frames sync v2
    adds."""
    defs: dict[str, Any] = {}
    for name, spec in _SHARED.items():
        defs[name] = _spec(spec, inline=True)
    defs["LiveMessage"] = _live_message()
    wire = _wire_fields()
    events = []
    for etype in sorted(ev.SCHEMAS):
        name = type_name(etype)
        data = _spec(ev.SCHEMAS[etype], inline=True)
        for field in WIRE_OPTIONAL.get(etype, ()):
            data["properties"][field] = wire[field]
        for field in WIRE_REQUIRED.get(etype, ()):
            if field not in data["required"]:
                data["required"].append(field)
        defs[f"{name}Data"] = data
        extra = []
        if etype in ev._NEEDS_TURN:
            extra.append("turn_id")
        if etype in ev._NEEDS_COMMAND:
            extra.append("command_id")
        defs[f"{name}Event"] = _envelope(
            etype, _ref(f"{name}Data"),
            seq=etype not in ev.TRANSIENT_TYPES, extra=tuple(extra))
        events.append(_ref(f"{name}Event"))
    defs["Event"] = {
        "description": "One v2 event. `seq` above 0 is its place in "
                       "the session's journal; 0 marks one the hub "
                       "made for the stream, which is not journaled "
                       "and never moves a cursor. message.delta has "
                       "none.",
        "oneOf": events,
    }
    defs["Row"] = {
        "description": "The session's row, as GET /v1/sessions lists "
                       "it (docs/remote-control.md).",
        "type": "object",
        "properties": {"key": {"type": "string", "minLength": 1}},
        "required": ["key"], "additionalProperties": True,
    }
    defs["Checkpoint"] = _checkpoint()
    defs["Snapshot"] = _snapshot()
    defs["SessionSnapshotFrame"] = _envelope(
        "session.snapshot", _ref("Snapshot"), seq=False)
    defs["SessionRowFrame"] = _envelope("session.row", {
        "type": "object", "properties": {"row": _ref("Row")},
        "required": ["row"], "additionalProperties": False}, seq=False)
    defs["PingFrame"] = _envelope("ping", {
        "type": "object",
        "properties": {"ts": {"type": "number", "minimum": 0},
                       "head": {"type": "integer", "minimum": 0}},
        "required": ["ts", "head"], "additionalProperties": False},
        seq=False)
    defs["Frame"] = {
        "description": "Anything a v=2 session stream sends.",
        "oneOf": events + [_ref("SessionSnapshotFrame"),
                           _ref("SessionRowFrame"), _ref("PingFrame")],
    }
    defs["MessagesPage"] = {
        "description": "GET /v1/sessions/{id}/messages?v=2.",
        "type": "object",
        "properties": {
            "session": {"type": "string", "minLength": 1},
            "messages": {"type": "array", "items": _ref("Event")},
            "has_more": {"type": "boolean"},
            "next_before": _nullable({"type": "integer", "minimum": 0}),
        },
        "required": ["session", "messages", "has_more", "next_before"],
        "additionalProperties": False,
    }
    defs["Status"] = {
        "description": "GET /v1/status.",
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "v": {"type": "integer", "minimum": 1},
            "protocols": {"type": "array",
                          "items": {"type": "integer", "minimum": 1}},
            "features": {"type": "array",
                         "items": {"type": "string", "minLength": 1}},
            "boot_id": {"type": "string", "minLength": 1},
        },
        "required": ["ok", "v", "protocols", "features", "boot_id"],
        "additionalProperties": True,
    }
    defs["CommandLookup"] = {
        "description": "GET /v1/sessions/{id}/commands/{command_id}: one "
                       "stored command, when /v1/status lists "
                       "command_lookup. `receipt` is the stored status; "
                       "`result` is there once the command finished. "
                       "404 unknown_command when the hub does not hold "
                       "the id for the session.",
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "session": {"type": "string", "minLength": 1},
            "command_id": {"type": "string", "minLength": 1},
            "op": {"type": "string", "minLength": 1},
            "receipt": {"type": "string",
                        "enum": sorted(ev.RECEIPT_STATUSES
                                       - {"duplicate"})},
            "reason": {"type": "string"},
            "result": {"type": "object", "additionalProperties": True},
            "received_at": {"type": "number"},
            "updated_at": {"type": "number"},
        },
        "required": ["ok", "session", "command_id", "op", "receipt",
                     "received_at", "updated_at"],
        "additionalProperties": False,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "SWE session sync v2",
        "description": "Generated by `python3 -m swe.remote.schema "
                       "--write` from src/swe/remote/adapters/events.py. "
                       "Do not edit by hand.",
        "x-swe": {
            "version": protocol.SYNC_VERSION,
            "version_header": protocol.VERSION_HEADER,
            "ping_s": protocol.PING_S,
            "silence_s": protocol.SILENCE_S,
            "replay_max": protocol.REPLAY_MAX,
            "snapshot_messages": protocol.SNAPSHOT_MESSAGES,
            "features": list(protocol.FEATURES)
            + [protocol.FEATURE_RUNTIME],
        },
        "$ref": "#/$defs/Frame",
        "$defs": defs,
    }


def dumps(schema: dict | None = None) -> str:
    """The schema as the committed file holds it."""
    return json.dumps(schema if schema is not None else build(),
                      indent=2) + "\n"


def load() -> dict:
    """The committed schema."""
    return json.loads(PATH.read_text(encoding="utf-8"))


_CACHED: dict | None = None


def _committed() -> dict:
    """:func:`load`, read once (``validate`` runs per frame)."""
    global _CACHED
    if _CACHED is None:
        _CACHED = load()
    return _CACHED


# ---------------------------------------------------------------------
# A validator for the subset of JSON Schema the file uses.

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float))
    and not isinstance(v, bool),
}

_KNOWN = frozenset({
    "$ref", "$schema", "$id", "$defs", "title", "description", "x-swe",
    "type", "const", "enum", "properties", "required",
    "additionalProperties", "items", "oneOf", "anyOf", "minimum",
    "minLength",
})


def _resolve(root: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"only local refs are supported, not {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _branch_type(root: dict, schema: dict) -> Any:
    """The ``type`` const of an object schema, through one ref."""
    if "$ref" in schema:
        schema = _resolve(root, schema["$ref"])
    return ((schema.get("properties") or {}).get("type") or {}).get("const")


def _check(value: Any, schema: dict, root: dict, path: str,
           problems: list[str]) -> None:
    unknown = set(schema) - _KNOWN
    if unknown:
        raise ValueError(f"unsupported schema keywords {sorted(unknown)}")
    if "$ref" in schema:
        _check(value, _resolve(root, schema["$ref"]), root, path, problems)
    for word in ("oneOf", "anyOf"):
        if word not in schema:
            continue
        branches = schema[word]
        results = []
        for branch in branches:
            found: list[str] = []
            _check(value, branch, root, path, found)
            results.append(found)
        passed = sum(1 for found in results if not found)
        if passed == 1 or (passed and word == "anyOf"):
            continue
        if passed > 1:
            problems.append(f"{path} matches {passed} branches of oneOf")
            continue
        # Say why the branch it was meant for failed: the one whose
        # ``type`` const it carries, else the nearest miss.
        named = [found for branch, found in zip(branches, results)
                 if isinstance(value, dict)
                 and _branch_type(root, branch) is not None
                 and _branch_type(root, branch) == value.get("type")]
        if named:
            problems.extend(named[0])
        elif isinstance(value, dict) and "type" in value \
                and any(_branch_type(root, b) is not None
                        for b in branches):
            problems.append(f"{path}.type {value.get('type')!r} is not a "
                            "known type")
        else:
            problems.extend(min(results, key=len))
    if "const" in schema and (
            value != schema["const"]
            or isinstance(value, bool) != isinstance(schema["const"], bool)):
        problems.append(f"{path} must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path} must be one of {schema['enum']}")
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_TYPES[k](value) for k in kinds):
            problems.append(f"{path} must be {' or '.join(kinds)}")
            return
    if "minimum" in schema and _TYPES["number"](value) \
            and value < schema["minimum"]:
        problems.append(f"{path} must be at least {schema['minimum']}")
    if "minLength" in schema and isinstance(value, str) \
            and len(value) < schema["minLength"]:
        problems.append(f"{path} must not be empty")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                problems.append(f"{path}.{name} is required")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in props:
                _check(item, props[name], root, f"{path}.{name}", problems)
            elif extra is False:
                problems.append(f"{path}.{name} is not a known field")
            elif isinstance(extra, dict):
                _check(item, extra, root, f"{path}.{name}", problems)
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _check(item, schema["items"], root, f"{path}[{i}]", problems)


def validate(value: Any, name: str = "Frame",
             schema: dict | None = None) -> list[str]:
    """Every problem with ``value`` as the ``$defs`` entry ``name`` of
    the committed schema (or ``schema``); empty when it is valid.
    ``name`` is ``Frame`` (anything a stream sends), ``Event``,
    ``Snapshot``, ``MessagesPage``, ``Status``, or any other entry."""
    root = schema if schema is not None else _committed()
    problems: list[str] = []
    _check(value, root["$defs"][name], root, name, problems)
    return problems


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    text = dumps()
    if "--write" in args:
        PATH.write_text(text, encoding="utf-8")
        print(f"wrote {PATH}")
        return 0
    if "--check" in args:
        try:
            fresh = PATH.read_text(encoding="utf-8") == text
        except OSError:
            fresh = False
        if not fresh:
            print(f"{PATH} is stale: run python3 -m swe.remote.schema "
                  "--write", file=sys.stderr)
            return 1
        return 0
    sys.stdout.write(text)
    return 0
