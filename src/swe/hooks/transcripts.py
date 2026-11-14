"""Hook runs recovered from Claude Code transcripts.

Claude Code writes every hook run into the session transcript, so history
from before the event log existed, and every Claude run since, can be
counted without a helper. What a transcript holds:

* ``attachment`` records of type ``hook_success`` (exit 0),
  ``hook_non_blocking_error`` (any other non-2 exit) and ``hook_cancelled``,
  each with ``hookName`` (``PreToolUse:Bash``), ``command``, ``exitCode``,
  ``durationMs``, ``stderr`` and ``toolUseID``;
* ``system`` records of subtype ``stop_hook_summary``, one per Stop that ran
  any hook, listing ``hookInfos`` (``command``, ``durationMs``) and
  ``hookErrors``;
* a PreToolUse block (exit 2) as a tool result reading
  ``PreToolUse:<Tool> hook error: [<command>]: <stderr>``;
* a Stop block as a meta user message
  ``Stop hook feedback:\\n[<command>]: <stderr>``.

Every record carries ``sessionId``, ``cwd``, ``gitBranch`` and
``timestamp``. This module turns them into the event dicts of
``hooks/events.py``, with ``source: "transcript"`` and the ``script`` the
command ran, resolved by ``hooks/identity.py`` the way ``swe hooks list``
reads a declared command (None when the path holds a variable swe cannot
know). A generated wrapper's run is named after its shared script, as the
hook log names it. It reads only: nothing
is written, and only the first line of a hook's message is kept.

The transcript list comes from ``sessions.search_writer.sources``, the same
enumeration the search index uses, so there is no second directory walk.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from functools import lru_cache
from datetime import datetime
from pathlib import Path

from swe.hooks import shim_runtime
from swe.hooks.identity import Resolver
from swe.hooks.events import SCRIPT_EXTENSIONS, hook_stem

# A cheap byte test before json.loads: every record this module reads
# carries one of these. Most transcript lines carry none, and a line that
# only talks about hooks (an assistant message, say) does not match.
_NEEDLES = (b'"hook_success"', b'"hook_non_blocking_error"',
            b'"hook_cancelled"', b'"hook_blocking_error"',
            b"stop_hook_summary", b" hook error: [", b" hook error from",
            b"hook feedback:")

# A feedback block or a summary that follows a block is the same run when
# it lands this close (seconds).
_STOP_WINDOW = 15.0

# ``PreToolUse:Bash hook error: [cmd]: stderr`` and the wordings Claude Code
# has used for it. The command sits in brackets; stderr is the rest.
_TOOL_BLOCK = re.compile(
    r"^(?P<event>[A-Za-z]+):(?P<tool>\S+) hook "
    r"(?:blocking )?error(?: from command)?:\s*"
    r"\[(?P<command>.*?)\]:\s*(?P<message>.*)\Z", re.DOTALL)
_FEEDBACK = re.compile(r"^(?P<event>[A-Za-z]+) hook feedback:\s*\n?(?P<body>.*)\Z",
                       re.DOTALL)
_FEEDBACK_ENTRY = re.compile(r"^\[(?P<command>.*?)\]:\s*", re.MULTILINE)


# Where a path ends inside a shell command: whitespace, quotes and the
# operators that glue commands together. ``=`` too, so ``f=/x/y.py`` and
# ``PATH=...`` split before the path. Splitting is linear; a path regex
# with a greedy class backtracked for milliseconds per long command.
_PATH_BREAKS = re.compile(r"[\s\"'`;|&()<>=]+")

# Stems that say nothing about which hook it is: the parent folder joins
# them (``.pixel-agents/claude-hook``).
_GENERIC_STEMS = frozenset({
    "hook", "hooks", "run", "main", "index", "handler", "entry", "callback",
    "notify", "claude-hook", "on-stop", "on-start", "stop", "start",
    "session-start", "session-end", "pre-tool-use", "post-tool-use",
    "user-prompt-submit"})
# Folders that only say "this is a hook script".
_PLAIN_FOLDERS = frozenset({"hooks", "scripts", "bin", "_generated", "src",
                            "lib", "dist", "."})
_INTERPRETERS = frozenset({"python", "python3", "node", "bash", "sh", "zsh",
                           "env", "exec", "ruby", "perl", "bun", "deno"})


def _variable_label(folder: str) -> str:
    """``superset`` for ``$SUPERSET_HOME_DIR``, ``claude-plugin`` for
    ``${CLAUDE_PLUGIN_ROOT}``."""
    name = folder.strip("${}\"'").lower()
    for suffix in ("_home_dir", "_root", "_dir", "_home"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[:-len(suffix)]
            break
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")


@lru_cache(maxsize=1024)
def command_hook(command: str) -> str:
    """The hook's name for a declared command line.

    The name comes from the script path inside the command: the last
    path-like token ending in a script extension, its basename without the
    extension. So these all work::

        python3 "/h/.claude/hooks/prose-guard.py"           -> prose-guard
        f="$HOME/.swe/hooks/x/command_guard.py"; ... "$f"   -> command_guard
        "${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh"      -> session-start
        PATH="..." node "/h/.pixel-agents/hooks/claude-hook.js"
                                                   -> .pixel-agents/claude-hook

    That is also how the hook shim names a shared hook (the stem of its
    script), so a run in the log and the same run in a transcript group
    together. A generic stem (``callback``, ``notify``) takes its nearest
    meaningful folder as a prefix. A command with no script path falls back
    to its first word that is not a ``VAR=value`` or an interpreter.
    """
    command = command or ""
    paths = [t for t in _PATH_BREAKS.split(command)
             if t.lower().endswith(SCRIPT_EXTENSIONS)
             and len(t) > 3]
    if paths:
        path = paths[-1]
        stem = hook_stem(os.path.basename(path))
        if stem.lower() in _GENERIC_STEMS:
            for folder in reversed(path.split("/")[:-1]):
                if not folder or folder.lower() in _PLAIN_FOLDERS:
                    continue
                if folder == "~":
                    break
                if "$" in folder:
                    label = _variable_label(folder)
                    return f"{label}/{stem}" if label else stem
                return f"{folder}/{stem}"
        return stem
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    words = [t for t in tokens if not re.match(r"^[A-Za-z_]\w*=", t)]
    if len(words) > 6 and "/" not in command:
        # A prompt hook stores its prompt where a command would go.
        return "prompt"
    for word in words:
        base = hook_stem(word)
        if base.lower() not in _INTERPRETERS:
            return base
    return hook_stem(words[0]) if words else ""


def _epoch(stamp) -> float | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def _int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _event(record: dict, ts: float, command: str, name: str,
           decision: str, reason: str, duration, exit_code,
           tool_use_id) -> dict:
    """One run as the file's bytes say it. ``hook`` and ``script`` are
    filled by ``finish`` from ``_command``, since what a command runs is
    the disk's to say, not the transcript's (and so is never cached)."""
    return {
        "ts": ts,
        "hook": None,
        "harness": "claude",
        "event": name,
        "session_id": record.get("sessionId")
        if isinstance(record.get("sessionId"), str) else None,
        "cwd": record.get("cwd") if isinstance(record.get("cwd"), str) else None,
        "repo": None,
        "git_branch": record.get("gitBranch")
        if isinstance(record.get("gitBranch"), str) and record.get("gitBranch")
        else None,
        "decision": decision,
        "reason": shim_runtime.reason_line(reason),
        "duration_ms": _int(duration),
        "exit_code": _int(exit_code),
        "stop_hook_active": None,
        "source": "transcript",
        "script": None,
        "_command": command,
        # Used by the in-file merge below and dropped before returning.
        "_tool_use_id": tool_use_id if isinstance(tool_use_id, str) else None,
        "_kind": "",
    }


def _text_of(content) -> str:
    """A message's or tool result's text, whatever shape it came in."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
            if isinstance(item, str)
            or (isinstance(item, dict) and item.get("type") == "text"))
    return ""


def _attachment(record: dict, ts: float) -> list[dict]:
    att = record.get("attachment")
    if not isinstance(att, dict):
        return []
    kind = att.get("type")
    if kind not in ("hook_success", "hook_non_blocking_error",
                    "hook_cancelled", "hook_blocking_error"):
        return []
    command = att.get("command")
    if not isinstance(command, str) and isinstance(att.get("blockingError"),
                                                   dict):
        command = att["blockingError"].get("command")
    command = command or ""
    name = att.get("hookName") or att.get("hookEvent")
    if not command_hook(command) or not isinstance(name, str):
        return []
    stderr = att.get("stderr")
    if not isinstance(stderr, str) or not stderr.strip():
        stderr = ""
    if kind == "hook_success":
        decision, reason = ("warn", stderr) if stderr else ("allow", "")
    elif kind == "hook_cancelled":
        decision = "error"
        reason = "timed out" if att.get("timedOut") else "cancelled"
    elif kind == "hook_blocking_error":
        blocking = att.get("blockingError")
        message = blocking.get("blockingError") if isinstance(
            blocking, dict) else ""
        decision, reason = "block", stderr or (message or "")
    else:
        stdout = att.get("stdout") if isinstance(att.get("stdout"), str) else ""
        decision = "error"
        reason = stderr or stdout or f"exit {att.get('exitCode')}"
    event = _event(record, ts, command, name, decision, reason,
                   att.get("durationMs"), att.get("exitCode"),
                   att.get("toolUseID"))
    event["_kind"] = "attachment"
    return [event]


def _stop_summary(record: dict, ts: float) -> list[dict]:
    infos = record.get("hookInfos")
    if not isinstance(infos, list):
        return []
    errors = [e for e in (record.get("hookErrors") or [])
              if isinstance(e, str)]
    out = []
    for info in infos:
        if not isinstance(info, dict) or not isinstance(info.get("command"),
                                                        str):
            continue
        command = info["command"]
        if not command_hook(command):
            continue
        decision, reason = "allow", ""
        for error in errors:
            # An error counts against a hook only when it names that
            # hook's exact command: ``[<command>]: <stderr>``. Never by
            # name: ``/a/guard.py`` and ``/b/guard.py`` share one, and
            # Claude Code writes the command as declared (every named
            # error in a real library matched its hook's command exactly).
            # One that names nothing is left to the run's own attachment.
            named = re.match(r"^\[(?P<command>.*?)\]:\s*(?P<rest>.*)\Z",
                             error.strip(), re.DOTALL)
            if named and named.group("command") == command:
                decision, reason = "error", named.group("rest")
                break
        event = _event(record, ts, command, "Stop", decision, reason,
                       info.get("durationMs"), None, record.get("toolUseID"))
        event["_kind"] = "summary"
        out.append(event)
    return out


def _feedback(record: dict, ts: float, text: str) -> list[dict]:
    match = _FEEDBACK.match(text.strip())
    if not match:
        return []
    marks = list(_FEEDBACK_ENTRY.finditer(match.group("body")))
    out = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else None
        message = match.group("body")[mark.end():end]
        command = mark.group("command")
        if command_hook(command):
            event = _event(record, ts, command, match.group("event"), "block",
                           message, None, 2, None)
            event["_kind"] = "feedback"
            out.append(event)
    return out


def _tool_results(record: dict, ts: float) -> list[dict]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    out = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        match = _TOOL_BLOCK.match(_text_of(item.get("content")).strip())
        if not match:
            continue
        command = match.group("command")
        if command_hook(command):
            event = _event(record, ts, command,
                           f"{match.group('event')}:{match.group('tool')}",
                           "block", match.group("message"), None, 2,
                           item.get("tool_use_id"))
            event["_kind"] = "block"
            out.append(event)
    return out


def parse_lines(lines, resolver: Resolver | None = None) -> list[dict]:
    """Hook events in one transcript's lines (bytes or str), file order,
    each with its ``hook`` name and ``script`` (``finish``). ``resolver``
    reads each command's script (one per read; a fresh one for this home
    when not given)."""
    return finish(parse_raw(lines), resolver or Resolver(Path.home()))


def finish(raw: list[dict], resolver: Resolver) -> list[dict]:
    """``parse_raw``'s events named and given their scripts: the part of a
    parse that depends on the disk (what a command runs today, which
    plugin a ``${CLAUDE_PLUGIN_ROOT}`` command came from) and so is done
    on every read, never cached. Returns new dicts."""
    out = []
    for event in raw:
        event = dict(event)
        command = event.pop("_command", "") or ""
        project = event.pop("_project", None)
        event["hook"], event["script"] = resolver.name(command, project)
        out.append(event)
    return out


def parse_raw(lines) -> list[dict]:
    """Hook events in one transcript's lines (bytes or str), file order,
    from the file's bytes alone: each carries its ``_command`` and the
    session's ``_project`` (the first ``cwd`` the file records, which is
    what Claude Code sets ``$CLAUDE_PROJECT_DIR`` to), and no name yet.
    This is what the parse cache keeps.

    Runs that two records describe become one: a block's tool result
    absorbs the attachment with the same tool use id, and a Stop feedback
    message absorbs the nearest stop summary for the same command.
    """
    found: list[dict] = []
    project: str | None = None
    for line in lines:
        raw = line if isinstance(line, bytes) else line.encode("utf-8")
        if project is None and b'"cwd"' in raw:
            try:
                first = json.loads(raw)
            except ValueError:
                first = None
            if isinstance(first, dict) and isinstance(first.get("cwd"), str) \
                    and first["cwd"]:
                project = first["cwd"]
        if not any(needle in raw for needle in _NEEDLES):
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        ts = _epoch(record.get("timestamp"))
        if ts is None:
            continue
        kind = record.get("type")
        if kind == "attachment":
            found += _attachment(record, ts)
        elif kind == "system" and record.get("subtype") == "stop_hook_summary":
            found += _stop_summary(record, ts)
        elif kind == "user":
            found += _tool_results(record, ts)
            message = record.get("message")
            content = message.get("content") if isinstance(
                message, dict) else None
            text = _text_of(content)
            if not text and isinstance(record.get("content"), str):
                text = record["content"]
            if record.get("isMeta") and "hook feedback" in text:
                found += _feedback(record, ts, text)
    events = _collapse(found)
    for event in events:
        event["_project"] = project
    return events


_ATTACH_WINDOW = 5.0


def _pair(events, one_kind: str, other_kind: str, window: float,
          same_tool_use: bool = False):
    """(one index, other index) pairs: the same run seen in two records.

    ``other`` is the nearest unused record of the same command (the very
    string: two scripts that share a name are two hooks), session and
    event name within ``window`` seconds (or with the same tool use id);
    each record pairs at most once. Records are bucketed first, because a
    long transcript holds thousands of hook records.
    """
    def key(event):
        tail = event["_tool_use_id"] if same_tool_use else event["event"]
        return (event["_command"], event["session_id"], tail)

    buckets: dict[tuple, list[int]] = {}
    for j, other in enumerate(events):
        if other["_kind"] == other_kind:
            buckets.setdefault(key(other), []).append(j)
    pairs = []
    for i, one in enumerate(events):
        if one["_kind"] != one_kind:
            continue
        if same_tool_use and not one["_tool_use_id"]:
            continue
        pool = buckets.get(key(one))
        if not pool:
            continue
        best = None
        for j in pool:
            gap = 0.0 if same_tool_use else abs(events[j]["ts"] - one["ts"])
            if gap <= window and (best is None or gap < best[0]):
                best = (gap, j)
        if best is not None:
            pool.remove(best[1])
            pairs.append((i, best[1]))
    return pairs


def _collapse(events: list[dict]) -> list[dict]:
    """One event per run when two or three records describe it.

    * A PreToolUse block's tool result and the ``hook_success`` attachment
      with the same tool use id are one run: the block wins and takes the
      attachment's duration. (An exit-2 hook is recorded as an exit 0
      ``hook_success`` carrying the stderr, so without the tool result it
      would read as a warning.)
    * A Stop hook that has output leaves an attachment and also an entry in
      that turn's ``stop_hook_summary``: the attachment, which carries the
      exit code and stderr, wins.
    * A ``Stop hook feedback`` message is the block of the nearest Stop run
      of that hook (attachment or summary entry) and replaces it.
    """
    dropped: set[int] = set()
    for block, attachment in _pair(events, "block", "attachment", 0.0,
                                   same_tool_use=True):
        if events[block]["duration_ms"] is None:
            events[block]["duration_ms"] = events[attachment]["duration_ms"]
        dropped.add(attachment)
    live = [e if i not in dropped else dict(e, _kind="gone")
            for i, e in enumerate(events)]
    for attachment, summary in _pair(live, "attachment", "summary",
                                     _ATTACH_WINDOW):
        if live[attachment]["event"] == "Stop" or \
                live[attachment]["event"].startswith("Stop"):
            if live[attachment]["duration_ms"] is None:
                live[attachment]["duration_ms"] = live[summary]["duration_ms"]
            dropped.add(summary)
            live[summary]["_kind"] = "gone"
    for feedback, run in _pair(live, "feedback", "summary", _STOP_WINDOW):
        events[feedback]["duration_ms"] = live[run]["duration_ms"]
        dropped.add(run)
        live[run]["_kind"] = "gone"
    for feedback, run in _pair(live, "feedback", "attachment", _STOP_WINDOW):
        if events[feedback]["duration_ms"] is None:
            events[feedback]["duration_ms"] = live[run]["duration_ms"]
        dropped.add(run)
        live[run]["_kind"] = "gone"
    kept = []
    for i, event in enumerate(events):
        if i in dropped:
            continue
        event.pop("_tool_use_id", None)
        event.pop("_kind", None)
        kept.append(event)
    return kept


def _raw_file(path: str) -> list[dict] | None:
    """``parse_raw`` of one file; None when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return parse_raw(handle)
    except OSError:
        return None


def parse_file(path: str, since: float | None = None,
               resolver: Resolver | None = None, cache=None) -> list[dict]:
    """Hook events in one transcript file; unreadable files give none.
    ``cache`` (``transcript_cache.TranscriptCache``) answers for a file
    that has not changed since it was last parsed."""
    raw = cache.events(path, _raw_file) if cache is not None \
        else (_raw_file(path) or [])
    raw = [e for e in raw if since is None or e["ts"] >= since]
    return finish(raw, resolver or Resolver(Path.home()))


def _subagent_transcripts(main: list[str]) -> list[str]:
    """``<project>/<session>/subagents/*.jsonl`` beside the main transcripts.

    The session enumeration leaves ``subagents`` folders out on purpose
    (they are not conversations), but their hook runs are real. Each
    project folder is listed once, then each session folder once for its
    ``subagents`` child: no glob, no walk below that.
    """
    out: list[str] = []
    for project in sorted({os.path.dirname(path) for path in main}):
        try:
            with os.scandir(project) as entries:
                sessions = [e.path for e in entries if e.is_dir()]
        except OSError:
            continue
        for session in sessions:
            try:
                with os.scandir(os.path.join(session, "subagents")) as inner:
                    out += [e.path for e in inner if e.name.endswith(".jsonl")
                            and not e.name.startswith(".")]
            except OSError:
                continue
    return out


def claude_transcripts(home: Path, since: float | None = None) -> list[str]:
    """Claude transcript files that can hold events newer than ``since``.

    One enumeration of the roots (``search_writer.sources``) plus the
    subagent transcripts beside them, then a stat each: a transcript last
    written before ``since`` holds nothing newer.
    """
    from swe.sessions import search_writer

    main = [source.path for source in search_writer.sources(home)
            if source.harness == "claude"]
    out = []
    for path in main + _subagent_transcripts(main):
        if since is not None:
            try:
                if os.stat(path).st_mtime < since:
                    continue
            except OSError:
                continue
        out.append(path)
    return out


def backfill(home: Path, since: float | None = None,
             resolver: Resolver | None = None) -> list[dict]:
    """Every hook event the Claude transcripts hold since ``since``.

    ``repo`` is filled from each event's cwd (one lookup per folder).
    """
    from swe.hooks.transcript_cache import TranscriptCache

    resolver = resolver or Resolver(home)
    events: list[dict] = []
    cache = TranscriptCache(home)
    try:
        for path in claude_transcripts(home, since):
            events += parse_file(path, since, resolver, cache)
    finally:
        cache.close()
    repos: dict[str, str | None] = {}
    for event in events:
        cwd = event["cwd"]
        if cwd:
            if cwd not in repos:
                repos[cwd] = shim_runtime.repo_info(cwd)[0]
            event["repo"] = repos[cwd]
    return events
