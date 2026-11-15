"""Tests for swe shell completion engine and script generation."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CompletionEngineTest(unittest.TestCase):
    def test_top_level_completions(self):
        from swe.completion import get_completions

        comps = get_completions([])
        names = [c for c, _ in comps]
        self.assertIn("list", names)
        self.assertIn("use", names)
        self.assertIn("session", names)
        self.assertIn("report", names)
        self.assertIn("config", names)
        self.assertIn("autocomplete", names)
        # __complete should NOT appear
        self.assertNotIn("__complete", names)

    def test_partial_subcommand_filter(self):
        from swe.completion import get_completions

        comps = get_completions(["s"])
        names = [c for c, _ in comps]
        self.assertIn("session", names)
        self.assertIn("skills", names)
        self.assertNotIn("list", names)
        # star moved under `swe harness` / `swe hs`.
        self.assertNotIn("star", names)

    def test_instructions_completions(self):
        from swe.completion import get_completions
        from swe.instructions.commands import SUBCOMMANDS

        self.assertIn("instructions", [name for name, _ in get_completions([])])
        self.assertEqual(
            {name for name, _ in get_completions(["instructions", ""])},
            set(SUBCOMMANDS),
        )
        self.assertEqual(
            [name for name, _ in get_completions(["instructions", "ch"])],
            ["check"],
        )

    def test_use_returns_tool_completions(self):
        fake_registry = {
            "claude": {"description": "Claude Code", "aliases": ["cc"]},
            "codex": {"description": "Codex CLI", "aliases": ["cx"]},
        }
        with patch("swe.completion.load_registry", return_value=fake_registry):
            from swe.completion import get_completions

            comps = get_completions(["use", ""])
        names = [c for c, _ in comps]
        self.assertIn("claude", names)
        self.assertIn("codex", names)
        self.assertIn("cc", names)
        self.assertIn("cx", names)

    def test_hs_star_returns_tool_completions(self):
        fake_registry = {
            "claude": {"description": "Claude Code", "aliases": ["cc"]},
        }
        with patch("swe.completion.load_registry", return_value=fake_registry):
            from swe.completion import get_completions

            comps = get_completions(["hs", "star", ""])
        names = [c for c, _ in comps]
        self.assertIn("claude", names)
        self.assertIn("cc", names)

    def test_partial_tool_filter(self):
        fake_registry = {
            "claude": {"description": "Claude Code", "aliases": ["cc"]},
            "cline": {"description": "Cline", "aliases": ["cl"]},
            "codex": {"description": "Codex CLI", "aliases": ["cx"]},
        }
        with patch("swe.completion.load_registry", return_value=fake_registry):
            from swe.completion import get_completions

            comps = get_completions(["use", "cl"])
        names = [c for c, _ in comps]
        self.assertIn("claude", names)
        self.assertIn("cline", names)
        self.assertIn("cl", names)
        self.assertNotIn("codex", names)

    def test_list_flag_completions(self):
        from swe.completion import get_completions

        comps = get_completions(["list", "--"])
        names = [c for c, _ in comps]
        self.assertIn("--refresh", names)

        short_comps = get_completions(["list", "-"])
        short_names = [c for c, _ in short_comps]
        self.assertIn("-n", short_names)

    def test_list_tag_completions(self):
        fake_registry = {
            "claude": {"description": "Claude", "aliases": [], "tags": ["coding", "byok"]},
            "codex": {"description": "Codex", "aliases": [], "tags": ["coding"]},
        }
        with patch("swe.completion.load_registry", return_value=fake_registry):
            from swe.completion import get_completions

            comps = get_completions(["list", ""])
        names = [c for c, _ in comps]
        self.assertIn("coding", names)
        self.assertIn("byok", names)

    def test_session_flag_completions(self):
        from swe.completion import get_completions

        comps = get_completions(["session", "--"])
        names = [c for c, _ in comps]
        self.assertIn("--search=", names)
        self.assertIn("--days=", names)
        self.assertIn("--start=", names)

    def test_report_and_config_subcommands(self):
        from swe.completion import get_completions

        report = [name for name, _ in get_completions(["report", ""])]
        config = [name for name, _ in get_completions(["config", ""])]
        self.assertIn("daily", report)
        self.assertIn("followups", report)
        self.assertIn("set", config)
        self.assertIn("check", config)

    def test_setup_sections_and_flags(self):
        from swe.completion import get_completions

        sections = [name for name, _ in get_completions(["setup", ""])]
        flags = [name for name, _ in get_completions(["setup", "--"])]
        self.assertEqual(
            sections,
            ["harnesses", "providers", "mcp", "skills", "report", "check"],
        )
        self.assertIn("--quick", flags)
        self.assertIn("--non-interactive", flags)

    def test_no_completions_for_unknown_context(self):
        from swe.completion import get_completions

        comps = get_completions(["unknown_cmd", "arg1", "arg2"])
        self.assertEqual(comps, [])

    def test_use_with_extra_args_returns_nothing(self):
        from swe.completion import get_completions

        comps = get_completions(["use", "claude", "extra"])
        self.assertEqual(comps, [])

    def test_primary_commands_cover_dispatch(self):
        # `swe <TAB>` must offer every non-alias command — find and
        # discover were missing while `harness` was listed twice.
        from swe.cli import COMMANDS
        from swe.completion import _PRIMARY_COMMANDS
        from swe.harness.drift import NO_TOPIC_WHITELIST

        names = [name for name, _ in _PRIMARY_COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertIn(name, COMMANDS, msg=name)
        for cmd in COMMANDS:
            if cmd in NO_TOPIC_WHITELIST:
                self.assertNotIn(cmd, names, msg=f"alias {cmd} in primary list")
            else:
                self.assertIn(cmd, names, msg=f"{cmd} not completable")

    def test_mcp_subcommands_and_sync_flags(self):
        from swe.completion import get_completions

        subs = [c for c, _ in get_completions(["mcp", ""])]
        for sub in ("discover", "list", "status", "sync", "diff", "edit",
                    "validate", "doctor", "help"):
            self.assertIn(sub, subs)
        flags = [c for c, _ in get_completions(["mcp", "sync", "--"])]
        for flag in ("--all", "--only=", "--except=", "--prune", "--dry-run"):
            self.assertIn(flag, flags)

    def test_edit_offers_json_beside_the_field_flags(self):
        from swe.completion import get_completions

        flags = [c for c, _ in get_completions(["edit", "claude", "--"])]
        for flag in ("--description=", "--notes=", "--display-name=", "--json"):
            self.assertIn(flag, flags)

    def test_find_and_skills_subcommands(self):
        from swe.completion import get_completions

        topics = [c for c, _ in get_completions(["find", ""])]
        self.assertIn("skills", topics)
        self.assertIn("mcp", topics)
        skills = [c for c, _ in get_completions(["skills", ""])]
        for sub in ("tree", "scope", "link", "unlink", "move", "catalog"):
            self.assertIn(sub, skills)

    def test_nested_flag_tables(self):
        from swe.completion import get_completions

        # `swe report followups --<TAB>` offers --status, not report's range flags
        flags = [c for c, _ in get_completions(["report", "followups", "--"])]
        self.assertIn("--status=open", flags)
        self.assertNotIn("--days=", flags)
        # `swe list edit --<TAB>` offers --reset, not --scope
        flags = [c for c, _ in get_completions(["list", "edit", "--"])]
        self.assertEqual(flags, ["--reset"])
        # `swe find mcp --<TAB>` drops -i (not supported for mcps)
        flags = [c for c, _ in get_completions(["find", "mcp", "--"])]
        self.assertNotIn("-i", flags)
        self.assertNotIn("--interactive", flags)
        self.assertIn("--scope=global", flags)
        # `find --root` is a switch, not --root=<dir>
        flags = [c for c, _ in get_completions(["find", "--"])]
        self.assertIn("--root", flags)
        self.assertNotIn("--root=", flags)

    def test_followup_work_flags_need_the_id_first(self):
        from swe.completion import get_completions

        # Before the ID: no work flags, and no report-generation flags.
        self.assertEqual(
            get_completions(["report", "followup", "work", "--"]), []
        )
        # After the ID: the action's own flags.
        flags = [
            c for c, _ in get_completions(["report", "followup", "work", "id1", "--"])
        ]
        self.assertIn("--resume", flags)
        self.assertIn("--new", flags)
        self.assertIn("--harness=", flags)
        self.assertNotIn("--days=", flags)

    def test_nested_subcommands(self):
        from swe.completion import get_completions

        self.assertEqual(
            [c for c, _ in get_completions(["plugins", "marketplace", ""])], ["add"])
        actions = [c for c, _ in get_completions(["report", "followup", ""])]
        self.assertIn("work", actions)
        self.assertIn("done", actions)
        catalog = [c for c, _ in get_completions(["skills", "catalog", ""])]
        self.assertIn("add", catalog)

    def test_mcp_sync_multi_tool_completion(self):
        fake_registry = {
            "claude": {"description": "Claude Code", "aliases": ["cc"]},
            "codex": {"description": "Codex CLI", "aliases": ["cx"]},
            "cursor": {"description": "Cursor", "aliases": []},
        }
        with patch("swe.completion.load_registry", return_value=fake_registry):
            from swe.completion import get_completions

            names = [c for c, _ in get_completions(["mcp", "sync", "claude", ""])]
        self.assertIn("codex", names)
        self.assertIn("cursor", names)
        # Already-typed sources are not re-offered.
        self.assertNotIn("claude", names)
        self.assertNotIn("cc", names)

    def test_mcp_list_tool_completion(self):
        fake_registry = {
            "claude": {"description": "Claude Code", "aliases": ["cc"]},
        }
        with patch("swe.completion.load_registry", return_value=fake_registry):
            from swe.completion import get_completions

            names = [c for c, _ in get_completions(["mcp", "list", ""])]
        self.assertIn("claude", names)

    def test_mcp_sync_resolves_alias_when_deduping(self):
        # Typing the alias `cc` must also remove `claude` itself.
        fake_registry = {
            "claude": {"description": "Claude Code", "aliases": ["cc"]},
            "codex": {"description": "Codex CLI", "aliases": ["cx"]},
        }
        with patch("swe.completion.load_registry", return_value=fake_registry):
            from swe.completion import get_completions

            names = [c for c, _ in get_completions(["mcp", "sync", "cc", ""])]
        self.assertIn("codex", names)
        self.assertNotIn("claude", names)
        self.assertNotIn("cc", names)

    def test_provider_name_completion(self):
        fake = {"anthropic": {"name": "Anthropic"}, "openai": {"name": "OpenAI"}}
        with patch(
            "swe.providers.registry.load_registry", return_value=fake
        ):
            from swe.completion import get_completions

            names = [c for c, _ in get_completions(["providers", "info", ""])]
            rm_names = [c for c, _ in get_completions(["pv", "rm", "o"])]
        self.assertEqual(names, ["anthropic", "openai"])
        self.assertEqual(rm_names, ["openai"])

    def test_mcp_edit_server_completion(self):
        with patch("swe.mcp.cli.load_registry", return_value={}), \
             patch("swe.mcp.cli.resolve_tool_arg", return_value="codex"), \
             patch("swe.mcp.cli.get_tool_config",
                   return_value={"path": Path("/x/mcp.json")}), \
             patch("swe.mcp.cli.get_tool_loader",
                   return_value=lambda p: {"srv": {}, "db": {}}):
            from swe.completion import get_completions

            names = [c for c, _ in get_completions(["mcp", "edit", "codex", ""])]
        self.assertEqual(names, ["db", "srv"])

    def test_mcp_edit_server_completion_unknown_tool(self):
        with patch("swe.mcp.cli.load_registry", return_value={}), \
             patch("swe.mcp.cli.resolve_tool_arg", return_value=None):
            from swe.completion import get_completions

            self.assertEqual(get_completions(["mcp", "edit", "ghost", ""]), [])

    def test_mcp_edit_server_completion_no_backend(self):
        with patch("swe.mcp.cli.load_registry", return_value={}), \
             patch("swe.mcp.cli.resolve_tool_arg", return_value="codex"), \
             patch("swe.mcp.cli.get_tool_config", return_value=None), \
             patch("swe.mcp.cli.get_tool_loader", return_value=None):
            from swe.completion import get_completions

            self.assertEqual(get_completions(["mcp", "edit", "codex", ""]), [])

    def test_provider_alias_completion(self):
        fake = {
            "anthropic": {"name": "Anthropic", "aliases": ["claude-api"]},
        }
        with patch(
            "swe.providers.registry.load_registry", return_value=fake
        ):
            from swe.completion import get_completions

            names = [c for c, _ in get_completions(["providers", "info", ""])]
        self.assertIn("anthropic", names)
        self.assertIn("claude-api", names)

    def test_provider_alias_matching_name_not_duplicated(self):
        fake = {"openai": {"name": "OpenAI", "aliases": ["openai"]}}
        with patch(
            "swe.providers.registry.load_registry", return_value=fake
        ):
            from swe.completion import get_completions

            names = [c for c, _ in get_completions(["providers", "info", ""])]
        self.assertEqual(names, ["openai"])

    def test_install_completes_catalog_names(self):
        fake_registry = {
            "claude": {"description": "Claude Code", "aliases": ["cc"]},
        }
        catalog = {
            "claude": {"description": "already registered"},
            "jules": {"description": "Jules agent"},
        }
        with patch("swe.completion.load_registry", return_value=fake_registry), \
             patch.dict("swe.harness.catalog.HARNESS_CATALOG", catalog, clear=True):
            from swe.completion import get_completions

            names = [c for c, _ in get_completions(["install", ""])]
        self.assertIn("claude", names)
        self.assertIn("jules", names)
        self.assertEqual(names.count("claude"), 1)

    def test_provider_completion_survives_broken_registry(self):
        with patch(
            "swe.providers.registry.load_registry",
            side_effect=OSError("corrupt"),
        ):
            from swe.completion import get_completions

            self.assertEqual(get_completions(["providers", "info", ""]), [])

    def test_completion_survives_broken_registry(self):
        # A corrupt registry must yield no candidates, not a crash on TAB.
        with patch(
            "swe.completion.load_registry", side_effect=OSError("corrupt")
        ):
            from swe.completion import get_completions

            self.assertEqual(get_completions(["use", ""]), [])
            self.assertEqual(get_completions(["mcp", "sync", "x", ""]), [])
            self.assertEqual(get_completions(["list", ""]), [])

    def test_first_word_flag_returns_nothing(self):
        from swe.completion import get_completions

        self.assertEqual(get_completions(["-"]), [])


class CompleteCommandTest(unittest.TestCase):
    """Test the hidden __complete command output format."""

    def _capture_complete(self, args):
        import io
        from swe.cli import cmd_complete

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            cmd_complete(args)
        return mock_stdout.getvalue()

    def test_directive_line(self):
        output = self._capture_complete([])
        lines = output.strip().split("\n")
        self.assertEqual(lines[-1], ":4")

    def test_tab_separated_format(self):
        output = self._capture_complete([])
        lines = output.strip().split("\n")
        # At least one line should have a tab separator (candidate\tdescription)
        has_tab = any("\t" in line for line in lines[:-1])  # exclude :4
        self.assertTrue(has_tab)

    def test_use_completions_output(self):
        fake_registry = {
            "claude": {"description": "Claude Code", "aliases": ["cc"]},
        }
        with patch("swe.completion.load_registry", return_value=fake_registry):
            output = self._capture_complete(["use", ""])
        lines = output.strip().split("\n")
        # Should have claude, cc, then :4
        candidates = [l.split("\t")[0] for l in lines[:-1]]
        self.assertIn("claude", candidates)
        self.assertIn("cc", candidates)


class AutocompleteCommandTest(unittest.TestCase):
    """Test the autocomplete command script generation and injection."""

    def test_zsh_generates_script(self):
        from swe.cli import cmd_autocomplete

        with tempfile.TemporaryDirectory() as tmp:
            completion_dir = Path(tmp) / "completions"
            zshrc = Path(tmp) / ".zshrc"

            fake_configs = {
                "zsh": {
                    "script": "# test zsh script",
                    "filename": "swe.zsh",
                    "profile": str(zshrc),
                    "profile_instructions": f"source {zshrc}",
                },
            }

            with (
                patch("swe.paths.COMPLETION_DIR", completion_dir),
                patch("swe.completion_scripts.SHELL_CONFIGS", fake_configs),
            ):
                result = cmd_autocomplete(["zsh"])

            self.assertEqual(result, 0)
            script_file = completion_dir / "swe.zsh"
            self.assertTrue(script_file.exists())
            self.assertIn("# test zsh script", script_file.read_text())

    def test_injection_idempotent(self):
        from swe.cli import cmd_autocomplete

        with tempfile.TemporaryDirectory() as tmp:
            completion_dir = Path(tmp) / "completions"
            zshrc = Path(tmp) / ".zshrc"
            zshrc.write_text("# existing config\n")

            fake_configs = {
                "zsh": {
                    "script": "# test zsh script",
                    "filename": "swe.zsh",
                    "profile": str(zshrc),
                    "profile_instructions": f"source {zshrc}",
                },
            }

            with (
                patch("swe.paths.COMPLETION_DIR", completion_dir),
                patch("swe.completion_scripts.SHELL_CONFIGS", fake_configs),
            ):
                cmd_autocomplete(["zsh"])
                content_after_first = zshrc.read_text()
                cmd_autocomplete(["zsh"])
                content_after_second = zshrc.read_text()

            expected = f'source "{completion_dir / "swe.zsh"}"  # swe autocomplete (zsh)'
            self.assertEqual(content_after_first.count(expected), 1)
            self.assertEqual(content_after_first, content_after_second)

    def test_unsupported_shell_returns_error(self):
        import io
        from swe.cli import cmd_autocomplete

        with patch("sys.stdout", new_callable=io.StringIO):
            result = cmd_autocomplete(["tcsh"])
        self.assertEqual(result, 1)

    def test_bash_script_uses_bash_syntax(self):
        from swe.completion_scripts import BASH_SCRIPT

        self.assertIn("while IFS= read -r line", BASH_SCRIPT)
        self.assertNotIn("${(f)out}", BASH_SCRIPT)
        self.assertNotIn("lines[-1]", BASH_SCRIPT)

    def test_no_args_returns_usage(self):
        import io
        from swe.cli import cmd_autocomplete

        with patch("sys.stdout", new_callable=io.StringIO):
            result = cmd_autocomplete([])
        self.assertEqual(result, 1)


class AutocompletePrintTest(unittest.TestCase):
    """`swe autocomplete <shell> --print` and the install confirmation."""

    def _run(self, args):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from swe.cli import cmd_autocomplete

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cmd_autocomplete(args)
        return code, out.getvalue(), err.getvalue()

    def _home(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {"HOME": str(home)}))
        self.enterContext(patch("swe.paths.COMPLETION_DIR", home / ".swe" / "completions"))
        return home

    def test_print_emits_install_script_and_touches_nothing(self):
        from swe.completion_scripts import SHELL_CONFIGS

        for shell, config in SHELL_CONFIGS.items():
            with self.subTest(shell=shell):
                home = self._home()
                code, out, _ = self._run([shell, "--print"])
                self.assertEqual(code, 0)
                self.assertEqual(out, config["script"])
                self.assertEqual(list(home.rglob("*")), [])

    def test_print_flag_before_shell(self):
        home = self._home()
        code, out, _ = self._run(["--print", "zsh"])
        self.assertEqual(code, 0)
        self.assertIn("#compdef swe", out)
        self.assertEqual(list(home.rglob("*")), [])

    def test_print_leaves_existing_rc_unchanged(self):
        home = self._home()
        rc = home / ".zshrc"
        rc.write_text("# mine\n")
        self._run(["zsh", "--print"])
        self.assertEqual(rc.read_text(), "# mine\n")
        self.assertFalse((home / ".swe").exists())

    def test_print_matches_written_file(self):
        home = self._home()
        _, printed, _ = self._run(["zsh", "--print"])
        self._run(["zsh"])
        self.assertEqual((home / ".swe" / "completions" / "swe.zsh").read_text(), printed)

    def test_install_writes_file_and_rc_line_once_and_confirms(self):
        home = self._home()
        code, out, _ = self._run(["zsh"])
        script = home / ".swe" / "completions" / "swe.zsh"
        rc = home / ".zshrc"
        self.assertEqual(code, 0)
        self.assertTrue(script.exists())
        self.assertIn(f"Wrote {script}", out)
        self.assertIn(f"Added a source line to {rc}", out)
        code2, out2, _ = self._run(["zsh"])
        self.assertEqual(code2, 0)
        self.assertEqual(rc.read_text().count("# swe autocomplete (zsh)"), 1)
        self.assertIn(f"Wrote {script}", out2)
        self.assertIn(f"{rc} already has the source line", out2)
        self.assertNotIn("Added a source line", out2)

    def test_install_bash_and_fish_name_their_files(self):
        home = self._home()
        _, out, _ = self._run(["bash"])
        self.assertIn(str(home / ".swe" / "completions" / "swe.bash"), out)
        self.assertIn(str(home / ".bashrc"), out)
        _, out, _ = self._run(["fish"])
        self.assertIn(str(home / ".config" / "fish" / "config.fish"), out)

    def test_print_leaves_legacy_quiver_home_untouched(self):
        # Importing swe.paths migrates ~/.quiver to ~/.swe, so --print must
        # run in a fresh interpreter that never reaches that import.
        import subprocess
        import sys

        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (home / ".quiver" / "data").mkdir(parents=True)
        (home / ".quiver" / "data" / "f.json").write_text('{"a": 1}')
        (home / ".quiver" / "harness.json").write_text("{}")

        def snapshot():
            return sorted(
                (str(p.relative_to(home)), p.read_bytes() if p.is_file() else None)
                for p in home.rglob("*")
            )

        before = snapshot()
        env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        proc = subprocess.run(
            [sys.executable, "-m", "swe.cli", "autocomplete", "zsh", "--print"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("#compdef swe", proc.stdout)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(snapshot(), before)
        self.assertFalse((home / ".swe").exists())
        self.assertFalse((home / ".quiver").is_symlink())

    def test_unknown_shell_still_errors_with_print(self):
        home = self._home()
        code, out, _ = self._run(["tcsh", "--print"])
        self.assertEqual(code, 1)
        self.assertIn("Unsupported shell: 'tcsh'", out)
        self.assertEqual(list(home.rglob("*")), [])

    def test_print_without_shell_errors(self):
        code, out, _ = self._run(["--print"])
        self.assertEqual(code, 1)
        self.assertIn("Usage: swe autocomplete", out)


if __name__ == "__main__":
    unittest.main()
