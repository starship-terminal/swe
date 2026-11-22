"""The ACP channel (``remote/acp.py``, ``inject.AcpInjector``) against a
fake agent.

The fake is a small Python script speaking newline-delimited JSON-RPC
on stdio, like the fake codex in test_codex_usage_daily.py. Its
behaviour comes from ``FAKE_ACP_MODE``; every message it receives is
appended to ``FAKE_ACP_LOG`` so a test can check what the client sent.
No network, no real HOME, no real agent.
"""

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from swe.remote import acp, inject, supervise
from swe.remote.adapters import acp as acp_adapter
from swe.remote.adapters import base as acp_adapter_base
from swe.remote.adapters import events, registry
from swe.remote.adapters import fake as adapters_fake
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, StartSpec, TurnInput, Unsupported,
)
from swe.sessions.models import Session

from tests._tempdir import temp_dir
from tests.test_remote_adapter_base import ALL_CAPS, AdapterContract
from tests.test_remote_adapters import (
    _contract_order, _load_fixture, _of, _streams_close,
)

FAKE_AGENT = r'''
import json, os, sys

mode = os.environ.get("FAKE_ACP_MODE", "ok")
log = os.environ.get("FAKE_ACP_LOG")
count_file = os.environ.get("FAKE_ACP_COUNT")
starts = 0
if count_file:
    try:
        starts = int(open(count_file).read() or 0)
    except OSError:
        starts = 0
    starts += 1
    open(count_file, "w").write(str(starts))


def out(msg):
    sys.stdout.write((msg if isinstance(msg, str) else json.dumps(msg)) + "\n")
    sys.stdout.flush()


def update(sid, body):
    out({"jsonrpc": "2.0", "method": "session/update",
         "params": {"sessionId": sid, "update": body}})


def chunk(sid, what, text, mid=None):
    body = {"sessionUpdate": what, "content": {"type": "text", "text": text}}
    if mid:
        body["messageId"] = mid
    update(sid, body)


sid = "fresh-1"
prompt_id = None
for raw in sys.stdin:
    msg = json.loads(raw)
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(msg) + "\n")
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        if mode == "init-error":
            out({"jsonrpc": "2.0", "id": mid,
                 "error": {"code": -32000, "message": "auth required"}})
            continue
        out({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": mode != "no-load"},
            "agentInfo": {"name": "fake-agent", "version": "0"},
            "authMethods": []}})
    elif method == "session/load":
        sid = msg["params"]["sessionId"]
        # History replays as updates before the answer.
        chunk(sid, "user_message_chunk", "old question")
        chunk(sid, "agent_message_chunk", "OLD ANSWER")
        if mode == "load-error":
            out({"jsonrpc": "2.0", "id": mid,
                 "error": {"code": -32002, "message": "no such session"}})
        else:
            out({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/new":
        out({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid}})
    elif method == "session/prompt":
        prompt_id = mid
        if mode == "crash" or (mode == "crash-once" and starts == 1):
            sys.exit(3)
        if mode == "refuse":
            out({"jsonrpc": "2.0", "id": mid,
                 "error": {"code": -32000, "message": "quota exceeded"}})
            continue
        if mode == "malformed":
            out("not json at all")
            out("[1, 2, 3]")
            out({"jsonrpc": "2.0", "method": "session/update",
                 "params": [1, 2]})
            out({"jsonrpc": "2.0", "id": [1], "result": {}})
            update(sid, {"sessionUpdate": "agent_message_chunk",
                         "content": "not a block"})
            out({"jsonrpc": "2.0", "id": 900, "method": "fs/read_text_file",
                 "params": {"sessionId": sid, "path": "/etc/hosts"}})
            continue  # answer the prompt once the fs error comes back
        if mode == "permission":
            out({"jsonrpc": "2.0", "id": "perm-1",
                 "method": "session/request_permission", "params": {
                     "sessionId": sid,
                     "toolCall": {"toolCallId": "t1", "title": "Run ls",
                                  "rawInput": {"command": "ls -la"}},
                     "options": json.loads(os.environ["FAKE_ACP_OPTS"])
                     if os.environ.get("FAKE_ACP_OPTS") else [
                         {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                         {"optionId": "yes-all", "name": "Always allow",
                          "kind": "allow_always"},
                         {"optionId": "no", "name": "Reject", "kind": "reject_once"},
                     ]}})
            continue
        if mode in ("slow", "late-error"):
            chunk(sid, "agent_message_chunk", "working on it ")
            continue  # waits for session/cancel
        chunk(sid, "agent_thought_chunk", "pondering")
        chunk(sid, "agent_message_chunk", "hel", "m1")
        chunk(sid, "agent_message_chunk", "lo world ", "m1")
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t9",
                     "title": "Read README.md", "kind": "read",
                     "status": "pending"})
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t9",
                     "status": "completed"})
        chunk(sid, "agent_message_chunk", "done ", "m2")
        out({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    elif method == "session/cancel":
        if prompt_id is not None and mode == "late-error":
            out({"jsonrpc": "2.0", "id": prompt_id,
                 "error": {"code": -32000, "message": "provider overloaded"}})
            prompt_id = None
        elif prompt_id is not None:
            out({"jsonrpc": "2.0", "id": prompt_id,
                 "result": {"stopReason": "cancelled"}})
            prompt_id = None
    elif mid == "perm-1":
        out({"jsonrpc": "2.0", "id": prompt_id,
             "result": {"stopReason": "end_turn"}})
        prompt_id = None
    elif mid == 900:
        out({"jsonrpc": "2.0", "id": prompt_id,
             "result": {"stopReason": "end_turn"}})
        prompt_id = None
'''


class _Recorder:
    """A DeltaStream and an emit sink in one, waitable."""

    def __init__(self):
        self.cond = threading.Condition()
        self.pushes = []
        self.finished = []
        self.finish_alls = 0
        self.events = []

    def push(self, key, machine, msg_id, kind, text):
        with self.cond:
            self.pushes.append((msg_id, kind, text))
            self.cond.notify_all()

    def finish(self, key, msg_id):
        with self.cond:
            self.finished.append(msg_id)
            self.cond.notify_all()

    def finish_all(self, key):
        with self.cond:
            self.finish_alls += 1
            self.cond.notify_all()

    def emit(self, key, event):
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def wait(self, pred, timeout=10.0):
        with self.cond:
            ok = self.cond.wait_for(pred, timeout=timeout)
        assert ok, "timed out waiting for the fake agent"

    def of(self, etype):
        return [e for e in self.events if e.get("type") == etype]


def _session(path, sid="d1", tool="devin"):
    return Session(timestamp=0, agent="Devin", path=str(path),
                   session_id=sid, tool_name=tool)


class _AcpCase(unittest.TestCase):
    mode = "ok"

    def setUp(self):
        self.home = temp_dir(self)
        self.proj = self.home / "proj"
        self.proj.mkdir()
        script = self.home / "fake_acp.py"
        script.write_text(FAKE_AGENT)
        self.log = self.home / "agent.log"
        self.argv = [sys.executable, str(script)]
        self.enterContext(patch.dict(os.environ, {
            "FAKE_ACP_MODE": self.mode, "FAKE_ACP_LOG": str(self.log),
            "FAKE_ACP_COUNT": str(self.home / "starts")}))
        self.enterContext(patch.object(
            acp, "command_for", lambda name, reg=None: list(self.argv)))
        self.key = "devin:d1"
        self.session = _session(self.proj)
        self.rec = _Recorder()
        self.addCleanup(inject.AcpInjector.close, self.key)

    def _mark(self, **extra):
        supervise.mark(self.home, self.key, {
            "mode": "acp", "tool_name": "devin", "session_id": "d1",
            "cwd": str(self.proj), **extra})

    def _injector(self):
        return inject.injector_for(
            self.home, self.key, supervise.meta_for(self.home, self.key),
            emit=self.rec.emit, machine="m1", deltas=self.rec)

    def _sent(self):
        return [json.loads(line) for line in
                self.log.read_text().splitlines()] if self.log.exists() \
            else []

    def _starts(self):
        return int((self.home / "starts").read_text())


class TestPromptStreaming(_AcpCase):
    def test_registry_picks_acp(self):
        self._mark()
        self.assertIsInstance(self._injector(), inject.AcpInjector)

    def test_prompt_streams_text_thought_and_tool(self):
        self._mark()
        inj = self._injector()
        res = inj.send(self.session, {"text": "reply with ok"}, "done")
        self.assertTrue(res.ok, res.note)
        conn = inject.AcpInjector._conns[self.key]
        self.assertTrue(conn.idle.wait(10))
        self.rec.wait(lambda: self.rec.finish_alls >= 1)
        kinds = [(k, t) for _id, k, t in self.rec.pushes]
        self.assertIn(("thinking", "pondering"), kinds)
        self.assertIn(("tool_use", "Read README.md"), kinds)
        texts = "".join(t for _id, k, t in self.rec.pushes
                        if k == "assistant")
        self.assertEqual(texts, "hello world done ")
        # The replayed history of session/load never streams.
        self.assertNotIn("OLD ANSWER", texts)
        # m1 and m2 are two messages, split by the tool call.
        ids = {i for i, k, _t in self.rec.pushes if k == "assistant"}
        self.assertEqual(len(ids), 2)
        self.assertTrue(any(i.endswith(":tool:t9")
                            for i in self.rec.finished))
        sent = self._sent()
        init = next(m for m in sent if m.get("method") == "initialize")
        self.assertEqual(init["params"]["protocolVersion"], 1)
        self.assertFalse(init["params"]["clientCapabilities"]["terminal"])
        load = next(m for m in sent if m.get("method") == "session/load")
        self.assertEqual(load["params"]["sessionId"], "d1")
        self.assertTrue(os.path.isabs(load["params"]["cwd"]))
        prompt = next(m for m in sent if m.get("method") == "session/prompt")
        self.assertEqual(prompt["params"]["prompt"],
                         [{"type": "text", "text": "reply with ok"}])

    def test_real_delta_stream_carries_each_kind(self):
        from swe.remote import stream

        events = []
        deltas = stream.DeltaStream(lambda key, ev: events.append(ev),
                                    interval=0.0)
        self._mark()
        inj = inject.injector_for(
            self.home, self.key, supervise.meta_for(self.home, self.key),
            emit=self.rec.emit, machine="m1", deltas=deltas)
        self.assertTrue(inj.send(self.session, {"text": "x"}, "done").ok)
        self.assertTrue(inject.AcpInjector._conns[self.key].idle.wait(10))
        kinds = {e["data"]["kind"] for e in events
                 if e["type"] == "message.delta"}
        self.assertEqual(kinds, {"assistant", "thinking", "tool_use"})
        self.assertTrue(any(e["data"]["id"].endswith(":tool:t9")
                            and e["data"].get("done") for e in events))

    def test_second_send_reuses_the_agent(self):
        self._mark()
        inj = self._injector()
        self.assertTrue(inj.send(self.session, {"text": "a"}, "done").ok)
        inject.AcpInjector._conns[self.key].idle.wait(10)
        first = {i for i, _k, _t in self.rec.pushes}
        self.rec.pushes.clear()
        self.assertTrue(self._injector().send(
            self.session, {"text": "b"}, "done").ok)
        inject.AcpInjector._conns[self.key].idle.wait(10)
        self.assertEqual(self._starts(), 1)
        # The fake reuses messageIds m1/m2 every turn; stream ids don't.
        second = {i for i, _k, _t in self.rec.pushes}
        self.assertTrue(second)
        self.assertFalse(first & second)
        loads = [m for m in self._sent() if m.get("method") == "session/load"]
        self.assertEqual(len(loads), 1)

    def test_empty_and_detached_sends_refused(self):
        inj = inject.AcpInjector(self.home, self.key)
        self.assertFalse(inj.send(self.session, {"text": " "}, "done").ok)
        res = inj.send(self.session, {"text": "hi"}, "done")
        self.assertFalse(res.ok)
        self.assertIn("detached", res.note)

    def test_model_control_unsupported(self):
        self._mark()
        res = self._injector().control(self.session, "model", "x")
        self.assertFalse(res.ok)
        self.assertTrue(res.note.startswith("unsupported_control"))

    def test_detach_closes_the_agent(self):
        self._mark()
        inj = self._injector()
        self.assertTrue(inj.send(self.session, {"text": "a"}, "done").ok)
        conn = inject.AcpInjector._conns[self.key]
        res = inj.control(self.session, "detach", "")
        self.assertTrue(res.ok)
        self.assertNotIn(self.key, inject.AcpInjector._conns)
        self.assertIsNone(supervise.meta_for(self.home, self.key))
        self.assertIsNotNone(conn.proc.poll())


class TestNoLoad(_AcpCase):
    mode = "no-load"

    def test_new_session_recorded_and_said(self):
        self._mark()
        res = self._injector().send(self.session, {"text": "hi"}, "done")
        self.assertTrue(res.ok, res.note)
        self.assertIn("new session", res.note)
        meta = supervise.meta_for(self.home, self.key)
        self.assertEqual(meta["acp_session_id"], "fresh-1")
        self.assertFalse(any(m.get("method") == "session/load"
                             for m in self._sent()))


class TestPermission(_AcpCase):
    mode = "permission"

    def _ask(self):
        self._mark()
        inj = self._injector()
        res = inj.send(self.session, {"text": "list files"}, "done")
        self.assertTrue(res.ok, res.note)
        self.rec.wait(lambda: self.rec.of("approval.requested"))
        return inj, self.rec.of("approval.requested")[0]["data"]

    def test_request_becomes_approval_with_menu_rows(self):
        _inj, data = self._ask()
        self.assertTrue(data["request_id"].startswith("acp-"))
        self.assertIn("Run ls", data["prompt"])
        self.assertIn("ls -la", data["prompt"])
        self.assertEqual(data["options"], [
            {"n": "1", "label": "Allow", "kind": "allow_once"},
            {"n": "2", "label": "Always allow", "kind": "allow_always",
             "standing": True},
            {"n": "3", "label": "Reject", "kind": "reject_once"},
        ])

    def test_semantic_decision_round_trip(self):
        inj, data = self._ask()
        res = inj.approval(self.session, data["request_id"], "always")
        self.assertTrue(res.ok, res.note)
        conn = inject.AcpInjector._conns[self.key]
        self.assertTrue(conn.idle.wait(10))
        reply = next(m for m in self._sent() if m.get("id") == "perm-1")
        self.assertEqual(reply["result"]["outcome"],
                         {"outcome": "selected", "optionId": "yes-all"})
        # A retried answer can't reach the agent twice.
        again = inj.approval(self.session, data["request_id"], "allow")
        self.assertFalse(again.ok)

    def test_menu_row_and_unknown_row(self):
        inj, data = self._ask()
        self.assertFalse(inj.approval(
            self.session, data["request_id"], "7").ok)
        self.assertFalse(inj.approval(self.session, "acp-nope", "1").ok)
        self.assertFalse(inj.approval(
            self.session, data["request_id"], "maybe").ok)
        res = inj.approval(self.session, data["request_id"], "3")
        self.assertTrue(res.ok, res.note)
        inject.AcpInjector._conns[self.key].idle.wait(10)
        reply = next(m for m in self._sent() if m.get("id") == "perm-1")
        self.assertEqual(reply["result"]["outcome"]["optionId"], "no")

    def test_interrupt_cancels_and_retires_the_request(self):
        inj, data = self._ask()
        res = inj.control(self.session, "interrupt", "")
        self.assertTrue(res.ok, res.note)
        self.rec.wait(lambda: self.rec.of("approval.resolved"))
        resolved = self.rec.of("approval.resolved")[0]["data"]
        self.assertEqual(resolved, {"request_id": data["request_id"],
                                    "source": "cancelled"})
        # Closing waits for the agent to exit, so it has logged all it read.
        inject.AcpInjector.close(self.key)
        sent = self._sent()
        self.assertTrue(any(m.get("method") == "session/cancel"
                            for m in sent))
        reply = next(m for m in sent if m.get("id") == "perm-1")
        self.assertEqual(reply["result"]["outcome"],
                         {"outcome": "cancelled"})


class TestCancel(_AcpCase):
    mode = "slow"

    def test_interrupt_ends_the_turn(self):
        self._mark()
        inj = self._injector()
        self.assertEqual(inj.send(self.session, {"text": "go"}, "done").note,
                         "turn started")
        conn = inject.AcpInjector._conns[self.key]
        self.assertFalse(conn.idle.is_set())
        self.assertTrue(inj.control(self.session, "interrupt", "").ok)
        self.assertTrue(conn.idle.wait(10))
        self.assertEqual(conn.last_stop, "cancelled")
        self.rec.wait(lambda: self.rec.finish_alls >= 1)
        # Nothing running: a second interrupt says so.
        self.assertFalse(inj.control(self.session, "interrupt", "").ok)

    def test_queue_waits_for_the_turn_too(self):
        # A harness with no status probe reads "" mid-turn, so the hub
        # lets a queued send through: it must wait here, not fail.
        self._mark()
        inj = self._injector()
        self.assertTrue(inj.send(self.session, {"text": "go"}, "done").ok)
        conn = inject.AcpInjector._conns[self.key]
        real_wait = conn.idle.wait
        waiting = threading.Event()

        def spy(timeout=None):
            waiting.set()
            return real_wait(timeout)

        conn.idle.wait = spy
        out = {}
        t = threading.Thread(target=lambda: out.setdefault(
            "r", inj.send(self.session, {"text": "more", "mode": "queue"},
                          "")))
        t.start()
        self.assertTrue(waiting.wait(10))
        inj.control(self.session, "interrupt", "")
        t.join(15)
        self.assertTrue(out["r"].ok, out["r"].note)
        self.assertFalse(out["r"].degraded)

    def test_steer_waits_for_the_turn_then_sends(self):
        self._mark()
        inj = self._injector()
        self.assertTrue(inj.send(self.session, {"text": "go"}, "done").ok)
        conn = inject.AcpInjector._conns[self.key]
        real_wait = conn.idle.wait
        waiting = threading.Event()

        def spy(timeout=None):
            waiting.set()
            return real_wait(timeout)

        conn.idle.wait = spy
        out = {}
        t = threading.Thread(target=lambda: out.setdefault(
            "r", inj.send(self.session, {"text": "next", "mode": "steer"},
                          "active")))
        t.start()
        self.assertTrue(waiting.wait(10))
        inj.control(self.session, "interrupt", "")
        t.join(15)
        self.assertTrue(out["r"].ok, out["r"].note)
        self.assertTrue(out["r"].degraded)
        inject.AcpInjector.close(self.key)
        prompts = [m for m in self._sent()
                   if m.get("method") == "session/prompt"]
        self.assertEqual(len(prompts), 2)


class TestMalformed(_AcpCase):
    mode = "malformed"

    def test_garbage_is_skipped_and_fs_request_refused(self):
        self._mark()
        inj = self._injector()
        self.assertTrue(inj.send(self.session, {"text": "x"}, "done").ok)
        conn = inject.AcpInjector._conns[self.key]
        self.assertTrue(conn.idle.wait(10))
        self.assertTrue(conn.alive())
        self.assertEqual(conn.last_stop, "end_turn")
        err = next(m for m in self._sent() if m.get("id") == 900)
        self.assertEqual(err["error"]["code"], -32601)
        self.assertEqual(self.rec.pushes, [])


class TestLateError(_AcpCase):
    mode = "late-error"

    def test_error_after_the_early_window_is_a_notify_event(self):
        self.enterContext(patch.object(inject, "_ACP_EARLY_S", 0.05))
        self._mark()
        inj = self._injector()
        res = inj.send(self.session, {"text": "go"}, "done")
        self.assertEqual(res.note, "turn started")
        inject.AcpInjector._conns[self.key].cancel()
        self.rec.wait(lambda: self.rec.of("notify"))
        data = self.rec.of("notify")[0]["data"]
        self.assertEqual(data["kind"], "error")
        self.assertIn("provider overloaded", data["message"])


class TestDetachRace(_AcpCase):
    def test_detach_during_the_handshake_wins(self):
        self._mark()
        real = acp.Connection.handshake

        def detach_midway(conn, ref, **kw):
            supervise.unmark(self.home, self.key)
            return real(conn, ref, **kw)

        with patch.object(acp.Connection, "handshake", detach_midway):
            res = self._injector().send(self.session, {"text": "x"}, "done")
        self.assertFalse(res.ok)
        self.assertIn("detached", res.note)
        self.assertNotIn(self.key, inject.AcpInjector._conns)
        self.assertFalse(any(m.get("method") == "session/prompt"
                             for m in self._sent()))


class TestRefusedPrompt(_AcpCase):
    mode = "refuse"

    def setUp(self):
        super().setUp()
        # Generous: the refusal must land inside send's early window.
        self.enterContext(patch.object(inject, "_ACP_EARLY_S", 10.0))

    def test_early_error_reported(self):
        self._mark()
        res = self._injector().send(self.session, {"text": "x"}, "done")
        self.assertFalse(res.ok)
        self.assertIn("quota exceeded", res.note)


class TestCrash(_AcpCase):
    mode = "crash-once"

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(inject, "_ACP_EARLY_S", 10.0))

    def test_crash_mid_turn_then_restart_loads_the_session(self):
        self._mark()
        inj = self._injector()
        res = inj.send(self.session, {"text": "x"}, "done")
        self.assertFalse(res.ok)
        self.assertIn("exited", res.note)
        conn = inject.AcpInjector._conns[self.key]
        self.assertTrue(conn.idle.wait(10))
        self.rec.wait(lambda: self.rec.finish_alls >= 1)
        # Next send restarts the agent and loads the same session.
        res = self._injector().send(self.session, {"text": "y"}, "done")
        self.assertTrue(res.ok, res.note)
        self.assertIn("restarted", res.note)
        self.assertEqual(self._starts(), 2)
        loads = [m["params"]["sessionId"] for m in self._sent()
                 if m.get("method") == "session/load"]
        self.assertEqual(loads, ["d1", "d1"])


class TestCrashWithApproval(_AcpCase):
    mode = "permission"

    def test_agent_death_retires_open_requests(self):
        self._mark()
        inj = self._injector()
        inj.send(self.session, {"text": "x"}, "done")
        self.rec.wait(lambda: self.rec.of("approval.requested"))
        conn = inject.AcpInjector._conns[self.key]
        conn.proc.kill()
        self.rec.wait(lambda: self.rec.of("approval.resolved"))
        self.assertEqual(self.rec.of("approval.resolved")[0]["data"]["source"],
                         "channel_closed")
        res = inj.approval(
            self.session,
            self.rec.of("approval.requested")[0]["data"]["request_id"],
            "allow")
        self.assertFalse(res.ok)


class TestAttach(_AcpCase):
    def _attach(self, mode="auto", **kw):
        kw.setdefault("last_append_at", None)
        kw.setdefault("status", "done")
        with patch.object(supervise, "_local_writer_pids", return_value=[]), \
             patch.object(acp, "auto_eligible", return_value=True):
            return supervise.attach(self.home, self.key, self.session,
                                    mode=mode, **kw)

    def test_auto_attaches_over_acp_and_keeps_the_agent(self):
        ok, note = self._attach()
        self.assertTrue(ok, note)
        self.assertIn("acp", note)
        meta = supervise.meta_for(self.home, self.key)
        self.assertEqual(meta["mode"], "acp")
        self.assertNotIn("acp_session_id", meta)
        conn = inject.AcpInjector._conns[self.key]
        self.assertTrue(conn.alive())
        # The first send reuses the agent attach started.
        self.assertTrue(self._injector().send(
            self.session, {"text": "hi"}, "done").ok)
        self.assertEqual(self._starts(), 1)

    def test_model_pick_keeps_auto_off_acp(self):
        with patch.object(acp, "auto_eligible", return_value=True):
            self.assertEqual(supervise.resolve_mode(
                self.session, "auto", model="x"), "tmux")
            self.assertEqual(supervise.resolve_mode(
                self.session, "auto"), "acp")

    def test_explicit_acp_refuses_a_model(self):
        ok, note = self._attach(mode="acp", model="gpt")
        self.assertFalse(ok)
        self.assertIn("/model", note)


class TestAttachNoLoad(_AcpCase):
    mode = "no-load"

    def test_auto_takes_tmux_when_the_agent_cannot_load(self):
        # The chat tails the attached transcript; a new ACP session
        # would never write to it.
        with patch.object(supervise, "_local_writer_pids", return_value=[]), \
             patch.object(acp, "auto_eligible", return_value=True), \
             patch.object(supervise, "_attach_tmux",
                          return_value=(True, "attached under tmux swe-x")):
            ok, note = supervise.attach(
                self.home, self.key, self.session, mode="auto",
                last_append_at=None, status="done")
        self.assertTrue(ok)
        self.assertIn("can't load sessions", note)
        self.assertIn("fell back to tmux", note)
        self.assertFalse(any(m.get("method") == "session/new"
                             for m in self._sent()))

    def test_explicit_acp_takes_tmux_too_and_says_why(self):
        # A forced --mode=acp used to run session/new here: the chat
        # kept tailing the attached transcript, which that new session
        # never wrote, so replies went nowhere the chat could follow.
        with patch.object(supervise, "_local_writer_pids", return_value=[]), \
             patch.object(supervise, "_attach_tmux",
                          return_value=(True, "attached under tmux swe-x")) \
                as tmux:
            ok, note = supervise.attach(
                self.home, self.key, self.session, mode="acp",
                last_append_at=None, status="done")
        self.assertTrue(ok, note)
        self.assertEqual(
            note, "attached under tmux swe-x (fake-agent can't load "
                  "sessions over ACP, and a new ACP session would not "
                  "write to this chat; fell back to tmux)")
        self.assertEqual(tmux.call_args[0][4]["mode"], "tmux")
        self.assertNotIn("acp_session_id", tmux.call_args[0][4])
        self.assertFalse(any(m.get("method") == "session/new"
                             for m in self._sent()))
        self.assertNotIn(self.key, inject.AcpInjector._conns)

    def test_explicit_acp_fallback_failure_keeps_both_reasons(self):
        with patch.object(supervise, "_local_writer_pids", return_value=[]), \
             patch.object(supervise, "_attach_tmux",
                          return_value=(False, "tmux is not available")):
            ok, note = supervise.attach(
                self.home, self.key, self.session, mode="acp",
                last_append_at=None, status="done")
        self.assertFalse(ok)
        self.assertIn("tmux is not available", note)
        self.assertIn("can't load sessions", note)
        self.assertIsNone(supervise.meta_for(self.home, self.key))


class TestAttachFallback(_AcpCase):
    mode = "init-error"

    def test_auto_falls_back_to_tmux(self):
        with patch.object(supervise, "_local_writer_pids", return_value=[]), \
             patch.object(acp, "auto_eligible", return_value=True), \
             patch.object(supervise, "_attach_tmux",
                          return_value=(True, "attached under tmux swe-x")) \
                as tmux:
            ok, note = supervise.attach(
                self.home, self.key, self.session, mode="auto",
                last_append_at=None, status="done")
        self.assertTrue(ok)
        self.assertIn("fell back to tmux", note)
        self.assertIn("auth required", note)
        self.assertEqual(tmux.call_args[0][4]["mode"], "tmux")
        self.assertNotIn(self.key, inject.AcpInjector._conns)

    def test_explicit_acp_reports_the_failure(self):
        with patch.object(supervise, "_local_writer_pids", return_value=[]):
            ok, note = supervise.attach(
                self.home, self.key, self.session, mode="acp",
                last_append_at=None, status="done")
        self.assertFalse(ok)
        self.assertIn("handshake failed", note)
        self.assertIsNone(supervise.meta_for(self.home, self.key))


class TestAttachLoadError(_AcpCase):
    mode = "load-error"

    def test_failed_load_falls_back(self):
        with patch.object(supervise, "_local_writer_pids", return_value=[]), \
             patch.object(acp, "auto_eligible", return_value=True), \
             patch.object(supervise, "_attach_tmux",
                          return_value=(True, "attached under tmux swe-x")):
            ok, note = supervise.attach(
                self.home, self.key, self.session, mode="auto",
                last_append_at=None, status="done")
        self.assertTrue(ok)
        self.assertIn("no such session", note)


class TestHubRoute(unittest.TestCase):
    def test_acp_mode_without_an_acp_cli_is_422(self):
        from tests.test_remote_hub import _Server, _session as hub_session

        srv = _Server(sessions=[hub_session(tool="devin", sid="d7")],
                      statuses=lambda ss: ["done"])
        self.addCleanup(srv.stop)
        cred = srv.pair()
        with patch.object(acp, "command_for", return_value=None):
            status, data = srv.request(
                "POST", "/v1/sessions/devin:d7/control",
                body={"op": "attach", "mode": "acp"},
                headers={"Authorization": f"Bearer {cred['credential']}"})
        self.assertEqual(status, 422)
        body = json.loads(data)["error"]
        self.assertEqual(body["code"], "bad_mode")
        self.assertIn("Agent Client Protocol", body["message"])


class _LaunchCase(_AcpCase):
    """``supervise.launch_new`` for an ACP-speaking harness."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(acp, "auto_eligible",
                                       return_value=True))
        self.tmux = self.enterContext(patch.object(
            supervise, "_launch_new_tmux",
            return_value=(True, "launched under tmux; the session "
                                "appears on refresh", "qv-x")))
        self.new_key = "devin:fresh-1"
        self.addCleanup(inject.AcpInjector.close, self.new_key)

    def _launch(self, **kw):
        kw.setdefault("known_ids", ["old"])
        kw.setdefault("launch_id", "l1")
        return supervise.launch_new(
            self.home, "devin", str(self.proj), "reply with ok",
            binary=sys.executable, **kw)

    def _markers(self):
        found = supervise._pending_dir(self.home).glob("*.json")
        return [json.loads(p.read_text()) for p in found]

    def _adopt(self, sid="fresh-1"):
        row = (f"devin:{sid}", _session(self.proj, sid=sid), "active")
        return supervise.adopt_pending(self.home, [row])


class TestLaunch(_LaunchCase):
    def test_launch_runs_session_new_and_the_first_prompt(self):
        ok, note, name = self._launch()
        self.assertTrue(ok, note)
        self.assertIn("over ACP", note)
        self.assertIsNone(name)
        self.tmux.assert_not_called()
        conn = inject.AcpInjector._conns[self.new_key]
        self.assertTrue(conn.idle.wait(10))
        sent = self._sent()
        self.assertFalse(any(m.get("method") == "session/load"
                             for m in sent))
        new = next(m for m in sent if m.get("method") == "session/new")
        self.assertEqual(new["params"]["cwd"], os.path.realpath(self.proj))
        prompt = next(m for m in sent if m.get("method") == "session/prompt")
        self.assertEqual(prompt["params"]["sessionId"], "fresh-1")
        self.assertEqual(prompt["params"]["prompt"][0]["text"],
                         "reply with ok")
        [marker] = self._markers()
        self.assertEqual(marker["mode"], "acp")
        self.assertEqual(marker["expect_id"], "fresh-1")
        self.assertEqual(marker["exclude"], ["old"])

    def test_adoption_binds_the_session_as_acp_and_reuses_the_agent(self):
        self.assertTrue(self._launch()[0])
        inject.AcpInjector._conns[self.new_key].idle.wait(10)
        # Another new session in the folder is not ours: expect_id.
        self.assertEqual(self._adopt("someone-else"), [])
        self.assertEqual(self._adopt(), [self.new_key])
        meta = supervise.meta_for(self.home, self.new_key)
        self.assertEqual(meta["mode"], "acp")
        self.assertEqual(meta["launch_id"], "l1")
        self.assertEqual(meta["tmux"], "")
        self.assertEqual(self._markers(), [])
        inj = inject.injector_for(self.home, self.new_key, meta,
                                  emit=self.rec.emit, machine="m1",
                                  deltas=self.rec)
        self.assertIsInstance(inj, inject.AcpInjector)
        res = inj.send(_session(self.proj, sid="fresh-1"),
                       {"text": "again"}, "done")
        self.assertTrue(res.ok, res.note)
        self.assertEqual(self._starts(), 1)

    def test_a_model_pick_keeps_the_tmux_launch(self):
        self.assertTrue(self._launch(model="gpt")[0])
        self.tmux.assert_called_once()
        self.assertFalse((self.home / "starts").exists())

    def test_the_folder_frees_once_the_session_id_is_known(self):
        self.assertTrue(self._launch()[0])
        inject.AcpInjector._conns[self.new_key].idle.wait(10)
        # The marker names its session (expect_id), so adoption can't
        # confuse it with another launch here: it blocks nothing.
        self.assertIsNone(supervise.fork_blocker(
            self.home, "devin", str(self.proj)))

    def test_a_marker_still_handshaking_adopts_nothing(self):
        folder = supervise._pending_dir(self.home)
        folder.mkdir(parents=True, exist_ok=True)
        marker = {"tmux": "acp-x", "mode": "acp", "tool_name": "devin",
                  "cwd": str(self.proj), "exclude": [], "launch_id": "l2",
                  "launched_at": time.time() - 5}
        (folder / "acp-x.json").write_text(json.dumps(marker))
        theirs = Session(timestamp=time.time() * 1000, agent="Devin",
                         path=str(self.proj), session_id="theirs",
                         tool_name="devin")
        rows = [("devin:theirs", theirs, "active")]
        # A person's new session in the folder, mid-handshake: not ours.
        with patch("swe.remote.tail.resolve_jsonl_path", return_value=None):
            self.assertEqual(supervise.adopt_pending(self.home, rows), [])
            # The same marker as a tmux launch's would have taken it.
            del marker["mode"]
            (folder / "acp-x.json").write_text(json.dumps(marker))
            self.assertEqual(supervise.adopt_pending(self.home, rows),
                             ["devin:theirs"])

    def test_held_events_flush_before_live_ones(self):
        self.assertTrue(self._launch()[0])
        conn = inject.AcpInjector._conns[self.new_key]
        conn.idle.wait(10)
        conn._emit("approval.requested", {"request_id": "a"})
        conn._emit("approval.requested", {"request_id": "b"})
        seen = []

        def emit(key, ev):
            seen.append(ev["data"]["request_id"])
            if ev["data"]["request_id"] == "a":
                # The reader resolving "a" mid-flush must queue behind b.
                conn._emit("approval.resolved", {"request_id": "a-done"})

        conn.bind(self.new_key, "m1", emit, None)
        self.assertEqual(seen, ["a", "b", "a-done"])
        conn._emit("notify", {"request_id": "live"})
        self.assertEqual(seen[-1], "live")

    def test_eligibility_rules(self):
        self.assertTrue(supervise.acp_launch_eligible("devin", "hi"))
        # Autonomy is honoured on the channel itself.
        self.assertTrue(supervise.acp_launch_eligible(
            "devin", "hi", yolo=True))
        for kw in ({"model": "m"}, {"effort": "high"},
                   {"provider": "groq"}, {"tracked": False}):
            self.assertFalse(supervise.acp_launch_eligible(
                "devin", "hi", **kw), kw)
        self.assertFalse(supervise.acp_launch_eligible("devin", "  "))


class TestLaunchApproval(_LaunchCase):
    mode = "permission"

    def test_first_turn_approval_waits_for_the_tailer(self):
        asked = threading.Event()
        real = acp.Connection._permission

        def spy(conn, rpc_id, params):
            real(conn, rpc_id, params)
            asked.set()

        with patch.object(acp.Connection, "_permission", spy):
            self.assertTrue(self._launch()[0])
            self.assertTrue(asked.wait(10))
        conn = inject.AcpInjector._conns[self.new_key]
        # The request came in, but nobody listens: it waits.
        self.assertTrue(conn.approvals)
        self.assertEqual(self.rec.events, [])
        inject.AcpInjector.release_held({"other:key": 1}, self.rec.emit)
        self.assertEqual(self.rec.events, [])
        inject.AcpInjector.release_held(
            {self.new_key: 1}, self.rec.emit, "m1", self.rec)
        [ev] = self.rec.of("approval.requested")
        self.assertEqual(ev["session"], self.new_key)
        self._adopt()
        inj = inject.injector_for(
            self.home, self.new_key,
            supervise.meta_for(self.home, self.new_key),
            emit=self.rec.emit, machine="m1", deltas=self.rec)
        res = inj.approval(_session(self.proj, sid="fresh-1"),
                           ev["data"]["request_id"], "allow")
        self.assertTrue(res.ok, res.note)
        self.assertTrue(conn.idle.wait(10))


class TestLaunchAutonomy(_LaunchCase):
    mode = "permission"

    def _released(self):
        conn = inject.AcpInjector._conns[self.new_key]
        self.assertTrue(conn.idle.wait(10) or conn.approvals)
        inject.AcpInjector.release_held(
            {self.new_key: 1}, self.rec.emit, "m1", self.rec)
        return conn

    def _reply(self):
        inject.AcpInjector.close(self.new_key)
        return next(m for m in self._sent() if m.get("id") == "perm-1")

    def test_autonomy_launch_runs_over_acp(self):
        ok, note, name = self._launch(yolo=True)
        self.assertTrue(ok, note)
        self.assertIn("over ACP", note)
        self.tmux.assert_not_called()
        [marker] = self._markers()
        self.assertTrue(marker["yolo"])
        self.assertEqual(self._adopt(), [self.new_key])
        self.assertTrue(supervise.meta_for(self.home, self.new_key)["yolo"])

    def test_allow_once_is_picked_by_kind_and_journaled(self):
        # "always" first, and named like a plain allow: kind decides.
        opts = [{"optionId": "always", "name": "Allow",
                 "kind": "allow_always"},
                {"optionId": "once", "name": "Yes, this time",
                 "kind": "allow_once"},
                {"optionId": "no", "name": "No", "kind": "reject_once"}]
        self.enterContext(patch.dict(os.environ,
                                     {"FAKE_ACP_OPTS": json.dumps(opts)}))
        self.assertTrue(self._launch(yolo=True)[0])
        conn = self._released()
        self.assertTrue(conn.idle.wait(10))
        self.assertEqual(conn.approvals, {})
        [req] = self.rec.of("approval.requested")
        [res] = self.rec.of("approval.resolved")
        self.assertTrue(req["data"]["auto"])
        self.assertIn("Run ls", req["data"]["prompt"])
        self.assertEqual(res["data"], {"request_id": req["data"]["request_id"],
                                       "decision": "allow",
                                       "source": "autonomy"})
        self.assertEqual(self._reply()["result"]["outcome"],
                         {"outcome": "selected", "optionId": "once"})

    def test_no_allow_once_falls_back_to_the_card(self):
        opts = [{"optionId": "always", "name": "Always",
                 "kind": "allow_always"},
                {"optionId": "no", "name": "No", "kind": "reject_once"}]
        self.enterContext(patch.dict(os.environ,
                                     {"FAKE_ACP_OPTS": json.dumps(opts)}))
        self.assertTrue(self._launch(yolo=True)[0])
        conn = self._released()
        self.rec.wait(lambda: self.rec.of("approval.requested"))
        [req] = self.rec.of("approval.requested")
        self.assertNotIn("auto", req["data"])
        self.assertEqual(self.rec.of("approval.resolved"), [])
        self.assertEqual(len(conn.approvals), 1)

    def test_autonomy_off_keeps_the_card(self):
        self.assertTrue(self._launch()[0])
        conn = self._released()
        self.rec.wait(lambda: self.rec.of("approval.requested"))
        self.assertNotIn("auto", self.rec.of("approval.requested")[0]["data"])
        self.assertEqual(len(conn.approvals), 1)
        self.assertFalse(conn.auto_allow)

    def test_a_restarted_agent_keeps_autonomy(self):
        self.assertTrue(self._launch(yolo=True)[0])
        self._adopt()
        inject.AcpInjector.close(self.new_key)
        inj = inject.injector_for(
            self.home, self.new_key,
            supervise.meta_for(self.home, self.new_key),
            emit=self.rec.emit, machine="m1", deltas=self.rec)
        res = inj.send(_session(self.proj, sid="fresh-1"),
                       {"text": "again"}, "done")
        self.assertTrue(res.ok, res.note)
        self.assertTrue(inject.AcpInjector._conns[self.new_key].auto_allow)


class TestAutonomyEdges(_LaunchCase):
    mode = "slow"

    def test_a_full_hold_queue_still_keeps_a_card(self):
        self.assertTrue(self._launch(yolo=True)[0])
        conn = inject.AcpInjector._conns[self.new_key]
        for i in range(40):
            conn._emit("approval.requested",
                       {"request_id": f"a{i}", "auto": True})
            conn._emit("approval.resolved",
                       {"request_id": f"a{i}", "source": "autonomy"})
        conn._emit("approval.requested", {"request_id": "card"})
        seen = []
        conn.bind(self.new_key, "m1",
                  lambda key, ev: seen.append(ev["data"]["request_id"]),
                  None)
        self.assertIn("card", seen)
        self.assertEqual(seen[-1], "card")

    def test_no_grant_after_stop(self):
        self.assertTrue(self._launch(yolo=True)[0])
        conn = inject.AcpInjector._conns[self.new_key]
        self.assertTrue(conn.cancel())
        answers = []
        with patch.object(conn, "_respond",
                          lambda rpc_id, result=None, error=None:
                          answers.append((rpc_id, result)) or True):
            # A request the reader had in hand when Stop landed.
            conn._permission("late", {
                "sessionId": conn.session_id,
                "toolCall": {"title": "rm -rf build"},
                "options": [{"optionId": "once", "name": "Allow",
                             "kind": "allow_once"}]})
        self.assertEqual(answers,
                         [("late", {"outcome": {"outcome": "cancelled"}})])
        self.assertEqual(conn.approvals, {})


class TestLaunchCancel(_LaunchCase):
    mode = "slow"

    def test_interrupt_a_launched_turn(self):
        self.assertTrue(self._launch()[0])
        self._adopt()
        conn = inject.AcpInjector._conns[self.new_key]
        self.assertFalse(conn.idle.is_set())
        inj = inject.injector_for(
            self.home, self.new_key,
            supervise.meta_for(self.home, self.new_key),
            emit=self.rec.emit, machine="m1", deltas=self.rec)
        res = inj.control(_session(self.proj, sid="fresh-1"),
                          "interrupt", "")
        self.assertTrue(res.ok, res.note)
        self.assertTrue(conn.idle.wait(10))
        self.assertEqual(conn.last_stop, "cancelled")


class TestLaunchCrashFallback(_LaunchCase):
    mode = "crash"

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(inject, "_ACP_EARLY_S", 10.0))

    def test_agent_dying_on_the_first_prompt_falls_back_to_tmux(self):
        ok, note, name = self._launch()
        self.assertTrue(ok)
        self.assertEqual(name, "qv-x")
        self.assertIn("exited on the first prompt", note)
        self.assertIn("fell back to tmux", note)
        self.tmux.assert_called_once()
        # The dead agent's session may have a transcript: never adopted.
        self.assertEqual(self.tmux.call_args[1]["known_ids"],
                         ["old", "fresh-1"])
        self.assertNotIn(self.new_key, inject.AcpInjector._conns)
        self.assertEqual(self._markers(), [])


class TestLaunchInitFallback(_LaunchCase):
    mode = "init-error"

    def test_initialize_failure_falls_back_to_tmux(self):
        ok, note, _name = self._launch()
        self.assertTrue(ok)
        self.assertIn("auth required", note)
        self.assertIn("fell back to tmux", note)
        self.tmux.assert_called_once()
        self.assertEqual(self._markers(), [])

    def test_initialize_timeout_falls_back_to_tmux(self):
        self.enterContext(patch.object(supervise, "LAUNCH_ACP_S", 0.0))
        ok, note, _name = self._launch()
        self.assertTrue(ok)
        self.assertIn("too long", note)
        self.tmux.assert_called_once()


class TestLaunchRefused(_LaunchCase):
    mode = "refuse"

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(inject, "_ACP_EARLY_S", 10.0))

    def test_a_refused_first_prompt_fails_without_tmux(self):
        ok, note, _name = self._launch()
        self.assertFalse(ok)
        self.assertIn("quota exceeded", note)
        self.tmux.assert_not_called()
        self.assertEqual(self._markers(), [])


class TestHubLaunch(unittest.TestCase):
    def test_fallback_note_reaches_the_launch_poll(self):
        from tests.test_remote_hub import _Server

        srv = _Server(sessions=[], statuses=lambda ss: [])
        self.addCleanup(srv.stop)
        hdr = {"Authorization": f"Bearer {srv.pair()['credential']}"}
        proj = temp_dir(self)
        note = ("launched under tmux; the session appears on refresh "
                "(ACP handshake failed: auth required; fell back to tmux)")
        with patch("swe.remote.catalog.launch_command",
                   return_value="devin"), \
             patch("swe.remote.catalog.launchable_harnesses",
                   return_value=[]), \
             patch("swe.remote.catalog.untracked", return_value=[]), \
             patch("swe.remote.catalog.known_session_ids",
                   return_value=[]), \
             patch("swe.remote.supervise.tmux_bin",
                   return_value="/usr/bin/tmux"), \
             patch.object(acp, "auto_eligible", return_value=True), \
             patch("swe.remote.supervise.launch_new",
                   return_value=(True, note, "qv-1")):
            status, data = srv.request(
                "POST", "/v1/sessions",
                body={"harness": "devin", "cwd": str(proj),
                      "prompt": "hi"}, headers=hdr)
            for t in threading.enumerate():
                if t.name.startswith("remote-launch-"):
                    t.join(timeout=5)
        lid = json.loads(data)["launch_id"]
        _, view = srv.request("GET", f"/v1/launches/{lid}", headers=hdr)
        view = json.loads(view)
        self.assertEqual(view["state"], "pending")
        self.assertIn("fell back to tmux", view["note"])

    def test_acp_launch_needs_no_tmux_and_says_so(self):
        from tests.test_remote_hub import _Server

        srv = _Server(sessions=[], statuses=lambda ss: [])
        self.addCleanup(srv.stop)
        cred = srv.pair()
        proj = temp_dir(self)
        with patch("swe.remote.catalog.launch_command",
                   return_value="devin"), \
             patch("swe.remote.catalog.launchable_harnesses",
                   return_value=[]), \
             patch("swe.remote.catalog.untracked", return_value=[]), \
             patch("swe.remote.catalog.known_session_ids",
                   return_value=[]), \
             patch("swe.remote.supervise.tmux_bin", return_value=None), \
             patch.object(acp, "auto_eligible", return_value=True), \
             patch("swe.remote.supervise.launch_new",
                   return_value=(True, "launched over ACP", None)) as run:
            status, data = srv.request(
                "POST", "/v1/sessions",
                body={"harness": "devin", "cwd": str(proj),
                      "prompt": "hi"},
                headers={"Authorization": f"Bearer {cred['credential']}"})
            for t in threading.enumerate():
                if t.name.startswith("remote-launch-"):
                    t.join(timeout=5)
        self.assertEqual(status, 202, data)
        body = json.loads(data)
        self.assertEqual(body["note"], "starting over ACP")
        self.assertEqual(set(body), {"ok", "launch_id", "state", "note"})
        run.assert_called_once()


class TestPermissionPrompt(unittest.TestCase):
    def test_devin_shape_uses_the_meta_command(self):
        # What devin acp sends: only the id and vendor _meta.
        tool = {"toolCallId": "exec:0#a2",
                "_meta": {"cognition.ai/editableCommand": "rm auto.txt",
                          "cognition.ai/permissionRule": "Exec(rm)"}}
        self.assertEqual(acp._permission_prompt(tool, {}),
                         "Run a command\n\n`rm auto.txt`")

    def test_an_earlier_tool_call_names_it(self):
        known = {"t1": {"title": "Delete auto.txt",
                        "rawInput": {"command": "rm auto.txt"}}}
        self.assertEqual(acp._permission_prompt({"toolCallId": "t1"}, known),
                         "Delete auto.txt\n\n`rm auto.txt`")

    def test_spec_shape_and_nothing_at_all(self):
        self.assertEqual(acp._permission_prompt(
            {"toolCallId": "t", "title": "Edit a.py",
             "rawInput": {"path": "/x/a.py"}}, {}),
            "Edit a.py\n\n`/x/a.py`")
        self.assertEqual(acp._permission_prompt(None, {}),
                         "permission requested")

    def test_tool_updates_are_remembered_even_before_a_sink(self):
        conn = acp.Connection.__new__(acp.Connection)
        conn.replaying, conn.session_id, conn.deltas, conn.key = \
            False, "s", None, ""
        conn._tool_info = {}
        conn._update({"sessionId": "s", "update": {
            "sessionUpdate": "tool_call", "toolCallId": "t2",
            "title": "Run ls", "rawInput": {"command": "ls"}}})
        self.assertEqual(conn._tool_info["t2"]["title"], "Run ls")
        # A partial update keeps what the first said, and adds to it.
        conn._update({"sessionId": "s", "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "t2",
            "status": "in_progress", "title": None}})
        conn._update({"sessionId": "s", "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "t2",
            "rawInput": {"command": "ls -la"}}})
        self.assertEqual(conn._tool_info["t2"],
                         {"title": "Run ls", "rawInput": {"command": "ls -la"}})
        self.assertEqual(
            acp._permission_prompt({"toolCallId": "t2"}, conn._tool_info),
            "Run ls\n\n`ls -la`")

    def test_the_tool_table_stays_bounded_oldest_first(self):
        conn = acp.Connection.__new__(acp.Connection)
        conn._tool_info = {}
        for i in range(acp._TOOL_INFO_MAX + 10):
            conn._remember_tool({"toolCallId": f"t{i}", "title": f"n{i}"})
        self.assertEqual(len(conn._tool_info), acp._TOOL_INFO_MAX)
        self.assertNotIn("t0", conn._tool_info)
        self.assertIn(f"t{acp._TOOL_INFO_MAX + 9}", conn._tool_info)


class TestDetection(unittest.TestCase):
    """``command_for``: harness.json first, then the --help probe."""

    def setUp(self):
        self.bin = temp_dir(self)
        self.enterContext(patch.dict(
            os.environ, {"PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin"}))
        acp._PROBES.clear()
        self.addCleanup(acp._PROBES.clear)

    def _tool(self, name, help_text):
        # A Python stub on this interpreter: PATH here is trimmed to
        # /usr/bin:/bin, and the Nix build sandbox has neither (no cat), so
        # a shell stub printed nothing and every probe read as "no ACP".
        path = self.bin / name
        runs = str(self.bin / f"{name}.runs")
        path.write_text(
            f"#!{sys.executable}\n"
            f"with open({runs!r}, 'a') as f:\n    f.write('x\\n')\n"
            f"print({help_text!r})\n")
        path.chmod(0o755)
        return path

    def _runs(self, name):
        p = self.bin / f"{name}.runs"
        return len(p.read_text().splitlines()) if p.exists() else 0

    def test_subcommand_found_and_cached(self):
        self._tool("devin", "Commands:\n  acp   Run as an ACP server\n")
        self.assertEqual(acp.command_for("devin", reg={}), ["devin", "acp"])
        self.assertEqual(acp.command_for("devin", reg={}), ["devin", "acp"])
        self.assertEqual(self._runs("devin"), 1)
        self.assertTrue(acp.auto_eligible("devin", reg={}))

    def test_cli_without_acp(self):
        self._tool("devin", "Commands:\n  acp-ish  nope\n  --acpx\n")
        self.assertIsNone(acp.command_for("devin", reg={}))
        self.assertIsNone(acp.command_for("devin", reg={}))

    def test_flag_candidates_in_order(self):
        self._tool("gemini", "Options:\n  --experimental-acp  start ACP\n")
        self.assertEqual(acp.command_for("gemini", reg={}),
                         ["gemini", "--experimental-acp"])

    def test_not_installed_and_native_channels(self):
        self.assertIsNone(acp.command_for("devin", reg={}))
        self._tool("codex", "Commands:\n  acp\n")
        self.assertIsNone(acp.command_for("codex", reg={}))

    def test_claude_adapter_is_opt_in(self):
        self.assertIsNone(acp.command_for("claude", reg={}))
        adapter = self._tool("claude-agent-acp", "")
        self.assertEqual(acp.command_for("claude", reg={}),
                         ["claude-agent-acp"])
        # An ACP server is never started with --help.
        self.assertEqual(self._runs("claude-agent-acp"), 0)
        self.assertFalse(acp.auto_eligible("claude", reg={}))
        self.assertTrue(adapter.exists())

    def test_registry_declaration_wins(self):
        self._tool("devin", "Commands:\n  acp\n")
        reg = {"devin": {"capabilities": {"acp": {"supported": False}}}}
        self.assertIsNone(acp.command_for("devin", reg=reg))
        self._tool("mytool", "")
        reg = {"mytool": {"capabilities": {"acp": {
            "argv": ["mytool", "serve-acp"]}}}}
        self.assertEqual(acp.command_for("mytool", reg=reg),
                         ["mytool", "serve-acp"])
        reg = {"mytool": {"capabilities": {"acp": {"argv": ["gone"]}}}}
        self.assertIsNone(acp.command_for("mytool", reg=reg))
        # A malformed declaration falls through to the fallback table.
        reg = {"devin": {"capabilities": {"acp": {"argv": [3]}}}}
        self.assertEqual(acp.command_for("devin", reg=reg), ["devin", "acp"])


# =====================================================================
# G16: the ACP session adapter (remote/adapters/acp.py).
#
# Three layers, as for the other adapters: every fixture under
# tests/fixtures/protocols/acp/ replays to its golden events (real
# recordings of qwen, and hand-written ones, marked synthetic, for
# agents that were not installed); ``AcpState`` message by message; the
# adapter over tests/fakes/fake_acp.py, including the shared
# AdapterContract. No test runs a real agent.

ACP_FAKE = Path(__file__).resolve().parent / "fakes" / "fake_acp.py"
ACP_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "protocols" \
    / "acp"
ACP_REAL = ACP_FIXTURES / "-".join(acp_adapter.RECORDED)
ACP_SESSION = "11111111-2222-4333-8444-555555555555"
ACP_KEY = "sk-ant-api03-" + "d" * 40
ACP_TIMEOUT = 10.0


class TestAcpFixtures(unittest.TestCase):
    def fixtures(self):
        found = sorted(ACP_FIXTURES.rglob("*.jsonl"))
        self.assertTrue(found, "no fixtures")
        return found

    def scenario(self, name, folder=ACP_REAL):
        lines = _load_fixture(folder / f"{name}.jsonl")
        return lines[0], lines[1:], acp_adapter.replay(lines)

    @staticmethod
    def replies(got):
        return [e["data"]["text"] for e in _of(got, "message.completed")
                if e["data"]["kind"] == "assistant"]

    def test_every_fixture_replays_to_its_golden_events(self):
        for path in self.fixtures():
            with self.subTest(fixture=f"{path.parent.name}/{path.name}"):
                golden = path.with_name(path.name[:-6] + ".events.json")
                self.assertTrue(golden.exists(), f"{golden.name} missing")
                got = acp_adapter.replay(_load_fixture(path))
                self.assertEqual(got, json.loads(golden.read_text()))
                for event in got:
                    self.assertEqual(events.validate(event), [], event)
                self.assertEqual([e["type"] for e in got[:2]],
                                 ["session.caps", "session.state"])
                self.assertEqual(got[-1]["data"]["state"], "closed")
                _streams_close(self, got)
                starts = [i for i, e in enumerate(got)
                          if e["type"] == "session.caps"] + [len(got)]
                for a, b in zip(starts, starts[1:]):
                    _contract_order(self, got[a:b])

    def test_headers_say_which_agent_and_whether_it_was_recorded(self):
        for path in self.fixtures():
            with self.subTest(fixture=f"{path.parent.name}/{path.name}"):
                header = _load_fixture(path)[0]
                self.assertEqual(header["adapter"], "acp")
                self.assertEqual(header["version"], path.parent.name)
                self.assertIsNone(header["error"])
                synthetic = path.parent.name.endswith("-synthetic")
                self.assertIs(header["synthetic"], synthetic)
                if synthetic:
                    self.assertIn("not installed",
                                  header["notes"]["why_synthetic"])
                else:
                    self.assertEqual(header["agent"],
                                     acp_adapter.RECORDED[0])
                    self.assertEqual(header["argv_tails"][0], ["--acp"])
        self.assertTrue(any(p.parent == ACP_REAL for p in self.fixtures()))

    def test_what_is_proven_was_recorded_not_written(self):
        # Every cap in PROVEN rests on a real recording.
        header, _lines, got = self.scenario("hello")
        self.assertIs(header["synthetic"], False)
        self.assertEqual(got[0]["data"]["caps"],
                         acp_adapter.PROVEN.as_dict())

    def test_hello(self):
        header, lines, got = self.scenario("hello")
        self.assertEqual(header["notes"]["modes"],
                         ["plan", "default", "auto-edit", "auto", "yolo"])
        self.assertEqual(self.replies(got), ["hello there"])
        usage = _of(got, "usage.updated")[0]["data"]
        self.assertGreater(usage["input_tokens"], 0)
        self.assertEqual(usage["context_window"], 128000)
        self.assertEqual(_of(got, "turn.ended")[0]["data"]["status"],
                         "completed")
        self.assertEqual(lines[-1], {"proc": 0, "exit": 0},
                         "the agent exits when its stdin closes")

    def test_approval_allow_deny_and_an_edit(self):
        header, lines, got = self.scenario("approval")
        asked = _of(got, "approval.requested")
        self.assertEqual([e["data"]["kind"] for e in asked],
                         ["command", "command", "tool"])
        self.assertTrue(asked[0]["data"]["prompt"].startswith(
            "touch allowed.txt"))
        first = asked[0]["data"]["options"]
        self.assertEqual([o["kind"] for o in first],
                         ["always", "always", "allow", "deny"])
        self.assertEqual([o.get("standing") for o in first],
                         [True, True, None, None])
        self.assertEqual(first[0]["label"],
                         "Always Allow in project: touch *")
        self.assertEqual([e["data"]["decision"] for e in
                          _of(got, "approval.resolved")],
                         ["allow", "deny", "allow"])
        done = _of(got, "tool.completed")
        self.assertEqual([e["data"]["status"] for e in done],
                         ["ok", "error", "ok"])
        self.assertEqual(done[2]["data"]["file_edits"], [
            {"path": "/work/written.txt", "kind": "add",
             "diff": "@@ -0,0 +1 @@\n+yes\n"}])
        # The tool card waits for the call's input.
        started = _of(got, "tool.started")
        self.assertEqual(started[0]["data"]["input"]["command"],
                         "touch allowed.txt")
        # A deny lets the turn go on.
        self.assertEqual([e["data"]["status"] for e in
                          _of(got, "turn.ended")], ["completed"] * 3)
        self.assertEqual(header["notes"]["files"], [
            "allowed.txt", "hello.py", "notes.txt", "written.txt"])

    def test_allow_always_is_a_standing_rule(self):
        # Why a bare "always" is sent as a one-time allow.
        header, _lines, _got = self.scenario("always")
        notes = header["notes"]
        self.assertEqual((notes["approvals_first_turn"],
                          notes["approvals_second_turn"]), (1, 0))
        self.assertIn(".qwen", notes["files"],
                      "the rule was written into the project")

    def test_interrupt(self):
        header, lines, got = self.scenario("interrupt")
        self.assertEqual(header["notes"]["stop"], "cancelled")
        self.assertEqual([e["data"]["status"] for e in
                          _of(got, "turn.ended")], ["interrupted"])
        self.assertEqual(len(self.replies(got)), 1,
                         "the cut reply is completed")

    def test_cancel_with_a_request_open(self):
        header, lines, got = self.scenario("cancel_approval")
        self.assertIs(header["notes"]["asked_file_exists"], False)
        self.assertEqual(_of(got, "approval.resolved")[0]["data"]["source"],
                         "turn_ended")
        answers = [ln["msg"]["result"] for ln in lines
                   if ln.get("dir") == "out" and "result" in ln.get("msg", {})]
        self.assertEqual(answers, [{"outcome": {"outcome": "cancelled"}}])
        self.assertEqual(_of(got, "turn.ended")[0]["data"]["status"],
                         "interrupted")

    def test_resume_loads_the_session_and_drops_the_replay(self):
        header, lines, got = self.scenario("resume")
        self.assertGreater(header["notes"]["replayed_updates"], 0)
        sids = [e["data"]["harness_session_id"]
                for e in _of(got, "session.state")
                if e["data"].get("harness_session_id")]
        self.assertEqual(len(sids), 2)
        self.assertEqual(sids[0], sids[1], "a load keeps the id")
        self.assertEqual([r.lower() for r in self.replies(got)],
                         ["ok", "papaya"])
        ids = [e["turn_id"] for e in _of(got, "turn.started")]
        self.assertEqual(len(set(ids)), 2)

    def test_plan_mode_writes_nothing(self):
        header, lines, _got = self.scenario("plan_mode")
        notes = header["notes"]
        self.assertEqual((notes["set_plan"], notes["file_while_planning"],
                          notes["set_back"], notes["file_after"],
                          notes["bad_mode_refused"]),
                         (True, False, True, True, True))
        modes = [ln["msg"]["params"]["modeId"] for ln in lines
                 if ln.get("msg", {}).get("method") == "session/set_mode"]
        self.assertEqual(modes, ["plan", "auto", "no-such-mode"])

    def test_set_model_takes_a_listed_model_only(self):
        header, _lines, _got = self.scenario("model")
        notes = header["notes"]
        self.assertIs(notes["listed_model_accepted"], True)
        self.assertIs(notes["unlisted_model_refused"], True)

    def test_a_resource_link_is_read(self):
        _header, lines, got = self.scenario("attach")
        prompt = next(ln["msg"]["params"]["prompt"] for ln in lines
                      if ln.get("msg", {}).get("method") == "session/prompt")
        self.assertEqual(prompt[1], {
            "type": "resource_link", "uri": "file:///work/notes.txt",
            "name": "notes.txt"})
        user = [e for e in _of(got, "message.completed")
                if e["data"]["kind"] == "user"][0]
        self.assertEqual(user["data"]["attachments"], ["/work/notes.txt"])
        self.assertEqual(self.replies(got), ["first line of the notes"])

    def test_a_failed_turn(self):
        _header, _lines, got = self.scenario("error")
        ended = _of(got, "turn.ended")[0]["data"]
        self.assertEqual(ended["status"], "failed")
        self.assertIn("no-such-model", ended["reason"])
        self.assertEqual(_of(got, "error")[0]["data"]["code"], "agent_error")

    def test_an_agent_with_no_plan_mode_is_offered_none(self):
        _header, _lines, got = self.scenario(
            "hello", ACP_FIXTURES / "gemini-synthetic")
        self.assertEqual(got[0]["data"]["caps"], Caps(
            options={"permission_mode"}).as_dict())
        _header, _lines, got = self.scenario(
            "approval", ACP_FIXTURES / "kimi-synthetic")
        self.assertEqual(got[0]["data"]["caps"], Caps().as_dict())

    def test_no_personal_data(self):
        for path in sorted(ACP_FIXTURES.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertNotIn("/Users/", text)
                self.assertNotIn("/var/folders/", text)
                self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@(?!example\.com)"
                                          r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
                self.assertNotRegex(text, r"\bsk-[A-Za-z0-9_-]{8,}")
                for key in ('"_meta": {"e', '"authMethods"', "executionId",
                            ".local"):
                    self.assertNotIn(key, text)


def _acp_update(what, **body):
    return {"sessionId": ACP_SESSION,
            "update": {"sessionUpdate": what, **body}}


def _acp_chunk(text, what="agent_message_chunk", **body):
    return _acp_update(what, content={"type": "text", "text": text}, **body)


class TestAcpState(unittest.TestCase):
    def state(self, info=None):
        clock = iter(range(1000, 100000))
        state = acp_adapter.AcpState(
            "acp:test", clock=lambda: float(next(clock)), caps=ALL_CAPS,
            run_id="r1")
        state.session_info(info or {})
        state.start_events(ALL_CAPS.narrowed(acp_adapter.PROVEN),
                           ACP_SESSION)
        return state

    def types(self, evs):
        return [e["type"] for e in evs]

    def test_helpers(self):
        info = {"modes": {"currentModeId": "auto", "availableModes": [
            {"id": "plan"}, {"id": "auto"}, {"name": "no id"}, "junk"]},
            "models": {"availableModels": [
                {"modelId": "p|m1", "name": "m1"}, {"modelId": "p|m2"},
                {"name": "no id"}, "junk"]}}
        self.assertEqual(acp_adapter.mode_ids(info), ["plan", "auto"])
        self.assertEqual(acp_adapter.current_mode(info), "auto")
        self.assertEqual(acp_adapter.model_ids(info), {
            "p|m1": "p|m1", "m1": "p|m1", "p|m2": "p|m2"})
        self.assertEqual(acp_adapter.advertised(info), Caps(
            plan_mode=True, options={"model", "permission_mode"}))
        for empty in ({}, None, {"modes": "x", "models": []}):
            self.assertEqual(acp_adapter.mode_ids(empty), [])
            self.assertEqual(acp_adapter.current_mode(empty), "")
            self.assertEqual(acp_adapter.model_ids(empty), {})
            self.assertEqual(acp_adapter.advertised(empty), Caps())
        self.assertEqual(acp_adapter.resource_links(["/w/a b.txt"]), [{
            "type": "resource_link", "uri": "file:///w/a%20b.txt",
            "name": "a b.txt"}])
        self.assertEqual(acp_adapter.error_text({
            "code": -32603, "message": "Internal error",
            "data": {"details": "404 no model"}}),
            "Internal error: 404 no model")
        self.assertEqual(acp_adapter.error_text({"code": -1, "data": "why"}),
                         "-1: why")
        self.assertEqual(acp_adapter.error_text("boom"), "boom")
        self.assertEqual(acp_adapter.error_text(None), "error")
        self.assertNotIn("sk-ant", acp_adapter.error_text(
            {"message": f"bad key {ACP_KEY}"}))

    def test_file_edits(self):
        edits = acp_adapter.file_edits([
            {"type": "diff", "path": "/w/new.txt", "oldText": None,
             "newText": "a\nb"},
            {"type": "diff", "path": "/w/mod.txt", "oldText": "a\nb\n",
             "newText": "a\nc\n"},
            {"type": "diff", "path": "/w/gone.txt", "oldText": "x",
             "newText": ""},
            {"type": "diff", "oldText": "no path"},
            {"type": "content", "content": {"type": "text", "text": "x"}},
            "junk"])
        self.assertEqual([(e["path"], e["kind"]) for e in edits], [
            ("/w/new.txt", "add"), ("/w/mod.txt", "modify"),
            ("/w/gone.txt", "delete")])
        self.assertEqual(edits[0]["diff"], "@@ -0,0 +1,2 @@\n+a\n+b\n")
        self.assertIn("-b\n+c\n", edits[1]["diff"])
        self.assertIsNone(acp_adapter.file_edits(None))
        self.assertIsNone(acp_adapter.file_edits([]))
        secret = acp_adapter.file_edits([{
            "type": "diff", "path": ".env", "oldText": "",
            "newText": "API_KEY=" + ACP_KEY}])
        self.assertNotIn("sk-ant", json.dumps(secret))
        # A key block is redacted whole before the diff cuts it: a hunk
        # from its middle holds neither its first line nor its last.
        body = "\n".join("QUJD" * 12 + f"{n:02d}" for n in range(30))
        pem = ("-----BEGIN RSA PRIVATE KEY-----\n" + body
               + "\n-----END RSA PRIVATE KEY-----\n")
        inside = acp_adapter.file_edits([{
            "type": "diff", "path": "id_rsa", "oldText": pem,
            "newText": pem.replace("QUJD" * 12 + "15", "WFla" * 12 + "15")}])
        self.assertNotIn("QUJDQUJD", json.dumps(inside))
        self.assertNotIn("WFlaWFla", json.dumps(inside))

    def test_a_turn_with_text(self):
        state = self.state()
        turn_id, out = state.begin("hi", command_id="c1", device="phone",
                                   attachments=["/w/a.txt"])
        self.assertEqual(self.types(out), [
            "turn.started", "session.state", "message.completed"])
        self.assertEqual(turn_id, "t-r1-1")
        self.assertTrue(state.busy)
        out = state.update(_acp_chunk("")).events
        self.assertEqual(out, [], "an empty chunk says nothing")
        out = state.update(_acp_chunk("hello there, ")).events
        out += state.update(_acp_chunk("friend", _meta={"usage": {
            "inputTokens": 9, "outputTokens": 2,
            "cachedReadTokens": 4}})).events
        out += state.update(_acp_update("usage_update", used=11,
                                        size=100)).events
        self.assertEqual(out[-1]["data"], {
            "input_tokens": 9, "output_tokens": 2, "cache_read_tokens": 4,
            "context_tokens": 11, "context_window": 100})
        out += state.prompt_answered({"id": 3, "result": {
            "stopReason": "end_turn"}})
        self.assertEqual(self.types(out)[-3:], [
            "message.completed", "turn.ended", "session.state"])
        self.assertEqual(out[-3]["data"]["text"], "hello there, friend")
        self.assertEqual(out[-2]["data"]["status"], "completed")
        self.assertEqual(out[-2]["command_id"], "c1")
        _streams_close(self, out)
        self.assertFalse(state.busy)
        # Nothing of a turn that is over.
        self.assertEqual(state.update(_acp_chunk("late")).events, [])
        self.assertEqual(state.prompt_answered({"id": 3, "result": {}}), [])

    def test_a_usage_update_with_nothing_to_say(self):
        state = self.state()
        self.assertEqual(state.update(_acp_update(
            "usage_update", used=-1, size="big")).events, [])

    def test_thoughts_and_messages_end_each_other(self):
        state = self.state()
        turn_id, _out = state.begin("hi")
        out = state.update(_acp_chunk("thinking it over",
                                      "agent_thought_chunk")).events
        out += state.update(_acp_chunk("the answer ", messageId="m1")).events
        out += state.update(_acp_chunk("goes on ", messageId="m1")).events
        out += state.update(_acp_chunk("another message",
                                       messageId="m2")).events
        out += state.prompt_answered({"id": 1, "result": {
            "stopReason": "end_turn"}})
        done = [(e["type"], e["data"]["id"], e["data"]["text"]) for e in out
                if e["type"] in ("thinking.completed", "message.completed")]
        self.assertEqual(done, [
            ("thinking.completed", f"{turn_id}:r1", "thinking it over"),
            ("message.completed", f"{turn_id}:m2", "the answer goes on "),
            ("message.completed", f"{turn_id}:m3", "another message")])
        _streams_close(self, out)

    def test_a_tool_call_is_shown_once_it_has_its_input(self):
        state = self.state()
        turn_id, _out = state.begin("hi")
        state.update(_acp_chunk("let me look "))
        out = state.update(_acp_update(
            "tool_call", toolCallId="c1", status="pending", title="Shell",
            kind="execute", rawInput={})).events
        self.assertEqual(out, [], "no card for a call with no input yet")
        out = state.update(_acp_update(
            "tool_call_update", toolCallId="c1", status="in_progress",
            title="Shell: ls -la", rawInput={"command": "ls -la"})).events
        self.assertEqual(self.types(out), [
            "message.completed", "tool.started"],
            "the message before the call is completed first")
        self.assertEqual(out[-1]["data"], {
            "call_id": "c1", "name": "execute", "summary": "Shell: ls -la",
            "input": {"command": "ls -la"}})
        self.assertEqual(state.update(_acp_update(
            "tool_call_update", toolCallId="c1")).events, [])
        out = state.update(_acp_update(
            "tool_call_update", toolCallId="c1", status="completed",
            content=[{"type": "content", "content": {
                "type": "text", "text": "a.txt"}}, {"type": "content"},
                "junk"])).events
        self.assertEqual(out[0]["data"], {
            "call_id": "c1", "status": "ok", "name": "execute",
            "output": "a.txt"})
        self.assertEqual(out[0]["turn_id"], turn_id)
        # A call that starts running at once, and one that fails.
        out = state.update(_acp_update(
            "tool_call", toolCallId="c2", status="in_progress",
            title="Read", kind="read")).events
        self.assertEqual(self.types(out), ["tool.started"])
        out = state.update(_acp_update(
            "tool_call_update", toolCallId="c2", status="failed")).events
        self.assertEqual(out[0]["data"]["status"], "error")
        # Noise: no id, an update for a call never seen, a second end.
        for update in (_acp_update("tool_call"),
                       _acp_update("tool_call_update", toolCallId="zz"),
                       _acp_update("tool_call_update", toolCallId="c2",
                                   status="completed")):
            self.assertEqual(self.types(state.update(update).events)[:1],
                             [] if update["update"].get("toolCallId") != "c2"
                             else ["tool.started"])

    def test_output_sent_before_the_call_ends_is_its_result(self):
        state = self.state()
        state.begin("hi")
        state.update(_acp_update(
            "tool_call", toolCallId="c1", status="in_progress", kind="edit",
            rawInput={"file_path": "/w/a.txt"}))
        state.update(_acp_update("tool_call_update", toolCallId="c1", content=[
            {"type": "content", "content": {"type": "text", "text": "wrote"}},
            {"type": "diff", "path": "/w/a.txt", "oldText": None,
             "newText": "yes"}]))
        state.update(_acp_update("tool_call_update", toolCallId="c1",
                                 content=[]))
        out = state.update(_acp_update("tool_call_update", toolCallId="c1",
                                       status="completed")).events
        self.assertEqual(out[0]["data"]["output"], "wrote")
        self.assertEqual(out[0]["data"]["file_edits"], [
            {"path": "/w/a.txt", "kind": "add", "diff": "@@ -0,0 +1 @@\n+yes\n"}])

    def test_a_call_never_shown_is_forgotten_with_its_turn(self):
        state = self.state()
        state.begin("hi")
        state.update(_acp_update("tool_call", toolCallId="c1",
                                 status="pending", rawInput={}))
        state.prompt_answered({"id": 1, "result": {"stopReason": "end_turn"}})
        self.assertEqual(state._tools, {})

    def test_a_tool_that_outlives_its_turn(self):
        state = self.state()
        state.begin("hi")
        state.update(_acp_update("tool_call", toolCallId="c1",
                                 status="in_progress", kind="execute"))
        state.prompt_answered({"id": 1, "result": {"stopReason": "cancelled"}})
        state.begin("next")
        out = state.update(_acp_update("tool_call_update", toolCallId="c1",
                                       status="completed")).events
        self.assertEqual(out[0]["type"], "tool.completed")
        self.assertNotIn("turn_id", out[0])

    def test_a_plan(self):
        state = self.state()
        state.begin("hi")
        out = state.update(_acp_update("plan", entries=[
            {"content": "look", "status": "completed"},
            {"content": "act", "status": "someday"}, {"status": "pending"},
            "junk"])).events
        self.assertEqual(out[0]["data"]["items"], [
            {"text": "look", "status": "completed"},
            {"text": "act", "status": "pending"}])
        cleared = state.update(_acp_update("plan", entries=[])).events
        self.assertEqual(cleared[0]["data"]["items"], [],
                         "an empty plan clears the one before")
        self.assertEqual(events.validate(cleared[0]), [])
        for junk in ({}, {"entries": "x"},
                     {"entries": [{"content": "a", "status": ["x"]}]}):
            got = state.update(_acp_update("plan", **junk)).events
            self.assertEqual([e["data"]["items"] for e in got],
                             [[{"text": "a", "status": "pending"}]]
                             if isinstance(junk.get("entries"), list) else [])

    def test_other_sessions_and_unknown_updates(self):
        state = self.state()
        state.begin("hi")
        other = _acp_chunk("not ours")
        other["sessionId"] = "someone-else"
        for params in (other, _acp_update("available_commands_update"),
                       {"sessionId": ACP_SESSION, "update": "junk"},
                       {"sessionId": ACP_SESSION}, "junk", None):
            self.assertEqual(state.update(params).events, [])
        self.assertEqual(state.unknown, {
            "other-session": 1, "available_commands_update": 1, "?": 2})
        state.update(_acp_update("current_mode_update",
                                 currentModeId="yolo"))
        self.assertEqual(state.mode, "yolo")

    def ask(self, state, rpc_id=7, **call):
        return state.permission(rpc_id, {
            "sessionId": ACP_SESSION,
            "toolCall": {"toolCallId": "c1", **call},
            "options": [
                {"optionId": "always_project", "kind": "allow_always",
                 "name": f"Always in project {ACP_KEY}"},
                {"optionId": "once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "cancel", "name": "Reject",
                 "kind": "reject_once"},
                {"optionId": "odd", "kind": "something_new"},
                {"name": "no id"}, "junk"]})

    def test_a_permission_request(self):
        state = self.state()
        turn_id, _out = state.begin("hi")
        state.update(_acp_update("tool_call", toolCallId="c1",
                                 status="pending", title="Shell",
                                 kind="execute", rawInput={}))
        fed = self.ask(state, title="touch a.txt",
                       rawInput={"command": "touch a.txt"})
        self.assertEqual(fed.replies, [])
        self.assertEqual(self.types(fed.events), [
            "tool.started", "approval.requested", "session.state"],
            "the card of the call it asks about comes first")
        self.assertEqual(fed.events[0]["data"]["input"],
                         {"command": "touch a.txt"})
        asked = fed.events[1]["data"]
        self.assertEqual((asked["request_id"], asked["kind"],
                          asked["prompt"], asked["call_id"]),
                         ("r1-p1", "command", "touch a.txt", "c1"))
        self.assertEqual(asked["options"][1:], [
            {"id": "o2", "label": "Allow", "kind": "allow"},
            {"id": "o3", "label": "Reject", "kind": "deny"},
            {"id": "o4", "label": "odd"}])
        self.assertEqual((asked["options"][0]["kind"],
                          asked["options"][0]["standing"]), ("always", True))
        self.assertNotIn("sk-ant", json.dumps(fed.events),
                         "a label the agent wrote is redacted")
        (rpc_id, result), out = state.answer("r1-p1",
                                             Answer(decision="allow"))
        self.assertEqual((rpc_id, result), (7, {"outcome": {
            "outcome": "selected", "optionId": "once"}}))
        self.assertEqual(self.types(out),
                         ["approval.resolved", "session.state"])
        self.assertEqual(out[0]["data"], {
            "request_id": "r1-p1", "source": "user", "decision": "allow"})
        self.assertEqual(out[0]["turn_id"], turn_id)
        with self.assertRaises(acp_adapter_base.UnknownRequest):
            state.answer("r1-p1", Answer(decision="allow"))

    def test_permission_answers(self):
        for answer, option, decision in (
                (Answer(decision="deny"), "cancel", "deny"),
                # A standing rule is only written when it is named.
                (Answer(decision="always"), "once", "allow"),
                (Answer(option_id="allow"), "once", "allow"),
                (Answer(option_id="o1"), "always_project", "always"),
                (Answer(option_id="o4"), "odd", None),
                (Answer(text="use another file"), "cancel", "deny")):
            with self.subTest(answer=answer):
                state = self.state()
                state.begin("hi")
                self.ask(state, title="x")
                (_rpc, result), out = state.answer("r1-p1", answer)
                self.assertEqual(result["outcome"]["optionId"], option)
                self.assertEqual(out[0]["data"].get("decision"), decision)

    def test_answers_the_request_does_not_offer(self):
        state = self.state()
        state.begin("hi")
        state.permission(7, {"options": [
            {"optionId": "always", "kind": "allow_always"}]})
        # Only a standing allow on offer: a bare allow does not take it.
        for answer in (Answer(option_id="o9"), Answer(decision="deny"),
                       Answer(decision="allow"), Answer(decision="always")):
            with self.assertRaises(AdapterError) as ctx:
                state.answer("r1-p1", answer)
            self.assertEqual(ctx.exception.code, "bad_request")
            self.assertIn("r1-p1", state.pending, "still open")
        (_rpc, result), _out = state.answer("r1-p1", Answer(option_id="o1"))
        self.assertEqual(result["outcome"]["optionId"], "always")
        # The same for a standing refusal: a bare deny (or a text-only
        # answer) must not write a permanent rule.
        state = self.state()
        state.begin("hi")
        state.permission(8, {"options": [
            {"optionId": "once", "kind": "allow_once"},
            {"optionId": "never", "kind": "reject_always"}]})
        for answer in (Answer(decision="deny"), Answer(text="no")):
            with self.assertRaises(AdapterError) as ctx:
                state.answer("r1-p1", answer)
            self.assertEqual(ctx.exception.code, "bad_request")
        (_rpc, result), _out = state.answer("r1-p1", Answer(option_id="o2"))
        self.assertEqual(result["outcome"]["optionId"], "never")

    def test_kinds_and_statuses_that_are_not_strings(self):
        state = self.state()
        state.begin("hi")
        fed = state.permission(7, {"options": [
            {"optionId": "a", "kind": ["allow_once"]},
            {"optionId": "b", "kind": "reject_once"}]})
        self.assertEqual(fed.replies, [])
        self.assertEqual(fed.events[0]["data"]["options"], [
            {"id": "o1", "label": "a"},
            {"id": "o2", "label": "b", "kind": "deny"}])
        out = state.update(_acp_update(
            "tool_call", toolCallId="c1", status=["completed"],
            rawInput={"command": "ls"})).events
        self.assertEqual(self.types(out), ["tool.started"])

    def test_requests_that_cannot_be_shown_are_cancelled(self):
        cancelled = [(7, {"outcome": {"outcome": "cancelled"}})]
        state = self.state()
        state.begin("hi")
        for params in ({"options": []}, {"options": "x"}, None,
                       {"sessionId": "someone-else", "options": [
                           {"optionId": "a", "kind": "allow_once"}]}):
            fed = state.permission(7, params)
            self.assertEqual((fed.events, fed.replies), ([], cancelled))
        self.assertEqual(state.pending, {})

    def test_a_cancel_answers_what_is_open_and_refuses_what_comes_after(self):
        state = self.state()
        self.assertEqual(state.cancel_sent(), ([], []), "no turn: nothing")
        state.begin("hi")
        self.ask(state, title="x")
        replies, out = state.cancel_sent()
        self.assertEqual(replies, [(7, {"outcome": {"outcome": "cancelled"}})])
        self.assertEqual(self.types(out),
                         ["approval.resolved", "session.state"])
        self.assertEqual(out[0]["data"]["source"], "turn_ended")
        late = self.ask(state, rpc_id=8, title="y")
        self.assertEqual((late.events, late.replies),
                         ([], [(8, {"outcome": {"outcome": "cancelled"}})]))
        out = state.prompt_answered({"id": 1, "result": {
            "stopReason": "cancelled"}})
        self.assertEqual(out[0]["data"]["status"], "interrupted")
        # The next turn asks again as usual.
        state.begin("again")
        self.assertEqual(self.ask(state, rpc_id=9, title="z").replies, [])

    def test_how_a_turn_ends(self):
        for msg, status, reason in (
                ({"result": {"stopReason": "end_turn"}}, "completed", None),
                ({"result": {"stopReason": "cancelled"}}, "interrupted",
                 None),
                ({"result": {"stopReason": "max_tokens"}}, "completed",
                 "max_tokens"),
                ({"result": {}}, "completed", None),
                ({"error": {"message": "Internal error",
                            "data": {"details": "404"}}}, "failed",
                 "Internal error: 404")):
            with self.subTest(msg=msg):
                state = self.state()
                state.begin("hi")
                state.update(_acp_chunk("partial text "))
                out = state.prompt_answered({"id": 1, **msg})
                ended = [e for e in out if e["type"] == "turn.ended"][0]
                self.assertEqual(ended["data"]["status"], status)
                self.assertEqual(ended["data"].get("reason"), reason)
                self.assertEqual("error" in self.types(out),
                                 status == "failed")
                _streams_close(self, out)

    def test_a_turn_that_ends_with_a_request_open(self):
        state = self.state()
        state.begin("hi")
        self.ask(state, title="x")
        out = state.prompt_answered({"id": 1, "result": {
            "stopReason": "end_turn"}})
        self.assertEqual(self.types(out), [
            "approval.resolved", "turn.ended", "session.state"])
        self.assertEqual(state.pending, {})

    def test_replay_skips_what_it_does_not_know(self):
        got = acp_adapter.replay([
            {"proc": 0, "dir": "out", "t": 0.1, "msg": {
                "id": 1, "method": "session/new", "params": {}}},
            {"proc": 0, "dir": "in", "t": 0.2, "msg": {
                "id": 1, "result": {"sessionId": ACP_SESSION}}},
            {"proc": 0, "dir": "in", "t": 0.3, "msg": {"method": "x/notify"}},
            {"proc": 0, "dir": "in", "t": 0.3, "msg": {
                "id": 99, "result": {}}},
            {"proc": 0, "dir": "out", "t": 0.3, "msg": {
                "id": 5, "result": {"outcome": {"outcome": "cancelled"}}}},
            {"proc": 0, "raw": "junk"},
            {"proc": 0, "exit": 0}])
        self.assertEqual([e["type"] for e in got],
                         ["session.caps", "session.state", "session.state"])


def _acp_resolved(caps: Caps = ALL_CAPS, min_version: str = "",
                  harness: str = "qwen-code") -> registry.Resolved:
    return registry.Resolved(harness, "acp", (sys.executable, str(ACP_FAKE)),
                             caps, min_version=min_version)


class _AcpAdapterCase(unittest.TestCase):
    """An adapter over the fake agent, cleaned up after the test."""

    def adapter(self, mode: str = "ok", *, caps: Caps = ALL_CAPS,
                reply: str = "hello", min_version: str = "", **kw):
        self.agent_log = temp_dir(self) / "fake.log"
        env = dict(os.environ, FAKE_ACP_MODE=mode, FAKE_ACP_REPLY=reply,
                   FAKE_ACP_LOG=str(self.agent_log))
        adapter = acp_adapter.AcpAdapter(
            _acp_resolved(caps, min_version), env=env, **kw)
        self.addCleanup(adapter.close, 0.5)
        return adapter

    def spec(self, **kw) -> StartSpec:
        return StartSpec(session_key="acp:test", cwd=str(temp_dir(self)),
                         **kw)

    def started(self, mode: str = "ok", **kw):
        spec_kw = kw.pop("spec", {})
        adapter = self.adapter(mode, **kw)
        sink = adapters_fake.ListSink()
        sid = adapter.start(self.spec(**spec_kw), sink)
        return adapter, sink, sid

    def sent(self) -> list:
        return [json.loads(line) for line in
                self.agent_log.read_text().splitlines()][1:] \
            if self.agent_log.exists() else []

    def sent_methods(self) -> list:
        return [m.get("method") for m in self.sent()]

    def wait(self, sink, match, what="the event"):
        found = sink.wait_for(match, ACP_TIMEOUT)
        self.assertIsNotNone(found, f"timed out waiting for {what}")
        return found

    def wait_turn(self, sink, turn_id):
        def ended_at():
            return next((i for i, e in enumerate(sink.events)
                         if e["type"] == "turn.ended"
                         and e.get("turn_id") == turn_id), None)

        def idle_after(event):
            at = ended_at()
            return at is not None and event["type"] == "session.state" \
                and any(e is event for e in sink.events[at + 1:])

        self.wait(sink, idle_after, "the turn to end")
        return sink.events[ended_at()]

    def wait_state(self, sink, state):
        return self.wait(sink, lambda e: e["type"] == "session.state"
                         and e["data"]["state"] == state, state)


class TestAcpContract(AdapterContract, _AcpAdapterCase):
    def make_adapter(self, *, caps, reply):
        return self.adapter(caps=caps, reply=reply)


class TestAcpAdapter(_AcpAdapterCase):
    def test_registered_and_resolved_for_every_acp_harness(self):
        self.assertIn("acp", registry.registered())
        self.assertIn("acp", registry.BUILTIN_MODULES)
        for harness in ("gemini", "qwen-code", "kimi", "devin"):
            resolved = registry.adapter_for(harness, {}, which=lambda b: b)
            self.assertTrue(resolved.ok, harness)
            self.assertEqual(resolved.adapter, "acp")
            self.assertEqual(resolved.caps, Caps(),
                             "the fallback keeps every feature off")
            self.assertIsInstance(registry.create(resolved),
                                  acp_adapter.AcpAdapter)

    def test_caps_are_the_ceiling_met_with_proof_and_the_agents_own(self):
        adapter = self.adapter()
        self.assertEqual(adapter.capabilities, acp_adapter.PROVEN)
        for off in ("steer", "rollback", "fork", "stop_task"):
            self.assertFalse(adapter.capabilities.supports(off))
        self.assertFalse(adapter.capabilities.can_set("effort"))
        bare, sink, _sid = self.started("bare")
        self.assertEqual(bare.capabilities, Caps(),
                         "an agent that lists no modes and no models")
        self.assertEqual(sink.events[0]["data"]["caps"], Caps().as_dict())
        with self.assertRaises(Unsupported):
            bare.set_option("plan_mode", True)

    def test_start_is_a_launch_through_session_new(self):
        adapter, sink, sid = self.started(spec={
            "session_id": "the-hubs-pick", "model": "model-2",
            "permission_mode": "yolo", "env": {"FAKE_ACP_EXTRA": "1"}})
        self.assertEqual(sid, ACP_SESSION)
        self.assertEqual((adapter.session_id, adapter.version,
                          adapter.agent_name),
                         (ACP_SESSION, "9.9.9", "fake-agent"))
        self.assertEqual(sink.types(), ["session.caps", "session.state",
                                        "session.options"])
        self.assertEqual(sink.events[1]["data"], {
            "state": "idle", "harness_session_id": ACP_SESSION})
        self.assertEqual(self.sent_methods(), [
            "initialize", "session/new", "session/set_model",
            "session/set_mode"])
        sent = self.sent()
        self.assertEqual(sent[0]["params"]["clientInfo"]["name"], "swe")
        self.assertEqual(sent[1]["params"], {
            "cwd": os.path.realpath(adapter._spec.cwd), "mcpServers": []})
        self.assertEqual(sent[2]["params"]["modelId"], "prov|model-2")
        self.assertEqual(sent[3]["params"]["modeId"], "yolo")
        self.assertIsNone(adapter.transcript_position())

    def test_start_options_this_agent_does_not_have_are_left_out(self):
        self.started(spec={"model": "gpt-other", "permission_mode":
                           "acceptEdits", "effort": "high"})
        self.assertEqual(self.sent_methods(), ["initialize", "session/new"])

    def test_plan_mode_at_start(self):
        adapter, _sink, _sid = self.started(spec={"plan_mode": True})
        self.assertEqual(self.sent()[-1]["params"]["modeId"], "plan")
        self.assertEqual(adapter._mode_before, "default")

    def test_plan_mode_at_start_goes_back_to_the_mode_asked_for(self):
        # A keeper restart gives both: plan on, and the mode to return to.
        adapter, _sink, _sid = self.started(spec={
            "plan_mode": True, "permission_mode": "yolo"})
        self.assertEqual([m["params"]["modeId"] for m in self.sent()
                          if m.get("method") == "session/set_mode"], ["plan"])
        adapter.set_option("plan_mode", False)
        self.assertEqual(self.sent()[-1]["params"]["modeId"], "yolo")

    def test_resume_loads_and_shows_no_replay(self):
        _adapter, sink, sid = self.started(spec={"resume_id": "older-1"})
        self.assertEqual(sid, "older-1")
        self.assertEqual(self.sent_methods(), ["initialize", "session/load"])
        self.assertEqual(sink.types(), ["session.caps", "session.state",
                                        "session.options"],
                         "the replayed history is not live text")

    def test_start_refusals(self):
        for mode, spec, code, said in (
                ("no-load", {"resume_id": "older-1"}, "bad_request",
                 "can't load sessions"),
                ("ok", {"resume_id": "missing"}, "refused",
                 "Session not found"),
                ("refuse-new", {}, "refused", "Authentication required"),
                ("exit-at-init", {}, "agent_down", ""),
                ("silent-init", {}, "timeout", ""),
                ("old", {}, "unsupported_version", "older than 1.0.0")):
            with self.subTest(mode=mode):
                adapter = self.adapter(mode, init_timeout=1.0,
                                       min_version="1.0.0")
                sink = adapters_fake.ListSink()
                with self.assertRaises(AdapterError) as ctx:
                    adapter.start(self.spec(**spec), sink)
                self.assertEqual(ctx.exception.code, code)
                self.assertIn(said, str(ctx.exception))
                self.assertEqual(sink.events, [])
                self.assertIsNotNone(adapter._agent.proc.wait(ACP_TIMEOUT),
                                     "the child is gone")
                with self.assertRaises(AdapterError):
                    adapter.start(self.spec(), sink)
                adapter.close(0.5)
                self.assertEqual(sink.events, [])

    def test_no_command_for_the_harness(self):
        adapter = acp_adapter.AcpAdapter(registry.Resolved(
            "no-such-harness", "acp", None, ALL_CAPS))
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(self.spec(), adapters_fake.ListSink())
        self.assertEqual(ctx.exception.code, "agent_down")
        self.assertIn("no-such-harness has no ACP command",
                      str(ctx.exception))
        missing = acp_adapter.AcpAdapter(registry.Resolved(
            "x", "acp", ("/nonexistent/agent",), ALL_CAPS))
        with self.assertRaises(AdapterError) as ctx:
            missing.start(self.spec(), adapters_fake.ListSink())
        self.assertEqual(ctx.exception.code, "agent_down")

    def test_the_command_comes_from_the_wire_modules_probe(self):
        adapter = acp_adapter.AcpAdapter(
            registry.Resolved("qwen-code", "acp", None, ALL_CAPS),
            env=dict(os.environ))
        self.addCleanup(adapter.close, 0.5)
        with patch.object(acp, "command_for", return_value=[
                sys.executable, str(ACP_FAKE)]) as probe:
            sid = adapter.start(self.spec(), adapters_fake.ListSink())
        probe.assert_called_once_with("qwen-code")
        self.assertEqual(sid, ACP_SESSION)

    def test_a_turn_streams_and_ends(self):
        adapter, sink, _sid = self.started(reply="hello there, friend")
        turn_id = adapter.send_turn(TurnInput(
            "hi", command_id="c1", device="phone",
            attachments=["/w/a.txt"]))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        self.assertEqual(ended["command_id"], "c1")
        mine = [e for e in sink.events if e.get("turn_id") == turn_id]
        self.assertEqual([e["type"] for e in mine][:2],
                         ["turn.started", "message.completed"])
        self.assertEqual(mine[1]["data"]["attachments"], ["/w/a.txt"])
        self.assertIn("usage.updated", [e["type"] for e in mine])
        _streams_close(self, sink.events)
        prompt = self.sent()[-1]["params"]["prompt"]
        self.assertEqual(prompt, [
            {"type": "text", "text": "hi"},
            {"type": "resource_link", "uri": "file:///w/a.txt",
             "name": "a.txt"}])
        second = adapter.send_turn(TurnInput("again"))
        self.assertNotEqual(second, turn_id)
        self.wait_turn(sink, second)
        self.assertRegex(turn_id, r"^t-[0-9a-f]{8}-1$")

    def test_busy_then_interrupt(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count"))
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput("another"))
        self.assertEqual(ctx.exception.code, "busy")
        self.wait(sink, "message.delta")
        adapter.interrupt()
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "interrupted")
        partial = [e for e in sink.of("message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(partial[0]["data"]["text"], "1\n2\n")
        self.assertEqual(self.sent_methods()[-1], "session/cancel")
        _streams_close(self, sink.events)
        before = len(sink.events)
        adapter.interrupt()
        self.assertEqual(len(sink.events), before)
        self.assertEqual(self.sent_methods().count("session/cancel"), 1)

    def test_thoughts_plans_and_tools(self):
        adapter, sink, _sid = self.started("rich")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.wait_turn(sink, turn_id)
        types = sink.types()
        for wanted in ("thinking.completed", "plan.updated", "tool.started",
                       "tool.completed", "message.completed"):
            self.assertIn(wanted, types)
        self.assertEqual(sink.of("tool.started")[0]["data"]["input"],
                         {"file_path": "/w/notes.txt"})
        self.assertEqual(adapter._state.mode, "yolo",
                         "a mode the agent changed itself is followed")
        _contract_order(self, sink.events[2:])
        _streams_close(self, sink.events)

    def test_approval_allow(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("touch it"))
        asked = self.wait(sink, "approval.requested")
        self.assertEqual((asked["data"]["kind"], asked["data"]["prompt"]),
                         ("command", "touch probe.txt"))
        self.wait_state(sink, "waiting")
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="always"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        answer = self.sent()[-1]
        self.assertEqual(answer["result"], {"outcome": {
            "outcome": "selected", "optionId": "once"}},
            "a bare always is a one-time allow")
        types = sink.types()
        self.assertLess(types.index("approval.resolved"),
                        types.index("tool.completed"))
        with self.assertRaises(acp_adapter_base.UnknownRequest):
            adapter.respond(asked["data"]["request_id"],
                            Answer(decision="allow"))
        _contract_order(self, sink.events[2:])

    def test_a_named_standing_option_is_sent_as_it_is(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("touch it"))
        asked = self.wait(sink, "approval.requested")
        adapter.respond(asked["data"]["request_id"], Answer(option_id="o1"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(self.sent()[-1]["result"]["outcome"]["optionId"],
                         "always_project")
        self.assertEqual(sink.of("approval.resolved")[0]["data"]["decision"],
                         "always")

    def test_approval_deny_and_an_edit(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("touch it"))
        asked = self.wait(sink, "approval.requested")
        adapter.respond(asked["data"]["request_id"], Answer(decision="deny"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(sink.of("tool.completed")[0]["data"]["status"],
                         "error")
        turn_id = adapter.send_turn(TurnInput("mode:edit write it"))
        asked = self.wait(sink, lambda e: e["type"] == "approval.requested"
                          and e.get("turn_id") == turn_id)
        self.assertEqual(asked["data"]["kind"], "tool")
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="allow"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(sink.of("tool.completed")[-1]["data"]["file_edits"],
                         [{"path": "/w/probe.txt", "kind": "add",
                           "diff": "@@ -0,0 +1 @@\n+yes\n"}])

    def test_an_interrupt_answers_the_open_request_cancelled(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("touch it"))
        asked = self.wait(sink, "approval.requested")
        adapter.interrupt()
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "interrupted")
        self.assertEqual(sink.of("approval.resolved")[0]["data"], {
            "request_id": asked["data"]["request_id"],
            "source": "turn_ended"})
        tail = self.sent()[-2:]
        self.assertEqual(tail[0]["method"], "session/cancel")
        self.assertEqual(tail[1]["result"],
                         {"outcome": {"outcome": "cancelled"}})

    def test_a_request_after_the_cancel_is_refused_unseen(self):
        adapter, sink, _sid = self.started("late-ask")
        turn_id = adapter.send_turn(TurnInput("count"))
        self.wait(sink, "message.delta")
        adapter.interrupt()
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "interrupted")
        self.assertEqual(sink.of("approval.requested"), [])

    def test_an_answer_that_cannot_be_written_reopens_the_request(self):
        adapter, sink, _sid = self.started("approval")
        adapter.send_turn(TurnInput("touch it"))
        asked = self.wait(sink, "approval.requested")
        request_id = asked["data"]["request_id"]
        with patch.object(adapter._agent, "_respond", return_value=False):
            with self.assertRaises(AdapterError) as ctx:
                adapter.respond(request_id, Answer(decision="allow"))
        self.assertEqual(ctx.exception.code, "agent_down")
        self.assertIn(request_id, adapter._state.pending)
        self.assertTrue(adapter._state.waiting)
        self.assertEqual(sink.of("approval.resolved"), [])
        adapter.respond(request_id, Answer(decision="allow"))
        self.wait(sink, "turn.ended")

    def test_a_failed_turn_and_one_that_stops_short(self):
        adapter, sink, _sid = self.started()
        turn_id = adapter.send_turn(TurnInput("mode:fail go"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "failed")
        self.assertIn("404 The model does not exist",
                      ended["data"]["reason"])
        turn_id = adapter.send_turn(TurnInput("mode:stopped go"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual((ended["data"]["status"], ended["data"]["reason"]),
                         ("completed", "max_tokens"))

    def test_noise_changes_nothing(self):
        adapter, sink, _sid = self.started("noise", reply="fine")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.wait_turn(sink, turn_id)
        replies = [e["data"]["text"] for e in sink.of("message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies, ["fine"])
        self.assertEqual(adapter._state.unknown["other-session"], 1)
        adapter.close(2.0)
        answered = [m for m in self.sent() if m.get("id") == 7000]
        self.assertEqual(answered[0]["error"]["code"], -32601,
                         "a request nothing serves gets an error answer")

    def test_secrets_are_masked_live(self):
        adapter, sink, _sid = self.started("secret")
        turn_id = adapter.send_turn(TurnInput("leak it"))
        asked = self.wait(sink, "approval.requested")
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="allow"))
        self.wait_turn(sink, turn_id)
        self.assertNotIn("sk-ant", json.dumps(sink.events))
        self.assertIn("the key is", json.dumps(sink.events))

    def test_options(self):
        adapter, sink, _sid = self.started()
        before = len(sink.events)
        adapter.set_option("model", "model-2")
        self.assertEqual(self.sent()[-1]["params"]["modelId"],
                         "prov|model-2")
        adapter.set_option("permission_mode", "yolo")
        self.assertEqual(self.sent()[-1]["params"]["modeId"], "yolo")
        adapter.set_option("plan_mode", True)
        adapter.set_option("plan_mode", True)
        self.assertEqual(self.sent_methods().count("session/set_mode"), 3,
                         "sent again: an agent can leave plan mode itself")
        turn_id = adapter.send_turn(TurnInput("do it"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(sink.of("message.completed")[-1]["data"]["text"],
                         "planning")
        adapter.set_option("plan_mode", False)
        self.assertEqual(self.sent()[-1]["params"]["modeId"], "yolo",
                         "back to the mode it was in")
        adapter.set_option("plan_mode", False)
        self.assertEqual(self.sent_methods().count("session/set_mode"), 4,
                         "not planning: nothing to undo")
        self.assertEqual(len([e for e in sink.events[before:]
                              if e["type"] == "session.caps"]), 0)
        for name, value, code in (("model", "no-such", "bad_request"),
                                  ("permission_mode", "no-such",
                                   "bad_request")):
            with self.assertRaises(AdapterError) as ctx:
                adapter.set_option(name, value)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(Unsupported):
            adapter.set_option("effort", "high")

    def test_a_refused_mode_changes_nothing(self):
        adapter, _sink, _sid = self.started("refuse-mode")
        with self.assertRaises(AdapterError) as ctx:
            adapter.set_option("plan_mode", True)
        self.assertEqual(ctx.exception.code, "refused")
        self.assertEqual(adapter._state.mode, "default")

    def test_what_acp_cannot_do_is_unsupported(self):
        adapter, sink, _sid = self.started("slow")
        adapter.send_turn(TurnInput("count"))
        for call in (lambda: adapter.steer(TurnInput("more")),
                     lambda: adapter.rollback("t-1"),
                     lambda: adapter.stop_task("task-1")):
            with self.assertRaises(Unsupported):
                call()
        forker = self.adapter()
        with self.assertRaises(Unsupported):
            forker.start(self.spec(resume_id="s", fork=True),
                         adapters_fake.ListSink())

    def test_the_agent_dies_mid_turn(self):
        adapter, sink, _sid = self.started("crash")
        turn_id = adapter.send_turn(TurnInput("go"))
        down = self.wait_state(sink, "agent_down")
        self.assertEqual(sink.types()[-2:], ["turn.ended", "session.state"])
        ended = sink.of("turn.ended")[0]
        self.assertEqual((ended["turn_id"], ended["data"]["status"]),
                         (turn_id, "failed"))
        self.assertIn("fake-agent exited with code 3",
                      down["data"]["reason"])
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput("again"))
        self.assertEqual(ctx.exception.code, "agent_down")
        count = len(sink.events)
        adapter.close(0.5)
        self.assertEqual(len(sink.events), count)

    def test_close_mid_turn_and_an_agent_that_lingers(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count"))
        self.wait(sink, "message.delta")
        adapter.close(1.0)
        self.assertEqual(sink.types()[-2:], ["turn.ended", "session.state"])
        self.assertEqual(sink.of("turn.ended")[0]["turn_id"], turn_id)
        self.assertEqual(adapter._agent.proc.poll(), 0,
                         "it exits when its stdin closes")
        lingerer, sink, _sid = self.started("linger")
        lingerer.close(0.3)
        self.assertEqual(lingerer._agent.proc.poll(), 143, "terminated")
        self.assertEqual(sink.events[-1]["data"]["state"], "closed")

    def test_an_agent_that_stops_reading_is_killed(self):
        adapter, sink, _sid = self.started("deaf", write_timeout=0.3)
        big = "x" * (4 * 1024 * 1024)
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput(big))
        self.assertEqual(ctx.exception.code, "agent_down")
        self.wait_state(sink, "agent_down")
        self.assertIsNotNone(adapter._agent.proc.wait(ACP_TIMEOUT))

    def test_calls_before_start(self):
        adapter = self.adapter()
        for call in (adapter.interrupt,
                     lambda: adapter.respond("r", Answer(decision="allow")),
                     lambda: adapter.set_option("model", "m"),
                     lambda: adapter.steer(TurnInput("x")),
                     lambda: adapter.stop_task("t"),
                     lambda: adapter.rollback("t")):
            with self.assertRaises(AdapterError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "not_started")


class TestAcpIsACapability(unittest.TestCase):
    """``auto`` picks ACP where it is the harness's session adapter."""

    def test_the_session_adapter_decides(self):
        with patch.object(acp, "command_for", return_value=["x"]):
            self.assertTrue(acp.auto_eligible("qwen-code", reg={}))
            self.assertFalse(acp.auto_eligible("claude", reg={}),
                             "claude's session adapter is its stream")
            self.assertFalse(acp.auto_eligible("codex", reg={}))
            self.assertFalse(acp.auto_eligible("nobody", reg={}))
            # harness.json can say otherwise, either way.
            opted = {"claude": {"capabilities": {"session": {
                "adapter": "acp"}}}}
            self.assertTrue(acp.auto_eligible("claude", reg=opted))
            out = {"qwen-code": {"capabilities": {"session": {
                "supported": False}}}}
            self.assertFalse(acp.auto_eligible("qwen-code", reg=out))
        with patch.object(acp, "command_for", return_value=None):
            self.assertFalse(acp.auto_eligible("qwen-code", reg={}))
        self.assertFalse(hasattr(acp, "EXPLICIT_ONLY"))


if __name__ == "__main__":
    unittest.main()
