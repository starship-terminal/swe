"""Tier-A adapter tests: opencode serve + codex app-server.

First the pre-keeper injectors (``remote/inject.py``): no real harness
binaries, ``_spawn`` is patched with scripted fakes (os.pipe for the
app-server's stdio so the real reader thread runs).

Then the G16 session adapters (``remote/adapters/``), from the "G16"
banner on: recorded fixtures, the wire modules, and each adapter over a
fake agent under ``tests/fakes/``, with the shared ``AdapterContract``.
"""

import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

from swe.remote import inject, protocol, supervise
from swe.remote.adapters import (
    codex_appserver, codex_wire, core, events, fake, opencode_serve,
    opencode_wire, registry,
)
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, StartSpec, TurnInput, UnknownRequest,
    Unsupported,
)
from swe.sessions.models import Session

from tests._tempdir import temp_dir
from tests.test_remote_adapter_base import ALL_CAPS, AdapterContract


class _FakeClock:
    """Stands in for ``inject.time``: ``sleep`` moves the clock on at once,
    so a readiness deadline runs out without the test waiting for it."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _session(tool="opencode", sid="ses_1", cwd="/tmp/proj"):
    return Session(
        timestamp=0, agent=tool, path=cwd, title="t",
        session_id=sid, tool_name=tool,
    )


def _meta(mode, tool):
    return {"mode": mode, "tool_name": tool, "session_id": "s1"}


# -- opencode serve -----------------------------------------------------


class _FakeServeProc:
    def __init__(self):
        self.killed = False

    def poll(self):
        return None if not self.killed else 0

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class TestOpencodeServe(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        inject.OpencodeServeInjector._serves.clear()
        self.calls = []          # (method, path, payload)
        self.proc = _FakeServeProc()
        # scripted replies keyed by path fragment
        self.routes = {"session": (200, b"[]")}

        def fake_call(base, method, path, payload, timeout=20.0):
            self.calls.append((method, path, payload))
            frags = sorted(self.routes, key=len, reverse=True)
            for frag in frags:
                if frag in path:
                    return self.routes[frag]
            return (404, b"{}")

        proc = self.proc
        self._patchers = [
            patch.object(
                inject.OpencodeServeInjector, "_spawn",
                staticmethod(lambda *a, **k: proc),
            ),
            patch.object(inject, "_serve_call", fake_call),
            # The spawned serve's event reader would dial a real local
            # port; the scripted replies above stand in for the serve.
            patch.object(inject, "_serve_event_loop", lambda serve: None),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        inject.OpencodeServeInjector._serves.clear()

    def _injector(self, emit=None):
        return inject.OpencodeServeInjector(
            self.home, "opencode:s1", emit=emit, machine="m1"
        )

    def test_injector_for_dispatch(self):
        inj = inject.injector_for(
            self.home, "k", _meta("serve", "opencode"))
        self.assertIsInstance(inj, inject.OpencodeServeInjector)
        inj2 = inject.injector_for(
            self.home, "k", _meta("serve", "claude"))
        self.assertIsInstance(inj2, inject.TmuxInjector)

    def test_send_posts_prompt_async(self):
        session = _session()
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        res = self._injector().send(
            session, {"text": "hi there", "mode": "queue"}, "idle")
        self.assertTrue(res.ok)
        self.assertEqual(
            self.calls[-1],
            ("POST", "/session/ses_1/prompt_async",
             {"parts": [{"type": "text", "text": "hi there"}]}),
        )

    def test_send_attachments_prefix(self):
        session = _session()
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        self._injector().send(
            session,
            {"text": "see this", "mode": "queue",
             "attachments": ["/tmp/a.png"]},
            "idle")
        sent = self.calls[-1][2]["parts"][0]["text"]
        # opencode's API never expands an @ ref: the path goes in plain.
        self.assertEqual(sent, "[Attached file: /tmp/a.png] see this")

    def test_send_rides_the_attach_pick_on_wire_fields(self):
        session = _session()
        meta = {**_meta("serve", "opencode"),
                "model": "acme/kimi-k3", "effort": "high"}
        supervise.mark(self.home, "opencode:s1", meta)
        res = self._injector().send(
            session, {"text": "hi", "mode": "queue"}, "idle")
        self.assertTrue(res.ok)
        payload = self.calls[-1][2]
        self.assertEqual(payload["model"],
                         {"providerID": "acme", "modelID": "kimi-k3"})
        self.assertEqual(payload["variant"], "high")

    def test_model_switch_rides_the_next_prompt(self):
        session = _session()
        supervise.mark(self.home, "opencode:s1",
                       {**_meta("serve", "opencode"), "model": "acme/kimi-k3"})
        inj = self._injector()
        res = inj.control(session, "model", "openrouter/anthropic/claude-4")
        self.assertTrue(res.ok, res.note)
        self.assertIn("next turn", res.note)
        self.assertEqual(self.calls, [])  # nothing is sent now
        res = inj.send(session, {"text": "hi", "mode": "queue"}, "idle")
        self.assertTrue(res.ok)
        # split at the first slash: the rest is the model id
        self.assertEqual(self.calls[-1][2]["model"],
                         {"providerID": "openrouter",
                          "modelID": "anthropic/claude-4"})

    def test_model_switch_refusals(self):
        session = _session()
        inj = self._injector()
        res = inj.control(session, "model", "acme/kimi-k3")
        self.assertFalse(res.ok)
        self.assertIn("detached", res.note)
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        # a bare name, an empty side, empty, a second line
        for bad in ("kimi-k3", "acme/", "", "acme/k\n/clear"):
            res = inj.control(session, "model", bad)
            self.assertFalse(res.ok, bad)
        self.assertIn("provider/model", inj.control(
            session, "model", "kimi-k3").note)
        self.assertNotIn("model", supervise.meta_for(self.home, "opencode:s1"))

    def test_send_steer_degrades_when_active(self):
        session = _session()
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        res = self._injector().send(
            session, {"text": "x", "mode": "steer"}, "active")
        self.assertTrue(res.ok)
        self.assertTrue(res.degraded)

    def test_send_requires_supervised(self):
        session = _session()
        res = self._injector().send(
            session, {"text": "x", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertIn("detached", res.note)

    def test_send_spawn_failure(self):
        spawn = MagicMock(side_effect=OSError("no opencode"))
        with patch.object(
            inject.OpencodeServeInjector, "_spawn", staticmethod(spawn),
        ):
            # Marked under the injector's own key, so the send gets past
            # the supervision check and reaches the spawn.
            supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
            res = self._injector().send(
                _session(), {"text": "x", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertEqual(res.note, "opencode serve failed to start")
        spawn.assert_called_once()
        self.assertEqual(inject.OpencodeServeInjector._serves, {})

    def test_approval_posts_mapped_decision(self):
        session = _session()
        supervise.mark(self.home, "k", _meta("serve", "opencode"))
        self.routes["permissions"] = (200, b"{}")
        res = self._injector().approval(session, "perm-9", "always")
        self.assertTrue(res.ok)
        method, path, payload = self.calls[-1]
        self.assertEqual(method, "POST")
        self.assertIn("/session/ses_1/permissions/perm-9", path)
        self.assertEqual(payload, {"response": "always"})

    def test_approval_falls_back_to_legacy_reply(self):
        session = _session()
        supervise.mark(self.home, "k", _meta("serve", "opencode"))
        # /permissions 404s; the legacy /permission/{id}/reply answers.
        self.routes["permissions"] = (404, b"")
        self.routes["reply"] = (200, b"{}")
        res = self._injector().approval(session, "perm-1", "deny")
        self.assertTrue(res.ok)
        self.assertIn("/permission/perm-1/reply", self.calls[-1][1])
        self.assertEqual(self.calls[-1][2], {"reply": "reject"})

    def test_approval_request_id_stays_one_path_segment(self):
        # The request id is the client's string: it must not climb to
        # another serve route such as /session/<id>/abort.
        session = _session()
        supervise.mark(self.home, "k", _meta("serve", "opencode"))
        self.routes["permissions"] = (404, b"")
        self.routes["reply"] = (200, b"{}")
        self._injector().approval(session, "../../ses_1/abort?x=1#", "deny")
        paths = [path for _m, path, _p in self.calls[-2:]]
        self.assertEqual(paths, [
            "/session/ses_1/permissions/..%2F..%2Fses_1%2Fabort%3Fx%3D1%23",
            "/permission/..%2F..%2Fses_1%2Fabort%3Fx%3D1%23/reply"])

    def test_approval_unknown_decision(self):
        res = self._injector().approval(_session(), "p1", "bogus")
        self.assertFalse(res.ok)

    def test_interrupt_posts_abort(self):
        session = _session()
        supervise.mark(self.home, "k", _meta("serve", "opencode"))
        self.routes["abort"] = (200, b"{}")
        res = self._injector().control(session, "interrupt", "")
        self.assertTrue(res.ok)
        self.assertIn("/session/ses_1/abort", self.calls[-1][1])

    def test_detach_releases_and_unmarks(self):
        session = _session()
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        inj = self._injector()
        inj.send(session, {"text": "x", "mode": "queue"}, "idle")
        res = inj.control(session, "detach", "")
        self.assertTrue(res.ok)
        self.assertIsNone(supervise.meta_for(self.home, "opencode:s1"))
        self.assertTrue(self.proc.killed)
        self.assertEqual(inject.OpencodeServeInjector._serves, {})

    def test_serve_event_permission_asked_emits(self):
        events = []
        serve = inject._ServeProc(_FakeServeProc(), "http://x")
        serve.subs["ses_1"] = (
            "k", lambda k, ev: events.append(ev), "m1")
        inject._serve_event(serve, json.dumps({
            "type": "permission.asked",
            "properties": {
                "id": "perm-7", "sessionID": "ses_1",
                "title": "run bash", "patterns": ["ls *"],
            },
        }).encode())
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["type"], "approval.requested")
        self.assertEqual(ev["session"], "k")
        self.assertEqual(ev["data"]["request_id"], "perm-7")
        self.assertIn("run bash", ev["data"]["prompt"])

    def test_serve_event_replied_resolves(self):
        events = []
        serve = inject._ServeProc(_FakeServeProc(), "http://x")
        serve.subs["ses_1"] = (
            "k", lambda k, ev: events.append(ev), "m1")
        serve.permissions["perm-3"] = "ses_1"
        inject._serve_event(serve, json.dumps({
            "type": "permission.replied",
            "properties": {"id": "perm-3", "sessionID": "ses_1"},
        }).encode())
        self.assertEqual(events[0]["type"], "approval.resolved")
        self.assertEqual(events[0]["data"]["request_id"], "perm-3")
        self.assertNotIn("perm-3", serve.permissions)

    def test_serve_event_ignores_other_types_and_sessions(self):
        events = []
        serve = inject._ServeProc(_FakeServeProc(), "http://x")
        serve.subs["ses_1"] = (
            "k", lambda k, ev: events.append(ev), "m1")
        for blob in (
            {"type": "session.idle", "properties": {"sessionID": "s"}},
            {"type": "permission.asked",
             "properties": {"id": "p", "sessionID": "other"}},
            {"type": "permission.asked", "properties": {}},
        ):
            inject._serve_event(serve, json.dumps(blob).encode())
        self.assertEqual(events, [])

    def test_serve_shared_per_cwd(self):
        supervise.mark(self.home, "k1", _meta("serve", "opencode"))
        supervise.mark(self.home, "k2", _meta("serve", "opencode"))
        s1, s2 = _session(sid="ses_a"), _session(sid="ses_b")
        inject.OpencodeServeInjector(self.home, "k1").send(
            s1, {"text": "a", "mode": "queue"}, "idle")
        inject.OpencodeServeInjector(self.home, "k2").send(
            s2, {"text": "b", "mode": "queue"}, "idle")
        serve = next(iter(inject.OpencodeServeInjector._serves.values()))
        self.assertEqual(serve.refs, 2)
        self.assertEqual(set(serve.subs), {"ses_a", "ses_b"})


    def test_release_after_respawn_keeps_the_new_serve(self):
        # A and B share serve 1; it dies and B's next send spawns
        # serve 2. A never subscribed to serve 2, so A detaching must
        # not drop B's hold and kill it.
        procs = [_FakeServeProc(), _FakeServeProc()]
        spawned = iter(procs)
        supervise.mark(self.home, "k1", _meta("serve", "opencode"))
        supervise.mark(self.home, "k2", _meta("serve", "opencode"))
        a, b = _session(sid="ses_a"), _session(sid="ses_b")
        inj_a = inject.OpencodeServeInjector(self.home, "k1")
        inj_b = inject.OpencodeServeInjector(self.home, "k2")
        with patch.object(inject.OpencodeServeInjector, "_spawn",
                          staticmethod(lambda *a_, **k: next(spawned))):
            inj_a.send(a, {"text": "a", "mode": "queue"}, "idle")
            inj_b.send(b, {"text": "b", "mode": "queue"}, "idle")
            procs[0].killed = True  # serve 1 exits on its own
            inj_b.send(b, {"text": "b2", "mode": "queue"}, "idle")
        inj_a._release(a)
        self.assertFalse(procs[1].killed)
        serve = next(iter(inject.OpencodeServeInjector._serves.values()))
        self.assertIs(serve.proc, procs[1])
        self.assertEqual(serve.refs, 1)
        inj_b._release(b)
        self.assertTrue(procs[1].killed)

    def test_ready_probe_after_another_waiter_gave_up_is_refused(self):
        # Waiter B times out and kills the proc while waiter A's probe
        # is in flight; A must not subscribe to (and send through) it.
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))

        def probe(base, method, path, payload, timeout=20.0):
            serves = inject.OpencodeServeInjector._serves
            for cwd, serve in list(serves.items()):
                serve.dead = True  # B's cleanup lands mid-probe
                serves.pop(cwd, None)
            return (200, b"[]")

        with patch.object(inject, "_serve_call", probe):
            res = self._injector().send(
                _session(), {"text": "x", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)

    def test_serve_exiting_before_ready_is_unregistered(self):
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        self.proc.killed = True  # poll() reports it exited
        res = self._injector().send(
            _session(), {"text": "x", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertEqual(inject.OpencodeServeInjector._serves, {})

    def test_timed_out_waiter_leaves_a_held_serve_alone(self):
        # Another session already holds the proc: a waiter that gives
        # up must not kill it under them.
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        held = inject._ServeProc(self.proc, "http://127.0.0.1:1")
        held.refs = 1
        held.reader_started = True
        cwd = os.path.realpath("/tmp/proj")
        inject.OpencodeServeInjector._serves[cwd] = held
        self.routes = {"session": (0, b"")}
        with patch.object(inject, "time", _FakeClock()):
            res = self._injector().send(
                _session(), {"text": "x", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertFalse(self.proc.killed)
        self.assertIs(inject.OpencodeServeInjector._serves[cwd], held)

    def test_kill_failure_on_timeout_still_unregisters(self):
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        self.routes = {"session": (0, b"")}
        with patch.object(inject, "time", _FakeClock()), \
             patch.object(self.proc, "kill", side_effect=OSError("gone")):
            res = self._injector().send(
                _session(), {"text": "x", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertEqual(inject.OpencodeServeInjector._serves, {})

    def test_serve_that_never_gets_ready_is_killed(self):
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        self.routes = {"session": (0, b"")}  # never answers
        with patch.object(inject, "time", _FakeClock()):
            res = self._injector().send(
                _session(), {"text": "x", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertTrue(self.proc.killed)
        self.assertEqual(inject.OpencodeServeInjector._serves, {})

# -- codex app-server ---------------------------------------------------


class _Pipe:
    """Write end of the fake proc's stdin; feeds canned responses."""

    def __init__(self, proc):
        self.proc = proc
        self.buf = b""

    def write(self, data):
        self.proc._on_stdin(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


class _FakeAppProc:
    """`codex app-server` stand-in: os.pipe stdout so the injector's
    real reader thread runs; scripted replies keyed by method."""

    def __init__(self, script=None):
        self.script = script or {}
        self.writes = []
        self.killed = False
        self._rfd, self._wfd = os.pipe()
        self.stdout = os.fdopen(self._rfd, "rb", buffering=0)
        self.stdin = _Pipe(self)

    def _on_stdin(self, data):
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self.writes.append(msg)
            method = msg.get("method")
            if method and "id" in msg and method in self.script:
                resp = dict(self.script[method])
                resp.setdefault("jsonrpc", "2.0")
                resp["id"] = msg["id"]
                self.push(resp)

    def push(self, obj):
        """A server->client line: response, request or notification."""
        os.write(self._wfd, json.dumps(obj).encode() + b"\n")

    def poll(self):
        return None if not self.killed else 0

    def kill(self):
        self.killed = True
        try:
            os.close(self._wfd)
        except OSError:
            pass

    def wait(self, timeout=None):
        return 0


_HANDSHAKE = {
    "initialize": {"result": {"serverInfo": {"name": "codex"}}},
    "thread/resume": {"result": {"thread": {"id": "thr-1"}}},
    "turn/start": {"result": {"turn": {"id": "turn-9", "status": "ok"}}},
    "turn/interrupt": {"result": {}},
}


class TestCodexAppServer(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        inject.CodexAppServerInjector._servers.clear()
        self.procs = []

        def spawn(cwd):
            proc = _FakeAppProc(dict(_HANDSHAKE))
            self.procs.append(proc)
            return proc

        self._patch = patch.object(
            inject.CodexAppServerInjector, "_spawn",
            staticmethod(spawn),
        )
        self._patch.start()
        # `thread/resume` needs a uuid inside the session id, same as
        # `codex resume <uuid>` — the parser lifts it out of the stem.
        self.session = _session(
            tool="codex",
            sid="rollout-2026-10-22T01-00-00-"
                "123e4567-e89b-42d3-a456-426614174000",
            cwd="/tmp/proj",
        )

    def tearDown(self):
        self._patch.stop()
        for key, server in list(inject.CodexAppServerInjector._servers.items()):
            try:
                server.proc.kill()
            except Exception:
                pass
        inject.CodexAppServerInjector._servers.clear()

    def _injector(self, emit=None):
        return inject.CodexAppServerInjector(
            self.home, "codex:k", emit=emit, machine="m1"
        )

    def test_injector_for_dispatch(self):
        inj = inject.injector_for(
            self.home, "k", _meta("appserver", "codex"))
        self.assertIsInstance(inj, inject.CodexAppServerInjector)
        inj2 = inject.injector_for(
            self.home, "k", _meta("appserver", "claude"))
        self.assertIsInstance(inj2, inject.TmuxInjector)

    def test_send_handshakes_then_turn_start(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        res = self._injector().send(
            self.session, {"text": "hello", "mode": "queue"}, "idle")
        self.assertTrue(res.ok, res.note)
        methods = [w["method"] for w in self.procs[0].writes]
        self.assertEqual(
            methods[:4],
            ["initialize", "initialized", "thread/resume", "turn/start"],
        )
        start = self.procs[0].writes[-1]
        self.assertEqual(
            start["params"]["input"],
            [{"type": "text", "text": "hello"}],
        )

    def test_send_rides_the_attach_pick_as_turn_overrides(self):
        meta = {**_meta("appserver", "codex"),
                "model": "gpt-5", "effort": "high"}
        supervise.mark(self.home, "codex:k", meta)
        res = self._injector().send(
            self.session, {"text": "go", "mode": "queue"}, "idle")
        self.assertTrue(res.ok, res.note)
        start = self.procs[0].writes[-1]
        self.assertEqual(start["method"], "turn/start")
        self.assertEqual(start["params"]["model"], "gpt-5")
        self.assertEqual(start["params"]["effort"], "high")

    def test_model_switch_rides_the_next_turn_start(self):
        supervise.mark(self.home, "codex:k",
                       {**_meta("appserver", "codex"), "model": "gpt-5"})
        inj = self._injector()
        res = inj.control(self.session, "model", "gpt-5.1-codex")
        self.assertTrue(res.ok, res.note)
        self.assertIn("next turn", res.note)
        # nothing is sent now: no app-server was even spawned
        self.assertEqual(self.procs, [])
        self.assertEqual(
            supervise.meta_for(self.home, "codex:k")["model"],
            "gpt-5.1-codex")
        res = inj.send(self.session, {"text": "go", "mode": "queue"}, "idle")
        self.assertTrue(res.ok, res.note)
        start = self.procs[0].writes[-1]
        self.assertEqual(start["method"], "turn/start")
        self.assertEqual(start["params"]["model"], "gpt-5.1-codex")

    def test_model_switch_refusals(self):
        inj = self._injector()
        # detached: nothing to record it on
        res = inj.control(self.session, "model", "gpt-5")
        self.assertFalse(res.ok)
        self.assertIn("detached", res.note)
        self.assertIsNone(supervise.meta_for(self.home, "codex:k"))
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        res = inj.control(self.session, "model", "")
        self.assertFalse(res.ok)
        self.assertIn("needs a value", res.note)
        # the launch rule holds here too, not only at the route
        res = inj.control(self.session, "model", "gpt-5\n/clear")
        self.assertFalse(res.ok)
        self.assertNotIn("model", supervise.meta_for(self.home, "codex:k"))
        # a failed save is a failed switch, not a silent one
        with patch.object(supervise, "atomic_write_text",
                          side_effect=OSError("disk full")):
            res = inj.control(self.session, "model", "gpt-5")
        self.assertFalse(res.ok)
        self.assertIn("model unchanged", res.note)

    def test_send_queue_refuses_when_active(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        res = self._injector().send(
            self.session, {"text": "x", "mode": "queue"}, "active")
        self.assertFalse(res.ok)
        self.assertIn("mid-turn", res.note)

    def test_send_detached_refuses(self):
        res = self._injector().send(
            self.session, {"text": "x", "mode": "steer"}, "idle")
        self.assertFalse(res.ok)

    def test_send_no_thread_id(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        bad = _session(tool="codex", sid="rollout-no-uuid-here")
        res = self._injector().send(
            bad, {"text": "x", "mode": "steer"}, "idle")
        self.assertFalse(res.ok)

    def test_server_approval_request_surfaces_and_answers(self):
        events = []
        asked = threading.Event()

        def emit(_key, ev):
            events.append(ev)
            asked.set()

        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        inj = self._injector(emit=emit)
        res = inj.send(self.session, {"text": "go", "mode": "steer"}, "idle")
        self.assertTrue(res.ok)
        proc = self.procs[0]
        # Server initiates an approval request mid-turn.
        proc.push({
            "jsonrpc": "2.0", "id": 777,
            "method": "item/commandExecution/requestApproval",
            "params": {"reason": "wants to run rm -rf", "command": "rm -rf"},
        })
        # The reader thread emits it; wait on that instead of polling.
        self.assertTrue(asked.wait(5))
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["type"], "approval.requested")
        self.assertEqual(ev["data"]["request_id"], "777")
        self.assertIn("rm -rf", ev["data"]["prompt"])

        res = inj.approval(self.session, "777", "always")
        self.assertTrue(res.ok, res.note)
        reply = self.procs[0].writes[-1]
        self.assertEqual(reply["id"], 777)
        self.assertEqual(reply["result"], {"decision": "acceptForSession"})
        # A replayed request_id is refused.
        res = inj.approval(self.session, "777", "allow")
        self.assertFalse(res.ok)

    def test_approval_unknown_request(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        inj = self._injector()
        self.assertTrue(
            inj.send(self.session, {"text": "x", "mode": "steer"},
                     "idle").ok)
        res = inj.approval(self.session, "999", "allow")
        self.assertFalse(res.ok)

    def test_interrupt_sends_turn_interrupt(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        inj = self._injector()
        self.assertTrue(
            inj.send(self.session, {"text": "x", "mode": "steer"},
                     "idle").ok)
        res = inj.control(self.session, "interrupt", "")
        self.assertTrue(res.ok)
        last = self.procs[0].writes[-1]
        self.assertEqual(last["method"], "turn/interrupt")
        self.assertEqual(last["params"]["turnId"], "turn-9")

    def test_detach_kills_and_unmarks(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        inj = self._injector()
        self.assertTrue(
            inj.send(self.session, {"text": "x", "mode": "steer"},
                     "idle").ok)
        res = inj.control(self.session, "detach", "")
        self.assertTrue(res.ok)
        self.assertIsNone(supervise.meta_for(self.home, "codex:k"))
        self.assertTrue(self.procs[0].killed)
        self.assertEqual(inject.CodexAppServerInjector._servers, {})

    def test_unsupported_control(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        res = self._injector().control(self.session, "compact", "")
        self.assertFalse(res.ok)
        self.assertIn("unsupported", res.note)


# -- mode resolution + attach --------------------------------------------


class TestAttachModes(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)

    def test_resolve_mode_matrix(self):
        oc = _session(tool="opencode")
        cx = _session(tool="codex", sid="x")
        cl = _session(tool="claude", sid="x")
        with patch("shutil.which", return_value="/bin/x"):
            self.assertEqual(supervise.resolve_mode(oc, "auto"), "serve")
            self.assertEqual(supervise.resolve_mode(cx, "auto"), "appserver")
            self.assertEqual(supervise.resolve_mode(cl, "auto"), "tmux")
            self.assertEqual(supervise.resolve_mode(oc, "tmux"), "tmux")
        with patch("shutil.which", return_value=None):
            self.assertEqual(supervise.resolve_mode(oc, "auto"), "tmux")
            self.assertEqual(supervise.resolve_mode(cx, "auto"), "tmux")

    def test_attach_serve_mode_marks_registry(self):
        session = _session(tool="opencode")
        with patch("shutil.which", return_value="/bin/opencode"), \
                patch.object(supervise, "_stop_local_writers"):
            ok, note = supervise.attach(
                self.home, "opencode:s1", session,
                last_append_at=None, status="idle", mode="serve",
            )
        self.assertTrue(ok, note)
        meta = supervise.meta_for(self.home, "opencode:s1")
        self.assertEqual(meta["mode"], "serve")
        self.assertNotIn("tmux", meta)

    def test_attach_appserver_mode(self):
        session = _session(
            tool="codex",
            sid="rollout-2026-10-22T01-00-00-"
                "123e4567-e89b-42d3-a456-426614174000",
        )
        with patch("shutil.which", return_value="/bin/codex"), \
                patch.object(supervise, "_stop_local_writers"):
            ok, note = supervise.attach(
                self.home, "codex:x", session,
                last_append_at=None, status="idle", mode="appserver",
            )
        self.assertTrue(ok, note)
        self.assertEqual(
            supervise.meta_for(self.home, "codex:x")["mode"], "appserver")

    def test_attach_auto_picks_api_when_binary_exists(self):
        session = _session(tool="opencode")
        with patch("shutil.which", return_value="/bin/opencode"), \
                patch.object(supervise, "_stop_local_writers"):
            ok, note = supervise.attach(
                self.home, "opencode:s1", session,
                last_append_at=None, status="idle", mode="auto",
            )
        self.assertTrue(ok, note)
        self.assertEqual(
            supervise.meta_for(self.home, "opencode:s1")["mode"], "serve")

    def test_attach_serve_rejects_wrong_harness(self):
        session = _session(tool="claude", sid="x")
        ok, note = supervise.attach(
            self.home, "claude:x", session,
            last_append_at=None, status="idle", mode="serve",
        )
        self.assertFalse(ok)
        self.assertIn("opencode", note)

    def test_attach_serve_requires_binary(self):
        session = _session(tool="opencode")
        with patch("shutil.which", return_value=None):
            ok, note = supervise.attach(
                self.home, "opencode:s1", session,
                last_append_at=None, status="idle", mode="serve",
            )
        self.assertFalse(ok)
        self.assertIn("not installed", note)

    def test_supervised_keeps_api_modes_alive(self):
        # non-tmux rows are never pruned by the tmux liveness check.
        supervise.mark(self.home, "k", _meta("serve", "opencode"))
        self.assertIn("k", supervise.supervised(self.home))


class TestOpencodeServeEdges(unittest.TestCase):
    """Error/lifecycle branches of the opencode serve adapter."""

    def setUp(self):
        self.home = temp_dir(self)
        inject.OpencodeServeInjector._serves.clear()

    def tearDown(self):
        inject.OpencodeServeInjector._serves.clear()

    def _injector(self):
        return inject.OpencodeServeInjector(self.home, "opencode:s1")

    def test_serve_call_error_paths(self):
        import urllib.error
        err = urllib.error.HTTPError("http://x", 409, "c", {}, io.BytesIO(b"no"))
        with patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(inject._serve_call("http://x", "GET", "/p", None),
                             (409, b"no"))
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(inject._serve_call("http://x", "GET", "/p", None),
                             (0, b""))
        # A 200 with a body passes through.
        ok = MagicMock()
        ok.status = 200
        ok.read.return_value = b"{}"
        ok.__enter__.return_value = ok
        with patch("urllib.request.urlopen", return_value=ok):
            self.assertEqual(inject._serve_call("http://x", "GET", "/p", {}),
                             (200, b"{}"))
        # HTTPError whose error body itself can't be read → code + b"".
        fp = MagicMock()
        fp.read.side_effect = OSError("gone")
        err2 = urllib.error.HTTPError("http://x", 500, "c", {}, fp)
        with patch("urllib.request.urlopen", side_effect=err2):
            self.assertEqual(inject._serve_call("http://x", "GET", "/p", None),
                             (500, b""))

    def test_event_loop_urlopen_failure_returns(self):
        serve = inject._ServeProc(_FakeServeProc(), "http://x")
        deltas = MagicMock()
        serve.subs["ses_1"] = ("k", None, "m")
        serve.deltas["ses_1"] = deltas
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as urlopen:
            # must return quietly, not raise
            inject._serve_event_loop(serve)
        self.assertEqual(urlopen.call_args[0][0].full_url, "http://x/event")
        # No stream ever opened, so nothing was mid-stream to close.
        deltas.finish_all.assert_not_called()

    def test_event_loop_read_error_swallowed(self):
        class BadResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                raise OSError("reset")

        serve = inject._ServeProc(_FakeServeProc(), "http://x")
        deltas = MagicMock()
        serve.subs["ses_1"] = ("k", None, "m")
        serve.deltas["ses_1"] = deltas
        with patch("urllib.request.urlopen", return_value=BadResp()):
            inject._serve_event_loop(serve)
        # The dropped stream closes what each session had mid-stream.
        deltas.finish_all.assert_called_once_with("k")

    def test_event_loop_parses_sse_frames(self):
        events = []
        serve = inject._ServeProc(_FakeServeProc(), "http://x")
        serve.subs["ses_1"] = ("k", lambda k, ev: events.append(ev), "m")
        blob = json.dumps({
            "type": "permission.asked",
            "properties": {"id": "p1", "sessionID": "ses_1",
                           "title": "run it"},
        }).encode()
        resp = io.BytesIO(b"data: " + blob + b"\n\n")
        ctx = MagicMock()
        ctx.__enter__.return_value = resp
        ctx.__exit__.return_value = False
        ctx.__iter__.return_value = iter(resp.readlines())
        with patch("urllib.request.urlopen", return_value=ctx):
            inject._serve_event_loop(serve)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["request_id"], "p1")

    def test_serve_event_garbage_and_missing_bits(self):
        serve = inject._ServeProc(_FakeServeProc(), "http://x")
        serve.subs["ses_1"] = ("k", None, "m")  # emit=None path
        inject._serve_event(serve, b"not json")
        inject._serve_event(serve, b"[1,2]")
        inject._serve_event(serve, json.dumps({"type": "x"}).encode())
        # known session but emit=None returns quietly — nothing tracks
        inject._serve_event(serve, json.dumps({
            "type": "permission.asked",
            "properties": {"id": "p1", "sessionID": "ses_1"}}).encode())
        self.assertNotIn("p1", serve.permissions)

    def test_serve_event_asked_without_id(self):
        events = []
        serve = inject._ServeProc(_FakeServeProc(), "http://x")
        serve.subs["ses_1"] = ("k", lambda k, ev: events.append(ev), "m")
        inject._serve_event(serve, json.dumps({
            "type": "permission.asked",
            "properties": {"sessionID": "ses_1"}}).encode())
        self.assertEqual(events, [])
        # a resolved event pops the tracked permission id
        serve.permissions["p9"] = "ses_1"
        inject._serve_event(serve, json.dumps({
            "type": "permission.replied",
            "properties": {"id": "p9", "sessionID": "ses_1"}}).encode())
        self.assertNotIn("p9", serve.permissions)
        self.assertEqual(events[0]["data"]["request_id"], "p9")

    def test_serve_for_evicts_dead_and_spawn_errors(self):
        cwd = os.path.realpath("/tmp/proj")
        dead = _FakeServeProc()
        dead.killed = True
        inject.OpencodeServeInjector._serves[cwd] = (
            inject._ServeProc(dead, "http://x"))

        def spawn_fail(*a, **k):
            raise OSError("no opencode")

        inj = self._injector()
        old = inject.OpencodeServeInjector._serves[cwd]
        spawn = MagicMock(side_effect=spawn_fail)
        with patch.object(
            inject.OpencodeServeInjector, "_spawn",
            staticmethod(spawn),
        ):
            self.assertIsNone(inj._serve_for(_session()))
        # The dead serve was dropped and marked, and a fresh spawn tried.
        self.assertTrue(old.dead)
        self.assertNotIn(cwd, inject.OpencodeServeInjector._serves)
        spawn.assert_called_once()

    def test_serve_for_proc_dies_during_readiness(self):
        proc = _FakeServeProc()
        inj = self._injector()

        calls = {"n": 0}

        def kill_then_serve(base, method, path, payload, timeout=20.0):
            calls["n"] += 1
            if calls["n"] == 1:
                proc.killed = True   # dies before it ever answers
            return (0, b"")

        with patch.object(
            inject.OpencodeServeInjector, "_spawn",
            staticmethod(lambda *a, **k: proc),
        ), patch.object(
            inject, "_serve_call", kill_then_serve
        ), patch.object(inject, "time", _FakeClock()), \
                patch.object(inject, "_serve_event_loop", lambda serve: None):
            self.assertIsNone(inj._serve_for(_session()))
        # One probe, then the next pass saw the exit and evicted it.
        self.assertEqual(calls["n"], 1)
        self.assertEqual(inject.OpencodeServeInjector._serves, {})

    def test_serve_for_readiness_timeout(self):
        proc = _FakeServeProc()
        inj = self._injector()
        clock = _FakeClock()
        with patch.object(
            inject.OpencodeServeInjector, "_spawn",
            staticmethod(lambda *a, **k: proc),
        ), patch.object(
            inject, "_serve_call", lambda *a, **k: (0, b"")
        ), patch.object(inject, "time", clock), \
                patch.object(inject, "_serve_event_loop", lambda serve: None):
            self.assertIsNone(inj._serve_for(_session()))
        # It probed until the whole readiness window had passed, then
        # killed the proc nobody else holds rather than leak it.
        self.assertGreaterEqual(sum(clock.slept), inject._SERVE_READY_S)
        self.assertTrue(proc.killed)
        self.assertEqual(inject.OpencodeServeInjector._serves, {})

    def test_release_without_and_with_refs(self):
        cwd = os.path.realpath("/tmp/proj")
        inj = self._injector()
        inj._release(_session())  # no serve → quiet no-op
        proc = _FakeServeProc()
        serve = inject._ServeProc(proc, "http://x")
        serve.refs = 2
        serve.subs["ses_a"] = ("k1", None, "m")
        serve.subs["ses_b"] = ("k2", None, "m")
        inject.OpencodeServeInjector._serves[cwd] = serve
        inj._release(_session(sid="ses_a"))   # 2→1: no kill
        self.assertFalse(proc.killed)
        inj._release(_session(sid="ses_b"))   # 1→0: kill + evict
        self.assertTrue(proc.killed)
        self.assertEqual(inject.OpencodeServeInjector._serves, {})

    def test_release_kill_failure_swallowed(self):
        cwd = os.path.realpath("/tmp/proj")

        class BadKill(_FakeServeProc):
            def kill(self):
                raise OSError("already dead")

        serve = inject._ServeProc(BadKill(), "http://x")
        serve.refs = 1
        serve.subs["ses_1"] = ("k", None, "m")
        inject.OpencodeServeInjector._serves[cwd] = serve
        self._injector()._release(_session())
        self.assertEqual(inject.OpencodeServeInjector._serves, {})

    def test_send_empty_and_refused(self):
        session = _session()
        res = self._injector().send(
            session, {"text": "  ", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertIn("empty", res.note)
        # supervised but serve spawn fails → refused
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        with patch.object(
            inject.OpencodeServeInjector, "_spawn",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(OSError())),
        ):
            res = self._injector().send(
                session, {"text": "x", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertIn("failed to start", res.note)

    def test_send_prompt_refused_status(self):
        session = _session()
        supervise.mark(self.home, "opencode:s1", _meta("serve", "opencode"))
        calls = []

        def fake(base, method, path, payload, timeout=20.0):
            calls.append(path)
            if "prompt_async" in path:
                return (429, b"busy")
            return (200, b"[]")   # /session readiness probe

        with patch.object(
            inject.OpencodeServeInjector, "_spawn",
            staticmethod(lambda *a, **k: _FakeServeProc()),
        ), patch.object(inject, "_serve_call", fake):
            res = self._injector().send(
                session, {"text": "x", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertIn("429", res.note)

    def test_approval_serve_fail_and_legacy_fail(self):
        session = _session()
        with patch.object(
            inject.OpencodeServeInjector, "_spawn",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(OSError())),
        ):
            res = self._injector().approval(session, "p1", "allow")
        self.assertFalse(res.ok)
        # serve up but both endpoints reject
        def fake(base, method, path, payload, timeout=20.0):
            if "/session" in path and "permissions" not in path:
                return (200, b"[]")
            return (500, b"")
        with patch.object(
            inject.OpencodeServeInjector, "_spawn",
            staticmethod(lambda *a, **k: _FakeServeProc()),
        ), patch.object(inject, "_serve_call", fake):
            res = self._injector().approval(session, "p1", "allow")
        self.assertFalse(res.ok)
        self.assertIn("500", res.note)

    def test_control_paths(self):
        session = _session()
        inj = self._injector()
        # interrupt with no serve proc → spawn fails → refused
        def spawn_fail(*a, **k):
            raise OSError("no opencode")
        with patch.object(
            inject.OpencodeServeInjector, "_spawn",
            staticmethod(spawn_fail),
        ):
            res = inj.control(session, "interrupt", "")
        self.assertFalse(res.ok)
        # serve up but the abort call itself is refused
        def fake(base, method, path, payload, timeout=20.0):
            if "/session" in path and "abort" not in path:
                return (200, b"[]")
            return (500, b"")
        with patch.object(
            inject.OpencodeServeInjector, "_spawn",
            staticmethod(lambda *a, **k: _FakeServeProc()),
        ), patch.object(inject, "_serve_call", fake):
            res = inj.control(session, "interrupt", "")
        self.assertFalse(res.ok)
        self.assertIn("refused abort", res.note)
        # unsupported control op
        res = inj.control(session, "compact", "")
        self.assertFalse(res.ok)
        self.assertIn("unsupported", res.note)


class TestCodexAppServerEdges(unittest.TestCase):
    """Error/handshake branches of the codex app-server adapter."""

    def setUp(self):
        self.home = temp_dir(self)
        inject.CodexAppServerInjector._servers.clear()
        self.session = _session(
            tool="codex",
            sid="rollout-2026-10-22T01-00-00-"
                "123e4567-e89b-42d3-a456-426614174000",
        )

    def tearDown(self):
        inject.CodexAppServerInjector._servers.clear()

    def _injector(self, emit=None):
        return inject.CodexAppServerInjector(
            self.home, "codex:k", emit=emit, machine="m1")

    def _spawn_patch(self, proc):
        return patch.object(
            inject.CodexAppServerInjector, "_spawn",
            staticmethod(lambda *a, **k: proc))

    def test_rpc_line_broken_pipe(self):
        proc = MagicMock()
        proc.stdin.write.side_effect = BrokenPipeError()
        self.assertFalse(inject._rpc_line(proc, {"a": 1}))

    def test_read_loop_demuxes_and_skips_garbage(self):
        proc = _FakeAppProc()
        server = inject._AppServer(proc, "thr")
        import queue as qmod
        reply_q = qmod.Queue()
        server.replies[5] = reply_q
        # garbage, non-dict, response, notification, approval without emit
        proc.stdout = io.BytesIO(
            b"garbage\n[1,2]\n"
            + json.dumps({"id": 5, "result": {"ok": 1}}).encode() + b"\n"
            + json.dumps({"method": "turn/started",
                          "params": {"turn": {"id": "t-7"}}}).encode() + b"\n"
            + json.dumps({"method": "turn/completed"}).encode() + b"\n"
            + json.dumps({"id": 42, "method": "x/requestApproval",
                          "params": {"reason": "run?"}}).encode() + b"\n")
        inject._appserver_read_loop(server, "k", None, "m1")
        self.assertEqual(reply_q.get_nowait()["id"], 5)
        self.assertEqual(server.turn_id, "")   # completed cleared it
        self.assertIn("42", server.approvals)  # tracked even without emit
        self.assertTrue(server.dead)           # EOF → marked dead

    def test_read_loop_survives_a_malformed_message(self):
        # List params on an approval and an unhashable id used to raise
        # out of the loop, ending the reader while codex kept running.
        proc = _FakeAppProc()
        proc.stdout.close()  # the fake's pipe; this test reads BytesIO
        self.addCleanup(proc.kill)
        server = inject._AppServer(proc, "thr")
        import queue as qmod
        reply_q = qmod.Queue()
        server.replies[9] = reply_q
        proc.stdout = io.BytesIO(
            json.dumps({"id": 1, "method": "x/requestApproval",
                        "params": [1]}).encode() + b"\n"
            + json.dumps({"id": [1], "result": {}}).encode() + b"\n"
            + json.dumps({"id": 9, "result": {"ok": 1}}).encode() + b"\n")
        inject._appserver_read_loop(server, "k", None, "m1")
        self.assertIn("1", server.approvals)
        self.assertEqual(reply_q.get_nowait()["id"], 9)

    def test_server_for_replaces_a_server_whose_reader_died(self):
        stale = _FakeAppProc()
        self.addCleanup(stale.kill)
        old = inject._AppServer(stale, "old-thread")
        old.dead = True
        inject.CodexAppServerInjector._servers["codex:k"] = old
        proc = _FakeAppProc({
            "initialize": {"result": {}},
            "thread/resume": {"result": {"thread": {"id": "t-new"}}},
        })
        # Ends the fresh server's reader thread with its pipe.
        self.addCleanup(proc.kill)
        with self._spawn_patch(proc):
            server = self._injector()._server_for(self.session)
        self.assertIs(server.proc, proc)
        self.assertTrue(stale.killed)

    def test_read_loop_stdout_error_marks_dead(self):
        class BadOut:
            def __iter__(self):
                raise OSError("reset")

        proc = _FakeAppProc()
        proc.stdout = BadOut()
        server = inject._AppServer(proc, "thr")
        inject._appserver_read_loop(server, "k", None, "m1")
        self.assertTrue(server.dead)

    def test_server_for_evicts_dead_server(self):
        dead = _FakeAppProc()
        dead.killed = True
        inject.CodexAppServerInjector._servers["codex:k"] = (
            inject._AppServer(dead, "old-thread"))
        proc = _FakeAppProc({
            "initialize": {"result": {}},
            "thread/resume": {"result": {"thread": {"id": "t-new"}}},
        })
        with self._spawn_patch(proc):
            server = self._injector()._server_for(self.session)
        self.assertIsNotNone(server)
        self.assertIs(server.proc, proc)  # dead one evicted, fresh spawn
        self.assertIs(
            inject.CodexAppServerInjector._servers["codex:k"], server)

    def test_handshake_cleanup_kill_failure_swallowed(self):
        proc = _FakeAppProc({"initialize": {"error": {"message": "no"}}})
        proc.kill = lambda: (_ for _ in ()).throw(OSError("x"))
        with self._spawn_patch(proc):
            self.assertIsNone(self._injector()._server_for(self.session))

    def test_server_for_no_thread_id_and_spawn_error(self):
        inj = self._injector()
        bad = _session(tool="codex", sid="rollout-no-uuid")
        self.assertIsNone(inj._server_for(bad))

        def spawn_fail(*a, **k):
            raise OSError("no codex")

        with patch.object(
            inject.CodexAppServerInjector, "_spawn",
            staticmethod(spawn_fail)):
            self.assertIsNone(inj._server_for(self.session))

    def test_handshake_error_kills_server(self):
        proc = _FakeAppProc({"initialize": {"error": {"message": "no"}}})
        with self._spawn_patch(proc):
            inj = self._injector()
            self.assertIsNone(inj._server_for(self.session))
        self.assertTrue(proc.killed)
        self.assertEqual(inject.CodexAppServerInjector._servers, {})

    def test_resume_error_kills_server(self):
        proc = _FakeAppProc({
            "initialize": {"result": {}},
            "thread/resume": {"error": {"message": "gone"}},
        })
        with self._spawn_patch(proc):
            self.assertIsNone(self._injector()._server_for(self.session))
        self.assertTrue(proc.killed)

    def test_call_write_failure_and_timeout(self):
        proc = _FakeAppProc({})  # no scripted replies
        proc.stdin = MagicMock()
        proc.stdin.write.side_effect = BrokenPipeError()
        server = inject._AppServer(proc, "thr")
        self.assertIsNone(self._injector()._call(server, "m", {}))
        # reply never arrives → timeout path
        proc2 = _FakeAppProc({})
        server2 = inject._AppServer(proc2, "thr")
        self.assertIsNone(
            self._injector()._call(server2, "m", {}, timeout=0.05))
        self.assertEqual(server2.replies, {})

    def test_concurrent_caller_waits_for_ready(self):
        # A _server_for finding a live-but-mid-handshake server waits on
        # `ready` and returns None when the handshake failed.
        proc = _FakeAppProc()
        server = inject._AppServer(proc, "thr-x")
        server.ready.clear()          # another caller "spawned" it
        server.handshake_ok = False
        inject.CodexAppServerInjector._servers["codex:k"] = server
        # the spawner finishes its (failed) handshake slightly later
        threading.Timer(0.05, server.ready.set).start()
        self.assertIsNone(self._injector()._server_for(self.session))

    def test_send_empty_detached_and_bad_responses(self):
        inj = self._injector()
        res = inj.send(self.session, {"text": " ", "mode": "queue"}, "idle")
        self.assertFalse(res.ok)
        self.assertIn("empty", res.note)
        # detached
        res = inj.send(
            self.session, {"text": "x", "mode": "steer"}, "idle")
        self.assertFalse(res.ok)
        self.assertIn("detached", res.note)

    def test_send_timeout_and_error_reply(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        # proc with no turn/start reply → _call times out
        proc = _FakeAppProc({
            "initialize": {"result": {}},
            "thread/resume": {"result": {"thread": {"id": "t"}}},
            # no "turn/start" reply → the send call blocks then times out
        })
        with self._spawn_patch(proc):
            inj = self._injector()
            with patch.object(inject, "_APPSERVER_CALL_S", 0.1):
                res = inj.send(
                    self.session, {"text": "x", "mode": "steer"}, "idle")
        self.assertFalse(res.ok)
        self.assertIn("timed out", res.note)

    def test_send_error_reply(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        proc = _FakeAppProc({
            "initialize": {"result": {}},
            "thread/resume": {"result": {"thread": {"id": "t"}}},
            "turn/start": {"error": {"message": "sandbox denied"}},
        })
        with self._spawn_patch(proc):
            res = self._injector().send(
                self.session, {"text": "x", "mode": "steer"}, "idle")
        self.assertFalse(res.ok)
        self.assertIn("sandbox denied", res.note)

    def test_send_attachments_prefix(self):
        supervise.mark(self.home, "codex:k", _meta("appserver", "codex"))
        proc = _FakeAppProc({
            "initialize": {"result": {}},
            "thread/resume": {"result": {"thread": {"id": "t"}}},
            "turn/start": {"result": {"turn": {"id": "u1"}}},
        })
        with self._spawn_patch(proc):
            res = self._injector().send(
                self.session,
                {"text": "look", "mode": "steer",
                 "attachments": ["/tmp/a.png"]},
                "idle")
        self.assertTrue(res.ok)
        start_call = next(
            w for w in proc.writes if w.get("method") == "turn/start")
        self.assertEqual(start_call["params"]["input"][0]["text"],
                         "[Attached file: /tmp/a.png] look")

    def test_approval_edges(self):
        inj = self._injector()
        res = inj.approval(self.session, "1", "bogus")
        self.assertFalse(res.ok)
        self.assertIn("unknown decision", res.note)
        res = inj.approval(self.session, "1", "allow")
        self.assertFalse(res.ok)
        self.assertIn("not running", res.note)
        # dead server → same refusal
        proc = _FakeAppProc()
        proc.killed = True
        server = inject._AppServer(proc, "t")
        inject.CodexAppServerInjector._servers["codex:k"] = server
        res = inj.approval(self.session, "1", "allow")
        self.assertFalse(res.ok)

    def test_approval_write_failure_requeues(self):
        proc = _FakeAppProc()
        server = inject._AppServer(proc, "t")
        server.approvals["req-1"] = 77
        proc.stdin = MagicMock()
        proc.stdin.write.side_effect = BrokenPipeError()
        inject.CodexAppServerInjector._servers["codex:k"] = server
        res = self._injector().approval(self.session, "req-1", "allow")
        self.assertFalse(res.ok)
        self.assertIn("unreachable", res.note)
        # the request id went back so a retry can still answer
        self.assertEqual(server.approvals["req-1"], 77)

    def test_control_edges(self):
        inj = self._injector()
        res = inj.control(self.session, "interrupt", "")
        self.assertFalse(res.ok)
        self.assertIn("not running", res.note)
        # interrupt reply with error → fails
        proc = _FakeAppProc()
        server = inject._AppServer(proc, "t")
        inject.CodexAppServerInjector._servers["codex:k"] = server
        with patch.object(
            inj, "_call", return_value={"error": {"message": "x"}}
        ):
            res = inj.control(self.session, "interrupt", "")
        self.assertFalse(res.ok)
        self.assertIn("interrupt failed", res.note)
        # unsupported op
        res = inj.control(self.session, "compact", "")
        self.assertFalse(res.ok)

    def test_detach_kill_failure_swallowed(self):
        proc = _FakeAppProc()
        proc.kill = lambda: (_ for _ in ()).throw(OSError("x"))
        server = inject._AppServer(proc, "t")
        inject.CodexAppServerInjector._servers["codex:k"] = server
        res = self._injector().control(self.session, "detach", "")
        self.assertTrue(res.ok)
        self.assertNotIn(
            "codex:k", inject.CodexAppServerInjector._servers)


class TestTmuxApprovalGates(unittest.TestCase):
    def test_stale_request_id_refused(self):
        home = temp_dir(self)
        supervise.mark(
            home, "k-stale", {"mode": "tmux", "tool_name": "claude"})
        supervise.set_pending_approval("k-stale", "req-new", "fp")
        inj = inject.TmuxInjector(home, "k-stale")
        res = inj.approval(_session(tool="claude"), "req-old", "allow")
        self.assertFalse(res.ok)
        self.assertIn("stale", res.note)


class TestInjectResultDegraded(unittest.TestCase):
    def test_headless_steer_marks_degraded(self):
        home = temp_dir(self)
        supervise.mark(home, "k", _meta("headless", "claude"))
        inj = inject.ClaudeHeadlessInjector(home, "k")
        session = _session(tool="claude", sid="s1")
        import io
        proc = MagicMock()
        proc.stdout = io.BytesIO(
            b'{"type":"result","result":"done"}\n')
        proc.wait.return_value = 0
        with patch.object(inject.subprocess, "Popen", return_value=proc):
            res = inj.send(
                session, {"text": "x", "mode": "steer"}, "idle")
        self.assertTrue(res.ok)
        self.assertTrue(res.degraded)
        self.assertIn("steer queued", res.note)

    def test_tmux_steer_not_degraded(self):
        home = temp_dir(self)
        supervise.mark(home, "k", _meta("tmux", "claude"))
        inj = inject.TmuxInjector(home, "k")
        session = _session(tool="claude", sid="s1")
        with patch.object(supervise, "send_text", return_value=True):
            res = inj.send(
                session, {"text": "x", "mode": "steer"}, "active")
        self.assertTrue(res.ok)
        self.assertFalse(res.degraded)



# =====================================================================
# G16: the Codex session adapter (remote/adapters/codex_appserver.py)
# and its wire module (codex_wire.py).
#
# Three layers, as for Claude (test_remote_claude_adapter.py): every
# recording under tests/fixtures/protocols/codex_appserver/ replays to
# its golden events; the wire module line by line; the adapter over
# tests/fakes/fake_codex.py, including the shared AdapterContract. No
# test runs the real ``codex``: CI machines have none.

CODEX_FAKE = Path(__file__).resolve().parent / "fakes" / "fake_codex.py"
PROTOCOLS = Path(__file__).resolve().parent / "fixtures" / "protocols"
CODEX_FIXTURES = PROTOCOLS / "codex_appserver"
TIMEOUT = 10.0
THREAD = "11111111-2222-7333-8444-555555555555"
FORKED = "11111111-2222-7333-8444-666666666666"
STEM = f"rollout-2026-01-02T03-04-05-{THREAD}"


def _load_fixture(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def _of(evs, etype):
    return [e for e in evs if e["type"] == etype]


def _streams_close(test, evs) -> None:
    """Every streamed message ends in a completion that starts with
    what its deltas added up to, at the offsets they claimed."""
    text: dict = {}
    for event in evs:
        data = event["data"]
        if event["type"] == "message.delta":
            test.assertEqual(data["offset"], len(text.get(data["id"], "")))
            text[data["id"]] = text.get(data["id"], "") + data["text"]
        elif event["type"] in ("message.completed", "thinking.completed") \
                and data["id"] in text:
            test.assertEqual(text.pop(data["id"]), data["text"])
    test.assertEqual(text, {}, "a stream was left open")


def _contract_order(test, evs) -> None:
    """The ordering rules of base.py over one replayed session."""
    open_turn = None
    open_requests: set = set()
    open_tools: set = set()
    for event in evs:
        etype, data = event["type"], event["data"]
        if etype == "turn.started":
            test.assertIsNone(open_turn)
            open_turn = event["turn_id"]
        elif etype == "turn.ended":
            test.assertEqual(event["turn_id"], open_turn)
            test.assertEqual(open_requests, set(),
                             "a request outlived its turn")
            open_turn = None
        elif etype in ("approval.requested", "input.requested"):
            open_requests.add(data["request_id"])
        elif etype == "approval.resolved":
            test.assertIn(data["request_id"], open_requests)
            open_requests.discard(data["request_id"])
        elif etype == "tool.started":
            open_tools.add(data["call_id"])
        elif etype == "tool.completed":
            test.assertIn(data["call_id"], open_tools)
            open_tools.discard(data["call_id"])
        if event.get("turn_id") and etype != "turn.ended":
            test.assertEqual(event["turn_id"], open_turn, event)
    test.assertIsNone(open_turn)


class TestCodexFixtures(unittest.TestCase):
    def fixtures(self):
        found = sorted(CODEX_FIXTURES.rglob("*.jsonl"))
        self.assertTrue(found, "no recorded fixtures")
        return found

    def scenario(self, name):
        lines = _load_fixture(
            CODEX_FIXTURES / codex_wire.RECORDED_VERSION / f"{name}.jsonl")
        return lines[0], lines[1:], codex_wire.replay(lines)

    def test_every_fixture_replays_to_its_golden_events(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                golden = path.with_name(path.name[:-6] + ".events.json")
                self.assertTrue(golden.exists(), f"{golden.name} missing")
                got = codex_wire.replay(_load_fixture(path))
                self.assertEqual(got, json.loads(golden.read_text()))
                for event in got:
                    self.assertEqual(events.validate(event), [], event)

    def test_headers_say_how_each_was_recorded(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                header = _load_fixture(path)[0]
                self.assertEqual(header["fixture"], 1)
                self.assertEqual(header["adapter"], "codex_appserver")
                self.assertEqual(header["version"], path.parent.name)
                self.assertIs(header["synthetic"], False)
                self.assertIsNone(header["error"])
                self.assertEqual(header["env_set"], ["CODEX_HOME", "HOME"])

    def test_golden_events_keep_the_contract_order(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                got = codex_wire.replay(_load_fixture(path))
                self.assertEqual([e["type"] for e in got[:2]],
                                 ["session.caps", "session.state"])
                self.assertEqual(got[-1]["data"]["state"], "closed")
                _streams_close(self, got)
                # One process at a time: each has its own start pair.
                starts = [i for i, e in enumerate(got)
                          if e["type"] == "session.caps"] + [len(got)]
                for a, b in zip(starts, starts[1:]):
                    _contract_order(self, got[a:b])

    def test_turn_ids_never_repeat_across_processes(self):
        _header, _lines, got = self.scenario("resume")
        ids = [e["turn_id"] for e in _of(got, "turn.started")]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        users = [e["data"]["id"] for e in _of(got, "message.completed")
                 if e["data"]["kind"] == "user"]
        self.assertEqual(len(set(users)), 3)

    def test_hello(self):
        _header, _lines, got = self.scenario("hello")
        self.assertEqual(got[0]["data"]["caps"],
                         codex_wire.PROVEN.as_dict())
        replies = [e["data"]["text"] for e in _of(got, "message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies, ["hello there"])
        self.assertGreater(len(_of(got, "message.delta")), 1)
        usage = _of(got, "usage.updated")[0]["data"]
        self.assertGreater(usage["context_window"], 0)
        self.assertGreater(usage["input_tokens"], 0)
        self.assertEqual(_of(got, "turn.ended")[0]["data"]["status"],
                         "completed")

    def test_approval_allow_deny_and_a_file_change(self):
        header, lines, got = self.scenario("approval")
        asked = _of(got, "approval.requested")
        self.assertEqual([e["data"]["kind"] for e in asked],
                         ["command", "command", "tool"])
        self.assertIn("touch allowed.txt", asked[0]["data"]["prompt"])
        self.assertIn("/work/patched.txt", asked[2]["data"]["prompt"])
        for request in asked:
            # The server lists no acceptForSession here: no "always".
            self.assertEqual([o["id"] for o in request["data"]["options"]],
                             ["allow", "deny"])
        self.assertEqual([e["data"].get("decision") for e in
                          _of(got, "approval.resolved")],
                         ["allow", "deny", "allow"])
        done = _of(got, "tool.completed")
        self.assertEqual([e["data"]["status"] for e in done],
                         ["ok", "cancelled", "ok"])
        self.assertEqual(done[2]["data"]["file_edits"], [
            {"path": "/work/patched.txt", "kind": "add",
             "diff": "@@ -0,0 +1,1 @@\n+yes\n"}])
        self.assertEqual(header["notes"]["files"], [
            "allowed.txt", "hello.py", "notes.txt", "patched.txt"])
        # What we answered on the wire.
        sent = [ln["msg"]["result"]["decision"] for ln in lines
                if ln.get("dir") == "out" and "result" in ln.get("msg", {})]
        self.assertEqual(sent, ["accept", "decline", "accept"])

    def test_accept_for_session_does_not_stop_the_next_file_change(self):
        # Why a file change offers no "always".
        header, _lines, got = self.scenario("always")
        self.assertEqual(header["notes"]["approvals_first_turn"], 1)
        self.assertEqual(header["notes"]["approvals_second_turn"], 1)
        self.assertEqual(len(_of(got, "approval.requested")), 2)
        for request in _of(got, "approval.requested"):
            self.assertNotIn("always",
                             [o["id"] for o in request["data"]["options"]])

    def test_interrupt(self):
        _header, lines, got = self.scenario("interrupt")
        ended = _of(got, "turn.ended")
        self.assertEqual([e["data"]["status"] for e in ended],
                         ["interrupted"])
        partial = [e for e in _of(got, "message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(len(partial), 1, "the cut reply is completed")
        self.assertTrue(partial[0]["data"]["text"].startswith("1."))
        # Idle afterwards: the server refuses, the adapter treats it as
        # a no-op.
        errors = [ln["msg"]["error"]["message"] for ln in lines
                  if "error" in ln.get("msg", {})]
        self.assertEqual(errors, ["no active turn to interrupt"])

    def test_steer_joins_the_turn_and_cuts_the_reply(self):
        _header, lines, got = self.scenario("steer")
        self.assertEqual(len(_of(got, "turn.started")), 1)
        self.assertEqual([e["data"]["status"] for e in
                          _of(got, "turn.ended")], ["completed"])
        done = [(e["data"]["kind"], e["data"]["id"]) for e in
                _of(got, "message.completed")]
        self.assertEqual([k for k, _i in done],
                         ["user", "assistant", "user", "assistant"],
                         "the cut reply completes before the steer")
        self.assertEqual(done[2][1], done[0][1] + "2")
        texts = [e["data"]["text"] for e in _of(got, "message.completed")]
        self.assertTrue(texts[1].startswith("1."))
        self.assertEqual(texts[3], "steered")
        # The server sent no completion for the cut reply.
        completed = [ln["msg"]["params"]["item"]["id"] for ln in lines
                     if ln.get("msg", {}).get("method") == "item/completed"
                     and ln["msg"]["params"]["item"]["type"]
                     == "agentMessage"]
        self.assertNotIn(done[1][1], completed)
        errors = [ln["msg"]["error"]["message"] for ln in lines
                  if "error" in ln.get("msg", {})]
        self.assertEqual(errors, ["no active turn to steer"])

    def test_steer_does_not_stop_a_running_command(self):
        header, _lines, got = self.scenario("steer_tool")
        notes = header["notes"]
        self.assertIs(notes["late_file_at_turn_end"], False)
        self.assertIs(notes["late_file_30s_after_command_began"], True)
        types = [e["type"] for e in got]
        ended = types.index("turn.ended")
        done = types.index("tool.completed")
        self.assertGreater(done, ended, "the command outlives the turn")
        self.assertNotIn("turn_id", got[done])
        self.assertEqual(got[done]["data"]["status"], "ok")
        self.assertEqual(got[done]["data"]["call_id"],
                         _of(got, "tool.started")[0]["data"]["call_id"])

    def test_steer_waits_for_an_open_approval(self):
        header, lines, got = self.scenario("steer_approval")
        self.assertIs(header["notes"]["turn_ended_without_an_answer"], False)
        self.assertIs(header["notes"]["asked_file_exists"], False)
        types = [(e["type"], e["data"].get("kind") or e["data"].get(
            "source")) for e in got]
        asked = types.index(("approval.requested", "command"))
        resolved = types.index(("approval.resolved", "user"))
        steered = [i for i, t in enumerate(types)
                   if t == ("message.completed", "user")][1]
        self.assertLess(asked, resolved)
        self.assertLess(resolved, steered,
                        "the steer lands once the approval is answered")
        # 20 s passed between the steer and our answer with no line
        # from the server but the steer's own answer.
        steer_at = next(ln["t"] for ln in lines if ln.get("msg", {}).get(
            "method") == "turn/steer")
        answer_at = next(ln["t"] for ln in lines if ln.get("dir") == "out"
                         and "result" in ln.get("msg", {}))
        between = [ln for ln in lines if ln.get("dir") == "in"
                   and steer_at < ln["t"] < answer_at
                   and "method" in ln.get("msg", {})]
        self.assertGreater(answer_at - steer_at, 15)
        self.assertEqual(between, [])

    def test_resume_keeps_the_id_and_fork_gets_a_new_one(self):
        _header, lines, got = self.scenario("resume")
        sids = [e["data"]["harness_session_id"]
                for e in _of(got, "session.state")
                if e["data"].get("harness_session_id")]
        self.assertEqual(len(sids), 3)
        self.assertEqual(sids[0], sids[1], "a resume keeps the id")
        self.assertNotEqual(sids[0], sids[2], "a fork gets the new id")
        for sid in sids:
            self.assertTrue(sid.startswith("rollout-"))
        methods = [ln["msg"]["method"] for ln in lines
                   if ln.get("dir") == "out" and ln["msg"].get(
                       "method", "").startswith("thread/")]
        self.assertEqual(methods,
                         ["thread/start", "thread/resume", "thread/fork"])
        replies = [e["data"]["text"] for e in _of(got, "message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual([r.lower() for r in replies],
                         ["ok", "papaya", "papaya"])

    def test_rollback_drops_the_turn_from_the_conversation(self):
        _header, lines, got = self.scenario("rollback")
        reverts = [ln["msg"] for ln in lines
                   if ln.get("msg", {}).get("method") == "thread/revert"]
        self.assertEqual(len(reverts), 1)
        second = [ln["msg"]["result"]["turn"]["id"] for ln in lines
                  if ln.get("dir") == "in" and "turn" in (
                      ln.get("msg", {}).get("result") or {})][1]
        self.assertEqual(reverts[0]["params"]["beforeTurnId"], second)
        replies = [e["data"]["text"] for e in _of(got, "message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies[-1].lower(), "papaya",
                         "walrus was rolled back")
        # The revert wrote a new rollout file: same thread, new stem.
        sids = [e["data"]["harness_session_id"]
                for e in _of(got, "session.state")
                if e["data"].get("harness_session_id")]
        self.assertEqual(len(sids), 2)
        self.assertNotEqual(sids[0], sids[1])
        self.assertEqual(codex_wire.thread_id_of(sids[0]),
                         codex_wire.thread_id_of(sids[1]))

    def test_turn_start_overrides_hold_for_later_turns(self):
        header, lines, _got = self.scenario("options")
        notes = header["notes"]
        self.assertEqual((notes["approvals_under_untrusted"],
                          notes["approvals_under_never"],
                          notes["approvals_with_no_override"]), (1, 0, 0))
        starts = [ln["msg"]["params"] for ln in lines
                  if ln.get("msg", {}).get("method") == "turn/start"]
        self.assertEqual((starts[1]["model"], starts[1]["effort"],
                          starts[1]["approvalPolicy"]),
                         ("gpt-6-luna", "medium", "never"))
        for name in ("model", "effort", "approvalPolicy"):
            self.assertNotIn(name, starts[2])

    def test_model_and_effort_overrides_are_read(self):
        header, _lines, got = self.scenario("error")
        notes = header["notes"]
        self.assertEqual(notes["next_turn_without_override"], "failed",
                         "the model override is kept")
        self.assertEqual(notes["bad_effort_turn"], "failed")
        self.assertEqual(notes["turn_after_a_good_override"], "completed")
        self.assertEqual([e["data"]["status"] for e in
                          _of(got, "turn.ended")],
                         ["failed", "failed", "failed", "completed"])
        errors = _of(got, "error")
        self.assertEqual([e["data"]["code"] for e in errors],
                         ["modelNotFound", "modelNotFound", "other"])
        self.assertIn("no-such-effort", errors[2]["data"]["message"])
        self.assertIn("does not exist",
                      _of(got, "turn.ended")[0]["data"]["reason"])

    def test_attachments(self):
        _header, lines, got = self.scenario("attach")
        starts = [ln["msg"]["params"]["input"] for ln in lines
                  if ln.get("msg", {}).get("method") == "turn/start"]
        self.assertEqual([i["type"] for i in starts[0]],
                         ["text", "localImage"])
        self.assertTrue(starts[1][0]["text"].startswith(
            "[Attached file: /work/notes.txt] "))
        replies = [e["data"]["text"] for e in _of(got, "message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies[0].lower().strip("."), "red")
        self.assertEqual(replies[1], "first line of the notes")

    def test_no_personal_data(self):
        for path in sorted(CODEX_FIXTURES.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertNotIn("/Users/", text)
                self.assertNotIn("/var/folders/", text)
                self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@(?!example\.com)"
                                          r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
                self.assertNotRegex(text, r"\bsk-[A-Za-z0-9_-]{8,}")
                for key in ('"serverName"', '"installationId"',
                            '"planType"', '"instructionSources"',
                            '"gitInfo"', ".local"):
                    self.assertNotIn(key, text)


class TestCodexWire(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(codex_wire.thread_id_of(STEM), THREAD)
        self.assertEqual(codex_wire.thread_id_of(THREAD.upper()), THREAD)
        self.assertEqual(
            codex_wire.thread_id_of(f"{STEM}_{FORKED}"), THREAD,
            "a resumed rollout's own uuid only names the file")
        for bad in ("", "s-1", None, 7):
            self.assertEqual(codex_wire.thread_id_of(bad), "")
        thread = {"id": THREAD, "path": f"/h/sessions/2026/{STEM}.jsonl"}
        self.assertEqual(codex_wire.session_ref(thread), STEM)
        self.assertEqual(codex_wire.session_ref({"id": THREAD}), THREAD)
        self.assertEqual(codex_wire.session_ref(
            {"id": THREAD, "path": "/h/x.jsonl"}), THREAD)
        self.assertEqual(codex_wire.session_ref(None), "")
        self.assertEqual(codex_wire.agent_version(
            "swe/0.170.0 (Mac OS 27.3.0; arm64) unknown"), "0.170.0")
        self.assertEqual(codex_wire.agent_version("codex"), "")
        self.assertEqual(codex_wire.agent_version(None), "")

    def test_encode_and_decode(self):
        line = codex_wire.encode({"a": "é"})
        self.assertEqual(line, '{"a":"é"}\n'.encode())
        self.assertEqual(codex_wire.decode(line), {"a": "é"})
        for junk in (b"", b"  \n", b"not json", b"[1]", "7"):
            self.assertIsNone(codex_wire.decode(junk))

    def test_requests(self):
        self.assertEqual(codex_wire.request(1, "x"), {
            "jsonrpc": "2.0", "id": 1, "method": "x", "params": {}})
        hello = codex_wire.initialize(1, "9.9")
        self.assertEqual(hello["params"]["clientInfo"],
                         {"name": "swe", "title": "SWE", "version": "9.9"})
        self.assertEqual(codex_wire.initialized(),
                         {"jsonrpc": "2.0", "method": "initialized"})
        self.assertEqual(
            codex_wire.thread_start(2, cwd="/w", model="m",
                                    approval_policy="never")["params"],
            {"cwd": "/w", "model": "m", "approvalPolicy": "never"})
        # A permission mode that is not a Codex policy is left out.
        self.assertEqual(
            codex_wire.thread_start(2, cwd="/w",
                                    approval_policy="acceptEdits")["params"],
            {"cwd": "/w"})
        self.assertEqual(
            codex_wire.thread_resume(3, THREAD, cwd="/w")["params"],
            {"threadId": THREAD, "excludeTurns": True, "cwd": "/w"})
        fork = codex_wire.thread_fork(4, THREAD, model="m")
        self.assertEqual((fork["method"], fork["params"]["model"]),
                         ("thread/fork", "m"))
        self.assertEqual(
            codex_wire.thread_revert(5, THREAD, "turn-2")["params"],
            {"threadId": THREAD, "beforeTurnId": "turn-2"})
        self.assertEqual(codex_wire.turn_interrupt(6, THREAD, "t")["params"],
                         {"threadId": THREAD, "turnId": "t"})
        self.assertEqual(codex_wire.approval_reply(0, "accept"), {
            "jsonrpc": "2.0", "id": 0, "result": {"decision": "accept"}})
        self.assertEqual(codex_wire.error(9, "no")["error"],
                         {"code": -32601, "message": "no"})

    def test_turn_start_and_steer(self):
        start = codex_wire.turn_start(
            7, THREAD, "look", attachments=["/a/shot.PNG", "/a/notes.txt"],
            model="m", effort="low", approval_policy="on-request")
        self.assertEqual(start["params"], {
            "threadId": THREAD, "clientUserMessageId": "swe-7",
            "input": [{"type": "text", "text_elements": [],
                       "text": "[Attached file: /a/notes.txt] look"},
                      {"type": "localImage", "path": "/a/shot.PNG"}],
            "model": "m", "effort": "low", "approvalPolicy": "on-request"})
        plain = codex_wire.turn_start(8, THREAD, "hi")["params"]
        self.assertEqual(sorted(plain),
                         ["clientUserMessageId", "input", "threadId"])
        steer = codex_wire.turn_steer(9, THREAD, "turn-1", "more")
        self.assertEqual(steer["method"], "turn/steer")
        self.assertEqual(steer["params"]["expectedTurnId"], "turn-1")
        self.assertEqual(steer["params"]["clientUserMessageId"], "swe-9")

    def test_file_edits(self):
        item = {"changes": [
            {"path": "/w/a", "kind": {"type": "add"}, "diff": "x\ny\n"},
            {"path": "/w/b", "kind": {"type": "delete"}, "diff": "gone\n"},
            {"path": "/w/c", "kind": {"type": "update", "move_path": "/w/d"},
             "diff": "@@ -1 +1 @@\n-a\n+b\n"},
            {"path": "/w/e", "kind": "update", "diff": ""},
            {"path": "", "kind": {"type": "add"}}, "junk"]}
        self.assertEqual(codex_wire.file_edits(item), [
            {"path": "/w/a", "kind": "add",
             "diff": "@@ -0,0 +1,2 @@\n+x\n+y\n"},
            {"path": "/w/b", "kind": "delete",
             "diff": "@@ -1,1 +0,0 @@\n-gone\n"},
            {"path": "/w/d", "kind": "modify", "old_path": "/w/c",
             "diff": "@@ -1 +1 @@\n-a\n+b\n"},
            {"path": "/w/e", "kind": "modify"}])
        self.assertIsNone(codex_wire.file_edits({"changes": []}))
        self.assertIsNone(codex_wire.file_edits("x"))
        secret = codex_wire.file_edits({"changes": [{
            "path": "/w/.env", "kind": {"type": "add"},
            "diff": "API_KEY=sk-ant-api03-" + "a" * 40 + "\n"}]})
        self.assertNotIn("sk-ant-api03", secret[0]["diff"])

    def test_tool_cards(self):
        card = codex_wire.tool_card
        self.assertEqual(card({"type": "commandExecution", "command":
                               "/bin/zsh -lc 'ls'", "cwd": "/w",
                               "commandActions": [{"command": "ls"}]}),
                         ("shell", {"command": "ls", "cwd": "/w"}))
        self.assertEqual(card({"type": "commandExecution",
                               "command": "ls -la"})[1]["command"], "ls -la")
        self.assertEqual(card({"type": "mcpToolCall", "server": "fs",
                               "tool": "read", "arguments": {"p": 1}}),
                         ("fs.read", {"p": 1}))
        self.assertEqual(card({"type": "dynamicToolCall", "tool": "calc",
                               "arguments": [1]}), ("calc", [1]))
        self.assertEqual(card({"type": "webSearch", "query": "q"}),
                         ("web_search", {"query": "q"}))
        self.assertEqual(card({"type": "imageView", "path": "/a.png"}),
                         ("view_image", {"path": "/a.png"}))
        self.assertEqual(card({"type": "collabAgentToolCall",
                               "tool": "spawnAgent", "prompt": "go"}),
                         ("agent.spawnAgent", {"prompt": "go"}))
        self.assertIsNone(card({"type": "sleep"}))

    def test_the_pre_keeper_channel_helpers(self):
        w = codex_wire
        self.assertTrue(w.is_approval_request(
            "item/commandExecution/requestApproval"))
        self.assertTrue(w.is_approval_request("execCommandApproval"))
        self.assertFalse(w.is_approval_request("turn/started"))
        self.assertEqual(w.approval_prompt("m", {"reason": "r",
                                                 "command": "c"}), "r")
        self.assertEqual(w.approval_prompt("m", {"itemId": "i"}), "i")
        self.assertEqual(w.approval_prompt("m", {}), "m")
        self.assertEqual(w.live_text("item/agentMessage/delta",
                                     {"itemId": "i", "delta": "d"}),
                         ("delta", "i", "d"))
        self.assertIsNone(w.live_text("item/agentMessage/delta",
                                      {"itemId": "i", "delta": ""}))
        self.assertEqual(w.live_text("item/completed",
                                     {"item": {"id": "i"}}),
                         ("done", "i", ""))
        self.assertIsNone(w.live_text("item/completed", {"item": 3}))
        self.assertEqual(w.live_text("turn/failed", {}),
                         ("turn_done", "", ""))
        self.assertIsNone(w.live_text("thread/started", {}))
        self.assertEqual(w.started_turn_id("turn/started",
                                           {"turn": {"id": "t9"}}), "t9")
        self.assertIsNone(w.started_turn_id("turn/started", {"turn": {}}))
        self.assertEqual(w.started_turn_id("codex/turnCompleted", {}), "")
        self.assertIsNone(w.started_turn_id("item/started", {}))
        self.assertEqual(w.plain_turn_params("t", "hi", model="m"), {
            "threadId": "t", "input": [{"type": "text", "text": "hi"}],
            "model": "m"})
        self.assertEqual(w.interrupt_params("t"), {"threadId": "t"})
        self.assertEqual(w.interrupt_params("t", "u"),
                         {"threadId": "t", "turnId": "u"})
        self.assertIs(inject._CODEX_DECISIONS, w.DECISIONS)


def _note(method, **params):
    params.setdefault("threadId", THREAD)
    return {"method": method, "params": params}


class TestCodexThreadState(unittest.TestCase):
    """``ThreadState`` line by line, with a fake clock."""

    def state(self, caps=codex_wire.PROVEN):
        clock = fake.FakeClock(100.0)
        state = codex_wire.ThreadState("codex:t", clock=clock,
                                       run_id="aaaa0001")
        state.thread_id = THREAD
        first = state.start_events(caps, STEM)
        self.assertEqual([e["type"] for e in first],
                         ["session.caps", "session.state"])
        return state, clock

    def feed(self, state, *msgs):
        out, replies = [], []
        for msg in msgs:
            fed = state.feed(msg)
            out.extend(fed.events)
            replies.extend(fed.replies)
        for event in out:
            self.assertEqual(events.validate(event), [], event)
        return out, replies

    def in_turn(self, text="go", **kw):
        state, clock = self.state()
        state.expect_turn(1, text, **kw)
        out, _ = self.feed(state, {"id": 1, "result": {
            "turn": {"id": "native-1"}}})
        return state, clock, out

    def test_a_turn_opens_when_the_server_accepts_it(self):
        state, _clock = self.state()
        self.assertFalse(state.busy)
        state.expect_turn(1, "go", attachments=["/a.txt"], command_id="c1",
                          device="phone")
        self.assertTrue(state.busy)
        self.assertIsNone(state.turn, "nothing is emitted before the answer")
        out, _ = self.feed(state, {"id": 1, "result": {
            "turn": {"id": "native-1"}}})
        self.assertEqual([e["type"] for e in out], [
            "turn.started", "session.state", "message.completed"])
        self.assertEqual(out[0]["turn_id"], "t-aaaa0001-1")
        self.assertEqual(out[0]["command_id"], "c1")
        self.assertEqual(out[0]["data"], {"by": "phone", "text": "go"})
        self.assertEqual(out[2]["data"], {
            "id": "t-aaaa0001-1:user", "kind": "user", "text": "go",
            "by": "phone", "attachments": ["/a.txt"]})
        self.assertEqual(state.accepted, {1: "t-aaaa0001-1"})
        self.assertEqual(state.native_turns, {"t-aaaa0001-1": "native-1"})

    def test_a_refused_turn_emits_nothing(self):
        state, _clock = self.state()
        state.expect_turn(1, "go")
        fed = state.feed({"id": 1, "error": {"code": -32600,
                                             "message": "busy"}})
        self.assertEqual(fed.events, [])
        self.assertEqual((fed.acks[0].ok, fed.acks[0].error,
                          fed.acks[0].code), (False, "busy", -32600))
        self.assertFalse(state.busy)
        state.expect_turn(2, "again")
        state.forget(2)
        self.assertFalse(state.busy)

    def test_notifications_that_overtake_the_answer_open_the_turn(self):
        state, _clock = self.state()
        state.expect_turn(1, "go", command_id="c1")
        out, _ = self.feed(state, _note("turn/started",
                                        turn={"id": "native-1"}))
        self.assertEqual([e["type"] for e in out], [
            "turn.started", "session.state", "message.completed"])
        self.assertEqual(out[0]["command_id"], "c1")
        self.assertEqual(state.turn.native_id, "native-1")
        # The answer then adds nothing.
        out, _ = self.feed(state, {"id": 1, "result": {
            "turn": {"id": "native-1"}}})
        self.assertEqual(out, [])
        self.assertEqual(state.accepted, {1: "t-aaaa0001-1"})

    def test_a_turn_the_server_starts_itself(self):
        state, _clock = self.state()
        out, _ = self.feed(state, _note("turn/started",
                                        turn={"id": "native-7"}))
        self.assertEqual([e["type"] for e in out],
                         ["turn.started", "session.state"])
        self.assertNotIn("command_id", out[0])
        self.assertTrue(state.busy)

    def test_streaming_text_and_completion(self):
        state, _clock, _ = self.in_turn()
        out, _ = self.feed(
            state,
            _note("item/started", item={"type": "agentMessage", "id": "m1"}),
            _note("item/agentMessage/delta", itemId="m1", delta="hello "),
            _note("item/agentMessage/delta", itemId="m1", delta="there"),
            _note("item/completed", item={"type": "agentMessage", "id": "m1",
                                          "text": "hello there"}))
        self.assertEqual([e["type"] for e in out], [
            "message.delta", "message.delta", "message.completed"])
        self.assertEqual([(e["data"]["text"], e["data"]["offset"],
                           e["data"]["seq"], e["data"]["done"])
                          for e in out[:2]],
                         [("hello ", 0, 1, False), ("there", 6, 2, True)])
        self.assertEqual(out[2]["data"]["text"], "hello there")
        # Late deltas and a second completion of the id are dropped.
        out, _ = self.feed(
            state,
            _note("item/agentMessage/delta", itemId="m1", delta="x"),
            _note("item/completed", item={"type": "agentMessage", "id": "m1",
                                          "text": "hello there"}))
        self.assertEqual(out, [])

    def test_a_secret_split_across_deltas_never_streams(self):
        state, _clock, _ = self.in_turn()
        key = "sk-ant-api03-" + "b" * 40
        out, _ = self.feed(
            state,
            _note("item/agentMessage/delta", itemId="m1", delta="the key "),
            _note("item/agentMessage/delta", itemId="m1", delta=key[:20]),
            _note("item/agentMessage/delta", itemId="m1", delta=key[20:]),
            _note("item/agentMessage/delta", itemId="m1", delta=" ok"),
            _note("item/completed", item={"type": "agentMessage", "id": "m1",
                                          "text": f"the key {key} ok"}))
        text = "".join(e["data"]["text"] for e in out
                       if e["type"] == "message.delta")
        self.assertNotIn("sk-ant", json.dumps(out))
        self.assertEqual(text, out[-1]["data"]["text"])
        _streams_close(self, out)

    def test_reasoning_and_empty_items(self):
        state, _clock, _ = self.in_turn()
        out, _ = self.feed(
            state,
            _note("item/started", item={"type": "reasoning", "id": "r1"}),
            _note("item/reasoning/summaryTextDelta", itemId="r1",
                  delta="part one"),
            _note("item/reasoning/summaryPartAdded", itemId="r1",
                  summaryIndex=1),
            _note("item/reasoning/textDelta", itemId="r1", delta="part two"),
            _note("item/completed", item={
                "type": "reasoning", "id": "r1",
                "summary": ["part one", "part two"], "content": []}),
            # Empty items are how the real CLI reports hidden reasoning
            # and commentary: nothing is shown for them.
            _note("item/started", item={"type": "reasoning", "id": "r2"}),
            _note("item/completed", item={"type": "reasoning", "id": "r2",
                                          "summary": [], "content": []}),
            _note("item/started", item={"type": "agentMessage", "id": "m0"}),
            _note("item/completed", item={"type": "agentMessage",
                                          "id": "m0", "text": ""}))
        self.assertEqual([e["type"] for e in out], [
            "message.delta", "message.delta", "message.delta",
            "thinking.completed"])
        self.assertEqual({e["data"]["kind"] for e in out[:3]}, {"thinking"})
        self.assertEqual(out[3]["data"]["text"], "part one\n\npart two")
        _streams_close(self, out)

    def test_tools(self):
        state, _clock, _ = self.in_turn()
        command = {"type": "commandExecution", "id": "x1", "command":
                   "/bin/zsh -lc 'ls'", "cwd": "/w", "status": "inProgress",
                   "commandActions": [{"command": "ls"}]}
        out, _ = self.feed(
            state,
            _note("item/started", item=command),
            _note("item/completed", item={**command, "status": "failed",
                                          "exitCode": 2,
                                          "aggregatedOutput": "y" * 9000}),
            # Completed without a start (a resumed thread): both rows.
            _note("item/completed", item={
                "type": "mcpToolCall", "id": "x2", "server": "fs",
                "tool": "read", "arguments": {"token": "abc"},
                "status": "completed",
                "result": {"content": [{"text": "one"}, {"text": "two"}]}}),
            _note("item/completed", item={"type": "sleep", "id": "x3"}))
        self.assertEqual([e["type"] for e in out], [
            "tool.started", "tool.completed", "tool.started",
            "tool.completed"])
        self.assertEqual(out[0]["data"], {
            "call_id": "x1", "name": "shell", "summary": "shell · ls",
            "input": {"command": "ls", "cwd": "/w"}})
        self.assertEqual(out[1]["data"]["status"], "error")
        self.assertEqual(len(out[1]["data"]["output"]), 8002)
        self.assertEqual(out[2]["data"]["input"], {"token": "[REDACTED]"})
        self.assertEqual(out[3]["data"]["output"], "one\ntwo")

    def test_command_approval_options_follow_the_server(self):
        state, _clock, _ = self.in_turn()
        out, _ = self.feed(state, {
            "id": 5, "method": "item/commandExecution/requestApproval",
            "params": {"threadId": THREAD, "itemId": "x1",
                       "command": "curl example.com", "reason": "network",
                       "networkApprovalContext": {"host": "example.com"},
                       "availableDecisions": ["accept", "acceptForSession",
                                              "cancel"]}})
        self.assertEqual([e["type"] for e in out],
                         ["approval.requested", "session.state"])
        asked = out[0]["data"]
        self.assertEqual(asked["request_id"], "aaaa0001-5")
        self.assertEqual(asked["kind"], "command")
        self.assertEqual(asked["call_id"], "x1")
        self.assertEqual(asked["prompt"], "curl example.com\n\nNetwork "
                         "access: example.com\n\nnetwork")
        self.assertEqual([o["id"] for o in asked["options"]],
                         ["allow", "always", "deny"])
        self.assertEqual(out[1]["data"]["state"], "waiting")
        reply, resolved = state.answer("aaaa0001-5",
                                       Answer(decision="always",
                                              device="mac"))
        self.assertEqual(reply, {"jsonrpc": "2.0", "id": 5, "result": {
            "decision": "acceptForSession"}})
        self.assertEqual(resolved[0]["data"], {
            "request_id": "aaaa0001-5", "source": "user",
            "decision": "always", "by": "mac"})
        self.assertEqual(resolved[1]["data"]["state"], "running")
        with self.assertRaises(UnknownRequest):
            state.answer("aaaa0001-5", Answer(decision="allow"))

    def test_always_is_allow_where_the_server_does_not_offer_it(self):
        state, _clock, _ = self.in_turn()
        out, _ = self.feed(
            state,
            {"id": 5, "method": "item/commandExecution/requestApproval",
             "params": {"command": "ls",
                        "availableDecisions": ["accept", "cancel"]}},
            {"id": 6, "method": "item/fileChange/requestApproval",
             "params": {"itemId": "p1", "grantRoot": "/w",
                        "reason": "outside the sandbox"}})
        asked = _of(out, "approval.requested")
        for request in asked:
            self.assertEqual([o["id"] for o in request["data"]["options"]],
                             ["allow", "deny"])
        self.assertEqual(asked[1]["data"]["prompt"],
                         "Apply file changes under /w\n\noutside the sandbox")
        self.assertEqual(len(_of(out, "session.state")), 1,
                         "waiting is said once")
        reply, resolved = state.answer("aaaa0001-5", Answer(decision="always"))
        self.assertEqual(reply["result"], {"decision": "accept"})
        self.assertEqual(resolved[0]["data"]["decision"], "allow")
        self.assertEqual(len(resolved), 1, "one request is still open")
        # A bare text answer is a deny; an option id is a decision.
        reply, resolved = state.answer("aaaa0001-6", Answer(text="not that"))
        self.assertEqual(reply["result"], {"decision": "decline"})
        self.assertEqual(resolved[0]["data"]["decision"], "deny")
        self.assertEqual(resolved[0]["data"]["text"], "not that")
        self.assertEqual(resolved[1]["data"]["state"], "running")

    def test_permissions_request(self):
        state, _clock, _ = self.in_turn()
        wanted = {"network": {"enabled": True}}
        for rpc, decision, body in (
                (1, "allow", {"permissions": wanted, "scope": "turn"}),
                (2, "always", {"permissions": wanted, "scope": "session"}),
                (3, "deny", {"permissions": {}, "scope": "turn"})):
            out, _ = self.feed(state, {
                "id": rpc, "method": "item/permissions/requestApproval",
                "params": {"itemId": "x", "permissions": wanted}})
            asked = _of(out, "approval.requested")[0]["data"]
            self.assertIn('"network"', asked["prompt"])
            reply, resolved = state.answer(asked["request_id"],
                                           Answer(option_id=decision))
            self.assertEqual(reply["result"], body)
            self.assertEqual(resolved[0]["data"]["decision"], decision)

    def test_a_question(self):
        state, _clock, _ = self.in_turn()
        out, _ = self.feed(state, {
            "id": 4, "method": "item/tool/requestUserInput",
            "params": {"questions": [{
                "id": "q1", "header": "Pick", "question": "Which?",
                "options": [{"label": "red", "description": "warm"},
                            {"label": "blue", "description": "cold"}]}]}})
        self.assertEqual([e["type"] for e in out],
                         ["input.requested", "session.state"])
        self.assertEqual(out[0]["data"], {
            "request_id": "aaaa0001-4", "prompt": "Which?", "kind": "choice",
            "options": [{"id": "o1", "label": "red"},
                        {"id": "o2", "label": "blue"}]})
        reply, resolved = state.answer("aaaa0001-4", Answer(option_id="o2"))
        self.assertEqual(reply["result"],
                         {"answers": {"q1": {"answers": ["blue"]}}})
        self.assertEqual(resolved[0]["data"]["option_id"], "o2")
        self.assertNotIn("decision", resolved[0]["data"])
        # Free text, and no options: a text question.
        out, _ = self.feed(state, {
            "id": 5, "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "q9", "question": "Name?"}]}})
        self.assertEqual(out[0]["data"]["kind"], "text")
        reply, _ = state.answer("aaaa0001-5", Answer(text="Ada"))
        self.assertEqual(reply["result"],
                         {"answers": {"q9": {"answers": ["Ada"]}}})

    def test_a_choice_that_holds_a_secret_is_never_shown(self):
        # The model writes the choices; one can quote a key from the
        # conversation. Devices and the log get an opaque id and the
        # redacted label; the answer to Codex is the label as written.
        key = "sk-ant-api03-" + "c" * 40
        state, _clock, _ = self.in_turn()
        out, _ = self.feed(state, {
            "id": 4, "method": "item/tool/requestUserInput",
            "params": {"questions": [{
                "id": "q1", "question": f"Use {key}?",
                "options": [{"label": f"use {key}", "description": ""},
                            {"label": "no", "description": ""}]}]}})
        self.assertNotIn("sk-ant", json.dumps(out))
        self.assertEqual([o["id"] for o in out[0]["data"]["options"]],
                         ["o1", "o2"])
        self.assertEqual(out[0]["data"]["options"][1]["label"], "no")
        reply, resolved = state.answer("aaaa0001-4", Answer(option_id="o1"))
        self.assertEqual(reply["result"],
                         {"answers": {"q1": {"answers": [f"use {key}"]}}})
        self.assertNotIn("sk-ant", json.dumps(resolved))
        self.assertEqual(resolved[0]["data"]["option_id"], "o1")
        # An id that names no choice is passed on as the answer.
        self.feed(state, {
            "id": 5, "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "q2", "question": "Which?",
                                      "options": [{"label": "a"}]}]}})
        reply, _ = state.answer("aaaa0001-5", Answer(option_id="other"))
        self.assertEqual(reply["result"],
                         {"answers": {"q2": {"answers": ["other"]}}})

    def test_requests_nothing_serves_get_an_error_answer(self):
        state, _clock, _ = self.in_turn()
        for rpc, method, params in (
                (1, "mcpServer/elicitation/request", {}),
                (2, "item/tool/requestUserInput",
                 {"questions": [{"id": "a"}, {"id": "b"}]}),
                (3, "account/chatgptAuthTokens/refresh", {})):
            out, replies = self.feed(state, {"id": rpc, "method": method,
                                             "params": params})
            self.assertEqual(out, [])
            self.assertEqual(replies[0]["id"], rpc)
            self.assertEqual(replies[0]["error"]["code"], -32601)
        self.assertEqual(state.pending, {})
        self.assertEqual(state.unknown["mcpServer/elicitation/request"], 1)

    def test_a_request_the_server_takes_back(self):
        state, _clock, _ = self.in_turn()
        self.feed(state, {"id": 5, "method":
                          "item/commandExecution/requestApproval",
                          "params": {"command": "ls"}})
        out, _ = self.feed(state, _note("serverRequest/resolved",
                                        requestId=5))
        self.assertEqual([(e["type"], e["data"].get("source")
                           or e["data"].get("state")) for e in out],
                         [("approval.resolved", "harness"),
                          ("session.state", "running")])
        # After our own answer the same notification finds nothing.
        out, _ = self.feed(state, _note("serverRequest/resolved",
                                        requestId=5))
        self.assertEqual(out, [])

    def test_a_steer_lands_when_its_item_does(self):
        state, _clock, _ = self.in_turn()
        self.feed(state,
                  _note("item/started", item={"type": "userMessage",
                                              "id": "u1", "clientId": "swe-1"}),
                  _note("item/agentMessage/delta", itemId="m1",
                        delta="one two "))
        state.expect_steer(2, "stop", command_id="c2", device="phone",
                           attachments=["/a.png"])
        out, _ = self.feed(state, {"id": 2, "result": {"turnId": "native-1"}})
        self.assertEqual(out, [], "accepted, not landed yet")
        self.assertEqual(state.accepted, {1: "t-aaaa0001-1",
                                          2: "t-aaaa0001-1"})
        out, _ = self.feed(state,
                           _note("item/agentMessage/delta", itemId="m1",
                                 delta="three "),
                           _note("item/started", item={
                               "type": "userMessage", "id": "u2",
                               "clientId": "swe-2"}))
        self.assertEqual([e["type"] for e in out], [
            "message.delta", "message.completed", "message.completed"])
        self.assertEqual(out[1]["data"], {"id": "m1", "kind": "assistant",
                                          "text": "one two three "})
        self.assertEqual(out[2]["data"], {
            "id": "t-aaaa0001-1:user2", "kind": "user", "text": "stop",
            "by": "phone", "attachments": ["/a.png"]})
        self.assertEqual(out[2]["command_id"], "c2")
        # The abandoned reply's late deltas are dropped.
        out, _ = self.feed(state, _note("item/agentMessage/delta",
                                        itemId="m1", delta="four"))
        self.assertEqual(out, [])

    def test_a_steer_whose_item_overtook_its_answer(self):
        state, _clock, _ = self.in_turn()
        state.expect_steer(2, "stop")
        out, _ = self.feed(state, _note("item/started", item={
            "type": "userMessage", "id": "u2", "clientId": "swe-2"}))
        self.assertEqual(out, [])
        out, _ = self.feed(state, {"id": 2, "result": {"turnId": "n"}})
        self.assertEqual([e["data"]["text"] for e in out], ["stop"])

    def test_steers_without_client_ids_land_in_order(self):
        state, _clock, _ = self.in_turn()
        state.expect_steer(2, "first")
        state.expect_steer(3, "second")
        self.feed(state, {"id": 2, "result": {}}, {"id": 3, "result": {}})
        out, _ = self.feed(state, _note("item/started", item={
            "type": "userMessage", "id": "u2", "clientId": None}))
        self.assertEqual([e["data"]["text"] for e in out], ["first"])
        # The turn ends before the second is announced: it still shows.
        out, _ = self.feed(state, _note("turn/completed", turn={
            "id": "native-1", "status": "completed"}))
        self.assertEqual([e["type"] for e in out], [
            "message.completed", "turn.ended", "session.state"])
        self.assertEqual(out[0]["data"]["id"], "t-aaaa0001-1:user3")

    def test_a_steer_answered_after_the_turn_ended(self):
        state, _clock, _ = self.in_turn()
        state.expect_steer(2, "late")
        self.feed(state, _note("turn/completed", turn={
            "id": "native-1", "status": "completed"}))
        fed = state.feed({"id": 2, "result": {"turnId": "native-1"}})
        self.assertEqual(fed.events, [])
        self.assertNotIn(2, state.accepted)

    def test_turn_end_closes_what_is_open(self):
        state, clock, _ = self.in_turn(command_id="c1")
        self.feed(state,
                  _note("item/agentMessage/delta", itemId="m1",
                        delta="half a"),
                  {"id": 5, "method": "item/commandExecution/requestApproval",
                   "params": {"command": "ls"}})
        clock.advance(2.5)
        out, _ = self.feed(
            state,
            _note("turn/completed", turn={"id": "some-older-turn",
                                          "status": "completed"}))
        self.assertEqual(out, [], "another turn's end is not ours")
        out, _ = self.feed(state, _note("turn/completed", turn={
            "id": "native-1", "status": "completed"}))
        self.assertEqual([e["type"] for e in out], [
            "message.delta", "message.completed", "approval.resolved",
            "turn.ended", "session.state"])
        self.assertEqual(out[1]["data"]["text"], "half a")
        self.assertEqual(out[2]["data"]["source"], "turn_ended")
        self.assertEqual(out[3]["data"], {"status": "completed",
                                          "duration_s": 2.5})
        self.assertEqual(out[3]["command_id"], "c1")
        self.assertEqual(out[4]["data"]["state"], "idle")
        self.assertFalse(state.busy)
        # Nothing of it is left for the next turn.
        out, _ = self.feed(state, _note("turn/completed", turn={
            "id": "native-1", "status": "completed"}))
        self.assertEqual(out, [])

    def test_interrupt(self):
        state, _clock, _ = self.in_turn()
        self.feed(state, {"id": 5, "method":
                          "item/fileChange/requestApproval", "params": {}})
        out = state.interrupt_sent()
        self.assertEqual([(e["type"], e["data"].get("source")
                           or e["data"].get("state")) for e in out],
                         [("approval.resolved", "turn_ended"),
                          ("session.state", "running")])
        out, _ = self.feed(state, _note("turn/completed", turn={
            "id": "native-1", "status": "interrupted"}))
        self.assertEqual(out[0]["data"]["status"], "interrupted")
        self.assertEqual(state.interrupt_sent(), [])

    def test_a_failed_turn(self):
        state, _clock, _ = self.in_turn()
        failure = {"message": "quota: token=abc12345 exhausted",
                   "codexErrorInfo": {"httpConnectionFailed": {}}}
        out, _ = self.feed(
            state,
            _note("error", error=failure, willRetry=True),
            _note("error", error=failure, willRetry=False),
            _note("turn/completed", turn={"id": "native-1",
                                          "status": "failed",
                                          "error": failure}))
        self.assertEqual([e["type"] for e in out],
                         ["error", "turn.ended", "session.state"])
        self.assertEqual(out[0]["data"]["code"], "httpConnectionFailed")
        self.assertEqual(out[1]["data"]["status"], "failed")
        self.assertEqual(out[1]["data"]["reason"], out[0]["data"]["message"])
        # With no error notification first, the turn's own error serves.
        state, _clock, _ = self.in_turn()
        out, _ = self.feed(state, _note("turn/completed", turn={
            "id": "native-1", "status": "failed",
            "error": {"message": "boom"}}))
        self.assertEqual([(e["type"], e["data"].get("code")) for e in out][0],
                         ("error", "error"))
        state, _clock, _ = self.in_turn()
        out, _ = self.feed(state, _note("turn/completed", turn={
            "id": "native-1", "status": "failed"}))
        self.assertEqual(out[0]["data"]["message"], "the turn failed")
        # An error with no turn open is not ours to report.
        out, _ = self.feed(state, _note("error", error=failure,
                                        willRetry=False))
        self.assertEqual(out, [])

    def test_plan_usage_and_compaction(self):
        state, _clock, _ = self.in_turn()
        out, _ = self.feed(
            state,
            _note("turn/plan/updated", explanation="because",
                  plan=[{"step": "a", "status": "completed"},
                        {"step": "b", "status": "inProgress"},
                        {"step": "c", "status": "pending"}, "junk"]),
            _note("item/completed", item={"type": "plan", "id": "p1",
                                          "text": "# The plan"}),
            _note("thread/tokenUsage/updated", tokenUsage={
                "last": {"inputTokens": 10, "outputTokens": 2,
                         "cachedInputTokens": 4, "totalTokens": 12,
                         "cacheWriteInputTokens": -1},
                "modelContextWindow": 1000}),
            _note("thread/tokenUsage/updated", tokenUsage={"last": None}),
            _note("thread/tokenUsage/updated", tokenUsage={"last": {}}),
            _note("thread/compacted"),
            _note("item/completed", item={"type": "contextCompaction",
                                          "id": "cc"}))
        self.assertEqual([e["type"] for e in out], [
            "plan.updated", "plan.updated", "usage.updated",
            "context.compacted", "context.compacted"])
        self.assertEqual(out[0]["data"], {"text": "because", "items": [
            {"text": "a", "status": "completed"},
            {"text": "b", "status": "in_progress"},
            {"text": "c", "status": "pending"}]})
        self.assertEqual(out[1]["data"], {"items": [], "text": "# The plan"})
        self.assertEqual(out[2]["data"], {
            "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 4,
            "context_tokens": 12, "context_window": 1000})

    def test_other_threads_and_unknown_lines_are_skipped(self):
        state, _clock, _ = self.in_turn()
        out, replies = self.feed(
            state,
            {"method": "item/agentMessage/delta",
             "params": {"threadId": "a-sub-agent", "itemId": "m9",
                        "delta": "not ours"}},
            {"method": "account/rateLimits/updated", "params": {}},
            {"method": "item/completed", "params": "not an object"},
            {"method": "item/started", "params": {"item": {"type": "x"}}},
            {"neither": "method nor id"},
            _note("item/completed", item={"type": "userMessage",
                                          "id": "u1"}))
        self.assertEqual((out, replies), ([], []))
        self.assertEqual(state.unknown, {
            "other-thread": 1, "account/rateLimits/updated": 1, "?": 1})
        self.assertEqual(state.feed("junk").events, [])

    def test_a_command_that_outlives_its_turn(self):
        state, _clock, _ = self.in_turn()
        command = {"type": "commandExecution", "id": "x1",
                   "command": "sleep 25", "status": "inProgress"}
        self.feed(state, _note("item/started", item=command),
                  _note("turn/completed", turn={"id": "native-1",
                                                "status": "completed"}))
        out, _ = self.feed(
            state,
            _note("item/completed", item={**command, "status": "completed",
                                          "aggregatedOutput": "late\n"}),
            # Anything else past the turn's end is dropped.
            _note("item/completed", item={"type": "agentMessage",
                                          "id": "m5", "text": "stray"}),
            _note("item/agentMessage/delta", itemId="m6", delta="stray"))
        self.assertEqual([e["type"] for e in out], ["tool.completed"])
        self.assertNotIn("turn_id", out[0])
        self.assertEqual(out[0]["data"]["output"], "late\n")

    def test_a_command_that_ends_during_a_later_turn(self):
        # The keeper may start the next turn at once: the old command's
        # result must not be filed under it.
        state, _clock, _ = self.in_turn()
        old = {"type": "commandExecution", "id": "x1",
               "command": "sleep 25", "status": "inProgress"}
        self.feed(state, _note("item/started", item=old),
                  _note("turn/completed", turn={"id": "native-1",
                                                "status": "completed"}))
        state.expect_turn(2, "next")
        self.feed(state, {"id": 2, "result": {"turn": {"id": "native-2"}}})
        new = {"type": "commandExecution", "id": "x2", "command": "ls",
               "status": "inProgress"}
        out, _ = self.feed(
            state,
            _note("item/started", item=new),
            _note("item/completed", item={**old, "status": "completed"}),
            _note("item/completed", item={**new, "status": "completed"}))
        done = _of(out, "tool.completed")
        self.assertEqual([e["data"]["call_id"] for e in done], ["x1", "x2"])
        self.assertNotIn("turn_id", done[0])
        self.assertEqual(done[1]["turn_id"], "t-aaaa0001-2")

    def test_events_before_the_start_pair_are_held(self):
        state = codex_wire.ThreadState("codex:t", run_id="aaaa0001")
        state.thread_id = THREAD
        fed = state.feed(_note("turn/started", turn={"id": "n1"}))
        self.assertEqual(fed.events, [])
        out = state.start_events(codex_wire.PROVEN, STEM)
        self.assertEqual([e["type"] for e in out], [
            "session.caps", "session.state", "turn.started",
            "session.state"])

    def test_exit_and_close_sequences(self):
        for finish, source, status, last in (
                (lambda s: s.exited("codex exited with code 3"),
                 "agent_exit", "failed", "agent_down"),
                (lambda s: s.closed(), "closed", "interrupted", "closed")):
            state, _clock, _ = self.in_turn()
            self.feed(state, {"id": 5, "method":
                              "item/commandExecution/requestApproval",
                              "params": {"command": "ls"}})
            out = finish(state)
            self.assertEqual([e["type"] for e in out], [
                "approval.resolved", "turn.ended", "session.state"])
            self.assertEqual(out[0]["data"]["source"], source)
            self.assertEqual(out[1]["data"]["status"], status)
            self.assertEqual(out[2]["data"]["state"], last)
            self.assertEqual(finish(state), [], "said once")
            self.assertEqual(state.feed(_note("turn/started",
                                              turn={"id": "n"})).events, [])

    def test_narrowing_says_so_once(self):
        state, _clock = self.state()
        out: list = []
        state.narrow(out, Caps(rollback=True, fork=True,
                               options=codex_wire.PROVEN.options))
        state.narrow(out, Caps(rollback=True, fork=True,
                               options=codex_wire.PROVEN.options))
        self.assertEqual(len(out), 1)
        self.assertIs(out[0]["data"]["caps"]["steer"], False)
        self.assertIs(out[0]["data"]["caps"]["rollback"], True)

    def test_replay_maps_recorded_answers(self):
        answer = codex_wire._answer_from_reply
        self.assertEqual(answer({"decision": "accept"}).decision, "allow")
        self.assertEqual(answer({"decision": "acceptForSession"}).decision,
                         "always")
        self.assertEqual(answer({"decision": "cancel"}).decision, "deny")
        self.assertEqual(answer(None).decision, "deny")
        self.assertEqual(answer({"permissions": {}}).decision, "deny")
        self.assertEqual(answer({"permissions": {"n": 1},
                                 "scope": "session"}).decision, "always")
        self.assertEqual(answer({"permissions": {"n": 1}}).decision, "allow")
        self.assertEqual(answer({"answers": {"q": {"answers": ["a", "b"]}}}
                                ).text, "a b")
        self.assertEqual(answer({"answers": {}}).text, "-")
        # A fixture with no header, and lines replay skips.
        got = codex_wire.replay([
            {"proc": 0, "dir": "out", "t": 0.0,
             "msg": codex_wire.thread_start(1, cwd="/w")},
            {"proc": 0, "dir": "in", "t": 0.1, "raw": "not json"},
            {"proc": 0, "dir": "in", "t": 0.2,
             "msg": {"id": 1, "result": {"thread": {"id": THREAD}}}},
            {"proc": 0, "exit": 0}])
        self.assertEqual([e["type"] for e in got],
                         ["session.caps", "session.state", "session.state"])
        self.assertEqual(got[1]["data"]["harness_session_id"], THREAD)


def _codex_resolved(caps: Caps = ALL_CAPS,
                    min_version: str = "") -> registry.Resolved:
    return registry.Resolved("codex", "codex_appserver",
                             (sys.executable, str(CODEX_FAKE)), caps,
                             min_version=min_version)


class _CodexCase(unittest.TestCase):
    """An adapter over the fake app-server, cleaned up after the test."""

    def fake_env(self, mode: str = "ok", reply: str = "hello",
                 **extra) -> dict:
        self.log = temp_dir(self) / "fake.log"
        self.codex_home = temp_dir(self)
        env = dict(os.environ, FAKE_CODEX_MODE=mode, FAKE_CODEX_REPLY=reply,
                   FAKE_CODEX_LOG=str(self.log),
                   CODEX_HOME=str(self.codex_home))
        env.update(extra)
        return env

    def adapter(self, mode: str = "ok", *, caps: Caps = ALL_CAPS,
                reply: str = "hello", min_version: str = "",
                env_extra: dict | None = None, **kw):
        adapter = codex_appserver.CodexAppServerAdapter(
            _codex_resolved(caps, min_version),
            env=self.fake_env(mode, reply, **(env_extra or {})), **kw)
        self.addCleanup(adapter.close, 0.5)
        return adapter

    def spec(self, **kw) -> StartSpec:
        return StartSpec(session_key="codex:test", cwd=str(temp_dir(self)),
                         **kw)

    def started(self, mode: str = "ok", **kw):
        spec_kw = kw.pop("spec", {})
        adapter = self.adapter(mode, **kw)
        sink = fake.ListSink()
        sid = adapter.start(self.spec(**spec_kw), sink)
        return adapter, sink, sid

    def sent(self) -> list:
        return [json.loads(line) for line in
                self.log.read_text().splitlines()] if self.log.exists() \
            else []

    def sent_methods(self) -> list:
        return [m.get("method") for m in self.sent()[1:]]

    def wait(self, sink, match, what="the event"):
        found = sink.wait_for(match, TIMEOUT)
        self.assertIsNotNone(found, f"timed out waiting for {what}")
        return found

    def wait_turn(self, sink, turn_id):
        """The turn's ``turn.ended``, once the ``session.state`` that
        follows it is in too (they are emitted together)."""
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

    def asked(self, sink, etype="approval.requested"):
        return self.wait(sink, etype, "the request")

    def wait_state(self, sink, state):
        return self.wait(sink, lambda e: e["type"] == "session.state"
                         and e["data"]["state"] == state, state)


class TestCodexContract(AdapterContract, _CodexCase):
    def make_adapter(self, *, caps, reply):
        return self.adapter(caps=caps, reply=reply)


class TestCodexAdapterStart(_CodexCase):
    def test_registered_and_resolved(self):
        self.assertIn("codex_appserver", registry.registered())
        self.assertIn("codex_appserver", registry.BUILTIN_MODULES)
        resolved = registry.adapter_for("codex", {}, which=lambda b: b)
        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.argv, codex_wire.DEFAULT_ARGV)
        self.assertEqual(resolved.caps, Caps(),
                         "the fallback keeps every feature off")
        made = registry.create(resolved)
        self.assertIsInstance(made, codex_appserver.CodexAppServerAdapter)
        self.assertEqual(made.capabilities, Caps())

    def test_caps_never_exceed_what_is_proven(self):
        adapter = self.adapter()
        self.assertEqual(adapter.capabilities, codex_wire.PROVEN)
        self.assertFalse(adapter.capabilities.plan_mode)
        self.assertFalse(adapter.capabilities.stop_task)
        self.assertEqual(self.adapter(caps=Caps(steer=True)).capabilities,
                         Caps(steer=True))

    def test_start_handshakes_and_names_the_session(self):
        adapter, sink, sid = self.started(spec={
            "model": "gpt-x", "permission_mode": "on-request",
            "session_id": "the-hubs-pick", "env": {"FAKE_CODEX_EXTRA": "1"}})
        self.assertEqual(sid, STEM, "the id SWE's session list uses")
        self.assertEqual(adapter.session_id, STEM)
        self.assertEqual(adapter.version, "0.170.0")
        self.assertEqual(sink.types(), ["session.caps", "session.state"])
        self.assertEqual(sink.events[0]["data"]["caps"],
                         codex_wire.PROVEN.as_dict())
        self.assertEqual(sink.events[1]["data"], {
            "state": "idle", "harness_session_id": STEM})
        first, *rest = self.sent()
        self.assertEqual(first["argv"], [])
        self.assertEqual(first["env"]["EXTRA"], "1")
        self.assertEqual([m.get("method") for m in rest],
                         ["initialize", "initialized", "thread/start"])
        self.assertEqual(rest[0]["params"]["clientInfo"]["name"], "swe")
        self.assertEqual(rest[2]["params"], {
            "cwd": adapter._spec.cwd, "model": "gpt-x",
            "approvalPolicy": "on-request"})

    def test_a_permission_mode_codex_does_not_know_is_left_out(self):
        self.started(spec={"permission_mode": "acceptEdits"})
        self.assertNotIn("approvalPolicy", self.sent()[3]["params"])

    def test_resume_and_fork(self):
        adapter, sink, sid = self.started(spec={"resume_id": STEM})
        self.assertEqual(sid, STEM)
        resume = self.sent()[3]
        self.assertEqual(resume["method"], "thread/resume")
        self.assertEqual(resume["params"]["threadId"], THREAD)
        self.assertIs(resume["params"]["excludeTurns"], True)
        adapter.close(0.5)
        forked, sink, sid = self.started(spec={
            "resume_id": THREAD, "fork": True, "model": "m"})
        self.assertEqual(sid, f"rollout-2026-01-02T03-04-05-{FORKED}")
        fork = self.sent()[3]
        self.assertEqual((fork["method"], fork["params"]["threadId"],
                          fork["params"]["model"]),
                         ("thread/fork", THREAD, "m"))
        turn_id = forked.send_turn(TurnInput("hi"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(self.sent()[-1]["params"]["threadId"], FORKED)

    def test_bad_resume_ids(self):
        adapter = self.adapter()
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(self.spec(resume_id="not-a-thread"),
                          fake.ListSink())
        self.assertEqual(ctx.exception.code, "bad_request")
        gone = "99999999-2222-7333-8444-555555555555"
        other = self.adapter()
        sink = fake.ListSink()
        with self.assertRaises(AdapterError) as ctx:
            other.start(self.spec(resume_id=gone), sink)
        self.assertEqual(ctx.exception.code, "refused")
        self.assertIn("no rollout found", str(ctx.exception))
        self.assertEqual(sink.events, [], "a failed start emits nothing")
        with self.assertRaises(AdapterError) as ctx:
            other.start(self.spec(), sink)
        self.assertEqual(ctx.exception.code, "started")

    def assert_start_fails(self, mode, code, text="", **kw):
        adapter = self.adapter(mode, **kw)
        sink = fake.ListSink()
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(self.spec(), sink)
        self.assertEqual(ctx.exception.code, code)
        if text:
            self.assertIn(text, str(ctx.exception))
        self.assertEqual(sink.events, [])
        self.assertIsNotNone(adapter._proc.poll(), "the child is stopped")
        with self.assertRaises(AdapterError):
            adapter.send_turn(TurnInput("hi"))
        return adapter

    def test_start_failures(self):
        self.assert_start_fails("exit-at-init", "agent_down",
                                text="not logged in")
        self.assert_start_fails("refuse-init", "refused",
                                text="client version not supported")
        self.assert_start_fails("refuse-thread", "refused",
                                text="not logged in")
        self.assert_start_fails("silent-init", "timeout", init_timeout=0.3)

    def test_version_floor(self):
        adapter = self.assert_start_fails("old", "unsupported_version",
                                          text="0.1.0 is older than 0.170.0")
        self.assertEqual(adapter.version, "0.1.0")
        # harness.json's min_version wins over the module's.
        _adapter, _sink, sid = self.started("old", min_version="0.0.1")
        self.assertEqual(sid, STEM)

    def test_spawn_failure(self):
        def boom(*a, **kw):
            raise OSError("no such file")

        adapter = codex_appserver.CodexAppServerAdapter(
            _codex_resolved(), spawn=boom)
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(self.spec(), fake.ListSink())
        self.assertEqual(ctx.exception.code, "agent_down")
        adapter.close()

    def test_transcript_position(self):
        adapter, _sink, _sid = self.started()
        self.assertIsNone(adapter.transcript_position(),
                          "no rollout before the first turn")
        rollout = Path(adapter._rollout)
        self.assertEqual(rollout.name, STEM + ".jsonl")
        rollout.parent.mkdir(parents=True)
        rollout.write_text("12345")
        position = adapter.transcript_position()
        self.assertEqual((position.path, position.offset), (str(rollout), 5))
        self.assertIsNone(self.adapter().transcript_position())


class TestCodexAdapterTurns(_CodexCase):
    def test_a_turn_streams_and_ends(self):
        adapter, sink, _sid = self.started(reply="hello there")
        turn_id = adapter.send_turn(TurnInput(
            "hi", command_id="c1", device="phone",
            attachments=["/w/a.txt"]))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        self.assertEqual(ended["command_id"], "c1")
        mine = [e for e in sink.events if e.get("turn_id") == turn_id]
        self.assertEqual([e["type"] for e in mine], [
            "turn.started", "message.completed", "message.delta",
            "message.delta", "message.completed", "usage.updated",
            "turn.ended"])
        self.assertEqual(mine[1]["data"]["attachments"], ["/w/a.txt"])
        _streams_close(self, sink.events)
        self.assertEqual(sink.events[-1]["data"]["state"], "idle")
        start = self.sent()[-1]
        self.assertEqual(start["method"], "turn/start")
        self.assertEqual(start["params"]["input"][0]["text"],
                         "[Attached file: /w/a.txt] hi")
        # A second turn gets the next id.
        second = adapter.send_turn(TurnInput("again"))
        self.assertNotEqual(second, turn_id)
        self.wait_turn(sink, second)

    def test_busy_mid_turn(self):
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
        self.assertEqual(self.sent()[-1]["params"]["turnId"],
                         adapter._state.native_turns[turn_id])
        _streams_close(self, sink.events)
        before = len(sink.events)
        adapter.interrupt()
        self.assertEqual(len(sink.events), before)

    def test_a_refused_turn(self):
        adapter, sink, _sid = self.started("refuse-turn")
        before = len(sink.events)
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput("go"))
        self.assertEqual(ctx.exception.code, "refused")
        self.assertIn("the model is not available", str(ctx.exception))
        self.assertEqual(len(sink.events), before)
        self.assertFalse(adapter._state.busy)

    def test_a_turn_start_the_server_never_answers(self):
        adapter, sink, _sid = self.started()
        # Swallow the answer: the reader never sees it.
        with mock.patch.object(adapter, "_dispatch"):
            with mock.patch.object(adapter, "_call_timeout", 0.2):
                with self.assertRaises(AdapterError) as ctx:
                    adapter.send_turn(TurnInput("go"))
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertFalse(adapter._state.busy)
        self.assertEqual(sink.of("turn.started"), [])

    def test_approval_allow(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.asked(sink)
        self.assertEqual(asked["data"]["kind"], "command")
        self.assertEqual(asked["data"]["call_id"], "exec-0001")
        self.assertEqual([o["id"] for o in asked["data"]["options"]],
                         ["allow", "always", "deny"])
        self.assertTrue(asked["data"]["request_id"].endswith("-0"))
        self.wait_state(sink, "waiting")
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="always", device="mac"))
        self.wait_turn(sink, turn_id)
        resolved = sink.of("approval.resolved")
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["data"]["decision"], "always")
        self.assertEqual(resolved[0]["data"]["by"], "mac")
        reply = [m for m in self.sent() if "result" in m][0]
        self.assertEqual(reply, {"jsonrpc": "2.0", "id": 0, "result": {
            "decision": "acceptForSession"}})
        done = sink.of("tool.completed")[0]["data"]
        self.assertEqual((done["status"], done["output"]),
                         ("ok", "made a.txt\n"))
        with self.assertRaises(UnknownRequest):
            adapter.respond(asked["data"]["request_id"],
                            Answer(decision="allow"))
        _contract_order(self, sink.events)

    def test_approval_deny_and_a_file_change(self):
        adapter, sink, _sid = self.started("patch")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.asked(sink)
        self.assertEqual(asked["data"]["kind"], "tool")
        self.assertEqual(asked["data"]["prompt"],
                         "Apply file changes:\n/w/new.txt\n/w/old.txt")
        adapter.respond(asked["data"]["request_id"], Answer(decision="deny"))
        self.wait_turn(sink, turn_id)
        done = sink.of("tool.completed")[0]["data"]
        self.assertEqual(done["status"], "cancelled")
        self.assertNotIn("file_edits", done)
        # Allowed, the edits come with the completion.
        turn_id = adapter.send_turn(TurnInput("again"))
        asked = self.wait(sink, lambda e: e["type"] == "approval.requested"
                          and e.get("turn_id") == turn_id)
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="allow"))
        self.wait_turn(sink, turn_id)
        edits = sink.of("tool.completed")[1]["data"]["file_edits"]
        self.assertEqual(edits, [
            {"path": "/w/new.txt", "kind": "add",
             "diff": "@@ -0,0 +1,1 @@\n+yes\n"},
            {"path": "/w/moved.txt", "kind": "modify",
             "old_path": "/w/old.txt", "diff": "@@ -1 +1 @@\n-a\n+b\n"}])

    def test_question_and_permissions(self):
        adapter, sink, _sid = self.started("question", reply="x")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.asked(sink, "input.requested")
        self.assertEqual(asked["data"]["kind"], "choice")
        self.assertEqual(asked["data"]["options"],
                         [{"id": "o1", "label": "red"},
                          {"id": "o2", "label": "blue"}])
        adapter.respond(asked["data"]["request_id"], Answer(option_id="o1"))
        self.wait_turn(sink, turn_id)
        replies = [e["data"]["text"] for e in sink.of("message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies, ["you said red"])
        turn_id = adapter.send_turn(TurnInput("mode:permissions go"))
        asked = self.wait(sink, lambda e: e["type"] == "approval.requested")
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="allow"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(sink.of("message.completed")[-1]["data"]["text"],
                         "granted")
        # Two questions at once have no card: the server is told so and
        # goes on.
        turn_id = adapter.send_turn(TurnInput("mode:questions go"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(sink.of("message.completed")[-1]["data"]["text"],
                         "unanswered")

    def test_a_withdrawn_approval(self):
        adapter, sink, _sid = self.started("withdraw")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.wait_turn(sink, turn_id)
        resolved = sink.of("approval.resolved")
        self.assertEqual([e["data"]["source"] for e in resolved],
                         ["harness"])
        with self.assertRaises(UnknownRequest):
            adapter.respond(resolved[0]["data"]["request_id"],
                            Answer(decision="allow"))
        _contract_order(self, sink.events)

    def test_interrupt_retires_an_open_approval(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.asked(sink)
        adapter.interrupt()
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "interrupted")
        self.assertEqual(sink.of("approval.resolved")[0]["data"]["source"],
                         "turn_ended")
        with self.assertRaises(UnknownRequest):
            adapter.respond(asked["data"]["request_id"],
                            Answer(decision="allow"))

    def test_noise_changes_nothing(self):
        adapter, sink, _sid = self.started("noise", reply="fine")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.wait_turn(sink, turn_id)
        replies = [e["data"]["text"] for e in sink.of("message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies, ["fine"])
        self.assertEqual(list(adapter.dropped), [])
        # The unknown server request got its error answer (queued by the
        # reader; closing lets the fake read to the end of its input).
        adapter.close(2.0)
        refusals = [m for m in self.sent() if "error" in m]
        self.assertEqual([(m["id"], m["error"]["code"]) for m in refusals],
                         [(900, -32601)])
        _contract_order(self, sink.events)

    def test_plan_reasoning_and_tools(self):
        adapter, sink, _sid = self.started("plan")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(len(sink.of("plan.updated")[0]["data"]["items"]), 3)
        thinking = sink.of("thinking.completed")
        self.assertEqual([e["data"]["text"] for e in thinking],
                         ["thinking it over"])
        tools = [(e["data"]["name"], e["data"]["status"])
                 for e in sink.of("tool.completed")]
        self.assertEqual(tools, [("web_search", "ok"),
                                 ("files.read", "error")])
        self.assertEqual(sink.of("tool.completed")[1]["data"]["output"],
                         "no such file")
        self.assertEqual(len(sink.of("context.compacted")), 1)
        _streams_close(self, sink.events)
        _contract_order(self, sink.events)

    def test_a_failed_turn_is_redacted(self):
        adapter, sink, _sid = self.started("fail")
        turn_id = adapter.send_turn(TurnInput("go"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "failed")
        errors = sink.of("error")
        self.assertEqual(len(errors), 1, "the retried error is not shown")
        self.assertEqual(errors[0]["data"]["code"], "modelNotFound")
        self.assertNotIn("sk-test", json.dumps(sink.events))
        self.assertIn("api_key=[REDACTED]", errors[0]["data"]["message"])
        # The session goes on.
        self.assertEqual(sink.events[-1]["data"]["state"], "idle")

    def test_the_agent_dying_mid_turn(self):
        adapter, sink, _sid = self.started("crash")
        turn_id = adapter.send_turn(TurnInput("go"))
        down = self.wait_state(sink, "agent_down")
        self.assertIn("code 3", down["data"]["reason"])
        self.assertIn("the model stream broke", down["data"]["reason"])
        self.assertEqual(sink.types()[-2:], ["turn.ended", "session.state"])
        ended = [e for e in sink.of("turn.ended")
                 if e.get("turn_id") == turn_id]
        self.assertEqual(ended[0]["data"]["status"], "failed")
        count = len(sink.events)
        for call in (lambda: adapter.send_turn(TurnInput("x")),
                     adapter.interrupt,
                     lambda: adapter.set_option("model", "m"),
                     lambda: adapter.respond("r", Answer(decision="allow"))):
            with self.assertRaises(AdapterError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "agent_down")
        adapter.close(0.5)
        self.assertEqual(len(sink.events), count, "nothing after agent_down")

    def test_a_dead_pipe_is_agent_down(self):
        adapter, sink, _sid = self.started("approval")
        adapter.send_turn(TurnInput("go"))
        asked = self.asked(sink)
        adapter._proc.stdin.close()
        for call in (lambda: adapter.respond(asked["data"]["request_id"],
                                             Answer(decision="allow")),
                     adapter.interrupt):
            with self.assertRaises(AdapterError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "agent_down")
        self.wait_state(sink, "agent_down")
        self.assertEqual(sink.of("approval.resolved")[-1]["data"]["source"],
                         "agent_exit", "the unanswered request is retired")

    def test_close_mid_turn(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.asked(sink)
        adapter.close(1.0)
        tail = sink.events[-3:]
        self.assertEqual([e["type"] for e in tail], [
            "approval.resolved", "turn.ended", "session.state"])
        self.assertEqual(tail[0]["data"]["source"], "closed")
        self.assertEqual((tail[1]["turn_id"], tail[1]["data"]["status"]),
                         (turn_id, "interrupted"))
        self.assertEqual(tail[2]["data"]["state"], "closed")
        self.assertEqual(adapter._proc.poll(), 0, "EOF ended the server")
        self.assertFalse(adapter._writer.thread.is_alive())

    def test_close_kills_a_server_that_lingers(self):
        adapter, sink, _sid = self.started("linger")
        adapter.close(0.2)
        self.assertEqual(sink.events[-1]["data"]["state"], "closed")
        self.assertIsNotNone(adapter._proc.poll(), "terminated")

    def test_a_server_that_stops_reading(self):
        import time as _time

        adapter, sink, _sid = self.started("deaf", write_timeout=0.5)
        big = "x" * (2 * 1024 * 1024)  # past any pipe buffer
        began = _time.monotonic()
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput(big))
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertLess(_time.monotonic() - began, 5.0)
        self.assertEqual(sink.of("turn.started"), [])
        self.assertFalse(adapter._state.busy)
        self.wait_state(sink, "agent_down")
        adapter.close(0.5)
        self.assertIsNotNone(adapter._proc.poll(), "the child is gone")
        self.assertFalse(adapter._writer.thread.is_alive())

    def test_close_while_the_writer_is_stuck(self):
        import time as _time

        adapter, sink, _sid = self.started("deaf", write_timeout=30.0)
        adapter._write(codex_wire.request(99, "x", {
            "pad": "y" * (2 * 1024 * 1024)}), wait=False)
        began = _time.monotonic()
        adapter.close(0.5)
        self.assertLess(_time.monotonic() - began, 8.0)
        self.assertIsNotNone(adapter._proc.poll(), "the child is gone")
        self.assertEqual(sink.events[-1]["data"]["state"], "closed")
        self.assertFalse(adapter._writer.thread.is_alive())

    def test_a_line_the_state_machine_chokes_on_is_skipped(self):
        adapter, sink, _sid = self.started(reply="still here")
        real = adapter._state.feed
        calls = []

        def feed(msg):
            if not calls:
                calls.append(msg)
                raise RuntimeError("boom")
            return real(msg)

        with mock.patch.object(adapter._state, "feed", feed):
            with mock.patch.object(adapter, "_call_timeout", 0.3):
                # The answer to turn/start is the line that is lost; the
                # turn's own notifications still open it.
                turn_id = adapter.send_turn(TurnInput("eaten"))
            self.wait_turn(sink, turn_id)
        self.assertEqual(len(adapter.dropped), 1)
        self.assertIn("boom", adapter.dropped[0])
        self.assertEqual(sink.of("message.completed")[-1]["data"]["text"],
                         "still here")


class TestCodexAdapterSteer(_CodexCase):
    def test_a_steer_joins_the_turn(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count", command_id="c1"))
        self.wait(sink, "message.delta")
        adapter.steer(TurnInput("stop", command_id="c2", device="phone",
                                attachments=["/w/a.png"]))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        self.assertEqual(len(sink.of("turn.started")), 1)
        done = sink.of("message.completed")
        self.assertEqual([(e["data"]["kind"], e["data"]["text"])
                          for e in done],
                         [("user", "count"), ("assistant", "1\n2\n"),
                          ("user", "stop"), ("assistant", "steered")])
        self.assertEqual(done[2]["command_id"], "c2")
        self.assertEqual(done[2]["data"]["by"], "phone")
        self.assertEqual(done[2]["data"]["attachments"], ["/w/a.png"])
        self.assertEqual(done[2]["turn_id"], turn_id)
        steer = [m for m in self.sent() if m.get("method") == "turn/steer"][0]
        self.assertEqual(steer["params"]["expectedTurnId"],
                         adapter._state.native_turns[turn_id])
        self.assertEqual(steer["params"]["input"][1],
                         {"type": "localImage", "path": "/w/a.png"})
        _streams_close(self, sink.events)
        _contract_order(self, sink.events)

    def test_a_steer_leaves_an_open_approval_open(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.asked(sink)
        adapter.steer(TurnInput("never mind"))
        self.assertEqual(sink.of("approval.resolved"), [])
        self.assertIn(asked["data"]["request_id"], adapter._state.pending)
        adapter.respond(asked["data"]["request_id"], Answer(decision="deny"))
        self.wait_turn(sink, turn_id)
        types = [(e["type"], e["data"].get("kind")) for e in sink.events]
        self.assertLess(types.index(("approval.resolved", None)),
                        [i for i, t in enumerate(types)
                         if t == ("message.completed", "user")][1])
        _contract_order(self, sink.events)

    def test_a_command_outlives_the_steered_turn(self):
        adapter, sink, _sid = self.started("late-tool")
        turn_id = adapter.send_turn(TurnInput("run it"))
        self.wait(sink, "message.delta")
        adapter.steer(TurnInput("never mind"))
        self.wait_turn(sink, turn_id)
        late = self.wait(sink, "tool.completed")
        self.assertNotIn("turn_id", late)
        self.assertEqual(late["data"]["output"], "late\n")
        types = sink.types()
        self.assertGreater(types.index("tool.completed"),
                           types.index("turn.ended"))

    def test_steer_refusals(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count"))
        self.wait(sink, "message.delta")
        # The server's turn id moved on under us: refused, nothing shown.
        with mock.patch.object(adapter._state.turn, "native_id", "stale"):
            with self.assertRaises(AdapterError) as ctx:
                adapter.steer(TurnInput("more"))
        self.assertEqual(ctx.exception.code, "refused")
        self.assertIn("does not match", str(ctx.exception))
        adapter.interrupt()
        self.wait_turn(sink, turn_id)
        before = len(sink.events)
        with self.assertRaises(AdapterError) as ctx:
            adapter.steer(TurnInput("more"))
        self.assertEqual(ctx.exception.code, "idle")
        self.assertEqual(len(sink.events), before)

    def test_a_steer_that_races_the_turns_end(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count"))
        self.wait(sink, "message.delta")
        native = adapter._state.native_turns[turn_id]
        adapter.interrupt()
        self.wait_turn(sink, turn_id)
        # The adapter still believed the turn was open when it wrote.
        adapter._state.open_turn([])
        adapter._state.turn.native_id = native
        with self.assertRaises(AdapterError) as ctx:
            adapter.steer(TurnInput("too late"))
        self.assertEqual(ctx.exception.code, "idle")

    def test_a_server_without_turn_steer_narrows_the_caps(self):
        adapter, sink, _sid = self.started("slow", env_extra={
            "FAKE_CODEX_MODE": "no-steer"})
        self.assertTrue(adapter.capabilities.steer)
        adapter.send_turn(TurnInput("mode:slow count"))
        self.wait(sink, "message.delta")
        with self.assertRaises(Unsupported) as ctx:
            adapter.steer(TurnInput("more"))
        self.assertEqual(ctx.exception.feature, "steer")
        caps = sink.of("session.caps")
        self.assertEqual(len(caps), 2)
        self.assertIs(caps[1]["data"]["caps"]["steer"], False)
        self.assertIs(caps[1]["data"]["caps"]["rollback"], True)
        self.assertFalse(adapter.capabilities.steer)
        with self.assertRaises(Unsupported):
            adapter.steer(TurnInput("again"))
        self.assertEqual(len(sink.of("session.caps")), 2)
        self.assertEqual(len([m for m in self.sent()
                              if m.get("method") == "turn/steer"]), 1)


class TestCodexAdapterOptions(_CodexCase):
    def test_options_ride_the_next_turn_start_once(self):
        adapter, sink, _sid = self.started(spec={"effort": "low"})
        adapter.set_option("model", "gpt-y")
        adapter.set_option("permission_mode", "never")
        self.assertEqual(self.sent_methods()[-1], "thread/start",
                         "nothing is written until a turn starts")
        self.wait_turn(sink, adapter.send_turn(TurnInput("one")))
        first = self.sent()[-1]["params"]
        self.assertEqual((first["model"], first["effort"],
                          first["approvalPolicy"]),
                         ("gpt-y", "low", "never"))
        self.wait_turn(sink, adapter.send_turn(TurnInput("two")))
        second = self.sent()[-1]["params"]
        for name in ("model", "effort", "approvalPolicy"):
            self.assertNotIn(name, second, "the server keeps them")
        adapter.set_option("effort", "high")
        self.wait_turn(sink, adapter.send_turn(TurnInput("three")))
        third = self.sent()[-1]["params"]
        self.assertEqual(third["effort"], "high")
        self.assertNotIn("model", third)

    def test_a_refused_turn_keeps_its_options_for_the_next(self):
        adapter, sink, _sid = self.started()
        adapter.set_option("model", "gpt-y")
        with self.assertRaises(AdapterError):
            adapter.send_turn(TurnInput("mode:refuse-turn go"))
        self.wait_turn(sink, adapter.send_turn(TurnInput("go")))
        self.assertEqual(self.sent()[-1]["params"]["model"], "gpt-y")

    def test_option_checks(self):
        adapter, _sink, _sid = self.started()
        with self.assertRaises(AdapterError) as ctx:
            adapter.set_option("permission_mode", "acceptEdits")
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertIn("untrusted, on-request, never", str(ctx.exception))
        with self.assertRaises(Unsupported):
            adapter.set_option("plan_mode", True)
        with self.assertRaises(ValueError):
            adapter.set_option("model", "two words")
        with self.assertRaises(ValueError):
            adapter.set_option("colour", "red")
        with self.assertRaises(Unsupported):
            adapter.stop_task("task-1")
        limited, _sink, _sid = self.started(caps=Caps(options={"model"}))
        limited.set_option("model", "m")
        with self.assertRaises(Unsupported):
            limited.set_option("effort", "high")

    def test_rollback(self):
        adapter, sink, _sid = self.started()
        first = adapter.send_turn(TurnInput("one"))
        self.wait_turn(sink, first)
        second = adapter.send_turn(TurnInput("two"))
        self.wait_turn(sink, second)
        third = adapter.send_turn(TurnInput("three"))
        self.wait_turn(sink, third)
        native = adapter._state.native_turns[second]
        before = len(sink.events)
        adapter.rollback(second)
        revert = self.sent()[-1]
        self.assertEqual(revert["method"], "thread/revert")
        self.assertEqual(revert["params"], {"threadId": THREAD,
                                            "beforeTurnId": native})
        # The reverted history is a new rollout file, so the id SWE's
        # session list knows the thread by moves; the adapter says so.
        moved = f"{STEM}_11111111-2222-7333-8444-777777777777"
        self.assertEqual([(e["type"], e["data"]) for e in
                          sink.events[before:]],
                         [("session.state", {"state": "idle",
                                             "harness_session_id": moved})])
        self.assertEqual(adapter.session_id, moved)
        self.assertTrue(adapter._rollout.endswith(moved + ".jsonl"))
        self.assertEqual(codex_wire.thread_id_of(moved), THREAD,
                         "it still resumes as the same thread")
        self.assertEqual(list(adapter._state.native_turns), [first])
        # A second one that names the same file says nothing.
        self.wait_turn(sink, adapter.send_turn(TurnInput("four")))
        count = len(sink.of("session.state"))
        adapter.rollback(list(adapter._state.native_turns)[-1])
        self.assertEqual(len(sink.of("session.state")), count)
        for gone in (second, third, "t-other-1"):
            with self.assertRaises(AdapterError) as ctx:
                adapter.rollback(gone)
            self.assertEqual(ctx.exception.code, "unknown_turn")

    def test_rollback_refusals(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count"))
        with self.assertRaises(AdapterError) as ctx:
            adapter.rollback(turn_id)
        self.assertEqual(ctx.exception.code, "busy")
        adapter.interrupt()
        self.wait_turn(sink, turn_id)
        other, sink, _sid = self.started("ok", env_extra={
            "FAKE_CODEX_MODE": "refuse-revert"})
        turn_id = other.send_turn(TurnInput("one"))
        self.wait_turn(sink, turn_id)
        with self.assertRaises(AdapterError) as ctx:
            other.rollback(turn_id)
        self.assertEqual(ctx.exception.code, "refused")
        self.assertIn(turn_id, other._state.native_turns)


class TestAdapterCore(unittest.TestCase):
    """The pieces of adapters/core.py the adapters share."""

    def test_small_helpers(self):
        self.assertEqual(core.text_of(3), "")
        self.assertEqual(core.jsonable({1: (float("nan"), {2}, None)}),
                         {"1": ["nan", "{2}", None]})
        self.assertEqual(core.clip("abc", 2), "ab\n…")
        self.assertEqual(core.clip("abc"), "abc")
        for bad in (-1, True, 1.5, "3", None):
            self.assertIsNone(core.count(bad))
        self.assertEqual(core.count(0), 0)

    def test_a_request_withdrawn_outside_a_turn(self):
        state = core.SessionCore("k:1", clock=fake.FakeClock(1.0),
                                 run_id="r")
        state.start_events(Caps(), "sid")
        out: list = []
        state.ask(out, "r1", "tool", "", None)
        self.assertEqual(out[0]["data"]["prompt"], "approval needed")
        self.assertFalse(state.withdrawn(out, "nope"))
        self.assertTrue(state.withdrawn(out, "r1"))
        self.assertEqual([e["type"] for e in out], [
            "approval.requested", "session.state", "approval.resolved"],
            "no turn is open: nothing is running again")
        state.finish_turn(out)  # no turn: a no-op
        self.assertEqual(len(out), 3)
        with self.assertRaises(UnknownRequest):
            state.take("r1")

    def test_an_answer_that_never_got_through_reopens_the_request(self):
        state = core.SessionCore("k:1", clock=fake.FakeClock(1.0),
                                 run_id="r")
        state.start_events(Caps(), "sid")
        out: list = []
        state.open_turn(out)
        state.ask(out, "r1", "tool", "run it", {"native": 7})
        payload = state.take("r1")
        dropped = state.answered("r1", Answer(decision="allow"))
        self.assertEqual([e["type"] for e in dropped],
                         ["approval.resolved", "session.state"])
        self.assertFalse(state.waiting)
        state.reopen("r1", payload)
        self.assertTrue(state.waiting)
        self.assertEqual(state.take("r1"), {"native": 7})
        # The answer that gets through says "running" again.
        again = state.answered("r1", Answer(decision="allow"))
        self.assertEqual([e["type"] for e in again],
                         ["approval.resolved", "session.state"])

    def test_the_writer_reports_a_broken_pipe(self):
        class Broken:
            def write(self, data):
                raise BrokenPipeError()

            def flush(self):
                pass

            def close(self):
                raise OSError("already closed")

        writer = core.LineWriter(Broken(), "test-writer")
        self.assertFalse(writer.send(b"x\n", 5.0))
        writer.thread.join(5.0)
        self.assertTrue(writer.broken)
        self.assertFalse(writer.send(b"y\n", 5.0), "takes nothing more")
        writer.stop(1.0)
        core.close_quietly(None)

    def test_the_writer_stalls_past_its_deadline(self):
        gate = threading.Event()

        class Stuck:
            def write(self, data):
                gate.wait(10.0)
                raise BrokenPipeError()  # as when the child is killed

            def flush(self):
                pass

            def close(self):
                pass

        writer = core.LineWriter(Stuck(), "test-writer")
        self.addCleanup(gate.set)
        with self.assertRaises(core.Stalled):
            writer.send(b"x\n", 0.1)
        self.assertTrue(writer.stalled)
        self.assertFalse(writer.send(b"y\n", None))
        gate.set()
        writer.stop(5.0)
        self.assertFalse(writer.thread.is_alive())


# =====================================================================
# G16: the opencode session adapter (remote/adapters/opencode_serve.py)
# and its wire module (opencode_wire.py).
#
# The same three layers: every recording under
# tests/fixtures/protocols/opencode_serve/ replays to its golden events;
# the wire module event by event; the adapter over
# tests/fakes/fake_opencode.py (a real HTTP server on a loopback port),
# including the shared AdapterContract. No test runs the real
# ``opencode``: CI machines have none.

OPENCODE_FAKE = Path(__file__).resolve().parent / "fakes" / "fake_opencode.py"
OPENCODE_FIXTURES = PROTOCOLS / "opencode_serve"
OC_SESSION = "ses_fake00000000000000000001"
OC_FORKED = "ses_fake00000000000000000002"
OC_MISSING = "ses_missing0000000000000001"
OC_KEY = "sk-ant-api03-" + "d" * 40


class TestOpencodeFixtures(unittest.TestCase):
    def fixtures(self):
        found = sorted(OPENCODE_FIXTURES.rglob("*.jsonl"))
        self.assertTrue(found, "no recorded fixtures")
        return found

    def scenario(self, name):
        lines = _load_fixture(OPENCODE_FIXTURES / opencode_wire.RECORDED_VERSION
                              / f"{name}.jsonl")
        return lines[0], lines[1:], opencode_wire.replay(lines)

    @staticmethod
    def requests(lines, tail):
        return [ln["http"] for ln in lines if ln.get("dir") == "out"
                and "http" in ln and ln["http"]["path"].endswith(tail)]

    @staticmethod
    def replies(got):
        return [e["data"]["text"] for e in _of(got, "message.completed")
                if e["data"]["kind"] == "assistant"]

    def test_every_fixture_replays_to_its_golden_events(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                golden = path.with_name(path.name[:-6] + ".events.json")
                self.assertTrue(golden.exists(), f"{golden.name} missing")
                got = opencode_wire.replay(_load_fixture(path))
                self.assertEqual(got, json.loads(golden.read_text()))
                for event in got:
                    self.assertEqual(events.validate(event), [], event)

    def test_headers_say_how_each_was_recorded(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                header = _load_fixture(path)[0]
                self.assertEqual(header["fixture"], 1)
                self.assertEqual(header["adapter"], "opencode_serve")
                self.assertEqual(header["version"], path.parent.name)
                self.assertIs(header["synthetic"], False)
                self.assertIsNone(header["error"])
                self.assertIn("OPENCODE_SERVER_PASSWORD", header["env_set"])
                self.assertIn("XDG_CONFIG_HOME", header["env_set"])

    def test_golden_events_keep_the_contract_order(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                got = opencode_wire.replay(_load_fixture(path))
                self.assertEqual([e["type"] for e in got[:2]],
                                 ["session.caps", "session.state"])
                self.assertEqual(got[-1]["data"]["state"], "closed")
                _streams_close(self, got)
                starts = [i for i, e in enumerate(got)
                          if e["type"] == "session.caps"] + [len(got)]
                for a, b in zip(starts, starts[1:]):
                    _contract_order(self, got[a:b])

    def test_only_events_the_module_reads_are_kept_whole(self):
        for path in self.fixtures():
            for line in _load_fixture(path)[1:]:
                event = line.get("sse")
                if event and event["type"] not in opencode_wire.READ_EVENTS:
                    self.assertLessEqual(set(event.get("data") or {}),
                                         {"sessionID"}, event)

    def test_hello(self):
        _header, lines, got = self.scenario("hello")
        self.assertEqual(got[0]["data"]["caps"],
                         opencode_wire.PROVEN.as_dict())
        self.assertEqual(got[1]["data"]["harness_session_id"], "ses_01")
        self.assertEqual(self.replies(got), ["hello there"])
        usage = _of(got, "usage.updated")[-1]["data"]
        self.assertGreater(usage["input_tokens"], 0)
        self.assertGreater(usage["cost_usd"], 0)
        self.assertEqual(_of(got, "turn.ended")[0]["data"]["status"],
                         "completed")
        info = next(ln["http"] for ln in lines if ln.get("dir") == "in"
                    and "http" in ln)
        self.assertEqual(info["body"], {"version": "2.3.2"})
        self.assertEqual(lines[-1], {"proc": 0, "exit": 143},
                         "SIGTERM is how the server stops")

    def test_approval_allow_deny_and_an_edit(self):
        header, lines, got = self.scenario("approval")
        asked = _of(got, "approval.requested")
        self.assertEqual([e["data"]["kind"] for e in asked],
                         ["command", "command", "tool"])
        self.assertEqual(asked[0]["data"]["prompt"], "touch allowed.txt")
        self.assertEqual(asked[2]["data"]["prompt"], "edit: written.txt")
        for request in asked:
            self.assertEqual([o["id"] for o in request["data"]["options"]],
                             ["allow", "deny"])
            self.assertTrue(request["data"]["call_id"].startswith("call_"))
        self.assertEqual([e["data"].get("decision") for e in
                          _of(got, "approval.resolved")],
                         ["allow", "deny", "allow"])
        done = _of(got, "tool.completed")
        self.assertEqual([e["data"]["status"] for e in done],
                         ["ok", "cancelled", "ok"])
        self.assertEqual(done[2]["data"]["file_edits"], [
            {"path": "written.txt", "kind": "add",
             "diff": "@@ -0,0 +1,1 @@\n+yes\n"}])
        # A deny with no message ends the turn there.
        self.assertEqual([e["data"]["status"] for e in
                          _of(got, "turn.ended")],
                         ["completed", "interrupted", "completed"])
        self.assertEqual(header["notes"]["files"], [
            "allowed.txt", "hello.py", "notes.txt", "written.txt"])
        sent = [r["body"] for r in self.requests(lines, "/reply")]
        self.assertEqual(sent, [{"decision": "once"}, {"decision": "reject"},
                                {"decision": "once"}])

    def test_a_deny_with_a_message_lets_the_turn_go_on(self):
        header, _lines, got = self.scenario("deny_message")
        resolved = _of(got, "approval.resolved")[0]["data"]
        self.assertEqual((resolved["decision"], resolved["text"]),
                         ("deny", "Not that file. Do not run anything."))
        done = _of(got, "tool.completed")[0]["data"]
        self.assertEqual((done["status"], done["output"]),
                         ("error", "Not that file. Do not run anything."))
        self.assertEqual(self.replies(got), ["denied"])
        self.assertEqual(_of(got, "turn.ended")[0]["data"]["status"],
                         "completed")
        self.assertNotIn("denied.txt", header["notes"]["files"])

    def test_always_saves_a_rule_for_the_project(self):
        # Why "always" is never offered: the rule outlives the session.
        header, lines, got = self.scenario("always")
        notes = header["notes"]
        self.assertEqual((notes["approvals_first_turn"],
                          notes["approvals_second_turn"]), (1, 0))
        self.assertEqual(notes["saved_rules"],
                         [{"action": "shell", "resource": "touch *"}])
        self.assertIs(notes["saved_rules_name_a_project"], True)
        self.assertEqual(self.requests(lines, "/reply")[0]["body"],
                         {"decision": "always"})
        for request in _of(got, "approval.requested"):
            self.assertNotIn("always",
                             [o["id"] for o in request["data"]["options"]])

    def test_interrupt(self):
        _header, lines, got = self.scenario("interrupt")
        self.assertEqual([e["data"]["status"] for e in
                          _of(got, "turn.ended")], ["interrupted"])
        partial = self.replies(got)
        self.assertEqual(len(partial), 1, "the cut reply is completed")
        self.assertTrue(partial[0].startswith("1"))
        answers = [ln["http"]["body"] for ln in lines if ln.get("dir") == "in"
                   and "interrupted" in (ln.get("http", {}).get("body")
                                         or {})]
        self.assertEqual(answers, [{"interrupted": True},
                                   {"interrupted": False}],
                         "an interrupt with no turn is answered, not refused")

    def test_an_interrupt_before_the_execution_starts_still_stops_it(self):
        header, _lines, got = self.scenario("interrupt_early")
        self.assertEqual(header["notes"]["answer"], {"interrupted": True})
        self.assertEqual(header["notes"]["deltas"], 0)
        self.assertEqual([e["data"]["status"] for e in
                          _of(got, "turn.ended")],
                         ["completed", "interrupted"])

    def test_steer_joins_the_turn_after_the_reply_in_flight(self):
        header, lines, got = self.scenario("steer")
        self.assertEqual(header["notes"]["executions_ended"], 1)
        self.assertEqual(len(_of(got, "turn.started")), 1)
        done = [(e["data"]["kind"], e["data"]["id"]) for e in
                _of(got, "message.completed")]
        self.assertEqual([k for k, _i in done],
                         ["user", "assistant", "user", "assistant"])
        self.assertEqual(done[2][1], done[0][1] + "2")
        texts = [e["data"]["text"] for e in _of(got, "message.completed")]
        # opencode does not cut the reply: all sixty numbers came.
        self.assertIn("60", texts[1])
        self.assertEqual(texts[3], "steered")
        steer = self.requests(lines, "/prompt")[1]
        self.assertEqual(steer["body"]["delivery"], "steer")

    def test_steer_does_not_stop_a_running_command(self):
        header, _lines, got = self.scenario("steer_tool")
        notes = header["notes"]
        self.assertGreater(notes["seconds_from_command_to_turn_end"], 20)
        self.assertIs(notes["late_file_at_turn_end"], True)
        types = [(e["type"], e["data"].get("kind")) for e in got]
        self.assertLess(types.index(("tool.completed", None)),
                        [i for i, t in enumerate(types)
                         if t == ("message.completed", "user")][1],
                        "the steer lands once the command is done")
        self.assertEqual(_of(got, "tool.completed")[0]["data"]["status"],
                         "ok")

    def test_a_steer_waits_behind_an_open_approval_then_gets_its_own_turn(
            self):
        header, _lines, got = self.scenario("steer_approval")
        notes = header["notes"]
        self.assertIs(notes["turn_ended_without_an_answer"], False)
        self.assertIs(notes["asked_file_exists"], False)
        self.assertEqual(notes["executions_ended"], 2)
        started = _of(got, "turn.started")
        self.assertEqual(len(started), 2)
        self.assertEqual(started[1]["data"]["text"],
                         "Do not run that. Reply only with the word: steered")
        users = [e for e in _of(got, "message.completed")
                 if e["data"]["kind"] == "user"]
        self.assertEqual([e["turn_id"] for e in users],
                         [started[0]["turn_id"], started[1]["turn_id"]])
        self.assertEqual([e["data"]["status"] for e in
                          _of(got, "turn.ended")],
                         ["interrupted", "completed"])
        self.assertEqual(self.replies(got), ["steered"])

    def test_a_steer_left_by_an_interrupt_rides_the_next_prompt(self):
        header, _lines, got = self.scenario("steer_interrupt")
        self.assertIs(header["notes"]["steer_ran_after_the_interrupt"],
                      False)
        resolved = _of(got, "approval.resolved")[0]["data"]
        self.assertEqual(resolved["source"], "turn_ended")
        second = _of(got, "turn.started")[1]
        users = [e["data"]["text"] for e in _of(got, "message.completed")
                 if e["data"]["kind"] == "user"
                 and e["turn_id"] == second["turn_id"]]
        self.assertEqual(users, [
            "Reply only with the word: next",
            "Do not run that. Reply only with the word: steered"])

    def test_resume_keeps_the_id_and_fork_gets_a_new_one(self):
        _header, lines, got = self.scenario("resume")
        sids = [e["data"]["harness_session_id"]
                for e in _of(got, "session.state")
                if e["data"].get("harness_session_id")]
        self.assertEqual(sids, ["ses_01", "ses_01", "ses_02"])
        opened = [(ln["http"]["method"], ln["http"]["path"]) for ln in lines
                  if ln.get("dir") == "out" and "http" in ln
                  and "/prompt" not in ln["http"]["path"]
                  and "/api/session" in ln["http"]["path"]]
        self.assertEqual(opened, [("POST", "/api/session"),
                                  ("GET", "/api/session/ses_01"),
                                  ("POST", "/api/session/ses_01/fork")])
        self.assertEqual([r.lower() for r in self.replies(got)],
                         ["ok", "papaya", "papaya"])
        ids = [e["turn_id"] for e in _of(got, "turn.started")]
        self.assertEqual(len(set(ids)), 3)

    def test_rollback_drops_the_turn_from_the_conversation(self):
        _header, lines, got = self.scenario("rollback")
        staged = self.requests(lines, "/revert/stage")
        self.assertEqual(len(staged), 1)
        second = [ln["http"]["body"]["data"]["id"] for ln in lines
                  if ln.get("dir") == "in" and "http" in ln
                  and (ln["http"].get("body") or {}).get("data", {}).get(
                      "type") == "user"][1]
        self.assertEqual(staged[0]["body"],
                         {"messageID": second, "files": False})
        self.assertEqual(len(self.requests(lines, "/revert/commit")), 1)
        self.assertEqual(self.replies(got)[-1].lower(), "papaya",
                         "walrus was rolled back")

    def test_model_and_effort_change_between_turns(self):
        header, lines, _got = self.scenario("options")
        ran = header["notes"]["models_that_ran"]
        self.assertEqual([(m["id"], m["variant"]) for m in ran],
                         [("gpt-5.4-nano", "low"), ("gpt-5.4-nano", "medium"),
                          ("gpt-5.4-mini", "low")])
        self.assertEqual(len(self.requests(lines, "/model")), 2)

    def test_plan_agent_writes_nothing(self):
        header, lines, _got = self.scenario("plan_mode")
        notes = header["notes"]
        self.assertIs(notes["file_while_planning"], False)
        self.assertIs(notes["file_after_build"], True)
        self.assertEqual(set(notes["agents_that_ran"]), {"plan", "build"})
        self.assertEqual([r["body"] for r in self.requests(lines, "/agent")],
                         [{"agent": "plan"}, {"agent": "build"}])

    def test_attachments(self):
        _header, lines, got = self.scenario("attach")
        prompts = self.requests(lines, "/prompt")
        self.assertEqual(prompts[0]["body"]["files"],
                         [{"uri": "file:///work/square.png",
                           "name": "square.png"}])
        replies = self.replies(got)
        self.assertEqual(replies[0].lower().strip("."), "red")
        self.assertEqual(replies[1], "first line of the notes")

    def test_a_failed_turn(self):
        header, _lines, got = self.scenario("error")
        self.assertIs(header["notes"]["bad_model_accepted"], True,
                      "a wrong model is only found out by the turn")
        self.assertEqual([e["data"]["status"] for e in
                          _of(got, "turn.ended")], ["failed", "completed"])
        error = _of(got, "error")[0]["data"]
        self.assertEqual(error["code"], "provider.no-route")
        self.assertIn("no-such-model", error["message"])
        self.assertEqual(_of(got, "turn.ended")[0]["data"]["reason"],
                         error["message"])

    def test_a_caller_may_choose_a_ses_id_only(self):
        header, _lines, _got = self.scenario("own_id")
        self.assertIs(header["notes"]["kept_our_id"], True)
        self.assertEqual(header["notes"]["uuid_id_status"], 400)

    def test_no_personal_data(self):
        for path in sorted(OPENCODE_FIXTURES.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertNotIn("/Users/", text)
                self.assertNotIn("/var/folders/", text)
                self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@(?!example\.com)"
                                          r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
                self.assertNotRegex(text, r"\bsk-[A-Za-z0-9_-]{8,}")
                for key in ('"native"', '"projectID"', '"hostname"',
                            '"subpath"', "EncryptedContent", ".local"):
                    self.assertNotIn(key, text)
                self.assertNotRegex(
                    text, r"\b(ses|msg|per|call)_[A-Za-z0-9]{12,}")


class TestOpencodeWire(unittest.TestCase):
    def test_ids_and_the_listening_line(self):
        self.assertTrue(opencode_wire.is_session_id(OC_SESSION))
        for bad in ("", "11111111-2222-4333-8444-555555555555", "ses_",
                    "ses_a/b", None, 7):
            self.assertFalse(opencode_wire.is_session_id(bad), bad)
        self.assertEqual(opencode_wire.listening_url(
            "opencode server listening on http://127.0.0.1:4096\n"),
            "http://127.0.0.1:4096")
        self.assertEqual(opencode_wire.listening_url("Warning: no"), "")
        self.assertEqual(opencode_wire.listening_url(None), "")

    def test_basic_auth(self):
        import base64

        header = opencode_wire.basic_auth("pw")
        self.assertEqual(base64.b64decode(header.split()[1]), b"opencode:pw")
        password = opencode_wire.new_password()
        self.assertRegex(password, r"^sk-[0-9a-f]{48}$")
        self.assertNotEqual(password, opencode_wire.new_password())
        for said in (password, f"echo {password}", f"x:{password}@host"):
            self.assertNotIn(password, core.redact(said),
                             "shaped so the redactor masks it")

    def test_model_refs(self):
        self.assertEqual(opencode_wire.model_ref("openai/gpt-x", "low"), {
            "providerID": "openai", "id": "gpt-x", "variant": "low"})
        self.assertEqual(opencode_wire.model_ref("a/b/c"),
                         {"providerID": "a", "id": "b/c"})
        for bad in ("gpt-x", "/x", "x/", ""):
            self.assertIsNone(opencode_wire.model_ref(bad))
        self.assertEqual(opencode_wire.model_name(
            {"providerID": "openai", "id": "gpt-x"}), "openai/gpt-x")
        self.assertEqual(opencode_wire.model_name({"id": "gpt-x"}), "")
        self.assertEqual(opencode_wire.model_name(None), "")

    def test_requests(self):
        w = opencode_wire
        self.assertEqual(w.info(), ("GET", "/api/info", None))
        self.assertEqual(w.events_stream().path, "/api/event")
        self.assertEqual(w.create_session("/w"), (
            "POST", "/api/session", {"location": {"directory": "/w"}}))
        made = w.create_session("/w", model="a/b", effort="high",
                                session_id=OC_SESSION).body
        self.assertEqual(made["model"],
                         {"providerID": "a", "id": "b", "variant": "high"})
        self.assertEqual(made["id"], OC_SESSION)
        self.assertNotIn("id", w.create_session(
            "/w", session_id="the-hubs-uuid").body)
        self.assertEqual(w.get_session("ses_1"),
                         ("GET", "/api/session/ses_1", None))
        self.assertEqual(w.fork_session("ses_1"),
                         ("POST", "/api/session/ses_1/fork", {}))
        self.assertEqual(w.prompt("ses_1", "hi"), (
            "POST", "/api/session/ses_1/prompt", {"text": "hi"}))
        steer = w.prompt("ses_1", "hi", attachments=["/w/a b.png"],
                         steer=True).body
        self.assertEqual(steer, {
            "text": "hi", "delivery": "steer",
            "files": [{"uri": "file:///w/a%20b.png", "name": "a b.png"}]})
        self.assertEqual(w.interrupt("ses_1"),
                         ("POST", "/api/session/ses_1/interrupt", None))
        self.assertEqual(w.permission_reply("ses_1", "per_1", "once"), (
            "POST", "/api/session/ses_1/permission/per_1/reply",
            {"decision": "once"}))
        self.assertEqual(
            w.permission_reply("ses_1", "per_1", "reject", "no").body,
            {"decision": "reject", "message": "no"})
        self.assertEqual(w.form_reply("ses_1", "frm_1", {"k": "v"}), (
            "POST", "/api/session/ses_1/form/frm_1/reply",
            {"answer": {"k": "v"}}))
        self.assertEqual(w.form_cancel("ses_1", "frm_1"),
                         ("DELETE", "/api/session/ses_1/form/frm_1", None))
        self.assertEqual(w.select_model("ses_1", {"id": "m"}).body,
                         {"model": {"id": "m"}})
        self.assertEqual(w.select_agent("ses_1", "plan").body,
                         {"agent": "plan"})
        self.assertEqual(w.revert_stage("ses_1", "msg_1").body,
                         {"messageID": "msg_1", "files": False})
        self.assertEqual(w.revert_commit("ses_1").path,
                         "/api/session/ses_1/revert/commit")
        self.assertEqual(w.revert_clear("ses_1"),
                         ("DELETE", "/api/session/ses_1/revert", None))

    def test_an_id_cannot_name_another_route(self):
        path = opencode_wire.permission_reply(
            "ses_1", "../../interrupt?x", "once").path
        self.assertEqual(
            path, "/api/session/ses_1/permission/..%2F..%2Finterrupt%3Fx/reply")

    def test_error_text(self):
        self.assertEqual(opencode_wire.error_text(
            {"_tag": "BadRequest", "message": "no"}), "no")
        self.assertEqual(opencode_wire.error_text({"_tag": "Gone"}), "Gone")
        self.assertEqual(opencode_wire.error_text(None), "")
        self.assertEqual(opencode_wire.error_tag({"_tag": "Gone"}), "Gone")
        self.assertEqual(opencode_wire.error_tag("x"), "")

    def test_sse_parser(self):
        parser = opencode_wire.SseParser()
        self.assertIsNone(parser.feed(b"event: message\n"))
        self.assertIsNone(parser.feed(b'data: {"type": "a",\n'))
        self.assertIsNone(parser.feed(b'data: "data": {}}\r\n'))
        self.assertEqual(parser.feed(b"\n"), {"type": "a", "data": {}})
        self.assertIsNone(parser.feed("\n"), "a blank line with no data")
        self.assertIsNone(parser.feed(": a comment\n"))
        parser.feed("data: not json\n")
        self.assertIsNone(parser.feed("\n"))
        parser.feed("data: [1]\n")
        self.assertIsNone(parser.feed("\n"), "an event is an object")

    def test_file_edits(self):
        edits = opencode_wire.file_edits({"files": [
            {"file": "a.txt", "status": "added",
             "patch": "Index: a\n===\n--- a\n+++ a\n@@ -0,0 +1 @@\n+x\n"},
            {"file": "b.txt", "status": "deleted", "patch": ""},
            {"file": "c.txt", "status": "renamed"},
            {"patch": "no file"}, "junk"]})
        self.assertEqual(edits, [
            {"path": "a.txt", "kind": "add", "diff": "@@ -0,0 +1 @@\n+x\n"},
            {"path": "b.txt", "kind": "delete"},
            {"path": "c.txt", "kind": "modify"}])
        self.assertIsNone(opencode_wire.file_edits({"files": "x"}))
        self.assertIsNone(opencode_wire.file_edits(None))
        self.assertIsNone(opencode_wire.file_edits({"files": []}))
        secret = opencode_wire.file_edits({"files": [{
            "file": ".env", "patch": "@@ -0,0 +1 @@\n+API_KEY=" + OC_KEY}]})
        self.assertNotIn("sk-ant", json.dumps(secret))

    def test_the_pre_keeper_helpers(self):
        w = opencode_wire
        self.assertEqual(w.legacy_prompt("s 1", "hi", variant="high"), (
            "POST", "/session/s%201/prompt_async",
            {"parts": [{"type": "text", "text": "hi"}], "variant": "high"}))
        self.assertEqual(w.legacy_abort("s1"),
                         ("POST", "/session/s1/abort", {}))
        self.assertEqual(w.legacy_permission_reply("s1", "p/1", "once"), (
            "POST", "/session/s1/permissions/p%2F1", {"response": "once"}))
        self.assertEqual(w.legacy_permission_reply_old("p1", "once"), (
            "POST", "/permission/p1/reply", {"reply": "once"}))
        self.assertEqual(w.legacy_event(
            b'{"type": "x", "properties": {"a": 1}}'), ("x", {"a": 1}))
        self.assertEqual(w.legacy_event(
            b'{"type": "x", "payload": {"a": 1}}'), ("x", {"a": 1}))
        for bad in (b"not json", b"[1]", b'{"type": "x"}', b"\xff"):
            self.assertIsNone(w.legacy_event(bad))
        self.assertTrue(w.legacy_is_live("message.part.updated"))
        self.assertTrue(w.legacy_is_live("session.idle"))
        self.assertFalse(w.legacy_is_live("permission.asked"))
        self.assertIsNone(w.legacy_permission("session.idle", {}, {}))
        self.assertEqual(w.legacy_permission(
            "permission.asked", {"id": "p1", "sessionID": "s1",
                                 "title": "run", "patterns": ["a", "b"]}, {}),
            ("asked", "p1", "s1", "run\n\n`a`\n`b`"))
        self.assertEqual(w.legacy_permission(
            "permission.replied", {"permissionID": "p1"}, {"p1": "s1"}),
            ("resolved", "p1", "s1", ""))


def _oc(etype, **data):
    return {"type": etype, "data": {"sessionID": OC_SESSION, **data}}


class TestOpencodeServeState(unittest.TestCase):
    def state(self, caps: Caps = ALL_CAPS):
        clock = iter(range(1000, 100000))
        state = opencode_wire.ServeState(
            "opencode:test", clock=lambda: float(next(clock)), caps=caps,
            run_id="r1")
        state.start_events(caps.narrowed(opencode_wire.PROVEN), OC_SESSION)
        return state

    def turn(self, state, text="hi", token=1, message="msg_u1", **kw):
        state.expect_turn(token, text, **kw)
        out = state.accepted(token, {"id": message})
        out += state.feed(_oc("session.execution.started")).events
        out += state.feed(_oc("session.inbox.delivered",
                              inboxID=message)).events
        return out

    def types(self, evs):
        return [e["type"] for e in evs]

    def test_a_turn(self):
        state = self.state()
        self.assertFalse(state.busy)
        state.expect_turn(1, "hi", command_id="c1", device="phone",
                          attachments=["/w/a.txt"])
        self.assertTrue(state.busy, "busy from the moment it is posted")
        out = state.accepted(1, {"id": "msg_u1"})
        self.assertEqual(self.types(out), [
            "turn.started", "session.state", "message.completed"])
        self.assertEqual(out[0]["command_id"], "c1")
        self.assertEqual(out[0]["data"], {"by": "phone", "text": "hi"})
        self.assertEqual(out[2]["data"]["attachments"], ["/w/a.txt"])
        turn_id = out[0]["turn_id"]
        self.assertEqual(turn_id, "t-r1-1")
        self.assertEqual(state.accepted_turns, {1: turn_id})
        self.assertEqual(state.turn_messages, {turn_id: "msg_u1"})
        self.assertEqual(state.feed(_oc("session.execution.started")).events,
                         [])
        self.assertEqual(state.feed(_oc("session.inbox.delivered",
                                        inboxID="msg_u1")).events, [])
        state.feed(_oc("session.step.started", agent="build",
                       model={"providerID": "p", "id": "m"},
                       assistantMessageID="msg_a1"))
        self.assertEqual((state.agent, state.model["id"]), ("build", "m"))
        out = state.feed(_oc("session.text.delta", assistantMessageID="msg_a1",
                             ordinal=0, delta="hello there, ")).events
        out += state.feed(_oc("session.text.delta",
                              assistantMessageID="msg_a1", ordinal=0,
                              delta="friend")).events
        out += state.feed(_oc("session.text.ended",
                              assistantMessageID="msg_a1", ordinal=0,
                              text="hello there, friend")).events
        self.assertEqual(self.types(out)[-1], "message.completed")
        self.assertEqual(out[-1]["data"]["id"], "msg_a1:0")
        _streams_close(self, out)
        state.feed(_oc("session.step.failed", assistantMessageID="msg_a0",
                       error={"type": "aborted", "message": "cut"},
                       tokens={"input": 1, "output": 1}))
        cut = state.feed(_oc("session.usage.updated", cost=0.25)).events
        self.assertEqual(cut[0]["data"], {
            "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.25},
            "a step that failed still used tokens")
        state.feed(_oc("session.step.ended", assistantMessageID="msg_a1",
                       tokens={"input": 9, "output": 2, "cache": {
                           "read": 4, "write": 1}}))
        usage = state.feed(_oc("session.usage.updated", cost=0.5,
                               tokens={"input": 999})).events
        self.assertEqual(usage[0]["data"], {
            "input_tokens": 9, "output_tokens": 2, "cache_read_tokens": 4,
            "cache_write_tokens": 1, "cost_usd": 0.5})
        out = state.feed(_oc("session.execution.succeeded")).events
        self.assertEqual(self.types(out), ["turn.ended", "session.state"])
        self.assertEqual(out[0]["data"]["status"], "completed")
        self.assertFalse(state.busy)
        # Usage outside a turn (the title's cost): no turn id.
        late = state.feed(_oc("session.usage.updated", cost=0.6)).events
        self.assertNotIn("turn_id", late[0])

    def test_usage_before_any_step_is_the_cost_alone(self):
        state = self.state()
        out = state.feed(_oc("session.usage.updated", cost=0.1,
                             tokens={"input": 5})).events
        self.assertEqual(out[0]["data"], {"cost_usd": 0.1})
        for bad in (True, -1, float("nan"), float("inf"), "1"):
            self.assertEqual(state.feed(_oc("session.usage.updated",
                                            cost=bad)).events, [])

    def test_events_that_overtake_the_prompts_answer(self):
        state = self.state()
        state.expect_turn(7, "hi", command_id="c1", device="phone")
        out = state.feed(_oc("session.execution.started")).events
        self.assertEqual(self.types(out), [
            "turn.started", "session.state", "message.completed"])
        self.assertEqual(out[0]["command_id"], "c1")
        turn_id = out[0]["turn_id"]
        self.assertEqual(state.accepted_turns, {7: turn_id})
        # The answer, late: nothing more to say, the message id is kept.
        self.assertEqual(state.accepted(7, {"id": "msg_u1"}), [])
        self.assertEqual(state.turn_messages, {turn_id: "msg_u1"})

    def test_a_turn_nobody_here_started(self):
        state = self.state()
        out = state.feed(_oc("session.execution.started")).events
        self.assertEqual(self.types(out), ["turn.started", "session.state"])
        self.assertEqual(out[0]["data"], {})
        out = state.feed(_oc("session.execution.succeeded")).events
        self.assertEqual(out[0]["data"]["status"], "completed")

    def test_a_refused_prompt_is_forgotten(self):
        state = self.state()
        state.expect_turn(1, "hi")
        state.forget(1)
        self.assertFalse(state.busy)
        self.assertEqual(state.accepted(1, {"id": "msg_u1"}), [])
        state.forget("never-posted")

    def test_a_steer_lands_when_it_is_delivered(self):
        state = self.state()
        first = self.turn(state)[0]["turn_id"]
        state.feed(_oc("session.text.delta", assistantMessageID="msg_a1",
                       ordinal=0, delta="1\n2\n"))
        state.expect_steer(2, "stop", command_id="c2", device="phone")
        self.assertEqual(state.accepted(2, {"id": "msg_u2"}), [],
                         "in the inbox, not in the turn yet")
        self.assertEqual(state.accepted_turns[2], first)
        out = state.feed(_oc("session.inbox.delivered",
                             inboxID="msg_u2")).events
        self.assertEqual([(e["type"], e["data"]["kind"]) for e in out], [
            ("message.completed", "assistant"),
            ("message.completed", "user")],
            "the reply in flight is completed first")
        self.assertEqual(out[0]["data"]["text"], "1\n2\n")
        self.assertEqual(out[1]["data"]["id"], f"{first}:user2")
        self.assertEqual(out[1]["command_id"], "c2")
        self.assertEqual(state.turn_messages, {first: "msg_u1"},
                         "a rollback goes to the turn's first message")

    def test_a_steer_delivered_before_its_answer_is_read(self):
        state = self.state()
        first = self.turn(state)[0]["turn_id"]
        state.expect_steer(2, "stop", command_id="c2")
        out = state.feed(_oc("session.inbox.delivered",
                             inboxID="msg_u2")).events
        self.assertEqual([e["data"]["text"] for e in out], ["stop"])
        self.assertEqual(out[0]["command_id"], "c2")
        # The whole turn can pass before the answer is read.
        state.feed(_oc("session.execution.succeeded"))
        self.assertEqual(state.accepted(2, {"id": "msg_u2"}), [])
        self.assertEqual(state.accepted_turns[2], first)
        self.assertIsNone(state.turn, "no turn is opened for it")
        self.assertEqual(state.turn_messages, {first: "msg_u1"})

    def test_a_steer_taken_after_the_turn_ended_waits_for_its_delivery(self):
        # After an interrupt the server keeps a steer in its inbox until
        # the next prompt: a turn opened for it here would never end.
        state = self.state()
        self.turn(state)
        state.expect_steer(2, "stop")
        state.feed(_oc("session.execution.interrupted", reason="user"))
        self.assertEqual(state.accepted(2, {"id": "msg_u2"}), [])
        self.assertEqual(state.accepted_turns[2], "")
        self.assertIsNone(state.turn)
        self.assertFalse(state.busy)
        out = self.turn(state, "next", token=3, message="msg_u3")
        out += state.feed(_oc("session.inbox.delivered",
                              inboxID="msg_u2")).events
        users = [e["data"]["text"] for e in out
                 if e["type"] == "message.completed"]
        self.assertEqual(users, ["next", "stop"])

    def test_a_delivery_nobody_posted(self):
        state = self.state()
        self.turn(state)
        for inbox in ("msg_fork_notice", "", None):
            self.assertEqual(state.feed(_oc("session.inbox.delivered",
                                            inboxID=inbox)).events, [])

    def test_a_steer_the_turn_never_reached_opens_the_next(self):
        state = self.state()
        first = self.turn(state)[0]["turn_id"]
        state.expect_steer(2, "stop", command_id="c2", device="phone")
        state.accepted(2, {"id": "msg_u2"})
        out = state.feed(_oc("session.execution.interrupted",
                             reason="shutdown")).events
        self.assertEqual(self.types(out), ["turn.ended", "session.state"])
        out = state.feed(_oc("session.execution.started")).events
        self.assertEqual(self.types(out), ["turn.started", "session.state"])
        self.assertEqual(out[0]["data"], {"by": "phone", "text": "stop"})
        self.assertEqual(out[0]["command_id"], "c2")
        second = out[0]["turn_id"]
        self.assertNotEqual(second, first)
        out = state.feed(_oc("session.inbox.delivered",
                             inboxID="msg_u2")).events
        self.assertEqual(out[0]["data"]["id"], f"{second}:user")
        self.assertEqual(state.turn_messages[second], "msg_u2")

    def test_a_steer_with_no_turn_and_no_execution_before_it(self):
        state = self.state()
        state.expect_steer(1, "go", device="phone")
        self.assertFalse(state.busy, "a steer is no turn of its own yet")
        self.assertEqual(state.accepted(1, {"id": "msg_u1"}), [])
        # Delivered with no ``execution.started`` seen first.
        out = state.feed(_oc("session.inbox.delivered",
                             inboxID="msg_u1")).events
        self.assertEqual(self.types(out), [
            "turn.started", "session.state", "message.completed"])
        self.assertEqual(out[0]["data"], {"by": "phone", "text": "go"})

    def test_a_steer_the_server_gave_no_id(self):
        state = self.state()
        first = self.turn(state)[0]["turn_id"]
        state.expect_steer(2, "stop")
        out = state.accepted(2, {})
        self.assertEqual([e["data"]["text"] for e in out], ["stop"],
                         "no delivery to wait for: said at once")
        self.assertEqual(state.accepted_turns[2], first)
        state.feed(_oc("session.execution.succeeded"))
        state.expect_steer(3, "late")
        self.assertEqual(state.accepted(3, None), [])
        self.assertEqual(state.accepted_turns[3], "")

    def test_reasoning_and_hidden_reasoning(self):
        state = self.state()
        self.turn(state)
        out = state.feed(_oc("session.reasoning.delta",
                             assistantMessageID="msg_a1", ordinal=0,
                             delta="thinking hard about it")).events
        out += state.feed(_oc("session.reasoning.ended",
                              assistantMessageID="msg_a1", ordinal=0,
                              text="thinking hard about it")).events
        self.assertEqual(out[-1]["type"], "thinking.completed")
        self.assertEqual(out[-1]["data"]["id"], "msg_a1:r0")
        self.assertEqual(state.feed(_oc(
            "session.reasoning.ended", assistantMessageID="msg_a1",
            ordinal=1, text="")).events, [], "hidden reasoning shows nothing")
        self.assertEqual(state.feed(_oc(
            "session.text.ended", assistantMessageID="msg_a1", ordinal=2,
            text="")).events, [])
        self.assertEqual(state.feed(_oc(
            "session.text.ended", ordinal=0, text="x")).events, [],
            "no message id, no stream")

    def test_text_outside_a_turn_is_dropped(self):
        state = self.state()
        for etype in ("session.text.delta", "session.reasoning.delta",
                      "session.text.ended", "session.reasoning.ended"):
            self.assertEqual(state.feed(_oc(
                etype, assistantMessageID="m", ordinal=0, delta="x",
                text="x")).events, [])

    def test_a_tool_call(self):
        state = self.state()
        turn_id = self.turn(state)[0]["turn_id"]
        state.feed(_oc("session.tool.input.started", id="call_1",
                       name="shell"))
        out = state.feed(_oc("session.tool.called", id="call_1",
                             input={"command": "ls -la"})).events
        self.assertEqual(out[0]["type"], "tool.started")
        self.assertEqual(out[0]["data"]["name"], "shell")
        self.assertIn("ls -la", out[0]["data"]["summary"])
        self.assertEqual(state.feed(_oc("session.tool.called", id="call_1",
                                        input={})).events, [],
                         "a call is started once")
        out = state.feed(_oc("session.tool.success", id="call_1", content=[
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"},
            "junk"], metadata={"exit": 0})).events
        self.assertEqual(out[0]["data"], {
            "call_id": "call_1", "status": "ok", "name": "shell",
            "output": "a\nb"})
        self.assertEqual(out[0]["turn_id"], turn_id)
        self.assertEqual(state.feed(_oc("session.tool.success", id="call_1",
                                        content=[])).events, [])
        # No name ahead of the call: the call's own, or "tool".
        out = state.feed(_oc("session.tool.called", id="call_2",
                             name="read", input={})).events
        self.assertEqual(out[0]["data"]["name"], "read")
        out = state.feed(_oc("session.tool.called", id="call_3")).events
        self.assertEqual(out[0]["data"]["name"], "tool")
        self.assertEqual(state.feed(_oc("session.tool.called")).events, [])
        out = state.feed(_oc("session.tool.failed", id="call_2", error={
            "type": "aborted", "message": "declined"})).events
        self.assertEqual((out[0]["data"]["status"], out[0]["data"]["output"]),
                         ("cancelled", "declined"))
        out = state.feed(_oc("session.tool.failed", id="call_3",
                             error="boom")).events
        self.assertEqual(out[0]["data"]["status"], "error")

    def test_a_tool_that_outlives_its_turn(self):
        state = self.state()
        self.turn(state)
        state.feed(_oc("session.tool.called", id="call_1", name="shell",
                       input={"command": "sleep 9"}))
        state.feed(_oc("session.execution.interrupted", reason="user"))
        self.turn(state, token=2, message="msg_u2")
        out = state.feed(_oc("session.tool.success", id="call_1",
                             content=[])).events
        self.assertEqual(out[0]["type"], "tool.completed")
        self.assertNotIn("turn_id", out[0])

    def test_secrets_never_leave(self):
        state = self.state()
        self.turn(state)
        out = state.feed(_oc("session.text.delta", assistantMessageID="m",
                             ordinal=0, delta="key " + OC_KEY[:20])).events
        out += state.feed(_oc("session.text.delta", assistantMessageID="m",
                              ordinal=0, delta=OC_KEY[20:] + " ok")).events
        out += state.feed(_oc("session.text.ended", assistantMessageID="m",
                              ordinal=0, text=f"key {OC_KEY} ok")).events
        state.feed(_oc("session.tool.input.started", id="c1", name="shell"))
        out += state.feed(_oc("session.tool.called", id="c1", input={
            "command": f"curl -H 'x-api-key: {OC_KEY}' x"})).events
        out += state.feed(_oc("permission.asked", id="per_1", action="shell",
                              resources=[f"echo {OC_KEY}"],
                              message=f"uses {OC_KEY}")).events
        out += state.feed(_oc("session.tool.success", id="c1", content=[
            {"type": "text", "text": OC_KEY}])).events
        out += state.feed(_oc("session.step.failed", error={
            "type": "api", "message": f"bad key {OC_KEY}"})).events
        self.assertNotIn("sk-ant", json.dumps(out))
        _streams_close(self, out)

    def test_a_permission(self):
        state = self.state()
        turn_id = self.turn(state)[0]["turn_id"]
        out = state.feed(_oc("permission.asked", id="per_1", action="shell",
                             resources=["touch a.txt", 7],
                             source={"type": "tool", "id": "call_1"})).events
        self.assertEqual(self.types(out),
                         ["approval.requested", "session.state"])
        self.assertEqual(out[0]["data"], {
            "request_id": "per_1", "kind": "command", "prompt": "touch a.txt",
            "options": [{"id": "allow", "label": "Allow", "kind": "allow"},
                        {"id": "deny", "label": "Deny", "kind": "deny"}],
            "call_id": "call_1"})
        self.assertEqual(out[1]["data"]["state"], "waiting")
        self.assertEqual(state.feed(_oc("permission.asked", id="per_1",
                                        action="shell")).events, [],
                         "asked once")
        self.assertEqual(state.feed(_oc("permission.asked")).events, [])
        request, out = state.answer(OC_SESSION, "per_1",
                                    Answer(decision="allow"))
        self.assertEqual(request, (
            "POST", f"/api/session/{OC_SESSION}/permission/per_1/reply",
            {"decision": "once"}))
        self.assertEqual(self.types(out),
                         ["approval.resolved", "session.state"])
        self.assertEqual(out[0]["data"], {
            "request_id": "per_1", "source": "user", "decision": "allow"})
        self.assertEqual(out[0]["turn_id"], turn_id)
        # The server's echo of our answer changes nothing.
        self.assertEqual(state.feed(_oc("permission.replied",
                                        requestID="per_1",
                                        reply="once")).events, [])
        with self.assertRaises(UnknownRequest):
            state.answer(OC_SESSION, "per_1", Answer(decision="allow"))

    def test_permission_answers(self):
        cases = [
            (Answer(decision="deny"), {"decision": "reject"}, "deny"),
            (Answer(decision="deny", text="not that"),
             {"decision": "reject", "message": "not that"}, "deny"),
            # A project-wide rule is never saved from a tap.
            (Answer(decision="always"), {"decision": "once"}, "allow"),
            (Answer(option_id="allow"), {"decision": "once"}, "allow"),
            (Answer(option_id="deny"), {"decision": "reject"}, "deny"),
            # Bare text is feedback: the call does not run.
            (Answer(text="use another file"),
             {"decision": "reject", "message": "use another file"}, "deny"),
        ]
        for n, (answer, body, decision) in enumerate(cases):
            with self.subTest(answer=answer):
                state = self.state()
                self.turn(state)
                state.feed(_oc("permission.asked", id=f"per_{n}",
                               action="edit", resources=["a.txt", "b.txt"],
                               message="two files"))
                request, out = state.answer(OC_SESSION, f"per_{n}", answer)
                self.assertEqual(request.body, body)
                self.assertEqual(out[0]["data"]["decision"], decision)

    def test_a_permission_for_something_that_is_no_command(self):
        state = self.state()
        self.turn(state)
        out = state.feed(_oc("permission.asked", id="per_1", action="edit",
                             resources=["a.txt", "b.txt"],
                             message="two files")).events
        self.assertEqual(out[0]["data"]["kind"], "tool")
        self.assertEqual(out[0]["data"]["prompt"],
                         "edit: a.txt\nb.txt\n\ntwo files")
        out = state.feed(_oc("permission.asked", id="per_2")).events
        self.assertEqual(out[0]["data"]["prompt"], "tool: tool")

    def test_a_permission_the_server_settles_itself(self):
        state = self.state()
        self.turn(state)
        state.feed(_oc("permission.asked", id="per_1", action="shell",
                       resources=["x"]))
        out = state.feed(_oc("permission.replied", requestID="per_1",
                             reply="once")).events
        self.assertEqual(self.types(out),
                         ["approval.resolved", "session.state"])
        self.assertEqual(out[0]["data"]["source"], "harness")

    def test_an_interrupt_retires_what_is_open(self):
        state = self.state()
        self.turn(state)
        self.assertEqual(state.interrupt_sent(), [])
        state.feed(_oc("permission.asked", id="per_1", action="shell",
                       resources=["x"]))
        out = state.interrupt_sent()
        self.assertEqual(self.types(out),
                         ["approval.resolved", "session.state"])
        self.assertEqual(out[0]["data"]["source"], "turn_ended")
        out = state.feed(_oc("session.execution.interrupted",
                             reason="user")).events
        self.assertEqual(out[0]["data"]["status"], "interrupted")

    def test_a_turn_that_ends_with_a_request_open(self):
        state = self.state()
        self.turn(state)
        state.feed(_oc("permission.asked", id="per_1", action="shell",
                       resources=["x"]))
        out = state.feed(_oc("session.execution.succeeded")).events
        self.assertEqual(self.types(out), [
            "approval.resolved", "turn.ended", "session.state"])
        self.assertEqual(out[0]["data"]["source"], "turn_ended")
        self.assertEqual(state.pending, {})

    def form(self, fields, **form):
        return {"type": "form.created", "data": {"form": {
            "id": "frm_1", "sessionID": OC_SESSION, "fields": fields,
            **form}}}

    def test_a_form_with_one_question(self):
        state = self.state()
        self.turn(state)
        out = state.feed(self.form([
            {"key": "colour", "type": "string", "title": "Which one?",
             "options": [{"value": "red", "label": "Red"},
                         {"value": OC_KEY}, {"label": "no value"}, "junk"]},
            {"key": "hidden", "type": "string", "hidden": True}])).events
        self.assertEqual(self.types(out),
                         ["input.requested", "session.state"])
        data = out[0]["data"]
        self.assertEqual((data["request_id"], data["prompt"], data["kind"]),
                         ("frm_1", "Which one?", "choice"))
        self.assertEqual(data["options"][0], {"id": "o1", "label": "Red"})
        self.assertEqual(data["options"][1]["id"], "o2")
        self.assertNotIn("sk-ant", json.dumps(out),
                         "a choice the model wrote is redacted")
        self.assertEqual(state.feed(self.form([])).events, [], "asked once")
        request, out = state.answer(OC_SESSION, "frm_1",
                                    Answer(option_id="o2"))
        self.assertEqual(request, (
            "POST", f"/api/session/{OC_SESSION}/form/frm_1/reply",
            {"answer": {"colour": OC_KEY}}))
        self.assertEqual(out[0]["data"]["option_id"], "o2")
        self.assertNotIn("sk-ant", json.dumps(out))
        self.assertEqual(state.feed({"type": "form.replied", "data": {
            "id": "frm_1"}}).events, [])

    def test_form_answers_in_text(self):
        for answer, value in ((Answer(text="blue"), "blue"),
                              (Answer(option_id="o9"), "o9")):
            state = self.state()
            self.turn(state)
            out = state.feed(self.form(
                [{"key": "k", "type": "string"}], title="Name it")).events
            self.assertEqual((out[0]["data"]["prompt"], out[0]["data"]["kind"]),
                             ("Name it", "text"))
            request, _out = state.answer(OC_SESSION, "frm_1", answer)
            self.assertEqual(request.body, {"answer": {"k": value}})

    def test_a_form_we_cannot_show_is_cancelled(self):
        for fields in ([{"key": "a", "type": "string"},
                        {"key": "b", "type": "string"}],
                       [{"key": "a", "type": "boolean"}], []):
            state = self.state()
            self.turn(state)
            fed = state.feed(self.form(fields))
            self.assertEqual(fed.events, [])
            self.assertEqual(fed.replies, [(
                "DELETE", f"/api/session/{OC_SESSION}/form/frm_1", None)])
            self.assertEqual(state.pending, {})
            self.assertEqual(state.unknown["form.created"], 1)

    def test_a_form_of_another_session_or_with_no_id(self):
        state = self.state()
        self.turn(state)
        other = self.form([{"key": "a", "type": "string"}])
        other["data"]["form"]["sessionID"] = "ses_other"
        nameless = self.form([{"key": "a", "type": "string"}])
        del nameless["data"]["form"]["id"]
        for event in (other, nameless,
                      {"type": "form.created", "data": {"form": "x"}}):
            fed = state.feed(event)
            self.assertEqual((fed.events, fed.replies), ([], []))

    def test_a_form_the_server_cancels(self):
        state = self.state()
        self.turn(state)
        state.feed(self.form([{"key": "a", "type": "string"}]))
        out = state.feed({"type": "form.cancelled",
                          "data": {"id": "frm_1"}}).events
        self.assertEqual(out[0]["data"]["source"], "harness")

    def test_a_failed_turn(self):
        state = self.state()
        self.turn(state)
        out = state.feed(_oc("session.step.failed", error={
            "type": "aborted", "message": "Step interrupted"})).events
        self.assertEqual(out, [], "an aborted step is no error")
        failure = {"type": "provider.no-route", "message": "Model unavailable"}
        out = state.feed(_oc("session.step.failed", error=failure)).events
        self.assertEqual(self.types(out), ["error"])
        self.assertEqual(out[0]["data"]["code"], "provider.no-route")
        out = state.feed(_oc("session.execution.failed",
                             error=failure)).events
        self.assertEqual(self.types(out), ["turn.ended", "session.state"],
                         "one error per turn")
        self.assertEqual(out[0]["data"], {
            "status": "failed", "reason": "Model unavailable",
            "duration_s": out[0]["data"]["duration_s"]})
        # A failure that says nothing still fails the turn.
        self.turn(state, token=2, message="msg_u2")
        out = state.feed(_oc("session.execution.failed")).events
        self.assertEqual(self.types(out),
                         ["error", "turn.ended", "session.state"])
        self.assertEqual(out[0]["data"]["message"], "the turn failed")

    def test_an_end_with_no_turn_open(self):
        state = self.state()
        self.assertEqual(state.feed(_oc("session.execution.succeeded")).events,
                         [])
        state.feed(_oc("session.step.failed", error={"type": "x"}))

    def test_other_sessions_and_unknown_events(self):
        state = self.state()
        self.turn(state)
        for event in (
                {"type": "session.text.delta", "data": {
                    "sessionID": "ses_other", "assistantMessageID": "m",
                    "ordinal": 0, "delta": "not ours"}},
                _oc("session.totally.new"), {"type": "project.updated"},
                {"type": 7, "data": {}}, "junk", None):
            fed = state.feed(event)
            self.assertEqual((fed.events, fed.replies), ([], []))
        self.assertEqual(state.unknown, {
            "other-session": 1, "session.totally.new": 1, "?": 2})

    def test_model_agent_and_compaction(self):
        state = self.state()
        state.feed(_oc("session.model.selected",
                       model={"providerID": "p", "id": "m2"}))
        state.feed(_oc("session.agent.selected", agent="plan"))
        state.feed(_oc("session.model.selected", model="x"))
        state.feed(_oc("session.agent.selected"))
        self.assertEqual((state.model["id"], state.agent), ("m2", "plan"))
        out = state.feed(_oc("session.compaction.ended")).events
        self.assertEqual(self.types(out), ["context.compacted"])

    def test_exit_mid_turn(self):
        state = self.state()
        turn_id = self.turn(state)[0]["turn_id"]
        state.feed(_oc("session.text.delta", assistantMessageID="m",
                       ordinal=0, delta="partial reply"))
        state.feed(_oc("permission.asked", id="per_1", action="shell",
                       resources=["x"]))
        out = state.exited("opencode exited with code 3")
        self.assertEqual(self.types(out), [
            "approval.resolved", "turn.ended", "session.state"])
        self.assertEqual(out[0]["data"]["source"], "agent_exit")
        self.assertEqual((out[1]["turn_id"], out[1]["data"]["status"]),
                         (turn_id, "failed"))
        self.assertEqual(out[-1]["data"], {
            "state": "agent_down", "reason": "opencode exited with code 3"})
        self.assertEqual(state.feed(_oc("session.execution.started")).events,
                         [], "nothing after the end")

    def test_replay_of_a_bare_process(self):
        got = opencode_wire.replay([
            {"proc": 0, "dir": "out", "t": 0.1, "http": {
                "n": 1, "method": "POST", "path": "/api/session",
                "body": {}}},
            {"proc": 0, "dir": "in", "t": 0.2, "http": {
                "n": 1, "status": 200, "body": {"data": {"id": OC_SESSION}}}},
            {"proc": 0, "dir": "out", "t": 0.3, "http": {
                "n": 2, "method": "POST",
                "path": f"/api/session/{OC_SESSION}/prompt",
                "body": {"text": "hi"}}},
            {"proc": 0, "dir": "in", "t": 0.4, "http": {
                "n": 2, "status": 400, "body": {"_tag": "Bad"}}},
            {"proc": 0, "junk": True},
            {"proc": 0, "exit": 0}])
        self.assertEqual([e["type"] for e in got],
                         ["session.caps", "session.state", "session.state"])
        self.assertEqual(got[1]["data"]["harness_session_id"], OC_SESSION)


def _opencode_resolved(caps: Caps = ALL_CAPS,
                       min_version: str = "") -> registry.Resolved:
    return registry.Resolved("opencode", "opencode_serve",
                             (sys.executable, str(OPENCODE_FAKE)), caps,
                             min_version=min_version)


class _OpencodeCase(unittest.TestCase):
    """An adapter over the fake server, cleaned up after the test."""

    def fake_env(self, mode: str = "ok", reply: str = "hello",
                 **extra) -> dict:
        self.log = temp_dir(self) / "fake.log"
        env = dict(os.environ, FAKE_OPENCODE_MODE=mode,
                   FAKE_OPENCODE_REPLY=reply,
                   FAKE_OPENCODE_LOG=str(self.log))
        env.update(extra)
        return env

    def adapter(self, mode: str = "ok", *, caps: Caps = ALL_CAPS,
                reply: str = "hello", min_version: str = "",
                env_extra: dict | None = None, **kw):
        adapter = opencode_serve.OpencodeServeAdapter(
            _opencode_resolved(caps, min_version),
            env=self.fake_env(mode, reply, **(env_extra or {})), **kw)
        self.addCleanup(adapter.close, 0.5)
        return adapter

    def spec(self, **kw) -> StartSpec:
        return StartSpec(session_key="opencode:test",
                         cwd=str(temp_dir(self)), **kw)

    def started(self, mode: str = "ok", **kw):
        spec_kw = kw.pop("spec", {})
        adapter = self.adapter(mode, **kw)
        sink = fake.ListSink()
        sid = adapter.start(self.spec(**spec_kw), sink)
        return adapter, sink, sid

    def sent(self) -> list:
        """The requests the fake got, the stream's and info's aside."""
        lines = [json.loads(line) for line in
                 self.log.read_text().splitlines()] if self.log.exists() \
            else []
        return [ln for ln in lines if "path" in ln
                and ln["path"] != "/api/info"]

    def first_line(self) -> dict:
        return json.loads(self.log.read_text().splitlines()[0])

    def sent_paths(self) -> list:
        return [(r["method"], r["path"].replace(
            f"/api/session/{OC_SESSION}", "")) for r in self.sent()]

    def wait(self, sink, match, what="the event"):
        found = sink.wait_for(match, TIMEOUT)
        self.assertIsNotNone(found, f"timed out waiting for {what}")
        return found

    def wait_turn(self, sink, turn_id):
        """The turn's ``turn.ended``, once the ``session.state`` that
        follows it is in too (they are emitted together)."""
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

    def asked(self, sink, etype="approval.requested"):
        return self.wait(sink, etype, "the request")

    def wait_state(self, sink, state):
        return self.wait(sink, lambda e: e["type"] == "session.state"
                         and e["data"]["state"] == state, state)


class TestOpencodeContract(AdapterContract, _OpencodeCase):
    def make_adapter(self, *, caps, reply):
        return self.adapter(caps=caps, reply=reply)


class TestOpencodeAdapterStart(_OpencodeCase):
    def test_registered_and_resolved(self):
        self.assertIn("opencode_serve", registry.registered())
        self.assertIn("opencode_serve", registry.BUILTIN_MODULES)
        resolved = registry.adapter_for("opencode", {}, which=lambda b: b)
        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.argv, opencode_wire.DEFAULT_ARGV)
        self.assertEqual(resolved.caps, Caps(),
                         "the fallback keeps every feature off")
        made = registry.create(resolved)
        self.assertIsInstance(made, opencode_serve.OpencodeServeAdapter)
        self.assertEqual(made.capabilities, Caps())

    def test_caps_never_exceed_what_is_proven(self):
        adapter = self.adapter()
        self.assertEqual(adapter.capabilities, opencode_wire.PROVEN)
        self.assertFalse(adapter.capabilities.stop_task)
        self.assertFalse(adapter.capabilities.can_set("permission_mode"))
        self.assertEqual(self.adapter(caps=Caps(steer=True)).capabilities,
                         Caps(steer=True))

    def test_start_binds_loopback_with_a_password_and_names_the_session(self):
        adapter, sink, sid = self.started(spec={
            "model": "prov/model-x", "effort": "low",
            "session_id": "the-hubs-pick"})
        self.assertEqual(sid, OC_SESSION)
        self.assertEqual(adapter.session_id, OC_SESSION)
        self.assertEqual(adapter.version, "2.3.2")
        self.assertEqual(sink.types(), ["session.caps", "session.state"])
        self.assertEqual(sink.events[0]["data"]["caps"],
                         opencode_wire.PROVEN.as_dict())
        self.assertEqual(sink.events[1]["data"], {
            "state": "idle", "harness_session_id": OC_SESSION})
        first = self.first_line()
        self.assertEqual(first["argv"], ["--hostname", "127.0.0.1",
                                         "--port", "0"])
        self.assertIs(first["password_set"], True)
        self.assertEqual(first["cwd"], os.path.realpath(adapter._spec.cwd))
        created = self.sent()[0]
        self.assertEqual((created["method"], created["path"]),
                         ("POST", "/api/session"))
        self.assertEqual(created["body"], {
            "location": {"directory": adapter._spec.cwd},
            "model": {"providerID": "prov", "id": "model-x",
                      "variant": "low"}})
        self.assertTrue(all(r["authed"] for r in self.sent()))
        self.assertIsNone(adapter.transcript_position())

    def test_the_password_is_never_shown(self):
        adapter, sink, _sid = self.started()
        password = adapter._password
        self.assertGreaterEqual(len(password), 48)
        turn_id = adapter.send_turn(TurnInput("hi"))
        self.wait_turn(sink, turn_id)
        self.assertNotIn(password, json.dumps(sink.events))
        self.assertNotIn(password, self.log.read_text())
        other, _sink, _sid = self.started()
        self.assertNotEqual(other._password, password, "one per start")

    def test_a_password_the_agent_prints_is_masked(self):
        # opencode hands its environment to the commands its agent runs.
        adapter, sink, _sid = self.started("password")
        turn_id = adapter.send_turn(TurnInput("print your environment"))
        self.wait_turn(sink, turn_id)
        reply = [e["data"]["text"] for e in sink.of("message.completed")
                 if e["data"]["kind"] == "assistant"][0]
        self.assertTrue(reply.startswith("it prints "), reply)
        self.assertNotIn(adapter._password, json.dumps(sink.events))
        _streams_close(self, sink.events)

    def test_an_effort_with_no_model_goes_on_the_servers_model(self):
        _adapter, _sink, _sid = self.started(spec={"effort": "high"})
        self.assertEqual(self.sent_paths(), [
            ("POST", "/api/session"), ("POST", "/model")])
        self.assertEqual(self.sent()[-1]["body"], {"model": {
            "providerID": "fake", "id": "model-1", "variant": "high"}})
        self.assertNotIn("model", self.sent()[0]["body"])

    def test_a_session_id_that_is_already_opencodes_is_kept(self):
        _adapter, _sink, sid = self.started(spec={
            "session_id": "ses_chosen0000000000000001"})
        self.assertEqual(sid, "ses_chosen0000000000000001")

    def test_argv_that_names_a_port_is_left_alone(self):
        adapter = opencode_serve.OpencodeServeAdapter(
            registry.Resolved("opencode", "opencode_serve", (
                sys.executable, str(OPENCODE_FAKE), "--port", "0"),
                ALL_CAPS), env=self.fake_env())
        self.addCleanup(adapter.close, 0.5)
        adapter.start(self.spec(), fake.ListSink())
        self.assertEqual(self.first_line()["argv"], ["--port", "0"])

    def test_resume_and_fork(self):
        adapter, sink, sid = self.started(spec={
            "resume_id": "ses_older00000000000000001"})
        self.assertEqual(sid, "ses_older00000000000000001")
        self.assertEqual(self.sent()[0]["method"], "GET")
        self.assertEqual(len(self.sent()), 1, "a resume changes no model")
        adapter.close(0.5)
        forked, sink, sid = self.started(spec={
            "resume_id": "ses_older00000000000000001", "fork": True,
            "model": "prov/m2", "effort": "high", "plan_mode": True})
        self.assertEqual(sid, OC_FORKED)
        self.assertEqual(
            [(r["method"], r["path"]) for r in self.sent()],
            [("POST", "/api/session/ses_older00000000000000001/fork"),
             ("POST", f"/api/session/{OC_FORKED}/model"),
             ("POST", f"/api/session/{OC_FORKED}/agent")])
        self.assertEqual(self.sent()[1]["body"], {"model": {
            "providerID": "prov", "id": "m2", "variant": "high"}})
        self.assertEqual(self.sent()[2]["body"], {"agent": "plan"})

    def test_start_refusals(self):
        for spec, code, said in (
                ({"resume_id": "11111111-2222-4333-8444-555555555555"},
                 "bad_request", "names no opencode session"),
                ({"resume_id": OC_MISSING}, "bad_request", "has no session"),
                ({"model": "no-provider"}, "bad_request", "provider/model")):
            with self.subTest(spec=spec):
                adapter = self.adapter()
                sink = fake.ListSink()
                with self.assertRaises(AdapterError) as ctx:
                    adapter.start(self.spec(**spec), sink)
                self.assertEqual(ctx.exception.code, code)
                self.assertIn(said, str(ctx.exception))
                self.assertEqual(sink.events, [])

    def test_failed_starts_emit_nothing(self):
        for mode, code, said in (
                ("exit-at-start", "agent_down", "cannot start"),
                ("silent", "timeout", "never said it was listening"),
                ("old", "unsupported_version", "older than 2.3.2"),
                ("refuse-session", "refused", "no"),
                ("no-stream", "refused", "event stream")):
            with self.subTest(mode=mode):
                adapter = self.adapter(mode, init_timeout=1.0)
                sink = fake.ListSink()
                with self.assertRaises(AdapterError) as ctx:
                    adapter.start(self.spec(), sink)
                self.assertEqual(ctx.exception.code, code)
                self.assertIn(said, str(ctx.exception))
                self.assertEqual(sink.events, [])
                self.assertIsNotNone(adapter._proc.poll(),
                                     "the child is gone")
                with self.assertRaises(AdapterError):
                    adapter.start(self.spec(), sink)
                adapter.close(0.5)
                self.assertEqual(sink.events, [])

    def test_a_start_that_fails_after_the_session_is_known_emits_nothing(
            self):
        for mode, spec in (
                ("refuse-model", {"resume_id": OC_SESSION,
                                  "model": "prov/m2"}),
                ("refuse-agent", {"plan_mode": True})):
            with self.subTest(mode=mode):
                adapter = self.adapter(mode)
                sink = fake.ListSink()
                with self.assertRaises(AdapterError) as ctx:
                    adapter.start(self.spec(**spec), sink)
                self.assertEqual(ctx.exception.code, "refused")
                self.assertEqual(sink.events, [],
                                 "no start pair for a session that is gone")
                self.assertIsNotNone(adapter._proc.poll())
                adapter.close(0.5)
                self.assertEqual(sink.events, [])

    def test_close_ends_what_the_server_started_too(self):
        # An npm-style launcher keeps the real server as its child; here
        # the launcher exits on SIGTERM and the child ignores it. The
        # child holds the server's stdout, so the reader only ends when
        # the child is gone as well.
        adapter, _sink, _sid = self.started("child")
        adapter.close(0.5)
        self.assertEqual(adapter._proc.poll(), 143)
        adapter._out_reader.join(TIMEOUT)
        self.assertFalse(adapter._out_reader.is_alive())

    def test_a_min_version_from_the_registry(self):
        adapter = self.adapter(min_version="9.0.0")
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(self.spec(), fake.ListSink())
        self.assertEqual(ctx.exception.code, "unsupported_version")

    def test_a_binary_that_is_not_there(self):
        adapter = opencode_serve.OpencodeServeAdapter(registry.Resolved(
            "opencode", "opencode_serve", ("/nonexistent/opencode",),
            ALL_CAPS))
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(self.spec(), fake.ListSink())
        self.assertEqual(ctx.exception.code, "agent_down")
        adapter.close(0.5)

    def test_close_stops_a_server_that_ignores_sigterm(self):
        adapter, sink, _sid = self.started("linger")
        adapter.close(0.3)
        self.assertEqual(adapter._proc.poll(), -9)
        self.assertEqual(sink.events[-1]["data"]["state"], "closed")

    def test_close_mid_turn(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count"))
        self.wait(sink, "message.delta")
        adapter.close(1.0)
        self.assertEqual(sink.types()[-2:], ["turn.ended", "session.state"])
        ended = sink.of("turn.ended")[0]
        self.assertEqual((ended["turn_id"], ended["data"]["status"]),
                         (turn_id, "interrupted"))
        self.assertEqual(sink.events[-1]["data"]["state"], "closed")
        self.assertEqual(adapter._proc.poll(), 143)

    def test_the_server_dies_mid_turn(self):
        adapter, sink, _sid = self.started("crash")
        turn_id = adapter.send_turn(TurnInput("go"))
        down = self.wait_state(sink, "agent_down")
        self.assertEqual(sink.types()[-2:], ["turn.ended", "session.state"])
        ended = sink.of("turn.ended")[0]
        self.assertEqual((ended["turn_id"], ended["data"]["status"]),
                         (turn_id, "failed"))
        self.assertIn("code 3", down["data"]["reason"])
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput("again"))
        self.assertEqual(ctx.exception.code, "agent_down")
        with self.assertRaises(AdapterError) as ctx:
            adapter._call(opencode_wire.info())
        self.assertEqual(ctx.exception.code, "agent_down",
                         "nothing listens there any more")
        count = len(sink.events)
        adapter.close(0.5)
        self.assertEqual(len(sink.events), count, "already over")

    def test_a_quiet_stream_is_not_a_dead_one(self):
        # The handshake's deadline must not stay on the event stream.
        adapter, sink, _sid = self.started(init_timeout=0.3)
        adapter._reader.join(0.8)
        self.assertTrue(adapter._reader.is_alive())
        self.assertIsNone(adapter._proc.poll())
        self.wait_turn(sink, adapter.send_turn(TurnInput("still there")))

    def test_an_event_line_over_the_bound_is_dropped_alone(self):
        with mock.patch.object(opencode_serve, "_MAX_LINE", 300):
            adapter, sink, _sid = self.started(reply="long " * 200)
            turn_id = adapter.send_turn(TurnInput("hi"))
            ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        self.assertIn("an event line over the size bound",
                      list(adapter.dropped))
        self.assertEqual([e for e in sink.of("message.completed")
                          if e["data"]["kind"] == "assistant"], [],
                         "the reply's own lines were the long ones")

    def test_a_stream_the_server_drops_ends_the_session(self):
        adapter, sink, _sid = self.started("drop-stream")
        turn_id = adapter.send_turn(TurnInput("hi"))
        self.wait_turn(sink, turn_id)
        down = self.wait_state(sink, "agent_down")
        self.assertIn("closed its event stream", down["data"]["reason"])
        self.assertIsNotNone(adapter._proc.wait(TIMEOUT))


class TestOpencodeAdapterTurns(_OpencodeCase):
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
        self.assertIn("message.delta", [e["type"] for e in mine])
        self.assertEqual(mine[-2]["type"], "usage.updated")
        _streams_close(self, sink.events)
        self.assertEqual(sink.events[-1]["data"]["state"], "idle")
        prompt = self.sent()[-1]
        self.assertEqual(prompt["body"], {
            "text": "hi", "files": [{"uri": "file:///w/a.txt",
                                     "name": "a.txt"}]})
        second = adapter.send_turn(TurnInput("again"))
        self.assertNotEqual(second, turn_id)
        self.wait_turn(sink, second)

    def test_busy_mid_turn_and_interrupt(self):
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
        self.assertEqual(self.sent_paths()[-1], ("POST", "/interrupt"))
        _streams_close(self, sink.events)
        before = len(sink.events)
        adapter.interrupt()
        self.assertEqual(len(sink.events), before)
        self.assertEqual(self.sent_paths()[-1], ("POST", "/interrupt"),
                         "an idle interrupt is not even sent")
        self.assertEqual(len([p for p in self.sent_paths()
                              if p[1] == "/interrupt"]), 1)

    def test_a_refused_turn(self):
        adapter, sink, _sid = self.started()
        before = len(sink.events)
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput("mode:refuse-prompt go"))
        self.assertEqual(ctx.exception.code, "refused")
        self.assertIn("Prompt refused", str(ctx.exception))
        self.assertEqual(len(sink.events), before)
        self.assertFalse(adapter._state.busy)
        self.wait_turn(sink, adapter.send_turn(TurnInput("fine now")))

    def test_a_prompt_the_server_never_answers(self):
        adapter, sink, _sid = self.started(call_timeout=0.3)
        before = len(sink.events)
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput("mode:hang-prompt go"))
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertEqual(len(sink.events), before)
        self.assertFalse(adapter._state.busy)

    def test_events_that_overtake_the_prompts_answer(self):
        adapter, sink, _sid = self.started(reply="there")
        turn_id = adapter.send_turn(TurnInput("mode:overtake hi",
                                              command_id="c1"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["command_id"], "c1")
        self.assertEqual(len(sink.of("turn.started")), 1)
        users = [e for e in sink.of("message.completed")
                 if e["data"]["kind"] == "user"]
        self.assertEqual(len(users), 1)
        replies = [e["data"]["text"] for e in sink.of("message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies, ["early there"])
        self.assertIn(turn_id, adapter._state.turn_messages)

    def test_a_reply_whose_end_names_no_text(self):
        adapter, sink, _sid = self.started("empty-end", reply="line one")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.wait_turn(sink, turn_id)
        replies = [e["data"]["text"] for e in sink.of("message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies, ["line one\n"])
        _streams_close(self, sink.events)

    def test_a_failed_turn(self):
        adapter, sink, _sid = self.started("fail")
        turn_id = adapter.send_turn(TurnInput("go"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "failed")
        self.assertIn("Model unavailable", ended["data"]["reason"])
        self.assertEqual(len(sink.of("error")), 1)
        self.assertEqual(sink.of("error")[0]["data"]["code"],
                         "provider.no-route")

    def test_noise_changes_nothing(self):
        adapter, sink, _sid = self.started("noise", reply="fine")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.wait_turn(sink, turn_id)
        replies = [e["data"]["text"] for e in sink.of("message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies, ["fine"])
        self.assertEqual(list(adapter.dropped), [])
        self.assertEqual(adapter._state.unknown["other-session"], 1)
        self.assertEqual(adapter._state.unknown["session.totally.new"], 1)
        for event in sink.events:
            self.assertEqual(events.validate(event), [])

    def test_an_event_the_state_machine_chokes_on_is_skipped(self):
        adapter, sink, _sid = self.started()
        real = adapter._state.feed
        calls = []

        def feed(event):
            calls.append(event.get("type"))
            if event.get("type") == "session.step.started":
                raise RuntimeError("boom")
            return real(event)

        with mock.patch.object(adapter._state, "feed", feed):
            turn_id = adapter.send_turn(TurnInput("hi"))
            self.wait_turn(sink, turn_id)
        self.assertEqual(list(adapter.dropped), ["RuntimeError: boom"])

    def test_approval_allow(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("touch it"))
        asked = self.asked(sink)
        self.assertEqual(asked["data"]["kind"], "command")
        self.assertEqual(asked["data"]["prompt"], "touch probe.txt")
        self.wait_state(sink, "waiting")
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="allow"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        types = sink.types()
        self.assertLess(types.index("approval.resolved"),
                        types.index("tool.completed"),
                        "resolved before what the answer allowed")
        self.assertEqual(sink.of("approval.resolved")[0]["data"]["source"],
                         "user")
        self.assertEqual(self.sent()[-1]["body"], {"decision": "once"})
        with self.assertRaises(UnknownRequest):
            adapter.respond(asked["data"]["request_id"],
                            Answer(decision="allow"))
        _contract_order(self, sink.events[2:])

    def test_approval_deny_ends_the_turn(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("touch it"))
        asked = self.asked(sink)
        adapter.respond(asked["data"]["request_id"], Answer(decision="deny"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "interrupted")
        self.assertEqual(sink.of("tool.completed")[0]["data"]["status"],
                         "cancelled")
        self.assertEqual(self.sent()[-1]["body"], {"decision": "reject"})

    def test_approval_deny_with_a_message_goes_on(self):
        adapter, sink, _sid = self.started("approval", reply="understood")
        turn_id = adapter.send_turn(TurnInput("touch it"))
        asked = self.asked(sink)
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="deny", text="another file"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        done = sink.of("tool.completed")[0]["data"]
        self.assertEqual((done["status"], done["output"]),
                         ("error", "another file"))
        self.assertEqual(self.sent()[-1]["body"],
                         {"decision": "reject", "message": "another file"})

    def test_always_is_sent_as_once(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("touch it"))
        asked = self.asked(sink)
        self.assertEqual([o["id"] for o in asked["data"]["options"]],
                         ["allow", "deny"])
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="always"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(self.sent()[-1]["body"], {"decision": "once"})
        self.assertEqual(sink.of("approval.resolved")[0]["data"]["decision"],
                         "allow")

    def test_an_edit_approval_and_its_file_edits(self):
        adapter, sink, _sid = self.started("patch")
        turn_id = adapter.send_turn(TurnInput("write it"))
        asked = self.asked(sink)
        self.assertEqual((asked["data"]["kind"], asked["data"]["prompt"]),
                         ("tool", "edit: probe.txt"))
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="allow"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(sink.of("tool.completed")[0]["data"]["file_edits"], [
            {"path": "probe.txt", "kind": "add",
             "diff": "@@ -0,0 +1,1 @@\n+yes\n"}])

    def test_a_refused_answer_leaves_the_request_open(self):
        adapter, sink, _sid = self.started("refuse-reply")
        adapter.send_turn(TurnInput("mode:approval touch it"))
        asked = self.asked(sink)
        request_id = asked["data"]["request_id"]
        with self.assertRaises(AdapterError) as ctx:
            adapter.respond(request_id, Answer(decision="allow"))
        self.assertEqual(ctx.exception.code, "refused")
        self.assertEqual(sink.of("approval.resolved"), [])
        self.assertIn(request_id, adapter._state.pending)
        self.assertTrue(adapter._state.waiting, "still waiting on it")
        # The interrupt retires it.
        adapter.interrupt()
        self.wait(sink, "turn.ended")
        self.assertEqual(sink.of("approval.resolved")[0]["data"]["source"],
                         "turn_ended")

    def test_an_answer_that_cannot_be_posted_leaves_the_request_open(self):
        adapter, sink, _sid = self.started("approval")
        adapter.send_turn(TurnInput("touch it"))
        asked = self.asked(sink)
        request_id = asked["data"]["request_id"]
        with mock.patch.object(adapter, "_call", side_effect=AdapterError(
                "opencode is not reachable", code="agent_down")):
            with self.assertRaises(AdapterError):
                adapter.respond(request_id, Answer(decision="allow"))
        self.assertIn(request_id, adapter._state.pending)
        adapter.respond(request_id, Answer(decision="allow"))
        self.wait(sink, "turn.ended")
        types = sink.types()
        at = types.index("approval.resolved")
        self.assertEqual((types[at + 1], sink.events[at + 1]["data"]), (
            "session.state", {"state": "running"}),
            "the retry that got through says the session runs again")

    def test_a_question(self):
        adapter, sink, _sid = self.started("question")
        turn_id = adapter.send_turn(TurnInput("ask me"))
        asked = self.asked(sink, "input.requested")
        self.assertEqual(asked["data"]["prompt"], "Which one?")
        self.assertEqual([o["id"] for o in asked["data"]["options"]],
                         ["o1", "o2"])
        self.assertNotIn("sk-ant", json.dumps(sink.events))
        adapter.respond(asked["data"]["request_id"], Answer(option_id="o2"))
        self.wait_turn(sink, turn_id)
        reply = self.sent()[-1]
        self.assertTrue(reply["path"].endswith("/reply"))
        self.assertEqual(reply["body"], {"answer": {"answer": OC_KEY}},
                         "the model's own value goes back to it")
        self.assertNotIn("sk-ant", json.dumps(sink.events))

    def test_a_form_with_two_questions_is_cancelled(self):
        adapter, sink, _sid = self.started("form-many", reply="went on")
        turn_id = adapter.send_turn(TurnInput("ask me"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        self.assertEqual(sink.of("input.requested"), [])
        self.assertEqual(self.sent()[-1]["method"], "DELETE")

    def test_secrets_are_masked_live(self):
        adapter, sink, _sid = self.started("secret")
        turn_id = adapter.send_turn(TurnInput("leak it"))
        asked = self.asked(sink)
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="allow"))
        self.wait_turn(sink, turn_id)
        self.assertNotIn("sk-ant", json.dumps(sink.events))
        self.assertIn("the key is", json.dumps(sink.events))
        _streams_close(self, sink.events)

    def test_turn_ids_carry_a_per_start_prefix(self):
        one, sink, _sid = self.started()
        first = one.send_turn(TurnInput("hi"))
        self.wait_turn(sink, first)
        two, sink2, _sid = self.started()
        second = two.send_turn(TurnInput("hi"))
        self.wait_turn(sink2, second)
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^t-[0-9a-f]{8}-1$")


class TestOpencodeAdapterSteer(_OpencodeCase):
    def test_a_steer_joins_the_turn(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count", command_id="c1"))
        self.wait(sink, "message.delta")
        adapter.steer(TurnInput("stop", command_id="c2", device="phone",
                                attachments=["/w/a.png"]))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        self.assertEqual(len(sink.of("turn.started")), 1)
        done = [(e["data"]["kind"], e["data"]["text"]) for e in
                sink.of("message.completed")]
        self.assertEqual(done, [("user", "count"), ("assistant", "1\n2\n"),
                                ("user", "stop"), ("assistant", "steered")])
        steer = [e for e in sink.of("message.completed")
                 if e["data"]["text"] == "stop"][0]
        self.assertEqual((steer["turn_id"], steer["command_id"],
                          steer["data"]["by"]), (turn_id, "c2", "phone"))
        body = self.sent()[-1]["body"]
        self.assertEqual((body["delivery"], body["files"][0]["name"]),
                         ("steer", "a.png"))
        _streams_close(self, sink.events)

    def test_a_steer_behind_an_approval_gets_the_next_turn(self):
        adapter, sink, _sid = self.started("approval")
        first = adapter.send_turn(TurnInput("touch it"))
        asked = self.asked(sink)
        adapter.steer(TurnInput("never mind", command_id="c2"))
        self.assertEqual(len([e for e in sink.of("message.completed")
                              if e["data"]["kind"] == "user"]), 1,
                         "not in the conversation yet")
        adapter.respond(asked["data"]["request_id"], Answer(decision="deny"))
        self.wait(sink, lambda e: e["type"] == "turn.ended"
                  and e.get("turn_id") != first, "the steer's own turn")
        started = sink.of("turn.started")
        self.assertEqual(len(started), 2)
        self.assertEqual(started[1]["data"]["text"], "never mind")
        self.assertEqual(started[1]["command_id"], "c2")
        users = [(e["turn_id"], e["data"]["text"]) for e in
                 sink.of("message.completed") if e["data"]["kind"] == "user"]
        self.assertEqual(users, [(first, "touch it"),
                                 (started[1]["turn_id"], "never mind")])
        _contract_order(self, sink.events[2:])

    def test_a_refused_steer(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count"))
        self.wait(sink, "message.delta")
        before = len(sink.events)
        with self.assertRaises(AdapterError) as ctx:
            adapter.steer(TurnInput("mode:refuse-prompt stop"))
        self.assertEqual(ctx.exception.code, "refused")
        self.assertEqual(len(sink.events), before)
        adapter.interrupt()
        self.wait_turn(sink, turn_id)

    def test_steer_is_gated(self):
        adapter, sink, _sid = self.started("slow", caps=Caps())
        adapter.send_turn(TurnInput("count"))
        with self.assertRaises(Unsupported):
            adapter.steer(TurnInput("stop"))


class TestOpencodeAdapterOptions(_OpencodeCase):
    def test_model_and_effort(self):
        adapter, sink, _sid = self.started(spec={"model": "prov/m1",
                                                 "effort": "low"})
        before = len(sink.events)
        adapter.set_option("effort", "high")
        self.assertEqual(self.sent()[-1]["body"], {"model": {
            "providerID": "prov", "id": "m1", "variant": "high"}})
        adapter.set_option("model", "other/m2")
        self.assertEqual(self.sent()[-1]["body"], {"model": {
            "providerID": "other", "id": "m2", "variant": "high"}})
        self.assertEqual(len(sink.events), before, "no event of its own")
        with self.assertRaises(AdapterError) as ctx:
            adapter.set_option("model", "no-provider")
        self.assertEqual(ctx.exception.code, "bad_request")
        with self.assertRaises(Unsupported):
            adapter.set_option("permission_mode", "plan")
        with self.assertRaises(ValueError):
            adapter.set_option("colour", "red")

    def test_effort_uses_the_model_the_session_reports(self):
        adapter, _sink, _sid = self.started()
        adapter.set_option("effort", "high")
        self.assertEqual(self.sent()[-1]["body"], {"model": {
            "providerID": "fake", "id": "model-1", "variant": "high"}})

    def test_a_refused_model_changes_nothing(self):
        adapter, _sink, _sid = self.started("refuse-model",
                                            spec={"model": "prov/m1"})
        with self.assertRaises(AdapterError) as ctx:
            adapter.set_option("model", "prov/m2")
        self.assertEqual(ctx.exception.code, "refused")
        self.assertIn("Unknown model", str(ctx.exception))
        self.assertEqual(adapter._model, "prov/m1")

    def test_plan_mode_switches_the_agent_and_back(self):
        adapter, sink, _sid = self.started()
        adapter.set_option("plan_mode", True)
        self.assertEqual(self.sent()[-1]["body"], {"agent": "plan"})
        turn_id = adapter.send_turn(TurnInput("do it"))
        self.wait_turn(sink, turn_id)
        replies = [e["data"]["text"] for e in sink.of("message.completed")
                   if e["data"]["kind"] == "assistant"]
        self.assertEqual(replies, ["planning"])
        adapter.set_option("plan_mode", True)
        adapter.set_option("plan_mode", False)
        self.assertEqual(self.sent()[-1]["body"], {"agent": "build"})
        with self.assertRaises(ValueError):
            adapter.set_option("plan_mode", "yes")

    def test_rollback(self):
        adapter, sink, _sid = self.started()
        first = adapter.send_turn(TurnInput("one"))
        self.wait_turn(sink, first)
        second = adapter.send_turn(TurnInput("two"))
        self.wait_turn(sink, second)
        message = adapter._state.turn_messages[second]
        before = len(sink.events)
        adapter.rollback(second)
        self.assertEqual(self.sent_paths()[-2:], [
            ("POST", "/revert/stage"), ("POST", "/revert/commit")])
        self.assertEqual(self.sent()[-2]["body"],
                         {"messageID": message, "files": False})
        self.assertEqual(len(sink.events), before)
        with self.assertRaises(AdapterError) as ctx:
            adapter.rollback(second)
        self.assertEqual(ctx.exception.code, "unknown_turn")
        adapter.rollback(first)
        self.assertEqual(adapter._state.turn_messages, {})

    def test_rollback_refusals(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count"))
        self.wait(sink, "message.delta")
        with self.assertRaises(AdapterError) as ctx:
            adapter.rollback(turn_id)
        self.assertEqual(ctx.exception.code, "busy")
        adapter.interrupt()
        self.wait_turn(sink, turn_id)
        with self.assertRaises(AdapterError) as ctx:
            adapter.rollback("t-nope-1")
        self.assertEqual(ctx.exception.code, "unknown_turn")
        refuser, sink, _sid = self.started("refuse-revert")
        turn_id = refuser.send_turn(TurnInput("one"))
        self.wait_turn(sink, turn_id)
        with self.assertRaises(AdapterError) as ctx:
            refuser.rollback(turn_id)
        self.assertEqual(ctx.exception.code, "refused")
        self.assertIn(turn_id, refuser._state.turn_messages)

    def test_an_interrupt_that_stopped_nothing_retires_nothing(self):
        adapter, sink, _sid = self.started("deaf-interrupt")
        turn_id = adapter.send_turn(TurnInput("mode:approval touch it"))
        asked = self.asked(sink)
        before = len(sink.events)
        adapter.interrupt()
        self.assertEqual(len(sink.events), before)
        self.assertIn(asked["data"]["request_id"], adapter._state.pending)
        adapter.respond(asked["data"]["request_id"],
                        Answer(decision="allow"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")

    def test_a_revert_that_cannot_be_committed_is_cleared(self):
        adapter, sink, _sid = self.started("refuse-commit")
        turn_id = adapter.send_turn(TurnInput("one"))
        self.wait_turn(sink, turn_id)
        with self.assertRaises(AdapterError) as ctx:
            adapter.rollback(turn_id)
        self.assertEqual(ctx.exception.code, "refused")
        self.assertEqual(self.sent_paths()[-3:], [
            ("POST", "/revert/stage"), ("POST", "/revert/commit"),
            ("DELETE", "/revert")])
        self.assertIn(turn_id, adapter._state.turn_messages)

    def test_a_refused_interrupt(self):
        adapter, sink, _sid = self.started("refuse-interrupt")
        adapter.send_turn(TurnInput("mode:slow count"))
        self.wait(sink, "message.delta")
        before = len(sink.events)
        with self.assertRaises(AdapterError) as ctx:
            adapter.interrupt()
        self.assertEqual(ctx.exception.code, "refused")
        self.assertEqual(len(sink.events), before)

    def test_stop_task_is_never_offered(self):
        adapter, _sink, _sid = self.started()
        with self.assertRaises(Unsupported):
            adapter.stop_task("task-1")

    def test_calls_before_start(self):
        adapter = self.adapter()
        for call in (lambda: adapter.steer(TurnInput("x")),
                     adapter.interrupt,
                     lambda: adapter.respond("r", Answer(decision="allow")),
                     lambda: adapter.set_option("model", "a/b"),
                     lambda: adapter.stop_task("t"),
                     lambda: adapter.rollback("t")):
            with self.assertRaises(AdapterError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "not_started")
        self.assertEqual(adapter.session_id, "")


if __name__ == "__main__":
    unittest.main()
