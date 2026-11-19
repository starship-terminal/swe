"""Session adapter resolution (remote/adapters/registry.py) over
harness.json's ``capabilities.session`` and ``SESSION_FALLBACK``."""

import unittest
from unittest import mock

from swe.harness.catalog import HARNESS_CATALOG
from swe.remote import acp
from swe.remote.adapters import fake, registry
from swe.remote.adapters.base import Caps
from swe.remote.adapters.fallback import SESSION_FALLBACK

FALLBACK_IDS = ("claude_stream", "codex_appserver", "opencode_serve", "acp")


def on_path(binary):
    return f"/usr/bin/{binary}"


def nowhere(binary):
    return None


def session(entry):
    return {"capabilities": {"session": entry}}


class RegistryCase(unittest.TestCase):
    def setUp(self):
        # Every id the fallback names gets a class, so resolution reaches
        # the precedence rules; each test can unregister one again.
        for adapter_id in FALLBACK_IDS + ("fake",):
            registry.register(adapter_id, fake.FakeAdapter, replace=True)
            self.addCleanup(registry.unregister, adapter_id)

    def resolve(self, harness, reg, which=on_path):
        return registry.adapter_for(harness, reg, which=which)


class TestPrecedence(RegistryCase):
    def test_registry_entry_wins(self):
        reg = {"codex": session({"adapter": "fake", "argv": ["fakeagent"],
                                 "min_version": "1.2", "steer": True,
                                 "options": ["model", "plan_mode", "x"]})}
        got = self.resolve("codex", reg)
        self.assertTrue(got.ok)
        self.assertEqual(got.adapter, "fake")
        self.assertEqual(got.argv, ("fakeagent",))
        self.assertEqual(got.min_version, "1.2")
        self.assertEqual(got.source, "registry")
        self.assertEqual(got.caps, Caps(steer=True, options={"model"}))

    def test_registry_fields_override_the_fallback_one_by_one(self):
        reg = {"codex": session({"steer": True})}
        got = self.resolve("codex", reg)
        self.assertEqual(got.adapter, "codex_appserver")
        self.assertEqual(got.argv, ("codex", "app-server"))
        self.assertTrue(got.caps.steer)
        self.assertEqual(got.source, "registry")

    def test_registry_describes_a_harness_the_table_lacks(self):
        reg = {"pi": session({"adapter": "acp", "argv": ["pi", "--acp"]})}
        got = self.resolve("pi", reg)
        self.assertEqual((got.adapter, got.argv), ("acp", ("pi", "--acp")))

    def test_supported_false_beats_the_fallback(self):
        got = self.resolve("codex", {"codex": session({"supported": False})})
        self.assertFalse(got.ok)
        self.assertIsInstance(got, registry.NoAdapter)
        self.assertEqual(got.source, "registry")
        self.assertIn("read-only", got.reason)
        why = self.resolve("codex", {"codex": session({
            "supported": False, "reason": "  pinned to an old build  "})})
        self.assertEqual(why.reason, "pinned to an old build")

    def test_fallback_only(self):
        for harness in ("claude", "opencode", "gemini"):
            got = self.resolve(harness, {})
            self.assertTrue(got.ok, harness)
            self.assertEqual(got.source, "fallback")
            self.assertEqual(got.caps, Caps(), "nothing is on until proven")
        self.assertEqual(self.resolve("gemini", {}).argv, None)
        self.assertEqual(self.resolve("claude", {}).adapter, "claude_stream")

    def test_unknown_harness(self):
        got = self.resolve("pi", {"pi": {"capabilities": {}}})
        self.assertFalse(got.ok)
        self.assertEqual(got.source, "unknown")
        self.assertIn("pi", got.reason)
        self.assertEqual(got.adapter, "")

    def test_malformed_fields_count_as_undeclared(self):
        for bad in ({"adapter": "Has Spaces"}, {"adapter": 3},
                    {"argv": []}, {"argv": ["ok", ""]}, {"argv": "codex"},
                    {"steer": "yes"}, {"steer": 1}, {"options": "model"},
                    {"min_version": "  "}):
            got = self.resolve("codex", {"codex": session(bad)})
            self.assertTrue(got.ok, bad)
            self.assertEqual(got.adapter, "codex_appserver", bad)
            self.assertEqual(got.argv, ("codex", "app-server"), bad)
            self.assertEqual(got.caps, Caps(), bad)
            self.assertEqual(got.source, "fallback", bad)
        # Not a dict at any level: the fallback answers.
        for reg in ({"codex": "x"}, {"codex": {"capabilities": "x"}},
                    {"codex": session("x")}, ["not", "a", "dict"]):
            self.assertTrue(self.resolve("codex", reg).ok, reg)

    def test_registry_none_reads_harness_json(self):
        reg = {"codex": session({"supported": False})}
        with mock.patch("swe.harness.registry.load_registry_readonly",
                        return_value=reg):
            self.assertFalse(registry.adapter_for("codex", which=on_path).ok)
        with mock.patch("swe.harness.registry.load_registry_readonly",
                        side_effect=ValueError("bad json")):
            self.assertTrue(registry.adapter_for("codex", which=on_path).ok)


class TestAvailability(RegistryCase):
    def test_no_class_registered(self):
        registry.unregister("codex_appserver")
        got = self.resolve("codex", {})
        self.assertFalse(got.ok)
        self.assertEqual(got.adapter, "codex_appserver")
        self.assertEqual(got.source, "fallback")
        self.assertIn("no codex_appserver session adapter", got.reason)

    def test_binary_not_on_path(self):
        got = self.resolve("codex", {}, which=nowhere)
        self.assertFalse(got.ok)
        self.assertEqual(got.reason, "codex is not installed on this machine")
        # No argv to check: the adapter probes for itself.
        self.assertTrue(self.resolve("gemini", {}, which=nowhere).ok)
        self.assertTrue(self.resolve("codex", {}, which=None).ok)

    def test_today_nothing_is_registered(self):
        for adapter_id in FALLBACK_IDS + ("fake",):
            registry.unregister(adapter_id)
        for harness in SESSION_FALLBACK:
            got = registry.adapter_for(harness, {}, which=on_path)
            self.assertFalse(got.ok, harness)


class TestRegistration(unittest.TestCase):
    def test_register_create_unregister(self):
        registry.register("fake", fake.FakeAdapter)
        self.addCleanup(registry.unregister, "fake")
        registry.register("fake", fake.FakeAdapter)  # same class: fine
        self.assertIn("fake", registry.registered())
        with self.assertRaises(ValueError):
            registry.register("fake", lambda resolved: None)
        reg = {"x": session({"adapter": "fake", "rollback": True})}
        got = registry.adapter_for("x", reg, which=None)
        adapter = registry.create(got, session_id="s-9")
        self.assertIsInstance(adapter, fake.FakeAdapter)
        self.assertTrue(adapter.capabilities.rollback)
        self.assertEqual(adapter.session_id, "s-9")
        registry.unregister("fake")
        registry.unregister("fake")
        with self.assertRaises(LookupError):
            registry.create(got)

    def test_register_checks_its_arguments(self):
        with self.assertRaises(ValueError):
            registry.register("Bad-Id", fake.FakeAdapter)
        with self.assertRaises(ValueError):
            registry.register("", fake.FakeAdapter)
        with self.assertRaises(TypeError):
            registry.register("ok", "not callable")

    def test_builtins_load_once(self):
        with mock.patch.object(registry, "_BUILTINS_LOADED", False), \
                mock.patch.object(registry, "BUILTIN_MODULES", ("fake",)), \
                mock.patch("importlib.import_module") as imp:
            registry.registered()
            registry.registered()
        imp.assert_called_once_with("swe.remote.adapters.fake")


class TestFallbackTable(unittest.TestCase):
    def test_keys_are_registry_ids(self):
        for name in SESSION_FALLBACK:
            self.assertIn(name, HARNESS_CATALOG, name)

    def test_names_only_channels_that_exist(self):
        for name, row in SESSION_FALLBACK.items():
            self.assertIn(row["adapter"], FALLBACK_IDS, name)
            self.assertEqual(set(row) - {"adapter", "argv"}, set(),
                             "no optional feature is on in the fallback")

    def test_acp_rows_follow_acp_fallback(self):
        acp_rows = {n for n, r in SESSION_FALLBACK.items()
                    if r["adapter"] == "acp"}
        self.assertEqual(acp_rows, set(acp.ACP_FALLBACK) - {"claude"})

    def test_read_only(self):
        with self.assertRaises(TypeError):
            SESSION_FALLBACK["pi"] = {}
        with self.assertRaises(TypeError):
            SESSION_FALLBACK["codex"]["steer"] = True


if __name__ == "__main__":
    unittest.main()
