"""Shared helpers for session family engines."""

from __future__ import annotations

import math
import os
import re
import sqlite3
from datetime import datetime
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import quote, unquote, urlparse


def get_mtime(path: str) -> float:
    """File mtime as epoch milliseconds."""
    try:
        return os.path.getmtime(path) * 1000
    except OSError:
        return 0.0


def parse_iso_ts(value: Any) -> float:
    """Parse ISO/datetime-ish values to epoch-ms. Returns 0 on failure."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        # NaN/±Inf pass every magnitude comparison below and then kill the
        # listing (int(nan) raises in relative_time, json writes them back
        # into the session cache). Reject non-finite up front.
        if not math.isfinite(v):
            return 0.0
        if v > 1e14:  # ns-ish
            return v / 1e6
        if v > 1e12:  # already ms
            return v
        if v > 1e9:  # seconds
            return v * 1000
        return v
    text = str(value).strip()
    if not text:
        return 0.0
    if text.isdigit() or (text.replace(".", "", 1).isdigit() and text.count(".") < 2):
        try:
            return parse_iso_ts(float(text))
        except ValueError:
            pass
    try:
        cleaned = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.timestamp() * 1000
    except Exception:
        return 0.0


def clean_title(text: str, max_len: int = 80) -> str:
    text = re.sub(r"<[^>]+>", "", text or "").strip()
    text = " ".join(text.split())
    if not text:
        return ""
    return text[:max_len] + ("..." if len(text) > max_len else "")


def extract_user_text(content: Any) -> str:
    """Pull plain text from str | list[blocks] | nested message shapes."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                return block
            if isinstance(block, dict):
                if block.get("type") in ("text", "input_text") and block.get("text"):
                    return str(block.get("text") or "")
                if block.get("text"):
                    return str(block.get("text") or "")
    if isinstance(content, dict):
        if content.get("text"):
            return str(content.get("text") or "")
        if "content" in content:
            return extract_user_text(content.get("content"))
    return ""


def expand_path(path: str) -> str:
    """Expand ~ only; leave absolute/relative paths untouched (mock-safe)."""
    if not path:
        return path
    if path == "~" or path.startswith("~/") or path.startswith("~\\"):
        return os.path.expanduser(path)
    return path


def decoded_lines(raw_lines: Iterable[bytes]) -> Iterator[str]:
    """Each line of a binary file object as text, bad UTF-8 replaced.

    A file opened in text mode raises ``UnicodeDecodeError`` mid-iteration
    on one invalid byte, losing that line and every one after it. Decoding
    line by line confines the damage to the line that holds the byte.
    """
    for raw in raw_lines:
        yield raw.decode("utf-8", errors="replace")


def read_tail_lines(path: str, tail_bytes: int) -> tuple[list[str], bool]:
    """The whole lines in the last ``tail_bytes`` of ``path``, and whether
    that window covers the file.

    A window that starts mid-file opens inside a record, so its first line
    is dropped as a partial. The read starts one byte before the window:
    when that byte is the newline ending the previous record, the dropped
    "partial" is empty and the window's first record is kept. ``OSError``
    propagates.
    """
    from swe.sessions import zstd_copy
    plain = zstd_copy.plain(path)
    if plain is not None:
        import io
        size = len(plain)
        start = max(0, size - tail_bytes)
        fh = io.BytesIO(plain)
        fh.seek(start - 1 if start > 0 else 0)
        chunk = fh.read().decode("utf-8", errors="ignore")
        lines = chunk.split("\n")
        if start > 0:
            lines = lines[1:]
        return lines, start == 0
    size = os.path.getsize(path)
    start = max(0, size - tail_bytes)
    with open(path, "rb") as fh:
        fh.seek(start - 1 if start > 0 else 0)
        chunk = fh.read().decode("utf-8", errors="ignore")
    lines = chunk.split("\n")
    if start > 0:
        lines = lines[1:]
    return lines, start == 0


def sqlite_uri(path, mode: str = "ro") -> str:
    """A ``file:`` URI for ``path`` that opens exactly that file.

    The path is percent-quoted: a bare ``#`` starts the URI fragment and
    ``?`` the query, so ``file:/x/C#proj/crush.db?mode=ro`` opened ``/x/C``
    read-write, creating an empty database there, and dropped ``mode=ro``
    for a store that belongs to another app.
    """
    return f"file:{quote(os.fspath(path))}?mode={mode}"


def open_sqlite_ro(path: str) -> sqlite3.Connection | None:
    expanded = expand_path(path)
    if not os.path.exists(expanded):
        return None
    try:
        return sqlite3.connect(sqlite_uri(expanded), uri=True)
    except Exception:
        return None


def path_from_encoded_dir(encoded_dir: str, strip_prefix: str = "-") -> str:
    """Decode -Users-foo-bar style directory names to absolute paths."""
    inner = encoded_dir
    if strip_prefix and inner.startswith(strip_prefix):
        inner = inner[len(strip_prefix) :]
    if not inner:
        return ""
    return "/" + inner.replace("-", "/")


def strip_file_uri(uri: str) -> str:
    if not isinstance(uri, str):
        return ""
    if uri.startswith("file://"):
        return unquote(urlparse(uri).path)
    return uri


def dig(obj: Any, *keys: str, default: Any = None) -> Any:
    """Safe nested dict get: dig(d, 'a', 'b')."""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, default)
        if cur is default:
            return default
    return cur
