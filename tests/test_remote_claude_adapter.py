"""The Claude stream adapter (remote/adapters/claude_stream.py) and its
wire module (remote/adapters/claude_wire.py).

Three layers:

- every recorded fixture under tests/fixtures/protocols/claude_stream/
  replays through ``claude_wire.replay`` to exactly its golden events,
  each of which is a valid v2 event;
- the wire module's encoders, decoders and state machine, line by line;
- the adapter over ``tests/fakes/fake_claude.py`` (started like
  ``FAKE_AGENT`` in test_remote_acp.py), including the shared
  ``AdapterContract``.

No test runs the real ``claude``: CI machines have none.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from swe.remote.adapters import claude_stream, claude_wire as wire
from swe.remote.adapters import events, fake, registry
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, StartSpec, TurnInput, UnknownRequest,
    Unsupported,
)
from tests._tempdir import temp_dir
from tests.test_remote_adapter_base import ALL_CAPS, AdapterContract

FAKE = Path(__file__).resolve().parent / "fakes" / "fake_claude.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "protocols" \
    / "claude_stream"
TIMEOUT = 10.0


def _load(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def _resolved(caps: Caps = ALL_CAPS, min_version: str = "") -> registry.Resolved:
    return registry.Resolved("claude", "claude_stream",
                             (sys.executable, str(FAKE)), caps,
                             min_version=min_version)


class _FakeCase(unittest.TestCase):
    """An adapter over the fake CLI, cleaned up after the test."""

    def fake_env(self, mode: str = "ok", reply: str = "hello",
                 **extra) -> dict:
        self.log = temp_dir(self) / "fake.log"
        env = dict(os.environ, FAKE_CLAUDE_MODE=mode,
                   FAKE_CLAUDE_REPLY=reply, FAKE_CLAUDE_LOG=str(self.log),
                   CLAUDECODE="1")
        env.update(extra)
        return env

    def adapter(self, mode: str = "ok", *, caps: Caps = ALL_CAPS,
                reply: str = "hello", min_version: str = "",
                env_extra: dict | None = None, **kw):
        adapter = claude_stream.ClaudeStreamAdapter(
            _resolved(caps, min_version),
            env=self.fake_env(mode, reply, **(env_extra or {})), **kw)
        self.addCleanup(adapter.close, 0.5)
        return adapter

    def spec(self, **kw) -> StartSpec:
        return StartSpec(session_key="claude:test", cwd=str(temp_dir(self)),
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

    def wait(self, sink, match, what="the event"):
        found = sink.wait_for(match, TIMEOUT)
        self.assertIsNotNone(found, f"timed out waiting for {what}")
        return found

    def wait_turn(self, sink, turn_id):
        return self.wait(sink, lambda e: e["type"] == "turn.ended"
                         and e.get("turn_id") == turn_id, "turn.ended")


# ---------------------------------------------------------------------
# Recorded fixtures


class TestRecordedFixtures(unittest.TestCase):
    def fixtures(self):
        found = sorted(FIXTURES.rglob("*.jsonl"))
        self.assertTrue(found, "no recorded fixtures")
        return found

    def test_every_fixture_replays_to_its_golden_events(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                golden = path.with_name(path.name[:-6] + ".events.json")
                self.assertTrue(golden.exists(), f"{golden.name} missing")
                got = wire.replay(_load(path))
                self.assertEqual(got, json.loads(golden.read_text()))
                for event in got:
                    self.assertEqual(events.validate(event), [], event)

    def test_headers_say_how_each_was_recorded(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                header = _load(path)[0]
                self.assertEqual(header["fixture"], 1)
                self.assertEqual(header["adapter"], "claude_stream")
                self.assertEqual(header["version"], path.parent.name)
                self.assertIs(header["synthetic"], False)
                self.assertIsNone(header["error"])
                self.assertEqual(header["env_set"], sorted(wire.SPAWN_ENV))
                for tail in header["argv_tails"]:
                    for flag in wire.PROTOCOL_FLAGS:
                        self.assertIn(flag[0], tail)

    def test_golden_events_keep_the_contract_order(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                got = wire.replay(_load(path))
                self.assertEqual([e["type"] for e in got[:2]],
                                 ["session.caps", "session.state"])
                self.assertEqual(got[-1]["data"]["state"], "closed")
                completed: set = set()
                open_turn = None
                for event in got:
                    if event["type"] == "turn.started":
                        self.assertIsNone(open_turn)
                        open_turn = event["turn_id"]
                    elif event["type"] == "turn.ended":
                        self.assertEqual(event["turn_id"], open_turn)
                        open_turn = None
                    elif event.get("turn_id"):
                        self.assertEqual(event["turn_id"], open_turn,
                                         event["type"])
                    if event["type"] in ("message.completed",
                                         "thinking.completed"):
                        completed.add(event["data"]["id"])
                    if event["type"] == "message.delta":
                        self.assertNotIn(event["data"]["id"], completed)

    def test_what_the_fixtures_prove(self):
        def scenario(name):
            return wire.replay(_load(FIXTURES / wire.RECORDED_VERSION
                                     / f"{name}.jsonl"))

        def of(evs, etype):
            return [e for e in evs if e["type"] == etype]

        hello = scenario("hello")
        self.assertEqual(of(hello, "message.completed")[-1]["data"]["text"],
                         "hello there")
        self.assertTrue(of(hello, "message.delta"))
        approval = scenario("approval")
        self.assertEqual([e["data"]["decision"] for e in
                          of(approval, "approval.resolved")],
                         ["allow", "deny"])
        self.assertEqual([e["data"]["status"] for e in
                          of(approval, "tool.completed")], ["ok", "error"])
        self.assertEqual(of(approval, "tool.completed")[0]["data"]
                         ["file_edits"][0]["kind"], "add")
        interrupt = scenario("interrupt")
        self.assertEqual([e["data"]["status"] for e in
                          of(interrupt, "turn.ended")],
                         ["interrupted", "completed"])
        self.assertEqual(of(interrupt, "error"), [])
        for name in ("plan_mode", "plan_approve"):
            plan = scenario(name)
            kinds = [e["data"]["kind"] for e in
                     of(plan, "approval.requested")]
            self.assertEqual(kinds[0], "plan")
        stop = scenario("stop_task")
        self.assertEqual([e["data"]["status"] for e in
                          of(stop, "task.updated")], ["running", "stopped"])
        sub = scenario("subagent")
        self.assertEqual(of(sub, "task.updated")[-1]["data"]["status"],
                         "completed")
        sub_tools = [e for e in of(sub, "tool.started")
                     if e["data"].get("task_id")]
        self.assertTrue(sub_tools, "a subagent's tool call carries task_id")
        always = scenario("always")
        self.assertEqual([e["data"]["decision"] for e in
                          of(always, "approval.resolved")], ["always"])
        self.assertEqual(len(of(always, "approval.requested")), 1,
                         "the second identical call is not asked")
        self.assertEqual([e["data"]["status"] for e in
                          of(always, "tool.completed")], ["ok", "ok"])
        notes = _load(FIXTURES / wire.RECORDED_VERSION / "always.jsonl")[0][
            "notes"]
        self.assertEqual((notes["asked_turn_1"], notes["asked_turn_2"],
                          notes["settings_files_after"]), (1, 0, []))
        sent = [line["msg"] for line in _load(
            FIXTURES / wire.RECORDED_VERSION / "always.jsonl")[1:]
            if line.get("dir") == "out"
            and line.get("msg", {}).get("type") == "control_response"]
        perms = sent[0]["response"]["response"]["updatedPermissions"]
        self.assertEqual({p["destination"] for p in perms}, {"session"})
        resume = scenario("resume")
        sids = [e["data"].get("harness_session_id")
                for e in of(resume, "session.state")
                if e["data"].get("harness_session_id")]
        self.assertEqual(len(sids), 3)
        self.assertEqual(sids[0], sids[1], "a resume keeps the id")
        self.assertNotEqual(sids[0], sids[2], "a fork gets the new id")
        steer = scenario("steer")
        self.assertEqual(len(of(steer, "turn.ended")), 1,
                         "a priority-now message joins the running turn")
        self.assertEqual(len([e for e in of(steer, "message.completed")
                              if e["data"]["kind"] == "user"]), 2)


class TestFixturesHoldNoPersonalData(unittest.TestCase):
    def test_no_home_user_email_or_temp_paths(self):
        for path in sorted(FIXTURES.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertNotIn("/Users/", text)
                self.assertNotIn("/var/folders/", text)
                self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@(?!example\.com)"
                                          r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
                self.assertNotIn('"account"', text)
                self.assertNotIn('"plugins"', text)
                self.assertNotIn('"slash_commands"', text)


def _scenario(name: str) -> tuple:
    path = FIXTURES / wire.RECORDED_VERSION / f"{name}.jsonl"
    lines = _load(path)
    return lines[0], lines[1:], wire.replay(lines)


def _streams_close(test, evs) -> None:
    """Every streamed block ends in a completion its deltas add up to."""
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


class TestSteerFixtures(unittest.TestCase):
    """What the three steer recordings prove (Claude Code 2.1.328)."""

    def test_steer_cuts_the_reply_inside_the_turn(self):
        _h, lines, evs = _scenario("steer")
        sent = [ln["msg"] for ln in lines if ln.get("dir") == "out"
                and ln["msg"].get("type") == "user"]
        self.assertEqual(sent[1]["priority"], "now")
        cut = [ln["msg"] for ln in lines if ln.get("dir") == "in"
               and ln["msg"].get("aborted")]
        self.assertEqual(len(cut), 1, "the reply in progress arrives cut")
        self.assertEqual(len([ln for ln in lines if ln.get("msg", {}).get(
            "type") == "result"]), 2, "the cut reply has its own result")
        types = [e["type"] for e in evs]
        self.assertEqual(types.count("turn.started"), 1)
        self.assertEqual(types.count("turn.ended"), 1)
        texts = [(e["data"]["kind"], e["data"]["text"]) for e in evs
                 if e["type"] == "message.completed"]
        self.assertEqual(texts[1:], [
            ("user", "Stop counting now and reply only with: steered"),
            ("assistant", "1\n2\n3"), ("assistant", "steered")])
        self.assertEqual([e["data"]["status"] for e in evs
                          if e["type"] == "turn.ended"], ["completed"])
        self.assertEqual([e for e in evs if e["type"] == "error"], [])
        _streams_close(self, evs)
        self.assertEqual(evs[0]["data"]["caps"]["steer"], True)

    def test_steer_kills_a_running_tool(self):
        header, _lines, evs = _scenario("steer_tool")
        self.assertEqual(header["notes"], {
            "written_before_steer": False, "idle_after_s": 4.5,
            "results": 2, "written_at_idle": False,
            "written_after_25s": False})
        done = [e["data"] for e in evs if e["type"] == "tool.completed"]
        self.assertEqual([(d["call_id"], d["status"]) for d in done],
                         [("toolu_01", "cancelled")])
        self.assertIn("aborted", done[0]["output"])
        order = [e["type"] for e in evs]
        steer_at = [i for i, e in enumerate(evs)
                    if e["type"] == "message.completed"
                    and e["data"]["kind"] == "user"][1]
        self.assertLess(steer_at, order.index("tool.completed"))
        self.assertEqual([e["data"]["status"] for e in evs
                          if e["type"] == "turn.ended"], ["completed"])
        _streams_close(self, evs)

    def test_steer_withdraws_an_open_approval(self):
        header, lines, evs = _scenario("steer_approval")
        self.assertEqual(header["notes"], {"cancelled": True,
                                           "result_before_answer": False,
                                           "file_written": False})
        self.assertTrue(any(ln.get("msg", {}).get("type")
                            == "control_cancel_request" for ln in lines))
        resolved = [e["data"] for e in evs if e["type"] == "approval.resolved"]
        self.assertEqual([r["source"] for r in resolved], ["superseded"],
                         "retired at the steer; the CLI's own cancel "
                         "finds nothing open")
        i = next(n for n, e in enumerate(evs)
                 if e["type"] == "approval.resolved")
        self.assertEqual((evs[i - 1]["type"], evs[i - 1]["data"]["kind"]),
                         ("message.completed", "user"))
        self.assertEqual(evs[i + 1]["data"], {"state": "running"})
        self.assertEqual([e["data"]["status"] for e in evs
                          if e["type"] == "tool.completed"], ["cancelled"])
        self.assertEqual([e["data"]["status"] for e in evs
                          if e["type"] == "turn.ended"], ["completed"])
        _streams_close(self, evs)


# ---------------------------------------------------------------------
# The wire module


class TestSpawnLine(unittest.TestCase):
    def test_protocol_flags_are_added_once(self):
        argv = wire.spawn_argv(["claude", "--verbose",
                                "--output-format=stream-json"],
                               session_id="11111111-2222-4333-8444-555555555555")
        self.assertEqual(argv.count("--verbose"), 1)
        self.assertNotIn("--output-format", argv)
        for flag in ("--input-format", "--permission-prompt-tool",
                     "--include-partial-messages"):
            self.assertEqual(argv.count(flag), 1)
        self.assertEqual(argv[-1],
                         "--session-id=11111111-2222-4333-8444-555555555555")
        self.assertEqual(list(wire.DEFAULT_ARGV[1:]),
                         [t for f in wire.PROTOCOL_FLAGS for t in f])

    def test_session_arguments(self):
        sid = "11111111-2222-4333-8444-555555555555"
        new = "aaaaaaaa-2222-4333-8444-555555555555"
        argv = wire.spawn_argv(wire.DEFAULT_ARGV, resume_id=sid, fork=True,
                               session_id=new, model="haiku", effort="low",
                               permission_mode="plan")
        tail = argv[len(wire.DEFAULT_ARGV):]
        self.assertEqual(tail, [f"--resume={sid}", "--fork-session",
                                f"--session-id={new}", "--model=haiku",
                                "--effort=low", "--permission-mode=plan"])
        self.assertNotIn("--fork-session",
                         wire.spawn_argv(["claude"], fork=True))
        with self.assertRaises(ValueError):
            wire.spawn_argv(["claude"], resume_id="--dangerous")

    def test_env_and_slug(self):
        env = wire.spawn_env({"CLAUDECODE": "1", "PATH": "/bin"},
                             {"EXTRA": "x"})
        self.assertEqual(env, {"PATH": "/bin", "EXTRA": "x",
                               "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"})
        self.assertEqual(wire.transcript_slug("/private/tmp/a_b.c/work"),
                         "-private-tmp-a-b-c-work")

    def test_versions(self):
        self.assertEqual(wire.parse_version("2.1.328 (Claude Code)"),
                         (2, 1, 328))
        self.assertIsNone(wire.parse_version(None))
        self.assertTrue(wire.version_ok("2.1.328", "2.0.0"))
        self.assertTrue(wire.version_ok("2.10.0", "2.9.9"))
        self.assertFalse(wire.version_ok("1.9.0", "2.0.0"))
        self.assertFalse(wire.version_ok("", "2.0.0"))
        self.assertTrue(wire.version_ok("", "not a version"))


class TestEncoding(unittest.TestCase):
    def test_lines(self):
        line = wire.encode(wire.user_message("héllo"))
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(json.loads(line), {
            "type": "user", "message": {"role": "user", "content": "héllo"},
            "parent_tool_use_id": None})
        msg = wire.user_message("look", attachments=["/a.png", "/b c.pdf"],
                                uuid="u-1", priority="now")
        self.assertEqual(msg["message"]["content"], "@/a.png @/b c.pdf look")
        self.assertEqual((msg["uuid"], msg["priority"]), ("u-1", "now"))

    def test_control_requests(self):
        self.assertEqual(wire.initialize("r1"), {
            "type": "control_request", "request_id": "r1",
            "request": {"subtype": "initialize", "hooks": None}})
        self.assertEqual(wire.interrupt("r")["request"],
                         {"subtype": "interrupt"})
        self.assertEqual(wire.set_model("r", "haiku")["request"],
                         {"subtype": "set_model", "model": "haiku"})
        self.assertEqual(wire.set_permission_mode("r", "plan")["request"],
                         {"subtype": "set_permission_mode", "mode": "plan"})
        self.assertEqual(wire.stop_task("r", "t9")["request"],
                         {"subtype": "stop_task", "task_id": "t9"})
        self.assertEqual(wire.rewind_files("r", "u1")["request"],
                         {"subtype": "rewind_files", "user_message_id": "u1"})

    def test_permission_answers(self):
        allow = wire.permission_allow("p", {"a": 1}, [{"type": "addRules"}])
        self.assertEqual(allow["response"], {
            "subtype": "success", "request_id": "p",
            "response": {"behavior": "allow", "updatedInput": {"a": 1},
                         "updatedPermissions": [{"type": "addRules"}]}})
        deny = wire.permission_deny("p", "no", interrupt=True)
        self.assertEqual(deny["response"]["response"],
                         {"behavior": "deny", "message": "no",
                          "interrupt": True})
        self.assertEqual(wire.control_error("p", "x")["response"],
                         {"subtype": "error", "request_id": "p",
                          "error": "x"})

    def test_decode_tolerates_anything(self):
        self.assertEqual(wire.decode(b'{"type": "x"}\n'), {"type": "x"})
        for junk in (b"", b"   \n", b"not json", b"[1]", b"\xff\xfe",
                     "3", "null"):
            self.assertIsNone(wire.decode(junk), junk)


class TestHelpers(unittest.TestCase):
    def test_handshake(self):
        hs = wire.Handshake.from_body({
            "claude_code_version": "2.1.328",
            "current_permission_mode": "default",
            "models": [{"value": "haiku"}, {"nope": 1}, "x"],
            "capabilities": ["a", 3]})
        self.assertEqual((hs.version, hs.models, hs.capabilities),
                         ("2.1.328", ("haiku",), ("a",)))
        self.assertEqual(hs.caps().options,
                         frozenset({"model", "permission_mode"}))
        bare = wire.Handshake.from_body(None)
        self.assertEqual(bare.caps().options, frozenset())
        self.assertFalse(bare.caps().plan_mode)

    def test_capability_requirements(self):
        # Steer needs interrupt_send_now_v1: no array, no steer.
        self.assertEqual(wire.capability_caps(None), Caps(
            steer=False, plan_mode=True, rollback=True, fork=True,
            stop_task=True, options={"model", "effort", "permission_mode"}))
        with mock.patch.dict(wire.REQUIRED_CAPABILITIES,
                             {"stop_task": "stop_v1", "model": "model_v1"}):
            caps = wire.capability_caps(["stop_v1"])
            self.assertTrue(caps.stop_task)
            self.assertNotIn("model", caps.options)
            self.assertFalse(wire.capability_caps(None).stop_task)

    def test_summary_edits_redaction(self):
        self.assertEqual(wire.tool_summary("Bash", {"command": "ls\nrm x"}),
                         "Bash · ls")
        self.assertEqual(wire.tool_summary("Task", None), "Task")
        self.assertTrue(wire.tool_summary(
            "Bash", {"command": "x" * 500}).endswith("…"))
        self.assertEqual(wire.file_edits({
            "type": "update", "filePath": "/w/a",
            "structuredPatch": [{"oldStart": 1, "oldLines": 1, "newStart": 1,
                                 "newLines": 1, "lines": ["-a", "+b"]},
                                "junk"]}),
            [{"path": "/w/a", "kind": "modify",
              "diff": "@@ -1,1 +1,1 @@\n-a\n+b\n"}])
        self.assertEqual(wire.file_edits({"type": "create",
                                          "filePath": "/w/n",
                                          "content": "x\ny"}),
                         [{"path": "/w/n", "kind": "add",
                           "diff": "@@ -0,0 +1,2 @@\n+x\n+y\n"}])
        self.assertEqual(wire.file_edits({"type": "update",
                                          "filePath": "/w/n"}),
                         [{"path": "/w/n", "kind": "modify"}])
        for nothing in (None, "Error", {}, {"filePath": ""}):
            self.assertIsNone(wire.file_edits(nothing))
        secret = "sk-ant-api03-" + "A" * 40
        self.assertNotIn(secret, json.dumps(wire.redact(
            {"k": [f"key {secret}"], 1: 2})))
        self.assertEqual(wire._jsonable({"n": float("nan"), 1: (2,),
                                         "s": {3}}),
                         {"n": "nan", "1": "(2,)", "s": "{3}"})
        self.assertEqual(wire._result_text(
            [{"type": "text", "text": "a"}, "b",
             {"type": "tool_reference", "tool_name": "T"}, 3]), "a\nb\nT")
        self.assertEqual(wire._result_text(None), "")


SECRETS = (
    "sk-ant-api03-" + "Q" * 30,
    "API_KEY=supersecretvalue123",
    '{"password": "hunter two pass"}',
    "Authorization: Bearer abcdefghijkl12345",
    "export GITHUB_TOKEN='ghp_" + "z" * 36 + "'",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\ndef456\n"
    "-----END RSA PRIVATE KEY-----",
)


class TestStreamedRedaction(unittest.TestCase):
    """Live text is redacted before it goes out, even when a secret
    arrives split across chunks."""

    def run_stream(self, kind: str, chunks: list, full: str):
        st = wire.StreamState("k", clock=lambda: 1.0, run_id="00000000")
        st.start_events(Caps(), "sid")
        st.begin_turn("go")
        btype, dtype, field = (("text", "text_delta", "text")
                               if kind == "assistant" else
                               ("thinking", "thinking_delta", "thinking"))

        def se(event):
            return {"type": "stream_event", "event": event,
                    "parent_tool_use_id": None, "api_message_id": "m1"}

        out = st.feed(se({"type": "content_block_start", "index": 0,
                          "content_block": {"type": btype}})).events
        for chunk in chunks:
            out += st.feed(se({"type": "content_block_delta", "index": 0,
                               "delta": {"type": dtype,
                                         field: chunk}})).events
        out += st.feed({"type": "assistant", "message": {
            "id": "m1", "content": [{"type": btype, field: full}]}}).events
        deltas = [e["data"] for e in out if e["type"] == "message.delta"]
        done = [e for e in out if e["type"] in ("message.completed",
                                                "thinking.completed")]
        return deltas, done[0]["data"]["text"]

    def check(self, deltas, final, secret):
        offset = 0
        for n, delta in enumerate(deltas, 1):
            self.assertEqual(delta["seq"], n)
            self.assertEqual(delta["offset"], offset)
            offset += len(delta["text"])
        self.assertEqual("".join(d["text"] for d in deltas), final)
        for delta in deltas:
            self.assertNotIn(secret, delta["text"])
        self.assertNotIn(secret, final)

    def test_a_secret_split_anywhere_never_streams(self):
        for secret in SECRETS:
            full = f"Here it is: {secret} and more words.\nNext line ok."
            # The part of the secret that must never show: its value.
            value = secret.split("=")[-1].split(": ")[-1][-12:]
            for kind in ("assistant", "thinking"):
                for cut in range(1, len(full)):
                    with self.subTest(secret=secret[:12], kind=kind,
                                      cut=cut):
                        deltas, final = self.run_stream(
                            kind, [full[:cut], full[cut:]], full)
                        self.check(deltas, final, value)
                # Three chunks around the secret too.
                start = full.index(secret)
                for a in range(start, start + len(secret)):
                    b = min(len(full) - 1, a + 5)
                    deltas, final = self.run_stream(
                        kind, [full[:a], full[a:b], full[b:]], full)
                    self.check(deltas, final, value)

    def test_plain_prose_still_streams_word_by_word(self):
        deltas, final = self.run_stream(
            "assistant", ["hello ", "there ", "friend"], "hello there friend")
        self.assertEqual([d["text"] for d in deltas],
                         ["hello ", "there ", "friend"])
        self.assertTrue(deltas[-1]["done"])
        self.assertEqual(final, "hello there friend")

    def test_stable_cut(self):
        self.assertEqual(wire.stable_cut("one two thr"), 8)
        self.assertEqual(wire.stable_cut("a\nAPI_KEY=abc def"), 2)
        self.assertEqual(wire.stable_cut("x -----BEGIN RSA PRIVATE"), 2)
        self.assertEqual(wire.stable_cut("my token\nnext wo"), 0)


class TestKeyAwareRedaction(unittest.TestCase):
    def test_secret_keys(self):
        for name in ("api_key", "apiKey", "API_KEY", "access_token",
                     "accessToken", "GITHUB_TOKEN", "client_secret",
                     "password", "DB_PASSWORD", "Authorization", "cookie",
                     "privateKey", "secret_key", "credentials"):
            self.assertTrue(wire.secret_key(name), name)
        for name in ("max_tokens", "token_count", "key", "keys", "path",
                     "monkey", "description", 3, ""):
            self.assertFalse(wire.secret_key(name), name)

    def test_nested_values_are_masked(self):
        got = wire.redact({
            "api_key": "plain-credential-123",
            "nested": {"list": [{"password": "pw1"}, {"ok": "fine"}],
                       "headers": {"Authorization": {"scheme": "x"}}},
            "items": [[{"clientSecret": 42}]],
            "empty_token": "", "note": "sk-ant-api03-" + "Q" * 30})
        self.assertEqual(got, {
            "api_key": "[REDACTED]",
            "nested": {"list": [{"password": "[REDACTED]"}, {"ok": "fine"}],
                       "headers": {"Authorization": "[REDACTED]"}},
            "items": [[{"clientSecret": "[REDACTED]"}]],
            "empty_token": "", "note": "[REDACTED]"})

    def test_tool_events_use_it(self):
        st = wire.StreamState("k", clock=lambda: 1.0, run_id="00000000")
        st.start_events(Caps(), "sid")
        st.begin_turn("go")
        out = st.feed({"type": "assistant", "message": {"id": "m", "content": [
            {"type": "tool_use", "id": "tu", "name": "Call",
             "input": {"url": "https://x", "api_key": "plain-credential-123",
                       "opts": [{"token": "t0k"}]}}]}}).events
        started = [e for e in out if e["type"] == "tool.started"][0]
        self.assertEqual(started["data"]["input"],
                         {"url": "https://x", "api_key": "[REDACTED]",
                          "opts": [{"token": "[REDACTED]"}]})
        self.assertNotIn("plain-credential-123", json.dumps(out))


class TestStreamState(unittest.TestCase):
    def state(self, caps=ALL_CAPS) -> wire.StreamState:
        clock = fake.FakeClock(100.0)
        st = wire.StreamState("k", clock=clock, caps=caps)
        st.start_events(caps, "sid")
        return st

    def feed(self, st, *msgs):
        out = []
        for msg in msgs:
            out.extend(st.feed(msg).events)
        return out

    def can_use(self, rid="p1", tool="Bash", suggestions=None, **extra):
        req = {"subtype": "can_use_tool", "tool_name": tool,
               "input": {"command": "ls"}, "tool_use_id": "tu1", **extra}
        if suggestions is not None:
            req["permission_suggestions"] = suggestions
        return {"type": "control_request", "request_id": rid, "request": req}

    def test_events_before_start_are_held(self):
        st = wire.StreamState("k", clock=lambda: 1.0)
        self.assertEqual(self.feed(st, self.can_use()), [])
        out = st.start_events(Caps(), "sid")
        self.assertEqual([e["type"] for e in out],
                         ["session.caps", "session.state", "turn.started",
                          "session.state", "approval.requested",
                          "session.state"])

    def test_unknown_lines_are_counted_not_fatal(self):
        st = self.state()
        out = self.feed(st, {"type": "brand_new"}, {"type": "brand_new"},
                        {"type": 5}, {"type": "has space"},
                        {"type": "system", "subtype": "who_knows"},
                        {"type": "control_response"},
                        {"type": "control_response", "response": {
                            "request_id": 1}},
                        {"type": "control_request", "request_id": "",
                         "request": {}},
                        {"type": "user", "message": {"content": "text"}},
                        {"type": "stream_event", "event": []},
                        {"type": "result"})
        self.assertEqual(out, [])
        self.assertEqual(st.unknown["brand_new"], 2)
        self.assertEqual(st.feed("not a dict").events, [])

    def test_other_control_requests_get_an_error_reply(self):
        st = self.state()
        fed = st.feed({"type": "control_request", "request_id": "h1",
                       "request": {"subtype": "hook_callback"}})
        self.assertEqual(fed.events, [])
        self.assertEqual(fed.replies[0]["response"]["subtype"], "error")
        self.assertEqual(fed.replies[0]["response"]["request_id"], "h1")

    def test_answers(self):
        st = self.state()
        st.begin_turn("go")
        self.feed(st, self.can_use("p1"), self.can_use("p2", suggestions=[
            {"type": "addRules"}]), self.can_use("p3"),
            self.can_use("p4", tool="ExitPlanMode", input={"plan": "do it"}))
        reply, out = st.answer("p1", Answer(decision="always"))
        self.assertNotIn("updatedPermissions", reply["response"]["response"])
        self.assertEqual(out[0]["data"]["decision"], "allow")
        reply, _ = st.answer("p2", Answer(option_id="always", device="mac"))
        self.assertEqual(reply["response"]["response"]["updatedPermissions"],
                         [{"type": "addRules", "destination": "session"}])
        reply, out = st.answer("p3", Answer(text="use ls -l instead"))
        self.assertEqual(reply["response"]["response"],
                         {"behavior": "deny", "message": "use ls -l instead"})
        self.assertEqual(out[0]["data"]["decision"], "deny")
        reply, out = st.answer("p4", Answer(decision="deny"))
        self.assertIn("plan mode", reply["response"]["response"]["message"])
        self.assertEqual(out[-1]["data"], {"state": "running"})
        with self.assertRaises(UnknownRequest):
            st.answer("p4", Answer(decision="allow"))

    def test_always_is_session_scoped_and_can_be_switched_off(self):
        self.assertEqual(wire.session_scoped([
            {"type": "addRules", "rules": [], "destination": "localSettings"},
            {"type": "addDirectories", "destination": "userSettings"},
            {"type": "setMode", "mode": "acceptEdits",
             "destination": "session"}, "junk"]),
            [{"type": "addRules", "rules": [], "destination": "session"},
             {"type": "addDirectories", "destination": "session"}])
        with mock.patch.object(wire, "OFFER_ALWAYS", False):
            st = self.state()
            st.begin_turn("go")
            out = self.feed(st, self.can_use("p1", suggestions=[
                {"type": "addRules", "destination": "localSettings"}]))
            asked = [e for e in out if e["type"] == "approval.requested"][0]
            self.assertEqual([o["id"] for o in asked["data"]["options"]],
                             ["allow", "deny"])
            reply, done = st.answer("p1", Answer(decision="always"))
            self.assertNotIn("updatedPermissions",
                             reply["response"]["response"])
            self.assertEqual(done[0]["data"]["decision"], "allow")

    def test_always_needs_a_rule_left_after_scoping(self):
        st = self.state()
        st.begin_turn("go")
        out = self.feed(st, self.can_use("p1", tool="Write", suggestions=[
            {"type": "setMode", "mode": "acceptEdits",
             "destination": "session"}]))
        asked = [e for e in out if e["type"] == "approval.requested"][0]
        self.assertEqual([o["id"] for o in asked["data"]["options"]],
                         ["allow", "deny"])
        reply, done = st.answer("p1", Answer(decision="always"))
        self.assertNotIn("updatedPermissions", reply["response"]["response"])
        self.assertEqual(done[0]["data"]["decision"], "allow")

    def test_context_window_is_the_active_models(self):
        st = self.state()
        st.begin_turn("go")
        # State frames on, so each result leaves the turn open.
        self.feed(st, {"type": "system", "subtype": "session_state_changed",
                       "state": "running"})
        usage = {"claude-sonnet-5-5": {"contextWindow": 1000000},
                 "claude-haiku-5-5": {"contextWindow": 200000}}

        def window(*msgs):
            out = self.feed(st, *msgs, {"type": "result",
                                        "modelUsage": usage})
            got = [e["data"] for e in out if e["type"] == "usage.updated"]
            return got[0].get("context_window") if got else None

        self.assertIsNone(window(), "no model known yet: no window")
        self.assertEqual(window({"type": "system", "subtype": "init",
                                 "model": "claude-haiku-5-5"}), 200000)
        # set_model's answer names the resolved id.
        self.assertEqual(window({"type": "control_response", "response": {
            "subtype": "success", "request_id": "r",
            "response": {"model": "claude-sonnet-5-5"}}}), 1000000)
        self.assertEqual(st.model, "claude-sonnet-5-5")
        self.assertIsNone(window({"type": "system", "subtype": "init",
                                  "model": "claude-other-1"}))

    def test_no_secret_reaches_an_event(self):
        key = "sk-ant-api03-" + "S" * 40
        st = self.state()
        _turn, out = st.begin_turn(f"use {key}",
                                   attachments=[f"/w/{key}.txt"])

        def sys_(sub, **kw):
            return {"type": "system", "subtype": sub, **kw}

        out += self.feed(
            st,
            sys_("task_started", task_id="a", description=f"Use {key}",
                 task_type="local_agent"),
            sys_("task_progress", task_id="a", description=f"Still {key}"),
            {"type": "control_request", "request_id": "p1", "request": {
                "subtype": "can_use_tool", "tool_name": f"mcp__x__{key}",
                "title": f"Run with {key}?", "display_name": key,
                "input": {"command": f"curl -H 'x: {key}'"}}},
            {"type": "assistant", "message": {"id": "m", "content": [
                # Glued to a letter on purpose: the transcript redactor
                # masks a key that directly follows one (PR 1990).
                {"type": "tool_use", "id": "tu", "name": f"T{key}",
                 "input": {"q": key}},
                {"type": "text", "text": f"the key is {key}"}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu",
                 "content": f"out {key}"}]}},
            {"type": "result", "is_error": True, "subtype": f"e {key}",
             "errors": [f"failed: {key}"]})
        out += st.exited(f"claude exited with code 3: {key}")
        text = json.dumps(out)
        self.assertNotIn(key, text)
        titles = [e["data"]["title"] for e in out
                  if e["type"] == "task.updated"]
        self.assertEqual(titles, ["Use [REDACTED]", "Still [REDACTED]"])
        self.assertEqual(st.tasks["a"]["title"], "Still [REDACTED]")

    def test_turn_ids_differ_across_starts(self):
        ids = set()
        for _ in range(2):
            st = wire.StreamState("same-session", clock=lambda: 1.0)
            st.start_events(Caps(), "sid")
            for _ in range(3):
                turn_id, out = st.begin_turn("hi")
                self.assertRegex(turn_id, r"^t-[0-9a-f]{8}-\d+$")
                user = [e for e in out if e["type"] == "message.completed"]
                for made in (turn_id, user[0]["data"]["id"]):
                    self.assertNotIn(made, ids)
                    ids.add(made)
                st.feed({"type": "system", "subtype":
                         "session_state_changed", "state": "idle"})
        fixed = wire.StreamState("k", clock=lambda: 1.0, run_id="0000abcd")
        fixed.start_events(Caps(), "sid")
        self.assertEqual(fixed.begin_turn("hi")[0], "t-0000abcd-1")

    def test_approval_shapes(self):
        st = self.state()
        st.begin_turn("go")
        out = self.feed(st, self.can_use("p1", suggestions=[{}], title="Run?"),
                        self.can_use("p2", tool="Read",
                                     input={"file_path": "/w/a"}),
                        self.can_use("p3", tool="ExitPlanMode", input={}))
        asks = [e["data"] for e in out if e["type"] == "approval.requested"]
        self.assertEqual([a["kind"] for a in asks],
                         ["command", "tool", "plan"])
        self.assertEqual(asks[0]["prompt"], "Run?\nBash · ls")
        self.assertEqual([o["id"] for o in asks[0]["options"]],
                         ["allow", "always", "deny"])
        self.assertEqual([o["id"] for o in asks[1]["options"]],
                         ["allow", "deny"])
        self.assertEqual(asks[2]["prompt"], "Approve this plan?")
        self.assertEqual([e["type"] for e in out].count("session.state"), 1)

    def test_cancel_and_interrupt_retire_requests(self):
        st = self.state()
        st.begin_turn("go")
        self.feed(st, self.can_use("p1"), self.can_use("p2"))
        out = self.feed(st, {"type": "control_cancel_request",
                             "request_id": "p1"},
                        {"type": "control_cancel_request",
                         "request_id": "ghost"})
        self.assertEqual(out[0]["data"]["source"], "harness")
        out = st.interrupt_sent()
        self.assertEqual([(e["type"], e["data"].get("source"))
                          for e in out],
                         [("approval.resolved", "turn_ended"),
                          ("session.state", None)])
        out = self.feed(st, {"type": "result", "is_error": True},
                        {"type": "system", "subtype":
                         "session_state_changed", "state": "idle"})
        self.assertEqual([e["type"] for e in out],
                         ["turn.ended", "session.state"])
        self.assertEqual(out[0]["data"]["status"], "interrupted")
        self.assertEqual(st.interrupt_sent(), [])

    def test_failed_result_and_no_state_frames(self):
        st = self.state()
        st.begin_turn("go")
        out = self.feed(st, {"type": "result", "is_error": True,
                             "subtype": "error_max_turns",
                             "errors": ["too many", 3],
                             "total_cost_usd": float("nan"),
                             "usage": {"input_tokens": True},
                             "modelUsage": {"m": "x"}})
        self.assertEqual([e["type"] for e in out],
                         ["error", "turn.ended", "session.state"])
        self.assertEqual(out[0]["data"]["code"], "error_max_turns")
        self.assertEqual(out[1]["data"]["status"], "failed")
        self.assertEqual(out[1]["data"]["reason"], "too many")

    def test_a_turn_the_cli_starts_itself(self):
        st = self.state()
        out = self.feed(st, {"type": "system", "subtype":
                             "session_state_changed", "state": "running"},
                        {"type": "system", "subtype":
                         "session_state_changed", "state":
                         "requires_action"},
                        {"type": "system", "subtype":
                         "session_state_changed", "state": "running"},
                        {"type": "system", "subtype": "compact_boundary",
                         "compact_metadata": {"pre_tokens": 900}},
                        {"type": "system", "subtype": "compact_boundary"},
                        {"type": "system", "subtype": "status",
                         "permissionMode": "plan"},
                        {"type": "system", "subtype":
                         "session_state_changed", "state": "idle"},
                        {"type": "system", "subtype":
                         "session_state_changed", "state": "idle"})
        self.assertEqual([e["type"] for e in out],
                         ["turn.started", "session.state", "session.state",
                          "session.state", "context.compacted",
                          "context.compacted", "turn.ended",
                          "session.state"])
        self.assertEqual([e["data"]["state"] for e in out
                          if e["type"] == "session.state"],
                         ["running", "waiting", "running", "idle"])
        self.assertEqual(out[4]["data"], {"tokens_before": 900})
        self.assertEqual(st.permission_mode, "plan")

    def test_tasks(self):
        st = self.state()
        st.begin_turn("go")

        def sys_(sub, **kw):
            return {"type": "system", "subtype": sub, **kw}

        out = self.feed(
            st, sys_("task_started", task_id="a", description="Look",
                     task_type="local_bash"),
            sys_("task_started"),
            sys_("task_progress", task_id="a", description="Look"),
            sys_("task_progress", task_id="a", description="Looking"),
            sys_("task_progress", task_id="ghost", description="x"),
            sys_("task_updated", task_id="a", patch={"status": "running"}),
            sys_("task_updated", task_id="a", patch={"status": "weird"}),
            sys_("task_updated", task_id="a", patch={"status": "failed"}),
            sys_("task_notification", task_id="a", status="failed"))
        self.assertEqual([(e["data"]["status"], e["data"].get("title"))
                          for e in out],
                         [("running", "Look"), ("running", "Looking"),
                          ("failed", "Looking")])
        self.assertEqual(out[0]["data"]["kind"], "background")
        self.assertTrue(out[0]["data"]["can_stop"])
        self.assertFalse(out[2]["data"]["can_stop"])

    def test_streams_tools_and_results(self):
        st = self.state()
        st.begin_turn("go")

        def se(event, parent=None, api="m1"):
            return {"type": "stream_event", "event": event,
                    "parent_tool_use_id": parent, "api_message_id": api}

        out = self.feed(
            st,
            se({"type": "message_start", "message": {"id": "m1"}}),
            se({"type": "content_block_start", "index": 0,
                "content_block": {"type": "thinking"}}),
            se({"type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "hm"}}),
            se({"type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_use"}}),
            se({"type": "content_block_start", "index": 2,
                "content_block": {"type": "text"}}),
            se({"type": "content_block_delta", "index": 2,
                "delta": {"type": "text_delta", "text": "Hi "}}),
            se({"type": "content_block_delta", "index": 2,
                "delta": {"type": "text_delta", "text": ""}}),
            se({"type": "content_block_delta", "index": 9,
                "delta": {"type": "text_delta", "text": "x"}}),
            se({"type": "content_block_delta", "index": 2,
                "delta": {"type": "text_delta", "text": "there"}}),
            se({"type": "content_block_delta", "index": 2,
                "delta": {"type": "text_delta", "text": "z"}}, parent="t"),
            {"type": "assistant", "thinking_duration_ms": 1500,
             "message": {"id": "m1", "content": [
                 {"type": "thinking", "thinking": "hm"},
                 {"type": "tool_use", "id": "tu1", "name": "Edit",
                  "input": {"file_path": "/w/a"}},
                 {"type": "tool_use", "name": "NoId"},
                 {"type": "text", "text": "Hi there"}, "junk"]}},
            se({"type": "content_block_delta", "index": 2,
                "delta": {"type": "text_delta", "text": "late"}}),
            {"type": "user", "tool_use_result": {
                "type": "update", "filePath": "/w/a",
                "structuredPatch": []},
             "message": {"content": [
                 {"type": "tool_result", "tool_use_id": "tu1",
                  "content": "x" * (wire.OUTPUT_LIMIT + 5)}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "unknown"}]}},
            {"type": "assistant", "message": {"id": "m2", "content": [
                {"type": "text", "text": "no stream"}]}})
        types = [e["type"] for e in out]
        # "Hi " streams at once; "hm" and "there" wait for a word break
        # and go out as each block's last delta, just before it ends.
        self.assertEqual(types, ["message.delta", "message.delta",
                                 "thinking.completed", "tool.started",
                                 "message.delta", "message.completed",
                                 "tool.completed", "message.completed"])
        self.assertEqual([(e["data"]["id"], e["data"]["text"],
                           e["data"]["seq"], e["data"]["offset"],
                           e["data"].get("done", False))
                          for e in out if e["type"] == "message.delta"],
                         [("m1:2", "Hi ", 1, 0, False),
                          ("m1:0", "hm", 1, 0, True),
                          ("m1:2", "there", 2, 3, True)])
        self.assertEqual(out[2]["data"], {"id": "m1:0", "text": "hm",
                                          "duration_s": 1.5})
        self.assertEqual(out[5]["data"]["id"], "m1:2")
        self.assertTrue(out[6]["data"]["output"].endswith("…"))
        self.assertEqual(out[6]["data"]["file_edits"],
                         [{"path": "/w/a", "kind": "modify"}])
        self.assertEqual(out[7]["data"]["id"], "m2:a0")

    def test_init_narrows_caps_once(self):
        st = self.state(caps=Caps(stop_task=True))
        with mock.patch.dict(wire.REQUIRED_CAPABILITIES,
                             {"stop_task": "stop_v1"}):
            out = self.feed(st, {"type": "system", "subtype": "init",
                                 "session_id": "bad",
                                 "capabilities": ["stop_v1"]})
            self.assertEqual(out, [])
            out = self.feed(st, {"type": "system", "subtype": "init",
                                 "session_id":
                                 "11111111-2222-4333-8444-555555555555",
                                 "model": "m", "permissionMode": "plan"})
            self.assertEqual([e["data"]["caps"]["stop_task"] for e in out],
                             [False])
            self.assertEqual(self.feed(st, {"type": "system",
                                            "subtype": "init"}), [])
        self.assertEqual((st.model, st.permission_mode, st.session_id),
                         ("m", "plan", "11111111-2222-4333-8444-555555555555"))

    def test_end_sequences_happen_once(self):
        st = self.state()
        st.begin_turn("go")
        self.feed(st, self.can_use("p1"))
        out = st.exited("claude exited with code 3")
        self.assertEqual([(e["type"], e["data"].get("source")
                           or e["data"].get("status")
                           or e["data"].get("state")) for e in out],
                         [("approval.resolved", "agent_exit"),
                          ("turn.ended", "failed"),
                          ("session.state", "agent_down")])
        self.assertEqual(st.closed(), [])
        self.assertEqual(st.feed({"type": "result"}).events, [])

    def test_replay_helpers(self):
        self.assertEqual(wire._argv_session(
            ["--resume=a", "--session-id=b"]), "b")
        self.assertEqual(wire._argv_session(["--resume=a"]), "a")
        self.assertEqual(wire._argv_session(None), "")
        self.assertEqual(wire._answer_from_reply({}).decision, "deny")
        self.assertEqual(wire._answer_from_reply(
            {"response": {"behavior": "allow",
                          "updatedPermissions": [1]}}).decision, "always")
        self.assertEqual(wire.replay([]), [])
        self.assertEqual(wire.replay([{"proc": 0, "dir": "in", "t": 1,
                                       "raw": "x"}]), [])


class TestStreamStateSteer(unittest.TestCase):
    KEY = "sk-ant-api03-" + "S" * 40

    def state(self) -> wire.StreamState:
        st = wire.StreamState("k", clock=fake.FakeClock(100.0), caps=ALL_CAPS)
        st.start_events(ALL_CAPS, "sid")
        return st

    def feed(self, st, *msgs):
        out = []
        for msg in msgs:
            out.extend(st.feed(msg).events)
        return out

    @staticmethod
    def se(index, etype, **kw):
        return {"type": "stream_event", "parent_tool_use_id": None,
                "api_message_id": "m1",
                "event": {"type": etype, "index": index, **kw}}

    def running(self, st):
        st.begin_turn("count")
        return self.feed(st, {"type": "system", "subtype":
                              "session_state_changed", "state": "running"},
                         {"type": "system", "subtype": "init",
                          "capabilities": ["interrupt_send_now_v1"]})

    def test_the_cut_reply_and_the_steer_are_redacted(self):
        st = self.state()
        out = self.running(st)
        self.assertTrue(st.init_seen)
        partial = f"1\n2\nthe key is {self.KEY} and "
        out += self.feed(st, self.se(0, "content_block_start",
                                     content_block={"type": "text"}),
                         self.se(0, "content_block_delta",
                                 delta={"type": "text_delta",
                                        "text": partial}))
        steer = st.steer(f"stop, use {self.KEY}", device="mac",
                         command_id="c2",
                         attachments=[f"/w/{self.KEY}.png"])
        self.assertEqual([e["type"] for e in steer], ["message.completed"])
        self.assertEqual(steer[0]["data"]["by"], "mac")
        self.assertEqual(steer[0]["command_id"], "c2")
        self.assertTrue(steer[0]["data"]["id"].endswith(":user2"))
        out += steer + self.feed(
            st, {"type": "assistant", "aborted": True, "message": {
                "id": "m1", "content": [{"type": "text", "text": partial}]}},
            {"type": "result", "subtype": "success",
             "terminal_reason": "aborted_streaming"})
        self.assertIsNotNone(st.turn, "the intermediate result ends nothing")
        out += self.feed(st, {"type": "system", "subtype":
                              "session_state_changed", "state": "idle"})
        self.assertNotIn(self.KEY, json.dumps(out))
        _streams_close(self, out)
        self.assertEqual([e["data"]["status"] for e in out
                          if e["type"] == "turn.ended"], ["completed"])

    def test_an_abandoned_block_closes_at_the_result(self):
        st = self.state()
        self.running(st)
        out = self.feed(
            st, self.se(0, "content_block_start",
                        content_block={"type": "thinking"}),
            self.se(0, "content_block_delta",
                    delta={"type": "thinking_delta", "thinking": "hmm so "}),
            self.se(1, "content_block_start", content_block={"type": "text"}),
            self.se(1, "content_block_delta",
                    delta={"type": "text_delta",
                           "text": f"a b {self.KEY} c"}),
            {"type": "stream_event", "parent_tool_use_id": None,
             "api_message_id": "m1", "event": {"type": "message_stop"},
             "abandoned_blocks": {"from_block_index": 0}},
            {"type": "result", "subtype": "success",
             "terminal_reason": "aborted_streaming"})
        done = [(e["type"], e["data"]["text"]) for e in out
                if e["type"] in ("message.completed", "thinking.completed")]
        self.assertEqual(done, [("thinking.completed", "hmm so "),
                                ("message.completed", "a b [REDACTED] c")])
        self.assertNotIn(self.KEY, json.dumps(out))
        _streams_close(self, out)
        # Nothing is closed twice.
        self.assertEqual(self.feed(st, {"type": "result"}), [])

    def test_steer_with_an_open_approval(self):
        st = self.state()
        self.running(st)
        self.feed(st, {"type": "control_request", "request_id": "p1",
                       "request": {"subtype": "can_use_tool",
                                   "tool_name": "Bash", "tool_use_id": "tu1",
                                   "input": {"command": "touch a"}}})
        self.assertTrue(st.waiting)
        out = st.steer("never mind")
        self.assertEqual([(e["type"], e["data"].get("source")
                           or e["data"].get("state")) for e in out[1:]],
                         [("approval.resolved", "superseded"),
                          ("session.state", "running")])
        with self.assertRaises(UnknownRequest):
            st.answer("p1", Answer(decision="allow"))
        out = self.feed(st, {"type": "control_cancel_request",
                             "request_id": "p1"},
                        {"type": "user", "message": {"content": [
                            {"type": "tool_result", "tool_use_id": "tu1",
                             "is_error": True,
                             "content": "Tool permission request aborted"}]},
                         "tool_use_result":
                             "Error: Tool permission request aborted"})
        # Already retired: the CLI's cancel adds nothing, and tu1 never
        # started as a tool row here.
        self.assertEqual(out, [])
        self.assertIsNotNone(st.turn)
        self.assertFalse(st.waiting)

    def test_a_withdrawn_call_reads_cancelled(self):
        st = self.state()
        self.running(st)
        self.feed(st, {"type": "assistant", "message": {"id": "m", "content": [
            {"type": "tool_use", "id": "tu1", "name": "Bash",
             "input": {"command": "touch a"}},
            {"type": "tool_use", "id": "tu2", "name": "Bash",
             "input": {"command": "false"}}]}},
            {"type": "control_request", "request_id": "p1", "request": {
                "subtype": "can_use_tool", "tool_name": "Bash",
                "tool_use_id": "tu1", "input": {"command": "touch a"}}},
            {"type": "control_cancel_request", "request_id": "p1"})

        def result(call_id):
            return {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": call_id,
                 "is_error": True, "content": "no"}]}}

        out = self.feed(st, result("tu1"), result("tu2"))
        self.assertEqual([e["data"]["status"] for e in out],
                         ["cancelled", "error"])

    def test_the_capability_gate(self):
        self.assertTrue(wire.capability_caps(["interrupt_send_now_v1"]).steer)
        self.assertFalse(wire.capability_caps(["interrupt_receipt_v1"]).steer)
        self.assertFalse(wire.capability_caps(None).steer)
        self.assertFalse(wire.capability_caps("interrupt_send_now_v1").steer)
        self.assertTrue(wire.PROVEN.steer)
        for caps in (["interrupt_receipt_v1"], None):
            with self.subTest(caps=caps):
                st = self.state()
                st.begin_turn("go")
                msg = {"type": "system", "subtype": "init"}
                if caps is not None:
                    msg["capabilities"] = caps
                out = self.feed(st, msg)
                self.assertEqual([e["data"]["caps"]["steer"] for e in out],
                                 [False])
                self.assertTrue(st.init_seen)


# ---------------------------------------------------------------------
# The adapter over the fake CLI


class TestClaudeContract(AdapterContract, _FakeCase):
    def make_adapter(self, *, caps, reply):
        return self.adapter(caps=caps, reply=reply)


class TestAdapterStart(_FakeCase):
    def test_registered_and_resolved(self):
        self.assertIn("claude_stream", registry.registered())
        resolved = registry.adapter_for("claude", {}, which=None)
        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.adapter, "claude_stream")
        made = registry.create(resolved)
        self.assertIsInstance(made, claude_stream.ClaudeStreamAdapter)
        self.assertEqual(made.capabilities, Caps(), "the fallback is all off")

    def test_spawn_line_env_and_session_id(self):
        adapter, sink, sid = self.started(spec={"model": "haiku",
                                                "effort": "low",
                                                "plan_mode": True})
        self.assertTrue(wire.is_session_id(sid))
        self.assertEqual(adapter.session_id, sid)
        first = self.sent()[0]
        self.assertIn(f"--session-id={sid}", first["argv"])
        self.assertIn("--model=haiku", first["argv"])
        self.assertIn("--effort=low", first["argv"])
        self.assertIn("--permission-mode=plan", first["argv"])
        self.assertEqual(first["env"], {
            "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1", "CLAUDECODE": None})
        self.assertEqual(self.sent()[1]["request"]["subtype"], "initialize")
        self.assertEqual(adapter.version, "2.1.328")
        caps = sink.of("session.caps")[0]["data"]["caps"]
        self.assertEqual(caps, {"steer": True, "plan_mode": True,
                                "rollback": False, "fork": True,
                                "stop_task": True,
                                "options": ["model", "permission_mode"]})
        self.assertEqual(sink.of("session.state")[0]["data"],
                         {"state": "idle", "harness_session_id": sid})
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(self.spec(), fake.ListSink())
        self.assertEqual(ctx.exception.code, "started")

    def test_resume_and_fork(self):
        old = "11111111-2222-4333-8444-555555555555"
        _a, _s, sid = self.started(spec={"resume_id": old})
        self.assertEqual(sid, old)
        argv = self.sent()[0]["argv"]
        self.assertIn(f"--resume={old}", argv)
        self.assertFalse(any(t.startswith("--session-id") for t in argv))
        _a, _s, fork_id = self.started(spec={"resume_id": old, "fork": True})
        self.assertNotEqual(fork_id, old)
        argv = self.sent()[0]["argv"]
        self.assertIn("--fork-session", argv)
        self.assertIn(f"--session-id={fork_id}", argv)
        bad = self.adapter()
        with self.assertRaises(AdapterError) as ctx:
            bad.start(self.spec(resume_id="not-a-uuid"), fake.ListSink())
        self.assertEqual(ctx.exception.code, "bad_request")

    def assert_start_fails(self, mode, code, *, text="", **kw):
        adapter = self.adapter(mode, **kw)
        sink = fake.ListSink()
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(self.spec(), sink)
        self.assertEqual(ctx.exception.code, code)
        self.assertIn(text, str(ctx.exception))
        self.assertEqual(sink.events, [], "a failed start emits nothing")
        with self.assertRaises(AdapterError):
            adapter.send_turn(TurnInput("hi"))
        with self.assertRaises(AdapterError):
            adapter.start(self.spec(), sink)
        adapter.close(0.1)
        self.assertEqual(sink.events, [])
        return adapter

    def test_too_old_version(self):
        adapter = self.assert_start_fails("old", "unsupported_version",
                                          text="1.0.0 is older than 2.0.0")
        self.assertIsNotNone(adapter._proc.poll(), "the child was stopped")
        self.assert_start_fails("ok", "unsupported_version",
                                text="older than 9.0.0", min_version="9.0.0")

    def test_handshake_failures(self):
        self.assert_start_fails("exit-at-init", "agent_down",
                                text="not logged in")
        self.assert_start_fails("refuse-init", "refused", text="auth required")
        self.assert_start_fails("silent-init", "timeout", init_timeout=0.3)

    def test_spawn_failure(self):
        def boom(*a, **kw):
            raise OSError("no such file")

        adapter = claude_stream.ClaudeStreamAdapter(_resolved(), spawn=boom)
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(self.spec(), fake.ListSink())
        self.assertEqual(ctx.exception.code, "agent_down")
        adapter.close()

    def test_missing_protocol_flags_are_supplied(self):
        # The fake exits 2 when a protocol flag is missing; harness.json
        # giving only the binary still gets the full line.
        resolved = registry.Resolved("claude", "claude_stream",
                                     (sys.executable, str(FAKE)), ALL_CAPS)
        adapter = claude_stream.ClaudeStreamAdapter(
            resolved, env=self.fake_env())
        self.addCleanup(adapter.close, 0.5)
        adapter.start(self.spec(argv=(sys.executable, str(FAKE),
                                      "--verbose")), fake.ListSink())
        self.assertEqual(self.sent()[0]["argv"].count("--verbose"), 1)


class TestAdapterTurns(_FakeCase):
    def test_turn_with_attachments_and_usage(self):
        adapter, sink, _sid = self.started(reply="hello world")
        turn_id = adapter.send_turn(TurnInput("look", attachments=["/w/a.png"],
                                              command_id="c1", device="ph"))
        self.wait_turn(sink, turn_id)
        sent = [m for m in self.sent() if m.get("type") == "user"]
        self.assertEqual(sent[0]["message"]["content"], "@/w/a.png look")
        mine = [e for e in sink.events if e.get("turn_id") == turn_id]
        user = [e for e in mine if e["type"] == "message.completed"][0]
        self.assertEqual(user["data"]["attachments"], ["/w/a.png"])
        self.assertEqual(user["command_id"], "c1")
        usage = [e for e in mine if e["type"] == "usage.updated"][0]["data"]
        self.assertEqual(usage, {"input_tokens": 3, "output_tokens": 5,
                                 "cache_read_tokens": 7,
                                 "cache_write_tokens": 0, "cost_usd": 0.001,
                                 "context_window": 200000})
        self.assertEqual(sink.events[-1]["data"]["state"], "idle")
        ended = [e for e in mine if e["type"] == "turn.ended"][0]
        self.assertEqual(ended["command_id"], "c1")

    def approval(self, sink):
        return self.wait(sink, "approval.requested")

    def test_allow_deny_and_always(self):
        for answer, behavior, status in (
                (Answer(decision="allow", device="mac"), "allow", "ok"),
                (Answer(decision="always"), "allow", "ok"),
                (Answer(decision="deny", text="not now"), "deny", "error")):
            with self.subTest(decision=answer.decision):
                adapter, sink, _sid = self.started("approval")
                turn_id = adapter.send_turn(TurnInput("go"))
                asked = self.approval(sink)
                self.assertEqual(asked["data"]["kind"], "command")
                self.assertEqual(asked["data"]["call_id"], "toolu_fake1")
                self.assertEqual(sink.of("session.state")[-1]["data"]
                                 ["state"], "waiting")
                adapter.respond(asked["data"]["request_id"], answer)
                self.wait_turn(sink, turn_id)
                reply = [m for m in self.sent()
                         if m.get("type") == "control_response"][0]
                body = reply["response"]["response"]
                self.assertEqual(body["behavior"], behavior)
                if answer.decision == "always":
                    self.assertIn("updatedPermissions", body)
                if behavior == "deny":
                    self.assertEqual(body["message"], "not now")
                done = sink.of("tool.completed")[0]["data"]
                self.assertEqual(done["status"], status)
                with self.assertRaises(UnknownRequest):
                    adapter.respond(asked["data"]["request_id"], answer)

    def test_busy_mid_turn(self):
        adapter, sink, _sid = self.started("approval")
        adapter.send_turn(TurnInput("go"))
        self.approval(sink)
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput("again"))
        self.assertEqual(ctx.exception.code, "busy")

    def test_interrupt(self):
        adapter, sink, _sid = self.started("slow")
        turn_id = adapter.send_turn(TurnInput("count"))
        self.wait(sink, "message.delta")
        adapter.interrupt()
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "interrupted")
        self.assertEqual(sink.of("error"), [], "an interrupt is no error")
        self.assertEqual(sink.events[-1]["data"]["state"], "idle")
        self.assertEqual(sink.of("message.completed")[-1]["data"]["text"],
                         "1\n2\n")

    def test_interrupt_with_an_open_approval(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.approval(sink)
        adapter.interrupt()
        resolved = sink.of("approval.resolved")[0]["data"]
        self.assertEqual(resolved["source"], "turn_ended")
        self.assertEqual(self.wait_turn(sink, turn_id)["data"]["status"],
                         "interrupted")
        with self.assertRaises(UnknownRequest):
            adapter.respond(asked["data"]["request_id"],
                            Answer(decision="allow"))

    def test_the_cli_withdraws_its_request(self):
        adapter, sink, _sid = self.started("cancel")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(sink.of("approval.resolved")[0]["data"]["source"],
                         "harness")

    def test_crash_mid_turn(self):
        adapter, sink, _sid = self.started("crash")
        turn_id = adapter.send_turn(TurnInput("go"))
        down = self.wait(sink, lambda e: e["type"] == "session.state"
                         and e["data"]["state"] == "agent_down", "agent_down")
        tail = sink.types()[-2:]
        self.assertEqual(tail, ["turn.ended", "session.state"])
        ended = sink.of("turn.ended")[0]
        self.assertEqual((ended["turn_id"], ended["data"]["status"]),
                         (turn_id, "failed"))
        self.assertIn("code 3", down["data"]["reason"])
        self.assertIn("panic", down["data"]["reason"])
        count = len(sink.events)
        for call in (lambda: adapter.send_turn(TurnInput("x")),
                     adapter.interrupt,
                     lambda: adapter.respond("r", Answer(text="x")),
                     lambda: adapter.set_option("model", "m")):
            with self.assertRaises(AdapterError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "agent_down")
        adapter.close(0.1)
        self.assertEqual(len(sink.events), count, "nothing after the exit")

    def test_exit_with_an_open_approval(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.approval(sink)
        sink.clear()
        adapter._proc.kill()
        self.wait(sink, lambda e: e["type"] == "session.state"
                  and e["data"]["state"] == "agent_down", "agent_down")
        self.assertEqual(sink.types(), ["approval.resolved", "turn.ended",
                                        "session.state"])
        self.assertEqual(sink.events[0]["data"],
                         {"request_id": asked["data"]["request_id"],
                          "source": "agent_exit"})
        self.assertEqual(sink.events[1]["turn_id"], turn_id)
        self.assertEqual(sink.events[1]["data"]["status"], "failed")

    def test_close_with_an_open_approval(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.approval(sink)
        sink.clear()
        adapter.close(2.0)
        self.assertEqual(sink.types(), ["approval.resolved", "turn.ended",
                                        "session.state"])
        self.assertEqual(sink.events[0]["data"]["source"], "closed")
        self.assertEqual(sink.events[0]["data"]["request_id"],
                         asked["data"]["request_id"])
        self.assertEqual((sink.events[1]["turn_id"],
                          sink.events[1]["data"]["status"]),
                         (turn_id, "interrupted"))
        self.assertEqual(sink.events[2]["data"]["state"], "closed")
        self.assertEqual(adapter._proc.poll(), 0, "EOF ended the CLI")

    def test_close_kills_a_cli_that_lingers(self):
        adapter, sink, _sid = self.started("linger")
        adapter.close(0.2)
        self.assertEqual(sink.events[-1]["data"]["state"], "closed")
        self.assertIsNotNone(adapter._proc.poll(), "terminated")

    def test_a_dead_pipe_is_agent_down(self):
        adapter, sink, _sid = self.started("approval")
        adapter.send_turn(TurnInput("go"))
        asked = self.approval(sink)
        adapter._proc.stdin.close()
        for call in (lambda: adapter.respond(asked["data"]["request_id"],
                                             Answer(decision="allow")),
                     lambda: adapter.set_option("model", "m"),
                     adapter.interrupt):
            with self.assertRaises(AdapterError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "agent_down")
        down = self.wait(sink, lambda e: e["type"] == "session.state"
                         and e["data"]["state"] == "agent_down", "exit")
        self.assertIsNotNone(down)
        self.assertEqual(sink.of("approval.resolved")[-1]["data"]["source"],
                         "agent_exit", "the unanswered request is retired")
        idle, sink2, _sid = self.started()
        idle._proc.stdin.close()
        with self.assertRaises(AdapterError) as ctx:
            idle.send_turn(TurnInput("hi"))
        self.assertEqual(ctx.exception.code, "agent_down")
        self.assertEqual(sink2.of("turn.started"), [])

    def test_a_line_the_state_machine_chokes_on_is_skipped(self):
        adapter, sink, _sid = self.started()
        with mock.patch.object(wire.StreamState, "_on_result",
                               side_effect=RuntimeError("bug")):
            turn_id = adapter.send_turn(TurnInput("go"))
            self.wait_turn(sink, turn_id)
        self.assertIn("RuntimeError: bug", adapter.dropped)

    def test_unknown_lines_and_fields_are_tolerated(self):
        adapter, sink, _sid = self.started("noise")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.assertEqual(self.wait_turn(sink, turn_id)["data"]["status"],
                         "completed")
        self.assertEqual(adapter._state.unknown.get("brand_new_type"), 1)

    def test_no_state_frames_end_the_turn_at_the_result(self):
        adapter, sink, _sid = self.started("no-state")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.assertEqual(self.wait_turn(sink, turn_id)["data"]["status"],
                         "completed")
        turn_id = adapter.send_turn(TurnInput("again"))
        self.wait_turn(sink, turn_id)

    def test_a_turn_the_cli_starts_itself(self):
        adapter, sink, _sid = self.started("wake")
        first = adapter.send_turn(TurnInput("go"))
        second = self.wait(sink, lambda e: e["type"] == "turn.ended"
                           and e["turn_id"] != first, "the woken turn")
        self.assertRegex(second["turn_id"], r"^t-[0-9a-f]{8}-2$")
        self.assertEqual(second["data"]["status"], "completed")
        self.assertIn("woke up", [e["data"]["text"] for e in
                                  sink.of("message.completed")])

    def test_unhandled_control_requests_get_an_error(self):
        adapter, sink, _sid = self.started("hook")
        turn_id = adapter.send_turn(TurnInput("go"))
        self.wait_turn(sink, turn_id)
        replies = [m for m in self.sent() if m.get("type") ==
                   "control_response"]
        self.assertEqual(replies[0]["response"]["request_id"], "hook-1")
        self.assertEqual(replies[0]["response"]["subtype"], "error")


class TestAdapterOptions(_FakeCase):
    def sent_requests(self, subtype):
        return [m["request"] for m in self.sent()
                if m.get("type") == "control_request"
                and m["request"]["subtype"] == subtype]

    def test_model_and_permission_mode(self):
        adapter, _sink, _sid = self.started()
        adapter.set_option("model", "haiku")
        adapter.set_option("permission_mode", "acceptEdits")
        self.assertEqual(self.sent_requests("set_model"),
                         [{"subtype": "set_model", "model": "haiku"}])
        self.assertEqual(self.sent_requests("set_permission_mode")[0]["mode"],
                         "acceptEdits")
        with self.assertRaises(AdapterError) as ctx:
            adapter.set_option("model", "nope")
        self.assertEqual(ctx.exception.code, "refused")
        with self.assertRaises(Unsupported):
            adapter.set_option("effort", "high")

    def test_plan_mode_round_trip(self):
        adapter, _sink, _sid = self.started()
        adapter.set_option("permission_mode", "acceptEdits")
        adapter.set_option("plan_mode", True)
        adapter.set_option("plan_mode", True)
        adapter.set_option("plan_mode", False)
        self.assertEqual([r["mode"] for r in
                          self.sent_requests("set_permission_mode")],
                         ["acceptEdits", "plan", "plan", "acceptEdits"])
        fresh, _sink, _sid = self.started()
        fresh.set_option("plan_mode", False)
        self.assertEqual(self.sent_requests("set_permission_mode")[-1]["mode"],
                         "default")

    def test_gates_follow_caps(self):
        adapter, _sink, _sid = self.started(caps=Caps())
        for call in (lambda: adapter.set_option("model", "m"),
                     lambda: adapter.set_option("plan_mode", True),
                     lambda: adapter.stop_task("x"),
                     lambda: adapter.rollback("t1"),
                     lambda: adapter.steer(TurnInput("x"))):
            with self.assertRaises(Unsupported):
                call()
        full, _sink, _sid = self.started()
        self.assertTrue(full.capabilities.steer, "steer is proven")
        self.assertFalse(full.capabilities.rollback)
        with self.assertRaises(Unsupported):
            full.rollback("t1")

    def test_capabilities_array_narrows_caps(self):
        with mock.patch.dict(wire.REQUIRED_CAPABILITIES,
                             {"plan_mode": "plan_mode_v9"}):
            adapter, sink, _sid = self.started()
            self.assertTrue(adapter.capabilities.plan_mode)
            turn_id = adapter.send_turn(TurnInput("go"))
            self.wait_turn(sink, turn_id)
            narrowed = sink.of("session.caps")
            self.assertEqual(len(narrowed), 2)
            self.assertFalse(narrowed[-1]["data"]["caps"]["plan_mode"])
            self.assertFalse(adapter.capabilities.plan_mode)
            with self.assertRaises(Unsupported):
                adapter.set_option("plan_mode", True)
        with mock.patch.dict(wire.REQUIRED_CAPABILITIES,
                             {"stop_task": "interrupt_receipt_v1"}):
            kept, sink, _sid = self.started(env_extra={
                "FAKE_CLAUDE_CAPS": "none"})
            turn_id = kept.send_turn(TurnInput("go"))
            self.wait_turn(sink, turn_id)
            self.assertFalse(kept.capabilities.stop_task,
                             "no array at all fails a requirement")

    def test_stop_task(self):
        adapter, sink, _sid = self.started("task")
        turn_id = adapter.send_turn(TurnInput("go"))
        started = self.wait(sink, "task.updated")["data"]
        self.assertEqual((started["task_id"], started["kind"],
                          started["status"], started["can_stop"],
                          started["parent_turn_id"]),
                         ("task-1", "subagent", "running", True, turn_id))
        # The subagent's message comes after `task_started` on the wire,
        # so its tool event is waited for, not read off the sink at once:
        # on a slow runner the list was still empty.
        sub_tool = self.wait(
            sink, lambda e: e["type"] == "tool.started"
            and e["data"]["name"] == "Bash", "the subagent's tool.started")
        self.assertEqual(sub_tool["data"]["task_id"], "task-1")
        with self.assertRaises(AdapterError) as ctx:
            adapter.stop_task("ghost")
        self.assertEqual(ctx.exception.code, "unknown_task")
        adapter.stop_task("task-1")
        self.wait_turn(sink, turn_id)
        self.assertEqual([e["data"]["status"] for e in
                          sink.of("task.updated")], ["running", "stopped"])
        self.assertEqual(sink.of("tool.completed")[-1]["data"]["status"],
                         "cancelled")
        # Checked once the turn is over, when nothing more can arrive: the
        # subagent's own words never become a message of the session's.
        self.assertNotIn("sub words", [e["data"]["text"] for e in
                                       sink.of("message.completed")])
        with self.assertRaises(AdapterError):
            adapter.stop_task("task-1")

    def test_transcript_position_follows_the_child_env(self):
        config = temp_dir(self) / "cfg"
        home = temp_dir(self) / "home"
        for env, root in (({"CLAUDE_CONFIG_DIR": str(config)}, config),
                          ({"HOME": str(home)}, home / ".claude")):
            with self.subTest(env=sorted(env)):
                adapter, _sink, sid = self.started(
                    spec={"env": env}, env_extra={"CLAUDE_CONFIG_DIR": ""})
                self.assertIsNone(adapter.transcript_position())
                folder = root / "projects" / wire.transcript_slug(
                    os.path.realpath(adapter._cwd))
                folder.mkdir(parents=True)
                (folder / f"{sid}.jsonl").write_text("{}\n")
                pos = adapter.transcript_position()
                self.assertEqual((pos.path, pos.offset),
                                 (str(folder / f"{sid}.jsonl"), 3))
        self.assertIsNone(claude_stream.ClaudeStreamAdapter()
                          .transcript_position())

    def test_a_cli_that_stops_reading(self):
        import time as _time

        adapter, sink, _sid = self.started("deaf", write_timeout=0.5)
        big = "x" * (2 * 1024 * 1024)  # past any pipe buffer
        began = _time.monotonic()
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput(big))
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertLess(_time.monotonic() - began, 5.0)
        self.assertEqual(sink.of("turn.started"), [])
        self.wait(sink, lambda e: e["type"] == "session.state"
                  and e["data"]["state"] == "agent_down", "the kill's exit")
        began = _time.monotonic()
        adapter.close(0.5)
        self.assertLess(_time.monotonic() - began, 5.0)
        self.assertIsNotNone(adapter._proc.poll(), "the child is gone")
        self.assertFalse(adapter._writer.thread.is_alive(),
                         "the writer thread ended")
        self.assertTrue(adapter._proc.stdin.closed, "stdin is closed")

    def test_close_while_the_writer_is_stuck(self):
        import time as _time

        adapter, sink, _sid = self.started("deaf", write_timeout=30.0)
        # A reply the reader queues without waiting fills the pipe.
        adapter._write(wire.user_message("y" * (2 * 1024 * 1024)),
                       wait=False)
        began = _time.monotonic()
        adapter.close(0.5)
        self.assertLess(_time.monotonic() - began, 8.0)
        self.assertIsNotNone(adapter._proc.poll(), "the child is gone")
        self.assertEqual(sink.events[-1]["data"]["state"], "closed")
        self.assertFalse(adapter._writer.thread.is_alive())
        self.assertTrue(adapter._proc.stdin.closed)

    def test_close_ends_the_writer(self):
        adapter, _sink, _sid = self.started()
        adapter.close(1.0)
        self.assertFalse(adapter._writer.thread.is_alive())
        self.assertTrue(adapter._proc.stdin.closed)
        self.assertEqual(adapter._proc.poll(), 0)


class TestAdapterSteer(_FakeCase):
    """``steer`` over the fake CLI, which answers ``priority: "now"`` the
    way the steer recordings show."""

    KEY = "sk-ant-api03-" + "K" * 40

    def steers_sent(self) -> list:
        return [m for m in self.sent() if m.get("type") == "user"
                and m.get("priority") == "now"]

    def mid_reply(self, **kw):
        adapter, sink, _sid = self.started("slow", **kw)
        turn_id = adapter.send_turn(TurnInput("count"))
        self.wait(sink, "message.delta")
        return adapter, sink, turn_id

    def test_steer_cuts_the_reply_and_answers_in_the_same_turn(self):
        adapter, sink, turn_id = self.mid_reply()
        adapter.steer(TurnInput("now this", attachments=["/w/a.png"],
                                command_id="c2", device="mac"))
        ended = self.wait_turn(sink, turn_id)
        self.assertEqual(ended["data"]["status"], "completed")
        sent = self.steers_sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"]["content"], "@/w/a.png now this")
        mine = [e for e in sink.events if e.get("turn_id") == turn_id]
        self.assertEqual(mine[0]["type"], "turn.started")
        self.assertEqual(mine[-1]["type"], "turn.ended")
        users = [e for e in mine if e["type"] == "message.completed"
                 and e["data"]["kind"] == "user"]
        self.assertEqual(users[1]["data"], {
            "id": f"{turn_id}:user2", "kind": "user", "text": "now this",
            "by": "mac", "attachments": ["/w/a.png"]})
        self.assertEqual(users[1]["command_id"], "c2")
        replies = [e["data"]["text"] for e in mine
                   if e["type"] == "message.completed"
                   and e["data"]["kind"] == "assistant"]
        self.assertEqual(replies, ["1\n2\n", "steered"])
        self.assertEqual(sink.types().count("turn.ended"), 1)
        self.assertEqual(sink.of("error"), [])
        self.assertEqual(sink.events[-1]["data"]["state"], "idle")
        _streams_close(self, sink.events)
        for event in sink.events:
            self.assertEqual(events.validate(event), [], event)

    def test_steer_when_idle(self):
        adapter, sink, _sid = self.started()
        turn_id = adapter.send_turn(TurnInput("hi"))
        self.wait_turn(sink, turn_id)
        count = len(sink.events)
        with self.assertRaises(AdapterError) as ctx:
            adapter.steer(TurnInput("late"))
        self.assertEqual(ctx.exception.code, "idle")
        self.assertEqual(len(sink.events), count)
        self.assertEqual(self.steers_sent(), [])

    def test_steer_with_caps_off(self):
        adapter, sink, _turn = self.mid_reply(caps=Caps())
        count = len(sink.events)
        with self.assertRaises(Unsupported) as ctx:
            adapter.steer(TurnInput("x"))
        self.assertEqual(ctx.exception.feature, "steer")
        self.assertEqual(len(sink.events), count)
        self.assertEqual(self.steers_sent(), [])

    def test_a_cli_without_the_capability_string(self):
        for caps in ('["interrupt_receipt_v1", "msg_lifecycle_v1"]', "none"):
            with self.subTest(caps=caps):
                adapter, sink, _turn = self.mid_reply(
                    env_extra={"FAKE_CLAUDE_CAPS": caps})
                self.assertTrue(sink.of("session.caps")[0]["data"]["caps"]
                                ["steer"], "the handshake cannot say")
                narrowed = sink.of("session.caps")
                self.assertEqual(len(narrowed), 2)
                self.assertFalse(narrowed[-1]["data"]["caps"]["steer"])
                self.assertFalse(adapter.capabilities.steer)
                with self.assertRaises(Unsupported):
                    adapter.steer(TurnInput("x"))
                self.assertEqual(self.steers_sent(), [])

    def test_steer_before_the_first_init_waits_for_it(self):
        adapter, sink, _turn = self.mid_reply(
            env_extra={"FAKE_CLAUDE_NO_INIT": "1"}, call_timeout=0.3)
        with self.assertRaises(AdapterError) as ctx:
            adapter.steer(TurnInput("x"))
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertEqual(self.steers_sent(), [])
        self.assertEqual(len(sink.of("message.completed")), 1)

    def test_a_steer_the_init_arrives_for(self):
        import threading

        adapter, sink, turn_id = self.mid_reply()
        # As if the init had not come yet: the steer waits for it.
        adapter._state.init_seen = False
        adapter._init_seen.clear()

        def init_arrives():
            with adapter._lock:
                adapter._state.init_seen = True
            adapter._init_seen.set()

        timer = threading.Timer(0.05, init_arrives)
        timer.start()
        self.addCleanup(timer.cancel)
        adapter.steer(TurnInput("after the init"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(len(self.steers_sent()), 1)

    def test_steer_with_an_open_approval(self):
        adapter, sink, _sid = self.started("approval")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.wait(sink, "approval.requested")["data"]
        adapter.steer(TurnInput("never mind"))
        resolved = sink.of("approval.resolved")
        self.assertEqual([r["data"] for r in resolved],
                         [{"request_id": asked["request_id"],
                           "source": "superseded"}])
        at = sink.events.index(resolved[0])
        self.assertEqual(sink.events[at - 1]["data"]["text"], "never mind")
        self.assertEqual(sink.events[at + 1]["data"], {"state": "running"})
        with self.assertRaises(UnknownRequest):
            adapter.respond(asked["request_id"], Answer(decision="allow"))
        self.wait_turn(sink, turn_id)
        self.assertEqual(len(sink.of("approval.resolved")), 1,
                         "the CLI's cancel retires nothing twice")
        self.assertEqual([e["data"]["status"] for e in
                          sink.of("tool.completed")], ["cancelled"])
        self.assertEqual(sink.of("message.completed")[-1]["data"]["text"],
                         "steered")
        self.assertEqual(sink.of("turn.ended")[0]["data"]["status"],
                         "completed")
        replies = [m for m in self.sent() if m.get("type") ==
                   "control_response"]
        self.assertEqual(replies, [], "the withdrawn request is not answered")

    def test_steer_kills_a_running_tool(self):
        adapter, sink, _sid = self.started("slow-tool")
        turn_id = adapter.send_turn(TurnInput("go"))
        asked = self.wait(sink, "approval.requested")["data"]
        # The fake reads in order: the allow starts the call, which runs
        # until the steer behind it cuts it.
        adapter.respond(asked["request_id"], Answer(decision="allow"))
        adapter.steer(TurnInput("stop that"))
        self.assertEqual(self.wait_turn(sink, turn_id)["data"]["status"],
                         "completed")
        done = sink.of("tool.completed")[0]["data"]
        self.assertEqual((done["call_id"], done["status"]),
                         ("toolu_fake1", "cancelled"))
        self.assertIn("aborted", done["output"])

    def test_a_failed_write_emits_nothing(self):
        adapter, sink, _turn = self.mid_reply()
        count = len(sink.events)
        with mock.patch.object(adapter._writer, "send",
                               side_effect=claude_stream._Stalled()), \
                mock.patch.object(adapter, "_kill"):
            with self.assertRaises(AdapterError) as ctx:
                adapter.steer(TurnInput("x"))
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertEqual(len(sink.events), count)
        dead, sink2, _turn = self.mid_reply()
        dead._proc.stdin.close()
        count = len(sink2.events)
        with self.assertRaises(AdapterError) as ctx:
            dead.steer(TurnInput("x"))
        self.assertEqual(ctx.exception.code, "agent_down")
        self.assertFalse([e for e in sink2.events[count:]
                          if e["type"] == "message.completed"])

    def test_no_secret_reaches_an_event(self):
        key = self.KEY
        adapter, sink, turn_id = self.mid_reply(env_extra={
            "FAKE_CLAUDE_PARTIAL": f"1\nthe key is {key} and ",
            "FAKE_CLAUDE_STEER_REPLY": f"ok, not using {key}"})
        adapter.steer(TurnInput(f"stop, use {key}",
                                attachments=[f"/w/{key}.txt"]))
        self.wait_turn(sink, turn_id)
        self.assertIn(key, json.dumps(self.steers_sent()),
                      "the CLI itself gets the real text")
        self.assertNotIn(key, json.dumps(sink.events))
        texts = [e["data"]["text"] for e in sink.of("message.completed")]
        self.assertEqual(texts[1:], ["stop, use [REDACTED]",
                                     "1\nthe key is [REDACTED] and ",
                                     "ok, not using [REDACTED]"])
        _streams_close(self, sink.events)


if __name__ == "__main__":
    unittest.main()
