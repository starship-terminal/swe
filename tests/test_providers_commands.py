"""Tests for ``swe providers`` (providers/commands.py): list, info, add
and remove. The discover layer they read through has its own file,
test_providers_discover.py.

Most of the list and info tests come from the migration of ``cmd_list``
to ``swe.table.Table``.

The migration replaces hand-rolled f-string padding with a single
5-column Table build (PROVIDER | ALIASES | ENV VAR | API KEY | URL)
plus an optional description sub-line below each rendered row,
indented to align under the ALIASES column header. Column widths are
pre-measured across the current row set so the Table schema and the
per-row ``cpad`` call agree by construction — every body row arrives
at exactly the column visible width.

These tests pin the structural invariants the migration promises:

1. 5-column header order with the original PROVIDER / ALIASES / ENV
   VAR / API KEY / URL labels.
2. Header SEParator visible length equals header visible length.
3. Every body row visible length equals header visible length
   (cpad-driven parity).
4. PROVIDER cell is bold ANSI; ALIASES and ENV VAR cells are dim;
   API KEY cell is green if a masked token is present and dim if
   the row shows the dash placeholder; URL cell is cyan.
5. URL column drops ``https://`` / ``http://`` / ``www.`` prefixes
   before rendering.
6. Description sub-line only emits when ``--desc`` is passed AND
   the row's ``info["description"]`` is truthy; otherwise the row
   is single-line.
7. Description indent is ``outer_pad + provider_w + 2`` (= HEAD/outer
   pad + PROVIDER column width + the gap between PROVIDER and
   ALIASES). This anchors the description under the ALIASES header,
   which is the column the description logically extends.
8. PROVIDER width is capped at 24 — long provider names truncate
   cleanly without breaking the otherwise-fixed left columns.
9. Empty aliases render as ``—`` (em-dash).
10. Env var column shows ``(+N)`` suffix when more than one
    ``env_vars`` is declared but not matched against ``matched_env``.
"""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from swe.console import c, strip_ansi, visible_len
from swe.providers.commands import cmd_info, cmd_list
from swe.providers.defaults import (
    DEFAULT_ALL,
    DEFAULT_INFRA,
    DEFAULT_PROVIDERS,
    DEFAULT_SERVICES,
)


def _registry_patches(config_dir: Path, registry_file: Path):
    return (
        patch("swe.providers.registry.CONFIG_DIR", config_dir),
        patch("swe.providers.registry.PROVIDERS_REGISTRY_FILE", registry_file),
    )


def _setup(tmp_path: Path, *, create_keys_dir: bool = True):
    config_dir = tmp_path / ".config" / "swe"
    registry_file = config_dir / "providers.json"
    keys_dir = tmp_path / ".api_keys"
    if create_keys_dir:
        keys_dir.mkdir()
    return config_dir, registry_file, keys_dir


def _run_cmd_list(args):
    """``cmd_list``'s stdout; every list these tests run succeeds."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_list(list(args))
    if rc != 0:
        raise AssertionError(f"cmd_list({args!r}) returned {rc!r}:\n{buf.getvalue()}")
    return buf.getvalue()


# Labels printed at the outer 2-space page padding.
HEADER_LABELS = ("PROVIDER", "ALIASES", "ENV VAR", "API KEY", "URL")


# Two providers: one with a description, one long-named without.
_FAKE_PROVIDERS = {
    "alpha": {
        "name": "alpha", "aliases": ["alpha", "al"],
        "env_vars": ["ALPHA_API_KEY"], "url": "https://alpha.example.com",
        "description": "Alpha blurb", "key_filename": "alpha",
    },
    "very-long-provider-name-that-exceeds-cap": {
        "name": "very-long-provider-name-that-exceeds-cap",
        "aliases": ["vlpn"], "env_vars": ["VLP_API_KEY"],
        "url": "https://very-long-provider-name-that-exceeds-cap.example.com",
        "description": "", "key_filename": "very-long",
    },
}


def _body_block(output: str) -> list[str]:
    """The lines from below the header separator to the first blank one."""
    lines = output.split("\n")
    hdr = next(i for i, raw in enumerate(lines)
               if all(lbl in strip_ansi(raw) for lbl in HEADER_LABELS))
    body = []
    for ln in lines[hdr + 2:]:
        if not ln.strip():
            break
        body.append(ln)
    return body


def _row_cells(output: str, provider: str) -> list[str]:
    """The raw (ANSI-carrying) cells of ``provider``'s row."""
    row = next(ln for ln in _body_block(output)
               if strip_ansi(ln).split(" │ ")[0].strip() == provider)
    return row.split(" │ ")


def _info_value(output: str, label: str) -> str:
    """The plain value printed after ``label:`` in cmd_info."""
    line = next(ln for ln in strip_ansi(output).split("\n")
                if ln.strip().startswith(f"{label}:"))
    return line.split(f"{label}:", 1)[1].strip()


class CmdListMigrationTest(unittest.TestCase):
    """Structural invariants for cmd_list post-migration."""

    # Fresh default providers + a keys dir — used by most tests.
    def _patches(self, tmp_path: Path):
        config_dir, registry_file, keys_dir = _setup(tmp_path)
        return _registry_patches(config_dir, registry_file) + (
            patch(
                "swe.providers.commands.default_keys_dir",
                return_value=keys_dir,
            ),
        )

    def _hdr_idx(self, lines):
        return next(
            i for i, raw in enumerate(lines)
            if all(lbl in strip_ansi(raw) for lbl in HEADER_LABELS)
        )

    def test_header_has_all_five_labels_in_order(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            plain = strip_ansi(output)
            for label in HEADER_LABELS:
                self.assertIn(label, plain, f"header label {label!r} missing")
            # The labels appear in the rendered order on the header line.
            label_positions = [plain.find(lbl) for lbl in HEADER_LABELS]
            self.assertEqual(label_positions, sorted(label_positions))

    def test_separator_visible_length_matches_header(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            lines = output.split("\n")
            hdr_idx = self._hdr_idx(lines)
            sep_dashes = strip_ansi(lines[hdr_idx + 1]).count("\u2500")
            # Both lines share the same 2-space outer page padding,
            # so their visible widths match by construction. Stripping
            # the 2-char outer pad from the header gives the table's
            # inner width, which the dashes count exactly.
            self.assertEqual(
                sep_dashes, visible_len(lines[hdr_idx]) - 2,
                f"sep dashes ({sep_dashes}) != inner header width "
                f"({visible_len(lines[hdr_idx]) - 2})",
            )

    def test_body_rows_aligned_to_header_visible_width(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            lines = output.split("\n")
            expected_width = visible_len(lines[self._hdr_idx(lines)])
            # Without --desc every body line is a provider row, one per
            # default provider, and each one is padded to the header.
            body_rows = _body_block(output)
            self.assertEqual(len(body_rows), len(DEFAULT_PROVIDERS))
            for row in body_rows:
                self.assertEqual(
                    expected_width, visible_len(row),
                    f"body row drifted: {strip_ansi(row)!r}",
                )

    def test_provider_column_is_bold(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            # cpad("bold", name, w) emits \033[1m followed by the
            # provider name. Every default provider's name appears at
            # least once in the body.
            self.assertIn("\033[1mopenai", output)
            self.assertIn("\033[1manthropic", output)

    def test_aliases_column_is_dim(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            # cpad("dim", aliases_text, w): fireworks_ai's alias cell is
            # the dim escape, then the alias.
            aliases = _row_cells(output, "fireworks_ai")[1]
            self.assertTrue(aliases.startswith("\033[2mfireworks-ai"), repr(aliases))

    def test_api_key_dim_when_no_key_file(self):
        # No key file for any provider → every masked == "-" →
        # every API KEY cell uses dim colour.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            # The masked "-" placeholder is emitted with dim escape
            # (cpad("dim", "-", w)).
            self.assertIn("\033[2m-", output)

    def test_api_key_green_when_masked_key_present(self):
        # One key file present → one row's API KEY cell is green.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, keys_dir = _setup(tmp_path)
            (keys_dir / "openai").write_text(
                "sk-proj-MigrationTestMaskMe12345678"
            )
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=keys_dir,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            # cpad("green", "sk-proj-...***...", w) → \033[32m prefix.
            self.assertIn("\033[32m", output)
            # The cell shows the prefix, the last four and the length.
            self.assertIn("sk-proj-***5678 (35)", strip_ansi(output))

    def test_url_strips_https_www_prefix(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            # Existing defaults carry https:// URLs (e.g.
            # "https://platform.openai.com/api-keys" for openai).
            # The implementation strips the prefix before rendering,
            # so:
            #   - No raw "https://" or "http://" should appear in the
            #     cyan-coloured URL cells.
            #   - The host tail (post-strip) must appear.
            plain = strip_ansi(output)
            # Use a substring guaranteed unique to the post-strip URL.
            self.assertIn("platform.openai.com", plain)
            self.assertNotIn("https://", plain)

    def test_em_dash_for_providers_without_aliases(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            self.assertEqual(strip_ansi(_row_cells(output, "anthropic")[1]).strip(), "—")
            self.assertEqual(strip_ansi(_row_cells(output, "fireworks_ai")[1]).strip(),
                             "fireworks-ai")

    def test_env_var_column_shows_matched_env(self):
        # Shell-export file layout → matched_env is set on the row.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, _ = _setup(
                tmp_path, create_keys_dir=False
            )
            shell_keys = tmp_path / ".api_keys"
            shell_keys.write_text(
                "export OPENAI_API_KEY=sk-proj-EnvCellTest12345678\n"
                "export ANTHROPIC_API_KEY=sk-ant-EnvCellTest123456\n"
            )
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=shell_keys,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            plain = strip_ansi(output)
            self.assertIn("OPENAI_API_KEY", plain)
            self.assertIn("ANTHROPIC_API_KEY", plain)

    def test_desc_flag_emits_description_sub_line(self):
        # With --desc, descriptions render as dim-indented sub-lines
        # below each row that has a truthy description.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list(["--desc"])
            # Defaults with non-empty description include openai and
            # anthropic; their description blurbs contain a known
            # substring (sourced from DEFAULT_PROVIDERS).
            from swe.providers.defaults import DEFAULT_PROVIDERS
            for name, info in DEFAULT_PROVIDERS.items():
                desc = info.get("description") or ""
                if desc:
                    self.assertIn(
                        desc, strip_ansi(output),
                        f"description for {name!r} not rendered with --desc",
                    )

    def test_short_desc_flag_and_no_description_by_default(self):
        with TemporaryDirectory() as tmp:
            patches = self._patches(Path(tmp))
            with patches[0], patches[1], patches[2]:
                plain = strip_ansi(_run_cmd_list(["openai"]))
                short = strip_ansi(_run_cmd_list(["-d", "openai"]))
        self.assertNotIn("OpenAI API (ChatGPT", plain)
        self.assertIn("OpenAI API (ChatGPT", short)

    def test_desc_flag_skips_empty_descriptions(self):
        # Even with --desc, a row whose description is empty gets no
        # sub-line: alpha's row is followed by its blurb, the long-named
        # provider's row by nothing.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2], patch(
                "swe.providers.commands.load_registry",
                return_value=_FAKE_PROVIDERS,
            ):
                output = _run_cmd_list(["--desc"])
            body = [strip_ansi(ln).strip() for ln in _body_block(output)]
            self.assertEqual(len(body), 3, body)
            self.assertTrue(body[0].startswith("alpha"))
            self.assertEqual(body[1], "Alpha blurb")
            self.assertTrue(body[2].startswith("very-long-provider"))

    def test_description_sub_line_indent_under_aliases_column(self):
        # The description sub-line must sit under the ALIASES column
        # header. Production emits
        # ``" " * desc_indent + c("dim", truncate(desc, 90))`` so the
        # line starts with whitespace, then a dim ANSI wrap, then the
        # truncated description text. We pin the indent by walking
        # every dim-coloured line and asserting the visible offset
        # into the line equals ``idx_aliases`` from the header.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list(["--desc"])
            lines = output.split("\n")
            hdr_idx = self._hdr_idx(lines)
            header_text = strip_ansi(lines[hdr_idx])
            idx_aliases = header_text.find("ALIASES")
            self.assertGreater(idx_aliases, 0)
            desc_indent_expected = idx_aliases

            # Find description sub-lines: lines carrying dim ANSI
            # (anywhere in the line) that have at least
            # ``desc_indent_expected`` leading visible spaces.
            desc_lines = []
            for ln in lines:
                if "\033[2m" not in ln:
                    continue
                plain = strip_ansi(ln)
                # The first ``desc_indent_expected`` visible chars
                # must be all whitespace, and there must be a non-
                # whitespace char after the indent.
                leading_ws = plain[:desc_indent_expected]
                if leading_ws.strip() != "":
                    continue
                if len(plain) <= desc_indent_expected:
                    continue
                if plain[desc_indent_expected] == " ":
                    continue
                desc_lines.append(ln)
            self.assertGreater(
                len(desc_lines), 0,
                f"no description sub-line at expected indent "
                f"{desc_indent_expected}; header was: {header_text!r}",
            )

    def test_provider_width_capped_at_24(self):
        # A provider whose name is longer than 24 chars truncates to
        # exactly 24 visible chars in the PROVIDER cell, keeping the
        # header and every row aligned.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2], patch(
                "swe.providers.commands.load_registry",
                return_value=_FAKE_PROVIDERS,
            ):
                output = _run_cmd_list([])
            lines = output.split("\n")
            hdr_plain = strip_ansi(lines[self._hdr_idx(lines)])
            # Outer pad (2) + PROVIDER column (24) + gap " │ " (3).
            self.assertEqual(hdr_plain.find("ALIASES"), 2 + 24 + 3)
            cell = strip_ansi(_row_cells(output, "very-long-provider-na...")[0])
            self.assertEqual(cell, "  very-long-provider-na...")
            self.assertEqual(visible_len(cell) - 2, 24)
            for row in _body_block(output):
                self.assertEqual(visible_len(row), visible_len(lines[self._hdr_idx(lines)]))

    def test_filter_drops_non_matching_providers(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                # Filter to "openai" — only openai rows match.
                lines = _run_cmd_list(["openai"]).split("\n")
            # Read the PROVIDER cell of each table row, not the whole
            # output: the footer prints the keys dir, and a random temp
            # folder name like "tmp4xaifqgl" contains "xai".
            hdr_idx = self._hdr_idx(lines)
            providers = []
            for ln in lines[hdr_idx + 2:]:
                if not ln.strip():
                    break  # the table ends at the first blank line
                providers.append(strip_ansi(ln).split("│")[0].strip())
            self.assertEqual(providers, ["openai"])

    def test_no_keys_tip_advisory_after_table(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            # Tip must come AFTER the dashed separator.
            idx_tip = output.find("Tip:")
            idx_dashes = output.find("\u2500" * 10)
            self.assertGreater(idx_dashes, 0)
            self.assertGreater(idx_tip, idx_dashes)
            # It counts against the whole catalog and names the next step.
            self.assertIn(f"0/{len(DEFAULT_ALL)}", output)
            self.assertIn("swe providers info", output)

    def test_no_keys_dir_advisory_after_table(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, _ = _setup(
                tmp_path, create_keys_dir=False
            )
            missing_dir = tmp_path / "no_such_dir"
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=missing_dir,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            idx_notice = output.find("No key store")
            idx_dashes = output.find("\u2500" * 10)
            self.assertGreater(idx_dashes, 0)
            self.assertGreater(idx_notice, idx_dashes)

    def test_footer_math_with_keys(self):
        # When one provider has a key, ``n_with = 1`` so footer
        # reads ``1/N providers with keys``.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, keys_dir = _setup(tmp_path)
            (keys_dir / "openai").write_text(
                "sk-proj-FooterMathTest12345678"
            )
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=keys_dir,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            plain = strip_ansi(output)
            self.assertIn(
                f"1/{len(DEFAULT_ALL)} providers with keys", plain,
                f"expected '1/{len(DEFAULT_ALL)} providers with "
                f"keys' in footer: {plain!r}",
            )

    def test_raw_key_never_appears_in_stdout(self):
        raw_key = "sk-proj-ShouldNotLeakAnywhere1234567890abcdefghij"
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, keys_dir = _setup(tmp_path)
            (keys_dir / "openai").write_text(raw_key)
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=keys_dir,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            self.assertNotIn(raw_key, output)
            self.assertNotIn("ShouldNotLeakAnywhere", output)
            # Masked summary IS rendered.
            self.assertIn("***", output)
            # A provider with no key shows the dash placeholder in its
            # API KEY cell ("sk-proj-" has a hyphen too, so read the cell).
            self.assertEqual(strip_ansi(_row_cells(output, "anthropic")[3]).strip(), "-")

    def test_visible_bar_column_gap_matches_swe_list(self):
        # Cross-Table regression: ``swe providers list`` is supposed
        # to render with the same `` │ `` column-boundary pattern as
        # ``swe list`` (harness/commands.py::cmd_list). The bare
        # ``Table()`` form would still produce rows whose visible
        # width matches the header (cpad parity), but the body-row
        # silhouette would lack the vertical-bar substring. This test
        # pins that silhouette so a future revert to ``Table()``
        # is caught rather than silently slipping past the
        # width-parity test.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            plain = strip_ansi(output)
            self.assertIn(
                " │ ", plain,
                f"providers body rows missing ' │ ' visible-bar "
                f"separator (cmd_list should match swe list's "
                f"column_gap pattern): {plain!r}",
            )


class CmdListInfraSectionTest(unittest.TestCase):
    """``swe providers list`` files infra providers under their own
    "Infra Providers" heading after the AI Providers table."""

    def _patches(self, tmp_path: Path):
        config_dir, registry_file, keys_dir = _setup(tmp_path)
        return _registry_patches(config_dir, registry_file) + (
            patch(
                "swe.providers.commands.default_keys_dir",
                return_value=keys_dir,
            ),
        )

    def test_infra_section_renders_after_the_ai_table(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            plain = strip_ansi(output)
            self.assertLess(plain.find("AI Providers"), plain.find("Infra Providers"))
            self.assertGreater(plain.find("Infra Providers"), 0)
            # The AI table still holds exactly the inference defaults;
            # the infra table every infra default.
            ai_body = _body_block(output)
            self.assertEqual(len(ai_body), len(DEFAULT_PROVIDERS))
            lines = plain.split("\n")
            infra_idx = next(i for i, ln in enumerate(lines)
                             if "Infra Providers" in ln)
            # The section ends where the next section's heading begins.
            end = next((i for i, ln in enumerate(lines)
                        if i > infra_idx and "Service Providers" in ln),
                       len(lines))
            infra_body = [
                ln for ln in lines[infra_idx + 4:end]
                if ln.strip()
            ]
            infra_rows = [ln for ln in infra_body if " │ " in ln]
            self.assertEqual(len(infra_rows), len(DEFAULT_INFRA),
                             infra_rows)
            self.assertTrue(
                any("vercel" in ln for ln in infra_rows), infra_rows)

    def test_infra_rows_carry_keys_like_any_provider(self):
        # A VERCEL_TOKEN export shows masked in the infra table.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, _ = _setup(
                tmp_path, create_keys_dir=False)
            shell_keys = tmp_path / ".api_keys"
            shell_keys.write_text(
                "export VERCEL_TOKEN=vtoken-InfraMaskTest12345678\n")
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=shell_keys,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            plain = strip_ansi(output)
            self.assertIn("vtoken-I***5678", plain)
            self.assertNotIn("vtoken-InfraMaskTest12345678", plain)

    def test_filter_infra_shows_only_the_infra_section(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list(["infra"])
            plain = strip_ansi(output)
            # The kind field itself is a filter match, so "infra" picks
            # every infra row and nothing else; the empty AI section is
            # skipped rather than rendered blank.
            self.assertNotIn("AI Providers", plain)
            self.assertIn("Infra Providers", plain)
            self.assertIn("vercel", plain)
            self.assertNotIn("openai", plain)

    def test_filter_an_infra_slug_shows_the_infra_section(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list(["vercel"])
            plain = strip_ansi(output)
            self.assertIn("Infra Providers", plain)
            self.assertIn("vercel", plain)
            self.assertNotIn("openai", plain)

    def test_no_infra_entries_skips_the_section(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2], patch(
                "swe.providers.commands.load_registry",
                return_value=_FAKE_PROVIDERS,
            ):
                output = _run_cmd_list([])
            self.assertNotIn("Infra Providers", strip_ansi(output))


class CmdListServicesSectionTest(unittest.TestCase):
    """``swe providers list`` files service providers under their own
    "Service Providers" heading after the Infra table."""

    def _patches(self, tmp_path: Path):
        config_dir, registry_file, keys_dir = _setup(tmp_path)
        return _registry_patches(config_dir, registry_file) + (
            patch(
                "swe.providers.commands.default_keys_dir",
                return_value=keys_dir,
            ),
        )

    def test_services_section_renders_after_the_infra_table(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            plain = strip_ansi(output)
            self.assertGreater(plain.find("Service Providers"), 0)
            self.assertLess(plain.find("Infra Providers"),
                            plain.find("Service Providers"))
            lines = plain.split("\n")
            svc_idx = next(i for i, ln in enumerate(lines)
                           if "Service Providers" in ln)
            svc_rows = [
                ln for ln in lines[svc_idx + 4:]
                if ln.strip() and " │ " in ln
            ]
            # Rows are the " │ " lines minus the table's footer.
            self.assertEqual(len(svc_rows) - 1, len(DEFAULT_SERVICES),
                             svc_rows)
            self.assertTrue(
                any("firecrawl" in ln for ln in svc_rows), svc_rows)
            self.assertTrue(
                any("agentmail" in ln for ln in svc_rows), svc_rows)

    def test_filter_services_shows_only_the_services_section(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list(["services"])
            plain = strip_ansi(output)
            self.assertNotIn("AI Providers", plain)
            self.assertNotIn("Infra Providers", plain)
            self.assertIn("Service Providers", plain)
            self.assertIn("firecrawl", plain)
            self.assertIn("agentmail", plain)
            self.assertNotIn("openai", plain)


class CmdInfoMigrationTest(unittest.TestCase):
    """Structural invariants for cmd_info post-migration.

    The migration replaces the hand-rolled
    ``f"  {'  ' + label + ':':<20} {val}"`` loop in cmd_info with a
    2-column Table build (LABEL | VALUE); the ``Key file`` row that
    previously was emitted outside the loop is merged into the Table
    build. All label cells are routed through the cyan colour so they
    differentiate from plain text values; values that carry ANSI
    (``Key status`` green-anon, ``Key file`` path cyan-anon) flow
    through Table unmodified via ``trust_cell_width=True``. The label
    column width is pre-measured to ``max(len("  LABEL:"))``.
    """

    # ----------------------------------------------------------------- helpers

    def _patches(self, tmp_path: Path):
        config_dir, registry_file, keys_dir = _setup(tmp_path)
        return _registry_patches(config_dir, registry_file) + (
            patch(
                "swe.providers.commands.default_keys_dir",
                return_value=keys_dir,
            ),
        )

    def _run_cmd_info(self, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_info(list(args))
        return buf.getvalue(), rc

    def _body_rows(self, output: str) -> list[str]:
        """Return Table-rendered body rows (filtered for visible-width parity).

        Body rows carry the full label_cell_width + gap + value_width
        visible length. We pick them out by walking the lines and
        asserting each carries cyan ANSI (the label-side signature).
        """
        return [
            ln for ln in output.split("\n")
            if "\033[36m" in ln and ln.strip()
        ]

    # ---------------------------------------------------------- structural

    def test_label_column_is_cyan(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        # Cyan ANSI escape \033[36m must preface every label cell. We
        # assert at least 7 distinct labels carry it (matches the 7
        # rows in rows_out for a non-key-default provider).
        body_rows = self._body_rows(output)
        self.assertGreaterEqual(
            len(body_rows), 7,
            f"expected ≥7 cyan label rows, got {len(body_rows)}: "
            f"{strip_ansi(output)!r}",
        )
        for ln in body_rows:
            self.assertIn(
                "\033[36m", ln,
                f"label row missing cyan ANSI: {strip_ansi(ln)!r}",
            )

    def test_body_rows_aligned_to_same_visible_width(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, rc = self._run_cmd_info(["openai"])
        self.assertEqual(rc, 0)
        body_rows = self._body_rows(output)
        self.assertEqual(len(body_rows), 9, strip_ansi(output))
        # Rows end at their own value's width (fit="content"), but every
        # value starts in the same column: outer pad (2) + the label cell
        # (15, "  Key filename:") + the gap (1).
        for ln in body_rows:
            plain = strip_ansi(ln)
            start = len(plain) - len(plain.lstrip(" ").split(":", 1)[1].lstrip(" "))
            self.assertEqual(start, 18, f"value misaligned: {plain!r}")

    def test_label_column_width_pre_measured_to_longest_label(self):
        # Production: ``label_cell_width = max(len("  LABEL:") ...)``.
        # The longest label is "  Key filename:" (15 chars), so it sits
        # flush against the gap while shorter labels pad out to it.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        plain = strip_ansi(output).split("\n")
        key_line = next(ln for ln in plain if "Key filename:" in ln)
        self.assertEqual(key_line, "    Key filename: openai")
        desc_line = next(ln for ln in plain if "Description:" in ln)
        self.assertTrue(desc_line.startswith("    Description:  OpenAI API"), repr(desc_line))

    def test_label_inner_pad_two_spaces(self):
        # The label cell carries an inner 2-space pad BEFORE the
        # label name (the original ``"  ' + label + ':'"`` shape).
        # Cyan-wrapped label cell, when stripped, must begin with
        # two spaces then the label name + colon.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        plain = strip_ansi(output)
        # The body-row for Slug begins with the outer 2-space page pad
        # + inner 2-space pad + "Slug:" so the visible substring
        # ``"  Slug:"`` (2 spaces + label+colon) appears verbatim in
        # the output. (Outer page pad prefix is in addition to this.)
        self.assertIn("  Slug:", plain)

    # ------------------------------------------------------------ behaviour

    def test_key_status_green_when_key_present(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, keys_dir = _setup(tmp_path)
            (keys_dir / "openai").write_text(
                "sk-proj-CmdInfoGreenKey1234567890"
            )
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=keys_dir,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        # Green ANSI escape \033[32m present (cpad("green", masked)).
        self.assertIn("\033[32m", output)
        # The masked token (containing "***") is rendered.
        self.assertIn("***", output)

    def test_key_status_dash_when_no_key_file(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        # No key file → row["masked"] == "-" → Key status cell is
        # just the dash (no green ANSI for the dash).
        plain = strip_ansi(output)
        # The dash placeholder appears in Key status.
        self.assertIn("Key status", plain)
        # And it is NOT preceded by green wrap inside the body row
        # for Key status. We assert by walking lines and finding the
        # key-status row, then check it does not start with \033[32m.
        for ln in output.split("\n"):
            if "Key status" in strip_ansi(ln):
                self.assertNotIn(
                    "\033[32m", ln,
                    f"no-key Key status should NOT be green: "
                    f"{strip_ansi(ln)!r}",
                )
                break

    def test_key_file_row_emitted_when_key_file_exists(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, keys_dir = _setup(tmp_path)
            (keys_dir / "openai").write_text(
                "sk-proj-CmdInfoKeyFileRow12345678"
            )
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=keys_dir,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        plain = strip_ansi(output)
        # "Key file" must appear (the row label, cyan-wrapped).
        self.assertIn("Key file:", plain)
        # And the key-file path (without the filename basename to
        # avoid platform-specific Path normalisation) must appear.
        self.assertIn("openai", plain)
        # The Key file value cell uses cyan ANSI for the path.
        # Walk lines and find the row containing "Key file:".
        for ln in output.split("\n"):
            if "Key file:" in strip_ansi(ln):
                self.assertIn(
                    "\033[36m", ln,
                    f"Key file value must use cyan ANSI: "
                    f"{strip_ansi(ln)!r}",
                )
                break

    def test_key_file_row_emits_expected_path_when_no_key_present(self):
        # When ``keys_dir`` exists but no per-provider key file is
        # present, ``discover_provider_keys`` populates
        # ``row["key_file"]`` with the EXPECTED path (str of
        # resolve_path even if disk has nothing there). cmd_info
        # therefore emits the ``Key file`` row showing where the key
        # SHOULD go, paired with the ``No key found`` advisory
        # prompting the user to drop their key there. This is the
        # friendly UX: we don't want users to be unable to find where
        # to put the key just because they haven't yet.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        plain = strip_ansi(output)
        # Key file row IS present (with the expected path).
        self.assertIn("Key file:", plain)
        # The expected key-file path appears in the cyan-wrapped
        # path cell (we can't pin the full ~/.../openai because the
        # keys_dir is a tmp dir under /tmp, so just check the basename).
        self.assertIn("openai", plain)
        # The 'No key found' advisory fires alongside — the pair
        # together tell the user "the key should go here, but it's
        # not there yet".
        self.assertIn("No key found", output)

    def test_em_dash_for_providers_without_aliases(self):
        # mistral ships with no aliases: its Aliases cell shows "—".
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["mistral"])
        self.assertEqual(_info_value(output, "Aliases"), "—")

    def test_em_dash_for_empty_description(self):
        # A provider without a description shows "—" in that cell (every
        # default has one, so this uses a stand-in registry).
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2], patch(
                "swe.providers.commands.load_registry",
                return_value=_FAKE_PROVIDERS,
            ):
                output, _ = self._run_cmd_info(
                    ["very-long-provider-name-that-exceeds-cap"])
                with_desc, _ = self._run_cmd_info(["alpha"])
        self.assertEqual(_info_value(output, "Description"), "—")
        self.assertEqual(_info_value(with_desc, "Description"), "Alpha blurb")

    def test_env_vars_comma_list_when_no_match(self):
        # Shell-export file layout with NO matching export → the
        # env_display path is just ", ".join(env_list) OR "—" if
        # env_list itself is empty.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, _ = _setup(
                tmp_path, create_keys_dir=False
            )
            empty_keys = tmp_path / ".api_keys"
            empty_keys.write_text(
                "# no env exports\n"
                "export UNRELATED=foo\n"
            )
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=empty_keys,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        plain = strip_ansi(output)
        # OPENAI_API_KEY appears (from env_vars list, not from
        # matched_env path).
        self.assertIn("OPENAI_API_KEY", plain)
        # No "(matched; fallbacks: ...)" string in this branch.
        self.assertNotIn("(matched;", plain)

    def test_env_vars_matched_alone_when_single_env_var(self):
        # Provider with exactly one env var + matched export →
        # env_display = matched_env (no "(matched; fallbacks: ...)").
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, _ = _setup(
                tmp_path, create_keys_dir=False
            )
            shell_keys = tmp_path / ".api_keys"
            shell_keys.write_text(
                "export OPENAI_API_KEY=sk-proj-InfoMatched1234567\n"
            )
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=shell_keys,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        plain = strip_ansi(output)
        self.assertIn("OPENAI_API_KEY", plain)
        # Single env var → no "(matched; fallbacks: ...)" line.
        self.assertNotIn("(matched; fallbacks:", plain)

    def test_env_vars_matched_with_fallbacks_when_multi(self):
        # Provider with ≥2 env_vars + a match → "matched_env
        # (matched; fallbacks: ...)" with dim-ANSI wrap on the
        # fallback half.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, _ = _setup(
                tmp_path, create_keys_dir=False
            )
            shell_keys = tmp_path / ".api_keys"
            # kimi has 2 env_vars: KIMI_API_KEY + MOONSHOT_API_KEY.
            # Match MOONSHOT_API_KEY in the shell-file → matched +
            # fallback path.
            shell_keys.write_text(
                "export MOONSHOT_API_KEY=sk-infoFallback12345678\n"
            )
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=shell_keys,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["kimi"])
        plain = strip_ansi(output)
        self.assertIn("MOONSHOT_API_KEY", plain)
        self.assertIn("KIMI_API_KEY", plain)
        self.assertIn("(matched; fallbacks:", plain)
        # A key read from the export file is masked too.
        self.assertNotIn("infoFallback", plain)

    # ------------------------------------------------------------ error paths

    def test_help_flag_returns_zero(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, rc = self._run_cmd_info(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("swe providers", output)

    def test_unknown_provider_returns_one_and_red_error(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, rc = self._run_cmd_info(["does-not-exist"])
        self.assertEqual(rc, 1)
        # Red ANSI escape \033[31m present.
        self.assertIn("\033[31m", output)
        self.assertIn("not found", output.lower())

    def test_no_args_returns_one_and_red_usage(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                output, rc = self._run_cmd_info([])
        self.assertEqual(rc, 1)
        self.assertIn("Usage", output)
        self.assertIn("\033[31m", output)

    # -------------------------------------------------------- advisories + safety

    def test_no_key_found_advisory_when_key_file_present_but_empty(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, keys_dir = _setup(tmp_path)
            # Empty key file exists but raw_key will be None.
            (keys_dir / "openai").write_text("")
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=keys_dir,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        # No-key-found advisory emitted below the table.
        self.assertIn("No key found", output)
        # Advisory uses dim ANSI.
        self.assertIn("\033[2m", output)

    def test_no_advisory_when_raw_key_present(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, keys_dir = _setup(tmp_path)
            (keys_dir / "openai").write_text(
                "sk-proj-CmdInfoNoAdvisory1234567"
            )
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=keys_dir,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        self.assertNotIn("No key found", output)

    def test_raw_key_never_appears_in_stdout(self):
        # Regression: ensure the masked summary is rendered but the
        # raw key string never appears in stdout.
        raw_key = "sk-proj-CmdInfoLeakMustNotHappen1234567890abcdefghij"
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir, registry_file, keys_dir = _setup(tmp_path)
            (keys_dir / "openai").write_text(raw_key)
            patches = _registry_patches(config_dir, registry_file) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=keys_dir,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output, _ = self._run_cmd_info(["openai"])
        self.assertNotIn(raw_key, output)
        self.assertNotIn("LeakMustNotHappen", output)
        self.assertIn("***", output)

    def test_aliases_filter_excludes_canonical_slug(self):
        # A registry's alias list may carry the slug itself; cmd_info
        # shows only the other aliases, or "—" when none are left.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2], patch(
                "swe.providers.commands.load_registry",
                return_value=_FAKE_PROVIDERS,
            ):
                alpha, _ = self._run_cmd_info(["alpha"])
            with patches[0], patches[1], patches[2]:
                openai, _ = self._run_cmd_info(["openai"])
        self.assertEqual(_info_value(alpha, "Aliases"), "al")
        self.assertEqual(_info_value(openai, "Aliases"), "—")

    def test_kind_row_shows_the_registry_kind(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patches[2]:
                openai, _ = self._run_cmd_info(["openai"])
                vercel, _ = self._run_cmd_info(["vercel"])
        self.assertEqual(_info_value(openai, "Kind"), "inference")
        self.assertEqual(_info_value(vercel, "Kind"), "infra")


class CmdAddTest(unittest.TestCase):
    """``providers add`` must not erase the ``_removed`` tombstone list."""

    def _patches(self, tmp_path: Path):
        config_dir = tmp_path / ".config" / "swe"
        registry_file = config_dir / "providers.json"
        config_dir.mkdir(parents=True, exist_ok=True)
        return _registry_patches(config_dir, registry_file), registry_file

    def _run_add(self, args):
        from swe.providers.commands import cmd_add

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_add(list(args))
        return rc, buf.getvalue()

    def _write_registry(self, registry_file: Path, data: dict):
        import json

        registry_file.write_text(json.dumps(data))

    def _read_registry(self, registry_file: Path) -> dict:
        import json

        return json.loads(registry_file.read_text())

    def test_add_preserves_other_tombstones(self):
        # Saving a registry loaded WITHOUT include_removed used to drop
        # the whole _removed list: removed defaults silently came back.
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            self._write_registry(registry_file, {
                "custom": {"env_vars": ["CUSTOM_API_KEY"], "key_filename": "custom"},
                "_removed": ["kimi", "deepseek"],
            })
            with patches[0], patches[1]:
                rc, _ = self._run_add(["newco", "--env=NEWCO_API_KEY"])
            saved = self._read_registry(registry_file)
        self.assertEqual(rc, 0)
        self.assertIn("newco", saved)
        self.assertEqual(sorted(saved["_removed"]), ["deepseek", "kimi"])

    def test_readd_lift_own_tombstone_and_seed_defaults(self):
        # Re-adding a removed builtin lifts only its tombstone and keeps
        # the default fields (env_vars/url) instead of a bare record.
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            self._write_registry(registry_file, {
                "custom": {"env_vars": ["CUSTOM_API_KEY"], "key_filename": "custom"},
                "_removed": ["kimi", "deepseek"],
            })
            with patches[0], patches[1]:
                rc, _ = self._run_add(["kimi"])
            saved = self._read_registry(registry_file)
            loaded = {}
            with patches[0], patches[1]:
                from swe.providers.registry import load_registry
                loaded = load_registry()
        self.assertEqual(rc, 0)
        self.assertEqual(saved["_removed"], ["deepseek"])
        self.assertEqual(saved["kimi"]["env_vars"], DEFAULT_PROVIDERS["kimi"]["env_vars"])
        self.assertIn("kimi", loaded)
        self.assertNotIn("deepseek", loaded)

    def test_add_via_alias_updates_canonical_entry(self):
        # Aliases are derived from env_vars[0] at load — slug "acme" with
        # env BAR_API_KEY carries alias "bar"; `add bar` must update
        # "acme", not fork a duplicate entry — and must keep acme's key
        # filename, not rename it to the alias.
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            self._write_registry(registry_file, {
                "acme": {
                    "env_vars": ["BAR_API_KEY"],
                    "key_filename": "acme",
                },
            })
            with patches[0], patches[1]:
                rc, _ = self._run_add(["bar", "--env=BAR_API_KEY"])
            saved = self._read_registry(registry_file)
        self.assertEqual(rc, 0)
        self.assertNotIn("bar", saved)
        self.assertEqual(saved["acme"]["env_vars"], ["BAR_API_KEY"])
        self.assertEqual(saved["acme"]["key_filename"], "acme")

    def test_add_removed_builtin_by_alias_lifts_tombstone(self):
        # together_ai's derived alias is together-ai; after removal the
        # alias isn't resolvable in the live registry, so `add
        # together-ai` used to fork a bare "together-ai" entry while
        # together_ai stayed tombstoned.
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            self._write_registry(registry_file, {
                "_removed": ["together_ai", "deepseek"],
            })
            with patches[0], patches[1]:
                rc, _ = self._run_add(["together-ai"])
            saved = self._read_registry(registry_file)
        self.assertEqual(rc, 0)
        self.assertNotIn("together-ai", saved)
        self.assertIn("together_ai", saved)
        self.assertEqual(saved["_removed"], ["deepseek"])
        self.assertEqual(
            saved["together_ai"]["key_filename"],
            DEFAULT_PROVIDERS["together_ai"]["key_filename"],
        )
        self.assertEqual(
            saved["together_ai"]["env_vars"],
            DEFAULT_PROVIDERS["together_ai"]["env_vars"],
        )

    def test_add_rejects_a_flag_token_as_the_name(self):
        # `providers add --env=FOO_API_KEY` used to register a provider
        # literally named "--env": after expansion the flag sat in the
        # name slot and was never validated.
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            with patches[0], patches[1]:
                rc, out = self._run_add(["--env=FOO_API_KEY"])
        self.assertEqual(rc, 1)
        self.assertIn("--env", out)
        self.assertFalse(registry_file.exists())

    def test_add_rejects_a_short_flag_as_the_name(self):
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            with patches[0], patches[1]:
                rc, out = self._run_add(["-d"])
        self.assertEqual(rc, 1)
        self.assertIn("not valid", out)

    def test_add_registers_a_new_provider(self):
        from swe.providers.registry import load_registry

        with TemporaryDirectory() as tmp:
            patches, _ = self._patches(Path(tmp))
            with patches[0], patches[1]:
                rc, _ = self._run_add([
                    "myprov", "My Provider blurb", "--env=MY_API_KEY",
                    "--url=https://example.com", "--file=myprov",
                ])
                providers = load_registry()
        self.assertEqual(rc, 0)
        info = providers["myprov"]
        self.assertEqual(info["url"], "https://example.com")
        self.assertEqual(info["env_vars"], ["MY_API_KEY"])
        self.assertEqual(info["key_filename"], "myprov")
        # `name` and `aliases` are derived from env_vars[0] at load time
        # (API_KEY is the source of truth); the blurb only lands in
        # `description`.
        self.assertEqual(info["aliases"], ["my"])
        self.assertEqual(info["name"], "My")
        self.assertEqual(info["description"], "My Provider blurb")

    def test_add_rejects_an_unknown_flag(self):
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            with patches[0], patches[1]:
                rc, _ = self._run_add(["myprov", "--aliases=my-provider"])
        self.assertEqual(rc, 1)
        self.assertFalse(registry_file.exists())

    def test_add_kind_infra_persists_and_echoes(self):
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            with patches[0], patches[1]:
                rc, out = self._run_add(
                    ["myinfra", "--kind=infra", "--env=MY_TOKEN"])
            saved = self._read_registry(registry_file)
        self.assertEqual(rc, 0)
        self.assertEqual(saved["myinfra"]["kind"], "infra")
        self.assertIn("kind: infra", out)

    def test_add_kind_services_persists_and_echoes(self):
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            with patches[0], patches[1]:
                rc, out = self._run_add(
                    ["myapi", "--kind=services", "--env=MY_TOKEN"])
            saved = self._read_registry(registry_file)
        self.assertEqual(rc, 0)
        self.assertEqual(saved["myapi"]["kind"], "services")
        self.assertIn("kind: services", out)

    def test_add_defaults_kind_to_inference(self):
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            with patches[0], patches[1]:
                rc, out = self._run_add(["myprov", "--env=MY_API_KEY"])
            saved = self._read_registry(registry_file)
        self.assertEqual(rc, 0)
        self.assertEqual(saved["myprov"]["kind"], "inference")
        # The confirmation stays quiet about the default kind.
        self.assertNotIn("kind:", out)

    def test_add_rejects_an_unknown_kind(self):
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            with patches[0], patches[1]:
                rc, out = self._run_add(
                    ["myprov", "--kind=cloud", "--env=MY_API_KEY"])
        self.assertEqual(rc, 1)
        self.assertIn("Unknown kind", out)
        self.assertFalse(registry_file.exists())

    def test_update_preserves_kind_without_the_flag(self):
        # Re-adding an infra entry without --kind must not file it back
        # under inference.
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            self._write_registry(registry_file, {
                "myinfra": {"env_vars": ["MY_TOKEN"], "kind": "infra",
                            "key_filename": "myinfra"},
            })
            with patches[0], patches[1]:
                rc, _ = self._run_add(["myinfra", "--url=https://x"])
            saved = self._read_registry(registry_file)
        self.assertEqual(rc, 0)
        self.assertEqual(saved["myinfra"]["kind"], "infra")

    def test_readd_removed_infra_builtin_restores_kind_and_vars(self):
        # A removed infra default re-adds with its catalog kind and env
        # vars, not a bare inference record.
        with TemporaryDirectory() as tmp:
            patches, registry_file = self._patches(Path(tmp))
            self._write_registry(registry_file, {"_removed": ["vercel"]})
            with patches[0], patches[1]:
                rc, _ = self._run_add(["vercel"])
            saved = self._read_registry(registry_file)
        self.assertEqual(rc, 0)
        self.assertEqual(saved["vercel"]["kind"], "infra")
        self.assertEqual(saved["vercel"]["env_vars"],
                         DEFAULT_INFRA["vercel"]["env_vars"])
        # Lifting the only tombstone drops the bookkeeping key entirely.
        self.assertNotIn("vercel", saved.get("_removed") or [])


class CmdRemoveTest(unittest.TestCase):
    def test_add_then_remove_roundtrip(self):
        from swe.providers.commands import cmd_add, cmd_remove
        from swe.providers.registry import load_registry

        with TemporaryDirectory() as tmp:
            config_dir, registry_file, keys_dir = _setup(Path(tmp))
            patches = _registry_patches(config_dir, registry_file)
            with patches[0], patches[1], patch(
                "swe.providers.commands.default_keys_dir",
                return_value=keys_dir,
            ), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cmd_add(["myprov", "--env=MY_KEY"]), 0)
                self.assertIn("myprov", load_registry())
                self.assertEqual(cmd_remove(["myprov"]), 0)
                self.assertNotIn("myprov", load_registry())
                # A second remove reports the provider as not found.
                self.assertEqual(cmd_remove(["myprov"]), 1)
        self.assertIn("Provider 'myprov' not found", out.getvalue())

    def test_remove_keeps_the_key_file(self):
        from swe.providers.commands import cmd_remove

        with TemporaryDirectory() as tmp:
            config_dir, registry_file, keys_dir = _setup(Path(tmp))
            key_file = keys_dir / "openai"
            key_file.write_text("sk-proj-DontDeleteMe1234567890")
            patches = _registry_patches(config_dir, registry_file)
            with patches[0], patches[1], patch(
                "swe.providers.commands.default_keys_dir",
                return_value=keys_dir,
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_remove(["openai"]), 0)
            self.assertEqual(key_file.read_text(), "sk-proj-DontDeleteMe1234567890")


class CmdRemoveHelpTest(unittest.TestCase):
    def test_remove_help_token_prints_help_not_lookup(self):
        # `providers remove --help` used to look up a provider literally
        # named "--help" and report it missing.
        from swe.providers.commands import cmd_remove

        # The help token is checked against the live registry (a provider
        # could legitimately alias "help"), so patch the registry paths
        # to keep the test hermetic.
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".config" / "swe"
            registry_file = config_dir / "providers.json"
            config_dir.mkdir(parents=True, exist_ok=True)
            patches = _registry_patches(config_dir, registry_file)
            for argv in (["--help"], ["-h"], ["help"]):
                with self.subTest(argv=argv):
                    buf = io.StringIO()
                    with patches[0], patches[1], redirect_stdout(buf):
                        rc = cmd_remove(argv)
                    self.assertEqual(rc, 0)
                    self.assertIn("swe providers remove", buf.getvalue())

    def test_remove_can_still_remove_a_provider_named_help(self):
        # `help` is a valid derived alias (env HELP_API_KEY) — the help
        # route must yield when the token resolves to a real provider.
        from swe.providers.commands import cmd_remove

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".config" / "swe"
            registry_file = config_dir / "providers.json"
            config_dir.mkdir(parents=True, exist_ok=True)
            import json

            registry_file.write_text(json.dumps({
                "helper": {"env_vars": ["HELP_API_KEY"], "key_filename": "helper"},
            }))
            patches = _registry_patches(config_dir, registry_file)
            buf = io.StringIO()
            with patches[0], patches[1], redirect_stdout(buf):
                rc = cmd_remove(["help"])
            saved = json.loads(registry_file.read_text())
        self.assertEqual(rc, 0)
        self.assertIn("Removed 'helper'", buf.getvalue())
        self.assertNotIn("helper", saved)
        self.assertIn("helper", saved["_removed"])

    def test_remove_help_survives_a_broken_registry(self):
        # A corrupt providers.json must not block `remove --help`:
        # help is exactly what a user needs when the registry broke.
        from swe.providers.commands import cmd_remove

        for argv in (["--help"], ["-h"], ["help"]):
            with self.subTest(argv=argv):
                buf = io.StringIO()
                with patch(
                    "swe.providers.commands.load_registry",
                    side_effect=Exception("corrupt"),
                ), redirect_stdout(buf):
                    rc = cmd_remove(argv)
                self.assertEqual(rc, 0)
                self.assertIn("swe providers remove", buf.getvalue())


class CmdStoreWordingTest(unittest.TestCase):
    """Printed key-store instructions must match the layout that is read.

    The default store is one shell-export file
    (``~/.swe/secrets/providers/.api_keys``); a directory of
    bare-key files only exists under ``--api-keys-dir=DIR`` or a legacy
    ``~/.api_keys/`` directory.
    """

    def setUp(self):
        # A home outside the temp folders, so printed key paths stay
        # absolute (not "~/...") even when TMPDIR sits under HOME.
        self.enterContext(patch.object(Path, "home", return_value=Path("/nonexistent-home")))

    def _patches(self, tmp_path: Path):
        return _registry_patches(
            tmp_path / ".config" / "swe",
            tmp_path / ".config" / "swe" / "providers.json",
        )

    def _add(self, tmp_path: Path, store: Path) -> str:
        from swe.providers.commands import cmd_add

        buf = io.StringIO()
        patches = self._patches(tmp_path)
        with patches[0], patches[1], patch(
            "swe.providers.commands.default_keys_dir", return_value=store,
        ), redirect_stdout(buf):
            self.assertEqual(
                cmd_add(["myprov", "--env=MY_API_KEY", "--file=myprov"]), 0)
        return buf.getvalue()

    def test_add_hint_points_at_the_export_file(self):
        # The default store is one file of export lines; a directory of
        # per-provider files is only for --api-keys-dir overrides.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / "keys" / ".api_keys"
            store.parent.mkdir()
            store.write_text("export OLD=1\n")
            out = self._add(tmp_path, store)
        self.assertIn("export MY_API_KEY", out)
        self.assertIn(str(store), out)
        self.assertNotIn(str(store / "myprov"), out)

    def test_add_hint_points_inside_a_real_keys_directory(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / ".api_keys"
            store.mkdir()
            out = self._add(tmp_path, store)
        self.assertIn(str(store / "myprov"), out)

    def test_api_keys_dir_flag_overrides_the_default_store(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            default = tmp_path / ".api_keys"
            default.mkdir()
            other = tmp_path / "other_keys"
            other.mkdir()
            (other / "openai").write_text("sk-proj-OtherDirTest1234567")
            patches = self._patches(tmp_path)
            with patches[0], patches[1], patch(
                "swe.providers.commands.default_keys_dir", return_value=default,
            ):
                output = strip_ansi(_run_cmd_list([f"--api-keys-dir={other}"]))
        # The key comes from the override, not the default dir.
        self.assertIn("sk-proj-***4567 (27)", output)
        self.assertIn(f"dir: {other}", output)

    def test_footer_names_the_store_through_the_display_helper(self):
        with TemporaryDirectory() as tmp:
            patches = self._patches(Path(tmp))
            with patches[0], patches[1], patch(
                "swe.providers.commands._display_keys_dir",
                return_value="~/.api_keys",
            ):
                output = _run_cmd_list([])
        self.assertIn("~/.api_keys", output)
        self.assertNotIn(str(Path.home()), output)

    def test_display_keys_dir_tildes_only_under_home(self):
        from swe.providers.commands import _display_keys_dir

        home = Path("/Users/someone")
        with patch.object(Path, "home", return_value=home):
            self.assertEqual(_display_keys_dir(home / ".api_keys"), "~/.api_keys")
            self.assertEqual(_display_keys_dir(home / "projects" / "keys"),
                             "~/projects/keys")
            self.assertEqual(_display_keys_dir(Path("/tmp/somewhere/else")),
                             "/tmp/somewhere/else")

    def test_missing_default_store_advises_export_lines(self):
        # The default store is a single file: telling users to "add a file
        # per provider" made them create a directory the app's key editor
        # cannot write.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing_store = tmp_path / ".swe" / "secrets" / "providers" / ".api_keys"
            patches = self._patches(tmp_path) + (
                patch(
                    "swe.providers.commands.default_keys_dir",
                    return_value=missing_store,
                ),
            )
            with patches[0], patches[1], patches[2]:
                output = _run_cmd_list([])
            self.assertIn("No key store", output)
            self.assertIn("export NAME=<key>", output)
            self.assertNotIn("file per provider", output)

    def test_missing_custom_dir_offers_both_layouts(self):
        # An explicit --api-keys-dir that does not exist yet can honestly
        # become either layout; the advice names both.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = self._patches(tmp_path)
            missing_dir = tmp_path / "keys"
            with patches[0], patches[1]:
                output = _run_cmd_list([f"--api-keys-dir={missing_dir}"])
            self.assertIn("No key store", output)
            self.assertIn("one bare-key file per provider", output)
            self.assertIn("export NAME=<key>", output)

    def _remove_with_store(self, tmp_path: Path, store: Path,
                           env_vars=("OPENAI_API_KEY",)) -> str:
        import json

        from swe.providers.commands import cmd_remove

        config_dir = tmp_path / ".config" / "swe"
        registry_file = config_dir / "providers.json"
        config_dir.mkdir(parents=True, exist_ok=True)
        registry_file.write_text(json.dumps({
            "openai": {"env_vars": list(env_vars), "key_filename": "openai"},
        }))
        patches = _registry_patches(config_dir, registry_file) + (
            patch(
                "swe.providers.commands.default_keys_dir",
                return_value=store,
            ),
        )
        buf = io.StringIO()
        with patches[0], patches[1], patches[2], redirect_stdout(buf):
            rc = cmd_remove(["openai"])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_remove_names_the_export_line_for_a_file_store(self):
        # With the real store the key is a line inside .api_keys, not a
        # file at ".api_keys/openai".
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / ".api_keys"
            store.write_text("export OPENAI_API_KEY=sk-x\n")
            out = self._remove_with_store(tmp_path, store)
        self.assertIn("OPENAI_API_KEY", out)
        self.assertIn(str(store), out)
        self.assertNotIn(f"{store}/openai", out)

    def test_remove_names_the_key_file_for_a_directory_store(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / "keys"
            store.mkdir()
            out = self._remove_with_store(tmp_path, store)
        self.assertIn(f"{store}/openai", out)

    def test_remove_names_the_export_the_file_actually_holds(self):
        # env_vars[0] is only the first accepted name: a provider whose
        # secondary name is the one exported must not be told its
        # primary-named line survived.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / ".api_keys"
            store.write_text("export SECONDARY_API_KEY=sk-x\n")
            out = self._remove_with_store(
                tmp_path, store,
                env_vars=("PRIMARY_API_KEY", "SECONDARY_API_KEY"),
            )
        self.assertIn("SECONDARY_API_KEY", out)
        self.assertNotIn("PRIMARY_API_KEY", out)

    def test_remove_lists_accepted_names_when_no_key_is_stored(self):
        # No matching export in the file — name all accepted names rather
        # than guessing env_vars[0].
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / ".api_keys"
            store.write_text("")
            out = self._remove_with_store(
                tmp_path, store,
                env_vars=("PRIMARY_API_KEY", "SECONDARY_API_KEY"),
            )
        self.assertIn("PRIMARY_API_KEY or SECONDARY_API_KEY", out)

    def test_remove_survives_a_store_with_invalid_utf8(self):
        # A non-UTF-8 .api_keys used to raise UnicodeDecodeError after the
        # removal was already saved — traceback on a completed remove.
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / ".api_keys"
            store.write_bytes(b"export PRIMARY_API_KEY=\xff\xfe\n")
            out = self._remove_with_store(
                tmp_path, store,
                env_vars=("PRIMARY_API_KEY", "SECONDARY_API_KEY"),
            )
        self.assertIn("Removed 'openai'", out)
        self.assertIn("PRIMARY_API_KEY or SECONDARY_API_KEY", out)


if __name__ == "__main__":
    unittest.main()

