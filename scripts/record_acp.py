"""The ``acp`` scenarios of ``record_protocol.py``.

Run through the recorder, never directly::

    python3 scripts/record_protocol.py record --adapter=acp \\
        --scenario=hello [--cli=/path/to/qwen] [--raw-dir=/scratch/dir]

One adapter drives every ACP agent, so a fixture says which agent it
came from: it is written under
``tests/fixtures/protocols/acp/<agent>-<version>/``. The agent is
whatever ``--cli`` names (default ``qwen``), started with the argv tail
``ACP_ARGS`` lists for it.

Every scenario runs the real agent in a fresh ``mktemp -d`` tree: a
work folder with two dummy files and a throwaway ``HOME``, so nothing
of the owner's (settings, memory files, MCP servers, sessions, logins)
is read or written. The child gets ``OPENAI_API_KEY`` from the
environment plus ``OPENAI_BASE_URL`` and ``OPENAI_MODEL``
(``RECORDER_MODEL``), the only credential it finds there.

Before a fixture is written, a ``session/update`` the adapter does not
read is cut down to its name, every other notification to its method,
``_meta`` (execution ids, provider details) is dropped except a
chunk's ``usage``, and the handshake answers to the fields the adapter
reads. The shared ``Redactor`` then replaces paths, ids, the user and
host names, and refuses to write anything that still holds one.

An agent that is not installed cannot be recorded. Its fixtures are
written by hand from the protocol's schema and marked
``"synthetic": true`` (``tests/fixtures/protocols/acp/*-synthetic/``).
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import record_protocol as rp
from swe.remote import acp as wire
from swe.remote.adapters import acp as adapter

replay = adapter.replay

RECORDER_MODEL = "gpt-4o-mini"
ADAPTER = "acp"

# The argv tail that starts each agent's ACP mode, by binary name.
ACP_ARGS = {"qwen": ["--acp"], "gemini": ["--acp"], "kimi": ["acp"]}

_ENV_KEEP = ("PATH", "TMPDIR", "LANG", "LC_ALL", "OPENAI_API_KEY")
_SECRET_ENV = ("OPENAI_API_KEY",)
_ID_PREFIXES = ("call", "msg", "chatcmpl", "resp", "fc")
_NO_METHOD = {"code": -32601, "message": "not supported"}
_COUNT = ("Count slowly from 1 to 60, one number per line, with a short "
          "sentence about each number.")


def default_cli() -> str:
    return shutil.which("qwen") or "qwen"


class AcpDriver(rp.Driver):
    """One ACP agent child, driven as JSON-RPC."""

    def __init__(self, argv, cwd, env) -> None:
        super().__init__(argv, cwd, env)
        self._rpc = 0
        self._cwd = cwd
        self.session = ""
        self.info: dict = {}

    def call(self, method: str, params: dict, timeout: float = 90.0) -> dict:
        self._rpc += 1
        rpc_id = self._rpc
        start = self.mark()
        self.send({"jsonrpc": "2.0", "id": rpc_id, "method": method,
                   "params": params})
        return self.wait(lambda m: m.get("id") == rpc_id
                         and "method" not in m, timeout, start,
                         f"{method} answer")

    def ok(self, method: str, params: dict, timeout: float = 90.0) -> dict:
        answer = self.call(method, params, timeout)
        if "error" in answer:
            raise rp.ScenarioError(f"{method}: {answer['error']}")
        return answer.get("result") or {}

    def open(self, load: str = "") -> str:
        """``initialize`` then ``session/new`` (or ``session/load``)."""
        self.ok("initialize", {
            "protocolVersion": wire.PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False},
            "clientInfo": {"name": "swe", "version": "0.0.0"}})
        cwd = os.path.realpath(self._cwd)
        if load:
            self.info = self.ok("session/load", {
                "sessionId": load, "cwd": cwd, "mcpServers": []}, 120.0)
            self.session = load
        else:
            self.info = self.ok("session/new", {"cwd": cwd,
                                                "mcpServers": []})
            self.session = self.info["sessionId"]
        return self.session

    def set_mode(self, mode: str) -> dict:
        return self.call("session/set_mode", {"sessionId": self.session,
                                              "modeId": mode})

    def start_turn(self, text: str, blocks=()) -> int:
        self._rpc += 1
        self.send({"jsonrpc": "2.0", "id": self._rpc,
                   "method": "session/prompt",
                   "params": {"sessionId": self.session, "prompt": [
                       {"type": "text", "text": text}, *blocks]}})
        return self._rpc

    def until_done(self, rpc_id: int, start: int, *, answer="allow_once",
                   timeout: float = 150.0, handled=()) -> dict:
        """Wait for the prompt's answer. Each permission request is
        answered with the option of kind ``answer`` (or what a callable
        returns: an option kind, or None to leave it open); any other
        request from the agent gets "method not found", as the adapter
        answers it."""
        deadline = time.monotonic() + timeout
        handled = set(handled)
        while True:
            msg = self.wait(
                lambda m: (m.get("id") == rpc_id and "method" not in m) or (
                    "method" in m and "id" in m
                    and m["id"] not in handled),
                max(0.1, deadline - time.monotonic()), start,
                "the prompt's answer")
            if "method" not in msg:
                return msg
            handled.add(msg["id"])
            if msg["method"] != "session/request_permission":
                self.send({"jsonrpc": "2.0", "id": msg["id"],
                           "error": _NO_METHOD})
                continue
            kind = answer(msg) if callable(answer) else answer
            if kind is None:
                continue
            self.answer(msg, kind)

    def answer(self, request: dict, kind: str) -> None:
        options = request["params"]["options"]
        option = next((o for o in options if o.get("kind") == kind), None)
        if option is None:
            raise rp.ScenarioError(
                f"no {kind} option in {[o.get('kind') for o in options]}")
        self.send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "outcome": {"outcome": "selected",
                        "optionId": option["optionId"]}}})

    def turn(self, text: str, *, answer="allow_once", blocks=(),
             timeout: float = 150.0) -> dict:
        start = self.mark()
        rpc_id = self.start_turn(text, blocks)
        return self.until_done(rpc_id, start, answer=answer, timeout=timeout)

    def wait_chunks(self, start: int, n: int = 1,
                    timeout: float = 90.0) -> None:
        """Wait until ``n`` reply chunks with text have come."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                got = sum(1 for e in self.log[start:] if e["dir"] == "in"
                          and _chunk_text(e.get("msg")))
                if got >= n:
                    return
                left = deadline - time.monotonic()
                if self.closed or left <= 0:
                    raise rp.ScenarioError(f"no {n} reply chunks")
                self.cond.wait(left)


def _chunk_text(msg) -> str:
    if not isinstance(msg, dict) or msg.get("method") != "session/update":
        return ""
    update = (msg.get("params") or {}).get("update") or {}
    if update.get("sessionUpdate") != "agent_message_chunk":
        return ""
    return (update.get("content") or {}).get("text") or ""


def _stop(answer: dict) -> str:
    if "error" in answer:
        return "error"
    return (answer.get("result") or {}).get("stopReason") or ""


# ---------------------------------------------------------------------
# Scenarios. Each gets a driver factory, the work folder and a notes dict.


def sc_hello(spawn, work, ctx):
    d = spawn()
    d.open()
    ctx["modes"] = adapter.mode_ids(d.info)
    ctx["mode_at_start"] = adapter.current_mode(d.info)
    ctx["models_listed"] = len(set(adapter.model_ids(d.info).values()))
    ctx["stop"] = _stop(d.turn(
        "Reply with exactly these words and nothing else: hello there"))


def sc_approval(spawn, work, ctx):
    d = spawn()
    d.open()
    d.ok("session/set_mode", {"sessionId": d.session, "modeId": "default"})
    ctx["stops"] = [
        _stop(d.turn("Run the shell command `touch allowed.txt` in the "
                     "current folder, then reply with just: done")),
        _stop(d.turn("Run the shell command `touch denied.txt`. If you "
                     "are not allowed, reply only with: denied",
                     answer="reject_once")),
        _stop(d.turn("Create a file named written.txt containing the "
                     "single word yes, using your file writing tool. Then "
                     "reply with just: done"))]
    ctx["files"] = sorted(os.listdir(work))


def sc_always(spawn, work, ctx):
    """``allow_always``: is the next same command asked again?"""
    d = spawn()
    d.open()
    d.ok("session/set_mode", {"sessionId": d.session, "modeId": "default"})
    asked = []

    def answer(msg):
        asked.append(sorted(o.get("kind") for o in
                            msg["params"]["options"]))
        return "allow_always"

    d.turn("Run the shell command `touch one.txt`, then reply with just: "
           "done", answer=answer)
    first = len(asked)
    d.turn("Run the shell command `touch two.txt`, then reply with just: "
           "done", answer=answer)
    ctx["approvals_first_turn"] = first
    ctx["approvals_second_turn"] = len(asked) - first
    ctx["option_kinds"] = asked[0] if asked else []
    ctx["files"] = sorted(os.listdir(work))
    ctx["settings_written"] = sorted(
        str(p.relative_to(ctx["home"])) for p in Path(ctx["home"]).rglob(
            "settings.json"))


def sc_interrupt(spawn, work, ctx):
    d = spawn()
    d.open()
    start = d.mark()
    rpc_id = d.start_turn(_COUNT)
    d.wait_chunks(start, 8)
    d.send({"jsonrpc": "2.0", "method": "session/cancel",
            "params": {"sessionId": d.session}})
    ctx["stop"] = _stop(d.until_done(rpc_id, start))


def sc_cancel_approval(spawn, work, ctx):
    """A cancel while a permission request is open: we answer it
    ``cancelled``, as the protocol says."""
    d = spawn()
    d.open()
    d.ok("session/set_mode", {"sessionId": d.session, "modeId": "default"})
    start = d.mark()
    rpc_id = d.start_turn("Run the shell command `touch asked.txt` in the "
                          "current folder, then reply with just: done")
    asked = d.wait(lambda m: m.get("method") == "session/request_permission",
                   90.0, start, "a permission request")
    d.send({"jsonrpc": "2.0", "method": "session/cancel",
            "params": {"sessionId": d.session}})
    d.send({"jsonrpc": "2.0", "id": asked["id"],
            "result": {"outcome": {"outcome": "cancelled"}}})
    ctx["stop"] = _stop(d.until_done(rpc_id, start, handled=[asked["id"]]))
    ctx["asked_file_exists"] = os.path.exists(os.path.join(work, "asked.txt"))


def sc_resume(spawn, work, ctx):
    d1 = spawn()
    session = d1.open()
    d1.turn("Remember this word: papaya. Reply only with: ok")
    d1.finish()
    d2 = spawn()
    d2.open(load=session)
    ctx["replayed_updates"] = sum(
        1 for e in d2.log if e["dir"] == "in"
        and (e.get("msg") or {}).get("method") == "session/update")
    ctx["stop"] = _stop(d2.turn(
        "Which word did I ask you to remember? Reply with just the word."))


def sc_plan_mode(spawn, work, ctx):
    """The ``plan`` mode and back."""
    d = spawn()
    d.open()
    before = adapter.current_mode(d.info)
    ctx["set_plan"] = "error" not in d.set_mode("plan")
    d.turn("Create a file named planned.txt containing the word no. If you "
           "cannot because you are only planning, reply only with: planning",
           answer="reject_once")
    ctx["file_while_planning"] = os.path.exists(
        os.path.join(work, "planned.txt"))
    ctx["set_back"] = "error" not in d.set_mode(before)
    d.turn("Create a file named built.txt containing the word yes, then "
           "reply only with: built")
    ctx["file_after"] = os.path.exists(os.path.join(work, "built.txt"))
    ctx["bad_mode_refused"] = "error" in d.set_mode("no-such-mode")


def sc_model(spawn, work, ctx):
    """``session/set_model`` with a model the agent listed, and with one
    it did not."""
    d = spawn()
    d.open()
    listed = list(adapter.model_ids(d.info).values())
    ctx["models_listed"] = len(set(listed))
    answer = d.call("session/set_model", {"sessionId": d.session,
                                          "modelId": listed[0]})
    ctx["listed_model_accepted"] = "error" not in answer
    bad = d.call("session/set_model", {"sessionId": d.session,
                                       "modelId": "no-such-model"})
    ctx["unlisted_model_refused"] = "error" in bad
    ctx["stop"] = _stop(d.turn("Reply only with: ok"))


def sc_attach(spawn, work, ctx):
    """A file attached as a ``resource_link``."""
    d = spawn()
    d.open()
    ctx["stop"] = _stop(d.turn(
        "What is the first line of the attached file? Reply with just that "
        "line.", blocks=adapter.resource_links(
            [os.path.join(os.path.realpath(work), "notes.txt")])))


def _mid_prompt(d, method: str, params: dict, ctx: dict) -> None:
    """Send ``method`` once the reply streams, and note what the prompt
    in progress did (G32): the answer, the chunks after it, how the
    prompt ended."""
    start = d.mark()
    rpc_id = d.start_turn(_COUNT)
    d.wait_chunks(start, 8)
    before = sum(1 for e in d.log[start:] if e["dir"] == "in"
                 and _chunk_text(e.get("msg")))
    answer = d.call(method, {"sessionId": d.session, **params})
    ctx["answered"] = "error" not in answer
    done = d.until_done(rpc_id, start)
    ctx["stop"] = _stop(done)
    ctx["chunks_before_change"] = before
    ctx["chunks_after_change"] = sum(
        1 for e in d.log[start:] if e["dir"] == "in"
        and _chunk_text(e.get("msg"))) - before


def sc_model_mid(spawn, work, ctx):
    """``session/set_model`` during an open ``session/prompt``."""
    d = spawn()
    d.open()
    listed = list(adapter.model_ids(d.info).values())
    _mid_prompt(d, "session/set_model", {"modelId": listed[0]}, ctx)


def sc_plan_mode_mid(spawn, work, ctx):
    """``session/set_mode`` plan during an open ``session/prompt``, then
    back."""
    d = spawn()
    d.open()
    before = adapter.current_mode(d.info)
    _mid_prompt(d, "session/set_mode", {"modeId": "plan"}, ctx)
    d.set_mode(before or "default")
    ctx["stop_after"] = _stop(d.turn("Reply with exactly: back"))


def sc_error(spawn, work, ctx):
    """A turn that fails: a model the provider does not have."""
    d = spawn(OPENAI_MODEL="no-such-model-for-the-recorder")
    d.open()
    ctx["stop"] = _stop(d.turn("Reply with: ok", timeout=90.0))


SCENARIOS = {
    "hello": sc_hello,
    "approval": sc_approval,
    "always": sc_always,
    "interrupt": sc_interrupt,
    "cancel_approval": sc_cancel_approval,
    "resume": sc_resume,
    "plan_mode": sc_plan_mode,
    "model": sc_model,
    "model_mid": sc_model_mid,
    "plan_mode_mid": sc_plan_mode_mid,
    "attach": sc_attach,
    "error": sc_error,
}


# ---------------------------------------------------------------------
# Cutting a message down to what the adapter reads

_INIT_KEEP = ("protocolVersion", "agentInfo", "agentCapabilities")
_SESSION_KEEP = ("sessionId", "modes", "models")


def _strip_meta(value, keep_usage=False):
    if isinstance(value, list):
        return [_strip_meta(v) for v in value]
    if isinstance(value, dict):
        out = {k: _strip_meta(v) for k, v in value.items() if k != "_meta"}
        meta = value.get("_meta")
        if keep_usage and isinstance(meta, dict) \
                and isinstance(meta.get("usage"), dict):
            out["_meta"] = {"usage": meta["usage"]}
        return out
    return value


def reduce(msg):
    if not isinstance(msg, dict):
        return msg
    method = msg.get("method")
    if method == "session/update":
        params = msg.get("params") or {}
        update = params.get("update") or {}
        name = update.get("sessionUpdate")
        if name not in adapter.READ_UPDATES:
            return {"jsonrpc": "2.0", "method": method, "params": {
                "sessionId": params.get("sessionId"),
                "update": {"sessionUpdate": name}}}
        return {"jsonrpc": "2.0", "method": method, "params": {
            "sessionId": params.get("sessionId"),
            "update": _strip_meta(update, keep_usage=True)}}
    if method == "session/request_permission":
        return _strip_meta(msg)
    if isinstance(method, str):
        # Another notification, or a request we answer "not supported".
        return {k: msg[k] for k in ("jsonrpc", "id", "method") if k in msg}
    body = msg.get("result")
    if isinstance(body, dict):
        if "agentInfo" in body or "agentCapabilities" in body:
            body = {k: body[k] for k in _INIT_KEEP if k in body}
        elif "sessionId" in body or "modes" in body or "models" in body:
            body = {k: body[k] for k in _SESSION_KEEP if k in body}
        return {**msg, "result": _strip_meta(body)}
    return _strip_meta(msg)


def record(scenario: str, version: str, raw_dir: Path | None,
           cli: str) -> Path:
    fn = SCENARIOS[scenario]
    binary = os.path.basename(cli)
    tail = ACP_ARGS.get(binary)
    if tail is None:
        raise SystemExit(f"no ACP argv known for {binary}: add it to "
                         "ACP_ARGS")
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
    env.update(HOME=home, OPENAI_BASE_URL="https://api.openai.com/v1",
               OPENAI_MODEL=RECORDER_MODEL)
    drivers: list[AcpDriver] = []
    ctx: dict = {"home": home}

    def spawn(**extra):
        d = AcpDriver([cli, *tail], work, {**env, **extra})
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
    ctx.pop("home", None)
    agent = "agent"
    for d in drivers:
        for e in d.log:
            info = ((e.get("msg") or {}).get("result") or {}).get(
                "agentInfo") if e["dir"] == "in" and isinstance(
                    (e.get("msg") or {}).get("result"), dict) else None
            if isinstance(info, dict) and info.get("name"):
                agent = info["name"]
    host = socket.gethostname()
    red = rp.Redactor(
        work, config=home, home=os.path.expanduser("~"),
        user=getpass.getuser(),
        secrets=[os.environ.get(k, "") for k in _SECRET_ENV], deny=[],
        hosts=[host, host.split(".")[0]], id_prefixes=_ID_PREFIXES)
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        with open(raw_dir / f"{scenario}.raw.jsonl", "w") as fh:
            for i, d in enumerate(drivers):
                for e in d.log:
                    fh.write(json.dumps({"proc": i, **e}) + "\n")
                fh.write(json.dumps({"proc": i, "stderr": d.stderr}) + "\n")
    folder = f"{agent}-{version}"
    lines = [{
        "fixture": 1, "adapter": ADAPTER, "agent": agent,
        "version": folder, "scenario": scenario, "synthetic": False,
        "recorded": time.strftime("%Y-%m-%d"),
        "argv_tails": [list(tail) for _ in drivers],
        "env_set": ["HOME", "OPENAI_BASE_URL", "OPENAI_MODEL"],
        "model": RECORDER_MODEL,
        "error": error or None,
        "notes": red.value(ctx) or None,
    }]
    for i, d in enumerate(drivers):
        for e in d.log:
            entry = {"proc": i, "dir": e["dir"], "t": e["t"]}
            if "msg" in e:
                entry["msg"] = red.value(reduce(e["msg"]) if e["dir"] == "in"
                                         else e["msg"])
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
    out = rp.FIXTURES / ADAPTER / folder / f"{scenario}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    shutil.rmtree(root, ignore_errors=True)
    rp.golden(out)
    print(f"{scenario}: {len(lines) - 1} lines, exit {codes}"
          + (f", stopped early: {error}" if error else ""))
    return out
