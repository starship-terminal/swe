#!/usr/bin/env python3
"""Shared-hook shim runtime — copied verbatim to ~/.swe/hooks/_runtime/shim.py.

It is deliberately stdlib-only and imports nothing from swe: a harness
runs the generated wrapper under whatever ``python3`` it finds, which
cannot import this package. Tests import it directly; ``swe init`` and
``swe hooks sync`` copy it into the hub so generated stubs can call it by
absolute path.

Invocation: ``python3 shim.py --contract '<json>' -- <shared> [args...]``

``--contract`` carries one JSON object the generator baked from the
harness's row in ``hooks/contracts.py``::

    {"harness": "cursor", "schema": "flat", "event_map": {...},
     "fields": {...}, "must_print_json": true}

stdin is the harness's hook payload. The runtime translates it to the
normalized shape documented in ``contracts.py``, execs the shared script
with it on stdin, then maps the script's exit code and stdout back into
the harness's contract. Anything after ``-- <shared>`` is passed to the
script as argv; the first such arg also serves as the native event name
when the payload does not carry ``hook_event_name`` (cursor declarations
can pass the event as an argument, the way superset's hooks do).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

# Normalized fields in output order. ``raw`` and ``timestamp`` are added
# separately: raw is the untouched payload, timestamp is the shim's own.
_FIELD_DEFAULTS = (
    "session_id", "transcript_path", "cwd", "tool_name", "tool_input",
    "tool_output", "prompt", "stop_hook_active", "session_source",
    "agent_id", "agent_type", "agent_transcript_path",
    "last_assistant_message", "error", "permission_mode", "tool_use_id",
)

# Normalized field -> native keys to try after the field's own name. The
# per-contract ``fields`` table adds more on top of these.
_ALIASES = {
    "session_id": ("conversation_id",),
    "tool_output": ("tool_response",),
    "session_source": ("source",),
    "agent_transcript_path": ("agent_transcript",),
    "error": ("error_message",),
}


# --- the event log ----------------------------------------------------------
#
# One JSON line per hook run goes to ~/.swe/cache/hooks/events.jsonl, which
# ``swe hooks stats`` and ``swe hooks log`` read (docs/data-model.md has the
# schema). The writer lives here, in the one file a harness can run, so it is
# stdlib-only and imports nothing from swe. Harness-tier scripts that want
# the same line call ``record`` themselves:
#
#     sys.path.insert(0, os.path.expanduser("~/.swe/hooks/_runtime"))
#     import shim
#     shim.record("my-hook", "block", "why", harness="claude",
#                 event="PreToolUse:Bash", session_id=payload.get("session_id"),
#                 cwd=payload.get("cwd"))
#
# ``record`` never raises and never blocks: one O_APPEND write, no locks.

EVENTS_VERSION = 1
# One generation is kept: events.jsonl rotates to events.jsonl.1 at this
# size, so the log never holds more than twice the cap (about 40,000 runs).
EVENTS_CAP_BYTES = 5 * 1024 * 1024
REASON_MAX = 200
_OFF_VALUES = ("0", "off", "false", "no")
# Normalized events whose native name carries the tool, the way Claude
# spells a matcher: ``PreToolUse:Bash``.
_TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure",
                "PermissionRequest")


def events_path(home: str | None = None) -> str:
    """``~/.swe/cache/hooks/events.jsonl``, spelled by hand (see above)."""
    root = home if home is not None else os.path.expanduser("~")
    return os.path.join(root, ".swe", "cache", "hooks", "events.jsonl")


def reason_line(text) -> str:
    """The first non-empty line of ``text``, trimmed and capped.

    Only this line is ever stored: never the rest of a message, a reply
    or a command.
    """
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line[:REASON_MAX]
    return ""


def _read_head(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(512).strip()
    except OSError:
        return None


def repo_info(cwd: str | None) -> tuple[str | None, str | None]:
    """(repo root, branch) for ``cwd`` from ``.git`` alone, no subprocess.

    Walks up to the first ``.git`` (a worktree's is a file naming its git
    dir) and reads ``HEAD``. A detached HEAD reports its short sha. Both
    come back None when there is no repository.
    """
    if not cwd or not isinstance(cwd, str):
        return None, None
    directory = os.path.abspath(cwd)
    for _ in range(64):
        dotgit = os.path.join(directory, ".git")
        if os.path.isdir(dotgit):
            git_dir = dotgit
        elif os.path.isfile(dotgit):
            pointer = _read_head(dotgit) or ""
            if not pointer.startswith("gitdir:"):
                return directory, None
            git_dir = os.path.join(directory, pointer[7:].strip())
        else:
            parent = os.path.dirname(directory)
            if parent == directory:
                return None, None
            directory = parent
            continue
        head = _read_head(os.path.join(git_dir, "HEAD")) or ""
        if head.startswith("ref:"):
            branch = head[4:].strip()
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
            return directory, branch or None
        return directory, head[:7] or None
    return None, None


def record(hook: str, decision: str, reason: str = "", *, harness: str = "",
           event: str = "", session_id: str | None = None,
           cwd: str | None = None, duration_ms: int | None = None,
           exit_code: int | None = None,
           stop_hook_active: bool | None = None,
           home: str | None = None, ts: float | None = None,
           script: str | None = None) -> bool:
    """Append one hook run to the event log; True when a line was written.

    Fails open: any error (an unwritable folder, a full disk, a bad
    argument) is swallowed and the caller's result is unchanged.
    ``SWE_HOOK_LOG=0`` switches recording off. ``decision`` is one of
    ``block``, ``warn``, ``allow`` or ``error``; ``ts`` defaults to now,
    the moment the run finished. ``script`` is the file that ran, which
    ``swe hooks stats`` uses to tell two hooks with one name apart; it
    defaults to the running program (``sys.argv[0]``) when that is a
    file, so a hook calling ``record`` itself is identified without
    passing it.
    """
    try:
        if os.environ.get("SWE_HOOK_LOG", "").strip().lower() in _OFF_VALUES:
            return False
        repo, branch = repo_info(cwd)
        if script is None and sys.argv and sys.argv[0]:
            script = sys.argv[0]
        script = (os.path.realpath(script)
                  if isinstance(script, str) and script
                  and os.path.isfile(script) else None)
        line = json.dumps({
            "v": EVENTS_VERSION,
            "ts": round(time.time() if ts is None else ts, 3),
            "hook": str(hook),
            "harness": str(harness or ""),
            "event": str(event or ""),
            "session_id": session_id if isinstance(session_id, str) else None,
            "cwd": cwd if isinstance(cwd, str) else None,
            "repo": repo,
            "git_branch": branch,
            "decision": str(decision),
            "reason": reason_line(reason),
            "duration_ms": (int(duration_ms)
                            if duration_ms is not None else None),
            "exit_code": exit_code if isinstance(exit_code, int) else None,
            "stop_hook_active": (stop_hook_active
                                 if isinstance(stop_hook_active, bool)
                                 else None),
            "script": script,
        }, ensure_ascii=False, separators=(",", ":")) + "\n"
        path = events_path(home)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        _rotate(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def _rotate(path: str) -> None:
    """Move a full log to ``<path>.1``, replacing the older generation.

    Best effort: two runs that rotate at once can swap a fresh file over
    ``.1``, which costs one generation of history and nothing else. The
    size is read twice so the window stays a few microseconds wide.
    """
    try:
        if os.stat(path).st_size >= EVENTS_CAP_BYTES:
            if os.stat(path).st_size >= EVENTS_CAP_BYTES:
                os.replace(path, path + ".1")
    except OSError:
        pass


def _decide(returncode: int, stdout: str, stderr: str,
            contract: dict | None = None) -> tuple[str, str]:
    """(decision, reason line) for one finished script.

    Exit 2 blocks. Exit 0 blocks too when stdout is a JSON object that
    denies (``decision: block``, ``permissionDecision: deny``,
    ``continue: false``), warns when anything went to stderr, and
    otherwise allows. Any other exit is the harness's non-blocking error:
    the hook failed open. A contract that must print JSON and is not the
    flat schema (gemini, qwen) has no "ask": ``translate`` sends deny, so
    an ``ask`` is logged as the block the harness received.
    """
    if returncode == 2:
        return "block", reason_line(stderr) or reason_line(
            _json_reason(stdout))
    if returncode == 0:
        text = (stdout or "").strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            verdict = _permission_of(parsed) if isinstance(parsed, dict) \
                else None
            contract = contract or {}
            asks_deny = (verdict == "ask" and contract.get("must_print_json")
                         and contract.get("schema") != "flat")
            if verdict == "deny" or asks_deny:
                return "block", reason_line(_json_reason(stdout)) or \
                    reason_line(stderr)
        if (stderr or "").strip():
            return "warn", reason_line(stderr)
        return "allow", ""
    return "error", reason_line(stderr) or f"exit {returncode}"


def _json_reason(stdout: str) -> str:
    """The ``reason`` a JSON answer carries, or ''."""
    try:
        parsed = json.loads((stdout or "").strip())
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    hso = parsed.get("hookSpecificOutput")
    for source in (parsed, hso if isinstance(hso, dict) else {}):
        for key in ("reason", "permissionDecisionReason", "stopReason"):
            if isinstance(source.get(key), str) and source[key].strip():
                return source[key]
    return ""


def _event_label(norm: dict, native: str) -> str:
    """The harness's own event name, with ``:<tool>`` on tool events."""
    tool = norm.get("tool_name")
    if isinstance(tool, str) and tool and norm.get("event") in _TOOL_EVENTS:
        return f"{native}:{tool}"
    return native


def _log_run(norm: dict, native: str, script: str, started: float,
             returncode: int | None, stdout: str, stderr: str,
             contract: dict | None = None) -> None:
    """Record one shim run; never raises."""
    try:
        if returncode is None:
            decision, reason = "error", "shim could not run the script"
        else:
            decision, reason = _decide(returncode, stdout, stderr, contract)
        hook = os.path.splitext(os.path.basename(script))[0]
        active = norm.get("stop_hook_active")
        record(
            hook, decision, reason,
            harness=str(norm.get("harness") or ""),
            event=_event_label(norm, native),
            session_id=norm.get("session_id"), cwd=norm.get("cwd"),
            duration_ms=int((time.time() - started) * 1000),
            exit_code=returncode,
            stop_hook_active=active if isinstance(active, bool) else None,
            # The shared script, not the shim: that is the hook that ran.
            script=script)
    except Exception:
        pass


def normalize(raw: dict, contract: dict, argv_event: str) -> tuple[dict, str]:
    """(normalized payload, native event name) for the shared script.

    Unknown event names pass through as themselves — the map only holds
    renames, so a harness event swe never surveyed still reaches the
    script with its native spelling intact.
    """
    if not isinstance(raw, dict):
        raw = {}
    # The payload's own event name wins; argv is the documented fallback
    # for harnesses whose payload lacks it (cursor declarations pass the
    # event as an argument). An option like --verbose must never become
    # the event name a real payload already carries.
    native = str(raw.get("hook_event_name") or "") or argv_event
    event_map = contract.get("event_map") or {}
    extra_fields = contract.get("fields") or {}

    roots = raw.get("workspace_roots")
    cwd = raw.get("cwd")
    if cwd is None and isinstance(roots, list) and roots:
        cwd = roots[0]

    norm: dict = {
        "event": event_map.get(native, native),
        "harness": contract.get("harness", ""),
        "cwd": cwd,
    }
    for name in _FIELD_DEFAULTS:
        if name == "cwd":
            continue
        value = raw.get(name)
        if value is None:
            for alias in _ALIASES.get(name, ()) + tuple(
                    extra_fields.get(name, ())):
                if raw.get(alias) is not None:
                    value = raw[alias]
                    break
        norm[name] = value
    norm["timestamp"] = time.time()
    norm["raw"] = raw
    return norm, native


def _is_permission_event(native: str) -> bool:
    """Cursor-style: permission-shaped answers for pre*/before*/subagent*."""
    return native.startswith(("pre", "before", "subagent"))


def _permission_of(parsed: dict) -> str | None:
    """allow|deny|ask from a normalized output object, or None."""
    hso = parsed.get("hookSpecificOutput")
    if isinstance(hso, dict):
        decision = hso.get("permissionDecision")
        if decision in ("allow", "deny", "ask"):
            return decision
    decision = parsed.get("decision")
    if decision in ("deny", "block"):
        return "deny"
    if decision in ("allow", "approve"):
        return "allow"
    if decision == "ask":
        return "ask"
    direct = parsed.get("permission")
    if direct in ("allow", "deny", "ask"):
        return direct
    # continue:false stops the turn — on a permission event that is a
    # deny, and it must not fall through to the allow fallback.
    if parsed.get("continue") is False:
        return "deny"
    return None


def default_output(contract: dict, native: str) -> str:
    """The contract's fail-safe stdout when nothing valid came back.

    Only called for contracts that MUST print JSON — cursor stalls on a
    pre*/before* event without a permission answer and gemini/qwen parse
    stdout unconditionally. Permission events answer allow (fail open);
    everything else continues. Both keys ride in one object for flat, so
    a stub that cannot see the event name still answers correctly.
    """
    if contract.get("schema") == "flat":
        if _is_permission_event(native):
            return '{"permission": "allow"}'
        return '{"continue": true}'
    return "{}"


def block_output(contract: dict, native: str) -> str:
    """The JSON an exit-2 block becomes for a must-print contract.

    The normalized contract's ``exit 2 = block`` maps to the harness's
    machine answer: deny for flat permission events, ``continue: false``
    for the rest, ``decision: block`` for the grouped schema. Both flat
    keys ride together, the same trick as ``default_output``.
    """
    if contract.get("schema") == "flat":
        return '{"permission": "deny", "continue": false}'
    return '{"decision": "block"}'


def translate(contract: dict, native: str, out: str) -> str:
    """Map a shared script's stdout into the harness's answer text.

    The normalized output contract is Claude's shape (``hookSpecificOutput``,
    ``decision``, ``continue``, ``reason``). Grouped-schema harnesses get it
    verbatim, except must-print contracts whose stdout has to be a JSON
    object (the decision is hoisted out of ``hookSpecificOutput`` so gemini
    sees it). The flat schema needs exactly the key it reads:
    ``permission`` on pre*/before* events, ``continue`` elsewhere.
    """
    schema = contract.get("schema")
    must_print = bool(contract.get("must_print_json"))
    stripped = (out or "").strip()
    parsed = None
    if stripped:
        try:
            candidate = json.loads(stripped)
        except ValueError:
            candidate = None
        if isinstance(candidate, dict):
            parsed = candidate

    if schema == "flat":
        if _is_permission_event(native):
            permission = _permission_of(parsed or {}) or "allow"
            return json.dumps({"permission": permission})
        cont = True
        if parsed:
            if parsed.get("continue") is not None:
                cont = bool(parsed["continue"])
            elif parsed.get("decision") == "block":
                cont = False
        return json.dumps({"continue": cont})

    if must_print:
        if parsed is None:
            return default_output(contract, native)
        if "decision" not in parsed:
            hso = parsed.get("hookSpecificOutput")
            if isinstance(hso, dict):
                decision = hso.get("permissionDecision")
                if decision in ("allow", "deny"):
                    parsed["decision"] = decision
                elif decision == "ask":
                    # No "ask" exists outside Claude; deny is the
                    # cautious read when the harness can't prompt.
                    parsed["decision"] = "deny"
        return json.dumps(parsed)

    return out


def _blocked(contract: dict, native: str, out: str) -> str:
    """The must-print answer for an exit-2 block.

    Flat contracts get the deny+continue:false union — a script's own
    stdout cannot weaken a block there. Grouped ones keep the script's
    JSON when it parses (``reason`` and friends still matter) but force
    ``decision: block`` over whatever it printed: exit 2 is the block
    signal, and a bare ``{}`` must not read as "no verdict".
    """
    if contract.get("schema") == "flat":
        return block_output(contract, native)
    stripped = (out or "").strip()
    if stripped:
        try:
            candidate = json.loads(stripped)
        except ValueError:
            candidate = None
        if isinstance(candidate, dict):
            candidate["decision"] = "block"
            return json.dumps(candidate)
    return block_output(contract, native)


def _split(argv: list[str]) -> tuple[dict, list[str]]:
    """(--contract value, rest) — everything after ``--`` is script argv."""
    try:
        sep = argv.index("--")
    except ValueError:
        raise SystemExit("shim: expected '--' before the script path")
    head, rest = argv[:sep], argv[sep + 1:]
    if not rest:
        raise SystemExit("shim: no shared script after '--'")
    contract: dict = {}
    for i, token in enumerate(head):
        if token == "--contract" and i + 1 < len(head):
            try:
                contract = json.loads(head[i + 1])
            except ValueError:
                contract = {}
    return contract if isinstance(contract, dict) else {}, rest


def main(argv: list[str]) -> int:
    contract, script_argv = _split(argv)
    script, extra = script_argv[0], script_argv[1:]
    argv_event = extra[0] if extra else ""

    try:
        raw_text = sys.stdin.read()
    except Exception:
        raw_text = ""
    try:
        raw = json.loads(raw_text)
    except ValueError:
        raw = {}
    norm, native = normalize(raw, contract, argv_event)

    must_print = bool(contract.get("must_print_json"))
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, script] + extra,
            input=json.dumps(norm), capture_output=True, text=True,
            close_fds=True)
    except Exception:
        _log_run(norm, native, script, started, None, "", "")
        if must_print:
            sys.stdout.write(default_output(contract, native) + "\n")
        return 0
    _log_run(norm, native, script, started, proc.returncode,
             proc.stdout, proc.stderr, contract)

    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if must_print:
        # These contracts stall on anything but valid JSON, so the shim
        # always answers. Exit 2 is the block signal and is checked
        # before stdout — a hook that prints {} and exits 2 still blocks.
        # Any other stdout is forwarded only through translate(), which
        # re-validates it: a prefix like '{"x' from a mid-write crash is
        # not valid JSON and must not reach the harness. Every other
        # failure fails open. Exit code is always 0 — the JSON carries
        # the verdict.
        if proc.returncode == 0:
            text = translate(contract, native, proc.stdout)
        elif proc.returncode == 2:
            text = _blocked(contract, native, proc.stdout)
        elif proc.stdout.strip().startswith("{"):
            text = translate(contract, native, proc.stdout)
        else:
            text = default_output(contract, native)
        if text:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0
    if proc.returncode == 0:
        text = translate(contract, native, proc.stdout)
        if text:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
    elif proc.stdout:
        sys.stdout.write(proc.stdout)
    return proc.returncode


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        # A shim that dies must never block a session: say nothing and
        # pass, matching every contract's fail-open path.
        sys.exit(0)
