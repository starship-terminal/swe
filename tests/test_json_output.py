"""`--json` output: the contract the macOS app reads.

Every command that takes --json prints exactly one JSON document on stdout,
shaped ``{"schema": 1, "data": ...}``. Nothing else may reach stdout: the
app decodes the whole stream, so a stray header line or ANSI escape breaks
every page that depends on it.

These run the real CLI in a subprocess against a throwaway HOME, because
swe resolves ~/.swe once at import time. An in-process test would
read the developer's own registry.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests._tempdir import temp_dir

SRC = Path(__file__).resolve().parents[1] / "src"


def run_swe(
    home: Path, *args: str, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    # A clean environment, so the developer's CLAUDE_CONFIG_DIR, CODEX_HOME
    # and friends cannot point the child at real configs. Coverage's own
    # variables pass through: .coveragerc patches subprocesses, and without
    # them the child's lines would go unmeasured.
    env = {
        key: value for key, value in os.environ.items()
        if key.startswith("COVERAGE_")
    }
    env.update({
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(SRC),
        "NO_COLOR": "1",
    })
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "swe.cli", *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def write_registry(home: Path, registry: dict) -> None:
    config = home / ".swe" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "harness.json").write_text(json.dumps(registry))


def decode(result: subprocess.CompletedProcess) -> dict:
    """The whole of stdout must be one JSON document."""
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - failure path
        raise AssertionError(
            f"stdout is not JSON ({exc}):\n{result.stdout}\n"
            f"stderr:\n{result.stderr}") from exc


REGISTRY = {
    "claude": {
        "command": "claude", "description": "Anthropic CLI",
        "aliases": ["cc"], "tags": ["vendor"],
        "state": "starred", "pin": 1,
    },
    "codex": {
        "command": "codex", "description": "OpenAI CLI",
        "aliases": ["cx"], "tags": ["vendor"],
    },
}


class ListJsonTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        write_registry(self.home, REGISTRY)

    def test_list_json_is_an_enveloped_list_of_harnesses(self):
        result = run_swe(self.home, "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = decode(result)
        self.assertEqual(doc["schema"], 1)
        rows = {row["name"]: row for row in doc["data"]["harnesses"]}
        self.assertEqual(set(rows), {"claude", "codex"})
        self.assertEqual(rows["claude"]["command"], "claude")
        self.assertEqual(rows["claude"]["aliases"], ["cc"])
        self.assertEqual(rows["claude"]["state"], "starred")
        self.assertEqual(rows["codex"]["state"], "active")

    def test_list_json_without_usage_carries_no_rate_limits(self):
        doc = decode(run_swe(self.home, "list", "--json"))
        for row in doc["data"]["harnesses"]:
            self.assertIsNone(row["rate_limit"])

    def test_a_tag_filter_narrows_the_rows(self):
        registry = dict(REGISTRY, gemini={"command": "gemini", "tags": []})
        write_registry(self.home, registry)
        doc = decode(run_swe(self.home, "list", "vendor", "--json"))
        self.assertEqual(
            sorted(row["name"] for row in doc["data"]["harnesses"]),
            ["claude", "codex"])

    def test_refusals_leave_stdout_empty(self):
        for args in (["--json", "--bogus"], ["--json", "--scope=bogus"],
                     ["--json", "--scope"], ["a", "b", "--json"]):
            with self.subTest(args=args):
                result = run_swe(self.home, "list", *args)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertNotEqual(result.stderr.strip(), "")

    def test_json_with_edit_or_legend_is_refused_before_any_write(self):
        config = self.home / ".swe" / "config" / "config.json"
        config.write_text(json.dumps({"list": {"session_window": 7}}))
        before = config.read_text()
        for args in (["edit", "--reset", "--json"], ["legend", "--json"]):
            with self.subTest(args=args):
                result = run_swe(self.home, "list", *args)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("--json", result.stderr)
        self.assertEqual(config.read_text(), before)

    def test_session_count_is_100_days_whatever_the_table_window(self):
        import time

        # The table can be set to a 7-day window; the JSON field promises 100.
        (self.home / ".swe" / "config" / "config.json").write_text(
            json.dumps({"list": {"session_window": 7}}))
        session = (self.home / ".claude" / "projects" / "-work" /
                   "0b7c2a52-9f1e-4f43-8f43-3a8e6b1c2d01.jsonl")
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({
            "type": "user", "cwd": "/work",
            "sessionId": "0b7c2a52-9f1e-4f43-8f43-3a8e6b1c2d01",
            "message": {"role": "user", "content": "hi"},
        }) + "\n")
        thirty_days_ago = time.time() - 30 * 86400
        os.utime(session, (thirty_days_ago, thirty_days_ago))
        rows = {row["name"]: row for row in
                decode(run_swe(self.home, "list", "--json"))["data"]["harnesses"]}
        self.assertEqual(rows["claude"]["sessions_100d"], 1)


class ListNotesJsonTest(unittest.TestCase):
    """The app's edit sheet prefills from `swe list --json`, notes included."""

    def test_notes_is_null_when_unset_and_the_text_when_set(self):
        home = temp_dir(self)
        write_registry(home, dict(
            REGISTRY, codex=dict(REGISTRY["codex"], notes="  daily driver ")))
        rows = {row["name"]: row for row in
                decode(run_swe(home, "list", "--json"))["data"]["harnesses"]}
        self.assertIn("notes", rows["claude"])
        self.assertIsNone(rows["claude"]["notes"])
        self.assertEqual(rows["codex"]["notes"], "  daily driver ")

    def test_an_empty_notes_string_reads_as_null(self):
        home = temp_dir(self)
        write_registry(home, {"claude": dict(REGISTRY["claude"], notes="")})
        row = decode(run_swe(home, "list", "--json"))["data"]["harnesses"][0]
        self.assertIsNone(row["notes"])


class ListDisplayNameJsonTest(unittest.TestCase):
    """`swe list --json` rows carry `display_name`, next to `name`."""

    def rows(self, registry):
        home = temp_dir(self)
        write_registry(home, registry)
        return {row["name"]: row for row in
                decode(run_swe(home, "list", "--scope=all", "--json")
                       )["data"]["harnesses"]}

    def test_display_name_is_null_when_unset_and_the_text_when_set(self):
        rows = self.rows(dict(
            REGISTRY, codex=dict(REGISTRY["codex"], display_name="Codex CLI")))
        self.assertIn("display_name", rows["claude"])
        self.assertIsNone(rows["claude"]["display_name"])
        self.assertEqual(rows["codex"]["display_name"], "Codex CLI")

    def test_an_empty_or_blank_display_name_reads_as_null(self):
        rows = self.rows({
            "claude": dict(REGISTRY["claude"], display_name=""),
            "codex": dict(REGISTRY["codex"], display_name="   "),
        })
        self.assertIsNone(rows["claude"]["display_name"])
        self.assertIsNone(rows["codex"]["display_name"])

    def test_display_name_sits_next_to_name(self):
        row = self.rows(dict(REGISTRY))["claude"]
        keys = list(row)
        self.assertEqual(keys[keys.index("name") + 1], "display_name")


class ListPrimaryColorJsonTest(unittest.TestCase):
    """`primary_color`: the entry's own value, then the catalog, then null.

    The catalog fallback resolves at read time: a registry written before
    the field existed must not need a rewrite to show its colour.
    """

    def rows(self, registry):
        home = temp_dir(self)
        write_registry(home, registry)
        return {row["name"]: row for row in
                decode(run_swe(home, "list", "--json"))["data"]["harnesses"]}

    def test_an_entry_without_the_field_gets_the_catalog_colour(self):
        rows = self.rows({"claude": {"command": "claude", "tags": []}})
        self.assertEqual(rows["claude"]["primary_color"], "#D97757")

    def test_an_entry_own_colour_wins_over_the_catalog(self):
        rows = self.rows({"claude": {"command": "claude",
                                     "primary_color": "#112233"}})
        self.assertEqual(rows["claude"]["primary_color"], "#112233")

    def test_an_uncatalogued_harness_without_a_colour_is_null(self):
        rows = self.rows({"mytool": {"command": "mytool"}})
        self.assertIn("primary_color", rows["mytool"])
        self.assertIsNone(rows["mytool"]["primary_color"])

    def test_edit_sets_normalises_and_clears_the_colour(self):
        home = temp_dir(self)
        write_registry(home, {"claude": {"command": "claude", "tags": []}})

        result = run_swe(home, "edit", "claude", "--primary-color=d97757")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(
            (home / ".swe" / "config" / "harness.json").read_text())
        self.assertEqual(saved["claude"]["primary_color"], "#D97757")
        rows = {row["name"]: row for row in
                decode(run_swe(home, "list", "--json"))["data"]["harnesses"]}
        self.assertEqual(rows["claude"]["primary_color"], "#D97757")

        # Clearing drops the key, and the catalog default takes over again.
        result = run_swe(home, "edit", "claude", "--primary-color=")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(
            (home / ".swe" / "config" / "harness.json").read_text())
        self.assertNotIn("primary_color", saved["claude"])
        rows = {row["name"]: row for row in
                decode(run_swe(home, "list", "--json"))["data"]["harnesses"]}
        self.assertEqual(rows["claude"]["primary_color"], "#D97757")

    def test_edit_rejects_a_bad_colour(self):
        home = temp_dir(self)
        write_registry(home, {"claude": {"command": "claude"}})
        result = run_swe(home, "edit", "claude", "--primary-color=notacolour")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Use a colour like #D97757.", result.stderr + result.stdout)
        saved = json.loads(
            (home / ".swe" / "config" / "harness.json").read_text())
        self.assertNotIn("primary_color", saved["claude"])

    def test_info_shows_the_effective_colour(self):
        home = temp_dir(self)
        write_registry(home, {"claude": {"command": "claude"}})
        result = run_swe(home, "info", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Colour:", result.stdout)
        self.assertIn("#D97757", result.stdout)

    def test_re_adding_a_harness_keeps_the_chosen_colour(self):
        home = temp_dir(self)
        write_registry(home, {"claude": {"command": "claude", "tags": []}})

        result = run_swe(home, "edit", "claude", "--primary-color=#102030")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_swe(home, "add", "claude", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)

        rows = {row["name"]: row for row in
                decode(run_swe(home, "list", "--json"))["data"]["harnesses"]}
        self.assertEqual(rows["claude"]["primary_color"], "#102030")
        result = run_swe(home, "info", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("#102030", result.stdout)


class EditJsonTest(unittest.TestCase):
    """`swe edit <name> --field=value --json`: the app's edit sheet."""

    def setUp(self):
        self.home = temp_dir(self)
        write_registry(self.home, {
            "claude": {
                "command": "claude", "description": "Anthropic CLI",
                "aliases": ["cc"], "tags": ["vendor", "coding"],
                "version": "1.2.3", "notes": "daily driver",
                "state": "starred", "pin": 1,
            },
            "codex": {
                "command": "codex", "description": "OpenAI CLI",
                "aliases": ["cx"], "tags": ["vendor"],
            },
        })

    def edit(self, *args):
        return run_swe(self.home, "edit", *args, "--json")

    def listed(self, name):
        rows = decode(run_swe(self.home, "list", "--scope=all", "--json")
                      )["data"]["harnesses"]
        return next(row for row in rows if row["name"] == name)

    def saved(self, name):
        path = self.home / ".swe" / "config" / "harness.json"
        return json.loads(path.read_text())[name]

    def assertRefused(self, result, needle=None):
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotEqual(result.stderr.strip(), "")
        if needle:
            self.assertIn(needle, result.stderr)

    def test_multi_field_edit_reports_name_changes_and_harness(self):
        result = self.edit("claude", "--description=Claude Code",
                           "--tags=a,b", "--version=2.0", "--notes=hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = decode(result)
        self.assertEqual(doc["schema"], 1)
        data = doc["data"]
        self.assertEqual(set(data), {"name", "changes", "harness"})
        self.assertEqual(data["name"], "claude")
        self.assertEqual(data["changes"],
                         ["description", "tags", "version", "notes"])
        harness = data["harness"]
        self.assertEqual(harness["description"], "Claude Code")
        self.assertEqual(harness["tags"], ["a", "b"])
        self.assertEqual(harness["version"], "2.0")
        self.assertEqual(harness["notes"], "hello")
        # Untouched fields survive, state included.
        self.assertEqual(harness["aliases"], ["cc"])
        self.assertEqual(harness["command"], "claude")
        self.assertEqual(harness["state"], "starred")

    def test_harness_is_exactly_the_list_row(self):
        data = decode(self.edit("claude", "--description=Changed"))["data"]
        self.assertEqual(data["harness"], self.listed("claude"))

    def test_the_edit_is_persisted(self):
        self.edit("claude", "--aliases=cc,claude-code", "--command=claude2")
        saved = self.saved("claude")
        self.assertEqual(saved["aliases"], ["cc", "claude-code"])
        self.assertEqual(saved["command"], "claude2")
        self.assertEqual(saved["state"], "starred")

    def test_set_form_works(self):
        data = decode(self.edit("claude", "--set=tags=x,y,notes=hi"))["data"]
        self.assertEqual(data["changes"], ["tags", "notes"])
        self.assertEqual(data["harness"]["tags"], ["x", "y"])
        self.assertEqual(data["harness"]["notes"], "hi")

    def test_json_flag_may_come_first(self):
        result = run_swe(self.home, "edit", "--json", "claude",
                         "--description=Early")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decode(result)["data"]["harness"]["description"],
                         "Early")

    def test_empty_version_clears_to_null(self):
        data = decode(self.edit("claude", "--version="))["data"]
        self.assertEqual(data["changes"], ["version"])
        self.assertIsNone(data["harness"]["version"])
        self.assertNotIn("version", self.saved("claude"))

    def test_empty_notes_clears_to_null(self):
        data = decode(self.edit("claude", "--notes="))["data"]
        self.assertEqual(data["changes"], ["notes"])
        self.assertIsNone(data["harness"]["notes"])
        self.assertIsNone(self.listed("claude")["notes"])

    def test_display_name_is_set_and_reported(self):
        result = self.edit("claude", "--display-name=Claude Code")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = decode(result)["data"]
        self.assertEqual(data["changes"], ["display_name"])
        self.assertEqual(data["harness"]["display_name"], "Claude Code")
        self.assertEqual(self.saved("claude")["display_name"], "Claude Code")
        self.assertEqual(self.listed("claude")["display_name"], "Claude Code")

    def test_display_name_is_null_in_the_list_until_it_is_set(self):
        self.assertIsNone(self.listed("claude")["display_name"])
        self.assertNotIn("display_name", self.saved("claude"))

    def test_display_name_edit_reply_row_is_exactly_the_list_row(self):
        data = decode(self.edit("claude", "--display-name=Claude Code"))["data"]
        self.assertEqual(data["harness"], self.listed("claude"))

    def test_empty_display_name_clears_it(self):
        self.edit("claude", "--display-name=Claude Code")
        data = decode(self.edit("claude", "--display-name="))["data"]
        self.assertEqual(data["changes"], ["display_name"])
        self.assertIsNone(data["harness"]["display_name"])
        self.assertNotIn("display_name", self.saved("claude"))
        self.assertIsNone(self.listed("claude")["display_name"])

    def test_clearing_an_unset_display_name_is_a_no_op(self):
        data = decode(self.edit("claude", "--display-name="))["data"]
        self.assertEqual(data["changes"], [])

    def test_display_name_is_trimmed(self):
        data = decode(self.edit("claude", "--display-name=  Qwen Code  "))["data"]
        self.assertEqual(data["harness"]["display_name"], "Qwen Code")
        self.assertEqual(self.saved("claude")["display_name"], "Qwen Code")

    def test_a_blank_display_name_clears_it(self):
        self.edit("claude", "--display-name=Claude Code")
        data = decode(self.edit("claude", "--display-name=   "))["data"]
        self.assertEqual(data["changes"], ["display_name"])
        self.assertNotIn("display_name", self.saved("claude"))

    def test_display_name_edit_by_alias_returns_the_canonical_name(self):
        data = decode(self.edit("cc", "--display-name=Claude Code"))["data"]
        self.assertEqual(data["name"], "claude")
        self.assertEqual(data["harness"]["display_name"], "Claude Code")
        self.assertEqual(self.saved("claude")["display_name"], "Claude Code")
        self.assertNotIn("display_name", self.saved("codex"))

    def test_underscore_spelling_works_too(self):
        data = decode(self.edit("claude", "--display_name=Claude Code"))["data"]
        self.assertEqual(data["changes"], ["display_name"])
        self.assertEqual(data["harness"]["display_name"], "Claude Code")

    def test_set_form_works_with_either_spelling(self):
        data = decode(self.edit("claude", "--set=display_name=Qwen Code,notes=hi")
                      )["data"]
        self.assertEqual(data["changes"], ["display_name", "notes"])
        self.assertEqual(data["harness"]["display_name"], "Qwen Code")
        self.assertEqual(data["harness"]["notes"], "hi")
        data = decode(self.edit("claude", "--set=display-name=Cursor"))["data"]
        self.assertEqual(data["harness"]["display_name"], "Cursor")

    def test_display_name_keeps_commas_and_unicode(self):
        name = "Claude, the Code \u00e9\u4ee3\u7801 \U0001f916"
        data = decode(self.edit("claude", f"--display-name={name}"))["data"]
        self.assertEqual(data["harness"]["display_name"], name)
        # Saved as given too, not just echoed back.
        self.assertEqual(self.saved("claude")["display_name"], name)

    def test_a_valueless_display_name_flag_is_refused(self):
        self.assertRefused(self.edit("claude", "--display-name"), "--display-name")

    def test_display_name_edit_leaves_other_fields_alone(self):
        before = self.saved("claude")
        self.edit("claude", "--display-name=Claude Code")
        after = self.saved("claude")
        after.pop("display_name")
        self.assertEqual(after, before)

    def test_empty_description_clears_to_empty_string(self):
        data = decode(self.edit("claude", "--description="))["data"]
        self.assertEqual(data["changes"], ["description"])
        self.assertEqual(data["harness"]["description"], "")

    def test_empty_aliases_and_tags_clear_to_empty_lists(self):
        data = decode(self.edit("claude", "--aliases=", "--tags="))["data"]
        self.assertEqual(data["changes"], ["aliases", "tags"])
        self.assertEqual(data["harness"]["aliases"], [])
        self.assertEqual(data["harness"]["tags"], [])
        self.assertEqual(self.listed("claude")["aliases"], [])

    def test_editing_by_alias_returns_the_canonical_name(self):
        data = decode(self.edit("cc", "--description=Via alias"))["data"]
        self.assertEqual(data["name"], "claude")
        self.assertEqual(data["harness"]["name"], "claude")
        self.assertEqual(self.saved("claude")["description"], "Via alias")

    def test_a_no_op_edit_succeeds_with_no_changes(self):
        data = decode(self.edit("claude", "--description=Anthropic CLI",
                                "--tags=vendor,coding"))["data"]
        self.assertEqual(data["changes"], [])
        self.assertEqual(data["harness"]["name"], "claude")

    def test_unknown_harness_is_refused(self):
        result = self.edit("nope", "--description=x")
        self.assertRefused(result, "nope")

    def test_alias_collision_is_refused_and_nothing_is_written(self):
        before = self.saved("claude")
        result = self.edit("claude", "--aliases=cx", "--description=changed")
        self.assertRefused(result, "cx")
        self.assertEqual(self.saved("claude"), before)

    def test_empty_command_is_refused(self):
        result = self.edit("claude", "--command=")
        self.assertRefused(result, "command")
        self.assertEqual(self.saved("claude")["command"], "claude")

    def test_unknown_flag_is_refused(self):
        self.assertRefused(self.edit("claude", "--bogus=1"), "--bogus")

    def test_a_valueless_field_flag_is_refused(self):
        self.assertRefused(self.edit("claude", "--description"), "--description")

    def test_no_field_flags_is_refused_and_never_prompts(self):
        before = self.saved("claude")
        result = self.edit("claude")
        self.assertRefused(result, "--json")
        self.assertEqual(self.saved("claude"), before)

    def test_no_arguments_is_refused(self):
        self.assertRefused(run_swe(self.home, "edit", "--json"))

    def test_extra_positional_is_refused(self):
        self.assertRefused(self.edit("claude", "codex", "--notes=x"))

    def test_an_unreadable_registry_is_refused_on_stderr(self):
        path = self.home / ".swe" / "config" / "harness.json"
        path.write_text("{not json")
        self.assertRefused(self.edit("claude", "--notes=x"))
        self.assertEqual(path.read_text(), "{not json")

    def test_without_json_the_output_is_unchanged(self):
        result = run_swe(self.home, "edit", "claude", "--description=Plain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated 'claude'", result.stdout)
        self.assertNotIn('"schema"', result.stdout)


def write_rate_cache(home: Path, readings: dict, taken_at: float) -> None:
    """A fresh rate-limit cache, so `--usage` never reaches the network."""
    cache = home / ".swe" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "rate_limits_cache.json").write_text(json.dumps({
        "cached_at": taken_at,
        "limits": readings,
        "updated_at": {name: taken_at for name in readings},
        "no_data": {},
    }))


def reading(**overrides) -> dict:
    base = {
        "tool_name": "claude", "used_percent": 28, "limit_reached": False,
        "reset_at": 1_900_000_000.0, "plan_type": "max",
        "window_seconds": 18000, "window": "5h",
        "remaining_units": None, "total_units": None, "fetched_at": 0.0,
    }
    base.update(overrides)
    return base


class ListUsageJsonTest(unittest.TestCase):
    def setUp(self):
        import time

        self.home = temp_dir(self)
        write_registry(self.home, REGISTRY)
        self.taken_at = time.time() - 60

    def usage_rows(self, readings: dict) -> dict:
        write_rate_cache(self.home, readings, self.taken_at)
        result = run_swe(self.home, "list", "--usage", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return {row["name"]: row for row in decode(result)["data"]["harnesses"]}

    def test_starred_harness_carries_its_reading(self):
        rows = self.usage_rows({"claude": reading()})
        rate = rows["claude"]["rate_limit"]
        self.assertEqual(rate["status"], "ok")
        self.assertEqual(rate["used_percent"], 28)
        self.assertEqual(rate["remaining_percent"], 72)
        self.assertEqual(rate["window"], "5h")
        self.assertEqual(rate["plan_type"], "max")
        self.assertEqual(rate["reset_at"], 1_900_000_000.0)

    def test_cached_reading_keeps_the_time_it_was_taken(self):
        # A reused reading must not look fresh: the app shows its age.
        rows = self.usage_rows({"claude": reading()})
        self.assertAlmostEqual(
            rows["claude"]["rate_limit"]["fetched_at"], self.taken_at, delta=1)

    def test_unstarred_harness_is_not_polled(self):
        rows = self.usage_rows({"claude": reading()})
        self.assertIsNone(rows["codex"]["rate_limit"])

    def test_a_placeholder_plan_is_no_plan(self):
        rows = self.usage_rows({"claude": reading(plan_type="\u2014")})
        self.assertEqual(rows["claude"]["rate_limit"]["status"], "ok")
        self.assertIsNone(rows["claude"]["rate_limit"]["plan_type"])

    def test_excluded_harnesses_are_listed_but_not_polled(self):
        write_rate_cache(self.home, {"claude": reading()}, self.taken_at)
        result = run_swe(self.home, "list", "--usage", "--json", "--exclude=claude,codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {row["name"]: row for row in decode(result)["data"]["harnesses"]}
        self.assertEqual(set(rows), {"claude", "codex"})
        self.assertIsNone(rows["claude"]["rate_limit"])

    def test_sentinel_plan_types_become_a_status(self):
        rows = self.usage_rows(
            {"claude": reading(plan_type="no-sub", used_percent=0)})
        self.assertEqual(rows["claude"]["rate_limit"]["status"], "no-sub")
        self.assertIsNone(rows["claude"]["rate_limit"]["plan_type"])

    def test_an_unavailable_window_keeps_its_flag_and_age(self):
        # The app reads `unavailable` and `as_of` (snake_case, decoded as
        # `asOf` like reset_at/window_seconds): drop them and the row
        # silently looks live again.
        rows = self.usage_rows({"claude": reading(windows=[
            {"window": "5h", "used_percent": 28, "limit_reached": False,
             "reset_at": 1_900_000_000.0, "window_seconds": 18000},
            {"window": "grok_bot", "used_percent": 42, "limit_reached": False,
             "reset_at": 900, "window_seconds": 604800,
             "unavailable": True, "as_of": 1700.0},
        ])})
        windows = rows["claude"]["rate_limit"]["windows"]
        self.assertNotIn("unavailable", windows[0])
        self.assertNotIn("as_of", windows[0])
        self.assertEqual(windows[1]["unavailable"], True)
        self.assertEqual(windows[1]["as_of"], 1700.0)
        self.assertEqual(windows[1]["used_percent"], 42)



class ExcludeSkipsTheFetcherTest(unittest.TestCase):
    """--exclude must keep a provider from being asked at all, refresh or not.

    In-process, with the network boundary (`get_all_rate_limits`) recording
    which providers it was asked for. Both the table and the JSON path build
    that set, so both are checked.
    """

    def run_list(self, *args):
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        from swe.harness import commands

        asked = []
        self.deadlines = []

        def fake_get_all(use_cache=True, tool_names=None, deadline=None,
                         refresh_failed=False, cache_ttl=None, no_reading=None,
                         issues=None):
            asked.append(set(tool_names))
            self.deadlines.append(deadline)
            return {}

        registry = {name: {"command": name, "tags": []} for name in ("claude", "codex", "devin")}
        patches = [
            mock.patch.object(commands, "load_registry", return_value=registry),
            mock.patch.object(commands, "load_stars", return_value=["claude", "codex", "devin"]),
            mock.patch.object(commands, "_session_counts", return_value={}),
            mock.patch.object(commands, "_broken_tools", return_value=set()),
            mock.patch.object(commands, "is_installed", return_value=False),
            mock.patch("swe.harness.archive.load_archive", dict),
            mock.patch("swe.harness.rate_limits.get_all_rate_limits", side_effect=fake_get_all),
            # -r would otherwise clear the developer's real session caches.
            mock.patch("swe.sessions.aggregator.invalidate_cache"),
            mock.patch("swe.sessions.usage.invalidate_counts_cache"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with redirect_stdout(io.StringIO()):
            # The table path ends without an explicit status; JSON returns 0.
            self.assertIn(commands.cmd_list(list(args)), (0, None))
        return asked

    def test_json_usage_waits_out_a_slow_first_fetch(self):
        # The app reads limits through --json from a fresh process each time,
        # where a provider's first fetch can take over the 2s piped cap; under
        # that cap it would always fall back to an old cached reading.
        from swe.harness.rate_limits import _RATE_LIMIT_FETCH_DEADLINE_TTY
        self.run_list("--usage", "--json")
        self.assertEqual(self.deadlines, [_RATE_LIMIT_FETCH_DEADLINE_TTY])

    def test_piped_table_keeps_the_short_cap(self):
        self.run_list("--usage")
        self.assertEqual(self.deadlines, [None])

    def test_json_refresh_never_asks_an_excluded_provider(self):
        asked = self.run_list("--usage", "--json", "-r", "--exclude=claude,codex")
        self.assertEqual(asked, [{"devin"}])

    def test_table_refresh_never_asks_an_excluded_provider(self):
        asked = self.run_list("--usage", "-r", "--exclude=claude")
        self.assertEqual(asked, [{"codex", "devin"}])


if __name__ == "__main__":
    unittest.main()
