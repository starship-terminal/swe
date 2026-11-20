"""Claude Code's stream-json control protocol: the one file that knows it.

Claude Code run as ``claude --output-format stream-json --verbose
--input-format stream-json --permission-prompt-tool stdio`` is one
long-lived process per session speaking newline-delimited JSON on
stdio, the transport the Agent SDK uses. This module is pure: it builds
the spawn line, encodes what we write, and turns what the CLI prints
into canonical v2 events (:mod:`swe.remote.adapters.events`). It starts
no process, owns no thread and does no I/O, so a recorded fixture
replays through it exactly as a live session does
(:func:`replay`); ``claude_stream.py`` adds the process around it.

What we write (one JSON object per line):

- ``{"type": "user", "message": {"role": "user", "content": ...},
  "parent_tool_use_id": null}``: a turn. ``priority: "now"`` makes it a
  steer (recorded, left off; see ``docs/specs/G7-claude-stream-adapter.md``).
- ``{"type": "control_request", "request_id": R, "request": {"subtype":
  S, ...}}`` for ``initialize``, ``interrupt``, ``set_model``,
  ``set_permission_mode``, ``stop_task`` and ``rewind_files``; the CLI
  answers each with a ``control_response`` carrying the same R.
- ``{"type": "control_response", "response": {"subtype": "success",
  "request_id": R, "response": {"behavior": "allow"|"deny", ...}}}``:
  our answer to the CLI's ``can_use_tool`` control request.

What the CLI prints, and what each becomes, is :class:`StreamState`'s
table (``feed``) and the spec doc's mapping table.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from swe.remote.adapters import events as ev
from swe.remote.adapters.base import Answer, Caps, UnknownRequest
# The transcript redactor is the one the G3 contract names for adapter
# events (base.py, "Redaction"); it is private to its module only by
# name, and sharing it keeps one set of secret patterns.
from swe.reports.transcripts import _redact_secrets

# ---------------------------------------------------------------------
# The spawn line

# Flags the protocol itself needs, whatever argv harness.json gives: the
# adapter appends any that are missing. ``--permission-prompt-tool
# stdio`` routes permission prompts to us as ``can_use_tool`` control
# requests; ``--include-partial-messages`` adds the ``stream_event``
# lines live text comes from. The CLI needs no ``-p``: stream-json input
# puts it in print mode (recorded).
PROTOCOL_FLAGS: tuple[tuple[str, ...], ...] = (
    ("--output-format", "stream-json"),
    ("--verbose",),
    ("--input-format", "stream-json"),
    ("--permission-prompt-tool", "stdio"),
    ("--include-partial-messages",),
)

# The command when neither harness.json nor the fallback gives one.
DEFAULT_ARGV: tuple[str, ...] = ("claude",) + tuple(
    tok for flag in PROTOCOL_FLAGS for tok in flag)

# Environment the adapter adds. The CLI prints ``session_state_changed``
# frames (``running``, ``requires_action``, ``idle``) only when asked;
# ``idle`` is the one reliable end of a turn (a steer or a background
# task's wake-up can put two ``result`` lines in one running stretch).
SPAWN_ENV = {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}

# Environment the adapter removes: the marker of a parent Claude Code
# session (the Agent SDK drops it too, so the child does not think it is
# nested).
SPAWN_ENV_DROP = ("CLAUDECODE",)

# The oldest CLI the adapter starts when harness.json names none: the
# Agent SDK's own floor for this protocol (MINIMUM_CLAUDE_CODE_VERSION).
MIN_VERSION = "2.0.0"

# The version the fixtures under tests/fixtures/protocols/claude_stream/
# were recorded against.
RECORDED_VERSION = "2.1.328"

# What the recordings prove, by feature. ``claude_stream`` never offers
# more than this, whatever harness.json says (harness.json and the
# handshake can only narrow it further). Steer is recorded but left off
# for the owner to decide; conversation rollback has no wire form
# (``rewind_files`` restores files only); effort has a spawn flag but no
# mid-session control request.
PROVEN = Caps(plan_mode=True, fork=True, stop_task=True,
              options=frozenset({"model", "permission_mode"}))

# A feature that needs a protocol capability string in the init
# message's ``capabilities`` array: when the array lacks it (or the CLI
# sends no array, as before 2.1.205), the feature narrows off. No string
# in 2.1.328's array names a proven feature, so it is empty; steer would
# go behind ``interrupt_send_now_v1`` (guessing: that string names the
# "interrupt and send now" behaviour the steer fixture shows).
REQUIRED_CAPABILITIES: dict[str, str] = {}

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def new_session_id() -> str:
    """A fresh session id: the CLI takes ours with ``--session-id``, so
    ``start`` knows it before the first turn."""
    return str(_uuid.uuid4())


def is_session_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.fullmatch(value))


def parse_version(text: Any) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(text) if isinstance(text, str) else None
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def version_ok(have: Any, want: str) -> bool:
    """Whether CLI version ``have`` is at least ``want``; an unreadable
    ``have`` is not."""
    got, need = parse_version(have), parse_version(want)
    if need is None:
        return True
    return got is not None and got >= need


def _has_flag(argv: list[str], flag: str) -> bool:
    return any(tok == flag or tok.startswith(flag + "=") for tok in argv)


def spawn_argv(base: Iterable[str], *, session_id: str = "",
               resume_id: str = "", fork: bool = False, model: str = "",
               effort: str = "", permission_mode: str = "") -> list[str]:
    """``base`` plus any missing protocol flag plus this session's own
    arguments, all in ``--flag=value`` form so no value can read as a
    flag. A new session passes ``session_id``; a resume passes
    ``resume_id``; a fork passes both and ``fork`` (``--fork-session``
    keeps the old transcript and writes the new id's)."""
    argv = list(base)
    for flag in PROTOCOL_FLAGS:
        if not _has_flag(argv, flag[0]):
            argv.extend(flag)
    for value in (session_id, resume_id):
        if value and not is_session_id(value):
            raise ValueError(f"not a Claude session id: {value!r}")
    if resume_id:
        argv.append(f"--resume={resume_id}")
        if fork:
            argv.append("--fork-session")
    if session_id:
        argv.append(f"--session-id={session_id}")
    for flag, value in (("--model", model), ("--effort", effort),
                        ("--permission-mode", permission_mode)):
        if value:
            argv.append(f"{flag}={value}")
    return argv


def spawn_env(base: dict, extra: dict | None = None) -> dict:
    env = {k: v for k, v in base.items() if k not in SPAWN_ENV_DROP}
    env.update(SPAWN_ENV)
    env.update(extra or {})
    return env


def transcript_slug(cwd: str) -> str:
    """The folder name Claude Code files a project's transcripts under:
    the real cwd with every character but letters and digits as ``-``
    (seen on disk for the recordings' folders)."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# ---------------------------------------------------------------------
# Encoding


def encode(message: dict) -> bytes:
    """One wire line."""
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            + "\n").encode("utf-8")


def user_message(text: str, *, attachments: Iterable[str] = (),
                 uuid: str = "", priority: str = "") -> dict:
    """A user turn. Attachments ride as ``@/abs/path`` refs ahead of the
    text, which Claude Code expands itself."""
    refs = " ".join(f"@{path}" for path in attachments)
    content = f"{refs} {text}" if refs else text
    msg: dict[str, Any] = {"type": "user",
                           "message": {"role": "user", "content": content},
                           "parent_tool_use_id": None}
    if uuid:
        msg["uuid"] = uuid
    if priority:
        msg["priority"] = priority
    return msg


def control_request(request_id: str, subtype: str, **fields: Any) -> dict:
    body: dict[str, Any] = {"subtype": subtype}
    body.update(fields)
    return {"type": "control_request", "request_id": request_id,
            "request": body}


def initialize(request_id: str) -> dict:
    return control_request(request_id, "initialize", hooks=None)


def interrupt(request_id: str) -> dict:
    return control_request(request_id, "interrupt")


def set_model(request_id: str, model: str) -> dict:
    return control_request(request_id, "set_model", model=model)


def set_permission_mode(request_id: str, mode: str) -> dict:
    return control_request(request_id, "set_permission_mode", mode=mode)


def stop_task(request_id: str, task_id: str) -> dict:
    return control_request(request_id, "stop_task", task_id=task_id)


def rewind_files(request_id: str, user_message_id: str) -> dict:
    return control_request(request_id, "rewind_files",
                           user_message_id=user_message_id)


def control_success(request_id: str, response: dict) -> dict:
    return {"type": "control_response",
            "response": {"subtype": "success", "request_id": request_id,
                         "response": response}}


def control_error(request_id: str, error: str) -> dict:
    return {"type": "control_response",
            "response": {"subtype": "error", "request_id": request_id,
                         "error": error}}


def permission_allow(request_id: str, updated_input: Any,
                     updated_permissions: list | None = None) -> dict:
    """Allow one ``can_use_tool``. ``updatedInput`` is required: the
    tool runs with it (we pass the input back unchanged)."""
    resp: dict[str, Any] = {"behavior": "allow",
                            "updatedInput": updated_input}
    if updated_permissions:
        resp["updatedPermissions"] = updated_permissions
    return control_success(request_id, resp)


# Whether approvals offer "always". The always fixture proves the CLI
# takes session-scoped rules and asks no more for that call.
OFFER_ALWAYS = True


def session_scoped(suggestions: list) -> list:
    """The CLI's ``permission_suggestions`` with every ``destination``
    set to ``session``: an answer from a phone never writes a settings
    file (``localSettings``, ``projectSettings``, ``userSettings``).
    ``setMode`` suggestions are dropped: the CLI offers "switch the
    session to acceptEdits" beside a Bash rule, and a mode change is
    ``set_option``'s decision, not a side effect of one approval."""
    out = []
    for item in suggestions or ():
        if isinstance(item, dict) and item.get("type") != "setMode":
            item = dict(item)
            item["destination"] = "session"
            out.append(item)
    return out


def permission_deny(request_id: str, message: str,
                    interrupt: bool = False) -> dict:
    """Deny one ``can_use_tool``; ``message`` goes to the model as the
    tool's error result."""
    resp: dict[str, Any] = {"behavior": "deny", "message": message}
    if interrupt:
        resp["interrupt"] = True
    return control_success(request_id, resp)


# ---------------------------------------------------------------------
# Decoding


def decode(line: bytes | str) -> dict | None:
    """One line from the CLI as a dict, or None for anything else (a
    blank line, text, a JSON value that is not an object)."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


@dataclass(frozen=True)
class Ack:
    """The CLI's answer to one of our control requests."""

    request_id: str
    ok: bool
    body: dict = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class Handshake:
    """What the ``initialize`` answer says about the CLI."""

    version: str = ""
    permission_mode: str = ""
    models: tuple = ()
    capabilities: tuple = ()
    session_state: str = ""

    @classmethod
    def from_body(cls, body: Any) -> "Handshake":
        body = body if isinstance(body, dict) else {}
        models = tuple(str(m.get("value")) for m in body.get("models") or ()
                       if isinstance(m, dict) and m.get("value"))
        caps = tuple(c for c in body.get("capabilities") or ()
                     if isinstance(c, str))
        return cls(version=_str(body.get("claude_code_version")),
                   permission_mode=_str(body.get("current_permission_mode")),
                   models=models, capabilities=caps,
                   session_state=_str(body.get("session_state")))

    def caps(self) -> Caps:
        """The ceiling this CLI's handshake allows: a model list means
        ``set_model`` has something to pick from; a current permission
        mode means it reports and takes modes (plan included)."""
        options = set()
        if self.models:
            options.add("model")
        if self.permission_mode:
            options.add("permission_mode")
        return Caps(steer=True, plan_mode=bool(self.permission_mode),
                    rollback=True, fork=True, stop_task=True,
                    options=frozenset(options))


def capability_caps(capabilities: Any) -> Caps:
    """The ceiling the init message's ``capabilities`` array allows,
    under ``REQUIRED_CAPABILITIES``. ``None`` (no array at all) fails
    every requirement."""
    have = set(c for c in capabilities if isinstance(c, str)) \
        if isinstance(capabilities, list) else set()
    allow = {f: REQUIRED_CAPABILITIES.get(f) in have
             if f in REQUIRED_CAPABILITIES else True
             for f in ("steer", "plan_mode", "rollback", "fork",
                       "stop_task")}
    options = {o for o in ("model", "effort", "permission_mode")
               if o not in REQUIRED_CAPABILITIES
               or REQUIRED_CAPABILITIES[o] in have}
    return Caps(options=frozenset(options), **allow)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


REDACTED = "[REDACTED]"

# Key name parts that make a value a credential whatever it looks like:
# the last part of the name (``api_key``, ``clientSecret``,
# ``GITHUB_TOKEN``, ``password``), or the whole name (``authorization``,
# ``cookie``). ``max_tokens`` or ``token_count`` are not credentials.
_SECRET_LAST = frozenset({"token", "secret", "password", "passwd",
                          "passphrase", "credential", "credentials",
                          "apikey", "authorization", "cookie"})
_SECRET_KEY_PREFIX = frozenset({"api", "private", "secret", "access",
                                "auth", "signing", "encryption"})
_NAME_PART_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def secret_key(name: Any) -> bool:
    """Whether a mapping key names a credential (``api_key``,
    ``accessToken``, ``Authorization``, ``DB_PASSWORD``)."""
    if not isinstance(name, str):
        return False
    parts = [p.lower() for p in _NAME_PART_RE.findall(name)]
    if not parts:
        return False
    if parts[-1] in _SECRET_LAST:
        return True
    return parts[-1] == "key" and len(parts) > 1 \
        and parts[-2] in _SECRET_KEY_PREFIX


def redact(value: Any) -> Any:
    """Secrets out of ``value`` (any JSON shape): every string through
    the transcript redactor, and anything under a key that names a
    credential (at any depth) masked whatever it holds."""
    if isinstance(value, str):
        return _redact_secrets(value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {str(k): REDACTED if secret_key(k) and v not in (None, "")
                else redact(v) for k, v in value.items()}
    return value


# Where a streamed text may be cut before it is redacted: everything up
# to the last whitespace, so a token still arriving is held back, unless
# the line so far could open a secret that runs past whitespace (a
# quoted value under a credential name, an Authorization header) or a
# private key block is open; then the whole line, or everything from the
# block's start, waits.
_OPEN_LINE_RE = re.compile(
    r"(?i)authorization|api_?key|token|secret|passw|private_?key")
_BLOCK_START = "-----BEGIN"
_BLOCK_END_RE = re.compile(r"-----END [A-Z0-9 ]*-----")


def stable_cut(raw: str) -> int:
    """How much of ``raw`` (a stream's text so far) can be redacted and
    shown now: a prefix no later text can change the redaction of."""
    line_start = raw.rfind("\n") + 1
    # The line before counts too: ``\s*`` between a name and its value
    # can span a newline.
    prev_start = raw.rfind("\n", 0, max(0, line_start - 1)) + 1
    if line_start and _OPEN_LINE_RE.search(raw[prev_start:line_start]):
        cut = prev_start
    elif _OPEN_LINE_RE.search(raw[line_start:]):
        cut = line_start
    else:
        cut = max(raw.rfind(" "), raw.rfind("\t"), raw.rfind("\n")) + 1
    # Never inside a key block: one whose end is not in the cut text
    # (not arrived, or past the cut) holds everything from its start.
    for begin in re.finditer(re.escape(_BLOCK_START), raw[:cut]):
        end = _BLOCK_END_RE.search(raw, begin.end())
        if end is None or end.end() > cut:
            return begin.start()
    return cut


def _jsonable(value: Any) -> Any:
    """``value`` as strict JSON: a non-finite number or anything JSON
    has no type for becomes its string."""
    if isinstance(value, float) and (value != value or value in (
            float("inf"), float("-inf"))):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


# Longest tool output an event carries; the transcript has the rest.
OUTPUT_LIMIT = 8000
_SUMMARY_LIMIT = 160

# Tool input fields that name what a call is about, in order of use.
_SUMMARY_KEYS = ("command", "file_path", "notebook_path", "path",
                 "pattern", "url", "query", "description", "prompt")


def tool_summary(name: str, tool_input: Any) -> str:
    """The one-line card text: ``Bash · ls -la``."""
    detail = ""
    if isinstance(tool_input, dict):
        for key in _SUMMARY_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip().splitlines()[0]
                break
    text = f"{name} · {detail}" if detail else name
    text = _redact_secrets(text)
    return text if len(text) <= _SUMMARY_LIMIT else \
        text[:_SUMMARY_LIMIT - 1] + "…"


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "tool_reference":
                    parts.append(str(block.get("tool_name") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


def file_edits(result: Any) -> list | None:
    """``file_edits`` from a Write or Edit call's ``tool_use_result``:
    ``type`` create is an add, anything else with a path a modify; the
    diff comes from ``structuredPatch`` hunks."""
    if not isinstance(result, dict):
        return None
    path = result.get("filePath")
    if not isinstance(path, str) or not path:
        return None
    kind = "add" if result.get("type") == "create" else "modify"
    hunks = []
    for hunk in result.get("structuredPatch") or ():
        if not isinstance(hunk, dict):
            continue
        lines = [ln for ln in hunk.get("lines") or () if isinstance(ln, str)]
        hunks.append("@@ -{},{} +{},{} @@\n".format(
            hunk.get("oldStart", 0), hunk.get("oldLines", 0),
            hunk.get("newStart", 0), hunk.get("newLines", 0))
            + "".join(ln + "\n" for ln in lines))
    edit: dict[str, Any] = {"path": path, "kind": kind}
    if hunks:
        edit["diff"] = _redact_secrets("".join(hunks))
    elif kind == "add" and isinstance(result.get("content"), str):
        body = result["content"].splitlines()
        edit["diff"] = _redact_secrets(
            f"@@ -0,0 +1,{len(body)} @@\n"
            + "".join(f"+{ln}\n" for ln in body))
    return [edit]


# ---------------------------------------------------------------------
# The session state machine


# ``can_use_tool`` tool names with their own approval kind.
_PLAN_TOOLS = frozenset({"ExitPlanMode"})
_COMMAND_TOOLS = frozenset({"Bash", "PowerShell"})

# The options every tool approval offers; ``always`` only when the CLI
# suggested a standing rule to save.
_ALLOW = {"id": "allow", "label": "Allow", "kind": "allow"}
_ALWAYS = {"id": "always", "label": "Always allow", "kind": "always",
           "standing": True}
_DENY = {"id": "deny", "label": "Deny", "kind": "deny"}
_PLAN_OPTIONS = [{"id": "allow", "label": "Approve plan", "kind": "allow"},
                 {"id": "deny", "label": "Keep planning", "kind": "deny"}]

_DENY_MESSAGE = "The user denied this tool use."
_PLAN_DENY_MESSAGE = ("The user did not approve the plan yet. Stay in "
                      "plan mode and wait for their feedback.")

# task_notification and task_updated statuses as v2 task statuses.
_TASK_STATUS = {"running": "running", "pending": "running",
                "paused": "running", "completed": "completed",
                "failed": "failed", "stopped": "stopped",
                "killed": "stopped"}


@dataclass
class _Pending:
    request_id: str
    kind: str
    tool_name: str
    tool_input: Any
    suggestions: list
    call_id: str = ""


@dataclass
class _Stream:
    """One streamed text or thinking block: everything received
    (``raw``) and the redacted prefix already sent (``shown``)."""

    id: str
    kind: str
    seq: int = 0
    raw: str = ""
    shown: str = ""


@dataclass
class _Turn:
    turn_id: str
    started: float
    command_id: str = ""
    interrupted: bool = False
    error: bool = False
    reason: str = ""
    user_msgs: int = 0


@dataclass
class Fed:
    """What one CLI line came to: events to emit, lines to write back
    (our answer to a control request we do not serve), and acks for
    our own control requests."""

    events: list = field(default_factory=list)
    replies: list = field(default_factory=list)
    acks: list = field(default_factory=list)


class StreamState:
    """One Claude session's protocol state, from the adapter's side.

    The adapter calls ``start_events`` after the handshake,
    ``begin_turn`` per ``send_turn``, ``answer`` per ``respond``,
    ``interrupt_sent`` per ``interrupt``, ``feed`` per line the CLI
    prints, and ``exited`` or ``closed`` once at the end; each returns
    the v2 events to emit, in order. Nothing is returned after the end.

    Line by line (``feed``):

    ==========================================  ==========================
    CLI line                                    v2 events
    ==========================================  ==========================
    ``control_response``                        an ``Ack`` (no event)
    ``control_request`` ``can_use_tool``        ``approval.requested``,
                                                ``session.state`` waiting
    ``control_request`` (any other subtype)     an error reply, no event
    ``control_cancel_request``                  ``approval.resolved``
                                                (``harness``)
    ``system`` ``session_state_changed``        ``running`` with no turn
                                                open: ``turn.started``;
                                                ``idle``: ``turn.ended``
                                                then ``session.state``
                                                idle
    ``system`` ``init``                         ``session.caps`` when its
                                                ``capabilities`` narrow
    ``system`` ``task_started`` / ``_progress``  ``task.updated``
    / ``_notification`` / ``_updated``
    ``system`` ``compact_boundary``             ``context.compacted``
    ``stream_event`` text / thinking deltas     ``message.delta``
    ``assistant`` text / thinking / tool_use    ``message.completed`` /
                                                ``thinking.completed`` /
                                                ``tool.started``
    ``user`` ``tool_result``                    ``tool.completed``
    ``result``                                  ``usage.updated``, and
                                                ``error`` when it failed
    anything else                               nothing (counted in
                                                ``unknown``)
    ==========================================  ==========================

    Lines from a subagent (``parent_tool_use_id`` set) give tool rows
    under the subagent's ``task_id`` and no text: the CLI forwards a
    subagent's tool calls, not its words.
    """

    def __init__(self, session: str, *, clock: Callable[[], float] = time.time,
                 caps: Caps = Caps(), session_id: str = "",
                 run_id: str = "") -> None:
        self.key = session
        # Turn ids are ``t-<run>-<n>``: one session's log spans many
        # starts (a resume after a keeper restart, Continue in SWE), and
        # turn ids key checkpoints and "restore to before this turn",
        # so each start gets its own run id.
        self.run_id = run_id or secrets.token_hex(4)
        self.clock = clock
        self.caps = caps
        self.session_id = session_id
        self.model = ""
        self.permission_mode = ""
        self.started = False
        self.done = False
        self.turn: _Turn | None = None
        self.waiting = False
        self.pending: dict[str, _Pending] = {}
        # task_id -> {kind, status, title, parent}; tool_use_id -> task_id.
        self.tasks: dict[str, dict] = {}
        self._task_by_tool: dict[str, str] = {}
        self._tools: dict[str, str] = {}  # call_id -> tool name
        # (api message id, block index) -> its stream
        self._blocks: dict[tuple, list] = {}
        # api message id -> {kind: [block indexes in order]}
        self._order: dict[str, dict[str, list]] = {}
        self._completed: set = set()
        self._state_events = False
        self._held: list[dict] = []
        self._n_turn = 0
        self.unknown: dict[str, int] = {}

    # -- helpers ---------------------------------------------------------

    def _tid(self) -> str | None:
        return self.turn.turn_id if self.turn else None

    def _out(self, out: list, event: dict) -> None:
        if not self.started:
            self._held.append(event)
        else:
            out.append(event)

    def _state(self, out: list, state: str, reason: str | None = None) -> None:
        self._out(out, ev.session_state(
            self.key, state,
            reason=_redact_secrets(reason) if reason else None,
            ts=self.clock()))

    def _retire(self, out: list, source: str) -> None:
        for rid in list(self.pending):
            del self.pending[rid]
            self._out(out, ev.approval_resolved(self.key, rid, source,
                                                turn_id=self._tid(),
                                                ts=self.clock()))
        self.waiting = False

    def _end_turn(self, out: list, status: str,
                  reason: str | None = None) -> None:
        turn = self.turn
        if turn is None:
            return
        self._out(out, ev.turn_ended(
            self.key, turn.turn_id, status,
            reason=_redact_secrets(reason) if reason else None,
            duration_s=round(max(0.0, self.clock() - turn.started), 3),
            command_id=turn.command_id or None, ts=self.clock()))
        self.turn = None
        self._blocks.clear()
        self._order.clear()
        self._completed.clear()

    def _open_turn(self, out: list, *, by: str = "", text: str = "",
                   command_id: str = "") -> _Turn:
        self._n_turn += 1
        turn = _Turn(f"t-{self.run_id}-{self._n_turn}", self.clock(),
                     command_id=command_id)
        self.turn = turn
        self._out(out, ev.turn_started(
            self.key, turn.turn_id, by=by or None,
            text=_redact_secrets(text) if text else None,
            command_id=command_id or None, ts=self.clock()))
        self._state(out, "running")
        return turn

    # -- calls from the adapter ------------------------------------------

    def start_events(self, caps: Caps, session_id: str) -> list:
        """``session.caps`` then ``session.state`` idle, then anything
        the CLI printed before the handshake finished."""
        self.caps = caps
        self.session_id = session_id
        out = [ev.session_caps(self.key, caps.as_dict(), ts=self.clock()),
               ev.session_state(self.key, "idle",
                                harness_session_id=session_id or None,
                                ts=self.clock())]
        self.started = True
        out.extend(self._held)
        self._held = []
        return out

    def begin_turn(self, text: str, *, attachments: Iterable[str] = (),
                   command_id: str = "", device: str = "") -> tuple[str, list]:
        """A turn we start: ``turn.started``, ``session.state`` running
        and the user's ``message.completed``."""
        out: list = []
        turn = self._open_turn(out, by=device, text=text,
                               command_id=command_id)
        out.append(self.user_message(text, attachments=attachments,
                                     command_id=command_id, device=device))
        return turn.turn_id, out

    def user_message(self, text: str, *, attachments: Iterable[str] = (),
                     command_id: str = "", device: str = "") -> dict:
        assert self.turn is not None
        self.turn.user_msgs += 1
        n = self.turn.user_msgs
        mid = f"{self.turn.turn_id}:user" + (str(n) if n > 1 else "")
        return ev.message_completed(
            self.key, mid, "user", _redact_secrets(text),
            by=device or None,
            attachments=[_redact_secrets(str(a)) for a in attachments] or None,
            turn_id=self.turn.turn_id, command_id=command_id or None,
            ts=self.clock())

    def interrupt_sent(self) -> list:
        """We asked the CLI to stop the turn: its open requests are
        retired now (the CLI drops them itself), and the turn will end
        ``interrupted`` when the CLI goes idle."""
        out: list = []
        if self.turn is None:
            return out
        self.turn.interrupted = True
        if self.pending:
            self._retire(out, "turn_ended")
            self._state(out, "running")
        return out

    def answer(self, request_id: str, answer: Answer) -> tuple[dict, list]:
        """Our ``control_response`` to an open ``can_use_tool`` and the
        ``approval.resolved`` it closes. Raises UnknownRequest."""
        pending = self.pending.get(request_id)
        if pending is None:
            raise UnknownRequest(request_id)
        decision = answer.decision or (answer.option_id if answer.option_id
                                       in ("allow", "always", "deny") else "")
        if not decision:
            # A bare text answer is feedback: the call does not run and
            # the model reads why.
            decision = "deny"
        rules = session_scoped(pending.suggestions)
        if decision == "always" and not (OFFER_ALWAYS and rules):
            decision = "allow"
        if decision in ("allow", "always"):
            reply = permission_allow(
                request_id, pending.tool_input,
                rules if decision == "always" else None)
        else:
            default = (_PLAN_DENY_MESSAGE if pending.kind == "plan"
                       else _DENY_MESSAGE)
            reply = permission_deny(request_id, answer.text or default)
        del self.pending[request_id]
        out = [ev.approval_resolved(
            self.key, request_id, "user", decision=decision,
            option_id=answer.option_id or None,
            text=_redact_secrets(answer.text) if answer.text else None,
            by=answer.device or None, turn_id=self._tid(),
            ts=self.clock())]
        if not self.pending and self.waiting:
            self.waiting = False
            self._state(out, "running")
        return reply, out

    def exited(self, reason: str) -> list:
        """The CLI exited on its own: the contract's exit sequence."""
        return self._finish("agent_exit", "failed", "agent_down", reason)

    def closed(self) -> list:
        return self._finish("closed", "interrupted", "closed", None,
                            turn_reason="session closed")

    def _finish(self, source: str, status: str, state: str,
                reason: str | None, turn_reason: str | None = None) -> list:
        if self.done:
            return []
        out: list = []
        self.started = True
        self._held = []
        self._retire(out, source)
        self._end_turn(out, status, turn_reason or reason)
        self._state(out, state, reason)
        self.done = True
        return out

    # -- lines from the CLI ----------------------------------------------

    def feed(self, msg: dict) -> Fed:
        fed = Fed()
        if self.done or not isinstance(msg, dict):
            return fed
        mtype = msg.get("type")
        handler = getattr(self, "_on_" + str(mtype), None) \
            if isinstance(mtype, str) and mtype.isidentifier() else None
        if handler is None:
            key = str(mtype)[:40]
            self.unknown[key] = self.unknown.get(key, 0) + 1
            return fed
        handler(msg, fed)
        return fed

    def _on_control_response(self, msg: dict, fed: Fed) -> None:
        resp = msg.get("response")
        if not isinstance(resp, dict):
            return
        rid = resp.get("request_id")
        if not isinstance(rid, str):
            return
        if resp.get("subtype") == "error":
            fed.acks.append(Ack(rid, False,
                                error=_str(resp.get("error")) or "refused"))
        else:
            body = resp.get("response")
            body = body if isinstance(body, dict) else {}
            # set_model answers with the resolved id (``sonnet`` comes
            # back as ``claude-sonnet-5-5``), the spelling modelUsage and
            # the init message use.
            if isinstance(body.get("model"), str) and body["model"]:
                self.model = body["model"]
            fed.acks.append(Ack(rid, True, body=body))

    def _on_control_request(self, msg: dict, fed: Fed) -> None:
        rid = msg.get("request_id")
        req = msg.get("request")
        if not isinstance(rid, str) or not rid or not isinstance(req, dict):
            return
        if req.get("subtype") != "can_use_tool":
            # Hooks, SDK MCP servers and the rest: we registered none,
            # so the CLI gets a clear no instead of waiting forever.
            fed.replies.append(control_error(
                rid, f"{req.get('subtype')!s:.40} is not handled by this "
                "client"))
            return
        name = _redact_secrets(_str(req.get("tool_name"))) or "tool"
        tool_input = req.get("input")
        suggestions = req.get("permission_suggestions")
        suggestions = [s for s in suggestions if isinstance(s, dict)] \
            if isinstance(suggestions, list) else []
        if name in _PLAN_TOOLS:
            kind = "plan"
            plan = tool_input.get("plan") if isinstance(tool_input, dict) \
                else None
            prompt = _str(plan) or "Approve this plan?"
            options = _PLAN_OPTIONS
        else:
            kind = "command" if name in _COMMAND_TOOLS else "tool"
            label = _str(req.get("title")) or (
                f"Allow {_str(req.get('display_name')) or name}?")
            detail = tool_summary(name, tool_input)
            prompt = label if detail == name else f"{label}\n{detail}"
            # Offered only when a session-scoped rule is left to save: a
            # setMode-only suggestion list would make "always" an
            # allow-once under another name.
            offer_always = OFFER_ALWAYS and bool(session_scoped(suggestions))
            options = [_ALLOW, _ALWAYS, _DENY] if offer_always \
                else [_ALLOW, _DENY]
        call_id = _str(req.get("tool_use_id"))
        self.pending[rid] = _Pending(rid, kind, name, tool_input,
                                     suggestions, call_id)
        if self.turn is None:
            self._open_turn(fed.events)
        self._out(fed.events, ev.approval_requested(
            self.key, rid, kind, _redact_secrets(prompt),
            options=[dict(o) for o in options], call_id=call_id or None,
            turn_id=self._tid(), ts=self.clock()))
        if not self.waiting:
            self.waiting = True
            self._state(fed.events, "waiting")

    def _on_control_cancel_request(self, msg: dict, fed: Fed) -> None:
        rid = msg.get("request_id")
        if isinstance(rid, str) and rid in self.pending:
            del self.pending[rid]
            self._out(fed.events, ev.approval_resolved(
                self.key, rid, "harness", turn_id=self._tid(),
                ts=self.clock()))
            if not self.pending and self.waiting:
                self.waiting = False
                self._state(fed.events, "running")

    def _on_system(self, msg: dict, fed: Fed) -> None:
        sub = msg.get("subtype")
        if sub == "session_state_changed":
            self._state_events = True
            self._on_state(_str(msg.get("state")), fed)
        elif sub == "init":
            self._on_init(msg, fed)
        elif sub in ("task_started", "task_progress", "task_notification",
                     "task_updated"):
            self._on_task(sub, msg, fed)
        elif sub == "compact_boundary":
            meta = msg.get("compact_metadata")
            before = meta.get("pre_tokens") if isinstance(meta, dict) else None
            self._out(fed.events, ev.context_compacted(
                self.key, tokens_before=before if isinstance(before, int)
                and not isinstance(before, bool) and before >= 0 else None,
                turn_id=self._tid(), ts=self.clock()))
        elif sub == "status":
            mode = msg.get("permissionMode")
            if isinstance(mode, str) and mode:
                self.permission_mode = mode
        # Other system lines (thinking token estimates, hook notes,
        # api retries) carry nothing the v2 model records.

    def _on_state(self, state: str, fed: Fed) -> None:
        if state == "running":
            if self.turn is None:
                # A turn the CLI started by itself: a background task's
                # result woke it.
                self._open_turn(fed.events)
            elif self.waiting and not self.pending:
                self.waiting = False
                self._state(fed.events, "running")
        elif state == "requires_action":
            if self.turn is not None and not self.waiting:
                self.waiting = True
                self._state(fed.events, "waiting")
        elif state == "idle":
            self._idle(fed.events)

    def _idle(self, out: list) -> None:
        turn = self.turn
        if turn is None:
            return
        self._retire(out, "turn_ended")
        if turn.interrupted:
            self._end_turn(out, "interrupted")
        elif turn.error:
            self._end_turn(out, "failed", turn.reason or None)
        else:
            self._end_turn(out, "completed")
        self._state(out, "idle")

    def _on_init(self, msg: dict, fed: Fed) -> None:
        sid = msg.get("session_id")
        if is_session_id(sid):
            self.session_id = sid
        self.model = _str(msg.get("model")) or self.model
        self.permission_mode = _str(msg.get("permissionMode")) \
            or self.permission_mode
        narrowed = self.caps.narrowed(capability_caps(
            msg.get("capabilities")))
        if narrowed != self.caps:
            self.caps = narrowed
            self._out(fed.events, ev.session_caps(
                self.key, narrowed.as_dict(), ts=self.clock()))

    def _on_task(self, sub: str, msg: dict, fed: Fed) -> None:
        task_id = msg.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return
        known = self.tasks.get(task_id)
        if sub == "task_started":
            kind = "subagent" if msg.get("task_type") in (
                None, "local_agent", "remote_agent") else "background"
            known = {"kind": kind, "status": "running",
                     "title": _redact_secrets(_str(msg.get("description"))),
                     "parent": self._tid()}
            self.tasks[task_id] = known
            tool_id = msg.get("tool_use_id")
            if isinstance(tool_id, str) and tool_id:
                self._task_by_tool[tool_id] = task_id
            status = "running"
        elif known is None:
            return
        elif sub == "task_progress":
            title = _redact_secrets(_str(msg.get("description")))
            if not title or title == known["title"]:
                return
            known["title"] = title
            status = known["status"]
        else:
            raw = msg.get("status")
            if sub == "task_updated":
                patch = msg.get("patch")
                raw = raw or (patch.get("status") if isinstance(patch, dict)
                              else None)
            status = _TASK_STATUS.get(_str(raw))
            if status is None or status == known["status"]:
                return
            known["status"] = status
        running = status == "running"
        self._out(fed.events, ev.task_updated(
            self.key, task_id, known["kind"], status,
            parent_turn_id=known["parent"], title=known["title"] or None,
            can_stop=running and self.caps.stop_task, ts=self.clock()))

    def _on_stream_event(self, msg: dict, fed: Fed) -> None:
        if msg.get("parent_tool_use_id") or self.turn is None:
            return
        event = msg.get("event")
        if not isinstance(event, dict):
            return
        api_id = _str(msg.get("api_message_id"))
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                api_id = _str(message.get("id")) or api_id
            return
        index = event.get("index")
        if not api_id or not isinstance(index, int):
            return
        key = (api_id, index)
        if etype == "content_block_start":
            block = event.get("content_block")
            btype = block.get("type") if isinstance(block, dict) else None
            kind = {"text": "assistant", "thinking": "thinking"}.get(btype)
            if kind is None:
                return
            self._blocks[key] = _Stream(f"{api_id}:{index}", kind)
            self._order.setdefault(api_id, {}).setdefault(kind, []).append(
                index)
        elif etype == "content_block_delta":
            block = self._blocks.get(key)
            delta = event.get("delta")
            if block is None or not isinstance(delta, dict) \
                    or block.id in self._completed:
                return
            text = delta.get("text") if block.kind == "assistant" \
                else delta.get("thinking")
            if not isinstance(text, str) or not text:
                return
            block.raw += text
            shown = _redact_secrets(block.raw[:stable_cut(block.raw)])
            self._show(fed.events, block, shown)

    def _show(self, out: list, block: "_Stream", shown: str,
              done: bool = False) -> None:
        """Emit what of ``shown`` (the block's redacted text so far) is
        new. Text is never taken back: a redaction that no longer
        extends what went out waits (the completed message has it)."""
        if len(shown) <= len(block.shown) or not shown.startswith(
                block.shown):
            return
        block.seq += 1
        self._out(out, ev.message_delta(
            self.key, block.id, block.kind, shown[len(block.shown):],
            block.seq, offset=len(block.shown), done=done,
            turn_id=self._tid(), ts=self.clock()))
        block.shown = shown

    def _flush(self, out: list, bid: str, final: str) -> None:
        """The rest of a streamed block, as its last delta, just before
        its completion: the deltas then add up to the completed text."""
        for block in self._blocks.values():
            if block.id == bid:
                self._show(out, block, final, done=True)
                return

    def _block_id(self, api_id: str, kind: str, k: int) -> str:
        """The delta id of the ``k``-th ``kind`` block of an API message,
        so its completion closes the stream its deltas opened."""
        order = self._order.get(api_id, {}).get(kind, [])
        for index in order:
            bid = f"{api_id}:{index}"
            if bid not in self._completed:
                return bid
        return f"{api_id}:{kind[0]}{k}"

    def _on_assistant(self, msg: dict, fed: Fed) -> None:
        message = msg.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        if self.turn is None:
            self._open_turn(fed.events)
        api_id = _str(message.get("id")) or f"m{id(message)}"
        parent = _str(msg.get("parent_tool_use_id"))
        task_id = self._task_by_tool.get(parent) if parent else None
        for k, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                call_id = _str(block.get("id"))
                name = _redact_secrets(_str(block.get("name"))) or "tool"
                if not call_id:
                    continue
                self._tools[call_id] = name
                tool_input = block.get("input")
                self._out(fed.events, ev.tool_started(
                    self.key, call_id, name,
                    summary=tool_summary(name, tool_input),
                    input=_jsonable(redact(tool_input)),
                    task_id=task_id or None, turn_id=self._tid(),
                    ts=self.clock()))
            elif parent:
                continue  # a subagent's words stay in its own transcript
            elif btype == "text" and isinstance(block.get("text"), str):
                bid = self._block_id(api_id, "assistant", k)
                final = _redact_secrets(block["text"])
                self._flush(fed.events, bid, final)
                self._completed.add(bid)
                self._out(fed.events, ev.message_completed(
                    self.key, bid, "assistant", final, turn_id=self._tid(),
                    ts=self.clock()))
            elif btype == "thinking":
                bid = self._block_id(api_id, "thinking", k)
                final = _redact_secrets(_str(block.get("thinking")))
                self._flush(fed.events, bid, final)
                self._completed.add(bid)
                ms = msg.get("thinking_duration_ms")
                self._out(fed.events, ev.thinking_completed(
                    self.key, bid, final,
                    duration_s=round(ms / 1000, 3)
                    if isinstance(ms, (int, float)) and not isinstance(
                        ms, bool) and ms >= 0 else None,
                    turn_id=self._tid(), ts=self.clock()))

    def _on_user(self, msg: dict, fed: Fed) -> None:
        message = msg.get("message")
        content = message.get("content") if isinstance(message, dict) \
            else None
        if not isinstance(content, list):
            return
        structured = msg.get("tool_use_result")
        results = [b for b in content if isinstance(b, dict)
                   and b.get("type") == "tool_result"]
        for block in results:
            call_id = _str(block.get("tool_use_id"))
            if not call_id or call_id not in self._tools:
                continue
            name = self._tools.pop(call_id)
            interrupted = isinstance(structured, dict) and \
                structured.get("interrupted") is True
            status = "cancelled" if interrupted else (
                "error" if block.get("is_error") else "ok")
            output = _redact_secrets(_result_text(block.get("content")))
            if len(output) > OUTPUT_LIMIT:
                output = output[:OUTPUT_LIMIT] + "\n…"
            edits = redact(file_edits(structured)) if status == "ok" and \
                len(results) == 1 else None
            self._out(fed.events, ev.tool_completed(
                self.key, call_id, status, name=name, output=output,
                file_edits=edits, turn_id=self._tid(), ts=self.clock()))

    def _on_result(self, msg: dict, fed: Fed) -> None:
        if self.turn is None:
            return
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        counts: dict[str, Any] = {}
        for name, key in (("input_tokens", "input_tokens"),
                          ("output_tokens", "output_tokens"),
                          ("cache_read_tokens", "cache_read_input_tokens"),
                          ("cache_write_tokens",
                           "cache_creation_input_tokens")):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) \
                    and value >= 0:
                counts[name] = value
        cost = msg.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) \
                and cost >= 0 and cost == cost and cost != float("inf"):
            counts["cost_usd"] = float(cost)
        # ``modelUsage`` keeps every model the session used; only the
        # active one's window is the session's now (none if it is not
        # listed: no window beats a wrong one).
        models = msg.get("modelUsage")
        info = models.get(self.model) if isinstance(models, dict) \
            and self.model else None
        window = info.get("contextWindow") if isinstance(info, dict) \
            else None
        if isinstance(window, int) and not isinstance(window, bool) \
                and window > 0:
            counts["context_window"] = window
        if counts:
            self._out(fed.events, ev.usage_updated(
                self.key, turn_id=self._tid(), ts=self.clock(), **counts))
        if msg.get("is_error") and not self.turn.interrupted:
            errors = msg.get("errors")
            text = "; ".join(e for e in errors if isinstance(e, str)) \
                if isinstance(errors, list) else ""
            text = _str(msg.get("result")) or text or _str(msg.get("subtype"))
            self.turn.error = True
            self.turn.reason = _redact_secrets(text)[:300]
            self._out(fed.events, ev.error(
                self.key, _redact_secrets(_str(msg.get("subtype"))) or "error",
                self.turn.reason or "the turn failed", turn_id=self._tid(),
                ts=self.clock()))
        if not self._state_events:
            # A CLI that sends no state frames: the result is all there
            # is to end the turn on.
            self._idle(fed.events)


# ---------------------------------------------------------------------
# Replaying a recorded fixture


FIXTURE_SESSION = "fixture:1"
_FIXTURE_T0 = 1_760_000_000.0
_ALL = Caps(steer=True, plan_mode=True, rollback=True, fork=True,
            stop_task=True, options=frozenset({"model", "effort",
                                               "permission_mode"}))


def _answer_from_reply(response: dict) -> Answer:
    """The ``Answer`` our recorded ``control_response`` stands for."""
    body = response.get("response") if isinstance(response, dict) else None
    body = body if isinstance(body, dict) else {}
    if body.get("behavior") == "allow":
        return Answer(decision="always" if body.get("updatedPermissions")
                      else "allow")
    return Answer(decision="deny", text=_str(body.get("message")))


def _argv_session(tail: list) -> str:
    """The session id a recorded spawn line names: ``--session-id``
    (new or forked), else ``--resume``."""
    found = {}
    for tok in tail or ():
        for flag in ("--session-id=", "--resume="):
            if isinstance(tok, str) and tok.startswith(flag):
                found[flag] = tok[len(flag):]
    return found.get("--session-id=") or found.get("--resume=", "")


def replay(lines: list[dict], *, session: str = FIXTURE_SESSION) -> list:
    """The v2 events a recorded fixture comes to, through the same
    :class:`StreamState` calls the adapter makes: each process of the
    fixture in turn, our lines driving ``begin_turn``, ``answer`` and
    ``interrupt_sent``, the CLI's going to ``feed``, and the recorder's
    closing stdin ending it with ``closed``. Timestamps are the
    fixture's relative times on a fixed epoch."""
    header = lines[0] if lines and "fixture" in lines[0] else {}
    tails = header.get("argv_tails") or []
    now = [0.0]

    def clock() -> float:
        return round(_FIXTURE_T0 + now[0], 3)

    out: list = []
    states: dict[int, StreamState] = {}
    init_ids: dict[int, str] = {}
    for line in lines[1:] if header else lines:
        proc = line.get("proc", 0)
        if isinstance(line.get("t"), (int, float)):
            now[0] = float(line["t"])
        state = states.get(proc)
        if state is None:
            tail = tails[proc] if proc < len(tails) else []
            state = states[proc] = StreamState(
                session, clock=clock, session_id=_argv_session(tail),
                run_id=f"{proc + 1:08x}")
        if "exit" in line:
            out.extend(state.closed())
            continue
        msg = line.get("msg")
        if not isinstance(msg, dict):
            continue
        if line.get("dir") == "out":
            out.extend(_replay_outgoing(state, msg, init_ids, proc))
            continue
        fed = state.feed(msg)
        for ack in fed.acks:
            if ack.request_id == init_ids.get(proc) and ack.ok:
                hs = Handshake.from_body(ack.body)
                caps = _ALL.narrowed(PROVEN).narrowed(hs.caps())
                state.permission_mode = hs.permission_mode
                out.extend(state.start_events(caps, state.session_id))
        out.extend(fed.events)
    return out


def _replay_outgoing(state: StreamState, msg: dict, init_ids: dict,
                     proc: int) -> list:
    mtype = msg.get("type")
    if mtype == "user":
        message = msg.get("message") or {}
        text = message.get("content")
        text = text if isinstance(text, str) else _result_text(text)
        if state.turn is None:
            return state.begin_turn(text)[1]
        # A message sent mid-turn (the steer recording) joins the turn.
        return [state.user_message(text)]
    if mtype == "control_request":
        req = msg.get("request") or {}
        if req.get("subtype") == "initialize":
            init_ids[proc] = msg.get("request_id")
        elif req.get("subtype") == "interrupt":
            return state.interrupt_sent()
        return []
    if mtype == "control_response":
        resp = msg.get("response") or {}
        rid = resp.get("request_id")
        if isinstance(rid, str) and rid in state.pending:
            return state.answer(rid, _answer_from_reply(resp))[1]
    return []
