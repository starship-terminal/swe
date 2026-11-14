"""Reading every harness log on this machine into the store
(ports the source walk in ``Ledger.swift``).

Claude transcripts only grow, so each is read on from where the last scan
stopped. A Codex rollout carries state from its first line (model, replay
gate, the totals it has seen), so a changed rollout is read whole; its
keys make that idempotent. Pi logs and the Droid and Cline session files
are read whole when they change, also keyed — the session files hold one
authoritative total each, so a re-read replaces and a now-empty file
deletes. OpenCode and Devin databases are skipped while they and their
WALs are unchanged, and otherwise read only past a watermark. Cursor's
usage is not a file: the web app polls it in the background on the
feed's own cadence (``CursorUsageFeed.keepFresh``), so a scan never
waits on network replies and a missed round keeps the stored rows.

Claude Code and Codex prune their transcripts; swe's mirror keeps a copy
of each (``~/.swe/data/transcripts``, #1918). Events already recorded
outlive the transcript, and :func:`_scan_copies` reads a copy for the
usage of a session the ledger never read, or read only partly, before it
went.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from swe.ledger import parsers
from swe.ledger.models import LedgerRecord
from swe.ledger.store import LedgerStore
from swe.paths import swe_dir_for, transcripts_dir_for


@dataclass
class ScanResult:
    files_read: int = 0
    records: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


def _homes(home: Path, vendor: str, default: str) -> list[Path]:
    """The vendor's own home, then each extra SWE account profile's
    (``~/.swe/accounts/<vendor>/<id>/home``)."""
    out = [home / default]
    out += sorted((swe_dir_for(home) / "accounts" / vendor).glob("*/home"))
    return [p for p in out if p.is_dir()]


def _files(root: Path, suffix: str) -> list[Path]:
    """Every file under ``root`` whose name ends in ``suffix``, skipping
    hidden entries, by path."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        found += [Path(dirpath) / f for f in filenames
                  if f.endswith(suffix) and not f.startswith(".")]
    return sorted(found)


def _jsonl_files(root: Path) -> list[Path]:
    """Every ``*.jsonl`` under ``root``, skipping hidden entries, by path."""
    return _files(root, ".jsonl")


def _stat(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def scan(store: LedgerStore, home: Path | None = None) -> ScanResult:
    home = Path(home) if home is not None else Path.home()
    result = ScanResult()
    started = time.monotonic()
    seen: set[str] = set()

    # Every live Claude and Codex session this scan enumerates, and each
    # walked folder's resolved and walked spellings, for the mirror copies.
    live: set[tuple[str, str]] = set()
    spellings: list[tuple[str, str]] = []

    for claude_home in _homes(home, "claude", ".claude"):
        spellings.append(_spelling(claude_home / "projects"))
        for path in _jsonl_files(claude_home / "projects"):
            real = os.path.realpath(path)
            live.add(_live_identity("claude", path, real))
            if real in seen:
                continue
            seen.add(real)
            _scan_claude_file(store, path, str(claude_home), result)

    for codex_home in _homes(home, "codex", ".codex"):
        for sub in ("sessions", "archived_sessions"):
            spellings.append(_spelling(codex_home / sub))
            for path in _jsonl_files(codex_home / sub):
                real = os.path.realpath(path)
                live.add(_live_identity("codex", path, real))
                if real in seen:
                    continue
                seen.add(real)
                _scan_whole(store, path, result,
                            lambda p=path, a=str(codex_home): parsers.codex_file_records(p, a))

    # Every scan here enumerates every live file (the Python ledger has no
    # capped scan), so a copy never stands for a session a list left out.
    _scan_copies(store, home, live, spellings, seen, result)

    for pi_home in _homes(home, "pi", ".pi"):
        for path in _jsonl_files(pi_home / "agent" / "sessions"):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            # Read whole like a Codex rollout: the cwd a `session` line
            # sets must reach the message lines after it, and the keys
            # make the re-read idempotent.
            _scan_whole(store, path, result,
                        lambda p=path, a=str(pi_home): parsers.pi_file_records(p, a))

    # The opencode cursor's key names its reader: one saved by a build that
    # read only the legacy tables (no suffix) must not skip the 2.x rows it
    # missed, and one saved before models carried their provider (``#v2``)
    # must not skip rows now named ``provider/model``. Every row is keyed
    # by message id, so the re-read renames events in place. ``@w`` names
    # a cursor whose offset is a watermark: one saved by a whole-file
    # reader holds the file's size there instead.
    for db, reader, key in (
        (home / ".local/share/opencode/opencode.db", parsers.opencode_scan, "#v3@w"),
        (home / ".local/share/devin/cli/sessions.db", parsers.devin_scan, "@w"),
    ):
        if db.is_file():
            _scan_database(store, db, result, reader, key_suffix=key)

    for droid_home in _homes(home, "droid", ".factory"):
        for path in _files(droid_home / "sessions", ".settings.json"):
            _scan_session_file(store, path, str(droid_home), result,
                               parsers.droid_session_totals,
                               watched=parsers.droid_transcript(path))

    for cline_home in _homes(home, "cline", ".cline"):
        for path in _files(cline_home / "data" / "sessions", ".json"):
            # ``<id>/<id>.json`` only: the stem must equal its session
            # directory, which the sibling ``.messages.json`` fails — it
            # is the transcript, not the file totals are read from.
            if path.stem != path.parent.name:
                continue
            _scan_session_file(store, path, str(cline_home), result,
                               parsers.cline_session_totals)

    store.set_meta("scanned_at", str(time.time()))
    store.db.commit()
    result.seconds = time.monotonic() - started
    return result


def _scan_claude_file(store: LedgerStore, path: Path, account: str,
                      result: ScanResult) -> None:
    st = _stat(path)
    if st is None:
        return
    inode, size, mtime = st
    key = str(path)
    cursor = store.file_cursor(key)
    offset = 0
    try:
        if cursor is not None:
            c_inode, _c_size, _c_mtime, c_offset, c_fp, _c_state = cursor
            if tuple(cursor[:3]) == (inode, size, mtime):
                return
            # Appended to: read on. Replaced, truncated or rewritten in
            # place (the bytes already read no longer match): from the start.
            c_offset = c_offset or 0
            if (c_inode == inode and size >= c_offset
                    and c_fp == _fingerprint(path, c_offset)):
                offset = c_offset
        end: list[int] = []
        for line in parsers.iter_lines(path, offset, end):
            for rec in parsers.claude_records(line, account):
                store.upsert(rec)
                result.records += 1
        done = end[0] if end else offset
        fp = _fingerprint(path, done)
    except OSError as exc:
        result.errors.append(f"{path}: {exc}")
        return
    store.set_file_cursor(key, inode, size, mtime, done, fp)
    result.files_read += 1
    store.db.commit()


_FP_SPAN = 4096


def _fingerprint(path: Path, offset: int) -> str:
    """A hash of the head of the file and of the bytes just before
    ``offset``, the native ledger's check that what was read is still
    there before reading on from it."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(min(_FP_SPAN, offset)))
        f.seek(max(0, offset - _FP_SPAN))
        h.update(f.read(min(_FP_SPAN, offset)))
    return f"{offset}:{h.hexdigest()}"


# -- mirror copies of pruned transcripts (#1918) -------------------------


def _spelling(folder: Path) -> tuple[str, str]:
    """``(resolved, walked)`` spellings of a folder the scan walks. Live
    cursors are keyed by the walked spelling; the mirror's manifest keeps
    resolved paths."""
    return os.path.realpath(folder), str(folder)


def _live_identity(harness: str, path: Path, real: str) -> tuple[str, str]:
    """``(harness, identity)`` of a live transcript (``liveIdentities``):
    a uuid-named file needs only its name; any other name is its own
    identity, its resolved path."""
    from swe.sessions.search_writer import transcript_identity

    identity = transcript_identity(harness, str(path))
    return harness, (real if identity == str(path) else identity)


def _cursor_keys(source: str, spellings: list[tuple[str, str]]) -> list[str]:
    """Every key a live scan may have saved ``source``'s cursor under: the
    path as the manifest has it (resolved), then as each walked folder
    spells it."""
    keys = [source]
    for real, walked in spellings:
        if source.startswith(real + os.sep):
            key = walked + source[len(real):]
            if key not in keys:
                keys.append(key)
    return keys


def _source_cursor(store: LedgerStore, source: str,
                   spellings: list[tuple[str, str]]) -> tuple | None:
    for key in _cursor_keys(source, spellings):
        cursor = store.file_cursor(key)
        if cursor is not None:
            return cursor
    return None


def _copy_source(rel: str, harness: str, manifest: dict, table, known) -> str | None:
    """The source path the copy at ``rel`` stands for (``sourceKey``): the
    manifest's record, else the layout's answer, preferring a candidate
    ``known`` says the ledger holds a cursor for. None when neither says."""
    from swe.sessions import transcript_mirror as mirror

    entry = manifest.get(rel)
    if isinstance(entry, dict) and entry.get("harness") == harness and isinstance(
            entry.get("source"), str) and entry["source"]:
        return entry["source"]
    candidates = mirror.source_candidates(rel, table)
    if len(candidates) > 1:
        for candidate in candidates:
            if known(candidate):
                return candidate
    return candidates[0] if candidates else None


def _copy_home(rel: str, harness: str, table) -> str | None:
    """The harness home whose tree holds the copy at ``rel``, by the
    longest prefix (``TranscriptMirror.home(for:harness:)``): events are
    filed under it, as a live scan files the source's."""
    homes = [r for r in table if r.harness == harness and rel.startswith(r.prefix + "/")]
    if not homes:
        return None
    return max(homes, key=lambda r: len(r.prefix)).home


def _covers(cursor: tuple | None, st: os.stat_result, copy: str) -> bool:
    """Whether the source's saved cursor proves the ledger already read
    exactly the copy's bytes (``Ledger.cursor(_:covers:copy:)``): the copy
    keeps its source's size and mtime, the cursor read to that size, and
    its samples match the copy. Never a bare offset comparison, which a
    shorter rewrite with new usage would pass.

    Swift samples head, middle and tail; this ledger's ``fp`` samples only
    head and tail (:func:`_fingerprint`). Swift trusts a cursor saved
    without a middle sample only where head and tail cover the file end to
    end, and so does this: a longer file is read, which costs one read
    and loses nothing, since event keys drop what the ledger holds. A
    Codex cursor keeps no sample at all, so its copy is always read once.
    """
    if cursor is None:
        return False
    _inode, size, mtime_ns, offset, fp, _state = cursor
    if not (size == offset == st.st_size and mtime_ns == st.st_mtime_ns):
        return False
    if not fp or offset > 2 * _FP_SPAN:
        return False
    try:
        return _fingerprint(Path(copy), offset) == fp
    except OSError:
        return False  # the read that follows notes it


def _scan_copies(store: LedgerStore, home: Path, live: set[tuple[str, str]],
                 spellings: list[tuple[str, str]], seen: set[str],
                 result: ScanResult) -> None:
    """Usage from the copies of sessions the harnesses pruned, for events
    the ledger never recorded (``Ledger.scanCopies``): a session that ran
    and was pruned between two scans, or before this ledger existed, or
    one rewritten after the ledger last read it.

    Never counted twice. A copy is read only when no live file this scan
    enumerated carries its session (``transcript_identity``, from the
    file name) and its source path is gone; a copy the source's cursor
    covers (:func:`_covers`) is not read at all. Otherwise it is read
    under the copy's own cursor, so once, and events keep their content
    keys (``message.id`` + ``requestId``, Codex's response id or rollout
    and timestamp), so a line the ledger already holds upserts onto its
    own row with nothing to add. Events are filed under the copy's
    harness home. A linked tree inside the mirror reads as absent, and a
    copy that cannot be read is a scan note, retried next scan. Nothing is
    ever deleted here."""
    from swe.sessions import search_writer
    from swe.sessions import transcript_mirror as mirror

    root = transcripts_dir_for(home)
    if not root.is_dir():
        return
    real_root = os.path.realpath(root)
    manifest = mirror.load_manifest(home)
    table = mirror.roots(home)

    def known(path: str) -> bool:
        return _source_cursor(store, path, spellings) is not None

    # The walk search's copy pass makes (``_copy_trees``, ``_walk``): each
    # harness's tree then each account's, linked trees left out, hidden
    # names pruned, linked files resolved. Unlike search it keeps
    # ``subagents/``: a Claude subagent transcript's usage is counted, under
    # its parent session (every line carries the parent's ``sessionId``),
    # while it is live, so its copy is read under the same rule. A subagent
    # file's name holds no session uuid, so its identity is its own path.
    for harness, tree in search_writer._copy_trees(real_root):
        for path in search_writer._walk(Path(tree), subagents=True):
            # A copy keeps its source's file name, so a session id in it
            # settles liveness before any lookup.
            if (not path.startswith(real_root + os.sep)
                    or (harness, search_writer.transcript_identity(harness, path)) in live):
                continue
            rel = path[len(real_root) + 1:]
            source = _copy_source(rel, harness, manifest, table, known)
            if (source is None or source.startswith(real_root + os.sep)
                    or os.path.lexists(source)):
                continue  # nowhere known to file it, or its source is on disk
            if (harness, search_writer.transcript_identity(harness, source)) in live:
                continue
            account = _copy_home(rel, harness, table)
            if account is None or path in seen:
                continue
            seen.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue  # gone since the walk
            if _covers(_source_cursor(store, source, spellings), st, path):
                continue
            copy = Path(path)
            if harness == "claude":
                _scan_claude_file(store, copy, account, result)
            else:
                _scan_whole(store, copy, result,
                            lambda p=copy, a=account: parsers.codex_file_records(p, a))


def _scan_whole(store: LedgerStore, path: Path, result: ScanResult, read) -> None:
    st = _stat(path)
    if st is None:
        return
    inode, size, mtime = st
    key = str(path)
    cursor = store.file_cursor(key)
    if cursor is not None and tuple(cursor[:3]) == (inode, size, mtime):
        return
    try:
        records = read()
    except Exception as exc:  # a corrupt or locked source must not stop the scan
        result.errors.append(f"{path}: {exc}")
        return
    for rec in records:
        store.upsert(rec)
    result.records += len(records)
    store.set_file_cursor(key, inode, size, mtime, size)
    result.files_read += 1
    store.db.commit()


def _scan_database(store: LedgerStore, path: Path, result: ScanResult, read,
                   key_suffix: str) -> None:
    """One harness database, read past its watermark (the source pass in
    ``DatabaseSources.swift``).

    Skipped while the file and its WAL are unchanged: the cursor's ``fp``
    slot keeps the stamp of both (``Fingerprint``), since a WAL grows
    without touching the main file. Otherwise ``read(path, since)``
    returns the rows past the watermark the cursor's ``offset`` slot keeps
    and the next one. A different inode is a recreated or restored file
    whose row ids and timestamps start over, so it is read from 0; every
    row is keyed, so reading one again is harmless."""
    st = _stat(path)
    if st is None:
        return
    inode, size, mtime = st
    wal = _stat(Path(str(path) + "-wal"))
    fingerprint = f"{inode}:{size}:{mtime}:{wal[1] if wal else 0}:{wal[2] if wal else 0}"
    key = str(path) + key_suffix
    cursor = store.file_cursor(key)
    if cursor is not None and cursor[4] == fingerprint:
        return
    since = (cursor[3] or 0) if cursor is not None and cursor[0] == inode else 0
    try:
        records, watermark = read(path, since)
    except Exception as exc:  # a corrupt or locked source must not stop the scan
        result.errors.append(f"{path}: {exc}")
        return
    for rec in records:
        store.upsert(rec)
    result.records += len(records)
    store.set_file_cursor(key, inode, size, mtime, watermark, fp=fingerprint)
    result.files_read += 1
    store.db.commit()


def _stamp(path: Path) -> list | None:
    """``WatchedStamp``: a sibling's ``(inode, size, mtime, ctime)``, or
    ``None`` when it is absent — its absence is part of the stamp, so the
    file appearing or vanishing re-reads its session file too."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _scan_session_file(store: LedgerStore, path: Path, account: str,
                       result: ScanResult, parse,
                       watched: Path | None = None) -> None:
    """One session-total file, re-read whole whenever it could have
    changed (``scanSessionFile``).

    The cursor's ``state`` slot keeps the file's own ctime — inode, size
    and mtime can all survive an in-place rewrite, ctime cannot — and the
    watched sibling's stamp, so a transcript that moved re-reads the pair.
    A record replaces whatever the file said before (the key is
    authoritative); a file that now reports no usage deletes its old
    event. The cursor is saved only after a clean parse, so a corrupt
    file is retried, never skipped."""
    try:
        st = path.stat()
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        return
    key = str(path)
    state = json.dumps({"changed": st.st_ctime_ns,
                        "watched": _stamp(watched) if watched is not None else None})
    cursor = store.file_cursor(key)
    if (cursor is not None
            and tuple(cursor[:3]) == (st.st_ino, st.st_size, st.st_mtime_ns)
            and cursor[5] == state):
        return
    try:
        totals = parse(path, account)
    except Exception as exc:  # a corrupt file must not stop the scan
        result.errors.append(f"{path}: {exc}")
        return
    if isinstance(totals, LedgerRecord):
        store.upsert(totals)
        result.records += 1
    elif isinstance(totals, parsers.Empty):
        store.delete(totals.key)
    store.set_file_cursor(key, st.st_ino, st.st_size, st.st_mtime_ns,
                          st.st_size, state=state)
    result.files_read += 1
    store.db.commit()
