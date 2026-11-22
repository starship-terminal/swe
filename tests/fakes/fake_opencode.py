"""A fake ``opencode serve``: the 2.x HTTP API on a loopback port.

Started by tests/test_remote_adapters.py as ``[sys.executable, this
file, --hostname, 127.0.0.1, --port, 0]``. It answers the way the
recordings under tests/fixtures/protocols/opencode_serve/ show the real
CLI does, closely enough for the adapter contract: the "listening"
line, Basic auth, ``GET /api/info``, the ``GET /api/event`` stream,
sessions (create, get, fork), a turn with streamed text, permissions
and forms that wait for our answer, steer, interrupt, ``/model``,
``/agent``, the two revert requests and the failures.

Environment:

FAKE_OPENCODE_MODE
    ``ok`` (default), ``approval`` (each turn asks to run a command
    first), ``patch`` (asks to edit a file), ``question`` (a form with
    one text field and two choices), ``form-many`` (a form with two
    fields, which the adapter cancels), ``slow`` (a turn streams once,
    then waits for an interrupt or a steer), ``crash`` (exits
    mid-turn), ``exit-at-start`` (exits before it listens), ``silent``
    (never says it listens), ``old`` (reports version 1.0.0),
    ``refuse-session`` (answers ``POST /api/session`` with 400),
    ``refuse-prompt``, ``hang-prompt`` (never answers a prompt),
    ``refuse-reply`` (answers a permission reply with 400),
    ``refuse-interrupt``, ``refuse-revert``, ``refuse-model``, ``fail``
    (the turn fails), ``noise`` (unknown events and another session's
    events around a turn), ``overtake`` (the turn's events come before
    the prompt's answer), ``linger`` (ignores SIGTERM), ``drop-stream``
    (closes the event stream after the first turn), ``no-stream``
    (answers ``GET /api/event`` with 500), ``secret`` (the reply, a
    tool's output and a permission's command hold an API key),
    ``password`` (the reply prints the server's own password, as a
    command the agent ran could), ``refuse-commit`` (takes a staged
    revert, refuses its commit), ``refuse-agent``, ``deaf-interrupt``
    (answers an interrupt with ``interrupted: false`` and goes on),
    ``empty-end`` (ends its streamed reply with no text), ``child``
    (starts a child process that inherits its stdout and ignores
    SIGTERM, as an npm-style launcher whose server is slow to stop).

    A prompt that starts with ``mode:<name> `` runs that turn in mode
    ``<name>``, whatever the environment says.

FAKE_OPENCODE_REPLY
    The assistant's reply text (default ``hello``).
FAKE_OPENCODE_STEER_REPLY
    The reply to a steer (default ``steered``).
FAKE_OPENCODE_PARTIAL
    The text ``slow`` streams before it waits (default ``1\\n2\\n``).
FAKE_OPENCODE_LOG
    A file every request we receive is appended to (JSON lines); the
    first line holds the argv and whether a password was set.
"""

import base64
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("FAKE_OPENCODE_MODE", "ok")
REPLY = os.environ.get("FAKE_OPENCODE_REPLY", "hello")
STEER_REPLY = os.environ.get("FAKE_OPENCODE_STEER_REPLY", "steered")
PARTIAL = os.environ.get("FAKE_OPENCODE_PARTIAL", "1\n2\n")
LOG = os.environ.get("FAKE_OPENCODE_LOG")
PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
VERSION = "1.0.0" if MODE == "old" else "2.3.2"

SESSION = "ses_fake00000000000000000001"
FORKED = "ses_fake00000000000000000002"
MISSING = "ses_missing0000000000000001"
OTHER = "ses_other000000000000000001"
# What ``secret`` leaks; the adapter must mask it.
KEY = "sk-ant-api03-" + "d" * 40
WAIT_S = 60.0

lock = threading.RLock()
log_lock = threading.Lock()
subscribers: list = []
signals: "queue.Queue" = queue.Queue()
state = {"session": "", "n": {}, "running": False, "inbox": [],
         "model": {"providerID": "fake", "id": "model-1"}, "agent": "build",
         "pending": "", "turns": 0, "staged": "", "reverted": []}


def log(obj):
    if LOG:
        with log_lock, open(LOG, "a") as fh:
            fh.write(json.dumps(obj) + "\n")


def next_id(prefix):
    with lock:
        state["n"][prefix] = state["n"].get(prefix, 0) + 1
        return f"{prefix}_{state['n'][prefix]:04d}fake0000000000"


def emit(etype, data=None, session=None):
    """One event to every open stream."""
    event = {"type": etype}
    if data is not None or session is not False:
        body = dict(data or {})
        if session is not False:
            body = {"sessionID": session or state["session"], **body}
        event["data"] = body
    with lock:
        for sub in subscribers:
            sub.put(event)


# ---------------------------------------------------------------------
# A turn


def say(message, text, ordinal=0, whole=True):
    emit("session.text.started", {"assistantMessageID": message})
    half = max(1, len(text) // 2)
    for piece in (text[:half], text[half:]) if whole else (text,):
        if piece:
            emit("session.text.delta", {"assistantMessageID": message,
                                        "ordinal": ordinal, "delta": piece})
    if whole:
        emit("session.text.ended", {"assistantMessageID": message,
                                    "ordinal": ordinal, "text": text})


def step_start():
    message = next_id("msg")
    emit("session.step.started", {"agent": state["agent"],
                                  "model": dict(state["model"]),
                                  "assistantMessageID": message})
    return message


def step_end(message, finish="stop"):
    tokens = {"input": 100, "output": 5, "reasoning": 0,
              "cache": {"read": 40, "write": 0}}
    emit("session.step.ended", {"assistantMessageID": message,
                                "finish": finish, "tokens": tokens,
                                "cost": 0.0001})
    emit("session.usage.updated", {"cost": 0.0001 * (state["turns"] + 1),
                                   "tokens": {"input": 1000, "output": 50,
                                              "reasoning": 0,
                                              "cache": {"read": 400,
                                                        "write": 0}}})


def step_abort(message):
    emit("session.step.failed", {
        "assistantMessageID": message,
        "error": {"type": "aborted", "message": "Step interrupted"}})


def end(etype, **data):
    with lock:
        state["running"] = False
        state["pending"] = ""
        state["turns"] += 1
    emit(etype, data)


def deliver_waiting(message):
    """Deliver the steers in the inbox to the step that starts now."""
    with lock:
        waiting, state["inbox"] = state["inbox"], []
    for inbox in waiting:
        emit("session.inbox.delivered", {"inboxID": inbox})
    return bool(waiting)


def take_signal():
    try:
        return signals.get(timeout=WAIT_S)
    except queue.Empty:
        return ("timeout",)


def run_turn(message_id, text, mode, started=None):
    """One execution: ``message_id`` is the prompt's inbox id."""
    emit("session.inbox.enqueued")
    emit("session.execution.started")
    emit("session.inbox.delivered", {"inboxID": message_id})
    if mode == "noise":
        emit("session.totally.new", {"anything": 1})
        emit("session.text.delta", {"assistantMessageID": "msg_x",
                                    "ordinal": 0, "delta": "not ours"},
             session=OTHER)
        emit("project.updated", session=False)
        for sub in list(subscribers):
            sub.put("raw:data: not json\n\n")
    if mode == "fail":
        failure = {"type": "provider.no-route",
                   "message": "Model unavailable: fake/none"}
        emit("session.step.failed", {"error": failure})
        end("session.execution.failed", error=failure)
        return
    message = step_start()
    if started is not None:
        emit("session.text.started", {"assistantMessageID": message})
        emit("session.text.delta", {"assistantMessageID": message,
                                    "ordinal": 0, "delta": "early "})
        started.set()
    if mode == "crash":
        say(message, PARTIAL, whole=False)
        os._exit(3)
    if mode in ("approval", "patch", "secret"):
        if not ask_permission(message, mode):
            return
        step_end(message, "tool-calls")
        message = step_start()
    elif mode in ("question", "form-many"):
        ask_form(mode)
    elif mode == "slow":
        if not slow(message):
            return
        message = step_start()
        deliver_waiting(message)
        say(message, STEER_REPLY)
        step_end(message)
        end("session.execution.succeeded")
        return
    reply = REPLY
    if state["agent"] == "plan":
        reply = "planning"
    if mode == "secret":
        reply = f"the key is {KEY}"
    if mode == "password":
        reply = f"it prints {PASSWORD} and\nPW={PASSWORD}\n"
    if mode == "empty-end":
        say(message, reply + "\n", whole=False)
        emit("session.text.ended", {"assistantMessageID": message,
                                    "ordinal": 0, "text": ""})
    elif started is not None:
        emit("session.text.delta", {"assistantMessageID": message,
                                    "ordinal": 0, "delta": reply})
        emit("session.text.ended", {"assistantMessageID": message,
                                    "ordinal": 0, "text": "early " + reply})
    else:
        say(message, reply)
    step_end(message)
    if mode == "noise":
        emit("session.renamed")
    end("session.execution.succeeded")
    with lock:
        waiting = bool(state["inbox"])
    if waiting:
        answer_inbox()


def answer_inbox():
    """A new execution for the steers the last one never reached."""
    with lock:
        state["running"] = True
    emit("session.execution.started")
    message = step_start()
    deliver_waiting(message)
    say(message, STEER_REPLY)
    step_end(message)
    end("session.execution.succeeded")


def ask_permission(message, mode):
    """A tool call that asks first; False when the turn ended here."""
    call = next_id("call")
    request = next_id("per")
    command = f"echo {KEY}" if mode == "secret" else "touch probe.txt"
    patch = mode == "patch"
    emit("session.tool.input.started", {
        "assistantMessageID": message, "id": call,
        "name": "patch" if patch else "shell"})
    emit("session.tool.called", {
        "assistantMessageID": message, "id": call, "executed": False,
        "input": {"patchText": "*** Add File: probe.txt\n+yes"} if patch
        else {"command": command, "timeout": 120000}})
    files = [{"file": "probe.txt", "status": "added", "additions": 1,
              "deletions": 0,
              "patch": "Index: probe.txt\n=====\n--- probe.txt\n"
                       "+++ probe.txt\n@@ -0,0 +1,1 @@\n+yes\n"}]
    asked = {"id": request, "action": "edit" if patch else "shell",
             "resources": ["probe.txt"] if patch else [command],
             "save": ["*"],
             "source": {"type": "tool", "messageID": message, "id": call}}
    if patch:
        asked["metadata"] = {"filepath": "probe.txt", "files": files}
    with lock:
        state["pending"] = request
    emit("permission.asked", asked)
    while True:
        got = take_signal()
        if got[0] == "steer":
            continue  # stays in the inbox while the request is open
        break
    with lock:
        state["pending"] = ""
    if got[0] == "reply" and got[2] in ("once", "always"):
        emit("permission.replied", {"requestID": request, "reply": got[2]})
        output = KEY if mode == "secret" else "(no output)"
        emit("session.tool.success", {
            "assistantMessageID": message, "id": call, "executed": False,
            "content": [{"type": "text", "text": output}],
            "metadata": {"files": files} if patch else {"exit": 0}})
        return True
    if got[0] == "reply" and got[3]:
        emit("permission.replied", {"requestID": request, "reply": "reject"})
        emit("session.tool.failed", {
            "assistantMessageID": message, "id": call, "executed": False,
            "error": {"type": "permission.rejected", "message": got[3]}})
        return True
    if got[0] == "reply":
        emit("permission.replied", {"requestID": request, "reply": "reject"})
        emit("session.tool.failed", {
            "assistantMessageID": message, "id": call, "executed": False,
            "error": {"type": "aborted",
                      "message": "The user declined this tool call"}})
        step_abort(message)
        end("session.execution.interrupted", reason="shutdown")
        with lock:
            waiting = bool(state["inbox"])
        if waiting:
            answer_inbox()
        return False
    # An interrupt (or nobody answered): the steers stay in the inbox.
    emit("session.tool.failed", {
        "assistantMessageID": message, "id": call, "executed": False,
        "error": {"type": "tool.execution",
                  "message": "Command interrupted by the user before it "
                             "started."}})
    step_abort(message)
    end("session.execution.interrupted", reason="user")
    return False


def ask_form(mode):
    form_id = next_id("frm")
    fields = [{"key": "answer", "type": "string", "title": "Which one?",
               "options": [{"value": "red", "label": "Red"},
                           {"value": KEY, "label": f"Blue {KEY}"}]}]
    if mode == "form-many":
        fields.append({"key": "second", "type": "string", "title": "And?"})
    with lock:
        state["pending"] = form_id
    emit("form.created", {"form": {"id": form_id,
                                   "sessionID": state["session"],
                                   "title": "A question",
                                   "fields": fields}}, session=False)
    got = take_signal()
    with lock:
        state["pending"] = ""
    emit("form.replied" if got[0] == "form" else "form.cancelled",
         {"id": form_id}, session=False)


def slow(message):
    """Stream once, then wait; False when an interrupt ended the turn."""
    say(message, PARTIAL, whole=False)
    got = take_signal()
    emit("session.text.ended", {"assistantMessageID": message, "ordinal": 0,
                                "text": PARTIAL})
    if got[0] == "steer":
        step_end(message)
        return True
    step_abort(message)
    end("session.execution.interrupted", reason="user")
    return False


# ---------------------------------------------------------------------
# HTTP


def mode_of(text):
    if text.startswith("mode:"):
        name, _, rest = text[5:].partition(" ")
        return name, rest
    return MODE, text


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # noqa: D401 - quiet
        pass

    def answer(self, status, body=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def refuse(self, status=400, tag="BadRequest", message="no"):
        self.answer(status, {"_tag": tag, "message": message})

    def authed(self):
        want = "Basic " + base64.b64encode(
            f"opencode:{PASSWORD}".encode()).decode()
        return self.headers.get("Authorization") == want

    def body(self):
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size) if size else b""
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def handle_any(self):
        body = self.body()
        ok = self.authed()
        if self.path != "/api/event":
            log({"method": self.command, "path": self.path, "body": body,
                 "authed": ok})
        if not ok:
            return self.refuse(401, "Unauthorized", "unauthorized")
        parts = self.path.strip("/").split("/")
        if self.path == "/api/info":
            return self.answer(200, {"version": VERSION, "pid": os.getpid(),
                                     "hostname": "fake-host"})
        if self.path == "/api/event":
            return self.events()
        if self.path == "/api/model" and self.command == "GET":
            # The server's model list (G32): ``models`` lists two,
            # ``slow-models`` answers 1.5 s later, after the start.
            if MODE == "slow-models":
                time.sleep(1.5)
            if MODE in ("models", "slow-models"):
                return self.answer(200, {"data": [
                    {"id": "model-1", "providerID": "fake", "name": "One"},
                    {"id": "model-2", "providerID": "fake", "name": "Two"}]})
        if self.path == "/api/session" and self.command == "POST":
            return self.create(body or {})
        if parts[:2] == ["api", "session"] and len(parts) >= 3:
            return self.session(parts[2], parts[3:], body or {})
        return self.refuse(404, "NotFound", "no such route")

    do_GET = do_POST = do_DELETE = handle_any

    def events(self):
        if MODE == "no-stream":
            return self.refuse(500, "Internal", "no stream")
        mine: "queue.Queue" = queue.Queue()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with lock:
            subscribers.append(mine)
        mine.put({"type": "server.connected", "data": {}})
        try:
            while True:
                event = mine.get()
                if event is None:
                    return
                if isinstance(event, str):
                    self.wfile.write(event[4:].encode())
                else:
                    self.wfile.write(
                        b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.flush()
                if MODE == "drop-stream" and isinstance(event, dict) \
                        and event["type"] == "session.execution.succeeded":
                    return
        except OSError:
            return
        finally:
            with lock:
                if mine in subscribers:
                    subscribers.remove(mine)

    def session_object(self, **extra):
        return {"data": {"id": state["session"], "projectID": "p" * 40,
                         "model": dict(state["model"]),
                         "agent": state["agent"], **extra}}

    def create(self, body):
        if MODE == "refuse-session":
            return self.refuse()
        with lock:
            state["session"] = body.get("id") or SESSION
            if isinstance(body.get("model"), dict):
                state["model"] = dict(body["model"])
        self.answer(200, self.session_object())
        emit("session.created")

    def session(self, session_id, tail, body):
        if session_id == MISSING:
            return self.answer(404, {"_tag": "SessionNotFound",
                                     "sessionID": session_id,
                                     "message": "Session not found"})
        if not tail and self.command == "GET":
            with lock:
                state["session"] = session_id
            return self.answer(200, self.session_object())
        if tail == ["fork"]:
            with lock:
                state["session"] = FORKED
            return self.answer(200, self.session_object(
                fork={"sessionID": session_id}))
        if session_id != state["session"]:
            return self.refuse(404, "SessionNotFound", "Session not found")
        if tail == ["prompt"]:
            return self.prompt(body)
        if tail == ["interrupt"]:
            if MODE == "refuse-interrupt":
                return self.refuse()
            if MODE == "deaf-interrupt":
                return self.answer(200, {"interrupted": False})
            with lock:
                running = state["running"]
            if running:
                signals.put(("interrupt",))
            return self.answer(200, {"interrupted": running})
        if len(tail) == 3 and tail[0] == "permission" and tail[2] == "reply":
            if MODE == "refuse-reply":
                return self.refuse()
            with lock:
                known = tail[1] == state["pending"]
            if not known:
                return self.refuse(404, "PermissionNotFound", "not open")
            signals.put(("reply", tail[1], body.get("decision"),
                         body.get("message") or ""))
            return self.answer(204)
        if len(tail) >= 2 and tail[0] == "form":
            with lock:
                known = tail[1] == state["pending"]
            if not known:
                return self.refuse(404, "FormNotFound", "not open")
            signals.put(("form", body.get("answer"))
                        if self.command == "POST" else ("cancel",))
            return self.answer(204)
        if tail == ["model"]:
            if MODE == "refuse-model":
                return self.refuse(message="Unknown model")
            with lock:
                previous, state["model"] = state["model"], dict(
                    body.get("model") or {})
            if MODE != "silent-model":
                emit("session.model.selected", {
                    "model": dict(state["model"]), "previous": previous})
            return self.answer(204)
        if tail == ["agent"]:
            if MODE == "refuse-agent":
                return self.refuse(message="Unknown agent")
            with lock:
                state["agent"] = body.get("agent") or "build"
            emit("session.agent.selected", {"agent": state["agent"]})
            return self.answer(204)
        if tail == ["revert", "stage"]:
            if MODE == "refuse-revert":
                return self.refuse(message="Message not found")
            with lock:
                state["staged"] = body.get("messageID") or ""
            return self.answer(200, {"data": {"messageID": state["staged"]}})
        if tail == ["revert", "commit"]:
            if MODE == "refuse-commit":
                return self.refuse(message="Nothing to commit")
            with lock:
                state["reverted"].append(state["staged"])
            return self.answer(204)
        if tail == ["revert"] and self.command == "DELETE":
            with lock:
                state["staged"] = ""
            return self.answer(204)
        return self.refuse(404, "NotFound", "no such route")

    def prompt(self, body):
        mode, text = mode_of(str(body.get("text") or ""))
        if mode == "refuse-prompt":
            return self.refuse(message="Prompt refused")
        if mode == "hang-prompt":
            threading.Event().wait(WAIT_S)
            return None
        message_id = next_id("msg")
        steer = body.get("delivery") == "steer"
        with lock:
            running = state["running"]
            if running:
                state["inbox"].append(message_id)
            else:
                state["running"] = True
        answer = {"data": {"id": message_id, "sessionID": state["session"],
                           "type": "user", "payload": {"text": text},
                           "delivery": "steer" if steer else "queue"}}
        if running:
            self.answer(200, answer)
            emit("session.inbox.enqueued")
            if steer:
                signals.put(("steer", message_id))
            return None
        started = threading.Event() if mode == "overtake" else None
        runner = threading.Thread(target=run_turn, daemon=True,
                                  args=(message_id, text, mode, started))
        if started is not None:
            runner.start()
            started.wait(WAIT_S)
            self.answer(200, answer)
        else:
            self.answer(200, answer)
            runner.start()
        return None


def main():
    argv = sys.argv[1:]
    log({"argv": argv, "password_set": bool(PASSWORD),
         "cwd": os.getcwd()})
    if MODE == "exit-at-start":
        sys.stderr.write("fake opencode: cannot start\n")
        return 3
    if MODE == "linger":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    if MODE == "silent":
        threading.Event().wait(WAIT_S)
        return 0
    if MODE == "child":
        # It ignores SIGTERM, as a server slow to stop would.
        subprocess.Popen([sys.executable, "-c", (
            "import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)")])
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    sys.stdout.write("Warning: this line is not the one\n")
    sys.stdout.write("opencode server listening on "
                     f"http://127.0.0.1:{server.server_address[1]}\n")
    sys.stdout.flush()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
