"""A fake ``claude`` speaking the stream-json control protocol on stdio.

Started by tests/test_remote_claude_adapter.py as ``[sys.executable,
this file]``; the adapter appends its usual flags. It answers the way
the recordings under tests/fixtures/protocols/claude_stream/ show the
real CLI does, closely enough for the adapter contract: the
``initialize`` handshake, a turn with streamed text, a ``can_use_tool``
request that waits for our answer, ``interrupt``, the option and task
control requests, and the failures.

Environment:

FAKE_CLAUDE_MODE
    ``ok`` (default), ``approval`` (each turn asks ``can_use_tool``
    first), ``slow`` (a turn streams once, then waits for ``interrupt``),
    ``crash`` (exits mid-turn), ``exit-at-init`` (exits before the
    handshake), ``silent-init`` (never answers ``initialize``),
    ``refuse-init`` (answers it with an error), ``old`` (reports version
    1.0.0), ``task`` (starts a background task, waits for
    ``stop_task``), ``noise`` (unknown lines and fields around a turn),
    ``no-state`` (no ``session_state_changed`` frames), ``hook`` (asks a
    ``hook_callback`` before the turn), ``wake`` (a turn the CLI starts
    by itself after the first one), ``cancel`` (withdraws its approval
    with ``control_cancel_request``), ``linger`` (ignores the end of its
    stdin until it is terminated), ``deaf`` (answers ``initialize``,
    then never reads stdin again), ``slow-tool`` (like ``approval``, but
    an allowed call keeps running until a steer cuts it).

A user message with ``priority: "now"`` mid-turn is a steer, answered
the way the steer, steer_tool and steer_approval recordings show: in
``slow`` the reply being streamed arrives cut (``aborted: true``) with a
``result``; in ``slow-tool`` the running call's result says
``interrupted``; with an approval open the CLI withdraws it
(``control_cancel_request``) and the call fails "Tool permission request
aborted". Then a new init, the steered reply, a second ``result`` and
one ``idle``. In the other modes it is queued and ignored.

FAKE_CLAUDE_REPLY
    The assistant's reply text (default ``hello``).
FAKE_CLAUDE_STEER_REPLY
    The reply to a steer (default ``steered``).
FAKE_CLAUDE_PARTIAL
    The text ``slow`` streams before it waits (default ``1\\n2\\n``).
FAKE_CLAUDE_CAPS
    A JSON list for the init message's ``capabilities`` (default the
    2.1.328 list; ``none`` leaves the key out). A list without
    ``interrupt_send_now_v1`` is a CLI that does not take steer.
FAKE_CLAUDE_NO_INIT
    Set: no turn prints an init message.
FAKE_CLAUDE_LOG
    A file every line we receive is appended to, and the argv on the
    first line.
"""

import json
import os
import sys

MODE = os.environ.get("FAKE_CLAUDE_MODE", "ok")
REPLY = os.environ.get("FAKE_CLAUDE_REPLY", "hello")
STEER_REPLY = os.environ.get("FAKE_CLAUDE_STEER_REPLY", "steered")
PARTIAL = os.environ.get("FAKE_CLAUDE_PARTIAL", "1\n2\n")
LOG = os.environ.get("FAKE_CLAUDE_LOG")
CAPS_ENV = os.environ.get("FAKE_CLAUDE_CAPS")
VERSION = "1.0.0" if MODE == "old" else "2.1.328"
CAPS = ["interrupt_receipt_v1", "interrupt_send_now_v1", "msg_lifecycle_v1"]
if CAPS_ENV and CAPS_ENV != "none":
    CAPS = json.loads(CAPS_ENV)

REQUIRED = ("--output-format", "--verbose", "--input-format",
            "--permission-prompt-tool", "--include-partial-messages")


def log(obj):
    if LOG:
        with open(LOG, "a") as fh:
            fh.write(json.dumps(obj) + "\n")


def out(obj):
    sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj))
                     + "\n")
    sys.stdout.flush()


def arg(flag):
    for tok in sys.argv[1:]:
        if tok.startswith(flag + "="):
            return tok[len(flag) + 1:]
    return ""


log({"argv": sys.argv[1:], "env": {
    k: os.environ.get(k) for k in ("CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS",
                                   "CLAUDECODE")}})
missing = [f for f in REQUIRED if f not in sys.argv[1:]]
if missing:
    sys.stderr.write(f"error: missing {missing}\n")
    sys.exit(2)
if MODE == "exit-at-init":
    sys.stderr.write("error: not logged in\n")
    sys.exit(1)

SID = arg("--session-id") or arg("--resume") or "11111111-2222-4333-8444-555555555555"
MODEL = arg("--model") or "claude-test-1"
state = {"mode": arg("--permission-mode") or "default", "turn": 0,
         "pending": None, "task": None, "busy": False}
msg_n = [0]


def system(sub, **kw):
    out({"type": "system", "subtype": sub, "session_id": SID, **kw})


def set_state(s):
    if MODE != "no-state":
        system("session_state_changed", state=s)


def init():
    if os.environ.get("FAKE_CLAUDE_NO_INIT"):
        return
    body = {"cwd": os.getcwd(), "model": MODEL,
            "permissionMode": state["mode"], "claude_code_version": VERSION,
            "tools": ["Bash", "Read", "Write", "Task"]}
    if CAPS_ENV != "none":
        body["capabilities"] = CAPS
    system("init", **body)


def stream(api, index, etype, **kw):
    out({"type": "stream_event", "event": {"type": etype, "index": index,
                                           **kw},
         "session_id": SID, "parent_tool_use_id": None,
         "api_message_id": api})


def say(text, chunks=2, aborted=False):
    msg_n[0] += 1
    api = f"msg_fake{msg_n[0]}"
    out({"type": "stream_event", "event": {"type": "message_start",
                                           "message": {"id": api}},
         "session_id": SID, "parent_tool_use_id": None})
    stream(api, 0, "content_block_start",
           content_block={"type": "text", "text": ""})
    size = max(1, len(text) // chunks)
    parts = [text[i:i + size] for i in range(0, len(text), size)] or [""]
    for part in parts:
        stream(api, 0, "content_block_delta",
               delta={"type": "text_delta", "text": part})
    assistant(api, [{"type": "text", "text": text}], aborted=aborted)
    stream(api, 0, "content_block_stop")


def assistant(api, content, parent=None, aborted=False):
    msg = {"type": "assistant", "message": {"id": api, "role": "assistant",
                                            "content": content},
           "parent_tool_use_id": parent, "session_id": SID}
    if aborted:
        msg["aborted"] = True
    if MODE == "noise":
        msg["brand_new_field"] = {"nested": [1, 2]}
    out(msg)


def result(error=False, subtype="success", terminal_reason="completed"):
    out({"type": "result", "subtype": subtype, "is_error": error,
         "terminal_reason": terminal_reason,
         "session_id": SID, "result": "" if error else REPLY,
         "errors": ["boom"] if error else None, "total_cost_usd": 0.001,
         "usage": {"input_tokens": 3, "output_tokens": 5,
                   "cache_read_input_tokens": 7,
                   "cache_creation_input_tokens": 0},
         "modelUsage": {MODEL: {"contextWindow": 200000}}})


def finish_turn():
    say(REPLY)
    result()
    set_state("idle")
    state["busy"] = False
    if MODE == "wake" and state["turn"] == 1:
        # A background task's result wakes the CLI for a turn of its own.
        state["turn"] += 1
        set_state("running")
        init()
        say("woke up")
        result()
        set_state("idle")


def ask_tool():
    tool = {"type": "tool_use", "id": "toolu_fake1", "name": "Bash",
            "input": {"command": "ls -la", "description": "List"}}
    assistant("msg_tool", [tool])
    set_state("requires_action")
    state["pending"] = "perm-1"
    out({"type": "control_request", "request_id": "perm-1", "request": {
        "subtype": "can_use_tool", "tool_name": "Bash", "display_name":
        "Bash", "input": tool["input"], "tool_use_id": "toolu_fake1",
        "permission_suggestions": [{"type": "addRules", "rules": [
            {"toolName": "Bash", "ruleContent": "ls *"}],
            "behavior": "allow", "destination": "localSettings"}]}})
    if MODE == "cancel":
        out({"type": "control_cancel_request", "request_id": "perm-1"})
        state["pending"] = None
        set_state("running")
        finish_turn()


def answered(resp):
    body = resp.get("response") or {}
    state["pending"] = None
    set_state("running")
    ok = body.get("behavior") == "allow"
    if ok and MODE == "slow-tool":
        state["running_tool"] = True  # until a steer cuts it
        return
    out({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_fake1",
         "content": "file-a\nfile-b" if ok else body.get("message", ""),
         "is_error": not ok}]},
        "parent_tool_use_id": None, "session_id": SID,
        "tool_use_result": {"stdout": "file-a\nfile-b"} if ok else
        "Error: denied"})
    finish_turn()


def tool_result(content, structured):
    out({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_fake1",
         "content": content, "is_error": True}]},
        "parent_tool_use_id": None, "session_id": SID,
        "tool_use_result": structured})


def steered(msg):
    """A ``priority: "now"`` message mid-turn, as recorded: the work in
    progress is cut, then the message is answered in the same running
    stretch, and the CLI goes idle once."""
    if state["pending"]:
        rid, state["pending"] = state["pending"], None
        out({"type": "control_cancel_request", "request_id": rid})
        set_state("running")
        tool_result("Tool permission request aborted",
                    "Error: Tool permission request aborted")
        result(terminal_reason="aborted_tools")
    elif state.get("running_tool"):
        state["running_tool"] = False
        tool_result("<error>Command was aborted before completion</error>",
                    {"stdout": "", "stderr": "", "interrupted": True})
        result(terminal_reason="aborted_tools")
    elif state.get("slow_api"):
        api = state.pop("slow_api")
        assistant(api, [{"type": "text", "text": PARTIAL}], aborted=True)
        stream(api, 0, "content_block_stop")
        result(terminal_reason="aborted_streaming")
    else:
        return
    init()
    say(STEER_REPLY)
    result()
    set_state("idle")
    state["busy"] = False


def start_turn(msg):
    state["turn"] += 1
    state["busy"] = True
    set_state("running")
    init()
    if MODE == "noise":
        out("this is not json")
        out("[1, 2]")
        out({"type": "brand_new_type", "x": 1})
        system("brand_new_subtype", y=2)
        out({"type": "stream_event", "event": "not an object"})
        out({"type": "assistant", "message": "not an object"})
        out({"type": "user", "message": {"content": "plain text"}})
        out({"type": "control_request", "request": "no id"})
        out({"type": "control_response", "response": {"request_id": 7}})
    if MODE in ("approval", "cancel", "slow-tool"):
        ask_tool()
        return
    if MODE in ("slow", "crash"):
        msg_n[0] += 1
        api = f"msg_fake{msg_n[0]}"
        stream(api, 0, "content_block_start",
               content_block={"type": "text", "text": ""})
        stream(api, 0, "content_block_delta",
               delta={"type": "text_delta", "text": PARTIAL})
        if MODE == "crash":
            sys.stderr.write("panic: the fake crashed\n")
            sys.stdout.flush()
            os._exit(3)
        state["slow_api"] = api
        return
    if MODE == "task":
        assistant("msg_task", [{"type": "tool_use", "id": "toolu_agent",
                                "name": "Task",
                                "input": {"description": "Count"}}])
        system("task_started", task_id="task-1", tool_use_id="toolu_agent",
               description="Count slowly", task_type="local_agent")
        assistant("msg_sub", [{"type": "tool_use", "id": "toolu_sub",
                               "name": "Bash",
                               "input": {"command": "sleep 1"}},
                              {"type": "text", "text": "sub words"}],
                  parent="toolu_agent")
        out({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_agent",
             "content": [{"type": "text", "text": "launched"}]}]},
            "parent_tool_use_id": None, "session_id": SID})
        return
    if MODE == "no-state":
        say(REPLY)
        result()
        state["busy"] = False
        return
    finish_turn()


def control(msg):
    rid = msg.get("request_id")
    req = msg.get("request") or {}
    sub = req.get("subtype")

    def ok(body=None):
        out({"type": "control_response", "response": {
            "subtype": "success", "request_id": rid, "response": body or {}}})

    def err(text):
        out({"type": "control_response", "response": {
            "subtype": "error", "request_id": rid, "error": text}})

    if sub == "initialize":
        if MODE == "silent-init":
            return
        if MODE == "refuse-init":
            err("auth required")
            return
        ok({"claude_code_version": VERSION,
            "current_permission_mode": state["mode"],
            "models": [{"value": "default"}, {"value": "haiku"}],
            "capabilities": ["ui_surface_v1"], "session_state": "idle",
            "account": {"email": "someone@example.com"}})
        if MODE == "deaf":
            import threading
            threading.Event().wait()  # never reads again, until killed
        if MODE == "hook":
            out({"type": "control_request", "request_id": "hook-1",
                 "request": {"subtype": "hook_callback",
                             "callback_id": "hook_0"}})
    elif sub == "interrupt":
        ok({"still_queued": []})
        if state["busy"] and MODE == "slow":
            assistant(state.pop("slow_api", "msg_x"),
                      [{"type": "text", "text": PARTIAL}], aborted=True)
            out({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "[Request interrupted by user]"}]},
                "parent_tool_use_id": None, "session_id": SID})
            result(error=True, subtype="error_during_execution")
            set_state("idle")
            state["busy"] = False
        elif state["busy"] and state["pending"]:
            state["pending"] = None
            result(error=True, subtype="error_during_execution")
            set_state("idle")
            state["busy"] = False
    elif sub == "set_model":
        if req.get("model") == "nope":
            err("unknown model")
            return
        ok({})
    elif sub == "set_permission_mode":
        state["mode"] = req.get("mode")
        ok({"mode": req.get("mode")})
        system("status", status=None, permissionMode=req.get("mode"))
    elif sub == "stop_task":
        ok({})
        if state["busy"] and MODE == "task":
            system("task_updated", task_id=req.get("task_id"),
                   patch={"status": "killed"})
            system("task_notification", task_id=req.get("task_id"),
                   status="stopped", tool_use_id="toolu_agent")
            out({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_sub",
                 "content": "", "is_error": True}]},
                "parent_tool_use_id": "toolu_agent", "session_id": SID,
                "tool_use_result": {"interrupted": True}})
            finish_turn()
    else:
        err(f"unsupported {sub}")


for raw in sys.stdin:
    try:
        msg = json.loads(raw)
    except ValueError:
        continue
    log(msg)
    mtype = msg.get("type")
    if mtype == "control_request":
        control(msg)
    elif mtype == "control_response":
        resp = msg.get("response") or {}
        if resp.get("request_id") == state["pending"]:
            answered(resp)
    elif mtype == "user":
        if state["busy"]:
            # A steer cuts the work in progress; any other message the
            # real CLI queues (the adapter never sends one).
            if msg.get("priority") == "now":
                steered(msg)
            continue
        start_turn(msg)
if MODE == "linger":
    import threading
    threading.Event().wait()  # until SIGTERM
sys.exit(0)
