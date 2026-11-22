"""A fake ACP agent speaking JSON-RPC on stdio.

Started by tests/test_remote_acp.py as ``[sys.executable, this file]``.
It answers the way the recordings under tests/fixtures/protocols/acp/
show a real agent does, closely enough for the adapter contract:
``initialize``, ``session/new`` and ``session/load``, a turn with
streamed chunks, tool calls, permission requests that wait for our
answer, ``session/cancel``, ``session/set_mode``, ``session/set_model``
and the failures.

Environment:

FAKE_ACP_MODE
    ``ok`` (default), ``approval`` (each turn asks to run a command
    first), ``edit`` (asks to write a file, and reports a diff),
    ``slow`` (a turn streams once, then waits for ``session/cancel``),
    ``rich`` (a thought, a plan and a tool call around the reply),
    ``crash`` (exits mid-turn), ``exit-at-init`` (exits before the
    handshake), ``silent-init`` (never answers ``initialize``),
    ``refuse-new`` (answers ``session/new`` with an error), ``no-load``
    (does not advertise ``loadSession``), ``old`` (reports version
    0.0.1), ``bare`` (lists no modes and no models), ``noise`` (unknown
    lines, another session's updates and an unknown request around a
    turn), ``fail`` (the turn's answer is an error), ``stopped`` (the
    turn ends with ``max_tokens``), ``refuse-mode``, ``late-ask`` (asks
    a permission after the cancel), ``linger`` (ignores the end of its
    stdin until it is terminated), ``deaf`` (handshakes, then never
    reads stdin again), ``secret`` (the reply, a tool's output and a
    permission's command hold an API key).

    A prompt that starts with ``mode:<name> `` runs that turn in mode
    ``<name>``, whatever the environment says.

FAKE_ACP_REPLY
    The assistant's reply text (default ``hello``).
FAKE_ACP_PARTIAL
    The text ``slow`` streams before it waits (default ``1\\n2\\n``).
FAKE_ACP_LOG
    A file every line we receive is appended to, and the argv on the
    first line.
"""

import json
import os
import signal
import sys
import threading

MODE = os.environ.get("FAKE_ACP_MODE", "ok")
REPLY = os.environ.get("FAKE_ACP_REPLY", "hello")
PARTIAL = os.environ.get("FAKE_ACP_PARTIAL", "1\n2\n")
LOG = os.environ.get("FAKE_ACP_LOG")
VERSION = "0.0.1" if MODE == "old" else "9.9.9"

SESSION = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-2222-4333-8444-555555555555"
# What ``secret`` leaks; the adapter must mask it.
KEY = "sk-ant-api03-" + "d" * 40
MODES = [{"id": "plan", "name": "Plan"}, {"id": "default", "name": "Default"},
         {"id": "yolo", "name": "YOLO"}]
if MODE == "yolo-first":
    # An agent whose list starts with a mode that asks nothing and has no
    # ``default``, started in plan mode (G32).
    MODES = [{"id": "yolo", "name": "YOLO"}, {"id": "auto", "name": "Auto"},
             {"id": "plan", "name": "Plan"}]
MODELS = [{"modelId": "prov|model-1", "name": "model-1"},
          {"modelId": "prov|model-2", "name": "model-2"}]

state = {"session": SESSION,
         "mode": "plan" if MODE == "yolo-first" else "default",
         "model": "prov|model-1",
         "prompt": None, "turn_mode": MODE, "pending": None, "n": 0,
         "call": "", "cancelled": False}


def log(obj):
    if LOG:
        with open(LOG, "a") as fh:
            fh.write(json.dumps(obj) + "\n")


def out(obj):
    sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj))
                     + "\n")
    sys.stdout.flush()


def result(rpc_id, body=None):
    out({"jsonrpc": "2.0", "id": rpc_id, "result": body or {}})


def error(rpc_id, message, code=-32603, details=None):
    err = {"code": code, "message": message}
    if details:
        err["data"] = {"details": details}
    out({"jsonrpc": "2.0", "id": rpc_id, "error": err})


def update(body, session=None):
    out({"jsonrpc": "2.0", "method": "session/update",
         "params": {"sessionId": session or state["session"],
                    "update": body}})


def chunk(text, what="agent_message_chunk", **extra):
    update({"sessionUpdate": what,
            "content": {"type": "text", "text": text}, **extra})


def say(text):
    half = max(1, len(text) // 2)
    chunk("")  # real agents send empty chunks that carry only _meta
    chunk(text[:half])
    chunk(text[half:], _meta={"usage": {
        "inputTokens": 100, "outputTokens": 5, "cachedReadTokens": 40}})
    update({"sessionUpdate": "usage_update", "used": 105, "size": 128000})


def session_info():
    if MODE == "bare":
        return {}
    return {"modes": {"currentModeId": state["mode"],
                      "availableModes": MODES},
            "models": {"currentModelId": state["model"],
                       "availableModels": MODELS}}


def next_call():
    state["n"] += 1
    return f"call_{state['n']:04d}"


def ask(mode):
    """A tool call that asks first; the turn waits for our answer."""
    call = state["call"] = next_call()
    edit = mode == "edit"
    command = f"echo {KEY}" if mode == "secret" else "touch probe.txt"
    raw = {"file_path": "/w/probe.txt", "content": "yes"} if edit \
        else {"command": command}
    title = "Writing to probe.txt" if edit else command
    update({"sessionUpdate": "tool_call", "toolCallId": call,
            "status": "pending", "title": "WriteFile" if edit else "Shell",
            "kind": "edit" if edit else "execute", "rawInput": {}})
    state["n"] += 1
    state["pending"] = 1000 + state["n"]
    out({"jsonrpc": "2.0", "id": state["pending"],
         "method": "session/request_permission",
         "params": {"sessionId": state["session"], "options": [
             {"optionId": "always_project", "kind": "allow_always",
              "name": f"Always Allow in project: {KEY}"
              if mode == "secret" else "Always Allow in project"},
             {"optionId": "once", "name": "Allow", "kind": "allow_once"},
             {"optionId": "cancel", "name": "Reject",
              "kind": "reject_once"}],
             "toolCall": {"toolCallId": call, "status": "pending",
                          "title": title,
                          "kind": "edit" if edit else "execute",
                          "rawInput": raw}}})


def finish(stop="end_turn"):
    rpc_id, state["prompt"] = state["prompt"], None
    state["pending"] = None
    if rpc_id is not None:
        result(rpc_id, {"stopReason": stop})


def answered(msg):
    """Our permission request got its answer."""
    mode = state["turn_mode"]
    outcome = (msg.get("result") or {}).get("outcome") or {}
    call = state["call"]
    state["pending"] = None
    if outcome.get("outcome") != "selected":
        update({"sessionUpdate": "tool_call_update", "toolCallId": call,
                "status": "failed", "content": [{"type": "content", "content": {
                    "type": "text",
                    "text": "Tool call was cancelled before execution."}}]})
        return finish("cancelled" if state["cancelled"] else "end_turn")
    if outcome.get("optionId") == "cancel":
        update({"sessionUpdate": "tool_call_update", "toolCallId": call,
                "status": "failed", "content": [{"type": "content", "content": {
                    "type": "text", "text": "canceled by the user"}}]})
        say("denied")
        return finish()
    update({"sessionUpdate": "tool_call_update", "toolCallId": call,
            "status": "in_progress"})
    if mode == "edit":
        content = [{"type": "diff", "path": "/w/probe.txt", "oldText": "",
                    "newText": "yes"}]
    else:
        content = [{"type": "content", "content": {
            "type": "text", "text": KEY if mode == "secret" else "Exit Code: 0"}}]
    update({"sessionUpdate": "tool_call_update", "toolCallId": call,
            "status": "completed", "content": content})
    say(f"the key is {KEY}" if mode == "secret" else REPLY)
    return finish()


def prompt(msg):
    params = msg.get("params") or {}
    blocks = params.get("prompt") or [{}]
    text = str(blocks[0].get("text") or "")
    mode = MODE
    if text.startswith("mode:"):
        mode, _, text = text[5:].partition(" ")
    if state["prompt"] is not None:
        return error(msg["id"], "a prompt is already running", -32600)
    state.update(prompt=msg["id"], turn_mode=mode, cancelled=False)
    if mode == "noise":
        out("this is not json")
        out(json.dumps([1, 2]))
        update({"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "not ours"}}, OTHER)
        update({"sessionUpdate": "available_commands_update"})
        out({"jsonrpc": "2.0", "method": "vendor/notify", "params": {}})
        out({"jsonrpc": "2.0", "id": 7000, "method": "vendor/ask",
             "params": {}})
    if mode == "fail":
        return finish_error("Internal error", "404 The model does not exist")
    if mode == "crash":
        chunk(PARTIAL)
        sys.stdout.flush()
        os._exit(3)
    if mode in ("approval", "edit", "secret"):
        return ask(mode)
    if mode in ("slow", "late-ask"):
        return chunk(PARTIAL)
    if mode == "rich":
        chunk("thinking it over", "agent_thought_chunk")
        update({"sessionUpdate": "plan", "entries": [
            {"content": "look", "status": "completed", "priority": "high"},
            {"content": "act", "status": "in_progress"},
            {"content": "odd", "status": "someday"}]})
        call = next_call()
        update({"sessionUpdate": "tool_call", "toolCallId": call,
                "status": "pending", "title": "ReadFile", "kind": "read",
                "rawInput": {}})
        update({"sessionUpdate": "tool_call_update", "toolCallId": call,
                "status": "in_progress", "title": "ReadFile: notes.txt",
                "rawInput": {"file_path": "/w/notes.txt"}})
        update({"sessionUpdate": "tool_call_update", "toolCallId": call,
                "status": "completed", "content": [{"type": "content",
                                                    "content": {"type": "text", "text": "first line"}}]})
        update({"sessionUpdate": "current_mode_update",
                "currentModeId": "yolo"})
    if state["mode"] == "plan":
        say("planning")
    else:
        say(REPLY)
    finish("max_tokens" if mode == "stopped" else "end_turn")
    return None


def finish_error(message, details):
    rpc_id, state["prompt"] = state["prompt"], None
    error(rpc_id, message, details=details)


def handle(msg):
    method = msg.get("method")
    rpc_id = msg.get("id")
    params = msg.get("params") or {}
    if method is None:
        if rpc_id == state["pending"]:
            answered(msg)
        return
    if method == "initialize":
        if MODE == "silent-init":
            return
        caps = {"loadSession": MODE != "no-load",
                "promptCapabilities": {"image": True}}
        return result(rpc_id, {
            "protocolVersion": 1, "agentCapabilities": caps,
            "agentInfo": {"name": "fake-agent", "title": "Fake",
                          "version": VERSION}})
    if method == "session/new":
        if MODE == "refuse-new":
            return error(rpc_id, "Authentication required", -32000)
        return result(rpc_id, {"sessionId": SESSION, **session_info()})
    if method == "session/load":
        if params.get("sessionId") == "missing":
            return error(rpc_id, "Session not found", -32002)
        state["session"] = params.get("sessionId") or SESSION
        chunk("an old reply, replayed")
        return result(rpc_id, session_info())
    if method == "session/set_mode":
        if MODE == "refuse-mode":
            return error(rpc_id, "Invalid params: no such mode", -32602)
        state["mode"] = params.get("modeId") or state["mode"]
        return result(rpc_id)
    if method == "session/set_model":
        state["model"] = params.get("modelId") or state["model"]
        return result(rpc_id)
    if method == "session/prompt":
        return prompt(msg)
    if method == "session/cancel":
        state["cancelled"] = True
        if state["turn_mode"] == "late-ask" and state["prompt"] is not None:
            state["turn_mode"] = "approval"
            return ask("approval")
        if state["pending"] is None and state["prompt"] is not None:
            finish("cancelled")
        return None
    if rpc_id is not None:
        return error(rpc_id, f"{method} is not supported", -32601)
    return None


def main():
    log({"argv": sys.argv[1:], "cwd": os.getcwd()})
    if MODE == "exit-at-init":
        sys.stderr.write("fake acp: cannot start\n")
        return 3
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        log(msg)
        handle(msg)
        if MODE == "deaf" and msg.get("method") == "session/new":
            threading.Event().wait(60)
    if MODE == "linger":
        signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
        threading.Event().wait(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
