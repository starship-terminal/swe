"""The ``codex_appserver`` scenarios of ``record_protocol.py``.

Run through the recorder, never directly::

    python3 scripts/record_protocol.py record --adapter=codex_appserver \\
        --scenario=hello [--raw-dir=/scratch/dir]

Every scenario runs the real ``codex app-server`` in a fresh
``mktemp -d`` tree: a work folder with two dummy files, a throwaway
``CODEX_HOME`` and a throwaway ``HOME``, so nothing of the owner's
(config, AGENTS.md, plugins, MCP servers, sessions, the ChatGPT login)
is read or written. The throwaway ``config.toml`` names one model
provider that reads ``OPENAI_API_KEY`` from the environment; the key is
passed through the child's environment and never written anywhere.
The model is ``RECORDER_MODEL`` at low effort throughout.

Before a fixture is written, a notification the adapter does not read
is cut down to its method name (account, remote-control, MCP and skill
status carry the machine's names and the account's plan), and a thread
object to the fields the adapter reads. The shared ``Redactor`` then
replaces paths, ids, the user and host names, and refuses to write
anything that still holds one.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import zlib
from pathlib import Path

import record_protocol as rp
from swe.remote.adapters import codex_wire as wire

replay = wire.replay

RECORDER_MODEL = "gpt-5.6-luna"
RECORDER_EFFORT = "low"
ADAPTER = "codex_appserver"

_ENV_KEEP = ("PATH", "TMPDIR", "LANG", "LC_ALL", "OPENAI_API_KEY")
_SECRET_ENV = ("OPENAI_API_KEY",)
_CONFIG = (
    'model_provider = "recorder"\n'
    "[model_providers.recorder]\n"
    'name = "OpenAI"\n'
    'base_url = "https://api.openai.com/v1"\n'
    'env_key = "OPENAI_API_KEY"\n'
    'wire_api = "responses"\n'
)

# What a recording keeps of a thread object and of the answers that
# carry one; the rest (environments, git info, instruction sources,
# plugin ids, nicknames) is dropped.
_THREAD_KEEP = ("id", "sessionId", "path", "cliVersion", "forkedFromId",
                "model", "status", "ephemeral", "preview")
_THREAD_ANSWER_KEEP = ("thread", "model", "approvalPolicy", "sandbox",
                       "reasoningEffort")
_INIT_KEEP = ("userAgent", "codexHome", "platformFamily", "platformOs")
# Id prefixes of the Responses API items Codex passes through.
_ID_PREFIXES = ("msg", "rs", "call", "fc", "ws", "ig", "resp")


def default_cli() -> str:
    return shutil.which("codex") or "codex"


class RpcDriver(rp.Driver):
    """One ``codex app-server`` child, driven as JSON-RPC."""

    def __init__(self, argv, cwd, env) -> None:
        super().__init__(argv, cwd, env)
        self._rpc = 0
        self.thread = ""
        self.native_turn = ""

    def call(self, method: str, params: dict, timeout: float = 90.0) -> dict:
        self._rpc += 1
        return self.call_built(wire.request(self._rpc, method, params),
                               timeout)

    def next_id(self) -> int:
        self._rpc += 1
        return self._rpc

    def call_built(self, message: dict, timeout: float = 90.0) -> dict:
        start = self.mark()
        rpc_id = message["id"]
        self.send(message)
        return self.wait(lambda m: m.get("id") == rpc_id
                         and "method" not in m, timeout, start,
                         f"{message['method']} answer")

    def handshake(self) -> dict:
        hello = self.call_built(wire.initialize(self.next_id(), "0.0.0"))
        self.send(wire.initialized())
        return hello

    def open(self, *, policy: str = "never", sandbox: str = "read-only",
             resume: str = "", fork: bool = False) -> str:
        """``thread/start`` (or resume or fork) with the recorder's
        model; the thread id. ``sandbox`` is recorder-only: the adapter
        leaves the sandbox to the user's config."""
        self.handshake()
        fields = {"cwd": self._cwd, "model": RECORDER_MODEL,
                  "approval_policy": policy}
        if fork:
            message = wire.thread_fork(self.next_id(), resume, **fields)
        elif resume:
            message = wire.thread_resume(self.next_id(), resume, **fields)
        else:
            message = wire.thread_start(self.next_id(), **fields)
        message["params"]["sandbox"] = sandbox
        answer = self.call_built(message)
        if "error" in answer:
            raise rp.ScenarioError(f"{message['method']}: {answer['error']}")
        self.thread = answer["result"]["thread"]["id"]
        return self.thread

    def start_turn(self, text: str, **kw) -> str:
        """Write ``turn/start``; the server's turn id."""
        kw.setdefault("effort", RECORDER_EFFORT)
        answer = self.call_built(wire.turn_start(self.next_id(), self.thread,
                                                 text, **kw))
        if "error" in answer:
            raise rp.ScenarioError(f"turn/start: {answer['error']}")
        self.native_turn = answer["result"]["turn"]["id"]
        return self.native_turn

    def until_done(self, start: int, *, answer="accept",
                   timeout: float = 150.0, handled=()) -> dict:
        """Wait for ``turn/completed``, answering each approval with
        ``answer`` (a decision, or a callable giving the result body);
        ``handled`` names requests the scenario answered itself."""
        deadline = time.monotonic() + timeout
        handled = set(handled)
        while True:
            msg = self.wait(
                lambda m: m.get("method") == "turn/completed" or (
                    "id" in m and "method" in m and m["id"] not in handled),
                max(0.1, deadline - time.monotonic()), start,
                "turn/completed")
            if msg.get("method") == "turn/completed":
                return msg
            handled.add(msg["id"])
            body = answer(msg) if callable(answer) else {"decision": answer}
            self.send(wire.result(msg["id"], body))

    def turn(self, text: str, *, answer="accept", timeout: float = 150.0,
             **kw) -> dict:
        start = self.mark()
        self.start_turn(text, **kw)
        return self.until_done(start, answer=answer, timeout=timeout)

    def wait_delta(self, start: int, n: int = 1,
                   timeout: float = 90.0) -> None:
        """Wait until ``n`` reply deltas have come since ``start``."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                got = sum(1 for e in self.log[start:] if e["dir"] == "in"
                          and e.get("msg", {}).get("method")
                          == "item/agentMessage/delta")
                if got >= n:
                    return
                left = deadline - time.monotonic()
                if self.closed or left <= 0:
                    raise rp.ScenarioError(f"no {n} reply deltas")
                self.cond.wait(left)


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
    d.open(policy="untrusted", sandbox="workspace-write")
    d.turn("Run the shell command `touch allowed.txt` in the current "
           "folder, then reply with just: done", answer="accept")
    d.turn("Run the shell command `touch denied.txt`. If you are not "
           "allowed, reply only with: denied", answer="decline")
    d.turn("Use apply_patch to create a file named patched.txt containing "
           "the single word yes. Then reply with just: done",
           answer="accept")
    ctx["files"] = sorted(os.listdir(work))


def sc_always(spawn, work, ctx):
    """``acceptForSession`` on a file change: is the next one asked?"""
    d = spawn()
    d.open(policy="untrusted", sandbox="workspace-write")
    asked = []

    def answer(msg):
        asked.append(msg.get("method"))
        return {"decision": "acceptForSession"}

    d.turn("Use apply_patch to create a file named first.txt containing "
           "the single word one. Then reply with just: done", answer=answer)
    first = len(asked)
    d.turn("Use apply_patch to create a file named second.txt containing "
           "the single word two. Then reply with just: done", answer=answer)
    ctx["approvals_first_turn"] = first
    ctx["approvals_second_turn"] = len(asked) - first
    ctx["files"] = sorted(os.listdir(work))


def sc_interrupt(spawn, work, ctx):
    d = spawn()
    d.open()
    start = d.mark()
    turn = d.start_turn(_COUNT)
    d.wait_delta(start, 8)
    d.call_built(wire.turn_interrupt(d.next_id(), d.thread, turn))
    d.until_done(start)
    # Idle now: what an interrupt with no turn answers.
    d.call_built(wire.turn_interrupt(d.next_id(), d.thread, turn))


def sc_steer(spawn, work, ctx):
    d = spawn()
    d.open()
    start = d.mark()
    turn = d.start_turn(_COUNT)
    d.wait_delta(start, 8)
    d.call_built(wire.turn_steer(
        d.next_id(), d.thread, turn,
        "Stop counting. Reply only with the word: steered"))
    d.until_done(start)
    # Idle now: what a steer with no turn answers.
    d.call_built(wire.turn_steer(d.next_id(), d.thread, turn, "hello?"))


def sc_steer_tool(spawn, work, ctx):
    """A steer while a command runs: is the command cut?"""
    d = spawn()
    d.open(policy="never", sandbox="workspace-write")
    start = d.mark()
    turn = d.start_turn(
        "Run this exact shell command and nothing else: "
        "sleep 25 && touch late.txt . Then reply with just: finished")
    d.wait(lambda m: m.get("method") == "item/started" and (
        m["params"].get("item") or {}).get("type") == "commandExecution",
        90.0, start, "the command to start")
    began = time.monotonic()
    time.sleep(2.0)
    d.call_built(wire.turn_steer(
        d.next_id(), d.thread, turn,
        "Never mind the command. Reply only with the word: steered"))
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
    """A steer while an approval is open: is the request withdrawn?"""
    d = spawn()
    d.open(policy="untrusted", sandbox="workspace-write")
    start = d.mark()
    turn = d.start_turn("Run the shell command `touch asked.txt` in the "
                        "current folder, then reply with just: done")
    asked = d.wait(lambda m: "id" in m and "requestApproval" in str(
        m.get("method")), 90.0, start, "an approval request")
    time.sleep(1.0)
    d.call_built(wire.turn_steer(
        d.next_id(), d.thread, turn,
        "Do not run that. Reply only with the word: steered"))
    try:
        # Does the turn go on without an answer?
        d.wait(lambda m: m.get("method") == "turn/completed", 20.0, start,
               "turn/completed")
        ctx["turn_ended_without_an_answer"] = True
    except rp.ScenarioError:
        ctx["turn_ended_without_an_answer"] = False
        d.send(wire.approval_reply(asked["id"], "decline"))
        d.until_done(start, handled=[asked["id"]])
    ctx["asked_file_exists"] = os.path.exists(os.path.join(work, "asked.txt"))


def sc_resume(spawn, work, ctx):
    d1 = spawn()
    thread = d1.open()
    d1.turn("Remember this word: papaya. Reply only with: ok")
    d1.finish()
    d2 = spawn()
    d2.open(resume=thread)
    d2.turn("Which word did I ask you to remember? Reply with just the "
            "word.")
    d2.finish()
    d3 = spawn()
    d3.open(resume=thread, fork=True)
    d3.turn("Which word did I ask you to remember? Reply with just the "
            "word.")


def sc_rollback(spawn, work, ctx):
    d = spawn()
    d.open()
    d.turn("Remember this word: papaya. Reply only with: ok")
    d.turn("Remember this second word: walrus. Reply only with: ok")
    second = d.native_turn
    d.call_built(wire.thread_revert(d.next_id(), d.thread, second))
    d.turn("Which words did I ask you to remember? Reply with just the "
           "words.")


def sc_options(spawn, work, ctx):
    """``turn/start`` overrides: model and effort, then approvalPolicy."""
    d = spawn()
    d.open(policy="untrusted", sandbox="workspace-write")
    asked = []

    def answer(msg):
        asked.append(msg.get("method"))
        return {"decision": "accept"}

    d.turn("Run the shell command `touch one.txt`, then reply with just: "
           "done", answer=answer)
    first = len(asked)
    d.turn("Run the shell command `touch two.txt`, then reply with just: "
           "done", answer=answer, approval_policy="never",
           model="gpt-6-luna", effort="medium")
    ctx["approvals_under_untrusted"] = first
    ctx["approvals_under_never"] = len(asked) - first
    # No override this time: does the server keep the last ones?
    d.turn("Run the shell command `touch three.txt`, then reply with "
           "just: done", answer=answer, effort="")
    ctx["approvals_with_no_override"] = len(asked) - first \
        - ctx["approvals_under_never"]
    ctx["files"] = sorted(os.listdir(work))


def sc_model_list(spawn, work, ctx):
    """``model/list`` after ``thread/start``, as the adapter asks it:
    the server's own list of models (G32)."""
    d = spawn()
    d.open()
    answer = d.call("model/list", {})
    ctx["listed"] = "error" not in answer
    ctx["models"] = len(((answer.get("result") or {}).get("data")) or [])


def sc_attach(spawn, work, ctx):
    """An image as a ``localImage`` input item, and a file named in the
    text."""
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
    start = d.mark()
    answer = d.call_built(wire.turn_start(
        d.next_id(), d.thread, "Reply with: ok",
        model="no-such-model-for-the-recorder"))
    ctx["turn_start_refused"] = "error" in answer
    if "error" not in answer:
        d.until_done(start, timeout=90.0)
    # No override now: is the bad model kept for the next turn?
    done = d.turn("Reply with: ok", effort="")
    ctx["next_turn_without_override"] = done["params"]["turn"]["status"]
    # An effort the provider does not know: is the field read at all?
    start = d.mark()
    answer = d.call_built(wire.turn_start(
        d.next_id(), d.thread, "Reply with: ok", model=RECORDER_MODEL,
        effort="no-such-effort"))
    ctx["bad_effort_refused_at_turn_start"] = "error" in answer
    if "error" not in answer:
        done = d.until_done(start, timeout=90.0)
        ctx["bad_effort_turn"] = done["params"]["turn"]["status"]
    done = d.turn("Reply with: ok", model=RECORDER_MODEL)
    ctx["turn_after_a_good_override"] = done["params"]["turn"]["status"]


SCENARIOS = {
    "hello": sc_hello,
    "approval": sc_approval,
    "always": sc_always,
    "interrupt": sc_interrupt,
    "steer": sc_steer,
    "steer_tool": sc_steer_tool,
    "steer_approval": sc_steer_approval,
    "resume": sc_resume,
    "rollback": sc_rollback,
    "options": sc_options,
    "model_list": sc_model_list,
    "attach": sc_attach,
    "error": sc_error,
}


def _png(r: int, g: int, b: int, size: int = 16) -> bytes:
    """A solid ``size`` x ``size`` PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body))

    row = b"\x00" + bytes((r, g, b)) * size
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * size))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------------
# Cutting a message down to what the adapter reads


def reduce(msg):
    if not isinstance(msg, dict):
        return msg
    method = msg.get("method")
    if isinstance(method, str) and "id" not in msg \
            and method not in wire._NOTIFICATIONS:
        return {"method": method}
    body = msg.get("result")
    if isinstance(body, dict) and "method" not in msg:
        if isinstance(body.get("thread"), dict):
            kept = {k: body[k] for k in _THREAD_ANSWER_KEEP if k in body}
            kept["thread"] = {k: body["thread"][k] for k in _THREAD_KEEP
                              if k in body["thread"]}
            return {**msg, "result": kept}
        if isinstance(body.get("data"), list) and all(
                isinstance(m, dict) and "displayName" in m
                for m in body["data"]):
            # model/list: ids and display names only (G32).
            return {**msg, "result": {
                "data": [{k: m[k] for k in ("id", "model", "displayName")
                          if k in m} for m in body["data"]],
                "nextCursor": body.get("nextCursor")}}
        if "userAgent" in body:
            return {**msg, "result": {k: body[k] for k in _INIT_KEEP
                                      if k in body}}
    return msg


def record(scenario: str, version: str, raw_dir: Path | None,
           cli: str) -> Path:
    fn = SCENARIOS[scenario]
    root = tempfile.mkdtemp(prefix="swe-rec-")
    work = os.path.join(root, "work")
    config = os.path.join(root, "config")
    home = os.path.join(root, "home")
    for folder in (work, config, home):
        os.mkdir(folder)
    Path(work, "notes.txt").write_text("first line of the notes\nsecond line\n")
    Path(work, "hello.py").write_text('print("hello")\n')
    Path(config, "config.toml").write_text(_CONFIG)
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set: the recorder's "
                         "throwaway CODEX_HOME has no other login")
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update(CODEX_HOME=config, HOME=home)
    drivers: list[RpcDriver] = []
    ctx: dict = {}

    def spawn():
        d = RpcDriver([cli, "app-server"], work, env)
        d._cwd = work
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
        work, config=config, home=os.path.expanduser("~"),
        user=getpass.getuser(),
        secrets=[os.environ.get(k, "") for k in _SECRET_ENV], deny=[],
        hosts=[host, host.split(".")[0]], id_prefixes=_ID_PREFIXES)
    # The throwaway HOME is one more path to hide.
    red.config += [p for p in {home, os.path.realpath(home)}]
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
        "argv_tails": [["app-server"] for _ in drivers],
        "env_set": ["CODEX_HOME", "HOME"],
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
    out = rp.FIXTURES / ADAPTER / version / f"{scenario}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    shutil.rmtree(root, ignore_errors=True)
    rp.golden(out)
    print(f"{scenario}: {len(lines) - 1} lines, exit {codes}"
          + (f", stopped early: {error}" if error else ""))
    return out
