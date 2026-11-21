#!/usr/bin/env python3
"""Record a harness CLI's structured protocol into replayable fixtures.

Runs the real CLI with a scripted stdin, records both directions line by
line (direction, seconds since spawn, the parsed message), redacts the
result, and writes it to::

    tests/fixtures/protocols/<adapter>/<version>/<scenario>.jsonl

with the canonical v2 events the adapter's wire module derives from it
beside it as ``<scenario>.events.json`` (the golden file the replay test
compares against).

    python3 scripts/record_protocol.py record --adapter=claude_stream \\
        --scenario=hello [--scenario=...] [--raw-dir=/scratch/dir]
    python3 scripts/record_protocol.py golden   # rebuild every golden
    python3 scripts/record_protocol.py scrub    # re-apply the blanking
    python3 scripts/record_protocol.py list     # the scenarios

``--adapter=claude_stream`` is recorded here; the other adapters'
scenarios live beside this file (``OTHER_ADAPTERS``: ``record_codex.py``
for ``codex_appserver``, ``record_opencode.py`` for ``opencode_serve``,
``record_acp.py`` for ``acp``) and share ``Redactor`` (and ``Driver``, where the wire is lines on
stdio).

Every scenario runs in a fresh ``mktemp -d`` folder holding two dummy
files, never in a real project, with the owner's MCP servers, hooks,
plugins and CLAUDE.md kept out as far as the CLI's flags allow
(``RECORDER_FLAGS``). The child gets a minimal environment: PATH, HOME,
the locale, and the API credential variables if the parent has them,
plus ``CLAUDE_CONFIG_DIR`` pointed at a second throwaway folder, so the
CLI's transcripts, plan files and settings never touch ~/.claude.

Redaction (``Redactor``) runs before anything is written: the work
folder becomes ``/work``, HOME ``~``, the user name ``user``; tokens,
keys and emails go; every UUID, API message id, request id and tool id
becomes a stable placeholder (``00000000-0000-4000-8000-000000000001``,
``msg_01``...) so a resume scenario still lines up; the init message and
the initialize answer are cut down to the fields the adapter reads (no
slash commands, skills, plugins, agents, MCP servers, account or paths).
A last check refuses to write a fixture that still holds the home path,
the user name, the account's email or ids, or a name from the owner's
plugin and skill lists. ``--raw-dir`` keeps an unredacted copy for the
person recording, outside the repo; never commit it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from swe.remote.adapters import claude_wire as wire  # noqa: E402
from swe.reports.transcripts import _redact_secrets  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "protocols"

# Isolation flags for recording only (the adapter never adds them):
# --safe-mode turns off CLAUDE.md, skills, plugins, hooks, MCP servers,
# custom commands and agents; --setting-sources=project skips the
# owner's user and local settings (the work folder has no project
# ones); --strict-mcp-config with an empty config leaves no MCP server.
# (--bare would also skip the keychain, but it trims the built-in tools
# to Bash, Edit and Read and ignores --tools, so plan mode, subagents
# and Write cannot be recorded under it.) A cheap model throughout.
RECORDER_FLAGS = ("--safe-mode", "--setting-sources=project",
                  "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}')
RECORDER_MODEL = "haiku"

# Environment the child keeps from ours, and what the adapter sets.
_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "USER", "SHELL",
             "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
_SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
               "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USER_EMAIL",
               "CLAUDE_CODE_ACCOUNT_UUID", "CLAUDE_CODE_ORGANIZATION_UUID",
               "CLAUDE_CODE_MESSAGING_TOKEN", "ANTHROPIC_BASE_URL")

SCENARIO_TIMEOUT_S = 240.0


class ScenarioError(Exception):
    pass


# ---------------------------------------------------------------------
# Redaction


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Not ``\b``: Codex joins two of them with ``_`` in a file name, and an
# underscore is a word character.
_UUID_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                      r"(?![0-9A-Za-z])")
# API message, request and tool-use ids (``msg_011Ch...``): a digit is
# required, so a field name such as ``task_notification`` never matches.
_ID_PREFIXES = ("msg", "req", "toolu", "srvtoolu")


def _prefixed_id_re(prefixes) -> "re.Pattern":
    # Lookarounds, not ``\b``: an id can follow an underscore
    # (``msg_fork_notice_ses_...``).
    return re.compile(r"(?<![0-9A-Za-z])(" + "|".join(prefixes)
                      + r")_(?=[A-Za-z]*[0-9])([A-Za-z0-9]{12,})"
                      r"(?![0-9A-Za-z])")


_PREFIXED_ID_RE = _prefixed_id_re(_ID_PREFIXES)
# The CLI's own per-user scratch folder (task output files).
_CLI_TMP_RE = re.compile(r"/(?:private/)?tmp/claude-\d+/[^\s\"'`]*")
_TMP_RE = re.compile(r"/(?:private/)?var/folders/[^\s\"'`]*")
_USERS_RE = re.compile(r"/(?:Users|home)/[^\s\"'`/]+")

# The fields of the init message and the initialize answer a recording
# keeps; everything else (commands, skills, plugins, agents, MCP
# servers, account, pid, paths) is dropped.
_INIT_KEEP = ("type", "subtype", "session_id", "uuid", "cwd", "model",
              "permissionMode", "claude_code_version", "capabilities",
              "tools", "output_style")
_INITIALIZE_KEEP = ("claude_code_version", "current_permission_mode",
                    "capabilities", "session_state", "models",
                    "fast_mode_state")


# Fields blanked whatever they hold, both opaque to the adapter: thinking
# signatures (large blobs) and a tool call's streamed input fragments,
# which split paths into pieces no pattern can catch (the complete input
# arrives in the assistant message and is redacted there).
_SCRUB = {"signature": lambda v: "sig", "partial_json": lambda v: ""}


def scrub(obj):
    """``_SCRUB`` applied to an already redacted fixture value."""
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _SCRUB[k](v) if k in _SCRUB and v else scrub(v)
                for k, v in obj.items()}
    return obj


class Redactor:
    def __init__(self, work: str, *, config: str = "", home: str, user: str,
                 secrets: list[str], deny: list[str],
                 hosts: list[str] | None = None,
                 id_prefixes: tuple = _ID_PREFIXES) -> None:
        self.work = [p for p in {work, os.path.realpath(work)} if p]
        self.config = [p for p in {config, os.path.realpath(config)} if p] \
            if config else []
        # The machine's own temp folder names and the work folder's
        # name: a streamed path can arrive split across deltas, so the
        # pieces are replaced (and checked for) on their own too.
        tmp = os.path.realpath(tempfile.gettempdir())
        self.pieces = sorted({seg for seg in tmp.split("/") if len(seg) >= 8}
                             | {os.path.basename(os.path.dirname(work))},
                             key=len,
                             reverse=True)
        self.home = home
        self.user = user
        self.secrets = [s for s in secrets if s and len(s) >= 6]
        self.deny = [d for d in deny if d]
        # This machine's names (a host name often holds its owner's).
        self.hosts = sorted({h for h in hosts or () if h and len(h) >= 4},
                            key=len, reverse=True)
        self._prefixed = _PREFIXED_ID_RE if id_prefixes == _ID_PREFIXES \
            else _prefixed_id_re(id_prefixes)
        self.ids: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def _id(self, kind: str, original: str, make) -> str:
        if original not in self.ids:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.ids[original] = make(self.counts[kind])
        return self.ids[original]

    def text(self, s: str) -> str:
        for secret in self.secrets:
            s = s.replace(secret, "[REDACTED]")
        for config in sorted(self.config, key=len, reverse=True):
            s = s.replace(config, "/config")
        for work in sorted(self.work, key=len, reverse=True):
            s = s.replace(work, "/work")
        s = _CLI_TMP_RE.sub("/tmp/claude/x", s)
        s = _TMP_RE.sub("/tmp/x", s)
        for piece in self.pieces:
            s = s.replace(piece, "x")
        if self.home:
            s = s.replace(self.home, "~")
        s = _USERS_RE.sub("~", s)
        for host in self.hosts:
            s = re.sub(re.escape(host), "host", s, flags=re.IGNORECASE)
        if self.user:
            s = re.sub(rf"\b{re.escape(self.user)}\b", "user", s)
        s = _EMAIL_RE.sub("user@example.com", s)
        s = _redact_secrets(s)
        s = _UUID_RE.sub(lambda m: self._id(
            "uuid", m.group(0).lower(),
            lambda n: f"00000000-0000-4000-8000-{n:012d}"), s)
        s = self._prefixed.sub(lambda m: self._id(
            m.group(1), m.group(0),
            lambda n, p=m.group(1): f"{p}_{n:02d}"), s)
        return s

    def value(self, obj):
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self.value(v) for v in obj]
        if isinstance(obj, dict):
            return {self.text(k): _SCRUB[k](v) if k in _SCRUB and v
                    else self.value(v) for k, v in obj.items()}
        return obj

    def reduce(self, msg):
        """The init message and the initialize answer cut to what the
        adapter reads."""
        if not isinstance(msg, dict):
            return msg
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            out = {k: msg[k] for k in _INIT_KEEP if k in msg}
            if isinstance(out.get("tools"), list):
                out["tools"] = [t for t in out["tools"] if isinstance(t, str)
                                and not t.startswith("mcp__")]
            return out
        if msg.get("type") == "control_response":
            resp = msg.get("response")
            body = resp.get("response") if isinstance(resp, dict) else None
            if isinstance(body, dict) and "commands" in body:
                kept = {k: body[k] for k in _INITIALIZE_KEEP if k in body}
                if isinstance(kept.get("models"), list):
                    kept["models"] = [{"value": m.get("value")}
                                      for m in kept["models"]
                                      if isinstance(m, dict)]
                msg = dict(msg)
                msg["response"] = dict(resp, response=kept)
        return msg

    def leaks(self, text: str) -> list[str]:
        """What in ``text`` must never reach the repo."""
        found = []
        needles = [self.home, *self.secrets, *self.deny, *self.pieces,
                   *self.hosts]
        if self.user:
            needles.append(self.user)
        low = text.lower()
        for needle in needles:
            if needle and needle.lower() in low:
                found.append(needle if needle in self.deny
                             or needle in self.pieces else "<secret>")
        if "/Users/" in text or "/var/folders/" in text:
            found.append("an absolute home or temp path")
        return found


# ---------------------------------------------------------------------
# Driving one CLI process


class Driver:
    """One CLI child: a reader thread that logs every line it prints,
    ``send`` that logs every line we write, and bounded waits."""

    def __init__(self, argv: list[str], cwd: str, env: dict) -> None:
        self.t0 = time.monotonic()
        self.log: list[dict] = []
        self.cond = threading.Condition()
        self.closed = False
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8")
        self.stderr = ""
        threading.Thread(target=self._read_err, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        self._n = 0

    def _read_err(self) -> None:
        assert self.proc.stderr is not None
        self.stderr = self.proc.stderr.read()[-4000:]

    def _now(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                entry = {"dir": "in", "t": self._now(),
                         "msg": json.loads(line)}
            except ValueError:
                entry = {"dir": "in", "t": self._now(), "raw": line}
            with self.cond:
                self.log.append(entry)
                self.cond.notify_all()
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def rid(self, what: str) -> str:
        self._n += 1
        return f"req_{self._n}_{what}"

    def send(self, msg: dict) -> dict:
        with self.cond:
            self.log.append({"dir": "out", "t": self._now(), "msg": msg})
        assert self.proc.stdin is not None
        self.proc.stdin.write(wire.encode(msg).decode("utf-8"))
        self.proc.stdin.flush()
        return msg

    def incoming(self, start: int = 0) -> list[dict]:
        with self.cond:
            return [e["msg"] for e in self.log[start:]
                    if e["dir"] == "in" and "msg" in e]

    def mark(self) -> int:
        with self.cond:
            return len(self.log)

    def wait(self, pred, timeout: float = 90.0, start: int = 0,
             what: str = "a message"):
        """The first incoming message since ``start`` that ``pred``
        accepts; ScenarioError after ``timeout`` or at EOF."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for e in self.log[start:]:
                    if e["dir"] == "in" and "msg" in e and pred(e["msg"]):
                        return e["msg"]
                left = deadline - time.monotonic()
                if self.closed or left <= 0:
                    raise ScenarioError(f"no {what} ({'EOF' if self.closed else 'timeout'})")
                self.cond.wait(left)

    def control(self, subtype: str, timeout: float = 60.0, **fields) -> dict:
        rid = self.rid(subtype)
        start = self.mark()
        self.send(wire.control_request(rid, subtype, **fields))
        return self.wait(lambda m: m.get("type") == "control_response"
                         and (m.get("response") or {}).get("request_id") == rid,
                         timeout, start, f"{subtype} answer")

    def turn(self, text: str, *, answer=None, timeout: float = 120.0,
             **kw) -> dict:
        """Send one user message and wait for its result, answering each
        ``can_use_tool`` with ``answer(request_id, request)`` (a wire
        message) or allowing it."""
        start = self.mark()
        self.send(wire.user_message(text, **kw))
        return self.until_result(start, answer=answer, timeout=timeout)

    def until_result(self, start: int, *, answer=None,
                     timeout: float = 120.0) -> dict:
        deadline = time.monotonic() + timeout
        handled: set = set()
        while True:
            msg = self.wait(
                lambda m: m.get("type") == "result" or (
                    m.get("type") == "control_request"
                    and m.get("request_id") not in handled),
                max(0.1, deadline - time.monotonic()), start, "result")
            if msg.get("type") == "result":
                return msg
            handled.add(msg.get("request_id"))
            req = msg.get("request") or {}
            if req.get("subtype") == "can_use_tool":
                reply = (answer(msg["request_id"], req) if answer else
                         wire.permission_allow(msg["request_id"],
                                               req.get("input")))
            else:
                reply = wire.control_error(msg["request_id"],
                                           "not supported by the recorder")
            self.send(reply)

    def settle(self, start: int, timeout: float = 15.0) -> None:
        """Wait for the session_state_changed idle that follows a turn
        (best effort: an older CLI sends none)."""
        try:
            self.wait(lambda m: m.get("type") == "system"
                      and m.get("subtype") == "session_state_changed"
                      and m.get("state") == "idle", timeout, start,
                      "idle state")
        except ScenarioError:
            pass

    def finish(self, grace: float = 15.0) -> int:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(5)


# ---------------------------------------------------------------------
# Scenarios. Each gets a Driver factory and the work folder.


def _handshake(d: Driver) -> dict:
    return d.control("initialize", hooks=None)


def sc_hello(spawn, work, ctx):
    d = spawn()
    _handshake(d)
    start = d.mark()
    d.turn("Reply with exactly these words and nothing else: hello there")
    d.settle(start)
    return [d]


def _allow(rid, req):
    return wire.permission_allow(rid, req.get("input"))


def _deny(rid, req):
    return wire.permission_deny(rid, "The user said no to this.")


def sc_approval(spawn, work, ctx):
    d = spawn(permission_mode="default")
    _handshake(d)
    start = d.mark()
    d.turn("Use the Write tool to create a file named allowed.txt in the "
           "current folder containing the single word yes. Do nothing else.",
           answer=_allow)
    d.settle(start)
    start = d.mark()
    d.turn("Use the Write tool to create a file named denied.txt in the "
           "current folder containing the single word no. If you are not "
           "allowed, reply only with: denied", answer=_deny)
    d.settle(start)
    return [d]


def _settings_files(*roots: str) -> list[str]:
    """Every settings file under ``roots``, relative to its root."""
    found = []
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.is_file() and path.name.startswith("settings") \
                    and path.suffix == ".json":
                found.append(f"{Path(root).name}/"
                             f"{path.relative_to(root).as_posix()}")
    return sorted(found)


def sc_always(spawn, work, ctx):
    """"Always allow" answered the way the adapter answers it: the CLI's
    own suggestions with every destination set to session. Proves the
    CLI takes them, asks no more for the same call, and writes no
    settings file."""
    d = spawn(permission_mode="default")
    _handshake(d)
    asked: list = []

    def always(rid, req):
        asked.append(req.get("tool_name"))
        return wire.permission_allow(
            rid, req.get("input"),
            wire.session_scoped(req.get("permission_suggestions") or []))

    before = _settings_files(work, ctx["config"])
    start = d.mark()
    d.turn("Use the Bash tool to run exactly this command and nothing "
           "else: touch marker.txt", answer=always)
    d.settle(start)
    first = len(asked)
    start = d.mark()
    d.turn("Use the Bash tool to run exactly the same command again: "
           "touch marker.txt. Then reply only with: done", answer=always)
    d.settle(start)
    ctx["asked_turn_1"] = first
    ctx["asked_turn_2"] = len(asked) - first
    ctx["settings_files_before"] = before
    ctx["settings_files_after"] = _settings_files(work, ctx["config"])
    return [d]


def sc_interrupt(spawn, work, ctx):
    d = spawn()
    _handshake(d)
    start = d.mark()
    d.send(wire.user_message(
        "Write the numbers from 1 to 400, one per line, and no other text."))
    d.wait(lambda m: m.get("type") == "stream_event"
           and ((m.get("event") or {}).get("delta") or {}).get("type")
           == "text_delta", 60, start, "a text delta")
    d.control("interrupt")
    d.until_result(start, timeout=60)
    d.settle(start)
    start = d.mark()
    d.turn("Reply with exactly: ok")
    d.settle(start)
    return [d]


def _exit_plan(rid, req):
    if req.get("tool_name") == "ExitPlanMode":
        return wire.permission_deny(
            rid, "The plan was received. Stop here and wait.")
    return _deny(rid, req)


def sc_plan_mode(spawn, work, ctx):
    d = spawn(permission_mode="default")
    _handshake(d)
    d.control("set_permission_mode", mode="plan")
    start = d.mark()
    d.turn("Make a two-step plan for appending the line 'done' to notes.txt "
           "in the current folder. Do not edit anything. Present the plan "
           "with the ExitPlanMode tool.", answer=_exit_plan)
    d.settle(start)
    d.control("set_permission_mode", mode="default")
    start = d.mark()
    d.turn("Reply with exactly: back")
    d.settle(start)
    return [d]


def sc_plan_approve(spawn, work, ctx):
    d = spawn(permission_mode="plan")
    _handshake(d)
    start = d.mark()

    def answer(rid, req):
        if req.get("tool_name") == "ExitPlanMode":
            return _allow(rid, req)
        return _deny(rid, req)

    d.turn("Plan, in one step, how to append the line 'done' to notes.txt in "
           "the current folder, present it with ExitPlanMode, and once it "
           "is approved carry it out with the Edit tool.", answer=answer)
    d.settle(start)
    return [d]


def sc_set_model(spawn, work, ctx):
    d = spawn()
    _handshake(d)
    d.control("set_model", model="sonnet")
    start = d.mark()
    d.turn("Reply with exactly: ok")
    d.settle(start)
    d.control("set_model", model=RECORDER_MODEL)
    start = d.mark()
    d.turn("Reply with exactly: ok again")
    d.settle(start)
    return [d]


def _mid_reply(d: Driver, start: int, change, ctx: dict) -> None:
    """Send ``change()`` (a control request) once the reply streams, and
    note what the reply in progress did: whether the change was answered
    before the turn's result, whether the reply ran on to its end, and
    the model of each assistant message (G32)."""
    d.wait(lambda m: m.get("type") == "stream_event"
           and ((m.get("event") or {}).get("delta") or {}).get("type")
           == "text_delta", 60, start, "a text delta")
    answer = change()
    ctx["answered"] = (answer.get("response") or {}).get("subtype")
    # Whether the turn had already ended by the time the answer came.
    ctx["result_before_answer"] = any(
        m.get("type") == "result" for m in d.incoming(start))
    result = d.until_result(start, timeout=120)
    d.settle(start)
    ctx["result_subtype"] = result.get("subtype")
    ctx["stop_reason"] = result.get("stop_reason")
    text = result.get("result") if isinstance(result.get("result"), str) \
        else ""
    ctx["reply_lines"] = len([x for x in text.splitlines() if x.strip()])
    ctx["models_in_turn"] = [
        (m.get("message") or {}).get("model") for m in d.incoming(start)
        if m.get("type") == "assistant"]


def sc_set_model_mid(spawn, work, ctx):
    """``set_model`` sent while a reply streams: does the reply in
    progress switch, get cut, or finish on the old model?"""
    d = spawn()
    _handshake(d)
    start = d.mark()
    d.send(wire.user_message(
        "Write the numbers from 1 to 150, one per line, and no other text."))
    _mid_reply(d, start, lambda: d.control("set_model", model="sonnet"), ctx)
    start = d.mark()
    d.turn("Reply with exactly: ok")
    d.settle(start)
    ctx["next_turn_models"] = [
        (m.get("message") or {}).get("model") for m in d.incoming(start)
        if m.get("type") == "assistant"]
    return [d]


def sc_plan_mode_mid(spawn, work, ctx):
    """``set_permission_mode`` plan sent while a reply streams, then
    back to default after it."""
    d = spawn(permission_mode="default")
    _handshake(d)
    start = d.mark()
    d.send(wire.user_message(
        "Write the numbers from 1 to 150, one per line, and no other text."))
    _mid_reply(d, start,
               lambda: d.control("set_permission_mode", mode="plan"), ctx)
    d.control("set_permission_mode", mode="default")
    start = d.mark()
    d.turn("Reply with exactly: back")
    d.settle(start)
    return [d]


def sc_subagent(spawn, work, ctx):
    d = spawn()
    _handshake(d)
    start = d.mark()
    d.turn("Use the Task tool with subagent_type general-purpose to read "
           "notes.txt in the current folder and report its first line. "
           "Then reply with that line only.", timeout=180)
    d.settle(start, 60)
    return [d]


def sc_stop_task(spawn, work, ctx):
    d = spawn()
    _handshake(d)
    start = d.mark()
    d.send(wire.user_message(
        "Use the Task tool with subagent_type general-purpose and "
        "run_in_background true to count slowly from 1 to 50 with the Bash "
        "tool, one 'sleep 1; echo n' command per number. Then reply: started"))
    task = d.wait(lambda m: m.get("type") == "system"
                  and m.get("subtype") == "task_started", 120, start,
                  "task_started")
    d.control("stop_task", task_id=task.get("task_id"))
    d.wait(lambda m: m.get("type") == "system"
           and m.get("subtype") in ("task_notification", "task_updated")
           and m.get("task_id") == task.get("task_id")
           and (m.get("status") in ("stopped", "killed", "failed")
                or (m.get("patch") or {}).get("status")
                in ("killed", "stopped", "failed")), 60, start,
           "the task's stop")
    d.until_result(start, timeout=120)
    d.settle(start, 60)
    return [d]


def sc_steer(spawn, work, ctx):
    d = spawn()
    _handshake(d)
    start = d.mark()
    d.send(wire.user_message(
        "Write the numbers from 1 to 300, one per line, and no other text."))
    d.wait(lambda m: m.get("type") == "stream_event"
           and ((m.get("event") or {}).get("delta") or {}).get("type")
           == "text_delta", 60, start, "a text delta")
    d.send(wire.user_message("Stop counting now and reply only with: steered",
                             priority="now"))
    first = d.until_result(start, timeout=120)
    after = d.mark()
    # A second result follows when the CLI ran the message as its own turn.
    try:
        d.wait(lambda m: m.get("type") == "result" and m is not first, 30,
               after, "a second result")
    except ScenarioError:
        pass
    d.settle(start, 20)
    return [d]


def _is_can_use(m) -> bool:
    return m.get("type") == "control_request" and (
        m.get("request") or {}).get("subtype") == "can_use_tool"


def _results_then_idle(d: Driver, start: int, *, skip=(), timeout=90.0,
                       answer=None) -> list:
    """Every ``result`` since ``start`` until the CLI goes idle, allowing
    (or ``answer``-ing) any new ``can_use_tool`` except the ids in
    ``skip``; the results in order."""
    deadline = time.monotonic() + timeout
    handled = set(skip)
    while True:
        msg = d.wait(
            lambda m: (m.get("type") == "system"
                       and m.get("subtype") == "session_state_changed"
                       and m.get("state") == "idle")
            or (_is_can_use(m) and m.get("request_id") not in handled),
            max(0.1, deadline - time.monotonic()), start, "idle")
        if msg.get("type") == "system":
            return [m for m in d.incoming(start) if m.get("type") == "result"]
        handled.add(msg["request_id"])
        req = msg.get("request") or {}
        d.send(answer(msg["request_id"], req) if answer
               else _allow(msg["request_id"], req))


def sc_steer_tool(spawn, work, ctx):
    """``priority: "now"`` sent while a slow Bash call runs: is the call
    killed, what does its tool_result say, does the file it would write
    appear, how does the turn end."""
    d = spawn(permission_mode="default")
    _handshake(d)
    start = d.mark()
    d.send(wire.user_message(
        "Use the Bash tool, in the foreground (not in the background), to "
        "run exactly this command and nothing else: sleep 20 && echo done "
        "> slept.txt. Then reply only with: finished"))
    asked = d.wait(_is_can_use, 90, start, "the Bash approval")
    d.send(_allow(asked["request_id"], asked.get("request") or {}))
    allowed_at = time.monotonic()
    # Let the command get going: a tool_progress line if the CLI sends
    # one, else three seconds.
    try:
        d.wait(lambda m: m.get("type") == "tool_progress", 3, start,
               "tool progress")
    except ScenarioError:
        pass
    time.sleep(max(0.0, 3.0 - (time.monotonic() - allowed_at)))
    ctx["written_before_steer"] = Path(work, "slept.txt").exists()
    d.send(wire.user_message("Stop what you are doing and reply only with: "
                             "steered", priority="now"))
    results = _results_then_idle(d, start, skip={asked["request_id"]})
    ctx["idle_after_s"] = round(time.monotonic() - allowed_at, 1)
    ctx["results"] = len(results)
    ctx["written_at_idle"] = Path(work, "slept.txt").exists()
    # A command left running would finish 20 s after it started.
    time.sleep(max(0.0, 25.0 - (time.monotonic() - allowed_at)))
    ctx["written_after_25s"] = Path(work, "slept.txt").exists()
    return [d]


def sc_steer_approval(spawn, work, ctx):
    """``priority: "now"`` sent while a ``can_use_tool`` is open: is the
    request cancelled, left open, or denied for us."""
    d = spawn(permission_mode="default")
    _handshake(d)
    start = d.mark()
    d.send(wire.user_message(
        "Use the Bash tool to run exactly this command and nothing else: "
        "touch approved.txt. Then reply only with: done"))
    asked = d.wait(_is_can_use, 90, start, "the Bash approval")
    rid = asked["request_id"]
    after = d.mark()
    d.send(wire.user_message("Do not run anything. Reply only with: steered",
                             priority="now"))
    # Unanswered on purpose: what does the CLI do with it?
    cancelled = False
    try:
        d.wait(lambda m: m.get("type") == "control_cancel_request"
               and m.get("request_id") == rid, 30, after, "a cancel")
        cancelled = True
    except ScenarioError:
        pass
    ctx["cancelled"] = cancelled
    ctx["result_before_answer"] = any(
        m.get("type") == "result" for m in d.incoming(after))
    if not cancelled:
        # Left open: deny it so the session can go on.
        d.send(wire.permission_deny(rid, "The user steered instead."))
    _results_then_idle(d, start, skip={rid}, answer=_deny)
    ctx["file_written"] = Path(work, "approved.txt").exists()
    return [d]


def sc_resume(spawn, work, ctx):
    sid = wire.new_session_id()
    d1 = spawn(session_id=sid, persist=True)
    _handshake(d1)
    start = d1.mark()
    d1.turn("Remember the word kumquat. Reply with exactly: noted")
    d1.settle(start)
    d1.finish()
    d2 = spawn(resume_id=sid, persist=True)
    _handshake(d2)
    start = d2.mark()
    d2.turn("What word did I ask you to remember? Reply with that word only.")
    d2.settle(start)
    fork_id = wire.new_session_id()
    d2.finish()
    d3 = spawn(resume_id=sid, fork=True, session_id=fork_id, persist=True)
    _handshake(d3)
    start = d3.mark()
    d3.turn("Reply with the remembered word in capital letters only.")
    d3.settle(start)
    return [d1, d2, d3]


def sc_rewind(spawn, work, ctx):
    d = spawn(permission_mode="acceptEdits", persist=True,
              env={"CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "true"})
    _handshake(d)
    mid = wire.new_session_id()
    start = d.mark()
    d.turn("Use the Write tool to overwrite notes.txt in the current folder "
           "with the single word changed. Do nothing else.", uuid=mid)
    d.settle(start)
    ctx["after_write"] = Path(work, "notes.txt").read_text()
    d.control("rewind_files", user_message_id=mid)
    ctx["after_rewind"] = Path(work, "notes.txt").read_text()
    return [d]


SCENARIOS = {
    "hello": sc_hello,
    "approval": sc_approval,
    "always": sc_always,
    "interrupt": sc_interrupt,
    "plan_mode": sc_plan_mode,
    "plan_approve": sc_plan_approve,
    "set_model": sc_set_model,
    "set_model_mid": sc_set_model_mid,
    "plan_mode_mid": sc_plan_mode_mid,
    "subagent": sc_subagent,
    "stop_task": sc_stop_task,
    "steer": sc_steer,
    "steer_tool": sc_steer_tool,
    "steer_approval": sc_steer_approval,
    "resume": sc_resume,
    "rewind": sc_rewind,
}


# ---------------------------------------------------------------------


def _child_env(extra: dict) -> dict:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update(wire.SPAWN_ENV)
    env.update(extra)
    return env


def _deny_names(raw_logs: list[list[dict]]) -> list[str]:
    """Names from the owner's setup (plugins, skills, commands, agents,
    MCP servers) distinctive enough to search for in a fixture."""
    names: set = set()
    for log in raw_logs:
        for e in log:
            msg = e.get("msg")
            if not isinstance(msg, dict):
                continue
            body = msg
            if msg.get("type") == "control_response":
                body = (msg.get("response") or {}).get("response") or {}
            for key in ("plugins", "skills", "slash_commands",
                        "mcp_servers", "commands"):
                # Built-in commands are the CLI's, not the owner's.
                for item in body.get(key) or ():
                    if isinstance(item, dict) and item.get("builtin"):
                        continue
                    name = item.get("name") if isinstance(item, dict) else item
                    if isinstance(name, str) and len(name) >= 8 and (
                            "-" in name or ":" in name):
                        names.add(name)
    # A name our own scenario typed (a built-in agent type) is no leak.
    sent = " ".join(json.dumps(e.get("msg")) for log in raw_logs for e in log
                    if e.get("dir") == "out")
    return sorted(n for n in names if n not in sent)


def record(adapter: str, scenario: str, version: str, raw_dir: Path | None,
           claude: str) -> Path:
    if adapter in OTHER_ADAPTERS:
        return _other(adapter).record(scenario, version, raw_dir, claude)
    if adapter != "claude_stream":
        raise SystemExit(f"no recorder for adapter {adapter!r}")
    fn = SCENARIOS[scenario]
    root = tempfile.mkdtemp(prefix="swe-rec-")
    work = os.path.join(root, "work")
    config = os.path.join(root, "config")
    os.mkdir(work)
    os.mkdir(config)
    Path(work, "notes.txt").write_text("first line of the notes\nsecond line\n")
    Path(work, "hello.py").write_text('print("hello")\n')
    drivers: list[Driver] = []
    tails: list[list[str]] = []
    ctx: dict = {"config": config}

    def spawn(*, persist: bool = False, env: dict | None = None, **kw):
        kw.setdefault("session_id", "" if kw.get("resume_id") else
                      wire.new_session_id())
        kw.setdefault("model", RECORDER_MODEL)
        argv = wire.spawn_argv([claude], **kw)
        tail = argv[1:]
        extras = list(RECORDER_FLAGS)
        if not persist:
            extras.append("--no-session-persistence")
        tails.append(tail + extras)
        child_env = _child_env(env or {})
        # The CLI's own state (transcripts, plans, settings) goes to a
        # throwaway config folder, never the owner's ~/.claude.
        child_env["CLAUDE_CONFIG_DIR"] = config
        d = Driver(argv + extras, work, child_env)
        drivers.append(d)
        return d

    timer = threading.Timer(SCENARIO_TIMEOUT_S,
                            lambda: [d.proc.kill() for d in drivers])
    timer.start()
    error = ""
    try:
        fn(spawn, work, ctx)
    except ScenarioError as exc:
        error = str(exc)
    finally:
        ctx.pop("config", None)
        timer.cancel()
        codes = [d.finish() for d in drivers]
    home = os.path.expanduser("~")
    user = getpass.getuser()
    red = Redactor(work, config=config, home=home, user=user,
                   secrets=[os.environ.get(k, "") for k in _SECRET_ENV],
                   deny=_deny_names([d.log for d in drivers]))
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        with open(raw_dir / f"{scenario}.raw.jsonl", "w") as fh:
            for i, d in enumerate(drivers):
                for e in d.log:
                    fh.write(json.dumps({"proc": i, **e}) + "\n")
                fh.write(json.dumps({"proc": i, "stderr": d.stderr}) + "\n")
    lines = [{
        "fixture": 1, "adapter": adapter, "version": version,
        "scenario": scenario, "synthetic": False,
        "recorded": time.strftime("%Y-%m-%d"),
        "argv_tails": [red.value(t) for t in tails],
        "env_set": sorted(wire.SPAWN_ENV),
        "error": error or None,
        "notes": red.value(ctx) or None,
    }]
    for i, d in enumerate(drivers):
        for e in d.log:
            entry = {"proc": i, "dir": e["dir"], "t": e["t"]}
            if "msg" in e:
                entry["msg"] = red.value(red.reduce(e["msg"]))
            else:
                entry["raw"] = red.text(e["raw"])
            lines.append(entry)
        lines.append({"proc": i, "exit": codes[i]})
    body = "".join(json.dumps(line, ensure_ascii=False) + "\n"
                   for line in lines)
    leaks = red.leaks(body)
    if leaks:
        raise SystemExit(f"{scenario}: refusing to write, the fixture still "
                         f"holds {sorted(set(leaks))}")
    out = FIXTURES / adapter / version / f"{scenario}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    shutil.rmtree(root, ignore_errors=True)
    golden(out)
    print(f"{scenario}: {len(lines) - 1} lines, exit {codes}"
          + (f", stopped early: {error}" if error else ""))
    return out


# Adapters recorded by a module beside this one: adapter id -> module.
OTHER_ADAPTERS = {"codex_appserver": "record_codex",
                  "opencode_serve": "record_opencode",
                  "acp": "record_acp"}


def _other(adapter: str):
    """The recorder module of an adapter this file does not record."""
    import importlib

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    return importlib.import_module(OTHER_ADAPTERS[adapter])


def _replay_for(path: Path):
    """The wire module's ``replay`` for a fixture, by the adapter folder
    it sits in."""
    adapter = path.parent.parent.name
    if adapter in OTHER_ADAPTERS:
        return _other(adapter).replay
    return wire.replay


def golden(path: Path) -> Path:
    """Write ``<scenario>.events.json`` beside a fixture: the canonical
    v2 events its adapter's wire module derives from it."""
    events = _replay_for(path)(load_fixture(path))
    out = path.with_name(path.name[:-len(".jsonl")] + ".events.json")
    out.write_text(json.dumps(events, indent=1, ensure_ascii=False) + "\n")
    return out


def load_fixture(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def cli_version(claude: str) -> str:
    """The version ``<cli> --version`` prints."""
    out = subprocess.run([claude, "--version"], capture_output=True,
                         text=True, timeout=30).stdout
    m = re.search(r"\d+\.\d+\.\d+", out)
    if not m:
        raise SystemExit(f"cannot read the version of {claude}")
    return m.group(0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--adapter", default="claude_stream",
                     choices=["claude_stream", *sorted(OTHER_ADAPTERS)])
    rec.add_argument("--scenario", action="append", required=True)
    rec.add_argument("--raw-dir", type=Path)
    rec.add_argument("--claude", default=shutil.which("claude") or "claude")
    rec.add_argument("--cli", help="the CLI binary of another adapter "
                                   "(default: found on PATH)")
    sub.add_parser("golden")
    sub.add_parser("scrub")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    if args.cmd == "list":
        print("\n".join(sorted(SCENARIOS)))
        for adapter in sorted(OTHER_ADAPTERS):
            print("\n".join(f"{adapter}: {name}"
                            for name in sorted(_other(adapter).SCENARIOS)))
        return 0
    if args.cmd == "scrub":
        for path in sorted(FIXTURES.rglob("*.jsonl")):
            lines = [scrub(line) for line in load_fixture(path)]
            path.write_text("".join(json.dumps(line, ensure_ascii=False)
                                    + "\n" for line in lines))
            print(golden(path).relative_to(ROOT))
        return 0
    if args.cmd == "golden":
        for path in sorted(FIXTURES.rglob("*.jsonl")):
            print(golden(path).relative_to(ROOT))
        return 0
    if args.adapter in OTHER_ADAPTERS:
        other = _other(args.adapter)
        known, cli = other.SCENARIOS, args.cli or other.default_cli()
    else:
        known, cli = SCENARIOS, args.claude
    unknown = [name for name in args.scenario if name not in known]
    if unknown:
        ap.error(f"no {args.adapter} scenario named {', '.join(unknown)} "
                 f"(choose from {', '.join(sorted(known))})")
    version = cli_version(cli)
    for scenario in args.scenario:
        record(args.adapter, scenario, version, args.raw_dir, cli)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
