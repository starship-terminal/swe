"""JSONL / project-directory family engine."""

from __future__ import annotations

import json
import os

from swe.sessions import failures, parse_cache
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from swe.sessions.engines.common import (
    clean_title,
    decoded_lines,
    expand_path,
    extract_user_text,
    get_mtime,
    parse_iso_ts,
    read_tail_lines,
)
from swe.sessions.models import Session


@dataclass
class JsonlParserConfig:
    tool_name: str
    agent: str
    base_dir: str
    # Discovery modes:
    # - nested_jsonl: walk project dirs, find *.jsonl
    # - glob: session_glob under base_dir
    # - session_dirs: base/<project>/[chats_subdir/]<session_id>/(jsonl)
    # - index_jsonl: base/<project>/index.jsonl lists sessions
    mode: str = "nested_jsonl"
    session_glob: str = "**/*.jsonl"
    # For index_jsonl mode
    index_basename: str = "index.jsonl"
    # Project dir filter: callable(name, path) -> bool
    project_filter: Callable[[str, str], bool] | None = None
    # Optional intermediate dir under each project (e.g. freebuff "chats")
    chats_subdir: str | None = None
    # If set, only these basenames are treated as session files
    primary_files: set[str] | None = None
    # Skip these basenames
    skip_basenames: set[str] = field(default_factory=set)
    # Path resolution
    path_from_event: Callable[[dict], str] | None = None
    path_from_project_dir: Callable[[str], str] | None = None
    default_path: str | None = None
    require_path: bool = True
    # Title extraction from event stream. May return a plain string or a
    # ``(text, source)`` pair, where source is the ``Session.title_source``
    # label ("rename", "auto"); a plain string means source "".
    title_from_event: Callable[[dict], str | tuple[str, str]] | None = None
    title_max_len: int = 80
    # Late title override, read from the END of the file. Some harnesses
    # append a rename or an auto-generated title after the first prompt; the
    # forward scan stops at its first title hit and would never see it.
    # Returns (priority, text, source) or None, where source is the
    # ``Session.title_source`` label ("rename", "auto"). Highest priority
    # wins; on a tie the line nearest the end wins. Only the last
    # ``tail_bytes`` are read, so a multi-megabyte transcript costs one seek
    # instead of a full pass.
    tail_title_from_event: Callable[[dict], tuple[int, str, str] | None] | None = None
    tail_bytes: int = 65536
    # Session id from path
    session_id_from_path: Callable[[str], str] | None = None
    # Max lines to scan per file for path/title
    path_scan_lines: int = 40
    title_scan_full: bool = True
    # Whether each jsonl is its own session (True) or one session per project dir (False)
    one_session_per_file: bool = True
    # Optional post-process for session_dirs (fields, sess_dir, project_name)
    enrich_session: Callable[[dict, str, str], None] | None = None
    # index_jsonl field extractors
    get_id: Callable[[dict], str] | None = None
    get_path: Callable[[dict], str] | None = None
    # get_title may also return a ``(text, source)`` pair, like title_from_event
    get_title: Callable[[dict], str | tuple[str, str]] | None = None
    get_ts: Callable[[dict], float] | None = None
    # Resolve side session file for title enrichment: (entry, project_dir) -> path
    session_file_from_entry: Callable[[dict, str], str] | None = None
    # Escape hatch for layouts that still need a bespoke walk
    custom: Callable[[], list[Session]] | None = None


def parse_jsonl_projects(config: JsonlParserConfig) -> list[Session]:
    if config.custom:
        try:
            return config.custom()
        except Exception as exc:
            # Still caught: one broken harness must not take down the rest.
            # Recorded so it shows up as an error rather than as no history.
            failures.record(config.tool_name, exc)
            return []

    base = expand_path(config.base_dir)
    if not os.path.exists(base):
        return []

    if config.mode == "glob":
        return _parse_glob(base, config)
    if config.mode == "session_dirs":
        return _parse_session_dirs(base, config)
    if config.mode == "index_jsonl":
        return _parse_index_jsonl(base, config)
    return _parse_nested(base, config)


def _parse_nested(base: str, config: JsonlParserConfig) -> list[Session]:
    sessions: list[Session] = []
    try:
        # ⚡ Bolt: Using os.scandir to reduce stat syscalls
        with os.scandir(base) as name_entry_it:
            for name_entry in name_entry_it:
                if not name_entry.is_dir():
                    continue
                proj = name_entry.path
                name = name_entry.name
                if config.project_filter and not config.project_filter(name, proj):
                    continue
                fallback_path = ""
                if config.path_from_project_dir:
                    try:
                        fallback_path = config.path_from_project_dir(name) or ""
                    except Exception:
                        fallback_path = ""
                jsonl_files = _list_jsonl(proj, config)
                if config.one_session_per_file:
                    for fp in jsonl_files:
                        sess = _session_from_jsonl(fp, config, fallback_path)
                        if sess:
                            sessions.append(sess)
                else:
                    if jsonl_files:
                        latest = max(jsonl_files, key=get_mtime)
                        sess = _session_from_jsonl(latest, config, fallback_path)
                        if sess:
                            sessions.append(sess)
    except Exception as exc:
        # Still caught: one broken harness must not take down the rest.
        # Recorded so it reads as an error rather than as no history.
        failures.record(config.tool_name, exc)
    return sessions


def _parse_glob(base: str, config: JsonlParserConfig) -> list[Session]:
    import glob as _glob

    sessions: list[Session] = []
    pattern = os.path.join(base, config.session_glob)
    try:
        for fp in _glob.glob(pattern, recursive=True):
            if not os.path.isfile(fp):
                continue
            if os.path.basename(fp) in config.skip_basenames:
                continue
            if config.primary_files and os.path.basename(fp) not in config.primary_files:
                continue
            sess = _session_from_jsonl(fp, config, "")
            if sess:
                sessions.append(sess)
    except Exception as exc:
        # Still caught: one broken harness must not take down the rest.
        # Recorded so it reads as an error rather than as no history.
        failures.record(config.tool_name, exc)
    return sessions


def _parse_session_dirs(base: str, config: JsonlParserConfig) -> list[Session]:
    """base/<project>/[chats_subdir/]<session_id>/(primary jsonl)."""
    sessions: list[Session] = []
    try:
        # ⚡ Bolt: Using os.scandir to reduce stat syscalls
        with os.scandir(base) as proj_entry_it:
            for proj_entry in proj_entry_it:
                if not proj_entry.is_dir():
                    continue
                proj = proj_entry.path
                proj_name = proj_entry.name
                if config.project_filter and not config.project_filter(proj_name, proj):
                    continue
                fallback = ""
                if config.path_from_project_dir:
                    try:
                        fallback = config.path_from_project_dir(proj_name) or ""
                    except Exception:
                        fallback = ""
                sessions_root = (
                    os.path.join(proj, config.chats_subdir)
                    if config.chats_subdir
                    else proj
                )
                if not os.path.isdir(sessions_root):
                    continue
                # ⚡ Bolt: Using os.scandir to reduce stat syscalls
                with os.scandir(sessions_root) as sid_entry_it:
                    for sid_entry in sid_entry_it:
                        if not sid_entry.is_dir():
                            continue
                        sess_dir = sid_entry.path
                        sid = sid_entry.name
                        jsonl_files = _list_jsonl(sess_dir, config)
                        if not jsonl_files:
                            continue
                        primary = max(jsonl_files, key=get_mtime)
                        sess = _session_from_jsonl(primary, config, fallback, forced_id=sid)
                        if not sess:
                            continue
                        if config.enrich_session:
                            fields = {
                                "timestamp": sess.timestamp,
                                "agent": sess.agent,
                                "path": sess.path,
                                "title": sess.title,
                                "session_id": sess.session_id,
                                "tool_name": sess.tool_name,
                                "title_source": sess.title_source,
                            }
                            try:
                                config.enrich_session(fields, sess_dir, proj_name)
                            except Exception:
                                pass
                            if config.require_path and not fields.get("path"):
                                continue
                            sess = Session(**fields)
                        sessions.append(sess)
    except Exception as exc:
        # Still caught: one broken harness must not take down the rest.
        # Recorded so it reads as an error rather than as no history.
        failures.record(config.tool_name, exc)
    return sessions


def _parse_index_jsonl(base: str, config: JsonlParserConfig) -> list[Session]:
    """base/<project>/index.jsonl — each line describes a session."""
    sessions: list[Session] = []
    try:
        # ⚡ Bolt: Using os.scandir to reduce stat syscalls
        with os.scandir(base) as proj_entry_it:
            for proj_entry in proj_entry_it:
                if not proj_entry.is_dir():
                    continue
                proj = proj_entry.path
                proj_name = proj_entry.name
                if config.project_filter and not config.project_filter(proj_name, proj):
                    continue
                index_path = os.path.join(proj, config.index_basename)
                if not os.path.isfile(index_path):
                    continue
                fallback = ""
                if config.path_from_project_dir:
                    try:
                        fallback = config.path_from_project_dir(proj_name) or ""
                    except Exception:
                        fallback = ""
                try:
                    with open(index_path, "rb") as f:
                        for line in decoded_lines(f):
                            if not line.strip():
                                continue
                            try:
                                entry = json.loads(line)
                            except Exception:
                                continue
                            if not isinstance(entry, dict):
                                continue
                            sess = _session_from_index_entry(
                                entry, index_path, proj, fallback, config
                            )
                            if sess:
                                sessions.append(sess)
                except Exception:
                    pass
    except Exception as exc:
        # Still caught: one broken harness must not take down the rest.
        # Recorded so it reads as an error rather than as no history.
        failures.record(config.tool_name, exc)
    return sessions


def _session_from_index_entry(
    entry: dict,
    index_path: str,
    proj_dir: str,
    fallback: str,
    config: JsonlParserConfig,
) -> Session | None:
    sid = ""
    if config.get_id:
        try:
            sid = config.get_id(entry) or ""
        except Exception:
            sid = ""
    else:
        sid = str(entry.get("id") or entry.get("session_id") or "")

    path = ""
    if config.get_path:
        try:
            path = config.get_path(entry) or ""
        except Exception:
            path = ""
    else:
        path = entry.get("cwd") or entry.get("path") or entry.get("directory") or ""
        if not isinstance(path, str):
            # Missing, so the fallbacks and ``require_path`` apply; Session
            # would blank it only after the require_path check.
            path = ""

    title = ""
    title_source = ""
    if config.get_title:
        try:
            title, title_source = _split_title(config.get_title(entry))
        except Exception:
            title = ""
    else:
        title = entry.get("title") or ""
    title = clean_title(str(title), config.title_max_len) if title else ""
    if not title:
        title_source = ""

    ts = 0.0
    if config.get_ts:
        try:
            ts = float(config.get_ts(entry) or 0)
        except Exception:
            ts = 0.0
    else:
        ts = (
            parse_iso_ts(entry.get("updated_at"))
            or parse_iso_ts(entry.get("created_at"))
            or parse_iso_ts(entry.get("timestamp"))
        )
    if not ts:
        ts = get_mtime(index_path)

    sess_file = ""
    if config.session_file_from_entry:
        try:
            sess_file = config.session_file_from_entry(entry, proj_dir) or ""
        except Exception:
            sess_file = ""
    elif sid:
        sess_file = os.path.join(proj_dir, f"{sid}.jsonl")

    if sess_file and os.path.exists(sess_file):
        if not title and config.title_from_event:
            title, title_source = _title_from_jsonl(sess_file, config)
        tail_title, tail_source = _title_from_tail(sess_file, config)
        if tail_title:
            title, title_source = tail_title, tail_source
        ts = max(ts, get_mtime(sess_file))

    if not path:
        path = fallback or config.default_path or ""
    if config.require_path and not path:
        return None

    return Session(
        timestamp=ts,
        agent=config.agent,
        path=path,
        title=title,
        session_id=sid,
        tool_name=config.tool_name,
        title_source=title_source,
    )


def _split_title(value: Any) -> tuple[str, str]:
    """Normalise a title callback's return into ``(text, source)``."""
    if isinstance(value, tuple):
        text = value[0] if value else ""
        source = value[1] if len(value) > 1 else ""
        return str(text or ""), str(source or "")
    return str(value or ""), ""


def _title_from_jsonl(fp: str, config: JsonlParserConfig) -> tuple[str, str]:
    """First title in ``fp`` via ``title_from_event``, as ``(text, source)``."""
    if not config.title_from_event:
        return "", ""
    try:
        with open(fp, "rb") as f:
            for line in decoded_lines(f):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                try:
                    raw, source = _split_title(config.title_from_event(data))
                except Exception:
                    raw, source = "", ""
                if raw:
                    return clean_title(raw, config.title_max_len), source
    except Exception:
        pass
    return "", ""


def _title_from_tail(fp: str, config: JsonlParserConfig) -> tuple[str, str]:
    """Best late title in the last ``config.tail_bytes`` of ``fp``.

    Returns ``(title, source)``, both empty when nothing was found. Lines are
    walked from the end; a lower-priority hit is kept only until something
    better turns up, so the latest line at the highest priority wins.
    """
    if not config.tail_title_from_event:
        return "", ""
    try:
        lines, _ = read_tail_lines(fp, config.tail_bytes)
    except OSError:
        parse_cache.note_read_error()
        return "", ""
    except Exception:
        return "", ""
    best_priority: int | None = None
    best_text = ""
    best_source = ""
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        try:
            hit = config.tail_title_from_event(data)
        except Exception:
            hit = None
        if not hit:
            continue
        priority, raw, source = hit
        if not raw or (best_priority is not None and priority <= best_priority):
            continue
        best_priority = priority
        best_text = raw
        best_source = str(source or "")
    if not best_text:
        return "", ""
    return clean_title(best_text, config.title_max_len), best_source


def _list_jsonl(directory: str, config: JsonlParserConfig) -> list[str]:
    out: list[str] = []
    try:
        # ⚡ Bolt: Using os.scandir to reduce stat syscalls
        with os.scandir(directory) as name_entry_it:
            for name_entry in name_entry_it:
                name = name_entry.name
                if not name.endswith(".jsonl"):
                    continue
                if name in config.skip_basenames:
                    continue
                if config.primary_files and name not in config.primary_files:
                    continue
                if name == config.index_basename and config.mode == "index_jsonl":
                    continue
                out.append(name_entry.path)
    except Exception:
        pass
    return out


def _session_from_jsonl(
    fp: str,
    config: JsonlParserConfig,
    fallback_path: str,
    forced_id: str | None = None,
) -> Session | None:
    try:
        st = os.stat(fp)
    except OSError:
        return None
    mtime = st.st_mtime * 1000
    if mtime == 0:
        return None

    if forced_id is not None:
        sid = forced_id
    elif config.session_id_from_path:
        sid = config.session_id_from_path(fp)
    else:
        sid = os.path.basename(fp).removesuffix(".jsonl")

    # What the file itself says is cached per file; the fallbacks below
    # depend on the caller and run every time.
    path, title, title_source = parse_cache.lookup(
        f"{config.tool_name}:jsonl",
        fp,
        lambda: _scan_jsonl(fp, config),
        st=st,
        valid=_is_scan,
    )

    if not path:
        path = fallback_path or config.default_path or ""
    if config.require_path and not path:
        return None

    return Session(
        timestamp=mtime,
        agent=config.agent,
        path=path,
        title=title,
        session_id=sid,
        tool_name=config.tool_name,
        title_source=title_source,
    )


def _is_scan(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and isinstance(value[1], str)
        and isinstance(value[2], str)
    )


def _scan_jsonl(fp: str, config: JsonlParserConfig) -> list:
    """``[path, title, title_source]`` read from ``fp`` alone."""
    path = ""
    title = ""
    title_source = ""
    # With neither callback there is nothing a line could contribute, so
    # the file is not read at all (freebuff's log.jsonl was parsed in full
    # for nothing).
    if config.path_from_event or config.title_from_event:
        try:
            from swe.sessions import zstd_copy
            import io
            plain = zstd_copy.plain(fp)
            handle = io.BytesIO(plain) if plain is not None else open(fp, "rb")
            with handle as f:
                for i, line in enumerate(decoded_lines(f)):
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if not path and config.path_from_event:
                        try:
                            path = config.path_from_event(data) or ""
                        except Exception:
                            path = ""
                    if not title and config.title_from_event:
                        try:
                            raw, source = _split_title(config.title_from_event(data))
                            if raw:
                                title = clean_title(raw, config.title_max_len)
                                title_source = source
                        except Exception:
                            pass
                    if path and title:
                        break
                    if not config.title_scan_full and i >= config.path_scan_lines:
                        break
                    if path and not config.title_from_event and i >= config.path_scan_lines:
                        break
        except OSError:
            parse_cache.note_read_error()
        except Exception:
            pass

    tail_title, tail_source = _title_from_tail(fp, config)
    if tail_title:
        title, title_source = tail_title, tail_source
    return [path, title, title_source]


# --- common event helpers used by adapters ---

def event_cwd(data: dict, *keys: str) -> str:
    for key in keys or ("cwd", "working_directory", "directory"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
        payload = data.get("payload")
        if isinstance(payload, dict):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def first_user_title(
    data: dict,
    *,
    type_field: str = "type",
    type_values: Iterable[str] = ("user", "message"),
    role_path: tuple[str, ...] = ("message", "role"),
    content_path: tuple[str, ...] = ("message", "content"),
    role_value: str = "user",
) -> str:
    """Generic title extractor; adapters can wrap with tool-specific checks."""
    t = data.get(type_field)

    # role at top level
    if data.get("role") == role_value:
        return extract_user_text(data.get("content"))

    # nested message
    msg = data
    for key in role_path[:-1] if role_path else ():
        msg = msg.get(key) if isinstance(msg, dict) else None
        if not isinstance(msg, dict):
            return ""
    if role_path:
        role = msg.get(role_path[-1]) if isinstance(msg, dict) else None
        if role != role_value:
            # type-only match (e.g. type==user)
            if t not in type_values:
                return ""
        content = data
        for key in content_path:
            if not isinstance(content, dict):
                return ""
            content = content.get(key)
        return extract_user_text(content)

    if t in type_values:
        return extract_user_text(data.get("message") or data.get("content") or "")
    return ""
