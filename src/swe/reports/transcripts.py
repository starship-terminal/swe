"""Normalize local harness transcripts into a common, semantic message stream."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from swe.console import sanitize, sanitize_document
from swe.paths import expand_user
from swe.sessions import cursor_store, opencode_store
from swe.sessions.engines.common import sqlite_uri
from swe.sessions.models import Session


@dataclass(frozen=True)
class NormalizedMessage:
    """A semantic transcript message, independent of a harness wire format."""

    role: str
    text: str
    kind: str = "message"
    timestamp: float | None = None


@dataclass
class NormalizedTranscript:
    """Normalized transcript plus provenance and a graceful read status."""

    tool_name: str
    session_id: str
    project_path: str
    messages: list[NormalizedMessage] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    readable: bool = True
    error: str = ""

    @property
    def normalized_text(self) -> str:
        return "\n\n".join(f"{m.role}: {m.text}" for m in self.messages)

    @property
    def digest(self) -> str:
        payload = {
            "tool": self.tool_name,
            "session": self.session_id,
            "path": self.project_path,
            "messages": [
                {"role": m.role, "kind": m.kind, "text": m.text}
                for m in self.messages
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()
        return hashlib.sha256(encoded).hexdigest()


Reader = Callable[[Session], NormalizedTranscript]
READER_REGISTRY: dict[str, Reader] = {}


def register_reader(*tool_names: str) -> Callable[[Reader], Reader]:
    def decorate(reader: Reader) -> Reader:
        for tool_name in tool_names:
            READER_REGISTRY[tool_name] = reader
        return reader

    return decorate


def read_transcript(session: Session) -> NormalizedTranscript:
    """Read one session without allowing malformed local state to escape."""

    reader = READER_REGISTRY.get(session.tool_name)
    if reader is None:
        return _unreadable(session, f"no transcript reader for {session.tool_name}")
    try:
        transcript = reader(session)
        if not transcript.readable and transcript.error == _NOT_FOUND:
            transcript = _read_mirror_copy(session) or transcript
        transcript.messages = _deduplicate(transcript.messages)
        return transcript
    except Exception as exc:
        return _unreadable(session, f"{type(exc).__name__}: {exc}")


_NOT_FOUND = "transcript file not found"


def _read_mirror_copy(session: Session) -> NormalizedTranscript | None:
    """The session read from swe's mirror copy, when its harness deleted
    the transcript and swe kept one (``sessions/transcript_mirror.py``)."""
    from swe.sessions import transcript_mirror

    path = transcript_mirror.find_copy(
        session.tool_name, session.session_id, session.account_home)
    if path is None:
        return None
    transcript = _new(session, [path])
    transcript.messages = _read_jsonl(path)
    return transcript


def _new(session: Session, paths: Iterable[Path] = ()) -> NormalizedTranscript:
    return NormalizedTranscript(
        tool_name=session.tool_name,
        session_id=session.session_id,
        project_path=session.path,
        source_paths=[str(path) for path in paths],
    )


def _unreadable(session: Session, error: str, paths: Iterable[Path] = ()) -> NormalizedTranscript:
    transcript = _new(session, paths)
    transcript.readable = False
    # ``error`` can carry an exception's filename, which the filesystem
    # does not restrict: clean it like everything else that displays.
    transcript.error = sanitize(error)
    return transcript


_ENVELOPE_TAGS = (
    "app-context",
    "environment_context",
    "permissions instructions",
    "recommended_plugins",
    "local-command-caveat",
    "command-name",
    "command-message",
    "local-command-stdout",
)
_ENVELOPE_RE = re.compile(
    r"<(?P<envelope_tag>"
    + "|".join(re.escape(tag) for tag in _ENVELOPE_TAGS)
    + r")\b[^>]*>.*?</(?P=envelope_tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----.*?"
    r"-----END (?P=label)-----",
    re.DOTALL,
)
_AUTHORIZATION_RE = re.compile(
    r"(?im)(\b(?:proxy-)?authorization\s*:\s*)"
    r"(?:(?:bearer|basic|token)\s+)?[^\s\"',;]+"
)
_SENSITIVE_NAME = (
    r"(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|BEARER_TOKEN|CLIENT_SECRET|"
    r"PRIVATE_KEY|SECRET_KEY|PASSWORD|PASSWD|TOKEN)"
)
_ENV_ASSIGNMENT_RE = re.compile(
    r"(?im)(\b(?:export\s+)?(?:[A-Z][A-Z0-9_]*_)?"
    + _SENSITIVE_NAME
    + r"\s*=\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s#;]+)"
)
_JSON_SECRET_RE = re.compile(
    r"(?i)([\"'](?:[A-Z][A-Z0-9_]*_)?"
    + _SENSITIVE_NAME
    + r"[\"']\s*:\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*')"
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"gh[opusr]_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|"
    r"AIza[A-Za-z0-9_-]{30,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")(?![A-Za-z0-9])"
)


def _redact_secrets(text: str) -> str:
    """Remove credential material while preserving useful transcript context."""

    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", text)
    text = _ENV_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    text = _JSON_SECRET_RE.sub(r"\1\"[REDACTED]\"", text)
    return _KNOWN_TOKEN_RE.sub("[REDACTED]", text)


def _clean_text(value: Any) -> str:
    """Extract prose from structured content without flattening protocol JSON."""

    if value is None:
        return ""
    if isinstance(value, str):
        text = _ENVELOPE_RE.sub("", value)
        # Clean before redact, deleting controls rather than spacing
        # them: ``KEY=secret\x07suffix`` fuses into one ``secretsuffix``
        # token the assignment pattern removes whole (a space would leave
        # ``suffix`` behind), and an OSC inside a value collapses instead
        # of stopping the pattern at its own semicolon.
        text = _redact_secrets(sanitize_document(text))
        text = text.strip()
        return text
    if isinstance(value, list):
        parts = [_clean_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if not isinstance(value, dict):
        return str(value).strip()

    block_type = str(value.get("type") or "").lower()
    if block_type in {"image", "image_url", "thinking", "reasoning", "redacted_thinking"}:
        return ""
    for key in ("text", "content", "message", "output", "result"):
        if key in value:
            text = _clean_text(value[key])
            if text:
                return text
    return ""


def _role(value: Any) -> str:
    role = str(value or "").lower()
    if role in {"user", "human"}:
        return "human"
    if role in {"assistant", "agent", "model", "ai"}:
        return "assistant"
    if role in {"tool", "function", "tool_result", "toolresult"}:
        return "tool"
    return ""


def _message(role: str, content: Any, *, kind: str = "message", timestamp: Any = None) -> NormalizedMessage | None:
    if role not in {"human", "assistant", "tool"}:
        return None
    text = _clean_text(content)
    if not text:
        return None
    ts: float | None = None
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        ts = float(timestamp)
    return NormalizedMessage(role=role, text=text, kind=kind, timestamp=ts)


def _messages_from_content(role: str, content: Any, timestamp: Any = None) -> list[NormalizedMessage]:
    if not isinstance(content, list):
        msg = _message(role, content, timestamp=timestamp)
        return [msg] if msg else []
    messages: list[NormalizedMessage] = []
    text_blocks: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            text_blocks.append(block)
            continue
        block_type = str(block.get("type") or "").lower()
        if block_type in {"tool_use", "tool-call", "function_call"}:
            msg = _tool_message(block.get("name"), block.get("input") or block.get("arguments"))
            if msg:
                messages.append(msg)
        elif block_type in {"tool_result", "tool-result", "function_call_output"}:
            msg = _tool_message(block.get("name") or "tool result", output=block.get("content") or block.get("output"))
            if msg:
                messages.append(msg)
        else:
            text_blocks.append(block)
    msg = _message(role, text_blocks, timestamp=timestamp)
    if msg:
        messages.insert(0, msg)
    return messages


def _tool_message(name: Any, arguments: Any = None, output: Any = None) -> NormalizedMessage | None:
    label = sanitize(str(name or "tool")).strip()
    if output is None and isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
            if isinstance(decoded, (dict, list)):
                arguments = decoded
        except (json.JSONDecodeError, RecursionError):
            pass
    content = output if output is not None else arguments
    text = _clean_text(content)
    if isinstance(arguments, dict) and output is None:
        evidence = next(
            (
                arguments.get(key)
                for key in ("command", "cmd", "path", "file_path", "filePath", "filename")
                if arguments.get(key)
            ),
            None,
        )
        text = _clean_text(evidence) or text
    if not text:
        return None
    return NormalizedMessage("tool", f"{label}: {text}", kind="tool")


def _messages_from_record(record: Any) -> list[NormalizedMessage]:
    if not isinstance(record, dict):
        return []
    text_envelope = record.get("text") if isinstance(record.get("text"), dict) else None
    if text_envelope is not None and text_envelope.get("role"):
        return _messages_from_content(
            _role(text_envelope.get("role")),
            text_envelope.get("content"),
            record.get("timestamp"),
        )
    if isinstance(record.get("payload"), dict):
        payload = record["payload"]
        payload_type = str(payload.get("type") or "").lower()
        if record.get("type") == "response_item" and payload_type == "message":
            return _messages_from_content(
                _role(payload.get("role")), payload.get("content"), record.get("timestamp")
            )
        if record.get("type") == "response_item" and payload_type in {"function_call", "custom_tool_call"}:
            msg = _tool_message(payload.get("name"), payload.get("arguments") or payload.get("input"))
            return [msg] if msg else []
        if record.get("type") == "response_item" and payload_type in {"function_call_output", "custom_tool_call_output"}:
            msg = _tool_message(payload.get("name") or "tool result", output=payload.get("output"))
            return [msg] if msg else []

    event_type = str(record.get("type") or "").lower()
    if event_type in {"session_meta", "session_start", "system", "developer", "progress", "summary"}:
        return []
    nested = record.get("message")
    if isinstance(nested, dict):
        nested_text = nested.get("text") if isinstance(nested.get("text"), dict) else {}
        role = _role(nested.get("role") or nested_text.get("role") or record.get("role"))
        return _messages_from_content(
            role,
            nested.get("content", nested_text.get("content", nested.get("text"))),
            record.get("timestamp"),
        )
    role = _role(record.get("role") or record.get("type"))
    return _messages_from_content(
        role,
        record.get("content", record.get("text", record.get("message"))),
        record.get("timestamp"),
    )


def _read_jsonl(path: Path) -> list[NormalizedMessage]:
    messages: list[NormalizedMessage] = []
    from swe.sessions import zstd_copy
    import io
    plain = zstd_copy.plain(str(path))
    handle = io.TextIOWrapper(io.BytesIO(plain), encoding="utf-8", errors="replace") \
        if plain is not None else path.open(encoding="utf-8", errors="replace")
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                continue
            messages.extend(_messages_from_record(record))
    return messages


def _read_json_messages(path: Path) -> list[NormalizedMessage]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        records = (
            data.get("messages")
            or data.get("history")
            or data.get("conversation")
            or data.get("logs")
            or []
        )
    else:
        records = []
    messages: list[NormalizedMessage] = []
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("message"), dict) and "role" not in record:
            messages.extend(_messages_from_record(record))
        else:
            messages.extend(_messages_from_record(record))
    return messages


def _deduplicate(messages: list[NormalizedMessage]) -> list[NormalizedMessage]:
    result: list[NormalizedMessage] = []
    for message in messages:
        if result and (message.role, message.kind, message.text) == (
            result[-1].role,
            result[-1].kind,
            result[-1].text,
        ):
            continue
        result.append(message)
    return result


# (root, patterns) -> (built at, {stem or path part: first candidate carrying
# it}). One walk of a root answers every lookup under it for a few seconds;
# see _find_session_file.
_SESSION_FILE_INDEX: dict[tuple[str, tuple[str, ...]], tuple[float, dict[str, Path]]] = {}
_SESSION_FILE_INDEX_LOCK = threading.Lock()
# How long a walk is trusted for a hit. A batch of lookups lands well inside
# it; a long-lived process (the remote tailer, a picker left open) re-walks
# after it, so a copy that appears beside the one it found is picked up the
# way the per-lookup glob would have.
_SESSION_FILE_INDEX_TTL_S = 10.0


def _session_file_index(
    root: Path, patterns: tuple[str, ...], rebuild: bool = False
) -> dict[str, Path]:
    key = (str(root), tuple(patterns))
    now = time.monotonic()
    with _SESSION_FILE_INDEX_LOCK:
        cached = None if rebuild else _SESSION_FILE_INDEX.get(key)
        if cached is not None and now - cached[0] <= _SESSION_FILE_INDEX_TTL_S:
            return cached[1]
        index: dict[str, Path] = {}
        # Candidates in the order the per-lookup glob produced them, and the
        # first one to carry a key keeps it, so a lookup returns the same
        # file the old scan stopped at.
        for pattern in patterns:
            for path in root.glob(f"**/{pattern}"):
                for part in (path.stem, *path.parts):
                    index.setdefault(part, path)
        _SESSION_FILE_INDEX[key] = (now, index)
        return index


def _find_session_file(root: Path, session_id: str, patterns: tuple[str, ...] = ("*.jsonl",)) -> Path | None:
    """The transcript under ``root`` whose stem is ``session_id``, or that sits
    in a directory named for it.

    The tree is walked once per root and pattern set and the walk is reused
    for ``_SESSION_FILE_INDEX_TTL_S``, so a report or picker resolving N
    sessions pays for one walk, not N. A miss, or a hit whose file has
    since gone, walks again before answering, so a transcript created after
    the last walk is still found and "not found" always reflects the disk
    as it is now.
    """
    if session_id:
        direct = expand_user(session_id)
        if direct.is_file():
            return direct
    sid = Path(session_id).stem if session_id else ""
    if not sid:
        # An empty id cannot name a transcript. Falling back to the newest
        # file under the tool root would attribute an unrelated session's
        # content to this one (Pi legitimately emits session_id="").
        return None
    if not root.exists():
        return None
    path = _session_file_index(root, patterns).get(sid)
    if path is not None and os.path.lexists(path):
        return path
    return _session_file_index(root, patterns, rebuild=True).get(sid)


# The transcript subdir each account-aware harness keeps inside its home,
# so a session in an SWE account home resolves there, not the default.
# Codex is absent: its account homes resolve through `_codex_rollout_path`.
_ACCOUNT_SUBDIRS = {"claude": "projects"}


def _jsonl_reader(root: str, patterns: tuple[str, ...] = ("*.jsonl",)) -> Reader:
    def read(session: Session) -> NormalizedTranscript:
        base = root
        subdir = _ACCOUNT_SUBDIRS.get(session.tool_name)
        if subdir and session.account_home:
            base = os.path.join(session.account_home, subdir)
        path = _find_session_file(Path(os.path.expanduser(base)), session.session_id, patterns)
        if path is None:
            return _unreadable(session, _NOT_FOUND)
        transcript = _new(session, [path])
        transcript.messages = _read_jsonl(path)
        return transcript

    return read


for _name, _root, _patterns in (
    ("claude", "~/.claude/projects", ("*.jsonl",)),
    ("droid", "~/.factory/sessions", ("*.jsonl",)),
    ("pi", "~/.pi/agent/sessions", ("*.jsonl",)),
):
    READER_REGISTRY[_name] = _jsonl_reader(_root, _patterns)


def _cursor_store_block(block: Any) -> Any:
    """Rename a store blob's AI-SDK fields to the block shapes readers know."""
    if not isinstance(block, dict):
        return block
    kind = str(block.get("type") or "").lower()
    if kind == "tool-call":
        return {
            "type": "tool_use",
            "name": block.get("toolName"),
            "input": block.get("args"),
        }
    if kind == "tool-result":
        return {
            "type": "tool_result",
            "name": block.get("toolName"),
            "content": block.get("result")
            if block.get("result") is not None
            else block.get("experimental_content"),
        }
    return block


def _read_cursor_store(path: str) -> list[NormalizedMessage]:
    messages: list[NormalizedMessage] = []
    for record in cursor_store.read_messages(path):
        content = record.get("content")
        blocks = (
            [_cursor_store_block(block) for block in content]
            if isinstance(content, list)
            else content
        )
        messages.extend(_messages_from_content(_role(record.get("role")), blocks))
    return messages


@register_reader("cursor")
def _read_cursor(session: Session) -> NormalizedTranscript:
    """The agent-transcripts JSONL when it exists, else the ACP ``store.db``.

    ``~/.cursor/acp-sessions/<id>/`` and ``~/.cursor/chats/<ws>/<id>/`` hold
    the sessions the editor and headless runs make without a transcript
    file; ``cursor_store`` decodes their manifest-ordered message blobs.
    """
    home = session.account_home or os.path.expanduser("~/.cursor")
    projects = Path(os.path.join(home, "projects"))
    sid = Path(session.session_id).stem if session.session_id else ""
    if sid and projects.exists():
        path = _session_file_index(projects, ("*.jsonl",)).get(sid)
        if path is not None and os.path.lexists(path):
            transcript = _new(session, [path])
            transcript.messages = _read_jsonl(path)
            return transcript
    store = cursor_store.find_store(session.session_id, home)
    if store:
        transcript = _new(session, [Path(store)])
        transcript.messages = _read_cursor_store(store)
        return transcript
    # Neither the cached index nor a store knows it: fall back to the full
    # resolver, which re-walks so a transcript written after the last index
    # is still found before answering "not found".
    path = _find_session_file(projects, session.session_id, ("*.jsonl",))
    if path is not None:
        transcript = _new(session, [path])
        transcript.messages = _read_jsonl(path)
        return transcript
    return _unreadable(session, _NOT_FOUND)


@register_reader("codex")
def _read_codex(session: Session) -> NormalizedTranscript:
    # `_codex_rollout_path` is the one resolver for rollout files: it finds
    # the dated path, the flat spot archiving moved it to, and any home a
    # profile keeps, so an archived thread reads back like a live one.
    from swe.sessions.status import _codex_rollout_path

    direct = expand_user(session.session_id) if session.session_id else None
    if direct is not None and direct.is_file():
        found = str(direct)
    else:
        found = _codex_rollout_path(session.session_id, session.account_home)
    if not found:
        return _unreadable(session, _NOT_FOUND)
    path = Path(found)
    transcript = _new(session, [path])
    transcript.messages = _read_jsonl(path)
    return transcript


@register_reader("freebuff")
def _read_freebuff(session: Session) -> NormalizedTranscript:
    root = Path(os.path.expanduser("~/.config/manicode/projects"))
    chat = _find_session_file(root, session.session_id, ("log.jsonl",))
    if chat is None:
        return _unreadable(session, "Freebuff log not found")
    transcript = _new(session, [chat])
    state = chat.parent / "run-state.json"
    if state.exists():
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            history = data.get("sessionState", {}).get("mainAgentState", {}).get("messageHistory", [])
            transcript.messages = [m for row in history for m in _messages_from_record(row)]
            transcript.source_paths.append(str(state))
        except (OSError, json.JSONDecodeError, RecursionError):
            pass
    if not transcript.messages:
        transcript.messages = _read_jsonl(chat)
    return transcript


@register_reader("grok")
def _read_grok(session: Session) -> NormalizedTranscript:
    root = Path(os.path.expanduser("~/.grok/sessions"))
    path = _find_session_file(root, session.session_id, ("chat_history.jsonl",))
    if path is None:
        return _unreadable(session, "Grok chat history not found")
    transcript = _new(session, [path])
    transcript.messages = _read_jsonl(path)
    return transcript


@register_reader("continue")
def _read_continue(session: Session) -> NormalizedTranscript:
    path = Path(os.path.expanduser(f"~/.continue/sessions/{session.session_id}.json"))
    if not path.exists():
        return _unreadable(session, "Continue session not found")
    transcript = _new(session, [path])
    transcript.messages = _read_json_messages(path)
    return transcript


_CLINE_USER_INPUT_RE = re.compile(
    r"^\s*<user_input\b[^>]*>(?P<inner>.*?)</user_input\s*>\s*$",
    re.IGNORECASE | re.DOTALL,
)


@register_reader("cline")
def _read_cline(session: Session) -> NormalizedTranscript:
    """Cline transcripts: 3.x sessions dir, then the legacy tasks layout.

    The CLI writes ``data/sessions/<id>/<id>.messages.json`` — a ``messages``
    list of ``{role, content: [{type: text|thinking, ...}], ts}`` where user
    text arrives wrapped in ``<user_input mode="…">``. Unwrapping only fires
    on a fully wrapped user message (mid-text literals and assistant text
    quoting the markup survive untouched), before ``_clean_text`` so secret
    redaction still sees the inner text. The VS Code extension kept per-task
    dirs at ``data/tasks/<id>/`` instead.
    """
    sess_dir = Path(os.path.expanduser(
        f"~/.cline/data/sessions/{session.session_id}"))
    messages_path = sess_dir / f"{session.session_id}.messages.json"
    if messages_path.exists():
        try:
            data = json.loads(messages_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, RecursionError) as exc:
            return _unreadable(
                session, f"Cline transcript error: {exc}", [messages_path])
        embedded = data.get("sessionId") if isinstance(data, dict) else None
        if embedded is not None and embedded != session.session_id:
            return _unreadable(
                session, "Cline transcript session mismatch", [messages_path])
        records = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(records, list):
            return _unreadable(
                session, "Cline transcript has no messages list",
                [messages_path])
        transcript = _new(session, [messages_path])
        for record in records:
            if not isinstance(record, dict):
                continue
            role = _role(record.get("role"))
            content = record.get("content")
            if role == "human" and isinstance(content, list):
                content = [
                    {**block, "text": _CLINE_USER_INPUT_RE.sub(
                        r"\g<inner>", block["text"])}
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                    else block
                    for block in content
                ]
            transcript.messages.extend(_messages_from_content(
                role, content, record.get("ts")))
        return transcript
    root = Path(os.path.expanduser(f"~/.cline/data/tasks/{session.session_id}"))
    paths = [root / "api_conversation_history.json", root / "ui_messages.json"]
    existing = [path for path in paths if path.exists()]
    if not existing:
        return _unreadable(session, "Cline task transcript not found")
    transcript = _new(session, existing)
    for path in existing:
        try:
            transcript.messages.extend(_read_json_messages(path))
        except (OSError, json.JSONDecodeError, RecursionError):
            continue
    return transcript


@register_reader("gemini")
def _read_gemini(session: Session) -> NormalizedTranscript:
    index = Path(os.path.expanduser("~/.gemini/projects.json"))
    try:
        projects = json.loads(index.read_text(encoding="utf-8")).get("projects", {})
        hash_dir = projects.get(session.path) or projects.get(session.session_id)
    except (OSError, json.JSONDecodeError, AttributeError, RecursionError):
        hash_dir = None
    if not hash_dir:
        return _unreadable(session, "Gemini project mapping not found")
    path = Path(os.path.expanduser(f"~/.gemini/tmp/{hash_dir}/logs.json"))
    if not path.exists():
        return _unreadable(session, "Gemini logs not found")
    transcript = _new(session, [path])
    transcript.messages = _read_json_messages(path)
    return transcript


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        return sqlite3.connect(sqlite_uri(path), uri=True)
    except sqlite3.Error:
        return None


def _sqlite_json_parts(session: Session, db_path: str) -> NormalizedTranscript:
    path = Path(os.path.expanduser(db_path))
    conn = _open_ro(path)
    if conn is None:
        return _unreadable(session, "SQLite store not found", [path])
    transcript = _new(session, [path])
    try:
        roles: dict[str, str] = {}
        for message_id, raw in conn.execute("SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created", (session.session_id,)):
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError, RecursionError):
                continue
            roles[str(message_id)] = _role(data.get("role"))
            direct = _message(roles[str(message_id)], data.get("content") or data.get("text"))
            if direct:
                transcript.messages.append(direct)
        for message_id, raw in conn.execute("SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created", (session.session_id,)):
            try:
                part = json.loads(raw)
            except (TypeError, json.JSONDecodeError, RecursionError):
                continue
            part_type = str(part.get("type") or "").lower()
            if part_type in {"reasoning", "thinking"}:
                continue  # the model's thinking, dropped like every other reader's
            if part_type in {"tool", "tool-invocation", "tool_result"}:
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                msg = _tool_message(part.get("tool"), state.get("input"), state.get("output"))
            else:
                msg = _message(roles.get(str(message_id), "assistant"), part.get("text") or part.get("content"))
            if msg:
                transcript.messages.append(msg)
        # OpenCode 2.x writes new turns only to ``session_message``, each
        # row carrying its whole content. Rows the 2.x migration copied
        # from ``message`` keep their ids and were read above.
        for _id, kind, _seq, _ts, data in opencode_store.v2_rows(conn, session.session_id):
            transcript.messages.extend(_opencode_v2_messages(kind, data))
    except sqlite3.Error as exc:
        return _unreadable(session, f"SQLite schema error: {exc}", [path])
    finally:
        conn.close()
    return transcript


def _opencode_v2_messages(kind: str, data: dict) -> list[NormalizedMessage]:
    """One ``session_message`` row as transcript messages.

    A prompt is the human's ``text``. An assistant row's ``content`` holds
    its blocks in order: ``text`` runs join into one message, each ``tool``
    block is a tool message with its ``state.input`` and ``state.content``
    (the output), and ``reasoning`` is dropped like every other reader's
    thinking. System, compaction and selection rows are bookkeeping.
    """
    if kind in opencode_store.PROMPT_TYPES:
        msg = _message("human", data.get("text"))
        return [msg] if msg else []
    if kind != "assistant":
        return []
    out: list[NormalizedMessage] = []
    words: list[str] = []

    def flush() -> None:
        msg = _message("assistant", words)
        if msg:
            out.append(msg)
        words.clear()

    for block in opencode_store.content_blocks(data):
        block_type = str(block.get("type") or "").lower()
        if block_type == "text" and isinstance(block.get("text"), str):
            words.append(block["text"])
        elif block_type == "tool":
            flush()
            state = block.get("state") if isinstance(block.get("state"), dict) else {}
            msg = _tool_message(block.get("name"), state.get("input"), state.get("content") or state.get("error"))
            if msg:
                out.append(msg)
    flush()
    return out


@register_reader("opencode")
def _read_opencode(session: Session) -> NormalizedTranscript:
    return _sqlite_json_parts(session, "~/.local/share/opencode/opencode.db")


@register_reader("kilo")
def _read_kilo(session: Session) -> NormalizedTranscript:
    # Kilo is an opencode fork: kilo.db shares the drizzle message/part schema.
    return _sqlite_json_parts(session, "~/.local/share/kilo/kilo.db")


@register_reader("copilot")
def _read_copilot(session: Session, max_turns: int | None = None) -> NormalizedTranscript:
    """``max_turns`` reads only the newest that many turns (the live
    tailer); reports read them all."""
    path = Path(os.path.expanduser("~/.copilot/session-store.db"))
    conn = _open_ro(path)
    if conn is None:
        return _unreadable(session, "Copilot store not found", [path])
    transcript = _new(session, [path])
    try:
        if max_turns is None:
            rows = conn.execute("SELECT user_message, assistant_response FROM turns WHERE session_id = ? ORDER BY turn_index", (session.session_id,))
        else:
            rows = conn.execute(
                "SELECT user_message, assistant_response FROM turns WHERE session_id = ? "
                "ORDER BY turn_index DESC LIMIT ?", (session.session_id, max_turns)).fetchall()
            rows.reverse()
        for user, assistant in rows:
            for role, content in (("human", user), ("assistant", assistant)):
                msg = _message(role, content)
                if msg:
                    transcript.messages.append(msg)
    except sqlite3.Error as exc:
        return _unreadable(session, f"SQLite schema error: {exc}", [path])
    finally:
        conn.close()
    return transcript


@register_reader("devin")
def _read_devin(session: Session, max_nodes: int | None = None) -> NormalizedTranscript:
    """Devin keeps each turn as a JSON ``chat_message`` in ``message_nodes``.

    The nodes form a forest: a streamed assistant reply is stored again
    each time it grows, as a sibling under the same parent, and the
    session's ``main_chain_id`` points at the head of the chain that holds
    the final version of every message. Walking parents from that head
    gives one node per message. Without a head (an older row) the newest
    node stands in for it: a chain is still walked, never the whole
    forest, so an abandoned branch cannot leak into the transcript.

    System rows carry rules, environment and tool catalogues, never the
    conversation, so they are dropped. An assistant row's ``tool_calls``
    become tool messages, and a ``tool`` row's output is labelled with the
    name of the call it answers (``tool_call_id`` joins the two).

    ``max_nodes`` reads only the newest that many nodes of the chain, one
    indexed row at a time from the head, so a long session (whose forest
    can hold hundreds of MB of streamed copies) is never loaded whole.
    The live tailer uses it; reports read everything.
    """
    path = Path(os.path.expanduser(DEVIN_DB))
    conn = _open_ro(path)
    if conn is None:
        return _unreadable(session, "Devin store not found", [path])
    transcript = _new(session, [path])
    try:
        head = _devin_head(conn, session.session_id)
        if max_nodes is not None:
            # A margin of older nodes resolves tool-call names for results
            # near the window's start; their own messages are not kept.
            # Without it a result's label would change as its call slid
            # out, and the live tailer would see the window as rewritten.
            ordered = _devin_chain_tail(conn, session.session_id, head, max_nodes + _DEVIN_NAME_MARGIN)
            context = max(0, len(ordered) - max_nodes)
        else:
            ordered = _devin_chain(conn, session.session_id, head)
            context = 0
        transcript.messages = _devin_messages(
            [_devin_parts(raw) for raw in ordered], context)
    except sqlite3.Error as exc:
        return _unreadable(session, f"SQLite schema error: {exc}", [path])
    finally:
        conn.close()
    return transcript


DEVIN_DB = "~/.local/share/devin/cli/sessions.db"

# Extra chain nodes a bounded Devin read walks only to learn tool-call
# names (a result names the call it answers, which sits just before it).
_DEVIN_NAME_MARGIN = 64


class _DevinTurn:
    """A human or assistant node: its message and its tool calls as
    ``(call id or "", name, message)``."""

    __slots__ = ("message", "calls")

    def __init__(self, message, calls) -> None:
        self.message = message
        self.calls = calls


class _DevinResult:
    """A ``tool`` node. Its label is the name of the call it answers,
    known only from the nodes before it, so the message is built for a
    label and rebuilt only if that label changes."""

    __slots__ = ("call_id", "content", "_label", "_message")

    def __init__(self, call_id: str, content: Any) -> None:
        self.call_id = call_id
        self.content = content
        self._label: str | None = None
        self._message: NormalizedMessage | None = None

    def message(self, label: str) -> NormalizedMessage | None:
        if label != self._label:
            self._message = _tool_message(label, output=self.content)
            self._label = label
        return self._message


def _devin_parts(raw: Any) -> "_DevinTurn | _DevinResult | None":
    """One ``chat_message`` decoded once; None for rows that show nothing
    (system rows, malformed JSON)."""
    try:
        record = json.loads(raw)
    except (TypeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    role = _role(record.get("role"))
    if role == "tool":
        return _DevinResult(str(record.get("tool_call_id") or ""), record.get("content"))
    if role not in {"human", "assistant"}:
        return None
    calls = []
    for call in record.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        call_id = str(call["id"]) if call.get("id") and name else ""
        calls.append((call_id, str(name or ""), _tool_message(name, call.get("arguments"))))
    return _DevinTurn(_message(role, record.get("content")), calls)


def _devin_messages(parts: list, context: int) -> list[NormalizedMessage]:
    """The messages of decoded chain nodes, oldest first. The first
    ``context`` nodes only teach tool-call names."""
    call_names: dict[str, str] = {}
    out: list[NormalizedMessage] = []
    for index, part in enumerate(parts):
        keep = index >= context
        if isinstance(part, _DevinResult):
            if keep:
                msg = part.message(call_names.get(part.call_id, "tool result"))
                if msg:
                    out.append(msg)
            continue
        if part is None:
            continue
        if keep and part.message:
            out.append(part.message)
        for call_id, name, msg in part.calls:
            if call_id:
                call_names[call_id] = name
            if keep and msg:
                out.append(msg)
    return out


def _devin_head(conn: sqlite3.Connection, session_id: str) -> int | None:
    row = conn.execute(
        "SELECT main_chain_id FROM sessions WHERE id = ?", (session_id,),
    ).fetchone()
    return row[0] if row else None


def _devin_chain(conn: sqlite3.Connection, session_id: str, head: int | None) -> list[str]:
    """Every node of the main chain, oldest first (loads the forest)."""
    nodes: dict[int, tuple[int | None, str]] = {}
    for node_id, parent_id, raw in conn.execute(
        "SELECT node_id, parent_node_id, chat_message FROM message_nodes "
        "WHERE session_id = ? ORDER BY node_id",
        (session_id,),
    ):
        nodes[node_id] = (parent_id, raw)
    if head not in nodes and nodes:
        head = max(nodes)
    ordered: list[str] = []
    seen: set[int] = set()
    cursor: int | None = head
    while cursor in nodes and cursor not in seen:
        seen.add(cursor)
        parent_id, raw = nodes[cursor]
        ordered.append(raw)
        cursor = parent_id
    ordered.reverse()
    return ordered


def _devin_start(conn: sqlite3.Connection, session_id: str, head: int | None) -> int | None:
    """The node a chain walk starts at: the head when its row exists,
    else the newest node."""
    row = conn.execute(
        "SELECT node_id FROM message_nodes WHERE session_id = ? AND node_id = ?",
        (session_id, head),
    ).fetchone() if head is not None else None
    if row is None:
        row = conn.execute(
            "SELECT MAX(node_id) FROM message_nodes WHERE session_id = ?", (session_id,),
        ).fetchone()
    return row[0] if row else None


def _devin_walk(
    conn: sqlite3.Connection,
    session_id: str,
    cursor: int | None,
    max_nodes: int,
    known: dict[int, int] | None = None,
) -> tuple[list[tuple[int, int | None, str]], int | None]:
    """Walk parents from ``cursor``, one indexed row at a time.

    Returns the nodes read as ``(node_id, parent_id, chat_message)``,
    newest first, and where the walk stopped: a node in ``known`` (not
    read), or None when the chain ended, looped, or ``max_nodes`` were
    read."""
    known = known or {}
    nodes: list[tuple[int, int | None, str]] = []
    seen: set[int] = set()
    while cursor is not None and cursor not in seen and len(nodes) < max_nodes:
        if cursor in known:
            return nodes, cursor
        seen.add(cursor)
        node = conn.execute(
            "SELECT parent_node_id, chat_message FROM message_nodes "
            "WHERE session_id = ? AND node_id = ?",
            (session_id, cursor),
        ).fetchone()
        if node is None:
            break
        nodes.append((cursor, node[0], node[1]))
        cursor = node[0]
    return nodes, None


def _devin_chain_tail(
    conn: sqlite3.Connection, session_id: str, head: int | None, max_nodes: int
) -> list[str]:
    """The newest ``max_nodes`` nodes of the main chain, oldest first,
    walked parent by parent through the (session_id, node_id) index."""
    nodes, _stop = _devin_walk(
        conn, session_id, _devin_start(conn, session_id, head), max_nodes)
    return [raw for _n, _p, raw in reversed(nodes)]


# A DevinChain reads the whole window again at least this often while
# its session keeps changing, so anything the step-by-step read can't
# see (a row edited in place, a deleted node) lasts at most this long.
DEVIN_FULL_READ_S = 60.0


class DevinChain:
    """A bounded Devin read kept between polls, for the live tailer.

    ``read_transcript_tail`` walks up to ``max_nodes`` chain nodes, one
    point query and one ``json.loads`` each: 100 to 500 ms for a long
    session on a 9.8 GB store, every second the session streams (#1522).
    This keeps the window's decoded nodes and, on the next read, fetches
    only what changed since:

    - Rows written since the last read (``row_id`` above the last one
      seen). Devin writes with ``INSERT OR REPLACE``, so a rewritten
      node comes back under a new row_id: a kept node among them is read
      again. One whose parent changed sends the read back to a full walk.
    - The walk from the head stops at the first kept node; the kept
      chain is cut there and the new nodes go on top. A switched chain
      head (``main_chain_id`` moved to another branch) meets the kept
      chain at the fork, so it splices the same way.

    A walk that meets nothing kept within the window, a missing parent,
    a store error, or DEVIN_FULL_READ_S since the last full walk all
    read the window whole, as ``read_transcript_tail`` does. Messages
    come out exactly as that read would build them.
    """

    def __init__(self, full_every_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._full_every_s = full_every_s
        self._clock = clock
        # (node_id, parent_id, decoded parts), oldest first.
        self._nodes: list[tuple[int, int | None, Any]] = []
        self._index: dict[int, int] = {}
        self._last_row = 0
        self._full_at = float("-inf")
        self.used_at = float("-inf")
        # (st_dev, st_ino) of the file the kept nodes came from.
        self._ident: tuple | None = None
        # How the last read went, for tests and benchmarks: "full" or
        # "step", and how many chain nodes it fetched.
        self.last_read = ("", 0)

    def reset(self) -> None:
        self._nodes = []
        self._index = {}
        self._last_row = 0
        self._full_at = float("-inf")

    def read(self, session: Session, max_nodes: int) -> NormalizedTranscript:
        path = Path(os.path.expanduser(DEVIN_DB))
        conn = _open_ro(path)
        if conn is None:
            self.reset()
            return _unreadable(session, "Devin store not found", [path])
        transcript = _new(session, [path])
        sid = session.session_id
        limit = max_nodes + _DEVIN_NAME_MARGIN
        now = self._clock()
        self.used_at = now
        try:
            st = os.stat(path)
            ident = (st.st_dev, st.st_ino)
        except OSError:
            ident = None
        if ident != self._ident:
            # Another file (replaced, restored): nothing kept applies.
            self.reset()
            self._ident = ident
        every = DEVIN_FULL_READ_S if self._full_every_s is None else self._full_every_s
        try:
            # Read before the walk: a row landing mid-walk sits above it
            # and is fetched next time.
            newest = conn.execute(
                "SELECT MAX(row_id) FROM message_nodes WHERE session_id = ?", (sid,),
            ).fetchone()
            last_row = (newest[0] if newest else None) or 0
            head = _devin_head(conn, sid)
            if last_row < self._last_row:
                # Rows went away under the watermark (a rewind, a restore
                # in place): what is kept can't be trusted.
                self.reset()
            if not (self._nodes and now - self._full_at < every
                    and self._step(conn, sid, head, limit)):
                self._full(conn, sid, head, limit)
                self._full_at = now
            self._last_row = last_row
            parts = [part for _n, _p, part in self._nodes]
            transcript.messages = _deduplicate(
                _devin_messages(parts, max(0, len(parts) - max_nodes)))
        except sqlite3.Error as exc:
            self.reset()
            return _unreadable(session, f"SQLite schema error: {exc}", [path])
        finally:
            conn.close()
        return transcript

    def _keep(self, nodes: list[tuple[int, int | None, Any]]) -> None:
        self._nodes = nodes
        self._index = {node_id: i for i, (node_id, _p, _x) in enumerate(nodes)}

    def _full(self, conn: sqlite3.Connection, sid: str, head: int | None,
              limit: int) -> None:
        walked, _stop = _devin_walk(conn, sid, _devin_start(conn, sid, head), limit)
        self._keep([(n, p, _devin_parts(raw)) for n, p, raw in reversed(walked)])
        self.last_read = ("full", len(walked))

    def _step(self, conn: sqlite3.Connection, sid: str, head: int | None,
              limit: int) -> bool:
        """Bring the kept window up to date; False when only a full walk
        can (the caller does one)."""
        rewritten: list[int] = []
        for node_id, parent_id in conn.execute(
            "SELECT node_id, parent_node_id FROM message_nodes "
            "WHERE session_id = ? AND row_id > ?",
            (sid, self._last_row),
        ):
            at = self._index.get(node_id)
            if at is None:
                continue
            if self._nodes[at][1] != parent_id:
                return False
            rewritten.append(node_id)
        walked, met = _devin_walk(
            conn, sid, _devin_start(conn, sid, head), limit, self._index)
        if met is None:
            return False
        kept = self._nodes[: self._index[met] + 1]
        fresh = [(n, p, _devin_parts(raw)) for n, p, raw in reversed(walked)]
        nodes = (kept + fresh)[-limit:]
        index = {node_id: i for i, (node_id, _p, _x) in enumerate(nodes)}
        fetched = len(walked)
        for node_id in rewritten:
            at = index.get(node_id)
            if at is None or at >= len(nodes) - len(fresh):
                continue  # dropped from the chain, or just read
            row = conn.execute(
                "SELECT parent_node_id, chat_message FROM message_nodes "
                "WHERE session_id = ? AND node_id = ?",
                (sid, node_id),
            ).fetchone()
            if row is None or row[0] != nodes[at][1]:
                return False
            nodes[at] = (node_id, row[0], _devin_parts(row[1]))
            fetched += 1
        if len(nodes) < limit and nodes and nodes[0][1] is not None:
            # The head moved back down the chain (or to a shorter
            # branch): older nodes the window had trimmed belong in it
            # again, as a full walk would read them.
            older, _stop = _devin_walk(
                conn, sid, nodes[0][1], limit - len(nodes), index)
            nodes = [(n, p, _devin_parts(raw)) for n, p, raw in reversed(older)] + nodes
            fetched += len(older)
        self._keep(nodes)
        self.last_read = ("step", fetched)
        return True


# Readers that can stop at the newest rows, keyed by tool. The rest keep
# a whole JSON document per session (Cline, Continue, Gemini, Forge's
# context blob), which has to be parsed whole to find its end anyway.
_TAIL_READERS: dict[str, Callable[[Session, int], NormalizedTranscript]] = {
    "devin": lambda session, n: _read_devin(session, max_nodes=n),
    "copilot": lambda session, n: _read_copilot(session, max_turns=n),
    "crush": lambda session, n: _read_crush(session, max_messages=n),
}


def read_transcript_tail(
    session: Session, max_messages: int, chain: "DevinChain | None" = None,
) -> NormalizedTranscript:
    """``read_transcript`` bounded to roughly the newest ``max_messages``
    where the store allows it (Devin, Copilot, Crush); a full read
    elsewhere. ``chain`` (Devin only) keeps the window between calls, so
    a repeat read fetches only what changed; see ``DevinChain``."""
    reader = _TAIL_READERS.get(session.tool_name)
    if reader is not None:
        try:
            if chain is not None and session.tool_name == "devin":
                return chain.read(session, max_messages)
            transcript = reader(session, max_messages)
            transcript.messages = _deduplicate(transcript.messages)
            return transcript
        except Exception as exc:
            if chain is not None:
                chain.reset()
            return _unreadable(session, f"{type(exc).__name__}: {exc}")
    return read_transcript(session)


@register_reader("forge")
def _read_forge(session: Session) -> NormalizedTranscript:
    path = Path(os.path.expanduser("~/.forge/.forge.db"))
    conn = _open_ro(path)
    if conn is None:
        return _unreadable(session, "Forge store not found", [path])
    transcript = _new(session, [path])
    try:
        row = conn.execute("SELECT context FROM conversations WHERE conversation_id = ?", (session.session_id,)).fetchone()
        context = json.loads(row[0]) if row and row[0] else {}
        records = context.get("messages", []) if isinstance(context, dict) else []
        transcript.messages = [message for record in records for message in _messages_from_record(record.get("message", record) if isinstance(record, dict) else record)]
    except (sqlite3.Error, json.JSONDecodeError, RecursionError) as exc:
        return _unreadable(session, f"Forge transcript error: {exc}", [path])
    finally:
        conn.close()
    return transcript


@register_reader("crush")
def _read_crush(session: Session, max_messages: int | None = None) -> NormalizedTranscript:
    """``max_messages`` reads only the newest that many rows (the live
    tailer); reports read them all."""
    index = Path(os.path.expanduser("~/.local/share/crush/projects.json"))
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
        projects = data.get("projects", data) if isinstance(data, dict) else data
    except (OSError, json.JSONDecodeError, RecursionError):
        projects = []
    for project in projects if isinstance(projects, list) else []:
        if not isinstance(project, dict) or project.get("path") != session.path:
            continue
        db_path = Path(os.path.expanduser(str(project.get("data_dir") or ""))) / "crush.db"
        conn = _open_ro(db_path)
        if conn is None:
            continue
        transcript = _new(session, [db_path])
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "messages" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
                role_col = "role" if "role" in columns else "sender"
                text_col = "content" if "content" in columns else "message"
                query = f"SELECT {role_col}, {text_col} FROM messages WHERE session_id = ?"
                if max_messages is None:
                    rows = conn.execute(query + " ORDER BY rowid", (session.session_id,))
                else:
                    rows = conn.execute(query + " ORDER BY rowid DESC LIMIT ?",
                                        (session.session_id, max_messages)).fetchall()
                    rows.reverse()
                for role, content in rows:
                    msg = _message(_role(role), content)
                    if msg:
                        transcript.messages.append(msg)
            return transcript
        except sqlite3.Error as exc:
            return _unreadable(session, f"Crush transcript error: {exc}", [db_path])
        finally:
            conn.close()
    return _unreadable(session, "Crush project store not found")


EXPECTED_READER_TOOLS = frozenset(
    {
        "opencode", "claude", "gemini", "codex", "pi", "cursor",
        "freebuff", "droid", "copilot", "continue", "crush",
        "grok", "cline", "forge", "devin", "kilo",
    }
)
