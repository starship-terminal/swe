"""G32: a running session's options (docs/specs/G32-session-options.md).

One test per row of the spec's table (each adapter crossed with each
option: listed or not, the value it reports, when a change takes
effect, what a refusal looks like), from the recorded fixtures where
one covers the row; then the keeper (the value stays true through a
change, a refusal, the agent's own change, a park and a keeper
restart), and the hub (the snapshot, the stream, ``/v1/status``).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from swe.remote import protocol, sync as sync_mod
from swe.remote.adapters import acp, claude_wire, codex_wire, opencode_wire
from swe.remote.adapters import events as ev
from swe.remote.adapters import fake, options as opts
from swe.remote.adapters.base import AdapterError, Caps
from swe.remote.adapters.core import ALL_CAPS

from tests.test_remote_keeper import KeeperCase, S, kill
from tests.test_remote_sse_v2 import V2Case

PROTOCOLS = Path(__file__).resolve().parent / "fixtures" / "protocols"


def _lines(adapter: str, version: str, scenario: str) -> list[dict]:
    path = PROTOCOLS / adapter / version / f"{scenario}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def _options(events: list[dict]) -> list[dict]:
    return [e["data"]["options"] for e in events
            if e["type"] == "session.options"]


def _values(events: list[dict], name: str) -> list:
    """Each value ``name`` took, in order, without repeats."""
    out: list = []
    for shown in _options(events):
        if name in shown and (not out or out[-1] != shown[name]["value"]):
            out.append(shown[name]["value"])
    return out


# ---------------------------------------------------------------------
# The table, per adapter


class TestClaude(unittest.TestCase):
    """claude_stream 2.1.328: model listed, permission mode and plan
    mode current only, effort not offered; every change sent at once."""

    def replay(self, scenario: str) -> list[dict]:
        return claude_wire.replay(_lines("claude_stream", "2.1.328",
                                         scenario))

    def test_model_is_listed_and_shown_as_the_listed_id(self):
        events = self.replay("set_model")
        first = _options(events)[0]["model"]
        # The handshake's own list, in its order: nothing written here.
        self.assertEqual([c["id"] for c in first["choices"]],
                         ["default", "opus", "fable", "sonnet", "haiku"])
        self.assertEqual(first["mid_turn"], "unverified")
        # The CLI answers ``sonnet`` with ``claude-sonnet-5-5``; the
        # listed id is the value, the resolved one ``reported``.
        self.assertEqual(_values(events, "model"),
                         ["haiku", "sonnet", "haiku"])
        last = _options(events)[-1]["model"]
        self.assertEqual((last["value"], last["reported"]),
                         ("haiku", "claude-haiku-5-5"))

    def test_effort_is_never_offered(self):
        # No control request changes it mid-session (G7): PROVEN keeps
        # it out of the caps, so no event names it.
        self.assertNotIn("effort", claude_wire.PROVEN.options)
        for scenario in ("hello", "set_model", "plan_mode"):
            for shown in _options(self.replay(scenario)):
                self.assertNotIn("effort", shown)

    def test_permission_and_plan_mode_follow_set_permission_mode(self):
        events = self.replay("plan_mode")
        self.assertEqual(_values(events, "plan_mode"), [False, True, False])
        self.assertEqual(_values(events, "permission_mode"),
                         ["default", "plan", "default"])
        # The CLI lists no permission modes: the value only.
        self.assertNotIn("choices", _options(events)[0]["permission_mode"])

    def test_the_cli_leaving_plan_mode_by_itself_is_shown(self):
        # plan_approve: started in plan mode, the plan approved, and the
        # CLI's own status line says ``default``: nothing of ours set it.
        lines = _lines("claude_stream", "2.1.328", "plan_approve")
        sent = [line["msg"]["request"]["subtype"] for line in lines
                if line.get("dir") == "out"
                and line.get("msg", {}).get("type") == "control_request"]
        self.assertNotIn("set_permission_mode", sent)
        self.assertEqual(_values(self.replay("plan_approve"), "plan_mode"),
                         [True, False])

    def test_display_names_are_taken_where_the_cli_sends_them(self):
        hs = claude_wire.Handshake.from_body({"models": [
            {"value": "sonnet", "displayName": "Sonnet 5.5",
             "description": "everyday"},
            {"value": "haiku", "displayName": "haiku"},
            {"displayName": "no value"}]})
        self.assertEqual(hs.models, ("sonnet", "haiku"))
        self.assertEqual(hs.model_choices, (
            {"id": "sonnet", "label": "Sonnet 5.5",
             "description": "everyday"},
            {"id": "haiku"}))

    def state(self) -> claude_wire.StreamState:
        st = claude_wire.StreamState("claude:x", clock=lambda: 1.0,
                                     run_id="aaaa0001")
        st.model_choices = (opts.choice("default"), opts.choice("sonnet"))
        st.start_events(claude_wire.PROVEN, "sid")
        return st

    def test_a_model_the_cli_switched_to_by_itself_shows_as_it_is(self):
        st = self.state()
        out = st.feed({"type": "system", "subtype": "init",
                       "model": "claude-opus-5-5"}).events
        self.assertEqual(_options(out)[-1]["model"]["value"], "default",
                         "no --model: the first model named is the default")
        # A later init naming a model nobody asked for (a fallback):
        # shown as the CLI names it, matching no listed id.
        out = st.feed({"type": "system", "subtype": "init",
                       "model": "claude-sonnet-4-9"}).events
        self.assertEqual(_options(out)[-1]["model"]["value"],
                         "claude-sonnet-4-9")

    def test_a_refused_set_model_changes_nothing(self):
        st = self.state()
        st.model_asked("r1", "sonnet")
        fed = st.feed({"type": "control_response", "response": {
            "subtype": "error", "request_id": "r1",
            "error": "Model 'sonnet' is not available"}})
        self.assertEqual(_options(fed.events), [])
        self.assertEqual(fed.acks[0].error, "Model 'sonnet' is not available")


class TestAcp(unittest.TestCase):
    """acp (qwen-code 0.28.0): models and modes listed by the agent,
    plan mode is its ``plan`` mode, effort not offered."""

    def replay(self, scenario: str) -> list[dict]:
        return acp.replay(_lines("acp", "qwen-code-0.28.0", scenario))

    def test_models_are_the_agents_list(self):
        events = self.replay("model")
        model = _options(events)[0]["model"]
        self.assertEqual(model["choices"], [{
            "id": "$runtime|openai|gpt-4o-mini(openai)",
            "label": "gpt-4o-mini"}])
        self.assertEqual(model["value"], "$runtime|openai|gpt-4o-mini(openai)")
        self.assertEqual(model["mid_turn"], "unverified")
        for shown in _options(events):
            self.assertNotIn("effort", shown)

    def test_modes_are_the_agents_list_and_plan_mode_is_one_of_them(self):
        events = self.replay("plan_mode")
        mode = _options(events)[0]["permission_mode"]
        self.assertEqual([c["id"] for c in mode["choices"]],
                         ["plan", "default", "auto-edit", "auto", "yolo"])
        self.assertEqual(mode["choices"][0]["label"], "Plan")
        self.assertEqual(_values(events, "permission_mode"),
                         ["auto", "plan", "auto"])
        self.assertEqual(_values(events, "plan_mode"), [False, True, False])

    def test_the_agent_changing_its_own_mode_is_shown(self):
        st = acp.AcpState("acp:x", clock=lambda: 1.0, run_id="aaaa0001")
        st.session_info({"sessionId": "s", "modes": {
            "currentModeId": "plan", "availableModes": [
                {"id": "plan"}, {"id": "default"}]}})
        st.session_id = "s"
        st.start_events(ALL_CAPS.narrowed(acp.PROVEN), "s")
        out = st.update({"sessionId": "s", "update": {
            "sessionUpdate": "current_mode_update",
            "currentModeId": "default"}}).events
        self.assertEqual(_options(out)[-1]["plan_mode"]["value"], False)


class TestCodex(unittest.TestCase):
    """codex_appserver 0.170.0: model and effort current only, the
    approval policies the API's enum; every change waits for the next
    turn."""

    def test_values_follow_the_turns_that_carry_them(self):
        events = codex_wire.replay(_lines("codex_appserver", "0.170.0",
                                          "options"))
        first = _options(events)[0]
        self.assertEqual(set(first), {"model", "effort", "permission_mode"},
                         "no plan mode")
        for shown in _options(events):
            for option in shown.values():
                self.assertEqual(option["mid_turn"], "next_turn")
        self.assertNotIn("choices", first["model"])
        self.assertEqual([c["id"] for c in first["permission_mode"]
                          ["choices"]], list(codex_wire.APPROVAL_POLICIES))
        self.assertEqual(_values(events, "model"),
                         ["gpt-5.6-luna", "gpt-6-luna"])
        self.assertEqual(_values(events, "permission_mode"),
                         ["untrusted", "never"])


class TestOpencode(unittest.TestCase):
    """opencode_serve 2.3.2: model and its variant (effort) current
    only, plan mode the ``plan`` agent, no permission mode."""

    def replay(self, scenario: str) -> list[dict]:
        return opencode_wire.replay(_lines("opencode_serve", "2.3.2",
                                           scenario))

    def test_model_and_effort(self):
        events = self.replay("options")
        first = _options(events)[0]
        self.assertEqual(set(first), {"model", "effort", "plan_mode"})
        self.assertNotIn("choices", first["model"])
        self.assertEqual(_values(events, "model"),
                         ["openai/gpt-5.4-nano", "openai/gpt-5.4-mini"])
        self.assertEqual(_values(events, "effort"), ["low", "medium", "low"])

    def test_plan_mode_is_the_plan_agent(self):
        self.assertEqual(_values(self.replay("plan_mode"), "plan_mode"),
                         [False, True, False])

    def test_the_server_switching_agent_by_itself_is_shown(self):
        st = opencode_wire.ServeState("oc:x", clock=lambda: 1.0,
                                      run_id="aaaa0001")
        st.session_id = "ses_1"
        st.agent = "plan"
        st.start_events(ALL_CAPS.narrowed(opencode_wire.PROVEN), "ses_1")
        out = st.feed({"type": "session.agent.selected", "data": {
            "sessionID": "ses_1", "agent": "build"}}).events
        self.assertEqual(_options(out)[-1]["plan_mode"]["value"], False)


class TestOptionsView(unittest.TestCase):
    def test_only_what_the_caps_allow_and_only_on_a_change(self):
        view = opts.OptionsView()
        offered = {"model": opts.string_option("a", opts.UNVERIFIED),
                   "plan_mode": opts.flag_option(False, opts.UNVERIFIED)}
        caps = Caps(options=frozenset({"model"}))
        first = view.changed("k", offered, caps, 1.0)
        self.assertEqual(set(first["data"]["options"]), {"model"})
        self.assertIsNone(view.changed("k", offered, caps, 2.0))
        # Nothing to offer, never offered: no event at all.
        self.assertIsNone(opts.OptionsView().changed("k", offered, Caps(), 1))


# ---------------------------------------------------------------------
# The keeper keeps the value true


CAPS = Caps(plan_mode=True, options=frozenset({"model", "permission_mode"}))


class TestKeeper(KeeperCase):
    def plan(self, **kw) -> dict:
        return {"caps": CAPS, "choices": {"model": ["sonnet", "haiku"]},
                **kw}

    def test_the_options_at_start_in_the_log_the_welcome_and_status(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        shown = feed.event("session.options")["data"]["options"]
        self.assertEqual(set(shown), {"model", "permission_mode",
                                      "plan_mode"})
        self.assertEqual([c["id"] for c in shown["model"]["choices"]],
                         ["sonnet", "haiku"])
        self.assertEqual(feed.client.welcome["options"], shown)
        self.assertEqual(self.status(feed)["options"], shown)

    def test_a_change_is_logged_before_its_result(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("set_option", command_id="m", name="model",
                            value="haiku")
        result = feed.result("m")
        self.assertTrue(result["data"]["ok"])
        changed = [e for e in feed.events("session.options")
                   if e["data"]["options"]["model"]["value"] == "haiku"]
        self.assertEqual(len(changed), 1)
        self.assertLess(changed[0]["seq"], result["seq"])
        self.assertEqual(self.status(feed)["options"]["model"]["value"],
                         "haiku")

    def test_a_refused_change_leaves_the_value_and_says_why(self):
        keeper = self.keeper(self.plan(refuse_options={
            "model": "Model 'haiku' is not available"}))
        feed = self.connect(keeper)
        feed.live()
        before = len(feed.events("session.options"))
        feed.client.request("set_option", command_id="m", name="model",
                            value="haiku")
        result = feed.result("m")
        self.assertEqual((result["data"]["ok"], result["data"]["note"]),
                         (False, "Model 'haiku' is not available"))
        self.assertEqual(len(feed.events("session.options")), before)

    def test_the_agent_changing_its_own_option_is_logged(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        feed.live()
        self.adapters[0].agent_sets("plan_mode", True)
        shown = feed.until(
            lambda f: f["t"] == "event"
            and f["e"]["type"] == "session.options"
            and f["e"]["data"]["options"]["plan_mode"]["value"] is True,
            "plan mode on")["e"]
        self.assertTrue(shown["data"]["options"]["plan_mode"]["value"])

    def test_a_change_while_parked_shows_what_the_next_start_uses(self):
        keeper = self.keeper(self.plan(), self.plan())
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("park", command_id="p")
        feed.result("p")
        feed.client.request("set_option", command_id="m", name="model",
                            value="sonnet")
        result = feed.result("m")
        self.assertEqual(result["data"]["note"],
                         "applies when the agent starts")
        shown = feed.events("session.options")[-1]["data"]["options"]
        self.assertEqual(shown["model"]["value"], "sonnet")
        self.assertLess(feed.events("session.options")[-1]["seq"],
                        result["seq"])
        # The next start says what the agent made of it.
        self.send(feed)
        feed.event("turn.ended")
        self.assertEqual(self.adapters[1].spec.model, "sonnet")
        self.assertEqual(self.status(feed)["options"]["model"]["value"],
                         "sonnet")

    def test_choosing_a_permission_mode_while_parked_leaves_plan_mode(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        feed.live()
        self.adapters[0].agent_sets("plan_mode", True)
        feed.client.request("park", command_id="p")
        feed.result("p")
        feed.client.request("set_option", command_id="m",
                            name="permission_mode", value="default")
        feed.result("m")
        shown = self.status(feed)["options"]
        self.assertEqual((shown["permission_mode"]["value"],
                          shown["plan_mode"]["value"]), ("default", False))

    def test_a_new_keeper_reads_the_options_back_from_the_log(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("set_option", command_id="m", name="model",
                            value="haiku")
        feed.result("m")
        kill(keeper)
        feed.closed()
        refuse = {"on": True}
        make = self.factory

        def failing():
            if refuse["on"]:
                raise AdapterError("claude did not start", code="agent_down")
            return make()

        self.factory = failing
        second = self.keeper()
        feed = self.connect(second)
        feed.live()
        # The agent is down, and the welcome still says what was in
        # force: the log's last session.options.
        self.assertEqual(feed.client.welcome["agent"], "down")
        self.assertEqual(feed.client.welcome["options"]["model"]["value"],
                         "haiku")

    def test_an_agent_with_narrower_caps_offers_less(self):
        keeper = self.keeper(self.plan(), {"caps": Caps()})
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("park", command_id="p")
        feed.result("p")
        self.send(feed)
        feed.event("turn.ended")
        self.assertEqual(self.status(feed)["options"], {})


# ---------------------------------------------------------------------
# The hub


class TestHub(V2Case):
    def test_status_lists_the_feature(self):
        status, body = self.call("GET", "/v1/status")
        self.assertIn(protocol.FEATURE_SESSION_OPTIONS, body["features"])

    def test_the_snapshot_and_the_stream_carry_the_options(self):
        self.plans.append({"choices": {"model": ["sonnet", "haiku"]}})
        key = self.launch()
        self.drain(key)
        body = self.snapshot(key)
        self.assertEqual(set(body["options"]), {"model", "plan_mode"})
        self.assertEqual(body["options"]["model"]["value"], "")
        status, out = self.call("POST", f"/v1/sessions/{key}/command", {
            "op": "set_option", "name": "model", "value": "haiku",
            "command_id": "w-1"})
        self.assertEqual((status, out["receipt"]), (200, "accepted"))
        self.feed.result("w-1")
        self.drain(key)
        body = self.snapshot(key)
        self.assertEqual(body["options"]["model"]["value"], "haiku")
        # A v2 stream replays the change as an event (not a timeline
        # item: the snapshot's messages leave it out).
        self.assertNotIn("session.options",
                         [m["type"] for m in body["messages"]])
        frames = self.caught_up(self.sse(key, last=1, epoch=body["epoch"]))
        self.assertIn("session.options", [f["type"] for f in frames])

    def test_a_kept_session_reads_the_keepers_copy(self):
        # The runtime's copy (from the keeper's welcome and its events)
        # wins over the journal's fold: the journal is a ring that can
        # have dropped the last session.options of a long session.
        self.plans.append({"choices": {"model": ["sonnet"]}})
        key = self.launch()
        self.drain(key)
        owned = self.srv.state.runtime.session(key)
        self.assertEqual(owned.offered, self.snapshot(key)["options"])
        owned.offered = {"model": opts.string_option("kept", opts.UNVERIFIED)}
        self.assertEqual(self.snapshot(key)["options"]["model"]["value"],
                         "kept")

    def test_a_refused_option_is_answered_with_the_reason(self):
        key = self.launch()
        self.drain(key)
        status, out = self.call("POST", f"/v1/sessions/{key}/command", {
            "op": "set_option", "name": "effort", "value": "high",
            "command_id": "w-2"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "command_rejected"))


class TestFold(unittest.TestCase):
    def test_a_session_no_keeper_here_holds_reads_the_journal(self):
        # A satellite keeper's session: the hub's copy of its journal
        # is all there is, so the snapshot folds it.
        fold = sync_mod.Fold()
        shown = {"model": opts.string_option("x", opts.NEXT_TURN)}
        fold.add(ev.session_options("k", shown))
        self.assertEqual(fold.view()["options"], shown)
        self.assertIn("session.options", sync_mod.BOOKKEEPING_TYPES)


if __name__ == "__main__":
    unittest.main()
