"""The ``opencode_serve`` scenarios of ``record_protocol.py``.

Run through the recorder, never directly::

    python3 scripts/record_protocol.py record --adapter=opencode_serve \\
        --scenario=hello [--raw-dir=/scratch/dir]

Every scenario runs the real ``opencode serve`` in a fresh ``mktemp -d``
tree: a work folder with two dummy files and a throwaway ``HOME`` with
the four ``XDG_*`` folders inside it, so nothing of the owner's
(config, AGENTS.md, plugins, MCP servers, sessions, logins) is read or
written. The child gets ``OPENAI_API_KEY`` from the environment, which
is the only credential opencode finds there, and a random
``OPENCODE_SERVER_PASSWORD``; neither is written anywhere. The model
is ``RECORDER_MODEL`` at ``RECORDER_EFFORT`` throughout.

A fixture holds the requests we made, their answers, and every event
of ``GET /api/event``. Before it is written, an event the adapter does
not read is cut down to its type, ``native`` blobs (encrypted
reasoning, provider ids) are dropped, and ``/api/info`` to its
version. The shared ``Redactor`` then replaces paths, ids, the user
and host names, and refuses to write anything that still holds one.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import record_protocol as rp
from record_codex import _png
from swe.remote.adapters import opencode_wire as wire

replay = wire.replay

RECORDER_MODEL = "openai/gpt-5.4-nano"
RECORDER_EFFORT = "low"
ADAPTER = "opencode_serve"

_ENV_KEEP = ("PATH", "TMPDIR", "LANG", "LC_ALL", "OPENAI_API_KEY")
_SECRET_ENV = ("OPENAI_API_KEY",)
_ID_PREFIXES = ("ses", "msg", "evt", "per", "call", "sh", "resp", "rs", "fc",
                "prt", "frm", "sav")
# Ask for everything: the recorder's way to see permission requests
# (the adapter leaves the rules to the user's opencode config).
ASK_ALL = [{"action": "*", "resource": "*", "effect": "ask"}]
_SESSION_KEEP = ("id", "model", "agent", "fork", "title")
END_EVENTS = ("session.execution.succeeded", "session.execution.failed",
              "session.execution.interrupted")


def default_cli() -> str:
    return shutil.which("opencode") or "opencode"


class HttpDriver:
    """One ``opencode serve`` child: every request, answer and stream
    event logged with its time."""

    def __init__(self, cli: str, work: str, env: dict) -> None:
        self.t0 = time.monotonic()
        self.log: list[dict] = []
        self.cond = threading.Condition()
        self.closed = False
        self.session = ""
        self._n = 0
        self._password = secrets.token_urlsafe(24)
        self.work = work
        self.proc = subprocess.Popen(
            [cli, "serve", *wire.BIND_ARGS], cwd=work,
            env={**env, wire.PASSWORD_ENV: self._password},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.stderr = ""
        threading.Thread(target=self._read_err, daemon=True).start()
        timer = threading.Timer(60.0, self.proc.kill)
        timer.start()
        line = self.proc.stdout.readline().decode("utf-8", "replace")
        timer.cancel()
        self.base = wire.listening_url(line)
        if not self.base:
            raise rp.ScenarioError(f"the server never listened: {line!r}")
        threading.Thread(target=self._read_events, daemon=True).start()
        self.wait(lambda e: e.get("type") == "server.connected", 30.0, 0,
                  "the event stream")

    def _read_err(self) -> None:
        self.stderr = self.proc.stderr.read().decode("utf-8", "replace")[-4000:]

    def _now(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def _headers(self) -> dict:
        return {"Authorization": wire.basic_auth(self._password),
                "Content-Type": "application/json",
                "Accept": "application/json"}

    def _read_events(self) -> None:
        req = urllib.request.Request(
            self.base + wire.events_stream().path,
            headers={**self._headers(), "Accept": "text/event-stream"})
        parser = wire.SseParser()
        try:
            with urllib.request.urlopen(req, timeout=None) as resp:
                for raw in resp:
                    event = parser.feed(raw)
                    if event is not None:
                        with self.cond:
                            self.log.append({"dir": "in", "t": self._now(),
                                             "sse": event})
                            self.cond.notify_all()
        except (OSError, ValueError):
            pass
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def call(self, request: wire.Request, timeout: float = 60.0):
        """One request; ``(status, body)``, both logged."""
        self._n += 1
        n = self._n
        with self.cond:
            self.log.append({"dir": "out", "t": self._now(), "http": {
                "n": n, "method": request.method, "path": request.path,
                "body": request.body}})
        data = None if request.body is None \
            else json.dumps(request.body).encode()
        req = urllib.request.Request(self.base + request.path, data=data,
                                     method=request.method,
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = {"raw": raw.decode("utf-8", "replace")[:500]}
        with self.cond:
            self.log.append({"dir": "in", "t": self._now(),
                             "http": {"n": n, "status": status,
                                      "body": body}})
        return status, body

    def mark(self) -> int:
        with self.cond:
            return len(self.log)

    def events(self, start: int = 0) -> list[dict]:
        with self.cond:
            return [e["sse"] for e in self.log[start:] if "sse" in e]

    def wait(self, pred, timeout: float = 120.0, start: int = 0,
             what: str = "an event"):
        """The first stream event since ``start`` that ``pred`` accepts."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for e in self.log[start:]:
                    if "sse" in e and pred(e["sse"]):
                        return e["sse"]
                left = deadline - time.monotonic()
                if self.closed or left <= 0:
                    raise rp.ScenarioError(
                        f"no {what} ({'EOF' if self.closed else 'timeout'})")
                self.cond.wait(left)

    def mine(self, event: dict) -> bool:
        data = event.get("data")
        return isinstance(data, dict) and data.get("sessionID") == self.session

    # -- the session -----------------------------------------------------

    def ok(self, request: wire.Request, what: str):
        status, body = self.call(request)
        if status not in (200, 204):
            raise rp.ScenarioError(f"{what}: {status} {body}")
        return (body or {}).get("data") if isinstance(body, dict) else None

    def open(self, *, ask: bool = False, session_id: str = "") -> str:
        """``GET /api/info`` and ``POST /api/session``; the session id.
        ``ask`` adds the ask-for-everything rules (recorder only)."""
        self.ok(wire.info(), "info")
        request = wire.create_session(self.work, model=RECORDER_MODEL,
                                      effort=RECORDER_EFFORT,
                                      session_id=session_id)
        if ask:
            request.body["permissions"] = ASK_ALL
        self.session = self.ok(request, "create")["id"]
        return self.session

    def resume(self, session_id: str, fork: bool = False) -> str:
        self.ok(wire.info(), "info")
        request = wire.fork_session(session_id) if fork \
            else wire.get_session(session_id)
        self.session = self.ok(request, "resume")["id"]
        return self.session

    def prompt(self, text: str, **kw) -> dict:
        return self.ok(wire.prompt(self.session, text, **kw), "prompt")

    def until_done(self, start: int, *, answer="once", message: str = "",
                   timeout: float = 150.0, handled=()) -> dict:
        """Wait for the execution to end, answering each permission with
        ``answer`` (a decision, or a callable from the event's data)."""
        deadline = time.monotonic() + timeout
        handled = set(handled)
        while True:
            event = self.wait(
                lambda e: self.mine(e) and (
                    e.get("type") in END_EVENTS or (
                        e.get("type") == "permission.asked"
                        and e["data"]["id"] not in handled)),
                max(0.1, deadline - time.monotonic()), start,
                "the execution to end")
            if event["type"] in END_EVENTS:
                return event
            handled.add(event["data"]["id"])
            decision = answer(event["data"]) if callable(answer) else answer
            self.call(wire.permission_reply(self.session, event["data"]["id"],
                                            decision, message))

    def turn(self, text: str, *, answer="once", message: str = "",
             timeout: float = 150.0, **kw) -> dict:
        start = self.mark()
        sent = self.prompt(text, **kw)
        ended = self.until_done(start, answer=answer, message=message,
                                timeout=timeout)
        # The usage line that follows the end belongs to the turn too.
        time.sleep(0.4)
        return {"end": ended["type"], "message_id": sent["id"]}

    def wait_delta(self, start: int, n: int = 1,
                   timeout: float = 90.0) -> None:
        """Wait until the reply has streamed ``n`` deltas since
        ``start``."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                got = sum(1 for e in self.log[start:] if "sse" in e
                          and e["sse"].get("type") == "session.text.delta")
                if got >= n:
                    return
                left = deadline - time.monotonic()
                if self.closed or left <= 0:
                    raise rp.ScenarioError(f"no {n} reply deltas")
                self.cond.wait(left)

    def finish(self, grace: float = 10.0) -> int:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            return self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(5)


# ---------------------------------------------------------------------
# Scenarios. Each gets a driver factory and the work folder.


_COUNT = ("Count slowly from 1 to 60, one number per line, with a short "
          "sentence about each number.")


def sc_hello(spawn, work, ctx):
    d = spawn()
    d.open()
    d.turn("Reply with exactly these words and nothing else: hello there")


def sc_approval(spawn, work, ctx):
    d = spawn()
    d.open(ask=True)
    ctx["ends"] = [
        d.turn("Run the shell command `touch allowed.txt` in the current "
               "folder, then reply with just: done", answer="once")["end"],
        d.turn("Run the shell command `touch denied.txt`. If you are not "
               "allowed, reply only with: denied", answer="reject")["end"],
        d.turn("Create a file named written.txt containing the single word "
               "yes, using your file editing tool. Then reply with just: "
               "done", answer="once")["end"]]
    ctx["files"] = sorted(os.listdir(work))


def sc_deny_message(spawn, work, ctx):
    """A reject that carries a message: does the turn go on?"""
    d = spawn()
    d.open(ask=True)
    ctx["end"] = d.turn(
        "Run the shell command `touch denied.txt`. If you are not allowed, "
        "reply only with: denied", answer="reject",
        message="Not that file. Do not run anything.")["end"]
    ctx["files"] = sorted(os.listdir(work))


def sc_always(spawn, work, ctx):
    """``always``: is the next same command asked, and what is saved?"""
    d = spawn()
    d.open(ask=True)
    asked = []

    def answer(data):
        asked.append(data.get("action"))
        return "always"

    d.turn("Run the shell command `touch one.txt`, then reply with just: "
           "done", answer=answer)
    first = len(asked)
    d.turn("Run the shell command `touch two.txt`, then reply with just: "
           "done", answer=answer)
    ctx["approvals_first_turn"] = first
    ctx["approvals_second_turn"] = len(asked) - first
    status, saved = d.call(wire.Request("GET", "/api/permission/saved"))
    rules = (saved or {}).get("data") if isinstance(saved, dict) else None
    ctx["saved_rules"] = [{k: r.get(k) for k in ("action", "resource")}
                          for r in rules or ()]
    ctx["saved_rules_name_a_project"] = bool(rules) and all(
        r.get("projectID") for r in rules)
    ctx["files"] = sorted(os.listdir(work))


def sc_interrupt(spawn, work, ctx):
    d = spawn()
    d.open()
    start = d.mark()
    d.prompt(_COUNT)
    d.wait_delta(start, 8)
    d.call(wire.interrupt(d.session))
    d.until_done(start)
    # Idle now: what an interrupt with no turn answers.
    d.call(wire.interrupt(d.session))


def sc_interrupt_early(spawn, work, ctx):
    """An interrupt sent the moment the prompt is taken, before the
    execution has started."""
    d = spawn()
    d.open()
    d.turn("Reply only with: ready")
    start = d.mark()
    d.prompt(_COUNT)
    status, body = d.call(wire.interrupt(d.session))
    ctx["answer"] = body
    ctx["end"] = d.until_done(start)["type"]
    time.sleep(0.5)
    ctx["deltas"] = sum(1 for e in d.events(start)
                        if e.get("type") == "session.text.delta")


def sc_steer(spawn, work, ctx):
    d = spawn()
    d.open()
    start = d.mark()
    d.prompt(_COUNT)
    d.wait_delta(start, 8)
    d.prompt("Stop counting. Reply only with the word: steered", steer=True)
    d.until_done(start)
    time.sleep(1.0)
    ctx["executions_ended"] = sum(
        1 for e in d.events(start) if e.get("type") in END_EVENTS)


def sc_steer_tool(spawn, work, ctx):
    """A steer while a command runs: is the command cut?"""
    d = spawn()
    d.open()
    start = d.mark()
    d.prompt("Run this exact shell command and nothing else: "
             "sleep 25 && touch late.txt . Then reply with just: finished")
    d.wait(lambda e: d.mine(e) and e.get("type") == "session.tool.called",
           90.0, start, "the command to start")
    began = time.monotonic()
    time.sleep(2.0)
    d.prompt("Never mind the command. Reply only with the word: steered",
             steer=True)
    d.until_done(start)
    ctx["seconds_from_command_to_turn_end"] = round(
        time.monotonic() - began, 1)
    ctx["late_file_at_turn_end"] = os.path.exists(
        os.path.join(work, "late.txt"))
    left = 30.0 - (time.monotonic() - began)
    if left > 0:
        time.sleep(left)
    ctx["late_file_30s_after_command_began"] = os.path.exists(
        os.path.join(work, "late.txt"))


def sc_steer_approval(spawn, work, ctx):
    """A steer while a permission is open: is the request withdrawn?"""
    d = spawn()
    d.open(ask=True)
    start = d.mark()
    d.prompt("Run the shell command `touch asked.txt` in the current "
             "folder, then reply with just: done")
    asked = d.wait(lambda e: e.get("type") == "permission.asked", 90.0,
                   start, "a permission request")
    time.sleep(1.0)
    d.prompt("Do not run that. Reply only with the word: steered",
             steer=True)
    try:
        d.wait(lambda e: d.mine(e) and e.get("type") in END_EVENTS, 20.0,
               start, "the execution to end")
        ctx["turn_ended_without_an_answer"] = True
    except rp.ScenarioError:
        ctx["turn_ended_without_an_answer"] = False
        d.call(wire.permission_reply(d.session, asked["data"]["id"],
                                     "reject"))
        d.until_done(start, handled=[asked["data"]["id"]])
        time.sleep(3.0)
    ctx["asked_file_exists"] = os.path.exists(os.path.join(work, "asked.txt"))
    ctx["executions_ended"] = sum(
        1 for e in d.events(start) if e.get("type") in END_EVENTS)


def sc_steer_interrupt(spawn, work, ctx):
    """An interrupt while a steer waits in the inbox: where does the
    steer go?"""
    d = spawn()
    d.open(ask=True)
    start = d.mark()
    d.prompt("Run the shell command `touch asked.txt` in the current "
             "folder, then reply with just: done")
    d.wait(lambda e: e.get("type") == "permission.asked", 90.0, start,
           "a permission request")
    d.prompt("Do not run that. Reply only with the word: steered",
             steer=True)
    time.sleep(1.0)
    d.call(wire.interrupt(d.session))
    d.until_done(start)
    after = d.mark()
    try:
        d.wait(lambda e: d.mine(e) and e.get("type") in END_EVENTS, 20.0,
               after, "a second execution")
        ctx["steer_ran_after_the_interrupt"] = True
    except rp.ScenarioError:
        ctx["steer_ran_after_the_interrupt"] = False
        # Still in the inbox? The next prompt shows.
        d.turn("Reply only with the word: next")
    ctx["asked_file_exists"] = os.path.exists(os.path.join(work, "asked.txt"))


def sc_resume(spawn, work, ctx):
    d1 = spawn()
    session = d1.open()
    d1.turn("Remember this word: papaya. Reply only with: ok")
    d1.finish()
    d2 = spawn()
    d2.resume(session)
    d2.turn("Which word did I ask you to remember? Reply with just the "
            "word.")
    d2.finish()
    d3 = spawn()
    d3.resume(session, fork=True)
    d3.turn("Which word did I ask you to remember? Reply with just the "
            "word.")


def sc_rollback(spawn, work, ctx):
    d = spawn()
    d.open()
    d.turn("Remember this word: papaya. Reply only with: ok")
    second = d.turn("Remember this second word: walrus. Reply only with: ok")
    d.ok(wire.revert_stage(d.session, second["message_id"]), "stage")
    d.ok(wire.revert_commit(d.session), "commit")
    d.turn("Which words did I ask you to remember? Reply with just the "
           "words.")


def sc_options(spawn, work, ctx):
    """``/model`` between turns: another effort, then another model."""
    d = spawn()
    d.open()
    d.turn("Reply only with: one")
    d.ok(wire.select_model(d.session, wire.model_ref(
        RECORDER_MODEL, "medium")), "effort")
    d.turn("Reply only with: two")
    d.ok(wire.select_model(d.session, wire.model_ref(
        "openai/gpt-5.4-mini", "low")), "model")
    d.turn("Reply only with: three")
    ctx["models_that_ran"] = [
        e["data"].get("model") for e in d.events()
        if e.get("type") == "session.step.started" and d.mine(e)]


def sc_plan_mode(spawn, work, ctx):
    """The ``plan`` agent and back."""
    d = spawn()
    d.open()
    d.ok(wire.select_agent(d.session, wire.PLAN_AGENT), "plan")
    d.turn("Create a file named planned.txt containing the word no. If you "
           "cannot because you are only planning, reply only with: planning")
    ctx["file_while_planning"] = os.path.exists(
        os.path.join(work, "planned.txt"))
    d.ok(wire.select_agent(d.session, wire.BUILD_AGENT), "build")
    d.turn("Create a file named built.txt containing the word yes, then "
           "reply only with: built")
    ctx["file_after_build"] = os.path.exists(os.path.join(work, "built.txt"))
    ctx["agents_that_ran"] = [
        e["data"].get("agent") for e in d.events()
        if e.get("type") == "session.step.started" and d.mine(e)]


def _mid_prompt(d, request, what: str, ctx: dict) -> None:
    """Post ``request`` once the reply streams, and note what the
    execution in progress did (G32)."""
    start = d.mark()
    d.prompt(_COUNT)
    d.wait_delta(start, 8)
    before = sum(1 for e in d.events(start)
                 if e.get("type") == "session.text.delta")
    status, _body = d.call(request)
    ctx["answer_status"] = status
    ctx["end"] = d.until_done(start)["type"]
    time.sleep(0.5)
    events = d.events(start)
    ctx["deltas_after_change"] = sum(
        1 for e in events if e.get("type") == "session.text.delta") - before
    ctx["steps"] = [{k: e["data"].get(k) for k in ("model", "agent")}
                    for e in events if e.get("type") == "session.step.started"
                    and d.mine(e)]


def sc_model_mid(spawn, work, ctx):
    """``POST .../model`` while a prompt runs, then a turn after it."""
    d = spawn()
    d.open()
    _mid_prompt(d, wire.select_model(d.session, wire.model_ref(
        "openai/gpt-5.4-mini", RECORDER_EFFORT)), "model", ctx)
    start = d.mark()
    d.turn("Reply only with: after")
    ctx["next_steps"] = [e["data"].get("model") for e in d.events(start)
                         if e.get("type") == "session.step.started"
                         and d.mine(e)]


def sc_plan_mode_mid(spawn, work, ctx):
    """``POST .../agent`` plan while a prompt runs, then back."""
    d = spawn()
    d.open()
    _mid_prompt(d, wire.select_agent(d.session, wire.PLAN_AGENT), "plan",
                ctx)
    d.ok(wire.select_agent(d.session, wire.BUILD_AGENT), "build")
    d.turn("Reply only with: back")


def sc_providers(spawn, work, ctx):
    """The server's own lists: ``GET /api/config/providers`` (1.x's
    path), ``GET /api/provider`` and ``GET /api/model``."""
    d = spawn()
    d.open()
    for path in ("/api/config/providers", "/api/provider", "/api/model"):
        status, _body = d.call(wire.Request("GET", path, None))
        ctx.setdefault("paths", {})[path] = status


def sc_attach(spawn, work, ctx):
    """Files as ``file://`` attachments: an image, then a text file."""
    path = os.path.join(work, "square.png")
    Path(path).write_bytes(_png(255, 0, 0))
    d = spawn()
    d.open()
    d.turn("What colour is the attached image? Reply with one word.",
           attachments=[path])
    d.turn("What is the first line of the attached file? Reply with just "
           "that line.", attachments=[os.path.join(work, "notes.txt")])


def sc_error(spawn, work, ctx):
    """A turn that fails: a model the provider does not have."""
    d = spawn()
    d.open()
    status, _body = d.call(wire.select_model(d.session, wire.model_ref(
        "openai/no-such-model-for-the-recorder")))
    ctx["bad_model_accepted"] = status in (200, 204)
    ctx["end"] = d.turn("Reply with: ok", timeout=90.0)["end"]
    d.ok(wire.select_model(d.session, wire.model_ref(
        RECORDER_MODEL, RECORDER_EFFORT)), "model")
    ctx["end_after_a_good_model"] = d.turn("Reply with: ok")["end"]


def sc_own_id(spawn, work, ctx):
    """A session id the caller chose."""
    d = spawn()
    session = d.open(session_id="ses_swe0recorder0000000000001")
    ctx["kept_our_id"] = session == "ses_swe0recorder0000000000001"
    d.turn("Reply only with: ok")
    status, body = d.call(wire.Request("POST", "/api/session", {
        "id": "11111111-2222-4333-8444-555555555555",
        "location": {"directory": work}}))
    ctx["uuid_id_status"] = status


SCENARIOS = {
    "hello": sc_hello,
    "approval": sc_approval,
    "deny_message": sc_deny_message,
    "always": sc_always,
    "interrupt": sc_interrupt,
    "interrupt_early": sc_interrupt_early,
    "steer": sc_steer,
    "steer_tool": sc_steer_tool,
    "steer_approval": sc_steer_approval,
    "steer_interrupt": sc_steer_interrupt,
    "resume": sc_resume,
    "rollback": sc_rollback,
    "options": sc_options,
    "plan_mode": sc_plan_mode,
    "attach": sc_attach,
    "error": sc_error,
    "own_id": sc_own_id,
    "model_mid": sc_model_mid,
    "plan_mode_mid": sc_plan_mode_mid,
    "providers": sc_providers,
}


# ---------------------------------------------------------------------
# Cutting a line down to what the adapter reads


def _strip_native(value):
    if isinstance(value, list):
        return [_strip_native(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_native(v) for k, v in value.items()
                if k != "native"}
    return value


def _providers(data):
    """A provider or model list cut down to ids and names (G32): a
    provider entry carries settings (base URLs, keys) and a model entry
    costs and limits, and nothing but ids and names is committed. None
    for anything that is neither."""
    listed = data.get("providers") if isinstance(data, dict) else data
    if not isinstance(listed, list) or not listed or not all(
            isinstance(p, dict) and isinstance(p.get("id"), str)
            for p in listed):
        return None
    if all("providerID" in p for p in listed):
        return [{k: p[k] for k in ("id", "providerID", "name")
                 if isinstance(p.get(k), str)} for p in listed]
    if all("package" in p or "settings" in p or "models" in p
           for p in listed):
        return [{k: p[k] for k in ("id", "name") if isinstance(p.get(k), str)}
                for p in listed]
    return None


def reduce(entry: dict) -> dict:
    if "sse" in entry:
        event = entry["sse"]
        etype = event.get("type")
        if etype not in wire.READ_EVENTS:
            data = event.get("data")
            kept = {"type": etype}
            if isinstance(data, dict) and "sessionID" in data:
                kept["data"] = {"sessionID": data["sessionID"]}
            return {**entry, "sse": kept}
        return {**entry, "sse": {"type": etype,
                                 "data": _strip_native(event.get("data"))}}
    http = entry.get("http") or {}
    body = http.get("body")
    if entry.get("dir") == "in" and isinstance(body, dict):
        if "version" in body and "pid" in body:
            return {**entry, "http": {**http, "body": {
                "version": body["version"]}}}
        data = body.get("data")
        providers = _providers(data)
        if providers is not None:
            return {**entry, "http": {**http, "body": {"data": providers}}}
        if isinstance(data, dict) and "projectID" in data:
            # A session object: the id, its model and where it forked.
            body = {"data": {k: data[k] for k in _SESSION_KEEP
                             if k in data}}
        if isinstance(data, list) and any(
                isinstance(d, dict) and "projectID" in d for d in data):
            # Saved permission rules: what each allows, nothing else.
            body = {"data": [
                {k: d[k] for k in ("action", "resource") if k in d}
                | {"project": bool(d.get("projectID"))}
                for d in data if isinstance(d, dict)]}
        return {**entry, "http": {**http, "body": _strip_native(body)}}
    return entry


def record(scenario: str, version: str, raw_dir: Path | None,
           cli: str) -> Path:
    fn = SCENARIOS[scenario]
    root = tempfile.mkdtemp(prefix="swe-rec-")
    work = os.path.join(root, "work")
    home = os.path.join(root, "home")
    for folder in (work, home):
        os.mkdir(folder)
    Path(work, "notes.txt").write_text("first line of the notes\nsecond line\n")
    Path(work, "hello.py").write_text('print("hello")\n')
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set: the recorder's "
                         "throwaway HOME has no other login")
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update(HOME=home, XDG_CONFIG_HOME=home + "/.config",
               XDG_DATA_HOME=home + "/.local/share",
               XDG_STATE_HOME=home + "/.local/state",
               XDG_CACHE_HOME=home + "/.cache")
    drivers: list[HttpDriver] = []
    ctx: dict = {}

    def spawn():
        d = HttpDriver(cli, work, env)
        drivers.append(d)
        return d

    timer = threading.Timer(rp.SCENARIO_TIMEOUT_S,
                            lambda: [d.proc.kill() for d in drivers])
    timer.start()
    error = ""
    try:
        fn(spawn, work, ctx)
    except rp.ScenarioError as exc:
        error = str(exc)
    finally:
        timer.cancel()
        codes = [d.finish() for d in drivers]
    host = socket.gethostname()
    red = rp.Redactor(
        work, config=home, home=os.path.expanduser("~"),
        user=getpass.getuser(),
        secrets=[os.environ.get(k, "") for k in _SECRET_ENV]
        + [d._password for d in drivers], deny=[],
        hosts=[host, host.split(".")[0]], id_prefixes=_ID_PREFIXES)
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        with open(raw_dir / f"{scenario}.raw.jsonl", "w") as fh:
            for i, d in enumerate(drivers):
                for e in d.log:
                    fh.write(json.dumps({"proc": i, **e}) + "\n")
                fh.write(json.dumps({"proc": i, "stderr": d.stderr}) + "\n")
    lines = [{
        "fixture": 1, "adapter": ADAPTER, "version": version,
        "scenario": scenario, "synthetic": False,
        "recorded": time.strftime("%Y-%m-%d"),
        "argv_tails": [["serve", *wire.BIND_ARGS] for _ in drivers],
        "env_set": sorted(["HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                           "XDG_DATA_HOME", "XDG_STATE_HOME",
                           wire.PASSWORD_ENV]),
        "model": RECORDER_MODEL,
        "error": error or None,
        "notes": red.value(ctx) or None,
    }]
    for i, d in enumerate(drivers):
        for e in d.log:
            lines.append({"proc": i, **red.value(reduce(e))})
        lines.append({"proc": i, "exit": codes[i]})
    body = "".join(json.dumps(line, ensure_ascii=False) + "\n"
                   for line in lines)
    leaks = red.leaks(body)
    if leaks:
        raise SystemExit(f"{scenario}: refusing to write, the fixture still "
                         f"holds {sorted(set(leaks))}")
    out = rp.FIXTURES / ADAPTER / version / f"{scenario}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    shutil.rmtree(root, ignore_errors=True)
    rp.golden(out)
    print(f"{scenario}: {len(lines) - 1} lines, exit {codes}"
          + (f", stopped early: {error}" if error else ""))
    return out
