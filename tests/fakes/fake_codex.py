"""A fake ``codex app-server`` speaking JSON-RPC on stdio.

Started by tests/test_remote_adapters.py as ``[sys.executable,
this file]``. It answers the way the recordings under
tests/fixtures/protocols/codex_appserver/ show the real CLI does,
closely enough for the adapter contract: the ``initialize`` handshake,
``thread/start`` / ``thread/resume`` / ``thread/fork``, a turn with
streamed text, approval requests that wait for our answer,
``turn/steer``, ``turn/interrupt``, ``thread/revert`` and the failures.

Environment:

FAKE_CODEX_MODE
    ``ok`` (default), ``approval`` (each turn asks a command approval
    first), ``patch`` (asks a file-change approval), ``question`` (asks
    ``item/tool/requestUserInput``), ``permissions`` (asks
    ``item/permissions/requestApproval``), ``slow`` (a turn streams
    once, then waits for ``turn/interrupt`` or ``turn/steer``),
    ``late-tool`` (like ``slow`` with a command running; a steer ends
    the turn and the command completes after it), ``crash`` (exits
    mid-turn), ``exit-at-init`` (exits before the handshake),
    ``silent-init`` (never answers ``initialize``), ``refuse-init``,
    ``refuse-thread`` (answers ``thread/start`` with an error), ``old``
    (reports version 0.1.0), ``noise`` (unknown lines, another thread's
    notifications and an unknown server request around a turn),
    ``fail`` (the turn fails with an ``error`` notification),
    ``refuse-turn`` (answers ``turn/start`` with an error), ``no-steer``
    (does not know ``turn/steer``), ``withdraw`` (takes its approval
    back with ``serverRequest/resolved``), ``linger`` (ignores the end
    of its stdin until it is terminated), ``deaf`` (handshakes, then
    never reads stdin again), ``plan`` (a plan update and a reasoning
    item in the turn).

    A prompt that starts with ``mode:<name> `` runs that turn in mode
    ``<name>``, whatever the environment says.

FAKE_CODEX_REPLY
    The assistant's reply text (default ``hello``).
FAKE_CODEX_STEER_REPLY
    The reply to a steer (default ``steered``).
FAKE_CODEX_PARTIAL
    The text ``slow`` streams before it waits (default ``1\\n2\\n``).
FAKE_CODEX_LOG
    A file every line we receive is appended to, and the argv and the
    environment's ``CODEX_HOME`` on the first line.
"""

import json
import os
import sys
import threading

MODE = os.environ.get("FAKE_CODEX_MODE", "ok")
REPLY = os.environ.get("FAKE_CODEX_REPLY", "hello")
STEER_REPLY = os.environ.get("FAKE_CODEX_STEER_REPLY", "steered")
PARTIAL = os.environ.get("FAKE_CODEX_PARTIAL", "1\n2\n")
LOG = os.environ.get("FAKE_CODEX_LOG")
VERSION = "0.1.0" if MODE == "old" else "0.170.0"
HOME = os.environ.get("CODEX_HOME") or "/nonexistent/codex-home"

THREAD = "11111111-2222-7333-8444-555555555555"
FORKED = "11111111-2222-7333-8444-666666666666"
REVERTED = "11111111-2222-7333-8444-777777777777"

state = {"thread": THREAD, "turn": "", "n": 0, "pending": None, "mode": MODE,
         "stream": "", "command": "", "reverted": [], "srv": 0}


def log(obj):
    if LOG:
        with open(LOG, "a") as fh:
            fh.write(json.dumps(obj) + "\n")


def out(obj):
    sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj))
                     + "\n")
    sys.stdout.flush()


def note(method, **params):
    params.setdefault("threadId", state["thread"])
    out({"method": method, "params": params})


def answer(rpc_id, body):
    out({"id": rpc_id, "result": body})


def refuse(rpc_id, message, code=-32600):
    out({"id": rpc_id, "error": {"code": code, "message": message}})


def thread(tid, forked=None, rollout=""):
    stem = f"rollout-2026-01-02T03-04-05-{tid}" + rollout
    return {"id": tid, "sessionId": tid, "cliVersion": VERSION,
            "forkedFromId": forked, "status": {"type": "idle"},
            "path": os.path.join(HOME, "sessions", stem + ".jsonl"),
            "ephemeral": False, "preview": "", "turns": []}


def item(kind, iid, **fields):
    return {"type": kind, "id": iid, **fields}


def started(it):
    note("item/started", item=it, turnId=state["turn"])


def completed(it):
    note("item/completed", item=it, turnId=state["turn"])


def say(text, chunks=2):
    state["n"] += 1
    iid = f"msg_{state['n']:04d}"
    started(item("agentMessage", iid, text="", phase="final_answer"))
    step = max(1, len(text) // chunks)
    for i in range(0, len(text), step):
        note("item/agentMessage/delta", turnId=state["turn"], itemId=iid,
             delta=text[i:i + step])
    completed(item("agentMessage", iid, text=text, phase="final_answer"))


def usage():
    count = {"totalTokens": 120, "inputTokens": 100, "cachedInputTokens": 40,
             "cacheWriteInputTokens": 10, "outputTokens": 20,
             "reasoningOutputTokens": 0}
    note("thread/tokenUsage/updated", turnId=state["turn"],
         tokenUsage={"total": count, "last": count,
                     "modelContextWindow": 200000})


def end_turn(status="completed", error=None):
    turn = {"id": state["turn"], "status": status, "error": error,
            "items": []}
    state["turn"] = ""
    state["pending"] = None
    state["stream"] = ""
    note("thread/status/changed", status={"type": "idle"})
    note("turn/completed", turn=turn)


def finish(text=None):
    say(REPLY if text is None else text)
    usage()
    end_turn()


def ask(method, **params):
    """One server request; the turn waits for its answer."""
    rpc_id = state["srv"]
    state["srv"] += 1
    state["pending"] = {"id": rpc_id, "method": method}
    out({"id": rpc_id, "method": method,
         "params": {"threadId": state["thread"], "turnId": state["turn"],
                    **params}})


def command_item(status, **extra):
    return item("commandExecution", "exec-0001",
                command="/bin/zsh -lc 'touch a.txt'", cwd="/w",
                commandActions=[{"type": "unknown",
                                 "command": "touch a.txt"}],
                status=status, **extra)


def patch_item(status):
    return item("fileChange", "exec-0002", status=status, changes=[
        {"path": "/w/new.txt", "kind": {"type": "add"}, "diff": "yes\n"},
        {"path": "/w/old.txt", "kind": {"type": "update",
                                       "move_path": "/w/moved.txt"},
         "diff": "@@ -1 +1 @@\n-a\n+b\n"}])


def start_turn(rpc_id, params):
    text = ""
    for part in params.get("input") or ():
        if part.get("type") == "text":
            text = part.get("text") or ""
    mode = MODE
    if text.startswith("mode:"):
        mode, _, text = text[5:].partition(" ")
    state["mode"] = mode
    if mode == "refuse-turn":
        refuse(rpc_id, "the model is not available")
        return
    state["n"] += 1
    state["turn"] = f"aaaaaaaa-0000-7000-8000-{state['n']:012d}"
    answer(rpc_id, {"turn": {"id": state["turn"], "status": "inProgress",
                             "items": [], "error": None}})
    note("thread/status/changed", status={"type": "active",
                                          "activeFlags": []})
    note("turn/started", turn={"id": state["turn"], "status": "inProgress"})
    user = item("userMessage", f"user-{state['n']}",
                clientId=params.get("clientUserMessageId"),
                content=params.get("input"))
    started(user)
    completed(user)
    if mode == "crash":
        note("item/agentMessage/delta", turnId=state["turn"],
             itemId="msg_crash", delta="par")
        sys.stderr.write("fatal: the model stream broke\n")
        sys.stderr.flush()
        sys.exit(3)
    if mode == "noise":
        out("this is not json")
        out(json.dumps(["a", "list"]))
        out({"method": "account/rateLimits/updated", "params": {}})
        out({"method": "item/agentMessage/delta",
             "params": {"threadId": "another-thread", "itemId": "x",
                        "delta": "not ours"}})
        out({"id": 900, "method": "mcpServer/elicitation/request",
             "params": {"threadId": state["thread"]}})
        out({"method": "turn/completed",
             "params": {"threadId": state["thread"],
                        "turn": {"id": "some-older-turn",
                                 "status": "completed"}}})
        out({"method": "item/completed", "params": "not an object"})
    if mode == "approval":
        started(command_item("inProgress"))
        ask("item/commandExecution/requestApproval", itemId="exec-0001",
            command="/bin/zsh -lc 'touch a.txt'", cwd="/w",
            reason="needs to write", availableDecisions=[
                "accept", "acceptForSession", "cancel"])
        return
    if mode == "withdraw":
        started(command_item("inProgress"))
        ask("item/commandExecution/requestApproval", itemId="exec-0001",
            command="/bin/zsh -lc 'touch a.txt'",
            availableDecisions=["accept", "cancel"])
        note("serverRequest/resolved", requestId=state["pending"]["id"])
        state["pending"] = None
        completed(command_item("declined"))
        finish()
        return
    if mode == "patch":
        started(patch_item("inProgress"))
        ask("item/fileChange/requestApproval", itemId="exec-0002",
            reason=None, grantRoot=None)
        return
    if mode == "permissions":
        ask("item/permissions/requestApproval", itemId="exec-0003",
            cwd="/w", reason="wants the network",
            permissions={"network": {"enabled": True}})
        return
    if mode == "question":
        ask("item/tool/requestUserInput", itemId="tool-1", isBlocking=True,
            questions=[{"id": "q1", "header": "Pick", "question":
                        "Which colour?", "options": [
                            {"label": "red", "description": "warm"},
                            {"label": "blue", "description": "cold"}]}])
        return
    if mode == "questions":
        ask("item/tool/requestUserInput", itemId="tool-1", isBlocking=True,
            questions=[{"id": "q1", "header": "", "question": "One?"},
                       {"id": "q2", "header": "", "question": "Two?"}])
        return
    if mode in ("slow", "late-tool"):
        if mode == "late-tool":
            started(command_item("inProgress"))
            state["command"] = "exec-0001"
        state["n"] += 1
        state["stream"] = f"msg_{state['n']:04d}"
        started(item("agentMessage", state["stream"], text=""))
        note("item/agentMessage/delta", turnId=state["turn"],
             itemId=state["stream"], delta=PARTIAL)
        return
    if mode == "fail":
        failure = {"message": "The model `nope` does not exist. "
                              "api_key=sk-test",
                   "codexErrorInfo": "modelNotFound"}
        note("error", turnId=state["turn"], error=dict(failure),
             willRetry=True)
        note("error", turnId=state["turn"], error=failure, willRetry=False)
        end_turn("failed", failure)
        return
    if mode == "plan":
        note("turn/plan/updated", turnId=state["turn"], explanation="why",
             plan=[{"step": "read", "status": "completed"},
                   {"step": "write", "status": "inProgress"},
                   {"step": "test", "status": "pending"}])
        started(item("reasoning", "rs_0001", summary=[], content=[]))
        note("item/reasoning/summaryTextDelta", turnId=state["turn"],
             itemId="rs_0001", delta="thinking it ", summaryIndex=0)
        note("item/reasoning/summaryTextDelta", turnId=state["turn"],
             itemId="rs_0001", delta="over", summaryIndex=0)
        completed(item("reasoning", "rs_0001",
                       summary=["thinking it over"], content=[]))
        started(item("reasoning", "rs_0002", summary=[], content=[]))
        completed(item("reasoning", "rs_0002", summary=[], content=[]))
        started(item("agentMessage", "msg_empty", text="",
                     phase="commentary"))
        completed(item("agentMessage", "msg_empty", text="",
                       phase="commentary"))
        started(item("webSearch", "ws_0001", query="adapters"))
        completed(item("webSearch", "ws_0001", query="adapters"))
        completed(item("mcpToolCall", "mcp_0001", server="files",
                       tool="read", arguments={"path": "/w/a"},
                       status="failed", error={"message": "no such file"}))
        note("thread/compacted", turnId=state["turn"])
    finish()


def answered(msg):
    """Our approval or question was answered: go on with the turn."""
    pending, state["pending"] = state["pending"], None
    note("serverRequest/resolved", requestId=pending["id"])
    body = msg.get("result") or {}
    method = pending["method"]
    if method == "item/commandExecution/requestApproval":
        ok = body.get("decision") in ("accept", "acceptForSession")
        completed(command_item("completed" if ok else "declined",
                               exitCode=0 if ok else None,
                               aggregatedOutput="made a.txt\n" if ok
                               else None))
        finish(REPLY if ok else "denied")
    elif method == "item/fileChange/requestApproval":
        ok = body.get("decision") in ("accept", "acceptForSession")
        completed(patch_item("completed" if ok else "declined"))
        finish(REPLY if ok else "denied")
    elif method == "item/permissions/requestApproval":
        finish("granted" if body.get("permissions") else "denied")
    else:
        got = (body.get("answers") or {}).get("q1") or {}
        finish("you said " + " ".join(got.get("answers") or ["nothing"]))


def steer(rpc_id, params):
    if state["mode"] == "no-steer" or MODE == "no-steer":
        refuse(rpc_id, "unknown method turn/steer", -32601)
        return
    if not state["turn"]:
        refuse(rpc_id, "no active turn to steer")
        return
    if params.get("expectedTurnId") != state["turn"]:
        refuse(rpc_id, "expected turn id does not match")
        return
    answer(rpc_id, {"turnId": state["turn"]})
    if state["pending"]:
        # As recorded: the message waits for the open approval.
        state.setdefault("held", []).append(params)
        return
    landed(params)


def landed(params):
    state["n"] += 1
    user = item("userMessage", f"user-{state['n']}",
                clientId=params.get("clientUserMessageId"),
                content=params.get("input"))
    started(user)
    completed(user)
    command = state["command"]
    state["command"] = ""
    say(STEER_REPLY)
    usage()
    end_turn()
    if command:
        # As recorded: a running command outlives the steered turn.
        note("item/completed", turnId="", item=command_item(
            "completed", exitCode=0, aggregatedOutput="late\n"))


def handle(msg):
    method = msg.get("method")
    rpc_id = msg.get("id")
    params = msg.get("params") or {}
    if method is None and rpc_id is not None:
        if state["pending"] and state["pending"]["id"] == rpc_id:
            if "error" in msg:
                state["pending"] = None
                finish("unanswered")
                return
            held = state.pop("held", [])
            if held:
                # The steer lands instead of the rest of this stretch.
                pending, state["pending"] = state["pending"], None
                note("serverRequest/resolved", requestId=pending["id"])
                completed(command_item("declined"))
                landed(held[0])
                return
            answered(msg)
        return
    if method == "initialize":
        if MODE == "silent-init":
            return
        if MODE == "refuse-init":
            refuse(rpc_id, "client version not supported")
            return
        answer(rpc_id, {"userAgent": f"swe/{VERSION} (Fake OS 1.0; arm64)",
                        "codexHome": HOME, "platformFamily": "unix",
                        "platformOs": "macos"})
        out({"method": "remoteControl/status/changed",
             "params": {"status": "disabled"}})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        if MODE == "refuse-thread":
            refuse(rpc_id, "not logged in")
            return
        answer(rpc_id, {"thread": thread(THREAD), "model": "gpt-test"})
        out({"method": "thread/started", "params": {}})
    elif method == "thread/resume":
        if params.get("threadId") != THREAD:
            refuse(rpc_id, f"no rollout found for thread id "
                           f"{params.get('threadId')}")
            return
        answer(rpc_id, {"thread": thread(THREAD)})
    elif method == "thread/fork":
        state["thread"] = FORKED
        answer(rpc_id, {"thread": thread(FORKED, forked=THREAD)})
    elif method == "turn/start":
        if state["turn"]:
            refuse(rpc_id, "a turn is already running")
            return
        start_turn(rpc_id, params)
    elif method == "turn/steer":
        steer(rpc_id, params)
    elif method == "turn/interrupt":
        if not state["turn"]:
            refuse(rpc_id, "no active turn to interrupt")
            return
        answer(rpc_id, {})
        end_turn("interrupted")
    elif method == "thread/revert":
        if MODE == "refuse-revert":
            refuse(rpc_id, "turn not found")
            return
        state["reverted"].append(params.get("beforeTurnId"))
        # As recorded: the shortened history is a new rollout file.
        answer(rpc_id, {"thread": thread(
            state["thread"], rollout=f"_{REVERTED}")})
        note("thread/reverted")
    elif rpc_id is not None:
        refuse(rpc_id, f"unknown method {method}", -32601)


def main():
    log({"argv": sys.argv[1:], "env": {"CODEX_HOME": os.environ.get(
        "CODEX_HOME"), "EXTRA": os.environ.get("FAKE_CODEX_EXTRA")}})
    if MODE == "exit-at-init":
        sys.stderr.write("error: not logged in\n")
        sys.exit(1)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        log(msg)
        handle(msg)
        if MODE == "deaf" and msg.get("method") == "thread/start":
            threading.Event().wait()  # never reads again, until killed
    if MODE == "linger":
        threading.Event().wait()  # until SIGTERM


if __name__ == "__main__":
    main()
